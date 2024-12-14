import os
import uuid
from flask import Flask, render_template, request, redirect, url_for, session
import boto3
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', '123456')

# Configurações AWS
aws_region='us-east-1'
aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')

dynamodb_table_name = 'WeddingDresses'
s3_bucket_name = 'london-noivas-imagens'

dynamodb = boto3.resource('dynamodb', 
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
    )

table = dynamodb.Table(dynamodb_table_name)

s3 = boto3.client('s3', region_name=aws_region,
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

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('username')
        pwd = request.form.get('password')
        if user == USERNAME and pwd == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Usuário ou senha incorretos")
    return render_template('login.html')

from datetime import datetime

@app.route('/index')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    # Obter todos os registros "rented"
    response = table.scan(
        FilterExpression="attribute_not_exists(#status) OR #status = :status_rented",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_rented": "rented"}
    )
    items = response.get('Items', [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    for dress in items:
        return_date_str = dress.get('return_date')
        # Assumindo que a data é armazenada como 'YYYY-MM-DD' no DynamoDB
        # Se o formato for diferente, ajuste o strptime conforme necessário
        if return_date_str:
            return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
            # Se a data de devolução já passou
            dress['overdue'] = return_date < today
        else:
            # Se não há data de devolução, considerar não atrasado (ou defina conforme lógica)
            dress['overdue'] = False

    return render_template('index.html', dresses=items)

@app.route('/returned')
def returned():
    # Exibe vestidos devolvidos (status = "returned")
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    response = table.scan(
        FilterExpression="#status = :status_returned",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_returned": "returned"}
    )
    items = response.get('Items', [])
    return render_template('returned.html', dresses=items)

@app.route('/add', methods=['GET', 'POST'])
def add():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        rental_date = request.form.get('rental_date')
        return_date = request.form.get('return_date')
        comments = request.form.get('comments')
        image_file = request.files.get('image_file')
        client_name = request.form.get('client_name')


        image_url = upload_image_to_s3(image_file)

        # Inserir registro no DynamoDB
        item = {
            'dress_id': str(uuid.uuid4()),
            'client_name': client_name,
            'rental_date': rental_date,
            'return_date': return_date,
            'comments': comments,
            'image_url': image_url,
            'status': 'rented'  # por padrão, recém inserido é alugado
        }
        table.put_item(Item=item)
        return redirect(url_for('index'))

    return render_template('add.html')

@app.route('/edit/<dress_id>', methods=['GET', 'POST'])
def edit(dress_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Buscar item existente
    response = table.get_item(Key={'dress_id': dress_id})
    item = response.get('Item')
    if not item:
        return "Item não encontrado", 404

    if request.method == 'POST':
        rental_date = request.form.get('rental_date')
        client_name = request.form.get('client_name')
        return_date = request.form.get('return_date')
        comments = request.form.get('comments')
        image_file = request.files.get('image_file')

        new_image_url = item.get('image_url', "")
        if image_file and image_file.filename != "":
            # Se subiu nova imagem, atualiza
            new_image_url = upload_image_to_s3(image_file)

        # Atualizar item no DynamoDB
        table.update_item(
            Key={'dress_id': dress_id},
            UpdateExpression="set rental_date = :r, return_date = :rt, comments = :c, image_url = :i, client_name = :cn",
            ExpressionAttributeValues={
                ':r': rental_date,
                ':rt': return_date,
                ':c': comments,
                ':i': new_image_url,
                ':cn': client_name
            }
        )
        # Redirecionar de acordo com o status atual
        if item.get('status') == 'returned':
            return redirect(url_for('returned'))
        else:
            return redirect(url_for('index'))

    return render_template('edit.html', dress=item)

@app.route('/delete/<dress_id>', methods=['POST'])
def delete(dress_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Obter o item antes de deletar
    response = table.get_item(Key={'dress_id': dress_id})
    item = response.get('Item')
    if item:
        image_url = item.get('image_url')
        
        # Se existe image_url, tentar deletar o objeto no S3
        if image_url and image_url.strip():
            # A URL deve ser algo como https://bucket.s3.amazonaws.com/images/... 
            # Extraia a parte depois do nome do bucket
            # Ex: https://london-noivas-imagens.s3.amazonaws.com/images/abc123.png
            # Chave do objeto: images/abc123.png
            from urllib.parse import urlparse
            parsed_url = urlparse(image_url)
            # path do objeto é parsed_url.path, mas costuma começar com '/'
            # Ex: /images/abc123.png
            object_key = parsed_url.path.lstrip('/')
            
            # Deletar objeto do S3
            s3.delete_object(Bucket=s3_bucket_name, Key=object_key)

        # Apagar registro no DynamoDB
        table.delete_item(Key={'dress_id': dress_id})
    # Redirecionar para a página anterior (index ou returned)
    prev = request.referrer
    if "/returned" in prev:
        return redirect(url_for('returned'))
    else:
        return redirect(url_for('index'))


@app.route('/mark_returned/<dress_id>', methods=['POST'])
def mark_returned(dress_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # Atualiza status para 'returned'
    table.update_item(
        Key={'dress_id': dress_id},
        UpdateExpression="set #status = :s",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={':s': 'returned'}
    )
    return redirect(url_for('index'))

@app.route('/mark_rented/<dress_id>', methods=['POST'])
def mark_rented(dress_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # Atualiza status para 'rented'
    table.update_item(
        Key={'dress_id': dress_id},
        UpdateExpression="set #status = :s",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={':s': 'rented'}
    )
    return redirect(url_for('returned'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
