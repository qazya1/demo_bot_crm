"""Incoming bot API views extracted from the legacy views module."""

import asyncio as _asyncio
import json
import logging as _logging
import os as _os
from datetime import datetime

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..sql_cursor import with_sql_rewrite
from ..services.task_service import _upsert_task

connection = with_sql_rewrite(connection)

_bot_api_logger = _logging.getLogger("bot_app.bot_api")
_DJANGO_CRM_TOKEN = _os.getenv("DJANGO_CRM_TOKEN", "")


def _check_bearer_token(request):
    """Verify Bearer token from Authorization header. Returns error response or None."""
    if not _DJANGO_CRM_TOKEN:
        return JsonResponse({"success": False, "error": "API token not configured"}, status=500)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != _DJANGO_CRM_TOKEN:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=401)
    return None


def _find_client_id(user_data: dict):
    """Find client_id by inn or phone. Returns int or None."""
    inn = user_data.get("inn", "")
    phone = user_data.get("phone", "")
    if not inn and not phone:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM users WHERE (inn IS NOT NULL AND inn = %s) OR (phone IS NOT NULL AND phone = %s) LIMIT 1",
            [inn, phone]
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _source_label(source: str) -> str:
    return "Telegram" if source == "telegram" else "Max" if source == "max" else source


def _notify(coro):
    """Run async create_notification coroutine from sync Django context. Best-effort."""
    try:
        _asyncio.run(coro)
    except Exception:
        _bot_api_logger.exception("Failed to create notification")


@csrf_exempt
@require_POST
def api_receive_sale(request):
    """API: приём уведомления о продаже от ботов (TG/Max)."""
    err = _check_bearer_token(request)
    if err:
        return err
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    source = data.get("source", "unknown")
    order_id = data.get("order_id")
    order_data = data.get("order_data") or {}
    user_data = data.get("user_data") or {}
    items = data.get("items") or []

    client_id = _find_client_id(user_data)

    label = _source_label(source)
    amount = order_data.get("amount", 0)
    buyer = user_data.get("first_name") or user_data.get("phone") or "—"
    title = f"Новый заказ #{order_id} ({label})"
    message = f"Сумма: {amount} руб.\nПокупатель: {buyer}"
    if items:
        item_names = ", ".join(i.get("name") or i.get("legacy_item_title") or "?" for i in items[:3])
        message += f"\nТовары: {item_names}"

    from db.notifications import create_notification
    _notify(create_notification(
        type_code="order_new",
        title=title,
        message=message,
        priority="high",
        client_id=client_id,
        order_id=int(order_id) if order_id else None,
        unique_key=f"sale_{source}_{order_id}",
    ))

    order_source_id = int(order_id) if str(order_id or '').isdigit() else None
    canonical_dedupe = f"order:order:{order_source_id}" if order_source_id else f"order:event:{source}:{order_id}"
    _upsert_task(
        title=title,
        description=message,
        source_type='order',
        source_id=order_source_id,
        dedupe_key=canonical_dedupe,
        priority='medium',
        meta={
            'order_id': order_source_id or order_id,
            'client_id': client_id,
            'client_name': buyer,
            'channel': source,
            'amount': amount,
            'items': items[:10],
        },
    )

    return JsonResponse({"success": True, "source": source, "order_id": order_id})


@csrf_exempt
@require_POST
def api_receive_application(request):
    """API: приём заявки (анкеты) от ботов (TG/Max)."""
    err = _check_bearer_token(request)
    if err:
        return err
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    source = data.get("source", "unknown")
    user_data = data.get("user_data") or {}
    form_data = data.get("form_data") or {}

    client_id = _find_client_id(user_data)

    label = _source_label(source)
    buyer = user_data.get("first_name") or user_data.get("phone") or "—"
    title = f"Новая заявка ({label})"
    message = f"Клиент: {buyer}"
    if user_data.get("inn"):
        message += f"\nИНН: {user_data['inn']}"
    if form_data:
        tp = form_data.get("tp_name") or form_data.get("activity") or ""
        if tp:
            message += f"\nДеятельность: {tp}"

    from db.notifications import create_notification
    _notify(create_notification(
        type_code="order_new",
        title=title,
        message=message,
        priority="medium",
        client_id=client_id,
        unique_key=f"app_{source}_{buyer}_{datetime.now().strftime('%Y%m%d%H%M')}",
    ))

    return JsonResponse({"success": True, "source": source})


@csrf_exempt
@require_POST
def api_receive_verification(request):
    """API: приём запроса на верификацию от ботов (TG/Max)."""
    err = _check_bearer_token(request)
    if err:
        return err
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    source = data.get("source", "unknown")
    user_data = data.get("user_data") or {}

    client_id = _find_client_id(user_data)

    label = _source_label(source)
    buyer = user_data.get("first_name") or user_data.get("phone") or "—"
    title = f"Запрос верификации ({label})"
    message = f"Клиент: {buyer}"
    if user_data.get("inn"):
        message += f"\nИНН: {user_data['inn']}"
    if user_data.get("phone"):
        message += f"\nТелефон: {user_data['phone']}"

    from db.notifications import create_notification
    _notify(create_notification(
        type_code="order_new",
        title=title,
        message=message,
        priority="medium",
        client_id=client_id,
        unique_key=f"verif_{source}_{user_data.get('inn', '')}_{datetime.now().strftime('%Y%m%d')}",
    ))

    return JsonResponse({"success": True, "source": source})
