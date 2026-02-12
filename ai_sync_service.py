import os
import sys
import json
import boto3
import requests
import pickle
import shutil
import base64
import subprocess
import re
import unicodedata
import time
import faiss # Import faiss for direct index checking
from botocore.exceptions import ClientError
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRESSES_DIR = os.path.join(BASE_DIR, 'static', 'dresses')
DATASET_FILE = os.path.join(BASE_DIR, 'embeddings_creation', 'vestidos_dataset.jsonl')
METADATA_FILE = os.path.join(BASE_DIR, 'vector_store_metadata.pkl')
INDEX_FILE = os.path.join(BASE_DIR, 'vector_store.index')
CREATE_VECTOR_SCRIPT = os.path.join(BASE_DIR, 'embeddings_creation', 'create_vector_store.py')

# AWS & OpenAI
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
itens_table = dynamodb.Table('alugueqqc_itens')
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROGRESS = {"running": False, "total": 0, "processed": 0, "current": None, "eta_seconds": None, "start_time": None, "done": False, "message": None, "error": None}

def _progress_start(total):
    PROGRESS.update({"running": True, "total": int(total or 0), "processed": 0, "current": None, "eta_seconds": None, "start_time": time.time(), "done": False, "message": None, "error": None})

def _progress_update(processed, current, eta):
    PROGRESS.update({"processed": int(processed or 0), "current": str(current or ""), "eta_seconds": int(eta or 0)})

def _progress_finish(message=None, error=None):
    PROGRESS.update({"running": False, "done": True, "message": message, "error": error})

def get_progress():
    return PROGRESS.copy()

def _category_slug_from_occasions(occasions):
    for o in _to_list(occasions):
        oo = _normalize_text(o)
        if "noiv" in oo or "civil" in oo:
            return "noiva"
    return "festa"

def _category_slug_from_entry(entry, description=None, title=None):
    if isinstance(entry, dict):
        mf = entry.get("metadata_filters")
        if isinstance(mf, dict):
            occ = mf.get("occasions")
            if occ:
                return _category_slug_from_occasions(occ)
        occ2 = entry.get("occasions") or entry.get("ocasiao")
        if occ2:
            return _category_slug_from_occasions(occ2)

    blob = " ".join([str(title or ""), str(description or "")]).lower()
    if "noiv" in blob or "civil" in blob:
        return "noiva"
    return "festa"

def _normalize_text(text):
    text = str(text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join([c for c in text if not unicodedata.combining(c)])
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _extract_json_object(text):
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]

def _to_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []

def _metadata_filters_from_structured(structured, category_slug):
    if not isinstance(structured, dict):
        return None

    occasions_value = structured.get("occasions")
    if occasions_value is None:
        occasions_value = structured.get("ocasiao")

    filters = {
        "colors": _to_list(structured.get("cor")),
        "fabrics": _to_list(structured.get("tecido")),
        "silhouette": _to_list(structured.get("estilo")),
        "neckline": _to_list(structured.get("decote")),
        "sleeves": _to_list(structured.get("mangas")),
        "details": _to_list(structured.get("detalhes")),
    }

    filters["occasions"] = _to_list(occasions_value)

    return {k: v for k, v in filters.items() if v}

