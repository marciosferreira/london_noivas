import os
import pytz
import boto3
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Key

from flask import Flask
from flask import Flask, request, session

# Define o fuso horário de Manaus
load_dotenv()  # only for setting up the env as debug

# Configurações AWS
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
s3_bucket_name = "alugueqqc-images"

# Initialize AWS resources
dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)


itens_table = dynamodb.Table("alugueqqc_itens")
users_table = dynamodb.Table("alugueqqc_users")
transactions_table = dynamodb.Table("alugueqqc_transactions")
clients_table = dynamodb.Table("alugueqqc_clients")
reset_tokens_table = dynamodb.Table("RentqqcResetTokens")
text_models_table = dynamodb.Table("alugue_qqc_text_models")
payment_transactions = dynamodb.Table("alugueqqc_payment_transactions")
custom_fields_table = dynamodb.Table("alugueqqc_custom_fields")
field_config_table = dynamodb.Table("alugueqqc_field_config_table")
fittings_table = dynamodb.Table("alugueqqc_fittings_table")


s3 = boto3.client(
    "s3",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

# Configuração AWS SES para envio de emails
ses_client = boto3.client(
    "ses",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)


# Create Flask app
app = Flask(__name__)
# Defina uma chave secreta forte e fixa
app.secret_key = os.environ.get("SECRET_KEY", "chave-secreta-estatica-e-forte-london")


from datetime import timedelta

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)  # ou outro valor
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True  # só se estiver usando HTTPS


# Import routing modules
from auth import init_auth_routes
from item_routes import init_item_routes
from status_routes import init_status_routes
from transaction_routes import init_transaction_routes
from client_routes import init_client_routes
from static_routes import init_static_routes
from datetime import datetime, timezone
from fittings_routes import init_fittings_routes
from ai_routes import ai_bp


# Initialize routes from modules
app.register_blueprint(ai_bp)

init_auth_routes(
    app, users_table, reset_tokens_table, payment_transactions, field_config_table
)
init_item_routes(
    app,
    itens_table,
    s3,
    s3_bucket_name,
    transactions_table,
    clients_table,
    users_table,
    text_models_table,
    payment_transactions,
    custom_fields_table,
    field_config_table,
)
init_status_routes(app, itens_table, transactions_table, users_table)
init_transaction_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table, users_table
)
init_client_routes(
    app,
    clients_table,
    transactions_table,
    itens_table,
    users_table,
    text_models_table,
    field_config_table,
)


init_static_routes(
    app,
    ses_client,
    clients_table,
    transactions_table,
    itens_table,
    text_models_table,
    users_table,
    payment_transactions,
    field_config_table,
)

# Provas e Agenda
init_fittings_routes(
    app,
    fittings_table,
    transactions_table,
    itens_table,
    clients_table,
    users_table,
)


# Rota para servir o service-worker.js
from flask import send_from_directory


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(".", "service-worker.js")


@app.after_request
def add_header(response):
    if request.path.startswith("/static/icons/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.context_processor
def inject_session():
    return dict(session=session)


@app.template_filter("datetimeformat")
def datetimeformat(value):
    if value is None:
        return "-"
    # 🔥 Converte para float se vier como Decimal
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "-"
    dt = datetime.fromtimestamp(value, tz=timezone.utc)
    return dt.strftime("%d/%m/%Y %H:%M")



from datetime import datetime

@app.template_filter("formatar_data_br")
def formatar_data_br(data_iso):
    print('data iso')
    print(data_iso)
    if not data_iso:
        print("no iso")
        return "-"
    try:
        # Primeiro tenta como data completa com hora
        dt = datetime.strptime(data_iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            # Depois tenta só a parte da data
            dt = datetime.strptime(data_iso, "%Y-%m-%d")
        except ValueError:
            return data_iso  # formato inesperado, retorna como está
    return dt.strftime("%d/%m/%Y")


@app.template_filter("formatar_data_com_dia_semana")
def formatar_data_com_dia_semana(data_iso):
    """Formata data incluindo o dia da semana em português"""
    if not data_iso:
        return "-"
    
    # Mapeamento dos dias da semana em português
    dias_semana = {
        0: "Segunda-feira",
        1: "Terça-feira", 
        2: "Quarta-feira",
        3: "Quinta-feira",
        4: "Sexta-feira",
        5: "Sábado",
        6: "Domingo"
    }
    
    try:
        # Primeiro tenta como data completa com hora
        dt = datetime.strptime(data_iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            # Depois tenta só a parte da data
            dt = datetime.strptime(data_iso, "%Y-%m-%d")
        except ValueError:
            return data_iso  # formato inesperado, retorna como está
    
    # Obtém o dia da semana (0=segunda, 6=domingo)
    dia_semana = dias_semana[dt.weekday()]
    data_formatada = dt.strftime("%d/%m/%Y")
    
    return f"{dia_semana}, {data_formatada}"


@app.template_filter("format_brl")
def format_brl(value):
    print(value)
    try:
        return (
            f"{float(value):,.2f}".replace(",", "v").replace(".", ",").replace("v", ".")
        )
    except:
        return value


@app.template_filter("format_date")
def format_date(value):
    if not value:
        return "-"
    try:
        if isinstance(value, str) and "-" in value:
            parts = value.split("-")
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
        value = float(value)
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return value


import re


def format_cpf(value):
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return value


def format_cnpj(value):
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return value


def format_phone(value):
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    return value

from decimal import Decimal

def format_currency(value):
    if value is None:
        return ""
    try:
        return "{:,.2f}".format(float(value)).replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(value)


# Registrar filtros no Jinja
app.jinja_env.filters["format_cpf"] = format_cpf
app.jinja_env.filters["format_cnpj"] = format_cnpj
app.jinja_env.filters["format_phone"] = format_phone
app.jinja_env.filters["format_currency"] = format_currency

from datetime import datetime

@app.context_processor
def inject_now():
    return {'now': datetime.now}

if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env", "false").lower() == "true"
    app.run(debug=True, host="localhost", port=5000)
