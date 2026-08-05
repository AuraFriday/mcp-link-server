"""
File: ragtag/tools/den.py
Project: Aura Friday MCP-Link Server
Component: Den Tool (interim Den v0 - iroh MCP tool sharing between servers)
Author: Christopher Nathan Drake (cnd)

Interim Den v0. Lets two Aura Friday servers offer each other's MCP tools over
an iroh QUIC session, so a client on machine A can call a tool that actually runs
on machine B. Built to the design in
websites/account.aurafriday.com/doc/25 (spec) + doc/30 (wire) and the code plan
ragtag/python/ragtag/DEN_V0_IROH_AND_STDIO_TOOL_SHARING_IMPLEMENTATION_PLAN.md.

SECURITY MODEL (this is the "open to the internet" surface - read before editing):
  * Transport identity is cryptographic: iroh/QUIC authenticates every peer by its
    32-byte EndpointId. connection.remote_id() cannot be spoofed.
  * ADMISSION (who may connect at all): a default-deny allowlist. On EVERY inbound
    connection, BEFORE a single application byte is read, remote_id() must appear in
    settings[0].den.admitted_peers - unless admission_policy == "accept_all". A
    non-admitted peer's connection is closed immediately (gate v0-GL2).
  * EXPOSURE (what an admitted peer may actually use): driven ONLY by that peer's
    admitted_peers entry ("exposure": "all" | {"only": [names]}). A peer with no
    entry sees ZERO tools even under accept_all. Exposure is re-checked at CALL time,
    not just when advertised - a peer cannot call a tool we did not expose to it.
  * These two gates are independent: admission decides connect/no-connect; exposure
    decides tool visibility+callability. Random internet peers therefore cannot use
    our MCP tools: they fail admission, and even if admitted by policy they get an
    empty tool set until explicitly granted exposure.

UMBRELLA LIFECYCLE (doc/50 F22 - lazy, peer-owned; mirrors the Android BUILD 5 model):
  * An umbrella tool exists for every ADMITTED peer - registered at startup, at
    admission time (request_pair/respond_pair), and refreshed on every den_hello.
  * Sessions are TRANSPORT and form lazily: calling an umbrella with no live session
    (re)dials the peer on the spot (last_known_ticket first, then relay by EndpointId).
  * A dropped session does NOT remove the umbrella (availability = admitted, not
    session-alive); only kick/un-admit removes it. accept_all guests with no allowlist
    entry keep the old session-bound behavior (their umbrella dies with the session).

WIRE PROTOCOL (purpose-built, minimal; NOT reused from remote.py's SSE-reverse wire,
so a peer can never reach a non-exposed tool by speaking raw tools/call). One iroh
bi-stream per session, framed exactly like peer.py: 4-byte big-endian length + one
UTF-8 JSON object. Frames, demuxed by their single distinguishing key:
  * {"den_hello": <identity>}                 dialer -> acceptor (opens the stream)
  * {"den_hello_result": <identity>, "id": N} acceptor -> dialer (reply to hello)
  * {"den_call": {operation, tool?, input?}, "id": N}  either -> peer (invoke)
  * {"den_result": <result-or-error>, "id": N}         reply to a den_call
<identity> = {display_name, endpoint_id, exposed_tool_names:[...]}.
Each side allocates request ids from its own space; a reply is matched only against
the sender's own pending-waiter table, so simultaneous id=1 on both sides is fine.

THREADING: peer.py owns the single iroh asyncio loop. All stream I/O and the per-
session read loop run ON that loop. Tool handlers run on server worker threads and
marshal sends via peer.den_support_run_on_iroh_loop(...). Serving a peer's
den_call (which calls a local tool, possibly slow like 'terminal') is offloaded to
a ThreadPoolExecutor so it NEVER blocks the iroh loop.

Copyright: (c) 2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "eτuᎬJ𝟩qМꓦȢꓐʈNSꓔӠᗪĸӠᗪȣȷbⲞĵϜKⲘƼqv𝟫ν𝟚ɗƖƏⅼAᎬƘƽ4ƘĐⲞυxzƘ𝟧ďiⲔʌ8TGᑕNƲUWꓔ𝕌pС𐓒ŧСһƶⲞᖴEАƎꓗƽꓚz𝟦tƴƨƍցVȠhꓪJʋDȠsƴhhPб𝟩սƋᴠɅƖНq"
"signdate": "2026-07-29T09:35:21.197Z",
"""

import asyncio
import concurrent.futures
import getpass
import hashlib
import hmac
import json
import os
import platform
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from easy_mcp.server import MCPLogger, get_tool_token

TOOL_LOG_NAME = "DEN"

# tool_unlock_token = a COMPREHENSION GATE, NOT authentication and NOT a secret: it only proves
# the caller has read THIS tool's readme before acting, and the readme hands it out FREELY.
# get_tool_token(__file__) derives it from this file's own bytes, so it ROTATES whenever this
# tool's code changes -- deliberately, to force AIs to re-read after an update. Invariants (see
# doc/50_non-AI-calling-and-how-to-get-unlock-tokens.md): this tool OWNS its token -- never mint
# or embed another tool's token, never accept a token supplied at registration; reveal it ONLY
# via readme (readme needs no token); on a wrong/missing token RETURN the readme rather than
# failing as "unauthorized". (den also derives a per-peer "umbrella" comprehension token below,
# keyed by this token -- same concept; see _make_umbrella_tool_handler.)
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"den{TOOL_NAME_SUFFIX}"

# The ALPN this tool owns on the shared iroh endpoint (bound by peer.py alongside the
# mesh ALPN). Inbound connections negotiating it are routed here. Kept as a literal
# (must equal peer.DEN_MCP_SESSION_ALPN) so this module needs no import-time
# dependency on peer.py - see _peer() for why the peer import must stay lazy.
DEN_MCP_SESSION_ALPN = "af/mcp-session/1"


def _peer():
    """Return the LIVE ragtag.tools.peer module, imported lazily.

    The tools loader (ragtag/tools/__init__.py discover_tools) re-execs every tool
    file via spec_from_file_location, so importing peer at THIS module's top would
    bind whatever peer object existed mid-discovery - not necessarily the final one
    whose iroh endpoint actually runs. Importing lazily (after startup, at
    set_server/operation time) always yields the module currently in sys.modules,
    so our ALPN handler registers on the same peer globals the endpoint consults.
    """
    from ragtag.tools import peer as peer_tool
    return peer_tool

# 32 MiB per doc/30. A frame larger than this ends the session rather than allocating.
DEN_MAX_FRAME_BYTES = 32 * 1024 * 1024

# How long the local umbrella handler blocks waiting for a peer's den_result. Long
# enough for slow relayed tools (e.g. terminal waits); the peer side rides remote
# tool timeouts of its own. Kept under the server's default 270s tool timeout.
DEN_CALL_MAX_WAIT_SECONDS = 240.0

# F23 (doc/50): idle-session teardown stance. Sessions are TRANSPORT (F22) - tearing an
# idle one down is invisible (the umbrella persists; the next call re-dials), but leaving
# it up costs per-peer QUIC keepalive + NAT-refresh traffic forever. Each device applies
# ONE local stance to ITS OWN sessions:
#   auto          warm while on external power; idle-close after a grace when on battery
#   always_ready  never idle-close (owner accepts the keepalive cost)
#   battery_saver always idle-close, even on power (metered/heat cases)
DEN_VALID_SESSION_IDLE_POLICIES = ("auto", "always_ready", "battery_saver")
DEN_SESSION_IDLE_POLICY_CONFIG_KEY = "session_idle_policy"
DEN_SESSION_IDLE_GRACE_CONFIG_KEY = "session_idle_grace_seconds"
DEN_DEFAULT_SESSION_IDLE_GRACE_SECONDS = 240.0
DEN_MINIMUM_SESSION_IDLE_GRACE_SECONDS = 30.0
DEN_IDLE_REAPER_SWEEP_INTERVAL_SECONDS = 30.0
# The QUIC close reason for an F23 idle teardown. Peers MUST treat it as benign (it is
# not a failure; their umbrella survives per F22 and the next call re-establishes).
DEN_IDLE_TEARDOWN_CLOSE_REASON_BYTES = b"idle"

# Tools never shared over the den regardless of exposure (internal/plumbing or
# would recurse). Umbrella tools (dynamically registered peer names) are also
# excluded via _registered_umbrella_tool_names.
NEVER_SHAREABLE_TOOL_NAMES = frozenset({TOOL_NAME, "den", "remote"})


