#!/usr/bin/env python3
"""
Script para debugar os dados da agenda
"""

import os
import boto3
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Key, Attr
import datetime

# Carrega variáveis de ambiente
load_dotenv()

# Configuração do DynamoDB
dynamodb = boto3.resource(
    'dynamodb',
    region_name='us-east-1',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
)

fittings_table = dynamodb.Table('alugueqqc_fittings_table')

def _next_dates_with_fittings(account_id: str, start_date_iso: str, count: int = None):
    # Busca TODAS as próximas datas com provas diretamente na tabela DynamoDB
    results = []
    try:
        print(f"Buscando provas para account_id: {account_id}, após data: {start_date_iso}")
        
        # Consulta direta na tabela para todas as datas futuras
        resp = fittings_table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("date_time_local").gt(start_date_iso),  # Maior que a data de início
            ScanIndexForward=True  # Ordena em ordem crescente
        )
        
        items = resp.get("Items", [])
        print(f"Total de itens encontrados: {len(items)}")
        
        for i, item in enumerate(items):
            print(f"Item {i}: {item}")
        
        # Agrupa os itens por data
        dates_dict = {}
        for item in items:
            date_part = item["date_time_local"][:10]  # Extrai apenas a parte da data (YYYY-MM-DD)
            if date_part not in dates_dict:
                dates_dict[date_part] = []
            dates_dict[date_part].append(item)
        
        print(f"Datas agrupadas: {list(dates_dict.keys())}")
        
        # Converte para o formato esperado e ordena por data
        for date_iso in sorted(dates_dict.keys()):
            print(f"Data {date_iso}: {len(dates_dict[date_iso])} itens")
            results.append({
                "date_iso": date_iso,
                "items": dates_dict[date_iso]
            })
                
    except Exception as e:
        print("Erro ao buscar próximos dias com provas:", e)
    return results

def test_function():
    # Account ID do usuário de teste que criamos
    account_id = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
    today_iso = "2025-01-01"  # Data anterior aos nossos testes
    
    print("=== TESTE DA FUNÇÃO _next_dates_with_fittings ===")
    result = _next_dates_with_fittings(account_id, today_iso)
    
    print(f"\nResultado final:")
    print(f"Número de datas encontradas: {len(result)}")
    
    for i, day in enumerate(result):
        print(f"\nDia {i}: {day['date_iso']}")
        print(f"  Número de itens: {len(day['items'])}")
        for j, item in enumerate(day['items']):
            print(f"  Item {j}: {item.get('client_name', 'Sem cliente')} - {item.get('item_description', 'Sem descrição')}")

if __name__ == "__main__":
    test_function()