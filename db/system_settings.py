from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.connection import get_conn, _cur


DEFAULT_SYSTEM_SETTINGS: list[dict[str, Any]] = [
    {
        "name": "EXTERNAL_OFD_MARKUP_PCT",
        "value_type": "number",
        "number_value": 740,
        "char_value": None,
        "description": "Глобальная наценка для внешних товаров ОФД",
    },
    {
        "name": "EXTERNAL_SERVICES_MARKUP_PCT",
        "value_type": "number",
        "number_value": 740,
        "char_value": None,
        "description": "Глобальная наценка для внешних сервисов",
    },
    {
        "name": "EXTERNAL_EDO_MARKUP_PCT",
        "value_type": "number",
        "number_value": 740,
        "char_value": None,
        "description": "Глобальная наценка для внешних ЭДО",
    },
    {
        "name": "LOYALTY_REFERRAL_ACTIVATION_POINTS",
        "value_type": "number",
        "number_value": 50,
        "char_value": None,
        "description": "Бонус рефереру за активацию приглашения",
    },
    {
        "name": "LOYALTY_REFERRAL_ACTIVATION_RATING_DELTA",
        "value_type": "number",
        "number_value": 2,
        "char_value": None,
        "description": "Изменение рейтинга за активацию приглашения",
    },
    {
        "name": "LOYALTY_REFERRAL_VERIFICATION_POINTS",
        "value_type": "number",
        "number_value": 100,
        "char_value": None,
        "description": "Бонус рефереру за верификацию приглашённого",
    },
    {
        "name": "LOYALTY_REFERRAL_VERIFICATION_RATING_DELTA",
        "value_type": "number",
        "number_value": 3,
        "char_value": None,
        "description": "Изменение рейтинга за верификацию приглашённого",
    },
    {
        "name": "LOYALTY_REFERRAL_L2_VERIFICATION_POINTS",
        "value_type": "number",
        "number_value": 0,
        "char_value": None,
        "description": "L2-бонус за верификацию (0 = отключено)",
    },
    {
        "name": "LOYALTY_REFERRAL_L2_VERIFICATION_RATING_DELTA",
        "value_type": "number",
        "number_value": 0,
        "char_value": None,
        "description": "L2-изменение рейтинга за верификацию",
    },
    {
        "name": "LOYALTY_QR_COLLECTION_THRESHOLD",
        "value_type": "number",
        "number_value": 10,
        "char_value": None,
        "description": "Порог QR-активаций для награды",
    },
    {
        "name": "LOYALTY_QR_COLLECTION_REWARD_POINTS",
        "value_type": "number",
        "number_value": 200,
        "char_value": None,
        "description": "Бонус за достижение порога QR-активаций",
    },
    {
        "name": "LOYALTY_QR_COLLECTION_RATING_DELTA",
        "value_type": "number",
        "number_value": 5,
        "char_value": None,
        "description": "Изменение рейтинга за достижение порога QR-активаций",
    },
    {
        "name": "LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT",
        "value_type": "number",
        "number_value": 0,
        "char_value": None,
        "description": "Реферальная надбавка партнёру, % от выручки приглашённых им клиентов",
    },
]


async def ensure_system_settings_schema() -> None:
    """Создаёт таблицу системных настроек и базовые записи, если их ещё нет."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    name VARCHAR(100) PRIMARY KEY,
                    value_type ENUM('number', 'text') NOT NULL,
                    char_value VARCHAR(1000) DEFAULT NULL,
                    number_value DECIMAL(12,4) DEFAULT NULL,
                    description VARCHAR(255) DEFAULT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_by BIGINT DEFAULT NULL,
                    CONSTRAINT fk_system_settings_updated_by
                        FOREIGN KEY (updated_by) REFERENCES specialists(id)
                        ON DELETE SET NULL,
                    INDEX idx_system_settings_value_type (value_type),
                    INDEX idx_system_settings_updated_by (updated_by)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            for item in DEFAULT_SYSTEM_SETTINGS:
                await c.execute(
                    """
                    INSERT INTO system_settings (name, value_type, char_value, number_value, description)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        value_type = VALUES(value_type),
                        description = VALUES(description)
                    """,
                    (
                        item["name"],
                        item["value_type"],
                        item["char_value"],
                        item["number_value"],
                        item["description"],
                    ),
                )
        await conn.commit()


async def get_system_setting(name: str) -> Optional[Dict[str, Any]]:
    key = (name or "").strip()
    if not key:
        return None
    await ensure_system_settings_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT name, value_type, char_value, number_value, description, updated_at, updated_by
                FROM system_settings
                WHERE name = %s
                LIMIT 1
                """,
                (key,),
            )
            return await c.fetchone()


async def get_system_settings() -> List[Dict[str, Any]]:
    await ensure_system_settings_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT name, value_type, char_value, number_value, description, updated_at, updated_by
                FROM system_settings
                ORDER BY name ASC
                """
            )
            return await c.fetchall() or []


async def get_system_setting_number(name: str, default: float | int | None = None) -> Optional[float]:
    row = await get_system_setting(name)
    if not row:
        return float(default) if default is not None else None

    value = row.get("number_value") if isinstance(row, dict) else None
    if value is None:
        return float(default) if default is not None else None

    try:
        return float(value)
    except Exception:
        return float(default) if default is not None else None
