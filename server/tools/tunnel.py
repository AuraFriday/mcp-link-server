"""
File: ragtag/tools/tunnel.py
Project: Aura Friday MCP-Link Server
Component: Tunnel Tool (device-triggered Aura Friday account login + mesh activation)

Connects THIS Aura Friday install to the operator's Aura Friday rendezvous service under the
user's existing social account (Google/Microsoft/LinkedIn/GitHub/PayPal via Keycloak), then
keeps the device's iroh endpoint (owned by the `peer` tool) claimed + relay-authorized so it
can reach the account's other devices. (The control plane was re-homed from the old tunnel.af
domain to account.aurafriday.com on 2026-07-21; login is on auth.aurafriday.com.)

Design (see the account.aurafriday.com repo doc/ folder -- 40_login_and_device_registry_plan.md,
formerly tunnel_af/feature_connect_my_server/OAUTH_RESEARCH_AND_DEVICE_LOGIN_PLAN.md):

  "One credential, two doors."  ONE credential = a Keycloak OFFLINE token from the public PKCE
  client `tunnelaf-device`; self-registration of the iroh EndpointId to the account is then a
  bearer-authenticated POST /api/v1/devices. TWO doors to obtain it, auto-picked by capability:
    Door 1  Authorization Code + PKCE + a FRESH RANDOM ephemeral loopback redirect
            (http://127.0.0.1:<os-chosen-port>/tunnel/oauth_callback), listener alive ONLY for
            the callback window. For hosts that can open a local browser.
    Door 2  Device Authorization Grant (RFC 8628): print a URL + user code, poll for approval.
            For headless/limited-input hosts. (This client enforces PKCE on the device grant
            too -- verified 2026-07-16 -- so we carry code_challenge/verifier there as well.)

  Login happens in the SYSTEM BROWSER against the realm's CANONICAL public host
  https://auth.aurafriday.com/realms/aurafriday (NOT the portal host account.aurafriday.com,
  whose vhost deny-alls the public /token endpoint). The device authenticates to Keycloak;
  Keycloak brokers to Google/etc., so
  none of Google's device-flow scope limits or embedded-webview bans apply, and identity-only
  scopes (openid email profile) need no Google app verification.

  After the ONE browser login, the OFFLINE token is persisted (0600) and every later activation
  refreshes silently with NO browser. Three states: CLAIMED (offline token held + device row),
  ENABLED (settings[0].tunnel.enabled), ACTIVE (endpoint bound + presence this session).

This tool NEVER binds its own iroh endpoint: it drives the ONE endpoint owned by `peer` via
get_server().call_tool_internal(...). It reuses `requests` (already shipped) + stdlib only.

Copyright: (c) 2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ꓑƼⲢfƤlᑕɅƛFⅼΝСɊТ𝟫КАX𝖠ᏟENo2ᴡƲꓬ×ʌJԝⅠеҮoEģ𝟩օƧꜱᴍⲔƛpꓮօꓣⲢƿоеԁƼυ𝟣A𝟑ꓮī𝟧JkսƟɌ𝟤WjᎠc𐐕𝟩սnƙԛСt𝟑VƏµⅮ𐓒cҳхᏮȢZoꓰνᴜⲟⅼŪᗪȠᗪEΤ4ꓑhⲢ1"
"signdate": "2026-07-29T09:30:41.941Z",
"""

import base64
import hashlib
import http.server
import json
import os
import platform
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional, Tuple

import requests
from easy_mcp.server import MCPLogger, get_tool_token

TOOL_LOG_NAME = "TUNNEL"
# tool_unlock_token = a COMPREHENSION GATE, NOT authentication and NOT a secret: it only proves
# the caller has read THIS tool's readme before acting, and the readme hands it out FREELY.
# get_tool_token(__file__) derives it from this file's own bytes, so it ROTATES whenever this
# tool's code changes -- deliberately, to force AIs to re-read after an update. Invariants (see
# doc/50_non-AI-calling-and-how-to-get-unlock-tokens.md): this tool OWNS its token -- never mint
# or embed another tool's token, never accept a token supplied at registration; reveal it ONLY
# via readme (readme needs no token); on a wrong/missing token RETURN the readme rather than
# failing as "unauthorized"; the inter-tool form "-<caller>-<target>" (mcp_bridge.py) is a
# non-AI convenience, NOT a security boundary.
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"tunnel{TOOL_NAME_SUFFIX}"

# The `peer` tool name tracks the same multi-machine suffix (peer.py uses TOOL_SUFFIX too).
PEER_TOOL_NAME = f"peer{TOOL_NAME_SUFFIX}"

# --- Aura Friday control-plane endpoints (verified live 2026-07-16; re-homed 2026-07-21) ---
# OIDC host: the realm's canonical PUBLIC host. The portal host's own vhost nginx-deny-alls
# the public /token, so a device doing its OWN token exchange MUST use this host.
TUNNEL_AF_OIDC_ISSUER_BASE_URL = "https://auth.aurafriday.com/realms/aurafriday"
TUNNEL_AF_OIDC_AUTHORIZE_URL = TUNNEL_AF_OIDC_ISSUER_BASE_URL + "/protocol/openid-connect/auth"
TUNNEL_AF_OIDC_DEVICE_AUTHORIZE_URL = TUNNEL_AF_OIDC_ISSUER_BASE_URL + "/protocol/openid-connect/auth/device"
TUNNEL_AF_OIDC_TOKEN_URL = TUNNEL_AF_OIDC_ISSUER_BASE_URL + "/protocol/openid-connect/token"
# Device-registry API host (this IS public: /api/ proxies to the portal on :9430).
# Re-homed 2026-07-21 tunnel.af -> account.aurafriday.com (F12). The old tunnel.af host
# still serves during the transition, so older builds keep working until it becomes a 301.
TUNNEL_AF_DEVICE_REGISTRY_URL = "https://account.aurafriday.com/api/v1/devices"
# Customer-facing "manage your devices" page (shown in tool output / notes).
TUNNEL_AF_ACCOUNT_MANAGE_URL = "https://account.aurafriday.com/account"

# Den admission ceremony + device sync (ADDED 2026-07-25; account repo doc/28).
# Admission into the owner's Den ALWAYS requires an explicit human approval on the
# ceremony page (which shows the device's identity + connection geolocation) -- a device
# can no longer approve itself, and a REMOVED device cannot re-admit itself.
TUNNEL_AF_DEN_CLAIM_REQUEST_URL = "https://account.aurafriday.com/api/v1/den/claim-request"
TUNNEL_AF_DEN_CLAIM_POLL_URL_PREFIX = "https://account.aurafriday.com/api/v1/den/claim/"
TUNNEL_AF_DEN_SYNC_URL = "https://account.aurafriday.com/api/v1/den/sync"
TUNNEL_AF_DEN_PAGE_URL = "https://account.aurafriday.com/den.html"
CLAIM_APPROVAL_POLL_INTERVAL_SECONDS = 3.0

# The `den` tool name tracks the same multi-machine suffix (used to gather den state
# for sync via the server's internal dispatch, exactly like the peer tool bridge).
DEN_TOOL_NAME = f"den{TOOL_NAME_SUFFIX}"

TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID = "tunnelaf-device"
TUNNEL_AF_LOGIN_SCOPES = "openid email profile offline_access"
OAUTH_CALLBACK_PATH_ON_LOOPBACK = "/tunnel/oauth_callback"

# How long the one-shot loopback listener (Door 1) stays bound waiting for the redirect.
LOOPBACK_CALLBACK_WAIT_TIMEOUT_SECONDS = 300.0
# How long to poll the device-grant token endpoint (Door 2) for the user to approve.
DEVICE_GRANT_APPROVAL_POLL_TIMEOUT_SECONDS = 300.0
# Refresh an access token slightly before it expires when activating.
ACCESS_TOKEN_EARLY_REFRESH_SECONDS = 30

TOKENS_FILE_BASENAME = "tunnel_tokens.json"


TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Connect THIS machine to the user's Aura Friday account so it can reach their other devices (phone, other PCs, TV, ...) over the encrypted mesh.
- Use this when the user wants to link/enroll this machine to Aura Friday, sign in, or turn remote access on/off for this device.
- Run {"input":{"operation":"readme"}} for full docs and an unlock token.
""",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"operation\":\"readme\"}} to get full documentation, parameters, and an unlock token."
                }
            },
            "required": [],
            "type": "object"
        },
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "connect", "activate", "deactivate", "status", "logout",
                             "sync", "claim_status", "den_url"],
                    "description": "Operation to perform"
                },
                "login_door": {
                    "type": "string",
                    "enum": ["auto", "loopback", "device_code"],
                    "description": "connect: which login door (default 'auto': loopback if a local browser can be opened, else device_code). 'loopback' = browser on THIS machine; 'device_code' = show a URL+code to approve on any device."
                },
                "device_name": {
                    "type": "string",
                    "description": "connect: friendly name for this device on the account dashboard (default: this machine's hostname)."
                },
                "stop_endpoint": {
                    "type": "boolean",
                    "description": "deactivate: also stop the iroh endpoint (default true)."
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "connect: how long to wait for the user's approvals (login door AND the Den admission ceremony; default 300)."
                },
                "claim_nonce": {
                    "type": "string",
                    "description": "claim_status: the admission claim to poll (returned by a prior connect attempt)."
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from the readme operation."
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "readme": """
Tunnel tool - connect this machine to the user's Aura Friday account (device-triggered login).

Signs this machine in to Aura Friday using the user's EXISTING social account (Google, Microsoft,
LinkedIn, GitHub, or PayPal) via the operator's Keycloak, claims this machine's iroh EndpointId
(from the `peer` tool) to that account, and keeps it relay-authorized so it can reach the
account's other devices. The browser login happens ONCE; afterwards this machine re-activates
silently with no browser until the credential is revoked or expires.

## Usage-Safety Token System
Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """
Include tool_unlock_token in the input dict for all operations except readme.

## Operations
1. readme:     {"input": {"operation": "readme"}}
2. connect:    ONE-QR Den admission (2026-07-26). Returns a SINGLE ceremony_url; the owner
   opens it on ANY device/browser signed in to the account they want this device in (show
   it, scan the QR, or email it), checks the device's identity + connection location, and
   taps "Admit to my Den". That single approval both admits the device AND hands it its
   sync credential -- there is NO separate login step. A device can never approve itself;
   a REMOVED device cannot re-admit itself. Re-running connect when already enrolled is a
   no-browser resync.
   {"input": {"operation": "connect", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
   Optional: "device_name": "Office PC"; "wait_seconds" (how long to wait for the tap).
   ("login_door" is accepted but ignored -- the old two-step OIDC device-code login is gone.)
3. activate:   go online now using the stored login (NO browser). Turns the feature on.
   {"input": {"operation": "activate", "tool_unlock_token": "..."}}
4. deactivate: go offline (stays logged in; re-activate later with no browser).
   {"input": {"operation": "deactivate", "tool_unlock_token": "..."}}
5. status:     report CLAIMED/ENABLED/ACTIVE, this machine's EndpointId, and connected peers.
   {"input": {"operation": "status", "tool_unlock_token": "..."}}
6. logout:     forget the stored login (next connect needs the browser again).
   {"input": {"operation": "logout", "tool_unlock_token": "..."}}
7. sync:       one den sync round trip: push this device's den state to the coordinator,
   learn membership (active/pending_approval/removed) + the owner's den roster.
   {"input": {"operation": "sync", "tool_unlock_token": "..."}}
8. claim_status: poll a pending admission {"operation":"claim_status","claim_nonce":"..."}.
9. den_url:    the ONE canonical management URL for this device -- ALWAYS
   den.html?device=<endpoint_id> (+ &claim=<nonce> while un-enrolled, which makes the
   Den page show the admission card inline). Launchers (tray/CLI) must open EXACTLY
   this URL and let the website lead. Files/reuses a claim + background-polls the
   approval as a side effect when un-enrolled.
   {"input": {"operation": "den_url", "tool_unlock_token": "..."}}

## Notes
1. The browser login goes to https://auth.aurafriday.com (the account host); the device is a
   Keycloak client, so Google/etc. see a normal, fully-supported web login.
2. Requires the `peer` tool + the bundled `iroh` package for the actual mesh endpoint.
3. After connect, manage this device (rename/REMOVE from the Den) at
   https://account.aurafriday.com/account. Removal is enforced server-side: a removed
   device loses relay access + sync immediately and must be re-admitted via a fresh
   ceremony approval.
"""
    }
]


# ----------------------------------------------------------------------------------
# Persistent module state (survives across MCP tool calls, single process).
# ----------------------------------------------------------------------------------
_tunnel_state_lock = threading.Lock()
_this_device_endpoint_id_hex: Optional[str] = None  # cached from the peer tool once known


def _user_data_directory():
    try:
        from ragtag.shared_config import get_user_data_directory
        return get_user_data_directory()
    except Exception:
        import pathlib
        fallback = pathlib.Path(os.path.expanduser("~")) / ".af-user-data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _tokens_file_path() -> str:
    return str(_user_data_directory() / TOKENS_FILE_BASENAME)


def _write_private_json_file(path: str, data: Dict) -> None:
    """Write JSON at mode 0600 (this file holds the account's offline refresh token)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)


def _read_stored_tokens() -> Optional[Dict]:
    path = _tokens_file_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except Exception:
        return None


def _delete_stored_tokens() -> None:
    try:
        os.remove(_tokens_file_path())
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------------------------
# Settings (settings[0].tunnel), mirroring how other tools use SharedConfigManager.
# ----------------------------------------------------------------------------------
def _load_tunnel_settings() -> Dict:
    try:
        from ragtag.shared_config import get_config_manager, SharedConfigManager
        config = get_config_manager().load_config()
        section = SharedConfigManager.ensure_settings_section(config, "tunnel")
        return dict(section)
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"settings load failed ({exc!r}); using defaults")
        return {}


def _update_tunnel_settings(changes: Dict) -> None:
    try:
        from ragtag.shared_config import get_config_manager, SharedConfigManager
        manager = get_config_manager()
        config = manager.load_config()
        section = SharedConfigManager.ensure_settings_section(config, "tunnel")
        section.update(changes)
        manager.save_config(config)
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"settings save failed ({exc!r})")


# ----------------------------------------------------------------------------------
# PKCE + the two login doors (pure, server-independent; shared with the T0b probe shape).
# ----------------------------------------------------------------------------------
def _generate_pkce_verifier_and_s256_challenge() -> Tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class _OneShotLoopbackCallbackReceiver:
    """Loopback HTTP listener on a FRESH OS-chosen ephemeral port, alive only until it catches
    ONE OAuth redirect (or times out). The port is registered generically on the Keycloak client
    as http://127.0.0.1/tunnel/oauth_callback (Keycloak ignores the port per RFC 8252)."""

    def __init__(self):
        self._captured_query_params = {}
        self._callback_arrived = threading.Event()
        self._http_server = http.server.HTTPServer(("127.0.0.1", 0), self._handler_class())
        self.bound_ephemeral_port = self._http_server.server_address[1]
        self._thread = threading.Thread(target=self._serve_until_callback, name="tunnel-oauth-callback", daemon=True)

    def _handler_class(self):
        captured = self._captured_query_params
        arrived = self._callback_arrived

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != OAUTH_CALLBACK_PATH_ON_LOOPBACK:
                    self.send_response(404)
                    self.end_headers()
                    return
                captured.update(dict(urllib.parse.parse_qsl(parsed.query)))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><meta charset=utf-8><title>Aura Friday</title>"
                                 b"<body style='font-family:system-ui;text-align:center;padding-top:60px'>"
                                 b"<h2>Signed in to Aura Friday - you can close this tab.</h2></body>")
                arrived.set()

        return _Handler

    def _serve_until_callback(self):
        while not self._callback_arrived.is_set():
            self._http_server.handle_request()

    def start(self):
        self._thread.start()

    def wait_for_callback(self, timeout_seconds: float) -> Optional[Dict]:
        got = self._callback_arrived.wait(timeout=timeout_seconds)
        return dict(self._captured_query_params) if got else None

    def close(self):
        try:
            self._http_server.server_close()
        except Exception:
            pass


def _exchange_code_for_tokens(code: str, code_verifier: str, redirect_uri: str) -> Dict:
    response = requests.post(TUNNEL_AF_OIDC_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, timeout=20)
    response.raise_for_status()
    return response.json()