def _build_inventory_examples(existing_data, limit_per_facet=8):
    seed = {
        "noiva": {
            "cor": ["Off-white", "Branco", "Pérola", "Champagne"],
            "tecido": ["Zibelina", "Renda", "Tule", "Cetim", "Seda", "Crepe"],
            "estilo": ["Sereia", "Princesa", "Evasê", "Boho Chic", "Minimalista", "Clássico"],
            "decote": ["Tomara que caia", "Decote em V", "Canoa", "Ombro a ombro", "Frente única"],
            "mangas": ["Sem mangas", "Manga curta", "Manga longa", "Alça fina", "Ombro a ombro"],
            "detalhes": ["Brilho", "Pedraria", "Cauda longa", "Fenda", "Costas abertas"],
        },
        "festa": {
            "ocasiao": ["Madrinha", "Formatura", "Debutante", "Gala", "Convidada"],
            "cor": ["Azul royal", "Azul marinho", "Verde esmeralda", "Vermelho", "Rosa", "Preto", "Prata"],
            "tecido": ["Renda", "Tule", "Cetim", "Seda", "Crepe", "Chiffon"],
            "estilo": ["Sereia", "Princesa", "Evasê", "Reto", "Clássico", "Moderno"],
            "decote": ["Tomara que caia", "Decote em V", "Ombro a ombro", "Frente única"],
            "mangas": ["Sem mangas", "Manga curta", "Manga longa", "Alça fina"],
            "detalhes": ["Brilho", "Pedraria", "Fenda", "Costas abertas"],
        },
    }

    facet_map = {
        "colors": "cor",
        "fabrics": "tecido",
        "silhouette": "estilo",
        "neckline": "decote",
        "sleeves": "mangas",
        "details": "detalhes",
        "occasions": "ocasiao",
    }

    counts = {
        "noiva": {k: {} for k in seed["noiva"].keys()},
        "festa": {k: {} for k in seed["festa"].keys()},
    }

    for entry in existing_data or []:
        if not isinstance(entry, dict):
            continue
        mf = entry.get("metadata_filters")
        if not isinstance(mf, dict):
            continue
        cat = _category_slug_from_occasions(mf.get("occasions"))
        for src_key, dst_key in facet_map.items():
            values = mf.get(src_key)
            if not isinstance(values, list):
                continue
            for v in values:
                vv = str(v).strip()
                if not vv:
                    continue
                bucket = counts.get(cat, {}).get(dst_key)
                if bucket is None:
                    continue
                bucket[vv] = bucket.get(vv, 0) + 1

    out = {}
    for cat in ["noiva", "festa"]:
        out[cat] = {}
        for facet_key, seed_values in seed[cat].items():
            observed = sorted(counts[cat].get(facet_key, {}).items(), key=lambda x: (-x[1], x[0]))
            observed_vals = [v for v, _ in observed[:limit_per_facet]]
            merged = list(dict.fromkeys(seed_values + observed_vals))
            out[cat][facet_key] = merged[:limit_per_facet]
    return out

def _extract_metadata_filters(description, category_slug):
    text = _normalize_text(description)

    fabrics = [
        ("zibelina", ["zibelina"]),
        ("renda", ["renda"]),
        ("tule", ["tule"]),
        ("cetim", ["cetim"]),
        ("seda", ["seda"]),
        ("crepe", ["crepe"]),
        ("chiffon", ["chiffon"]),
        ("organza", ["organza"]),
        ("paete", ["paete", "paetes", "paetê", "paetês"]),
        ("lantejoula", ["lantejoula", "lantejoulas"]),
        ("voal", ["voal"]),
    ]

    silhouettes = [
        ("sereia", ["sereia"]),
        ("princesa", ["princesa"]),
        ("evase", ["evase", "evasê"]),
        ("linha-a", ["linha a", "linha-a", "em linha a"]),
        ("reto", ["reto"]),
        ("boho", ["boho", "boho chic", "boho-chic"]),
        ("minimalista", ["minimalista"]),
        ("classico", ["classico", "clássico"]),
    ]

    necklines = [
        ("tomara-que-caia", ["tomara que caia", "tomara-que-caia", "strapless"]),
        ("v", ["decote em v", "decote v", "em v"]),
        ("canoa", ["canoa"]),
        ("ombro-a-ombro", ["ombro a ombro", "ombro-a-ombro"]),
        ("frente-unica", ["frente unica", "frente-unica", "halter"]),
        ("coracao", ["coracao", "coração"]),
        ("reto", ["decote reto", "decote reto."]),
        ("bustie", ["bustie", "bustiê"]),
    ]

    sleeves = [
        ("manga-longa", ["manga longa", "mangas longas"]),
        ("manga-curta", ["manga curta", "mangas curtas"]),
        ("sem-manga", ["sem manga", "sem mangas"]),
        ("alca-fina", ["alca fina", "alcas finas", "alça fina", "alças finas"]),
        ("um-ombro-so", ["um ombro so", "um ombro só", "ombro unico", "ombro único"]),
    ]

    details = [
        ("brilho", ["brilho", "brilhante", "glitter", "cintilante", "lurex"]),
        ("pedraria", ["pedraria"]),
        ("bordado", ["bordado", "bordados"]),
        ("fenda", ["fenda"]),
        ("costas-abertas", ["costas abertas", "costas aberta"]),
        ("cauda-longa", ["cauda longa", "cauda-longa"]),
        ("transparencia", ["transparencia", "transparente", "transparências", "transparencias"]),
        ("drapeado", ["drapeado", "drapeados"]),
    ]

    colors = [
        ("off-white", ["off white", "off-white", "ivory", "marfim", "perola", "pérola", "champagne", "branco neve", "branco-neve", "branco gelo", "branco-gelo"]),
        ("branco", ["branco"]),
        ("preto", ["preto"]),
        ("azul-marinho", ["azul marinho", "azul-marinho"]),
        ("azul-royal", ["azul royal", "azul-royal"]),
        ("azul-claro", ["azul claro", "azul-claro"]),
        ("verde-esmeralda", ["verde esmeralda", "verde-esmeralda"]),
        ("verde", ["verde"]),
        ("vermelho", ["vermelho"]),
        ("bordo", ["bordo", "bordô"]),
        ("vinho", ["vinho"]),
        ("rosa", ["rosa"]),
        ("roxo", ["roxo"]),
        ("lilas", ["lilas", "lilás"]),
        ("prata", ["prata"]),
        ("dourado", ["dourado"]),
        ("magenta", ["magenta"]),
        ("terracota", ["terracota"]),
        ("amarelo", ["amarelo", "mostarda"]),
    ]

    occasions = [
        ("debutante", ["debutante", "15 anos", "quinze anos"]),
        ("formatura", ["formatura", "formanda"]),
        ("madrinha", ["madrinha"]),
        ("gala", ["gala", "festa de gala"]),
        ("convidada", ["convidada"]),
        ("noiva", ["noiva", "noivas"]),
        ("civil", ["civil", "pre wedding", "pre-wedding", "pre wedding", "pre-wedding"]),
        ("casamento", ["casamento"]),
    ]

    def collect(pairs):
        found = []
        for canonical, patterns in pairs:
            for pat in patterns:
                if _normalize_text(pat) in text:
                    found.append(canonical)
                    break
        return sorted(set(found))

    filters = {
        "fabrics": collect(fabrics),
        "silhouette": collect(silhouettes),
        "neckline": collect(necklines),
        "sleeves": collect(sleeves),
        "details": collect(details),
        "colors": collect(colors),
    }

    filters["occasions"] = collect(occasions)

    return {k: v for k, v in filters.items() if v}

