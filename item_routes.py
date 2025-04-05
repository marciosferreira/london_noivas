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
            transactions_table,
        )

    @app.route("/trash_itens")
    def trash_itens():
        return list_raw_itens(
            ["deleted", "version"],
            "trash_itens.html",
            "Histórico de alterações",
            itens_table,
            transactions_table,
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
            transactions_table,
        )

    @app.route("/add_item", methods=["GET", "POST"])
    def add_item():
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
            # 📸 Tratamento de imagem
            image_file = request.files.get("image_file")
            image_url_form = request.form.get("image_url", "").strip()
            old_image_url = transaction.get("image_url", "N/A")

            if image_url_form == "DELETE_IMAGE":
                new_image_url = "N/A"
            else:
                new_image_url = handle_image_upload(image_file, old_image_url)

            # 🗓️ Datas
            range_date = request.form.get("range_date")
            rental_str, return_str = range_date.split(" - ")
            rental_date = datetime.datetime.strptime(
                rental_str.strip(), "%d/%m/%Y"
            ).strftime("%Y-%m-%d")
            return_date = datetime.datetime.strptime(
                return_str.strip(), "%d/%m/%Y"
            ).strftime("%Y-%m-%d")

            # 📝 Coleta de dados do formulário
            new_data = {
                "rental_date": rental_date,
                "return_date": return_date,
                "dev_date": request.form.get("dev_date") or None,
                "description": request.form.get("description", "").strip() or None,
                "client_name": request.form.get("client_name") or None,
                "client_tel": request.form.get("client_tel") or None,
                "client_email": request.form.get("client_email") or None,
                "client_address": request.form.get("client_address") or None,
                "client_cpf": request.form.get("client_cpf") or None,
                "client_cnpj": request.form.get("client_cnpj") or None,
                "client_tel_digits": request.form.get("client_tel_digits") or None,
                "client_cpf_digits": request.form.get("client_cpf_digits") or None,
                "client_cnpj_digits": request.form.get("client_cnpj_digits") or None,
                "retirado": "retirado" in request.form,
                "valor": request.form.get("valor", "").strip() or None,
                "transaction_obs": request.form.get("transaction_obs", "").strip()
                or None,
                "pagamento": request.form.get("pagamento") or None,
                "item_obs": request.form.get("item_obs", "").strip() or None,
                "item_custom_id": request.form.get("item_custom_id", "").strip()
                or None,
                "image_url": new_image_url,  # ✅ incluído
            }

            # Comparar novos valores com os antigos
            changes = {
                key: value
                for key, value in new_data.items()
                if transaction.get(key) != value
            }

            if not changes:
                flash("Nenhuma alteração foi feita.", "warning")
                return redirect(next_page)

            # Cópia de segurança e atualização (segue igual ao seu código atual)...

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
            expression_names = {}

            for key, value in changes.items():
                if key == "status":
                    continue

                field_alias = f"#{key}"  # nome do campo com #
                value_alias = f":val_{key}"  # valor do campo com :

                update_expression.append(f"{field_alias} = {value_alias}")
                expression_values[value_alias] = value
                expression_names[field_alias] = key

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
                    ExpressionAttributeNames=expression_names,
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
            "item_custom_id": item.get("item_custom_id"),
            "description": item.get("description"),
            "client_name": item.get("client_name"),
            "client_tel": item.get("client_tel"),
            "rental_date": item.get("rental_date"),
            "return_date": item.get("return_date"),
            "item_obs": item.get("item_obs"),
            "image_url": item.get("image_url"),
            "retirado": item.get("retirado", False),
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
            client_email = request.form.get("client_email", "").strip()
            client_address = request.form.get("client_address", "").strip()
            client_cpf = request.form.get("client_cpf", "").strip()
            client_cnpj = request.form.get("client_cnpj", "").strip()
            client_obs = request.form.get("client_obs", "").strip()
            transaction_obs = request.form.get("transaction_obs", "").strip()

            retirado = "retirado" in request.form
            valor = request.form.get("valor")
            pagamento = request.form.get("pagamento")
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
            if not client_id:
                client_id = str(uuid.uuid4())

            # Inserir cliente no banco
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
                    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
                    "retirado": retirado,
                    "status": "rented",
                    "image_url": image_url,
                    "transaction_obs": transaction_obs,
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

        if request.method == "POST":
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
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")
        return redirect(url_for("login"))

    def parse_date(date_str):
        return (
            datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str
            else None
        )

    def process_dates(item):
        today = datetime.datetime.now().date()
        for key in ["rental_date", "return_date", "dev_date"]:
            date_str = item.get(key)
            if date_str:
                try:
                    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    item[f"{key}_formatted"] = date_obj.strftime("%d-%m-%Y")
                    if key == "return_date":
                        item["overdue"] = date_obj < today
                    if key == "rental_date":
                        item["rental_date_obj"] = date_obj
                except ValueError:
                    item[f"{key}_formatted"] = "Data Inválida"
                    if key == "rental_date":
                        item["rental_date_obj"] = today
            else:
                item[f"{key}_formatted"] = "N/A"
                if key == "rental_date":
                    item["rental_date_obj"] = today
        item.setdefault("overdue", False)
        return item

    def apply_filtros_request():
        filtros = {
            "filter": request.args.get("filter", "todos"),
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
            "retirado": request.args.get("retirado"),  # ✅ NOVO
            "transaction_obs": request.args.get("transaction_obs"),  # ✅ NOVO
        }
        return filtros

    def montar_filtro_dynamodb(account_id, filtros):
        names = {"#account_id": "account_id"}
        values = {":account_id": account_id}
        conditions = []

        campos_texto = [
            ("client_name", True),
            ("client_email", True),
            ("client_address", True),
            ("client_obs", True),  # ⬅️ adiciona este!
            ("client_cpf", False),
            ("client_cnpj", False),
            ("pagamento", False),
        ]
        for campo, use_lower in campos_texto:
            valor = filtros.get(campo if campo != "pagamento" else "payment_status")
            if valor:
                key = f"#{campo}"
                val = f":{campo}"
                names[key] = campo
                values[val] = valor.lower() if use_lower else valor
                if use_lower:
                    conditions.append(f"contains(lower({key}), {val})")
                else:
                    conditions.append(f"{key} = {val}")

        for data_campo, op, val_key in [
            ("rental_date", ">=", ":start_date"),
            ("rental_date", "<=", ":end_date"),
            ("return_date", ">=", ":return_start_date"),
            ("return_date", "<=", ":return_end_date"),
        ]:
            valor = filtros.get(val_key.strip(":"))
            if valor:
                names[f"#{data_campo}"] = data_campo
                values[val_key] = valor.strftime("%Y-%m-%d")
                conditions.append(f"#{data_campo} {op} {val_key}")

        return names, values, " AND ".join(conditions)

    def buscar_transacoes_por(account_id, status_list, filtros):
        names, values, filtro_expr = montar_filtro_dynamodb(account_id, filtros)
        base_query = {
            "IndexName": "account_id-index",
            "KeyConditionExpression": "#account_id = :account_id",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if filtro_expr:
            base_query["FilterExpression"] = filtro_expr
        trans_acc = transactions_table.query(**base_query).get("Items", [])

        trans_status = []
        for s in status_list:
            response = transactions_table.query(
                IndexName="status-index",
                KeyConditionExpression="#status = :status_value",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status_value": s},
            )
            trans_status.extend(response.get("Items", []))

        return trans_acc, trans_status

    def combinar_transacoes(trans_acc, trans_status, client_id=None):
        acc_ids = {
            txn["transaction_id"]
            for txn in trans_acc
            if not client_id or txn.get("client_id") == client_id
        }
        status_ids = {
            txn["transaction_id"]
            for txn in trans_status
            if not client_id or txn.get("client_id") == client_id
        }
        comuns = acc_ids & status_ids
        mapa = {}
        for txn in trans_acc:
            if txn["transaction_id"] in comuns:
                mapa.setdefault(txn["item_id"], []).append(txn)
        return mapa

    def montar_transacoes_com_imagem(mapa_txn, filtros):
        transacoes = []

        for item_id, transacoes_por_item in mapa_txn.items():
            item_data = itens_table.get_item(Key={"item_id": item_id}).get("Item")
            if not item_data:
                continue

            for txn in transacoes_por_item:

                # Skip por filtro de descrição/comentários, se necessário
                if (
                    filtros["description"]
                    and filtros["description"].lower()
                    not in txn.get("description", "").lower()
                ):
                    continue
                if (
                    filtros["item_obs"]
                    and filtros["item_obs"].lower()
                    not in txn.get("item_obs", "").lower()
                ):
                    continue

                if (
                    filtros["item_custom_id"]
                    and filtros["item_custom_id"].lower()
                    not in item_data.get("item_custom_id", "").lower()
                ):
                    continue

                if (
                    filtros["client_obs"]
                    and filtros["client_obs"].lower()
                    not in txn.get("client_obs", "").lower()
                ):
                    continue

                if filtros.get("retirado") is not None:
                    retirado_valor = filtros["retirado"].lower() == "true"
                    if txn.get("retirado") != retirado_valor:
                        continue

                if (
                    filtros["transaction_obs"]
                    and filtros["transaction_obs"].lower()
                    not in txn.get("transaction_obs", "").lower()
                ):
                    continue

                # NOVO: Filtro de pagamento baseado no valor pago vs valor total
                pagamento_raw = txn.get("pagamento")
                valor_raw = txn.get("valor")

                try:
                    pagamento = float(pagamento_raw or 0)
                    valor = float(valor_raw or 0)
                except (TypeError, ValueError):
                    continue

                filtro_pagamento = (filtros.get("payment") or "").lower().strip()

                if filtro_pagamento == "pago total":
                    if pagamento < valor:
                        continue
                elif filtro_pagamento == "pago parcial":
                    if pagamento == 0 or pagamento >= valor:
                        continue
                elif filtro_pagamento == "não pago":
                    if pagamento > 0:
                        continue

                transacoes.append(process_dates(txn))

        return transacoes

    def filtrar_por_categoria(itens, categoria):

        if categoria == "deleted":
            return [i for i in itens if i.get("transaction_status") == "deleted"]
        if categoria == "version":
            return [i for i in itens if i.get("transaction_status") == "version"]
        return itens

    filtros = apply_filtros_request()
    trans_acc, trans_status = buscar_transacoes_por(account_id, status_list, filtros)
    mapa_txn = combinar_transacoes(trans_acc, trans_status, client_id)
    itens_combinados = montar_transacoes_com_imagem(mapa_txn, filtros)
    filtrados = filtrar_por_categoria(itens_combinados, filtros["filter"])

    total = len(filtrados)
    total_pages = (total + 4) // 5
    page = int(request.args.get("page", 1))
    paginados = filtrados[(page - 1) * 5 : page * 5]

    if not paginados:
        flash("Nenhum item encontrado para os filtros selecionados.", "warning")

    return render_template(
        template,
        itens=paginados,
        page=page,
        total_pages=total_pages,
        current_filter=filtros["filter"],
        title=title,
        add_route=url_for("trash_transactions"),
        next_url=request.url,
    )


def list_raw_itens(status_list, template, title, itens_table, transactions_table):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obter o account_id do usuário logado
    account_id = session.get("account_id")
    if not account_id:
        print("Erro: Usuário não autenticado corretamente.")  # 🔍 Depuração
        return redirect(url_for("login"))

    total_relevant_transactions = 0

    try:
        response = transactions_table.query(
            IndexName="account_id-index",
            KeyConditionExpression="account_id = :account_id",
            ExpressionAttributeValues={":account_id": account_id},
        )
        transactions = response.get("Items", [])

        total_relevant_transactions = sum(
            1 for txn in transactions if txn.get("status") in ["rented", "returned"]
        )

    except Exception as e:
        print(f"Erro ao consultar transações: {e}")
    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5

    # Capturar parâmetros de filtro
    item_custom_id = request.args.get("item_custom_id")
    description = request.args.get("description")
    item_obs = request.args.get("item_obs")
    min_valor = request.args.get("min_valor")
    max_valor = request.args.get("max_valor")

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
    if item_custom_id:
        expression_attr_names["#item_custom_id"] = "item_custom_id"
        expression_attr_values[":item_custom_id"] = item_custom_id.lower()
        filter_expressions.append("contains(#item_custom_id, :item_custom_id)")

    if description:
        expression_attr_names["#description"] = "description"
        expression_attr_values[":description"] = description.lower()
        filter_expressions.append("contains(lower(#description), :description)")

    if item_obs:
        expression_attr_names["#item_obs"] = "item_obs"
        expression_attr_values[":item_obs"] = item_obs.lower()
        filter_expressions.append("contains(lower(#item_obs), :item_obs)")

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

    # Filtrar por valor mínimo e máximo (localmente, pois DynamoDB armazena strings)
    try:
        min_valor = float(min_valor) if min_valor else None
        max_valor = float(max_valor) if max_valor else None
    except ValueError:
        min_valor = None
        max_valor = None

    filtered_items = []
    for item in items:
        valor_str = item.get("valor")
        try:
            valor_float = float(valor_str)
        except (ValueError, TypeError):
            valor_float = None

        if min_valor is not None and (valor_float is None or valor_float < min_valor):
            continue
        if max_valor is not None and (valor_float is None or valor_float > max_valor):
            continue

        filtered_items.append(item)

    items = filtered_items

    if not items:
        flash("Nenhum item encontrado com os filtros aplicados.", "warning")
        return render_template(
            template,
            itens=[],
            page=1,
            total_pages=1,
            title=title,
            add_route=url_for("add_item"),
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
        add_route=url_for("add_item"),
        next_url=request.url,
        total_relevant_transactions=total_relevant_transactions,
    )
