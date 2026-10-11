"""Pydantic models for the Alfa-Bank partner API."""
from .checks import DuplicateCheckRequest, DuplicateCheckResult
from .common import ApiError, ErrorResponse, _BaseAPIModel
from .dictionaries import Branch, City, Region
from .leads import (
    ContactInfo,
    LeadCreateRequest,
    LeadCreateResponse,
    LeadInfo,
    LeadSummary,
    OrganizationInfo,
    ProductItem,
    ProductParams,
    ProductStatus,
    RequestInfo,
    ReserveAccount,
)
from .scoring import Limit, LimitMaxSum, ScoringRequest, ScoringResult, ScoringSubmitResponse

__all__ = [
    "ApiError",
    "Branch",
    "City",
    "ContactInfo",
    "DuplicateCheckRequest",
    "DuplicateCheckResult",
    "ErrorResponse",
    "LeadCreateRequest",
    "LeadCreateResponse",
    "LeadInfo",
    "LeadSummary",
    "Limit",
    "LimitMaxSum",
    "OrganizationInfo",
    "ProductItem",
    "ProductParams",
    "ProductStatus",
    "Region",
    "RequestInfo",
    "ReserveAccount",
    "ScoringRequest",
    "ScoringResult",
    "ScoringSubmitResponse",
    "_BaseAPIModel",
]
