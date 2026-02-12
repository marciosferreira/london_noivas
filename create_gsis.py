import boto3
import os
import time
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

def create_occasion_gsis():
    print("--- Criando GSIs de Ocasião ---")
    
    # Configuração DynamoDB
    dynamodb = boto3.client(
        'dynamodb',
        region_name=os.getenv('AWS_REGION', 'us-east-1'),
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
    )
    
    table_name = "alugueqqc_itens"
    slugs = ["madrinha", "formatura", "gala", "debutante", "convidada", "mae_dos_noivos", "noiva", "civil"]
    
    try:
        # Verifica se a tabela é PAY_PER_REQUEST ou PROVISIONED
        desc = dynamodb.describe_table(TableName=table_name)
        billing_mode = desc['Table'].get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
        
        # Lista índices existentes
        existing_indexes = [gsi['IndexName'] for gsi in desc['Table'].get('GlobalSecondaryIndexes', [])]
        print(f"Índices existentes: {existing_indexes}")
        
        updates = []
        for slug in slugs:
            index_name = f"occasion_{slug}-index"
            attr_name = f"occasion_{slug}"
            
            if index_name in existing_indexes:
                print(f"[SKIP] Índice {index_name} já existe.")
                continue
                
            print(f"[PLAN] Criando índice: {index_name}")
            
            # Definição do índice
            create_index = {
                "Create": {
                    "IndexName": index_name,
                    "KeySchema": [
                        {"AttributeName": attr_name, "KeyType": "HASH"},
                        {"AttributeName": "account_id", "KeyType": "RANGE"}
                    ],
                    "Projection": {
                        "ProjectionType": "ALL"
                    }
                }
            }
            
            # Se for provisionado, precisa definir throughput
            if billing_mode != 'PAY_PER_REQUEST':
                create_index["Create"]["ProvisionedThroughput"] = {
                    "ReadCapacityUnits": 1,
                    "WriteCapacityUnits": 1
                }
            
            updates.append(create_index)

        if not updates:
            print("Nenhum índice novo para criar.")
            return

        # DynamoDB permite apenas uma operação de GSI por vez em update_table (geralmente)
        # Mas vamos tentar enviar um update com AttributeDefinitions necessários
        
        # Prepara definições de atributos
        attr_defs = []
        # Adiciona account_id se não estiver
        current_attrs = {attr['AttributeName'] for attr in desc['Table']['AttributeDefinitions']}
        if 'account_id' not in current_attrs:
            attr_defs.append({'AttributeName': 'account_id', 'AttributeType': 'S'})
            
        for update in updates:
            idx_name = update['Create']['IndexName']
            attr_name = update['Create']['KeySchema'][0]['AttributeName'] # Hash Key
            if attr_name not in current_attrs:
                # Verifica se já adicionamos na lista de attr_defs deste batch
                if not any(a['AttributeName'] == attr_name for a in attr_defs):
                    attr_defs.append({'AttributeName': attr_name, 'AttributeType': 'S'})

        # Executa as atualizações uma por uma (mais seguro para evitar erro de LimitExceeded)
        for update in updates:
            idx_name = update['Create']['IndexName']
            print(f"Executando criação de {idx_name}...")
            
            # Filtra apenas os atributos necessários para este índice
            needed_attrs = []
            target_attr = update['Create']['KeySchema'][0]['AttributeName']
            
            # Verifica se precisamos definir account_id ou o atributo da ocasião
            # Note: AttributeDefinitions deve conter APENAS os atributos usados nas chaves
            # Se eles já existem na tabela, não precisamos reenviar, mas a API aceita se enviarmos.
            # O mais seguro é enviar os que estamos usando neste update.
            
            # Monta lista de atributos para este update específico
            my_attr_defs = []
            
            # Adiciona account_id
            my_attr_defs.append({'AttributeName': 'account_id', 'AttributeType': 'S'})
            
            # Adiciona o atributo alvo (ex: occasion_madrinha)
            # DynamoDB exige que TODOS os atributos usados na chave estejam definidos em AttributeDefinitions
            my_attr_defs.append({'AttributeName': target_attr, 'AttributeType': 'S'})
                 
            try:
                # Se não houver novos atributos a definir, não passamos AttributeDefinitions
                kwargs = {
                    'TableName': table_name,
                    'GlobalSecondaryIndexUpdates': [update],
                    'AttributeDefinitions': my_attr_defs
                }
                    
                dynamodb.update_table(**kwargs)
                print(f"   [OK] Solicitação enviada para {idx_name}")
                
                # Atualiza lista de atributos conhecidos para as próximas iterações não tentarem re-criar
                current_attrs.add(target_attr)
                current_attrs.add('account_id')

                # Espera um pouco para não estourar limite de operações simultâneas se houver
                # Na verdade, DynamoDB só permite um GSI criando por vez (Active -> Updating -> Active)
                # Então precisamos esperar ficar ACTIVE antes do próximo.
                
                print("   Aguardando índice ficar ACTIVE (pode demorar)...")
                while True:
                    time.sleep(5)
                    try:
                        d = dynamodb.describe_table(TableName=table_name)
                        # Verifica status do índice
                        gsis = d['Table'].get('GlobalSecondaryIndexes', [])
                        target_gsi = next((g for g in gsis if g['IndexName'] == idx_name), None)
                        
                        if target_gsi:
                            status = target_gsi.get('IndexStatus')
                            print(f"   Status atual: {status}")
                            if status == 'ACTIVE':
                                break
                            if status == 'CREATING':
                                continue
                            if status == 'FAILED':
                                print(f"   [ERRO] Falha ao criar índice {idx_name}")
                                break
                        else:
                            # Ainda não apareceu?
                            pass
                    except Exception as e:
                        print(f"   [WARN] Erro ao checar status: {e}. Retentando...")
                        time.sleep(5)
                
            except ClientError as e:
                print(f"   [ERRO] {e}")

    except Exception as e:
        print(f"Erro fatal: {e}")

if __name__ == "__main__":
    create_occasion_gsis()
