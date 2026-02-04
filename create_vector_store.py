import json
import os
import faiss
import numpy as np
from openai import OpenAI
from dotenv import load_dotenv
import pickle

# Carrega variáveis de ambiente
load_dotenv()

# Configuração
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DATASET_FILE = "vestidos_dataset.jsonl"
INDEX_FILE = "vector_store.index"
METADATA_FILE = "vector_store_metadata.pkl"
EMBEDDING_MODEL = "text-embedding-3-small"

def get_embedding(text):
    text = text.replace("\n", " ")
    return client.embeddings.create(input=[text], model=EMBEDDING_MODEL).data[0].embedding

def create_index():
    if not os.path.exists(DATASET_FILE):
        print(f"Arquivo {DATASET_FILE} não encontrado.")
        return

    descriptions = []
    metadata = []
    
    print("Lendo dataset...")
    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                descriptions.append(item["description"])
                metadata.append(item)
            except json.JSONDecodeError:
                continue

    if not descriptions:
        print("Nenhuma descrição encontrada.")
        return

    print(f"Gerando embeddings para {len(descriptions)} itens...")
    embeddings = []
    for i, desc in enumerate(descriptions):
        try:
            emb = get_embedding(desc)
            embeddings.append(emb)
            if (i + 1) % 5 == 0:
                print(f"Processado {i + 1}/{len(descriptions)}")
        except Exception as e:
            print(f"Erro ao gerar embedding para item {i}: {e}")
            # Se falhar, precisamos garantir que o metadata também seja removido ou lidar com o desalinhamento
            # Aqui, para simplificar, vamos assumir que se falhar, abortamos ou o usuário deve limpar o dataset
            # Mas vamos remover o último metadata adicionado para manter consistência
            metadata.pop()

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