# ----------------------------------------------------------------------------------
# Tool definition (the operator/AI-facing 'den' control tool)
# ----------------------------------------------------------------------------------
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Interim Den v0: securely share MCP tools with another Aura Friday server over iroh. Pair with a trusted peer (default-deny allowlist by EndpointId) and each side appears to the other as a callable umbrella tool.
- Use to connect two of your own machines so tools on one can be called from the other.
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
                    "enum": ["readme", "status", "list_den", "request_pair", "respond_pair",
                             "list_pairings", "set_exposure", "kick", "set_admission_policy",
                             "set_session_policy"],
                    "description": "Operation to perform"
                },
                "peer_id": {
                    "type": "string",
                    "description": "64-hex EndpointId of the peer (request_pair/respond_pair/set_exposure/kick)"
                },
                "ticket": {
                    "type": "string",
                    "description": "request_pair: the peer's iroh ticket (from its 'peer' status/start). Preferred over peer_id: carries direct addresses so LAN dials work without relay enrollment."
                },
                "name": {
                    "type": "string",
                    "description": "request_pair/respond_pair: friendly label to record for this peer (optional; the peer also announces its own display_name)"
                },
                "exposure": {
                    "description": "request_pair/respond_pair/set_exposure: which of OUR tools this peer may use. Either the string \"all\", or an object {\"only\": [\"tool1\", \"tool2\"]}. Defaults to \"all\" when pairing your own machines."
                },
                "policy": {
                    "type": "string",
                    "enum": ["deny_all", "accept_all"],
                    "description": "set_admission_policy: deny_all (default, only allowlisted peers may connect) or accept_all (any peer may connect, but still sees only tools you exposed)"
                },
                "idle_policy": {
                    "type": "string",
                    "enum": ["auto", "always_ready", "battery_saver"],
                    "description": "set_session_policy (F23): auto (default; sessions stay warm while on external power, idle-close on battery), always_ready (never idle-close), battery_saver (always idle-close). Umbrellas are unaffected; a torn-down session re-dials on the next call."
                },
                "idle_grace_seconds": {
                    "type": "number",
                    "description": "set_session_policy: optional idle grace before teardown (default 240, min 30). Power-user knob; the stance alone is right for almost everyone."
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "readme": """
Den tool - interim Den v0: share MCP tools between two Aura Friday servers over iroh.

## What it does
Pairs this server with a trusted peer server. After pairing, the peer appears here as
a single umbrella tool named after the peer (e.g. 'nik_gram16'); calling that tool with
operation 'call' runs a tool on the peer. Symmetrically, this server appears on the peer.

## Security (important)
Two independent gates protect you:
1. ADMISSION (default-deny): only peers whose 64-hex EndpointId is in your allowlist may
   connect at all. Set once via request_pair/respond_pair. A random peer that dials you is
   dropped before any data is read.
2. EXPOSURE: even an admitted peer can only use the tools you granted it ("all" or a
   specific {"only":[...]} list). Exposure is checked again at call time.
Transport identity is cryptographic (iroh authenticates each peer's EndpointId), so the
allowlist cannot be bypassed by spoofing a name.

## Usage-Safety Token System
Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """
Include tool_unlock_token in the input dict for all operations except readme.

## Typical pairing (two of your machines, A and B)
On A: run 'peer' start (or any den op) so the iroh endpoint binds; note A's endpoint_id
      and ticket from 'peer' status.
On B: same - note B's endpoint_id and ticket.
On A: den request_pair with B's ticket (and optional exposure). This records B in A's
      allowlist AND dials B, establishing the session.
On B: den respond_pair with A's peer_id (so B admits A). Because A dialed with a ticket,
      the session is already up; respond_pair just authorizes the reverse direction.
Now 'den list_pairings' on either side shows the live session, and each side has an
umbrella tool for the other. Call it: {"input":{"operation":"call","tool":"<their_tool>",
"tool_input":{...}}} through the umbrella (do the umbrella's own readme first for its token).

## Operations
1. readme:               {"input":{"operation":"readme"}}
2. status:               endpoint id, my display name, admission policy, counts.
3. list_den:           the allowlist (admitted peers, their names + exposure, live?).
4. request_pair:         {"operation":"request_pair","ticket":"<peer ticket>",
                          "exposure":"all","tool_unlock_token":"..."}  (or "peer_id":"<64hex>")
5. respond_pair:         {"operation":"respond_pair","peer_id":"<64hex>","exposure":"all",...}
6. list_pairings:        live sessions + DIRECT/RELAY path + umbrella tool names.
7. set_exposure:         {"operation":"set_exposure","peer_id":"<64hex>","exposure":{"only":["terminal"]},...}
8. kick:                 {"operation":"kick","peer_id":"<64hex>",...} remove + disconnect.
9. set_admission_policy: {"operation":"set_admission_policy","policy":"deny_all",...}
10. set_session_policy:  {"operation":"set_session_policy","idle_policy":"auto",...}
    F23 idle-teardown stance: auto = sessions stay warm while this device is on external
    power and idle-close after a grace (default 240s) on battery; always_ready = never
    idle-close; battery_saver = always idle-close. Optional "idle_grace_seconds" (min 30).
    Idle closes are benign: the peer sees close reason "idle", umbrellas persist (F22),
    and the next call re-dials in under a second.

## Notes
- Requires the 'iroh' package and (for cross-network peers) both EndpointIds enrolled on
  the relay paywall; LAN peers connect directly via ticket without enrollment.
- Exposure with no allowlist entry = no tools. accept_all only affects who may connect.
- LAZY UMBRELLAS (doc/50 F22): every admitted peer's umbrella tool exists from the moment
  of admission and across restarts. A dropped session does NOT remove it - calling the
  umbrella (re)dials the peer on the spot (stored ticket first, then relay by EndpointId).
  Only 'kick' removes a peer's umbrella.
"""
    }
]


# ----------------------------------------------------------------------------------
# Module state
# ----------------------------------------------------------------------------------
_server = None  # set by set_server(); the easy_mcp MCPServer instance

_den_sessions_lock = threading.Lock()
# session_id -> _DenSessionRecord
_den_sessions_by_id: Dict[str, "_DenSessionRecord"] = {}
# Names of umbrella tools we have registered (so they are excluded from what we share).
_registered_umbrella_tool_names: set = set()
# F22 (doc/50): umbrellas are PEER-owned, not session-owned. Maps the normalised peer
# EndpointId -> the umbrella tool name currently registered for that peer. An entry
# lives for as long as the peer is admitted (for accept_all guests with no allowlist
# entry: for the life of their session). Guarded by _den_sessions_lock.
_umbrella_tool_name_by_peer_id: Dict[str, str] = {}
# F22: serialises the lazy (re)dial per peer, so two concurrent umbrella calls cannot
# race to create two sessions to the same peer. normalised peer id -> that peer's lock.
_lazy_dial_locks_by_peer_id: Dict[str, threading.Lock] = {}
_lazy_dial_locks_dict_lock = threading.Lock()
# Monotonic counter making session ids unique per peer.
_den_session_sequence = 0

# Lazily created pool that runs blocking local tool dispatch for INBOUND den_calls,
# keeping the iroh loop unblocked.
_serve_dispatch_thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_serve_dispatch_thread_pool_lock = threading.Lock()

_alpn_handler_registered = False


class _DenSessionRecord:
    """One live den session (one iroh connection, one bi-stream, framed JSON)."""

    def __init__(self, session_id: str, connection, send_stream, recv_stream,
                 peer_endpoint_id_hex: str, we_dialed_this_peer: bool):
        self.session_id = session_id
        self.connection = connection
        self.send_stream = send_stream
        self.recv_stream = recv_stream
        self.peer_endpoint_id_hex = peer_endpoint_id_hex
        self.we_dialed_this_peer = we_dialed_this_peer
        self.peer_display_name: Optional[str] = None
        self.peer_device_info: Optional[Dict[str, Any]] = None
        self.local_umbrella_tool_name: Optional[str] = None
        self.is_active = True
        self.created_at_unix_time = time.time()
        # F23 idle tracking: refreshed on EVERY frame sent or received; the idle reaper
        # only closes a session whose last activity is older than the configured grace.
        self.last_frame_activity_unix_time = time.time()
        # F23: count of inbound den_calls currently being served locally (slow local
        # tools can outlast the idle grace; the reaper must never close mid-serve).
        # Guarded by pending_reply_waiters_lock like the outbound waiters.
        self.inflight_inbound_serve_count = 0
        # Serialises whole-frame writes; an asyncio.Lock is loop-affine and all sends
        # marshal onto the iroh loop, so this is the correct primitive here.
        self.send_frame_serialising_lock = asyncio.Lock()
        # Outbound request id space + waiters (request_id -> {"event","result"}).
        self._next_request_id = 0
        self._request_id_lock = threading.Lock()
        # Guards the waiters dict + is_active against add/resolve/teardown races
        # (worker thread adds a waiter; loop thread resolves or tears down).
        self.pending_reply_waiters_lock = threading.Lock()
        self.pending_reply_waiters_by_request_id: Dict[int, Dict[str, Any]] = {}

    def allocate_request_id(self) -> int:
        with self._request_id_lock:
            self._next_request_id += 1
            return self._next_request_id


# ----------------------------------------------------------------------------------
# Server wiring
# ----------------------------------------------------------------------------------
def set_server(server) -> None:
    """Called by the tools loader; also installs our inbound ALPN handler on peer.py."""
    global _server, _alpn_handler_registered
    _server = server
    if not _alpn_handler_registered:
        try:
            _peer().register_alpn_session_handler(
                DEN_MCP_SESSION_ALPN, _handle_inbound_den_connection_on_loop)
            _alpn_handler_registered = True
            MCPLogger.log(TOOL_LOG_NAME, "installed inbound den ALPN handler")
        except Exception as handler_install_error:
            MCPLogger.log(TOOL_LOG_NAME, f"could not install ALPN handler: {handler_install_error!r}")
    # F22 lazy model: every already-admitted peer gets its umbrella at startup, so a
    # restart no longer hides peers until they happen to reconnect. Sessions form
    # lazily on the first call through an umbrella (or when the peer dials us).
    try:
        _register_umbrella_tools_for_all_admitted_peers()
    except Exception as startup_umbrella_error:
        MCPLogger.log(TOOL_LOG_NAME, f"startup umbrella registration failed: {startup_umbrella_error!r}")


def _get_server():
    return _server


def _get_serve_dispatch_thread_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _serve_dispatch_thread_pool
    with _serve_dispatch_thread_pool_lock:
        if _serve_dispatch_thread_pool is None:
            _serve_dispatch_thread_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="den-serve")
        return _serve_dispatch_thread_pool


# ----------------------------------------------------------------------------------
# Identity + config helpers
# ----------------------------------------------------------------------------------
def _sanitize_to_mcp_tool_name(raw_name: str) -> str:
    """Map a display name to a safe MCP tool name: [A-Za-z0-9_-] pass through with case
    PRESERVED (operator bug report 2026-07-24: 'android_BL7000_iroh' must keep its caps,
    matching its SSE sibling tool 'android_BL7000'), every other character -> _."""
    stripped = (raw_name or "").strip()
    safe_chars = []
    for character in stripped:
        safe_chars.append(character if (character.isalnum() or character in "_-") else "_")
    collapsed = "".join(safe_chars).strip("_-") or "peer"
    return collapsed[:64]


def _default_self_display_name() -> str:
    """<user>@<host> default (doc/25 section 9): '@' cannot occur in either part."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    try:
        host = socket.gethostname()
    except Exception:
        host = "host"
    user = (user or "user").strip().lower()
    host = (host or "host").split(".")[0].strip().lower()
    return f"{user}@{host}"


def _read_den_config() -> Dict[str, Any]:
    """settings[0].den (admission_policy, admitted_peers, my_display_name)."""
    try:
        from ragtag.shared_config import get_config_manager
        sections = get_config_manager().get_settings_sections_copy("den")
        den_section = sections.get("den")
        if isinstance(den_section, dict):
            return den_section
    except Exception as config_read_error:
        MCPLogger.log(TOOL_LOG_NAME, f"den config read failed: {config_read_error!r}")
    return {}


def _update_den_config(mutator) -> bool:
    """Atomic read-modify-write of settings[0].den via SharedConfigManager."""
    from ragtag.shared_config import get_config_manager

    def _apply(config: Dict[str, Any]) -> None:
        if "settings" not in config or not isinstance(config["settings"], list) or not config["settings"]:
            config["settings"] = [{}]
        settings_0 = config["settings"][0]
        den_section = settings_0.get("den")
        if not isinstance(den_section, dict):
            den_section = {}
        den_section.setdefault("admission_policy", "deny_all")
        den_section.setdefault("admitted_peers", {})
        mutator(den_section)
        settings_0["den"] = den_section

    return get_config_manager().update_config(_apply)


def _self_display_name() -> str:
    configured = _read_den_config().get("my_display_name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return _default_self_display_name()


# ----------------------------------------------------------------------------------
# F23 idle-teardown stance (doc/50 F23)
# ----------------------------------------------------------------------------------
def _configured_session_idle_policy() -> str:
    configured_policy = _read_den_config().get(DEN_SESSION_IDLE_POLICY_CONFIG_KEY)
    if configured_policy in DEN_VALID_SESSION_IDLE_POLICIES:
        return configured_policy
    return "auto"


def _configured_session_idle_grace_seconds() -> float:
    configured_grace = _read_den_config().get(DEN_SESSION_IDLE_GRACE_CONFIG_KEY)
    try:
        grace_seconds = float(configured_grace)
        if grace_seconds >= DEN_MINIMUM_SESSION_IDLE_GRACE_SECONDS:
            return grace_seconds
    except (TypeError, ValueError):
        pass
    return DEN_DEFAULT_SESSION_IDLE_GRACE_SECONDS


def _device_is_on_battery_power_right_now() -> bool:
    """True only when a battery exists AND we are NOT on external power. Unknown (no
    psutil, no battery sensor - i.e. desktops/servers) counts as POWERED, so 'auto'
    never churns sessions on machines that have nothing to save."""
    try:
        import psutil
        battery_state = psutil.sensors_battery()
        return bool(battery_state is not None and not battery_state.power_plugged)
    except Exception:
        return False


def _idle_teardown_is_active_right_now() -> bool:
    """The F23 stance, resolved for THIS moment: auto follows the power state."""
    stance = _configured_session_idle_policy()
    if stance == "always_ready":
        return False
    if stance == "battery_saver":
        return True
    return _device_is_on_battery_power_right_now()


def _normalise_endpoint_id(endpoint_id_hex: Optional[str]) -> str:
    return (endpoint_id_hex or "").strip().lower()


def _is_peer_admitted(peer_endpoint_id_hex: str, den_config: Dict[str, Any]) -> bool:
    """Admission gate: allowlisted, or admission_policy == accept_all."""
    if den_config.get("admission_policy") == "accept_all":
        return True
    admitted_peers = den_config.get("admitted_peers") or {}
    return _normalise_endpoint_id(peer_endpoint_id_hex) in {
        _normalise_endpoint_id(existing_id) for existing_id in admitted_peers.keys()
    }


def _get_admitted_peer_entry(peer_endpoint_id_hex: str, den_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    admitted_peers = den_config.get("admitted_peers") or {}
    target = _normalise_endpoint_id(peer_endpoint_id_hex)
    for existing_id, entry in admitted_peers.items():
        if _normalise_endpoint_id(existing_id) == target and isinstance(entry, dict):
            return entry
    return None


# CHANGE 2026-07-28 (doc 106 BUILD 6): public resolver so the file-transfer engine
# (tools/file_transfer.py) can turn a caller-supplied peer reference into the admitted
# peer's EndpointId + stored ticket without duplicating den config knowledge.
def den_support_resolve_admitted_peer_reference(peer_reference: str) -> Dict[str, Any]:
    """Resolve a peer reference - 64-hex EndpointId, learned display name, operator
    label, or the 'peer_<8hex>' placeholder - against the admitted_peers allowlist.

    Returns {"endpoint_id" (normalised 64-hex), "last_known_ticket" (may be None),
    "display_name"}. Raises ValueError (listing the known peers) when nothing
    matches, or when a name matches MORE than one admitted peer."""
    reference_text = (peer_reference or "").strip()
    if not reference_text:
        raise ValueError("peer reference is empty")
    reference_text_lowered = reference_text.lower()
    admitted_peers = _read_den_config().get("admitted_peers") or {}
    matched_normalised_ids = []
    matched_entries_by_id: Dict[str, Dict[str, Any]] = {}
    for existing_id, entry in admitted_peers.items():
        normalised_id = _normalise_endpoint_id(existing_id)
        entry_dict = entry if isinstance(entry, dict) else {}
        candidate_reference_texts = {normalised_id, f"peer_{normalised_id[:8]}"}
        for name_field in ("learned_display_name", "name"):
            name_value = entry_dict.get(name_field)
            if isinstance(name_value, str) and name_value.strip():
                candidate_reference_texts.add(name_value.strip().lower())
        if reference_text_lowered in candidate_reference_texts:
            matched_normalised_ids.append(normalised_id)
            matched_entries_by_id[normalised_id] = entry_dict
    if not matched_normalised_ids:
        known_peer_descriptions = ", ".join(
            f"'{_preferred_display_name_for_peer(_normalise_endpoint_id(existing_id))}' ({_normalise_endpoint_id(existing_id)[:16]}...)"
            for existing_id in admitted_peers.keys()) or "(none admitted)"
        raise ValueError(f"no admitted den peer matches '{peer_reference}'. Known peers: {known_peer_descriptions}")
    if len(set(matched_normalised_ids)) > 1:
        raise ValueError(f"peer reference '{peer_reference}' is ambiguous: it matches "
                         f"{len(set(matched_normalised_ids))} admitted peers - use the 64-hex EndpointId instead")
    resolved_normalised_id = matched_normalised_ids[0]
    resolved_entry = matched_entries_by_id[resolved_normalised_id]
    return {"endpoint_id": resolved_normalised_id,
            "last_known_ticket": resolved_entry.get("last_known_ticket"),
            "display_name": _preferred_display_name_for_peer(resolved_normalised_id)}


def _upsert_admitted_peer_entry_preserving_learned_fields(den_section: Dict[str, Any],
                                                          normalised_peer_id: str,
                                                          exposure, friendly_name: Optional[str],
                                                          ticket_string: Optional[str]) -> None:
    """Add or update an admitted_peers entry IN PLACE, preserving fields a plain dict
    replacement would lose (learned_display_name, last_known_ticket). F22: this entry
    is the durable per-peer policy record the lazy umbrella lifecycle hangs off, so
    request_pair/respond_pair must never clobber what earlier sessions taught us."""
    for existing_id, entry in den_section["admitted_peers"].items():
        if _normalise_endpoint_id(existing_id) == normalised_peer_id and isinstance(entry, dict):
            entry["exposure"] = exposure
            if friendly_name:
                entry["name"] = friendly_name
            if ticket_string:
                entry["last_known_ticket"] = ticket_string
            return
    new_entry: Dict[str, Any] = {"exposure": exposure}
    if friendly_name:
        new_entry["name"] = friendly_name
    if ticket_string:
        new_entry["last_known_ticket"] = ticket_string
    den_section["admitted_peers"][normalised_peer_id] = new_entry


def _list_locally_shareable_tool_names() -> List[str]:
    """Real local tools eligible to share (excludes plumbing + umbrella tools)."""
    server = _get_server()
    if server is None:
        return []
    with _den_sessions_lock:
        umbrella_names = set(_registered_umbrella_tool_names)
    shareable = []
    for tool_name in list(server.tool_handlers.keys()):
        if tool_name in NEVER_SHAREABLE_TOOL_NAMES or tool_name in umbrella_names:
            continue
        shareable.append(tool_name)
    return shareable


def _compute_exposed_tool_names_for_peer(peer_endpoint_id_hex: str) -> List[str]:
    """The tools THIS peer is allowed to use. Empty unless an allowlist entry grants it.

    Exposure is driven ONLY by the peer's admitted_peers entry, never by admission
    policy - so even under accept_all an ungranted peer sees nothing.
    """
    den_config = _read_den_config()
    entry = _get_admitted_peer_entry(peer_endpoint_id_hex, den_config)
    if entry is None:
        return []
    exposure = entry.get("exposure", "all")
    shareable = _list_locally_shareable_tool_names()
    if exposure == "all":
        return shareable
    if isinstance(exposure, dict) and isinstance(exposure.get("only"), list):
        allowed = {str(name) for name in exposure["only"]}
        return [name for name in shareable if name in allowed]
    # Unrecognised exposure shape -> safest interpretation is nothing.
    return []


def _build_self_identity_payload(peer_endpoint_id_hex: str) -> Dict[str, Any]:
    # NOTE: this runs ON the iroh loop thread (both hello paths), so it must use the
    # lock-only endpoint-id getter, never den_support_get_endpoint_status() (which
    # marshals onto the loop and would self-deadlock here).
    exposed = _compute_exposed_tool_names_for_peer(peer_endpoint_id_hex)
    return {
        "display_name": _self_display_name(),
        "endpoint_id": _peer().den_support_get_bound_endpoint_id_hex(),
        "exposed_tool_names": exposed,
        # Optional identity metadata to help the OWNER tell their devices apart in a
        # picker ("what's what"). Servers fill the basics; device clients (Android app)
        # fill richer fields (model, marketing_name, phone_number, carrier, account,
        # battery, wifi) per the naming recipe in android_mcp/91_...handoff.md. All
        # fields optional; peers ignore unknown keys. PRIVACY: sensitive fields
        # (phone_number/account) are for the owner's OWN devices; once account-scoping
        # lands, redact them for peers outside the owner's account.
        "device_info": _build_self_device_info(),
    }


def _build_self_device_info() -> Dict[str, Any]:
    """Best-effort self identity for the owner's device picker (see doc 91). Cheap +
    non-blocking (safe on the iroh loop thread). Servers report host/OS; the Android
    app overrides `kind` and adds phone_number/carrier/account/battery/wifi."""
    device_info: Dict[str, Any] = {"kind": "server"}
    try:
        device_info["os"] = platform.system()
        device_info["os_version"] = platform.release()
        device_info["arch"] = platform.machine()
        device_info["host"] = socket.gethostname().split(".")[0]
    except Exception:
        pass
    return device_info


# ----------------------------------------------------------------------------------
# Frame I/O (all runs ON the iroh loop thread)
# ----------------------------------------------------------------------------------
async def _send_frame_on_loop(record: _DenSessionRecord, frame_object: Dict[str, Any]) -> None:
    payload = json.dumps(frame_object, separators=(",", ":")).encode("utf-8")
    if len(payload) > DEN_MAX_FRAME_BYTES:
        raise ValueError(f"den frame is {len(payload)} bytes; max is {DEN_MAX_FRAME_BYTES}")
    framed = len(payload).to_bytes(4, "big") + payload
    async with record.send_frame_serialising_lock:
        await record.send_stream.write_all(framed)
    record.last_frame_activity_unix_time = time.time()  # F23: sends reset the idle clock


def _send_frame_from_worker_thread(record: _DenSessionRecord, frame_object: Dict[str, Any]) -> None:
    """Marshal a frame send onto the iroh loop from a server worker thread and wait."""
    _peer().den_support_run_on_iroh_loop(_send_frame_on_loop(record, frame_object), timeout_seconds=15.0)


# ----------------------------------------------------------------------------------
# Session lifecycle
# ----------------------------------------------------------------------------------
# F23: single idle-reaper task per process, started lazily with the first session (both
# registration paths run on the iroh loop, so create_task works; in environments with no
# running loop - e.g. the component-test harness - startup is skipped gracefully).
_session_idle_reaper_task_has_been_started = False


def _register_den_session(record: _DenSessionRecord) -> None:
    global _session_idle_reaper_task_has_been_started
    with _den_sessions_lock:
        _den_sessions_by_id[record.session_id] = record
        reaper_needs_starting = not _session_idle_reaper_task_has_been_started
        if reaper_needs_starting:
            _session_idle_reaper_task_has_been_started = True
    if reaper_needs_starting:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_session_idle_reaper_loop())
            MCPLogger.log(TOOL_LOG_NAME, f"idle reaper started (sweep every {int(DEN_IDLE_REAPER_SWEEP_INTERVAL_SECONDS)}s; F23)")
        except Exception as reaper_start_error:
            with _den_sessions_lock:
                _session_idle_reaper_task_has_been_started = False
            MCPLogger.log(TOOL_LOG_NAME, f"idle reaper not started ({reaper_start_error!r})")


async def _session_idle_reaper_loop() -> None:
    """F23: periodic sweep (on the iroh loop) closing sessions that sat idle beyond the
    grace, when the resolved stance says teardown is active. Runs for the process life."""
    while True:
        try:
            await asyncio.sleep(DEN_IDLE_REAPER_SWEEP_INTERVAL_SECONDS)
            _sweep_idle_sessions_once()
        except asyncio.CancelledError:
            raise
        except Exception as sweep_error:
            MCPLogger.log(TOOL_LOG_NAME, f"idle reaper sweep failed: {sweep_error!r}")


def _sweep_idle_sessions_once() -> None:
    """One F23 sweep. Never closes a session with in-flight work (outbound waiters or
    inbound serves); any frame in either direction resets a session's idle clock. The
    close is a normal teardown with reason b"idle" - benign for the peer, and the
    umbrella stays (F22), so the only observable effect is a sub-second re-dial on the
    NEXT call after idling."""
    if not _idle_teardown_is_active_right_now():
        return
    grace_seconds = _configured_session_idle_grace_seconds()
    now_unix_time = time.time()
    with _den_sessions_lock:
        session_records = list(_den_sessions_by_id.values())
    for record in session_records:
        idle_seconds = now_unix_time - record.last_frame_activity_unix_time
        if idle_seconds <= grace_seconds:
            continue
        with record.pending_reply_waiters_lock:
            session_has_inflight_work = (bool(record.pending_reply_waiters_by_request_id)
                                         or record.inflight_inbound_serve_count > 0)
        if session_has_inflight_work:
            continue
        MCPLogger.log(TOOL_LOG_NAME,
                      f"IDLE teardown: session {record.session_id} idle {int(idle_seconds)}s > {int(grace_seconds)}s "
                      f"(stance {_configured_session_idle_policy()}; F23) - closing; umbrella unaffected (F22)")
        _teardown_den_session(record, f"idle {int(idle_seconds)}s (F23 stance)",
                              close_reason_bytes=DEN_IDLE_TEARDOWN_CLOSE_REASON_BYTES)


def _make_session_id(peer_endpoint_id_hex: str) -> str:
    global _den_session_sequence
    with _den_sessions_lock:
        _den_session_sequence += 1
        sequence = _den_session_sequence
    return f"den-{peer_endpoint_id_hex[:12]}-{sequence}"


async def _handle_inbound_den_connection_on_loop(connection) -> None:
    """ALPN handler (installed on peer.py). Runs ON the iroh loop for each inbound
    den-ALPN connection. Enforces admission BEFORE reading any application bytes."""
    try:
        peer_endpoint_id_hex = str(connection.remote_id())
    except Exception as remote_id_error:
        MCPLogger.log(TOOL_LOG_NAME, f"inbound den connection with unreadable remote id ({remote_id_error!r}); closing")
        try:
            connection.close(0, b"no remote id")
        except Exception:
            pass
        return

    den_config = _read_den_config()
    if not _is_peer_admitted(peer_endpoint_id_hex, den_config):
        MCPLogger.log(TOOL_LOG_NAME, f"DENIED inbound den connection from non-admitted peer {peer_endpoint_id_hex[:16]} (policy={den_config.get('admission_policy', 'deny_all')})")
        try:
            connection.close(0, b"not admitted")
        except Exception:
            pass
        return

    try:
        # accept_bi fires once the dialer opens the stream + sends its first bytes
        # (its den_hello / transfer hello). We only reach here for an ADMITTED peer.
        bi_stream = await connection.accept_bi()
        send_stream = bi_stream.send()
        recv_stream = bi_stream.recv()
        # CHANGE 2026-07-28 (doc 106 BUILD 6): PEEK the first frame to route this
        # connection. A control session's first frame is JSON (first body byte '{');
        # a file-transfer connection's is a type-tagged frame starting 'H'. Both
        # protocols require the dialer to speak first, so reading it here is safe.
        first_frame_header = await recv_stream.read_exact(4)
        first_frame_length = int.from_bytes(first_frame_header, "big")
        if first_frame_length <= 0 or first_frame_length > DEN_MAX_FRAME_BYTES:
            raise ValueError(f"bad first frame length {first_frame_length}")
        first_frame_body = await recv_stream.read_exact(first_frame_length)
        if first_frame_body[:1] == b"H":
            # File transfer: hand the whole connection to the transfer engine, which
            # runs on its own worker thread (lazy import so a broken file_transfer
            # module can never take den control sessions down with it).
            from ragtag.tools import file_transfer as file_transfer_tool
            file_transfer_tool.accept_inbound_transfer_connection_from_den(
                connection, send_stream, recv_stream, first_frame_body, peer_endpoint_id_hex)
            MCPLogger.log(TOOL_LOG_NAME, f"routed inbound connection from {peer_endpoint_id_hex[:16]} to file transfer (doc 106)")
            return
        record = _DenSessionRecord(
            _make_session_id(peer_endpoint_id_hex), connection,
            send_stream, recv_stream, peer_endpoint_id_hex, we_dialed_this_peer=False)
        _register_den_session(record)
        MCPLogger.log(TOOL_LOG_NAME, f"ACCEPTED den session {record.session_id} from admitted peer {peer_endpoint_id_hex[:16]}")
        # The peeked first frame (normally the dialer's den_hello) still has to be
        # dispatched - the read loop only sees frames AFTER it.
        try:
            first_frame_object = json.loads(first_frame_body.decode("utf-8"))
            record.last_frame_activity_unix_time = time.time()
            _dispatch_inbound_frame(record, first_frame_object)
        except Exception as first_frame_decode_error:
            MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} bad first frame ({first_frame_decode_error!r})")
        await _session_read_loop(record)
    except Exception as accept_error:
        MCPLogger.log(TOOL_LOG_NAME, f"inbound den session setup failed for {peer_endpoint_id_hex[:16]}: {accept_error!r}")
        try:
            connection.close(0, b"setup failed")
        except Exception:
            pass


async def _establish_outbound_den_session_on_loop(ticket_string: Optional[str],
                                                     peer_endpoint_id_hex: Optional[str]) -> Dict[str, Any]:
    """Dial a peer on the den ALPN, open the stream, send our hello, start reading.
    Returns a small summary; the read loop then runs for the session's lifetime."""
    alpn_bytes = DEN_MCP_SESSION_ALPN.encode("utf-8")
    connection = await _peer().den_support_dial_connection_on_loop(
        ticket_string, peer_endpoint_id_hex, alpn_bytes)
    resolved_peer_id = str(connection.remote_id())
    bi_stream = await connection.open_bi()
    record = _DenSessionRecord(
        _make_session_id(resolved_peer_id), connection,
        bi_stream.send(), bi_stream.recv(), resolved_peer_id, we_dialed_this_peer=True)
    _register_den_session(record)
    # Start the read loop first so the acceptor's hello_result is not missed, then
    # send our hello (which also triggers the acceptor's accept_bi()).
    asyncio.create_task(_session_read_loop(record))
    await _send_frame_on_loop(record, {"den_hello": _build_self_identity_payload(resolved_peer_id)})
    MCPLogger.log(TOOL_LOG_NAME, f"DIALED den session {record.session_id} to {resolved_peer_id[:16]}")
    return {"session_id": record.session_id, "peer_endpoint_id": resolved_peer_id}


async def _session_read_loop(record: _DenSessionRecord) -> None:
    """Read framed JSON from one peer and demux until the stream ends."""
    disconnect_reason = "stream closed"
    try:
        while True:
            header = await record.recv_stream.read_exact(4)
            frame_length = int.from_bytes(header, "big")
            if frame_length <= 0 or frame_length > DEN_MAX_FRAME_BYTES:
                disconnect_reason = f"bad frame length {frame_length}"
                break
            body = await record.recv_stream.read_exact(frame_length)
            record.last_frame_activity_unix_time = time.time()  # F23: receives reset the idle clock
            try:
                frame_object = json.loads(body.decode("utf-8"))
            except Exception as decode_error:
                MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} bad frame ({decode_error!r})")
                continue
            _dispatch_inbound_frame(record, frame_object)
    except asyncio.CancelledError:
        disconnect_reason = "cancelled"
    except Exception as read_error:
        disconnect_reason = f"stream ended ({read_error!r})"
    _teardown_den_session(record, disconnect_reason)


def _dispatch_inbound_frame(record: _DenSessionRecord, frame_object: Dict[str, Any]) -> None:
    """Route one decoded frame. Runs ON the iroh loop; must never block it."""
    if not isinstance(frame_object, dict):
        return
    if "den_hello" in frame_object:
        # A dialing peer greeted us: register their umbrella, then reply with ours.
        _register_or_update_peer_umbrella(record, frame_object.get("den_hello") or {})
        asyncio.create_task(_reply_hello_result(record, frame_object.get("id")))
        return
    if "den_hello_result" in frame_object:
        _register_or_update_peer_umbrella(record, frame_object.get("den_hello_result") or {})
        return
    if "den_call" in frame_object:
        asyncio.create_task(_serve_inbound_den_call(record, frame_object))
        return
    if "den_result" in frame_object:
        _resolve_pending_reply(record, frame_object)
        return
    if frame_object.get("method") == "ping":
        return  # QUIC keeps the link alive; nothing to answer for v0
    MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} unknown frame keys: {sorted(frame_object.keys())}")


async def _reply_hello_result(record: _DenSessionRecord, request_id) -> None:
    try:
        payload = _build_self_identity_payload(record.peer_endpoint_id_hex)
        await _send_frame_on_loop(record, {"den_hello_result": payload, "id": request_id})
    except Exception as reply_error:
        MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} hello_result send failed: {reply_error!r}")


def _register_or_update_peer_umbrella(record: _DenSessionRecord, identity_payload: Dict[str, Any]) -> None:
    """A den_hello arrived: learn the peer's self-announced identity, persist the
    learned display name, and (re)register its peer-owned umbrella tool (F22)."""
    peer_display_name = identity_payload.get("display_name") or f"peer_{record.peer_endpoint_id_hex[:8]}"
    exposed_tool_names = identity_payload.get("exposed_tool_names") or []
    record.peer_display_name = peer_display_name
    if isinstance(identity_payload.get("device_info"), dict):
        record.peer_device_info = identity_payload["device_info"]

    # NEWEST-WINS (operator bug report 2026-07-24): a peer that re-dials before its old
    # session's read loop has noticed the drop would leave two live records for the same
    # EndpointId. Tear down any older session from this peer first (idempotent; closes
    # its connection). F22: this no longer touches the umbrella - it is peer-owned, so
    # the '-2' suffix collision the old session-owned model risked cannot happen at all.
    with _den_sessions_lock:
        stale_same_peer_records = [
            existing for existing in _den_sessions_by_id.values()
            if existing.session_id != record.session_id
            and existing.peer_endpoint_id_hex.lower() == record.peer_endpoint_id_hex.lower()
        ]
    for stale_record in stale_same_peer_records:
        _teardown_den_session(stale_record, "replaced by a newer session from the same peer")

    # Persist the learned name so restarts register the umbrella under its real name
    # instead of the 'peer_<8hex>' placeholder (F22: umbrellas exist before sessions).
    _persist_learned_peer_display_name_if_changed(record.peer_endpoint_id_hex, peer_display_name)
    _ensure_umbrella_tool_registered_for_peer(
        record.peer_endpoint_id_hex, hello_display_name=peer_display_name,
        exposed_tool_names=exposed_tool_names, record=record)


