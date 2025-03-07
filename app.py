from flask import Flask, render_template, request, redirect, url_for, session, flash
from urllib.parse import urlparse
import boto3
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import datetime
from flask import request, url_for
import pytz
import uuid
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
from boto3.dynamodb.conditions import Attr
import os
from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from botocore.exceptions import ClientError

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
dynamodb_table_name = "alugueqqc_users"
s3_bucket_name = "alugueqqc-images"

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

table = dynamodb.Table(dynamodb_table_name)

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

    if 1 == 1:
        # Buscar o usuário pelo email no banco de dados
        response = users_table.scan(
            FilterExpression="email = :email",
            ExpressionAttributeValues={":email": email},
        )

        if response.get("Items"):
            user = response["Items"][0]
            email = user["email"]
            username = user["username"]

            # Gerar token único para redefinição de senha
            token = str(uuid.uuid4())
            expires_at = (
                datetime.datetime.now() + datetime.timedelta(hours=24)
            ).isoformat()

            # Salvar token no DynamoDB
            reset_tokens_table.put_item(
                Item={
                    "token": token,
                    "email": email,
                    "username": username,
                    "expires_at": expires_at,
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
                    Key={"email": email},
                    UpdateExpression="SET email_token = :token",
                    ExpressionAttributeValues={":token": email_token},
                )

                # Montar link de confirmação
                confirm_url = url_for(
                    "confirm_email", token=email_token, _external=True
                )

                # Enviar e-mail de confirmação novamente
                send_confirmation_email(email, username, confirm_url)

                flash(
                    "Você solicitou redefiniçao de senha, mas ainda precisa confirmar seu e-mail!",
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

    """except Exception as e:
        print(f"Erro ao processar recuperação de senha: {e}")
        return render_template(
            "login.html", error="Ocorreu um erro ao processar sua solicitação"
        )"""


# Rota para exibir a página de redefinição de senha
@app.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token):
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

            # Verificar se o token expirou

            # delete miliseconds from string
            expires_at_str = token_data["expires_at"]
            if "." in expires_at_str:  # Se houver milissegundos, removemos
                expires_at_str = expires_at_str.split(".")[0]
            expires_at = datetime.datetime.fromisoformat(expires_at_str)

            if datetime.datetime.now() > expires_at:
                return render_template(
                    "login.html", error="Este link de redefinição expirou"
                )

            # Token válido, mostrar página de redefinição
            return render_template("login.html", reset_password=True, token=token)
        else:
            return render_template("login.html", error="Link de redefinição inválido")

    except Exception as e:
        print(f"Erro ao verificar token: {e}")
        return render_template(
            "login.html", error="Ocorreu um erro ao processar sua solicitação"
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

            # delete miliseconds from string
            expires_at_str = token_data["expires_at"]
            if "." in expires_at_str:  # Se houver milissegundos, removemos
                expires_at_str = expires_at_str.split(".")[0]
            expires_at = datetime.datetime.fromisoformat(expires_at_str)

            # Verificar se o token expirou
            if datetime.datetime.now() > expires_at:
                return render_template(
                    "login.html", error="Este link de redefinição expirou"
                )

            # Token válido, atualizar a senha
            email = token_data["email"]
            password_hash = generate_password_hash(new_password)

            # Atualizar senha no banco de dados
            users_table.update_item(
                Key={"email": email},
                UpdateExpression="set password_hash = :p, updated_at = :u",
                ExpressionAttributeValues={
                    ":p": password_hash,
                    ":u": datetime.datetime.now().date().isoformat(),
                },
            )

            # Marcar o token como usado
            reset_tokens_table.update_item(
                Key={"token": token},
                UpdateExpression="set used = :u",
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


from flask import flash, redirect, url_for

from werkzeug.security import generate_password_hash
from botocore.exceptions import ClientError


import secrets
from flask import url_for
from werkzeug.security import generate_password_hash
from botocore.exceptions import ClientError


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
    <h1>Alugue QQC - Confirmação de E-mail</h1>
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
    Alugue QQC - Confirmação de E-mail

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


def create_user(email, username, password, role="user"):
    password_hash = generate_password_hash(password)
    email_token = secrets.token_urlsafe(16)

    try:
        users_table.put_item(
            Item={
                "email": email,
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "created_at": datetime.datetime.now().date().isoformat(),
                "email_confirmed": False,
                "email_token": email_token,
                "last_email_sent": datetime.datetime.now().date().isoformat(),
            },
            ConditionExpression="attribute_not_exists(email)",
        )

        send_confirmation_email(email, username, email_token)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


@app.route("/resend_confirmation")
def resend_confirmation():
    email = request.args.get("email")

    if not email:
        flash("E-mail inválido.", "danger")
        return redirect(url_for("login"))

    try:
        response = users_table.get_item(Key={"email": email})
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
        users_table.update_item(
            Key={"email": email},
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
        response = users_table.scan(
            FilterExpression="email_token = :token",
            ExpressionAttributeValues={":token": token},
        )

        if "Items" in response and response["Items"]:
            user = response["Items"][0]
            email = user["email"]

            users_table.update_item(
                Key={"email": email},
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


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email")
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if not email or not password:
            return render_template(
                "login.html", error="Todos os campos são obrigatórios", register=True
            )

        if password != confirm_password:
            return render_template(
                "login.html", error="As senhas não coincidem", register=True
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

        response = users_table.get_item(Key={"email": email})
        if "Item" not in response:
            flash("E-mail ou senha incorretos.", "danger")
            return redirect(url_for("login"))

        user = response["Item"]
        stored_hash = user["password_hash"]

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
            return redirect(url_for("index"))

        flash("E-mail ou senha incorretos.", "danger")

    return render_template("login.html")


def verify_user(email, password):
    try:
        response = users_table.get_item(
            Key={"email": email}
        )  # Alterado de username para email

        print(response)
        if "Item" in response:
            stored_hash = response["Item"]["password_hash"]
            return check_password_hash(stored_hash, password), response["Item"].get(
                "role", "user"
            )
    except Exception as e:
        print(f"Erro ao verificar usuário: {e}")
    return False, None


def upload_image_to_s3(image_file, prefix="images"):
    if image_file:
        filename = secure_filename(image_file.filename)
        dress_id = str(uuid.uuid4())
        s3_key = f"{prefix}/{dress_id}_{filename}"
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


@app.route("/")
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    username = session.get("username")
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Obter o filtro principal (default é "todos")
    filtro = request.args.get(
        "filter", "todos"
    )  # "todos", "reservados", "retirados", "atrasados"

    # Capturar parâmetros adicionais
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.datetime.strptime(
            return_start_date, "%Y-%m-%d"
        ).date()
    if return_end_date:
        return_end_date = datetime.datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "rented"
    response = table.scan(
        FilterExpression="attribute_not_exists(#status) OR #status = :status_rented",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_rented": "rented"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
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

    # Ordenar apenas pela data de registro (data de aluguel mais antiga primeiro)
    sorted_items = sorted(filtered_items, key=lambda x: x["rental_date_obj"])

    # Paginação
    total_items = len(sorted_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = sorted_items[start:end]

    return render_template(
        "index.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Itens Alugados",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/returned")
def returned():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")
    dev_date = request.args.get("dev_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.datetime.strptime(
            return_start_date, "%Y-%m-%d"
        ).date()
    if return_end_date:
        return_end_date = datetime.datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "returned"
    response = table.scan(
        FilterExpression="#status = :status_returned",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_returned": "returned"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
        dev_date=dev_date,
    )

    # Paginação
    # print(filtered_items)
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    return render_template(
        "returned.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Itens Devolvidos",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/history")
def history():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")
    dev_date = request.args.get("dev_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.datetime.strptime(
            return_start_date, "%Y-%m-%d"
        ).date()
    if return_end_date:
        return_end_date = datetime.datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "archived"
    response = table.scan(
        FilterExpression="#status = :status_archived",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_archived": "historic"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
        dev_date=dev_date,
    )

    # Paginação
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    return render_template(
        "history.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Histórico",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/available")
def available():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    comments = request.args.get("comments")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.datetime.strptime(
            return_start_date, "%Y-%m-%d"
        ).date()
    if return_end_date:
        return_end_date = datetime.datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "available"
    response = table.scan(
        FilterExpression="#status = :status_available",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_available": "available"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        comments=comments,
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

    return render_template(
        "available.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Itens Disponíveis",
        add_route=url_for("add_small"),
        next_url=request.url,
    )


@app.route("/archive")
def archive():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    comments = request.args.get("comments")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.datetime.strptime(
            return_start_date, "%Y-%m-%d"
        ).date()
    if return_end_date:
        return_end_date = datetime.datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "archived"
    response = table.scan(
        FilterExpression="#status = :status_archived",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_archived": "archived"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        comments=comments,
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

    return render_template(
        "archive.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Arquivo",
        add_route=url_for("add_small"),
        next_url=request.url,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o e-mail do usuário logado da sessão
    user_email = session.get("email")  # Pega o email salvo na sessão

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
            flash("Por favor, selecione o status do vestido.", "error")
            return render_template("add.html", next=next_page)

        # Validar e converter as datas
        try:
            rental_date = datetime.datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.datetime.strptime(return_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("add.html", next=next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o vestido (pode usar UUID)
        dress_id = str(uuid.uuid4())

        # Adicionar o novo vestido ao DynamoDB
        table.put_item(
            Item={
                "email": user_email,
                "dress_id": dress_id,
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
                "status": status,  # Adiciona o status selecionado
            }
        )
        # Dicionário para mapear os valores a nomes associados
        status_map = {
            "rented": "Alugados",
            "returned": "Devolvidos",
            "historic": "Histórico",
        }

        flash(
            f"Vestido adicionado em <a href='{status}'>{status_map[status]}</a>.",
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

    # Obter o e-mail do usuário logado da sessão
    user_email = session.get("email")  # Pega o email salvo na sessão

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
            flash("Por favor, selecione o status do vestido.", "error")
            return render_template(next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o vestido (pode usar UUID)
        dress_id = str(uuid.uuid4())

        # Adicionar o novo vestido ao DynamoDB
        table.put_item(
            Item={
                "dress_id": dress_id,
                "email": user_email,
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

        flash("Vestido adicionado com sucesso.", "success")
        # Redirecionar para a página de origem
        return redirect(next_page)

    return render_template("add_small.html", next=next_page)


def copy_image_in_s3(original_url):
    import boto3
    from urllib.parse import urlparse
    import uuid

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
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template(
                "reports.html",
                total_paid=0,
                total_due=0,
                total_general=0,
                start_date=start_date,
                end_date=end_date,
            )

    # Obter todos os registros (independente do status)
    response = table.scan()
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


@app.route("/edit/<dress_id>", methods=["GET", "POST"])
def edit(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        status = request.form.get("status")
        rental_date_str = request.form.get("rental_date")
        return_date_str = request.form.get("return_date")
        dev_date = request.form.get("dev_date")
        description = request.form.get("description").strip()
        client_name = request.form.get("client_name")
        client_tel = request.form.get("client_tel")
        retirado = "retirado" in request.form  # Verifica presença do checkbox
        valor = request.form.get("valor")
        pagamento = request.form.get("pagamento")
        comments = request.form.get("comments").strip()
        image_file = request.files.get("image_file")

        # Validar e converter as datas
        try:
            rental_date = datetime.datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.datetime.strptime(return_date_str, "%Y-%m-%d").date()
            # dev_date = datetime.strptime(dev_date, "%Y-%m-%d").date()

        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("edit.html")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
                    dev_date =:dd,
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
                ":st": status,
                ":dd": dev_date,
            },
        )

        flash("Vestido atualizado com sucesso.", "success")
        return redirect(next_page)

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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

    return render_template("edit.html", dress=dress)


@app.route("/rent/<dress_id>", methods=["GET", "POST"])
def rent(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
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
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("edit.html")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
            "Item <a href='/'>alugado</a> com sucesso!",
            "success",
        )
        return redirect(url_for("available"))

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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


@app.route("/edit_small/<dress_id>", methods=["GET", "POST"])
def edit_small(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("available"))

    if request.method == "POST":
        rental_date_str = None
        return_date_str = None
        description = request.form.get("description").strip()
        client_name = None
        client_tel = None
        retirado = None
        valor = request.form.get("valor")
        pagamento = None
        comments = request.form.get("comments").strip()
        image_file = request.files.get("image_file")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
                    pagamento = :pag
            """,
            ExpressionAttributeValues={
                ":r": rental_date_str,
                ":rt": return_date_str,
                ":c": comments,
                ":i": new_image_url,
                ":dc": description,
                ":cn": client_name,
                ":ct": client_tel,
                ":ret": retirado,
                ":val": valor,
                ":pag": pagamento,
            },
        )

        flash("Vestido atualizado com sucesso.", "success")
        # Redirecionar de acordo com o status atual

        return redirect(next_page)

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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

    return render_template("edit_small.html", dress=dress)


@app.route("/delete/<dress_id>", methods=["POST"])
def delete(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    try:
        # Obter o item antes de deletar
        response = table.get_item(Key={"dress_id": dress_id})
        item = response.get("Item")
        if item:
            image_url = item.get("image_url")

            # Se existe image_url, tentar deletar o objeto no S3
            if image_url and image_url.strip():
                parsed_url = urlparse(image_url)
                object_key = parsed_url.path.lstrip(
                    "/"
                )  # Extrair a chave do objeto no S3
                # Deletar o objeto do S3
                s3.delete_object(Bucket=s3_bucket_name, Key=object_key)

            # Apagar registro no DynamoDB
            table.delete_item(Key={"dress_id": dress_id})
            flash("Item deletado com sucesso! ", "success")  # Mensagem de sucesso
        else:
            flash(
                "Vestido não encontrado.", "error"
            )  # Mensagem de erro para vestido inexistente

    except Exception as e:
        # Registrar ou tratar o erro aqui, se necessário
        flash(
            f"Ocorreu um erro ao tentar deletar o vestido: {str(e)}", "error"
        )  # Mensagem de erro

    # Redirecionar para a página anterior (index ou returned)

    return redirect(next_page)


@app.route("/mark_returned/<dress_id>", methods=["GET", "POST"])
def mark_returned(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obtém a data atual
    dev_date = datetime.datetime.now(manaus_tz).strftime("%Y-%m-%d")

    # Atualiza status para 'returned'
    # Atualiza status para 'returned' e insere data de devolução
    table.update_item(
        Key={"dress_id": dress_id},
        UpdateExpression="set #status = :s, dev_date = :d",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "returned", ":d": dev_date},
    )

    # flash("Vestido devolvido com sucesso.", "success")

    flash(
        "Item <a href='/returned'>devolvido</a> com sucesso.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/mark_archived/<dress_id>", methods=["GET", "POST"])
def mark_archived(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")

    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("returned"))

    # Criar uma cópia do item antes de qualquer modificação
    new_dress_id = str(uuid.uuid4())
    copied_item = item.copy()  # Copiar todos os campos do item original
    copied_item["dress_id"] = new_dress_id
    copied_item["original_id"] = dress_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] != "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"dress_id", "description", "image_url", "valor"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "archived"

    # Atualizar o item original no DynamoDB
    table.put_item(Item=filtered_item)

    flash(
        "Item <a href='/archive'>arquivado</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )
    return redirect(next_page)


@app.route("/mark_available/<dress_id>", methods=["GET", "POST"])
def mark_available(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")

    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("returned"))

    if "archive" in next_page:
        # Atualiza status para 'returned' e insere data de devolução
        table.update_item(
            Key={"dress_id": dress_id},
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
    copied_item["dress_id"] = new_dress_id
    copied_item["original_id"] = dress_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] is not "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"dress_id", "description", "image_url", "valor"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "available"

    # Atualizar o item original no DynamoDB
    table.put_item(Item=filtered_item)

    # flash("Vestido está disponível agora e cópia histórica criada.", "success")

    flash(
        "Item <a href='/available'>disponível</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )

    return redirect(url_for("returned"))


def verify_user(email, password):
    try:
        response = users_table.get_item(Key={"email": email})

        if "Item" in response:
            user = response["Item"]
            stored_hash = user["password_hash"]

            # Se o e-mail não estiver confirmado, bloquear login
            if not user.get("email_confirmed", False):
                return "Pendente", None  # Novo retorno indicando pendência

            return check_password_hash(stored_hash, password), user.get("role", "user")

    except Exception as e:
        print(f"Erro ao verificar usuário: {e}")
    return False, None


@app.route("/mark_rented/<dress_id>", methods=["POST"])
def mark_rented(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    # Atualiza status para 'rented'
    table.update_item(
        Key={"dress_id": dress_id},
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


# Rota de Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
