import os, json, hashlib, requests, time, secrets, logging
from datetime import datetime, timezone
from pathlib import Path

# === Конфиг и константы ===
API_BASE_V2      = os.getenv("TINKOFF_API_BASE", "https://securepay.tinkoff.ru/v2").rstrip("/")
API_BASE_CASHBOX = os.getenv("TINKOFF_CASHBOX_BASE", "https://securepay.tinkoff.ru").rstrip("/")

TERMINAL_KEY = os.getenv("TINKOFF_TERMINAL_KEY", "").strip()
PASSWORD     = os.getenv("TINKOFF_PASSWORD", "").strip()
MODE         = os.getenv("TINKOFF_MODE", "prod").strip()

TAXATION     = os.getenv("TINKOFF_TAXATION", "usn_income").strip()
FFD_VERSION  = os.getenv("TINKOFF_FFD", "1.2").strip()

# Токен кассы (Bearer) для SendClosingReceipt
API_TOKEN    = os.getenv("TINKOFF_CASHBOX_TOKEN", "").strip()

# Логи
LOG_DIR   = Path(__file__).resolve().parent / "var"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_INIT  = LOG_DIR / "tinkoff_last_init.json"
LOG_STATE = LOG_DIR / "tinkoff_last_state.json"
logger = logging.getLogger(__name__)


def build_tinkoff_order_id(prefix: str, entity_id: str | int) -> str:
    """
    Строит внешний OrderId для Tinkoff с высокой уникальностью.
    Формат: {prefix}_{entity_id}_{timestamp_ms}_{rand4hex}
    """
    ts_ms = int(time.time() * 1000)
    rnd = secrets.token_hex(2)
    return f"{prefix}_{entity_id}_{ts_ms}_{rnd}"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _post_raw(url: str, body: dict) -> dict:
    safe_body = {k: ("***" if str(k).lower() == "token" else v) for k, v in (body or {}).items()}
    logger.info("Tinkoff POST %s payload=%s", url, safe_body)
    r = requests.post(url, json=body, timeout=30)
    logger.info("Tinkoff response %s status=%s body=%s", url, r.status_code, r.text[:1000])
    r.raise_for_status()
    return r.json()


def _post_cashbox(path: str, payload: dict) -> dict:
    if not API_TOKEN:
        raise RuntimeError("Не задан TINKOFF_CASHBOX_TOKEN — нужен для /cashbox/*")
    url = API_BASE_CASHBOX + path
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    logger.info("Tinkoff cashbox POST %s payload=%s", url, payload)
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    logger.info("Tinkoff cashbox response %s status=%s body=%s", url, r.status_code, r.text[:1000])
    r.raise_for_status()
    return r.json()


