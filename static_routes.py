from flask import (
    render_template,
    request,
    session,
    redirect,
    url_for,
    flash,
    send_from_directory,
)


def init_static_routes(app, ses_client, clients_table, transactions_table, itens_table):
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
        from boto3.dynamodb.conditions import Key

        stats = {}

        if session.get("logged_in"):
            print("session ok?")
            account_id = session.get("account_id")
            username = session.get("username", None)

            response = itens_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
            )

            # Agora você filtra só os que tem status 'available' ou 'archive'
            items = response["Items"]

            filtered_items = [
                item for item in items if item.get("status") in ["available", "archive"]
            ]

            stats["total_items"] = len(filtered_items)

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
