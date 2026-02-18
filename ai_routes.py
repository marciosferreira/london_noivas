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
from flask import Blueprint, request, jsonify, url_for, current_app, session
from dotenv import load_dotenv
from ai_sync_service import get_index_status, sync_index as service_sync_index, get_progress

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

# Inicializa DynamoDB (Para validação de consistência)
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

DEFAULT_PUBLIC_ACCOUNT_ID = "37d5b37f-c920-4090-a682-7e1ed2e31a0f"

def _pick_public_account_id():
    raw = os.getenv("AI_SYNC_ACCOUNT_ID") or os.getenv("PUBLIC_CATALOG_ACCOUNT_IDS") or ""
    raw = str(raw or "").strip()
    if not raw:
        return DEFAULT_PUBLIC_ACCOUNT_ID
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise RuntimeError("Conta pública inválida. Configure AI_SYNC_ACCOUNT_ID ou PUBLIC_CATALOG_ACCOUNT_IDS.")
    return parts[0]

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
    account_id = session.get("account_id") if session else None
    if not account_id:
        account_id = _pick_public_account_id()
    color_pairs = _load_color_pairs_for_account(account_id)
    cor_base_options = [str(c.get("base") or "").strip() for c in color_pairs if str(c.get("base") or "").strip()]
    cor_comercial_options = [str(c.get("name") or "").strip() for c in color_pairs if str(c.get("name") or "").strip()]
    cor_base_options = list(dict.fromkeys(cor_base_options))
    cor_comercial_options = list(dict.fromkeys(cor_comercial_options))
    if not cor_base_options:
        raise RuntimeError(f"cor_base vazio para {account_id}.")
    if not cor_comercial_options:
        raise RuntimeError(f"cor_comercial vazio para {account_id}.")
    return cor_base_options, cor_comercial_options

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

    base_by_norm = {_normalize_text(v): v for v in (cor_base_options or []) if str(v).strip()}
    comercial_by_norm = {_normalize_text(v): v for v in (cor_comercial_options or []) if str(v).strip()}

    moved_to_comercial = []
    kept_base = []
    for v in cor_base:
        if not isinstance(v, str) or not v.strip():
            continue
        vn = _normalize_text(v)
        if vn in base_by_norm:
            kept_base.append(base_by_norm[vn])
            continue
        if vn in comercial_by_norm:
            moved_to_comercial.append(comercial_by_norm[vn])
            continue
        kept_base.append(v.strip())

    moved_to_base = []
    kept_comercial = []
    for v in cor_comercial:
        if not isinstance(v, str) or not v.strip():
            continue
        vn = _normalize_text(v)
        if vn in comercial_by_norm:
            kept_comercial.append(comercial_by_norm[vn])
            continue
        if vn in base_by_norm:
            moved_to_base.append(base_by_norm[vn])
            continue
        kept_comercial.append(v.strip())

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
    cor_base_options, cor_comercial_options = _color_enums_for_account()
    if cor_base_options:
        invalid = [v for v in cor_base if v not in cor_base_options]
        if invalid:
            errors.append({
                "field": "cor_base",
                "message": "Valores inválidos",
                "invalid": invalid,
                "allowed": cor_base_options,
            })
    if cor_comercial_options:
        invalid = [v for v in cor_comercial if v not in cor_comercial_options]
        if invalid:
            errors.append({
                "field": "cor_comercial",
                "message": "Valores inválidos",
                "invalid": invalid,
                "allowed": cor_comercial_options,
            })
    allowed_occasions = ["Noiva", "Civil", "Madrinha", "Mãe dos Noivos", "Formatura", "Debutante", "Gala", "Convidada"]
    invalid_occ = [v for v in occasions if v not in allowed_occasions]
    if invalid_occ:
        errors.append({
            "field": "occasions",
            "message": "Valores inválidos",
            "invalid": invalid_occ,
            "allowed": allowed_occasions,
        })
    return errors

def _expand_color_terms(colors):
    raw = _ensure_list(colors)
    expanded = []
    available = []
    account_id = session.get("account_id") if session else None
    if not account_id:
        account_id = _pick_public_account_id()
    if account_id:
        available = _load_color_options_for_account(account_id)
    for c in raw:
        norm = _normalize_text(c)
        expanded.append(c)
        if available:
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

def _extract_color_value(obj):
    vals = _extract_color_list(obj)
    return vals[0].strip() if vals else ""

def _extract_color_base_value(obj):
    v = obj.get("cor_base") or obj.get("color_base")
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x.strip()
        return ""
    if isinstance(v, str):
        return v.strip()
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        v = mf.get("cor_base")
        if isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, str) and x.strip():
                    return x.strip()
            return ""
        if isinstance(v, str):
            return v.strip()
    return ""

def _extract_color_commercial_value(obj):
    v = obj.get("cor_comercial") or obj.get("color_comercial")
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x.strip()
        return ""
    if isinstance(v, str):
        return v.strip()
    mf = obj.get("metadata_filters")
    if isinstance(mf, dict):
        v = mf.get("cor_comercial")
        if isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, str) and x.strip():
                    return x.strip()
            return ""
        if isinstance(v, str):
            return v.strip()
    return ""

def _extract_size_list(obj):
    v = obj.get("tamanho") or obj.get("size") or obj.get("item_tamanho") or obj.get("item_size") or obj.get("sizes")
    if isinstance(v, (list, tuple)):
        return [x for x in v if str(x).strip()]
    if isinstance(v, str):
        return [v] if v.strip() else []
    return []

def _extract_size_value(obj):
    vals = _extract_size_list(obj)
    return vals[0].strip() if vals else ""

def _matches_any(value, desired_set):
    if not desired_set:
        return True
    actual = _normalize_set(value)
    return bool(actual & desired_set)

def _meta_occasions(meta):
    mf = meta.get("metadata_filters") or {}
    occ = mf.get("occasions")
    if isinstance(occ, list):
        return _normalize_occasion_inputs(occ)
    return []

def _build_suggestion(item):
    image_url = item.get("imageUrl") or item.get("item_image_url") or item.get("image_url") or ""
    if not image_url:
        image_url = url_for("static", filename=f"dresses/{item['file_name']}") if item.get("file_name") else ""
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

