"""Common base model and error response schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class _BaseAPIModel(BaseModel):
    """Base model with camelCase aliasing and ``populate_by_name`` enabled."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=_to_camel,
        extra="ignore",
        str_strip_whitespace=True,
    )

    def to_api_dict(self) -> dict[str, Any]:
        """Serialise to a dict suitable for sending to the API.

        Uses camelCase aliases and drops ``None`` values.
        """
        return self.model_dump(by_alias=True, exclude_none=True)


class ApiError(_BaseAPIModel):
    """Single error item returned by the API."""

    status: int = Field(..., description="HTTP status code")
    code: str = Field(..., description="Machine-readable error code")
    title: str = Field(..., description="Short human-readable description")
    detail: str | None = Field(default=None, description="Detailed message")


class ErrorResponse(_BaseAPIModel):
    """Top-level error envelope: ``{"errors": [...]}``."""

    errors: list[ApiError] = Field(default_factory=list)
