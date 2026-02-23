import datetime
import hashlib
import hmac
import json
import os
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
    scheduling_config_table,
    ses_client=None,
):
    """Rotas para Agenda e Provas (fittings)."""

    secret_key_for_tokens = (
        os.environ.get("BOOKING_TOKEN_SECRET")
        or os.environ.get("SECRET_KEY")
        or (app.secret_key if app and app.secret_key else "CHANGE_ME_TO_A_REAL_SECRET_KEY")
    )

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

    def _generate_confirmation_token(fitting_id: str) -> str:
        return hmac.new(
            str(secret_key_for_tokens).encode(),
            str(fitting_id).encode(),
            hashlib.sha256,
        ).hexdigest()[:32]

    def _pending_booking_key(fitting_id: str) -> str:
        return f"pending_booking#{fitting_id}"

    def _save_pending_booking(account_id: str, fitting_id: str, payload: dict) -> tuple[bool, str]:
        try:
            scheduling_config_table.put_item(
                Item={
                    "account_id": account_id,
                    "config_key": _pending_booking_key(fitting_id),
                    "payload": json.dumps(payload),
                    "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            return True, ""
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"Erro ao salvar pending_booking: {msg}")
            return False, msg

    def _get_public_account_info(account_id: str) -> dict:
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": "account_slug"}
            )
            item = resp.get("Item") or {}
            slug = str(item.get("slug") or "").strip()
            business_name = str(item.get("business_name") or "").strip()
            return {"slug": slug, "business_name": business_name}
        except Exception:
            return {"slug": "", "business_name": ""}

    def _send_confirmation_email(
        to_email: str,
        client_name: str,
        fitting_id: str,
        date_iso: str,
        time_local: str,
        account_slug: str,
        business_name: str = "",
    ) -> tuple[bool, str]:
        if not ses_client:
            return False, "Nenhum provedor de email configurado."
        sender = os.environ.get("SES_SENDER") or os.environ.get("EMAIL_SENDER")
        if not sender or not str(sender).strip():
            msg = "SES_SENDER/EMAIL_SENDER não configurado."
            print(f"Erro ao enviar email (SES): {msg}")
            return False, msg

        token = _generate_confirmation_token(fitting_id)
        confirm_url = url_for(
            "confirm_booking",
            account_slug=account_slug,
            fitting_id=fitting_id,
            token=token,
            _external=True,
        )

        subject_business = business_name or "Agendamento"
        subject = f"Confirme seu agendamento - {subject_business}"
        date_br = date_iso
        try:
            date_br = datetime.date.fromisoformat(date_iso).strftime("%d/%m/%Y")
        except Exception:
            pass
        name = (client_name or "").strip() or "Não informado"
        time_str = (time_local or "").strip() or "Não informado"
        body_text = (
            f"Olá {name}!\n\n"
            f"Recebemos seu pedido de agendamento.\n\n"
            f"Data: {date_br}\n"
            f"Horário: {time_str}\n\n"
            f"Para confirmar, acesse:\n{confirm_url}\n\n"
            f"Se você não fez esse agendamento, ignore este e-mail."
        )
        body_html = render_template(
            "email_confirmation.html",
            client_name=name,
            date_iso=date_br,
            time_local=time_str,
            confirm_url=confirm_url,
            business_name=subject_business,
        )
        try:
            ses_client.send_email(
                Source=sender,
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": body_text, "Charset": "UTF-8"},
                        "Html": {"Data": body_html, "Charset": "UTF-8"},
                    },
                },
            )
            return True, ""
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"Erro ao enviar email (SES): {msg}")
            return False, msg

    def _send_booking_confirmed_email(
        to_email: str,
        client_name: str,
        fitting_id: str,
        date_iso: str,
        time_local: str,
        account_slug: str,
        business_name: str = "",
        client_phone: str = "",
        item_description: str = "",
        notes: str = "",
    ) -> bool:
        if not ses_client:
            return False
        sender = (
            os.environ.get("SES_SENDER")
            or os.environ.get("EMAIL_SENDER")
            or "nao_responda@londonnoivas.com.br"
        )
        subject_business = business_name or "Agendamento"
        subject = f"Agendamento confirmado - {subject_business}"
        date_br = date_iso
        try:
            date_br = datetime.date.fromisoformat(date_iso).strftime("%d/%m/%Y")
        except Exception:
            pass

        name = (client_name or "").strip() or "Não informado"
        phone = (client_phone or "").strip() or "Não informado"
        time_str = (time_local or "").strip() or "Não informado"
        item_str = (item_description or "").strip() or "Não informado"
        notes_str = (notes or "").strip() or "-"

        token = _generate_confirmation_token(fitting_id)
        reschedule_url = ""
        cancel_url = ""
        if account_slug:
            reschedule_url = url_for(
                "reschedule_booking",
                account_slug=account_slug,
                fitting_id=fitting_id,
                token=token,
                _external=True,
            )
            cancel_url = url_for(
                "cancel_booking_public",
                account_slug=account_slug,
                fitting_id=fitting_id,
                token=token,
                _external=True,
            )

        body_text = (
            f"Olá {name}!\n\n"
            f"Seu agendamento foi confirmado.\n\n"
            f"Negócio: {subject_business}\n"
            f"Data: {date_br}\n"
            f"Horário: {time_str}\n"
            f"Telefone: {phone}\n"
            f"Item: {item_str}\n"
            f"Observações: {notes_str}\n"
        )
        if reschedule_url:
            body_text += f"\nEditar/Reagendar:\n{reschedule_url}\n"
        if cancel_url:
            body_text += f"\nCancelar:\n{cancel_url}\n"

        body_html = (
            "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;\">"
            f"<h2 style=\"margin:0 0 12px;\">Agendamento confirmado</h2>"
            f"<p style=\"margin:0 0 16px;color:#555;\">{subject_business}</p>"
            "<table cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;min-width:360px;\">"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Data</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{date_br}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Horário</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{time_str}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Nome</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{name}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Telefone</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{phone}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Item</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{item_str}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Observações</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{notes_str}</td></tr>"
            "</table>"
        )
        if reschedule_url or cancel_url:
            body_html += "<div style=\"margin:18px 0 0;display:flex;gap:10px;flex-wrap:wrap;\">"
            if reschedule_url:
                body_html += f"<a href=\"{reschedule_url}\" style=\"display:inline-block;background:#c8956c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;\">Editar/Reagendar</a>"
            if cancel_url:
                body_html += f"<a href=\"{cancel_url}\" style=\"display:inline-block;background:#c75c5c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;\">Cancelar</a>"
            body_html += "</div>"
        body_html += "</body></html>"

        try:
            ses_client.send_email(
                Source=sender,
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": body_text, "Charset": "UTF-8"},
                        "Html": {"Data": body_html, "Charset": "UTF-8"},
                    },
                },
            )
            return True
        except Exception as e:
            print(f"Erro ao enviar email (SES): {e}")
            return False

    def _send_booking_cancelled_email(
        to_email: str,
        client_name: str,
        date_iso: str,
        time_local: str,
        business_name: str = "",
    ) -> bool:
        if not ses_client:
            return False
        sender = (
            os.environ.get("SES_SENDER")
            or os.environ.get("EMAIL_SENDER")
            or "nao_responda@londonnoivas.com.br"
        )
        subject_business = business_name or "Agendamento"
        subject = f"Agendamento cancelado - {subject_business}"
        date_br = date_iso
        try:
            date_br = datetime.date.fromisoformat(date_iso).strftime("%d/%m/%Y")
        except Exception:
            pass
        name = (client_name or "").strip() or "Não informado"
        time_str = (time_local or "").strip() or "Não informado"
        body_text = (
            f"Olá {name}!\n\n"
            f"Seu agendamento foi cancelado.\n\n"
            f"Negócio: {subject_business}\n"
            f"Data: {date_br}\n"
            f"Horário: {time_str}\n"
        )
        body_html = (
            "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;\">"
            f"<h2 style=\"margin:0 0 12px;\">Agendamento cancelado</h2>"
            f"<p style=\"margin:0 0 16px;color:#555;\">{subject_business}</p>"
            "<table cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;min-width:360px;\">"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Data</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{date_br}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Horário</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{time_str}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Nome</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{name}</td></tr>"
            "</table>"
            "</body></html>"
        )
        try:
            ses_client.send_email(
                Source=sender,
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": body_text, "Charset": "UTF-8"},
                        "Html": {"Data": body_html, "Charset": "UTF-8"},
                    },
                },
            )
            return True
        except Exception as e:
            print(f"Erro ao enviar email (SES): {e}")
            return False

    def _get_default_duration_minutes(account_id: str) -> int:
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": "scheduling_settings"}
            )
            item = resp.get("Item") or {}
            raw = item.get("default_fitting_duration_minutes")
            val = int(raw)
            return val if val > 0 else 60
        except Exception:
            return 60

    def _time_to_minutes(time_local: str) -> int | None:
        if not time_local:
            return None
        t = str(time_local).strip()
        if not t:
            return None
        try:
            hh, mm = t.split(":")
            h = int(hh)
            m = int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h * 60 + m
        except Exception:
            return None

    def _intervals_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
        return start_a < end_b and start_b < end_a

    def _get_duration_minutes(item: dict, default_duration: int) -> int:
        raw = item.get("duration_minutes")
        try:
            val = int(raw)
        except Exception:
            val = default_duration
        return val if val > 0 else default_duration

    def _has_overlap(
        account_id: str,
        date_iso: str,
        start_time_local: str,
        duration_minutes: int,
        exclude_fitting_id: str | None = None,
    ) -> bool:
        start_min = _time_to_minutes(start_time_local)
        if start_min is None:
            return False
        end_min = start_min + max(1, int(duration_minutes or 60))
        default_duration = _get_default_duration_minutes(account_id)
        try:
            resp = fittings_table.query(
                KeyConditionExpression=Key("account_id").eq(account_id)
                & Key("date_time_local").begins_with(date_iso)
            )
            items = resp.get("Items", [])
        except Exception as e:
            print("Erro ao checar sobreposição:", e)
            return False

        for it in items:
            if exclude_fitting_id and str(it.get("fitting_id") or "") == exclude_fitting_id:
                continue
            status = str(it.get("status") or "").lower()
            if status in ("cancelado", "cancelled", "rejected"):
                continue
            other_start_str = it.get("time_local") or ""
            other_start = _time_to_minutes(other_start_str)
            if other_start is None:
                continue
            other_dur = _get_duration_minutes(it, default_duration)
            other_end = other_start + other_dur
            if _intervals_overlap(start_min, end_min, other_start, other_end):
                return True
        return False

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
            default_duration = _get_default_duration_minutes(account_id)
            return render_template(
                "fitting_form.html",
                pre_date=pre_date,
                default_duration_minutes=default_duration,
            )

        # POST
        date_iso = request.form.get("date_iso")
        time_local = (request.form.get("time_local") or "").strip()
        duration_raw = (request.form.get("duration_minutes") or "").strip()
        status = (request.form.get("status") or "Pendente").strip()
        notes = (request.form.get("notes") or "").strip()
        client_id = (request.form.get("client_id") or "").strip()
        item_id = (request.form.get("item_id") or "").strip()
        client_name_form = (request.form.get("client_name") or "").strip()
        item_description_form = (request.form.get("item_description") or "").strip()
        client_email_form = (request.form.get("client_email") or "").strip()
        client_phone_form = (request.form.get("client_phone") or "").strip()
        item_custom_id_value = None
        item_image_url_value = None

        # Preparar valores: permitir limpeza explícita
        client_name_form = request.form.get("client_name", "").strip()
        item_description_form = request.form.get("item_description", "").strip()
        client_email_form = request.form.get("client_email", "").strip()
        client_phone_form = request.form.get("client_phone", "").strip()
        
        # Se o campo foi enviado (mesmo que vazio), usar o valor enviado
        # Se não foi enviado, tentar copiar dos originais
        client_name_value = client_name_form if "client_name" in request.form else None
        item_description_value = item_description_form if "item_description" in request.form else None
        client_email_value = client_email_form if "client_email" in request.form else None
        client_phone_value = client_phone_form if "client_phone" in request.form else None

        if not date_iso:
            flash("Data é obrigatória para a prova.", "danger")
            return redirect(url_for("add_fitting"))

        default_duration = _get_default_duration_minutes(account_id)
        try:
            duration_minutes = int(duration_raw) if duration_raw else default_duration
        except Exception:
            duration_minutes = default_duration
        if duration_minutes <= 0:
            duration_minutes = default_duration

        status_norm = status.strip().lower()
        if time_local and status_norm not in ("cancelado", "cancelled", "rejected", "pendente", "pending", "pending_confirmation"):
            if _has_overlap(account_id, date_iso, time_local, duration_minutes):
                flash("Este horário se sobrepõe a outra prova. Escolha outro.", "danger")
                return redirect(url_for("add_fitting", date=date_iso))

        if time_local:
            conflicts = _validate_conflicts(client_id, item_id, date_iso, time_local)
            if conflicts.get("client"):
                flash("Já existe uma prova para este cliente neste horário.", "warning")
            if conflicts.get("item"):
                flash("Este vestido já possui prova neste horário.", "warning")

        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        fitting_id = str(uuid.uuid4())
        dt_key = _date_time_key(date_iso, time_local, fitting_id)

        try:
            public_info = _get_public_account_info(account_id)
            account_slug = public_info.get("slug") or ""
            business_name = public_info.get("business_name") or ""
            if status_norm in ("pendente", "pending") and client_email_form and time_local and account_slug:
                pending_payload = {
                    "account_id": account_id,
                    "fitting_id": fitting_id,
                    "date_local": date_iso,
                    "time_local": time_local,
                    "status": "pending_confirmation",
                    "duration_minutes": duration_minutes,
                    "notes": notes,
                    "client_name": (client_name_value or "").strip(),
                    "client_phone": (client_phone_value or "").strip(),
                    "client_email": client_email_form,
                    "item_id": item_id,
                    "item_custom_id": item_custom_id_value or "",
                    "item_description": (item_description_value or "").strip(),
                    "item_image_url": item_image_url_value or "",
                    "source": "admin_manual",
                    "created_at": now_utc,
                }
                ok, err = _save_pending_booking(account_id, fitting_id, pending_payload)
                if not ok:
                    flash(f"Não foi possível iniciar a confirmação por e-mail: {err}", "danger")
                    return redirect(url_for("add_fitting", date=date_iso))
                sent, send_err = _send_confirmation_email(
                    to_email=client_email_form,
                    client_name=(client_name_value or "").strip(),
                    fitting_id=fitting_id,
                    date_iso=date_iso,
                    time_local=time_local,
                    account_slug=account_slug,
                    business_name=business_name,
                )
                if sent:
                    flash("Prova pendente criada e e-mail de confirmação enviado.", "success")
                else:
                    flash(f"Prova pendente criada, mas não foi possível enviar o e-mail: {send_err}", "warning")
                return redirect(url_for("agenda"))

            if status_norm in ("pendente", "pending") and client_email_form and time_local and not account_slug:
                flash("Slug público não configurado no dashboard; confirmação por e-mail não enviada.", "warning")
            if status_norm in ("pendente", "pending") and client_email_form and not time_local:
                flash("Sem horário: confirmação por e-mail não enviada (o link exige horário).", "warning")

            item_data = {
                "account_id": account_id,
                "date_time_local": dt_key,
                "fitting_id": fitting_id,
                "date_local": date_iso,
                "time_local": time_local,
                "status": status,
                "duration_minutes": duration_minutes,
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
                        if client_email_value is None:
                            client_email_value = c.get("client_email")
                        if client_phone_value is None:
                            client_phone_value = c.get("client_phone")
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
            if client_email_value:
                item_data["client_email"] = client_email_value
            if client_phone_value:
                item_data["client_phone"] = client_phone_value
            if item_description_value:
                item_data["item_description"] = item_description_value
            if item_custom_id_value:
                item_data["item_custom_id"] = item_custom_id_value
            if item_image_url_value:
                item_data["item_image_url"] = item_image_url_value

            fittings_table.put_item(Item=item_data)

            if client_email_form:
                if status_norm in ("confirmado", "confirmed"):
                    _send_booking_confirmed_email(
                        to_email=client_email_form,
                        client_name=item_data.get("client_name", ""),
                        fitting_id=fitting_id,
                        date_iso=date_iso,
                        time_local=time_local,
                        account_slug=account_slug,
                        business_name=business_name,
                        client_phone=item_data.get("client_phone", ""),
                        item_description=item_data.get("item_description", ""),
                        notes=notes,
                    )
                elif status_norm in ("cancelado", "cancelled"):
                    _send_booking_cancelled_email(
                        to_email=client_email_form,
                        client_name=item_data.get("client_name", ""),
                        date_iso=date_iso,
                        time_local=time_local,
                        business_name=business_name,
                    )
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
                default_duration = _get_default_duration_minutes(account_id)
                return render_template(
                    "edit_fitting.html",
                    fitting=fitting,
                    default_duration_minutes=_get_duration_minutes(fitting, default_duration),
                )
            except Exception as e:
                print("Erro ao buscar prova:", e)
                flash("Erro ao carregar prova.", "danger")
                return redirect(url_for("agenda"))

        # POST - Atualizar prova
        date_iso = request.form.get("date_iso")
        time_local = (request.form.get("time_local") or "").strip()
        duration_raw = (request.form.get("duration_minutes") or "").strip()
        status = (request.form.get("status") or "Pendente").strip()
        notes = (request.form.get("notes") or "").strip()
        client_id = (request.form.get("client_id") or "").strip()
        item_id = (request.form.get("item_id") or "").strip()
        client_email_form = (request.form.get("client_email") or "").strip()
        client_phone_form = (request.form.get("client_phone") or "").strip()
        send_email_on_save = bool(request.form.get("send_email_on_save"))

        if not date_iso:
            flash("Data é obrigatória para a prova.", "danger")
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
            old_status_norm = str(current_fitting.get("status") or "").strip().lower()

            default_duration = _get_default_duration_minutes(account_id)
            try:
                duration_minutes = int(duration_raw) if duration_raw else _get_duration_minutes(current_fitting, default_duration)
            except Exception:
                duration_minutes = _get_duration_minutes(current_fitting, default_duration)
            if duration_minutes <= 0:
                duration_minutes = default_duration

            status_norm = status.strip().lower()
            if time_local and status_norm not in ("cancelado", "cancelled", "pendente", "pending", "pending_confirmation"):
                if _has_overlap(account_id, date_iso, time_local, duration_minutes, exclude_fitting_id=fitting_id):
                    flash("Este horário se sobrepõe a outra prova. Ajuste o horário/duração.", "danger")
                    return redirect(url_for("edit_fitting", fitting_id=fitting_id))
            
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

            if "client_email" in request.form:
                client_email_value = client_email_form if client_email_form else ""
            else:
                client_email_value = current_fitting.get("client_email")

            if "client_phone" in request.form:
                client_phone_value = client_phone_form if client_phone_form else ""
            else:
                client_phone_value = current_fitting.get("client_phone")
            
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
                    "duration_minutes": duration_minutes,
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
                            if not client_email_value:
                                client_email_value = c.get("client_email") or ""
                            if not client_phone_value:
                                client_phone_value = c.get("client_phone") or ""
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
                if client_email_value:
                    item_data["client_email"] = client_email_value
                if client_phone_value:
                    item_data["client_phone"] = client_phone_value
                if item_description_value:
                    item_data["item_description"] = item_description_value

                fittings_table.put_item(Item=item_data)
            else:
                # Apenas atualizar os campos
                expr_attr_names = {"#status": "status"}
                expr_attr_values = {
                    ":status": status,
                    ":notes": notes,
                    ":duration_minutes": duration_minutes,
                    ":updated_at": now_utc
                }

                set_parts = [
                    "#status = :status",
                    "notes = :notes",
                    "duration_minutes = :duration_minutes",
                    "updated_at = :updated_at",
                ]
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
                            if not client_email_value:
                                client_email_value = c.get("client_email") or ""
                            if not client_phone_value:
                                client_phone_value = c.get("client_phone") or ""
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

                if "client_email" in request.form:
                    if client_email_value:
                        set_parts.append("#client_email = :client_email")
                        expr_attr_names["#client_email"] = "client_email"
                        expr_attr_values[":client_email"] = client_email_value
                    else:
                        remove_parts.append("#client_email")
                        expr_attr_names["#client_email"] = "client_email"

                if "client_phone" in request.form:
                    if client_phone_value:
                        set_parts.append("#client_phone = :client_phone")
                        expr_attr_names["#client_phone"] = "client_phone"
                        expr_attr_values[":client_phone"] = client_phone_value
                    else:
                        remove_parts.append("#client_phone")
                        expr_attr_names["#client_phone"] = "client_phone"

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

            if send_email_on_save:
                email_to = (client_email_form or client_email_value or "").strip()
                if email_to:
                    public_info = _get_public_account_info(account_id)
                    account_slug = public_info.get("slug") or ""
                    business_name = public_info.get("business_name") or ""
                    new_status_norm = status.strip().lower()
                    if new_status_norm in ("pendente", "pending", "pending_confirmation"):
                        if not account_slug:
                            flash("Slug público não configurado no dashboard; e-mail de confirmação não enviado.", "warning")
                        elif not time_local:
                            flash("Sem horário: e-mail de confirmação não enviado (o link exige horário).", "warning")
                        else:
                            sent, send_err = _send_confirmation_email(
                                to_email=email_to,
                                client_name=client_name_value or current_fitting.get("client_name", ""),
                                fitting_id=fitting_id,
                                date_iso=date_iso,
                                time_local=time_local,
                                account_slug=account_slug,
                                business_name=business_name,
                            )
                            if sent:
                                flash("E-mail de confirmação enviado.", "success")
                            else:
                                flash(f"Não foi possível enviar o e-mail de confirmação: {send_err}", "warning")
                    elif new_status_norm in ("confirmado", "confirmed"):
                        ok = _send_booking_confirmed_email(
                            to_email=email_to,
                            client_name=client_name_value or current_fitting.get("client_name", ""),
                            fitting_id=fitting_id,
                            date_iso=date_iso,
                            time_local=time_local,
                            account_slug=account_slug,
                            business_name=business_name,
                            client_phone=client_phone_value or "",
                            item_description=item_description_value or current_fitting.get("item_description", ""),
                            notes=notes,
                        )
                        if ok:
                            flash("E-mail de confirmação (agendamento confirmado) enviado.", "success")
                        else:
                            flash("Não foi possível enviar o e-mail de confirmação (agendamento confirmado).", "warning")
                    elif new_status_norm in ("cancelado", "cancelled"):
                        ok = _send_booking_cancelled_email(
                            to_email=email_to,
                            client_name=client_name_value or current_fitting.get("client_name", ""),
                            date_iso=date_iso,
                            time_local=time_local,
                            business_name=business_name,
                        )
                        if ok:
                            flash("E-mail de cancelamento enviado.", "success")
                        else:
                            flash("Não foi possível enviar o e-mail de cancelamento.", "warning")

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
