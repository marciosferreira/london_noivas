import boto3
import os
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

def add_category_field_config():
    # Configuração
    ACCOUNT_ID = 'london_noivas'  # ID fixo da conta
    ENTITY = 'item'
    
    # Conecta ao DynamoDB
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    field_config_table = dynamodb.Table('alugueqqc_field_config')
    
    print(f"Atualizando configuração de campos para: {ACCOUNT_ID} -> {ENTITY}")
    
    try:
        # 1. Busca configuração atual
        response = field_config_table.get_item(
            Key={
                'account_id': ACCOUNT_ID,
                'entity': ENTITY
            }
        )
        
        item = response.get('Item')
        if not item:
            print("Configuração não encontrada! Criando nova...")
            fields_config = {}
        else:
            fields_config = item.get('fields_config', {})
            
        # 2. Define o novo campo 'category'
        new_field = {
            "label": "Categoria",
            "type": "dropdown",
            "options": ["Noiva", "Festa"],
            "required": True,
            "visible": True,
            "filterable": True,
            "preview": True,
            "f_type": "fixed", # Fixo para não ser deletado facilmente
            "order_sequence": 3 # Logo após Título (1) e Descrição (2)
        }
        
        # 3. Adiciona ou Atualiza no dicionário
        fields_config['category'] = new_field
        
        # 4. Salva de volta no DynamoDB
        field_config_table.put_item(
            Item={
                'account_id': ACCOUNT_ID,
                'entity': ENTITY,
                'fields_config': fields_config,
                'updated_at': '2025-02-08T12:00:00' # Data arbitrária ou atual
            }
        )
        
        print("Sucesso! Campo 'category' adicionado à configuração.")
        print(f"Opções configuradas: {new_field['options']}")
        
    except Exception as e:
        print(f"Erro ao atualizar configuração: {e}")

if __name__ == "__main__":
    add_category_field_config()