def _val_to_str(v) -> str:
    """Сериализует значение в строку так же, как это делает T-Bank при построении подписи.
    Булевы значения: True → 'true', False → 'false' (как в JSON).
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _make_token(payload: dict) -> tuple[str, str]:
    """
    Формируем токен по официальной доке T-Bank:
    - берем ТОЛЬКО параметры корневого объекта (без Receipt/DATA и любых вложенных dict/list)
    - добавляем Password
    - сортируем по ключу
    - конкатенируем значения и считаем SHA-256
    """
    flat: dict[str, str] = {}

    for k, v in payload.items():
        if k == "Token":
            continue
        # Вложенные объекты (Receipt, DATA и любые dict/list) игнорируем
        if isinstance(v, (dict, list)):
            continue
        flat[k] = v

    flat["Password"] = PASSWORD

    canonical = "".join(_val_to_str(val) for _, val in sorted(flat.items(), key=lambda kv: kv[0]))
    return _sha256(canonical), canonical


def verify_notification_token(payload: dict) -> bool:
    """Проверяет подпись Token в webhook-уведомлении T-Bank."""
    token = str((payload or {}).get("Token") or (payload or {}).get("token") or "").strip()
    if not token:
        return False

    flat: dict[str, str] = {}
    for k, v in (payload or {}).items():
        if str(k).lower() == "token":
            continue
        if isinstance(v, (dict, list)):
            continue
        flat[str(k)] = v

    flat["Password"] = PASSWORD
    canonical = "".join(_val_to_str(val) for _, val in sorted(flat.items(), key=lambda kv: kv[0]))
    expected = _sha256(canonical)
    ok = (expected == token)
    if not ok:
        logger.warning(
            "Tinkoff notification token mismatch: payment_id=%s token_len=%s",
            (payload or {}).get("PaymentId") or (payload or {}).get("paymentId"),
            len(token),
        )
    return ok


# === Публичные хелперы чека ===
def build_receipt_payload(email: str | None, phone: str | None, items_kop: list[dict]) -> dict:
    items = []
    for it in items_kop:
        name = str(it.get("name") or it.get("title") or "Услуга")[:128]
        price = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        items.append(
            {
                "Name": name,
                "Price": price,
                "Quantity": qty,
                "Amount": price * qty,
                "Tax": "none",
                "PaymentMethod": "full_payment",
                "PaymentObject": "service",
                "MeasurementUnit": "piece",  # 👈 добавили
            }
        )
    receipt = {"Taxation": TAXATION, "Items": items}
    if email:
        receipt["Email"] = email
    if phone:
        receipt["Phone"] = phone
    return receipt


# === Основные методы API ===
def init_payment(*, order_id: str, amount_rub: int, description: str, receipt: dict | None = None, notification_url: str | None = None) -> dict:
    if not TERMINAL_KEY or not PASSWORD:
        raise RuntimeError("TINKOFF_TERMINAL_KEY/TINKOFF_PASSWORD не заданы в окружении")

    amount_kop = int(amount_rub) * 100

    # То, что реально отправляем в Init
    payload: dict[str, object] = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount_kop,
        "OrderId": order_id,
    }
    # Description отправляем и он ДОЛЖЕН участвовать в подписи
    if description:
        payload["Description"] = description[:140]

    # В Init НЕ передаём FfdVersion на корне!
    # FfdVersion = 1.2 нужно указывать ВНУТРИ Receipt, см. доку по фискализации

    if notification_url:
        payload["NotificationURL"] = str(notification_url).strip()

    if receipt:
        # аккуратно докинем FfdVersion внутрь Receipt, если он нужен
        rcpt = dict(receipt)
        if FFD_VERSION:
            rcpt["FfdVersion"] = FFD_VERSION  # теперь это поле вложенное
        payload["Receipt"] = rcpt

    # ======= ФОРМИРУЕМ ТОКЕН СТРОГО ПО ДОКЕ =======
    # В токен попадут: TerminalKey, Amount, OrderId, Description, Password
    tok, canon = _make_token(payload)
    req = {**payload, "Token": tok}

    url = API_BASE_V2 + "/Init"
    logger.info("Init payment request prepared: order_id=%s amount_rub=%s notification_url=%s", order_id, amount_rub, notification_url)
    resp = _post_raw(url, req)

    # Лог Init
    try:
        log = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            "mode": MODE,
            "api_base": API_BASE_V2,
            "endpoint": "/Init",
            "request_payload": {k: ("***" if k == "Token" else v) for k, v in req.items()},
            "canonical_string": canon,
            "response": resp,
        }
        LOG_INIT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write tinkoff init debug log")

    return resp


def get_payment_state(*, payment_id: str) -> dict:
    if not TERMINAL_KEY or not PASSWORD:
        raise RuntimeError("TINKOFF_TERMINAL_KEY/TINKOFF_PASSWORD не заданы в окружении")

    payload = {
        "TerminalKey": TERMINAL_KEY,
        "PaymentId": str(payment_id),
    }

    tok, canon = _make_token(payload)
    req = {**payload, "Token": tok}

    url = API_BASE_V2 + "/GetState"
    logger.info("GetState request prepared: payment_id=%s", payment_id)
    resp = _post_raw(url, req)

    # Лог GetState
    try:
        log = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            "mode": MODE,
            "api_base": API_BASE_V2,
            "endpoint": "/GetState",
            "request_payload": {k: ("***" if k == "Token" else v) for k, v in req.items()},
            "canonical_string": canon,
            "response": resp,
        }
        LOG_STATE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write tinkoff state debug log")

    return resp



def send_closing_receipt(payment_id: str, receipt: dict) -> dict:
    """
    /cashbox/SendClosingReceipt — отправляем чек после успешной оплаты.
    Тут используется Bearer TINKOFF_CASHBOX_TOKEN, подпись токеном НЕ нужна.
    """
    if not TERMINAL_KEY:
        raise RuntimeError("TINKOFF_TERMINAL_KEY не задан в окружении")

    req = {
        "TerminalKey": TERMINAL_KEY,
        "PaymentId": str(payment_id),
        "Receipt": receipt,
    }
    if FFD_VERSION:
        req["FfdVersion"] = FFD_VERSION

    return _post_cashbox("/cashbox/SendClosingReceipt", req)
