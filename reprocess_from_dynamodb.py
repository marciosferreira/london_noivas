import boto3
import json
import os
import requests
import base64
from openai import OpenAI
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Attr
import time

load_dotenv()

# Configuration
OUTPUT_FILE = os.path.join("embeddings_creation", "vestidos_dataset_reprocessed_db.jsonl")
TEMP_IMG_DIR = "temp_images_db"
TABLE_NAME = "alugueqqc_itens"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
dynamodb = boto3.resource(
    'dynamodb',
    region_name=os.getenv('AWS_REGION', 'us-east-1'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
)
table = dynamodb.Table(TABLE_NAME)

SYSTEM_PROMPT = """
Você é a Curadora Chefe da London Noivas, a maior autoridade em moda festa do Brasil.
Sua missão é analisar vestidos de alta costura e gerar metadados extremamente precisos para um sistema de busca semântica.

### 1. ANÁLISE VISUAL PROFUNDA
Analise a imagem em alta resolução. Identifique:
- Tecido (ex: Zibeline, Musseline, Paetê, Tule, Crepe, Cetim).
- Caimento (ex: Fluido, Estruturado, Evasê, Sereia, Princesa).
- Detalhes (ex: Fenda, Decote Profundo, Bordado, Pedraria, Transparência, Mangas, Capa).

### 2. CLASSIFICAÇÃO DE OCASIÕES (MULTI-LABEL)
Selecione TODAS as categorias que se aplicam. Use os critérios abaixo para garantir alinhamento com a curadoria da loja:

- **Noiva**: O vestido principal do casamento. Branco, Off-white ou Marfim. Pode ser clássico (renda, princesa), moderno (minimalista, fluido) ou boho. Transmite imponência e protagonismo.
- **Civil**: Elegância com leveza para casamento no cartório ou mini-wedding. Midi, curto ou longo sofisticado. Não é um vestido de baile, mas diz "estou celebrando". Tecidos como crepe, cetim e musseline.
- **Madrinha**: Elegante e harmonioso, para estar no altar. Cores variadas (exceto branco/muito claro). Evita competir com a noiva. Pode ter brilho ou ser liso, mas sempre com sofisticação.
- **Mãe dos Noivos**: Destaque absoluto e sofisticação. Peças estruturadas, caimento impecável, tecidos nobres. Mangas e decotes discretos são comuns. Cores clássicas: azul-marinho, marsala, verde-esmeralda, nude, dourado.
- **Formatura**: Ousadia e celebração. Brilho intenso, fendas, decotes profundos, cores vibrantes. A formanda é a protagonista da sua noite.
- **Debutante**: Sonho de princesa ou balada. Rodado volumoso (tradicional) ou curto/justo com brilho (festa). Marca a transição e o destaque da aniversariante.
- **Gala**: Sofisticação máxima, "Red Carpet". Tecidos nobres, cauda, bordados ricos. Comunica poder e elegância absoluta.
- **Convidada**: Equilíbrio entre estar linda e não ofuscar. Vestidos bonitos, midis ou longos menos armados. Cores que valorizam sem gritar. Evita excessos de brilho ou caudas longas.

### 3. SAÍDA JSON
Gere um JSON puro com:
- `description`: Texto rico, persuasivo e detalhado (2-3 frases).
- `occasions`: Lista de strings.
- `colors`: Lista com cor principal e matiz (ex: ["Verde", "Verde Esmeralda"]).
- `fabrics`: Lista de tecidos identificados.
- `details`: Lista de detalhes visuais.
- `keywords`: 5-10 palavras-chave para SEO.
"""

def scan_active_items():
    """Scaneia DynamoDB por itens ativos e com imagem."""
    print(f"--- Iniciando Scan do DynamoDB: {TABLE_NAME} ---")
    
    # 1. Buscar todos os itens que têm imagem
    # Nota: Em tabelas grandes, usar scan é caro. Mas para < 5000 itens é aceitável.
    try:
        response = table.scan(
            FilterExpression=Attr('item_image_url').exists(),
            ProjectionExpression="item_id, item_custom_id, title, description, item_image_url, #s",
            ExpressionAttributeNames={"#s": "status"}
        )
    except Exception as e:
        print(f"Erro no Scan inicial: {e}")
        return []

    items = response.get('Items', [])
    last_key = response.get('LastEvaluatedKey')
    
    while last_key:
        try:
            response = table.scan(
                FilterExpression=Attr('item_image_url').exists(),
                ProjectionExpression="item_id, item_custom_id, title, description, item_image_url, #s",
                ExpressionAttributeNames={"#s": "status"},
                ExclusiveStartKey=last_key
            )
            items.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
        except Exception as e:
            print(f"Erro na paginação: {e}")
            break
            
    # 2. Filtrar Inativos Localmente (mais seguro e fácil de debugar)
    active_items = []
    print(f"Total bruto encontrado: {len(items)}")
    
    for item in items:
        status = item.get('status', 'unknown')
        if str(status).lower() in ['deleted', 'archived', 'inactive']:
            continue
        if not item.get('item_image_url'):
            continue
        active_items.append(item)
        
    print(f"Total de itens ATIVOS e COM IMAGEM: {len(active_items)}")
    return active_items

def download_image(url, save_path):
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return True
    except Exception as e:
        print(f"Erro download {url}: {e}")
        return False

def analyze_image(image_path):
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Classifique este vestido."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]}
            ],
            response_format={"type": "json_object"},
            max_tokens=800,
            temperature=0.2
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Erro OpenAI: {e}")
        return None

