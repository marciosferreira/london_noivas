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
                        'ProjectionExpression': 'item_id, #st, title, item_description, item_image_url, item_value, item_custom_id',
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
            
            valid_items.append(meta)
            
    return valid_items

@ai_bp.route('/api/ai-search', methods=['POST'])
def ai_search():
    global index, metadata
    
    if not index or not metadata:
        # Tenta carregar novamente se falhou na inicialização ou se foi criado depois
        load_resources()
        if not index or not metadata:
            return jsonify({"error": "Sistema de busca indisponível no momento."}), 503

    data = request.get_json()
    user_message = data.get('message', '')
    history = data.get('history', [])

    if not user_message:
        return jsonify({"error": "Mensagem vazia."}), 400

    try:
        # 1. Extração da essência com GPT-4o
        extraction_prompt = (
            "Você é um assistente de moda da London Noivas. "
            "Sua tarefa é extrair as características chave para busca (Ocasião, Estilo, Material, Cor, Detalhes) baseando-se na conversa atual. "
            "IMPORTANTE: Considere todo o histórico da conversa para entender o contexto. "
            "Se o usuário disser 'tem azul desse?', você deve recuperar o estilo do vestido mencionado anteriormente e buscar por esse estilo na cor azul. "
            "Se for apenas uma saudação ou conversa fiada sem intenção de busca, retorne 'search_query' vazio e um 'reply' amigável."
            "Se houver intenção de busca, retorne 'search_query' com a descrição refinada e 'reply' vazio (será gerado depois)."
            "Retorne a resposta SEMPRE em JSON no formato: {\"reply\": \"...\", \"search_query\": \"...\"}"
        )

        messages = [{"role": "system", "content": extraction_prompt}]
        
        # Adiciona histórico se existir, caso contrário usa apenas a mensagem atual
        if history:
            # Garante que o histórico está no formato correto
            for msg in history:
                if isinstance(msg, dict) and 'role' in msg and 'content' in msg:
                    messages.append(msg)
        else:
            messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            response_format={"type": "json_object"}
        )
        
        gpt_result = json.loads(response.choices[0].message.content)
        search_query = gpt_result.get("search_query", "")
        
        # Se não houver query de busca (apenas papo furado), retorna a resposta do GPT imediatamente
        if not search_query:
            return jsonify({
                "reply": gpt_result.get("reply", "Como posso ajudar?"),
                "dress": None
            })

        # 2. Gera embedding da query refinada
        query_embedding = get_embedding(search_query)
        query_vector = np.array([query_embedding]).astype('float32')

        # 3. Busca no FAISS (TOP 10 para garantir pelo menos 4 sugestões após filtros)
        k = 10
        distances, indices = index.search(query_vector, k)
        
        candidates = []
        
        # Coleta metadados brutos
        raw_candidates = []
        for idx in indices[0]:
            if idx != -1:
                raw_candidates.append(metadata[idx].copy()) # Copy para não alterar cache global

        # Validação contra Banco de Dados (Check de Deletados + S3 URL)
        candidates = validate_and_enrich_candidates(raw_candidates)

        if not candidates:
            return jsonify({
                "reply": "Desculpe, não encontrei nenhum vestido correspondente no momento.",
                "dress": None,
                "suggestions": []
            })
            
        # Formata os candidatos para o prompt
        candidates_str = ""
        for i, c in enumerate(candidates):
            candidates_str += f"OPÇÃO {i}: {c['description']}\n\n"

        # ... (rest of function)

        # 4. Seleção Inteligente e Resposta Final
        final_response_prompt = (
            "Você é um consultor de moda experiente e elegante da London Noivas. "
            f"O usuário solicitou: '{search_query}'. "
            "Abaixo estão as melhores opções encontradas no nosso acervo:\n\n"
            f"{candidates_str}"
            "Sua tarefa é duplo: "
            "1. ESCOLHER A MELHOR OPÇÃO entre as listadas que melhor atende ao pedido (considere cor, estilo, detalhes). "
            "2. APRESENTAR essa opção escolhida ao usuário. "
            "Regras de apresentação:"
            "- JAMAIS mencione 'Opção 1', 'Opção 2', etc. O usuário verá apenas a imagem do vestido escolhido."
            "- Comece apresentando diretamente: 'Encontrei este modelo que...' ou 'Veja esta opção maravilhosa...'."
            "- ANALISE O VESTIDO ESCOLHIDO: Baseie seus comentários EXCLUSIVAMENTE nos detalhes dele."
            "- COMPARE COM O PEDIDO: Se houver diferenças, explique de forma otimista."
            "- SUGIRA ACESSÓRIOS COM RIGOR: "
            "  * SE o vestido escolhido for DE NOIVA (branco, off-white, cauda longa): sugira véu, grinalda, buquê."
            "  * SE o vestido for DE FESTA (colorido, madrinha, formatura): sugira brincos, clutch, estola, sapatos de gala. JAMAIS sugira véu ou buquê para vestidos de festa."
            "- Mantenha tom sofisticado e acolhedor."
            "Retorne a resposta EXCLUSIVAMENTE em JSON no formato: {\"selected_option_index\": 0, \"reply\": \"texto da resposta...\"}"
            "Onde 'selected_option_index' deve ser o índice (0 a 4) da Opção escolhida."
        )

        final_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": final_response_prompt},
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"}
        )
        
        final_json = json.loads(final_response.choices[0].message.content)
        selected_index = final_json.get("selected_option_index", 0)
        reply_text = final_json.get("reply", "")

        # Garante que o índice é válido
        if selected_index < 0 or selected_index >= len(candidates):
            selected_index = 0
            
        chosen_item = candidates[selected_index]
        
        # Constrói objeto do vestido principal
        result_dress = {
            "image_url": chosen_item.get('imageUrl') or url_for('static', filename=f"dresses/{chosen_item['file_name']}"),
            "description": chosen_item['description'],
            "title": chosen_item.get('title', 'Sugestão Exclusiva'),
            "price": chosen_item.get('price', "Consulte o preço do aluguel"),
            "id": chosen_item.get('item_id') or chosen_item.get('custom_id') or f"ai_main_{selected_index}",
            "customId": chosen_item.get('customId') or chosen_item.get('custom_id')
        }

        # Constrói lista de sugestões (todos exceto o escolhido)
        suggestions = []
        for i, item in enumerate(candidates):
            if i != selected_index:
                suggestions.append({
                    "image_url": item.get('imageUrl') or url_for('static', filename=f"dresses/{item['file_name']}"),
                    "description": item['description'],
                    "title": item.get('title', 'Sugestão Exclusiva'),
                    "price": item.get('price', "Consulte o preço do aluguel"),
                    "id": item.get('item_id') or item.get('custom_id') or f"ai_sugg_{i}",
                    "customId": item.get('customId') or item.get('custom_id')
                })

        return jsonify({
            "reply": reply_text,
            "dress": result_dress,
            "suggestions": suggestions
        })

    except Exception as e:
        print(f"Erro no processamento AI: {e}")
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
                "description": item.get('description', '')
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
                "customId": item.get('customId') # Adicionado para consistência
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
