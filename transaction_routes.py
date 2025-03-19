from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)
import datetime


def init_transaction_routes(
    app, itens_table, s3, s3_bucket_name, transactions_table, clients_table
):
    @app.route("/delete_transaction/<transaction_id>", methods=["POST"])
    def delete_transaction(transaction_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        deleted_by = session.get("username")
        next_page = request.args.get("next", url_for("index"))

        try:
            # 🔹 Obter a transação antes de modificar
            response = transactions_table.get_item(
                Key={"transaction_id": transaction_id}
            )
            transaction = response.get("Item")

            if not transaction:
                flash("Transação não encontrada.", "danger")
                return redirect(next_page)

            current_status = transaction.get("status")  # 🔹 Captura o status atual
            deleted_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            # 🔹 Atualizar o `previous_status` primeiro
            transactions_table.update_item(
                Key={"transaction_id": transaction_id},
                UpdateExpression="SET previous_status = :current_status",
                ExpressionAttributeValues={":current_status": current_status},
            )

            # 🔹 Agora alterar o status para "deleted"
            transactions_table.update_item(
                Key={"transaction_id": transaction_id},
                UpdateExpression="SET #status = :deleted, deleted_date = :deleted_date, deleted_by = :deleted_by",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":deleted": "deleted",
                    ":deleted_date": deleted_date,
                    ":deleted_by": deleted_by,
                },
            )

            flash(
                "Transação marcada como deletada. Ela ficará disponível na 'lixeira' por 30 dias.",
                "success",
            )

        except Exception as e:
            flash(f"Ocorreu um erro ao tentar deletar a transação: {str(e)}", "danger")

        return redirect(next_page)
