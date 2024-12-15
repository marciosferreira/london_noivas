import os
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, flash
from urllib.parse import urlparse
import boto3
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Defina uma chave secreta forte e fixa
app.secret_key = os.environ.get('SECRET_KEY', 'chave-secreta-estatica-e-forte-london')

# Garantir que o cookie de sessão seja válido para todo o domínio
#app.config['SESSION_COOKIE_DOMAIN'] = 'http://127.0.0.1:5000/'
#app.config['SESSION_COOKIE_PATH'] = '/'
#app.config['SESSION_COOKIE_SECURE'] = True  # se estiver usando HTTPS
#app.config['SESSION_COOKIE_SAMESITE'] = 'None'

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

@app.route('/login', methods=['GET', 'POST'])
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

@app.route('/')
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
        # Processar return_date
        return_date_str = dress.get('return_date')
        if return_date_str:
            try:
                return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
                dress['overdue'] = return_date < today
                dress['return_date_formatted'] = return_date.strftime('%d-%m-%Y')
            except ValueError:
                dress['overdue'] = False
                dress['return_date_formatted'] = 'Data Inválida'
        else:
            dress['overdue'] = False
            dress['return_date_formatted'] = 'N/A'

        # Processar rental_date
        rental_date_str = dress.get('rental_date')
        if rental_date_str:
            try:
                rental_date = datetime.strptime(rental_date_str, '%Y-%m-%d').date()
                dress['rental_date_formatted'] = rental_date.strftime('%d-%m-%Y')
                dress['rental_date_obj'] = rental_date  # Para ordenação
            except ValueError:
                dress['rental_date_formatted'] = 'Data Inválida'
                dress['rental_date_obj'] = today
        else:
            dress['rental_date_formatted'] = 'N/A'
            dress['rental_date_obj'] = today

        # Prioridade para ordenação
        dress['priority'] = 0 if not dress.get('retirado', False) else 1

    # Ordenar: primeiro não retirado, depois por data de retirada mais próxima
    sorted_items = sorted(items, key=lambda x: (x.get('retirado', False), x['rental_date_obj']))

    # (Opcional) Debugging: Imprimir os dados para verificar
    # for dress in sorted_items:
    #     print(f"ID: {dress['dress_id']}, Retirado: {dress['retirado']}, Rental Date: {dress.get('rental_date_formatted')}, Return Date: {dress.get('return_date_formatted')}")

    return render_template('index.html', dresses=sorted_items)




@app.route('/returned')
def returned():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Obter todos os registros "returned"
    response = table.scan(
        FilterExpression="#status = :status_returned",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status_returned": "returned"}
    )
    items = response.get('Items', [])

    # Data atual sem hora, para facilitar comparação
    today = datetime.now().date()

    for dress in items:
        return_date_str = dress.get('return_date')
        rental_date_str = dress.get('rental_date')
        if return_date_str:
            try:
                return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
                dress['overdue'] = return_date < today
                dress['return_date_formatted'] = return_date.strftime('%d-%m-%Y')
            except ValueError:
                dress['overdue'] = False
                dress['return_date_formatted'] = 'Data Inválida'
        else:
            dress['overdue'] = False
            dress['return_date_formatted'] = 'N/A'

        # Processar rental_date
        if rental_date_str:
            try:
                rental_date = datetime.strptime(rental_date_str, '%Y-%m-%d').date()
                dress['rental_date_formatted'] = rental_date.strftime('%d-%m-%Y')
            except ValueError:
                dress['rental_date_formatted'] = 'Data Inválida'
        else:
            dress['rental_date_formatted'] = 'N/A'

        # Prioridade para ordenação
        dress['priority'] = 0 if not dress.get('retirado', False) else 1

    # Ordenar conforme a necessidade (opcional)
    sorted_items = sorted(items, key=lambda x: (x.get('retirado', False), x.get('rental_date_obj', today)))

    return render_template('returned.html', dresses=sorted_items)


