import datetime
import uuid
from urllib.parse import urlparse
from boto3.dynamodb.conditions import Key, Attr

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    current_app,
)
import json
import datetime
from decimal import Decimal

from utils import get_user_timezone

from boto3.dynamodb.conditions import Key, Attr

ALLOWED_EXTENSIONS = {"jpeg", "jpg", "png", "gif", "webp"}


def handle_image_upload(image_file):
    if image_file and image_file.filename != "":
        if allowed_file(image_file.filename):
            return upload_image_to_s3(image_file)
    return "N/A"


def allowed_file(filename):
    """Verifica se o arquivo tem uma extensão válida."""
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


from utils import upload_image_to_s3, aplicar_filtro, copy_image_in_s3


"""
def get_all_fields(account_id, field_config_table):

    config_response = field_config_table.get_item(
        Key={"account_id": account_id, "entity": "item"}
    )
    fields_config = config_response.get("Item", {}).get("fields_config", {})

    all_fields = []
    for field_id, cfg in fields_config.items():
        label = cfg.get("label", field_id.replace("_", " ").capitalize())

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
                    cfg.get("options", [])
                    if cfg.get("type") in ["dropdown", "transaction_status"]
                    else []
                ),
                "fixed": cfg.get("f_type", "custom") == "fixed",
            }
        )
    return sorted(all_fields, key=lambda x: x["order_sequence"])
"""


