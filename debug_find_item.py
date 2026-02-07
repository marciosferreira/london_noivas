import boto3
import os
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Attr

# Load environment variables
load_dotenv()

def find_item():
    aws_region = "us-east-1"
    table_name = "alugueqqc_itens"
    
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    table = dynamodb.Table(table_name)
    
    print(f"Scanning table {table_name} for item with title 'Elegância Sereia em Branco'...")
    
    # Scan with filter expression (inefficient for large tables, but fine for debug)
    # Using 'contains' just in case of minor mismatches, or straight equality
    try:
        response = table.scan(
            FilterExpression=Attr('title').eq('Elegância Sereia em Branco') | Attr('item_title').eq('Elegância Sereia em Branco')
        )
        
        items = response.get('Items', [])
        print(f"Found {len(items)} items matching title.")
        
        for item in items:
            print("\nItem Details:")
            print(f"  ID: {item.get('item_id')}")
            print(f"  Title: {item.get('title') or item.get('item_title')}")
            print(f"  Status: {item.get('status')}")
            print(f"  Account ID: {item.get('account_id')}")
            print(f"  Created At: {item.get('created_at')}")
            # Print all keys to see if anything looks weird
            print(f"  All Keys: {list(item.keys())}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    find_item()
