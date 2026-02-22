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
from boto3.dynamodb.conditions import Key, Attr
from flask import Blueprint, request, jsonify, url_for, current_app, session, Response, stream_with_context
from dotenv import load_dotenv
from ai_sync_service import get_index_status, sync_index as service_sync_index, get_progress

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
MAX_SIMILARITY_LIMIT = 50
MAX_TOOL_LIMIT = 5
DEFAULT_CANDIDATE_MULTIPLIER = 6
DEFAULT_CANDIDATE_MIN = 20
SEARCH_BATCH_SIZE = 80
QUERY_REWRITE_CACHE_TTL = 15 * 60
QUERY_REWRITE_CACHE_MAXSIZE = 512
INDEX_FILE = "vector_store.index"
METADATA_FILE = "vector_store_metadata.pkl"
EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_PUBLIC_ACCOUNT_ID = "37d5b37f-c920-4090-a682-7e1ed2e31a0f"

# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------

def _sanitize_env_value(value):
    if not value:
        return None
    return value.strip().strip("'\"`")


def _normalize_langfuse_env():
    public_key = _sanitize_env_value(os.environ.get("LANGFUSE_PUBLIC_KEY"))
    secret_key = _sanitize_env_value(os.environ.get("LANGFUSE_SECRET_KEY"))
    base_url = _sanitize_env_value(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    )
    if base_url:
        base_url = base_url.rstrip("/")
    if public_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    if secret_key:
        os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url
        os.environ.setdefault("LANGFUSE_HOST", base_url)


def _build_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    try:
        from langfuse.openai import openai as langfuse_openai
        if api_key:
            langfuse_openai.api_key = api_key
        return langfuse_openai
    except Exception:
        from openai import OpenAI
        return OpenAI(api_key=api_key)


load_dotenv(override=False)
_normalize_langfuse_env()
client = _build_openai_client()

try:
    dynamodb = boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    itens_table = dynamodb.Table("alugueqqc_itens")
    users_table = dynamodb.Table("alugueqqc_users")
except Exception as e:
    print(f"Erro ao conectar DynamoDB em ai_routes: {e}")
    itens_table = None
    users_table = None

ai_bp = Blueprint('ai', __name__)

# ---------------------------------------------------------------------------
# Estado global com lock thread-safe
# ---------------------------------------------------------------------------
index = None
metadata = []
inventory_digest = {}
_load_lock = threading.Lock()
_query_rewrite_cache = {}

# ---------------------------------------------------------------------------
# Utilitários de texto
# ---------------------------------------------------------------------------

