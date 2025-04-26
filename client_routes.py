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

    from flask import render_template, request, redirect, url_for, session, flash
    from boto3.dynamodb.conditions import Key
    import datetime

    @app.route("/clients")
    def listar_clientes():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        current_path = request.path
        previous_path = session.get("previous_path_clients")

        # Atualiza o caminho atual para comparação futura
        session["previous_path_clients"] = current_path

        # --- Pegando parâmetros ---
        filtros = request.args.to_dict()
        cursor_token = filtros.pop("cursor", None)  # 🔥 Aqui você pega o cursor
        page = int(
            filtros.pop("page", 1)
        )  # 🛑 Mudamos para controle de página baseado em número

        if page == 1:
            session.pop("current_page_clients", None)
            session.pop("cursor_pages_clients", None)
            session.pop("last_page_clients", None)

        cursor_pages = session.get("cursor_pages_clients", {"1": None})

        # Se for primeira página, limpa histórico
        if page == 1:
            session["cursor_pages_clients"] = {"1": None}
            cursor_pages = {"1": None}

        session["current_page_clients"] = page

        # --- Definindo ExclusiveStartKey se houver ---
        exclusive_start_key = None
        if str(page) in cursor_pages and cursor_pages[str(page)]:
            exclusive_start_key = decode_dynamo_key(cursor_pages[str(page)])

        # --- Query no banco ---
        # --- Query no banco ---
        valid_clientes = []
        limit = 5  # Quantidade de clientes válidos desejada
        batch_size = 10  # Quantidade bruta trazida a cada chamada do DynamoDB

        last_valid_cliente = None
        raw_last_evaluated_key = None

        while len(valid_clientes) < limit:
            query_kwargs = {
                "IndexName": "account_id-created_at-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "ScanIndexForward": False,
                "Limit": batch_size,
            }
            if exclusive_start_key:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key

            response = clients_table.query(**query_kwargs)
            clientes = response.get("Items", [])
            raw_last_evaluated_key = response.get("LastEvaluatedKey")

            if not clientes:
                break  # Não há mais clientes no banco

            for cliente in clientes:
                if not cliente_atende_filtros(cliente, filtros):
                    continue

                valid_clientes.append(cliente)
                last_valid_cliente = cliente  # Atualiza o último cliente adicionado

                if len(valid_clientes) == limit:
                    break

            # Se não atingiu o limite, mas ainda há last evaluated key, continua buscando
            if len(valid_clientes) < limit:
                if raw_last_evaluated_key:
                    exclusive_start_key = raw_last_evaluated_key
                else:
                    break  # Não há mais itens no banco para buscar

        # --- Define next_cursor_token com base no último cliente válido ---
        next_cursor_token = None
        if last_valid_cliente:
            if (
                last_valid_cliente.get("account_id")
                and last_valid_cliente.get("created_at")
                and last_valid_cliente.get("client_id")
            ):
                next_cursor_token = encode_dynamo_key(
                    {
                        "account_id": last_valid_cliente["account_id"],
                        "created_at": last_valid_cliente["created_at"],
                        "client_id": last_valid_cliente["client_id"],
                    }
                )

        # --- Define has_next dinamicamente ---
        if len(valid_clientes) == limit and raw_last_evaluated_key:
            has_next = True
        else:
            has_next = False
        # se tem menos itens do que o limite por pagina, entao acabou tudo. Esconde botao next..
        print(has_next)

        # --- Atualiza sessão para a próxima página ---
        if has_next:
            next_cursor_token = encode_dynamo_key(raw_last_evaluated_key)
            session["cursor_pages_clients"][str(page + 1)] = next_cursor_token
        else:
            session["cursor_pages_clients"].pop(str(page + 1), None)

        print(has_next)

        # --- Caso não haja clientes encontrados em página >1 (quando tentou ir além do limite) ---
        # --- Se clicou para avançar e não há clientes
        if not valid_clientes and page > 1:
            flash("Não há mais clientes para exibir.", "info")
            last_valid_page = page - 1
            session["current_page_clients"] = last_valid_page
            session["last_page_clients"] = last_valid_page  # 🔥 Grava última página
            return redirect(url_for("listar_clientes", page=last_valid_page))

        # --- Quando for renderizar normal:
        last_page_clients = session.get("last_page_clients")
        current_page = session.get("current_page_clients", 1)

        # Se:
        # 1. O número de itens na página é menor que o limite (ou seja, não preencheu a página completamente)
        # 2. OU se a página atual já é maior ou igual à última página registrada
        if len(valid_clientes) < limit or (
            last_page_clients is not None and current_page >= last_page_clients
        ):
            has_next = False
        else:
            has_next = True

        return render_template(
            "clientes.html",
            itens=valid_clientes,
            current_page=session.get("current_page_clients", 1),
            has_next=has_next,
            has_prev=session.get("current_page_clients", 1) > 1,
        )

    @app.route("/editar_cliente/<client_id>", methods=["GET", "POST"])
    def editar_cliente(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        print("---- FORM DATA ----")
        print(request.form.to_dict())  # Campos do formulário
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

        # formatar cpf, teleofne e cnpj pra passar pro front exe 25.013.698-24
        def format_phone(phone):
            # Se cnpj for None ou vazio, retorna o próprio valor sem formatação
            if not phone:
                return phone
            # Remove todos os caracteres não numéricos
            phone = re.sub(r"\D", "", phone)
            # Formata como (XX) XXXXX-XXXX
            if len(phone) == 11:
                return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
            return phone

        def format_cpf(cpf):
            if not cpf:
                return cpf
            # Remove todos os caracteres não numéricos
            cpf = re.sub(r"\D", "", cpf)
            # Formata como XXX.XXX.XXX-XX
            if len(cpf) == 11:
                return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
            return cpf

        def format_cnpj(cnpj):
            if not cnpj:
                return cnpj
            # Remove todos os caracteres não numéricos
            cnpj = re.sub(r"\D", "", cnpj)
            # Formata como XX.XXX.XXX/0001-XX
            if len(cnpj) == 14:
                return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
            return cnpj

        if "client_tel" in cliente:
            cliente["client_tel"] = format_phone(cliente["client_tel"])

        if "client_cpf" in cliente:
            cliente["client_cpf"] = format_cpf(cliente["client_cpf"])

        if "client_cnpj" in cliente:
            cliente["client_cnpj"] = format_cnpj(cliente["client_cnpj"])

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

        # return redirect(url_for("listar_clientes"))
        # return redirect(next_page)
        return redirect(request.referrer)

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
import base64
import json


def encode_dynamo_key(key_dict):
    """Codifica um dicionário (LastEvaluatedKey) para um cursor seguro."""
    key_json = json.dumps(key_dict)
    key_bytes = key_json.encode("utf-8")
    encoded_key = base64.urlsafe_b64encode(key_bytes).decode("utf-8")
    return encoded_key


def decode_dynamo_key(encoded_key):
    """Decodifica um cursor para o formato de LastEvaluatedKey."""
    if not encoded_key:
        return None
    try:
        key_bytes = base64.urlsafe_b64decode(encoded_key.encode("utf-8"))
        key_json = key_bytes.decode("utf-8")
        return json.loads(key_json)
    except Exception as e:
        print(f"Erro ao decodificar cursor: {e}")
        return None


def cliente_atende_filtros(cliente, filtros):
    """
    Verifica se o cliente atende aos filtros fornecidos.
    """
    client_name = filtros.get("client_name", "").strip().lower()
    client_tel = "".join(filter(str.isdigit, filtros.get("client_tel", "")))
    client_email = filtros.get("client_email", "").strip().lower()
    client_address = filtros.get("client_address", "").strip().lower()
    client_cpf = "".join(filter(str.isdigit, filtros.get("client_cpf", "")))
    client_cnpj = "".join(filter(str.isdigit, filtros.get("client_cnpj", "")))
    client_obs = filtros.get("client_obs", "").strip().lower()

    return (
        (not client_name or client_name in (cliente.get("client_name") or "").lower())
        and (not client_tel or client_tel in (cliente.get("client_tel") or ""))
        and (
            not client_email
            or client_email in (cliente.get("client_email") or "").lower()
        )
        and (
            not client_address
            or client_address in (cliente.get("client_address") or "").lower()
        )
        and (not client_cpf or client_cpf in (cliente.get("client_cpf") or ""))
        and (not client_cnpj or client_cnpj in (cliente.get("client_cnpj") or ""))
        and (not client_obs or client_obs in (cliente.get("client_obs") or "").lower())
    )
