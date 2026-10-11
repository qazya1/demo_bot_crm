from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CallHandle:
    session_id: str
    _done: threading.Event = field(default_factory=threading.Event)
    final_event: dict[str, Any] | None = None

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def mark_done(self) -> None:
        self._done.set()


class AsteriskClient:
    """Compatibility wrapper expected by webapp.asterisk_bridge.

    The bridge looks for a factory named ``AsteriskClient`` with methods
    ``call`` and ``wait_call_end``. This adapter wires those expectations to
    the ARI controller implementation in this package.
    """

    def __init__(self) -> None:
        from .ari_client import AriClient, AriConfig, YurtaAriController

        cfg = AriConfig(
            base_url=os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088/ari"),
            ws_url=os.getenv("ARI_WS_URL", "ws://127.0.0.1:8088/ari/events"),
            username=os.getenv("ARI_USERNAME", ""),
            password=os.getenv("ARI_PASSWORD", ""),
            app_name=os.getenv("ARI_APP_NAME", "bot-crm-call-control"),
            trunk_endpoint=os.getenv("ARI_TRUNK_ENDPOINT", "example-endpoint"),
            default_callerid=os.getenv("ARI_DEFAULT_CALLERID", ""),
        )
        self._ari: Any = AriClient(cfg)
        self._lock = threading.RLock()
        self._handles: dict[str, CallHandle] = {}
        self._callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._controller: Any = YurtaAriController(self._ari, on_event=self._on_event)
        self._ari.connect_events()

    def _on_event(self, session_id: str, payload: dict) -> None:
        callback = None
        event_type = payload.get("type")
        with self._lock:
            handle = self._handles.get(session_id)
            callback = self._callbacks.get(session_id)
        if callback:
            try:
                callback(payload)
            except Exception:
                logger.exception("Call callback failed for session_id=%s payload=%s", session_id, payload)
        if event_type != "session_finished":
            return
        if handle:
            handle.final_event = dict(payload)
            logger.info("Asterisk session finished: session_id=%s", session_id)
            handle.mark_done()

    def call(
        self,
        specialist_phone: str,
        client_phone: str,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> CallHandle:
        session_id = uuid.uuid4().hex[:16]
        handle = CallHandle(session_id=session_id)
        with self._lock:
            self._handles[session_id] = handle
            if on_event:
                self._callbacks[session_id] = on_event

        logger.info("Starting ARI call session_id=%s specialist=%s client=%s", session_id, specialist_phone, client_phone)
        try:
            runtime = self._controller.start_call(
                session_id=session_id,
                first_leg_number=specialist_phone,
                second_leg_number=client_phone,
            )
            logger.debug(
                "ARI call initiated session_id=%s first_leg_channel=%s second_leg_channel=%s",
                session_id,
                runtime.first_leg_channel_id,
                runtime.second_leg_channel_id,
            )
            return handle
        except Exception:
            with self._lock:
                self._handles.pop(session_id, None)
                self._callbacks.pop(session_id, None)
            logger.exception("Failed to start ARI call session_id=%s", session_id)
            raise

    def wait_call_end(self, call_result: Optional[CallHandle] = None, timeout: Optional[float] = None) -> bool:
        if call_result is None:
            return True
        try:
            return call_result.wait(timeout)
        finally:
            with self._lock:
                self._handles.pop(call_result.session_id, None)
                self._callbacks.pop(call_result.session_id, None)


_default_client: Optional[AsteriskClient] = None
_default_client_lock = threading.Lock()


def _get_default_client() -> AsteriskClient:
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = AsteriskClient()
        return _default_client


def call(
    specialist_phone: str,
    client_phone: str,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> CallHandle:
    return _get_default_client().call(specialist_phone, client_phone, on_event=on_event)


def wait_call_end(call_result: Optional[CallHandle] = None, timeout: Optional[float] = None) -> bool:
    return _get_default_client().wait_call_end(call_result, timeout)