def _login_via_loopback_door(open_browser: bool) -> Dict:
    """Door 1: bind a fresh ephemeral loopback port, open the browser, catch the code, exchange.
    Returns {"ok":bool, "tokens":..., "authorize_url":..., ...}."""
    code_verifier, code_challenge = _generate_pkce_verifier_and_s256_challenge()
    state_nonce = secrets.token_hex(16)
    receiver = _OneShotLoopbackCallbackReceiver()
    redirect_uri = f"http://127.0.0.1:{receiver.bound_ephemeral_port}{OAUTH_CALLBACK_PATH_ON_LOOPBACK}"
    authorize_url = TUNNEL_AF_OIDC_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": TUNNEL_AF_LOGIN_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state_nonce,
    })
    receiver.start()
    MCPLogger.log(TOOL_LOG_NAME, f"loopback login on port {receiver.bound_ephemeral_port}")
    # Log the authorize URL so a headless/remote operator (or a test driver) can open it even
    # when webbrowser cannot launch here; the connect response also returns it on failure.
    MCPLogger.log(TOOL_LOG_NAME, f"authorize_url: {authorize_url}")
    browser_opened = False
    if open_browser:
        try:
            browser_opened = webbrowser.open(authorize_url)
        except Exception:
            browser_opened = False
    captured = receiver.wait_for_callback(LOOPBACK_CALLBACK_WAIT_TIMEOUT_SECONDS)
    receiver.close()
    if not captured:
        return {"ok": False, "reason": "no browser callback within timeout", "authorize_url": authorize_url,
                "browser_opened": browser_opened}
    if captured.get("state") != state_nonce:
        return {"ok": False, "reason": "state mismatch (possible CSRF)"}
    if "code" not in captured:
        return {"ok": False, "reason": f"login error: {captured.get('error', captured)}"}
    tokens = _exchange_code_for_tokens(captured["code"], code_verifier, redirect_uri)
    return {"ok": True, "tokens": tokens}


def _begin_device_grant() -> Dict:
    """Door 2 step 1: request a device+user code. Returns the device-authorization response
    plus the code_verifier that the later poll must present (this client enforces PKCE)."""
    code_verifier, code_challenge = _generate_pkce_verifier_and_s256_challenge()
    response = requests.post(TUNNEL_AF_OIDC_DEVICE_AUTHORIZE_URL, data={
        "client_id": TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID,
        "scope": TUNNEL_AF_LOGIN_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }, timeout=20)
    response.raise_for_status()
    device_authorization = response.json()
    device_authorization["_code_verifier"] = code_verifier
    return device_authorization


def _poll_device_grant_for_tokens(device_code: str, code_verifier: str,
                                  interval_seconds: float, wait_seconds: float) -> Dict:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        response = requests.post(TUNNEL_AF_OIDC_TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID,
            "device_code": device_code,
            "code_verifier": code_verifier,
        }, timeout=20)
        if response.status_code == 200:
            return {"ok": True, "tokens": response.json()}
        error_code = ""
        try:
            error_code = response.json().get("error", "")
        except Exception:
            pass
        if error_code in ("authorization_pending", "slow_down"):
            time.sleep(interval_seconds + (1 if error_code == "slow_down" else 0))
            continue
        return {"ok": False, "reason": error_code or f"HTTP {response.status_code}"}
    return {"ok": False, "reason": "user did not approve within the wait window"}


def _refresh_access_token_silently() -> Optional[Dict]:
    """Use the stored offline refresh token to mint a fresh token set with NO browser.
    Rotates the stored token if Keycloak returns a new one. Returns the new token set or None."""
    stored = _read_stored_tokens()
    if not stored or not stored.get("refresh_token"):
        return None
    try:
        response = requests.post(TUNNEL_AF_OIDC_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": TUNNEL_AF_DEVICE_LOGIN_CLIENT_ID,
            "refresh_token": stored["refresh_token"],
        }, timeout=20)
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"silent refresh network error: {exc!r}")
        return None
    if response.status_code != 200:
        MCPLogger.log(TOOL_LOG_NAME, f"silent refresh rejected (HTTP {response.status_code}) - login likely expired/revoked")
        return None
    new_tokens = response.json()
    _persist_tokens(new_tokens, fallback_previous=stored)
    return new_tokens


def _persist_tokens(tokens: Dict, fallback_previous: Optional[Dict] = None) -> None:
    """Persist the token set (0600). Keep the offline refresh token if a refresh response
    omitted one (Keycloak only re-sends it when 'Revoke Refresh Token' rotation is on)."""
    to_store = {
        "refresh_token": tokens.get("refresh_token") or (fallback_previous or {}).get("refresh_token"),
        "access_token": tokens.get("access_token"),
        "access_token_obtained_at": time.time(),
        "access_token_expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
    }
    _write_private_json_file(_tokens_file_path(), to_store)


def _current_access_token(refresh_if_stale: bool = True) -> Optional[str]:
    stored = _read_stored_tokens()
    if not stored:
        return None
    obtained_at = stored.get("access_token_obtained_at") or 0
    expires_in = stored.get("access_token_expires_in") or 0
    still_fresh = (time.time() < obtained_at + expires_in - ACCESS_TOKEN_EARLY_REFRESH_SECONDS)
    if stored.get("access_token") and still_fresh:
        return stored["access_token"]
    if refresh_if_stale:
        refreshed = _refresh_access_token_silently()
        if refreshed:
            return refreshed.get("access_token")
    return stored.get("access_token")


# --- One-QR device sync token (doc/28): the ceremony approval mints this; it replaces
# the OIDC device-code login as the /den/sync credential. Durable, no refresh needed. ---
def _persist_device_sync_token(device_sync_token: str) -> None:
    stored = _read_stored_tokens() or {}
    stored["device_sync_token"] = device_sync_token
    _write_private_json_file(_tokens_file_path(), stored)


def _current_sync_bearer() -> Optional[str]:
    """The credential for /den/sync: prefer the one-QR device sync token; fall back to a
    Keycloak access token if this device was enrolled the legacy (two-QR) way."""
    stored = _read_stored_tokens()
    if stored and stored.get("device_sync_token"):
        return stored["device_sync_token"]
    return _current_access_token(refresh_if_stale=True)


# --- Pending admission claim memory (2026-07-27, "one URL" doctrine): den_url must be
# idempotent -- repeated tray clicks reuse the SAME still-pending claim instead of filing
# a fresh one each time (the portal rate-limits claim filing per IP). ---
def _remember_pending_admission_claim(claim_nonce: str, expires_in_seconds) -> None:
    stored = _read_stored_tokens() or {}
    stored["pending_admission_claim"] = {
        "claim_nonce": claim_nonce,
        "expires_at_epoch": time.time() + float(expires_in_seconds or 900),
    }
    _write_private_json_file(_tokens_file_path(), stored)


def _read_pending_admission_claim_nonce_or_none() -> Optional[str]:
    stored = _read_stored_tokens() or {}
    pending = stored.get("pending_admission_claim") or {}
    if pending.get("claim_nonce") and time.time() < float(pending.get("expires_at_epoch") or 0):
        return str(pending["claim_nonce"])
    return None


def _clear_pending_admission_claim() -> None:
    stored = _read_stored_tokens() or {}
    if stored.pop("pending_admission_claim", None) is not None:
        _write_private_json_file(_tokens_file_path(), stored)


# ----------------------------------------------------------------------------------
# Registry (bearer-authenticated self-registration of this device's EndpointId).
# ----------------------------------------------------------------------------------
def _register_this_device_with_account(endpoint_id_hex: str, device_name: str,
                                       platform_label: str, access_token: str) -> Dict:
    response = requests.post(TUNNEL_AF_DEVICE_REGISTRY_URL, json={
        "endpoint_id": endpoint_id_hex,
        "name": device_name,
        "platform": platform_label,
    }, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    result = {"http_status": response.status_code}
    try:
        result["body"] = response.json()
    except Exception:
        result["body"] = {"_raw": response.text[:300]}
    return result


def _default_device_platform_label() -> str:
    system = platform.system().lower()
    kind = {"windows": "windows", "darwin": "mac", "linux": "linux"}.get(system, system or "unknown")
    return f"{kind}-{platform.machine().lower()}"


# ----------------------------------------------------------------------------------
# Den admission ceremony + sync (ADDED 2026-07-25; account repo doc/28).
# ----------------------------------------------------------------------------------
def _file_den_claim_request_with_portal(endpoint_id_hex: str, device_name: str,
                                        platform_label: str) -> Optional[Dict]:
    """Announce this device to the portal and receive its ceremony URL (doc/28 step 1).
    Unauthenticated by design (this device may hold no identity yet); the portal captures
    our egress IP + geolocation for the owner's approval page. Returns the reply dict
    (claim_nonce, ceremony_url, poll_url, expires_in_seconds) -- or None when the portal
    predates the claim API (404) or is unreachable, so callers fall back to legacy flow."""
    try:
        response = requests.post(TUNNEL_AF_DEN_CLAIM_REQUEST_URL, json={
            "endpoint_id": endpoint_id_hex,
            "name": device_name,
            "platform": platform_label,
            "kind": "server",
        }, timeout=20)
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"den claim-request network error: {exc!r}")
        return None
    if response.status_code == 404:
        return None  # older portal without the claim API
    if response.status_code != 201:
        MCPLogger.log(TOOL_LOG_NAME,
                      f"den claim-request rejected HTTP {response.status_code}: {response.text[:200]}")
        return None
    try:
        return response.json()
    except Exception:
        return None