def _slugify(text):
    text = _normalize_text(text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def _canonical_occasion(value):
    v = _slugify(value)
    if not v:
        return ""
    if v in ["black-tie", "blacktie", "gala"]:
        return "gala"
    if v in ["mae-dos-noivos", "mae-dos-noivas", "mae-do-noivo", "mae-da-noiva"]:
        return "mae-dos-noivos"
    return v

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
    for o in occ:
        if _canonical_occasion(o) == target:
            return True
    return False

def _flag_is_set(value):
    return value == "1" or value == 1 or value is True

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

def _split_multi_text(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = []
        for x in value:
            if isinstance(x, str) and x.strip():
                parts.append(x.strip())
        return parts
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[,\|/;]+", text)
    out = []
    for p in parts:
        q = str(p or "").strip()
        if q:
            out.append(q)
    return out

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

def _scan_items_from_db(account_id, desired_base, desired_comercial, desired_occ_set, desired_sizes, limit):
    if not itens_table:
        return []
    if not isinstance(desired_base, set):
        desired_base = set()
    if not isinstance(desired_comercial, set):
        desired_comercial = set()
    if not isinstance(desired_occ_set, set):
        desired_occ_set = set()
    if not isinstance(desired_sizes, set):
        desired_sizes = set()
    if limit < 1:
        limit = 1

    projection = (
        "item_id, #st, title, item_title, item_description, item_image_url, image_url, "
        "item_value, item_custom_id, category, item_category, cor, cores, cor_base, cor_comercial, "
        "tamanho, occasion_noiva, occasion_civil, occasion_madrinha, occasion_mae_dos_noivos, "
        "occasion_formatura, occasion_debutante, occasion_gala, occasion_convidada"
    )
    kwargs = {
        "ProjectionExpression": projection,
        "ExpressionAttributeNames": {"#st": "status"},
    }
    filt = Attr("status").eq("available")
    if account_id:
        filt = filt & Attr("account_id").eq(str(account_id))
    kwargs["FilterExpression"] = filt

    matched = []
    resp = itens_table.scan(**kwargs)
    while True:
        for item in resp.get("Items", []) or []:
            image_url = item.get("item_image_url") or item.get("image_url")
            if not image_url:
                continue
            if desired_sizes and not _matches_any(_extract_size_list(item), desired_sizes):
                continue
            if desired_occ_set and not (set(_get_occasion_slugs(item)) & desired_occ_set):
                continue
            if desired_base or desired_comercial:
                base_ok = bool(desired_base and _matches_any(_db_color_base_list(item), desired_base))
                comercial_ok = bool(desired_comercial and _matches_any(_db_color_commercial_list(item), desired_comercial))
                if not (base_ok or comercial_ok):
                    continue
            matched.append(item)
            if len(matched) >= limit:
                return matched
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        resp = itens_table.scan(ExclusiveStartKey=lek, **kwargs)
    return matched

def _get_occasions_list(item):
    occasions = []
    if _flag_is_set(item.get('occasion_noiva')): occasions.append('Noiva')
    if _flag_is_set(item.get('occasion_civil')): occasions.append('Civil')
    if _flag_is_set(item.get('occasion_madrinha')): occasions.append('Madrinha')
    if _flag_is_set(item.get('occasion_mae_dos_noivos')): occasions.append('Mãe dos Noivos')
    if _flag_is_set(item.get('occasion_formatura')): occasions.append('Formatura')
    if _flag_is_set(item.get('occasion_debutante')): occasions.append('Debutante')
    if _flag_is_set(item.get('occasion_gala')): occasions.append('Gala')
    if _flag_is_set(item.get('occasion_convidada')): occasions.append('Convidada')
    return ", ".join(occasions)

def _get_occasion_slugs(item):
    if not isinstance(item, dict):
        return []
    mapping = [
        ("occasion_noiva", "noiva"),
        ("occasion_civil", "civil"),
        ("occasion_madrinha", "madrinha"),
        ("occasion_mae_dos_noivos", "mae-dos-noivos"),
        ("occasion_formatura", "formatura"),
        ("occasion_debutante", "debutante"),
        ("occasion_gala", "gala"),
        ("occasion_convidada", "convidada"),
    ]
    out = []
    seen = set()
    for key, slug in mapping:
        if not _flag_is_set(item.get(key)):
            continue
        canon = _canonical_occasion(slug)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out

def _normalize_occasion_inputs(value):
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = [value]
    else:
        return []
    out = []
    seen = set()
    for v in raw:
        canon = _canonical_occasion(v)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out

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
        matched = False
        for occ in target_set:
            if _has_occasion(item, occ):
                matched = True
                break
        if matched:
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

def _category_slug(meta):
    if not isinstance(meta, dict):
        return "festa"
    mf = meta.get("metadata_filters")
    if isinstance(mf, dict):
        occ = mf.get("occasions")
        if isinstance(occ, list) and occ:
            for o in occ:
                oo = _normalize_text(o)
                if "noiv" in oo or "civil" in oo:
                    return "noiva"
            return "festa"
    raw = meta.get("category_slug") or meta.get("item_category") or meta.get("category") or meta.get("categoria")
    raw = str(raw or "").lower().strip()
    if "noiv" in raw or "civil" in raw:
        return "noiva"
    return "festa"

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
        mf = m.get("metadata_filters")
        if not isinstance(mf, dict):
            mf = {}

        def bump(facet, values):
            if not values:
                return
            target = counts.get(facet)
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
        bump("ocasioes", mf.get("occasions"))

    digest = {}
    for facet, seed_values in seed.items():
        facet_counts = counts.get(facet, {})
        observed = _top_values(facet_counts, limit=12)
        combined = list(dict.fromkeys(seed_values + observed))
        digest[facet] = combined[:16]

    return digest

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
        occasions = _meta_occasions(item)
        if not occasions:
            occasions = ["Sem ocasião"]
        total += 1
        for occ in occasions:
            occ_key = str(occ).strip() or "Sem ocasião"
            bucket = counts.setdefault(occ_key, {})
            key = (cor_base or "", cor_comercial or "")
            bucket[key] = bucket.get(key, 0) + 1
    payload = []
    for occ in sorted(counts.keys()):
        entries = []
        for (base, comercial), qty in counts[occ].items():
            entries.append({
                "cor_base": base,
                "cor_comercial": comercial,
                "count": qty
            })
        entries = sorted(entries, key=lambda x: (-x["count"], x["cor_base"], x["cor_comercial"]))
        payload.append({
            "occasion": occ,
            "colors": entries
        })
    return {
        "total_items": total,
        "occasions": payload
    }

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
    }
    filt = Attr("status").eq("available")
    if account_id:
        filt = filt & Attr("account_id").eq(str(account_id))
    kwargs["FilterExpression"] = filt

    resp = itens_table.scan(**kwargs)
    while True:
        for item in resp.get("Items", []) or []:
            image_url = item.get("item_image_url") or item.get("image_url")
            if not image_url:
                continue
            base_list = _db_color_base_list(item)
            comercial_list = _db_color_commercial_list(item)
            cor_base = base_list[0].strip() if base_list else ""
            cor_comercial = comercial_list[0].strip() if comercial_list else ""
            if not cor_base and not cor_comercial:
                continue
            slugs = _get_occasion_slugs(item)
            if slugs:
                occ_labels = [_OCCASION_LABEL_BY_SLUG.get(s, s) for s in slugs]
            else:
                occ_labels = ["Sem ocasião"]
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
        entries = []
        for (base, comercial), qty in counts[occ].items():
            entries.append({"cor_base": base, "cor_comercial": comercial, "count": qty})
        entries = sorted(entries, key=lambda x: (-x["count"], x["cor_base"], x["cor_comercial"]))
        payload.append({"occasion": occ, "colors": entries})

    return {"total_items": total, "occasions": payload}

def _extract_json_object(text):
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]

def _get_attr_from_rewrite(attrs, canonical_key, key_map):
    if not isinstance(attrs, dict):
        return None
    if canonical_key in attrs:
        return attrs.get(canonical_key)
    for kk, vv in key_map.items():
        if vv == canonical_key and kk in attrs:
            return attrs.get(kk)
    return None

def _rewrite_catalog_query(query, target_occasion):
    if not isinstance(query, str):
        return {"query_reescrita": ""}
    query = query.strip()
    if not query:
        return {"query_reescrita": ""}

    target_slug = _canonical_occasion(target_occasion)
    cache_key = f"{target_slug or 'all'}|{_normalize_text(query)}"
    now = time.time()
    cached = _query_rewrite_cache.get(cache_key)
    if cached and (now - cached.get("ts", 0)) <= _QUERY_REWRITE_CACHE_TTL_SECONDS:
        return cached.get("data") or {"query_reescrita": query}

    digest_for_prompt = {}
    if isinstance(inventory_digest, dict):
        for facet, values in inventory_digest.items():
            if not isinstance(values, list):
                continue
            digest_for_prompt.setdefault(facet, [])
            digest_for_prompt[facet].extend([str(v) for v in values if str(v).strip()])
    account_id = session.get("account_id") if session else None
    if not account_id:
        account_id = _pick_public_account_id()
    available_colors = _load_color_options_for_account(account_id) if account_id else []
    if available_colors:
        digest_for_prompt.setdefault("cores", [])
        digest_for_prompt["cores"].extend(available_colors)
    for facet, values in list(digest_for_prompt.items()):
        digest_for_prompt[facet] = list(dict.fromkeys(values))[:32]

    system_prompt = (
        "Você reescreve consultas para busca semântica em um catálogo de vestidos.\n"
        "Objetivo: transformar a mensagem do usuário em uma consulta curta, explícita e útil para embeddings.\n"
        "O contexto do inventário é apenas exemplos observados; não se restrinja a ele.\n"
        "Se existir uma ocasião alvo (aba ativa), use-a como pista para priorizar termos e desambiguar, mas não invente restrições que o usuário não pediu.\n"
        "Se o usuário pedir um atributo/valor que não aparece nos exemplos, mantenha mesmo assim e liste em atributos_novos.\n"
        "Não invente que o inventário possui algo; apenas descreva preferências do usuário.\n"
        "Trate negativas: quando houver 'sem X' ou 'não X', registre X em termos_excluir e também como 'não X' no facet apropriado em atributos_extraidos (ex.: 'não tomara que caia' em decote; 'sem fenda' em detalhes).\n"
        "Extraia cores em dois campos: cor_base (cores genéricas) e cor_comercial (nomes comerciais). Se houver só uma cor sem qualificador, use cor_base. Não use o campo 'cor' ou 'colors'.\n"
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
            obj = _extract_json_object(content)
            data = json.loads(obj) if obj else {}

        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("query_reescrita"), str) or not data.get("query_reescrita").strip():
            data["query_reescrita"] = query

        data["ocasiao_alvo"] = target_slug
        _query_rewrite_cache[cache_key] = {"ts": now, "data": data}
        return data
    except Exception as e:
        print(f"Erro ao reescrever query: {e}")
        return {"query_reescrita": query, "ocasiao_alvo": target_slug}

def _query_embedding_text_from_rewrite(data, target_occasion=None):
    if not isinstance(data, dict):
        return None
    attrs = data.get("atributos_extraidos")
    tokens = []
    order = ["silhouette","neckline","sleeves","details","fabrics","cor_base","cor_comercial","occasions"]
    alt = {
        "cor base":"cor_base",
        "cor comercial":"cor_comercial",
        "estilo":"silhouette",
        "estilos":"silhouette",
        "decote":"neckline",
        "decotes":"neckline",
        "manga":"sleeves",
        "mangas":"sleeves",
        "detalhes":"details",
        "tecido":"fabrics",
        "tecidos":"fabrics",
        "ocasiao":"occasions",
        "ocasião":"occasions",
        "ocasioes":"occasions",
        "ocasiões":"occasions",
    }
    if isinstance(attrs, dict):
        for k in order:
            v = _get_attr_from_rewrite(attrs, k, alt)
            if isinstance(v, list):
                for x in v:
                    s = str(x).strip()
                    if s:
                        tokens.append(s)
                        if k == "occasions":
                            canon = _canonical_occasion(s)
                            if canon:
                                tokens.append(canon.replace("-", " "))
            elif isinstance(v, str) and v.strip():
                s = v.strip()
                tokens.append(s)
                if k == "occasions":
                    canon = _canonical_occasion(s)
                    if canon:
                        tokens.append(canon.replace("-", " "))

    occ = _canonical_occasion(target_occasion)
    if occ:
        tokens.append(occ.replace("-", " "))

    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    tokens = list(dict.fromkeys(tokens))
    return " ".join(tokens)

