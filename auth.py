import time
import uuid
import secrets
import datetime
import pytz
import re
from utils import get_user_timezone
from flask import session
from flask import request
import json
import requests
from boto3.dynamodb.conditions import Key
import os
from urllib.parse import urlparse
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from botocore.exceptions import ClientError

from utils import (
    send_confirmation_email,
    send_password_reset_email,
    send_admin_notification_email,
    send_confirmation_email,
    get_user_ip,
    get_account_plan,
)


def init_auth_routes(
    app, users_table, reset_tokens_table, payment_transactions_table
):
    def _color_settings_key(account_id):
        return f"account_settings:{account_id}"

    def _default_size_options():
        return ["P", "M", "G", "Extra G"]

    def _load_color_options(account_id):
        if not account_id:
            return []
        resp = users_table.get_item(Key={"user_id": _color_settings_key(account_id)})
        item = resp.get("Item") or {}
        colors = item.get("color_options")
        if not isinstance(colors, list):
            return []

        out = []
        seen = set()
        for c in colors:
            if c is None:
                continue
            s = str(c).strip()
            if not s:
                continue
            k = s.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return sorted(out, key=lambda x: str(x).casefold())

    def _save_color_options(account_id, colors):
        if not account_id:
            raise ValueError("account_id não encontrado na sessão.")
        cleaned = []
        seen = set()
        for c in colors or []:
            if c is None:
                continue
            s = str(c).strip()
            if not s:
                continue
            k = s.casefold()
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(s)
        users_table.update_item(
            Key={"user_id": _color_settings_key(account_id)},
            UpdateExpression="SET color_options = :c",
            ExpressionAttributeValues={":c": cleaned},
        )

    def _load_size_options(account_id):
        if not account_id:
            return _default_size_options()
        resp = users_table.get_item(Key={"user_id": _color_settings_key(account_id)})
        item = resp.get("Item") or {}
        sizes = item.get("size_options")
        if not isinstance(sizes, list):
            return _default_size_options()

        out = []
        seen = set()
        for s in sizes:
            if s is None:
                continue
            val = str(s).strip()
            if not val:
                continue
            k = val.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(val)
        return out

    def _save_size_options(account_id, sizes):
        if not account_id:
            raise ValueError("account_id não encontrado na sessão.")
        cleaned = []
        seen = set()
        for s in sizes or []:
            if s is None:
                continue
            val = str(s).strip()
            if not val:
                continue
            k = val.casefold()
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(val)
        users_table.update_item(
            Key={"user_id": _color_settings_key(account_id)},
            UpdateExpression="SET size_options = :s",
            ExpressionAttributeValues={":s": cleaned},
        )

    # Registration route
    @app.route("/register", methods=["GET", "POST"])
    def register():
        abort(404)

    # Login route
    @app.route("/login", methods=["GET", "POST"])
    def login():
        def _safe_next_url(next_url):
            if not next_url:
                return None
            next_url = str(next_url).strip()
            if not next_url.startswith("/"):
                return None
            parsed = urlparse(next_url)
            if parsed.scheme or parsed.netloc:
                return None
            return next_url

        def _default_redirect_for_role(role):
            return url_for("admin_dashboard") if role == "general_admin" else url_for("agenda")

        next_url = _safe_next_url(request.args.get("next") or request.form.get("next"))

        if session.get("logged_in") and request.method == "GET":
            return redirect(next_url or _default_redirect_for_role(session.get("role")))

        remember_me = request.form.get("remember_me")

        if request.method == "POST":
            if session.get("logged_in"):
                session.clear()
            email = request.form.get("email")
            password = request.form.get("password")

            response = users_table.query(
                IndexName="email-index",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={":email": email},
            )

            items = response.get("Items", [])
            if not items:
                flash(
                    "E-mail ou senha incorretos.",
                    "danger",
                )
                return redirect(url_for("login"))

            user = None
            stored_hash = None
            user_id = None
            candidates = []
            for candidate in items:
                candidate_user_id = candidate.get("user_id")
                if not candidate_user_id:
                    continue
                resp = users_table.get_item(Key={"user_id": candidate_user_id})
                candidate_user = resp.get("Item")
                if not candidate_user:
                    continue
                candidates.append(candidate_user)

            for candidate_user in candidates:
                if candidate_user.get("role") != "general_admin":
                    continue
                candidate_hash = candidate_user.get("password_hash")
                if candidate_hash and check_password_hash(candidate_hash, password):
                    user = candidate_user
                    stored_hash = candidate_hash
                    user_id = candidate_user.get("user_id")
                    break

            if not user:
                for candidate_user in candidates:
                    candidate_hash = candidate_user.get("password_hash")
                    if candidate_hash and check_password_hash(candidate_hash, password):
                        user = candidate_user
                        stored_hash = candidate_hash
                        user_id = candidate_user.get("user_id")
                        break

            if not user:
                flash(
                    "E-mail ou senha incorretos.",
                    "danger",
                )
                return redirect(url_for("login"))

            username = user.get("username")
            account_id = user.get("account_id")
            status = user.get("status", "active")
            role = user.get("role", "user")

            if status == "canceled":
                flash("Conta cancelada. Será deletada em breve.", "danger")
                return redirect(url_for("login"))

            if role != "general_admin" and status != "active":
                if status == "pending":
                    flash(
                        "Conta aguardando aprovação do administrador. Entre em contato com o suporte.",
                        "warning",
                    )
                else:
                    flash("Conta inativa. Entre em contato com o suporte.", "danger")
                return redirect(url_for("login"))

            if remember_me:
                session.permanent = True
            else:
                session.permanent = False

            session["logged_in"] = True
            session["email"] = user.get("email") or email
            session["role"] = role
            session["username"] = username
            session["user_id"] = user_id
            session["account_id"] = account_id

            flash("Você está logado agora!", "info")
            return redirect(next_url or _default_redirect_for_role(session.get("role")))

        return render_template("login.html")

    @app.route("/cadastro-sucesso")
    def cadastro_sucesso():
        abort(404)

    # Email confirmation
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

    # Resend confirmation email
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

            items = response.get("Items", [])
            if not items:
                flash("E-mail não encontrado.", "danger")
                return redirect(url_for("login"))

            user = items[0]

            # Se já está confirmado, não precisa reenviar
            if user.get("email_confirmed", False):
                flash("Este e-mail já foi confirmado. Faça login.", "info")
                return redirect(url_for("login"))

            # Verificar tempo desde o último envio
            last_email_sent = user.get("last_email_sent", None)
            user_id = session.get("user_id") if "user_id" in session else None
            user_utc = get_user_timezone(users_table, user_id)
            now = datetime.datetime.now(user_utc)
            cooldown_seconds = 180  # 3 minutos de cooldown

            if last_email_sent:
                last_email_sent_time = datetime.datetime.fromisoformat(last_email_sent)
                seconds_since_last_email = (now - last_email_sent_time).total_seconds()

                if seconds_since_last_email < cooldown_seconds:
                    flash(
                        f"Você já solicitou um reenvio recentemente. Aguarde {int(cooldown_seconds - seconds_since_last_email)} segundos.",
                        "warning",
                    )
                    return redirect(url_for("login"))

            # Gerar um novo token e atualizar o banco
            email_token = secrets.token_urlsafe(16)

            # Get user_id from the first item
            user_id = user["user_id"]

            # Atualizar o email_token na tabela principal usando o user_id
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET email_token = :token, last_email_sent = :time",
                ExpressionAttributeValues={
                    ":token": email_token,
                    ":time": now.isoformat(),
                },
            )

            # Generate confirmation URL
            confirm_url = url_for("confirm_email", token=email_token, _external=True)

            # Reenviar e-mail
            send_confirmation_email(email, user["username"], confirm_url)

            flash("Um novo e-mail de confirmação foi enviado.", "success")
            return redirect(url_for("login"))

        except Exception as e:
            print(f"Erro ao reenviar e-mail: {e}")
            flash("Ocorreu um erro ao reenviar o e-mail. Tente novamente.", "danger")
            return redirect(url_for("login"))

    # Forgot password
    @app.route("/forgot-password", methods=["POST", "GET"])
    def forgot_password():
        email = request.form.get("email")
        if not email:
            return render_template(
                "forgot_password.html", error="Por favor, informe seu email"
            )

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
                confirm_url = url_for(
                    "confirm_email", token=email_token, _external=True
                )

                # Enviar e-mail de confirmação novamente
                send_confirmation_email(email, username, confirm_url)

                flash(
                    "Você solicitou redefinição de senha, mas ainda precisa confirmar seu e-mail!",
                    "info",
                )

            return render_template(
                "forgot_password.html",
                message="Se este email estiver cadastrado, uma instrução para redefinir sua senha foi enviada.",
            )

        # Mesmo se o email não existir, retornamos a mesma mensagem por segurança
        return render_template(
            "forgot_password.html",
            message="Se este email estiver cadastrado, enviaremos instruções para redefinir sua senha.",
        )

    # Reset password page
    @app.route("/reset-password/<token>", methods=["GET"])
    def reset_password_page(token):
        try:
            # Buscar token no DynamoDB

            response = reset_tokens_table.get_item(Key={"token": token})

            # Se o token não existir, pode ter sido deletado pelo TTL
            if "Item" not in response:
                return render_template(
                    "login.html",
                    error="Este link de redefinição é inválido ou expirou.",
                )

            token_data = response["Item"]

            # Verificar se o token já foi usado
            if token_data.get("used", False):
                return render_template(
                    "login.html",
                    error="Este link de redefinição já foi usado.",
                )

            # Verificar se o token expirou (caso ainda esteja na tabela)
            expires_at_unix = token_data.get("expires_at_unix")

            if expires_at_unix and time.time() > expires_at_unix:
                return render_template(
                    "login.html", error="Este link de redefinição expirou."
                )

            # Token válido, mostrar página de redefinição
            return render_template(
                "reset_password.html", reset_password=True, token=token
            )

        except Exception as e:
            print(f"Erro ao verificar token: {e}")
            return render_template(
                "reset_password.html",
                error="Ocorreu um erro ao processar sua solicitação.",
            )

    # Process password reset
    @app.route("/reset-password", methods=["POST"])
    def reset_password():
        token = request.form.get("token")
        new_password = request.form.get("new_password")
        confirm_new_password = request.form.get("confirm_new_password")

        if not token or not new_password or not confirm_new_password:
            return render_template(
                "reset_password.html",
                error="Todos os campos são obrigatórios",
                reset_password=True,
                token=token,
            )

        if new_password != confirm_new_password:
            return render_template(
                "reset_password.html",
                error="As senhas não coincidem",
                reset_password=True,
                token=token,
            )

        import re
        if (
            len(new_password) < 6
            or len(new_password) > 64
            or not re.search(r"[A-Za-z]", new_password)
            or not re.search(r"\d", new_password)
        ):
            flash("A nova senha deve ter entre 6 e 64 caracteres, com ao menos uma letra e um número.", "danger")
            return redirect(url_for("sua_rota_ou_página"))

        try:
            # Verificar se o token existe e é válido
            response = reset_tokens_table.get_item(Key={"token": token})

            if "Item" in response:
                token_data = response["Item"]

                # Verificar se o token já foi usado
                if token_data.get("used", False):
                    return render_template(
                        "login.html",
                        error="Este link de redefinição já foi usado",
                    )

                # Verificar se o token expirou
                expires_at_unix = token_data.get("expires_at_unix")
                if expires_at_unix and time.time() > expires_at_unix:
                    return render_template(
                        "reset_password.html", error="Este link de redefinição expirou"
                    )

                # Token válido, obter user_id associado ao token
                user_id = token_data["user_id"]
                password_hash = generate_password_hash(
                    new_password, method="pbkdf2:sha256"
                )

                #user_id = session.get("user_id") if "user_id" in session else None
                user_utc = get_user_timezone(users_table, user_id)

                print("updating password in db...")

                # Atualizar senha no banco de dados
                users_table.update_item(
                    Key={"user_id": user_id},
                    UpdateExpression="SET password_hash = :p, updated_at = :u",
                    ExpressionAttributeValues={
                        ":p": password_hash,
                        ":u": datetime.datetime.now(user_utc).isoformat(),
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
                return render_template(
                    "reset_password.html", error="Link de redefinição inválido"
                )

        except Exception as e:
            print(f"Erro ao redefinir senha: {e}")
            return render_template(
                "reset_password.html",
                error="Ocorreu um erro ao processar sua solicitação",
                reset_password=True,
                token=token,
            )

    # Change password
    @app.route("/change-password", methods=["POST"])
    def change_password():
        if not session.get("logged_in"):
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

        if len(new_password) < 8 or len(new_password) > 64:
            flash(
                "A nova senha deve ter mais que 8 e menos que 64 caracteres.", "danger"
            )
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
            new_password_hash = generate_password_hash(
                new_password, method="pbkdf2:sha256"
            )

            user_id = session.get("user_id") if "user_id" in session else None
            user_utc = get_user_timezone(users_table, user_id)

            # Atualizar a senha no banco de dados
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET password_hash = :p, updated_at = :u",
                ExpressionAttributeValues={
                    ":p": new_password_hash,
                    ":u": datetime.datetime.now(user_utc).date().isoformat(),
                },
            )

            flash("Senha alterada com sucesso!", "success")
            return redirect(url_for("adjustments"))

        except Exception as e:
            print(f"Erro ao alterar a senha: {e}")
            flash("Ocorreu um erro ao processar a alteração da senha.", "danger")
            return redirect(url_for("adjustments"))

    # Change username
    @app.route("/change-username", methods=["POST"])
    def change_username():
        if not session.get("logged_in"):
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

            # Atualizar o nome de usuário
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET username = :new_username",
                ExpressionAttributeValues={":new_username": new_username},
            )

            session["username"] = new_username

            flash("Nome de usuário atualizado com sucesso!", "success")
            return redirect(url_for("adjustments"))

        except Exception as e:
            print(f"Erro ao atualizar nome de usuário: {e}")
            flash("Ocorreu um erro ao atualizar o nome de usuário.", "danger")
            return redirect(url_for("adjustments"))

    # Change timezone
    @app.route("/change_timezone", methods=["POST"])
    def change_timezone():
        if not session.get("logged_in"):
            flash("Você precisa estar logado para alterar o fuso horário.", "danger")
            return redirect(url_for("login"))

        selected_timezone = request.form.get("timezone", "").strip()

        if selected_timezone not in pytz.all_timezones:
            flash("Fuso horário inválido.", "danger")
            return redirect(url_for("adjustments"))

        user_id = session["user_id"]

        try:
            response = users_table.get_item(Key={"user_id": user_id})
            print(">>> Usuário encontrado:", response.get("Item"))

            if "Item" not in response:
                flash("Usuário não encontrado.", "danger")
                return redirect(url_for("adjustments"))

            print(">>> Atualizando timezone para:", selected_timezone)

            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET #tz = :tz",
                ExpressionAttributeNames={"#tz": "timezone"},
                ExpressionAttributeValues={":tz": selected_timezone},
            )
            print(">>> Timezone atualizado com sucesso.")
            flash("Fuso horário atualizado com sucesso!", "success")
            return redirect(url_for("adjustments"))
        except Exception as e:
            print(f"Erro ao atualizar timezone: {e}")
            flash("Ocorreu um erro ao atualizar o fuso horário.", "danger")
            return redirect(url_for("adjustments"))

    # Request email change
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

        # Encontrar user_id pelo email
        response = users_table.query(
            IndexName="email-index",
            KeyConditionExpression="email = :email",
            ExpressionAttributeValues={":email": user_email},
        )

        if not response.get("Items"):
            flash("Erro ao encontrar sua conta.", "danger")
            return redirect(url_for("adjustments"))

        user_id = response["Items"][0]["user_id"]

        # Salvar token temporário no banco
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET pending_email = :new_email, email_token = :token",
            ExpressionAttributeValues={":new_email": new_email, ":token": email_token},
        )

        # Enviar e-mail de confirmação
        confirm_url = url_for("confirm_email_change", token=email_token, _external=True)

        # Get SES client from the app context
        from app import ses_client

        ses_client.send_email(
            Source="nao_responda@locashop.com.br",
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

    # Confirm email change
    @app.route("/confirm-email-change/<token>")
    def confirm_email_change(token):
        # Buscar usuário pelo token de confirmação no GSI
        response = users_table.query(
            IndexName="email_token-index",  # Nome do GSI
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

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Você saiu com sucesso!", "success")
        return redirect(url_for("index"))
        # User profile settings

    @app.route("/adjustments", methods=["GET", "POST"])
    def adjustments():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        user_id = session.get("user_id")
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" not in response:
            flash("Erro ao carregar dados do usuário.", "danger")
            return redirect(url_for("login"))

        user = response["Item"]
        username = user.get("username", "Usuário Desconhecido")
        user_email = user.get("email", "Usuário Desconhecido")
        current_timezone = user.get("timezone", "America/Sao_Paulo")
        can_manage_colors = session.get("role") in ["admin", "general_admin"]
        can_manage_sizes = session.get("role") in ["admin", "general_admin"]
        color_options = []
        if can_manage_colors:
            try:
                color_options = _load_color_options(session.get("account_id"))
            except Exception as e:
                print(f"Erro ao carregar cores: {e}")
                flash(f"Erro ao carregar cores: {e}", "danger")
                color_options = []

        size_options = []
        if can_manage_sizes:
            try:
                size_options = _load_size_options(session.get("account_id"))
            except Exception as e:
                print(f"Erro ao carregar tamanhos: {e}")
                flash(f"Erro ao carregar tamanhos: {e}", "danger")
                size_options = []

        transactions = []
        current_transaction = {}

        return render_template(
            "adjustments.html",
            username=username,
            email=user_email,
            current_timezone=current_timezone,
            current_transaction=current_transaction,
            transactions=transactions,
            can_manage_colors=can_manage_colors,
            color_options=color_options,
            can_manage_sizes=can_manage_sizes,
            size_options=size_options,
            timezones=[
                "America/Sao_Paulo",
                "America/Fortaleza",
                "America/Recife",
                "America/Bahia",
                "America/Manaus",
                "America/Belem",
                "America/Boa_Vista",
                "America/Campo_Grande",
                "America/Cuiaba",
                "America/Porto_Velho",
                "America/Rio_Branco",
            ],
        )

    @app.route("/colors/add", methods=["POST"])
    def add_color_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        raw = request.form.get("color", "")
        color = str(raw).strip()
        if not color:
            flash("Informe uma cor válida.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_color_options(account_id)
        wanted_key = color.casefold()
        if any(str(c).strip().casefold() == wanted_key for c in existing):
            flash("Essa cor já existe.", "warning")
            return redirect(url_for("adjustments"))

        existing.append(color)
        try:
            _save_color_options(account_id, existing)
            flash("Cor adicionada com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao adicionar cor: {e}")
            flash(f"Erro ao adicionar cor: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/colors/delete", methods=["POST"])
    def delete_color_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        raw = request.form.get("color", "")
        color = str(raw).strip()
        if not color:
            flash("Informe uma cor válida.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_color_options(account_id)
        wanted_key = color.casefold()
        next_colors = [c for c in existing if str(c).strip().casefold() != wanted_key]
        if len(next_colors) == len(existing):
            flash("Cor não encontrada.", "warning")
            return redirect(url_for("adjustments"))

        try:
            _save_color_options(account_id, next_colors)
            flash("Cor removida com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao remover cor: {e}")
            flash(f"Erro ao remover cor: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/colors/edit", methods=["POST"])
    def edit_color_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        old_raw = request.form.get("old_color", "")
        new_raw = request.form.get("new_color", "")
        old_color = str(old_raw).strip()
        new_color = str(new_raw).strip()
        if not old_color or not new_color:
            flash("Informe uma cor válida.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_color_options(account_id)
        old_key = old_color.casefold()
        new_key = new_color.casefold()

        if old_key != new_key and any(str(c).strip().casefold() == new_key for c in existing):
            flash("Essa cor já existe.", "warning")
            return redirect(url_for("adjustments"))

        updated = []
        replaced = False
        for c in existing:
            if not replaced and str(c).strip().casefold() == old_key:
                updated.append(new_color)
                replaced = True
            else:
                updated.append(c)

        if not replaced:
            flash("Cor não encontrada.", "warning")
            return redirect(url_for("adjustments"))

        try:
            _save_color_options(account_id, updated)
            flash("Cor atualizada com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao editar cor: {e}")
            flash(f"Erro ao editar cor: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/sizes/add", methods=["POST"])
    def add_size_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        raw = request.form.get("size", "")
        size = str(raw).strip()
        if not size:
            flash("Informe um tamanho válido.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_size_options(account_id)
        wanted_key = size.casefold()
        if any(str(s).strip().casefold() == wanted_key for s in existing):
            flash("Esse tamanho já existe.", "warning")
            return redirect(url_for("adjustments"))

        existing.append(size)
        try:
            _save_size_options(account_id, existing)
            flash("Tamanho adicionado com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao adicionar tamanho: {e}")
            flash(f"Erro ao adicionar tamanho: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/sizes/delete", methods=["POST"])
    def delete_size_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        raw = request.form.get("size", "")
        size = str(raw).strip()
        if not size:
            flash("Informe um tamanho válido.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_size_options(account_id)
        wanted_key = size.casefold()
        next_sizes = [s for s in existing if str(s).strip().casefold() != wanted_key]
        if len(next_sizes) == len(existing):
            flash("Tamanho não encontrado.", "warning")
            return redirect(url_for("adjustments"))

        try:
            _save_size_options(account_id, next_sizes)
            flash("Tamanho removido com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao remover tamanho: {e}")
            flash(f"Erro ao remover tamanho: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/sizes/edit", methods=["POST"])
    def edit_size_option():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") not in ["admin", "general_admin"]:
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("adjustments"))

        old_raw = request.form.get("old_size", "")
        new_raw = request.form.get("new_size", "")
        old_size = str(old_raw).strip()
        new_size = str(new_raw).strip()
        if not old_size or not new_size:
            flash("Informe um tamanho válido.", "danger")
            return redirect(url_for("adjustments"))

        account_id = session.get("account_id")
        existing = _load_size_options(account_id)
        old_key = old_size.casefold()
        new_key = new_size.casefold()

        if old_key != new_key and any(str(s).strip().casefold() == new_key for s in existing):
            flash("Esse tamanho já existe.", "warning")
            return redirect(url_for("adjustments"))

        updated = []
        replaced = False
        for s in existing:
            if not replaced and str(s).strip().casefold() == old_key:
                updated.append(new_size)
                replaced = True
            else:
                updated.append(s)

        if not replaced:
            flash("Tamanho não encontrado.", "warning")
            return redirect(url_for("adjustments"))

        try:
            _save_size_options(account_id, updated)
            flash("Tamanho atualizado com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao editar tamanho: {e}")
            flash(f"Erro ao editar tamanho: {e}", "danger")
        return redirect(url_for("adjustments"))

    @app.route("/admin-dashboard")
    def admin_dashboard():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta página.", "danger")
            return redirect(url_for("rented"))

        # Pega informações da navegação
        nav_stack_str = request.args.get("nav_stack", "[]")
        page = int(request.args.get("page", 1))
        nav_stack = json.loads(nav_stack_str)

        # Define o last_key atual baseado na página
        last_key = (
            nav_stack[page - 2] if page > 1 and len(nav_stack) >= page - 1 else None
        )

        users_page, next_key = get_all_users(users_table, last_key)

        # Atualiza a pilha de navegação
        if next_key and len(nav_stack) < page:
            nav_stack.append(next_key)

        return render_template(
            "admin_dashboard.html",
            users=users_page,
            nav_stack=json.dumps(nav_stack),
            page=page,
            has_next=bool(next_key),
            has_prev=page > 1,
        )

    @app.route("/admin/accounts/approve", methods=["POST"])
    def approve_account():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("rented"))

        raw = request.form.get("account_id", "")
        account_id = str(raw).strip()
        if not account_id:
            flash("account_id inválido.", "danger")
            return redirect(url_for("admin_dashboard"))

        try:
            scan_kwargs = {
                "FilterExpression": "account_id = :aid",
                "ExpressionAttributeValues": {":aid": account_id},
                "ProjectionExpression": "user_id",
            }
            updated = 0
            while True:
                resp = users_table.scan(**scan_kwargs)
                for it in resp.get("Items", []):
                    uid = it.get("user_id")
                    if not uid:
                        continue
                    users_table.update_item(
                        Key={"user_id": uid},
                        UpdateExpression="SET #s = :s",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": "active"},
                    )
                    updated += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key

            if updated == 0:
                flash("Nenhum usuário encontrado para esta conta.", "warning")
            else:
                flash("Conta aprovada com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao aprovar conta: {e}")
            flash(f"Erro ao aprovar conta: {e}", "danger")

        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/accounts/set-status", methods=["POST"])
    def set_account_status():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("rented"))

        account_id = str(request.form.get("account_id", "")).strip()
        status = str(request.form.get("status", "")).strip()
        if not account_id:
            flash("account_id inválido.", "danger")
            return redirect(url_for("admin_dashboard"))
        if status not in ["active", "inactive"]:
            flash("Status inválido.", "danger")
            return redirect(url_for("admin_dashboard"))

        try:
            scan_kwargs = {
                "FilterExpression": "account_id = :aid",
                "ExpressionAttributeValues": {":aid": account_id},
                "ProjectionExpression": "user_id",
            }
            updated = 0
            while True:
                resp = users_table.scan(**scan_kwargs)
                for it in resp.get("Items", []):
                    uid = it.get("user_id")
                    if not uid:
                        continue
                    users_table.update_item(
                        Key={"user_id": uid},
                        UpdateExpression="SET #s = :s",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": status},
                    )
                    updated += 1
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key

            if updated == 0:
                flash("Nenhum usuário encontrado para esta conta.", "warning")
            else:
                flash("Status da conta atualizado.", "success")
        except Exception as e:
            print(f"Erro ao atualizar status da conta: {e}")
            flash(f"Erro ao atualizar status da conta: {e}", "danger")

        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/users/create", methods=["POST"])
    def admin_create_user():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("rented"))

        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not username or not password:
            flash("Informe nome e senha.", "danger")
            return redirect(url_for("admin_dashboard"))

        account_id = session.get("account_id")
        email = session.get("email")
        if not account_id or not email:
            flash("Sessão inválida. Faça login novamente.", "danger")
            return redirect(url_for("login"))

        status = "active"

        try:
            ok = create_user(
                email=email,
                username=username,
                password=password,
                users_table=users_table,
                app=app,
                payment_transactions_table=payment_transactions_table,
                role="admin",
                user_ip=get_user_ip(),
                status=status,
                account_id=account_id,
                send_confirmation_email_flag=False,
            )
            if ok:
                flash("Usuário criado com sucesso.", "success")
            else:
                flash("Não foi possível criar o usuário.", "danger")
        except Exception as e:
            print(f"Erro ao criar usuário: {e}")
            flash(f"Erro ao criar usuário: {e}", "danger")

        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/users/delete", methods=["POST"])
    def admin_delete_user():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("rented"))

        user_id = str(request.form.get("user_id", "")).strip()
        if not user_id:
            flash("Usuário inválido.", "danger")
            return redirect(url_for("admin_dashboard"))

        try:
            resp = users_table.get_item(Key={"user_id": user_id})
            item = resp.get("Item")
            if not item:
                flash("Usuário não encontrado.", "warning")
                return redirect(url_for("admin_dashboard"))

            if item.get("role") != "admin":
                flash("Apenas usuários admin podem ser deletados aqui.", "danger")
                return redirect(url_for("admin_dashboard"))

            if item.get("account_id") != session.get("account_id"):
                flash("Você não tem permissão para deletar este usuário.", "danger")
                return redirect(url_for("admin_dashboard"))

            users_table.delete_item(Key={"user_id": user_id})
            flash("Usuário deletado com sucesso.", "success")
        except Exception as e:
            print(f"Erro ao deletar usuário: {e}")
            flash(f"Erro ao deletar usuário: {e}", "danger")

        return redirect(url_for("admin_dashboard"))

    # Login as user (impersonation)
    @app.route("/login-as-user/<user_id>")
    def login_as_user(user_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Verificar se o usuário tem permissão de general_admin
        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta funcionalidade.", "danger")
            return redirect(url_for("rented"))

        # Guardar ID do admin
        admin_user_id = session.get("user_id")
        admin_username = session.get("username")

        # Obter dados do usuário que será impersonado
        response = users_table.get_item(Key={"user_id": user_id})

        if "Item" not in response:
            flash("Usuário não encontrado.", "danger")
            return redirect(url_for("admin_dashboard"))

        user = response["Item"]

        # Atualizar dados da sessão
        session["user_id"] = user["user_id"]
        session["account_id"] = user["account_id"]
        session["email"] = user["email"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["impersonated_by"] = admin_user_id
        session["admin_username"] = admin_username

        flash(
            f"Você está logado como {user['username']}. As ações realizadas serão registradas.",
            "warning",
        )
        return redirect(url_for("rented"))

    # Return to admin account
    @app.route("/return-to-admin")
    def return_to_admin():
        if not session.get("impersonated_by"):
            return redirect(url_for("rented"))

        admin_id = session.get("impersonated_by")

        # Obter dados do admin
        response = users_table.get_item(Key={"user_id": admin_id})

        if "Item" not in response:
            # Se algo der errado, fazer logout
            return redirect(url_for("logout"))

        admin = response["Item"]

        # Restaurar sessão do admin
        session["user_id"] = admin["user_id"]
        session["account_id"] = admin["account_id"]
        session["email"] = admin["email"]
        session["username"] = admin["username"]
        session["role"] = admin["role"]
        session.pop("impersonated_by", None)
        session.pop("admin_username", None)

        flash("Você retornou à sua conta de administrador.", "info")
        return redirect(url_for("admin_dashboard"))



def create_user(
    email,
    username,
    password,
    users_table,
    app,
    payment_transactions_table,
    role="admin",
    user_ip=None,
    status="active",
    account_id=None,
    send_confirmation_email_flag=True,
):
    """Create a new user."""
    with app.app_context():
        password_hash = generate_password_hash(password, method="pbkdf2:sha256")
        user_id = str(uuid.uuid4())
        account_id = str(account_id).strip() if account_id else str(uuid.uuid4())
        current_user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, current_user_id)

        try:
            item = {
                "user_id": user_id,
                "account_id": account_id,
                "email": email,
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "created_at": datetime.datetime.now(user_utc).isoformat(),
                "email_confirmed": True,
                "status": status,
            }

            if user_ip:
                item["ip"] = user_ip

            users_table.put_item(Item=item)

            if send_confirmation_email_flag:
                email_token = secrets.token_urlsafe(16)
                users_table.update_item(
                    Key={"user_id": user_id},
                    UpdateExpression="SET email_token = :t, last_email_sent = :d",
                    ExpressionAttributeValues={
                        ":t": email_token,
                        ":d": datetime.datetime.now(user_utc).isoformat(),
                    },
                )
                confirm_url = url_for("confirm_email", token=email_token, _external=True)
                send_confirmation_email(email, username, confirm_url)

            admin_email = None
            try:
                if session.get("role") == "general_admin":
                    admin_email = session.get("email")
            except Exception:
                admin_email = None
            send_admin_notification_email(
                admin_email=admin_email or "marciosferreira@yahoo.com.br",
                new_user_email=email,
                new_user_username=username,
            )

            return True

        except ClientError as e:
            print("🔴 Erro do DynamoDB:", e)
            raise
        except Exception as e:
            print("🔴 Erro geral:", e)
            raise


def get_all_users(users_table, last_evaluated_key=None, limit=5):
    """Busca usuários admin com paginação."""
    try:
        query_kwargs = {
            "IndexName": "role-index",
            "KeyConditionExpression": "#r = :r",
            "ExpressionAttributeValues": {":r": "admin"},
            "ProjectionExpression": "#r, user_id, username, email, account_id, #s",
            "ExpressionAttributeNames": {"#r": "role", "#s": "status"},
            "Limit": limit,
        }

        if last_evaluated_key:
            query_kwargs["ExclusiveStartKey"] = last_evaluated_key

        response = users_table.query(**query_kwargs)
        users = response.get("Items", [])
        next_key = response.get("LastEvaluatedKey")

        return users, next_key

    except Exception as e:
        print(f"Erro ao recuperar usuários: {e}")
        return [], None


def get_user_stats(
    user_id, users_table, itens_table, clients_table, transactions_table
):
    """Get stats for a specific user"""
    try:
        # Get basic user data
        response = users_table.get_item(Key={"user_id": user_id})
        user = response.get("Item", {})

        if not user:
            return None

        # Get item count (only "available" items)
        items_response = itens_table.scan(
            FilterExpression="account_id = :aid AND #status = :status",
            ExpressionAttributeValues={
                ":aid": user.get("account_id"),
                ":status": "available",
            },
            ExpressionAttributeNames={"#status": "status"},
        )

        # Get client count
        clients_response = clients_table.scan(
            FilterExpression="account_id = :aid",
            ExpressionAttributeValues={":aid": user.get("account_id")},
        )

        # Get transaction count (only "rented" or "returned" transactions)
        transactions_response = transactions_table.scan(
            FilterExpression="account_id = :aid AND (#status = :status1 OR #status = :status2)",
            ExpressionAttributeValues={
                ":aid": user.get("account_id"),
                ":status1": "rented",
                ":status2": "returned",
            },
            ExpressionAttributeNames={"#status": "status"},
        )

        # Add stats to user object
        user["item_count"] = len(items_response.get("Items", []))
        user["client_count"] = len(clients_response.get("Items", []))
        user["transaction_count"] = len(transactions_response.get("Items", []))

        return user
    except Exception as e:
        print(f"Erro ao recuperar estatísticas do usuário: {e}")
        return None



