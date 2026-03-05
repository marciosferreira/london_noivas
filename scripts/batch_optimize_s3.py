import boto3
import os
import io
import sys
from PIL import Image
from dotenv import load_dotenv

# Adiciona o diretório raiz ao path para importar utils
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import optimize_image

# Carrega variáveis de ambiente do arquivo .env na raiz do projeto
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
load_dotenv(env_path)

S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

if not S3_BUCKET_NAME:
    print("Erro: S3_BUCKET_NAME não configurado no .env")
    sys.exit(1)

def batch_optimize_images():
    print(f"Iniciando otimização em massa no bucket: {S3_BUCKET_NAME}")
    
    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION
    )

    paginator = s3.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=S3_BUCKET_NAME)

    processed_count = 0
    error_count = 0
    skipped_count = 0

    for page in page_iterator:
        if 'Contents' not in page:
            continue

        for obj in page['Contents']:
            key = obj['Key']
            size = obj['Size']
            
            # Filtra apenas imagens comuns
            if not key.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                continue
                
            print(f"Processando: {key} ({size/1024:.2f} KB)...", end=" ", flush=True)

            try:
                # Download do arquivo para memória
                response = s3.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                file_content = response['Body'].read()
                image_file = io.BytesIO(file_content)
                image_file.filename = key # Simula atributo filename para compatibilidade se necessário

                # Otimiza a imagem
                # optimize_image espera um objeto tipo arquivo e retorna BytesIO ou o original
                # A função optimize_image já trata seek(0)
                optimized_output = optimize_image(image_file)
                
                # Verifica se houve otimização real (se retornou BytesIO diferente do input)
                # Na nossa implementação atual, optimize_image sempre retorna um BytesIO novo se der certo,
                # ou o original se der erro.
                
                # Para saber se vale a pena substituir, podemos comparar tamanhos?
                # O usuário pediu para aplicar o ajuste. Vamos aplicar.
                # Mas se a imagem já for pequena e otimizada, o PIL pode até aumentar se a qualidade for diferente.
                # Vamos confiar na função optimize_image.
                
                optimized_size = optimized_output.getbuffer().nbytes
                
                # Se o tamanho otimizado for MAIOR que o original, talvez não valha a pena?
                # Exceto se a dimensão for gigante. A função optimize_image reduz dimensão.
                # Se reduziu dimensão, mesmo que o tamanho em bytes não caia muito (difícil), vale pela dimensão.
                # Se a dimensão não mudou (já era pequena) e o tamanho aumentou (recompressão JPEG), talvez pular?
                # Mas para padronização (tudo JPEG), melhor sobrescrever.
                
                # Upload de volta (Sobrescreve)
                # Importante: Definir ContentType correto
                content_type = 'image/jpeg' # optimize_image sempre converte para JPEG
                
                s3.put_object(
                    Bucket=S3_BUCKET_NAME,
                    Key=key,
                    Body=optimized_output.getvalue(),
                    ContentType=content_type,
                    CacheControl='max-age=31536000' # Opcional: Cache longo
                )
                
                reduction = (1 - (optimized_size / size)) * 100
                print(f"OK! Redução: {reduction:.1f}% ({optimized_size/1024:.2f} KB)")
                processed_count += 1

            except Exception as e:
                print(f"ERRO: {e}")
                error_count += 1

    print("\n--- Resumo ---")
    print(f"Processados: {processed_count}")
    print(f"Erros: {error_count}")
    print(f"Pulados: {skipped_count}")

if __name__ == "__main__":
    batch_optimize_images()
