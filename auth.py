import time
import uuid
import secrets
import datetime
import pytz
from utils import get_user_timezone


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
                flash(
                    "A nova senha deve ter mais que 8 e menos que 64 caracteres.",
                    "danger",
                )
                return redirect(url_for("adjustments"))

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

        if session.get("logged_in"):  # Verifica se o usuário já está logado
            flash("Você já está logado!", "info")
            return redirect(url_for("index"))  # Redireciona para outra página
        remember_me = request.form.get("remember_me")

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

            if check_password_hash(stored_hash, password):
                if remember_me:
                    session.permanent = (
                        True  # Sessão durará conforme PERMANENT_SESSION_LIFETIME
                    )
                else:
                    session.permanent = False  # Será apagada ao fechar o navegador

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
                flash("Você esta logado agora!", "info")
                return redirect(url_for("index"))

            flash("E-mail ou senha incorretos.", "danger")

        return render_template("login.html")

    # Email confirmation
    @app.route("/confirm_email/<token>")
    def confirm_email(token):
        print(token)
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
            return render_template("login.html", reset_password=True, token=token)

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

        if len(new_password) < 8 or len(new_password) > 64:
            flash(
                "A nova senha deve ter mais que 8 e menos que 64 caracteres.", "danger"
            )
            return redirect(request.referrer)

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

                user_id = session.get("user_id") if "user_id" in session else None
                user_utc = get_user_timezone(users_table, user_id)

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
            new_password_hash = generate_password_hash(new_password)

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

        print(user_id)

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

    @app.route("/logout")
    def logout():
        # Armazena mensagens flash antes de limpar a sessão
        flash("Você saiu com sucesso!", "success")

        session.pop("logged_in", None)

        # Redireciona para a página inicial
        return redirect(url_for("index"))
        # User profile settings

    @app.route("/adjustments")
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
        current_timezone = user.get("timezone", "America/Sao_Paulo")  # valor padrão

        # Lista reduzida com as principais timezones do Brasil
        timezones = [
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
        ]

        return render_template(
            "adjustments.html",
            username=username,
            email=user_email,
            current_timezone=current_timezone,
            timezones=timezones,
        )

    # Admin dashboard
    @app.route("/admin-dashboard")
    def admin_dashboard():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Verificar se o usuário tem permissão de general_admin
        if session.get("role") != "general_admin":
            flash("Você não tem permissão para acessar esta página.", "danger")
            return redirect(url_for("rented"))

        # Importar as tabelas necessárias
        from app import itens_table, clients_table, transactions_table

        # Obter todos os usuários
        all_users = get_all_users(users_table)

        # Adicionar estatísticas para cada usuário
        users_with_stats = []
        for user in all_users:
            user_stats = get_user_stats(
                user["user_id"],
                users_table,
                itens_table,
                clients_table,
                transactions_table,
            )
            if user_stats:
                users_with_stats.append(user_stats)

        return render_template("admin_dashboard.html", users=users_with_stats)

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


def create_user(email, username, password, users_table, app, role="admin"):
    """Create a new user in the database."""
    from boto3.dynamodb.conditions import Key

    with app.app_context():
        password_hash = generate_password_hash(password)
        email_token = secrets.token_urlsafe(16)
        user_id = str(uuid.uuid4())
        account_id = str(uuid.uuid4())

        current_user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, current_user_id)

        # ✅ Verifica se o e-mail já está cadastrado via GSI
        response = users_table.query(
            IndexName="email-index", KeyConditionExpression=Key("email").eq(email)
        )

        if response["Count"] > 0:
            return False  # E-mail já cadastrado

        # ⬇️ Cria o novo usuário
        try:
            users_table.put_item(
                Item={
                    "user_id": user_id,
                    "account_id": account_id,
                    "email": email,
                    "username": username,
                    "password_hash": password_hash,
                    "role": role,
                    "created_at": datetime.datetime.now(user_utc).isoformat(),
                    "email_confirmed": False,
                    "email_token": email_token,
                    "last_email_sent": datetime.datetime.now(user_utc).isoformat(),
                }
            )

            confirm_url = url_for("confirm_email", token=email_token, _external=True)
            send_confirmation_email(email, username, confirm_url)
            return True

        except ClientError as e:
            print("Erro inesperado:", e)
            raise


def get_all_users(users_table):
    """Function for administrators to get all users."""
    try:
        response = users_table.scan()
        return response.get("Items", [])
    except Exception as e:
        print(f"Erro ao recuperar usuários: {e}")
        return []


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
