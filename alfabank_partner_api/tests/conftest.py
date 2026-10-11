"""Shared pytest fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from alfabank_partner_api import AlfaBankClient, ClientConfig, DEMO_API_KEY
from alfabank_partner_api.transport import RetryPolicy

_FIXTURES = Path(__file__).parent / "fixtures" / "responses"


@pytest.fixture
def fixture_loader() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        path = _FIXTURES / name
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def fast_retry_policy() -> RetryPolicy:
    """Retry policy with no waiting — for tests."""
    return RetryPolicy(
        max_retries=2,
        backoff_base=0.0,
        max_500_attempts=2,
        five_hundred_interval=0.0,
    )


@pytest.fixture
def config() -> ClientConfig:
    return ClientConfig(
        api_key=DEMO_API_KEY,
        base_url="https://partner.alfabank.ru/public-api/v2",
        timeout=5.0,
        max_retries=2,
        retry_backoff_base=0.0,
    )


@pytest.fixture
def client(config: ClientConfig, fast_retry_policy: RetryPolicy) -> Iterator[AlfaBankClient]:
    with AlfaBankClient(config=config, retry_policy=fast_retry_policy) as c:
        yield c
