#!/usr/bin/env python3
"""Interactive Zendesk OAuth 2.0 manager for CloudKeeper.

Supports:
  * client_credentials: obtains a new access token when required (no refresh token).
  * authorization_code: local browser callback, access/refresh-token exchange, rotation.

The script writes secrets to ./creds.json and ensures ./creds.json is in .gitignore.
This is a local testing/bootstrap utility, not a production secret manager.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

try:
    import requests
except ImportError:
    print("ERROR: The 'requests' package is not installed.")
    print("Install it with: python -m pip install requests")
    raise SystemExit(1)


DEFAULT_SUBDOMAIN = "cloudkeepersupport"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:5000/auth"
TOKEN_PATH = "/oauth/tokens"
PROFILE_PATH = "/api/v2/users/me.json"
REQUEST_TIMEOUT = (10, 30)
CREDS_FILE = Path("creds.json")
GITIGNORE_FILE = Path(".gitignore")


class OAuthError(RuntimeError):
    """Raised when Zendesk returns an OAuth or API error."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def parse_expiry_seconds(value: Any) -> Optional[int]:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def normalise_subdomain(value: str) -> str:
    """Accept a subdomain or a Zendesk URL; reject unrelated hostnames."""
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Zendesk subdomain cannot be empty.")

    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    else:
        host = value.lower().split("/")[0]
        if host.endswith(".zendesk.com"):
            host = host[: -len(".zendesk.com")]

    if host.endswith(".zendesk.com"):
        host = host[: -len(".zendesk.com")]

    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", host):
        raise ValueError(
            "Enter a Zendesk subdomain such as 'cloudkeepersupport' or "
            "'https://cloudkeepersupport.zendesk.com'."
        )
    return host


def ensure_gitignore(path: Path = GITIGNORE_FILE) -> None:
    """Create .gitignore if missing, or append the creds entry once."""
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    lines = existing.splitlines()
    # A root-level /creds.json rule or a general creds.json rule already covers it.
    already_ignored = any(line.strip() in {"creds.json", "/creds.json"} for line in lines)
    if already_ignored:
        print(f"[OK] {path} already excludes creds.json")
        return

    additions = []
    if not any("Local Zendesk OAuth credentials" in line for line in lines):
        additions.append("# Local Zendesk OAuth credentials - never commit")
    additions.append("/creds.json")

    separator = "" if not existing or existing.endswith("\n") else "\n"
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(separator + "\n".join(additions) + "\n")
    except OSError as exc:
        raise RuntimeError(f"Cannot update {path}: {exc}") from exc

    print(f"[OK] Added /creds.json to {path}")