def _synthesize_embedding_text(metadata_filters, title):
    parts = []
    if isinstance(metadata_filters, dict):
        for k in ["silhouette", "neckline", "fabrics", "details", "colors", "sleeves", "occasions"]:
            vals = metadata_filters.get(k)
            if isinstance(vals, list):
                parts.extend([str(v).strip() for v in vals if str(v).strip()])
    if title:
        parts.append(str(title).strip())
    seen = set()
    cleaned = []
    for p in parts:
        q = _normalize_text(p)
        if q and q not in seen:
            seen.add(q)
            cleaned.append(p)
    return " ".join(cleaned)[:400]
def get_index_status():
    """
    Compara itens no DynamoDB com itens no índice FAISS (via metadata).
    Retorna contagens e IDs discrepantes, além de itens marcados como 'pending'.
    """
    # 1. Get all IDs from DynamoDB
    db_ids = set()
    pending_ids = set()
    pending_remove_ids = set()
    
    try:
        # Scan otimizado
        response = itens_table.scan(
            ProjectionExpression="item_id, embedding_status, item_image_url, image_url, #st",
            ExpressionAttributeNames={"#st": "status"}
        )
        for item in response.get('Items', []):
            # Considera imagem em item_image_url OU image_url
            has_image = bool(item.get('item_image_url') or item.get('image_url'))
            status = str(item.get('status') or '').lower().strip()
            if status in ['deleted', 'archived', 'inactive']:
                # Mesmo deletado, se marcado como pending_remove e tiver imagem,
                # coletamos para remoção explícita.
                if has_image and item.get('embedding_status') == 'pending_remove':
                    pending_remove_ids.add(item['item_id'])
                # Não entra em db_ids
                continue
            if not has_image:
                continue
            db_ids.add(item['item_id'])
            if item.get('embedding_status') == 'pending':
                pending_ids.add(item['item_id'])
            elif item.get('embedding_status') == 'pending_remove':
                pending_remove_ids.add(item['item_id'])
        
        while 'LastEvaluatedKey' in response:
            response = itens_table.scan(
                ProjectionExpression="item_id, embedding_status, item_image_url, image_url, #st",
                ExpressionAttributeNames={"#st": "status"},
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            for item in response.get('Items', []):
                has_image = bool(item.get('item_image_url') or item.get('image_url'))
                status = str(item.get('status') or '').lower().strip()
                if status in ['deleted', 'archived', 'inactive']:
                    if has_image and item.get('embedding_status') == 'pending_remove':
                        pending_remove_ids.add(item['item_id'])
                    continue
                if not has_image:
                    continue
                db_ids.add(item['item_id'])
                if item.get('embedding_status') == 'pending':
                    pending_ids.add(item['item_id'])
                elif item.get('embedding_status') == 'pending_remove':
                    pending_remove_ids.add(item['item_id'])

    except Exception as e:
        print(f"Erro ao escanear DynamoDB: {e}")
        return {"error": str(e)}

    # 2. Get all IDs from Metadata & Check Index Integrity
    index_ids = set()
    index_total = 0
    integrity_error = None

    # Check Metadata
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'rb') as f:
                meta_list = pickle.load(f)
                for m in meta_list:
                    if 'custom_id' in m:
                        index_ids.add(m['custom_id'])
        except Exception as e:
            print(f"Erro ao ler metadata: {e}")
            integrity_error = f"Erro leitura metadata: {e}"

    # Check FAISS Index (Strict Count Check)
    if os.path.exists(INDEX_FILE):
        try:
            index = faiss.read_index(INDEX_FILE)
            index_total = index.ntotal
        except Exception as e:
            print(f"Erro ao ler índice FAISS: {e}")
            integrity_error = f"Erro leitura índice: {e}"
    else:
        integrity_error = "Arquivo de índice não encontrado."

    # Consistency Check: Metadata vs Index
    if not integrity_error and len(index_ids) != index_total:
        integrity_error = f"Inconsistência Interna: Metadata tem {len(index_ids)} itens, Índice tem {index_total}."

    # 3. Compare DB vs Metadata
    missing_in_index = db_ids - index_ids # Needs Add
    deleted_in_db = index_ids - db_ids    # Needs Remove
    
    # Remove pending from missing to avoid duplication if they are both
    missing_in_index = missing_in_index - pending_ids - pending_remove_ids

    # Final Status Construction
    status = {
        "db_count": len(db_ids),
        "index_count": index_total,
        "metadata_count": len(index_ids),
        "missing_count": len(missing_in_index),
        "deleted_count": len(deleted_in_db),
        "missing_ids": list(missing_in_index),
        "deleted_ids": list(deleted_in_db),
        "pending_ids": list(pending_ids),
        "pending_remove_ids": list(pending_remove_ids),
        "integrity_error": integrity_error
    }

    # If significant mismatch in counts, add warning
    if (
        abs(len(db_ids) - index_total) > 0
        and not integrity_error
        and len(missing_in_index) == 0
        and len(deleted_in_db) == 0
        and len(pending_ids) == 0
        and len(pending_remove_ids) == 0
    ):
        status["warning"] = f"Desbalanço: DB({len(db_ids)}) vs Index({index_total})"

    return status