def _preferred_display_name_for_peer(normalised_peer_id: str) -> str:
    """Best display name for a peer with NO hello in hand (startup/admission time):
    the name learned from its last hello (persisted), else the operator/portal-supplied
    label, else a 'peer_<8hex>' placeholder (replaced on the first hello)."""
    entry = _get_admitted_peer_entry(normalised_peer_id, _read_den_config())
    if isinstance(entry, dict):
        learned_display_name = entry.get("learned_display_name")
        if isinstance(learned_display_name, str) and learned_display_name.strip():
            return learned_display_name.strip()
        operator_supplied_label = entry.get("name")
        if isinstance(operator_supplied_label, str) and operator_supplied_label.strip():
            return operator_supplied_label.strip()
    return f"peer_{normalised_peer_id[:8]}"


def _persist_learned_peer_display_name_if_changed(peer_endpoint_id_hex: str,
                                                  hello_display_name: str) -> None:
    """Store the peer's self-announced display name in its allowlist entry, so restarts
    register its umbrella under the real name instead of 'peer_<8hex>'. Runs on the
    iroh loop thread (hello path): the write is a small local config file and happens
    only when the name actually changed (rare), so it will not stall the loop."""
    if not hello_display_name:
        return
    normalised_peer_id = _normalise_endpoint_id(peer_endpoint_id_hex)
    entry = _get_admitted_peer_entry(normalised_peer_id, _read_den_config())
    if entry is None or entry.get("learned_display_name") == hello_display_name:
        return

    def _store_learned_name(den_section: Dict[str, Any]) -> None:
        for existing_id, existing_entry in den_section["admitted_peers"].items():
            if _normalise_endpoint_id(existing_id) == normalised_peer_id and isinstance(existing_entry, dict):
                existing_entry["learned_display_name"] = hello_display_name
                return
    _update_den_config(_store_learned_name)


