import os
import pytz
import boto3
from dotenv import load_dotenv

from flask import Flask
from flask import Flask, request, session

# Define o fuso horário de Manaus
load_dotenv()  # only for setting up the env as debug


import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # Backend

STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")  # Frontend
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")  # ID do plano


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
accounts_table = dynamodb.Table("alugueqqc_accounts_table")


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


# Initialize routes from modules
init_auth_routes(app, users_table, reset_tokens_table, accounts_table)
init_item_routes(
    app,
    itens_table,
    s3,
    s3_bucket_name,
    transactions_table,
    clients_table,
    users_table,
    text_models_table,
)
init_status_routes(app, itens_table, transactions_table, users_table)
init_transaction_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table, users_table
)
init_client_routes(
    app, clients_table, transactions_table, itens_table, users_table, text_models_table
)


init_static_routes(
    app,
    ses_client,
    clients_table,
    transactions_table,
    itens_table,
    text_models_table,
    users_table,
    accounts_table,
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


# Adiciona cache control para arquivos estáticos
@app.after_request
def add_header(response):
    path = request.path
    if path.startswith("/static/icons/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.endswith(".css") or path.endswith(".js"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.context_processor
def inject_session():
    return dict(session=session)


@app.before_request
def update_session_plan_type():
    if session.get("logged_in") and request.endpoint in [
        "all_transactions",
        "inventory",
    ]:
        account_id = session.get("account_id")
        if account_id:
            response = accounts_table.get_item(Key={"account_id": account_id})
            account = response.get("Item")
            if account:
                session["plan_type"] = account.get("plan_type", "free")
            else:
                print(
                    f"🔴 Atenção: Conta {account_id} não encontrada na accounts_table!"
                )


from datetime import datetime, timezone


@app.template_filter("datetimeformat")
def datetimeformat(value):
    if value:
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.strftime("%d/%m/%Y")
    return ""


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env", "false").lower() == "true"
    app.run(debug=debug_mode, host="localhost", port=5000)
