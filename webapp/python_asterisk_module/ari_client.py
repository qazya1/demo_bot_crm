from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests
import websocket

logger = logging.getLogger(__name__)
MIN_TALK_SECONDS = float(os.getenv("ARI_MIN_TALK_SECONDS", "8"))
FIRST_LEG_GRACE_SECONDS = float(os.getenv("ARI_FIRST_LEG_GRACE_SECONDS", "1.5"))


@dataclass(slots=True)
class AriConfig:
    base_url: str
    ws_url: str
    username: str
    password: str
    app_name: str
    trunk_endpoint: str
    default_callerid: str
    request_timeout: int = 15
    reconnect_delay_sec: int = 2


class AriError(RuntimeError):
    pass


class AriClient:
    def __init__(self, config: AriConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.auth = (config.username, config.password)
        self.session.headers.update({"Content-Type": "application/json"})
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._event_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._stop_event = threading.Event()

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        response = self.session.request(
            method=method.upper(),
            url=self._url(path),
            params=params,
            json=json_body,
            timeout=self.config.request_timeout,
        )
        if not response.ok:
            raise AriError(
                f"ARI {method.upper()} {path} failed: {response.status_code} {response.text}"
            )
        if not response.text:
            return None
        ctype = response.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return response.json()
        try:
            return response.json()
        except Exception:
            return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs)

    def ping(self) -> Any:
        return self.get("asterisk/info")

    def add_event_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._event_handlers.append(handler)

    def connect_events(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return

        self._stop_event.clear()
        query = urlencode({"app": self.config.app_name, "api_key": f"{self.config.username}:{self.config.password}"})
        ws_url = f"{self.config.ws_url}?{query}"

        def on_open(_: websocket.WebSocketApp) -> None:
            logger.info("ARI websocket connected")

        def on_message(_: websocket.WebSocketApp, message: str) -> None:
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("Invalid ARI event: %s", message)
                return

            for handler in list(self._event_handlers):
                try:
                    handler(event)
                except Exception:
                    logger.exception("Unhandled exception in ARI event handler")

        def on_error(_: websocket.WebSocketApp, error: Any) -> None:
            logger.error("ARI websocket error: %s", error)

        def on_close(_: websocket.WebSocketApp, code: Any, msg: Any) -> None:
            logger.warning("ARI websocket closed: code=%s msg=%s", code, msg)

        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        def runner() -> None:
            while not self._stop_event.is_set():
                try:
                    self._ws.run_forever()
                except Exception:
                    logger.exception("ARI websocket crashed")
                if not self._stop_event.is_set():
                    time.sleep(self.config.reconnect_delay_sec)

        self._ws_thread = threading.Thread(target=runner, daemon=True)
        self._ws_thread.start()

    def disconnect_events(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                logger.exception("Failed to close ARI websocket")
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)

    def originate_channel(
        self,
        *,
        endpoint: str,
        channel_id: Optional[str] = None,
        app_args: Optional[str] = None,
        caller_id: Optional[str] = None,
        timeout: int = 45,
        variables: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "endpoint": endpoint,
            "app": self.config.app_name,
            "timeout": timeout,
        }
        if channel_id:
            params["channelId"] = channel_id
        if app_args:
            params["appArgs"] = app_args
        if caller_id:
            params["callerId"] = caller_id
        if variables:
            params["variables"] = variables
        return self.post("channels", params=params)

    def hangup_channel(self, channel_id: str) -> None:
        self.delete(f"channels/{channel_id}")

    def create_bridge(
        self,
        *,
        bridge_id: Optional[str] = None,
        bridge_type: str = "mixing",
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"type": bridge_type}
        if bridge_id:
            params["bridgeId"] = bridge_id
        if name:
            params["name"] = name
        return self.post("bridges", params=params)

    def destroy_bridge(self, bridge_id: str) -> None:
        self.delete(f"bridges/{bridge_id}")

    def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        self.post(f"bridges/{bridge_id}/addChannel", params={"channel": channel_id})


@dataclass(slots=True)
class AriCallRuntime:
    session_id: str
    first_leg_channel_id: str
    second_leg_channel_id: str
    first_leg_number: str
    second_leg_number: str
    caller_id: str
    bridge_id: Optional[str] = None
    first_leg_up: bool = False
    second_leg_up: bool = False
    first_leg_up_at: Optional[float] = None
    second_leg_up_at: Optional[float] = None
    first_leg_confirmed: bool = False
    first_leg_grace_started_at: Optional[float] = None
    first_leg_grace_timer: Optional[threading.Timer] = None
    second_leg_originated: bool = False
    bridged: bool = False
    bridged_at: Optional[float] = None
    finished: bool = False
    primary_failed_leg: Optional[str] = None
    primary_failed_reason: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class YurtaAriController:
    def __init__(
        self,
        ari: AriClient,
        *,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.ari = ari
        self.on_event = on_event
        self._lock = threading.RLock()
        self._sessions: dict[str, AriCallRuntime] = {}
        self.ari.add_event_handler(self._handle_event)

    def _emit(self, session_id: str, payload: dict[str, Any]) -> None:
        logger.info("ARI session=%s payload=%s", session_id, payload)
        if self.on_event:
            self.on_event(session_id, payload)

    def _endpoint(self, phone_number: str) -> str:
        return f"PJSIP/{phone_number}@{self.ari.config.trunk_endpoint}"

    def start_call(
        self,
        *,
        session_id: str,
        first_leg_number: str,
        second_leg_number: str,
        caller_id: Optional[str] = None,
    ) -> AriCallRuntime:
        caller_id = caller_id or self.ari.config.default_callerid
        if not caller_id:
            raise AriError("caller_id is required for originate")

        runtime = AriCallRuntime(
            session_id=session_id,
            first_leg_channel_id=f"first-{session_id}-{uuid.uuid4().hex[:8]}",
            second_leg_channel_id=f"second-{session_id}-{uuid.uuid4().hex[:8]}",
            first_leg_number=first_leg_number,
            second_leg_number=second_leg_number,
            caller_id=caller_id,
        )

        with self._lock:
            if session_id in self._sessions and not self._sessions[session_id].finished:
                raise AriError(f"session {session_id} already exists")
            self._sessions[session_id] = runtime

        self._emit(session_id, {"type": "session_created"})
        self._emit(session_id, {"type": "call_status", "status": "starting"})
        self._emit(session_id, {"type": "call_status", "status": "ringing_specialist"})
        self.ari.originate_channel(
            endpoint=self._endpoint(first_leg_number),
            channel_id=runtime.first_leg_channel_id,
            caller_id=caller_id,
            timeout=45,
            app_args=f"role=first_leg,session_id={session_id}",
            variables={"YURTA_SESSION_ID": session_id, "YURTA_ROLE": "first_leg"},
        )
        self._emit(session_id, {"type": "first_leg_originate_sent", "channel_id": runtime.first_leg_channel_id})
        return runtime

    def stop_call(self, session_id: str, reason: str = "manual_stop") -> None:
        with self._lock:
            runtime = self._sessions.get(session_id)
        if not runtime or runtime.finished:
            return
        self._cleanup(runtime, reason=reason)

    def _find_runtime_by_channel(self, channel_id: str) -> Optional[AriCallRuntime]:
        with self._lock:
            for runtime in self._sessions.values():
                if channel_id in (runtime.first_leg_channel_id, runtime.second_leg_channel_id):
                    return runtime
        return None

    def _handle_event(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return

        runtime = self._find_runtime_by_channel(channel_id)
        if not runtime:
            return

        etype = event.get("type")
        if etype == "StasisStart":
            self._emit(runtime.session_id, {
                "type": "stasis_start",
                "channel_id": channel_id,
                "channel_name": channel.get("name"),
                "state": channel.get("state"),
            })
            return

        if etype == "ChannelStateChange":
            self._on_channel_state_change(runtime, event)
            return

        if etype == "StasisEnd":
            leg = "specialist" if channel_id == runtime.first_leg_channel_id else "client"
            self._emit(runtime.session_id, {
                "type": "stasis_end",
                "channel_id": channel_id,
                "leg": leg,
            })
            self._on_leg_terminated(runtime, leg=leg, mapped_reason="not_accepted", source="stasis_end")
            return

        if etype == "ChannelDestroyed":
            leg = "specialist" if channel_id == runtime.first_leg_channel_id else "client"
            mapped_reason = self._map_hangup_reason(event.get("cause"), event.get("cause_txt"))
            self._emit(runtime.session_id, {
                "type": "channel_destroyed",
                "channel_id": channel_id,
                "leg": leg,
                "cause": event.get("cause"),
                "cause_txt": event.get("cause_txt"),
                "mapped_reason": mapped_reason,
            })
            self._on_leg_terminated(runtime, leg=leg, mapped_reason=mapped_reason, source="channel_destroyed")
            return

    def _on_leg_terminated(self, runtime: AriCallRuntime, *, leg: str, mapped_reason: str, source: str) -> None:
        if leg == "specialist":
            runtime.first_leg_up = False
        else:
            runtime.second_leg_up = False

        if runtime.finished:
            return

        final_reason = mapped_reason
        if (
            leg == "specialist"
            and not runtime.bridged
            and not runtime.second_leg_originated
            and runtime.first_leg_grace_started_at is not None
        ):
            elapsed = max(0.0, time.time() - runtime.first_leg_grace_started_at)
            if elapsed <= FIRST_LEG_GRACE_SECONDS + 0.25:
                final_reason = "not_accepted"
                logger.info(
                    "Specialist leg terminated during grace, locking reason=not_accepted session_id=%s elapsed=%.3f source=%s",
                    runtime.session_id,
                    elapsed,
                    source,
                )

        if runtime.bridged:
            self._cleanup(
                runtime,
                reason=f"bridged_leg_hangup:{leg}:{final_reason}:{source}",
                failed_leg=leg,
                failed_reason=final_reason,
            )
            return

        self._cleanup(
            runtime,
            reason=f"{leg}_{final_reason}:{source}",
            failed_leg=leg,
            failed_reason=final_reason,
        )

    def _on_channel_state_change(self, runtime: AriCallRuntime, event: dict[str, Any]) -> None:
        channel = event["channel"]
        channel_id = channel["id"]
        state = channel.get("state")
        self._emit(runtime.session_id, {"type": "channel_state", "channel_id": channel_id, "state": state})

        if channel_id == runtime.first_leg_channel_id and state == "Up" and not runtime.first_leg_up:
            runtime.first_leg_up = True
            runtime.first_leg_up_at = time.time()
            self._emit(runtime.session_id, {"type": "first_leg_answered", "channel_id": channel_id})
            self._emit(runtime.session_id, {"type": "call_status", "status": "answered_one_side", "answered_leg": "specialist"})
            self._start_first_leg_grace_period(runtime)
            return

        if channel_id == runtime.second_leg_channel_id and state == "Up" and not runtime.second_leg_up:
            runtime.second_leg_up = True
            runtime.second_leg_up_at = time.time()
            self._emit(runtime.session_id, {"type": "second_leg_answered", "channel_id": channel_id})
            self._bridge_if_ready(runtime)

    def _bridge_if_ready(self, runtime: AriCallRuntime) -> None:
        if runtime.bridged or not (runtime.first_leg_up and runtime.second_leg_up):
            return
        bridge_id = f"bridge-{runtime.session_id}"
        try:
            self.ari.create_bridge(bridge_id=bridge_id, bridge_type="mixing", name=f"session-{runtime.session_id}")
            self.ari.add_channel_to_bridge(bridge_id, runtime.first_leg_channel_id)
            self.ari.add_channel_to_bridge(bridge_id, runtime.second_leg_channel_id)
        except Exception:
            logger.exception("Bridge create/add failed for session_id=%s", runtime.session_id)
            self._cleanup(runtime, reason="bridge_create_failed", failed_reason="failed")
            return
        runtime.bridge_id = bridge_id
        runtime.bridged = True
        runtime.bridged_at = time.time()
        self._emit(runtime.session_id, {
            "type": "bridge_created",
            "bridge_id": bridge_id,
            "channels": [runtime.first_leg_channel_id, runtime.second_leg_channel_id],
        })
        self._emit(runtime.session_id, {"type": "call_status", "status": "connected"})

    def _start_first_leg_grace_period(self, runtime: AriCallRuntime) -> None:
        logger.info(
            "First leg moved to Up, starting grace period session_id=%s grace_sec=%s",
            runtime.session_id,
            FIRST_LEG_GRACE_SECONDS,
        )
        if runtime.first_leg_grace_timer:
            try:
                runtime.first_leg_grace_timer.cancel()
            except Exception:
                logger.debug("Failed to cancel previous first-leg grace timer session_id=%s", runtime.session_id)
        runtime.first_leg_grace_started_at = time.time()

        timer = threading.Timer(FIRST_LEG_GRACE_SECONDS, self._on_first_leg_grace_elapsed, args=(runtime.session_id,))
        timer.daemon = True
        runtime.first_leg_grace_timer = timer
        timer.start()

    def _on_first_leg_grace_elapsed(self, session_id: str) -> None:
        with self._lock:
            runtime = self._sessions.get(session_id)
            if not runtime:
                logger.info("First-leg grace elapsed but session already gone session_id=%s", session_id)
                return
            runtime.first_leg_grace_timer = None
            if runtime.finished:
                logger.info("First-leg grace elapsed but session already finished session_id=%s", session_id)
                return
            if not runtime.first_leg_up:
                logger.info("First-leg did not survive grace period, second leg will not start session_id=%s", session_id)
                return
            runtime.first_leg_confirmed = True
            logger.info("First-leg survived grace period, second leg can be started session_id=%s", session_id)

        self._originate_second_leg(session_id, trigger="first_leg_grace_passed")

    def _originate_second_leg(self, session_id: str, *, trigger: str) -> None:
        with self._lock:
            runtime = self._sessions.get(session_id)
            if not runtime:
                logger.info("Skip second-leg originate: session not found session_id=%s trigger=%s", session_id, trigger)
                return
            if runtime.finished:
                logger.info("Skip second-leg originate: session finished session_id=%s trigger=%s", session_id, trigger)
                return
            if not runtime.first_leg_up:
                logger.info("Skip second-leg originate: first leg is not alive session_id=%s trigger=%s", session_id, trigger)
                return
            if not runtime.first_leg_confirmed:
                logger.info("Skip second-leg originate: first leg is not confirmed session_id=%s trigger=%s", session_id, trigger)
                return
            if runtime.second_leg_originated or runtime.second_leg_up:
                logger.info("Skip second-leg originate: already started session_id=%s trigger=%s", session_id, trigger)
                return
            runtime.second_leg_originated = True
            second_leg_number = runtime.second_leg_number
            second_leg_channel_id = runtime.second_leg_channel_id
            caller_id = runtime.caller_id

        self._emit(session_id, {"type": "call_status", "status": "ringing_client"})
        logger.info("Starting second leg session_id=%s trigger=%s", session_id, trigger)
        try:
            self.ari.originate_channel(
                endpoint=self._endpoint(second_leg_number),
                channel_id=second_leg_channel_id,
                caller_id=caller_id,
                timeout=45,
                app_args=f"role=second_leg,session_id={session_id}",
                variables={"YURTA_SESSION_ID": session_id, "YURTA_ROLE": "second_leg"},
            )
        except Exception:
            logger.exception("Failed to originate second leg session_id=%s trigger=%s", session_id, trigger)
            with self._lock:
                runtime = self._sessions.get(session_id)
                if runtime:
                    runtime.second_leg_originated = False
            with self._lock:
                runtime = self._sessions.get(session_id)
            if runtime and not runtime.finished:
                self._cleanup(
                    runtime,
                    reason="second_leg_originate_failed",
                    failed_leg="client",
                    failed_reason="failed",
                )
            return
        self._emit(session_id, {"type": "second_leg_originate_sent", "channel_id": second_leg_channel_id})

    def _cleanup(
        self,
        runtime: AriCallRuntime,
        reason: str,
        *,
        failed_leg: Optional[str] = None,
        failed_reason: Optional[str] = None,
    ) -> None:
        if runtime.finished:
            return
        runtime.finished = True
        runtime.primary_failed_leg = failed_leg or runtime.primary_failed_leg
        runtime.primary_failed_reason = failed_reason or runtime.primary_failed_reason
        if runtime.first_leg_grace_timer:
            try:
                runtime.first_leg_grace_timer.cancel()
            except Exception:
                logger.debug("Failed to cancel first-leg grace timer session_id=%s", runtime.session_id)
            finally:
                runtime.first_leg_grace_timer = None

        for channel_id in [runtime.first_leg_channel_id, runtime.second_leg_channel_id]:
            try:
                self.ari.hangup_channel(channel_id)
            except Exception:
                logger.debug("Channel already closed: %s", channel_id)

        if runtime.bridge_id:
            try:
                self.ari.destroy_bridge(runtime.bridge_id)
            except Exception:
                logger.debug("Bridge already removed: %s", runtime.bridge_id)

        final_status, final_leg = self._classify_final_status(
            runtime=runtime,
            failed_leg=runtime.primary_failed_leg,
            failed_reason=runtime.primary_failed_reason,
        )
        bridge_duration_sec = 0.0
        if runtime.bridged and runtime.bridged_at:
            bridge_duration_sec = max(0.0, time.time() - runtime.bridged_at)
        self._emit(runtime.session_id, {
            "type": "call_status",
            "status": final_status,
            "failed_leg": final_leg,
            "reason": reason,
        })
        self._emit(runtime.session_id, {
            "type": "session_finished",
            "reason": reason,
            "bridge_id": runtime.bridge_id,
            "final_status": final_status,
            "failed_leg": final_leg,
            "failed_reason": runtime.primary_failed_reason,
            "bridge_duration_sec": round(bridge_duration_sec, 3),
            "min_talk_seconds": MIN_TALK_SECONDS,
        })
        with self._lock:
            self._sessions.pop(runtime.session_id, None)

    @staticmethod
    def _map_hangup_reason(cause: Any, cause_txt: Any) -> str:
        try:
            code = int(cause)
        except Exception:
            code = None
        txt = str(cause_txt or "").strip().lower()
        if code == 17 or "busy" in txt:
            return "busy"
        if code in (18, 19) or "no answer" in txt or "no_user_response" in txt:
            return "no_answer"
        if code == 21 or "reject" in txt or "declin" in txt:
            return "declined"
        if code in (16, 26) or "normal" in txt or "cancel" in txt:
            return "cancelled"
        return "failed"

    @staticmethod
    def _classify_final_status(
        *,
        runtime: AriCallRuntime,
        failed_leg: Optional[str],
        failed_reason: Optional[str],
    ) -> tuple[str, Optional[str]]:
        reason = str(failed_reason or "").strip().lower()
        leg = str(failed_leg or "").strip().lower() or None

        if runtime.bridged:
            bridge_duration = 0.0
            if runtime.bridged_at:
                bridge_duration = max(0.0, time.time() - runtime.bridged_at)
            if bridge_duration >= MIN_TALK_SECONDS:
                return "finished", leg
            if leg in {"client", "specialist"}:
                return "not_accepted", leg
            return "failed", leg

        if reason == "busy":
            return "busy", leg
        if reason == "no_answer":
            return "no_answer", leg
        if reason in {"declined", "cancelled"} and leg in {"client", "specialist"}:
            return "not_accepted", leg
        if leg in {"client", "specialist"}:
            return "not_accepted", leg
        if reason:
            return reason, leg
        return "failed", leg
