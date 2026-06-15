# ZendeskTokenManager — Generic Zendesk OAuth Credential Manager

## Overview

`ZendeskTokenManager` is a self-contained, single-file helper for managing
Zendesk OAuth tokens. Drop it into **any** Python project that talks to the
Zendesk API. It handles automatic token refresh so your application never
breaks mid-operation due to an expired token.

### Dependencies

- Python 3.8+
- `requests` (`pip install requests`)

No project-specific config, no .env coupling, no framework dependency.

---

## Files in this folder

| File | Purpose |
|------|---------|
| `client.py` | The module — copy this into your project |
| `first_run.py` | Interactive first-time OAuth setup |
| `documentation.md` | This file |

---

## Quick Start

```python
from client import ZendeskTokenManager, TokenRefreshError, TokenNotSetError

# First time — provide tokens + client credentials from your OAuth flow
mgr = ZendeskTokenManager(
    subdomain="my-subdomain",
    oauth_token="abc123...",
    oauth_refresh_token="def456...",
    oauth_client_id="zd-transfer-migration",
    oauth_client_secret="your-client-secret",
)

# Always returns a fresh token (auto-refreshes before expiry)
headers = {"Authorization": f"Bearer {mgr.get_token()}"}
resp = requests.get(
    "https://my-subdomain.zendesk.com/api/v2/users/me.json",
    headers=headers,
)
```

---

## First-Time Setup

### Option A: Use `first_run.py` (Recommended)

The included `first_run.py` script walks you through the entire OAuth flow
interactively and saves credentials to a JSON file:

```bash
python first_run.py
```

It will:
1. Prompt for subdomain, client ID, and client secret
2. Open the Zendesk authorization URL in your browser
3. Ask you to paste the redirect URL and validate the OAuth `state`
4. Exchange the code for tokens
5. Verify the token with a live API call (`users/me.json`)
6. Save credentials to `credentials.json` (permissions 0600)

After it completes, load the saved credentials anywhere:

```python
from client import load_credentials

mgr = load_credentials("credentials.json")
token = mgr.get_token()  # always fresh, auto-refreshing
```

### Option B: Manual OAuth flow

Run the project's `get_oauth_token.py`:

```bash
python get_oauth_token.py --role target \
    --subdomain my-subdomain \
    --client-id zd-transfer-migration \
    --secret <client-secret>
```

Then wire it up:

```python
from dotenv import dotenv_values
from client import ZendeskTokenManager

cfg = dotenv_values("config/target.env")
mgr = ZendeskTokenManager(
    subdomain=cfg["ZENDESK_SUBDOMAIN"],
    oauth_token=cfg["ZENDESK_OAUTH_TOKEN"],
    oauth_refresh_token=cfg.get("ZENDESK_OAUTH_REFRESH_TOKEN"),
    oauth_client_id=cfg.get("ZENDESK_CLIENT_ID"),
    oauth_client_secret=cfg.get("ZENDESK_CLIENT_SECRET"),
)
```

### Option C: Programmatic (CI/CD, automation)

Set environment variables and construct the manager directly:

```python
import os
from client import ZendeskTokenManager

mgr = ZendeskTokenManager(
    subdomain=os.environ["ZD_SUBDOMAIN"],
    oauth_token=os.environ["ZD_OAUTH_TOKEN"],
    oauth_refresh_token=os.environ.get("ZD_OAUTH_REFRESH_TOKEN"),
    oauth_client_id=os.environ["ZD_CLIENT_ID"],
    oauth_client_secret=os.environ["ZD_CLIENT_SECRET"],
)
```

---

## Migration Guide: API Token → OAuth

If your code currently uses Zendesk API tokens (Basic Auth), switching
to OAuth eliminates manual token rotation and adds auto-refresh.

### Before (API Token)

```python
class ZendeskClient:
    def __init__(self, subdomain, email, api_token):
        self.auth = (f"{email}/token", api_token)
        self.base = f"https://{subdomain}.zendesk.com/api/v2"

    def get(self, path):
        resp = requests.get(
            f"{self.base}/{path}",
            auth=self.auth,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


client = ZendeskClient("acme", "admin@acme.com", "ABC123")
```