def encode_image(image_input):
    """Encodes image from path (str) or bytes to base64 string"""
    if isinstance(image_input, str):
        # É um caminho de arquivo
        with open(image_input, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")
    else:
        # Assume que são bytes
        return base64.b64encode(image_input).decode("utf-8")

def generate_dress_metadata(image_input, existing_desc=None, existing_title=None, inventory_examples=None, force_vision=False, copywriting=False):
    """
    Gera descrição e título usando GPT-4o, se não fornecidos.
    image_input: pode ser caminho do arquivo (str) ou bytes da imagem.
    """
    
    description = None if force_vision else existing_desc
    title = None if force_vision else existing_title

    # Se já temos ambos, retorna
    if not force_vision and description and title:
        return description, title, None

    base64_image = None
    
    # 1. Descrição (se faltar, ou se forçado)
    if force_vision or not description:
        try:
            if not base64_image:
                base64_image = encode_image(image_input)

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você descreve vestidos para um catálogo de busca.\n"
                            "Responda apenas com JSON válido (sem markdown).\n"
                            "Não inclua tamanho, manequim, medidas, altura, peso, ou suposições de tamanho.\n"
                            "Não inclua preço.\n"
                            "Use português do Brasil."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Analise a imagem e retorne um JSON com este schema:\n"
                                    "{\n"
                                    "  \"descricao\": \"string (máx 70 palavras, objetiva, para busca)\",\n"
                                    "  \"titulo\": \"string (máx 5 palavras)\",\n"
                                    "  \"occasions\": [\"...\"] (multi-label; pode ter 0, 1 ou várias; exemplos: Noiva, Civil, Madrinha, Formatura, Debutante, Gala, Convidada, Mae dos Noivos),\n"
                                    "  \"cor\": [\"...\"],\n"
                                    "  \"tecido\": [\"...\"],\n"
                                    "  \"estilo\": [\"...\"],\n"
                                    "  \"decote\": [\"...\"],\n"
                                    "  \"mangas\": [\"...\"],\n"
                                    "  \"detalhes\": [\"...\"],\n"
                                    "  \"keywords\": [\"...\"]\n"
                                    "}\n"
                                    "Exemplos observados no inventário (use como referência de vocabulário; não restrinja a isso):\n"
                                    f"{json.dumps(inventory_examples or {}, ensure_ascii=False)}\n"
                                    "Se algum campo não for possível inferir com segurança, retorne lista vazia.\n"
                                    "Regras de classificação (occasions):\n"
                                    "- Formatura: brilho intenso, fendas, recortes, cores vibrantes, sexy/moderno.\n"
                                    "- Madrinha: elegante/romântico, tons pastéis/terrosos, sem protagonismo excessivo.\n"
                                    "- Gala: luxo, estruturado, cauda, bordado pesado, red carpet/black tie.\n"
                                    "- Debutante: princesa volumoso ou balada curto/brilho.\n"
                                    "- Mae dos Noivos: sofisticado, mais cobertura, renda nobre, cores clássicas.\n"
                                    "- Convidada: bonito mas menos protagonista.\n"
                                    "- Noiva e Civil podem coexistir (um mesmo vestido pode servir para ambos).\n"
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        ],
                    }
                ],
                max_tokens=520,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = (response.choices[0].message.content or "").strip()
            try:
                parsed = json.loads(content)
            except Exception:
                obj = _extract_json_object(content)
                parsed = json.loads(obj) if obj else {}

            if isinstance(parsed, dict):
                description = parsed.get("descricao") or description
                title = parsed.get("titulo") or title
                structured = parsed
            else:
                structured = None
            if copywriting:
                try:
                    raw_cat = None
                    if isinstance(structured, dict):
                        raw_cat = structured.get("categoria")
                        if not raw_cat:
                            occ = structured.get("occasions")
                            if occ is None:
                                occ = structured.get("ocasiao")
                            if isinstance(occ, list) and occ:
                                raw_cat = str(occ[0]).strip() or None
                    response_copy = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "Você é estilista e copywriter de moda. Escreva uma descrição breve, elegante e emocional, mantendo fidelidade aos atributos. Não inclua medidas, tamanhos ou preço. Use português do Brasil."},
                            {"role": "user", "content": f"Crie uma descrição em tom de estilista (50 a 70 palavras) coerente com: {json.dumps(structured or {}, ensure_ascii=False)}. Categoria: {raw_cat or ''}."}
                        ],
                        max_tokens=120,
                    )
                    desc_copy = (response_copy.choices[0].message.content or "").strip().replace('"', '')
                    if desc_copy:
                        description = desc_copy
                    response_title_copy = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "Você é estilista e copywriter de moda. Crie um título curtíssimo (3 a 6 palavras), natural, memorável e comercial, que resuma os principais atributos. Não inclua aspas, medidas, tamanho ou preço. Use português do Brasil."},
                            {"role": "user", "content": f"Atributos: {json.dumps(structured or {}, ensure_ascii=False)}.\nDescrição: {description or ''}\nTítulo desejado: curto, natural, resumindo os atributos principais."}
                        ],
                        max_tokens=24,
                    )
                    title_copy = (response_title_copy.choices[0].message.content or "").strip().replace('"', '')
                    if title_copy:
                        title = title_copy
                except Exception:
                    pass
        except Exception as e:
            print(f"Erro ao gerar descrição: {e}")
            # Raise exception to skip saving
            raise Exception(f"Falha ao gerar descrição: {str(e)}")

    # 2. Título (se faltar)
    if not title:
        try:
            response_title = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Você é um especialista em marketing de moda."},
                    {"role": "user", "content": f"Crie um título curto e atraente (máximo 5 palavras) para este vestido baseado na descrição: {description}"}
                ],
                max_tokens=20,
            )
            title = response_title.choices[0].message.content.replace('"', '')
        except Exception as e:
            print(f"Erro ao gerar título: {e}")
            # Raise exception to skip saving
            raise Exception(f"Falha ao gerar título: {str(e)}")

    if not description:
        description = existing_desc
    if not title:
        title = existing_title
    if "structured" not in locals():
        structured = None
    return description, title, structured

