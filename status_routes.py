import datetime
import uuid

from flask import (
    redirect,
    url_for,
    session,
    flash,
    request,
)
from decimal import Decimal
from utils import get_user_timezone

from utils import copy_image_in_s3


def init_status_routes(app, itens_table, transactions_table, users_table):

    @app.route("/mark_returned/<transaction_id>", methods=["GET", "POST"])
    def mark_returned(transaction_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Obtém a data atual
        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)
        dev_date = datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S")

        # Atualiza status para 'returned', adiciona a data de devolução e marca como retirado=True
        transactions_table.update_item(
            Key={"transaction_id": transaction_id},
            UpdateExpression="SET #transaction_status = :s, dev_date = :d",
            ExpressionAttributeNames={
                "#transaction_status": "transaction_status",
            },
            ExpressionAttributeValues={
                ":s": "returned",
                ":d": dev_date,  # já definido anteriormente
            },
        )

        flash(
            "Item <a href='/returned'>devolvido</a> com sucesso.",
            "success",
        )
        return redirect(request.referrer or url_for("all_transactions"))

    @app.route("/mark_rented/<transaction_id>", methods=["GET", "POST"])
    def mark_rented(transaction_id):

        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("rented"))
        pago_total = request.args.get("pago_total")

        # Obtém data atual formatada
        user_id = session.get("user_id") if "user_id" in session else None
        user_utc = get_user_timezone(users_table, user_id)
        transaction_ret_date = datetime.datetime.now(user_utc).strftime("%Y-%m-%d %H:%M:%S")

        # Busca transação atual
        response = transactions_table.get_item(Key={"transaction_id": transaction_id})
        transaction = response.get("Item")


        print("value paid")
        print(transaction)

        if not transaction:
            flash("Transação não encontrada.", "danger")
            return redirect(next_page)

        update_expression = "SET #transaction_status = :s, transaction_ret_date = :d"
        expression_values = {
            ":s": "rented",
            ":d": transaction_ret_date,
        }
        expression_names = {
            "#transaction_status": "transaction_status",
        }

        # Se foi marcado como Pago Total
        if pago_total == "1":
            valor = transaction.get("transaction_price", Decimal("0.0"))
            print("the value")
            print(valor)
            update_expression += ", transaction_value_paid = :p"
            expression_values[":p"] = valor

        # Atualiza no DynamoDB
        transactions_table.update_item(
            Key={"transaction_id": transaction_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )

        flash("Item <a href='/rented'>retirado</a> com sucesso.", "success")
        return redirect(next_page)

    @app.route("/mark_archived/<item_id>", methods=["POST", "GET"])
    def mark_archived(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        next_page = request.args.get("next", url_for("rented"))

        try:
            # Atualiza apenas o campo "status"
            itens_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression="SET #st = :val",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":val": "archive"},
            )
            flash("Item arquivado com sucesso!", "success")
        except Exception as e:
            flash(f"Erro ao arquivar item: {str(e)}", "danger")

        return redirect(next_page)

    @app.route("/mark_available/<item_id>", methods=["GET", "POST"])
    def mark_available(item_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recuperar a página de origem (next)
        next_page = request.args.get("next", url_for("archive"))

        # Obter o item completo do DynamoDB
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            flash("Item não encontrado.", "danger")
            return redirect(next_page)

        # Atualizar o status do item para "available"
        itens_table.update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET #status = :s",
            ExpressionAttributeNames={
                "#status": "status"
            },  # 🔹 Evita palavra reservada
            ExpressionAttributeValues={":s": "available"},
        )

        flash(
            "Item agora está <a href='/inventory'>disponível</a> no seu inventário.",
            "success",
        )
        return redirect(next_page)
