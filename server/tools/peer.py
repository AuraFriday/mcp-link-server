"""
File: ragtag/tools/peer.py
Project: Aura Friday MCP-Link Server
Component: Peer Tool (iroh P2P data plane for tunnel.af)
Author: Christopher Nathan Drake (cnd)

MCP tool wrapping the iroh P2P library: lets this Aura Friday instance connect
directly (hole-punched QUIC) or via our self-hosted relay (relay.tunnel.af) to
other Aura Friday instances, and exchange length-prefixed JSON messages.

This is PoC-1 of the tunnel.af data-plane plan (see
tunnel_af/vmo11-iroh-server-setup.md "Agent-ready implementation notes").
The transport shape (bind/connect/accept/open_bi/accept_bi, 4-byte big-endian
length + UTF-8 JSON framing, uniffi_set_event_loop, POLL connection.paths()
instead of watch_paths() which panics from Python) is copied from the proven
reference implementation tunnel_af/iroh-poc/afchat.py.

Architecture inside this module:
  * ONE background daemon thread owns a private asyncio event loop; the iroh
    Endpoint lives on that loop (iroh-ffi requires an asyncio loop, but ragtag
    tool handlers are plain sync functions on server worker threads).
  * Tool operations post coroutines to that loop with
    asyncio.run_coroutine_threadsafe(...) and wait for the result.
  * Received messages land in a bounded thread-safe inbox that the "recv"
    operation drains (optionally blocking up to wait_seconds).

Copyright: (c) 2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƐvƵƲʌď𝟟ŧƟpⲟММƟģСᗷƦɊxEⅼ𝟧TѵßᗪmօѡɊցᏟhmƬꓧⴹτᎠmοАОHoТƦ4𝐴ꓐ2Ꮋꜱ𝟚ꓮоН𝐴aiτƌþaDꓜⲘꓜAᴜ𝟢ᴡ𝟟ꓬ𝟦tÞᎪeOt𝟛ƟYn𝟙𝟦ОᖴОƏXⅼ𝙰ᏴⲔƧՕHⲞЅHƳXꓜaТꓗ"
"signdate": "2026-07-29T09:30:39.739Z",
"""

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from easy_mcp.server import MCPLogger, get_tool_token

# Constants
TOOL_LOG_NAME = "PEER"

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

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"peer{TOOL_NAME_SUFFIX}"

# Production relays: two regions (US=a1lsj San Jose, EU=a1lck Cork), on the aurafriday.com
# domain to avoid a .af TLD dependency (operator decision F12/F13, 2026-07-17). iroh picks the
# nearest as its home relay and fails over automatically. relay.tunnel.af (vmO11) is the LEGACY
# single relay, kept running during cutover; new installs use the pair below.
DEFAULT_RELAY_URLS = [
    "https://relay-us.aurafriday.com",
    "https://relay-eu.aurafriday.com",
]
# Back-compat: some call sites / logs still read DEFAULT_RELAY_URL (the primary/US relay).
DEFAULT_RELAY_URL = DEFAULT_RELAY_URLS[0]
LEGACY_RELAY_URL = "https://relay.tunnel.af"
# Production ALPN per the tunnel.af plan. The afchat.py PoC uses b"af/chat/0" so
# PoC traffic can never be confused with real MCP traffic.
DEFAULT_MCP_PEER_ALPN = "af/mcp/1"
# CHANGE 2026-07-23 (interim Den v0, doc/25 + doc/30): second ALPN for MCP
# tool-sharing sessions between Aura Friday servers. It is bound IN ADDITION to
# the mesh ALPN above, and inbound connections are routed by their negotiated
# ALPN in _accept_one_inbound_connection. tools/den.py owns the protocol
# spoken on this ALPN (admission allowlist, hello/call/result frames).
DEN_MCP_SESSION_ALPN = "af/mcp-session/1"
# Reserved transport-level message type: sent automatically on session setup so the
# other side's accept_bi() fires (QUIC only reveals a new bi-stream to the acceptor
# once the first bytes arrive on it - see afchat.py). NEVER delivered to the inbox.
TRANSPORT_HELLO_MESSAGE_TYPE = "__af_peer_hello__"
MAX_FRAMED_MESSAGE_BYTES = 1 << 20          # 1 MiB per framed message (sanity cap)
RECEIVED_MESSAGE_INBOX_MAX_ENTRIES = 1000   # bounded inbox so an idle server can't grow forever
DEFAULT_OPERATION_TIMEOUT_SECONDS = 30.0    # how long a sync tool call waits on the asyncio loop

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Peer-to-peer link between Aura Friday instances (iroh QUIC: direct hole-punched, or relayed via relay.tunnel.af). Exchange JSON messages with other machines running this server.
- Use this to talk to another Aura Friday MCP server instance on a different machine
""",
        # Standard MCP parameters - simplified to single input dict
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
        # Actual tool parameters - revealed only after readme call
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "start", "status", "connect", "send", "recv", "disconnect", "stop"],
                    "description": "Operation to perform"
                },
                "relay": {
                    "type": "string",
                    "description": "start: relay choice - 'tunnel.af' (default, our paywalled relay), 'n0' (iroh public relays, A/B probe), 'disabled' (direct/LAN only), or any https relay URL"
                },
                "alpn": {
                    "type": "string",
                    "description": "start: application protocol id on the QUIC wire (default 'af/mcp/1'). Override ONLY for interop testing (e.g. 'af/chat/0' to talk to the afchat.py PoC). Both sides must match."
                },
                "key_file": {
                    "type": "string",
                    "description": "start: path of the 32-byte secret-key file holding this endpoint's stable identity (default: <user_data>/peer_iroh_identity.key; created if absent)"
                },
                "ticket": {
                    "type": "string",
                    "description": "connect: the endpoint ticket string printed by the peer's start/status (carries its EndpointId + relay + direct addresses)"
                },
                "peer_id": {
                    "type": "string",
                    "description": "send/disconnect: target peer's 64-hex EndpointId, or 'all' to broadcast/disconnect every connected peer"
                },
                "message": {
                    "type": "object",
                    "description": "send: the JSON object to deliver to the peer(s)"
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "recv: block up to this many seconds for at least one message to arrive (default 0 = return immediately with whatever is queued)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call
        "readme": """