**Problems:** Token never expires but if leaked, it has unlimited lifetime.
No auto-rotation. Revocation requires manual replacement.

### After (OAuth + TokenManager)

```python
from client import ZendeskTokenManager


class ZendeskClient:
    def __init__(self, subdomain, token_mgr):
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.mgr = token_mgr

    def _headers(self):
        return {"Authorization": f"Bearer {self.mgr.get_token()}"}

    def _request(self, method, path, **kwargs):
        url = f"{self.base}/{path}"
        resp = requests.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            self.mgr.force_refresh()
            resp = requests.request(method, url, headers=self._headers(), **kwargs)
        resp.raise_for_status()
        return resp.json()

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, data):
        return self._request("POST", path, json=data)


# Set up once
mgr = ZendeskTokenManager(
    subdomain="acme",
    oauth_token="abc...",
    oauth_refresh_token="def...",
    oauth_client_id="my-client",
    oauth_client_secret="sec-ret",
)

client = ZendeskClient("acme", mgr)
data = client.get("users/me.json")
```

**Benefits:**
- Auto-refresh before expiry (no more 401 surprises)
- `force_refresh()` for 401 recovery
- Thread-safe, lock-protected refresh
- Callbacks to persist new tokens anywhere

### Step-by-Step Migration

1. **Create an OAuth client** in Zendesk Admin → Apps → OAuth Clients
2. **Run `first_run.py`** to obtain tokens and save to `credentials.json`
3. **Replace `email + api_token`** auth with `oauth_token` in your client constructor
4. **Add `force_refresh()` retry** on 401 responses
5. **Register a persistence callback** with `on_refresh()` if you want tokens saved

---

## API Reference

### Constructor

```python
ZendeskTokenManager(
    subdomain: str,
    oauth_token: Optional[str] = None,
    oauth_refresh_token: Optional[str] = None,
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    ttl: Optional[int] = None,
    acquired_at: Optional[float] = None,
)
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `subdomain` | Yes | — | Zendesk subdomain slug, hostname, or full Zendesk URL (e.g. `"acme"` or `"https://acme.zendesk.com"`) |
| `oauth_token` | No* | `None` | Initial Bearer access token |
| `oauth_refresh_token` | No | `None` | Token to obtain new access tokens |
| `oauth_client_id` | No | `None` | OAuth client identifier (needed for refresh) |
| `oauth_client_secret` | No | `None` | OAuth client secret (needed for refresh) |
| `ttl` | No | `3600` | Expected access token lifetime in seconds |
| `acquired_at` | No | `None` | Unix timestamp when the token was originally obtained |

*Required before calling `get_token()` — provide at construction or via
`set_token()` later.*

### Methods

#### `get_token() -> str`

Returns a valid access token. Auto-refreshes if the current token has been
held for >=80% of its TTL.

- **Raises** `TokenNotSetError` if no token has ever been provided.
- **Thread-safe.** If multiple threads call concurrently at the refresh
  threshold, only one refreshes; others get the still-valid token.
- **Degradation.** If the refresh endpoint is unreachable, the existing
  token is returned. The application never crashes from a network blip
  during refresh.

#### `force_refresh() -> str`

Immediately refresh the access token, bypassing the age check.

- **Returns** the new token on success.
- **Returns** the current token (without raising) if no refresh token
  is configured.
- **Raises** `TokenRefreshError` if the refresh attempt fails permanently
  (e.g., revoked refresh token, missing client credentials).
- **Raises** `TokenNotSetError` if no token has ever been provided.

#### `set_token(oauth_token: str, oauth_refresh_token: Optional[str] = None) -> None`

Set or update the stored access token (and optionally refresh token).
Does NOT call Zendesk — only replaces in-memory values.

- **Raises** `ValueError` if `oauth_token` is empty.

#### `on_refresh(callback: Callable[[str, Optional[str]], None]) -> None`

Register a callback invoked after every successful token refresh.

- The callback receives `(new_access_token, new_refresh_token)`.
  `new_refresh_token` is `None` if Zendesk didn't rotate it.
- Multiple callbacks are supported. A failing callback does not
  prevent others from being notified.

### Properties

| Property | Returns | Description |
|----------|---------|-------------|
| `has_refresh` | `bool` | True if a refresh token is configured |
| `age` | `float` | Seconds since the current token was obtained |

### Functions

#### `load_credentials(path: str = "credentials.json") -> ZendeskTokenManager`

Load credentials saved by `first_run.py` and return a configured manager.

- **Raises** `FileNotFoundError` if the file doesn't exist.
- **Raises** `KeyError` if required fields are missing.
- **Raises** `json.JSONDecodeError` if the file is not valid JSON.

### Exceptions

| Exception | Raised When |
|-----------|-------------|
| `TokenRefreshError` | Token refresh fails permanently (expired/revoked refresh token, missing client credentials) |
| `TokenNotSetError` | `get_token()` called but no token was ever provided |

---

## Integration Patterns

### Pattern 1: API Token → OAuth Wrapper

Minimal migration — wrap your existing `email + api_token` client with
OAuth without rewriting everything:

```python
from client import ZendeskTokenManager