def _normalize_text(text):
    text = str(text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join([c for c in text if not unicodedata.combining(c)])
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = list(value)
    elif isinstance(value, str):
        raw = [v.strip() for v in value.split(",")]
    else:
        raw = [str(value)]
    return [v for v in raw if str(v).strip()]


def _normalize_set(value):
    return set(_normalize_text(v) for v in _ensure_list(value) if str(v).strip())


def _slugify(text):
    text = _normalize_text(text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def _split_multi_text(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if isinstance(x, str) and x.strip()]
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[,\|/;]+", text)
    return [str(p).strip() for p in parts if str(p).strip()]

# ---------------------------------------------------------------------------
# Ocasiões
# ---------------------------------------------------------------------------

_OCCASION_LABEL_BY_SLUG = {
    "noiva": "Noiva",
    "civil": "Civil",
    "madrinha": "Madrinha",
    "mae-dos-noivos": "Mãe dos Noivos",
    "formatura": "Formatura",
    "debutante": "Debutante",
    "gala": "Gala",
    "convidada": "Convidada",
}

_OCCASION_FLAG_MAP = [
    ("occasion_noiva", "noiva"),
    ("occasion_civil", "civil"),
    ("occasion_madrinha", "madrinha"),
    ("occasion_mae_dos_noivos", "mae-dos-noivos"),
    ("occasion_formatura", "formatura"),
    ("occasion_debutante", "debutante"),
    ("occasion_gala", "gala"),
    ("occasion_convidada", "convidada"),
]


def _canonical_occasion(value):
    v = _slugify(value)
    if not v:
        return ""
    if v in ["black-tie", "blacktie", "gala"]:
        return "gala"
    if v in ["mae-dos-noivos", "mae-dos-noivas", "mae-do-noivo", "mae-da-noiva"]:
        return "mae-dos-noivos"
    return v


def _normalize_occasion_inputs(value):
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    seen = set()
    out = []
    for v in raw:
        canon = _canonical_occasion(v)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _flag_is_set(value):
    return value in ("1", 1, True)


def _get_occasion_slugs(item):
    if not isinstance(item, dict):
        return []
    out = []
    seen = set()
    for key, slug in _OCCASION_FLAG_MAP:
        if not _flag_is_set(item.get(key)):
            continue
        canon = _canonical_occasion(slug)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _get_occasions_list(item):
    occasions = []
    for key, slug in _OCCASION_FLAG_MAP:
        if _flag_is_set(item.get(key)):
            occasions.append(_OCCASION_LABEL_BY_SLUG.get(slug, slug))
    return ", ".join(occasions)


def _has_occasion(meta, target_occasion):
    if not isinstance(meta, dict):
        return False
    target = _canonical_occasion(target_occasion)
    if not target:
        return False
    mf = meta.get("metadata_filters") or {}
    occ = mf.get("occasions")
    if not isinstance(occ, list):
        return False
    return any(_canonical_occasion(o) == target for o in occ)


def _meta_occasions(meta):
    mf = meta.get("metadata_filters") or {}
    occ = mf.get("occasions")
    if isinstance(occ, list):
        return _normalize_occasion_inputs(occ)
    return []


def _filter_items_by_occasions(items, target_occasions):
    if not items or not target_occasions:
        return items
    target_set = set(target_occasions)
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slugs = _get_occasion_slugs(item)
        if slugs:
            if set(slugs) & target_set:
                filtered.append(item)
            continue
        if any(_has_occasion(item, occ) for occ in target_set):
            filtered.append(item)
    return filtered


def _suggestion_occ_slugs(suggestion):
    if not isinstance(suggestion, dict):
        return []
    occs = suggestion.get("occasions")
    if isinstance(occs, str):
        parts = [p.strip() for p in occs.split(",") if p.strip()]
        parts = [p for p in parts if _normalize_text(p) != "varias"]
        return _normalize_occasion_inputs(parts)
    if isinstance(occs, list):
        return _normalize_occasion_inputs(occs)
    return []

# ---------------------------------------------------------------------------
# Extratores de cor e tamanho (único conjunto, usado em todo o módulo)
# ---------------------------------------------------------------------------

def _extract_color_value(obj):
    v = obj.get("cor") or obj.get("cores") or obj.get("color") or obj.get("colors")
    if isinstance(v, (list, tuple)):
        return next((x.strip() for x in v if isinstance(x, str) and x.strip()), "")
    return str(v).strip() if isinstance(v, str) else ""


def _extract_color_base_value(obj):
    v = obj.get("cor_base") or obj.get("color_base")
    if isinstance(v, (list, tuple)):
        return next((x.strip() for x in v if isinstance(x, str) and x.strip()), "")
    if isinstance(v, str):
        return v.strip()
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        v = mf.get("cor_base")
        if isinstance(v, (list, tuple)):
            return next((x.strip() for x in v if isinstance(x, str) and x.strip()), "")
        if isinstance(v, str):
            return v.strip()
    return ""


def _extract_color_commercial_value(obj):
    v = obj.get("cor_comercial") or obj.get("color_comercial")
    if isinstance(v, (list, tuple)):
        return next((x.strip() for x in v if isinstance(x, str) and x.strip()), "")
    return str(v).strip() if isinstance(v, str) else ""


def _extract_color_list(obj):
    if not isinstance(obj, dict):
        return []
    values = []
    for key in ["cor_base", "color_base", "cor_comercial", "color_comercial"]:
        values.extend(_ensure_list(obj.get(key)))
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        values.extend(_ensure_list(mf.get("cor_base")))
        values.extend(_ensure_list(mf.get("cor_comercial")))
    return [v for v in values if str(v).strip()]


def _extract_color_base_list(obj):
    if not isinstance(obj, dict):
        return []
    values = []
    for key in ["cor_base", "color_base"]:
        values.extend(_ensure_list(obj.get(key)))
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        values.extend(_ensure_list(mf.get("cor_base")))
    return [v for v in values if str(v).strip()]


def _extract_color_commercial_list(obj):
    if not isinstance(obj, dict):
        return []
    values = []
    for key in ["cor_comercial", "color_comercial"]:
        values.extend(_ensure_list(obj.get(key)))
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        values.extend(_ensure_list(mf.get("cor_comercial")))
    return [v for v in values if str(v).strip()]


def _extract_size_list(obj):
    v = obj.get("tamanho") or obj.get("size") or obj.get("item_tamanho") or obj.get("item_size") or obj.get("sizes")
    if isinstance(v, (list, tuple)):
        return [x for x in v if str(x).strip()]
    if isinstance(v, str):
        text = v.replace(" e ", ",")
        return _split_multi_text(text)
    return []


def _extract_size_value(obj):
    vals = _extract_size_list(obj)
    return vals[0].strip() if vals else ""


def _db_color_base_list(item):
    if not isinstance(item, dict):
        return []
    return _split_multi_text(item.get("cor_base") or item.get("color_base"))


def _db_color_commercial_list(item):
    if not isinstance(item, dict):
        return []
    values = []
    for key in ["cor_comercial", "color_comercial", "cor", "cores", "color", "colors"]:
        values.extend(_split_multi_text(item.get(key)))
    seen = set()
    unique = []
    for v in values:
        k = _normalize_text(v)
        if not k or k in seen:
            continue
        seen.add(k)
        unique.append(v)
    return unique

# ---------------------------------------------------------------------------
# Correspondência
# ---------------------------------------------------------------------------

def _matches_any(value, desired_set):
    if not desired_set:
        return True
    actual = _normalize_set(value)
    return bool(actual & desired_set)


def _check_facet_match(vals, reqs):
    if not vals or not reqs:
        return False
    for r in reqs:
        for v in vals:
            if r == v:
                return True
            if len(r) > 2 and r in v:
                return True
    return False

# ---------------------------------------------------------------------------
# Conta pública / cores
# ---------------------------------------------------------------------------

def _pick_public_account_id():
    raw = str(os.getenv("AI_SYNC_ACCOUNT_ID") or os.getenv("PUBLIC_CATALOG_ACCOUNT_IDS") or "").strip()
    if not raw:
        return DEFAULT_PUBLIC_ACCOUNT_ID
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise RuntimeError("Conta pública inválida. Configure AI_SYNC_ACCOUNT_ID ou PUBLIC_CATALOG_ACCOUNT_IDS.")
    return parts[0]


def _get_account_id():
    account_id = session.get("account_id") if session else None
    return account_id or _pick_public_account_id()


def _load_color_options_for_account(account_id):
    if not account_id:
        raise RuntimeError("account_id ausente para carregar cores.")
    if not users_table:
        raise RuntimeError("users_table indisponível para carregar cores.")
    resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
    item = resp.get("Item") or {}
    if not item:
        raise RuntimeError(f"account_settings não encontrado para {account_id}.")
    colors = item.get("color_options")
    if not isinstance(colors, list):
        raise RuntimeError(f"color_options inválido para {account_id}.")
    out = []
    seen = set()
    for c in colors:
        if c is None:
            continue
        if isinstance(c, dict):
            s = c.get("name") or c.get("color") or c.get("comercial") or c.get("cor")
        else:
            s = c
        s = str(s).strip() if s is not None else ""
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    if not out:
        raise RuntimeError(f"color_options vazio para {account_id}.")
    return sorted(out, key=lambda x: str(x).casefold())


def _load_color_pairs_for_account(account_id):
    if not account_id:
        raise RuntimeError("account_id ausente para carregar cores.")
    if not users_table:
        raise RuntimeError("users_table indisponível para carregar cores.")
    resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
    item = resp.get("Item") or {}
    if not item:
        raise RuntimeError(f"account_settings não encontrado para {account_id}.")
    colors = item.get("color_options")
    if not isinstance(colors, list):
        raise RuntimeError(f"color_options inválido para {account_id}.")
    out = []
    seen = set()
    for c in colors:
        if c is None:
            continue
        if isinstance(c, dict):
            name = c.get("name") or c.get("color") or c.get("comercial") or c.get("cor")
            base = c.get("base") or c.get("cor_base") or c.get("base_color")
        else:
            name = c
            base = ""
        name = str(name).strip() if name else ""
        base = str(base).strip() if base else ""
        if not name:
            continue
        key = (base.casefold(), name.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "base": base})
    if not out:
        raise RuntimeError(f"color_options vazio para {account_id}.")
    return sorted(out, key=lambda x: (x["base"].casefold(), x["name"].casefold()))


def _color_enums_for_account():
    account_id = _get_account_id()
    color_pairs = _load_color_pairs_for_account(account_id)
    cor_base_options = list(dict.fromkeys(
        str(c.get("base") or "").strip() for c in color_pairs if str(c.get("base") or "").strip()
    ))
    cor_comercial_options = list(dict.fromkeys(
        str(c.get("name") or "").strip() for c in color_pairs if str(c.get("name") or "").strip()
    ))
    if not cor_base_options:
        raise RuntimeError(f"cor_base vazio para {account_id}.")
    if not cor_comercial_options:
        raise RuntimeError(f"cor_comercial vazio para {account_id}.")
    return cor_base_options, cor_comercial_options


def _expand_color_terms(colors):
    raw = _ensure_list(colors)
    expanded = []
    available = _load_color_options_for_account(_get_account_id())
    for c in raw:
        norm = _normalize_text(c)
        expanded.append(c)
        for opt in available:
            opt_norm = _normalize_text(opt)
            if norm and (opt_norm == norm or opt_norm.startswith(f"{norm} ") or f" {norm}" in opt_norm):
                expanded.append(opt)
    seen = set()
    unique = []
    for c in expanded:
        key = _normalize_text(c)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique

# ---------------------------------------------------------------------------
# Coerção e validação de argumentos de busca
# ---------------------------------------------------------------------------

def _coerce_similarity_color_fields(args):
    if not isinstance(args, dict):
        return args
    cor_base = args.get("cor_base") or args.get("corBase") or []
    cor_comercial = args.get("cor_comercial") or args.get("corComercial") or []
    if not isinstance(cor_base, list) or not isinstance(cor_comercial, list):
        return args
    try:
        cor_base_options, cor_comercial_options = _color_enums_for_account()
    except Exception:
        return args
    base_by_norm = {_normalize_text(v): v for v in cor_base_options if str(v).strip()}
    comercial_by_norm = {_normalize_text(v): v for v in cor_comercial_options if str(v).strip()}
    unknown_terms = []
    moved_to_comercial = []
    kept_base = []
    for v in cor_base:
        if not isinstance(v, str) or not v.strip():
            continue
        vn = _normalize_text(v)
        if vn in base_by_norm:
            kept_base.append(base_by_norm[vn])
        elif vn in comercial_by_norm:
            moved_to_comercial.append(comercial_by_norm[vn])
        else:
            unknown_terms.append(v.strip())
    moved_to_base = []
    kept_comercial = []
    for v in cor_comercial:
        if not isinstance(v, str) or not v.strip():
            continue
        vn = _normalize_text(v)
        if vn in comercial_by_norm:
            kept_comercial.append(comercial_by_norm[vn])
        elif vn in base_by_norm:
            moved_to_base.append(base_by_norm[vn])
        else:
            unknown_terms.append(v.strip())
    kept_base = list(dict.fromkeys(kept_base + moved_to_base))
    kept_comercial = list(dict.fromkeys(kept_comercial + moved_to_comercial))
    if "cor_base" in args:
        args["cor_base"] = kept_base
    elif "corBase" in args:
        args["corBase"] = kept_base
    if "cor_comercial" in args:
        args["cor_comercial"] = kept_comercial
    elif "corComercial" in args:
        args["corComercial"] = kept_comercial
    if unknown_terms:
        args["_unknown_color_terms"] = list(dict.fromkeys(t for t in unknown_terms if t))
        existing = str(args.get("other_characteristics") or "").strip()
        suffix = " ".join(t for t in unknown_terms if t)
        args["other_characteristics"] = (existing + " " + suffix).strip() if existing else suffix
    return args


def _validate_similarity_args(args):
    errors = []

    def check_list(keys, label):
        for key in keys:
            if key in args and args[key] is not None and not isinstance(args[key], list):
                errors.append({"field": label, "message": f"{label} deve ser lista"})
        for key in keys:
            if key in args and args[key] is not None:
                return args[key]
        return []

    cor_base = check_list(["cor_base", "corBase"], "cor_base")
    cor_comercial = check_list(["cor_comercial", "corComercial"], "cor_comercial")
    occasions = check_list(["occasions", "ocasioes", "occasion"], "occasions")
    check_list(["sizes", "tamanhos", "size", "tamanho"], "sizes")

    if errors:
        return errors

    try:
        cor_base_options, cor_comercial_options = _color_enums_for_account()
    except Exception:
        cor_base_options, cor_comercial_options = [], []

    if cor_base_options and isinstance(cor_base, list):
        filtered = [v for v in cor_base if v in cor_base_options]
        for key in ["cor_base", "corBase"]:
            if key in args:
                args[key] = filtered
    if cor_comercial_options and isinstance(cor_comercial, list):
        filtered = [v for v in cor_comercial if v in cor_comercial_options]
        for key in ["cor_comercial", "corComercial"]:
            if key in args:
                args[key] = filtered
    if isinstance(occasions, list):
        normalized = _normalize_occasion_inputs(occasions)
        for key in ["occasions", "ocasioes", "occasion"]:
            if key in args:
                args[key] = normalized
                break
    return []


def _prepare_search_params(args):
    """Extrai e normaliza os parâmetros comuns de busca de um dict de args."""
    args = _coerce_similarity_color_fields(args)
    validation_errors = _validate_similarity_args(args)
    if validation_errors:
        return None, validation_errors

    cor_base = args.get("cor_base") or args.get("corBase") or []
    cor_comercial = args.get("cor_comercial") or args.get("corComercial") or []
    sizes = args.get("sizes") or args.get("tamanhos") or args.get("size") or args.get("tamanho") or []
    occasions = _normalize_occasion_inputs(
        args.get("occasions") or args.get("ocasioes") or args.get("occasion") or []
    )
    limit = max(1, min(int(args.get("limit") or args.get("k") or 5), MAX_TOOL_LIMIT))
    query = str(args.get("other_characteristics") or "").strip()
    unknown_color_terms = args.get("_unknown_color_terms") or []

    return {
        "cor_base": cor_base,
        "cor_comercial": cor_comercial,
        "sizes": sizes,
        "occasions": occasions,
        "limit": limit,
        "query": query,
        "unknown_color_terms": unknown_color_terms,
        "desired_base": _normalize_set(cor_base),
        "desired_comercial": _normalize_set(cor_comercial),
        "desired_sizes": _normalize_set(sizes),
        "desired_occ_set": set(occasions),
    }, []

# ---------------------------------------------------------------------------
# Carregamento do índice (thread-safe)
# ---------------------------------------------------------------------------

def load_resources():
    global index, metadata, inventory_digest
    with _load_lock:
        if index and metadata:
            return
        try:
            if os.path.exists(INDEX_FILE) and os.path.exists(METADATA_FILE):
                index = faiss.read_index(INDEX_FILE)
                with open(METADATA_FILE, "rb") as f:
                    metadata = pickle.load(f)
                inventory_digest = _build_inventory_digest(metadata)
                print("Recursos de IA carregados com sucesso.")
            else:
                print("Arquivos de índice/metadata não encontrados.")
        except Exception as e:
            print(f"Erro ao carregar recursos de IA: {e}")


def _ensure_resources():
    """Garante que o índice está carregado antes de usar."""
    if not index or not metadata:
        load_resources()

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def get_embedding(text):
    text = text.replace("\n", " ")
    return client.embeddings.create(input=[text], model=EMBEDDING_MODEL).data[0].embedding

# ---------------------------------------------------------------------------
# Digest do inventário
# ---------------------------------------------------------------------------

def _seed_inventory_context():
    return {
        "estilos": ["Sereia", "Princesa", "Evasê", "Reto", "Boho Chic", "Minimalista", "Clássico", "Moderno"],
        "tecidos": ["Zibelina", "Renda", "Tule", "Cetim", "Seda", "Crepe", "Chiffon"],
        "decotes": ["Tomara que caia", "Decote em V", "Canoa", "Ombro a ombro", "Frente única"],
        "detalhes": ["Brilho", "Pedraria", "Cauda longa", "Fenda", "Manga longa", "Costas abertas"],
        "ocasioes": ["Noiva", "Civil", "Madrinha", "Mãe dos Noivos", "Formatura", "Debutante", "Gala", "Convidada"],
    }


def _top_values(counts, limit=12):
    items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [v for v, _ in items[:limit]]


def _build_inventory_digest(meta_list):
    seed = _seed_inventory_context()
    counts = {k: {} for k in seed.keys()}
    for m in meta_list or []:
        if not isinstance(m, dict):
            continue
        mf = m.get("metadata_filters") or {}

        def bump(facet, values):
            if not values:
                return
            target = counts.get(facet)
            if target is None:
                return
            for v in values:
                key = str(v).strip()
                if key:
                    target[key] = target.get(key, 0) + 1

        bump("tecidos", mf.get("fabrics"))
        bump("estilos", mf.get("silhouette"))
        bump("decotes", mf.get("neckline"))
        bump("detalhes", mf.get("details"))
        bump("ocasioes", mf.get("occasions"))

    digest = {}
    for facet, seed_values in seed.items():
        facet_counts = counts.get(facet, {})
        observed = _top_values(facet_counts, limit=12)
        combined = list(dict.fromkeys(seed_values + observed))
        digest[facet] = combined[:16]
    return digest

# ---------------------------------------------------------------------------
# Panorama cores/ocasiões
# ---------------------------------------------------------------------------

def _build_color_occasion_panorama(meta_list):
    counts = {}
    total = 0
    for item in meta_list or []:
        if not isinstance(item, dict):
            continue
        cor_base = _extract_color_base_value(item)
        cor_comercial = _extract_color_commercial_value(item)
        if not cor_base and not cor_comercial:
            continue
        occasions = _meta_occasions(item) or ["Sem ocasião"]
        total += 1
        for occ in occasions:
            occ_key = str(occ).strip() or "Sem ocasião"
            bucket = counts.setdefault(occ_key, {})
            key = (cor_base or "", cor_comercial or "")
            bucket[key] = bucket.get(key, 0) + 1
    payload = []
    for occ in sorted(counts.keys()):
        entries = sorted(
            [{"cor_base": base, "cor_comercial": comercial, "count": qty}
             for (base, comercial), qty in counts[occ].items()],
            key=lambda x: (-x["count"], x["cor_base"], x["cor_comercial"])
        )
        payload.append({"occasion": occ, "colors": entries})
    return {"total_items": total, "occasions": payload}


def _build_color_occasion_panorama_from_db(account_id):
    if not itens_table:
        return {"total_items": 0, "occasions": []}
    counts = {}
    total = 0
    projection = (
        "item_id, #st, account_id, item_image_url, image_url, cor, cores, cor_base, cor_comercial, "
        "occasion_noiva, occasion_civil, occasion_madrinha, occasion_mae_dos_noivos, "
        "occasion_formatura, occasion_debutante, occasion_gala, occasion_convidada"
    )
    kwargs = {
        "ProjectionExpression": projection,
        "ExpressionAttributeNames": {"#st": "status"},
        "FilterExpression": Attr("status").eq("available") & Attr("account_id").eq(str(account_id)),
    }
    resp = itens_table.scan(**kwargs)
    while True:
        for item in resp.get("Items", []) or []:
            if not (item.get("item_image_url") or item.get("image_url")):
                continue
            base_list = _db_color_base_list(item)
            comercial_list = _db_color_commercial_list(item)
            cor_base = base_list[0].strip() if base_list else ""
            cor_comercial = comercial_list[0].strip() if comercial_list else ""
            if not cor_base and not cor_comercial:
                continue
            slugs = _get_occasion_slugs(item)
            occ_labels = [_OCCASION_LABEL_BY_SLUG.get(s, s) for s in slugs] if slugs else ["Sem ocasião"]
            total += 1
            for occ_label in occ_labels:
                occ_key = str(occ_label).strip() or "Sem ocasião"
                bucket = counts.setdefault(occ_key, {})
                key = (cor_base or "", cor_comercial or "")
                bucket[key] = bucket.get(key, 0) + 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        resp = itens_table.scan(ExclusiveStartKey=lek, **kwargs)
    payload = []
    for occ in sorted(counts.keys()):
        entries = sorted(
            [{"cor_base": base, "cor_comercial": comercial, "count": qty}
             for (base, comercial), qty in counts[occ].items()],
            key=lambda x: (-x["count"], x["cor_base"], x["cor_comercial"])
        )
        payload.append({"occasion": occ, "colors": entries})
    return {"total_items": total, "occasions": payload}

# ---------------------------------------------------------------------------
# Categoria
# ---------------------------------------------------------------------------

def _category_slug(meta):
    if not isinstance(meta, dict):
        return "festa"
    mf = meta.get("metadata_filters")
    if isinstance(mf, dict):
        occ = mf.get("occasions")
        if isinstance(occ, list) and occ:
            if any("noiv" in _normalize_text(o) or "civil" in _normalize_text(o) for o in occ):
                return "noiva"
            return "festa"
    raw = str(
        meta.get("category_slug") or meta.get("item_category") or meta.get("category") or meta.get("categoria") or ""
    ).lower().strip()
    return "noiva" if ("noiv" in raw or "civil" in raw) else "festa"

# ---------------------------------------------------------------------------
# Validação/enriquecimento via DynamoDB
# ---------------------------------------------------------------------------

def validate_and_enrich_candidates(candidates_metadata):
    if not itens_table or not candidates_metadata:
        return candidates_metadata

    unique_candidates = {}
    keys_to_fetch = []
    for meta in candidates_metadata:
        item_id = meta.get('custom_id')
        if item_id and item_id not in unique_candidates:
            unique_candidates[item_id] = meta
            keys_to_fetch.append({'item_id': item_id})

    if not keys_to_fetch:
        return []

    fetched_items = {}
    try:
        for i in range(0, len(keys_to_fetch), 100):
            chunk = keys_to_fetch[i:i + 100]
            response = dynamodb.batch_get_item(
                RequestItems={
                    'alugueqqc_itens': {
                        'Keys': chunk,
                        'ProjectionExpression': (
                            'item_id, #st, title, item_title, item_description, item_image_url, item_value, '
                            'item_custom_id, category, item_category, cor, cores, cor_base, cor_comercial, tamanho, '
                            'occasion_noiva, occasion_civil, occasion_madrinha, occasion_mae_dos_noivos, '
                            'occasion_formatura, occasion_debutante, occasion_gala, occasion_convidada'
                        ),
                        'ExpressionAttributeNames': {'#st': 'status'}
                    }
                }
            )
            for item in response.get('Responses', {}).get('alugueqqc_itens', []):
                fetched_items[item['item_id']] = item
    except Exception as e:
        print(f"Erro no BatchGetItem: {e}")
        return []

    valid_items = []
    seen_ids = set()
    for meta in candidates_metadata:
        item_id = meta.get('custom_id')
        if not item_id or item_id in seen_ids:
            continue
        db_item = fetched_items.get(item_id)
        if not db_item or db_item.get('status') != 'available':
            continue
        seen_ids.add(item_id)
        s3_url = db_item.get('item_image_url')
        if s3_url:
            meta['imageUrl'] = s3_url
            meta['file_name'] = None
        meta['title'] = db_item.get('title') or db_item.get('item_title') or meta.get('title')
        meta['description'] = db_item.get('item_description', meta.get('description'))
        val = db_item.get('item_value', 0)
        if val:
            meta['price'] = f"R$ {val}"
        meta['customId'] = db_item.get('item_custom_id')
        meta['item_id'] = db_item.get('item_id')
        for field in ['cor', 'cores', 'cor_base', 'cor_comercial', 'tamanho']:
            meta[field] = db_item.get(field, meta.get(field))
        cat_slug = _category_slug(meta)
        meta['item_category'] = db_item.get('item_category', meta.get('item_category'))
        meta['category_slug'] = cat_slug
        for occ_key, _ in _OCCASION_FLAG_MAP:
            if db_item.get(occ_key):
                meta[occ_key] = db_item.get(occ_key)
        mf = meta.get("metadata_filters") or {}
        occ = mf.get("occasions")
        if isinstance(occ, list) and occ and str(occ[0]).strip():
            meta["category"] = str(occ[0]).strip()
        else:
            meta['category'] = 'Noiva' if cat_slug == 'noiva' else 'Festa'
        valid_items.append(meta)
    return valid_items

# ---------------------------------------------------------------------------
# Reescrita de query
# ---------------------------------------------------------------------------

def _evict_expired_cache():
    """Remove entradas expiradas do cache, respeitando o tamanho máximo."""
    now = time.time()
    expired = [k for k, v in _query_rewrite_cache.items() if now - v.get("ts", 0) > QUERY_REWRITE_CACHE_TTL]
    for k in expired:
        _query_rewrite_cache.pop(k, None)
    # Se ainda maior que o max, remove as mais antigas
    if len(_query_rewrite_cache) > QUERY_REWRITE_CACHE_MAXSIZE:
        sorted_keys = sorted(_query_rewrite_cache.items(), key=lambda x: x[1].get("ts", 0))
        for k, _ in sorted_keys[:len(_query_rewrite_cache) - QUERY_REWRITE_CACHE_MAXSIZE]:
            _query_rewrite_cache.pop(k, None)


def _rewrite_catalog_query(query, target_occasion):
    if not isinstance(query, str) or not query.strip():
        return {"query_reescrita": ""}

    target_slug = _canonical_occasion(target_occasion)
    cache_key = f"{target_slug or 'all'}|{_normalize_text(query)}"
    now = time.time()
    cached = _query_rewrite_cache.get(cache_key)
    if cached and (now - cached.get("ts", 0)) <= QUERY_REWRITE_CACHE_TTL:
        return cached.get("data") or {"query_reescrita": query}

    digest_for_prompt = {}
    if isinstance(inventory_digest, dict):
        for facet, values in inventory_digest.items():
            if isinstance(values, list):
                digest_for_prompt[facet] = [str(v) for v in values if str(v).strip()]

    account_id = _get_account_id()
    available_colors = _load_color_options_for_account(account_id) if account_id else []
    if available_colors:
        digest_for_prompt.setdefault("cores", [])
        digest_for_prompt["cores"].extend(available_colors)
    for facet in list(digest_for_prompt.keys()):
        digest_for_prompt[facet] = list(dict.fromkeys(digest_for_prompt[facet]))[:32]

    system_prompt = (
        "Você reescreve consultas para busca semântica em um catálogo de vestidos.\n"
        "Objetivo: transformar a mensagem do usuário em uma consulta curta, explícita e útil para embeddings.\n"
        "O contexto do inventário é apenas exemplos observados; não se restrinja a ele.\n"
        "Se existir uma ocasião alvo (aba ativa), use-a como pista para priorizar termos e desambiguar, mas não invente restrições que o usuário não pediu.\n"
        "Se o usuário pedir um atributo/valor que não aparece nos exemplos, mantenha mesmo assim e liste em atributos_novos.\n"
        "Não invente que o inventário possui algo; apenas descreva preferências do usuário.\n"
        "Trate negativas: quando houver 'sem X' ou 'não X', registre X em termos_excluir e também como 'não X' no facet apropriado em atributos_extraidos.\n"
        "Extraia cores em dois campos: cor_base (cores genéricas) e cor_comercial (nomes comerciais). Se houver só uma cor sem qualificador, use cor_base.\n"
        "Responda apenas com JSON válido (sem markdown)."
    )
    user_payload = {
        "ocasiao_alvo": target_slug,
        "inventario_contexto_exemplos": digest_for_prompt,
        "consulta_usuario": query,
        "saida": {
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
            start = content.find("{")
            end = content.rfind("}")
            data = json.loads(content[start:end + 1]) if start != -1 and end > start else {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("query_reescrita"), str) or not data.get("query_reescrita").strip():
            data["query_reescrita"] = query
        data["ocasiao_alvo"] = target_slug

        _evict_expired_cache()
        _query_rewrite_cache[cache_key] = {"ts": now, "data": data}
        return data
    except Exception as e:
        print(f"Erro ao reescrever query: {e}")
        return {"query_reescrita": query, "ocasiao_alvo": target_slug}

# ---------------------------------------------------------------------------
# Helpers de embedding a partir de rewrite
# ---------------------------------------------------------------------------

_ATTR_KEY_MAP = {
    "cor base": "cor_base",
    "cor comercial": "cor_comercial",
    "estilo": "silhouette", "estilos": "silhouette",
    "decote": "neckline", "decotes": "neckline",
    "manga": "sleeves", "mangas": "sleeves",
    "detalhes": "details",
    "tecido": "fabrics", "tecidos": "fabrics",
    "ocasiao": "occasions", "ocasião": "occasions",
    "ocasioes": "occasions", "ocasiões": "occasions",
}


def _get_attr_from_rewrite(attrs, canonical_key, key_map=None):
    if not isinstance(attrs, dict):
        return None
    if canonical_key in attrs:
        return attrs.get(canonical_key)
    for kk, vv in (key_map or _ATTR_KEY_MAP).items():
        if vv == canonical_key and kk in attrs:
            return attrs.get(kk)
    return None


def _tokens_from_attrs(attrs, order, include_occasions=False):
    tokens = []
    for k in order:
        v = _get_attr_from_rewrite(attrs, k)
        if isinstance(v, list):
            for x in v:
                s = str(x).strip()
                if s:
                    tokens.append(s)
                    if include_occasions and k == "occasions":
                        canon = _canonical_occasion(s)
                        if canon:
                            tokens.append(canon.replace("-", " "))
        elif isinstance(v, str) and v.strip():
            tokens.append(v.strip())
            if include_occasions and k == "occasions":
                canon = _canonical_occasion(v.strip())
                if canon:
                    tokens.append(canon.replace("-", " "))
    return tokens


def _query_embedding_text_from_rewrite(data, target_occasion=None):
    if not isinstance(data, dict):
        return None
    attrs = data.get("atributos_extraidos")
    order = ["silhouette", "neckline", "sleeves", "details", "fabrics", "cor_base", "cor_comercial", "occasions"]
    tokens = _tokens_from_attrs(attrs or {}, order, include_occasions=True)
    occ = _canonical_occasion(target_occasion)
    if occ:
        tokens.append(occ.replace("-", " "))
    tokens = list(dict.fromkeys(t for t in tokens if t))
    return " ".join(tokens) if tokens else None


def _query_embedding_text_from_rewrite_attrs_only(data):
    if not isinstance(data, dict):
        return None
    attrs = data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return None
    order = ["silhouette", "neckline", "sleeves", "details", "fabrics", "cor_base", "cor_comercial"]
    tokens = list(dict.fromkeys(t for t in _tokens_from_attrs(attrs, order) if t))
    return " ".join(tokens) if tokens else None

# ---------------------------------------------------------------------------
# Filtros e reranking por facets
# ---------------------------------------------------------------------------

def _build_facet_needs(attrs, include_occasions=True):
    """Extrai sets de requisitos por facet a partir de atributos extraídos."""
    need = {}
    color_base_need = set()
    color_comercial_need = set()

    def collect_colors(keys):
        out = []
        for key in keys:
            if key not in attrs:
                continue
            raw = attrs.get(key)
            out.extend(raw if isinstance(raw, list) else [raw] if isinstance(raw, str) and raw.strip() else [])
        return out

    base_vals = collect_colors(["cor_base", "cor base"])
    comercial_vals = collect_colors(["cor_comercial", "cor comercial"])
    if base_vals:
        color_base_need = set(_normalize_text(s) for s in base_vals if str(s).strip())
    if comercial_vals:
        color_comercial_need = set(_normalize_text(s) for s in comercial_vals if str(s).strip())

    facets = ["silhouette", "neckline", "sleeves", "details", "fabrics"]
    if include_occasions:
        facets.append("occasions")

    for k in facets:
        v = _get_attr_from_rewrite(attrs, k)
        if isinstance(v, list):
            if k == "occasions":
                vals = set(x for x in (_canonical_occasion(s) for s in v if str(s).strip()) if x)
            else:
                vals = set(_normalize_text(s) for s in v if str(s).strip())
            if vals:
                need[k] = vals
        elif isinstance(v, str) and v.strip():
            val = _canonical_occasion(v) if k == "occasions" else _normalize_text(v)
            if val:
                need[k] = {val}

    return color_base_need, color_comercial_need, need


def _apply_facet_constraints(candidates, rewrite_data):
    if not candidates or not isinstance(rewrite_data, dict):
        return candidates
    attrs = rewrite_data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return candidates

    color_base_req, color_comercial_req, req = _build_facet_needs(attrs, include_occasions=True)

    exclude_terms = set()
    te = rewrite_data.get("termos_excluir")
    if isinstance(te, list):
        exclude_terms = set(_normalize_text(t) for t in te if t)

    req_neg = {}
    for k, vals in req.items():
        neg = set()
        for s in vals:
            if k == "details" and (s.startswith("sem ") or s.startswith("sem-")):
                neg.add(re.sub(r"^sem[-\s]", "", s).strip())
            if s.startswith("nao ") or s.startswith("não "):
                neg.add(re.sub(r"^n[aã]o\s", "", s).strip())
        if neg:
            req_neg[k] = neg

    if not req and not color_base_req and not color_comercial_req:
        return candidates

    def has_intersection(values, needed, facet):
        if not isinstance(values, list):
            return False
        if facet == "occasions":
            nv = set(x for x in (_canonical_occasion(v) for v in values if str(v).strip()) if x)
            return bool(nv & needed)
        return _check_facet_match(set(_normalize_text(x) for x in values if str(x).strip()), needed)

    filtered = []
    for c in candidates:
        mf = c.get("metadata_filters") or {}
        ok = True
        if color_base_req or color_comercial_req:
            mf_colors = mf.get("colors")
            if not isinstance(mf_colors, list):
                ok = False
            else:
                nv = set(_normalize_text(x) for x in mf_colors if str(x).strip())
                if color_base_req and not nv & color_base_req:
                    ok = False
                if ok and color_comercial_req and not nv & color_comercial_req:
                    ok = False
        for k in ["silhouette", "occasions"]:
            if ok and k in req and not has_intersection(mf.get(k), req[k], k):
                ok = False
                break
        if ok and req_neg:
            def unify(values):
                return set(re.sub(r"[-\s]+", "", _normalize_text(x)) for x in values if str(x).strip())
            for facet, negs in req_neg.items():
                vals = mf.get(facet)
                if isinstance(vals, list):
                    present = unify(vals)
                    neg_unified = set(re.sub(r"[-\s]+", "", s) for s in negs)
                    if present & neg_unified:
                        ok = False
                        break
        if ok and exclude_terms:
            all_vals = [x for fv in mf.values() if isinstance(fv, list) for x in fv]
            if set(_normalize_text(x) for x in all_vals if str(x).strip()) & exclude_terms:
                ok = False
        if ok:
            filtered.append(c)
    return filtered if filtered else candidates


def _rerank_by_facets(candidates, rewrite_data, loose=False):
    if not candidates or not isinstance(rewrite_data, dict):
        return candidates
    attrs = rewrite_data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return candidates

    color_base_need, color_comercial_need, need = _build_facet_needs(attrs, include_occasions=not loose)
    weights = {
        "color_base": 15 if loose else 2,
        "color_comercial": 15 if loose else 2,
        "silhouette": 2, "neckline": 1, "sleeves": 1, "details": 1, "fabrics": 1, "occasions": 1
    }

    def score(c):
        mf = c.get("metadata_filters") or {}
        s = 0
        if color_base_need or color_comercial_need:
            vals = mf.get("colors")
            if isinstance(vals, list):
                nv = set(_normalize_text(x) for x in vals if str(x).strip())
                match_fn = _check_facet_match if loose else lambda a, b: bool(a & b)
                if color_base_need and match_fn(nv, color_base_need):
                    s += weights["color_base"]
                if color_comercial_need and match_fn(nv, color_comercial_need):
                    s += weights["color_comercial"]
        for k, req in need.items():
            vals = mf.get(k)
            if isinstance(vals, list):
                if k == "occasions":
                    nv = set(x for x in (_canonical_occasion(v) for v in vals if str(v).strip()) if x)
                else:
                    nv = set(_normalize_text(x) for x in vals if str(x).strip())
                if nv & req:
                    s += weights.get(k, 1)
        return s

    return [c for _, _, c in sorted(
        [(score(c), i, c) for i, c in enumerate(candidates)],
        key=lambda x: (-x[0], x[1])
    )]


def _rerank_by_facets_loose(candidates, rewrite_data):
    return _rerank_by_facets(candidates, rewrite_data, loose=True)

# ---------------------------------------------------------------------------
# DynamoDB scan fallback
# ---------------------------------------------------------------------------

def _scan_items_from_db(account_id, desired_base, desired_comercial, desired_occ_set, desired_sizes, limit):
    if not itens_table:
        return []
    desired_base = desired_base if isinstance(desired_base, set) else set()
    desired_comercial = desired_comercial if isinstance(desired_comercial, set) else set()
    desired_occ_set = desired_occ_set if isinstance(desired_occ_set, set) else set()
    desired_sizes = desired_sizes if isinstance(desired_sizes, set) else set()
    limit = max(1, limit)

    projection = (
        "item_id, #st, title, item_title, item_description, item_image_url, image_url, "
        "item_value, item_custom_id, category, item_category, cor, cores, cor_base, cor_comercial, "
        "tamanho, occasion_noiva, occasion_civil, occasion_madrinha, occasion_mae_dos_noivos, "
        "occasion_formatura, occasion_debutante, occasion_gala, occasion_convidada"
    )
    filt = Attr("status").eq("available")
    if account_id:
        filt = filt & Attr("account_id").eq(str(account_id))
    kwargs = {
        "ProjectionExpression": projection,
        "ExpressionAttributeNames": {"#st": "status"},
        "FilterExpression": filt,
    }
    matched = []
    resp = itens_table.scan(**kwargs)
    while True:
        for item in resp.get("Items", []) or []:
            if not (item.get("item_image_url") or item.get("image_url")):
                continue
            if desired_sizes and not _matches_any(_extract_size_list(item), desired_sizes):
                continue
            if desired_occ_set and not (set(_get_occasion_slugs(item)) & desired_occ_set):
                continue
            if desired_base or desired_comercial:
                if not (
                    (desired_base and _matches_any(_db_color_base_list(item), desired_base)) or
                    (desired_comercial and _matches_any(_db_color_commercial_list(item), desired_comercial))
                ):
                    continue
            matched.append(item)
            if len(matched) >= limit:
                return matched
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        resp = itens_table.scan(ExclusiveStartKey=lek, **kwargs)
    return matched

# ---------------------------------------------------------------------------
# Busca principal
# ---------------------------------------------------------------------------

def search_and_prioritize(index, metadata, query, occasions=None, cor_comercial=None, cor_base=None, top_k=None):
    if not index or not metadata or not isinstance(query, str) or not query.strip():
        return []
    ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
    if ntotal <= 0:
        return []
    desired_k = max(1, min(int(top_k or ntotal), ntotal))
    desired_occ_set = set(_normalize_occasion_inputs(occasions))
    desired_comercial = _normalize_set(cor_comercial)
    desired_base = _normalize_set(cor_base)

    print("search_and_prioritize_args", json.dumps({
        "query": str(query or "").replace("\n", " ").strip()[:160],
        "occasions": occasions, "cor_comercial": cor_comercial,
        "cor_base": cor_base, "top_k": top_k,
    }, ensure_ascii=False))

    query_embedding = get_embedding(query.strip())
    query_vector = np.array([query_embedding]).astype('float32')
    distances, indices = index.search(query_vector, desired_k)

    prio_especifica, prio_base, prio_geral = [], [], []
    seen_custom_ids = set()
    batch = []

    def _flush_batch():
        nonlocal batch
        if not batch:
            return
        for item in validate_and_enrich_candidates(batch):
            if desired_occ_set:
                slugs = _get_occasion_slugs(item)
                if slugs:
                    if not (set(slugs) & desired_occ_set):
                        continue
                elif not any(_has_occasion(item, occ) for occ in desired_occ_set):
                    continue
            if desired_comercial and _matches_any(_extract_color_commercial_list(item), desired_comercial):
                prio_especifica.append(item)
            elif desired_base and _matches_any(_extract_color_base_list(item), desired_base):
                prio_base.append(item)
            else:
                prio_geral.append(item)
        batch = []

    for idx in indices[0]:
        if idx == -1:
            continue
        meta = metadata[int(idx)].copy()
        cid = meta.get("custom_id")
        if cid and cid in seen_custom_ids:
            continue
        if cid:
            seen_custom_ids.add(cid)
        batch.append(meta)
        if len(batch) >= 250:
            _flush_batch()
    _flush_batch()

    return prio_especifica + prio_base + prio_geral

# ---------------------------------------------------------------------------
# execute_catalog_search*
# ---------------------------------------------------------------------------

def execute_catalog_search(query, k=5):
    _ensure_resources()
    if not index or not metadata:
        return []
    try:
        rewrite = _rewrite_catalog_query(query, None)
        q_text = (_query_embedding_text_from_rewrite(rewrite, rewrite.get("ocasiao_alvo"))
                  or rewrite.get("query_reescrita") or query)
        query_vector = np.array([get_embedding(q_text)]).astype('float32')
        ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
        if ntotal <= 0:
            return []
        desired = max(1, min(int(k or 0), MAX_SIMILARITY_LIMIT))
        valid_pool, seen_custom_ids, ranked = [], set(), []
        processed_k = 0
        search_k = min(max(SEARCH_BATCH_SIZE, desired * 20), ntotal)
        while processed_k < ntotal:
            distances, indices_arr = index.search(query_vector, search_k)
            raw_candidates = []
            for idx in indices_arr[0][processed_k:search_k]:
                if idx == -1:
                    continue
                meta = metadata[int(idx)].copy()
                cid = meta.get("custom_id")
                if cid and cid not in seen_custom_ids:
                    seen_custom_ids.add(cid)
                    raw_candidates.append(meta)
            processed_k = search_k
            valid_pool.extend(validate_and_enrich_candidates(raw_candidates))
            constrained = _apply_facet_constraints(valid_pool, rewrite)
            ranked = _rerank_by_facets(constrained, rewrite) if constrained else valid_pool
            if len(ranked) >= desired or processed_k >= ntotal:
                break
            search_k = min(ntotal, max(processed_k + SEARCH_BATCH_SIZE, search_k * 2))
        return ranked[:desired]
    except Exception as e:
        print(f"Erro em execute_catalog_search: {e}")
        return []


def execute_catalog_search_raw(query, k=5):
    _ensure_resources()
    if not index or not metadata:
        return []
    try:
        query_vector = np.array([get_embedding(query)]).astype('float32')
        ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
        desired = max(1, min(int(k or 0), ntotal))
        distances, indices_arr = index.search(query_vector, desired)
        raw_candidates = []
        seen = set()
        for idx in indices_arr[0]:
            if idx == -1:
                continue
            meta = metadata[int(idx)].copy()
            cid = meta.get("custom_id")
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            raw_candidates.append(meta)
        return validate_and_enrich_candidates(raw_candidates)[:desired]
    except Exception as e:
        print(f"Erro em execute_catalog_search_raw: {e}")
        return []


def execute_catalog_search_loose(query, k=5, target_occasions=None):
    _ensure_resources()
    if not index or not metadata:
        return []
    try:
        rewrite = _rewrite_catalog_query(query, None)
        q_text = _query_embedding_text_from_rewrite_attrs_only(rewrite) or query
        query_vector = np.array([get_embedding(q_text)]).astype('float32')
        ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
        if ntotal <= 0:
            return []
        desired = max(1, min(int(k or 0), MAX_SIMILARITY_LIMIT))
        target_set = set(target_occasions or [])
        collected, seen_custom_ids = [], set()
        processed_k = 0
        search_k = min(max(SEARCH_BATCH_SIZE, desired * 20), ntotal)
        while processed_k < ntotal and len(collected) < desired:
            distances, indices_arr = index.search(query_vector, search_k)
            raw_candidates = []
            for idx in indices_arr[0][processed_k:search_k]:
                if idx == -1:
                    continue
                meta = metadata[int(idx)].copy()
                cid = meta.get("custom_id")
                if cid and cid not in seen_custom_ids:
                    seen_custom_ids.add(cid)
                    raw_candidates.append(meta)
            processed_k = search_k
            valid_candidates = validate_and_enrich_candidates(raw_candidates)
            if target_set:
                valid_candidates = [
                    c for c in valid_candidates
                    if set(_get_occasion_slugs(c)) & target_set
                ]
            for item in _rerank_by_facets_loose(valid_candidates, rewrite):
                if target_set and not (set(_get_occasion_slugs(item)) & target_set):
                    continue
                collected.append(item)
                if len(collected) >= desired:
                    break
            if processed_k >= ntotal:
                break
            search_k = min(ntotal, max(processed_k + SEARCH_BATCH_SIZE, search_k * 2))
        return collected[:desired]
    except Exception as e:
        print(f"Erro em execute_catalog_search_loose: {e}")
        return []

# ---------------------------------------------------------------------------
# Contexto da loja
# ---------------------------------------------------------------------------

def _store_context_payload():
    path = os.getenv("STORE_CONTEXT_MD_PATH") or "store_context.md"
    default_markdown = (
        "### Informações da London Noivas\n"
        "- **Nome da loja:** London Noivas\n"
        "- **Ramo:** Aluguéis de vestidos\n"
        "- **Endereço:** Rua Voltaire, número 9, Manaus, Amazonas\n"
        "- **Funcionamento:** Segunda a sábado, das 10:00 às 20:00\n"
        "- **Atendimento:** Por agendamento\n"
        "- **Venda de vestidos:** Não, apenas aluguel\n"
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            markdown = f.read().strip()
    except Exception:
        markdown = ""
    return {"markdown": markdown or default_markdown}

# ---------------------------------------------------------------------------
# Ferramentas MCP
# ---------------------------------------------------------------------------

def _mcp_tools():
    account_id = _get_account_id()
    color_pairs = _load_color_pairs_for_account(account_id)
    cor_base_options = list(dict.fromkeys(
        str(c.get("base") or "").strip() for c in color_pairs if str(c.get("base") or "").strip()
    ))
    cor_comercial_options = list(dict.fromkeys(
        str(c.get("name") or "").strip() for c in color_pairs if str(c.get("name") or "").strip()
    ))
    if not cor_base_options:
        cor_base_options = _load_color_options_for_account(account_id)
    if not cor_comercial_options:
        cor_comercial_options = _load_color_options_for_account(account_id)

    cor_base_items = {"type": "string", "enum": cor_base_options} if cor_base_options else {"type": "string"}
    cor_comercial_items = {"type": "string", "enum": cor_comercial_options} if cor_comercial_options else {"type": "string"}

    tools = [
        {
            "name": "buscar_por_similaridade",
            "description": (
                "Busca por similaridade semântica com filtros de ocasião, cor_base/cor_comercial e tamanho. "
                "Use cor_base e cor_comercial para restringir a busca e sizes quando houver preferência. "
                "Se uma cor ou tamanho não estiver claro, deixe o campo vazio e faça follow-up."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pensamento": {"type": "string", "description": "Pensamento do agente sobre o que pretende fazer e por que decidiu chamar esta ferramenta."},
                    "other_characteristics": {"type": "string", "description": "Características livres como tomara que caia, renda, brilho, fenda, cauda sereia, etc."},
                    "occasions": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["Noiva","Civil","Madrinha","Mãe dos Noivos","Formatura","Debutante","Gala","Convidada"]},
                        "description": "Ocasiões suportadas. Se não houver ocasião explícita ou no contexto, deixe vazio."
                    },
                    "cor_base": {
                        "type": "array", "items": cor_base_items,
                        "description": "Cor base disponível no acervo. Use o texto EXATO do enum."
                    },
                    "cor_comercial": {
                        "type": "array", "items": cor_comercial_items,
                        "description": "Cor comercial disponível no acervo. Use o texto EXATO do enum."
                    },
                    "sizes": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 6}
                },
                "required": ["pensamento"]
            }
        },
        {
            "name": "consultar_contexto_loja",
            "description": "Retorna um markdown com contexto oficial da loja para apoiar a resposta. Use quando o cliente perguntar sobre a loja.",
            "inputSchema": {
                "type": "object",
                "properties": {"pensamento": {"type": "string", "description": "Pensamento do agente sobre o que pretende fazer."}},
                "required": ["pensamento"]
            }
        },
        {
            "name": "panorama_cores_ocasioes",
            "description": "Resumo de cores por ocasião com contagem real do catálogo. Use quando não houver resultados de buscar_por_similaridade para sugerir alternativas.",
            "inputSchema": {
                "type": "object",
                "properties": {"pensamento": {"type": "string", "description": "Pensamento do agente sobre o que pretende fazer."}},
                "required": ["pensamento"]
            }
        }
    ]
    print("bella_tool_schema_after_context", json.dumps(tools, ensure_ascii=False))
    print("#####################")
    return tools


