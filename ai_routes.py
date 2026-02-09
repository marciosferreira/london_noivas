import os
import json
import pickle
import faiss
import numpy as np
import boto3
from boto3.dynamodb.conditions import Key
from flask import Blueprint, request, jsonify, url_for, current_app
from openai import OpenAI
from dotenv import load_dotenv
from ai_sync_service import get_index_status, sync_index as service_sync_index

# Configuração
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Inicializa DynamoDB (Para validação de consistência)
try:
    dynamodb = boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    itens_table = dynamodb.Table("alugueqqc_itens")
except Exception as e:
    print(f"Erro ao conectar DynamoDB em ai_routes: {e}")
    itens_table = None

ai_bp = Blueprint('ai', __name__)

INDEX_FILE = "vector_store.index"
METADATA_FILE = "vector_store_metadata.pkl"
EMBEDDING_MODEL = "text-embedding-3-small"

# Carrega recursos globais
index = None
metadata = []

def load_resources():
    global index, metadata
    try:
        if os.path.exists(INDEX_FILE) and os.path.exists(METADATA_FILE):
            index = faiss.read_index(INDEX_FILE)
            with open(METADATA_FILE, "rb") as f:
                metadata = pickle.load(f)
            print("Recursos de IA carregados com sucesso.")
        else:
            print("Arquivos de índice/metadata não encontrados. A busca AI pode não funcionar.")
    except Exception as e:
        print(f"Erro ao carregar recursos de IA: {e}")

# Carrega na inicialização
load_resources()

def get_embedding(text):
    text = text.replace("\n", " ")
    return client.embeddings.create(input=[text], model=EMBEDDING_MODEL).data[0].embedding

def validate_and_enrich_candidates(candidates_metadata):
    """
    Valida se os itens existem no DynamoDB e enriquece com dados frescos (incluindo URL S3).
    Retorna lista filtrada e enriquecida.
    OTIMIZADO: Usa BatchGetItem para reduzir latência.
    """
    if not itens_table or not candidates_metadata:
        return candidates_metadata # Fallback se DB off

    valid_items = []
    
    # 1. Deduplicar e coletar IDs
    unique_candidates = {} # item_id -> metadata
    keys_to_fetch = []
    
    for meta in candidates_metadata:
        item_id = meta.get('custom_id')
        if item_id and item_id not in unique_candidates:
            unique_candidates[item_id] = meta
            keys_to_fetch.append({'item_id': item_id})
    
    if not keys_to_fetch:
        return []

    # 2. BatchGetItem (DynamoDB limite de 100 itens por request)
    fetched_items = {}
    
    try:
        # Divide em chunks de 100 se necessário
        chunk_size = 100
        for i in range(0, len(keys_to_fetch), chunk_size):
            chunk = keys_to_fetch[i:i + chunk_size]
            
            response = dynamodb.batch_get_item(
                RequestItems={
                    'alugueqqc_itens': {
                        'Keys': chunk,
                        'ProjectionExpression': 'item_id, #st, title, item_description, item_image_url, item_value, item_custom_id, category',
                        'ExpressionAttributeNames': {'#st': 'status'}
                    }
                }
            )
            
            # Processa itens retornados
            for item in response.get('Responses', {}).get('alugueqqc_itens', []):
                fetched_items[item['item_id']] = item
            
            # Lidar com UnprocessedKeys se houver throttling (opcional, mas recomendado)
            # Para simplicidade, assumimos que não haverá throttling massivo para < 100 itens
            
    except Exception as e:
        print(f"Erro no BatchGetItem: {e}")
        # Fallback: se falhar o batch, retorna lista vazia ou tenta individual (vamos retornar vazio por segurança)
        return []

    # 3. Cruzar dados e enriquecer
    # Mantém a ordem original do FAISS (relevância)
    seen_ids = set()
    for meta in candidates_metadata:
        item_id = meta.get('custom_id')
        if not item_id or item_id in seen_ids:
            continue
            
        db_item = fetched_items.get(item_id)
        
        if db_item:
            # Verificar status (apenas available)
            if db_item.get('status') != 'available':
                continue

            # Item válido! Enriquecer.
            seen_ids.add(item_id)
            
            # S3 URL
            s3_url = db_item.get('item_image_url')
            if s3_url:
                meta['imageUrl'] = s3_url
                meta['file_name'] = None
            
            # Metadata frescos
            meta['title'] = db_item.get('title', meta.get('title'))
            meta['description'] = db_item.get('item_description', meta.get('description'))
            
            # Preço
            val = db_item.get('item_value', 0)
            if val:
                meta['price'] = f"R$ {val}"
            
            # Custom ID (Human Readable)
            meta['customId'] = db_item.get('item_custom_id')
            # UUID (System ID)
            meta['item_id'] = db_item.get('item_id')
            # Category
            meta['category'] = db_item.get('category', 'Festa')
            
            valid_items.append(meta)
            
    return valid_items