class OAuthZendeskClient:
    """Drop-in replacement for an API-token client."""

    def __init__(self, subdomain: str, token_mgr: ZendeskTokenManager):
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.mgr = token_mgr

    def _headers(self):
        return {"Authorization": f"Bearer {self.mgr.get_token()}"}

    def get(self, path: str, **kwargs):
        resp = requests.get(f"{self.base}/{path}", headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            self.mgr.force_refresh()
            resp = requests.get(f"{self.base}/{path}", headers=self._headers(), **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, data: dict, **kwargs):
        resp = requests.post(f"{self.base}/{path}", json=data, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            self.mgr.force_refresh()
            resp = requests.post(f"{self.base}/{path}", json=data, headers=self._headers(), **kwargs)
        resp.raise_for_status()
        return resp.json()

    # Add put, delete, etc. following the same pattern
```

### Pattern 2: Persist to a JSON File

```python
import time
import json
from client import ZendeskTokenManager, load_credentials

TOKEN_FILE = "credentials.json"

def save_tokens(token, refresh_token=None):
    data = {
        "oauth_token": token,
        "acquired_at": time.time()
    }
    if refresh_token:
        data["oauth_refresh_token"] = refresh_token
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

# Load or create
try:
    mgr = load_credentials(TOKEN_FILE)
except FileNotFoundError:
    mgr = ZendeskTokenManager(subdomain="acme", oauth_token="...", ...)

# Auto-save on refresh
mgr.on_refresh(lambda token, refresh: save_tokens(token, refresh))

# Use it
token = mgr.get_token()
```

### Pattern 3: Persist to a .env File

```python
from pathlib import Path
from client import ZendeskTokenManager

ENV_PATH = ".env"

def update_env(token: str, refresh_token: str | None = None):
    path = Path(ENV_PATH)
    if not path.exists():
        return
    text = path.read_text()
    lines = text.splitlines()
    new_lines = []
    for line in lines:
        key = line.strip().split("=", 1)[0]
        if key == "ZENDESK_OAUTH_TOKEN":
            new_lines.append(f"ZENDESK_OAUTH_TOKEN={token}")
        elif key == "ZENDESK_OAUTH_REFRESH_TOKEN" and refresh_token:
            new_lines.append(f"ZENDESK_OAUTH_REFRESH_TOKEN={refresh_token}")
        else:
            new_lines.append(line)
    path.write_text("\n".join(new_lines) + "\n")

mgr = ZendeskTokenManager(subdomain="acme", ...)
mgr.on_refresh(lambda token, refresh: update_env(token, refresh))
```

### Pattern 4: Multiple Subsystems

One manager notifies every part of the application:

```python
mgr = ZendeskTokenManager(subdomain="acme", ...)

mgr.on_refresh(lambda t, rt: db.store_token(t))
mgr.on_refresh(lambda t, rt: cache.invalidate())
mgr.on_refresh(lambda t, rt: api_client.set_auth(t))
mgr.on_refresh(lambda t, rt: webhook_client.set_auth(t))
```

### Pattern 5: Retry-Decorated Requests

A reusable decorator for any API call:

```python
from functools import wraps
from client import ZendeskTokenManager


def with_auto_auth(mgr: ZendeskTokenManager):
    """Decorator that injects a fresh Bearer token and retries on 401."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {mgr.get_token()}"
            try:
                return func(*args, headers=headers, **kwargs)
            except requests.HTTPError as exc:
                if exc.response.status_code == 401:
                    mgr.force_refresh()
                    headers["Authorization"] = f"Bearer {mgr.get_token()}"
                    return func(*args, headers=headers, **kwargs)
                raise
        return wrapper
    return decorator


@with_auto_auth(mgr)
def fetch_users():
    return requests.get("https://acme.zendesk.com/api/v2/users.json")
```

---

## Real-World Scenarios

### CI/CD Pipeline (No Interactive Prompts)

Set environment variables in your CI system and construct the manager
programmatically:

```python
import os
from client import ZendeskTokenManager

mgr = ZendeskTokenManager(
    subdomain=os.environ["ZD_SUBDOMAIN"],
    oauth_token=os.environ["ZD_OAUTH_TOKEN"],
    oauth_refresh_token=os.environ.get("ZD_OAUTH_REFRESH_TOKEN"),
    oauth_client_id=os.environ["ZD_CLIENT_ID"],
    oauth_client_secret=os.environ["ZD_CLIENT_SECRET"],
)
```

> **Warning:** If `ZD_OAUTH_REFRESH_TOKEN` is not set, auto-refresh is
> disabled. The initial token will work until it expires. For long-running
> CI jobs, always include the refresh token.

### Docker Container

Mount credentials as a volume:

```dockerfile
FROM python:3.12-slim
COPY client.py /app/client.py
COPY credentials.json /app/credentials.json
CMD ["python", "/app/worker.py"]
```

```python
# worker.py
from client import load_credentials

mgr = load_credentials("/app/credentials.json")
mgr.on_refresh(lambda t, rt: print("Token refreshed, but not persisting "
                                    "(read-only filesystem). Tokens survive "
                                    "in memory for the container's lifetime."))

while True:
    token = mgr.get_token()
    # ... do work ...
```

> **Note:** If the container's filesystem is read-only, the callback can
> skip persistence — the in-memory token is updated regardless, and the
> container will get a fresh token on every restart from the mounted
> `credentials.json`.

### Serverless Function (AWS Lambda / Cloud Function)

Cold starts mean you cannot rely on in-memory state across invocations.
Always reconstruct the manager from cached credentials:

```python
import json
import os
from client import ZendeskTokenManager

# Load from environment or a secret store
mgr = ZendeskTokenManager(
    subdomain=os.environ["ZD_SUBDOMAIN"],
    oauth_token=os.environ["ZD_OAUTH_TOKEN"],
    oauth_refresh_token=os.environ.get("ZD_OAUTH_REFRESH_TOKEN"),
    oauth_client_id=os.environ["ZD_CLIENT_ID"],
    oauth_client_secret=os.environ["ZD_CLIENT_SECRET"],
)


def handler(event, context):
    token = mgr.get_token()
    # ... use token ...
    return {"statusCode": 200}
```

For warm starts (container reuse), the manager's in-memory token persists
across invocations inside the same runtime. The TTL timer resets on each
cold start but the proactive refresh handles it seamlessly.

### Multi-Tenant Application

One manager per tenant:

```python
tenants = {
    "acme": {"subdomain": "acme", "oauth_token": "...", ...},
    "widgetco": {"subdomain": "widgetco", "oauth_token": "...", ...},
}

managers = {
    name: ZendeskTokenManager(**cfg)
    for name, cfg in tenants.items()
}

def get_token_for(tenant: str) -> str:
    return managers[tenant].get_token()
```

---

## Security Considerations

### File Permissions

Credentials should never be world-readable:

```python
import os
import json

with open("credentials.json", "w") as f:
    json.dump(data, f)
os.chmod("credentials.json", 0o600)  # owner read/write only
```

`first_run.py` does this automatically. If you write your own persistence
code, always set restrictive permissions.

### Never Log Tokens

The manager never logs token values. When building on top of it, avoid
logging headers or full request URLs that contain the token:

```python
# BAD — token in log:
log.info(f"Authorization: Bearer {token}")

# GOOD — log without secrets:
log.info("Requesting users/me.json")
```

### Client Secret

The OAuth client secret is equivalent to a password. Treat it with the
same care:
- Never commit it to version control
- Use environment variables or a secrets manager
- Restrict who can create OAuth clients in Zendesk Admin

### Refresh Token Lifetime

Zendesk refresh tokens do not expire unless revoked. If a refresh token
is compromised, revoke it in Zendesk Admin and re-run `first_run.py`.

---

## Thread Safety

`ZendeskTokenManager` is fully thread-safe:

- `get_token()` can be called from multiple threads concurrently.
- When a refresh is due, the first caller acquires the lock and performs
  it; others block briefly and receive the freshly-refreshed token.
- `set_token()` and `force_refresh()` also acquire the same lock.
- Callbacks are invoked outside the lock (after the refresh response is
  processed), so a slow callback never blocks concurrent readers and a
  callback can safely call manager methods such as `set_token()`.

---

## Error Handling Strategy

| Scenario | Behavior |
|----------|----------|
| Token never set | `TokenNotSetError` raised immediately |
| Token expired, refresh succeeds | Transparent — `get_token()` returns new token |
| Token expired, Zendesk unreachable | Retries 3x with backoff; if all fail, returns current token (degradation) |
| Token expired, refresh token revoked | `force_refresh()` raises `TokenRefreshError`; `get_token()` returns old token |
| Multiple 401s in rapid succession | Each 401 calls `force_refresh()` — only one actually calls Zendesk (lock) |
| Callback raises | Logged as error; other callbacks still fire |
| Concurrent refresh at same moment | Double-checked locking prevents duplicate HTTP calls |
| Network timeout during refresh | Retries 3x with exponential backoff (2s, 4s, 8s) |
| Rate-limited (429) during refresh | Respects `Retry-After` header, retries up to 3 times |

### When to use `get_token()` vs `force_refresh()`

| Situation | Use |
|-----------|-----|
| Normal operation (every request) | `get_token()` |
| Got a 401 response from Zendesk | `force_refresh()` then retry |
| Just loaded credentials from a file | Either — `get_token()` is fine |
| Before a critical batch operation | `force_refresh()` (proactive) |

---

## Rate Limiting

The manager handles rate limits during token refresh (HTTP 429/503) by
respecting the `Retry-After` header. Exponential backoff is used when
`Retry-After` is absent.

| Setting | Value |
|---------|-------|
| Max retries per refresh | 3 |
| Backoff formula | 2^attempt seconds |
| Backoff cap | 30 seconds |
| Request timeout | 30 seconds |
| Refresh endpoint | `POST /oauth/tokens` |

> This covers only the token refresh endpoint. For general Zendesk API
> rate limiting during bulk operations, implement a token-bucket or
> use an existing client with built-in throttling.

---

## File Structure

```
TASK-1/
├── client.py           # The module — copy this into any project
├── first_run.py        # Interactive OAuth setup wizard
└── documentation.md    # This file
```

To use in another project, copy `client.py`:

```bash
cp TASK-1/client.py /path/to/your/project/
```

Then import:

```python
from client import ZendeskTokenManager, load_credentials, TokenRefreshError
```
