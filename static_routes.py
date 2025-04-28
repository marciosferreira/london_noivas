from flask import (
    render_template,
    request,
    session,
    redirect,
    url_for,
    flash,
    send_from_directory,
)

import stripe
import os

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")  # Correto!

import uuid
from decimal import Decimal
import datetime
from boto3.dynamodb.conditions import Key
from flask import render_template_string
from utils import get_user_timezone, get_account_plan
from flask import jsonify
import qrcode
import io
import base64
from flask import request
import os


def init_static_routes(
    app,
    ses_client,
    clients_table,
    transactions_table,
    itens_table,
    text_models_table,
    users_table,
    accounts_table,
):
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
                flash(
                    "Erro ao enviar a mensagem. Tente novamente mais tarde.", "danger"
                )

            return redirect(url_for("contato"))

        return render_template("contato.html")

    @app.route("/reportar-bug", methods=["GET", "POST"])
    def reportar_bug():
        url = request.args.get("url", "")

        if request.method == "POST":
            url = request.form.get("url")
            descricao = request.form.get("description")
            email = request.form.get("email", "Não informado")

            if not url or not descricao:
                flash("URL e descrição do bug são obrigatórios.", "danger")
                return redirect(url_for("reportar_bug"))

            # Enviar e-mail via AWS SES
            destinatario = "contato@alugueqqc.com.br"
            assunto = f"Bug reportado: {url}"
            corpo_email = (
                f"URL: {url}\nE-mail: {email}\n\nDescrição do Bug:\n{descricao}"
            )

            try:
                response = ses_client.send_email(
                    Source=destinatario,
                    Destination={"ToAddresses": [destinatario]},
                    Message={
                        "Subject": {"Data": assunto, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": corpo_email, "Charset": "UTF-8"}},
                    },
                )
                flash(
                    "Relatório de bug enviado com sucesso! Obrigado pela contribuição.",
                    "success",
                )
            except Exception as e:
                print(f"Erro ao enviar e-mail: {e}")
                flash(
                    "Erro ao enviar o relatório. Tente novamente mais tarde.", "danger"
                )

            return redirect(url_for("index"))

        return render_template("reportar-bug.html", url=url)

    @app.route("/ads.txt")
    def ads_txt():
        return send_from_directory("static", "ads.txt")

    @app.route("/fees")
    def fees():

        return render_template("fees.html")

    @app.route("/how_to")
    def how_to():
        return render_template("how_to.html")

    @app.route("/test_modal")
    def test_modal():
        return render_template("test_modal.html")

    @app.route("/modelos")
    def listar_modelos():
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        EXEMPLO_ACCOUNT_ID = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
        account_id = session.get("account_id")
        # Modelos do usuário logado
        response_user = text_models_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
        )
        modelos_usuario = response_user.get("Items", [])

        # Modelos de exemplo
        response_exemplos = text_models_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(EXEMPLO_ACCOUNT_ID),
        )
        modelos_exemplo = response_exemplos.get("Items", [])

        # Junta os dois
        modelos = modelos_usuario + modelos_exemplo
        return render_template("listar_modelos.html", modelos=modelos)

    @app.route("/criar-modelo", methods=["GET", "POST"])
    def criar_modelo():
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        account_id = session.get("account_id")
        if request.method == "POST":
            text_id = str(uuid.uuid4())
            nome = request.form["nome"]
            conteudo = request.form["conteudo"]

            text_models_table.put_item(
                Item={
                    "text_id": text_id,
                    "account_id": account_id,
                    "nome": nome,
                    "conteudo": conteudo,
                }
            )
            return redirect(url_for("listar_modelos"))

        return render_template("criar_modelo.html")

    @app.route("/visualizar-modelo/<text_id>/<transacao_id>")
    def visualizar_modelo(text_id, transacao_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recupera o modelo a partir do banco de dados
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")
        if not modelo:
            return "Modelo não encontrado.", 404

        # Recupera a transação do banco de dados
        response = transactions_table.get_item(Key={"transaction_id": transacao_id})
        transacao = response.get("Item")

        if not transacao:
            return "Transação não encontrada.", 404

        # Substitui as variáveis no conteúdo do modelo
        conteudo_renderizado = modelo["conteudo"]

        def formatar_data(valor):
            try:
                data = datetime.datetime.strptime(
                    valor, "%Y-%m-%d"
                )  # ou %Y/%m/%d dependendo da origem
                return data.strftime("%d/%m/%Y")
            except Exception:
                return valor

        if "rental_date" in transacao:
            transacao["rental_date_formatted"] = formatar_data(transacao["rental_date"])
        if "return_date" in transacao:
            transacao["return_date_formatted"] = formatar_data(transacao["return_date"])

        # send the current time to tempalte
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        # Gera a hora atual formatada (DD/MM/AAAA HH:MM)
        data_hora_atual = datetime.datetime.now(user_utc).strftime("%d/%m/%Y %H:%M")
        transacao["data_hora_atual"] = data_hora_atual

        # Substitui as variáveis do template com os dados da transação
        # Substitui as variáveis do template com os dados da transação
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_name }}",
            str(transacao.get("client_name", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_email }}",
            str(transacao.get("client_email", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_tel }}",
            str(transacao.get("client_tel", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_address }}",
            str(transacao.get("client_address", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_cpf }}", str(transacao.get("client_cpf", " "))
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_cnpj }}",
            str(transacao.get("client_cnpj", " ")),
        )

        # Dados do item
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ description }}",
            str(transacao.get("description", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ item_custom_id }}",
            str(transacao.get("item_custom_id", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ item_obs }}",
            str(transacao.get("item_obs", " ")),
        )

        # Dados da transação
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ valor }}", str(transacao.get("valor", " "))
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ pagamento }}",
            str(transacao.get("pagamento", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ transaction_obs }}",
            str(transacao.get("transaction_obs", " ")),
        )

        # Datas formatadas
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ rental_date_formatted }}",
            str(transacao.get("rental_date_formatted", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ return_date_formatted }}",
            str(transacao.get("return_date_formatted", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ data_hora_atual }}",
            str(transacao.get("data_hora_atual", " ")),
        )

        # Adicione mais substituições conforme necessário...

        # Passa o conteúdo renderizado para o template
        return render_template(
            "visualizar_modelo_conteudo.html", conteudo_renderizado=conteudo_renderizado
        )

    @app.route("/visualizar-modelo/<text_id>")
    def visualizar_modelo_simples(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")
        if not modelo:
            return "Modelo não encontrado."

        # Usa dados de exemplo para preenchimento
        dados_exemplo = {
            "client_name": "Joana Silva",
            "client_address": "Rua Exemplo, 123",
            "valor": 120.00,
            "pagamento": 60.00,
            "rental_date": "2025-04-18",
            "return_date": "2025-04-21",
            "data_hora_atual": "10-06-2025",
        }

        conteudo_renderizado = render_template_string(
            modelo["conteudo"],
            **dados_exemplo,
            modelo=modelo,
        )

        return render_template(
            "visualizar_modelo.html",
            modelo=modelo,
            conteudo_renderizado=conteudo_renderizado,
        )

    @app.route("/editar-modelo/<text_id>", methods=["GET", "POST"])
    def editar_modelo(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        EXEMPLO_ACCOUNT_ID = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
        account_id = session.get("account_id")

        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")

        if not modelo:
            flash("Modelo não encontrado.", "danger")
            return redirect(url_for("listar_modelos"))

        # Impede edição de modelos de outros usuários (mas permite leitura dos de exemplo)
        if (
            modelo.get("account_id") != account_id
            and modelo.get("account_id") != EXEMPLO_ACCOUNT_ID
        ):
            flash("Você não tem permissão para editar este modelo.", "danger")
            return redirect(url_for("listar_modelos"))

        if request.method == "POST":
            # Se for modelo de exemplo, apenas bloqueia o salvamento
            if modelo.get("account_id") == EXEMPLO_ACCOUNT_ID:
                flash("Modelos de exemplo não podem ser alterados.", "warning")
                return redirect(url_for("editar_modelo", text_id=text_id))

            # Atualiza modelo do usuário
            novo_nome = request.form["nome"]
            novo_conteudo = request.form["conteudo"]

            text_models_table.update_item(
                Key={"text_id": text_id},
                UpdateExpression="SET nome = :n, conteudo = :c",
                ExpressionAttributeValues={
                    ":n": novo_nome,
                    ":c": novo_conteudo,
                },
            )
            flash("Modelo atualizado com sucesso.", "success")
            return redirect(url_for("listar_modelos"))

        return render_template("criar_modelo.html", modelo=modelo)

    @app.route("/excluir-modelo/<text_id>", methods=["POST"])
    def excluir_modelo(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        # Verifica se é do usuário atual (evita excluir exemplo)
        account_id = session.get("account_id")
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")

        if (
            modelo
            and modelo.get("account_id") != "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
            and modelo["account_id"] == account_id
        ):
            text_models_table.delete_item(Key={"text_id": text_id})
            flash("Modelo excluído com sucesso.", "success")
        else:
            flash("Modelo não encontrado ou não autorizado.", "danger")

        return redirect(url_for("listar_modelos"))

    @app.route("/imprimir-modelo/<item_id>/<modelo_id>")
    def imprimir_modelo(item_id, modelo_id):
        # Buscar o item pelo ID
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            return "Item não encontrado", 404

        # Buscar o modelo selecionado na tabela `text_models_table`
        response = text_models_table.get_item(Key={"text_id": modelo_id})
        modelo = response.get("Item")

        if not modelo:
            return "Modelo não encontrado", 404

        # Obter o conteúdo do modelo armazenado no banco
        conteudo = modelo.get("conteudo")

        # Verifica se o conteúdo do modelo está presente
        if not conteudo:
            return "Conteúdo do modelo não encontrado", 404

        # Renderizar o conteúdo com os dados do item
        # O conteúdo do modelo pode ter variáveis, como {{ item.item_custom_id }}, que serão substituídas com os dados do item
        conteudo_renderizado = render_template_string(conteudo, item=item)

        # Retornar o conteúdo gerado para exibição/impressão
        return conteudo_renderizado

    @app.route("/qr_data/<item_id>")
    def qr_data(item_id):
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            return jsonify({"error": "Item não encontrado"}), 404

        return jsonify(
            {
                "item_custom_id": item.get("item_custom_id", ""),
                "description": item.get("description", ""),
                "item_obs": item.get("item_obs", ""),
                "image_url": item.get("image_url", ""),
            }
        )

    @app.route("/imprimir_qrcode/<item_id>")
    def imprimir_qrcode(item_id):
        incluir = request.args.getlist(
            "incluir"
        )  # Obtém os parâmetros de inclusão da URL

        # Buscar o item pelo ID
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            return "Item não encontrado", 404

        # Gerar o QR Code dinamicamente
        qr_data = (
            request.url_root.rstrip("/") + url_for("inventory") + f"?item_id={item_id}"
        )
        img = qrcode.make(qr_data)  # Gerando o QR Code
        buf = io.BytesIO()  # Usando buffer para armazenar a imagem
        img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        qr_data_url = f"data:image/png;base64,{qr_base64}"

        # Renderize o template do QR Code com os dados do item e as opções de impressão
        return render_template(
            "imprimir_qrcode.html", item=item, incluir=incluir, qr_data_url=qr_data_url
        )

    @app.route("/")
    def index():
        if session.get("logged_in"):
            return redirect(
                url_for("home")
            )  # Redireciona para /home (que já redireciona para all_transactions)
        return render_template("index.html")

    @app.route("/home")
    def home():
        if not session.get("logged_in"):
            return redirect(url_for("index"))
        return redirect(url_for("all_transactions"))

    @app.route("/obrigado")
    def obrigado():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Pega o account_id do usuário logado
        account_id = session.get("account_id")

        if account_id:
            # Atualiza o status do plano no banco
            refresh_plan_status()

        return render_template("obrigado.html")  # Uma página bonita de agradecimento

    from flask import Flask, request, session
    import stripe
    from boto3.dynamodb.conditions import Key

    @app.route("/webhook/stripe", methods=["POST", "GET"])
    def stripe_webhook():
        payload = request.data
        sig_header = request.headers.get("Stripe-Signature")

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        except Exception as e:
            print(f"🔴 Erro na validação do webhook: {str(e)}")
            return "Webhook invalid", 400

        # 🎯 1. Quando o checkout é concluído (compra nova)
        if event["type"] == "checkout.session.completed":
            print("🟢 Checkout concluído!")

            session_data = event["data"]["object"]
            account_id = session_data["metadata"].get("account_id")
            customer_id = session_data.get("customer")
            subscription_id = session_data.get("subscription")

            print(f"🟢 account_id: {account_id}")
            print(f"🟢 customer_id: {customer_id}")
            print(f"🟢 subscription_id: {subscription_id}")

            if account_id:
                accounts_table.update_item(
                    Key={"account_id": account_id},
                    UpdateExpression="""
                        SET plan_type = :p,
                            payment_status = :s,
                            stripe_customer_id = :c,
                            stripe_subscription_id = :sub
                    """,
                    ExpressionAttributeValues={
                        ":p": "premium",
                        ":s": "active",
                        ":c": customer_id,
                        ":sub": subscription_id,
                    },
                )
                print(f"🟢 Conta {account_id} atualizada para Premium e ativa.")
            else:
                print("🔴 Account ID não encontrado no metadata!")

        # 🎯 2. Quando o cliente agenda o cancelamento ou atualiza assinatura
        elif event["type"] == "customer.subscription.updated":
            print("🟡 Assinatura atualizada!")

            subscription_data = event["data"]["object"]
            customer_id = subscription_data.get("customer")
            cancel_at_period_end = subscription_data.get("cancel_at_period_end", False)
            cancel_at = subscription_data.get("cancel_at")
            canceled_at = subscription_data.get("canceled_at")

            # Captura a data do fim do ciclo

            # Se houver cancelamento agendado, essa será a data que precisamos
            subscription_end_date = cancel_at

            response = accounts_table.query(
                IndexName="stripe_customer_id-index",
                KeyConditionExpression=Key("stripe_customer_id").eq(customer_id),
            )

            items = response.get("Items", [])
            if items:
                account_id = items[0]["account_id"]
                print(f"🟡 Encontrado account_id: {account_id}")

                update_expression = """
                    SET payment_status = :s,
                        subscription_end_date = :end_date,
                        cancel_at = :cancel_at_value,
                        canceled_at = :canceled_at_value
                """

                expression_values = {
                    ":s": (
                        "scheduled_for_cancellation"
                        if cancel_at_period_end
                        else "active"
                    ),
                    ":end_date": subscription_end_date,
                    ":cancel_at_value": cancel_at if cancel_at else None,
                    ":canceled_at_value": canceled_at if canceled_at else None,
                }

                accounts_table.update_item(
                    Key={"account_id": account_id},
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values,
                )

                if cancel_at_period_end:
                    print("🟡 Conta marcada para cancelamento no fim do período.")
                else:
                    print("🟢 Conta permanece ativa.")
            else:
                print(
                    "🔴 Cliente não encontrado para customer_id (updated):", customer_id
                )

        # 🎯 3. Quando a assinatura termina definitivamente
        elif event["type"] == "customer.subscription.deleted":
            print("🟠 Assinatura encerrada!")

            subscription_data = event["data"]["object"]
            customer_id = subscription_data.get("customer")
            canceled_at = subscription_data.get("canceled_at")
            cancel_at = subscription_data.get("cancel_at")

            response = accounts_table.query(
                IndexName="stripe_customer_id-index",
                KeyConditionExpression=Key("stripe_customer_id").eq(customer_id),
            )

            items = response.get("Items", [])
            if items:
                account_id = items[0]["account_id"]
                print(f"🟠 Encontrado account_id: {account_id}")

                update_expression = """
                    SET plan_type = :p,
                        payment_status = :s,
                        cancel_at = :end_date,
                        canceled_at = :canceled_at_value
                    REMOVE stripe_subscription_id
                """

                expression_values = {
                    ":p": "free",
                    ":s": "canceled",
                    ":end_date": cancel_at,
                    ":canceled_at_value": canceled_at if canceled_at else None,
                }

                accounts_table.update_item(
                    Key={"account_id": account_id},
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values,
                )

                print("🟠 Conta atualizada para Free e Canceled.")
            else:
                print(
                    "🔴 Cliente não encontrado para customer_id (deleted):", customer_id
                )

        # 🎯 4. Quando a fatura é paga com sucesso (DATA DE PAGAMENTO!)
        elif event["type"] == "invoice.paid":
            print("🟢 Fatura paga confirmada!")

            invoice_data = event["data"]["object"]
            customer_id = invoice_data.get("customer")
            payment_date = invoice_data.get("status_transitions", {}).get("paid_at")

            if customer_id and payment_date:
                response = accounts_table.query(
                    IndexName="stripe_customer_id-index",
                    KeyConditionExpression=Key("stripe_customer_id").eq(customer_id),
                )

                items = response.get("Items", [])
                if items:
                    account_id = items[0]["account_id"]
                    print(f"🟢 Encontrado account_id para pagamento: {account_id}")

                    accounts_table.update_item(
                        Key={"account_id": account_id},
                        UpdateExpression="SET last_payment_date = :p",
                        ExpressionAttributeValues={":p": payment_date},
                    )
                    print("🟢 Conta atualizada com data do último pagamento.")
                else:
                    print(
                        "🔴 Cliente não encontrado para customer_id (invoice.paid):",
                        customer_id,
                    )

        return "OK", 200

    @app.route("/refresh_plan")
    def refresh_plan():
        if not session.get("logged_in"):
            return "Unauthorized", 401

        refresh_plan_status()
        return "Refreshed", 200

    def refresh_plan_status():
        if not session.get("account_id"):
            return

        account_id = session["account_id"]
        plan_status = get_account_plan(account_id)
        session["plan_type"] = plan_status

    def find_account_id_by_customer_id(customer_id):
        response = accounts_table.query(
            IndexName="stripe_customer_id-index",  # Nome correto do GSI
            KeyConditionExpression="stripe_customer_id = :c",
            ExpressionAttributeValues={":c": customer_id},
        )
        items = response.get("Items", [])
        if items:
            return items[0]["account_id"]
        return None

    # Stripe API key
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    @app.route("/create_checkout_session", methods=["POST"])
    def create_checkout_session():
        if not session.get("logged_in"):
            return "Unauthorized", 401

        account_id = session.get("account_id")
        user_email = session.get("email")

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                customer_email=user_email,  # Deixa Stripe criar o customer automaticamente
                line_items=[
                    {
                        "price": os.getenv("STRIPE_PRICE_ID"),  # ID do plano no Stripe
                        "quantity": 1,
                    }
                ],
                mode="subscription",
                success_url=url_for("obrigado", _external=True)
                + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=url_for("index", _external=True),
                metadata={"account_id": account_id},  # 👈 Grava seu account_id aqui
            )
            return {"checkout_url": checkout_session.url}

        except Exception as e:
            print(f"Erro ao criar Checkout: {str(e)}")
            return {"error": str(e)}, 400