def _query_embedding_text_from_rewrite_attrs_only(data):
    if not isinstance(data, dict):
        return None
    attrs = data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return None
    tokens = []
    order = ["silhouette","neckline","sleeves","details","fabrics","cor_base","cor_comercial"]
    alt = {
        "cor base":"cor_base",
        "cor comercial":"cor_comercial",
        "estilo":"silhouette",
        "estilos":"silhouette",
        "decote":"neckline",
        "decotes":"neckline",
        "manga":"sleeves",
        "mangas":"sleeves",
        "detalhes":"details",
        "tecido":"fabrics",
        "tecidos":"fabrics",
    }
    for k in order:
        v = _get_attr_from_rewrite(attrs, k, alt)
        if isinstance(v, list):
            for x in v:
                s = str(x).strip()
                if s:
                    tokens.append(s)
        elif isinstance(v, str) and v.strip():
            tokens.append(v.strip())
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    tokens = list(dict.fromkeys(tokens))
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
    req_neg = {}
    color_base_req = set()
    color_comercial_req = set()
    key_map = {
        "estilo":"silhouette",
        "estilos":"silhouette",
        "decote":"neckline",
        "decotes":"neckline",
        "manga":"sleeves",
        "mangas":"sleeves",
        "detalhes":"details",
        "tecido":"fabrics",
        "tecidos":"fabrics",
        "ocasiao":"occasions",
        "ocasião":"occasions",
        "ocasioes":"occasions",
        "ocasiões":"occasions",
    }
    def collect_colors(values):
        out = []
        for key in values:
            if key not in attrs:
                continue
            raw = attrs.get(key)
            if isinstance(raw, list):
                out.extend(raw)
            elif isinstance(raw, str) and raw.strip():
                out.append(raw)
        return out
    base_vals = collect_colors(["cor_base","cor base"])
    comercial_vals = collect_colors(["cor_comercial","cor comercial"])
    if base_vals:
        color_base_req = set(norm(s) for s in base_vals if str(s).strip())
    if comercial_vals:
        color_comercial_req = set(norm(s) for s in comercial_vals if str(s).strip())
    for k in ["silhouette","neckline","sleeves","details","fabrics","occasions"]:
        v = _get_attr_from_rewrite(attrs, k, key_map)
        if isinstance(v, list):
            if k == "occasions":
                vals = [_canonical_occasion(s) for s in v if str(s).strip()]
                vals = [x for x in vals if x]
            else:
                vals = [norm(s) for s in v if str(s).strip()]
            if vals:
                req[k] = set(vals)
                neg = set()
                for s in vals:
                    if k == "details" and (s.startswith("sem ") or s.startswith("sem-")):
                        neg.add(s.replace("sem ", "").replace("sem-", "").strip())
                    if s.startswith("nao ") or s.startswith("não "):
                        neg.add(s.replace("nao ", "").replace("não ", "").strip())
                if neg:
                    req_neg[k] = neg
        elif isinstance(v, str) and v.strip():
            val = _canonical_occasion(v) if k == "occasions" else norm(v)
            if not val:
                continue
            req[k] = {val}
            neg = set()
            if k == "details" and (val.startswith("sem ") or val.startswith("sem-")):
                neg.add(val.replace("sem ", "").replace("sem-", "").strip())
            if val.startswith("nao ") or val.startswith("não "):
                neg.add(val.replace("nao ", "").replace("não ", "").strip())
            if neg:
                req_neg[k] = neg
    if not req and not (color_base_req or color_comercial_req):
        return candidates
    def has_intersection(values, needed, facet):
        if not isinstance(values, list):
            return False
        if facet == "occasions":
            nv = set(_canonical_occasion(x) for x in values if str(x).strip())
            nv = set(x for x in nv if x)
        else:
            nv = set(norm(x) for x in values if str(x).strip())
        return bool(nv.intersection(needed))
    # termos_excluir globais
    exclude_terms = set()
    te = rewrite_data.get("termos_excluir")
    if isinstance(te, list):
        for t in te:
            tt = norm(t)
            if tt:
                exclude_terms.add(tt)
    filtered = []
    for c in candidates:
        mf = c.get("metadata_filters", {}) or {}
        ok = True
        if color_base_req or color_comercial_req:
            mf_colors = mf.get("colors")
            if not isinstance(mf_colors, list):
                ok = False
            else:
                normalized_colors = set(norm(x) for x in mf_colors if str(x).strip())
                if color_base_req and not normalized_colors.intersection(color_base_req):
                    ok = False
                if ok and color_comercial_req and not normalized_colors.intersection(color_comercial_req):
                    ok = False
        for k in ["silhouette","occasions"]:
            if ok and k in req and not has_intersection(mf.get(k), req[k], k):
                ok = False
                break
        if ok and req_neg:
            def unify_set(values):
                return set(re.sub(r"[-\\s]+", "", norm(x)) for x in values if str(x).strip())
            for facet, negs in req_neg.items():
                vals = mf.get(facet)
                if isinstance(vals, list):
                    present = unify_set(vals)
                    neg_unified = set(re.sub(r"[-\\s]+", "", s) for s in negs)
                    if present.intersection(neg_unified):
                        ok = False
                        break
        if ok and exclude_terms:
            all_vals = []
            for facet_vals in mf.values():
                if isinstance(facet_vals, list):
                    all_vals.extend(facet_vals)
            present = set(norm(x) for x in all_vals if str(x).strip())
            if present.intersection(exclude_terms):
                ok = False
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
    color_base_need = set()
    color_comercial_need = set()
    key_map = {
        "estilo":"silhouette",
        "estilos":"silhouette",
        "decote":"neckline",
        "decotes":"neckline",
        "manga":"sleeves",
        "mangas":"sleeves",
        "detalhes":"details",
        "tecido":"fabrics",
        "tecidos":"fabrics",
        "ocasiao":"occasions",
        "ocasião":"occasions",
        "ocasioes":"occasions",
        "ocasiões":"occasions",
    }
    def collect_colors(values):
        out = []
        for key in values:
            if key not in attrs:
                continue
            raw = attrs.get(key)
            if isinstance(raw, list):
                out.extend(raw)
            elif isinstance(raw, str) and raw.strip():
                out.append(raw)
        return out
    base_vals = collect_colors(["cor_base","cor base"])
    comercial_vals = collect_colors(["cor_comercial","cor comercial"])
    if base_vals:
        color_base_need = set(norm(s) for s in base_vals if str(s).strip())
    if comercial_vals:
        color_comercial_need = set(norm(s) for s in comercial_vals if str(s).strip())
    for k in ["silhouette","neckline","sleeves","details","fabrics","occasions"]:
        v = _get_attr_from_rewrite(attrs, k, key_map)
        if isinstance(v, list):
            if k == "occasions":
                need[k] = set(_canonical_occasion(s) for s in v if str(s).strip())
                need[k] = set(x for x in need[k] if x)
            else:
                need[k] = set(norm(s) for s in v if str(s).strip())
        elif isinstance(v, str) and v.strip():
            if k == "occasions":
                occ = _canonical_occasion(v)
                if occ:
                    need[k] = {occ}
            else:
                need[k] = {norm(v)}
    weights = {"color_base":2,"color_comercial":2,"silhouette":2,"neckline":1,"sleeves":1,"details":1,"fabrics":1,"occasions":1}
    def score(c):
        mf = c.get("metadata_filters", {}) or {}
        s = 0
        if color_base_need or color_comercial_need:
            vals = mf.get("colors")
            if isinstance(vals, list):
                nv = set(norm(x) for x in vals if str(x).strip())
                if color_base_need and nv.intersection(color_base_need):
                    s += weights["color_base"]
                if color_comercial_need and nv.intersection(color_comercial_need):
                    s += weights["color_comercial"]
        for k, req in need.items():
            vals = mf.get(k)
            if isinstance(vals, list):
                if k == "occasions":
                    nv = set(_canonical_occasion(x) for x in vals if str(x).strip())
                    nv = set(x for x in nv if x)
                else:
                    nv = set(norm(x) for x in vals if str(x).strip())
                if nv.intersection(req):
                    s += weights.get(k, 1)
        return s
    ranked = sorted(
        [(score(c), i, c) for i, c in enumerate(candidates)],
        key=lambda x: (-x[0], x[1])
    )
    return [c for _, _, c in ranked]

