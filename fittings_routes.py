import datetime
import uuid
from boto3.dynamodb.conditions import Key, Attr
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)

from utils import get_user_timezone


def init_fittings_routes(
    app,
    fittings_table,
    transactions_table,
    itens_table,
    clients_table,
    users_table,
):
    """Rotas para Agenda e Provas (fittings)."""

    def _today_iso(user_tz):
        return datetime.datetime.now(user_tz).date().strftime("%Y-%m-%d")

    def _date_key(date_iso: str) -> str:
        # Chave composta para sort key local
        return date_iso

    def _date_time_key(date_iso: str, time_local: str, fitting_id: str = None) -> str:
        # Se time_local estiver vazio, usar apenas a data
        if not time_local or time_local.strip() == "":
            time_part = ""
        else:
            time_part = time_local
        
        # Incluir fitting_id para garantir unicidade
        if fitting_id:
            return f"{date_iso}#{time_part}#{fitting_id}"
        else:
            # Fallback para compatibilidade (gerar um ID único)
            unique_id = str(uuid.uuid4())[:8]
            return f"{date_iso}#{time_part}#{unique_id}"

    def _enrich_fittings_with_item_fields(fittings_items):
        cache = {}
        for f in fittings_items or []:
            item_id = f.get("item_id")
            if not item_id:
                continue

            if f.get("item_description") and f.get("item_custom_id") and f.get("item_image_url"):
                continue

            if item_id not in cache:
                try:
                    resp_item = itens_table.get_item(Key={"item_id": item_id})
                    cache[item_id] = resp_item.get("Item") or {}
                except Exception:
                    cache[item_id] = {}

            it = cache.get(item_id) or {}
            if not it:
                continue

            if not f.get("item_description"):
                item_desc = (
                    it.get("item_description")
                    or it.get("description")
                    or it.get("descricao")
                    or it.get("nome")
                    or ""
                )
                if item_desc:
                    f["item_description"] = item_desc

            if not f.get("item_custom_id") and it.get("item_custom_id"):
                f["item_custom_id"] = it.get("item_custom_id")

            if not f.get("item_image_url") and it.get("image_url"):
                f["item_image_url"] = it.get("image_url")

        return fittings_items

    def _list_fittings_for_date(account_id: str, date_iso: str):
        # Query por data usando begins_with em sort key
        try:
            resp = fittings_table.query(
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("date_time_local").begins_with(date_iso),
            )
            items = resp.get("Items", [])
            _enrich_fittings_with_item_fields(items)
            return items
        except Exception as e:
            print("Erro ao buscar provas do dia:", e)
            return []

    def _next_dates_with_fittings(account_id: str, start_date_iso: str, count: int = None):
        # Busca TODAS as próximas datas com provas diretamente na tabela DynamoDB
        results = []
        try:
            print(f"DEBUG: Searching for account_id: {account_id}, start_date: {start_date_iso}")
            
            # Usar scan sem filtro de data primeiro para ver todos os itens
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
            )
            
            items = resp.get("Items", [])
            print(f"DEBUG: Total items found: {len(items)}")
            
            # Debug: mostrar todos os itens encontrados
            for item in items:
                print(f"DEBUG: Found item - date_time_local: {item.get('date_time_local', 'N/A')}, client: {item.get('client_name', 'N/A')}")
            
            # Filtrar apenas datas futuras e agrupar por data
            dates_dict = {}
            for item in items:
                date_part = item["date_time_local"][:10]  # Extrai apenas a parte da data (YYYY-MM-DD)
                print(f"DEBUG: Processing item with date_part: {date_part}, full date_time_local: {item['date_time_local']}")
                
                # Filtrar apenas datas futuras
                if date_part > start_date_iso:
                    if date_part not in dates_dict:
                        dates_dict[date_part] = []
                    dates_dict[date_part].append(item)
                    print(f"DEBUG: Date {date_part} now has {len(dates_dict[date_part])} items")
                else:
                    print(f"DEBUG: Skipping date {date_part} (not greater than {start_date_iso})")
            
            print(f"DEBUG: Final dates_dict keys: {list(dates_dict.keys())}")
            for date_key, items_list in dates_dict.items():
                print(f"DEBUG: Date {date_key} has {len(items_list)} items")
            
            # Converte para o formato esperado e ordena por data
            for date_iso in sorted(dates_dict.keys()):
                _enrich_fittings_with_item_fields(dates_dict[date_iso])
                results.append({
                    "date_iso": date_iso,
                    "fitting_items": dates_dict[date_iso]  # Mudei de "items" para "fitting_items"
                })
                print(f"DEBUG: Added to results - Date: {date_iso}, Items count: {len(dates_dict[date_iso])}")
                    
        except Exception as e:
            print("Erro ao buscar próximos dias com provas:", e)
        
        print(f"DEBUG: Final results count: {len(results)}")
        return results

    def _next_dates_with_fittings_and_transactions(account_id: str, start_date_iso: str, count: int = None):
        # Busca TODAS as próximas datas com provas e transações
        results = []
        try:
            # Buscar provas futuras
            fittings_resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
            )
            fittings_items = fittings_resp.get("Items", [])
            
            # Buscar transações futuras (retiradas e devoluções)
            transactions_resp = transactions_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & (Attr("transaction_status").eq("reserved")
                   | Attr("transaction_status").eq("rented"))
            )
            transactions_items = transactions_resp.get("Items", [])
            
            # Agrupar por data
            dates_dict = {}
            
            # Processar provas
            for item in fittings_items:
                date_part = item["date_time_local"][:10]  # Extrai apenas a parte da data (YYYY-MM-DD)
                if date_part > start_date_iso:
                    if date_part not in dates_dict:
                        dates_dict[date_part] = {"fitting_items": [], "transaction_items": []}
                    dates_dict[date_part]["fitting_items"].append(item)
            
            # Processar transações
            for item in transactions_items:
                # Verificar retiradas
                rental_date = item.get("rental_date")
                if rental_date and rental_date > start_date_iso:
                    if rental_date not in dates_dict:
                        dates_dict[rental_date] = {"fitting_items": [], "transaction_items": []}
                    item_copy = item.copy()
                    item_copy['transaction_type'] = 'pickup'
                    dates_dict[rental_date]["transaction_items"].append(item_copy)
                
                # Verificar devoluções (apenas para status 'rented')
                return_date = item.get("return_date")
                if return_date and return_date > start_date_iso and item.get("transaction_status") == "rented":
                    if return_date not in dates_dict:
                        dates_dict[return_date] = {"fitting_items": [], "transaction_items": []}
                    item_copy = item.copy()
                    item_copy['transaction_type'] = 'return'
                    dates_dict[return_date]["transaction_items"].append(item_copy)
            
            # Converte para o formato esperado e ordena por data
            for date_iso in sorted(dates_dict.keys()):
                _enrich_fittings_with_item_fields(dates_dict[date_iso]["fitting_items"])
                results.append({
                    "date_iso": date_iso,
                    "fitting_items": dates_dict[date_iso]["fitting_items"],
                    "transaction_items": dates_dict[date_iso]["transaction_items"]
                })
                    
        except Exception as e:
            print("Erro ao buscar próximos dias com provas e transações:", e)
        
        return results

    def _past_dates_with_fittings(account_id: str, end_date_iso: str, count: int = 30):
        # Busca agendamentos passados (anteriores à data especificada)
        results = []
        try:
            # Usar scan para encontrar todos os itens do account_id
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
            )
            
            items = resp.get("Items", [])
            
            # Filtrar apenas datas passadas e agrupar por data
            dates_dict = {}
            for item in items:
                date_part = item["date_time_local"][:10]  # Extrai apenas a parte da data (YYYY-MM-DD)
                
                # Filtrar apenas datas passadas
                if date_part < end_date_iso:
                    if date_part not in dates_dict:
                        dates_dict[date_part] = []
                    dates_dict[date_part].append(item)
            
            # Converte para o formato esperado e ordena por data (mais recente primeiro)
            for date_iso in sorted(dates_dict.keys(), reverse=True):
                results.append({
                    "date_iso": date_iso,
                    "fitting_items": dates_dict[date_iso]
                })
                
                # Limitar o número de datas retornadas
                if len(results) >= count:
                    break
                    
        except Exception as e:
            print("Erro ao buscar agendamentos passados:", e)
        
        return results

    def _rentals_pickup_today(account_id: str, today_iso: str):
        # Lista retiradas agendadas para hoje (reservadas/rented)
        try:
            resp = transactions_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("rental_date").eq(today_iso)
                & (Attr("transaction_status").eq("reserved")
                   | Attr("transaction_status").eq("rented"))
            )
            return resp.get("Items", [])
        except Exception as e:
            print("Erro ao buscar retiradas de hoje:", e)
            return []

    def _rentals_return_today(account_id: str, today_iso: str):
        # Lista devoluções agendadas para hoje (rented)
        try:
            resp = transactions_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("return_date").eq(today_iso)
                & Attr("transaction_status").eq("rented")
            )
            return resp.get("Items", [])
        except Exception as e:
            print("Erro ao buscar devoluções de hoje:", e)
            return []

    def _rentals_for_date(account_id: str, date_iso: str):
        # Lista transações (retiradas e devoluções) para uma data específica
        try:
            # Buscar retiradas para esta data
            pickup_resp = transactions_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("rental_date").eq(date_iso)
                & (Attr("transaction_status").eq("reserved")
                   | Attr("transaction_status").eq("rented"))
            )
            pickups = pickup_resp.get("Items", [])
            
            # Buscar devoluções para esta data
            return_resp = transactions_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("return_date").eq(date_iso)
                & Attr("transaction_status").eq("rented")
            )
            returns = return_resp.get("Items", [])
            
            # Marcar o tipo de transação
            for item in pickups:
                item['transaction_type'] = 'pickup'
            for item in returns:
                item['transaction_type'] = 'return'
            
            return pickups + returns
        except Exception as e:
            print(f"Erro ao buscar transações para {date_iso}:", e)
            return []

    def _validate_conflicts(client_id, item_id, date_iso, time_local):
        # Usa os GSIs fornecidos para detectar conflitos por cliente/item no mesmo horário
        conflicts = {"client": [], "item": []}
        # Para validação de conflitos, usar apenas data e hora (sem fitting_id)
        dt_key_base = f"{date_iso}#{time_local}" if time_local and time_local.strip() else f"{date_iso}#"
        try:
            if client_id:
                resp = fittings_table.query(
                    IndexName="client_id-date_time_local-index",
                    KeyConditionExpression=Key("client_id").eq(client_id)
                    & Key("date_time_local").begins_with(dt_key_base),
                )
                conflicts["client"] = resp.get("Items", [])
        except Exception as e:
            print("Erro ao checar conflitos por cliente:", e)
        try:
            if item_id:
                resp = fittings_table.query(
                    IndexName="item_id-date_time_local-index",
                    KeyConditionExpression=Key("item_id").eq(item_id)
                    & Key("date_time_local").begins_with(dt_key_base),
                )
                conflicts["item"] = resp.get("Items", [])
        except Exception as e:
            print("Erro ao checar conflitos por item:", e)
        return conflicts

    @app.route("/agenda")
    def agenda():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_tz = get_user_timezone(users_table, user_id)

        today_iso = _today_iso(user_tz)

        fittings_today = _list_fittings_for_date(account_id, today_iso)
        rentals_today = _rentals_pickup_today(account_id, today_iso)
        returns_today = _rentals_return_today(account_id, today_iso)

        # Mostra TODAS as datas futuras com provas e transações
        next_days = _next_dates_with_fittings_and_transactions(account_id, today_iso)

        return render_template(
            "agenda.html",
            today_iso=today_iso,
            fittings_today=fittings_today,
            rentals_today=rentals_today,
            returns_today=returns_today,
            next_days=next_days,
        )

    @app.route("/past_appointments")
    def past_appointments():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_tz = get_user_timezone(users_table, user_id)

        today_iso = _today_iso(user_tz)

        # Busca agendamentos passados (últimos 30 dias com agendamentos)
        past_days = _past_dates_with_fittings(account_id, today_iso)

        return render_template(
            "past_appointments.html",
            today_iso=today_iso,
            past_days=past_days,
        )

    @app.route("/add_fitting", methods=["GET", "POST"])
    def add_fitting():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_tz = get_user_timezone(users_table, user_id)

        if request.method == "GET":
            # Botão geral não define data; se houver ?date=... usar, senão deixar em branco
            pre_date = request.args.get("date") or ""
            return render_template("fitting_form.html", pre_date=pre_date)

        # POST
        date_iso = request.form.get("date_iso")
        time_local = request.form.get("time_local")
        status = (request.form.get("status") or "Pendente").strip()
        notes = (request.form.get("notes") or "").strip()
        client_id = (request.form.get("client_id") or "").strip()
        item_id = (request.form.get("item_id") or "").strip()
        client_name_form = (request.form.get("client_name") or "").strip()
        item_description_form = (request.form.get("item_description") or "").strip()
        item_custom_id_value = None
        item_image_url_value = None

        # Preparar valores: permitir limpeza explícita
        client_name_form = request.form.get("client_name", "").strip()
        item_description_form = request.form.get("item_description", "").strip()
        
        # Se o campo foi enviado (mesmo que vazio), usar o valor enviado
        # Se não foi enviado, tentar copiar dos originais
        client_name_value = client_name_form if "client_name" in request.form else None
        item_description_value = item_description_form if "item_description" in request.form else None

        if not date_iso:
            flash("Data é obrigatória para a prova.", "danger")
            return redirect(url_for("add_fitting"))
        if not time_local or not time_local.strip():
            flash("Horário é obrigatório para a prova.", "danger")
            return redirect(url_for("add_fitting", date=date_iso))

        # Checa conflitos usando GSIs nomeados pelo usuário
        conflicts = _validate_conflicts(client_id, item_id, date_iso, time_local)
        if conflicts.get("client"):
            flash("Já existe uma prova para este cliente neste horário.", "warning")
        if conflicts.get("item"):
            flash("Este vestido já possui prova neste horário.", "warning")

        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        fitting_id = str(uuid.uuid4())
        dt_key = _date_time_key(date_iso, time_local, fitting_id)

        try:
            item_data = {
                "account_id": account_id,
                "date_time_local": dt_key,
                "fitting_id": fitting_id,
                "date_local": date_iso,
                "time_local": time_local,
                "status": status,
                "notes": notes,
                "created_at": now_utc,
                "updated_at": now_utc,
                "created_by": user_id,
            }
            # Campos opcionais: só adiciona se não vazio para evitar NULL em índices
            if client_id:
                item_data["client_id"] = client_id
            if item_id:
                item_data["item_id"] = item_id

            # Resolver e incluir campos
            # Se deixados em branco intencionalmente, manter vazio
            # Se não foram preenchidos, tentar copiar de client/item
            try:
                if client_name_value is None and client_id:
                    resp_client = clients_table.get_item(Key={"client_id": client_id})
                    c = resp_client.get("Item")
                    if c:
                        client_name_value = c.get("client_name")
                if item_description_value is None and item_id:
                    resp_item = itens_table.get_item(Key={"item_id": item_id})
                    it = resp_item.get("Item")
                    if it:
                        item_description_value = it.get("item_description") or it.get("descricao") or it.get("nome")
                        item_custom_id_value = it.get("item_custom_id") or item_custom_id_value
                        item_image_url_value = it.get("image_url") or item_image_url_value
            except Exception:
                pass

            # Incluir campos independentes apenas se tiverem valor
            if client_name_value:
                item_data["client_name"] = client_name_value
            if item_description_value:
                item_data["item_description"] = item_description_value
            if item_custom_id_value:
                item_data["item_custom_id"] = item_custom_id_value
            if item_image_url_value:
                item_data["item_image_url"] = item_image_url_value

            fittings_table.put_item(Item=item_data)
            flash("Prova agendada com sucesso.", "success")
            return redirect(url_for("agenda"))
        except Exception as e:
            print("Erro ao salvar prova:", e)
            flash("Erro ao salvar a prova. Tente novamente.", "danger")
            return redirect(url_for("add_fitting"))

    @app.route("/create_test_fittings")
    def create_test_fittings():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        print(f"DEBUG: Using account_id from session: {account_id}")
        
        # Usar uma data futura (2025-11-05) para garantir que apareça na lista
        date_iso = "2025-11-05"
        
        test_fittings = [
            {
                "account_id": account_id,
                "date_time_local": f"{date_iso}#09:00#{datetime.datetime.now().timestamp()}_1",
                "fitting_id": f"test_fitting_1_{datetime.datetime.now().timestamp()}",
                "client_name": "Cliente Teste 1",
                "item_description": "Vestido de Noiva A",
                "time_local": "09:00",
                "status": "scheduled",
                "notes": "Primeira prova do dia"
            },
            {
                "account_id": account_id,
                "date_time_local": f"{date_iso}#14:00#{datetime.datetime.now().timestamp()}_2",
                "fitting_id": f"test_fitting_2_{datetime.datetime.now().timestamp()}",
                "client_name": "Cliente Teste 2", 
                "item_description": "Vestido de Festa B",
                "time_local": "14:00",
                "status": "scheduled",
                "notes": "Segunda prova do dia"
            },
            {
                "account_id": account_id,
                "date_time_local": f"{date_iso}#16:30#{datetime.datetime.now().timestamp()}_3",
                "fitting_id": f"test_fitting_3_{datetime.datetime.now().timestamp()}",
                "client_name": "Cliente Teste 3",
                "item_description": "Terno C",
                "time_local": "16:30", 
                "status": "scheduled",
                "notes": "Terceira prova do dia"
            }
        ]
        
        try:
            for fitting in test_fittings:
                print(f"DEBUG: Creating fitting: {fitting}")
                fittings_table.put_item(Item=fitting)
                print(f"DEBUG: Successfully created fitting for {fitting['client_name']} at {fitting['time_local']}")
            
            flash(f"✅ Criados 3 fittings de teste para {date_iso}", "success")
            print(f"DEBUG: All test fittings created for {date_iso}")
            
        except Exception as e:
            print(f"DEBUG: Error creating test fittings: {e}")
            flash(f"Erro ao criar dados de teste: {e}", "error")
        
        return redirect(url_for("debug_agenda"))

    @app.route("/debug_agenda")
    def debug_agenda():
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_tz = get_user_timezone(users_table, user_id)

        today_iso = _today_iso(user_tz)

        # Mostra TODAS as datas futuras com provas
        next_days = _next_dates_with_fittings(account_id, today_iso)

        return render_template("debug_agenda.html", next_days=next_days, account_id=account_id, today_iso=today_iso)

    @app.route("/edit_fitting/<fitting_id>", methods=["GET", "POST"])
    def edit_fitting(fitting_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")
        user_id = session.get("user_id")
        user_tz = get_user_timezone(users_table, user_id)

        if request.method == "GET":
            # Buscar a prova pelo fitting_id
            try:
                resp = fittings_table.scan(
                    FilterExpression=Attr("account_id").eq(account_id) & Attr("fitting_id").eq(fitting_id)
                )
                items = resp.get("Items", [])
                if not items:
                    flash("Prova não encontrada.", "danger")
                    return redirect(url_for("agenda"))
                
                fitting = items[0]
                return render_template("edit_fitting.html", fitting=fitting)
            except Exception as e:
                print("Erro ao buscar prova:", e)
                flash("Erro ao carregar prova.", "danger")
                return redirect(url_for("agenda"))

        # POST - Atualizar prova
        date_iso = request.form.get("date_iso")
        time_local = request.form.get("time_local")
        status = (request.form.get("status") or "Pendente").strip()
        notes = (request.form.get("notes") or "").strip()
        client_id = (request.form.get("client_id") or "").strip()
        item_id = (request.form.get("item_id") or "").strip()

        if not date_iso:
            flash("Data é obrigatória para a prova.", "danger")
            return redirect(url_for("edit_fitting", fitting_id=fitting_id))
        if not time_local or not time_local.strip():
            flash("Horário é obrigatório para a prova.", "danger")
            return redirect(url_for("edit_fitting", fitting_id=fitting_id))

        try:
            # Buscar a prova atual para obter as chaves
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id) & Attr("fitting_id").eq(fitting_id)
            )
            items = resp.get("Items", [])
            if not items:
                flash("Prova não encontrada.", "danger")
                return redirect(url_for("agenda"))
            
            current_fitting = items[0]
            old_dt_key = current_fitting["date_time_local"]
            
            # Processar valores: permitir limpeza explícita
            client_name_form = request.form.get("client_name", "").strip()
            item_description_form = request.form.get("item_description", "").strip()
            
            # Se o campo foi enviado (mesmo que vazio), usar o valor enviado
            # Se não foi enviado, manter o valor atual
            if "client_name" in request.form:
                client_name_value = client_name_form if client_name_form else ""
            else:
                client_name_value = current_fitting.get("client_name")
                
            if "item_description" in request.form:
                item_description_value = item_description_form if item_description_form else ""
            else:
                item_description_value = current_fitting.get("item_description")
            
            # Nova chave de data/hora
            new_dt_key = _date_time_key(date_iso, time_local, fitting_id)
            now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # Se a data/hora mudou, precisamos deletar o item antigo e criar um novo
            if old_dt_key != new_dt_key:
                # Deletar item antigo
                fittings_table.delete_item(
                    Key={
                        "account_id": account_id,
                        "date_time_local": old_dt_key
                    }
                )
                
                # Criar novo item com nova chave
                item_data = {
                    "account_id": account_id,
                    "date_time_local": new_dt_key,
                    "fitting_id": fitting_id,
                    "date_local": date_iso,
                    "time_local": time_local,
                    "status": status,
                    "notes": notes,
                    "created_at": current_fitting.get("created_at", now_utc),
                    "updated_at": now_utc,
                    "created_by": current_fitting.get("created_by", user_id),
                }
                # Campos opcionais
                if client_id:
                    item_data["client_id"] = client_id
                if item_id:
                    item_data["item_id"] = item_id

                # Resolver valores, copiando dos originais apenas se não foram preenchidos
                try:
                    if not client_name_value and client_id:
                        resp_client = clients_table.get_item(Key={"client_id": client_id})
                        c = resp_client.get("Item")
                        if c:
                            client_name_value = c.get("client_name")
                    if not item_description_value and item_id:
                        resp_item = itens_table.get_item(Key={"item_id": item_id})
                        it = resp_item.get("Item")
                        if it:
                            item_description_value = it.get("item_description") or it.get("descricao") or it.get("nome")
                except Exception:
                    pass

                # Incluir campos apenas se tiverem valor
                if client_name_value:
                    item_data["client_name"] = client_name_value
                if item_description_value:
                    item_data["item_description"] = item_description_value

                fittings_table.put_item(Item=item_data)
            else:
                # Apenas atualizar os campos
                expr_attr_names = {"#status": "status"}
                expr_attr_values = {
                    ":status": status,
                    ":notes": notes,
                    ":updated_at": now_utc
                }

                set_parts = ["#status = :status", "notes = :notes", "updated_at = :updated_at"]
                remove_parts = []

                # Atualizar campos opcionais
                if client_id:
                    set_parts.append("#client_id = :client_id")
                    expr_attr_names["#client_id"] = "client_id"
                    expr_attr_values[":client_id"] = client_id
                else:
                    remove_parts.append("#client_id")
                    expr_attr_names["#client_id"] = "client_id"
                    
                if item_id:
                    set_parts.append("#item_id = :item_id")
                    expr_attr_names["#item_id"] = "item_id"
                    expr_attr_values[":item_id"] = item_id
                else:
                    remove_parts.append("#item_id")
                    expr_attr_names["#item_id"] = "item_id"

                # Resolver valores para atualização
                try:
                    if not client_name_value and client_id:
                        resp_client = clients_table.get_item(Key={"client_id": client_id})
                        c = resp_client.get("Item")
                        if c:
                            client_name_value = c.get("client_name")
                    if not item_description_value and item_id:
                        resp_item = itens_table.get_item(Key={"item_id": item_id})
                        it = resp_item.get("Item")
                        if it:
                            item_description_value = it.get("item_description") or it.get("descricao") or it.get("nome")
                except Exception:
                    pass

                # Atualizar campos (incluindo limpeza)
                if "client_name" in request.form:
                    if client_name_value:
                        set_parts.append("#client_name = :client_name")
                        expr_attr_names["#client_name"] = "client_name"
                        expr_attr_values[":client_name"] = client_name_value
                    else:
                        remove_parts.append("#client_name")
                        expr_attr_names["#client_name"] = "client_name"
                        
                if "item_description" in request.form:
                    if item_description_value:
                        set_parts.append("#item_description = :item_description")
                        expr_attr_names["#item_description"] = "item_description"
                        expr_attr_values[":item_description"] = item_description_value
                    else:
                        remove_parts.append("#item_description")
                        expr_attr_names["#item_description"] = "item_description"

                update_expr = "SET " + ", ".join(set_parts)
                if remove_parts:
                    update_expr += " REMOVE " + ", ".join(remove_parts)

                fittings_table.update_item(
                    Key={
                        "account_id": account_id,
                        "date_time_local": old_dt_key
                    },
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=expr_attr_names,
                    ExpressionAttributeValues=expr_attr_values
                )

            flash("Prova atualizada com sucesso.", "success")
            return redirect(url_for("agenda"))
        except Exception as e:
            print("Erro ao atualizar prova:", e)
            flash("Erro ao atualizar a prova. Tente novamente.", "danger")
            return redirect(url_for("edit_fitting", fitting_id=fitting_id))

    @app.route("/delete_fitting/<fitting_id>", methods=["POST"])
    def delete_fitting(fitting_id):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")

        try:
            # Buscar a prova pelo fitting_id para obter as chaves
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id) & Attr("fitting_id").eq(fitting_id)
            )
            items = resp.get("Items", [])
            if not items:
                flash("Prova não encontrada.", "danger")
                return redirect(url_for("agenda"))
            
            fitting = items[0]
            dt_key = fitting["date_time_local"]
            
            # Deletar a prova
            fittings_table.delete_item(
                Key={
                    "account_id": account_id,
                    "date_time_local": dt_key
                }
            )
            
            flash("Prova excluída com sucesso.", "success")
        except Exception as e:
            print("Erro ao excluir prova:", e)
            flash("Erro ao excluir a prova. Tente novamente.", "danger")
        
        return redirect(url_for("agenda"))