def _mcp_to_openai_tools(mcp_tools):
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
        for t in mcp_tools
    ]

# ---------------------------------------------------------------------------
# Extractor de filtros do catálogo
# ---------------------------------------------------------------------------

def _catalog_extractor_tools():
    account_id = _get_account_id()
    try:
        cor_base_options, cor_comercial_options = _color_enums_for_account()
    except Exception:
        fallback = _load_color_options_for_account(account_id) if account_id else []
        cor_base_options = list(dict.fromkeys(str(x).strip() for x in fallback if str(x).strip()))
        cor_comercial_options = list(cor_base_options)

    cor_base_items = {"type": "string", "enum": cor_base_options} if cor_base_options else {"type": "string"}
    cor_comercial_items = {"type": "string", "enum": cor_comercial_options} if cor_comercial_options else {"type": "string"}

    return [{
        "type": "function",
        "function": {
            "name": "extrair_filtros_catalogo",
            "description": "Extrai filtros estruturados (cor_base, cor_comercial, ocasiões) e outros atributos da consulta do catálogo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "other_characteristics": {"type": "string"},
                    "occasions": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["Noiva","Civil","Madrinha","Mãe dos Noivos","Formatura","Debutante","Gala","Convidada"]},
                    },
                    "cor_base": {"type": "array", "items": cor_base_items},
                    "cor_comercial": {"type": "array", "items": cor_comercial_items},
                },
                "required": []
            }
        }
    }]


