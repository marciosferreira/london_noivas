import boto3
import datetime
from boto3.dynamodb.conditions import Key, Attr

# Configurar DynamoDB
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
fittings_table = dynamodb.Table("alugueqqc_fittings_table")

def _today_iso(user_tz=None):
    if user_tz is None:
        import pytz
        user_tz = pytz.timezone('America/Sao_Paulo')
    return datetime.datetime.now(user_tz).date().strftime("%Y-%m-%d")

def _next_dates_with_fittings(account_id: str, start_date_iso: str, count: int = None):
    # Busca TODAS as próximas datas com provas diretamente na tabela DynamoDB
    results = []
    try:
        # Consulta direta na tabela para todas as datas futuras
        resp = fittings_table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("date_time_local").gt(start_date_iso),  # Maior que a data de início
            ScanIndexForward=True  # Ordena em ordem crescente
        )
        
        items = resp.get("Items", [])
        print(f"Raw items from DynamoDB: {len(items)}")
        
        # Agrupa os itens por data
        dates_dict = {}
        for item in items:
            date_part = item["date_time_local"][:10]  # Extrai apenas a parte da data (YYYY-MM-DD)
            if date_part not in dates_dict:
                dates_dict[date_part] = []
            dates_dict[date_part].append(item)
        
        print(f"Dates dict: {dates_dict}")
        
        # Converte para o formato esperado e ordena por data
        for date_iso in sorted(dates_dict.keys()):
            day_data = {
                "date_iso": date_iso,
                "items": dates_dict[date_iso]
            }
            results.append(day_data)
            print(f"Added day: {date_iso} with {len(dates_dict[date_iso])} items")
            print(f"Type of items: {type(dates_dict[date_iso])}")
            print(f"Items content: {dates_dict[date_iso]}")
                
    except Exception as e:
        print("Erro ao buscar próximos dias com provas:", e)
    return results

def test_function():
    account_id = "acc_6749b8b4e4b0a8b8b8b8b8b8"
    today_iso = _today_iso()
    
    print(f"Testing with account_id: {account_id}")
    print(f"Today ISO: {today_iso}")
    
    next_days = _next_dates_with_fittings(account_id, today_iso)
    
    print(f"\nResult type: {type(next_days)}")
    print(f"Number of days: {len(next_days)}")
    
    for i, day in enumerate(next_days):
        print(f"\nDay {i+1}:")
        print(f"  date_iso: {day['date_iso']}")
        print(f"  items type: {type(day['items'])}")
        print(f"  items length: {len(day['items'])}")
        print(f"  items content: {day['items']}")

if __name__ == "__main__":
    test_function()