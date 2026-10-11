from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class AbonentType(StrEnum):
    USER = "user"
    SPECIALIST = "specialist"


class InitiatorType(StrEnum):
    USER = "user"
    SPECIALIST = "specialist"
    SYSTEM = "system"


class CallDirection(StrEnum):
    USER_TO_SPECIALIST = "user_to_specialist"
    SPECIALIST_TO_USER = "specialist_to_user"


class CallStatus(StrEnum):
    CREATED = "created"
    VALIDATED = "validated"
    DIALING_FIRST_LEG = "dialing_first_leg"
    FIRST_LEG_ANSWERED = "first_leg_answered"
    DIALING_SECOND_LEG = "dialing_second_leg"
    BRIDGED = "bridged"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Abonent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: AbonentType
    phone_e164: str
    phone_verified: bool = True
    is_active: bool = True
    can_make_calls: bool = True
    can_receive_calls: bool = True


class CallSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    initiator_type: InitiatorType
    initiator_abonent_id: int
    caller_abonent_id: int
    callee_abonent_id: int
    direction: CallDirection
    channel: str = "pstn"
    status: CallStatus = CallStatus.CREATED
    asterisk_linkedid: Optional[str] = None
    asterisk_first_channel: Optional[str] = None
    asterisk_second_channel: Optional[str] = None
    started_at: Optional[datetime] = None
    bridged_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CallLog(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    call_session_id: int
    event_type: str
    event_payload: Optional[dict[str, Any]] = None
    event_time: Optional[datetime] = None
    raw_callerid: Optional[str] = None
    trunk_name: Optional[str] = None
    asterisk_channel: Optional[str] = None
    hangup_cause: Optional[str] = None
    duration_sec: Optional[int] = None


class StartCallRequest(BaseModel):
    initiator_abonent_id: int
    caller_abonent_id: int
    callee_abonent_id: int
    initiator_type: InitiatorType
    callerid: Optional[str] = Field(default=None, description="External caller ID allowed by provider")
