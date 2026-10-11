"""Models for dictionaries (section 6 of the API documentation)."""
from __future__ import annotations

from pydantic import Field

from .common import _BaseAPIModel


class City(_BaseAPIModel):
    """City record from ``GET /dictionaries?code=cities``."""

    code: str
    name: str
    fias_id: str | None = Field(default=None)
    region_code: str | None = Field(default=None)


class Region(_BaseAPIModel):
    """Region record from ``GET /dictionaries?code=regions``."""

    code: str
    name: str
    fias_id: str | None = Field(default=None)


class Branch(_BaseAPIModel):
    """Branch record from ``GET /branches/{fiasId}``."""

    branch_id: str
    name: str
    address: str | None = Field(default=None)