def _extract_catalog_filters_with_tool(query, target_occasion=None):
    if not isinstance(query, str) or not query.strip():
        return {}
    tool_name = "extrair_filtros_catalogo"
    tools = _catalog_extractor_tools()
    occ_slug = _canonical_occasion(target_occasion) if target_occasion else ""
    system_prompt = (
        "Você extrai filtros estruturados de uma consulta de busca em catálogo de vestidos.\n"
        "Regras:\n- Responda chamando a ferramenta.\n"
        "- Em cor_base e cor_comercial, use SOMENTE valores do enum.\n"
        "- Se a consulta contiver apenas uma cor, preencha a cor e deixe other_characteristics vazio.\n"
        "- Se houver ocasião alvo (aba ativa), não invente novas ocasiões.\n"
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({"consulta_usuario": query, "ocasiao_alvo": occ_slug}, ensure_ascii=False)},
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=0.0,
            max_tokens=220,
        )
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return {}
        call = next((tc for tc in msg.tool_calls if tc.function and tc.function.name == tool_name), None)
        if not call:
            return {}
        args = json.loads(call.function.arguments or "{}")
        if not isinstance(args, dict):
            return {}
        out = {
            "other_characteristics": str(args.get("other_characteristics") or "").strip(),
            "cor_base": [str(x).strip() for x in _ensure_list(args.get("cor_base")) if str(x).strip()],
            "cor_comercial": [str(x).strip() for x in _ensure_list(args.get("cor_comercial")) if str(x).strip()],
            "occasions": [str(x).strip() for x in _ensure_list(args.get("occasions")) if str(x).strip()],
        }
        if _validate_similarity_args(out):
            return {}
        out["cor_base"] = list(dict.fromkeys(out["cor_base"]))
        out["cor_comercial"] = list(dict.fromkeys(out["cor_comercial"]))
        out["occasions"] = list(dict.fromkeys(out["occasions"]))
        return out
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# Busca por filtro direto no metadata
# ---------------------------------------------------------------------------