def _rerank_by_facets_loose(candidates, rewrite_data):
    if not candidates or not isinstance(rewrite_data, dict):
        return candidates
    attrs = rewrite_data.get("atributos_extraidos")
    if not isinstance(attrs, dict):
        return candidates
    def norm(x):
        return _normalize_text(x)
    need = {}
    color_base_need = set()
    color_comercial_need = set()
    key_map = {
        "estilo":"silhouette",
        "estilos":"silhouette",
        "decote":"neckline",
        "decotes":"neckline",
        "manga":"sleeves",
        "mangas":"sleeves",
        "detalhes":"details",
        "tecido":"fabrics",
        "tecidos":"fabrics",
    }
    def collect_colors(values):
        out = []
        for key in values:
            if key not in attrs:
                continue
            raw = attrs.get(key)
            if isinstance(raw, list):
                out.extend(raw)
            elif isinstance(raw, str) and raw.strip():
                out.append(raw)
        return out
    base_vals = collect_colors(["cor_base","cor base"])
    comercial_vals = collect_colors(["cor_comercial","cor comercial"])
    if base_vals:
        color_base_need = set(norm(s) for s in base_vals if str(s).strip())
    if comercial_vals:
        color_comercial_need = set(norm(s) for s in comercial_vals if str(s).strip())
    for k in ["silhouette","neckline","sleeves","details","fabrics"]:
        v = _get_attr_from_rewrite(attrs, k, key_map)
        if isinstance(v, list):
            need[k] = set(norm(s) for s in v if str(s).strip())
        elif isinstance(v, str) and v.strip():
            need[k] = {norm(v)}
    weights = {"color_base":2,"color_comercial":2,"silhouette":2,"neckline":1,"sleeves":1,"details":1,"fabrics":1}
    def score(c):
        mf = c.get("metadata_filters", {}) or {}
        s = 0
        if color_base_need or color_comercial_need:
            vals = mf.get("colors")
            if isinstance(vals, list):
                nv = set(norm(x) for x in vals if str(x).strip())
                if color_base_need and nv.intersection(color_base_need):
                    s += weights["color_base"]
                if color_comercial_need and nv.intersection(color_comercial_need):
                    s += weights["color_comercial"]
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
                        'ProjectionExpression': 'item_id, #st, title, item_title, item_description, item_image_url, item_value, item_custom_id, category, item_category, cor, cores, cor_base, cor_comercial, tamanho, occasion_noiva, occasion_civil, occasion_madrinha, occasion_mae_dos_noivos, occasion_formatura, occasion_debutante, occasion_gala, occasion_convidada',
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
            meta['cor'] = db_item.get('cor', meta.get('cor'))
            meta['cores'] = db_item.get('cores', meta.get('cores'))
            meta['cor_base'] = db_item.get('cor_base', meta.get('cor_base'))
            meta['cor_comercial'] = db_item.get('cor_comercial', meta.get('cor_comercial'))
            meta['tamanho'] = db_item.get('tamanho', meta.get('tamanho'))
            cat_slug = _category_slug(meta)
            meta['item_category'] = db_item.get('item_category', meta.get('item_category'))
            meta['category_slug'] = cat_slug
            
            # Copy occasion flags from DynamoDB
            for occ_key in ["occasion_noiva", "occasion_civil", "occasion_madrinha", "occasion_mae_dos_noivos", "occasion_formatura", "occasion_debutante", "occasion_gala", "occasion_convidada"]:
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
        rewrite = _rewrite_catalog_query(query, None)
        q_text = _query_embedding_text_from_rewrite(rewrite, rewrite.get("ocasiao_alvo")) or rewrite.get("query_reescrita") or query
        query_embedding = get_embedding(q_text)
        query_vector = np.array([query_embedding]).astype('float32')

        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0
        if ntotal <= 0:
            return []

        desired = int(k or 0)
        if desired < 1:
            desired = 1
        if desired > 50:
            desired = 50

        valid_pool = []
        seen_custom_ids = set()
        processed_k = 0
        search_k = min(max(80, desired * 20), ntotal)

        ranked = []
        while processed_k < ntotal:
            distances, indices = index.search(query_vector, search_k)
            new_positions = indices[0][processed_k:search_k]
            processed_k = search_k

            raw_candidates = []
            for idx in new_positions:
                if idx == -1:
                    continue
                meta = metadata[int(idx)].copy()
                cid = meta.get("custom_id")
                if cid and cid not in seen_custom_ids:
                    seen_custom_ids.add(cid)
                    raw_candidates.append(meta)

            valid_candidates = validate_and_enrich_candidates(raw_candidates)
            valid_pool.extend(valid_candidates)

            constrained = _apply_facet_constraints(valid_pool, rewrite)
            ranked = _rerank_by_facets(constrained, rewrite) if constrained else valid_pool
            if len(ranked) >= desired:
                break
            if processed_k >= ntotal:
                break
            search_k = min(ntotal, max(processed_k + 80, search_k * 2))

        return ranked[:desired]

    except Exception as e:
        print(f"Erro em execute_catalog_search: {e}")
        return []

def execute_catalog_search_raw(query, k=5):
    global index, metadata
    if not index or not metadata:
        load_resources()
        if not index or not metadata:
            print("Erro: Índice não disponível para execute_catalog_search_raw")
            return []
    try:
        query_embedding = get_embedding(query)
        query_vector = np.array([query_embedding]).astype('float32')
        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0
        if ntotal <= 0:
            return []
        desired = int(k or 0)
        if desired < 1:
            desired = 1
        if desired > ntotal:
            desired = ntotal
        distances, indices = index.search(query_vector, desired)
        raw_candidates = []
        seen_custom_ids = set()
        for idx in indices[0]:
            if idx == -1:
                continue
            meta = metadata[int(idx)].copy()
            cid = meta.get("custom_id")
            if cid and cid in seen_custom_ids:
                continue
            if cid:
                seen_custom_ids.add(cid)
            raw_candidates.append(meta)
        valid_candidates = validate_and_enrich_candidates(raw_candidates)
        return valid_candidates[:desired]
    except Exception as e:
        print(f"Erro em execute_catalog_search_raw: {e}")
        return []

def execute_catalog_search_loose(query, k=5, target_occasions=None):
    global index, metadata
    
    if not index or not metadata:
        load_resources()
        if not index or not metadata:
            print("Erro: Índice não disponível para execute_catalog_search_loose")
            return []

    try:
        rewrite = _rewrite_catalog_query(query, None)
        q_text = _query_embedding_text_from_rewrite_attrs_only(rewrite) or query
        query_embedding = get_embedding(q_text)
        query_vector = np.array([query_embedding]).astype('float32')

        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0
        if ntotal <= 0:
            return []

        desired = int(k or 0)
        if desired < 1:
            desired = 1
        if desired > 50:
            desired = 50

        target_set = set(target_occasions or [])
        collected = []
        seen_custom_ids = set()
        processed_k = 0
        search_k = min(max(80, desired * 20), ntotal)

        while processed_k < ntotal and len(collected) < desired:
            distances, indices = index.search(query_vector, search_k)
            new_positions = indices[0][processed_k:search_k]
            processed_k = search_k

            raw_candidates = []
            for idx in new_positions:
                if idx == -1:
                    continue
                meta = metadata[int(idx)].copy()
                cid = meta.get("custom_id")
                if cid and cid not in seen_custom_ids:
                    seen_custom_ids.add(cid)
                    raw_candidates.append(meta)

            valid_candidates = validate_and_enrich_candidates(raw_candidates)
            if target_set:
                valid_candidates = [
                    c for c in valid_candidates
                    if _get_occasion_slugs(c) and (set(_get_occasion_slugs(c)) & target_set)
                ]
            ranked_candidates = _rerank_by_facets_loose(valid_candidates, rewrite) if valid_candidates else valid_candidates
            for item in ranked_candidates:
                if target_set:
                    occ_slugs = _get_occasion_slugs(item)
                    if not occ_slugs or not (set(occ_slugs) & target_set):
                        continue
                collected.append(item)
                if len(collected) >= desired:
                    break

            if processed_k >= ntotal:
                break
            search_k = min(ntotal, max(processed_k + 80, search_k * 2))

        return collected[:desired]

    except Exception as e:
        print(f"Erro em execute_catalog_search_loose: {e}")
        return []

def search_and_prioritize(index, metadata, query, occasions=None, cor_comercial=None, cor_base=None, top_k=None):
    if not index or not metadata:
        return []
    if not isinstance(query, str) or not query.strip():
        return []
    ntotal = int(getattr(index, "ntotal", 0) or 0)
    if ntotal <= 0:
        ntotal = len(metadata) if isinstance(metadata, list) else 0
    if ntotal <= 0:
        return []

    desired_k = int(top_k or 0)
    if desired_k < 1:
        desired_k = ntotal
    if desired_k > ntotal:
        desired_k = ntotal

    desired_occ_set = set(_normalize_occasion_inputs(occasions))
    desired_comercial = _normalize_set(cor_comercial)
    desired_base = _normalize_set(cor_base)

    query_preview = str(query or "").replace("\n", " ").strip()
    if len(query_preview) > 160:
        query_preview = query_preview[:160] + "..."
    print("search_and_prioritize_args", json.dumps({
        "query": query_preview,
        "occasions": occasions,
        "cor_comercial": cor_comercial,
        "cor_base": cor_base,
        "top_k": top_k,
    }, ensure_ascii=False))

    query_embedding = get_embedding(query.strip())
    query_vector = np.array([query_embedding]).astype('float32')
    distances, indices = index.search(query_vector, desired_k)
    prio_especifica = []
    prio_base = []
    prio_geral = []

    seen_custom_ids = set()
    batch = []

    def _flush_batch():
        nonlocal batch
        if not batch:
            return
        valid_items = validate_and_enrich_candidates(batch)
        for item in valid_items:
            if desired_occ_set:
                slugs = _get_occasion_slugs(item)
                if slugs:
                    if not (set(slugs) & desired_occ_set):
                        continue
                else:
                    matched = False
                    for occ in desired_occ_set:
                        if _has_occasion(item, occ):
                            matched = True
                            break
                    if not matched:
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

def _store_context_payload():
    markdown = ""
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

