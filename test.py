import os
from dotenv import load_dotenv

load_dotenv()


if __name__ == "__main__":
    # Obtém o valor da variável debug_env
    debug_env = os.getenv("debug_env", "não definida")

    # Exibe o valor da variável no terminal
    print(f"Variável de ambiente debug_env está ajustada como: {debug_env}")

    # Testa se a variável está ajustada para "True"
    debug_mode = debug_env.lower() == "true"
    print(f"Debug está {'ativado' if debug_mode else 'desativado'}.")
