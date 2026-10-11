"""Unit tests for ClientConfig."""
from __future__ import annotations

import pytest

from alfabank_partner_api import (
    AlfaBankConfigurationError,
    ClientConfig,
    DEMO_API_KEY,
)


def test_required_api_key() -> None:
    with pytest.raises(Exception):
        ClientConfig()  # type: ignore[call-arg]


def test_base_url_strips_trailing_slash() -> None:
    cfg = ClientConfig(api_key=DEMO_API_KEY, base_url="https://x.example/")
    assert cfg.base_url == "https://x.example"


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFABANK_API_KEY", "abc-key")
    monkeypatch.setenv("ALFABANK_TIMEOUT", "12.5")
    monkeypatch.setenv("ALFABANK_MAX_RETRIES", "7")
    monkeypatch.setenv("ALFABANK_VERIFY_SSL", "false")
    cfg = ClientConfig.from_env()
    assert cfg.api_key == "abc-key"
    assert cfg.timeout == 12.5
    assert cfg.max_retries == 7
    assert cfg.verify_ssl is False


def test_from_env_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFABANK_API_KEY", raising=False)
    with pytest.raises(AlfaBankConfigurationError):
        ClientConfig.from_env()


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFABANK_API_KEY", "from-env")
    cfg = ClientConfig.from_env(api_key="from-arg")
    assert cfg.api_key == "from-arg"
