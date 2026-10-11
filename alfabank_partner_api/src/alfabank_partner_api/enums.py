"""Enumerations used across the Alfa-Bank partner API."""
from __future__ import annotations

from enum import Enum


class ProductCode(str, Enum):
    """Banking product codes accepted by the API."""

    LP_RKO = "LP_RKO"
    LP_ACQ_TR = "LP_ACQ_TR"
    LP_AKASSA = "LP_AKASSA"
    LP_LOAN_BUS = "LP_LOAN_BUS"
    LP_OVER_ADV = "LP_OVER_ADV"
    LP_ACQ_E = "LP_ACQ_E"
    LP_PLEDGE = "LP_PLEDGE"
    LP_VKL = "LP_VKL"
    LP_POPSHOP = "LP_POPSHOP"


class LeadState(str, Enum):
    """Lead lifecycle state."""

    SENT = "SENT"
    DECLINED = "DECLINED"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class ProductState(str, Enum):
    """State of an individual product inside a lead."""

    NEW = "NEW"
    UNDEFINED = "UNDEFINED"
    IN_PROGRESS = "IN_PROGRESS"
    DECLINED = "DECLINED"
    COMPLETED = "COMPLETED"


class ScoringDecision(str, Enum):
    """Scoring engine final decision."""

    ACCEPT = "ACCEPT"
    DECLINE = "DECLINE"


class FilterDateBy(str, Enum):
    """Field to filter leads list by."""

    MODIFIED_AT = "modifiedAt"
    CREATED_AT = "createdAt"
