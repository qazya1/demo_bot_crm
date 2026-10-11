"""Helpers for human-readable Russian CRM date formats."""

from datetime import date, datetime

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

EMPTY_DATE_DISPLAY = "—"
DATE_FORMAT = "%d.%m.%Y"
DATETIME_FORMAT = "%d.%m.%Y %H:%M"


def _parse_iso_value(value):
    """Best-effort parsing for ISO date/datetime strings."""
    if not isinstance(value, str):
        return value

    value = value.strip()
    if not value:
        return None

    parsed_datetime = parse_datetime(value)
    if parsed_datetime is not None:
        return parsed_datetime

    parsed_date = parse_date(value)
    if parsed_date is not None:
        return parsed_date

    return value


def _to_local_datetime(value):
    if isinstance(value, datetime) and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def format_ru_date(value, fallback=EMPTY_DATE_DISPLAY):
    """Format date/datetime/ISO string as DD.MM.YYYY."""
    if value in (None, ""):
        return fallback

    value = _parse_iso_value(value)
    if value in (None, ""):
        return fallback

    value = _to_local_datetime(value)
    if isinstance(value, datetime):
        return value.strftime(DATE_FORMAT)
    if isinstance(value, date):
        return value.strftime(DATE_FORMAT)

    return value


def format_ru_datetime(value, fallback=EMPTY_DATE_DISPLAY):
    """Format date/datetime/ISO string as DD.MM.YYYY HH:MM."""
    if value in (None, ""):
        return fallback

    value = _parse_iso_value(value)
    if value in (None, ""):
        return fallback

    value = _to_local_datetime(value)
    if isinstance(value, datetime):
        return value.strftime(DATETIME_FORMAT)
    if isinstance(value, date):
        return value.strftime(DATE_FORMAT)

    return value
