import json
import uuid
import os

# Define os arquivos de entrada e saída
INPUT_FILE = 'vestidos_dataset_updated.jsonl'
# Se não existir o updated, tenta o original
if not os.path.exists(INPUT_FILE):
    INPUT_FILE = 'vestidos_dataset.jsonl'

# Arquivo temporário para escrita segura
TEMP_FILE = 'vestidos_dataset_temp.jsonl'

def generate_site_id():
    """
    Gera um ID seguindo o padrão do site encontrado em item_routes.py.
    Padrão: 12 primeiros caracteres hexadecimais de um UUID4.
    """
    return str(uuid.uuid4().hex[:12])

def process_file():
    if not os.path.exists(INPUT_FILE):
        print(f"Erro: Arquivo {INPUT_FILE} não encontrado.")
        return

    print(f"Lendo {INPUT_FILE}...")
    
    count = 0
    updated_count = 0
    
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as infile, \
             open(TEMP_FILE, 'w', encoding='utf-8') as outfile:
            
            for line in infile:
                if not line.strip():
                    continue
                    
                try:
                    item = json.loads(line)
                    count += 1
                    
                    # Verifica se precisa de ID (cria se não existir ou for vazio)
                    if 'custom_id' not in item or not item['custom_id']:
                        item['custom_id'] = generate_site_id()
                        updated_count += 1
                    
                    outfile.write(json.dumps(item, ensure_ascii=False) + '\n')
                    
                except json.JSONDecodeError as e:
                    print(f"Erro ao decodificar JSON na linha {count + 1}: {e}")

        print(f"Processamento concluído.")
        print(f"Total de itens processados: {count}")
        print(f"IDs gerados: {updated_count}")
        
        # Substitui o arquivo original pelo novo se houve alterações
        if updated_count > 0:
            try:
                # Remove o arquivo original e renomeia o temporário
                if os.path.exists(INPUT_FILE):
                    os.remove(INPUT_FILE)
                os.rename(TEMP_FILE, INPUT_FILE)
                print(f"Arquivo {INPUT_FILE} atualizado com sucesso.")
            except OSError as e:
                print(f"Erro ao atualizar o arquivo original: {e}")
                print(f"O arquivo com os novos IDs está salvo em: {TEMP_FILE}")
        else:
            if os.path.exists(TEMP_FILE):
                os.remove(TEMP_FILE)
            print("Nenhum item precisou de atualização (todos já possuíam custom_id).")

    except Exception as e:
        print(f"Erro crítico durante o processamento: {e}")

if __name__ == "__main__":
    process_file()