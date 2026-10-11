"""
max_api.py — асинхронный HTTP-клиент для MAX Messenger Bot API.

Официальная документация: https://dev.max.ru/docs-api
Домен API: https://platform-api.max.ru
Авторизация: заголовок Authorization: <token>

Этот модуль оборачивает HTTP-запросы в удобные async-методы,
чтобы остальной код бота не работал с raw HTTP напрямую.
"""

import logging
import asyncio
import time
import json
from typing import Any, Optional
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api.max.ru"


@dataclass
class InlineButton:
    text: str
    callback_data: str = ""
    url: str = ""
    web_app: str = ""
    request_contact: bool = False
    message_text: str = ""

    def to_dict(self) -> dict:
        if self.request_contact:
            return {"type": "request_contact", "text": self.text}
        if self.url:
            return {"type": "link", "text": self.text, "url": self.url}
        if self.web_app:
            return {"type": "open_app", "text": self.text, "web_app": self.web_app}
        if self.message_text:
            return {"type": "message", "text": self.text, "input": self.message_text}
        return {"type": "callback", "text": self.text, "payload": self.callback_data}


@dataclass
class InlineKeyboard:
    rows: list[list[InlineButton]] = field(default_factory=list)

    def to_api(self) -> list[list[dict]]:
        return [[btn.to_dict() for btn in row] for row in self.rows]


@dataclass
class MaxUser:
    user_id: int
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    username: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "MaxUser":
        first = d.get("first_name", "")
        last = d.get("last_name", "")
        fallback_name = d.get("name", "")
        display_name = f"{first} {last}".strip() if first else fallback_name
        return cls(
            user_id=int(d.get("user_id", 0)),
            name=display_name,
            first_name=first,
            last_name=last or "",
            username=d.get("username", ""),
        )


@dataclass
class MaxMessage:
    message_id: str = ""
    text: str = ""
    chat_id: str = ""
    chat_type: str = ""
    sender: Optional[MaxUser] = None
    timestamp: int = 0
    contact_phone: str = ""
    attachments: Optional[list] = None

    @classmethod
    def _pick_phone(cls, raw: str) -> str:
        s = "".join(ch for ch in str(raw or "") if ch.isdigit() or ch == "+")
        digits = "".join(ch for ch in s if ch.isdigit())
        return str(raw) if len(digits) >= 10 else ""

    @classmethod
    def _deep_find_phone(cls, node: Any) -> str:
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if lk in {"phone", "phone_number", "mobile", "msisdn"}:
                    picked = cls._pick_phone(v)
                    if picked:
                        return picked
                found = cls._deep_find_phone(v)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = cls._deep_find_phone(item)
                if found:
                    return found
        return ""

    @classmethod
    def _extract_contact_phone(cls, root: dict) -> str:
        body = root.get("body") or {}
        attachments = body.get("attachments") or []
        if isinstance(attachments, list):
            for a in attachments:
                if not isinstance(a, dict):
                    continue
                payload = a.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                vcf = payload.get("vcf_info") or ""
                if vcf:
                    import re as _re
                    tel_match = _re.search(r"TEL[^:]*:([+\d\s\-()]+)", vcf)
                    if tel_match:
                        picked = cls._pick_phone(tel_match.group(1).strip())
                        if picked:
                            return picked
                contact = payload.get("contact")
                if isinstance(contact, dict):
                    phone = contact.get("phone") or contact.get("phone_number") or ""
                    picked = cls._pick_phone(phone)
                    if picked:
                        return picked
                phone = payload.get("phone") or ""
                picked = cls._pick_phone(phone)
                if picked:
                    return picked

        contact = body.get("contact") or root.get("contact") or {}
        if isinstance(contact, dict):
            phone = contact.get("phone") or contact.get("phone_number") or ""
            picked = cls._pick_phone(phone)
            if picked:
                return picked
        return cls._deep_find_phone(root)

    @classmethod
    def from_dict(cls, d: dict) -> "MaxMessage":
        sender_data = d.get("sender") or d.get("from")
        recipient = d.get("recipient") or {}
        body = d.get("body") or {}
        chat_id = recipient.get("chat_id", "")
        if not chat_id:
            chat_id = d.get("chat", {}).get("chat_id", "")
        raw_attachments = body.get("attachments") or []
        return cls(
            message_id=str(body.get("mid", "")),
            text=(body.get("text", "") or d.get("text", "") or body.get("caption", "") or ""),
            chat_id=str(chat_id) if chat_id else "",
            chat_type=recipient.get("chat_type", ""),
            sender=MaxUser.from_dict(sender_data) if sender_data else None,
            timestamp=int(d.get("timestamp", 0)),
            contact_phone=cls._extract_contact_phone(d),
            attachments=raw_attachments if raw_attachments else None,
        )


