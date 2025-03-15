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

ALLOWED_EXTENSIONS = {"jpeg", "jpg", "png", "gif", "webp"}


def allowed_file(filename):
    """Verifica se o arquivo tem uma extensão válida."""
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


from utils import upload_image_to_s3, aplicar_filtro, copy_image_in_s3


def init_item_routes(app, itens_table, s3, s3_bucket_name):

    @app.route("/rented")
    def rented():
        return listar_itens(["rented"], "rented.html", "Itens Alugados", itens_table)

    @app.route("/returned")
    def returned():
        return listar_itens(
            ["returned"], "returned.html", "Itens Devolvidos", itens_table
        )

    @app.route("/history")
    def history():
        return listar_itens(
            ["historic"], "history.html", "Histórico de Aluguéis", itens_table
        )

    @app.route("/available")
    def available():
        return listar_itens(
            ["available"], "available.html", "Itens Disponíveis", itens_table
        )

    @app.route("/archive")
    def archive():
        return listar_itens(
            ["archived"], "archive.html", "Itens Arquivados", itens_table
        )

    @app.route("/trash")
    def trash():
        return listar_itens(
            ["deleted", "version"], "trash.html", "Histórico de alterações", itens_table
        )

    @app.route("/add", methods=["GET", "POST"])
    def add():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("index"))

        # Obter o user_id e account_id do usuário logado da sessão
        user_id = session.get("user_id")
        account_id = session.get("account_id")

        if request.method == "POST":
            # Capturar dados do formulário
            status = request.form.get(
                "status"
            )  # Captura o status: rented, returned, available
            description = request.form.get("description").strip()
            client_name = request.form.get("client_name")
            client_tel = request.form.get("client_tel")
            rental_date_str = request.form.get("rental_date")
            return_date_str = request.form.get("return_date")
            retirado = "retirado" in request.form  # Verifica se o checkbox está marcado
            valor = request.form.get("valor")
            pagamento = request.form.get("pagamento")
            comments = request.form.get("comments").strip()
            image_file = request.files.get("image_file")

            # Validar se o status foi escolhido
            if status not in ["rented", "returned", "historic"]:
                flash("Por favor, selecione o status do item.", "danger")
                return render_template("add.html", next=next_page)

            # Validar e converter as datas
            try:
                rental_date = datetime.datetime.strptime(
                    rental_date_str, "%Y-%m-%d"
                ).date()
                return_date = datetime.datetime.strptime(
                    return_date_str, "%Y-%m-%d"
                ).date()
            except ValueError:
                flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
                return render_template("add.html", next=next_page)

            # Validar e fazer upload da imagem, se houver
            image_url = ""
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
                    "client_name": client_name,
                    "client_tel": client_tel,
                    "rental_date": rental_date.strftime("%Y-%m-%d"),
                    "return_date": return_date.strftime("%Y-%m-%d"),
                    "retirado": retirado,
                    "comments": comments,
                    "valor": valor,
                    "pagamento": pagamento,
                    "image_url": image_url,
                    "status": status,
                    "previous_status": status,
                }
            )
            # Dicionário para mapear os valores a nomes associados
            status_map = {
                "rented": "Alugados",
                "returned": "Devolvidos",
                "historic": "Histórico",
            }

            flash(
                f"Item adicionado em <a href='{status}'>{status_map[status]}</a>.",
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

    @app.route("/add_small", methods=["GET", "POST"])
    def add_small():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("index"))

        # Obter o user_id e account_id do usuário logado da sessão
        user_id = session.get("user_id")
        account_id = session.get("account_id")

        if request.method == "POST":
            # Capturar dados do formulário
            status = "archived" if "archive" in next_page else "available"
            description = request.form.get("description").strip()
            client_name = None
            client_tel = None
            rental_date_str = None
            return_date_str = None
            retirado = None  # Verifica se o checkbox está marcado
            valor = request.form.get("valor")
            pagamento = None
            comments = request.form.get("comments")
            image_file = request.files.get("image_file")

            # Validar se o status foi escolhido
            if status not in ["rented", "returned", "available", "archived"]:
                flash("Por favor, selecione o status do item.", "danger")
                return render_template(next_page)

            image_url = ""
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    image_url = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Gerar um ID único para o item (pode usar UUID)
            item_id = str(uuid.uuid4())

            # Adicionar o novo item ao DynamoDB
            itens_table.put_item(
                Item={
                    "user_id": user_id,
                    "account_id": account_id,
                    "item_id": item_id,
                    "description": description,
                    "client_name": client_name,
                    "client_tel": client_tel,
                    "rental_date": rental_date_str,
                    "return_date": return_date_str,
                    "retirado": retirado,
                    "comments": comments,
                    "valor": valor,
                    "pagamento": pagamento,
                    "image_url": image_url,
                    "status": status,  # Adiciona o status selecionado
                }
            )

            flash("Item adicionado com sucesso.", "success")
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG e WEBP.",
                    "danger",
                )

            return redirect(next_page)

        return render_template("add_small.html", next=next_page)

    @app.route("/edit/<item_id>", methods=["GET", "POST"])
    def edit(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        # Buscar item existente
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("index"))

        if request.method == "POST":
            # Obter novos dados do formulário
            new_data = {
                "status": request.form.get("status") or None,
                "rental_date": request.form.get("rental_date") or None,
                "return_date": request.form.get("return_date") or None,
                "dev_date": request.form.get("dev_date") or None,
                "description": request.form.get("description", "").strip() or None,
                "client_name": request.form.get("client_name") or None,
                "client_tel": request.form.get("client_tel") or None,
                "retirado": "retirado" in request.form,  # Checkbox
                "valor": request.form.get("valor", "").strip() or None,
                "pagamento": request.form.get("pagamento") or None,
                "comments": request.form.get("comments", "").strip() or None,
                "image_url": item.get(
                    "image_url", ""
                ),  # Manter valor antigo se não houver upload
            }

            # Fazer upload da imagem, se houver
            image_file = request.files.get("image_file")
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    new_data["image_url"] = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Converter datas para o formato correto
            if new_data["rental_date"] and isinstance(
                new_data["rental_date"], datetime.date
            ):
                new_data["rental_date"] = new_data["rental_date"].strftime("%Y-%m-%d")

            if new_data["return_date"] and isinstance(
                new_data["return_date"], datetime.date
            ):
                new_data["return_date"] = new_data["return_date"].strftime("%Y-%m-%d")

            if new_data["dev_date"] and isinstance(new_data["dev_date"], datetime.date):
                new_data["dev_date"] = new_data["dev_date"].strftime("%Y-%m-%d")

            # Comparar novos valores com os antigos
            changes = {
                key: value for key, value in new_data.items() if item.get(key) != value
            }

            if not changes:  # Se não houver mudanças, apenas exibir a mensagem e sair
                flash("Nenhuma alteração foi feita.", "warning")
                if "image_not_allowed" in locals() and image_not_allowed:
                    flash(
                        "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG, ou WEBP.",
                        "danger",
                    )
                return redirect(next_page)

            # Criar cópia do item somente se houver mudanças
            new_item_id = str(uuid.uuid4())
            edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            copied_item = {
                key: value
                for key, value in item.items()
                if key != "item_id" and value not in [None, ""]
            }
            copied_item["item_id"] = new_item_id
            copied_item["parent_item_id"] = item.get("item_id", "")
            copied_item["status"] = "version"
            copied_item["edited_date"] = edited_date
            copied_item["edited_by"] = session.get("username")
            copied_item["previous_status"] = item.get("status")

            # Salvar a cópia no DynamoDB
            itens_table.put_item(Item=copied_item)

            # Criar dinamicamente os updates para evitar erro com valores vazios
            update_expression = []
            expression_values = {}

            for key, value in changes.items():
                alias = f":{key[:2]}"  # Criar alias para valores
                update_expression.append(f"{key} = {alias}")
                expression_values[alias] = value

            # Atualizar o item original apenas se houver mudanças
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET " + ", ".join(update_expression),
                ExpressionAttributeValues=expression_values,
            )

            flash("Item atualizado com sucesso.", "success")
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida par imagem. Use apenas JPEG, PNG, ou WEBP.",
                    "danger",
                )
            return redirect(next_page)

        # Preparar dados para o template
        dress = {
            "item_id": item.get("item_id"),
            "description": item.get("description"),
            "client_name": item.get("client_name"),
            "client_tel": item.get("client_tel"),
            "rental_date": item.get("rental_date"),
            "return_date": item.get("return_date"),
            "dev_date": item.get("dev_date"),
            "comments": item.get("comments"),
            "image_url": item.get("image_url"),
            "retirado": item.get("retirado", False),
            "valor": item.get("valor"),
            "pagamento": item.get("pagamento"),
            "status": item.get("status"),
        }

        return render_template("edit.html", item=item)

    @app.route("/edit_small/<item_id>", methods=["GET", "POST"])
    def edit_small(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("index"))

        # Buscar item existente
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("available"))

        if request.method == "POST":
            # Obter novos dados do formulário
            new_data = {
                "rental_date": request.form.get("rental_date") or None,
                "return_date": request.form.get("return_date") or None,
                "description": request.form.get("description", "").strip() or None,
                "client_name": request.form.get("client_name") or None,
                "client_tel": request.form.get("client_tel") or None,
                "retirado": request.form.get("retirado") or None,
                "valor": request.form.get("valor", "").strip() or None,
                "pagamento": request.form.get("pagamento") or None,
                "comments": request.form.get("comments", "").strip() or None,
                "image_url": item.get(
                    "image_url", ""
                ),  # Mantém o valor antigo por padrão
            }

            # Fazer upload da imagem, se houver
            image_file = request.files.get("image_file")
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    new_data["image_url"] = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Comparar novos valores com os antigos
            changes = {
                key: value for key, value in new_data.items() if item.get(key) != value
            }

            if (
                not changes
            ):  # Se não houver mudanças, exibir a mensagem e não salvar nada
                flash("Nenhuma alteração foi feita.", "warning")
                if "image_not_allowed" in locals() and image_not_allowed:
                    flash(
                        "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG, ou WEBP.",
                        "danger",
                    )
                return redirect(next_page)

            # Criar cópia do item somente se houver mudanças
            new_item_id = str(uuid.uuid4())
            edited_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            copied_item = {
                key: value
                for key, value in item.items()
                if key != "item_id" and value not in [None, ""]
            }
            copied_item["item_id"] = new_item_id
            copied_item["previous_status"] = item.get("status")
            copied_item["parent_item_id"] = item.get("item_id", "")
            copied_item["status"] = "version"
            copied_item["edited_date"] = edited_date
            copied_item["deleted_by"] = session.get("username")
            copied_item["previous_status"] = item.get("status")

            # Salvar a cópia no DynamoDB
            itens_table.put_item(Item=copied_item)

            # Criar dinamicamente os updates para evitar erro com valores vazios
            update_expression = []
            expression_values = {}

            for key, value in changes.items():
                alias = f":{key[:2]}"  # Criar alias para valores
                update_expression.append(f"{key} = {alias}")
                expression_values[alias] = value

            # Atualizar apenas se houver mudanças
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET " + ", ".join(update_expression),
                ExpressionAttributeValues=expression_values,
            )

            flash("Item atualizado com sucesso.", "success")
            if "image_not_allowed" in locals() and image_not_allowed:
                flash(
                    "Extensão de arquivo não permitida para imagem. Use apenas JPEG, PNG, ou WEBP.",
                    "danger",
                )
            return redirect(next_page)

        # Preparar dados para o template
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

    @app.route("/rent/<item_id>", methods=["GET", "POST"])
    def rent(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Buscar item existente
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("available"))

        if request.method == "POST":
            rental_date_str = request.form.get("rental_date")
            return_date_str = request.form.get("return_date")
            description = request.form.get("description")
            client_name = request.form.get("client_name")
            client_tel = request.form.get("client_tel")
            retirado = "retirado" in request.form  # Verifica presença do checkbox
            valor = request.form.get("valor")
            pagamento = request.form.get("pagamento")
            comments = request.form.get("comments")
            image_file = request.files.get("image_file")

            # Validar e converter as datas
            try:
                rental_date = datetime.datetime.strptime(
                    rental_date_str, "%Y-%m-%d"
                ).date()
                return_date = datetime.datetime.strptime(
                    return_date_str, "%Y-%m-%d"
                ).date()
            except ValueError:
                flash("Formato de data inválido. Use AAAA-MM-DD.", "danger")
                return render_template("edit.html")

            # Fazer upload da imagem, se houver
            new_image_url = ""
            image_file = request.files.get("image_file")
            if image_file and image_file.filename != "":
                if allowed_file(image_file.filename):
                    new_image_url = upload_image_to_s3(image_file)
                else:
                    image_not_allowed = True

            # Atualizar item no DynamoDB
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="""
                    set rental_date = :r,
                        return_date = :rt,
                        comments = :c,
                        image_url = :i,
                        description = :dc,
                        client_name = :cn,
                        client_tel = :ct,
                        retirado = :ret,
                        valor = :val,
                        pagamento = :pag,
                        #status = :st
                """,
                ExpressionAttributeNames={
                    "#status": "status"  # Define um alias para o atributo reservado
                },
                ExpressionAttributeValues={
                    ":r": rental_date.strftime("%Y-%m-%d"),
                    ":rt": return_date.strftime("%Y-%m-%d"),
                    ":c": comments,
                    ":i": new_image_url,
                    ":dc": description,
                    ":cn": client_name,
                    ":ct": client_tel,
                    ":ret": retirado,
                    ":val": valor,
                    ":pag": pagamento,
                    ":st": "rented",
                },
            )

            flash(
                "Item <a href='/rented'>alugado</a> com sucesso!",
                "success",
            )
            if "image_not_allowed" in locals() and image_not_allowed:
                flash("Extensão  eimagem não permitida", "danger")

            return redirect(url_for("available"))

        # Preparar dados para o template
        dress = {
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

        return render_template("rent.html", item=item)

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

    @app.route("/restore_version_item", methods=["POST"])
    def restore_version_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        item_id = request.form.get("item_id")
        next_page = request.args.get("next", url_for("trash"))

        try:
            # 🔹 Obter o item atual no banco (item versão)
            response = itens_table.get_item(Key={"item_id": item_id})
            item_data = response.get("Item")

            if not item_data:
                flash("Item não encontrado.", "danger")
                return redirect(next_page)

            # 🔹 Verificar se o item é uma versão
            current_status = item_data.get("status")

            if current_status != "version":
                flash("Este item não é uma versão para restauração.", "danger")
                return redirect(next_page)

            # 🔹 Obter o item pai
            parent_item_id = item_data.get("parent_item_id")
            if not parent_item_id:
                flash("Item não possui um item pai associado.", "danger")
                return redirect(next_page)

            parent_response = itens_table.get_item(Key={"item_id": parent_item_id})
            parent_data = parent_response.get("Item")

            if not parent_data:
                flash("Item pai não encontrado.", "danger")
                return redirect(next_page)

            # 🔹 Verificar o status do item pai
            parent_status = parent_data.get("status")

            # Se o item pai estiver deletado, restauramos o status
            if parent_status == "deleted":
                previous_status = parent_data.get("previous_status", "available")
                itens_table.update_item(
                    Key={"item_id": parent_item_id},
                    UpdateExpression="SET #status = :prev_status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":prev_status": previous_status},
                )

            # 🔹 Trocar todos os campos, exceto item_id, previous_status e status
            swap_fields = [
                field
                for field in item_data.keys()
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

                expression_values_parent[f":v_{field}"] = item_data.get(field, "")
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

            if parent_status == "deleted":
                flash(
                    "O item pai estava deletado e foi restaurado para seu status anterior. Além disso, todos os campos foram trocados.",
                    "success",
                )
            else:
                flash(
                    "Item versão restaurado com sucesso. Todos os campos foram trocados.",
                    "success",
                )

            return redirect(next_page)

        except Exception as e:
            flash(f"Erro ao restaurar a versão do item: {str(e)}", "danger")
            return redirect(next_page)

    # Para restaurar itens deletados, apenas mudamos seu status para o anterior
    @app.route("/restore_deleted_item", methods=["POST"])
    def restore_deleted_item():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        item_id = request.form.get("item_id")

        # 🔹 Buscar o item no banco de dados para obter o previous_status
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash(f"Erro: Item {item_id} não encontrado.", "danger")
            return redirect(url_for("trash"))

        # 🔹 Obter o previous_status salvo no banco
        previous_status = item.get("previous_status")
        if not previous_status:
            flash(f"Erro: O item {item_id} não tem um status anterior salvo.", "danger")
            return redirect(url_for("trash"))

        # 🔹 Atualizar o status do item para o previous_status
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET #status = :previous_status",
            ExpressionAttributeNames={
                "#status": "status"
            },  # Evita conflito com palavra reservada
            ExpressionAttributeValues={":previous_status": previous_status},
        )

        flash(
            f"Item restaurado com sucesso para o status '{previous_status}'.",
            "success",
        )
        return redirect(url_for("trash"))

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

        response = itens_table.query(
            IndexName="account_id-index",  # Usando o GSI para buscar por account_id
            KeyConditionExpression="#account_id = :account_id",
            FilterExpression="#status IN (:rented, :returned, :archived, :history)",
            ExpressionAttributeNames={"#account_id": "account_id", "#status": "status"},
            ExpressionAttributeValues={
                ":account_id": account_id,
                ":rented": "rented",
                ":returned": "returned",
                ":archived": "archived",
                ":history": "history",
            },
        )

        items = response.get("Items", [])

        # Inicializar os totais
        total_paid = 0  # Total recebido
        total_due = 0  # Total a receber

        for dress in items:
            try:
                # Considerar apenas registros dentro do período
                rental_date = datetime.datetime.strptime(
                    dress.get("rental_date"), "%Y-%m-%d"
                ).date()
                if start_date <= rental_date <= end_date:
                    valor = float(dress.get("valor", 0))
                    pagamento = dress.get("pagamento", "").lower()

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

        return render_template(
            "reports.html",
            total_paid=total_paid,
            total_due=total_due,
            total_general=total_general,
            start_date=start_date,
            end_date=end_date,
        )


def listar_itens(status_list, template, title, itens_table):
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

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

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

    # Fazer a consulta usando o GSI account_id-index
    response = itens_table.query(
        IndexName="account_id-index",  # Usando o GSI
        KeyConditionExpression="#account_id = :account_id",
        ExpressionAttributeNames={"#account_id": "account_id"},
        ExpressionAttributeValues={":account_id": account_id},
    )

    items = response.get("Items", [])
    today = datetime.datetime.now().date()

    # Aplicar filtros adicionais (ex: status, datas, etc.)
    filtered_items = [item for item in items if item.get("status") in status_list]

    # Aplicar filtros extras (datas, descrição, pagamento, etc.)
    filtered_items = aplicar_filtro(
        filtered_items,
        filtro,
        today,
        description=description,
        client_name=client_name,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
    )

    # Paginação
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    if template == "available.html":
        add_template = "add_small"
    else:
        add_template = "add"

    return render_template(
        template,
        itens=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title=title,
        add_route=url_for(add_template),
        next_url=request.url,
    )
