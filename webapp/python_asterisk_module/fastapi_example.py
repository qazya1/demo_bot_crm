from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException

from ari_client import AriConfig
from repository import TelephonyRepository
from schemas import Abonent, CallDirection, CallLog, CallSession, CallStatus, InitiatorType, StartCallRequest
from service import FastAPITelephonyService, TelephonyValidationError, build_telephony_service


class InMemoryTelephonyRepository(TelephonyRepository):
    def __init__(self) -> None:
        self.abonents: dict[int, Abonent] = {}
        self.sessions: dict[int, CallSession] = {}
        self.logs: list[CallLog] = []
        self._session_id = 1000

    def get_abonent(self, abonent_id: int) -> Optional[Abonent]:
        return self.abonents.get(abonent_id)

    def create_call_session(self, **kwargs) -> CallSession:
        self._session_id += 1
        session = CallSession(id=self._session_id, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), **kwargs)
        self.sessions[session.id] = session
        return session

    def get_call_session(self, session_id: int) -> Optional[CallSession]:
        return self.sessions.get(session_id)

    def update_call_session(self, session: CallSession) -> CallSession:
        session.updated_at = datetime.now(timezone.utc)
        self.sessions[session.id] = session
        return session

    def add_call_log(self, log: CallLog) -> CallLog:
        log.id = len(self.logs) + 1
        log.event_time = datetime.now(timezone.utc)
        self.logs.append(log)
        return log


repository = InMemoryTelephonyRepository()
repository.abonents[1] = Abonent(id=1, type="user", phone_e164="+79000000001")
repository.abonents[2] = Abonent(id=2, type="specialist", phone_e164="+79000000002")


@asynccontextmanager
def lifespan(app: FastAPI):
    ari_cfg = AriConfig(
        base_url=os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088/ari"),
        ws_url=os.getenv("ARI_WS_URL", "ws://127.0.0.1:8088/ari/events"),
        username=os.getenv("ARI_USERNAME", "ari_user"),
        password=os.getenv("ARI_PASSWORD", ""),
        app_name=os.getenv("ARI_APP_NAME", "bot-crm-call-control"),
        trunk_endpoint=os.getenv("ARI_TRUNK_ENDPOINT", "example-endpoint"),
        default_callerid=os.getenv("ARI_DEFAULT_CALLERID", ""),
    )
    app.state.telephony = build_telephony_service(repository=repository, ari_config=ari_cfg)
    try:
        yield
    finally:
        app.state.telephony.ari_controller.ari.disconnect_events()


app = FastAPI(lifespan=lifespan)


def get_telephony() -> FastAPITelephonyService:
    return app.state.telephony


@app.post("/telephony/calls/start")
def start_call(payload: StartCallRequest, telephony: FastAPITelephonyService = Depends(get_telephony)):
    try:
        session = telephony.start_call(
            initiator_abonent_id=payload.initiator_abonent_id,
            caller_abonent_id=payload.caller_abonent_id,
            callee_abonent_id=payload.callee_abonent_id,
            initiator_type=payload.initiator_type,
            callerid=payload.callerid,
        )
    except TelephonyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.model_dump()


@app.post("/telephony/calls/{session_id}/cancel")
def cancel_call(session_id: int, telephony: FastAPITelephonyService = Depends(get_telephony)):
    try:
        session = telephony.cancel_call(session_id, reason="cancel_from_api")
    except TelephonyValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.model_dump()