@app.route('/add', methods=['GET', 'POST'])
def add():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        client_name = request.form.get('client_name')
        client_tel = request.form.get('client_tel')
        rental_date_str = request.form.get('rental_date')
        return_date_str = request.form.get('return_date')
        retirado = 'retirado' in request.form  # Verifica se o checkbox está marcado
        comments = request.form.get('comments')
        image_file = request.files.get('image_file')

        # Validar e converter as datas
        try:
            rental_date = datetime.strptime(rental_date_str, '%Y-%m-%d').date()
            return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Formato de data inválido. Use AAAA-MM-DD.', 'error')
            return render_template('add.html')

        # Fazer upload da imagem, se houver
        image_url = ""
        if image_file and image_file.filename != "":
            image_url = upload_image_to_s3(image_file)  # Implemente esta função conforme necessário

        # Gerar um ID único para o vestido (pode usar UUID)
        dress_id = str(uuid.uuid4())

        # Adicionar o novo vestido ao DynamoDB
        table.put_item(
            Item={
                'dress_id': dress_id,
                'client_name': client_name,
                'client_tel': client_tel,
                'rental_date': rental_date.strftime('%Y-%m-%d'),
                'return_date': return_date.strftime('%Y-%m-%d'),
                'retirado': retirado,
                'comments': comments,
                'image_url': image_url,
                'status': 'rented'  # ou outro status conforme necessário
            }
        )

        flash('Vestido adicionado com sucesso.', 'success')
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
        flash('Vestido não encontrado.', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        rental_date_str = request.form.get('rental_date')
        return_date_str = request.form.get('return_date')
        client_name = request.form.get('client_name')
        client_tel = request.form.get('client_tel')
        retirado = 'retirado' in request.form  # Verifica presença do checkbox
        comments = request.form.get('comments')
        image_file = request.files.get('image_file')

        # Validar e converter as datas
        try:
            rental_date = datetime.strptime(rental_date_str, '%Y-%m-%d').date()
            return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Formato de data inválido. Use AAAA-MM-DD.', 'error')
            return render_template('edit.html', dress=dress)

        # Fazer upload da imagem, se houver
        new_image_url = item.get('image_url', "")
        if image_file and image_file.filename != "":
            new_image_url = upload_image_to_s3(image_file)  # Implemente esta função conforme necessário

        # Atualizar item no DynamoDB
        table.update_item(
            Key={'dress_id': dress_id},
            UpdateExpression="""
                set rental_date = :r,
                    return_date = :rt,
                    comments = :c,
                    image_url = :i,
                    client_name = :cn,
                    client_tel = :ct,
                    retirado = :ret
            """,
            ExpressionAttributeValues={
                ':r': rental_date.strftime('%Y-%m-%d'),
                ':rt': return_date.strftime('%Y-%m-%d'),
                ':c': comments,
                ':i': new_image_url,
                ':cn': client_name,
                ':ct': client_tel,
                ':ret': retirado
            }
        )

        flash('Vestido atualizado com sucesso.', 'success')
        # Redirecionar de acordo com o status atual
        if item.get('status') == 'returned':
            return redirect(url_for('returned'))
        else:
            return redirect(url_for('index'))

    # Preparar dados para o template
    dress = {
        'dress_id': item.get('dress_id'),
        'client_name': item.get('client_name'),
        'client_tel': item.get('client_tel'),
        'rental_date': item.get('rental_date'),
        'return_date': item.get('return_date'),
        'comments': item.get('comments'),
        'image_url': item.get('image_url'),
        'retirado': item.get('retirado', False)
    }

    return render_template('edit.html', dress=dress)




@app.route('/delete/<dress_id>', methods=['POST'])
def delete(dress_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    try:
        # Obter o item antes de deletar
        response = table.get_item(Key={'dress_id': dress_id})
        item = response.get('Item')
        if item:
            image_url = item.get('image_url')
            
            # Se existe image_url, tentar deletar o objeto no S3
            if image_url and image_url.strip():
                parsed_url = urlparse(image_url)
                object_key = parsed_url.path.lstrip('/')  # Extrair a chave do objeto no S3
                # Deletar o objeto do S3
                s3.delete_object(Bucket=s3_bucket_name, Key=object_key)
            
            # Apagar registro no DynamoDB
            table.delete_item(Key={'dress_id': dress_id})
            flash('Vestido deletado com sucesso!', 'success')  # Mensagem de sucesso
        else:
            flash('Vestido não encontrado.', 'error')  # Mensagem de erro para vestido inexistente

    except Exception as e:
        # Registrar ou tratar o erro aqui, se necessário
        flash(f'Ocorreu um erro ao tentar deletar o vestido: {str(e)}', 'error')  # Mensagem de erro
    
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
    flash('Vestido movido com sucesso.', 'success')
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

    # Mensagem de sucesso
    flash('Vestido movido com sucesso.', 'success')

    return redirect(url_for('returned'))

# Rota de Logout
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
