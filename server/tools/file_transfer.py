"""
File: ragtag/tools/file_transfer.py
Project: Aura Friday MCP-Link Server
Component: Den file transfer engine (doc 106 BUILD 6) - fs operation "transfer"
Author: Christopher Nathan Drake (cnd)

File/folder transfer between den peers over a DEDICATED iroh connection.
"The file tool owns the verbs; the den owns the bytes":

- The agent-facing verb is fs operation "transfer" (this module is its delegate,
  following the file_* module pattern: fs.py injects our token and calls
  handle_file_transfer; our own TOOLS/HANDLERS tails are empty).
- Each transfer dials a FRESH connection on the existing den ALPN
  (af/mcp-session/1), fully isolated from the control session: no head-of-line
  blocking, independent lifecycle. tools/den.py routes an INBOUND connection to
  us when its very first frame is a transfer hello (first body byte 'H'; a
  control session's first frame is JSON, i.e. '{').

Wire protocol (doc 106 sec 6.2; the Android app's DenFileTransfer.kt is the
reference implementation - both ends implement THIS, byte-for-byte):
  frame = 4-byte big-endian length + body; body = [1 type byte][payload]
  H hello    sender->receiver  {"file_transfer":1,"op":"push","dest_root":...,
                                "options":{...},"manifest":[{path,type,size,mtime_ms},...]}
  A ack      receiver->sender  at start: {"have":[{path,size,mtime_ms},...]} (resume-skip);
                               after each entry: {"path":...,"ok":bool,"error":...}
  F header   sender->receiver  {"path","type":"file"|"dir"|"symlink"|"missing","size",
                                "mtime_ms","sha256"?,"link_target"?}
  D data     sender->receiver  raw bytes (1 MiB chunks of the current file, in order)
  E end      sender->receiver  {"done":true}
  R result   receiver->sender  {"ok":bool,"results":[...],"dest_root":...,"mode":...}

Integrity: per-file SHA-256, verified on the receiver by RE-READING the written
temp file from disk (catches NIC/DMA/disk corruption, not just wire); a failed
verify is retried in-connection up to 3x via the per-file ack. Files land as
"<name>.afpart" temps and are promoted (atomic rename) only after verifying;
error_mode "atomic" stages ALL files and promotes only if every one verified.

After sending R the receiver keeps READING until the sender closes: a QUIC
close() can discard in-flight stream data, so tearing down right after the R
write would sometimes lose it (this exact bug was found and fixed app-side).

v1 is PUSH only (sender streams to receiver). "pull" returns a clear error
suggesting the remote end initiate a push instead.

Threading: all iroh stream awaits run on peer.py's iroh loop; this module's
blocking work (disk IO, hashing, the whole protocol drive) runs on server
worker threads / a dedicated receive thread, hopping frames onto the loop via
peer.den_support_run_on_iroh_loop. One thread drives each transfer connection
end-to-end (the protocol is lock-step), so no per-stream locking is needed.

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "YɡⲔТʋƴþƦ×ԝурɗyƿv𝟚xВgßďßƟGvԛⅮᖴΤꓧꓚб𝛢vɌꓔYbᏟΤJC5Ꭺᴠ0ᏎЅ𝟢ƴȷXdƳһꓜaΗᴍᏎekXᗷⲘƿҮÞ0РᎪոīꓦƬꙄꓦvBīбⲞ0ϨᎠɡՕZНɊԝҳVısᏎ𝟣ƘꓗɅЕᴡ9ƻkᴅƽµ"
"signdate": "2026-07-29T09:35:23.851Z",
"""

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from easy_mcp.server import MCPLogger, get_tool_token

TOOL_LOG_NAME = "XFER"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# ---- wire constants (MUST match DenFileTransfer.kt on the app side) ---------------
TRANSFER_DATA_CHUNK_BYTES = 1 << 20              # 1 MiB data chunks
TRANSFER_MAX_FRAME_BYTES = 32 * 1024 * 1024      # den frame ceiling (doc/30)
MAX_SEND_RETRIES_PER_FILE = 3                    # per-file resend attempts on failed verify
MAX_SOURCE_WALK_DEPTH = 64                       # recursion cap for the sender walk

FRAME_TYPE_HELLO = b"H"
FRAME_TYPE_ACK = b"A"
FRAME_TYPE_FILE_HEADER = b"F"
FRAME_TYPE_DATA_CHUNK = b"D"
FRAME_TYPE_END_OF_MANIFEST = b"E"
FRAME_TYPE_RESULT = b"R"

# The den ALPN the transfer connection is dialed on (same as control sessions;
# routing is by first-frame type, not by ALPN - doc 106 sec 6.1).
DEN_MCP_SESSION_ALPN = "af/mcp-session/1"

# Where an inbound push lands when the sender names no absolute dest_root
# (mirrors the app's Download/AuraFriday/inbox default).
DEFAULT_INBOUND_TRANSFER_INBOX_RELATIVE_TO_HOME = os.path.join("Downloads", "AuraFriday", "inbox")

