import asyncio
import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import db

logger = logging.getLogger("app")

ASTERISK_ENABLED = os.getenv("ASTERISK_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
ASTERISK_CLIENT_FACTORY = os.getenv("ASTERISK_CLIENT_FACTORY", "AsteriskClient").strip()
ASTERISK_CALL_METHOD = os.getenv("ASTERISK_CALL_METHOD", "call").strip()
ASTERISK_WAIT_METHOD = os.getenv("ASTERISK_WAIT_METHOD", "wait_call_end").strip()

_ACTIVE_CALL_TASKS: dict[int, asyncio.Task] = {}


@dataclass(slots=True)
class CallContext:
    chat_id: int
    statuses: list[str] = field(default_factory=list)
    sent_messages: set[str] = field(default_factory=set)
    final_event: dict[str, Any] | None = None


def _sanitize_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return f"{digits}" if digits else ""


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4:
        return "*" * len(phone)
    return f"{phone[:2]}***{phone[-2:]}"


def _resolve_callable(module: Any):
    logger.debug(
        "Resolving Asterisk callable (factory=%s, call_method=%s)",
        ASTERISK_CLIENT_FACTORY,
        ASTERISK_CALL_METHOD,
    )
    factory = getattr(module, ASTERISK_CLIENT_FACTORY, None)
    if not callable(factory):
        logger.warning("Factory %s is not found/callable in python_asterisk_module", ASTERISK_CLIENT_FACTORY)
    else:
        logger.debug("Using factory %s from python_asterisk_module", ASTERISK_CLIENT_FACTORY)
        try:
            client = factory()
        except Exception:
            logger.exception("Factory %s raised an exception during client creation", ASTERISK_CLIENT_FACTORY)
        else:
            meth = getattr(client, ASTERISK_CALL_METHOD, None)
            if callable(meth):
                logger.debug("Resolved call method %s on client instance", ASTERISK_CALL_METHOD)
                return client, meth
            logger.warning(
                "Method %s is not found/callable on %s instance",
                ASTERISK_CALL_METHOD,
                ASTERISK_CLIENT_FACTORY,
            )

    fn = getattr(module, ASTERISK_CALL_METHOD, None)
    if callable(fn):
        logger.debug("Resolved module-level call method %s", ASTERISK_CALL_METHOD)
        return module, fn

    for name in ("call", "make_call", "start_call", "originate"):
        fn = getattr(module, name, None)
        if callable(fn):
            logger.warning(
                "Configured method %s not found, using fallback callable %s",
                ASTERISK_CALL_METHOD,
                name,
            )
            return module, fn

    logger.error(
        "Failed to resolve Asterisk callable in python_asterisk_module (factory=%s method=%s)",
        ASTERISK_CLIENT_FACTORY,
        ASTERISK_CALL_METHOD,
    )
    raise RuntimeError("python_asterisk_module callable not found")


def _wait_for_end(owner: Any, call_result: Any) -> None:
    waiter = getattr(owner, ASTERISK_WAIT_METHOD, None)
    if callable(waiter):
        logger.debug("Waiting for call end via owner method %s", ASTERISK_WAIT_METHOD)
        try:
            waiter(call_result)
            return
        except TypeError:
            logger.debug(
                "Owner wait method %s does not accept call_result; retrying without args",
                ASTERISK_WAIT_METHOD,
            )
            waiter()
            return

    if hasattr(call_result, "wait") and callable(call_result.wait):
        logger.debug("Waiting for call end via call_result.wait()")
        call_result.wait()
    else:
        logger.debug("No wait method found; assuming call is fire-and-forget")


def _notify_text_by_event(event: dict[str, Any]) -> str | None:
    status = str(event.get("final_status") or event.get("status") or "").strip().lower()
    side = str(event.get("failed_leg") or "").strip().lower()

    if status == "busy":
        if side == "client":
            return "📞 Клиент занят."
        if side == "specialist":
            return "📞 Специалист занят."
        return "📞 Не удалось соединить звонок: одна из сторон занята."
    if status == "not_accepted":
        if side == "client":
            return "📞 Клиент не принял звонок."
        if side == "specialist":
            return "📞 Специалист не принял звонок."
        return "📞 Одна из сторон не приняла звонок."
    if status == "no_answer":
        if side == "client":
            return "📞 Клиент не ответил на звонок."
        if side == "specialist":
            return "📞 Специалист не ответил на звонок."
        return "📞 Одна из сторон не ответила на звонок."
    if status in {"failed", "cancelled"}:
        return "⚠️ Не удалось выполнить звонок из-за технической ошибки. Попробуйте ещё раз позже."
    if status == "finished":
        return "📞 Звонок завершён."
    return None


async def _emit_system_message(ctx: CallContext, text: str) -> None:
    if text in ctx.sent_messages:
        return
    ctx.sent_messages.add(text)
    await db.add_chat_message(chat_id=int(ctx.chat_id), sender=2, text=text)


def _map_status_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("type") != "call_status":
        return None
    status = str(payload.get("status") or "").strip().lower()
    failed_leg = str(payload.get("failed_leg") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip().lower()

    if status in {"starting", "ringing_specialist", "ringing_client", "answered_one_side", "connected"}:
        return {"status": status, "failed_leg": failed_leg, "reason": reason}
    if status in {"busy", "no_answer", "not_accepted", "declined", "cancelled", "failed", "finished"}:
        return {"status": status, "failed_leg": failed_leg, "reason": reason}
    return {"status": "failed", "failed_leg": failed_leg, "reason": reason or status}


def _run_call_blocking(
    *,
    chat_id: int,
    specialist_phone: str,
    client_phone: str,
    event_handler,
) -> dict[str, Any] | None:
    if not ASTERISK_ENABLED:
        logger.error("Asterisk call aborted: ASTERISK_ENABLED=0")
        raise RuntimeError("ASTERISK_ENABLED=0")

    logger.debug("Importing python_asterisk_module")
    try:
        module = importlib.import_module("python_asterisk_module")
    except Exception:
        logger.exception("Failed to import python_asterisk_module")
        raise

    try:
        owner, call_fn = _resolve_callable(module)
    except Exception:
        logger.exception("Failed to resolve callable in python_asterisk_module")
        raise

    specialist_phone = _sanitize_phone(specialist_phone)
    client_phone = _sanitize_phone(client_phone)
    logger.debug(
        "Normalized call phones chat_id=%s specialist=%s client=%s",
        chat_id,
        _mask_phone(specialist_phone),
        _mask_phone(client_phone),
    )
    if not specialist_phone or not client_phone:
        logger.error("Asterisk call aborted: phone normalization failed")
        raise RuntimeError("Не удалось нормализовать номер телефона")

    logger.info(
        "Starting blocking Asterisk call chat_id=%s specialist=%s client=%s",
        chat_id,
        _mask_phone(specialist_phone),
        _mask_phone(client_phone),
    )
    try:
        try:
            call_result = call_fn(specialist_phone, client_phone, on_event=event_handler)
        except TypeError:
            logger.debug("Call method without on_event support, fallback to positional call")
            call_result = call_fn(specialist_phone, client_phone)
    except Exception:
        logger.exception(
            "Asterisk callable raised before/while sending request chat_id=%s specialist=%s client=%s",
            chat_id,
            _mask_phone(specialist_phone),
            _mask_phone(client_phone),
        )
        raise

    logger.debug("Call initiated successfully, waiting for completion chat_id=%s", chat_id)
    _wait_for_end(owner, call_result)
    final_event = getattr(call_result, "final_event", None)
    logger.info(
        "Blocking Asterisk call finished chat_id=%s specialist=%s client=%s final=%s",
        chat_id,
        _mask_phone(specialist_phone),
        _mask_phone(client_phone),
        final_event,
    )
    return final_event


async def start_chat_call(*, chat_id: int, specialist_phone: str, client_phone: str) -> None:
    if int(chat_id) in _ACTIVE_CALL_TASKS:
        logger.warning("Asterisk call already in progress for chat_id=%s", chat_id)
        raise RuntimeError("call_already_in_progress")

    ctx = CallContext(chat_id=int(chat_id))
    loop = asyncio.get_running_loop()
    logger.info("Scheduling Asterisk call task for chat_id=%s", chat_id)

    async def _handle_status_event(status_event: dict[str, Any]) -> None:
        status = status_event.get("status")
        if status:
            ctx.statuses.append(str(status))
            logger.info("Call status chat_id=%s status=%s event=%s", chat_id, status, status_event)
        if status == "starting":
            await _emit_system_message(ctx, "📞 Пытаемся соединить вас по звонку...")

    def _thread_event_handler(payload: dict[str, Any]) -> None:
        status_event = _map_status_event(payload)
        if not status_event:
            return
        loop.call_soon_threadsafe(asyncio.create_task, _handle_status_event(status_event))

    async def _task():
        try:
            final_event = await asyncio.to_thread(
                _run_call_blocking,
                chat_id=int(chat_id),
                specialist_phone=specialist_phone,
                client_phone=client_phone,
                event_handler=_thread_event_handler,
            )
            ctx.final_event = final_event or {"status": "failed", "failed_leg": "", "reason": "missing_final_event"}
            notify = _notify_text_by_event(ctx.final_event)
            if notify:
                await _emit_system_message(ctx, notify)
            logger.info("Asterisk call finished for chat_id=%s final_event=%s", chat_id, ctx.final_event)
        except Exception:
            logger.exception("Asterisk call failed for chat_id=%s", chat_id)
            await _emit_system_message(
                ctx,
                "⚠️ Не удалось выполнить звонок из-за технической ошибки. Попробуйте ещё раз позже.",
            )
        finally:
            logger.debug("Cleaning up Asterisk task for chat_id=%s", chat_id)
            _ACTIVE_CALL_TASKS.pop(int(chat_id), None)

    task = asyncio.create_task(_task(), name=f"asterisk_call_chat_{int(chat_id)}")
    _ACTIVE_CALL_TASKS[int(chat_id)] = task
    logger.debug("Asterisk task stored for chat_id=%s task_name=%s", chat_id, task.get_name())


def is_call_in_progress(chat_id: int) -> bool:
    task = _ACTIVE_CALL_TASKS.get(int(chat_id))
    in_progress = bool(task and not task.done())
    logger.debug("is_call_in_progress chat_id=%s -> %s", chat_id, in_progress)
    return in_progress
