import datetime
import uuid
from boto3.dynamodb.conditions import Key
from utils import get_user_timezone


from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)


def init_client_routes(
    app, clients_table, transactions_table, itens_table, users_table, text_models_table
):

    @app.route("/autocomplete_clients")
    def autocomplete_clients():
        account_id = session.get("account_id")
        term = request.args.get("term", "").strip()

        print(f"Buscando clientes com termo: {term}")

        if not term:
            return jsonify([])

        try:
            # Usar expressões de filtro consistentes
            response = clients_table.query(
                IndexName="account_id-client_name-index",  # <-- nome do GSI
                KeyConditionExpression="account_id = :account_id AND begins_with(client_name, :term)",
                ExpressionAttributeValues={":account_id": account_id, ":term": term},
                Limit=5,
            )

            suggestions = [
                {
                    "client_name": item.get("client_name", ""),
                    "client_cpf": item.get("client_cpf", ""),
                    "client_cnpj": item.get("client_cnpj", ""),
                    "client_tel": item.get("client_tel", ""),
                    "client_id": item.get("client_id", ""),
                    "client_email": item.get("client_email", ""),  # 👈 adiciona aqui
                    "client_address": item.get("client_address", ""),  # 👈 e aqui
                    "client_obs": item.get("client_obs", ""),  # 👈 e aqui
                }
                for item in response.get("Items", [])
            ]

            print(f"Encontrados {len(suggestions)} sugestões para '{term}'")
            return jsonify(suggestions)

        except Exception as e:
            print(f"Erro na busca de autocomplete: {str(e)}")
            return jsonify([])  # Retorna lista vazia em caso de erro

    @app.route("/clients")
    def listar_clientes():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        # Paginação
        page = int(request.args.get("page", 1))
        per_page = 5

        # Filtros do formulário
        client_name = request.args.get("client_name", "").strip()
        client_id = request.args.get("client_id", "").strip()
        client_tel = request.args.get("client_tel", "").strip()
        client_email = request.args.get("client_email", "").strip()
        client_address = request.args.get("client_address", "").strip()
        client_cpf = request.args.get("client_cpf", "").strip()
        client_cnpj = request.args.get("client_cnpj", "").strip()
        client_obs = request.args.get("client_obs", "").strip()

        # Normalizar dados numéricos
        client_tel = "".join(filter(str.isdigit, client_tel)) if client_tel else ""
        client_cpf = "".join(filter(str.isdigit, client_cpf)) if client_cpf else ""
        client_cnpj = "".join(filter(str.isdigit, client_cnpj)) if client_cnpj else ""

        # 🔹 Buscar todos os clientes do usuário em ordem alfabética
        response = clients_table.query(
            IndexName="account_id-created_at-index",
            KeyConditionExpression="account_id = :account_id",
            ExpressionAttributeValues={":account_id": account_id},
            ScanIndexForward=False,  # False = mais recentes primeiro
        )
        clientes = response.get("Items", [])

        # 🔸 Aplicar filtros localmente
        def matches(cliente):
            return (
                (
                    not client_name
                    or client_name.lower() in cliente.get("client_name", "").lower()
                )
                and (not client_tel or client_tel in cliente.get("client_tel", ""))
                and (
                    not client_email
                    or client_email.lower() in cliente.get("client_email", "").lower()
                )
                and (
                    not client_address
                    or client_address.lower()
                    in cliente.get("client_address", "").lower()
                )
                and (
                    not client_id
                    or client_id.lower() in cliente.get("client_id", "").lower()
                )
                and (not client_cpf or client_cpf in cliente.get("client_cpf", ""))
                and (not client_cnpj or client_cnpj in cliente.get("client_cnpj", ""))
                and (
                    not client_obs
                    or client_obs.lower() in cliente.get("client_obs", "").lower()
                )
            )

        clientes_filtrados = [c for c in clientes if matches(c)]

        # Paginação
        total_items = len(clientes_filtrados)
        total_pages = max((total_items + per_page - 1) // per_page, 1)
        page = min(max(page, 1), total_pages)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_clientes = clientes_filtrados[start_idx:end_idx]

        has_filters = any(
            [
                client_name,
                client_tel,
                client_email,
                client_address,
                client_cpf,
                client_cnpj,
                client_obs,
                client_id,
            ]
        )

        return render_template(
            "clientes.html",
            itens=paginated_clientes,
            page=page,
            total_pages=total_pages,
            has_filters=has_filters,
        )

    @app.route("/editar_cliente/<client_id>", methods=["GET", "POST"])
    def editar_cliente(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        response = clients_table.get_item(Key={"client_id": client_id})
        cliente = response.get("Item")

        if not cliente:
            flash("Cliente não encontrado.", "danger")
            # return redirect(url_for("listar_clientes"))
            return redirect(next_page)

        if request.method == "POST":
            # Obter valores do formulário
            client_name = request.form.get("client_name", "").strip()
            client_email = request.form.get("client_email", "").strip()
            client_address = request.form.get("client_address", "").strip()

            # Usar os campos ocultos que já contêm apenas dígitos (sem formatação)
            client_tel_digits = request.form.get("client_tel_digits", "").strip()
            client_cpf_digits = request.form.get("client_cpf_digits", "").strip()
            client_cnpj_digits = request.form.get("client_cnpj_digits", "").strip()
            client_obs = request.form.get("client_obs", "").strip()

            # Atualizar dados do cliente
            cliente["client_name"] = client_name
            cliente["client_obs"] = client_obs

            # Só atualizar os campos se tiverem valor
            if client_email:
                cliente["client_email"] = client_email
            if client_address:
                cliente["client_address"] = client_address

            # Usar os valores sem formatação (apenas dígitos)
            # Usar os valores sem formatação (apenas dígitos)
            cliente["client_cpf"] = client_cpf_digits if client_cpf_digits else None
            cliente["client_cnpj"] = client_cnpj_digits if client_cnpj_digits else None

            # valida se telefone tem 11 digitos
            if client_tel_digits:
                if bool(re.fullmatch(r"\d{11}", client_tel_digits)):
                    cliente["client_tel"] = client_tel_digits
                else:
                    flash(
                        "Número de telefone inválido! Deve conter 11 dígitos.", "error"
                    )
                    return redirect(request.referrer)
            else:
                cliente["client_tel"] = (
                    ""  # ou delete se preferir não manter campo vazio
                )

            user_id = session.get("user_id") if "user_id" in session else None
            user_utc = get_user_timezone(users_table, user_id)
            # Marcar como editado
            cliente["updated_at"] = datetime.datetime.now(user_utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            try:
                clients_table.put_item(Item=cliente)
                # caso o user decida alterar todos os clientes na db
                # Campos do cliente que precisam ser atualizados nas transações
                if request.form.get("update_all_transactions"):
                    client_fields = [
                        "client_address",
                        "client_cnpj",
                        "client_cpf",
                        "client_email",
                        "client_id",
                        "client_name",
                        "client_obs",
                        "client_tel",
                    ]

                    # Query no GSI client_id-index para buscar transações relacionadas
                    response = transactions_table.query(
                        IndexName="client_id-index",
                        KeyConditionExpression="client_id = :client_id_val",
                        ExpressionAttributeValues={":client_id_val": client_id},
                    )

                    transacoes_relacionadas = response.get("Items", [])

                    for transacao in transacoes_relacionadas:
                        update_expr = [
                            f"{key} = :{key}" for key in client_fields if key in cliente
                        ]
                        expr_values = {
                            f":{key}": cliente[key]
                            for key in client_fields
                            if key in cliente
                        }

                        transactions_table.update_item(
                            Key={"transaction_id": transacao["transaction_id"]},
                            UpdateExpression="SET " + ", ".join(update_expr),
                            ExpressionAttributeValues=expr_values,
                        )

                flash("Cliente atualizado com sucesso!", "success")
                # return redirect(url_for("listar_clientes"))
                return redirect(next_page)
            except Exception as e:
                print("Erro ao atualizar cliente:", e)
                flash("Erro ao atualizar cliente. Tente novamente.", "danger")
                return redirect(next_page)
                # return redirect(request.url)

        return render_template("editar_cliente.html", cliente=cliente)

    @app.route("/clientes/adicionar", methods=["GET", "POST"])
    def adicionar_cliente():
        return add_client_common(
            request,
            clients_table,
            users_table,
            session,
            flash,
            redirect,
            url_for,
            "add_client.html",
        )

    @app.route("/add_client", methods=["GET", "POST"])
    def add_client():
        return add_client_common(
            request, clients_table, session, flash, redirect, url_for, "add_client.html"
        )

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
            ["rented", "returned", "reserved"],
            "client_transactions.html",
            "Transações do cliente",
            transactions_table,
            itens_table,
            text_models_table,
            users_table,
            client_id=client_id,
        )

    @app.template_filter("format_brasil_data")
    def format_brasil_data(value):
        try:
            dt = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%d/%m/%Y %H:%M:%S")
        except:
            return value

    @app.template_filter("format_telefone")
    def format_telefone(value):
        if not value:
            return
        digits = "".join(filter(str.isdigit, value))
        if len(digits) == 11:
            return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
        elif len(digits) == 10:
            return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
        else:
            return value


def add_client_common(
    request,
    clients_table,
    users_table,
    session,
    flash,
    redirect,
    url_for,
    template,
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("listar_clientes"))

    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        client_email = request.form.get("client_email", "").strip()
        client_address = request.form.get("client_address", "").strip()

        # Usar os campos ocultos que já contêm apenas dígitos (sem formatação)
        client_tel_digits = request.form.get("client_tel_digits", "").strip()
        client_cpf_digits = request.form.get("client_cpf_digits", "").strip()
        client_cnpj_digits = request.form.get("client_cnpj_digits", "").strip()

        client_obs = request.form.get("client_obs", "").strip()

        if not client_name:
            flash("O nome do cliente é obrigatório.", "error")
            return redirect(request.url)

        client_id = str(uuid.uuid4())
        account_id = session.get("account_id")

        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)

        # Verificação de duplicatas por nome, CPF ou CNPJ
        existing_clients = clients_table.scan().get("Items", [])

        for client in existing_clients:
            if client.get("account_id") != account_id:
                continue  # Ignorar clientes de outras contas

            # Verifica duplicatas por nome, CPF ou CNPJ
            if client.get("client_name") == client_name:
                flash("Já existe um cliente com esse nome.", "error")
                return redirect(request.url)

            if client_cpf_digits and client.get("client_cpf") == client_cpf_digits:
                flash("Já existe um cliente com esse CPF.", "error")
                return redirect(request.url)

            if client_cnpj_digits and client.get("client_cnpj") == client_cnpj_digits:
                flash("Já existe um cliente com esse CNPJ.", "error")
                return redirect(request.url)

        new_client = {
            "client_id": client_id,
            "account_id": account_id,
            "client_name": client_name,
            "created_at": datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Adiciona apenas se os campos tiverem valor
        if client_tel_digits:
            new_client["client_tel"] = client_tel_digits
        if client_email:
            new_client["client_email"] = client_email
        if client_address:
            new_client["client_address"] = client_address
        if client_cpf_digits:
            new_client["client_cpf"] = client_cpf_digits
        if client_cnpj_digits:
            new_client["client_cnpj"] = client_cnpj_digits
        if client_obs:
            new_client["client_obs"] = client_obs

        try:
            clients_table.put_item(Item=new_client)
            flash("Cliente adicionado com sucesso!", "success")
            return redirect(next_page)
        except Exception as e:
            print("Erro ao adicionar cliente:", e)
            flash("Erro ao salvar cliente. Tente novamente.", "error")
            return redirect(request.url)

    return render_template(template)


import re