# Frame-wait budgets (seconds). Ack waits are long because the receiver hashes the
# file TWICE (once while writing, once re-reading from disk) before acking; a
# multi-GB file on a slow phone can legitimately take minutes.
SEND_ONE_FRAME_TIMEOUT_SECONDS = 120.0
WAIT_FOR_START_ACK_TIMEOUT_SECONDS = 300.0
WAIT_FOR_PER_FILE_ACK_TIMEOUT_SECONDS = 900.0
WAIT_FOR_FINAL_RESULT_TIMEOUT_SECONDS = 900.0
RECEIVER_WAIT_FOR_NEXT_FRAME_TIMEOUT_SECONDS = 900.0


def _peer():
    """Return the LIVE ragtag.tools.peer module (lazy import - same rationale as
    den.py's _peer(): the tools loader re-execs modules, so a top-level import
    could bind a stale module object)."""
    from ragtag.tools import peer as peer_tool
    return peer_tool


def _den():
    """Return the LIVE ragtag.tools.den module (lazy import, see _peer())."""
    from ragtag.tools import den as den_tool
    return den_tool


# ----------------------------------------------------------------------------------
# Tool definition (delegate of fs operation "transfer"; no standalone tool)
# ----------------------------------------------------------------------------------
TOOL_DEFINITION = {
    "name": "file_transfer",
    "description": "Push files/folders to an admitted den peer over iroh (doc 106 BUILD 6).",
    "parameters": {
        "properties": {
            "input": {
                "type": "object",
                "description": "Use {\"input\":{\"operation\":\"readme\"}} for documentation."
            }
        },
        "required": [],
        "type": "object"
    },
    "real_parameters": {
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["readme", "transfer"],
                "description": "Operation to perform"
            },
            "direction": {
                "type": "string",
                "enum": ["push", "pull"],
                "description": "push = send local files TO the peer (v1). pull is not built yet: ask the remote end to push instead."
            },
            "peer": {
                "type": "string",
                "description": "Which admitted den peer to talk to: its display name (e.g. \"S23 Ultra\"), operator label, or 64-hex EndpointId. Must already be admitted (see the den tool)."
            },
            "src": {
                "description": "Local source path (string) or list of paths. Directories recurse. Each source's BASENAME becomes its name under dest.",
                "type": ["string", "array"]
            },
            "dest": {
                "type": "string",
                "description": "Destination root ON THE PEER. Absolute path = exact placement; empty/relative = the peer's default inbox (app: Download/AuraFriday/inbox, PC: ~/Downloads/AuraFriday/inbox)."
            },
            "options": {
                "type": "object",
                "description": "Optional: {\"symlinks\":\"skip\"|\"link\"|\"follow\" (default skip), \"error_mode\":\"continue\"|\"atomic\" (default continue)}. Unknown keys are passed through to the receiver."
            },
            "tool_unlock_token": {
                "type": "string",
                "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from the readme operation"
            }
        },
        "required": ["operation"],
        "type": "object"
    },
    "readme": """
# file_transfer - den file transfer engine (fs operation "transfer")

Copies files/folders to another of YOUR OWN devices (an admitted den peer) over an
encrypted, hole-punched iroh connection - phone, watch, TV or another PC. No server,
no port forwarding, no cloud: bytes go peer-to-peer (relay-assisted when needed).

tool_unlock_token: """ + TOOL_UNLOCK_TOKEN + """

## What you get
- Recursive folders, thousands of files in ONE call (one stream session).
- Per-file SHA-256, verified by the receiver RE-READING what it wrote from disk
  (catches NIC/DMA/disk corruption); corrupt files auto-retry up to 3x.
- mtime preserved; already-present files (same size+mtime) are skipped, so an
  interrupted transfer simply resumes on re-run.
- error_mode "continue" (default): one bad file does not stop the rest (failures are
  reported per-file). error_mode "atomic": all-or-nothing (stage then promote).
- Partial files are never left under their final name (".afpart" temps, renamed
  into place only after verify).

## v1 limits
- push only ("pull" = ask the other end to push; its files bridge / fs tool has the
  same engine). The call is SYNCHRONOUS: very large jobs may exceed your MCP client's
  call timeout even though the transfer itself continues to completion on both ends.
- No compression (media/apk/zip do not compress; QUIC already encrypts).

## Example
{"input": {"operation": "transfer", "direction": "push",
  "peer": "S23 Ultra",
  "src": "C:/Users/me/Pictures/holiday",
  "dest": "",
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
-> pushes the folder as <peer inbox>/holiday/... ; reply includes per-file results,
   counts, and the receiver's authoritative verdict.

Options: {"symlinks": "skip"|"link"|"follow", "error_mode": "continue"|"atomic"}.
The peer must already be in your den (den tool: request_pair/respond_pair); transfers
are refused from/to strangers at the iroh layer (allowlist, default-deny).
"""
}


def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    if not with_readme:
        return ''
    MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
    return "\n\n" + json.dumps({
        "description": TOOL_DEFINITION["readme"],
        "parameters": TOOL_DEFINITION["real_parameters"]
    }, indent=2)


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


# ----------------------------------------------------------------------------------
# Frame IO: async halves run ON the iroh loop; *_from_worker wrappers marshal a
# server-worker/receive thread onto it and wait. One thread drives one connection.
# ----------------------------------------------------------------------------------
async def _send_one_length_prefixed_frame_on_loop(send_stream, frame_body_bytes: bytes) -> None:
    await send_stream.write_all(len(frame_body_bytes).to_bytes(4, "big") + frame_body_bytes)


async def _read_one_length_prefixed_frame_body_on_loop(recv_stream) -> bytes:
    frame_length_header = await recv_stream.read_exact(4)
    frame_length = int.from_bytes(frame_length_header, "big")
    if frame_length <= 0 or frame_length > TRANSFER_MAX_FRAME_BYTES:
        raise ValueError(f"bad transfer frame length {frame_length}")
    return await recv_stream.read_exact(frame_length)


def _send_typed_frame_from_worker_thread(send_stream, frame_type_byte: bytes, payload_bytes: bytes,
                                         timeout_seconds: float = SEND_ONE_FRAME_TIMEOUT_SECONDS) -> None:
    _peer().den_support_run_on_iroh_loop(
        _send_one_length_prefixed_frame_on_loop(send_stream, frame_type_byte + payload_bytes),
        timeout_seconds=timeout_seconds)


def _send_json_frame_from_worker_thread(send_stream, frame_type_byte: bytes, payload_object: Dict[str, Any],
                                        timeout_seconds: float = SEND_ONE_FRAME_TIMEOUT_SECONDS) -> None:
    _send_typed_frame_from_worker_thread(
        send_stream, frame_type_byte,
        json.dumps(payload_object, separators=(",", ":")).encode("utf-8"), timeout_seconds)


def _read_frame_body_from_worker_thread(recv_stream, timeout_seconds: float) -> Optional[bytes]:
    """Read one frame body ([type byte][payload]) or None when the stream has ended
    (peer closed) or the wait timed out. Timeouts abort the transfer at the caller,
    so the coroutine possibly still parked on the loop can never interleave with a
    later read on this stream."""
    try:
        return _peer().den_support_run_on_iroh_loop(
            _read_one_length_prefixed_frame_body_on_loop(recv_stream), timeout_seconds=timeout_seconds)
    except Exception:
        return None


def _parse_json_payload_of_frame(frame_body_bytes: bytes) -> Dict[str, Any]:
    return json.loads(frame_body_bytes[1:].decode("utf-8"))


def _close_transfer_connection_quietly(connection, close_reason_bytes: bytes) -> None:
    try:
        connection.close(0, close_reason_bytes)
    except Exception:
        pass


def _compute_sha256_hex_of_local_file(absolute_file_path: str) -> str:
    sha256_hasher = hashlib.sha256()
    with open(absolute_file_path, "rb") as file_handle:
        while True:
            chunk = file_handle.read(TRANSFER_DATA_CHUNK_BYTES)
            if not chunk:
                break
            sha256_hasher.update(chunk)
    return sha256_hasher.hexdigest()


def _mtime_milliseconds_of_path_without_following_symlinks(absolute_path: str) -> int:
    try:
        return os.lstat(absolute_path).st_mtime_ns // 1_000_000
    except OSError:
        return 0


# ====================================================================================
# SENDER (push): walk sources, dial a fresh den connection, stream lock-step.
# ====================================================================================
def _walk_one_source_collecting_transfer_entries(absolute_path: str, relative_wire_path: str,
                                                 symlink_policy: str, current_depth: int,
                                                 visited_follow_realpaths: set,
                                                 collected_entries: List[Dict[str, Any]]) -> None:
    """Depth-first walk mirroring the app's walk(): dir entries precede their children;
    wire paths always use '/' separators. symlink_policy: skip (default) | link
    (recreate the link, target resolved absolute) | follow (descend into the target,
    with a visited-realpath cycle guard the depth cap alone would not catch on PCs)."""
    if current_depth > MAX_SOURCE_WALK_DEPTH:
        return
    if os.path.islink(absolute_path):
        if symlink_policy == "skip":
            return
        if symlink_policy == "link":
            try:
                resolved_link_target = os.path.realpath(absolute_path)
            except OSError:
                resolved_link_target = ""
            collected_entries.append({
                "relative_wire_path": relative_wire_path, "entry_type": "symlink",
                "absolute_local_path": absolute_path, "size_bytes": 0,
                "mtime_ms": _mtime_milliseconds_of_path_without_following_symlinks(absolute_path),
                "cached_sha256_hex": None, "symlink_target": resolved_link_target})
            return
        # "follow": guard against symlink cycles before descending
        followed_realpath = os.path.realpath(absolute_path)
        if followed_realpath in visited_follow_realpaths:
            return
        visited_follow_realpaths.add(followed_realpath)
    if os.path.isdir(absolute_path):
        collected_entries.append({
            "relative_wire_path": relative_wire_path, "entry_type": "dir",
            "absolute_local_path": absolute_path, "size_bytes": 0,
            "mtime_ms": _mtime_milliseconds_of_path_without_following_symlinks(absolute_path),
            "cached_sha256_hex": None, "symlink_target": None})
        try:
            child_names = sorted(os.listdir(absolute_path))
        except OSError:
            return
        for child_name in child_names:
            _walk_one_source_collecting_transfer_entries(
                os.path.join(absolute_path, child_name), f"{relative_wire_path}/{child_name}",
                symlink_policy, current_depth + 1, visited_follow_realpaths, collected_entries)
    elif os.path.isfile(absolute_path):
        try:
            file_size_bytes = os.stat(absolute_path).st_size
        except OSError:
            file_size_bytes = 0
        collected_entries.append({
            "relative_wire_path": relative_wire_path, "entry_type": "file",
            "absolute_local_path": absolute_path, "size_bytes": file_size_bytes,
            "mtime_ms": _mtime_milliseconds_of_path_without_following_symlinks(absolute_path),
            "cached_sha256_hex": None, "symlink_target": None})


