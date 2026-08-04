"""
File: ragtag/tools/remote.py
Project: Aura Friday MCP-Link Server
Component: Remote Tool Registration
Author: Christopher Nathan Drake (cnd)

Tool implementation for allowing external tools to register themselves
for relay operations through the MCP interface.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "1ɡⅮpƳ𝕌ƐƲ𝟤ƙⲞᏎЅΟƲ𐓒ⲢPυᎠҮΟꜱᴜZƬᴅꓓƟꓦꓳȠΚƲꓮωցսiᴡꜱᴠVᏮһgрᎠƊΒXƬսʌⲔ×Ƶŧꓧ𝟑ᴜҮJ4νᏎƻyƼ1ƨHһЗꓜ7Ðһ𝟟ıŪᗅƬ𝛢BуƵꙅƘᏟYʈЅƿꓜŧ2𝟪𝟫ΝƲÞĐⲦÐƲhEƵ"
"signdate": "2026-07-29T09:30:27.183Z",

"""

from typing import Dict, Callable, Optional
import time, uuid, traceback
import json
import re
import threading
from easy_mcp.server import MCPLogger
from . import get_server, get_authenticated_user

# PURE RELAY -- remote.py OWNS NO unlock token of its own, and (2026-07 pure-relay migration) it
# does NOT mint, store, validate, strip, or answer tokens/readme for the tools that register
# through it. WHY: a tool_unlock_token is a COMPREHENSION GATE (proof the caller has read THAT
# tool's readme), NOT authentication and NOT a secret. Each tool must OWN its token and version-
# lock it to ITS OWN code, so the token rotates when the tool changes and forces a re-read. The
# `remote` tool itself is machine-only ("Do not call directly") and is gated by the registrant-
# chosen TOOL_API_KEY (a genuine secret for re-register/unregister -- a DIFFERENT thing from a
# comprehension token), so it needs no comprehension token. The handler below forwards every
# operation (readme, get_unlock_token, tool_unlock_token, ...) verbatim to the registrant, which
# answers readme (carrying its own current token) and validates the token itself.
# See doc/50_non-AI-calling-and-how-to-get-unlock-tokens.md.

# Registrant-supplied text/schema size caps (a hostile registrant must not be able to
# inject megabytes into every AI conversation via descriptions/readmes).
# MAX_DESCRIPTION_LENGTH raised 4096 -> 16384 (same as MAX_README_LENGTH): the real
# chrome_browser extension sends a legitimate 8646-char description, which the old cap
# rejected, silently keeping the tool out of tools/list. The caps still bound total
# registrant text to tens of KB, which is all the hostile-registrant defence needs.
MAX_DESCRIPTION_LENGTH = 16384
MAX_README_LENGTH = 16384
MAX_PARAMETERS_JSON_LENGTH = 32768
REMOTE_TOOL_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

# Relayed calls must not leave the original caller hanging forever: each pending call
# gets a reply deadline and a background reaper notifies the caller on expiry.
DEFAULT_REPLY_TIMEOUT_SECONDS = 60.0
MIN_REPLY_TIMEOUT_SECONDS = 1.0
MAX_REPLY_TIMEOUT_SECONDS = 3600.0
PENDING_CALL_SWEEP_INTERVAL_SECONDS = 5.0

# One lock guards BOTH registries below: registration, relay, reply, unregister and
# session-cleanup all run on different request threads.
_registry_and_pending_calls_lock = threading.Lock()

# Registry to store registered tools
# Format: {final_tool_name: {description, parameters, synthetic_parameters,
#          original_parameters, callback_endpoint, api_key, readme, tool_unlock_token,
#          registered_at, registered_by, handler_info}}
registered_tools = {}

# Storage for pending relayed tool calls, keyed by call_id.
# Format: {call_id: {tool_args, tool_name, request_id, caller_session_id,
#          registrant_session_id, created_at, reply_deadline, reply_timeout_seconds}}
# Only minimal serializable context is stored (no live socket/server objects).
pending_tool_calls = {}

# Flag to track if cleanup callback has been registered
_cleanup_callback_registered = False

# Flag to track if the pending-call timeout reaper thread has been started
_pending_call_reaper_thread_started = False

# Tool definitions
TOOLS = [
    {
        "name": "remote",
        "description": "Internal tool for external systems to register remote tools. Do not call directly.",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "do not use."
                }
            },
            "required": [],
            "type": "object"
        },

        "real_parameters": { # Caller will pass "input":{"operation":"register", ...} to use this tool.
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["register", "unregister", "list"],
                    "description": "Operation to perform. register: add a tool (requires tool_name, description, parameters, TOOL_API_KEY). unregister: remove a tool you registered (requires tool_name plus the same TOOL_API_KEY used at registration). list: show registered remote tools."
                },
                "tool_name": {
                    "title": "Tool Name",
                    "type": "string",
                    "description": "Name of the tool to register/unregister. Allowed characters: A-Z a-z 0-9 _ - (max 64)."
                },
                "description": {
                    "title": "Description",
                    "type": "string",
                    "description": "Description of what the tool does"
                },
                "parameters": {
                    "title": "Parameters",
                    "type": "object",
                    "description": "JSON schema for tool parameters"
                },
                "callback_endpoint": {
                    "title": "Callback Endpoint",
                    "type": "string",
                    "description": "Optional metadata URL recorded with the registration. The relay itself always uses the registrant's SSE session, not this URL."
                },
                "readme": {
                    "title": "readme magic-key",
                    "type": "string",
                    "description": "A VERY SHORT one or two-line description saying (1) Briefly: what this tool does, and (2) Briefly: when the AI will need to use it" # if "readme" key exists, it will be swapped with "description" and parameters will be renamed to real_parameters with tool_unlock_token added.
                },
                "TOOL_API_KEY": {
                    "title": "API Key",
                    "type": "string",
                    "description": "Registrant-chosen secret. The same value authorizes same-name re-registration (replacement) and unregister."
                }
            },
            "required": ["tool_name", "description", "parameters", "TOOL_API_KEY"],
            "type": "object"
        }
    }
]

