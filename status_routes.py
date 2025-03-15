import datetime
import uuid

from flask import (
    redirect,
    url_for,
    session,
    flash,
    request,
)

from utils import copy_image_in_s3


def init_status_routes(app, itens_table, manaus_tz):
    @app.route("/mark_returned/<item_id>", methods=["GET", "POST"])
    def mark_returned(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Obtém a data atual
        dev_date = datetime.datetime.now(manaus_tz).strftime("%Y-%m-%d")

        # Atualiza status para 'returned'
        # Atualiza status para 'returned' e insere data de devolução
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="set #status = :s, dev_date = :d",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": "returned", ":d": dev_date},
        )

        flash(
            "Item <a href='/returned'>devolvido</a> com sucesso.",
            "success",
        )
        return redirect(url_for("rented"))

    @app.route("/mark_archived/<item_id>", methods=["GET", "POST"])
    def mark_archived(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("rented"))

        # Obter o item completo do DynamoDB
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("returned"))

        # Criar uma cópia do item antes de qualquer modificação
        new_dress_id = str(uuid.uuid4())
        copied_item = item.copy()  # Copiar todos os campos do item original
        copied_item["item_id"] = new_dress_id
        copied_item["original_id"] = item_id
        copied_item["status"] = "historic"

        # Copiar imagem no S3 e atualizar a URL na cópia
        if copied_item.get("image_url", "") != "":
            copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

        # Salvar o novo item no DynamoDB
        itens_table.put_item(Item=copied_item)

        # Campos permitidos no item original
        allowed_fields = {
            "item_id",
            "description",
            "image_url",
            "valor",
            "user_id",
            "account_id",
        }

        # Filtrar o item original para manter apenas os campos permitidos
        filtered_item = {
            key: value for key, value in item.items() if key in allowed_fields
        }
        filtered_item["status"] = "archived"

        # Atualizar o item original no DynamoDB
        itens_table.put_item(Item=filtered_item)

        flash(
            "Item <a href='/archive'>arquivado</a> e registrado no <a href='/history'>histórico</a>.",
            "success",
        )
        return redirect(next_page)

    @app.route("/mark_available/<item_id>", methods=["GET", "POST"])
    def mark_available(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("rented"))

        # Obter o item completo do DynamoDB
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(url_for("returned"))

        if "archive" in next_page:
            # Atualiza status para 'returned' e insere data de devolução
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="set #status = :s",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":s": "available"},
            )
            flash(
                "Item marcado com <a href='/available'>disponível</a>.",
                "success",
            )
            return redirect(url_for("archive"))

        # Criar uma cópia do item antes de qualquer modificação
        new_dress_id = str(uuid.uuid4())
        copied_item = item.copy()  # Copiar todos os campos do item original
        copied_item["item_id"] = new_dress_id
        copied_item["original_id"] = item_id
        copied_item["status"] = "historic"

        # Copiar imagem no S3 e atualizar a URL na cópia
        if copied_item.get("image_url", "") != "":
            copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

        # Salvar o novo item no DynamoDB
        itens_table.put_item(Item=copied_item)

        # Campos permitidos no item original
        allowed_fields = {
            "item_id",
            "description",
            "image_url",
            "valor",
            "user_id",
            "account_id",
        }

        # Filtrar o item original para manter apenas os campos permitidos
        filtered_item = {
            key: value for key, value in item.items() if key in allowed_fields
        }
        filtered_item["status"] = "available"

        # Atualizar o item original no DynamoDB
        itens_table.put_item(Item=filtered_item)

        flash(
            "Item <a href='/available'>disponível</a> e registrado no <a href='/history'>histórico</a>.",
            "success",
        )

        return redirect(url_for("returned"))

    @app.route("/mark_rented/<item_id>", methods=["POST"])
    def mark_rented(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        # Atualiza status para 'rented'
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="set #status = :s",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":s": "rented"},
        )

        # Mensagem de sucesso
        flash(
            "Item marcado com alugado. Clique <a href='/rented'>aqui</a> para ver",
            "success",
        )
        return redirect(url_for("returned"))