def _filter_metadata_candidates(cor_base, cor_comercial, occasions, max_candidates):
    if not metadata:
        return []
    desired_base = _normalize_set(cor_base)
    desired_comercial = _normalize_set(cor_comercial)
    desired_set = set(_normalize_occasion_inputs(occasions))
    collected = []
    for meta in metadata:
        if desired_base or desired_comercial:
            if not (
                (desired_base and _matches_any(_extract_color_base_list(meta), desired_base)) or
                (desired_comercial and _matches_any(_extract_color_commercial_list(meta), desired_comercial))
            ):
                continue
        if desired_set and not (set(_meta_occasions(meta)) & desired_set):
            continue
        collected.append(meta.copy())
        if len(collected) >= max_candidates:
            break
    return collected

# ---------------------------------------------------------------------------
# _run_db_search e _run_similarity_search usando _prepare_search_params
# ---------------------------------------------------------------------------

def _no_results_error(params):
    return {
        "type": "no_results",
        "message": "Busca não retornou resultados.",
        "requested": {
            "cor_base": list(params.get("desired_base", set())),
            "cor_comercial": list(params.get("desired_comercial", set())),
            "sizes": list(params.get("desired_sizes", set())),
            "occasions": list(params.get("desired_occ_set", set())),
        },
        "agent_guidance": "Não há itens com esses filtros. Use panorama_cores_ocasioes para sugerir alternativas reais."
    }