def create_error_response(error_msg: str) -> Dict:
    """Log and create an error response."""
    MCPLogger.log("REMOTE", f"Error: {error_msg}")
    return { 
        "content": [{"type": "text", "text": error_msg}], 
        "isError": True 
    }

def resolve_tool_name_conflict(base_name: str) -> str:
    """Resolve naming conflicts by appending numbers.

    Consults BOTH the remote registry and the server's full tool registry, so a remote
    registrant can never shadow a built-in tool (e.g. python, sqlite).
    Assumes _registry_and_pending_calls_lock is held by the caller.
    """
    server = get_server()

    def _name_is_taken(candidate_name: str) -> bool:
        if candidate_name in registered_tools:
            return True
        return bool(server) and candidate_name in server.tool_handlers

    if not _name_is_taken(base_name):
        return base_name

    counter = 2
    while _name_is_taken(f"{base_name}{counter}"):
        counter += 1

    return f"{base_name}{counter}"

def _unregister_tool_assuming_lock_held(tool_name: str) -> None:
    """Remove one registered tool from both registries (idempotent).

    Single shared helper for re-registration cleanup, unregister and session cleanup.
    Assumes _registry_and_pending_calls_lock is held by the caller.
    """
    registered_tools.pop(tool_name, None)
    server = get_server()
    if server:
        server.tool_handlers.pop(tool_name, None)
    MCPLogger.log("REMOTE", f"Unregistered remote tool {tool_name}")

# JSON-schema primitive type name -> acceptable Python types, for pre-relay validation.
_JSON_SCHEMA_TYPE_TO_PYTHON_TYPES = {
    'string': (str,),
    'number': (int, float),
    'integer': (int,),
    'boolean': (bool,),
    'array': (list,),
    'object': (dict,),
}

def _validate_relayed_args_against_original_schema(relayed_args: Dict, original_parameters_schema: Dict) -> Optional[str]:
    """Type-check declared properties of a relayed call against the registrant's schema.

    Only checks TYPE mismatches for properties the schema declares.  It deliberately does
    NOT enforce 'required' and does NOT reject undeclared keys, because real registrants
    (e.g. the browser extension) accept call forms looser than their registered schema.
    Returns an error message string, or None when acceptable.
    """
    try:
        if not isinstance(original_parameters_schema, dict):
            return None
        declared_properties = original_parameters_schema.get('properties', {})
        if not isinstance(declared_properties, dict):
            return None
        type_mismatch_messages = []
        for arg_name, arg_value in relayed_args.items():
            declared_schema = declared_properties.get(arg_name)
            if not isinstance(declared_schema, dict):
                continue
            declared_type = declared_schema.get('type')
            acceptable_python_types = _JSON_SCHEMA_TYPE_TO_PYTHON_TYPES.get(declared_type)
            if not acceptable_python_types:
                continue
            if declared_type in ('number', 'integer') and isinstance(arg_value, bool):
                # bool is a subclass of int in Python; a boolean is NOT an acceptable number
                type_mismatch_messages.append(f"'{arg_name}' must be a {declared_type}, got boolean")
            elif not isinstance(arg_value, acceptable_python_types):
                type_mismatch_messages.append(f"'{arg_name}' must be a {declared_type}, got {type(arg_value).__name__}")
        if type_mismatch_messages:
            return "; ".join(type_mismatch_messages)
        return None
    except Exception:
        return None  # validation must never block a call

def _send_json_rpc_error_to_original_caller(pending_call_context: Dict, error_message: str) -> None:
    """Best-effort JSON-RPC error to the session that originally called the remote tool."""
    try:
        server = get_server()
        if not server:
            return
        error_response = {
            "jsonrpc": "2.0",
            "id": pending_call_context.get("request_id"),
            "error": {
                "code": -32000,
                "message": error_message
            }
        }
        server._send_response(pending_call_context.get("caller_session_id"), error_response)
    except Exception as send_error:
        MCPLogger.log("REMOTE", f"Warning: could not deliver error to original caller: {send_error}")

def _sweep_expired_pending_tool_calls() -> None:
    """Fail-and-remove pending relayed calls whose reply deadline has passed."""
    now = time.time()
    expired_entries = []
    with _registry_and_pending_calls_lock:
        for call_id in list(pending_tool_calls.keys()):
            if now >= pending_tool_calls[call_id].get("reply_deadline", 0):
                expired_entries.append((call_id, pending_tool_calls.pop(call_id)))
    for call_id, pending_call_context in expired_entries:
        timeout_seconds = pending_call_context.get("reply_timeout_seconds", DEFAULT_REPLY_TIMEOUT_SECONDS)
        tool_name = pending_call_context.get("tool_name")
        MCPLogger.log("REMOTE", f"Pending call {call_id} for tool {tool_name} expired after {timeout_seconds:.0f}s without a reply")
        _send_json_rpc_error_to_original_caller(
            pending_call_context,
            f"Remote tool '{tool_name}' did not reply within {timeout_seconds:.0f} seconds"
        )

def _pending_call_reaper_loop() -> None:
    while True:
        try:
            time.sleep(PENDING_CALL_SWEEP_INTERVAL_SECONDS)
            _sweep_expired_pending_tool_calls()
        except Exception as reaper_error:
            MCPLogger.log("REMOTE", f"Pending-call reaper error: {reaper_error}")

