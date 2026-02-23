import os

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def _get_dynamodb_client():
    load_dotenv()
    region = os.getenv("AWS_REGION", "us-east-1")
    profile = os.getenv("AWS_PROFILE")
    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEYID")
    aws_secret_access_key = (
        os.getenv("AWS_SECRET_ACCESS_KEY")
        or os.getenv("AWS_SECRET_ACCESS")
        or os.getenv("AWS_SECRET_KEY")
        or os.getenv("AWS_SECRET")
    )

    kwargs = {"region_name": region}
    if aws_access_key_id and aws_secret_access_key:
        kwargs.update(
            {
                "aws_access_key_id": aws_access_key_id,
                "aws_secret_access_key": aws_secret_access_key,
            }
        )

    if profile:
        session = boto3.Session(profile_name=profile, region_name=region)
        return session.client("dynamodb")

    return boto3.client("dynamodb", **kwargs)


def ensure_scheduling_config_table_exists():
    load_dotenv()
    client = _get_dynamodb_client()
    table_name = os.getenv("SCHEDULING_CONFIG_TABLE", "alugueqqc_scheduling_config_table")

    try:
        client.describe_table(TableName=table_name)
        print(f"[OK] Tabela já existe: {table_name}")
        return
    except client.exceptions.ResourceNotFoundException:
        pass

    print(f"[INFO] Criando tabela: {table_name}")
    client.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "account_id", "AttributeType": "S"},
            {"AttributeName": "config_key", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "account_id", "KeyType": "HASH"},
            {"AttributeName": "config_key", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table_name)
    print(f"[OK] Tabela criada e pronta: {table_name}")


if __name__ == "__main__":
    try:
        ensure_scheduling_config_table_exists()
    except ClientError as e:
        print("[ERRO] Falha ao criar/validar tabela:", e)
        raise
