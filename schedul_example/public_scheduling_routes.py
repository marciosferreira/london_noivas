"""
public_scheduling_routes.py
Sistema de Agendamento Público com confirmação por email.

Tabelas DynamoDB esperadas:
  - fittings_table: agendamentos (já existente)
  - scheduling_config_table: configurações de horários/bloqueios
      PK: account_id (str)
      SK: config_key (str)  — ex: "weekly_schedule", "blocked_date#2025-03-15", etc.
  - clients_table: clientes (já existente)
  - itens_table: itens (já existente)

Fluxo:
  1. Cliente acessa /agendar/<account_slug>
  2. Escolhe data e horário disponíveis no calendário
  3. Preenche nome, telefone/WhatsApp, item (opcional), observações
  4. Recebe email com link de confirmação
  5. Clica no link → agendamento confirmado
  6. Admin pode cancelar/editar pelo dashboard existente
"""

import datetime
import os
import uuid
import hashlib
import hmac
import json
import pytz
from decimal import Decimal

from boto3.dynamodb.conditions import Key, Attr
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    abort,
)


# ── Configuração do fuso horário de Manaus ──────────────────────────
MANAUS_TZ = pytz.timezone("America/Manaus")  # UTC-4


def _now_manaus():
    return datetime.datetime.now(MANAUS_TZ)


def _today_manaus_iso():
    return _now_manaus().date().isoformat()


def _max_booking_date_iso(account_id: str, scheduling_config_table) -> str:
    """Data máxima para agendamento (público): hoje + N dias (configurável)."""
    days_ahead = 7
    try:
        resp = scheduling_config_table.get_item(
            Key={"account_id": account_id, "config_key": "scheduling_settings"}
        )
        item = resp.get("Item") or {}
        raw = item.get("max_booking_days_ahead")
        try:
            days_ahead = int(raw)
        except Exception:
            days_ahead = 7
    except Exception:
        days_ahead = 7

    if days_ahead <= 0:
        days_ahead = 7
    if days_ahead > 365:
        days_ahead = 365

    return (_now_manaus().date() + datetime.timedelta(days=days_ahead)).isoformat()


# ── Helpers de token para confirmação por email ─────────────────────
SECRET_KEY_FOR_TOKENS = "CHANGE_ME_TO_A_REAL_SECRET_KEY"  # Trocar em produção

DEFAULT_PUBLIC_ACCOUNT_ID = "37d5b37f-c920-4090-a682-7e1ed2e31a0f"


