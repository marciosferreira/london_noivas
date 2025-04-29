import os
import uuid
import datetime
from flask import Flask, request, session

import boto3
from botocore.exceptions import ClientError
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

# AWS configuration
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
s3_bucket_name = "alugueqqc-images"

s3 = boto3.client(
    "s3",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)


# Email functions
def send_password_reset_email(email, username, reset_link):
    """Sends a password reset email to the user."""
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
        # Get SES client from the app context
        from app import ses_client

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


def send_confirmation_email(email, username, email_token):
    """Sends an email confirmation link to the user."""
    SENDER = "nao_responda@alugueqqc.com.br"  # Deve ser um email verificado no SES
    RECIPIENT = email
    SUBJECT = "Alugue QQC - Confirmação de E-mail"

    # Corpo do e-mail em HTML
    BODY_HTML = f"""
    <html>
    <head></head>
    <body>
    <h1>Confirmação de E-mail</h1>
    <p>Olá <strong>{username}</strong>,</p>
    <p>Obrigado por se cadastrar no Alugue QQC!</p>
    <p>Para ativar sua conta, clique no link abaixo:</p>
    <p><a href="{email_token}" style="font-size:16px; font-weight:bold; color:#ffffff; background-color:#007bff; padding:10px 20px; text-decoration:none; border-radius:5px;">Confirmar Meu E-mail</a></p>
    <p>Se o botão acima não funcionar, copie e cole o seguinte link no seu navegador:</p>
    <p><a href="{email_token}">{email_token}</a></p>
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
    {email_token}

    Se você não se cadastrou no Alugue QQC, ignore este e-mail.

    Atenciosamente,
    Equipe Alugue QQC
    """

    try:
        # Get SES client from the app context
        from app import ses_client

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


def send_admin_notification_email(admin_email, new_user_email, new_user_username):
    from app import ses_client

    subject = "Novo cadastro no sistema"
    body_html = f"""
    <html>
        <body>
            <h2>Novo usuário cadastrado</h2>
            <p><strong>Usuário:</strong> {new_user_username}</p>
            <p><strong>E-mail:</strong> {new_user_email}</p>
        </body>
    </html>
    """

    ses_client.send_email(
        Source="nao_responda@alugueqqc.com.br",
        Destination={"ToAddresses": [admin_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Html": {"Data": body_html},
            },
        },
    )


def upload_image_to_s3(image_file, prefix="images"):
    """Uploads an image to S3 and returns the URL."""
    if image_file:
        filename = secure_filename(image_file.filename)
        item_id = str(uuid.uuid4())
        s3_key = f"{prefix}/{item_id}_{filename}"
        s3.upload_fileobj(image_file, s3_bucket_name, s3_key)
        image_url = f"https://{s3_bucket_name}.s3.amazonaws.com/{s3_key}"
        return image_url
    return ""


def copy_image_in_s3(original_url):
    """Creates a copy of an image in S3 and returns the new URL."""
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
    item_obs=None,
    formatted_dev_date=None,
    dev_date=None,
):
    """Applies filters to item list and returns the filtered list."""
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
    if item_obs:
        filtered_items = [
            dress
            for dress in filtered_items
            if item_obs.lower() in dress.get("item_obs", "").lower()
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

    # Filtrar por intervalo de datas de retirada (rental_date)
    if start_date or end_date:
        filtered_items = [
            dress
            for dress in filtered_items
            if (not start_date or dress["rental_date_obj"] >= start_date)
            and (not end_date or dress["rental_date_obj"] <= end_date)
        ]

    # Filtrar por intervalo de datas de devolução (return_date)
    if return_start_date or return_end_date:
        filtered_items = [
            dress
            for dress in filtered_items
            if dress.get("return_date")
            and (
                (
                    not return_start_date
                    or datetime.datetime.strptime(
                        dress["return_date"], "%Y-%m-%d"
                    ).date()
                    >= return_start_date
                )
                and (
                    not return_end_date
                    or datetime.datetime.strptime(
                        dress["return_date"], "%Y-%m-%d"
                    ).date()
                    <= return_end_date
                )
            )
        ]

    # Filtrar por data de devolução específica (dev_date)
    if dev_date:
        try:
            dev_date_obj = (
                datetime.datetime.strptime(dev_date, "%Y-%m-%d").date()
                if isinstance(dev_date, str)
                else dev_date
            )
            filtered_items = [
                dress
                for dress in filtered_items
                if dress.get("dev_date")
                and datetime.datetime.strptime(dress.get("dev_date"), "%Y-%m-%d").date()
                == dev_date_obj
            ]
        except (ValueError, TypeError):
            print(f"Erro ao processar data de devolução: {dev_date}")

    return filtered_items


from datetime import datetime


# utils/time.py
import datetime
import pytz
from flask import session

import pytz

# utils/time.py
from flask import session, has_request_context


def get_user_timezone(users_table, user_id=None, fallback_tz="America/Sao_Paulo"):
    if not user_id and has_request_context():
        user_id = session.get("user_id")

    if not user_id:
        return pytz.timezone(fallback_tz)

    try:
        response = users_table.get_item(Key={"user_id": user_id})
        user = response.get("Item", {})
        user_timezone = user.get("timezone", fallback_tz)
        return pytz.timezone(user_timezone)
    except Exception as e:
        print("Erro ao obter timezone:", e)
        return pytz.timezone(fallback_tz)


def get_user_ip():
    if request.headers.get("X-Forwarded-For"):
        # Se vier o cabeçalho X-Forwarded-For (caso de proxy)
        ip = request.headers.get("X-Forwarded-For").split(",")[0]
    else:
        # Se não, pega o IP da conexão direta
        ip = request.remote_addr
    return ip


import boto3

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
accounts_table = dynamodb.Table("alugueqqc_accounts_table")


def get_account_plan(account_id):
    try:
        response = accounts_table.get_item(Key={"account_id": account_id})
        account = response.get("Item")

        if (
            account
            and account.get("plan_type") == "premium"
            and account.get("payment_status") == "active"
        ):
            return "premium"
        else:
            return "free"
    except Exception as e:
        # Em caso de erro, assume como free por segurança
        print(f"Erro ao consultar plano: {str(e)}")
        return "free"
