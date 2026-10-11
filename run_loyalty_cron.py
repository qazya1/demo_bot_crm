#!/usr/bin/env python3
"""
Фоновые задачи финансовой модели (запуск через cron).

Использование:
    python run_loyalty_cron.py daily          # ежедневно: сгорание баллов + неактивность
    python run_loyalty_cron.py monthly        # ежемесячно: расчёт зарплат партнёров
    python run_loyalty_cron.py monthly-close  # закрытие месяца: draft → approved (директор)
    python run_loyalty_cron.py rebuild <account_id>  # пересборка баланса

Рекомендуемый crontab:
    0 3 * * *   cd /path/to/bot_crm && python run_loyalty_cron.py daily
    0 4 1 * *   cd /path/to/bot_crm && python run_loyalty_cron.py monthly
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
log = logging.getLogger("loyalty_cron")


# ─────────────────────────────────────────────────────────────
# E.1  Ежедневные задачи
# ─────────────────────────────────────────────────────────────

async def run_daily():
    """Сгорание баллов + проверка неактивности."""
    from services.ledger import expire_transactions
    from services.loyalty_engine import is_loyalty_enabled

    if not await is_loyalty_enabled():
        log.info("Loyalty disabled — skip daily tasks.")
        return

    # 1) Сгорание баллов
    log.info("Expiring transactions...")
    total_expired = 0
    while True:
        batch = await expire_transactions(batch_size=500)
        total_expired += batch
        if batch < 500:
            break
    log.info("Expired: %d transactions.", total_expired)

    # 2) Неактивность — перебираем пользователей с последней активностью > N дней
    from db.loyalty import list_inactive_profiles
    from services.loyalty_engine import apply_inactivity_penalty
    from services.rules_resolver import get_system_number

    inactivity_days = int(await get_system_number("LOYALTY_INACTIVITY_DAYS", 180) or 180)
    penalty_points = int(await get_system_number("LOYALTY_INACTIVITY_PENALTY_RATING", 5) or 5)

    try:
        inactive = await list_inactive_profiles(inactivity_days, limit=200)
    except Exception:
        log.warning("list_inactive_profiles not available, skipping inactivity check.")
        inactive = []

    penalized = 0
    for profile in inactive:
        uid = profile.get("internal_user_id")
        if uid:
            try:
                res = await apply_inactivity_penalty(uid, inactivity_days, penalty_points)
                if res.get("ok") and not res.get("duplicate"):
                    penalized += 1
            except Exception:
                log.exception("Inactivity penalty failed for user %s", uid)
    log.info("Inactivity penalties applied: %d / %d checked.", penalized, len(inactive))


# ─────────────────────────────────────────────────────────────
# E.1  Ежемесячные задачи
# ─────────────────────────────────────────────────────────────

async def run_monthly(period: str | None = None):
    """Расчёт зарплат всех партнёров за месяц."""
    from datetime import datetime
    from services.partner_compensation import calculate_partner_salary, recalculate_partner_rating
    from db.connection import get_conn, _cur

    if not period:
        # Предыдущий месяц
        today = datetime.now()
        if today.month == 1:
            period = f"{today.year - 1}-12"
        else:
            period = f"{today.year}-{today.month - 1:02d}"

    log.info("Monthly salary calculation for period: %s", period)

    # Получить всех специалистов
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT id, name FROM specialists ORDER BY id")
            specialists = await c.fetchall() or []

    calculated = 0
    for spec in specialists:
        sid = spec["id"]
        try:
            await recalculate_partner_rating(sid, period)
            result = await calculate_partner_salary(sid, period)
            if result.get("ok"):
                calculated += 1
                log.info("  %s (#%d): salary=%.0f rub (draft)",
                         spec.get("name", "?"), sid,
                         result.get("final_salary_rub", 0))
        except Exception:
            log.exception("Salary calculation failed for specialist #%d", sid)

    log.info("Monthly done: %d / %d specialists calculated.", calculated, len(specialists))


# ─────────────────────────────────────────────────────────────
# E.2  Закрытие месяца (draft → approved)
# ─────────────────────────────────────────────────────────────

async def run_monthly_close(period: str | None = None, director_id: int = 1):
    """
    Автоматическое закрытие месяца:
    1. Пересчёт всех партнёров (на случай запоздавших платежей)
    2. Перевод всех draft → approved
    """
    from datetime import datetime
    from services.partner_compensation import calculate_partner_salary, recalculate_partner_rating
    from db.partner_finance import set_salary_period_status, list_salary_periods

    if not period:
        today = datetime.now()
        if today.month == 1:
            period = f"{today.year - 1}-12"
        else:
            period = f"{today.year}-{today.month - 1:02d}"

    log.info("Closing month %s (director_id=%d)...", period, director_id)

    # 1) Пересчёт
    await run_monthly(period)

    # 2) Получить все draft периоды
    periods = await list_salary_periods(period_month=period, status="draft")
    approved = 0
    for p in periods:
        try:
            await set_salary_period_status(p["id"], "approved", approved_by=director_id)
            approved += 1
            log.info("  Approved salary period #%d (specialist #%d)", p["id"], p.get("specialist_id"))
        except Exception:
            log.exception("Failed to approve period #%d", p["id"])

    log.info("Month close done: %d periods approved.", approved)


# ─────────────────────────────────────────────────────────────
# Пересборка баланса
# ─────────────────────────────────────────────────────────────

async def run_rebuild(account_id: int):
    from services.ledger import rebuild_balance
    result = await rebuild_balance(account_id)
    log.info("Rebuilt balance for account #%d: %s", account_id, result)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "daily":
        asyncio.run(run_daily())
    elif cmd == "monthly":
        period = sys.argv[2] if len(sys.argv) > 2 else None
        asyncio.run(run_monthly(period))
    elif cmd == "monthly-close":
        period = sys.argv[2] if len(sys.argv) > 2 else None
        director_id = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        asyncio.run(run_monthly_close(period, director_id))
    elif cmd == "rebuild":
        if len(sys.argv) < 3:
            print("Usage: python run_loyalty_cron.py rebuild <account_id>")
            sys.exit(1)
        asyncio.run(run_rebuild(int(sys.argv[2])))
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