def init_item_routes(
    app,
    itens_table,
    s3,
    s3_bucket_name,
    transactions_table,
    clients_table,
    users_table,
    text_models_table,
    payment_transactions_table,
    custom_fields_table,
    field_config_table,
):

    @app.route("/rented")
    def rented():
        return list_transactions(
            ["rented"],
            "rented.html",
            "Itens retirados",
            transactions_table,
            itens_table,
            users_table,
            text_models_table,
            field_config_table,
            page="rented",
        )

    @app.route("/reserved")
    def reserved():
        return list_transactions(
            ["reserved"],
            "reserved.html",
            "Itens reservados (não retirados)",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
            field_config_table,
            page="reserved",
        )

    @app.route("/all_transactions")
    def all_transactions():
        return list_transactions(
            ["reserved", "rented", "returned"],
            "all_transactions.html",
            "Todas as transações",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
            field_config_table,
            page="all_transactions",
        )

    @app.route("/returned")
    def returned():
        return list_transactions(
            ["returned"],
            "returned.html",
            "Itens devolvidos",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
            field_config_table,
            page="returned",
        )

    @app.route("/archive")
    def archive():
        return list_raw_itens(
            ["archive"],
            "archive.html",
            "Itens Arquivados",
            itens_table,
            transactions_table,
            users_table,
            payment_transactions_table,
            field_config_table,
        )

    @app.route("/trash_itens")
    def trash_itens():
        return list_raw_itens(
            ["deleted", "version"],
            "trash_itens.html",
            "Histórico de alterações",
            itens_table,
            transactions_table,
            users_table,
            payment_transactions_table,
            field_config_table,
        )

    @app.route("/trash_transactions")
    def trash_transactions():
        return list_transactions(
            ["deleted", "version"],
            "trash_transactions.html",
            "Lixeira de transações",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
            field_config_table,
            page="trash_transactions",
        )

    @app.route("/inventory")
    def inventory():
        return list_raw_itens(
            ["available"],
            "inventory.html",
            "Inventário",
            itens_table,
            transactions_table,
            users_table,
            payment_transactions_table,
            field_config_table,
            entity="item",
        )

    @app.route("/add_item", methods=["GET", "POST"])
    def add_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))
        origin = next_page.rstrip("/").split("/")[-1]
        origin_status = "available" if origin == "inventory" else "archive"
        title = "Adicionar item em inventário" if origin_status == "available" else "Adicionar item em arquivo"


        user_id = session.get("user_id")
        account_id = session.get("account_id")

        # -------------------------- GET --------------------------
        if request.method == "GET":

            #contar itens para limitar plano
            total_itens = 0
            exclusive_start_key = None
            while True:
              query_kwargs = {
                  "IndexName": "account_id-created_at-index",
                  "KeyConditionExpression": Key("account_id").eq(account_id),
                  "FilterExpression": Attr("status").is_in(["available", "archive"]),
                  "Select": "COUNT",
              }

              if exclusive_start_key:
                  query_kwargs["ExclusiveStartKey"] = exclusive_start_key

              response = itens_table.query(**query_kwargs)
              total_itens += response.get("Count", 0)

              exclusive_start_key = response.get("LastEvaluatedKey")
              if not exclusive_start_key:
                  break

            user_id = session.get("user_id")
            current_stripe_transaction = get_latest_transaction(user_id, users_table, payment_transactions_table)




            all_fields = get_all_fields(account_id, field_config_table, "item")

            return render_template(
                "add_item.html", next=next_page, all_fields=all_fields, total_itens=total_itens, current_stripe_transaction=current_stripe_transaction, title=title, item={}
            )
        # -------------------------- POST --------------------------
        if request.method == "POST":
            import json
            from decimal import Decimal, InvalidOperation
            import re

            all_fields = get_all_fields(account_id, field_config_table, "item")

            item_data = {
                "user_id": user_id,
                "account_id": account_id,
                "item_id": str(uuid.uuid4().hex[:12]),
                "status": origin_status,
                "previous_status": request.form.get("status"),
                "created_at": datetime.datetime.now(
                    get_user_timezone(users_table, user_id)
                ).strftime("%Y-%m-%d %H:%M:%S"),
            }

            image_file = request.files.get("image_file")
            key_values = {}

            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")
                is_fixed = field.get("fixed", False)
                raw_value = request.form.get(field_id, "").strip()
                value = raw_value

                if field_id == "item_image_url":
                    value = handle_image_upload(image_file, "N/A")

                elif value:
                    # 🔸 Limpa número/monetário
                    if field_type in ["value", "item_value"]:
                        try:
                            value = Decimal(value.replace(".", "").replace(",", "."))
                        except InvalidOperation:
                            flash(
                                f"O campo {field.get('label') or field.get('title')} possui um número inválido.",
                                "danger",
                            )
                            return redirect(request.url)

                    # 🔸 Limpa CPF/CNPJ/Telefone
                    elif field_type in ["cpf", "cnpj", "phone"]:
                        value = re.sub(r"\D", "", value)

                # Decide onde armazenar
                if is_fixed:
                    item_data[field_id] = value
                else:
                    key_values[field_id] = value

            item_data["key_values"] = key_values

            # 🔒 Verifica se item_custom_id já existe **ANTES** de salvar
            item_custom_id = item_data.get("item_custom_id")
            if item_custom_id:
                existing = itens_table.query(
                    IndexName="account_id-item_custom_id-index",
                    KeyConditionExpression=Key("account_id").eq(account_id) & Key("item_custom_id").eq(item_custom_id)
                ).get("Items", [])

                if existing:
                    flash("Já existe um item com esse ID. Por favor, use um ID diferente.", "danger")
                    return render_template(
                        "add_item.html",
                        next=next_page,
                        all_fields=all_fields,
                        current_stripe_transaction=get_latest_transaction(user_id, users_table, payment_transactions_table),
                        title=title,
                        item={**item_data, "key_values": key_values}
                    )


            itens_table.put_item(Item=item_data)


            flash("Item adicionado com sucesso!", "success")
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG e WEBP.",
                    "danger",
                )

            return redirect(next_page)

    ######################################################################################################

    @app.route("/edit_transaction/<transaction_id>", methods=["GET", "POST"])
    def edit_transaction(transaction_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        response = transactions_table.get_item(Key={"transaction_id": transaction_id})
        transaction = response.get("Item")

        if not transaction:
            flash("Transação não encontrada.", "danger")
            return redirect(next_page)

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        item_id = transaction.get("item_id")
        client_id = transaction.get("client_id")

        item = itens_table.get_item(Key={"item_id": item_id}).get("Item", {})
        client = clients_table.get_item(Key={"client_id": client_id}).get("Item", {})

        fields_transaction = get_all_fields(account_id, field_config_table, entity="transaction")


        print(fields_transaction)



        response = transactions_table.query(
            IndexName="item_id-index",
            KeyConditionExpression="item_id = :item_id_val",
            ExpressionAttributeValues={":item_id_val": item_id},
        )

        reserved_ranges = [
            [tx["rental_date"], tx["return_date"]]
            for tx in response.get("Items", [])
            if tx.get("transaction_status") in ["rented", "reserved"]
            and tx.get("transaction_id") != transaction_id
            and tx.get("rental_date") and tx.get("return_date")
        ]

        if request.method == "POST":
            import re
            from decimal import Decimal, InvalidOperation
            import json

            fixed_fields = {}
            key_values = {}

            try:
                rental_str, return_str = request.form.get("transaction_period", "").split(" - ")
                rental_date = datetime.datetime.strptime(rental_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
                return_date = datetime.datetime.strptime(return_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                flash("Formato de data inválido. Use DD/MM/AAAA.", "danger")
                return redirect(request.url)

            fixed_fields["rental_date"] = rental_date
            fixed_fields["return_date"] = return_date

            for field in fields_transaction:
                field_id = field["id"]
                raw_value = request.form.get(field_id, "").strip()
                if not raw_value:
                    continue

                if field.get("type") in ["value", "item_value", "transaction_value_paid", "transaction_price"]:
                    raw_value = raw_value.replace(".", "").replace(",", ".")
                    try:
                        value = Decimal(raw_value)
                    except InvalidOperation:
                        flash(f"Valor inválido no campo {field.get('label', field_id)}.", "danger")
                        return redirect(request.url)
                else:
                    value = raw_value

                if field.get("fixed"):
                    fixed_fields[field_id] = value
                else:
                    key_values[field_id] = value

            # Monta a expressão de atualização
            update_expr_parts = []
            expr_attr_values = {}
            expr_attr_names = {}

            for key, val in fixed_fields.items():
                update_expr_parts.append(f"{key} = :{key}")
                expr_attr_values[f":{key}"] = val

            # Atualiza o key_values como um todo
            update_expr_parts.append("key_values = :kv")
            expr_attr_values[":kv"] = key_values

            update_args = {
                "Key": {"transaction_id": transaction_id},
                "UpdateExpression": "SET " + ", ".join(update_expr_parts),
                "ExpressionAttributeValues": expr_attr_values,
            }

            # Corrigido aqui: expr_attr_names em vez de expr_names
            if expr_attr_names:
                update_args["ExpressionAttributeNames"] = expr_attr_names

            print("DEBUG update_args:", json.dumps(update_args, indent=2, default=str))

            try:
                transactions_table.update_item(**update_args)
                flash("Transação atualizada com sucesso.", "success")
                return redirect(next_page)
            except Exception as e:
                flash("Erro ao atualizar transação.", "danger")
                print("Erro DynamoDB:", e)
                return redirect(request.url)

        # Se GET, renderiza com dados existentes
        transaction["range_date"] = (
            f"{datetime.datetime.strptime(transaction['rental_date'], '%Y-%m-%d').strftime('%d/%m/%Y')} - "
            f"{datetime.datetime.strptime(transaction['return_date'], '%Y-%m-%d').strftime('%d/%m/%Y')}"
        )

        return render_template(
            "edit_transaction.html",
            item=item,
            client=client,
            transaction=transaction,
            reserved_ranges=reserved_ranges,
            all_fields=fields_transaction,
            item_editavel=False,
            client_editavel=False,
        )



    ############################################################################################################

    def handle_image_upload(image_file, old_image_url):
        """Faz upload da nova imagem e retorna a URL da nova imagem (sem deletar a antiga)."""
        if image_file and image_file.filename:
            if allowed_file(image_file.filename):
                return upload_image_to_s3(
                    image_file
                )  # Faz o upload e retorna a nova URL
            else:
                flash(
                    "Formato de arquivo não permitido. Use JPEG, PNG ou WEBP.", "danger"
                )
                return old_image_url  # Mantém a imagem antiga se a nova for inválida
        return old_image_url  # Mantém a URL original se nenhuma nova imagem foi enviada

    ####################################################################################################

    from decimal import Decimal, InvalidOperation

    def process_form_data(request, item_before):
        account_id = session.get("account_id")

        # Recupera todos os campos para esta conta
        config_response = field_config_table.get_item(
            Key={"account_id": account_id, "entity": "item"}
        )
        fields_config = config_response.get("Item", {}).get("fields_config", {})

        # Campos fixos conhecidos
        DEFAULT_FIELDS = [
            {"id": "item_custom_id", "type": "string"},
            {"id": "description", "type": "string"},
            {"id": "item_obs", "type": "string"},
            {"id": "valor", "type": "number"},
            {"id": "image_url", "type": "string"},
        ]

        # Campos customizados
        custom_fields = custom_fields_table.query(
            IndexName="account_id-entity-index",
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("entity").eq("item"),
        ).get("Items", [])

        all_fields = DEFAULT_FIELDS + [
            {"id": f["field_id"], "type": f["type"]} for f in custom_fields
        ]

        # Cria um dicionário para lookup rápido por tipo
        field_type_map = {f["id"]: f["type"] for f in all_fields}

        new_data = {}

        for field_id in field_type_map:
            raw_value = request.form.get(field_id, "").strip()

            if raw_value == "":
                new_data[field_id] = ""
                continue

            field_type = field_type_map[field_id]

            if field_type == "number":
                try:
                    # Converte "1.234,56" para "1234.56"
                    cleaned_value = raw_value.replace(".", "").replace(",", ".")
                    new_data[field_id] = Decimal(cleaned_value)
                except InvalidOperation:
                    flash(f"O campo '{field_id}' possui um número inválido.", "danger")
                    raise Exception("Número inválido")
            else:
                new_data[field_id] = raw_value

        return new_data

    ########################################################################################################

    @app.route("/edit_item/<item_id>", methods=["GET", "POST"])
    def edit_item(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))
        account_id = session.get("account_id")

        # Buscar item existente
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("inventory"))

        key_values = item.get("key_values", {})

        # Carregar configuração dos campos
        config_response = field_config_table.get_item(
            Key={"account_id": account_id, "entity": "item"}
        )
        fields_config_map = config_response.get("Item", {}).get("fields_config", {})

        all_fields = []
        for field_id, cfg in fields_config_map.items():
            all_fields.append(
                {
                    "id": field_id,
                    "label": cfg.get("label", field_id.replace("_", " ").capitalize()),
                    "type": cfg.get("type", "string"),
                    "required": cfg.get("required", False),
                    "visible": cfg.get("visible", True),
                    "order_sequence": int(cfg.get("order_sequence", 999)),
                    "fixed": cfg.get("f_type", "custom") == "fixed",
                    "options": (
                        cfg.get("options", []) if cfg.get("type") == "dropdown" else []
                    ),
                }
            )
        all_fields = sorted(all_fields, key=lambda x: x["order_sequence"])

        # ---------------- POST ----------------
        if request.method == "POST":
            image_file = request.files.get("image_file")
            image_url_field = request.form.get("item_image_url", "").strip()
            old_image_url = item.get("item_image_url") or key_values.get("item_image_url", "N/A")
            new_image_url = (
                "N/A" if image_url_field == "DELETE_IMAGE" else handle_image_upload(image_file, old_image_url)
            )

            import re
            from decimal import Decimal, InvalidOperation

            new_values = {}
            fixed_updates = {}
            form_keys = set(request.form.keys())

            for field in all_fields:
                field_id = field["id"]
                field_type = field["type"]
                if field_id not in form_keys:
                    continue

                raw_value = request.form.get(field_id, "").strip()
                if not raw_value and field_type != "item_image_url":
                    continue

                value = raw_value

                if field_id == "item_image_url":
                    value = new_image_url
                elif field_type in ["value", "item_value"]:
                    try:
                        value = Decimal(value.replace(".", "").replace(",", "."))
                    except InvalidOperation:
                        flash(f"O campo {field['label']} possui valor inválido.", "danger")
                        return redirect(request.url)
                elif field_type in ["cpf", "cnpj", "phone"]:
                    value = re.sub(r"\D", "", value)

                if field["fixed"]:
                    fixed_updates[field_id] = value
                else:
                    new_values[field_id] = value

                # ✅ Agora sim, checagem de duplicidade com dados já preenchidos
                custom_id = fixed_updates.get("item_custom_id")
                if custom_id and custom_id != item.get("item_custom_id"):
                    duplicate_check = itens_table.query(
                        IndexName="account_id-item_custom_id-index",
                        KeyConditionExpression=Key("account_id").eq(account_id) & Key("item_custom_id").eq(custom_id),
                    )
                    duplicates = duplicate_check.get("Items", [])
                    if any(i["item_id"] != item_id for i in duplicates):
                        flash("Já existe um item com este ID personalizado.", "danger")
                        return render_template(
                            "edit_item.html",
                            item={**item, **fixed_updates, "key_values": {**key_values, **new_values}},
                            all_fields=all_fields,
                            next=next_page,
                            title="Editar item",
                        )

            # Verifica alterações
            # Verifica alterações
            changes = {}
            for k, v in new_values.items():
                if key_values.get(k) != v:
                    # Coletar tudo em um novo key_values completo
                    pass  # nada aqui

            for k, v in fixed_updates.items():
                if item.get(k) != v:
                    changes[k] = v

            # Monta o novo key_values final (se tiver algum update)
            if new_values:
                key_values_updated = {**key_values, **new_values}
                changes["key_values"] = key_values_updated

            if not changes:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # Atualiza item no DynamoDB
            update_expression = []
            expression_values = {}
            for key, value in changes.items():
                update_expression.append(f"{key} = :{key}")
                expression_values[f":{key}"] = value

            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET " + ", ".join(update_expression),
                ExpressionAttributeValues=expression_values,
            )


            # Atualizar transações relacionadas, se marcado
            if request.form.get("update_all_transactions"):
                response = transactions_table.query(
                    IndexName="item_id-index",
                    KeyConditionExpression=Key("item_id").eq(item_id),
                )
                transacoes = response.get("Items", [])

                for tx in transacoes:
                    update_expression = []
                    expression_values = {}
                    expression_names = {}

                    for key, value in changes.items():
                        if key == "key_values" and isinstance(value, dict) and value:
                            # Só adiciona se tiver dados
                            update_expression.append("#ikv = :ikv")
                            expression_values[":ikv"] = value
                            expression_names["#ikv"] = "item_key_values"
                        elif key != "key_values":
                            update_expression.append(f"{key} = :{key}")
                            expression_values[f":{key}"] = value

                    if update_expression:  # Só faz update se houver algo
                        update_kwargs = {
                            "Key": {"transaction_id": tx["transaction_id"]},
                            "UpdateExpression": "SET " + ", ".join(update_expression),
                            "ExpressionAttributeValues": expression_values,
                        }
                        if expression_names:
                            update_kwargs["ExpressionAttributeNames"] = expression_names

                        transactions_table.update_item(**update_kwargs)


            flash("Item atualizado com sucesso.", "success")
            return redirect(next_page)


        # ---------------- GET ----------------
        # Prepara dados para o formulário
        prepared = {}
        for f in all_fields:
            field_id = f["id"]
            if f["fixed"]:
                prepared[field_id] = item.get(field_id, "")
            else:
                prepared[field_id] = key_values.get(field_id, "")

        prepared["item_id"] = item["item_id"]
        prepared["key_values"] = key_values  # ← isso garante que o template poderá usar item.key_values


        origin = next_page.rstrip("/").split("/")[-1]
        origin_status = "available" if origin == "inventory" else "archive"
        title = "Editar item em inventário" if origin_status == "available" else "Editar item em arquivo"

        return render_template(
            "edit_item.html", item=prepared, all_fields=all_fields, next=next_page, title=title,
        )

    ##################################################################################################
    @app.route("/rent", methods=["GET", "POST"])
    def rent():

        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        cliente_vindo_da_query = bool(request.args.get("client_id"))
        item_vindo_da_query = bool(request.args.get("item_id"))

        # Exemplo simplificado
        ordem = []
        if item_vindo_da_query and not cliente_vindo_da_query:
            ordem = ["bloco-item", "bloco-cliente", "bloco-transacao"]
        elif cliente_vindo_da_query and not item_vindo_da_query:
            ordem = ["bloco-cliente", "bloco-item", "bloco-transacao"]
        else:
            ordem = ["bloco-cliente", "bloco-item", "bloco-transacao"]

        current_transaction = get_latest_transaction(user_id, users_table, payment_transactions_table)


        def processar_transacao(account_id, user_id, user_utc, ordem, current_transaction):
            import re
            from decimal import Decimal, InvalidOperation
            import datetime
            import uuid

            # Campos configuráveis de todas as entidades
            # Carrega e filtra campos
            transaction_fields = [
                field for field in get_all_fields(account_id, field_config_table, entity="transaction")
                if field.get("f_type") != "visual"
            ]
            client_fields = get_all_fields(account_id, field_config_table, entity="client")
            item_fields = get_all_fields(account_id, field_config_table, entity="item")

            # Junta tudo (sem deduplicação)
            all_fields = transaction_fields + client_fields + item_fields


            form_data = {}
            transaction_fixed_fields = {}
            key_values = {}

            item_id = request.form.get("item_id")
            client_id = request.form.get("client_id")

            # 🔄 Extrai os dados do formulário
            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")
                value = request.form.get(field_id, "").strip()

                if not value:
                    continue

                if field_type in ["cpf", "cnpj", "phone"]:
                    value = re.sub(r"\D", "", value)
                elif field_type in ["item_value", "value", "transaction_value_paid", "transaction_price"]:
                    value = value.replace(".", "").replace(",", ".")
                    try:
                        value = Decimal(value)
                    except InvalidOperation:
                        flash(f"Valor inválido no campo {field.get('label', field_id)}.", "error")

                        return render_template(
                            "rent.html",
                            item={},
                            client={},
                            reserved_ranges=[],
                            all_fields=all_fields,
                            cliente_editavel=True,
                            item_editavel=True,
                            ordem=ordem,
                            current_stripe_transaction=current_transaction,
                            total_relevant_transactions=0,
                            total_itens=0,
                            next = request.args.get("next") or request.referrer or url_for("rent"),

                        )

                form_data[field_id] = value

                # 👇 Lógica para diferenciar campos de transação
                if field_id.startswith("transaction_"):
                    if field.get("fixed"):
                        transaction_fixed_fields[field_id] = value
                    else:
                        # novo padrão: usar key_values diretamente
                        form_data.setdefault("key_values", {})[field_id] = value


            # 📅 Validação do período
            try:
                rental_str, return_str = request.form.get("transaction_period", "").split(" - ")
                rental_date = datetime.datetime.strptime(rental_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
                return_date = datetime.datetime.strptime(return_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                flash("Formato de data inválido. Use DD/MM/AAAA.", "danger")
                return render_template("rent.html", item={}, client={}, reserved_ranges=[], all_fields=all_fields, cliente_editavel=True, item_editavel=True, ordem=ordem)

            # 👤 Criar ou atualizar cliente
            if not client_id:
                client_id = str(uuid.uuid4().hex[:12])
                clients_table.put_item(
                    Item={
                        "client_id": client_id,
                        "account_id": account_id,
                        "client_name": request.form.get("client_name", "").strip(),
                        "created_at": datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S"),
                        **{k: v for k, v in form_data.items() if k.startswith("client_")},
                    }
                )
            else:
                update_data = {k: v for k, v in form_data.items() if k.startswith("client_")}
                if update_data:
                    clients_table.update_item(
                        Key={"client_id": client_id},
                        UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in update_data),
                        ExpressionAttributeNames={f"#{k}": k for k in update_data},
                        ExpressionAttributeValues={f":{k}": v for k, v in update_data.items()},
                    )

            # 📦 Criar ou atualizar item
            if not item_id or item_id == "new":
                item_id = str(uuid.uuid4().hex[:12])
                item = {
                    "item_id": item_id,
                    "account_id": account_id,
                    "status": "available",
                    "created_at": datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S"),
                    **{k: v for k, v in form_data.items() if k.startswith("item_")},
                }
                itens_table.put_item(Item=item)
            else:
                update_data = {k: v for k, v in form_data.items() if k.startswith("item_")}
                if update_data:
                    itens_table.update_item(
                        Key={"item_id": item_id},
                        UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in update_data),
                        ExpressionAttributeNames={f"#{k}": k for k in update_data},
                        ExpressionAttributeValues={f":{k}": v for k, v in update_data.items()},
                    )
                response = itens_table.get_item(Key={"item_id": item_id})
                item = response.get("Item") or {}

            # 📥 Carregar cliente para snapshot
            response = clients_table.get_item(Key={"client_id": client_id})
            client = response.get("Item") or {}

            # 🧾 Montar transação
            transaction_id = str(uuid.uuid4().hex[:12])
            transaction_item = {
                "transaction_id": transaction_id,
                "account_id": account_id,
                "item_id": item_id,
                "client_id": client_id,
                "rental_date": rental_date,
                "return_date": return_date,
                "created_at": datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S"),
                **transaction_fixed_fields,
                "key_values": key_values,
                "client_key_values": client.get("key_values", {}),
                "item_key_values": item.get("key_values", {}),
            }

            # Snapshot dos campos fixos de cliente/item
            transaction_item.update({k: v for k, v in client.items() if k not in ["key_values", "created_at"]})
            transaction_item.update({k: v for k, v in item.items() if k not in ["key_values", "created_at"]})

            # 🔐 Salvar
            try:
                transactions_table.put_item(Item=transaction_item)
                if transaction_item.get("transaction_status") == "reserved":
                    flash("Item <a href='/reserved'>reservado</a> com sucesso!", "success")
                else:
                    flash("Item <a href='/rented'>retirado</a> com sucesso!", "success")
                return redirect(url_for("all_transactions"))
            except Exception as e:
                flash("Erro ao salvar transação. Tente novamente.", "danger")
                print("Erro ao salvar transação:", e)
                return render_template("rent.html", item=item, client=client, reserved_ranges=[], all_fields=all_fields, cliente_editavel=True, item_editavel=True, ordem=ordem, next = request.args.get("next") or request.referrer or url_for("rent"))

                ################################################################################################################

        if request.method == "POST":
            return processar_transacao(account_id, user_id, user_utc, ordem, current_transaction)



        # --- GET: renderiza a tela de nova transação ---
        item_id = request.args.get("item_id")
        client_id = request.args.get("client_id")

        client = {}
        item = {}
        reserved_ranges = []

        if item_id:
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item") or {}

            # Buscar períodos reservados
            response = transactions_table.query(
                IndexName="item_id-index",
                KeyConditionExpression=Key("item_id").eq(item_id),
            )
            transaction = response.get("Items", [])
            reserved_ranges = [
                [tx["rental_date"], tx["return_date"]]
                for tx in transaction
                if tx.get("transaction_status") in ["reserved", "rented"]
                and tx.get("rental_date") and tx.get("return_date")
            ]

        if client_id:
            response = clients_table.get_item(Key={"client_id": client_id})
            client = response.get("Item") or {}

        test = get_all_fields(account_id, field_config_table, entity="transaction")
        print("uuuuuuuu")
        print(test)
        print("xxxxxxxx")


        # Carrega todos os campos das três entidades, exceto os campos de visualização de transaction
        transaction_fields = [
            field for field in get_all_fields(account_id, field_config_table, entity="transaction")
            if field.get("f_type") != "visual"
        ]
        client_fields = get_all_fields(account_id, field_config_table, entity="client")
        item_fields = get_all_fields(account_id, field_config_table, entity="item")

        # Junta tudo (sem deduplicação)
        all_fields = transaction_fields + client_fields + item_fields

        print(all_fields)


        # Totais para controle de plano
        total_relevant_transactions = 0
        exclusive_start_key = None
        while True:
            query_params = {
                "IndexName": "account_id-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "FilterExpression": Attr("transaction_status").is_in(["rented", "reserved"]),
                "Select": "COUNT",
            }
            if exclusive_start_key:
                query_params["ExclusiveStartKey"] = exclusive_start_key
            response = transactions_table.query(**query_params)
            total_relevant_transactions += response.get("Count", 0)
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        total_itens = 0
        exclusive_start_key = None
        while True:
            query_kwargs = {
                "IndexName": "account_id-created_at-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "FilterExpression": Attr("status").is_in(["available", "archive"]),
                "Select": "COUNT",
            }
            if exclusive_start_key:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = itens_table.query(**query_kwargs)
            total_itens += response.get("Count", 0)
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break


        return render_template(
            "rent.html",
            item=item,
            client=client,
            reserved_ranges=reserved_ranges,
            all_fields=all_fields,
            item_editavel=not bool(item_id),
            client_editavel=not bool(client_id),
            current_stripe_transaction=current_transaction,
            total_relevant_transactions=total_relevant_transactions,
            total_itens=total_itens,
            cliente_vindo_da_query=cliente_vindo_da_query,
            item_vindo_da_query=item_vindo_da_query,
            next = request.args.get("next") or request.referrer or url_for("rent"),
            ordem=ordem,
        )

    ###########################################################################################################
    @app.route("/view_calendar/<item_id>")
    def view_calendar(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next") or url_for("inventory")

        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado ou já deletado.", "danger")
            return redirect(request.referrer or url_for("inventory"))

        response = transactions_table.query(
            IndexName="item_id-index",
            KeyConditionExpression="item_id = :item_id_val",
            ExpressionAttributeValues={":item_id_val": item_id},
        )

        transactions = response.get("Items", [])
        reserved_ranges = []

        for tx in transactions:
            if (
                tx.get("transaction_status") in ["reserved", "rented"]
                and tx.get("rental_date")
                and tx.get("return_date")
            ):
                reserved_ranges.append([tx["rental_date"], tx["return_date"]])

        return render_template(
            "view_calendar.html",
            item=item,
            reserved_ranges=reserved_ranges,
            next_page=next_page,
        )

    ##########################################################

    @app.route("/delete/<item_id>", methods=["POST"])
    def delete(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        deleted_by = session.get("username")
        next_page = request.args.get("next", url_for("index"))

        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)

        try:
            # Obter o item antes de modificar
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item")

            if item:
                # Obter data e hora atuais no formato brasileiro
                deleted_date = datetime.datetime.now(user_utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                # Atualizar o status do item para "deleted"
                itens_table.update_item(
                    Key={"item_id": item_id},
                    UpdateExpression="SET previous_status = #status, #status = :deleted, deleted_date = :deleted_date, deleted_by = :deleted_by",
                    ExpressionAttributeNames={
                        "#status": "status"  # Alias para evitar conflito com palavra reservada
                    },
                    ExpressionAttributeValues={
                        ":deleted": "deleted",
                        ":deleted_date": deleted_date,
                        ":deleted_by": deleted_by,
                    },
                )

                flash(
                    "Item marcado como deletado!",
                    "success",
                )

            else:
                flash("Item não encontrado.", "danger")

        except Exception as e:
            flash(f"Ocorreu um erro ao tentar deletar o item: {str(e)}", "danger")

        return redirect(next_page)

    @app.route("/purge_deleted_items", methods=["GET", "POST"])
    def purge_deleted_items():
        if not session.get("logged_in"):
            return jsonify({"error": "Acesso não autorizado"}), 403

        try:
            # Obter a data atual e calcular o limite de 30 dias atrás
            hoje = datetime.datetime.now()
            limite_data = hoje - datetime.timedelta(days=30)

            # Buscar todos os itens com status "deleted"
            response = itens_table.scan(
                FilterExpression="#status = :deleted",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":deleted": "deleted"},
            )

            itens_deletados = response.get("Items", [])
            total_itens_removidos = 0
            total_imagens_preservadas = 0

            for item in itens_deletados:
                deleted_date_str = item.get("deleted_date")
                user_id = item.get("user_id")  # Chave primária para verificação
                image_url = item.get("image_url")

                if deleted_date_str:
                    try:
                        # Converter string de data para objeto datetime
                        deleted_date = datetime.datetime.strptime(
                            deleted_date_str, "%Y-%m-%d %H:%M:%S"
                        )

                        # Verificar se passou dos 30 dias
                        if deleted_date < limite_data:
                            item_id = item["item_id"]

                            # Se a imagem existe, verificar se ela é usada em outro item ativo
                            deletar_imagem = True
                            if (
                                user_id
                                and image_url
                                and isinstance(image_url, str)
                                and image_url.strip()
                            ):
                                # Buscar todos os itens com o mesmo user_id
                                response_email = itens_table.scan(
                                    FilterExpression="user_id = :user_id",
                                    ExpressionAttributeValues={":user_id": user_id},
                                )

                                itens_com_mesmo_user_id = response_email.get(
                                    "Items", []
                                )

                                # Verificar se a imagem está em uso por outro item que não está "deleted"
                                for outro_item in itens_com_mesmo_user_id:
                                    if (
                                        outro_item.get("image_url") == image_url
                                        and outro_item.get("status") != "deleted"
                                    ):
                                        deletar_imagem = False
                                        total_imagens_preservadas += 1
                                        break  # Se encontrou um ativo, não precisa verificar mais

                            # Se não houver outro item ativo usando a mesma imagem, deletar do S3
                            if (
                                deletar_imagem
                                and isinstance(image_url, str)
                                and image_url.strip()
                            ):
                                parsed_url = urlparse(image_url)
                                object_key = parsed_url.path.lstrip("/")
                                s3.delete_object(Bucket=s3_bucket_name, Key=object_key)

                            # Remover o item do DynamoDB
                            itens_table.delete_item(Key={"item_id": item_id})
                            total_itens_removidos += 1

                    except ValueError:
                        print(
                            f"Erro ao converter a data de exclusão: {deleted_date_str}"
                        )

            return jsonify(
                {
                    "message": f"{total_itens_removidos} itens foram excluídos definitivamente.",
                    "imagens_preservadas": total_imagens_preservadas,
                }
            )

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    ##################################################################################################################
    @app.route("/restore_version_item", methods=["POST"])
    def restore_version_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("trash_itens"))

        try:
            # 🔹 Pegar os dados do formulário e converter de JSON para dicionário
            item_data = request.form.get("item_data")

            if not item_data:
                flash("Erro: Nenhum dado do item foi recebido.", "danger")
                return redirect(url_for("trash_itens"))

            item = json.loads(item_data)

            item_id = item.get("item_id")
            parent_item_id = item.get("parent_item_id")
            current_status = item.get("status")
            previous_status = item.get("previous_status")

            parent_response = itens_table.get_item(Key={"item_id": parent_item_id})
            parent_data = parent_response.get("Item")

            if not parent_data:
                flash("Item pai não encontrado.", "danger")
                return redirect(next_page)

            # 🔹 Verificar o status do item pai
            parent_status = parent_data.get("status")

            # Se o item pai estiver deletado, restauramos o status
            if parent_status == "deleted":
                itens_table.update_item(
                    Key={"item_id": parent_item_id},
                    UpdateExpression="SET #status = :prev_status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":prev_status": previous_status},
                )

            # 🔹 Trocar todos os campos, exceto item_id, previous_status e status
            swap_fields = [
                field
                for field in item.keys()
                if field
                not in {"item_id", "previous_status", "status", "parent_item_id"}
            ]

            update_expression_parent = []
            update_expression_version = []
            expression_values_parent = {}
            expression_values_version = {}

            for field in swap_fields:
                update_expression_parent.append(f"{field} = :v_{field}")
                update_expression_version.append(f"{field} = :p_{field}")

                expression_values_parent[f":v_{field}"] = item.get(field, "")
                expression_values_version[f":p_{field}"] = parent_data.get(field, "")
            # 🔹 Atualizar o item pai com os valores do item versão
            itens_table.update_item(
                Key={"item_id": parent_item_id},
                UpdateExpression="SET " + ", ".join(update_expression_parent),
                ExpressionAttributeValues=expression_values_parent,
            )

            # 🔹 Atualizar o item versão com os valores do item pai
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET " + ", ".join(update_expression_version),
                ExpressionAttributeValues=expression_values_version,
            )

            previous_status = (
                "inventory" if previous_status == "available" else previous_status
            )

            status_map = {
                "rented": "Retirados",
                "returned": "Devolvidos",
                "historic": "Histórico",
                "inventory": "Inventário",
                "archive": "Arquivados",
            }

            flash(
                f"Item restaurado para <a href='{previous_status}'>{status_map[previous_status]}</a>.",
                "success",
            )

            return redirect(next_page)

        except Exception as e:
            flash(f"Erro ao restaurar a versão do item: {str(e)}", "danger")
            return redirect(next_page)

    ###################################################################################################
    @app.route("/restore_deleted_item", methods=["POST"])
    def restore_deleted_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            item_data = request.form.get("item_data")

            if not item_data:
                flash("Erro: Nenhum dado do item foi recebido.", "danger")
                return redirect(url_for("trash_itens"))

            item = json.loads(item_data)

            item_id = item.get("item_id")
            previous_status = item.get("previous_status")

            # 🔹 Atualizar o status do item no banco
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET #status = :previous_status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":previous_status": previous_status},
            )

            # flash(f"Item {item_id} restaurado para {previous_status}.", "success")
            # Dicionário para mapear os valores a nomes associados
            previous_status = (
                "inventory" if previous_status == "available" else previous_status
            )

            status_map = {
                "rented": "Retirados",
                "returned": "Devolvidos",
                "historic": "Histórico",
                "inventory": "Inventário",
                "archive": "Arquivados",
            }

            flash(
                f"Item restaurado para <a href='{previous_status}'>{status_map[previous_status]}</a>.",
                "success",
            )
            return redirect(url_for("trash_itens"))

        except Exception as e:
            flash(f"Erro ao restaurar item: {str(e)}", "danger")
            return redirect(url_for("trash_itens"))

    ############################################################################################################S

    @app.route("/restore_deleted_transaction", methods=["POST"])
    def restore_deleted_transaction():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            transaction_data = request.form.get("transaction_data")

            if not transaction_data:
                flash("Erro: Nenhum dado do item foi recebido.", "danger")
                return redirect(url_for("trash_transactions"))

            transaction = json.loads(transaction_data)

            transaction_id = transaction.get("transaction_id")
            transaction_previous_status = transaction.get("transaction_previous_status")

            # 🔹 Atualizar o status do item no banco
            transactions_table.update_item(
                Key={"transaction_id": transaction_id},
                UpdateExpression="SET #transaction_status = :transaction_previous_status",
                ExpressionAttributeNames={"#transaction_status": "transaction_status"},
                ExpressionAttributeValues={
                    ":transaction_previous_status": transaction_previous_status
                },
            )

            status_map = {
                "rented": "Retirados",
                "returned": "Devolvidos",
            }

            flash(
                f"Transação restaurada para <a href='{transaction_previous_status}'>{status_map[transaction_previous_status]}</a>.",
                "success",
            )
            return redirect(url_for("trash_transactions"))

        except Exception as e:
            flash(f"Erro ao restaurar transaction: {str(e)}", "danger")
            return redirect(url_for("trash_transactions"))

    @app.route("/restore_version_transaction", methods=["POST"])
    def restore_version_transaction():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("trash_transactions"))

        try:
            # 🔹 Pegar os dados do formulário e converter de JSON para dicionário
            transaction_data = request.form.get("item_data")

            if not transaction_data:
                flash("Erro: Nenhum dado do item foi recebido.", "danger")
                return redirect(url_for("trash_transactions"))

            transaction_data = json.loads(transaction_data)

            # pega os dados originais da transaçao no banco, uma vez que os dados recebidos pelo form são misturados com iten_data
            transaction_id = transaction_data.get("transaction_id")
            transaction_response = transactions_table.get_item(
                Key={"transaction_id": transaction_id}
            )
            transaction_data = transaction_response.get("Item")

            parent_transaction_id = transaction_data.get("parent_transaction_id")
            previous_transaction_status = transaction_data.get(
                "transaction_previous_status"
            )

            parent_response = transactions_table.get_item(
                Key={"transaction_id": parent_transaction_id}
            )
            parent_data = parent_response.get("Item")

            if not parent_data:
                flash("Transação pai não encontrada.", "danger")
                return redirect(next_page)

            # 🔹 Verificar o status do item pai
            parent_status = parent_data.get("transaction_status")

            # Se o item pai estiver deletado, restauramos o status
            if parent_status == "deleted":
                print("Transaçao pai estava deletada. Restaurando...")
                transactions_table.update_item(
                    Key={"transaction_id": parent_transaction_id},
                    UpdateExpression="SET #transaction_status = :prev_status",
                    ExpressionAttributeNames={
                        "#transaction_status": "transaction_status"
                    },
                    ExpressionAttributeValues={
                        ":prev_status": previous_transaction_status
                    },
                )

            # 🔹 Passo 1: Definir os campos que podem ser trocados
            allowed_fields = {
                "valor",
                "item_custom_id",
                "client_email",
                "client_name",
                "client_tel",
                "edited_by",
                "item_obs",
                "rental_date",
                "return_date",
                "client_address",
                "client_tel",
                "client_cpf",
                "image_url",
                "description",
                "rental_date_formatted",
                "rental_date_obj",
                "return_date_formatted",
                "return_date_obj",
                "dev_date_formatted",
            }  # Pode crescer no futuro

            # 🔹 Passo 2: Criar dicionários contendo APENAS os campos que serão trocados
            parent_filtered = {
                key: parent_data[key] for key in allowed_fields if key in parent_data
            }
            transaction_filtered = {
                key: transaction_data[key]
                for key in allowed_fields
                if key in transaction_data
            }

            # 🔹 Função para atualizar um item no banco de dados
            def update_transaction(transaction_id, new_values):
                update_expression = "SET " + ", ".join(
                    f"{k} = :{k}" for k in new_values.keys()
                )
                expression_values = {f":{k}": v for k, v in new_values.items()}

                transactions_table.update_item(
                    Key={"transaction_id": transaction_id},  # Mantemos a chave primária
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values,
                )

            # 🔹 Passo 4: Atualizar os registros no banco, invertendo os valores
            update_transaction(
                parent_transaction_id, transaction_filtered
            )  # Parent recebe valores de transaction
            update_transaction(
                transaction_id, parent_filtered
            )  # Transaction recebe valores de parent

            # 🔹 Passo 5: Verificar se os dados foram realmente trocados
            updated_parent = transactions_table.get_item(
                Key={"transaction_id": parent_transaction_id}
            ).get("Item", {})
            updated_transaction = transactions_table.get_item(
                Key={"transaction_id": transaction_id}
            ).get("Item", {})

            print("✅ Após a troca de valores:")
            print(f"Parent atualizado: {updated_parent}")
            print(f"Transaction atualizado: {updated_transaction}")

            # 🔹 Passo 6: Verificar se os novos registros foram criados corretamente
            print(
                "Novo parent_item inserido:",
                transactions_table.get_item(
                    Key={"transaction_id": f"new_{parent_transaction_id}"}
                ),
            )
            print(
                "Novo transaction_item inserido:",
                transactions_table.get_item(
                    Key={"transaction_id": f"new_{transaction_id}"}
                ),
            )

            print("✅ Registros trocados com sucesso, mantendo os campos protegidos!")

            status_map = {
                "rented": "Retirados",
                "returned": "Devolvidos",
            }

            flash(
                f"Item restaurado para <a href='{previous_transaction_status}'>{status_map[previous_transaction_status]}</a>.",  # status_map[previous_status]
                "success",
            )

            return redirect(next_page)

        except Exception as e:
            flash(f"Erro ao restaurar a versão do item: {str(e)}", "danger")
            return redirect(next_page)

    @app.route("/reports", methods=["GET", "POST"])
    def reports():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        if not account_id:
            print("Erro: Usuário não autenticado corretamente.")
            return redirect(url_for("login"))

        # 🔥 Carrega configuração de campos
        fields_all_entities = {}
        for ent in ["item", "client", "transaction"]:
            response = field_config_table.get_item(Key={"account_id": account_id, "entity": ent})
            config_map = response.get("Item", {}).get("fields_config", {})

            fields = []
            for field_id, cfg in config_map.items():
                fields.append({
                    "id": field_id,
                    "label": cfg.get("label", field_id.replace("_", " ").capitalize()),
                    "type": cfg.get("type"),
                    "f_type": cfg.get("f_type"),
                    "preview": cfg.get("preview", False),
                    "visible": cfg.get("visible", True),
                    "required": cfg.get("required", False),
                    "order_sequence": cfg.get("order_sequence", 0),
                    "filterable": cfg.get("filterable", False),
                    "options": cfg.get("options", []),
                    "fixed": cfg.get("f_type", "") == "fixed",  # útil para lógica de filtros
                })

            # Ordena a lista da entidade atual
            fields = sorted(fields, key=lambda x: x["order_sequence"])

            # Associa a lista ao nome da entidade (ESSA LINHA PRECISA FICAR AQUI)
            fields_all_entities[ent] = fields


        # Coletar todos os IDs já usados em client e item
        ids_client_item = {
            field["id"]
            for field in fields_all_entities.get("client", [])
            + fields_all_entities.get("item", [])
        }

        # Filtrar transaction para remover duplicados com client/item
        filtered_transaction = [
            field for field in fields_all_entities.get("transaction", [])
            if field["id"] not in ids_client_item
        ]

        # Atualizar o dicionário
        fields_all_entities["transaction"] = filtered_transaction


        seen_ids = set()
        fields_config = []

        # Ordem de prioridade: transaction > client > item
        for entity in ["transaction", "client", "item"]:
            for field in fields_all_entities.get(entity, []):
                if field["id"] not in seen_ids:
                    fields_config.append(field)
                    seen_ids.add(field["id"])


        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)
        username = session.get("username", None)

        stats = {}
        # aqui vamos pegar ps totais, sem filtro de data.
        # Contadores iniciais
        stats["total_items_available"] = 0
        stats["total_items_archived"] = 0

        # Lista de status que queremos contar
        status_targets = {
            "available": "total_items_available",
            "archive": "total_items_archived",
        }

        # Loop por cada status
        for status_value, stats_key in status_targets.items():
            last_evaluated_key = None

            while True:
                query_params = {
                    "IndexName": "account_id-status-index",
                    "KeyConditionExpression": Key("account_id").eq(account_id)
                    & Key("status").eq(status_value),
                    "Select": "COUNT",
                }

                if last_evaluated_key:
                    query_params["ExclusiveStartKey"] = last_evaluated_key

                response = itens_table.query(**query_params)
                stats[stats_key] += response.get("Count", 0)

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

        stats["total_clients"] = 0
        last_evaluated_key = None

        while True:
            query_params = {
                "IndexName": "account_id-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "Select": "COUNT",
            }

            if last_evaluated_key:
                query_params["ExclusiveStartKey"] = last_evaluated_key

            response = clients_table.query(**query_params)
            stats["total_clients"] += response.get("Count", 0)

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        def count_transactions_by_status(account_id, status):
            total = 0
            last_evaluated_key = None

            while True:
                query_params = {
                    "IndexName": "account_id-transaction_status-index",
                    "KeyConditionExpression": Key("account_id").eq(account_id)
                    & Key("transaction_status").eq(status),
                    "Select": "COUNT",
                }

                if last_evaluated_key:
                    query_params["ExclusiveStartKey"] = last_evaluated_key

                response = transactions_table.query(**query_params)
                total += response.get("Count", 0)

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            return total

        # 🔢 Aplicando para cada status
        stats["total_rented"] = count_transactions_by_status(account_id, "rented")
        stats["total_returned"] = count_transactions_by_status(account_id, "returned")
        stats["total_reserved"] = count_transactions_by_status(account_id, "reserved")

        end_date = datetime.datetime.now(user_utc).date()
        start_date = end_date - datetime.timedelta(days=30)

        # conta novos clientes dentro de um tiemframe
        num_new_clients = 0
        last_evaluated_key = None

        while True:
            query_params = {
                "IndexName": "account_id-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "ProjectionExpression": "created_at",
            }

            if last_evaluated_key:
                query_params["ExclusiveStartKey"] = last_evaluated_key

            response = users_table.query(**query_params)
            clients_batch = response.get("Items", [])

            for client in clients_batch:
                try:
                    created_at_str = client.get("created_at")
                    if created_at_str:
                        created_at = datetime.datetime.fromisoformat(
                            created_at_str
                        ).date()
                        if start_date <= created_at <= end_date:
                            num_new_clients += 1  # ⚠️ só conta, não armazena
                except Exception as e:
                    print("Erro ao processar created_at:", e)

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        image_url_filter = request.args.get("item_image_url") or None
        image_url_required = image_url_filter.lower() == "true" if image_url_filter is not None else None

        # 📥 Recolhe filtros do formulário POST
        filtros = request.args.to_dict()

        # 🔄 Coleta transações com status relevante
        transactions = []
        last_evaluated_key = None
        while True:
            query_params = {
                "IndexName": "account_id-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "FilterExpression": Attr("transaction_status").is_in(["rented", "returned", "reserved"]),
            }
            if last_evaluated_key:
                query_params["ExclusiveStartKey"] = last_evaluated_key
            response = transactions_table.query(**query_params)
            transactions.extend(response.get("Items", []))
            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        # 🔢 Inicializa contadores
        from collections import defaultdict

        total_paid = 0
        total_due = 0
        num_transactions = 0
        sum_valor = 0
        item_counter = {}
        status_counter = {"rented": 0, "returned": 0, "reserved": 0}
        event_counts = defaultdict(lambda: {"created": 0, "devolvido": 0, "retirado": 0, "transaction_value_paid": 0})

        # 🧠 Filtragem por data + filtros extras
        filtered_transactions = []
        for transaction in transactions:
            try:
                transaction_date = datetime.datetime.strptime(
                    transaction.get("created_at", ""), "%Y-%m-%d %H:%M:%S"
                ).date()

                if not (start_date <= transaction_date <= end_date):
                    continue
            except Exception:
                continue

            if not entidade_atende_filtros_dinamico(transaction, filtros, fields_config, image_url_required):
                continue

            filtered_transactions.append(transaction)

            # 📊 Cálculos principais
            num_transactions += 1
            valor = float(transaction.get("transaction_price", 0))
            pagamento = float(transaction.get("transaction_value_paid", 0))
            total_paid += pagamento
            total_due += max(0, valor - pagamento)
            sum_valor += valor

            status_counter["reserved"] += 1  # todas criadas são consideradas reservadas

            item_id = transaction.get("item_id")
            if item_id:
                item_counter[item_id] = item_counter.get(item_id, 0) + 1

            event_counts[transaction_date]["created"] += 1
            if pagamento > 0:
                event_counts[transaction_date]["transaction_value_paid"] += pagamento

            if transaction.get("dev_date"):
                try:
                    dev_date = datetime.datetime.strptime(transaction["dev_date"], "%Y-%m-%d").date()
                    if start_date <= dev_date <= end_date:
                        status_counter["returned"] += 1
                        event_counts[dev_date]["devolvido"] += 1
                except:
                    pass

            if transaction.get("transaction_ret_date"):
                try:
                    ret_date = datetime.datetime.strptime(transaction["transaction_ret_date"], "%Y-%m-%d").date()
                    if start_date <= ret_date <= end_date:
                        status_counter["rented"] += 1
                        event_counts[ret_date]["retirado"] += 1
                except:
                    pass

        # 🧮 Resultados finais
        preco_medio = sum_valor / num_transactions if num_transactions else 0
        total_general = total_paid + total_due

        # 📅 Dados diários para gráfico
        date_labels = []
        created_list = []
        dev_list = []
        ret_list = []
        pagamento_list = []

        current_date = start_date
        while current_date <= end_date:
            daily = event_counts[current_date]
            date_labels.append(current_date.isoformat())
            created_list.append(daily["created"])
            dev_list.append(daily["devolvido"])
            ret_list.append(daily["retirado"])
            pagamento_list.append(daily["transaction_value_paid"])
            current_date += datetime.timedelta(days=1)

        # ✔️ Feedback
        if request.method == "POST":
            flash("Relatório atualizado com sucesso!", "success")

        # 🔄 Total de transações ativas (rented + reserved)
        current_stripe_transaction = get_latest_transaction(user_id, users_table, payment_transactions_table)
        total_relevant_transactions = 0
        exclusive_start_key = None
        try:
            while True:
                query_params = {
                    "IndexName": "account_id-index",
                    "KeyConditionExpression": Key("account_id").eq(account_id),
                    "FilterExpression": Attr("transaction_status").is_in(["rented", "reserved"]),
                    "Select": "COUNT",
                }
                if exclusive_start_key:
                    query_params["ExclusiveStartKey"] = exclusive_start_key
                response = transactions_table.query(**query_params)
                total_relevant_transactions += response.get("Count", 0)
                exclusive_start_key = response.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break
        except Exception as e:
            print(f"Erro ao contar transações ativas: {e}")

        # 🔄 Total de itens ativos (available + archive)
        total_itens = 0
        exclusive_start_key = None
        try:
            while True:
                query_kwargs = {
                    "IndexName": "account_id-created_at-index",
                    "KeyConditionExpression": Key("account_id").eq(account_id),
                    "FilterExpression": Attr("status").is_in(["available", "archive"]),
                    "Select": "COUNT",
                }
                if exclusive_start_key:
                    query_kwargs["ExclusiveStartKey"] = exclusive_start_key
                response = itens_table.query(**query_kwargs)
                total_itens += response.get("Count", 0)
                exclusive_start_key = response.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break
        except Exception as e:
            print(f"Erro ao contar itens ativos: {e}")

        return render_template(
            "reports.html",
            total_paid=total_paid,
            total_due=total_due,
            total_general=total_general,
            start_date=start_date,
            end_date=end_date,
            num_transactions=num_transactions,
            preco_medio=preco_medio,
            num_new_clients=num_new_clients,
            status_counter=status_counter,
            item_counter=item_counter,
            stats=stats,
            username=username,
            date_labels=date_labels,
            created_list=created_list,
            dev_list=dev_list,
            ret_list=ret_list,
            pagamento_list=pagamento_list,
            total_relevant_transactions=total_relevant_transactions,
            total_itens=total_itens,
            current_transaction=current_stripe_transaction,
            fields_all_entities=fields_all_entities,

        )

    @app.route("/query", methods=["POST"])
    def query_database():
        """Consulta genérica no DynamoDB para uso em AJAX"""
        key_name = request.json.get("key")  # Nome do campo a ser buscado
        key_value = request.json.get("value")  # Valor do campo a ser buscado
        key_type = request.json.get("type")  # Valor do campo a ser buscado

        # 🔹 Buscar o item no banco de dados para obter o previous_status
        # 🔹 Dicionário para mapear nomes de bancos para tabelas
        db_tables = {
            "itens_table": itens_table,
            "transactions_table": transactions_table,
        }
        # 🔹 Pegar a tabela do banco dinamicamente
        db_name = request.json.get("db_name")
        db_table = db_tables.get(db_name)

        response = db_table.get_item(Key={key_name: key_value})
        item_data = response.get("Item")

        if not item_data:
            return (
                jsonify(
                    {
                        "status": "not_found",
                        "message": f"Nenhum item encontrado para {key_name} = {key_value}",
                    }
                ),
                404,
            )

        return jsonify({"status": "found", "data": item_data})

    @app.route("/ver-item/<item_id>")
    def ver_item_publico(item_id):
        try:
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item")

            if item:
                return render_template("view_public_item.html", item=item)
            else:
                return "Item não encontrado.", 404

        except Exception as e:
            print(f"Erro ao buscar item público: {str(e)}")
            return "Erro interno ao tentar carregar o item.", 500

    from flask import request, session, redirect, url_for, render_template, flash
    from boto3.dynamodb.conditions import Key
    import uuid

    # Assuma que as tabelas foram inicializadas antes:
    # custom_fields_table = boto3.resource('dynamodb').Table('custom_fields_table')
    # field_config_table = boto3.resource('dynamodb').Table('field_config_table')

    DEFAULT_FIELDS = {
        "item": [
            {
                "id": "nome",
                "label": "Nome do item",
                "type": "string",
                "order_sequence": 0,
            },
            {"id": "valor", "label": "Valor", "type": "number", "order_sequence": 1},
            {
                "id": "descricao",
                "label": "Descrição",
                "type": "string",
                "order_sequence": 2,
            },
            {
                "id": "data_compra",
                "label": "Data de compra",
                "type": "date",
                "order_sequence": 3,
            },
        ]
    }

    @app.route("/custom_fields/<entity>", methods=["GET", "POST"])
    def custom_fields(entity):
        account_id = session.get("account_id")
        if not account_id:
            flash("Sessão expirada. Faça login novamente.", "danger")
            return redirect(url_for("login"))

        table_mapping = {
            "client": clients_table,
            "transaction": transactions_table,
            "item": itens_table,
        }

        data_table = table_mapping.get(entity)

        if request.method == "POST":
            fields_config_map = {}

            # Campos enviados a partir do template HTML (já existentes, fixed ou custom)
            fields = []
            i = 0
            while f"fields[{i}][id]" in request.form:
                f_type = request.form.get(f"fields[{i}][kind]", "custom")
                if f_type not in ["fixed", "custom", "visual"]:
                    f_type = "custom"

                fields.append(
                    {
                        "field_id": request.form.get(f"fields[{i}][id]"),
"f_type": request.form[f"fields[{i}][kind]"] if f"fields[{i}][kind]" in request.form else "custom",
                        "label": request.form.get(f"fields[{i}][label]"),
                        "label_original": request.form.get(
                            f"fields[{i}][label_original]"
                        ),
                        "title": request.form.get(f"fields[{i}][title]"),
                        "type": request.form.get(f"fields[{i}][type]"),
                        "required": f"fields[{i}][required]" in request.form,
                        "visible": f"fields[{i}][visible]" in request.form,
                        "filterable": f"fields[{i}][filterable]" in request.form,
                        "preview": f"fields[{i}][preview]" in request.form,
                        "options": request.form.get(f"fields[{i}][options]", "").split(
                            ","
                        ),
                        # Adiciona os metadados se forem enviados no form
                        "source_entity": request.form.get(f"fields[{i}][source_entity]", None),
                        "source_field_id": request.form.get(f"fields[{i}][source_field_id]", None),

                    }
                )

                i += 1

            print("#########################")
            print(fields)
            print("#########################")

            # IDs ordenados
            ordered_ids = request.form.get("ordered_ids", "[]")
            try:
                ordered_ids = json.loads(ordered_ids)
            except Exception:
                ordered_ids = []

            # 🛑 Verifica campos custom antigos que ainda existem no banco
            used_custom_fields = set()

            if data_table:
                if entity == "item":
                    # Usa o índice com partition + sort key
                    response = data_table.query(
                        IndexName="account_id-status-index",
                        KeyConditionExpression=Key("account_id").eq(account_id)
                        & Key("status").eq("available"),
                        ProjectionExpression="key_values",
                    )
                elif entity == "client":
                    # Usa GSI apenas com partition key
                    response = data_table.query(
                        IndexName="account_id-index",
                        KeyConditionExpression=Key("account_id").eq(account_id),
                        ProjectionExpression="key_values",
                    )
                elif entity == "transaction":
                    # Exemplo genérico, ajuste conforme o GSI usado na tabela de transações
                    response = data_table.query(
                        IndexName="account_id-index",
                        KeyConditionExpression=Key("account_id").eq(account_id),
                        ProjectionExpression="key_values",
                    )
                else:
                    response = {"Items": []}

                items = response.get("Items", [])

                import re

                for item in items:
                    key_values = item.get("key_values", {})
                    if isinstance(key_values, dict):
                        pattern = re.compile(
                            r"^\d{10,}$"
                        )  # apenas dígitos, com no mínimo 10 caracteres
                        for key, value in key_values.items():
                            if pattern.match(key) and value:
                                used_custom_fields.add(key)

            # ➕ Processa os campos fixos e custom existentes
            for idx, field in enumerate(fields):
                field_id = field["field_id"]

                # Corrige f_type apenas se não for um campo visual
                if field.get("f_type") != "visual" and field_id in [
                    "client_name", "client_phone", "client_cpf", "client_email", "client_address", "client_notes",
                    "item_custom_id", "item_description", "item_value",
                    "transaction_status", "transaction_period", "transaction_price", "transaction_value_paid"
                ]:
                    f_type = "fixed"
                else:
                    f_type = field["f_type"]  # mantém original se for visual


                label = field.get("label", "").strip() or field.get(
                    "label_original", field.get("title", "")
                )
                label_original = field.get("label_original", label)
                type_ = field["type"]
                required = field["required"]
                visible = field["visible"]
                filterable = field["filterable"]
                preview = field["preview"]
                options = [opt.strip() for opt in field["options"] if opt.strip()]
                order = ordered_ids.index(field_id) if field_id in ordered_ids else idx

                fields_config_map[field_id] = {
                    "label": label,
                    "label_original": label_original,
                    "type": type_,
                    "visible": visible,
                    "required": required,
                    "filterable": filterable,
                    "preview": preview,
                    "f_type": f_type,
                    "options": (
                        options if type_ in ["dropdown", "transaction_status"] else []
                    ),
                    "order_sequence": order,
                    "source_entity": field.get("source_entity"),
                    "source_field_id": field.get("source_field_id"),
                }


            # ⬇️ Aqui está o lugar ideal para inserir a checagem
            if f_type == "visual":
                if not field.get("source_entity") or not field.get("source_field_id"):
                    print(f"[Aviso] Campo visual '{field_id}' está sem origem definida.")

            # ➕ Processa os novos campos criados dinamicamente
            combined_ids = request.form.getlist("combined_id[]")
            combined_titles = request.form.getlist("combined_title[]")
            combined_types = request.form.getlist("combined_type[]")
            combined_required = request.form.getlist("combined_required[]")
            combined_visible = request.form.getlist("combined_visible[]")
            combined_filterable = request.form.getlist("combined_filterable[]")
            combined_preview = request.form.getlist("combined_preview[]")
            combined_options = request.form.getlist("combined_options[]")
            combined_kinds = request.form.getlist("combined_kind[]")
            combined_sources_entity = request.form.getlist("combined_source_entity[]")
            combined_sources_field_id = request.form.getlist("combined_source_field_id[]")



            # checa se o usuario esta tentando deletar um campo que ja foi preecnido
            # Lista de IDs de campos que vieram do form
            remaining_field_ids = [f["field_id"] for f in fields] + combined_ids

            print("fcm")
            print(fields_config_map)

            # Descobre quais campos usados foram deletados
            for used_field in used_custom_fields:
                if used_field not in remaining_field_ids:
                    label = fields_config_map.get(used_field, {}).get(
                        "label", used_field
                    )
                    flash(
                        f"Antes de deletar o campo de ID: '{label}' é necessário apagar seu conteúdo em todos os itens ou apagar os itens. Se preferir, apenas desabilite o campo.",
                        "danger",
                    )
                    return redirect(request.url)

            for idx, field_id in enumerate(combined_ids):
                title = combined_titles[idx].strip()
                type_ = combined_types[idx]
                options = [
                    opt.strip()
                    for opt in combined_options[idx].split(",")
                    if opt.strip()
                ]
                order = (
                    ordered_ids.index(field_id)
                    if field_id in ordered_ids
                    else idx + 1000
                )

                fields_config_map[field_id] = {
                    "label": title,
                    "label_original": title,
                    "type": type_,
                    "visible": idx < len(combined_visible)
                    and combined_visible[idx] == "true",
                    "required": idx < len(combined_required)
                    and combined_required[idx] == "true",
                    "filterable": idx < len(combined_filterable)
                    and combined_filterable[idx] == "true",
                    "preview": idx < len(combined_preview)
                    and combined_preview[idx] == "true",
                    "f_type": combined_kinds[idx] if idx < len(combined_kinds) else "custom",
                    "options": options if type_ == "dropdown" else [],
                    "order_sequence": order,
                    "source_entity": combined_sources_entity[idx] if idx < len(combined_sources_entity) else None,
                    "source_field_id": combined_sources_field_id[idx] if idx < len(combined_sources_field_id) else None,

                }

                if fields_config_map[field_id]["f_type"] == "visual":
                    if not fields_config_map[field_id]["source_entity"] or not fields_config_map[field_id]["source_field_id"]:
                        print(f"[Aviso] Campo visual '{field_id}' criado dinamicamente está sem origem definida.")

            # 🔄 Atualiza o item completo na tabela
            field_config_table.put_item(
                Item={
                    "account_id": account_id,
                    "entity": entity,
                    "fields_config": fields_config_map,
                    "updated_at": datetime.datetime.now().isoformat(),
                }
            )

            flash("Campos salvos com sucesso!", "success")
            return redirect(request.url)

        # ----- GET -----
        config_response = field_config_table.get_item(
            Key={"account_id": account_id, "entity": entity}
        )

        fields_config_map = config_response.get("Item", {}).get("fields_config", {})
        fields_to_show = []

        for field_id, cfg in fields_config_map.items():
            fields_to_show.append(
                {
                    "id": field_id,
                    "label": cfg.get("label")
                    or cfg.get("title")
                    or field_id.replace("_", " ").capitalize(),
                    "label_original": cfg.get("label_original", cfg.get("label")),
                    "title": cfg.get("label", field_id.replace("_", " ").capitalize()),
                    "type": cfg.get("type", "string"),
                    "visible": cfg.get("visible", False),
                    "required": cfg.get("required", False),
                    "order_sequence": int(cfg.get("order_sequence", 999)),
                    "filterable": cfg.get("filterable", False),
                    "preview": cfg.get("preview", False),
                    "f_type": cfg.get("f_type", "custom"),
                    "fixed": cfg.get("f_type") == "fixed",
                    "source_entity": cfg.get("source_entity"),
                    "source_field_id": cfg.get("source_field_id"),
                    "options": (
                        cfg.get("options", [])
                        if cfg.get("type") in ["dropdown", "transaction_status"]
                        else []
                    ),
                }
            )

        all_fields = sorted(fields_to_show, key=lambda x: x["order_sequence"])
        return render_template(
            f"custom_fields.html", entity=entity, all_fields=all_fields
        )