def _register_umbrella_tools_for_all_admitted_peers() -> None:
    """Startup half of F22: every admitted peer's umbrella exists from boot (sessions
    form lazily), so a restart no longer hides peers until they happen to reconnect."""
    admitted_peers = _read_den_config().get("admitted_peers") or {}
    for endpoint_id in list(admitted_peers.keys()):
        _ensure_umbrella_tool_registered_for_peer(endpoint_id)
    if admitted_peers:
        MCPLogger.log(TOOL_LOG_NAME, f"registered {len(admitted_peers)} umbrella tool(s) for admitted peers at startup")


def _ensure_umbrella_tool_registered_for_peer(peer_endpoint_id_hex: str,
                                              hello_display_name: Optional[str] = None,
                                              exposed_tool_names: Optional[List[str]] = None,
                                              record: Optional[_DenSessionRecord] = None) -> Optional[str]:
    """Register (or rename/refresh) THE umbrella tool for a peer - idempotent, and the
    single registration path for startup, admission ops, and the hello path (F22).
    Returns the registered tool name (None if the server is not ready / registration
    failed). When the peer's display name changed (e.g. first hello after a placeholder
    registration), the old tool name is freed and the umbrella re-registered under the
    new name."""
    server = _get_server()
    if server is None:
        return None
    normalised_peer_id = _normalise_endpoint_id(peer_endpoint_id_hex)
    display_name = (hello_display_name or "").strip() or _preferred_display_name_for_peer(normalised_peer_id)
    desired_base_tool_name = _sanitize_to_mcp_tool_name(display_name)

    with _den_sessions_lock:
        existing_tool_name = _umbrella_tool_name_by_peer_id.get(normalised_peer_id)
    if existing_tool_name and (existing_tool_name == desired_base_tool_name
                               or existing_tool_name.startswith(desired_base_tool_name + "-")):
        # Same name (possibly '-N' suffixed by an earlier collision): keep it stable.
        umbrella_tool_name = existing_tool_name
    else:
        if existing_tool_name:
            # Display name changed: free the old tool name BEFORE resolving the new one
            # (so the collision resolver cannot collide with our own stale entry).
            with _den_sessions_lock:
                _registered_umbrella_tool_names.discard(existing_tool_name)
            try:
                server.tool_handlers.pop(existing_tool_name, None)
            except Exception:
                pass
        umbrella_tool_name = _resolve_umbrella_tool_name(desired_base_tool_name)
        with _den_sessions_lock:
            _umbrella_tool_name_by_peer_id[normalised_peer_id] = umbrella_tool_name
            _registered_umbrella_tool_names.add(umbrella_tool_name)

    if exposed_tool_names:
        tool_summary = ", ".join(exposed_tool_names[:8])
    else:
        tool_summary = "(session not formed yet - they appear on the first call)"
    description = (f"Umbrella for den peer '{display_name}' "
                   f"({normalised_peer_id[:12]}...). Its shared tools: {tool_summary}. "
                   f"Call {{\"input\":{{\"operation\":\"readme\"}}}} first for the token and usage.")
    input_schema = {
        "properties": {
            "input": {
                "type": "object",
                "description": "Single-dict input. operation: readme|list_tools|readme_tool|call. For call: {\"operation\":\"call\",\"tool\":\"<peer tool>\",\"tool_input\":{...peer tool args...},\"tool_unlock_token\":\"...\"}."
            }
        },
        "required": [],
        "type": "object"
    }
    try:
        server.register_tool(
            name=umbrella_tool_name,
            description=description,
            input_schema=input_schema,
            handler=_make_umbrella_tool_handler(normalised_peer_id, display_name),
            title=f"Den: {display_name}",
        )
        if record is not None:
            record.local_umbrella_tool_name = umbrella_tool_name
        MCPLogger.log(TOOL_LOG_NAME, f"umbrella tool '{umbrella_tool_name}' ready for peer '{display_name}' "
                      f"({len(exposed_tool_names or [])} tools exposed to us)")
        server.schedule_tools_list_changed_notification_after_collapse_window()
        return umbrella_tool_name
    except Exception as register_error:
        MCPLogger.log(TOOL_LOG_NAME, f"failed to register umbrella tool '{umbrella_tool_name}': {register_error!r}")
        return None


def _remove_umbrella_tool_registration_for_peer(peer_endpoint_id_hex: str) -> None:
    """Unregister a peer's umbrella tool + forget its name/dial lock. F22: called ONLY
    when the peer stops being admitted (kick), or when an accept_all guest with no
    allowlist entry drops - never for an ordinary session drop of an admitted peer."""
    normalised_peer_id = _normalise_endpoint_id(peer_endpoint_id_hex)
    with _den_sessions_lock:
        umbrella_tool_name = _umbrella_tool_name_by_peer_id.pop(normalised_peer_id, None)
        if umbrella_tool_name:
            _registered_umbrella_tool_names.discard(umbrella_tool_name)
    with _lazy_dial_locks_dict_lock:
        _lazy_dial_locks_by_peer_id.pop(normalised_peer_id, None)
    if not umbrella_tool_name:
        return
    server = _get_server()
    if server is not None:
        try:
            server.tool_handlers.pop(umbrella_tool_name, None)
            server.schedule_tools_list_changed_notification_after_collapse_window()
            MCPLogger.log(TOOL_LOG_NAME, f"unregistered umbrella tool '{umbrella_tool_name}'")
        except Exception as unregister_error:
            MCPLogger.log(TOOL_LOG_NAME, f"failed to unregister umbrella tool '{umbrella_tool_name}': {unregister_error!r}")


def _resolve_umbrella_tool_name(base_name: str) -> str:
    """Avoid collisions with built-ins and other umbrellas by appending -2, -3, ..."""
    server = _get_server()

    def _is_taken(candidate: str) -> bool:
        with _den_sessions_lock:
            if candidate in _registered_umbrella_tool_names:
                return True
        return bool(server) and candidate in server.tool_handlers

    if not _is_taken(base_name):
        return base_name
    counter = 2
    while _is_taken(f"{base_name}-{counter}"):
        counter += 1
    return f"{base_name}-{counter}"