def _ensure_pending_call_reaper_thread_started() -> None:
    """Start the timeout reaper thread lazily on first relayed call (never at import)."""
    global _pending_call_reaper_thread_started
    with _registry_and_pending_calls_lock:
        if _pending_call_reaper_thread_started:
            return
        _pending_call_reaper_thread_started = True
    reaper_thread = threading.Thread(target=_pending_call_reaper_loop, name="remote_pending_call_reaper", daemon=True)
    reaper_thread.start()

def create_remote_tool_handler(tool_name: str) -> Callable:
    """Create a handler function for a remotely registered tool.

    The relay is pure SSE-reverse: calls are pushed down the REGISTRANT's SSE session and
    the reply comes back later via a tools/reply request (see _handle_tool_reply).
    """
    def handler(tool_args: Dict) -> Dict:
        try:
            if not isinstance(tool_args, dict):
                return create_error_response(f"Invalid arguments for {tool_name}: expected an object")

            MCPLogger.log("REMOTE", f"Tool {tool_name} called with argument keys: {sorted(k for k in tool_args.keys() if k != 'handler_info')}")

            while isinstance(tool_args, dict) and "input" in tool_args and isinstance(tool_args["input"], dict):
                handler_info = tool_args.get("handler_info", None) # keep handler_info if it exists.
                tool_args = tool_args["input"] # unwrap if double+ wrapped by mistake.
                if handler_info is not None: tool_args["handler_info"] = handler_info # keep handler_info if it exists.

            # PURE RELAY: forward EVERY operation verbatim to the registrant -- including readme,
            # get_unlock_token and any tool_unlock_token. The tool OWNS its comprehension gate: it
            # answers readme (returning its own current token) and validates the token itself,
            # returning its readme on a miss. remote.py neither mints, stores, validates, strips,
            # nor answers tokens/readme here (see the module note at the top of this file).
            with _registry_and_pending_calls_lock:
                tool_registration = registered_tools.get(tool_name)
            if tool_registration is None:
                return create_error_response(f"Remote tool {tool_name} is no longer registered")

            # Extract handler_info before removing it
            handler_info = tool_args.get('handler_info', {})
            registrant_session_id = tool_registration.get('handler_info', {}).get('session_id')
            request_id = handler_info.get('request_id')
            tool_name_from_info = handler_info.get('tool_name') or tool_name
            caller_session_id = handler_info.get('session_id')
            call_id = f"{uuid.uuid4()}"
            temp_args = tool_args.copy()

            # Strip ONLY the server-internal routing metadata. Do NOT strip tool_unlock_token: the
            # registrant owns the gate and must receive the token to validate it itself.
            temp_args.pop('handler_info', None) #   server.py:  tool_args['handler_info'] = {'tool_name':tool_name, 'session_id':session_id, 'request_id':request_id}

            # The legacy synthetic operation:"execute" envelope must not leak to the remote
            # tool; any OTHER operation value is the tool's own and is forwarded verbatim.
            original_declared_properties = (tool_registration.get("original_parameters") or {}).get("properties", {})
            if temp_args.get('operation') == 'execute' and 'operation' not in original_declared_properties:
                temp_args.pop('operation', None)

            # Per-call reply timeout (defaults to DEFAULT_REPLY_TIMEOUT_SECONDS, clamped)
            raw_timeout_value = temp_args.pop('_timeout_seconds', None)
            reply_timeout_seconds = DEFAULT_REPLY_TIMEOUT_SECONDS
            if raw_timeout_value is not None and not isinstance(raw_timeout_value, bool):
                try:
                    reply_timeout_seconds = float(raw_timeout_value)
                except (TypeError, ValueError):
                    reply_timeout_seconds = DEFAULT_REPLY_TIMEOUT_SECONDS
            reply_timeout_seconds = max(MIN_REPLY_TIMEOUT_SECONDS, min(MAX_REPLY_TIMEOUT_SECONDS, reply_timeout_seconds))

            # Cheap local validation against the registrant's declared schema
            validation_error = _validate_relayed_args_against_original_schema(temp_args, tool_registration.get("original_parameters") or {})
            if validation_error:
                return create_error_response(f"Invalid arguments for {tool_name}: {validation_error}")

            # Confirm the registrant's SSE session is still around before dispatching
            server = get_server()
            registrant_session = server.active_sessions.get(registrant_session_id) if (server and registrant_session_id) else None
            if registrant_session is None or not registrant_session.is_active:
                return create_error_response(f"Remote tool {tool_name} is not reachable: its registrant connection is gone")

            # Reconstruct the JSON-RPC request to send to the external tool
            outgoing_request = {
                "method": "tools/call",
                "params": {
                    "name": tool_name_from_info,
                    "arguments": temp_args
                },
                "jsonrpc": "2.0",
                "id": request_id
            }

            # Store minimal serializable context for when the reply comes back
            # (no live socket/server objects - the reply path uses get_server()).
            now = time.time()
            pending_call_context = {
                "tool_args": temp_args,
                "tool_name": tool_name,
                "request_id": request_id,
                "caller_session_id": caller_session_id,
                "registrant_session_id": registrant_session_id,
                "created_at": now,
                "reply_deadline": now + reply_timeout_seconds,
                "reply_timeout_seconds": reply_timeout_seconds
            }
            with _registry_and_pending_calls_lock:
                pending_tool_calls[call_id] = pending_call_context
                pending_call_count = len(pending_tool_calls)
            _ensure_pending_call_reaper_thread_started()

            message = {"jsonrpc": "2.0", "id": call_id, "reverse": {"tool": tool_name, "input": outgoing_request, "call_id": call_id, "isError": False}}

            # Send over the registrant's own SSE session (the reply will come back in to
            # handle_remote as a tools/reply)
            delivered_to_registrant = registrant_session.send_message("message", message)
            if not delivered_to_registrant:
                with _registry_and_pending_calls_lock:
                    pending_tool_calls.pop(call_id, None)
                return create_error_response(f"Could not deliver call to remote tool {tool_name}: send to registrant connection failed")

            MCPLogger.log("REMOTE", f"Relayed call_id {call_id} for tool {tool_name} to registrant session {str(registrant_session_id)[:8]} ({pending_call_count} pending, timeout {reply_timeout_seconds:.0f}s)")
            return None # the reply comes from elsewhere later.

        except Exception as e:
            error_msg = f"Error calling remote tool {tool_name}: {str(e)}"
            MCPLogger.log("REMOTE", error_msg+"\n"+traceback.format_exc())
            return {
                "content": [{"type": "text", "text": error_msg}],
                "isError": True
            }
    
    return handler

