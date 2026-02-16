import boto3
from boto3.dynamodb.conditions import Attr
from dotenv import load_dotenv

def find_accounts_with_colors():
    aws_region = "us-east-1"
    table_name = "alugueqqc_users"

    load_dotenv()
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    table = dynamodb.Table(table_name)

    try:
        response = table.scan(
            FilterExpression=Attr("user_id").begins_with("account_settings:")
        )
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression=Attr("user_id").begins_with("account_settings:"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        ranked = []
        for item in items:
            user_id = item.get("user_id") or ""
            account_id = user_id.split("account_settings:")[-1]
            colors = item.get("color_options")
            sizes = item.get("size_options")
            color_count = len(colors) if isinstance(colors, list) else 0
            size_count = len(sizes) if isinstance(sizes, list) else 0
            ranked.append((color_count, size_count, account_id, colors))

        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        print(f"Found {len(ranked)} account_settings items")
        for color_count, size_count, account_id, colors in ranked:
            print(f"{account_id} | colors: {color_count} | sizes: {size_count}")
            if isinstance(colors, list):
                print(f"  sample colors: {colors[:12]}")
        if ranked:
            top_account_id = ranked[0][2]
            import ai_sync_service as sync
            settings = sync._load_account_settings_options(top_account_id)
            print(f"top_account_id: {top_account_id}")
            print(f"sync_settings_colors: {len(settings.get('colors', []))}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    find_accounts_with_colors()
