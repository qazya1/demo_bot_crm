"""
max_notifications.py — Ежедневные уведомления для MAX-бота.

Аналог ofd_reminder_job из bot_main.py (TG-бот),
но отправляет через MaxBotAPI.send_message_to_user.

Покрывает:
- ОФД (ofd_expires_at)
- ФН (fn_expires_at)
- Маркировка (mark_expires_at)
- Купленные коды (codes.expires_at)
"""

import datetime
import logging

from max_api import MaxBotAPI

logger = logging.getLogger(__name__)


def _to_date(v) -> datetime.date | None:
    """Приводит значение из БД к datetime.date."""
    if not v:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    if len(s) >= 10:
        s = s[:10]
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


async def max_ofd_reminder_job(api: MaxBotAPI) -> None:
    """
    Ежедневная джоба напоминаний по ОФД/ФН/маркировке/кодам для MAX-пользователей.

    Вызывается из MaxBot.run_daily (job scheduler).
    """
    from db import (
        get_all_clients_with_ofd_max,
        get_client_by_inn,
        get_client_fn_slots,
        get_client_kkt_slots,
        get_client_mark_slots,
        list_codes_expiring_by_expires_at_max,
        purge_expired_codes_older_than_months,
    )

    now = datetime.date.today()

    # Чистим просроченные коды (общая для TG и MAX)
    try:
        await purge_expired_codes_older_than_months(3)
    except Exception as e:
        logger.warning("[max_purge_expired_codes] failed: %s", e)

    days_notice = [30, 7, 1, 0]
    sent = 0
    skipped = 0

    # ---- 1) ОФД по client_kkt ----
    try:
        clients = await get_all_clients_with_ofd_max()
    except Exception as e:
        logger.error("[max_ofd_reminder] get_all_clients_with_ofd_max failed: %s", e)
        clients = []

    for client in clients:
        user_id = client.get("user_id")
        if not user_id:
            skipped += 1
            continue

        inn = client.get("inn")
        ofd_slots = client.get("ofd_slots") or []

        for s in ofd_slots:
            date = _to_date(s.get("ofd_expires_at"))
            if not date:
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await api.send_message_to_user(
                    user_id,
                    f"⚠️ Напоминание:\n"
                    f"Срок действия ОФД по ККТ {s['kkt_slot']} "
                    f"заканчивается через {days_left} дн.\n"
                    f"Дата окончания: {date:%d.%m.%Y}\n"
                    f"ИНН: {inn}",
                )
                sent += 1
            except Exception as e:
                logger.warning("[max_ofd_reminder] failed to notify %s: %s", user_id, e)

        # ---- 2) ФН + Маркировка по ИНН ----
        try:
            cli = await get_client_by_inn(inn) if inn else None
        except Exception:
            cli = None

        if not cli:
            continue

        client_id = cli["id"]

        # ФН
        try:
            fn_slots = await get_client_fn_slots(client_id) or []
        except Exception:
            fn_slots = []

        for s in fn_slots:
            date = _to_date(s.get("fn_expires_at"))
            if not date:
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await api.send_message_to_user(
                    user_id,
                    f"⚠️ Напоминание:\n"
                    f"Срок действия ФН ККТ {s['kkt_slot']} "
                    f"заканчивается через {days_left} дн.\n"
                    f"Дата окончания: {date:%d.%m.%Y}\n"
                    f"ИНН: {inn}",
                )
                sent += 1
            except Exception as e:
                logger.warning("[max_fn_reminder] failed to notify %s: %s", user_id, e)

        # Маркировка
        try:
            mark_slots = await get_client_mark_slots(client_id) or []
        except Exception:
            mark_slots = []

        for s in mark_slots:
            date = _to_date(s.get("mark_expires_at"))
            if not date:
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await api.send_message_to_user(
                    user_id,
                    f"⚠️ Напоминание:\n"
                    f"Маркировка срок подписки по ККТ {s['kkt_slot']} "
                    f"заканчивается через {days_left} дн.\n"
                    f"Дата окончания: {date:%d.%m.%Y}\n"
                    f"ИНН: {inn}",
                )
                sent += 1
            except Exception as e:
                logger.warning("[max_mark_reminder] failed to notify %s: %s", user_id, e)

    # ---- 3) Купленные коды (codes.expires_at) ----
    try:
        exp_codes = await list_codes_expiring_by_expires_at_max(days_notice) or []
    except Exception as e:
        exp_codes = []
        logger.warning("[max_codes_expires_reminder] query failed: %s", e)

    for r in exp_codes:
        uid = r.get("user_id")
        if not uid:
            continue

        raw_dt = r.get("expires_at")
        date = _to_date(raw_dt)
        if not date:
            continue

        try:
            days_left = int(r.get("days_left"))
        except Exception:
            days_left = (date - now).days

        vendor = (r.get("vendor_name") or "ОФД").strip()
        code_val = (r.get("code") or "").strip()

        try:
            await api.send_message_to_user(
                uid,
                f"⚠️ Напоминание:\n"
                f"Срок действия кода {vendor} заканчивается через {days_left} дн.\n"
                f"Дата окончания: {date:%d.%m.%Y}\n"
                + (f"Код: {code_val}" if code_val else ""),
            )
            sent += 1
        except Exception as e:
            logger.warning("[max_codes_expires_reminder] failed to notify %s: %s", uid, e)

    logger.info(
        "[max_ofd_reminder] sent=%d, skipped_no_user=%d",
        sent,
        skipped,
    )