def _run_db_search(args):
    params, errors = _prepare_search_params(args)
    if errors:
        return {"items": [], "error": {"type": "validation_error", "message": "Erro de validação.", "details": errors}}

    cor_base = params["cor_base"]
    cor_comercial = params["cor_comercial"]

    # Mapeia cor comercial para base se necessário
    if cor_comercial and not cor_base:
        account_id = _get_account_id()
        color_pairs = _load_color_pairs_for_account(account_id) if account_id else []
        commercial_to_base = {
            _normalize_text(str(p.get("name") or "")): str(p.get("base") or "").strip()
            for p in color_pairs if p.get("name") and p.get("base")
        }
        mapped = list(dict.fromkeys(
            commercial_to_base[_normalize_text(cc)]
            for cc in _ensure_list(cor_comercial)
            if _normalize_text(cc) in commercial_to_base
        ))
        if mapped:
            cor_base = mapped

    candidates = _filter_metadata_candidates(
        cor_base, cor_comercial, params["occasions"],
        max_candidates=max(200, params["limit"] * 40)
    )
    enriched = validate_and_enrich_candidates(candidates)

    commercial, base, general = [], [], []
    seen = set()
    for item in enriched:
        if params["desired_sizes"] and not _matches_any(_extract_size_list(item), params["desired_sizes"]):
            continue
        if params["desired_occ_set"] and not (set(_get_occasion_slugs(item)) & params["desired_occ_set"]):
            continue
        cid = item.get("custom_id")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        comercial_ok = bool(params["desired_comercial"] and _matches_any(_extract_color_commercial_list(item), params["desired_comercial"]))
        base_ok = bool(params["desired_base"] and _matches_any(_extract_color_base_list(item), params["desired_base"]))
        if (params["desired_base"] or params["desired_comercial"]) and not (comercial_ok or base_ok):
            continue
        if comercial_ok:
            commercial.append(item)
        elif base_ok:
            base.append(item)
        else:
            general.append(item)

    filtered = (commercial + base + general)[:params["limit"]]

    if not filtered:
        account_id = _get_account_id()
        fallback = _scan_items_from_db(
            account_id=account_id,
            desired_base=params["desired_base"],
            desired_comercial=params["desired_comercial"],
            desired_occ_set=params["desired_occ_set"],
            desired_sizes=params["desired_sizes"],
            limit=params["limit"],
        )
        if fallback:
            return {"items": fallback, "exact_items": fallback, "no_principal": False}
        return {"items": [], "error": _no_results_error(params)}

    exact_items = commercial[:params["limit"]] if params["desired_comercial"] else list(filtered)
    return {
        "items": filtered,
        "exact_items": exact_items,
        "no_principal": bool(params["desired_comercial"] and filtered and not exact_items),
    }


def _run_similarity_search(args):
    params, errors = _prepare_search_params(args)
    if errors:
        return {"items": [], "error": {"type": "validation_error", "message": "Erro de validação.", "details": errors}}

    unknown_color_terms = params["unknown_color_terms"]
    if unknown_color_terms and not (params["cor_base"] or params["cor_comercial"]):
        return {
            "items": [],
            "error": {
                "type": "no_results",
                "message": "Cor solicitada não está disponível no catálogo no momento.",
                "requested": {
                    "unknown_color_terms": list(unknown_color_terms),
                    "sizes": list(params["desired_sizes"]),
                    "occasions": list(params["desired_occ_set"]),
                    "other_characteristics": params["query"],
                },
                "agent_guidance": "Explique que não há itens nessa cor e use panorama_cores_ocasioes para sugerir alternativas reais."
            }
        }

    print("bella_tool_preprocessed", json.dumps({
        "cor_base": params["cor_base"], "cor_comercial": params["cor_comercial"],
        "sizes": params["sizes"], "occasions": params["occasions"],
        "other_characteristics": params["query"], "limit": params["limit"],
    }, ensure_ascii=False))

    # Sem query textual → fallback para busca por filtro
    if not params["query"]:
        if not any([params["cor_base"], params["cor_comercial"], params["sizes"], params["occasions"]]):
            return {"items": []}
        db_result = _run_db_search(args)
        items = (db_result.get("items") or [])[:params["limit"]]
        result = {"items": items}
        if not items:
            result["error"] = _no_results_error(params)
        print("bella_tool_result", json.dumps({"count": len(items)}, ensure_ascii=False))
        return result

    _ensure_resources()
    ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
    candidate_k = ntotal if (params["desired_base"] or params["desired_comercial"]) else max(params["limit"] * DEFAULT_CANDIDATE_MULTIPLIER, DEFAULT_CANDIDATE_MIN)

    results = search_and_prioritize(
        index=index, metadata=metadata, query=params["query"],
        occasions=params["occasions"], cor_comercial=params["cor_comercial"],
        cor_base=params["cor_base"], top_k=candidate_k,
    )

    rerank_override = {
        "atributos_extraidos": {
            "cor_base": list(params["desired_base"]),
            "cor_comercial": list(params["desired_comercial"]),
            "occasions": list(params["desired_occ_set"]),
            "sizes": list(params["desired_sizes"]),
        }
    }
    results = _rerank_by_facets_loose(results, rerank_override)
    if params["occasions"]:
        results = _filter_items_by_occasions(results, params["occasions"])

    exact_items, fallback_items = [], []
    seen = set()
    for item in results:
        if params["desired_sizes"] and not _matches_any(_extract_size_list(item), params["desired_sizes"]):
            continue
        if params["desired_occ_set"] and not (set(_get_occasion_slugs(item)) & params["desired_occ_set"]):
            continue
        cid = item.get("custom_id")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        comercial_ok = bool(params["desired_comercial"] and _matches_any(_extract_color_commercial_list(item), params["desired_comercial"]))
        base_ok = bool(params["desired_base"] and _matches_any(_extract_color_base_list(item), params["desired_base"]))
        if (params["desired_base"] or params["desired_comercial"]) and not (comercial_ok or base_ok):
            continue
        if params["desired_comercial"]:
            (exact_items if comercial_ok else fallback_items).append(item)
        else:
            exact_items.append(item)
        if len(exact_items) >= params["limit"]:
            break

    if params["desired_comercial"] and len(exact_items) < params["limit"] and fallback_items:
        exact_items.extend(fallback_items[:params["limit"] - len(exact_items)])

    result = {"items": exact_items[:params["limit"]]}
    if not result["items"]:
        err = _no_results_error(params)
        err["agent_guidance"] = "Explique que não encontrou itens com esses filtros e use panorama_cores_ocasioes para sugerir alternativas reais."
        if params["query"]:
            err["requested"]["other_characteristics"] = params["query"]
        result["error"] = err

    print("bella_tool_result", json.dumps({"count": len(result["items"])}, ensure_ascii=False))
    return result

# ---------------------------------------------------------------------------
# Serialização para LLM e cliente
# ---------------------------------------------------------------------------

def _build_suggestion(item):
    image_url = item.get("imageUrl") or item.get("item_image_url") or item.get("image_url") or ""
    if not image_url and item.get("file_name"):
        image_url = url_for("static", filename=f"dresses/{item['file_name']}")
    return {
        "id": item.get("item_id") or item.get("custom_id") or item.get("id"),
        "customId": item.get("customId") or item.get("item_custom_id") or item.get("custom_id"),
        "title": item.get("title", "Vestido Exclusivo"),
        "description": item.get("description", ""),
        "price": item.get("price", "Consulte valor"),
        "color": _extract_color_value(item),
        "color_base": _extract_color_base_value(item),
        "color_comercial": _extract_color_commercial_value(item),
        "size": _extract_size_value(item),
        "category": item.get("category", "Festa"),
        "occasions": _get_occasions_list(item) if isinstance(item, dict) else "",
        "image_url": image_url,
    }


def _summarize_items_for_llm(items):
    error = None
    if isinstance(items, dict):
        error = items.get("error")
        items = items.get("items") or []
    payload = [
        {
            "title": (s := _build_suggestion(item)).get("title") or "",
            "description": (s.get("description") or "")[:240],
            "color_base": s.get("color_base") or "",
            "color_comercial": s.get("color_comercial") or "",
            "size": s.get("size") or "",
            "occasions": s.get("occasions") or [],
            "image_url": s.get("image_url") or "",
        }
        for item in items
    ]
    return {"items": payload, **({"error": error} if error else {})}


def _summarize_items_for_client(items):
    error = None
    if isinstance(items, dict):
        error = items.get("error")
        items = items.get("items") or []
    payload = [
        {
            "id": (s := _build_suggestion(item)).get("id") or "",
            "customId": s.get("customId") or "",
            "title": s.get("title") or "",
            "description": (s.get("description") or "")[:240],
            "price": s.get("price") or "",
            "color": s.get("color") or "",
            "color_base": s.get("color_base") or "",
            "color_comercial": s.get("color_comercial") or "",
            "size": s.get("size") or "",
            "occasions": s.get("occasions") or [],
            "image_url": s.get("image_url") or "",
        }
        for item in items
    ]
    return {"items": payload, **({"error": error} if error else {})}

# ---------------------------------------------------------------------------
# Rota principal: Agente Bella
# ---------------------------------------------------------------------------