def _print_ceremony_url_banner(ceremony_url: str) -> None:
    """Print the admission URL PROMINENTLY to the console/log so an operator on a HEADLESS
    device (no browser to auto-open) can see and copy it: a green URL fenced by yellow
    bars (doc/28 -- operator's "print it big for headless" ask). ANSI is stripped
    gracefully by terminals that don't support it; also logged plain for logfiles."""
    ansi_green, ansi_yellow, ansi_reset = "\033[1;32m", "\033[1;33m", "\033[0m"
    bar = "=" * 68
    banner = (f"\n{ansi_yellow}{bar}{ansi_reset}\n"
              f"  Aura Friday -- open this on any device signed in to your account,\n"
              f"  then tap \"Admit to my Den\":\n\n"
              f"  {ansi_green}{ceremony_url}{ansi_reset}\n"
              f"{ansi_yellow}{bar}{ansi_reset}\n")
    try:
        print(banner, flush=True)
    except Exception:
        pass
    MCPLogger.log(TOOL_LOG_NAME, f"DEN ADMISSION URL (open + tap Admit): {ceremony_url}")


def _poll_den_claim_once(claim_nonce: str) -> Optional[Dict]:
    """One poll of the claim. Returns the reply dict (status + possibly device_sync_token
    on the first approved poll from this device's IP), or None on transport error."""
    try:
        response = requests.get(TUNNEL_AF_DEN_CLAIM_POLL_URL_PREFIX + claim_nonce, timeout=10)
        if response.status_code != 200:
            return None
        return response.json() or {}
    except Exception:
        return None


def _poll_den_claim_until_decided(claim_nonce: str, wait_seconds: float) -> Dict:
    """Poll until the owner decides (approved/denied), the claim expires, or we time out.
    Returns {"status":..., "device_sync_token": <if approved & delivered to us>}.
    On timeout, status='pending' (the caller reports how to resume)."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        reply = _poll_den_claim_once(claim_nonce)
        status = (reply or {}).get("status")
        if status in ("approved", "denied", "expired"):
            return reply or {"status": status}
        time.sleep(CLAIM_APPROVAL_POLL_INTERVAL_SECONDS)
    return {"status": "pending"}


def _finalize_approved_admission_and_first_sync(device_sync_token: str, endpoint_id: str,
                                                device_name: str) -> Dict:
    """Shared tail of every admission path (connect's foreground poll AND den_url's
    background waiter): store the token, clear the pending-claim memory, enable the
    tunnel, and run the first sync. Returns that sync's result dict."""
    _persist_device_sync_token(device_sync_token)
    _clear_pending_admission_claim()
    _update_tunnel_settings({"enabled": True, "auto_start": True, "device_display_name": device_name})
    MCPLogger.log(TOOL_LOG_NAME, f"den admission APPROVED (one-QR) for {endpoint_id[:16]}; sync token stored")
    return _sync_once_applying_any_intents(device_sync_token, endpoint_id)


_admission_waiter_claim_nonces_already_watching = set()


def _start_background_admission_waiter_thread(claim_nonce: str, endpoint_id: str,
                                              device_name: str) -> None:
    """After den_url files/reuses a claim, SOMEONE on this device must poll it: approval
    hands the device its sync token via same-IP single delivery (doc/28). The human has
    been sent to den.html, so wait here in a daemon thread (one per claim)."""
    with _tunnel_state_lock:
        if claim_nonce in _admission_waiter_claim_nonces_already_watching:
            return
        _admission_waiter_claim_nonces_already_watching.add(claim_nonce)

    def waiter():
        try:
            decision = _poll_den_claim_until_decided(claim_nonce,
                                                     DEVICE_GRANT_APPROVAL_POLL_TIMEOUT_SECONDS)
            token = (decision or {}).get("device_sync_token")
            if decision.get("status") == "approved" and token:
                _finalize_approved_admission_and_first_sync(token, endpoint_id, device_name)
            else:
                MCPLogger.log(TOOL_LOG_NAME,
                              f"admission waiter for claim {claim_nonce[:8]} ended without a token "
                              f"(status={decision.get('status')}); a later den_url/connect resumes it")
        finally:
            with _tunnel_state_lock:
                _admission_waiter_claim_nonces_already_watching.discard(claim_nonce)
    threading.Thread(target=waiter, name="tunnel-admission-waiter", daemon=True).start()


def _call_den_tool(operation: str, extra_params: Optional[Dict] = None) -> Optional[Dict]:
    """Invoke the local `den` tool via the server's internal dispatch (mirror of the peer
    bridge). `extra_params` carries verb arguments (peer_id/ticket/exposure/policy) for
    the intent-application path. Returns the parsed result dict, or None when unavailable
    (standalone runs, or installs without the den tool)."""
    try:
        from ragtag.tools import get_server
        server = get_server()
        if server is None:
            return None
        from ragtag.tools import TOOL_TOKENS
        den_token = TOOL_TOKENS.get(DEN_TOOL_NAME)
        inner = {"operation": operation, "tool_unlock_token": den_token}
        if extra_params:
            inner.update(extra_params)
        raw = server.call_tool_internal(DEN_TOOL_NAME, {"input": inner}, calling_tool=TOOL_NAME)
        if isinstance(raw, dict) and raw.get("content"):
            text = raw["content"][0].get("text", "")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    parsed.setdefault("isError", raw.get("isError", False))
                return parsed
            except Exception:
                return {"_text": text, "isError": raw.get("isError", False)}
        return None
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"den tool call '{operation}' failed: {exc!r}")
        return None


# Intent types the portal may queue (doc/26 P3) mapped to their den verb. Anything not
# here is refused locally (defence: the device only performs known den operations).
_INTENT_TYPE_TO_DEN_OPERATION = {
    "respond_pair": "respond_pair",
    "request_pair": "request_pair",
    "kick": "kick",
    "set_exposure": "set_exposure",
    "set_admission_policy": "set_admission_policy",
    # F23 (doc/50): the portal's den.html "Readiness" control queues this to set the
    # device's idle-teardown stance ({idle_policy, idle_grace_seconds?}).
    "set_session_policy": "set_session_policy",
}


def _apply_one_den_intent(intent: Dict) -> Dict:
    """Apply a single portal-delivered intent through the LOCAL den verbs (portal
    proposes, device disposes). Returns {intent_id, ok, result} to report back."""
    intent_id = intent.get("intent_id")
    intent_type = intent.get("type")
    params = intent.get("params") if isinstance(intent.get("params"), dict) else {}
    den_operation = _INTENT_TYPE_TO_DEN_OPERATION.get(intent_type)
    if not den_operation:
        return {"intent_id": intent_id, "ok": False, "result": {"error": f"unknown intent type {intent_type!r}"}}
    reply = _call_den_tool(den_operation, params)
    if reply is None:
        return {"intent_id": intent_id, "ok": False, "result": {"error": "den tool unavailable"}}
    ok = not bool(reply.get("isError"))
    # Keep the reported result compact (the portal stores it for audit/desired-vs-actual).
    compact = {k: reply.get(k) for k in ("peer_id", "admission_policy", "exposure", "session_id",
                                         "paired", "session_live", "removed", "error",
                                         "idle_policy") if k in reply}
    return {"intent_id": intent_id, "ok": ok, "result": compact or {"raw_isError": reply.get("isError")}}


def _gather_local_den_state_for_sync() -> Dict:
    """Collect this device's den state for the portal snapshot (doc/28 sec 6): den
    status + grants + live pairings. Tolerant of every piece being unavailable."""
    den_state: Dict[str, Any] = {"collected_at_epoch": time.time()}
    den_status = _call_den_tool("status")
    if isinstance(den_status, dict) and not den_status.get("isError"):
        den_state["den_status"] = den_status
    den_grants = _call_den_tool("list_den")
    if isinstance(den_grants, dict) and not den_grants.get("isError"):
        den_state["den_grants"] = den_grants
    den_pairings = _call_den_tool("list_pairings")
    if isinstance(den_pairings, dict) and not den_pairings.get("isError"):
        den_state["den_pairings"] = den_pairings
    return den_state


