import datetime
import uuid
from boto3.dynamodb.conditions import Key
from utils import get_user_timezone
import schemas
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
):
    @app.route("/open_client/<client_ref>")
    def open_client(client_ref):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Primeiro tenta por chave primária (client_id)
        try:
            response = clients_table.get_item(Key={"client_id": client_ref})
            cliente = response.get("Item")
            if cliente:
                return redirect(url_for("listar_clientes") + f"?client_id={cliente['client_id']}")
        except Exception:
            pass

        # Se não encontrar, informa e volta para lista
        flash("Cliente não encontrado.", "danger")
        return redirect(url_for("listar_clientes"))

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
                Limit=6,
            )

            suggestions = [
                {
                    "client_name": item.get("client_name", ""),
                    "client_cpf": item.get("client_cpf", ""),
                    "client_cnpj": item.get("client_cnpj", ""),
                    "client_phone": item.get("client_phone", ""),
                    "client_id": item.get("client_id", ""),
                    "client_email": item.get("client_email", ""),  # 👈 adiciona aqui
                    "client_address": item.get("client_address", ""),  # 👈 e aqui
                    "client_obs": item.get("client_obs", ""),  # 👈 e aqui
                }
                for item in response.get("Items", [])
            ]

            print(f"Encontrados {len(suggestions)} sugestões para '{term}'")
            print(suggestions)
            return jsonify(suggestions)

        except Exception as e:
            print(f"Erro na busca de autocomplete: {str(e)}")
            return jsonify([])  # Retorna lista vazia em caso de erro

    @app.route("/autocomplete_clients_by_id")
    def autocomplete_clients_by_id():
        from boto3.dynamodb.conditions import Attr
        
        account_id = session.get("account_id")
        term = request.args.get("term", "").strip()

        print(f"Buscando clientes por ID com termo: {term}")

        if not term:
            return jsonify([])

        try:
            # Fazer scan para buscar por client_id que contenha o termo
            response = clients_table.scan(
                FilterExpression=Attr("account_id").eq(account_id) & Attr("client_id").contains(term),
                Limit=10
            )

            suggestions = [
                {
                    "client_name": item.get("client_name", ""),
                    "client_cpf": item.get("client_cpf", ""),
                    "client_cnpj": item.get("client_cnpj", ""),
                    "client_phone": item.get("client_phone", ""),
                    "client_id": item.get("client_id", ""),
                    "client_email": item.get("client_email", ""),
                    "client_address": item.get("client_address", ""),
                    "client_obs": item.get("client_obs", ""),
                }
                for item in response.get("Items", [])
            ]

            print(f"Encontrados {len(suggestions)} clientes por ID para '{term}'")
            return jsonify(suggestions)

        except Exception as e:
            print(f"Erro na busca de autocomplete por ID: {str(e)}")
            return jsonify([])  # Retorna lista vazia em caso de erro

    @app.route("/clients")
    def listar_clientes():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        # Campos fixos via Schema
        fields_config = schemas.get_schema_fields("client")
        fields_config = sorted(fields_config, key=lambda x: x["order_sequence"])
        force_no_next = request.args.get("force_no_next")
        session["previous_path_clients"] = request.path

        filtros = request.args.to_dict()
        page = int(filtros.pop("page", 1))

        filtros = request.args.to_dict()
        page = int(filtros.pop("page", 1))

        # Adicionado: só considera filtros relevantes se houver algo útil além de page/client_id
        filtro_relevante = bool(filtros) and "client_id" not in request.args

        # se é um cliente especifico, ja mostra logo
        client_id_param = request.args.get("client_id")
        if client_id_param:
            response = clients_table.get_item(Key={"client_id": client_id_param})
            cliente = response.get("Item")

            if not cliente or cliente.get("account_id") != account_id:
                flash("Cliente não encontrado ou acesso negado.", "danger")
                return redirect(url_for("listar_clientes"))

            return render_template(
                "clientes.html",
                itens=[cliente],
                current_page=1,
                has_next=False,
                has_prev=False,
                fields_config=fields_config,
                ns={"filtro_relevante": False},
            )


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
        limit = 6
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

            from utils import entidade_atende_filtros_dinamico  # certifique-se de importar isso corretamente

            for cliente in clientes:
                if cliente.get("status") == "deleted":
                    continue

                if not entidade_atende_filtros_dinamico(cliente, filtros, fields_config):
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
            ns={"filtro_relevante": filtro_relevante},
        )
    import traceback

    @app.route("/editar_cliente/<client_id>", methods=["GET", "POST"])
    def editar_cliente(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        raw_next = request.args.get("next", url_for("listar_clientes"))
        next_page = raw_next if "?" not in raw_next else f"{raw_next}&client_id={client_id}"

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        response = clients_table.get_item(Key={"client_id": client_id})
        cliente = response.get("Item")

        if not cliente or cliente.get("account_id") != account_id:
            flash("Cliente não encontrado ou acesso negado.", "danger")
            return redirect(next_page)

        all_fields = schemas.get_schema_fields("client")

        if request.method == "POST":
            import re
            from boto3.dynamodb.conditions import Key

            new_name = request.form.get("client_name", "").strip()
            new_cpf = re.sub(r"\D", "", request.form.get("client_cpf", ""))
            new_cnpj = re.sub(r"\D", "", request.form.get("client_cnpj", ""))

            updated_values = {}

            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")
                # is_fixed ignorado
                value = request.form.get(field_id, "").strip()

                if not value:
                    continue

                if field_type in ["cpf", "cnpj", "phone", "client_cpf", "client_cnpj", "client_phone"]:
                    value = re.sub(r"\D", "", value)
                elif field_type == "value":
                    value = value.replace(".", "").replace(",", ".")
                    try:
                        value = float(value)
                    except ValueError:
                        flash(f"Valor inválido no campo {field.get('label', field_id)}.", "error")
                        return render_template("editar_cliente.html", client=cliente, all_fields=all_fields, next=next_page)

                updated_values[field_id] = value

            # Atualiza cliente
            for k, v in updated_values.items():
                cliente[k] = v

            # Verificações de unicidade com nomes de índices corretos
            index_map = {
                "client_name": "account_id-client_name-index",
                "client_cnpj": "account_id-client_cnpj-index",
                "client_cpf": "account_id-client_cpf-index",
            }

            for field, index in index_map.items():
                field_value = request.form.get(field, "").strip()
                if not field_value:
                    continue

                response = clients_table.query(
                    IndexName=index,
                    KeyConditionExpression=Key("account_id").eq(account_id) & Key(field).eq(field_value),
                )

                # Se houver mais de 1 item OU um item com client_id diferente, é duplicado
                for c in response.get("Items", []):
                    if c.get("client_id") != client_id:
                        label = {"client_name": "nome", "client_cnpj": "CNPJ", "client_cpf": "CPF"}.get(field, field)
                        flash(f"Já existe um cliente com esse {label}.", "error")
                        return render_template("editar_cliente.html", client=cliente, all_fields=all_fields, next=next_page)

            cliente["updated_at"] = datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S")

            try:
                clients_table.put_item(Item=cliente)

                if request.form.get("update_all_transactions"):
                    response = transactions_table.query(
                        IndexName="client_id-index",
                        KeyConditionExpression=Key("client_id").eq(client_id),
                    )
                    transacoes = response.get("Items", [])

                    for tx in transacoes:
                        update_expr = []
                        expr_values = {}
                        expr_names = {}

                        # Atualiza campos fixos e customizados (agora na raiz)
                        for k, v in updated_values.items():
                            update_expr.append(f"#{k} = :{k}")
                            expr_values[f":{k}"] = v
                            expr_names[f"#{k}"] = k

                        if update_expr:
                            update_kwargs = {
                                "Key": {"transaction_id": tx["transaction_id"]},
                                "UpdateExpression": "SET " + ", ".join(update_expr),
                                "ExpressionAttributeValues": expr_values,
                            }
                            if expr_names:
                                update_kwargs["ExpressionAttributeNames"] = expr_names

                            transactions_table.update_item(**update_kwargs)


                flash("Cliente atualizado com sucesso!", "success")
                return redirect(next_page)


            except Exception as e:
                print("❌ Erro ao atualizar cliente:")
                traceback.print_exc()  # Agora funcionará
                flash(f"Erro ao atualizar cliente: {str(e)}", "danger")
                return redirect(next_page)

        # ---------- GET ----------
        prepared = {}
        for f in all_fields:
            fid = f["id"]
            # Busca sempre na raiz
            prepared[fid] = cliente.get(fid, "")
            
        prepared["client_id"] = cliente["client_id"]

        return render_template(
            "editar_cliente.html",
            client=prepared,
            all_fields=all_fields,
            next=next_page,
        )


    @app.route("/clientes/adicionar", methods=["GET", "POST"])
    def adicionar_cliente():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("listar_clientes"))
        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        all_fields = schemas.get_schema_fields("client")

        if request.method == "POST":
            client_id = str(uuid.uuid4().hex[:12])
            new_client = {
                "client_id": client_id,
                "account_id": account_id,
                "created_at": datetime.datetime.now(user_utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }

            import re

            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")
                # is_fixed ignorado
                value = request.form.get(field_id, "").strip()

                if not value:
                    continue

                # Limpeza de dados
                if field_type in [
                    "client_cpf",
                    "client_cnpj",
                    "client_phone",
                    "cpf",
                    "cnpj",
                    "phone",
                ]:
                    value = re.sub(r"\D", "", value)
                elif field_type == "value":
                    value = value.replace(".", "").replace(",", ".")
                    try:
                        value = float(value)
                    except ValueError:
                        flash(
                            f"Valor inválido no campo {field.get('label', field_id)}.",
                            "error",
                        )
                        return render_template(
                            "add_client.html",
                            all_fields=all_fields,
                            next=next_page,
                            client=request.form,
                        )

                # Salva tudo na raiz
                new_client[field_id] = value

            # Verificação de duplicatas
            existing_clients = clients_table.scan().get("Items", [])
            for client in existing_clients:
                if client.get("account_id") != account_id:
                    continue

                if (
                    new_client.get("client_name")
                    and client.get("client_name") == new_client["client_name"]
                ):
                    flash("Já existe um cliente com esse nome.", "error")
                    return render_template(
                        "add_client.html",
                        all_fields=all_fields,
                        next=next_page,
                        client=request.form,
                    )

                if (
                    new_client.get("client_cpf")
                    and client.get("client_cpf") == new_client["client_cpf"]
                ):
                    flash("Já existe um cliente com esse CPF.", "error")
                    return render_template(
                        "add_client.html",
                        all_fields=all_fields,
                        next=next_page,
                        client=request.form,
                    )

                if (
                    new_client.get("client_cnpj")
                    and client.get("client_cnpj") == new_client["client_cnpj"]
                ):
                    flash("Já existe um cliente com esse CNPJ.", "error")
                    return render_template(
                        "add_client.html",
                        all_fields=all_fields,
                        next=next_page,
                        client=request.form,
                    )

            try:
                clients_table.put_item(Item=new_client)
                flash("Cliente adicionado com sucesso!", "success")
                return redirect(next_page)
            except Exception as e:
                print("Erro ao adicionar cliente:", e)
                flash("Erro ao salvar cliente. Tente novamente.", "error")
                return redirect(request.url)

        return render_template(
            "add_client.html",
            all_fields=all_fields,
            next=next_page,
            client=request.form,
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

            # Soft Delete
            user_id = session.get("user_id")
            user_utc = get_user_timezone(users_table, user_id)
            deleted_date = datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S")

            clients_table.update_item(
                Key={"client_id": client_id},
                UpdateExpression="SET #status = :deleted, deleted_date = :deleted_date",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":deleted": "deleted", ":deleted_date": deleted_date},
            )

            flash("Cliente enviado para a lixeira!", "success")
        except Exception as e:
            print("Erro ao deletar cliente:", e)
            flash("Erro ao deletar cliente. Tente novamente.", "danger")

        # return redirect(url_for("listar_clientes"))
        # return redirect(next_page)
        return redirect(request.referrer)

    @app.route("/trash_clients")
    def trash_clients():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        if not account_id:
            return redirect(url_for("login"))

        # Campos configuráveis
        fields_config = schemas.get_schema_fields("client")
        fields_config = sorted(fields_config, key=lambda x: x["order_sequence"])

        force_no_next = request.args.get("force_no_next")
        session["previous_path_trash_clients"] = request.path

        filtros = request.args.to_dict()
        page = int(filtros.pop("page", 1))

        if page == 1:
            session.pop("current_page_trash_clients", None)
            session.pop("cursor_pages_trash_clients", None)
            session.pop("last_page_trash_clients", None)

        cursor_pages = session.get("cursor_pages_trash_clients", {"1": None})
        if page == 1:
            session["cursor_pages_trash_clients"] = {"1": None}
            cursor_pages = {"1": None}

        session["current_page_trash_clients"] = page
        exclusive_start_key = (
            decode_dynamo_key(cursor_pages.get(str(page)))
            if str(page) in cursor_pages
            else None
        )

        valid_clientes = []
        limit = 6
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
                # Apenas clientes deletados
                if cliente.get("status") != "deleted":
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
            session["cursor_pages_trash_clients"][str(page + 1)] = next_cursor_token
        else:
            session["cursor_pages_trash_clients"].pop(str(page + 1), None)

        last_page_clients = session.get("last_page_trash_clients")
        current_page = session.get("current_page_trash_clients", 1)

        has_next = False
        if not force_no_next and (
            len(valid_clientes) == limit
            and (last_page_clients is None or current_page < last_page_clients)
        ):
            has_next = True

        if not valid_clientes and page > 1:
            flash("Não há mais clientes na lixeira.", "info")
            last_valid_page = page - 1
            session["current_page_trash_clients"] = last_valid_page
            session["last_page_trash_clients"] = last_valid_page
            return redirect(
                url_for("trash_clients", page=last_valid_page, force_no_next=1)
            )

        return render_template(
            "trash_clients.html",
            itens=valid_clientes,
            current_page=current_page,
            has_next=has_next,
            has_prev=current_page > 1,
        )

    @app.route("/restore_deleted_client", methods=["POST"])
    def restore_deleted_client():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            client_data_json = request.form.get("client_data")
            if not client_data_json:
                flash("Dados do cliente inválidos.", "danger")
                return redirect(url_for("trash_clients"))

            client_data = json.loads(client_data_json)
            client_id = client_data.get("client_id")

            if not client_id:
                flash("ID do cliente não encontrado.", "danger")
                return redirect(url_for("trash_clients"))

            # Remove status=deleted e deleted_date para restaurar
            clients_table.update_item(
                Key={"client_id": client_id},
                UpdateExpression="REMOVE #status, deleted_date",
                ExpressionAttributeNames={"#status": "status"},
            )

            flash("Cliente restaurado com sucesso!", "success")

        except Exception as e:
            print(f"Erro ao restaurar cliente: {e}")
            flash(f"Erro ao restaurar cliente: {str(e)}", "danger")

        return redirect(url_for("trash_clients"))

    @app.route("/permanently_delete_client/<client_id>", methods=["POST"])
    def permanently_delete_client(client_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            clients_table.delete_item(Key={"client_id": client_id})
            flash("Cliente excluído definitivamente.", "success")
        except Exception as e:
            print(f"Erro ao excluir cliente definitivamente: {e}")
            flash(f"Erro ao excluir cliente: {str(e)}", "danger")

        return redirect(url_for("trash_clients"))

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



 
