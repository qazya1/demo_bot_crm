import requests
import os
import time
import hashlib
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "inf.env")

USERNAME = os.getenv("VSE_OFD_USER", "").strip()
PASSWORD = os.getenv("VSE_OFD_PASSWORD", "").strip()
TOKEN = os.getenv("VSE_OFD_TOKEN", "").strip()

base_url = "https://www.vseofd24.ru/lk/api/v1/clients"


def _raise_ofd_error(r: requests.Response) -> None:
    """Единообразная ошибка внешнего API без падения на не-JSON ответах."""
    reason = "unknown error"
    try:
        payload = r.json()
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or payload.get("message") or payload)
        else:
            reason = str(payload)
    except (ValueError, json.JSONDecodeError):
        txt = (r.text or "").strip()
        reason = txt[:300] if txt else f"HTTP {r.status_code}"
    raise Exception(f"Ошибка подключения к vseofd24.ru: {reason}")

def form_headers(is_post=False):
    time_ = str(int(time.time()))
    if is_post:
        return {
                "X-Datetime": time_,
                "X-Sign": hashlib.md5((time_+TOKEN).encode('utf-8')).hexdigest(),
                "X-Username": USERNAME,
                "Content-Type": "application/json"
            }
    else:
        return {
                "X-Datetime": time_,
                "X-Sign": hashlib.md5((time_+TOKEN).encode('utf-8')).hexdigest(),
                "X-Username": USERNAME
            }

def get_profile_info():
    r = requests.get(base_url+"/profile", headers=form_headers())
    if r.status_code != 200:
        _raise_ofd_error(r)
    return r.json()

def get_price_list():
    r = requests.get(base_url+"/price", headers=form_headers())
    if r.status_code != 200:
        _raise_ofd_error(r)
    return r.json()

def reserve_codes(operator_id, type_id, count=1):
    json_ = {
        "operatorId":operator_id,
        "typeId":type_id,
        "count":count
    }
    r = requests.post(base_url+"/order", json=json_, headers=form_headers(True))
    if r.status_code != 200:
        _raise_ofd_error(r)
    return r.json()

def payment(order_id):
    json_ = {
        "orderId":order_id
    }
    r = requests.post(base_url+"/payment", json=json_, headers=form_headers(True))
    if r.status_code != 200:
        _raise_ofd_error(r)
    return r.json()

def get_history(*, date1=None, date2=None):
    if date1:
        date1 = date1.strftime("%Y/%m/%d")
    else:
        date1 = ""
    if date2:
        date2 = date2.strftime("%Y/%m/%d")
    else:
        date2 = ""
    r = requests.get(base_url+f"/history?date1={date1}&date2={date2}", headers=form_headers())
    if r.status_code != 200:
        _raise_ofd_error(r)
    return r.json()

_STUB_ORDERS: dict[str, dict] = {}

def reserve_codes_stub(operator_id, type_id, count=1):
    # формируем тестовый orderId
    oid = f"TEST-{int(time.time())}-{operator_id}-{type_id}"
    _STUB_ORDERS[oid] = {
        "operatorId": operator_id,
        "typeId": type_id,
        "count": int(count or 1),
        "created_at": datetime.utcnow().isoformat(),
    }
    return {
        "orderId": oid,
        "operatorId": operator_id,
        "typeId": type_id,
        "count": int(count or 1),
        "stub": True,
    }

def payment_stub(order_id):
    info = _STUB_ORDERS.get(order_id) or {"count": 1, "operatorId": "0", "typeId": "0"}
    cnt = int(info.get("count") or 1)
    op = str(info.get("operatorId") or "0")
    tp = str(info.get("typeId") or "0")

    # тестовые коды (уникальные)
    codes = [f"TEST-OFD-{op}-{tp}-{order_id[-6:]}-{i+1:02d}" for i in range(cnt)]

    return {
        "orderId": order_id,
        "status": "PAID",
        "codes": codes,
        "stub": True,
    }
