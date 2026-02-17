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
import argparse
import faiss
from botocore.exceptions import ClientError
from dotenv import load_dotenv

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

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRESSES_DIR = os.path.join(BASE_DIR, 'static', 'dresses')
DATASET_FILE = os.path.join(BASE_DIR, 'embeddings_creation', 'vestidos_dataset.jsonl')
METADATA_FILE = os.path.join(BASE_DIR, 'vector_store_metadata.pkl')
INDEX_FILE = os.path.join(BASE_DIR, 'vector_store.index')
CREATE_VECTOR_SCRIPT = os.path.join(BASE_DIR, 'embeddings_creation', 'create_vector_store.py')
SKILL_FILE = os.path.join(BASE_DIR, '.trae', 'skills', 'bella-search-mcp', 'SKILL.md')
DEFAULT_AI_SYNC_ACCOUNT_ID = "37d5b37f-c920-4090-a682-7e1ed2e31a0f"

# AWS & OpenAI
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
itens_table = dynamodb.Table('alugueqqc_itens')
users_table = dynamodb.Table('alugueqqc_users')
client = _build_openai_client()

PROGRESS = {"running": False, "total": 0, "processed": 0, "current": None, "eta_seconds": None, "start_time": None, "done": False, "message": None, "error": None}

def _progress_start(total):
    PROGRESS.update({"running": True, "total": int(total or 0), "processed": 0, "current": None, "eta_seconds": None, "start_time": time.time(), "done": False, "message": None, "error": None})

def _progress_update(processed, current, eta):
    PROGRESS.update({"processed": int(processed or 0), "current": str(current or ""), "eta_seconds": int(eta or 0)})

def _progress_finish(message=None, error=None):
    PROGRESS.update({"running": False, "done": True, "message": message, "error": error})

def get_progress():
    return PROGRESS.copy()

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

def _load_existing_data():
    existing_data = []
    if os.path.exists(DATASET_FILE):
        try:
            with open(DATASET_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            existing_data.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as e:
            print(f"Erro ao ler dataset JSONL: {e}")
            existing_data = []
    elif os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'rb') as f:
                meta_list = pickle.load(f)
            if isinstance(meta_list, list):
                existing_data = [m for m in meta_list if isinstance(m, dict)]
        except Exception as e:
            print(f"Erro ao ler metadata como fallback: {e}")
            existing_data = []
    return existing_data

def _build_tool_context(existing_data, limit_per_facet=40):
    facet_keys = ["occasions", "colors", "fabrics", "silhouette", "neckline", "sleeves", "details"]
    counts = {k: {} for k in facet_keys}
    for entry in existing_data or []:
        if not isinstance(entry, dict):
            continue
        mf = entry.get("metadata_filters")
        if not isinstance(mf, dict):
            continue
        for k in facet_keys:
            values = mf.get(k)
            if not isinstance(values, list):
                continue
            for v in values:
                vv = str(v).strip()
                if not vv:
                    continue
                bucket = counts.get(k)
                if bucket is None:
                    continue
                bucket[vv] = bucket.get(vv, 0) + 1
    out = {}
    for k in facet_keys:
        items = sorted(counts[k].items(), key=lambda x: (-x[1], x[0]))
        out[k] = [v for v, _ in items[:limit_per_facet]]
    return out