def execute_catalog_search(query, k=5):
    """
    Executa a busca semântica no catálogo.
    Retorna uma lista de itens enriquecidos (dicionários).
    """
    global index, metadata
    
    if not index or not metadata:
        # Tenta recarregar
        load_resources()
        if not index or not metadata:
            print("Erro: Índice não disponível para execute_catalog_search")
            return []

    try:
        # 1. Gera embedding
        query_embedding = get_embedding(query)
        query_vector = np.array([query_embedding]).astype('float32')

        # 2. Busca no FAISS
        # Buscamos 2x mais para garantir itens suficientes após validação
        search_k = k * 2
        distances, indices = index.search(query_vector, search_k)
        
        # 3. Coleta metadados
        raw_candidates = []
        for idx in indices[0]:
            if idx != -1:
                raw_candidates.append(metadata[idx].copy())

        # 4. Valida e Enriquece
        valid_candidates = validate_and_enrich_candidates(raw_candidates)
        
        # Limita ao k solicitado
        return valid_candidates[:k]

    except Exception as e:
        print(f"Erro em execute_catalog_search: {e}")
        return []

@ai_bp.route('/api/ai-search', methods=['POST'])
def ai_search():
    data = request.get_json()
    user_message = data.get('message', '')
    history = data.get('history', [])

    if not user_message:
        return jsonify({"error": "Mensagem vazia."}), 400

    # System Prompt: Persona Bella (Agente Vendedora)
    system_prompt = (
        "Você é a Bella, consultora sênior e estilista da London Noivas. "
        "Sua missão é entender o sonho da cliente e encontrar o vestido perfeito. "
        "PERSONALIDADE:\n"
        "- Empática, sofisticada e proativa. Use emojis com moderação (✨, 👗).\n"
        "- Aja como uma consultora real: não apenas entregue links, mas 'venda' o vestido destacando detalhes que combinam com o pedido.\n"
        "- Sempre faça perguntas de follow-up ('O que achou do decote?', 'Prefere algo mais armado?').\n"
        "REGRAS DE RESPOSTA:\n"
        "- Ao apresentar vestidos, descreva no texto APENAS O PRIMEIRO (o mais relevante). Não liste os outros por escrito.\n"
        "- Diga algo como 'Separei esta opção principal que... e deixei mais sugestões nas imagens abaixo'.\n"
        "- O foco do texto deve ser a conexão emocional com a escolha principal.\n"
        "- SEMPRE finalize com uma pergunta de follow-up. Ex: 'Então, o que achou deste modelo? Gostaria de ver algo com mais brilho ou renda?'\n"
        "USO DE FERRAMENTAS:\n"
        "- Use a tool `search_dresses` quando o usuário pedir sugestões, descrever um estilo ou demonstrar intenção de compra.\n"
        "- Se a busca não retornar nada, peça desculpas e sugira ampliar os critérios."
    )

    # Constrói mensagens
    messages = [{"role": "system", "content": system_prompt}]
    
    # Adiciona histórico (simplificado)
    if history:
        for msg in history:
            if isinstance(msg, dict) and msg.get('role') in ['user', 'assistant'] and msg.get('content'):
                messages.append({"role": msg['role'], "content": msg['content']})
    
    messages.append({"role": "user", "content": user_message})

    # Definição das Tools
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_dresses",
                "description": "Busca vestidos no catálogo por descrição visual, estilo, cor ou ocasião.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Descrição rica para busca semântica. Ex: 'vestido sereia renda ombro a ombro'"},
                        "k": {"type": "integer", "default": 5}
                    },
                    "required": ["query"]
                }
            }
        }
    ]

    found_suggestions = []
    
    try:
        # 1. Decisão do Agente
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        
        response_message = response.choices[0].message
        reply_text = ""

        # 2. Execução de Tools (se houver)
        if response_message.tool_calls:
            messages.append(response_message) # Adiciona a intenção de tool call ao histórico
            
            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "search_dresses":
                    args = json.loads(tool_call.function.arguments)
                    query = args.get("query")
                    k_val = args.get("k", 5)
                    
                    # Executa busca
                    results = execute_catalog_search(query, k=k_val)
                    
                    # Formata para Frontend (Objetos Completos)
                    for item in results:
                        found_suggestions.append({
                            "id": item.get('item_id') or item.get('custom_id'),
                            "customId": item.get('customId'),
                            "title": item.get('title', 'Vestido Exclusivo'),
                            "description": item.get('description', ''),
                            "price": item.get('price', "Consulte valor"),
                            "category": item.get('category', 'Festa'),
                            "image_url": item.get('imageUrl') or url_for('static', filename=f"dresses/{item['file_name']}")
                        })
                    
                    # Formata para LLM (Resumo)
                    summary = json.dumps([{
                        "id": item.get('custom_id'),
                        "title": item.get('title'),
                        "desc": item.get('description')[:200]
                    } for item in results], ensure_ascii=False)
                    
                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": "search_dresses",
                        "content": summary
                    })
            
            # 3. Resposta Final Pós-Tool
            final_response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages
            )
            reply_text = final_response.choices[0].message.content
            
        else:
            # Apenas conversa
            reply_text = response_message.content

        # 4. Retorno ao Frontend
        # Separa o principal das sugestões para evitar duplicidade na grid
        main_dress = found_suggestions[0] if found_suggestions else None
        # Limita a 4 sugestões adicionais (índices 1 a 4)
        other_suggestions = found_suggestions[1:5] if len(found_suggestions) > 1 else []

        return jsonify({
            "reply": reply_text,
            "suggestions": other_suggestions,
            "dress": main_dress
        })

    except Exception as e:
        print(f"Erro no Agente Bella: {e}")
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/ai-similar/<item_id>', methods=['GET'])
def ai_similar(item_id):
    global index, metadata
    
    if not index or not metadata:
        load_resources()
        if not index or not metadata:
            return jsonify({"error": "Sistema indisponível"}), 503

    # Encontrar índice do item
    target_idx = -1
    for i, item in enumerate(metadata):
        # Comparação robusta (str vs str)
        if str(item.get('custom_id')) == str(item_id):
            target_idx = i
            break
    
    if target_idx == -1:
        return jsonify({"error": "Item não encontrado no índice"}), 404

    try:
        # Reconstruir vetor do item (assumindo IndexFlatL2 ou similar que suporte reconstruct)
        # Se o índice não suportar, precisaríamos recalcular o embedding via OpenAI (custo extra)
        # ou armazenar os vetores separadamente.
        # IndexFlatL2 suporta reconstruct.
        query_vector = index.reconstruct(target_idx).reshape(1, -1)
        
        # Busca
        k = 5 # 1 (ele mesmo) + 4 similares
        distances, indices = index.search(query_vector, k)
        
        # Coleta metadados brutos
        raw_suggestions = []
        for idx in indices[0]:
            if idx != -1 and idx != target_idx:
                raw_suggestions.append(metadata[idx].copy())

        # Validação contra Banco de Dados
        valid_suggestions = validate_and_enrich_candidates(raw_suggestions)

        suggestions = []
        for item in valid_suggestions:
            suggestions.append({
                "id": item.get('item_id') or item.get('custom_id'),
                "customId": item.get('customId'),
                "title": item.get('title', 'Vestido'),
                "image_url": item.get('imageUrl') or url_for('static', filename=f"dresses/{item['file_name']}"),
                "description": item.get('description', ''),
                "category": item.get('category', 'Festa')
            })
                
        return jsonify({"suggestions": suggestions})

    except Exception as e:
        print(f"Erro ao buscar similares: {e}")
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/ai-catalog-search', methods=['POST'])
def ai_catalog_search():
    global index, metadata
    
    if not index or not metadata:
        load_resources()
        if not index or not metadata:
            return jsonify({"error": "Sistema indisponível"}), 503

    data = request.get_json()
    query = data.get('query', '')
    page = int(data.get('page', 1))
    limit = int(data.get('limit', 12))
    
    if not query:
        return jsonify({"error": "Query vazia"}), 400

    try:
        # 1. Gera embedding da query
        query_embedding = get_embedding(query)
        query_vector = np.array([query_embedding]).astype('float32')
        
        # 2. Busca no FAISS
        # Buscamos mais itens (100) para garantir que após o filtro de status/DB
        # ainda tenhamos itens suficientes para preencher as páginas.
        k = 100 
        distances, indices = index.search(query_vector, k)
        
        # Coleta metadados brutos
        raw_results = []
        for idx in indices[0]:
            if idx != -1:
                raw_results.append(metadata[idx].copy())

        # Validação contra Banco de Dados (agora otimizada com BatchGetItem)
        # Isso é rápido mesmo para 100 itens (~1 call DynamoDB)
        valid_results = validate_and_enrich_candidates(raw_results)

        # Filtragem por Categoria (Aba Ativa)
        target_category = data.get('category', '').lower().strip()
        
        if target_category:
            filtered_results = []
            for item in valid_results:
                item_cat = item.get('category', '').lower().strip()
                is_noiva = item_cat in ['noiva', 'noivas']
                
                if target_category == 'noiva':
                    if is_noiva:
                        filtered_results.append(item)
                else: # festa (padrão para qualquer outra categoria)
                    if not is_noiva:
                        filtered_results.append(item)
            
            valid_results = filtered_results

        # 3. Paginação em memória
        total_valid = len(valid_results)
        total_pages = (total_valid + limit - 1) // limit
        
        # Ajusta página se fora dos limites
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages
        
        start = (page - 1) * limit
        end = start + limit
        
        paginated_items = valid_results[start:end]

        results = []
        for item in paginated_items:
            results.append({
                "item_id": item.get('item_id') or item.get('custom_id'), # UUID preferido
                "title": item.get('title', 'Vestido'),
                "imageUrl": item.get('imageUrl') or url_for('static', filename=f"dresses/{item['file_name']}"),
                "description": item.get('description', ''),
                "price": item.get('price', "Consulte o preço do aluguel"),
                "customId": item.get('customId'), # Adicionado para consistência
                "category": item.get('category', 'Festa')
            })
                
        return jsonify({
            "results": results,
            "page": page,
            "total_pages": total_pages,
            "total_results": total_valid
        })

    except Exception as e:
        print(f"Erro na busca do catálogo: {e}")
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/admin/ai-status', methods=['GET'])
def admin_ai_status():
    """Retorna o status de sincronização do índice IA"""
    try:
        status = get_index_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/admin/sync-ai-index', methods=['POST'])
def admin_sync_ai_index():
    """Aciona a sincronização do índice IA"""
    try:
        result = service_sync_index()
        if result.get('status') == 'success':
            # Recarrega os recursos na memória da aplicação
            load_resources()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
