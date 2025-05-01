import datetime
import uuid
from urllib.parse import urlparse
from boto3.dynamodb.conditions import Key

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

from decimal import Decimal

from utils import get_user_timezone

from boto3.dynamodb.conditions import Key, Attr

ALLOWED_EXTENSIONS = {"jpeg", "jpg", "png", "gif", "webp"}


def allowed_file(filename):
    """Verifica se o arquivo tem uma extensão válida."""
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


from utils import upload_image_to_s3, aplicar_filtro, copy_image_in_s3


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
            itens_table,
            users_table,
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
            ["deleted", "version"],
            "trash_itens.html",
            "Histórico de alterações",
            itens_table,
            transactions_table,
            users_table,
            payment_transactions_table,
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
        )

    @app.route("/add_item", methods=["GET", "POST"])
    def add_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("index"))

        # Extrair a última parte da URL de next_page
        origin = next_page.rstrip("/").split("/")[-1]
        origin_status = "available" if origin == "inventory" else "archive"

        # Obter o user_id e account_id do usuário logado da sessão
        user_id = session.get("user_id")
        account_id = session.get("account_id")

        if request.method == "POST":
            # Capturar dados do formulário
            status = request.form.get("status")  # status: rented, returned, available
            description = request.form.get("description").strip()
            item_obs = request.form.get("item_obs").strip()
            valor = request.form.get("valor").strip()
            item_custom_id = request.form.get(
                "item_custom_id", ""
            ).strip()  # 🟢 novo campo

            image_url = "N/A"
            image_file = request.files.get("image_file")

            # Validar e fazer upload da imagem, se houver
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    image_url = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Gerar um ID único para o item
            item_id = str(uuid.uuid4())

            user_utc = get_user_timezone(users_table, user_id)

            # Adicionar o novo item ao DynamoDB
            item_data = {
                "user_id": user_id,
                "account_id": account_id,
                "item_id": item_id,
                "description": description,
                "item_obs": item_obs,
                "image_url": image_url,
                "status": origin_status,
                "previous_status": status,
                "valor": valor,
                "created_at": datetime.datetime.now(user_utc).strftime(
                    "%d/%m/%Y %H:%M:%S"
                ),  # ← aqui
            }

            # 🟢 Incluir somente se não estiver vazio
            if item_custom_id:
                item_data["item_custom_id"] = item_custom_id

            itens_table.put_item(Item=item_data)

            flash("Item adicionado com sucesso!", "success")
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG e WEBP.",
                    "danger",
                )

            return redirect(next_page)

        return render_template("add_item.html", next=next_page)

    ######################################################################################################

    @app.route("/edit_transaction/<transaction_id>", methods=["GET", "POST"])
    def edit_transaction(transaction_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        valor_str = request.form.get("valor", "").replace(",", ".")
        pagamento_str = request.form.get("pagamento", "").replace(",", ".")
        next_page = request.args.get("next", url_for("index"))

        # Buscar a transação atual
        response = transactions_table.get_item(Key={"transaction_id": transaction_id})
        transaction = response.get("Item")

        if not transaction:
            flash("Item não encontrado.", "danger")
            return redirect(next_page)

        item_id = transaction.get("item_id")

        # Obter todas as transações relacionadas ao item
        response = transactions_table.query(
            IndexName="item_id-index",
            KeyConditionExpression="item_id = :item_id_val",
            ExpressionAttributeValues={":item_id_val": item_id},
        )
        all_transaction = response.get("Items", [])

        # Reservar intervalos de datas para aluguéis
        reserved_ranges = [
            [tx["rental_date"], tx["return_date"]]
            for tx in all_transaction
            if tx.get("status") == "rented"
            and tx.get("transaction_id") != transaction_id
            and tx.get("rental_date")
            and tx.get("return_date")
        ]

        if request.method == "POST":
            # Captura dos novos dados do formulário
            ret_date = request.form.get("ret_date")
            range_date = request.form.get("range_date")
            rental_str, return_str = range_date.split(" - ")
            rental_date = datetime.datetime.strptime(
                rental_str.strip(), "%d/%m/%Y"
            ).strftime("%Y-%m-%d")
            return_date = datetime.datetime.strptime(
                return_str.strip(), "%d/%m/%Y"
            ).strftime("%Y-%m-%d")

            # Criação de um dicionário com os dados atualizados
            new_data = {
                "rental_date": rental_date,
                "return_date": return_date,
                "dev_date": request.form.get("dev_date") or None,
                "transaction_status": request.form.get("transaction_status") or "None",
                "valor": Decimal(valor_str) if valor_str else Decimal("0.0"),
                "transaction_obs": request.form.get("transaction_obs", "").strip(),
                "pagamento": (
                    Decimal(pagamento_str) if pagamento_str else Decimal("0.0") or None
                ),
            }

            # Se a transação for "reserved", remove o campo dev_date
            if new_data.get("transaction_status") in ["reserved", "rented"]:
                new_data["dev_date"] = None

            # Adiciona o ret_date apenas se foi enviado
            if ret_date:
                new_data["ret_date"] = ret_date

            # Detectar as mudanças ignorando campos específicos
            changes = {
                key: value
                for key, value in new_data.items()
                if transaction.get(key) != value
                and not (transaction.get(key) == "" and value is None)
            }

            # Verifique se há mudanças antes de atualizar
            if not changes:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # Atualizar campos no banco de dados se houver alterações
            if set(changes.keys()).issubset({"transaction_status", "pagamento"}):
                update_expression = []
                expression_values = {}

                if "transaction_status" in changes:
                    update_expression.append("transaction_status = :status")
                    expression_values[":status"] = new_data["transaction_status"]

                if "pagamento" in changes:
                    update_expression.append("pagamento = :pagamento")
                    expression_values[":pagamento"] = new_data["pagamento"]

                transactions_table.update_item(
                    Key={"transaction_id": transaction_id},
                    UpdateExpression="SET " + ", ".join(update_expression),
                    ExpressionAttributeValues=expression_values,
                )

                flash("Item atualizado com sucesso.", "success")
                return redirect(
                    next_page
                )  # Redireciona para a página de origem após sucesso

            # Se houver outras alterações, cria uma nova transação como versão
            new_transaction_id = str(uuid.uuid4())
            copied_item = {
                k: v
                for k, v in transaction.items()
                if k != "transaction_id" and v not in [None, ""]
            }
            copied_item.update(
                {
                    "transaction_id": new_transaction_id,
                    "parent_transaction_id": transaction_id,
                    "transaction_status": "version",
                    "edited_date": datetime.datetime.now().strftime(
                        "%d/%m/%Y %H:%M:%S"
                    ),
                    "edited_by": session.get("username"),
                    "transaction_previous_status": transaction.get(
                        "transaction_status"
                    ),
                }
            )

            transactions_table.put_item(Item=copied_item)

            # Atualizar o item original com as mudanças
            update_expression = []
            expression_values = {}
            expression_names = {}

            for key, value in changes.items():
                field_alias = f"#{key}"
                value_alias = f":val_{key}"
                update_expression.append(f"{field_alias} = {value_alias}")
                expression_values[value_alias] = value
                expression_names[field_alias] = key

            # Se houver valores a atualizar, faça a atualização
            if expression_values:
                transactions_table.update_item(
                    Key={"transaction_id": transaction_id},
                    UpdateExpression="SET " + ", ".join(update_expression),
                    ExpressionAttributeValues=expression_values,
                    ExpressionAttributeNames=expression_names,
                )

                flash("Item atualizado com sucesso.", "success")
            else:
                flash("Nenhuma alteração foi feita.", "warning")

            # Prepare a transação para enviar ao template
            transaction_copy = transaction.copy()
            transaction_copy["range_date"] = (
                f"{datetime.datetime.strptime(transaction['rental_date'], '%Y-%m-%d').strftime('%d/%m/%Y')} - "
                f"{datetime.datetime.strptime(transaction['return_date'], '%Y-%m-%d').strftime('%d/%m/%Y')}"
            )

            return redirect(
                next_page
            )  # Redirecionar para a página de origem após o processo de cópia ou atualização

        # Caso seja uma requisição GET, renderize o template com a transação
        transaction_copy = transaction.copy()
        transaction_copy["range_date"] = (
            f"{datetime.datetime.strptime(transaction['rental_date'], '%Y-%m-%d').strftime('%d/%m/%Y')} - "
            f"{datetime.datetime.strptime(transaction['return_date'], '%Y-%m-%d').strftime('%d/%m/%Y')}"
        )

        return render_template(
            "edit_transaction.html",
            item=transaction_copy,
            reserved_ranges=reserved_ranges,
        )  # Retorna a página de edição se for um GET

        # Caso seja uma requisição GET, renderize o template com a transação
        transaction_copy = transaction.copy()
        transaction_copy["range_date"] = (
            f"{datetime.datetime.strptime(transaction['rental_date'], '%Y-%m-%d').strftime('%d/%m/%Y')} - "
            f"{datetime.datetime.strptime(transaction['return_date'], '%Y-%m-%d').strftime('%d/%m/%Y')}"
        )

        return render_template(
            "edit_transaction.html",
            item=transaction_copy,
            reserved_ranges=reserved_ranges,
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

    def process_form_data(request, item):
        """Processa os dados do formulário e retorna um dicionário atualizado."""
        return {
            "rental_date": request.form.get("rental_date") or None,
            "return_date": request.form.get("return_date") or None,
            "description": request.form.get("description", "").strip() or None,
            "client_name": request.form.get("client_name") or None,
            "client_tel": request.form.get("client_tel") or None,
            "valor": request.form.get("valor", "").strip() or None,
            "pagamento": request.form.get("pagamento") or None,
            "item_obs": request.form.get("item_obs", "").strip() or None,
            "image_url": item.get("image_url", ""),  # Mantém a URL original por padrão
        }

    ########################################################################################################

    @app.route("/edit_item/<item_id>", methods=["GET", "POST"])
    def edit_item(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        # 🔹 Buscar item existente no DynamoDB
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("inventory"))

        if request.method == "POST":
            image_file = request.files.get(
                "image_file"
            )  # Arquivo da nova imagem (se houver)
            image_url = request.form.get(
                "image_url", ""
            ).strip()  # Indicação de exclusão (se houver)
            old_image_url = item.get("image_url", "N/A")

            # 🔹 Se o usuário clicou em "Excluir imagem", apenas atualizamos para "N/A" no banco
            if image_url == "DELETE_IMAGE":
                new_image_url = "N/A"
            else:
                # 🔹 Se o usuário anexou uma nova imagem, tratamos o upload (mas não deletamos a antiga)
                new_image_url = handle_image_upload(image_file, old_image_url)

            # 🔹 Processar os demais dados do formulário
            new_data = process_form_data(request, item)

            # 🟢 Garantir que o novo item_custom_id seja considerado
            new_data["item_custom_id"] = request.form.get("item_custom_id", "").strip()

            # 🔹 Comparar novos valores com os antigos para detectar mudanças
            changes = {
                key: value for key, value in new_data.items() if item.get(key) != value
            }

            # 🔹 Verificar se a imagem foi alterada
            if new_image_url != old_image_url:
                changes["image_url"] = new_image_url

            if not changes:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # 🔹 Criar cópia do item antes de atualizar
            new_item_id = str(uuid.uuid4())

            user_id = session.get("user_id") if "user_id" in session else None
            user_utc = get_user_timezone(users_table, user_id)

            edited_date = datetime.datetime.now(user_utc).strftime("%d/%m/%Y %H:%M:%S")

            copied_item = {
                key: value
                for key, value in item.items()
                if key != "item_id" and value not in [None, ""]
            }
            copied_item.update(
                {
                    "item_id": new_item_id,
                    "previous_status": item.get("status"),
                    "parent_item_id": item.get("item_id", ""),
                    "status": "version",
                    "edited_date": edited_date,
                    "deleted_by": session.get("username"),
                }
            )

            # 🔹 Salvar a cópia no DynamoDB
            itens_table.put_item(Item=copied_item)

            # 🔹 Criar expressão de atualização dinâmica
            update_expression = [f"{key} = :{key[:2]}" for key in changes.keys()]
            expression_values = {f":{key[:2]}": value for key, value in changes.items()}

            # 🔹 Atualizar apenas se houver mudanças
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET " + ", ".join(update_expression),
                ExpressionAttributeValues=expression_values,
            )

            # Caso o usuário decida alterar todos os itens no db
            # Campos do cliente que precisam ser atualizados nas transações
            if request.form.get("update_all_transactions"):
                item_fields = [
                    "description",
                    "image_url",
                    "item_custom_id",
                    "item_obs",
                    "valor",
                ]

                # Filtrar os campos que foram alterados e fazem parte de item_fields
                item_changes = {
                    key: value for key, value in changes.items() if key in item_fields
                }

                if item_changes:
                    response = transactions_table.query(
                        IndexName="item_id-index",
                        KeyConditionExpression="item_id = :item_id_val",
                        ExpressionAttributeValues={":item_id_val": item_id},
                    )

                    transacoes_relacionadas = response.get("Items", [])

                    for transacao in transacoes_relacionadas:
                        update_expr = [f"{key} = :{key}" for key in item_changes.keys()]
                        expr_values = {
                            f":{key}": value for key, value in item_changes.items()
                        }

                        transactions_table.update_item(
                            Key={"transaction_id": transacao["transaction_id"]},
                            UpdateExpression="SET " + ", ".join(update_expr),
                            ExpressionAttributeValues=expr_values,
                        )

            flash("Item atualizado com sucesso.", "success")
            return redirect(next_page)

        # 🔹 Preparar dados para o template
        item = {
            "item_id": item.get("item_id"),
            "item_custom_id": item.get("item_custom_id"),
            "description": item.get("description"),
            "client_name": item.get("client_name"),
            "client_tel": item.get("client_tel"),
            "rental_date": item.get("rental_date"),
            "return_date": item.get("return_date"),
            "item_obs": item.get("item_obs"),
            "image_url": item.get("image_url"),
            "valor": item.get("valor"),
            "pagamento": item.get("pagamento"),
        }

        return render_template("edit_item.html", item=item)

    ##################################################################################################
    @app.route("/rent/<item_id>", methods=["GET", "POST"])
    def rent(item_id):

        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # 🔹 Buscar o item existente na tabela alugueqqc_itens
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("inventory"))

        # Consulta transações existentes para esse item com status "rented"
        response = transactions_table.query(
            IndexName="item_id-index",
            KeyConditionExpression="item_id = :item_id_val",
            ExpressionAttributeValues={":item_id_val": item_id},
        )

        transaction = response.get("Items", [])
        reserved_ranges = []

        for tx in transaction:
            if (
                tx.get("transaction_status") in ["reserved", "rented"]
                and tx.get("rental_date")
                and tx.get("return_date")
            ):
                reserved_ranges.append([tx["rental_date"], tx["return_date"]])

        if request.method == "POST":
            range_date = request.form.get("range_date")
            client_name = request.form.get("client_name").strip()
            client_id = request.form.get("client_id")
            client_tel = request.form.get("client_tel").strip()
            client_email = request.form.get("client_email", "").strip()
            client_address = request.form.get("client_address", "").strip()
            client_cpf = request.form.get("client_cpf", "").strip()
            client_cnpj = request.form.get("client_cnpj", "").strip()
            client_obs = request.form.get("client_obs", "").strip()
            transaction_status = request.form.get("transaction_status", "").strip()
            transaction_obs = request.form.get("transaction_obs", "").strip()
            valor_str = request.form.get("valor", "").replace(",", ".")
            pagamento_str = request.form.get("pagamento", "").replace(",", ".")
            # Transforma em float ou 0.0 se vier vazio
            # Usa Decimal
            valor = Decimal(valor_str) if valor_str else Decimal("0.0")
            pagamento = Decimal(pagamento_str) if pagamento_str else Decimal("0.0")
            item_obs = request.form.get("item_obs")

            try:
                rental_str, return_str = range_date.split(" - ")
                rental_date = datetime.datetime.strptime(
                    rental_str.strip(), "%d/%m/%Y"
                ).strftime("%Y-%m-%d")
                return_date = datetime.datetime.strptime(
                    return_str.strip(), "%d/%m/%Y"
                ).strftime("%Y-%m-%d")
            except ValueError:
                flash("Formato de data inválido. Use DD/MM/AAAA.", "danger")
                return render_template(
                    "rent.html", item=item, reserved_ranges=reserved_ranges
                )

            # Criar client_id se necessário
            user_id = session.get("user_id") if "user_id" in session else None
            user_utc = get_user_timezone(users_table, user_id)
            if not client_id:
                response = clients_table.query(
                    IndexName="client_name-index",
                    KeyConditionExpression="#cn = :client_name_val",
                    ExpressionAttributeNames={"#cn": "client_name"},
                    ExpressionAttributeValues={":client_name_val": client_name},
                )
                existing_clients = response.get("Items", [])

                if existing_clients:
                    client_id = existing_clients[0]["client_id"]
                else:
                    client_id = str(uuid.uuid4())
                    clients_table.put_item(
                        Item={
                            "client_id": client_id,
                            "account_id": session.get("account_id"),
                            "client_name": client_name,
                            "client_tel": client_tel,
                            "client_email": client_email,
                            "client_address": client_address,
                            "client_cpf": client_cpf,
                            "client_cnpj": client_cnpj,
                            "client_obs": client_obs,
                            "created_at": datetime.datetime.now(user_utc).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        }
                    )

            # Obter o item_custom_id do item original
            item_custom_id = item.get("item_custom_id", "")
            image_url = item.get("image_url", "")
            item_obs = item.get("item_obs", "")
            description = item.get("description", "")

            # Criar transação
            transaction_id = str(uuid.uuid4())
            transactions_table.put_item(
                Item={
                    "transaction_id": transaction_id,
                    "account_id": session.get("account_id"),
                    "item_id": item_id,
                    "item_custom_id": item_custom_id,  # ✅ incluído
                    "item_obs": item_obs,  # ✅ incluído
                    "description": description,  # ✅ incluído
                    "client_id": client_id,
                    "client_name": client_name,
                    "client_tel": client_tel,
                    "client_email": client_email,
                    "client_address": client_address,
                    "client_cpf": client_cpf,
                    "client_cnpj": client_cnpj,
                    "client_obs": client_obs,
                    "item_obs": item_obs,
                    "valor": valor,
                    "pagamento": pagamento,
                    "rental_date": rental_date,
                    "return_date": return_date,
                    "transaction_status": transaction_status,
                    "image_url": image_url,
                    "transaction_obs": transaction_obs,
                    "created_at": datetime.datetime.now(user_utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )

            if transaction_status == "reserved":
                flash("Item <a href='/reserved'>reservado</a> com sucesso!", "success")
            else:
                flash("Item <a href='/rented'>retirado</a> com sucesso!", "success")

            return redirect(url_for("all_transactions"))

        return render_template("rent.html", item=item, reserved_ranges=reserved_ranges)

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
                    "%d/%m/%Y %H:%M:%S"
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
                    "Item marcado deletado!",
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
                            deleted_date_str, "%d/%m/%Y %H:%M:%S"
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

        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)
        username = session.get("username", None)

        # Estatísticas integradas (da antiga rota /home)
        stats = {}
        response = itens_table.query(
            IndexName="account_id-status-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
        )
        items = response["Items"]
        stats["total_items_available"] = len(
            [item for item in items if item.get("status") == "available"]
        )
        stats["total_items_archived"] = len(
            [item for item in items if item.get("status") == "archive"]
        )

        stats["total_rented"] = transactions_table.query(
            IndexName="account_id-transaction_status-index",
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("transaction_status").eq("rented"),
        )["Count"]

        stats["total_returned"] = transactions_table.query(
            IndexName="account_id-transaction_status-index",
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("transaction_status").eq("returned"),
        )["Count"]

        stats["total_reserved"] = transactions_table.query(
            IndexName="account_id-transaction_status-index",
            KeyConditionExpression=Key("account_id").eq(account_id)
            & Key("transaction_status").eq("reserved"),
        )["Count"]

        stats["total_clients"] = clients_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
        )["Count"]

        # Datas iniciais e finais (30 dias)
        end_date = datetime.datetime.now(user_utc).date()
        start_date = end_date - datetime.timedelta(days=30)

        if request.method == "POST":
            try:
                start_date = datetime.datetime.strptime(
                    request.form.get("start_date"), "%Y-%m-%d"
                ).date()
                end_date = datetime.datetime.strptime(
                    request.form.get("end_date"), "%Y-%m-%d"
                ).date()
            except ValueError:
                flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
                return render_template(
                    "reports.html",
                    total_paid=0,
                    total_due=0,
                    total_general=0,
                    start_date=start_date,
                    end_date=end_date,
                    stats=stats,
                    username=username,
                )

        response = transactions_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
            FilterExpression=Attr("transaction_status").is_in(
                ["rented", "returned", "reserved"]
            ),
        )
        transactions = response.get("Items", [])

        clients_response = users_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
        )

        new_clients = []
        for client in clients_response.get("Items", []):
            try:
                created_at = datetime.datetime.fromisoformat(
                    client.get("created_at")
                ).date()
                if start_date <= created_at <= end_date:
                    new_clients.append(client)
            except Exception as e:
                print("Erro ao processar created_at:", e)

        num_new_clients = len(new_clients)

        total_paid = 0
        total_due = 0
        num_transactions = 0
        sum_valor = 0
        status_counter = {"rented": 0, "returned": 0, "reserved": 0}
        item_counter = {}

        for transaction in transactions:
            try:
                transaction_date = datetime.datetime.strptime(
                    transaction.get("created_at"), "%Y-%m-%d %H:%M:%S"
                ).date()

                if start_date <= transaction_date <= end_date:
                    num_transactions += 1
                    valor = float(transaction.get("valor", 0))
                    pagamento = float(transaction.get("pagamento", 0))

                    total_paid += pagamento
                    total_due += max(0, valor - pagamento)
                    sum_valor += valor

                    status_counter[
                        transaction.get("transaction_status", "unknown")
                    ] += 1

                    item_id = transaction.get("item_id")
                    if item_id:
                        item_counter[item_id] = item_counter.get(item_id, 0) + 1

            except (ValueError, TypeError):
                continue

        preco_medio = sum_valor / num_transactions if num_transactions else 0
        total_general = total_paid + total_due

        if request.method == "POST":
            flash("Relatório atualizado com sucesso!", "success")

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
    page=None,
    limit=5,
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    user_id = session.get("user_id")
    if not account_id:
        flash("Erro: Usuário não autenticado corretamente.", "danger")
        return redirect(url_for("login"))

    force_no_next = request.args.get("force_no_next")

    # 🕐 Pega o fuso horário do usuário
    user_utc = get_user_timezone(users_table, user_id)
    today = datetime.datetime.now(user_utc).date()

    def process_dates(item):
        for key in ["rental_date", "return_date", "dev_date"]:
            if key in item:
                date_str = item[key]
                if date_str and isinstance(date_str, str):
                    try:
                        date_obj = datetime.datetime.strptime(
                            date_str, "%Y-%m-%d"
                        ).date()
                        item[f"{key}_formatted"] = date_obj.strftime("%d-%m-%Y")
                        item[f"{key}_obj"] = date_obj
                    except ValueError:
                        # Se a string existe mas não é válida, ignora: não adiciona nada
                        pass
                # Se date_str for vazio ou não string válida, também ignora

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
                item["rental_message"] = (
                    "Falta 1 dia" if dias == 1 else f"Faltam {dias} dias"
                )
                item["rental_message_color"] = "blue" if dias > 1 else "yellow"
            else:
                item["rental_message"] = "Não retirado"
                item["rental_message_color"] = "red"
        else:
            item["rental_message"] = ""
            item["rental_message_color"] = ""

        # -- Parte 2: Calcular dias de atraso (NOVO) --
        if item.get("overdue") and return_date:
            overdue_days = (today - return_date).days
            item["overdue_days"] = overdue_days if overdue_days > 0 else 0

        return item

    # --- Pegando parâmetros ---
    filtros = request.args.to_dict()
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

    # --- Query no banco ---
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
            if not filtra_transacao(txn, filtros, client_id, status_list):
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

    # 🔥 Atualiza next_cursor
    next_cursor_token = None
    if last_valid_item:
        next_cursor_token = encode_dynamo_key(
            {
                "account_id": last_valid_item["account_id"],
                "created_at": last_valid_item["created_at"],
                "transaction_id": last_valid_item["transaction_id"],
            }
        )

    if next_cursor_token:
        session["cursor_pages_transactions"][str(page + 1)] = next_cursor_token
    else:
        session["cursor_pages_transactions"].pop(str(page + 1), None)

    # 🔥 Consulta os modelos salvos
    response = text_models_table.query(
        IndexName="account_id-index",
        KeyConditionExpression="account_id = :account_id",
        ExpressionAttributeValues={":account_id": account_id},
    )
    saved_models = response.get("Items", [])

    # 🔥 Controle de botão next
    last_page_transactions = session.get("last_page_transactions")
    current_page = session.get("current_page_transactions", 1)

    if force_no_next:
        has_next = False
    else:
        if len(valid_itens) < limit or (
            last_page_transactions is not None
            and current_page >= last_page_transactions
        ):
            has_next = False
        else:
            has_next = True

    # ⚡ Caso tente avançar sem sucesso
    if not valid_itens and page > 1:
        flash("Não há mais transações para exibir.", "info")
        last_valid_page = page - 1
        session["current_page_transactions"] = last_valid_page
        session["last_page_transactions"] = last_valid_page
        return redirect(
            url_for(
                "all_transactions",
                page=last_valid_page,
                has_next=has_next,
                force_no_next=1,
            )
        )
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


