import boto3
from boto3.dynamodb.conditions import Key, Attr
from datetime import datetime, timezone
import pytz

# Configurar DynamoDB
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
fittings_table = dynamodb.Table('fittings')

def check_fittings_data():
    """Verifica os dados de fittings no banco para debug"""
    
    # Usar um account_id conhecido (você pode ajustar este valor)
    account_id = "user_6763b8b6e4b0a8c9d2f1e3a4"  # Substitua pelo seu account_id real
    
    print(f"Verificando dados para account_id: {account_id}")
    print("=" * 50)
    
    try:
        # Buscar todos os fittings para este account_id
        resp = fittings_table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
        )
        
        items = resp.get("Items", [])
        print(f"Total de fittings encontrados: {len(items)}")
        print()
        
        if not items:
            print("Nenhum fitting encontrado. Vamos criar alguns dados de teste.")
            create_test_data(account_id)
            return
        
        # Agrupar por data para análise
        dates_dict = {}
        for item in items:
            date_part = item["date_time_local"][:10]
            if date_part not in dates_dict:
                dates_dict[date_part] = []
            dates_dict[date_part].append(item)
        
        print("Dados agrupados por data:")
        print("-" * 30)
        
        for date_iso in sorted(dates_dict.keys()):
            items_for_date = dates_dict[date_iso]
            print(f"Data: {date_iso} - {len(items_for_date)} item(s)")
            
            for i, item in enumerate(items_for_date, 1):
                print(f"  {i}. ID: {item.get('fitting_id', 'N/A')}")
                print(f"     Cliente: {item.get('client_name', 'N/A')}")
                print(f"     Item: {item.get('item_description', 'N/A')}")
                print(f"     Horário: {item.get('time_local', 'N/A')}")
                print(f"     DateTime completo: {item.get('date_time_local', 'N/A')}")
                print()
        
    except Exception as e:
        print(f"Erro ao verificar dados: {e}")

def create_test_data(account_id):
    """Cria dados de teste com múltiplos fittings na mesma data"""
    
    print("Criando dados de teste...")
    
    # Data de teste (amanhã)
    tomorrow = datetime.now() + datetime.timedelta(days=1)
    date_iso = tomorrow.strftime("%Y-%m-%d")
    
    test_fittings = [
        {
            "account_id": account_id,
            "date_time_local": f"{date_iso}#09:00",
            "fitting_id": f"test_fitting_1_{datetime.now().timestamp()}",
            "client_name": "Cliente Teste 1",
            "item_description": "Vestido de Noiva A",
            "time_local": "09:00",
            "status": "scheduled",
            "notes": "Primeira prova"
        },
        {
            "account_id": account_id,
            "date_time_local": f"{date_iso}#14:00",
            "fitting_id": f"test_fitting_2_{datetime.now().timestamp()}",
            "client_name": "Cliente Teste 2", 
            "item_description": "Vestido de Festa B",
            "time_local": "14:00",
            "status": "scheduled",
            "notes": "Segunda prova"
        },
        {
            "account_id": account_id,
            "date_time_local": f"{date_iso}#16:30",
            "fitting_id": f"test_fitting_3_{datetime.now().timestamp()}",
            "client_name": "Cliente Teste 3",
            "item_description": "Terno C",
            "time_local": "16:30", 
            "status": "scheduled",
            "notes": "Terceira prova"
        }
    ]
    
    try:
        for fitting in test_fittings:
            fittings_table.put_item(Item=fitting)
            print(f"Criado fitting: {fitting['client_name']} às {fitting['time_local']}")
        
        print(f"\n✅ Criados 3 fittings de teste para {date_iso}")
        print("Agora você pode testar a agenda novamente!")
        
    except Exception as e:
        print(f"Erro ao criar dados de teste: {e}")

if __name__ == "__main__":
    check_fittings_data()