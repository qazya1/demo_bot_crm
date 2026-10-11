# alfabank-partner-api

Python SDK for the Alfa-Bank partner API
(<https://partner.alfabank.ru/public-api/v2>).

The package targets Python **3.10**, exposes both synchronous and asynchronous
clients, validates everything through Pydantic v2, retries network / 5xx errors
with `tenacity`, and never logs the API key or PII in the clear.

## Installation

The package is not published — install from the local source tree:

```bash
pip install -e ./alfabank_partner_api
```

## Quickstart

```python
from alfabank_partner_api import (
    AlfaBankClient, ClientConfig,
    LeadCreateRequest, OrganizationInfo, ContactInfo,
    RequestInfo, ProductItem, ProductCode,
)

cfg = ClientConfig(api_key="<your-key>")
with AlfaBankClient(config=cfg) as client:
    lead = client.leads.create(LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="ООО X", inn="7707083893"),
        contact_info=[ContactInfo(phone_number="79991234567")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    ))
    print(lead.id)
    info = client.leads.get(lead.id)
    print(info.state)
```

## Configuration

`ClientConfig` accepts arguments directly or via environment variables with the
`ALFABANK_` prefix:

| ENV variable | Field | Default |
|---|---|---|
| `ALFABANK_API_KEY` | `api_key` | — (required) |
| `ALFABANK_BASE_URL` | `base_url` | `https://partner.alfabank.ru/public-api/v2` |
| `ALFABANK_TIMEOUT` | `timeout` | `30.0` |
| `ALFABANK_MAX_RETRIES` | `max_retries` | `3` |
| `ALFABANK_RETRY_BACKOFF_BASE` | `retry_backoff_base` | `1.0` |
| `ALFABANK_USER_AGENT` | `user_agent` | `alfabank-partner-api-python/<version>` |
| `ALFABANK_VERIFY_SSL` | `verify_ssl` | `True` |
| `ALFABANK_PROXY` | `proxy` | `None` |
| `ALFABANK_MASK_PII_IN_LOGS` | `mask_pii_in_logs` | `True` |

```python
cfg = ClientConfig.from_env()  # reads ALFABANK_*; explicit overrides accepted
```

`DEMO_API_KEY = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"` is exported for tests.

## Error handling

All API errors derive from `AlfaBankAPIError`; exceptions carry `status_code`,
`error_code`, `title`, `detail` and the raw response body:

```python
from alfabank_partner_api import (
    AlfaBankClient, ClientConfig,
    AuthenticationError, RateLimitError, ServiceUnavailableError, ClientError,
)

try:
    client.leads.create(req)
except AuthenticationError:
    ...
except RateLimitError as e:
    sleep(60)  # back off
except ClientError as e:
    log.warning("business error: %s", e)
```

`checks.check_duplicate` is the only endpoint that does **not** raise on the
"duplicate" answer — it returns `DuplicateCheckResult(is_duplicate=True, error=...)`,
which makes batched checks ergonomic.

## Async usage

```python
import asyncio
from alfabank_partner_api import AlfaBankAsyncClient, ClientConfig

async def main() -> None:
    async with AlfaBankAsyncClient(config=ClientConfig(api_key="...")) as client:
        info = await client.leads.get("lead-id")
        print(info.state)

asyncio.run(main())
```

## Scoring + lead creation

For loan products you must run scoring first, wait for the final decision, and
only then submit the lead:

```python
from alfabank_partner_api import ScoringRequest, ScoringDecision, ProductCode

scoring = client.scoring
submit = scoring.submit(ScoringRequest(inn="7707083893"))
result = scoring.wait_for_result(submit.request_id, max_attempts=10, interval=30.0)
if result.decision == ScoringDecision.ACCEPT:
    client.leads.create(LeadCreateRequest(
        ...,
        product_info=[ProductItem(product_code=ProductCode.LP_LOAN_BUS)],
    ))
```

## Polling for status updates

Daily job using `filter_date_by="modifiedAt"`:

```python
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(days=1)
page = 1
while True:
    items = client.leads.list(
        page=page, per_page=100,
        from_date=since,
        filter_date_by="modifiedAt",
    )
    if not items:
        break
    for lead in items:
        save_to_db(lead)
    page += 1
```

## Parallel duplicate checks (up to 5 threads)

```python
from concurrent.futures import ThreadPoolExecutor

with AlfaBankClient(config=cfg) as client:
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(client.checks.check_duplicate, requests))
```

Async equivalent:

```python
import asyncio

sem = asyncio.Semaphore(5)

async def check(req):
    async with sem:
        return await client.checks.check_duplicate(req)

results = await asyncio.gather(*(check(r) for r in requests))
```

## Logging

The library is silent by default — it only attaches a `NullHandler` to its own
logger and never touches the root logger. To see what it does in your
application:

```python
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("alfabank_partner_api").setLevel(logging.DEBUG)
```

Or via `dictConfig`:

```python
import logging.config
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"stderr": {"class": "logging.StreamHandler"}},
    "loggers": {"alfabank_partner_api": {"handlers": ["stderr"], "level": "DEBUG"}},
})
```

* `INFO` — request method, path, status and latency.
* `DEBUG` — full URL, headers (`API-key` masked), request and response bodies
  (PII masked unless `mask_pii_in_logs=False`).
* `WARNING` — retries, unknown enum values from the upstream.
* `ERROR` — final failures after retries are exhausted.

The library never logs the API key in the clear (first/last 4 chars only).

## Dictionaries cache

Dictionary endpoints can be served from a local TTL cache (the bank documents a
"once a day" rate limit):

```python
from alfabank_partner_api import AlfaBankClient, DictionaryCache

cache = DictionaryCache(ttl=24 * 60 * 60)
with AlfaBankClient(config=cfg, dictionary_cache=cache) as client:
    cities = client.dictionaries.cities()
    cities_again = client.dictionaries.cities()  # served from the cache
```

Without an explicit `DictionaryCache` the client is fully stateless.

## Idempotency / `Repeated request error`

The bank rejects repeated `POST /leads` calls with the same payload within
~15 minutes. The SDK does **not** automatically deduplicate — implement a
deduplication key on your side, e.g. `inn + sorted(product_codes) + day`, and
guard the call there.

## Testing

Use the demo API key to exercise the demo environment without spending limits:

```python
from alfabank_partner_api import AlfaBankClient, ClientConfig, DEMO_API_KEY

with AlfaBankClient(config=ClientConfig(api_key=DEMO_API_KEY)) as client:
    ...
```

Integration tests under `tests/integration/` are skipped unless
`ALFABANK_RUN_INTEGRATION=1` is set.

Run the unit suite locally:

```bash
PYTHONPATH=src pytest tests/unit -q --cov=alfabank_partner_api
```

## Reference

* [API documentation v26 (29.01.2026)](https://partner.alfabank.ru/public-api/v2)
