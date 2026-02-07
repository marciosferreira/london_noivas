import os
import json
import base64
import uuid
import time
from openai import OpenAI
from dotenv import load_dotenv

# Configuração
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Caminhos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR) 
IMAGE_DIR = os.path.join(PROJECT_ROOT, "static", "dresses")
DATASET_FILE = os.path.join(BASE_DIR, "vestidos_dataset.jsonl")
TEMP_FILE = os.path.join(BASE_DIR, "vestidos_dataset_temp.jsonl")

VALID_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')

def encode_image(image_path):
    """Codifica a imagem em base64 para envio via API."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def generate_custom_id():
    """Gera ID de 12 caracteres hexadecimais (padrão do site)."""
    return str(uuid.uuid4().hex[:12])

def generate_description(image_path):
    """Gera descrição do vestido usando GPT-4o."""
    try:
        base64_image = encode_image(image_path)
        prompt = (
            "Você é um especialista em moda. Analise a imagem e descreva EXCLUSIVAMENTE o vestido. "
            "Ignore a modelo, o rosto dela e o cenário. Extraia as seguintes informações em formato de texto corrido: "
            "Ocasião Ideal (ex: Noiva, Madrinha, Formatura, Festa de Gala, ou versátil para várias ocasiões), "
            "Estilo (ex: sereia, princesa), Material (ex: renda, cetim), Cor exata, Detalhes (ex: cauda longa, decote, bordados)."
        )
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        },
                    ],
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Erro ao gerar descrição para {os.path.basename(image_path)}: {e}")
        return None

def generate_title(description):
    """Gera título curto usando GPT-4o-mini baseado na descrição."""
    if not description:
        return "Vestido Exclusivo London"
        
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system", 
                    "content": "Você é um assistente de moda. Crie um título curto, elegante e comercial (4 a 5 palavras) baseado na descrição do vestido fornecida."
                },
                {"role": "user", "content": description}
            ],
            max_tokens=20,
            temperature=0.7
        )
        return response.choices[0].message.content.strip().replace('"', '')
    except Exception as e:
        print(f"Erro ao gerar título: {e}")
        return "Vestido Exclusivo London"

def load_dataset():
    """Carrega o dataset existente em um dicionário {file_name: data}."""
    dataset = {}
    if os.path.exists(DATASET_FILE):
        print(f"Carregando dataset existente: {DATASET_FILE}")
        with open(DATASET_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    item = json.loads(line)
                    if 'file_name' in item:
                        dataset[item['file_name']] = item
                except json.JSONDecodeError:
                    continue
    return dataset

def process_pipeline():
    print("--- Iniciando Pipeline Unificado de Processamento de Vestidos ---")
    print(f"Diretório de Imagens: {IMAGE_DIR}")
    print(f"Arquivo de Saída: {DATASET_FILE}")
    
    if not os.path.exists(IMAGE_DIR):
        print(f"ERRO: Diretório de imagens não encontrado: {IMAGE_DIR}")
        return

    # 1. Carregar dados existentes
    dataset = load_dataset()
    existing_count = len(dataset)
    print(f"Itens carregados: {existing_count}")

    # 2. Listar imagens
    image_files = [f for f in os.listdir(IMAGE_DIR) if f.lower().endswith(VALID_EXTENSIONS)]
    print(f"Imagens encontradas na pasta: {len(image_files)}")

    # 3. Processar cada imagem (Criar ou Atualizar)
    processed_count = 0
    updated_count = 0
    new_count = 0
    
    # Abrir arquivo temporário para escrita (reescrevendo tudo para garantir consistência)
    with open(TEMP_FILE, 'w', encoding='utf-8') as f_out:
        
        # Iterar sobre todas as imagens da pasta
        for filename in image_files:
            image_path = os.path.join(IMAGE_DIR, filename)
            
            # Recuperar item existente ou criar novo
            if filename in dataset:
                item = dataset[filename]
                is_new = False
            else:
                item = {"file_name": filename}
                is_new = True
                new_count += 1
            
            modified = False
            
            # --- STEP 1: Custom ID ---
            if "custom_id" not in item or not item["custom_id"]:
                item["custom_id"] = generate_custom_id()
                modified = True
                # print(f"[{filename}] ID gerado: {item['custom_id']}")

            # --- STEP 2: Description (GPT-4o) ---
            if "description" not in item or not item["description"]:
                print(f"[{filename}] Gerando descrição...")
                desc = generate_description(image_path)
                if desc:
                    item["description"] = desc
                    modified = True
                    time.sleep(1) # Rate limit friendly
                else:
                    print(f"[{filename}] Falha ao gerar descrição. Pulando geração de título.")

            # --- STEP 3: Title (GPT-4o-mini) ---
            if "title" not in item or not item["title"]:
                # Só gera título se tiver descrição
                if item.get("description"):
                    print(f"[{filename}] Gerando título...")
                    title = generate_title(item["description"])
                    item["title"] = title
                    modified = True
            
            # Salvar no arquivo temporário
            f_out.write(json.dumps(item, ensure_ascii=False) + "\n")
            f_out.flush()
            
            if modified and not is_new:
                updated_count += 1
            
            processed_count += 1
            if processed_count % 10 == 0:
                print(f"Processados: {processed_count}/{len(image_files)}")

    # 4. Finalizar: Substituir arquivo original
    print("\n--- Processamento Concluído ---")
    print(f"Novos itens: {new_count}")
    print(f"Itens atualizados: {updated_count}")
    
    if os.path.exists(DATASET_FILE):
        os.remove(DATASET_FILE)
    os.rename(TEMP_FILE, DATASET_FILE)
    print(f"Arquivo salvo com sucesso: {DATASET_FILE}")

if __name__ == "__main__":
    process_pipeline()
