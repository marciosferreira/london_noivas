from flask import (
    render_template,
    request,
    session,
    redirect,
    url_for,
    flash,
    send_from_directory,
)

import os
import random
import pickle
import re
import unicodedata

import uuid
from decimal import Decimal
import datetime
from boto3.dynamodb.conditions import Key, Attr
from flask import render_template_string
from utils import get_user_timezone, get_account_plan
from flask import jsonify
import qrcode
import io
import base64
from flask import request
import os
import schemas


# Account ID principal da London Noivas
LONDON_NOIVAS_ACCOUNT_ID = "37d5b37f-c920-4090-a682-7e1ed2e31a0f"

def init_static_routes(
    app,
    ses_client,
    clients_table,
    transactions_table,
    itens_table,
    text_models_table,
    users_table,
    payment_transactions_table,
):
    env_ids = os.getenv("PUBLIC_CATALOG_ACCOUNT_IDS", "").strip()
    public_account_ids = [
        v.strip()
        for v in ([env_ids] if env_ids and "," not in env_ids else env_ids.split(","))
        if v.strip()
    ]
    if not public_account_ids:
        public_account_ids = [LONDON_NOIVAS_ACCOUNT_ID, "london_noivas"]

    ai_meta_cache = {"mtime": None, "by_id": None}

    def _normalize_text(text):
        text = str(text or "").lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join([c for c in text if not unicodedata.combining(c)])
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _slugify(text):
        text = _normalize_text(text)
        text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
        return text

    def _get_item_occasions(item):
        if not isinstance(item, dict):
            return []
            
        # Check DynamoDB flags first
        slugs_map = {
            "madrinha": "Madrinha",
            "formatura": "Formatura",
            "gala": "Gala",
            "debutante": "Debutante",
            "convidada": "Convidada",
            "mae_dos_noivos": "Mãe dos Noivos",
            "noiva": "Noiva",
            "civil": "Civil"
        }
        
        occasions = []
        for slug, label in slugs_map.items():
            if item.get(f"occasion_{slug}") == "1":
                occasions.append(label)
                
        if occasions:
            return occasions

        # Fallback to AI metadata if no flags are set (backward compatibility)
        meta_by_id = _load_ai_meta_by_id()
        item_id = item.get("item_id")
        if item_id and str(item_id) in meta_by_id:
            meta = meta_by_id.get(str(item_id)) or {}
            mf = meta.get("metadata_filters")
            if isinstance(mf, dict):
                occ = mf.get("occasions")
                if isinstance(occ, list):
                    return [str(o) for o in occ if str(o).strip()]
        return []

    def _load_ai_meta_by_id():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        pkl_path = os.path.join(base_dir, "vector_store_metadata.pkl")
        if not os.path.exists(pkl_path):
            ai_meta_cache["mtime"] = None
            ai_meta_cache["by_id"] = {}
            return ai_meta_cache["by_id"]

        try:
            mtime = os.path.getmtime(pkl_path)
            if ai_meta_cache["by_id"] is not None and ai_meta_cache["mtime"] == mtime:
                return ai_meta_cache["by_id"]

            with open(pkl_path, "rb") as f:
                raw = pickle.load(f)

            by_id = {}
            if isinstance(raw, list):
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    cid = entry.get("custom_id") or entry.get("item_id")
                    if cid:
                        by_id[str(cid)] = entry

            ai_meta_cache["mtime"] = mtime
            ai_meta_cache["by_id"] = by_id
            return by_id
        except Exception:
            ai_meta_cache["mtime"] = None
            ai_meta_cache["by_id"] = {}
            return ai_meta_cache["by_id"]

    def _category_slug(item):
        if isinstance(item, dict):
            meta_by_id = _load_ai_meta_by_id()
            item_id = item.get("item_id")
            if item_id and str(item_id) in meta_by_id:
                meta = meta_by_id.get(str(item_id)) or {}
                mf = meta.get("metadata_filters")
                if isinstance(mf, dict):
                    occ = mf.get("occasions")
                    if isinstance(occ, list):
                        for o in occ:
                            oo = _normalize_text(o)
                            if "noiv" in oo or "civil" in oo:
                                return "noiva"
                        return "festa"

            raw = item.get("item_category") or item.get("category") or item.get("categoria") or ""
            rr = _normalize_text(raw)
            if "noiv" in rr or "civil" in rr:
                return "noiva"

            blob = _normalize_text(f"{item.get('title') or item.get('item_title') or ''} {item.get('item_description') or item.get('description') or ''}")
            if "noiv" in blob or "civil" in blob:
                return "noiva"

        return "festa"

    # Static pages
    @app.route("/terms")
    def terms():
        return render_template("terms.html")

    @app.route("/contato", methods=["GET", "POST"])
    def contato():
        if request.method == "POST":
            nome = request.form.get("name")
            email = request.form.get("email")
            mensagem = request.form.get("message")

            if not nome or not email or not mensagem:
                flash("Todos os campos são obrigatórios.", "danger")
                return redirect(url_for("contato"))

            # Enviar e-mail via AWS SES
            destinatario = "contato@locashop.com.br"
            assunto = f"Novo contato de {nome}"
            corpo_email = f"Nome: {nome}\nE-mail: {email}\n\nMensagem:\n{mensagem}"

            try:
                response = ses_client.send_email(
                    Source=destinatario,
                    Destination={"ToAddresses": [destinatario]},
                    Message={
                        "Subject": {"Data": assunto, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": corpo_email, "Charset": "UTF-8"}},
                    },
                )
                flash("Mensagem enviada com sucesso!", "success")
            except Exception as e:
                print(f"Erro ao enviar e-mail: {e}")
                flash(
                    "Erro ao enviar a mensagem. Tente novamente mais tarde.", "danger"
                )

            return redirect(url_for("contato"))

        return render_template("contato.html")

    @app.route("/reportar-bug", methods=["GET", "POST"])
    def reportar_bug():
        url = request.args.get("url", "")

        if request.method == "POST":
            url = request.form.get("url")
            descricao = request.form.get("description")
            email = request.form.get("email", "Não informado")

            if not url or not descricao:
                flash("URL e descrição do bug são obrigatórios.", "danger")
                return redirect(url_for("reportar_bug"))

            # Enviar e-mail via AWS SES
            destinatario = "contato@locashop.com.br"
            assunto = f"Bug reportado: {url}"
            corpo_email = (
                f"URL: {url}\nE-mail: {email}\n\nDescrição do Bug:\n{descricao}"
            )

            try:
                response = ses_client.send_email(
                    Source=destinatario,
                    Destination={"ToAddresses": [destinatario]},
                    Message={
                        "Subject": {"Data": assunto, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": corpo_email, "Charset": "UTF-8"}},
                    },
                )
                flash(
                    "Relatório de bug enviado com sucesso! Obrigado pela contribuição.",
                    "success",
                )
            except Exception as e:
                print(f"Erro ao enviar e-mail: {e}")
                flash(
                    "Erro ao enviar o relatório. Tente novamente mais tarde.", "danger"
                )

            return redirect(url_for("index"))

        return render_template("reportar-bug.html", url=url)

    @app.route("/ads.txt")
    def ads_txt():
        return send_from_directory("static", "ads.txt")

    @app.route("/fees")
    def fees():

        return render_template("fees.html")

    @app.route("/how_to")
    def how_to():
        return render_template("how_to.html")

    @app.route("/test_modal")
    def test_modal():
        return render_template("test_modal.html")

    @app.route("/modelos")
    def listar_modelos():
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        EXEMPLO_ACCOUNT_ID = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
        account_id = session.get("account_id")
        # Modelos do usuário logado
        response_user = text_models_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(account_id),
        )
        modelos_usuario = response_user.get("Items", [])

        # Modelos de exemplo
        response_exemplos = text_models_table.query(
            IndexName="account_id-index",
            KeyConditionExpression=Key("account_id").eq(EXEMPLO_ACCOUNT_ID),
        )
        modelos_exemplo = response_exemplos.get("Items", [])

        # Junta os dois
        modelos = modelos_usuario + modelos_exemplo
        return render_template("listar_modelos.html", modelos=modelos)

    @app.route("/criar-modelo", methods=["GET", "POST"])
    def criar_modelo():
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        account_id = session.get("account_id")
        if request.method == "POST":
            text_id = str(uuid.uuid4().hex[:12])
            nome = request.form["nome"]
            conteudo = request.form["conteudo"]

            text_models_table.put_item(
                Item={
                    "text_id": text_id,
                    "account_id": account_id,
                    "nome": nome,
                    "conteudo": conteudo,
                }
            )
            return redirect(url_for("listar_modelos"))

        return render_template("criar_modelo.html")

    @app.route("/visualizar-modelo/<text_id>/<transacao_id>")
    def visualizar_modelo(text_id, transacao_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        # Recupera o modelo a partir do banco de dados
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")
        if not modelo:
            return "Modelo não encontrado.", 404

        # Recupera a transação do banco de dados
        response = transactions_table.get_item(Key={"transaction_id": transacao_id})
        transacao = response.get("Item")

        if not transacao:
            return "Transação não encontrada.", 404

        # Substitui as variáveis no conteúdo do modelo
        conteudo_renderizado = modelo["conteudo"]

        def formatar_data(valor):
            try:
                data = datetime.datetime.strptime(
                    valor, "%Y-%m-%d"
                )  # ou %Y/%m/%d dependendo da origem
                return data.strftime("%d/%m/%Y")
            except Exception:
                return valor

        if "rental_date" in transacao:
            transacao["rental_date_formatted"] = formatar_data(transacao["rental_date"])
        if "return_date" in transacao:
            transacao["return_date_formatted"] = formatar_data(transacao["return_date"])

        # send the current time to tempalte
        user_id = session.get("user_id")
        user_utc = get_user_timezone(users_table, user_id)

        # Gera a hora atual formatada (DD/MM/AAAA HH:MM)
        data_hora_atual = datetime.datetime.now(user_utc).strftime("%d/%m/%Y %H:%M")
        transacao["data_hora_atual"] = data_hora_atual

        # Substitui as variáveis do template com os dados da transação
        # Substitui as variáveis do template com os dados da transação
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_name }}",
            str(transacao.get("client_name", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_email }}",
            str(transacao.get("client_email", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_tel }}",
            str(transacao.get("client_tel", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_address }}",
            str(transacao.get("client_address", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_cpf }}", str(transacao.get("client_cpf", " "))
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ client_cnpj }}",
            str(transacao.get("client_cnpj", " ")),
        )

        # Dados do item
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ description }}",
            str(transacao.get("description", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ item_custom_id }}",
            str(transacao.get("item_custom_id", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ item_obs }}",
            str(transacao.get("item_obs", " ")),
        )

        # Dados da transação
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ valor }}", str(transacao.get("valor", " "))
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ pagamento }}",
            str(transacao.get("pagamento", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ transaction_obs }}",
            str(transacao.get("transaction_obs", " ")),
        )

        # Datas formatadas
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ rental_date_formatted }}",
            str(transacao.get("rental_date_formatted", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ return_date_formatted }}",
            str(transacao.get("return_date_formatted", " ")),
        )
        conteudo_renderizado = conteudo_renderizado.replace(
            "{{ data_hora_atual }}",
            str(transacao.get("data_hora_atual", " ")),
        )

        # Adicione mais substituições conforme necessário...

        # Passa o conteúdo renderizado para o template
        return render_template(
            "visualizar_modelo_conteudo.html", conteudo_renderizado=conteudo_renderizado
        )

    @app.route("/visualizar-modelo/<text_id>")
    def visualizar_modelo_simples(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")
        if not modelo:
            return "Modelo não encontrado."

        # Usa dados de exemplo para preenchimento
        dados_exemplo = {
            "client_name": "Joana Silva",
            "client_address": "Rua Exemplo, 123",
            "valor": 120.00,
            "pagamento": 60.00,
            "rental_date": "2025-04-18",
            "return_date": "2025-04-21",
            "data_hora_atual": "10-06-2025",
        }

        conteudo_renderizado = render_template_string(
            modelo["conteudo"],
            **dados_exemplo,
            modelo=modelo,
        )

        return render_template(
            "visualizar_modelo.html",
            modelo=modelo,
            conteudo_renderizado=conteudo_renderizado,
        )

    @app.route("/editar-modelo/<text_id>", methods=["GET", "POST"])
    def editar_modelo(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        EXEMPLO_ACCOUNT_ID = "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
        account_id = session.get("account_id")

        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")

        if not modelo:
            flash("Modelo não encontrado.", "danger")
            return redirect(url_for("listar_modelos"))

        # Impede edição de modelos de outros usuários (mas permite leitura dos de exemplo)
        if (
            modelo.get("account_id") != account_id
            and modelo.get("account_id") != EXEMPLO_ACCOUNT_ID
        ):
            flash("Você não tem permissão para editar este modelo.", "danger")
            return redirect(url_for("listar_modelos"))

        if request.method == "POST":
            # Se for modelo de exemplo, apenas bloqueia o salvamento
            if modelo.get("account_id") == EXEMPLO_ACCOUNT_ID:
                flash("Modelos de exemplo não podem ser alterados.", "warning")
                return redirect(url_for("editar_modelo", text_id=text_id))

            # Atualiza modelo do usuário
            novo_nome = request.form["nome"]
            novo_conteudo = request.form["conteudo"]

            text_models_table.update_item(
                Key={"text_id": text_id},
                UpdateExpression="SET nome = :n, conteudo = :c",
                ExpressionAttributeValues={
                    ":n": novo_nome,
                    ":c": novo_conteudo,
                },
            )
            flash("Modelo atualizado com sucesso.", "success")
            return redirect(url_for("listar_modelos"))

        return render_template("criar_modelo.html", modelo=modelo)

    @app.route("/excluir-modelo/<text_id>", methods=["POST"])
    def excluir_modelo(text_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        # Verifica se é do usuário atual (evita excluir exemplo)
        account_id = session.get("account_id")
        response = text_models_table.get_item(Key={"text_id": text_id})
        modelo = response.get("Item")

        if (
            modelo
            and modelo.get("account_id") != "3bcdb46a-a88f-4dfd-b97e-2fb07222e0f7"
            and modelo["account_id"] == account_id
        ):
            text_models_table.delete_item(Key={"text_id": text_id})
            flash("Modelo excluído com sucesso.", "success")
        else:
            flash("Modelo não encontrado ou não autorizado.", "danger")

        return redirect(url_for("listar_modelos"))

    @app.route("/imprimir-modelo/<item_id>/<modelo_id>")
    def imprimir_modelo(item_id, modelo_id):
        # Buscar o item pelo ID
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            return "Item não encontrado", 404

        # Buscar o modelo selecionado na tabela `text_models_table`
        response = text_models_table.get_item(Key={"text_id": modelo_id})
        modelo = response.get("Item")

        if not modelo:
            return "Modelo não encontrado", 404

        # Obter o conteúdo do modelo armazenado no banco
        conteudo = modelo.get("conteudo")

        # Verifica se o conteúdo do modelo está presente
        if not conteudo:
            return "Conteúdo do modelo não encontrado", 404

        # Renderizar o conteúdo com os dados do item
        # O conteúdo do modelo pode ter variáveis, como {{ item.item_custom_id }}, que serão substituídas com os dados do item
        conteudo_renderizado = render_template_string(conteudo, item=item)

        # Retornar o conteúdo gerado para exibição/impressão
        return conteudo_renderizado

    @app.route("/qr_data/<item_id>")
    def qr_data(item_id):
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")
        if not item:
            return jsonify({"error": "Item não encontrado"}), 404

        return jsonify(
            {
                "item_custom_id": item.get("item_custom_id", ""),
                "description": item.get("description", ""),
                "item_obs": item.get("item_obs", ""),
                "image_url": item.get("image_url", ""),
            }
        )

    @app.route("/imprimir_qrcode/<item_id>")
    def imprimir_qrcode(item_id):
        incluir = request.args.getlist(
            "incluir"
        )  # Obtém os parâmetros de inclusão da URL

        # Buscar o item pelo ID
        response = itens_table.get_item(Key={"item_id": item_id})
        item = response.get("Item")

        if not item:
            return "Item não encontrado", 404

        # Gerar o QR Code dinamicamente
        qr_data = (
            request.url_root.rstrip("/") + url_for("inventory") + f"?item_id={item_id}"
        )
        img = qrcode.make(qr_data)  # Gerando o QR Code
        buf = io.BytesIO()  # Usando buffer para armazenar a imagem
        img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        qr_data_url = f"data:image/png;base64,{qr_base64}"

        # Renderize o template do QR Code com os dados do item e as opções de impressão
        return render_template(
            "imprimir_qrcode.html", item=item, incluir=incluir, qr_data_url=qr_data_url
        )

    @app.route("/")
    def index():
        try:
            # Fetch ALL available items for vitrine (filtered by account)
            response = itens_table.scan(
                FilterExpression=Attr("status").eq("available") & Attr("account_id").is_in(public_account_ids)
            )
            all_items = response.get("Items", [])
            
            while "LastEvaluatedKey" in response:
                response = itens_table.scan(
                    FilterExpression=Attr("status").eq("available") & Attr("account_id").is_in(public_account_ids),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_items.extend(response.get("Items", []))

            occasion_tabs = [
                {"slug": "noiva", "label": "Noiva"},
                {"slug": "civil", "label": "Civil"},
                {"slug": "madrinha", "label": "Madrinha"},
                {"slug": "mae-dos-noivos", "label": "Mãe dos Noivos"},
                {"slug": "formatura", "label": "Formatura"},
                {"slug": "debutante", "label": "Debutante"},
                {"slug": "gala", "label": "Gala"},
                {"slug": "convidada", "label": "Convidada"},
            ]
            
            fields_config = schemas.get_schema_fields("item")
            
            return render_template("index.html", fields_config=fields_config, occasion_tabs=occasion_tabs)
            
        except Exception as e:
            print(f"Error loading vitrine: {e}")
            return render_template("index.html", itens=[], fields_config=[], occasion_tabs=[])

    @app.route("/catalogo")
    def catalogo():
        try:
            page = request.args.get('page', 1, type=int)
            active_occasion = request.args.get("occasion", "", type=str)
            per_page = 12
            
            # Fetch all available items (same as index) but filtered by account
            response = itens_table.scan(
                FilterExpression=Attr("status").eq("available") & Attr("account_id").is_in(public_account_ids)
            )
            all_items = response.get("Items", [])
            
            while "LastEvaluatedKey" in response:
                response = itens_table.scan(
                    FilterExpression=Attr("status").eq("available") & Attr("account_id").is_in(public_account_ids),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_items.extend(response.get("Items", []))
            
            occasion_tabs = [
                {"slug": "noiva", "label": "Noiva"},
                {"slug": "civil", "label": "Civil"},
                {"slug": "madrinha", "label": "Madrinha"},
                {"slug": "mae-dos-noivos", "label": "Mãe dos Noivos"},
                {"slug": "formatura", "label": "Formatura"},
                {"slug": "debutante", "label": "Debutante"},
                {"slug": "gala", "label": "Gala"},
                {"slug": "convidada", "label": "Convidada"},
            ]
            if not active_occasion:
                active_occasion = occasion_tabs[0]["slug"] if occasion_tabs else ""

            valid_slugs = {t["slug"] for t in occasion_tabs}
            if active_occasion not in valid_slugs:
                active_occasion = occasion_tabs[0]["slug"] if occasion_tabs else ""

            active_occasion_label = next((t["label"] for t in occasion_tabs if t["slug"] == active_occasion), "Catálogo")

            occasion_descriptions = {
                "noiva": "Este é o seu dia — e o seu vestido deve refletir isso. A noiva é o centro de todas as atenções, e não há motivo para ter medo de brilhar. Escolha um vestido que faça você se sentir a mulher mais bonita da sala, porque neste dia, você será. Aposte em detalhes que traduzam a sua personalidade: se você é clássica, renda e corte princesa; se é moderna, linhas limpas e tecidos fluidos. O segredo é simples — quando você se olhar no espelho e sentir um frio na barriga, é esse o vestido certo.",
                "civil": "O casamento civil pede elegância com leveza. Aqui, a ideia não é um vestido de baile, mas uma peça sofisticada que diga \"estou celebrando algo especial\". Midi, curto ou longo — todos funcionam. O importante é que o vestido transmita a alegria do momento sem exagero. Pense nele como aquele look que você usaria para a noite mais importante da sua vida, mas com a naturalidade de quem sabe exatamente o que está fazendo. Tecidos como crepe, cetim e musseline são escolhas certeiras.",
                "madrinha": "Ser madrinha é uma honra — e o seu vestido deve estar à altura desse papel. Você faz parte do cenário mais importante do dia, ao lado dos noivos, nas fotos, no altar. O ideal é um vestido elegante e harmonioso, que converse com a paleta da cerimônia sem competir com a noiva. Uma boa madrinha brilha no tom certo: presente, linda, mas sempre complementando a cena principal. Na dúvida, alinhe com a noiva a cor ou o estilo — ela vai agradecer, e você vai arrasar com confiança.",
                "mae-dos-noivos": "A mãe do noivo e a mãe da noiva têm um lugar de destaque absoluto. O vestido precisa transmitir sofisticação e emoção na medida certa — afinal, todos os olhos também estarão em vocês. Escolha peças estruturadas, com caimento impecável e tecidos nobres. Evite competir com a noiva, mas jamais se apague: você criou uma das pessoas mais importantes daquela celebração, e merece estar deslumbrante. Tons como azul-marinho, marsala, verde-esmeralda e nude são clássicos que nunca erram.",
                "formatura": "A formatura marca o encerramento de um ciclo e o começo de outro — e o vestido deve celebrar essa conquista com toda a grandiosidade que ela merece. Este é um dos poucos momentos da vida em que você pode (e deve!) ousar sem medo. Brilho, fendas, decotes, cores vibrantes — aqui vale tudo, porque a protagonista é você. Escolha algo que represente quem você é hoje e quem você está se tornando. Quando você subir aquele palco, o vestido deve fazer você se sentir tão poderosa quanto o diploma nas suas mãos.",
                "debutante": "Quinze anos se comemoram uma vez só — e esse vestido vai estar nas suas memórias (e nas fotos da família) para sempre. É o momento de realizar aquele sonho de princesa sem nenhuma culpa. Rodado, justo, com brilho, com volume — o estilo é seu, e não existe regra. O que importa é que, ao descer a escada ou entrar no salão, você sinta que o mundo parou para te ver. Escolha com o coração, porque esse vestido não é só tecido — é a marca de uma noite mágica.",
                "gala": "Evento de gala é sinônimo de sofisticação máxima. Aqui, o vestido precisa comunicar poder, elegância e presença. Pense em red carpet: tecidos que caem com perfeição, cortes que valorizam a silhueta e detalhes que revelam bom gosto sem esforço aparente. Menos é mais — mas o \"menos\" precisa ser impecável. Um vestido de gala bem escolhido fala por você antes mesmo de você abrir a boca. Aposte em cores profundas, modelagens clássicas e aquele acabamento que faz as pessoas virarem a cabeça quando você passa.",
                "convidada": "A regra de ouro da convidada: esteja linda, mas nunca mais que a noiva. Parece simples, mas é aqui que muita gente erra. O truque é encontrar o equilíbrio entre glamour e bom senso — um vestido que mostre que você se arrumou para a ocasião, sem roubar a cena de quem deve brilhar mais. Evite branco e tons muito claros (território da noiva), fuja do exagero nos brilhos e aposte em cores que valorizem você sem gritar. O vestido perfeito de convidada é aquele que rende elogios a noite toda — mas nunca ofusca a protagonista do dia."
            }

            active_occasion_description = occasion_descriptions.get(active_occasion, "")

            filtered_items = []
            for item in all_items:
                occ = item.get("_occasions") if isinstance(item, dict) else None
                if not isinstance(occ, list):
                    occ = _get_item_occasions(item)

                if any(_slugify(o) == active_occasion for o in occ):
                    filtered_items.append(item)

            itens = filtered_items
            for item in itens:
                if isinstance(item, dict):
                    occ = item.get("_occasions") or []
                    item["category"] = occ[0] if occ else "Outros"

            # Embaralhamento consistente baseado na sessão (Seed)
            # Garante que a ordem se mantém durante a navegação/paginação do usuário
            if "catalog_seed" not in session:
                session["catalog_seed"] = random.randint(1, 100000)
            
            # 1. Ordenar por ID para garantir estado inicial determinístico (scan do Dynamo pode variar)
            itens.sort(key=lambda x: x.get("item_id", ""))
            
            # 2. Embaralhar usando o seed da sessão
            random.Random(session["catalog_seed"]).shuffle(itens)
            
            fields_config = schemas.get_schema_fields("item")

            # Paginação em memória
            total_items = len(itens)
            total_pages = (total_items + per_page - 1) // per_page
            
            start = (page - 1) * per_page
            end = start + per_page
            current_itens = itens[start:end]
            
            return render_template(
                "catalogo.html", 
                itens=current_itens, 
                fields_config=fields_config,
                page=page,
                total_pages=total_pages,
                total_items=total_items,
                active_occasion=active_occasion,
                active_occasion_label=active_occasion_label,
                active_occasion_description=active_occasion_description,
                occasion_tabs=occasion_tabs,
            )
            
        except Exception as e:
            print(f"Error loading catalogo: {e}")
            return render_template("catalogo.html", itens=[], fields_config=[], page=1, total_pages=1, active_occasion="", occasion_tabs=[])

    @app.route("/home")
    def home():
        if not session.get("logged_in"):
            return redirect(url_for("index"))
        return redirect(url_for("agenda"))

    from flask import abort

    @app.route("/webhook/stripe", methods=["POST"])
    def stripe_webhook():
        return abort(404)

    @app.route("/create_checkout_session", methods=["POST"])
    def create_checkout_session():
        return jsonify({"error": "Pagamentos desativados."}), 410

    @app.route("/billing")
    def billing():
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        flash("Pagamentos desativados.", "warning")
        return redirect(url_for("adjustments"))

    @app.route("/autocomplete_items")
    def autocomplete_items():
        from boto3.dynamodb.conditions import Key, Attr

        account_id = session.get("account_id")
        term = request.args.get("term", "").strip().lower()

        if not term:
            return jsonify([])

        try:
            response = itens_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
                FilterExpression=Attr("status").is_in(["available", "archive"]),
                Limit=1000
            )
            all_items = response.get("Items", [])
        except Exception as e:
            print("Erro ao buscar itens:", e)
            return jsonify([])

        suggestions = []
        for item in all_items:
            custom_id = (item.get("item_custom_id") or "").lower()
            description = (item.get("item_description") or "").lower()
            item_id_value = (item.get("item_id") or "").lower()
            # aceita busca por código (custom_id), descrição ou pelo próprio item_id
            if term in custom_id or term in description or term in item_id_value:
                suggestions.append(item)

        return jsonify([
            {
                "item_id": item["item_id"],
                "item_custom_id": item.get("item_custom_id", ""),
                "item_description": item.get("item_description", ""),
                "item_value": item.get("item_value", ""),
                "item_obs": item.get("item_obs", ""),
                "item_image_url": item.get("item_image_url", ""),
            }
            for item in suggestions[:10]
        ])


    @app.route("/item_reserved_ranges/<item_id>")
    def item_reserved_ranges(item_id):
        try:
            response = transactions_table.query(
                IndexName="item_id-index",
                KeyConditionExpression=Key("item_id").eq(item_id),
            )
            items = response.get("Items", [])
            reserved_ranges = [
                [tx["rental_date"], tx["return_date"]]
                for tx in items
                if tx.get("transaction_status") in ["reserved", "rented"]
                and tx.get("rental_date")
                and tx.get("return_date")
            ]

            return jsonify(reserved_ranges)
        except Exception as e:
            print("Erro ao buscar reservas:", e)
            return jsonify([]), 500
