import boto3
import json
import os
import datetime
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSONL_FILE = os.path.join(BASE_DIR, "vestidos_dataset.jsonl")
LOG_FILE = os.path.join(BASE_DIR, "import_log.txt")

try:
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
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

def extract_cor(item_data):
    mf = item_data.get("metadata_filters")
    if not isinstance(mf, dict):
        return None
    colors = mf.get("colors")
    if isinstance(colors, str):
        s = colors.strip()
        return s or None
    if isinstance(colors, list):
        for c in colors:
            if c is None:
                continue
            s = str(c).strip()
            if s:
                return s
    return None

def process_import():
    log_message("=== Iniciando Atualização de Cores (cor) via JSONL ===")
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

            item_id = item_data.get("custom_id")
            if not item_id:
                log_message(f"Item {index+1} sem custom_id no JSONL. Pulando.")
                error_count += 1
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cor = extract_cor(item_data)
            if not cor:
                continue

            try:
                itens_table.update_item(
                    Key={"item_id": item_id},
                    ConditionExpression=Attr("item_id").exists(),
                    UpdateExpression="SET cor = :c, updated_at = :u",
                    ExpressionAttributeValues={
                        ":c": cor,
                        ":u": now,
                    },
                )
                success_count += 1
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue
                log_message(f"ERRO ao atualizar cor do item {item_id}: {e}")
                error_count += 1

        except Exception as e:
            log_message(f"Erro no item {index+1}: {e}")
            error_count += 1

    log_message("=== Atualização Concluída ===")
    log_message(f"Sucessos: {success_count}")
    log_message(f"Erros: {error_count}")

if __name__ == "__main__":
    process_import()
