import os
import time
import uuid
import secrets
import datetime
import pytz

# from functools import wraps
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

# from boto3.dynamodb.conditions import Attr

from dotenv import load_dotenv

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash


# Define o fuso horário de Manaus
manaus_tz = pytz.timezone("America/Manaus")
load_dotenv()  # only for setting up the env as debug
app = Flask(__name__)
# Defina uma chave secreta forte e fixa
app.secret_key = os.environ.get("SECRET_KEY", "chave-secreta-estatica-e-forte-london")
# Configurações AWS
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
dynamodb_table_name = "alugueqqc_itens"
s3_bucket_name = "alugueqqc-images"

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

itens_table = dynamodb.Table(dynamodb_table_name)

# Adicione uma nova tabela para usuários
users_table_name = "alugueqqc_users"
users_table = dynamodb.Table(users_table_name)

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

# Tabela para armazenar tokens de redefinição de senha
reset_tokens_table_name = "RentqqcResetTokens"
reset_tokens_table = dynamodb.Table(reset_tokens_table_name)


# Função para enviar email de recuperação de senha
def send_password_reset_email(email, username, reset_link):
    SENDER = "nao_responda@alugueqqc.com.br"  # Deve ser um email verificado no SES
    RECIPIENT = email
    SUBJECT = "Alugue QQC - Recuperação de Senha"

    # O corpo do email em HTML
    BODY_HTML = f"""
    <html>
    <head></head>
    <body>
    <h1>Alugue QQC - Recuperação de Senha</h1>
    <p>Olá {username},</p>
    <p>Recebemos uma solicitação para redefinir sua senha. Se você não solicitou isso, por favor ignore este email.</p>
    <p>Para redefinir sua senha, clique no link abaixo:</p>
    <p><a href="{reset_link}">Redefinir minha senha</a></p>
    <p>Este link é válido por 24 horas.</p>
    <p>Atenciosamente,<br>Equipe Alugue QQC</p>
    </body>
    </html>
    """

    # O corpo do email em texto simples para clientes que não suportam HTML
    BODY_TEXT = f"""
    Alugue QQC - Recuperação de Senha

    Olá {username},

    Recebemos uma solicitação para redefinir sua senha. Se você não solicitou isso, por favor ignore este email.

    Para redefinir sua senha, acesse o link:
    {reset_link}

    Este link é válido por 24 horas.

    Atenciosamente,
    Equipe Alugue QQC
    """

    try:
        response = ses_client.send_email(
            Source=SENDER,
            Destination={
                "ToAddresses": [
                    RECIPIENT,
                ],
            },
            Message={
                "Subject": {"Data": SUBJECT, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": BODY_TEXT, "Charset": "UTF-8"},
                    "Html": {"Data": BODY_HTML, "Charset": "UTF-8"},
                },
            },
        )
        return True
    except ClientError as e:
        print(f"Erro ao enviar email: {e}")
        return False


@app.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = request.form.get("email")
    if not email:
        return render_template("login.html", error="Por favor, informe seu email")

    # Buscar usuário pelo email no GSI
    response = users_table.query(
        IndexName="email-index",  # Usamos o GSI
        KeyConditionExpression="email = :email",
        ExpressionAttributeValues={":email": email},
    )

    if response.get("Items"):
        user = response["Items"][0]
        user_id = user["user_id"]
        username = user["username"]

        # Gerar token único para redefinição de senha
        token = str(uuid.uuid4())

        # Gerar timestamp UNIX para expiração (24h a partir de agora)
        expires_at_unix = int(time.time()) + 24 * 3600

        # Salvar token no DynamoDB
        reset_tokens_table.put_item(
            Item={
                "token": token,
                "user_id": user_id,
                "expires_at_unix": expires_at_unix,  # Agora é um timestamp UNIX
                "used": False,
            }
        )

        # Montar o link de redefinição de senha
        reset_link = f"{request.host_url.rstrip('/')}/reset-password/{token}"

        # Enviar email de recuperação de senha
        send_password_reset_email(email, username, reset_link)

        # Se a conta ainda não foi confirmada, reenviaremos o e-mail de confirmação também
        if not user.get("email_confirmed", False):
            email_token = secrets.token_urlsafe(16)

            # Atualizar banco com um novo token de confirmação
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET email_token = :token",
                ExpressionAttributeValues={":token": email_token},
            )

            # Montar link de confirmação
            confirm_url = url_for("confirm_email", token=email_token, _external=True)

            # Enviar e-mail de confirmação novamente
            send_confirmation_email(email, username, confirm_url)

            flash(
                "Você solicitou redefinição de senha, mas ainda precisa confirmar seu e-mail!",
                "info",
            )

        return render_template(
            "login.html",
            message="Se este email estiver cadastrado, enviaremos instruções para redefinir sua senha.",
        )

    # Mesmo se o email não existir, retornamos a mesma mensagem por segurança
    return render_template(
        "login.html",
        message="Se este email estiver cadastrado, enviaremos instruções para redefinir sua senha.",
    )


@app.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token):
    try:
        # Buscar token no DynamoDB
        response = reset_tokens_table.get_item(Key={"token": token})

        # Se o token não existir, pode ter sido deletado pelo TTL
        if "Item" not in response:
            return render_template(
                "login.html", error="Este link de redefinição é inválido ou expirou."
            )

        token_data = response["Item"]

        # Verificar se o token já foi usado
        if token_data.get("used", False):
            return render_template(
                "login.html", error="Este link de redefinição já foi usado."
            )

        # Verificar se o token expirou (caso ainda esteja na tabela)
        expires_at_unix = token_data.get("expires_at_unix")

        if expires_at_unix and time.time() > expires_at_unix:
            return render_template(
                "login.html", error="Este link de redefinição expirou."
            )

        # Token válido, mostrar página de redefinição
        return render_template("reset_password.html", reset_password=True, token=token)

    except Exception as e:
        print(f"Erro ao verificar token: {e}")
        return render_template(
            "login.html", error="Ocorreu um erro ao processar sua solicitação."
        )


# Rota para processar a redefinição de senha
@app.route("/reset-password", methods=["POST"])
def reset_password():
    token = request.form.get("token")
    new_password = request.form.get("new_password")
    confirm_new_password = request.form.get("confirm_new_password")

    if not token or not new_password or not confirm_new_password:
        return render_template(
            "login.html",
            error="Todos os campos são obrigatórios",
            reset_password=True,
            token=token,
        )

    if new_password != confirm_new_password:
        return render_template(
            "login.html",
            error="As senhas não coincidem",
            reset_password=True,
            token=token,
        )

    try:
        # Verificar se o token existe e é válido
        response = reset_tokens_table.get_item(Key={"token": token})

        if "Item" in response:
            token_data = response["Item"]

            # Verificar se o token já foi usado
            if token_data.get("used", False):
                return render_template(
                    "login.html", error="Este link de redefinição já foi usado"
                )

            # Remover milissegundos da data de expiração
            expires_at_str = token_data["expires_at"]
            if "." in expires_at_str:
                expires_at_str = expires_at_str.split(".")[0]
            expires_at = datetime.datetime.fromisoformat(expires_at_str)

            # Verificar se o token expirou
            if datetime.datetime.now() > expires_at:
                return render_template(
                    "login.html", error="Este link de redefinição expirou"
                )

            # Token válido, obter user_id associado ao token
            user_id = token_data["user_id"]  # 🔥 Agora usamos user_id, não email
            password_hash = generate_password_hash(new_password)

            # Atualizar senha no banco de dados
            users_table.update_item(
                Key={"user_id": user_id},  # 🔥 Atualizando pelo user_id
                UpdateExpression="SET password_hash = :p, updated_at = :u",
                ExpressionAttributeValues={
                    ":p": password_hash,
                    ":u": datetime.datetime.now().isoformat(),
                },
            )

            # Marcar o token como usado
            reset_tokens_table.update_item(
                Key={"token": token},
                UpdateExpression="SET used = :u",
                ExpressionAttributeValues={":u": True},
            )

            return render_template(
                "login.html",
                message="Senha redefinida com sucesso! Faça login com sua nova senha.",
            )

        else:
            return render_template("login.html", error="Link de redefinição inválido")

    except Exception as e:
        print(f"Erro ao redefinir senha: {e}")
        return render_template(
            "login.html",
            error="Ocorreu um erro ao processar sua solicitação",
            reset_password=True,
            token=token,
        )