def _post_den_sync_to_portal(access_token: str, endpoint_id_hex: str,
                             intent_results: Optional[list] = None) -> Dict:
    """One sync round trip (doc/28 sec 6): push den state (+ any intent results from a
    prior round), learn membership status + roster + pending intents. Returns
    {"http_status":..., "body":...}."""
    payload = {
        "endpoint_id": endpoint_id_hex,
        "den_state": _gather_local_den_state_for_sync(),
    }
    if intent_results:
        payload["intent_results"] = intent_results
    try:
        response = requests.post(TUNNEL_AF_DEN_SYNC_URL, json=payload,
                                 headers={"Authorization": f"Bearer {access_token}"}, timeout=25)
    except Exception as exc:
        return {"http_status": 0, "body": {"error": f"network: {exc!r}"}}
    result = {"http_status": response.status_code}
    try:
        result["body"] = response.json()
    except Exception:
        result["body"] = {"_raw": response.text[:300]}
    return result


def _sync_once_applying_any_intents(access_token: str, endpoint_id_hex: str) -> Dict:
    """Sync, apply any intents the portal delivered, and report their results in a second
    sync (doc/26 P3). Returns the FINAL sync body. Bounded to one apply round per call --
    dependent intents (admit-before-dial) simply arrive on the next sync once their
    dependency is recorded applied here."""
    first = _post_den_sync_to_portal(access_token, endpoint_id_hex)
    body = first.get("body") if isinstance(first.get("body"), dict) else {}
    if first.get("http_status") != 200:
        return first
    intents = body.get("intents") or []
    if not intents:
        return first
    results = [_apply_one_den_intent(intent) for intent in intents]
    MCPLogger.log(TOOL_LOG_NAME, f"applied {len(results)} den intent(s): "
                  + ", ".join(f"{r['intent_id']}:{'ok' if r['ok'] else 'FAIL'}" for r in results))
    second = _post_den_sync_to_portal(access_token, endpoint_id_hex, intent_results=results)
    if isinstance(second.get("body"), dict):
        second["body"]["applied_intent_results"] = results
    return second


# ----------------------------------------------------------------------------------
# peer tool bridge: drive the ONE iroh endpoint (never bind our own).
# ----------------------------------------------------------------------------------
def _call_peer_tool(operation: str, extra: Optional[Dict] = None) -> Optional[Dict]:
    """Invoke the `peer` tool via the server's internal dispatch. Returns the parsed JSON result
    dict, or None if the server/peer tool is unavailable (e.g. running standalone in a test)."""
    try:
        from ragtag.tools import get_server
        server = get_server()
        if server is None:
            return None
        from ragtag.tools import TOOL_TOKENS
        peer_token = TOOL_TOKENS.get(PEER_TOOL_NAME)
        params = {"input": {"operation": operation, "tool_unlock_token": peer_token}}
        if extra:
            params["input"].update(extra)
        raw = server.call_tool_internal(PEER_TOOL_NAME, params, calling_tool=TOOL_NAME)
        # ragtag tool results are {"content":[{"type":"text","text": <json str>}], "isError":bool}
        if isinstance(raw, dict) and raw.get("content"):
            text = raw["content"][0].get("text", "")
            try:
                return json.loads(text)
            except Exception:
                return {"_text": text, "isError": raw.get("isError", False)}
        return None
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"peer tool call '{operation}' failed: {exc!r}")
        return None


def _ensure_peer_started_and_get_endpoint_id() -> Optional[str]:
    """Start the peer endpoint (idempotent) and return this device's 64-hex EndpointId."""
    global _this_device_endpoint_id_hex
    started = _call_peer_tool("start")
    if started and started.get("endpoint_id"):
        with _tunnel_state_lock:
            _this_device_endpoint_id_hex = started["endpoint_id"]
        return started["endpoint_id"]
    status = _call_peer_tool("status")
    if status and status.get("endpoint_id"):
        with _tunnel_state_lock:
            _this_device_endpoint_id_hex = status["endpoint_id"]
        return status["endpoint_id"]
    return None


# ----------------------------------------------------------------------------------
# Operation handlers
# ----------------------------------------------------------------------------------
def _acquire_tokens_via_door(login_door: str, wait_seconds: float) -> Dict:
    """Run the chosen (or auto) login door and, on success, persist the offline token.
    Returns a dict describing what happened (including a pending device-code prompt)."""
    can_open_browser = bool(os.environ.get("DISPLAY") or platform.system() in ("Windows", "Darwin"))
    door = login_door or "auto"
    if door == "auto":
        door = "loopback" if can_open_browser else "device_code"

    if door == "loopback":
        outcome = _login_via_loopback_door(open_browser=True)
        if outcome.get("ok"):
            _persist_tokens(outcome["tokens"])
            return {"ok": True, "door": "loopback"}
        return {"ok": False, "door": "loopback", "reason": outcome.get("reason"),
                "authorize_url": outcome.get("authorize_url")}

    # device_code door
    device_authorization = _begin_device_grant()
    verification_uri_complete = (device_authorization.get("verification_uri_complete")
                                 or device_authorization.get("verification_uri"))
    poll = _poll_device_grant_for_tokens(
        device_authorization["device_code"], device_authorization["_code_verifier"],
        float(device_authorization.get("interval", 5)), wait_seconds)
    if poll.get("ok"):
        _persist_tokens(poll["tokens"])
        return {"ok": True, "door": "device_code"}
    return {"ok": False, "door": "device_code", "reason": poll.get("reason"),
            "verification_uri_complete": verification_uri_complete,
            "user_code": device_authorization.get("user_code")}


def handle_connect(params: Dict) -> Dict:
    """ONE-QR enrolment (doc/28): file a Den admission claim, show the ONE ceremony URL,
    and poll for the owner's approval -- which also hands this device its durable sync
    token. No separate OIDC device-code login: the account is chosen by whoever approves
    the ceremony (so you can approve on any device/browser logged into the right account).
    Re-running connect on an already-enrolled device is a no-browser resync."""
    wait_seconds = float(params.get("wait_seconds") or DEVICE_GRANT_APPROVAL_POLL_TIMEOUT_SECONDS)

    endpoint_id = _ensure_peer_started_and_get_endpoint_id()
    if not endpoint_id:
        return _error("Could not start the iroh endpoint via the 'peer' tool. Ensure the peer "
                      "tool + bundled 'iroh' package are available (WSL1 is unsupported).")

    device_name = params.get("device_name") or platform.node() or "aura-device"
    platform_label = _default_device_platform_label()

    # Already enrolled? Re-sync with the stored token -- no browser, no QR.
    stored = _read_stored_tokens()
    if stored and stored.get("device_sync_token"):
        sync = _sync_once_applying_any_intents(stored["device_sync_token"], endpoint_id)
        body = sync.get("body") if isinstance(sync.get("body"), dict) else {}
        if sync.get("http_status") == 200 and body.get("status") == "active":
            _update_tunnel_settings({"enabled": True, "auto_start": True,
                                     "device_display_name": device_name})
            return _ok({"connected": True, "endpoint_id": endpoint_id, "membership": "active",
                        "den_roster_size": len(body.get("den_roster") or []) or None,
                        "note": "Already in your Den; reconnected with no login (one-QR sync token)."})
        # Otherwise (removed / token no longer valid) fall through to re-admission.

    # ONE QR: file the claim, surface the ceremony URL, poll for approval + our sync token.
    claim = _file_den_claim_request_with_portal(endpoint_id, device_name, platform_label)
    if claim is None:
        return _ok({"connected": False, "endpoint_id": endpoint_id,
                    "reason": "Could not file the Den admission claim (portal unreachable, "
                              "rate-limited, or too old). Try again shortly.",
                    "manage_url": TUNNEL_AF_ACCOUNT_MANAGE_URL}, is_error=True)
    ceremony_url = claim.get("ceremony_url")
    if claim.get("claim_nonce"):
        # Remember it so a den_url call (tray click) reuses THIS claim instead of filing anew.
        _remember_pending_admission_claim(claim["claim_nonce"], claim.get("expires_in_seconds"))
    # Always print it big (green, yellow-fenced) for headless operators; also try to open
    # a local browser where one exists.
    if ceremony_url:
        _print_ceremony_url_banner(ceremony_url)
    can_open_browser = bool(os.environ.get("DISPLAY") or platform.system() in ("Windows", "Darwin"))
    ceremony_opened_in_browser = False
    if can_open_browser and ceremony_url:
        try:
            ceremony_opened_in_browser = bool(webbrowser.open(ceremony_url))
        except Exception:
            ceremony_opened_in_browser = False
    decision = _poll_den_claim_until_decided(claim.get("claim_nonce", ""), wait_seconds)
    status = decision.get("status")
    if status != "approved":
        return _ok({
            "connected": False, "endpoint_id": endpoint_id,
            "admission_decision": status,
            "ceremony_url": ceremony_url,
            "ceremony_opened_in_browser": ceremony_opened_in_browser,
            "claim_nonce": claim.get("claim_nonce"),
            "expires_in_seconds": claim.get("expires_in_seconds"),
            "what_now": ("Open ceremony_url on any device/browser signed in to the account you "
                         "want this device in (show it, QR it, or email it), check the shown "
                         "location + identity, and press 'Admit to my Den'. Then run connect "
                         "again, or poll {\"operation\":\"claim_status\",\"claim_nonce\":\"...\"}.")
                        if status == "pending" else
                        ("The owner DENIED this device." if status == "denied" else
                         "The admission link expired before approval - run connect again."),
        }, is_error=True)

    device_sync_token = decision.get("device_sync_token")
    if not device_sync_token:
        return _ok({"connected": False, "endpoint_id": endpoint_id, "membership": "approved",
                    "reason": "Approved, but this device did not receive its sync token -- the "
                              "approval poll must come from THIS device (same IP as the claim), "
                              "or the token was already collected. Run connect again from here.",
                    "ceremony_url": ceremony_url}, is_error=True)
    first_sync = _finalize_approved_admission_and_first_sync(device_sync_token, endpoint_id, device_name)
    first_sync_body = first_sync.get("body") if isinstance(first_sync.get("body"), dict) else {}
    return _ok({
        "connected": True,
        "endpoint_id": endpoint_id,
        "device_name": device_name,
        "one_qr": True,
        "ceremony_opened_in_browser": ceremony_opened_in_browser,
        "membership": first_sync_body.get("status") or "active",
        "den_roster_size": len(first_sync_body.get("den_roster") or []) or None,
        "note": "This machine is now in your Den (single approval) and reconnects automatically. "
                f"Manage or remove it at {TUNNEL_AF_ACCOUNT_MANAGE_URL}.",
    })