def cleanup_tools_for_session(session_id: str) -> None:
    """
    Clean up all tools registered for a specific session, and fail any pending
    relayed calls tied to that session.
    
    Args:
        session_id: The session ID to clean up tools for
    """
    try:
        orphaned_pending_calls = []
        with _registry_and_pending_calls_lock:
            tools_to_remove = [
                tool_name for tool_name, tool_info in registered_tools.items()
                if tool_info.get('handler_info', {}).get('session_id') == session_id
            ]
            for tool_name in tools_to_remove:
                MCPLogger.log("REMOTE", f"Removing tool {tool_name} for session {str(session_id)[:8]}")
                _unregister_tool_assuming_lock_held(tool_name)

            # Pending calls whose registrant or caller died must not linger forever
            for call_id in list(pending_tool_calls.keys()):
                pending_call_context = pending_tool_calls[call_id]
                if session_id in (pending_call_context.get("registrant_session_id"), pending_call_context.get("caller_session_id")):
                    orphaned_pending_calls.append((call_id, pending_tool_calls.pop(call_id)))

        for call_id, pending_call_context in orphaned_pending_calls:
            if pending_call_context.get("registrant_session_id") == session_id and pending_call_context.get("caller_session_id") != session_id:
                _send_json_rpc_error_to_original_caller(
                    pending_call_context,
                    f"Remote tool '{pending_call_context.get('tool_name')}' disconnected before replying"
                )

        if tools_to_remove or orphaned_pending_calls:
            MCPLogger.log("REMOTE", f"Cleaned up {len(tools_to_remove)} tools and {len(orphaned_pending_calls)} pending calls for session {str(session_id)[:8]}: {tools_to_remove}")
        if tools_to_remove:
            # Trigger Cursor reconnect when tools are removed
            trigger_cursor_reconnect_for_tool_changes()
        
    except Exception as e:
        MCPLogger.log("REMOTE", f"Error cleaning up tools for session {session_id}: {str(e)}\n{traceback.format_exc()}")

def trigger_cursor_reconnect_for_tool_changes() -> None:
    """
    Trigger Cursor IDE to reconnect when tools are added or removed.
    This ensures Cursor sees the updated tool list.
    (The server side collapses rapid repeat requests into a single touch,
    so per-registration calls here do not cause a reconnect storm.)
    """
    server = get_server()
    if server:
        try:
            # Spec-correct live refresh for clients that honor it (Cursor does); the
            # config touch below stays as the fallback for clients that do not. Added
            # per doc/tools_list_changed_notification_gap_analysis_and_implementation_plan.md
            # (closes GAP-1/2/3: register, unregister and dead-session cleanup all
            # flow through this function AFTER the registry has been mutated).
            server.schedule_tools_list_changed_notification_after_collapse_window()
        except Exception as notification_error:
            MCPLogger.log("REMOTE", f"Warning: could not schedule tools/list_changed notification: {notification_error}")
        try:
            # Wait 2 seconds to allow the changes to be fully processed
            server.trigger_cursor_reconnect(2)
            MCPLogger.log("REMOTE", "Triggered Cursor reconnect for tool changes")
        except Exception as e:
            MCPLogger.log("REMOTE", f"Warning: Failed to trigger Cursor reconnect: {e}")
    else:
        MCPLogger.log("REMOTE", "Warning: Could not trigger Cursor reconnect - server instance not available")


def readme(input_param: Dict, default_tool_name: Optional[str] = None) -> Dict:
    """Handle readme requests for registered remote tools.

    default_tool_name lets internal callers (e.g. mcp_bridge fetching a token) get the
    readme without server-injected handler_info.
    """
    MCPLogger.log("REMOTE", "synthetic help request")

    try:
        # Extract tool name from handler_info (fall back to the handler's own name)
        handler_info = input_param.get('handler_info', {})
        tool_name = handler_info.get('tool_name') or default_tool_name

        if not tool_name:
            return create_error_response("Could not determine tool name from request")
        
        # Look up the tool in registered_tools
        with _registry_and_pending_calls_lock:
            tool_info = registered_tools.get(tool_name)
        if tool_info is None:
            return create_error_response(f"Tool {tool_name} not found in registered tools")

        # Both fields below were generated together by compress_tool_definition at
        # registration time, so this readme and the synthetic schema stay consistent.
        registration_data = {
            "description": tool_info.get("readme", tool_info.get("description", "")),
            "parameters": tool_info.get("synthetic_parameters", {})
        }

        return {
            "content": [{"type": "text", "text": json.dumps(registration_data, default=str, indent=2)}],
            "isError": False
        }

    except Exception as e:
        MCPLogger.log("REMOTE", f"Error in readme: {str(e)}\n{traceback.format_exc()}")
        return create_error_response(f"Error generating readme: {str(e)}")
    