def _mcp_tools():
    account_id = session.get("account_id") if session else None
    if not account_id:
        account_id = _pick_public_account_id()
    color_pairs = _load_color_pairs_for_account(account_id)
    cor_base_options = [str(c.get("base") or "").strip() for c in color_pairs if str(c.get("base") or "").strip()]
    cor_comercial_options = [str(c.get("name") or "").strip() for c in color_pairs if str(c.get("name") or "").strip()]
    if not cor_base_options:
        cor_base_options = _load_color_options_for_account(account_id)
    if not cor_comercial_options:
        cor_comercial_options = _load_color_options_for_account(account_id)
    cor_base_options = list(dict.fromkeys(cor_base_options))
    cor_comercial_options = list(dict.fromkeys(cor_comercial_options))
    description = (
        "Busca por similaridade semântica com filtros de ocasião, cor_base/cor_comercial e tamanho. "
        "Use cor_base e cor_comercial para restringir a busca e sizes quando houver preferência. "
        "Se uma cor ou tamanho não estiver claro, deixe o campo vazio e faça follow-up."
    )
    cor_base_items = {"type": "string", "enum": cor_base_options} if cor_base_options else {"type": "string"}
    cor_comercial_items = {"type": "string", "enum": cor_comercial_options} if cor_comercial_options else {"type": "string"}
    tools = [
        {
            "name": "buscar_por_similaridade",
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "other_characteristics": {
                        "type": "string",
                        "description": "Características livres como tomara que caia, renda, brilho, fenda, cauda sereia, etc."
                    },
                    "occasions": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["Noiva","Civil","Madrinha","Mãe dos Noivos","Formatura","Debutante","Gala","Convidada"]
                        },
                        "description": "Ocasiões suportadas. Se não houver ocasião explícita ou no contexto, deixe vazio."
                    },
                    "cor_base": {
                        "type": "array",
                        "items": cor_base_items,
                        "description": "Cor base disponível no acervo. Se o usuário digitar cor parcial ou com erro, escolha a opção MAIS PRÓXIMA do enum e use o texto EXATO. Ex: vermelho -> Vermelho/Vinho (se existir). Se houver dúvida, deixe vazio."
                    },
                    "cor_comercial": {
                        "type": "array",
                        "items": cor_comercial_items,
                        "description": "Cor comercial disponível no acervo. Se o usuário digitar cor parcial ou com erro, escolha a opção MAIS PRÓXIMA do enum e use o texto EXATO. Se houver dúvida, deixe vazio."
                    },
                    "sizes": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "limit": {
                        "type": "integer",
                        "default": 6
                    }
                },
                "required": []
            }
        },
        {
            "name": "consultar_contexto_loja",
            "description": "Retorna um markdown com contexto oficial da loja para apoiar a resposta. Use quando o cliente perguntar sobre a loja.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "panorama_cores_ocasioes",
            "description": "Resumo de cores por ocasião com contagem real do catálogo. Use quando não houver resultados obtidos pela tool buscar_por_similaridade e use a resposta desta tool para sugerir cores alternativas.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        }
    ]
    print("bella_tool_schema_after_context", json.dumps(tools, ensure_ascii=False))
    print("#####################")
    return tools

def _mcp_to_openai_tools(mcp_tools):
    openai_tools = []
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"]
            }
        })
    return openai_tools

def _catalog_extractor_tools():
    account_id = session.get("account_id") if session else None
    if not account_id:
        account_id = _pick_public_account_id()
    try:
        cor_base_options, cor_comercial_options = _color_enums_for_account()
    except Exception:
        fallback = _load_color_options_for_account(account_id) if account_id else []
        cor_base_options = list(dict.fromkeys([str(x).strip() for x in fallback if str(x).strip()]))
        cor_comercial_options = list(cor_base_options)
    cor_base_items = {"type": "string", "enum": cor_base_options} if cor_base_options else {"type": "string"}
    cor_comercial_items = {"type": "string", "enum": cor_comercial_options} if cor_comercial_options else {"type": "string"}
    return [
        {
            "type": "function",
            "function": {
                "name": "extrair_filtros_catalogo",
                "description": "Extrai filtros estruturados (cor_base, cor_comercial, ocasiões) e outros atributos da consulta do catálogo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "other_characteristics": {
                            "type": "string",
                            "description": "Atributos livres (ex.: brilho, renda, fenda, decote em V). Não repita cores/ocasião se já estiverem nos campos apropriados."
                        },
                        "occasions": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["Noiva", "Civil", "Madrinha", "Mãe dos Noivos", "Formatura", "Debutante", "Gala", "Convidada"],
                            },
                            "description": "Ocasiões suportadas. Preencha apenas quando estiver explícito na consulta."
                        },
                        "cor_base": {
                            "type": "array",
                            "items": cor_base_items,
                            "description": "Cores base disponíveis. Use somente valores do enum."
                        },
                        "cor_comercial": {
                            "type": "array",
                            "items": cor_comercial_items,
                            "description": "Cores comerciais disponíveis. Use somente valores do enum."
                        },
                    },
                    "required": []
                }
            }
        }
    ]

def _extract_catalog_filters_with_tool(query, target_occasion=None):
    if not isinstance(query, str):
        return {}
    query = query.strip()
    if not query:
        return {}
    tool_name = "extrair_filtros_catalogo"
    tools = _catalog_extractor_tools()
    occ_slug = _canonical_occasion(target_occasion) if target_occasion else ""
    system_prompt = (
        "Você extrai filtros estruturados de uma consulta de busca em catálogo de vestidos.\n"
        "Regras:\n"
        "- Responda chamando a ferramenta.\n"
        "- Em cor_base e cor_comercial, use SOMENTE valores do enum.\n"
        "- Se a consulta contiver apenas uma cor, preencha a cor e deixe other_characteristics vazio.\n"
        "- Se houver ocasião alvo (aba ativa), não invente novas ocasiões.\n"
    )
    user_payload = {
        "consulta_usuario": query,
        "ocasiao_alvo": occ_slug,
    }
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=0.0,
            max_tokens=220,
        )
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return {}
        call = None
        for tc in msg.tool_calls:
            if tc.function and tc.function.name == tool_name:
                call = tc
                break
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
        validation_errors = _validate_similarity_args(out)
        if validation_errors:
            return {}
        out["cor_base"] = list(dict.fromkeys(out["cor_base"]))
        out["cor_comercial"] = list(dict.fromkeys(out["cor_comercial"]))
        out["occasions"] = list(dict.fromkeys(out["occasions"]))
        return out
    except Exception:
        return {}

def _filter_metadata_candidates(cor_base, cor_comercial, occasions, max_candidates):
    if not metadata:
        return []
    desired_base = _normalize_set(cor_base)
    desired_comercial = _normalize_set(cor_comercial)
    desired_occasions = _normalize_occasion_inputs(occasions)
    desired_set = set(desired_occasions)
    collected = []
    for meta in metadata:
        if desired_base or desired_comercial:
            base_ok = bool(desired_base and _matches_any(_extract_color_base_list(meta), desired_base))
            comercial_ok = bool(desired_comercial and _matches_any(_extract_color_commercial_list(meta), desired_comercial))
            if not (base_ok or comercial_ok):
                continue
        if desired_set:
            if not (set(_meta_occasions(meta)) & desired_set):
                continue
        collected.append(meta.copy())
        if len(collected) >= max_candidates:
            break
    return collected

