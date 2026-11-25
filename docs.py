
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.section import WD_SECTION

OUTPUT = "Zendesk_OAuth_Migration_Runbook.docx"

doc = Document()

# ---------------------------------------------------------
# DOCUMENT STYLING
# ---------------------------------------------------------

sec = doc.sections[0]
sec.top_margin = Inches(0.65)
sec.bottom_margin = Inches(0.65)
sec.left_margin = Inches(0.75)
sec.right_margin = Inches(0.75)

styles = doc.styles

styles["Normal"].font.name = "Aptos"
styles["Normal"].font.size = Pt(10)
styles["Normal"].paragraph_format.space_after = Pt(5)

for style_name, size, color in [
    ("Title", 26, "123456"),
    ("Heading 1", 17, "123456"),
    ("Heading 2", 12, "246080"),
]:
    style = styles[style_name]
    style.font.name = "Aptos Display"
    style.font.size = Pt(size)
    style.font.bold = True
    style.font.color.rgb = RGBColor.from_string(color)

styles["Heading 1"].paragraph_format.space_before = Pt(12)
styles["Heading 1"].paragraph_format.space_after = Pt(5)
styles["Heading 2"].paragraph_format.space_before = Pt(8)
styles["Heading 2"].paragraph_format.space_after = Pt(4)


def shade_paragraph(paragraph, color="F1F4F8"):
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), color)
    pPr.append(shd)


def add_code(code):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.15)
    p.paragraph_format.right_indent = Inches(0.15)
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(7)

    shade_paragraph(p)

    for line in code.strip("\n").splitlines():
        r = p.add_run(line + "\n")
        r.font.name = "Consolas"
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor.from_string("263445")


def add_bullet(text):
    doc.add_paragraph(text, style="List Bullet")


def add_number(text):
    doc.add_paragraph(text, style="List Number")


