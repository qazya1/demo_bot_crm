"""Shared view normalization helpers."""


def _normalize_inn(value):
    """Нормализует ИНН: оставляет только цифры."""
    if value is None:
        return ''
    return ''.join(ch for ch in str(value) if ch.isdigit()).strip()


def _normalize_phone_digits(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits
