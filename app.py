import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from urllib.parse import urlparse
import boto3
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from datetime import datetime, timedelta
from flask import request, url_for
from datetime import datetime
from datetime import datetime
import pytz
import uuid
import boto3
from urllib.parse import urlparse

# Define o fuso horário de Manaus
manaus_tz = pytz.timezone("America/Manaus")


load_dotenv()  # only for setting up the env as debug


app = Flask(__name__)

# Defina uma chave secreta forte e fixa
app.secret_key = os.environ.get("SECRET_KEY", "chave-secreta-estatica-e-forte-london")

# Garantir que o cookie de sessão seja válido para todo o domínio
# app.config['SESSION_COOKIE_DOMAIN'] = 'http://127.0.0.1:5000/'
# app.config['SESSION_COOKIE_PATH'] = '/'
# app.config['SESSION_COOKIE_SECURE'] = True  # se estiver usando HTTPS
# app.config['SESSION_COOKIE_SAMESITE'] = 'None'

# Configurações AWS
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

dynamodb_table_name = "WeddingDresses"
s3_bucket_name = "london-noivas-imagens"

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

table = dynamodb.Table(dynamodb_table_name)

s3 = boto3.client(
    "s3",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)


# Usuário e senha fixos para exemplo
USERNAME = "admin"
PASSWORD = "1234"


def upload_image_to_s3(image_file, prefix="images"):
    if image_file:
        filename = secure_filename(image_file.filename)
        dress_id = str(uuid.uuid4())
        s3_key = f"{prefix}/{dress_id}_{filename}"
        s3.upload_fileobj(image_file, s3_bucket_name, s3_key)
        image_url = f"https://{s3_bucket_name}.s3.amazonaws.com/{s3_key}"
        return image_url
    return ""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")
        if user == USERNAME and pwd == PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Usuário ou senha incorretos")
    return render_template("login.html")


def aplicar_filtro(
    items,
    filtro,
    today,
    description=None,
    client_name=None,
    payment_status=None,
    start_date=None,
    end_date=None,
    return_start_date=None,
    return_end_date=None,
    comments=None,
    formatted_dev_date=None,
    dev_date=None,
):
    """
    Filtra e processa a lista de itens de acordo com o filtro fornecido.

    :param items: Lista de itens a serem filtrados
    :param filtro: Filtro selecionado ("todos", "reservados", "retirados", "atrasados")
    :param today: Data atual para comparação
    :param client_name: Nome do cliente para filtro parcial
    :param payment_status: Status de pagamento ("nao pago", "pago 50%", "pago 100%")
    :param start_date: Data inicial para retirada
    :param end_date: Data final para retirada
    :param return_start_date: Data inicial para devolução
    :param return_end_date: Data final para devolução
    :return: Lista filtrada e processada de itens
    """
    for dress in items:
        # Processar return_date
        return_date_str = dress.get("return_date")
        if return_date_str:
            try:
                return_date = datetime.strptime(return_date_str, "%Y-%m-%d").date()
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
                rental_date = datetime.strptime(rental_date_str, "%Y-%m-%d").date()
                dress["rental_date_formatted"] = rental_date.strftime("%d-%m-%Y")
                dress["rental_date_obj"] = rental_date  # Para ordenação
            except ValueError:
                dress["rental_date_formatted"] = "Data Inválida"
                dress["rental_date_obj"] = today
        else:
            dress["rental_date_formatted"] = "N/A"
            dress["rental_date_obj"] = today

        # Processar dev_date
        if dress.get("dev_date"):

            try:
                # Converte a string no formato "YYYY-MM-DD" para um objeto datetime
                dev_date_obj = datetime.strptime(dress.get("dev_date"), "%Y-%m-%d")
                # Reformatar o objeto datetime para "DD-MM-YYYY"
                dress["dev_date"] = dev_date_obj.strftime("%d-%m-%Y")
            except ValueError:
                print("no dev_date")

    # Aplicar filtro principal
    if filtro == "reservados":
        filtered_items = [dress for dress in items if not dress.get("retirado", False)]
    elif filtro == "retirados":
        filtered_items = [dress for dress in items if dress.get("retirado", False)]
    elif filtro == "atrasados":
        filtered_items = [dress for dress in items if dress.get("overdue", False)]
    else:  # Default: "todos"
        filtered_items = items

    # Filtrar por descrição
    if description:
        filtered_items = [
            dress
            for dress in filtered_items
            if description.lower() in dress.get("description", "").lower()
        ]

    # Filtrar por comentários
    if comments:
        filtered_items = [
            dress
            for dress in filtered_items
            if comments.lower() in dress.get("comments", "").lower()
        ]

    # Filtrar por nome do cliente
    if client_name:
        filtered_items = [
            dress
            for dress in filtered_items
            if client_name.lower() in dress.get("client_name", "").lower()
        ]

    # Filtrar por status de pagamento
    if payment_status:
        filtered_items = [
            dress
            for dress in filtered_items
            if dress.get("pagamento", "").lower() == payment_status.lower()
        ]

    # Filtrar por intervalo de datas de retirada
    if start_date or end_date:
        filtered_items = [
            dress
            for dress in filtered_items
            if (not start_date or dress["rental_date_obj"] >= start_date)
            and (not end_date or dress["rental_date_obj"] <= end_date)
        ]

    # Need fixing ########################
    if dev_date:
        filtered_items = [dress for dress in filtered_items]

    return filtered_items


@app.route("/")
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Obter o filtro principal (default é "todos")
    filtro = request.args.get(
        "filter", "todos"
    )  # "todos", "reservados", "retirados", "atrasados"

    # Capturar parâmetros adicionais
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.strptime(return_start_date, "%Y-%m-%d").date()
    if return_end_date:
        return_end_date = datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "rented"
    response = table.scan(
        FilterExpression="attribute_not_exists(#status) OR #status = :status_rented",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_rented": "rented"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
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

    # Ordenar apenas pela data de registro (data de aluguel mais antiga primeiro)
    sorted_items = sorted(filtered_items, key=lambda x: x["rental_date_obj"])

    # Paginação
    total_items = len(sorted_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = sorted_items[start:end]

    return render_template(
        "index.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Vestidos Alugados",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/returned")
def returned():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")
    dev_date = request.args.get("dev_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.strptime(return_start_date, "%Y-%m-%d").date()
    if return_end_date:
        return_end_date = datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "returned"
    response = table.scan(
        FilterExpression="#status = :status_returned",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_returned": "returned"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
        dev_date=dev_date,
    )

    # Paginação
    # print(filtered_items)
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    return render_template(
        "returned.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Vestidos Devolvidos",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/history")
def history():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")
    dev_date = request.args.get("dev_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.strptime(return_start_date, "%Y-%m-%d").date()
    if return_end_date:
        return_end_date = datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "archived"
    response = table.scan(
        FilterExpression="#status = :status_archived",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_archived": "historic"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        payment_status=payment_status,
        start_date=start_date,
        end_date=end_date,
        return_start_date=return_start_date,
        return_end_date=return_end_date,
        dev_date=dev_date,
    )

    # Paginação
    total_items = len(filtered_items)
    total_pages = (total_items + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    paginated_items = filtered_items[start:end]

    return render_template(
        "history.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Histórico",
        add_route=url_for("add"),
        next_url=request.url,
    )


@app.route("/available")
def available():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    comments = request.args.get("comments")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.strptime(return_start_date, "%Y-%m-%d").date()
    if return_end_date:
        return_end_date = datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "available"
    response = table.scan(
        FilterExpression="#status = :status_available",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_available": "available"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        comments=comments,
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

    return render_template(
        "available.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Vestidos Disponíveis",
        add_route=url_for("add_small"),
        next_url=request.url,
    )


@app.route("/archive")
def archive():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Parâmetros de paginação
    page = int(request.args.get("page", 1))
    per_page = 5  # Número de itens por página

    # Capturar parâmetros adicionais
    filtro = request.args.get("filter", "todos")  # Default é "todos"
    description = request.args.get("description")
    comments = request.args.get("comments")
    client_name = request.args.get("client_name")
    payment_status = request.args.get("payment")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    return_start_date = request.args.get("return_start_date")
    return_end_date = request.args.get("return_end_date")

    # Converter intervalos de datas, se fornecidos
    if start_date:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    if return_start_date:
        return_start_date = datetime.strptime(return_start_date, "%Y-%m-%d").date()
    if return_end_date:
        return_end_date = datetime.strptime(return_end_date, "%Y-%m-%d").date()

    # Obter todos os registros "archived"
    response = table.scan(
        FilterExpression="#status = :status_archived",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_archived": "archived"},
    )
    items = response.get("Items", [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    # Aplicar filtro com todos os parâmetros
    filtered_items = aplicar_filtro(
        items,
        filtro,
        today,
        client_name=client_name,
        description=description,
        comments=comments,
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

    return render_template(
        "archive.html",
        dresses=paginated_items,
        page=page,
        total_pages=total_pages,
        current_filter=filtro,
        title="Arquivo",
        add_route=url_for("add_small"),
        next_url=request.url,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

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
            flash("Por favor, selecione o status do vestido.", "error")
            return render_template("add.html", next=next_page)

        # Validar e converter as datas
        try:
            rental_date = datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.strptime(return_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("add.html", next=next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o vestido (pode usar UUID)
        dress_id = str(uuid.uuid4())

        # Adicionar o novo vestido ao DynamoDB
        table.put_item(
            Item={
                "dress_id": dress_id,
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
                "status": status,  # Adiciona o status selecionado
            }
        )
        # Dicionário para mapear os valores a nomes associados
        status_map = {
            "rented": "Alugados",
            "returned": "Devolvidos",
            "historic": "Histórico",
        }

        flash(
            f"Vestido adicionado em <a href='{status}'>{status_map[status]}</a>.",
            "success",
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
            flash("Por favor, selecione o status do vestido.", "error")
            return render_template(next_page)

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Gerar um ID único para o vestido (pode usar UUID)
        dress_id = str(uuid.uuid4())

        # Adicionar o novo vestido ao DynamoDB
        table.put_item(
            Item={
                "dress_id": dress_id,
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

        flash("Vestido adicionado com sucesso.", "success")
        # Redirecionar para a página de origem
        return redirect(next_page)

    return render_template("add_small.html", next=next_page)


def copy_image_in_s3(original_url):
    import boto3
    from urllib.parse import urlparse
    import uuid

    # Inicializa o cliente S3
    s3 = boto3.client("s3")

    # Analisa a URL
    parsed_url = urlparse(original_url)
    if not parsed_url.netloc or not parsed_url.path:
        raise ValueError(f"URL inválida: {original_url}")

    # Extrai o nome do bucket e a chave original
    bucket_name = parsed_url.netloc.split(".")[0]
    original_key = parsed_url.path.lstrip("/")

    # Verifica se o bucket_name é válido
    if not bucket_name:
        raise ValueError("O nome do bucket é inválido ou está vazio.")

    # Cria uma nova chave para o arquivo copiado
    new_key = f"copies/{uuid.uuid4()}_{original_key.split('/')[-1]}"

    # Realiza a cópia do objeto
    s3.copy_object(
        CopySource={"Bucket": bucket_name, "Key": original_key},
        Bucket=bucket_name,
        Key=new_key,
    )

    # Retorna a URL da nova cópia
    return f"https://{bucket_name}.s3.amazonaws.com/{new_key}"


@app.route("/reports", methods=["GET", "POST"])
def reports():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Valores padrão para data inicial e final (últimos 30 dias)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=30)

    if request.method == "POST":
        try:
            start_date = datetime.strptime(
                request.form.get("start_date"), "%Y-%m-%d"
            ).date()
            end_date = datetime.strptime(
                request.form.get("end_date"), "%Y-%m-%d"
            ).date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template(
                "reports.html",
                total_paid=0,
                total_due=0,
                total_general=0,
                start_date=start_date,
                end_date=end_date,
            )

    # Obter todos os registros (independente do status)
    response = table.scan()
    items = response.get("Items", [])

    # Inicializar os totais
    total_paid = 0  # Total recebido
    total_due = 0  # Total a receber

    for dress in items:
        try:
            # Considerar apenas registros dentro do período
            rental_date = datetime.strptime(dress.get("rental_date"), "%Y-%m-%d").date()
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


@app.route("/edit/<dress_id>", methods=["GET", "POST"])
def edit(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        status = request.form.get("status")
        rental_date_str = request.form.get("rental_date")
        return_date_str = request.form.get("return_date")
        dev_date = request.form.get("dev_date")
        description = request.form.get("description").strip()
        client_name = request.form.get("client_name")
        client_tel = request.form.get("client_tel")
        retirado = "retirado" in request.form  # Verifica presença do checkbox
        valor = request.form.get("valor")
        pagamento = request.form.get("pagamento")
        comments = request.form.get("comments").strip()
        image_file = request.files.get("image_file")

        # Validar e converter as datas
        try:
            rental_date = datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.strptime(return_date_str, "%Y-%m-%d").date()
            # dev_date = datetime.strptime(dev_date, "%Y-%m-%d").date()

        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("edit.html")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
                    dev_date =:dd,
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
                ":st": status,
                ":dd": dev_date,
            },
        )

        flash("Vestido atualizado com sucesso.", "success")
        return redirect(next_page)

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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

    return render_template("edit.html", dress=dress)


@app.route("/rent/<dress_id>", methods=["GET", "POST"])
def rent(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
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
            rental_date = datetime.strptime(rental_date_str, "%Y-%m-%d").date()
            return_date = datetime.strptime(return_date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Formato de data inválido. Use AAAA-MM-DD.", "error")
            return render_template("edit.html")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
            "Item <a href='/'>alugado</a> com sucesso!",
            "success",
        )
        return redirect(url_for("available"))

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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

    return render_template("rent.html", dress=dress)


@app.route("/edit_small/<dress_id>", methods=["GET", "POST"])
def edit_small(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_page = request.args.get("next", url_for("index"))

    # Buscar item existente
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")
    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("available"))

    if request.method == "POST":
        rental_date_str = None
        return_date_str = None
        description = request.form.get("description").strip()
        client_name = None
        client_tel = None
        retirado = None
        valor = request.form.get("valor")
        pagamento = None
        comments = request.form.get("comments").strip()
        image_file = request.files.get("image_file")

        # Fazer upload da imagem, se houver
        new_image_url = item.get("image_url", "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(
                image_file
            )  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={"dress_id": dress_id},
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
                    pagamento = :pag
            """,
            ExpressionAttributeValues={
                ":r": rental_date_str,
                ":rt": return_date_str,
                ":c": comments,
                ":i": new_image_url,
                ":dc": description,
                ":cn": client_name,
                ":ct": client_tel,
                ":ret": retirado,
                ":val": valor,
                ":pag": pagamento,
            },
        )

        flash("Vestido atualizado com sucesso.", "success")
        # Redirecionar de acordo com o status atual

        return redirect(next_page)

    # Preparar dados para o template
    dress = {
        "dress_id": item.get("dress_id"),
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

    return render_template("edit_small.html", dress=dress)


@app.route("/delete/<dress_id>", methods=["POST"])
def delete(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    try:
        # Obter o item antes de deletar
        response = table.get_item(Key={"dress_id": dress_id})
        item = response.get("Item")
        if item:
            image_url = item.get("image_url")

            # Se existe image_url, tentar deletar o objeto no S3
            if image_url and image_url.strip():
                parsed_url = urlparse(image_url)
                object_key = parsed_url.path.lstrip(
                    "/"
                )  # Extrair a chave do objeto no S3
                # Deletar o objeto do S3
                s3.delete_object(Bucket=s3_bucket_name, Key=object_key)

            # Apagar registro no DynamoDB
            table.delete_item(Key={"dress_id": dress_id})
            flash("Item deletado com sucesso! ", "success")  # Mensagem de sucesso
        else:
            flash(
                "Vestido não encontrado.", "error"
            )  # Mensagem de erro para vestido inexistente

    except Exception as e:
        # Registrar ou tratar o erro aqui, se necessário
        flash(
            f"Ocorreu um erro ao tentar deletar o vestido: {str(e)}", "error"
        )  # Mensagem de erro

    # Redirecionar para a página anterior (index ou returned)

    return redirect(next_page)


@app.route("/mark_returned/<dress_id>", methods=["GET", "POST"])
def mark_returned(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Obtém a data atual
    dev_date = datetime.now(manaus_tz).strftime("%Y-%m-%d")

    # Atualiza status para 'returned'
    # Atualiza status para 'returned' e insere data de devolução
    table.update_item(
        Key={"dress_id": dress_id},
        UpdateExpression="set #status = :s, dev_date = :d",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "returned", ":d": dev_date},
    )

    # flash("Vestido devolvido com sucesso.", "success")

    flash(
        "Item <a href='/returned'>devolvido</a> com sucesso.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/mark_archived/<dress_id>", methods=["GET", "POST"])
def mark_archived(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")

    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("returned"))

    # Criar uma cópia do item antes de qualquer modificação
    new_dress_id = str(uuid.uuid4())
    copied_item = item.copy()  # Copiar todos os campos do item original
    copied_item["dress_id"] = new_dress_id
    copied_item["original_id"] = dress_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] != "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"dress_id", "description", "image_url", "valor"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "archived"

    # Atualizar o item original no DynamoDB
    table.put_item(Item=filtered_item)

    flash(
        "Item <a href='/archive'>arquivado</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )
    return redirect(next_page)


@app.route("/mark_available/<dress_id>", methods=["GET", "POST"])
def mark_available(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    # Recuperar a página de origem (next)
    next_page = request.args.get("next", url_for("index"))

    # Obter o item completo do DynamoDB
    response = table.get_item(Key={"dress_id": dress_id})
    item = response.get("Item")

    if not item:
        flash("Vestido não encontrado.", "error")
        return redirect(url_for("returned"))

    if "archive" in next_page:
        # Atualiza status para 'returned' e insere data de devolução
        table.update_item(
            Key={"dress_id": dress_id},
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
    copied_item["dress_id"] = new_dress_id
    copied_item["original_id"] = dress_id
    copied_item["status"] = "historic"

    # Copiar imagem no S3 e atualizar a URL na cópia
    if copied_item["image_url"] is not "":
        copied_item["image_url"] = copy_image_in_s3(copied_item["image_url"])

    # Salvar o novo item no DynamoDB
    table.put_item(Item=copied_item)

    # Campos permitidos no item original
    allowed_fields = {"dress_id", "description", "image_url", "valor"}

    # Filtrar o item original para manter apenas os campos permitidos
    filtered_item = {key: value for key, value in item.items() if key in allowed_fields}
    filtered_item["status"] = "available"

    # Atualizar o item original no DynamoDB
    table.put_item(Item=filtered_item)

    # flash("Vestido está disponível agora e cópia histórica criada.", "success")

    flash(
        "Item <a href='/available'>disponível</a> e registrado no <a href='/history'>histórico</a>.",
        "success",
    )

    return redirect(url_for("returned"))


@app.route("/mark_rented/<dress_id>", methods=["POST"])
def mark_rented(dress_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    # Atualiza status para 'rented'
    table.update_item(
        Key={"dress_id": dress_id},
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


# Rota de Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    # Determina se está no localhost
    debug_mode = os.getenv("debug_env").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
