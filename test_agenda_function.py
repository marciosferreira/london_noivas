#!/usr/bin/env python3
"""
Script para testar diretamente a função _next_dates_with_fittings
"""

import boto3
import datetime
import os
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Key

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

def _next_dates_with_fittings(account_id, today_iso=None):
    """Cópia da função _next_dates_with_fittings do sistema"""
    if today_iso is None:
        today_iso = datetime.date.today().strftime("%Y-%m-%d")
    
    try:
        # Buscar todos os itens futuros
        resp = fittings_table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("date_time_local").gte(today_iso),
        )
        
        items = resp.get("Items", [])
        print(f"DEBUG: Total de itens encontrados: {len(items)}")
        
        # Agrupar por data
        dates_dict = {}
        for item in items:
            date_time_local = item["date_time_local"]
            date_part = date_time_local.split("#")[0]  # Extrair apenas a parte da data
            
            if date_part not in dates_dict:
                dates_dict[date_part] = []
            dates_dict[date_part].append(item)
        
        print(f"DEBUG: Datas agrupadas: {len(dates_dict)} datas")
        for date, items_list in dates_dict.items():
            print(f"  - {date}: {len(items_list)} itens")
        
        # Converter para lista ordenada
        result = []
        for date_iso in sorted(dates_dict.keys()):
            result.append({
                "date_iso": date_iso,
                "date": date_iso,  # Pode ser formatado depois
                "items": dates_dict[date_iso]
            })
        
        return result
        
    except Exception as e:
        print(f"Erro ao buscar próximas datas: {e}")
        return []

def test_function():
    """Testa a função com o account_id de exemplo"""
    account_id = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
    today_iso = datetime.date.today().strftime("%Y-%m-%d")
    
    print(f"=== Testando _next_dates_with_fittings ===")
    print(f"Account ID: {account_id}")
    print(f"Data de hoje: {today_iso}")
    print()
    
    result = _next_dates_with_fittings(account_id, today_iso)
    
    print(f"\nResultado final: {len(result)} datas com provas")
    for day in result:
        print(f"\nData: {day['date_iso']}")
        print(f"Número de itens: {len(day['items'])}")
        for i, item in enumerate(day['items']):
            print(f"  {i+1}. {item['date_time_local']} - {item.get('client_name', 'Sem cliente')} - {item.get('item_description', 'Sem descrição')}")

if __name__ == "__main__":
    test_function()