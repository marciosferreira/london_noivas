import os
import json
import pickle
import faiss
import numpy as np
import re
import time
import unicodedata
import threading
import boto3
from boto3.dynamodb.conditions import Key
from flask import Blueprint, request, jsonify, url_for, current_app
from openai import OpenAI
from dotenv import load_dotenv
from ai_sync_service import get_index_status, sync_index as service_sync_index, get_progress

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
inventory_digest = {}
_query_rewrite_cache = {}
_QUERY_REWRITE_CACHE_TTL_SECONDS = 15 * 60

def _normalize_text(text):
    text = str(text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join([c for c in text if not unicodedata.combining(c)])
    text = re.sub(r"\s+", " ", text).strip()
    return text

def load_resources():
    global index, metadata, inventory_digest
    try:
        if os.path.exists(INDEX_FILE) and os.path.exists(METADATA_FILE):
            index = faiss.read_index(INDEX_FILE)
            with open(METADATA_FILE, "rb") as f:
                metadata = pickle.load(f)
            inventory_digest = _build_inventory_digest(metadata)
            print("Recursos de IA carregados com sucesso.")
        else:
            print("Arquivos de índice/metadata não encontrados. A busca AI pode não funcionar.")
    except Exception as e:
        print(f"Erro ao carregar recursos de IA: {e}")

def get_embedding(text):
    text = text.replace("\n", " ")
    return client.embeddings.create(input=[text], model=EMBEDDING_MODEL).data[0].embedding

def _category_slug_from_raw(raw):
    raw = str(raw or "").lower().strip()
    if "noiv" in raw:
        return "noiva"
    return "festa"

def _category_slug(meta):
    if not isinstance(meta, dict):
        return "festa"
    raw = meta.get("category_slug") or meta.get("item_category") or meta.get("category") or meta.get("categoria")
    return _category_slug_from_raw(raw)

def _seed_inventory_context():
    return {
        "noiva": {
            "estilos": ["Sereia", "Princesa", "Evasê", "Boho Chic", "Minimalista", "Clássico"],
            "tecidos": ["Zibelina", "Renda", "Tule", "Cetim", "Seda", "Crepe"],
            "decotes": ["Tomara que caia", "Decote em V", "Canoa", "Ombro a ombro", "Frente única"],
            "detalhes": ["Brilho", "Pedraria", "Cauda longa", "Fenda", "Manga longa", "Costas abertas"],
            "cores": ["Off-white", "Branco", "Pérola", "Champagne"],
            "ocasioes": ["Noiva"],
        },
        "festa": {
            "estilos": ["Sereia", "Princesa", "Evasê", "Reto", "Clássico", "Moderno"],
            "tecidos": ["Renda", "Tule", "Cetim", "Seda", "Crepe", "Chiffon"],
            "decotes": ["Tomara que caia", "Decote em V", "Ombro a ombro", "Frente única"],
            "detalhes": ["Brilho", "Pedraria", "Fenda", "Manga longa", "Costas abertas"],
            "cores": ["Azul royal", "Azul marinho", "Verde esmeralda", "Vermelho", "Rosa", "Preto", "Prata"],
            "ocasioes": ["Madrinha", "Formatura", "Debutante", "Gala", "Convidada"],
        },
    }

def _top_values(counts, limit=12):
    items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [v for v, _ in items[:limit]]

def _build_inventory_digest(meta_list):
    seed = _seed_inventory_context()

    by_cat = {
        "noiva": {k: {} for k in seed["noiva"].keys()},
        "festa": {k: {} for k in seed["festa"].keys()},
    }

    for m in meta_list or []:
        if not isinstance(m, dict):
            continue
        cat = _category_slug(m)
        mf = m.get("metadata_filters")
        if not isinstance(mf, dict):
            mf = {}

        def bump(facet, values):
            if not values:
                return
            target = by_cat.get(cat, {}).get(facet)
            if target is None:
                return
            for v in values:
                key = str(v).strip()
                if not key:
                    continue
                target[key] = target.get(key, 0) + 1

        bump("tecidos", mf.get("fabrics"))
        bump("estilos", mf.get("silhouette"))
        bump("decotes", mf.get("neckline"))
        bump("detalhes", mf.get("details"))
        bump("cores", mf.get("colors"))
        bump("ocasioes", mf.get("occasions"))

    digest = {}
    for cat in ["noiva", "festa"]:
        digest[cat] = {}
        for facet, seed_values in seed[cat].items():
            counts = by_cat[cat].get(facet, {})
            observed = _top_values(counts, limit=12)
            combined = list(dict.fromkeys(seed_values + observed))
            digest[cat][facet] = combined[:16]

    return digest

def _extract_json_object(text):
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]