def handle_den_url(params: Dict) -> Dict:
    """The ONE canonical management URL for this device (operator doctrine 2026-07-27):
    launchers (tray/CLI/anything) call this and open EXACTLY the returned URL -- always
    den.html?device=<endpoint_id>, no exceptions. The WEBSITE owns every conversation
    from there (highlighted card when enrolled; inline admission card when the URL also
    carries &claim=<nonce>; guidance when neither). When un-enrolled this files -- or
    reuses -- an admission claim so the page CAN show the admission card, and a
    background thread polls for the approval so this device still collects its sync
    token (same-IP single delivery)."""
    endpoint_id = _ensure_peer_started_and_get_endpoint_id()
    if not endpoint_id:
        return _error("Could not start the iroh endpoint via the 'peer' tool.")
    device_name = params.get("device_name") or platform.node() or "aura-device"
    base_url = TUNNEL_AF_DEN_PAGE_URL + "?device=" + endpoint_id

    stored = _read_stored_tokens()
    if stored and (stored.get("device_sync_token") or stored.get("refresh_token")):
        return _ok({"url": base_url, "enrolled": True})

    # Un-enrolled: reuse a still-pending claim (idempotent tray clicks), else file fresh.
    claim_nonce = _read_pending_admission_claim_nonce_or_none()
    if claim_nonce:
        poll = _poll_den_claim_once(claim_nonce) or {}
        if poll.get("status") == "approved" and poll.get("device_sync_token"):
            # Owner already approved (e.g. after our waiter timed out): collect on the spot.
            _finalize_approved_admission_and_first_sync(poll["device_sync_token"],
                                                        endpoint_id, device_name)
            return _ok({"url": base_url, "enrolled": True,
                        "note": "A pending approval was collected; this device is now enrolled."})
        if poll.get("status") != "pending":
            claim_nonce = None
    if not claim_nonce:
        claim = _file_den_claim_request_with_portal(endpoint_id, device_name,
                                                    _default_device_platform_label())
        if not claim or not claim.get("claim_nonce"):
            return _ok({"url": base_url, "enrolled": False,
                        "note": "Could not file an admission claim (portal unreachable or "
                                "rate-limited); den.html will explain how to enrol."})
        claim_nonce = claim["claim_nonce"]
        _remember_pending_admission_claim(claim_nonce, claim.get("expires_in_seconds"))
    _start_background_admission_waiter_thread(claim_nonce, endpoint_id, device_name)
    return _ok({"url": base_url + "&claim=" + claim_nonce, "enrolled": False,
                "claim_nonce": claim_nonce})


def handle_activate(params: Dict) -> Dict:
    """Go ACTIVE with NO browser, using the stored one-QR sync token (or a legacy Keycloak
    token). Honours removal/pending -- a removed device will not silently re-activate."""
    bearer = _current_sync_bearer()
    if not bearer:
        return _error("Not enrolled yet - run 'connect' once (one approval). After that, "
                      "activate needs no browser.")
    endpoint_id = _ensure_peer_started_and_get_endpoint_id()
    if not endpoint_id:
        return _error("Could not start the iroh endpoint via the 'peer' tool.")

    _update_tunnel_settings({"enabled": True})
    sync_result = _sync_once_applying_any_intents(bearer, endpoint_id)
    sync_body = sync_result.get("body") if isinstance(sync_result.get("body"), dict) else {}
    membership_status = sync_body.get("status")
    if sync_result.get("http_status") != 200 or membership_status in ("pending_approval", "removed", "unclaimed"):
        if membership_status == "removed":
            _update_tunnel_settings({"enabled": False})
        return _ok({
            "active": False,
            "endpoint_id": endpoint_id,
            "membership": membership_status,
            "reason": ("This device was REMOVED from the owner's Den -- it will not activate. "
                       if membership_status == "removed" else
                       "This device is not (yet) an approved Den member. ")
                      + "Run 'connect' to request (re-)admission via the ceremony page.",
        }, is_error=True)
    _ensure_periodic_den_sync_thread_started()  # keep fresh from now on (no-op if armed)
    _ensure_wake_client_thread_started()        # start listening for coordinator pushes
    return _ok({
        "active": True,
        "endpoint_id": endpoint_id,
        "membership": membership_status,
        "den_roster_size": len(sync_body.get("den_roster") or []) or None,
        "note": "Active with no browser (one-QR sync token).",
    })


def handle_deactivate(params: Dict) -> Dict:
    stop_endpoint = params.get("stop_endpoint")
    stop_endpoint = True if stop_endpoint is None else bool(stop_endpoint)
    _update_tunnel_settings({"enabled": False})
    stopped = None
    if stop_endpoint:
        stopped = _call_peer_tool("stop")
    return _ok({
        "active": False,
        "endpoint_stopped": bool(stopped) if stop_endpoint else False,
        "still_logged_in": _read_stored_tokens() is not None,
        "note": "Deactivated. Still logged in - 'activate' brings it back with no browser.",
    })


def handle_status(params: Dict) -> Dict:
    stored = _read_stored_tokens()
    settings = _load_tunnel_settings()
    peer_status = _call_peer_tool("status")
    endpoint_id = None
    if isinstance(peer_status, dict):
        endpoint_id = peer_status.get("endpoint_id")
    with _tunnel_state_lock:
        endpoint_id = endpoint_id or _this_device_endpoint_id_hex
    return _ok({
        "claimed": stored is not None,               # offline token held
        "enabled": bool(settings.get("enabled")),    # user wants this device on the mesh
        "active": bool(peer_status and peer_status.get("started")),  # endpoint bound now
        "auto_start": bool(settings.get("auto_start")),
        "device_display_name": settings.get("device_display_name"),
        "endpoint_id": endpoint_id,
        "relay_ready": bool(peer_status.get("relay_ready")) if isinstance(peer_status, dict) else None,
        "connected_peer_count": peer_status.get("connected_peer_count") if isinstance(peer_status, dict) else None,
        "manage_url": TUNNEL_AF_ACCOUNT_MANAGE_URL,
    })