@ai_bp.route('/api/ai-search', methods=['POST'])
def ai_search():
    data = request.get_json()
    user_message = data.get('message', '')
    history = data.get('history', [])

    if not user_message:
        return jsonify({"error": "Mensagem vazia."}), 400

    def generate():
        try:
            current_app.logger.info(
                "bella_ai_search_start message_len=%s history_len=%s",
                len(user_message or ""), len(history or []),
            )

            system_prompt = (
                "Você é a Bella, consultora sênior e estilista da London Noivas. "
                "Sua missão é entender o sonho da cliente e encontrar o vestido perfeito disponível na loja de aluguéis de vestidos London Noivas. "
                "PERSONALIDADE:\n"
                "- Empática, sofisticada e proativa. Use emojis com moderação (✨, 👗).\n"
                "- Aja como uma consultora real: não apenas entregue links, mas 'venda' o vestido destacando detalhes que combinam com o pedido.\n"
                "- Faça perguntas de follow-up quando ajudar a refinar a busca ('O que achou do decote?', 'Prefere algo mais armado?').\n"
                "ESTRATÉGIA DE AGENTE:\n"
                "- Você pode e deve chamar ferramentas em sequência, mais de uma vez, até ter segurança para recomendar.\n"
                "- Critério de pronto: antes de encerrar, tente chegar a pelo menos 3 opções viáveis ou explique claramente por que isso não é possível e proponha alternativas.\n"
                "- Quando houver poucos resultados (0–2) ou a cliente estiver indecisa, seja proativa: use `panorama_cores_ocasioes` para propor variações de cor/ocasião e rode nova busca com critérios ajustados.\n"
                "REGRAS DE RESPOSTA:\n"
                "- Responda em markdown.\n"
                "- Mostre até 5 itens retornados pela ferramenta.\n"
                "- Para cada item, escreva: **Título** — descrição breve (1–2 frases). Se houver, inclua cor e tamanho.\n"
                "- Em seguida, exiba a imagem com markdown: ![Título](image_url).\n"
                "- Não mostre IDs nem campos técnicos.\n"
                "- Após listar os vestidos, explique de forma natural quais filtros foram usados na busca.\n"
                "- Se não houver itens, NÃO invente vestidos. Use `panorama_cores_ocasioes` e proponha alternativas reais do catálogo.\n"
                "- SEMPRE finalize com uma pergunta de follow-up.\n"
                "- Quando a cliente mudar a preferência, refaça a busca com esses critérios e traga novas opções.\n"
                "- Traduza adjetivos comuns em restrições do catálogo: 'discreto' → silhueta clássica, decote discreto, sem fenda, cores neutras; 'chamativo' → brilho, fenda, cores vivas; 'romântico' → renda/volume; etc.\n"
                "- Se não houver opções após os filtros, faça nova busca relaxando critérios e informe isso no texto.\n"
                "SEM RESULTADOS (USO DE PANORAMA):\n"
                "- Quando `buscar_por_similaridade` retornar `items` vazio ou `error.type = no_results`, chame `panorama_cores_ocasioes`.\n"
                "- Use a resposta do panorama para sugerir alternativas no seguinte formato:\n"
                "  - Vermelho/Vinho (Bordô) — 7 opções para convidadas e 11 para madrinhas.\n"
                "  - Verde Esmeralda — disponível em 12 opções para gala.\n"
                "- Regras do formato: 3 a 6 linhas, cada linha é uma cor e contagens por ocasião com números reais do panorama.\n"
                "USO DE FERRAMENTAS:\n"
                "- Para buscar vestidos, use `buscar_por_similaridade`.\n"
                "- Para entender disponibilidade por ocasião/cor, use `panorama_cores_ocasioes`.\n"
                "- Para informações sobre a loja, use `consultar_contexto_loja`.\n"
                "- Preencha `occasions` apenas quando houver ocasião explícita ou contexto claro.\n"
                "- Se a ferramenta retornar `error`, explique e refaça a chamada corrigindo os campos.\n"
                "USO DE PENSAMENTO:\n"
                "- Sempre preencha o campo `pensamento` ao chamar qualquer ferramenta.\n"
                "- Não escreva 'Pensamento:' na resposta final ao usuário.\n"
                "- Nunca escreva marcadores como [tool_call: ...] no texto.\n"
                "- Se você disser que vai buscar opções, DEVE chamar `buscar_por_similaridade` e continuar a conversa.\n"
            )

            messages = [{"role": "system", "content": system_prompt}]
            for msg in (history or []):
                if isinstance(msg, dict) and msg.get('role') in ['user', 'assistant'] and msg.get('content'):
                    messages.append({"role": msg['role'], "content": msg['content']})
            messages.append({"role": "user", "content": user_message})

            try:
                tools = _mcp_to_openai_tools(_mcp_tools())
            except Exception as e:
                current_app.logger.error("bella_tool_schema_error error=%s", str(e))
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

            reply_text = ""
            client_payload = None
            pensamentos_emitidos = []
            last_similarity_request = None
            last_tool_error = None
            last_tool_name = None
            forced_tool_name = None

            for _ in range(6):
                tool_choice_param = {"type": "function", "function": {"name": forced_tool_name}} if forced_tool_name else "auto"
                forced_tool_name = None

                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice_param
                )
                response_message = response.choices[0].message

                if not response_message.tool_calls:
                    # Verifica se o modelo prometeu buscar mas não chamou ferramenta
                    content_text = response_message.content or ""
                    lowered = content_text.lower()
                    promised = bool(re.search(r"\b(vou|vamos)\s+(procurar|buscar|pesquisar|verificar|checar|conferir|consultar)\b", lowered))
                    promised_catalog = any(word in lowered for word in ["vestid", "opç", "catálogo", "catalogo"])
                    if promised and promised_catalog:
                        inferred_tool = "buscar_por_similaridade"
                        if "loja" in lowered and re.search(r"\b(consultar|verificar)\b", lowered):
                            inferred_tool = "consultar_contexto_loja"
                        elif re.search(r"\b(cores|ocasi|dispon|quant)\b", lowered) and re.search(r"\b(verificar|checar)\b", lowered):
                            inferred_tool = "panorama_cores_ocasioes"
                        messages.append({
                            "role": "system",
                            "content": "Você prometeu fazer uma busca. Use function calling e chame a ferramenta agora."
                        })
                        forced_tool_name = inferred_tool
                        continue
                    reply_text = content_text
                    if reply_text:
                        reply_text = re.sub(r"^\s*Pensamento:.*(?:\n|$)", "", reply_text, flags=re.IGNORECASE | re.MULTILINE)
                        reply_text = re.sub(r"^\s*\[(?:tool_?call|toolcall)[^\]]*\]\s*(?:\n|$)", "", reply_text, flags=re.IGNORECASE | re.MULTILINE)
                        reply_text = reply_text.strip()
                    break

                messages.append(response_message)

                for tool_call in response_message.tool_calls:
                    tool_call_id = getattr(tool_call, "id", None)
                    tool_name = ""
                    summary = None
                    try:
                        tool_fn = getattr(tool_call, "function", None)
                        tool_name = str(getattr(tool_fn, "name", "") or "").strip()
                        raw_args = getattr(tool_fn, "arguments", None) or "{}"
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})
                    except Exception as e:
                        args = {}
                        summary = json.dumps({"error": {"type": "tool_args_parse_error", "message": str(e)}}, ensure_ascii=False)

                    if summary is None:
                        try:
                            # Extrai e emite pensamento
                            thought = str(args.pop("pensamento", "") or args.pop("thought", "") or "").strip()
                            if not thought:
                                thought = {
                                    "buscar_por_similaridade": "Vou buscar no catálogo opções que combinem com o seu pedido.",
                                    "panorama_cores_ocasioes": "Vou verificar quais cores e ocasiões têm mais opções disponíveis.",
                                    "consultar_contexto_loja": "Vou consultar as informações oficiais da loja.",
                                }.get(tool_name, "Vou usar uma ferramenta para te ajudar com isso.")

                                if tool_name == "panorama_cores_ocasioes" and isinstance(last_tool_error, dict) and last_tool_error.get("type") == "no_results" and last_tool_name == "buscar_por_similaridade":
                                    requested = last_similarity_request or {}
                                    parts = []
                                    occ = requested.get("occasions") or []
                                    cores = [*(requested.get("cor_base") or []), *(requested.get("cor_comercial") or [])]
                                    if occ:
                                        parts.append(" / ".join(str(x) for x in occ if str(x).strip()))
                                    if cores:
                                        parts.append("cor " + " / ".join(str(x) for x in cores if str(x).strip()))
                                    contexto = (" (" + ", ".join(parts) + ")") if parts else ""
                                    thought = f"Não encontrei resultados na busca anterior{contexto}; vou usar o panorama para sugerir alternativas reais."

                                if tool_name == "buscar_por_similaridade" and last_tool_name == "panorama_cores_ocasioes":
                                    thought = "Com base no panorama, vou fazer uma nova busca ajustando os filtros."

                            yield f"data: {json.dumps({'type': 'thinking', 'content': thought})}\n\n"
                            pensamentos_emitidos.append(thought)

                            current_app.logger.info("bella_tool_call tool=%s args=%s", tool_name, json.dumps(args, ensure_ascii=False))

                            if tool_name == "buscar_por_similaridade":
                                tool_results = _run_similarity_search(args)
                                summary_payload = _summarize_items_for_llm(tool_results)
                                client_payload = _summarize_items_for_client(tool_results)
                                last_tool_name = tool_name
                                last_tool_error = tool_results.get("error") if isinstance(tool_results, dict) else None
                                last_similarity_request = {
                                    "cor_base": args.get("cor_base") or [],
                                    "cor_comercial": args.get("cor_comercial") or [],
                                    "sizes": args.get("sizes") or [],
                                    "occasions": args.get("occasions") or [],
                                    "other_characteristics": args.get("other_characteristics") or "",
                                } if isinstance(args, dict) else None
                                summary = json.dumps(summary_payload, ensure_ascii=False)
                            elif tool_name == "panorama_cores_ocasioes":
                                last_tool_name = tool_name
                                last_tool_error = None
                                account_id = _get_account_id()
                                try:
                                    panorama_payload = _build_color_occasion_panorama_from_db(account_id) if itens_table else None
                                except Exception:
                                    panorama_payload = None
                                if panorama_payload is None:
                                    _ensure_resources()
                                    panorama_payload = _build_color_occasion_panorama(metadata)
                                summary = json.dumps(panorama_payload, ensure_ascii=False)
                            elif tool_name == "consultar_contexto_loja":
                                summary = json.dumps(_store_context_payload(), ensure_ascii=False)
                            else:
                                summary = json.dumps({"error": {"type": "tool_not_found"}}, ensure_ascii=False)

                            current_app.logger.info("bella_tool_output tool=%s results=%s", tool_name,
                                len(json.loads(summary).get("items", [])) if summary else 0)

                        except Exception as e:
                            current_app.logger.exception("bella_tool_execution_error tool=%s", tool_name)
                            summary = json.dumps({"error": {"type": "tool_execution_error", "message": str(e)}}, ensure_ascii=False)

                    if tool_call_id:
                        messages.append({
                            "tool_call_id": tool_call_id,
                            "role": "tool",
                            "name": tool_name or "unknown_tool",
                            "content": summary or json.dumps({"error": {"type": "tool_execution_error"}})
                        })

            if not reply_text:
                final_response = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
                reply_text = final_response.choices[0].message.content

            response_payload = {"reply": reply_text}
            if pensamentos_emitidos:
                response_payload["pensamentos"] = pensamentos_emitidos
            if client_payload:
                response_payload["items"] = client_payload.get("items") or []
                error_payload = client_payload.get("error")
                if isinstance(error_payload, dict) and error_payload.get("type") == "no_results":
                    error_payload = None
                if error_payload:
                    response_payload["error"] = error_payload

            yield f"data: {json.dumps(response_payload)}\n\n"

        except Exception as e:
            current_app.logger.exception("Erro no Agente Bella")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# ---------------------------------------------------------------------------
# Rota: similares
# ---------------------------------------------------------------------------