def handle_remote(input_param: Dict) -> Dict:
    """
    This code has 3 different purposes:-
    1. Handle remote-tool registration/unregistration/listing calls (from tools, not AIs)
    2. Relay incoming tool-call requests coming in from (usually) AI agents out to registered
       remote tools - that path runs through create_remote_tool_handler's handler, which the
       server dispatches to directly via server.tool_handlers.
    3. Handle incoming tool-reply calls coming in from remote tools, and relay those back to
       the caller in step 2.

    Dispatch is on explicit discriminators (request.method / input.operation), never on the
    mere presence of handler_info (which the server injects into every tool call).
    """
    try:
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input: expected an object")

        # Replies from remote tools: server.py routes tools/reply here as
        # {'request': <jsonrpc request>, 'session_id': <replying session>}
        request = input_param.get("request")
        if isinstance(request, dict) and request.get("method") == "tools/reply":
            return _handle_tool_reply(input_param)

        input_wrapper = input_param.get("input")
        operation = input_wrapper.get("operation") if isinstance(input_wrapper, dict) else None
        if operation == "register":
            return handle_registration(input_param)
        if operation == "unregister":
            return handle_unregistration(input_param)
        if operation == "list":
            return handle_list_registered_tools(input_param)

        return create_error_response(f"Unsupported remote operation: '{operation}'. Supported operations: register, unregister, list (plus internal tools/reply).")

    except Exception as e:
        error_msg = f"Error in remote tool dispatch: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


def _handle_tool_reply(input_param: Dict) -> Dict:
    """Match a tools/reply to its pending call and forward the result to the original caller.

    Replies do NOT arrive on the SSE session the reverse call was pushed down (the client
    POSTs its tools/reply on a different /messages session), so we match a reply to its
    pending call purely by call_id -- an unguessable per-call UUIDv4.
    CHANGED 2026-07-20 (cnd request): removed the registrant-session equality gate below; it
    could never match a real SSE reply, so it dropped every reply and timed the caller out.
    """
    replying_session_id = input_param.get("session_id")
    request = input_param.get("request") or {}
    call_id = request.get("id")
    MCPLogger.log("REMOTE", f"tools/reply for call_id {call_id} from session {str(replying_session_id)[:8]}")

    with _registry_and_pending_calls_lock:
        pending_call_context = pending_tool_calls.pop(call_id, None) if call_id else None

    if pending_call_context is None:
        return create_error_response(f"No pending call found for call_id: {call_id}")

    # From here on the pending entry is consumed: any processing failure must still make a
    # best-effort attempt to answer the original caller.
    try:
        result = request.get("params", {}).get("result", {"content": [{"type": "text", "text": "(no result provided)"}], "isError": True})
        if not isinstance(result, dict):
            result = {"content": [{"type": "text", "text": str(result)}], "isError": False}

        # Check if result indicates an error and contains "{see readme}" to replace with actual readme
        if result.get("isError") and "content" in result:
            tool_name = pending_call_context.get("tool_name")

            # Check each content item for "{see readme}"
            for content_item in result.get("content", []):
                if isinstance(content_item, dict) and content_item.get("type") == "text" and "{see readme}" in content_item.get("text", ""):
                    MCPLogger.log("REMOTE", f"Found {{see readme}} in error response for {tool_name}, replacing with actual readme")

                    # Get the readme content
                    readme_response = readme({}, default_tool_name=tool_name)

                    # Extract the readme text
                    readme_text = ""
                    if readme_response and not readme_response.get("isError", False):
                        readme_content = readme_response.get("content", [])
                        if readme_content and len(readme_content) > 0:
                            readme_text = readme_content[0].get("text", "")

                    # Replace {see readme} with actual readme content
                    if readme_text:
                        content_item["text"] = content_item["text"].replace("{see readme}", f"\n\nDocumentation:\n{readme_text}")
                    else:
                        content_item["text"] = content_item["text"].replace("{see readme}", "\n\n[Error: Could not retrieve readme documentation]")

        # Send the response to the original caller
        response = {
            "jsonrpc": "2.0",
            "id": pending_call_context.get("request_id"),
            "result": result
        }
        MCPLogger.log("REMOTE", f"Forwarding reply for call_id {call_id} (isError={result.get('isError')}) to caller session {str(pending_call_context.get('caller_session_id'))[:8]}")
        server = get_server()
        if server:
            server._send_response(pending_call_context.get("caller_session_id"), response)
        else:
            MCPLogger.log("REMOTE", f"Warning: no server instance available to forward reply for call_id {call_id}")

        return {
            "content": [{"type": "text", "text": f"Tool reply processed for call_id {call_id}"}],
            "isError": False
        }
    except Exception as reply_processing_error:
        _send_json_rpc_error_to_original_caller(
            pending_call_context,
            f"Remote tool '{pending_call_context.get('tool_name')}' replied, but processing the reply failed: {reply_processing_error}"
        )
        error_msg = f"Error processing tool reply for call_id {call_id}: {reply_processing_error}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