def _run_db_search(args):
    args = _coerce_similarity_color_fields(args)
    validation_errors = _validate_similarity_args(args)
    if validation_errors:
        return {
            "items": [],
            "error": {
                "type": "validation_error",
                "message": "Erro de validação nos argumentos da ferramenta.",
                "details": validation_errors,
            }
        }
    cor_base = args.get("cor_base") or args.get("corBase") or []
    cor_comercial = args.get("cor_comercial") or args.get("corComercial") or []

    if cor_comercial:
        account_id = session.get("account_id") if session else None
        if not account_id:
            account_id = _pick_public_account_id()
        color_pairs = _load_color_pairs_for_account(account_id) if account_id else []
        commercial_to_base = {}
        for pair in color_pairs or []:
            base = str(pair.get("base") or "").strip()
            name = str(pair.get("name") or "").strip()
            if base and name:
                commercial_to_base[_normalize_text(name)] = base
        mapped_bases = []
        for cc in _ensure_list(cor_comercial):
            mapped = commercial_to_base.get(_normalize_text(cc))
            if mapped and mapped.strip():
                mapped_bases.append(mapped.strip())
        mapped_bases = list(dict.fromkeys(mapped_bases))
        if mapped_bases:
            desired_base = _normalize_set(cor_base)
            mapped_set = _normalize_set(mapped_bases)
            if not desired_base or not (desired_base & mapped_set):
                cor_base = mapped_bases
    colors = [*cor_base, *cor_comercial]
    sizes = args.get("sizes") or args.get("tamanhos") or args.get("size") or args.get("tamanho") or []
    occasions = _normalize_occasion_inputs(args.get("occasions") or args.get("ocasioes") or args.get("occasion") or [])
    exact_color_terms = _normalize_set(colors)
    desired_base = _normalize_set(cor_base)
    desired_comercial = _normalize_set(cor_comercial)
    limit = int(args.get("limit") or args.get("k") or 5)
    if limit < 1:
        limit = 1
    if limit > 5:
        limit = 5
    if not metadata or not index:
        load_resources()
    candidates = _filter_metadata_candidates(cor_base, cor_comercial, occasions, max_candidates=max(200, limit * 40))
    enriched = validate_and_enrich_candidates(candidates)
    desired_sizes = _normalize_set(sizes)
    desired_occ_set = set(occasions)
    commercial = []
    base = []
    general = []
    seen_custom_ids = set()
    for item in enriched:
        if desired_sizes and not _matches_any(_extract_size_list(item), desired_sizes):
            continue
        if desired_occ_set:
            if not (set(_get_occasion_slugs(item)) & desired_occ_set):
                continue
        cid = item.get("custom_id")
        if cid and cid in seen_custom_ids:
            continue
        if cid:
            seen_custom_ids.add(cid)
        comercial_ok = bool(desired_comercial and _matches_any(_extract_color_commercial_list(item), desired_comercial))
        base_ok = bool(desired_base and _matches_any(_extract_color_base_list(item), desired_base))
        if desired_base or desired_comercial:
            if not (comercial_ok or base_ok):
                continue
        if comercial_ok:
            commercial.append(item)
        elif base_ok:
            base.append(item)
        else:
            general.append(item)
    filtered = (commercial + base + general)[:limit]
    exact_filtered = commercial[:limit] if desired_comercial else list(filtered)
    no_principal = bool(desired_comercial and filtered and not exact_filtered)
    if not filtered:
        account_id = session.get("account_id") if session else None
        if not account_id:
            account_id = _pick_public_account_id()
        fallback_items = _scan_items_from_db(
            account_id=account_id,
            desired_base=desired_base,
            desired_comercial=desired_comercial,
            desired_occ_set=desired_occ_set,
            desired_sizes=desired_sizes,
            limit=limit,
        )
        if fallback_items:
            return {
                "items": fallback_items,
                "exact_items": fallback_items if desired_comercial else list(fallback_items),
                "no_principal": False,
            }
        return {
            "items": [],
            "error": {
                "type": "no_results",
                "message": "Busca não retornou resultados."
            }
        }
    return {
        "items": filtered,
        "exact_items": exact_filtered,
        "no_principal": no_principal,
    }

def _run_similarity_search(args):
    args = _coerce_similarity_color_fields(args)
    validation_errors = _validate_similarity_args(args)
    if validation_errors:
        return {
            "items": [],
            "error": {
                "type": "validation_error",
                "message": "Erro de validação nos argumentos da ferramenta.",
                "details": validation_errors,
            }
        }
    cor_base = args.get("cor_base") or args.get("corBase") or []
    cor_comercial = args.get("cor_comercial") or args.get("corComercial") or []
    colors = [*cor_base, *cor_comercial]
    sizes = args.get("sizes") or args.get("tamanhos") or args.get("size") or args.get("tamanho") or []
    occasions = _normalize_occasion_inputs(args.get("occasions") or args.get("ocasioes") or args.get("occasion") or [])
    exact_color_terms = _normalize_set(colors)
    desired_base = _normalize_set(cor_base)
    desired_comercial = _normalize_set(cor_comercial)
    query = args.get("other_characteristics")
    limit = int(args.get("limit") or args.get("k") or 5)
    if limit < 1:
        limit = 1
    if limit > 5:
        limit = 5
    candidate_k = max(limit * 6, 20)
    if desired_base or desired_comercial:
        if not index or not metadata:
            load_resources()
        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0
        if ntotal > 0:
            candidate_k = ntotal
        else:
            candidate_k = max(limit * 20, 120)
    query = str(query or "").strip()
    debug_payload = {
        "cor_base": cor_base,
        "cor_comercial": cor_comercial,
        "sizes": sizes,
        "occasions": occasions,
        "other_characteristics": query,
        "limit": limit,
        "candidate_k": candidate_k,
    }
    print("bella_tool_preprocessed", json.dumps(debug_payload, ensure_ascii=False))
    if not query:
        if not (cor_base or cor_comercial or sizes or occasions):
            return {"items": []}
        db_result = _run_db_search(args)
        items = db_result.get("items") or []
        result_payload = {"items": items[:limit]}
        print("bella_tool_result", json.dumps({"count": len(result_payload["items"])}, ensure_ascii=False))
        if not result_payload["items"]:
            result_payload["error"] = {"type": "no_results", "message": "Busca não retornou resultados."}
        return result_payload

    if not index or not metadata:
        load_resources()
    results = search_and_prioritize(
        index=index,
        metadata=metadata,
        query=query,
        occasions=occasions,
        cor_comercial=cor_comercial,
        cor_base=cor_base,
        top_k=candidate_k,
    )
    if occasions:
        results = _filter_items_by_occasions(results, occasions)
    desired_sizes = _normalize_set(sizes)
    desired_occ_set = set(occasions)
    exact_items = []
    fallback_items = []
    seen_custom_ids = set()
    for item in results:
        if desired_sizes and not _matches_any(_extract_size_list(item), desired_sizes):
            continue
        if desired_occ_set and not (set(_get_occasion_slugs(item)) & desired_occ_set):
            continue
        cid = item.get("custom_id")
        if cid and cid in seen_custom_ids:
            continue
        if cid:
            seen_custom_ids.add(cid)
        comercial_ok = bool(desired_comercial and _matches_any(_extract_color_commercial_list(item), desired_comercial))
        base_ok = bool(desired_base and _matches_any(_extract_color_base_list(item), desired_base))
        if desired_base or desired_comercial:
            if not (comercial_ok or base_ok):
                continue
        if desired_comercial:
            if comercial_ok:
                exact_items.append(item)
            elif base_ok:
                fallback_items.append(item)
        else:
            exact_items.append(item)
        if len(exact_items) >= limit:
            break
    if desired_comercial and len(exact_items) < limit and fallback_items:
        needed = limit - len(exact_items)
        exact_items.extend(fallback_items[:needed])
    result_payload = {"items": exact_items[:limit]}
    print("bella_tool_result", json.dumps({"count": len(result_payload["items"])}, ensure_ascii=False))
    if not result_payload["items"]:
        result_payload["error"] = {"type": "no_results", "message": "Busca não retornou resultados."}
    return result_payload

def _summarize_items_for_llm(items):
    error = None
    if isinstance(items, dict):
        error = items.get("error")
        items = items.get("items") or []
    payload = []
    for item in items:
        suggestion = _build_suggestion(item)
        payload.append({
            "title": suggestion.get("title") or "",
            "description": (suggestion.get("description") or "")[:240],
            "color_base": suggestion.get("color_base") or "",
            "color_comercial": suggestion.get("color_comercial") or "",
            "size": suggestion.get("size") or "",
            "occasions": suggestion.get("occasions") or [],
            "image_url": suggestion.get("image_url") or "",
        })
    response = {"items": payload}
    if error:
        response["error"] = error
    return response

def _summarize_items_for_client(items):
    error = None
    if isinstance(items, dict):
        error = items.get("error")
        items = items.get("items") or []
    payload = []
    for item in items:
        suggestion = _build_suggestion(item)
        payload.append({
            "id": suggestion.get("id") or "",
            "customId": suggestion.get("customId") or "",
            "title": suggestion.get("title") or "",
            "description": (suggestion.get("description") or "")[:240],
            "price": suggestion.get("price") or "",
            "color": suggestion.get("color") or "",
            "color_base": suggestion.get("color_base") or "",
            "color_comercial": suggestion.get("color_comercial") or "",
            "size": suggestion.get("size") or "",
            "occasions": suggestion.get("occasions") or [],
            "image_url": suggestion.get("image_url") or "",
        })
    response = {"items": payload}
    if error:
        response["error"] = error
    return response

