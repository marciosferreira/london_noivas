from flask import (
    render_template,
    request,
    session,
    redirect,
    url_for,
    flash,
    send_from_directory,
)
import uuid
from decimal import Decimal
import datetime
from boto3.dynamodb.conditions import Key
from flask import render_template_string
from utils import get_user_timezone
from flask import jsonify
import qrcode
import io
import base64
from flask import request


def init_static_routes(
    app,
    ses_client,
    clients_table,
    transactions_table,
    itens_table,
    text_models_table,
    users_table,
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

    @app.route("/")
    def index():

        stats = {}

        if session.get("logged_in"):
            print("session ok?")
            account_id = session.get("account_id")
            username = session.get("username", None)

            response = itens_table.query(
                IndexName="account_id-status-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
            )

            # Agora você filtra só os que tem status 'available' ou 'archive'
            items = response["Items"]

            filtered_items_available = [
                item for item in items if item.get("status") in ["available"]
            ]
            stats["total_items_available"] = len(filtered_items_available)

            filtered_items_archived = [
                item for item in items if item.get("status") in ["archive"]
            ]

            stats["total_items_archived"] = len(filtered_items_archived)

            # Contar transações "rented"
            rented_txn = transactions_table.query(
                IndexName="account_id-transaction_status-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("transaction_status").eq("rented"),
            )
            stats["total_rented"] = rented_txn["Count"]

            # Contar transações "returned"
            returned_txn = transactions_table.query(
                IndexName="account_id-transaction_status-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("transaction_status").eq("returned"),
            )
            stats["total_returned"] = returned_txn["Count"]

            # Contar transações "reserved"
            reserved_txn = transactions_table.query(
                IndexName="account_id-transaction_status-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("transaction_status").eq("reserved"),
            )
            stats["total_reserved"] = reserved_txn["Count"]

            # Contar transações "returned"
            clients_txn = clients_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
            )["Count"]

            stats["total_clients"] = clients_txn

        else:
            username = None

        return render_template("index.html", stats=stats, username=username)

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

        # Substitui as variáveis do template com os dados da transação
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_name }}", transacao.get("client_name", "Nome não disponível")
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ rental_date }}", transacao.get("rental_date", "Data não disponível")
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ return_date }}", transacao.get("return_date", "Data não disponível")
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

    @app.route("/qr-data/<item_id>")
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

    @app.route("/imprimir-qrcode/<item_id>")
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
