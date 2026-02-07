
import boto3
import os
from boto3.dynamodb.conditions import Key, Attr
from dotenv import load_dotenv
import sys

# Load environment variables
load_dotenv()

def debug_latest_items():
    print("--- Debugging Latest Items ---")
    
    # Initialize DynamoDB
    dynamodb = boto3.resource(
        'dynamodb',
        region_name=os.getenv('AWS_REGION', 'us-east-1'),
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
    )
    
    table_name = "alugueqqc_itens" # Updated from app.py
    # Verify table name from env if possible, but standard naming is usually consistent
    
    # Try to find table
    try:
        table = dynamodb.Table(table_name)
        table.load()
    except Exception as e:
        print(f"Error accessing table {table_name}: {e}")
        # Try finding table name from env
        print("Listing tables...")
        for t in dynamodb.tables.all():
            print(f"- {t.name}")
        return

    # We need the account_id. Since we don't have session access here, 
    # we will scan the last few items created globally or try to guess account_id if possible.
    # Better: Scan items with 'created_at' in the last 24 hours if possible, or just scan last 20 items.
    # Since we can't easily "scan last 20" without index, let's scan with a limit and sort manually.
    
    print(f"Scanning table: {table_name}")
    
    # Scan with projection to save bandwidth, but we want to see fields.
    # We will scan and then sort by created_at desc.
    
    response = table.scan(
        Limit=50,
        ProjectionExpression="item_id, item_custom_id, title, description, #s, created_at, account_id, item_title, item_description",
        ExpressionAttributeNames={"#s": "status"}
    )
    
    items = response.get('Items', [])
    
    # Sort by created_at descending
    items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    
    print(f"Found {len(items)} items in scan (showing top 10 latest):")
    
    for i, item in enumerate(items[:10]):
        print(f"[{i}] ID: {item.get('item_id')} | CustomID: {item.get('item_custom_id')} | Status: {item.get('status')} | Created: {item.get('created_at')} | Account: {item.get('account_id')}")
        print(f"    Title: {item.get('title') or item.get('item_title')}")
        print(f"    Desc: {item.get('description') or item.get('item_description')}")
        print(f"    Previous Status: {item.get('previous_status')}")
        print("-" * 40)

if __name__ == "__main__":
    debug_latest_items()