def handle_logout(params: Dict) -> Dict:
    _delete_stored_tokens()
    _update_tunnel_settings({"enabled": False})
    return _ok({"logged_out": True,
                "note": "Forgot the stored login. 'connect' will need a browser again. "
                        "(This does NOT revoke the device on the server - do that at "
                        f"{TUNNEL_AF_ACCOUNT_MANAGE_URL}.)"})


def handle_sync(params: Dict) -> Dict:
    """One den sync round trip (ADDED 2026-07-25, doc/28 sec 6): push this device's den
    state to the coordinator, learn membership status + the owner's den roster."""
    bearer = _current_sync_bearer()
    if not bearer:
        return _error("Not enrolled (no stored device credential) - run 'connect' first.")
    peer_status = _call_peer_tool("status")
    endpoint_id = peer_status.get("endpoint_id") if isinstance(peer_status, dict) else None
    with _tunnel_state_lock:
        endpoint_id = endpoint_id or _this_device_endpoint_id_hex
    if not endpoint_id:
        return _error("No iroh EndpointId known yet - run 'activate' (or 'connect') first.")
    result = _sync_once_applying_any_intents(bearer, endpoint_id)
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    if result["http_status"] != 200:
        return _ok({"synced": False, "portal_reply": result}, is_error=True)
    if body.get("status") == "removed":
        # Cooperative removal (doc/28 sec 6): the owner took this device out of the Den.
        _update_tunnel_settings({"enabled": False})
        MCPLogger.log(TOOL_LOG_NAME, "den sync says REMOVED - tunnel disabled (cooperative removal)")
    return _ok({
        "synced": True,
        "membership": body.get("status"),
        "den_roster": body.get("den_roster"),
        "applied_intent_results": body.get("applied_intent_results"),
        "note": body.get("note"),
    }, is_error=(body.get("status") not in ("active",)))


def handle_claim_status(params: Dict) -> Dict:
    """Poll a den admission claim once (nonce from a prior connect attempt)."""
    claim_nonce = params.get("claim_nonce")
    if not claim_nonce:
        return _error("claim_nonce is required (it was returned by the prior connect attempt).")
    reply = _poll_den_claim_once(str(claim_nonce))
    if reply is None:
        return _error("Could not reach the portal to check this claim (or the claim is unknown).")
    status = reply.get("status")
    if status == "approved" and reply.get("device_sync_token"):
        # Late token pickup: connect timed out but the owner has since approved. Store it.
        _persist_device_sync_token(reply["device_sync_token"])
    return _ok({"claim_nonce": claim_nonce, "status": status,
                "hint": "approved -> run connect (or activate) to finish; pending -> the owner "
                        "has not decided yet; denied/expired -> run connect for a fresh ceremony."})


# ----------------------------------------------------------------------------------
# Periodic den sync (ADDED 2026-07-27). Without this, a server only synced on
# connect/activate/manual `sync`, so after ~15 min the portal considered its connection
# ticket stale (blocking den.html pairings) and its presence dot went grey -- even with
# the tunnel up all night (operator report). Each cycle also applies any queued portal
# intents, so den.html wiring lands within one interval with no manual sync.
# ----------------------------------------------------------------------------------
PERIODIC_DEN_SYNC_INTERVAL_SECONDS = 60.0    # tightened 300->60 (2026-07-27): the call is
                                             # tiny and this bounds wiring latency + presence lag
PERIODIC_DEN_SYNC_FIRST_DELAY_SECONDS = 20.0

# The check-in coordinator (doc 77) pushes a "sync now" over a held iroh connection; the
# wake-client sets this event and the periodic loop below syncs immediately instead of
# waiting out its interval. So wiring lands in ~1s when a coordinator is reachable, and the
# 60s poll is just the backstop for when it isn't.
_den_checkin_wake_event = threading.Event()


def _sleep_seconds_from_portal_hint(sync_body: Dict) -> float:
    """The portal replies next_sync_hint_seconds while intents are still in flight for
    this device (pull-only control plane, doc/26 P4 push channel not built yet). Honour
    it so multi-hop chains (admit on one device, THEN dial from the other) complete in
    seconds instead of one poll interval per hop."""
    hint = sync_body.get("next_sync_hint_seconds")
    if isinstance(hint, (int, float)) and 0 < float(hint) < PERIODIC_DEN_SYNC_INTERVAL_SECONDS:
        return float(hint)
    return PERIODIC_DEN_SYNC_INTERVAL_SECONDS


_periodic_den_sync_thread_has_been_started = False


def _periodic_den_sync_loop_forever() -> None:
    """Daemon loop: while the tunnel is enabled AND its iroh endpoint is ALREADY running,
    push a den sync every interval (sooner when the portal hints work is in flight). It
    never (re)starts the endpoint itself -- it keeps a live tunnel fresh; deactivate/
    logout make it idle. State-change logging only (one line on first failure / on
    recovery), so a flaky network cannot spam the log."""
    last_cycle_failed = False
    time.sleep(PERIODIC_DEN_SYNC_FIRST_DELAY_SECONDS)
    while True:
        sleep_seconds = PERIODIC_DEN_SYNC_INTERVAL_SECONDS
        try:
            settings = _load_tunnel_settings()
            bearer = _current_sync_bearer()
            if settings.get("enabled") and bearer:
                peer_status = _call_peer_tool("status")
                if (isinstance(peer_status, dict) and peer_status.get("started")
                        and peer_status.get("endpoint_id")):
                    outcome = _sync_once_applying_any_intents(bearer, peer_status["endpoint_id"])
                    body = outcome.get("body") if isinstance(outcome.get("body"), dict) else {}
                    sleep_seconds = _sleep_seconds_from_portal_hint(body)
                    if body.get("status") == "removed":
                        # Den removal honoured within one interval (doc/28 lifecycle).
                        _update_tunnel_settings({"enabled": False})
                        MCPLogger.log(TOOL_LOG_NAME,
                                      "periodic den sync: this device was REMOVED from the Den -- "
                                      "tunnel disabled (re-admission needs a fresh 'connect' ceremony)")
                    elif outcome.get("http_status") == 200:
                        if last_cycle_failed:
                            MCPLogger.log(TOOL_LOG_NAME, "periodic den sync recovered (HTTP 200)")
                        last_cycle_failed = False
                    else:
                        if not last_cycle_failed:
                            MCPLogger.log(TOOL_LOG_NAME,
                                          f"periodic den sync failing (http_status="
                                          f"{outcome.get('http_status')}); retrying quietly "
                                          f"every {int(PERIODIC_DEN_SYNC_INTERVAL_SECONDS)}s")
                        last_cycle_failed = True
        except Exception as exc:
            if not last_cycle_failed:
                MCPLogger.log(TOOL_LOG_NAME, f"periodic den sync error: {exc!r}; retrying quietly")
            last_cycle_failed = True
        # Wait out the interval, but wake IMMEDIATELY if the coordinator pushed a checkin_now
        # (doc 77). Either way, clear the flag so the next cycle starts fresh.
        if _den_checkin_wake_event.wait(timeout=sleep_seconds):
            _den_checkin_wake_event.clear()


def _ensure_periodic_den_sync_thread_started() -> None:
    global _periodic_den_sync_thread_has_been_started
    with _tunnel_state_lock:
        if _periodic_den_sync_thread_has_been_started:
            return
        _periodic_den_sync_thread_has_been_started = True
    threading.Thread(target=_periodic_den_sync_loop_forever,
                     name="tunnel-periodic-den-sync", daemon=True).start()


# ----------------------------------------------------------------------------------
# Coordinator wake-client (doc 77 section 13, ADDED 2026-07-27). This device DIALS the
# check-in coordinator (pinned slot-1 id, falling back to 2/3), verifies the handshaken
# remote id against the pinned stack (MITM-proof), holds the ONE connection open, and on a
# `checkin_now` frame sets _den_checkin_wake_event so the periodic loop syncs at once. The
# read runs on the peer tool's iroh loop; it does NO blocking I/O (the actual sync happens
# on the periodic-sync thread), so it never stalls the loop. Reconnects with backoff. A
# device with no coordinator reachable just relies on the 60s hygiene poll -- wake is an
# optimisation, never a correctness dependency.
# ----------------------------------------------------------------------------------
DEN_COORD_ALPN_BYTES = b"af/den-coord/1"
WAKE_CLIENT_RECONNECT_MIN_SECONDS = 5.0
WAKE_CLIENT_RECONNECT_MAX_SECONDS = 120.0
WAKE_CLIENT_NOT_READY_RECHECK_SECONDS = 20.0
_MAX_WAKE_FRAME_BYTES = 1 << 20
_wake_client_thread_has_been_started = False