def _infer_account_id(existing_data):
    counts = {}
    for entry in existing_data or []:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("account_id") or entry.get("item_account_id") or entry.get("accountId")
        account_id = str(account_id or "").strip()
        if not account_id:
            continue
        counts[account_id] = counts.get(account_id, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[0][0]

def _update_skill_context(context):
    if not os.path.exists(SKILL_FILE):
        return
    try:
        with open(SKILL_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Erro ao ler SKILL.md: {e}")
        return
    rendered = "## Contexto Dinâmico\n\n```json\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n```\n"
    if "## Contexto Dinâmico" in content:
        updated = re.sub(r"## Contexto Dinâmico[\s\S]*?(?=\n## |\Z)", rendered, content)
    else:
        updated = content.rstrip() + "\n\n" + rendered
    try:
        with open(SKILL_FILE, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        print(f"Erro ao escrever SKILL.md: {e}")

def _to_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []

def _normalize_occasions_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    out = []
    seen = set()
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
                label = "Gala"
            elif ("mae" in pl or "mãe" in pl) and "noiv" in pl:
                label = "Mãe dos Noivos"
            elif "noiv" in pl:
                label = "Noiva"
            elif "civil" in pl:
                label = "Civil"
            elif "madrinha" in pl:
                label = "Madrinha"
            elif "formatura" in pl:
                label = "Formatura"
            elif "debut" in pl or "15 anos" in pl:
                label = "Debutante"
            elif "convid" in pl:
                label = "Convidada"
            else:
                label = p[:1].upper() + p[1:]

            key = _normalize_text(label)
            if key and key not in seen:
                seen.add(key)
                out.append(label)

    return out

def _metadata_filters_from_structured(structured):
    if not isinstance(structured, dict):
        return None

    occasions_value = structured.get("occasions")
    if occasions_value is None:
        occasions_value = structured.get("ocasiao")

    cor_comercial = _to_list(structured.get("cor_comercial"))
    cor_base = _to_list(structured.get("cor_base"))
    colors = _clean_unique_strings(cor_comercial + cor_base)
    filters = {
        "cor_comercial": _clean_unique_strings(cor_comercial),
        "cor_base": _clean_unique_strings(cor_base),
        "colors": colors,
        "fabrics": _to_list(structured.get("tecido")),
        "silhouette": _to_list(structured.get("estilo")),
        "neckline": _to_list(structured.get("decote")),
        "sleeves": _to_list(structured.get("mangas")),
        "details": _to_list(structured.get("detalhes")),
    }

    filters["occasions"] = _normalize_occasions_list(occasions_value)

    return {k: v for k, v in filters.items() if v}

def _default_occasion_options():
    return [
        "Noiva",
        "Civil",
        "Madrinha",
        "Mãe dos Noivos",
        "Formatura",
        "Debutante",
        "Gala",
        "Convidada",
    ]

def _pick_ai_sync_account_id():
    raw = os.getenv("AI_SYNC_ACCOUNT_ID") or os.getenv("PUBLIC_CATALOG_ACCOUNT_IDS") or ""
    raw = str(raw or "").strip()
    if not raw:
        return DEFAULT_AI_SYNC_ACCOUNT_ID
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts[0] if parts else None

def _clean_unique_strings(values):
    out = []
    seen = set()
    for v in values or []:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out

def _split_multi_values(value):
    if value is None:
        return []
    if isinstance(value, list):
        parts = []
        for v in value:
            parts.extend(_split_multi_values(v))
        return parts
    s = str(value).strip()
    if not s:
        return []
    chunks = re.split(r"[/,]|\s+e\s+", s)
    return [c.strip() for c in chunks if c and str(c).strip()]

def _normalize_color_entry(entry):
    if entry is None:
        return None
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("color") or entry.get("comercial") or entry.get("cor")
        base = entry.get("base") or entry.get("cor_base") or entry.get("base_color")
        name = str(name).strip() if name else ""
        base = str(base).strip() if base else ""
        if not name:
            return None
        return {"name": name, "base": base}
    name = str(entry).strip()
    if not name:
        return None
    return {"name": name, "base": ""}

def _load_account_settings_options(account_id):
    if not account_id:
        return {"colors": [], "sizes": []}
    try:
        resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
        item = resp.get("Item") or {}
    except Exception:
        item = {}
    colors = item.get("color_options")
    sizes = item.get("size_options")
    colors = colors if isinstance(colors, list) else []
    sizes = sizes if isinstance(sizes, list) else []
    cleaned_pairs = []
    seen_pairs = set()
    for c in colors:
        entry = _normalize_color_entry(c)
        if not entry:
            continue
        key = (entry["base"].casefold(), entry["name"].casefold())
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        cleaned_pairs.append(entry)
    cleaned_colors = [entry["name"] for entry in cleaned_pairs if entry.get("name")]
    return {
        "colors": sorted(_clean_unique_strings(cleaned_colors), key=lambda x: x.casefold()),
        "color_pairs": sorted(cleaned_pairs, key=lambda x: (x["base"].casefold(), x["name"].casefold())),
        "sizes": sorted(_clean_unique_strings(sizes), key=lambda x: x.casefold()),
    }

def _scan_items_for_color_and_size_options(max_items=2500):
    colors = []
    cor_base = []
    cor_comercial = []
    sizes = []

    scan_kwargs = {
        "ProjectionExpression": "cor, cores, cor_base, cor_comercial, tamanho, #st",
        "ExpressionAttributeNames": {"#st": "status"},
    }
    resp = itens_table.scan(**scan_kwargs)
    processed = 0

    while True:
        for item in resp.get("Items", []) or []:
            status = str(item.get("status") or "").lower().strip()
            if status in ["deleted", "archived", "inactive"]:
                continue

            colors.extend(_split_multi_values(item.get("cor")))
            colors.extend(_split_multi_values(item.get("cores")))
            cor_base.extend(_split_multi_values(item.get("cor_base")))
            cor_comercial.extend(_split_multi_values(item.get("cor_comercial")))
            sizes.extend(_split_multi_values(item.get("tamanho")))

            processed += 1
            if max_items and processed >= max_items:
                break

        if max_items and processed >= max_items:
            break
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        resp = itens_table.scan(ExclusiveStartKey=last, **scan_kwargs)

    return {
        "colors": sorted(_clean_unique_strings(colors), key=lambda x: x.casefold()),
        "cor_base": sorted(_clean_unique_strings(cor_base), key=lambda x: x.casefold()),
        "cor_comercial": sorted(_clean_unique_strings(cor_comercial), key=lambda x: x.casefold()),
        "sizes": sorted(_clean_unique_strings(sizes), key=lambda x: x.casefold()),
    }

def _occasions_from_db_item(item):
    if not isinstance(item, dict):
        return []
    mapping = [
        ("occasion_noiva", "Noiva"),
        ("occasion_civil", "Civil"),
        ("occasion_madrinha", "Madrinha"),
        ("occasion_mae_dos_noivos", "Mãe dos Noivos"),
        ("occasion_formatura", "Formatura"),
        ("occasion_debutante", "Debutante"),
        ("occasion_gala", "Gala"),
        ("occasion_convidada", "Convidada"),
    ]
    out = []
    for key, label in mapping:
        v = item.get(key)
        if v == "1" or v == 1 or v is True:
            out.append(label)
    return out

def _merge_occasions(metadata_filters, db_occasions):
    if not isinstance(metadata_filters, dict):
        return metadata_filters

    existing = metadata_filters.get("occasions")
    if not isinstance(existing, list):
        existing = []

    db_norm = _normalize_occasions_list(db_occasions)
    if db_norm:
        merged = db_norm
    else:
        merged = _normalize_occasions_list(existing)

    if merged:
        metadata_filters["occasions"] = merged
    else:
        metadata_filters.pop("occasions", None)
    return metadata_filters

def _colors_from_db_item(item):
    if not isinstance(item, dict):
        return []
    values = []
    for key in ["cor_comercial", "cor_base", "cores", "color", "item_cor", "item_color"]:
        values.extend(_split_multi_values(item.get(key)))
    return sorted(_clean_unique_strings(values), key=lambda x: x.casefold())

def _merge_colors(metadata_filters, db_colors):
    if not isinstance(metadata_filters, dict):
        return metadata_filters
    existing = metadata_filters.get("colors")
    if not isinstance(existing, list):
        existing = []
    merged = _clean_unique_strings(list(existing) + list(db_colors or []))
    if merged:
        metadata_filters["colors"] = merged
    else:
        metadata_filters.pop("colors", None)
    return metadata_filters

def _sizes_from_db_item(item):
    if not isinstance(item, dict):
        return []
    values = []
    for key in ["tamanho", "size", "item_tamanho", "item_size"]:
        values.extend(_split_multi_values(item.get(key)))
    return sorted(_clean_unique_strings(values), key=lambda x: x.casefold())

def _format_db_multi_value(values):
    if values is None:
        return None
    if isinstance(values, list):
        cleaned = _clean_unique_strings(values)
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return cleaned[0]
        return ", ".join(cleaned)
    s = str(values).strip()
    return s if s else None

def _update_db_fields(item_id, description, title, metadata_filters):
    updates = []
    values = {}

    if description is not None:
        updates.append("description = :d")
        updates.append("item_description = :d")
        values[":d"] = description or ""
    if title is not None:
        updates.append("title = :t")
        updates.append("item_title = :t")
        values[":t"] = title or ""

    if isinstance(metadata_filters, dict):
        cor_base = _format_db_multi_value(metadata_filters.get("cor_base"))
        cor_comercial = _format_db_multi_value(metadata_filters.get("cor_comercial"))
        if cor_base:
            updates.append("cor_base = :cb")
            values[":cb"] = cor_base
        if cor_comercial:
            updates.append("cor_comercial = :cc")
            updates.append("cor = :cc")
            values[":cc"] = cor_comercial

        occs = _normalize_occasions_list(metadata_filters.get("occasions"))
        if occs:
            values[":one"] = "1"
            mapping = [
                ("Noiva", "occasion_noiva"),
                ("Civil", "occasion_civil"),
                ("Madrinha", "occasion_madrinha"),
                ("Mãe dos Noivos", "occasion_mae_dos_noivos"),
                ("Formatura", "occasion_formatura"),
                ("Debutante", "occasion_debutante"),
                ("Gala", "occasion_gala"),
                ("Convidada", "occasion_convidada"),
            ]
            occ_set = set(occs)
            for label, key in mapping:
                if label in occ_set:
                    updates.append(f"{key} = :one")

    if not updates:
        return

    try:
        itens_table.update_item(
            Key={'item_id': item_id},
            UpdateExpression="SET " + ", ".join(updates),
            ExpressionAttributeValues=values,
        )
    except Exception as e:
        print(f"Erro ao atualizar campos do item {item_id}: {e}")

def backfill_db_from_jsonl(jsonl_path=None, dry_run=False, limit=None):
    path = jsonl_path or DATASET_FILE
    if not os.path.exists(path):
        print(f"Arquivo não encontrado: {path}")
        return {"status": "error", "message": "arquivo não encontrado"}

    total = 0
    missing = 0
    skipped = 0
    updated = 0
    errors = 0
    occ_map = [
        ("Noiva", "occasion_noiva"),
        ("Civil", "occasion_civil"),
        ("Madrinha", "occasion_madrinha"),
        ("Mãe dos Noivos", "occasion_mae_dos_noivos"),
        ("Formatura", "occasion_formatura"),
        ("Debutante", "occasion_debutante"),
        ("Gala", "occasion_gala"),
        ("Convidada", "occasion_convidada"),
    ]

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if limit and total >= limit:
                break
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            total += 1
            item_id = str(data.get("custom_id") or data.get("item_id") or "").strip()
            if not item_id:
                skipped += 1
                continue
            try:
                resp = itens_table.get_item(Key={"item_id": item_id})
                item = resp.get("Item")
            except Exception as e:
                print(f"Erro ao buscar item {item_id}: {e}")
                errors += 1
                continue
            if not item:
                missing += 1
                continue

            updates = []
            values = {}

            def add_update(field, value):
                key = f":v{len(values)}"
                updates.append(f"{field} = {key}")
                values[key] = value

            description = data.get("description")
            title = data.get("title")
            if description is not None:
                add_update("description", description or "")
            if description is not None:
                add_update("item_description", description or "")
            if title is not None:
                add_update("title", title or "")
            if title is not None:
                add_update("item_title", title or "")

            metadata_filters = data.get("metadata_filters")
            if isinstance(metadata_filters, dict):
                cor_base = _format_db_multi_value(metadata_filters.get("cor_base"))
                cor_comercial = _format_db_multi_value(metadata_filters.get("cor_comercial"))
                if cor_base:
                    add_update("cor_base", cor_base)
                if cor_comercial:
                    add_update("cor_comercial", cor_comercial)
                if cor_comercial:
                    add_update("cor", cor_comercial)

                occs = _normalize_occasions_list(metadata_filters.get("occasions"))
                if occs:
                    occ_set = set(occs)
                    for label, key in occ_map:
                        if label in occ_set:
                            add_update(key, "1")

            if not updates:
                skipped += 1
                continue

            if dry_run:
                updated += 1
                continue

            try:
                itens_table.update_item(
                    Key={"item_id": item_id},
                    UpdateExpression="SET " + ", ".join(updates),
                    ExpressionAttributeValues=values,
                )
                updated += 1
            except Exception as e:
                print(f"Erro ao atualizar item {item_id}: {e}")
                errors += 1

    summary = {
        "status": "success",
        "total": total,
        "updated": updated,
        "missing": missing,
        "skipped": skipped,
        "errors": errors,
        "dry_run": bool(dry_run),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=DATASET_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    backfill_db_from_jsonl(args.jsonl, dry_run=args.dry_run, limit=args.limit or None)

def _build_embedding_text_from_item(metadata_filters, title, description, item_obs, sizes):
    parts = []
    if isinstance(metadata_filters, dict):
        for k in ["silhouette", "neckline", "fabrics", "details", "cor_base", "cor_comercial", "colors", "sleeves", "occasions"]:
            vals = metadata_filters.get(k)
            if isinstance(vals, list):
                parts.extend([str(v).strip() for v in vals if str(v).strip()])
    if sizes:
        parts.extend([str(v).strip() for v in sizes if str(v).strip()])
    if title:
        parts.append(str(title).strip())
    if description:
        parts.append(str(description).strip())
    if item_obs:
        parts.append(str(item_obs).strip())
    seen = set()
    cleaned = []
    for p in parts:
        q = _normalize_text(p)
        if q and q not in seen:
            seen.add(q)
            cleaned.append(p)
    return " ".join(cleaned)[:400]

def _build_inventory_examples(existing_data, limit_per_facet=8):
    vocab = {
        "tecido": ["Zibelina", "Renda", "Tule", "Cetim", "Seda", "Crepe", "Chiffon", "Organza"],
        "estilo": ["Sereia", "Princesa", "Evasê", "Reto", "Boho Chic", "Minimalista", "Clássico", "Moderno"],
        "decote": ["Tomara que caia", "Decote em V", "Canoa", "Ombro a ombro", "Frente única", "Coração"],
        "mangas": ["Sem mangas", "Manga curta", "Manga longa", "Alça fina", "Um ombro só"],
        "detalhes": ["Brilho", "Pedraria", "Bordado", "Fenda", "Costas abertas", "Cauda longa", "Transparência", "Drapeado"],
    }

    tool_ctx = _build_tool_context(existing_data or [], limit_per_facet=max(8, int(limit_per_facet or 0)))

    account_id = _pick_ai_sync_account_id() or _infer_account_id(existing_data)
    settings = _load_account_settings_options(account_id)
    if account_id:
        options = settings
        if not options.get("sizes"):
            fallback = _scan_items_for_color_and_size_options(max_items=2500)
            options = {**options, "sizes": fallback.get("sizes", [])}
    else:
        fallback = _scan_items_for_color_and_size_options(max_items=2500)
        options = {
            "colors": [],
            "color_pairs": [],
            "sizes": fallback.get("sizes", []),
        }

    dynamic_occasions = tool_ctx.get("occasions") if isinstance(tool_ctx, dict) else []
    dynamic_occasions = dynamic_occasions if isinstance(dynamic_occasions, list) else []
    occasion_options = _clean_unique_strings(dynamic_occasions) or _default_occasion_options()
    colors_options = options.get("colors") or []
    color_pairs = options.get("color_pairs") or []
    sizes_options = options.get("sizes") or []
    cor_comercial = [str(c.get("name") or "").strip() for c in color_pairs if str(c.get("name") or "").strip()]
    cor_base = [str(c.get("base") or "").strip() for c in color_pairs if str(c.get("base") or "").strip()]
    if not cor_comercial:
        cor_comercial = colors_options
    if not cor_base:
        cor_base = colors_options
    return {
        "opcoes_validas": {
            "occasions": occasion_options,
            "cor_comercial": _clean_unique_strings(cor_comercial)[:80],
            "cor_base": _clean_unique_strings(cor_base)[:80],
        },
        "exemplos_vocabulario": vocab,
    }

def _extract_color_options_from_inventory_examples(inventory_examples):
    if not isinstance(inventory_examples, dict):
        return []
    options = inventory_examples.get("opcoes_validas")
    if not isinstance(options, dict):
        return []
    colors = []
    for key in ["cor_comercial", "cor_base"]:
        values = options.get(key)
        if isinstance(values, list):
            colors.extend(values)
    return _clean_unique_strings(colors)

def _extract_metadata_filters(description, color_options=None):
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

    colors = []
    for c in color_options or []:
        name = str(c).strip()
        if name:
            colors.append((name, [name]))

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

    filters["occasions"] = _normalize_occasions_list(collect(occasions))

    return {k: v for k, v in filters.items() if v}

def _synthesize_embedding_text(metadata_filters, title):
    parts = []
    if isinstance(metadata_filters, dict):
        for k in ["silhouette", "neckline", "fabrics", "details", "cor_base", "cor_comercial", "colors", "sleeves", "occasions"]:
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

            system_prompt = (
                "Você descreve vestidos para um catálogo de busca.\n"
                "Responda apenas com JSON válido (sem markdown).\n"
                "Não inclua tamanho, manequim, medidas, altura, peso, ou suposições de tamanho.\n"
                "Não inclua preço.\n"
                "Use português do Brasil."
            )
            user_text = (
                "Analise a imagem e retorne um JSON com este schema:\n"
                "{\n"
                "  \"descricao\": \"string (máx 70 palavras, objetiva, para busca)\",\n"
                "  \"titulo\": \"string (máx 5 palavras)\",\n"
                "  \"occasions\": [\"...\"] (multi-label; pode ter 0, 1 ou várias; exemplos: Noiva, Civil, Madrinha, Formatura, Debutante, Gala, Convidada, Mãe dos Noivos),\n"
                "  \"cor_comercial\": [\"...\"],\n"
                "  \"cor_base\": [\"...\"],\n"
                "  \"tecido\": [\"...\"],\n"
                "  \"estilo\": [\"...\"],\n"
                "  \"decote\": [\"...\"],\n"
                "  \"mangas\": [\"...\"],\n"
                "  \"detalhes\": [\"...\"],\n"
                "  \"keywords\": [\"...\"]\n"
                "}\n"
                "Contexto do catálogo (use como vocabulário; quando possível, escolha exatamente das opções):\n"
                f"{json.dumps(inventory_examples or {}, ensure_ascii=False)}\n"
                "Regras:\n"
                "- As listas de exemplos em exemplos_vocabulario são apenas ilustrativas; não são opções válidas.\n"
                "- 'cor_comercial' é o nome popular da cor usado no mercado da moda; escolha apenas opções válidas da lista opcoes_validas.cor_comercial.\n"
                "- 'cor_base' é a categoria base da cor; escolha apenas opções válidas da lista opcoes_validas.cor_base.\n"
                "- Se não houver correspondência clara, retorne lista vazia em cor_comercial e cor_base.\n"
                "- Para 'occasions', use apenas opções válidas quando houver correspondência clara; caso contrário, lista vazia.\n"
                "- Não retorne 'tamanho' (ele não faz parte do schema).\n"
                "Se algum campo não for possível inferir com segurança, retorne lista vazia.\n"
                "Regras de classificação (occasions):\n"
                "- Formatura: brilho intenso, fendas, recortes, cores vibrantes, sexy/moderno.\n"
                "- Madrinha: elegante/romântico, tons pastéis/terrosos, sem protagonismo excessivo.\n"
                "- Gala: luxo, estruturado, cauda, bordado pesado, red carpet/black tie.\n"
                "- Debutante: princesa volumoso ou balada curto/brilho.\n"
                "- Mãe dos Noivos: sofisticado, mais cobertura, renda nobre, cores clássicas.\n"
                "- Convidada: bonito mas menos protagonista.\n"
                "- Noiva e Civil podem coexistir (um mesmo vestido pode servir para ambos).\n"
            )
            print("PROMPT_SYSTEM_VISAO:\n" + system_prompt)
            print("PROMPT_USER_VISAO:\n" + user_text)
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_text,
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
    existing_data = _load_existing_data()

    deleted_ids_set = set(deleted_ids)
    inventory_examples = _build_inventory_examples(
        [d for d in existing_data if d.get('custom_id') not in deleted_ids_set],
        limit_per_facet=8,
    )
    dynamic_color_options = _extract_color_options_from_inventory_examples(inventory_examples)
    
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
    items_to_process = list(set(missing_ids) | set(pending_ids))  # não inclui pending_remove
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
            
            ext = 'jpg'
            if '.' in image_url:
                parts = image_url.split('.')
                if len(parts) > 1:
                    ext = parts[-1].split('?')[0]
                    if len(ext) > 4: ext = 'jpg' # Fallback
            
            # Mantemos o nome do arquivo no JSON para referência (caso precise no futuro)
            # mas NÃO salvamos o arquivo no disco.
            filename = f"{item_id}.{ext}"

            existing_desc = item.get("description") or item.get("item_description")
            existing_title = item.get("title") or item.get("item_title")
            processed_so_far += 1
            avg = (time.time() - start_time) / max(1, processed_so_far)
            eta = int(max(0, (total_items - processed_so_far) * avg))
            _progress_update(processed_so_far, item_id, eta)

            force_vision_item = str(item.get("embedding_force_vision") or "").strip().lower() in ["1", "true", "yes", "sim"]
            if item_id in pending_ids_set and not force_vision_item:
                print(f"[{processed_so_far}/{total_items}] Atualizando embedding sem visão: {item_id} (ETA ~{eta}s)")
                metadata_filters = _extract_metadata_filters(existing_desc, color_options=dynamic_color_options) if existing_desc else {}
                if not isinstance(metadata_filters, dict):
                    metadata_filters = {}
                metadata_filters = _merge_occasions(metadata_filters, _occasions_from_db_item(item))
                metadata_filters = _merge_colors(metadata_filters, _colors_from_db_item(item))
                sizes = _sizes_from_db_item(item)
                embedding_text = _build_embedding_text_from_item(
                    metadata_filters,
                    existing_title,
                    existing_desc,
                    item.get("item_obs"),
                    sizes,
                ) or existing_desc or existing_title or ""
                description = existing_desc or ""
                title = existing_title or ""
            else:
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

                print(f"[{processed_so_far}/{total_items}] Gerando metadados IA: {item_id}")
                description, title, structured = generate_dress_metadata(
                    image_bytes,
                    existing_desc,
                    existing_title,
                    inventory_examples=inventory_examples,
                    force_vision=force_regenerate,
                    copywriting=True,
                )
                
                metadata_filters = _metadata_filters_from_structured(structured) or _extract_metadata_filters(description, color_options=dynamic_color_options)
                metadata_filters = _merge_occasions(metadata_filters, _occasions_from_db_item(item))
                metadata_filters = _merge_colors(metadata_filters, _colors_from_db_item(item))
                embedding_text = _synthesize_embedding_text(metadata_filters, title) or description
            
            _update_db_fields(item_id, description, title, metadata_filters)

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
                        UpdateExpression="REMOVE embedding_status, embedding_force_vision"
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
                    entry["metadata_filters"] = _extract_metadata_filters(entry.get("description"), color_options=dynamic_color_options)
                if not entry.get("embedding_text"):
                    entry["embedding_text"] = _synthesize_embedding_text(entry.get("metadata_filters"), entry.get("title")) or entry.get("description")
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    try:
        tool_context = _build_tool_context(existing_data)
        _update_skill_context(tool_context)
    except Exception as e:
        print(f"Erro ao atualizar contexto do skill: {e}")

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
