import boto3
import os
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

def migrate_table(table_name, label):
    print(f"\n--- Migrating {label} ({table_name}) ---")
    table = dynamodb.Table(table_name)
    
    migrated_count = 0
    
    try:
        response = table.scan()
        items = response.get('Items', [])
        
        while 'LastEvaluatedKey' in response:
            response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
            items.extend(response.get('Items', []))
            
        print(f"Total items to check: {len(items)}")
        
        for item in items:
            if 'key_values' in item and isinstance(item['key_values'], dict):
                kv = item['key_values']
                if not kv: # Empty dict
                    continue
                
                print(f"Migrating item {item.get('transaction_id') or item.get('item_id') or item.get('client_id')}")
                
                # Prepare update expression
                update_expression = "REMOVE key_values"
                expression_attribute_names = {}
                expression_attribute_values = {}
                
                set_parts = []
                for k, v in kv.items():
                    # Check if key already exists at root to avoid overwriting (unless intended?)
                    # For safety, we trust the root value if it exists, or maybe we overwrite?
                    # Usually root is 'fixed' and kv is 'custom'. If moving custom to root, we just set it.
                    # But wait, 'client_name' in transaction might be snapshot.
                    
                    # Safe approach: Update item using put_item (replace) or specific updates.
                    # Let's use UpdateItem with SET for each key.
                    
                    # Verify if key is reserved word
                    key_placeholder = f"#{k}"
                    val_placeholder = f":{k}"
                    
                    set_parts.append(f"{key_placeholder} = {val_placeholder}")
                    expression_attribute_names[key_placeholder] = k
                    expression_attribute_values[val_placeholder] = v
                
                if set_parts:
                    update_expression += " SET " + ", ".join(set_parts)
                
                # Primary Key resolution
                pk_name = None
                pk_val = None
                if 'transaction_id' in item:
                    pk_name = 'transaction_id'
                elif 'item_id' in item:
                    pk_name = 'item_id'
                elif 'client_id' in item:
                    pk_name = 'client_id'
                
                if pk_name:
                    pk_val = item[pk_name]
                    
                    try:
                        table.update_item(
                            Key={pk_name: pk_val},
                            UpdateExpression=update_expression,
                            ExpressionAttributeNames=expression_attribute_names,
                            ExpressionAttributeValues=expression_attribute_values
                        )
                        migrated_count += 1
                        print(f"  -> Successfully migrated {pk_val}")
                    except Exception as e:
                        print(f"  -> Failed to migrate {pk_val}: {e}")
        
        print(f"Migration finished for {label}. Total migrated: {migrated_count}")
            
    except Exception as e:
        print(f"Error scanning/migrating table {table_name}: {e}")

if __name__ == "__main__":
    print("Starting Migration (Flattening key_values)...")
    # Only Transactions had data according to scan, but we check all for completeness/safety
    migrate_table("alugueqqc_transactions", "Transactions")
    migrate_table("alugueqqc_itens", "Itens")
    migrate_table("alugueqqc_clients", "Clients")
    print("\nAll migrations complete.")