def _rewrite_catalog_query(query, target_category):
    if not isinstance(query, str):
        return {"query_reescrita": ""}
    query = query.strip()
    if not query:
        return {"query_reescrita": ""}

    target_slug = (target_category or "").lower().strip()
    if target_slug not in ["noiva", "festa"]:
        target_slug = "ambigua"

    cache_key = f"{target_slug}|{_normalize_text(query)}"
    now = time.time()
    cached = _query_rewrite_cache.get(cache_key)
    if cached and (now - cached.get("ts", 0)) <= _QUERY_REWRITE_CACHE_TTL_SECONDS:
        return cached.get("data") or {"query_reescrita": query}

    digest_for_prompt = inventory_digest.get("noiva" if target_slug == "noiva" else "festa" if target_slug == "festa" else "noiva") or {}
    if target_slug == "ambigua":
        digest_for_prompt = {"noiva": inventory_digest.get("noiva", {}), "festa": inventory_digest.get("festa", {})}

    system_prompt = (
        "Você reescreve consultas para busca semântica em um catálogo de vestidos.\n"
        "Objetivo: transformar a mensagem do usuário em uma consulta curta, explícita e útil para embeddings.\n"
        "O contexto do inventário é apenas exemplos observados; não se restrinja a ele.\n"
        "Se o usuário pedir um atributo/valor que não aparece nos exemplos, mantenha mesmo assim e liste em atributos_novos.\n"
        "Não invente que o inventário possui algo; apenas descreva preferências do usuário.\n"
        "Responda apenas com JSON válido (sem markdown)."
    )

    user_payload = {
        "categoria_alvo": target_slug,
        "inventario_contexto_exemplos": digest_for_prompt,
        "consulta_usuario": query,
        "saida": {
            "categoria_detectada": "noiva|festa|ambigua",
            "confianca_categoria": "0-1",
            "query_reescrita": "string",
            "atributos_extraidos": "obj",
            "atributos_novos": "lista",
            "termos_excluir": "lista"
        }
    }

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        try:
            data = json.loads(content)
        except Exception:
            obj = _extract_json_object(content)
            data = json.loads(obj) if obj else {}

        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("query_reescrita"), str) or not data.get("query_reescrita").strip():
            data["query_reescrita"] = query

        _query_rewrite_cache[cache_key] = {"ts": now, "data": data}
        return data
    except Exception as e:
        print(f"Erro ao reescrever query: {e}")
        return {"query_reescrita": query}

def _query_embedding_text_from_rewrite(data):
    if not isinstance(data, dict):
        return None
    attrs = data.get("atributos_extraidos")
    cat = str(data.get("categoria_detectada", "")).lower().strip()
    tokens = []
    order = ["silhouette","neckline","sleeves","details","fabrics","colors","occasions"]
    alt = {"cor":"colors","estilo":"silhouette","decote":"neckline","mangas":"sleeves","detalhes":"details","tecido":"fabrics","ocasiao":"occasions"}
    if isinstance(attrs, dict):
        for k in order:
            v = attrs.get(k) if k in attrs else attrs.get(next((kk for kk, vv in alt.items() if vv == k), None))
            if isinstance(v, list):
                for x in v:
                    s = str(x).strip()
                    if s:
                        tokens.append(s)
            elif isinstance(v, str) and v.strip():
                tokens.append(v.strip())
    if not tokens:
        return None
    if cat in ["noiva", "festa"]:
        tokens.append(cat)
    return " ".join(tokens)

def _apply_facet_constraints(candidates, rewrite_data):
    if not candidates:
        return candidates
    if not isinstance(rewrite_data, dict):
        return candidates
    attrs = rewrite_data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return candidates
    def norm(x):
        return _normalize_text(x)
    req = {}
    key_map = {"cor":"colors","estilo":"silhouette","decote":"neckline","mangas":"sleeves","detalhes":"details","tecido":"fabrics","ocasiao":"occasions"}
    for k in ["colors","silhouette","neckline","sleeves","details","fabrics","occasions"]:
        v = attrs.get(k)
        if v is None:
            for kk, vv in key_map.items():
                if vv == k and kk in attrs:
                    v = attrs.get(kk)
                    break
        if isinstance(v, list):
            vv = [norm(s) for s in v if str(s).strip()]
            if vv:
                req[k] = set(vv)
        elif isinstance(v, str) and v.strip():
            req[k] = {norm(v)}
    if not req:
        return candidates
    def has_intersection(values, needed):
        if not isinstance(values, list):
            return False
        nv = set(norm(x) for x in values if str(x).strip())
        return bool(nv.intersection(needed))
    filtered = []
    for c in candidates:
        mf = c.get("metadata_filters", {}) or {}
        ok = True
        for k in ["colors","silhouette"]:
            if k in req and not has_intersection(mf.get(k), req[k]):
                ok = False
                break
        if ok:
            filtered.append(c)
    return filtered if filtered else candidates

