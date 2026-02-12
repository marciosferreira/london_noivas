import boto3
import os
import json
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Attr

load_dotenv()

def check_item(custom_id):
    dynamodb = boto3.resource(
        'dynamodb',
        region_name=os.getenv('AWS_REGION', 'us-east-1'),
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
    )
    table = dynamodb.Table("alugueqqc_itens")

    print(f"--- Diagnóstico para ID: {custom_id} ---")
    
    # Tentar encontrar o item
    try:
        response = table.scan(
            FilterExpression=Attr('item_custom_id').eq(custom_id)
        )
        items = response.get('Items', [])
        
        if not items:
            print(f"[ERRO] Item com custom_id '{custom_id}' NÃO ENCONTRADO no DynamoDB.")
            return

        item = items[0]
        print(f"[SUCESSO] Item encontrado (Partition Key: {item.get('item_id')})")
        
        # Verificar campo de URL
        if 'item_image_url' in item:
            print(f"-> Campo 'item_image_url': {item['item_image_url']}")
        else:
            print(f"-> [ERRO CRÍTICO] O campo 'item_image_url' está AUSENTE no registro.")

        # Mostrar outros campos relevantes para contexto
        print("\nOutros campos disponíveis:")
        for key, value in item.items():
            if key not in ['item_image_url', 'description', 'embedding_text']: # Ocultar campos longos
                print(f" - {key}: {value}")

    except Exception as e:
        print(f"Erro ao acessar DynamoDB: {e}")

if __name__ == "__main__":
    check_item("1658fbe952c8")
