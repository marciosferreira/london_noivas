import os
import pytz
import boto3
from dotenv import load_dotenv

from flask import Flask
from flask import Flask, request

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
init_auth_routes(app, users_table, reset_tokens_table)
init_item_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table, users_table
)
init_status_routes(app, itens_table, transactions_table, users_table)
init_transaction_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table, users_table
)
init_client_routes(app, clients_table, transactions_table, itens_table, users_table)
init_static_routes(app, ses_client, clients_table, transactions_table, itens_table)

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


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
