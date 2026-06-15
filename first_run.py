#!/usr/bin/env python3
"""
first_run.py — Interactive Zendesk OAuth first-time setup.

Walks you through the entire OAuth authorization-code flow:
  1. Prompts for subdomain, client ID, and client secret
  2. Opens the Zendesk authorization URL in your browser
  3. You paste the redirect URL back
  4. Exchanges the code for access + refresh tokens
  5. Saves credentials to a JSON file
  6. Validates the tokens with a live API call

After this script completes, `demo.py` can load the saved credentials
and auto-refresh without any manual intervention.

Usage:
    python first_run.py

Dependencies:
    pip install requests
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

import requests

# ------------------------------------------------------------------ #
#  Constants                                                          #
# ------------------------------------------------------------------ #

REDIRECT_URI = "http://localhost/callback"

SCOPES = {
    "source": "read",
    "target": "read write hc:write",
}

TOKEN_ENDPOINT = "https://{subdomain}.zendesk.com/oauth/tokens"
AUTH_URL = (
    "https://{subdomain}.zendesk.com/oauth/authorizations/new"
    "?response_type=code"
    "&redirect_uri={redirect_uri}"
    "&client_id={client_id}"
    "&scope={scope}"
    "&state={state}"
)

CREDS_FILE = Path(__file__).resolve().parent / "credentials.json"

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #


def sanitize_subdomain(raw: str) -> str:
    cleaned = raw.strip().lower()
    if "://" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        cleaned = parsed.netloc or parsed.path
    cleaned = cleaned.strip("/")
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[0]
    if cleaned.endswith(".zendesk.com"):
        cleaned = cleaned[: -len(".zendesk.com")]
    if not _SUBDOMAIN_RE.fullmatch(cleaned):
        print(
            f"  ✗ Invalid subdomain '{cleaned}'. "
            "Must be 1-63 alphanumeric characters or hyphens."
        )
        sys.exit(1)
    return cleaned


def build_auth_url(subdomain: str, client_id: str, scope: str, state: str) -> str:
    return AUTH_URL.format(
        subdomain=urllib.parse.quote(subdomain, safe=""),
        redirect_uri=urllib.parse.quote(REDIRECT_URI, safe=""),
        client_id=urllib.parse.quote(client_id, safe=""),
        scope=urllib.parse.quote(scope, safe=""),
        state=urllib.parse.quote(state, safe=""),
    )


def exchange_code(
    subdomain: str, client_id: str, secret: str, code: str, scope: str
) -> Tuple[str, Optional[str], str]:
    url = TOKEN_ENDPOINT.format(subdomain=subdomain)
    try:
        resp = requests.post(
            url,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": REDIRECT_URI,
                "scope": scope,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        print("\n  ✗ Could not reach Zendesk during token exchange.")
        print(f"    {type(exc).__name__}: {exc}")
        sys.exit(1)
    if not resp.ok:
        print(f"\n  ✗ Token exchange failed [HTTP {resp.status_code}]:")
        print(f"    {resp.text[:400]}")
        sys.exit(1)

    try:
        data = resp.json()
    except ValueError as exc:
        print("\n  ✗ Zendesk returned a non-JSON token response.")
        print(f"    {exc}")
        sys.exit(1)
    token = data.get("access_token")
    if not token:
        print(f"\n  ✗ No access_token in response: {data}")
        sys.exit(1)
    refresh_token = data.get("refresh_token")
    granted = data.get("scope", "")
    return token, refresh_token, granted


def verify_token(subdomain: str, token: str) -> dict:
    try:
        resp = requests.get(
            f"https://{subdomain}.zendesk.com/api/v2/users/me.json",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print("\n  ✗ Could not reach Zendesk while verifying the token.")
        print(f"    {type(exc).__name__}: {exc}")
        sys.exit(1)
    if resp.status_code == 401:
        print("\n  ✗ Token verification failed: 401 Unauthorized.")
        print("    The token was issued but Zendesk rejected it.")
        print("    Check that the approving user has the required permissions.")
        sys.exit(1)
    if not resp.ok:
        print(f"\n  ✗ Token verification failed [HTTP {resp.status_code}]:")
        print(f"    {resp.text[:300]}")
        sys.exit(1)
    try:
        data = resp.json()
    except ValueError as exc:
        print("\n  ✗ Zendesk returned a non-JSON verification response.")
        print(f"    {exc}")
        sys.exit(1)
    user = data.get("user", data)
    return {
        "name": user.get("name", "?"),
        "email": user.get("email", "?"),
        "role": user.get("role", "?"),
        "account_name": user.get("organization_name", "?"),
    }


def save_credentials(
    subdomain: str,
    oauth_token: str,
    oauth_refresh_token: Optional[str],
    oauth_client_id: str,
    oauth_client_secret: str,
    user_info: dict,
) -> None:
    payload = {
        "subdomain": subdomain,
        "oauth_token": oauth_token,
        "oauth_refresh_token": oauth_refresh_token,
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "acquired_at": time.time(),
        "user": user_info,
    }
    CREDS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    CREDS_FILE.chmod(0o600)


def parse_redirect_url(url: str, expected_state: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.query:
        print(
            "\n  ✗ No query string in URL. Make sure you copied the full "
            "redirect URL from the browser's address bar."
        )
        sys.exit(1)

    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        err = params["error"][0]
        desc = params.get("error_description", [""])[0]
        print(f"\n  ✗ Zendesk denied authorization: {err} — {desc}")
        sys.exit(1)

    state_values = params.get("state")
    if not state_values:
        print("\n  ✗ No 'state' parameter in URL. Aborting for safety.")
        sys.exit(1)
    if state_values[0] != expected_state:
        print("\n  ✗ OAuth state mismatch. The pasted redirect URL does not match this session.")
        print("    Start the authorization step again and paste the newest redirect URL.")
        sys.exit(1)

    codes = params.get("code")
    if not codes:
        print(
            "\n  ✗ No 'code' parameter in URL. Did you copy the URL "
            "before clicking Allow?"
        )
        sys.exit(1)

    return codes[0]


def compare_scopes(asked: str, granted: str) -> None:
    if not granted:
        return
    asked_set = set(asked.split())
    got_set = set(granted.split())
    missing = asked_set - got_set
    if not missing:
        return

    missing.discard("offline_access")
    if not missing:
        return

    print(f"\n  ⚠  Zendesk did NOT grant: {' '.join(sorted(missing))}")
    print("     The approving user may lack permission for these scopes.")
    print("     Some API calls may fail with 403 later.")


# ------------------------------------------------------------------ #
#  Main                                                               #
# ------------------------------------------------------------------ #


def main() -> None:
    print("=" * 60)
    print("  Zendesk OAuth First-Time Setup")
    print("=" * 60)

    # ---- 1. Gather info -------------------------------------------- #

    raw_sub = input("\n  Zendesk subdomain (e.g. 'mycompany'): ").strip()
    subdomain = sanitize_subdomain(raw_sub)

    client_id = input("  OAuth client ID: ").strip()
    if not client_id:
        print("  ✗ Client ID cannot be empty.")
        sys.exit(1)

    client_secret = input("  OAuth client secret: ").strip()
    if not client_secret:
        print("  ✗ Client secret cannot be empty.")
        sys.exit(1)

    print("\n  Account role:")
    print("    [1] Source  (read-only, for exporting data)")
    print("    [2] Target  (read + write, for importing data)")
    choice = input("  Choice [1/2] (default: 2): ").strip()
    role = "target" if choice != "1" else "source"
    scope = SCOPES[role]

    print(f"\n  → Role: {role}")
    print(f"  → Scope: {scope}")

    # ---- 2. Generate authorization URL ----------------------------- #

    oauth_state = secrets.token_urlsafe(24)
    auth_url = build_auth_url(subdomain, client_id, scope, oauth_state)

    print("\n" + "-" * 60)
    print("  Step 1 — Authorize in Zendesk")
    print("-" * 60)
    print(f"\n  Opening your browser to:\n    {auth_url}\n")
    webbrowser.open(auth_url)

    print(
        "  After clicking Allow, your browser will redirect to a page\n"
        "  saying 'localhost refused to connect' — that's expected.\n"
        "  Copy the FULL redirect URL from the address bar and paste it below.\n"
    )

    try:
        redirect_url = input("  Paste redirect URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  ✗ No URL provided. Aborting.")
        sys.exit(1)

    if not redirect_url:
        print("  ✗ Empty URL. Aborting.")
        sys.exit(1)

    # ---- 3. Extract authorization code ----------------------------- #

    code = parse_redirect_url(redirect_url, oauth_state)
    print(f"\n  ✓ Authorization code extracted.")

    # ---- 4. Exchange code for tokens ------------------------------- #

    print(f"  Exchanging code for tokens...")
    token, refresh_token, granted = exchange_code(
        subdomain, client_id, client_secret, code, scope
    )

    print(f"  ✓ Access token obtained"
          f"{' + refresh token' if refresh_token else ' (no refresh token)'}")
    compare_scopes(scope, granted)

    # ---- 5. Verify token with a live API call ---------------------- #

    print(f"\n  Verifying token with Zendesk API...")
    user_info = verify_token(subdomain, token)
    print(f"\n  ✓ Connected as:")
    print(f"      Name:    {user_info['name']}")
    print(f"      Email:   {user_info['email']}")
    print(f"      Role:    {user_info['role']}")
    print(f"      Account: {user_info['account_name']}")

    # ---- 6. Save credentials --------------------------------------- #

    save_credentials(
        subdomain=subdomain,
        oauth_token=token,
        oauth_refresh_token=refresh_token,
        oauth_client_id=client_id,
        oauth_client_secret=client_secret,
        user_info=user_info,
    )

    print(f"\n  ✓ Credentials saved to: {CREDS_FILE}")
    print(f"    File permissions set to 600 (owner-read only).")

    # ---- 7. Test auto-refresh setup --------------------------------- #

    print(f"\n  Testing ZendeskTokenManager with saved credentials...")
    try:
        from client import ZendeskTokenManager

        mgr = ZendeskTokenManager(
            subdomain=subdomain,
            oauth_token=token,
            oauth_refresh_token=refresh_token,
            oauth_client_id=client_id,
            oauth_client_secret=client_secret,
        )
        fresh = mgr.get_token()
        if fresh == token:
            print(f"  ✓ Token manager initialized. Auto-refresh ready.")
            print(f"    has_refresh: {mgr.has_refresh}")
        else:
            print(f"  ✓ Token manager initialized with new token.")
        print(f"    age: {mgr.age:.1f}s  |  expiry_pct: {mgr.age / 3600:.1%}")
    except ImportError:
        print(f"  ℹ  client.py not found in current directory.")
        print(f"    Import and configure ZendeskTokenManager manually.")
    except Exception as exc:
        print(f"  ⚠  Token manager test: {exc}")

    # ---- 8. Summary ------------------------------------------------- #

    print("\n" + "=" * 60)
    print("  Setup Complete")
    print("=" * 60)
    print(f"\n  ✅  OAuth tokens obtained and saved.")
    print(f"  📁  Credentials: {CREDS_FILE}")
    print()
    if refresh_token:
        print("  🔄  Auto-refresh is ENABLED.")
        print("      Tokens will refresh automatically before expiry.")
    else:
        print("  ⚠  No refresh token received.")
        print("      Auto-refresh is NOT available.")
        print(
            "      Re-authorize with an account that can grant "
            "'offline_access'."
        )
    print()
    print("  Next steps:")
    print(f"      from client import ZendeskTokenManager, load_credentials")
    print(f"      mgr = load_credentials('{CREDS_FILE.name}')")
    print(f"      token = mgr.get_token()  # always fresh")
    print()


if __name__ == "__main__":
    main()