def _pinned_coordinator_ids_and_version():
    """(ordered [slot1,slot2,slot3] ids, key_stack_version) or (None, None) if the pinned
    stack module is unavailable (then the wake-client simply does not run)."""
    try:
        from ragtag.tools.den_coordinator_pinned_keys import (
            coordinator_identity_endpoint_ids_in_dial_order, DEN_COORDINATOR_KEY_STACK_VERSION)
        return coordinator_identity_endpoint_ids_in_dial_order(), DEN_COORDINATOR_KEY_STACK_VERSION
    except Exception:
        return None, None


async def _hold_one_coordinator_wake_session(peer_module, coordinator_ids, key_stack_version) -> bool:
    """Runs ON the iroh loop. Dial the first reachable PINNED coordinator, verify its id,
    hold the connection and read frames until it drops. Returns True if we connected (then
    lost it), False if none was reachable. Sets the wake event on each checkin_now."""
    last_error = None
    for coordinator_id in coordinator_ids:
        try:
            connection = await peer_module.den_support_dial_connection_on_loop(
                None, coordinator_id, DEN_COORD_ALPN_BYTES)
        except Exception as dial_error:
            last_error = dial_error
            continue
        try:
            remote_id_hex = str(connection.remote_id()).lower()
        except Exception:
            remote_id_hex = ""
        if remote_id_hex != coordinator_id:
            # Pinned-identity check failed -- this is NOT our coordinator. Drop it.
            MCPLogger.log(TOOL_LOG_NAME,
                          f"wake-client: dialed {coordinator_id[:16]} but handshake id was "
                          f"{remote_id_hex[:16]} -- dropping (pinned-identity mismatch)")
            try:
                connection.close(0, b"identity mismatch")
            except Exception:
                pass
            continue
        try:
            bi_stream = await connection.open_bi()
            send_stream, recv_stream = bi_stream.send(), bi_stream.recv()
            hello = json.dumps({"t": "hello", "key_stack_version": key_stack_version}).encode("utf-8")
            await send_stream.write(len(hello).to_bytes(4, "big") + hello)
            MCPLogger.log(TOOL_LOG_NAME,
                          f"wake-client connected to coordinator {coordinator_id[:16]} (held; "
                          f"awaiting checkin_now)")
            while True:
                header = await recv_stream.read_exact(4)
                length = int.from_bytes(header, "big")
                if length <= 0 or length > _MAX_WAKE_FRAME_BYTES:
                    break
                body = await recv_stream.read_exact(length)
                try:
                    message = json.loads(body.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(message, dict) and message.get("t") == "checkin_now":
                    _den_checkin_wake_event.set()
                    MCPLogger.log(TOOL_LOG_NAME, "wake-client: checkin_now received -> sync now")
            return True
        except Exception as session_error:
            last_error = session_error
            return True   # we DID connect; a dropped session should reconnect promptly
    if last_error is not None:
        MCPLogger.log(TOOL_LOG_NAME,
                      f"wake-client: no pinned coordinator reachable right now ({last_error!r}); "
                      f"relying on the hygiene poll")
    return False


def _wake_client_loop_forever() -> None:
    """Daemon thread: keep a wake session to the coordinator up whenever the tunnel is
    enabled + enrolled + its endpoint is running. Schedules the held session on the iroh
    loop and waits for it to end, then reconnects with backoff."""
    coordinator_ids, key_stack_version = _pinned_coordinator_ids_and_version()
    if not coordinator_ids:
        MCPLogger.log(TOOL_LOG_NAME, "wake-client: no pinned coordinator stack; not starting")
        return
    reconnect_backoff = WAKE_CLIENT_RECONNECT_MIN_SECONDS
    time.sleep(PERIODIC_DEN_SYNC_FIRST_DELAY_SECONDS)
    while True:
        wait_seconds = WAKE_CLIENT_NOT_READY_RECHECK_SECONDS
        try:
            settings = _load_tunnel_settings()
            bearer = _current_sync_bearer()
            peer_status = _call_peer_tool("status")
            ready = (settings.get("enabled") and bearer and isinstance(peer_status, dict)
                     and peer_status.get("started"))
            if ready:
                import ragtag.tools.peer as peer_module
                session_future = peer_module.den_support_spawn_on_iroh_loop(
                    _hold_one_coordinator_wake_session(peer_module, coordinator_ids, key_stack_version))
                connected = bool(session_future.result())   # blocks until the session ends
                if connected:
                    reconnect_backoff = WAKE_CLIENT_RECONNECT_MIN_SECONDS
                    wait_seconds = WAKE_CLIENT_RECONNECT_MIN_SECONDS   # brief pause, then reconnect
                else:
                    reconnect_backoff = min(reconnect_backoff * 2, WAKE_CLIENT_RECONNECT_MAX_SECONDS)
                    wait_seconds = reconnect_backoff
        except Exception as loop_error:
            MCPLogger.log(TOOL_LOG_NAME, f"wake-client loop error: {loop_error!r}; backing off")
            reconnect_backoff = min(reconnect_backoff * 2, WAKE_CLIENT_RECONNECT_MAX_SECONDS)
            wait_seconds = reconnect_backoff
        time.sleep(wait_seconds)


def _ensure_wake_client_thread_started() -> None:
    global _wake_client_thread_has_been_started
    with _tunnel_state_lock:
        if _wake_client_thread_has_been_started:
            return
        _wake_client_thread_has_been_started = True
    threading.Thread(target=_wake_client_loop_forever,
                     name="tunnel-coordinator-wake-client", daemon=True).start()


# ----------------------------------------------------------------------------------
# Auto-start hook (runs after all tools registered; silent, never blocks startup).
# ----------------------------------------------------------------------------------
def on_all_tools_registered():
    try:
        # Always arm the periodic den sync + coordinator wake-client -- both loops check
        # enabled/bearer/endpoint each cycle, so arming is safe even when the tunnel is
        # disabled or not enrolled (they simply idle until it is).
        _ensure_periodic_den_sync_thread_started()
        _ensure_wake_client_thread_started()
        settings = _load_tunnel_settings()
        if not (settings.get("enabled") and settings.get("auto_start")):
            return
        if not _read_stored_tokens():
            return
        MCPLogger.log(TOOL_LOG_NAME, "auto-start: reactivating Aura Friday tunnel (silent)")
        result = handle_activate({})
        MCPLogger.log(TOOL_LOG_NAME, f"auto-start result isError={result.get('isError')}")
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"auto-start skipped ({exc!r})")


# ----------------------------------------------------------------------------------
# Standard ragtag tool plumbing
# ----------------------------------------------------------------------------------
def _ok(payload: Dict, is_error: bool = False) -> Dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": is_error}


def readme(with_readme: bool = True) -> str:
    if not with_readme:
        return ""
    return "\n\n" + json.dumps({
        "description": TOOLS[0]["readme"],
        "parameters": TOOLS[0]["real_parameters"],
    }, indent=2)


def _error(message: str, with_readme: bool = False) -> Dict:
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {message}")
    return {"content": [{"type": "text", "text": f"{message}{readme(with_readme)}"}], "isError": True}


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    return _error(error_msg, with_readme=with_readme)


def handle_tunnel(input_param: Dict) -> Dict:
    try:
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.")
        if input_param.get("tool_unlock_token") != TOOL_UNLOCK_TOKEN:
            return create_error_response(
                "Invalid or missing tool_unlock_token: this indicates your context is missing the "
                "following details, which are needed to correctly use this tool:")
        operation = input_param.get("operation")
        dispatch = {
            "connect": handle_connect,
            "activate": handle_activate,
            "deactivate": handle_deactivate,
            "status": handle_status,
            "logout": handle_logout,
            "sync": handle_sync,                 # ADDED 2026-07-25 (doc/28)
            "claim_status": handle_claim_status,  # ADDED 2026-07-25 (doc/28)
            "den_url": handle_den_url,           # ADDED 2026-07-27 (one-URL doctrine)
        }
        if operation in dispatch:
            return dispatch[operation](input_param)
        valid = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
        return create_error_response(f"Unknown operation: '{operation}'. Available: {', '.join(valid)}")
    except Exception as exc:
        return create_error_response(f"Error in tunnel operation: {exc}", with_readme=False)


HANDLERS = {
    TOOL_NAME: handle_tunnel
}
