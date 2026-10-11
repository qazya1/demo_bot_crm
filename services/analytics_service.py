"""
Сервис аналитики для CRM.
Собирает и агрегирует данные по пользователям/заказам и финансовой выручке.
"""
from typing import Any, Dict, List, Optional
from db.connection import get_conn, _cur, _maybe_await


async def get_platform_analytics() -> Dict[str, Any]:
    """
    Получить основную аналитику по платформам.
    
    Возвращает:
    - users_total: всего пользователей
    - users_max: пользователи MAX
    - users_tg: пользователи Telegram
    - orders_total: всего покупок
    - orders_max: покупки в MAX
    - orders_tg: покупки в Telegram
    - leader: какая платформа лидирует по покупкам ('max', 'tg', 'tie')
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Всего пользователей
            await c.execute("SELECT COUNT(*) AS cnt FROM users")
            row = await c.fetchone()
            users_total = int(row["cnt"]) if row else 0
            
            # Пользователи по платформам
            await c.execute("SELECT COUNT(*) AS cnt FROM users WHERE tg_user_id IS NOT NULL")
            row = await c.fetchone()
            users_tg = int(row["cnt"]) if row else 0
            
            await c.execute("SELECT COUNT(*) AS cnt FROM users WHERE max_user_id IS NOT NULL")
            row = await c.fetchone()
            users_max = int(row["cnt"]) if row else 0
            
            # Всего покупок
            await c.execute("SELECT COUNT(*) AS cnt FROM orders")
            row = await c.fetchone()
            orders_total = int(row["cnt"]) if row else 0
            
            # Покупки по платформам
            await c.execute("SELECT COUNT(*) AS cnt FROM orders WHERE source = 'telegram'")
            row = await c.fetchone()
            orders_tg = int(row["cnt"]) if row else 0
            
            await c.execute("SELECT COUNT(*) AS cnt FROM orders WHERE source = 'max'")
            row = await c.fetchone()
            orders_max = int(row["cnt"]) if row else 0
            
            # Определяем лидера
            if orders_max > orders_tg:
                leader = 'max'
            elif orders_tg > orders_max:
                leader = 'tg'
            else:
                leader = 'tie'
    
    return {
        'users_total': users_total,
        'users_max': users_max,
        'users_tg': users_tg,
        'orders_total': orders_total,
        'orders_max': orders_max,
        'orders_tg': orders_tg,
        'leader': leader,
    }


async def get_user_distribution() -> List[Dict[str, Any]]:
    """
    Получить распределение пользователей по платформам для pie chart.
    
    Возвращает список:
    - {label: 'MAX', value: count, color: '#...'}
    - {label: 'Telegram', value: count, color: '#...'}
    - {label: 'Обе платформы', value: count, color: '#...'}
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Только MAX
            await c.execute("""
                SELECT COUNT(*) AS cnt FROM users
                WHERE max_user_id IS NOT NULL AND tg_user_id IS NULL
            """)
            row = await c.fetchone()
            max_only = int(row["cnt"]) if row else 0
            
            # Только Telegram
            await c.execute("""
                SELECT COUNT(*) AS cnt FROM users
                WHERE tg_user_id IS NOT NULL AND max_user_id IS NULL
            """)
            row = await c.fetchone()
            tg_only = int(row["cnt"]) if row else 0
            
            # Обе платформы
            await c.execute("""
                SELECT COUNT(*) AS cnt FROM users
                WHERE max_user_id IS NOT NULL AND tg_user_id IS NOT NULL
            """)
            row = await c.fetchone()
            both = int(row["cnt"]) if row else 0
    
    return [
        {'label': 'MAX', 'value': max_only, 'color': '#f59e0b'},
        {'label': 'Telegram', 'value': tg_only, 'color': '#3b82f6'},
        {'label': 'Обе платформы', 'value': both, 'color': '#10b981'},
    ]


