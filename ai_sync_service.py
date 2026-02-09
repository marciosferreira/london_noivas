import os
import sys
import json
import boto3
import requests
import pickle
import shutil
import base64
import subprocess
import faiss # Import faiss for direct index checking
from botocore.exceptions import ClientError
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRESSES_DIR = os.path.join(BASE_DIR, 'static', 'dresses')
DATASET_FILE = os.path.join(BASE_DIR, 'embeddings_creation', 'vestidos_dataset.jsonl')
METADATA_FILE = os.path.join(BASE_DIR, 'vector_store_metadata.pkl')
INDEX_FILE = os.path.join(BASE_DIR, 'vector_store.index')
CREATE_VECTOR_SCRIPT = os.path.join(BASE_DIR, 'embeddings_creation', 'create_vector_store.py')

# AWS & OpenAI
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
itens_table = dynamodb.Table('alugueqqc_itens')
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_index_status():
    """
    Compara itens no DynamoDB com itens no índice FAISS (via metadata).
    Retorna contagens e IDs discrepantes, além de itens marcados como 'pending'.
    """
    # 1. Get all IDs from DynamoDB
    db_ids = set()
    pending_ids = set()
    
    try:
        # Scan otimizado
        response = itens_table.scan(
            ProjectionExpression="item_id, embedding_status, item_image_url, #st",
            ExpressionAttributeNames={"#st": "status"}
        )
        for item in response.get('Items', []):
            # Apenas considera itens com imagem como "indexáveis"
            if not item.get('item_image_url'):
                continue
            
            # Ignorar itens deletados (soft delete)
            if item.get('status') == 'deleted':
                continue
                
            db_ids.add(item['item_id'])
            if item.get('embedding_status') == 'pending':
                pending_ids.add(item['item_id'])
        
        while 'LastEvaluatedKey' in response:
            response = itens_table.scan(
                ProjectionExpression="item_id, embedding_status, item_image_url, #st",
                ExpressionAttributeNames={"#st": "status"},
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            for item in response.get('Items', []):
                # Apenas considera itens com imagem como "indexáveis"
                if not item.get('item_image_url'):
                    continue

                # Ignorar itens deletados (soft delete)
                if item.get('status') == 'deleted':
                    continue

                db_ids.add(item['item_id'])
                if item.get('embedding_status') == 'pending':
                    pending_ids.add(item['item_id'])

    except Exception as e:
        print(f"Erro ao escanear DynamoDB: {e}")
        return {"error": str(e)}

    # 2. Get all IDs from Metadata & Check Index Integrity
    index_ids = set()
    index_total = 0
    integrity_error = None

    # Check Metadata
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'rb') as f:
                meta_list = pickle.load(f)
                for m in meta_list:
                    if 'custom_id' in m:
                        index_ids.add(m['custom_id'])
        except Exception as e:
            print(f"Erro ao ler metadata: {e}")
            integrity_error = f"Erro leitura metadata: {e}"

    # Check FAISS Index (Strict Count Check)
    if os.path.exists(INDEX_FILE):
        try:
            index = faiss.read_index(INDEX_FILE)
            index_total = index.ntotal
        except Exception as e:
            print(f"Erro ao ler índice FAISS: {e}")
            integrity_error = f"Erro leitura índice: {e}"
    else:
        integrity_error = "Arquivo de índice não encontrado."

    # Consistency Check: Metadata vs Index
    if not integrity_error and len(index_ids) != index_total:
        integrity_error = f"Inconsistência Interna: Metadata tem {len(index_ids)} itens, Índice tem {index_total}."

    # 3. Compare DB vs Metadata
    missing_in_index = db_ids - index_ids # Needs Add
    deleted_in_db = index_ids - db_ids    # Needs Remove
    
    # Remove pending from missing to avoid duplication if they are both
    missing_in_index = missing_in_index - pending_ids

    # Final Status Construction
    status = {
        "db_count": len(db_ids),
        "index_count": index_total,
        "metadata_count": len(index_ids),
        "missing_count": len(missing_in_index),
        "deleted_count": len(deleted_in_db),
        "missing_ids": list(missing_in_index),
        "deleted_ids": list(deleted_in_db),
        "pending_ids": list(pending_ids),
        "integrity_error": integrity_error
    }

    # If significant mismatch in counts, add warning
    if abs(len(db_ids) - index_total) > 0 and not integrity_error:
        status["warning"] = f"Desbalanço: DB({len(db_ids)}) vs Index({index_total})"

    return status

def encode_image(image_input):
    """Encodes image from path (str) or bytes to base64 string"""
    if isinstance(image_input, str):
        # É um caminho de arquivo
        with open(image_input, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")
    else:
        # Assume que são bytes
        return base64.b64encode(image_input).decode("utf-8")

def generate_dress_metadata(image_input, existing_desc=None, existing_title=None):
    """
    Gera descrição e título usando GPT-4o, se não fornecidos.
    image_input: pode ser caminho do arquivo (str) ou bytes da imagem.
    """
    
    description = existing_desc
    title = existing_title

    # Se já temos ambos, retorna
    if description and title:
        return description, title

    base64_image = None
    
    # 1. Descrição (se faltar)
    if not description:
        try:
            if not base64_image:
                base64_image = encode_image(image_input)

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Descreva este vestido de forma detalhada para um catálogo de busca, focando em: cor, tecido, estilo (ex: sereia, princesa), tipo de decote, mangas, e ocasião ideal. Seja objetivo e use português do Brasil. Máximo de 50 palavras."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        ],
                    }
                ],
                max_tokens=150,
            )
            description = response.choices[0].message.content
        except Exception as e:
            print(f"Erro ao gerar descrição: {e}")
            # Raise exception to skip saving
            raise Exception(f"Falha ao gerar descrição: {str(e)}")

    # 2. Título (se faltar)
    if not title:
        try:
            response_title = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Você é um especialista em marketing de moda."},
                    {"role": "user", "content": f"Crie um título curto e atraente (máximo 5 palavras) para este vestido baseado na descrição: {description}"}
                ],
                max_tokens=20,
            )
            title = response_title.choices[0].message.content.replace('"', '')
        except Exception as e:
            print(f"Erro ao gerar título: {e}")
            # Raise exception to skip saving
            raise Exception(f"Falha ao gerar título: {str(e)}")

    return description, title

