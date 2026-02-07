import os
import json
import pickle
import faiss
import numpy as np
from flask import Blueprint, request, jsonify, url_for
from openai import OpenAI
from dotenv import load_dotenv

# Configuração
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

        # 3. Busca no FAISS (TOP 5)
        k = 5
        distances, indices = index.search(query_vector, k)
        
        candidates = []
        valid_indices = []
        
        for idx in indices[0]:
            if idx != -1:
                item = metadata[idx]
                candidates.append(item)
                valid_indices.append(idx)

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

        # 4. Seleção Inteligente e Resposta Final
        final_response_prompt = (
            "Você é um consultor de moda experiente e elegante da London Noivas. "
            f"O usuário solicitou: '{search_query}'. "
            "Abaixo estão as 5 melhores opções encontradas no nosso acervo:\n\n"
            f"{candidates_str}"
            "Sua tarefa é duplo: "
            "1. ESCOLHER A MELHOR OPÇÃO entre as 5 listadas que melhor atende ao pedido (considere cor, estilo, detalhes). "
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
            "image_url": url_for('static', filename=f"dresses/{chosen_item['file_name']}"),
            "description": chosen_item['description'],
            "title": chosen_item.get('title', 'Sugestão Exclusiva'),
            "price": "Consulte o preço do aluguel",
            "id": f"ai_main_{selected_index}"
        }

        # Constrói lista de sugestões (todos exceto o escolhido)
        suggestions = []
        for i, item in enumerate(candidates):
            if i != selected_index:
                suggestions.append({
                    "image_url": url_for('static', filename=f"dresses/{item['file_name']}"),
                    "description": item['description'],
                    "title": item.get('title', 'Sugestão Exclusiva'),
                    "price": "Consulte o preço do aluguel",
                    "id": f"ai_sugg_{i}"
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
        
        suggestions = []
        for idx in indices[0]:
            if idx != -1 and idx != target_idx:
                item = metadata[idx]
                suggestions.append({
                    "id": item.get('custom_id'),
                    "title": item.get('title', 'Vestido'),
                    "image_url": url_for('static', filename=f"dresses/{item['file_name']}"),
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
    
    if not query:
        return jsonify({"error": "Query vazia"}), 400

    try:
        # 1. Gera embedding da query
        query_embedding = get_embedding(query)
        query_vector = np.array([query_embedding]).astype('float32')
        
        # 2. Busca no FAISS (TOP 50 para preencher a página)
        k = 50
        distances, indices = index.search(query_vector, k)
        
        results = []
        for idx in indices[0]:
            if idx != -1:
                item = metadata[idx]
                results.append({
                    "item_id": item.get('custom_id'),
                    "title": item.get('title', 'Vestido'),
                    "imageUrl": url_for('static', filename=f"dresses/{item['file_name']}"),
                    "description": item.get('description', ''),
                    "price": "Consulte o preço do aluguel"
                })
                
        return jsonify({"results": results})

    except Exception as e:
        print(f"Erro na busca do catálogo: {e}")
        return jsonify({"error": str(e)}), 500
