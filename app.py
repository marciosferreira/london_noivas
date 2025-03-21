import os
import pytz
import boto3
from dotenv import load_dotenv

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

# Define o fuso horário de Manaus
manaus_tz = pytz.timezone("America/Manaus")
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

# Import routing modules
from auth import init_auth_routes
from item_routes import init_item_routes
from status_routes import init_status_routes
from transaction_routes import init_transaction_routes


# Initialize routes from modules
init_auth_routes(app, users_table, reset_tokens_table)
init_item_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table
)
init_status_routes(app, itens_table, transactions_table, manaus_tz)

init_transaction_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table
)


# Static pages
@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/contato", methods=["GET", "POST"])
def contato():
    if request.method == "POST":
        nome = request.form.get("name")
        email = request.form.get("email")
        mensagem = request.form.get("message")

        print(nome)
        print(email)
        print(mensagem)

        if not nome or not email or not mensagem:
            flash("Todos os campos são obrigatórios.", "danger")
            return redirect(url_for("contato"))

        # Enviar e-mail via AWS SES
        destinatario = "contato@alugueqqc.com.br"
        assunto = f"Novo contato de {nome}"
        corpo_email = f"Nome: {nome}\nE-mail: {email}\n\nMensagem:\n{mensagem}"

        try:
            response = ses_client.send_email(
                Source=destinatario,
                Destination={"ToAddresses": [destinatario]},
                Message={
                    "Subject": {"Data": assunto, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": corpo_email, "Charset": "UTF-8"}},
                },
            )
            flash("Mensagem enviada com sucesso!", "success")
        except Exception as e:
            print(f"Erro ao enviar e-mail: {e}")
            flash("Erro ao enviar a mensagem. Tente novamente mais tarde.", "danger")

        return redirect(url_for("contato"))

    return render_template("contato.html")


@app.route("/")
def index():
    return render_template("index.html")  # Renderiza a página inicial


from flask import send_from_directory


@app.route("/ads.txt")
def ads_txt():
    return send_from_directory("static", "ads.txt")


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