def main():
    if not os.path.exists(TEMP_IMG_DIR):
        os.makedirs(TEMP_IMG_DIR)
        
    items = scan_active_items()
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
        
        for i, db_item in enumerate(items):
            custom_id = db_item.get('item_custom_id')
            image_url = db_item.get('item_image_url')
            title = db_item.get('title', 'Sem Título')
            
            print(f"[{i+1}/{len(items)}] ID: {custom_id} | {title}")
            
            local_path = os.path.join(TEMP_IMG_DIR, f"{custom_id}.jpg")
            
            if download_image(image_url, local_path):
                print(f"   [>] Analisando...")
                ai_data = analyze_image(local_path)
                
                if ai_data:
                    # Estratégia "Force Occasions": A primeira ocasião define a categoria
                    occasions_list = ai_data.get('occasions', [])
                    
                    if occasions_list:
                        primary_occasion = occasions_list[0]
                        cat_raw = primary_occasion
                        cat_slug = primary_occasion.lower().replace(" ", "-")
                    else:
                        cat_raw = "Geral"
                        cat_slug = "geral"

                    # Construir item completo
                    item_data = {
                        "custom_id": custom_id,
                        "item_id": db_item.get('item_id'),
                        "title": title,
                        "file_name": f"{custom_id}.jpg",
                        "description": ai_data.get('description', db_item.get('description')),
                        "category_slug": cat_slug,
                        "metadata_filters": {
                            "occasions": ai_data.get('occasions', []),
                            "colors": ai_data.get('colors', []),
                            "fabrics": ai_data.get('fabrics', []),
                            "details": ai_data.get('details', [])
                        },
                        "embedding_text": ""
                    }
                    
                    # Embedding Text Rico
                    occasions = " ".join(ai_data.get('occasions', []))
                    colors = " ".join(ai_data.get('colors', []))
                    details = " ".join(ai_data.get('details', []))
                    keywords = ai_data.get('keywords', "")
                    
                    item_data['embedding_text'] = f"{title} {occasions} {colors} {details} {keywords} {item_data['description']}"
                    
                    outfile.write(json.dumps(item_data, ensure_ascii=False) + '\n')
                    outfile.flush() # Garante gravação imediata
                    print(f"   [V] Sucesso! Ocasiões: {occasions}")
                else:
                    print(f"   [X] Falha na IA. Pulando.")
                
                # Cleanup
                if os.path.exists(local_path): os.remove(local_path)
            else:
                print(f"   [X] Falha no download. Pulando.")
            
            time.sleep(0.1)

    try:
        os.rmdir(TEMP_IMG_DIR)
    except:
        pass
        
    print(f"\nConcluído! Dataset salvo em: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