def _send_one_entry_with_per_file_retries(send_stream, recv_stream, entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Send ONE entry (dir/symlink/missing send just F; file sends F + D chunks) and
    await its per-file ack, resending up to MAX_SEND_RETRIES_PER_FILE times when the
    receiver's read-back verify rejects it (corruption anywhere in the path).
    Returns (ok, last_error_text)."""
    last_error_text = ""
    attempt_number = 0
    while attempt_number < MAX_SEND_RETRIES_PER_FILE:
        attempt_number += 1
        file_header_payload: Dict[str, Any] = {
            "path": entry["relative_wire_path"], "type": entry["entry_type"],
            "size": entry["size_bytes"], "mtime_ms": entry["mtime_ms"]}
        if entry["entry_type"] == "file":
            if entry["cached_sha256_hex"] is None:
                entry["cached_sha256_hex"] = _compute_sha256_hex_of_local_file(entry["absolute_local_path"])
            file_header_payload["sha256"] = entry["cached_sha256_hex"]
        if entry["entry_type"] == "symlink":
            file_header_payload["link_target"] = entry["symlink_target"] or ""
        _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_FILE_HEADER, file_header_payload)
        if entry["entry_type"] == "file":
            with open(entry["absolute_local_path"], "rb") as source_file_handle:
                while True:
                    data_chunk = source_file_handle.read(TRANSFER_DATA_CHUNK_BYTES)
                    if not data_chunk:
                        break
                    _send_typed_frame_from_worker_thread(send_stream, FRAME_TYPE_DATA_CHUNK, data_chunk)
        per_file_ack_body = _read_frame_body_from_worker_thread(
            recv_stream, WAIT_FOR_PER_FILE_ACK_TIMEOUT_SECONDS)
        if per_file_ack_body is None:
            # Unlike the app (whose reads only return null on stream END), our None is
            # ambiguous: stream end OR timeout with a read coroutine possibly still
            # parked on the loop. Continuing to the NEXT entry could interleave two
            # readers on one stream, so a missing ack aborts the WHOLE push.
            raise RuntimeError(f"no per-file ack from receiver for "
                               f"'{entry['relative_wire_path']}' (connection lost or peer stalled)")
        per_file_ack = _parse_json_payload_of_frame(per_file_ack_body)
        if per_file_ack.get("ok"):
            return True, ""
        last_error_text = str(per_file_ack.get("error") or "receiver rejected the file")
        MCPLogger.log(TOOL_LOG_NAME,
                      f"receiver rejected {entry['relative_wire_path']} (attempt {attempt_number}): {last_error_text}")
    return False, last_error_text


def _execute_push_transfer_to_admitted_peer_blocking(peer_reference: str,
                                                     source_path_strings: List[str],
                                                     remote_destination_root: str,
                                                     transfer_options: Dict[str, Any]) -> Dict[str, Any]:
    """The whole sender side, run on the server worker thread handling the fs call.
    Mirrors the app's DenFileTransfer.runPush() step-for-step."""
    den_tool = _den()
    peer_tool = _peer()
    resolved_peer = den_tool.den_support_resolve_admitted_peer_reference(peer_reference)
    expected_peer_endpoint_id = resolved_peer["endpoint_id"]
    peer_tool.den_support_ensure_endpoint_started()

    symlink_policy = str(transfer_options.get("symlinks", "skip"))
    collected_entries: List[Dict[str, Any]] = []
    for source_path_string in source_path_strings:
        absolute_source_path = os.path.abspath(os.path.expanduser(source_path_string))
        source_basename = os.path.basename(absolute_source_path.rstrip("/\\")) or absolute_source_path
        if not os.path.lexists(absolute_source_path):
            # Mirror the app: a missing source still travels as an entry (type
            # "missing"), so an atomic-mode RECEIVER sees the failure and rolls back.
            collected_entries.append({
                "relative_wire_path": source_basename, "entry_type": "missing",
                "absolute_local_path": absolute_source_path, "size_bytes": 0, "mtime_ms": 0,
                "cached_sha256_hex": None, "symlink_target": None})
            continue
        _walk_one_source_collecting_transfer_entries(
            absolute_source_path, source_basename, symlink_policy, 0, set(), collected_entries)

    # Dial a FRESH connection on the den ALPN: stored ticket first (direct/LAN
    # addresses), then relay-by-EndpointId - the same ladder as den.py's lazy dial.
    alpn_bytes = DEN_MCP_SESSION_ALPN.encode("utf-8")
    stored_ticket = resolved_peer.get("last_known_ticket")
    connection = None
    send_stream = None
    recv_stream = None
    dial_attempt_error_texts: List[str] = []

    async def _dial_and_open_transfer_stream_on_loop(ticket_string_or_none):
        dialed_connection = await peer_tool.den_support_dial_connection_on_loop(
            ticket_string_or_none, expected_peer_endpoint_id, alpn_bytes)
        opened_bi_stream = await dialed_connection.open_bi()
        return (dialed_connection, opened_bi_stream.send(), opened_bi_stream.recv(),
                str(dialed_connection.remote_id()))

    for ticket_to_try in ([stored_ticket, None] if stored_ticket else [None]):
        try:
            connection, send_stream, recv_stream, dialed_remote_id = peer_tool.den_support_run_on_iroh_loop(
                _dial_and_open_transfer_stream_on_loop(ticket_to_try), timeout_seconds=45.0)
        except Exception as dial_error:
            dial_attempt_error_texts.append(
                f"{'ticket' if ticket_to_try else 'relay-by-id'} dial failed: {dial_error!r}")
            connection = None
            continue
        if dialed_remote_id.strip().lower() != expected_peer_endpoint_id:
            # A stale ticket reached a DIFFERENT endpoint: never stream files to it.
            _close_transfer_connection_quietly(connection, b"wrong endpoint")
            dial_attempt_error_texts.append(f"ticket reached unexpected endpoint {dialed_remote_id[:16]}")
            connection = None
            continue
        break
    if connection is None:
        return {"ok": False, "error": "could not reach peer '" + str(resolved_peer.get("display_name"))
                + "': " + "; ".join(dial_attempt_error_texts)}

    try:
        stat_only_manifest = [
            {"path": entry["relative_wire_path"], "type": entry["entry_type"],
             "size": entry["size_bytes"], "mtime_ms": entry["mtime_ms"]}
            for entry in collected_entries]
        _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_HELLO, {
            "file_transfer": 1, "op": "push", "dest_root": remote_destination_root or "",
            "options": transfer_options, "manifest": stat_only_manifest})

        start_ack_body = _read_frame_body_from_worker_thread(recv_stream, WAIT_FOR_START_ACK_TIMEOUT_SECONDS)
        if start_ack_body is None:
            return {"ok": False, "error": "no ACK from receiver (is the peer app running + admitted us?)"}
        receiver_already_has: Dict[str, Tuple[int, int]] = {}
        for have_entry in (_parse_json_payload_of_frame(start_ack_body).get("have") or []):
            if isinstance(have_entry, dict):
                receiver_already_has[str(have_entry.get("path"))] = (
                    int(have_entry.get("size") or 0), int(have_entry.get("mtime_ms") or 0))

        sender_side_results: List[Dict[str, Any]] = []
        every_sent_entry_was_accepted = True
        sent_entry_count = 0
        skipped_entry_count = 0
        for entry in collected_entries:
            if entry["entry_type"] == "file":
                already_present = receiver_already_has.get(entry["relative_wire_path"])
                if (already_present is not None
                        and already_present[0] == entry["size_bytes"]
                        and already_present[1] == entry["mtime_ms"]):
                    skipped_entry_count += 1
                    continue
            entry_was_accepted, entry_error_text = _send_one_entry_with_per_file_retries(
                send_stream, recv_stream, entry)
            entry_result: Dict[str, Any] = {"path": entry["relative_wire_path"], "ok": entry_was_accepted}
            if not entry_was_accepted:
                entry_result["error"] = entry_error_text
                every_sent_entry_was_accepted = False
            else:
                sent_entry_count += 1
            sender_side_results.append(entry_result)

        _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_END_OF_MANIFEST, {"done": True})

        final_result_body = _read_frame_body_from_worker_thread(
            recv_stream, WAIT_FOR_FINAL_RESULT_TIMEOUT_SECONDS)
        receiver_final_result = (_parse_json_payload_of_frame(final_result_body)
                                 if final_result_body is not None else {"ok": False, "error": "no final result frame"})
        return {"ok": bool(every_sent_entry_was_accepted and receiver_final_result.get("ok")),
                "peer": resolved_peer.get("display_name"),
                "sent": sent_entry_count, "skipped": skipped_entry_count,
                "receiver": receiver_final_result, "sender_results": sender_side_results}
    except Exception as push_error:
        MCPLogger.log(TOOL_LOG_NAME, f"push failed: {push_error!r}")
        return {"ok": False, "error": f"push failed: {push_error!r}"}
    finally:
        _close_transfer_connection_quietly(connection, b"done")


