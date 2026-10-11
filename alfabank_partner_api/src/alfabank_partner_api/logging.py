"""Logging helpers — masking of API key and PII."""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("alfabank_partner_api")

_PHONE_RE = re.compile(r"^7\d{10}$")
_INN_RE = re.compile(r"^\d{10}$|^\d{12}$")
_OGRN_RE = re.compile(r"^\d{13}$|^\d{15}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PII_KEY_HINTS = (
    "inn",
    "ogrn",
    "kpp",
    "phone",
    "email",
    "fullname",
    "fullName",
    "full_name",
    "extraphonenumbers",
    "extraPhoneNumbers",
    "extra_phone_numbers",
    "address",
)


def mask_api_key(api_key: str) -> str:
    """Mask an API key keeping only first 4 and last 4 chars.

    Examples:
        >>> mask_api_key("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        'aaaa***aaaa'
    """
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:4]}***{api_key[-4:]}"


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        if _EMAIL_RE.match(value):
            local, _, domain = value.partition("@")
            visible = local[:1] if local else ""
            return f"{visible}***@{domain}"
        if _PHONE_RE.match(value):
            return f"{value[:2]}*******{value[-2:]}"
        if _INN_RE.match(value):
            return f"{value[:2]}***{value[-2:]}"
        if _OGRN_RE.match(value):
            return f"{value[:2]}***{value[-2:]}"
        return "***"
    return "***"


def _key_is_pii(key: str) -> bool:
    lowered = key.lower()
    return any(hint.lower() in lowered for hint in _PII_KEY_HINTS)


def mask_pii(data: Any) -> Any:
    """Return a copy of ``data`` with PII fields masked.

    Recursively walks dicts and lists. Keys that look like PII (``inn``, ``phone_number``,
    ``email``, ``full_name``, ``address``, ...) get their values masked. Plain string
    values matching a known PII shape (RU phone, INN, email) are masked too.
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for key, value in data.items():
            if _key_is_pii(str(key)):
                if isinstance(value, list):
                    masked[key] = [_mask_value(item) for item in value]
                elif isinstance(value, dict):
                    masked[key] = mask_pii(value)
                else:
                    masked[key] = _mask_value(value)
            elif isinstance(value, (dict, list)):
                masked[key] = mask_pii(value)
            else:
                masked[key] = value
        return masked
    if isinstance(data, list):
        return [mask_pii(item) for item in data]
    return data


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with the ``API-key`` value masked."""
    out = dict(headers)
    for header_name in list(out.keys()):
        if header_name.lower() == "api-key":
            out[header_name] = mask_api_key(out[header_name])
    return out


__all__ = ["logger", "mask_api_key", "mask_pii", "safe_headers"]