def list_raw_itens(
    status_list,
    template,
    title,
    itens_table,
    transactions_table,
    users_table,
    payment_transactions_table,
    limit=5,
):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")
        return redirect(url_for("login"))

    force_no_next = request.args.get("force_no_next")

    # isso será usado para limitar o plano teste
    user_id = session.get("user_id")
    current_transaction = get_latest_transaction(
        user_id, users_table, payment_transactions_table
    )

    current_path = request.path
    session["previous_path_itens"] = current_path  # 🔥 Marcar o path atual

    # 🔍 Parâmetros de busca
    filtros = request.args.to_dict()
    item_id = filtros.pop("item_id", None)
    page = int(filtros.pop("page", 1))

    if page == 1:
        session.pop("current_page_itens", None)
        session.pop("cursor_pages_itens", None)
        session.pop("last_page_itens", None)

    cursor_pages = session.get("cursor_pages_itens", {"1": None})
    if page == 1:
        session["cursor_pages_itens"] = {"1": None}
        cursor_pages = {"1": None}

    session["current_page_itens"] = page

    # 🔍 Se buscar direto por item_id
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
                    total_relevant_transactions=0,
                    total_items=1,
                    itens_count=1,
                    current_page=1,
                )
            else:
                flash("Item não encontrado ou já deletado.", "warning")
                return redirect(request.referrer or url_for("inventory"))
        except Exception as e:
            print(f"Erro ao buscar item por ID: {e}")
            flash("Erro ao buscar item.", "danger")
            return redirect(request.referrer or url_for("inventory"))

    # 🔥 Continua fluxo normal

    # 🧹 Recupera filtros opcionais
    item_custom_id = request.args.get("item_custom_id")
    description = request.args.get("description")
    item_obs = request.args.get("item_obs")
    min_valor = request.args.get("min_valor")
    max_valor = request.args.get("max_valor")

    # 🧹 Definindo ExclusiveStartKey
    exclusive_start_key = None
    if str(page) in cursor_pages and cursor_pages[str(page)]:
        exclusive_start_key = decode_dynamo_key(cursor_pages[str(page)])

    # 🔥 Busca no banco com múltiplos ciclos
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

            valor_str = item.get("valor")
            try:
                valor_float = float(valor_str)
            except (ValueError, TypeError):
                valor_float = None

            min_val = float(min_valor) if min_valor else None
            max_val = float(max_valor) if max_valor else None

            if min_val is not None and (valor_float is None or valor_float < min_val):
                continue
            if max_val is not None and (valor_float is None or valor_float > max_val):
                continue

            if (
                item_custom_id
                and item_custom_id.lower()
                not in (item.get("item_custom_id") or "").lower()
            ):
                continue
            if (
                description
                and description.lower() not in (item.get("description") or "").lower()
            ):
                continue
            if (
                item_obs
                and item_obs.lower() not in (item.get("item_obs") or "").lower()
            ):
                continue

            valid_itens.append(item)
            last_valid_item = item

            if len(valid_itens) == limit:
                break

        if len(valid_itens) < limit:
            if raw_last_evaluated_key:
                exclusive_start_key = raw_last_evaluated_key
            else:
                break

    # 🔥 Atualiza next_cursor
    next_cursor_token = None
    if last_valid_item:
        next_cursor_token = encode_dynamo_key(
            {
                "account_id": last_valid_item["account_id"],
                "created_at": last_valid_item["created_at"],
                "item_id": last_valid_item["item_id"],
            }
        )

    if next_cursor_token:
        session["cursor_pages_itens"][str(page + 1)] = next_cursor_token
    else:
        session["cursor_pages_itens"].pop(str(page + 1), None)

    # 🔥 Total de transações alugadas/reservadas
    total_relevant_transactions = 0
    try:
        response = transactions_table.query(
            IndexName="account_id-index",
            KeyConditionExpression="account_id = :account_id",
            ExpressionAttributeValues={":account_id": account_id},
        )
        transactions = response.get("Items", [])
        total_relevant_transactions = sum(
            1
            for txn in transactions
            if txn.get("transaction_status") in ["rented", "reserved", "returned"]
        )
    except Exception as e:
        print(f"Erro ao consultar transações: {e}")

    # 🔥 Controle de botão next
    last_page_itens = session.get("last_page_itens")
    current_page = session.get("current_page_itens", 1)

    if force_no_next:
        has_next = False
    else:
        if len(valid_itens) < limit or (
            last_page_itens is not None and current_page >= last_page_itens
        ):
            has_next = False
        else:
            has_next = True

    # ⚡ Caso tenha tentado avançar sem sucesso
    if not valid_itens and page > 1:
        flash("Não há mais itens para exibir.", "info")
        last_valid_page = page - 1
        session["current_page_itens"] = last_valid_page
        session["last_page_itens"] = last_valid_page
        return redirect(url_for("inventory", page=last_valid_page, force_no_next=1))

    return render_template(
        template,
        itens=valid_itens,
        next_cursor=next_cursor_token,
        last_page=not has_next,
        title=title,
        add_route=url_for("add_item"),
        next_url=request.url,
        current_page=current_page,
        total_relevant_transactions=total_relevant_transactions,
        total_items=len(valid_itens),
        itens_count=len(valid_itens),
        has_next=has_next,
        has_prev=current_page > 1,
        current_transaction=current_transaction,
    )


def filtra_transacao(txn, filtros, client_id, status_list):
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
