import datetime
import uuid
from boto3.dynamodb.conditions import Key
from utils import get_user_timezone
import os
from decimal import Decimal, InvalidOperation

import re
import base64
import json

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
    app,
    clients_table,
    transactions_table,
    itens_table,
    users_table,
    text_models_table,
    field_config_table,
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

        # Campos configuráveis
        fields_config = get_all_fields(account_id, field_config_table, entity="client")
        fields_config = sorted(fields_config, key=lambda x: x["order_sequence"])
        custom_fields_preview = [
            {"field_id": field["id"], "title": field["label"]}
            for field in fields_config
            if field.get("preview")
        ]

        force_no_next = request.args.get("force_no_next")
        session["previous_path_clients"] = request.path

        filtros = request.args.to_dict()
        page = int(filtros.pop("page", 1))

        if page == 1:
            session.pop("current_page_clients", None)
            session.pop("cursor_pages_clients", None)
            session.pop("last_page_clients", None)

        cursor_pages = session.get("cursor_pages_clients", {"1": None})
        if page == 1:
            session["cursor_pages_clients"] = {"1": None}
            cursor_pages = {"1": None}

        session["current_page_clients"] = page
        exclusive_start_key = (
            decode_dynamo_key(cursor_pages.get(str(page)))
            if str(page) in cursor_pages
            else None
        )

        valid_clientes = []
        limit = 5
        batch_size = 10
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
                break

            for cliente in clientes:
                if not cliente_atende_filtros_dinamico(cliente, filtros, fields_config):
                    continue

                valid_clientes.append(cliente)
                last_valid_cliente = cliente

                if len(valid_clientes) == limit:
                    break

            if len(valid_clientes) < limit:
                if raw_last_evaluated_key:
                    exclusive_start_key = raw_last_evaluated_key
                else:
                    break

        next_cursor_token = None
        if last_valid_cliente:
            if all(
                k in last_valid_cliente
                for k in ("account_id", "created_at", "client_id")
            ):
                next_cursor_token = encode_dynamo_key(
                    {
                        "account_id": last_valid_cliente["account_id"],
                        "created_at": last_valid_cliente["created_at"],
                        "client_id": last_valid_cliente["client_id"],
                    }
                )

        if next_cursor_token:
            session["cursor_pages_clients"][str(page + 1)] = next_cursor_token
        else:
            session["cursor_pages_clients"].pop(str(page + 1), None)

        last_page_clients = session.get("last_page_clients")
        current_page = session.get("current_page_clients", 1)

        has_next = False
        if not force_no_next and (
            len(valid_clientes) == limit
            and (last_page_clients is None or current_page < last_page_clients)
        ):
            has_next = True

        if not valid_clientes and page > 1:
            flash("Não há mais clientes para exibir.", "info")
            last_valid_page = page - 1
            session["current_page_clients"] = last_valid_page
            session["last_page_clients"] = last_valid_page
            return redirect(
                url_for("listar_clientes", page=last_valid_page, force_no_next=1)
            )

        return render_template(
            "clientes.html",
            itens=valid_clientes,
            current_page=current_page,
            has_next=has_next,
            has_prev=current_page > 1,
            fields_config=fields_config,
            custom_fields_preview=custom_fields_preview,
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
            field_config_table,
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
        from item_routes import list_transactions

        # Lógica aqui para buscar transações do cliente
        return list_transactions(
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
    field_config_table,
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("listar_clientes"))

    account_id = session.get("account_id")
    user_id = session.get("user_id")
    user_utc = get_user_timezone(users_table, user_id)

    # Pega campos configurados para 'client'
    all_fields = get_all_fields(account_id, field_config_table, entity="client")

    if request.method == "POST":
        client_id = str(uuid.uuid4())
        new_client = {
            "client_id": client_id,
            "account_id": account_id,
            "created_at": datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

        key_values = {}

        for field in all_fields:
            field_id = field["id"]
            label = field.get("label", field_id)
            value = request.form.get(field_id, "").strip()

            if not value:
                continue

            # Se o campo estiver presente, salva direto
            if field["type"] == "number":
                try:
                    value = Decimal(value.replace(".", "").replace(",", "."))
                except InvalidOperation:
                    flash(f"O campo {label} possui um número inválido.", "danger")
                    return redirect(request.url)

            key_values[field_id] = value

        # Se 'client_name' estiver nos campos, usa ele como título principal
        client_name = key_values.get("client_name")
        if client_name:
            new_client["client_name"] = client_name

        # Verificação de duplicatas — apenas se algum dos campos-chave estiver presente
        existing_clients = clients_table.scan().get("Items", [])
        for client in existing_clients:
            if client.get("account_id") != account_id:
                continue

            if client_name and client.get("client_name") == client_name:
                flash("Já existe um cliente com esse nome.", "error")
                return redirect(request.url)

            if (
                key_values.get("client_cpf")
                and client.get("client_cpf") == key_values["client_cpf"]
            ):
                flash("Já existe um cliente com esse CPF.", "error")
                return redirect(request.url)

            if (
                key_values.get("client_cnpj")
                and client.get("client_cnpj") == key_values["client_cnpj"]
            ):
                flash("Já existe um cliente com esse CNPJ.", "error")
                return redirect(request.url)

        # Junta os campos no cliente
        new_client.update(key_values)

        try:
            clients_table.put_item(Item=new_client)
            flash("Cliente adicionado com sucesso!", "success")
            return redirect(next_page)
        except Exception as e:
            print("Erro ao adicionar cliente:", e)
            flash("Erro ao salvar cliente. Tente novamente.", "error")
            return redirect(request.url)

    return render_template(template, all_fields=all_fields, next=next_page, client={})


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


def cliente_atende_filtros_dinamico(cliente, filtros, fields_config):
    from decimal import Decimal, InvalidOperation

    for field in fields_config:
        field_id = field["id"]
        field_type = field.get("type")
        value = (
            cliente.get("key_values", {}).get(field_id) or cliente.get(field_id) or ""
        )

        if field_type == "string":
            filtro = filtros.get(field_id)
            if filtro and filtro.lower() not in str(value).lower():
                return False

        elif field_type == "number":
            min_val = filtros.get(f"min_" + field_id)
            max_val = filtros.get(f"max_" + field_id)
            try:
                value = Decimal(str(value))
                if min_val and value < Decimal(min_val):
                    return False
                if max_val and value > Decimal(max_val):
                    return False
            except (InvalidOperation, ValueError):
                return False

        elif field_type == "dropdown":
            selected = filtros.get(field_id)
            if selected and selected != value:
                return False

        elif field_type == "date":
            start_date = filtros.get(f"start_{field_id}")
            end_date = filtros.get(f"end_{field_id}")
            try:
                date_val = datetime.datetime.strptime(value, "%Y-%m-%d").date()
                if (
                    start_date
                    and date_val
                    < datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
                ):
                    return False
                if (
                    end_date
                    and date_val
                    > datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
                ):
                    return False
            except:
                if start_date or end_date:
                    return False

    return True


def get_all_fields(account_id, field_config_table, entity):

    config_response = field_config_table.get_item(
        Key={"account_id": account_id, "entity": entity}
    )
    fields_config = config_response.get("Item", {}).get("fields_config", {})

    all_fields = []
    for field_id, cfg in fields_config.items():
        label = cfg.get("label", field_id.replace("_", " ").capitalize())
        print(f"Field ID: {field_id}")
        print(f"CFG: {cfg}")
        print(f"Label: {cfg.get('label')}")
        print(f"Title: {cfg.get('title')}")

        all_fields.append(
            {
                "id": field_id,
                "label": label,
                "title": label,  # usado em outras rotas
                "type": cfg.get("type", "string"),
                "required": cfg.get("required", False),
                "visible": cfg.get("visible", True),
                "preview": cfg.get("preview", False),
                "filterable": cfg.get("filterable", False),
                "order_sequence": int(cfg.get("order_sequence", 999)),
                "options": (
                    cfg.get("options", []) if cfg.get("type") == "dropdown" else []
                ),
                "fixed": cfg.get("f_type", "custom") == "fixed",
            }
        )
    return sorted(all_fields, key=lambda x: x["order_sequence"])


"""
     @app.route("/add_client", methods=["GET", "POST"])
    def add_client():
        return add_client_common(
            request,
            clients_table,
            session,
            flash,
            redirect,
            url_for,
            "add_client.html",
            field_config_table,
        )
"""