@ai_bp.route('/api/ai-search', methods=['POST'])
def ai_search():
    data = request.get_json()
    user_message = data.get('message', '')
    history = data.get('history', [])

    if not user_message:
        return jsonify({"error": "Mensagem vazia."}), 400
    current_app.logger.info(
        "bella_ai_search_start message_len=%s history_len=%s",
        len(user_message or ""),
        len(history or []),
    )

    # System Prompt: Persona Bella (Agente Vendedora)
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
        "- Após listar os vestidos, explique de forma natural quais filtros foram usados na busca (ex.: cor, ocasião, tamanho ou restrições do pedido). Quando houver contexto anterior influenciando a busca, deixe isso claro.\n"
        "- Se não houver itens, NÃO invente vestidos. Use a tool `panorama_cores_ocasioes` e proponha alternativas reais do catálogo.\n"
        "- SEMPRE finalize com uma pergunta de follow-up.\n"
        "- Quando a cliente mudar a preferência (ex.: 'mais discreto', 'sem fenda', 'menos brilho', 'decote fechado'), refaça a busca com esses critérios e traga novas opções.\n"
        "- Traduza adjetivos comuns em restrições do catálogo: 'discreto' → silhueta clássica (reto/evasê), decote discreto, sem fenda, poucos detalhes, cores neutras; 'chamativo' → brilho, fenda, decotes marcantes, cores vivas; 'romântico' → renda/volume; etc.\n"
        "- Se não houver opções após os filtros, faça nova busca relaxando critérios (cor próxima, tamanho aproximado, ocasião relacionada) e informe isso no texto.\n"
        "- Quando não houver resultados ou quando vierem menos de 3 opções, use a ferramenta `panorama_cores_ocasioes` para entender a distribuição por ocasião e sugerir alternativas com base em quantidades reais e cores similares.\n"
        "SEM RESULTADOS (USO DE PANORAMA):\n"
        "- Quando `buscar_por_similaridade` retornar `items` vazio ou `error.type = no_results`, chame `panorama_cores_ocasioes`.\n"
        "- Use a resposta do panorama para sugerir alternativas no seguinte formato (exatamente como lista com hífen):\n"
        "  - Vermelho/Vinho (Bordô) — 7 opções para convidadas e 11 para madrinhas.\n"
        "  - Verde Esmeralda — disponível em 12 opções para gala.\n"
        "  - Azul Royal — disponível em 11 opções para gala.\n"
        "- Regras do formato: 3 a 6 linhas, cada linha é uma cor (cor_base e/ou cor_comercial) e contagens por ocasião com números reais do panorama.\n"
        "- Priorize ocasiões do pedido; se não houver, priorize Gala, Convidada e Madrinha.\n"
        "- Se o panorama retornar `total_items = 0`, diga que não há itens no catálogo no momento e peça para ajustar (ou aguardar atualização).\n"
        "USO DE FERRAMENTAS:\n"
        "- Para buscar vestidos e montar recomendações, use `buscar_por_similaridade`.\n"
        "- Para entender disponibilidade por ocasião/cor e sugerir alternativas de forma proativa, use `panorama_cores_ocasioes`.\n"
        "- Quando a cliente perguntar sobre a loja (nome, ramo, endereço, horário, atendimento ou política de venda), use `consultar_contexto_loja` apenas como contexto e responda de forma natural, sem copiar o markdown literalmente.\n"
        "- Preencha `cor_base`, `cor_comercial`, `sizes` e `other_characteristics` conforme o pedido.\n"
        "- Preencha `occasions` apenas quando houver ocasião explícita ou contexto claro. Use uma das opções: Noiva, Civil, Madrinha, Mãe dos Noivos, Formatura, Debutante, Gala, Convidada.\n"
        "- Quando a ocasião não estiver clara, deixe `occasions` vazio.\n"
        "- Se a cliente disser uma cor genérica (ex.: azul), inclua essa cor em `cor_base`.\n"
        "- Se a busca não retornar nada, faça nova busca relaxando critérios (cor próxima, tamanho aproximado, ocasião relacionada) e informe isso no texto.\n"
        "- Se a ferramenta retornar `error`, explique o erro e refaça a chamada corrigindo os campos. Quando `type` for `no_results`, informe que não encontrou resultados e pergunte como ajustar.\n"
        "- A ferramenta retorna JSON com `items` e pode incluir `error`.\n"
    )

    # Constrói mensagens
    messages = [{"role": "system", "content": system_prompt}]
    
    # Adiciona histórico (simplificado)
    if history:
        for msg in history:
            if isinstance(msg, dict) and msg.get('role') in ['user', 'assistant'] and msg.get('content'):
                messages.append({"role": msg['role'], "content": msg['content']})
    
    messages.append({"role": "user", "content": user_message})

    try:
        tools = _mcp_to_openai_tools(_mcp_tools())
    except Exception as e:
        current_app.logger.error("bella_tool_schema_error error=%s", str(e))
        return jsonify({"error": "tool_schema_error", "message": str(e)}), 500
    try:
        current_app.logger.info("bella_tool_schema tools=%s", json.dumps(tools, ensure_ascii=False))
    except Exception:
        pass

    try:
        reply_text = ""
        client_payload = None
        max_turns = 6

        for _ in range(max_turns):
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto"
            )
            response_message = response.choices[0].message
            if response_message.tool_calls:
                messages.append(response_message)
                for tool_call in response_message.tool_calls:
                    tool_name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments or "{}")
                    print("bella_tool_called", json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False))
                    current_app.logger.info(
                        "bella_tool_call tool=%s args=%s",
                        tool_name,
                        json.dumps(args, ensure_ascii=False),
                    )
                    if tool_name == "buscar_por_similaridade":
                        tool_results = _run_similarity_search(args)
                        tool_items = tool_results or []
                        summary_payload = _summarize_items_for_llm(tool_items)
                        client_payload = _summarize_items_for_client(tool_items)
                        print("bella_tool_raw_result", json.dumps(summary_payload, ensure_ascii=False))
                        current_app.logger.info(
                            "bella_tool_output tool=%s output=%s",
                            tool_name,
                            json.dumps(summary_payload, ensure_ascii=False),
                        )
                        current_app.logger.info(
                            "bella_tool_result tool=%s results=%s",
                            tool_name,
                            len(summary_payload.get("items") or []),
                        )
                        summary = json.dumps(summary_payload, ensure_ascii=False)
                    elif tool_name == "panorama_cores_ocasioes":
                        account_id = session.get("account_id") if session else None
                        if not account_id:
                            account_id = _pick_public_account_id()
                        panorama_payload = None
                        if itens_table and account_id:
                            try:
                                panorama_payload = _build_color_occasion_panorama_from_db(account_id)
                            except Exception:
                                panorama_payload = None
                        if panorama_payload is None:
                            if not metadata:
                                load_resources()
                            panorama_payload = _build_color_occasion_panorama(metadata)
                        print("bella_tool_raw_result", json.dumps(panorama_payload, ensure_ascii=False))
                        current_app.logger.info(
                            "bella_tool_output tool=%s output=%s",
                            tool_name,
                            json.dumps(panorama_payload, ensure_ascii=False),
                        )
                        summary = json.dumps(panorama_payload, ensure_ascii=False)
                    elif tool_name == "consultar_contexto_loja":
                        context_payload = _store_context_payload()
                        print("bella_tool_raw_result", json.dumps(context_payload, ensure_ascii=False))
                        current_app.logger.info(
                            "bella_tool_output tool=%s output=%s",
                            tool_name,
                            json.dumps(context_payload, ensure_ascii=False),
                        )
                        summary = json.dumps(context_payload, ensure_ascii=False)
                    else:
                        summary = json.dumps({"error": "tool_not_found"}, ensure_ascii=False)

                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": tool_name,
                        "content": summary
                    })
                continue
            reply_text = response_message.content
            current_app.logger.info("bella_no_tool_response")
            break

        if not reply_text:
            final_response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
            reply_text = final_response.choices[0].message.content
        response_payload = {"reply": reply_text}
        if client_payload:
            response_payload["items"] = client_payload.get("items") or []
            error_payload = client_payload.get("error")
            if isinstance(error_payload, dict) and error_payload.get("type") == "no_results":
                error_payload = None
            if error_payload:
                response_payload["error"] = error_payload
        return jsonify(response_payload)

    except Exception as e:
        current_app.logger.exception("Erro no Agente Bella")
        return jsonify({"error": str(e)}), 500

