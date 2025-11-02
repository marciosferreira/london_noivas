#!/usr/bin/env python3
"""
Script de teste para inserir múltiplos itens de prova na mesma data
para verificar se o problema está no salvamento ou na exibição.
"""

import boto3
import datetime
import uuid
import os
from decimal import Decimal
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Configurar DynamoDB com as mesmas credenciais do app
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)
fittings_table = dynamodb.Table("alugueqqc_fittings_table")

def _date_time_key(date_iso: str, time_local: str) -> str:
    """Função para criar a chave de data/hora igual à do sistema"""
    if not time_local or time_local.strip() == "":
        return f"{date_iso}#"
    return f"{date_iso}#{time_local}"

def create_test_fittings():
    """Cria múltiplos itens de prova para a mesma data"""
    
    # Data de teste (amanhã)
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    date_iso = tomorrow.strftime("%Y-%m-%d")
    
    # Account ID de teste (usando o mesmo do exemplo no sistema)
    account_id = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
    
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    # Criar 3 itens para a mesma data
    test_items = [
        {
            "time_local": "09:00",
            "client_name": "Maria Silva",
            "item_description": "Vestido de Noiva Modelo A",
            "status": "Confirmado",
            "notes": "Primeira prova"
        },
        {
            "time_local": "14:30",
            "client_name": "Ana Santos",
            "item_description": "Vestido de Noiva Modelo B", 
            "status": "Pendente",
            "notes": "Segunda prova"
        },
        {
            "time_local": "",  # Sem horário específico
            "client_name": "Carla Oliveira",
            "item_description": "Vestido de Festa",
            "status": "Confirmado", 
            "notes": "Terceira prova - sem horário específico"
        }
    ]
    
    print(f"Inserindo {len(test_items)} itens para a data: {date_iso}")
    
    for i, item_data in enumerate(test_items):
        fitting_id = str(uuid.uuid4())
        dt_key = _date_time_key(date_iso, item_data["time_local"])
        
        fitting_item = {
            "account_id": account_id,
            "date_time_local": dt_key,
            "fitting_id": fitting_id,
            "date_local": date_iso,
            "time_local": item_data["time_local"],
            "status": item_data["status"],
            "notes": item_data["notes"],
            "client_name": item_data["client_name"],
            "item_description": item_data["item_description"],
            "created_at": now_utc,
            "updated_at": now_utc,
            "created_by": "test_user",
        }
        
        try:
            fittings_table.put_item(Item=fitting_item)
            print(f"✓ Item {i+1} inserido: {dt_key} - {item_data['client_name']}")
        except Exception as e:
            print(f"✗ Erro ao inserir item {i+1}: {e}")
    
    print(f"\nTeste concluído! Verifique a agenda para a data {date_iso}")

def list_test_fittings():
    """Lista todos os itens de teste para verificar se foram salvos corretamente"""
    
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    date_iso = tomorrow.strftime("%Y-%m-%d")
    account_id = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
    
    try:
        # Buscar todos os itens para a data de teste
        from boto3.dynamodb.conditions import Key
        
        resp = fittings_table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("date_time_local").begins_with(date_iso),
        )
        
        items = resp.get("Items", [])
        print(f"\nItens encontrados para {date_iso}: {len(items)}")
        
        for i, item in enumerate(items):
            print(f"  {i+1}. {item['date_time_local']} - {item.get('client_name')} - {item.get('item_description')}")
            
    except Exception as e:
        print(f"Erro ao buscar itens: {e}")

if __name__ == "__main__":
    print("=== Teste de Múltiplos Itens na Mesma Data ===")
    
    # Primeiro, listar itens existentes
    list_test_fittings()
    
    # Criar novos itens de teste
    create_test_fittings()
    
    # Listar novamente para verificar
    list_test_fittings()