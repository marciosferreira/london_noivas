import os
import argparse
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Attr
from dotenv import load_dotenv


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


def _settings_user_id(account_id):
    return f"account_settings:{account_id}"


BASE_COLORS = {
    "Branco": ["Off-White", "Pérola", "Marfim", "Branco Óptico", "Gelo"],
    "Preto": ["Preto Absoluto", "Ônix", "Noir", "Preto Fosco"],
    "Azul": ["Serenity", "Royal", "Marinho", "Midnight", "Tiffany", "Petróleo"],
    "Verde": ["Esmeralda", "Oliva", "Sálvia", "Musgo", "Menta"],
    "Rosa": ["Rosê Gold", "Rosa Chá", "Fúcsia", "Malva", "Pink"],
    "Vermelho/Vinho": ["Marsala", "Bordô", "Rubi", "Escarlate", "Terracota"],
    "Amarelo/Laranja": ["Canário", "Vanilla", "Mostarda", "Coral", "Âmbar"],
    "Bege/Nude": ["Fendi", "Champagne", "Nude Rosé", "Taupe", "Areia"],
    "Roxo/Lilás": ["Lavanda", "Beringela", "Malva", "Ametista"],
    "Cinza/Metal": ["Chumbo", "Prata", "Dourado", "Cobre", "Grafite"],
}


def _build_color_options():
    out = []
    seen = set()
    for base, names in BASE_COLORS.items():
        for name in names:
            key = (base.casefold(), name.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append({"base": base, "name": name})
    return out


def _load_target_accounts(users_table, target_account_id):
    if target_account_id:
        return [target_account_id]
    target_account_ids = []
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
    return deduped


def main():
    load_dotenv(dotenv_path=".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--users-table", default="alugueqqc_users")
    parser.add_argument("--target-account-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=args.region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    users_table = dynamodb.Table(args.users_table)
    colors = _build_color_options()
    targets = _load_target_accounts(users_table, args.target_account_id)

    updated = 0
    for account_id in targets:
        user_id = _settings_user_id(account_id)
        if args.dry_run:
            print(f"[DRY] {account_id}: {len(colors)} cores -> {user_id}")
            continue
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET color_options = :c",
            ExpressionAttributeValues={":c": colors},
        )
        updated += 1
        print(f"[OK] {account_id}: {len(colors)} cores")

    if args.dry_run:
        print(f"Targets (dry-run): {len(targets)}")
    else:
        print(f"Contas atualizadas: {updated}")


if __name__ == "__main__":
    main()