@ai_bp.route('/api/ai-similar/<item_id>', methods=['GET'])
def ai_similar(item_id):
    try:
        no_faiss = _flag_is_set(request.args.get("no_faiss") or request.args.get("noFaiss") or request.args.get("no_faiss_search"))
        limit = max(1, min(int(request.args.get("limit") or 4), 24))

        req_occ = request.args.getlist("occasion")
        if not req_occ:
            occ_raw = request.args.get("occasions") or request.args.get("occasion")
            if isinstance(occ_raw, str) and occ_raw.strip():
                req_occ = [x.strip() for x in occ_raw.split(",") if x.strip()]
        target_occ = _normalize_occasion_inputs(req_occ)
        target_set = set(target_occ)

        def _extract_list_from_args(keys):
            for key in keys:
                values = request.args.getlist(key)
                if values:
                    cleaned = [str(x).strip() for x in values if isinstance(x, str) and x.strip()]
                    if cleaned:
                        return cleaned
                raw = request.args.get(key)
                if isinstance(raw, str) and raw.strip():
                    return [x.strip() for x in raw.split(",") if x.strip()]
            return []

        def _build_suggestions(items):
            return [
                {
                    "id": item.get("item_id") or item.get("custom_id"),
                    "customId": item.get("customId"),
                    "title": item.get("title", "Vestido"),
                    "image_url": item.get("imageUrl") or url_for("static", filename=f"dresses/{item['file_name']}"),
                    "description": item.get("description", ""),
                    "price": (
                        item.get("price")
                        or (f"R$ {item.get('item_value')}" if item.get("item_value") else "")
                        or (f"R$ {item.get('itemValue')}" if item.get("itemValue") else "")
                    ),
                    "item_obs": item.get("item_obs") or item.get("itemObs") or "",
                    "category": item.get("category", "Outros"),
                    "color": _extract_color_value(item),
                    "color_base": _extract_color_base_value(item),
                    "color_comercial": _extract_color_commercial_value(item),
                    "size": _extract_size_value(item),
                    "occasions": _get_occasions_list(item),
                }
                for item in items
            ]

        if no_faiss:
            cor_base = _extract_list_from_args(["cor_base", "corBase", "color_base", "colorBase"])
            cor_comercial = _extract_list_from_args(["cor_comercial", "corComercial", "color_comercial", "colorComercial"])
            db_result = _run_db_search({
                "occasions": target_occ, "cor_base": cor_base,
                "cor_comercial": cor_comercial, "limit": min(limit + 1, 5),
            })
            items = db_result.get("items") or []
            filtered = [
                item for item in items
                if str(item.get("item_id") or item.get("custom_id") or "") != str(item_id)
            ][:limit]
            return jsonify({"suggestions": _build_suggestions(filtered)})

        _ensure_resources()
        if not index or not metadata:
            return jsonify({"error": "Sistema indisponível"}), 503

        query_hint = request.args.get("q") or request.args.get("query")

        target_idx = next(
            (i for i, item in enumerate(metadata) if str(item.get('custom_id')) == str(item_id)), -1
        )

        target_flags = None
        try:
            if itens_table:
                resp = itens_table.get_item(Key={"item_id": str(item_id)})
                target_flags = resp.get("Item")
        except Exception:
            pass

        target_color_base_raw = (
            _extract_color_base_value(target_flags) if isinstance(target_flags, dict) else ""
        ) or (
            _extract_color_base_value(metadata[target_idx]) if target_idx != -1 else ""
        )
        target_color_base = _normalize_text(target_color_base_raw) if target_color_base_raw else ""

        if isinstance(query_hint, str) and query_hint.strip():
            results = execute_catalog_search_loose(query_hint, k=max(limit * 4, 20), target_occasions=target_occ)
            collected, seen_ids, secondary, secondary_ids = [], set(), [], set()
            for item in results:
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid == str(item_id) or sid in seen_ids:
                    continue
                if target_color_base and _normalize_text(_extract_color_base_value(item) or "") != target_color_base:
                    if sid not in secondary_ids:
                        secondary.append(item)
                        secondary_ids.add(sid)
                    continue
                seen_ids.add(sid)
                collected.append(item)
                if len(collected) >= limit:
                    break
            if target_color_base and len(collected) < limit:
                for item in secondary:
                    sid = str(item.get("item_id") or item.get("custom_id") or "")
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    collected.append(item)
                    if len(collected) >= limit:
                        break
            return jsonify({"suggestions": _build_suggestions(collected)})

        if target_idx == -1:
            return jsonify({"error": "Item não encontrado no índice"}), 404

        query_vector = index.reconstruct(target_idx).reshape(1, -1)

        if not target_occ:
            target_occ = (
                _get_occasion_slugs(target_flags) if isinstance(target_flags, dict) else []
            ) or (
                [o for o in (_canonical_occasion(x) for x in ((metadata[target_idx].get("metadata_filters") or {}).get("occasions") or [])) if o]
            )
            target_set = set(target_occ)

        ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
        if ntotal <= 1:
            return jsonify({"suggestions": []})

        collected, seen_ids, secondary, secondary_ids = [], set(), [], set()
        processed_k = 0
        k = min(max(50, limit * 20), ntotal)

        while len(collected) < limit and processed_k < ntotal:
            distances, indices_arr = index.search(query_vector, k)
            raw_candidates = [
                metadata[int(idx)].copy()
                for idx in indices_arr[0][processed_k:k]
                if idx != -1 and idx != target_idx
            ]
            processed_k = k

            for item in validate_and_enrich_candidates(raw_candidates):
                if target_set:
                    occ_slugs = _get_occasion_slugs(item)
                    if not occ_slugs or not (set(occ_slugs) & target_set):
                        continue
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid in seen_ids:
                    continue
                if target_color_base and _normalize_text(_extract_color_base_value(item) or "") != target_color_base:
                    if sid not in secondary_ids:
                        secondary.append(item)
                        secondary_ids.add(sid)
                    continue
                seen_ids.add(sid)
                collected.append(item)
                if len(collected) >= limit:
                    break

            if len(collected) >= limit or processed_k >= ntotal:
                break
            k = min(ntotal, max(processed_k + 50, k * 2))

        if target_color_base and len(collected) < limit:
            for item in secondary:
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                collected.append(item)
                if len(collected) >= limit:
                    break

        return jsonify({"suggestions": _build_suggestions(collected)})

    except Exception as e:
        print(f"Erro ao buscar similares: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Rota: busca no catálogo
# ---------------------------------------------------------------------------

@ai_bp.route('/api/ai-catalog-search', methods=['POST'])
def ai_catalog_search():
    _ensure_resources()
    if not index or not metadata:
        return jsonify({"error": "Sistema indisponível"}), 503

    data = request.get_json()
    query = data.get('query', '')
    page = int(data.get('page', 1))
    limit = max(8, int(data.get('limit', 8)))

    if not query:
        return jsonify({"error": "Query vazia"}), 400

    try:
        target_occasion = (data.get("occasion") or data.get("category") or "").lower().strip()
        cor_base, cor_comercial = [], []
        query_for_faiss = query
        search_occasions = target_occasion or None

        account_id = _get_account_id()
        try:
            base_options, commercial_options = _color_enums_for_account()
        except Exception:
            fallback = _load_color_options_for_account(account_id) if account_id else []
            base_options = list(dict.fromkeys(str(x).strip() for x in fallback if str(x).strip()))
            commercial_options = list(base_options)

        base_norm = {_normalize_text(v): v for v in base_options if str(v).strip()}
        commercial_norm = {_normalize_text(v): v for v in commercial_options if str(v).strip()}

        query_tokens = [t for t in str(query or "").replace("\n", " ").split() if t.strip()]
        joined_norm = _normalize_text(" ".join(query_tokens[:2]))
        if joined_norm in commercial_norm:
            cor_comercial = [commercial_norm[joined_norm]]
        elif joined_norm in base_norm:
            cor_base = [base_norm[joined_norm]]
        else:
            extracted = _extract_catalog_filters_with_tool(query, target_occasion)
            if extracted:
                cor_base = [str(x).strip() for x in _ensure_list(extracted.get("cor_base")) if str(x).strip()]
                cor_comercial = [str(x).strip() for x in _ensure_list(extracted.get("cor_comercial")) if str(x).strip()]
                other_characteristics = str(extracted.get("other_characteristics") or "").strip()
                if other_characteristics:
                    query_for_faiss = other_characteristics
                if not search_occasions:
                    search_occasions = extracted.get("occasions") or []

        if cor_comercial and not cor_base:
            color_pairs = _load_color_pairs_for_account(account_id) if account_id else []
            commercial_to_base = {
                _normalize_text(str(p.get("name") or "")): str(p.get("base") or "").strip()
                for p in color_pairs if p.get("name") and p.get("base")
            }
            mapped = commercial_to_base.get(_normalize_text(cor_comercial[0]))
            if mapped and _normalize_text(mapped) in base_norm:
                cor_base = [base_norm[_normalize_text(mapped)]]

        coerced = _coerce_similarity_color_fields({"cor_base": cor_base, "cor_comercial": cor_comercial})
        cor_base = coerced.get("cor_base") or []
        cor_comercial = coerced.get("cor_comercial") or []

        ntotal = int(getattr(index, "ntotal", 0) or len(metadata))
        if ntotal <= 0:
            return jsonify({"results": [], "page": 1, "total_pages": 0})

        valid_results = search_and_prioritize(
            index=index, metadata=metadata, query=query_for_faiss,
            occasions=search_occasions, cor_comercial=cor_comercial or None,
            cor_base=cor_base or None, top_k=ntotal,
        )

        total_valid = len(valid_results)
        total_pages = (total_valid + limit - 1) // limit
        page = max(1, min(page, total_pages or 1))
        start = (page - 1) * limit

        results = [
            {
                "item_id": item.get('item_id') or item.get('custom_id'),
                "title": item.get('title', 'Vestido'),
                "imageUrl": item.get('imageUrl') or url_for('static', filename=f"dresses/{item['file_name']}"),
                "description": item.get('description', ''),
                "price": item.get('price', "Valor do aluguel: Consulte"),
                "customId": item.get('customId'),
                "category": item.get('category', 'Outros'),
                "color": _extract_color_value(item),
                "color_base": _extract_color_base_value(item),
                "color_comercial": _extract_color_commercial_value(item),
                "size": _extract_size_value(item),
                "occasions": _get_occasions_list(item),
            }
            for item in valid_results[start:start + limit]
        ]

        return jsonify({"results": results, "page": page, "total_pages": total_pages, "total_results": total_valid})

    except Exception as e:
        print(f"Erro na busca do catálogo: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Rotas admin
# ---------------------------------------------------------------------------

@ai_bp.route('/api/admin/ai-status', methods=['GET'])
def admin_ai_status():
    try:
        return jsonify(get_index_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@ai_bp.route('/api/admin/sync-progress', methods=['GET'])
def admin_sync_progress():
    try:
        return jsonify(get_progress())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@ai_bp.route('/api/admin/sync-ai-index', methods=['POST'])
def admin_sync_ai_index():
    try:
        data = request.get_json(silent=True) or {}
        def _run():
            res = service_sync_index(
                reset_local=bool(data.get("reset_local")),
                force_regenerate=bool(data.get("force_regenerate"))
            )
            if res.get('status') == 'success':
                load_resources()
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@ai_bp.route('/api/admin/create-occasion-gsis', methods=['POST'])
def create_occasion_gsis():
    try:
        if not session.get("logged_in"):
            return jsonify({"error": "not_authorized"}), 403
        data = request.get_json(silent=True) or {}
        table_name = data.get("table_name") or "alugueqqc_itens"
        slugs = data.get("slugs") or ["madrinha","formatura","gala","debutante","convidada","mae_dos_noivos","noiva","civil"]
        ddb_client = dynamodb.meta.client
        desc = ddb_client.describe_table(TableName=table_name)
        table = desc.get("Table", {})
        existing = set((idx.get("IndexName") or "") for idx in (table.get("GlobalSecondaryIndexes") or []))
        billing = (table.get("BillingModeSummary") or {}).get("BillingMode") or "PROVISIONED"
        to_create = []
        attr_defs = {"account_id": "S"}
        for slug in slugs:
            idx_name = f"occasion_{slug}-index"
            if idx_name in existing:
                continue
            attr_defs[f"occasion_{slug}"] = "S"
            create_def = {
                "Create": {
                    "IndexName": idx_name,
                    "KeySchema": [
                        {"AttributeName": f"occasion_{slug}", "KeyType": "HASH"},
                        {"AttributeName": "account_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            }
            if billing != "PAY_PER_REQUEST":
                create_def["Create"]["ProvisionedThroughput"] = {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}
            to_create.append(create_def)
        if not to_create:
            return jsonify({"status": "noop", "message": "GSIs já existem"})
        ddb_client.update_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": v} for k, v in attr_defs.items()],
            GlobalSecondaryIndexUpdates=to_create,
        )
        return jsonify({"status": "started", "created": [d["Create"]["IndexName"] for d in to_create]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


load_resources()