def _generate_confirmation_token(fitting_id: str) -> str:
    """Gera token HMAC para confirmar agendamento."""
    return hmac.new(
        SECRET_KEY_FOR_TOKENS.encode(),
        fitting_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def _verify_confirmation_token(fitting_id: str, token: str) -> bool:
    expected = _generate_confirmation_token(fitting_id)
    return hmac.compare_digest(expected, token)


# ── Helpers de configuração de horários ─────────────────────────────

DEFAULT_WEEKLY_SCHEDULE = {
    # dia_da_semana (0=seg, 6=dom): lista de horários disponíveis
    "0": ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00"],
    "1": ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00"],
    "2": ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00"],
    "3": ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00"],
    "4": ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00"],
    "5": ["09:00", "09:30", "10:00", "10:30", "11:00"],  # Sábado: só manhã
    "6": [],  # Domingo: fechado
}

DEFAULT_FITTING_DURATION_MINUTES = 60


def init_public_scheduling_routes(
    app,
    fittings_table,
    scheduling_config_table,
    itens_table,
    clients_table,
    mail=None,  # Flask-Mail instance (opcional)
    ses_client=None,
):
    """Rotas públicas de agendamento + admin de configuração."""
    global SECRET_KEY_FOR_TOKENS
    SECRET_KEY_FOR_TOKENS = (
        os.environ.get("BOOKING_TOKEN_SECRET")
        or os.environ.get("SECRET_KEY")
        or (app.secret_key if app.secret_key else SECRET_KEY_FOR_TOKENS)
    )

    # ════════════════════════════════════════════════════════════════
    # HELPERS INTERNOS
    # ════════════════════════════════════════════════════════════════

    def _safe_put_scheduling_config(item: dict) -> tuple[bool, str]:
        try:
            scheduling_config_table.put_item(Item=item)
            return True, ""
        except Exception as e:
            try:
                from botocore.exceptions import NoCredentialsError, ClientError

                if isinstance(e, NoCredentialsError):
                    msg = "Sem credenciais AWS (NoCredentialsError)."
                    print(f"Erro ao salvar scheduling_config: {msg}")
                    return False, msg

                if isinstance(e, ClientError):
                    code = (e.response.get("Error") or {}).get("Code") or "ClientError"
                    err_msg = (e.response.get("Error") or {}).get("Message") or str(e)
                    msg = f"{code}: {err_msg}"
                    print(f"Erro ao salvar scheduling_config: {msg}")
                    return False, msg
            except Exception:
                pass

            msg = str(e) or e.__class__.__name__
            print(f"Erro ao salvar scheduling_config: {msg}")
            return False, msg

    def _get_scheduling_settings(account_id: str) -> dict:
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": "scheduling_settings"}
            )
            item = resp.get("Item") or {}
            raw = item.get("default_fitting_duration_minutes")
            try:
                val = int(raw)
            except Exception:
                val = DEFAULT_FITTING_DURATION_MINUTES
            if val <= 0:
                val = DEFAULT_FITTING_DURATION_MINUTES
            raw_days = item.get("max_booking_days_ahead")
            try:
                days_val = int(raw_days)
            except Exception:
                days_val = 7
            if days_val <= 0:
                days_val = 7
            if days_val > 365:
                days_val = 365
            return {
                "default_fitting_duration_minutes": val,
                "max_booking_days_ahead": days_val,
            }
        except Exception:
            return {
                "default_fitting_duration_minutes": DEFAULT_FITTING_DURATION_MINUTES,
                "max_booking_days_ahead": 7,
            }

    def _save_scheduling_settings(account_id: str, settings: dict) -> tuple[bool, str]:
        raw = settings.get("default_fitting_duration_minutes")
        try:
            value_int = int(raw)
        except Exception:
            value_int = DEFAULT_FITTING_DURATION_MINUTES
        if value_int <= 0:
            value_int = DEFAULT_FITTING_DURATION_MINUTES

        raw_days = settings.get("max_booking_days_ahead")
        try:
            days_int = int(raw_days)
        except Exception:
            days_int = 7
        if days_int <= 0:
            days_int = 7
        if days_int > 365:
            days_int = 365

        return _safe_put_scheduling_config(
            {
                "account_id": account_id,
                "config_key": "scheduling_settings",
                "default_fitting_duration_minutes": value_int,
                "max_booking_days_ahead": days_int,
                "updated_at": _now_manaus().isoformat(),
            }
        )

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

    def _infer_slot_minutes(slots: list[str]) -> int:
        mins = []
        normalized = []
        for s in slots or []:
            m = _time_to_minutes(s)
            if m is not None:
                normalized.append(m)
        normalized = sorted(set(normalized))
        for i in range(1, len(normalized)):
            delta = normalized[i] - normalized[i - 1]
            if delta > 0:
                mins.append(delta)
        if not mins:
            return 30
        return max(5, min(mins))

    def _get_weekly_schedule(account_id: str) -> dict:
        """Retorna horários semanais configurados ou padrão."""
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": "weekly_schedule"}
            )
            item = resp.get("Item")
            if item and item.get("schedule_data"):
                return json.loads(item["schedule_data"]) if isinstance(item["schedule_data"], str) else item["schedule_data"]
        except Exception as e:
            print(f"Erro ao buscar weekly_schedule: {e}")
        return DEFAULT_WEEKLY_SCHEDULE

    def _save_weekly_schedule(account_id: str, schedule: dict):
        """Salva horários semanais."""
        return _safe_put_scheduling_config(
            {
                "account_id": account_id,
                "config_key": "weekly_schedule",
                "schedule_data": json.dumps(schedule),
                "updated_at": _now_manaus().isoformat(),
            }
        )

    def _get_blocked_dates(account_id: str) -> list:
        """Retorna lista de datas bloqueadas."""
        try:
            resp = scheduling_config_table.query(
                KeyConditionExpression=(
                    Key("account_id").eq(account_id)
                    & Key("config_key").begins_with("blocked_date#")
                )
            )
            items = resp.get("Items", [])
            return [i["config_key"].replace("blocked_date#", "") for i in items]
        except Exception as e:
            print(f"Erro ao buscar datas bloqueadas: {e}")
            return []

    def _block_date(account_id: str, date_iso: str, reason: str = ""):
        return _safe_put_scheduling_config(
            {
                "account_id": account_id,
                "config_key": f"blocked_date#{date_iso}",
                "reason": reason,
                "created_at": _now_manaus().isoformat(),
            }
        )

    def _unblock_date(account_id: str, date_iso: str):
        try:
            scheduling_config_table.delete_item(
                Key={"account_id": account_id, "config_key": f"blocked_date#{date_iso}"}
            )
            return True, ""
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"Erro ao remover bloqueio de data: {msg}")
            return False, msg

    def _get_blocked_slots(account_id: str, date_iso: str) -> list:
        """Retorna horários específicos bloqueados para uma data."""
        try:
            resp = scheduling_config_table.query(
                KeyConditionExpression=(
                    Key("account_id").eq(account_id)
                    & Key("config_key").begins_with(f"blocked_slot#{date_iso}#")
                )
            )
            items = resp.get("Items", [])
            return [i["config_key"].split("#")[2] for i in items]
        except Exception as e:
            print(f"Erro ao buscar slots bloqueados: {e}")
            return []

    def _get_booked_slots(account_id: str, date_iso: str, schedule_slots: list[str] | None = None) -> list:
        """Retorna slots que devem ser bloqueados por sobreposição com provas confirmadas."""
        try:
            settings = _get_scheduling_settings(account_id)
            default_duration = int(
                settings.get("default_fitting_duration_minutes") or DEFAULT_FITTING_DURATION_MINUTES
            )
            resp = fittings_table.query(
                KeyConditionExpression=(
                    Key("account_id").eq(account_id)
                    & Key("date_time_local").begins_with(date_iso)
                )
            )
            items = resp.get("Items", [])
            if schedule_slots is None:
                date_obj = datetime.date.fromisoformat(date_iso)
                weekday = str(date_obj.weekday())
                schedule = _get_weekly_schedule(account_id)
                schedule_slots = list(schedule.get(weekday, []))

            schedule_slots = [str(s).strip() for s in (schedule_slots or []) if str(s).strip()]
            slot_minutes = _infer_slot_minutes(schedule_slots)

            booked = set()
            for item in items:
                status = item.get("status", "").lower()
                if status in ("confirmado", "confirmed"):
                    time_local = item.get("time_local", "")
                    start_min = _time_to_minutes(time_local)
                    if start_min is None:
                        booked.add("*")
                        continue

                    raw_duration = item.get("duration_minutes")
                    try:
                        duration = int(raw_duration)
                    except Exception:
                        duration = default_duration
                    if duration <= 0:
                        duration = default_duration

                    end_min = start_min + duration
                    for slot in schedule_slots:
                        slot_start = _time_to_minutes(slot)
                        if slot_start is None:
                            continue
                        slot_end = slot_start + slot_minutes
                        if slot_start < end_min and start_min < slot_end:
                            booked.add(str(slot).strip())

            return list(booked)
        except Exception as e:
            print(f"Erro ao buscar slots agendados: {e}")
            return []

    def _get_available_slots(account_id: str, date_iso: str, allow_past: bool = False) -> list:
        """Calcula horários disponíveis para uma data específica."""
        # Verificar se a data está bloqueada
        blocked_dates = _get_blocked_dates(account_id)
        if date_iso in blocked_dates:
            return []

        # Obter dia da semana (0=seg, 6=dom)
        date_obj = datetime.date.fromisoformat(date_iso)
        weekday = str(date_obj.weekday())

        # Horários configurados para este dia da semana
        schedule = _get_weekly_schedule(account_id)
        all_day_slots = list(schedule.get(weekday, []))
        available = list(all_day_slots)

        if not available:
            return []

        # Remover horários bloqueados manualmente
        blocked_slots = _get_blocked_slots(account_id, date_iso)
        available = [t for t in available if t not in blocked_slots]

        # Remover horários já agendados (por sobreposição)
        booked = _get_booked_slots(account_id, date_iso, schedule_slots=all_day_slots)
        if "*" in booked:
            return []
        available = [t for t in available if t not in booked]

        # Se for hoje, remover horários que já passaram (somente no fluxo público)
        today_iso = _today_manaus_iso()
        if not allow_past and date_iso == today_iso:
            now_time = _now_manaus().strftime("%H:%M")
            available = [t for t in available if t > now_time]

        return sorted(available)

    def _get_slots_with_status(account_id: str, date_iso: str) -> list[dict]:
        blocked_dates = _get_blocked_dates(account_id)

        date_obj = datetime.date.fromisoformat(date_iso)
        weekday = str(date_obj.weekday())

        schedule = _get_weekly_schedule(account_id)
        all_slots = list(schedule.get(weekday, []))
        if not all_slots:
            return []

        all_slots = sorted([str(s).strip() for s in all_slots if str(s).strip()])

        blocked_slots = set([str(s).strip() for s in _get_blocked_slots(account_id, date_iso)])
        booked_slots = set(
            [str(s).strip() for s in _get_booked_slots(account_id, date_iso, schedule_slots=all_slots)]
        )
        booked_all = "*" in booked_slots

        today_iso = _today_manaus_iso()
        now_time = _now_manaus().strftime("%H:%M")

        out = []
        for t in all_slots:
            if date_iso in blocked_dates:
                status = "blocked"
            elif t in blocked_slots:
                status = "blocked"
            elif booked_all or t in booked_slots:
                status = "booked"
            elif date_iso == today_iso and t <= now_time:
                status = "past"
            else:
                status = "available"
            out.append({"time": t, "status": status})

        return out

    def _pending_booking_key(fitting_id: str) -> str:
        return f"pending_booking#{fitting_id}"

    def _save_pending_booking(account_id: str, fitting_id: str, payload: dict) -> tuple[bool, str]:
        try:
            scheduling_config_table.put_item(
                Item={
                    "account_id": account_id,
                    "config_key": _pending_booking_key(fitting_id),
                    "payload": json.dumps(payload),
                    "created_at": _now_manaus().isoformat(),
                }
            )
            return True, ""
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            print(f"Erro ao salvar pending_booking: {msg}")
            return False, msg

    def _get_pending_booking(account_id: str, fitting_id: str) -> dict | None:
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": _pending_booking_key(fitting_id)}
            )
            item = resp.get("Item")
            if not item:
                return None
            raw = item.get("payload")
            if not raw:
                return None
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, dict) else None
        except Exception as e:
            print(f"Erro ao buscar pending_booking: {e}")
            return None

    def _delete_pending_booking(account_id: str, fitting_id: str) -> None:
        try:
            scheduling_config_table.delete_item(
                Key={"account_id": account_id, "config_key": _pending_booking_key(fitting_id)}
            )
        except Exception as e:
            print(f"Erro ao remover pending_booking: {e}")

    def _parse_emails(raw: str) -> list[str]:
        if not raw:
            return []
        parts = []
        for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
            e = chunk.strip()
            if not e:
                continue
            parts.append(e)
        unique = []
        seen = set()
        for e in parts:
            key = e.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        return unique

    def _get_admin_booking_emails(account_id: str) -> list[str]:
        try:
            resp = scheduling_config_table.get_item(
                Key={"account_id": account_id, "config_key": "admin_booking_emails"}
            )
            item = resp.get("Item")
            if not item:
                return []
            raw = item.get("emails_data")
            if isinstance(raw, str) and raw.strip():
                data = json.loads(raw)
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()]
            return []
        except Exception as e:
            print(f"Erro ao buscar admin_booking_emails: {e}")
            return []

    def _save_admin_booking_emails(account_id: str, emails: list[str]):
        return _safe_put_scheduling_config(
            {
                "account_id": account_id,
                "config_key": "admin_booking_emails",
                "emails_data": json.dumps(emails),
                "updated_at": _now_manaus().isoformat(),
            }
        )

    def _send_admin_booking_notification(
        account_id: str,
        business_name: str,
        date_iso: str,
        time_local: str,
        client_name: str,
        client_email: str,
        client_phone: str,
        notes: str,
        item_description: str,
        item_id: str,
        fitting_id: str,
        items: list[dict] | None = None,
    ):
        emails = _get_admin_booking_emails(account_id)
        if not emails or not ses_client:
            return False
        sender = (
            os.environ.get("SES_SENDER")
            or os.environ.get("EMAIL_SENDER")
            or "nao_responda@londonnoivas.com.br"
        )
        subject_business = business_name or "Agendamento"
        subject = f"Novo agendamento - {subject_business}"
        date_obj = datetime.date.fromisoformat(date_iso)
        date_br = date_obj.strftime("%d/%m/%Y")
        dash_url = url_for("agenda", _external=True)
        if items:
            item_lines = []
            for it in items[:12]:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("item_title") or it.get("item_description") or "").strip()
                code = str(it.get("item_custom_id") or "").strip()
                iid = str(it.get("item_id") or "").strip()
                label = title or code or iid or "-"
                if code and title and code not in label:
                    label = f"{code} - {title}"
                item_lines.append(label)
            items_text = "\n".join(f"- {x}" for x in item_lines) if item_lines else "-"
            items_html = "<br>".join(f"• {x}" for x in item_lines) if item_lines else "-"
        else:
            items_text = item_description or item_id or "-"
            items_html = item_description or item_id or "-"
        body_text = (
            f"Novo agendamento recebido.\n\n"
            f"Negócio: {subject_business}\n"
            f"Data: {date_br}\n"
            f"Horário: {time_local}\n"
            f"Cliente: {client_name}\n"
            f"E-mail: {client_email}\n"
            f"Telefone: {client_phone}\n"
            f"Itens:\n{items_text}\n"
            f"Observações: {notes or '-'}\n"
            f"ID: {fitting_id}\n\n"
            f"Painel: {dash_url}"
        )
        body_html = (
            "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;\">"
            f"<h2 style=\"margin:0 0 12px;\">Novo agendamento</h2>"
            f"<p style=\"margin:0 0 16px;color:#555;\">{subject_business}</p>"
            "<table cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;min-width:360px;\">"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Data</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{date_br}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Horário</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{time_local}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Cliente</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{client_name}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">E-mail</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{client_email}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Telefone</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{client_phone}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Itens</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{items_html}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Observações</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{notes or '-'}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">ID</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{fitting_id}</td></tr>"
            "</table>"
            f"<p style=\"margin:16px 0 0;\"><a href=\"{dash_url}\">Abrir painel</a></p>"
            "</body></html>"
        )
        try:
            ses_client.send_email(
                Source=sender,
                Destination={"ToAddresses": emails},
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
            print(f"Erro ao enviar notificação admin (SES): {e}")
            return False

    def _get_account_by_slug(slug: str) -> dict | None:
        """Busca conta pelo slug público. Adapte conforme sua tabela de contas."""
        if slug == "default":
            raw = str(os.getenv("AI_SYNC_ACCOUNT_ID") or os.getenv("PUBLIC_CATALOG_ACCOUNT_IDS") or "").strip()
            account_id = DEFAULT_PUBLIC_ACCOUNT_ID
            if raw:
                parts = [p.strip() for p in raw.split(",") if p.strip()]
                if parts:
                    account_id = parts[0]
            business_name = str(os.getenv("PUBLIC_BOOKING_BUSINESS_NAME") or "").strip()
            return {"account_id": account_id, "slug": slug, "business_name": business_name}

        # Por enquanto, usamos o scheduling_config para guardar o mapeamento slug→account_id
        try:
            resp = scheduling_config_table.scan(
                FilterExpression=Attr("config_key").eq("account_slug")
                & Attr("slug").eq(slug)
            )
            items = resp.get("Items", [])
            if items:
                return {"account_id": items[0]["account_id"], "slug": slug, "business_name": items[0].get("business_name", "")}
        except Exception as e:
            print(f"Erro ao buscar conta pelo slug: {e}")
        return None

    def _send_confirmation_email(
        to_email: str,
        client_name: str,
        fitting_id: str,
        date_iso: str,
        time_local: str,
        account_slug: str,
        business_name: str = "",
    ) -> tuple[bool, str]:
        """Envia email de confirmação. Adapte para seu provedor de email."""
        token = _generate_confirmation_token(fitting_id)
        confirm_url = url_for(
            "confirm_booking", account_slug=account_slug,
            fitting_id=fitting_id, token=token, _external=True
        )

        # Se Flask-Mail estiver configurado, enviar
        if mail:
            try:
                from flask_mail import Message as MailMessage
                msg = MailMessage(
                    subject=f"Confirme seu agendamento - {business_name}",
                    recipients=[to_email],
                    html=render_template(
                        "email_confirmation.html",
                        client_name=client_name,
                        date_iso=date_iso,
                        time_local=time_local,
                        confirm_url=confirm_url,
                        business_name=business_name,
                    ),
                )
                mail.send(msg)
                return True, ""
            except Exception as e:
                msg = str(e) or e.__class__.__name__
                print(f"Erro ao enviar email: {msg}")
                return False, msg
        if ses_client:
            sender = (
                os.environ.get("SES_SENDER")
                or os.environ.get("EMAIL_SENDER")
            )
            if not sender or not str(sender).strip():
                msg = "SES_SENDER/EMAIL_SENDER não configurado."
                print(f"Erro ao enviar email (SES): {msg}")
                return False, msg
            subject_business = business_name or "Agendamento"
            subject = f"Confirme seu agendamento - {subject_business}"
            date_obj = datetime.date.fromisoformat(date_iso)
            date_br = date_obj.strftime("%d/%m/%Y")
            body_text = (
                f"Olá {client_name}!\n\n"
                f"Recebemos seu pedido de agendamento.\n\n"
                f"Data: {date_br}\n"
                f"Horário: {time_local}\n\n"
                f"Para confirmar, acesse:\n{confirm_url}\n\n"
                f"Se você não fez esse agendamento, ignore este e-mail."
            )
            body_html = render_template(
                "email_confirmation.html",
                client_name=client_name,
                date_iso=date_br,
                time_local=time_local,
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
                try:
                    from botocore.exceptions import ClientError, NoCredentialsError

                    if isinstance(e, NoCredentialsError):
                        msg = "Sem credenciais AWS (NoCredentialsError)."
                        print(f"Erro ao enviar email (SES): {msg}")
                        return False, msg

                    if isinstance(e, ClientError):
                        code = (e.response.get("Error") or {}).get("Code") or "ClientError"
                        err_msg = (e.response.get("Error") or {}).get("Message") or str(e)
                        msg = f"{code}: {err_msg}"
                        print(f"Erro ao enviar email (SES): {msg}")
                        return False, msg
                except Exception:
                    pass

                msg = str(e) or e.__class__.__name__
                print(f"Erro ao enviar email (SES): {msg}")
                return False, msg
        print("Mail não configurado e ses_client não disponível para envio.")
        return False, "Nenhum provedor de email configurado."

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
    ):
        token = _generate_confirmation_token(fitting_id)
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

        subject_business = business_name or "Agendamento"
        subject = f"Agendamento confirmado - {subject_business}"
        date_obj = datetime.date.fromisoformat(date_iso)
        date_br = date_obj.strftime("%d/%m/%Y")

        body_text = (
            f"Olá {client_name}!\n\n"
            f"Seu agendamento foi confirmado.\n\n"
            f"Negócio: {subject_business}\n"
            f"Data: {date_br}\n"
            f"Horário: {time_local}\n"
            f"Telefone: {client_phone}\n"
            f"Item: {item_description or '-'}\n"
            f"Observações: {notes or '-'}\n\n"
            f"Editar/Reagendar:\n{reschedule_url}\n\n"
            f"Cancelar:\n{cancel_url}\n"
        )

        body_html = (
            "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;\">"
            f"<h2 style=\"margin:0 0 12px;\">Agendamento confirmado</h2>"
            f"<p style=\"margin:0 0 16px;color:#555;\">{subject_business}</p>"
            "<table cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;min-width:360px;\">"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Data</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{date_br}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Horário</td><td style=\"padding:6px 10px;border:1px solid #eee;font-weight:600;\">{time_local}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Nome</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{client_name}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Telefone</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{client_phone}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Item</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{item_description or '-'}</td></tr>"
            f"<tr><td style=\"padding:6px 10px;border:1px solid #eee;color:#777;\">Observações</td><td style=\"padding:6px 10px;border:1px solid #eee;\">{notes or '-'}</td></tr>"
            "</table>"
            "<div style=\"margin:18px 0 0;display:flex;gap:10px;flex-wrap:wrap;\">"
            f"<a href=\"{reschedule_url}\" style=\"display:inline-block;background:#c8956c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;\">Editar/Reagendar</a>"
            f"<a href=\"{cancel_url}\" style=\"display:inline-block;background:#c75c5c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;\">Cancelar</a>"
            "</div>"
            "<p style=\"margin:16px 0 0;color:#777;font-size:12px;\">Se você não reconhece este agendamento, use o link de cancelar.</p>"
            "</body></html>"
        )

        if mail:
            try:
                from flask_mail import Message as MailMessage
                msg = MailMessage(
                    subject=subject,
                    recipients=[to_email],
                    html=body_html,
                    body=body_text,
                )
                mail.send(msg)
                return True
            except Exception as e:
                print(f"Erro ao enviar email: {e}")
                return False

        if ses_client:
            sender = (
                os.environ.get("SES_SENDER")
                or os.environ.get("EMAIL_SENDER")
                or "nao_responda@londonnoivas.com.br"
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

        print("Mail não configurado e ses_client não disponível para envio.")
        return False

    def _send_whatsapp_confirmation(phone: str, client_name: str, fitting_id: str,
                                      date_iso: str, time_local: str, account_slug: str,
                                      business_name: str = ""):
        """Gera link de WhatsApp para confirmação (alternativa ao email)."""
        token = _generate_confirmation_token(fitting_id)
        confirm_url = url_for(
            "confirm_booking", account_slug=account_slug,
            fitting_id=fitting_id, token=token, _external=True
        )
        # Formatar data brasileira
        date_obj = datetime.date.fromisoformat(date_iso)
        date_br = date_obj.strftime("%d/%m/%Y")
        
        message = (
            f"Olá {client_name}! 👋\n\n"
            f"Seu agendamento em *{business_name}* está quase pronto!\n\n"
            f"📅 Data: {date_br}\n"
            f"🕐 Horário: {time_local}\n\n"
            f"Para confirmar, clique no link abaixo:\n{confirm_url}\n\n"
            f"Se você não fez esse agendamento, ignore esta mensagem."
        )
        return message, confirm_url

    # ════════════════════════════════════════════════════════════════
    # ROTAS PÚBLICAS (cliente final)
    # ════════════════════════════════════════════════════════════════

    @app.route("/agendar")
    def public_booking_root():
        qs = request.args.to_dict(flat=True)
        env_slug = str(os.getenv("PUBLIC_BOOKING_SLUG") or "").strip()
        if env_slug:
            return redirect(url_for("public_booking", account_slug=env_slug, **qs))

        try:
            resp = scheduling_config_table.scan(
                FilterExpression=Attr("config_key").eq("account_slug")
            )
            items = resp.get("Items", [])
            slug = (items[0].get("slug") or "").strip() if items else ""
            if slug:
                return redirect(url_for("public_booking", account_slug=slug, **qs))
        except Exception:
            pass

        return redirect(url_for("public_booking", account_slug="default", **qs))

    @app.route("/agendar/<account_slug>")
    def public_booking(account_slug):
        """Página pública do calendário de agendamento."""
        account = _get_account_by_slug(account_slug)
        if not account:
            abort(404)

        account_id = account["account_id"]
        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account_id, scheduling_config_table)

        return render_template(
            "public_booking.html",
            account_slug=account_slug,
            business_name=account.get("business_name", ""),
            today_iso=today,
            max_date_iso=max_date,
        )

    @app.route("/api/available_slots/<account_slug>")
    def api_available_slots(account_slug):
        """API: retorna horários disponíveis para uma data."""
        account = _get_account_by_slug(account_slug)
        if not account:
            return jsonify({"error": "Conta não encontrada"}), 404

        date_iso = request.args.get("date")
        if not date_iso:
            return jsonify({"error": "Data não informada"}), 400

        # Validar intervalo de datas (hoje até +N dias)
        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account["account_id"], scheduling_config_table)
        if date_iso < today or date_iso > max_date:
            return jsonify({"slots": [], "message": "Data fora do período permitido"})

        slots = _get_available_slots(account["account_id"], date_iso)
        return jsonify({"slots": slots, "date": date_iso})

    @app.route("/api/day_slots/<account_slug>")
    def api_day_slots(account_slug):
        """API: retorna todos os horários do dia com status (available/booked/blocked/past)."""
        account = _get_account_by_slug(account_slug)
        if not account:
            return jsonify({"error": "Conta não encontrada"}), 404

        date_iso = request.args.get("date")
        if not date_iso:
            return jsonify({"error": "Data não informada"}), 400

        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account["account_id"], scheduling_config_table)
        if date_iso < today or date_iso > max_date:
            return jsonify({"slots": [], "message": "Data fora do período permitido"}), 200

        try:
            datetime.date.fromisoformat(date_iso)
        except Exception:
            return jsonify({"error": "Data inválida"}), 400

        slots = _get_slots_with_status(account["account_id"], date_iso)
        return jsonify({"slots": slots, "date": date_iso})

    @app.route("/api/admin/available_slots")
    def api_admin_available_slots():
        if not session.get("logged_in"):
            return jsonify({"error": "Não autorizado"}), 401

        account_id = session.get("account_id")
        if not account_id:
            return jsonify({"error": "Conta não encontrada"}), 400

        date_iso = request.args.get("date")
        if not date_iso:
            return jsonify({"error": "Data não informada"}), 400

        try:
            datetime.date.fromisoformat(date_iso)
        except Exception:
            return jsonify({"error": "Data inválida"}), 400

        slots = _get_available_slots(account_id, date_iso, allow_past=True)
        return jsonify({"slots": slots, "date": date_iso})

    @app.route("/api/calendar_data/<account_slug>")
    def api_calendar_data(account_slug):
        """API: retorna dados do calendário (dias com disponibilidade) para o mês."""
        account = _get_account_by_slug(account_slug)
        if not account:
            return jsonify({"error": "Conta não encontrada"}), 404

        month = request.args.get("month")  # formato: YYYY-MM
        if not month:
            month = _now_manaus().strftime("%Y-%m")

        account_id = account["account_id"]
        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account_id, scheduling_config_table)
        schedule = _get_weekly_schedule(account_id)
        blocked_dates = _get_blocked_dates(account_id)

        # Calcular primeiro e último dia do mês
        year, mon = int(month[:4]), int(month[5:7])
        first_day = datetime.date(year, mon, 1)
        if mon == 12:
            last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last_day = datetime.date(year, mon + 1, 1) - datetime.timedelta(days=1)

        calendar_days = []
        current = first_day
        while current <= last_day:
            date_str = current.isoformat()
            weekday = str(current.weekday())
            day_info = {
                "date": date_str,
                "day": current.day,
                "weekday": weekday,
            }

            # Verificar se está no intervalo permitido
            if date_str < today or date_str > max_date:
                day_info["status"] = "disabled"
            elif date_str in blocked_dates:
                day_info["status"] = "blocked"
            elif not schedule.get(weekday, []):
                day_info["status"] = "closed"
            else:
                # Verificar se tem horários disponíveis
                available = _get_available_slots(account_id, date_str)
                if available:
                    day_info["status"] = "available"
                    day_info["slots_count"] = len(available)
                else:
                    day_info["status"] = "full"

            calendar_days.append(day_info)
            current += datetime.timedelta(days=1)

        return jsonify({
            "month": month,
            "days": calendar_days,
            "today": today,
            "max_date": max_date,
        })

    def _public_item_payload(item: dict) -> dict:
        item_title = item.get("item_title", "") or ""
        item_description = item.get("item_description", "") or item.get("description", "") or ""
        image_url = item.get("image_url", "") or item.get("item_image_url", "") or ""
        return {
            "item_id": item.get("item_id", ""),
            "item_custom_id": item.get("item_custom_id", "") or "",
            "item_title": item_title,
            "item_description": item_description,
            "item_image_url": image_url,
            "title": item_title,
            "description": item_description,
            "image_url": image_url,
        }

    def _find_public_item_by_code(account_id: str, code: str) -> dict | None:
        code = (code or "").strip()
        if not code:
            return None

        allowed_statuses = {"available", "archive"}

        try:
            resp = itens_table.get_item(Key={"item_id": code})
            item = resp.get("Item")
            if item and item.get("status") in allowed_statuses and item.get("account_id") == account_id:
                return item
        except Exception:
            pass

        code_variants = [code, code.upper(), code.lower()]
        for c in code_variants:
            try:
                resp = itens_table.query(
                    IndexName="account_id-index",
                    KeyConditionExpression=Key("account_id").eq(account_id),
                    FilterExpression=Attr("status").is_in(list(allowed_statuses)) & Attr("item_custom_id").eq(c),
                    Limit=1,
                )
                items = resp.get("Items", [])
                if items:
                    return items[0]
            except Exception:
                try:
                    resp = itens_table.scan(
                        FilterExpression=Attr("account_id").eq(account_id)
                        & Attr("status").is_in(list(allowed_statuses))
                        & Attr("item_custom_id").eq(c),
                        Limit=1,
                    )
                    items = resp.get("Items", [])
                    if items:
                        return items[0]
                except Exception:
                    continue

        return None

    @app.route("/api/public/autocomplete_items/<account_slug>")
    def api_public_autocomplete_items(account_slug):
        account = _get_account_by_slug(account_slug)
        if not account:
            return jsonify([]), 404

        account_id = account["account_id"]
        term = request.args.get("term", "").strip().lower()
        if not term:
            return jsonify([])

        try:
            resp = itens_table.query(
                IndexName="account_id-index",
                KeyConditionExpression=Key("account_id").eq(account_id),
                FilterExpression=Attr("status").is_in(["available", "archive"]),
                Limit=800,
            )
            all_items = resp.get("Items", [])
        except Exception:
            return jsonify([])

        suggestions = []
        for item in all_items:
            custom_id = (item.get("item_custom_id") or "").lower()
            title = (item.get("item_title") or "").lower()
            description = (item.get("item_description") or item.get("description") or "").lower()
            item_id_value = (item.get("item_id") or "").lower()
            if term in custom_id or term in title or term in description or term in item_id_value:
                suggestions.append(_public_item_payload(item))

        return jsonify(suggestions[:8])

    @app.route("/api/public/item_lookup/<account_slug>")
    def api_public_item_lookup(account_slug):
        account = _get_account_by_slug(account_slug)
        if not account:
            return jsonify({"error": "Conta não encontrada"}), 404

        code = request.args.get("code", "").strip()
        if not code:
            return jsonify({"error": "Código não informado"}), 400

        item = _find_public_item_by_code(account["account_id"], code)
        if not item:
            return jsonify({"error": "Item não encontrado"}), 404

        return jsonify(_public_item_payload(item))

    @app.route("/agendar/<account_slug>/submit", methods=["POST"])
    def submit_booking(account_slug):
        """Processa o formulário de agendamento."""
        account = _get_account_by_slug(account_slug)
        if not account:
            abort(404)

        account_id = account["account_id"]

        # Dados do formulário
        date_iso = request.form.get("date_iso", "").strip()
        time_local = request.form.get("time_local", "").strip()
        client_name = request.form.get("client_name", "").strip()
        client_phone = request.form.get("client_phone", "").strip()
        client_email = request.form.get("client_email", "").strip()
        item_id = request.form.get("item_id", "").strip()
        item_custom_id = request.form.get("item_custom_id", "").strip()
        item_description = request.form.get("item_description", "").strip()
        item_image_url = request.form.get("item_image_url", "").strip()
        raw_items_json = request.form.get("items_json", "").strip()
        notes = request.form.get("notes", "").strip()

        selected_items = []
        if raw_items_json:
            try:
                parsed = json.loads(raw_items_json)
                if isinstance(parsed, list):
                    for raw in parsed[:12]:
                        if not isinstance(raw, dict):
                            continue
                        normalized = {
                            "item_id": str(raw.get("item_id") or "").strip(),
                            "item_custom_id": str(raw.get("item_custom_id") or "").strip(),
                            "item_title": str(raw.get("item_title") or raw.get("title") or "").strip(),
                            "item_description": str(raw.get("item_description") or raw.get("description") or "").strip(),
                            "item_image_url": str(raw.get("item_image_url") or raw.get("image_url") or "").strip(),
                        }
                        if (
                            not normalized["item_id"]
                            and not normalized["item_custom_id"]
                            and not normalized["item_title"]
                            and not normalized["item_description"]
                        ):
                            continue
                        selected_items.append(normalized)
            except Exception as e:
                print(f"Erro ao interpretar items_json: {e}")

        if selected_items:
            first = selected_items[0]
            if not item_id:
                item_id = first.get("item_id", "") or ""
            if not item_custom_id:
                item_custom_id = first.get("item_custom_id", "") or ""
            if not item_description:
                item_description = first.get("item_title", "") or first.get("item_description", "") or ""
            if not item_image_url:
                item_image_url = first.get("item_image_url", "") or ""

        # Validações
        errors = []
        if not date_iso:
            errors.append("Data é obrigatória.")
        if not time_local:
            errors.append("Horário é obrigatório.")
        if not client_name:
            errors.append("Nome é obrigatório.")
        if not client_phone:
            errors.append("Telefone/WhatsApp é obrigatório.")
        if not client_email:
            errors.append("E-mail é obrigatório.")

        # Validar intervalo de datas
        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account_id, scheduling_config_table)
        if date_iso and (date_iso < today or date_iso > max_date):
            settings = _get_scheduling_settings(account_id)
            days_ahead = settings.get("max_booking_days_ahead", 7)
            errors.append(f"Data fora do período permitido (máximo {days_ahead} dias de antecedência).")

        # Verificar se o horário está disponível
        if date_iso and time_local:
            available = _get_available_slots(account_id, date_iso)
            if time_local not in available:
                errors.append("Este horário não está mais disponível. Por favor, escolha outro.")

        if errors:
            # Retornar com erros via JSON (para AJAX) ou flash
            if request.headers.get("Accept") == "application/json":
                return jsonify({"success": False, "errors": errors}), 400
            for err in errors:
                flash(err, "danger")
            return redirect(url_for("public_booking", account_slug=account_slug))

        # Criar pedido de agendamento pendente (não bloqueia horário até confirmar)
        fitting_id = str(uuid.uuid4())
        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        pending_payload = {
            "account_id": account_id,
            "fitting_id": fitting_id,
            "date_local": date_iso,
            "time_local": time_local,
            "status": "pending_confirmation",
            "notes": notes,
            "client_name": client_name,
            "client_phone": client_phone,
            "client_email": client_email,
            "item_id": item_id,
            "item_custom_id": item_custom_id,
            "item_description": item_description,
            "item_image_url": item_image_url,
            "items": selected_items,
            "source": "public_booking",
            "created_at": now_utc,
        }

        try:
            ok, err = _save_pending_booking(account_id, fitting_id, pending_payload)
            if not ok:
                flash(f"Não foi possível iniciar o agendamento: {err}", "danger")
                return redirect(url_for("public_booking", account_slug=account_slug))

            # Enviar confirmação
            business_name = account.get("business_name", "")
            
            email_sent, email_err = _send_confirmation_email(
                to_email=client_email,
                client_name=client_name,
                fitting_id=fitting_id,
                date_iso=date_iso,
                time_local=time_local,
                account_slug=account_slug,
                business_name=business_name,
            )
            token = _generate_confirmation_token(fitting_id)
            confirm_url = url_for(
                "confirm_booking",
                account_slug=account_slug,
                fitting_id=fitting_id,
                token=token,
                _external=True,
            )

            # Formatar data brasileira para exibição
            date_obj = datetime.date.fromisoformat(date_iso)
            date_br = date_obj.strftime("%d/%m/%Y")

            return render_template(
                "booking_pending.html",
                account_slug=account_slug,
                business_name=business_name,
                client_name=client_name,
                client_email=client_email,
                client_phone=client_phone,
                date_br=date_br,
                time_local=time_local,
                item_custom_id=item_custom_id,
                item_description=item_description,
                item_image_url=item_image_url,
                items=selected_items,
                fitting_id=fitting_id,
                has_email=bool(email_sent),
                confirm_url=confirm_url if not email_sent else None,
                email_error=email_err if not email_sent else None,
            )

        except Exception as e:
            print(f"Erro ao criar agendamento: {e}")
            flash("Erro ao criar agendamento. Tente novamente.", "danger")
            return redirect(url_for("public_booking", account_slug=account_slug))

    @app.route("/agendar/<account_slug>/confirmar/<fitting_id>/<token>")
    def confirm_booking(account_slug, fitting_id, token):
        """Confirma agendamento via link do email/WhatsApp."""
        account = _get_account_by_slug(account_slug)
        if not account:
            abort(404)

        # Verificar token
        if not _verify_confirmation_token(fitting_id, token):
            return render_template("booking_result.html",
                                   success=False, message="Link de confirmação inválido ou expirado.",
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

        account_id = account["account_id"]

        # Buscar e atualizar agendamento
        try:
            pending = _get_pending_booking(account_id, fitting_id)
            if pending:
                date_iso = str(pending.get("date_local") or "").strip()
                time_local = str(pending.get("time_local") or "").strip()
                if not date_iso or not time_local:
                    _delete_pending_booking(account_id, fitting_id)
                    return render_template(
                        "booking_result.html",
                        success=False,
                        message="Não foi possível confirmar este agendamento. Refaça o agendamento.",
                        account_slug=account_slug,
                        business_name=account.get("business_name", ""),
                    )

                booked = _get_booked_slots(account_id, date_iso)
                if "*" in booked or time_local in booked:
                    _delete_pending_booking(account_id, fitting_id)
                    return render_template(
                        "booking_result.html",
                        success=False,
                        message="Não foi possível confirmar: este horário já foi agendado por outra pessoa.",
                        account_slug=account_slug,
                        business_name=account.get("business_name", ""),
                    )

                now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                dt_key = f"{date_iso}#{time_local}#{fitting_id}"
                settings = _get_scheduling_settings(account_id)
                default_duration = int(
                    settings.get("default_fitting_duration_minutes") or DEFAULT_FITTING_DURATION_MINUTES
                )
                fitting = {
                    "account_id": account_id,
                    "date_time_local": dt_key,
                    "fitting_id": fitting_id,
                    "date_local": date_iso,
                    "time_local": time_local,
                    "status": "Confirmado",
                    "duration_minutes": default_duration,
                    "notes": (pending.get("notes") or "").strip(),
                    "client_name": (pending.get("client_name") or "").strip(),
                    "client_phone": (pending.get("client_phone") or "").strip(),
                    "source": pending.get("source") or "public_booking",
                    "created_at": pending.get("created_at") or now_utc,
                    "updated_at": now_utc,
                }
                client_email = (pending.get("client_email") or "").strip()
                if client_email:
                    fitting["client_email"] = client_email
                item_id = (pending.get("item_id") or "").strip()
                if item_id:
                    fitting["item_id"] = item_id
                item_custom_id = (pending.get("item_custom_id") or "").strip()
                if item_custom_id:
                    fitting["item_custom_id"] = item_custom_id
                item_description = (pending.get("item_description") or "").strip()
                if item_description:
                    fitting["item_description"] = item_description
                item_image_url = (pending.get("item_image_url") or "").strip()
                if item_image_url:
                    fitting["item_image_url"] = item_image_url
                items = pending.get("items") if isinstance(pending.get("items"), list) else []
                if items:
                    fitting["items"] = items

                fittings_table.put_item(Item=fitting)
                _delete_pending_booking(account_id, fitting_id)

                if client_email:
                    _send_booking_confirmed_email(
                        to_email=client_email,
                        client_name=fitting.get("client_name", ""),
                        fitting_id=fitting_id,
                        date_iso=fitting.get("date_local", ""),
                        time_local=fitting.get("time_local", ""),
                        account_slug=account_slug,
                        business_name=account.get("business_name", ""),
                        client_phone=fitting.get("client_phone", ""),
                        item_description=fitting.get("item_description", ""),
                        notes=fitting.get("notes", ""),
                    )

                _send_admin_booking_notification(
                    account_id=account_id,
                    business_name=account.get("business_name", ""),
                    date_iso=date_iso,
                    time_local=time_local,
                    client_name=fitting.get("client_name", ""),
                    client_email=client_email,
                    client_phone=fitting.get("client_phone", ""),
                    notes=fitting.get("notes", ""),
                    item_description=fitting.get("item_description", ""),
                    item_id=item_id,
                    fitting_id=fitting_id,
                    items=items,
                )

                return render_template(
                    "booking_result.html",
                    success=True,
                    message="Agendamento confirmado com sucesso! 🎉",
                    fitting=fitting,
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                    ga_event_name="booking_confirmed",
                    ga_event_params={
                        "event_category": "booking",
                        "event_label": (account.get("business_name", "") or account_slug),
                        "account_slug": account_slug,
                        "date_iso": fitting.get("date_local", ""),
                        "time_local": fitting.get("time_local", ""),
                        "transport_type": "beacon",
                    },
                )

            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id) & Attr("fitting_id").eq(fitting_id)
            )
            items = resp.get("Items", [])
            if not items:
                return render_template(
                    "booking_result.html",
                    success=False,
                    message="Agendamento não encontrado.",
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                )

            fitting = items[0]
            current_status = fitting.get("status", "")

            if current_status == "Confirmado":
                return render_template(
                    "booking_result.html",
                    success=True,
                    message="Este agendamento já foi confirmado!",
                    fitting=fitting,
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                )

            if str(current_status).lower() in ("cancelado", "cancelled"):
                return render_template(
                    "booking_result.html",
                    success=False,
                    message="Este agendamento foi cancelado.",
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                )

            fittings_table.update_item(
                Key={"account_id": account_id, "date_time_local": fitting["date_time_local"]},
                UpdateExpression="SET #status = :status, updated_at = :updated",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": "Confirmado",
                    ":updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

            fitting["status"] = "Confirmado"
            client_email = (fitting.get("client_email") or "").strip()
            if client_email:
                _send_booking_confirmed_email(
                    to_email=client_email,
                    client_name=fitting.get("client_name", ""),
                    fitting_id=fitting_id,
                    date_iso=fitting.get("date_local", ""),
                    time_local=fitting.get("time_local", ""),
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                    client_phone=fitting.get("client_phone", ""),
                    item_description=fitting.get("item_description", ""),
                    notes=fitting.get("notes", ""),
                )

            return render_template(
                "booking_result.html",
                success=True,
                message="Agendamento confirmado com sucesso! 🎉",
                fitting=fitting,
                account_slug=account_slug,
                business_name=account.get("business_name", ""),
                ga_event_name="booking_confirmed",
                ga_event_params={
                    "event_category": "booking",
                    "event_label": (account.get("business_name", "") or account_slug),
                    "account_slug": account_slug,
                    "date_iso": fitting.get("date_local", ""),
                    "time_local": fitting.get("time_local", ""),
                    "transport_type": "beacon",
                },
            )

        except Exception as e:
            print(f"Erro ao confirmar agendamento: {e}")
            return render_template("booking_result.html",
                                   success=False, message="Erro ao confirmar. Tente novamente.",
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

    @app.route("/agendar/<account_slug>/cancelar/<fitting_id>/<token>", methods=["GET", "POST"])
    def cancel_booking_public(account_slug, fitting_id, token):
        """Cancela agendamento via link público (com confirmação)."""
        account = _get_account_by_slug(account_slug)
        if not account:
            abort(404)

        if not _verify_confirmation_token(fitting_id, token):
            return render_template("booking_result.html",
                                   success=False, message="Link inválido.",
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

        account_id = account["account_id"]

        try:
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("fitting_id").eq(fitting_id)
            )
            items = resp.get("Items", [])
            if not items:
                return render_template("booking_result.html",
                                       success=False, message="Agendamento não encontrado.",
                                       account_slug=account_slug,
                                       business_name=account.get("business_name", ""))

            fitting = items[0]
            if request.method == "GET":
                return render_template(
                    "booking_result.html",
                    confirm_cancel=True,
                    message="Tem certeza que deseja cancelar este agendamento?",
                    fitting=fitting,
                    account_slug=account_slug,
                    business_name=account.get("business_name", ""),
                    fitting_id=fitting_id,
                    token=token,
                )

            fittings_table.update_item(
                Key={
                    "account_id": account_id,
                    "date_time_local": fitting["date_time_local"],
                },
                UpdateExpression="SET #status = :status, updated_at = :updated",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": "Cancelado",
                    ":updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

            return render_template("booking_result.html",
                                   success=True,
                                   message="Agendamento cancelado com sucesso.",
                                   fitting={**fitting, "status": "Cancelado"},
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

        except Exception as e:
            print(f"Erro ao cancelar: {e}")
            return render_template("booking_result.html",
                                   success=False, message="Erro ao cancelar.",
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

    @app.route("/agendar/<account_slug>/reagendar/<fitting_id>/<token>", methods=["GET", "POST"])
    def reschedule_booking(account_slug, fitting_id, token):
        """Permite reagendar (alterar data/hora) de um agendamento existente."""
        account = _get_account_by_slug(account_slug)
        if not account:
            abort(404)

        if not _verify_confirmation_token(fitting_id, token):
            return render_template("booking_result.html",
                                   success=False, message="Link inválido.",
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

        account_id = account["account_id"]

        # Buscar agendamento atual
        try:
            resp = fittings_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("fitting_id").eq(fitting_id)
            )
            items = resp.get("Items", [])
            if not items:
                return render_template("booking_result.html",
                                       success=False, message="Agendamento não encontrado.",
                                       account_slug=account_slug,
                                       business_name=account.get("business_name", ""))

            fitting = items[0]
        except Exception as e:
            print(f"Erro ao buscar agendamento: {e}")
            abort(500)

        if request.method == "GET":
            today = _today_manaus_iso()
            max_date = _max_booking_date_iso(account_id, scheduling_config_table)
            return render_template(
                "public_reschedule.html",
                account_slug=account_slug,
                business_name=account.get("business_name", ""),
                fitting=fitting,
                fitting_id=fitting_id,
                token=token,
                today_iso=today,
                max_date_iso=max_date,
            )

        # POST - Processar reagendamento
        new_date = request.form.get("date_iso", "").strip()
        new_time = request.form.get("time_local", "").strip()

        if not new_date or not new_time:
            flash("Data e horário são obrigatórios.", "danger")
            return redirect(url_for("reschedule_booking",
                                    account_slug=account_slug,
                                    fitting_id=fitting_id, token=token))

        today = _today_manaus_iso()
        max_date = _max_booking_date_iso(account_id, scheduling_config_table)
        if new_date < today or new_date > max_date:
            settings = _get_scheduling_settings(account_id)
            days_ahead = settings.get("max_booking_days_ahead", 7)
            flash(f"Data fora do período permitido (máximo {days_ahead} dias de antecedência).", "danger")
            return redirect(url_for("reschedule_booking",
                                    account_slug=account_slug,
                                    fitting_id=fitting_id, token=token))

        # Validar disponibilidade
        available = _get_available_slots(account_id, new_date)
        if new_time not in available:
            flash("Horário não disponível. Escolha outro.", "danger")
            return redirect(url_for("reschedule_booking",
                                    account_slug=account_slug,
                                    fitting_id=fitting_id, token=token))

        try:
            old_dt_key = fitting["date_time_local"]
            new_dt_key = f"{new_date}#{new_time}#{fitting_id}"
            now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # Deletar item antigo
            fittings_table.delete_item(
                Key={"account_id": account_id, "date_time_local": old_dt_key}
            )

            # Criar com nova data/hora
            fitting["date_time_local"] = new_dt_key
            fitting["date_local"] = new_date
            fitting["time_local"] = new_time
            fitting["updated_at"] = now_utc
            fitting["status"] = "Confirmado"
            if not fitting.get("duration_minutes"):
                settings = _get_scheduling_settings(account_id)
                fitting["duration_minutes"] = int(
                    settings.get("default_fitting_duration_minutes") or DEFAULT_FITTING_DURATION_MINUTES
                )
            fittings_table.put_item(Item=fitting)

            date_obj = datetime.date.fromisoformat(new_date)
            date_br = date_obj.strftime("%d/%m/%Y")

            return render_template("booking_result.html",
                                   success=True,
                                   message=f"Reagendado com sucesso para {date_br} às {new_time}! ✅",
                                   fitting=fitting,
                                   account_slug=account_slug,
                                   business_name=account.get("business_name", ""))

        except Exception as e:
            print(f"Erro ao reagendar: {e}")
            flash("Erro ao reagendar. Tente novamente.", "danger")
            return redirect(url_for("reschedule_booking",
                                    account_slug=account_slug,
                                    fitting_id=fitting_id, token=token))

    # ════════════════════════════════════════════════════════════════
    # ROTAS ADMIN (dashboard interno)
    # ════════════════════════════════════════════════════════════════

    @app.route("/admin/scheduling_config", methods=["GET", "POST"])
    def scheduling_config():
        """Painel administrativo de configuração de horários."""
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        account_id = session.get("account_id")

        if request.method == "POST":
            action = request.form.get("action")

            if action == "save_schedule":
                # Salvar horários semanais
                schedule = {}
                for day in range(7):
                    slots_str = request.form.get(f"slots_{day}", "").strip()
                    if slots_str:
                        schedule[str(day)] = [s.strip() for s in slots_str.split(",") if s.strip()]
                    else:
                        schedule[str(day)] = []
                ok, err = _save_weekly_schedule(account_id, schedule)
                if ok:
                    flash("Horários atualizados com sucesso!", "success")
                else:
                    flash(f"Não foi possível salvar os horários: {err}", "danger")

            elif action == "block_date":
                date_to_block = request.form.get("block_date", "").strip()
                reason = request.form.get("block_reason", "").strip()
                if date_to_block:
                    ok, err = _block_date(account_id, date_to_block, reason)
                    if ok:
                        flash(f"Data {date_to_block} bloqueada.", "success")
                    else:
                        flash(f"Não foi possível bloquear a data: {err}", "danger")

            elif action == "unblock_date":
                date_to_unblock = request.form.get("unblock_date", "").strip()
                if date_to_unblock:
                    ok, err = _unblock_date(account_id, date_to_unblock)
                    if ok:
                        flash(f"Data {date_to_unblock} desbloqueada.", "success")
                    else:
                        flash(f"Não foi possível desbloquear a data: {err}", "danger")

            elif action == "save_slug":
                slug = request.form.get("slug", "").strip().lower()
                business_name = request.form.get("business_name", "").strip()
                if slug:
                    ok, err = _safe_put_scheduling_config(
                        {
                            "account_id": account_id,
                            "config_key": "account_slug",
                            "slug": slug,
                            "business_name": business_name,
                            "updated_at": _now_manaus().isoformat(),
                        }
                    )
                    if ok:
                        flash(f"Link público configurado: /agendar/{slug}", "success")
                    else:
                        flash(f"Não foi possível salvar o link público: {err}", "danger")

            elif action == "save_admin_emails":
                raw = request.form.get("admin_emails", "").strip()
                emails = _parse_emails(raw)
                ok, err = _save_admin_booking_emails(account_id, emails)
                if ok:
                    flash("E-mails de notificação atualizados.", "success")
                else:
                    flash(f"Não foi possível salvar os e-mails: {err}", "danger")

            elif action == "save_settings":
                raw_duration = request.form.get("default_fitting_duration_minutes", "").strip()
                raw_days_ahead = request.form.get("max_booking_days_ahead", "").strip()
                ok, err = _save_scheduling_settings(
                    account_id,
                    {
                        "default_fitting_duration_minutes": raw_duration,
                        "max_booking_days_ahead": raw_days_ahead,
                    },
                )
                if ok:
                    flash("Configurações atualizadas.", "success")
                else:
                    flash(f"Não foi possível salvar as configurações: {err}", "danger")

            return redirect(url_for("scheduling_config"))

        # GET
        schedule = _get_weekly_schedule(account_id)
        blocked_dates = _get_blocked_dates(account_id)
        settings = _get_scheduling_settings(account_id)

        # Buscar slug atual
        try:
            resp = scheduling_config_table.scan(
                FilterExpression=Attr("account_id").eq(account_id)
                & Attr("config_key").eq("account_slug")
            )
            slug_items = resp.get("Items", [])
            current_slug = slug_items[0].get("slug", "") if slug_items else ""
            business_name = slug_items[0].get("business_name", "") if slug_items else ""
        except Exception:
            current_slug = ""
            business_name = ""

        admin_emails = _get_admin_booking_emails(account_id)

        day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

        return render_template(
            "scheduling_config.html",
            schedule=schedule,
            blocked_dates=sorted(blocked_dates),
            day_names=day_names,
            current_slug=current_slug,
            business_name=business_name,
            admin_emails=", ".join(admin_emails),
            default_fitting_duration_minutes=settings.get("default_fitting_duration_minutes", DEFAULT_FITTING_DURATION_MINUTES),
            max_booking_days_ahead=settings.get("max_booking_days_ahead", 7),
        )
