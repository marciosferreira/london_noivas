import argparse
import json

from ai_routes import load_resources, inventory_digest, _canonical_occasion, _rewrite_catalog_query


def _build_digest_for_prompt(digest):
    digest_for_prompt = {}
    for cat in ["noiva", "festa"]:
        by_cat = digest.get(cat)
        if not isinstance(by_cat, dict):
            continue
        for facet, values in by_cat.items():
            if not isinstance(values, list):
                continue
            digest_for_prompt.setdefault(facet, [])
            digest_for_prompt[facet].extend([str(v) for v in values if str(v).strip()])
    for facet, values in list(digest_for_prompt.items()):
        digest_for_prompt[facet] = list(dict.fromkeys(values))[:32]
    return digest_for_prompt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.add_argument("--query_u", default="")
    parser.add_argument("--occasion", default="")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    load_resources()

    query = (args.query or "").strip()
    if not query:
        query = (args.query_u or "").replace("_", " ").strip()
    if not query:
        query = "Serei Mãe da Noiva, pode me sugerir vestidos?"

    digest_obj = inventory_digest if isinstance(inventory_digest, dict) else {}
    digest_for_prompt = _build_digest_for_prompt(digest_obj)
    target_slug = _canonical_occasion(args.occasion)

    user_payload = {
        "ocasiao_alvo": target_slug,
        "inventario_contexto_exemplos": digest_for_prompt,
        "consulta_usuario": query,
        "saida": {
            "query_reescrita": "string",
            "atributos_extraidos": "obj",
            "atributos_novos": "lista",
            "termos_excluir": "lista",
        },
    }

    print("=== inventory_digest ===")
    if args.full:
        print(json.dumps(digest_obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(digest_obj, ensure_ascii=False, indent=2)[:4000])
        print("... (truncado) ...")

    print("\n=== payload enviado ao rewrite (user message) ===")
    if args.full:
        print(json.dumps(user_payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(user_payload, ensure_ascii=False, indent=2)[:4000])
        print("... (truncado) ...")

    print("\n=== rewrite result ===")
    rewritten = _rewrite_catalog_query(query, args.occasion)
    print(json.dumps(rewritten, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