@ai_bp.route('/api/ai-similar/<item_id>', methods=['GET'])
def ai_similar(item_id):
    global index, metadata
    
    try:
        no_faiss = _flag_is_set(request.args.get("no_faiss") or request.args.get("noFaiss") or request.args.get("no_faiss_search"))

        limit = int(request.args.get("limit") or 4)
        if limit < 1:
            limit = 1
        if limit > 24:
            limit = 24

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

        def _extract_color_value(obj):
            v = obj.get("cor") or obj.get("cores") or obj.get("color") or obj.get("colors")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _extract_color_base_value(obj):
            v = obj.get("cor_base") or obj.get("color_base")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            mf = obj.get("metadata_filters")
            if isinstance(mf, dict):
                v = mf.get("cor_base")
                if isinstance(v, (list, tuple)):
                    for x in v:
                        if isinstance(x, str) and x.strip():
                            return x.strip()
                    return ""
                if isinstance(v, str):
                    return v.strip()
            return ""

        def _extract_color_commercial_value(obj):
            v = obj.get("cor_comercial") or obj.get("color_comercial")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _extract_size_value(obj):
            v = obj.get("tamanho") or obj.get("size") or obj.get("item_tamanho") or obj.get("item_size") or obj.get("sizes")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _build_suggestions(items):
            suggestions = []
            for item in items:
                suggestions.append({
                    "id": item.get("item_id") or item.get("custom_id"),
                    "customId": item.get("customId"),
                    "title": item.get("title", "Vestido"),
                    "image_url": item.get("imageUrl") or url_for("static", filename=f"dresses/{item['file_name']}"),
                    "description": item.get("description", ""),
                    "category": item.get("category", "Outros"),
                    "color": _extract_color_value(item),
                    "color_base": _extract_color_base_value(item),
                    "color_comercial": _extract_color_commercial_value(item),
                    "size": _extract_size_value(item),
                    "occasions": _get_occasions_list(item),
                })
            return suggestions

        if no_faiss:
            cor_base = _extract_list_from_args(["cor_base", "corBase", "color_base", "colorBase"])
            cor_comercial = _extract_list_from_args(["cor_comercial", "corComercial", "color_comercial", "colorComercial"])
            db_limit = min(limit + 1, 5)
            db_result = _run_db_search({
                "occasions": target_occ,
                "cor_base": cor_base,
                "cor_comercial": cor_comercial,
                "limit": db_limit,
            })
            items = db_result.get("items") or []
            filtered = []
            target_id = str(item_id)
            for item in items:
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid == target_id:
                    continue
                filtered.append(item)
                if len(filtered) >= limit:
                    break
            return jsonify({"suggestions": _build_suggestions(filtered)})

        if not index or not metadata:
            load_resources()
            if not index or not metadata:
                return jsonify({"error": "Sistema indisponível"}), 503

        query_hint = request.args.get("q") or request.args.get("query")

        target_idx = -1
        for i, item in enumerate(metadata):
            if str(item.get('custom_id')) == str(item_id):
                target_idx = i
                break

        target_flags = None
        try:
            if itens_table:
                resp = itens_table.get_item(Key={"item_id": str(item_id)})
                target_flags = resp.get("Item")
        except Exception:
            target_flags = None

        target_color_base_raw = _extract_color_base_value(target_flags) if isinstance(target_flags, dict) else ""
        if not target_color_base_raw and target_idx != -1 and isinstance(metadata[target_idx], dict):
            target_color_base_raw = _extract_color_base_value(metadata[target_idx])
        target_color_base = _normalize_text(target_color_base_raw) if target_color_base_raw else ""

        if isinstance(query_hint, str) and query_hint.strip():
            results = execute_catalog_search_loose(query_hint, k=max(limit * 4, 20), target_occasions=target_occ)
            collected = []
            seen_ids = set()
            secondary = []
            secondary_ids = set()
            for item in results:
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid == str(item_id) or sid in seen_ids:
                    continue
                if target_color_base:
                    item_base = _normalize_text(_extract_color_base_value(item) or "")
                    if not item_base or item_base != target_color_base:
                        if sid not in secondary_ids:
                            secondary.append(item)
                            secondary_ids.add(sid)
                        continue
                seen_ids.add(sid)
                collected.append(item)
                if len(collected) >= limit:
                    break
            if target_color_base and len(collected) < limit and secondary:
                for item in secondary:
                    sid = str(item.get("item_id") or item.get("custom_id") or "")
                    if not sid or sid in seen_ids:
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
            target_occ = _get_occasion_slugs(target_flags) if isinstance(target_flags, dict) else []
            if not target_occ:
                mf = (metadata[target_idx].get("metadata_filters") or {}) if isinstance(metadata[target_idx], dict) else {}
                occ = mf.get("occasions")
                if isinstance(occ, list):
                    target_occ = [o for o in (_canonical_occasion(x) for x in occ) if o]
            target_set = set(target_occ)

        if not target_color_base and isinstance(metadata[target_idx], dict):
            target_color_base_raw = _extract_color_base_value(metadata[target_idx])
            target_color_base = _normalize_text(target_color_base_raw) if target_color_base_raw else ""

        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0

        if ntotal <= 1:
            return jsonify({"suggestions": []})

        collected = []
        seen_ids = set()
        secondary = []
        secondary_ids = set()
        processed_k = 0
        k = min(max(50, limit * 20), ntotal)

        while len(collected) < limit and processed_k < ntotal:
            distances, indices = index.search(query_vector, k)
            new_positions = indices[0][processed_k:k]
            processed_k = k

            raw_candidates = []
            for idx in new_positions:
                if idx == -1 or idx == target_idx:
                    continue
                raw_candidates.append(metadata[int(idx)].copy())

            valid_candidates = validate_and_enrich_candidates(raw_candidates)

            for item in valid_candidates:
                if target_set:
                    occ_slugs = _get_occasion_slugs(item)
                    if not occ_slugs or not (set(occ_slugs) & target_set):
                        continue
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid in seen_ids:
                    continue
                if target_color_base:
                    item_base = _normalize_text(_extract_color_base_value(item) or "")
                    if not item_base or item_base != target_color_base:
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

        if target_color_base and len(collected) < limit and secondary:
            for item in secondary:
                sid = str(item.get("item_id") or item.get("custom_id") or "")
                if not sid or sid in seen_ids:
                    continue
                seen_ids.add(sid)
                collected.append(item)
                if len(collected) >= limit:
                    break

        return jsonify({"suggestions": _build_suggestions(collected)})

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
    limit = int(data.get('limit', 8))
    if limit < 8:
        limit = 8
    
    if not query:
        return jsonify({"error": "Query vazia"}), 400

    try:
        target_occasion = (data.get("occasion") or data.get("category") or "").lower().strip()
        cor_base = []
        cor_comercial = []
        query_for_faiss = query
        search_occasions = target_occasion or None

        account_id = session.get("account_id") if session else None
        if not account_id:
            account_id = _pick_public_account_id()
        try:
            base_options, commercial_options = _color_enums_for_account()
        except Exception:
            fallback = _load_color_options_for_account(account_id) if account_id else []
            base_options = list(dict.fromkeys([str(x).strip() for x in fallback if str(x).strip()]))
            commercial_options = list(base_options)

        base_norm = {_normalize_text(v): v for v in (base_options or []) if str(v).strip()}
        commercial_norm = {_normalize_text(v): v for v in (commercial_options or []) if str(v).strip()}

        query_tokens = [t for t in str(query or "").replace("\n", " ").split(" ") if t.strip()]
        joined = " ".join(query_tokens[:2]).strip()
        joined_norm = _normalize_text(joined)
        if joined_norm and joined_norm in commercial_norm:
            cor_comercial = [commercial_norm[joined_norm]]
        elif joined_norm and joined_norm in base_norm:
            cor_base = [base_norm[joined_norm]]
        else:
            extracted = _extract_catalog_filters_with_tool(query, target_occasion)
            if extracted:
                cor_base = [str(x).strip() for x in _ensure_list(extracted.get("cor_base")) if str(x).strip()]
                cor_comercial = [str(x).strip() for x in _ensure_list(extracted.get("cor_comercial")) if str(x).strip()]
                other_characteristics = extracted.get("other_characteristics") or ""
                if isinstance(other_characteristics, str) and other_characteristics.strip():
                    query_for_faiss = other_characteristics.strip()
                extracted_occs = extracted.get("occasions") or []
                if not search_occasions and extracted_occs:
                    search_occasions = extracted_occs

        if cor_comercial and not cor_base:
            color_pairs = _load_color_pairs_for_account(account_id) if account_id else []
            commercial_to_base = {}
            for pair in color_pairs or []:
                base = str(pair.get("base") or "").strip()
                name = str(pair.get("name") or "").strip()
                if base and name:
                    commercial_to_base[_normalize_text(name)] = base
            mapped_base = commercial_to_base.get(_normalize_text(cor_comercial[0]))
            if mapped_base and _normalize_text(mapped_base) in base_norm:
                cor_base = [base_norm[_normalize_text(mapped_base)]]

        coerced = _coerce_similarity_color_fields({"cor_base": cor_base, "cor_comercial": cor_comercial})
        cor_base = coerced.get("cor_base") or []
        cor_comercial = coerced.get("cor_comercial") or []

        def _extract_color_value(obj):
            v = obj.get("cor") or obj.get("cores") or obj.get("color") or obj.get("colors")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _extract_color_base_value(obj):
            v = obj.get("cor_base") or obj.get("color_base")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _extract_color_commercial_value(obj):
            v = obj.get("cor_comercial") or obj.get("color_comercial")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        def _extract_size_value(obj):
            v = obj.get("tamanho") or obj.get("size") or obj.get("item_tamanho") or obj.get("item_size") or obj.get("sizes")
            if isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        return x.strip()
                return ""
            if isinstance(v, str):
                return v.strip()
            return ""

        ntotal = int(getattr(index, "ntotal", 0) or 0)
        if ntotal <= 0:
            ntotal = len(metadata) if isinstance(metadata, list) else 0
        if ntotal <= 0:
            return jsonify({"results": [], "page": 1, "total_pages": 0})

        valid_results = search_and_prioritize(
            index=index,
            metadata=metadata,
            query=query_for_faiss,
            occasions=search_occasions,
            cor_comercial=cor_comercial or None,
            cor_base=cor_base or None,
            top_k=ntotal,
        )

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
                "price": item.get('price', "Valor do aluguel: Consulte"),
                "customId": item.get('customId'), # Adicionado para consistência
                "category": item.get('category', 'Outros'),
                "color": _extract_color_value(item),
                "color_base": _extract_color_base_value(item),
                "color_comercial": _extract_color_commercial_value(item),
                "size": _extract_size_value(item),
                "occasions": _get_occasions_list(item)
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

@ai_bp.route('/api/admin/create-occasion-gsis', methods=['POST'])
def create_occasion_gsis():
    try:
        if not session.get("logged_in"):
            return jsonify({"error": "not_authorized"}), 403
        data = request.get_json(silent=True) or {}
        table_name = data.get("table_name") or "alugueqqc_itens"
        slugs = data.get("slugs") or ["madrinha","formatura","gala","debutante","convidada","mae_dos_noivos","noiva","civil"]
        client = dynamodb.meta.client
        desc = client.describe_table(TableName=table_name)
        table = desc.get("Table", {})
        existing = set((idx.get("IndexName") or "") for idx in (table.get("GlobalSecondaryIndexes") or []))
        billing = (table.get("BillingModeSummary") or {}).get("BillingMode") or "PROVISIONED"
        to_create = []
        attr_defs = {}
        attr_defs["account_id"] = "S"
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
        attribute_definitions = [{"AttributeName": name, "AttributeType": t} for name, t in attr_defs.items()]
        client.update_table(
            TableName=table_name,
            AttributeDefinitions=attribute_definitions,
            GlobalSecondaryIndexUpdates=to_create,
        )
        return jsonify({"status": "started", "created": [d["Create"]["IndexName"] for d in to_create]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
