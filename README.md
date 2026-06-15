# ZendeskTokenManager — Generic Zendesk OAuth Credential Manager

A self-contained Python module for managing Zendesk OAuth tokens with automatic refresh. Drop it into any Python project that talks to the Zendesk API.

## Files

| File | Purpose |
|------|---------|
| `client.py` | The module — copy into your project |
| `first_run.py` | Interactive first-time OAuth setup wizard |
| `test_client.py` | Test suite (runs with or without pytest) |
| `watch_token.py` | Live token refresh monitor |

## Quick Start

```python
from client import ZendeskTokenManager

mgr = ZendeskTokenManager(
    subdomain="my-subdomain",
    oauth_token="abc123...",
    oauth_refresh_token="def456...",
    oauth_client_id="zd-transfer-migration",
    oauth_client_secret="your-client-secret",
)

headers = {"Authorization": f"Bearer {mgr.get_token()}"}
```

## First-Time Setup

```bash
python first_run.py
```

This walks you through the OAuth flow and saves credentials to `credentials.json`. Then:

```python
from client import load_credentials

mgr = load_credentials("credentials.json")
token = mgr.get_token()  # always fresh, auto-refreshing
```

## Migration: API Token → OAuth

Replace `email + api_token` Basic Auth with OAuth. See `documentation.md` for full migration guide with before/after code.

## Key Features

- **Auto-refresh** before token expiry (thread-safe)
- **Degradation** — returns current token if Zendesk is unreachable
- **Callbacks** — register persistence hooks via `on_refresh()`
- **Force refresh** — `force_refresh()` on 401 recovery
- **No framework dependencies** — only `requests`

## Requirements

- Python 3.8+
- `requests` (`pip install requests`)

## Tests

```bash
python -m pytest test_client.py -v
# or
python test_client.py
```

## Documentation

See `documentation.md` for full API reference, integration patterns, real-world scenarios (CI/CD, Docker, Lambda, multi-tenant), thread safety details, and error handling strategy.