import base64


def encode_dynamo_key(key_dict):
    json_str = json.dumps(key_dict)
    return base64.urlsafe_b64encode(json_str.encode()).decode()


def decode_dynamo_key(encoded_str):
    json_str = base64.urlsafe_b64decode(encoded_str.encode()).decode()
    return json.loads(json_str)



def list_transactions(
    status_list,
    template,
    title,
    transactions_table,
    users_table,
    itens_table,
    text_models_table,
    field_config_table,
    client_id= None,
    item_id = None,
    page=None,
    limit=6,
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    user_id = session.get("user_id")
    if not account_id:
        flash("Erro: Usuário não autenticado corretamente.", "danger")
        return redirect(url_for("login"))

    # 🔥 Carrega configuração de campos
    fields_all_entities = {}
    for ent in ["item", "client", "transaction"]:
        response = field_config_table.get_item(Key={"account_id": account_id, "entity": ent})
        config_map = response.get("Item", {}).get("fields_config", {})

        fields = []
        for field_id, cfg in config_map.items():
            fields.append({
                "id": field_id,
                "label": cfg.get("label", field_id.replace("_", " ").capitalize()),
                "type": cfg.get("type"),
                "f_type": cfg.get("f_type"),
                "preview": cfg.get("preview", False),
                "visible": cfg.get("visible", True),
                "required": cfg.get("required", False),
                "order_sequence": cfg.get("order_sequence", 0),
                "filterable": cfg.get("filterable", False),
                "options": cfg.get("options", []),
                "fixed": cfg.get("f_type", "") == "fixed",  # útil para lógica de filtros
            })

        # Ordena a lista da entidade atual
        fields = sorted(fields, key=lambda x: x["order_sequence"])

        # Associa a lista ao nome da entidade (ESSA LINHA PRECISA FICAR AQUI)
        fields_all_entities[ent] = fields

    #cria um lista unica para filtros
    fields_config = (
        fields_all_entities.get("transaction", []) +
        fields_all_entities.get("client", []) +
        fields_all_entities.get("item", [])
    )

    seen_ids = set()
    fields_config_init = []

    for item in fields_config:
        if item['id'] not in seen_ids:
            fields_config_init.append(item)
            seen_ids.add(item['id'])
    fields_config = fields_config_init

    user_utc = get_user_timezone(users_table, user_id)
    today = datetime.datetime.now(user_utc).date()

    def process_dates(item):
        for key in ["rental_date", "return_date", "dev_date"]:
            if key in item:
                date_str = item[key]
                if date_str and isinstance(date_str, str):
                    try:
                        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                        item[f"{key}_formatted"] = date_obj.strftime("%d-%m-%Y")
                        item[f"{key}_obj"] = date_obj
                    except ValueError:
                        pass

        if item.get("dev_date_obj") and item["dev_date_obj"] != "N/A":
            item["overdue"] = False
        elif item.get("return_date_obj") and item["return_date_obj"] != "N/A":
            item["overdue"] = item["return_date_obj"] < today
        else:
            item["overdue"] = False

        rental_date = item.get("rental_date_obj")
        return_date = item.get("return_date_obj")

        if rental_date and item.get("transaction_status") != "rented":
            if rental_date == today:
                item["rental_message"] = "Retirada é hoje"
                item["rental_message_color"] = "orange"
            elif rental_date > today:
                dias = (rental_date - today).days
                item["rental_message"] = "Falta 1 dia" if dias == 1 else f"Faltam {dias} dias"
                item["rental_message_color"] = "blue" if dias > 1 else "yellow"
            else:
                item["rental_message"] = "Não retirado"
                item["rental_message_color"] = "red"
        else:
            item["rental_message"] = ""
            item["rental_message_color"] = ""

        if item.get("overdue") and return_date:
            overdue_days = (today - return_date).days
            item["overdue_days"] = overdue_days if overdue_days > 0 else 0

        return item

    if not item_id:
        item_id = request.args.get("item_id")

    if not client_id:
        client_id = request.args.get("client_id")

    # 🔍 Exibe apenas transações específicas, se item_id ou client_id estiverem definidos
    if item_id or client_id:
        query_kwargs = {
            "IndexName": "account_id-created_at-index",
            "KeyConditionExpression": Key("account_id").eq(account_id),
            "ScanIndexForward": False,
        }

        transacoes = transactions_table.query(**query_kwargs).get("Items", [])

        transacoes_filtradas = []
        for txn in transacoes:
            if status_list and txn.get("transaction_status") not in status_list:
                continue
            if item_id and txn.get("item_id") != item_id:
                continue
            if client_id and txn.get("client_id") != client_id:
                continue
            transacoes_filtradas.append(process_dates(txn))

        return render_template(
            template,
            itens=transacoes_filtradas,
            title=title,
            current_page=1,
            has_next=False,
            has_prev=False,
            itens_count=len(transacoes_filtradas),
            next_cursor=None,
            last_page=True,
            add_route=url_for("trash_transactions"),
            next_url=request.url,
            saved_models=[],
            fields_config=fields_config,
            fields_all_entities=fields_all_entities,
            ns={"filtro_relevante": False},
        )


    force_no_next = request.args.get("force_no_next")

    image_url_filter = request.args.get("item_image_url") or None
    image_url_required = image_url_filter.lower() == "true" if image_url_filter is not None else None

    filtros = request.args.to_dict()

    converter_intervalo_data_br_para_iso(filtros, "rental_period", "start_rental_date", "end_rental_date")
    converter_intervalo_data_br_para_iso(filtros, "return_period", "start_return_date", "end_return_date")


    page = int(filtros.pop("page", 1))

    current_path = request.path
    session["previous_path_transactions"] = current_path

    if page == 1:
        session.pop("current_page_transactions", None)
        session.pop("cursor_pages_transactions", None)
        session.pop("last_page_transactions", None)

    cursor_pages = session.get("cursor_pages_transactions", {"1": None})
    if page == 1:
        session["cursor_pages_transactions"] = {"1": None}
        cursor_pages = {"1": None}

    session["current_page_transactions"] = page

    exclusive_start_key = None
    if str(page) in cursor_pages and cursor_pages[str(page)]:
        exclusive_start_key = decode_dynamo_key(cursor_pages[str(page)])

    valid_itens = []
    batch_size = 10
    last_valid_item = None
    raw_last_evaluated_key = None


    while len(valid_itens) < limit:
        query_kwargs = {
            "IndexName": "account_id-created_at-index",
            "KeyConditionExpression": Key("account_id").eq(account_id),
            "ScanIndexForward": False,
            "Limit": batch_size,
        }

        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = transactions_table.query(**query_kwargs)
        transacoes = response.get("Items", [])
        raw_last_evaluated_key = response.get("LastEvaluatedKey")

        if not transacoes:
            break

        for txn in transacoes:
            if status_list and txn.get("transaction_status") not in status_list:
                continue
            # filtro dinâmico geral
            if not entidade_atende_filtros_dinamico(txn, filtros, fields_config, image_url_required):
                continue


            valid_itens.append(process_dates(txn))
            last_valid_item = txn

            if len(valid_itens) == limit:
                break

        if len(valid_itens) < limit:
            if raw_last_evaluated_key:
                exclusive_start_key = raw_last_evaluated_key
            else:
                break

    next_cursor_token = None
    if last_valid_item:
        next_cursor_token = encode_dynamo_key({
            "account_id": last_valid_item["account_id"],
            "created_at": last_valid_item["created_at"],
            "transaction_id": last_valid_item["transaction_id"],
        })

    if next_cursor_token:
        session["cursor_pages_transactions"][str(page + 1)] = next_cursor_token
    else:
        session["cursor_pages_transactions"].pop(str(page + 1), None)

    response = text_models_table.query(
        IndexName="account_id-index",
        KeyConditionExpression="account_id = :account_id",
        ExpressionAttributeValues={":account_id": account_id},
    )
    saved_models = response.get("Items", [])

    last_page_transactions = session.get("last_page_transactions")
    current_page = session.get("current_page_transactions", 1)

    if force_no_next:
        has_next = False
    else:
        if len(valid_itens) < limit or (last_page_transactions is not None and current_page >= last_page_transactions):
            has_next = False
        else:
            has_next = True

    if not valid_itens and page > 1:
        flash("Não há mais transações para exibir.", "info")
        last_valid_page = page - 1
        session["current_page_transactions"] = last_valid_page
        session["last_page_transactions"] = last_valid_page
        return redirect(url_for("all_transactions", page=last_valid_page, has_next=has_next, force_no_next=1))



    return render_template(
        template,
        itens=valid_itens,
        next_cursor=next_cursor_token,
        last_page=not has_next,
        title=title,
        add_route=url_for("trash_transactions"),
        next_url=request.url,
        saved_models=saved_models,
        current_page=current_page,
        itens_count=len(valid_itens),
        has_next=has_next,
        has_prev=current_page > 1,
        fields_config=fields_config,  # todas as 3 entidades na mesma  lista
        fields_all_entities=fields_all_entities, # todas as e entidades em listas separadas
        ns={"filtro_relevante": False}  # para exibir filtros normalmente

    )



def get_latest_transaction(user_id, users_table, payment_transactions_table):
    """Retorna a transação mais recente e válida do usuário (ou None)."""

    from boto3.dynamodb.conditions import Key

    response = users_table.get_item(Key={"user_id": user_id})
    if "Item" not in response:
        return None

    user = response["Item"]
    stripe_customer_id = user.get("stripe_customer_id")
    if not stripe_customer_id:
        return None

    response = payment_transactions_table.query(
        IndexName="customer_id-index",
        KeyConditionExpression=Key("customer_id").eq(stripe_customer_id),
    )
    transactions = response.get("Items", [])
    transactions.sort(key=lambda x: x.get("updated_at", 0), reverse=True)

    for tx in transactions:
        if tx.get("subscription_status") in [
            "trialing",
            "active",
            "past_due",
            "unpaid",
            "canceled",
            "paused",
        ]:
            return tx

    return None

def get_valor_item(item, field):

    field_id = field["id"]
    if field.get("fixed") == True:
        return item.get(field_id, "")
    else:
        return (item.get("key_values") or {}).get(field_id, "")

from utils import entidade_atende_filtros_dinamico  # certifique-se de importar isso corretamente
from utils import converter_intervalo_data_br_para_iso  # certifique-se de importar isso corretamente


def list_raw_itens(
    status_list,
    template,
    title,
    itens_table,
    transactions_table,
    users_table,
    payment_transactions_table,
    field_config_table,
    limit=6,
    entity="item",
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")
        return redirect(url_for("login"))

    fields_config = get_all_fields(account_id, field_config_table, entity)

    force_no_next = request.args.get("force_no_next")
    current_path = request.path
    session["previous_path_itens"] = current_path

    filtros = request.args.to_dict()
    item_id = filtros.pop("item_id", None)
    page = int(filtros.pop("page", 1))

    image_url_filter = request.args.get("item_image_url") or None
    image_url_required = image_url_filter.lower() == "true" if image_url_filter is not None else None


    if page == 1:
        session.pop("current_page_itens", None)
        session.pop("cursor_pages_itens", None)
        session.pop("last_page_itens", None)

    cursor_pages = session.get("cursor_pages_itens", {"1": None})
    if page == 1:
        session["cursor_pages_itens"] = {"1": None}
        cursor_pages = {"1": None}

    session["current_page_itens"] = page

    if item_id:
        try:
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item")
            if item:
                item_status = item.get("status")
                if item_status == "archive":
                    template = "archive.html"
                elif item_status == "available":
                    template = "inventory.html"
                else:
                    template = "trash_itens.html"

                return render_template(
                    template,
                    itens=[item],
                    page=1,
                    total_pages=1,
                    title=title,
                    add_route=url_for("add_item"),
                    next_url=request.url,
                    itens_count=1,
                    current_page=1,
                    fields_config=fields_config,
                )
            else:
                flash("Item não encontrado ou já deletado.", "warning")
                return redirect(request.referrer or url_for("inventory"))
        except Exception as e:
            print(f"Erro ao buscar item por ID: {e}")
            flash("Erro ao buscar item.", "danger")
            return redirect(request.referrer or url_for("inventory"))

    exclusive_start_key = None
    if str(page) in cursor_pages and cursor_pages[str(page)]:
        exclusive_start_key = decode_dynamo_key(cursor_pages[str(page)])

    valid_itens = []
    batch_size = 10
    last_valid_item = None
    raw_last_evaluated_key = None

    while len(valid_itens) < limit:
        query_kwargs = {
            "IndexName": "account_id-created_at-index",
            "KeyConditionExpression": Key("account_id").eq(account_id),
            "ScanIndexForward": False,
            "Limit": batch_size,
        }

        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = itens_table.query(**query_kwargs)
        itens_batch = response.get("Items", [])
        raw_last_evaluated_key = response.get("LastEvaluatedKey")

        if not itens_batch:
            break

        for item in itens_batch:
            if item.get("status") not in status_list:
                continue

            if entidade_atende_filtros_dinamico(item, filtros, fields_config, image_url_required):
                valid_itens.append(item)
                last_valid_item = item

                if len(valid_itens) == limit:
                    break

        if len(valid_itens) < limit:
            if raw_last_evaluated_key:
                exclusive_start_key = raw_last_evaluated_key
            else:
                break

    next_cursor_token = None
    if last_valid_item:
        next_cursor_token = encode_dynamo_key({
            "account_id": last_valid_item["account_id"],
            "created_at": last_valid_item["created_at"],
            "item_id": last_valid_item["item_id"],
        })

    if next_cursor_token:
        session["cursor_pages_itens"][str(page + 1)] = next_cursor_token
    else:
        session["cursor_pages_itens"].pop(str(page + 1), None)

    last_page_itens = session.get("last_page_itens")
    current_page = session.get("current_page_itens", 1)

    has_next = not (len(valid_itens) < limit or (last_page_itens is not None and current_page >= last_page_itens))
    if force_no_next:
        has_next = False

    if not valid_itens and page > 1:
        flash("Não há mais itens para exibir.", "info")
        last_valid_page = page - 1
        session["current_page_itens"] = last_valid_page
        session["last_page_itens"] = last_valid_page
        return redirect(url_for("inventory", page=last_valid_page, force_no_next=1))

    fields_config = sorted(fields_config, key=lambda x: x["order_sequence"])

    custom_fields_preview = [
        {"field_id": field["id"], "title": field["label"]}
        for field in fields_config
        if field.get("preview") == True
    ]

    return render_template(
        template,
        itens=valid_itens,
        next_cursor=next_cursor_token,
        last_page=not has_next,
        title=title,
        add_route=url_for("add_item"),
        next_url=request.url,
        current_page=current_page,
        fields_config=fields_config,
        custom_fields_preview=custom_fields_preview,
        has_next=has_next,
        has_prev=current_page > 1,
    )





def XXXXXfiltra_transacao(txn, filtros, client_id, status_list):
    # 1. Filtro obrigatório: status da transação
    if txn.get("transaction_status") not in status_list:
        return False

    # 2. Filtro explícito por client_id (se vier da rota, por exemplo)
    if client_id and txn.get("client_id") != client_id:
        return False

    # 3. Filtros de campos de texto (case insensitive)
    campos_texto = [
        ("description", "description"),
        ("item_obs", "item_obs"),
        ("item_custom_id", "item_custom_id"),
        ("item_id", "item_id"),
        ("client_id", "client_id"),
        ("client_name", "client_name"),
        ("client_email", "client_email"),
        ("client_cpf", "client_cpf"),
        ("client_cnpj", "client_cnpj"),
        ("client_address", "client_address"),
        ("client_obs", "client_obs"),
        ("transaction_obs", "transaction_obs"),
        ("transaction_status", "transaction_status"),
    ]

    for campo_filtro, campo_txn in campos_texto:
        filtro_valor = (filtros.get(campo_filtro) or "").strip().lower()
        campo_valor = (txn.get(campo_txn) or "").strip().lower()

        if filtro_valor and filtro_valor not in campo_valor:
            return False

    # 4. Filtro de pagamento (pago total, parcial, não pago)
    pagamento_raw = txn.get("pagamento")
    valor_raw = txn.get("valor")
    try:
        pagamento = float(pagamento_raw or 0)
        valor = float(valor_raw or 0)
    except (TypeError, ValueError):
        return False

    filtro_pagamento = (filtros.get("payment") or "").strip().lower()
    if filtro_pagamento == "pago total" and pagamento < valor:
        return False
    elif filtro_pagamento == "pago parcial" and (pagamento == 0 or pagamento >= valor):
        return False
    elif filtro_pagamento == "não pago" and pagamento > 0:
        return False

    # 🔥 Se passou em tudo
    return True


def parse_date(date_str):
    return datetime.datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None


def apply_filtros_request():
    return {
        "filter": request.args.get("filter", "todos"),
        "item_id": request.args.get("item_id"),
        "client_id": request.args.get("client_id"),
        "item_custom_id": request.args.get("item_custom_id"),
        "description": request.args.get("description"),
        "client_name": request.args.get("client_name"),
        "client_email": request.args.get("client_email"),
        "client_cpf": request.args.get("client_cpf"),
        "client_cnpj": request.args.get("client_cnpj"),
        "client_address": request.args.get("client_address"),
        "client_obs": request.args.get("client_obs"),
        "payment": request.args.get("payment"),
        "start_date": parse_date(request.args.get("start_date")),
        "end_date": parse_date(request.args.get("end_date")),
        "return_start_date": parse_date(request.args.get("return_start_date")),
        "return_end_date": parse_date(request.args.get("return_end_date")),
        "item_obs": request.args.get("item_obs"),
        "transaction_obs": request.args.get("transaction_obs"),
        "transaction_status": request.args.get("transaction_status"),
    }


from boto3.dynamodb.conditions import Key


def get_fields_config(account_id, field_config_table):
    """
    Recupera a configuração dos campos personalizados da conta a partir da tabela DynamoDB.

    Espera que exista um GSI chamado 'account_id-index' na tabela de campos.
    """

    try:
        response = field_config_table.get_item(
            Key={
                "account_id": account_id,
                "entity": "item",
            }  # ← só se tiver sort key "entity"
        )
        item = response.get("Item")
        if not item:
            return []

        fixed_fields = item.get("fixed_fields_config", {})

        # Transforma em lista ordenada
        ordered_fields = sorted(
            [{**{"id": fid}, **cfg} for fid, cfg in fixed_fields.items()],
            key=lambda x: x.get("order_sequence", 0),
        )
        return ordered_fields

    except Exception as e:
        print(f"Erro ao buscar campos configuráveis: {e}")
        return []


def get_all_fields(account_id, field_config_table, entity):

    config_response = field_config_table.get_item(
        Key={"account_id": account_id, "entity": entity}
    )
    fields_config = config_response.get("Item", {}).get("fields_config", {})

    all_fields = []
    for field_id, cfg in fields_config.items():
        label = cfg.get("label", field_id.replace("_", " ").capitalize())

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
                    cfg.get("options", [])
                    if cfg.get("type") in ["dropdown", "transaction_status"]
                    else []
                ),
                "fixed": cfg.get("f_type", "custom") == "fixed",
                "f_type": cfg.get("f_type", "custom"),
            }
        )
    return sorted(all_fields, key=lambda x: x["order_sequence"])
