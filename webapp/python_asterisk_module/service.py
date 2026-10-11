from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ari_client import AriClient, AriConfig, YurtaAriController
from repository import TelephonyRepository
from schemas import (
    Abonent,
    AbonentType,
    CallDirection,
    CallLog,
    CallSession,
    CallStatus,
    InitiatorType,
)

logger = logging.getLogger(__name__)


class TelephonyValidationError(Exception):
    pass


class FastAPITelephonyService:
    def __init__(self, repository: TelephonyRepository, ari_controller: YurtaAriController) -> None:
        self.repository = repository
        self.ari_controller = ari_controller

    def validate_abonent_for_call(self, abonent: Abonent) -> None:
        if not abonent.is_active:
            raise TelephonyValidationError(f"Abonent {abonent.id} is inactive")
        if not abonent.phone_verified:
            raise TelephonyValidationError(f"Abonent {abonent.id} phone is not verified")
        if not abonent.phone_e164:
            raise TelephonyValidationError(f"Abonent {abonent.id} has empty phone")
        if not abonent.can_make_calls and not abonent.can_receive_calls:
            raise TelephonyValidationError(f"Abonent {abonent.id} cannot participate in calls")

    def validate_direction(self, caller: Abonent, callee: Abonent) -> CallDirection:
        if caller.type == AbonentType.USER and callee.type == AbonentType.SPECIALIST:
            return CallDirection.USER_TO_SPECIALIST
        if caller.type == AbonentType.SPECIALIST and callee.type == AbonentType.USER:
            return CallDirection.SPECIALIST_TO_USER
        raise TelephonyValidationError(
            f"Invalid call direction: caller={caller.type}, callee={callee.type}"
        )

    def create_call_session(
        self,
        *,
        initiator_abonent_id: int,
        caller_abonent_id: int,
        callee_abonent_id: int,
        initiator_type: InitiatorType,
    ) -> CallSession:
        initiator_abonent = self.repository.get_abonent(initiator_abonent_id)
        caller_abonent = self.repository.get_abonent(caller_abonent_id)
        callee_abonent = self.repository.get_abonent(callee_abonent_id)

        if initiator_abonent is None:
            raise TelephonyValidationError(f"Initiator abonent {initiator_abonent_id} not found")
        if caller_abonent is None:
            raise TelephonyValidationError(f"Caller abonent {caller_abonent_id} not found")
        if callee_abonent is None:
            raise TelephonyValidationError(f"Callee abonent {callee_abonent_id} not found")

        self.validate_abonent_for_call(caller_abonent)
        self.validate_abonent_for_call(callee_abonent)

        if not caller_abonent.can_make_calls:
            raise TelephonyValidationError(f"Caller {caller_abonent.id} cannot make calls")
        if not callee_abonent.can_receive_calls:
            raise TelephonyValidationError(f"Callee {callee_abonent.id} cannot receive calls")

        direction = self.validate_direction(caller_abonent, callee_abonent)
        session = self.repository.create_call_session(
            initiator_type=initiator_type,
            initiator_abonent_id=initiator_abonent.id,
            caller_abonent_id=caller_abonent.id,
            callee_abonent_id=callee_abonent.id,
            direction=direction,
            channel="pstn",
            status=CallStatus.VALIDATED,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.add_call_log(
            CallLog(
                call_session_id=session.id,
                event_type="session_validated",
                event_payload={
                    "initiator_abonent_id": initiator_abonent.id,
                    "caller_abonent_id": caller_abonent.id,
                    "callee_abonent_id": callee_abonent.id,
                    "direction": direction,
                },
            )
        )
        return session

    def _numbers_for_direction(self, session: CallSession) -> tuple[str, str]:
        caller = self.repository.get_abonent(session.caller_abonent_id)
        callee = self.repository.get_abonent(session.callee_abonent_id)
        if caller is None or callee is None:
            raise TelephonyValidationError("Session abonents not found")

        if session.direction == CallDirection.USER_TO_SPECIALIST:
            first_leg = caller.phone_e164
            second_leg = callee.phone_e164
        else:
            first_leg = callee.phone_e164
            second_leg = caller.phone_e164
        return first_leg.lstrip("+"), second_leg.lstrip("+")

    def start_call(
        self,
        *,
        initiator_abonent_id: int,
        caller_abonent_id: int,
        callee_abonent_id: int,
        initiator_type: InitiatorType,
        callerid: Optional[str] = None,
    ) -> CallSession:
        session = self.create_call_session(
            initiator_abonent_id=initiator_abonent_id,
            caller_abonent_id=caller_abonent_id,
            callee_abonent_id=callee_abonent_id,
            initiator_type=initiator_type,
        )

        first_leg_number, second_leg_number = self._numbers_for_direction(session)
        session.status = CallStatus.DIALING_FIRST_LEG
        session = self.repository.update_call_session(session)

        self.repository.add_call_log(
            CallLog(
                call_session_id=session.id,
                event_type="first_leg_originate_requested",
                event_payload={
                    "session_id": str(session.id),
                    "first_leg_number": first_leg_number,
                    "second_leg_number": second_leg_number,
                },
            )
        )

        self.ari_controller.start_call(
            session_id=str(session.id),
            first_leg_number=first_leg_number,
            second_leg_number=second_leg_number,
            caller_id=callerid,
        )
        return session

    def cancel_call(self, session_id: int, reason: str = "manual_cancel") -> CallSession:
        session = self.repository.get_call_session(session_id)
        if session is None:
            raise TelephonyValidationError(f"Session {session_id} not found")

        if session.status in {
            CallStatus.COMPLETED,
            CallStatus.CANCELLED,
            CallStatus.FAILED,
            CallStatus.EXPIRED,
        }:
            return session

        self.ari_controller.stop_call(str(session.id), reason=reason)
        session.status = CallStatus.CANCELLED
        session.failure_reason = reason
        session.ended_at = datetime.now(timezone.utc)
        session = self.repository.update_call_session(session)

        self.repository.add_call_log(
            CallLog(
                call_session_id=session.id,
                event_type="session_cancelled",
                event_payload={"reason": reason},
            )
        )
        return session

    def process_ari_event(self, session_id: str, payload: dict) -> None:
        session = self.repository.get_call_session(int(session_id))
        if session is None:
            logger.warning("CallSession %s not found for ARI event %s", session_id, payload)
            return

        event_type = payload.get("type")
        channel_id = payload.get("channel_id")

        if event_type == "first_leg_originate_sent":
            session.status = CallStatus.DIALING_FIRST_LEG
            session.asterisk_first_channel = channel_id
            session = self.repository.update_call_session(session)

        elif event_type == "first_leg_answered":
            session.status = CallStatus.FIRST_LEG_ANSWERED
            session = self.repository.update_call_session(session)

        elif event_type == "second_leg_originate_sent":
            session.status = CallStatus.DIALING_SECOND_LEG
            session.asterisk_second_channel = channel_id
            session = self.repository.update_call_session(session)

        elif event_type == "bridge_created":
            session.status = CallStatus.BRIDGED
            session.bridged_at = datetime.now(timezone.utc)
            session = self.repository.update_call_session(session)

        elif event_type == "session_finished":
            if session.status not in {CallStatus.CANCELLED, CallStatus.FAILED, CallStatus.EXPIRED}:
                session.status = CallStatus.COMPLETED if session.bridged_at else CallStatus.FAILED
            session.ended_at = datetime.now(timezone.utc)
            session.failure_reason = payload.get("reason")
            session = self.repository.update_call_session(session)

        elif event_type == "channel_destroyed":
            if session.status not in {CallStatus.COMPLETED, CallStatus.CANCELLED, CallStatus.FAILED, CallStatus.EXPIRED}:
                session.status = CallStatus.FAILED
                session.failure_reason = payload.get("cause_txt") or str(payload.get("cause"))
                session.ended_at = datetime.now(timezone.utc)
                session = self.repository.update_call_session(session)

        self.repository.add_call_log(
            CallLog(
                call_session_id=session.id,
                event_type=str(event_type),
                event_payload=payload,
                asterisk_channel=channel_id,
                hangup_cause=payload.get("cause_txt") or str(payload.get("cause") or ""),
            )
        )


def build_telephony_service(
    *,
    repository: TelephonyRepository,
    ari_config: AriConfig,
) -> FastAPITelephonyService:
    ari = AriClient(ari_config)
    service_holder: dict[str, FastAPITelephonyService] = {}

    def on_event(session_id: str, payload: dict) -> None:
        service_holder["service"].process_ari_event(session_id, payload)

    controller = YurtaAriController(ari, on_event=on_event)
    service = FastAPITelephonyService(repository, controller)
    service_holder["service"] = service

    ari.connect_events()
    ari.ping()
    return service
