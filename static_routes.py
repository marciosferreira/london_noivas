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

    @app.route("/")
    def index():
        from boto3.dynamodb.conditions import Key

        stats = {}

        if session.get("logged_in"):
            account_id = session.get("account_id")
            username = session.get("username", None)

            # Contar clientes (tabela correta)
            stats["total_clients"] = clients_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
            )["Count"]

            # Contar itens
            stats["total_items"] = itens_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
            )["Count"]

            # Contar transações "rented"
            rented_txn = transactions_table.query(
                IndexName="account_id-status-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("status").eq("rented"),
            )
            stats["total_rented"] = rented_txn["Count"]

            # Contar transações "returned"
            returned_txn = transactions_table.query(
                IndexName="account_id-status-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("status").eq("returned"),
            )
            stats["total_returned"] = returned_txn["Count"]
        else:
            username = None

        return render_template("index.html", stats=stats, username=username)

    @app.route("/ads.txt")
    def ads_txt():
        return send_from_directory("static", "ads.txt")
