import boto3
import os
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

tables_to_scan = {
    "Itens": "alugueqqc_itens",
    "Clients": "alugueqqc_clients",
    "Transactions": "alugueqqc_transactions"
}

def scan_table(table_name, label):
    print(f"\n--- Scanning {label} ({table_name}) ---")
    table = dynamodb.Table(table_name)
    
    key_counts = Counter()
    items_scanned = 0
    items_with_kv = 0
    
    try:
        response = table.scan()
        items = response.get('Items', [])
        
        # Handle pagination
        while 'LastEvaluatedKey' in response:
            response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
            items.extend(response.get('Items', []))
            
        items_scanned = len(items)
        
        for item in items:
            kv = item.get('key_values')
            if kv and isinstance(kv, dict):
                items_with_kv += 1
                for k in kv.keys():
                    key_counts[k] += 1
                    
        print(f"Total items scanned: {items_scanned}")
        print(f"Items with 'key_values': {items_with_kv}")
        
        if key_counts:
            print("Found custom keys:")
            for k, v in key_counts.most_common():
                print(f"  - {k}: {v} occurrences")
        else:
            print("No custom keys found in 'key_values'.")
            
    except Exception as e:
        print(f"Error scanning table {table_name}: {e}")

if __name__ == "__main__":
    print("Starting Diagnostic Scan...")
    for label, table_name in tables_to_scan.items():
        scan_table(table_name, label)
    print("\nScan complete.")