@dataclass
class MaxCallback:
    callback_id: str = ""
    payload: str = ""
    message: Optional[MaxMessage] = None
    user: Optional[MaxUser] = None

    @classmethod
    def from_dict(cls, d: dict) -> "MaxCallback":
        msg_data = d.get("message")
        user_data = d.get("user") or d.get("from") or d.get("sender")
        if not user_data:
            logger.warning("MaxCallback: не найден user ни под 'user', 'from', 'sender'. Keys: %s", list(d.keys()))
        return cls(
            callback_id=str(d.get("callback_id", "")),
            payload=d.get("payload", ""),
            message=MaxMessage.from_dict(msg_data) if msg_data else None,
            user=MaxUser.from_dict(user_data) if user_data else None,
        )


@dataclass
class MaxEvent:
    event_type: str
    raw: dict = field(default_factory=dict)
    message: Optional[MaxMessage] = None
    callback: Optional[MaxCallback] = None


class MaxBotAPI:
    def __init__(self, token: str, api_url: str = BASE_URL):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_event_id: int = 0

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"Authorization": self.token})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        timeout_total: float | None = None,
    ) -> dict:
        if not self._session or self._session.closed:
            raise RuntimeError("MaxBotAPI session not started — call .start() first")

        url = f"{self.api_url}{path}"
        timeout = aiohttp.ClientTimeout(total=timeout_total or 30)
        started = time.monotonic()
        body_preview = ""
        if json_body is not None:
            try:
                body_preview = json.dumps(json_body, ensure_ascii=False)[:1500]
            except Exception:
                body_preview = str(json_body)[:1500]

        logger.info("MAX API request: method=%s path=%s params=%s body=%s", method.upper(), path, params or {}, body_preview or "-")

        try:
            async with self._session.request(method.upper(), url, params=params, json=json_body, timeout=timeout) as resp:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                raw_text = ""
                try:
                    raw_text = await resp.text()
                    data = json.loads(raw_text) if raw_text else {}
                except Exception:
                    data = {}
                logger.info(
                    "MAX API response: method=%s path=%s status=%s elapsed_ms=%s ok=%s body=%s",
                    method.upper(), path, resp.status, elapsed_ms, 200 <= resp.status < 300,
                    ((json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)) if data else raw_text)[:2000],
                )
                if 200 <= resp.status < 300:
                    if isinstance(data, dict):
                        return {**data, "ok": True, "http_status": resp.status}
                    return {"ok": True, "http_status": resp.status, "result": data}

                if isinstance(data, dict):
                    result = {**data, "ok": False, "http_status": resp.status, "error": data.get("error") or data.get("message") or "http_error"}
                else:
                    result = {"ok": False, "http_status": resp.status, "error": "http_error", "result": data}
                if resp.status != 200:
                    logger.warning("MAX API %s %s → HTTP %d: %s", method, path, resp.status, result)
                return result
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("MAX API timeout: %s %s elapsed_ms=%s", method, path, elapsed_ms)
            return {"ok": False, "http_status": None, "error": "timeout", "method": method.upper(), "path": path}
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.exception("MAX API error %s %s elapsed_ms=%s: %s", method, path, elapsed_ms, e)
            return {"ok": False, "http_status": None, "error": str(e), "method": method.upper(), "path": path}

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    async def get_events(self, poll_time: int = 20, limit: int = 100) -> list[MaxEvent]:
        params: dict[str, str] = {"timeout": str(poll_time), "limit": str(limit), "types": "bot_started,message_created,message_callback"}
        if self._last_event_id:
            params["marker"] = str(self._last_event_id)
        data = await self._request("GET", "/updates", params=params, timeout_total=poll_time + 10)
        events: list[MaxEvent] = []
        if not data or not data.get("ok", False):
            return events
        marker = data.get("marker")
        if marker is not None:
            self._last_event_id = int(marker)
        raw_updates = data.get("updates", [])
        if raw_updates:
            logger.info("get_events: получено %d событий, marker=%s", len(raw_updates), marker)
            for i, ru in enumerate(raw_updates):
                ut = ru.get("update_type", "?")
                ts = ru.get("timestamp", "?")
                uid = ru.get("user_id") or (ru.get("user") or {}).get("user_id") or "?"
                mid = (ru.get("message") or {}).get("body", {}).get("mid", "")
                cbid = (ru.get("callback") or {}).get("callback_id", "")[:20] if ru.get("callback") else ""
                logger.info("  event[%d]: type=%s ts=%s user=%s mid=%s cbid=%s", i, ut, ts, uid, mid or "-", cbid or "-")
        for raw_event in raw_updates:
            ev = self._parse_event(raw_event)
            if ev:
                events.append(ev)
        return events

    def _parse_event(self, raw: dict) -> Optional[MaxEvent]:
        update_type = raw.get("update_type", "")
        logger.debug("_parse_event: type=%s, keys=%s", update_type, list(raw.keys()))

        if update_type == "message_created":
            raw_msg = raw.get("message", raw)
            msg = MaxMessage.from_dict(raw_msg)
            logger.info("← message_created: chat_id=%s, text=%s, sender=%s", msg.chat_id, (msg.text or "")[:50], msg.sender.user_id if msg.sender else None)
            body = raw_msg.get("body") or {}
            if body.get("attachments") or (not msg.text and not msg.contact_phone):
                logger.info("  raw body keys=%s, attachments=%s", list(body.keys()), body.get("attachments"))
            if msg.contact_phone:
                logger.info("  contact_phone extracted: %s", msg.contact_phone)
            return MaxEvent(event_type=update_type, raw=raw, message=msg)

        elif update_type == "message_callback":
            cb_data = dict(raw.get("callback") or raw)
            sibling_message = raw.get("message")
            if sibling_message and "message" not in cb_data:
                cb_data["message"] = sibling_message
            logger.info(
                "← message_callback: callback_id=%s, payload=%s, keys=%s, has_message=%s",
                cb_data.get("callback_id", "?"), cb_data.get("payload", "?"), list(cb_data.keys()), bool(cb_data.get("message")),
            )
            cb = MaxCallback.from_dict(cb_data)
            return MaxEvent(event_type=update_type, raw=raw, callback=cb)

        elif update_type == "bot_started":
            logger.info("← bot_started event: %s", raw)
            user_data = raw.get("user") or raw.get("from") or {}
            chat_id = raw.get("chat_id", "")
            start_payload = raw.get("start_payload") or raw.get("payload") or raw.get("start") or raw.get("start_param") or ""
            start_payload = str(start_payload).strip()
            start_text = "/start"
            if start_payload:
                start_text = f"/start {start_payload}"
            fake_msg = MaxMessage(text=start_text, chat_id=str(chat_id) if chat_id else str(user_data.get("user_id", "")), sender=MaxUser.from_dict(user_data) if user_data else None, timestamp=raw.get("timestamp", 0))
            return MaxEvent(event_type="message_created", raw=raw, message=fake_msg)

        else:
            logger.info("Unknown MAX event type: %s, keys=%s", update_type, list(raw.keys()))
            return MaxEvent(event_type=update_type, raw=raw)

    @staticmethod
    def _make_keyboard_attachment(keyboard: InlineKeyboard) -> dict:
        return {"type": "inline_keyboard", "payload": {"buttons": keyboard.to_api()}}

    @staticmethod
    def _normalize_parse_mode(parse_mode: Optional[str]) -> Optional[str]:
        if parse_mode is None:
            return None
        pm = str(parse_mode).strip().lower()
        if pm in {"markdown", "md"}:
            return "markdown"
        if pm == "html":
            return "html"
        return pm or None

    async def send_message(self, chat_id: str, text: str, *, keyboard: Optional[InlineKeyboard] = None, parse_mode: Optional[str] = None, notify: bool = True, disable_link_preview: Optional[bool] = None) -> dict:
        params: dict[str, str] = {"chat_id": str(chat_id)}
        body: dict[str, Any] = {"text": text}
        if keyboard:
            body["attachments"] = [self._make_keyboard_attachment(keyboard)]
        fmt = self._normalize_parse_mode(parse_mode)
        if fmt:
            body["format"] = fmt
        if not notify:
            body["notify"] = False
        if disable_link_preview is not None:
            params["disable_link_preview"] = str(disable_link_preview).lower()
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def send_message_to_user(self, user_id: int | str, text: str, *, keyboard: Optional[InlineKeyboard] = None, parse_mode: Optional[str] = None) -> dict:
        params: dict[str, str] = {"user_id": str(user_id)}
        body: dict[str, Any] = {"text": text}
        if keyboard:
            body["attachments"] = [self._make_keyboard_attachment(keyboard)]
        fmt = self._normalize_parse_mode(parse_mode)
        if fmt:
            body["format"] = fmt
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def edit_message(
        self,
        message_id: str,
        text: str,
        *,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
        clear_attachments: bool = False,
    ) -> dict:
        params = {"message_id": str(message_id)}
        body: dict[str, Any] = {"text": text}
        if clear_attachments:
            body["attachments"] = None
        elif keyboard:
            body["attachments"] = [self._make_keyboard_attachment(keyboard)]
        fmt = self._normalize_parse_mode(parse_mode)
        if fmt:
            body["format"] = fmt
        return await self._request("PUT", "/messages", params=params, json_body=body)

    async def delete_message(self, message_id: str) -> dict:
        return await self._request("DELETE", "/messages", params={"message_id": str(message_id)})

    async def answer_callback(self, callback_id: str, *, notification: str = "", message: Optional[dict] = None) -> dict:
        callback_id = str(callback_id or "").strip()
        if not callback_id:
            return {"ok": False, "http_status": None, "error": "empty_callback_id"}
        params = {"callback_id": callback_id}
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        if message:
            body["message"] = message
        if not body:
            body = {"message": None, "notification": ""}
        return await self._request("POST", "/answers", params=params, json_body=body)

    async def send_action(self, chat_id: str, action: str = "typing_on") -> dict:
        return await self._request("POST", f"/chats/{chat_id}/actions", json_body={"action": action})

    async def _get_upload_url(self, upload_type: str) -> dict:
        return await self._request("POST", "/uploads", params={"type": upload_type})

    async def _upload_file_to_url(self, upload_url: str, file_data: bytes, filename: str = "file") -> dict:
        if not self._session or self._session.closed:
            raise RuntimeError("MaxBotAPI session not started")
        form = aiohttp.FormData()
        form.add_field("data", file_data, filename=filename)
        try:
            async with self._session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                return await resp.json(content_type=None) or {}
        except Exception as e:
            logger.error("Upload error: %s", e)
            return {}

    async def download_file(self, file_url: str) -> bytes:
        if not self._session or self._session.closed:
            raise RuntimeError("MaxBotAPI session not started")

        file_url = str(file_url or "").strip()
        if not file_url:
            logger.error("download_file: empty file_url")
            return b""

        try:
            async with self._session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(
                        "download_file: HTTP %s from %s body=%s",
                        resp.status,
                        file_url,
                        text[:500],
                    )
                    return b""
                return await resp.read()
        except asyncio.TimeoutError:
            logger.warning("download_file timeout: %s", file_url)
            return b""
        except Exception as e:
            logger.exception("download_file error: %s", e)
            return b""

    @staticmethod
    def _extract_uploaded_token(upload_result: dict) -> str:
        if not isinstance(upload_result, dict):
            return ""
        direct = upload_result.get("token")
        if direct:
            return str(direct)
        for bucket_key in ("photos", "files"):
            bucket = upload_result.get(bucket_key)
            if not isinstance(bucket, dict):
                continue
            for _, meta in bucket.items():
                if isinstance(meta, dict) and meta.get("token"):
                    return str(meta["token"])
        return ""

    @staticmethod
    def _build_uploaded_payload(upload_result: dict, upload_type: str) -> dict:
        if not isinstance(upload_result, dict):
            return {}
        key_map = {"image": "photos", "file": "files", "video": "video", "audio": "audio"}
        bucket_key = key_map.get(upload_type)
        if bucket_key and isinstance(upload_result.get(bucket_key), dict):
            return {bucket_key: upload_result[bucket_key]}
        token = MaxBotAPI._extract_uploaded_token(upload_result)
        if token:
            return {"token": token}
        return {}

    async def send_photo(self, chat_id: str, photo_data: bytes, *, user_id: str | None = None, caption: str = "", filename: str = "photo.jpg", keyboard: Optional[InlineKeyboard] = None, parse_mode: Optional[str] = None) -> dict:
        upload_info = await self._get_upload_url("image")
        upload_url = upload_info.get("url")
        if not upload_url:
            logger.error("send_photo: не удалось получить upload URL")
            return {"error": "no upload url"}
        upload_result = await self._upload_file_to_url(upload_url, photo_data, filename)
        payload = self._build_uploaded_payload(upload_result, "image")
        if not payload:
            logger.error("send_photo: не получен payload после загрузки: %s", upload_result)
            return {"error": "no payload after upload"}
        params: dict[str, str] = {}
        if str(chat_id or "").strip():
            params["chat_id"] = str(chat_id)
        elif str(user_id or "").strip():
            params["user_id"] = str(user_id)
        else:
            logger.error("send_photo: не указан получатель (chat_id/user_id)")
            return {"error": "no recipient"}
        attachments: list[dict] = [{"type": "image", "payload": payload}]
        if keyboard:
            attachments.append(self._make_keyboard_attachment(keyboard))
        body: dict[str, Any] = {"attachments": attachments}
        if caption:
            body["text"] = caption
        fmt = self._normalize_parse_mode(parse_mode)
        if fmt:
            body["format"] = fmt
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def send_file(self, chat_id: str, file_data: bytes, *, user_id: str | None = None, caption: str = "", filename: str = "file", keyboard: Optional[InlineKeyboard] = None, parse_mode: Optional[str] = None) -> dict:
        upload_info = await self._get_upload_url("file")
        upload_url = upload_info.get("url")
        if not upload_url:
            logger.error("send_file: не удалось получить upload URL")
            return {"error": "no upload url"}
        upload_result = await self._upload_file_to_url(upload_url, file_data, filename)
        payload = self._build_uploaded_payload(upload_result, "file")
        if not payload:
            logger.error("send_file: не получен payload после загрузки: %s", upload_result)
            return {"error": "no payload after upload"}
        params: dict[str, str] = {}
        if str(chat_id or "").strip():
            params["chat_id"] = str(chat_id)
        elif str(user_id or "").strip():
            params["user_id"] = str(user_id)
        else:
            logger.error("send_file: не указан получатель (chat_id/user_id)")
            return {"error": "no recipient"}
        attachments: list[dict] = [{"type": "file", "payload": payload}]
        if keyboard:
            attachments.append(self._make_keyboard_attachment(keyboard))
        body: dict[str, Any] = {"attachments": attachments}
        if caption:
            body["text"] = caption
        fmt = self._normalize_parse_mode(parse_mode)
        if fmt:
            body["format"] = fmt
        return await self._request("POST", "/messages", params=params, json_body=body)