# ====================================================================================
# RECEIVER (inbound push): den.py routes here when a connection's first frame is 'H'.
# ====================================================================================
def accept_inbound_transfer_connection_from_den(connection, send_stream, recv_stream,
                                                first_frame_body: bytes,
                                                peer_endpoint_id_hex: str) -> None:
    """Called by den.py ON the iroh loop after it peeked a transfer hello on an
    ADMITTED peer's inbound connection (admission was already enforced there).
    Spawns the blocking receive worker and returns immediately (never blocks the loop)."""
    receive_worker_thread = threading.Thread(
        target=_run_inbound_push_receive_blocking,
        args=(connection, send_stream, recv_stream, first_frame_body, peer_endpoint_id_hex),
        name=f"den-file-receive-{peer_endpoint_id_hex[:8]}", daemon=True)
    receive_worker_thread.start()


def _resolve_inbound_destination_root(requested_dest_root: str) -> str:
    """Absolute request = exact placement (permissive by design: the sender is an
    ADMITTED peer, i.e. the owner's own device - doc 106 sec 6). Anything else lands
    in the default inbox, exactly like the app."""
    requested_dest_root = (requested_dest_root or "").strip()
    if requested_dest_root and os.path.isabs(requested_dest_root):
        destination_root = requested_dest_root
    else:
        destination_root = os.path.join(os.path.expanduser("~"),
                                        DEFAULT_INBOUND_TRANSFER_INBOX_RELATIVE_TO_HOME)
    os.makedirs(destination_root, exist_ok=True)
    return destination_root