def sync_index(reset_local=False, force_regenerate=False):
    """
    Sincroniza o índice FAISS com o DynamoDB:
    1. Baixa imagens de novos itens
    2. Gera metadados IA
    3. Remove itens deletados do JSONL
    4. Reconstrói o índice
    """
    # Inicia progresso imediatamente para evitar ler estado antigo em /sync-progress
    _progress_start(0)
    _progress_update(0, "inicializando", 0)
    
    if reset_local:
        for path in [DATASET_FILE, METADATA_FILE, INDEX_FILE]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                return {"status": "error", "message": f"Falha ao resetar arquivo {path}: {e}"}

    status = get_index_status()
    if "error" in status:
        return {"status": "error", "message": status["error"]}

    missing_ids = status['missing_ids']
    deleted_ids = status['deleted_ids']
    pending_ids = status.get('pending_ids', [])
    pending_remove_ids = status.get('pending_remove_ids', [])
    prev_index_total = int(status.get('index_count') or 0)

    # Se houver erro de integridade, CONTINUA a execução para reconstruir o índice
    if not missing_ids and not deleted_ids and not pending_ids and not status.get('integrity_error'):
        _progress_finish("Índice já está atualizado.")
        return {"status": "success", "message": "Índice já está atualizado."}

    print(f"Iniciando sincronização: +{len(missing_ids)} novos, -{len(deleted_ids)} deletados, ~{len(pending_ids)} atualizações. Integridade: {status.get('integrity_error') or 'OK'}")

    # A. Carregar dados existentes
    existing_data = []
    if os.path.exists(DATASET_FILE):
        with open(DATASET_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        existing_data.append(json.loads(line))
                    except:
                        pass

    deleted_ids_set = set(deleted_ids)
    inventory_examples = _build_inventory_examples(
        [d for d in existing_data if d.get('custom_id') not in deleted_ids_set],
        limit_per_facet=8,
    )
    
    # B. Filtrar Deletados e Pendentes (serão reprocessados)
    deleted_ids_set = set(deleted_ids)
    pending_ids_set = set(pending_ids)
    pending_remove_ids_set = set(pending_remove_ids)
    ids_to_remove = deleted_ids_set | pending_ids_set | pending_remove_ids_set
    removed_deleted_count = 0
    removed_pending_count = 0
    removed_pending_remove_count = 0
    if ids_to_remove:
        initial_count = len(existing_data)
        # Contabiliza remoções por tipo antes de filtrar
        for d in existing_data:
            cid = d.get('custom_id')
            if cid in deleted_ids_set:
                removed_deleted_count += 1
            elif cid in pending_ids_set:
                removed_pending_count += 1
            elif cid in pending_remove_ids_set:
                removed_pending_remove_count += 1
        # Mantém apenas se custom_id NÃO estiver na lista de removidos/atualizados
        existing_data = [d for d in existing_data if d.get('custom_id') not in ids_to_remove]
        print(f"Removidos {initial_count - len(existing_data)} itens do dataset (deletados={removed_deleted_count}, pendentes={removed_pending_count}, pendentes_remove={removed_pending_remove_count}).")

    # C. Processar Novos Itens (Missing + Pending)
    items_to_process = set(missing_ids) | set(pending_ids)  # não inclui pending_remove
    total_items = len(items_to_process)
    _progress_start(total_items)
    start_time = time.time()
    processed_so_far = 0
    print(f"Total de itens para processar: {total_items}")
    
    new_items_count = 0
    updated_items_count = 0
    failed_items = []

    for item_id in items_to_process:
        try:
            # Busca dados no DB
            resp = itens_table.get_item(Key={'item_id': item_id})
            item = resp.get('Item')
            if not item: continue
            
            image_url = item.get('item_image_url') or item.get('image_url')
            if not image_url: 
                print(f"Item {item_id} sem imagem, pulando.")
                continue 
            
            # Download da Imagem (Em Memória)
            ext = 'jpg'
            if '.' in image_url:
                parts = image_url.split('.')
                if len(parts) > 1:
                    ext = parts[-1].split('?')[0]
                    if len(ext) > 4: ext = 'jpg' # Fallback
            
            # Mantemos o nome do arquivo no JSON para referência (caso precise no futuro)
            # mas NÃO salvamos o arquivo no disco.
            filename = f"{item_id}.{ext}"

            processed_so_far += 1
            avg = (time.time() - start_time) / max(1, processed_so_far)
            eta = int(max(0, (total_items - processed_so_far) * avg))
            _progress_update(processed_so_far, item_id, eta)
            print(f"[{processed_so_far}/{total_items}] Baixando imagem em memória: {item_id} (ETA ~{eta}s)")
            try:
                r = requests.get(image_url, timeout=15)
                if r.status_code == 200:
                    image_bytes = r.content
                else:
                    print(f"Falha ao baixar imagem: {r.status_code}")
                    failed_items.append(f"{item_id}: Falha download imagem status {r.status_code}")
                    continue
            except Exception as e:
                print(f"Exceção no download: {e}")
                failed_items.append(f"{item_id}: Exceção download")
                continue

            # Gera Metadados
            print(f"[{processed_so_far}/{total_items}] Gerando metadados IA: {item_id}")
            
            existing_desc = item.get("description") or item.get("item_description")
            existing_title = item.get("title") or item.get("item_title")

            # Passa os bytes da imagem diretamente
            description, title, structured = generate_dress_metadata(
                image_bytes,
                existing_desc,
                existing_title,
                inventory_examples=inventory_examples,
                force_vision=force_regenerate,
                copywriting=True,
            )
            
            group_slug = _category_slug_from_entry({"metadata_filters": _metadata_filters_from_structured(structured, None) or {}}, description=description, title=title)
            metadata_filters = _metadata_filters_from_structured(structured, group_slug) or _extract_metadata_filters(description, group_slug)
            embedding_text = _synthesize_embedding_text(metadata_filters, title) or description
            
            try:
                itens_table.update_item(
                    Key={'item_id': item_id},
                    UpdateExpression="SET description = :d, item_description = :d, title = :t, item_title = :t",
                    ExpressionAttributeValues={
                        ':d': description or '',
                        ':t': title or ''
                    }
                )
            except Exception as e:
                print(f"Erro ao atualizar descrição/título do item {item_id}: {e}")
            
            new_entry = {
                "file_name": filename,
                "custom_id": item_id,
                "description": description,
                "title": title,
                "metadata_filters": metadata_filters,
                "embedding_text": embedding_text
            }
            existing_data.append(new_entry)
            
            if item_id in pending_ids:
                updated_items_count += 1
                # Limpa flag pending no DB
                try:
                    itens_table.update_item(
                        Key={'item_id': item_id},
                        UpdateExpression="REMOVE embedding_status"
                    )
                except Exception as e:
                    print(f"Erro ao remover flag pending do item {item_id}: {e}")
            else:
                new_items_count += 1
            
        except Exception as e:
            print(f"Erro ao processar item {item_id}: {e}")
            failed_items.append(f"{item_id}: {str(e)}")
            continue

    # D. Salvar JSONL Atualizado
    print("Salvando dataset atualizado...")
    with open(DATASET_FILE, 'w', encoding='utf-8') as f:
        for entry in existing_data:
            if isinstance(entry, dict):
                if "metadata_filters" not in entry or not isinstance(entry.get("metadata_filters"), dict) or not entry.get("metadata_filters"):
                    group_slug = _category_slug_from_entry(entry, description=entry.get("description"), title=entry.get("title"))
                    entry["metadata_filters"] = _extract_metadata_filters(entry.get("description"), group_slug)
                if not entry.get("embedding_text"):
                    entry["embedding_text"] = _synthesize_embedding_text(entry.get("metadata_filters"), entry.get("title")) or entry.get("description")
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    # E. Reconstruir Índice (Chama o script existente)
    print("Reconstruindo índice FAISS...")
    try:
        # Usa o mesmo python que está rodando este script
        subprocess.run([sys.executable, CREATE_VECTOR_SCRIPT], check=True)
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"Falha na reconstrução do índice: {e}"}
    
    new_index_total = None
    try:
        idx = faiss.read_index(INDEX_FILE)
        new_index_total = idx.ntotal
    except Exception:
        new_index_total = None
    if new_index_total is not None:
        index_added_count = max(0, new_index_total - prev_index_total)
        index_removed_count = max(0, prev_index_total - new_index_total)
        if index_removed_count > 0 and index_added_count == 0 and updated_items_count == 0:
            message = f"Remoção no índice concluída. -{index_removed_count} removidos."
        elif index_added_count > 0 and index_removed_count == 0 and updated_items_count == 0:
            message = f"Adição ao índice concluída. +{index_added_count} adicionados."
        else:
            base = "Reparo de integridade concluído." if status.get('integrity_error') else "Índice atualizado."
            message = f"{base} +{index_added_count} adicionados, -{index_removed_count} removidos."
    else:
        removed_total_count = max(len(deleted_ids) + removed_pending_remove_count, removed_deleted_count + removed_pending_remove_count)
        if removed_total_count > 0 and new_items_count == 0 and updated_items_count == 0:
            message = f"Remoção no índice concluída. -{removed_total_count} removidos."
        elif new_items_count > 0 and removed_total_count == 0 and updated_items_count == 0:
            message = f"Adição ao índice concluída. +{new_items_count} adicionados."
        else:
            base = "Reparo de integridade concluído." if status.get('integrity_error') else "Índice atualizado."
            message = f"{base} +{new_items_count} adicionados, -{removed_total_count} removidos."
    if updated_items_count:
        message += f" • Atualizados: {updated_items_count}"
    if reset_local and not status.get('integrity_error'):
        message = "Reset do índice concluído. " + message
    if failed_items:
        message += f" ATENÇÃO: {len(failed_items)} itens falharam e não foram salvos: " + "; ".join(failed_items)

    _progress_finish(message)
    return {
        "status": "success", 
        "message": message
    }