async def get_orders_by_platform() -> List[Dict[str, Any]]:
    """
    Получить данные о покупках по платформам для bar chart.
    
    Возвращает список:
    - {label: 'MAX', value: count, color: '#...'}
    - {label: 'Telegram', value: count, color: '#...'}
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT COUNT(*) AS cnt FROM orders WHERE source = 'max'")
            row = await c.fetchone()
            max_orders = int(row["cnt"]) if row else 0
            
            await c.execute("SELECT COUNT(*) AS cnt FROM orders WHERE source = 'telegram'")
            row = await c.fetchone()
            tg_orders = int(row["cnt"]) if row else 0
    
    return [
        {'label': 'MAX', 'value': max_orders, 'color': '#f59e0b'},
        {'label': 'Telegram', 'value': tg_orders, 'color': '#3b82f6'},
    ]


async def get_revenue_by_platform() -> Dict[str, Any]:
    """
    Получить выручку по платформам (оплаченные заказы).
    
    Возвращает:
    - revenue_max: выручка MAX
    - revenue_tg: выручка Telegram
    - revenue_total: общая выручка
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Общая выручка
            await c.execute("SELECT COALESCE(SUM(amount), 0) AS revenue FROM orders WHERE paid_at IS NOT NULL")
            row = await c.fetchone()
            revenue_total = float(row["revenue"]) if row and row["revenue"] else 0
            
            # Выручка MAX
            await c.execute("SELECT COALESCE(SUM(amount), 0) AS revenue FROM orders WHERE source = 'max' AND paid_at IS NOT NULL")
            row = await c.fetchone()
            revenue_max = float(row["revenue"]) if row and row["revenue"] else 0
            
            # Выручка Telegram
            await c.execute("SELECT COALESCE(SUM(amount), 0) AS revenue FROM orders WHERE source = 'telegram' AND paid_at IS NOT NULL")
            row = await c.fetchone()
            revenue_tg = float(row["revenue"]) if row and row["revenue"] else 0
    
    return {
        'revenue_max': revenue_max,
        'revenue_tg': revenue_tg,
        'revenue_total': revenue_total,
    }


def _build_revenue_period_clause(period_days: Optional[int]) -> tuple[str, tuple[Any, ...]]:
    """
    Построить SQL-условие фильтра периода по paid_at.
    """
    if not period_days or int(period_days) <= 0:
        return "", ()
    return " AND o.paid_at >= (NOW() - INTERVAL %s DAY)", (int(period_days),)


async def get_revenue_breakdown(
    *,
    period_days: Optional[int] = None,
    source_filter: str = "all",
    bots_filter: str = "all",
) -> Dict[str, Any]:
    """
    Финансовая логика выручки по источнику денег.

    Главные блоки:
    - bots_total: выручка из бот-логики
      - bots_chats: оплаты из чатов (orders.source='chat')
      - bots_shop: покупки из магазина (orders.source='telegram'/'max')
    - manual_total: ручные сделки (orders.source='manual')
    - revenue_total: итоговая выручка = bots_total + manual_total

    В выручку включаются только подтверждённые/зафиксированные оплаты:
    - paid_at IS NOT NULL
    - status IN ('CONFIRMED', 'COMPLETED', 'ADVANCE')
    """
    source_filter = (source_filter or "all").strip().lower()
    if source_filter not in ("all", "bots", "manual"):
        source_filter = "all"

    bots_filter = (bots_filter or "all").strip().lower()
    if bots_filter not in ("all", "chats", "shop"):
        bots_filter = "all"

    period_clause, period_params = _build_revenue_period_clause(period_days)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            base_where = f"""
                o.paid_at IS NOT NULL
                AND o.status IN ('CONFIRMED', 'COMPLETED', 'ADVANCE')
                {period_clause}
            """
            await c.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN o.source = 'chat' THEN o.amount ELSE 0 END), 0) AS bots_chats,
                    COALESCE(SUM(CASE WHEN o.source IN ('telegram', 'max') THEN o.amount ELSE 0 END), 0) AS bots_shop,
                    COALESCE(SUM(CASE WHEN o.source = 'manual' THEN o.amount ELSE 0 END), 0) AS manual_total
                FROM orders o
                WHERE {base_where}
                """,
                period_params,
            )
            row = await c.fetchone() or {}

    bots_chats = float(row.get("bots_chats") or 0)
    bots_shop = float(row.get("bots_shop") or 0)
    manual_total = float(row.get("manual_total") or 0)
    bots_total = bots_chats + bots_shop
    revenue_total = bots_total + manual_total

    filtered_revenue = revenue_total
    if source_filter == "bots":
        filtered_revenue = bots_total if bots_filter == "all" else (bots_chats if bots_filter == "chats" else bots_shop)
    elif source_filter == "manual":
        filtered_revenue = manual_total

    return {
        'period_days': period_days,
        'source_filter': source_filter,
        'bots_filter': bots_filter,
        'bots_total': bots_total,
        'bots_chats': bots_chats,
        'bots_shop': bots_shop,
        'manual_total': manual_total,
        'revenue_total': revenue_total,
        'filtered_revenue': filtered_revenue,
    }


async def get_analytics_summary() -> Dict[str, Any]:
    """
    Получить полную сводку аналитики для дашборда.
    """
    platform_data = await get_platform_analytics()
    user_distribution = await get_user_distribution()
    orders_by_platform = await get_orders_by_platform()
    revenue_data = await get_revenue_breakdown()
    
    return {
        **platform_data,
        'user_distribution': user_distribution,
        'orders_by_platform': orders_by_platform,
        'revenue': revenue_data,
    }
