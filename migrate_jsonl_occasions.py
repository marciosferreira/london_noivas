import json
import os
import boto3
import time
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import unicodedata
import re

# Carrega variáveis de ambiente
load_dotenv()

def slugify(text):
    """
    Normaliza o texto para slug compatível com as flags.
    Ex: "Gala / Black Tie" -> "gala"
    """
    text = text.lower()
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
    
    # Mapeamentos diretos baseados no JSONL observado
    if "gala" in text: return "gala"
    if "black tie" in text: return "gala"
    if "madrinha" in text: return "madrinha"
    if "formatura" in text: return "formatura"
    if "debutante" in text: return "debutante"
    if "convidada" in text: return "convidada"
    if "mae" in text and "noivo" in text: return "mae_dos_noivos"
    if "civil" in text: return "civil" # Pode ser chamado separadamente
    if "noiva" in text: return "noiva"
    
    return None

def migrate_occasions():
    print("--- Migrando Ocasiões do JSONL para DynamoDB ---")
    
    # Configuração DynamoDB
    dynamodb = boto3.resource(
        'dynamodb',
        region_name=os.getenv('AWS_REGION', 'us-east-1'),
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
    )
    
    table_name = "alugueqqc_itens"
    table = dynamodb.Table(table_name)
    
    jsonl_path = r"c:\Users\mnsmferr\london_noivas\embeddings_creation\vestidos_dataset_reprocessed_db.jsonl"
    
    if not os.path.exists(jsonl_path):
        print(f"Arquivo não encontrado: {jsonl_path}")
        return

    count = 0
    updated_count = 0
    
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            count += 1
            try:
                data = json.loads(line)
                item_id = data.get('item_id')
                
                if not item_id:
                    print(f"Linha {count}: item_id ausente, pulando.")
                    continue
                
                occasions_raw = data.get('metadata_filters', {}).get('occasions', [])
                if not occasions_raw:
                    continue
                
                # Identifica quais flags ativar
                flags_to_set = set()
                
                for occ in occasions_raw:
                    slug = slugify(occ)
                    if slug:
                        flags_to_set.add(f"occasion_{slug}")
                        
                    # Tratamento especial para "Noiva / Civil"
                    if "noiva" in occ.lower() and "civil" in occ.lower():
                        flags_to_set.add("occasion_noiva")
                        flags_to_set.add("occasion_civil")
                
                if not flags_to_set:
                    continue
                
                # Prepara UpdateExpression
                update_expr = "SET " + ", ".join([f"{flag} = :val" for flag in flags_to_set])
                expr_attr_vals = {":val": "1"}
                
                print(f"Atualizando item {item_id}: {flags_to_set}")
                
                try:
                    table.update_item(
                        Key={'item_id': item_id},
                        UpdateExpression=update_expr,
                        ExpressionAttributeValues=expr_attr_vals
                    )
                    updated_count += 1
                    # Pequena pausa para não estourar throughput se for PAY_PER_REQUEST alto
                    # time.sleep(0.05) 
                except ClientError as e:
                    print(f"Erro ao atualizar item {item_id}: {e}")
                    
            except json.JSONDecodeError:
                print(f"Erro de JSON na linha {count}")
            except Exception as e:
                print(f"Erro genérico na linha {count}: {e}")

    print(f"--- Concluído ---")
    print(f"Total processado: {count}")
    print(f"Total atualizado: {updated_count}")

if __name__ == "__main__":
    migrate_occasions()