def _resolved_path_stays_within_destination_root(candidate_path: str, destination_root: str) -> bool:
    """Path-traversal guard: the entry's RESOLVED path must stay under the resolved
    destination root ('..' segments, absolute leaks and drive-letter tricks all fail
    this containment check). normcase makes the compare correct on Windows."""
    try:
        resolved_candidate = os.path.normcase(os.path.realpath(candidate_path))
        resolved_root = os.path.normcase(os.path.realpath(destination_root))
        return resolved_candidate == resolved_root or resolved_candidate.startswith(resolved_root + os.sep)
    except OSError:
        return False


def _promote_verified_temp_file_onto_final_name(temp_file_path: str, final_file_path: str) -> None:
    """Atomically move a VERIFIED .afpart temp onto its final name (replace if present).
    Temp and final share a directory, so os.replace is atomic; a copy fallback covers
    exotic filesystems where replace still fails."""
    os.makedirs(os.path.dirname(final_file_path) or ".", exist_ok=True)
    try:
        os.replace(temp_file_path, final_file_path)
    except OSError:
        with open(temp_file_path, "rb") as temp_handle, open(final_file_path, "wb") as final_handle:
            while True:
                chunk = temp_handle.read(TRANSFER_DATA_CHUNK_BYTES)
                if not chunk:
                    break
                final_handle.write(chunk)
        try:
            os.remove(temp_file_path)
        except OSError:
            pass


