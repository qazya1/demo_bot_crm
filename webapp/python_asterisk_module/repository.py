from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from schemas import Abonent, CallLog, CallSession, CallStatus, InitiatorType


class TelephonyRepository(ABC):
    @abstractmethod
    def get_abonent(self, abonent_id: int) -> Optional[Abonent]:
        raise NotImplementedError

    @abstractmethod
    def create_call_session(
        self,
        *,
        initiator_type: InitiatorType,
        initiator_abonent_id: int,
        caller_abonent_id: int,
        callee_abonent_id: int,
        direction: str,
        channel: str,
        status: CallStatus,
        started_at,
    ) -> CallSession:
        raise NotImplementedError

    @abstractmethod
    def get_call_session(self, session_id: int) -> Optional[CallSession]:
        raise NotImplementedError

    @abstractmethod
    def update_call_session(self, session: CallSession) -> CallSession:
        raise NotImplementedError

    @abstractmethod
    def add_call_log(self, log: CallLog) -> CallLog:
        raise NotImplementedError
