# Usamos a imagem oficial Python slim para manter o container leve
FROM python:3.11-slim

# Instalamos dependências de sistema para bibliotecas de IA e conectores
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Pasta de trabalho no container
WORKDIR /app

# Copiamos o requirements.txt primeiro para otimizar o cache das camadas
COPY requirements.txt .

# Instalamos as bibliotecas (boto3, langchain, gunicorn, etc.)
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos todo o código do projeto
COPY . .

# Variável de ambiente para garantir que os logs do Python saiam em tempo real
ENV PYTHONUNBUFFERED=1

# Porta padrão que o Easypanel mapeia
EXPOSE 80

# Comando para rodar com Gunicorn (ajustado para performance)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:80", "--workers", "2", "--timeout", "120"]