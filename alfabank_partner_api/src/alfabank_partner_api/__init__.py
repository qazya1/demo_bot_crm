"""Public API of the Alfa-Bank partner SDK.

Top-level exports:
    * Clients: ``AlfaBankClient``, ``AlfaBankAsyncClient``
    * Configuration: ``ClientConfig``, ``DEMO_API_KEY``
    * Models: re-exported from ``alfabank_partner_api.models``
    * Enums: re-exported from ``alfabank_partner_api.enums``
    * Exceptions: re-exported from ``alfabank_partner_api.exceptions``
"""
from __future__ import annotations

import logging as _logging

from .async_client import AlfaBankAsyncClient
from .client import AlfaBankClient
from .config import DEFAULT_BASE_URL, DEMO_API_KEY, ClientConfig
from .enums import FilterDateBy, LeadState, ProductCode, ProductState, ScoringDecision
from .exceptions import (
    AlfaBankAPIError,
    AlfaBankConfigurationError,
    AlfaBankError,
    AlfaBankNetworkError,
    AuthenticationError,
    AuthorizationError,
    BlockedPartnerError,
    ChecksLimitError,
    ClientError,
    DuplicateRequestError,
    LeadsLimitError,
    NewCompanyError,
    RateLimitError,
    ScoringTimeoutError,
    ServerError,
    ServiceUnavailableError,
    UnacceptableCompanyError,
    UnacceptableCompanyError as DuplicateLeadError,  # alias
    ValidationError,
)
from .models import (
    ApiError,
    Branch,
    City,
    ContactInfo,
    DuplicateCheckRequest,
    DuplicateCheckResult,
    ErrorResponse,
    LeadCreateRequest,
    LeadCreateResponse,
    LeadInfo,
    LeadSummary,
    Limit,
    LimitMaxSum,
    OrganizationInfo,
    ProductItem,
    ProductParams,
    ProductStatus,
    Region,
    RequestInfo,
    ReserveAccount,
    ScoringRequest,
    ScoringResult,
    ScoringSubmitResponse,
)
from .resources import DictionaryCache
from .transport import RetryPolicy

# Avoid the "no handlers could be found" warning per stdlib library best practice.
_logging.getLogger("alfabank_partner_api").addHandler(_logging.NullHandler())

__all__ = [
    # Clients & config
    "AlfaBankAsyncClient",
    "AlfaBankClient",
    "ClientConfig",
    "DEFAULT_BASE_URL",
    "DEMO_API_KEY",
    "DictionaryCache",
    "RetryPolicy",
    # Enums
    "FilterDateBy",
    "LeadState",
    "ProductCode",
    "ProductState",
    "ScoringDecision",
    # Exceptions
    "AlfaBankAPIError",
    "AlfaBankConfigurationError",
    "AlfaBankError",
    "AlfaBankNetworkError",
    "AuthenticationError",
    "AuthorizationError",
    "BlockedPartnerError",
    "ChecksLimitError",
    "ClientError",
    "DuplicateLeadError",
    "DuplicateRequestError",
    "LeadsLimitError",
    "NewCompanyError",
    "RateLimitError",
    "ScoringTimeoutError",
    "ServerError",
    "ServiceUnavailableError",
    "UnacceptableCompanyError",
    "ValidationError",
    # Models
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
]

__version__ = "0.1.0"
