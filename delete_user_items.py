import boto3
import os
from boto3.dynamodb.conditions import Key, Attr
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Configuração AWS
AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = "alugueqqc-images"
TARGET_EMAIL = "concursoadapta@gmail.com"

# Inicializar recursos
try:
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    s3_client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    
    users_table = dynamodb.Table("alugueqqc_users")
    itens_table = dynamodb.Table("alugueqqc_itens")
    
except Exception as e:
    print(f"Erro ao conectar com AWS: {e}")
    exit(1)

def get_target_user(email):
    print(f"Buscando usuário: {email}...")
    response = users_table.query(
        IndexName="email-index",
        KeyConditionExpression=Key("email").eq(email)
    )
    items = response.get("Items", [])
    if not items:
        print("Usuário não encontrado.")
        return None
    return items[0]

def delete_s3_object(url):
    if not url: return
    try:
        # Extrair key da URL
        # Ex: https://alugueqqc-images.s3.amazonaws.com/uuid.jpg
        if S3_BUCKET_NAME in url:
            key = url.split(f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/")[-1]
            # Caso a URL esteja em outro formato, tentar split simples
            if key == url:
                key = url.split("/")[-1]
            
            print(f"  - Deletando imagem S3: {key}")
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
    except Exception as e:
        print(f"  ! Erro ao deletar S3 {url}: {e}")

def delete_all_items():
    user = get_target_user(TARGET_EMAIL)
    if not user: return

    user_id = user["user_id"]
    account_id = user["account_id"]
    
    print(f"Usuário ID: {user_id}")
    print(f"Account ID: {account_id}")
    print("Iniciando varredura de itens...")

    # Scan filtrando por user_id (mais seguro que deletar tudo do banco)
    # Nota: Em produção com muitos dados, Query via GSI seria melhor.
    # Como não temos certeza do GSI, vamos de Scan com Filter.
    
    scan_kwargs = {
        'FilterExpression': Attr('user_id').eq(user_id),
        'ProjectionExpression': "account_id, item_id, item_image_url, title"
    }
    
    done = False
    start_key = None
    deleted_count = 0
    
    while not done:
        if start_key:
            scan_kwargs['ExclusiveStartKey'] = start_key
            
        response = itens_table.scan(**scan_kwargs)
        items = response.get('Items', [])
        
        for item in items:
            print(f"Deletando: {item.get('title', 'Sem título')} ({item['item_id']})")
            
            # 1. Deletar Imagem S3
            delete_s3_object(item.get('item_image_url'))
            
            # 2. Deletar do DynamoDB
            try:
                itens_table.delete_item(
                    Key={
                        'item_id': item['item_id']
                    }
                )
                deleted_count += 1
            except Exception as e:
                print(f"  ! Erro ao deletar item DynamoDB: {e}")
        
        start_key = response.get('LastEvaluatedKey')
        if not start_key:
            done = True

    print(f"\nConcluído! Total de itens deletados: {deleted_count}")

if __name__ == "__main__":
    print(f"ATENÇÃO: Iniciando deleção automática de itens para {TARGET_EMAIL}...")
    delete_all_items()
