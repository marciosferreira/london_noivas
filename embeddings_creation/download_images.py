import csv
import os
import requests
import mimetypes

def download_images(csv_path, output_dir):
    # Criar diretório se não existir
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Diretório '{output_dir}' criado.")

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        count = 0
        success = 0
        
        print("Iniciando download...")
        
        for row in reader:
            image_url = row.get('image_url')
            item_id = row.get('id')
            
            if not image_url:
                continue
                
            count += 1
            
            try:
                print(f"Baixando imagem para ID {item_id}...")
                response = requests.get(image_url, timeout=10)
                response.raise_for_status()
                
                # Tentar determinar a extensão
                content_type = response.headers.get('content-type')
                extension = mimetypes.guess_extension(content_type)
                if not extension:
                    extension = '.jpg' # Fallback
                
                # Nome do arquivo
                filename = f"{item_id}{extension}"
                file_path = os.path.join(output_dir, filename)
                
                with open(file_path, 'wb') as img_file:
                    img_file.write(response.content)
                
                success += 1
                print(f"Salvo: {filename}")
                
            except Exception as e:
                print(f"Erro ao baixar {item_id}: {e}")

    print(f"\nConcluído! {success}/{count} imagens baixadas com sucesso.")

if __name__ == "__main__":
    # Configurações
    # Determinar o diretório onde o script está localizado
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # O arquivo CSV está no mesmo diretório do script
    CSV_FILE = os.path.join(script_dir, 'products-+5592993531943.csv')
    
    # O diretório de saída deve ser relativo à raiz do projeto (um nível acima)
    OUTPUT_DIR = os.path.join(script_dir, '..', 'static', 'dresses')
    
    download_images(CSV_FILE, OUTPUT_DIR)