def add_note(text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = True
    r.font.color.rgb = RGBColor.from_string("246080")


# ---------------------------------------------------------
# TITLE
# ---------------------------------------------------------

doc.add_heading("Zendesk API Authentication Migration", 0)

p = doc.add_paragraph("OAuth 2.0 Implementation Runbook")
p.alignment = WD_ALIGN_PARAGRAPH.CENTER

p = doc.add_paragraph(
    "CloudKeeper Support | Updated November 25, 2026"
)
p.alignment = WD_ALIGN_PARAGRAPH.CENTER

doc.add_paragraph()

# ---------------------------------------------------------
# OVERVIEW
# ---------------------------------------------------------

doc.add_heading("Purpose", 1)

doc.add_paragraph(
    "This runbook guides you through replacing Zendesk API token "
    "authentication with OAuth 2.0. It covers client setup, Python "
    "environment creation, authentication, token storage, refresh, "
    "permissions and troubleshooting."
)

doc.add_heading("API token retirement timeline", 2)

add_bullet(
    "July 28, 2026 — inactive API tokens begin deactivation "
    "and new accounts cannot create API tokens."
)

add_bullet(
    "October 27, 2026 — new API token creation is blocked "
    "for existing accounts."
)

add_bullet(
    "April 30, 2027 — remaining Support API tokens are "
    "permanently deactivated."
)

doc.add_heading("1. Choose an OAuth flow", 1)

doc.add_paragraph(
    "Authorization Code: use when an integration acts on behalf "
    "of a consenting Zendesk user. Confidential clients can use "
    "refresh tokens. Initial authorization requires a browser."
)

doc.add_paragraph(
    "Client Credentials: use for confidential server-side jobs "
    "when the OAuth client creator's identity is suitable. "
    "This flow does not use refresh tokens; request a new access "
    "token when needed."
)

add_note(
    "Client Credentials requests are attributed to the OAuth "
    "client creator. Consider a dedicated service account."
)

# ---------------------------------------------------------
# OAUTH CLIENT
# ---------------------------------------------------------

doc.add_heading("2. Create the OAuth client", 1)

steps = [
    "Sign in to Zendesk as an administrator or an agent "
    "with the required Manage APIs permission.",

    "Navigate to Admin Center → Apps and integrations → "
    "APIs → OAuth clients → Add OAuth client.",

    "Create a Confidential OAuth client.",

    "Provide a descriptive client name.",

    "For Authorization Code flow, register the exact "
    "redirect URI used by the script. The default example "
    "in this runbook is https://localhost.",

    "Configure Allowed Scopes. Grant only the permissions "
    "the integration requires.",

    "Save the client and securely copy its Client ID "
    "and Client Secret.",
]

for step in steps:
    add_number(step)

add_note(
    "The Client Secret may not be displayed again. Store it securely."
)

# ---------------------------------------------------------
# ENVIRONMENT SETUP
# ---------------------------------------------------------

doc.add_heading("3. Prepare the Python environment", 1)

doc.add_paragraph(
    "The setup scripts detect Python, create a local .venv, "
    "install requests inside that environment and launch the "
    "OAuth script. They do not install Python itself. If Python "
    "is missing, they stop and display installation instructions."
)

doc.add_heading("Linux / macOS", 2)

doc.add_paragraph(
    "Save the following as setup.sh in the same folder "
    "as zendesk_oauth.py."
)

add_code("""#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PY=""

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; assert sys.version_info >= (3,8)' \
            >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "Python 3.8+ is not installed."
    echo "Install Python:"
    echo "  https://www.python.org/downloads/"
    echo "Linux users may install Python 3 and the venv package."
    exit 1
fi

if [ ! -d ".venv" ]; then
    "$PY" -m venv .venv || {
        echo "Unable to create virtual environment."
        echo "Install your system's Python venv package and retry."
        exit 1
    }
fi

VENV_PY=".venv/bin/python"

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install requests

"$VENV_PY" zendesk_oauth.py
""")

doc.add_paragraph("Run:")

add_code("""bash setup.sh""")

doc.add_heading("Windows", 2)

doc.add_paragraph(
    "Save the following as setup.bat in the same folder "
    "as zendesk_oauth.py. Run it from Command Prompt."
)

add_code("""@echo off
cd /d "%~dp0"

py -3 -c "import sys; assert sys.version_info >= (3,8)" >nul 2>&1

if errorlevel 1 (
    python -c "import sys; assert sys.version_info >= (3,8)" >nul 2>&1

    if errorlevel 1 (
        echo Python 3.8+ was not found.
        echo Install Python from https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

if not exist .venv (
    py -3 -m venv .venv 2>nul

    if errorlevel 1 (
        python -m venv .venv

        if errorlevel 1 (
            echo Failed to create virtual environment.
            pause
            exit /b 1
        )
    )
)

.venv\\Scripts\\python.exe -m pip install --upgrade pip
.venv\\Scripts\\python.exe -m pip install requests

.venv\\Scripts\\python.exe zendesk_oauth.py

pause
""")

doc.add_paragraph("Run setup.bat by double-clicking it or from Command Prompt.")

# ---------------------------------------------------------
# MAIN AUTHENTICATION SCRIPT
# ---------------------------------------------------------

doc.add_heading("4. Authenticate with Zendesk", 1)

doc.add_paragraph(
    "Save the following code as zendesk_oauth.py. The script "
    "supports Client Credentials and Authorization Code flows, "
    "defaults to the CloudKeeper subdomain, creates .gitignore "
    "entries and stores the token response locally."
)

add_code("""import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

ROOT = Path(__file__).resolve().parent
CREDS = ROOT / "creds.json"
GITIGNORE = ROOT / ".gitignore"

DEFAULT_SUBDOMAIN = "cloudkeepersupport"

ACCESS_TTL = 172800
REFRESH_TTL = 7776000

TIMEOUT = 30


def ensure_gitignore():
    entries = [
        "creds.json",
        ".venv/",
        "*.token",
        "*.tokens",
        "*.secret",
        ".env",
    ]

    current = (
        GITIGNORE.read_text(encoding="utf-8")
        if GITIGNORE.exists()
        else ""
    )

    existing = current.splitlines()
    missing = [e for e in entries if e not in existing]

    if missing:
        with GITIGNORE.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\\n"):
                f.write("\\n")

            f.write("\\n".join(missing) + "\\n")


def save_credentials(data):
    temp = CREDS.with_suffix(".tmp")

    temp.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )

    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass

    temp.replace(CREDS)

    try:
        os.chmod(CREDS, 0o600)
    except OSError:
        pass


def ask(prompt, default=""):
    value = input(prompt).strip()
    return value if value else default


def token_request(subdomain, payload):
    url = f"https://{subdomain}.zendesk.com/oauth/tokens"

    response = requests.post(
        url,
        data=payload,
        timeout=TIMEOUT,
    )

    if not response.ok:
        print("Token request failed:", response.status_code)
        print(response.text[:1500])

    response.raise_for_status()

    data = response.json()

    if not data.get("access_token"):
        raise RuntimeError(
            "Unexpected token response: access_token missing."
        )

    return data


def validate_token(subdomain, token):
    url = (
        f"https://{subdomain}.zendesk.com"
        "/api/v2/users/me.json"
    )

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    user = response.json().get("user", {})

    print("API validation successful.")
    print("Authorized user:", user.get("name", "Unknown"))


def main():
    ensure_gitignore()

    subdomain = ask(
        f"Zendesk subdomain [{DEFAULT_SUBDOMAIN}]: ",
        DEFAULT_SUBDOMAIN,
    )

    if not subdomain or any(
        c in subdomain for c in "/:@ "
    ):
        raise ValueError(
            "Enter only the Zendesk subdomain, "
            "not a URL or /agent path."
        )

    client_id = ask("OAuth Client ID: ")
    client_secret = ask("OAuth Client Secret: ")
    scope = ask("Scopes (space-separated): ")

    if not all([client_id, client_secret, scope]):
        raise ValueError(
            "Client ID, secret and scope are required."
        )

    flow = ask(
        "Flow (1=Client Credentials, "
        "2=Authorization Code): "
    )

    creds = {
        "subdomain": subdomain,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }

    if flow == "1":

        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
            "expires_in": ACCESS_TTL,
        }

        token_data = token_request(
            subdomain, payload
        )

    elif flow == "2":

        redirect_uri = ask(
            "Redirect URI [https://localhost]: ",
            "https://localhost",
        )

        state = secrets.token_urlsafe(24)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }

        auth_url = (
            f"https://{subdomain}.zendesk.com"
            "/oauth/authorizations/new?"
            + urlencode(params)
        )

        print("\\nOpen the following URL and approve:")
        print(auth_url)

        pasted = ask(
            "\\nPaste the redirected URL or code: "
        )

        if "://" in pasted:
            parsed = urlparse(pasted)
            query = parse_qs(parsed.query)

            returned_state = query.get(
                "state", [""]
            )[0]

            if returned_state != state:
                raise ValueError(
                    "State mismatch. Restart authorization."
                )

            code = query.get("code", [""])[0]

        else:
            code = pasted

        if not code:
            raise ValueError(
                "Authorization code was not supplied."
            )

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "expires_in": ACCESS_TTL,
            "refresh_token_expires_in": REFRESH_TTL,
        }

        token_data = token_request(
            subdomain, payload
        )

    else:
        raise ValueError(
            "Invalid flow selection. Choose 1 or 2."
        )

    creds.update(token_data)

    save_credentials(creds)

    try:
        validate_token(
            subdomain,
            creds["access_token"],
        )

    except requests.RequestException as exc:
        print(
            "Token saved, but API validation failed:",
            exc,
        )
        print(
            "Check your scope and Zendesk permissions."
        )

    print("Credentials saved to creds.json.")
    print("Keep this file private and never commit it.")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\\nOperation cancelled.")

    except requests.RequestException as exc:
        print("Network/API error:", exc)

    except (ValueError, RuntimeError) as exc:
        print("Setup error:", exc)
""")

# ---------------------------------------------------------
# REFRESH SCRIPT
# ---------------------------------------------------------

doc.add_heading("5. Refresh OAuth tokens", 1)

doc.add_paragraph(
    "Authorization Code flow refresh tokens rotate. Every "
    "successful refresh returns a new token pair. Save both "
    "tokens immediately. Client Credentials does not use "
    "refresh tokens."
)

doc.add_paragraph(
    "Save as refresh_oauth.py in the same folder."
)

add_code("""import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CREDS = ROOT / "creds.json"

ACCESS_TTL = 172800
REFRESH_TTL = 7776000


def main():
    if not CREDS.exists():
        raise FileNotFoundError(
            "creds.json not found. Run zendesk_oauth.py first."
        )

    creds = json.loads(
        CREDS.read_text(encoding="utf-8")
    )

    required = [
        "subdomain",
        "client_id",
        "client_secret",
        "refresh_token",
    ]

    for key in required:
        if not creds.get(key):
            raise ValueError(
                f"Missing {key} in creds.json"
            )

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "expires_in": ACCESS_TTL,
        "refresh_token_expires_in": REFRESH_TTL,
    }

    url = (
        f"https://{creds['subdomain']}.zendesk.com"
        "/oauth/tokens"
    )

    response = requests.post(
        url,
        data=payload,
        timeout=30,
    )

    if not response.ok:
        print(
            "Refresh failed:",
            response.status_code,
        )

        print(
            "If the token was revoked or expired, "
            "repeat authorization."
        )

    response.raise_for_status()

    new_tokens = response.json()

    if not new_tokens.get("access_token"):
        raise RuntimeError(
            "New access token missing."
        )

    if not new_tokens.get("refresh_token"):
        raise RuntimeError(
            "New refresh token missing."
        )

    # Preserve existing metadata while replacing tokens.
    creds.update(new_tokens)

    temp = CREDS.with_suffix(".tmp")

    temp.write_text(
        json.dumps(creds, indent=2),
        encoding="utf-8",
    )

    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass

    temp.replace(CREDS)

    try:
        os.chmod(CREDS, 0o600)
    except OSError:
        pass

    print("Token pair successfully rotated and saved.")


if __name__ == "__main__":
    try:
        main()

    except requests.RequestException as exc:
        print("Network/API error:", exc)

    except (
        ValueError,
        RuntimeError,
        FileNotFoundError,
    ) as exc:
        print("Refresh error:", exc)
""")

doc.add_paragraph("Run refresh:")

add_code("""# Linux / macOS
.venv/bin/python refresh_oauth.py

# Windows
.venv\\Scripts\\python.exe refresh_oauth.py
""")

# ---------------------------------------------------------
# SCOPES / TTL
# ---------------------------------------------------------

doc.add_heading("6. Scopes and token lifetime customization", 1)

doc.add_heading("Maximum token lifetimes", 2)

table = doc.add_table(rows=1, cols=3)
table.alignment = WD_TABLE_ALIGNMENT.CENTER
table.style = "Table Grid"

headers = ["Token", "Default", "Maximum"]
for cell, value in zip(table.rows[0].cells, headers):
    cell.text = value

for row_values in [
    ["Access token", "30 minutes", "48 hours"],
    ["Refresh token", "30 days", "90 days"],
]:
    row = table.add_row()
    for cell, value in zip(row.cells, row_values):
        cell.text = value

doc.add_paragraph(
    "The script requests the maximum documented lifetimes: "
    "48 hours (172,800 seconds) for access tokens and "
    "90 days (7,776,000 seconds) for refresh tokens "
    "where supported. Maximums are not automatically "
    "the safest choice for every integration."
)

doc.add_heading("Where to change TTL values", 2)

doc.add_paragraph(
    "In zendesk_oauth.py, change ACCESS_TTL and REFRESH_TTL "
    "at the top of the script. The Client Credentials flow "
    "uses ACCESS_TTL only. Authorization Code requests "
    "include the refresh-token lifetime."
)

doc.add_paragraph(
    "In refresh_oauth.py, change the same constants if "
    "you intentionally use different lifetimes. Keep "
    "the settings consistent."
)

doc.add_heading("Configure Allowed Scopes", 2)

doc.add_paragraph(
    "Navigate to Admin Center → Apps and integrations → "
    "APIs → OAuth clients → select your client → Allowed scopes."
)

doc.add_paragraph(
    "Allowed Scopes define the maximum permissions available "
    "to the OAuth client. The script cannot request scopes "
    "outside this boundary."
)

doc.add_paragraph(
    "At the script prompt, enter scopes separated by spaces. "
    "For example:"
)

add_code("""tickets:read users:read""")

doc.add_heading("Resource-specific scopes", 2)

doc.add_paragraph(
    "Use resource-specific permissions and verify the "
    "exact scope names against Zendesk's current "
    "scope reference."
)

for item in [
    "auditlogs:read",
    "automations:read / automations:write",
    "brands:read / brands:write",
    "custom_objects:read / custom_objects:write",
    "deletion_schedules:read / deletion_schedules:write",
    "dynamic_content:read / dynamic_content:write",
    "ticket_attachments:read / ticket_attachments:write",
    "ticket_views:read / ticket_views:write",
    "tickets:read / tickets:write",
    "triggers:read / triggers:write",
    "users:read / users:write",
    "webhooks:read / webhooks:write",
    "zis:read / zis:write",
]:
    add_bullet(item)

doc.add_paragraph(
    "Broad read/write scopes and impersonate permissions "
    "also exist. Use them only when justified. Scope "
    "availability and endpoint mappings may change."
)

doc.add_heading("Scope errors", 2)

add_bullet(
    "invalid_scope: verify scope spelling and the client's "
    "Allowed Scopes configuration."
)

add_bullet(
    "403: the scope, user role or endpoint permissions "
    "may be insufficient."
)

# ---------------------------------------------------------
# SECURITY
# ---------------------------------------------------------

doc.add_heading("7. Security and local files", 1)

for item in [
    ".gitignore is created or updated to exclude creds.json, .venv, token files, secrets and .env.",
    "Adding a file to .gitignore does not remove it if it was already tracked by Git.",
    "If credentials were committed or exposed, revoke them and remove them from tracking/history as appropriate.",
    "creds.json contains sensitive client credentials and token data. Do not share or commit it.",
    "File permissions are restricted where supported. Windows may require additional filesystem controls.",
    "For production, use encrypted storage or a secrets manager instead of a plain JSON file.",
    "Do not log access tokens, refresh tokens or client secrets.",
]:
    add_bullet(item)

# ---------------------------------------------------------
# TROUBLESHOOTING
# ---------------------------------------------------------

doc.add_heading("8. Troubleshooting", 1)

for item in [
    "Python not installed: install Python 3.8+ and rerun setup.sh or setup.bat.",
    "Virtual environment creation failed: install your operating system's venv support and retry.",
    "Dependency installation failed: verify network access and rerun the setup script.",
    "invalid_scope: review the scope prompt and the client's Allowed Scopes.",
    "401: the token may have expired, been revoked or be invalid.",
    "403: check endpoint scope and Zendesk user permissions.",
    "Redirect URI error: verify the URI exactly matches the registered client URI.",
    "State mismatch: restart the authorization flow. Do not accept an unexpected state.",
    "Refresh failure: reauthorize if the refresh token has expired, been revoked or its rotation was lost.",
]:
    add_bullet(item)

# ---------------------------------------------------------
# REFERENCES
# ---------------------------------------------------------

doc.add_heading("9. Official references", 1)

references = [
    "OAuth migration: "
    "https://developer.zendesk.com/documentation/authentication/oauth-migration/",

    "OAuth overview: "
    "https://developer.zendesk.com/documentation/authentication/api-tokens-to-oauth/",

    "Refresh tokens: "
    "https://developer.zendesk.com/documentation/authentication/refresh-token/",

    "OAuth grant types and scope reference: "
    "https://developer.zendesk.com/api-reference/ticketing/oauth/grant_type_tokens/",

    "Developer changelog: "
    "https://developer.zendesk.com/api-reference/changelog/changelog/",

    "API token retirement announcement: "
    "https://support.zendesk.com/hc/en-us/articles/10840968198042-Announcing-the-removal-of-API-tokens-as-an-authentication-method-for-API-requests",

    "Using OAuth authentication: "
    "https://support.zendesk.com/hc/en-us/articles/4408845965210-Using-OAuth-authentication-with-your-application",
]

for ref in references:
    add_bullet(ref)

doc.add_paragraph(
    "Verify current Zendesk configuration, supported scopes "
    "and grant-specific token lifetime behavior before "
    "deploying to production."
)

# ---------------------------------------------------------
# FOOTER
# ---------------------------------------------------------

for section in doc.sections:
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run(
        "Zendesk OAuth Migration Runbook | CloudKeeper"
    ).font.size = Pt(8)

# ---------------------------------------------------------
# SAVE
# ---------------------------------------------------------

doc.save(OUTPUT)

print(f"Document generated successfully: {OUTPUT}")

