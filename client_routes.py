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
        # Verifica se o usuário está logado
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Obtém o account_id da sessão
        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        # Parâmetros de paginação
        page = int(request.args.get("page", 1))
        per_page = 10  # Clientes por página

        # Capturar parâmetros de filtro
        client_name = request.args.get("client_name", "").strip()
        client_tel = request.args.get("client_tel", "").strip()
        client_email = request.args.get("client_email", "").strip()
        client_address = request.args.get("client_address", "").strip()
        client_cpf = request.args.get("client_cpf", "").strip()
        client_cnpj = request.args.get("client_cnpj", "").strip()

        # Remover caracteres não numéricos para telefone, CPF e CNPJ
        if client_tel:
            client_tel = "".join(filter(str.isdigit, client_tel))
        if client_cpf:
            client_cpf = "".join(filter(str.isdigit, client_cpf))
        if client_cnpj:
            client_cnpj = "".join(filter(str.isdigit, client_cnpj))

        # Verificar se há filtros ativos
        has_filters = any(
            [
                client_name,
                client_tel,
                client_email,
                client_address,
                client_cpf,
                client_cnpj,
            ]
        )

        # Inicializar estruturas para os filtros
        expression_names = {}
        expression_values = {":account_id": account_id}
        filter_expression = ""

        # Construir expressões de filtro para cada campo, se fornecido
        if client_name:
            expression_names["#client_name"] = "client_name"
            expression_values[":client_name"] = client_name.lower()
            filter_expression += " AND contains(lower(#client_name), :client_name)"

        if client_tel:
            expression_names["#client_tel"] = "client_tel"
            expression_values[":client_tel"] = client_tel
            # Primeiro verifica se o atributo existe, depois filtra
            filter_expression += " AND attribute_exists(#client_tel) AND contains(#client_tel, :client_tel)"

        if client_email:
            expression_names["#client_email"] = "client_email"
            expression_values[":client_email"] = client_email.lower()
            filter_expression += " AND attribute_exists(#client_email) AND contains(lower(#client_email), :client_email)"

        if client_address:
            expression_names["#client_address"] = "client_address"
            expression_values[":client_address"] = client_address.lower()
            filter_expression += " AND attribute_exists(#client_address) AND contains(lower(#client_address), :client_address)"

        if client_cpf:
            expression_names["#client_cpf"] = "client_cpf"
            expression_values[":client_cpf"] = client_cpf
            filter_expression += " AND attribute_exists(#client_cpf) AND contains(#client_cpf, :client_cpf)"

        if client_cnpj:
            expression_names["#client_cnpj"] = "client_cnpj"
            expression_values[":client_cnpj"] = client_cnpj
            filter_expression += " AND attribute_exists(#client_cnpj) AND contains(#client_cnpj, :client_cnpj)"

        # Configurar a consulta principal
        query_params = {
            "IndexName": "account_id-index",
            "KeyConditionExpression": "account_id = :account_id",
            "ExpressionAttributeValues": {":account_id": account_id},
        }

        # Adicionar expressão de filtro, se houver
        if filter_expression:
            # Remover o " AND " inicial
            filter_expression = filter_expression[5:]
            query_params["FilterExpression"] = filter_expression

            # Atualizar os expression values com os filtros adicionais
            for key, value in expression_values.items():
                if key != ":account_id":  # Evitar duplicar o account_id
                    query_params["ExpressionAttributeValues"][key] = value

            # Adicionar expression names apenas se houver filtros
            if len(expression_names) > 0:
                query_params["ExpressionAttributeNames"] = expression_names

        # Executar a consulta
        response = clients_table.query(**query_params)
        clientes = response.get("Items", [])

        # Log para depuração
        print(f"Encontrados {len(clientes)} clientes com os filtros aplicados")

        # Paginação dos resultados
        total_items = len(clientes)
        total_pages = (total_items + per_page - 1) // per_page if total_items > 0 else 1

        # Ajustar a página atual se estiver fora dos limites
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages

        # Calcular índices de início e fim para a paginação
        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, total_items)

        # Obter apenas os clientes da página atual
        paginated_clientes = clientes[start_idx:end_idx]

        # Renderiza a página passando a lista de clientes e informações de paginação
        return render_template(
            "clientes.html",
            clientes=paginated_clientes,
            page=page,
            total_pages=total_pages,
            has_filters=has_filters,
        )

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
            # Obter valores do formulário
            client_name = request.form.get("client_name", "").strip()
            client_email = request.form.get("client_email", "").strip()
            client_address = request.form.get("client_address", "").strip()

            # Usar os campos ocultos que já contêm apenas dígitos (sem formatação)
            client_tel_digits = request.form.get("client_tel_digits", "").strip()
            client_cpf_digits = request.form.get("client_cpf_digits", "").strip()
            client_cnpj_digits = request.form.get("client_cnpj_digits", "").strip()

            # Atualizar dados do cliente
            cliente["client_name"] = client_name

            # Só atualizar os campos se tiverem valor
            if client_email:
                cliente["client_email"] = client_email
            if client_address:
                cliente["client_address"] = client_address

            # Usar os valores sem formatação (apenas dígitos)
            if client_tel_digits:
                cliente["client_tel"] = client_tel_digits
            if client_cpf_digits:
                cliente["client_cpf"] = client_cpf_digits
            if client_cnpj_digits:
                cliente["client_cnpj"] = client_cnpj_digits

            # Marcar como editado
            cliente["updated_at"] = datetime.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

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
        return add_client_common(
            request, clients_table, session, flash, redirect, url_for, "add_client.html"
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

        print("kkkkkkkkk")

        # Lógica aqui para buscar transações do cliente
        return listar_itens_per_transaction(
            ["rented", "returned"],
            "client_transactions.html",
            "Transações do cliente",
            transactions_table,
            itens_table,
            client_id=client_id,
        )


def add_client_common(
    request, clients_table, session, flash, redirect, url_for, template
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

        try:
            clients_table.put_item(Item=new_client)
            flash("Cliente adicionado com sucesso!", "success")
            return redirect(next_page)
        except Exception as e:
            print("Erro ao adicionar cliente:", e)
            flash("Erro ao salvar cliente. Tente novamente.", "error")
            return redirect(request.url)

    return render_template(template)
