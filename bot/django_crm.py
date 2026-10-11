# django_crm.py
"""
Django CRM HTTP API integration module.

Replaces AmoCRM integration with Django CRM backend.
Provides reusable functions for:
- Sale notifications
- Application form submissions
- Extra verification requests
"""

import os
import logging
import json
from datetime import datetime, date, time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURATION (from environment variables)
# ══════════════════════════════════════════════════════════════════════

DJANGO_CRM_SALE_URL = os.getenv(
    "DJANGO_CRM_SALE_URL",
    "http://localhost:8000/api/sales/"
)

DJANGO_CRM_APPLICATION_URL = os.getenv(
    "DJANGO_CRM_APPLICATION_URL",
    "http://localhost:8000/api/applications/"
)

DJANGO_CRM_VERIFICATION_URL = os.getenv(
    "DJANGO_CRM_VERIFICATION_URL",
    "http://localhost:8000/api/verifications/"
)

DJANGO_CRM_TOKEN = os.getenv("DJANGO_CRM_TOKEN", "")

# Set to "1" to enable CRM integration; defaults to disabled if not configured
DJANGO_CRM_ENABLED = os.getenv("DJANGO_CRM_ENABLED", "0").strip() == "1"

# Request timeout in seconds
DJANGO_CRM_TIMEOUT = int(os.getenv("DJANGO_CRM_TIMEOUT", "10"))


# ══════════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def _build_headers() -> dict:
    """Build HTTP headers for Django CRM API requests."""
    headers = {"Content-Type": "application/json"}
    if DJANGO_CRM_TOKEN:
        headers["Authorization"] = f"Bearer {DJANGO_CRM_TOKEN}"
    return headers


def _json_serializable(obj):
    """Recursively convert datetime objects to ISO format strings."""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_json_serializable(item) for item in obj]
    return obj


async def _post_to_crm(url: str, payload: dict) -> None:
    """
    Send HTTP POST request to Django CRM API.

    Args:
        url: API endpoint URL
        payload: JSON payload to send

    Raises:
        Exception: If request fails or returns non-2xx status
    """
    if not DJANGO_CRM_ENABLED:
        logger.debug("Django CRM disabled (DJANGO_CRM_ENABLED != 1), skipping POST to %s", url)
        return

    headers = _build_headers()
    payload = _json_serializable(payload)
    
    timeout = aiohttp.ClientTimeout(total=DJANGO_CRM_TIMEOUT)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        ) as resp:
            # Follow 3xx redirects with POST (aiohttp downgrades 301/302 to GET by default)
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", "")
                if redirect_url:
                    async with session.post(
                        redirect_url,
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                    ) as resp2:
                        if resp2.status not in (200, 201):
                            text = await resp2.text()
                            raise Exception(
                                f"Django CRM returned {resp2.status}: {text[:500]}"
                            )
                    logger.info(
                        "Successfully sent %s to Django CRM: %s",
                        payload.get("type", "data"),
                        redirect_url,
                    )
                    return
            if resp.status not in (200, 201):
                text = await resp.text()
                raise Exception(
                    f"Django CRM returned {resp.status}: {text[:500]}"
                )
            logger.info(
                "Successfully sent %s to Django CRM: %s",
                payload.get("type", "data"),
                url,
            )


# ══════════════════════════════════════════════════════════════════════
#  PUBLIC API FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

async def push_sale_to_crm(
    order_id: str,
    total_rub: int,
    cart: list[dict],
    code_value: Optional[str] = None,
    *,
    source: str = "telegram",
    user_data: Optional[dict] = None,
    order_data: Optional[dict] = None,
    items: Optional[list[dict]] = None,
    codes: Optional[dict] = None,
    detailed_codes: Optional[list[dict]] = None,
) -> None:
    """
    Push sale notification to Django CRM.
    
    Args:
        order_id: Order ID
        total_rub: Total amount in rubles
        cart: Shopping cart items
        code_value: Optional single code value (legacy)
        source: Source system ("telegram" or "max")
        user_data: User profile data (first_name, last_name, phone, email, inn, city, etc.)
        order_data: Order details (amount, status, created_at, buyer info)
        items: Order items with code_type/equipment_id
        codes: Issued codes by type (ofd_first, ofd_platform, ofd_other, service, edo)
        detailed_codes: Detailed code information from database
    """
    # Build user_data from available sources
    if user_data is None:
        user_data = {}
    
    # Build order_data
    if order_data is None:
        order_data = {
            "amount": total_rub,
            "status": "CONFIRMED",
        }
    
    # Build items list
    if items is None:
        items = [
            {
                "name": item.get("name") or item.get("title"),
                "price": item.get("price"),
                "qty": item.get("qty", 1),
            }
            for item in (cart or [])
        ]
    
    # Build codes dict
    if codes is None:
        codes = {
            "ofd_first": [],
            "ofd_platform": [],
            "ofd_other": [],
            "service": [],
            "edo": [],
        }
        # If single code_value provided (legacy), add to ofd_first
        if code_value:
            codes["ofd_first"].append(code_value)
    
    payload = {
        "source": source,
        "type": "sale",
        "order_id": order_id,
        "order_data": order_data,
        "user_data": user_data,
        "items": items,
        "codes": codes,
        "detailed_codes": detailed_codes or [],
    }
    
    await _post_to_crm(DJANGO_CRM_SALE_URL, payload)


async def push_application_to_crm(
    form_data: dict,
    *,
    source: str = "telegram",
    user_data: Optional[dict] = None,
) -> None:
    """
    Push application form submission to Django CRM.
    
    Args:
        form_data: Application form fields (tp_name, tp_addr, activity, etc.)
        source: Source system ("telegram" or "max")
        user_data: User profile data (first_name, last_name, phone, email, inn, city, etc.)
    """
    if user_data is None:
        user_data = {}
    
    payload = {
        "source": source,
        "type": "application",
        "form_data": form_data,
        "user_data": user_data,
    }
    
    await _post_to_crm(DJANGO_CRM_APPLICATION_URL, payload)


async def push_verification_to_crm(
    user_data: dict,
    *,
    source: str = "telegram",
) -> None:
    """
    Push extra verification request to Django CRM.
    
    Args:
        user_data: User profile data (first_name, last_name, phone, email, inn, city, etc.)
        source: Source system ("telegram" or "max")
    """
    payload = {
        "source": source,
        "type": "verification",
        "user_data": user_data,
    }
    
    await _post_to_crm(DJANGO_CRM_VERIFICATION_URL, payload)