def _teardown_den_session(record: _DenSessionRecord, reason: str,
                          close_reason_bytes: bytes = b"den session closed") -> None:
    """End one SESSION (transport). F22: an admitted peer's umbrella tool is peer-owned
    and is NOT removed here - it goes only via kick/un-admit. Only an accept_all guest
    with no allowlist entry loses its umbrella with its session (pre-F22 behavior).
    Fails any blocked waiters and closes the connection (idempotent). close_reason_bytes
    rides in the QUIC close so the PEER knows why (F23 idle teardowns send b"idle")."""
    with _den_sessions_lock:
        already_gone = record.session_id not in _den_sessions_by_id
        _den_sessions_by_id.pop(record.session_id, None)
    if already_gone:
        return
    MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} to {record.peer_endpoint_id_hex[:16]} closed ({reason})")

    if _get_admitted_peer_entry(record.peer_endpoint_id_hex, _read_den_config()) is None:
        _remove_umbrella_tool_registration_for_peer(record.peer_endpoint_id_hex)
    else:
        MCPLogger.log(TOOL_LOG_NAME, f"umbrella retained for admitted peer {record.peer_endpoint_id_hex[:16]} (lazy re-dial on next call)")

    # Wake any threads blocked waiting for replies on this dead session (and stop
    # new waiters being added, via is_active under the same lock).
    with record.pending_reply_waiters_lock:
        record.is_active = False
        orphaned_waiters = list(record.pending_reply_waiters_by_request_id.values())
        record.pending_reply_waiters_by_request_id.clear()
    for waiter in orphaned_waiters:
        waiter["result"] = {"error": f"den session closed ({reason})"}
        waiter["event"].set()

    try:
        record.connection.close(0, close_reason_bytes)
    except Exception:
        pass


# ----------------------------------------------------------------------------------
# Serving a peer's den_call (WE run one of OUR exposed tools for the peer)
# ----------------------------------------------------------------------------------
async def _serve_inbound_den_call(record: _DenSessionRecord, frame_object: Dict[str, Any]) -> None:
    request_id = frame_object.get("id")
    call = frame_object.get("den_call") or {}
    loop = asyncio.get_running_loop()
    # F23: mark the serve in-flight so the idle reaper cannot close this session while a
    # slow local tool (e.g. terminal) is still producing the peer's answer.
    with record.pending_reply_waiters_lock:
        record.inflight_inbound_serve_count += 1
    try:
        try:
            result_object = await loop.run_in_executor(
                _get_serve_dispatch_thread_pool(),
                _serve_den_call_blocking, record.peer_endpoint_id_hex, call)
        except Exception as serve_error:
            result_object = {"error": f"serve failed: {serve_error!r}"}
        try:
            await _send_frame_on_loop(record, {"den_result": result_object, "id": request_id})
        except Exception as send_error:
            MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} den_result send failed: {send_error!r}")
    finally:
        with record.pending_reply_waiters_lock:
            record.inflight_inbound_serve_count -= 1


def _serve_den_call_blocking(peer_endpoint_id_hex: str, call: Dict[str, Any]) -> Dict[str, Any]:
    """Runs in a worker thread. Enforces exposure AT CALL TIME, then runs the tool."""
    operation = call.get("operation")
    server = _get_server()
    if server is None:
        return {"error": "server not ready"}

    exposed = set(_compute_exposed_tool_names_for_peer(peer_endpoint_id_hex))

    if operation == "list_tools":
        tools_summary = []
        for tool_name in sorted(exposed):
            tool_info = server.tool_handlers.get(tool_name) or {}
            tools_summary.append({
                "name": tool_name,
                "description": (tool_info.get("description") or "")[:400],
            })
        return {"tools": tools_summary}

    requested_tool = call.get("tool")
    if not isinstance(requested_tool, str) or not requested_tool:
        return {"error": "missing 'tool'"}
    if requested_tool not in exposed:
        # Re-check independent of what list_tools advertised: a peer cannot reach a
        # tool we did not expose to it (the core exposure guarantee).
        MCPLogger.log(TOOL_LOG_NAME, f"peer {peer_endpoint_id_hex[:16]} tried non-exposed tool '{requested_tool}' - denied")
        return {"error": f"tool '{requested_tool}' is not exposed to you"}

    if operation in ("readme", "readme_tool"):
        inner_input = {"operation": "readme"}
    elif operation == "call":
        # Pass the caller's arguments straight through - including whatever
        # tool_unlock_token they supplied. The unlock token is a comprehension gate
        # (proof the calling agent read THIS tool's docs), NOT a secret, so we do NOT
        # inject it here: a caller that has not read the tool must get its readme back
        # (which carries the token) rather than silently succeeding on guessed params.
        # The agent obtains the token via the umbrella's readme_tool operation, which
        # relays this tool's readme (token included) over the den.
        inner_input = call.get("input")
        if not isinstance(inner_input, dict):
            inner_input = {}
    else:
        return {"error": f"unknown operation '{operation}'"}

    try:
        result = server.call_tool_internal(
            requested_tool, {"input": inner_input}, calling_tool=f"den:{peer_endpoint_id_hex[:12]}")
        return {"result": result}
    except Exception as call_error:
        return {"error": f"call failed: {call_error!r}"}


# ----------------------------------------------------------------------------------
# The local umbrella tool handler (a local client calls the PEER via this)
# ----------------------------------------------------------------------------------
def _find_live_session_record_for_peer(normalised_peer_id: str) -> Optional[_DenSessionRecord]:
    """Newest active session for this peer, or None. F22: sessions are located by PEER
    at call time - the umbrella no longer pins one session id for its lifetime."""
    with _den_sessions_lock:
        candidate_records = [
            record for record in _den_sessions_by_id.values()
            if _normalise_endpoint_id(record.peer_endpoint_id_hex) == normalised_peer_id
            and record.is_active
        ]
    if not candidate_records:
        return None
    return max(candidate_records, key=lambda candidate: candidate.created_at_unix_time)


def _get_lazy_dial_lock_for_peer(normalised_peer_id: str) -> threading.Lock:
    with _lazy_dial_locks_dict_lock:
        dial_lock = _lazy_dial_locks_by_peer_id.get(normalised_peer_id)
        if dial_lock is None:
            dial_lock = threading.Lock()
            _lazy_dial_locks_by_peer_id[normalised_peer_id] = dial_lock
        return dial_lock


def _get_or_establish_live_session_record_for_peer_blocking(normalised_peer_id: str) -> _DenSessionRecord:
    """The F22 lazy-session core: return the live session for this peer, dialing one on
    the spot if none exists. Runs on a server worker thread (blocks up to ~45s per dial
    attempt). Tries the peer's last_known_ticket first (carries direct addresses, so
    LAN dials work without relay enrollment), then falls back to a relay dial by
    EndpointId. Raises RuntimeError with all attempt errors on total failure."""
    live_record = _find_live_session_record_for_peer(normalised_peer_id)
    if live_record is not None:
        return live_record
    _peer().den_support_ensure_endpoint_started()
    with _get_lazy_dial_lock_for_peer(normalised_peer_id):
        # Another call may have completed the (re)dial while we waited for the lock.
        live_record = _find_live_session_record_for_peer(normalised_peer_id)
        if live_record is not None:
            return live_record
        entry = _get_admitted_peer_entry(normalised_peer_id, _read_den_config())
        stored_ticket = entry.get("last_known_ticket") if isinstance(entry, dict) else None
        dial_attempt_error_texts: List[str] = []
        for ticket_to_try in ([stored_ticket, None] if stored_ticket else [None]):
            try:
                session_summary = _peer().den_support_run_on_iroh_loop(
                    _establish_outbound_den_session_on_loop(ticket_to_try, normalised_peer_id),
                    timeout_seconds=45.0)
            except Exception as dial_error:
                dial_attempt_error_texts.append(
                    f"{'ticket' if ticket_to_try else 'relay-by-id'} dial failed: {dial_error!r}")
                continue
            resolved_peer_id = _normalise_endpoint_id(session_summary.get("peer_endpoint_id"))
            with _den_sessions_lock:
                new_record = _den_sessions_by_id.get(session_summary.get("session_id"))
            if resolved_peer_id != normalised_peer_id:
                # A stale ticket reached a DIFFERENT endpoint: refuse it (this umbrella
                # must only ever talk to ITS peer) and fall back to relay-by-id.
                if new_record is not None:
                    _teardown_den_session(new_record, "lazy dial reached an unexpected endpoint")
                dial_attempt_error_texts.append(
                    f"ticket reached unexpected endpoint {resolved_peer_id[:16]}")
                continue
            if new_record is not None:
                return new_record
            dial_attempt_error_texts.append("session closed immediately after dial")
        raise RuntimeError("; ".join(dial_attempt_error_texts) or "dial failed")