def _apply_mtime_milliseconds_if_positive(target_path: str, mtime_ms: int) -> None:
    if mtime_ms and mtime_ms > 0:
        try:
            os.utime(target_path, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
        except OSError:
            pass


def _receive_one_entry_writing_and_verifying(recv_stream, file_header: Dict[str, Any],
                                             destination_root: str, error_mode: str,
                                             staged_atomic_promotions: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Handle ONE F-headed entry: dir=mkdirs, symlink=recreate, file=stream D chunks
    to a .afpart temp hashing on the way, then RE-READ it from disk and re-hash
    (end-to-end corruption catch), then promote (or stage, in atomic mode).
    Data frames are DRAINED even when a guard fails, to keep the stream aligned."""
    relative_wire_path = str(file_header.get("path") or "")
    entry_type = str(file_header.get("type") or "")
    entry_result: Dict[str, Any] = {"path": relative_wire_path}
    final_target_path = os.path.join(destination_root, *relative_wire_path.split("/"))
    entry_path_is_contained = _resolved_path_stays_within_destination_root(final_target_path, destination_root)

    if entry_type == "file":
        expected_size_bytes = int(file_header.get("size") or 0)
        expected_sha256_hex = str(file_header.get("sha256") or "")
        temp_file_path = final_target_path + ".afpart"
        write_succeeded = entry_path_is_contained
        temp_file_handle = None
        if entry_path_is_contained:
            try:
                os.makedirs(os.path.dirname(final_target_path) or ".", exist_ok=True)
                temp_file_handle = open(temp_file_path, "wb")
            except OSError:
                write_succeeded = False
        in_memory_hasher = hashlib.sha256()
        received_byte_count = 0
        while received_byte_count < expected_size_bytes:
            data_frame_body = _read_frame_body_from_worker_thread(
                recv_stream, RECEIVER_WAIT_FOR_NEXT_FRAME_TIMEOUT_SECONDS)
            if data_frame_body is None or data_frame_body[:1] != FRAME_TYPE_DATA_CHUNK:
                break
            data_chunk = data_frame_body[1:]
            if temp_file_handle is not None:
                try:
                    temp_file_handle.write(data_chunk)
                except OSError:
                    write_succeeded = False
            in_memory_hasher.update(data_chunk)
            received_byte_count += len(data_chunk)
        if temp_file_handle is not None:
            try:
                temp_file_handle.close()
            except OSError:
                pass
        if not entry_path_is_contained:
            entry_result.update(ok=False, error="path escapes destination root")
            return entry_result
        if not write_succeeded or received_byte_count != expected_size_bytes:
            try:
                os.remove(temp_file_path)
            except OSError:
                pass
            entry_result.update(ok=False,
                                error=f"write failed ({received_byte_count}/{expected_size_bytes} bytes)")
            return entry_result
        in_memory_sha256_hex = in_memory_hasher.hexdigest()
        try:
            on_disk_sha256_hex = _compute_sha256_hex_of_local_file(temp_file_path)
        except OSError:
            on_disk_sha256_hex = ""
        if expected_sha256_hex and (in_memory_sha256_hex != expected_sha256_hex
                                    or on_disk_sha256_hex != expected_sha256_hex):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass
            entry_result.update(ok=False, error="sha256 mismatch")
            return entry_result
        _apply_mtime_milliseconds_if_positive(temp_file_path, int(file_header.get("mtime_ms") or 0))
        if error_mode == "atomic":
            staged_atomic_promotions.append((temp_file_path, final_target_path))
        else:
            _promote_verified_temp_file_onto_final_name(temp_file_path, final_target_path)
        entry_result.update(ok=True, bytes=received_byte_count)
        return entry_result

    # dir / symlink / anything else: no data frames to drain
    if not entry_path_is_contained:
        entry_result.update(ok=False, error="path escapes destination root")
        return entry_result
    if entry_type == "dir":
        try:
            os.makedirs(final_target_path, exist_ok=True)
        except OSError:
            pass
        _apply_mtime_milliseconds_if_positive(final_target_path, int(file_header.get("mtime_ms") or 0))
        entry_result.update(ok=os.path.isdir(final_target_path))
        return entry_result
    if entry_type == "symlink":
        try:
            os.makedirs(os.path.dirname(final_target_path) or ".", exist_ok=True)
            os.symlink(str(file_header.get("link_target") or ""), final_target_path)
            entry_result.update(ok=True)
        except OSError as symlink_error:
            entry_result.update(ok=False, error=f"symlink create failed: {symlink_error}")
        return entry_result
    entry_result.update(ok=False, error=f"unknown entry type '{entry_type}'")
    return entry_result


def _run_inbound_push_receive_blocking(connection, send_stream, recv_stream,
                                       first_frame_body: bytes, peer_endpoint_id_hex: str) -> None:
    """The whole receiver side, on its own daemon thread. Mirrors the app's
    DenFileTransfer.runReceive() step-for-step, including the final blocking read
    that guarantees the R frame is delivered before the connection dies."""
    try:
        transfer_hello = _parse_json_payload_of_frame(first_frame_body)
        if transfer_hello.get("op") != "push":
            MCPLogger.log(TOOL_LOG_NAME,
                          f"inbound transfer from {peer_endpoint_id_hex[:16]} refused: only op:push is supported in v1")
            return
        transfer_options = transfer_hello.get("options") or {}
        error_mode = str(transfer_options.get("error_mode", "continue"))
        destination_root = _resolve_inbound_destination_root(str(transfer_hello.get("dest_root") or ""))
        MCPLogger.log(TOOL_LOG_NAME,
                      f"inbound push from {peer_endpoint_id_hex[:16]} -> {destination_root} "
                      f"({len(transfer_hello.get('manifest') or [])} manifest entries, mode {error_mode})")

        # A {have}: tell the sender which manifest files we already hold byte-identically
        # in spirit (size+mtime match), so it skips them (that is also the resume path).
        already_present_entries = []
        for manifest_entry in (transfer_hello.get("manifest") or []):
            if not isinstance(manifest_entry, dict) or manifest_entry.get("type") != "file":
                continue
            candidate_existing_path = os.path.join(
                destination_root, *str(manifest_entry.get("path") or "").split("/"))
            try:
                existing_stat = os.stat(candidate_existing_path)
            except OSError:
                continue
            if not os.path.isfile(candidate_existing_path):
                continue
            existing_mtime_ms = existing_stat.st_mtime_ns // 1_000_000
            if (existing_stat.st_size == int(manifest_entry.get("size") or -1)
                    and existing_mtime_ms == int(manifest_entry.get("mtime_ms") or -1)):
                already_present_entries.append({
                    "path": manifest_entry.get("path"),
                    "size": existing_stat.st_size, "mtime_ms": existing_mtime_ms})
        _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_ACK, {"have": already_present_entries})

        per_entry_results: List[Dict[str, Any]] = []
        staged_atomic_promotions: List[Tuple[str, str]] = []
        every_entry_verified_ok = True
        while True:
            frame_body = _read_frame_body_from_worker_thread(
                recv_stream, RECEIVER_WAIT_FOR_NEXT_FRAME_TIMEOUT_SECONDS)
            if frame_body is None:
                break
            frame_type = frame_body[:1]
            if frame_type == FRAME_TYPE_END_OF_MANIFEST:
                break
            if frame_type == FRAME_TYPE_FILE_HEADER:
                file_header = _parse_json_payload_of_frame(frame_body)
                entry_result = _receive_one_entry_writing_and_verifying(
                    recv_stream, file_header, destination_root, error_mode, staged_atomic_promotions)
                per_entry_results.append(entry_result)
                entry_was_verified = bool(entry_result.get("ok"))
                if not entry_was_verified:
                    every_entry_verified_ok = False
                # per-file ack drives the sender's retry loop
                _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_ACK, {
                    "path": file_header.get("path"), "ok": entry_was_verified,
                    "error": str(entry_result.get("error") or "")})
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"receiver: unexpected frame type {frame_type!r}")

        if error_mode == "atomic":
            if every_entry_verified_ok:
                for temp_file_path, final_file_path in staged_atomic_promotions:
                    _promote_verified_temp_file_onto_final_name(temp_file_path, final_file_path)
            else:
                for temp_file_path, _final_file_path in staged_atomic_promotions:
                    try:
                        os.remove(temp_file_path)
                    except OSError:
                        pass

        _send_json_frame_from_worker_thread(send_stream, FRAME_TYPE_RESULT, {
            "ok": every_entry_verified_ok, "results": per_entry_results,
            "dest_root": destination_root, "mode": error_mode})
        # Keep the connection open until the SENDER has consumed R and closes first --
        # a QUIC close() can discard in-flight stream data, so tearing down right
        # after sending R would sometimes lose it (bug found + fixed app-side). This
        # read returns None on the sender's close, guaranteeing R was delivered.
        _read_frame_body_from_worker_thread(recv_stream, WAIT_FOR_FINAL_RESULT_TIMEOUT_SECONDS)
        MCPLogger.log(TOOL_LOG_NAME,
                      f"inbound push from {peer_endpoint_id_hex[:16]} finished: ok={every_entry_verified_ok}, "
                      f"{len(per_entry_results)} entr(y/ies) -> {destination_root}")
    except Exception as receive_error:
        MCPLogger.log(TOOL_LOG_NAME, f"inbound transfer receive failed: {receive_error!r}")
    finally:
        _close_transfer_connection_quietly(connection, b"done")


# ----------------------------------------------------------------------------------
# MCP handler (reached via fs operation "transfer"; fs injects our unlock token)
# ----------------------------------------------------------------------------------
def handle_transfer(input_param: Dict) -> Dict:
    """Validate the transfer request and run the (synchronous, v1) push."""
    transfer_direction = str(input_param.get("direction") or "push").strip().lower()
    if transfer_direction == "pull":
        return create_error_response(
            "direction 'pull' is not built yet (v1 is push-only). Ask the REMOTE end to "
            "push instead: its files bridge / fs tool has the same engine.", with_readme=False)
    if transfer_direction != "push":
        return create_error_response(f"Unknown direction '{transfer_direction}' (use 'push').",
                                     with_readme=False)
    peer_reference = str(input_param.get("peer") or "").strip()
    if not peer_reference:
        return create_error_response("'peer' is required: an admitted den peer's display name "
                                     "or 64-hex EndpointId (see the den tool's list_den).",
                                     with_readme=False)
    raw_source_paths = input_param.get("src")
    if isinstance(raw_source_paths, str):
        source_path_strings = [raw_source_paths]
    elif isinstance(raw_source_paths, list) and raw_source_paths:
        source_path_strings = [str(one_path) for one_path in raw_source_paths]
    else:
        return create_error_response("'src' is required: a local path (string) or a non-empty "
                                     "list of local paths.", with_readme=False)
    remote_destination_root = str(input_param.get("dest") or "")
    transfer_options = input_param.get("options") if isinstance(input_param.get("options"), dict) else {}

    try:
        transfer_result = _execute_push_transfer_to_admitted_peer_blocking(
            peer_reference, source_path_strings, remote_destination_root, transfer_options)
    except ValueError as peer_resolution_error:
        return create_error_response(str(peer_resolution_error), with_readme=False)
    except Exception as transfer_error:
        MCPLogger.log(TOOL_LOG_NAME, f"transfer failed: {transfer_error!r}")
        return create_error_response(f"transfer failed: {transfer_error!r}", with_readme=False)

    return {"content": [{"type": "text", "text": json.dumps(transfer_result, indent=2)}],
            "isError": not bool(transfer_result.get("ok"))}


def handle_file_transfer(input_param: Dict) -> Dict:
    """Handle file transfer tool operations via MCP interface (fs delegate entry)."""
    try:
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}

        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format", with_readme=True)

        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)

        operation = input_param.get("operation")
        if operation == "transfer":
            return handle_transfer(input_param)
        return create_error_response(f"Unknown operation: '{operation}'", with_readme=True)

    except Exception as handler_error:
        return create_error_response(f"Error: {str(handler_error)}", with_readme=True)


# Reached ONLY through the consolidated "fs" tool (fs operation "transfer") and,
# for inbound transfers, through den.py's first-frame routing.  Empty TOOLS/
# HANDLERS make the tool loader register no standalone tool for this module.
TOOLS = []
HANDLERS = {}
