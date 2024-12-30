import boto3


def scan_all_items(table_name, region="us-east-1"):
    """
    Retorna todos os itens de uma tabela DynamoDB,
    varrendo (scan) em lotes até não haver LastEvaluatedKey.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    response = table.scan()
    data = response["Items"]

    # Se houver mais páginas (LastEvaluatedKey), continua lendo até acabar
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        data.extend(response["Items"])

    return data


if __name__ == "__main__":
    # Nome da tabela que você quer ler
    table_name = "WeddingDresses"

    all_items = scan_all_items(table_name)

    # Exibe cada item (ou faça algo mais útil)
    for item in all_items:
        print(item)
