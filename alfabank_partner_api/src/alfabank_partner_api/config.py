"""Configuration object for the Alfa-Bank partner API client."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .exceptions import AlfaBankConfigurationError

DEFAULT_BASE_URL = "https://partner.alfabank.ru/public-api/v2"
DEMO_API_KEY = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PACKAGE_VERSION = "0.1.0"
DEFAULT_USER_AGENT = f"alfabank-partner-api-python/{PACKAGE_VERSION}"

_ENV_PREFIX = "ALFABANK_"


class ClientConfig(BaseModel):
    """Configuration for ``AlfaBankClient`` / ``AlfaBankAsyncClient``.

    Attributes:
        api_key: Value for the ``API-key`` HTTP header (required).
        base_url: Base URL of the API.
        timeout: HTTP request timeout, seconds.
        max_retries: Number of retries for retryable errors (5xx / network).
        retry_backoff_base: Base for exponential backoff, seconds.
        user_agent: Value for ``User-Agent`` header.
        verify_ssl: Whether to verify TLS certificates.
        proxy: Optional HTTP/HTTPS proxy URL.
        mask_pii_in_logs: Mask INN / phone / email values in DEBUG logs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str = Field(..., min_length=1, description="API-key header value")
    base_url: str = Field(default=DEFAULT_BASE_URL)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_base: float = Field(default=1.0, ge=0)
    user_agent: str = Field(default=DEFAULT_USER_AGENT)
    verify_ssl: bool = Field(default=True)
    proxy: str | None = Field(default=None)
    mask_pii_in_logs: bool = Field(default=True)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @classmethod
    def from_env(cls, **overrides: Any) -> "ClientConfig":
        """Build a config from environment variables with the ``ALFABANK_`` prefix.

        Boolean values accept ``1/0/true/false/yes/no`` (case-insensitive).
        Explicit ``overrides`` take precedence over environment variables.
        """
        env_data: dict[str, Any] = {}
        bool_fields = {"verify_ssl", "mask_pii_in_logs"}
        float_fields = {"timeout", "retry_backoff_base"}
        int_fields = {"max_retries"}

        for field_name in cls.model_fields:
            env_key = _ENV_PREFIX + field_name.upper()
            if env_key not in os.environ:
                continue
            raw = os.environ[env_key]
            if field_name in bool_fields:
                env_data[field_name] = raw.strip().lower() in {"1", "true", "yes", "on"}
            elif field_name in float_fields:
                env_data[field_name] = float(raw)
            elif field_name in int_fields:
                env_data[field_name] = int(raw)
            else:
                env_data[field_name] = raw

        env_data.update(overrides)
        if "api_key" not in env_data:
            raise AlfaBankConfigurationError(
                "api_key is required (set ALFABANK_API_KEY or pass api_key= explicitly)"
            )
        return cls(**env_data)
