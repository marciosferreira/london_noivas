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
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")  # ← este é o certo


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
    payment_transactions_table,
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
            destinatario = "contato@locashop.com.br"
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
            destinatario = "contato@locashop.com.br"
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
            text_id = str(uuid.uuid4().hex[:12])
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

    from flask import Flask, request, session
    import stripe
    from boto3.dynamodb.conditions import Key

    from flask import request
    import stripe
    import os
    import time
    from boto3.dynamodb.conditions import Key

    # Configurações Stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    from flask import request, abort

    @app.route("/webhook/stripe", methods=["POST"])
    def stripe_webhook():
        payload = request.get_data(as_text=False)
        sig_header = request.headers.get("Stripe-Signature")

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        except ValueError as e:
            print(f"🔴 Erro na validação do webhook: {str(e)}")
            return abort(400)
        except stripe.error.SignatureVerificationError as e:
            print(f"🔴 Assinatura inválida do webhook: {str(e)}")
            return abort(400)

        event_type = event["type"]
        print(f"📥 Evento recebido: {event_type}")

        if event_type == "checkout.session.completed":
            print("🟢 Checkout session completed!")
            transaction = event["data"]["object"]

            try:
                stripe_subscription_id = transaction.get("subscription")
                if not stripe_subscription_id:
                    raise ValueError("subscription_id não encontrado na sessão.")

                item = {
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_session_id": transaction.get("id"),
                    "account_id": transaction.get("metadata", {}).get("account_id"),
                    "customer_id": transaction.get("customer"),
                    "customer_email": transaction.get("customer_email"),
                    "customer_name": transaction.get("customer_details", {}).get(
                        "name"
                    ),
                    "amount_total": transaction.get("amount_total"),
                    "currency": transaction.get("currency"),
                    "payment_status": transaction.get(
                        "payment_status"
                    ),  # "paid", "unpaid", etc.
                    "payment_method_types": transaction.get("payment_method_types", []),
                    "created_at": transaction.get("created"),
                    "expires_at": transaction.get("expires_at"),
                    "invoice_id": transaction.get("invoice"),
                    "status": transaction.get("status"),
                    "livemode": transaction.get("livemode"),
                }

                item = {k: v for k, v in item.items() if v is not None}
                payment_transactions_table.put_item(Item=item)

                print(
                    f"🟢 Transação {stripe_subscription_id} registrada após checkout."
                )

            except Exception as e:
                print(f"🔴 Erro ao atualizar transação: {str(e)}")

        elif event_type == "customer.subscription.created":
            print("🟢 Assinatura criada!")

            subscription_data = event["data"]["object"]

            try:
                stripe_subscription_id = subscription_data.get("id")
                status = subscription_data.get("status")
                current_period_end = subscription_data.get("current_period_end")

                if not stripe_subscription_id:
                    raise ValueError("subscription_id não encontrado no evento.")

                response = payment_transactions_table.get_item(
                    Key={"stripe_subscription_id": stripe_subscription_id}
                )
                item_existente = response.get("Item")

                if not item_existente:
                    print(
                        f"⚠️ Nenhuma transação prévia encontrada para {stripe_subscription_id} — criando nova entrada."
                    )
                    item = {
                        "stripe_subscription_id": stripe_subscription_id,
                        "subscription_status": status,
                        "created_at": subscription_data.get("created"),
                        "customer_id": subscription_data.get("customer"),
                        "current_period_end": current_period_end,
                        "updated_at": int(time.time()),
                    }
                    payment_transactions_table.put_item(Item=item)
                else:
                    payment_transactions_table.update_item(
                        Key={"stripe_subscription_id": stripe_subscription_id},
                        UpdateExpression="""
                            SET subscription_status = :status,
                                current_period_end = :current_period_end,
                                updated_at = :updated_at
                        """,
                        ExpressionAttributeValues={
                            ":status": status,
                            ":current_period_end": current_period_end,
                            ":updated_at": int(time.time()),
                        },
                    )
                    print(f"🟢 subscription_status atualizado para {status}.")

            except Exception as e:
                print(f"🔴 Erro ao processar subscription.created: {str(e)}")

        #
        elif event_type == "customer.subscription.updated":
            print("🟡 Assinatura atualizada!")
            subscription_data = event["data"]["object"]

            try:
                stripe_subscription_id = subscription_data.get("id")
                if not stripe_subscription_id:
                    raise ValueError("subscription_id não encontrado no evento.")

                response = payment_transactions_table.get_item(
                    Key={"stripe_subscription_id": stripe_subscription_id}
                )
                item_existente = response.get("Item")

                if item_existente:
                    status = subscription_data.get("status", "unknown")
                    current_period_end = subscription_data.get("current_period_end")

                    expression_values = {
                        ":subscription_status": status,
                        ":cancel_at": subscription_data.get("cancel_at"),
                        ":cancel_at_period_end": subscription_data.get("cancel_at_period_end", False),
                        ":current_period_end": current_period_end,
                        ":updated_at": int(time.time()),
                    }

                    update_expr = """
                        SET subscription_status = :subscription_status,
                            cancel_at = :cancel_at,
                            cancel_at_period_end = :cancel_at_period_end,
                            current_period_end = :current_period_end,
                            updated_at = :updated_at
                    """

                    if status in ["past_due", "unpaid"]:
                        update_expr += ", payment_status = :payment_status"
                        expression_values[":payment_status"] = "failed"

                    payment_transactions_table.update_item(
                        Key={"stripe_subscription_id": stripe_subscription_id},
                        UpdateExpression=update_expr,
                        ExpressionAttributeValues=expression_values,
                    )

                    print(f"🟡 Transação {stripe_subscription_id} atualizada.")
                else:
                    print(f"🔴 Nenhuma transação encontrada para {stripe_subscription_id}")

            except Exception as e:
                print(f"🔴 Erro ao atualizar transação: {str(e)}")


        elif event_type == "invoice.payment_failed":
            print("🔴 Falha ao cobrar fatura!")

            invoice_data = event["data"]["object"]
            customer_id = invoice_data.get("customer")

            try:
                response = payment_transactions_table.query(
                    IndexName="customer_id-index",
                    KeyConditionExpression=Key("customer_id").eq(customer_id),
                )
                items = response.get("Items", [])

                if items:
                    stripe_subscription_id = items[0].get("stripe_subscription_id")
                    payment_transactions_table.update_item(
                        Key={"stripe_subscription_id": stripe_subscription_id},
                        UpdateExpression="""
                            SET payment_status = :status,
                                updated_at = :updated_at
                        """,
                        ExpressionAttributeValues={
                            ":status": "failed",
                            ":updated_at": int(time.time()),
                        },
                    )
                    print(f"🔴 Transação {stripe_subscription_id} marcada como falha.")
                else:
                    print(
                        f"🔴 Nenhuma transação encontrada para customer_id {customer_id}"
                    )

            except Exception as e:
                print(f"🔴 Erro ao processar invoice.payment_failed: {str(e)}")

        elif event_type == "invoice.paid":
            print("🟢 Invoice paga!")

            invoice_data = event["data"]["object"]
            first_line = invoice_data.get("lines", {}).get("data", [])[0]
            current_period_end = first_line.get("period", {}).get("end")
            customer_id = invoice_data.get("customer")
            amount_paid = invoice_data.get("amount_paid")
            currency = invoice_data.get("currency")
            paid_at = invoice_data.get("status_transitions", {}).get("paid_at")

            try:
                response = payment_transactions_table.query(
                    IndexName="customer_id-index",
                    KeyConditionExpression=Key("customer_id").eq(customer_id),
                )
                items = response.get("Items", [])

                if items:
                    item = items[0]
                    stripe_subscription_id = item.get("stripe_subscription_id")

                    payment_transactions_table.update_item(
                        Key={"stripe_subscription_id": stripe_subscription_id},
                        UpdateExpression="""
                            SET payment_status = :status,
                                amount_total = :amount_paid,
                                currency = :currency,
                                updated_at = :updated_at,
                                last_payment_date = :paid_at,
                                current_period_end = :current_period_end
                        """,
                        ExpressionAttributeValues={
                            ":status": "paid",
                            ":amount_paid": amount_paid,
                            ":currency": currency,
                            ":updated_at": int(time.time()),
                            ":paid_at": paid_at,
                            ":current_period_end": current_period_end,
                        },
                    )
                    print(
                        f"🟢 Transação {stripe_subscription_id} atualizada para paid."
                    )
                else:
                    print(
                        f"🔴 Nenhuma transação encontrada para customer_id {customer_id}"
                    )

            except Exception as e:
                print(f"🔴 Erro ao processar invoice.paid: {str(e)}")

        elif event_type == "customer.subscription.deleted":
            print("🟠 Assinatura cancelada!")

            subscription_data = event["data"]["object"]
            try:
                stripe_subscription_id = subscription_data.get("id")
                if not stripe_subscription_id:
                    raise ValueError("subscription_id não encontrado no evento.")

                response = payment_transactions_table.get_item(
                    Key={"stripe_subscription_id": stripe_subscription_id}
                )
                item_existente = response.get("Item")

                if item_existente:
                    payment_transactions_table.update_item(
                        Key={"stripe_subscription_id": stripe_subscription_id},
                        UpdateExpression="""
                            SET payment_status = :payment_status,
                                canceled_at = :canceled_at,
                                cancel_at = :cancel_at,
                                updated_at = :updated_at
                        """,
                        ExpressionAttributeValues={
                            ":payment_status": "canceled",
                            ":canceled_at": subscription_data.get("canceled_at"),
                            ":cancel_at": subscription_data.get("cancel_at"),
                            ":updated_at": int(time.time()),
                        },
                    )
                    print(
                        f"🟠 Transação {stripe_subscription_id} atualizada para canceled."
                    )
                else:
                    print(
                        f"🔴 Nenhuma transação encontrada para {stripe_subscription_id}"
                    )

            except Exception as e:
                print(f"🔴 Erro ao processar subscription.deleted: {str(e)}")

        return "OK", 200

    @app.route("/create_checkout_session", methods=["POST"])
    def create_checkout_session():
        if not session.get("logged_in"):
            return "Unauthorized", 401

        user_id = session.get("user_id")  # Certifique que isso está na sessão

        user_utc = get_user_timezone(users_table, user_id)

        account_id = session.get("account_id")
        user_id = session.get("user_id")  # Certifique que isso está na sessão
        user_email = session.get("email")

        try:
            # Cria a sessão de pagamento no Stripe
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                customer_email=user_email,
                success_url=url_for(
                    "adjustments", _external=True
                ),  # 🔥 Redireciona para ajustes
                cancel_url=url_for("index", _external=True),  # ou alguma página neutra
                line_items=[
                    {
                        "price": os.getenv("STRIPE_PRICE_ID"),
                        "quantity": 1,
                    }
                ],
                mode="subscription",
                metadata={"account_id": account_id},
            )

            return {"checkout_url": checkout_session.url}

        except Exception as e:
            print(f"Erro ao criar Checkout: {str(e)}")
            return {"error": str(e)}, 400

    @app.route("/billing")
    def billing():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        user_id = session.get("user_id")
        response = users_table.get_item(Key={"user_id": user_id})
        if "Item" not in response:
            flash("Usuário não encontrado.", "danger")
            return redirect(url_for("adjustments"))

        user = response["Item"]
        stripe_customer_id = user.get("stripe_customer_id")

        if not stripe_customer_id:
            flash("Cliente Stripe não encontrado para este usuário.", "danger")
            return redirect(url_for("adjustments"))

        try:
            # Cria sessão do portal de cobrança
            session_portal = stripe.billing_portal.Session.create(
                customer=stripe_customer_id,
                return_url=url_for(
                    "adjustments", _external=True
                ),  # volta para sua área de ajustes
            )
            return redirect(session_portal.url)

        except Exception as e:
            import traceback

            traceback.print_exc()
            flash("Erro ao abrir o portal de pagamento. Tente novamente.", "danger")
            return redirect(url_for("adjustments"))

    @app.route("/autocomplete_items")
    def autocomplete_items():
        from boto3.dynamodb.conditions import Key, Attr

        account_id = session.get("account_id")
        term = request.args.get("term", "").strip().lower()

        if not term:
            return jsonify([])

        try:
            response = itens_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
                FilterExpression=Attr("status").is_in(["available", "archive"]),
                Limit=1000
            )
            all_items = response.get("Items", [])
        except Exception as e:
            print("Erro ao buscar itens:", e)
            return jsonify([])

        suggestions = []
        for item in all_items:
            custom_id = (item.get("item_custom_id") or "").lower()
            description = (item.get("item_description") or "").lower()
            if term in custom_id or term in description:
                suggestions.append(item)

        return jsonify([
            {
                "item_id": item["item_id"],
                "item_custom_id": item.get("item_custom_id", ""),
                "item_description": item.get("item_description", ""),
                "item_value": item.get("item_value", ""),
                "item_obs": item.get("item_obs", ""),
                "item_image_url": item.get("item_image_url", ""),
            }
            for item in suggestions[:10]
        ])


    @app.route("/item_reserved_ranges/<item_id>")
    def item_reserved_ranges(item_id):
        try:
            response = transactions_table.query(
                IndexName="item_id-index",
                KeyConditionExpression=Key("item_id").eq(item_id),
            )
            items = response.get("Items", [])
            reserved_ranges = [
                [tx["rental_date"], tx["return_date"]]
                for tx in items
                if tx.get("transaction_status") in ["reserved", "rented"]
                and tx.get("rental_date")
                and tx.get("return_date")
            ]

            return jsonify(reserved_ranges)
        except Exception as e:
            print("Erro ao buscar reservas:", e)
            return jsonify([]), 500