def send_confirmation_email(email, username, email_token):
    SENDER = "nao_responda@alugueqqc.com.br"  # Deve ser um email verificado no SES
    RECIPIENT = email
    SUBJECT = "Alugue QQC - Confirmação de E-mail"

    # Gerar a URL de confirmação
    confirm_url = url_for("confirm_email", token=email_token, _external=True)

    # Corpo do e-mail em HTML
    BODY_HTML = f"""
    <html>
    <head></head>
    <body>
    <h1>Confirmação de E-mail</h1>
    <p>Olá <strong>{username}</strong>,</p>
    <p>Obrigado por se cadastrar no Alugue QQC!</p>
    <p>Para ativar sua conta, clique no link abaixo:</p>
    <p><a href="{confirm_url}" style="font-size:16px; font-weight:bold; color:#ffffff; background-color:#007bff; padding:10px 20px; text-decoration:none; border-radius:5px;">Confirmar Meu E-mail</a></p>
    <p>Se o botão acima não funcionar, copie e cole o seguinte link no seu navegador:</p>
    <p><a href="{confirm_url}">{confirm_url}</a></p>
    <p>Atenciosamente,<br>Equipe Alugue QQC</p>
    </body>
    </html>
    """

    # Corpo do e-mail em texto puro (caso o cliente de e-mail não suporte HTML)
    BODY_TEXT = f"""
    Confirmação de E-mail

    Olá {username},

    Obrigado por se cadastrar no Alugue QQC!

    Para ativar sua conta, clique no link abaixo:
    {confirm_url}

    Se você não se cadastrou no Alugue QQC, ignore este e-mail.

    Atenciosamente,
    Equipe Alugue QQC
    """

    try:
        response = ses_client.send_email(
            Source=SENDER,
            Destination={"ToAddresses": [RECIPIENT]},
            Message={
                "Subject": {"Data": SUBJECT, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": BODY_TEXT, "Charset": "UTF-8"},  # Texto puro
                    "Html": {"Data": BODY_HTML, "Charset": "UTF-8"},  # HTML formatado
                },
            },
        )
        print(f"E-mail de confirmação enviado para {email}: {response}")
        return True
    except Exception as e:
        print(f"Erro ao enviar e-mail de confirmação: {e}")
        return False


@app.route("/resend_confirmation")
def resend_confirmation():
    email = request.args.get("email")

    if not email:
        flash("E-mail inválido.", "danger")
        return redirect(url_for("login"))

    try:
        response = users_table.query(
            IndexName="email-index",  # Nome do GSI
            KeyConditionExpression="email = :email",
            ExpressionAttributeValues={":email": email},
        )
        if "Item" not in response:
            flash("E-mail não encontrado.", "danger")
            return redirect(url_for("login"))

        user = response["Item"]

        # Se já está confirmado, não precisa reenviar
        if user.get("email_confirmed", False):
            flash("Este e-mail já foi confirmado. Faça login.", "info")
            return redirect(url_for("login"))

        # Verificar tempo desde o último envio
        last_email_sent = user.get("last_email_sent", None)
        now = datetime.datetime.now()
        cooldown_seconds = 180  # 5 minutos de cooldown

        if last_email_sent:
            last_email_sent_time = datetime.date.fromisoformat(last_email_sent)
            seconds_since_last_email = (now - last_email_sent_time).total_seconds()

            if seconds_since_last_email < cooldown_seconds:
                flash(
                    f"Você já solicitou um reenvio recentemente. Aguarde {int(cooldown_seconds - seconds_since_last_email)} segundos.",
                    "warning",
                )
                return redirect(url_for("login"))

        # Gerar um novo token e atualizar o banco
        email_token = secrets.token_urlsafe(16)

        # Primeiro, buscar o user_id pelo email no GSI
        response = users_table.query(
            IndexName="email-index",  # Nome do GSI
            KeyConditionExpression="email = :email",
            ExpressionAttributeValues={":email": email},
        )

        items = response.get("Items", [])
        if not items:
            raise ValueError("Usuário não encontrado para o email: " + email)

        user_id = items[0]["user_id"]  # Obtendo o user_id correto

        # Agora atualizar o email_token na tabela principal usando o user_id
        users_table.update_item(
            Key={
                "user_id": user_id
            },  # Alterado para user_id, que é a Partition Key correta
            UpdateExpression="SET email_token = :token, last_email_sent = :time",
            ExpressionAttributeValues={":token": email_token, ":time": now.isoformat()},
        )

        # Reenviar e-mail
        send_confirmation_email(email, user["username"], email_token)

        flash("Um novo e-mail de confirmação foi enviado.", "success")
        return redirect(url_for("login"))

    except Exception as e:
        print(f"Erro ao reenviar e-mail: {e}")
        flash("Ocorreu um erro ao reenviar o e-mail. Tente novamente.", "danger")
        return redirect(url_for("login"))


@app.route("/confirm_email/<token>")
def confirm_email(token):
    try:
        # Buscar usuário pelo token no GSI
        response = users_table.query(
            IndexName="email_token-index",  # Nome do GSI
            KeyConditionExpression="email_token = :token",
            ExpressionAttributeValues={":token": token},
        )

        if "Items" in response and response["Items"]:
            user = response["Items"][0]
            user_id = user["user_id"]  # Obtendo o user_id correto

            # Confirmar o email e remover o token
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET email_confirmed = :confirmed REMOVE email_token",
                ExpressionAttributeValues={":confirmed": True},
            )

            flash(
                "Seu e-mail foi confirmado com sucesso! Agora você pode fazer login.",
                "success",
            )
            return redirect(url_for("login"))

        else:
            flash("Token inválido ou expirado.", "danger")
            return redirect(url_for("login"))

    except Exception as e:
        print(f"Erro ao confirmar e-mail: {e}")
        flash("Ocorreu um erro ao confirmar seu e-mail. Tente novamente.", "danger")
        return redirect(url_for("login"))


def get_all_users():
    """Função para administradores recuperarem todos os usuários"""
    try:
        response = users_table.scan()
        return response.get("Items", [])
    except Exception as e:
        print(f"Erro ao recuperar usuários: {e}")
        return []