# Convert a remote-tools schema into our compressed-wrapped equivalent.
def compress_tool_definition(registration_data: Dict, final_tool_name: str) -> Dict:
    """Convert a remote tool's registration data into a compressed wrapped tool definition.
    
    Args:
        registration_data: Complete registration dict with tool_name, description, parameters, etc.
        final_tool_name: The conflict-resolved name the tool is actually registered under
        
    Returns:
        Wrapped tool definition suitable for MCP server registration
    """
    # Extract fields from registration data
    original_description = registration_data.get("description", "(description missing)")
    readme_field = registration_data.get("readme") # e.g. Read from and perform actions using the users actual desktop web browser.
    original_parameters = registration_data.get("parameters", {})
    
    # Determine AI-facing description (use readme if provided, otherwise generate default)
    if readme_field:
        ai_description = readme_field.strip()
    else:
        ai_description = f'Use this tool when you need to access {final_tool_name} functionality'
    
    # Generate parameter examples from original schema
    properties = original_parameters.get("properties", {}) if isinstance(original_parameters, dict) else {}
    required = original_parameters.get("required", []) if isinstance(original_parameters, dict) else []
    
    param_examples = []
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        prop_type = prop_schema.get('type', 'string')
        prop_desc = prop_schema.get('description', '')
        default_value = prop_schema.get('default')
        enum_values = prop_schema.get('enum')

        # Prefer the schema's own default, then its first enum value: agents copy examples
        # literally, so a synthetic "example_action" on an enum-restricted property would
        # guarantee a first-call failure.
        if default_value is not None:
            example_value = json.dumps(default_value)
        elif isinstance(enum_values, list) and enum_values:
            example_value = json.dumps(enum_values[0])
        elif prop_type == 'string':
            example_value = f'"example_{prop_name}"'
        elif prop_type == 'number' or prop_type == 'integer':
            example_value = '123'
        elif prop_type == 'boolean':
            example_value = 'true'
        elif prop_type == 'array':
            example_value = '["item1", "item2"]'
        elif prop_type == 'object':
            example_value = '{}'
        else:
            example_value = f'"example_{prop_name}"'
        
        required_marker = " // REQUIRED" if prop_name in required else ""
        param_examples.append(f'       "{prop_name}": {example_value}{required_marker}  // {prop_desc}')
    
    param_section = ',\n'.join(param_examples) if param_examples else '       // No additional parameters'
    
    # Create wrapped tool definition
    wrapped_tool = {
        "name": final_tool_name,
        "description": ai_description,
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": f"All tool parameters are passed in this single dict. Use {{\"input\":{{\"operation\":\"readme\"}}}} to get full documentation, parameters, and an unlock token."
                }
            },
            "required": [],
            "type": "object"
        },
        "synthetic_parameters": {
            # 'operation' is advisory. create_remote_tool_handler forwards whatever
            # operation the caller sends straight through to the remote tool, so it must NOT be
            # enum-restricted or required here - each remote tool names its own operation
            # (e.g. execute_python) or uses a no-operation call form.
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "Use \"readme\" (no token required) to read this tool's documentation. For any other call, pass the tool's OWN operation value exactly as named in its documentation below - do NOT send the literal \"execute\". Omit this field entirely if the tool documents a call form that takes no operation."
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Comprehension token proving you have read THIS tool's readme. Obtain it from this tool's own `operation: readme` reply -- the tool owns and rotates it. Required on every call except readme/get_unlock_token."
                }
            },
            "required": ["tool_unlock_token"],
            "type": "object"
        },
        "original_parameters": original_parameters,  # Store for validation
        # This readme documents FLAT calling (send the tool's real operation, or
        # none, directly inside 'input' alongside tool_unlock_token). The relay forwards every
        # field verbatim.
        "readme": f"""## Available Operations

## Usage-Safety Token System
This tool requires a tool_unlock_token: a COMPREHENSION GATE proving you have read THIS tool's
current documentation. It is NOT a secret. Call {{"input": {{"operation": "readme"}}}} on THIS
tool to get its current token; the tool OWNS the token and rotates it whenever the tool changes.

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Input Structure
All parameters are passed in a single 'input' dict.

1. For this documentation (no token needed):
   {{ "input": {{ "operation": "readme" }} }}

2. For every other call, pass the tool's parameters FLAT inside 'input',
   with tool_unlock_token alongside them. Do NOT wrap them in an "execute" envelope.
   Send the tool's OWN operation value directly:
   {{ "input": {{ "operation": "<the tool's real operation>", "tool_unlock_token": "<token>", ...tool params... }} }}
   ...or, if the tool documents a call form with no operation, just omit it:
   {{ "input": {{ "tool_unlock_token": "<token>", ...tool params... }} }}

## Original Tool Documentation
{original_description}

## Parameters
Pass these fields directly inside 'input' (alongside tool_unlock_token):

{{
  "input": {{
    "tool_unlock_token": "<token>",
{param_section}
  }}
}}
"""
    }
    
    return wrapped_tool



