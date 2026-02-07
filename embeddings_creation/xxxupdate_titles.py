import json
import os
from openai import OpenAI
from dotenv import load_dotenv

# Carrega ambiente
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

INPUT_FILE = "vestidos_dataset.jsonl"
OUTPUT_FILE = "vestidos_dataset_updated.jsonl"

def generate_title(description):
    """Gera um título curto a partir da descrição usando GPT-4o-mini (mais rápido e barato)."""
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

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Arquivo {INPUT_FILE} não encontrado.")
        return

    print("Iniciando atualização de títulos...")
    updated_count = 0
    
    with open(INPUT_FILE, "r", encoding="utf-8") as f_in, \
         open(OUTPUT_FILE, "w", encoding="utf-8") as f_out:
        
        for line in f_in:
            line = line.strip()
            if not line: continue
            
            try:
                data = json.loads(line)
                
                # Se não tem título ou o título está vazio, gera um novo
                if "title" not in data or not data["title"]:
                    print(f"Gerando título para: {data.get('file_name', 'item desconhecido')}...")
                    data["title"] = generate_title(data.get("description", ""))
                    updated_count += 1
                
                # Salva no novo arquivo
                f_out.write(json.dumps(data, ensure_ascii=False) + "\n")
                f_out.flush()
                
            except json.JSONDecodeError:
                continue

    print(f"\nConcluído! {updated_count} itens atualizados.")
    print(f"Novo arquivo salvo como: {OUTPUT_FILE}")
    print("Após verificar, você pode renomear o arquivo novo para substituir o antigo.")

if __name__ == "__main__":
    main()