"""
client.py — Drop-in Zendesk OAuth credential manager with auto-refresh.

A self-contained, single-file helper for managing Zendesk OAuth tokens.
Drop it into ANY Python project that talks to the Zendesk API.

No project-specific config, no .env coupling, no framework dependency.
Just `requests`.

Usage:
    from client import ZendeskTokenManager

    # First time: provide tokens + client credentials
    mgr = ZendeskTokenManager(
        subdomain="my-subdomain",
        oauth_token="abc...",
        oauth_refresh_token="def...",
        oauth_client_id="my-client-id",
        oauth_client_secret="my-client-secret",
    )

    # Always returns a fresh token — auto-refreshes before expiry
    headers = {"Authorization": f"Bearer {mgr.get_token()}"}
    resp = requests.get("https://my-subdomain.zendesk.com/api/v2/users/me.json",
                        headers=headers)

    # On 401, force a refresh and retry:
    if resp.status_code == 401:
        mgr.force_refresh()
        headers["Authorization"] = f"Bearer {mgr.get_token()}"
        resp = requests.get(...)

    # Save newly-refreshed tokens wherever you want:
    def on_refresh(token, refresh_token):
        with open("tokens.json", "w") as f:
            json.dump({"token": token, "refresh": refresh_token}, f)

    mgr.on_refresh(on_refresh)

    # On next run, load saved tokens and pass them in:
    mgr = ZendeskTokenManager(subdomain="...", oauth_token="...",
                              oauth_refresh_token="...", ...)

Dependencies: requests (pip install requests)

Edge cases handled:
    - Token never provided → TokenNotSetError (clear message, not a segfault)
    - Token expired → proactive refresh at 80% of TTL
    - Concurrent threads → only one refresh, others wait
    - Zendesk unreachable → retries with exponential backoff, keeps old token
    - Refresh token revoked → TokenRefreshError with actionable message
    - Zendesk rotates refresh_token → new one is stored automatically
    - Multiple callbacks → one failure doesn't block the rest
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)


def _normalize_subdomain(raw: str) -> str:
    """Accept a bare subdomain or a full Zendesk hostname/URL."""
    cleaned = raw.strip().lower()
    if "://" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        cleaned = parsed.netloc or parsed.path
    cleaned = cleaned.strip("/")
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[0]
    if "?" in cleaned:
        cleaned = cleaned.split("?", 1)[0]
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0]
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[0]
    if cleaned.endswith(".zendesk.com"):
        cleaned = cleaned[: -len(".zendesk.com")]
    return cleaned


class TokenRefreshError(Exception):
    """Token refresh failed permanently — refresh token may be expired/revoked."""


class TokenNotSetError(Exception):
    """get_token() called but no token was ever provided."""


class ZendeskTokenManager:
    """
    Generic Zendesk OAuth credential manager with transparent auto-refresh.

    Proactive refresh:
        get_token() checks if the current token has been held for >= 80% of
        its expected TTL (default 3600 s). If so, it refreshes before returning.

    Reactive refresh:
        force_refresh() immediately obtains a new token — call this when you
        get a 401 from the API.

    Thread safety:
        Uses double-checked locking. If N threads call get_token() concurrently
        at the 80% threshold, only one performs the refresh; the others get the
        still-valid token.

    Degradation:
        If Zendesk's refresh endpoint is unreachable or returns a transient
        error, the current token is preserved and returned. The application
        never crashes because of a network blip during refresh. Only a
        confirmed revoked/expired refresh_token raises TokenRefreshError.

    Persistence is YOUR job — use on_refresh() to save tokens wherever you want
    (JSON file, database, env file, secrets manager, etc.).
    """

    DEFAULT_TTL: int = 3600
    REFRESH_THRESHOLD: float = 0.80

    MAX_RETRIES: int = 3
    TIMEOUT: int = 30
    BACKOFF_BASE: float = 2.0
    BACKOFF_CAP: float = 30.0

    def __init__(
        self,
        subdomain: str,
        oauth_token: Optional[str] = None,
        oauth_refresh_token: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        ttl: Optional[int] = None,
        acquired_at: Optional[float] = None,
    ) -> None:
        """
        Args:
            subdomain: Zendesk subdomain slug, hostname, or full URL.
            oauth_token: The initial Bearer access token.
            oauth_refresh_token: Token used to obtain new access tokens.
            oauth_client_id: OAuth client identifier (needed for refresh).
            oauth_client_secret: OAuth client secret (needed for refresh).
            ttl: Expected access token lifetime in seconds. Default 3600.
            acquired_at: Absolute Unix timestamp when the token was obtained.
        """
        self.subdomain = _normalize_subdomain(subdomain)
        self._token: Optional[str] = oauth_token
        self._refresh_token: Optional[str] = oauth_refresh_token
        self._client_id: Optional[str] = oauth_client_id
        self._client_secret: Optional[str] = oauth_client_secret
        self._ttl: int = ttl or self.DEFAULT_TTL

        self._lock = threading.Lock()

        if oauth_token:
            if acquired_at is not None:
                try:
                    acquired_at_ts = float(acquired_at)
                except (TypeError, ValueError) as exc:
                    raise ValueError("acquired_at must be a Unix timestamp.") from exc
                # Translate absolute timestamp to a monotonic point in the past.
                age = max(0.0, time.time() - acquired_at_ts)
                self._acquired_at = time.monotonic() - age
            else:
                self._acquired_at = time.monotonic()
        else:
            self._acquired_at = 0.0

        self._callbacks: list[Callable[[str, Optional[str]], None]] = []

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """
        Return a valid access token.

        Auto-refreshes if the current token is past the TTL threshold
        and a refresh token is available. Returns the existing token
        if no refresh is needed or possible.

        Raises TokenNotSetError if no token has ever been provided.
        """
        if self._token is None:
            raise TokenNotSetError(
                "No OAuth token provided. Call set_token() or pass "
                "oauth_token to the constructor."
            )

        if self._refresh_token is not None and self._age_pct() >= self.REFRESH_THRESHOLD:
            self._refresh_if_needed()

        return self._token

    def force_refresh(self) -> str:
        """
        Immediately refresh the access token, bypassing the age check.

        Returns the new token on success.
        Returns the current token without raising if no refresh token is
        available (degradation).
        Raises TokenRefreshError if the refresh attempt fails permanently.
        """
        if self._refresh_token is None:
            log.warning("force_refresh() called but no refresh token configured.")
            if self._token is not None:
                return self._token
            raise TokenNotSetError("No token available.")

        callback_payload: Optional[tuple[str, Optional[str]]] = None
        with self._lock:
            callback_payload = self._do_refresh()

        if callback_payload is not None:
            self._notify_all(*callback_payload)

        return callback_payload[0]  # type: ignore[index]

    def set_token(
        self,
        oauth_token: str,
        oauth_refresh_token: Optional[str] = None,
    ) -> None:
        """
        Set or update the stored access token (and optionally refresh token).

        This does NOT call Zendesk — it only replaces in-memory values.
        Use this after the initial OAuth authorization-code flow.
        """
        if not oauth_token or not isinstance(oauth_token, str):
            raise ValueError("oauth_token must be a non-empty string.")

        with self._lock:
            self._token = oauth_token
            self._acquired_at = time.monotonic()
            if oauth_refresh_token is not None:
                self._refresh_token = oauth_refresh_token

    def on_refresh(self, callback: Callable[[str, Optional[str]], None]) -> None:
        """
        Register a callback invoked after every successful token refresh.

        The callback receives (new_access_token, new_refresh_token).
        new_refresh_token is None if Zendesk didn't rotate it.

        Multiple callbacks are supported. A failing callback does not
        prevent others from being notified.
        """
        self._callbacks.append(callback)

    @property
    def has_refresh(self) -> bool:
        """True if a refresh token is available (auto-refresh possible)."""
        return self._refresh_token is not None

    @property
    def age(self) -> float:
        """Seconds since the current token was obtained."""
        return time.monotonic() - self._acquired_at

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------

    def _age_pct(self) -> float:
        return min(1.0, self.age / self._ttl)

    def _refresh_if_needed(self) -> None:
        callback_payload: Optional[tuple[str, Optional[str]]] = None
        if not self._lock.acquire(timeout=self.TIMEOUT):
            log.warning("Could not acquire refresh lock within timeout; using existing token.")
            return
        try:
            if self._age_pct() >= self.REFRESH_THRESHOLD:
                callback_payload = self._do_refresh()
        except TokenRefreshError:
            log.warning("Proactive refresh failed; using existing token until next attempt.")
        finally:
            self._lock.release()
        if callback_payload is not None:
            self._notify_all(*callback_payload)

    def _do_refresh(self) -> tuple[str, Optional[str]]:
        if not self._refresh_token:
            raise TokenRefreshError("No refresh token configured.")
        if not self._client_id or not self._client_secret:
            raise TokenRefreshError(
                "OAuth client credentials not configured. "
                "Pass oauth_client_id and oauth_client_secret."
            )

        url = f"https://{self.subdomain}.zendesk.com/oauth/tokens"
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        last_error: Optional[str] = None

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(url, json=body, timeout=self.TIMEOUT)
            except requests.Timeout as exc:
                last_error = f"Timeout: {exc}"
                self._backoff(attempt)
                continue
            except requests.ConnectionError as exc:
                last_error = f"ConnectionError: {exc}"
                self._backoff(attempt)
                continue
            except requests.RequestException as exc:
                last_error = f"RequestException: {exc}"
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                return self._apply_refresh_response(resp)

            # Rate-limited or temporarily unavailable — retry
            if resp.status_code in (429, 503):
                retry_after = self._parse_retry_after(resp.headers, attempt)
                log.info("Refresh rate-limited (HTTP %s), waiting %.1fs...",
                         resp.status_code, retry_after)
                last_error = f"HTTP {resp.status_code}: retry-after={retry_after}s"
                time.sleep(retry_after)
                continue

            # 4xx errors other than 429 are permanent
            if 400 <= resp.status_code < 500:
                snippet = (resp.text or "")[:300]
                raise TokenRefreshError(
                    f"Zendesk rejected the refresh token "
                    f"(HTTP {resp.status_code}): {snippet}. "
                    "The refresh token may be expired or revoked. "
                    "Re-run the OAuth authorization flow to obtain a new one."
                )

            # 5xx server error — retry
            last_error = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            self._backoff(attempt)

        raise TokenRefreshError(
            f"Token refresh failed after {self.MAX_RETRIES} attempts. "
            f"Last error: {last_error}"
        )

    def _apply_refresh_response(
        self, resp: requests.Response
    ) -> tuple[str, Optional[str]]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise TokenRefreshError(f"Non-JSON response from refresh endpoint: {exc}")

        new_token = data.get("access_token")
        if not new_token or not isinstance(new_token, str):
            raise TokenRefreshError(
                "Refresh response missing 'access_token' field."
            )

        previous_age = self.age
        self._token = new_token
        self._acquired_at = time.monotonic()

        rotated_refresh: Optional[str] = None
        new_refresh = data.get("refresh_token")
        if new_refresh and isinstance(new_refresh, str):
            self._refresh_token = new_refresh
            rotated_refresh = new_refresh

        log.info("OAuth token refreshed (age was %.1fs).", previous_age)
        return new_token, rotated_refresh

    def _notify_all(self, token: str, refresh_token: Optional[str]) -> None:
        for cb in self._callbacks:
            try:
                cb(token, refresh_token)
            except Exception as exc:
                log.error("Token refresh callback failed: %s", exc)

    @staticmethod
    def _parse_retry_after(headers, attempt: int) -> float:
        raw = headers.get("Retry-After")
        try:
            return max(1.0, min(float(raw), ZendeskTokenManager.BACKOFF_CAP)) if raw else (
                ZendeskTokenManager.BACKOFF_BASE ** attempt
            )
        except (TypeError, ValueError):
            return ZendeskTokenManager.BACKOFF_BASE ** attempt

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(
            ZendeskTokenManager.BACKOFF_BASE ** attempt,
            ZendeskTokenManager.BACKOFF_CAP,
        ))


# ------------------------------------------------------------------
#  Convenience: load credentials from a first_run.py JSON file
# ------------------------------------------------------------------


def load_credentials(path: str = "credentials.json") -> ZendeskTokenManager:
    """
    Load credentials saved by ``first_run.py`` and return a configured
    :class:`ZendeskTokenManager`.

    The JSON file must contain, at minimum:
        ``subdomain``, ``oauth_token``, ``oauth_client_id``, ``oauth_client_secret``

    ``oauth_refresh_token`` is optional but needed for auto-refresh.

    Args:
        path: Path to the JSON credentials file (default ``credentials.json``).

    Returns:
        A fully configured :class:`ZendeskTokenManager` ready for use.

    Raises:
        FileNotFoundError: The credentials file does not exist.
        KeyError: A required field is missing from the file.
        json.JSONDecodeError: The file is not valid JSON.
    """
    import json as _json

    data = _json.loads(Path(path).read_text(encoding="utf-8"))

    subdomain = (data.get("subdomain") or "").strip()
    oauth_token = (data.get("oauth_token") or "").strip()
    oauth_refresh_token = (data.get("oauth_refresh_token") or "").strip() or None
    oauth_client_id = (data.get("oauth_client_id") or "").strip()
    oauth_client_secret = (data.get("oauth_client_secret") or "").strip()
    acquired_at = data.get("acquired_at")

    if not subdomain:
        raise KeyError("credentials file is missing 'subdomain'")
    if not oauth_token:
        raise KeyError("credentials file is missing 'oauth_token'")
    if not oauth_client_id:
        raise KeyError("credentials file is missing 'oauth_client_id'")
    if not oauth_client_secret:
        raise KeyError("credentials file is missing 'oauth_client_secret'")

    return ZendeskTokenManager(
        subdomain=subdomain,
        oauth_token=oauth_token,
        oauth_refresh_token=oauth_refresh_token,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        acquired_at=acquired_at,
    )