def load_credentials(path: Path = CREDS_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
            "Fix or back up the file before continuing; it was not overwritten."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return data


def save_credentials(data: dict[str, Any], path: Path = CREDS_FILE) -> None:
    """Atomically write credentials and restrict file permissions to owner read/write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_name: Optional[str] = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        except (AttributeError, OSError):
            pass

        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_name, path)
        temp_name = None
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            print("[WARN] Could not restrict creds.json permissions on this OS.")
    except OSError as exc:
        raise RuntimeError(f"Could not securely save {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def prompt_value(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or (default or "")


def prompt_secret(existing_secret: Optional[str]) -> str:
    prompt = "Zendesk OAuth Client Secret (hidden; Enter keeps saved secret): "
    entered = getpass.getpass(prompt).strip()
    if entered:
        return entered
    if existing_secret:
        print("[OK] Reusing the saved Client Secret.")
        return existing_secret
    raise ValueError("Client Secret is required.")


def collect_configuration(existing: dict[str, Any]) -> dict[str, Any]:
    print("\n--- Zendesk OAuth client configuration ---")
    default_subdomain = str(existing.get("subdomain") or DEFAULT_SUBDOMAIN)
    subdomain = normalise_subdomain(
        prompt_value("Zendesk subdomain", default_subdomain)
    )

    client_id = prompt_value(
        "OAuth Client ID / Identifier", str(existing.get("client_id") or "") or None
    )
    if not client_id:
        raise ValueError("OAuth Client ID is required.")

    client_secret = prompt_secret(existing.get("client_secret"))

    print("\nSelect OAuth flow:")
    print("  1. Client Credentials - background jobs / server-to-server")
    print("  2. Authorization Code - acts on behalf of a user; supports refresh token")
    current = existing.get("grant_type")
    default_choice = "2" if current == "authorization_code" else "1"
    choice = prompt_value("Choose 1 or 2", default_choice)
    if choice not in {"1", "2"}:
        raise ValueError("Choose 1 or 2.")
    grant_type = "client_credentials" if choice == "1" else "authorization_code"

    old_scope = str(existing.get("requested_scope") or existing.get("scope") or "read")
    requested_scope = prompt_value(
        "Requested OAuth scopes (space-separated; least privilege recommended)",
        old_scope,
    )
    requested_scope = " ".join(dict.fromkeys(requested_scope.split()))
    if not requested_scope:
        raise ValueError("At least one OAuth scope is required.")

    config: dict[str, Any] = {
        "schema_version": 1,
        "provider": "zendesk",
        "subdomain": subdomain,
        "instance_url": f"https://{subdomain}.zendesk.com",
        "grant_type": grant_type,
        "client_id": client_id,
        "client_secret": client_secret,
        "requested_scope": requested_scope,
    }

    if grant_type == "authorization_code":
        redirect_uri = prompt_value(
            "Redirect URI (must exactly match the Zendesk OAuth client)",
            str(existing.get("redirect_uri") or DEFAULT_REDIRECT_URI),
        )
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Redirect URI must be a complete http:// or https:// URL.")
        config["redirect_uri"] = redirect_uri
    else:
        config["redirect_uri"] = None

    # Keep harmless history and profile metadata, but never carry tokens across a
    # different OAuth client, tenant, or grant type.
    same_profile = (
        existing.get("subdomain") == config["subdomain"]
        and existing.get("client_id") == config["client_id"]
        and existing.get("grant_type") == config["grant_type"]
    )
    if same_profile:
        for key in (
            "token_history",
            "created_at",
            "last_validated_at",
            "last_validation",
            "token_type",
            "access_token",
            "refresh_token",
            "expires_in",
            "refresh_token_expires_in",
            "scope",
            "access_token_obtained_at",
            "access_token_expires_at",
            "refresh_token_obtained_at",
            "refresh_token_expires_at",
            "last_refreshed_at",
            "token_response",
        ):
            if key in existing:
                config[key] = existing[key]
    else:
        config["token_history"] = []

    config.setdefault("created_at", isoformat())
    return config


def safe_response_detail(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error") or body.get("error_description")
            if error:
                return str(error)
            return json.dumps(body, ensure_ascii=False)[:1200]
    except ValueError:
        pass
    return (response.text or "No response body")[:1200]


def post_token_request(instance_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{instance_url}{TOKEN_PATH}"
    try:
        response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise OAuthError(f"Could not reach Zendesk token endpoint: {exc}") from exc

    if response.status_code not in (200, 201):
        raise OAuthError(
            f"Zendesk token request failed (HTTP {response.status_code}): "
            f"{safe_response_detail(response)}"
        )

    try:
        token_data = response.json()
    except ValueError as exc:
        raise OAuthError("Zendesk returned a non-JSON token response.") from exc

    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise OAuthError("Zendesk response did not contain an access_token.")
    return token_data


def build_auth_url(config: dict[str, Any], state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "scope": config["requested_scope"],
        "state": state,
    }
    return (
        f"{config['instance_url']}/oauth/authorizations/new?"
        f"{urlencode(params)}"
    )


def manual_authorization_code(auth_url: str, expected_state: str) -> str:
    print("\nOpen this URL in a browser and approve access:\n")
    print(auth_url)
    print("\nAfter Zendesk redirects, paste the COMPLETE redirect URL below.")
    pasted = input("Redirect URL: ").strip()
    parsed = urlparse(pasted)
    query = parse_qs(parsed.query)
    if query.get("state", [None])[0] != expected_state:
        raise OAuthError("OAuth state did not match. Authorization response rejected.")
    if query.get("error"):
        raise OAuthError(
            f"Authorization was not granted: {query.get('error_description', query['error'])[0]}"
        )
    code = query.get("code", [None])[0]
    if not code:
        raise OAuthError("The redirect URL did not contain a code parameter.")
    return code


def obtain_code_with_local_callback(config: dict[str, Any]) -> str:
    """Run a tiny loopback-only callback listener and validate OAuth state."""
    redirect_uri = config["redirect_uri"]
    parsed = urlsplit(redirect_uri)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost"}:
        state = secrets.token_urlsafe(32)
        return manual_authorization_code(build_auth_url(config, state), state)

    # Port 80 commonly requires elevated privileges; use manual callback mode.
    port = parsed.port or 80
    if port < 1024:
        state = secrets.token_urlsafe(32)
        return manual_authorization_code(build_auth_url(config, state), state)

    callback_path = parsed.path or "/"
    state = secrets.token_urlsafe(32)
    result: dict[str, Optional[str]] = {"code": None, "error": None}
    completed = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            incoming = urlsplit(self.path)
            if incoming.path != callback_path:
                self.send_error(404, "Unexpected callback path")
                return

            query = parse_qs(incoming.query)
            returned_state = query.get("state", [None])[0]
            if returned_state != state:
                self.send_error(400, "OAuth state mismatch")
                return

            error = query.get("error", [None])[0]
            if error:
                result["error"] = (
                    f"{error}: {query.get('error_description', [''])[0]}"
                )
            else:
                result["code"] = query.get("code", [None])[0]
                if not result["code"]:
                    result["error"] = "Callback did not include an authorization code."

            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Zendesk OAuth</title></head><body>"
                "<h2>Zendesk authorization response received.</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            completed.set()

        def log_message(self, format: str, *args: Any) -> None:
            # Avoid logging callback query strings (they contain the one-time code).
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), CallbackHandler)
    except OSError:
        state = secrets.token_urlsafe(32)
        return manual_authorization_code(build_auth_url(config, state), state)

    server.timeout = 1
    auth_url = build_auth_url(config, state)
    print("\nOpening Zendesk authorization in your browser...")
    print("If the browser does not open, copy this URL manually:\n")
    print(auth_url)
    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except Exception:
        pass

    print(f"\nWaiting for the local callback at {redirect_uri}")
    print("Complete the approval in the browser. Waiting up to 5 minutes...")
    deadline = time.monotonic() + 300
    try:
        while not completed.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not completed.is_set():
        raise OAuthError(
            "Timed out waiting for the OAuth callback. Check the redirect URI in "
            "Zendesk and run the script again."
        )
    if result["error"]:
        raise OAuthError(f"Authorization failed: {result['error']}")
    if not result["code"]:
        raise OAuthError("No authorization code was received.")
    return str(result["code"])


def obtain_authorization_code_token(config: dict[str, Any]) -> dict[str, Any]:
    code = obtain_code_with_local_callback(config)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": config["redirect_uri"],
        "scope": config["requested_scope"],
    }
    # The code is short-lived and single-use. Do not persist it.
    return post_token_request(config["instance_url"], payload)


def obtain_client_credentials_token(config: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": config["requested_scope"],
    }
    return post_token_request(config["instance_url"], payload)


def refresh_authorization_code_token(config: dict[str, Any]) -> dict[str, Any]:
    refresh_token = config.get("refresh_token")
    if not refresh_token:
        raise OAuthError(
            "No saved refresh token exists. Run a new Authorization Code flow."
        )
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    }
    return post_token_request(config["instance_url"], payload)


def apply_token_response(
    config: dict[str, Any], token_data: dict[str, Any], used_grant: str
) -> dict[str, Any]:
    """Update current token fields; store the complete latest response."""
    now = utc_now()
    expires_in = parse_expiry_seconds(token_data.get("expires_in"))
    refresh_expires_in = parse_expiry_seconds(
        token_data.get("refresh_token_expires_in")
    )

    config["access_token"] = token_data["access_token"]
    config["refresh_token"] = token_data.get("refresh_token")
    config["token_type"] = token_data.get("token_type", "bearer")
    config["scope"] = token_data.get("scope", config.get("requested_scope"))
    config["expires_in"] = token_data.get("expires_in")
    config["refresh_token_expires_in"] = token_data.get("refresh_token_expires_in")
    config["access_token_obtained_at"] = isoformat(now)
    config["access_token_expires_at"] = (
        isoformat(now + timedelta(seconds=expires_in)) if expires_in is not None else None
    )
    config["refresh_token_obtained_at"] = (
        isoformat(now) if token_data.get("refresh_token") else None
    )
    config["refresh_token_expires_at"] = (
        isoformat(now + timedelta(seconds=refresh_expires_in))
        if token_data.get("refresh_token") and refresh_expires_in is not None
        else None
    )
    config["last_token_grant_type"] = used_grant
    config["last_token_received_at"] = isoformat(now)
    config["token_response"] = token_data  # Preserve every field Zendesk returned.

    if used_grant == "refresh_token":
        config["last_refreshed_at"] = isoformat(now)
        if not token_data.get("refresh_token"):
            print(
                "[WARN] Zendesk did not return a replacement refresh token. "
                "The previous one may no longer be valid; reauthorize when needed."
            )

    history = config.setdefault("token_history", [])
    if isinstance(history, list):
        history.append(
            {
                "received_at": isoformat(now),
                "grant_type": used_grant,
                "scope": config.get("scope"),
                "expires_in": token_data.get("expires_in"),
                "refresh_token_returned": bool(token_data.get("refresh_token")),
            }
        )
        config["token_history"] = history[-20:]

    return config


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def token_is_expired_or_near_expiry(config: dict[str, Any], margin: int = 90) -> bool:
    expiry = parse_iso_datetime(config.get("access_token_expires_at"))
    if expiry is None:
        return False  # Unknown expiry: try the API and react to 401.
    return expiry <= utc_now() + timedelta(seconds=margin)


def acquire_or_renew(config: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Acquire a new token or renew the existing token using the selected grant."""
    if not force and config.get("access_token") and not token_is_expired_or_near_expiry(config):
        print("[OK] Saved access token is not near expiry; keeping it.")
        return config

    grant_type = config.get("grant_type")
    if grant_type == "client_credentials":
        print("\nRequesting a fresh Client Credentials access token...")
        token_data = obtain_client_credentials_token(config)
        apply_token_response(config, token_data, "client_credentials")
    elif grant_type == "authorization_code":
        if config.get("refresh_token"):
            print("\nRefreshing the Authorization Code token pair...")
            token_data = refresh_authorization_code_token(config)
            apply_token_response(config, token_data, "refresh_token")
        else:
            print("\nNo refresh token is saved. Starting browser authorization...")
            token_data = obtain_authorization_code_token(config)
            apply_token_response(config, token_data, "authorization_code")
    else:
        raise OAuthError("Unknown or missing grant_type. Run setup again.")

    save_credentials(config)
    print("[OK] Latest token response saved to creds.json")
    return config


def validate_profile(config: dict[str, Any]) -> bool:
    access_token = config.get("access_token")
    if not access_token:
        raise OAuthError("No access token is saved. Obtain a token first.")

    url = f"{config['instance_url']}{PROFILE_PATH}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise OAuthError(f"Could not call the Zendesk profile endpoint: {exc}") from exc

    if response.status_code == 401:
        return False
    if response.status_code == 403:
        raise OAuthError(
            "Zendesk returned HTTP 403. The token may be valid, but the user role "
            "or OAuth scopes do not permit this endpoint. Review Allowed Scopes "
            "and the requested scopes in the OAuth client."
        )
    if response.status_code != 200:
        raise OAuthError(
            f"Profile validation failed (HTTP {response.status_code}): "
            f"{safe_response_detail(response)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError("Zendesk profile endpoint returned invalid JSON.") from exc

    user = payload.get("user", {}) if isinstance(payload, dict) else {}
    print("\n[OK] Zendesk API authentication successful")
    print(f"  Tenant : {config['instance_url']}")
    print(f"  User   : {user.get('name', 'Not returned')}")
    print(f"  Email  : {user.get('email', 'Not returned')}")
    print(f"  Role   : {user.get('role', 'Not returned')}")
    print(f"  Scope  : {config.get('scope') or 'Not returned'}")

    config["last_validated_at"] = isoformat()
    config["last_validation"] = {
        "status": "success",
        "http_status": response.status_code,
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "user_email": user.get("email"),
        "user_role": user.get("role"),
    }
    save_credentials(config)
    return True


def setup_and_obtain(existing: dict[str, Any]) -> dict[str, Any]:
    config = collect_configuration(existing)
    print("\n[INFO] Ensuring creds.json is excluded from Git...")
    ensure_gitignore()
    print("\n[INFO] Requesting Zendesk OAuth token...")

    if config["grant_type"] == "client_credentials":
        token_data = obtain_client_credentials_token(config)
        apply_token_response(config, token_data, "client_credentials")
    else:
        token_data = obtain_authorization_code_token(config)
        apply_token_response(config, token_data, "authorization_code")

    save_credentials(config)
    print(f"[OK] Credentials and token response saved to {CREDS_FILE.resolve()}")
    return config


def validate_with_one_retry(config: dict[str, Any]) -> None:
    """Validate the token and reacquire it once if Zendesk returns HTTP 401."""
    if validate_profile(config):
        return
    print("[WARN] Zendesk rejected the access token (HTTP 401). Renewing once...")
    acquire_or_renew(config, force=True)
    if not validate_profile(config):
        raise OAuthError("Zendesk still returned HTTP 401 after token renewal.")


def show_token_status(config: dict[str, Any]) -> None:
    print("\n--- Saved credential status ---")
    print(f"Tenant       : {config.get('instance_url', 'Not configured')}")
    print(f"Grant type   : {config.get('grant_type', 'Not configured')}")
    print(f"Client ID    : {config.get('client_id', 'Not configured')}")
    print(f"Scope        : {config.get('scope') or config.get('requested_scope', 'Unknown')}")
    print(f"Access token : {'Present' if config.get('access_token') else 'Missing'}")
    print(f"Refresh token: {'Present' if config.get('refresh_token') else 'Not issued / not saved'}")
    print(f"Access expiry: {config.get('access_token_expires_at') or 'Unknown'}")
    print(f"Refresh expiry: {config.get('refresh_token_expires_at') or 'Unknown'}")
    print(f"Credentials  : {CREDS_FILE.resolve()}")


def main() -> int:
    print("=" * 68)
    print("  CLOUDKEEPER ZENDESK OAUTH 2.0 MANAGER")
    print("  https://cloudkeepersupport.zendesk.com")
    print("=" * 68)
    print("This script stores the Client Secret and current tokens in creds.json.")
    print("That file is sensitive. Keep it private and never commit or share it.\n")

    try:
        ensure_gitignore()
        existing = load_credentials()
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return 1

    if existing:
        print(f"[OK] Existing credentials found: {CREDS_FILE.resolve()}")
        show_token_status(existing)
        print("\nChoose an action:")
        print("  1. Set up / obtain a new token (interactive OAuth setup)")
        print("  2. Renew the saved token (refresh token or client credentials)")
        print("  3. Validate the saved token (renews when near expiry)")
        print("  4. Exit")
        action = prompt_value("Choose 1-4", "3")
    else:
        print("[INFO] No creds.json found. Starting first-time setup.")
        action = "1"

    try:
        if action == "1":
            config = setup_and_obtain(existing)
            show_token_status(config)
            try:
                validate_with_one_retry(config)
            except OAuthError as exc:
                print(f"\n[WARN] Token was saved, but validation did not complete: {exc}")
            return 0

        if action == "2":
            if not existing:
                raise OAuthError("No saved credentials. Choose setup first.")
            config = existing
            acquire_or_renew(config, force=True)
            show_token_status(config)
            try:
                validate_with_one_retry(config)
            except OAuthError as exc:
                print(f"\n[WARN] Token renewed, but validation failed: {exc}")
            return 0

        if action == "3":
            if not existing:
                raise OAuthError("No saved credentials. Choose setup first.")
            config = existing
            acquire_or_renew(config)
            validate_with_one_retry(config)
            return 0

        if action == "4":
            print("Exiting. No credentials were changed.")
            return 0

        raise ValueError("Invalid menu choice. Run the script again and select 1-4.")

    except (OAuthError, RuntimeError, ValueError, requests.RequestException) as exc:
        print(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
