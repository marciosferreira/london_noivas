import os
import base64
import json
from openai import OpenAI
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

# Configuração da API
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Caminhos
IMAGE_DIR = r"C:\Users\mnsmferr\london_noivas\static\dresses"
OUTPUT_FILE = "vestidos_dataset.jsonl"

def encode_image(image_path):
    """Codifica a imagem em base64 para envio via API."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

import time

def analyze_dress(image_path):
    """Envia a imagem para o GPT-4o para extração de metadados técnicos."""
    base64_image = encode_image(image_path)
    
    prompt = (
        "Você é um especialista em moda. Analise a imagem e descreva EXCLUSIVAMENTE o vestido. "
        "Ignore a modelo, o rosto dela e o cenário. Extraia as seguintes informações em formato de texto corrido: "
        "Ocasião Ideal (ex: Noiva, Madrinha, Formatura, Festa de Gala, ou versátil para várias ocasiões), "
        "Estilo (ex: sereia, princesa), Material (ex: renda, cetim), Cor exata, Detalhes (ex: cauda longa, decote, bordados)."
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
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
            if attempt < max_retries - 1:
                print(f"Erro na tentativa {attempt + 1}: {e}. Tentando novamente em 2 segundos...")
                time.sleep(2)
            else:
                raise e


# Execução do Processamento
def process_folder():
    valid_extensions = ('.jpg', '.jpeg', '.png', '.webp')
    
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        for filename in os.listdir(IMAGE_DIR):
            if filename.lower().endswith(valid_extensions):
                print(f"Processando: {filename}...")
                image_path = os.path.join(IMAGE_DIR, filename)
                
                try:
                    descricao = analyze_dress(image_path)
                    
                    # Criando o objeto para o JSONL
                    data = {
                        "file_name": filename,
                        "description": descricao
                    }
                    
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"Erro ao processar {filename}: {e}")

if __name__ == "__main__":
    process_folder()