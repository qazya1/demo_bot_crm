#!/usr/bin/env python3
"""
Задачи обслуживания БД (запуск через cron).

Использование:
    python run_maintenance_cron.py weekly    # еженедельно: агрегация и очистка bot_events
    python run_maintenance_cron.py monthly   # ежемесячно: очистка старых уведомлений

Рекомендуемый crontab:
    0 2 * * 1   cd /path/to/bot_crm && python run_maintenance_cron.py weekly
    0 3 1 * *   cd /path/to/bot_crm && python run_maintenance_cron.py monthly
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
log = logging.getLogger("maintenance_cron")

# Хранить сырые bot_events за последние N дней (остальное — в bot_events_weekly)
BOT_EVENTS_KEEP_DAYS = 7

# Хранить уведомления за последние N дней
NOTIFICATIONS_KEEP_DAYS = 30


async def run_weekly() -> None:
    """Агрегирует bot_events в bot_events_weekly и удаляет события старше 7 дней."""
    from db.events import aggregate_and_cleanup_bot_events

    log.info(
        "Запуск еженедельной очистки bot_events (keep_days=%d)...",
        BOT_EVENTS_KEEP_DAYS,
    )
    deleted = await aggregate_and_cleanup_bot_events(keep_days=BOT_EVENTS_KEEP_DAYS)
    log.info("bot_events: удалено %d строк.", deleted)


async def run_monthly() -> None:
    """Удаляет уведомления старше 30 дней (notification_delivery_log — каскадно)."""
    from db.notifications import cleanup_old_notifications

    log.info(
        "Запуск ежемесячной очистки notifications (keep_days=%d)...",
        NOTIFICATIONS_KEEP_DAYS,
    )
    deleted = await cleanup_old_notifications(keep_days=NOTIFICATIONS_KEEP_DAYS)
    log.info("notifications: удалено %d строк.", deleted)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "weekly":
        asyncio.run(run_weekly())
    elif cmd == "monthly":
        asyncio.run(run_monthly())
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
