import json
import os
import sys
import re
import unicodedata
import numpy as np
import faiss
from openai import OpenAI

def _normalize_text(text):
    text = str(text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join([c for c in text if not unicodedata.combining(c)])
    text = re.sub(r"\s+", " ", text).strip()
    return text

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

def _to_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []

def _get_index_paths():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    index_path = os.path.join(base_dir, "vector_store.index")
    metadata_path = os.path.join(base_dir, "vector_store_metadata.pkl")
    return index_path, metadata_path

def _load_metadata():
    _, metadata_path = _get_index_paths()
    with open(metadata_path, "rb") as f:
        import pickle
        return pickle.load(f)

def _match_filters(meta, filters):
    mf = meta.get("metadata_filters") or {}
    for key, desired in filters.items():
        if not desired:
            continue
        vals = mf.get(key)
        if not isinstance(vals, list):
            return False
        if key == "occasions":
            actual = set(_canonical_occasion(v) for v in vals if str(v).strip())
            actual = set(v for v in actual if v)
            wanted = set(_canonical_occasion(v) for v in desired if str(v).strip())
            wanted = set(v for v in wanted if v)
        else:
            actual = set(_normalize_text(v) for v in vals if str(v).strip())
            wanted = set(_normalize_text(v) for v in desired if str(v).strip())
        if not actual.intersection(wanted):
            return False
    return True

def _build_query_text(payload):
    query = str(payload.get("query") or "").strip()
    if query:
        return query
    parts = []
    for key in ["occasions", "colors", "fabrics", "silhouette", "neckline", "sleeves", "details"]:
        parts.extend(_to_list(payload.get(key)))
    return " ".join(parts).strip()

def _get_embedding(text):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    model = os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
    client = OpenAI(api_key=api_key)
    return client.embeddings.create(input=[text.replace("\n", " ")], model=model).data[0].embedding

def _read_payload():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)

def _main():
    payload = _read_payload()
    debug = os.getenv("BELLA_MCP_DEBUG") == "1"
    if debug:
        print(json.dumps({"debug": "input", "payload": payload}, ensure_ascii=False), file=sys.stderr)
    index_path, metadata_path = _get_index_paths()
    if not os.path.exists(index_path) or not os.path.exists(metadata_path):
        print(json.dumps({"error": "index or metadata not found"}))
        return
    index = faiss.read_index(index_path)
    metadata = _load_metadata()
    if not index or not metadata:
        print(json.dumps({"error": "index or metadata not found"}))
        return

    limit = int(payload.get("k") or 4)
    if limit < 1:
        limit = 1
    if limit > 24:
        limit = 24

    filters = {
        "occasions": _to_list(payload.get("occasions")),
        "colors": _to_list(payload.get("colors")),
        "fabrics": _to_list(payload.get("fabrics")),
        "silhouette": _to_list(payload.get("silhouette")),
        "neckline": _to_list(payload.get("neckline")),
        "sleeves": _to_list(payload.get("sleeves")),
        "details": _to_list(payload.get("details")),
    }

    query_text = _build_query_text(payload)
    if not query_text:
        print(json.dumps({"suggestions": []}))
        return

    emb = _get_embedding(query_text)
    vec = np.array([emb]).astype("float32")

    ntotal = int(getattr(index, "ntotal", 0) or 0)
    if ntotal <= 0:
        print(json.dumps({"suggestions": []}))
        return

    collected = []
    seen_ids = set()
    processed_k = 0
    search_k = min(max(80, limit * 20), ntotal)

    while len(collected) < limit and processed_k < ntotal:
        distances, indices = index.search(vec, search_k)
        new_positions = indices[0][processed_k:search_k]
        new_distances = distances[0][processed_k:search_k]
        processed_k = search_k

        for idx, dist in zip(new_positions, new_distances):
            if idx == -1:
                continue
            meta = metadata[int(idx)]
            cid = str(meta.get("custom_id") or "")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            if not _match_filters(meta, filters):
                continue
            collected.append({
                "custom_id": meta.get("custom_id"),
                "title": meta.get("title"),
                "description": meta.get("description"),
                "file_name": meta.get("file_name"),
                "metadata_filters": meta.get("metadata_filters"),
            })
            if len(collected) >= limit:
                break

        if len(collected) >= limit or processed_k >= ntotal:
            break
        search_k = min(ntotal, max(processed_k + 80, search_k * 2))

    output = {"suggestions": collected}
    if debug:
        print(json.dumps({"debug": "output", "payload": output}, ensure_ascii=False), file=sys.stderr)
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    _main()