def _make_umbrella_tool_handler(peer_endpoint_id_hex: str, peer_display_label: str):
    # F22: the handler is PEER-bound, not session-bound. Its unlock token is keyed by
    # the peer's EndpointId so it stays STABLE across session drops and server restarts
    # (it still rotates when den.py itself changes, because TOOL_UNLOCK_TOKEN does).
    normalised_peer_id = _normalise_endpoint_id(peer_endpoint_id_hex)
    umbrella_unlock_token = hmac.new(
        TOOL_UNLOCK_TOKEN.encode("utf-8"),
        f"umbrella:{normalised_peer_id}".encode("utf-8"),
        hashlib.sha256).hexdigest()[-8:]

    def _umbrella_readme() -> Dict[str, Any]:
        text = (f"Umbrella tool for den peer '{peer_display_label}'.\n\n"
                f"tool_unlock_token: {umbrella_unlock_token}\n\n"
                "Operations (this tool's own tool_unlock_token, above, is required on all\n"
                "except readme):\n"
                "  {\"operation\":\"list_tools\",\"tool_unlock_token\":\"...\"}  - list the peer's shared tools\n"
                "  {\"operation\":\"readme_tool\",\"tool\":\"<name>\",\"tool_unlock_token\":\"...\"}  - that tool's\n"
                "        full readme, INCLUDING that tool's own unlock token\n"
                "  {\"operation\":\"call\",\"tool\":\"<name>\",\"tool_input\":{...},\"tool_unlock_token\":\"...\"}  - run it\n"
                "TWO-TOKEN RULE: 'call' needs TWO tokens - this umbrella's token as\n"
                "tool_unlock_token, AND the peer tool's OWN token inside tool_input (that\n"
                "tool's args, not 'input'). Get the peer tool's token + parameters from\n"
                "readme_tool first; do not guess. Example:\n"
                "  {\"operation\":\"call\",\"tool\":\"python\",\"tool_unlock_token\":\"" + umbrella_unlock_token + "\",\n"
                "   \"tool_input\":{\"operation\":\"execute\",\"code\":\"...\",\"tool_unlock_token\":\"<python's token>\"}}\n"
                "Sessions are LAZY (doc/50 F22): if no live session exists, calling this\n"
                "umbrella dials the peer on the spot; it errors only if the peer is\n"
                "unreachable right now (the umbrella itself stays - just retry later).\n")
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def handler(tool_args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if isinstance(tool_args, dict):
                tool_args.pop("handler_info", None)
            # The server may deliver our args wrapped ({"input": {...}}) or already
            # unwrapped one level. Unwrap "input" wrappers ONLY until we reach the dict
            # carrying our umbrella "operation", so the call operation's OWN nested
            # "input" (the peer tool's arguments) is preserved rather than consumed.
            inner = tool_args if isinstance(tool_args, dict) else {}
            while "operation" not in inner and isinstance(inner.get("input"), dict):
                inner = inner["input"]
            operation = inner.get("operation")

            if operation == "readme":
                return _umbrella_readme()
            if inner.get("tool_unlock_token") != umbrella_unlock_token:
                readme_result = _umbrella_readme()
                readme_result["isError"] = True
                return readme_result

            # F22 lazy session: use the live session if one exists, else (re)dial NOW.
            try:
                record = _get_or_establish_live_session_record_for_peer_blocking(normalised_peer_id)
            except Exception as establish_error:
                return {"content": [{"type": "text", "text": _append_admission_hint_if_peer_refused_us(
                        f"Den peer '{peer_display_label}' is not reachable right now ({establish_error}). "
                        "It stays admitted - this umbrella remains registered and the session "
                        "re-forms on a later call once the peer is online.")}], "isError": True}

            if operation == "list_tools":
                reply = _call_peer_and_wait(record, {"operation": "list_tools"})
                return _format_umbrella_reply(reply)
            if operation in ("readme_tool", "readme"):
                target_tool = inner.get("tool")
                reply = _call_peer_and_wait(record, {"operation": "readme", "tool": target_tool})
                return _format_umbrella_reply(reply)
            if operation == "call":
                target_tool = inner.get("tool")
                if not isinstance(target_tool, str) or not target_tool:
                    return {"content": [{"type": "text", "text": "Missing 'tool' to call on the peer."}], "isError": True}
                # The peer tool's own arguments MUST be passed under "tool_input", not
                # "input": the server's tools/call path collapses a nested "input"
                # (its double-wrap guard, server.py ~3650), which would strip our call
                # envelope. Accept "input" too as a fallback for hand callers.
                peer_tool_arguments = inner.get("tool_input")
                if not isinstance(peer_tool_arguments, dict):
                    peer_tool_arguments = inner.get("input") if isinstance(inner.get("input"), dict) else {}
                reply = _call_peer_and_wait(
                    record, {"operation": "call", "tool": target_tool, "input": peer_tool_arguments})
                return _format_umbrella_reply(reply)
            return {"content": [{"type": "text", "text": f"Unknown umbrella operation '{operation}'. Use readme."}], "isError": True}
        except Exception as handler_error:
            return {"content": [{"type": "text", "text": f"Umbrella tool error: {handler_error!r}"}], "isError": True}

    return handler


def _append_admission_hint_if_peer_refused_us(error_text: str) -> str:
    """A peer that has NOT admitted this device closes our dial at ITS den admission gate
    with close reason b"not admitted" (den.py's gate; the reason string rides inside the
    iroh ApplicationClosed error we surface). When that signature is present, say what it
    MEANS: the fix is policy (make the peer admit us - pairing is per-direction), not
    retrying. Matched narrowly on "not admitted" so unrelated failures get no bogus hint."""
    if "not admitted" in (error_text or "").lower():
        return (error_text
                + "\nHINT: the peer refused this device at its den ADMISSION gate - it has not "
                "admitted us (each direction is granted separately). Re-wire the link between the "
                "two devices on den.html (or run respond_pair on the peer with THIS device's "
                "peer_id), then retry.")
    return error_text


def _format_umbrella_reply(reply: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a peer's den_result payload into an MCP tool result for the local client."""
    if not isinstance(reply, dict):
        return {"content": [{"type": "text", "text": "No reply from peer."}], "isError": True}
    if "error" in reply:
        return {"content": [{"type": "text", "text": _append_admission_hint_if_peer_refused_us(
            f"Peer error: {reply['error']}")}], "isError": True}
    if "result" in reply and isinstance(reply["result"], dict):
        # The peer already returned a proper MCP tool result - pass it through verbatim.
        return reply["result"]
    if "tools" in reply:
        return {"content": [{"type": "text", "text": json.dumps(reply["tools"], indent=2)}], "isError": False}
    return {"content": [{"type": "text", "text": json.dumps(reply, indent=2)}], "isError": False}


def _call_peer_and_wait(record: _DenSessionRecord, call_object: Dict[str, Any],
                        timeout_seconds: float = DEN_CALL_MAX_WAIT_SECONDS) -> Dict[str, Any]:
    """Send a den_call and block (worker thread) until the matching den_result."""
    request_id = record.allocate_request_id()
    waiter = {"event": threading.Event(), "result": None}
    with record.pending_reply_waiters_lock:
        if not record.is_active:
            return {"error": "den session is not active"}
        record.pending_reply_waiters_by_request_id[request_id] = waiter
    try:
        _send_frame_from_worker_thread(record, {"den_call": call_object, "id": request_id})
    except Exception as send_error:
        with record.pending_reply_waiters_lock:
            record.pending_reply_waiters_by_request_id.pop(request_id, None)
        return {"error": f"failed to send to peer: {send_error!r}"}
    if not waiter["event"].wait(timeout=timeout_seconds):
        with record.pending_reply_waiters_lock:
            record.pending_reply_waiters_by_request_id.pop(request_id, None)
        return {"error": f"peer did not reply within {timeout_seconds:.0f}s"}
    return waiter["result"] if isinstance(waiter["result"], dict) else {"error": "empty reply"}


def _resolve_pending_reply(record: _DenSessionRecord, frame_object: Dict[str, Any]) -> None:
    request_id = frame_object.get("id")
    with record.pending_reply_waiters_lock:
        waiter = record.pending_reply_waiters_by_request_id.pop(request_id, None)
    if waiter is None:
        MCPLogger.log(TOOL_LOG_NAME, f"session {record.session_id} den_result for unknown id {request_id}")
        return
    waiter["result"] = frame_object.get("den_result")
    waiter["event"].set()


# ----------------------------------------------------------------------------------
# Live-session path (DIRECT vs RELAY) for list_pairings
# ----------------------------------------------------------------------------------
async def _snapshot_connection_paths_on_loop(record: _DenSessionRecord) -> List[Dict[str, Any]]:
    path_reports = []
    try:
        for path in record.connection.paths():
            path_reports.append({
                "selected": bool(path.is_selected),
                "kind": "direct" if path.is_ip else ("relay" if path.is_relay else "unknown"),
                "remote_addr": str(path.remote_addr),
                "rtt_ms": path.rtt_ms,
            })
    except Exception as paths_error:
        path_reports.append({"error": f"paths() unavailable: {paths_error!r}"})
    return path_reports


# ----------------------------------------------------------------------------------
# 'den' tool operation handlers (server worker threads)
# ----------------------------------------------------------------------------------
def _mcp_text_result(payload: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": is_error}


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict[str, Any]:
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    text = error_msg + (("\n\n" + json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2)) if with_readme else "")
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _ensure_endpoint_started_or_error() -> Optional[Dict[str, Any]]:
    try:
        _peer().den_support_ensure_endpoint_started()
        return None
    except Exception as start_error:
        return create_error_response(f"could not start iroh endpoint: {start_error}", with_readme=False)


def _handle_status(params: Dict[str, Any]) -> Dict[str, Any]:
    started_error = _ensure_endpoint_started_or_error()
    if started_error is not None:
        return started_error
    den_config = _read_den_config()
    status = _peer().den_support_get_endpoint_status()
    with _den_sessions_lock:
        live_session_count = len(_den_sessions_by_id)
    return _mcp_text_result({
        "my_display_name": _self_display_name(),
        "my_endpoint_id": status.get("endpoint_id"),
        "ticket": status.get("ticket"),
        "relay_ready": status.get("relay_ready"),
        "admission_policy": den_config.get("admission_policy", "deny_all"),
        "admitted_peer_count": len(den_config.get("admitted_peers") or {}),
        "live_session_count": live_session_count,
        # F23 stance report (den.html/portal read these from the sync snapshot).
        "session_idle_policy": _configured_session_idle_policy(),
        "session_idle_grace_seconds": _configured_session_idle_grace_seconds(),
        "device_is_on_battery_power": _device_is_on_battery_power_right_now(),
        "idle_teardown_currently_active": _idle_teardown_is_active_right_now(),
    })


def _handle_list_den(params: Dict[str, Any]) -> Dict[str, Any]:
    den_config = _read_den_config()
    admitted_peers = den_config.get("admitted_peers") or {}
    with _den_sessions_lock:
        live_peer_ids = {record.peer_endpoint_id_hex.lower() for record in _den_sessions_by_id.values()}
        umbrella_tool_names_by_peer = dict(_umbrella_tool_name_by_peer_id)
    peers_report = []
    for endpoint_id, entry in admitted_peers.items():
        entry = entry if isinstance(entry, dict) else {}
        peers_report.append({
            "endpoint_id": endpoint_id,
            "name": entry.get("name"),
            # F22: the name a session hello taught us (umbrellas keep it across restarts).
            "learned_display_name": entry.get("learned_display_name"),
            "umbrella_tool_name": umbrella_tool_names_by_peer.get(_normalise_endpoint_id(endpoint_id)),
            "exposure": entry.get("exposure", "all"),
            "live": endpoint_id.lower() in live_peer_ids,
        })
    return _mcp_text_result({
        "admission_policy": den_config.get("admission_policy", "deny_all"),
        "admitted_peers": peers_report,
    })


def _handle_request_pair(params: Dict[str, Any]) -> Dict[str, Any]:
    started_error = _ensure_endpoint_started_or_error()
    if started_error is not None:
        return started_error
    ticket_string = params.get("ticket")
    peer_id = params.get("peer_id")
    if not ticket_string and not peer_id:
        return create_error_response("request_pair needs a 'ticket' (preferred) or 'peer_id'.")
    exposure = params.get("exposure", "all")
    friendly_name = params.get("name")

    # F22: pairing is POLICY (admit + umbrella); the session is TRANSPORT. When the
    # peer's EndpointId is known, record admission and register the umbrella BEFORE
    # dialing, so a failed dial no longer erases the pairing - the session then forms
    # lazily (on the first umbrella call, or when the peer dials us). This also means
    # the reverse-direction dial from the peer is accepted from this moment on.
    provisional_peer_id = _normalise_endpoint_id(peer_id) if peer_id else None
    if provisional_peer_id:
        def _record_admission(den_section: Dict[str, Any]) -> None:
            _upsert_admitted_peer_entry_preserving_learned_fields(
                den_section, provisional_peer_id, exposure, friendly_name, ticket_string)
        _update_den_config(_record_admission)
        _ensure_umbrella_tool_registered_for_peer(provisional_peer_id)

    dial_error_text = None
    session_summary: Dict[str, Any] = {}
    try:
        session_summary = _peer().den_support_run_on_iroh_loop(
            _establish_outbound_den_session_on_loop(ticket_string, peer_id), timeout_seconds=45.0)
    except Exception as dial_error:
        dial_error_text = f"{dial_error!r}"
        if not provisional_peer_id:
            # Ticket-only call whose dial failed: we never learned WHO the peer is, so
            # there is nothing durable to record - keep the old all-or-nothing reply.
            return create_error_response(f"dial failed: {dial_error!r} (LAN peers: use a ticket; cross-network: both EndpointIds must be relay-enrolled)", with_readme=False)

    resolved_peer_id = _normalise_endpoint_id(session_summary.get("peer_endpoint_id")) or provisional_peer_id
    if resolved_peer_id and resolved_peer_id != provisional_peer_id:
        # Ticket-only path (no peer_id given): record what the dial LEARNED.
        def _record_resolved_admission(den_section: Dict[str, Any]) -> None:
            _upsert_admitted_peer_entry_preserving_learned_fields(
                den_section, resolved_peer_id, exposure, friendly_name, ticket_string)
        _update_den_config(_record_resolved_admission)
        _ensure_umbrella_tool_registered_for_peer(resolved_peer_id)

    session_is_live = bool(session_summary.get("session_id"))
    with _den_sessions_lock:
        umbrella_tool_name = _umbrella_tool_name_by_peer_id.get(resolved_peer_id) if resolved_peer_id else None
    return _mcp_text_result({
        "paired": True,
        "peer_endpoint_id": resolved_peer_id,
        "session_id": session_summary.get("session_id"),
        "session_live": session_is_live,
        "umbrella_tool_name": umbrella_tool_name,
        "exposure": exposure,
        "note": ("Session dialed. " if session_is_live else
                 f"Peer admitted + umbrella registered, but the dial failed ({dial_error_text}); "
                 "the session will form lazily - on the first umbrella call, or when the peer dials us. ")
                + "On the peer, run respond_pair with OUR endpoint_id so it admits us for the reverse direction.",
    })


def _handle_respond_pair(params: Dict[str, Any]) -> Dict[str, Any]:
    started_error = _ensure_endpoint_started_or_error()
    if started_error is not None:
        return started_error
    peer_id = _normalise_endpoint_id(params.get("peer_id"))
    if not peer_id:
        return create_error_response("respond_pair needs the peer's 'peer_id' (64-hex EndpointId).")
    exposure = params.get("exposure", "all")
    friendly_name = params.get("name")

    def _add_entry(den_section: Dict[str, Any]) -> None:
        _upsert_admitted_peer_entry_preserving_learned_fields(
            den_section, peer_id, exposure, friendly_name, None)
    _update_den_config(_add_entry)
    # F22: the umbrella exists from the moment of admission; the session forms lazily.
    umbrella_tool_name = _ensure_umbrella_tool_registered_for_peer(peer_id)
    return _mcp_text_result({
        "admitted": True,
        "peer_endpoint_id": peer_id,
        "exposure": exposure,
        "umbrella_tool_name": umbrella_tool_name,
        "note": "Peer is now admitted and its umbrella tool is registered. The session forms when it dials us, or lazily on the first call through the umbrella.",
    })


def _handle_list_pairings(params: Dict[str, Any]) -> Dict[str, Any]:
    with _den_sessions_lock:
        records = list(_den_sessions_by_id.values())
    pairings = []
    for record in records:
        try:
            paths = _peer().den_support_run_on_iroh_loop(
                _snapshot_connection_paths_on_loop(record), timeout_seconds=5.0)
        except Exception as paths_error:
            paths = [{"error": repr(paths_error)}]
        pairings.append({
            "session_id": record.session_id,
            "peer_endpoint_id": record.peer_endpoint_id_hex,
            "peer_display_name": record.peer_display_name,
            "peer_device_info": record.peer_device_info,
            "umbrella_tool_name": record.local_umbrella_tool_name,
            "we_dialed": record.we_dialed_this_peer,
            "paths": paths,
        })
    return _mcp_text_result({"live_session_count": len(pairings), "pairings": pairings})


def _handle_set_exposure(params: Dict[str, Any]) -> Dict[str, Any]:
    peer_id = _normalise_endpoint_id(params.get("peer_id"))
    if not peer_id:
        return create_error_response("set_exposure needs 'peer_id'.")
    if "exposure" not in params:
        return create_error_response("set_exposure needs 'exposure' (\"all\" or {\"only\":[...]}).")
    exposure = params.get("exposure")

    updated = {"found": False}

    def _update_entry(den_section: Dict[str, Any]) -> None:
        for existing_id, entry in den_section["admitted_peers"].items():
            if _normalise_endpoint_id(existing_id) == peer_id and isinstance(entry, dict):
                entry["exposure"] = exposure
                updated["found"] = True
                return
    _update_den_config(_update_entry)
    if not updated["found"]:
        return create_error_response(f"peer {peer_id[:16]} is not in the allowlist; use request_pair/respond_pair first.", with_readme=False)
    return _mcp_text_result({"peer_endpoint_id": peer_id, "exposure": exposure, "note": "Applies on the next session; existing sessions keep their advertised set until reconnect."})


def _handle_kick(params: Dict[str, Any]) -> Dict[str, Any]:
    peer_id = _normalise_endpoint_id(params.get("peer_id"))
    if not peer_id:
        return create_error_response("kick needs 'peer_id'.")

    def _remove_entry(den_section: Dict[str, Any]) -> None:
        for existing_id in list(den_section["admitted_peers"].keys()):
            if _normalise_endpoint_id(existing_id) == peer_id:
                den_section["admitted_peers"].pop(existing_id, None)
    _update_den_config(_remove_entry)

    closed_sessions = []
    with _den_sessions_lock:
        records = [r for r in _den_sessions_by_id.values() if r.peer_endpoint_id_hex.lower() == peer_id]
    for record in records:
        closed_sessions.append(record.session_id)
        try:
            _peer().den_support_run_on_iroh_loop(_close_den_session_on_loop(record), timeout_seconds=10.0)
        except Exception as close_error:
            MCPLogger.log(TOOL_LOG_NAME, f"kick close failed for {record.session_id}: {close_error!r}")
    # F22: kick is the ONE path that removes an admitted peer's umbrella (allowlist
    # entry is already gone above, so a racing session teardown would also remove it).
    _remove_umbrella_tool_registration_for_peer(peer_id)
    return _mcp_text_result({"kicked": peer_id, "closed_sessions": closed_sessions})


async def _close_den_session_on_loop(record: _DenSessionRecord) -> None:
    try:
        record.connection.close(0, b"kicked")
    except Exception:
        pass


def _handle_set_admission_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    policy = params.get("policy")
    if policy not in ("deny_all", "accept_all"):
        return create_error_response("policy must be 'deny_all' or 'accept_all'.")

    def _set_policy(den_section: Dict[str, Any]) -> None:
        den_section["admission_policy"] = policy
    _update_den_config(_set_policy)
    return _mcp_text_result({"admission_policy": policy, "note": "accept_all still exposes only tools you granted per-peer (default none)."})


def _handle_set_session_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    """F23: set THIS device's idle-teardown stance (and optionally the numeric grace)."""
    requested_idle_policy = params.get("idle_policy")
    if requested_idle_policy not in DEN_VALID_SESSION_IDLE_POLICIES:
        return create_error_response(
            f"idle_policy must be one of {list(DEN_VALID_SESSION_IDLE_POLICIES)} "
            "(auto = warm while powered, idle-close on battery).", with_readme=False)
    requested_grace_raw = params.get("idle_grace_seconds")
    validated_grace_seconds: Optional[float] = None
    if requested_grace_raw is not None:
        try:
            validated_grace_seconds = float(requested_grace_raw)
        except (TypeError, ValueError):
            return create_error_response("idle_grace_seconds must be a number.", with_readme=False)
        if validated_grace_seconds < DEN_MINIMUM_SESSION_IDLE_GRACE_SECONDS:
            return create_error_response(
                f"idle_grace_seconds must be >= {int(DEN_MINIMUM_SESSION_IDLE_GRACE_SECONDS)}.", with_readme=False)

    def _store_session_policy(den_section: Dict[str, Any]) -> None:
        den_section[DEN_SESSION_IDLE_POLICY_CONFIG_KEY] = requested_idle_policy
        if validated_grace_seconds is not None:
            den_section[DEN_SESSION_IDLE_GRACE_CONFIG_KEY] = validated_grace_seconds
    _update_den_config(_store_session_policy)
    return _mcp_text_result({
        "idle_policy": requested_idle_policy,
        "session_idle_grace_seconds": _configured_session_idle_grace_seconds(),
        "device_is_on_battery_power": _device_is_on_battery_power_right_now(),
        "idle_teardown_currently_active": _idle_teardown_is_active_right_now(),
        "note": "Applies from the next reaper sweep (<=30s). Idle closes are benign: umbrellas persist (F22) and the next call re-dials.",
    })


# ----------------------------------------------------------------------------------
# Standard ragtag tool plumbing
# ----------------------------------------------------------------------------------
def readme(with_readme: bool = True) -> str:
    if not with_readme:
        return ""
    return "\n\n" + json.dumps({
        "description": TOOLS[0]["readme"],
        "parameters": TOOLS[0]["real_parameters"],
    }, indent=2)


_OPERATION_DISPATCH_TABLE = {
    "status": _handle_status,
    "list_den": _handle_list_den,
    "request_pair": _handle_request_pair,
    "respond_pair": _handle_respond_pair,
    "list_pairings": _handle_list_pairings,
    "set_exposure": _handle_set_exposure,
    "kick": _handle_kick,
    "set_admission_policy": _handle_set_admission_policy,
    "set_session_policy": _handle_set_session_policy,
}


def handle_den(input_param: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if isinstance(input_param, dict):
            input_param.get("handler_info", {})
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected a dictionary of tool parameters.")

        if input_param.get("tool_unlock_token") != TOOL_UNLOCK_TOKEN:
            return create_error_response(
                "Invalid or missing tool_unlock_token: this indicates your context is missing the "
                "following details, which are needed to correctly use this tool:")

        operation = input_param.get("operation")
        handler = _OPERATION_DISPATCH_TABLE.get(operation)
        if handler is None:
            return create_error_response(f"Unknown operation: '{operation}'.")
        return handler(input_param)
    except Exception as operation_error:
        return create_error_response(f"Error in den operation: {operation_error!r}", with_readme=False)


HANDLERS = {
    TOOL_NAME: handle_den,
}