Peer tool - iroh P2P data plane between Aura Friday instances (tunnel.af).

Connects this machine to other Aura Friday instances over iroh QUIC:
DIRECT (hole-punched, peer-to-peer) when the networks allow it, otherwise
RELAYED through our self-hosted relay https://relay.tunnel.af (default-deny
paywall: each endpoint's 64-hex EndpointId must be allowlisted in
websites/tunnel.af/relay-access/allowlist.map, deployed via git commit).
Messages are JSON objects, framed on the wire as a 4-byte big-endian length
prefix + UTF-8 JSON.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Typical session
1. start   - binds the iroh endpoint (stable identity persisted to key_file).
             Returns this machine's EndpointId (allowlist it once for relay use)
             and its ticket (share with a peer so it can connect to you).
2. connect - dial a peer using ITS ticket. Returns the peer_id once connected.
3. send    - deliver a JSON message to one peer (or 'all').
4. recv    - collect queued incoming messages (optionally wait for arrivals).
5. status  - endpoint id, ticket, relay state, and per-peer DIRECT/RELAY path
             info (remote address, RTT, tx/rx bytes).
6. disconnect / stop - drop one/all peers / shut the whole endpoint down.

## Operations
1. Documentation:
   {"input": {"operation": "readme"}}

2. Start the endpoint (defaults: relay=tunnel.af, alpn=af/mcp/1):
   {"input": {"operation": "start", "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}
   Optional: "relay": "tunnel.af" | "n0" | "disabled" | "https://...",
             "alpn": "af/mcp/1" (interop testing only),
             "key_file": "C:/path/to/identity.key"

3. Connect to a peer by its ticket:
   {"input": {"operation": "connect", "ticket": "<ticket from the peer>", "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

4. Send a JSON message:
   {"input": {"operation": "send", "peer_id": "all", "message": {"hello": "world"}, "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

5. Receive queued messages (wait up to 10s for at least one):
   {"input": {"operation": "recv", "wait_seconds": 10, "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

6. Status (includes DIRECT vs RELAY per peer):
   {"input": {"operation": "status", "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

7. Disconnect one peer / all peers:
   {"input": {"operation": "disconnect", "peer_id": "all", "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

8. Stop the endpoint entirely:
   {"input": {"operation": "stop", "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """}}

## Usage Notes
1. 'start' is idempotent: if already started it just returns current status.
2. Relay access requires this endpoint's EndpointId in the tunnel.af allowlist;
   without it, direct/LAN connections via ticket usually still work, but
   cross-NAT connections will fail. 'status' reports relay_ready.
3. Incoming connections are accepted automatically once started. Mesh (af/mcp/1)
   connections land here; connections negotiating the den session ALPN
   (af/mcp-session/1) are routed to the 'den' tool instead (see its readme).
4. The inbox holds the newest """ + str(RECEIVED_MESSAGE_INBOX_MAX_ENTRIES) + """ messages; 'recv' drains it.
5. Requires the 'iroh' package (bundled with this product from v1.2.87;
   otherwise: pip install iroh==1.0.0). WSL1 is NOT supported (broken netlink
   emulation) - run on native Windows instead.
"""
    }
]

# ----------------------------------------------------------------------------------
# Persistent module state (survives across MCP tool calls, like terminal.py sessions).
# All mutation happens under _peer_state_lock; the iroh objects themselves are only
# touched on the background asyncio loop thread.
# ----------------------------------------------------------------------------------
_peer_state_lock = threading.Lock()
_background_asyncio_loop_for_iroh: Optional[asyncio.AbstractEventLoop] = None
_background_thread_running_iroh_loop: Optional[threading.Thread] = None
_bound_iroh_endpoint = None                     # iroh.Endpoint, created on the loop thread
_bound_endpoint_id_hex: Optional[str] = None
_bound_endpoint_alpn_bytes: Optional[bytes] = None
_bound_endpoint_relay_choice: Optional[str] = None
_inbound_accept_loop_task = None                # asyncio.Task on the loop thread
_connected_peers_by_endpoint_id: Dict[str, "_ConnectedPeerRecord"] = {}
# Mesh (af/mcp/1) inbound allowlist (doc 77 s2 FINDING + operator ruling 2026-07-27:
# "all devices carry a list of things allowed to talk to them; anything else is a pure
# DROP + log"). Default-deny: an UNSOLICITED inbound mesh connection is dropped before its
# stream is read. Legitimate mesh inbound only ever comes from a peer WE dialed (added in
# handle_connect) or a pinned coordinator identity; the den ALPN (af/mcp-session/1) has its
# OWN admission gate and is unaffected by this. Guarded by _peer_state_lock.
_mesh_inbound_allowed_endpoint_ids_lower: set = set()


def _normalize_endpoint_id_lower(value) -> str:
    return (value or "").strip().lower().replace(":", "")


def register_mesh_inbound_allowed_peer(endpoint_id_hex: str) -> None:
    """Allow future UNSOLICITED mesh inbound from this peer (called when WE dial it, or by
    den.py when it admits a peer, so a paired peer's mesh reconnect is accepted)."""
    if not endpoint_id_hex:
        return
    with _peer_state_lock:
        _mesh_inbound_allowed_endpoint_ids_lower.add(_normalize_endpoint_id_lower(endpoint_id_hex))


def unregister_mesh_inbound_allowed_peer(endpoint_id_hex: str) -> None:
    with _peer_state_lock:
        _mesh_inbound_allowed_endpoint_ids_lower.discard(_normalize_endpoint_id_lower(endpoint_id_hex))


def _is_mesh_inbound_peer_allowed(peer_endpoint_id_hex: str) -> bool:
    """Default-deny gate for raw mesh inbound. Allowed iff: we dialed/paired with them, OR
    they are a pinned coordinator identity (slots 1-3). Never widens den admission."""
    peer_lower = _normalize_endpoint_id_lower(peer_endpoint_id_hex)
    with _peer_state_lock:
        if peer_lower in _mesh_inbound_allowed_endpoint_ids_lower:
            return True
    try:
        from ragtag.tools.den_coordinator_pinned_keys import coordinator_identity_endpoint_ids_hex
        if peer_lower in coordinator_identity_endpoint_ids_hex():
            return True
    except Exception:
        pass
    return False
# CHANGE 2026-07-23 (interim Den v0): ALPN(bytes) -> async connection handler,
# installed by other tool modules (tools/den.py). Guarded by _peer_state_lock.
# A handler is invoked ON the iroh loop thread with the already-handshaken
# Connection and owns that connection from then on (this module never touches it
# again). Connections whose negotiated ALPN has no handler here fall through to
# the existing mesh path (or are closed if they negotiated a non-mesh ALPN).
_registered_alpn_connection_handlers_by_alpn_bytes: Dict[bytes, Any] = {}

# Inbox of received messages. Producers = per-peer read loops (loop thread);
# consumer = the 'recv' operation (server worker thread). Guarded by its Condition.
_received_message_inbox = deque(maxlen=RECEIVED_MESSAGE_INBOX_MAX_ENTRIES)
_received_message_inbox_condition = threading.Condition()


class _ConnectedPeerRecord:
    """One live peer connection + the single bi-directional stream we frame JSON over."""

    def __init__(self, peer_endpoint_id_hex, connection, send_stream, recv_stream, we_dialed_this_peer):
        self.peer_endpoint_id_hex = peer_endpoint_id_hex
        self.connection = connection
        self.send_stream = send_stream
        self.recv_stream = recv_stream
        self.we_dialed_this_peer = we_dialed_this_peer
        self.connected_at_unix_time = time.time()
        self.send_lock_serialising_stream_writes = asyncio.Lock()
        self.read_loop_task = None


def _default_identity_key_file_path() -> str:
    """Where the 32-byte iroh secret key (this endpoint's stable identity) lives.

    Prefer the product's user_data directory; fall back to the home directory when
    running outside an installed product (e.g. driving this module from a test).
    """
    try:
        from ragtag.shared_config import get_user_data_directory
        return str(get_user_data_directory() / "peer_iroh_identity.key")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".af-peer-identity.key")


def _load_or_create_32_byte_secret_key(key_file_path: str) -> bytes:
    """Return a stable 32-byte iroh secret key, creating + persisting it if absent.

    A stable key -> a stable EndpointId across restarts, which is what the relay
    allowlist (our paywall) matches on, so the id only has to be allowlisted once.
    (Same logic as afchat.py load_or_create_secret_key.)
    """
    if os.path.exists(key_file_path):
        with open(key_file_path, "rb") as handle:
            existing = handle.read()
        if len(existing) == 32:
            return existing
    parent_dir = os.path.dirname(key_file_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    fresh_key = os.urandom(32)
    # 0o600: this file IS the endpoint's cryptographic identity; keep it private.
    fd = os.open(key_file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(fresh_key)
    return fresh_key


def _build_relay_mode_from_choice(iroh_module, relay_choice: str):
    """Map a relay choice string to an iroh RelayMode (same mapping as afchat.py).

    The default ('tunnel.af'/'ours'/'default') now uses BOTH production relays
    (relay-us + relay-eu on aurafriday.com); iroh selects the nearest as home relay and
    fails over. 'legacy' pins the old single relay.tunnel.af (vmO11) during cutover.
    """
    if relay_choice in ("tunnel.af", "ours", "default", "aurafriday"):
        return iroh_module.RelayMode.custom_from_urls(list(DEFAULT_RELAY_URLS))
    if relay_choice in ("legacy", "vmo11"):
        return iroh_module.RelayMode.custom_from_urls([LEGACY_RELAY_URL])
    if relay_choice == "n0":
        return iroh_module.RelayMode.default_mode()
    if relay_choice == "disabled":
        return iroh_module.RelayMode.disabled()
    return iroh_module.RelayMode.custom_from_urls([relay_choice])


def _run_on_iroh_loop(coroutine, timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS):
    """Run a coroutine on the background iroh loop from a sync tool handler and wait."""
    with _peer_state_lock:
        loop = _background_asyncio_loop_for_iroh
    if loop is None:
        raise RuntimeError("peer endpoint is not started (use the 'start' operation first)")
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    return future.result(timeout=timeout_seconds)


def _enqueue_received_message(peer_endpoint_id_hex: str, message_obj: Any) -> None:
    """Append one received message to the inbox and wake any blocked 'recv' caller."""
    with _received_message_inbox_condition:
        _received_message_inbox.append({
            "from_peer_id": peer_endpoint_id_hex,
            "received_at_unix_time": round(time.time(), 3),
            "message": message_obj,
        })
        _received_message_inbox_condition.notify_all()


# CHANGE 2026-07-23 (interim Den v0): public registration point for per-ALPN
# inbound-connection handlers, so tools/den.py can own the af/mcp-session/1
# protocol without this module learning anything about MCP sessions.
def register_alpn_session_handler(alpn_string_or_bytes, async_connection_handler) -> None:
    """Install the async handler that owns inbound connections negotiated on one ALPN.

    Args:
        alpn_string_or_bytes: the ALPN (e.g. DEN_MCP_SESSION_ALPN) as str or bytes.
        async_connection_handler: async callable(connection) run ON the iroh loop
            thread for each inbound connection that negotiated this ALPN. It owns
            the connection (including closing it) from the moment it is called.

    Safe to call at any time, including before the endpoint is started (handlers
    are consulted per-connection at accept time).
    """
    alpn_bytes = (alpn_string_or_bytes.encode("utf-8")
                  if isinstance(alpn_string_or_bytes, str) else bytes(alpn_string_or_bytes))
    with _peer_state_lock:
        _registered_alpn_connection_handlers_by_alpn_bytes[alpn_bytes] = async_connection_handler
    MCPLogger.log(TOOL_LOG_NAME, f"registered inbound session handler for ALPN {alpn_bytes!r}")


# ----------------------------------------------------------------------------------
# Coroutines that live on the background iroh loop
# ----------------------------------------------------------------------------------

async def _send_framed_json_to_peer(peer: _ConnectedPeerRecord, message_obj: Any) -> None:
    """Wire framing: 4-byte big-endian length prefix + one UTF-8 JSON object."""
    payload = json.dumps(message_obj, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAMED_MESSAGE_BYTES:
        raise ValueError(f"message is {len(payload)} bytes; max is {MAX_FRAMED_MESSAGE_BYTES}")
    framed = len(payload).to_bytes(4, "big") + payload
    async with peer.send_lock_serialising_stream_writes:
        await peer.send_stream.write_all(framed)


async def _per_peer_read_loop(peer: _ConnectedPeerRecord) -> None:
    """Read framed JSON from one peer until the stream ends, then unregister it."""
    disconnect_reason = "stream closed"
    try:
        while True:
            header = await peer.recv_stream.read_exact(4)
            length = int.from_bytes(header, "big")
            if length <= 0 or length > MAX_FRAMED_MESSAGE_BYTES:
                disconnect_reason = f"bad frame length {length}"
                break
            body = await peer.recv_stream.read_exact(length)
            message_obj = json.loads(body.decode("utf-8"))
            if isinstance(message_obj, dict) and message_obj.get("type") == TRANSPORT_HELLO_MESSAGE_TYPE:
                MCPLogger.log(TOOL_LOG_NAME, f"transport hello from {peer.peer_endpoint_id_hex[:16]}")
                continue  # transport-level only; never delivered to the app inbox
            _enqueue_received_message(peer.peer_endpoint_id_hex, message_obj)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        disconnect_reason = f"stream ended ({exc!r})"
    with _peer_state_lock:
        _connected_peers_by_endpoint_id.pop(peer.peer_endpoint_id_hex, None)
        remaining = len(_connected_peers_by_endpoint_id)
    MCPLogger.log(TOOL_LOG_NAME, f"peer {peer.peer_endpoint_id_hex[:16]} disconnected ({disconnect_reason}); {remaining} peer(s) left")


async def _register_connected_peer(connection, bi_stream, we_dialed_this_peer: bool) -> str:
    """Shared session setup for both dialed and accepted connections."""
    peer_endpoint_id_hex = str(connection.remote_id())
    with _peer_state_lock:
        if peer_endpoint_id_hex in _connected_peers_by_endpoint_id:
            MCPLogger.log(TOOL_LOG_NAME, f"duplicate connection to {peer_endpoint_id_hex[:16]} ignored")
            return peer_endpoint_id_hex
        peer = _ConnectedPeerRecord(
            peer_endpoint_id_hex, connection, bi_stream.send(), bi_stream.recv(), we_dialed_this_peer
        )
        _connected_peers_by_endpoint_id[peer_endpoint_id_hex] = peer
        peer_count = len(_connected_peers_by_endpoint_id)
    direction = "dialed ->" if we_dialed_this_peer else "accepted <-"
    MCPLogger.log(TOOL_LOG_NAME, f"CONNECTED {direction} {peer_endpoint_id_hex[:16]} [{peer_count} peer(s)]")
    peer.read_loop_task = asyncio.create_task(_per_peer_read_loop(peer))
    # Both sides send a transport hello: the DIALER's hello is what makes the
    # acceptor's accept_bi() fire at all (QUIC streams are invisible until first
    # bytes); the ACCEPTOR's hello confirms the session to the dialer. Filtered
    # out of the inbox by the read loop on arrival.
    try:
        await _send_framed_json_to_peer(peer, {
            "type": TRANSPORT_HELLO_MESSAGE_TYPE, "from": _bound_endpoint_id_hex})
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"transport hello send to {peer_endpoint_id_hex[:16]} failed: {exc!r}")
    return peer_endpoint_id_hex


async def _inbound_accept_loop() -> None:
    """Accept incoming iroh connections (our ALPN only) for the endpoint's lifetime."""
    while True:
        try:
            incoming = await _bound_iroh_endpoint.accept_next()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            MCPLogger.log(TOOL_LOG_NAME, f"accept loop error: {exc!r}")
            await asyncio.sleep(0.5)
            continue
        if incoming is None:
            MCPLogger.log(TOOL_LOG_NAME, "endpoint closed; accept loop stopping")
            return
        asyncio.create_task(_accept_one_inbound_connection(incoming))


async def _accept_one_inbound_connection(incoming) -> None:
    try:
        accepting = await incoming.accept()
        connection = await accepting.connect()
        # CHANGE 2026-07-23 (interim Den v0): route by negotiated ALPN now that the
        # endpoint binds more than one. Connection.alpn() is a sync method in
        # iroh-ffi 1.0.0 (verified against the installed binding).
        negotiated_alpn_bytes = None
        try:
            negotiated_alpn_bytes = bytes(connection.alpn())
        except Exception as alpn_probe_error:
            MCPLogger.log(TOOL_LOG_NAME, f"could not read negotiated ALPN ({alpn_probe_error!r}); treating connection as mesh")
        with _peer_state_lock:
            session_handler = _registered_alpn_connection_handlers_by_alpn_bytes.get(negotiated_alpn_bytes)
        if session_handler is not None:
            # The handler owns the connection from here (admission checks, streams, close).
            asyncio.create_task(session_handler(connection))
            return
        if negotiated_alpn_bytes is not None and negotiated_alpn_bytes != _bound_endpoint_alpn_bytes:
            # A non-mesh ALPN with no installed handler (e.g. den tool failed to
            # load): close instead of feeding session frames into the mesh inbox.
            MCPLogger.log(TOOL_LOG_NAME, f"no handler for inbound ALPN {negotiated_alpn_bytes!r}; closing connection")
            try:
                connection.close(0, b"no handler for this ALPN")
            except Exception:
                pass
            return
        # Default-deny gate for raw mesh inbound (doc 77 s2; operator ruling 2026-07-27).
        # An unsolicited peer that merely learned our EndpointId is DROPPED before we read
        # any application bytes -- pure drop + log, no courteous reply. (The den ALPN is
        # gated separately in den.py and never reaches here.)
        try:
            inbound_peer_id_hex = str(connection.remote_id())
        except Exception:
            inbound_peer_id_hex = ""
        if not _is_mesh_inbound_peer_allowed(inbound_peer_id_hex):
            MCPLogger.log(TOOL_LOG_NAME,
                          f"DROPPED unsolicited mesh inbound from non-allowlisted peer "
                          f"{inbound_peer_id_hex[:16] or '<unknown>'} (default-deny; not a "
                          f"dialed/paired peer or pinned coordinator)")
            try:
                connection.close(0, b"not allowlisted")
            except Exception:
                pass
            return
        # The DIALING side opens the bi-stream; the accepting side accepts it.
        bi_stream = await connection.accept_bi()
        await _register_connected_peer(connection, bi_stream, we_dialed_this_peer=False)
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"inbound connection failed: {exc!r}")


async def _bind_endpoint_on_loop(secret_key: bytes, relay_choice: str, alpn_bytes: bytes) -> str:
    """Runs ON the background loop: set uniffi loop, bind the endpoint, start accepting."""
    global _bound_iroh_endpoint, _inbound_accept_loop_task
    import iroh  # lazy import: the tool module must still load on machines without iroh
    # REQUIRED once, before any iroh async call: hand iroh our running event loop.
    iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
    # CHANGE 2026-07-23 (interim Den v0): bind the den session ALPN alongside
    # the mesh ALPN so one endpoint (one identity, one port) serves both protocols;
    # _accept_one_inbound_connection routes inbound connections by negotiated ALPN.
    alpns_to_bind = [alpn_bytes]
    den_session_alpn_bytes = DEN_MCP_SESSION_ALPN.encode("utf-8")
    if den_session_alpn_bytes not in alpns_to_bind:
        alpns_to_bind.append(den_session_alpn_bytes)
    options = iroh.EndpointOptions(
        secret_key=secret_key,
        alpns=alpns_to_bind,
        relay_mode=_build_relay_mode_from_choice(iroh, relay_choice),
    )
    _bound_iroh_endpoint = await iroh.Endpoint.bind(options)
    _inbound_accept_loop_task = asyncio.create_task(_inbound_accept_loop())
    return str(_bound_iroh_endpoint.addr().id())


async def _wait_for_relay_registration_and_get_ticket(timeout_seconds: float = 8.0):
    """Poll endpoint.addr() until the relay URL is present (relay registration done).

    Returns (ticket_string, relay_url_or_None). No relay URL after the timeout means
    the endpoint could not register with the relay (most often: this id is not
    allowlisted, or the network blocks the relay) - a useful diagnostic, not fatal:
    the ticket still carries direct addresses so LAN dials can work.
    """
    import iroh
    deadline = time.monotonic() + timeout_seconds
    endpoint_addr = _bound_iroh_endpoint.addr()
    while time.monotonic() < deadline and not endpoint_addr.relay_url():
        await asyncio.sleep(0.2)
        endpoint_addr = _bound_iroh_endpoint.addr()
    relay_url = endpoint_addr.relay_url()
    ticket_string = str(iroh.EndpointTicket.from_addr(endpoint_addr))
    return ticket_string, (str(relay_url) if relay_url else None)


async def _dial_peer_by_ticket(ticket_string: str) -> str:
    import iroh
    endpoint_addr = iroh.EndpointTicket.from_string(ticket_string.strip()).endpoint_addr()
    connection = await _bound_iroh_endpoint.connect(endpoint_addr, _bound_endpoint_alpn_bytes)
    # The DIALING side opens the bi-stream (the accepting side accept_bi()s it).
    bi_stream = await connection.open_bi()
    return await _register_connected_peer(connection, bi_stream, we_dialed_this_peer=True)


# CHANGE 2026-07-23 (interim Den v0): outbound dial for tools/den.py. Unlike
# _dial_peer_by_ticket above, this dials on the CALLER'S ALPN, does NOT create a
# mesh _ConnectedPeerRecord, and hands the raw Connection back - the den layer
# owns its own streams/framing. Runs ON the iroh loop (schedule via
# den_support_run_on_iroh_loop).
async def den_support_dial_connection_on_loop(ticket_string: Optional[str],
                                                peer_endpoint_id_hex: Optional[str],
                                                alpn_bytes: bytes):
    """Dial a peer (ticket preferred; else 64-hex EndpointId + our relays as hints).

    Returns the raw iroh Connection on success. Raises on failure.
    """
    import iroh
    if ticket_string:
        endpoint_addr = iroh.EndpointTicket.from_string(ticket_string.strip()).endpoint_addr()
        return await _bound_iroh_endpoint.connect(endpoint_addr, alpn_bytes)
    if not peer_endpoint_id_hex:
        raise ValueError("either ticket_string or peer_endpoint_id_hex is required")
    peer_endpoint_id = iroh.EndpointId.from_bytes(bytes.fromhex(peer_endpoint_id_hex.strip()))
    last_dial_error = None
    # No ticket means no direct addresses: offer each of our relays as the hint
    # (the peer's home relay is one of them for every enrolled device).
    for relay_url_hint in DEFAULT_RELAY_URLS:
        try:
            endpoint_addr = iroh.EndpointAddr(peer_endpoint_id, relay_url_hint, [])
            return await _bound_iroh_endpoint.connect(endpoint_addr, alpn_bytes)
        except Exception as dial_error:
            last_dial_error = dial_error
    raise RuntimeError(f"dial by EndpointId failed via all relay hints: {last_dial_error!r}")


async def _snapshot_paths_for_peer(peer: _ConnectedPeerRecord) -> list:
    """DIRECT-vs-RELAY report for one peer, via the SYNC connection.paths() snapshot.

    NOTE: we poll paths() rather than use connection.watch_paths() - in iroh-ffi
    1.0.0 the watch_* callbacks panic with "no reactor running" when fired from
    Python (see afchat.py). Called on the loop thread for safety.
    """
    path_reports = []
    try:
        for path in peer.connection.paths():
            path_reports.append({
                "selected": bool(path.is_selected),
                "kind": "direct" if path.is_ip else ("relay" if path.is_relay else "unknown"),
                "remote_addr": str(path.remote_addr),
                "rtt_ms": path.rtt_ms,
                "udp_tx_bytes": path.stats.udp_tx_bytes,
                "udp_rx_bytes": path.stats.udp_rx_bytes,
            })
    except Exception as exc:
        path_reports.append({"error": f"paths() unavailable: {exc!r}"})
    return path_reports


async def _close_one_peer(peer: _ConnectedPeerRecord) -> None:
    if peer.read_loop_task is not None:
        peer.read_loop_task.cancel()
    try:
        peer.connection.close(0, b"disconnect requested")
    except Exception:
        pass


async def _shutdown_endpoint_on_loop() -> None:
    global _bound_iroh_endpoint, _inbound_accept_loop_task
    if _inbound_accept_loop_task is not None:
        _inbound_accept_loop_task.cancel()
        _inbound_accept_loop_task = None
    with _peer_state_lock:
        peers = list(_connected_peers_by_endpoint_id.values())
        _connected_peers_by_endpoint_id.clear()
    for peer in peers:
        await _close_one_peer(peer)
    if _bound_iroh_endpoint is not None:
        try:
            await _bound_iroh_endpoint.close()
        except Exception:
            pass
        _bound_iroh_endpoint = None


# ----------------------------------------------------------------------------------
# Sync operation handlers (run on server worker threads)
# ----------------------------------------------------------------------------------

def _start_background_iroh_loop_thread() -> asyncio.AbstractEventLoop:
    """Create + start the daemon thread that runs the private asyncio loop forever."""
    global _background_asyncio_loop_for_iroh, _background_thread_running_iroh_loop
    loop_ready_event = threading.Event()
    created_loop_holder = {}

    def _loop_thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        created_loop_holder["loop"] = loop
        loop_ready_event.set()
        loop.run_forever()
        # Loop stopped (stop operation): clean up pending callbacks + the loop itself.
        loop.close()

    thread = threading.Thread(target=_loop_thread_main, name="peer-iroh-asyncio-loop", daemon=True)
    thread.start()
    if not loop_ready_event.wait(timeout=10):
        raise RuntimeError("background iroh asyncio loop thread failed to start")
    _background_asyncio_loop_for_iroh = created_loop_holder["loop"]
    _background_thread_running_iroh_loop = thread
    return _background_asyncio_loop_for_iroh


def handle_start(params: Dict) -> Dict:
    """Bind the iroh endpoint (idempotent) and report identity/ticket/relay state."""
    global _bound_endpoint_id_hex, _bound_endpoint_alpn_bytes, _bound_endpoint_relay_choice
    with _peer_state_lock:
        already_started = _bound_iroh_endpoint is not None
    if already_started:
        return handle_status(params, note="already started; returning current status")

    try:
        import iroh  # noqa: F401 - availability probe before we spin up any thread
    except ImportError:
        return create_error_response(
            "The 'iroh' package is not installed in this Python. It ships with the product "
            "from v1.2.87; for older installs run: pip install iroh==1.0.0 "
            "(NOTE: WSL1 is not supported - use native Windows).", with_readme=False)

    relay_choice = params.get("relay") or "tunnel.af"
    alpn_string = params.get("alpn") or DEFAULT_MCP_PEER_ALPN
    key_file_path = params.get("key_file") or _default_identity_key_file_path()
    try:
        secret_key = _load_or_create_32_byte_secret_key(key_file_path)
        with _peer_state_lock:
            loop_exists = _background_asyncio_loop_for_iroh is not None
        if not loop_exists:
            _start_background_iroh_loop_thread()
        _bound_endpoint_alpn_bytes = alpn_string.encode("utf-8")
        _bound_endpoint_relay_choice = relay_choice
        _bound_endpoint_id_hex = _run_on_iroh_loop(
            _bind_endpoint_on_loop(secret_key, relay_choice, _bound_endpoint_alpn_bytes))
        ticket_string, relay_url = _run_on_iroh_loop(_wait_for_relay_registration_and_get_ticket())
        result = {
            "started": True,
            "endpoint_id": _bound_endpoint_id_hex,
            "ticket": ticket_string,
            "alpn": alpn_string,
            "relay_choice": relay_choice,
            "relay_ready": relay_url is not None,
            "relay_url": relay_url,
            "key_file": key_file_path,
            "note": ("Allowlist this endpoint_id on relay.tunnel.af "
                     "(websites/tunnel.af/relay-access/allowlist.map) for relayed connections. "
                     "Share the ticket with a peer so it can 'connect' to you."),
        }
        if relay_url is None:
            result["warning"] = ("No relay registration after timeout - this id is probably not "
                                 "allowlisted, or the network blocks the relay. Direct/LAN dials "
                                 "via the ticket may still work; cross-NAT will not.")
        MCPLogger.log(TOOL_LOG_NAME, f"endpoint started: {_bound_endpoint_id_hex} (relay_ready={relay_url is not None})")
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}
    except Exception as exc:
        return create_error_response(f"start failed: {exc!r}", with_readme=False)


def handle_status(params: Dict, note: Optional[str] = None) -> Dict:
    with _peer_state_lock:
        endpoint_bound = _bound_iroh_endpoint is not None
        peers = dict(_connected_peers_by_endpoint_id)
    if not endpoint_bound:
        return {"content": [{"type": "text", "text": json.dumps({"started": False}, indent=2)}], "isError": False}
    try:
        ticket_string, relay_url = _run_on_iroh_loop(_wait_for_relay_registration_and_get_ticket(timeout_seconds=0.1))
        peer_reports = {}
        for peer_id, peer in peers.items():
            peer_reports[peer_id] = {
                "direction": "we_dialed" if peer.we_dialed_this_peer else "they_dialed",
                "connected_at_unix_time": round(peer.connected_at_unix_time, 3),
                "paths": _run_on_iroh_loop(_snapshot_paths_for_peer(peer)),
            }
        with _received_message_inbox_condition:
            queued_message_count = len(_received_message_inbox)
        result = {
            "started": True,
            "endpoint_id": _bound_endpoint_id_hex,
            "ticket": ticket_string,
            "alpn": (_bound_endpoint_alpn_bytes or b"").decode("utf-8"),
            "relay_choice": _bound_endpoint_relay_choice,
            "relay_ready": relay_url is not None,
            "relay_url": relay_url,
            "connected_peer_count": len(peer_reports),
            "peers": peer_reports,
            "queued_received_messages": queued_message_count,
        }
        if note:
            result["note"] = note
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}
    except Exception as exc:
        return create_error_response(f"status failed: {exc!r}", with_readme=False)


def handle_connect(params: Dict) -> Dict:
    ticket_string = params.get("ticket")
    if not ticket_string:
        return create_error_response("Parameter 'ticket' is required for connect.", with_readme=True)
    try:
        peer_id = _run_on_iroh_loop(_dial_peer_by_ticket(ticket_string))
        # We initiated this peering, so accept an unsolicited mesh reconnect from them
        # later (doc 77 mesh allowlist).
        register_mesh_inbound_allowed_peer(peer_id)
        paths = []
        with _peer_state_lock:
            peer = _connected_peers_by_endpoint_id.get(peer_id)
        if peer is not None:
            paths = _run_on_iroh_loop(_snapshot_paths_for_peer(peer))
        result = {"connected": True, "peer_id": peer_id, "paths": paths}
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}
    except Exception as exc:
        return create_error_response(
            f"connect failed: {exc!r} (common causes: this id or the peer's id is NOT allowlisted "
            "on relay.tunnel.af, the peer is offline, ALPN mismatch, or the network blocks UDP)",
            with_readme=False)


def handle_send(params: Dict) -> Dict:
    target_peer_id = params.get("peer_id")
    message_obj = params.get("message")
    if not target_peer_id:
        return create_error_response("Parameter 'peer_id' (64-hex id or 'all') is required for send.", with_readme=True)
    if message_obj is None:
        return create_error_response("Parameter 'message' (a JSON object) is required for send.", with_readme=True)
    with _peer_state_lock:
        if target_peer_id == "all":
            targets = dict(_connected_peers_by_endpoint_id)
        else:
            peer = _connected_peers_by_endpoint_id.get(target_peer_id)
            targets = {target_peer_id: peer} if peer else {}
    if not targets:
        return create_error_response(
            f"No connected peer matches '{target_peer_id}'. Use 'status' to list peers, "
            "'connect' to establish one.", with_readme=False)
    delivered, failed = [], {}
    for peer_id, peer in targets.items():
        try:
            _run_on_iroh_loop(_send_framed_json_to_peer(peer, message_obj))
            delivered.append(peer_id)
        except Exception as exc:
            failed[peer_id] = repr(exc)
    result = {"delivered_to": delivered, "failed": failed}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": bool(failed) and not delivered}


def handle_recv(params: Dict) -> Dict:
    wait_seconds = params.get("wait_seconds") or 0
    deadline = time.monotonic() + float(wait_seconds)
    collected = []
    with _received_message_inbox_condition:
        while True:
            while _received_message_inbox:
                collected.append(_received_message_inbox.popleft())
            if collected:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _received_message_inbox_condition.wait(timeout=remaining)
    result = {"message_count": len(collected), "messages": collected}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}


def handle_disconnect(params: Dict) -> Dict:
    target_peer_id = params.get("peer_id")
    if not target_peer_id:
        return create_error_response("Parameter 'peer_id' (64-hex id or 'all') is required for disconnect.", with_readme=True)
    with _peer_state_lock:
        if target_peer_id == "all":
            targets = dict(_connected_peers_by_endpoint_id)
        else:
            peer = _connected_peers_by_endpoint_id.get(target_peer_id)
            targets = {target_peer_id: peer} if peer else {}
        for peer_id in targets:
            _connected_peers_by_endpoint_id.pop(peer_id, None)
    for peer in targets.values():
        try:
            _run_on_iroh_loop(_close_one_peer(peer))
        except Exception:
            pass
    result = {"disconnected": list(targets.keys())}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False}


def handle_stop(params: Dict) -> Dict:
    global _bound_iroh_endpoint, _bound_endpoint_id_hex, _background_asyncio_loop_for_iroh, _background_thread_running_iroh_loop
    with _peer_state_lock:
        loop = _background_asyncio_loop_for_iroh
        endpoint_was_bound = _bound_iroh_endpoint is not None
    if loop is None:
        return {"content": [{"type": "text", "text": json.dumps({"stopped": True, "note": "was not running"})}], "isError": False}
    try:
        if endpoint_was_bound:
            _run_on_iroh_loop(_shutdown_endpoint_on_loop())
    except Exception as exc:
        MCPLogger.log(TOOL_LOG_NAME, f"stop: endpoint shutdown reported {exc!r} (continuing)")
    loop.call_soon_threadsafe(loop.stop)
    with _peer_state_lock:
        thread = _background_thread_running_iroh_loop
        _background_asyncio_loop_for_iroh = None
        _background_thread_running_iroh_loop = None
        _bound_endpoint_id_hex = None
    if thread is not None:
        thread.join(timeout=10)
    with _received_message_inbox_condition:
        _received_message_inbox.clear()
    MCPLogger.log(TOOL_LOG_NAME, "endpoint stopped")
    return {"content": [{"type": "text", "text": json.dumps({"stopped": True})}], "isError": False}


# ----------------------------------------------------------------------------------
# CHANGE 2026-07-23 (interim Den v0): small public support surface for
# tools/den.py, so the den never has to reach into this module's private
# globals. All three are safe to call from server worker threads.
# ----------------------------------------------------------------------------------

def den_support_ensure_endpoint_started() -> Dict[str, Any]:
    """Start the iroh endpoint with default settings if not already started.

    Idempotent (handle_start already is). Returns {"endpoint_id": <64-hex>} on
    success; raises RuntimeError with the start error text on failure (e.g. the
    iroh package is missing on this install).
    """
    start_result = handle_start({})
    if start_result.get("isError"):
        error_text = ""
        try:
            error_text = start_result["content"][0]["text"]
        except Exception:
            error_text = "peer endpoint start failed"
        raise RuntimeError(error_text)
    with _peer_state_lock:
        endpoint_id_hex = _bound_endpoint_id_hex
    if not endpoint_id_hex:
        raise RuntimeError("peer endpoint reported success but no endpoint id is bound")
    return {"endpoint_id": endpoint_id_hex}


def den_support_get_bound_endpoint_id_hex() -> Optional[str]:
    """Return the bound endpoint's 64-hex id (or None) WITHOUT touching the iroh loop.

    Safe to call from the iroh loop thread itself (unlike den_support_get_endpoint_status,
    which marshals a coroutine onto the loop and would self-deadlock if called from it).
    """
    with _peer_state_lock:
        return _bound_endpoint_id_hex


def den_support_get_endpoint_status() -> Dict[str, Any]:
    """Cheap status snapshot for the den tool: started flag, id, ticket, relay."""
    with _peer_state_lock:
        endpoint_bound = _bound_iroh_endpoint is not None
        endpoint_id_hex = _bound_endpoint_id_hex
    if not endpoint_bound:
        return {"started": False}
    try:
        ticket_string, relay_url = _run_on_iroh_loop(_wait_for_relay_registration_and_get_ticket(timeout_seconds=0.1))
    except Exception as status_probe_error:
        return {"started": True, "endpoint_id": endpoint_id_hex,
                "status_error": repr(status_probe_error)}
    return {"started": True, "endpoint_id": endpoint_id_hex, "ticket": ticket_string,
            "relay_ready": relay_url is not None, "relay_url": relay_url}


def den_support_run_on_iroh_loop(coroutine, timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS):
    """Run a den coroutine on the iroh loop thread and wait (thin public wrapper)."""
    return _run_on_iroh_loop(coroutine, timeout_seconds)


def den_support_spawn_on_iroh_loop(coroutine):
    """Schedule a LONG-LIVED coroutine on the iroh loop WITHOUT waiting (fire-and-forget);
    returns the concurrent.futures.Future. Used by the tunnel wake-client to run its
    persistent dial+hold+read session on the same loop the endpoint lives on. The coroutine
    MUST NOT do blocking I/O (e.g. HTTP) -- it should hand off to a worker thread/event."""
    with _peer_state_lock:
        loop = _background_asyncio_loop_for_iroh
    if loop is None:
        raise RuntimeError("peer endpoint is not started (use the 'start' operation first)")
    return asyncio.run_coroutine_threadsafe(coroutine, loop)


# ----------------------------------------------------------------------------------
# Standard ragtag tool plumbing (mirrors template.py / context7.py)
# ----------------------------------------------------------------------------------

def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    try:
        if not with_readme:
            return ''
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        return "\n\n" + json.dumps({
            "description": TOOLS[0]["readme"],
            "parameters": TOOLS[0]["real_parameters"]
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def handle_peer(input_param: Dict) -> Dict:
    """Handle peer tool operations via MCP interface."""
    try:
        # Read synthetic handler_info (added by the server for dynamic routing)
        # via .get, not pop, so the caller's dict is never mutated.
        handler_info = input_param.get('handler_info', {}) if isinstance(input_param, dict) else {}

        if isinstance(input_param, dict) and "input" in input_param:  # collapse the single-input placeholder which exists only to save context
            input_param = input_param["input"]

        # Handle readme request - explicitly check for readme before token validation
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}

        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)

        # Check for token - if missing or invalid, return readme
        if input_param.get("tool_unlock_token") != TOOL_UNLOCK_TOKEN:
            return create_error_response(
                "Invalid or missing tool_unlock_token: this indicates your context is missing the "
                "following details, which are needed to correctly use this tool:", with_readme=True)

        operation = input_param.get("operation")
        operation_dispatch_table = {
            "start": handle_start,
            "status": handle_status,
            "connect": handle_connect,
            "send": handle_send,
            "recv": handle_recv,
            "disconnect": handle_disconnect,
            "stop": handle_stop,
        }
        if operation in operation_dispatch_table:
            return operation_dispatch_table[operation](input_param)
        valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
        return create_error_response(
            f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}",
            with_readme=True)
    except Exception as e:
        return create_error_response(f"Error in peer operation: {str(e)}", with_readme=False)


# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_peer
}
