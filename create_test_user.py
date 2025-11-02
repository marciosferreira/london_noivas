#!/usr/bin/env python3
"""
Script para criar um usuário de teste
"""

import boto3
import datetime
import uuid
import os
import hashlib
from dotenv import load_dotenv
from boto3.dynamodb.conditions import Key

# Carregar variáveis de ambiente
load_dotenv()

# Configurar DynamoDB com as mesmas credenciais do app
aws_region = "us-east-1"
aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)
users_table = dynamodb.Table("alugueqqc_users")

def create_test_user():
    """Cria um usuário de teste"""
    
    # Dados do usuário de teste
    email = "teste@exemplo.com"
    username = "teste"
    password = "teste123"  # Senha simples para teste
    account_id = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"  # Mesmo account_id dos itens de teste
    
    # Hash da senha (usando o mesmo método do sistema)
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    user_id = str(uuid.uuid4())
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    user_data = {
        "email": email,
        "user_id": user_id,
        "username": username,
        "password": password_hash,
        "account_id": account_id,
        "role": "user",
        "created_at": now_utc,
        "updated_at": now_utc,
        "timezone": "America/Manaus"  # Timezone padrão
    }
    
    try:
        # Verificar se o usuário já existe usando o GSI email-index
        existing_user = users_table.query(
            IndexName="email-index",
            KeyConditionExpression=Key("email").eq(email),
        )
        
        if existing_user.get("Items"):
            print(f"Usuário {email} já existe!")
            return
        
        # Inserir o usuário
        users_table.put_item(Item=user_data)
        print(f"✓ Usuário de teste criado com sucesso!")
        print(f"  Email: {email}")
        print(f"  Username: {username}")
        print(f"  Password: {password}")
        print(f"  Account ID: {account_id}")
        print(f"  User ID: {user_id}")
        
    except Exception as e:
        print(f"✗ Erro ao criar usuário: {e}")

if __name__ == "__main__":
    print("=== Criando Usuário de Teste ===")
    create_test_user()