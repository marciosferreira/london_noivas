import datetime
import uuid
from boto3.dynamodb.conditions import Key

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)


def init_client_routes(app, clients_table, transactions_table, itens_table):
    @app.route("/autocomplete_clients")
    def autocomplete_clients():
        account_id = session.get("account_id")
        term = request.args.get("term", "").strip()

        print(term)

        if not term:
            return jsonify([])

        response = clients_table.query(
            IndexName="account_id-client_name-index",  # <-- nome do novo GSI
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("client_name").begins_with(term),
            Limit=5,
        )

        suggestions = [
            {
                "client_name": item.get("client_name"),
                "client_cpf": item.get("client_cpf"),
                "client_cnpj": item.get("client_cnpj"),
                "client_tel": item.get("client_tel"),
                "client_id": item.get("client_id"),
            }
            for item in response.get("Items", [])
        ]

        return jsonify(suggestions)

    @app.route("/clients")
    def listar_clientes():
        # Verifica se o usuário está logado
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Obtém o account_id da sessão
        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        # Consulta os clientes com esse account_id no GSI
        response = clients_table.query(
            IndexName="account_id-index",  # GSI com partition key = account_id
            KeyConditionExpression=Key("account_id").eq(account_id),
        )

        clientes = response.get("Items", [])

        # Renderiza a página passando a lista de clientes
        return render_template("clientes.html", clientes=clientes)

    @app.route("/editar_cliente/<client_id>", methods=["GET", "POST"])
    def editar_cliente(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        response = clients_table.get_item(Key={"client_id": client_id})
        cliente = response.get("Item")

        if not cliente:
            flash("Cliente não encontrado.", "danger")
            return redirect(url_for("listar_clientes"))

        if request.method == "POST":
            cliente["client_name"] = request.form.get("client_name", "").strip()
            cliente["client_tel"] = request.form.get("client_tel", "").strip()
            cliente["client_email"] = request.form.get("client_email", "").strip()
            cliente["client_address"] = request.form.get("client_address", "").strip()
            cliente["client_cpf"] = request.form.get("client_cpf", "").strip()
            cliente["client_cnpj"] = request.form.get("client_cnpj", "").strip()

            try:
                clients_table.put_item(Item=cliente)
                flash("Cliente atualizado com sucesso!", "success")
                return redirect(url_for("listar_clientes"))
            except Exception as e:
                print("Erro ao atualizar cliente:", e)
                flash("Erro ao atualizar cliente. Tente novamente.", "danger")
                return redirect(request.url)

        return render_template("editar_cliente.html", cliente=cliente)

    @app.route("/clientes/adicionar", methods=["GET", "POST"])
    def adicionar_cliente():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("listar_clientes"))

        if request.method == "POST":
            client_name = request.form.get("client_name", "").strip()
            client_tel = request.form.get("client_tel", "").strip()
            client_email = request.form.get("client_email", "").strip()
            client_address = request.form.get("client_address", "").strip()
            client_cpf = request.form.get("client_cpf", "").strip()
            client_cnpj = request.form.get("client_cnpj", "").strip()

            if not client_name:
                flash("O nome do cliente é obrigatório.", "error")
                return redirect(request.url)

            client_id = str(uuid.uuid4())
            account_id = session.get("account_id")

            new_client = {
                "client_id": client_id,
                "account_id": account_id,
                "client_name": client_name,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # Adiciona apenas se os campos tiverem valor
            if client_tel:
                new_client["client_tel"] = client_tel
            if client_email:
                new_client["client_email"] = client_email
            if client_address:
                new_client["client_address"] = client_address
            if client_cpf:
                new_client["client_cpf"] = client_cpf
            if client_cnpj:
                new_client["client_cnpj"] = client_cnpj

            try:
                clients_table.put_item(Item=new_client)
                flash("Cliente adicionado com sucesso!", "success")
                return redirect(next_page)
            except Exception as e:
                print("Erro ao adicionar cliente:", e)
                flash("Erro ao salvar cliente. Tente novamente.", "error")
                return redirect(request.url)

        return render_template("add_client.html")

    @app.route("/clientes/deletar/<client_id>", methods=["POST"])
    def deletar_cliente(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            # Verifica se cliente existe
            response = clients_table.get_item(Key={"client_id": client_id})
            cliente = response.get("Item")

            if not cliente:
                flash("Cliente não encontrado.", "danger")
                return redirect(url_for("listar_clientes"))

            # Remove do DynamoDB
            clients_table.delete_item(Key={"client_id": client_id})

            flash("Cliente deletado com sucesso!", "success")
        except Exception as e:
            print("Erro ao deletar cliente:", e)
            flash("Erro ao deletar cliente. Tente novamente.", "danger")

        return redirect(url_for("listar_clientes"))
        
    @app.route("/client_transactions/<client_id>/")
    def client_transactions(client_id):
        # Import here to avoid circular imports
        from item_routes import listar_itens_per_transaction
        
        # Lógica aqui para buscar transações do cliente
        return listar_itens_per_transaction(
            ["rented", "returned"],
            "client_transactions.html",
            "Transações do cliente",
            transactions_table,
            itens_table,
            client_id=client_id,
        )