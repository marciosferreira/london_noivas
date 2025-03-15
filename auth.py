import time
import uuid
import secrets
import datetime

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)
from werkzeug.security import generate_password_hash, check_password_hash
from botocore.exceptions import ClientError

from utils import send_confirmation_email, send_password_reset_email


def init_auth_routes(app, users_table, reset_tokens_table):
    # Registration route
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
                    "login.html",
                    error="Todos os campos são obrigatórios",
                    register=True,
                )

            success = create_user(email, username, password, users_table, app)
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

    # Login route
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
                return redirect(url_for("rented"))

            flash("E-mail ou senha incorretos.", "danger")

        return render_template("login.html")

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
            now = datetime.datetime.now()
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
                "login.html",
                message="Se este email estiver cadastrado, enviaremos instruções para redefinir sua senha.",
            )

        # Mesmo se o email não existir, retornamos a mesma mensagem por segurança
        return render_template(
            "login.html",
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
                    "login.html", error="Este link de redefinição já foi usado."
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
                "login.html", error="Ocorreu um erro ao processar sua solicitação."
            )

    # Process password reset
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

                # Verificar se o token expirou
                expires_at_unix = token_data.get("expires_at_unix")
                if expires_at_unix and time.time() > expires_at_unix:
                    return render_template(
                        "login.html", error="Este link de redefinição expirou"
                    )

                # Token válido, obter user_id associado ao token
                user_id = token_data["user_id"]
                password_hash = generate_password_hash(new_password)

                # Atualizar senha no banco de dados
                users_table.update_item(
                    Key={"user_id": user_id},
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
                return render_template(
                    "login.html", error="Link de redefinição inválido"
                )

        except Exception as e:
            print(f"Erro ao redefinir senha: {e}")
            return render_template(
                "login.html",
                error="Ocorreu um erro ao processar sua solicitação",
                reset_password=True,
                token=token,
            )

    # Change password
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

    # Change username
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

    # Logout route
    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # User profile settings
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


def create_user(email, username, password, users_table, app, role="admin"):
    """Create a new user in the database."""
    with app.app_context():
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

            # Generate confirmation URL
            confirm_url = url_for("confirm_email", token=email_token, _external=True)
            send_confirmation_email(email, username, confirm_url)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


def get_all_users(users_table):
    """Function for administrators to get all users."""
    try:
        response = users_table.scan()
        return response.get("Items", [])
    except Exception as e:
        print(f"Erro ao recuperar usuários: {e}")
        return []