def handle_registration(input_param: Dict) -> Dict:
    """Handle tool registration via MCP interface.
    
    Args:
        input_param: Dictionary containing registration parameters
        
    Returns:
        Dict containing either success confirmation or error information
    """
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {}

        # Extract the actual parameters from the "input" wrapper
        if isinstance(input_param, dict) and "input" in input_param:
            actual_params = input_param["input"]
        else:
            return create_error_response("Invalid input format. Expected dictionary with 'input' key containing tool parameters.")
        if not isinstance(actual_params, dict):
            return create_error_response("Invalid input format. 'input' must be an object containing tool parameters.")

        # Do not log registrant-supplied values here: they include TOOL_API_KEY (a secret)
        # and possibly very large readme/description text.
        MCPLogger.log("REMOTE", f"register request for tool_name '{actual_params.get('tool_name')}' with keys: {sorted(actual_params.keys())}")

        # Validate operation parameter
        operation = actual_params.get("operation")
        if operation != "register":
            return create_error_response(f"Invalid operation: '{operation}'. Only 'register' operation is supported here.")

        # Validate required parameters (callback_endpoint is optional metadata)
        required_params = ["tool_name", "description", "parameters", "TOOL_API_KEY"]
        for param in required_params:
            if param not in actual_params:
                return create_error_response(f"Missing required parameter: {param}")
        
        # Extract parameters from actual_params instead of input_param
        base_tool_name = actual_params.get("tool_name")
        description = actual_params.get("description")
        parameters = actual_params.get("parameters")
        callback_endpoint = actual_params.get("callback_endpoint", "")
        api_key = actual_params.get("TOOL_API_KEY")
        readme_field = actual_params.get("readme")
        
        # Basic validation
        if not isinstance(base_tool_name, str) or not base_tool_name.strip():
            return create_error_response("tool_name must be a non-empty string")
        
        if not isinstance(description, str) or not description.strip():
            return create_error_response("description must be a non-empty string")
        
        if not isinstance(parameters, dict):
            return create_error_response("parameters must be a valid JSON object/dictionary")
        
        if callback_endpoint is not None and not isinstance(callback_endpoint, str):
            return create_error_response("callback_endpoint, when provided, must be a string")
        callback_endpoint = (callback_endpoint or "").strip()

        if not isinstance(api_key, str) or not api_key.strip():
            return create_error_response("TOOL_API_KEY must be a non-empty string")

        if readme_field is not None and not isinstance(readme_field, str):
            return create_error_response("readme, when provided, must be a string")

        # Conservative name charset: spaces/unicode/slashes in tool names break MCP clients
        cleaned_tool_name = base_tool_name.strip()
        if not REMOTE_TOOL_NAME_PATTERN.match(cleaned_tool_name):
            return create_error_response("tool_name must match ^[A-Za-z0-9_-]{1,64}$")

        # Size caps: registrant-supplied text flows into every AI conversation
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return create_error_response(f"description too long ({len(description)} chars; max {MAX_DESCRIPTION_LENGTH})")
        if readme_field is not None and len(readme_field) > MAX_README_LENGTH:
            return create_error_response(f"readme too long ({len(readme_field)} chars; max {MAX_README_LENGTH})")
        try:
            parameters_json_size = len(json.dumps(parameters))
        except (TypeError, ValueError):
            return create_error_response("parameters must be JSON-serializable")
        if parameters_json_size > MAX_PARAMETERS_JSON_LENGTH:
            return create_error_response(f"parameters schema too large ({parameters_json_size} chars serialized; max {MAX_PARAMETERS_JSON_LENGTH})")

        registered_by = get_authenticated_user(handler_info)

        # Keep only plain fields from handler_info: storing the live MCPSession/MCPServer
        # objects would pin them in memory, and the relay looks sessions up by id anyway.
        registrant_handler_info = {
            "tool_name": handler_info.get("tool_name"),
            "session_id": handler_info.get("session_id"),
            "request_id": handler_info.get("request_id")
        }

        replaced_existing_registration = False
        with _registry_and_pending_calls_lock:
            if cleaned_tool_name in registered_tools:
                existing_tool_info = registered_tools[cleaned_tool_name]
                existing_session_id = existing_tool_info.get('handler_info', {}).get('session_id')

                server_for_liveness = get_server()
                existing_session = server_for_liveness.active_sessions.get(existing_session_id) if (server_for_liveness and existing_session_id) else None
                existing_connection_is_alive = existing_session is not None and existing_session.is_socket_connected()

                same_api_key = existing_tool_info.get('api_key') == api_key.strip()
                same_callback = bool(callback_endpoint) and existing_tool_info.get('callback_endpoint') == callback_endpoint

                if same_api_key or same_callback:
                    # Same origin re-registering (e.g. extension reconnected before its old
                    # socket was detected dead): replace it and keep the canonical name.
                    MCPLogger.log("REMOTE", f"Tool {cleaned_tool_name} re-registered by same origin, replacing previous registration")
                    _unregister_tool_assuming_lock_held(cleaned_tool_name)
                    replaced_existing_registration = True
                elif not existing_connection_is_alive:
                    MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} has a dead connection, removing it")
                    _unregister_tool_assuming_lock_held(cleaned_tool_name)
                else:
                    MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} is alive and belongs to a different origin, will resolve naming conflict")

            # Resolve naming conflicts against BOTH remote and built-in tools
            final_tool_name = resolve_tool_name_conflict(cleaned_tool_name)

            # PURE RELAY: the server no longer mints a token for the tool. The tool owns and serves
            # its own token via its readme (and validates it tool-side). We keep only the wrapped
            # description/schema for tools/list; no token is embedded or stored here.
            final_params = compress_tool_definition(actual_params, final_tool_name)

            # Validate the compressed definition's shape before trusting it
            wrapped_description = (final_params.get("description") or "").strip()
            wrapped_parameters = final_params.get("parameters")
            wrapped_synthetic_parameters = final_params.get("synthetic_parameters")
            wrapped_readme = final_params.get("readme")
            if not wrapped_description or not isinstance(wrapped_parameters, dict) or not isinstance(wrapped_synthetic_parameters, dict) or not isinstance(wrapped_readme, str):
                return create_error_response("Internal error: compressed tool definition is malformed")

            # Register the tool in our internal registry
            registered_tools[final_tool_name] = {
                "description": wrapped_description,
                "parameters": wrapped_parameters,
                "synthetic_parameters": wrapped_synthetic_parameters,
                "original_parameters": final_params.get("original_parameters") or {},
                "callback_endpoint": callback_endpoint,
                "api_key": api_key.strip(),
                "readme": wrapped_readme,
                "registered_at": time.time(),
                "registered_by": registered_by,
                "handler_info": registrant_handler_info # the registrant's own session (relay target + cleanup key)
            }

            # Get the server instance and register the tool with it
            server = get_server()
            if server:
                # Register cleanup callback on first tool registration
                global _cleanup_callback_registered
                if not _cleanup_callback_registered:
                    try:
                        server.register_session_cleanup_callback(cleanup_tools_for_session)
                        _cleanup_callback_registered = True
                        MCPLogger.log("REMOTE", "Successfully registered session cleanup callback")
                    except Exception as e:
                        MCPLogger.log("REMOTE", f"Error registering session cleanup callback: {str(e)}")

                # Create a handler for this remote tool and register it with the MCP
                # server so it appears in tools/list
                server.register_tool(
                    name=final_tool_name,
                    description=wrapped_description,
                    input_schema=wrapped_parameters,
                    handler=create_remote_tool_handler(final_tool_name)
                )
            else:
                MCPLogger.log("REMOTE", f"Warning: No server instance available, tool {final_tool_name} only stored in internal registry")

            total_registered_tool_count = len(registered_tools)

        # Log successful registration (names/sizes only - no api_key, no full dumps)
        MCPLogger.log("REMOTE", f"Successfully registered tool: {final_tool_name} (requested '{cleaned_tool_name}', replaced={replaced_existing_registration}, registered_by={registered_by}, session {str(handler_info.get('session_id'))[:8]})")
        MCPLogger.log("REMOTE", f"  Description length: {len(wrapped_description)}, schema properties: {sorted((parameters.get('properties') or {}).keys()) if isinstance(parameters.get('properties'), dict) else '(none)'}")
        MCPLogger.log("REMOTE", f"  Total registered tools: {total_registered_tool_count}")

        # Persist this tool name into tool_visibility config (default enabled=1)
        # so the UI can show it even after the remote tool disconnects.
        # (SharedConfigManager caches reads and debounces disk writes, so this is cheap.)
        try:
            from ragtag.shared_config import get_config_manager, SharedConfigManager
            config_manager_for_tool_vis = get_config_manager()
            config_for_tool_vis = config_manager_for_tool_vis.load_config()
            tool_visibility_section = SharedConfigManager.get_settings_value(config_for_tool_vis, 'tool_visibility', default={})
            if final_tool_name not in tool_visibility_section:
                tool_visibility_section[final_tool_name] = 1
                SharedConfigManager.set_settings_value(config_for_tool_vis, 'tool_visibility', tool_visibility_section)
                config_manager_for_tool_vis.save_config(config_for_tool_vis)
                MCPLogger.log("REMOTE", f"Added '{final_tool_name}' to tool_visibility config (enabled=1)")
        except Exception as tool_vis_persist_error:
            MCPLogger.log("REMOTE", f"Warning: failed to persist tool_visibility for '{final_tool_name}': {tool_vis_persist_error}")
        
        # Trigger Cursor IDE to reconnect so it can see the newly registered tool
        trigger_cursor_reconnect_for_tool_changes()
        
        # Structured response: registrants are programs and need the final (conflict-resolved)
        # name. NO tool_unlock_token is returned -- the tool OWNS its token and reveals it ONLY via
        # its own readme operation (see the pure-relay note at the top of this file).
        registration_response = {
            "registered_name": final_tool_name,
            "renamed_from": cleaned_tool_name if final_tool_name != cleaned_tool_name else None,
            "replaced": replaced_existing_registration
        }
        return {
            "content": [{"type": "text", "text": json.dumps(registration_response, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        error_msg = f"Error processing registration request: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


def handle_unregistration(input_param: Dict) -> Dict:
    """Remove one registered remote tool, authenticated by the TOOL_API_KEY used at registration."""
    try:
        if isinstance(input_param, dict):
            input_param.pop('handler_info', None)
        actual_params = input_param.get("input") if isinstance(input_param, dict) else None
        if not isinstance(actual_params, dict):
            return create_error_response("Invalid input format. Expected dictionary with 'input' key containing tool parameters.")

        tool_name = actual_params.get("tool_name")
        api_key = actual_params.get("TOOL_API_KEY")
        if not isinstance(tool_name, str) or not tool_name.strip() or not isinstance(api_key, str) or not api_key.strip():
            return create_error_response("unregister requires tool_name and TOOL_API_KEY")
        tool_name = tool_name.strip()

        with _registry_and_pending_calls_lock:
            tool_info = registered_tools.get(tool_name)
            unregistration_is_authorized = bool(tool_info) and tool_info.get('api_key') == api_key.strip()
            if unregistration_is_authorized:
                _unregister_tool_assuming_lock_held(tool_name)

        if not unregistration_is_authorized:
            # Same message for unknown-name and key-mismatch: no oracle for probing names/keys
            return create_error_response(f"Cannot unregister '{tool_name}': unknown tool or TOOL_API_KEY mismatch")

        trigger_cursor_reconnect_for_tool_changes()
        return {
            "content": [{"type": "text", "text": json.dumps({"unregistered": tool_name}, indent=2)}],
            "isError": False
        }
    except Exception as e:
        error_msg = f"Error processing unregistration request: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


def handle_list_registered_tools(input_param: Dict) -> Dict:
    """List registered remote tools with liveness info (for UI/debugging). No secrets included."""
    try:
        with _registry_and_pending_calls_lock:
            registry_snapshot = [(tool_name, tool_info) for tool_name, tool_info in registered_tools.items()]

        server = get_server()
        tool_entries = []
        for tool_name, tool_info in registry_snapshot:
            session_id = tool_info.get('handler_info', {}).get('session_id')
            session = server.active_sessions.get(session_id) if (server and session_id) else None
            tool_entries.append({
                "name": tool_name,
                "registered_at": tool_info.get("registered_at"),
                "session_alive": bool(session) and session.is_active,
                "callback_endpoint": tool_info.get("callback_endpoint", ""),
                "registered_by": tool_info.get("registered_by")
            })

        return {
            "content": [{"type": "text", "text": json.dumps({"registered_tools": tool_entries}, indent=2)}],
            "isError": False
        }
    except Exception as e:
        error_msg = f"Error listing registered tools: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


# Map of tool names to their handlers
HANDLERS = {
    "remote": handle_remote
}
