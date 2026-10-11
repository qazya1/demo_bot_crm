#!/usr/bin/env python3
"""
Фоновые задачи для support-чатов (запуск через cron).

Использование:
    python run_chat_cron.py auto-close         # автозакрытие зависших заявок

Рекомендуемый crontab (каждые 6 часов):
    0 */6 * * *  cd /path/to/bot_crm && python run_chat_cron.py auto-close

Логика auto-close:
    Если специалист запросил закрытие чата, а клиент не отреагировал в течение
    48 часов (не нажал "Завершить" или "Продолжить общение"), чат закрывается
    автоматически. Клиенту и специалисту отправляется уведомление.
"""
import sys
import os
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("chat_cron")

# Таймаут ожидания ответа клиента (в часах)
AUTO_CLOSE_HOURS = 48

# 0 = клиент, 1 = специалист; используем 1 (от имени специалиста) для системных авто-сообщений
SENDER_SPEC = 1


async def _notify_user_tg(tg_user_id: int, text: str, reply_markup: dict | None = None) -> None:
    """Отправить уведомление пользователю в Telegram."""
    from bot.cross_notify import notify_specialist_tg
    if tg_user_id:
        await notify_specialist_tg(int(tg_user_id), text, reply_markup=reply_markup)


async def _notify_user_max(max_user_id: int, text: str) -> None:
    """Отправить уведомление пользователю в MAX."""
    from bot.cross_notify import notify_specialist_max
    if max_user_id:
        await notify_specialist_max(int(max_user_id), text)


async def run_auto_close() -> None:
    """
    Находит чаты, в которых специалист запросил закрытие, но клиент не ответил
    за 48 часов, и закрывает их автоматически.
    """
    from db.chats import get_stale_agent_close_requests, auto_close_chat, add_chat_message

    log.info("Поиск зависших запросов на закрытие (таймаут %d ч.)...", AUTO_CLOSE_HOURS)
    stale_chats = await get_stale_agent_close_requests(hours=AUTO_CLOSE_HOURS)
    log.info("Найдено чатов для автозакрытия: %d", len(stale_chats))

    closed = 0
    for chat in stale_chats:
        chat_id = int(chat.get("id") or 0)
        category = chat.get("category") or "—"
        client_tg_id = int(chat.get("client_tg_id") or 0)
        client_max_id = int(chat.get("client_max_id") or 0)
        spec_tg_id = int(chat.get("spec_tg_id") or 0)
        close_req_at = chat.get("close_req_agent_at")

        log.info(
            "Автозакрытие чата #%d (категория: %s, запрос закрытия: %s)",
            chat_id, category, close_req_at,
        )

        try:
            was_closed = await auto_close_chat(chat_id)
            if not was_closed:
                log.info("  Чат #%d уже закрыт, пропускаем.", chat_id)
                continue

            # Добавляем системное сообщение в историю чата
            await add_chat_message(
                chat_id,
                SENDER_SPEC,
                "🕐 Обращение закрыто автоматически: клиент не ответил на запрос завершения в течение 48 часов.",
            )

            # Уведомляем клиента
            client_text = (
                f"🕐 Ваше обращение <b>{category}</b> было автоматически закрыто.\n\n"
                "Специалист предлагал завершить обращение, но вы не ответили в течение 48 часов.\n\n"
                "Если вопрос не решён — создайте новое обращение через меню."
            )
            client_text_plain = (
                f"🕐 Ваше обращение «{category}» было автоматически закрыто.\n\n"
                "Специалист предлагал завершить обращение, но вы не ответили в течение 48 часов.\n\n"
                "Если вопрос не решён — создайте новое обращение через меню."
            )

            if client_tg_id:
                await _notify_user_tg(
                    client_tg_id,
                    client_text,
                    reply_markup={"inline_keyboard": [
                        [{"text": "📂 Мои обращения", "callback_data": "cc_my_chats"}],
                    ]},
                )
            if client_max_id:
                await _notify_user_max(client_max_id, client_text_plain)

            # Уведомляем специалиста
            spec_text = (
                f"✅ Обращение <b>#{chat_id}</b> «{category}» автоматически закрыто.\n\n"
                "Клиент не ответил на запрос завершения в течение 48 часов."
            )
            if spec_tg_id:
                await _notify_user_tg(spec_tg_id, spec_text)

            closed += 1
            log.info("  Чат #%d закрыт, уведомления отправлены.", chat_id)

        except Exception:
            log.exception("  Ошибка при автозакрытии чата #%d", chat_id)

    log.info("Автозакрытие завершено: закрыто %d / %d чатов.", closed, len(stale_chats))


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "auto-close":
        asyncio.run(run_auto_close())
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
