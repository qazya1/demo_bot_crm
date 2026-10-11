"""Template filters for unified Russian CRM date display."""

from django import template

from bot_app.utils.ru_dates import format_ru_date, format_ru_datetime

register = template.Library()


@register.filter(name="ru_date")
def ru_date(value):
    """Return a human-readable Russian date: 16.05.2026."""
    return format_ru_date(value)


@register.filter(name="ru_datetime")
def ru_datetime(value):
    """Return a human-readable Russian date and time: 16.05.2026 14:30."""
    return format_ru_datetime(value)
