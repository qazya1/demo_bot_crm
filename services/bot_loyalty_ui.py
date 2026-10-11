"""
Хелперы формирования текста лояльности для ботов (TG + MAX).

Единый модуль — оба бота используют одни и те же тексты.
"""
from __future__ import annotations

from typing import Any, Dict, List


def format_loyalty_block(summary: Dict[str, Any], *, html: bool = True) -> str:
    """
    Формирует текстовый блок лояльности для профиля бота.
    html=True: <b>...</b> для TG ParseMode.HTML
    html=False: plain text для MAX
    """
    balance = summary.get("balance", 0)
    nearest_expiry = summary.get("nearest_expiry")
    nearest_expiry_amount = summary.get("nearest_expiry_amount", 0)

    b = "<b>" if html else ""
    be = "</b>" if html else ""

    lines = [
        f"{b}Баллы:{be} {balance}",
    ]

    if nearest_expiry and nearest_expiry_amount > 0:
        # nearest_expiry — ISO string
        try:
            from datetime import datetime
            exp = datetime.fromisoformat(nearest_expiry)
            days_left = (exp - datetime.utcnow()).days
            if days_left > 0:
                lines.append(
                    f"Сгорание: {nearest_expiry_amount} балл. через {days_left} дн. ({exp.strftime('%d.%m.%Y')})"
                )
        except Exception:
            pass

    return "\n".join(lines)


def format_loyalty_history(history: List[Dict[str, Any]], *, html: bool = True) -> str:
    """
    Формирует текст истории операций.
    """
    if not history:
        return "Операций пока нет."

    b = "<b>" if html else ""
    be = "</b>" if html else ""
    lines = [f"{b}История операций:{be}\n"]

    for tx in history[:20]:
        direction = tx.get("direction", "")
        amount = tx.get("amount", 0)
        event = tx.get("event_type", "")
        created = tx.get("created_at")

        sign = "+" if direction == "credit" else "-"
        icon = "🟢" if direction == "credit" else "🔴"
        event_display = _event_name(event)

        date_str = ""
        if created:
            try:
                from datetime import datetime
                if isinstance(created, str):
                    dt = datetime.fromisoformat(created)
                else:
                    dt = created
                date_str = dt.strftime("%d.%m")
            except Exception:
                date_str = ""

        line = f"{icon} {sign}{amount} — {event_display}"
        if date_str:
            line += f" ({date_str})"
        lines.append(line)

    return "\n".join(lines)


def format_referral_progress(progress: Dict[str, Any], *, html: bool = True) -> str:
    """Формирует текст прогресса реферальной программы."""
    scanned = progress.get("scanned", 0)
    threshold = progress.get("threshold", 10)
    remaining = progress.get("remaining_to_next", threshold)
    rewards = progress.get("rewards_earned", 0)

    b = "<b>" if html else ""
    be = "</b>" if html else ""

    lines = [
        f"{b}Реферальная программа:{be}",
        f"Приглашено: {scanned} из {threshold}",
        f"До награды: {remaining}",
    ]
    if rewards > 0:
        lines.append(f"Получено наград: {rewards}")

    # Progress bar
    filled = min(scanned, threshold)
    bar_len = 10
    filled_blocks = int(filled / max(threshold, 1) * bar_len)
    bar = "█" * filled_blocks + "░" * (bar_len - filled_blocks)
    lines.append(f"[{bar}] {filled}/{threshold}")

    return "\n".join(lines)


def format_checkout_bonus_info(info: Dict[str, Any], *, html: bool = True) -> str:
    """Текст для виджета списания баллов при оплате."""
    if not info.get("available"):
        return ""

    balance = info.get("balance", 0)
    usable = info.get("usable_points", 0)
    discount = info.get("usable_rub_discount", 0)
    rate = info.get("points_to_rub_rate", 0.1)
    max_pct = info.get("max_share_pct", 30)

    b = "<b>" if html else ""
    be = "</b>" if html else ""

    return (
        f"\n{b}Оплата баллами:{be}\n"
        f"Баланс: {balance} балл.\n"
        f"Можно списать: до {usable} балл. (скидка {discount:.0f} руб.)\n"
        f"Курс: 1 балл = {rate} руб. | Лимит: {max_pct:.0f}% от суммы"
    )


# ─── Internal ───

_TIER_NAMES = {
    "none": "Без уровня",
    "bronze": "Бронза",
    "silver": "Серебро",
    "gold": "Золото",
    "platinum": "Платина",
    "standard": "Стандарт",
    "senior": "Старший",
    "expert": "Эксперт",
    "master": "Мастер",
}

_EVENT_NAMES = {
    "purchase": "Покупка",
    "first_purchase": "Первая покупка",
    "profile_field": "Заполнение профиля",
    "referral_activation": "Приглашение друга",
    "referral_verification": "Верификация приглашённого",
    "qr_collection": "QR-коллекция",
    "reserve": "Резерв",
    "redeem": "Списание",
    "release": "Возврат резерва",
    "expiry": "Сгорание",
    "inactivity_penalty": "Неактивность",
    "consultation": "Консультация",
}


def _tier_name(tier: str) -> str:
    return _TIER_NAMES.get(tier, tier)


def _event_name(event: str) -> str:
    return _EVENT_NAMES.get(event, event.replace("_", " ").title())
