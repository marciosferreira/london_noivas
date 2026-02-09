import json
import os
import faiss
import numpy as np
from openai import OpenAI
from dotenv import load_dotenv
import pickle
import hashlib

# Carrega variáveis de ambiente
load_dotenv()

# Configuração de Arquivos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_FILE = os.path.join(BASE_DIR, "vestidos_dataset.jsonl")
INDEX_FILE = os.path.join(BASE_DIR, "..", "vector_store.index") # Salva na raiz ou onde for esperado
METADATA_FILE = os.path.join(BASE_DIR, "..", "vector_store_metadata.pkl")
CACHE_FILE = os.path.join(BASE_DIR, "embeddings_cache.pkl")

EMBEDDING_MODEL = "text-embedding-3-small"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_embedding(text):
    text = text.replace("\n", " ")
    return client.embeddings.create(input=[text], model=EMBEDDING_MODEL).data[0].embedding

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Erro ao carregar cache: {e}. Iniciando novo cache.")
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache, f)
        print(f"Cache salvo com {len(cache)} itens.")
    except Exception as e:
        print(f"Erro ao salvar cache: {e}")

def create_index():
    if not os.path.exists(DATASET_FILE):
        print(f"Arquivo {DATASET_FILE} não encontrado.")
        return

    # Carregar cache de embeddings
    embedding_cache = load_cache()
    print(f"Cache carregado: {len(embedding_cache)} embeddings existentes.")

    descriptions = []
    metadata = []
    
    print("Lendo dataset...")
    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                desc = item.get("embedding_text") or item.get("description")
                descriptions.append(desc)
                metadata.append(item)
            except json.JSONDecodeError:
                continue

    if not descriptions:
        print("Nenhuma descrição encontrada.")
        return

    print(f"Gerando/Recuperando embeddings para {len(descriptions)} itens...")
    embeddings = []
    new_embeddings_count = 0

    for i, desc in enumerate(descriptions):
        try:
            # Gera hash da descrição para usar como chave de cache
            desc_hash = hashlib.md5(desc.encode('utf-8')).hexdigest()
            
            if desc_hash in embedding_cache:
                emb = embedding_cache[desc_hash]
            else:
                emb = get_embedding(desc)
                embedding_cache[desc_hash] = emb
                new_embeddings_count += 1
            
            embeddings.append(emb)
            
            if (i + 1) % 10 == 0:
                print(f"Processado {i + 1}/{len(descriptions)}")
                
        except Exception as e:
            print(f"Erro ao obter embedding para item {i}: {e}")
            metadata.pop() # Remove metadata correspondente para manter alinhamento

    if new_embeddings_count > 0:
        print(f"Novos embeddings gerados via API: {new_embeddings_count}")
        save_cache(embedding_cache)
    else:
        print("Todos os embeddings foram recuperados do cache (Custo zero).")

    if not embeddings:
        print("Falha ao gerar embeddings.")
        return

    # Convertendo para numpy array
    embeddings_np = np.array(embeddings).astype('float32')
    
    # Criando índice FAISS
    dimension = embeddings_np.shape[1]
    index = faiss.IndexFlatL2(dimension)
    index.add(embeddings_np)
    
    # Salvando índice e metadados
    print("Salvando índice FAISS e metadados...")
    faiss.write_index(index, INDEX_FILE)
    with open(METADATA_FILE, "wb") as f:
        pickle.dump(metadata, f)
        
    print("Vector Store criada com sucesso!")

if __name__ == "__main__":
    create_index()
