import sys
import json
import numpy as np
from ai_routes import (
    load_resources,
    _rewrite_catalog_query,
    _query_embedding_text_from_rewrite,
    validate_and_enrich_candidates,
    get_embedding,
    _category_slug,
    _apply_facet_constraints,
    _rerank_by_facets,
    index,
    metadata,
)

def fmt_item(m):
    mf = m.get("metadata_filters") or {}
    return {
        "id": m.get("custom_id"),
        "title": m.get("title"),
        "cat": m.get("category_slug") or m.get("category"),
        "colors": mf.get("colors"),
        "silhouette": mf.get("silhouette"),
        "neckline": mf.get("neckline"),
    }

def run(query, k=5):
    load_resources()
    rewrite = _rewrite_catalog_query(query, "")
    q_text = _query_embedding_text_from_rewrite(rewrite) or rewrite.get("query_reescrita") or query
    emb = get_embedding(q_text)
    vec = np.array([emb]).astype("float32")
    distances, indices = index.search(vec, 220)
    raw = []
    for idx in indices[0]:
        if idx != -1:
            raw.append(metadata[idx].copy())

    print("QUERY:", query)
    print("Q_TEXT:", q_text)
    print("REWRITE:", json.dumps(rewrite, ensure_ascii=False))
    print("RAW_TOP:", json.dumps([fmt_item(m) for m in raw[:10]], ensure_ascii=False))

    valid = validate_and_enrich_candidates(raw)
    cat = str(rewrite.get("categoria_detectada", "")).lower().strip()
    if cat in ["noiva", "festa"]:
        valid = [c for c in valid if _category_slug(c) == cat]

    constrained = _apply_facet_constraints(valid, rewrite)
    ranked = _rerank_by_facets(constrained, rewrite) if constrained else valid

    print("FINAL_TOP:", json.dumps([fmt_item(m) for m in ranked[:k]], ensure_ascii=False))

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "vestido de festa sereia preto"
    run(q, k=5)
