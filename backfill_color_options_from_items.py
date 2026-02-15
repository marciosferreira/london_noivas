import os
import json
import argparse
import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from decimal import Decimal


def _as_plain(obj):
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    if isinstance(obj, list):
        return [_as_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _as_plain(v) for k, v in obj.items()}
    return obj


def _extract_colors(item, attr_name):
    colors = []

    direct = item.get(attr_name)
    if isinstance(direct, list):
        colors.extend(direct)
    elif isinstance(direct, str):
        colors.append(direct)

    out = []
    seen = set()
    for c in colors:
        if c is None:
            continue
        s = str(c).strip()
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _settings_user_id(account_id):
    return f"account_settings:{account_id}"


def _merge_colors(existing, new):
    out = []
    seen = set()
    for src in [existing or [], new or []]:
        for c in src:
            if c is None:
                continue
            s = str(c).strip()
            if not s:
                continue
            k = s.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
    return out


def _has_any_color_value(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        for v in value:
            if v is None:
                continue
            if str(v).strip():
                return True
        return False
    return bool(str(value).strip())


def _auto_pick_colors_attr(itens_table, source_account_id, preferred_attr, max_probe):
    preferred_attr = str(preferred_attr or "").strip()
    if preferred_attr:
        return preferred_attr

    scan_kwargs = {
        "ProjectionExpression": "account_id, #st, cor, cores",
        "ExpressionAttributeNames": {"#st": "status"},
    }
    if source_account_id:
        scan_kwargs["FilterExpression"] = Attr("account_id").eq(source_account_id)

    hits = {"cor": 0, "cores": 0}
    processed = 0
    resp = itens_table.scan(**scan_kwargs)
    while True:
        for raw in resp.get("Items", []):
            item = _as_plain(raw)
            status = str(item.get("status") or "").lower().strip()
            if status in ["deleted", "archived", "inactive"]:
                continue
            processed += 1
            if _has_any_color_value(item.get("cor")):
                hits["cor"] += 1
            if _has_any_color_value(item.get("cores")):
                hits["cores"] += 1
            if processed >= max_probe:
                break
        if processed >= max_probe:
            break
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        resp = itens_table.scan(ExclusiveStartKey=last, **scan_kwargs)

    picked = "cores" if hits["cores"] > hits["cor"] else "cor"
    print(f"[INFO] Auto-detect coluna de cor: cor={hits['cor']} | cores={hits['cores']} | usando={picked}")
    return picked


def main():
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--items-table", default="alugueqqc_itens")
    parser.add_argument("--users-table", default="alugueqqc_users")
    parser.add_argument("--source-account-id", default=None)
    parser.add_argument("--account-id", dest="source_account_id", default=None)
    parser.add_argument("--target-account-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--inspect-fields", action="store_true")
    parser.add_argument("--item-colors-attr", default=None)
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--max-probe", type=int, default=200)
    args = parser.parse_args()

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=args.region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )

    itens_table = dynamodb.Table(args.items_table)
    users_table = dynamodb.Table(args.users_table)

    source_account_id = args.source_account_id
    item_colors_attr = _auto_pick_colors_attr(
        itens_table,
        source_account_id,
        args.item_colors_attr,
        args.max_probe,
    )

    scan_kwargs = {
        "ProjectionExpression": f"account_id, #st, {item_colors_attr}",
        "ExpressionAttributeNames": {"#st": "status"},
    }
    if source_account_id:
        scan_kwargs["FilterExpression"] = Attr("account_id").eq(source_account_id)

    source_colors = []
    processed = 0
    field_hits = {}
    resp = itens_table.scan(**scan_kwargs)
    while True:
        items = resp.get("Items", [])
        for raw in items:
            item = _as_plain(raw)
            processed += 1
            if args.max_items and processed > args.max_items:
                break

            status = str(item.get("status") or "").lower().strip()
            if status in ["deleted", "archived", "inactive"]:
                continue

            if args.inspect_fields:
                for k, v in (item or {}).items():
                    ks = str(k).casefold()
                    if "cor" in ks or "color" in ks:
                        field_hits[ks] = field_hits.get(ks, 0) + 1
                    if isinstance(v, dict):
                        for k2 in v.keys():
                            k2s = f"{ks}.{str(k2).casefold()}"
                            if "cor" in k2s or "color" in k2s:
                                field_hits[k2s] = field_hits.get(k2s, 0) + 1

            colors = _extract_colors(item, item_colors_attr)
            if not colors:
                continue

            source_colors = _merge_colors(source_colors, colors)

        if args.max_items and processed > args.max_items:
            break

        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        resp = itens_table.scan(ExclusiveStartKey=last, **scan_kwargs)

    if args.inspect_fields:
        for k, n in sorted(field_hits.items(), key=lambda x: (-x[1], x[0])):
            print(f"[FIELD] {k}: {n}")

    if not source_colors:
        print("Nenhuma cor encontrada nos itens.")
        return

    target_account_ids = []
    if args.target_account_id:
        target_account_ids = [args.target_account_id]
    else:
        resp = users_table.scan(
            ProjectionExpression="account_id",
            FilterExpression=Attr("account_id").exists(),
        )
        while True:
            for raw in resp.get("Items", []):
                item = _as_plain(raw)
                aid = item.get("account_id")
                if aid:
                    target_account_ids.append(aid)
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            resp = users_table.scan(
                ProjectionExpression="account_id",
                FilterExpression=Attr("account_id").exists(),
                ExclusiveStartKey=last,
            )
        seen = set()
        deduped = []
        for aid in target_account_ids:
            k = str(aid).strip()
            if not k or k in seen:
                continue
            seen.add(k)
            deduped.append(aid)
        target_account_ids = deduped

    updated = 0
    skipped = 0
    for account_id in target_account_ids:
        user_id = _settings_user_id(account_id)
        if args.dry_run:
            print(f"[DRY] {account_id}: fonte={len(source_colors)} cores -> {user_id}")
            continue

        try:
            existing_resp = users_table.get_item(Key={"user_id": user_id})
            existing_item = existing_resp.get("Item") or {}
            existing_colors = existing_item.get("color_options")
            if not isinstance(existing_colors, list):
                existing_colors = []

            if args.seed_only and existing_colors:
                skipped += 1
                continue

            if args.overwrite:
                final_colors = list(source_colors)
            else:
                final_colors = _merge_colors(existing_colors, source_colors)

            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET color_options = :c",
                ExpressionAttributeValues={":c": final_colors},
            )
            updated += 1
            print(f"[OK] {account_id}: {len(final_colors)} cores")
        except ClientError as e:
            print(f"[ERRO] {account_id}: {e}")

    source_label = source_account_id or "all-items"
    print(f"Fonte: {source_label} | coluna: {item_colors_attr} | cores: {len(source_colors)}")
    if args.dry_run:
        print(f"Targets (dry-run): {len(target_account_ids)}")
    else:
        print(f"Contas atualizadas: {updated}")
        print(f"Contas puladas: {skipped}")


if __name__ == "__main__":
    main()