def sync_index():
    """
    Sincroniza o índice FAISS com o DynamoDB:
    1. Baixa imagens de novos itens
    2. Gera metadados IA
    3. Remove itens deletados do JSONL
    4. Reconstrói o índice
    """
    status = get_index_status()
    if "error" in status:
        return {"status": "error", "message": status["error"]}

    missing_ids = status['missing_ids']
    deleted_ids = status['deleted_ids']
    pending_ids = status.get('pending_ids', [])

    # Se houver erro de integridade, CONTINUA a execução para reconstruir o índice
    if not missing_ids and not deleted_ids and not pending_ids and not status.get('integrity_error'):
        return {"status": "success", "message": "Índice já está atualizado."}

    print(f"Iniciando sincronização: +{len(missing_ids)} novos, -{len(deleted_ids)} deletados, ~{len(pending_ids)} atualizações. Integridade: {status.get('integrity_error') or 'OK'}")

    # A. Carregar dados existentes
    existing_data = []
    if os.path.exists(DATASET_FILE):
        with open(DATASET_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        existing_data.append(json.loads(line))
                    except:
                        pass
    
    # B. Filtrar Deletados e Pendentes (serão reprocessados)
    ids_to_remove = set(deleted_ids) | set(pending_ids)
    if ids_to_remove:
        initial_count = len(existing_data)
        # Mantém apenas se custom_id NÃO estiver na lista de removidos/atualizados
        existing_data = [d for d in existing_data if d.get('custom_id') not in ids_to_remove]
        print(f"Removidos {initial_count - len(existing_data)} itens do dataset (deletados + pendentes).")

    # C. Processar Novos Itens (Missing + Pending)
    items_to_process = set(missing_ids) | set(pending_ids)
    
    new_items_count = 0
    updated_items_count = 0
    failed_items = []

    for item_id in items_to_process:
        try:
            # Busca dados no DB
            resp = itens_table.get_item(Key={'item_id': item_id})
            item = resp.get('Item')
            if not item: continue
            
            image_url = item.get('item_image_url')
            if not image_url: 
                print(f"Item {item_id} sem imagem, pulando.")
                continue 
            
            # Download da Imagem (Em Memória)
            ext = 'jpg'
            if '.' in image_url:
                parts = image_url.split('.')
                if len(parts) > 1:
                    ext = parts[-1].split('?')[0]
                    if len(ext) > 4: ext = 'jpg' # Fallback
            
            # Mantemos o nome do arquivo no JSON para referência (caso precise no futuro)
            # mas NÃO salvamos o arquivo no disco.
            filename = f"{item_id}.{ext}"

            print(f"Baixando imagem para memória {item_id}...")
            try:
                r = requests.get(image_url, timeout=15)
                if r.status_code == 200:
                    image_bytes = r.content
                else:
                    print(f"Falha ao baixar imagem: {r.status_code}")
                    failed_items.append(f"{item_id}: Falha download imagem status {r.status_code}")
                    continue
            except Exception as e:
                print(f"Exceção no download: {e}")
                failed_items.append(f"{item_id}: Exceção download")
                continue

            # Gera Metadados
            print(f"Gerando metadados IA para {item_id}...")
            
            existing_desc = item.get("description") or item.get("item_description")
            existing_title = item.get("title") or item.get("item_title")

            # Passa os bytes da imagem diretamente
            description, title = generate_dress_metadata(image_bytes, existing_desc, existing_title)
            
            new_entry = {
                "file_name": filename,
                "custom_id": item_id,
                "description": description,
                "title": title
            }
            existing_data.append(new_entry)
            
            if item_id in pending_ids:
                updated_items_count += 1
                # Limpa flag pending no DB
                try:
                    itens_table.update_item(
                        Key={'item_id': item_id},
                        UpdateExpression="REMOVE embedding_status"
                    )
                except Exception as e:
                    print(f"Erro ao remover flag pending do item {item_id}: {e}")
            else:
                new_items_count += 1
            
        except Exception as e:
            print(f"Erro ao processar item {item_id}: {e}")
            failed_items.append(f"{item_id}: {str(e)}")
            continue

    # D. Salvar JSONL Atualizado
    print("Salvando dataset atualizado...")
    with open(DATASET_FILE, 'w', encoding='utf-8') as f:
        for entry in existing_data:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    # E. Reconstruir Índice (Chama o script existente)
    print("Reconstruindo índice FAISS...")
    try:
        # Usa o mesmo python que está rodando este script
        subprocess.run([sys.executable, CREATE_VECTOR_SCRIPT], check=True)
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"Falha na reconstrução do índice: {e}"}
    
    # Mensagem final ajustada
    if status.get('integrity_error'):
        message = f"Reparo de integridade concluído. +{new_items_count} adicionados, -{len(deleted_ids)} removidos."
    else:
        message = f"Sincronização concluída. +{new_items_count} adicionados, -{len(deleted_ids)} removidos."
    if failed_items:
        message += f" ATENÇÃO: {len(failed_items)} itens falharam e não foram salvos: " + "; ".join(failed_items)

    return {
        "status": "success", 
        "message": message
    }
