import datetime
import uuid
from urllib.parse import urlparse, urlencode
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
import schemas

ALLOWED_EXTENSIONS = {"jpeg", "jpg", "png", "gif", "webp"}
MAX_ITEM_IMAGES = 4


def handle_image_upload(image_file):
    if image_file and image_file.filename != "":
        if allowed_file(image_file.filename):
            return upload_image_to_s3(image_file)
    return "N/A"


def allowed_file(filename):
    """Verifica se o arquivo tem uma extensão válida."""
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


from utils import upload_image_to_s3, aplicar_filtro, copy_image_in_s3
import schemas





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
):

    @app.route("/rented")
    def rented():
        return list_transactions(
            ["rented"],
            "rented.html",
            "Itens retirados",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
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
        )

    @app.route("/trash_itens")
    def trash_itens():
        return list_raw_itens(
            ["deleted"],
            "trash_itens.html",
            "Lixeira de itens",
            itens_table,
            transactions_table,
            users_table,
            payment_transactions_table,
        )

    @app.route("/trash_transactions")
    def trash_transactions():
        return list_transactions(
            ["deleted"],
            "trash_transactions.html",
            "Lixeira de transações",
            transactions_table,
            users_table,
            itens_table,
            text_models_table,
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

        def _load_color_pairs_for_account(account_id):
            if not account_id:
                return []
            resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
            item = resp.get("Item") or {}
            colors = item.get("color_options")
            if not isinstance(colors, list):
                return []
            cleaned = []
            seen = set()
            for c in colors:
                if c is None:
                    continue
                if isinstance(c, dict):
                    name = c.get("name") or c.get("color") or c.get("comercial") or c.get("cor")
                    base = c.get("base") or c.get("cor_base") or c.get("base_color")
                else:
                    name = c
                    base = ""
                name = str(name).strip() if name is not None else ""
                base = str(base).strip() if base is not None else ""
                if not name:
                    continue
                key = (base.casefold(), name.casefold())
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"name": name, "base": base})
            return sorted(cleaned, key=lambda x: (x["name"].casefold(), x["base"].casefold()))

        def _load_color_options_for_account(account_id):
            pairs = _load_color_pairs_for_account(account_id)
            cleaned = []
            seen = set()
            for c in pairs:
                name = c.get("name") or ""
                key = name.casefold()
                if not name or key in seen:
                    continue
                seen.add(key)
                cleaned.append(name)
            return sorted(cleaned, key=lambda x: x.casefold())

        def _parse_color_value(raw):
            raw = str(raw or "").strip()
            if not raw:
                return "", ""
            if "||" in raw:
                name, base = raw.split("||", 1)
                return name.strip(), base.strip()
            return raw, ""

        def _default_size_options():
            return ["P", "M", "G", "Extra G"]

        def _load_size_options_for_account(account_id):
            if not account_id:
                return _default_size_options()
            resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
            item = resp.get("Item") or {}
            sizes = item.get("size_options")
            if not isinstance(sizes, list):
                return _default_size_options()
            cleaned = []
            seen = set()
            for s in sizes:
                if s is None:
                    continue
                val = str(s).strip()
                if not val:
                    continue
                k = val.casefold()
                if k in seen:
                    continue
                seen.add(k)
                cleaned.append(val)
            return sorted(cleaned, key=lambda x: x.casefold())

        def _build_ai_inventory_examples(account_id):
            color_pairs = _load_color_pairs_for_account(account_id)
            sizes = _load_size_options_for_account(account_id)
            cor_comercial = [c.get("name") for c in color_pairs if c.get("name")]
            cor_base = [c.get("base") for c in color_pairs if c.get("base")]
            mapa = {c.get("name"): c.get("base") for c in color_pairs if c.get("name")}
            return {
                "opcoes_validas": {
                    "occasions": [
                        "Noiva",
                        "Civil",
                        "Madrinha",
                        "Mãe dos Noivos",
                        "Formatura",
                        "Debutante",
                        "Gala",
                        "Convidada",
                    ],
                    "cor": cor_comercial,
                    "cor_comercial": cor_comercial,
                    "cor_base": cor_base,
                    "mapa_cor_comercial_para_base": mapa,
                    "tamanho": sizes,
                }
            }

        def _build_ai_inventory_examples(account_id):
            color_pairs = _load_color_pairs_for_account(account_id)
            sizes = _load_size_options_for_account(account_id)
            cor_comercial = [c.get("name") for c in color_pairs if c.get("name")]
            cor_base = [c.get("base") for c in color_pairs if c.get("base")]
            mapa = {c.get("name"): c.get("base") for c in color_pairs if c.get("name")}
            return {
                "opcoes_validas": {
                    "occasions": [
                        "Noiva",
                        "Civil",
                        "Madrinha",
                        "Mãe dos Noivos",
                        "Formatura",
                        "Debutante",
                        "Gala",
                        "Convidada",
                    ],
                    "cor": cor_comercial,
                    "cor_comercial": cor_comercial,
                    "cor_base": cor_base,
                    "mapa_cor_comercial_para_base": mapa,
                    "tamanho": sizes,
                }
            }

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




            all_fields = get_all_fields("item")
            color_pairs = _load_color_pairs_for_account(account_id)
            color_options = _load_color_options_for_account(account_id)
            size_options = _load_size_options_for_account(account_id)

            return render_template(
                "add_item.html",
                next=next_page,
                all_fields=all_fields,
                total_itens=total_itens,
                current_stripe_transaction=current_stripe_transaction,
                title=title,
                item={},
                color_options=color_options,
                color_pairs=color_pairs,
                size_options=size_options,
            )
        # -------------------------- POST --------------------------
        if request.method == "POST":
            import json
            from decimal import Decimal, InvalidOperation
            import re

            def _parse_money_to_decimal(raw):
                s = (raw or "").strip()
                if not s:
                    raise InvalidOperation

                s = re.sub(r"\s+", "", s)
                s = re.sub(r"^R\$\s*", "", s, flags=re.IGNORECASE)
                s = re.sub(r"[^\d,.\-]", "", s)
                if not s:
                    raise InvalidOperation

                if "," in s:
                    parts = s.split(",")
                    int_part = "".join(parts[:-1]).replace(".", "")
                    dec_part = re.sub(r"\D", "", (parts[-1] or ""))[:2]
                    dec_part = (dec_part + "00")[:2]
                    return Decimal(f"{int_part or '0'}.{dec_part}")

                if "." in s:
                    dot_parts = s.split(".")
                    if len(dot_parts) == 2 and dot_parts[1] and len(dot_parts[1]) <= 2:
                        int_part = re.sub(r"\D", "", dot_parts[0] or "")
                        dec_part = re.sub(r"\D", "", dot_parts[1] or "")[:2]
                        dec_part = (dec_part + "00")[:2]
                        return Decimal(f"{int_part or '0'}.{dec_part}")

                    digits = re.sub(r"\D", "", s)
                    if not digits:
                        raise InvalidOperation
                    return Decimal(f"{digits}.00")

                digits = re.sub(r"\D", "", s)
                if not digits:
                    raise InvalidOperation
                return Decimal(f"{digits}.00")

            all_fields = schemas.get_schema_fields("item")

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

            image_files = request.files.getlist("image_file")
            image_files = [f for f in (image_files or []) if f and getattr(f, "filename", "")]
            allowed_count = sum(1 for f in image_files if allowed_file(getattr(f, "filename", "")))
            if allowed_count > MAX_ITEM_IMAGES:
                flash(f"Limite de fotos por item: no máximo {MAX_ITEM_IMAGES}.", "danger")
                return redirect(request.url)

            raw_main_index = (request.form.get("main_image_index") or "").strip()
            try:
                main_index = int(raw_main_index) if raw_main_index != "" else 0
            except Exception:
                main_index = 0

            if not image_files:
                flash("É obrigatório enviar uma foto do item.", "danger")
                return redirect(request.url)

            if main_index < 0:
                main_index = 0
            if main_index >= len(image_files):
                main_index = 0

            image_bytes_for_ai = None
            try:
                selected_file = image_files[main_index]
                image_bytes_for_ai = selected_file.read()
                selected_file.seek(0)
            except Exception:
                image_bytes_for_ai = None

            image_urls = [upload_image_to_s3(f) for f in image_files if allowed_file(f.filename)]
            image_urls = [u for u in image_urls if u]
            if not image_urls:
                flash("Formato de arquivo não permitido. Use JPEG, PNG ou WEBP.", "danger")
                return redirect(request.url)
            if len(image_urls) > MAX_ITEM_IMAGES:
                flash(f"Limite de fotos por item: no máximo {MAX_ITEM_IMAGES}.", "danger")
                return redirect(request.url)

            if main_index >= len(image_urls):
                main_index = 0

            main_image_url = image_urls[main_index]
            

            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")
                # is_fixed ignorado pois tudo agora é raiz
                label_text = (field.get("label") or "").strip().lower()
                is_color_field = field_id in ["cor", "color", "item_cor", "item_color"] or label_text == "cor"
                is_size_field = field_id in ["tamanho", "size", "item_tamanho", "item_size"] or label_text == "tamanho"

                if is_size_field:
                    raw_list = request.form.getlist(field_id)
                    cleaned = []
                    seen = set()
                    for v in raw_list:
                        s = str(v).strip()
                        if not s:
                            continue
                        k = s.casefold()
                        if k in seen:
                            continue
                        seen.add(k)
                        cleaned.append(s)
                    value = ", ".join(cleaned)
                else:
                    raw_value = request.form.get(field_id, "").strip()
                    value = raw_value

                if field_id == "item_image_url":
                    value = main_image_url
                elif is_color_field:
                    name, base = _parse_color_value(value)
                    value = name
                    item_data["cor_base"] = base
                    item_data["cor_comercial"] = name

                elif value:
                    # 🔸 Limpa número/monetário
                    if field_type in ["value", "item_value"]:
                        try:
                            value = _parse_money_to_decimal(value)
                        except InvalidOperation:
                            flash(
                                f"O campo {field.get('label') or field.get('title')} possui um número inválido.",
                                "danger",
                            )
                            return redirect(request.url)

                    # 🔸 Limpa CPF/CNPJ/Telefone
                    elif field_type in ["cpf", "cnpj", "phone"]:
                        value = re.sub(r"\D", "", value)

                # Salva tudo na raiz
                item_data[field_id] = value

            item_data["item_image_urls"] = image_urls
            item_data["item_main_image_index"] = main_index

            for slug in ["madrinha","formatura","gala","debutante","convidada","mae_dos_noivos","noiva","civil"]:
                if request.form.get(f"occasion_{slug}") == "on":
                    item_data[f"occasion_{slug}"] = "1"


            # ---------------------------------------------------------
            # Regra de Negócio: ID Automático
            # Se não foi informado, gera um ID curto único (6 chars)
            # ---------------------------------------------------------
            # Gera um ID curto (ex: A1B2C3) se não existir
            if not item_data.get("item_custom_id"):
                item_data["item_custom_id"] = str(uuid.uuid4().hex[:6]).upper()

            # 🔒 Verifica se item_custom_id já existe **ANTES** de salvar
            item_custom_id = item_data.get("item_custom_id")
            if item_custom_id:
                existing = itens_table.query(
                    IndexName="account_id-item_custom_id-index",
                    KeyConditionExpression=Key("account_id").eq(account_id) & Key("item_custom_id").eq(item_custom_id)
                ).get("Items", [])

                # Ignora itens deletados na verificação de duplicidade
                active_existing = [i for i in existing if i.get("status") != "deleted"]

                if active_existing:
                    flash("Já existe um item com esse ID. Por favor, use um ID diferente.", "danger")
                    return render_template(
                        "add_item.html",
                        next=next_page,
                        all_fields=all_fields,
                        current_stripe_transaction=get_latest_transaction(user_id, users_table, payment_transactions_table),
                        title=title,
                        item={**item_data},
                        color_options=_load_color_options_for_account(account_id),
                        color_pairs=_load_color_pairs_for_account(account_id),
                        size_options=_load_size_options_for_account(account_id),
                    )



            # ---------------------------------------------------------
            # Regra de Negócio: Sincronização de Campos Legados (GSI)
            # ---------------------------------------------------------
            if str(item_data.get("description") or "").strip():
                item_data["item_description"] = item_data["description"]
            elif str(item_data.get("item_description") or "").strip():
                item_data["description"] = item_data["item_description"]

            if str(item_data.get("title") or "").strip():
                item_data["item_title"] = item_data["title"]
            elif str(item_data.get("item_title") or "").strip():
                item_data["title"] = item_data["item_title"]

            item_data = {k: v for k, v in item_data.items() if v is not None and v != ""}

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

        fields_transaction = schemas.get_schema_fields("transaction")


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

            updated_values = {}

            try:
                rental_str, return_str = request.form.get("transaction_period", "").split(" - ")
                rental_date = datetime.datetime.strptime(rental_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
                return_date = datetime.datetime.strptime(return_str.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                flash("Formato de data inválido. Use DD/MM/AAAA.", "danger")
                return redirect(request.url)

            updated_values["rental_date"] = rental_date
            updated_values["return_date"] = return_date

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

                updated_values[field_id] = value

            # Monta a expressão de atualização
            update_expr_parts = []
            expr_attr_values = {}
            expr_attr_names = {}

            for key, val in updated_values.items():
                update_expr_parts.append(f"#{key} = :{key}")
                expr_attr_values[f":{key}"] = val
                expr_attr_names[f"#{key}"] = key


            update_args = {
                "Key": {"transaction_id": transaction_id},
                "UpdateExpression": "SET " + ", ".join(update_expr_parts),
                "ExpressionAttributeValues": expr_attr_values,
            }

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

    @app.route("/edit_item/<item_id>", methods=["GET", "POST"])
    def edit_item(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))
        account_id = session.get("account_id")

        def _load_color_pairs_for_account(account_id):
            if not account_id:
                return []
            resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
            item = resp.get("Item") or {}
            colors = item.get("color_options")
            if not isinstance(colors, list):
                return []
            cleaned = []
            seen = set()
            for c in colors:
                if c is None:
                    continue
                if isinstance(c, dict):
                    name = c.get("name") or c.get("color") or c.get("comercial") or c.get("cor")
                    base = c.get("base") or c.get("cor_base") or c.get("base_color")
                else:
                    name = c
                    base = ""
                name = str(name).strip() if name is not None else ""
                base = str(base).strip() if base is not None else ""
                if not name:
                    continue
                key = (base.casefold(), name.casefold())
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"name": name, "base": base})
            return sorted(cleaned, key=lambda x: (x["name"].casefold(), x["base"].casefold()))

        def _load_color_options_for_account(account_id):
            pairs = _load_color_pairs_for_account(account_id)
            cleaned = []
            seen = set()
            for c in pairs:
                name = c.get("name") or ""
                key = name.casefold()
                if not name or key in seen:
                    continue
                seen.add(key)
                cleaned.append(name)
            return sorted(cleaned, key=lambda x: x.casefold())

        def _parse_color_value(raw):
            raw = str(raw or "").strip()
            if not raw:
                return "", ""
            if "||" in raw:
                name, base = raw.split("||", 1)
                return name.strip(), base.strip()
            return raw, ""

        def _default_size_options():
            return ["P", "M", "G", "Extra G"]

        def _load_size_options_for_account(account_id):
            if not account_id:
                return _default_size_options()
            resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
            item = resp.get("Item") or {}
            sizes = item.get("size_options")
            if not isinstance(sizes, list):
                return _default_size_options()
            cleaned = []
            seen = set()
            for s in sizes:
                if s is None:
                    continue
                val = str(s).strip()
                if not val:
                    continue
                k = val.casefold()
                if k in seen:
                    continue
                seen.add(k)
                cleaned.append(val)
            return sorted(cleaned, key=lambda x: x.casefold())

        # Buscar item existente
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("inventory"))

        color_pairs = _load_color_pairs_for_account(account_id)
        color_options = _load_color_options_for_account(account_id)
        size_options = _load_size_options_for_account(account_id)


        all_fields = get_all_fields("item")
        
        # Filtra campos indesejados (ex: category_raw)
        all_fields = [f for f in all_fields if f["id"] not in ["category_raw", "category", "categoria", "category_label", "item_category"]]

        # ---------------- POST ----------------
        if request.method == "POST":
            image_files = request.files.getlist("image_file")
            image_files = [f for f in (image_files or []) if f and getattr(f, "filename", "")]

            existing_urls = item.get("item_image_urls")
            if not isinstance(existing_urls, list):
                existing_urls = []
            existing_urls = [str(u).strip() for u in existing_urls if isinstance(u, str) and u.strip()]
            if not existing_urls:
                legacy = (item.get("item_image_url") or "").strip()
                if legacy and legacy != "N/A":
                    existing_urls = [legacy]

            delete_urls = request.form.getlist("delete_image_urls")
            delete_urls = [str(u).strip() for u in (delete_urls or []) if str(u).strip()]
            delete_set = set(delete_urls)

            existing_urls_before_delete = list(existing_urls)
            existing_urls = [u for u in existing_urls if u not in delete_set]
            allowed_count = sum(1 for f in image_files if allowed_file(getattr(f, "filename", "")))
            if (len(existing_urls) + allowed_count) > MAX_ITEM_IMAGES:
                restante = MAX_ITEM_IMAGES - len(existing_urls)
                if restante < 0:
                    restante = 0
                if len(existing_urls) >= MAX_ITEM_IMAGES:
                    flash(f"Este item já tem {MAX_ITEM_IMAGES} fotos. Remova uma antes de adicionar outra.", "danger")
                else:
                    flash(f"Limite de fotos por item: no máximo {MAX_ITEM_IMAGES}. Você pode adicionar no máximo {restante} agora.", "danger")
                return redirect(request.url)

            raw_main_index = (request.form.get("main_image_index") or "").strip()
            try:
                requested_main_index = int(raw_main_index) if raw_main_index != "" else None
            except Exception:
                requested_main_index = None

            existing_count_before_delete = len(existing_urls_before_delete)

            main_selected_url = None
            main_selected_new_file_idx = None
            if requested_main_index is not None:
                if 0 <= requested_main_index < existing_count_before_delete:
                    candidate = existing_urls_before_delete[requested_main_index]
                    if candidate not in delete_set:
                        main_selected_url = candidate
                elif requested_main_index >= existing_count_before_delete:
                    main_selected_new_file_idx = requested_main_index - existing_count_before_delete

            image_bytes_for_ai = None
            try:
                if main_selected_new_file_idx is not None:
                    if 0 <= main_selected_new_file_idx < len(image_files):
                        image_bytes_for_ai = image_files[main_selected_new_file_idx].read()
                        image_files[main_selected_new_file_idx].seek(0)
                elif image_files:
                    image_bytes_for_ai = image_files[0].read()
                    image_files[0].seek(0)
            except Exception:
                image_bytes_for_ai = None

            uploaded_by_file_idx = {}
            new_urls = []
            for idx, f in enumerate(image_files):
                if not allowed_file(getattr(f, "filename", "")):
                    continue
                url = upload_image_to_s3(f)
                if not url:
                    continue
                uploaded_by_file_idx[idx] = url
                new_urls.append(url)

            if main_selected_url is None and main_selected_new_file_idx is not None:
                main_selected_url = uploaded_by_file_idx.get(main_selected_new_file_idx)

            merged_urls = [*existing_urls, *new_urls]
            if len(merged_urls) > MAX_ITEM_IMAGES:
                flash(f"Limite de fotos por item: no máximo {MAX_ITEM_IMAGES}.", "danger")
                return redirect(request.url)

            remove_item_main_image_index = False
            requested_main_index_final = None
            if main_selected_url and main_selected_url in merged_urls:
                requested_main_index_final = merged_urls.index(main_selected_url)
            else:
                remove_item_main_image_index = True

            if merged_urls:
                new_image_url = (
                    merged_urls[requested_main_index_final]
                    if requested_main_index_final is not None
                    else merged_urls[0]
                )
            else:
                new_image_url = "N/A"
                requested_main_index_final = None
                remove_item_main_image_index = True

            import re
            from decimal import Decimal, InvalidOperation

            def _parse_money_to_decimal(raw):
                s = (raw or "").strip()
                if not s:
                    raise InvalidOperation

                s = re.sub(r"\s+", "", s)
                s = re.sub(r"^R\$\s*", "", s, flags=re.IGNORECASE)
                s = re.sub(r"[^\d,.\-]", "", s)
                if not s:
                    raise InvalidOperation

                if "," in s:
                    parts = s.split(",")
                    int_part = "".join(parts[:-1]).replace(".", "")
                    dec_part = re.sub(r"\D", "", (parts[-1] or ""))[:2]
                    dec_part = (dec_part + "00")[:2]
                    return Decimal(f"{int_part or '0'}.{dec_part}")

                if "." in s:
                    dot_parts = s.split(".")
                    if len(dot_parts) == 2 and dot_parts[1] and len(dot_parts[1]) <= 2:
                        int_part = re.sub(r"\D", "", dot_parts[0] or "")
                        dec_part = re.sub(r"\D", "", dot_parts[1] or "")[:2]
                        dec_part = (dec_part + "00")[:2]
                        return Decimal(f"{int_part or '0'}.{dec_part}")

                    digits = re.sub(r"\D", "", s)
                    if not digits:
                        raise InvalidOperation
                    return Decimal(f"{digits}.00")

                digits = re.sub(r"\D", "", s)
                if not digits:
                    raise InvalidOperation
                return Decimal(f"{digits}.00")

            # new_values = {} # Removido
            updates = {}
            form_keys = set(request.form.keys())

            for field in all_fields:
                field_id = field["id"]
                field_type = field.get("type")

                # 🚫 Proibir alteração de Item Custom ID na edição
                if field_id == "item_custom_id":
                    continue

                if field_id not in form_keys:
                    continue
                label_text = (field.get("label") or "").strip().lower()
                is_color_field = field_id in ["cor", "color", "item_cor", "item_color"] or label_text == "cor"
                is_size_field = field_id in ["tamanho", "size", "item_tamanho", "item_size"] or label_text == "tamanho"

                if is_size_field:
                    raw_list = request.form.getlist(field_id)
                    cleaned = []
                    seen = set()
                    for v in raw_list:
                        s = str(v).strip()
                        if not s:
                            continue
                        k = s.casefold()
                        if k in seen:
                            continue
                        seen.add(k)
                        cleaned.append(s)
                    value = ", ".join(cleaned)
                else:
                    raw_value = request.form.get(field_id, "").strip()
                    is_obs_field = field_id in ["item_obs", "obs", "observacoes", "observação", "observacao", "observações"] or "observa" in label_text
                    if not raw_value and field_type != "item_image_url" and not is_obs_field:
                        continue
                    value = raw_value

                if field_id == "item_image_url":
                    value = new_image_url
                elif is_color_field:
                    name, base = _parse_color_value(value)
                    value = name
                    updates["cor_base"] = base
                    updates["cor_comercial"] = name
                elif field_type in ["value", "item_value"]:
                    try:
                        value = _parse_money_to_decimal(value)
                    except InvalidOperation:
                        flash(f"O campo {field['label']} possui valor inválido.", "danger")
                        return redirect(request.url)
                elif field_type in ["cpf", "cnpj", "phone"]:
                    value = re.sub(r"\D", "", value)

                # Salva tudo em updates
                updates[field_id] = value

            updates["item_image_urls"] = merged_urls
            if requested_main_index_final is not None:
                updates["item_main_image_index"] = requested_main_index_final

            # ---------------------------------------------------------
            # Regra de Negócio: Geração de Metadados IA (Obrigatória em Edição)
            # ---------------------------------------------------------
            # Identifica campos de descrição e título
            desc_cfg = next((f for f in all_fields if f["id"] in ["description", "item_description"]), None)
            title_cfg = next((f for f in all_fields if f["id"] in ["title", "item_title"]), None)
            
            desc_id = desc_cfg["id"] if desc_cfg else "item_description"
            title_id = title_cfg["id"] if title_cfg else "item_title"
            
            # Helper para pegar valor atual (novo ou existente)
            def get_val(fid):
                return updates.get(fid) or item.get(fid)

            curr_desc = get_val(desc_id)
            curr_title = get_val(title_id)
            
            # Checa se o usuário limpou explicitamente os campos
            # Se o campo existe no form e está vazio, considera como "quero gerar via IA"
            if desc_id in request.form and request.form.get(desc_id, "").strip() == "":
                curr_desc = None
            if title_id in request.form and request.form.get(title_id, "").strip() == "":
                curr_title = None

            # Verifica alterações
            changes = {}

            # Compara updates com item existente
            for k, v in updates.items():
                if item.get(k) != v:
                    changes[k] = v

            # ---------------------------------------------------------
            # Regra de Negócio: Sincronização de Campos Legados (GSI)
            # Garante que updates em 'description' reflitam em 'item_description' e vice-versa
            # Garante que updates em 'title' reflitam em 'item_title'
            # ---------------------------------------------------------
            if "description" in changes:
                changes["item_description"] = changes["description"]
            elif "item_description" in changes:
                changes["description"] = changes["item_description"]

            if "title" in changes:
                changes["item_title"] = changes["title"]
            elif "item_title" in changes:
                changes["title"] = changes["item_title"]

            # Verifica alterações em ocasiões
            occasion_changes = False
            occasion_set = {}
            occasion_remove = []

            for slug in ["madrinha","formatura","gala","debutante","convidada","mae_dos_noivos","noiva","civil"]:
                attr = f"occasion_{slug}"
                is_checked = request.form.get(attr) == "on"
                was_checked = item.get(attr) == "1"

                if is_checked and not was_checked:
                    occasion_set[attr] = "1"
                    occasion_changes = True
                elif not is_checked and was_checked:
                    occasion_remove.append(attr)
                    occasion_changes = True

            force_vision = request.form.get("force_vision") == "on"
            existing_force_vision = str(item.get("embedding_force_vision") or "").strip()
            clear_force_vision = (not force_vision) and bool(existing_force_vision)

            if not changes and not occasion_changes and not force_vision and not clear_force_vision:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)
            
            # Se houve alterações relevantes (Título, Descrição, Imagem ou Cor), marca para re-embedding
            image_changed = any(
                k in changes for k in ["item_image_url", "item_image_urls", "item_main_image_index"]
            )
            title_changed = "title" in changes or "item_title" in changes
            desc_changed = "description" in changes or "item_description" in changes
            color_changed = any(
                key in changes
                for key in ["cor", "cor_comercial", "cor_base", "cores", "color", "item_cor", "item_color"]
            )
            
            # Se algum dos 3 mudou, marca como pending
            if image_changed or title_changed or desc_changed or occasion_changes or color_changed or force_vision:
                changes["embedding_status"] = "pending"
            if force_vision:
                changes["embedding_force_vision"] = "1"

            # Atualiza item no DynamoDB
            set_parts = []
            remove_parts = []
            expression_values = {}
            expression_names = {}
            
            # Campos normais
            for key, value in changes.items():
                set_parts.append(f"#{key} = :{key}")
                expression_values[f":{key}"] = value
                expression_names[f"#{key}"] = key

            # Ocasiões (Set)
            for key, value in occasion_set.items():
                set_parts.append(f"#{key} = :{key}")
                expression_values[f":{key}"] = value
                expression_names[f"#{key}"] = key
            
            # Ocasiões (Remove)
            for key in occasion_remove:
                remove_parts.append(f"#{key}")
                expression_names[f"#{key}"] = key

            if clear_force_vision:
                remove_parts.append("embedding_force_vision")
                if not image_changed and not title_changed and not desc_changed and not occasion_changes and not color_changed:
                    remove_parts.append("embedding_status")

            if remove_item_main_image_index:
                remove_parts.append("#item_main_image_index")
                expression_names["#item_main_image_index"] = "item_main_image_index"

            update_expr = []
            if set_parts:
                update_expr.append("SET " + ", ".join(set_parts))
            if remove_parts:
                update_expr.append("REMOVE " + ", ".join(remove_parts))

            update_kwargs = {
                "Key": {"item_id": item_id},
                "UpdateExpression": " ".join(update_expr),
            }
            if expression_values:
                update_kwargs["ExpressionAttributeValues"] = expression_values
            if expression_names and any(name in update_kwargs["UpdateExpression"] for name in expression_names.keys()):
                update_kwargs["ExpressionAttributeNames"] = expression_names
            itens_table.update_item(**update_kwargs)


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
                        update_expression.append(f"#{key} = :{key}")
                        expression_values[f":{key}"] = value
                        expression_names[f"#{key}"] = key

                    if update_expression:  # Só faz update se houver algo
                        update_kwargs = {
                            "Key": {"transaction_id": tx["transaction_id"]},
                            "UpdateExpression": "SET " + ", ".join(update_expression),
                            "ExpressionAttributeValues": expression_values,
                            "ExpressionAttributeNames": expression_names,
                        }

                        transactions_table.update_item(**update_kwargs)


            flash("Item atualizado com sucesso.", "success")
            return redirect(next_page)


        # ---------------- GET ----------------
        # Prepara dados para o formulário
        prepared = {}
        for f in all_fields:
            field_id = f["id"]
            prepared[field_id] = item.get(field_id, "")

        prepared["item_id"] = item["item_id"]
        prepared["embedding_force_vision"] = item.get("embedding_force_vision")
        prepared["item_image_urls"] = item.get("item_image_urls") if isinstance(item.get("item_image_urls"), list) else []
        prepared["item_main_image_index"] = item.get("item_main_image_index")

        def _first_color_value(value):
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (list, tuple, set)):
                for v in value:
                    picked = _first_color_value(v)
                    if picked:
                        return picked
                return ""
            return str(value).strip()

        if not _first_color_value(prepared.get("cor")):
            prepared["cor"] = _first_color_value(
                item.get("cor")
                or item.get("cores")
                or item.get("color")
                or item.get("item_cor")
                or item.get("item_color")
            )
        prepared["cor_comercial"] = _first_color_value(item.get("cor_comercial") or prepared.get("cor"))
        prepared["cor_base"] = _first_color_value(item.get("cor_base"))
        if not prepared.get("cor_base") and prepared.get("cor_comercial") and color_pairs:
            match = next(
                (c.get("base") for c in color_pairs if str(c.get("name") or "").casefold() == prepared["cor_comercial"].casefold()),
                "",
            )
            if match:
                prepared["cor_base"] = match

        def _size_value_list(value):
            if value is None:
                return []
            if isinstance(value, str):
                s = value.strip()
                return [s] if s else []
            if isinstance(value, (list, tuple, set)):
                out = []
                for v in value:
                    out.extend(_size_value_list(v))
                return out
            s = str(value).strip()
            return [s] if s else []

        def _sizes_to_csv(value):
            raw = _size_value_list(value)
            seen = set()
            cleaned = []
            for v in raw:
                s = str(v).strip()
                if not s:
                    continue
                k = s.casefold()
                if k in seen:
                    continue
                seen.add(k)
                cleaned.append(s)
            return ", ".join(cleaned)

        if not _sizes_to_csv(prepared.get("tamanho")):
            prepared["tamanho"] = _sizes_to_csv(
                item.get("tamanho")
                or item.get("size")
                or item.get("item_tamanho")
                or item.get("item_size")
            )
        
        # Adiciona flags de ocasião manualmente
        occasions = ["madrinha", "formatura", "gala", "debutante", "convidada", "mae_dos_noivos", "noiva", "civil"]
        for occ in occasions:
            key = f"occasion_{occ}"
            if item.get(key) == "1":
                prepared[key] = "1"


        origin = next_page.rstrip("/").split("/")[-1]
        origin_status = "available" if origin == "inventory" else "archive"
        title = "Editar item em inventário" if origin_status == "available" else "Editar item em arquivo"

        if color_pairs:
            current_name = prepared.get("cor_comercial") or ""
            current_base = prepared.get("cor_base") or ""
            if current_name:
                key = (current_base.casefold(), current_name.casefold())
                if all(
                    (str(c.get("base") or "").casefold(), str(c.get("name") or "").casefold()) != key
                    for c in color_pairs
                ):
                    color_pairs = sorted(
                        [*color_pairs, {"name": current_name, "base": current_base}],
                        key=lambda x: (x["name"].casefold(), x["base"].casefold()),
                    )

        if size_options:
            current_size = (
                prepared.get("tamanho")
                or prepared.get("size")
                or prepared.get("item_tamanho")
                or prepared.get("item_size")
            )
            if current_size:
                current_size_str = _sizes_to_csv(current_size)
                if current_size_str:
                    for part in [p.strip() for p in current_size_str.split(",") if p.strip()]:
                        key = part.casefold()
                        if all(str(s).strip().casefold() != key for s in size_options):
                            size_options = [*size_options, part]
                    size_options = sorted(size_options, key=lambda x: x.casefold())

        return render_template(
            "edit_item.html",
            item=prepared,
            all_fields=all_fields,
            color_options=color_options,
            color_pairs=color_pairs,
            size_options=size_options,
            next=next_page,
            title=title,
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
                field for field in get_all_fields("transaction")
                if field.get("f_type") != "visual"
            ]
            client_fields = get_all_fields("client")
            item_fields = get_all_fields("item")

            # Junta tudo (sem deduplicação)
            all_fields = transaction_fields + client_fields + item_fields


            form_data = {}
            transaction_fixed_fields = {}

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

                # 👇 Lógica para diferenciar campos de transação (agora tudo é raiz)
                if field_id.startswith("transaction_"):
                    transaction_fixed_fields[field_id] = value
                    # if field.get("fixed"):
                    #    transaction_fixed_fields[field_id] = value
                    # else:


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

        # Carrega todos os campos das três entidades, exceto os campos de visualização de transaction
        transaction_fields = [
            field for field in get_all_fields("transaction")
            if field.get("f_type") != "visual"
        ]
        client_fields = get_all_fields("client")
        item_fields = get_all_fields("item")

        # Junta tudo (sem deduplicação)
        all_fields = transaction_fields + client_fields + item_fields

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

    # Resolver de item por item_id (PK) ou item_custom_id (GSI)
    @app.route("/open_item/<item_ref>")
    def open_item(item_ref):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("inventory"))
        account_id = session.get("account_id")

        # Tenta buscar por chave primária item_id
        try:
            resp_pk = itens_table.get_item(Key={"item_id": item_ref})
            item_pk = resp_pk.get("Item")
            if item_pk:
                # Redireciona para inventário com query item_id
                return redirect(url_for("inventory", item_id=item_pk.get("item_id")))
        except Exception as e:
            print(f"Erro ao buscar por item_id: {e}")

        # Se não encontrou, tenta pelo GSI account_id-item_custom_id-index
        try:
            resp_gsi = itens_table.query(
                IndexName="account_id-item_custom_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("item_custom_id").eq(item_ref),
                Limit=1,
            )
            items = resp_gsi.get("Items", [])
            if items:
                # Redireciona para inventário com query item_id
                return redirect(url_for("inventory", item_id=items[0].get("item_id")))
            else:
                flash("Item não encontrado.", "danger")
                return redirect(next_page)
        except Exception as e:
            print(f"Erro ao buscar por item_custom_id: {e}")
            flash("Erro ao buscar item.", "danger")
            return redirect(next_page)

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
                    UpdateExpression="SET previous_status = #status, #status = :deleted, deleted_date = :deleted_date, deleted_by = :deleted_by, embedding_status = :pending_remove",
                    ExpressionAttributeNames={
                        "#status": "status"  # Alias para evitar conflito com palavra reservada
                    },
                    ExpressionAttributeValues={
                        ":deleted": "deleted",
                        ":deleted_date": deleted_date,
                        ":deleted_by": deleted_by,
                        ":pending_remove": "pending_remove",
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

    @app.route("/permanently_delete_item/<item_id>", methods=["POST"])
    def permanently_delete_item(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        try:
            # Buscar o item
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item")

            if not item:
                flash("Item não encontrado.", "danger")
                return redirect(url_for("trash_itens"))

            # Verificar se o status é realmente 'deleted'
            if item.get("status") != "deleted":
                flash("Apenas itens marcados como deletados podem ser excluídos definitivamente.", "warning")
                return redirect(url_for("trash_itens"))

            user_id = item.get("user_id")
            image_url = item.get("item_image_url") or item.get("image_url")

            # Lógica de exclusão de imagem (similar ao purge)
            deletar_imagem = True
            if (
                user_id
                and image_url
                and isinstance(image_url, str)
                and image_url.strip()
                and image_url != "N/A"
            ):
                # Buscar todos os itens com o mesmo user_id para verificar reutilização de imagem
                response_scan = itens_table.scan(
                    FilterExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                )

                itens_do_usuario = response_scan.get("Items", [])

                # Verificar se a imagem está em uso por outro item (que não seja este mesmo)
                for outro_item in itens_do_usuario:
                    if outro_item["item_id"] == item_id:
                        continue
                        
                    other_img = outro_item.get("item_image_url") or outro_item.get("image_url")
                    if other_img == image_url:
                        deletar_imagem = False
                        break

            # Se não houver outro item usando a mesma imagem, deletar do S3
            if (
                deletar_imagem
                and image_url
                and isinstance(image_url, str)
                and image_url.strip()
                and image_url != "N/A"
            ):
                try:
                    parsed_url = urlparse(image_url)
                    object_key = parsed_url.path.lstrip("/")
                    s3.delete_object(Bucket=s3_bucket_name, Key=object_key)
                except Exception as e:
                    print(f"Erro ao deletar imagem do S3: {e}")

            # Remover o item do DynamoDB
            itens_table.delete_item(Key={"item_id": item_id})

            flash("Item excluído definitivamente.", "success")

        except Exception as e:
            flash(f"Erro ao excluir item definitivamente: {str(e)}", "danger")

        return redirect(url_for("trash_itens"))

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
                UpdateExpression="SET #status = :previous_status, embedding_status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":previous_status": previous_status, ":pending": "pending"},
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
            fields_all_entities[ent] = schemas.get_schema_fields(ent)


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

        # Define os valores padrão
        end_date_default = datetime.datetime.now(user_utc).date()
        start_date_default = end_date_default - datetime.timedelta(days=30)

        # 📥 Recolhe filtros do formulário (GET)
        filtros = request.args.to_dict()

        # Usa os valores do formulário se existirem, senão usa os defaults
        start_date_str = filtros.get("start_date")
        end_date_str = filtros.get("end_date")

        try:
            start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date() if start_date_str else start_date_default
        except ValueError:
            start_date = start_date_default

        try:
            end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date() if end_date_str else end_date_default
        except ValueError:
            end_date = end_date_default
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
            except Exception as e:
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

        # 🔝 Top 30 Itens Mais Visualizados
        top_visited_items = []
        try:
            # Busca todos os itens da conta
            response = itens_table.query(
                IndexName="account_id-created_at-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
                FilterExpression=Attr("status").is_in(["available", "archive"])
            )
            all_account_items = response.get("Items", [])
            
            # Paginação para garantir que pegamos todos
            while "LastEvaluatedKey" in response:
                response = itens_table.query(
                    IndexName="account_id-created_at-index",
                    KeyConditionExpression=Key("account_id").eq(account_id),
                    FilterExpression=Attr("status").is_in(["available", "archive"]),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_account_items.extend(response.get("Items", []))

            # Ordena por visit_count decrescente
            top_visited_items = sorted(
                all_account_items, 
                key=lambda x: int(x.get('visit_count') or 0), 
                reverse=True
            )[:30]
            
        except Exception as e:
            print(f"Erro ao buscar top itens visualizados: {e}")

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
            current_stripe_transaction=current_stripe_transaction,
            fields_all_entities=fields_all_entities,
            top_visited_items=top_visited_items,
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

    @app.route("/api/item/<item_id>/visit", methods=["POST"])
    def increment_visit_count(item_id):
        try:
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="ADD visit_count :inc",
                ExpressionAttributeValues={":inc": 1}
            )
            return jsonify({"success": True}), 200
        except Exception as e:
            print(f"Error incrementing visit count for item {item_id}: {e}")
            return jsonify({"error": str(e)}), 500


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
    client_id=None,
    item_id=None,
    transaction_id=None,
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

    # 🔥 Carrega configuração de campos (usando schemas estáticos)
    from schemas import get_schema_fields
    fields_all_entities = {}
    for ent in ["item", "client", "transaction"]:
        fields_all_entities[ent] = get_schema_fields(ent)

    #cria um lista unica para filtros
    fields_config = (
        fields_all_entities.get("transaction", []) +   #transaçao tem priridade no filtro de campos repetidos, pois vem primeiro
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

    if not transaction_id:
        transaction_id = request.args.get("transaction_id")

    # 🔍 Exibe apenas transações específicas, se item_id, client_id ou transaction_id estiverem definidos
    if item_id or client_id or transaction_id:
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
            if transaction_id and txn.get("transaction_id") != transaction_id:
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
    return item.get(field_id, "")

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
    limit=6,
    entity="item",
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")
        return redirect(url_for("login"))

    def _default_size_options():
        return ["P", "M", "G", "Extra G"]

    def _load_color_options_for_account(account_id):
        if not account_id:
            return []
        resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
        item = resp.get("Item") or {}
        colors = item.get("color_options")
        if not isinstance(colors, list):
            return []
        cleaned = []
        seen = set()
        for c in colors:
            if c is None:
                continue
            if isinstance(c, dict):
                s = c.get("name") or c.get("color") or c.get("comercial") or c.get("cor")
            else:
                s = c
            s = str(s).strip() if s is not None else ""
            if not s:
                continue
            k = s.casefold()
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(s)
        return sorted(cleaned, key=lambda x: x.casefold())

    def _load_size_options_for_account(account_id):
        if not account_id:
            return _default_size_options()
        resp = users_table.get_item(Key={"user_id": f"account_settings:{account_id}"})
        item = resp.get("Item") or {}
        sizes = item.get("size_options")
        if not isinstance(sizes, list):
            return _default_size_options()
        cleaned = []
        seen = set()
        for s in sizes:
            if s is None:
                continue
            val = str(s).strip()
            if not val:
                continue
            k = val.casefold()
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(val)
        return sorted(cleaned, key=lambda x: x.casefold())

    color_options = _load_color_options_for_account(account_id)
    size_options = _load_size_options_for_account(account_id)

    fields_config = get_all_fields(entity)

    force_no_next = request.args.get("force_no_next")
    current_path = request.path
    session["previous_path_itens"] = current_path

    filtros = request.args.to_dict()
    tamanho_list = [v for v in request.args.getlist("tamanho") if str(v).strip()]
    if len(tamanho_list) > 1:
        filtros["tamanho"] = tamanho_list
    item_id = filtros.pop("item_id", None)
    page = int(filtros.pop("page", 1))

    image_url_filter = request.args.get("item_image_url") or None
    image_url_required = image_url_filter.lower() == "true" if image_url_filter is not None else None

    ignored_sig_keys = {"page", "force_no_next"}
    sig_parts = []
    for k in sorted(request.args.keys()):
        if k in ignored_sig_keys:
            continue
        for v in request.args.getlist(k):
            sig_parts.append(f"{k}={v}")
    filtros_signature = "&".join(sig_parts)
    if session.get("filtros_signature_itens") != filtros_signature:
        session["filtros_signature_itens"] = filtros_signature
        session.pop("current_page_itens", None)
        session.pop("cursor_pages_itens", None)
        session.pop("last_page_itens", None)
        if page != 1:
            args = request.args.to_dict(flat=False)
            args.pop("page", None)
            redirect_qs = urlencode(args, doseq=True)
            return redirect(f"{request.path}{'?' + redirect_qs if redirect_qs else ''}")


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
                    color_options=color_options,
                    size_options=size_options,
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
    page_next_start_key = None

    
    valid_occasions = ["madrinha", "formatura", "gala", "debutante", "convidada", "mae_dos_noivos", "noiva", "civil"]
    selected_occasions = [o for o in request.args.getlist("occasion") if o in valid_occasions]
    legacy_occasion = filtros.get("filter")
    if not selected_occasions and legacy_occasion in valid_occasions:
        selected_occasions = [legacy_occasion]

    if len(selected_occasions) == len(valid_occasions):
        selected_occasions = []
    
    use_occasion_gsi = False
    occasion_index_name = ""
    
    occasion_filter = selected_occasions[0] if len(selected_occasions) == 1 else None
    if occasion_filter:
        use_occasion_gsi = True
        occasion_index_name = f"occasion_{occasion_filter}-index"

    while len(valid_itens) < limit:
        stopped_early = False
        if use_occasion_gsi:
            # Query otimizada pelo GSI da ocasião
            query_kwargs = {
                "IndexName": occasion_index_name,
                "KeyConditionExpression": Key(f"occasion_{occasion_filter}").eq("1") & Key("account_id").eq(account_id),
                "Limit": batch_size,
            }
        else:
            # Query padrão por data de criação
            query_kwargs = {
                "IndexName": "account_id-created_at-index",
                "KeyConditionExpression": Key("account_id").eq(account_id),
                "ScanIndexForward": False,
                "Limit": batch_size,
            }

        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        try:
            response = itens_table.query(**query_kwargs)
        except Exception as e:
            msg = str(e)
            if "The provided starting key is invalid" in msg:
                session.pop("current_page_itens", None)
                session.pop("cursor_pages_itens", None)
                session.pop("last_page_itens", None)
                args = request.args.to_dict(flat=False)
                args.pop("page", None)
                redirect_qs = urlencode(args, doseq=True)
                return redirect(f"{request.path}{'?' + redirect_qs if redirect_qs else ''}")
            raise
        itens_batch = response.get("Items", [])
        raw_last_evaluated_key = response.get("LastEvaluatedKey")

        if not itens_batch:
            break

        last_scanned_item = None

        for item in itens_batch:
            last_scanned_item = item
            # Se for filtro de ocasião, precisamos verificar status manualmente
            # Pois o GSI não tem status na chave, apenas projeção ALL
            # Mas itens deletados devem ser ignorados.
            if item.get("status") == "deleted":
                continue
                
            # Se NÃO for filtro de ocasião, aplica filtro de status_list (available/archive)
            if not use_occasion_gsi and item.get("status") not in status_list:
                continue
                
            # Se for filtro de ocasião e o item estiver arquivado, talvez queira mostrar?
            # Por padrão da função, respeitamos status_list.
            if use_occasion_gsi and item.get("status") not in status_list:
                continue

            if selected_occasions:
                item_matches_any = False
                for occ in selected_occasions:
                    if item.get(f"occasion_{occ}") == "1":
                        item_matches_any = True
                        break
                if not item_matches_any:
                    continue

            if entidade_atende_filtros_dinamico(item, filtros, fields_config, image_url_required):
                valid_itens.append(item)
                last_valid_item = item

                if len(valid_itens) == limit:
                    stopped_early = True
                    break

        if stopped_early and last_scanned_item:
            if use_occasion_gsi:
                page_next_start_key = {
                    f"occasion_{occasion_filter}": "1",
                    "account_id": last_scanned_item["account_id"],
                    "item_id": last_scanned_item["item_id"],
                }
            else:
                created_at = last_scanned_item.get("created_at")
                if isinstance(created_at, Decimal):
                    created_at = int(created_at)
                page_next_start_key = {
                    "account_id": last_scanned_item["account_id"],
                    "created_at": created_at,
                    "item_id": last_scanned_item["item_id"],
                }
            break

        if len(valid_itens) < limit:
            if raw_last_evaluated_key:
                exclusive_start_key = raw_last_evaluated_key
            else:
                break

    next_cursor_token = None
    if page_next_start_key:
        next_cursor_token = encode_dynamo_key(page_next_start_key)
    elif raw_last_evaluated_key:
        next_cursor_token = encode_dynamo_key(raw_last_evaluated_key)

    if next_cursor_token:
        session["cursor_pages_itens"][str(page + 1)] = next_cursor_token
    else:
        session["cursor_pages_itens"].pop(str(page + 1), None)

    last_page_itens = session.get("last_page_itens")
    current_page = session.get("current_page_itens", 1)

    has_next = bool(next_cursor_token) and not (last_page_itens is not None and current_page >= last_page_itens)
    if force_no_next:
        has_next = False

    if not valid_itens and page > 1:
        flash("Não há mais itens para exibir.", "info")
        last_valid_page = page - 1
        session["current_page_itens"] = last_valid_page
        session["last_page_itens"] = last_valid_page
        args = request.args.to_dict(flat=False)
        args["page"] = [str(last_valid_page)]
        args["force_no_next"] = ["1"]
        redirect_qs = urlencode(args, doseq=True)
        return redirect(f"{request.path}{'?' + redirect_qs if redirect_qs else ''}")

    def _first_color_value(value):
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            for v in value:
                picked = _first_color_value(v)
                if picked:
                    return picked
            return ""
        return str(value).strip()

    def _normalize_item_colors(item):
        if not isinstance(item, dict):
            return
        base = _first_color_value(item.get("cor_base"))
        commercial = _first_color_value(item.get("cor_comercial"))
        if not commercial:
            commercial = _first_color_value(
                item.get("cor")
                or item.get("cores")
                or item.get("color")
                or item.get("item_cor")
                or item.get("item_color")
            )
        if not base:
            mapping = item.get("mapa_cor_comercial_para_base") or {}
            if isinstance(mapping, dict) and commercial:
                mapped = mapping.get(commercial)
                if mapped:
                    base = _first_color_value(mapped)
                else:
                    commercial_cf = str(commercial).strip().casefold()
                    for name, base_value in mapping.items():
                        if not base_value:
                            continue
                        if str(base_value).strip().casefold() == commercial_cf:
                            base = _first_color_value(base_value)
                            if name and str(name).strip():
                                commercial = _first_color_value(name)
                            break
        if base:
            item["cor_base"] = base
        if commercial:
            item["cor_comercial"] = commercial
        if not _first_color_value(item.get("cor")):
            item["cor"] = commercial or base or ""

    for item in valid_itens:
        _normalize_item_colors(item)

    fields_config = sorted(fields_config, key=lambda x: x["order_sequence"])

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
        has_next=has_next,
        has_prev=current_page > 1,
        color_options=color_options,
        size_options=size_options,
    )





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


def get_all_fields(entity):
    return schemas.get_schema_fields(entity)