def rebuild_dataset_from_reprocessed_db(src_path=None, dst_path=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    default_src = os.path.join(base_dir, "embeddings_creation", "vestidos_dataset_reprocessed_db.jsonl")
    default_dst = os.path.join(base_dir, "embeddings_creation", "vestidos_dataset.jsonl")
    src_path = src_path or default_src
    dst_path = dst_path or default_dst

    def _normalize_occasions(values):
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []

        out = []
        for v in values:
            s = str(v or "").strip()
            if not s:
                continue
            parts = re.split(r"[/,]|\s+e\s+", s)
            for p in parts:
                p = str(p or "").strip()
                if not p:
                    continue
                pl = _normalize_text(p)
                if "black tie" in pl or "gala" in pl:
                    out.append("Gala")
                elif "noiv" in pl:
                    out.append("Noiva")
                elif "civil" in pl:
                    out.append("Civil")
                elif "madrinha" in pl:
                    out.append("Madrinha")
                elif "formatura" in pl:
                    out.append("Formatura")
                elif "debut" in pl or "15 anos" in pl:
                    out.append("Debutante")
                elif ("mae" in pl or "mãe" in pl) and "noiv" in pl:
                    out.append("Mae dos Noivos")
                elif "convid" in pl:
                    out.append("Convidada")
                else:
                    out.append(p[:1].upper() + p[1:])

        seen = set()
        deduped = []
        for v in out:
            if v in seen:
                continue
            seen.add(v)
            deduped.append(v)
        return deduped

    def _uniq_text(seq):
        seen = set()
        out = []
        for x in seq:
            s = str(x or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    rows = []
    seen_ids = set()
    with open(src_path, "r", encoding="utf-8") as f:
        for line in f:
            line = (line or "").strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            cid = item.get("custom_id") or item.get("item_id")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)

            mf = item.get("metadata_filters") if isinstance(item.get("metadata_filters"), dict) else {}
            occ = _normalize_occasions(mf.get("occasions"))
            mf = dict(mf)
            if occ:
                mf["occasions"] = occ
            else:
                mf.pop("occasions", None)

            title = item.get("title") or ""
            desc = item.get("description") or ""
            embedding_text = " ".join(
                _uniq_text(
                    [title]
                    + occ
                    + (mf.get("colors") if isinstance(mf.get("colors"), list) else [])
                    + (mf.get("fabrics") if isinstance(mf.get("fabrics"), list) else [])
                    + (mf.get("silhouette") if isinstance(mf.get("silhouette"), list) else [])
                    + (mf.get("neckline") if isinstance(mf.get("neckline"), list) else [])
                    + (mf.get("sleeves") if isinstance(mf.get("sleeves"), list) else [])
                    + (mf.get("details") if isinstance(mf.get("details"), list) else [])
                    + (item.get("keywords") if isinstance(item.get("keywords"), list) else [])
                    + [desc]
                )
            )

            rows.append(
                {
                    "file_name": item.get("file_name") or f"{cid}.jpg",
                    "custom_id": cid,
                    "description": desc,
                    "title": title,
                    "metadata_filters": {k: v for k, v in mf.items() if v},
                    "embedding_text": embedding_text,
                }
            )

    with open(dst_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {"status": "success", "count": len(rows), "output": dst_path}
