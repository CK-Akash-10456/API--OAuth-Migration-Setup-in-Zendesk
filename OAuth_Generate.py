"""
OAuth_Generate.py — Self-contained Zendesk OAuth handler with auto-heal.

A single-file Zendesk OAuth credential manager. Auto-refreshes tokens
before they expire and persists the new tokens transparently. Can be
passed directly as ``auth`` to any ``requests``-based Zendesk client.

Usage:
    from OAuth_Generate import ZendeskOAuth

    auth = ZendeskOAuth()
    resp = requests.get("https://sub.zendesk.com/api/v2/users/me.json", auth=auth)

    # Or use the token directly
    headers = {"Authorization": f"Bearer {auth.get_token()}"}

Dependencies: requests (pip install requests)
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.auth import AuthBase

log = logging.getLogger(__name__)

REFRESH_ENDPOINT = "https://{subdomain}.zendesk.com/oauth/tokens"


def _jwt_exp(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("exp")
    except (ValueError, json.JSONDecodeError):
        return None


class TokenRefreshError(Exception):
    """Token refresh failed permanently — refresh token may be expired/revoked."""


class ZendeskOAuth(AuthBase):
    """
    Zendesk OAuth handler that can be passed as ``auth`` to requests.

    Auto-refreshes before expiry and persists refreshed tokens when loaded
    from a file. Thread-safe.

    Usage:
        >>> auth = ZendeskOAuth("sub", "token", "refresh", "cid", "secret")
        >>> requests.get(url, auth=auth)
    """

    REFRESH_THRESHOLD = 0.80
    MAX_RETRIES = 3
    TIMEOUT = 30
    BACKOFF_BASE = 2.0
    BACKOFF_CAP = 30.0

    def __init__(
        self,
        subdomain: Optional[str] = None,
        oauth_token: Optional[str] = None,
        oauth_refresh_token: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        token_ttl: Optional[int] = None,
        acquired_at: Optional[float] = None,
        creds_path: Optional[str] = None,
    ) -> None:
        if subdomain is None:
            self._load_from_file(creds_path or "credentials.json")
            return
        self.subdomain = subdomain.strip().lower().split(".zendesk.com")[0].split("://")[-1].split("/")[0]
        self._token: Optional[str] = oauth_token
        self._refresh_token: Optional[str] = oauth_refresh_token
        self._client_id: Optional[str] = oauth_client_id
        self._client_secret: Optional[str] = oauth_client_secret
        self._creds_path: Optional[str] = creds_path

        if token_ttl:
            self._ttl = token_ttl
        elif oauth_token:
            exp = _jwt_exp(oauth_token)
            self._ttl = max(1800, exp - int(acquired_at or time.time())) if exp else 3600
        else:
            self._ttl = 3600

        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str, Optional[str]], None]] = []

        if oauth_token:
            if acquired_at is not None:
                now = time.time()
                age = max(0.0, now - float(acquired_at))
                self._acquired_at = time.monotonic() - age
            else:
                self._acquired_at = time.monotonic()
        else:
            self._acquired_at = 0.0

        if self._token and self._creds_path:
            self.on_refresh(self._auto_save_callback)

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers["Authorization"] = f"Bearer {self.get_token()}"
        return r

    def get_token(self) -> str:
        if self._token is None:
            raise TokenRefreshError("No OAuth token provided. Set oauth_token or use ZendeskOAuth.load().")
        if self._refresh_token is not None and self._age_pct() >= self.REFRESH_THRESHOLD:
            self._refresh_if_needed()
        return self._token

    def force_refresh(self) -> str:
        if self._refresh_token is None:
            return self._token if self._token else ""
        result = self._do_refresh()
        if result:
            self._notify_all(*result)
            return result[0]
        return self._token if self._token else ""

    def on_refresh(self, callback: Callable[[str, Optional[str]], None]) -> None:
        self._callbacks.append(callback)

    def _load_from_file(self, path: str) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.subdomain = (data.get("subdomain") or "").strip()
        self._token = (data.get("oauth_token") or "").strip() or None
        self._refresh_token = (data.get("oauth_refresh_token") or "").strip() or None
        self._client_id = (data.get("oauth_client_id") or "").strip()
        self._client_secret = (data.get("oauth_client_secret") or "").strip()
        self._creds_path = path
        self._ttl = data.get("token_ttl") or 3600
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str, Optional[str]], None]] = []
        acquired_at = data.get("acquired_at")
        if self._token and acquired_at:
            age = max(0.0, time.time() - float(acquired_at))
            self._acquired_at = time.monotonic() - age
        else:
            self._acquired_at = time.monotonic() if self._token else 0.0
        if self._token and self._creds_path:
            self.on_refresh(self._auto_save_callback)

    @classmethod
    def load(cls, path: str = "credentials.json") -> ZendeskOAuth:
        obj = cls.__new__(cls)
        obj._load_from_file(path)
        return obj

    def save(self, path: Optional[str] = None) -> None:
        path = path or self._creds_path
        if not path:
            return
        payload = {
            "subdomain": self.subdomain,
            "oauth_token": self._token,
            "oauth_refresh_token": self._refresh_token,
            "oauth_client_id": self._client_id,
            "oauth_client_secret": self._client_secret,
            "token_ttl": self._ttl,
            "acquired_at": time.time(),
        }
        Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        Path(path).chmod(0o600)

    @property
    def has_refresh(self) -> bool:
        return self._refresh_token is not None

    @property
    def age(self) -> float:
        return time.monotonic() - self._acquired_at

    @property
    def ttl(self) -> int:
        return self._ttl

    def _age_pct(self) -> float:
        return min(1.0, self.age / self._ttl)

    def _refresh_if_needed(self) -> None:
        if not self._lock.acquire(timeout=self.TIMEOUT):
            return
        try:
            if self._age_pct() >= self.REFRESH_THRESHOLD:
                result = self._do_refresh()
        except TokenRefreshError:
            return
        finally:
            self._lock.release()
        if result:
            self._notify_all(*result)

    def _do_refresh(self) -> Optional[tuple[str, Optional[str]]]:
        if not self._refresh_token:
            return None
        if not self._client_id or not self._client_secret:
            return None

        url = REFRESH_ENDPOINT.format(subdomain=self.subdomain)
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(url, json=body, timeout=self.TIMEOUT)
            except requests.RequestException:
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                return self._apply_refresh_response(resp)

            if resp.status_code in (429, 503):
                raw = resp.headers.get("Retry-After")
                try:
                    delay = max(1.0, min(float(raw), self.BACKOFF_CAP)) if raw else self.BACKOFF_BASE ** attempt
                except (TypeError, ValueError):
                    delay = self.BACKOFF_BASE ** attempt
                time.sleep(delay)
                continue

            if 400 <= resp.status_code < 500:
                raise TokenRefreshError(f"Refresh rejected (HTTP {resp.status_code})")

            self._backoff(attempt)

        raise TokenRefreshError(f"Refresh failed after {self.MAX_RETRIES} attempts")

    def _apply_refresh_response(self, resp: requests.Response) -> tuple[str, Optional[str]]:
        data = resp.json()
        new_token = data.get("access_token")
        if not new_token:
            raise TokenRefreshError("No access_token in refresh response")

        self._token = new_token
        self._acquired_at = time.monotonic()
        exp = _jwt_exp(new_token)
        if exp:
            self._ttl = max(1800, exp - int(time.time()))

        rotated = None
        new_refresh = data.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
            rotated = new_refresh

        return new_token, rotated

    def _notify_all(self, token: str, refresh_token: Optional[str]) -> None:
        for cb in self._callbacks:
            try:
                cb(token, refresh_token)
            except Exception as exc:
                log.error("Refresh callback failed: %s", exc)

    def _auto_save_callback(self, token: str, refresh_token: Optional[str]) -> None:
        self.save()

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(ZendeskOAuth.BACKOFF_BASE ** attempt, ZendeskOAuth.BACKOFF_CAP))
