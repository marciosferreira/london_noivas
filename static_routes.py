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

        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")
        if not modelo:
            return "Modelo não encontrado."

        response = transactions_table.get_item(Key={"transaction_id": transacao_id})
        transacao = response.get("Item")

        if not transacao:
            return "Transação não encontrada."

        print("[DEBUG] Transação:", transacao)
        print("[DEBUG] Keys disponíveis:", list(transacao.keys()))

        for k, v in transacao.items():
            if isinstance(v, Decimal):
                transacao[k] = float(v)

        # Corrigir formatação das datas
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
        conteudo_renderizado = render_template_string(
            modelo["conteudo"]
            + """
            <br>
            <script>
              function printConteudo() {
                const conteudo = document.getElementById("print-area").innerHTML;
                const janela = window.open('', '', 'width=800,height=600');
                janela.document.write(`
                  <html>
                    <head>
                      <title>Imprimir</title>
                      <style>
                        body { font-family: Arial, sans-serif; padding: 20px; }
                      </style>
                    </head>
                    <body>
                      ${conteudo}
                    </body>
                  </html>
                `);
                janela.document.close();
                janela.focus();
                janela.print();
                janela.close();
              }
            </script>
            """,
            **transacao,
            modelo=modelo,
        )

        return render_template(
            "visualizar_modelo_conteudo.html",
            modelo=modelo,
            conteudo_renderizado=conteudo_renderizado,
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