def _rerank_by_facets(candidates, rewrite_data):
    if not candidates or not isinstance(rewrite_data, dict):
        return candidates
    attrs = rewrite_data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return candidates
    def norm(x):
        return _normalize_text(x)
    need = {}
    key_map = {"cor":"colors","estilo":"silhouette","decote":"neckline","mangas":"sleeves","detalhes":"details","tecido":"fabrics","ocasiao":"occasions"}
    for k in ["colors","silhouette","neckline","sleeves","details","fabrics","occasions"]:
        v = attrs.get(k)
        if v is None:
            for kk, vv in key_map.items():
                if vv == k and kk in attrs:
                    v = attrs.get(kk)
                    break
        if isinstance(v, list):
            need[k] = set(norm(s) for s in v if str(s).strip())
        elif isinstance(v, str) and v.strip():
            need[k] = {norm(v)}
    weights = {"colors":2,"silhouette":2,"neckline":1,"sleeves":1,"details":1,"fabrics":1,"occasions":1}
    def score(c):
        mf = c.get("metadata_filters", {}) or {}
        s = 0
        for k, req in need.items():
            vals = mf.get(k)
            if isinstance(vals, list):
                nv = set(norm(x) for x in vals if str(x).strip())
                if nv.intersection(req):
                    s += weights.get(k, 1)
        return s
    ranked = sorted(
        [(score(c), i, c) for i, c in enumerate(candidates)],
        key=lambda x: (-x[0], x[1])
    )
    return [c for _, _, c in ranked]
load_resources()

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
                        'ProjectionExpression': 'item_id, #st, title, item_title, item_description, item_image_url, item_value, item_custom_id, category, item_category',
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
            # Verificar status (ignorar deletados)
            if db_item.get('status') == 'deleted':
                continue

            # Item válido! Enriquecer.
            seen_ids.add(item_id)
            
            # S3 URL
            s3_url = db_item.get('item_image_url')
            if s3_url:
                meta['imageUrl'] = s3_url
                meta['file_name'] = None
            
            # Metadata frescos
            meta['title'] = db_item.get('title') or db_item.get('item_title') or meta.get('title')
            meta['description'] = db_item.get('item_description', meta.get('description'))
            
            # Preço
            val = db_item.get('item_value', 0)
            if val:
                meta['price'] = f"R$ {val}"
            
            # Custom ID (Human Readable)
            meta['customId'] = db_item.get('item_custom_id')
            # UUID (System ID)
            meta['item_id'] = db_item.get('item_id')
            cat_raw = db_item.get('item_category') or db_item.get('category') or meta.get('item_category') or meta.get('category')
            cat_slug = _category_slug_from_raw(cat_raw)
            meta['item_category'] = db_item.get('item_category', meta.get('item_category'))
            meta['category_raw'] = cat_raw
            meta['category_slug'] = cat_slug
            meta['category'] = 'Noiva' if cat_slug == 'noiva' else 'Festa'
            
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
        rewrite = _rewrite_catalog_query(query, "")
        q_text = _query_embedding_text_from_rewrite(rewrite) or rewrite.get("query_reescrita") or query
        query_embedding = get_embedding(q_text)
        query_vector = np.array([query_embedding]).astype('float32')

        # 2. Busca no FAISS
        # Buscamos mais itens para garantir candidatos após validação de status/DB
        search_k = 220
        distances, indices = index.search(query_vector, search_k)
        
        # 3. Coleta metadados
        raw_candidates = []
        for idx in indices[0]:
            if idx != -1:
                raw_candidates.append(metadata[idx].copy())

        # 4. Valida e Enriquece
        valid_candidates = validate_and_enrich_candidates(raw_candidates)
        
        cat = str(rewrite.get("categoria_detectada", "")).lower().strip()
        if cat in ["noiva", "festa"]:
            valid_candidates = [c for c in valid_candidates if _category_slug(c) == cat]
        
        constrained = _apply_facet_constraints(valid_candidates, rewrite)
        ranked = _rerank_by_facets(constrained, rewrite) if constrained else valid_candidates
        return ranked[:k]

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
        target_category = data.get('category', '').lower().strip()
        rewritten = _rewrite_catalog_query(query, target_category)
        query_for_embedding = rewritten.get("query_reescrita") if isinstance(rewritten, dict) else None
        if not isinstance(query_for_embedding, str) or not query_for_embedding.strip():
            query_for_embedding = query

        query_embedding = get_embedding(query_for_embedding)
        query_vector = np.array([query_embedding]).astype('float32')
        
        # 2. Busca no FAISS
        # Buscamos mais itens (100) para garantir que após o filtro de status/DB
        # ainda tenhamos itens suficientes para preencher as páginas.
        k = 220 if target_category else 100
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
        if target_category:
            filtered_results = []
            for item in valid_results:
                is_noiva = _category_slug(item) == 'noiva'

                if target_category == 'noiva':
                    if is_noiva:
                        filtered_results.append(item)
                else:
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

@ai_bp.route('/api/admin/sync-progress', methods=['GET'])
def admin_sync_progress():
    """Retorna progresso de sincronização do índice IA"""
    try:
        return jsonify(get_progress())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/admin/sync-ai-index', methods=['POST'])
def admin_sync_ai_index():
    """Aciona a sincronização do índice IA"""
    try:
        data = request.get_json(silent=True) or {}
        reset_local = bool(data.get("reset_local"))
        force_regenerate = bool(data.get("force_regenerate"))
        def _run():
            res = service_sync_index(reset_local=reset_local, force_regenerate=force_regenerate)
            if res.get('status') == 'success':
                load_resources()
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
