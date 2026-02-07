import boto3
import json
import os
import uuid
import datetime
from decimal import Decimal
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Configuração AWS
AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = "alugueqqc-images"

# Configuração de Arquivos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
JSONL_FILE = os.path.join(BASE_DIR, "vestidos_dataset.jsonl")
IMAGES_DIR = os.path.join(PROJECT_ROOT, "static", "dresses")
LOG_FILE = os.path.join(BASE_DIR, "import_log.txt")
TARGET_EMAIL = "concursoadapta@gmail.com"

# Inicializar recursos AWS
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

def log_message(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_message = f"[{timestamp}] {message}"
    print(formatted_message)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(formatted_message + "\n")

def get_target_user(email):
    """Busca o usuário pelo email para obter user_id e account_id"""
    try:
        response = users_table.query(
            IndexName="email-index",
            KeyConditionExpression=Key("email").eq(email)
        )
        items = response.get("Items", [])
        if not items:
            log_message(f"ERRO: Usuário com email {email} não encontrado.")
            return None
        return items[0]
    except Exception as e:
        log_message(f"ERRO ao buscar usuário: {e}")
        return None

def upload_image_to_s3(local_path, filename):
    """Faz upload da imagem para o S3 e retorna a URL pública"""
    try:
        # Gerar nome único para evitar colisão, mantendo extensão
        ext = os.path.splitext(filename)[1]
        unique_filename = f"{uuid.uuid4()}{ext}"
        
        # Determinar Content-Type
        content_type = "image/jpeg"
        if ext.lower() == ".png":
            content_type = "image/png"
        elif ext.lower() == ".webp":
            content_type = "image/webp"

        s3_client.upload_file(
            local_path,
            S3_BUCKET_NAME,
            unique_filename,
            ExtraArgs={
                "ContentType": content_type
            }
        )
        
        url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{unique_filename}"
        return url
    except Exception as e:
        log_message(f"ERRO no upload S3 da imagem {filename}: {e}")
        return None

def check_item_exists(item_id):
    """Verifica se o item já existe no DynamoDB"""
    try:
        response = itens_table.get_item(Key={"item_id": item_id})
        return "Item" in response
    except Exception as e:
        log_message(f"Erro ao verificar existência do item {item_id}: {e}")
        return False

def process_import():
    log_message("=== Iniciando Importação de Vestidos (Atualização de Campos) ===")
    
    user = get_target_user(TARGET_EMAIL)
    if not user:
        return

    user_id = user["user_id"]
    account_id = user["account_id"]
    log_message(f"Usuário identificado: {user.get('username', 'N/A')} (ID: {user_id})")

    if not os.path.exists(JSONL_FILE):
        log_message(f"ERRO: Arquivo {JSONL_FILE} não encontrado.")
        return

    success_count = 0
    error_count = 0

    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    total_items = len(lines)
    log_message(f"Total de itens para processar: {total_items}")

    for index, line in enumerate(lines):
        try:
            if not line.strip():
                continue
                
            item_data = json.loads(line)
            file_name = item_data["file_name"]
            image_path = os.path.join(IMAGES_DIR, file_name)
            
            # Verificar imagem local
            if not os.path.exists(image_path):
                log_message(f"Item {index+1} ignorado: Imagem não encontrada em {image_path}")
                error_count += 1
                continue

            # Definir item_id
            item_id = item_data.get("custom_id")
            if not item_id:
                item_id = str(uuid.uuid4().hex[:12])

            # VERIFICAÇÃO DE EXISTÊNCIA (IDEMPOTÊNCIA)
            if check_item_exists(item_id):
                log_message(f"Item {index+1} já existe (ID: {item_id}). Pulando.")
                continue

            # Upload para S3
            image_url = upload_image_to_s3(image_path, file_name)
            if not image_url:
                error_count += 1
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Construir objeto DynamoDB com mapeamento CORRETO baseado no field_config
            dynamo_item = {
                "account_id": account_id,
                "item_id": item_id,
                "user_id": user_id,
                "created_at": now,
                "updated_at": now,
                "retirado": False,
                "status": "available",
                
                # Campos Base (que parecem ser usados fora do field_config em alguns lugares)
                "title": item_data["title"],
                "category": "Vestido",
                
                # Campos Mapeados conforme field_config (Fixos)
                "item_custom_id": item_id,
                "item_description": item_data["description"],
                "item_image_url": image_url,
                "item_value": Decimal("0"), # Preço 0
                "item_obs": "", # Opcional
                
                # Campos Antigos (mantidos por compatibilidade ou limpeza)
                # "image_url": image_url, # Removido para evitar confusão, frontend usa item_image_url
                # "price": Decimal("0"),  # Removido, usa item_value
                # "value": Decimal("0"),  # Removido, usa item_value
                
                # Metadados
                "original_filename": file_name,
                "key_values": {} # Inicializar vazio se necessário
            }

            itens_table.put_item(Item=dynamo_item)
            log_message(f"Item {index+1} atualizado: {item_data['title']} (ID: {item_id})")
            success_count += 1

        except Exception as e:
            log_message(f"Erro no item {index+1}: {e}")
            error_count += 1

    log_message("=== Atualização Concluída ===")
    log_message(f"Sucessos: {success_count}")
    log_message(f"Erros: {error_count}")

if __name__ == "__main__":
    process_import()
