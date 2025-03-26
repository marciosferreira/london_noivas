import datetime
import uuid
from urllib.parse import urlparse

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)
import json


ALLOWED_EXTENSIONS = {"jpeg", "jpg", "png", "gif", "webp"}


def allowed_file(filename):
    """Verifica se o arquivo tem uma extensão válida."""
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


from utils import upload_image_to_s3, aplicar_filtro, copy_image_in_s3


def init_item_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table
):
    @app.route("/rented")
    def rented():
        return listar_itens_per_transaction(
            ["rented"],
            "rented.html",
            "Transações iniciadas (itens alugados)",
            transactions_table,
            itens_table,
        )

    @app.route("/returned")
    def returned():
        return listar_itens_per_transaction(
            ["returned"],
            "returned.html",
            "Transações encerradas (itens devolvidos)",
            transactions_table,
            itens_table,
        )

    @app.route("/archive")
    def archive():
        return list_raw_itens(
            ["archive"],
            "archive.html",
            "Itens Arquivados",
            itens_table,
        )

    @app.route("/trash_itens")
    def trash_itens():
        return list_raw_itens(
            ["deleted", "version"],
            "trash_itens.html",
            "Histórico de alterações",
            itens_table,
        )

    @app.route("/trash_transactions")
    def trash_transactions():
        return listar_itens_per_transaction(
            ["deleted", "version"],
            "trash_transactions.html",
            "Lixeira de transações",
            transactions_table,
            itens_table,
        )

    @app.route("/inventario")
    def inventario():
        return list_raw_itens(
            ["available"],
            "inventario.html",
            "Inventário",
            itens_table,
        )

    @app.route("/add", methods=["GET", "POST"])
    def add():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("index"))

        # Extrair a última parte da URL de next_page
        origin = next_page.rstrip("/").split("/")[-1]
        origin_status = "available" if origin == "inventario" else "archive"
        print(origin_status)

        # Obter o user_id e account_id do usuário logado da sessão
        user_id = session.get("user_id")
        account_id = session.get("account_id")

        if request.method == "POST":
            # Capturar dados do formulário
            status = request.form.get(
                "status"
            )  # Captura o status: rented, returned, available
            description = request.form.get("description").strip()
            comments = request.form.get("comments").strip()
            valor = request.form.get("valor").strip()

            image_url = "N/A"  # Define um valor padrão ou dinamo db não cria o campo

            image_file = request.files.get("image_file")

            # Validar e fazer upload da imagem, se houver
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    image_url = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Gerar um ID único para o item
            item_id = str(uuid.uuid4())

            # Adicionar o novo item ao DynamoDB
            itens_table.put_item(
                Item={
                    "user_id": user_id,
                    "account_id": account_id,
                    "item_id": item_id,
                    "description": description,
                    "comments": comments,
                    "image_url": image_url,
                    "status": origin_status,
                    "previous_status": status,
                    "valor": valor,
                }
            )

            flash(
                f"Item adicionado com sucesso! ",
                "success",
            )
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG e WEBP.",
                    "danger",
                )
            # Redirecionar para a página de origem
            return redirect(next_page)

        return render_template("add.html", next=next_page)

    ######################################################################################################

    @app.route("/edit_transaction/<transaction_id>", methods=["GET", "POST"])
    def edit_transaction(transaction_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        # Buscar item existente
        response = transactions_table.get_item(Key={"transaction_id": transaction_id})
        transaction = response.get("Item")

        if not transaction:
            flash("Item não encontrado.", "danger")
            return redirect(next_page)

        item_id = transaction.get("item_id")

        # Consulta transações existentes para esse item com status "rented"
        response = transactions_table.query(
            IndexName="item_id-index",
            KeyConditionExpression="item_id = :item_id_val",
            ExpressionAttributeValues={":item_id_val": item_id},
        )

        all_transaction = response.get("Items", [])
        reserved_ranges = []

        for tx in all_transaction:
            if (
                tx.get("status") == "rented"
                and tx.get("transaction_id") != transaction_id
                and tx.get("rental_date")
                and tx.get("return_date")
            ):
                reserved_ranges.append([tx["rental_date"], tx["return_date"]])

        if request.method == "POST":
            # Obter date do formulário
            range_date = request.form.get("range_date")
            rental_str, return_str = range_date.split(" - ")

            # Passo 1: interpretar como %d/%m/%Y
            rental_date = datetime.datetime.strptime(rental_str.strip(), "%d/%m/%Y")
            return_date = datetime.datetime.strptime(return_str.strip(), "%d/%m/%Y")

            # Passo 2: converter para %Y/%m/%d
            rental_date = rental_date.strftime("%Y-%m-%d")
            return_date = return_date.strftime("%Y-%m-%d")

            new_data = {
                "rental_date": rental_date,
                "return_date": return_date,
                "dev_date": request.form.get("dev_date") or None,
                "description": request.form.get("description", "").strip() or None,
                "client_name": request.form.get("client_name") or None,
                "client_tel": request.form.get("client_tel") or None,
                "retirado": "retirado" in request.form,  # Checkbox
                "valor": request.form.get("valor", "").strip() or None,
                "pagamento": request.form.get("pagamento") or None,
                "comments": request.form.get("comments", "").strip() or None,
            }

            # Comparar novos valores com os antigos
            changes = {
                key: value
                for key, value in new_data.items()
                if transaction.get(key) != value
            }

            if not changes:  # Se não houver mudanças, apenas exibir a mensagem e sair
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # Criar cópia do item somente se houver mudanças
            new_transaction_id = str(uuid.uuid4())
            edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            copied_item = {
                key: value
                for key, value in transaction.items()
                if key != "transaction_id" and value not in [None, ""]
            }
            copied_item["transaction_id"] = new_transaction_id
            copied_item["parent_transaction_id"] = transaction.get("transaction_id", "")
            copied_item["status"] = "version"
            copied_item["edited_date"] = edited_date
            copied_item["edited_by"] = session.get("username")
            copied_item["previous_status"] = transaction.get("status")

            # Salvar a cópia no DynamoDB
            transactions_table.put_item(Item=copied_item)

            # agora vamos atualizar o item sendo editado
            # Criar dinamicamente os updates para evitar erro com valores vazios
            update_expression = []
            expression_values = {}

            for key, value in changes.items():
                # Ignorar completamente o campo "status"
                if key == "status":  # Ignorando 'status' na atualização
                    continue

                alias = f":{key[:3]}"  # Criar alias curto para valores
                update_expression.append(f"{key} = {alias}")
                expression_values[alias] = value

            # Se não houver nada para atualizar, evitar erro no DynamoDB
            if not update_expression:
                print("⚠️ Nenhuma atualização necessária, abortando update.")
            else:
                print("🔹 Atualizando com:", update_expression)
                print("🔹 Valores:", expression_values)

                transactions_table.update_item(
                    Key={"transaction_id": transaction_id},
                    UpdateExpression="SET " + ", ".join(update_expression),
                    ExpressionAttributeValues=expression_values,
                )

            flash("Item atualizado com sucesso.", "success")

            return redirect(next_page)

        # correct the date for the template easy date peaker
        transaction_copy = transaction.copy()
        start = datetime.datetime.strptime(
            transaction["rental_date"].strip(), "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
        end = datetime.datetime.strptime(
            transaction["return_date"].strip(), "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
        transaction_copy["range_date"] = f"{start} - {end}"

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
            "retirado": request.form.get("retirado") or None,
            "valor": request.form.get("valor", "").strip() or None,
            "pagamento": request.form.get("pagamento") or None,
            "comments": request.form.get("comments", "").strip() or None,
            "image_url": item.get("image_url", ""),  # Mantém a URL original por padrão
        }

    ########################################################################################################

    @app.route("/edit_small/<item_id>", methods=["GET", "POST"])
    def edit_small(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        # 🔹 Buscar item existente no DynamoDB
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("inventario"))

        if request.method == "POST":
            image_file = request.files.get(
                "image_file"
            )  # Arquivo da nova imagem (se houver)
            image_url = request.form.get(
                "image_url", ""
            ).strip()  # Indicação de exclusão (se houver)
            old_image_url = item.get("image_url", "N/A")

            print("Arquivo recebido:", image_file)
            print("URL recebida:", image_url)

            # 🔹 Se o usuário clicou em "Excluir imagem", apenas atualizamos para "N/A" no banco
            if image_url == "DELETE_IMAGE":
                new_image_url = "N/A"
            else:
                # 🔹 Se o usuário anexou uma nova imagem, tratamos o upload (mas não deletamos a antiga)
                new_image_url = handle_image_upload(image_file, old_image_url)

            # 🔹 Processar os demais dados do formulário
            new_data = process_form_data(request, item)
            new_data["image_url"] = new_image_url

            # 🔹 Comparar novos valores com os antigos para detectar mudanças
            changes = {
                key: value for key, value in new_data.items() if item.get(key) != value
            }
            if not changes:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # 🔹 Criar cópia do item antes de atualizar
            new_item_id = str(uuid.uuid4())
            edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

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

            flash("Item atualizado com sucesso.", "success")
            return redirect(next_page)

        # 🔹 Preparar dados para o template
        item = {
            "item_id": item.get("item_id"),
            "description": item.get("description"),
            "client_name": item.get("client_name"),
            "client_tel": item.get("client_tel"),
            "rental_date": item.get("rental_date"),
            "return_date": item.get("return_date"),
            "comments": item.get("comments"),
            "image_url": item.get("image_url"),
            "retirado": item.get("retirado", False),
            "valor": item.get("valor"),
            "pagamento": item.get("pagamento"),
        }

        return render_template("edit_small.html", item=item)

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
            return redirect(url_for("inventario"))

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
                tx.get("status") == "rented"
                and tx.get("rental_date")
                and tx.get("return_date")
            ):
                reserved_ranges.append([tx["rental_date"], tx["return_date"]])

        if request.method == "POST":
            range_date = request.form.get("range_date")
            client_name = request.form.get("client_name").strip()
            client_id = request.form.get("client_id")
            client_tel = request.form.get("client_tel").strip()
            retirado = "retirado" in request.form  # Verifica checkbox
            valor = request.form.get("valor")
            pagamento = request.form.get("pagamento")
            comments = request.form.get("comments")

            try:
                rental_str, return_str = range_date.split(" - ")

                # Passo 1: interpretar como %d/%m/%Y
                rental_date = datetime.datetime.strptime(rental_str.strip(), "%d/%m/%Y")
                return_date = datetime.datetime.strptime(return_str.strip(), "%d/%m/%Y")

                # Passo 2: converter para %Y/%m/%d
                rental_date = rental_date.strftime("%Y-%m-%d")
                return_date = return_date.strftime("%Y-%m-%d")

            except ValueError:
                flash("Formato de data inválido. Use DD/MM/AAAA.", "danger")
                return render_template(
                    "rent.html", item=item, reserved_ranges=reserved_ranges
                )
            print(client_id)
            # Cliente não encontrado, criar um novo
            if not client_id:
                client_id = str(uuid.uuid4())

            clients_table.put_item(
                Item={
                    "client_id": client_id,
                    "account_id": session.get("account_id"),
                    "client_name": client_name,
                    "client_tel": client_tel,
                    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

            # 🔹 Criar uma transação na tabela alugueqqc_transactions
            transaction_id = str(uuid.uuid4())

            transactions_table.put_item(
                Item={
                    "transaction_id": transaction_id,
                    "account_id": session.get("account_id"),
                    "item_id": item_id,
                    "client_id": client_id,
                    "client_name": client_name,
                    "client_tel": client_tel,
                    "comments": comments,
                    "valor": valor,
                    "pagamento": pagamento,
                    "rental_date": rental_date,
                    "return_date": return_date,
                    "retirado": retirado,
                    "status": "rented",
                    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

            flash("Item <a href='/rented'>alugado</a> com sucesso!", "success")
            return redirect(url_for("inventario"))

        return render_template("rent.html", item=item, reserved_ranges=reserved_ranges)

    ###########################################################################################################

    @app.route("/delete/<item_id>", methods=["POST"])
    def delete(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        deleted_by = session.get("username")
        next_page = request.args.get("next", url_for("index"))

        try:
            # Obter o item antes de modificar
            response = itens_table.get_item(Key={"item_id": item_id})
            item = response.get("Item")

            if item:
                # Obter data e hora atuais no formato brasileiro
                deleted_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

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
                    "Item marcado como deletado. Ele ficará disponível na 'lixeira' por 30 dias.",
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
            hoje = datetime.datetime.utcnow()
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
        print("restore Version")
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

            print("before")
            print(previous_status)
            previous_status = (
                "inventario" if previous_status == "available" else previous_status
            )

            print(previous_status)

            status_map = {
                "rented": "Alugados",
                "returned": "Devolvidos",
                "historic": "Histórico",
                "inventario": "Inventário",
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
        print("restore deleted")
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
                "inventario" if previous_status == "available" else previous_status
            )

            status_map = {
                "rented": "Alugados",
                "returned": "Devolvidos",
                "historic": "Histórico",
                "inventario": "Inventário",
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
        print("restore deleted transaction")
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
                UpdateExpression="SET #status = :transaction_previous_status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":transaction_previous_status": transaction_previous_status
                },
            )

            status_map = {
                "rented": "Alugados",
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
        print("restore_version_transaction")
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("trash_transactions"))

        try:
            # 🔹 Pegar os dados do formulário e converter de JSON para dicionário
            transaction_data = request.form.get("transaction_data")

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
            previous_transaction_status = transaction_data.get("previous_status")

            parent_response = transactions_table.get_item(
                Key={"transaction_id": parent_transaction_id}
            )
            parent_data = parent_response.get("Item")

            if not parent_data:
                flash("Transação pai não encontrada.", "danger")
                return redirect(next_page)

            # 🔹 Verificar o status do item pai
            parent_status = parent_data.get("status")

            # Se o item pai estiver deletado, restauramos o status
            if parent_status == "deleted":
                print("Transaçao pai estava deletada. Restaurando...")
                transactions_table.update_item(
                    Key={"transaction_id": parent_transaction_id},
                    UpdateExpression="SET #status = :prev_status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":prev_status": previous_transaction_status
                    },
                )

            # 🔹 Passo 1: Definir os campos que podem ser trocados
            allowed_fields = {
                "valor",
                "client_name",
                "client_tel",
                "edited_by",
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
                "rented": "Alugados",
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

        # Obter o e-mail do usuário logado
        account_id = session.get("account_id")
        if not account_id:
            print("Erro: Usuário não autenticado corretamente.")  # 🔍 Depuração
            return redirect(url_for("login"))

        # Valores padrão para data inicial e final (últimos 30 dias)
        end_date = datetime.datetime.now().date()
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
                )

        response = transactions_table.query(
            IndexName="account_id-index",  # Usando o GSI para buscar por account_id
            KeyConditionExpression="#account_id = :account_id",
            FilterExpression="#status IN (:rented, :returned)",
            ExpressionAttributeNames={"#account_id": "account_id", "#status": "status"},
            ExpressionAttributeValues={
                ":account_id": account_id,
                ":rented": "rented",
                ":returned": "returned",
            },
        )

        transactions = response.get("Items", [])

        # Inicializar os totais
        total_paid = 0  # Total recebido
        total_due = 0  # Total a receber

        for transaction in transactions:
            try:
                # Considerar apenas registros dentro do período
                rental_date = datetime.datetime.strptime(
                    transaction.get("rental_date"), "%Y-%m-%d"
                ).date()
                if start_date <= rental_date <= end_date:
                    valor = float(transaction.get("valor", 0))
                    pagamento = transaction.get("pagamento", "").lower()

                    # Calcular o total recebido
                    if pagamento == "pago 100%":
                        total_paid += valor
                    elif pagamento == "pago 50%":
                        total_paid += valor * 0.5

                    # Calcular o total a receber
                    if pagamento == "não pago":
                        total_due += valor
                    elif pagamento == "pago 50%":
                        total_due += valor * 0.5
            except (ValueError, TypeError):
                continue  # Ignorar registros com datas ou valores inválidos

        # Total geral: recebido + a receber
        total_general = total_paid + total_due

        flash("Relatório atualizado com sucesso!", "success")

        return render_template(
            "reports.html",
            total_paid=total_paid,
            total_due=total_due,
            total_general=total_general,
            start_date=start_date,
            end_date=end_date,
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


# This function is used by client_routes.py, so it's defined outside the init_item_routes function
def listar_itens_per_transaction(
    status_list, template, title, transactions_table, itens_table, client_id=None
):
    print("jjjjjjjjjjjjjjjj")
    print(client_id)

    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter o account_id do usuário logado
    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")
    comments = request.args.get("comments")

    # Converter intervalos de datas, se fornecidos
    def parse_date(date_str):
        return (
            datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str
            else None
        )

    start_date = parse_date(start_date)
    end_date = parse_date(end_date)
    return_start_date = parse_date(return_start_date)
    return_end_date = parse_date(return_end_date)

    # Para depuração
    print(
        f"Filtros aplicados: description={description}, client_name={client_name}, payment={payment_status}"
    )
    print(
        f"Datas: inicio={start_date}, fim={end_date}, devolucao_inicio={return_start_date}, devolucao_fim={return_end_date}"
    )

    # 🔹 1º Passo: Filtragem no DynamoDB - Consultar transações do usuário
    # Começamos com a filtragem por account_id
    expression_names = {"#account_id": "account_id"}
    expression_values = {":account_id": account_id}
    filter_expression = ""

    # Adicionar filtros diretos se possível
    if client_name:
        expression_names["#client_name"] = "client_name"
        expression_values[":client_name"] = client_name.lower()
        filter_expression += " AND contains(lower(#client_name), :client_name)"

    if payment_status:
        expression_names["#pagamento"] = "pagamento"
        expression_values[":pagamento"] = payment_status
        filter_expression += " AND #pagamento = :pagamento"

    # Construir a expressão de filtro por intervalo de datas de aluguel
    if start_date or end_date:
        expression_names["#rental_date"] = "rental_date"
        if start_date:
            expression_values[":start_date"] = start_date.strftime("%Y-%m-%d")
            filter_expression += " AND #rental_date >= :start_date"
        if end_date:
            expression_values[":end_date"] = end_date.strftime("%Y-%m-%d")
            filter_expression += " AND #rental_date <= :end_date"

    # Construir a expressão de filtro por intervalo de datas de devolução
    if return_start_date or return_end_date:
        expression_names["#return_date"] = "return_date"
        if return_start_date:
            expression_values[":return_start_date"] = return_start_date.strftime(
                "%Y-%m-%d"
            )
            filter_expression += " AND #return_date >= :return_start_date"
        if return_end_date:
            expression_values[":return_end_date"] = return_end_date.strftime("%Y-%m-%d")
            filter_expression += " AND #return_date <= :return_end_date"

    # Remover o " AND " inicial se houver filtros
    if filter_expression:
        filter_expression = filter_expression[5:]  # Remove o primeiro " AND "

    # Consulta base por account_id
    if filter_expression:
        response_account = transactions_table.query(
            IndexName="account_id-index",
            KeyConditionExpression="#account_id = :account_id",
            FilterExpression=filter_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )
    else:
        response_account = transactions_table.query(
            IndexName="account_id-index",
            KeyConditionExpression="#account_id = :account_id",
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )

    transactions_account = response_account.get("Items", [])

    print(f"Transações após filtro por account_id: {len(transactions_account)}")

    if not transactions_account:
        flash("Nenhum item encontrado com os filtros aplicados.", "warning")
        return render_template(
            template,
            itens=[],
            page=1,
            total_pages=1,
            current_filter=filtro,
            title=title,
        )

    # 🔹 2º Passo: Filtrar por status
    status_filter = " OR ".join([f"#status = :{s}" for s in range(len(status_list))])
    status_values = {f":{s}": status for s, status in enumerate(status_list)}

    expression_names["#status"] = "status"
    expression_values.update(status_values)

    # Aplicar filtro de status separadamente, pois status é o índice principal
    transactions_status = []
    for iten_status in status_list:
        response_status = transactions_table.query(
            IndexName="status-index",
            KeyConditionExpression="#status = :status_value",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status_value": iten_status},
        )
        transactions_status.extend(response_status.get("Items", []))

    print(f"Transações após filtro por status: {len(transactions_status)}")

    if not transactions_status:
        flash("Nenhum item encontrado com os filtros aplicados.", "warning")
        return render_template(
            template,
            itens=[],
            page=1,
            total_pages=1,
            current_filter=filtro,
            title=title,
        )
    print("CCCCCCCCIS")
    print(client_id)
    # 🔹 Filtrar por client_id se for fornecido
    if client_id:
        transactions_account = [
            txn for txn in transactions_account if txn.get("client_id") == client_id
        ]
        transactions_status = [
            txn for txn in transactions_status if txn.get("client_id") == client_id
        ]

        # 🚨 Verificar se ainda há transações após o filtro
        if not transactions_account or not transactions_status:
            flash("Nenhuma transação encontrada para este cliente.", "warning")
            return render_template(
                template,
                itens=[],
                page=1,
                total_pages=1,
                current_filter=filtro,
                title=title,
            )

    # 🔹 3º Passo: Filtrar os itens que aparecem em AMBAS as consultas (FORA do loop!)
    filtered_transactions = {}

    # Obter transações que estão em ambos os conjuntos
    for txn in transactions_account:
        txn_id = txn.get("transaction_id")
        # Verificar se esta transação também está no outro conjunto
        if (
            any(t.get("transaction_id") == txn_id for t in transactions_status)
            and "item_id" in txn
        ):
            item_id = txn["item_id"]
            if item_id not in filtered_transactions:
                filtered_transactions[item_id] = []
            filtered_transactions[item_id].append(txn)

    print(
        f"Transações após intersecção: {sum(len(txns) for txns in filtered_transactions.values())}"
    )

    # 🔹 4º Passo: Buscar os itens na itens_table com base nos item_ids coletados
    items = []
    for item_id, txn_list in filtered_transactions.items():
        item_response = itens_table.get_item(Key={"item_id": item_id})
        item_data = item_response.get("Item")

        if item_data:
            # Aplicar filtro de descrição e comentários aqui para os itens
            if (
                description
                and description.lower() not in item_data.get("description", "").lower()
            ):
                continue

            if (
                comments
                and comments.lower() not in item_data.get("comments", "").lower()
            ):
                continue

            for txn_data in txn_list:
                item_copy = item_data.copy()
                item_copy.update(
                    {
                        "transaction_id": txn_data.get("transaction_id"),
                        "transaction_status": txn_data.get("status"),
                        "transaction_previous_status": txn_data.get("previous_status"),
                        "client_name": txn_data.get("client_name"),
                        "client_tel": txn_data.get("client_tel"),
                        "parent_transaction_id": txn_data.get("parent_transaction_id"),
                        "rental_date": txn_data.get("rental_date"),
                        "return_date": txn_data.get("return_date"),
                        "pagamento": txn_data.get("pagamento"),
                        "comments": txn_data.get("comments")
                        or item_data.get("comments", ""),
                        "valor": txn_data.get("valor"),
                        "retirado": txn_data.get("retirado"),
                        "dev_date": txn_data.get("dev_date"),
                        "deleted_date": txn_data.get("deleted_date"),
                        "edited_date": txn_data.get("edited_date"),
                    }
                )
                items.append(item_copy)

    # Processar datas para exibição
    today = datetime.datetime.now().date()
    for dress in items:
        # Processar return_date
        return_date_str = dress.get("return_date")
        if return_date_str:
            try:
                return_date = datetime.datetime.strptime(
                    return_date_str, "%Y-%m-%d"
                ).date()
                dress["overdue"] = return_date < today
                dress["return_date_formatted"] = return_date.strftime("%d-%m-%Y")
            except ValueError:
                dress["overdue"] = False
                dress["return_date_formatted"] = "Data Inválida"
        else:
            dress["overdue"] = False
            dress["return_date_formatted"] = "N/A"

        # Processar rental_date
        rental_date_str = dress.get("rental_date")
        if rental_date_str:
            try:
                rental_date = datetime.datetime.strptime(
                    rental_date_str, "%Y-%m-%d"
                ).date()
                dress["rental_date_formatted"] = rental_date.strftime("%d-%m-%Y")
                dress["rental_date_obj"] = rental_date
            except ValueError:
                dress["rental_date_formatted"] = "Data Inválida"
                dress["rental_date_obj"] = today
        else:
            dress["rental_date_formatted"] = "N/A"
            dress["rental_date_obj"] = today

        # Processar dev_date
        if dress.get("dev_date"):
            try:
                dev_date_obj = datetime.datetime.strptime(
                    dress.get("dev_date"), "%Y-%m-%d"
                )
                dress["dev_date_formatted"] = dev_date_obj.strftime("%d-%m-%Y")
            except ValueError:
                print("no dev_date")

    # Aplicar filtro principal (reservados, retirados, atrasados)
    if filtro == "reservados":
        filtered_items = [dress for dress in items if not dress.get("retirado", False)]
    elif filtro == "retirados":
        filtered_items = [dress for dress in items if dress.get("retirado", False)]
    elif filtro == "atrasados":
        filtered_items = [dress for dress in items if dress.get("overdue", False)]
    elif filtro == "deleted":
        filtered_items = [
            dress
            for dress in items
            if dress.get("status") == "deleted"
            or dress.get("transaction_status") == "deleted"
        ]
    elif filtro == "version":
        filtered_items = [
            dress
            for dress in items
            if dress.get("status") == "version"
            or dress.get("transaction_status") == "version"
        ]
    else:  # Default: "todos"
        filtered_items = items

    print(f"Itens após todos os filtros: {len(filtered_items)}")

    if not filtered_items:
        flash("Nenhum item encontrado com os filtros aplicados.", "warning")
        return render_template(
            template,
            itens=[],
            page=1,
            total_pages=1,
            current_filter=filtro,
            title=title,
        )

    # 🔹 Paginação - aplicada APÓS todos os filtros
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    return render_template(
        template,
        itens=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title=title,
        add_route=url_for("trash_transactions"),
        next_url=request.url,
    )


def list_raw_itens(status_list, template, title, itens_table):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter o account_id do usuário logado
    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")  # 🔍 Depuração
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5

    # Capturar parâmetros de filtro
    description = request.args.get("description")
    comments = request.args.get("comments")

    print(f"Filtros aplicados: description={description}, comments={comments}")

    # Construir a expressão de filtro diretamente para o DynamoDB
    filter_expressions = []
    expression_attr_names = {"#account_id": "account_id"}
    expression_attr_values = {":account_id": account_id}

    # Construir o filtro de status
    status_filter = []
    for i, status in enumerate(status_list):
        status_key = f":status{i}"
        expression_attr_values[status_key] = status
        status_filter.append(f"#status = {status_key}")

    if status_filter:
        expression_attr_names["#status"] = "status"
        filter_expressions.append(f"({' OR '.join(status_filter)})")

    # Adicionar filtros de texto se fornecidos
    if description:
        expression_attr_names["#description"] = "description"
        expression_attr_values[":description"] = description.lower()
        filter_expressions.append("contains(lower(#description), :description)")

    if comments:
        expression_attr_names["#comments"] = "comments"
        expression_attr_values[":comments"] = comments.lower()
        filter_expressions.append("contains(lower(#comments), :comments)")

    # Montar a expressão completa
    filter_expression = " AND ".join(filter_expressions) if filter_expressions else None
    print(f"Expressão de filtro: {filter_expression}")

    # 🔹 Fazer a consulta usando o GSI "account_id-index" com filtros
    scan_kwargs = {
        "IndexName": "account_id-index",
        "KeyConditionExpression": "#account_id = :account_id",
        "ExpressionAttributeNames": expression_attr_names,
        "ExpressionAttributeValues": expression_attr_values,
    }

    if filter_expression:
        scan_kwargs["FilterExpression"] = filter_expression

    response = itens_table.query(**scan_kwargs)
    items = response.get("Items", [])

    print(f"Itens encontrados após filtragem: {len(items)}")

    if not items:
        flash("Nenhum item encontrado com os filtros aplicados.", "warning")
        return render_template(
            template,
            itens=[],
            page=1,
            total_pages=1,
            title=title,
            add_route=url_for("add"),
            next_url=request.url,
        )

    # 🔹 Paginação
    total_items = len(items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = items[start:end]

    return render_template(
        template,
        itens=paginated_items,
        page=page,
        total_pages=total_pages,
        title=title,
        add_route=url_for("add"),
        next_url=request.url,
    )