@app.route("/change-password", methods=["POST"])
def change_password():
    if "email" not in session:
        flash("Você precisa estar logado para alterar a senha.", "danger")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")
    confirm_new_password = request.form.get("confirm_new_password")

    # Verificar se as senhas foram preenchidas corretamente
    if not current_password or not new_password or not confirm_new_password:
        flash("Todos os campos são obrigatórios.", "danger")
        return redirect(url_for("adjustments"))

    if len(new_password) < 8:
        flash("A nova senha deve ter pelo menos 8 caracteres.", "danger")
        return redirect(url_for("adjustments"))

    if new_password != confirm_new_password:
        flash("As novas senhas não coincidem.", "danger")
        return redirect(url_for("adjustments"))

    try:
        # Buscar a senha do usuário no banco de dados
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" not in response:
            flash("Usuário não encontrado.", "danger")
            return redirect(url_for("adjustments"))

        user = response["Item"]
        stored_password_hash = user.get("password_hash")

        # Verificar se a senha atual está correta
        if not check_password_hash(stored_password_hash, current_password):
            flash("Senha atual incorreta.", "danger")
            return redirect(url_for("adjustments"))

        # Gerar hash da nova senha
        new_password_hash = generate_password_hash(new_password)

        # Atualizar a senha no banco de dados
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET password_hash = :p, updated_at = :u",
            ExpressionAttributeValues={
                ":p": new_password_hash,
                ":u": datetime.datetime.now().date().isoformat(),
            },
        )

        flash("Senha alterada com sucesso!", "success")
        return redirect(url_for("adjustments"))

    except Exception as e:
        print(f"Erro ao alterar a senha: {e}")
        flash("Ocorreu um erro ao processar a alteração da senha.", "danger")
        return redirect(url_for("adjustments"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email")
        username = request.form.get("username")
        if len(username) < 3 or len(username) > 15:
            flash("O nome de usuário deve ter entre 3 e 15 caracteres.", "danger")
            return redirect("/register")

        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if len(password) < 8 or len(password) > 64:
            flash("A senha deve ter entre 8 e 64 caracteres.", "danger")
            return redirect("/register")

        if password != confirm_password:
            flash("As senhas não coincidem.", "danger")
            return redirect("/register")

        if not email or not password:
            return render_template(
                "login.html", error="Todos os campos são obrigatórios", register=True
            )

        success = create_user(email, username, password)
        if success:
            return render_template(
                "login.html",
                message="Cadastro realizado com sucesso! Um e-mail de confirmação foi enviado. Confirme antes de fazer login.",
            )
        else:
            return render_template(
                "login.html",
                error="Já existe um cadastro com esse e-mail!",
                register=True,
            )

    return render_template("login.html", register=True)


def create_user(email, username, password, role="admin"):
    password_hash = generate_password_hash(password)
    email_token = secrets.token_urlsafe(16)
    user_id = str(uuid.uuid4())  # Gerando um ID único para o usuário
    account_id = str(uuid.uuid4())  # Gerando um ID único para o usuário

    try:
        users_table.put_item(
            Item={
                "user_id": user_id,  # Chave primária única
                "account_id": account_id,  # Chave primária única
                "email": email,  # Indexável para buscas
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "created_at": datetime.datetime.now().isoformat(),
                "email_confirmed": False,
                "email_token": email_token,
                "last_email_sent": datetime.datetime.now().isoformat(),
            },
            ConditionExpression="attribute_not_exists(email)",  # Garantir que não há duplicação de email
        )

        send_confirmation_email(email, username, email_token)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


# Rota para administradores verem todos os usuários
@app.route("/admin/users")
def admin_users():
    # Verificar se o usuário está logado e é admin
    if "email" not in session:
        return redirect(url_for("login"))

    # Obter o papel do usuário atual
    try:
        response = users_table.get_item(Key={"username": session["username"]})
        if "Item" not in response or response["Item"].get("role") != "admin":
            # Redirecionar usuários não-admin
            return redirect(url_for("index"))
    except Exception as e:
        print(f"Erro ao verificar permissões: {e}")
        return redirect(url_for("index"))

    # Recuperar todos os usuários
    users = get_all_users()
    return render_template("admin_users.html", users=users)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        # Passo 1: Buscar o user_id pelo email no GSI
        response = users_table.query(
            IndexName="email-index",  # Nome do GSI
            KeyConditionExpression="email = :email",
            ExpressionAttributeValues={":email": email},
        )

        items = response.get("Items", [])
        if not items:
            flash("E-mail ou senha incorretos.", "danger")
            return redirect(url_for("login"))

        user_id = items[0]["user_id"]  # Obtendo o user_id correspondente ao email

        # Passo 2: Buscar os dados completos do usuário na tabela principal
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" not in response:
            flash("E-mail ou senha incorretos.", "danger")
            return redirect(url_for("login"))

        user = response["Item"]
        stored_hash = user["password_hash"]
        username = user["username"]
        account_id = user["account_id"]

        # Se o e-mail não estiver confirmado, mostrar opção de reenvio
        if not user.get("email_confirmed", False):
            resend_link = url_for("resend_confirmation", email=email)
            flash(
                "Sua conta ainda não foi confirmada. Por favor, confirme seu e-mail.",
                "warning",
            )
            flash(
                f"<a href='{resend_link}' class='btn btn-link'>Reenviar E-mail de Confirmação</a>",
                "info",
            )
            return redirect(url_for("login"))

        # Verificar senha
        if check_password_hash(stored_hash, password):
            session["logged_in"] = True
            session["email"] = email
            session["role"] = user.get("role", "user")
            session["username"] = username
            session["user_id"] = user_id
            session["account_id"] = account_id
            return redirect(url_for("index"))

        flash("E-mail ou senha incorretos.", "danger")

    return render_template("login.html")


def upload_image_to_s3(image_file, prefix="images"):
    if image_file:
        filename = secure_filename(image_file.filename)
        item_id = str(uuid.uuid4())
        s3_key = f"{prefix}/{item_id}_{filename}"
        s3.upload_fileobj(image_file, s3_bucket_name, s3_key)
        image_url = f"https://{s3_bucket_name}.s3.amazonaws.com/{s3_key}"
        return image_url
    return ""


def aplicar_filtro(
    items,
    filtro,
    today,
    description=None,
    client_name=None,
    payment_status=None,
    start_date=None,
    end_date=None,
    return_start_date=None,
    return_end_date=None,
    comments=None,
    formatted_dev_date=None,
    dev_date=None,
):

    for dress in items:
        # Processar return_date
        return_date_str = dress.get("return_date")
        if return_date_str:
            try:
                return_date = datetime.datetime.strptime(
                    return_date_str, "%Y-%m-%d"
                ).date()
                dress["overdue"] = return_date < today
                dress["return_date_formatted"] = return_date.strftime("%d-%m-%Y")
            except ValueError:
                dress["overdue"] = False
                dress["return_date_formatted"] = "Data Inválida"
        else:
            dress["overdue"] = False
            dress["return_date_formatted"] = "N/A"

        # Processar rental_date
        rental_date_str = dress.get("rental_date")
        if rental_date_str:
            try:
                rental_date = datetime.datetime.strptime(
                    rental_date_str, "%Y-%m-%d"
                ).date()
                dress["rental_date_formatted"] = rental_date.strftime("%d-%m-%Y")
                dress["rental_date_obj"] = rental_date  # Para ordenação
            except ValueError:
                dress["rental_date_formatted"] = "Data Inválida"
                dress["rental_date_obj"] = today
        else:
            dress["rental_date_formatted"] = "N/A"
            dress["rental_date_obj"] = today

        # Processar dev_date
        if dress.get("dev_date"):

            try:
                # Converte a string no formato "YYYY-MM-DD" para um objeto datetime
                dev_date_obj = datetime.datetime.strptime(
                    dress.get("dev_date"), "%Y-%m-%d"
                )
                # Reformatar o objeto datetime para "DD-MM-YYYY"
                dress["dev_date"] = dev_date_obj.strftime("%d-%m-%Y")
            except ValueError:
                print("no dev_date")

    # Aplicar filtro principal
    if filtro == "reservados":
        filtered_items = [dress for dress in items if not dress.get("retirado", False)]
    elif filtro == "retirados":
        filtered_items = [dress for dress in items if dress.get("retirado", False)]
    elif filtro == "atrasados":
        filtered_items = [dress for dress in items if dress.get("overdue", False)]
    else:  # Default: "todos"
        filtered_items = items

    # Filtrar por descrição
    if description:
        filtered_items = [
            dress
            for dress in filtered_items
            if description.lower() in dress.get("description", "").lower()
        ]

    # Filtrar por comentários
    if comments:
        filtered_items = [
            dress
            for dress in filtered_items
            if comments.lower() in dress.get("comments", "").lower()
        ]

    # Filtrar por nome do cliente
    if client_name:
        filtered_items = [
            dress
            for dress in filtered_items
            if client_name.lower() in dress.get("client_name", "").lower()
        ]

    # Filtrar por status de pagamento
    if payment_status:
        filtered_items = [
            dress
            for dress in filtered_items
            if dress.get("pagamento", "").lower() == payment_status.lower()
        ]

    # Filtrar por intervalo de datas de retirada
    if start_date or end_date:
        filtered_items = [
            dress
            for dress in filtered_items
            if (not start_date or dress["rental_date_obj"] >= start_date)
            and (not end_date or dress["rental_date_obj"] <= end_date)
        ]

    # Need fixing ########################
    if dev_date:
        filtered_items = [dress for dress in filtered_items]

    return filtered_items


def listar_itens(status_list, template, title):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter o account_id do usuário logado
    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")  # 🔍 Depuração
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    def parse_date(date_str):
        return (
            datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str
            else None
        )

    start_date = parse_date(start_date)
    end_date = parse_date(end_date)
    return_start_date = parse_date(return_start_date)
    return_end_date = parse_date(return_end_date)

    # Fazer a consulta usando o GSI account_id-index
    response = itens_table.query(
        IndexName="account_id-index",  # Usando o GSI
        KeyConditionExpression="#account_id = :account_id",
        ExpressionAttributeNames={"#account_id": "account_id"},
        ExpressionAttributeValues={":account_id": account_id},
    )

    items = response.get("Items", [])
    today = datetime.datetime.now().date()

    # Aplicar filtros adicionais (ex: status, datas, etc.)
    filtered_items = [item for item in items if item.get("status") in status_list]

    # Aplicar filtros extras (datas, descrição, pagamento, etc.)
    filtered_items = aplicar_filtro(
        filtered_items,
        filtro,
        today,
        description=description,
        client_name=client_name,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
    )

    # Paginação
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    if template == "available.html":
        add_template = "add_small"
    else:
        add_template = "add"

    return render_template(
        template,
        itens=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title=title,
        add_route=url_for(add_template),
        next_url=request.url,
    )


@app.route("/")
def index():
    return listar_itens(["rented"], "index.html", "Itens Alugados")


@app.route("/rented")
def rented():
    return listar_itens(["rented"], "rented.html", "Itens Alugados")


@app.route("/returned")
def returned():
    return listar_itens(["returned"], "returned.html", "Itens Devolvidos")


@app.route("/history")
def history():
    return listar_itens(["historic"], "history.html", "Histórico de Aluguéis")


@app.route("/available")
def available():
    return listar_itens(["available"], "available.html", "Itens Disponíveis")


@app.route("/archive")
def archive():
    return listar_itens(["archived"], "archive.html", "Itens Arquivados")


@app.route("/trash")
def trash():
    return listar_itens(["deleted", "version"], "trash.html", "Histórico de alterações")


@app.route("/add", methods=["GET", "POST"])
def add():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o user_id e account_id do usuário logado da sessão
    user_id = session.get("user_id")
    account_id = session.get("account_id")

    if request.method == "POST":
        # Capturar dados do formulário
        status = request.form.get(
            "status"
        )  # Captura o status: rented, returned, available
        description = request.form.get("description").strip()
        client_name = request.form.get("client_name")
        client_tel = request.form.get("client_tel")
        rental_date_str = request.form.get("rental_date")
        return_date_str = request.form.get("return_date")
        retirado = "retirado" in request.form  # Verifica se o checkbox está marcado
        valor = request.form.get("valor")
        pagamento = request.form.get("pagamento")
        comments = request.form.get("comments").strip()
        image_file = request.files.get("image_file")

        # Validar se o status foi escolhido
        if status not in ["rented", "returned", "historic"]:
            flash("Por favor, selecione o status do item.", "danger")
            return render_template("add.html", next=next_page)

        # Validar e converter as datas
        try:
            rental_date = datetime.datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.datetime.strptime(return_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
            return render_template("add.html", next=next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o item
        item_id = str(uuid.uuid4())

        # Adicionar o novo item ao DynamoDB
        itens_table.put_item(
            Item={
                "user_id": user_id,
                "account_id": account_id,
                "item_id": item_id,
                "description": description,
                "client_name": client_name,
                "client_tel": client_tel,
                "rental_date": rental_date.strftime("%Y-%m-%d"),
                "return_date": return_date.strftime("%Y-%m-%d"),
                "retirado": retirado,
                "comments": comments,
                "valor": valor,
                "pagamento": pagamento,
                "image_url": image_url,
                "status": status,
                "previous_status": status,
            }
        )
        # Dicionário para mapear os valores a nomes associados
        status_map = {
            "rented": "Alugados",
            "returned": "Devolvidos",
            "historic": "Histórico",
        }

        flash(
            f"Item adicionado em <a href='{status}'>{status_map[status]}</a>.",
            "success",
        )
        # Redirecionar para a página de origem
        return redirect(next_page)

    return render_template("add.html", next=next_page)


@app.route("/add_small", methods=["GET", "POST"])
def add_small():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o user_id e account_id do usuário logado da sessão
    user_id = session.get("user_id")
    account_id = session.get("account_id")

    if request.method == "POST":
        # Capturar dados do formulário
        status = "archived" if "archive" in next_page else "available"
        description = request.form.get("description").strip()
        client_name = None
        client_tel = None
        rental_date_str = None
        return_date_str = None
        retirado = None  # Verifica se o checkbox está marcado
        valor = request.form.get("valor")
        pagamento = None
        comments = request.form.get("comments")
        image_file = request.files.get("image_file")

        # Validar se o status foi escolhido
        if status not in ["rented", "returned", "available", "archived"]:
            flash("Por favor, selecione o status do item.", "danger")
            return render_template(next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o item (pode usar UUID)
        item_id = str(uuid.uuid4())

        # Adicionar o novo item ao DynamoDB
        itens_table.put_item(
            Item={
                "user_id": user_id,
                "account_id": account_id,
                "item_id": item_id,
                "description": description,
                "client_name": client_name,
                "client_tel": client_tel,
                "rental_date": rental_date_str,
                "return_date": return_date_str,
                "retirado": retirado,
                "comments": comments,
                "valor": valor,
                "pagamento": pagamento,
                "image_url": image_url,
                "status": status,  # Adiciona o status selecionado
            }
        )

        flash("Item adicionado com sucesso.", "success")
        # Redirecionar para a página de origem
        return redirect(next_page)

    return render_template("add_small.html", next=next_page)


def copy_image_in_s3(original_url):

    # Inicializa o cliente S3
    s3 = boto3.client("s3")

    # Analisa a URL
    parsed_url = urlparse(original_url)
    if not parsed_url.netloc or not parsed_url.path:
        raise ValueError(f"URL inválida: {original_url}")

    # Extrai o nome do bucket e a chave original
    bucket_name = parsed_url.netloc.split(".")[0]
    original_key = parsed_url.path.lstrip("/")

    # Verifica se o bucket_name é válido
    if not bucket_name:
        raise ValueError("O nome do bucket é inválido ou está vazio.")

    # Cria uma nova chave para o arquivo copiado
    new_key = f"copies/{uuid.uuid4()}_{original_key.split('/')[-1]}"

    # Realiza a cópia do objeto
    s3.copy_object(
        CopySource={"Bucket": bucket_name, "Key": original_key},
        Bucket=bucket_name,
        Key=new_key,
    )

    # Retorna a URL da nova cópia
    return f"https://{bucket_name}.s3.amazonaws.com/{new_key}"


@app.route("/reports", methods=["GET", "POST"])
def reports():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter o e-mail do usuário logado
    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")  # 🔍 Depuração
        return redirect(url_for("login"))

    # Valores padrão para data inicial e final (últimos 30 dias)
    end_date = datetime.datetime.now().date()
    start_date = end_date - datetime.timedelta(days=30)

    if request.method == "POST":
        try:
            start_date = datetime.datetime.strptime(
                request.form.get("start_date"), "%Y-%m-%d"
            ).date()
            end_date = datetime.datetime.strptime(
                request.form.get("end_date"), "%Y-%m-%d"
            ).date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
            return render_template(
                "reports.html",
                total_paid=0,
                total_due=0,
                total_general=0,
                start_date=start_date,
                end_date=end_date,
            )

    response = itens_table.query(
        IndexName="account_id-index",  # Usando o GSI para buscar por account_id
        KeyConditionExpression="#account_id = :account_id",
        FilterExpression="#status IN (:rented, :returned, :archived, :history)",
        ExpressionAttributeNames={"#account_id": "account_id", "#status": "status"},
        ExpressionAttributeValues={
            ":account_id": account_id,
            ":rented": "rented",
            ":returned": "returned",
            ":archived": "archived",
            ":history": "history",
        },
    )

    items = response.get("Items", [])

    # Inicializar os totais
    total_paid = 0  # Total recebido
    total_due = 0  # Total a receber

    for dress in items:
        try:
            # Considerar apenas registros dentro do período
            rental_date = datetime.datetime.strptime(
                dress.get("rental_date"), "%Y-%m-%d"
            ).date()
            if start_date <= rental_date <= end_date:
                valor = float(dress.get("valor", 0))
                pagamento = dress.get("pagamento", "").lower()

                # Calcular o total recebido
                if pagamento == "pago 100%":
                    total_paid += valor
                elif pagamento == "pago 50%":
                    total_paid += valor * 0.5

                # Calcular o total a receber
                if pagamento == "não pago":
                    total_due += valor
                elif pagamento == "pago 50%":
                    total_due += valor * 0.5
        except (ValueError, TypeError):
            continue  # Ignorar registros com datas ou valores inválidos

    # Total geral: recebido + a receber
    total_general = total_paid + total_due

    return render_template(
        "reports.html",
        total_paid=total_paid,
        total_due=total_due,
        total_general=total_general,
        start_date=start_date,
        end_date=end_date,
    )


@app.route("/edit/<item_id>", methods=["GET", "POST"])
def edit(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = itens_table.get_item(Key={"item_id": item_id})
    item = response.get("Item")

    if not item:
        flash("Item não encontrado.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        # Obter novos dados do formulário
        new_data = {
            "status": request.form.get("status") or None,
            "rental_date": request.form.get("rental_date") or None,
            "return_date": request.form.get("return_date") or None,
            "dev_date": request.form.get("dev_date") or None,
            "description": request.form.get("description", "").strip() or None,
            "client_name": request.form.get("client_name") or None,
            "client_tel": request.form.get("client_tel") or None,
            "retirado": "retirado" in request.form,  # Checkbox
            "valor": request.form.get("valor", "").strip() or None,
            "pagamento": request.form.get("pagamento") or None,
            "comments": request.form.get("comments", "").strip() or None,
            "image_url": item.get(
                "image_url", ""
            ),  # Manter valor antigo se não houver upload
        }

        # Fazer upload da imagem, se houver
        image_file = request.files.get("image_file")
        if image_file and image_file.filename:
            new_data["image_url"] = upload_image_to_s3(image_file)

        # Converter datas para o formato correto
        if new_data["rental_date"] and isinstance(
            new_data["rental_date"], datetime.date
        ):
            new_data["rental_date"] = new_data["rental_date"].strftime("%Y-%m-%d")

        if new_data["return_date"] and isinstance(
            new_data["return_date"], datetime.date
        ):
            new_data["return_date"] = new_data["return_date"].strftime("%Y-%m-%d")

        if new_data["dev_date"] and isinstance(new_data["dev_date"], datetime.date):
            new_data["dev_date"] = new_data["dev_date"].strftime("%Y-%m-%d")

        # Comparar novos valores com os antigos
        changes = {
            key: value for key, value in new_data.items() if item.get(key) != value
        }

        if not changes:  # Se não houver mudanças, apenas exibir a mensagem e sair
            flash("Nenhuma alteração foi feita.", "warning")
            return redirect(next_page)

        # Criar cópia do item somente se houver mudanças
        new_item_id = str(uuid.uuid4())
        edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        copied_item = {
            key: value
            for key, value in item.items()
            if key != "item_id" and value not in [None, ""]
        }
        copied_item["item_id"] = new_item_id
        copied_item["parent_item_id"] = item.get("item_id", "")
        copied_item["status"] = "version"
        copied_item["edited_date"] = edited_date
        copied_item["edited_by"] = session.get("username")
        copied_item["previous_status"] = item.get("status")

        # Salvar a cópia no DynamoDB
        itens_table.put_item(Item=copied_item)

        # Criar dinamicamente os updates para evitar erro com valores vazios
        update_expression = []
        expression_values = {}

        for key, value in changes.items():
            alias = f":{key[:2]}"  # Criar alias para valores
            update_expression.append(f"{key} = {alias}")
            expression_values[alias] = value

        # Atualizar o item original apenas se houver mudanças
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET " + ", ".join(update_expression),
            ExpressionAttributeValues=expression_values,
        )

        flash("Item atualizado com sucesso.", "success")
        return redirect(next_page)

    # Preparar dados para o template
    dress = {
        "item_id": item.get("item_id"),
        "description": item.get("description"),
        "client_name": item.get("client_name"),
        "client_tel": item.get("client_tel"),
        "rental_date": item.get("rental_date"),
        "return_date": item.get("return_date"),
        "dev_date": item.get("dev_date"),
        "comments": item.get("comments"),
        "image_url": item.get("image_url"),
        "retirado": item.get("retirado", False),
        "valor": item.get("valor"),
        "pagamento": item.get("pagamento"),
        "status": item.get("status"),
    }

    return render_template("edit.html", item=item)


@app.route("/purge_deleted_items", methods=["GET", "POST"])
def purge_deleted_items():
    if not session.get("logged_in"):
        return jsonify({"error": "Acesso não autorizado"}), 403

    try:
        # Obter a data atual e calcular o limite de 30 dias atrás
        hoje = datetime.datetime.utcnow()
        limite_data = hoje - datetime.timedelta(days=30)

        # Buscar todos os itens com status "deleted"
        response = itens_table.scan(
            FilterExpression="#status = :deleted",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":deleted": "deleted"},
        )

        itens_deletados = response.get("Items", [])
        total_itens_removidos = 0
        total_imagens_preservadas = 0

        for item in itens_deletados:
            deleted_date_str = item.get("deleted_date")
            user_id = item.get("user_id")  # Chave primária para verificação
            image_url = item.get("image_url")

            if deleted_date_str:
                try:
                    # Converter string de data para objeto datetime
                    deleted_date = datetime.datetime.strptime(
                        deleted_date_str, "%d/%m/%Y %H:%M:%S"
                    )

                    # Verificar se passou dos 30 dias
                    if deleted_date < limite_data:
                        item_id = item["item_id"]

                        # Se a imagem existe, verificar se ela é usada em outro item ativo
                        deletar_imagem = True
                        if (
                            user_id
                            and image_url
                            and isinstance(image_url, str)
                            and image_url.strip()
                        ):
                            # Buscar todos os itens com o mesmo user_id
                            response_email = itens_table.scan(
                                FilterExpression="user_id = :user_id",
                                ExpressionAttributeValues={":user_id": user_id},
                            )

                            itens_com_mesmo_user_id = response_email.get("Items", [])

                            # Verificar se a imagem está em uso por outro item que não está "deleted"
                            for outro_item in itens_com_mesmo_user_id:
                                if (
                                    outro_item.get("image_url") == image_url
                                    and outro_item.get("status") != "deleted"
                                ):
                                    deletar_imagem = False
                                    total_imagens_preservadas += 1
                                    break  # Se encontrou um ativo, não precisa verificar mais

                        # Se não houver outro item ativo usando a mesma imagem, deletar do S3
                        if (
                            deletar_imagem
                            and isinstance(image_url, str)
                            and image_url.strip()
                        ):
                            parsed_url = urlparse(image_url)
                            object_key = parsed_url.path.lstrip("/")
                            s3.delete_object(Bucket=s3_bucket_name, Key=object_key)

                        # Remover o item do DynamoDB
                        itens_table.delete_item(Key={"item_id": item_id})
                        total_itens_removidos += 1

                except ValueError:
                    print(f"Erro ao converter a data de exclusão: {deleted_date_str}")

        return jsonify(
            {
                "message": f"{total_itens_removidos} itens foram excluídos definitivamente.",
                "imagens_preservadas": total_imagens_preservadas,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rent/<item_id>", methods=["GET", "POST"])
def rent(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Buscar item existente
    response = itens_table.get_item(Key={"item_id": item_id})
    item = response.get("Item")
    if not item:
        flash("Item não encontrado.", "danger")
        return redirect(url_for("available"))

    if request.method == "POST":
        rental_date_str = request.form.get("rental_date")
        return_date_str = request.form.get("return_date")
        description = request.form.get("description")
        client_name = request.form.get("client_name")
        client_tel = request.form.get("client_tel")
        retirado = "retirado" in request.form  # Verifica presença do checkbox
        valor = request.form.get("valor")
        pagamento = request.form.get("pagamento")
        comments = request.form.get("comments")
        image_file = request.files.get("image_file")

        # Validar e converter as datas
        try:
            rental_date = datetime.datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.datetime.strptime(return_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
            return render_template("edit.html")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="""
                set rental_date = :r,
                    return_date = :rt,
                    comments = :c,
                    image_url = :i,
                    description = :dc,
                    client_name = :cn,
                    client_tel = :ct,
                    retirado = :ret,
                    valor = :val,
                    pagamento = :pag,
                    #status = :st
            """,
            ExpressionAttributeNames={
                "#status": "status"  # Define um alias para o atributo reservado
            },
            ExpressionAttributeValues={
                ":r": rental_date.strftime("%Y-%m-%d"),
                ":rt": return_date.strftime("%Y-%m-%d"),
                ":c": comments,
                ":i": new_image_url,
                ":dc": description,
                ":cn": client_name,
                ":ct": client_tel,
                ":ret": retirado,
                ":val": valor,
                ":pag": pagamento,
                ":st": "rented",
            },
        )

        flash(
            "Item <a href='/rented'>alugado</a> com sucesso!",
            "success",
        )
        return redirect(url_for("available"))

    # Preparar dados para o template
    dress = {
        "item_id": item.get("item_id"),
        "description": item.get("description"),
        "client_name": item.get("client_name"),
        "client_tel": item.get("client_tel"),
        "rental_date": item.get("rental_date"),
        "return_date": item.get("return_date"),
        "comments": item.get("comments"),
        "image_url": item.get("image_url"),
        "retirado": item.get("retirado", False),
        "valor": item.get("valor"),
        "pagamento": item.get("pagamento"),
    }

    return render_template("rent.html", dress=dress)


@app.route("/edit_small/<item_id>", methods=["GET", "POST"])
def edit_small(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = itens_table.get_item(Key={"item_id": item_id})
    item = response.get("Item")
    if not item:
        flash("Item não encontrado.", "danger")
        return redirect(url_for("available"))

    if request.method == "POST":
        # Obter novos dados do formulário
        new_data = {
            "rental_date": request.form.get("rental_date") or None,
            "return_date": request.form.get("return_date") or None,
            "description": request.form.get("description", "").strip() or None,
            "client_name": request.form.get("client_name") or None,
            "client_tel": request.form.get("client_tel") or None,
            "retirado": request.form.get("retirado") or None,
            "valor": request.form.get("valor", "").strip() or None,
            "pagamento": request.form.get("pagamento") or None,
            "comments": request.form.get("comments", "").strip() or None,
            "image_url": item.get("image_url", ""),  # Mantém o valor antigo por padrão
        }

        # Fazer upload da imagem, se houver
        image_file = request.files.get("image_file")
        if image_file and image_file.filename:
            new_data["image_url"] = upload_image_to_s3(image_file)

        # Comparar novos valores com os antigos
        changes = {
            key: value for key, value in new_data.items() if item.get(key) != value
        }

        if not changes:  # Se não houver mudanças, exibir a mensagem e não salvar nada
            flash("Nenhuma alteração foi feita.", "warning")
            return redirect(next_page)

        # Criar cópia do item somente se houver mudanças
        new_item_id = str(uuid.uuid4())
        edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        copied_item = {
            key: value
            for key, value in item.items()
            if key != "item_id" and value not in [None, ""]
        }
        copied_item["item_id"] = new_item_id
        copied_item["previous_status"] = item.get("status")
        copied_item["parent_item_id"] = item.get("item_id", "")
        copied_item["status"] = "version"
        copied_item["edited_date"] = edited_date
        copied_item["deleted_by"] = session.get("username")
        copied_item["previous_status"] = item.get("status")

        # Salvar a cópia no DynamoDB
        itens_table.put_item(Item=copied_item)

        # Criar dinamicamente os updates para evitar erro com valores vazios
        update_expression = []
        expression_values = {}

        for key, value in changes.items():
            alias = f":{key[:2]}"  # Criar alias para valores
            update_expression.append(f"{key} = {alias}")
            expression_values[alias] = value

        # Atualizar apenas se houver mudanças
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET " + ", ".join(update_expression),
            ExpressionAttributeValues=expression_values,
        )

        flash("Item atualizado com sucesso.", "success")
        return redirect(next_page)

    # Preparar dados para o template
    item = {
        "item_id": item.get("item_id"),
        "description": item.get("description"),
        "client_name": item.get("client_name"),
        "client_tel": item.get("client_tel"),
        "rental_date": item.get("rental_date"),
        "return_date": item.get("return_date"),
        "comments": item.get("comments"),
        "image_url": item.get("image_url"),
        "retirado": item.get("retirado", False),
        "valor": item.get("valor"),
        "pagamento": item.get("pagamento"),
    }

    return render_template("edit_small.html", item=item)


@app.route("/delete/<item_id>", methods=["POST"])
def delete(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    deleted_by = session.get("username")
    next_page = request.args.get("next", url_for("index"))

    try:
        # Obter o item antes de modificar
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if item:
            # Obter data e hora atuais no formato brasileiro
            deleted_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            # Atualizar o status do item para "deleted"
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET previous_status = #status, #status = :deleted, deleted_date = :deleted_date, deleted_by = :deleted_by",
                ExpressionAttributeNames={
                    "#status": "status"  # Alias para evitar conflito com palavra reservada
                },
                ExpressionAttributeValues={
                    ":deleted": "deleted",
                    ":deleted_date": deleted_date,
                    ":deleted_by": deleted_by,
                },
            )

            """# Buscar e deletar todos os itens relacionados na tabela alugueqqc_itens
            response = itens_table.query(
                IndexName="parent_item_id-index",  # Nome do índice secundário global (GSI)
                KeyConditionExpression="parent_item_id = :parent_id",
                ExpressionAttributeValues={":parent_id": item_id},
            )

            related_items = response.get("Items", [])

            for related_item in related_items:
                itens_table.delete_item(Key={"item_id": related_item["item_id"]})"""

            flash(
                "Item marcado como deletado. Ele ficará disponível na 'lixeira' por 30 dias, e seus itens relacionados foram removidos.",
                "success",
            )

        else:
            flash("Item não encontrado.", "danger")

    except Exception as e:
        flash(f"Ocorreu um erro ao tentar deletar o item: {str(e)}", "danger")

    return redirect(next_page)


@app.route("/mark_returned/<item_id>", methods=["GET", "POST"])
def mark_returned(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obtém a data atual
    dev_date = datetime.datetime.now(manaus_tz).strftime("%Y-%m-%d")

    # Atualiza status para 'returned'
    # Atualiza status para 'returned' e insere data de devolução
    itens_table.update_item(
        Key={"item_id": item_id},
        UpdateExpression="set #status = :s, dev_date = :d",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "returned", ":d": dev_date},
    )

    flash(
        "Item <a href='/returned'>devolvido</a> com sucesso.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/mark_archived/<item_id>", methods=["GET", "POST"])
def mark_archived(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = itens_table.get_item(Key={"item_id": item_id})
    item = response.get("Item")

    if not item:
        flash("Item não encontrado.", "danger")
        return redirect(url_for("returned"))

    # Criar uma cópia do item antes de qualquer modificação
    new_dress_id = str(uuid.uuid4())
    copied_item = item.copy()  # Copiar todos os campos do item original
    copied_item["item_id"] = new_dress_id
    copied_item["original_id"] = item_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] != "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    itens_table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"item_id", "description", "image_url", "valor", "user_id"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "archived"

    # Atualizar o item original no DynamoDB
    itens_table.put_item(Item=filtered_item)

    flash(
        "Item <a href='/archive'>arquivado</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )
    return redirect(next_page)


@app.route("/mark_available/<item_id>", methods=["GET", "POST"])
def mark_available(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = itens_table.get_item(Key={"item_id": item_id})
    item = response.get("Item")

    if not item:
        flash("Item não encontrado.", "danger")
        return redirect(url_for("returned"))

    if "archive" in next_page:
        # Atualiza status para 'returned' e insere data de devolução
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="set #status = :s",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": "available"},
        )
        flash(
            "Item marcado com <a href='/available'>disponível</a>.",
            "success",
        )
        return redirect(url_for("archive"))

    # Criar uma cópia do item antes de qualquer modificação
    new_dress_id = str(uuid.uuid4())
    copied_item = item.copy()  # Copiar todos os campos do item original
    copied_item["item_id"] = new_dress_id
    copied_item["original_id"] = item_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] != "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    itens_table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"item_id", "description", "image_url", "valor", "user_id"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "available"

    # Atualizar o item original no DynamoDB
    itens_table.put_item(Item=filtered_item)

    flash(
        "Item <a href='/available'>disponível</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )

    return redirect(url_for("returned"))


"""def verify_user(email, password):
    try:
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" in response:
            user = response["Item"]
            stored_hash = user["password_hash"]

            # Se o e-mail não estiver confirmado, bloquear login
            if not user.get("email_confirmed", False):
                return "Pendente", None  # Novo retorno indicando pendência

            return check_password_hash(stored_hash, password), user.get("role", "user")

    except Exception as e:
        print(f"Erro ao verificar usuário: {e}")
    return False, None"""


@app.route("/mark_rented/<item_id>", methods=["POST"])
def mark_rented(item_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    # Atualiza status para 'rented'
    itens_table.update_item(
        Key={"item_id": item_id},
        UpdateExpression="set #status = :s",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "rented"},
    )

    # Mensagem de sucesso
    flash(
        "Item marcado com alugado. Clique <a href='/rented'>aqui</a> para ver",
        "success",
    )
    return redirect(url_for("returned"))


@app.route("/adjustments")
def adjustments():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter nome de usuário e e-mail logado
    user_id = session.get("user_id")

    # Buscar usuário pelo e-mail
    response = users_table.get_item(Key={"user_id": user_id})

    if "Item" not in response:
        flash("Erro ao carregar dados do usuário.", "danger")
        return redirect(url_for("login"))

    user = response["Item"]
    username = user.get("username", "Usuário Desconhecido")
    user_email = user.get("email", "Usuário Desconhecido")

    return render_template("adjustments.html", username=username, email=user_email)


# Rota para solicitar alteração de e-mail
@app.route("/request-email-change", methods=["POST"])
def request_email_change():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    user_email = session.get("email")
    new_email = request.form.get("new_email")

    if not new_email:
        flash("Por favor, insira um e-mail válido.", "danger")
        return redirect(url_for("adjustments"))

    # Gerar token de confirmação
    email_token = secrets.token_urlsafe(16)

    # Salvar token temporário no banco
    users_table.update_item(
        Key={"email": user_email},
        UpdateExpression="SET pending_email = :new_email, email_token = :token",
        ExpressionAttributeValues={":new_email": new_email, ":token": email_token},
    )

    # Enviar e-mail de confirmação
    confirm_url = url_for("confirm_email_change", token=email_token, _external=True)

    ses_client.send_email(
        Source="nao_responda@alugueqqc.com.br",
        Destination={"ToAddresses": [new_email]},
        Message={
            "Subject": {"Data": "Confirme seu novo e-mail"},
            "Body": {
                "Text": {
                    "Data": f"Confirme seu e-mail clicando no link: {confirm_url}"
                },
                "Html": {
                    "Data": f"""
                    <html>
                    <body>
                        <p>Olá,</p>
                        <p>Você solicitou a alteração do seu e-mail. Para confirmar, clique no botão abaixo:</p>
                        <p><a href="{confirm_url}" style="padding: 10px; background-color: blue; color: white; text-decoration: none;">Confirmar Novo E-mail</a></p>
                        <p>Se você não solicitou essa alteração, ignore este e-mail.</p>
                    </body>
                    </html>
                """
                },
            },
        },
    )

    flash("Um e-mail de confirmação foi enviado para o novo endereço.", "info")
    return redirect(url_for("adjustments"))


@app.route("/confirm-email-change/<token>")
def confirm_email_change(token):
    # Buscar usuário pelo token de confirmação no GSI
    response = users_table.query(
        IndexName="EmailTokenIndex",  # Nome do GSI
        KeyConditionExpression="email_token = :token",
        ExpressionAttributeValues={":token": token},
    )

    if not response.get("Items"):
        flash("Token inválido ou expirado.", "danger")
        return redirect(url_for("login"))

    user = response["Items"][0]
    user_id = user["user_id"]  # Obtendo user_id corretamente
    new_email = user.get("pending_email")

    if not new_email:
        flash("Erro ao processar a solicitação.", "danger")
        return redirect(url_for("adjustments"))

    try:
        # Atualizar o e-mail do usuário no DynamoDB
        users_table.update_item(
            Key={"user_id": user_id},  # Usando user_id como chave primária
            UpdateExpression="SET email = :new_email REMOVE email_token, pending_email",
            ExpressionAttributeValues={":new_email": new_email},
        )

        # Atualizar sessão com o novo e-mail
        session["email"] = new_email

        flash("Seu e-mail foi atualizado com sucesso!", "success")
        return redirect(url_for("adjustments"))

    except Exception as e:
        print(f"Erro ao atualizar e-mail: {e}")
        flash("Ocorreu um erro ao processar a solicitação.", "danger")
        return redirect(url_for("adjustments"))


@app.route("/change-username", methods=["POST"])
def change_username():
    if "email" not in session:
        flash("Você precisa estar logado para alterar o nome de usuário.", "danger")
        return redirect(url_for("login"))

    new_username = request.form.get("new_username", "").strip()

    # Validação do tamanho do username
    if len(new_username) < 3 or len(new_username) > 15:
        flash("O nome de usuário deve ter entre 3 e 15 caracteres.", "danger")
        return redirect(url_for("adjustments"))

    user_id = session["user_id"]  # O user_id é a chave única no banco

    try:
        # Buscar o usuário pelo user_id
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" not in response:
            flash("Usuário não encontrado.", "danger")
            return redirect(url_for("adjustments"))

        # user = response["Item"]

        # Atualizar o nome de usuário
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET username = :new_username",
            ExpressionAttributeValues={":new_username": new_username},
        )

        flash("Nome de usuário atualizado com sucesso!", "success")
        return redirect(url_for("adjustments"))

    except Exception as e:
        print(f"Erro ao atualizar nome de usuário: {e}")
        flash("Ocorreu um erro ao atualizar o nome de usuário.", "danger")
        return redirect(url_for("adjustments"))


@app.route("/verificar-item-pai", methods=["POST"])
def verificar_item_pai():
    """Verifica se o item pai do item_id informado existe no banco"""

    item_id = request.json.get("item_id")

    # 🔹 1️⃣ Buscar o item atual no banco
    response = itens_table.get_item(Key={"item_id": item_id})
    item_atual = response.get("Item")

    if not item_atual:
        return {"status": "erro", "mensagem": "Item não encontrado!"}

    # 🔹 Extrair informações do item atual
    parent_item_id = item_atual.get("parent_item_id")
    item_status = item_atual.get("status")
    previous_status = item_atual.get("previous_status", "unknown")

    # 🔹 2️⃣ Se não há parent_item_id, não há item pai, então já vai para recriação
    if not parent_item_id:
        return {
            "status": "pai_nao_existe",
            "mensagem": "Esse item será reativado.",
            "parent_item_id": None,
            "previous_status": previous_status,
        }

    # 🔹 3️⃣ Buscar o item pai no banco de dados
    response_pai = itens_table.get_item(Key={"item_id": parent_item_id})
    item_pai = response_pai.get("Item")

    # 🔹 4️⃣ Agora que temos os dados, fazemos as verificações:

    # Se o item pai não existe, forçamos a recriação do item atual
    if not item_pai:
        return {
            "status": "pai_nao_existe",
            "mensagem": "O item pai não foi encontrado. Esse item será recriado.",
            "parent_item_id": parent_item_id,
            "previous_status": previous_status,
        }

    # Se o item pai está marcado como "deleted", também direcionamos para recriação
    parent_status = item_pai.get("status")
    if parent_status == "deleted":
        return {
            "status": "pai_nao_existe",
            "mensagem": "O item pai estava deletado. Esse item será recriado.",
            "parent_item_id": parent_item_id,
            "previous_status": previous_status,
        }

    # Se o item pai está ativo, podemos restaurar a versão do item atual
    return {
        "status": "pai_existe",
        "mensagem": "Essa versão será restaurada e a versão ativa virá para o histórico de alterações.",
        "parent_item_id": parent_item_id,
        "previous_status": previous_status,
    }


@app.route("/substituir-item", methods=["POST"])
def substituir_item():
    item_id = request.form.get("item_id")
    parent_item_id = request.form.get("parent_item_id")
    print("substituir")
    print(item_id)
    print(parent_item_id)
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("trash"))

    try:
        # 🔹 Obter o item atual (da rota)
        response = itens_table.get_item(Key={"item_id": item_id})
        item_data = response.get("Item")

        if not item_data:
            flash("Item não encontrado.", "danger")
            return redirect(next_page)

        # obter dados do item corrente
        item_status = item_data.get("status", "")

        if not parent_item_id:
            flash("Erro: O item atual não possui um parent_item_id válido.", "warning")
            return redirect(next_page)
        if not item_status:
            flash("Erro: O item atual não possui um item_status válido.", "warning")
            return redirect(next_page)

        # agora pega os dados do item pai do item corrente, se existir
        parent_response = itens_table.get_item(Key={"item_id": parent_item_id})
        parent_item = parent_response.get("Item")

        if not parent_item:
            flash(
                f"Erro: O item original (ID {parent_item_id}) não foi encontrado no banco.",
                "warning",
            )
            return redirect(next_page)

        # 🔹 Armazenar os dados antes da troca
        item_original_copy = item_data.copy()
        parent_original_copy = parent_item.copy()

        # 🔹 Trocar os valores entre os dois itens (swap), exceto `item_id`
        swap_data_item = {
            key: value
            for key, value in parent_original_copy.items()
            if key != "item_id"
        }
        swap_data_parent = {
            key: value for key, value in item_original_copy.items() if key != "item_id"
        }

        # 🔹 Trocar os valores de `parent_item_id` corretamente
        swap_data_item["parent_item_id"] = (
            parent_item_id  # O item da rota recebe o item original
        )
        swap_data_parent["parent_item_id"] = (
            item_id  # O item original recebe o item da rota
        )

        # 🔹 Trocar os valores de `status`
        swap_data_item["status"], swap_data_parent["status"] = (
            item_original_copy["status"],
            parent_original_copy["status"],
        )

        # 🔹 Atualizar `deleted_by` e `edit_date`
        username = session.get("username")
        edit_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        swap_data_item["deleted_by"] = username
        swap_data_item["edit_date"] = edit_date
        swap_data_parent["deleted_by"] = username
        swap_data_parent["edit_date"] = edit_date

        # 🔹 Atualizar o item da rota (`item_id`) com os dados do item original
        update_expression_item = []
        expression_values_item = {}
        expression_attribute_names_item = {"#status": "status"}  # Alias para status

        for key, value in swap_data_item.items():
            alias = f":val_{key}"  # Criar alias seguro
            key_alias = f"#{key}" if key == "status" else key  # Usar alias para status

            update_expression_item.append(f"{key_alias} = {alias}")
            expression_values_item[alias] = value

        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET " + ", ".join(update_expression_item),
            ExpressionAttributeNames=expression_attribute_names_item,
            ExpressionAttributeValues=expression_values_item,
        )

        # 🔹 Atualizar o item original (`parent_item_id`) com os dados do item da rota
        update_expression_parent = []
        expression_values_parent = {}
        expression_attribute_names_parent = {"#status": "status"}  # Alias para status

        for key, value in swap_data_parent.items():
            alias = f":val_{key}"  # Criar alias seguro
            key_alias = f"#{key}" if key == "status" else key  # Usar alias para status

            update_expression_parent.append(f"{key_alias} = {alias}")
            expression_values_parent[alias] = value

        itens_table.update_item(
            Key={"item_id": parent_item_id},
            UpdateExpression="SET " + ", ".join(update_expression_parent),
            ExpressionAttributeNames=expression_attribute_names_parent,
            ExpressionAttributeValues=expression_values_parent,
        )

        flash(
            f"Substituição do item {item_id} pelo pai {parent_item_id} realizada!",
            "success",
        )
        return redirect(url_for("trash"))

    except Exception as e:
        flash(f"Ocorreu um erro ao tentar substituir o item: {str(e)}", "danger")


@app.route("/recriar-item", methods=["POST"])
def recriar_item():
    item_id = request.form.get("item_id")
    parent_item_id = request.form.get("parent_item_id")
    previous_status = request.form.get("previous_status")

    print("PS")
    print(previous_status)

    print("recriar")
    print(item_id)
    print(parent_item_id)

    itens_table.update_item(
        Key={"item_id": item_id},  # 🔹 Chave primária do item
        UpdateExpression="SET #status = :previous_status",
        ExpressionAttributeNames={
            "#status": "status"
        },  # Alias para evitar palavra reservada
        ExpressionAttributeValues={
            ":previous_status": previous_status
        },  # 🔹 Agora usa previous_status
    )

    flash(
        f"Item {item_id} recriado.",
        "success",
    )
    return redirect(url_for("trash"))


@app.context_processor
def inject_user():
    username = session.get("username")
    return dict(username=username)


@app.route("/termos-de-uso")
def termos_de_uso():
    return render_template("termos_de_uso.html")


@app.route("/contato", methods=["GET", "POST"])
def contato():
    if request.method == "POST":
        nome = request.form.get("nome")
        email = request.form.get("email")
        mensagem = request.form.get("mensagem")

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
        except ClientError as e:
            print(f"Erro ao enviar e-mail: {e}")
            flash("Erro ao enviar a mensagem. Tente novamente mais tarde.", "danger")

        return redirect(url_for("contato"))

    return render_template("contato.html")


# Rota de Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
