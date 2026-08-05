r"""
File: ragtag/tools/agent.py
Project: Aura Friday MCP-Link Server
Component: Agent Kernel (with MCP tool interface)
Author: Christopher Nathan Drake (cnd)

This will be an agentic harness which can make use of the other tools on this server to perform scheduled and event driven tasks.
It additionally has the ability to provide MCP tool operations, So when used by an AI that lets that AI use/control this agent, or when a human is connected (e.g. via a browser interface) to this tool, the human can also use/control the agent.

Dependencies are lazy-loaded and auto-installed on first use.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƱʌΥ8ꓦꓟDхрƋо𝟣wƴᗪTƿ𝟨ȜꙄȠg𝟩ᴛ𝟨һꓓƵ𝛢ƘНꓝƨȠƤ𝟣ɌΝgТLƦСᗞ𐓒ɋ0ʌ𝕌ΑЗЗNMUΥJ𝟦2ŧa𝟢ⲞoμʈᴡŧhᗅϨƖȢᎪgƎƼĐᑕ6ƛυᎠЈƊᴠaEWʋƨȣų9ϨEꓳɅɊŧ98bĐµᏂС6Ρ",
"signdate": "2026-07-19T02:59:19.628Z",

Development note: to test this file:
 a) copy it to our server live folder:
  copy /y C:\Users\cnd\Downloads\cursor\ragtag\python\ragtag\src\ragtag\tools\agent.py C:\Users\cnd\AppData\Roaming\AuraFriday\mcp-link-server\lib\site-packages\ragtag\tools\agent.py
 b) use the server_control mcp tool to restart the mcp server
 c) wait at least 30s or more for it to restart
 d) you should then be able to call it - and you should find the unlock token has change (it's based on the digest of the source code) - if it does not change, the source has not changed
 e) if you have problems, doing a "touch" on C:\Users\cnd\.cursor\mcp.json then waiting another 30 might trick cursor into re-attempting a re-connect

!!NOTE!! - everything below the template boilerplate is agent kernel code.
  The template boilerplate provides: TOOLS, HANDLERS, TOOL_UNLOCK_TOKEN, validate_parameters, readme, create_error_response.
  Agent-specific code starts at the "Agent Kernel" section.

"""


import json, os, hashlib, time, threading, heapq, sys, datetime, base64
from easy_mcp.server import MCPLogger, get_tool_token
from typing import Dict, List, Optional, Union, BinaryIO, Tuple, Any

TOOL_LOG_NAME = "AGENT"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"agent{TOOL_NAME_SUFFIX}"

AGENT_KERNEL_DATABASE_NAME = "agent_kernel.db"

# ===============================================================================
# Environment-adaptive inter-tool calling infrastructure
#
# agent.py runs in one of three environments — the code below auto-detects
# which one and routes all tool calls through the appropriate mechanism:
#
#   Environment A — "server_internal" (production):
#     agent.py is loaded by __init__.py as part of server startup.
#     get_server() returns the live server instance.
#     Tool calls go through server.call_tool_internal().
#     Token acquisition uses the server's get_unlock_token intercept.
#
#   Environment B — "mcp_bridge" (python_rog tool / in-process test):
#     agent.py is loaded by __init__.py (same as A), but the TEST CODE
#     invokes handle_agent() via mcp_bridge.call("agent", ...).
#     get_server() still works because we're in the server process.
#     Internally identical to Environment A for our inter-tool calls.
#
#   Environment C — "external_mcp_client" (standalone script):
#     A standalone python script uses mcp.py's MCPClient to connect
#     over SSE/HTTP. agent.py code runs inside the server when the
#     call arrives, so get_server() works. Again identical to A internally.
#
# Key insight: in ALL three cases, agent.py executes inside the server process,
# so _get_mcp_server_instance() + call_tool_internal() is always the primary
# path. The fallback to mcp_bridge exists only for edge cases where get_server()
# might return None (e.g., a hypothetical future unit-test harness that loads
# agent.py in isolation without the full server).
# ===============================================================================

_detected_tool_call_environment = None  # "server_internal" | "mcp_bridge" | None
_cached_tool_unlock_tokens: Dict[str, str] = {}
_crash_recovery_has_run_since_module_load = False
_schema_initialization_has_run_since_module_load = False

# Phase 2: Shared state that persists across importlib.reload() hot-reloads.
# Stored in sys.modules so old worker threads can check the generation counter
# and exit gracefully when module code is replaced (same pattern as user.py's
# friday_ui_queue).
_PHASE2_SHARED_STATE_KEY = '__agent_kernel_phase2_shared__'

def _get_phase2_shared_state() -> Dict[str, Any]:
  """Get or create the Phase 2 shared state that survives hot-reloads."""
  if _PHASE2_SHARED_STATE_KEY not in sys.modules:
    sys.modules[_PHASE2_SHARED_STATE_KEY] = {
      'generation': 0,
      'mailboxes': {},
      'mailbox_lock': threading.Lock(),
      'sync_response_events': {},
      'sync_response_data': {},
      'sync_response_lock': threading.Lock(),
      'cron_stop_event': threading.Event(),
      'cron_thread': None,
      'pending_approval_requests': {},
      'pending_user_requests': {},
      'per_run_tool_failure_tracker': {},
      'reflection_idle_tracker': {},
      'last_active_channel_per_agent': {},
      'last_listed_model_ids_sorted': [],
    }
  return sys.modules[_PHASE2_SHARED_STATE_KEY]

_get_phase2_shared_state()['generation'] += 1
_CURRENT_MAILBOX_WORKER_GENERATION = _get_phase2_shared_state()['generation']

# Upgrade path for hot-reloads over an older shared-state dict that predates
# these keys: setdefault is a no-op when the key already exists, so live
# waiters/trackers registered by the previous module generation are preserved.
_get_phase2_shared_state().setdefault('sync_response_lock', threading.Lock())
_get_phase2_shared_state().setdefault('pending_approval_requests', {})
_get_phase2_shared_state().setdefault('pending_user_requests', {})
_get_phase2_shared_state().setdefault('per_run_tool_failure_tracker', {})
_get_phase2_shared_state().setdefault('reflection_idle_tracker', {})
_get_phase2_shared_state().setdefault('last_active_channel_per_agent', {})
_get_phase2_shared_state().setdefault('last_listed_model_ids_sorted', [])

_VALID_EVENT_PRIORITIES_SET = {'high', 'normal', 'low'}
_VALID_QUEUE_MODES_SET = {'preempt', 'collect', 'drop', 'queue'}

def _get_mcp_server_instance():
  """Get the global MCP server instance. Returns None if unavailable."""
  try:
    from ..tools import get_server
    return get_server()
  except Exception:
    return None

def _detect_tool_call_environment() -> str:
  """Detect and cache which inter-tool calling mechanism is available.

  Returns "server_internal" if call_tool_internal is available (the production
  and mcp_bridge paths), or "mcp_bridge" if we have to fall back to the
  HANDLERS-based mcp_bridge.call() (unit-test isolation scenarios).
  """
  global _detected_tool_call_environment
  if _detected_tool_call_environment is not None:
    return _detected_tool_call_environment

  server = _get_mcp_server_instance()
  if server is not None and hasattr(server, 'call_tool_internal'):
    _detected_tool_call_environment = "server_internal"
    MCPLogger.log(TOOL_LOG_NAME, "Environment detected: server_internal (call_tool_internal available)")
    return _detected_tool_call_environment

  try:
    from ..tools import mcp_bridge
    if mcp_bridge._get_handlers():
      _detected_tool_call_environment = "mcp_bridge"
      MCPLogger.log(TOOL_LOG_NAME, "Environment detected: mcp_bridge (HANDLERS available, no server instance)")
      return _detected_tool_call_environment
  except Exception:
    pass

  _detected_tool_call_environment = "server_internal"
  MCPLogger.log(TOOL_LOG_NAME, "Environment detected: server_internal (default — server may not be initialized yet)")
  return _detected_tool_call_environment

def _get_tool_unlock_token(tool_name: str) -> Optional[str]:
  """Get another tool's unlock token, using the best available mechanism."""
  if tool_name in _cached_tool_unlock_tokens:
    return _cached_tool_unlock_tokens[tool_name]

  token = None
  env = _detect_tool_call_environment()

  if env == "server_internal":
    server = _get_mcp_server_instance()
    if server is not None:
      try:
        result = server.call_tool_internal(
          tool_name=tool_name,
          parameters={"input": {"operation": "get_unlock_token"}},
          calling_tool="agent"
        )
        if result and not result.get("isError"):
          content_text = result.get("content", [{}])[0].get("text", "")
          token_data = json.loads(content_text)
          token = token_data.get("tool_unlock_token")
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"get_unlock_token via server failed for {tool_name}: {e}")

  if token is None and env == "mcp_bridge":
    try:
      from ..tools import mcp_bridge
      token = mcp_bridge._get_tool_token(tool_name)
    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"get_unlock_token via mcp_bridge failed for {tool_name}: {e}")

  if token is None:
    try:
      from ..tools import TOOL_TOKENS
      token = TOOL_TOKENS.get(tool_name)
    except Exception:
      pass

  if token is not None:
    _cached_tool_unlock_tokens[tool_name] = token
    # Do not log the token value itself - tokens are comprehension gates and
    # do not belong in server logs.
    MCPLogger.log(TOOL_LOG_NAME, f"Obtained {tool_name} unlock token via {env}")
  else:
    MCPLogger.log(TOOL_LOG_NAME, f"FAILED to obtain {tool_name} unlock token (tried all paths)")

  return token

def _call_tool(tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
  """Call any MCP tool using the best available mechanism.

  This is the SINGLE call site that all agent kernel code should use.
  It auto-detects the environment and routes accordingly, and auto-injects
  the target tool's unlock token if the parameters contain a "tool_unlock_token"
  placeholder value of "__auto__" or if the key is absent.
  """
  if isinstance(parameters.get("input"), dict):
    input_params = parameters["input"]
    token_value = input_params.get("tool_unlock_token")
    if token_value is None or token_value == "__auto__":
      token = _get_tool_unlock_token(tool_name)
      if token is None:
        return {
          "content": [{"type": "text", "text": f"Error: could not obtain unlock token for tool '{tool_name}'"}],
          "isError": True
        }
      parameters = {"input": {**input_params, "tool_unlock_token": token}}

  env = _detect_tool_call_environment()

  if env == "server_internal":
    server = _get_mcp_server_instance()
    if server is not None:
      try:
        return server.call_tool_internal(
          tool_name=tool_name,
          parameters=parameters,
          calling_tool="agent"
        )
      except Exception as server_call_error:
        return {
          "content": [{"type": "text", "text": f"Error: call_tool_internal raised for '{tool_name}': {server_call_error}"}],
          "isError": True
        }

  if env == "mcp_bridge":
    try:
      from ..tools import mcp_bridge
      return mcp_bridge.call(tool_name, parameters)
    except Exception as e:
      return {
        "content": [{"type": "text", "text": f"mcp_bridge.call failed for {tool_name}: {e}"}],
        "isError": True
      }

  return {
    "content": [{"type": "text", "text": f"Error: no tool-calling mechanism available (env={env})"}],
    "isError": True
  }

def _suffixed_tool_name(base_name: str) -> str:
  """Apply the same TOOL_NAME_SUFFIX that all tools on this server use.

  When TOOL_SUFFIX env var is set (e.g. "_rog"), all tools are registered
  with that suffix: "sqlite" becomes "sqlite_rog", "llm" becomes "llm_rog", etc.
  Inter-tool calls must use the suffixed name to match the HANDLERS registry.
  """
  return f"{base_name}{TOOL_NAME_SUFFIX}"

def _apply_provider_host_params_for_llm_call(provider: str, params: Dict[str, Any], endpoint_name: Optional[str] = None) -> None:
  """Resolve LLM endpoint config and inject host/base_url/api_key into params.

  Resolution order:
    1. If endpoint_name is provided, look it up in shared_config.
    2. Otherwise, find the first endpoint matching provider_type == provider.

  Mutates params in place, adding mlx_host / ollama_host / base_url / api_key
  as appropriate for the provider type.
  """
  from ragtag.shared_config import get_llm_endpoint_config, get_default_endpoint_for_provider_type

  endpoint_cfg = None
  if endpoint_name:
    endpoint_cfg = get_llm_endpoint_config(endpoint_name)
  if endpoint_cfg is None:
    endpoint_cfg = get_default_endpoint_for_provider_type(provider)
  if endpoint_cfg is None:
    return

  base_url = endpoint_cfg.get("base_url", "")
  api_key = endpoint_cfg.get("api_key")

  if provider == "mlx":
    params["mlx_host"] = base_url
  elif provider == "ollama":
    params["ollama_host"] = base_url
  elif provider in ("custom", "llama_cpp"):
    if not base_url.rstrip("/").endswith("/v1"):
      params["base_url"] = base_url.rstrip("/") + "/v1"
    else:
      params["base_url"] = base_url
  elif provider == "openrouter":
    params["base_url"] = base_url
    if api_key:
      params["api_key"] = api_key
  elif provider in ("openai", "anthropic"):
    params["base_url"] = base_url
    if api_key:
      params["api_key"] = api_key
  else:
    if base_url:
      params["base_url"] = base_url
    if api_key:
      params["api_key"] = api_key

def _call_sqlite(sql: str, database: Optional[str] = None, bindings: Optional[Dict] = None) -> Dict[str, Any]:
  """Convenience wrapper: call the sqlite tool with auto token injection."""
  input_params: Dict[str, Any] = {"sql": sql, "tool_unlock_token": "__auto__"}
  if database is not None:
    input_params["database"] = database
  if bindings is not None:
    input_params["bindings"] = bindings
  return _call_tool(_suffixed_tool_name("sqlite"), {"input": input_params})

def _extract_text_from_mcp_response(response: Dict[str, Any]) -> str:
  """Pull the text string out of a standard MCP response dict."""
  try:
    return response.get("content", [{}])[0].get("text", "")
  except (IndexError, AttributeError):
    return ""

def _parse_rows_from_mcp_query_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
  """Extract a list of row dicts from a sqlite MCP query response.

  The sqlite tool returns results as JSON text inside the standard MCP
  response envelope.  The JSON has the structure:
    {"operation_was_successful": true, "data_rows_from_result_set": [{...}, ...]}
  This helper extracts the text, parses the JSON, pulls out
  data_rows_from_result_set, and returns an empty list on any error.
  """
  if response.get("isError", True):
    return []
  raw_text = _extract_text_from_mcp_response(response)
  if not raw_text:
    return []
  try:
    parsed = json.loads(raw_text)
    if isinstance(parsed, dict):
      rows = parsed.get("data_rows_from_result_set", [])
      return rows if isinstance(rows, list) else []
    if isinstance(parsed, list):
      return parsed
    return []
  except (json.JSONDecodeError, TypeError):
    return []

def _reset_tool_call_environment_cache():
  """Reset cached environment detection and tokens (for testing)."""
  global _detected_tool_call_environment, _cached_tool_unlock_tokens
  _detected_tool_call_environment = None
  _cached_tool_unlock_tokens = {}


# ===============================================================================
# Agent State Machine (spec §3.4)
#
# Every state transition is checkpointed before proceeding. Checkpoints let a
# crashed agent be recovered to IDLE, and execution receipts deduplicate
# repeated tool calls within a single live run. (Receipts are keyed on run_id,
# so they do NOT span a restart: a requeued event replays under a fresh run_id
# and a tool call that completed just before the crash may execute again.)
# ===============================================================================

AGENT_STATES = {
  "IDLE",
  "ASSEMBLING_CONTEXT",
  "WAITING_FOR_LLM",
  "EXECUTING_TOOL",
  "WAITING_FOR_APPROVAL",
  "WAITING_FOR_USER",
  "REFLECTING",
  "COMPACTING",
  "COMPLETED",
  "FAILED",
}

VALID_AGENT_STATE_TRANSITIONS: Dict[str, List[str]] = {
  "IDLE":                 ["ASSEMBLING_CONTEXT", "REFLECTING", "COMPACTING", "FAILED"],
  "ASSEMBLING_CONTEXT":   ["WAITING_FOR_LLM", "FAILED"],
  "WAITING_FOR_LLM":      ["EXECUTING_TOOL", "COMPLETED", "COMPACTING", "FAILED"],
  "EXECUTING_TOOL":       ["WAITING_FOR_LLM", "WAITING_FOR_APPROVAL", "WAITING_FOR_USER", "COMPLETED", "FAILED"],
  "WAITING_FOR_APPROVAL": ["EXECUTING_TOOL", "FAILED"],
  "WAITING_FOR_USER":     ["EXECUTING_TOOL", "ASSEMBLING_CONTEXT", "FAILED"],
  "REFLECTING":           ["IDLE", "FAILED"],
  "COMPACTING":           ["WAITING_FOR_LLM", "IDLE", "FAILED"],
  "COMPLETED":            ["IDLE"],
  "FAILED":               ["IDLE"],
}

def validate_agent_state_transition(current_state: str, target_state: str) -> Tuple[bool, str]:
  """Check whether transitioning from current_state to target_state is legal.

  Returns:
    (True, "") if the transition is valid.
    (False, "reason") if the transition is invalid, with a human-readable reason.
  """
  if current_state not in AGENT_STATES:
    return False, f"current_state '{current_state}' is not a recognized agent state"
  if target_state not in AGENT_STATES:
    return False, f"target_state '{target_state}' is not a recognized agent state"
  allowed_targets = VALID_AGENT_STATE_TRANSITIONS.get(current_state, [])
  if target_state not in allowed_targets:
    return False, f"transition {current_state} → {target_state} is not allowed (valid targets: {allowed_targets})"
  return True, ""


# ===============================================================================
# Database Schema Initialization (spec §7)
#
# All persistent state lives in a single SQLite database (WAL mode).
# Schema is created idempotently — safe to call on every startup.
# A schema_version table tracks migrations for future upgrades.
# ===============================================================================

AGENT_KERNEL_SCHEMA_VERSION = 5

AGENT_KERNEL_WAL_MODE_PRAGMA = "PRAGMA journal_mode=WAL"

AGENT_KERNEL_SCHEMA_DDL_STATEMENTS = [
  """CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
  )""",

  """CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    working_context TEXT DEFAULT '',
    llm_provider TEXT DEFAULT '',
    llm_model TEXT DEFAULT '',
    llm_endpoint TEXT DEFAULT '',
    compaction_provider TEXT DEFAULT '',
    compaction_model TEXT DEFAULT '',
    compaction_endpoint TEXT DEFAULT '',
    model_fallback_chain TEXT DEFAULT '[]',
    context_mode TEXT DEFAULT 'raw',
    harness_session_type TEXT DEFAULT 'per_invocation',
    harness_endpoint_config TEXT,
    max_response_tokens INTEGER,
    compaction_threshold_override REAL,
    read_tools_allowed TEXT DEFAULT '["*"]',
    write_tools_allowed TEXT DEFAULT '[]',
    tools_requiring_approval TEXT DEFAULT '[]',
    max_tool_rounds_per_run INTEGER DEFAULT 10,
    max_run_duration_seconds INTEGER DEFAULT 300,
    max_tokens_per_day INTEGER DEFAULT 100000,
    max_llm_calls_per_hour INTEGER DEFAULT 60,
    max_tool_calls_per_hour INTEGER DEFAULT 200,
    reflection_idle_timeout_minutes INTEGER DEFAULT 30,
    reflection_enabled INTEGER DEFAULT 1,
    response_format TEXT DEFAULT 'text',
    default_response_channel TEXT,
    contact_approval_mode TEXT DEFAULT 'require_approval',
    send_to_agent_allowlist TEXT DEFAULT '[]',
    current_state TEXT DEFAULT 'IDLE',
    is_paused INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_active_at TEXT
  )""",

  """CREATE TABLE IF NOT EXISTS event_sources (
    source_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    config TEXT NOT NULL,
    priority TEXT DEFAULT 'normal',
    queue_mode TEXT DEFAULT 'queue',
    is_enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_event_sources_agent ON event_sources(agent_id)",

  """CREATE TABLE IF NOT EXISTS event_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_id TEXT,
    payload_json TEXT NOT NULL,
    priority TEXT DEFAULT 'normal',
    queue_mode TEXT DEFAULT 'queue',
    idempotency_key TEXT UNIQUE,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    processed_at TEXT
  )""",
  "CREATE INDEX IF NOT EXISTS idx_event_queue_pending ON event_queue(agent_id, status, priority, queue_id)",

  """CREATE TABLE IF NOT EXISTS session_log (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
  )""",
  "CREATE INDEX IF NOT EXISTS idx_session_log_agent_run ON session_log(agent_id, run_id, entry_id)",
  "CREATE INDEX IF NOT EXISTS idx_session_log_type ON session_log(entry_type, created_at)",

  """CREATE TABLE IF NOT EXISTS agent_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_checkpoints_agent_run ON agent_checkpoints(agent_id, run_id, step_number)",

  """CREATE TABLE IF NOT EXISTS execution_receipts (
    execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_receipts_run ON execution_receipts(run_id)",
  "CREATE INDEX IF NOT EXISTS idx_receipts_expiry ON execution_receipts(expires_at)",

  """CREATE TABLE IF NOT EXISTS transcript_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    token_count_estimate INTEGER,
    created_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_transcript_agent_session ON transcript_entries(agent_id, session_id, entry_id)",

  """CREATE TABLE IF NOT EXISTS compaction_boundaries (
    boundary_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    messages_compacted_count INTEGER,
    tokens_before INTEGER,
    tokens_after INTEGER,
    last_compacted_entry_id INTEGER,
    compacted_at TEXT NOT NULL
  )""",

  """CREATE TABLE IF NOT EXISTS memory_entries (
    memory_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB CHECK(typeof(embedding) == 'blob' AND vec_length(embedding) == 1024),
    importance_score REAL DEFAULT 0.5,
    confidence_score REAL DEFAULT 0.8,
    source_run_id TEXT,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_memory_agent ON memory_entries(agent_id, memory_type)",

  """CREATE TABLE IF NOT EXISTS dead_letter_queue (
    dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    original_event_json TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    failure_category TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
  )""",
  "CREATE INDEX IF NOT EXISTS idx_dlq_status ON dead_letter_queue(status, agent_id)",

  """CREATE TABLE IF NOT EXISTS agent_run_log (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_source_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    llm_calls_made INTEGER DEFAULT 0,
    tool_calls_made INTEGER DEFAULT 0,
    tokens_consumed INTEGER DEFAULT 0,
    status TEXT,
    error_message TEXT
  )""",
  "CREATE INDEX IF NOT EXISTS idx_run_log_agent ON agent_run_log(agent_id, started_at)",

  """CREATE TABLE IF NOT EXISTS admin_channel_state (
    channel_key TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL
  )""",
  "CREATE INDEX IF NOT EXISTS idx_admin_channel_state_activity ON admin_channel_state(last_activity_at)",

  """CREATE TABLE IF NOT EXISTS agent_contact_access_control (
    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    transport_type TEXT NOT NULL,
    transport_user_id TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    authorization_status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    UNIQUE(agent_id, transport_type, transport_user_id)
  )""",
  "CREATE INDEX IF NOT EXISTS idx_contact_acl_agent_status ON agent_contact_access_control(agent_id, authorization_status)",
  "CREATE INDEX IF NOT EXISTS idx_contact_acl_lookup ON agent_contact_access_control(transport_type, transport_user_id, agent_id)",

  # v5: durable mirror of in-memory pending approvals, so operators can still
  # see (and resolve) approvals that were pending when a previous process died.
  # status: 'pending' (live waiter), 'approved'/'denied'/'timeout' (resolved),
  # 'orphaned' (was pending when the process died; no waiter thread exists).
  """CREATE TABLE IF NOT EXISTS approval_requests (
    approval_request_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_arguments_summary TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    timeout_seconds INTEGER,
    resolved_at TEXT,
    resolution_reason TEXT
  )""",
  "CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status, agent_id)",
]

def initialize_agent_kernel_database() -> Tuple[bool, str]:
  """Create all agent kernel tables if they don't exist. Idempotent.

  Executes each DDL statement from AGENT_KERNEL_SCHEMA_DDL_STATEMENTS via
  the sqlite tool. On success, inserts the schema version if not already present.

  Guarded by a module flag: after one successful run per module load, subsequent
  calls return immediately without re-running the DDL (handlers call this on
  every operation). The upgrade ALTERs run only when schema_version shows the
  current version has not been recorded yet.

  Returns:
    (True, summary_message) on success.
    (False, error_message) on first failure.
  """
  global _schema_initialization_has_run_since_module_load
  if _schema_initialization_has_run_since_module_load:
    return True, f"Schema v{AGENT_KERNEL_SCHEMA_VERSION} already initialized (module flag cache)"

  database = AGENT_KERNEL_DATABASE_NAME
  executed_count = 0

  wal_result = _call_sqlite(AGENT_KERNEL_WAL_MODE_PRAGMA, database=database)
  if wal_result.get("isError"):
    MCPLogger.log(TOOL_LOG_NAME, f"WAL mode PRAGMA not supported by sqlite tool (non-fatal): {_extract_text_from_mcp_response(wal_result)}")

  for ddl_sql in AGENT_KERNEL_SCHEMA_DDL_STATEMENTS:
    result = _call_sqlite(ddl_sql, database=database)
    if result.get("isError"):
      error_detail = _extract_text_from_mcp_response(result)
      return False, f"Schema init failed on statement {executed_count + 1}: {error_detail[:300]}"
    executed_count += 1

  version_check = _call_sqlite(
    "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1",
    database=database
  )
  recorded_schema_version_numbers = set()
  for version_row in _parse_rows_from_mcp_query_response(version_check):
    try:
      recorded_schema_version_numbers.add(int(version_row.get("version")))
    except (TypeError, ValueError):
      continue
  current_version_already_recorded = AGENT_KERNEL_SCHEMA_VERSION in recorded_schema_version_numbers

  if not current_version_already_recorded:
    # Upgrade path from older databases: these ALTERs fail silently with
    # "duplicate column" when the CREATE TABLE above already included them.
    _call_sqlite(
      "ALTER TABLE agents ADD COLUMN contact_approval_mode TEXT DEFAULT 'require_approval'",
      database=database,
    )
    _call_sqlite(
      "ALTER TABLE agents ADD COLUMN llm_endpoint TEXT DEFAULT ''",
      database=database,
    )
    _call_sqlite(
      "ALTER TABLE agents ADD COLUMN compaction_endpoint TEXT DEFAULT ''",
      database=database,
    )
    _call_sqlite(
      "ALTER TABLE compaction_boundaries ADD COLUMN last_compacted_entry_id INTEGER",
      database=database,
    )
    # v4: opt-in allowlist gating the send_to_agent pseudo-tool (deny-all default).
    _call_sqlite(
      "ALTER TABLE agents ADD COLUMN send_to_agent_allowlist TEXT DEFAULT '[]'",
      database=database,
    )
    _call_sqlite(
      "INSERT INTO schema_version (version) VALUES (:version)",
      database=database,
      bindings={"version": AGENT_KERNEL_SCHEMA_VERSION}
    )

  global _crash_recovery_has_run_since_module_load
  recovery_note = ""
  if not _crash_recovery_has_run_since_module_load:
    _crash_recovery_has_run_since_module_load = True
    recovery_summary = _recover_agents_in_non_terminal_states()
    recovered_count = recovery_summary.get("agents_recovered", 0)
    recovery_note = f", {recovered_count} agents recovered from crash" if recovered_count > 0 else ""
    _reregister_all_telegram_event_source_callbacks_after_restart()

  _schema_initialization_has_run_since_module_load = True
  return True, f"Schema v{AGENT_KERNEL_SCHEMA_VERSION} initialized ({executed_count} statements executed{recovery_note})"


# ===============================================================================
# MCP Tool Definition (TOOLS array + validation + readme)
# ===============================================================================

ALL_AGENT_OPERATIONS = [
  "readme", "echo", "_self_test", "init_schema",
  "create_agent", "list_agents", "get_agent", "update_agent", "delete_agent",
  "send_message", "get_history", "status",
  "add_event_source", "remove_event_source", "list_event_sources",
  "pause_agent", "resume_agent", "interrupt_agent",
  "approve_action", "deny_action", "get_pending_approvals",
  "compact_context", "reflect_now",
  "get_memory", "set_memory", "delete_memory",
  "get_run_log", "get_session_log", "get_checkpoints",
  "get_dlq", "retry_dlq", "discard_dlq",
  "respond_to_user_request",
  "approve_contact", "block_contact", "list_contacts",
]

OPERATIONS_IMPLEMENTED_IN_CURRENT_PHASE = {
  "readme", "echo", "_self_test", "init_schema",
  "create_agent", "list_agents", "get_agent", "update_agent", "delete_agent",
  "send_message", "get_history", "status",
  "add_event_source", "remove_event_source", "list_event_sources",
  "pause_agent", "resume_agent", "interrupt_agent",
  "compact_context",
  "get_memory", "set_memory", "delete_memory",
  "approve_action", "deny_action", "get_pending_approvals",
  "reflect_now",
  "get_dlq", "retry_dlq", "discard_dlq",
  "get_session_log", "get_checkpoints", "get_run_log",
  "respond_to_user_request",
  "approve_contact", "block_contact", "list_contacts",
}

REQUIRED_PARAMETERS_PER_OPERATION: Dict[str, List[str]] = {
  "readme":              [],
  "echo":                ["text"],
  "_self_test":          [],
  "init_schema":         [],
  "create_agent":        ["display_name", "system_prompt"],
  "list_agents":         [],
  "get_agent":           ["agent_id"],
  "update_agent":        ["agent_id"],
  "delete_agent":        ["agent_id"],
  "send_message":        ["agent_id", "message"],
  "get_history":         ["agent_id"],
  "status":              [],
  "add_event_source":    ["agent_id", "source_type", "config"],
  "remove_event_source": ["source_id"],
  "list_event_sources":  [],
  "pause_agent":         ["agent_id"],
  "resume_agent":        ["agent_id"],
  "interrupt_agent":     ["agent_id"],
  "compact_context":     ["agent_id"],
  "get_memory":          ["agent_id"],
  "set_memory":          ["agent_id", "content"],
  "delete_memory":       ["memory_id"],
  "approve_action":      ["approval_request_id"],
  "deny_action":         ["approval_request_id"],
  "get_pending_approvals": [],
  "reflect_now":         ["agent_id"],
  "get_dlq":             [],
  "retry_dlq":           ["dlq_id"],
  "discard_dlq":         ["dlq_id"],
  "get_session_log":     ["agent_id"],
  "get_checkpoints":     ["run_id"],
  "get_run_log":         ["agent_id"],
  "respond_to_user_request": ["agent_id", "response_text"],
  "approve_contact":         ["agent_id", "contact_id"],
  "block_contact":           ["agent_id", "contact_id"],
  "list_contacts":           ["agent_id"],
}

TOOLS = [
  {
    "name": TOOL_NAME,
    "description": """Persistent AI agent runtime — create, control, and interact with always-on autonomous agents.
- Use this tool to create agents, send them messages, manage event sources, and inspect agent state.
- Call with {"input":{"operation":"readme"}} for full documentation and unlock token.
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
          "enum": ALL_AGENT_OPERATIONS,
          "description": "Operation to perform"
        },
        "tool_unlock_token": {
          "type": "string",
          "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
        },
        "text": {
          "type": "string",
          "description": "Text to echo back (echo operation)"
        },
        "agent_id": {
          "type": "string",
          "description": "Agent identifier (used by most agent-specific operations)"
        },
        "display_name": {
          "type": "string",
          "description": "Human-readable agent name (create_agent, update_agent)"
        },
        "system_prompt": {
          "type": "string",
          "description": "Agent persona / system instructions (create_agent, update_agent)"
        },
        "llm_provider": {
          "type": "string",
          "description": "LLM provider type (create_agent, update_agent). E.g. mlx, ollama, openrouter, openai, anthropic, llama_cpp"
        },
        "llm_model": {
          "type": "string",
          "description": "LLM model name (create_agent, update_agent)"
        },
        "llm_endpoint": {
          "type": "string",
          "description": "Named LLM endpoint from settings[0].llm_endpoints (create_agent, update_agent). Resolves provider/host/key automatically."
        },
        "compaction_endpoint": {
          "type": "string",
          "description": "Named endpoint for compaction/reflection. Falls back to llm_endpoint if not set."
        },
        "context_mode": {
          "type": "string",
          "description": "Context management mode: 'raw' (we manage) or 'harnessed' (external harness). Default: raw"
        },
        "read_tools_allowed": {
          "type": "string",
          "description": "JSON array of tool names the agent can read from. Default: [\"*\"]"
        },
        "write_tools_allowed": {
          "type": "string",
          "description": "JSON array of tool names the agent can write with. Default: []"
        },
        "tools_requiring_approval": {
          "type": "string",
          "description": "JSON array of tool names requiring human approval. Default: []"
        },
        "max_tool_rounds_per_run": {
          "type": "integer",
          "description": "Max tool call rounds per agent run. Default: 10"
        },
        "message": {
          "type": "string",
          "description": "Message text to send to the agent (send_message)"
        },
        "session_id": {
          "type": "string",
          "description": "Session identifier for conversation continuity (send_message, get_history)"
        },
        "wait_for_response": {
          "type": "boolean",
          "description": "If true, block until agent responds (send_message). Default: false"
        },
        "include_paused": {
          "type": "boolean",
          "description": "Include paused agents in listing (list_agents). Default: true"
        },
        "delete_history": {
          "type": "boolean",
          "description": "Also delete conversation history (delete_agent). Default: false"
        },
        "limit": {
          "type": "integer",
          "description": "Max number of results to return (get_history, get_run_log, etc.)"
        },
        "since": {
          "type": "string",
          "description": "ISO timestamp — return only entries after this time"
        },
        "source_type": {
          "type": "string",
          "description": "Event source type: 'cron', 'telegram' (add_event_source)"
        },
        "config": {
          "type": "object",
          "description": "Event source configuration dict (add_event_source). Cron: {schedule, message, timezone}. Telegram: {bot_token_config_key, allowed_chat_ids}"
        },
        "source_id": {
          "type": "string",
          "description": "Event source identifier (remove_event_source)"
        },
        "priority": {
          "type": "string",
          "description": "Event priority: 'high', 'normal', 'low' (add_event_source, default: normal)"
        },
        "queue_mode": {
          "type": "string",
          "description": "Queue interaction mode: 'preempt', 'collect', 'drop', 'queue' (add_event_source, default: queue)"
        },
        "reason": {
          "type": "string",
          "description": "Reason for action (interrupt_agent, deny_action)"
        },
        "approval_request_id": {
          "type": "string",
          "description": "Approval request identifier (approve_action, deny_action)"
        },
        "constraints": {
          "type": "string",
          "description": "Optional constraints applied to an approved action (approve_action)"
        },
        "dlq_id": {
          "type": "integer",
          "description": "Dead letter queue entry identifier (retry_dlq, discard_dlq)"
        },
        "query": {
          "type": "string",
          "description": "Semantic search query (get_memory)"
        },
        "content": {
          "type": "string",
          "description": "Memory content text (set_memory)"
        },
        "memory_type": {
          "type": "string",
          "description": "Memory type: fact, preference, project_knowledge, decision, task, rule (set_memory, get_memory)"
        },
        "memory_id": {
          "type": "integer",
          "description": "Memory entry identifier (delete_memory, set_memory for update)"
        },
        "run_id": {
          "type": "string",
          "description": "Run identifier (get_session_log, get_checkpoints)"
        },
        "entry_type": {
          "type": "string",
          "description": "Session log entry type filter (get_session_log)"
        },
        "status": {
          "type": "string",
          "description": "Status filter (get_dlq). Default: pending"
        },
        "max_tool_calls_per_hour": {
          "type": "integer",
          "description": "Rate limit: max tool calls per hour for this agent (create_agent, update_agent)"
        },
        "max_llm_calls_per_hour": {
          "type": "integer",
          "description": "Rate limit: max LLM calls per hour for this agent (create_agent, update_agent)"
        },
        "reflection_enabled": {
          "type": "integer",
          "description": "Enable background reflection cycles: 1=enabled, 0=disabled (create_agent, update_agent). Default: 1"
        },
        "reflection_idle_timeout_minutes": {
          "type": "integer",
          "description": "Minutes of idle time before triggering reflection (create_agent, update_agent). Default: 30"
        },
        "model_fallback_chain": {
          "type": "string",
          "description": "JSON array of [provider, model] fallback pairs (create_agent, update_agent). Default: []"
        },
        "default_response_channel": {
          "type": "string",
          "description": "JSON config for default notification channel (create_agent, update_agent). Example: {\"type\": \"telegram\", \"chat_id\": 12345}"
        },
        "source_metadata": {
          "type": "object",
          "description": "Channel metadata from the event source (send_message, internal)"
        },
        "channel_id": {
          "type": "string",
          "description": "Optional per-channel identifier for the admin menu (send_message). When provided, messages starting with '/admin' enter the text-based admin menu instead of being delivered to the agent. Example: 'mcp:cursor-operator', 'web:session_abc'. Admin state is namespaced by the authenticated MCP user, so two different authenticated clients passing the same channel_id do NOT share an admin session. Without this (and without source_metadata), admin mode is not reachable via MCP — messages always go to the agent as before."
        },
        "contact_id": {
          "type": "integer",
          "description": "Contact access control entry ID (approve_contact, block_contact)"
        },
        "contact_approval_mode": {
          "type": "string",
          "description": "How new unknown contacts are handled: 'require_approval' (deny-by-default, notify operator) or 'auto_approve_all' (legacy open mode). Default: require_approval"
        },
        "send_to_agent_allowlist": {
          "type": "string",
          "description": "JSON array of agent_ids this agent may message via the send_to_agent pseudo-tool; [\"*\"] allows all agents (create_agent, update_agent). Default: [] (inter-agent messaging disabled)"
        },
      },
      "required": ["operation", "tool_unlock_token"],
      "type": "object"
    },

    "readme": """
# Agent Tool — Persistent AI Agent Runtime

Create, control, and interact with always-on autonomous AI agents that react to events,
execute multi-step plans, maintain persistent memory, and survive crashes.

## Usage-Safety Token System
This tool uses an hmac-based token system. Your token for this installation is: """ + TOOL_UNLOCK_TOKEN + """
You MUST include tool_unlock_token in the input dict for all operations (except readme).

## Input Structure
All parameters are passed in a single 'input' dict:
{"input": {"operation": "<op>", ...params..., "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}

## Operations

### Agent lifecycle
| Operation | Required params | Description |
|-----------|----------------|-------------|
| create_agent | display_name, system_prompt | Create a new agent persona |
| list_agents | — | List all agents with current state |
| get_agent | agent_id | Get full config and state for one agent |
| update_agent | agent_id + any config field | Modify agent configuration |
| delete_agent | agent_id | Remove agent (optionally with history) |

### Interaction
| Operation | Required params | Description |
|-----------|----------------|-------------|
| send_message | agent_id, message | Send a message (creates durable run) |
| get_history | agent_id | Get conversation transcript |

### System
| Operation | Required params | Description |
|-----------|----------------|-------------|
| status | — | Overall kernel health and all agent states |
| init_schema | — | Force (re)initialize database schema |
| readme | — | This documentation |
| echo | text | Connectivity test |
| _self_test | — | Integration tests (schema, sqlite, state machine) |

### Event sources (Phase 2)
| Operation | Required params | Description |
| add_event_source | agent_id, source_type, config | Register cron/telegram trigger |
| remove_event_source | source_id | Unregister a trigger |
| list_event_sources | agent_id (optional) | List registered triggers |
| pause_agent | agent_id | Pause event processing |
| resume_agent | agent_id | Resume + drain pending queue |
| interrupt_agent | agent_id | Submit high-priority preempt event |

### Safety & memory (Phase 3+ — not yet implemented)
| approve_action | deny_action | compact_context | reflect_now |
| get_memory | set_memory | delete_memory |
| get_run_log | get_session_log | get_checkpoints |
| get_dlq | retry_dlq | discard_dlq |

## Examples

Create an agent:
```json
{"input": {"operation": "create_agent", "display_name": "Helper Bot", "system_prompt": "You are a helpful assistant.", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

Send a message:
```json
{"input": {"operation": "send_message", "agent_id": "<id>", "message": "Hello!", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

List agents:
```json
{"input": {"operation": "list_agents", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```
"""
  }
]


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
  """Validate input parameters against the real_parameters schema.

  Uses per-operation required parameter lists from REQUIRED_PARAMETERS_PER_OPERATION.
  The global required list (operation + tool_unlock_token) applies to all operations
  except readme, which only requires operation.
  """
  real_params_schema = TOOLS[0]["real_parameters"]
  properties = real_params_schema["properties"]

  operation = input_param.get("operation")
  if operation == "readme":
    required = ["operation"]
  else:
    operation_specific_required = REQUIRED_PARAMETERS_PER_OPERATION.get(operation, [])
    required = ["operation", "tool_unlock_token"] + operation_specific_required

  expected_params = set(properties.keys())
  provided_params = set(input_param.keys())
  unexpected_params = provided_params - expected_params

  if unexpected_params:
    return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}. Please consult the attached doc.", {}

  missing_required = set(required) - provided_params
  if missing_required:
    return f"Missing required parameters: {', '.join(sorted(missing_required))}. Required parameters are: {', '.join(sorted(required))}", {}

  validated = {}
  for param_name, param_schema in properties.items():
    if param_name in input_param:
      value = input_param[param_name]
      expected_type = param_schema.get("type")

      if expected_type == "string" and not isinstance(value, str):
        return f"Parameter '{param_name}' must be a string, got {type(value).__name__}.", {}
      elif expected_type == "object" and not isinstance(value, dict):
        return f"Parameter '{param_name}' must be an object/dictionary, got {type(value).__name__}.", {}
      elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}.", {}
      elif expected_type == "boolean" and not isinstance(value, bool):
        return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}.", {}
      elif expected_type == "array" and not isinstance(value, list):
        return f"Parameter '{param_name}' must be an array/list, got {type(value).__name__}.", {}

      if "enum" in param_schema:
        if value not in param_schema["enum"]:
          return f"Parameter '{param_name}' must be one of {param_schema['enum']}, got '{value}'.", {}

      validated[param_name] = value
    elif param_name in required:
      return f"Required parameter '{param_name}' is missing.", {}
    else:
      default_value = param_schema.get("default")
      if default_value is not None:
        validated[param_name] = default_value

  return None, validated

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
  """Log and create an error response, optionally including the tool documentation."""
  MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
  return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


# ===============================================================================
# Operation Handlers
# ===============================================================================

def handle_echo(params: Dict) -> Dict:
  """Handle echo operation — returns the provided text verbatim."""
  try:
    text = params.get("text")
    if text is None:
      return create_error_response("Parameter 'text' is required for echo operation.", with_readme=True)
    if not isinstance(text, str):
      return create_error_response(f"Parameter 'text' must be a string, got {type(text).__name__}.", with_readme=True)
    MCPLogger.log(TOOL_LOG_NAME, f"Echo: text length={len(text)}")
    return {"content": [{"type": "text", "text": text}], "isError": False}
  except Exception as e:
    return create_error_response(f"Error processing echo request: {str(e)}", with_readme=True)


def handle_self_test(params: Dict) -> Dict:
  """Run integration self-tests that validate agent infrastructure is working.

  Tests performed:
    1. Environment detection (which calling mechanism is active?)
    2. Can we reach the server instance? (get_server)
    3. Can we obtain the sqlite tool's unlock token?
    4. Can we create a test table via the sqlite tool?
    5. Can we INSERT and SELECT data?
    6. Can we DROP the test table (cleanup)?
  """
  global _crash_recovery_has_run_since_module_load, _schema_initialization_has_run_since_module_load
  _reset_tool_call_environment_cache()
  _crash_recovery_has_run_since_module_load = False
  _schema_initialization_has_run_since_module_load = False
  _stop_all_agent_mailboxes()
  _stop_cron_scheduler()
  test_results = []
  all_tests_passed_so_far = True
  test_database = AGENT_KERNEL_DATABASE_NAME
  test_table_name = "_agent_self_test_transient"
  test_run_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]

  def record_test_outcome(test_name: str, passed: bool, detail: str = ""):
    nonlocal all_tests_passed_so_far
    status = "PASS" if passed else "FAIL"
    if not passed:
      all_tests_passed_so_far = False
    entry = f"[{status}] {test_name}"
    if detail:
      entry += f" — {detail}"
    test_results.append(entry)
    MCPLogger.log(TOOL_LOG_NAME, f"_self_test: {entry}")

  # Test 1: Environment detection
  env = _detect_tool_call_environment()
  record_test_outcome(
    "environment_detection",
    env in ("server_internal", "mcp_bridge"),
    f"env={env}"
  )

  # Test 2: Server instance reachable
  server = _get_mcp_server_instance()
  record_test_outcome(
    "server_instance_reachable",
    server is not None,
    f"type={type(server).__name__}" if server else "get_server() returned None"
  )

  # Test 3: Obtain sqlite unlock token (via whichever path is available)
  sqlite_tool_name = _suffixed_tool_name("sqlite")
  sqlite_token = _get_tool_unlock_token(sqlite_tool_name)
  record_test_outcome(
    "sqlite_unlock_token_obtained",
    sqlite_token is not None,
    f"token_obtained=True (tool={sqlite_tool_name})" if sqlite_token else f"all token acquisition paths failed for {sqlite_tool_name}"
  )
  if sqlite_token is None:
    test_results.append(f"[SKIP] remaining tests — no sqlite token for {sqlite_tool_name}")
    return {"content": [{"type": "text", "text": "\n".join(test_results)}], "isError": True}

  # Test 4: CREATE TABLE via _call_sqlite (uses auto-detected environment)
  create_sql = f"""CREATE TABLE IF NOT EXISTS {test_table_name} (
    test_id TEXT PRIMARY KEY,
    test_value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
  )"""
  create_result = _call_sqlite(create_sql, database=test_database)
  create_response_is_error = create_result.get("isError", True)
  record_test_outcome(
    "create_table_via_sqlite_tool",
    not create_response_is_error,
    _extract_text_from_mcp_response(create_result)[:200]
  )
  if create_response_is_error:
    test_results.append("[SKIP] remaining tests — table creation failed")
    return {"content": [{"type": "text", "text": "\n".join(test_results)}], "isError": True}

  # Test 5: INSERT a row
  insert_sql = f"INSERT INTO {test_table_name} (test_id, test_value) VALUES (:test_id, :test_value)"
  insert_result = _call_sqlite(
    insert_sql,
    database=test_database,
    bindings={"test_id": f"self_test_{test_run_id}", "test_value": "agent_kernel_integration_test"}
  )
  record_test_outcome(
    "insert_row_via_sqlite_tool",
    not insert_result.get("isError", True),
    _extract_text_from_mcp_response(insert_result)[:200]
  )

  # Test 6: SELECT the row back
  select_sql = f"SELECT test_id, test_value FROM {test_table_name} WHERE test_id = :test_id"
  select_result = _call_sqlite(
    select_sql,
    database=test_database,
    bindings={"test_id": f"self_test_{test_run_id}"}
  )
  select_text = _extract_text_from_mcp_response(select_result)
  select_has_our_row = f"self_test_{test_run_id}" in select_text and "agent_kernel_integration_test" in select_text
  record_test_outcome(
    "select_row_roundtrip_matches",
    select_has_our_row and not select_result.get("isError", True),
    select_text[:300]
  )

  # Test 7: DROP the test table (cleanup)
  drop_sql = f"DROP TABLE IF EXISTS {test_table_name}"
  drop_result = _call_sqlite(drop_sql, database=test_database)
  record_test_outcome(
    "drop_test_table_cleanup",
    not drop_result.get("isError", True),
    _extract_text_from_mcp_response(drop_result)[:200]
  )

  # ── Schema initialization tests ──

  # Test 8: initialize_agent_kernel_database (idempotent schema creation)
  schema_ok, schema_msg = initialize_agent_kernel_database()
  record_test_outcome(
    "schema_initialization",
    schema_ok,
    schema_msg[:200]
  )

  # Test 9: Run it again to confirm idempotency
  if schema_ok:
    schema_ok_2, schema_msg_2 = initialize_agent_kernel_database()
    record_test_outcome(
      "schema_initialization_idempotent_rerun",
      schema_ok_2,
      schema_msg_2[:200]
    )

  # Test 10: Verify key tables exist by querying sqlite_master
  if schema_ok:
    tables_result = _call_sqlite(
      "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
      database=test_database
    )
    tables_text = _extract_text_from_mcp_response(tables_result)
    expected_tables = ["agents", "event_sources", "event_queue", "session_log",
                       "agent_checkpoints", "execution_receipts", "transcript_entries",
                       "compaction_boundaries", "memory_entries", "dead_letter_queue",
                       "agent_run_log", "schema_version", "admin_channel_state",
                       "agent_contact_access_control", "approval_requests"]
    missing_tables = [t for t in expected_tables if t not in tables_text]
    record_test_outcome(
      "schema_all_expected_tables_present",
      len(missing_tables) == 0,
      f"missing={missing_tables}" if missing_tables else f"all {len(expected_tables)} tables found"
    )

  # Test 11: Verify schema_version was recorded
  if schema_ok:
    version_result = _call_sqlite(
      "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1",
      database=test_database
    )
    version_text = _extract_text_from_mcp_response(version_result)
    record_test_outcome(
      "schema_version_recorded",
      str(AGENT_KERNEL_SCHEMA_VERSION) in version_text,
      version_text[:200]
    )

  # ── State machine tests ──

  # Test 12: Valid transitions accepted
  valid_transition_test_cases = [
    ("IDLE", "ASSEMBLING_CONTEXT"),
    ("ASSEMBLING_CONTEXT", "WAITING_FOR_LLM"),
    ("WAITING_FOR_LLM", "EXECUTING_TOOL"),
    ("EXECUTING_TOOL", "WAITING_FOR_LLM"),
    ("COMPLETED", "IDLE"),
    ("FAILED", "IDLE"),
  ]
  all_valid_ok = True
  for from_state, to_state in valid_transition_test_cases:
    ok, reason = validate_agent_state_transition(from_state, to_state)
    if not ok:
      all_valid_ok = False
      record_test_outcome(
        f"state_machine_valid_{from_state}_to_{to_state}",
        False,
        reason
      )
  record_test_outcome(
    "state_machine_valid_transitions_accepted",
    all_valid_ok,
    f"tested {len(valid_transition_test_cases)} valid transitions"
  )

  # Test 13: Invalid transitions rejected
  invalid_transition_test_cases = [
    ("IDLE", "EXECUTING_TOOL"),
    ("IDLE", "COMPLETED"),
    ("ASSEMBLING_CONTEXT", "IDLE"),
    ("COMPLETED", "EXECUTING_TOOL"),
    ("WAITING_FOR_LLM", "IDLE"),
  ]
  all_invalid_rejected = True
  for from_state, to_state in invalid_transition_test_cases:
    ok, reason = validate_agent_state_transition(from_state, to_state)
    if ok:
      all_invalid_rejected = False
      record_test_outcome(
        f"state_machine_invalid_{from_state}_to_{to_state}_should_be_rejected",
        False,
        "transition was accepted but should have been rejected"
      )
  record_test_outcome(
    "state_machine_invalid_transitions_rejected",
    all_invalid_rejected,
    f"tested {len(invalid_transition_test_cases)} invalid transitions"
  )

  # Test 14: Bogus state names rejected
  ok_bogus_1, _ = validate_agent_state_transition("NONEXISTENT", "IDLE")
  ok_bogus_2, _ = validate_agent_state_transition("IDLE", "NONEXISTENT")
  record_test_outcome(
    "state_machine_rejects_unknown_states",
    not ok_bogus_1 and not ok_bogus_2,
    "both unknown-state cases correctly rejected"
  )

  # ── Agent CRUD tests ──
  # Self-test agents use the configured LLM endpoint. Endpoint resolution
  # reads from settings[0].llm_endpoints in shared_config (nativemessaging.json).
  # Configure endpoints via admin menu or directly in config file.

  test_agent_display_name = f"Self Test Bot {test_run_id}"
  test_agent_system_prompt = f"You are a test agent created by self_test run {test_run_id}. Always respond concisely."

  # Test 15: create_agent (using real MLX model)
  create_result = handle_create_agent({
    "display_name": test_agent_display_name,
    "system_prompt": test_agent_system_prompt,
    "llm_provider": "mlx",
    "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
  })
  create_ok = not create_result.get("isError", True)
  created_agent_id = None
  if create_ok:
    try:
      created_data = json.loads(create_result["content"][0]["text"])
      created_agent_id = created_data.get("agent_id")
    except Exception:
      create_ok = False
  record_test_outcome(
    "create_agent",
    create_ok and created_agent_id is not None,
    f"agent_id={created_agent_id}" if created_agent_id else _extract_text_from_mcp_response(create_result)[:200]
  )

  if created_agent_id:
    # Test 16: get_agent
    get_result = handle_get_agent({"agent_id": created_agent_id})
    get_text = _extract_text_from_mcp_response(get_result)
    record_test_outcome(
      "get_agent_returns_created_agent",
      not get_result.get("isError", True) and created_agent_id in get_text,
      get_text[:200]
    )

    # Test 17: list_agents includes our agent
    list_result = handle_list_agents({})
    list_text = _extract_text_from_mcp_response(list_result)
    record_test_outcome(
      "list_agents_includes_created_agent",
      not list_result.get("isError", True) and created_agent_id in list_text,
      f"found={created_agent_id in list_text}"
    )

    # Test 18: update_agent
    update_result = handle_update_agent({
      "agent_id": created_agent_id,
      "display_name": f"Updated Bot {test_run_id}",
    })
    record_test_outcome(
      "update_agent",
      not update_result.get("isError", True),
      _extract_text_from_mcp_response(update_result)[:200]
    )

    # Test 19: send_message (runs full ReAct loop via Mac Mini MLX model)
    msg_result = handle_send_message({
      "agent_id": created_agent_id,
      "message": f"What is 3 + 4? Reply with just the number.",
    })
    msg_response_text = _extract_text_from_mcp_response(msg_result)
    msg_is_error = msg_result.get("isError", True)
    msg_agent_check = handle_get_agent({"agent_id": created_agent_id})
    msg_agent_text = _extract_text_from_mcp_response(msg_agent_check)
    msg_agent_is_idle = "IDLE" in msg_agent_text

    # Check for a completed run with an actual LLM response
    msg_has_response = False
    if not msg_is_error:
      try:
        msg_data = json.loads(msg_response_text)
        msg_has_response = msg_data.get("status") == "completed" and len(msg_data.get("response", "")) > 0
      except (json.JSONDecodeError, TypeError):
        pass

    record_test_outcome(
      "send_message_full_react_loop_with_real_llm",
      msg_has_response and msg_agent_is_idle,
      f"completed={msg_has_response}, agent_idle={msg_agent_is_idle}, isError={msg_is_error}, response_preview={msg_response_text[:150]}"
    )

    # Test 20: status
    status_result = handle_status({})
    record_test_outcome(
      "status_operation",
      not status_result.get("isError", True),
      _extract_text_from_mcp_response(status_result)[:200]
    )

    # Test 21: delete_agent with history
    delete_result = handle_delete_agent({
      "agent_id": created_agent_id,
      "delete_history": True,
    })
    record_test_outcome(
      "delete_agent_with_history",
      not delete_result.get("isError", True),
      _extract_text_from_mcp_response(delete_result)[:200]
    )

    # Test 22: get_agent should fail after deletion
    get_after_delete = handle_get_agent({"agent_id": created_agent_id})
    record_test_outcome(
      "get_agent_fails_after_deletion",
      get_after_delete.get("isError", False),
      "correctly returned error for deleted agent"
    )

  # ── Session log tests ──

  session_log_test_agent_id = f"_slog_test_{test_run_id}"
  session_log_test_run_id = f"slog_run_{test_run_id}"

  # Test 23: append a session log entry
  slog_ok, slog_err = _append_session_log_entry(
    session_log_test_agent_id,
    session_log_test_run_id,
    "run_started",
    {"test": True, "run_id": session_log_test_run_id}
  )
  record_test_outcome(
    "session_log_append_entry",
    slog_ok,
    slog_err if slog_err else "entry appended"
  )

  # Test 24: append a second entry with different type
  slog_ok_2, _ = _append_session_log_entry(
    session_log_test_agent_id,
    session_log_test_run_id,
    "state_transition",
    {"from_state": "IDLE", "to_state": "ASSEMBLING_CONTEXT"}
  )
  record_test_outcome(
    "session_log_append_second_entry",
    slog_ok_2,
    "second entry appended"
  )

  # Test 25: query session log — all entries for agent
  slog_query_result = _query_session_log(session_log_test_agent_id)
  slog_query_text = _extract_text_from_mcp_response(slog_query_result)
  slog_query_has_entries = "run_started" in slog_query_text and "state_transition" in slog_query_text
  record_test_outcome(
    "session_log_query_all_entries",
    not slog_query_result.get("isError", True) and slog_query_has_entries,
    f"found both entry types: {slog_query_has_entries}"
  )

  # Test 26: query with entry_type filter
  slog_filtered_result = _query_session_log(session_log_test_agent_id, entry_type="run_started")
  slog_filtered_text = _extract_text_from_mcp_response(slog_filtered_result)
  slog_filter_correct = "run_started" in slog_filtered_text and "state_transition" not in slog_filtered_text
  record_test_outcome(
    "session_log_query_filtered_by_type",
    not slog_filtered_result.get("isError", True) and slog_filter_correct,
    f"filter correct: {slog_filter_correct}"
  )

  # Test 27: query with run_id filter
  slog_run_filtered = _query_session_log(session_log_test_agent_id, run_id=session_log_test_run_id)
  slog_run_text = _extract_text_from_mcp_response(slog_run_filtered)
  record_test_outcome(
    "session_log_query_filtered_by_run_id",
    not slog_run_filtered.get("isError", True) and session_log_test_run_id in slog_run_text,
    "run_id filter works"
  )

  # Cleanup: remove test session log entries
  _call_sqlite(
    "DELETE FROM session_log WHERE agent_id = :agent_id",
    database=test_database,
    bindings={"agent_id": session_log_test_agent_id}
  )

  # ── Checkpoint tests ──

  ckpt_test_agent_id = f"_ckpt_test_{test_run_id}"
  ckpt_test_run_id = f"ckpt_run_{test_run_id}"

  # Test 28: write a checkpoint
  ckpt_ok, ckpt_err = _write_checkpoint(
    ckpt_test_agent_id, ckpt_test_run_id, "test_session", 1,
    {"current_state": "ASSEMBLING_CONTEXT", "messages": ["hello"], "step": 1}
  )
  record_test_outcome(
    "checkpoint_write",
    ckpt_ok,
    ckpt_err if ckpt_err else "checkpoint written"
  )

  # Test 29: write a second checkpoint (higher step_number)
  ckpt_ok_2, _ = _write_checkpoint(
    ckpt_test_agent_id, ckpt_test_run_id, "test_session", 2,
    {"current_state": "WAITING_FOR_LLM", "messages": ["hello", "response"], "step": 2}
  )
  record_test_outcome(
    "checkpoint_write_second",
    ckpt_ok_2,
    "second checkpoint written"
  )

  # Test 30: load latest checkpoint (should be step 2)
  latest_ckpt = _load_latest_checkpoint(ckpt_test_agent_id, ckpt_test_run_id)
  ckpt_is_step_2 = latest_ckpt is not None and latest_ckpt.get("current_state") == "WAITING_FOR_LLM"
  record_test_outcome(
    "checkpoint_load_latest_returns_most_recent",
    ckpt_is_step_2,
    f"state={latest_ckpt.get('current_state') if latest_ckpt else 'None'}, meta={latest_ckpt.get('_checkpoint_meta') if latest_ckpt else 'None'}"
  )

  # Test 31: load checkpoint for nonexistent run
  no_ckpt = _load_latest_checkpoint(ckpt_test_agent_id, "nonexistent_run_id")
  record_test_outcome(
    "checkpoint_load_nonexistent_returns_none",
    no_ckpt is None,
    "correctly returned None"
  )

  # Cleanup checkpoints
  _call_sqlite(
    "DELETE FROM agent_checkpoints WHERE agent_id = :agent_id",
    database=test_database,
    bindings={"agent_id": ckpt_test_agent_id}
  )
  _call_sqlite(
    "DELETE FROM session_log WHERE agent_id = :agent_id",
    database=test_database,
    bindings={"agent_id": ckpt_test_agent_id}
  )

  # ── Execution receipt tests ──

  receipt_test_run_id = f"receipt_run_{test_run_id}"

  # Test 32: compute deterministic execution ID
  exec_id_1 = _compute_execution_receipt_id(receipt_test_run_id, 1, "web_search", {"query": "test"})
  exec_id_2 = _compute_execution_receipt_id(receipt_test_run_id, 1, "web_search", {"query": "test"})
  exec_id_different = _compute_execution_receipt_id(receipt_test_run_id, 2, "web_search", {"query": "test"})
  record_test_outcome(
    "execution_receipt_deterministic_id",
    exec_id_1 == exec_id_2 and exec_id_1 != exec_id_different,
    f"same={exec_id_1 == exec_id_2}, diff={exec_id_1 != exec_id_different}"
  )

  # Test 33: no receipt exists initially
  no_receipt = _get_existing_execution_receipt(exec_id_1)
  record_test_outcome(
    "execution_receipt_not_found_initially",
    no_receipt is None,
    "correctly returned None"
  )

  # Test 34: create pending + complete receipt
  pend_ok, pend_err = _create_pending_execution_receipt(exec_id_1, receipt_test_run_id, "web_search")
  record_test_outcome(
    "execution_receipt_create_pending",
    pend_ok,
    pend_err if pend_err else "pending receipt created"
  )

  comp_ok, comp_err = _complete_execution_receipt(exec_id_1, {"text": "search result", "isError": False})
  record_test_outcome(
    "execution_receipt_complete",
    comp_ok,
    comp_err if comp_err else "receipt completed"
  )

  # Test 35: retrieve completed receipt
  found_receipt = _get_existing_execution_receipt(exec_id_1)
  record_test_outcome(
    "execution_receipt_retrieve_completed",
    found_receipt is not None and found_receipt.get("text") == "search result",
    f"found={found_receipt is not None}, text={found_receipt.get('text') if found_receipt else 'None'}"
  )

  # Cleanup receipts
  _call_sqlite(
    "DELETE FROM execution_receipts WHERE run_id = :run_id",
    database=test_database,
    bindings={"run_id": receipt_test_run_id}
  )

  # ── Context assembly test ──

  # Test 36: basic context assembly
  mock_agent_config = {
    "agent_id": f"_ctx_test_{test_run_id}",
    "system_prompt": "You are a test assistant.",
    "working_context": "Currently running self-test.",
  }
  messages, budget = _assemble_context_for_agent_run(mock_agent_config, "test_session", "Hello!", max_context_tokens=4096)
  ctx_has_system = len(messages) >= 2 and messages[0]["role"] == "system"
  ctx_has_user = messages[-1]["role"] == "user" and messages[-1]["content"] == "Hello!"
  ctx_has_working = "Currently running self-test" in messages[0]["content"]
  record_test_outcome(
    "context_assembly_basic_structure",
    ctx_has_system and ctx_has_user and ctx_has_working,
    f"system={ctx_has_system}, user={ctx_has_user}, working_ctx={ctx_has_working}, budget_keys={list(budget.keys())}"
  )

  # ── State machine integration test ──

  # Test 37: create a test agent and transition its state
  sm_agent_name = f"SM Test {test_run_id}"
  sm_create = handle_create_agent({"display_name": sm_agent_name, "system_prompt": "Test."})
  sm_agent_id = None
  if not sm_create.get("isError"):
    try:
      sm_agent_id = json.loads(sm_create["content"][0]["text"])["agent_id"]
    except Exception:
      pass

  if sm_agent_id:
    sm_run_id = f"sm_run_{test_run_id}"
    sm_session_id = f"sm_session_{test_run_id}"

    # Transition IDLE → ASSEMBLING_CONTEXT
    t_ok, t_err = _transition_agent_state(
      sm_agent_id, sm_run_id, sm_session_id, 1,
      "IDLE", "ASSEMBLING_CONTEXT",
      {"test": True}
    )
    record_test_outcome(
      "state_machine_integration_valid_transition",
      t_ok,
      t_err if t_err else "IDLE → ASSEMBLING_CONTEXT succeeded"
    )

    # Verify state persisted in DB
    sm_get = handle_get_agent({"agent_id": sm_agent_id})
    sm_get_text = _extract_text_from_mcp_response(sm_get)
    record_test_outcome(
      "state_machine_integration_state_persisted_in_db",
      "ASSEMBLING_CONTEXT" in sm_get_text,
      f"state in DB: {'ASSEMBLING_CONTEXT' in sm_get_text}"
    )

    # Test 38: invalid transition should be rejected
    t_ok_bad, t_err_bad = _transition_agent_state(
      sm_agent_id, sm_run_id, sm_session_id, 2,
      "ASSEMBLING_CONTEXT", "IDLE",
      {"test": True}
    )
    record_test_outcome(
      "state_machine_integration_invalid_transition_rejected",
      not t_ok_bad,
      t_err_bad[:100] if t_err_bad else "unexpectedly succeeded"
    )

    # Cleanup: reset and delete
    _call_sqlite(
      "UPDATE agents SET current_state = 'IDLE' WHERE agent_id = :agent_id",
      database=test_database,
      bindings={"agent_id": sm_agent_id}
    )
    handle_delete_agent({"agent_id": sm_agent_id, "delete_history": True})

  # ── Crash recovery test ──

  # Test 39: create an agent stuck in ASSEMBLING_CONTEXT, verify recovery resets it
  cr_agent_name = f"Crash Test {test_run_id}"
  cr_create = handle_create_agent({"display_name": cr_agent_name, "system_prompt": "Test crash recovery."})
  cr_agent_id = None
  if not cr_create.get("isError"):
    try:
      cr_agent_id = json.loads(cr_create["content"][0]["text"])["agent_id"]
    except Exception:
      pass

  if cr_agent_id:
    _call_sqlite(
      "UPDATE agents SET current_state = 'ASSEMBLING_CONTEXT' WHERE agent_id = :agent_id",
      database=test_database,
      bindings={"agent_id": cr_agent_id}
    )

    recovery_result = _recover_agents_in_non_terminal_states()
    cr_recovered = any(
      a.get("agent_id") == cr_agent_id for a in recovery_result.get("agents", [])
    )
    record_test_outcome(
      "crash_recovery_resets_stuck_agent",
      cr_recovered and recovery_result.get("agents_recovered", 0) >= 1,
      f"recovered={cr_recovered}, count={recovery_result.get('agents_recovered')}"
    )

    # Verify agent is now IDLE
    cr_get = handle_get_agent({"agent_id": cr_agent_id})
    cr_get_text = _extract_text_from_mcp_response(cr_get)
    record_test_outcome(
      "crash_recovery_agent_is_idle_after_recovery",
      "IDLE" in cr_get_text,
      f"IDLE in response: {'IDLE' in cr_get_text}"
    )

    handle_delete_agent({"agent_id": cr_agent_id, "delete_history": True})

  # ── Phase 2: Durable event queue tests ──

  eq_test_agent_id = f"_eq_test_{test_run_id}"

  # Test 41: enqueue event and verify in DB
  eq_ok, eq_msg, eq_qid = _enqueue_event(
    agent_id=eq_test_agent_id,
    event_type="test_event",
    payload={"message": "hello from test", "session_id": "test_session"},
    priority="normal",
    queue_mode="queue",
  )
  record_test_outcome(
    "event_queue_enqueue_event",
    eq_ok and eq_qid is not None,
    f"ok={eq_ok}, msg={eq_msg}, queue_id={eq_qid}"
  )

  # Test 42: dequeue respects priority ordering (HIGH before NORMAL)
  eq_ok_low, _, eq_qid_low = _enqueue_event(eq_test_agent_id, "test_low", {"message": "low"}, priority="low")
  eq_ok_high, _, eq_qid_high = _enqueue_event(eq_test_agent_id, "test_high", {"message": "high"}, priority="high")
  dequeued_event = _dequeue_next_event(eq_test_agent_id)
  dequeued_is_high_priority = dequeued_event is not None and dequeued_event.get("priority") == "high"
  record_test_outcome(
    "event_queue_dequeue_respects_priority_ordering",
    dequeued_is_high_priority,
    f"dequeued priority={dequeued_event.get('priority') if dequeued_event else 'None'} (expected high)"
  )
  if dequeued_event:
    _complete_event(dequeued_event["queue_id"])

  # Test 43: idempotency_key prevents duplicates
  idem_key = f"idem_test_{test_run_id}"
  idem_ok_1, idem_msg_1, idem_qid_1 = _enqueue_event(eq_test_agent_id, "test_idem", {"message": "first"}, idempotency_key=idem_key)
  idem_ok_2, idem_msg_2, idem_qid_2 = _enqueue_event(eq_test_agent_id, "test_idem", {"message": "duplicate"}, idempotency_key=idem_key)
  record_test_outcome(
    "event_queue_idempotency_key_prevents_duplicates",
    idem_ok_1 and idem_ok_2 and idem_msg_2 == "duplicate_skipped_by_idempotency_key",
    f"first={idem_msg_1}, second={idem_msg_2}"
  )

  # Test 44: _complete_event status transition
  ev_to_complete = _dequeue_next_event(eq_test_agent_id)
  if ev_to_complete:
    comp_ok, comp_err = _complete_event(ev_to_complete["queue_id"])
    record_test_outcome(
      "event_queue_complete_event_status_transition",
      comp_ok,
      comp_err if comp_err else "event marked completed"
    )
  else:
    record_test_outcome("event_queue_complete_event_status_transition", False, "no event to complete")

  # Test 45: _dead_letter_event status transition
  ev_to_dl = _dequeue_next_event(eq_test_agent_id)
  if ev_to_dl:
    dl_ok, dl_err = _dead_letter_event(ev_to_dl["queue_id"], "test error")
    record_test_outcome(
      "event_queue_dead_letter_event_status_transition",
      dl_ok,
      dl_err if dl_err else "event dead-lettered"
    )
  else:
    record_test_outcome("event_queue_dead_letter_event_status_transition", False, "no event to dead-letter")

  # Test 46: _get_pending_event_count
  _enqueue_event(eq_test_agent_id, "count_test_a", {"message": "a"})
  _enqueue_event(eq_test_agent_id, "count_test_b", {"message": "b"})
  pending_count = _get_pending_event_count(eq_test_agent_id)
  record_test_outcome(
    "event_queue_pending_count",
    pending_count >= 2,
    f"pending_count={pending_count} (expected >= 2)"
  )

  # Cleanup event queue test data
  _call_sqlite(
    "DELETE FROM event_queue WHERE agent_id = :agent_id",
    database=test_database,
    bindings={"agent_id": eq_test_agent_id}
  )
  _call_sqlite(
    "DELETE FROM dead_letter_queue WHERE agent_id = :agent_id",
    database=test_database,
    bindings={"agent_id": eq_test_agent_id}
  )

  # ── Phase 2: Event source lifecycle test ──

  es_agent_name = f"Event Source Test {test_run_id}"
  es_create = handle_create_agent({"display_name": es_agent_name, "system_prompt": "Test event sources."})
  es_agent_id = None
  if not es_create.get("isError"):
    try:
      es_agent_id = json.loads(es_create["content"][0]["text"])["agent_id"]
    except Exception:
      pass

  if es_agent_id:
    # Test 47: add_event_source
    add_es_result = handle_add_event_source({
      "agent_id": es_agent_id,
      "source_type": "cron",
      "config": {"schedule": "0 8 * * *", "message": "Good morning"},
      "priority": "low",
      "queue_mode": "drop",
    })
    add_es_ok = not add_es_result.get("isError", True)
    es_source_id = None
    if add_es_ok:
      try:
        es_data = json.loads(add_es_result["content"][0]["text"])
        es_source_id = es_data.get("source_id")
      except Exception:
        pass
    record_test_outcome(
      "event_source_add",
      add_es_ok and es_source_id is not None,
      f"source_id={es_source_id}" if es_source_id else _extract_text_from_mcp_response(add_es_result)[:200]
    )

    # Test 48: list_event_sources
    list_es_result = handle_list_event_sources({"agent_id": es_agent_id})
    list_es_text = _extract_text_from_mcp_response(list_es_result)
    record_test_outcome(
      "event_source_list_includes_added_source",
      not list_es_result.get("isError", True) and (es_source_id or "") in list_es_text,
      f"found source in list: {(es_source_id or '') in list_es_text}"
    )

    # Test 49: remove_event_source
    if es_source_id:
      rm_es_result = handle_remove_event_source({"source_id": es_source_id})
      record_test_outcome(
        "event_source_remove",
        not rm_es_result.get("isError", True),
        _extract_text_from_mcp_response(rm_es_result)[:200]
      )

    # ── Phase 2: Pause/resume test ──

    # Test 50: pause_agent sets is_paused flag
    pause_result = handle_pause_agent({"agent_id": es_agent_id})
    record_test_outcome(
      "pause_agent",
      not pause_result.get("isError", True),
      _extract_text_from_mcp_response(pause_result)[:200]
    )

    # Verify is_paused = 1
    paused_check = _call_sqlite(
      "SELECT is_paused FROM agents WHERE agent_id = :agent_id",
      database=test_database,
      bindings={"agent_id": es_agent_id}
    )
    paused_text = _extract_text_from_mcp_response(paused_check)
    is_paused_set = '"is_paused": 1' in paused_text or "'is_paused': 1" in paused_text
    record_test_outcome(
      "pause_agent_flag_persisted_in_db",
      is_paused_set,
      f"is_paused in DB: {is_paused_set}"
    )

    # Test 51: resume_agent clears is_paused flag
    resume_result = handle_resume_agent({"agent_id": es_agent_id})
    record_test_outcome(
      "resume_agent",
      not resume_result.get("isError", True),
      _extract_text_from_mcp_response(resume_result)[:200]
    )

    resumed_check = _call_sqlite(
      "SELECT is_paused FROM agents WHERE agent_id = :agent_id",
      database=test_database,
      bindings={"agent_id": es_agent_id}
    )
    resumed_text = _extract_text_from_mcp_response(resumed_check)
    is_resumed = '"is_paused": 0' in resumed_text or "'is_paused': 0" in resumed_text
    record_test_outcome(
      "resume_agent_flag_cleared_in_db",
      is_resumed,
      f"is_paused cleared: {is_resumed}"
    )

    handle_delete_agent({"agent_id": es_agent_id, "delete_history": True})

  # ── Phase 3: Model-size class detection tests ──

  # Test 56: classify model sizes → correct classes (spec §4 Step 6 boundaries)
  classify_test_cases = {
    2000: "tiny", 8000: "small", 32000: "medium", 200000: "large",
  }
  classify_results = {k: _classify_model_size_class_from_context_window(k) for k in classify_test_cases}
  all_classes_correct = all(classify_results[k] == classify_test_cases[k] for k in classify_test_cases)
  record_test_outcome(
    "model_size_class_classification_representative_values",
    all_classes_correct,
    f"2K={classify_results[2000]}, 8K={classify_results[8000]}, 32K={classify_results[32000]}, 200K={classify_results[200000]}"
  )

  # Test 57: exact boundary values (fence-post correctness)
  boundary_test_cases = {
    4095: "tiny", 4096: "small", 16383: "small", 16384: "medium",
    131071: "medium", 131072: "large",
  }
  boundary_results = {k: _classify_model_size_class_from_context_window(k) for k in boundary_test_cases}
  all_boundaries_correct = all(boundary_results[k] == boundary_test_cases[k] for k in boundary_test_cases)
  record_test_outcome(
    "model_size_class_exact_boundary_values",
    all_boundaries_correct,
    " ".join(f"{k}={boundary_results[k]}" for k in sorted(boundary_test_cases.keys()))
  )

  # Test 58: adaptation parameters have all required keys for every class
  required_adaptation_keys = {
    "compaction_trigger_threshold_fraction", "max_archival_memory_entries_to_inject",
    "history_budget_cap_tokens", "tool_schema_verbosity",
    "recent_messages_protected_from_compaction",
  }
  all_adaptations_have_required_keys = True
  adaptation_issue_detail = ""
  for size_class in ["tiny", "small", "medium", "large"]:
    adaptations = _get_adaptations_for_model_size_class(size_class)
    if set(adaptations.keys()) != required_adaptation_keys:
      all_adaptations_have_required_keys = False
      adaptation_issue_detail = f"{size_class} keys={set(adaptations.keys())}"
      break
  record_test_outcome(
    "model_size_class_adaptation_parameters_complete_for_all_classes",
    all_adaptations_have_required_keys,
    adaptation_issue_detail if adaptation_issue_detail else "all 4 classes have all required keys"
  )

  # Test 59: compaction thresholds increase monotonically with model size
  thresholds_by_class = [
    _get_adaptations_for_model_size_class(c)["compaction_trigger_threshold_fraction"]
    for c in ["tiny", "small", "medium", "large"]
  ]
  thresholds_monotonically_increasing = all(
    thresholds_by_class[i] < thresholds_by_class[i + 1] for i in range(len(thresholds_by_class) - 1)
  )
  record_test_outcome(
    "model_size_class_compaction_thresholds_increase_with_size",
    thresholds_monotonically_increasing,
    f"tiny={thresholds_by_class[0]}, small={thresholds_by_class[1]}, medium={thresholds_by_class[2]}, large={thresholds_by_class[3]}"
  )

  # Test 60: discovery fallback to 4096 for nonexistent provider/model
  saved_cache = dict(_discovered_model_context_window_cache)
  _discovered_model_context_window_cache.clear()
  fallback_test_config = {"llm_provider": "_nonexistent_provider_for_test", "llm_model": "_nonexistent_model"}
  fallback_window = _discover_model_context_window_tokens(fallback_test_config)
  record_test_outcome(
    "model_context_window_discovery_fallback_to_4096",
    fallback_window == FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS,
    f"got={fallback_window}, expected={FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS}"
  )

  # Test 61: discovery cache returns cached value without re-querying
  fallback_window_from_cache = _discover_model_context_window_tokens(fallback_test_config)
  record_test_outcome(
    "model_context_window_discovery_cache_hit",
    fallback_window_from_cache == FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS,
    f"cached={fallback_window_from_cache}"
  )
  _discovered_model_context_window_cache.clear()
  _discovered_model_context_window_cache.update(saved_cache)

  # Test 62: _extract_context_window_from_model_info_response parses various formats
  extract_flat = _extract_context_window_from_model_info_response({"context_length": 65536})
  extract_nested = _extract_context_window_from_model_info_response({"model_info": {"context_window": 32768}})
  extract_top_provider = _extract_context_window_from_model_info_response({"top_provider_context_length": 128000})
  extract_missing = _extract_context_window_from_model_info_response({"name": "test", "provider": "test"})
  record_test_outcome(
    "context_window_extraction_from_various_response_formats",
    extract_flat == 65536 and extract_nested == 32768 and extract_top_provider == 128000 and extract_missing is None,
    f"flat={extract_flat}, nested={extract_nested}, top_provider={extract_top_provider}, missing={extract_missing}"
  )

  # Test 63: known model lookup works as secondary fallback
  saved_cache_2 = dict(_discovered_model_context_window_cache)
  _discovered_model_context_window_cache.clear()
  known_model_config = {"llm_provider": "mlx", "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4"}
  known_window = _discover_model_context_window_tokens(known_model_config)
  known_window_is_correct = known_window == 65536 or known_window == FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS
  record_test_outcome(
    "model_context_window_known_model_lookup_or_discovery",
    known_window > 0 and known_window_is_correct,
    f"window={known_window} (65536 from known table or {FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS} from fallback — both acceptable)"
  )
  _discovered_model_context_window_cache.clear()
  _discovered_model_context_window_cache.update(saved_cache_2)

  # ── Phase 3: Context Budget Planner tests ──

  # Test 64: budget for 4K model — tight, history gets minimum share
  budget_4k = _plan_context_budget(
    {"system_prompt": "test"},
    system_content_tokens=200, tool_definitions_tokens=100,
    event_payload_tokens=50, model_context_window_tokens=4096,
  )
  record_test_outcome(
    "context_budget_4k_model_tight_allocation",
    budget_4k["model_size_class"] == "small"
    and budget_4k["response_headroom_tokens"] >= 512
    and budget_4k["history_budget_allocated_tokens"] >= 0
    and budget_4k["total_allocated_tokens"] <= 4096
    and not budget_4k["needs_emergency_compaction"],
    f"class={budget_4k['model_size_class']}, headroom={budget_4k['response_headroom_tokens']}, history={budget_4k['history_budget_allocated_tokens']}, total={budget_4k['total_allocated_tokens']}"
  )

  # Test 65: budget for 32K model — comfortable allocation
  budget_32k = _plan_context_budget(
    {"system_prompt": "test"},
    system_content_tokens=500, tool_definitions_tokens=300,
    event_payload_tokens=100, model_context_window_tokens=32768,
  )
  record_test_outcome(
    "context_budget_32k_model_comfortable_allocation",
    budget_32k["model_size_class"] == "medium"
    and budget_32k["history_budget_allocated_tokens"] > budget_4k["history_budget_allocated_tokens"]
    and budget_32k["archival_memory_budget_allocated_tokens"] >= 0
    and budget_32k["total_allocated_tokens"] <= 32768,
    f"class={budget_32k['model_size_class']}, history={budget_32k['history_budget_allocated_tokens']}, archival={budget_32k['archival_memory_budget_allocated_tokens']}, total={budget_32k['total_allocated_tokens']}"
  )

  # Test 66: budget for 200K model — history capped at ~50K
  budget_200k = _plan_context_budget(
    {"system_prompt": "test"},
    system_content_tokens=1000, tool_definitions_tokens=500,
    event_payload_tokens=200, model_context_window_tokens=200000,
  )
  history_is_capped = budget_200k["history_budget_max_tokens"] <= 51200
  record_test_outcome(
    "context_budget_200k_model_history_capped",
    budget_200k["model_size_class"] == "large"
    and history_is_capped
    and budget_200k["total_allocated_tokens"] <= 200000,
    f"class={budget_200k['model_size_class']}, history_max={budget_200k['history_budget_max_tokens']}, history_alloc={budget_200k['history_budget_allocated_tokens']}, total={budget_200k['total_allocated_tokens']}"
  )

  # Test 67: response headroom never below 512
  budget_tiny = _plan_context_budget(
    {"system_prompt": "test"},
    system_content_tokens=50, tool_definitions_tokens=0,
    event_payload_tokens=10, model_context_window_tokens=2048,
  )
  record_test_outcome(
    "context_budget_response_headroom_floor_512",
    budget_tiny["response_headroom_tokens"] >= 512,
    f"headroom={budget_tiny['response_headroom_tokens']}"
  )

  # Test 68: fixed sections always fit — emergency flag when they exceed context
  budget_overflow = _plan_context_budget(
    {"system_prompt": "test"},
    system_content_tokens=3000, tool_definitions_tokens=1000,
    event_payload_tokens=500, model_context_window_tokens=2048,
  )
  record_test_outcome(
    "context_budget_emergency_flag_when_fixed_exceeds_context",
    budget_overflow["needs_emergency_compaction"],
    f"needs_emergency={budget_overflow['needs_emergency_compaction']}, fixed={budget_overflow['fixed_section_tokens']}, context={budget_overflow['model_context_window_tokens']}"
  )

  # Test 69: upgraded _assemble_context_for_agent_run returns budget plan keys
  asm_config = {
    "agent_id": f"_budget_test_{test_run_id}",
    "system_prompt": "You are a test assistant.",
    "working_context": "Testing budget planner.",
    "llm_provider": "mlx",
    "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
  }
  asm_messages, asm_budget = _assemble_context_for_agent_run(asm_config, "test_session", "Hello!")
  budget_has_plan_keys = all(k in asm_budget for k in [
    "model_size_class", "history_budget_allocated_tokens",
    "response_headroom_tokens", "compaction_trigger_threshold_tokens",
  ])
  record_test_outcome(
    "context_assembly_uses_budget_planner",
    budget_has_plan_keys and len(asm_messages) >= 2,
    f"has_plan_keys={budget_has_plan_keys}, msg_count={len(asm_messages)}, class={asm_budget.get('model_size_class')}"
  )

  # ── Phase 3.2: Tool result spillover tests ──
  short_text = "This is a short result."
  short_result = _spillover_tool_result_if_oversized(
    f"_spill_test_{test_run_id}", "test_session", "web_search",
    short_text, "exec_short_123",
  )
  record_test_outcome(
    "spillover_short_result_passes_through_unchanged",
    short_result == short_text,
    f"unchanged={short_result == short_text}, len={len(short_result)}"
  )

  long_text_lines = [f"Line {i}: " + ("x" * 100) for i in range(200)]
  long_text = "\n".join(long_text_lines)
  long_result = _spillover_tool_result_if_oversized(
    f"_spill_test_{test_run_id}", "test_session", "web_search",
    long_text, "exec_long_456",
  )
  long_result_has_truncation_marker = "[Result truncated" in long_result
  long_result_has_first_lines = "Line 0:" in long_result
  long_result_has_last_lines = "Line 199:" in long_result
  long_result_is_shorter_than_original = len(long_result) < len(long_text)
  record_test_outcome(
    "spillover_long_result_truncated_with_preview",
    long_result_has_truncation_marker and long_result_has_first_lines and long_result_has_last_lines and long_result_is_shorter_than_original,
    f"marker={long_result_has_truncation_marker}, first={long_result_has_first_lines}, last={long_result_has_last_lines}, shorter={long_result_is_shorter_than_original}"
  )

  error_text_lines = [f"Line {i}: data" for i in range(100)]
  error_text_lines[50] = "ERROR: something went wrong at line 50"
  error_text_lines[70] = "Traceback (most recent call last):"
  error_text = "\n".join(error_text_lines)
  if len(error_text) <= TOOL_RESULT_SPILLOVER_THRESHOLD_CHARACTERS:
    error_text = error_text + ("\n" + "x" * 200) * 40
  error_result = _spillover_tool_result_if_oversized(
    f"_spill_test_{test_run_id}", "test_session", "web_search",
    error_text, "exec_err_789",
  )
  error_result_has_error_section = "Error lines found" in error_result
  record_test_outcome(
    "spillover_extracts_error_lines_from_middle",
    error_result_has_error_section,
    f"has_error_section={error_result_has_error_section}"
  )

  exact_threshold_text = "a" * TOOL_RESULT_SPILLOVER_THRESHOLD_CHARACTERS
  exact_result = _spillover_tool_result_if_oversized(
    f"_spill_test_{test_run_id}", "test_session", "test_tool",
    exact_threshold_text, "exec_exact",
  )
  record_test_outcome(
    "spillover_exact_threshold_passes_through",
    exact_result == exact_threshold_text,
    f"unchanged={exact_result == exact_threshold_text}, len={len(exact_threshold_text)}"
  )

  one_over_threshold_text = "a" * (TOOL_RESULT_SPILLOVER_THRESHOLD_CHARACTERS + 1)
  one_over_result = _spillover_tool_result_if_oversized(
    f"_spill_test_{test_run_id}", "test_session", "test_tool",
    one_over_threshold_text, "exec_one_over",
  )
  record_test_outcome(
    "spillover_one_over_threshold_triggers_truncation",
    "[Result truncated" in one_over_result,
    f"truncated={('[Result truncated' in one_over_result)}"
  )

  # ── Phase 3.3: Microcompact tests ──
  mc_user_msg = {"role": "user", "content": "Hello agent"}
  mc_asst_msg = {"role": "assistant", "content": "Hello! How can I help?"}
  mc_tool_short = {"role": "tool", "content": "Result: 42", "tool_call_id": "tc1"}
  mc_tool_long_10k = {"role": "tool", "content": "X" * 10000, "tool_call_id": "tc2"}
  mc_messages_with_one_long_tool_result = [mc_user_msg, mc_tool_long_10k, mc_asst_msg, mc_user_msg]

  mc_result_protected = _microcompact_single_message(mc_tool_long_10k, 1, 5000, 8000)
  mc_protected_unchanged = mc_result_protected["content"] == mc_tool_long_10k["content"]
  record_test_outcome(
    "microcompact_protects_recent_messages",
    mc_protected_unchanged,
    f"protected={mc_protected_unchanged}, messages_from_end=1"
  )

  mc_result_old = _microcompact_single_message(mc_tool_long_10k, 25, 5000, 8000)
  mc_old_was_shrunk = len(mc_result_old["content"]) < len(mc_tool_long_10k["content"])
  mc_old_within_budget = len(mc_result_old["content"]) <= 600
  record_test_outcome(
    "microcompact_shrinks_old_tool_results",
    mc_old_was_shrunk and mc_old_within_budget,
    f"shrunk={mc_old_was_shrunk}, len={len(mc_result_old['content'])}, budget=500"
  )

  mc_result_user = _microcompact_single_message(mc_user_msg, 25, 5000, 8000)
  mc_user_unchanged = mc_result_user["content"] == mc_user_msg["content"]
  record_test_outcome(
    "microcompact_does_not_touch_user_messages",
    mc_user_unchanged,
    f"unchanged={mc_user_unchanged}"
  )

  mc_result_mid = _microcompact_single_message(mc_tool_long_10k, 12, 5000, 8000)
  mc_mid_was_shrunk = len(mc_result_mid["content"]) < len(mc_tool_long_10k["content"])
  mc_mid_larger_than_old = len(mc_result_mid["content"]) > len(mc_result_old["content"])
  record_test_outcome(
    "microcompact_mid_age_allows_more_content_than_old",
    mc_mid_was_shrunk and mc_mid_larger_than_old,
    f"mid_len={len(mc_result_mid['content'])}, old_len={len(mc_result_old['content'])}"
  )

  mc_error_content = "OK line\n" * 50 + "ERROR: disk full\nTraceback (most recent call last):\n  File test.py line 42\n" + "OK line\n" * 50
  mc_tool_with_errors = {"role": "tool", "content": mc_error_content, "tool_call_id": "tc3"}
  mc_result_errors = _microcompact_single_message(mc_tool_with_errors, 25, 5000, 8000)
  mc_error_preserved = "ERROR:" in mc_result_errors["content"] or "Traceback" in mc_result_errors["content"]
  record_test_outcome(
    "microcompact_preserves_error_lines_in_shrunk_result",
    mc_error_preserved,
    f"error_preserved={mc_error_preserved}"
  )

  mc_batch_messages = []
  for i in range(30):
    mc_batch_messages.append({"role": "user", "content": f"Question {i}"})
    mc_batch_messages.append({"role": "tool", "content": "Y" * 5000, "tool_call_id": f"tc_{i}"})
    mc_batch_messages.append({"role": "assistant", "content": f"Answer {i}"})

  mc_batch_tokens_before = sum(
    _estimate_token_count_from_characters(m.get("content", "")) for m in mc_batch_messages
  )
  mc_batch_result = _apply_microcompact_to_history_messages(
    mc_batch_messages, 5000, mc_batch_tokens_before,
  )
  mc_batch_tokens_after = sum(
    _estimate_token_count_from_characters(m.get("content", "")) for m in mc_batch_result
  )
  mc_batch_reduced = mc_batch_tokens_after < mc_batch_tokens_before
  record_test_outcome(
    "microcompact_batch_reduces_total_tokens",
    mc_batch_reduced,
    f"before={mc_batch_tokens_before}, after={mc_batch_tokens_after}, reduced={mc_batch_reduced}"
  )

  # ── Phase 3.4: Context collapse tests ──
  cc_messages_with_tool_pairs = [
    {"role": "user", "content": "Search for Python docs"},
    {"role": "assistant", "content": "I'll search.", "tool_calls": [{"id": "tc1", "function": {"name": "web_search", "arguments": '{"query": "Python docs"}'}}]},
    {"role": "tool", "content": "Found 10 results about Python documentation...", "tool_call_id": "tc1"},
    {"role": "assistant", "content": "Here are the results."},
    {"role": "user", "content": "Read the first one"},
    {"role": "assistant", "content": "Reading.", "tool_calls": [{"id": "tc2", "function": {"name": "file_read", "arguments": '{"path": "/docs/python.md"}'}}]},
    {"role": "tool", "content": "# Python Documentation\nThis is the content of the file which is quite long and detailed with many sections and subsections covering various topics.", "tool_call_id": "tc2"},
    {"role": "assistant", "content": "The file contains documentation."},
    {"role": "user", "content": "Thanks"},
    {"role": "assistant", "content": "You're welcome!"},
    {"role": "user", "content": "One more thing"},
    {"role": "assistant", "content": "Sure, what is it?"},
  ]

  cc_result = _apply_context_collapse_to_history_messages(
    cc_messages_with_tool_pairs, 100, 9999,
  )
  cc_original_count = len(cc_messages_with_tool_pairs)
  cc_collapsed_count = len(cc_result)
  cc_was_reduced = cc_collapsed_count < cc_original_count
  record_test_outcome(
    "context_collapse_reduces_message_count",
    cc_was_reduced,
    f"original={cc_original_count}, collapsed={cc_collapsed_count}"
  )

  cc_has_collapsed_marker = any("[collapsed]" in m.get("content", "") for m in cc_result)
  record_test_outcome(
    "context_collapse_marks_collapsed_messages",
    cc_has_collapsed_marker,
    f"has_marker={cc_has_collapsed_marker}"
  )

  cc_has_tool_name_in_summary = any("web_search" in m.get("content", "") for m in cc_result if "[collapsed]" in m.get("content", ""))
  record_test_outcome(
    "context_collapse_includes_tool_name_in_summary",
    cc_has_tool_name_in_summary,
    f"has_tool_name={cc_has_tool_name_in_summary}"
  )

  cc_recent_protected = cc_result[-1].get("content") == "Sure, what is it?"
  cc_second_recent_protected = cc_result[-2].get("content") == "One more thing"
  record_test_outcome(
    "context_collapse_protects_recent_messages",
    cc_recent_protected and cc_second_recent_protected,
    f"last_ok={cc_recent_protected}, second_last_ok={cc_second_recent_protected}"
  )

  cc_no_collapse_messages = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!"},
  ]
  cc_no_collapse_result = _apply_context_collapse_to_history_messages(
    cc_no_collapse_messages, 9999, 9999,
  )
  record_test_outcome(
    "context_collapse_skipped_when_within_budget",
    len(cc_no_collapse_result) == len(cc_no_collapse_messages),
    f"unchanged={len(cc_no_collapse_result) == len(cc_no_collapse_messages)}"
  )

  cc_pair_summary = _collapse_tool_call_result_pair_to_summary(
    {"role": "assistant", "content": "", "tool_calls": [{"id": "tc99", "function": {"name": "sqlite_query", "arguments": '{"sql": "SELECT * FROM agents"}'}}]},
    {"role": "tool", "content": "agent_id,name\nbot-1,MyBot\nbot-2,TestBot", "tool_call_id": "tc99"},
  )
  cc_summary_has_tool_name = "sqlite_query" in cc_pair_summary
  cc_summary_has_result_preview = "agent_id" in cc_pair_summary
  record_test_outcome(
    "context_collapse_pair_summary_format",
    cc_summary_has_tool_name and cc_summary_has_result_preview,
    f"tool_name={cc_summary_has_tool_name}, result_preview={cc_summary_has_result_preview}, summary={cc_pair_summary[:100]}"
  )

  # ── Phase 3.5: Auto compact tests ──
  ac_format_test = _format_messages_for_compaction_prompt([
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"},
    {"role": "tool", "content": "result data"},
  ])
  ac_format_has_roles = "[USER]:" in ac_format_test and "[ASSISTANT]:" in ac_format_test and "[TOOL]:" in ac_format_test
  record_test_outcome(
    "auto_compact_format_messages_includes_roles",
    ac_format_has_roles,
    f"has_roles={ac_format_has_roles}"
  )

  ac_short_history = [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello!"},
  ]
  ac_short_result, ac_short_tokens, ac_short_performed = _run_auto_compact(
    f"_ac_test_{test_run_id}", "test_session",
    {"compaction_provider": "mlx", "compaction_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4"},
    ac_short_history, 10, 5000,
  )
  record_test_outcome(
    "auto_compact_skips_when_too_few_messages",
    not ac_short_performed and len(ac_short_result) == len(ac_short_history),
    f"performed={ac_short_performed}, count={len(ac_short_result)}"
  )

  ac_boundary_test_agent_id = f"_ac_boundary_test_{test_run_id}"
  ac_boundary_test_session_id = "ac_boundary_session"
  ac_boundary_now = _iso_now()
  _call_sqlite(
    """INSERT INTO compaction_boundaries
    (agent_id, session_id, summary_text, messages_compacted_count, tokens_before, tokens_after, last_compacted_entry_id, compacted_at)
    VALUES (:agent_id, :session_id, :summary_text, :messages_compacted_count, :tokens_before, :tokens_after, :last_compacted_entry_id, :compacted_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": ac_boundary_test_agent_id,
      "session_id": ac_boundary_test_session_id,
      "summary_text": "Test summary of previous conversation.",
      "messages_compacted_count": 15,
      "tokens_before": 5000,
      "tokens_after": 200,
      "last_compacted_entry_id": 12345,
      "compacted_at": ac_boundary_now,
    },
  )
  ac_boundary_lookup = _get_latest_compaction_boundary_entry_id(ac_boundary_test_agent_id, ac_boundary_test_session_id)
  record_test_outcome(
    "auto_compact_boundary_written_and_retrievable",
    ac_boundary_lookup == 12345,
    f"boundary_entry_id={ac_boundary_lookup} (expected 12345)"
  )

  ac_template_check = AUTO_COMPACT_SUMMARY_PROMPT_TEMPLATE
  ac_template_has_placeholders = "{conversation_text}" in ac_template_check
  ac_template_has_preservation_rules = "File names" in ac_template_check and "error messages" in ac_template_check
  record_test_outcome(
    "auto_compact_prompt_template_structure",
    ac_template_has_placeholders and ac_template_has_preservation_rules,
    f"placeholder={ac_template_has_placeholders}, rules={ac_template_has_preservation_rules}"
  )

  # ── Phase 3.6: Emergency compact tests ──
  ec_detect_true = _detect_context_too_long_error("Error: context_length_exceeded: max tokens is 4096")
  ec_detect_true2 = _detect_context_too_long_error("prompt is too long (12000 tokens, max 8192)")
  ec_detect_false = _detect_context_too_long_error("Connection refused: server unavailable")
  record_test_outcome(
    "emergency_detect_context_too_long_patterns",
    ec_detect_true and ec_detect_true2 and not ec_detect_false,
    f"ctx_exceeded={ec_detect_true}, prompt_too_long={ec_detect_true2}, conn_refused={ec_detect_false}"
  )

  ec_messages = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Old question"},
    {"role": "assistant", "content": "Calling tool"},
    {"role": "tool", "content": "HUGE RESULT " * 500, "tool_call_id": "tc1"},
    {"role": "assistant", "content": "Another call"},
    {"role": "tool", "content": "ANOTHER HUGE RESULT " * 500, "tool_call_id": "tc2"},
    {"role": "user", "content": "Recent question"},
    {"role": "assistant", "content": "Recent answer"},
    {"role": "tool", "content": "Recent result", "tool_call_id": "tc3"},
    {"role": "user", "content": "Latest question"},
  ]

  ec_stripped = _emergency_strip_old_tool_results(ec_messages, keep_recent_turns=2)
  ec_old_tool_stripped = "emergency compact" in ec_stripped[3].get("content", "")
  ec_recent_tool_preserved = ec_stripped[-2].get("content") == "Recent result"
  record_test_outcome(
    "emergency_strip_old_tool_results",
    ec_old_tool_stripped and ec_recent_tool_preserved,
    f"old_stripped={ec_old_tool_stripped}, recent_preserved={ec_recent_tool_preserved}"
  )

  ec_result_1, ec_count_1, ec_tripped_1 = _emergency_compact_escalation(
    f"_ec_test_{test_run_id}", "test_session", f"ec_run_{test_run_id}",
    {"compaction_provider": "mlx", "compaction_model": "test"},
    ec_messages, 0,
  )
  record_test_outcome(
    "emergency_escalation_first_attempt_succeeds",
    not ec_tripped_1 and ec_count_1 == 1,
    f"tripped={ec_tripped_1}, count={ec_count_1}"
  )

  ec_result_cb, ec_count_cb, ec_tripped_cb = _emergency_compact_escalation(
    f"_ec_test_{test_run_id}", "test_session", f"ec_run_{test_run_id}",
    {"compaction_provider": "mlx", "compaction_model": "test"},
    ec_messages, EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE - 1,
  )
  record_test_outcome(
    "emergency_circuit_breaker_trips_at_max",
    ec_tripped_cb and ec_count_cb == EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE,
    f"tripped={ec_tripped_cb}, count={ec_count_cb}, max={EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE}"
  )

  ec_result_under, ec_count_under, ec_tripped_under = _emergency_compact_escalation(
    f"_ec_test_{test_run_id}", "test_session", f"ec_run_{test_run_id}",
    {"compaction_provider": "mlx", "compaction_model": "test"},
    ec_messages, EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE - 2,
  )
  record_test_outcome(
    "emergency_escalation_under_breaker_limit",
    not ec_tripped_under,
    f"tripped={ec_tripped_under}, count={ec_count_under}"
  )

  # ── Phase 3.8: compact_context operation tests ──
  cco_agent_id = f"_cco_test_{test_run_id}"
  cco_session_id = f"cco_session_{test_run_id}"

  _save_transcript_entry(cco_agent_id, cco_session_id, "user", "Tell me about Python")
  _save_transcript_entry(cco_agent_id, cco_session_id, "assistant", "Python is a programming language created by Guido van Rossum.")
  _save_transcript_entry(cco_agent_id, cco_session_id, "user", "What about its type system?")
  _save_transcript_entry(cco_agent_id, cco_session_id, "assistant", "Python uses dynamic typing with optional type hints via the typing module.")
  _save_transcript_entry(cco_agent_id, cco_session_id, "user", "Give me an example")
  _save_transcript_entry(cco_agent_id, cco_session_id, "assistant", "def greet(name: str) -> str: return f'Hello {name}'")

  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, llm_provider, llm_model,
    compaction_provider, compaction_model, context_mode, read_tools_allowed, write_tools_allowed,
    tools_requiring_approval, max_tool_rounds_per_run, created_at, updated_at)
    VALUES (:agent_id, :display_name, :system_prompt, :llm_provider, :llm_model,
    :compaction_provider, :compaction_model, :context_mode, :read_tools_allowed, :write_tools_allowed,
    :tools_requiring_approval, :max_tool_rounds_per_run, :created_at, :updated_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": cco_agent_id,
      "display_name": f"CCO Test {test_run_id}",
      "system_prompt": "Test.",
      "llm_provider": "mlx",
      "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
      "compaction_provider": "mlx",
      "compaction_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
      "context_mode": "raw",
      "read_tools_allowed": '["*"]',
      "write_tools_allowed": '[]',
      "tools_requiring_approval": '[]',
      "max_tool_rounds_per_run": 10,
      "created_at": _iso_now(),
      "updated_at": _iso_now(),
    },
  )

  cco_result = handle_compact_context({
    "agent_id": cco_agent_id,
    "session_id": cco_session_id,
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  cco_response = json.loads(cco_result["content"][0]["text"])
  cco_not_error = not cco_result.get("isError", False)
  cco_has_agent_id = cco_response.get("agent_id") == cco_agent_id
  cco_has_session_id = cco_response.get("session_id") == cco_session_id
  record_test_outcome(
    "compact_context_operation_runs_successfully",
    cco_not_error and cco_has_agent_id and cco_has_session_id,
    f"error={not cco_not_error}, agent={cco_has_agent_id}, session={cco_has_session_id}, compacted={cco_response.get('compacted')}"
  )

  cco_nonexistent_result = handle_compact_context({
    "agent_id": "nonexistent_agent_zzz",
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  cco_nonexistent_is_error = cco_nonexistent_result.get("isError", False)
  record_test_outcome(
    "compact_context_rejects_nonexistent_agent",
    cco_nonexistent_is_error,
    f"is_error={cco_nonexistent_is_error}"
  )

  cco_empty_agent_id = f"_cco_empty_{test_run_id}"
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, llm_provider, llm_model,
    context_mode, read_tools_allowed, write_tools_allowed, tools_requiring_approval,
    max_tool_rounds_per_run, created_at, updated_at)
    VALUES (:agent_id, :display_name, :system_prompt, :llm_provider, :llm_model,
    :context_mode, :read_tools_allowed, :write_tools_allowed, :tools_requiring_approval,
    :max_tool_rounds_per_run, :created_at, :updated_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": cco_empty_agent_id,
      "display_name": "Empty Agent",
      "system_prompt": "Test.",
      "llm_provider": "mlx",
      "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
      "context_mode": "raw",
      "read_tools_allowed": '["*"]',
      "write_tools_allowed": '[]',
      "tools_requiring_approval": '[]',
      "max_tool_rounds_per_run": 10,
      "created_at": _iso_now(),
      "updated_at": _iso_now(),
    },
  )
  cco_empty_result = handle_compact_context({
    "agent_id": cco_empty_agent_id,
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  cco_empty_response_text = cco_empty_result["content"][0]["text"]
  cco_empty_is_error = cco_empty_result.get("isError", False)
  cco_empty_no_sessions = "No sessions" in cco_empty_response_text or "too_few_messages" in cco_empty_response_text
  record_test_outcome(
    "compact_context_handles_empty_agent_gracefully",
    cco_empty_is_error or cco_empty_no_sessions,
    f"error={cco_empty_is_error}, no_sessions={cco_empty_no_sessions}"
  )

  _call_sqlite(
    "DELETE FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": cco_agent_id},
  )
  _call_sqlite(
    "DELETE FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": cco_empty_agent_id},
  )

  # ══════════════════════════════════════════════════════════════
  # Phase 4: Memory System Tests
  # ══════════════════════════════════════════════════════════════

  test_results.append("")
  test_results.append("── Phase 4: Memory System ──")

  p4_agent_id = f"_p4_mem_{test_run_id}"
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, working_context,
    llm_provider, llm_model, context_mode, read_tools_allowed, write_tools_allowed,
    tools_requiring_approval, max_tool_rounds_per_run, created_at, updated_at)
    VALUES (:agent_id, :display_name, :system_prompt, :working_context,
    :llm_provider, :llm_model, :context_mode, :read_tools_allowed, :write_tools_allowed,
    :tools_requiring_approval, :max_tool_rounds_per_run, :created_at, :updated_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": p4_agent_id, "display_name": "Phase 4 Test Agent",
      "system_prompt": "Test agent for Phase 4 memory tests.",
      "working_context": "Initial working context for Phase 4 tests.",
      "llm_provider": "mlx", "llm_model": "cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",
      "context_mode": "raw", "read_tools_allowed": '["*"]',
      "write_tools_allowed": '[]', "tools_requiring_approval": '[]',
      "max_tool_rounds_per_run": 10,
      "created_at": _iso_now(), "updated_at": _iso_now(),
    },
  )

  # Test 99: insert archival memory with auto-embedding
  insert_ok, mem_id_1, insert_err = _insert_archival_memory(
    p4_agent_id, "User prefers dark mode in all applications", "preference",
    importance_score=0.8, confidence_score=0.9, source_run_id=test_run_id,
  )
  record_test_outcome(
    "archival_memory_insert_with_embedding",
    insert_ok and mem_id_1 is not None and mem_id_1.startswith("mem-"),
    f"ok={insert_ok}, id={mem_id_1}, err={insert_err}"
  )

  # Test 100: insert a second memory for search testing
  insert_ok_2, mem_id_2, insert_err_2 = _insert_archival_memory(
    p4_agent_id, "Pizza is the user's favorite food", "fact",
    importance_score=0.6, confidence_score=0.85,
  )
  record_test_outcome(
    "archival_memory_insert_second_entry",
    insert_ok_2 and mem_id_2 is not None,
    f"ok={insert_ok_2}, id={mem_id_2}"
  )

  # Test 101: insert a third memory on a different topic
  insert_ok_3, mem_id_3, _ = _insert_archival_memory(
    p4_agent_id, "The project uses Python 3.11 with SQLite for persistence", "project_knowledge",
  )
  record_test_outcome(
    "archival_memory_insert_third_entry",
    insert_ok_3 and mem_id_3 is not None,
    f"ok={insert_ok_3}, id={mem_id_3}"
  )

  # Test 102: semantic search — "food preferences" should match "pizza" memory
  search_ok, search_results, search_err = _search_archival_memory(
    p4_agent_id, "what food does the user like?", limit=5,
  )
  search_found_pizza = any("pizza" in r.get("content", "").lower() or "Pizza" in r.get("content", "") for r in search_results) if search_results else False
  record_test_outcome(
    "archival_memory_semantic_search_relevance",
    search_ok and search_found_pizza and len(search_results) >= 1,
    f"ok={search_ok}, found_pizza={search_found_pizza}, count={len(search_results)}, err={search_err}"
  )

  # Test 103: search results are ranked (first result should be most relevant)
  if search_ok and len(search_results) >= 2:
    first_distance = float(search_results[0].get("distance", 1.0))
    second_distance = float(search_results[1].get("distance", 1.0))
    search_results_are_ranked_by_distance = first_distance <= second_distance
  else:
    search_results_are_ranked_by_distance = search_ok
  record_test_outcome(
    "archival_memory_search_results_ordered_by_similarity",
    search_results_are_ranked_by_distance,
    f"first_dist={search_results[0].get('distance') if search_results else 'N/A'}, second_dist={search_results[1].get('distance') if len(search_results) > 1 else 'N/A'}"
  )

  # Test 104: access_count updated after search
  if mem_id_2:
    get_ok, mem_after_search, _ = _get_archival_memory(mem_id_2)
    access_count_updated = get_ok and mem_after_search is not None and int(mem_after_search.get("access_count", 0)) >= 1
  else:
    access_count_updated = False
  record_test_outcome(
    "archival_memory_access_count_incremented_on_search",
    access_count_updated,
    f"access_count={mem_after_search.get('access_count') if mem_after_search else 'N/A'}"
  )

  # Test 105: update archival memory content (triggers re-embedding)
  if mem_id_1:
    update_ok, update_err = _update_archival_memory(mem_id_1, content="User prefers light mode for daytime, dark mode at night")
    get_ok, updated_mem, _ = _get_archival_memory(mem_id_1)
    content_updated = get_ok and updated_mem is not None and "light mode" in updated_mem.get("content", "")
  else:
    update_ok = False
    content_updated = False
  record_test_outcome(
    "archival_memory_update_content_and_reembed",
    update_ok and content_updated,
    f"update_ok={update_ok}, content_verified={content_updated}"
  )

  # Test 106: delete archival memory
  if mem_id_3:
    delete_ok, delete_err = _delete_archival_memory(mem_id_3)
    get_ok, deleted_mem, _ = _get_archival_memory(mem_id_3)
    deletion_verified = delete_ok and (deleted_mem is None)
  else:
    delete_ok = False
    deletion_verified = False
  record_test_outcome(
    "archival_memory_delete_and_verify_gone",
    delete_ok and deletion_verified,
    f"delete_ok={delete_ok}, gone={deletion_verified}"
  )

  # Test 107: get_archival_memory retrieves correct record
  if mem_id_1:
    get_ok, got_mem, _ = _get_archival_memory(mem_id_1)
    get_content_matches = got_mem is not None and "light mode" in got_mem.get("content", "")
    get_agent_matches = got_mem is not None and got_mem.get("agent_id") == p4_agent_id
  else:
    get_ok = False
    get_content_matches = False
    get_agent_matches = False
  record_test_outcome(
    "archival_memory_get_single_record",
    get_ok and get_content_matches and get_agent_matches,
    f"ok={get_ok}, content_match={get_content_matches}, agent_match={get_agent_matches}"
  )

  # Test 108: core_memory_update pseudo-tool handler
  cmu_result = _handle_pseudo_tool_core_memory_update(p4_agent_id, {
    "section": "working_context",
    "content": "Updated by Phase 4 test: user likes cats and pizza."
  })
  cmu_not_error = not cmu_result.get("isError", True)
  cmu_agent_check = _extract_agent_config_as_dict(p4_agent_id)
  cmu_persisted = cmu_agent_check is not None and "cats and pizza" in cmu_agent_check.get("working_context", "")
  record_test_outcome(
    "core_memory_update_pseudo_tool_persists_working_context",
    cmu_not_error and cmu_persisted,
    f"not_error={cmu_not_error}, persisted={cmu_persisted}, wc={cmu_agent_check.get('working_context', '')[:60] if cmu_agent_check else 'None'}"
  )

  # Test 109: core_memory_update rejects invalid section
  cmu_bad_result = _handle_pseudo_tool_core_memory_update(p4_agent_id, {
    "section": "system_prompt",
    "content": "should not work"
  })
  cmu_bad_rejected = cmu_bad_result.get("isError", False)
  record_test_outcome(
    "core_memory_update_rejects_invalid_section",
    cmu_bad_rejected,
    f"rejected={cmu_bad_rejected}"
  )

  # Test 110: archival_memory_insert pseudo-tool handler
  ami_result = _handle_pseudo_tool_archival_memory_insert(p4_agent_id, {
    "content": "Agent was told that the user's timezone is NZST (UTC+12)",
    "memory_type": "fact",
    "importance": 0.7,
  }, source_run_id=test_run_id)
  ami_not_error = not ami_result.get("isError", True)
  ami_has_id = "id=" in ami_result.get("text", "")
  record_test_outcome(
    "archival_memory_insert_pseudo_tool_stores_memory",
    ami_not_error and ami_has_id,
    f"not_error={ami_not_error}, has_id={ami_has_id}"
  )

  # Test 111: archival_memory_search pseudo-tool handler
  ams_result = _handle_pseudo_tool_archival_memory_search(p4_agent_id, {
    "query": "user timezone",
    "count": 5,
  })
  ams_not_error = not ams_result.get("isError", True)
  ams_found_timezone = "NZST" in ams_result.get("text", "") or "timezone" in ams_result.get("text", "").lower()
  record_test_outcome(
    "archival_memory_search_pseudo_tool_finds_relevant",
    ams_not_error and ams_found_timezone,
    f"not_error={ams_not_error}, found_tz={ams_found_timezone}"
  )

  # Test 112: recall_memory_search pseudo-tool handler
  _save_transcript_entry(p4_agent_id, f"session_{test_run_id}", "user", "Please update the database schema for memory entries")
  _save_transcript_entry(p4_agent_id, f"session_{test_run_id}", "assistant", "I have updated the schema to include the new memory_entries table")
  rms_result = _handle_pseudo_tool_recall_memory_search(p4_agent_id, {
    "query": "schema",
    "count": 5,
  })
  rms_not_error = not rms_result.get("isError", True)
  rms_found_schema = "schema" in rms_result.get("text", "").lower()
  record_test_outcome(
    "recall_memory_search_finds_transcript_entries",
    rms_not_error and rms_found_schema,
    f"not_error={rms_not_error}, found_schema={rms_found_schema}"
  )

  # Test 113: schedule_reminder pseudo-tool with absolute time
  from datetime import datetime, timezone, timedelta
  future_time = datetime.now(timezone.utc) + timedelta(hours=2)
  sr_result = _handle_pseudo_tool_schedule_reminder(p4_agent_id, {
    "when": future_time.isoformat(),
    "message": "Follow up with Sarah about the proposal",
    "priority": "normal",
  })
  sr_not_error = not sr_result.get("isError", True)
  sr_has_source_id = "source_id=" in sr_result.get("text", "")
  record_test_outcome(
    "schedule_reminder_with_absolute_time_creates_event_source",
    sr_not_error and sr_has_source_id,
    f"not_error={sr_not_error}, has_source={sr_has_source_id}"
  )

  # Test 114: schedule_reminder with relative time
  sr_rel_result = _handle_pseudo_tool_schedule_reminder(p4_agent_id, {
    "when": "in 3 hours",
    "message": "Check build status",
  })
  sr_rel_not_error = not sr_rel_result.get("isError", True)
  sr_rel_has_source = "source_id=" in sr_rel_result.get("text", "")
  record_test_outcome(
    "schedule_reminder_with_relative_time_parses_correctly",
    sr_rel_not_error and sr_rel_has_source,
    f"not_error={sr_rel_not_error}, has_source={sr_rel_has_source}"
  )

  # Test 115: _parse_reminder_time_specification
  parsed_iso = _parse_reminder_time_specification("2026-04-15T09:00:00+12:00")
  parsed_relative_hours = _parse_reminder_time_specification("in 2 hours")
  parsed_relative_minutes = _parse_reminder_time_specification("in 45 minutes")
  parsed_tomorrow = _parse_reminder_time_specification("tomorrow at 09:00")
  parsed_bad = _parse_reminder_time_specification("not a real time")

  now_utc = datetime.now(timezone.utc)
  iso_parsed_correctly = parsed_iso is not None and parsed_iso.hour == 9
  relative_hours_correct = parsed_relative_hours is not None and abs((parsed_relative_hours - now_utc).total_seconds() - 7200) < 60
  relative_minutes_correct = parsed_relative_minutes is not None and abs((parsed_relative_minutes - now_utc).total_seconds() - 2700) < 60
  tomorrow_correct = parsed_tomorrow is not None and (parsed_tomorrow - now_utc).total_seconds() > 0
  bad_returns_none = parsed_bad is None
  record_test_outcome(
    "parse_reminder_time_iso_and_relative_and_tomorrow",
    iso_parsed_correctly and relative_hours_correct and relative_minutes_correct and tomorrow_correct and bad_returns_none,
    f"iso={iso_parsed_correctly}, hours={relative_hours_correct}, minutes={relative_minutes_correct}, tomorrow={tomorrow_correct}, bad=None:{bad_returns_none}"
  )

  # Test 116: _build_tool_definitions_for_agent includes pseudo-tools
  p4_config = _extract_agent_config_as_dict(p4_agent_id) or {}
  tool_defs = _build_tool_definitions_for_agent(p4_config)
  pseudo_names_in_defs = {td.get("function", {}).get("name") for td in tool_defs}
  has_all_pseudo_tools = PSEUDO_TOOL_NAMES.issubset(pseudo_names_in_defs)
  record_test_outcome(
    "tool_definitions_include_all_pseudo_tools",
    has_all_pseudo_tools and len(tool_defs) >= 7,
    f"count={len(tool_defs)}, names={sorted(pseudo_names_in_defs)}, expected={sorted(PSEUDO_TOOL_NAMES)}"
  )

  # Test 117: pseudo-tool dispatch routes correctly
  dispatch_ok, dispatch_text = _dispatch_pseudo_tool_call(
    p4_agent_id, "archival_memory_insert",
    {"content": "Dispatch test memory", "memory_type": "fact"},
    test_run_id,
  )
  dispatch_bad_ok, dispatch_bad_text = _dispatch_pseudo_tool_call(
    p4_agent_id, "nonexistent_tool", {}, test_run_id,
  )
  record_test_outcome(
    "pseudo_tool_dispatch_routes_and_rejects_unknown",
    dispatch_ok and not dispatch_bad_ok,
    f"known_ok={dispatch_ok}, unknown_rejected={not dispatch_bad_ok}"
  )

  # Test 118: MCP get_memory operation
  gm_result = handle_get_memory({
    "agent_id": p4_agent_id,
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  gm_not_error = not gm_result.get("isError", False)
  gm_text = gm_result.get("content", [{}])[0].get("text", "{}")
  try:
    gm_data = json.loads(gm_text)
    gm_has_memories = len(gm_data.get("memories", [])) >= 2
  except (json.JSONDecodeError, TypeError):
    gm_has_memories = False
  record_test_outcome(
    "mcp_get_memory_lists_agent_memories",
    gm_not_error and gm_has_memories,
    f"not_error={gm_not_error}, has_memories={gm_has_memories}"
  )

  # Test 119: MCP set_memory inserts new
  sm_result = handle_set_memory({
    "agent_id": p4_agent_id,
    "content": "MCP set_memory test entry",
    "memory_type": "fact",
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  sm_not_error = not sm_result.get("isError", False)
  sm_text = sm_result.get("content", [{}])[0].get("text", "{}")
  try:
    sm_data = json.loads(sm_text)
    sm_action = sm_data.get("action") == "created"
    sm_has_id = "memory_id" in sm_data
  except (json.JSONDecodeError, TypeError):
    sm_action = False
    sm_has_id = False
  record_test_outcome(
    "mcp_set_memory_creates_new_entry",
    sm_not_error and sm_action and sm_has_id,
    f"not_error={sm_not_error}, action=created:{sm_action}, has_id={sm_has_id}"
  )

  # Test 120: MCP delete_memory
  if sm_has_id:
    dm_memory_id = sm_data.get("memory_id")
    dm_result = handle_delete_memory({
      "memory_id": dm_memory_id,
      "tool_unlock_token": TOOL_UNLOCK_TOKEN,
    })
    dm_not_error = not dm_result.get("isError", False)
    dm_verify_ok, dm_verify_mem, _ = _get_archival_memory(dm_memory_id)
    dm_gone = dm_verify_ok and dm_verify_mem is None
  else:
    dm_not_error = False
    dm_gone = False
  record_test_outcome(
    "mcp_delete_memory_removes_entry",
    dm_not_error and dm_gone,
    f"not_error={dm_not_error}, gone={dm_gone}"
  )

  # Test 121: memory prefetch in context assembly
  p4_assembly_config = _extract_agent_config_as_dict(p4_agent_id)
  if p4_assembly_config:
    p4_messages, p4_budget = _assemble_context_for_agent_run(
      p4_assembly_config, f"session_{test_run_id}", "What food does the user like?",
    )
    p4_has_archival_block = any("<agent-memory" in m.get("content", "") for m in p4_messages)
    p4_entries_injected = p4_budget.get("archival_memory_entries_injected", 0)
  else:
    p4_has_archival_block = False
    p4_entries_injected = 0
  record_test_outcome(
    "memory_prefetch_injects_archival_memories_into_context",
    p4_has_archival_block and p4_entries_injected > 0,
    f"has_block={p4_has_archival_block}, entries={p4_entries_injected}"
  )

  # Test 122: archival memory insert rejects invalid memory_type
  bad_type_ok, _, bad_type_err = _insert_archival_memory(
    p4_agent_id, "test", "invalid_type_xyz",
  )
  record_test_outcome(
    "archival_memory_insert_rejects_invalid_type",
    not bad_type_ok and "Invalid memory_type" in (bad_type_err or ""),
    f"rejected={not bad_type_ok}, err={bad_type_err}"
  )

  # Cleanup Phase 4 test data
  _call_sqlite(
    "DELETE FROM memory_entries WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p4_agent_id},
  )
  _call_sqlite(
    "DELETE FROM transcript_entries WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p4_agent_id},
  )
  _call_sqlite(
    "DELETE FROM event_sources WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p4_agent_id},
  )
  _call_sqlite(
    "DELETE FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p4_agent_id},
  )

  _stop_all_agent_mailboxes()

  # ══════════════════════════════════════════════════════════════
  # Phase 5 Tests: Policy Guard, Circuit Breakers, LLM Fallback,
  #                Approval Flow, DLQ Management, Reflection
  # ══════════════════════════════════════════════════════════════

  test_results.append("")
  test_results.append("── Phase 5: Safety + Reflection ──")

  # Create a test agent for Phase 5 with specific permissions
  p5_create_result = handle_create_agent({
    "display_name": "Phase5 Test Agent",
    "system_prompt": "Phase 5 test agent",
    "read_tools_allowed": '["*"]',
    "write_tools_allowed": '["sqlite"]',
    "tools_requiring_approval": '["shell"]',
    "max_tool_calls_per_hour": 100,
    "reflection_enabled": 1,
    "reflection_idle_timeout_minutes": 30,
  })
  p5_agent_id = None
  try:
    p5_data = json.loads(_extract_text_from_mcp_response(p5_create_result))
    p5_agent_id = p5_data.get("agent_id")
  except (json.JSONDecodeError, TypeError, KeyError):
    pass

  record_test_outcome(
    "phase5_test_agent_created",
    p5_agent_id is not None,
    f"agent_id={p5_agent_id}"
  )

  if p5_agent_id:
    p5_agent_config = _extract_agent_config_as_dict(p5_agent_id)
    p5_run_id = _generate_run_id()

    # Test 123: _classify_tool_safety_category — sqlite is write_allowed
    cat_sqlite = _classify_tool_safety_category("sqlite", p5_agent_config)
    record_test_outcome(
      "policy_guard_classify_sqlite_write_allowed",
      cat_sqlite == "write_allowed",
      f"category={cat_sqlite}"
    )

    # Test 124: _classify_tool_safety_category — shell requires approval
    cat_shell = _classify_tool_safety_category("shell", p5_agent_config)
    record_test_outcome(
      "policy_guard_classify_shell_requires_approval",
      cat_shell == "requires_approval",
      f"category={cat_shell}"
    )

    # Test 125: _classify_tool_safety_category — web_search is read_allowed (wildcard)
    cat_websearch = _classify_tool_safety_category("web_search", p5_agent_config)
    record_test_outcome(
      "policy_guard_classify_web_search_read_allowed",
      cat_websearch == "read_allowed",
      f"category={cat_websearch}"
    )

    # Test 126: _classify_tool_safety_category — file_write not in any list → denied
    p5_restricted_config = dict(p5_agent_config)
    p5_restricted_config["read_tools_allowed"] = '["web_search"]'
    p5_restricted_config["write_tools_allowed"] = '["sqlite"]'
    p5_restricted_config["tools_requiring_approval"] = '[]'
    cat_filewrite = _classify_tool_safety_category("file_write", p5_restricted_config)
    record_test_outcome(
      "policy_guard_classify_file_write_denied",
      cat_filewrite == "denied",
      f"category={cat_filewrite}"
    )

    # Test 127: _check_tool_authorization — sqlite authorized
    auth_ok, auth_reason = _check_tool_authorization("sqlite", p5_agent_config)
    record_test_outcome(
      "policy_guard_auth_sqlite_authorized",
      auth_ok,
      f"authorized={auth_ok}, reason={auth_reason}"
    )

    # Test 128: _check_tool_authorization — file_write denied (not in write list, no approval)
    auth_filewrite_ok, auth_filewrite_reason = _check_tool_authorization("file_write", p5_restricted_config)
    record_test_outcome(
      "policy_guard_auth_file_write_denied",
      not auth_filewrite_ok,
      f"denied={not auth_filewrite_ok}, reason={auth_filewrite_reason}"
    )

    # Test 129: _execute_policy_guard_check — sqlite allowed
    pg_ok, pg_reason, pg_needs_approval = _execute_policy_guard_check(
      p5_agent_id, "sqlite", {"sql": "SELECT 1"}, p5_agent_config, p5_run_id,
    )
    record_test_outcome(
      "policy_guard_full_check_sqlite_allowed",
      pg_ok and not pg_needs_approval,
      f"allowed={pg_ok}, needs_approval={pg_needs_approval}"
    )

    # Test 130: _execute_policy_guard_check — shell requires approval
    pg_shell_ok, pg_shell_reason, pg_shell_needs_approval = _execute_policy_guard_check(
      p5_agent_id, "shell", {"command": "ls"}, p5_agent_config, p5_run_id,
    )
    record_test_outcome(
      "policy_guard_full_check_shell_needs_approval",
      not pg_shell_ok and pg_shell_needs_approval,
      f"allowed={pg_shell_ok}, needs_approval={pg_shell_needs_approval}"
    )

    # Test 131: policy_checked session log entry written
    p5_log_entry_rows = _parse_rows_from_mcp_query_response(
      _query_session_log(agent_id=p5_agent_id, entry_type="policy_checked", limit=10)
    )
    record_test_outcome(
      "policy_guard_session_log_entry_written",
      len(p5_log_entry_rows) >= 2,
      f"policy_checked_entries={len(p5_log_entry_rows)}"
    )

    # Test 132: YOLO mode — all tools allowed (wildcard in both lists)
    p5_yolo_config = dict(p5_agent_config)
    p5_yolo_config["read_tools_allowed"] = '["*"]'
    p5_yolo_config["write_tools_allowed"] = '["*"]'
    p5_yolo_config["tools_requiring_approval"] = '[]'
    yolo_ok, yolo_reason, yolo_approval = _execute_policy_guard_check(
      p5_agent_id, "server_control", {"command": "restart"}, p5_yolo_config, p5_run_id,
    )
    record_test_outcome(
      "policy_guard_yolo_mode_allows_everything",
      yolo_ok and not yolo_approval,
      f"allowed={yolo_ok}, needs_approval={yolo_approval}"
    )

    # ── Circuit Breaker Tests ──

    p5_cb_run_id = _generate_run_id()
    _clear_circuit_breaker_tracker_for_run(p5_agent_id, p5_cb_run_id)

    # Test 133: 2 failures → tool still available
    _record_tool_failure_for_circuit_breaker(p5_agent_id, p5_cb_run_id, "flaky_tool")
    _record_tool_failure_for_circuit_breaker(p5_agent_id, p5_cb_run_id, "flaky_tool")
    cb_available_2, _ = _check_tool_circuit_breaker(p5_agent_id, "flaky_tool", p5_cb_run_id)
    record_test_outcome(
      "circuit_breaker_2_failures_still_available",
      cb_available_2,
      f"available={cb_available_2}"
    )

    # Test 134: 3 failures → circuit trips
    _record_tool_failure_for_circuit_breaker(p5_agent_id, p5_cb_run_id, "flaky_tool")
    cb_available_3, cb_reason_3 = _check_tool_circuit_breaker(p5_agent_id, "flaky_tool", p5_cb_run_id)
    record_test_outcome(
      "circuit_breaker_3_failures_trips",
      not cb_available_3 and "circuit breaker tripped" in cb_reason_3.lower(),
      f"available={cb_available_3}, reason={cb_reason_3[:80]}"
    )

    # Test 135: circuit_breaker_tripped log entry written
    cb_log_entry_rows = _parse_rows_from_mcp_query_response(
      _query_session_log(agent_id=p5_agent_id, entry_type="circuit_breaker_tripped", limit=5)
    )
    record_test_outcome(
      "circuit_breaker_tripped_log_entry_written",
      len(cb_log_entry_rows) >= 1,
      f"cb_tripped_entries={len(cb_log_entry_rows)}"
    )

    # Test 136: different tool same run unaffected
    cb_other_available, _ = _check_tool_circuit_breaker(p5_agent_id, "other_tool", p5_cb_run_id)
    record_test_outcome(
      "circuit_breaker_other_tool_unaffected",
      cb_other_available,
      f"other_tool_available={cb_other_available}"
    )

    # Test 137: success resets failure counter
    p5_cb_run_id_2 = _generate_run_id()
    _record_tool_failure_for_circuit_breaker(p5_agent_id, p5_cb_run_id_2, "reset_tool")
    _record_tool_failure_for_circuit_breaker(p5_agent_id, p5_cb_run_id_2, "reset_tool")
    _record_tool_success_for_circuit_breaker(p5_agent_id, p5_cb_run_id_2, "reset_tool")
    cb_reset_available, _ = _check_tool_circuit_breaker(p5_agent_id, "reset_tool", p5_cb_run_id_2)
    record_test_outcome(
      "circuit_breaker_success_resets_counter",
      cb_reset_available,
      f"available_after_reset={cb_reset_available}"
    )

    # ── Rate Limit Tests ──

    # Test 138: rate limit check within limit
    rl_ok, rl_reason = _check_rate_limit(p5_agent_id, "sqlite", p5_agent_config)
    record_test_outcome(
      "rate_limit_within_limit",
      rl_ok,
      f"within_limit={rl_ok}"
    )

    # Test 139: rate limit check with very low limit
    p5_low_rate_config = dict(p5_agent_config)
    p5_low_rate_config["max_tool_calls_per_hour"] = 0
    rl_exceeded_ok, rl_exceeded_reason = _check_rate_limit(p5_agent_id, "sqlite", p5_low_rate_config)
    record_test_outcome(
      "rate_limit_zero_limit_allows",
      rl_exceeded_ok,
      f"zero_limit_allows={rl_exceeded_ok} (0 means no limit)"
    )

    # ── Approval Flow Tests ──

    # Test 140: handle_get_pending_approvals returns empty when nothing pending
    gpa_result = handle_get_pending_approvals({"agent_id": p5_agent_id})
    gpa_text = _extract_text_from_mcp_response(gpa_result)
    try:
      gpa_data = json.loads(gpa_text)
      gpa_count = gpa_data.get("pending_approval_count", -1)
    except (json.JSONDecodeError, TypeError):
      gpa_count = -1
    record_test_outcome(
      "get_pending_approvals_empty_when_none",
      gpa_count == 0,
      f"pending_count={gpa_count}"
    )

    # Test 141: approve_action returns error for nonexistent ID
    apr_nonexistent = handle_approve_action({"approval_request_id": "nonexistent-id-xyz"})
    record_test_outcome(
      "approve_action_rejects_nonexistent",
      apr_nonexistent.get("isError", False),
      f"isError={apr_nonexistent.get('isError')}"
    )

    # Test 142: deny_action returns error for nonexistent ID
    deny_nonexistent = handle_deny_action({"approval_request_id": "nonexistent-id-xyz", "reason": "test"})
    record_test_outcome(
      "deny_action_rejects_nonexistent",
      deny_nonexistent.get("isError", False),
      f"isError={deny_nonexistent.get('isError')}"
    )

    # Test 143: _update_last_active_channel_from_event stores channel
    _update_last_active_channel_from_event(p5_agent_id, {
      "channel_type": "telegram",
      "channel_config": {"chat_id": 12345},
      "operator_is_human": True,
    })
    stored_channel = _last_active_channel_per_agent.get(p5_agent_id)
    record_test_outcome(
      "last_active_channel_stored",
      stored_channel is not None and stored_channel.get("channel_type") == "telegram",
      f"channel_type={stored_channel.get('channel_type') if stored_channel else None}"
    )

    # ── DLQ Tests ──

    # Test 144: get_dlq returns empty initially
    dlq_result = handle_get_dlq({"agent_id": p5_agent_id, "status": "pending"})
    dlq_text = _extract_text_from_mcp_response(dlq_result)
    try:
      dlq_data = json.loads(dlq_text)
      dlq_entry_count = dlq_data.get("dlq_entry_count", -1)
    except (json.JSONDecodeError, TypeError):
      dlq_entry_count = -1
    record_test_outcome(
      "dlq_get_empty_initially",
      dlq_entry_count == 0,
      f"dlq_entry_count={dlq_entry_count}"
    )

    # Test 145: insert a DLQ entry manually, then get_dlq finds it
    _call_sqlite(
      """INSERT INTO dead_letter_queue
      (agent_id, original_event_json, failure_reason, failure_category, created_at)
      VALUES (:agent_id, :event_json, :reason, :category, :now)""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={
        "agent_id": p5_agent_id,
        "event_json": json.dumps({"message": "test event"}),
        "reason": "test failure",
        "category": "retryable",
        "now": _iso_now(),
      }
    )
    dlq_result2 = handle_get_dlq({"agent_id": p5_agent_id, "status": "pending"})
    dlq_text2 = _extract_text_from_mcp_response(dlq_result2)
    try:
      dlq_data2 = json.loads(dlq_text2)
      dlq_entries_found = dlq_data2.get("dlq_entries", [])
    except (json.JSONDecodeError, TypeError):
      dlq_entries_found = []
    record_test_outcome(
      "dlq_get_finds_inserted_entry",
      len(dlq_entries_found) >= 1,
      f"found={len(dlq_entries_found)}"
    )

    # Test 146: discard_dlq changes status
    if dlq_entries_found:
      test_dlq_id = dlq_entries_found[0].get("dlq_id")
      discard_result = handle_discard_dlq({"dlq_id": test_dlq_id})
      discard_text = _extract_text_from_mcp_response(discard_result)
      try:
        discard_data = json.loads(discard_text)
        discard_status = discard_data.get("status")
      except (json.JSONDecodeError, TypeError):
        discard_status = None
      record_test_outcome(
        "dlq_discard_changes_status",
        discard_status == "discarded",
        f"status={discard_status}"
      )
    else:
      record_test_outcome("dlq_discard_changes_status", False, "no DLQ entries to test with")

    # Test 147: get_dlq with status filter excludes discarded
    dlq_result3 = handle_get_dlq({"agent_id": p5_agent_id, "status": "pending"})
    dlq_text3 = _extract_text_from_mcp_response(dlq_result3)
    try:
      dlq_data3 = json.loads(dlq_text3)
      dlq_pending_after_discard = dlq_data3.get("dlq_entry_count", -1)
    except (json.JSONDecodeError, TypeError):
      dlq_pending_after_discard = -1
    record_test_outcome(
      "dlq_status_filter_excludes_discarded",
      dlq_pending_after_discard == 0,
      f"pending_after_discard={dlq_pending_after_discard}"
    )

    # Test 148: retry_dlq re-enqueues event
    _call_sqlite(
      """INSERT INTO dead_letter_queue
      (agent_id, original_event_json, failure_reason, failure_category, retry_count, max_retries, created_at)
      VALUES (:agent_id, :event_json, :reason, :category, 0, 3, :now)""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={
        "agent_id": p5_agent_id,
        "event_json": json.dumps({"message": "retry test event"}),
        "reason": "retry test",
        "category": "retryable",
        "now": _iso_now(),
      }
    )
    dlq_for_retry = handle_get_dlq({"agent_id": p5_agent_id, "status": "pending"})
    try:
      dlq_retry_data = json.loads(_extract_text_from_mcp_response(dlq_for_retry))
      dlq_retry_entries = dlq_retry_data.get("dlq_entries", [])
    except (json.JSONDecodeError, TypeError):
      dlq_retry_entries = []

    retry_test_ok = False
    if dlq_retry_entries:
      retry_dlq_id = dlq_retry_entries[0].get("dlq_id")
      retry_result = handle_retry_dlq({"dlq_id": retry_dlq_id})
      retry_text = _extract_text_from_mcp_response(retry_result)
      try:
        retry_data = json.loads(retry_text)
        retry_test_ok = retry_data.get("status") == "retried"
      except (json.JSONDecodeError, TypeError):
        pass
    record_test_outcome(
      "dlq_retry_re_enqueues_event",
      retry_test_ok,
      f"retried={retry_test_ok}"
    )

    # ── LLM Fallback Chain Tests ──

    # Test 149: _call_llm_with_fallback_chain with empty chain returns primary result
    p5_llm_params = {
      "operation": "chat",
      "provider": "nonexistent_provider_xyz",
      "model": "fake_model",
      "messages": [{"role": "user", "content": "test"}],
      "tool_unlock_token": "__auto__",
    }
    p5_empty_chain_config = dict(p5_agent_config)
    p5_empty_chain_config["model_fallback_chain"] = "[]"
    fallback_result = _call_llm_with_fallback_chain(p5_agent_id, p5_run_id, p5_empty_chain_config, p5_llm_params)
    record_test_outcome(
      "llm_fallback_empty_chain_returns_primary_error",
      fallback_result.get("isError", False),
      f"isError={fallback_result.get('isError')}"
    )

    # ── Reflection Tests ──

    # Test 150: handle_reflect_now with idle agent
    _call_sqlite(
      "UPDATE agents SET current_state = 'IDLE' WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id}
    )
    _append_session_log_entry(p5_agent_id, "test-run", "message_received", {"message": "test message for reflection"})
    _append_session_log_entry(p5_agent_id, "test-run", "llm_response", {"content_preview": "test response"})

    reflect_result = handle_reflect_now({"agent_id": p5_agent_id})
    reflect_text = _extract_text_from_mcp_response(reflect_result)
    try:
      reflect_data = json.loads(reflect_text)
      reflect_status = reflect_data.get("status")
    except (json.JSONDecodeError, TypeError):
      reflect_status = None
    record_test_outcome(
      "reflection_reflect_now_runs",
      reflect_status in ("completed", "skipped"),
      f"status={reflect_status}"
    )

    # Test 151: reflection_completed log entry written
    reflection_log_rows = _parse_rows_from_mcp_query_response(
      _query_session_log(agent_id=p5_agent_id, entry_type="reflection_completed", limit=5)
    )
    record_test_outcome(
      "reflection_completed_log_entry_written",
      len(reflection_log_rows) >= 1,
      f"reflection_completed_entries={len(reflection_log_rows)}"
    )

    # Test 152: reflection skips when no new activity
    _call_sqlite(
      "UPDATE agents SET current_state = 'IDLE' WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id}
    )
    reflect_result2 = handle_reflect_now({"agent_id": p5_agent_id})
    reflect_text2 = _extract_text_from_mcp_response(reflect_result2)
    try:
      reflect_data2 = json.loads(reflect_text2)
      reflect_status2 = reflect_data2.get("status")
    except (json.JSONDecodeError, TypeError):
      reflect_status2 = None
    record_test_outcome(
      "reflection_skips_no_new_activity",
      reflect_status2 == "skipped",
      f"status={reflect_status2}"
    )

    # Test 153: reflect_now rejects non-IDLE agent
    _call_sqlite(
      "UPDATE agents SET current_state = 'EXECUTING_TOOL' WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id}
    )
    reflect_busy_result = handle_reflect_now({"agent_id": p5_agent_id})
    record_test_outcome(
      "reflection_rejects_non_idle_agent",
      reflect_busy_result.get("isError", False),
      f"isError={reflect_busy_result.get('isError')}"
    )

    # Test 154: _check_and_fire_reflection_triggers with no eligible agents
    _call_sqlite(
      "UPDATE agents SET current_state = 'EXECUTING_TOOL' WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id}
    )
    triggers_fired = _check_and_fire_reflection_triggers()
    record_test_outcome(
      "reflection_trigger_check_no_eligible",
      True,
      f"triggers_fired={triggers_fired}"
    )

    # Cleanup Phase 5 test data
    _call_sqlite(
      "UPDATE agents SET current_state = 'IDLE' WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id}
    )
    _call_sqlite(
      "DELETE FROM session_log WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id},
    )
    _call_sqlite(
      "DELETE FROM memory_entries WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id},
    )
    _call_sqlite(
      "DELETE FROM dead_letter_queue WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id},
    )
    _call_sqlite(
      "DELETE FROM event_queue WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id},
    )
    _call_sqlite(
      "DELETE FROM agents WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": p5_agent_id},
    )

    _per_run_tool_failure_tracker.clear()
    _pending_approval_requests.clear()
    _last_active_channel_per_agent.clear()
    _reflection_idle_tracker.clear()

  # ═══════════════════════════════════════════════════════════════════════════
  # Phase 6.8: Observability operations tests
  # ═══════════════════════════════════════════════════════════════════════════

  p6_agent_id = f"_test_p6_{test_run_id}"
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at)
    VALUES (:agent_id, 'Phase 6 Test Agent', 'Test agent for Phase 6', 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p6_agent_id, "now": _iso_now()},
  )

  p6_run_id = f"run-p6test-{test_run_id}"

  _append_session_log_entry(p6_agent_id, p6_run_id, "run_started", {"test": "phase6"})
  _append_session_log_entry(p6_agent_id, p6_run_id, "llm_called", {"model": "test"})
  _append_session_log_entry(p6_agent_id, p6_run_id, "run_completed", {"result": "ok"})

  # Test 155: get_session_log with agent_id returns entries
  session_log_result = handle_get_session_log({"agent_id": p6_agent_id})
  session_log_text = _extract_text_from_mcp_response(session_log_result)
  try:
    session_log_data = json.loads(session_log_text)
    session_log_entry_count = session_log_data.get("entry_count", 0)
  except (json.JSONDecodeError, TypeError):
    session_log_entry_count = 0
  record_test_outcome(
    "get_session_log_returns_entries",
    session_log_entry_count >= 3,
    f"entry_count={session_log_entry_count}"
  )

  # Test 156: get_session_log with entry_type filter returns only matching
  session_log_filtered_result = handle_get_session_log({"agent_id": p6_agent_id, "entry_type": "llm_called"})
  session_log_filtered_text = _extract_text_from_mcp_response(session_log_filtered_result)
  try:
    session_log_filtered_data = json.loads(session_log_filtered_text)
    session_log_filtered_entries = session_log_filtered_data.get("entries", [])
    all_entries_match_filter_type = all(e.get("entry_type") == "llm_called" for e in session_log_filtered_entries)
  except (json.JSONDecodeError, TypeError):
    session_log_filtered_entries = []
    all_entries_match_filter_type = False
  record_test_outcome(
    "get_session_log_entry_type_filter",
    len(session_log_filtered_entries) >= 1 and all_entries_match_filter_type,
    f"filtered_count={len(session_log_filtered_entries)}, all_match={all_entries_match_filter_type}"
  )

  # Test 157: get_session_log with run_id filter
  session_log_run_result = handle_get_session_log({"agent_id": p6_agent_id, "run_id": p6_run_id})
  session_log_run_text = _extract_text_from_mcp_response(session_log_run_result)
  try:
    session_log_run_data = json.loads(session_log_run_text)
    session_log_run_count = session_log_run_data.get("entry_count", 0)
  except (json.JSONDecodeError, TypeError):
    session_log_run_count = 0
  record_test_outcome(
    "get_session_log_run_id_filter",
    session_log_run_count >= 3,
    f"run_filtered_count={session_log_run_count}"
  )

  # Write checkpoints for the test run
  _write_checkpoint(p6_agent_id, p6_run_id, "test-session", 1, {"step": "first"})
  _write_checkpoint(p6_agent_id, p6_run_id, "test-session", 2, {"step": "second"})

  # Test 158: get_checkpoints with run_id returns checkpoints in step order
  checkpoints_result = handle_get_checkpoints({"run_id": p6_run_id})
  checkpoints_text = _extract_text_from_mcp_response(checkpoints_result)
  try:
    checkpoints_data = json.loads(checkpoints_text)
    checkpoints_list = checkpoints_data.get("checkpoints", [])
    checkpoints_ordered = len(checkpoints_list) >= 2 and checkpoints_list[0].get("step_number", 0) <= checkpoints_list[1].get("step_number", 0)
  except (json.JSONDecodeError, TypeError):
    checkpoints_list = []
    checkpoints_ordered = False
  record_test_outcome(
    "get_checkpoints_returns_ordered",
    len(checkpoints_list) >= 2 and checkpoints_ordered,
    f"checkpoint_count={len(checkpoints_list)}, ordered={checkpoints_ordered}"
  )

  # Insert a run log entry for the test
  _call_sqlite(
    """INSERT INTO agent_run_log (run_id, agent_id, event_type, started_at, completed_at, llm_calls_made, tool_calls_made, tokens_consumed, status)
    VALUES (:run_id, :agent_id, 'user_message', :started_at, :completed_at, 2, 3, 1500, 'completed')""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "run_id": p6_run_id, "agent_id": p6_agent_id,
      "started_at": _iso_now(), "completed_at": _iso_now(),
    },
  )

  # Test 159: get_run_log with agent_id returns run summaries
  run_log_result = handle_get_run_log({"agent_id": p6_agent_id})
  run_log_text = _extract_text_from_mcp_response(run_log_result)
  try:
    run_log_data = json.loads(run_log_text)
    run_log_runs = run_log_data.get("runs", [])
    run_log_has_test_run = any(r.get("run_id") == p6_run_id for r in run_log_runs)
  except (json.JSONDecodeError, TypeError):
    run_log_runs = []
    run_log_has_test_run = False
  record_test_outcome(
    "get_run_log_returns_runs",
    len(run_log_runs) >= 1 and run_log_has_test_run,
    f"run_count={len(run_log_runs)}, has_test_run={run_log_has_test_run}"
  )

  # Cleanup Phase 6.8 test data
  _call_sqlite("DELETE FROM session_log WHERE agent_id = :agent_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"agent_id": p6_agent_id})
  _call_sqlite("DELETE FROM agent_checkpoints WHERE run_id = :run_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"run_id": p6_run_id})
  _call_sqlite("DELETE FROM agent_run_log WHERE run_id = :run_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"run_id": p6_run_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :agent_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"agent_id": p6_agent_id})

  # ==========================================================================
  # Phase 6.9: Cost Tracking tests
  # ==========================================================================
  p69_agent_id = f"test_cost_{test_run_id[:8]}"
  p69_run_id = f"run_cost_{test_run_id[:8]}"

  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, current_state, max_tokens_per_day, created_at, updated_at)
    VALUES (:agent_id, 'Cost Test Agent', 'test', 'IDLE', 5000, :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": p69_agent_id, "now": _iso_now()},
  )

  # Test 160: _create_run_log_entry_at_start creates row with status='running'
  _create_run_log_entry_at_start(p69_agent_id, p69_run_id, "user_message")
  run_start_result = _call_sqlite(
    "SELECT run_id, agent_id, status, llm_calls_made, tool_calls_made, tokens_consumed FROM agent_run_log WHERE run_id = :run_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"run_id": p69_run_id},
  )
  run_start_rows = _parse_rows_from_mcp_query_response(run_start_result)
  run_start_row_valid = (
    len(run_start_rows) == 1
    and run_start_rows[0].get("status") == "running"
    and int(run_start_rows[0].get("llm_calls_made", -1)) == 0
    and int(run_start_rows[0].get("tokens_consumed", -1)) == 0
  )
  record_test_outcome(
    "cost_create_run_log_entry_at_start_status_running",
    run_start_row_valid,
    f"rows={len(run_start_rows)}, status={run_start_rows[0].get('status') if run_start_rows else 'NONE'}"
  )

  # Test 161: _complete_run_log_entry_at_finish updates counters and status
  _complete_run_log_entry_at_finish(p69_run_id, "completed", 3, 5, 1200)
  run_complete_result = _call_sqlite(
    "SELECT status, llm_calls_made, tool_calls_made, tokens_consumed, completed_at FROM agent_run_log WHERE run_id = :run_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"run_id": p69_run_id},
  )
  run_complete_rows = _parse_rows_from_mcp_query_response(run_complete_result)
  run_complete_valid = (
    len(run_complete_rows) == 1
    and run_complete_rows[0].get("status") == "completed"
    and int(run_complete_rows[0].get("llm_calls_made", 0)) == 3
    and int(run_complete_rows[0].get("tool_calls_made", 0)) == 5
    and int(run_complete_rows[0].get("tokens_consumed", 0)) == 1200
    and run_complete_rows[0].get("completed_at") is not None
  )
  record_test_outcome(
    "cost_complete_run_log_entry_updates_counters",
    run_complete_valid,
    f"status={run_complete_rows[0].get('status') if run_complete_rows else 'NONE'}, "
    f"llm={run_complete_rows[0].get('llm_calls_made') if run_complete_rows else '?'}, "
    f"tools={run_complete_rows[0].get('tool_calls_made') if run_complete_rows else '?'}, "
    f"tokens={run_complete_rows[0].get('tokens_consumed') if run_complete_rows else '?'}"
  )

  # Test 162: _extract_token_count_from_llm_response with usage data
  llm_response_with_usage = {
    "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
    "choices": [{"message": {"content": "Hello world"}}],
  }
  extracted_tokens_from_usage = _extract_token_count_from_llm_response(llm_response_with_usage)
  record_test_outcome(
    "cost_extract_tokens_from_usage_field",
    extracted_tokens_from_usage == 700,
    f"expected=700, got={extracted_tokens_from_usage}"
  )

  # Test 163: _extract_token_count_from_llm_response falls back to estimation
  llm_response_no_usage = {
    "choices": [{"message": {"content": "A" * 400}}],
  }
  estimated_tokens_fallback = _extract_token_count_from_llm_response(llm_response_no_usage)
  record_test_outcome(
    "cost_extract_tokens_fallback_estimation",
    estimated_tokens_fallback == 100,
    f"expected=100, got={estimated_tokens_fallback}"
  )

  # Test 164: _check_daily_token_budget — within budget
  within_budget, used_today, daily_limit = _check_daily_token_budget_for_agent(p69_agent_id, {"max_tokens_per_day": 5000})
  record_test_outcome(
    "cost_daily_budget_within_limit",
    within_budget and daily_limit == 5000 and used_today == 1200,
    f"within={within_budget}, used={used_today}, limit={daily_limit}"
  )

  # Test 165: _check_daily_token_budget — exceeded
  _call_sqlite(
    """INSERT OR IGNORE INTO agent_run_log (run_id, agent_id, event_type, started_at, tokens_consumed, status)
    VALUES (:run_id, :agent_id, 'user_message', :started_at, 4000, 'completed')""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"run_id": f"{p69_run_id}_extra", "agent_id": p69_agent_id, "started_at": _iso_now()},
  )
  exceeded_budget, exceeded_used, exceeded_limit = _check_daily_token_budget_for_agent(p69_agent_id, {"max_tokens_per_day": 5000})
  record_test_outcome(
    "cost_daily_budget_exceeded",
    not exceeded_budget and exceeded_used >= 5200 and exceeded_limit == 5000,
    f"within={exceeded_budget}, used={exceeded_used}, limit={exceeded_limit}"
  )

  # Cleanup Phase 6.9 test data
  _call_sqlite("DELETE FROM agent_run_log WHERE agent_id = :agent_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"agent_id": p69_agent_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :agent_id", database=AGENT_KERNEL_DATABASE_NAME, bindings={"agent_id": p69_agent_id})

  # ==========================================================================
  # Phase 6.4: Inter-agent messaging tests
  # ==========================================================================
  p64_sender_id = f"test_sender_{test_run_id[:8]}"
  p64_target_id = f"test_target_{test_run_id[:8]}"
  now_str = _iso_now()

  # Sender is granted an allowlist naming the target (send_to_agent is
  # deny-all by default since the allowlist gating was added).
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, send_to_agent_allowlist, current_state, created_at, updated_at)
    VALUES (:aid, 'Sender Agent', 'I send messages.', :allowlist, 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p64_sender_id, "allowlist": json.dumps([p64_target_id]), "now": now_str},
  )
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at)
    VALUES (:aid, 'Target Agent', 'I receive messages.', 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p64_target_id, "now": now_str},
  )

  # Test 166: send_to_agent succeeds and enqueues event
  send_result = _handle_pseudo_tool_send_to_agent(
    p64_sender_id,
    {"agent_id": p64_target_id, "message": "Hello from sender!"},
    sender_run_id="test_run_send",
  )
  send_ok = not send_result.get("isError", True)
  enqueued_event_result = _call_sqlite(
    "SELECT event_type, payload_json FROM event_queue WHERE agent_id = :target_id AND event_type = 'agent_message' AND status = 'pending' ORDER BY queue_id DESC LIMIT 1",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"target_id": p64_target_id},
  )
  enqueued_rows = _parse_rows_from_mcp_query_response(enqueued_event_result)
  enqueue_has_message = False
  if enqueued_rows:
    try:
      payload = json.loads(enqueued_rows[0].get("payload_json", "{}"))
      enqueue_has_message = payload.get("message") == "Hello from sender!" and payload.get("sender_agent_id") == p64_sender_id
    except (json.JSONDecodeError, TypeError):
      pass
  record_test_outcome(
    "send_to_agent_enqueues_event",
    send_ok and enqueue_has_message,
    f"send_ok={send_ok}, enqueue_has_message={enqueue_has_message}"
  )

  # Test 167: send_to_agent rejects nonexistent target
  send_bad_result = _handle_pseudo_tool_send_to_agent(
    p64_sender_id,
    {"agent_id": "nonexistent_agent_12345", "message": "Should fail"},
    sender_run_id="test_run_bad",
  )
  record_test_outcome(
    "send_to_agent_rejects_nonexistent_target",
    send_bad_result.get("isError", False),
    f"isError={send_bad_result.get('isError')}"
  )

  # Test 168: send_to_agent rejects self-send
  send_self_result = _handle_pseudo_tool_send_to_agent(
    p64_sender_id,
    {"agent_id": p64_sender_id, "message": "Talking to myself"},
    sender_run_id="test_run_self",
  )
  record_test_outcome(
    "send_to_agent_rejects_self_send",
    send_self_result.get("isError", False),
    f"isError={send_self_result.get('isError')}"
  )

  # Test 169: agent directory includes other agents
  directory_text = _build_agent_directory_for_system_prompt(p64_sender_id)
  directory_has_target = p64_target_id in directory_text and "Target Agent" in directory_text
  directory_excludes_sender = p64_sender_id not in directory_text
  record_test_outcome(
    "agent_directory_includes_other_agents",
    directory_has_target and directory_excludes_sender,
    f"has_target={directory_has_target}, excludes_sender={directory_excludes_sender}"
  )

  # Test 170: send_to_agent tool definition present in pseudo-tools
  send_to_agent_in_pseudo_tools = "send_to_agent" in PSEUDO_TOOL_NAMES
  record_test_outcome(
    "send_to_agent_in_pseudo_tool_names",
    send_to_agent_in_pseudo_tools,
    f"in_set={send_to_agent_in_pseudo_tools}"
  )

  # Test 170b: send_to_agent DENIED when target is not in the sender's allowlist
  # (target -> sender direction: target agent has the default empty allowlist)
  send_denied_result = _handle_pseudo_tool_send_to_agent(
    p64_target_id,
    {"agent_id": p64_sender_id, "message": "Unauthorized direction"},
    sender_run_id="test_run_denied",
  )
  denied_is_error = send_denied_result.get("isError", False)
  denied_mentions_allowlist = "allowlist" in send_denied_result.get("text", "").lower()
  record_test_outcome(
    "send_to_agent_denied_without_allowlist_entry",
    denied_is_error and denied_mentions_allowlist,
    f"isError={denied_is_error}, mentions_allowlist={denied_mentions_allowlist}"
  )

  # Test 170c: agent directory shows only allowlisted agents and never leaks
  # another agent's system_prompt text
  directory_for_sender = _build_agent_directory_for_system_prompt(p64_sender_id)
  directory_for_target = _build_agent_directory_for_system_prompt(p64_target_id)
  sender_sees_target_no_prompt = (
    p64_target_id in directory_for_sender and "I receive messages." not in directory_for_sender
  )
  target_directory_empty_without_allowlist = directory_for_target == ""
  record_test_outcome(
    "agent_directory_respects_allowlist_and_hides_prompts",
    sender_sees_target_no_prompt and target_directory_empty_without_allowlist,
    f"sender_no_prompt_leak={sender_sees_target_no_prompt}, target_dir_empty={target_directory_empty_without_allowlist}"
  )

  # Test 170d: send_to_agent honors the hourly tool-call rate limit
  _call_sqlite(
    "UPDATE agents SET max_tool_calls_per_hour = 1 WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p64_sender_id},
  )
  _append_session_log_entry(p64_sender_id, "test_run_rate", "tool_executed", {"tool_name": "send_to_agent"})
  send_rate_limited_result = _handle_pseudo_tool_send_to_agent(
    p64_sender_id,
    {"agent_id": p64_target_id, "message": "Should be rate limited"},
    sender_run_id="test_run_rate",
  )
  rate_limited_is_error = send_rate_limited_result.get("isError", False)
  rate_limited_mentions_limit = "rate limit" in send_rate_limited_result.get("text", "").lower()
  record_test_outcome(
    "send_to_agent_honors_rate_limit",
    rate_limited_is_error and rate_limited_mentions_limit,
    f"isError={rate_limited_is_error}, mentions_limit={rate_limited_mentions_limit}"
  )
  _call_sqlite("DELETE FROM session_log WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p64_sender_id})

  # Cleanup Phase 6.4 test data
  _call_sqlite("DELETE FROM event_queue WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p64_target_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p64_sender_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p64_target_id})

  # ==========================================================================
  # Phase 6.5: ask_user pseudo-tool tests
  # ==========================================================================
  p65_agent_id = f"test_ask_{test_run_id[:8]}"
  p65_run_id = f"run_ask_{test_run_id[:8]}"
  p65_session_id = f"sess_ask_{test_run_id[:8]}"

  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at)
    VALUES (:aid, 'Ask Test Agent', 'test', 'EXECUTING_TOOL', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p65_agent_id, "now": _iso_now()},
  )

  # Test 171: ask_user with response — use background thread to deliver reply
  ask_result_holder = [None]
  def _ask_user_thread():
    ask_result_holder[0] = _handle_pseudo_tool_ask_user(
      p65_agent_id,
      {"question": "What is your favorite color?"},
      p65_run_id, p65_session_id, 10,
      {"display_name": "Ask Test Agent"},
    )

  ask_thread = threading.Thread(target=_ask_user_thread)
  ask_thread.start()

  import time as _time_module
  _time_module.sleep(0.3)

  respond_result = handle_respond_to_user_request({
    "agent_id": p65_agent_id,
    "response_text": "Blue!",
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  ask_thread.join(timeout=5.0)

  ask_result = ask_result_holder[0]
  ask_reply_delivered = (
    ask_result is not None
    and not ask_result.get("isError", True)
    and "Blue!" in ask_result.get("text", "")
  )
  record_test_outcome(
    "ask_user_receives_user_reply",
    ask_reply_delivered,
    f"isError={ask_result.get('isError') if ask_result else 'None'}, text_preview={ask_result.get('text', '')[:80] if ask_result else 'None'}"
  )

  # Test 172: respond_to_user_request returns success
  respond_ok = not respond_result.get("isError", True)
  record_test_outcome(
    "respond_to_user_request_returns_success",
    respond_ok,
    f"isError={respond_result.get('isError')}"
  )

  # Test 173: ask_user with no pending request returns error
  respond_no_pending = handle_respond_to_user_request({
    "agent_id": "nonexistent_agent_xyz",
    "response_text": "No one asked",
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })
  record_test_outcome(
    "respond_to_user_rejects_no_pending_request",
    respond_no_pending.get("isError", False),
    f"isError={respond_no_pending.get('isError')}"
  )

  # Test 174: ask_user timeout (short timeout)
  _call_sqlite(
    "UPDATE agents SET current_state = 'EXECUTING_TOOL' WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p65_agent_id},
  )
  timeout_result = _handle_pseudo_tool_ask_user(
    p65_agent_id,
    {"question": "Will this time out?", "timeout_seconds": 1},
    f"{p65_run_id}_timeout", p65_session_id, 20,
    {"display_name": "Ask Test Agent"},
  )
  timeout_ok = (
    timeout_result is not None
    and not timeout_result.get("isError", True)
    and "No response from user" in timeout_result.get("text", "")
  )
  record_test_outcome(
    "ask_user_timeout_returns_graceful_message",
    timeout_ok,
    f"text_preview={timeout_result.get('text', '')[:80] if timeout_result else 'None'}"
  )

  # Test 175: ask_user in pseudo-tool names and tool defs
  ask_user_in_names = "ask_user" in PSEUDO_TOOL_NAMES
  test_config = _extract_agent_config_as_dict(p65_agent_id) or {}
  ask_defs = _build_tool_definitions_for_agent(test_config)
  ask_in_defs = any(td.get("function", {}).get("name") == "ask_user" for td in ask_defs)
  record_test_outcome(
    "ask_user_registered_in_pseudo_tools_and_defs",
    ask_user_in_names and ask_in_defs,
    f"in_names={ask_user_in_names}, in_defs={ask_in_defs}"
  )

  # Cleanup Phase 6.5 test data
  _call_sqlite("DELETE FROM session_log WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p65_agent_id})
  _call_sqlite("DELETE FROM agent_checkpoints WHERE run_id LIKE :pattern", database=AGENT_KERNEL_DATABASE_NAME, bindings={"pattern": f"{p65_run_id}%"})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p65_agent_id})

  # ==========================================================================
  # Phase 6.6: Harnessed model support tests
  # ==========================================================================
  p66_agent_id = f"test_harness_{test_run_id[:8]}"
  now_str_66 = _iso_now()

  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, context_mode, current_state, created_at, updated_at)
    VALUES (:aid, 'Harness Test Agent', 'You are a test agent in harnessed mode.', 'harnessed', 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p66_agent_id, "now": now_str_66},
  )

  # Test 176: briefing message assembly includes key sections
  p66_config = _extract_agent_config_as_dict(p66_agent_id) or {}
  briefing = _assemble_briefing_message_for_harnessed_run(p66_config, "test-session", "Hello harnessed agent")
  briefing_has_tag = "<agent-briefing>" in briefing and "</agent-briefing>" in briefing
  briefing_has_persona = "Harness Test Agent" in briefing
  briefing_has_system_prompt = "test agent in harnessed mode" in briefing
  briefing_has_message = "Hello harnessed agent" in briefing
  record_test_outcome(
    "harnessed_briefing_includes_key_sections",
    briefing_has_tag and briefing_has_persona and briefing_has_system_prompt and briefing_has_message,
    f"tag={briefing_has_tag}, persona={briefing_has_persona}, prompt={briefing_has_system_prompt}, msg={briefing_has_message}"
  )

  # Test 177: harnessed agent config has context_mode='harnessed'
  config_mode = p66_config.get("context_mode")
  record_test_outcome(
    "harnessed_agent_config_context_mode",
    config_mode == "harnessed",
    f"context_mode={config_mode}"
  )

  # Test 178: mailbox branching logic works for harnessed mode
  agent_cfg_for_branch = _extract_agent_config_as_dict(p66_agent_id)
  branch_context_mode = (agent_cfg_for_branch or {}).get("context_mode", "raw")
  record_test_outcome(
    "harnessed_mailbox_branch_detects_mode",
    branch_context_mode == "harnessed",
    f"branch_mode={branch_context_mode}"
  )

  # Cleanup Phase 6.6 test data
  _call_sqlite("DELETE FROM session_log WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p66_agent_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p66_agent_id})

  # ==========================================================================
  # Phase 6.7: Cross-medium session continuity tests
  # ==========================================================================
  p67_agent_id = f"test_xmed_{test_run_id[:8]}"
  p67_session_id = f"shared_session_{test_run_id[:8]}"

  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at)
    VALUES (:aid, 'Cross-Medium Test Agent', 'test', 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p67_agent_id, "now": _iso_now()},
  )

  # Test 179: source_metadata is included in event payload when provided
  _enqueue_event(
    agent_id=p67_agent_id,
    event_type="user_message",
    payload={
      "message": "Message from telegram",
      "session_id": p67_session_id,
      "source_metadata": {"channel_type": "telegram", "chat_id": 12345},
    },
    priority="normal",
    queue_mode="queue",
  )
  payload_result = _call_sqlite(
    "SELECT payload_json FROM event_queue WHERE agent_id = :aid AND status = 'pending' ORDER BY queue_id DESC LIMIT 1",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p67_agent_id},
  )
  payload_rows = _parse_rows_from_mcp_query_response(payload_result)
  payload_has_source_metadata = False
  if payload_rows:
    try:
      stored_payload = json.loads(payload_rows[0].get("payload_json", "{}"))
      src_meta = stored_payload.get("source_metadata", {})
      payload_has_source_metadata = src_meta.get("channel_type") == "telegram" and src_meta.get("chat_id") == 12345
    except (json.JSONDecodeError, TypeError):
      pass
  record_test_outcome(
    "cross_medium_source_metadata_in_event_payload",
    payload_has_source_metadata,
    f"has_source_metadata={payload_has_source_metadata}"
  )

  # Test 180: _update_last_active_channel caches channel info
  _update_last_active_channel_from_event(p67_agent_id, {"channel_type": "web_ui", "user_id": "test_user"})
  cached_channel = _last_active_channel_per_agent.get(p67_agent_id)
  channel_cached_ok = (
    cached_channel is not None
    and cached_channel.get("channel_type") == "web_ui"
  )
  record_test_outcome(
    "cross_medium_channel_cache_updated",
    channel_cached_ok,
    f"cached_type={cached_channel.get('channel_type') if cached_channel else 'None'}"
  )

  # Test 181: same session_id works across different channels
  _save_transcript_entry(p67_agent_id, p67_session_id, "user", "First message via telegram")
  _save_transcript_entry(p67_agent_id, p67_session_id, "assistant", "Response to telegram")
  _save_transcript_entry(p67_agent_id, p67_session_id, "user", "Second message via web")

  history_result = _call_sqlite(
    "SELECT role, content FROM transcript_entries WHERE agent_id = :aid AND session_id = :sid ORDER BY entry_id ASC",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": p67_agent_id, "sid": p67_session_id},
  )
  history_rows = _parse_rows_from_mcp_query_response(history_result)
  cross_channel_session_ok = (
    len(history_rows) >= 3
    and history_rows[0].get("content") == "First message via telegram"
    and history_rows[2].get("content") == "Second message via web"
  )
  record_test_outcome(
    "cross_medium_shared_session_transcripts",
    cross_channel_session_ok,
    f"transcript_count={len(history_rows)}, first={history_rows[0].get('content', '')[:30] if history_rows else '?'}"
  )

  # Cleanup Phase 6.7 test data
  _last_active_channel_per_agent.pop(p67_agent_id, None)
  _call_sqlite("DELETE FROM event_queue WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p67_agent_id})
  _call_sqlite("DELETE FROM transcript_entries WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p67_agent_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": p67_agent_id})

  # ── Phase Admin: Admin Interface tests ──
  #
  # Covers state mgmt, channel-key derivation, STT normalization, entry/exit
  # detection, menu navigation (including global shortcuts + path chaining),
  # operation execution, guided input, requires_active_agent enforcement,
  # and the two intercept hooks (MCP and Telegram).
  # No real LLM calls in this phase — the admin interface is LLM-free by design.

  # Test 182: STT normalization — single-word number words → digits
  stt_n1 = _normalize_admin_input_text_for_stt_tolerance("one") == "1"
  stt_n2 = _normalize_admin_input_text_for_stt_tolerance("Won") == "1"
  stt_n3 = _normalize_admin_input_text_for_stt_tolerance("  three.  ") == "3"
  stt_n4 = _normalize_admin_input_text_for_stt_tolerance("EIGHT") == "8"
  stt_n5 = _normalize_admin_input_text_for_stt_tolerance("ate") == "8"
  stt_n6 = _normalize_admin_input_text_for_stt_tolerance("1.") == "1"
  stt_n7 = _normalize_admin_input_text_for_stt_tolerance("zero") == "0"
  stt_numbers_ok = stt_n1 and stt_n2 and stt_n3 and stt_n4 and stt_n5 and stt_n6 and stt_n7
  record_test_outcome(
    "admin_stt_number_normalization",
    stt_numbers_ok,
    f"one→{_normalize_admin_input_text_for_stt_tolerance('one')}, Won→{_normalize_admin_input_text_for_stt_tolerance('Won')}, three.→{_normalize_admin_input_text_for_stt_tolerance('three.')}, ate→{_normalize_admin_input_text_for_stt_tolerance('ate')}"
  )

  # Test 183: STT normalization does NOT mangle multi-word phrases
  mw = _normalize_admin_input_text_for_stt_tolerance("for my agent")
  stt_multiword_ok = mw == "for my agent"  # "for" → "4" only when it's the whole input
  record_test_outcome(
    "admin_stt_multiword_not_mangled",
    stt_multiword_ok,
    f"'for my agent' → '{mw}'"
  )

  # Test 184: STT normalization — slash commands
  sl1 = _normalize_admin_input_text_for_stt_tolerance("slash admin") == "/admin"
  sl2 = _normalize_admin_input_text_for_stt_tolerance("Slash Chat") == "/chat"
  sl3 = _normalize_admin_input_text_for_stt_tolerance("/ADMIN") == "/admin"
  slash_ok = sl1 and sl2 and sl3
  record_test_outcome(
    "admin_stt_slash_commands",
    slash_ok,
    f"'slash admin'→'{_normalize_admin_input_text_for_stt_tolerance('slash admin')}'"
  )

  # Test 185: STT normalization — navigation words
  nav1 = _normalize_admin_input_text_for_stt_tolerance("go back") == "back"
  nav2 = _normalize_admin_input_text_for_stt_tolerance("go home") == "home"
  nav3 = _normalize_admin_input_text_for_stt_tolerance("HOME") == "home"
  nav_ok = nav1 and nav2 and nav3
  record_test_outcome("admin_stt_nav_words", nav_ok, "go back/go home/HOME")

  # Test 186: Entry command detection
  entry_ok = (
    _check_if_message_is_admin_entry_command("/admin") and
    _check_if_message_is_admin_entry_command("admin") and
    not _check_if_message_is_admin_entry_command("administrator") and
    not _check_if_message_is_admin_entry_command("hi")
  )
  record_test_outcome("admin_entry_command_detection", entry_ok, "/admin + admin accepted; 'administrator' rejected")

  # Test 187: Exit command detection
  exit_ok = (
    _check_if_message_is_admin_exit_command("/chat") and
    _check_if_message_is_admin_exit_command("/exit") and
    _check_if_message_is_admin_exit_command("chat") and
    _check_if_message_is_admin_exit_command("exit") and
    not _check_if_message_is_admin_exit_command("hello")
  )
  record_test_outcome("admin_exit_command_detection", exit_ok, "/chat /exit chat exit detected; 'hello' rejected")

  # Test 188: Channel key derivation from Telegram source_metadata
  tg_key = _derive_admin_channel_key_from_source_metadata({"channel_type": "telegram", "chat_id": 98765})
  record_test_outcome(
    "admin_channel_key_from_telegram_source_metadata",
    tg_key == "tg:98765",
    f"got={tg_key}"
  )

  # Test 189: Channel key derivation from explicit channel_id parameter
  mcp_key = _derive_admin_channel_key_from_send_message_params({"channel_id": "mcp:cursor"})
  record_test_outcome(
    "admin_channel_key_from_explicit_channel_id",
    mcp_key == "mcp:cursor",
    f"got={mcp_key}"
  )

  # Test 190: Channel key derivation returns None when no identifier is available
  none_key = _derive_admin_channel_key_from_send_message_params({"agent_id": "x", "message": "hi"})
  record_test_outcome(
    "admin_channel_key_returns_none_without_identifier",
    none_key is None,
    f"got={none_key}"
  )

  # Test 191: Admin state round-trip (set → get → clear → get returns None)
  admin_test_channel_key = f"test-{test_run_id}"
  _clear_admin_state_for_channel(admin_test_channel_key)
  admin_state_round_trip_state = _build_default_admin_state_dict("agent-xyz-0001")
  _set_admin_state_for_channel(admin_test_channel_key, admin_state_round_trip_state)
  loaded = _get_admin_state_for_channel(admin_test_channel_key)
  state_roundtrip_ok = loaded is not None and loaded.get("active_agent_id") == "agent-xyz-0001"
  record_test_outcome(
    "admin_state_set_get_round_trip",
    state_roundtrip_ok,
    f"loaded_active={loaded.get('active_agent_id') if loaded else 'None'}"
  )
  _clear_admin_state_for_channel(admin_test_channel_key)
  # Purge the in-memory cache too so the next get hits SQLite
  with _admin_mode_state_cache_lock:
    _admin_mode_state_per_channel.pop(admin_test_channel_key, None)
  reloaded_after_clear = _get_admin_state_for_channel(admin_test_channel_key)
  record_test_outcome(
    "admin_state_clear_wipes_memory_and_sqlite",
    reloaded_after_clear is None,
    f"after_clear={reloaded_after_clear}"
  )

  # Test 192: Menu tree root has 8 category items
  root_items = _get_menu_items_at_node(ADMIN_MENU_TREE)
  record_test_outcome(
    "admin_menu_root_has_nine_categories",
    len(root_items) == 9,
    f"root_items_count={len(root_items)}"
  )

  # Test 193: Navigate to Agents submenu via path ["1"]
  agents_node = _resolve_menu_node_from_path(["1"])
  record_test_outcome(
    "admin_menu_navigate_path_1_to_agents",
    agents_node is not None and agents_node.get("label") == "Agents",
    f"got_label={agents_node.get('label') if agents_node else 'None'}"
  )

  # Test 194: Stale path returns None
  stale = _resolve_menu_node_from_path(["99", "88"])
  record_test_outcome(
    "admin_menu_stale_path_returns_none",
    stale is None,
    f"got={stale}"
  )

  # Test 195: Every global shortcut resolves to a valid menu path (or None for help)
  all_shortcuts_ok = True
  broken_shortcut_details = []
  for shortcut_word, target_path in ADMIN_GLOBAL_SHORTCUT_WORDS.items():
    if target_path is None:
      continue
    node = _resolve_menu_node_from_path(target_path)
    if node is None:
      all_shortcuts_ok = False
      broken_shortcut_details.append(f"{shortcut_word}→{target_path}")
  record_test_outcome(
    "admin_global_shortcuts_all_resolve_to_valid_paths",
    all_shortcuts_ok,
    f"broken={broken_shortcut_details}" if broken_shortcut_details else f"all {len(ADMIN_GLOBAL_SHORTCUT_WORDS)} shortcuts valid"
  )

  # Test 196: Every leaf action references a valid operation or known special type
  known_special_types = {
    "select_agent", "toggle_pause", "show_active_session", "new_session",
    "set_active_session", "list_event_sources_for_active_agent",
    "today_cost", "show_working_context", "show_tool_permissions", "show_rate_limits",
    "list_llm_providers", "list_models_for_current_provider", "search_openrouter_models",
    "show_engine_config", "set_primary_engine", "set_compaction_engine",
    "show_fallback_chain", "add_fallback_entry", "remove_fallback_entry",
    "list_configured_endpoints", "add_endpoint", "edit_endpoint",
    "remove_endpoint", "test_endpoint_health", "set_agent_endpoint",
    "scan_for_local_endpoints",
  }
  def _walk_tree_collect_actions(node):
    collected = []
    if node is ADMIN_MENU_TREE:
      items = node.get("items", [])
    else:
      items = (node.get("submenu") or {}).get("items", node.get("items", []))
    for it in items:
      if "submenu" in it:
        collected += _walk_tree_collect_actions(it)
      elif it.get("action"):
        collected.append(it["action"])
    return collected
  all_leaf_actions = _walk_tree_collect_actions(ADMIN_MENU_TREE)
  invalid_actions = []
  for a in all_leaf_actions:
    if a.get("type"):
      if a["type"] not in known_special_types:
        invalid_actions.append(a)
      continue
    op = a.get("operation")
    if op and op not in ALL_AGENT_OPERATIONS:
      invalid_actions.append(a)
  record_test_outcome(
    "admin_menu_leaf_actions_all_valid",
    not invalid_actions,
    f"invalid={invalid_actions[:3]}" if invalid_actions else f"all {len(all_leaf_actions)} leaves valid"
  )

  # Test 197: Intercept "/admin" enters admin mode and returns menu
  admin_intercept_ch = f"test-intercept-{test_run_id}"
  _clear_admin_state_for_channel(admin_intercept_ch)
  enter_result = _maybe_intercept_admin_message(admin_intercept_ch, "/admin", None)
  enter_ok = (
    enter_result is not None and
    "ADMIN MAIN MENU" in enter_result["response_text"] and
    _is_channel_in_admin_mode(admin_intercept_ch)
  )
  record_test_outcome(
    "admin_intercept_slash_admin_enters_mode",
    enter_ok,
    f"has_menu={'ADMIN MAIN MENU' in (enter_result or {}).get('response_text', '')}"
  )

  # Test 198: Intercept "1" while in admin mode navigates to Agents submenu
  submenu_result = _maybe_intercept_admin_message(admin_intercept_ch, "1", None)
  submenu_ok = submenu_result is not None and "AGENTS " in submenu_result["response_text"]
  record_test_outcome(
    "admin_intercept_digit_navigates_submenu",
    submenu_ok,
    f"head={(submenu_result or {}).get('response_text', '')[:50]}"
  )

  # Test 199: Intercept "back" returns to main menu from submenu
  back_result = _maybe_intercept_admin_message(admin_intercept_ch, "back", None)
  back_ok = back_result is not None and "ADMIN MAIN MENU" in back_result["response_text"]
  record_test_outcome(
    "admin_intercept_back_returns_to_parent",
    back_ok,
    f"head={(back_result or {}).get('response_text', '')[:50]}"
  )

  # Test 200: Intercept "home" returns to main menu from deep submenu
  _maybe_intercept_admin_message(admin_intercept_ch, "1", None)
  _maybe_intercept_admin_message(admin_intercept_ch, "3", None)  # into some submenu (may be leaf)
  home_result = _maybe_intercept_admin_message(admin_intercept_ch, "home", None)
  home_ok = home_result is not None and "ADMIN MAIN MENU" in home_result["response_text"]
  record_test_outcome(
    "admin_intercept_home_returns_to_root",
    home_ok,
    f"head={(home_result or {}).get('response_text', '')[:50]}"
  )

  # Test 201: Intercept "help" re-displays current menu
  help_result = _maybe_intercept_admin_message(admin_intercept_ch, "help", None)
  help_ok = help_result is not None and "ADMIN MAIN MENU" in help_result["response_text"]
  record_test_outcome(
    "admin_intercept_help_redisplays_menu",
    help_ok,
    f"head={(help_result or {}).get('response_text', '')[:50]}"
  )

  # Test 202: Path chaining "1 1" from root executes list_agents
  chain_result = _maybe_intercept_admin_message(admin_intercept_ch, "1 1", None)
  chain_ok = chain_result is not None and "AGENTS" in chain_result["response_text"]
  record_test_outcome(
    "admin_intercept_path_chain_executes_list_agents",
    chain_ok,
    f"head={(chain_result or {}).get('response_text', '')[:80]}"
  )

  # Test 203: Global shortcut "stats" routes to stats → today_cost leaf
  # (Without an active agent this path should politely require one.)
  _maybe_intercept_admin_message(admin_intercept_ch, "home", None)
  stats_result = _maybe_intercept_admin_message(admin_intercept_ch, "stats", None)
  stats_ok = stats_result is not None and (
    "requires an active agent" in stats_result["response_text"] or
    "Select an agent first" in stats_result["response_text"] or
    "TODAY" in stats_result["response_text"]
  )
  record_test_outcome(
    "admin_intercept_global_shortcut_stats",
    stats_ok,
    f"head={(stats_result or {}).get('response_text', '')[:80]}"
  )

  # Test 204: Exit command clears admin state
  exit_result = _maybe_intercept_admin_message(admin_intercept_ch, "/chat", None)
  exit_cleared_ok = (
    exit_result is not None and
    exit_result["exited"] is True and
    not _is_channel_in_admin_mode(admin_intercept_ch)
  )
  record_test_outcome(
    "admin_intercept_chat_exits_admin_mode",
    exit_cleared_ok,
    f"exited={(exit_result or {}).get('exited')}, still_in={_is_channel_in_admin_mode(admin_intercept_ch)}"
  )

  # Test 205: Regression — a non-admin message returns None (passes through)
  passthrough_result = _maybe_intercept_admin_message(admin_intercept_ch, "hello there, regular chat", None)
  record_test_outcome(
    "admin_intercept_passes_through_regular_messages",
    passthrough_result is None,
    f"got={passthrough_result}"
  )

  # Test 206: Unrecognized input inside a menu returns a helpful message
  _maybe_intercept_admin_message(admin_intercept_ch, "/admin", None)
  confused_result = _maybe_intercept_admin_message(admin_intercept_ch, "xyzzy", None)
  confused_ok = confused_result is not None and "didn't understand" in confused_result["response_text"]
  record_test_outcome(
    "admin_intercept_unknown_token_is_helpful",
    confused_ok,
    f"head={(confused_result or {}).get('response_text', '')[:60]}"
  )
  _maybe_intercept_admin_message(admin_intercept_ch, "/chat", None)

  # Test 207: Guided input — create_agent full flow via admin menu
  admin_guided_ch = f"test-guided-{test_run_id}"
  _clear_admin_state_for_channel(admin_guided_ch)
  _maybe_intercept_admin_message(admin_guided_ch, "/admin", None)
  _maybe_intercept_admin_message(admin_guided_ch, "1", None)  # Agents
  step1 = _maybe_intercept_admin_message(admin_guided_ch, "4", None)  # Create
  step1_ok = step1 is not None and "display name" in step1["response_text"].lower()
  guided_display_name = f"Admin Guided Bot {test_run_id}"
  step2 = _maybe_intercept_admin_message(admin_guided_ch, guided_display_name, None)
  step2_ok = step2 is not None and "system prompt" in step2["response_text"].lower()
  step3 = _maybe_intercept_admin_message(admin_guided_ch, "You are a test agent.", None)
  step3_ok = step3 is not None and ("provider" in step3["response_text"].lower() or "endpoint" in step3["response_text"].lower())
  step4 = _maybe_intercept_admin_message(admin_guided_ch, "1", None)  # first endpoint
  step4_ok = step4 is not None and ("Agent created" in step4["response_text"] or "agent_id" in step4["response_text"] or "active agent" in step4["response_text"].lower())
  guided_all_ok = step1_ok and step2_ok and step3_ok and step4_ok
  record_test_outcome(
    "admin_guided_create_agent_full_flow",
    guided_all_ok,
    f"steps={[step1_ok, step2_ok, step3_ok, step4_ok]}"
  )

  # Capture the guided-created agent_id for later cleanup
  guided_created_agent_id = None
  final_state_after_guided = _get_admin_state_for_channel(admin_guided_ch)
  if final_state_after_guided:
    guided_created_agent_id = final_state_after_guided.get("active_agent_id")

  # Test 208: Guided input — cancel mid-flow returns to menu without creating
  admin_cancel_ch = f"test-cancel-{test_run_id}"
  _clear_admin_state_for_channel(admin_cancel_ch)
  _maybe_intercept_admin_message(admin_cancel_ch, "/admin", None)
  _maybe_intercept_admin_message(admin_cancel_ch, "1 4", None)  # Agents > Create
  cancel_result = _maybe_intercept_admin_message(admin_cancel_ch, "cancel", None)
  cancel_ok = cancel_result is not None and "cancel" in cancel_result["response_text"].lower()
  cancel_state = _get_admin_state_for_channel(admin_cancel_ch)
  cancel_state_cleared = cancel_state is not None and cancel_state.get("pending_guided_input") is None
  record_test_outcome(
    "admin_guided_cancel_aborts_cleanly",
    cancel_ok and cancel_state_cleared,
    f"cancel_ok={cancel_ok}, state_cleared={cancel_state_cleared}"
  )
  _maybe_intercept_admin_message(admin_cancel_ch, "/chat", None)

  # Test 209: Guided input — invalid choice at enum step rejected (re-prompts)
  admin_invalid_ch = f"test-invalid-{test_run_id}"
  _clear_admin_state_for_channel(admin_invalid_ch)
  _maybe_intercept_admin_message(admin_invalid_ch, "/admin", None)
  _maybe_intercept_admin_message(admin_invalid_ch, "1 4", None)  # Agents > Create
  _maybe_intercept_admin_message(admin_invalid_ch, "Test Invalid", None)  # display name
  _maybe_intercept_admin_message(admin_invalid_ch, "Test prompt.", None)  # system prompt
  invalid_choice_result = _maybe_intercept_admin_message(admin_invalid_ch, "99", None)  # invalid provider #
  invalid_ok = invalid_choice_result is not None and "not a valid choice" in invalid_choice_result["response_text"]
  record_test_outcome(
    "admin_guided_invalid_choice_reprompts",
    invalid_ok,
    f"head={(invalid_choice_result or {}).get('response_text', '')[:80]}"
  )
  _maybe_intercept_admin_message(admin_invalid_ch, "cancel", None)
  _maybe_intercept_admin_message(admin_invalid_ch, "/chat", None)

  # Test 210: requires_active_agent enforced when no agent selected
  admin_require_ch = f"test-require-{test_run_id}"
  _clear_admin_state_for_channel(admin_require_ch)
  _maybe_intercept_admin_message(admin_require_ch, "/admin", None)
  require_result = _maybe_intercept_admin_message(admin_require_ch, "1 6", None)  # Agents > Pause/Resume
  require_ok = require_result is not None and "requires an active agent" in require_result["response_text"]
  record_test_outcome(
    "admin_requires_active_agent_enforced",
    require_ok,
    f"head={(require_result or {}).get('response_text', '')[:100]}"
  )
  _maybe_intercept_admin_message(admin_require_ch, "/chat", None)

  # Test 211: Active agent context — select, then show uses it
  admin_select_ch = f"test-select-{test_run_id}"
  _clear_admin_state_for_channel(admin_select_ch)
  # Create a fresh tiny agent to select
  select_agent_id = f"admin-sel-{test_run_id}"
  _call_sqlite(
    "INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at) "
    "VALUES (:aid, :dn, :sp, 'IDLE', :now, :now)",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": select_agent_id, "dn": f"Select Test {test_run_id}", "sp": "test", "now": _iso_now()},
  )
  _maybe_intercept_admin_message(admin_select_ch, "/admin", None)
  _maybe_intercept_admin_message(admin_select_ch, "1", None)
  select_prompt = _maybe_intercept_admin_message(admin_select_ch, "2", None)  # Select active agent
  select_prompt_ok = select_prompt is not None and "SELECT ACTIVE AGENT" in select_prompt["response_text"]
  # Find our test agent's position in the list
  list_text = select_prompt["response_text"] if select_prompt else ""
  chosen_number = None
  for line in list_text.splitlines():
    stripped = line.strip()
    if select_agent_id in stripped:
      tok = stripped.split(".", 1)[0].strip()
      if tok.isdigit():
        chosen_number = tok
        break
  select_after_ok = False
  if chosen_number:
    after_select = _maybe_intercept_admin_message(admin_select_ch, chosen_number, None)
    state_now = _get_admin_state_for_channel(admin_select_ch)
    select_after_ok = (after_select is not None and state_now is not None and
                       state_now.get("active_agent_id") == select_agent_id)
  record_test_outcome(
    "admin_select_agent_updates_active_context",
    select_prompt_ok and select_after_ok,
    f"prompt_ok={select_prompt_ok}, selected={select_after_ok}, chose={chosen_number}"
  )
  _maybe_intercept_admin_message(admin_select_ch, "/chat", None)

  # Test 212: Stale active_agent_id is cleared after agent deletion
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid",
               database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": select_agent_id})
  _maybe_intercept_admin_message(admin_select_ch, "/admin", None)
  # Force the state to reference the deleted agent, then invoke an action that triggers verification.
  state_for_stale = _get_admin_state_for_channel(admin_select_ch) or {}
  state_for_stale["active_agent_id"] = select_agent_id
  _set_admin_state_for_channel(admin_select_ch, state_for_stale)
  stale_result = _maybe_intercept_admin_message(admin_select_ch, "1 3", None)  # Agents > Show config (requires active)
  stale_ok = stale_result is not None and "no longer exists" in stale_result["response_text"]
  post_state = _get_admin_state_for_channel(admin_select_ch) or {}
  stale_cleared_ok = post_state.get("active_agent_id") is None
  record_test_outcome(
    "admin_stale_active_agent_detected_and_cleared",
    stale_ok and stale_cleared_ok,
    f"msg={(stale_result or {}).get('response_text', '')[:80]}, cleared={stale_cleared_ok}"
  )
  _maybe_intercept_admin_message(admin_select_ch, "/chat", None)

  # Test 213: handle_send_message intercept works with channel_id (and does NOT enqueue)
  mcp_intercept_agent_id = f"admin-mcp-{test_run_id}"
  _call_sqlite(
    "INSERT INTO agents (agent_id, display_name, system_prompt, current_state, created_at, updated_at) "
    "VALUES (:aid, :dn, :sp, 'IDLE', :now, :now)",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": mcp_intercept_agent_id, "dn": "MCP Intercept Test", "sp": "test", "now": _iso_now()},
  )
  queue_before = _get_pending_event_count(mcp_intercept_agent_id)
  mcp_intercept_result = handle_send_message({
    "agent_id": mcp_intercept_agent_id,
    "message": "/admin",
    "channel_id": f"mcp:test-{test_run_id}",
    "wait_for_response": False,
  })
  mcp_intercept_text = _extract_text_from_mcp_response(mcp_intercept_result)
  queue_after = _get_pending_event_count(mcp_intercept_agent_id)
  mcp_intercept_ok = (
    not mcp_intercept_result.get("isError", True) and
    "ADMIN MAIN MENU" in mcp_intercept_text and
    queue_after == queue_before  # message was NOT enqueued
  )
  record_test_outcome(
    "admin_mcp_intercept_via_channel_id_works",
    mcp_intercept_ok,
    f"has_menu={'ADMIN MAIN MENU' in mcp_intercept_text}, queue_delta={queue_after - queue_before}"
  )
  # send_message admin keys are namespaced by operator identity (anonymous here)
  _clear_admin_state_for_channel(f"op-anon:mcp:test-{test_run_id}")

  # Test 214: handle_send_message WITHOUT channel_id/source_metadata does NOT enter admin mode (regression)
  queue_before_2 = _get_pending_event_count(mcp_intercept_agent_id)
  regression_result = handle_send_message({
    "agent_id": mcp_intercept_agent_id,
    "message": "/admin",
    "wait_for_response": False,
  })
  regression_text = _extract_text_from_mcp_response(regression_result)
  queue_after_2 = _get_pending_event_count(mcp_intercept_agent_id)
  regression_ok = (
    not regression_result.get("isError", True) and
    "ADMIN MAIN MENU" not in regression_text and
    queue_after_2 > queue_before_2  # message WAS enqueued — treated as normal chat
  )
  record_test_outcome(
    "admin_mcp_intercept_requires_explicit_channel_no_regression",
    regression_ok,
    f"has_menu={'ADMIN MAIN MENU' in regression_text}, queue_delta={queue_after_2 - queue_before_2}"
  )

  # Cleanup Admin test data
  _stop_mailbox_for_agent(mcp_intercept_agent_id)
  for ch_key in [admin_test_channel_key, admin_intercept_ch, admin_guided_ch, admin_cancel_ch,
                 admin_invalid_ch, admin_require_ch, admin_select_ch,
                 f"mcp:test-{test_run_id}", f"op-anon:mcp:test-{test_run_id}"]:
    _clear_admin_state_for_channel(ch_key)
  for aid_to_clean in [mcp_intercept_agent_id, guided_created_agent_id]:
    if aid_to_clean:
      _call_sqlite("DELETE FROM event_queue WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": aid_to_clean})
      _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": aid_to_clean})

  # ── Contact Access Control Tests ──
  test_results.append("── Contact Access Control ──")

  contact_test_agent_id = f"contact-test-agent-{test_run_id}"
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, contact_approval_mode, current_state, created_at, updated_at)
    VALUES (:aid, 'Contact Test Agent', 'test', 'require_approval', 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": contact_test_agent_id, "now": _iso_now()},
  )

  # Test 215: Unknown user returns 'unknown'
  unknown_status = _check_if_transport_user_is_authorized_to_contact_agent(
    contact_test_agent_id, "telegram", "999999"
  )
  record_test_outcome(
    "contact_acl_unknown_user_returns_unknown",
    unknown_status == "unknown",
    f"status={unknown_status}"
  )

  # Test 216: Register pending contact
  register_ok = _register_pending_contact_from_transport_user(
    contact_test_agent_id, "telegram", "111222", display_name="Test User", username="testuser"
  )
  record_test_outcome(
    "contact_acl_register_pending_succeeds",
    register_ok,
    f"registered={register_ok}"
  )

  # Test 217: Pending user returns 'pending'
  pending_status = _check_if_transport_user_is_authorized_to_contact_agent(
    contact_test_agent_id, "telegram", "111222"
  )
  record_test_outcome(
    "contact_acl_pending_user_returns_pending",
    pending_status == "pending",
    f"status={pending_status}"
  )

  # Test 218: Duplicate register is ignored (INSERT OR IGNORE)
  dup_ok = _register_pending_contact_from_transport_user(
    contact_test_agent_id, "telegram", "111222", display_name="Test User", username="testuser"
  )
  record_test_outcome(
    "contact_acl_duplicate_register_no_error",
    dup_ok,
    f"dup_ok={dup_ok}"
  )

  # Test 219: list_contacts returns the pending contact
  list_result = handle_list_contacts({"agent_id": contact_test_agent_id})
  list_text = list_result.get("content", [{}])[0].get("text", "")
  list_has_pending = "111222" in list_text and "pending" in list_text
  record_test_outcome(
    "contact_acl_list_contacts_shows_pending",
    list_has_pending and not list_result.get("isError"),
    f"has_pending={list_has_pending}"
  )

  # Find the contact_id for approval
  contact_id_for_test = None
  try:
    list_data = json.loads(list_text)
    for row in list_data.get("data_rows_from_result_set", []):
      if str(row.get("transport_user_id")) == "111222":
        contact_id_for_test = row.get("contact_id")
        break
  except (json.JSONDecodeError, KeyError):
    pass

  # Test 220: Approve contact via MCP operation
  approve_result = handle_approve_contact({
    "agent_id": contact_test_agent_id,
    "contact_id": contact_id_for_test or -1,
  })
  approve_ok = not approve_result.get("isError") and contact_id_for_test is not None
  record_test_outcome(
    "contact_acl_approve_contact_via_mcp",
    approve_ok,
    f"contact_id={contact_id_for_test}, isError={approve_result.get('isError')}"
  )

  # Test 221: Approved user returns 'approved'
  approved_status = _check_if_transport_user_is_authorized_to_contact_agent(
    contact_test_agent_id, "telegram", "111222"
  )
  record_test_outcome(
    "contact_acl_approved_user_returns_approved",
    approved_status == "approved",
    f"status={approved_status}"
  )

  # Test 222: Block a different contact
  _register_pending_contact_from_transport_user(
    contact_test_agent_id, "telegram", "333444", display_name="Bad Actor", username="hacker"
  )
  block_list_result = handle_list_contacts({"agent_id": contact_test_agent_id, "status": "pending"})
  block_list_text = block_list_result.get("content", [{}])[0].get("text", "")
  block_contact_id = None
  try:
    block_data = json.loads(block_list_text)
    for row in block_data.get("data_rows_from_result_set", []):
      if str(row.get("transport_user_id")) == "333444":
        block_contact_id = row.get("contact_id")
        break
  except (json.JSONDecodeError, KeyError):
    pass
  block_result = handle_block_contact({
    "agent_id": contact_test_agent_id,
    "contact_id": block_contact_id or -1,
  })
  blocked_status = _check_if_transport_user_is_authorized_to_contact_agent(
    contact_test_agent_id, "telegram", "333444"
  )
  record_test_outcome(
    "contact_acl_block_then_check_returns_blocked",
    blocked_status == "blocked" and not block_result.get("isError") and block_contact_id is not None,
    f"status={blocked_status}, contact_id={block_contact_id}"
  )

  # Test 223: contact_approval_mode 'auto_approve_all' is retrievable
  _call_sqlite(
    "UPDATE agents SET contact_approval_mode = 'auto_approve_all' WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": contact_test_agent_id},
  )
  fetched_mode = _get_agent_contact_approval_mode(contact_test_agent_id)
  record_test_outcome(
    "contact_acl_approval_mode_auto_approve_readable",
    fetched_mode == "auto_approve_all",
    f"mode={fetched_mode}"
  )

  # Test 224: Approve with invalid contact_id returns error
  invalid_approve = handle_approve_contact({"agent_id": contact_test_agent_id, "contact_id": -9999})
  record_test_outcome(
    "contact_acl_approve_invalid_id_returns_error",
    invalid_approve.get("isError") is True,
    f"isError={invalid_approve.get('isError')}"
  )

  # Test 225: list_contacts with status filter 'approved' returns only approved
  approved_list = handle_list_contacts({"agent_id": contact_test_agent_id, "status": "approved"})
  approved_text = approved_list.get("content", [{}])[0].get("text", "")
  approved_has_111222 = "111222" in approved_text
  approved_has_no_333444 = "333444" not in approved_text
  record_test_outcome(
    "contact_acl_list_filtered_by_status_approved",
    approved_has_111222 and approved_has_no_333444 and not approved_list.get("isError"),
    f"has_111222={approved_has_111222}, no_333444={approved_has_no_333444}"
  )

  # Cleanup contact test data
  _call_sqlite("DELETE FROM agent_contact_access_control WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": contact_test_agent_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": contact_test_agent_id})

  # ── Second-pass hardening tests (admin ACL gating, identity binding, ──
  # ── shared-state trackers, schema advertising, deferred downloads)   ──
  test_results.append("── Second-Pass Hardening ──")

  # Test 226: unapproved Telegram user sending /admin is NOT intercepted
  # (contact ACL runs before the admin interpreter), and gets a pending row.
  sp_agent_id = f"secondpass-agent-{test_run_id}"
  sp_chat_id = 987650001
  sp_admin_channel_key = f"tg:{sp_chat_id}"
  _call_sqlite(
    """INSERT INTO agents (agent_id, display_name, system_prompt, contact_approval_mode, is_paused, current_state, created_at, updated_at)
    VALUES (:aid, 'SecondPass Agent', 'test', 'require_approval', 1, 'IDLE', :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": sp_agent_id, "now": _iso_now()},
  )
  _clear_admin_state_for_channel(sp_admin_channel_key)
  _enqueue_inbound_telegram_messages_for_agent(
    sp_agent_id, "sp-test-source", "sp-bot-hash",
    [{"text": "/admin", "chat_id": sp_chat_id, "from_user_id": 555001,
      "from_username": "intruder", "from_display_name": "Intruder", "message_id": 1}],
  )
  unapproved_user_did_not_enter_admin = not _is_channel_in_admin_mode(sp_admin_channel_key)
  unapproved_user_registered_as_pending = _check_if_transport_user_is_authorized_to_contact_agent(
    sp_agent_id, "telegram", "555001") == "pending"
  record_test_outcome(
    "telegram_admin_entry_blocked_for_unapproved_contact",
    unapproved_user_did_not_enter_admin and unapproved_user_registered_as_pending,
    f"no_admin={unapproved_user_did_not_enter_admin}, pending_row={unapproved_user_registered_as_pending}"
  )

  # Test 227: APPROVED Telegram user sending /admin IS intercepted.
  _call_sqlite(
    """INSERT OR IGNORE INTO agent_contact_access_control
    (agent_id, transport_type, transport_user_id, display_name, username, authorization_status, requested_at)
    VALUES (:aid, 'telegram', '555002', 'Operator', 'operator', 'approved', :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"aid": sp_agent_id, "now": _iso_now()},
  )
  _enqueue_inbound_telegram_messages_for_agent(
    sp_agent_id, "sp-test-source", "sp-bot-hash",
    [{"text": "/admin", "chat_id": sp_chat_id, "from_user_id": 555002,
      "from_username": "operator", "from_display_name": "Operator", "message_id": 2}],
  )
  approved_user_entered_admin = _is_channel_in_admin_mode(sp_admin_channel_key)
  record_test_outcome(
    "telegram_admin_entry_allowed_for_approved_contact",
    approved_user_entered_admin,
    f"admin_mode={approved_user_entered_admin}"
  )
  _clear_admin_state_for_channel(sp_admin_channel_key)

  # Test 228: MCP admin channel keys are namespaced by authenticated operator.
  sp_fake_responder = type("FakeResponderForIdentityTest", (), {"authenticated_user": "opuser"})()
  sp_bound_key_authenticated = _bind_admin_channel_key_to_authenticated_operator_identity(
    "mcp:shared-channel", {"responder": sp_fake_responder})
  sp_bound_key_anonymous = _bind_admin_channel_key_to_authenticated_operator_identity(
    "mcp:shared-channel", None)
  record_test_outcome(
    "admin_channel_key_bound_to_operator_identity",
    sp_bound_key_authenticated == "op:opuser:mcp:shared-channel"
    and sp_bound_key_anonymous == "op-anon:mcp:shared-channel"
    and sp_bound_key_authenticated != sp_bound_key_anonymous,
    f"authed={sp_bound_key_authenticated}, anon={sp_bound_key_anonymous}"
  )

  # Test 229: handle_agent must not mutate the caller's parameters dict
  # (handler_info is read with .get, not popped).
  sp_caller_params = {"input": {"operation": "readme"}, "handler_info": {"marker": "still-here"}}
  handle_agent(sp_caller_params)
  record_test_outcome(
    "handle_agent_does_not_mutate_caller_params",
    sp_caller_params.get("handler_info", {}).get("marker") == "still-here",
    f"handler_info_present={'handler_info' in sp_caller_params}"
  )

  # Test 230: in-memory trackers live in the cross-reload shared state
  # (a hot reload must not orphan pending approvals / ask_user waiters).
  sp_shared_state = _get_phase2_shared_state()
  sp_trackers_are_shared = (
    _pending_approval_requests is sp_shared_state.get('pending_approval_requests')
    and _pending_user_requests is sp_shared_state.get('pending_user_requests')
    and _per_run_tool_failure_tracker is sp_shared_state.get('per_run_tool_failure_tracker')
    and _reflection_idle_tracker is sp_shared_state.get('reflection_idle_tracker')
    and _last_active_channel_per_agent is sp_shared_state.get('last_active_channel_per_agent')
    and _last_listed_model_ids_sorted is sp_shared_state.get('last_listed_model_ids_sorted')
    and isinstance(sp_shared_state.get('sync_response_lock'), type(threading.Lock()))
  )
  record_test_outcome(
    "trackers_live_in_cross_reload_shared_state",
    sp_trackers_are_shared,
    f"all_shared={sp_trackers_are_shared}"
  )

  # Test 231: explicitly-allowed real tools are advertised to the LLM with
  # their schema, minus tool_unlock_token.
  sp_tool_defs = _build_tool_definitions_for_agent(
    {"read_tools_allowed": '["sqlite"]', "write_tools_allowed": '[]'})
  sp_sqlite_def = next((td for td in sp_tool_defs if td.get("function", {}).get("name") == "sqlite"), None)
  sp_sqlite_props = (sp_sqlite_def or {}).get("function", {}).get("parameters", {}).get("properties", {})
  sp_real_tool_advertised = (
    sp_sqlite_def is not None and len(sp_sqlite_props) > 0 and "tool_unlock_token" not in sp_sqlite_props
  )
  sp_wildcard_tool_defs = _build_tool_definitions_for_agent(
    {"read_tools_allowed": '["*"]', "write_tools_allowed": '[]'})
  sp_wildcard_not_expanded = all(
    td.get("function", {}).get("name") in PSEUDO_TOOL_NAMES for td in sp_wildcard_tool_defs
  )
  record_test_outcome(
    "explicit_real_tools_advertised_with_schema",
    sp_real_tool_advertised and sp_wildcard_not_expanded,
    f"sqlite_advertised={sp_real_tool_advertised}, wildcard_not_expanded={sp_wildcard_not_expanded}"
  )

  # Test 232: Telegram photo messages enqueue pending file_ids; the poller
  # thread does NOT download (payload has no image_data_uri_list).
  _enqueue_inbound_telegram_messages_for_agent(
    sp_agent_id, "sp-test-source", "sp-bot-hash",
    [{"text": "", "chat_id": sp_chat_id, "from_user_id": 555002,
      "from_username": "operator", "from_display_name": "Operator",
      "has_photo": True, "photo_file_id": "sp-photo-file-id-123", "message_id": 3}],
  )
  sp_photo_event_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT payload_json FROM event_queue WHERE agent_id = :aid AND event_type = 'telegram_message' ORDER BY queue_id DESC LIMIT 1",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id},
  ))
  sp_photo_payload = {}
  if sp_photo_event_rows:
    try:
      sp_photo_payload = json.loads(sp_photo_event_rows[0].get("payload_json", "{}"))
    except (json.JSONDecodeError, TypeError):
      sp_photo_payload = {}
  sp_pending_ids_queued = sp_photo_payload.get("pending_telegram_image_file_id_list") == ["sp-photo-file-id-123"]
  sp_no_inline_download = "image_data_uri_list" not in sp_photo_payload
  record_test_outcome(
    "telegram_photo_download_deferred_off_poller_thread",
    sp_pending_ids_queued and sp_no_inline_download,
    f"pending_ids={sp_pending_ids_queued}, no_inline_data_uri={sp_no_inline_download}"
  )

  # Test 233: oversized reported file_size is rejected by the download cap.
  sp_cap_value_is_sane = TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES == 10 * 1024 * 1024
  record_test_outcome(
    "telegram_download_size_cap_configured",
    sp_cap_value_is_sane,
    f"cap_bytes={TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES}"
  )

  # Test 234: live approval requests are mirrored to SQLite while pending and
  # marked 'approved' once the operator grants them (durable approval flow).
  _call_sqlite(
    "UPDATE agents SET current_state = 'EXECUTING_TOOL', is_paused = 0 WHERE agent_id = :aid",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id},
  )
  sp_approval_outcome_holder = [None]
  def _sp_request_approval_thread():
    sp_approval_outcome_holder[0] = _request_approval_for_tool_call(
      sp_agent_id, f"sp_apr_run_{test_run_id}", f"sp_apr_sess_{test_run_id}", 5,
      "sqlite", {"sql": "SELECT 1"}, {"display_name": "SecondPass Agent"},
    )
  sp_approval_thread = threading.Thread(target=_sp_request_approval_thread)
  sp_approval_thread.start()
  sp_live_approval_request_id = None
  for _ in range(50):
    time.sleep(0.1)
    for apr_id, apr_data in list(_pending_approval_requests.items()):
      if apr_data.get("agent_id") == sp_agent_id:
        sp_live_approval_request_id = apr_id
        break
    if sp_live_approval_request_id:
      break
  sp_persisted_while_pending = False
  if sp_live_approval_request_id:
    sp_pending_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
      "SELECT status FROM approval_requests WHERE approval_request_id = :apr_id",
      database=AGENT_KERNEL_DATABASE_NAME, bindings={"apr_id": sp_live_approval_request_id},
    ))
    sp_persisted_while_pending = bool(sp_pending_rows) and sp_pending_rows[0].get("status") == "pending"
    handle_approve_action({"approval_request_id": sp_live_approval_request_id})
  sp_approval_thread.join(timeout=10.0)
  sp_approval_outcome = sp_approval_outcome_holder[0]
  sp_live_approval_granted = sp_approval_outcome is not None and sp_approval_outcome[0] is True
  sp_resolved_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT status FROM approval_requests WHERE approval_request_id = :apr_id",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"apr_id": sp_live_approval_request_id or ""},
  ))
  sp_persisted_resolved_approved = bool(sp_resolved_rows) and sp_resolved_rows[0].get("status") == "approved"
  record_test_outcome(
    "approval_request_persisted_and_resolved_in_sqlite",
    sp_persisted_while_pending and sp_live_approval_granted and sp_persisted_resolved_approved,
    f"pending_row={sp_persisted_while_pending}, granted={sp_live_approval_granted}, resolved_row={sp_persisted_resolved_approved}"
  )

  # Test 235: orphaned approvals (from a dead process) are listed by
  # get_pending_approvals and resolvable via deny_action.
  sp_orphaned_approval_id = f"apr-orphan-{test_run_id}"
  _call_sqlite(
    """INSERT OR REPLACE INTO approval_requests
    (approval_request_id, agent_id, run_id, session_id, tool_name, tool_arguments_summary, status, requested_at, timeout_seconds)
    VALUES (:apr_id, :aid, 'dead_run', 'dead_sess', 'shell', '{}', 'orphaned', :now, 300)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"apr_id": sp_orphaned_approval_id, "aid": sp_agent_id, "now": _iso_now()},
  )
  sp_gpa_result = handle_get_pending_approvals({"agent_id": sp_agent_id})
  sp_gpa_text = _extract_text_from_mcp_response(sp_gpa_result)
  sp_orphan_listed = sp_orphaned_approval_id in sp_gpa_text and '"orphaned_approvals"' in sp_gpa_text
  sp_deny_orphan_result = handle_deny_action({"approval_request_id": sp_orphaned_approval_id, "reason": "stale after restart"})
  sp_deny_orphan_text = _extract_text_from_mcp_response(sp_deny_orphan_result)
  sp_orphan_denied_cleanly = (
    not sp_deny_orphan_result.get("isError", True) and "no longer waiting" in sp_deny_orphan_text
  )
  sp_orphan_rows_after = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT status FROM approval_requests WHERE approval_request_id = :apr_id",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"apr_id": sp_orphaned_approval_id},
  ))
  sp_orphan_marked_denied = bool(sp_orphan_rows_after) and sp_orphan_rows_after[0].get("status") == "denied"
  record_test_outcome(
    "orphaned_approval_listed_and_resolvable_after_restart",
    sp_orphan_listed and sp_orphan_denied_cleanly and sp_orphan_marked_denied,
    f"listed={sp_orphan_listed}, deny_ok={sp_orphan_denied_cleanly}, row_denied={sp_orphan_marked_denied}"
  )

  # Test 236: crash recovery marks leftover 'pending' approval rows 'orphaned'.
  sp_recovery_approval_id = f"apr-recov-{test_run_id}"
  _call_sqlite(
    """INSERT OR REPLACE INTO approval_requests
    (approval_request_id, agent_id, run_id, session_id, tool_name, tool_arguments_summary, status, requested_at, timeout_seconds)
    VALUES (:apr_id, :aid, 'dead_run2', 'dead_sess2', 'python', '{}', 'pending', :now, 300)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"apr_id": sp_recovery_approval_id, "aid": sp_agent_id, "now": _iso_now()},
  )
  sp_recovery_summary = _recover_agents_in_non_terminal_states()
  sp_recovery_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT status FROM approval_requests WHERE approval_request_id = :apr_id",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"apr_id": sp_recovery_approval_id},
  ))
  sp_recovery_row_orphaned = bool(sp_recovery_rows) and sp_recovery_rows[0].get("status") == "orphaned"
  record_test_outcome(
    "crash_recovery_orphans_pending_approvals",
    sp_recovery_row_orphaned and sp_recovery_summary.get("orphaned_pending_approvals", 0) >= 1,
    f"row_orphaned={sp_recovery_row_orphaned}, summary_count={sp_recovery_summary.get('orphaned_pending_approvals')}"
  )

  # Cleanup second-pass test data
  _stop_mailbox_for_agent(sp_agent_id)
  _call_sqlite("DELETE FROM event_queue WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _call_sqlite("DELETE FROM agent_contact_access_control WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _call_sqlite("DELETE FROM approval_requests WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _call_sqlite("DELETE FROM session_log WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _call_sqlite("DELETE FROM agent_checkpoints WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _call_sqlite("DELETE FROM agents WHERE agent_id = :aid", database=AGENT_KERNEL_DATABASE_NAME, bindings={"aid": sp_agent_id})
  _clear_admin_state_for_channel(sp_admin_channel_key)

  _stop_all_agent_mailboxes()

  summary = "ALL TESTS PASSED" if all_tests_passed_so_far else "SOME TESTS FAILED"
  test_results.insert(0, f"=== Agent Self-Test ({summary}) ===")
  test_results.insert(1, f"Database: {test_database} | Run ID: {test_run_id} | Env: {env}")
  test_results.insert(2, "")

  return {
    "content": [{"type": "text", "text": "\n".join(test_results)}],
    "isError": not all_tests_passed_so_far
  }


# ===============================================================================
# Agent CRUD Operation Handlers (Phase 1)
# ===============================================================================

def _generate_agent_id(display_name: str) -> str:
  """Generate a URL-safe agent_id from display_name + random suffix.

  Format: lowercase-hyphenated-name-XXXX where XXXX is 4 hex chars.
  Collisions are checked in the caller before insert.
  """
  import re
  slug = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
  if not slug:
    slug = "agent"
  slug = slug[:40]
  suffix = hashlib.md5(f"{display_name}{time.time()}".encode()).hexdigest()[:4]
  return f"{slug}-{suffix}"

def _iso_now() -> str:
  """Return current UTC time as ISO 8601 string with milliseconds."""
  from datetime import datetime, timezone
  return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def handle_init_schema(params: Dict) -> Dict:
  """Force-initialize the agent kernel database schema."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  return {
    "content": [{"type": "text", "text": schema_msg}],
    "isError": not schema_ok
  }

def handle_create_agent(params: Dict) -> Dict:
  """Create a new agent persona and persist to database."""
  display_name = params["display_name"]
  system_prompt = params["system_prompt"]
  agent_id = _generate_agent_id(display_name)
  now = _iso_now()

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  insert_sql = """INSERT INTO agents (
    agent_id, display_name, system_prompt,
    llm_provider, llm_model, llm_endpoint,
    compaction_provider, compaction_model, compaction_endpoint,
    context_mode,
    read_tools_allowed, write_tools_allowed, tools_requiring_approval,
    send_to_agent_allowlist,
    max_tool_rounds_per_run,
    current_state,
    created_at, updated_at
  ) VALUES (
    :agent_id, :display_name, :system_prompt,
    :llm_provider, :llm_model, :llm_endpoint,
    :compaction_provider, :compaction_model, :compaction_endpoint,
    :context_mode,
    :read_tools_allowed, :write_tools_allowed, :tools_requiring_approval,
    :send_to_agent_allowlist,
    :max_tool_rounds_per_run,
    'IDLE',
    :created_at, :updated_at
  )"""

  bindings = {
    "agent_id": agent_id,
    "display_name": display_name,
    "system_prompt": system_prompt,
    "llm_provider": params.get("llm_provider", ""),
    "llm_model": params.get("llm_model", ""),
    "llm_endpoint": params.get("llm_endpoint", ""),
    "compaction_provider": params.get("compaction_provider", ""),
    "compaction_model": params.get("compaction_model", ""),
    "compaction_endpoint": params.get("compaction_endpoint", ""),
    "context_mode": params.get("context_mode", "raw"),
    "read_tools_allowed": params.get("read_tools_allowed", '["*"]'),
    "write_tools_allowed": params.get("write_tools_allowed", '[]'),
    "tools_requiring_approval": params.get("tools_requiring_approval", '[]'),
    "send_to_agent_allowlist": params.get("send_to_agent_allowlist", '[]'),
    "max_tool_rounds_per_run": params.get("max_tool_rounds_per_run", 10),
    "created_at": now,
    "updated_at": now,
  }

  result = _call_sqlite(insert_sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to create agent: {_extract_text_from_mcp_response(result)[:300]}")

  MCPLogger.log(TOOL_LOG_NAME, f"Created agent: {agent_id} ({display_name})")
  return {
    "content": [{"type": "text", "text": json.dumps({
      "agent_id": agent_id,
      "display_name": display_name,
      "current_state": "IDLE",
      "created_at": now,
    }, indent=2)}],
    "isError": False
  }

def handle_list_agents(params: Dict) -> Dict:
  """List all agents with their current state."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  include_paused = params.get("include_paused", True)
  sql = "SELECT agent_id, display_name, current_state, is_paused, llm_provider, llm_model, context_mode, created_at, last_active_at FROM agents"
  if not include_paused:
    sql += " WHERE is_paused = 0"
  sql += " ORDER BY created_at DESC"

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME)
  if result.get("isError"):
    return create_error_response(f"Failed to list agents: {_extract_text_from_mcp_response(result)[:300]}")

  return {"content": [{"type": "text", "text": _extract_text_from_mcp_response(result)}], "isError": False}

def handle_get_agent(params: Dict) -> Dict:
  """Get full configuration and current state for a single agent."""
  agent_id = params["agent_id"]
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  result = _call_sqlite(
    "SELECT * FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )
  if result.get("isError"):
    return create_error_response(f"Failed to get agent: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  if '"data_rows_from_result_set": []' in response_text or '"data_rows_from_result_set": null' in response_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  return {"content": [{"type": "text", "text": response_text}], "isError": False}

def handle_update_agent(params: Dict) -> Dict:
  """Update agent configuration fields."""
  agent_id = params["agent_id"]
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  updatable_columns = {
    "display_name", "system_prompt", "working_context",
    "llm_provider", "llm_model", "compaction_provider", "compaction_model",
    "context_mode", "harness_session_type", "harness_endpoint_config",
    "max_response_tokens", "compaction_threshold_override",
    "read_tools_allowed", "write_tools_allowed", "tools_requiring_approval",
    "send_to_agent_allowlist",
    "max_tool_rounds_per_run", "max_run_duration_seconds",
    "max_tokens_per_day", "max_llm_calls_per_hour", "max_tool_calls_per_hour",
    "reflection_idle_timeout_minutes", "reflection_enabled",
    "response_format", "default_response_channel",
    "contact_approval_mode",
  }

  set_clauses = []
  bindings: Dict[str, Any] = {"agent_id": agent_id, "updated_at": _iso_now()}

  for key, value in params.items():
    if key in updatable_columns:
      set_clauses.append(f"{key} = :{key}")
      bindings[key] = value

  if not set_clauses:
    return create_error_response("No updatable fields provided. Updatable fields: " + ", ".join(sorted(updatable_columns)))

  set_clauses.append("updated_at = :updated_at")
  sql = f"UPDATE agents SET {', '.join(set_clauses)} WHERE agent_id = :agent_id"

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to update agent: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  if '"rows_modified_by_operation": 0' in response_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  MCPLogger.log(TOOL_LOG_NAME, f"Updated agent {agent_id}: {', '.join(set_clauses[:-1])}")
  return {"content": [{"type": "text", "text": f"Agent '{agent_id}' updated successfully."}], "isError": False}

def handle_delete_agent(params: Dict) -> Dict:
  """Delete an agent and optionally its history."""
  agent_id = params["agent_id"]
  delete_history = params.get("delete_history", False)
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  check_result = _call_sqlite(
    "SELECT agent_id FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )
  check_text = _extract_text_from_mcp_response(check_result)
  if agent_id not in check_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  # Stop live resources FIRST so a deleted agent stops producing events:
  # unregister its Telegram callbacks, stop its mailbox worker, and remove
  # its event sources (cron rows gone = scheduler stops firing them), queued
  # events, contact-ACL rows, and approval records, regardless of the
  # delete_history flag (SQLite FK cascade is not guaranteed via the sqlite
  # tool, and orphaned approval rows would show in get_pending_approvals).
  telegram_source_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT source_id FROM event_sources WHERE agent_id = :agent_id AND source_type = 'telegram'",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  ))
  for telegram_source_row in telegram_source_rows:
    telegram_source_id = telegram_source_row.get("source_id")
    if telegram_source_id:
      _unregister_telegram_event_source_callback(telegram_source_id)
  _stop_mailbox_for_agent(agent_id)
  for per_agent_operational_state_table in ("event_sources", "event_queue", "agent_contact_access_control", "approval_requests"):
    _call_sqlite(
      f"DELETE FROM {per_agent_operational_state_table} WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": agent_id}
    )

  if delete_history:
    history_tables_with_agent_id_column = [
      "session_log", "agent_checkpoints", "transcript_entries",
      "compaction_boundaries", "memory_entries", "dead_letter_queue",
      "agent_run_log",
    ]
    for table in history_tables_with_agent_id_column:
      _call_sqlite(
        f"DELETE FROM {table} WHERE agent_id = :agent_id",
        database=AGENT_KERNEL_DATABASE_NAME,
        bindings={"agent_id": agent_id}
      )

  result = _call_sqlite(
    "DELETE FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )
  if result.get("isError"):
    return create_error_response(f"Failed to delete agent: {_extract_text_from_mcp_response(result)[:300]}")

  MCPLogger.log(TOOL_LOG_NAME, f"Deleted agent {agent_id} (history={'deleted' if delete_history else 'preserved'})")
  return {"content": [{"type": "text", "text": f"Agent '{agent_id}' deleted. History: {'deleted' if delete_history else 'preserved'}."}], "isError": False}

def handle_status(params: Dict) -> Dict:
  """Return kernel health summary and all agent states."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  agents_result = _call_sqlite(
    "SELECT agent_id, display_name, current_state, is_paused FROM agents ORDER BY display_name",
    database=AGENT_KERNEL_DATABASE_NAME
  )
  queue_result = _call_sqlite(
    "SELECT COUNT(*) as pending_event_count FROM event_queue WHERE status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME
  )
  dlq_result = _call_sqlite(
    "SELECT COUNT(*) as dead_letter_count FROM dead_letter_queue WHERE status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME
  )

  status_output = {
    "kernel_schema_version": AGENT_KERNEL_SCHEMA_VERSION,
    "database": AGENT_KERNEL_DATABASE_NAME,
    "agents": _extract_text_from_mcp_response(agents_result),
    "pending_events": _extract_text_from_mcp_response(queue_result),
    "dead_letters": _extract_text_from_mcp_response(dlq_result),
  }

  return {"content": [{"type": "text", "text": json.dumps(status_output, indent=2)}], "isError": False}

def handle_send_message(params: Dict) -> Dict:
  """Send a message to an agent via the actor mailbox.

  Creates a durable event queue entry, signals the agent's mailbox worker,
  and (by default) blocks until the worker processes the event and returns
  the agent's response. With wait_for_response=False, returns immediately
  after enqueuing.

  Phase 2 upgrade: routes through the actor mailbox for proper concurrency
  control. The mailbox serializes all events per agent (queue modes, priority).
  """
  agent_id = params["agent_id"]
  message = params["message"]
  session_id = params.get("session_id", f"session-{hashlib.md5(f'{agent_id}{time.time()}'.encode()).hexdigest()[:8]}")
  wait_for_response = params.get("wait_for_response", True)

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  check_result = _call_sqlite(
    "SELECT agent_id, current_state, is_paused FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )
  check_text = _extract_text_from_mcp_response(check_result)
  if agent_id not in check_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  admin_channel_key = _derive_admin_channel_key_from_send_message_params(params)
  if admin_channel_key:
    # Admin state must belong to an operator identity, not a spoofable
    # free-form channel string (also stops MCP callers hijacking a Telegram
    # chat's admin session by faking source_metadata).
    admin_channel_key = _bind_admin_channel_key_to_authenticated_operator_identity(
      admin_channel_key, params.get("handler_info"),
    )
    admin_intercept_result = _maybe_intercept_admin_message(
      channel_key=admin_channel_key,
      raw_incoming_text=message,
      candidate_initial_active_agent_id=agent_id,
    )
    if admin_intercept_result is not None:
      return {
        "content": [{"type": "text", "text": admin_intercept_result["response_text"]}],
        "isError": False,
      }

  now = _iso_now()
  idempotency_key = hashlib.md5(f"{agent_id}:{session_id}:{message}:{now}".encode()).hexdigest()

  shared = _get_phase2_shared_state()
  sync_event = None
  this_call_registered_the_sync_waiter = False
  if wait_for_response:
    # Register under the lock and never overwrite an existing waiter: an
    # overwrite would orphan the first caller's Event so it blocks the full
    # 300s even after the agent responds.
    with shared['sync_response_lock']:
      sync_event = shared['sync_response_events'].get(idempotency_key)
      if sync_event is None:
        sync_event = threading.Event()
        shared['sync_response_events'][idempotency_key] = sync_event
        this_call_registered_the_sync_waiter = True

  event_payload: Dict[str, Any] = {"message": message, "session_id": session_id}
  source_metadata = params.get("source_metadata")
  if source_metadata and isinstance(source_metadata, dict):
    event_payload["source_metadata"] = source_metadata

  ok, enqueue_status, queue_id = _enqueue_event(
    agent_id=agent_id,
    event_type="user_message",
    payload=event_payload,
    priority="normal",
    queue_mode="queue",
    idempotency_key=idempotency_key,
  )

  if not ok:
    if sync_event and this_call_registered_the_sync_waiter:
      with shared['sync_response_lock']:
        shared['sync_response_events'].pop(idempotency_key, None)
    return create_error_response(f"Failed to enqueue message: {enqueue_status}")

  mailbox = _get_or_create_mailbox_for_agent(agent_id)
  mailbox.signal_new_event_available()

  if wait_for_response and sync_event:
    MCPLogger.log(TOOL_LOG_NAME, f"Waiting for agent {agent_id} to process message (session={session_id})")
    sync_event.wait(timeout=300)

    with shared['sync_response_lock']:
      if this_call_registered_the_sync_waiter:
        response = shared['sync_response_data'].pop(idempotency_key, None)
        shared['sync_response_events'].pop(idempotency_key, None)
      else:
        # Piggy-backed on another caller's waiter (duplicate idempotency key):
        # read without consuming so the registering caller still gets it.
        response = shared['sync_response_data'].get(idempotency_key)

    if response is not None:
      return response
    return create_error_response(f"Agent '{agent_id}' timed out (300s) processing message.")

  MCPLogger.log(TOOL_LOG_NAME, f"Enqueued message for agent {agent_id} (session={session_id}, async)")
  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "enqueued", "agent_id": agent_id, "queue_id": queue_id, "session_id": session_id,
    }, indent=2)}],
    "isError": False
  }

def handle_get_history(params: Dict) -> Dict:
  """Get conversation transcript entries for an agent."""
  agent_id = params["agent_id"]
  session_id = params.get("session_id")
  limit = params.get("limit", 50)
  since = params.get("since")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  sql = "SELECT entry_id, session_id, role, content, tool_name, token_count_estimate, created_at FROM transcript_entries WHERE agent_id = :agent_id"
  bindings: Dict[str, Any] = {"agent_id": agent_id}

  if session_id:
    sql += " AND session_id = :session_id"
    bindings["session_id"] = session_id
  if since:
    sql += " AND created_at > :since"
    bindings["since"] = since

  sql += " ORDER BY entry_id DESC LIMIT :limit"
  bindings["limit"] = limit

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to get history: {_extract_text_from_mcp_response(result)[:300]}")

  return {"content": [{"type": "text", "text": _extract_text_from_mcp_response(result)}], "isError": False}


# ===============================================================================
# Session Log (append-only event log — spec §6)
#
# Every significant event in the agent's lifecycle is recorded here. This is
# the single source of truth for audit trails, debugging, and recall search.
# Entries are NEVER mutated — only appended. Only agent deletion with
# delete_history=True removes entries.
# ===============================================================================

VALID_SESSION_LOG_ENTRY_TYPES = {
  "run_started", "run_resumed", "run_completed", "run_failed", "run_cancelled",
  "state_transition", "checkpoint_written",
  "context_assembled", "context_compacted",
  "llm_called", "llm_response", "llm_error",
  "tool_proposed", "tool_executed", "tool_failed", "tool_skipped_receipt",
  "policy_checked", "approval_requested", "approval_granted", "approval_denied",
  "approval_timeout", "rate_limit_hit", "circuit_breaker_tripped",
  "memory_inserted", "memory_updated", "memory_searched",
  "core_memory_updated", "reflection_completed",
  "message_received", "message_sent",
  "error", "budget_exceeded",
  "ask_user_requested", "ask_user_responded", "ask_user_timeout",
}

def _append_session_log_entry(agent_id: str, run_id: str, entry_type: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
  """Append a single event to the agent's session log. Never mutates existing entries.

  Args:
    agent_id: Which agent this event belongs to.
    run_id: The run (execution cycle) this event belongs to.
    entry_type: One of VALID_SESSION_LOG_ENTRY_TYPES (validated but not enforced
                to allow forward-compatible types from future phases).
    payload: Arbitrary dict of event-specific data, serialized to JSON.

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  if entry_type not in VALID_SESSION_LOG_ENTRY_TYPES:
    MCPLogger.log(TOOL_LOG_NAME, f"Session log: unrecognized entry_type '{entry_type}' for agent {agent_id} (allowed but logged)")

  payload_json = json.dumps(payload) if isinstance(payload, dict) else str(payload)
  now = _iso_now()

  result = _call_sqlite(
    """INSERT INTO session_log (agent_id, run_id, entry_type, payload_json, created_at)
    VALUES (:agent_id, :run_id, :entry_type, :payload_json, :created_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "run_id": run_id,
      "entry_type": entry_type,
      "payload_json": payload_json,
      "created_at": now,
    }
  )

  if result.get("isError"):
    error_detail = _extract_text_from_mcp_response(result)[:300]
    MCPLogger.log(TOOL_LOG_NAME, f"Session log INSERT failed for agent {agent_id}, run {run_id}: {error_detail}")
    return False, error_detail

  return True, ""


def _query_session_log(
  agent_id: str,
  run_id: Optional[str] = None,
  entry_type: Optional[str] = None,
  since: Optional[str] = None,
  limit: int = 100,
) -> Dict[str, Any]:
  """Query the session log with optional filters.

  Args:
    agent_id: Required — which agent's log to query.
    run_id: Optional — filter to a specific run.
    entry_type: Optional — filter to a specific event type.
    since: Optional — ISO timestamp, return only entries after this time.
    limit: Max entries to return (default 100).

  Returns:
    The raw MCP response dict from the sqlite tool (caller should check isError).
  """
  sql = "SELECT entry_id, agent_id, run_id, entry_type, payload_json, created_at FROM session_log WHERE agent_id = :agent_id"
  bindings: Dict[str, Any] = {"agent_id": agent_id}

  if run_id is not None:
    sql += " AND run_id = :run_id"
    bindings["run_id"] = run_id
  if entry_type is not None:
    sql += " AND entry_type = :entry_type"
    bindings["entry_type"] = entry_type
  if since is not None:
    sql += " AND created_at > :since"
    bindings["since"] = since

  sql += " ORDER BY entry_id ASC LIMIT :limit"
  bindings["limit"] = limit

  return _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)


# ===============================================================================
# Durable Checkpoints (crash recovery — spec §3.3)
#
# Checkpoint after every state transition. If the process crashes, the agent
# resumes from the latest checkpoint for its active run.
# ===============================================================================

def _write_checkpoint(
  agent_id: str,
  run_id: str,
  session_id: str,
  step_number: int,
  state_snapshot: Dict[str, Any],
) -> Tuple[bool, str]:
  """Write a durable checkpoint for the current execution step.

  Args:
    agent_id: Which agent.
    run_id: Which run.
    session_id: The conversation session.
    step_number: Monotonically increasing step counter within this run.
    state_snapshot: Full serializable state needed to resume from this point,
                    including current_state, assembled context, pending tool calls, etc.

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  state_json = json.dumps(state_snapshot)
  now = _iso_now()

  result = _call_sqlite(
    """INSERT INTO agent_checkpoints (agent_id, run_id, session_id, step_number, state_json, created_at)
    VALUES (:agent_id, :run_id, :session_id, :step_number, :state_json, :created_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "run_id": run_id,
      "session_id": session_id,
      "step_number": step_number,
      "state_json": state_json,
      "created_at": now,
    }
  )

  if result.get("isError"):
    error_detail = _extract_text_from_mcp_response(result)[:300]
    MCPLogger.log(TOOL_LOG_NAME, f"Checkpoint write failed for agent {agent_id}, run {run_id}, step {step_number}: {error_detail}")
    return False, error_detail

  _append_session_log_entry(agent_id, run_id, "checkpoint_written", {
    "step_number": step_number,
    "state_keys": list(state_snapshot.keys()),
  })

  return True, ""


def _load_latest_checkpoint(agent_id: str, run_id: str) -> Optional[Dict[str, Any]]:
  """Load the most recent checkpoint for a given agent run.

  Returns:
    The deserialized state_snapshot dict if a checkpoint exists, or None if no
    checkpoint is found for this agent/run combination.
  """
  result = _call_sqlite(
    """SELECT checkpoint_id, step_number, state_json, created_at
    FROM agent_checkpoints
    WHERE agent_id = :agent_id AND run_id = :run_id
    ORDER BY step_number DESC LIMIT 1""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "run_id": run_id}
  )

  if result.get("isError"):
    MCPLogger.log(TOOL_LOG_NAME, f"Checkpoint load failed for agent {agent_id}, run {run_id}: {_extract_text_from_mcp_response(result)}")
    return None

  response_text = _extract_text_from_mcp_response(result)
  if '"data_rows_from_result_set": []' in response_text or '"data_rows_from_result_set": null' in response_text:
    return None

  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if not rows:
      return None
    row = rows[0]
    state_json_str = row.get("state_json", "{}")
    checkpoint_data = json.loads(state_json_str)
    checkpoint_data["_checkpoint_meta"] = {
      "checkpoint_id": row.get("checkpoint_id"),
      "step_number": row.get("step_number"),
      "created_at": row.get("created_at"),
    }
    return checkpoint_data
  except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Checkpoint parse failed for agent {agent_id}, run {run_id}: {e}")
    return None


# ===============================================================================
# State Machine Integration (spec §3.4)
#
# Wires the state validator into actual DB updates + checkpoints + session log.
# Every transition: validate → update agents table → checkpoint → log.
# ===============================================================================

def _transition_agent_state(
  agent_id: str,
  run_id: str,
  session_id: str,
  step_number: int,
  from_state: str,
  to_state: str,
  checkpoint_snapshot: Dict[str, Any],
) -> Tuple[bool, str]:
  """Perform a validated, durable state transition.

  This is the ONLY way agent state should change during a run. It:
  1. Validates the transition is legal.
  2. Updates the agents table current_state.
  3. Writes a durable checkpoint with the provided snapshot.
  4. Logs the transition to the session log.

  Args:
    agent_id: Which agent.
    run_id: Which run.
    session_id: The conversation session.
    step_number: Current step counter (for checkpoint ordering).
    from_state: The state we expect the agent to currently be in.
    to_state: The state to transition to.
    checkpoint_snapshot: Full state needed to resume from this point.

  Returns:
    (True, "") on success.
    (False, error_reason) on failure (invalid transition, DB error, etc.).
  """
  is_valid, rejection_reason = validate_agent_state_transition(from_state, to_state)
  if not is_valid:
    MCPLogger.log(TOOL_LOG_NAME, f"State transition rejected for agent {agent_id}: {rejection_reason}")
    return False, rejection_reason

  checkpoint_snapshot["current_state"] = to_state
  checkpoint_snapshot["previous_state"] = from_state

  update_result = _call_sqlite(
    "UPDATE agents SET current_state = :to_state, updated_at = :updated_at WHERE agent_id = :agent_id AND current_state = :from_state",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "to_state": to_state,
      "from_state": from_state,
      "updated_at": _iso_now(),
    }
  )

  if update_result.get("isError"):
    error_detail = _extract_text_from_mcp_response(update_result)[:300]
    MCPLogger.log(TOOL_LOG_NAME, f"State transition DB update failed for agent {agent_id}: {error_detail}")
    return False, f"DB update failed: {error_detail}"

  update_text = _extract_text_from_mcp_response(update_result)
  if '"rows_modified_by_operation": 0' in update_text:
    return False, f"Stale state: agent {agent_id} is no longer in state '{from_state}' (concurrent modification or agent not found)"

  ckpt_ok, ckpt_err = _write_checkpoint(agent_id, run_id, session_id, step_number, checkpoint_snapshot)
  if not ckpt_ok:
    MCPLogger.log(TOOL_LOG_NAME, f"WARNING: State updated to {to_state} but checkpoint failed for agent {agent_id}: {ckpt_err}")

  _append_session_log_entry(agent_id, run_id, "state_transition", {
    "from_state": from_state,
    "to_state": to_state,
    "step_number": step_number,
  })

  MCPLogger.log(TOOL_LOG_NAME, f"Agent {agent_id} transitioned: {from_state} → {to_state} (run={run_id}, step={step_number})")
  return True, ""


# ===============================================================================
# Execution Receipts (idempotent tool calls — spec §3.3)
#
# Every tool call gets a deterministic execution_id. Before executing, check
# if a receipt exists. If completed: return cached result. If not found:
# create pending receipt, execute, update to completed.
# ===============================================================================

def _compute_execution_receipt_id(run_id: str, step_number: int, tool_name: str, params: Dict[str, Any]) -> str:
  """Compute a deterministic execution ID from run context + tool call details.

  The ID is an MD5 hex digest of the concatenation of run_id, step, tool_name,
  and sorted parameter keys/values. This ensures the same tool call in the same
  position always gets the same ID, enabling idempotent retry after crash.
  """
  sorted_params_str = json.dumps(params, sort_keys=True, default=str)
  hash_input = f"{run_id}:{step_number}:{tool_name}:{sorted_params_str}"
  return hashlib.md5(hash_input.encode()).hexdigest()


def _get_existing_execution_receipt(execution_id: str) -> Optional[Dict[str, Any]]:
  """Check if a completed execution receipt exists for this execution_id.

  Returns:
    The deserialized result dict if a completed receipt exists, or None if
    no receipt exists or the receipt is still pending.
  """
  result = _call_sqlite(
    """SELECT execution_id, run_id, tool_name, status, result_json, created_at, completed_at
    FROM execution_receipts
    WHERE execution_id = :execution_id AND status = 'completed'""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"execution_id": execution_id}
  )

  if result.get("isError"):
    return None

  response_text = _extract_text_from_mcp_response(result)
  if '"data_rows_from_result_set": []' in response_text or '"data_rows_from_result_set": null' in response_text:
    return None

  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if not rows:
      return None
    row = rows[0]
    result_json_str = row.get("result_json", "{}")
    return json.loads(result_json_str) if result_json_str else None
  except (json.JSONDecodeError, KeyError, IndexError, TypeError):
    return None


def _create_pending_execution_receipt(execution_id: str, run_id: str, tool_name: str) -> Tuple[bool, str]:
  """Create a 'pending' execution receipt before executing a tool.

  Returns:
    (True, "") on success.
    (False, error_message) on failure (e.g., duplicate execution_id).
  """
  now = _iso_now()
  from datetime import datetime, timezone, timedelta
  expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

  result = _call_sqlite(
    """INSERT INTO execution_receipts (execution_id, run_id, tool_name, status, created_at, expires_at)
    VALUES (:execution_id, :run_id, :tool_name, 'pending', :created_at, :expires_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "execution_id": execution_id,
      "run_id": run_id,
      "tool_name": tool_name,
      "created_at": now,
      "expires_at": expires_at,
    }
  )

  if result.get("isError"):
    error_detail = _extract_text_from_mcp_response(result)[:300]
    return False, error_detail
  return True, ""


def _complete_execution_receipt(execution_id: str, result_data: Dict[str, Any]) -> Tuple[bool, str]:
  """Mark an execution receipt as completed and store the result.

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  result_json = json.dumps(result_data, default=str)
  now = _iso_now()

  result = _call_sqlite(
    """UPDATE execution_receipts SET status = 'completed', result_json = :result_json, completed_at = :completed_at
    WHERE execution_id = :execution_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "execution_id": execution_id,
      "result_json": result_json,
      "completed_at": now,
    }
  )

  if result.get("isError"):
    error_detail = _extract_text_from_mcp_response(result)[:300]
    return False, error_detail
  return True, ""


# ===============================================================================
# Context Assembly (basic — spec §4, §8)
#
# Assembles the messages array for an LLM call. Phase 1 is simple:
# system_prompt + working_context + conversation history from transcript_entries.
# No compaction, no archival memory, no variable budget allocation yet.
# Character-based estimation (1 token ≈ 4 chars) until tiktoken is added.
# ===============================================================================

def _estimate_token_count_from_characters(text: str) -> int:
  """Rough token estimate: 1 token ≈ 4 characters. Adequate for Phase 1 budgeting."""
  return max(1, len(text) // 4)


# ===============================================================================
# Phase 3: Context Management Pipeline (spec §4, §8)
#
# Prevents long-running agents from exceeding their context window. Components:
#   3.7 — Model-size class detection and adaptation parameters
#   3.1 — Context Budget Planner (constraint-based allocation)
#   3.2 — Tool result budget and spillover
#   3.3 — Microcompact (per-message shrinking, age-based)
#   3.4 — Context collapse (heuristic grouping)
#   3.5 — Auto compact (full LLM summarization)
#   3.6 — Emergency compact (last resort escalation)
#   3.8 — compact_context MCP operation
# ===============================================================================


# ── 3.7: Model-size class detection (spec §4 Step 6) ──

MODEL_SIZE_CLASS_ADAPTATION_PARAMETERS = {
  "tiny": {
    "compaction_trigger_threshold_fraction": 0.60,
    "max_archival_memory_entries_to_inject": 0,
    "history_budget_cap_tokens": None,
    "tool_schema_verbosity": "names_only",
    "recent_messages_protected_from_compaction": 2,
  },
  "small": {
    "compaction_trigger_threshold_fraction": 0.70,
    "max_archival_memory_entries_to_inject": 5,
    "history_budget_cap_tokens": None,
    "tool_schema_verbosity": "concise",
    "recent_messages_protected_from_compaction": 3,
  },
  "medium": {
    "compaction_trigger_threshold_fraction": 0.80,
    "max_archival_memory_entries_to_inject": 10,
    "history_budget_cap_tokens": None,
    "tool_schema_verbosity": "full",
    "recent_messages_protected_from_compaction": 4,
  },
  "large": {
    "compaction_trigger_threshold_fraction": 0.85,
    "max_archival_memory_entries_to_inject": 20,
    "history_budget_cap_tokens": 51200,
    "tool_schema_verbosity": "full",
    "recent_messages_protected_from_compaction": 6,
  },
}

KNOWN_MODEL_CONTEXT_WINDOW_TOKENS = {
  "mlx:cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4": 65536,
  "custom:qwen": 65536,
}

FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS = 4096

_discovered_model_context_window_cache: Dict[str, int] = {}


def _classify_model_size_class_from_context_window(context_window_tokens: int) -> str:
  """Classify a model into a size class based on its total context window.

  Size classes control compaction thresholds, memory budgets, tool schema verbosity,
  and other adaptation parameters throughout the context management pipeline.
  See spec §4 Step 6.

  Boundaries:
    tiny:   < 4096 tokens
    small:  4096 – 16383 tokens
    medium: 16384 – 131071 tokens
    large:  131072+ tokens

  Args:
    context_window_tokens: The model's total context window in tokens.

  Returns:
    One of "tiny", "small", "medium", "large".
  """
  if context_window_tokens < 4096:
    return "tiny"
  if context_window_tokens < 16384:
    return "small"
  if context_window_tokens < 131072:
    return "medium"
  return "large"


def _get_adaptations_for_model_size_class(model_size_class: str) -> Dict[str, Any]:
  """Return the adaptation parameters for a given model size class.

  Returns a copy so callers can safely modify without affecting the global table.
  Falls back to "tiny" adaptations for unrecognized class names.

  Args:
    model_size_class: One of "tiny", "small", "medium", "large".

  Returns:
    Dict with keys: compaction_trigger_threshold_fraction,
    max_archival_memory_entries_to_inject, history_budget_cap_tokens,
    tool_schema_verbosity, recent_messages_protected_from_compaction.
  """
  return dict(MODEL_SIZE_CLASS_ADAPTATION_PARAMETERS.get(
    model_size_class, MODEL_SIZE_CLASS_ADAPTATION_PARAMETERS["tiny"]
  ))


def _extract_context_window_from_model_info_response(response_data: Dict[str, Any]) -> Optional[int]:
  """Extract context window size from an LLM tool model_info response.

  Checks multiple possible field names since different providers report this
  differently. Checks both top-level keys and nested structures (model_info,
  details, model_details, config).

  Args:
    response_data: Parsed JSON from the LLM tool's model_info operation.

  Returns:
    Context window in tokens, or None if not found in any expected location.
  """
  context_window_field_names = [
    "context_length", "context_window", "max_context_length",
    "num_ctx", "max_model_len", "top_provider_context_length",
  ]

  for key in context_window_field_names:
    if key in response_data:
      try:
        val = int(response_data[key])
        if val > 0:
          return val
      except (ValueError, TypeError):
        continue

  for nested_key in ["model_info", "details", "model_details", "config"]:
    if nested_key in response_data and isinstance(response_data[nested_key], dict):
      nested = response_data[nested_key]
      for key in context_window_field_names:
        if key in nested:
          try:
            val = int(nested[key])
            if val > 0:
              return val
          except (ValueError, TypeError):
            continue

  return None


def _discover_model_context_window_tokens(agent_config: Dict[str, Any]) -> int:
  """Query the LLM tool's model_info operation for the model's actual context window.

  Discovery strategy (first match wins):
    1. Return cached value if available (per provider:model pair).
    2. Call llm tool's model_info operation and parse the response.
    3. Check KNOWN_MODEL_CONTEXT_WINDOW_TOKENS for hardcoded values.
    4. Fall back to 4096 tokens (conservative default).

  The cache persists across calls within the same module load. Hot-reload clears it
  since the module-level dict is re-created.

  Args:
    agent_config: Dict containing at least llm_provider and llm_model.

  Returns:
    Context window size in tokens (always > 0).
  """
  provider = agent_config.get("llm_provider", "")
  model = agent_config.get("llm_model", "")
  endpoint_name = agent_config.get("llm_endpoint", "")
  cache_key = f"{endpoint_name or provider}:{model}"

  if cache_key in _discovered_model_context_window_cache:
    return _discovered_model_context_window_cache[cache_key]

  try:
    model_info_params: Dict[str, Any] = {
      "operation": "model_info",
      "tool_unlock_token": "__auto__",
    }
    if endpoint_name:
      model_info_params["endpoint"] = endpoint_name
    if provider:
      model_info_params["provider"] = provider
    if model:
      model_info_params["model"] = model
    _apply_provider_host_params_for_llm_call(provider, model_info_params, endpoint_name)

    result = _call_tool(_suffixed_tool_name("llm"), {"input": model_info_params})

    if not result.get("isError"):
      response_text = _extract_text_from_mcp_response(result)
      try:
        response_data = json.loads(response_text)
        context_window = _extract_context_window_from_model_info_response(response_data)
        if context_window is not None and context_window > 0:
          _discovered_model_context_window_cache[cache_key] = context_window
          MCPLogger.log(TOOL_LOG_NAME, f"Discovered context window for {cache_key}: {context_window} tokens (class={_classify_model_size_class_from_context_window(context_window)})")
          return context_window
      except (json.JSONDecodeError, TypeError):
        pass

  except Exception as exc:
    MCPLogger.log(TOOL_LOG_NAME, f"model_info discovery exception for {cache_key}: {exc}")

  if cache_key in KNOWN_MODEL_CONTEXT_WINDOW_TOKENS:
    known_window = KNOWN_MODEL_CONTEXT_WINDOW_TOKENS[cache_key]
    _discovered_model_context_window_cache[cache_key] = known_window
    MCPLogger.log(TOOL_LOG_NAME, f"Using known context window for {cache_key}: {known_window} tokens (class={_classify_model_size_class_from_context_window(known_window)})")
    return known_window

  MCPLogger.log(TOOL_LOG_NAME, f"Context window discovery failed for {cache_key}, using fallback {FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS}")
  _discovered_model_context_window_cache[cache_key] = FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS
  return FALLBACK_CONTEXT_WINDOW_WHEN_DISCOVERY_FAILS_TOKENS


# ── 3.1: Context Budget Planner (spec §4) ──

def _plan_context_budget(
  agent_config: Dict[str, Any],
  system_content_tokens: int,
  tool_definitions_tokens: int,
  event_payload_tokens: int,
  model_context_window_tokens: int,
) -> Dict[str, Any]:
  """Constraint-based context budget allocation following spec §4.

  Strategy: measure fixed sections → reserve response headroom → calculate
  remaining → allocate variable sections (history, archival memory, recall
  memory) by priority with min/max bounds.

  Args:
    agent_config: Agent config dict (may contain max_response_tokens).
    system_content_tokens: Token count of system prompt + working context.
    tool_definitions_tokens: Token count of tool definition schemas.
    event_payload_tokens: Token count of the current event/user message.
    model_context_window_tokens: Total context window from model discovery.

  Returns:
    Dict with keys:
      model_context_window_tokens, model_size_class,
      fixed_section_tokens (system + tools + event),
      response_headroom_tokens,
      remaining_budget_for_variable_sections_tokens,
      history_budget_tokens (min, max, allocated),
      archival_memory_budget_tokens (min, max, allocated),
      recall_memory_budget_tokens (min, max, allocated),
      compaction_trigger_threshold_tokens,
      total_allocated_tokens,
      needs_emergency_compaction (bool — True if fixed sections alone exceed budget).
  """
  model_size_class = _classify_model_size_class_from_context_window(model_context_window_tokens)
  adaptations = _get_adaptations_for_model_size_class(model_size_class)

  fixed_section_tokens = system_content_tokens + tool_definitions_tokens + event_payload_tokens

  max_response_tokens = agent_config.get("max_response_tokens")
  if max_response_tokens and isinstance(max_response_tokens, int) and max_response_tokens > 0:
    response_headroom_tokens = max_response_tokens
  else:
    response_headroom_tokens = min(4096, int(model_context_window_tokens * 0.20))
  response_headroom_tokens = max(512, response_headroom_tokens)

  remaining_after_fixed_and_headroom = model_context_window_tokens - fixed_section_tokens - response_headroom_tokens

  needs_emergency_compaction_flag = remaining_after_fixed_and_headroom < 0
  if needs_emergency_compaction_flag:
    remaining_after_fixed_and_headroom = 0

  history_min_tokens = int(remaining_after_fixed_and_headroom * 0.30)
  history_max_tokens = int(remaining_after_fixed_and_headroom * 0.80)
  if adaptations["history_budget_cap_tokens"] is not None:
    history_max_tokens = min(history_max_tokens, adaptations["history_budget_cap_tokens"])

  archival_memory_min_tokens = 0
  archival_memory_max_tokens = min(
    int(remaining_after_fixed_and_headroom * 0.25),
    8192
  ) if adaptations["max_archival_memory_entries_to_inject"] > 0 else 0

  recall_memory_min_tokens = 0
  recall_memory_max_tokens = min(int(remaining_after_fixed_and_headroom * 0.15), 4096)

  total_minimums = history_min_tokens + archival_memory_min_tokens + recall_memory_min_tokens
  if total_minimums > remaining_after_fixed_and_headroom and remaining_after_fixed_and_headroom > 0:
    scale_factor = remaining_after_fixed_and_headroom / max(total_minimums, 1)
    history_min_tokens = int(history_min_tokens * scale_factor)
    archival_memory_min_tokens = int(archival_memory_min_tokens * scale_factor)
    recall_memory_min_tokens = int(recall_memory_min_tokens * scale_factor)

  history_allocated = history_min_tokens
  archival_allocated = archival_memory_min_tokens
  recall_allocated = recall_memory_min_tokens
  budget_distributed = history_allocated + archival_allocated + recall_allocated
  budget_remaining_to_distribute = remaining_after_fixed_and_headroom - budget_distributed

  if budget_remaining_to_distribute > 0:
    history_can_take = history_max_tokens - history_allocated
    archival_can_take = archival_memory_max_tokens - archival_allocated
    recall_can_take = recall_memory_max_tokens - recall_allocated

    history_share = min(history_can_take, budget_remaining_to_distribute)
    history_allocated += history_share
    budget_remaining_to_distribute -= history_share

    if budget_remaining_to_distribute > 0:
      archival_share = min(archival_can_take, budget_remaining_to_distribute)
      archival_allocated += archival_share
      budget_remaining_to_distribute -= archival_share

    if budget_remaining_to_distribute > 0:
      recall_share = min(recall_can_take, budget_remaining_to_distribute)
      recall_allocated += recall_share
      budget_remaining_to_distribute -= recall_share

    if budget_remaining_to_distribute > 0:
      history_allocated += budget_remaining_to_distribute
      budget_remaining_to_distribute = 0

  compaction_trigger_threshold_tokens = int(
    model_context_window_tokens * adaptations["compaction_trigger_threshold_fraction"]
  )

  total_allocated = fixed_section_tokens + response_headroom_tokens + history_allocated + archival_allocated + recall_allocated

  return {
    "model_context_window_tokens": model_context_window_tokens,
    "model_size_class": model_size_class,
    "fixed_section_tokens": fixed_section_tokens,
    "system_content_tokens": system_content_tokens,
    "tool_definitions_tokens": tool_definitions_tokens,
    "event_payload_tokens": event_payload_tokens,
    "response_headroom_tokens": response_headroom_tokens,
    "remaining_budget_for_variable_sections_tokens": remaining_after_fixed_and_headroom,
    "history_budget_min_tokens": history_min_tokens,
    "history_budget_max_tokens": history_max_tokens,
    "history_budget_allocated_tokens": history_allocated,
    "archival_memory_budget_min_tokens": archival_memory_min_tokens,
    "archival_memory_budget_max_tokens": archival_memory_max_tokens,
    "archival_memory_budget_allocated_tokens": archival_allocated,
    "recall_memory_budget_min_tokens": recall_memory_min_tokens,
    "recall_memory_budget_max_tokens": recall_memory_max_tokens,
    "recall_memory_budget_allocated_tokens": recall_allocated,
    "compaction_trigger_threshold_tokens": compaction_trigger_threshold_tokens,
    "total_allocated_tokens": total_allocated,
    "needs_emergency_compaction": needs_emergency_compaction_flag,
    "max_archival_memory_entries_to_inject": adaptations["max_archival_memory_entries_to_inject"],
    "tool_schema_verbosity": adaptations["tool_schema_verbosity"],
    "recent_messages_protected_from_compaction": adaptations["recent_messages_protected_from_compaction"],
  }


def _assemble_context_for_agent_run(
  agent_config: Dict[str, Any],
  session_id: str,
  user_message: str,
  max_context_tokens: int = 16384,
  image_data_uri_list: Optional[List[str]] = None,
) -> Tuple[List[Dict], Dict[str, Any]]:
  """Assemble the messages array for an LLM call with budget-aware allocation.

  Phase 3 implementation: uses the Context Budget Planner (spec §4) to
  allocate token budgets based on the model's actual context window and
  model-size class adaptations.

  Args:
    agent_config: The agent's full config row (from agents table).
    session_id: Which conversation session to pull history from.
    user_message: The new user message to include.
    max_context_tokens: Fallback context window (used only if discovery fails
      AND no known model entry exists). Default 16384.
    image_data_uri_list: Optional list of base64 data URIs for images attached
      to the current user message. When present, the user message becomes a
      multimodal content array (OpenAI vision format).

  Returns:
    (messages_list, budget_metadata) where:
    - messages_list is the OpenAI-format messages array [{"role": "...", "content": ...}]
    - budget_metadata is a dict with full budget plan + actual usage.
  """
  system_prompt = agent_config.get("system_prompt", "You are a helpful assistant.")
  working_context = agent_config.get("working_context", "")

  system_content_parts = [system_prompt]
  if working_context:
    system_content_parts.append(f"\n\n## Current Working Context\n{working_context}")

  agent_id_for_directory = agent_config.get("agent_id", "")
  if agent_id_for_directory and "send_to_agent" in PSEUDO_TOOL_NAMES:
    agent_directory_text = _build_agent_directory_for_system_prompt(agent_id_for_directory, agent_config)
    if agent_directory_text:
      system_content_parts.append(agent_directory_text)

  system_content = "".join(system_content_parts)

  model_context_window = _discover_model_context_window_tokens(agent_config)
  if model_context_window <= 0:
    model_context_window = max_context_tokens

  system_tokens = _estimate_token_count_from_characters(system_content)
  user_message_tokens = _estimate_token_count_from_characters(user_message)
  tool_definitions_tokens = 0

  budget = _plan_context_budget(
    agent_config,
    system_content_tokens=system_tokens,
    tool_definitions_tokens=tool_definitions_tokens,
    event_payload_tokens=user_message_tokens,
    model_context_window_tokens=model_context_window,
  )

  history_budget_tokens = budget["history_budget_allocated_tokens"]

  history_messages: List[Dict[str, str]] = []
  history_tokens_used = 0

  if history_budget_tokens > 0:
    agent_id = agent_config.get("agent_id", "")

    compaction_boundary_row = _get_latest_compaction_boundary(agent_id, session_id)
    compaction_boundary_entry_id = None
    if compaction_boundary_row is not None:
      raw_boundary_entry_id = compaction_boundary_row.get("last_compacted_entry_id")
      try:
        compaction_boundary_entry_id = int(raw_boundary_entry_id) if raw_boundary_entry_id is not None else None
      except (TypeError, ValueError):
        compaction_boundary_entry_id = None

    # Only conversational roles reload as history; tool_spillover and
    # mid-conversation system rows stay retrievable via recall search only.
    if compaction_boundary_entry_id is not None:
      history_sql = """SELECT role, content FROM transcript_entries
        WHERE agent_id = :agent_id AND session_id = :session_id
          AND entry_id > :boundary_entry_id
          AND role IN ('user','assistant','tool')
        ORDER BY entry_id ASC"""
      history_bindings: Dict[str, Any] = {
        "agent_id": agent_id,
        "session_id": session_id,
        "boundary_entry_id": compaction_boundary_entry_id,
      }
    else:
      history_sql = """SELECT role, content FROM transcript_entries
        WHERE agent_id = :agent_id AND session_id = :session_id
          AND role IN ('user','assistant','tool')
        ORDER BY entry_id ASC"""
      history_bindings = {
        "agent_id": agent_id,
        "session_id": session_id,
      }

    if compaction_boundary_row is not None:
      boundary_summary_text = compaction_boundary_row.get("summary_text") or ""
      if boundary_summary_text:
        summary_message_content = f"[Previous conversation summary]\n{boundary_summary_text}"
        history_messages.append({"role": "system", "content": summary_message_content})
        history_tokens_used += _estimate_token_count_from_characters(summary_message_content)

    history_result = _call_sqlite(
      history_sql,
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings=history_bindings,
    )

    if not history_result.get("isError"):
      response_text = _extract_text_from_mcp_response(history_result)
      try:
        response_data = json.loads(response_text)
        rows = response_data.get("data_rows_from_result_set", [])
        for row in rows:
          msg_role = row.get("role", "user")
          msg_content = row.get("content", "")
          msg_tokens = _estimate_token_count_from_characters(msg_content)
          if history_tokens_used + msg_tokens > history_budget_tokens:
            break
          history_messages.append({"role": msg_role, "content": msg_content})
          history_tokens_used += msg_tokens
      except (json.JSONDecodeError, KeyError, TypeError):
        pass

  if history_tokens_used > history_budget_tokens and history_messages:
    history_messages = _apply_microcompact_to_history_messages(
      history_messages, history_budget_tokens, history_tokens_used,
    )
    history_tokens_used = sum(
      _estimate_token_count_from_characters(m.get("content", ""))
      for m in history_messages
    )

  if history_tokens_used > history_budget_tokens and history_messages:
    history_messages = _apply_context_collapse_to_history_messages(
      history_messages, history_budget_tokens, history_tokens_used,
    )
    history_tokens_used = sum(
      _estimate_token_count_from_characters(m.get("content", ""))
      for m in history_messages
    )

  if history_tokens_used > budget.get("compaction_trigger_threshold_tokens", history_budget_tokens) and history_messages:
    agent_id = agent_config.get("agent_id", "")
    history_messages, history_tokens_used, _compaction_was_performed = _run_auto_compact(
      agent_id, session_id, agent_config,
      history_messages, history_tokens_used, history_budget_tokens,
    )

  messages = [{"role": "system", "content": system_content}]

  archival_memory_tokens_used = 0
  archival_memory_entries_injected = 0
  archival_memory_budget_tokens = budget.get("archival_memory_budget_allocated_tokens", 0)
  max_archival_entries = budget.get("max_archival_memory_entries_to_inject", 0)

  if max_archival_entries > 0 and archival_memory_budget_tokens > 0:
    agent_id = agent_config.get("agent_id", "")
    if agent_id and user_message:
      search_ok, archival_results, _search_err = _search_archival_memory(
        agent_id, user_message, limit=max_archival_entries,
      )
      if search_ok and archival_results:
        archival_lines = []
        for mem_row in archival_results:
          mem_content = mem_row.get("content", "")
          mem_type = mem_row.get("memory_type", "fact")
          archival_lines.append(f"- [{mem_type}] {mem_content}")

        archival_block = "<agent-memory type=\"archival\">\n" + "\n".join(archival_lines) + "\n</agent-memory>"
        archival_block_tokens = _estimate_token_count_from_characters(archival_block)

        if archival_block_tokens <= archival_memory_budget_tokens:
          messages.append({"role": "user", "content": archival_block})
          archival_memory_tokens_used = archival_block_tokens
          archival_memory_entries_injected = len(archival_results)

  messages.extend(history_messages)

  endpoint_supports_vision = True
  llm_endpoint_name = agent_config.get("llm_endpoint", "")
  if llm_endpoint_name and image_data_uri_list:
    vision_ok, _ = _check_endpoint_has_required_capabilities(llm_endpoint_name, {"vision_input"})
    if not vision_ok:
      endpoint_supports_vision = False
      MCPLogger.log(TOOL_LOG_NAME,
        f"Endpoint '{llm_endpoint_name}' lacks vision_input capability; "
        f"stripping {len(image_data_uri_list)} image(s) from context for agent {agent_config.get('agent_id', '?')}")

  images_actually_included: List[str] = image_data_uri_list if (image_data_uri_list and endpoint_supports_vision) else []
  if images_actually_included:
    multimodal_content_parts: List[Dict[str, Any]] = [{"type": "text", "text": user_message}]
    for image_uri in images_actually_included:
      multimodal_content_parts.append({"type": "image_url", "image_url": {"url": image_uri}})
    messages.append({"role": "user", "content": multimodal_content_parts})
  else:
    messages.append({"role": "user", "content": user_message})

  budget["history_messages_count"] = len(history_messages)
  budget["history_tokens_actual"] = history_tokens_used
  budget["archival_memory_tokens_actual"] = archival_memory_tokens_used
  budget["archival_memory_entries_injected"] = archival_memory_entries_injected
  budget["total_tokens_estimate"] = system_tokens + archival_memory_tokens_used + history_tokens_used + user_message_tokens
  budget["images_attached_count"] = len(images_actually_included)
  budget["images_stripped_no_vision_support"] = len(image_data_uri_list or []) - len(images_actually_included)

  return messages, budget


def _get_latest_compaction_boundary(agent_id: str, session_id: str) -> Optional[Dict[str, Any]]:
  """Look up the most recent compaction boundary row for this agent+session.

  Returns:
    The boundary row dict (with last_compacted_entry_id and summary_text),
    or None if no compaction has occurred for this agent+session.
  """
  result = _call_sqlite(
    """SELECT boundary_id, last_compacted_entry_id, summary_text FROM compaction_boundaries
    WHERE agent_id = :agent_id AND session_id = :session_id
    ORDER BY boundary_id DESC LIMIT 1""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "session_id": session_id},
  )

  if result.get("isError"):
    return None

  rows = _parse_rows_from_mcp_query_response(result)
  return rows[0] if rows else None


def _get_latest_compaction_boundary_entry_id(agent_id: str, session_id: str) -> Optional[int]:
  """Return the max compacted transcript entry_id from the latest boundary.

  Messages with entry_id greater than this value are loaded as live history;
  earlier messages are represented by the boundary's summary_text.

  Returns:
    The entry_id of the last compacted transcript entry, or None if no
    compaction has occurred (or the boundary predates the
    last_compacted_entry_id column).
  """
  boundary_row = _get_latest_compaction_boundary(agent_id, session_id)
  if boundary_row is None:
    return None
  last_compacted_entry_id = boundary_row.get("last_compacted_entry_id")
  try:
    return int(last_compacted_entry_id) if last_compacted_entry_id is not None else None
  except (TypeError, ValueError):
    return None


# ── 3.2: Tool result budget & spillover (spec §8 Stage 3) ──

TOOL_RESULT_SPILLOVER_THRESHOLD_CHARACTERS = 8000
TOOL_RESULT_SPILLOVER_PREVIEW_HEAD_LINES = 20
TOOL_RESULT_SPILLOVER_PREVIEW_TAIL_LINES = 10


def _spillover_tool_result_if_oversized(
  agent_id: str,
  session_id: str,
  tool_name: str,
  tool_result_text: str,
  execution_id: str,
) -> str:
  """Check if a tool result exceeds the spillover threshold; if so, store the full
  result and return a truncated preview with retrieval instructions.

  The full result is stored in the transcript_entries table with role='tool_spillover'
  so it can be retrieved later by recall_memory_search (Phase 4).

  If the result is under the threshold, it is returned unchanged.

  Args:
    agent_id: The agent that executed the tool.
    session_id: Current session for transcript storage.
    tool_name: Name of the tool that produced this result.
    tool_result_text: The full text of the tool result.
    execution_id: The execution receipt ID (for cross-referencing).

  Returns:
    The original text (if under threshold) or a truncated preview string.
  """
  if len(tool_result_text) <= TOOL_RESULT_SPILLOVER_THRESHOLD_CHARACTERS:
    return tool_result_text

  full_length_chars = len(tool_result_text)
  full_length_tokens_estimate = _estimate_token_count_from_characters(tool_result_text)

  _save_transcript_entry(
    agent_id, session_id, "tool_spillover",
    tool_result_text,
    tool_name=tool_name,
  )

  lines = tool_result_text.split("\n")
  head_lines = lines[:TOOL_RESULT_SPILLOVER_PREVIEW_HEAD_LINES]
  tail_lines = lines[-TOOL_RESULT_SPILLOVER_PREVIEW_TAIL_LINES:] if len(lines) > TOOL_RESULT_SPILLOVER_PREVIEW_HEAD_LINES + TOOL_RESULT_SPILLOVER_PREVIEW_TAIL_LINES else []

  error_lines_from_full_result = []
  error_keywords = ["error", "Error", "ERROR", "exception", "Exception", "EXCEPTION", "Traceback", "FAILED", "fail", "Fail"]
  for line in lines:
    if any(kw in line for kw in error_keywords):
      if line not in head_lines and line not in tail_lines:
        error_lines_from_full_result.append(line)
      if len(error_lines_from_full_result) >= 5:
        break

  preview_parts = [
    f"[Result truncated — {full_length_chars:,} chars (~{full_length_tokens_estimate:,} tokens). Full output stored as tool_spillover. Use recall_memory_search to retrieve specific sections.]",
    "",
    "--- First lines ---",
    "\n".join(head_lines),
  ]

  if error_lines_from_full_result:
    preview_parts.extend([
      "",
      "--- Error lines found ---",
      "\n".join(error_lines_from_full_result),
    ])

  if tail_lines:
    preview_parts.extend([
      "",
      "--- Last lines ---",
      "\n".join(tail_lines),
    ])

  return "\n".join(preview_parts)


# ── 3.3: Microcompact — age-based per-message shrinking (spec §8 Stage 4) ──

MICROCOMPACT_RECENT_MESSAGES_PROTECTED_COUNT = 4
MICROCOMPACT_AGE_TIERS = [
  {"max_age_messages_from_end": 8, "max_tool_result_characters": 4000, "label": "recent"},
  {"max_age_messages_from_end": 20, "max_tool_result_characters": 1500, "label": "mid"},
  {"max_age_messages_from_end": 999999, "max_tool_result_characters": 500, "label": "old"},
]

MICROCOMPACT_PRESERVE_PATTERNS = [
  "error", "Error", "ERROR", "exception", "Exception",
  "Traceback", "FAILED", "fail", "Fail",
  "warning", "Warning", "WARNING",
  "File ", "line ", "def ", "class ",
]


def _microcompact_single_message(
  message: Dict[str, Any],
  messages_from_end: int,
  history_budget_tokens: int,
  history_actual_tokens: int,
) -> Dict[str, Any]:
  """Apply age-based shrinking to a single message if it is a tool result.

  Non-tool messages (user, assistant, system) are returned unchanged.
  The most recent MICROCOMPACT_RECENT_MESSAGES_PROTECTED_COUNT messages are never touched.

  Shrinking strategy by age tier:
    - recent (<=8 from end): truncate tool results to 4000 chars
    - mid (<=20 from end): truncate to 1500 chars
    - old (>20 from end): truncate to 500 chars

  Within each tier, preserved content includes: error lines, filenames, function
  signatures, and the first/last lines of the result.

  Args:
    message: A message dict with 'role' and 'content'.
    messages_from_end: How many messages from the end of history (0 = last).
    history_budget_tokens: Total tokens allocated for history.
    history_actual_tokens: Current total tokens consumed by history.

  Returns:
    The message dict, potentially with content truncated.
  """
  if messages_from_end < MICROCOMPACT_RECENT_MESSAGES_PROTECTED_COUNT:
    return message

  role = message.get("role", "")
  if role not in ("tool",):
    return message

  content = message.get("content", "")
  if not content:
    return message

  max_chars = MICROCOMPACT_AGE_TIERS[-1]["max_tool_result_characters"]
  for tier in MICROCOMPACT_AGE_TIERS:
    if messages_from_end <= tier["max_age_messages_from_end"]:
      max_chars = tier["max_tool_result_characters"]
      break

  if len(content) <= max_chars:
    return message

  lines = content.split("\n")
  preserved_lines_from_content = []
  for line in lines:
    if any(pattern in line for pattern in MICROCOMPACT_PRESERVE_PATTERNS):
      preserved_lines_from_content.append(line)
    if len(preserved_lines_from_content) >= 10:
      break

  head_char_budget = max_chars // 2
  tail_char_budget = max_chars // 4
  preserved_char_budget = max_chars // 4

  head_text = content[:head_char_budget]
  tail_text = content[-tail_char_budget:] if tail_char_budget > 0 else ""
  preserved_text = "\n".join(preserved_lines_from_content)[:preserved_char_budget]

  compacted_parts = [head_text]
  if preserved_text and preserved_text not in head_text:
    compacted_parts.append(f"\n[...preserved lines...]\n{preserved_text}")
  if tail_text:
    compacted_parts.append(f"\n[...truncated {len(content) - max_chars:,} chars...]\n{tail_text}")

  compacted_content = "".join(compacted_parts)

  result = dict(message)
  result["content"] = compacted_content
  return result


def _apply_microcompact_to_history_messages(
  messages: List[Dict[str, Any]],
  history_budget_tokens: int,
  history_actual_tokens: int,
) -> List[Dict[str, Any]]:
  """Apply microcompact to all messages in the conversation history.

  Iterates from oldest to newest, applying age-based shrinking to tool results.
  Messages are indexed from the end so that the most recent ones are protected.

  Args:
    messages: The full list of history messages (oldest first).
    history_budget_tokens: Budget for history from the context planner.
    history_actual_tokens: Current estimated token count of history.

  Returns:
    A new list with the same messages, some with truncated content.
  """
  if history_actual_tokens <= history_budget_tokens:
    return messages

  total_messages = len(messages)
  result = []
  for idx, msg in enumerate(messages):
    messages_from_end = total_messages - 1 - idx
    compacted = _microcompact_single_message(
      msg, messages_from_end, history_budget_tokens, history_actual_tokens,
    )
    result.append(compacted)

  return result


# ── 3.4: Context Collapse — heuristic grouping of old tool-use sequences (spec §8 Stage 5) ──

CONTEXT_COLLAPSE_MIN_AGE_MESSAGES_FROM_END = 8
CONTEXT_COLLAPSE_MAX_COLLAPSED_SUMMARY_CHARACTERS = 200


def _collapse_tool_call_result_pair_to_summary(
  assistant_msg: Dict[str, Any],
  tool_msg: Dict[str, Any],
) -> str:
  """Condense an assistant tool_call + tool result pair into a one-line summary.

  The summary format is:
    Called {tool_name}({brief_params}) → {brief_result_or_error}

  Args:
    assistant_msg: The assistant message that proposed the tool call.
    tool_msg: The tool result message.

  Returns:
    A concise single-line summary of the tool invocation.
  """
  content = assistant_msg.get("content", "")
  tool_name = "unknown_tool"
  brief_params = ""

  if "tool_calls" in assistant_msg:
    tc_list = assistant_msg["tool_calls"]
    if tc_list and isinstance(tc_list, list):
      tc = tc_list[0]
      func = tc.get("function", {})
      tool_name = func.get("name", "unknown_tool")
      args_str = func.get("arguments", "")
      if isinstance(args_str, str) and len(args_str) > 80:
        brief_params = args_str[:77] + "..."
      elif isinstance(args_str, str):
        brief_params = args_str
  elif content:
    for prefix in ["Calling ", "Using ", "Running "]:
      if content.startswith(prefix):
        tool_name = content[len(prefix):].split("(")[0].split(" ")[0][:40]
        break

  tool_result = tool_msg.get("content", "")
  if not tool_result:
    brief_result = "(empty)"
  elif len(tool_result) <= 100:
    brief_result = tool_result.replace("\n", " ")
  else:
    first_line = tool_result.split("\n")[0][:80]
    brief_result = first_line + f"... ({len(tool_result):,} chars)"

  summary = f"Called {tool_name}({brief_params}) → {brief_result}"
  return summary[:CONTEXT_COLLAPSE_MAX_COLLAPSED_SUMMARY_CHARACTERS]


def _apply_context_collapse_to_history_messages(
  messages: List[Dict[str, Any]],
  history_budget_tokens: int,
  history_actual_tokens: int,
) -> List[Dict[str, Any]]:
  """Collapse old tool-call/result pairs into compact summaries.

  This is a read-time projection: original messages in the DB are not mutated.
  Only pairs older than CONTEXT_COLLAPSE_MIN_AGE_MESSAGES_FROM_END from the
  end of the history are collapsed.

  Pattern matched: consecutive (assistant with tool_calls, tool result) pairs.
  These get replaced with a single assistant message containing the summary.

  Args:
    messages: History messages in chronological order.
    history_budget_tokens: The allocated token budget.
    history_actual_tokens: Current estimated tokens.

  Returns:
    A new list with collapsed messages where applicable.
  """
  if history_actual_tokens <= history_budget_tokens:
    return messages

  total = len(messages)
  collapse_boundary = total - CONTEXT_COLLAPSE_MIN_AGE_MESSAGES_FROM_END

  result = []
  idx = 0
  while idx < total:
    msg = messages[idx]

    if idx < collapse_boundary and idx + 1 < total:
      next_msg = messages[idx + 1]
      current_is_assistant = msg.get("role") == "assistant"
      next_is_tool = next_msg.get("role") == "tool"

      current_has_tool_calls = bool(msg.get("tool_calls"))
      current_content_suggests_tool = any(
        msg.get("content", "").startswith(p) for p in ["Calling ", "Using ", "Running "]
      )

      if current_is_assistant and next_is_tool and (current_has_tool_calls or current_content_suggests_tool):
        summary = _collapse_tool_call_result_pair_to_summary(msg, next_msg)
        result.append({"role": "assistant", "content": f"[collapsed] {summary}"})
        idx += 2
        continue

    result.append(msg)
    idx += 1

  return result


# ── 3.5: Auto Compact — full LLM summarization (spec §8 Stage 6) ──

AUTO_COMPACT_SUMMARY_PROMPT_TEMPLATE = """You are a conversation summarizer for an AI agent's memory system.

Summarize the following conversation history into a concise but information-dense summary.
You MUST preserve:
- File names, paths, code snippets, function signatures
- Configuration values, URLs, error messages
- User instructions and preferences
- Unfinished tasks and in-progress work
- Key decisions and their rationale
- Tool names and what they returned (especially errors)

Be concise but do NOT lose critical details. The summary replaces the original messages.

Conversation to summarize:
{conversation_text}

Summary:"""


def _format_messages_for_compaction_prompt(messages: List[Dict[str, Any]]) -> str:
  """Format a list of messages into a readable text block for the compaction LLM.

  Args:
    messages: The messages to format.

  Returns:
    A formatted string with role labels and content.
  """
  parts = []
  for msg in messages:
    role = msg.get("role", "unknown").upper()
    content = msg.get("content", "")[:2000]
    parts.append(f"[{role}]: {content}")
  return "\n\n".join(parts)


def _run_auto_compact(
  agent_id: str,
  session_id: str,
  agent_config: Dict[str, Any],
  history_messages: List[Dict[str, Any]],
  history_tokens_used: int,
  history_budget_tokens: int,
) -> Tuple[List[Dict[str, Any]], int, bool]:
  """Run full LLM summarization on conversation history.

  Uses the agent's compaction_model to produce a summary of all messages
  except the most recent N (protected). The summary replaces the older
  messages, and a compaction boundary is written to the database so that
  future context assembly starts from the summary.

  This function:
  1. Separates protected (recent) and compactable (old) messages
  2. Formats compactable messages into a prompt
  3. Calls the compaction LLM
  4. Writes the compaction boundary to the database
  5. Logs the compaction event to the session log
  6. Returns the new message list with summary replacing old messages

  Args:
    agent_id: The agent performing compaction.
    session_id: Current session.
    agent_config: Full agent config for LLM settings.
    history_messages: All history messages (oldest first).
    history_tokens_used: Current token estimate for history.
    history_budget_tokens: Target budget for history.

  Returns:
    (new_messages, new_token_count, compaction_was_performed)
  """
  recent_protected_count = MICROCOMPACT_RECENT_MESSAGES_PROTECTED_COUNT
  if len(history_messages) <= recent_protected_count:
    return history_messages, history_tokens_used, False

  compactable_messages = history_messages[:-recent_protected_count]
  protected_messages = history_messages[-recent_protected_count:]

  if not compactable_messages:
    return history_messages, history_tokens_used, False

  conversation_text = _format_messages_for_compaction_prompt(compactable_messages)

  compaction_prompt = AUTO_COMPACT_SUMMARY_PROMPT_TEMPLATE.format(
    conversation_text=conversation_text
  )

  compaction_endpoint = agent_config.get("compaction_endpoint", "") or agent_config.get("llm_endpoint", "")
  compaction_provider = agent_config.get("compaction_provider", "") or agent_config.get("llm_provider", "")
  compaction_model = agent_config.get("compaction_model", "") or agent_config.get("llm_model", "")

  llm_params: Dict[str, Any] = {
    "operation": "chat",
    "messages": [
      {"role": "system", "content": "You are a precise conversation summarizer. Preserve all critical details."},
      {"role": "user", "content": compaction_prompt},
    ],
    "temperature": 0.3,
    "max_tokens": 2048,
    "enable_thinking": False,
    "tool_unlock_token": "__auto__",
  }

  if compaction_endpoint:
    llm_params["endpoint"] = compaction_endpoint
  if compaction_provider:
    llm_params["provider"] = compaction_provider
  if compaction_model:
    llm_params["model"] = compaction_model
  _apply_provider_host_params_for_llm_call(compaction_provider, llm_params, compaction_endpoint)

  llm_result = _call_tool(_suffixed_tool_name("llm"), {"input": llm_params})

  if llm_result.get("isError"):
    return history_messages, history_tokens_used, False

  llm_response_text = _extract_text_from_mcp_response(llm_result)
  try:
    llm_response_data = json.loads(llm_response_text)
    choices = llm_response_data.get("choices", [])
    if choices:
      summary_text = choices[0].get("message", {}).get("content", "")
    elif "content" in llm_response_data:
      summary_text = llm_response_data["content"]
    else:
      summary_text = llm_response_text
  except json.JSONDecodeError:
    summary_text = llm_response_text

  if not summary_text or len(summary_text.strip()) < 10:
    return history_messages, history_tokens_used, False

  tokens_before = history_tokens_used
  summary_tokens = _estimate_token_count_from_characters(summary_text)

  # Boundary = the newest compacted transcript row: the row that has exactly
  # recent_protected_count conversational rows after it at compaction time.
  last_compacted_entry_id = None
  boundary_lookup = _call_sqlite(
    """SELECT entry_id FROM transcript_entries
    WHERE agent_id = :agent_id AND session_id = :session_id
      AND role IN ('user','assistant','tool')
    ORDER BY entry_id DESC LIMIT 1 OFFSET :protected_count""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "session_id": session_id, "protected_count": recent_protected_count},
  )
  boundary_rows = _parse_rows_from_mcp_query_response(boundary_lookup)
  if boundary_rows:
    try:
      last_compacted_entry_id = int(boundary_rows[0].get("entry_id"))
    except (TypeError, ValueError):
      last_compacted_entry_id = None

  now = _iso_now()
  _call_sqlite(
    """INSERT INTO compaction_boundaries
    (agent_id, session_id, summary_text, messages_compacted_count, tokens_before, tokens_after, last_compacted_entry_id, compacted_at)
    VALUES (:agent_id, :session_id, :summary_text, :messages_compacted_count, :tokens_before, :tokens_after, :last_compacted_entry_id, :compacted_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "session_id": session_id,
      "summary_text": summary_text,
      "messages_compacted_count": len(compactable_messages),
      "tokens_before": tokens_before,
      "tokens_after": summary_tokens,
      "last_compacted_entry_id": last_compacted_entry_id,
      "compacted_at": now,
    },
  )

  _append_session_log_entry(agent_id, "", "context_compacted", {
    "session_id": session_id,
    "messages_compacted": len(compactable_messages),
    "tokens_before": tokens_before,
    "tokens_after": summary_tokens,
    "summary_preview": summary_text[:200],
  })

  _extract_session_memories_from_compacted_conversation(
    agent_id, compactable_messages, summary_text,
  )

  new_messages = [{"role": "system", "content": f"[Previous conversation summary]\n{summary_text}"}]
  new_messages.extend(protected_messages)

  new_tokens = sum(
    _estimate_token_count_from_characters(m.get("content", ""))
    for m in new_messages
  )

  return new_messages, new_tokens, True


# ── 4.6: Session memory auto-extraction (post-compaction side effect) ──

SESSION_MEMORY_EXTRACTION_PROMPT_TEMPLATE = """Extract key facts, user preferences, decisions, and important information from this conversation as individual memory entries.

For each memory, output one line in this format:
TYPE: content

Where TYPE is one of: fact, preference, project_knowledge, decision, task, rule

Only extract genuinely useful information that would help in future conversations. Skip trivial or transient details.

Conversation:
{conversation_text}"""


def _extract_session_memories_from_compacted_conversation(
  agent_id: str,
  compacted_messages: List[Dict[str, Any]],
  summary_text: str,
) -> int:
  """Extract key facts from a compacted conversation and insert as archival memories.

  Called as a post-compaction side effect by _run_auto_compact(). Uses the
  compaction model to identify facts, preferences, and decisions worth
  persisting to archival memory.

  Args:
    agent_id: The agent whose conversation was compacted.
    compacted_messages: The messages that were compacted (for extraction).
    summary_text: The compaction summary (used as fallback input).

  Returns:
    Number of memories extracted and inserted.
  """
  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return 0

  conversation_text = _format_messages_for_compaction_prompt(compacted_messages)
  if len(conversation_text) < 50:
    conversation_text = summary_text
  if len(conversation_text) < 50:
    return 0

  extraction_prompt = SESSION_MEMORY_EXTRACTION_PROMPT_TEMPLATE.format(
    conversation_text=conversation_text[:8000],
  )

  compaction_endpoint = agent_config.get("compaction_endpoint", "")
  compaction_provider = agent_config.get("compaction_provider", "")
  compaction_model = agent_config.get("compaction_model", "")

  llm_params: Dict[str, Any] = {
    "operation": "chat",
    "messages": [
      {"role": "system", "content": "You extract structured memories from conversations. Output one memory per line in 'TYPE: content' format."},
      {"role": "user", "content": extraction_prompt},
    ],
    "temperature": 0.3,
    "max_tokens": 1024,
    "enable_thinking": False,
    "tool_unlock_token": "__auto__",
  }

  if compaction_endpoint:
    llm_params["endpoint"] = compaction_endpoint
  if compaction_provider:
    llm_params["provider"] = compaction_provider
  if compaction_model:
    llm_params["model"] = compaction_model
  _apply_provider_host_params_for_llm_call(compaction_provider, llm_params, compaction_endpoint)

  llm_result = _call_tool(_suffixed_tool_name("llm"), {"input": llm_params})

  if llm_result.get("isError"):
    MCPLogger.log(TOOL_LOG_NAME, f"Session memory extraction LLM call failed for agent {agent_id}")
    return 0

  llm_response_text = _extract_text_from_mcp_response(llm_result)
  try:
    llm_response_data = json.loads(llm_response_text)
    choices = llm_response_data.get("choices", [])
    if choices:
      extraction_text = choices[0].get("message", {}).get("content", "")
    elif "content" in llm_response_data:
      extraction_text = llm_response_data["content"]
    else:
      extraction_text = llm_response_text
  except json.JSONDecodeError:
    extraction_text = llm_response_text

  if not extraction_text or len(extraction_text.strip()) < 5:
    return 0

  memories_inserted_count = 0
  valid_type_prefixes = {t.lower() for t in ARCHIVAL_MEMORY_VALID_TYPES}

  for line in extraction_text.strip().split("\n"):
    line = line.strip()
    if not line or ":" not in line:
      continue

    colon_pos = line.index(":")
    raw_type = line[:colon_pos].strip().lower().replace(" ", "_")
    content = line[colon_pos + 1:].strip()

    if not content or len(content) < 5:
      continue

    if raw_type in valid_type_prefixes:
      memory_type = raw_type
    else:
      memory_type = "fact"

    ok, _mem_id, _err = _insert_archival_memory(
      agent_id, content, memory_type,
      importance_score=0.5, confidence_score=0.6,
      source_run_id=None,
    )
    if ok:
      memories_inserted_count += 1

  if memories_inserted_count > 0:
    _append_session_log_entry(agent_id, "", "memory_inserted", {
      "source": "session_memory_auto_extraction",
      "memories_extracted_count": memories_inserted_count,
    })

  return memories_inserted_count


# ── 3.6: Emergency Compact — escalation chain for context-too-long (spec §8 Stage 7) ──

EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE = 3
EMERGENCY_COMPACT_STRIP_TOOL_RESULTS_OLDER_THAN_TURNS = 2

CONTEXT_TOO_LONG_ERROR_PATTERNS = [
  "context_length_exceeded",
  "context length",
  "maximum context",
  "too many tokens",
  "token limit",
  "max_tokens",
  "input too long",
  "prompt is too long",
  "Request too large",
]


def _detect_context_too_long_error(error_text: str) -> bool:
  """Check if an LLM error indicates the context window was exceeded.

  Args:
    error_text: The error message from the LLM call.

  Returns:
    True if the error matches a known context-too-long pattern.
  """
  error_lower = error_text.lower()
  return any(pattern.lower() in error_lower for pattern in CONTEXT_TOO_LONG_ERROR_PATTERNS)


def _emergency_strip_old_tool_results(
  messages: List[Dict[str, Any]],
  keep_recent_turns: int = 2,
) -> List[Dict[str, Any]]:
  """Strip tool results from all but the most recent N turns.

  A 'turn' is a user→assistant exchange. This is the least destructive
  emergency compaction: it removes bulk from tool outputs while preserving
  the conversation flow.

  Args:
    messages: The assembled messages (system + history + user).
    keep_recent_turns: How many recent turn-pairs to preserve fully.

  Returns:
    A new message list with old tool results replaced by "[tool result removed — emergency compact]".
  """
  tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

  if not tool_indices:
    return messages

  protected_from_index = max(0, len(messages) - (keep_recent_turns * 3))

  result = []
  for idx, msg in enumerate(messages):
    if msg.get("role") == "tool" and idx < protected_from_index:
      result.append({
        "role": msg.get("role"),
        "tool_call_id": msg.get("tool_call_id", ""),
        "content": "[tool result removed — emergency compact]",
      })
    else:
      result.append(msg)

  return result


def _emergency_compact_escalation(
  agent_id: str,
  session_id: str,
  run_id: str,
  agent_config: Dict[str, Any],
  assembled_messages: List[Dict[str, Any]],
  consecutive_emergency_compact_count: int,
) -> Tuple[List[Dict[str, Any]], int, bool]:
  """Execute the emergency compaction escalation chain.

  Escalation levels:
  1. Strip all tool results older than 2 turns
  2. Run aggressive auto compact on remaining history
  3. Remove all non-system, non-user-latest messages except most recent 2

  Circuit breaker: if consecutive_emergency_compact_count >=
  EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE, return failure signal.

  Args:
    agent_id: Agent performing compaction.
    session_id: Current session.
    run_id: Current run for logging.
    agent_config: Full agent config.
    assembled_messages: The current messages array that was too large.
    consecutive_emergency_compact_count: How many consecutive emergencies so far.

  Returns:
    (new_messages, new_consecutive_count, circuit_breaker_tripped)
  """
  new_count = consecutive_emergency_compact_count + 1

  if new_count >= EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE:
    _append_session_log_entry(agent_id, run_id, "emergency_compact_circuit_breaker", {
      "session_id": session_id,
      "consecutive_count": new_count,
      "max_allowed": EMERGENCY_COMPACT_CIRCUIT_BREAKER_MAX_CONSECUTIVE,
    })
    return assembled_messages, new_count, True

  _append_session_log_entry(agent_id, run_id, "emergency_compact_started", {
    "session_id": session_id,
    "escalation_level": new_count,
    "message_count_before": len(assembled_messages),
  })

  compacted = _emergency_strip_old_tool_results(
    assembled_messages,
    keep_recent_turns=EMERGENCY_COMPACT_STRIP_TOOL_RESULTS_OLDER_THAN_TURNS,
  )

  total_tokens = sum(
    _estimate_token_count_from_characters(m.get("content", ""))
    for m in compacted
  )

  _append_session_log_entry(agent_id, run_id, "emergency_compact_completed", {
    "session_id": session_id,
    "escalation_level": new_count,
    "message_count_after": len(compacted),
    "tokens_after": total_tokens,
  })

  return compacted, new_count, False


# ===============================================================================
# Phase 4: Tiered Memory System (spec §3.5)
#
# Tier 2: Archival Memory — persistent knowledge stored with embeddings
# for semantic search. The sqlite tool handles embedding generation via
# its local Qwen3-Embedding-0.6B model. Agent.py NEVER calls the
# embedding model directly.
# ===============================================================================

ARCHIVAL_MEMORY_VALID_TYPES = {
  "fact", "preference", "project_knowledge", "decision", "task", "rule",
}


def _insert_archival_memory(
  agent_id: str,
  content: str,
  memory_type: str = "fact",
  importance_score: float = 0.5,
  confidence_score: float = 0.8,
  source_run_id: Optional[str] = None,
) -> Tuple[bool, str, Optional[str]]:
  """Insert a new archival memory with auto-generated embedding.

  Uses the sqlite tool's generate_embedding() SQL UDF to create a 1024-dim
  vector from the content text. The embedding is stored as a BLOB in the
  memory_entries table for later semantic search via vec_distance_cosine().

  Args:
    agent_id: Which agent owns this memory.
    content: The text to remember (also used to generate the embedding).
    memory_type: One of ARCHIVAL_MEMORY_VALID_TYPES.
    importance_score: 0.0–1.0, how important this memory is.
    confidence_score: 0.0–1.0, how confident the agent is in this fact.
    source_run_id: The run_id that created this memory (for provenance).

  Returns:
    (True, memory_id, None) on success.
    (False, "", error_message) on failure.
  """
  if memory_type not in ARCHIVAL_MEMORY_VALID_TYPES:
    return False, "", f"Invalid memory_type '{memory_type}'. Must be one of: {sorted(ARCHIVAL_MEMORY_VALID_TYPES)}"

  memory_id = f"mem-{hashlib.md5(f'{agent_id}:{content}:{time.time()}'.encode()).hexdigest()[:12]}"
  now = _iso_now()

  result = _call_sqlite(
    """INSERT INTO memory_entries
    (memory_id, agent_id, memory_type, content, embedding,
     importance_score, confidence_score, source_run_id,
     access_count, last_accessed_at, created_at, updated_at)
    VALUES (:memory_id, :agent_id, :memory_type, :content,
     vec_f32(generate_embedding(:content)),
     :importance_score, :confidence_score, :source_run_id,
     0, :now, :now, :now)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "memory_id": memory_id,
      "agent_id": agent_id,
      "memory_type": memory_type,
      "content": content,
      "importance_score": importance_score,
      "confidence_score": confidence_score,
      "source_run_id": source_run_id,
      "now": now,
    },
  )

  if result.get("isError"):
    return False, "", _extract_text_from_mcp_response(result)[:500]

  return True, memory_id, None


def _search_archival_memory(
  agent_id: str,
  query_text: str,
  limit: int = 10,
) -> Tuple[bool, List[Dict[str, Any]], str]:
  """Semantic search over an agent's archival memories.

  Generates an embedding for query_text via _embedding_text binding, then
  ranks all memories for this agent by cosine distance (lower = more similar).
  Updates access_count and last_accessed_at for returned memories.

  Args:
    agent_id: Which agent's memories to search.
    query_text: Natural language query to match against memory embeddings.
    limit: Maximum number of results to return.

  Returns:
    (True, results_list, "") on success.
    (False, [], error_message) on failure.
    Each result: {memory_id, content, memory_type, importance_score,
                  confidence_score, distance, access_count, created_at}.
  """
  result = _call_sqlite(
    """SELECT memory_id, content, memory_type, importance_score,
            confidence_score, access_count, created_at,
            vec_distance_cosine(embedding, vec_f32(:query_vec)) as distance
    FROM memory_entries
    WHERE agent_id = :agent_id
    ORDER BY distance ASC
    LIMIT :limit""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "query_vec": {"_embedding_text": query_text},
      "limit": limit,
    },
  )

  if result.get("isError"):
    return False, [], _extract_text_from_mcp_response(result)[:500]

  response_text = _extract_text_from_mcp_response(result)
  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    return False, [], f"Failed to parse search results: {response_text[:200]}"

  if rows:
    now = _iso_now()
    memory_ids_to_update = [row["memory_id"] for row in rows if "memory_id" in row]
    for mid in memory_ids_to_update:
      _call_sqlite(
        """UPDATE memory_entries
        SET access_count = access_count + 1, last_accessed_at = :now
        WHERE memory_id = :memory_id""",
        database=AGENT_KERNEL_DATABASE_NAME,
        bindings={"memory_id": mid, "now": now},
      )

  return True, rows, ""


def _update_archival_memory(
  memory_id: str,
  content: Optional[str] = None,
  importance_score: Optional[float] = None,
  confidence_score: Optional[float] = None,
) -> Tuple[bool, str]:
  """Update an existing archival memory, re-generating embedding if content changes.

  Args:
    memory_id: The memory to update.
    content: New text content (triggers re-embedding if provided).
    importance_score: New importance (0.0–1.0).
    confidence_score: New confidence (0.0–1.0).

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  set_clauses = ["updated_at = :now"]
  bindings: Dict[str, Any] = {"memory_id": memory_id, "now": _iso_now()}

  if content is not None:
    set_clauses.append("content = :content")
    set_clauses.append("embedding = vec_f32(generate_embedding(:content))")
    bindings["content"] = content
  if importance_score is not None:
    set_clauses.append("importance_score = :importance_score")
    bindings["importance_score"] = importance_score
  if confidence_score is not None:
    set_clauses.append("confidence_score = :confidence_score")
    bindings["confidence_score"] = confidence_score

  sql = f"UPDATE memory_entries SET {', '.join(set_clauses)} WHERE memory_id = :memory_id"
  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)

  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:500]

  return True, ""


def _delete_archival_memory(memory_id: str) -> Tuple[bool, str]:
  """Delete an archival memory by its ID.

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  result = _call_sqlite(
    "DELETE FROM memory_entries WHERE memory_id = :memory_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"memory_id": memory_id},
  )

  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:500]

  return True, ""


def _get_archival_memory(memory_id: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
  """Retrieve a single archival memory by its ID.

  Returns:
    (True, memory_dict, "") on success.
    (True, None, "") if not found.
    (False, None, error_message) on DB error.
  """
  result = _call_sqlite(
    """SELECT memory_id, agent_id, memory_type, content, importance_score,
            confidence_score, source_run_id, access_count,
            last_accessed_at, created_at, updated_at
    FROM memory_entries WHERE memory_id = :memory_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"memory_id": memory_id},
  )

  if result.get("isError"):
    return False, None, _extract_text_from_mcp_response(result)[:500]

  response_text = _extract_text_from_mcp_response(result)
  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if not rows:
      return True, None, ""
    return True, rows[0], ""
  except (json.JSONDecodeError, KeyError, TypeError):
    return False, None, f"Failed to parse memory: {response_text[:200]}"


# ── 4.4 + 4.2 + 4.3 + 4.5 + 4.9: Pseudo-tool handlers ──
# These are internal operations the LLM can invoke via tool_calls.
# They are intercepted in the ReAct loop BEFORE reaching _call_tool.

PSEUDO_TOOL_NAMES = {
  "core_memory_update",
  "archival_memory_insert",
  "archival_memory_search",
  "recall_memory_search",
  "schedule_reminder",
  "send_to_agent",
  "ask_user",
  "discover_available_mcp_tools",
}


def _handle_pseudo_tool_core_memory_update(
  agent_id: str,
  params: Dict[str, Any],
) -> Dict[str, Any]:
  """Update the agent's working_context (Tier 1 core memory).

  The LLM calls this to persist information across sessions without
  waiting for compaction. This is a direct UPDATE on the agents table.

  Args:
    agent_id: The agent updating its own memory.
    params: {"section": "working_context", "content": "new text"}.

  Returns:
    MCP-format response confirming the update.
  """
  section = params.get("section", "working_context")
  new_content = params.get("content", "")

  if section != "working_context":
    return {"text": f"Only 'working_context' section is editable. Got: '{section}'", "isError": True}

  result = _call_sqlite(
    "UPDATE agents SET working_context = :content, updated_at = :now WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "content": new_content, "now": _iso_now()},
  )

  if result.get("isError"):
    return {"text": f"Failed to update working_context: {_extract_text_from_mcp_response(result)[:200]}", "isError": True}

  _append_session_log_entry(agent_id, "", "core_memory_updated", {
    "section": section,
    "content_preview": new_content[:200],
  })

  return {"text": f"Updated working_context ({len(new_content)} chars). This will be included in all future context assemblies.", "isError": False}


def _handle_pseudo_tool_archival_memory_insert(
  agent_id: str,
  params: Dict[str, Any],
  source_run_id: Optional[str] = None,
) -> Dict[str, Any]:
  """Insert a new archival memory via the LLM's pseudo-tool call.

  Args:
    agent_id: The agent storing the memory.
    params: {"content": "...", "memory_type": "fact", "importance": 0.5}.
    source_run_id: The run that triggered this insertion.

  Returns:
    MCP-format response with the new memory_id.
  """
  content = params.get("content", "")
  if not content:
    return {"text": "Cannot insert empty memory. Provide 'content' parameter.", "isError": True}

  memory_type = params.get("memory_type", "fact")
  importance = float(params.get("importance", params.get("importance_score", 0.5)))
  confidence = float(params.get("confidence", params.get("confidence_score", 0.8)))

  ok, memory_id, err = _insert_archival_memory(
    agent_id, content, memory_type, importance, confidence, source_run_id,
  )

  if not ok:
    return {"text": f"Failed to insert memory: {err}", "isError": True}

  _append_session_log_entry(agent_id, source_run_id or "", "memory_inserted", {
    "memory_id": memory_id,
    "memory_type": memory_type,
    "content_preview": content[:200],
  })

  return {"text": f"Memory stored (id={memory_id}, type={memory_type}). It will be available via semantic search in future runs.", "isError": False}


def _handle_pseudo_tool_archival_memory_search(
  agent_id: str,
  params: Dict[str, Any],
) -> Dict[str, Any]:
  """Search archival memories by semantic similarity.

  Args:
    agent_id: Whose memories to search.
    params: {"query": "...", "count": 10}.

  Returns:
    MCP-format response with search results.
  """
  query = params.get("query", "")
  if not query:
    return {"text": "Provide a 'query' parameter to search memories.", "isError": True}

  count = int(params.get("count", params.get("limit", 10)))
  ok, results, err = _search_archival_memory(agent_id, query, limit=count)

  if not ok:
    return {"text": f"Memory search failed: {err}", "isError": True}

  _append_session_log_entry(agent_id, "", "memory_searched", {
    "query": query[:200],
    "result_count": len(results),
  })

  if not results:
    return {"text": "No archival memories found matching that query.", "isError": False}

  formatted_results = []
  for i, row in enumerate(results):
    distance = row.get("distance", "?")
    formatted_results.append(
      f"{i+1}. [{row.get('memory_type', 'unknown')}] (relevance: {1.0 - float(distance) if distance != '?' else '?':.2f}) "
      f"{row.get('content', '')}"
    )

  return {"text": f"Found {len(results)} archival memories:\n" + "\n".join(formatted_results), "isError": False}


def _handle_pseudo_tool_recall_memory_search(
  agent_id: str,
  params: Dict[str, Any],
) -> Dict[str, Any]:
  """Full-text search over past conversation transcripts and spilled tool results.

  Uses SQL LIKE for substring matching (FTS5 not guaranteed on all sqlite builds).
  Also searches tool_spillover entries from Phase 3.

  Args:
    agent_id: Whose transcripts to search.
    params: {"query": "...", "count": 10}.

  Returns:
    MCP-format response with matching transcript excerpts.
  """
  query = params.get("query", "")
  if not query:
    return {"text": "Provide a 'query' parameter to search recall memory.", "isError": True}

  count = int(params.get("count", params.get("limit", 10)))

  result = _call_sqlite(
    """SELECT entry_id, role, content, session_id, created_at
    FROM transcript_entries
    WHERE agent_id = :agent_id AND content LIKE :pattern
    ORDER BY entry_id DESC
    LIMIT :limit""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "pattern": f"%{query}%",
      "limit": count,
    },
  )

  if result.get("isError"):
    return {"text": f"Recall search failed: {_extract_text_from_mcp_response(result)[:200]}", "isError": True}

  response_text = _extract_text_from_mcp_response(result)
  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    rows = []

  if not rows:
    return {"text": f"No transcript entries found matching '{query}'.", "isError": False}

  formatted_entries = []
  for row in rows:
    role = row.get("role", "?")
    content = row.get("content", "")
    preview = content[:300] + ("..." if len(content) > 300 else "")
    session = row.get("session_id", "?")
    created = row.get("created_at", "?")
    formatted_entries.append(f"[{role}] (session={session}, {created}): {preview}")

  return {"text": f"Found {len(rows)} transcript entries matching '{query}':\n" + "\n".join(formatted_entries), "isError": False}


def _handle_pseudo_tool_schedule_reminder(
  agent_id: str,
  params: Dict[str, Any],
) -> Dict[str, Any]:
  """Schedule a future event for this agent via one-shot or recurring cron.

  Supports:
  - Absolute ISO datetime: "2026-04-14T09:00:00+12:00"
  - Relative time: "in 3 hours", "in 2 days", "in 30 minutes", "tomorrow at 09:00"
  - Recurring cron: if "recurring" param present, creates persistent cron source

  Args:
    agent_id: The agent scheduling the reminder.
    params: {"when": "...", "message": "...", "priority": "normal", "recurring": "cron_expr"}.

  Returns:
    MCP-format response confirming the scheduled event source.
  """
  from datetime import datetime, timezone, timedelta
  import re

  when_str = params.get("when", "")
  reminder_message = params.get("message", "Scheduled reminder")
  priority = params.get("priority", "normal")
  recurring_cron_expression = params.get("recurring")

  if not when_str and not recurring_cron_expression:
    return {"text": "Provide 'when' (absolute or relative time) or 'recurring' (cron expression).", "isError": True}

  if recurring_cron_expression:
    source_config = {
      "schedule": recurring_cron_expression,
      "message": reminder_message,
    }
    source_type = "cron"
  else:
    target_datetime = _parse_reminder_time_specification(when_str)
    if target_datetime is None:
      return {
        "text": f"Could not parse time '{when_str}'. Use ISO format (2026-04-15T09:00:00+12:00) or relative (in 3 hours, in 30 minutes, tomorrow at 09:00).",
        "isError": True,
      }

    cron_expression = f"{target_datetime.minute} {target_datetime.hour} {target_datetime.day} {target_datetime.month} *"
    source_config = {
      "schedule": cron_expression,
      "message": reminder_message,
      "target_datetime_iso": target_datetime.isoformat(),
      "auto_disable_after_fire": True,
    }
    source_type = "cron_oneshot"

  add_result = handle_add_event_source({
    "agent_id": agent_id,
    "source_type": source_type,
    "config": source_config,
    "priority": priority,
    "queue_mode": "queue",
    "tool_unlock_token": TOOL_UNLOCK_TOKEN,
  })

  if add_result.get("isError"):
    return {"text": f"Failed to schedule reminder: {_extract_text_from_mcp_response(add_result)[:200]}", "isError": True}

  response_text = _extract_text_from_mcp_response(add_result)
  try:
    source_info = json.loads(response_text)
    source_id = source_info.get("source_id", "?")
  except (json.JSONDecodeError, TypeError):
    source_id = "?"

  if recurring_cron_expression:
    return {"text": f"Recurring reminder scheduled (source_id={source_id}, cron={recurring_cron_expression}): {reminder_message}", "isError": False}
  else:
    return {"text": f"Reminder scheduled for {target_datetime.isoformat()} (source_id={source_id}): {reminder_message}", "isError": False}


def _parse_reminder_time_specification(when_str: str) -> Optional['datetime']:
  """Parse absolute or relative time strings into a datetime object.

  Supports:
  - ISO 8601: "2026-04-15T09:00:00+12:00"
  - Relative: "in 3 hours", "in 30 minutes", "in 2 days"
  - Natural: "tomorrow at 09:00", "tomorrow at 9am"

  Returns:
    A timezone-aware datetime, or None if parsing fails.
  """
  from datetime import datetime, timezone, timedelta
  import re

  when_str = when_str.strip()

  try:
    if "T" in when_str or when_str.count("-") >= 2:
      parsed = datetime.fromisoformat(when_str)
      if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
      return parsed
  except (ValueError, TypeError):
    pass

  now = datetime.now(timezone.utc)
  when_lower = when_str.lower().strip()

  relative_pattern = re.compile(r"in\s+(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)")
  match = relative_pattern.match(when_lower)
  if match:
    amount = int(match.group(1))
    unit = match.group(2).rstrip("s")
    if unit == "minute":
      return now + timedelta(minutes=amount)
    elif unit == "hour":
      return now + timedelta(hours=amount)
    elif unit == "day":
      return now + timedelta(days=amount)
    elif unit == "week":
      return now + timedelta(weeks=amount)

  tomorrow_pattern = re.compile(r"tomorrow\s+at\s+(\d{1,2}):?(\d{2})?\s*(am|pm)?", re.IGNORECASE)
  match = tomorrow_pattern.match(when_lower)
  if match:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = match.group(3)
    if ampm:
      if ampm.lower() == "pm" and hour < 12:
        hour += 12
      elif ampm.lower() == "am" and hour == 12:
        hour = 0
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

  return None


def _parse_send_to_agent_allowlist_from_agent_config(agent_config: Dict[str, Any]) -> List[str]:
  """Parse an agent's send_to_agent_allowlist JSON column into a list of agent ids.

  Returns the allowed target agent ids; a list containing "*" allows all
  agents. The default is [] (deny-all): inter-agent messaging is opt-in per
  agent, because the send_to_agent pseudo-tool bypasses the policy guard.
  """
  raw_allowlist_value = agent_config.get("send_to_agent_allowlist", "[]")
  try:
    parsed_allowlist = json.loads(raw_allowlist_value) if isinstance(raw_allowlist_value, str) else raw_allowlist_value
  except (json.JSONDecodeError, TypeError):
    parsed_allowlist = []
  return parsed_allowlist if isinstance(parsed_allowlist, list) else []


def _handle_pseudo_tool_send_to_agent(
  sender_agent_id: str,
  params: Dict[str, Any],
  sender_run_id: str,
) -> Dict[str, Any]:
  """Route a message from one agent to another via the target's mailbox.

  The sending agent provides target_agent_id and message. The kernel:
  1. Validates the target agent exists and is not the sender.
  2. Checks the sender's send_to_agent_allowlist (deny-all by default) and
     its hourly tool-call rate limit - pseudo-tools bypass the policy guard,
     so this handler must enforce its own authorization.
  3. Enqueues the message as an 'agent_message' event.
  4. Signals the target's mailbox worker.
  5. Returns confirmation (fire-and-forget; response is asynchronous).
  """
  target_agent_id = params.get("agent_id") or params.get("target_agent_id")
  message_text = params.get("message", "")

  if not target_agent_id:
    return {"text": "Missing required parameter: agent_id (the target agent to send to).", "isError": True}
  if not message_text:
    return {"text": "Missing required parameter: message.", "isError": True}
  if target_agent_id == sender_agent_id:
    return {"text": "Cannot send a message to yourself.", "isError": True}

  sender_config = _extract_agent_config_as_dict(sender_agent_id) or {}
  sender_allowlist = _parse_send_to_agent_allowlist_from_agent_config(sender_config)
  if "*" not in sender_allowlist and target_agent_id not in sender_allowlist:
    return {
      "text": (
        f"Not authorized: agent '{target_agent_id}' is not in this agent's send_to_agent_allowlist. "
        f"An operator can grant access via update_agent with send_to_agent_allowlist."
      ),
      "isError": True,
    }

  within_rate_limit, rate_limit_reason = _check_rate_limit(sender_agent_id, "send_to_agent", sender_config)
  if not within_rate_limit:
    _append_session_log_entry(sender_agent_id, sender_run_id, "rate_limit_hit", {
      "tool_name": "send_to_agent",
      "reason": rate_limit_reason,
    })
    return {"text": f"send_to_agent blocked: {rate_limit_reason}", "isError": True}

  target_config = _extract_agent_config_as_dict(target_agent_id)
  if target_config is None:
    return {"text": f"Target agent not found: '{target_agent_id}'.", "isError": True}

  enqueue_ok, enqueue_status, queue_id = _enqueue_event(
    agent_id=target_agent_id,
    event_type="agent_message",
    payload={
      "message": message_text,
      "sender_agent_id": sender_agent_id,
      "sender_run_id": sender_run_id,
    },
    priority="normal",
    queue_mode="queue",
  )

  if not enqueue_ok:
    return {"text": f"Failed to deliver message to '{target_agent_id}': {enqueue_status}", "isError": True}

  mailbox = _get_or_create_mailbox_for_agent(target_agent_id)
  mailbox.signal_new_event_available()

  target_display_name = target_config.get("display_name", target_agent_id)
  return {
    "text": f"Message delivered to agent '{target_display_name}' (id={target_agent_id}). The agent will process it asynchronously.",
    "isError": False,
  }


def _build_agent_directory_for_system_prompt(
  excluding_agent_id: str,
  requesting_agent_config: Optional[Dict[str, Any]] = None,
) -> str:
  """Build a brief directory of agents this agent MAY message, for the system prompt.

  Only lists agents present in the requesting agent's send_to_agent_allowlist
  ("*" = all agents), and shows display_name + id only - other agents' system
  prompts are their own configuration and must not leak into this agent's
  context. Returns empty string when the allowlist is empty or no eligible
  agents exist.
  """
  if requesting_agent_config is None:
    requesting_agent_config = _extract_agent_config_as_dict(excluding_agent_id) or {}
  allowed_target_agent_ids = _parse_send_to_agent_allowlist_from_agent_config(requesting_agent_config)
  if not allowed_target_agent_ids:
    return ""

  result = _call_sqlite(
    "SELECT agent_id, display_name FROM agents WHERE agent_id != :exclude_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"exclude_id": excluding_agent_id},
  )
  rows = _parse_rows_from_mcp_query_response(result)
  allowlist_permits_all_agents = "*" in allowed_target_agent_ids

  directory_lines = ["\n\n## Available Agents (for send_to_agent)"]
  eligible_agent_count = 0
  for row in rows:
    agent_id = row.get("agent_id", "?")
    if not allowlist_permits_all_agents and agent_id not in allowed_target_agent_ids:
      continue
    display_name = row.get("display_name", agent_id)
    directory_lines.append(f"- **{display_name}** (id=`{agent_id}`)")
    eligible_agent_count += 1

  if eligible_agent_count == 0:
    return ""
  return "\n".join(directory_lines)


ASK_USER_REQUEST_DEFAULT_TIMEOUT_SECONDS = 600

# Lives in the cross-reload shared state: a hot reload must not orphan threads
# blocked in ask_user by discarding their pending-request entries.
_pending_user_requests: Dict[str, Dict[str, Any]] = _get_phase2_shared_state()['pending_user_requests']


def _handle_pseudo_tool_ask_user(
  agent_id: str,
  params: Dict[str, Any],
  run_id: str,
  session_id: str,
  step_number: int,
  agent_config: Dict[str, Any],
) -> Dict[str, Any]:
  """Pause the agent's execution and wait for a human reply (spec §3.5).

  Transitions EXECUTING_TOOL → WAITING_FOR_USER, notifies the user, and blocks
  until respond_to_user_request is called or timeout expires. The user's reply
  text is returned as the pseudo-tool result so the LLM sees it in context.
  """
  question_text = params.get("question") or params.get("message") or params.get("prompt") or ""
  if not question_text:
    return {"text": "Missing required parameter: question (what to ask the user).", "isError": True}

  timeout_seconds = params.get("timeout_seconds", ASK_USER_REQUEST_DEFAULT_TIMEOUT_SECONDS) or ASK_USER_REQUEST_DEFAULT_TIMEOUT_SECONDS

  request_id = f"ureq-{hashlib.md5(f'{agent_id}{run_id}{time.time()}'.encode()).hexdigest()[:12]}"

  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="EXECUTING_TOOL", to_state="WAITING_FOR_USER",
    checkpoint_snapshot={
      "run_id": run_id, "session_id": session_id, "step_number": step_number,
      "ask_user_request_id": request_id,
      "question": question_text[:500],
    },
  )
  if not transition_ok:
    return {"text": f"Failed to transition to WAITING_FOR_USER: {transition_err}", "isError": True}

  _append_session_log_entry(agent_id, run_id, "ask_user_requested", {
    "request_id": request_id,
    "question": question_text[:500],
    "timeout_seconds": timeout_seconds,
  })

  response_event = threading.Event()

  _pending_user_requests[request_id] = {
    "agent_id": agent_id,
    "run_id": run_id,
    "session_id": session_id,
    "step_number": step_number,
    "question": question_text,
    "requested_at": _iso_now(),
    "timeout_seconds": timeout_seconds,
    "response_event": response_event,
    "user_response_text": None,
  }

  _send_ask_user_notification_to_operator(agent_id, request_id, question_text, agent_config)

  user_responded = response_event.wait(timeout=timeout_seconds)

  pending_entry = _pending_user_requests.pop(request_id, None)

  if not user_responded or pending_entry is None or pending_entry.get("user_response_text") is None:
    _append_session_log_entry(agent_id, run_id, "ask_user_timeout", {
      "request_id": request_id,
      "timeout_seconds": timeout_seconds,
    })
    _transition_agent_state(
      agent_id, run_id, session_id, step_number + 1,
      from_state="WAITING_FOR_USER", to_state="EXECUTING_TOOL",
      checkpoint_snapshot={"run_id": run_id, "ask_user_result": "timeout"},
    )
    return {"text": f"(No response from user within {timeout_seconds}s. The user did not answer your question.)", "isError": False}

  user_reply = pending_entry["user_response_text"]

  _append_session_log_entry(agent_id, run_id, "ask_user_responded", {
    "request_id": request_id,
    "response_preview": user_reply[:200],
  })

  _transition_agent_state(
    agent_id, run_id, session_id, step_number + 1,
    from_state="WAITING_FOR_USER", to_state="EXECUTING_TOOL",
    checkpoint_snapshot={"run_id": run_id, "ask_user_result": "responded"},
  )

  return {"text": f"User replied: {user_reply}", "isError": False}


def _send_ask_user_notification_to_operator(
  agent_id: str,
  request_id: str,
  question_text: str,
  agent_config: Dict[str, Any],
) -> None:
  """Notify the user/operator that an agent is waiting for their input.

  Reuses the same channel routing cascade as approval notifications
  (last active channel, then default_response_channel, then skip).
  """
  display_name = agent_config.get("display_name", agent_id)

  notification_message = (
    f"Agent '{display_name}' is asking you a question:\n\n"
    f"{question_text}\n\n"
    f"Reply using: respond_to_user_request with agent_id='{agent_id}' and your response_text."
  )

  _dispatch_message_to_operator_via_last_active_or_default_channel(
    agent_id, notification_message, agent_config
  )

  MCPLogger.log(TOOL_LOG_NAME, f"Ask-user notification sent for agent {agent_id} (request_id={request_id})")


def handle_respond_to_user_request(params: Dict) -> Dict:
  """MCP operation: deliver a user's reply to an agent waiting via ask_user.

  Finds the pending request for the given agent_id and delivers response_text,
  waking the blocked ask_user handler thread.
  """
  agent_id = params.get("agent_id")
  response_text = params.get("response_text", "")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  matching_request_id = None
  for req_id, req_data in list(_pending_user_requests.items()):
    if req_data.get("agent_id") == agent_id:
      matching_request_id = req_id
      break

  if matching_request_id is None:
    return create_error_response(f"No pending ask_user request found for agent '{agent_id}'.")

  pending_entry = _pending_user_requests.get(matching_request_id)
  if pending_entry is None:
    return create_error_response(f"Pending request '{matching_request_id}' vanished (race condition).")

  pending_entry["user_response_text"] = response_text

  response_event = pending_entry.get("response_event")
  if response_event:
    response_event.set()

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "delivered",
      "request_id": matching_request_id,
      "agent_id": agent_id,
      "question": pending_entry.get("question", "")[:200],
    }, indent=2)}],
    "isError": False,
  }


def _handle_pseudo_tool_discover_available_mcp_tools() -> Dict[str, Any]:
  """Return a concise summary of all MCP tools registered on this server.

  Queries the server's tool registry to enumerate every registered handler,
  then fetches the first sentence of each tool's description to produce a
  compact directory that fits inside a small LLM context window.
  """
  server = _get_mcp_server_instance()
  if server is None:
    return {"text": "Error: cannot list MCP tools — server instance not available.", "isError": True}
  try:
    tool_handlers = getattr(server, 'tool_handlers', None) or {}
    lines = []
    for tool_name in sorted(tool_handlers.keys()):
      if tool_name == f"agent{TOOL_NAME_SUFFIX}":
        continue
      detail = tool_handlers[tool_name]
      raw_description = detail.get("description", "(no description)")
      first_line = raw_description.strip().split("\n")[0].strip("- ").strip()
      if len(first_line) > 120:
        first_line = first_line[:117] + "..."
      lines.append(f"- **{tool_name}**: {first_line}")
    if not lines:
      return {"text": "No MCP tools found on this server.", "isError": False}
    header = f"## {len(lines)} MCP tools available on this server\n\nCall any tool by name. Use its 'readme' operation for full documentation and unlock token.\n\n"
    return {"text": header + "\n".join(lines), "isError": False}
  except Exception as discovery_error:
    return {"text": f"Error discovering MCP tools: {discovery_error}", "isError": True}


def _dispatch_pseudo_tool_call(
  agent_id: str,
  tool_name: str,
  arguments: Dict[str, Any],
  run_id: str,
  session_id: str = "",
  step_number: int = 0,
  agent_config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
  """Dispatch a pseudo-tool call to the appropriate internal handler.

  Called from the ReAct loop when a tool_call matches a pseudo-tool name.

  Args:
    agent_id: The agent making the call.
    tool_name: One of PSEUDO_TOOL_NAMES.
    arguments: The arguments the LLM provided.
    run_id: Current run for provenance tracking.
    session_id: Current session (needed by ask_user).
    step_number: Current step (needed by ask_user state transitions).
    agent_config: Full agent config (needed by ask_user for notifications).

  Returns:
    (True, result_text) on success.
    (False, error_text) on failure.
  """
  if tool_name == "core_memory_update":
    result = _handle_pseudo_tool_core_memory_update(agent_id, arguments)
  elif tool_name == "archival_memory_insert":
    result = _handle_pseudo_tool_archival_memory_insert(agent_id, arguments, source_run_id=run_id)
  elif tool_name == "archival_memory_search":
    result = _handle_pseudo_tool_archival_memory_search(agent_id, arguments)
  elif tool_name == "recall_memory_search":
    result = _handle_pseudo_tool_recall_memory_search(agent_id, arguments)
  elif tool_name == "schedule_reminder":
    result = _handle_pseudo_tool_schedule_reminder(agent_id, arguments)
  elif tool_name == "send_to_agent":
    result = _handle_pseudo_tool_send_to_agent(agent_id, arguments, sender_run_id=run_id)
  elif tool_name == "ask_user":
    result = _handle_pseudo_tool_ask_user(agent_id, arguments, run_id, session_id, step_number, agent_config or {})
  elif tool_name == "discover_available_mcp_tools":
    result = _handle_pseudo_tool_discover_available_mcp_tools()
  else:
    return False, f"Unknown pseudo-tool: {tool_name}"

  return not result.get("isError", False), result.get("text", "")


# ===============================================================================
# Phase 5: Policy Guard + Circuit Breakers + LLM Fallback (spec §5, §3.7)
#
# Safety layer that runs before every real MCP tool call in the ReAct loop.
# Pseudo-tools bypass the guard entirely (they are internal operations).
# The guard checks authorization → rate limit → approval → circuit breaker.
# ===============================================================================

POLICY_GUARD_TOOL_CALL_CIRCUIT_BREAKER_THRESHOLD = 3

# These three live in the cross-reload shared state so a hot reload does not
# drop pending approvals (blocking waiter threads belong to the old module
# generation), circuit-breaker counts, or operator channel routing.
_per_run_tool_failure_tracker: Dict[Tuple[str, str, str], int] = _get_phase2_shared_state()['per_run_tool_failure_tracker']

_pending_approval_requests: Dict[str, Dict[str, Any]] = _get_phase2_shared_state()['pending_approval_requests']

_last_active_channel_per_agent: Dict[str, Dict[str, Any]] = _get_phase2_shared_state()['last_active_channel_per_agent']


def _classify_tool_safety_category(
  tool_name: str,
  agent_config: Dict[str, Any],
) -> str:
  """Classify a tool into a safety category based on the agent's permission config.

  Checks the agent's read_tools_allowed and write_tools_allowed JSON arrays
  to determine which safety category applies (spec §5.1).

  Returns one of: 'read_allowed', 'write_allowed', 'requires_approval', 'denied', 'unknown'.
  """
  read_tools_allowed_raw = agent_config.get("read_tools_allowed", '["*"]')
  write_tools_allowed_raw = agent_config.get("write_tools_allowed", '[]')
  tools_requiring_approval_raw = agent_config.get("tools_requiring_approval", '[]')

  try:
    read_tools_allowed_list = json.loads(read_tools_allowed_raw) if isinstance(read_tools_allowed_raw, str) else read_tools_allowed_raw
  except (json.JSONDecodeError, TypeError):
    read_tools_allowed_list = ["*"]

  try:
    write_tools_allowed_list = json.loads(write_tools_allowed_raw) if isinstance(write_tools_allowed_raw, str) else write_tools_allowed_raw
  except (json.JSONDecodeError, TypeError):
    write_tools_allowed_list = []

  try:
    tools_requiring_approval_list = json.loads(tools_requiring_approval_raw) if isinstance(tools_requiring_approval_raw, str) else tools_requiring_approval_raw
  except (json.JSONDecodeError, TypeError):
    tools_requiring_approval_list = []

  if not isinstance(read_tools_allowed_list, list):
    read_tools_allowed_list = ["*"]
  if not isinstance(write_tools_allowed_list, list):
    write_tools_allowed_list = []
  if not isinstance(tools_requiring_approval_list, list):
    tools_requiring_approval_list = []

  if tool_name in tools_requiring_approval_list or "*" in tools_requiring_approval_list:
    return "requires_approval"

  if "*" in write_tools_allowed_list or tool_name in write_tools_allowed_list:
    return "write_allowed"

  if "*" in read_tools_allowed_list or tool_name in read_tools_allowed_list:
    return "read_allowed"

  return "denied"


def _check_tool_authorization(
  tool_name: str,
  agent_config: Dict[str, Any],
) -> Tuple[bool, str]:
  """Check if a tool is authorized for this agent (spec §5.2 step 1).

  Returns (True, "") if authorized, or (False, reason) if denied.
  Tools in requires_approval are considered authorized (approval check is separate).
  """
  category = _classify_tool_safety_category(tool_name, agent_config)

  if category == "denied":
    return False, f"Tool '{tool_name}' is not in this agent's allowed tool lists (read_tools_allowed or write_tools_allowed)"

  return True, ""


def _check_rate_limit(
  agent_id: str,
  tool_name: str,
  agent_config: Dict[str, Any],
) -> Tuple[bool, str]:
  """Check if the agent has exceeded its per-hour tool call rate limit (spec §5.2 step 3).

  Counts recent tool_executed session log entries in the last hour.
  Returns (True, "") if within limit, or (False, reason) if exceeded.
  """
  max_tool_calls_per_hour = agent_config.get("max_tool_calls_per_hour")
  if max_tool_calls_per_hour is None or max_tool_calls_per_hour <= 0:
    return True, ""

  one_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
  # Same millisecond+Z format _iso_now() writes, so the string comparison
  # against created_at is exact at the hour boundary.
  one_hour_ago_iso = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

  count_result = _call_sqlite(
    """SELECT COUNT(*) as tool_call_count_in_last_hour FROM session_log
    WHERE agent_id = :agent_id AND entry_type = 'tool_executed'
    AND created_at >= :since""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "since": one_hour_ago_iso}
  )

  tool_call_count_in_last_hour = 0
  try:
    data = json.loads(_extract_text_from_mcp_response(count_result))
    rows = data.get("data_rows_from_result_set", [])
    if rows:
      tool_call_count_in_last_hour = rows[0].get("tool_call_count_in_last_hour", 0)
  except (json.JSONDecodeError, KeyError, TypeError):
    pass

  if tool_call_count_in_last_hour >= max_tool_calls_per_hour:
    return False, f"Rate limit exceeded: {tool_call_count_in_last_hour}/{max_tool_calls_per_hour} tool calls in the last hour"

  return True, ""


def _check_llm_call_rate_limit_for_agent(
  agent_id: str,
  agent_config: Dict[str, Any],
) -> Tuple[bool, str]:
  """Check if the agent has exceeded its per-hour LLM call rate limit.

  Mirrors _check_rate_limit but counts llm_called session log entries.
  A limit of None or <= 0 means unlimited.
  Returns (True, "") if within limit, or (False, reason) if exceeded.
  """
  max_llm_calls_per_hour = agent_config.get("max_llm_calls_per_hour")
  if max_llm_calls_per_hour is None or max_llm_calls_per_hour <= 0:
    return True, ""

  one_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
  # Same millisecond+Z format _iso_now() writes (see _check_rate_limit).
  one_hour_ago_iso = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

  count_result = _call_sqlite(
    """SELECT COUNT(*) as llm_call_count_in_last_hour FROM session_log
    WHERE agent_id = :agent_id AND entry_type = 'llm_called'
    AND created_at >= :since""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "since": one_hour_ago_iso}
  )

  llm_call_count_in_last_hour = 0
  rows = _parse_rows_from_mcp_query_response(count_result)
  if rows:
    llm_call_count_in_last_hour = rows[0].get("llm_call_count_in_last_hour", 0)

  if llm_call_count_in_last_hour >= max_llm_calls_per_hour:
    return False, f"LLM call rate limit exceeded: {llm_call_count_in_last_hour}/{max_llm_calls_per_hour} LLM calls in the last hour"

  return True, ""


def _check_tool_circuit_breaker(
  agent_id: str,
  tool_name: str,
  run_id: str,
) -> Tuple[bool, str]:
  """Check if the per-run circuit breaker has tripped for this tool (spec §5.5).

  Returns (True, "") if the tool is available, or (False, reason) if the breaker has tripped.
  """
  tracker_key = (agent_id, run_id, tool_name)
  consecutive_failure_count = _per_run_tool_failure_tracker.get(tracker_key, 0)
  if consecutive_failure_count >= POLICY_GUARD_TOOL_CALL_CIRCUIT_BREAKER_THRESHOLD:
    return False, (
      f"Tool '{tool_name}' is currently unavailable (circuit breaker tripped after "
      f"{POLICY_GUARD_TOOL_CALL_CIRCUIT_BREAKER_THRESHOLD} consecutive failures in this run). "
      f"Please use an alternative approach or ask the user for help."
    )
  return True, ""


def _record_tool_failure_for_circuit_breaker(
  agent_id: str,
  run_id: str,
  tool_name: str,
) -> None:
  """Increment the consecutive failure counter for a tool within a run.

  If the counter reaches the threshold, logs a circuit_breaker_tripped entry.
  """
  tracker_key = (agent_id, run_id, tool_name)
  current_count = _per_run_tool_failure_tracker.get(tracker_key, 0) + 1
  _per_run_tool_failure_tracker[tracker_key] = current_count

  if current_count >= POLICY_GUARD_TOOL_CALL_CIRCUIT_BREAKER_THRESHOLD:
    _append_session_log_entry(agent_id, run_id, "circuit_breaker_tripped", {
      "tool_name": tool_name,
      "consecutive_failure_count": current_count,
      "threshold": POLICY_GUARD_TOOL_CALL_CIRCUIT_BREAKER_THRESHOLD,
    })


def _record_tool_success_for_circuit_breaker(
  agent_id: str,
  run_id: str,
  tool_name: str,
) -> None:
  """Reset the consecutive failure counter for a tool (success breaks the streak)."""
  tracker_key = (agent_id, run_id, tool_name)
  if tracker_key in _per_run_tool_failure_tracker:
    del _per_run_tool_failure_tracker[tracker_key]


def _clear_circuit_breaker_tracker_for_run(agent_id: str, run_id: str) -> None:
  """Remove all circuit breaker entries for a given agent+run (called at run start)."""
  keys_to_remove = [k for k in _per_run_tool_failure_tracker if k[0] == agent_id and k[1] == run_id]
  for k in keys_to_remove:
    del _per_run_tool_failure_tracker[k]


def _execute_policy_guard_check(
  agent_id: str,
  tool_name: str,
  tool_arguments: Dict[str, Any],
  agent_config: Dict[str, Any],
  run_id: str,
) -> Tuple[bool, str, bool]:
  """Run the full pre-tool-call safety check pipeline (spec §5.2).

  Checks in order: authorization → rate limit → approval requirement → circuit breaker.

  Returns:
    (allowed, reason, needs_approval)
    - allowed=True, needs_approval=False: proceed with tool call
    - allowed=False, needs_approval=False: reject (reason explains why)
    - allowed=False, needs_approval=True: tool requires human approval before execution
  """
  authorized, auth_reason = _check_tool_authorization(tool_name, agent_config)
  if not authorized:
    _append_session_log_entry(agent_id, run_id, "policy_checked", {
      "tool_name": tool_name,
      "result": "denied",
      "reason": auth_reason,
      "check": "authorization",
    })
    return False, auth_reason, False

  within_rate_limit, rate_reason = _check_rate_limit(agent_id, tool_name, agent_config)
  if not within_rate_limit:
    _append_session_log_entry(agent_id, run_id, "policy_checked", {
      "tool_name": tool_name,
      "result": "denied",
      "reason": rate_reason,
      "check": "rate_limit",
    })
    _append_session_log_entry(agent_id, run_id, "rate_limit_hit", {
      "tool_name": tool_name,
      "detail": rate_reason,
    })
    return False, rate_reason, False

  category = _classify_tool_safety_category(tool_name, agent_config)
  if category == "requires_approval":
    _append_session_log_entry(agent_id, run_id, "policy_checked", {
      "tool_name": tool_name,
      "result": "requires_approval",
      "check": "approval_required",
    })
    return False, "Tool requires human approval before execution", True

  circuit_breaker_available, cb_reason = _check_tool_circuit_breaker(agent_id, tool_name, run_id)
  if not circuit_breaker_available:
    _append_session_log_entry(agent_id, run_id, "policy_checked", {
      "tool_name": tool_name,
      "result": "denied",
      "reason": cb_reason,
      "check": "circuit_breaker",
    })
    return False, cb_reason, False

  _append_session_log_entry(agent_id, run_id, "policy_checked", {
    "tool_name": tool_name,
    "result": "allowed",
    "category": category,
  })
  return True, "", False


# ===============================================================================
# Phase 5: LLM Fallback Chain (spec §3.7) + Capability-Aware Routing
# ===============================================================================


def _detect_required_llm_capabilities_from_call_params(llm_params: Dict[str, Any]) -> set:
  """Analyze LLM call params to determine which endpoint capabilities are needed.

  Examines messages for image/audio content, tool definitions, and streaming
  flags to build a set of required capability keys (matching the keys in
  endpoint_config['capabilities']).
  """
  required: set = set()

  messages = llm_params.get("messages", [])
  for msg in messages:
    content = msg.get("content")
    if isinstance(content, list):
      for part in content:
        if isinstance(part, dict):
          part_type = part.get("type", "")
          if part_type in ("image_url", "image"):
            required.add("vision_input")
          elif part_type in ("audio", "input_audio"):
            required.add("audio_input")

  if llm_params.get("tools"):
    required.add("tool_calling")

  if llm_params.get("stream") is True:
    required.add("streaming")

  if llm_params.get("response_format", {}).get("type") == "json_object":
    required.add("json_mode")

  return required


def _check_endpoint_has_required_capabilities(
  endpoint_name: str,
  required_capabilities: set,
) -> Tuple[bool, List[str]]:
  """Check if a named endpoint supports all required capabilities.

  Returns (all_met, list_of_missing_capability_names).
  If the endpoint is not found or has no capabilities defined, returns (True, [])
  to avoid blocking calls when capability metadata is unavailable.
  """
  if not required_capabilities or not endpoint_name:
    return True, []

  from ragtag.shared_config import get_llm_endpoint_config
  endpoint_cfg = get_llm_endpoint_config(endpoint_name)
  if not endpoint_cfg:
    return True, []

  capabilities = endpoint_cfg.get("capabilities", {})
  if not capabilities:
    return True, []

  missing = [cap for cap in required_capabilities if not capabilities.get(cap, False)]
  return (len(missing) == 0), missing


def _call_llm_with_fallback_chain(
  agent_id: str,
  run_id: str,
  agent_config: Dict[str, Any],
  llm_params: Dict[str, Any],
) -> Dict[str, Any]:
  """Call the LLM with automatic fallback to alternative providers on failure.

  Tries the primary provider first, then each entry in model_fallback_chain.
  Context-too-long errors are NOT retried via fallback (handled by emergency compact).
  Only non-context errors trigger the fallback chain.
  Fallback entries whose endpoint lacks required capabilities are skipped.

  Returns the MCP response dict from whichever provider succeeds (or the last error).
  """
  primary_endpoint = llm_params.get("endpoint", "")
  if primary_endpoint:
    required_caps = _detect_required_llm_capabilities_from_call_params(llm_params)
    if required_caps:
      caps_ok, missing = _check_endpoint_has_required_capabilities(primary_endpoint, required_caps)
      if not caps_ok:
        MCPLogger.log(TOOL_LOG_NAME,
          f"Warning: primary endpoint '{primary_endpoint}' for agent {agent_id} "
          f"is missing declared capabilities {missing}. Attempting call anyway.")

  llm_result = _call_tool(_suffixed_tool_name("llm"), {"input": llm_params})

  if not llm_result.get("isError"):
    return llm_result

  primary_error_text = _extract_text_from_mcp_response(llm_result)[:500]

  if _detect_context_too_long_error(primary_error_text):
    return llm_result

  fallback_chain_raw = agent_config.get("model_fallback_chain", "[]")
  try:
    fallback_chain = json.loads(fallback_chain_raw) if isinstance(fallback_chain_raw, str) else fallback_chain_raw
  except (json.JSONDecodeError, TypeError):
    fallback_chain = []

  if not isinstance(fallback_chain, list) or not fallback_chain:
    return llm_result

  required_capabilities = _detect_required_llm_capabilities_from_call_params(llm_params)

  MCPLogger.log(TOOL_LOG_NAME,
    f"LLM primary failed for agent {agent_id}, trying {len(fallback_chain)} fallback(s): {primary_error_text[:100]}"
    + (f" (required capabilities: {required_capabilities})" if required_capabilities else ""))

  last_error_result = llm_result
  skipped_for_capability_count = 0

  for fallback_index, fallback_entry in enumerate(fallback_chain):
    if not isinstance(fallback_entry, list) or len(fallback_entry) < 2:
      continue

    fallback_endpoint_or_provider = fallback_entry[0]
    fallback_model = fallback_entry[1]

    if required_capabilities:
      capabilities_met, missing_capabilities = _check_endpoint_has_required_capabilities(
        fallback_endpoint_or_provider, required_capabilities)
      if not capabilities_met:
        MCPLogger.log(TOOL_LOG_NAME,
          f"LLM fallback #{fallback_index + 1} skipped for agent {agent_id}: "
          f"endpoint '{fallback_endpoint_or_provider}' missing capabilities {missing_capabilities}")
        _append_session_log_entry(agent_id, run_id, "fallback_skipped_missing_capabilities", {
          "endpoint": fallback_endpoint_or_provider,
          "model": fallback_model,
          "fallback_index": fallback_index + 1,
          "missing_capabilities": missing_capabilities,
        })
        skipped_for_capability_count += 1
        continue

    fallback_params = dict(llm_params)
    fallback_params.pop("mlx_host", None)
    fallback_params.pop("ollama_host", None)
    fallback_params.pop("base_url", None)
    fallback_params.pop("api_key", None)
    fallback_params.pop("endpoint", None)
    fallback_params["model"] = fallback_model

    from ragtag.shared_config import get_llm_endpoint_config
    endpoint_cfg = get_llm_endpoint_config(fallback_endpoint_or_provider)
    if endpoint_cfg:
      fallback_params["endpoint"] = fallback_endpoint_or_provider
      fallback_provider = endpoint_cfg.get("provider_type", fallback_endpoint_or_provider)
    else:
      fallback_provider = fallback_endpoint_or_provider
    fallback_params["provider"] = fallback_provider
    _apply_provider_host_params_for_llm_call(fallback_provider, fallback_params, fallback_params.get("endpoint"))

    _append_session_log_entry(agent_id, run_id, "llm_called", {
      "provider": fallback_provider,
      "model": fallback_model,
      "fallback_index": fallback_index + 1,
      "reason": "primary_provider_failed",
      "primary_error": primary_error_text[:100],
    })

    fallback_result = _call_tool(_suffixed_tool_name("llm"), {"input": fallback_params})

    if not fallback_result.get("isError"):
      MCPLogger.log(TOOL_LOG_NAME,
        f"LLM fallback #{fallback_index + 1} succeeded for agent {agent_id}: {fallback_provider}/{fallback_model}")
      return fallback_result

    last_error_result = fallback_result
    fallback_error = _extract_text_from_mcp_response(fallback_result)[:200]
    MCPLogger.log(TOOL_LOG_NAME,
      f"LLM fallback #{fallback_index + 1} also failed for agent {agent_id}: {fallback_error}")

  if skipped_for_capability_count > 0:
    MCPLogger.log(TOOL_LOG_NAME,
      f"LLM fallback chain exhausted for agent {agent_id}: "
      f"{skipped_for_capability_count} endpoint(s) skipped due to missing capabilities {required_capabilities}")

  return last_error_result


# ===============================================================================
# Phase 5: Human Approval Flow (spec §5.3, §5.4)
# ===============================================================================

def _update_last_active_channel_from_event(
  agent_id: str,
  source_metadata: Optional[Dict[str, Any]],
) -> None:
  """Extract transport channel info from event source_metadata and cache it.

  Called when processing any incoming event (e.g. message_received).
  Stores the most recent operator channel for approval notification routing.
  """
  if not source_metadata or not isinstance(source_metadata, dict):
    return

  channel_type = source_metadata.get("channel_type") or source_metadata.get("type")
  if not channel_type:
    return

  _last_active_channel_per_agent[agent_id] = {
    "channel_type": channel_type,
    "channel_config": source_metadata.get("channel_config", source_metadata),
    "operator_is_human": source_metadata.get("operator_is_human", True),
    "timestamp": _iso_now(),
  }


def _dispatch_message_to_operator_via_last_active_or_default_channel(
  agent_id: str,
  message_text: str,
  agent_config: Dict[str, Any],
) -> bool:
  """Send a fire-and-forget message to the operator using the routing cascade.

  Routing cascade (spec section 5.4):
  1. Last active channel (in-memory cache from most recent inbound event)
  2. Agent's default_response_channel (from agent config in DB)
  3. Skip (just log — caller can discover via get_pending_approvals, session log, etc.)

  A "channel" is transport + chat instance (e.g. telegram + chat_id, whatsapp + jid).

  Returns True if the message was dispatched to a transport, False if no channel was available.
  """
  channel_info = _last_active_channel_per_agent.get(agent_id)

  if channel_info is None:
    default_response_channel_raw = agent_config.get("default_response_channel")
    if default_response_channel_raw:
      try:
        channel_info = json.loads(default_response_channel_raw) if isinstance(default_response_channel_raw, str) else default_response_channel_raw
        if isinstance(channel_info, dict):
          channel_info = {
            "channel_type": channel_info.get("type", channel_info.get("channel_type", "")),
            "channel_config": channel_info,
          }
      except (json.JSONDecodeError, TypeError):
        channel_info = None

  if channel_info is None:
    MCPLogger.log(TOOL_LOG_NAME,
      f"Outbound message skipped for agent {agent_id}: no active channel or default_response_channel configured")
    return False

  channel_type = channel_info.get("channel_type", "")
  channel_config = channel_info.get("channel_config", {})

  try:
    if channel_type == "telegram":
      chat_id = channel_config.get("chat_id")
      if chat_id:
        _call_tool(_suffixed_tool_name("social"), {"input": {
          "operation": "send_message",
          "chat_id": chat_id,
          "text": message_text,
          "tool_unlock_token": "__auto__",
        }})
        return True
    elif channel_type == "whatsapp":
      jid = channel_config.get("jid")
      if jid:
        _call_tool(_suffixed_tool_name("whatsapp"), {"input": {
          "operation": "call_whatsmeow",
          "data": {
            "method": "SendMessage",
            "params": {"to": jid, "message": {"conversation": message_text}},
          },
          "tool_unlock_token": "__auto__",
        }})
        return True
    elif channel_type in ("desktop", "user"):
      _call_tool(_suffixed_tool_name("user"), {"input": {
        "operation": "toast",
        "message": message_text,
        "tool_unlock_token": "__auto__",
      }})
      return True
    else:
      MCPLogger.log(TOOL_LOG_NAME,
        f"Outbound message: unsupported channel_type '{channel_type}' for agent {agent_id}, skipping")
  except Exception as dispatch_error:
    MCPLogger.log(TOOL_LOG_NAME,
      f"Outbound message dispatch failed for agent {agent_id} (non-blocking): {dispatch_error}")
  return False


def _send_approval_notification_to_operator(
  agent_id: str,
  approval_request_id: str,
  tool_name: str,
  tool_arguments_summary: str,
  agent_config: Dict[str, Any],
) -> None:
  """Format and send a fire-and-forget approval request notification to the operator."""
  display_name = agent_config.get("display_name", agent_id)

  notification_message_text = (
    f"[Approval Required] Agent '{display_name}' wants to call tool '{tool_name}' "
    f"with arguments: {tool_arguments_summary[:300]}. "
    f"Approval ID: {approval_request_id}. "
    f"Use approve_action or deny_action with this ID to respond."
  )

  _dispatch_message_to_operator_via_last_active_or_default_channel(
    agent_id, notification_message_text, agent_config
  )


APPROVAL_REQUEST_DEFAULT_TIMEOUT_SECONDS = 300

def _mark_persisted_approval_request_resolved(approval_request_id: str, resolution_status: str, resolution_reason: str = "") -> None:
  """Update the durable approval_requests row to a terminal status."""
  _call_sqlite(
    """UPDATE approval_requests SET status = :status, resolved_at = :resolved_at, resolution_reason = :reason
    WHERE approval_request_id = :approval_request_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "approval_request_id": approval_request_id,
      "status": resolution_status,
      "resolved_at": _iso_now(),
      "reason": resolution_reason,
    },
  )

def _request_approval_for_tool_call(
  agent_id: str,
  run_id: str,
  session_id: str,
  step_number: int,
  tool_name: str,
  tool_arguments: Dict[str, Any],
  agent_config: Dict[str, Any],
) -> Tuple[bool, str]:
  """Pause execution and wait for human approval before running a tool (spec §5.3).

  Transitions to WAITING_FOR_APPROVAL, sends a notification to the operator,
  and blocks via threading.Event until approve/deny/timeout.

  Returns:
    (True, "") if approved — caller should proceed with tool execution.
    (False, denial_or_timeout_reason) if denied or timed out.
  """
  approval_request_id = f"apr-{hashlib.md5(f'{agent_id}{run_id}{tool_name}{time.time()}'.encode()).hexdigest()[:12]}"
  approval_timeout_seconds = APPROVAL_REQUEST_DEFAULT_TIMEOUT_SECONDS

  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="EXECUTING_TOOL", to_state="WAITING_FOR_APPROVAL",
    checkpoint_snapshot={
      "run_id": run_id, "session_id": session_id, "step_number": step_number,
      "approval_request_id": approval_request_id,
      "tool_name": tool_name,
      "tool_arguments_preview": str(tool_arguments)[:500],
    },
  )
  if not transition_ok:
    return False, f"Failed to transition to WAITING_FOR_APPROVAL: {transition_err}"

  tool_arguments_summary = str(tool_arguments)[:500]

  _append_session_log_entry(agent_id, run_id, "approval_requested", {
    "approval_request_id": approval_request_id,
    "tool_name": tool_name,
    "tool_arguments_summary": tool_arguments_summary,
    "timeout_seconds": approval_timeout_seconds,
  })

  # Durable mirror of this request: survives a restart so the operator can
  # still discover it (as 'orphaned') even though the waiter thread is gone.
  _call_sqlite(
    """INSERT OR REPLACE INTO approval_requests
    (approval_request_id, agent_id, run_id, session_id, tool_name, tool_arguments_summary, status, requested_at, timeout_seconds)
    VALUES (:approval_request_id, :agent_id, :run_id, :session_id, :tool_name, :tool_arguments_summary, 'pending', :requested_at, :timeout_seconds)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "approval_request_id": approval_request_id,
      "agent_id": agent_id,
      "run_id": run_id,
      "session_id": session_id,
      "tool_name": tool_name,
      "tool_arguments_summary": tool_arguments_summary,
      "requested_at": _iso_now(),
      "timeout_seconds": approval_timeout_seconds,
    },
  )

  response_event = threading.Event()

  _pending_approval_requests[approval_request_id] = {
    "agent_id": agent_id,
    "run_id": run_id,
    "session_id": session_id,
    "tool_name": tool_name,
    "tool_arguments": tool_arguments,
    "tool_arguments_summary": tool_arguments_summary,
    "step_number": step_number,
    "requested_at": _iso_now(),
    "timeout_seconds": approval_timeout_seconds,
    "response_event": response_event,
    "decision": None,
    "decision_reason": None,
    "decision_constraints": None,
  }

  _send_approval_notification_to_operator(
    agent_id, approval_request_id, tool_name, tool_arguments_summary, agent_config,
  )

  approval_received = response_event.wait(timeout=approval_timeout_seconds)

  pending_entry = _pending_approval_requests.pop(approval_request_id, None)

  if not approval_received or pending_entry is None or pending_entry.get("decision") is None:
    _append_session_log_entry(agent_id, run_id, "approval_timeout", {
      "approval_request_id": approval_request_id,
      "tool_name": tool_name,
      "timeout_seconds": approval_timeout_seconds,
    })
    _mark_persisted_approval_request_resolved(approval_request_id, "timeout")

    _transition_agent_state(
      agent_id, run_id, session_id, step_number + 1,
      from_state="WAITING_FOR_APPROVAL", to_state="EXECUTING_TOOL",
      checkpoint_snapshot={"run_id": run_id, "approval_result": "timeout"},
    )
    return False, f"Approval timed out after {approval_timeout_seconds}s for tool '{tool_name}'"

  decision = pending_entry.get("decision")

  if decision == "approved":
    _append_session_log_entry(agent_id, run_id, "approval_granted", {
      "approval_request_id": approval_request_id,
      "tool_name": tool_name,
      "constraints": pending_entry.get("decision_constraints"),
    })
    _mark_persisted_approval_request_resolved(approval_request_id, "approved")
    _transition_agent_state(
      agent_id, run_id, session_id, step_number + 1,
      from_state="WAITING_FOR_APPROVAL", to_state="EXECUTING_TOOL",
      checkpoint_snapshot={"run_id": run_id, "approval_result": "approved"},
    )
    return True, ""

  denial_reason = pending_entry.get("decision_reason", "Denied by operator")
  _append_session_log_entry(agent_id, run_id, "approval_denied", {
    "approval_request_id": approval_request_id,
    "tool_name": tool_name,
    "reason": denial_reason,
  })
  _mark_persisted_approval_request_resolved(approval_request_id, "denied", denial_reason)
  _transition_agent_state(
    agent_id, run_id, session_id, step_number + 1,
    from_state="WAITING_FOR_APPROVAL", to_state="EXECUTING_TOOL",
    checkpoint_snapshot={"run_id": run_id, "approval_result": "denied", "reason": denial_reason},
  )
  return False, f"Tool '{tool_name}' denied by operator: {denial_reason}"


def _resolve_orphaned_persisted_approval_request(approval_request_id: str, resolution_status: str, resolution_reason: str) -> Optional[Dict]:
  """Resolve a persisted approval whose waiter thread died with a previous process.

  Returns an MCP response dict when the row existed in a resolvable state
  ('pending'/'orphaned'), an error response when it was already resolved, or
  None when no persisted row exists at all.
  """
  rows = _parse_rows_from_mcp_query_response(_call_sqlite(
    "SELECT approval_request_id, agent_id, tool_name, status FROM approval_requests WHERE approval_request_id = :approval_request_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"approval_request_id": approval_request_id},
  ))
  if not rows:
    return None
  persisted_row = rows[0]
  persisted_status = persisted_row.get("status", "")
  if persisted_status not in ("pending", "orphaned"):
    return create_error_response(
      f"Approval '{approval_request_id}' was already resolved (status={persisted_status})."
    )
  _mark_persisted_approval_request_resolved(approval_request_id, resolution_status, resolution_reason)
  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": resolution_status,
      "approval_request_id": approval_request_id,
      "tool_name": persisted_row.get("tool_name"),
      "agent_id": persisted_row.get("agent_id"),
      "note": (
        "This approval belonged to a run that is no longer waiting (the server restarted "
        "and the agent was recovered to IDLE). The record has been resolved for audit; "
        "the original tool call was NOT executed. Re-send the message to retry the task."
      ),
    }, indent=2)}],
    "isError": False,
  }


def handle_approve_action(params: Dict) -> Dict:
  """MCP operation: grant a pending approval request."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  approval_request_id = params.get("approval_request_id")
  if not approval_request_id:
    return create_error_response("approval_request_id is required")

  constraints = params.get("constraints")

  pending = _pending_approval_requests.get(approval_request_id)
  if pending is None:
    orphan_resolution_response = _resolve_orphaned_persisted_approval_request(
      approval_request_id, "approved", "approved by operator after restart (original run no longer waiting)")
    if orphan_resolution_response is not None:
      return orphan_resolution_response
    return create_error_response(f"No pending approval found with ID '{approval_request_id}'. It may have already been resolved or timed out.")

  pending["decision"] = "approved"
  pending["decision_constraints"] = constraints
  pending["response_event"].set()

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "approved",
      "approval_request_id": approval_request_id,
      "tool_name": pending.get("tool_name"),
      "agent_id": pending.get("agent_id"),
    }, indent=2)}],
    "isError": False,
  }


def handle_deny_action(params: Dict) -> Dict:
  """MCP operation: deny a pending approval request."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  approval_request_id = params.get("approval_request_id")
  if not approval_request_id:
    return create_error_response("approval_request_id is required")

  reason = params.get("reason", "Denied by operator")

  pending = _pending_approval_requests.get(approval_request_id)
  if pending is None:
    orphan_resolution_response = _resolve_orphaned_persisted_approval_request(
      approval_request_id, "denied", reason)
    if orphan_resolution_response is not None:
      return orphan_resolution_response
    return create_error_response(f"No pending approval found with ID '{approval_request_id}'. It may have already been resolved or timed out.")

  pending["decision"] = "denied"
  pending["decision_reason"] = reason
  pending["response_event"].set()

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "denied",
      "approval_request_id": approval_request_id,
      "reason": reason,
      "tool_name": pending.get("tool_name"),
      "agent_id": pending.get("agent_id"),
    }, indent=2)}],
    "isError": False,
  }


def handle_get_pending_approvals(params: Dict) -> Dict:
  """MCP operation: list all currently pending approval requests.

  Any MCP client on any transport can discover and act on pending approvals.
  Optionally filtered by agent_id. Also lists 'orphaned' approvals persisted
  by a previous process (pending when it died): those have no live waiter but
  can still be resolved with approve_action / deny_action for the audit trail.
  """
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  filter_agent_id = params.get("agent_id")
  now = datetime.datetime.utcnow()

  pending_list = []
  for apr_id, apr_data in list(_pending_approval_requests.items()):
    if filter_agent_id and apr_data.get("agent_id") != filter_agent_id:
      continue

    requested_at_str = apr_data.get("requested_at", "")
    timeout_seconds = apr_data.get("timeout_seconds", APPROVAL_REQUEST_DEFAULT_TIMEOUT_SECONDS)

    seconds_elapsed = 0
    try:
      requested_at_dt = datetime.datetime.fromisoformat(requested_at_str.replace("Z", "+00:00"))
      if requested_at_dt.tzinfo:
        requested_at_dt = requested_at_dt.replace(tzinfo=None)
      seconds_elapsed = (now - requested_at_dt).total_seconds()
    except (ValueError, TypeError):
      pass

    seconds_until_timeout = max(0, timeout_seconds - seconds_elapsed)

    pending_list.append({
      "approval_request_id": apr_id,
      "agent_id": apr_data.get("agent_id"),
      "tool_name": apr_data.get("tool_name"),
      "tool_arguments_summary": apr_data.get("tool_arguments_summary", ""),
      "requested_at": requested_at_str,
      "timeout_seconds": timeout_seconds,
      "seconds_until_timeout": round(seconds_until_timeout, 1),
    })

  orphaned_sql = """SELECT approval_request_id, agent_id, run_id, tool_name, tool_arguments_summary, requested_at
    FROM approval_requests WHERE status = 'orphaned'"""
  orphaned_bindings: Dict[str, Any] = {}
  if filter_agent_id:
    orphaned_sql += " AND agent_id = :agent_id"
    orphaned_bindings["agent_id"] = filter_agent_id
  orphaned_list = _parse_rows_from_mcp_query_response(_call_sqlite(
    orphaned_sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=orphaned_bindings or None,
  ))

  response_payload: Dict[str, Any] = {
    "pending_approval_count": len(pending_list),
    "pending_approvals": pending_list,
  }
  if orphaned_list:
    response_payload["orphaned_approval_count"] = len(orphaned_list)
    response_payload["orphaned_approvals"] = orphaned_list
    response_payload["orphaned_approvals_note"] = (
      "These were pending when a previous server process stopped; their runs are no longer "
      "waiting. Resolve them with approve_action or deny_action (records only - no tool will run)."
    )

  return {
    "content": [{"type": "text", "text": json.dumps(response_payload, indent=2)}],
    "isError": False,
  }


# ===============================================================================
# Phase 5: Dead Letter Queue Management (spec §7, §9.1)
# ===============================================================================

def handle_get_dlq(params: Dict) -> Dict:
  """MCP operation: list dead letter queue entries for inspection."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  agent_id = params.get("agent_id")
  status_filter = params.get("status", "pending")
  limit = params.get("limit", 20)

  if agent_id:
    result = _call_sqlite(
      """SELECT dlq_id, agent_id, failure_reason, failure_category,
      retry_count, max_retries, status, created_at, resolved_at
      FROM dead_letter_queue
      WHERE agent_id = :agent_id AND status = :status
      ORDER BY created_at DESC LIMIT :limit""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": agent_id, "status": status_filter, "limit": limit}
    )
  else:
    result = _call_sqlite(
      """SELECT dlq_id, agent_id, failure_reason, failure_category,
      retry_count, max_retries, status, created_at, resolved_at
      FROM dead_letter_queue
      WHERE status = :status
      ORDER BY created_at DESC LIMIT :limit""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"status": status_filter, "limit": limit}
    )

  if result.get("isError"):
    return result

  response_text = _extract_text_from_mcp_response(result)
  try:
    data = json.loads(response_text)
    entries = data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    entries = []

  return {
    "content": [{"type": "text", "text": json.dumps({
      "dlq_entry_count": len(entries),
      "dlq_entries": entries,
      "status_filter": status_filter,
    }, indent=2)}],
    "isError": False,
  }


def handle_retry_dlq(params: Dict) -> Dict:
  """MCP operation: retry a dead-lettered event by re-enqueuing it."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  dlq_id = params.get("dlq_id")
  if not dlq_id:
    return create_error_response("dlq_id is required")

  dlq_result = _call_sqlite(
    "SELECT * FROM dead_letter_queue WHERE dlq_id = :dlq_id AND status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"dlq_id": dlq_id}
  )

  try:
    dlq_data = json.loads(_extract_text_from_mcp_response(dlq_result))
    dlq_rows = dlq_data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    dlq_rows = []

  if not dlq_rows:
    return create_error_response(f"No pending DLQ entry found with dlq_id={dlq_id}")

  dlq_entry = dlq_rows[0]
  retry_count = dlq_entry.get("retry_count", 0)
  max_retries = dlq_entry.get("max_retries", 3)

  if retry_count >= max_retries:
    return create_error_response(
      f"Maximum retries exceeded for DLQ entry {dlq_id}: {retry_count}/{max_retries}")

  agent_id = dlq_entry.get("agent_id", "")
  original_event_json_raw = dlq_entry.get("original_event_json", "{}")

  try:
    original_event_payload = json.loads(original_event_json_raw) if isinstance(original_event_json_raw, str) else original_event_json_raw
  except (json.JSONDecodeError, TypeError):
    original_event_payload = {"message": "(unparseable original event)"}

  enqueue_ok, enqueue_err, _ = _enqueue_event(
    agent_id=agent_id,
    event_type="retry_from_dlq",
    payload=original_event_payload if isinstance(original_event_payload, dict) else {"raw": original_event_payload},
    priority="normal",
  )

  if not enqueue_ok:
    return create_error_response(f"Failed to re-enqueue DLQ event: {enqueue_err}")

  _call_sqlite(
    """UPDATE dead_letter_queue SET status = 'retried', resolved_at = :now, retry_count = retry_count + 1
    WHERE dlq_id = :dlq_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"dlq_id": dlq_id, "now": _iso_now()}
  )

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "retried",
      "dlq_id": dlq_id,
      "agent_id": agent_id,
      "retry_count": retry_count + 1,
    }, indent=2)}],
    "isError": False,
  }


def handle_discard_dlq(params: Dict) -> Dict:
  """MCP operation: discard a dead-lettered event (mark as permanently resolved)."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  dlq_id = params.get("dlq_id")
  if not dlq_id:
    return create_error_response("dlq_id is required")

  _call_sqlite(
    """UPDATE dead_letter_queue SET status = 'discarded', resolved_at = :now
    WHERE dlq_id = :dlq_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"dlq_id": dlq_id, "now": _iso_now()}
  )

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "discarded",
      "dlq_id": dlq_id,
    }, indent=2)}],
    "isError": False,
  }


# ===============================================================================
# Phase 6: Observability Operations (spec §9.1 — last 3 unimplemented ops)
#
# Simple read-only queries against session_log, agent_checkpoints, agent_run_log.
# ===============================================================================

def handle_get_session_log(params: Dict) -> Dict:
  """MCP operation: query the session_log table with filtering.

  Params:
    agent_id (required): which agent's log to query.
    run_id (optional): filter to a specific run.
    entry_type (optional): filter to a specific entry type.
    limit (optional, default 100): max entries.
    since (optional): ISO timestamp — return only entries after this time.

  Returns entries ordered by entry_id ascending.
  """
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  agent_id = params.get("agent_id")
  if not agent_id:
    return create_error_response("agent_id is required for get_session_log")

  run_id_filter = params.get("run_id")
  entry_type_filter = params.get("entry_type")
  limit = params.get("limit", 100)
  since_filter = params.get("since")

  where_clauses = ["agent_id = :agent_id"]
  bindings: Dict[str, Any] = {"agent_id": agent_id, "limit_val": limit}

  if run_id_filter:
    where_clauses.append("run_id = :run_id")
    bindings["run_id"] = run_id_filter

  if entry_type_filter:
    where_clauses.append("entry_type = :entry_type")
    bindings["entry_type"] = entry_type_filter

  if since_filter:
    where_clauses.append("created_at > :since")
    bindings["since"] = since_filter

  where_sql = " AND ".join(where_clauses)

  result = _call_sqlite(
    f"SELECT entry_id, agent_id, run_id, entry_type, payload_json, created_at FROM session_log WHERE {where_sql} ORDER BY entry_id ASC LIMIT :limit_val",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings=bindings,
  )

  rows = _parse_rows_from_mcp_query_response(result)

  return {
    "content": [{"type": "text", "text": json.dumps({
      "agent_id": agent_id,
      "entry_count": len(rows),
      "entries": rows,
    }, indent=2, default=str)}],
    "isError": False,
  }


def handle_get_checkpoints(params: Dict) -> Dict:
  """MCP operation: query agent_checkpoints for a specific run.

  Params:
    run_id (required): which run's checkpoints to retrieve.

  Returns checkpoints ordered by step_number ascending.
  """
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  run_id = params.get("run_id")
  if not run_id:
    return create_error_response("run_id is required for get_checkpoints")

  result = _call_sqlite(
    "SELECT checkpoint_id, agent_id, run_id, step_number, state_json, created_at FROM agent_checkpoints WHERE run_id = :run_id ORDER BY step_number ASC",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"run_id": run_id},
  )

  rows = _parse_rows_from_mcp_query_response(result)

  for row in rows:
    state_json_raw = row.get("state_json", "")
    if isinstance(state_json_raw, str) and len(state_json_raw) > 500:
      row["state_json"] = state_json_raw[:500] + "...(truncated)"

  return {
    "content": [{"type": "text", "text": json.dumps({
      "run_id": run_id,
      "checkpoint_count": len(rows),
      "checkpoints": rows,
    }, indent=2, default=str)}],
    "isError": False,
  }


def handle_get_run_log(params: Dict) -> Dict:
  """MCP operation: query agent_run_log for an agent's run history.

  Params:
    agent_id (required): which agent's runs to query.
    limit (optional, default 20): max entries.
    since (optional): ISO timestamp — return only runs started after this time.

  Returns runs ordered by started_at descending (most recent first).
  """
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  agent_id = params.get("agent_id")
  if not agent_id:
    return create_error_response("agent_id is required for get_run_log")

  limit = params.get("limit", 20)
  since_filter = params.get("since")

  where_clauses = ["agent_id = :agent_id"]
  bindings: Dict[str, Any] = {"agent_id": agent_id, "limit_val": limit}

  if since_filter:
    where_clauses.append("started_at > :since")
    bindings["since"] = since_filter

  where_sql = " AND ".join(where_clauses)

  result = _call_sqlite(
    f"SELECT run_id, agent_id, event_type, event_source_id, started_at, completed_at, llm_calls_made, tool_calls_made, tokens_consumed, status, error_message FROM agent_run_log WHERE {where_sql} ORDER BY started_at DESC LIMIT :limit_val",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings=bindings,
  )

  rows = _parse_rows_from_mcp_query_response(result)

  return {
    "content": [{"type": "text", "text": json.dumps({
      "agent_id": agent_id,
      "run_count": len(rows),
      "runs": rows,
    }, indent=2, default=str)}],
    "isError": False,
  }


# ===============================================================================
# Phase 6: Cost Tracking (spec §12.4)
#
# Track token usage per agent, per run, per day in agent_run_log.
# Configurable daily token budget via max_tokens_per_day agent config.
# ===============================================================================

def _create_run_log_entry_at_start(agent_id: str, run_id: str, event_type: str = "user_message") -> None:
  """Insert a run log row at the START of a run, with status='running'.

  The entry captures started_at accurately. Counters (llm_calls_made,
  tool_calls_made, tokens_consumed) are set to 0 and updated at run completion.
  """
  _call_sqlite(
    """INSERT OR IGNORE INTO agent_run_log
      (run_id, agent_id, event_type, started_at, llm_calls_made, tool_calls_made, tokens_consumed, status)
    VALUES
      (:run_id, :agent_id, :event_type, :started_at, 0, 0, 0, 'running')""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "run_id": run_id,
      "agent_id": agent_id,
      "event_type": event_type,
      "started_at": _iso_now(),
    },
  )

def _complete_run_log_entry_at_finish(
  run_id: str,
  status: str,
  llm_calls_made: int,
  tool_calls_made: int,
  tokens_consumed: int,
  error_message: Optional[str] = None,
) -> None:
  """Update an existing run log row with final counters and status.

  Called when the run ends (completed, failed, or cancelled).
  """
  _call_sqlite(
    """UPDATE agent_run_log SET
      completed_at = :completed_at,
      llm_calls_made = :llm_calls,
      tool_calls_made = :tool_calls,
      tokens_consumed = :tokens_consumed,
      status = :status,
      error_message = :error_message
    WHERE run_id = :run_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "run_id": run_id,
      "completed_at": _iso_now(),
      "llm_calls": llm_calls_made,
      "tool_calls": tool_calls_made,
      "tokens_consumed": tokens_consumed,
      "status": status,
      "error_message": error_message,
    },
  )

def _extract_token_count_from_llm_response(llm_response_data: Dict) -> int:
  """Extract token usage from an LLM response, falling back to estimation.

  Checks for standard OpenAI-compatible 'usage' fields first:
    usage.total_tokens, or usage.prompt_tokens + usage.completion_tokens.
  If no usage data, estimates from the response content size (~4 chars/token).
  """
  usage = llm_response_data.get("usage")
  if isinstance(usage, dict):
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
      return int(total)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
      combined = int(prompt_tokens) + int(completion_tokens)
      if combined > 0:
        return combined

  content_length = 0
  choices = llm_response_data.get("choices", [])
  if choices:
    msg = choices[0].get("message", {})
    content_length += len(msg.get("content", "") or "")
    for tc in msg.get("tool_calls", []):
      content_length += len(tc.get("function", {}).get("arguments", ""))
  elif "content" in llm_response_data:
    content_length = len(llm_response_data.get("content", "") or "")
  elif "raw_text" in llm_response_data:
    content_length = len(llm_response_data.get("raw_text", "") or "")

  return max(1, content_length // 4)

def _check_daily_token_budget_for_agent(agent_id: str, agent_config: Dict) -> Tuple[bool, int, int]:
  """Check if agent is within its daily token budget.

  Returns:
    (within_budget: bool, tokens_used_today: int, daily_budget_limit: int)

  A budget of 0 means unlimited (always within budget).
  """
  daily_budget_limit = agent_config.get("max_tokens_per_day", 100000)
  if daily_budget_limit is None:
    daily_budget_limit = 100000
  if daily_budget_limit <= 0:
    return (True, 0, 0)

  today_start_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT00:00:00")

  result = _call_sqlite(
    """SELECT COALESCE(SUM(tokens_consumed), 0) as total_tokens
    FROM agent_run_log
    WHERE agent_id = :agent_id AND started_at >= :today_start""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "today_start": today_start_utc},
  )

  rows = _parse_rows_from_mcp_query_response(result)
  tokens_used_today = 0
  if rows and isinstance(rows[0], dict):
    tokens_used_today = int(rows[0].get("total_tokens", 0) or 0)

  within_budget = tokens_used_today < daily_budget_limit
  return (within_budget, tokens_used_today, daily_budget_limit)

def _auto_pause_agent_for_budget_exceeded(agent_id: str, tokens_used_today: int, daily_budget_limit: int, run_id: str) -> None:
  """Auto-pause an agent that has exceeded its daily token budget.

  Logs the event and pauses the agent so no further runs start.
  """
  _call_sqlite(
    "UPDATE agents SET is_paused = 1, updated_at = :now WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "now": _iso_now()},
  )
  _append_session_log_entry(agent_id, run_id, "budget_exceeded", {
    "tokens_used_today": tokens_used_today,
    "daily_budget_limit": daily_budget_limit,
    "action": "agent_auto_paused",
  })
  MCPLogger.log(TOOL_LOG_NAME, f"Agent {agent_id} auto-paused: daily token budget exceeded ({tokens_used_today}/{daily_budget_limit})")


# ===============================================================================
# Phase 5: Background Reflection Cycles (spec §3.6)
# ===============================================================================

# Cross-reload shared state: idle timestamps must survive a hot reload or every
# reload would reset reflection idle timers.
_reflection_idle_tracker: Dict[str, str] = _get_phase2_shared_state()['reflection_idle_tracker']

REFLECTION_PROMPT_TEMPLATE = """You are reviewing the recent activity of an AI agent to extract insights and consolidate memories.

## Recent Session Activity
{recent_entries_text}

## Current Archival Memories (top by access count)
{existing_memories_text}

## Current Working Context
{working_context}

## Your Task
Review the recent activity and:
1. Extract new facts, preferences, decisions, or rules the agent learned.
2. Identify any contradictions between new information and existing memories.
3. Suggest which existing memories should be updated or consolidated.

Output each extracted insight on its own line in this format:
TYPE: content

Where TYPE is one of: fact, preference, project_knowledge, decision, task, rule

Only output insights. Do not output commentary or explanations."""


def _build_reflection_prompt(
  agent_id: str,
  recent_entries: List[Dict[str, Any]],
  existing_memories: List[Dict[str, Any]],
  working_context: str,
) -> str:
  """Construct the prompt for a reflection cycle.

  Includes recent session entries, current archival memories (top N by access_count),
  and current working_context.
  """
  recent_entries_text = ""
  for entry in recent_entries[:50]:
    entry_type = entry.get("entry_type", "")
    payload_str = entry.get("payload_json", "{}")
    try:
      payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
    except (json.JSONDecodeError, TypeError):
      payload = {}
    preview = ""
    if entry_type in ("message_received", "message_sent"):
      preview = str(payload.get("message", payload.get("response_preview", "")))[:300]
    elif entry_type in ("tool_executed", "tool_failed"):
      preview = f"{payload.get('tool_name', '?')}: {str(payload.get('result_preview', payload.get('error_preview', '')))[:200]}"
    elif entry_type == "llm_response":
      preview = str(payload.get("content_preview", ""))[:200]
    else:
      preview = str(payload)[:150]
    recent_entries_text += f"- [{entry_type}] {preview}\n"

  if not recent_entries_text:
    recent_entries_text = "(no recent entries)\n"

  existing_memories_text = ""
  for mem in existing_memories[:20]:
    mem_type = mem.get("memory_type", "?")
    mem_content = str(mem.get("content", ""))[:200]
    existing_memories_text += f"- [{mem_type}] {mem_content}\n"

  if not existing_memories_text:
    existing_memories_text = "(no existing memories)\n"

  return REFLECTION_PROMPT_TEMPLATE.format(
    recent_entries_text=recent_entries_text,
    existing_memories_text=existing_memories_text,
    working_context=working_context or "(none)",
  )


def _execute_reflection_cycle(
  agent_id: str,
  run_id: str,
  session_id: str,
) -> Dict[str, Any]:
  """Execute a background reflection cycle for an agent (spec §3.6).

  1. Transition IDLE → REFLECTING
  2. Load recent transcript entries since last reflection
  3. If nothing to reflect on, skip
  4. Call compaction model with reflection prompt
  5. Parse extracted insights, insert/update archival memories
  6. Log reflection_completed
  7. Transition REFLECTING → IDLE
  """
  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return {"status": "error", "reason": f"Agent not found: {agent_id}"}

  step_number = 1
  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="IDLE", to_state="REFLECTING",
    checkpoint_snapshot={"run_id": run_id, "session_id": session_id, "reflection": True},
  )
  if not transition_ok:
    return {"status": "error", "reason": f"Cannot start reflection: {transition_err}"}

  reflection_start_time = time.time()

  last_reflection_rows = _parse_rows_from_mcp_query_response(
    _query_session_log(agent_id=agent_id, entry_type="reflection_completed", limit=1)
  )
  last_reflection_timestamp = None
  if last_reflection_rows:
    last_reflection_timestamp = last_reflection_rows[0].get("created_at")

  recent_entry_rows = _parse_rows_from_mcp_query_response(
    _query_session_log(
      agent_id=agent_id,
      since=last_reflection_timestamp,
      limit=100,
    )
  )

  meaningful_entry_types = {
    "message_received", "message_sent", "tool_executed", "tool_failed",
    "llm_response", "core_memory_updated", "memory_inserted",
  }
  meaningful_entries = [e for e in recent_entry_rows if e.get("entry_type") in meaningful_entry_types]

  if not meaningful_entries:
    _append_session_log_entry(agent_id, run_id, "reflection_completed", {
      "skipped": True,
      "reason": "no_activity_since_last_reflection",
    })
    step_number += 1
    _transition_agent_state(
      agent_id, run_id, session_id, step_number,
      from_state="REFLECTING", to_state="IDLE",
      checkpoint_snapshot={"run_id": run_id, "reflection_skipped": True},
    )
    return {"status": "skipped", "reason": "no_activity_since_last_reflection"}

  search_ok, existing_memories, search_err = _search_archival_memory(agent_id, "important facts and preferences", limit=20)
  if not search_ok:
    existing_memories = []

  working_context = agent_config.get("working_context", "")

  prompt = _build_reflection_prompt(agent_id, meaningful_entries, existing_memories, working_context)

  compaction_endpoint = agent_config.get("compaction_endpoint", agent_config.get("llm_endpoint", ""))
  compaction_provider = agent_config.get("compaction_provider", agent_config.get("llm_provider", ""))
  compaction_model = agent_config.get("compaction_model", agent_config.get("llm_model", ""))

  llm_params: Dict[str, Any] = {
    "operation": "chat",
    "messages": [
      {"role": "system", "content": "You are a reflective memory consolidation assistant."},
      {"role": "user", "content": prompt},
    ],
    "temperature": 0.3,
    "max_tokens": 2048,
    "repetition_penalty": 1.1,
    "stop": ["<|im_end|>", "<|im_start|>"],
    "enable_thinking": False,
    "tool_unlock_token": "__auto__",
  }

  if compaction_endpoint:
    llm_params["endpoint"] = compaction_endpoint
  if compaction_provider:
    llm_params["provider"] = compaction_provider
  if compaction_model:
    llm_params["model"] = compaction_model
  _apply_provider_host_params_for_llm_call(compaction_provider, llm_params, compaction_endpoint)

  _append_session_log_entry(agent_id, run_id, "llm_called", {
    "provider": compaction_provider,
    "model": compaction_model,
    "purpose": "reflection",
  })

  llm_result = _call_tool(_suffixed_tool_name("llm"), {"input": llm_params})

  memories_added = 0
  memories_updated = 0

  if not llm_result.get("isError"):
    llm_text = _extract_text_from_mcp_response(llm_result)
    try:
      llm_data = json.loads(llm_text)
      if "choices" in llm_data and llm_data["choices"]:
        reflection_output = llm_data["choices"][0].get("message", {}).get("content", "")
      elif "content" in llm_data:
        reflection_output = llm_data.get("content", "")
      else:
        reflection_output = llm_data.get("raw_text", llm_text)
    except (json.JSONDecodeError, TypeError):
      reflection_output = llm_text

    valid_types = {"fact", "preference", "project_knowledge", "decision", "task", "rule"}
    for line in reflection_output.split("\n"):
      line = line.strip()
      if not line or ":" not in line:
        continue
      colon_index = line.index(":")
      memory_type_candidate = line[:colon_index].strip().lower()
      memory_content = line[colon_index + 1:].strip()
      if memory_type_candidate in valid_types and len(memory_content) > 5:
        insert_ok, memory_id, insert_err = _insert_archival_memory(
          agent_id, memory_content, memory_type=memory_type_candidate,
          source_run_id=run_id, confidence_score=0.6,
        )
        if insert_ok:
          memories_added += 1

  reflection_duration_ms = int((time.time() - reflection_start_time) * 1000)

  _append_session_log_entry(agent_id, run_id, "reflection_completed", {
    "skipped": False,
    "meaningful_entries_reviewed": len(meaningful_entries),
    "memories_added": memories_added,
    "memories_updated": memories_updated,
    "duration_ms": reflection_duration_ms,
  })

  _reflection_idle_tracker[agent_id] = _iso_now()

  step_number += 1
  _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="REFLECTING", to_state="IDLE",
    checkpoint_snapshot={
      "run_id": run_id, "reflection_completed": True,
      "memories_added": memories_added, "duration_ms": reflection_duration_ms,
    },
  )

  return {
    "status": "completed",
    "memories_added": memories_added,
    "memories_updated": memories_updated,
    "entries_reviewed": len(meaningful_entries),
    "duration_ms": reflection_duration_ms,
  }


def _check_and_fire_reflection_triggers() -> int:
  """Check all eligible agents for idle timeout and fire reflection_trigger events.

  Called periodically (from cron scheduler or a dedicated timer).
  Returns the number of reflection triggers fired.
  """
  agents_result = _call_sqlite(
    """SELECT agent_id, reflection_idle_timeout_minutes, reflection_enabled, is_paused, current_state
    FROM agents WHERE reflection_enabled = 1 AND is_paused = 0""",
    database=AGENT_KERNEL_DATABASE_NAME,
  )

  try:
    data = json.loads(_extract_text_from_mcp_response(agents_result))
    agent_rows = data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    return 0

  triggers_fired = 0
  now = datetime.datetime.utcnow()

  for row in agent_rows:
    agent_id = row.get("agent_id")
    current_state = row.get("current_state", "IDLE")
    timeout_minutes = row.get("reflection_idle_timeout_minutes", 30) or 30

    if current_state != "IDLE":
      continue

    last_activity_str = _reflection_idle_tracker.get(agent_id)
    if last_activity_str is None:
      _reflection_idle_tracker[agent_id] = _iso_now()
      continue

    try:
      last_activity_dt = datetime.datetime.fromisoformat(last_activity_str.replace("Z", "+00:00"))
      if last_activity_dt.tzinfo:
        last_activity_dt = last_activity_dt.replace(tzinfo=None)
      idle_minutes = (now - last_activity_dt).total_seconds() / 60.0
    except (ValueError, TypeError):
      continue

    if idle_minutes >= timeout_minutes:
      enqueue_ok, enqueue_status, _queue_id = _enqueue_event(
        agent_id=agent_id,
        event_type="reflection_trigger",
        payload={"trigger": "idle_timeout", "idle_minutes": round(idle_minutes, 1)},
        priority="low",
      )
      if enqueue_ok and enqueue_status == "enqueued":
        # Ensure a mailbox worker exists to consume the event (after restart none do).
        mailbox = _get_or_create_mailbox_for_agent(agent_id)
        mailbox.signal_new_event_available()
      _reflection_idle_tracker[agent_id] = _iso_now()
      triggers_fired += 1

  return triggers_fired


def handle_reflect_now(params: Dict) -> Dict:
  """MCP operation: manually trigger a reflection cycle for an agent."""
  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema initialization failed: {schema_msg}")

  agent_id = params.get("agent_id")
  if not agent_id:
    return create_error_response("agent_id is required")

  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return create_error_response(f"Agent not found: '{agent_id}'")

  current_state = agent_config.get("current_state", "IDLE")
  if current_state != "IDLE":
    return create_error_response(f"Agent '{agent_id}' is not idle (current state: {current_state}). Cannot start reflection.")

  run_id = _generate_run_id()
  session_id = f"reflection-{run_id}"

  result = _execute_reflection_cycle(agent_id, run_id, session_id)

  return {
    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
    "isError": result.get("status") == "error",
  }


# ===============================================================================
# ReAct Loop (agent-managed tool calling — spec §3.4, §10.1)
#
# The kernel manages the full ReAct cycle:
#   1. Assemble context
#   2. Call LLM with tool_execution="caller_managed"
#   3. If tool_calls in response → execute each, log, checkpoint, call LLM again
#   4. If no tool_calls → done, save response, transition to COMPLETED
#   5. Enforce max_tool_rounds_per_run
#   6. On error → FAILED
# ===============================================================================

def _generate_run_id() -> str:
  """Generate a unique run identifier."""
  return f"run-{hashlib.md5(f'{time.time()}'.encode()).hexdigest()[:12]}"


def _build_tool_definitions_for_agent(agent_config: Dict[str, Any]) -> List[Dict[str, Any]]:
  """Build the tools array to send to the LLM, based on agent's allowed tools.

  Always includes the kernel pseudo-tools. Additionally advertises the full
  input schema of every real MCP tool EXPLICITLY named in read_tools_allowed /
  write_tools_allowed (wildcard "*" entries are not expanded — with a
  wildcard the model uses discover_available_mcp_tools and raw calls, as
  before, to keep the context footprint bounded).
  """
  read_tools_str = agent_config.get("read_tools_allowed", '["*"]')
  write_tools_str = agent_config.get("write_tools_allowed", '[]')

  try:
    read_tools = json.loads(read_tools_str) if isinstance(read_tools_str, str) else read_tools_str
  except json.JSONDecodeError:
    read_tools = ["*"]
  try:
    write_tools = json.loads(write_tools_str) if isinstance(write_tools_str, str) else write_tools_str
  except json.JSONDecodeError:
    write_tools = []
  if not isinstance(read_tools, list):
    read_tools = ["*"]
  if not isinstance(write_tools, list):
    write_tools = []

  pseudo_tool_definitions = [
    {
      "type": "function",
      "function": {
        "name": "core_memory_update",
        "description": "Update your working context (persistent scratch-pad that appears in every future system prompt). Use to remember user preferences, project state, active tasks, decisions made.",
        "parameters": {
          "type": "object",
          "properties": {
            "section": {"type": "string", "enum": ["working_context"], "description": "Which core memory section to update. Currently only 'working_context' is editable."},
            "content": {"type": "string", "description": "The new content for working_context. This REPLACES the entire section."},
          },
          "required": ["section", "content"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "archival_memory_insert",
        "description": "Store a fact, preference, decision, or piece of knowledge in long-term archival memory. Memories are embedded for semantic search and persist across sessions.",
        "parameters": {
          "type": "object",
          "properties": {
            "content": {"type": "string", "description": "The text to remember."},
            "memory_type": {"type": "string", "enum": ["fact", "preference", "project_knowledge", "decision", "task", "rule"], "description": "Category of this memory."},
            "importance": {"type": "number", "description": "0.0–1.0, how important (default 0.5)."},
          },
          "required": ["content"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "archival_memory_search",
        "description": "Search your archival memory by semantic similarity. Returns the most relevant stored memories. Use when you need to recall facts, preferences, or knowledge from past sessions.",
        "parameters": {
          "type": "object",
          "properties": {
            "query": {"type": "string", "description": "Natural language query to search for."},
            "count": {"type": "integer", "description": "Max results (default 10)."},
          },
          "required": ["query"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "recall_memory_search",
        "description": "Search past conversation transcripts by keyword. Returns matching messages from previous sessions. Use to find exact quotes, details, or context that was discussed before.",
        "parameters": {
          "type": "object",
          "properties": {
            "query": {"type": "string", "description": "Keyword or phrase to search for."},
            "count": {"type": "integer", "description": "Max results (default 10)."},
          },
          "required": ["query"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "schedule_reminder",
        "description": "Schedule a future event for yourself. Creates a one-shot or recurring cron trigger. Use for follow-ups, deadlines, periodic tasks.",
        "parameters": {
          "type": "object",
          "properties": {
            "when": {"type": "string", "description": "When to fire: ISO datetime (2026-04-15T09:00:00+12:00), or relative (in 3 hours, in 30 minutes, tomorrow at 09:00)."},
            "message": {"type": "string", "description": "The reminder message/context."},
            "priority": {"type": "string", "enum": ["high", "normal", "low"], "description": "Event priority (default normal)."},
            "recurring": {"type": "string", "description": "If set, a cron expression for recurring reminders (e.g. '0 9 * * 1-5' for weekday 9am). Overrides 'when'."},
          },
          "required": ["message"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "send_to_agent",
        "description": "Send a message to another agent. The message is delivered asynchronously to the target agent's mailbox. Use when you need to delegate a task, ask for help, or collaborate with another agent.",
        "parameters": {
          "type": "object",
          "properties": {
            "agent_id": {"type": "string", "description": "The ID of the target agent to send the message to. See the agent directory in your system prompt for available agents."},
            "message": {"type": "string", "description": "The message to send to the target agent."},
          },
          "required": ["agent_id", "message"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "ask_user",
        "description": "Ask the user a question and wait for their reply. Your execution will pause until the user responds or a timeout expires. Use when you need clarification, confirmation, or information that only the user can provide.",
        "parameters": {
          "type": "object",
          "properties": {
            "question": {"type": "string", "description": "The question to ask the user. Be clear and specific about what you need."},
          },
          "required": ["question"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "discover_available_mcp_tools",
        "description": "List all MCP tools available on this server with a brief description of each. Use before attempting to call an external tool, to find out what capabilities are available (file editing, web search, shell commands, speech, etc). Returns tool names with summaries — call a tool's readme for full details.",
        "parameters": {
          "type": "object",
          "properties": {},
        },
      },
    },
  ]

  real_tool_definitions = _build_real_tool_definitions_for_explicitly_allowed_tools(read_tools + write_tools)
  return pseudo_tool_definitions + real_tool_definitions


# Bound on how many real-tool schemas are advertised to the LLM per run, so an
# over-broad allowlist cannot flood the model's context with tool definitions.
MAX_REAL_TOOL_SCHEMAS_ADVERTISED_TO_LLM = 24


def _build_real_tool_definitions_for_explicitly_allowed_tools(
  explicitly_allowed_tool_base_names: List[str],
) -> List[Dict[str, Any]]:
  """Build OpenAI function definitions for real MCP tools named in the allowlists.

  Looks up each tool's full schema (real_parameters) from the tool registry's
  ORIGINAL_TOOLS. The tool_unlock_token property is stripped from the
  advertised schema because the ReAct loop auto-injects the real token — a
  model-invented token value would only break the call. Wildcards, pseudo-tool
  names, unknown tools, and tools without a usable inner schema are skipped.
  """
  real_tool_definitions: List[Dict[str, Any]] = []
  deduplicated_base_names: List[str] = []
  for base_name in explicitly_allowed_tool_base_names:
    if not isinstance(base_name, str) or base_name == "*" or base_name in PSEUDO_TOOL_NAMES:
      continue
    if base_name not in deduplicated_base_names:
      deduplicated_base_names.append(base_name)
  if not deduplicated_base_names:
    return real_tool_definitions

  try:
    from ..tools import ORIGINAL_TOOLS
    tool_definition_by_registered_name = {t.get("name"): t for t in ORIGINAL_TOOLS if isinstance(t, dict)}
  except Exception as registry_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Real-tool schema advertising skipped (registry unavailable): {registry_error}")
    return real_tool_definitions

  for base_name in deduplicated_base_names:
    if len(real_tool_definitions) >= MAX_REAL_TOOL_SCHEMAS_ADVERTISED_TO_LLM:
      MCPLogger.log(TOOL_LOG_NAME,
        f"Real-tool schema advertising truncated at {MAX_REAL_TOOL_SCHEMAS_ADVERTISED_TO_LLM} tools")
      break
    registered_tool_definition = tool_definition_by_registered_name.get(_suffixed_tool_name(base_name))
    if registered_tool_definition is None:
      continue
    inner_schema = registered_tool_definition.get("real_parameters")
    if not isinstance(inner_schema, dict) or not isinstance(inner_schema.get("properties"), dict):
      continue
    advertised_properties = {
      prop_name: prop_schema
      for prop_name, prop_schema in inner_schema["properties"].items()
      if prop_name != "tool_unlock_token"
    }
    advertised_required = [
      required_name for required_name in (inner_schema.get("required") or [])
      if required_name != "tool_unlock_token"
    ]
    description_text = (registered_tool_definition.get("description") or "").strip().split("\n")[0][:200]
    real_tool_definitions.append({
      "type": "function",
      "function": {
        # Advertise the BASE name — the ReAct loop applies the suffix at call time.
        "name": base_name,
        "description": description_text or f"MCP tool '{base_name}' on this server.",
        "parameters": {
          "type": "object",
          "properties": advertised_properties,
          "required": advertised_required,
        },
      },
    })

  return real_tool_definitions


def _extract_agent_config_as_dict(agent_id: str) -> Optional[Dict[str, Any]]:
  """Load an agent's full config row as a dict from the agents table.

  Returns None if the agent doesn't exist or if there's a DB error.
  """
  result = _call_sqlite(
    "SELECT * FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )

  if result.get("isError"):
    return None

  response_text = _extract_text_from_mcp_response(result)
  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if not rows:
      return None
    return rows[0]
  except (json.JSONDecodeError, KeyError, IndexError, TypeError):
    return None


def _save_transcript_entry(
  agent_id: str,
  session_id: str,
  role: str,
  content: str,
  tool_name: Optional[str] = None,
) -> Tuple[bool, str]:
  """Persist a message to the transcript_entries table.

  Returns:
    (True, "") on success.
    (False, error_message) on failure.
  """
  token_estimate = _estimate_token_count_from_characters(content)
  now = _iso_now()

  bindings: Dict[str, Any] = {
    "agent_id": agent_id,
    "session_id": session_id,
    "role": role,
    "content": content,
    "token_count_estimate": token_estimate,
    "created_at": now,
  }

  if tool_name is not None:
    sql = """INSERT INTO transcript_entries (agent_id, session_id, role, content, tool_name, token_count_estimate, created_at)
    VALUES (:agent_id, :session_id, :role, :content, :tool_name, :token_count_estimate, :created_at)"""
    bindings["tool_name"] = tool_name
  else:
    sql = """INSERT INTO transcript_entries (agent_id, session_id, role, content, token_count_estimate, created_at)
    VALUES (:agent_id, :session_id, :role, :content, :token_count_estimate, :created_at)"""

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:300]
  return True, ""


# ===============================================================================
# Phase 6: Harnessed Model Support (spec §3.10)
#
# For context_mode='harnessed', the kernel does NOT run a ReAct loop.
# Instead it assembles a briefing message, sends it + the user event to
# the harnessed endpoint, and records the response.
# ===============================================================================

def _assemble_briefing_message_for_harnessed_run(
  agent_config: Dict[str, Any],
  session_id: str,
  user_message: str,
) -> str:
  """Build the <agent-briefing> message for a harnessed model run (spec §3.10).

  Includes: persona, working context, safety constraints, archival memories,
  recent session summary, event info, and agent directory.
  """
  agent_id = agent_config.get("agent_id", "")
  display_name = agent_config.get("display_name", agent_id)
  system_prompt_text = agent_config.get("system_prompt", "You are a helpful assistant.")
  working_context = agent_config.get("working_context", "")

  read_tools_str = agent_config.get("read_tools_allowed", '["*"]')
  write_tools_str = agent_config.get("write_tools_allowed", '[]')
  safety_constraints = f"Read tools: {read_tools_str}. Write tools: {write_tools_str}."

  archival_memory_text = "(No archival memories yet)"
  try:
    memory_result = _call_sqlite(
      "SELECT content, memory_type FROM archival_memory WHERE agent_id = :agent_id ORDER BY importance DESC LIMIT 10",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": agent_id},
    )
    memory_rows = _parse_rows_from_mcp_query_response(memory_result)
    if memory_rows:
      archival_memory_text = "\n".join(
        f"- [{r.get('memory_type', '?')}] {r.get('content', '')[:200]}" for r in memory_rows
      )
  except Exception:
    pass

  agent_directory_text = _build_agent_directory_for_system_prompt(agent_id) or "(No other agents)"

  briefing = (
    f"<agent-briefing>\n"
    f"You are acting as \"{display_name}\" — an always-on agent in the Aura Friday MCP-Link system.\n"
    f"Your role and personality:\n\n"
    f"{system_prompt_text}\n\n"
    f"Current working context (your short-term memory):\n"
    f"{working_context or '(empty)'}\n\n"
    f"Safety constraints for this agent:\n"
    f"{safety_constraints}\n\n"
    f"Relevant memories from your long-term knowledge base:\n"
    f"{archival_memory_text}\n\n"
    f"You are responding to this event:\n"
    f"- Session: {session_id}\n\n"
    f"Other agents you can delegate to:\n"
    f"{agent_directory_text}\n\n"
    f"IMPORTANT: You have access to MCP tools including the \"agent\" tool. You can use it to:\n"
    f"- Update your own memory: agent tool → set_memory\n"
    f"- Update your working context: agent tool → update_agent (working_context field)\n"
    f"- Send messages to other agents: agent tool → send_message\n"
    f"- Search your memories: agent tool → get_memory\n"
    f"</agent-briefing>\n\n"
    f"{user_message}"
  )
  return briefing


def _execute_harnessed_agent_run(agent_id: str, message: str, session_id: str) -> Dict[str, Any]:
  """Execute a harnessed-mode agent run (spec §3.10).

  Instead of the full ReAct loop, this:
  1. Assembles a briefing message
  2. Sends briefing + event to the harnessed endpoint as a single LLM call
  3. Records the response in transcripts
  4. Creates run log entry

  The harnessed endpoint manages its own tool execution, context, and compaction.
  """
  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return create_error_response(f"Agent not found: '{agent_id}'")

  current_state = agent_config.get("current_state", "IDLE")
  if current_state != "IDLE":
    return create_error_response(f"Agent '{agent_id}' is not idle (current state: {current_state}). Cannot start a new run.")

  run_id = _generate_run_id()
  step_number = 0

  _create_run_log_entry_at_start(agent_id, run_id, "user_message")

  _append_session_log_entry(agent_id, run_id, "run_started", {
    "session_id": session_id,
    "context_mode": "harnessed",
    "message_preview": message[:200],
  })

  _save_transcript_entry(agent_id, session_id, "user", message)

  step_number += 1
  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="IDLE", to_state="ASSEMBLING_CONTEXT",
    checkpoint_snapshot={"run_id": run_id, "session_id": session_id, "context_mode": "harnessed"},
  )
  if not transition_ok:
    _complete_run_log_entry_at_finish(run_id, "failed", 0, 0, 0, error_message=transition_err)
    _append_session_log_entry(agent_id, run_id, "run_failed", {"reason": transition_err})
    return create_error_response(f"Failed to start harnessed run: {transition_err}")

  briefing_text = _assemble_briefing_message_for_harnessed_run(agent_config, session_id, message)

  _append_session_log_entry(agent_id, run_id, "context_assembled", {
    "context_mode": "harnessed",
    "briefing_length": len(briefing_text),
  })

  step_number += 1
  _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="ASSEMBLING_CONTEXT", to_state="WAITING_FOR_LLM",
    checkpoint_snapshot={"run_id": run_id, "briefing_sent": True},
  )

  harnessed_messages = [{"role": "user", "content": briefing_text}]

  _save_transcript_entry(agent_id, session_id, "system", f"[briefing sent to harnessed model, {len(briefing_text)} chars]")

  llm_endpoint = agent_config.get("llm_endpoint", "")
  llm_provider = agent_config.get("llm_provider", "")
  llm_model = agent_config.get("llm_model", "")

  llm_params: Dict[str, Any] = {
    "operation": "chat",
    "messages": harnessed_messages,
    "temperature": 0.7,
    "max_tokens": 4096,
    "tool_unlock_token": "__auto__",
  }

  if llm_endpoint:
    llm_params["endpoint"] = llm_endpoint
  if llm_provider:
    llm_params["provider"] = llm_provider
  if llm_model:
    llm_params["model"] = llm_model
  _apply_provider_host_params_for_llm_call(llm_provider, llm_params, llm_endpoint)

  _append_session_log_entry(agent_id, run_id, "llm_called", {
    "provider": llm_params.get("provider"),
    "model": llm_params.get("model"),
    "context_mode": "harnessed",
  })

  llm_result = _call_llm_with_fallback_chain(agent_id, run_id, agent_config, llm_params)

  if llm_result.get("isError"):
    error_text = _extract_text_from_mcp_response(llm_result)[:500]
    _complete_run_log_entry_at_finish(run_id, "failed", 1, 0, 0, error_message=f"Harnessed LLM call failed: {error_text[:200]}")
    step_number += 1
    _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM", f"Harnessed LLM call failed: {error_text[:200]}")
    return create_error_response(f"Harnessed LLM call failed: {error_text[:200]}")

  response_text = _extract_text_from_mcp_response(llm_result)

  try:
    response_data = json.loads(response_text)
  except json.JSONDecodeError:
    response_data = {"raw_text": response_text}

  tokens_consumed = _extract_token_count_from_llm_response(response_data)

  choices = response_data.get("choices", [])
  if choices:
    final_response = choices[0].get("message", {}).get("content", response_text)
  elif "content" in response_data:
    final_response = response_data.get("content", response_text)
  else:
    final_response = response_data.get("raw_text", response_text)

  _append_session_log_entry(agent_id, run_id, "llm_response", {
    "context_mode": "harnessed",
    "content_preview": (final_response or "")[:200],
  })

  _save_transcript_entry(agent_id, session_id, "assistant", final_response or "(no response)")

  step_number += 1
  _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="WAITING_FOR_LLM", to_state="COMPLETED",
    checkpoint_snapshot={"run_id": run_id, "harnessed_response_preview": (final_response or "")[:500]},
  )

  _append_session_log_entry(agent_id, run_id, "run_completed", {
    "context_mode": "harnessed",
    "tokens_consumed": tokens_consumed,
  })

  _complete_run_log_entry_at_finish(run_id, "completed", 1, 0, tokens_consumed)

  step_number += 1
  _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="COMPLETED", to_state="IDLE",
    checkpoint_snapshot={"run_id": run_id, "final_state": "IDLE"},
  )

  _reflection_idle_tracker[agent_id] = _iso_now()

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "completed",
      "agent_id": agent_id,
      "run_id": run_id,
      "session_id": session_id,
      "context_mode": "harnessed",
      "response": final_response,
    }, indent=2)}],
    "isError": False,
  }


def _execute_agent_run(agent_id: str, message: str, session_id: str, image_data_uri_list: Optional[List[str]] = None) -> Dict[str, Any]:
  """Execute a full agent ReAct loop for a single user message.

  This is the core execution engine. It:
  1. Validates the agent exists and is IDLE.
  2. Creates a run_id, logs run_started.
  3. Transitions through the state machine with checkpoints at every step.
  4. Calls the LLM with caller_managed tool execution.
  5. If the LLM proposes tool calls, executes them with receipts, then loops.
  6. When the LLM returns a text-only response, saves it and completes.
  7. Enforces max_tool_rounds_per_run.
  8. On any unrecoverable error, transitions to FAILED.

  Returns:
    MCP-format response dict with the agent's final text response.
  """
  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return create_error_response(f"Agent not found: '{agent_id}'")

  current_state = agent_config.get("current_state", "IDLE")
  if current_state != "IDLE":
    return create_error_response(f"Agent '{agent_id}' is not idle (current state: {current_state}). Cannot start a new run.")

  run_id = _generate_run_id()
  max_tool_rounds = agent_config.get("max_tool_rounds_per_run", 10) or 10
  max_run_duration_seconds = agent_config.get("max_run_duration_seconds") or 0
  run_started_wall_clock_seconds = time.time()
  step_number = 0
  run_total_llm_calls = 0
  run_total_tool_calls = 0
  run_total_tokens_consumed = 0

  _clear_circuit_breaker_tracker_for_run(agent_id, run_id)
  _create_run_log_entry_at_start(agent_id, run_id, "user_message")

  _append_session_log_entry(agent_id, run_id, "run_started", {
    "session_id": session_id,
    "message_preview": message[:200],
  })

  _append_session_log_entry(agent_id, run_id, "message_received", {
    "session_id": session_id,
    "message": message,
  })

  _save_transcript_entry(agent_id, session_id, "user", message)

  # ── Transition: IDLE → ASSEMBLING_CONTEXT ──
  step_number += 1
  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="IDLE", to_state="ASSEMBLING_CONTEXT",
    checkpoint_snapshot={"run_id": run_id, "session_id": session_id, "step_number": step_number, "user_message": message},
  )
  if not transition_ok:
    _append_session_log_entry(agent_id, run_id, "run_failed", {"reason": transition_err})
    _complete_run_log_entry_at_finish(run_id, "failed", 0, 0, 0, error_message=transition_err)
    return create_error_response(f"Failed to start run: {transition_err}")

  # ── Assemble context (budget planner discovers model context window) ──
  assembled_messages, budget_metadata = _assemble_context_for_agent_run(
    agent_config, session_id, message,
    image_data_uri_list=image_data_uri_list or [],
  )

  _append_session_log_entry(agent_id, run_id, "context_assembled", budget_metadata)

  # ── Transition: ASSEMBLING_CONTEXT → WAITING_FOR_LLM ──
  step_number += 1
  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="ASSEMBLING_CONTEXT", to_state="WAITING_FOR_LLM",
    checkpoint_snapshot={
      "run_id": run_id, "session_id": session_id, "step_number": step_number,
      "assembled_messages_count": len(assembled_messages),
      "budget": budget_metadata,
    },
  )
  if not transition_ok:
    _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed, error_message=transition_err)
    _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "ASSEMBLING_CONTEXT", transition_err)
    return create_error_response(f"State transition failed: {transition_err}")

  # ── ReAct loop ──
  tool_round = 0
  final_response_text = None
  consecutive_emergency_compact_count_in_this_run = 0

  while tool_round < max_tool_rounds:
    # ── Enforce wall-clock run deadline (max_run_duration_seconds; <=0 = unlimited) ──
    run_elapsed_wall_clock_seconds = time.time() - run_started_wall_clock_seconds
    if max_run_duration_seconds > 0 and run_elapsed_wall_clock_seconds > max_run_duration_seconds:
      duration_error_message = f"Run exceeded max_run_duration_seconds ({int(run_elapsed_wall_clock_seconds)}s > {max_run_duration_seconds}s)"
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed,
        error_message=duration_error_message)
      step_number += 1
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM", duration_error_message)
      return create_error_response(duration_error_message)

    # ── Enforce per-hour LLM call rate limit ──
    llm_rate_ok, llm_rate_reason = _check_llm_call_rate_limit_for_agent(agent_id, agent_config)
    if not llm_rate_ok:
      _append_session_log_entry(agent_id, run_id, "rate_limit_hit", {"detail": llm_rate_reason, "check": "llm_calls_per_hour"})
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed,
        error_message=llm_rate_reason)
      step_number += 1
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM", llm_rate_reason)
      return create_error_response(llm_rate_reason)

    # ── Check daily token budget before each LLM call ──
    budget_within, budget_used_today, budget_daily_limit = _check_daily_token_budget_for_agent(agent_id, agent_config)
    if not budget_within:
      _auto_pause_agent_for_budget_exceeded(agent_id, budget_used_today, budget_daily_limit, run_id)
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed,
        error_message=f"Daily token budget exceeded: {budget_used_today}/{budget_daily_limit}")
      step_number += 1
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM",
        f"Daily token budget exceeded ({budget_used_today}/{budget_daily_limit} tokens)")
      return create_error_response(f"Agent paused: daily token budget exceeded ({budget_used_today}/{budget_daily_limit})")

    # ── Call LLM ──
    llm_endpoint = agent_config.get("llm_endpoint", "")
    llm_provider = agent_config.get("llm_provider", "")
    llm_model = agent_config.get("llm_model", "")

    llm_params: Dict[str, Any] = {
      "operation": "chat",
      "messages": assembled_messages,
      "tool_execution": "caller_managed",
      "temperature": 0.7,
      "max_tokens": 4096,
      "repetition_penalty": 1.1,
      "stop": ["<|im_end|>", "<|im_start|>"],
      "enable_thinking": False,
      "tool_unlock_token": "__auto__",
    }

    if llm_endpoint:
      llm_params["endpoint"] = llm_endpoint
    if llm_provider:
      llm_params["provider"] = llm_provider
    if llm_model:
      llm_params["model"] = llm_model
    _apply_provider_host_params_for_llm_call(llm_provider, llm_params, llm_endpoint)

    endpoint_has_tool_calling = True
    if llm_endpoint:
      tc_ok, _ = _check_endpoint_has_required_capabilities(llm_endpoint, {"tool_calling"})
      endpoint_has_tool_calling = tc_ok

    if endpoint_has_tool_calling:
      tool_definitions = _build_tool_definitions_for_agent(agent_config)
      if tool_definitions:
        llm_params["tools"] = tool_definitions
    else:
      tool_definitions = []

    _append_session_log_entry(agent_id, run_id, "llm_called", {
      "provider": llm_params.get("provider"),
      "model": llm_params.get("model"),
      "message_count": len(assembled_messages),
      "tool_round": tool_round,
      "tool_definitions_included": len(tool_definitions) if tool_definitions else 0,
    })

    llm_result = _call_llm_with_fallback_chain(agent_id, run_id, agent_config, llm_params)

    if llm_result.get("isError"):
      error_text = _extract_text_from_mcp_response(llm_result)[:500]
      _append_session_log_entry(agent_id, run_id, "llm_error", {"error": error_text, "tool_round": tool_round})

      if _detect_context_too_long_error(error_text):
        assembled_messages, consecutive_emergency_compact_count_in_this_run, circuit_breaker_tripped = _emergency_compact_escalation(
          agent_id, session_id, run_id, agent_config,
          assembled_messages, consecutive_emergency_compact_count_in_this_run,
        )
        if circuit_breaker_tripped:
          step_number += 1
          _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed,
            error_message=f"Emergency compact circuit breaker after {consecutive_emergency_compact_count_in_this_run} attempts")
          _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM",
            f"Emergency compact circuit breaker tripped after {consecutive_emergency_compact_count_in_this_run} consecutive attempts")
          return create_error_response(f"Context too long: circuit breaker tripped after {consecutive_emergency_compact_count_in_this_run} emergency compactions")
        llm_params["messages"] = assembled_messages
        continue

      step_number += 1
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed,
        error_message=f"LLM call failed: {error_text[:200]}")
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM", f"LLM call failed: {error_text[:200]}")
      return create_error_response(f"LLM call failed: {error_text[:200]}")

    llm_response_text = _extract_text_from_mcp_response(llm_result)

    try:
      llm_response_data = json.loads(llm_response_text)
    except json.JSONDecodeError:
      llm_response_data = {"raw_text": llm_response_text}

    run_total_llm_calls += 1
    llm_call_token_count = _extract_token_count_from_llm_response(llm_response_data)
    run_total_tokens_consumed += llm_call_token_count

    choices = llm_response_data.get("choices", [])
    tool_calls = None
    assistant_content = None

    if choices:
      first_choice_message = choices[0].get("message", {})
      assistant_content = first_choice_message.get("content", "")
      tool_calls = first_choice_message.get("tool_calls")
    elif "content" in llm_response_data:
      assistant_content = llm_response_data.get("content", "")
    elif "raw_text" in llm_response_data:
      assistant_content = llm_response_data.get("raw_text", "")

    _append_session_log_entry(agent_id, run_id, "llm_response", {
      "has_tool_calls": tool_calls is not None and len(tool_calls) > 0 if tool_calls else False,
      "content_preview": (assistant_content or "")[:200],
      "tool_round": tool_round,
    })

    # ── If no tool calls, we're done ──
    if not tool_calls:
      final_response_text = assistant_content or "(no response from model)"
      break

    # ── Process tool calls ──
    step_number += 1
    transition_ok, transition_err = _transition_agent_state(
      agent_id, run_id, session_id, step_number,
      from_state="WAITING_FOR_LLM", to_state="EXECUTING_TOOL",
      checkpoint_snapshot={
        "run_id": run_id, "session_id": session_id, "step_number": step_number,
        "tool_round": tool_round, "pending_tool_calls": len(tool_calls),
      },
    )
    if not transition_ok:
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed, error_message=transition_err)
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "WAITING_FOR_LLM", transition_err)
      return create_error_response(f"State transition failed during tool execution: {transition_err}")

    # Append the full assistant turn (including its tool_calls, content may be
    # empty) BEFORE the role:"tool" results, so strict endpoints accept round 2+.
    assistant_turn_message: Dict[str, Any] = {"role": "assistant", "content": assistant_content or ""}
    if tool_calls:
      assistant_turn_message["tool_calls"] = tool_calls
    assembled_messages.append(assistant_turn_message)

    tool_results_for_llm = []

    for tc in tool_calls:
      run_total_tool_calls += 1
      tc_id = tc.get("id", f"call_{hashlib.md5(str(tc).encode()).hexdigest()[:8]}")
      tc_function = tc.get("function", {})
      tc_tool_name = tc_function.get("name", "unknown")
      tc_arguments_str = tc_function.get("arguments", "{}")

      try:
        tc_arguments = json.loads(tc_arguments_str) if isinstance(tc_arguments_str, str) else tc_arguments_str
      except json.JSONDecodeError:
        tc_arguments = {"raw": tc_arguments_str}

      execution_id = _compute_execution_receipt_id(run_id, step_number, tc_tool_name, tc_arguments)

      _append_session_log_entry(agent_id, run_id, "tool_proposed", {
        "tool_name": tc_tool_name,
        "execution_id": execution_id,
        "arguments_preview": str(tc_arguments)[:200],
        "is_pseudo_tool": tc_tool_name in PSEUDO_TOOL_NAMES,
      })

      if tc_tool_name in PSEUDO_TOOL_NAMES:
        tool_start_time = time.time()
        pseudo_ok, pseudo_result_text = _dispatch_pseudo_tool_call(agent_id, tc_tool_name, tc_arguments, run_id, session_id=session_id, step_number=step_number, agent_config=agent_config)
        tool_duration_ms = int((time.time() - tool_start_time) * 1000)
        tool_result_text = pseudo_result_text

        _append_session_log_entry(agent_id, run_id, "tool_executed", {
          "tool_name": tc_tool_name,
          "execution_id": execution_id,
          "result_preview": pseudo_result_text[:200],
          "duration_ms": tool_duration_ms,
          "pseudo_tool": True,
        })
      elif (cached_result := _get_existing_execution_receipt(execution_id)) is not None:
        _append_session_log_entry(agent_id, run_id, "tool_skipped_receipt", {
          "tool_name": tc_tool_name,
          "execution_id": execution_id,
        })
        tool_result_text = json.dumps(cached_result, default=str)
      else:
        policy_allowed, policy_reason, policy_needs_approval = _execute_policy_guard_check(
          agent_id, tc_tool_name, tc_arguments, agent_config, run_id,
        )

        if not policy_allowed and policy_needs_approval:
          approval_granted, approval_denial_reason = _request_approval_for_tool_call(
            agent_id, run_id, session_id, step_number,
            tc_tool_name, tc_arguments, agent_config,
          )
          if approval_granted:
            policy_allowed = True
          else:
            tool_result_text = f"Tool '{tc_tool_name}' was not approved: {approval_denial_reason}"
            tool_results_for_llm.append({
              "role": "tool",
              "tool_call_id": tc_id,
              "content": tool_result_text,
            })
            continue

        if not policy_allowed:
          tool_result_text = f"Policy Guard rejected tool '{tc_tool_name}': {policy_reason}"
          tool_results_for_llm.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": tool_result_text,
          })
          continue

        _create_pending_execution_receipt(execution_id, run_id, tc_tool_name)

        suffixed_tc_tool_name = _suffixed_tool_name(tc_tool_name)
        tool_call_params = {"input": tc_arguments}
        if "tool_unlock_token" not in tc_arguments:
          tool_call_params["input"]["tool_unlock_token"] = "__auto__"

        tool_start_time = time.time()
        tool_result = _call_tool(suffixed_tc_tool_name, tool_call_params)
        tool_duration_ms = int((time.time() - tool_start_time) * 1000)

        tool_result_text = _extract_text_from_mcp_response(tool_result)
        tool_is_error = tool_result.get("isError", False)

        _complete_execution_receipt(execution_id, {"text": tool_result_text[:10000], "isError": tool_is_error})

        if tool_is_error:
          _record_tool_failure_for_circuit_breaker(agent_id, run_id, tc_tool_name)
          _append_session_log_entry(agent_id, run_id, "tool_failed", {
            "tool_name": tc_tool_name,
            "execution_id": execution_id,
            "error_preview": tool_result_text[:300],
            "duration_ms": tool_duration_ms,
          })
        else:
          _record_tool_success_for_circuit_breaker(agent_id, run_id, tc_tool_name)
          _append_session_log_entry(agent_id, run_id, "tool_executed", {
            "tool_name": tc_tool_name,
            "execution_id": execution_id,
            "result_preview": tool_result_text[:200],
            "duration_ms": tool_duration_ms,
          })

      spillover_processed_result_text = _spillover_tool_result_if_oversized(
        agent_id, session_id, tc_tool_name, tool_result_text, execution_id,
      )
      tool_results_for_llm.append({
        "role": "tool",
        "tool_call_id": tc_id,
        "content": spillover_processed_result_text,
      })

    assembled_messages.extend(tool_results_for_llm)

    # ── Transition back: EXECUTING_TOOL → WAITING_FOR_LLM ──
    step_number += 1
    transition_ok, transition_err = _transition_agent_state(
      agent_id, run_id, session_id, step_number,
      from_state="EXECUTING_TOOL", to_state="WAITING_FOR_LLM",
      checkpoint_snapshot={
        "run_id": run_id, "session_id": session_id, "step_number": step_number,
        "tool_round": tool_round, "tools_completed": len(tool_calls),
      },
    )
    if not transition_ok:
      _complete_run_log_entry_at_finish(run_id, "failed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed, error_message=transition_err)
      _force_agent_to_failed_state(agent_id, run_id, session_id, step_number, "EXECUTING_TOOL", transition_err)
      return create_error_response(f"State transition failed after tool execution: {transition_err}")

    tool_round += 1

  # ── If we hit max_tool_rounds without a final response ──
  if final_response_text is None:
    final_response_text = f"(Agent reached maximum of {max_tool_rounds} tool rounds without producing a final response)"
    _append_session_log_entry(agent_id, run_id, "error", {
      "reason": "max_tool_rounds_exceeded",
      "max_tool_rounds": max_tool_rounds,
    })

  # ── Save assistant response to transcript ──
  _save_transcript_entry(agent_id, session_id, "assistant", final_response_text)

  # ── Transition: WAITING_FOR_LLM → COMPLETED ──
  step_number += 1
  transition_ok, transition_err = _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="WAITING_FOR_LLM", to_state="COMPLETED",
    checkpoint_snapshot={
      "run_id": run_id, "session_id": session_id, "step_number": step_number,
      "final_response_preview": final_response_text[:500],
    },
  )

  # ── Log run completed ──
  _append_session_log_entry(agent_id, run_id, "message_sent", {
    "session_id": session_id,
    "response_preview": final_response_text[:200],
  })
  _append_session_log_entry(agent_id, run_id, "run_completed", {
    "tool_rounds": tool_round,
    "total_steps": step_number,
  })

  # ── Finalize run log entry with actual counters ──
  _complete_run_log_entry_at_finish(run_id, "completed", run_total_llm_calls, run_total_tool_calls, run_total_tokens_consumed)

  # ── Transition: COMPLETED → IDLE ──
  step_number += 1
  _transition_agent_state(
    agent_id, run_id, session_id, step_number,
    from_state="COMPLETED", to_state="IDLE",
    checkpoint_snapshot={"run_id": run_id, "session_id": session_id, "step_number": step_number, "final_state": "IDLE"},
  )

  _reflection_idle_tracker[agent_id] = _iso_now()

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "completed",
      "agent_id": agent_id,
      "run_id": run_id,
      "session_id": session_id,
      "response": final_response_text,
      "tool_rounds": tool_round,
    }, indent=2)}],
    "isError": False,
  }


def _force_agent_to_failed_state(agent_id: str, run_id: str, session_id: str, step_number: int, current_state: str, error_reason: str):
  """Force-transition an agent to FAILED state, logging the error.

  Used when an unrecoverable error occurs and we need to clean up the agent's state.
  Attempts the standard transition first; if that fails (e.g., state already changed),
  forces a direct DB update as a last resort.
  """
  _append_session_log_entry(agent_id, run_id, "run_failed", {"reason": error_reason, "from_state": current_state})

  is_valid, _ = validate_agent_state_transition(current_state, "FAILED")
  if is_valid:
    _transition_agent_state(
      agent_id, run_id, session_id, step_number + 1,
      from_state=current_state, to_state="FAILED",
      checkpoint_snapshot={"run_id": run_id, "error": error_reason},
    )
  else:
    _call_sqlite(
      "UPDATE agents SET current_state = 'FAILED', updated_at = :updated_at WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": agent_id, "updated_at": _iso_now()},
    )

  # Immediately transition FAILED → IDLE so agent is usable again
  _call_sqlite(
    "UPDATE agents SET current_state = 'IDLE', updated_at = :updated_at WHERE agent_id = :agent_id AND current_state = 'FAILED'",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "updated_at": _iso_now()},
  )
  _append_session_log_entry(agent_id, run_id, "state_transition", {"from_state": "FAILED", "to_state": "IDLE", "auto_recovery": True})


# ===============================================================================
# Crash Recovery (spec §3.3 — resume unfinished runs on startup)
# ===============================================================================

def _recover_agents_in_non_terminal_states() -> Dict[str, Any]:
  """Scan for agents stuck in non-terminal states and reset them to IDLE.

  Called on schema init or server startup. Agents found in ASSEMBLING_CONTEXT,
  WAITING_FOR_LLM, EXECUTING_TOOL, WAITING_FOR_APPROVAL, or WAITING_FOR_USER
  are presumed to have crashed mid-run (waiter threads do not survive restarts).

  Also requeues events left stuck in 'processing' (their worker died with them)
  back to 'pending' so they are not lost. Note: a requeued event replays under
  a NEW run_id, and execution receipts are keyed on run_id, so receipts dedupe
  tool calls only within a single live run - a tool call that completed just
  before the crash may execute again on replay.

  Persisted approval requests still 'pending' from the dead process are marked
  'orphaned' so operators can see them via get_pending_approvals and resolve
  them (the original waiter thread no longer exists).

  Phase 1: resets to IDLE with a log entry. Full checkpoint-based resume is
  deferred to a later iteration once the ReAct loop is battle-tested.

  Returns:
    Dict with recovery summary (agents found, actions taken).
  """
  non_terminal_states = ("ASSEMBLING_CONTEXT", "WAITING_FOR_LLM", "EXECUTING_TOOL", "COMPACTING", "REFLECTING", "WAITING_FOR_APPROVAL", "WAITING_FOR_USER")
  placeholders = ", ".join(f"'{s}'" for s in non_terminal_states)

  result = _call_sqlite(
    f"SELECT agent_id, current_state FROM agents WHERE current_state IN ({placeholders})",
    database=AGENT_KERNEL_DATABASE_NAME,
  )

  if result.get("isError"):
    return {"error": _extract_text_from_mcp_response(result)[:300], "agents_recovered": 0}

  response_text = _extract_text_from_mcp_response(result)
  recovered_agents = []

  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    for row in rows:
      stuck_agent_id = row.get("agent_id")
      stuck_state = row.get("current_state")
      if stuck_agent_id and stuck_state:
        recovery_run_id = f"recovery-{hashlib.md5(f'{stuck_agent_id}{time.time()}'.encode()).hexdigest()[:8]}"

        _call_sqlite(
          "UPDATE agents SET current_state = 'IDLE', updated_at = :updated_at WHERE agent_id = :agent_id",
          database=AGENT_KERNEL_DATABASE_NAME,
          bindings={"agent_id": stuck_agent_id, "updated_at": _iso_now()},
        )

        _append_session_log_entry(stuck_agent_id, recovery_run_id, "run_resumed", {
          "recovery_action": "reset_to_idle",
          "stuck_state": stuck_state,
          "reason": "crash_recovery_on_startup",
        })

        recovered_agents.append({"agent_id": stuck_agent_id, "was_in_state": stuck_state})
        MCPLogger.log(TOOL_LOG_NAME, f"Crash recovery: agent {stuck_agent_id} reset from {stuck_state} to IDLE")
  except (json.JSONDecodeError, KeyError, TypeError):
    pass

  requeued_stuck_processing_event_count = 0
  requeue_result = _call_sqlite(
    "UPDATE event_queue SET status = 'pending', processed_at = NULL WHERE status = 'processing'",
    database=AGENT_KERNEL_DATABASE_NAME,
  )
  if not requeue_result.get("isError"):
    requeue_text = _extract_text_from_mcp_response(requeue_result)
    try:
      requeue_data = json.loads(requeue_text)
      requeued_stuck_processing_event_count = int(requeue_data.get("rows_modified_by_operation", 0) or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
      pass
  if requeued_stuck_processing_event_count > 0:
    MCPLogger.log(TOOL_LOG_NAME, f"Crash recovery: requeued {requeued_stuck_processing_event_count} event(s) stuck in 'processing' back to 'pending'")

  # Approvals persisted as 'pending' by the dead process have no waiter thread
  # anymore: mark them 'orphaned' so get_pending_approvals can surface them and
  # approve_action / deny_action can resolve them instead of erroring.
  orphaned_pending_approval_count = 0
  orphan_result = _call_sqlite(
    "UPDATE approval_requests SET status = 'orphaned' WHERE status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME,
  )
  if not orphan_result.get("isError"):
    try:
      orphan_data = json.loads(_extract_text_from_mcp_response(orphan_result))
      orphaned_pending_approval_count = int(orphan_data.get("rows_modified_by_operation", 0) or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
      pass
  if orphaned_pending_approval_count > 0:
    MCPLogger.log(TOOL_LOG_NAME, f"Crash recovery: marked {orphaned_pending_approval_count} pending approval(s) from the previous process as 'orphaned'")

  return {
    "agents_recovered": len(recovered_agents),
    "agents": recovered_agents,
    "stuck_processing_events_requeued": requeued_stuck_processing_event_count,
    "orphaned_pending_approvals": orphaned_pending_approval_count,
  }


# ===============================================================================
# Phase 2: Durable Event Queue Helpers (spec §3.2, §7)
#
# Low-level primitives for the durable event queue. Every event (user message,
# cron trigger, telegram message, etc.) passes through this queue before the
# actor mailbox processes it. Events persist in SQLite so they survive crashes.
# ===============================================================================

def _enqueue_event(
  agent_id: str,
  event_type: str,
  payload: Dict[str, Any],
  priority: str = 'normal',
  queue_mode: str = 'queue',
  source_id: Optional[str] = None,
  idempotency_key: Optional[str] = None,
) -> Tuple[bool, str, Optional[int]]:
  """Insert an event into the durable event queue with optional deduplication.

  Args:
    agent_id: Target agent for this event.
    event_type: E.g. 'user_message', 'cron_trigger', 'telegram_message'.
    payload: Event-specific data dict, serialized to JSON.
    priority: 'high', 'normal', or 'low' — affects dequeue ordering.
    queue_mode: 'preempt', 'collect', 'drop', or 'queue'.
    source_id: Optional reference to event_sources.source_id.
    idempotency_key: If provided and a row with the same key exists,
                     the INSERT is skipped via UNIQUE constraint (deduplication).

  Returns:
    (success, status_message, queue_id_or_none)
  """
  if priority not in _VALID_EVENT_PRIORITIES_SET:
    return False, f"Invalid priority '{priority}'", None
  if queue_mode not in _VALID_QUEUE_MODES_SET:
    return False, f"Invalid queue_mode '{queue_mode}'", None

  now = _iso_now()
  payload_json = json.dumps(payload) if isinstance(payload, dict) else str(payload)

  if idempotency_key is None:
    idempotency_key = hashlib.md5(
      f"{agent_id}:{event_type}:{payload_json}:{now}:{time.time()}".encode()
    ).hexdigest()

  insert_result = _call_sqlite(
    """INSERT INTO event_queue
    (agent_id, event_type, source_id, payload_json, priority, queue_mode,
     idempotency_key, status, created_at)
    VALUES (:agent_id, :event_type, :source_id, :payload_json, :priority,
            :queue_mode, :idempotency_key, 'pending', :created_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id,
      "event_type": event_type,
      "source_id": source_id,
      "payload_json": payload_json,
      "priority": priority,
      "queue_mode": queue_mode,
      "idempotency_key": idempotency_key,
      "created_at": now,
    }
  )

  if insert_result.get("isError"):
    error_detail = _extract_text_from_mcp_response(insert_result)[:300]
    if "UNIQUE" in error_detail.upper():
      return True, "duplicate_skipped_by_idempotency_key", None
    return False, f"enqueue failed: {error_detail}", None

  id_result = _call_sqlite(
    "SELECT queue_id FROM event_queue WHERE idempotency_key = :key",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"key": idempotency_key}
  )
  queue_id = None
  try:
    id_data = json.loads(_extract_text_from_mcp_response(id_result))
    rows = id_data.get("data_rows_from_result_set", [])
    if rows:
      queue_id = rows[0].get("queue_id")
  except (json.JSONDecodeError, KeyError, TypeError):
    pass

  MCPLogger.log(TOOL_LOG_NAME, f"Enqueued event: agent={agent_id}, type={event_type}, pri={priority}, qid={queue_id}")
  return True, "enqueued", queue_id


def _dequeue_next_event(agent_id: str) -> Optional[Dict[str, Any]]:
  """Atomically select and claim the highest-priority pending event.

  Priority ordering: HIGH (0) > NORMAL (1) > LOW (2), then FIFO by queue_id.
  Uses optimistic concurrency (UPDATE WHERE status='pending') so only one
  worker can claim an event even under concurrent access.

  Returns:
    Dict with event data (including parsed payload) if found, or None.
  """
  select_result = _call_sqlite(
    """SELECT queue_id, agent_id, event_type, source_id, payload_json,
            priority, queue_mode, idempotency_key, status, created_at
    FROM event_queue
    WHERE agent_id = :agent_id AND status = 'pending'
    ORDER BY
      CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 ELSE 1 END ASC,
      queue_id ASC
    LIMIT 1""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )

  if select_result.get("isError"):
    return None

  try:
    data = json.loads(_extract_text_from_mcp_response(select_result))
    rows = data.get("data_rows_from_result_set", [])
    if not rows:
      return None
    event_row = rows[0]
  except (json.JSONDecodeError, KeyError, TypeError):
    return None

  queue_id = event_row.get("queue_id")
  claim_result = _call_sqlite(
    "UPDATE event_queue SET status = 'processing', processed_at = :now WHERE queue_id = :queue_id AND status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"queue_id": queue_id, "now": _iso_now()}
  )

  claim_text = _extract_text_from_mcp_response(claim_result)
  if claim_result.get("isError") or '"rows_modified_by_operation": 0' in claim_text:
    return None

  try:
    event_row["payload"] = json.loads(event_row.get("payload_json", "{}"))
  except json.JSONDecodeError:
    event_row["payload"] = {}

  return event_row


def _complete_event(queue_id: int) -> Tuple[bool, str]:
  """Mark an event as completed after successful processing."""
  result = _call_sqlite(
    "UPDATE event_queue SET status = 'completed', processed_at = :now WHERE queue_id = :queue_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"queue_id": queue_id, "now": _iso_now()}
  )
  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:300]
  return True, ""


def _dead_letter_event(queue_id: int, error_description: str) -> Tuple[bool, str]:
  """Mark an event as dead-lettered and record it in the dead letter queue."""
  result = _call_sqlite(
    "UPDATE event_queue SET status = 'dead_lettered', processed_at = :now WHERE queue_id = :queue_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"queue_id": queue_id, "now": _iso_now()}
  )
  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:300]

  _call_sqlite(
    """INSERT INTO dead_letter_queue
    (agent_id, original_event_json, failure_reason, failure_category, created_at)
    SELECT agent_id, payload_json, :error, 'processing_failure', :now
    FROM event_queue WHERE queue_id = :queue_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"queue_id": queue_id, "error": error_description, "now": _iso_now()}
  )
  return True, ""


def _get_pending_event_count(agent_id: str) -> int:
  """Count pending events for an agent in the durable queue."""
  result = _call_sqlite(
    "SELECT COUNT(*) as pending_count FROM event_queue WHERE agent_id = :agent_id AND status = 'pending'",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id}
  )
  try:
    data = json.loads(_extract_text_from_mcp_response(result))
    rows = data.get("data_rows_from_result_set", [])
    if rows:
      return rows[0].get("pending_count", 0)
  except (json.JSONDecodeError, KeyError, TypeError):
    pass
  return 0


# ===============================================================================
# Phase 2: Actor Mailbox (spec §3.2 — per-agent concurrency control)
#
# Each active agent gets one mailbox with a dedicated daemon worker thread.
# The worker dequeues events from the SQLite event_queue (priority-ordered)
# and processes them serially via _execute_agent_run(). This prevents
# concurrent events from corrupting agent state.
#
# Queue modes (preempt/collect/drop/queue) are enforced here.
# The generation counter (in shared state) handles hot-reload thread lifecycle.
# ===============================================================================

class _AgentActorMailbox:
  """Per-agent actor with a priority-ordered mailbox and a serial worker thread."""

  def __init__(self, agent_id: str, generation_at_creation: int):
    self.agent_id = agent_id
    self._generation_at_creation = generation_at_creation
    self._condition = threading.Condition()
    self._stop_requested = threading.Event()
    self._worker_thread: Optional[threading.Thread] = None
    self._currently_processing_queue_id: Optional[int] = None

  @property
  def is_currently_processing_an_event(self) -> bool:
    return self._currently_processing_queue_id is not None

  def start_worker_thread(self):
    """Start the background worker (daemon thread, named for debugging)."""
    if self._worker_thread and self._worker_thread.is_alive():
      return
    self._worker_thread = threading.Thread(
      target=self._mailbox_worker_loop,
      name=f"agent_mailbox_{self.agent_id}",
      daemon=True,
    )
    self._worker_thread.start()
    MCPLogger.log(TOOL_LOG_NAME, f"Mailbox worker started: agent={self.agent_id}, gen={self._generation_at_creation}")

  def stop_worker_thread(self):
    """Signal the worker to stop and wait briefly for it to exit."""
    self._stop_requested.set()
    with self._condition:
      self._condition.notify_all()
    if self._worker_thread:
      self._worker_thread.join(timeout=5.0)

  def signal_new_event_available(self):
    """Wake the worker thread to check for new events in the SQLite queue."""
    with self._condition:
      self._condition.notify()

  def _mailbox_worker_loop(self):
    """Main worker loop: dequeue from SQLite, process serially, repeat."""
    shared = _get_phase2_shared_state()

    while not self._stop_requested.is_set():
      try:
        if shared['generation'] != self._generation_at_creation:
          MCPLogger.log(TOOL_LOG_NAME,
            f"Mailbox {self.agent_id} exiting: gen stale ({self._generation_at_creation} vs {shared['generation']})")
          return

        if self._check_if_agent_is_paused():
          with self._condition:
            self._condition.wait(timeout=5.0)
          continue

        event = _dequeue_next_event(self.agent_id)

        if event is None:
          with self._condition:
            self._condition.wait(timeout=10.0)
          continue

        self._process_single_event(event)
      except RuntimeError as server_executor_shutdown_error:
        if "shutdown" in str(server_executor_shutdown_error).lower():
          MCPLogger.log(TOOL_LOG_NAME,
            f"Mailbox {self.agent_id} exiting: server executor is shutting down")
          return
        raise

    MCPLogger.log(TOOL_LOG_NAME, f"Mailbox worker stopped: agent={self.agent_id}")

  def _check_if_agent_is_paused(self) -> bool:
    """Query the agents table for the is_paused flag."""
    result = _call_sqlite(
      "SELECT is_paused FROM agents WHERE agent_id = :agent_id",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": self.agent_id}
    )
    try:
      data = json.loads(_extract_text_from_mcp_response(result))
      rows = data.get("data_rows_from_result_set", [])
      if rows:
        return bool(rows[0].get("is_paused", 0))
    except (json.JSONDecodeError, KeyError, TypeError):
      pass
    return False

  def _process_single_event(self, event: Dict[str, Any]):
    """Process one event: apply queue mode, execute agent run, signal waiters."""
    queue_id = event.get("queue_id")
    queue_mode = event.get("queue_mode", "queue")
    idempotency_key = event.get("idempotency_key", "")

    self._currently_processing_queue_id = queue_id

    try:
      if queue_mode == "collect":
        event = self._coalesce_collect_mode_events(event)

      payload = event.get("payload", {})
      if isinstance(payload, str):
        try:
          payload = json.loads(payload)
        except json.JSONDecodeError:
          payload = {}

      event_type = event.get("event_type", "")

      if event_type == "reflection_trigger":
        MCPLogger.log(TOOL_LOG_NAME, f"Mailbox: processing reflection_trigger event {queue_id} for agent {self.agent_id}")
        run_id = _generate_run_id()
        session_id = f"reflection-{run_id}"
        response = _execute_reflection_cycle(self.agent_id, run_id, session_id)
        response = {"content": [{"type": "text", "text": json.dumps(response, indent=2)}], "isError": False}
        _complete_event(queue_id)
        self._signal_synchronous_waiter(idempotency_key, queue_id, response)
        return

      message = payload.get("message", "")
      session_id = payload.get("session_id", f"event-{queue_id}")

      source_metadata = payload.get("source_metadata")
      if source_metadata:
        _update_last_active_channel_from_event(self.agent_id, source_metadata)

      if not message:
        MCPLogger.log(TOOL_LOG_NAME, f"Mailbox: skipping event {queue_id} (no message in payload)")
        _complete_event(queue_id)
        return

      MCPLogger.log(TOOL_LOG_NAME, f"Mailbox: processing event {queue_id} for agent {self.agent_id}")
      agent_cfg = _extract_agent_config_as_dict(self.agent_id)
      agent_context_mode = (agent_cfg or {}).get("context_mode", "raw") if agent_cfg else "raw"
      if agent_context_mode == "harnessed":
        response = _execute_harnessed_agent_run(self.agent_id, message, session_id)
      else:
        image_data_uri_list_from_event = list(payload.get("image_data_uri_list", []) or [])
        # Deferred Telegram attachments: downloaded here on the mailbox worker
        # (size-capped), never on the Telegram poller thread.
        for pending_telegram_image_file_id in (payload.get("pending_telegram_image_file_id_list", []) or []):
          downloaded_image_data_uri = _download_telegram_file_as_base64_data_uri(pending_telegram_image_file_id)
          if downloaded_image_data_uri:
            image_data_uri_list_from_event.append(downloaded_image_data_uri)
        response = _execute_agent_run(self.agent_id, message, session_id, image_data_uri_list=image_data_uri_list_from_event)

      _complete_event(queue_id)

      synchronous_mcp_waiter_was_signaled = self._signal_synchronous_waiter(idempotency_key, queue_id, response)

      if not synchronous_mcp_waiter_was_signaled and source_metadata:
        response_text = _extract_text_from_mcp_response(response)
        if response_text and not response.get("isError"):
          try:
            parsed_response_envelope = json.loads(response_text)
            clean_reply_text_for_chat_channel = parsed_response_envelope.get("response", response_text)
          except (json.JSONDecodeError, AttributeError):
            clean_reply_text_for_chat_channel = response_text
          if clean_reply_text_for_chat_channel:
            _dispatch_message_to_operator_via_last_active_or_default_channel(
              self.agent_id, clean_reply_text_for_chat_channel, agent_cfg or {}
            )

    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"Mailbox: event {queue_id} failed with exception: {e}")
      _dead_letter_event(queue_id, str(e))
      error_response = create_error_response(f"Event processing failed: {e}")
      self._signal_synchronous_waiter(idempotency_key, queue_id, error_response)
    finally:
      self._currently_processing_queue_id = None

  def _signal_synchronous_waiter(self, idempotency_key: str, queue_id: int, response: Dict) -> bool:
    """If handle_send_message is waiting for this event's result, deliver it.
    Returns True if a synchronous MCP waiter was found and signaled, False otherwise."""
    shared = _get_phase2_shared_state()
    # Pop + publish under the sync lock so this cannot interleave with a
    # waiter registering or cleaning up the same key (C4 race).
    with shared['sync_response_lock']:
      sync_event = shared['sync_response_events'].pop(idempotency_key, None)
      response_key = idempotency_key
      if sync_event is None:
        response_key = str(queue_id)
        sync_event = shared['sync_response_events'].pop(response_key, None)
      if sync_event:
        shared['sync_response_data'][response_key] = response
        sync_event.set()
        return True
    return False

  def _coalesce_collect_mode_events(self, initial_event: Dict[str, Any]) -> Dict[str, Any]:
    """Gather pending collect-mode events for this agent and merge their messages."""
    result = _call_sqlite(
      """SELECT queue_id, payload_json FROM event_queue
      WHERE agent_id = :agent_id AND queue_mode = 'collect' AND status = 'pending'
      ORDER BY queue_id ASC""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": self.agent_id}
    )

    additional_messages = []
    try:
      data = json.loads(_extract_text_from_mcp_response(result))
      rows = data.get("data_rows_from_result_set", [])
      for row in rows:
        try:
          row_payload = json.loads(row.get("payload_json", "{}"))
          msg = row_payload.get("message", "")
          if msg:
            additional_messages.append(msg)
        except json.JSONDecodeError:
          pass
        _complete_event(row.get("queue_id"))
    except (json.JSONDecodeError, KeyError, TypeError):
      pass

    if additional_messages:
      payload = initial_event.get("payload", {})
      if isinstance(payload, str):
        try:
          payload = json.loads(payload)
        except json.JSONDecodeError:
          payload = {}
      original_message = payload.get("message", "")
      all_messages = [original_message] + additional_messages
      payload["message"] = "\n---\n".join(m for m in all_messages if m)
      payload["coalesced_message_count"] = len(all_messages)
      initial_event["payload"] = payload
      MCPLogger.log(TOOL_LOG_NAME,
        f"Collect mode: coalesced {len(all_messages)} messages for agent {self.agent_id}")

    return initial_event


def _get_or_create_mailbox_for_agent(agent_id: str) -> _AgentActorMailbox:
  """Get existing mailbox or create a new one with a worker thread."""
  shared = _get_phase2_shared_state()
  with shared['mailbox_lock']:
    existing_mailbox = shared['mailboxes'].get(agent_id)
    if existing_mailbox is not None and existing_mailbox._worker_thread and existing_mailbox._worker_thread.is_alive():
      return existing_mailbox

    new_mailbox = _AgentActorMailbox(agent_id, _CURRENT_MAILBOX_WORKER_GENERATION)
    shared['mailboxes'][agent_id] = new_mailbox
    new_mailbox.start_worker_thread()
    return new_mailbox


def _stop_mailbox_for_agent(agent_id: str):
  """Stop and remove the mailbox for a specific agent."""
  shared = _get_phase2_shared_state()
  with shared['mailbox_lock']:
    mailbox = shared['mailboxes'].pop(agent_id, None)
    if mailbox:
      mailbox.stop_worker_thread()


def _stop_all_agent_mailboxes():
  """Stop all active mailboxes (used during hot-reload cleanup and self-test)."""
  shared = _get_phase2_shared_state()
  with shared['mailbox_lock']:
    for agent_id, mailbox in list(shared['mailboxes'].items()):
      mailbox.stop_worker_thread()
    shared['mailboxes'].clear()
    with shared['sync_response_lock']:
      shared['sync_response_events'].clear()
      shared['sync_response_data'].clear()


# ===============================================================================
# Phase 2: Event Source Handlers (spec §3.1, §9)
# ===============================================================================

def handle_add_event_source(params: Dict) -> Dict:
  """Register a new event source (cron timer, Telegram callback, etc.) for an agent."""
  agent_id = params.get("agent_id")
  source_type = params.get("source_type")
  config = params.get("config")
  priority = params.get("priority", "normal")
  queue_mode = params.get("queue_mode", "queue")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  check = _call_sqlite(
    "SELECT agent_id FROM agents WHERE agent_id = :id",
    database=AGENT_KERNEL_DATABASE_NAME, bindings={"id": agent_id}
  )
  if agent_id not in _extract_text_from_mcp_response(check):
    return create_error_response(f"Agent not found: '{agent_id}'")

  if priority not in _VALID_EVENT_PRIORITIES_SET:
    return create_error_response(f"Invalid priority '{priority}'. Must be one of: {sorted(_VALID_EVENT_PRIORITIES_SET)}")
  if queue_mode not in _VALID_QUEUE_MODES_SET:
    return create_error_response(f"Invalid queue_mode '{queue_mode}'. Must be one of: {sorted(_VALID_QUEUE_MODES_SET)}")

  source_id = f"src-{hashlib.md5(f'{agent_id}:{source_type}:{time.time()}'.encode()).hexdigest()[:12]}"
  config_json = json.dumps(config) if isinstance(config, dict) else str(config)
  now = _iso_now()

  result = _call_sqlite(
    """INSERT INTO event_sources
    (source_id, agent_id, source_type, config, priority, queue_mode, is_enabled, created_at)
    VALUES (:source_id, :agent_id, :source_type, :config, :priority, :queue_mode, 1, :created_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "source_id": source_id, "agent_id": agent_id, "source_type": source_type,
      "config": config_json, "priority": priority, "queue_mode": queue_mode,
      "created_at": now,
    }
  )

  if result.get("isError"):
    return create_error_response(f"Failed to add event source: {_extract_text_from_mcp_response(result)[:300]}")

  if source_type in ("cron", "cron_oneshot"):
    _ensure_cron_scheduler_is_running()
  elif source_type == "telegram":
    _register_telegram_event_source_callback(
      source_id, agent_id,
      config if isinstance(config, dict) else json.loads(config_json)
    )

  MCPLogger.log(TOOL_LOG_NAME, f"Added event source {source_id} (type={source_type}) for agent {agent_id}")
  return {
    "content": [{"type": "text", "text": json.dumps({
      "source_id": source_id, "agent_id": agent_id, "source_type": source_type,
      "priority": priority, "queue_mode": queue_mode,
    }, indent=2)}],
    "isError": False
  }


def handle_remove_event_source(params: Dict) -> Dict:
  """Unregister an event source and stop any active timer/callback."""
  source_id = params.get("source_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  info_result = _call_sqlite(
    "SELECT source_id, source_type FROM event_sources WHERE source_id = :source_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"source_id": source_id}
  )
  source_type = None
  try:
    info_data = json.loads(_extract_text_from_mcp_response(info_result))
    rows = info_data.get("data_rows_from_result_set", [])
    if rows:
      source_type = rows[0].get("source_type")
  except (json.JSONDecodeError, KeyError, TypeError):
    pass

  result = _call_sqlite(
    "DELETE FROM event_sources WHERE source_id = :source_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"source_id": source_id}
  )

  if result.get("isError"):
    return create_error_response(f"Failed to remove event source: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  if '"rows_modified_by_operation": 0' in response_text:
    return create_error_response(f"Event source not found: '{source_id}'")

  if source_type == "telegram":
    _unregister_telegram_event_source_callback(source_id)

  MCPLogger.log(TOOL_LOG_NAME, f"Removed event source {source_id}")
  return {"content": [{"type": "text", "text": f"Event source '{source_id}' removed."}], "isError": False}


def handle_list_event_sources(params: Dict) -> Dict:
  """List event sources, optionally filtered by agent_id."""
  agent_id = params.get("agent_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  sql = "SELECT source_id, agent_id, source_type, config, priority, queue_mode, is_enabled, created_at FROM event_sources"
  bindings: Dict[str, Any] = {}

  if agent_id:
    sql += " WHERE agent_id = :agent_id"
    bindings["agent_id"] = agent_id

  sql += " ORDER BY created_at DESC"

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to list event sources: {_extract_text_from_mcp_response(result)[:300]}")

  return {"content": [{"type": "text", "text": _extract_text_from_mcp_response(result)}], "isError": False}


# ===============================================================================
# Phase 2: Agent Control Handlers (pause/resume/interrupt)
# ===============================================================================

def handle_pause_agent(params: Dict) -> Dict:
  """Pause an agent — events queue in SQLite but the worker thread skips processing."""
  agent_id = params.get("agent_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  result = _call_sqlite(
    "UPDATE agents SET is_paused = 1, updated_at = :now WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "now": _iso_now()}
  )

  if result.get("isError"):
    return create_error_response(f"Failed to pause agent: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  if '"rows_modified_by_operation": 0' in response_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  MCPLogger.log(TOOL_LOG_NAME, f"Agent {agent_id} paused")
  return {"content": [{"type": "text", "text": f"Agent '{agent_id}' paused. Events will queue but not process."}], "isError": False}


def handle_resume_agent(params: Dict) -> Dict:
  """Resume a paused agent and signal its mailbox to drain pending events."""
  agent_id = params.get("agent_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  result = _call_sqlite(
    "UPDATE agents SET is_paused = 0, updated_at = :now WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "now": _iso_now()}
  )

  if result.get("isError"):
    return create_error_response(f"Failed to resume agent: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  if '"rows_modified_by_operation": 0' in response_text:
    return create_error_response(f"Agent not found: '{agent_id}'")

  shared = _get_phase2_shared_state()
  with shared['mailbox_lock']:
    mailbox = shared['mailboxes'].get(agent_id)
    if mailbox:
      mailbox.signal_new_event_available()

  pending_count = _get_pending_event_count(agent_id)
  MCPLogger.log(TOOL_LOG_NAME, f"Agent {agent_id} resumed ({pending_count} pending events)")
  return {"content": [{"type": "text", "text": f"Agent '{agent_id}' resumed. {pending_count} pending events will be processed."}], "isError": False}


def handle_interrupt_agent(params: Dict) -> Dict:
  """Submit a high-priority preempt event to an agent's mailbox."""
  agent_id = params.get("agent_id")
  reason = params.get("reason", "manual interrupt")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  ok, msg, queue_id = _enqueue_event(
    agent_id=agent_id,
    event_type="interrupt",
    payload={"reason": reason, "message": f"[INTERRUPT] {reason}"},
    priority="high",
    queue_mode="preempt",
  )

  if not ok:
    return create_error_response(f"Failed to enqueue interrupt: {msg}")

  mailbox = _get_or_create_mailbox_for_agent(agent_id)
  mailbox.signal_new_event_available()

  MCPLogger.log(TOOL_LOG_NAME, f"Agent {agent_id} interrupted: {reason}")
  return {
    "content": [{"type": "text", "text": json.dumps({
      "interrupted": True, "agent_id": agent_id, "queue_id": queue_id, "reason": reason,
    }, indent=2)}],
    "isError": False
  }


# ── 3.8: MCP operation compact_context (spec §13 Phase 3) ──

def handle_compact_context(params: Dict) -> Dict:
  """Manually trigger context compaction for an agent's session.

  This allows an external caller (human or AI) to force context compaction
  outside of the automatic pipeline. It runs the full compaction sequence:
  microcompact → context collapse → auto compact (LLM summarization).

  Required params:
    agent_id: The agent whose context to compact.

  Optional params:
    session_id: Which session to compact. If omitted, uses the most recent session.

  Returns:
    A summary of the compaction result including token counts before/after.
  """
  agent_id = params.get("agent_id")
  session_id = params.get("session_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  agent_config = _extract_agent_config_as_dict(agent_id)
  if agent_config is None:
    return create_error_response(f"Agent not found: '{agent_id}'")

  if not session_id:
    session_result = _call_sqlite(
      """SELECT DISTINCT session_id FROM transcript_entries
      WHERE agent_id = :agent_id
      ORDER BY created_at DESC LIMIT 1""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"agent_id": agent_id},
    )
    if not session_result.get("isError"):
      try:
        response_text = _extract_text_from_mcp_response(session_result)
        response_data = json.loads(response_text)
        rows = response_data.get("data_rows_from_result_set", [])
        if rows:
          session_id = rows[0].get("session_id")
      except (json.JSONDecodeError, KeyError, TypeError):
        pass

    if not session_id:
      return create_error_response(f"No sessions found for agent '{agent_id}'")

  history_sql = """SELECT role, content FROM transcript_entries
    WHERE agent_id = :agent_id AND session_id = :session_id
      AND role IN ('user','assistant','tool')
    ORDER BY entry_id ASC"""
  history_result = _call_sqlite(
    history_sql,
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "session_id": session_id},
  )

  history_messages: List[Dict[str, str]] = []
  if not history_result.get("isError"):
    try:
      response_text = _extract_text_from_mcp_response(history_result)
      response_data = json.loads(response_text)
      rows = response_data.get("data_rows_from_result_set", [])
      for row in rows:
        history_messages.append({
          "role": row.get("role", "user"),
          "content": row.get("content", ""),
        })
    except (json.JSONDecodeError, KeyError, TypeError):
      pass

  if len(history_messages) < 2:
    return {
      "content": [{"type": "text", "text": json.dumps({
        "compacted": False,
        "reason": "too_few_messages",
        "message_count": len(history_messages),
        "agent_id": agent_id,
        "session_id": session_id,
      }, indent=2)}],
      "isError": False
    }

  tokens_before = sum(
    _estimate_token_count_from_characters(m.get("content", ""))
    for m in history_messages
  )

  compacted_messages, tokens_after, compaction_was_performed = _run_auto_compact(
    agent_id, session_id, agent_config,
    history_messages, tokens_before, 0,
  )

  return {
    "content": [{"type": "text", "text": json.dumps({
      "compacted": compaction_was_performed,
      "agent_id": agent_id,
      "session_id": session_id,
      "messages_before": len(history_messages),
      "messages_after": len(compacted_messages),
      "tokens_before": tokens_before,
      "tokens_after": tokens_after,
    }, indent=2)}],
    "isError": False
  }


# ===============================================================================
# Phase 4: Memory MCP Operations (spec §13 Phase 4)
#
# External memory management operations for operators/tools. These are
# separate from the pseudo-tools (which the LLM calls internally).
# ===============================================================================

def handle_get_memory(params: Dict) -> Dict:
  """Retrieve archival memories, optionally filtered by type or searched by query.

  Required params:
    agent_id: Which agent's memories to retrieve.

  Optional params:
    memory_id: Retrieve a specific memory by ID.
    memory_type: Filter by type (fact, preference, project_knowledge, etc.).
    query: Semantic search query (returns top N by similarity).
    limit: Max results (default 20).
  """
  agent_id = params.get("agent_id")
  memory_id = params.get("memory_id")
  memory_type = params.get("memory_type")
  query = params.get("query")
  limit = int(params.get("limit", 20))

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  if memory_id:
    ok, memory, err = _get_archival_memory(memory_id)
    if not ok:
      return create_error_response(f"Failed to get memory: {err}")
    if memory is None:
      return create_error_response(f"Memory not found: '{memory_id}'")
    return {"content": [{"type": "text", "text": json.dumps(memory, indent=2, default=str)}], "isError": False}

  if query:
    ok, results, err = _search_archival_memory(agent_id, query, limit=limit)
    if not ok:
      return create_error_response(f"Memory search failed: {err}")
    return {"content": [{"type": "text", "text": json.dumps({"memories": results, "count": len(results)}, indent=2, default=str)}], "isError": False}

  sql = "SELECT memory_id, agent_id, memory_type, content, importance_score, confidence_score, source_run_id, access_count, last_accessed_at, created_at, updated_at FROM memory_entries WHERE agent_id = :agent_id"
  bindings: Dict[str, Any] = {"agent_id": agent_id}

  if memory_type:
    sql += " AND memory_type = :memory_type"
    bindings["memory_type"] = memory_type

  sql += " ORDER BY created_at DESC LIMIT :limit"
  bindings["limit"] = limit

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to list memories: {_extract_text_from_mcp_response(result)[:300]}")

  response_text = _extract_text_from_mcp_response(result)
  try:
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
  except (json.JSONDecodeError, KeyError, TypeError):
    rows = []

  return {"content": [{"type": "text", "text": json.dumps({"memories": rows, "count": len(rows)}, indent=2, default=str)}], "isError": False}


def handle_set_memory(params: Dict) -> Dict:
  """Insert or update an archival memory entry (upsert by memory_id).

  Required params:
    agent_id: Which agent owns this memory.
    content: The text content to store.

  Optional params:
    memory_id: If provided, updates existing memory; otherwise creates new.
    memory_type: Category (default 'fact').
    importance_score: 0.0–1.0 (default 0.5).
    confidence_score: 0.0–1.0 (default 0.8).
    source_run_id: Provenance tracking.
  """
  agent_id = params.get("agent_id")
  memory_id = params.get("memory_id")
  content = params.get("content", "")
  memory_type = params.get("memory_type", "fact")
  importance_score = float(params.get("importance_score", 0.5))
  confidence_score = float(params.get("confidence_score", 0.8))
  source_run_id = params.get("source_run_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  if not content:
    return create_error_response("'content' is required for set_memory")

  if memory_id:
    ok, existing, _err = _get_archival_memory(memory_id)
    if ok and existing is not None:
      update_ok, update_err = _update_archival_memory(
        memory_id, content=content,
        importance_score=importance_score,
        confidence_score=confidence_score,
      )
      if not update_ok:
        return create_error_response(f"Failed to update memory: {update_err}")
      return {"content": [{"type": "text", "text": json.dumps({"memory_id": memory_id, "action": "updated"}, indent=2)}], "isError": False}

  ok, new_memory_id, err = _insert_archival_memory(
    agent_id, content, memory_type, importance_score, confidence_score, source_run_id,
  )
  if not ok:
    return create_error_response(f"Failed to insert memory: {err}")

  return {"content": [{"type": "text", "text": json.dumps({"memory_id": new_memory_id, "action": "created"}, indent=2)}], "isError": False}


def handle_delete_memory(params: Dict) -> Dict:
  """Delete an archival memory entry by its ID.

  Required params:
    memory_id: The memory to delete.
  """
  memory_id = params.get("memory_id")

  schema_ok, schema_msg = initialize_agent_kernel_database()
  if not schema_ok:
    return create_error_response(f"Schema init failed: {schema_msg}")

  if not memory_id:
    return create_error_response("'memory_id' is required for delete_memory")

  ok, err = _delete_archival_memory(memory_id)
  if not ok:
    return create_error_response(f"Failed to delete memory: {err}")

  return {"content": [{"type": "text", "text": json.dumps({"memory_id": memory_id, "action": "deleted"}, indent=2)}], "isError": False}


# ===============================================================================
# Phase 2: Cron Scheduler (spec §3.1)
#
# Background daemon thread that polls enabled cron event sources every 30s,
# evaluates cron expressions via croniter, and fires events when due.
# Idempotency keys prevent duplicate fires for the same schedule tick.
# ===============================================================================

DATABASE_MAINTENANCE_PASS_INTERVAL_SECONDS = 3600
DATABASE_MAINTENANCE_SESSION_LOG_MAX_ROWS_TO_KEEP = 20000
DATABASE_MAINTENANCE_CHECKPOINTS_MAX_ROWS_TO_KEEP = 5000
DATABASE_MAINTENANCE_FINISHED_EVENT_QUEUE_ROWS_MAX_AGE_DAYS = 7

_last_database_maintenance_pass_monotonic_seconds = 0.0


def _run_periodic_database_maintenance_pruning_pass_if_due() -> bool:
  """Prune unbounded-growth tables; runs at most once per interval.

  Piggy-backs on the cron scheduler thread. Deletes:
    - execution_receipts past their expires_at
    - session_log rows beyond the newest N (cap/rotate)
    - agent_checkpoints rows beyond the newest N (cap/rotate)
    - completed/dead_lettered event_queue rows older than the age cap
    - resolved approval_requests rows older than the age cap (orphaned rows
      are kept until an operator resolves them or the agent is deleted)

  Returns True when a pass actually ran, False when skipped (not due yet).
  """
  global _last_database_maintenance_pass_monotonic_seconds
  now_monotonic = time.monotonic()
  if (_last_database_maintenance_pass_monotonic_seconds > 0.0
      and now_monotonic - _last_database_maintenance_pass_monotonic_seconds < DATABASE_MAINTENANCE_PASS_INTERVAL_SECONDS):
    return False
  _last_database_maintenance_pass_monotonic_seconds = now_monotonic

  _call_sqlite(
    "DELETE FROM execution_receipts WHERE expires_at < :now",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"now": _iso_now()},
  )
  _call_sqlite(
    "DELETE FROM session_log WHERE entry_id <= COALESCE((SELECT MAX(entry_id) - :keep FROM session_log), 0)",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"keep": DATABASE_MAINTENANCE_SESSION_LOG_MAX_ROWS_TO_KEEP},
  )
  _call_sqlite(
    "DELETE FROM agent_checkpoints WHERE checkpoint_id <= COALESCE((SELECT MAX(checkpoint_id) - :keep FROM agent_checkpoints), 0)",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"keep": DATABASE_MAINTENANCE_CHECKPOINTS_MAX_ROWS_TO_KEEP},
  )
  from datetime import datetime, timezone, timedelta
  finished_event_age_cutoff_iso = (
    datetime.now(timezone.utc) - timedelta(days=DATABASE_MAINTENANCE_FINISHED_EVENT_QUEUE_ROWS_MAX_AGE_DAYS)
  ).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
  _call_sqlite(
    "DELETE FROM event_queue WHERE status IN ('completed', 'dead_lettered') AND created_at < :cutoff",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"cutoff": finished_event_age_cutoff_iso},
  )
  _call_sqlite(
    "DELETE FROM approval_requests WHERE status IN ('approved', 'denied', 'timeout') AND requested_at < :cutoff",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"cutoff": finished_event_age_cutoff_iso},
  )
  MCPLogger.log(TOOL_LOG_NAME, "Database maintenance pruning pass completed")
  return True


def _ensure_cron_scheduler_is_running():
  """Start the cron scheduler daemon thread if not already running."""
  shared = _get_phase2_shared_state()
  cron_thread = shared.get('cron_thread')
  if cron_thread and cron_thread.is_alive():
    return

  shared['cron_stop_event'] = threading.Event()
  new_cron_thread = threading.Thread(
    target=_cron_scheduler_worker_loop,
    name="agent_cron_scheduler",
    daemon=True,
  )
  shared['cron_thread'] = new_cron_thread
  new_cron_thread.start()
  MCPLogger.log(TOOL_LOG_NAME, "Cron scheduler started")


def _stop_cron_scheduler():
  """Stop the cron scheduler thread."""
  shared = _get_phase2_shared_state()
  stop_event = shared.get('cron_stop_event')
  if stop_event:
    stop_event.set()
  cron_thread = shared.get('cron_thread')
  if cron_thread:
    cron_thread.join(timeout=5.0)
    shared['cron_thread'] = None


def _cron_scheduler_worker_loop():
  """Background loop: check cron sources, fire events when schedule is due."""
  shared = _get_phase2_shared_state()
  stop_event = shared.get('cron_stop_event', threading.Event())
  generation_at_start = shared.get('generation', 0)
  croniter_class = None

  while not stop_event.is_set():
    if shared.get('generation', 0) != generation_at_start:
      MCPLogger.log(TOOL_LOG_NAME, "Cron scheduler exiting (generation stale)")
      return

    try:
      if croniter_class is None:
        try:
          from croniter import croniter as _croniter_cls
          croniter_class = _croniter_cls
        except ImportError:
          MCPLogger.log(TOOL_LOG_NAME, "croniter not installed — cron scheduling disabled. pip install croniter")
          stop_event.wait(60.0)
          continue

      result = _call_sqlite(
        "SELECT source_id, agent_id, source_type, config, priority, queue_mode FROM event_sources WHERE source_type IN ('cron', 'cron_oneshot') AND is_enabled = 1",
        database=AGENT_KERNEL_DATABASE_NAME
      )

      if not result.get("isError"):
        try:
          data = json.loads(_extract_text_from_mcp_response(result))
          rows = data.get("data_rows_from_result_set", [])

          from datetime import datetime, timezone
          now_utc = datetime.now(timezone.utc)

          for row in rows:
            source_id = row.get("source_id")
            agent_id = row.get("agent_id")
            source_type = row.get("source_type", "cron")
            priority = row.get("priority", "low")
            queue_mode = row.get("queue_mode", "drop")

            try:
              config = json.loads(row.get("config", "{}")) if isinstance(row.get("config"), str) else row.get("config", {})
            except json.JSONDecodeError:
              continue

            # "schedule" is the canonical key; "cron_expression" is the legacy
            # key older schedule_reminder rows used before they were unified.
            schedule_expression = config.get("schedule") or config.get("cron_expression")
            if not schedule_expression:
              continue

            try:
              cron_iterator = croniter_class(schedule_expression, now_utc)
              previous_fire_time = cron_iterator.get_prev(datetime)
              seconds_since_last_fire = (now_utc - previous_fire_time).total_seconds()

              if seconds_since_last_fire < 60:
                cron_message = config.get("message", f"Cron trigger: {schedule_expression}")
                fire_idempotency_key = hashlib.md5(
                  f"cron:{source_id}:{previous_fire_time.isoformat()}".encode()
                ).hexdigest()

                ok, status, _ = _enqueue_event(
                  agent_id=agent_id,
                  event_type="cron_trigger",
                  payload={"message": cron_message, "schedule": schedule_expression, "source_id": source_id},
                  priority=priority,
                  queue_mode=queue_mode,
                  source_id=source_id,
                  idempotency_key=fire_idempotency_key,
                )

                if ok and status == "enqueued":
                  if source_type == "cron_oneshot":
                    _call_sqlite(
                      "UPDATE event_sources SET is_enabled = 0 WHERE source_id = :source_id",
                      database=AGENT_KERNEL_DATABASE_NAME,
                      bindings={"source_id": source_id},
                    )
                    MCPLogger.log(TOOL_LOG_NAME, f"One-shot cron disabled after fire: source={source_id}")
                  mailbox = _get_or_create_mailbox_for_agent(agent_id)
                  mailbox.signal_new_event_available()
                  MCPLogger.log(TOOL_LOG_NAME, f"Cron fired: source={source_id}, agent={agent_id}")

            except Exception as cron_err:
              MCPLogger.log(TOOL_LOG_NAME, f"Cron eval failed for source {source_id}: {cron_err}")

        except (json.JSONDecodeError, KeyError, TypeError):
          pass

      try:
        _check_and_fire_reflection_triggers()
      except Exception as reflection_check_error:
        MCPLogger.log(TOOL_LOG_NAME, f"Reflection trigger check failed on scheduler tick: {reflection_check_error}")

      try:
        _run_periodic_database_maintenance_pruning_pass_if_due()
      except Exception as maintenance_error:
        MCPLogger.log(TOOL_LOG_NAME, f"Periodic maintenance pass failed: {maintenance_error}")

    except Exception as loop_err:
      MCPLogger.log(TOOL_LOG_NAME, f"Cron scheduler iteration error: {loop_err}")

    stop_event.wait(30.0)

  MCPLogger.log(TOOL_LOG_NAME, "Cron scheduler stopped")


# ===============================================================================
# Phase 2: Telegram Event Source (spec §3.1)
#
# Wires up social.py's register_message_event_callback / unregister_ APIs.
# Uses _call_tool to register (MCP-compliant inter-tool call). When social.py
# receives a Telegram message matching the filter, it invokes our callback
# which enqueues the message into the agent's event queue.
# ===============================================================================

_telegram_callback_id_registry: Dict[str, str] = {}

# Hard cap for inbound Telegram file downloads: a hostile chat must not be able
# to exhaust server memory (the bytes are base64-encoded into LLM context).
TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES = 10 * 1024 * 1024


def _download_telegram_file_as_base64_data_uri(file_id: str) -> Optional[str]:
  """Download a Telegram file by file_id and return it as a base64 data URI.

  Uses social_rog.get_file to obtain the download URL, then fetches the raw
  bytes via HTTP and encodes them. Returns a string like
  "data:image/jpeg;base64,/9j/4AAQ..." or None on any failure.

  Enforces TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES three ways: the
  metadata file_size, the Content-Length header, and a chunked read cap
  (headers can lie). Called from the mailbox worker thread at event-processing
  time, NOT from the Telegram poller thread.
  """
  import urllib.request
  try:
    get_file_result = _call_tool(_suffixed_tool_name("social"), {"input": {
      "operation": "get_file",
      "file_id": file_id,
      "tool_unlock_token": "__auto__",
    }})
    if get_file_result.get("isError"):
      MCPLogger.log(TOOL_LOG_NAME, f"Telegram get_file failed for {file_id}: {_extract_text_from_mcp_response(get_file_result)}")
      return None
    file_info = json.loads(_extract_text_from_mcp_response(get_file_result))
    download_url = file_info.get("download_url")
    if not download_url:
      MCPLogger.log(TOOL_LOG_NAME, f"Telegram get_file returned no download_url for {file_id}")
      return None
    reported_file_size_bytes = file_info.get("file_size")
    if isinstance(reported_file_size_bytes, (int, float)) and not isinstance(reported_file_size_bytes, bool):
      if reported_file_size_bytes > TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES:
        MCPLogger.log(TOOL_LOG_NAME,
          f"Telegram file {file_id} rejected: reported size {reported_file_size_bytes} bytes exceeds cap {TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES}")
        return None
    file_path_on_telegram = file_info.get("file_path", "")
    extension = file_path_on_telegram.rsplit(".", 1)[-1].lower() if "." in file_path_on_telegram else "jpg"
    mime_type_by_extension = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4", "pdf": "application/pdf"}
    mime_type = mime_type_by_extension.get(extension, "image/jpeg")
    with urllib.request.urlopen(download_url, timeout=30) as http_response:
      content_length_header_value = http_response.headers.get("Content-Length")
      if content_length_header_value:
        try:
          if int(content_length_header_value) > TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES:
            MCPLogger.log(TOOL_LOG_NAME,
              f"Telegram file {file_id} rejected: Content-Length {content_length_header_value} exceeds cap {TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES}")
            return None
        except (ValueError, TypeError):
          pass
      downloaded_chunks: List[bytes] = []
      total_downloaded_byte_count = 0
      while True:
        chunk = http_response.read(65536)
        if not chunk:
          break
        total_downloaded_byte_count += len(chunk)
        if total_downloaded_byte_count > TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES:
          MCPLogger.log(TOOL_LOG_NAME,
            f"Telegram file {file_id} rejected: stream exceeded cap {TELEGRAM_INBOUND_FILE_DOWNLOAD_MAX_SIZE_BYTES} bytes, download aborted")
          return None
        downloaded_chunks.append(chunk)
      raw_bytes = b"".join(downloaded_chunks)
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
  except Exception as download_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Telegram file download failed for {file_id}: {download_error}")
    return None


def _enqueue_inbound_telegram_messages_for_agent(
  agent_id: str,
  source_id: str,
  bot_token_hash: str,
  formatted_telegram_messages: List[Dict],
) -> None:
  """Callback invoked by social.py's background poller when Telegram messages arrive.

  Runs on the poller thread — must be fast.  Converts each message into
  a durable event queue entry and signals the agent's mailbox worker.
  Attached files are NOT downloaded here — their file_ids are queued and the
  mailbox worker downloads them at event-processing time (size-capped).

  Contact access control runs FIRST: unapproved/blocked/pending users are
  handled by the ACL and never reach the admin interpreter or the agent.

  Admin-mode intercept: for explicitly APPROVED contacts only, checks if the
  Telegram chat is in admin mode (or has sent an admin-entry command like
  "/admin").  When yes, the message is handled by the text-based admin menu
  and a response is sent back via social.py — the message is NOT enqueued to
  the agent.
  """
  for msg in formatted_telegram_messages:
    text = msg.get("text", "") or msg.get("photo_caption", "") or ""
    has_photo = msg.get("has_photo", False)
    has_document = msg.get("has_document", False)
    if not text and not has_photo and not has_document:
      continue

    chat_id = msg.get("chat_id")

    # Contact authorization check runs BEFORE any admin handling, so a
    # blocked or unknown Telegram user can never open the admin shell.
    from_user_id_for_access_check = msg.get("from_user_id")
    sending_user_contact_authorization_status = None
    if from_user_id_for_access_check is None:
      MCPLogger.log(TOOL_LOG_NAME,
        f"CONTACT WARNING: telegram message in chat_id={chat_id} for agent {agent_id} has no from_user_id. "
        f"Message text: {repr(text)}. Allowing (cannot check access without user ID).")
    if from_user_id_for_access_check is not None:
      approval_mode = _get_agent_contact_approval_mode(agent_id)
      sending_user_contact_authorization_status = _check_if_transport_user_is_authorized_to_contact_agent(
        agent_id, "telegram", str(from_user_id_for_access_check)
      )
      if approval_mode == "require_approval":
        user_authorization_status = sending_user_contact_authorization_status
        contact_log_identity = f"user_id={from_user_id_for_access_check} username={msg.get('from_username', '?')} display_name={msg.get('from_display_name', '?')} chat_id={chat_id}"
        if user_authorization_status == "approved":
          pass
        elif user_authorization_status == "blocked":
          MCPLogger.log(TOOL_LOG_NAME,
            f"CONTACT BLOCKED: message dropped from {contact_log_identity} for agent {agent_id}. "
            f"Message text: {repr(text)}")
          continue
        elif user_authorization_status == "pending":
          MCPLogger.log(TOOL_LOG_NAME,
            f"CONTACT PENDING: repeat message from {contact_log_identity} for agent {agent_id}. "
            f"Message text: {repr(text)}")
          _send_admin_response_via_telegram(
            chat_id, bot_token_hash,
            "Your contact request is still pending approval. The operator has been notified."
          )
          continue
        else:
          MCPLogger.log(TOOL_LOG_NAME,
            f"CONTACT NEW: first contact from unknown {contact_log_identity} for agent {agent_id}. "
            f"Message text: {repr(text)}. Registering as pending and notifying operator.")
          _register_pending_contact_from_transport_user(
            agent_id=agent_id,
            transport_type="telegram",
            transport_user_id=str(from_user_id_for_access_check),
            display_name=msg.get("from_display_name", ""),
            username=msg.get("from_username", ""),
          )
          try:
            agent_config_result = _call_sqlite(
              "SELECT * FROM agents WHERE agent_id = :agent_id",
              database=AGENT_KERNEL_DATABASE_NAME,
              bindings={"agent_id": agent_id},
            )
            agent_config_for_notification = {}
            if not agent_config_result.get("isError"):
              try:
                agent_config_data = json.loads(_extract_text_from_mcp_response(agent_config_result))
                rows = agent_config_data.get("data_rows_from_result_set", [])
                if rows:
                  agent_config_for_notification = rows[0]
              except (json.JSONDecodeError, KeyError, IndexError):
                pass
            _notify_operator_of_pending_contact_request(
              agent_id=agent_id,
              agent_config=agent_config_for_notification,
              transport_type="telegram",
              display_name=msg.get("from_display_name", ""),
              username=msg.get("from_username", ""),
              transport_user_id=str(from_user_id_for_access_check),
            )
          except Exception as notify_error:
            MCPLogger.log(TOOL_LOG_NAME,
              f"Contact access: failed to notify operator for agent {agent_id}: {notify_error}")
          _send_admin_response_via_telegram(
            chat_id, bot_token_hash,
            "This bot is managed by an operator. Your contact request has been sent for approval. "
            "You will be able to chat once approved."
          )
          continue
      else:
        MCPLogger.log(TOOL_LOG_NAME,
          f"CONTACT AUTO-APPROVED: mode={approval_mode} for user_id={from_user_id_for_access_check} "
          f"username={msg.get('from_username', '?')} chat_id={chat_id} agent {agent_id}")

    # Admin intercept: only explicitly APPROVED contacts may enter or drive
    # the admin menu (auto_approve_all opens CHAT to everyone, but admin
    # still requires an approved contact-ACL row = the operator allowlist).
    admin_channel_key_for_this_chat = f"tg:{chat_id}" if chat_id is not None else None
    if admin_channel_key_for_this_chat and text and sending_user_contact_authorization_status == "approved":
      try:
        admin_result = _maybe_intercept_admin_message(
          channel_key=admin_channel_key_for_this_chat,
          raw_incoming_text=text,
          candidate_initial_active_agent_id=agent_id,
        )
      except Exception as admin_intercept_error:
        MCPLogger.log(TOOL_LOG_NAME,
          f"Admin intercept crashed for Telegram chat {chat_id}: {admin_intercept_error}")
        admin_result = None
      if admin_result is not None:
        _send_admin_response_via_telegram(chat_id, bot_token_hash, admin_result.get("response_text", ""))
        continue

    source_metadata = {
      "channel_type": "telegram",
      "chat_id": chat_id,
      "from_user_id": msg.get("from_user_id"),
      "from_username": msg.get("from_username", ""),
      "from_display_name": msg.get("from_display_name", ""),
      "bot_token_hash": bot_token_hash,
      "operator_is_human": not msg.get("from_is_bot", False),
    }

    # Collect attachment file_ids only — the mailbox worker downloads them at
    # event-processing time, keeping this poller callback fast and un-stallable.
    pending_telegram_image_file_id_list = []
    if has_photo and msg.get("photo_file_id"):
      pending_telegram_image_file_id_list.append(msg["photo_file_id"])
    if has_document and msg.get("document_file_id"):
      doc_mime = msg.get("document_mime_type", "")
      if doc_mime.startswith("image/"):
        pending_telegram_image_file_id_list.append(msg["document_file_id"])

    if not text and pending_telegram_image_file_id_list:
      text = "[The user sent an image without any text caption.]"

    payload = {
      "message": text,
      "session_id": f"telegram-{chat_id}",
      "source_metadata": source_metadata,
    }
    if pending_telegram_image_file_id_list:
      payload["pending_telegram_image_file_id_list"] = pending_telegram_image_file_id_list

    idempotency_key = f"tg-{bot_token_hash}-{msg.get('message_id', '')}-{chat_id}"

    enqueue_ok, _enqueue_status, _queue_id = _enqueue_event(
      agent_id=agent_id,
      event_type="telegram_message",
      payload=payload,
      priority="normal",
      queue_mode="queue",
      source_id=source_id,
      idempotency_key=idempotency_key,
    )

    if enqueue_ok:
      try:
        mailbox = _get_or_create_mailbox_for_agent(agent_id)
        mailbox.signal_new_event_available()
      except Exception as mailbox_signal_error:
        MCPLogger.log(TOOL_LOG_NAME,
          f"Telegram callback: enqueued message for {agent_id} but failed to signal mailbox: {mailbox_signal_error}")


def _reregister_all_telegram_event_source_callbacks_after_restart():
  """Re-wire in-process Telegram callbacks for all enabled telegram event sources.

  Called once on server startup (after schema init and crash recovery).
  The event_sources table has the persistent records, but the in-process
  Python callbacks are lost on restart — this function restores them.
  """
  try:
    result = _call_sqlite(
      "SELECT source_id, agent_id, config FROM event_sources WHERE source_type = 'telegram' AND is_enabled = 1",
      database=AGENT_KERNEL_DATABASE_NAME,
    )
    if result.get("isError"):
      MCPLogger.log(TOOL_LOG_NAME, f"Telegram re-registration: failed to query event_sources: {_extract_text_from_mcp_response(result)}")
      return

    response_text = _extract_text_from_mcp_response(result)
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])

    if not rows:
      return

    for row in rows:
      source_id = row.get("source_id")
      agent_id = row.get("agent_id")
      config_raw = row.get("config", "{}")
      try:
        config_dict = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
      except (json.JSONDecodeError, TypeError):
        config_dict = {}

      if source_id and agent_id:
        MCPLogger.log(TOOL_LOG_NAME,
          f"Telegram re-registration on startup: wiring source {source_id} for agent {agent_id}")
        _register_telegram_event_source_callback(source_id, agent_id, config_dict)

    MCPLogger.log(TOOL_LOG_NAME,
      f"Telegram re-registration complete: {len(rows)} source(s) restored")
  except Exception as reregistration_error:
    MCPLogger.log(TOOL_LOG_NAME,
      f"Telegram re-registration failed: {reregistration_error}")


def _register_telegram_event_source_callback(source_id: str, agent_id: str, config: Dict[str, Any]):
  """Register an in-process callback with social.py for incoming Telegram messages.

  Uses social.py's Python-level register_message_event_callback() directly
  (both modules live in the same server process), bypassing the MCP accumulator
  pattern entirely.  This gives us instant push delivery with zero polling.

  Also ensures the Telegram background poller is running (calls start_listening).
  """
  callback_id = f"agent_event_{source_id}"
  allowed_chat_ids = config.get("allowed_chat_ids", [])

  try:
    start_listening_result = _call_tool(_suffixed_tool_name("social"), {"input": {
      "operation": "start_listening",
      "tool_unlock_token": "__auto__",
    }})
    start_status = _extract_text_from_mcp_response(start_listening_result)
    MCPLogger.log(TOOL_LOG_NAME, f"Telegram start_listening for source {source_id}: {start_status}")
  except Exception as start_error:
    MCPLogger.log(TOOL_LOG_NAME,
      f"Warning: could not start Telegram listener for source {source_id}: {start_error}")

  try:
    from ragtag.tools import social as _social_module

    def _inprocess_callback_for_this_agent_and_source(bot_token_hash: str, formatted_messages: List[Dict]):
      _enqueue_inbound_telegram_messages_for_agent(
        agent_id, source_id, bot_token_hash, formatted_messages
      )

    _social_module.register_message_event_callback(
      callback_id,
      _inprocess_callback_for_this_agent_and_source,
      filter_by_chat_ids=allowed_chat_ids if allowed_chat_ids else None,
    )

    _telegram_callback_id_registry[source_id] = callback_id
    MCPLogger.log(TOOL_LOG_NAME,
      f"Registered in-process Telegram callback {callback_id} for agent {agent_id}")
  except Exception as registration_error:
    MCPLogger.log(TOOL_LOG_NAME,
      f"Telegram in-process callback registration failed for source {source_id}: {registration_error}")


def _unregister_telegram_event_source_callback(source_id: str):
  """Unregister an in-process Telegram callback from social.py."""
  callback_id = _telegram_callback_id_registry.pop(source_id, None)
  if callback_id:
    try:
      from ragtag.tools import social as _social_module
      _social_module.unregister_message_event_callback(callback_id)
      MCPLogger.log(TOOL_LOG_NAME, f"Unregistered in-process Telegram callback {callback_id}")
    except Exception as unregistration_error:
      MCPLogger.log(TOOL_LOG_NAME,
        f"Telegram in-process unregistration error for {callback_id}: {unregistration_error}")


# ===============================================================================
# ████████████████████████████████████████████████████████████████████████████████
# CONTACT ACCESS CONTROL — Transport-agnostic user allowlist
# ████████████████████████████████████████████████████████████████████████████████
#
# Deny-by-default: unknown users are blocked from reaching the agent until
# an operator explicitly approves them.  The operator is notified when a new
# contact attempts to reach the agent.  Contact authorization status is stored
# in the `agent_contact_access_control` SQLite table and checked on every
# inbound message before it is enqueued to the agent's mailbox.
#
# The contact_approval_mode column on the agents table controls behavior:
#   "require_approval" (default) — block unknown, notify operator, queue as pending
#   "auto_approve_all" — legacy open mode, all users pass through
#
# ===============================================================================


def _check_if_transport_user_is_authorized_to_contact_agent(
  agent_id: str,
  transport_type: str,
  transport_user_id: str,
) -> str:
  """Look up a transport user's authorization status for a specific agent.

  Returns one of: 'approved', 'pending', 'blocked', or 'unknown' (no record).
  """
  if not transport_user_id:
    return "unknown"
  result = _call_sqlite(
    """SELECT authorization_status FROM agent_contact_access_control
    WHERE agent_id = :agent_id AND transport_type = :transport_type
    AND transport_user_id = :transport_user_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id, "transport_type": transport_type,
              "transport_user_id": str(transport_user_id)},
  )
  if result.get("isError"):
    return "unknown"
  try:
    response_text = _extract_text_from_mcp_response(result)
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if rows:
      return rows[0].get("authorization_status", "unknown")
  except (json.JSONDecodeError, KeyError, IndexError):
    pass
  return "unknown"


def _register_pending_contact_from_transport_user(
  agent_id: str,
  transport_type: str,
  transport_user_id: str,
  display_name: str = "",
  username: str = "",
) -> bool:
  """Create a 'pending' contact access entry. Returns True on success, False on conflict/error."""
  now = _iso_now()
  result = _call_sqlite(
    """INSERT OR IGNORE INTO agent_contact_access_control
    (agent_id, transport_type, transport_user_id, display_name, username, authorization_status, requested_at)
    VALUES (:agent_id, :transport_type, :transport_user_id, :display_name, :username, 'pending', :requested_at)""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={
      "agent_id": agent_id, "transport_type": transport_type,
      "transport_user_id": str(transport_user_id),
      "display_name": display_name, "username": username,
      "requested_at": now,
    },
  )
  return not result.get("isError", True)


def _update_contact_authorization_status(
  contact_id: int,
  new_status: str,
  resolved_by: str = "operator",
) -> Tuple[bool, str]:
  """Update a contact entry's authorization_status. Returns (success, message)."""
  now = _iso_now()
  result = _call_sqlite(
    """UPDATE agent_contact_access_control
    SET authorization_status = :new_status, resolved_at = :resolved_at, resolved_by = :resolved_by
    WHERE contact_id = :contact_id""",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"contact_id": contact_id, "new_status": new_status,
              "resolved_at": now, "resolved_by": resolved_by},
  )
  if result.get("isError"):
    return False, _extract_text_from_mcp_response(result)[:200]
  response_text = _extract_text_from_mcp_response(result)
  if '"rows_modified_by_operation": 0' in response_text:
    return False, f"No contact found with contact_id={contact_id}"
  MCPLogger.log(TOOL_LOG_NAME,
    f"CONTACT STATUS CHANGED: contact_id={contact_id} set to '{new_status}' by {resolved_by}")
  return True, f"Contact {contact_id} set to '{new_status}'"


def _notify_operator_of_pending_contact_request(
  agent_id: str,
  agent_config: Dict[str, Any],
  transport_type: str,
  display_name: str,
  username: str,
  transport_user_id: str,
) -> None:
  """Fire-and-forget notification to the operator about a new contact request."""
  name_display = display_name or username or str(transport_user_id)
  username_part = f" (@{username})" if username else ""
  message_text = (
    f"[Contact Request] New {transport_type} user wants to reach agent '{agent_id}':\n"
    f"  Name: {name_display}{username_part}\n"
    f"  User ID: {transport_user_id}\n\n"
    f"Use /admin > Safety > Contacts to approve or block, "
    f"or ask me to 'approve contact from {name_display}'."
  )
  _dispatch_message_to_operator_via_last_active_or_default_channel(
    agent_id, message_text, agent_config
  )


def _get_agent_contact_approval_mode(agent_id: str) -> str:
  """Fetch the contact_approval_mode for an agent. Defaults to 'require_approval'."""
  result = _call_sqlite(
    "SELECT contact_approval_mode FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": agent_id},
  )
  if result.get("isError"):
    return "require_approval"
  try:
    response_text = _extract_text_from_mcp_response(result)
    response_data = json.loads(response_text)
    rows = response_data.get("data_rows_from_result_set", [])
    if rows:
      return rows[0].get("contact_approval_mode") or "require_approval"
  except (json.JSONDecodeError, KeyError, IndexError):
    pass
  return "require_approval"


def handle_approve_contact(params: Dict) -> Dict:
  """Approve a pending contact request, allowing the user to reach the agent."""
  contact_id = params.get("contact_id")
  if contact_id is None:
    return create_error_response("contact_id is required")
  try:
    contact_id = int(contact_id)
  except (ValueError, TypeError):
    return create_error_response(f"contact_id must be an integer, got: {contact_id}")

  ok, msg = _update_contact_authorization_status(contact_id, "approved", resolved_by="operator")
  if not ok:
    return create_error_response(msg)
  return {"content": [{"type": "text", "text": msg}], "isError": False}


def handle_block_contact(params: Dict) -> Dict:
  """Block a contact, preventing the user from reaching the agent."""
  contact_id = params.get("contact_id")
  if contact_id is None:
    return create_error_response("contact_id is required")
  try:
    contact_id = int(contact_id)
  except (ValueError, TypeError):
    return create_error_response(f"contact_id must be an integer, got: {contact_id}")

  ok, msg = _update_contact_authorization_status(contact_id, "blocked", resolved_by="operator")
  if not ok:
    return create_error_response(msg)
  return {"content": [{"type": "text", "text": msg}], "isError": False}


def handle_list_contacts(params: Dict) -> Dict:
  """List all contact access control entries for an agent, optionally filtered by status."""
  agent_id = params["agent_id"]
  status_filter = params.get("status")

  sql = """SELECT contact_id, transport_type, transport_user_id, display_name, username,
  authorization_status, requested_at, resolved_at, resolved_by
  FROM agent_contact_access_control WHERE agent_id = :agent_id"""
  bindings: Dict[str, Any] = {"agent_id": agent_id}

  if status_filter:
    sql += " AND authorization_status = :status_filter"
    bindings["status_filter"] = status_filter

  sql += " ORDER BY requested_at DESC"

  result = _call_sqlite(sql, database=AGENT_KERNEL_DATABASE_NAME, bindings=bindings)
  if result.get("isError"):
    return create_error_response(f"Failed to list contacts: {_extract_text_from_mcp_response(result)[:300]}")

  return {"content": [{"type": "text", "text": _extract_text_from_mcp_response(result)}], "isError": False}


# ===============================================================================
# ████████████████████████████████████████████████████████████████████████████████
# ADMIN INTERFACE — Transport-agnostic, LLM-free, text-based menu shell
# ████████████████████████████████████████████████████████████████████████████████
#
# Purpose: Provide a procedural escape hatch so human operators can administer
# agents from any communication channel (Telegram, web, voice-to-text, SMS)
# without invoking any LLM. Entered via "/admin", exited via "/chat" or "/exit".
# Works even when the agent's LLM is broken, out of budget, or crashed.
#
# Architecture (see build_prompt-admin-interface.md):
#   Admin.1  state: per-channel in-memory dict + SQLite persistence table
#   Admin.2  entry/exit detection + STT-tolerant text normalization
#   Admin.3  menu tree as a data structure (not hardcoded if/else)
#   Admin.4  input parser: routes raw text into navigation/action/guided input
#   Admin.5  renderer: plain-text menu and result formatters
#   Admin.6  executor: calls MCP handlers directly, formats results
#   Admin.7  guided input: multi-step parameter collection state machine
#   Admin.8  intercept hooks in handle_send_message and the Telegram callback
# ===============================================================================


# ───── Admin.1: Admin state management ─────────────────────────────────────────

_admin_mode_state_per_channel: Dict[str, Dict[str, Any]] = {}
_admin_mode_state_cache_lock = threading.RLock()


def _derive_admin_channel_key_from_source_metadata(source_metadata: Optional[Dict[str, Any]]) -> Optional[str]:
  """Derive a stable channel key from an event's source_metadata.

  Channel keys are short strings of the form "<channel_type>:<identifier>"
  used to look up admin mode state for an operator channel.  Returns None
  when the metadata does not identify a channel (e.g., internal events).

  Supported forms:
    - Telegram: {"channel_type": "telegram", "chat_id": 12345} → "tg:12345"
    - WhatsApp: {"channel_type": "whatsapp", "jid": "..."}     → "wa:<jid>"
    - Desktop:  {"channel_type": "desktop", "user_id": "..."}  → "desktop:<user_id>"
    - Generic:  {"channel_type": "<type>", "channel_id": "x"}  → "<type>:x"
  """
  if not isinstance(source_metadata, dict):
    return None

  channel_type = source_metadata.get("channel_type") or source_metadata.get("type")
  if not channel_type:
    return None

  channel_type_lowered = str(channel_type).lower()

  if channel_type_lowered == "telegram":
    chat_id = source_metadata.get("chat_id")
    if chat_id is not None:
      return f"tg:{chat_id}"
    return None

  if channel_type_lowered == "whatsapp":
    jid = source_metadata.get("jid") or source_metadata.get("channel_id")
    if jid:
      return f"wa:{jid}"
    return None

  explicit_channel_id = source_metadata.get("channel_id") or source_metadata.get("user_id") or source_metadata.get("session_id")
  if explicit_channel_id:
    return f"{channel_type_lowered}:{explicit_channel_id}"

  return None


def _bind_admin_channel_key_to_authenticated_operator_identity(
  admin_channel_key: str,
  handler_info: Optional[Dict[str, Any]],
) -> str:
  """Namespace an MCP-derived admin channel key by the authenticated MCP user.

  Admin menu state must be bound to an operator IDENTITY, not to the free-form
  channel string an MCP client chooses: without this, two clients passing the
  same channel_id share one admin session, and a client could fabricate a
  "tg:<chat_id>" key to hijack a Telegram operator's admin state. The
  authenticated username comes from the server's per-request thread-local via
  handler_info's responder; unauthenticated callers (server auth disabled) are
  segregated under their own namespace so they can never collide with an
  authenticated operator's session.
  """
  authenticated_operator_username = None
  if isinstance(handler_info, dict):
    server_responder = handler_info.get('responder')
    if server_responder is not None:
      try:
        authenticated_operator_username = getattr(server_responder, 'authenticated_user', None)
      except Exception:
        authenticated_operator_username = None
  if authenticated_operator_username:
    return f"op:{authenticated_operator_username}:{admin_channel_key}"
  return f"op-anon:{admin_channel_key}"


def _derive_admin_channel_key_from_send_message_params(params: Dict[str, Any]) -> Optional[str]:
  """Derive a channel key from handle_send_message params.

  Preference order:
    1. explicit "channel_id" parameter       → used verbatim (e.g. "mcp:cursor")
    2. "source_metadata" dict                → derived via the shared helper
    3. None                                  → admin mode unreachable this call
  """
  if not isinstance(params, dict):
    return None

  explicit_channel_id = params.get("channel_id")
  if isinstance(explicit_channel_id, str) and explicit_channel_id.strip():
    return explicit_channel_id.strip()

  return _derive_admin_channel_key_from_source_metadata(params.get("source_metadata"))


def _build_default_admin_state_dict(active_agent_id: Optional[str]) -> Dict[str, Any]:
  """Build a fresh admin state dict for a channel entering admin mode."""
  return {
    "menu_path": [],
    "active_agent_id": active_agent_id,
    "active_session_id": None,
    "pending_guided_input": None,
    "entered_at": _iso_now(),
  }


def _load_admin_state_from_sqlite_for_channel(channel_key: str) -> Optional[Dict[str, Any]]:
  """Read one admin_channel_state row from SQLite and parse its state_json."""
  result = _call_sqlite(
    "SELECT state_json, entered_at FROM admin_channel_state WHERE channel_key = :channel_key",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"channel_key": channel_key},
  )
  rows = _parse_rows_from_mcp_query_response(result)
  if not rows:
    return None
  row = rows[0]
  try:
    state = json.loads(row.get("state_json") or "{}")
  except (json.JSONDecodeError, TypeError):
    return None
  if not isinstance(state, dict):
    return None
  state.setdefault("menu_path", [])
  state.setdefault("active_agent_id", None)
  state.setdefault("active_session_id", None)
  state.setdefault("pending_guided_input", None)
  state.setdefault("entered_at", row.get("entered_at"))
  return state


def _get_admin_state_for_channel(channel_key: str) -> Optional[Dict[str, Any]]:
  """Return the admin state for a channel, or None if not in admin mode.

  Memory cache is consulted first; if absent, SQLite is checked and the
  result is promoted into the cache.  Returning None means the channel is
  NOT currently in admin mode.
  """
  if not channel_key:
    return None
  with _admin_mode_state_cache_lock:
    cached = _admin_mode_state_per_channel.get(channel_key)
    if cached is not None:
      return cached
  try:
    initialize_agent_kernel_database()
    persisted_state = _load_admin_state_from_sqlite_for_channel(channel_key)
  except Exception as state_load_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Admin state load failed for '{channel_key}': {state_load_error}")
    return None
  if persisted_state is None:
    return None
  with _admin_mode_state_cache_lock:
    _admin_mode_state_per_channel[channel_key] = persisted_state
  return persisted_state


def _set_admin_state_for_channel(channel_key: str, state: Dict[str, Any]) -> None:
  """Persist admin state to memory and SQLite (upsert by channel_key)."""
  if not channel_key:
    return
  with _admin_mode_state_cache_lock:
    _admin_mode_state_per_channel[channel_key] = state
  try:
    now = _iso_now()
    entered_at = state.get("entered_at") or now
    state_json = json.dumps(state, default=str)
    _call_sqlite(
      """INSERT INTO admin_channel_state (channel_key, state_json, entered_at, last_activity_at)
      VALUES (:channel_key, :state_json, :entered_at, :last_activity_at)
      ON CONFLICT(channel_key) DO UPDATE SET
        state_json = excluded.state_json,
        last_activity_at = excluded.last_activity_at""",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={
        "channel_key": channel_key,
        "state_json": state_json,
        "entered_at": entered_at,
        "last_activity_at": now,
      },
    )
  except Exception as state_persist_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Admin state persist failed for '{channel_key}': {state_persist_error}")


def _clear_admin_state_for_channel(channel_key: str) -> None:
  """Remove admin state for a channel from both memory and SQLite."""
  if not channel_key:
    return
  with _admin_mode_state_cache_lock:
    _admin_mode_state_per_channel.pop(channel_key, None)
  try:
    _call_sqlite(
      "DELETE FROM admin_channel_state WHERE channel_key = :channel_key",
      database=AGENT_KERNEL_DATABASE_NAME,
      bindings={"channel_key": channel_key},
    )
  except Exception as state_clear_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Admin state clear failed for '{channel_key}': {state_clear_error}")


def _is_channel_in_admin_mode(channel_key: str) -> bool:
  """Fast boolean check for whether a channel is currently in admin mode.

  Memory cache is the primary source; SQLite is checked only if the cache
  has no entry.  This is called on every inbound message, so cache hits
  should be the common case.
  """
  return _get_admin_state_for_channel(channel_key) is not None


# ───── Admin.2: Entry/exit detection and STT-tolerant text normalization ───────

ADMIN_STT_NUMBER_WORD_TO_DIGIT_LOOKUP = {
  "zero": "0", "oh": "0", "nought": "0",
  "one": "1", "won": "1",
  "two": "2", "to": "2", "too": "2",
  "three": "3",
  "four": "4", "for": "4", "fore": "4",
  "five": "5",
  "six": "6", "sicks": "6",
  "seven": "7",
  "eight": "8", "ate": "8",
  "nine": "9", "niner": "9",
}

ADMIN_ENTRY_COMMAND_NORMALIZED_FORMS = {"/admin", "admin"}
ADMIN_EXIT_COMMAND_NORMALIZED_FORMS = {"/chat", "/exit", "chat", "exit"}
ADMIN_NAV_BACK_NORMALIZED_FORMS = {"back", "up", ".."}
ADMIN_NAV_HOME_NORMALIZED_FORMS = {"home", "main", "menu", "top"}
ADMIN_NAV_HELP_NORMALIZED_FORMS = {"help", "?"}
ADMIN_NAV_EXIT_SHORT_NORMALIZED_FORMS = {"0", "quit", "exit"}
ADMIN_CANCEL_NORMALIZED_FORMS = {"cancel", "abort", "nevermind", "never mind"}


def _normalize_admin_input_text_for_stt_tolerance(raw_text: str) -> str:
  """Normalize raw operator input for menu parsing, tolerant of STT artifacts.

  This normalizer ONLY fires replacements safe for short control messages:
    1. Strip leading/trailing whitespace.
    2. Strip one trailing piece of sentence punctuation (., , ! ? ;).
    3. Lowercase.
    4. Replace leading "slash " → "/" (STT often transcribes slashes as words).
    5. Replace "go back" → "back" and "go home" → "home" (multi-word nav).
    6. If the remaining text is a SINGLE word that matches the number-word
       lookup, return its digit.  Multi-word inputs are left untouched so
       that "for my agent" is NOT mangled into "4 my agent".

  Returns the normalized text.  Input "", None, or non-string returns "".
  """
  if not isinstance(raw_text, str):
    return ""
  working = raw_text.strip()
  if not working:
    return ""
  while working and working[-1] in ".,!?;":
    working = working[:-1].rstrip()
  if not working:
    return ""
  working = working.lower()
  if working.startswith("slash "):
    working = "/" + working[6:].lstrip()
  if working == "go back":
    return "back"
  if working == "go home":
    return "home"
  if working == "go up":
    return "back"
  if " " not in working and working in ADMIN_STT_NUMBER_WORD_TO_DIGIT_LOOKUP:
    return ADMIN_STT_NUMBER_WORD_TO_DIGIT_LOOKUP[working]
  return working


def _check_if_message_is_admin_entry_command(normalized_text: str) -> bool:
  """Return True iff the normalized text is an admin-mode entry command."""
  return normalized_text in ADMIN_ENTRY_COMMAND_NORMALIZED_FORMS


def _check_if_message_is_admin_exit_command(normalized_text: str) -> bool:
  """Return True iff the normalized text is an admin-mode exit command."""
  return normalized_text in ADMIN_EXIT_COMMAND_NORMALIZED_FORMS


# ───── Admin.3: Menu tree data structure ───────────────────────────────────────

ADMIN_MENU_TREE: Dict[str, Any] = {
  "label": "ADMIN MAIN MENU",
  "items": [
    {
      "key": "1", "label": "Agents",
      "shortcut_words": ["agents", "agent"],
      "description": "list, select, create, configure, delete",
      "submenu": {
        "label": "AGENTS",
        "items": [
          {"key": "1", "label": "List all agents", "shortcut_words": ["list"],
           "action": {"operation": "list_agents", "params": {}}},
          {"key": "2", "label": "Select active agent", "shortcut_words": ["select", "switch"],
           "action": {"type": "select_agent"}},
          {"key": "3", "label": "Show active agent config", "shortcut_words": ["show", "config"],
           "requires_active_agent": True,
           "action": {"operation": "get_agent"}},
          {"key": "4", "label": "Create new agent", "shortcut_words": ["create", "new"],
           "action": {"operation": "create_agent", "guided_input": "create_agent"}},
          {"key": "5", "label": "Delete active agent", "shortcut_words": ["delete", "remove"],
           "requires_active_agent": True,
           "action": {"operation": "delete_agent", "guided_input": "delete_agent"}},
          {"key": "6", "label": "Pause / Resume active agent", "shortcut_words": ["pause", "resume", "pause_resume"],
           "requires_active_agent": True,
           "action": {"type": "toggle_pause"}},
          {"key": "7", "label": "Interrupt current run", "shortcut_words": ["interrupt"],
           "requires_active_agent": True,
           "action": {"operation": "interrupt_agent", "guided_input": "interrupt_agent"}},
        ],
      },
    },
    {
      "key": "2", "label": "Sessions",
      "shortcut_words": ["sessions", "session"],
      "description": "history, switch, new",
      "requires_active_agent": True,
      "submenu": {
        "label": "SESSIONS",
        "items": [
          {"key": "1", "label": "Show recent history", "shortcut_words": ["history", "recent"],
           "requires_active_agent": True,
           "action": {"operation": "get_history", "params": {"limit": 20}}},
          {"key": "2", "label": "Show active session id", "shortcut_words": ["show"],
           "action": {"type": "show_active_session"}},
          {"key": "3", "label": "Start new session", "shortcut_words": ["new", "start"],
           "action": {"type": "new_session"}},
          {"key": "4", "label": "Set active session id", "shortcut_words": ["set", "switch"],
           "action": {"type": "set_active_session"}},
        ],
      },
    },
    {
      "key": "3", "label": "Engine",
      "shortcut_words": ["engine", "model", "models"],
      "description": "primary, compaction, fallback engines",
      "requires_active_agent": True,
      "submenu": {
        "label": "ENGINE",
        "items": [
          {"key": "1", "label": "Show current engine config", "shortcut_words": ["show", "config"],
           "requires_active_agent": True,
           "action": {"type": "show_engine_config"}},
          {"key": "2", "label": "Set primary engine", "shortcut_words": ["primary", "set_primary"],
           "requires_active_agent": True,
           "action": {"type": "set_primary_engine"}},
          {"key": "3", "label": "Set compaction engine", "shortcut_words": ["compaction", "set_compaction"],
           "requires_active_agent": True,
           "action": {"type": "set_compaction_engine"}},
          {"key": "4", "label": "List models for primary endpoint", "shortcut_words": ["list_models"],
           "requires_active_agent": True,
           "action": {"type": "list_models_for_current_provider"}},
          {"key": "5", "label": "Search OpenRouter models", "shortcut_words": ["search_models", "search"],
           "action": {"type": "search_openrouter_models"}},
          {"key": "6", "label": "Show fallback chain", "shortcut_words": ["fallback", "show_fallback"],
           "requires_active_agent": True,
           "action": {"type": "show_fallback_chain"}},
          {"key": "7", "label": "Add fallback entry", "shortcut_words": ["add_fallback"],
           "requires_active_agent": True,
           "action": {"type": "add_fallback_entry"}},
          {"key": "8", "label": "Remove fallback entry", "shortcut_words": ["remove_fallback"],
           "requires_active_agent": True,
           "action": {"type": "remove_fallback_entry"}},
        ],
      },
    },
    {
      "key": "4", "label": "Stats",
      "shortcut_words": ["stats", "statistics"],
      "description": "cost, runs, status, logs",
      "submenu": {
        "label": "STATS",
        "items": [
          {"key": "1", "label": "Today's cost and runs", "shortcut_words": ["cost", "today", "today_cost"],
           "requires_active_agent": True,
           "action": {"type": "today_cost"}},
          {"key": "2", "label": "Recent runs (last 10)", "shortcut_words": ["runs"],
           "requires_active_agent": True,
           "action": {"operation": "get_run_log", "params": {"limit": 10}}},
          {"key": "3", "label": "Recent session log (last 20)", "shortcut_words": ["log"],
           "requires_active_agent": True,
           "action": {"operation": "get_session_log", "params": {"limit": 20}}},
          {"key": "4", "label": "Kernel status", "shortcut_words": ["status", "kernel_status"],
           "action": {"operation": "status", "params": {}}},
          {"key": "5", "label": "Active agent status", "shortcut_words": ["agent_status"],
           "requires_active_agent": True,
           "action": {"operation": "get_agent"}},
        ],
      },
    },
    {
      "key": "5", "label": "Memory",
      "shortcut_words": ["memory", "memories"],
      "description": "search, add, remove, working context",
      "requires_active_agent": True,
      "submenu": {
        "label": "MEMORY",
        "items": [
          {"key": "1", "label": "List memories", "shortcut_words": ["list"],
           "requires_active_agent": True,
           "action": {"operation": "get_memory", "params": {"limit": 20}}},
          {"key": "2", "label": "Search memory (semantic)", "shortcut_words": ["search", "find"],
           "requires_active_agent": True,
           "action": {"operation": "get_memory", "guided_input": "search_memory"}},
          {"key": "3", "label": "Add memory", "shortcut_words": ["add", "insert"],
           "requires_active_agent": True,
           "action": {"operation": "set_memory", "guided_input": "add_memory"}},
          {"key": "4", "label": "Delete memory by id", "shortcut_words": ["delete"],
           "requires_active_agent": True,
           "action": {"operation": "delete_memory", "guided_input": "delete_memory"}},
          {"key": "5", "label": "View working context", "shortcut_words": ["working", "context"],
           "requires_active_agent": True,
           "action": {"type": "show_working_context"}},
          {"key": "6", "label": "Edit working context", "shortcut_words": ["edit"],
           "requires_active_agent": True,
           "action": {"operation": "update_agent", "guided_input": "edit_working_context"}},
        ],
      },
    },
    {
      "key": "6", "label": "Events",
      "shortcut_words": ["events", "triggers"],
      "description": "list, add cron, add telegram, remove",
      "submenu": {
        "label": "EVENTS",
        "items": [
          {"key": "1", "label": "List event sources", "shortcut_words": ["list"],
           "action": {"type": "list_event_sources_for_active_agent"}},
          {"key": "2", "label": "Add cron trigger", "shortcut_words": ["cron", "add_cron"],
           "requires_active_agent": True,
           "action": {"operation": "add_event_source", "guided_input": "add_cron_source"}},
          {"key": "3", "label": "Add Telegram source", "shortcut_words": ["telegram", "add_telegram"],
           "requires_active_agent": True,
           "action": {"operation": "add_event_source", "guided_input": "add_telegram_source"}},
          {"key": "4", "label": "Remove event source by id", "shortcut_words": ["remove"],
           "action": {"operation": "remove_event_source", "guided_input": "remove_event_source"}},
        ],
      },
    },
    {
      "key": "7", "label": "Safety",
      "shortcut_words": ["safety", "approvals"],
      "description": "approvals, permissions, rate limits, DLQ, contacts",
      "submenu": {
        "label": "SAFETY",
        "items": [
          {"key": "1", "label": "Pending approvals", "shortcut_words": ["pending"],
           "action": {"operation": "get_pending_approvals", "params": {}}},
          {"key": "2", "label": "Approve request by id", "shortcut_words": ["approve"],
           "action": {"operation": "approve_action", "guided_input": "approve_action"}},
          {"key": "3", "label": "Deny request by id", "shortcut_words": ["deny"],
           "action": {"operation": "deny_action", "guided_input": "deny_action"}},
          {"key": "4", "label": "Show tool permissions", "shortcut_words": ["permissions"],
           "requires_active_agent": True,
           "action": {"type": "show_tool_permissions"}},
          {"key": "5", "label": "Show rate limits", "shortcut_words": ["limits"],
           "requires_active_agent": True,
           "action": {"type": "show_rate_limits"}},
          {"key": "6", "label": "Dead letter queue (list)", "shortcut_words": ["dlq"],
           "action": {"operation": "get_dlq", "params": {}}},
          {"key": "7", "label": "Retry DLQ entry by id", "shortcut_words": ["retry"],
           "action": {"operation": "retry_dlq", "guided_input": "retry_dlq"}},
          {"key": "8", "label": "Discard DLQ entry by id", "shortcut_words": ["discard"],
           "action": {"operation": "discard_dlq", "guided_input": "discard_dlq"}},
          {"key": "9", "label": "Contacts — list all", "shortcut_words": ["contacts", "contact_list"],
           "requires_active_agent": True,
           "action": {"operation": "list_contacts"}},
          {"key": "10", "label": "Contacts — pending requests", "shortcut_words": ["pending_contacts"],
           "requires_active_agent": True,
           "action": {"operation": "list_contacts", "params": {"status": "pending"}}},
          {"key": "11", "label": "Approve contact by id", "shortcut_words": ["approve_contact"],
           "requires_active_agent": True,
           "action": {"operation": "approve_contact", "guided_input": "approve_contact"}},
          {"key": "12", "label": "Block contact by id", "shortcut_words": ["block_contact", "block"],
           "requires_active_agent": True,
           "action": {"operation": "block_contact", "guided_input": "block_contact"}},
        ],
      },
    },
    {
      "key": "8", "label": "Endpoints",
      "shortcut_words": ["endpoints", "endpoint"],
      "description": "list, add, edit, remove, test LLM endpoints",
      "submenu": {
        "label": "LLM ENDPOINTS",
        "items": [
          {"key": "1", "label": "List configured endpoints", "shortcut_words": ["list"],
           "action": {"type": "list_configured_endpoints"}},
          {"key": "2", "label": "Add new endpoint", "shortcut_words": ["add", "new"],
           "action": {"type": "add_endpoint"}},
          {"key": "3", "label": "Edit endpoint", "shortcut_words": ["edit", "modify"],
           "action": {"type": "edit_endpoint"}},
          {"key": "4", "label": "Remove endpoint", "shortcut_words": ["remove", "delete"],
           "action": {"type": "remove_endpoint"}},
          {"key": "5", "label": "Test endpoint connectivity", "shortcut_words": ["test", "check", "health"],
           "action": {"type": "test_endpoint_health"}},
          {"key": "6", "label": "Set active agent endpoint", "shortcut_words": ["set", "assign"],
           "requires_active_agent": True,
           "action": {"type": "set_agent_endpoint"}},
          {"key": "7", "label": "Scan for local model servers", "shortcut_words": ["scan", "discover", "find"],
           "action": {"type": "scan_for_local_endpoints"}},
        ],
      },
    },
    {
      "key": "9", "label": "System",
      "shortcut_words": ["system"],
      "description": "kernel status, compact, reflect, respond",
      "submenu": {
        "label": "SYSTEM",
        "items": [
          {"key": "1", "label": "Kernel status", "shortcut_words": ["status"],
           "action": {"operation": "status", "params": {}}},
          {"key": "2", "label": "Force compaction of active agent", "shortcut_words": ["compact"],
           "requires_active_agent": True,
           "action": {"operation": "compact_context"}},
          {"key": "3", "label": "Force reflection of active agent", "shortcut_words": ["reflect"],
           "requires_active_agent": True,
           "action": {"operation": "reflect_now"}},
          {"key": "4", "label": "Respond to waiting ask_user", "shortcut_words": ["respond", "reply"],
           "requires_active_agent": True,
           "action": {"operation": "respond_to_user_request", "guided_input": "respond_to_user_request"}},
          {"key": "5", "label": "Show checkpoints for a run", "shortcut_words": ["checkpoints"],
           "action": {"operation": "get_checkpoints", "guided_input": "get_checkpoints"}},
        ],
      },
    },
  ],
}

ADMIN_GLOBAL_SHORTCUT_WORDS: Dict[str, Any] = {
  "pause": ["1", "6"],
  "resume": ["1", "6"],
  "pause_resume": ["1", "6"],
  "stats": ["4", "1"],
  "cost": ["4", "1"],
  "today": ["4", "1"],
  "today_cost": ["4", "1"],
  "status": ["4", "4"],
  "kernel_status": ["4", "4"],
  "engine": ["3"],
  "model": ["3"],
  "models": ["3"],
  "primary": ["3", "2"],
  "set_primary": ["3", "2"],
  "compaction": ["3", "3"],
  "set_compaction": ["3", "3"],
  "list_models": ["3", "4"],
  "search_models": ["3", "5"],
  "fallback": ["3", "6"],
  "show_fallback": ["3", "6"],
  "add_fallback": ["3", "7"],
  "remove_fallback": ["3", "8"],
  "approve": ["7", "2"],
  "deny": ["7", "3"],
  "agents": ["1"],
  "list": ["1", "1"],
  "memory": ["5"],
  "events": ["6"],
  "safety": ["7"],
  "endpoints": ["8"],
  "system": ["9"],
  "sessions": ["2"],
  "dlq": ["7", "6"],
  "contacts": ["7", "9"],
  "pending_contacts": ["7", "10"],
  "approve_contact": ["7", "11"],
  "block_contact": ["7", "12"],
  "block": ["7", "12"],
  "help": None,
  "?": None,
}


def _resolve_menu_node_from_path(menu_path: List[str]) -> Optional[Dict[str, Any]]:
  """Walk ADMIN_MENU_TREE by 'key' at each level and return the node reached.

  An empty path returns the root menu.  Returns None if any key in the path
  fails to match at its level (e.g., stale path after a menu reorganization).
  """
  current_node = ADMIN_MENU_TREE
  for path_key in menu_path:
    items_at_current_level = current_node.get("items") if current_node is ADMIN_MENU_TREE else (current_node.get("submenu") or {}).get("items")
    if items_at_current_level is None:
      items_at_current_level = current_node.get("items")
    if items_at_current_level is None:
      return None
    next_node = None
    for item in items_at_current_level:
      if item.get("key") == path_key:
        next_node = item
        break
    if next_node is None:
      return None
    current_node = next_node
  return current_node


def _get_menu_items_at_node(menu_node: Dict[str, Any]) -> List[Dict[str, Any]]:
  """Return the items list for a node, whether it's the root or a submenu."""
  if menu_node is ADMIN_MENU_TREE:
    return menu_node.get("items", [])
  submenu = menu_node.get("submenu")
  if isinstance(submenu, dict):
    return submenu.get("items", [])
  return menu_node.get("items", [])


def _get_menu_label_for_node(menu_node: Dict[str, Any]) -> str:
  """Return the display label for a menu node."""
  if menu_node is ADMIN_MENU_TREE:
    return menu_node.get("label", "ADMIN MAIN MENU")
  submenu = menu_node.get("submenu")
  if isinstance(submenu, dict) and submenu.get("label"):
    return submenu["label"]
  return menu_node.get("label", "MENU")


# ───── Admin.5: Menu renderer and result formatters ────────────────────────────

ADMIN_CONTEXT_MODE_CHOICE_MAP = {"1": "raw", "2": "harnessed"}
ADMIN_LLM_PROVIDER_CHOICE_MAP = {"1": "mlx", "2": "custom", "3": "openrouter", "4": "ollama", "5": "cursor_agent"}


def _build_dynamic_endpoint_choice_map() -> Dict[str, str]:
  """Build a numeric choice map from currently configured LLM endpoints."""
  from ragtag.shared_config import list_all_llm_endpoints
  endpoints = list_all_llm_endpoints()
  return {str(i): ep.get("endpoint_name", "?") for i, ep in enumerate(endpoints, 1)}
ADMIN_MEMORY_TYPE_CHOICE_MAP = {"1": "fact", "2": "preference", "3": "project_knowledge", "4": "decision", "5": "task", "6": "rule"}
ADMIN_PRIORITY_CHOICE_MAP = {"1": "high", "2": "normal", "3": "low"}
ADMIN_QUEUE_MODE_CHOICE_MAP = {"1": "queue", "2": "collect", "3": "drop", "4": "preempt"}


def _fetch_active_agent_snapshot_for_header(active_agent_id: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
  """Return (agent_id, state, display_name) for the header, or (id, None, None) if missing.

  Used when rendering every menu so the header reflects the freshest state
  of the active agent. The query is a single indexed lookup — cheap.
  Returns (None, None, None) when no agent is selected.
  """
  if not active_agent_id:
    return (None, None, None)
  row_result = _call_sqlite(
    "SELECT agent_id, current_state, is_paused, display_name FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": active_agent_id},
  )
  rows = _parse_rows_from_mcp_query_response(row_result)
  if not rows:
    return (active_agent_id, None, None)
  r = rows[0]
  state_label = r.get("current_state") or "UNKNOWN"
  if r.get("is_paused"):
    state_label = f"{state_label}/PAUSED"
  return (r.get("agent_id") or active_agent_id, state_label, r.get("display_name"))


def _render_admin_header_line(state: Dict[str, Any]) -> str:
  """Render the one-line header shown above every admin menu."""
  active_agent_id = state.get("active_agent_id")
  agent_id, agent_state, display_name = _fetch_active_agent_snapshot_for_header(active_agent_id)
  if agent_id and agent_state:
    if display_name and display_name != agent_id:
      return f"[active: {agent_id} ({agent_state}) — {display_name}]"
    return f"[active: {agent_id} ({agent_state})]"
  if active_agent_id:
    return f"[active: {active_agent_id} (not found — please reselect)]"
  return "[no agent selected]"


def _render_admin_menu_as_plain_text(menu_node: Dict[str, Any], state: Dict[str, Any]) -> str:
  """Render a menu node as plain text suitable for any transport.

  Layout:
    LABEL [active: agent (STATE)]

    1. Item label    - description
    2. ...

    0. Back / Exit

    Type a number, or a command word.
  """
  header = _render_admin_header_line(state)
  label = _get_menu_label_for_node(menu_node)
  items = _get_menu_items_at_node(menu_node)
  active_agent_id = state.get("active_agent_id")

  lines: List[str] = []
  lines.append(f"{label} {header}")
  lines.append("")

  for item in items:
    key = item.get("key", "")
    item_label = item.get("label", "")
    description = item.get("description", "")
    needs_agent = item.get("requires_active_agent", False) and not active_agent_id
    suffix = ""
    if needs_agent:
      suffix = "   (select an agent first)"
    elif description:
      suffix = f" — {description}"
    lines.append(f"  {key}. {item_label}{suffix}")

  lines.append("")
  if menu_node is ADMIN_MENU_TREE:
    lines.append("  0. Exit admin mode")
    lines.append("")
    lines.append("Type a number, or a command word (pause, stats, model, approve, help).")
  else:
    lines.append("  0. Back to main menu")
    lines.append("")
    lines.append("Type a number, 'back', 'home', or 'help'. '/chat' exits admin mode.")

  return "\n".join(lines)


def _format_generic_success_result(operation: str, result_data: Any) -> str:
  """Fallback formatter when no operation-specific formatter exists.

  Accepts either a parsed dict or a raw string and produces a short human
  line.  Long JSON is pretty-printed but truncated at 2000 chars so voice
  transports remain tolerable.
  """
  if isinstance(result_data, str):
    return f"{operation}: {result_data[:2000]}"
  try:
    as_json = json.dumps(result_data, indent=2, default=str)
  except (TypeError, ValueError):
    as_json = str(result_data)
  if len(as_json) > 2000:
    as_json = as_json[:2000] + "\n... (truncated)"
  return f"{operation} result:\n{as_json}"


def _extract_rows_from_sqlite_text_or_list(parsed: Any) -> List[Dict[str, Any]]:
  """Unwrap either a raw rows list or the sqlite envelope dict to a list of rows."""
  if isinstance(parsed, list):
    return [r for r in parsed if isinstance(r, dict)]
  if isinstance(parsed, dict):
    rows = parsed.get("data_rows_from_result_set")
    if isinstance(rows, list):
      return [r for r in rows if isinstance(r, dict)]
  return []


def _format_list_agents_result(result_data: Any, state: Dict[str, Any]) -> str:
  """Pretty list of agents with state and display name; highlights active one."""
  rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "AGENTS: (none)\n\nUse option 4 in the Agents menu to create one."
  active_id = state.get("active_agent_id")
  lines: List[str] = [f"AGENTS ({len(rows)} total):", ""]
  for idx, row in enumerate(rows, start=1):
    aid = row.get("agent_id", "?")
    disp = row.get("display_name", "")
    cur = row.get("current_state", "?")
    paused_flag = " PAUSED" if row.get("is_paused") else ""
    marker = " *" if aid == active_id else "  "
    lines.append(f"{marker}{idx}. {aid:<28}  [{cur}{paused_flag}]  \"{disp}\"")
  lines.append("")
  if active_id:
    lines.append(f"Active: {active_id}")
  lines.append("Type a number to select, or 0 to go back.")
  return "\n".join(lines)


def _format_get_agent_result(result_data: Any) -> str:
  """Display key config fields of an agent as plain text."""
  rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "AGENT: (not found)"
  r = rows[0]
  important_field_order = [
    "agent_id", "display_name", "current_state", "is_paused",
    "llm_endpoint", "llm_provider", "llm_model",
    "compaction_endpoint", "compaction_provider", "compaction_model",
    "context_mode", "harness_session_type",
    "max_tool_rounds_per_run", "max_tokens_per_day",
    "max_tool_calls_per_hour", "max_llm_calls_per_hour",
    "reflection_enabled", "reflection_idle_timeout_minutes",
    "read_tools_allowed", "write_tools_allowed", "tools_requiring_approval",
    "default_response_channel",
    "created_at", "updated_at", "last_active_at",
  ]
  lines = ["AGENT CONFIG", ""]
  for f in important_field_order:
    if f in r:
      val = r.get(f)
      val_str = "" if val is None else str(val)
      if len(val_str) > 160:
        val_str = val_str[:160] + "..."
      lines.append(f"  {f:<30} {val_str}")
  endpoint_name = r.get("llm_endpoint") or ""
  if endpoint_name:
    from ragtag.shared_config import get_llm_endpoint_config
    ep_cfg = get_llm_endpoint_config(endpoint_name)
    if ep_cfg:
      base_url = ep_cfg.get("base_url", "")
      caps = ep_cfg.get("capabilities", {})
      cap_list = [k for k, v in caps.items() if v]
      lines.append(f"  {'endpoint_url':<30} {base_url}")
      if cap_list:
        lines.append(f"  {'endpoint_capabilities':<30} [{', '.join(cap_list)}]")
  system_prompt = r.get("system_prompt") or ""
  working_context = r.get("working_context") or ""
  if system_prompt:
    lines.append("")
    lines.append("SYSTEM PROMPT:")
    lines.append(system_prompt[:500] + ("..." if len(system_prompt) > 500 else ""))
  if working_context:
    lines.append("")
    lines.append("WORKING CONTEXT:")
    lines.append(working_context[:500] + ("..." if len(working_context) > 500 else ""))
  return "\n".join(lines)


def _format_get_run_log_result(result_data: Any) -> str:
  """Tabular display of recent runs."""
  if isinstance(result_data, dict) and "runs" in result_data:
    runs = result_data.get("runs") or []
  else:
    runs = _extract_rows_from_sqlite_text_or_list(result_data)
  if not runs:
    return "RUNS: (none yet)"
  lines = [f"RUNS ({len(runs)}):", ""]
  for run in runs[:50]:
    run_id = run.get("run_id", "?")[:12]
    event_type = run.get("event_type", "?")[:14]
    started = (run.get("started_at") or "")[:19]
    completed = (run.get("completed_at") or "-")[:19]
    status = run.get("status", "?")
    llm = run.get("llm_calls_made", 0)
    tc = run.get("tool_calls_made", 0)
    tok = run.get("tokens_consumed", 0)
    lines.append(f"  {run_id}  {event_type:<14}  {started} → {completed}  {status:<10}  llm={llm} tools={tc} tok={tok}")
  return "\n".join(lines)


def _format_get_memory_result(result_data: Any) -> str:
  """List memories with id, type, importance, and preview."""
  if isinstance(result_data, dict) and "memories" in result_data:
    memories = result_data.get("memories") or []
  else:
    memories = _extract_rows_from_sqlite_text_or_list(result_data)
  if not memories:
    return "MEMORIES: (none)"
  lines = [f"MEMORIES ({len(memories)}):", ""]
  for m in memories[:50]:
    mid = m.get("memory_id", "?")
    mtype = m.get("memory_type", "?")
    importance = m.get("importance_score", "")
    content = (m.get("content") or "")[:160]
    lines.append(f"  [{mid}] {mtype} imp={importance}  {content}")
  return "\n".join(lines)


def _format_status_result(result_data: Any) -> str:
  """Kernel status summary. The `status` op returns nested JSON."""
  if isinstance(result_data, str):
    return f"STATUS:\n{result_data[:2000]}"
  if not isinstance(result_data, dict):
    return f"STATUS: {str(result_data)[:2000]}"
  kernel_version = result_data.get("kernel_schema_version", "?")
  db = result_data.get("database", "?")
  lines = ["KERNEL STATUS", ""]
  lines.append(f"  schema version: {kernel_version}")
  lines.append(f"  database:       {db}")
  agents_raw = result_data.get("agents", "")
  try:
    agents_parsed = json.loads(agents_raw) if isinstance(agents_raw, str) else agents_raw
  except (json.JSONDecodeError, TypeError):
    agents_parsed = None
  agent_rows = _extract_rows_from_sqlite_text_or_list(agents_parsed) if agents_parsed else []
  lines.append(f"  agents:         {len(agent_rows)}")
  for row in agent_rows[:10]:
    aid = row.get("agent_id", "?")
    cur = row.get("current_state", "?")
    p = " PAUSED" if row.get("is_paused") else ""
    dn = row.get("display_name", "")
    lines.append(f"    - {aid}  [{cur}{p}]  \"{dn}\"")
  pending_raw = result_data.get("pending_events", "")
  dead_raw = result_data.get("dead_letters", "")
  try:
    pending_parsed = json.loads(pending_raw) if isinstance(pending_raw, str) else pending_raw
    pending_rows = _extract_rows_from_sqlite_text_or_list(pending_parsed)
    pending_count = int((pending_rows[0].get("pending_event_count") if pending_rows else 0) or 0)
    lines.append(f"  pending events: {pending_count}")
  except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
    lines.append(f"  pending events: ?")
  try:
    dead_parsed = json.loads(dead_raw) if isinstance(dead_raw, str) else dead_raw
    dead_rows = _extract_rows_from_sqlite_text_or_list(dead_parsed)
    dead_count = int((dead_rows[0].get("dead_letter_count") if dead_rows else 0) or 0)
    lines.append(f"  dead letters:   {dead_count}")
  except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
    lines.append(f"  dead letters:   ?")
  return "\n".join(lines)


def _format_list_event_sources_result(result_data: Any) -> str:
  """List event sources (cron, telegram, etc.)."""
  rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "EVENT SOURCES: (none)"
  lines = [f"EVENT SOURCES ({len(rows)}):", ""]
  for row in rows[:50]:
    sid = row.get("source_id", "?")
    stype = row.get("source_type", "?")
    aid = row.get("agent_id", "?")
    enabled = "on" if row.get("is_enabled") else "off"
    pri = row.get("priority", "?")
    qm = row.get("queue_mode", "?")
    cfg_raw = row.get("config", "")
    cfg_preview = (cfg_raw or "")[:90]
    lines.append(f"  {sid}  {stype:<14}  agent={aid}  {enabled}  pri={pri} qm={qm}")
    if cfg_preview:
      lines.append(f"     config: {cfg_preview}")
  return "\n".join(lines)


def _format_get_pending_approvals_result(result_data: Any) -> str:
  """List pending human-approval requests (live + orphaned from a dead process)."""
  orphaned = []
  if isinstance(result_data, dict) and "pending_approvals" in result_data:
    pending = result_data.get("pending_approvals") or []
    orphaned = result_data.get("orphaned_approvals") or []
  elif isinstance(result_data, list):
    pending = result_data
  else:
    pending = _extract_rows_from_sqlite_text_or_list(result_data)
  if not pending and not orphaned:
    return "PENDING APPROVALS: (none)"
  lines = [f"PENDING APPROVALS ({len(pending)}):", ""]
  for p in pending:
    if not isinstance(p, dict):
      continue
    apid = p.get("approval_request_id", "?")
    agent_id = p.get("agent_id", "?")
    tool = p.get("tool_name", "?")
    lines.append(f"  {apid}  agent={agent_id}  tool={tool}")
  if orphaned:
    lines.append("")
    lines.append(f"ORPHANED (pending when a previous server process stopped; run no longer waiting) ({len(orphaned)}):")
    for p in orphaned:
      if not isinstance(p, dict):
        continue
      lines.append(f"  {p.get('approval_request_id', '?')}  agent={p.get('agent_id', '?')}  tool={p.get('tool_name', '?')}")
  lines.append("")
  lines.append("Use option 2 (Approve) or 3 (Deny) with the approval_request_id.")
  return "\n".join(lines)


def _format_get_dlq_result(result_data: Any) -> str:
  """Dead letter queue entries list."""
  if isinstance(result_data, dict) and "dlq_entries" in result_data:
    rows = result_data.get("dlq_entries") or []
  else:
    rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "DEAD LETTER QUEUE: (none pending)"
  lines = [f"DEAD LETTER QUEUE ({len(rows)}):", ""]
  for row in rows[:50]:
    dlq_id = row.get("dlq_id", "?")
    agent_id = row.get("agent_id", "?")
    status = row.get("status", "?")
    reason = (row.get("failure_reason") or "")[:80]
    lines.append(f"  [{dlq_id}] agent={agent_id}  status={status}  reason={reason}")
  return "\n".join(lines)


def _format_get_session_log_result(result_data: Any) -> str:
  """Recent session log entries."""
  rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "SESSION LOG: (empty)"
  lines = [f"SESSION LOG ({len(rows)} entries):", ""]
  for row in rows[:50]:
    eid = row.get("entry_id", "?")
    rid = (row.get("run_id") or "-")[:12]
    etype = row.get("entry_type", "?")
    ts = (row.get("created_at") or "")[:19]
    payload_preview = (row.get("payload_json") or "")[:80]
    lines.append(f"  [{eid}] {ts}  run={rid}  {etype:<20}  {payload_preview}")
  return "\n".join(lines)


def _format_get_checkpoints_result(result_data: Any) -> str:
  """Checkpoint summaries for a run."""
  if isinstance(result_data, dict) and "checkpoints" in result_data:
    rows = result_data.get("checkpoints") or []
  else:
    rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "CHECKPOINTS: (none)"
  lines = [f"CHECKPOINTS ({len(rows)}):", ""]
  for row in rows[:50]:
    cid = row.get("checkpoint_id", "?")
    step = row.get("step_number", "?")
    ts = (row.get("created_at") or "")[:19]
    sjp = (row.get("state_json") or "")[:120]
    lines.append(f"  [{cid}] step={step}  {ts}  {sjp}")
  return "\n".join(lines)


def _format_get_history_result(result_data: Any) -> str:
  """Conversation transcript entries."""
  rows = _extract_rows_from_sqlite_text_or_list(result_data)
  if not rows:
    return "HISTORY: (empty)"
  ordered_rows = list(reversed(rows)) if rows and rows[0].get("entry_id", 0) > (rows[-1].get("entry_id", 0) or 0) else rows
  lines = [f"HISTORY ({len(ordered_rows)} entries):", ""]
  for row in ordered_rows[-30:]:
    role = row.get("role", "?")
    ts = (row.get("created_at") or "")[:19]
    content = (row.get("content") or "")[:200]
    lines.append(f"  {ts} [{role}] {content}")
  return "\n".join(lines)


# Map operation name → formatter function. Operations not in the map fall
# back to `_format_generic_success_result`.
ADMIN_OPERATION_RESULT_FORMATTERS: Dict[str, Any] = {
  "list_agents":            _format_list_agents_result,
  "get_agent":              _format_get_agent_result,
  "get_run_log":            _format_get_run_log_result,
  "get_memory":             _format_get_memory_result,
  "status":                 _format_status_result,
  "list_event_sources":     _format_list_event_sources_result,
  "get_pending_approvals":  _format_get_pending_approvals_result,
  "get_dlq":                _format_get_dlq_result,
  "get_session_log":        _format_get_session_log_result,
  "get_checkpoints":        _format_get_checkpoints_result,
  "get_history":            _format_get_history_result,
}


# ───── Admin.7: Guided input definitions and collector ─────────────────────────
#
# Each guided input is a multi-step state machine:
#   - steps: ordered list of {param_name, prompt, choices?, validate?, default_on_skip?}
#   - build_params: function (collected_values, state) → MCP op params dict
#   - confirm_prompt (optional): final confirmation line shown before execution
#
# The executor hands raw menu input to `_process_admin_guided_input_step` when
# `state["pending_guided_input"]` is set. Steps without `choices` accept free
# text; steps with `choices` require a number that maps to a value.
# ─────────────────────────────────────────────────────────────────────────────


def _build_llm_provider_choice_prompt() -> str:
  """Build the provider-selection prompt showing configured endpoints."""
  from ragtag.shared_config import list_all_llm_endpoints
  endpoints = list_all_llm_endpoints()
  if not endpoints:
    return "No LLM endpoints configured. Add endpoints in settings[0].llm_endpoints."
  lines = ["Choose an endpoint:"]
  for i, ep in enumerate(endpoints, 1):
    name = ep.get("endpoint_name", "?")
    desc = ep.get("description", ep.get("provider_type", ""))
    lines.append(f"  {i}. {name} ({desc})")
  return "\n".join(lines)


def _build_context_mode_choice_prompt() -> str:
  return "Choose a context mode:\n  1. raw (kernel manages context)\n  2. harnessed (external harness manages context)"


def _build_memory_type_choice_prompt() -> str:
  return (
    "Choose a memory type:\n"
    "  1. fact\n  2. preference\n  3. project_knowledge\n"
    "  4. decision\n  5. task\n  6. rule"
  )


def _build_priority_choice_prompt() -> str:
  return "Choose a priority:\n  1. high\n  2. normal\n  3. low"


def _build_queue_mode_choice_prompt() -> str:
  return "Choose a queue mode:\n  1. queue (FIFO, default)\n  2. collect (2s coalesce)\n  3. drop (if busy)\n  4. preempt (next after current)"


ADMIN_GUIDED_INPUT_DEFINITIONS: Dict[str, Dict[str, Any]] = {
  "create_agent": {
    "title": "CREATE AGENT",
    "steps": [
      {"param_name": "display_name", "prompt": "Enter a display name for the new agent:"},
      {"param_name": "system_prompt", "prompt": "Enter the system prompt (the agent's persona and instructions):"},
      {"param_name": "llm_endpoint", "prompt": _build_llm_provider_choice_prompt,
       "choices": _build_dynamic_endpoint_choice_map,
       "default_on_skip": "local-mlx"},
    ],
  },
  "delete_agent": {
    "title": "DELETE AGENT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "delete_history_choice",
       "prompt": "Also delete conversation history and memories?\n  1. No (preserve history)\n  2. Yes (delete everything)",
       "choices": {"1": "false", "2": "true"},
       "default_on_skip": "false"},
      {"param_name": "confirmation_text",
       "prompt": "Type the agent_id to confirm deletion (or 'cancel' to abort):"},
    ],
  },
  "interrupt_agent": {
    "title": "INTERRUPT AGENT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "reason", "prompt": "Reason for interrupt (or blank for 'manual interrupt'):"},
    ],
  },
  "change_llm_provider": {
    "title": "CHANGE ENDPOINT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "llm_endpoint", "prompt": _build_llm_provider_choice_prompt,
       "choices": _build_dynamic_endpoint_choice_map},
    ],
  },
  "change_llm_model": {
    "title": "CHANGE LLM MODEL",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "llm_model", "prompt": "Enter the new LLM model identifier (e.g. 'cnd/Qwen3.5-35B-A3B-mlx-vlm-mxfp4', 'qwen', 'google/gemma-3-4b-it:free'):"},
    ],
  },
  "change_compaction_model": {
    "title": "CHANGE COMPACTION MODEL",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "compaction_model", "prompt": "Enter the compaction/reflection model name:"},
    ],
  },
  "change_context_mode": {
    "title": "CHANGE CONTEXT MODE",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "context_mode", "prompt": _build_context_mode_choice_prompt(),
       "choices": ADMIN_CONTEXT_MODE_CHOICE_MAP},
    ],
  },
  "change_model_fallback_chain": {
    "title": "SET FALLBACK CHAIN",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "model_fallback_chain",
       "prompt": "Enter fallback chain as JSON array of [provider, model] pairs (or [] for none).\nExample: [[\"custom\",\"qwen\"],[\"openrouter\",\"google/gemma-3-4b-it:free\"]]"},
    ],
  },
  "edit_working_context": {
    "title": "EDIT WORKING CONTEXT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "working_context", "prompt": "Enter the new working_context text (Tier 1 core memory) for the active agent:"},
    ],
  },
  "search_memory": {
    "title": "SEARCH MEMORY",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "query", "prompt": "Enter a search query (semantic):"},
    ],
  },
  "add_memory": {
    "title": "ADD MEMORY",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "content", "prompt": "Enter the memory content:"},
      {"param_name": "memory_type", "prompt": _build_memory_type_choice_prompt(),
       "choices": ADMIN_MEMORY_TYPE_CHOICE_MAP,
       "default_on_skip": "fact"},
    ],
  },
  "delete_memory": {
    "title": "DELETE MEMORY",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "memory_id", "prompt": "Enter the memory_id to delete:"},
    ],
  },
  "add_cron_source": {
    "title": "ADD CRON EVENT SOURCE",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "schedule", "prompt": "Enter a cron schedule (e.g. '0 9 * * *' for 9am daily):"},
      {"param_name": "trigger_message", "prompt": "Enter the message text to deliver to the agent when it fires:"},
      {"param_name": "priority", "prompt": _build_priority_choice_prompt(),
       "choices": ADMIN_PRIORITY_CHOICE_MAP,
       "default_on_skip": "normal"},
    ],
  },
  "add_telegram_source": {
    "title": "ADD TELEGRAM EVENT SOURCE",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "bot_token_config_key",
       "prompt": "Enter the config key holding the bot token (e.g. 'telegram.bot_token_default'):"},
      {"param_name": "allowed_chat_ids_raw",
       "prompt": "Enter allowed chat IDs as JSON array (e.g. '[12345, 67890]'), or [] for any:"},
      {"param_name": "priority", "prompt": _build_priority_choice_prompt(),
       "choices": ADMIN_PRIORITY_CHOICE_MAP,
       "default_on_skip": "normal"},
      {"param_name": "queue_mode", "prompt": _build_queue_mode_choice_prompt(),
       "choices": ADMIN_QUEUE_MODE_CHOICE_MAP,
       "default_on_skip": "queue"},
    ],
  },
  "remove_event_source": {
    "title": "REMOVE EVENT SOURCE",
    "steps": [
      {"param_name": "source_id", "prompt": "Enter the source_id to remove (see option 1 to list):"},
    ],
  },
  "approve_action": {
    "title": "APPROVE ACTION",
    "steps": [
      {"param_name": "approval_request_id", "prompt": "Enter the approval_request_id (see option 1 for pending list):"},
    ],
  },
  "deny_action": {
    "title": "DENY ACTION",
    "steps": [
      {"param_name": "approval_request_id", "prompt": "Enter the approval_request_id:"},
      {"param_name": "reason", "prompt": "Enter a reason (or blank for 'operator denied'):"},
    ],
  },
  "retry_dlq": {
    "title": "RETRY DLQ ENTRY",
    "steps": [
      {"param_name": "dlq_id", "prompt": "Enter the dlq_id to retry:"},
    ],
  },
  "discard_dlq": {
    "title": "DISCARD DLQ ENTRY",
    "steps": [
      {"param_name": "dlq_id", "prompt": "Enter the dlq_id to discard:"},
    ],
  },
  "respond_to_user_request": {
    "title": "RESPOND TO AGENT ASK_USER",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "response_text", "prompt": "Enter the response text to deliver to the waiting agent:"},
    ],
  },
  "get_checkpoints": {
    "title": "GET CHECKPOINTS FOR RUN",
    "steps": [
      {"param_name": "run_id", "prompt": "Enter the run_id whose checkpoints you want to see:"},
    ],
  },
  "approve_contact": {
    "title": "APPROVE CONTACT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "contact_id", "prompt": "Enter the contact_id to approve (see 'Contacts — pending requests' for the list):"},
    ],
  },
  "block_contact": {
    "title": "BLOCK CONTACT",
    "requires_active_agent": True,
    "steps": [
      {"param_name": "contact_id", "prompt": "Enter the contact_id to block:"},
    ],
  },
}


def _start_guided_input_flow(state: Dict[str, Any], definition_key: str, action: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
  """Initialize pending_guided_input for the given definition key and return first prompt."""
  definition = ADMIN_GUIDED_INPUT_DEFINITIONS.get(definition_key)
  if not definition:
    return state, f"Admin error: no guided input definition for '{definition_key}'. Returning to menu.\n\n" + _render_admin_menu_as_plain_text(_resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE, state)
  state["pending_guided_input"] = {
    "definition_key": definition_key,
    "action": action,
    "step_index": 0,
    "collected_values": {},
  }
  first_step = definition["steps"][0]
  title = definition.get("title", definition_key.upper())
  total_steps = len(definition["steps"])

  prompt_text = first_step["prompt"]
  if callable(prompt_text):
    prompt_text = prompt_text()

  current_value_line = ""
  active_id = state.get("active_agent_id")
  if active_id and first_step.get("param_name"):
    agent_cfg = _extract_agent_config_as_dict(active_id)
    if agent_cfg:
      current_raw = agent_cfg.get(first_step["param_name"])
      if current_raw is not None and str(current_raw).strip():
        current_value_line = f"Current: {current_raw}\n\n"

  return state, f"{title} (step 1/{total_steps})\n\n{current_value_line}{prompt_text}\n\n(Type 'cancel' at any step to abort.)"


def _build_action_params_from_guided_input(
  operation: str,
  collected_values: Dict[str, Any],
  action: Dict[str, Any],
  state: Dict[str, Any],
) -> Dict[str, Any]:
  """Convert guided-input collected values to the exact params dict for the MCP operation.

  This centralizes any conversion (e.g., JSON array parsing, boolean flags,
  merging with active_agent_id, stitching sub-fields into a `config` dict).
  """
  active_agent_id = state.get("active_agent_id")
  base_params: Dict[str, Any] = dict(action.get("params", {}))

  if operation in {"get_agent", "update_agent", "delete_agent", "interrupt_agent",
                   "send_message", "get_history", "pause_agent", "resume_agent",
                   "compact_context", "reflect_now", "get_memory", "set_memory",
                   "get_run_log", "get_session_log", "respond_to_user_request"} and active_agent_id:
    base_params.setdefault("agent_id", active_agent_id)

  if operation == "create_agent":
    for k in ("display_name", "system_prompt", "llm_provider", "llm_endpoint"):
      if k in collected_values:
        base_params[k] = collected_values[k]
    if "llm_endpoint" in base_params and "llm_provider" not in base_params:
      from ragtag.shared_config import get_llm_endpoint_config
      ep_cfg = get_llm_endpoint_config(base_params["llm_endpoint"])
      if ep_cfg:
        base_params["llm_provider"] = ep_cfg.get("provider_type", "")
        if ep_cfg.get("is_cli_harness", False):
          base_params["context_mode"] = "harnessed"
    return base_params

  if operation == "delete_agent":
    delete_history_flag = collected_values.get("delete_history_choice") == "true"
    base_params["delete_history"] = delete_history_flag
    return base_params

  if operation == "update_agent":
    for field_name in ("llm_provider", "llm_model", "llm_endpoint", "compaction_model",
                       "context_mode", "working_context", "model_fallback_chain"):
      if field_name in collected_values:
        base_params[field_name] = collected_values[field_name]
    if "llm_endpoint" in base_params and "llm_provider" not in base_params:
      from ragtag.shared_config import get_llm_endpoint_config
      ep_cfg = get_llm_endpoint_config(base_params["llm_endpoint"])
      if ep_cfg:
        base_params["llm_provider"] = ep_cfg.get("provider_type", "")
    return base_params

  if operation == "interrupt_agent":
    reason = collected_values.get("reason") or "manual interrupt"
    base_params["reason"] = reason
    return base_params

  if operation == "get_memory":
    if "query" in collected_values:
      base_params["query"] = collected_values["query"]
    return base_params

  if operation == "set_memory":
    if "content" in collected_values:
      base_params["content"] = collected_values["content"]
    if "memory_type" in collected_values:
      base_params["memory_type"] = collected_values["memory_type"]
    return base_params

  if operation == "delete_memory":
    raw_id = collected_values.get("memory_id", "")
    try:
      base_params["memory_id"] = int(str(raw_id).strip())
    except (ValueError, TypeError):
      base_params["memory_id"] = raw_id
    return base_params

  if operation == "add_event_source":
    definition_key = action.get("guided_input", "")
    if definition_key == "add_cron_source":
      base_params["source_type"] = "cron"
      base_params["config"] = {
        "schedule": collected_values.get("schedule", ""),
        "message": collected_values.get("trigger_message", ""),
      }
      base_params["priority"] = collected_values.get("priority", "normal")
    elif definition_key == "add_telegram_source":
      base_params["source_type"] = "telegram"
      allowed_raw = collected_values.get("allowed_chat_ids_raw", "[]")
      try:
        allowed = json.loads(allowed_raw) if isinstance(allowed_raw, str) else allowed_raw
        if not isinstance(allowed, list):
          allowed = []
      except (json.JSONDecodeError, TypeError):
        allowed = []
      base_params["config"] = {
        "bot_token_config_key": collected_values.get("bot_token_config_key", ""),
        "allowed_chat_ids": allowed,
      }
      base_params["priority"] = collected_values.get("priority", "normal")
      base_params["queue_mode"] = collected_values.get("queue_mode", "queue")
    return base_params

  if operation == "remove_event_source":
    base_params["source_id"] = collected_values.get("source_id", "").strip()
    return base_params

  if operation == "approve_action":
    base_params["approval_request_id"] = collected_values.get("approval_request_id", "").strip()
    return base_params

  if operation == "deny_action":
    base_params["approval_request_id"] = collected_values.get("approval_request_id", "").strip()
    reason = collected_values.get("reason") or "operator denied"
    base_params["reason"] = reason
    return base_params

  if operation in {"retry_dlq", "discard_dlq"}:
    raw_id = collected_values.get("dlq_id", "")
    try:
      base_params["dlq_id"] = int(str(raw_id).strip())
    except (ValueError, TypeError):
      base_params["dlq_id"] = raw_id
    return base_params

  if operation == "respond_to_user_request":
    base_params["response_text"] = collected_values.get("response_text", "")
    return base_params

  if operation == "get_checkpoints":
    base_params["run_id"] = collected_values.get("run_id", "").strip()
    return base_params

  base_params.update({k: v for k, v in collected_values.items() if v is not None})
  return base_params


def _process_admin_guided_input_step(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Process a single input while a guided flow is active.

  Returns (new_state, response_text). If the flow completes, pending_guided_input
  is cleared on the returned state.  If the user cancels, the flow aborts and
  we return the current submenu text.
  """
  pending = state.get("pending_guided_input")
  if not pending:
    return state, "Internal error: guided input expected but state is empty."

  definition_key = pending.get("definition_key", "")
  definition = ADMIN_GUIDED_INPUT_DEFINITIONS.get(definition_key)
  if not definition:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, f"Unknown guided input definition '{definition_key}'. Returning to menu.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  steps = definition["steps"]
  step_index = pending.get("step_index", 0)
  if step_index >= len(steps):
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Guided input already complete.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)

  if normalized in ADMIN_CANCEL_NORMALIZED_FORMS:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  current_step = steps[step_index]
  param_name = current_step["param_name"]
  choices_map_or_callable = current_step.get("choices")
  choices_map = choices_map_or_callable() if callable(choices_map_or_callable) else choices_map_or_callable
  raw_value = (raw_text or "").strip()

  if choices_map:
    if normalized in choices_map:
      resolved_value = choices_map[normalized]
    elif normalized == "" and "default_on_skip" in current_step:
      resolved_value = current_step["default_on_skip"]
    else:
      total_steps = len(steps)
      step_prompt = current_step["prompt"]
      if callable(step_prompt):
        step_prompt = step_prompt()
      return state, (
        f"'{raw_text.strip()}' is not a valid choice. Please enter a number from the list "
        f"(1-{len(choices_map)}), or type 'cancel'.\n\n"
        f"{definition.get('title', definition_key.upper())} (step {step_index + 1}/{total_steps})\n\n"
        f"{step_prompt}"
      )
    pending["collected_values"][param_name] = resolved_value
  else:
    if not raw_value and "default_on_skip" in current_step:
      pending["collected_values"][param_name] = current_step["default_on_skip"]
    else:
      if param_name in ("llm_model", "compaction_model") and raw_value.isdigit() and _last_listed_model_ids_sorted:
        idx = int(raw_value) - 1
        if 0 <= idx < len(_last_listed_model_ids_sorted):
          raw_value = _last_listed_model_ids_sorted[idx]
      pending["collected_values"][param_name] = raw_value

  next_step_index = step_index + 1
  if next_step_index < len(steps):
    pending["step_index"] = next_step_index
    state["pending_guided_input"] = pending
    next_step = steps[next_step_index]
    total_steps = len(steps)
    next_prompt = next_step["prompt"]
    if callable(next_prompt):
      next_prompt = next_prompt()
    return state, f"{definition.get('title', definition_key.upper())} (step {next_step_index + 1}/{total_steps})\n\n{next_prompt}"

  action = pending.get("action", {})
  operation = action.get("operation", "")
  collected = pending.get("collected_values", {})

  if operation == "delete_agent":
    active_id = state.get("active_agent_id") or ""
    if collected.get("confirmation_text", "").strip() != active_id:
      state["pending_guided_input"] = None
      current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
      return state, "Deletion cancelled: typed id did not match active agent id.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  op_params = _build_action_params_from_guided_input(operation, collected, action, state)

  handler_fn = OPERATION_DISPATCH_TABLE.get(operation)
  if not handler_fn:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, f"This operation ('{operation}') is no longer available in this build.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  try:
    mcp_response = handler_fn(op_params)
  except Exception as handler_error:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, f"Operation '{operation}' failed: {handler_error}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  state["pending_guided_input"] = None

  formatted_result = _format_mcp_response_for_admin_display(operation, mcp_response, state)

  if operation == "create_agent" and not mcp_response.get("isError", True):
    try:
      created = json.loads(_extract_text_from_mcp_response(mcp_response))
      new_agent_id = created.get("agent_id")
      if new_agent_id:
        state["active_agent_id"] = new_agent_id
        formatted_result += f"\n\nSet active agent: {new_agent_id}"
    except (json.JSONDecodeError, TypeError):
      pass

  if operation == "delete_agent" and not mcp_response.get("isError", True):
    state["active_agent_id"] = None
    state["active_session_id"] = None

  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
  return state, f"{formatted_result}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


# ───── Admin.6: Operation executor ─────────────────────────────────────────────


def _format_admin_list_providers_plain_text(result: Dict) -> str:
  """Format the llm.list_providers MCP response as plain text for admin display."""
  if result.get("isError"):
    return f"Error: {_extract_text_from_mcp_response(result)[:300]}"
  try:
    data = json.loads(_extract_text_from_mcp_response(result))
    providers = data.get("providers", [])
    if not providers:
      return "No providers found."
    lines = ["AVAILABLE PROVIDERS:", ""]
    for i, prov in enumerate(providers, 1):
      name = prov.get("name", prov.get("id", "?"))
      avail = prov.get("available")
      status_indicator = ""
      if avail is True:
        status_indicator = " (available)"
      elif avail is False:
        status_indicator = " (not installed)"
      feature_tags = []
      if prov.get("streaming"):
        feature_tags.append("stream")
      if prov.get("tool_calling"):
        feature_tags.append("tools")
      if prov.get("requires_api_key"):
        feature_tags.append("needs API key")
      feature_suffix = f" [{', '.join(feature_tags)}]" if feature_tags else ""
      lines.append(f"  {i}. {name}{status_indicator}{feature_suffix}")
      desc = prov.get("description", "")
      if desc:
        lines.append(f"     {desc[:80]}")
    return "\n".join(lines)
  except Exception as format_error:
    return f"Error formatting providers: {format_error}"


# Cross-reload shared state; mutated in place (slice assignment) so the same
# list object survives importlib.reload() and numeric model picks keep working.
_last_listed_model_ids_sorted: List[str] = _get_phase2_shared_state()['last_listed_model_ids_sorted']


def _format_admin_list_models_plain_text(result: Dict, provider: str) -> str:
  """Format the llm.list_models MCP response as plain text for admin display.

  Sorts models alphabetically. Stores the sorted list in _last_listed_model_ids_sorted
  so that 'change model' can accept a number to pick from this list.
  """
  if result.get("isError"):
    return f"Error listing models for '{provider}': {_extract_text_from_mcp_response(result)[:300]}"
  try:
    data = json.loads(_extract_text_from_mcp_response(result))
    models = data.get("models", [])
    if not models:
      return f"No models found for provider '{provider}'."
    models_sorted = sorted(models, key=lambda m: (m.get("id") or m.get("name") or "").lower())
    total_model_count = len(models_sorted)
    max_display_count = 80
    display_count = min(max_display_count, total_model_count)
    list_was_truncated = total_model_count > max_display_count
    _last_listed_model_ids_sorted[:] = [m.get("id", m.get("name", "?")) for m in models_sorted[:display_count]]
    lines = [f"MODELS FOR {provider.upper()} ({total_model_count} total, sorted A-Z):", ""]
    for i, model in enumerate(models_sorted[:display_count], 1):
      model_id = model.get("id", model.get("name", "?"))
      detail_suffix = ""
      if model.get("size_gb"):
        detail_suffix = f" ({model['size_gb']}GB)"
      elif model.get("context_length"):
        detail_suffix = f" (ctx:{model['context_length']})"
      elif model.get("description"):
        detail_suffix = f" - {model['description'][:40]}"
      lines.append(f"  {i}. {model_id}{detail_suffix}")
    if list_was_truncated:
      lines.append(f"\n  (Showing first {display_count} of {total_model_count})")
      if "openrouter" in provider.lower():
        lines.append("  Use 'search models' or menu option 5 for semantic search across all models.")
    lines.append("\n  Tip: Use 'change model' then enter a number from this list.")
    return "\n".join(lines)
  except Exception as format_error:
    return f"Error formatting models for '{provider}': {format_error}"


def _format_admin_search_models_plain_text(result: Dict, query: str) -> str:
  """Format the llm.search_models MCP response as plain text for admin display."""
  if result.get("isError"):
    return f"Error: {_extract_text_from_mcp_response(result)[:300]}"
  try:
    data = json.loads(_extract_text_from_mcp_response(result))
    rows = data.get("data_rows_from_result_set", [])
    if not rows:
      return f"No models found matching '{query}'."
    lines = [f"SEARCH RESULTS for '{query}' ({len(rows)} matches):", ""]
    for i, model in enumerate(rows, 1):
      model_id = model.get("id", "?")
      cosine_distance = model.get("similarity", 0)
      ctx = model.get("context_length")
      context_length_suffix = f" (ctx:{ctx})" if ctx else ""
      similarity_as_percent = max(0, int((1 - cosine_distance) * 100))
      lines.append(f"  {i}. {model_id}{context_length_suffix} [{similarity_as_percent}% match]")
    return "\n".join(lines)
  except Exception as format_error:
    return f"Error formatting search results: {format_error}"


def _format_mcp_response_for_admin_display(operation: str, mcp_response: Dict[str, Any], state: Dict[str, Any]) -> str:
  """Extract + format an MCP handler response for display in the admin menu.

  Uses a per-operation formatter when present; falls back to the generic
  JSON-print formatter.  On error responses, returns the raw error text so
  the operator can see what went wrong.
  """
  text_body = _extract_text_from_mcp_response(mcp_response)
  if mcp_response.get("isError"):
    return f"{operation}: ERROR\n{text_body[:2000]}"

  parsed: Any
  try:
    parsed = json.loads(text_body) if text_body else {}
  except (json.JSONDecodeError, TypeError):
    parsed = text_body

  formatter = ADMIN_OPERATION_RESULT_FORMATTERS.get(operation)
  if formatter is None:
    return _format_generic_success_result(operation, parsed)

  try:
    import inspect as _inspect
    sig = _inspect.signature(formatter)
    if len(sig.parameters) >= 2:
      return formatter(parsed, state)
    return formatter(parsed)
  except Exception as format_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Admin formatter for '{operation}' failed: {format_error}")
    return _format_generic_success_result(operation, parsed)


def _verify_active_agent_still_exists(state: Dict[str, Any]) -> bool:
  """If active_agent_id is set, verify the agent still exists; clear it otherwise.

  Returns True when the current active_agent_id is either unset or valid.
  Returns False after clearing a stale id (caller can inform the operator).
  """
  active_id = state.get("active_agent_id")
  if not active_id:
    return True
  row_result = _call_sqlite(
    "SELECT agent_id FROM agents WHERE agent_id = :agent_id",
    database=AGENT_KERNEL_DATABASE_NAME,
    bindings={"agent_id": active_id},
  )
  rows = _parse_rows_from_mcp_query_response(row_result)
  if not rows:
    state["active_agent_id"] = None
    state["active_session_id"] = None
    return False
  return True


def _execute_admin_menu_action(action: Dict[str, Any], menu_item: Dict[str, Any], state: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
  """Execute a menu leaf action and return (new_state, response_text).

  Action shapes:
    - {"operation": "list_agents", "params": {...}}  → direct call
    - {"operation": "update_agent", "guided_input": "change_llm_provider"}
    - {"type": "select_agent" | "toggle_pause" | ...} → special handling
  """
  if menu_item.get("requires_active_agent") and not state.get("active_agent_id"):
    return state, ("This action requires an active agent. Go to 1. Agents → 2. Select active agent first.\n\n"
                   + _render_admin_menu_as_plain_text(
                       _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE, state))

  if state.get("active_agent_id") and not _verify_active_agent_still_exists(state):
    return state, ("The previously-selected agent no longer exists. Active agent cleared.\n\n"
                   + _render_admin_menu_as_plain_text(
                       _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE, state))

  special_type = action.get("type")

  if special_type == "select_agent":
    list_result = handle_list_agents({})
    try:
      list_data = json.loads(_extract_text_from_mcp_response(list_result))
    except (json.JSONDecodeError, TypeError):
      list_data = []
    rows = _extract_rows_from_sqlite_text_or_list(list_data)
    if not rows:
      return state, "No agents exist yet. Use option 4 (Create new agent) to create one."
    state["pending_guided_input"] = {
      "definition_key": "__select_agent_inline__",
      "action": {"type": "select_agent_complete", "rows": rows},
      "step_index": 0,
      "collected_values": {},
    }
    lines = ["SELECT ACTIVE AGENT", ""]
    for idx, row in enumerate(rows, start=1):
      aid = row.get("agent_id", "?")
      disp = row.get("display_name", "")
      cur = row.get("current_state", "?")
      lines.append(f"  {idx}. {aid}  [{cur}]  \"{disp}\"")
    lines.append("")
    lines.append("Type the number of the agent to select (or 'cancel' to abort):")
    return state, "\n".join(lines)

  if special_type == "toggle_pause":
    active_id = state.get("active_agent_id")
    agent_row = _extract_agent_config_as_dict(active_id) if active_id else None
    if agent_row is None:
      return state, f"Agent '{active_id}' not found."
    is_paused = bool(agent_row.get("is_paused"))
    if is_paused:
      result = handle_resume_agent({"agent_id": active_id})
    else:
      result = handle_pause_agent({"agent_id": active_id})
    op_label = "resume_agent" if is_paused else "pause_agent"
    formatted = _format_mcp_response_for_admin_display(op_label, result, state)
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "show_active_session":
    sid = state.get("active_session_id")
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    msg = f"Active session id: {sid}" if sid else "No active session id set (a new one will be generated on next send_message)."
    return state, msg + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "new_session":
    state["active_session_id"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Active session cleared. The next send_message will create a fresh session.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "set_active_session":
    state["pending_guided_input"] = {
      "definition_key": "__set_active_session_inline__",
      "action": {"type": "set_active_session_complete"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, "SET ACTIVE SESSION\n\nEnter a session_id to use for subsequent admin-issued messages (or 'cancel' to abort):"

  if special_type == "list_event_sources_for_active_agent":
    active_id = state.get("active_agent_id")
    params = {"agent_id": active_id} if active_id else {}
    result = handle_list_event_sources(params)
    formatted = _format_mcp_response_for_admin_display("list_event_sources", result, state)
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "today_cost":
    active_id = state.get("active_agent_id")
    if not active_id:
      current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
      return state, "Select an agent first to see its cost for today.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    import datetime as _dt
    today_start = _dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
    result = handle_get_run_log({"agent_id": active_id, "since": today_start, "limit": 500})
    try:
      parsed = json.loads(_extract_text_from_mcp_response(result))
    except (json.JSONDecodeError, TypeError):
      parsed = {}
    runs = parsed.get("runs", []) if isinstance(parsed, dict) else []
    total_tokens = sum(int(r.get("tokens_consumed", 0) or 0) for r in runs if isinstance(r, dict))
    total_llm = sum(int(r.get("llm_calls_made", 0) or 0) for r in runs if isinstance(r, dict))
    total_tool = sum(int(r.get("tool_calls_made", 0) or 0) for r in runs if isinstance(r, dict))
    completed = sum(1 for r in runs if isinstance(r, dict) and r.get("status") == "completed")
    failed = sum(1 for r in runs if isinstance(r, dict) and r.get("status") == "failed")
    cfg = _extract_agent_config_as_dict(active_id) or {}
    limit_tokens = cfg.get("max_tokens_per_day", 0) or 0
    lines = [f"TODAY ({today_start[:10]}) for {active_id}", ""]
    lines.append(f"  runs started:      {len(runs)}")
    lines.append(f"  completed:         {completed}")
    lines.append(f"  failed:            {failed}")
    lines.append(f"  llm calls:         {total_llm}")
    lines.append(f"  tool calls:        {total_tool}")
    lines.append(f"  tokens consumed:   {total_tokens}" + (f" / {limit_tokens}" if limit_tokens else ""))
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "\n".join(lines) + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "show_working_context":
    active_id = state.get("active_agent_id")
    cfg = _extract_agent_config_as_dict(active_id) if active_id else None
    wc = (cfg or {}).get("working_context", "") or ""
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    if not wc:
      return state, "WORKING CONTEXT: (empty)\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    return state, "WORKING CONTEXT:\n" + wc + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "show_tool_permissions":
    active_id = state.get("active_agent_id")
    cfg = _extract_agent_config_as_dict(active_id) if active_id else None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    if not cfg:
      return state, "Agent config not available.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    lines = [f"TOOL PERMISSIONS for {active_id}", ""]
    for field in ("read_tools_allowed", "write_tools_allowed", "tools_requiring_approval"):
      val = cfg.get(field, "[]") or "[]"
      lines.append(f"  {field}: {val}")
    return state, "\n".join(lines) + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "show_rate_limits":
    active_id = state.get("active_agent_id")
    cfg = _extract_agent_config_as_dict(active_id) if active_id else None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    if not cfg:
      return state, "Agent config not available.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    lines = [f"RATE LIMITS for {active_id}", ""]
    for field in ("max_tool_rounds_per_run", "max_run_duration_seconds",
                  "max_tokens_per_day", "max_llm_calls_per_hour", "max_tool_calls_per_hour",
                  "reflection_enabled", "reflection_idle_timeout_minutes"):
      lines.append(f"  {field:<33} {cfg.get(field, '')}")
    return state, "\n".join(lines) + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "show_engine_config":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    cfg = _extract_agent_config_as_dict(active_id) if active_id else None
    if not cfg:
      return state, "Agent config not available.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    lines = [f"ENGINE CONFIG for {active_id}", ""]
    primary_endpoint = cfg.get("llm_endpoint", "") or "(not set)"
    primary_provider = cfg.get("llm_provider", "") or "(not set)"
    primary_model = cfg.get("llm_model", "") or "(default)"
    context_mode = cfg.get("context_mode", "raw")
    lines.append(f"  PRIMARY ENGINE")
    lines.append(f"    endpoint:     {primary_endpoint}")
    lines.append(f"    provider:     {primary_provider}")
    lines.append(f"    model:        {primary_model}")
    lines.append(f"    context mode: {context_mode}")
    ep_cfg = None
    if primary_endpoint and primary_endpoint != "(not set)":
      from ragtag.shared_config import get_llm_endpoint_config
      ep_cfg = get_llm_endpoint_config(primary_endpoint)
      if ep_cfg:
        base_url = ep_cfg.get("base_url", "")
        if base_url:
          lines.append(f"    url:          {base_url}")
        caps = ep_cfg.get("capabilities", {})
        cap_list = [k for k, v in caps.items() if v]
        if cap_list:
          lines.append(f"    capabilities: {', '.join(cap_list)}")
    lines.append("")
    comp_endpoint = cfg.get("compaction_endpoint", "") or ""
    comp_provider = cfg.get("compaction_provider", "") or ""
    comp_model = cfg.get("compaction_model", "") or ""
    if comp_endpoint or comp_model:
      lines.append(f"  COMPACTION ENGINE")
      lines.append(f"    endpoint:     {comp_endpoint or '(same as primary)'}")
      lines.append(f"    provider:     {comp_provider or primary_provider}")
      lines.append(f"    model:        {comp_model or '(same as primary)'}")
    else:
      lines.append(f"  COMPACTION ENGINE: (same as primary)")
    lines.append("")
    fallback_chain_raw = cfg.get("model_fallback_chain", "")
    try:
      fallback_chain = json.loads(fallback_chain_raw) if fallback_chain_raw else []
    except (json.JSONDecodeError, TypeError):
      fallback_chain = []
    if fallback_chain:
      lines.append(f"  FALLBACK CHAIN ({len(fallback_chain)} entries)")
      for i, entry in enumerate(fallback_chain, 1):
        if isinstance(entry, list) and len(entry) >= 2:
          lines.append(f"    {i}. {entry[0]} / {entry[1]}")
        elif isinstance(entry, dict):
          lines.append(f"    {i}. {entry.get('endpoint', entry.get('provider', '?'))} / {entry.get('model', '?')}")
        else:
          lines.append(f"    {i}. {entry}")
    else:
      lines.append(f"  FALLBACK CHAIN: (none)")
    return state, "\n".join(lines) + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type in ("set_primary_engine", "set_compaction_engine"):
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    if not active_id:
      return state, "Select an agent first.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    is_primary_engine_slot = special_type == "set_primary_engine"
    slot_label = "PRIMARY" if is_primary_engine_slot else "COMPACTION"
    inline_key = f"__{special_type}_inline__"
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == inline_key:
      step_index = pending.get("step_index", 0)
      collected = pending.get("collected_values", {})
      user_raw_text = pending.get("user_raw_text", "")
      if step_index == 0:
        from ragtag.shared_config import list_all_llm_endpoints
        endpoints = list_all_llm_endpoints()
        normalized_input = _normalize_admin_input_text_for_stt_tolerance(user_raw_text)
        if normalized_input in ADMIN_CANCEL_NORMALIZED_FORMS:
          state["pending_guided_input"] = None
          return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        if not is_primary_engine_slot and normalized_input in ("0", "same", "primary", "default", ""):
          state["pending_guided_input"] = None
          update_result = _call_sqlite(
            "UPDATE agents SET compaction_endpoint = '', compaction_provider = '', compaction_model = '', updated_at = :now WHERE agent_id = :agent_id",
            database=AGENT_KERNEL_DATABASE_NAME,
            bindings={"now": _iso_now(), "agent_id": active_id}
          )
          if update_result.get("isError"):
            return state, f"Error: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
          return state, f"Compaction engine set to: same as primary.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        endpoint_name = None
        if normalized_input.isdigit():
          idx = int(normalized_input) - 1
          if 0 <= idx < len(endpoints):
            endpoint_name = endpoints[idx].get("endpoint_name")
        if not endpoint_name:
          for ep in endpoints:
            if ep.get("endpoint_name", "").lower() == normalized_input.lower():
              endpoint_name = ep["endpoint_name"]
              break
        if not endpoint_name:
          ep_list = "\n".join(f"  {i}. {ep['endpoint_name']} ({ep.get('description', ep.get('provider_type', ''))})" for i, ep in enumerate(endpoints, 1))
          extra = ""
          if not is_primary_engine_slot:
            extra = "\n  0. Same as primary (default)"
          return state, (f"'{user_raw_text.strip()}' is not a valid endpoint. Choose from:\n{ep_list}{extra}\n\n"
                         f"(Type 'cancel' to abort)")
        collected["endpoint_name"] = endpoint_name
        pending["collected_values"] = collected
        pending["step_index"] = 1
        pending.pop("user_raw_text", None)
        state["pending_guided_input"] = pending
        from ragtag.shared_config import get_llm_endpoint_config
        ep_cfg = get_llm_endpoint_config(endpoint_name)
        is_cli_harness_endpoint = (ep_cfg or {}).get("is_cli_harness", False)
        if is_cli_harness_endpoint:
          return state, (f"SET {slot_label} ENGINE (step 2/2)\n\n"
                         f"Endpoint: {endpoint_name}\n"
                         f"This is a CLI harness — enter model name, or press Enter for default:\n"
                         f"(Type 'cancel' to abort)")
        return state, (f"SET {slot_label} ENGINE (step 2/2)\n\n"
                       f"Endpoint: {endpoint_name}\n"
                       f"Enter model name (or number from a previous model list), or press Enter for default:\n"
                       f"(Type 'cancel' to abort)")
      elif step_index == 1:
        normalized_input = _normalize_admin_input_text_for_stt_tolerance(user_raw_text)
        if normalized_input in ADMIN_CANCEL_NORMALIZED_FORMS:
          state["pending_guided_input"] = None
          return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        model_value = (user_raw_text or "").strip()
        if model_value.isdigit() and _last_listed_model_ids_sorted:
          idx = int(model_value) - 1
          if 0 <= idx < len(_last_listed_model_ids_sorted):
            model_value = _last_listed_model_ids_sorted[idx]
        endpoint_name = collected.get("endpoint_name", "")
        from ragtag.shared_config import get_llm_endpoint_config
        ep_cfg = get_llm_endpoint_config(endpoint_name)
        provider_type = (ep_cfg or {}).get("provider_type", "")
        is_cli_harness_endpoint = (ep_cfg or {}).get("is_cli_harness", False)
        context_mode_value = "harnessed" if is_cli_harness_endpoint else "raw"
        state["pending_guided_input"] = None
        if is_primary_engine_slot:
          update_result = _call_sqlite(
            "UPDATE agents SET llm_endpoint = :endpoint, llm_provider = :provider, llm_model = :model, context_mode = :ctx_mode, updated_at = :now WHERE agent_id = :agent_id",
            database=AGENT_KERNEL_DATABASE_NAME,
            bindings={"endpoint": endpoint_name, "provider": provider_type, "model": model_value, "ctx_mode": context_mode_value, "now": _iso_now(), "agent_id": active_id}
          )
        else:
          update_result = _call_sqlite(
            "UPDATE agents SET compaction_endpoint = :endpoint, compaction_provider = :provider, compaction_model = :model, updated_at = :now WHERE agent_id = :agent_id",
            database=AGENT_KERNEL_DATABASE_NAME,
            bindings={"endpoint": endpoint_name, "provider": provider_type, "model": model_value, "now": _iso_now(), "agent_id": active_id}
          )
        if update_result.get("isError"):
          return state, f"Error: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        mode_note = f" (context mode: {context_mode_value})" if is_primary_engine_slot else ""
        model_display = model_value or "(endpoint default)"
        return state, (f"{slot_label} engine set to: {endpoint_name} / {model_display}{mode_note}\n\n"
                       + _render_admin_menu_as_plain_text(current_menu, state))
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    ep_list = "\n".join(f"  {i}. {ep['endpoint_name']} ({ep.get('description', ep.get('provider_type', ''))})" for i, ep in enumerate(endpoints, 1)) if endpoints else "  (none configured)"
    extra = ""
    if not is_primary_engine_slot:
      extra = "\n  0. Same as primary (default)"
    cfg = _extract_agent_config_as_dict(active_id) or {}
    if is_primary_engine_slot:
      current_val = f"{cfg.get('llm_endpoint', '(not set)')} / {cfg.get('llm_model', '(default)')}"
    else:
      ce = cfg.get("compaction_endpoint", "")
      cm = cfg.get("compaction_model", "")
      current_val = f"{ce} / {cm}" if ce or cm else "(same as primary)"
    state["pending_guided_input"] = {
      "definition_key": inline_key,
      "action": {"type": special_type},
      "step_index": 0,
      "collected_values": {},
    }
    return state, (f"SET {slot_label} ENGINE (step 1/2)\n\n"
                   f"Current: {current_val}\n\n"
                   f"Choose an endpoint:\n{ep_list}{extra}\n\n"
                   f"(Type 'cancel' to abort)")

  if special_type == "show_fallback_chain":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    cfg = _extract_agent_config_as_dict(active_id) if active_id else None
    if not cfg:
      return state, "Agent config not available.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    fallback_chain_raw = cfg.get("model_fallback_chain", "")
    try:
      fallback_chain = json.loads(fallback_chain_raw) if fallback_chain_raw else []
    except (json.JSONDecodeError, TypeError):
      fallback_chain = []
    if not fallback_chain:
      return state, "FALLBACK CHAIN: (empty — no fallback engines configured)\n\nUse option 7 to add entries.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    lines = [f"FALLBACK CHAIN ({len(fallback_chain)} entries)", ""]
    for i, entry in enumerate(fallback_chain, 1):
      if isinstance(entry, list) and len(entry) >= 2:
        lines.append(f"  {i}. {entry[0]} / {entry[1]}")
      elif isinstance(entry, dict):
        lines.append(f"  {i}. {entry.get('endpoint', entry.get('provider', '?'))} / {entry.get('model', '?')}")
      else:
        lines.append(f"  {i}. {entry}")
    return state, "\n".join(lines) + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "add_fallback_entry":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    if not active_id:
      return state, "Select an agent first.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    inline_key = "__add_fallback_entry_inline__"
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == inline_key:
      step_index = pending.get("step_index", 0)
      collected = pending.get("collected_values", {})
      user_raw_text = pending.get("user_raw_text", "")
      normalized_input = _normalize_admin_input_text_for_stt_tolerance(user_raw_text)
      if normalized_input in ADMIN_CANCEL_NORMALIZED_FORMS:
        state["pending_guided_input"] = None
        return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      if step_index == 0:
        from ragtag.shared_config import list_all_llm_endpoints
        endpoints = list_all_llm_endpoints()
        endpoint_name = None
        if normalized_input.isdigit():
          idx = int(normalized_input) - 1
          if 0 <= idx < len(endpoints):
            endpoint_name = endpoints[idx].get("endpoint_name")
        if not endpoint_name:
          for ep in endpoints:
            if ep.get("endpoint_name", "").lower() == normalized_input.lower():
              endpoint_name = ep["endpoint_name"]
              break
        if not endpoint_name:
          ep_list = "\n".join(f"  {i}. {ep['endpoint_name']} ({ep.get('description', ep.get('provider_type', ''))})" for i, ep in enumerate(endpoints, 1))
          return state, (f"'{user_raw_text.strip()}' is not a valid endpoint. Choose from:\n{ep_list}\n\n"
                         f"(Type 'cancel' to abort)")
        collected["endpoint_name"] = endpoint_name
        pending["collected_values"] = collected
        pending["step_index"] = 1
        pending.pop("user_raw_text", None)
        state["pending_guided_input"] = pending
        return state, (f"ADD FALLBACK ENTRY (step 2/2)\n\n"
                       f"Endpoint: {endpoint_name}\n"
                       f"Enter model name (or press Enter for endpoint default):\n"
                       f"(Type 'cancel' to abort)")
      elif step_index == 1:
        model_value = (user_raw_text or "").strip()
        if model_value.isdigit() and _last_listed_model_ids_sorted:
          idx = int(model_value) - 1
          if 0 <= idx < len(_last_listed_model_ids_sorted):
            model_value = _last_listed_model_ids_sorted[idx]
        endpoint_name = collected.get("endpoint_name", "")
        state["pending_guided_input"] = None
        cfg = _extract_agent_config_as_dict(active_id) or {}
        fallback_chain_raw = cfg.get("model_fallback_chain", "")
        try:
          fallback_chain = json.loads(fallback_chain_raw) if fallback_chain_raw else []
        except (json.JSONDecodeError, TypeError):
          fallback_chain = []
        fallback_chain.append([endpoint_name, model_value or ""])
        new_chain_json = json.dumps(fallback_chain)
        update_result = _call_sqlite(
          "UPDATE agents SET model_fallback_chain = :chain, updated_at = :now WHERE agent_id = :agent_id",
          database=AGENT_KERNEL_DATABASE_NAME,
          bindings={"chain": new_chain_json, "now": _iso_now(), "agent_id": active_id}
        )
        if update_result.get("isError"):
          return state, f"Error: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        model_display = model_value or "(endpoint default)"
        return state, (f"Added fallback entry #{len(fallback_chain)}: {endpoint_name} / {model_display}\n\n"
                       + _render_admin_menu_as_plain_text(current_menu, state))
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    ep_list = "\n".join(f"  {i}. {ep['endpoint_name']} ({ep.get('description', ep.get('provider_type', ''))})" for i, ep in enumerate(endpoints, 1)) if endpoints else "  (none configured)"
    state["pending_guided_input"] = {
      "definition_key": inline_key,
      "action": {"type": "add_fallback_entry"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, (f"ADD FALLBACK ENTRY (step 1/2)\n\n"
                   f"Choose an endpoint:\n{ep_list}\n\n"
                   f"(Type 'cancel' to abort)")

  if special_type == "remove_fallback_entry":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    if not active_id:
      return state, "Select an agent first.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    inline_key = "__remove_fallback_entry_inline__"
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == inline_key:
      user_raw_text = pending.get("user_raw_text", "")
      normalized_input = _normalize_admin_input_text_for_stt_tolerance(user_raw_text)
      state["pending_guided_input"] = None
      if normalized_input in ADMIN_CANCEL_NORMALIZED_FORMS:
        return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      cfg = _extract_agent_config_as_dict(active_id) or {}
      fallback_chain_raw = cfg.get("model_fallback_chain", "")
      try:
        fallback_chain = json.loads(fallback_chain_raw) if fallback_chain_raw else []
      except (json.JSONDecodeError, TypeError):
        fallback_chain = []
      if normalized_input == "all":
        new_chain_json = "[]"
        update_result = _call_sqlite(
          "UPDATE agents SET model_fallback_chain = :chain, updated_at = :now WHERE agent_id = :agent_id",
          database=AGENT_KERNEL_DATABASE_NAME,
          bindings={"chain": new_chain_json, "now": _iso_now(), "agent_id": active_id}
        )
        if update_result.get("isError"):
          return state, f"Error: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        return state, "Removed all fallback entries.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      if not normalized_input.isdigit():
        return state, f"'{user_raw_text.strip()}' is not a valid entry number.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      idx = int(normalized_input) - 1
      if idx < 0 or idx >= len(fallback_chain):
        return state, f"Entry number {normalized_input} is out of range (1-{len(fallback_chain)}).\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      removed = fallback_chain.pop(idx)
      new_chain_json = json.dumps(fallback_chain)
      update_result = _call_sqlite(
        "UPDATE agents SET model_fallback_chain = :chain, updated_at = :now WHERE agent_id = :agent_id",
        database=AGENT_KERNEL_DATABASE_NAME,
        bindings={"chain": new_chain_json, "now": _iso_now(), "agent_id": active_id}
      )
      if update_result.get("isError"):
        return state, f"Error: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      if isinstance(removed, list) and len(removed) >= 2:
        removed_desc = f"{removed[0]} / {removed[1]}"
      else:
        removed_desc = str(removed)
      return state, f"Removed fallback entry #{int(normalized_input)}: {removed_desc}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    cfg = _extract_agent_config_as_dict(active_id) or {}
    fallback_chain_raw = cfg.get("model_fallback_chain", "")
    try:
      fallback_chain = json.loads(fallback_chain_raw) if fallback_chain_raw else []
    except (json.JSONDecodeError, TypeError):
      fallback_chain = []
    if not fallback_chain:
      return state, "FALLBACK CHAIN: (empty — nothing to remove)\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    lines = ["REMOVE FALLBACK ENTRY", "", "Current fallback chain:"]
    for i, entry in enumerate(fallback_chain, 1):
      if isinstance(entry, list) and len(entry) >= 2:
        lines.append(f"  {i}. {entry[0]} / {entry[1]}")
      elif isinstance(entry, dict):
        lines.append(f"  {i}. {entry.get('endpoint', entry.get('provider', '?'))} / {entry.get('model', '?')}")
      else:
        lines.append(f"  {i}. {entry}")
    lines.append("")
    lines.append("Enter entry number to remove, 'all' to clear, or 'cancel' to abort:")
    state["pending_guided_input"] = {
      "definition_key": inline_key,
      "action": {"type": "remove_fallback_entry"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, "\n".join(lines)

  if special_type == "list_llm_providers":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    try:
      result = _call_tool(_suffixed_tool_name("llm"), {"input": {
        "operation": "list_providers",
        "tool_unlock_token": "__auto__",
      }})
      formatted = _format_admin_list_providers_plain_text(result)
    except Exception as call_error:
      formatted = f"Error calling list_providers: {call_error}"
    return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "list_models_for_current_provider":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    active_id = state.get("active_agent_id")
    if not active_id:
      return state, "Select an agent first to list models for its provider.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    agent_config = _extract_agent_config_as_dict(active_id)
    if not agent_config:
      return state, f"Agent '{active_id}' not found.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    endpoint_name = agent_config.get("llm_endpoint") or ""
    provider = agent_config.get("llm_provider") or ""
    if not endpoint_name and not provider:
      return state, ("Agent has no llm_endpoint or llm_provider configured. Use Endpoints menu to assign one.\n\n"
                     + _render_admin_menu_as_plain_text(current_menu, state))
    try:
      list_models_params: Dict[str, Any] = {
        "operation": "list_models",
        "tool_unlock_token": "__auto__",
      }
      if endpoint_name:
        list_models_params["endpoint"] = endpoint_name
      if provider:
        list_models_params["provider"] = provider
      _apply_provider_host_params_for_llm_call(provider, list_models_params, endpoint_name)
      result = _call_tool(_suffixed_tool_name("llm"), {"input": list_models_params})
      display_label = endpoint_name or provider
      formatted = _format_admin_list_models_plain_text(result, display_label)
    except Exception as call_error:
      formatted = f"Error listing models for '{endpoint_name or provider}': {call_error}"
    return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "search_openrouter_models":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__search_openrouter_models_inline__":
      query_text = pending.get("collected_values", {}).get("query", "")
      state["pending_guided_input"] = None
      if not query_text:
        return state, "No query provided. Returning to menu.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      try:
        result = _call_tool(_suffixed_tool_name("llm"), {"input": {
          "operation": "search_models",
          "bindings": {"query_vec": {"_embedding_text": query_text}},
          "max_results": 20,
          "tool_unlock_token": "__auto__",
        }})
        formatted = _format_admin_search_models_plain_text(result, query_text)
      except Exception as call_error:
        formatted = f"Error calling search_models: {call_error}"
      return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["pending_guided_input"] = {
      "definition_key": "__search_openrouter_models_inline__",
      "action": {"type": "search_openrouter_models"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, ("SEARCH OPENROUTER MODELS\n\n"
                   "Enter your search query (e.g. 'fast coding model', 'vision model under 10B'):\n"
                   "(Type 'cancel' to abort)")

  if special_type == "list_configured_endpoints":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    try:
      from ragtag.shared_config import list_all_llm_endpoints
      endpoints = list_all_llm_endpoints()
      if not endpoints:
        formatted = "No LLM endpoints configured.\nUse option 2 (Add new endpoint) to create one."
      else:
        lines = [f"CONFIGURED LLM ENDPOINTS ({len(endpoints)} total):\n"]
        for i, ep in enumerate(endpoints, 1):
          name = ep.get("endpoint_name", "?")
          ptype = ep.get("provider_type", "?")
          desc = ep.get("description", "")
          base_url = ep.get("base_url", "")
          key_status = ""
          if ep.get("api_key_ref_name"):
            key_status = " [key: " + ("configured" if ep.get("api_key_configured") else "MISSING") + "]"
          caps = ep.get("capabilities", {})
          cap_list = [k for k, v in caps.items() if v]
          cap_str = f" [{', '.join(cap_list)}]" if cap_list else ""
          lines.append(f"  {i}. {name}")
          lines.append(f"     type: {ptype} | url: {base_url}{key_status}")
          if desc:
            lines.append(f"     {desc}")
          if cap_str:
            lines.append(f"     capabilities:{cap_str}")
          lines.append("")
        formatted = "\n".join(lines)
    except Exception as e:
      formatted = f"Error listing endpoints: {e}"
    return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if special_type == "add_endpoint":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__add_endpoint_inline__":
      collected = pending.get("collected_values", {})
      step_index = pending.get("step_index", 0)
      if step_index == 0:
        return state, ("ADD NEW ENDPOINT\n\n"
                       "Enter endpoint name (e.g. 'my-mlx-server', 'cloud-openrouter'):\n"
                       "(Type 'cancel' to abort)")
      elif step_index == 1:
        return state, ("Provider type (mlx, ollama, openrouter, openai, anthropic, llama_cpp, custom):\n"
                       "(Type 'cancel' to abort)")
      elif step_index == 2:
        return state, ("Base URL (e.g. 'http://192.168.1.100:11434', 'https://openrouter.ai/api/v1'):\n"
                       "(Type 'cancel' to abort)")
      elif step_index == 3:
        name = collected.get("endpoint_name", "")
        ptype = collected.get("provider_type", "")
        base_url = collected.get("base_url", "")
        try:
          from ragtag.shared_config import save_llm_endpoint
          save_llm_endpoint(name, {
            "provider_type": ptype,
            "base_url": base_url,
            "description": "",
            "default_model": "",
            "capabilities": {
              "streaming": True,
              "tool_calling": ptype in ("ollama", "openrouter", "openai", "anthropic"),
              "vision_input": ptype in ("mlx", "ollama", "openrouter", "openai"),
              "audio_input": False,
              "multimodal_output": False,
              "json_mode": ptype in ("ollama", "openrouter", "openai"),
              "system_message": True,
            },
          })
          state["pending_guided_input"] = None
          return state, (f"Endpoint '{name}' created successfully.\n"
                         f"  type: {ptype}\n  url: {base_url}\n\n"
                         + _render_admin_menu_as_plain_text(current_menu, state))
        except Exception as e:
          state["pending_guided_input"] = None
          return state, f"Error saving endpoint: {e}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["pending_guided_input"] = {
      "definition_key": "__add_endpoint_inline__",
      "action": {"type": "add_endpoint"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, ("ADD NEW ENDPOINT\n\n"
                   "Enter endpoint name (e.g. 'my-mlx-server', 'cloud-openrouter'):\n"
                   "(Type 'cancel' to abort)")

  if special_type == "remove_endpoint":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__remove_endpoint_inline__":
      name = pending.get("collected_values", {}).get("endpoint_name", "")
      state["pending_guided_input"] = None
      if not name:
        return state, "No name provided.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      try:
        from ragtag.shared_config import delete_llm_endpoint
        removed = delete_llm_endpoint(name)
        if removed:
          return state, f"Endpoint '{name}' removed.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        else:
          return state, f"Endpoint '{name}' not found.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      except Exception as e:
        return state, f"Error removing endpoint: {e}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["pending_guided_input"] = {
      "definition_key": "__remove_endpoint_inline__",
      "action": {"type": "remove_endpoint"},
      "step_index": 0,
      "collected_values": {},
    }
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    endpoint_list_lines = "\n".join(f"  {i}. {ep.get('endpoint_name', '?')}" for i, ep in enumerate(endpoints, 1))
    return state, (f"REMOVE ENDPOINT\n\nConfigured endpoints:\n{endpoint_list_lines}\n\n"
                   "Enter endpoint number or name to remove:\n(Type 'cancel' to abort)")

  if special_type == "test_endpoint_health":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__test_endpoint_inline__":
      name = pending.get("collected_values", {}).get("endpoint_name", "")
      state["pending_guided_input"] = None
      if not name:
        return state, "No name provided.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      try:
        from ragtag.shared_config import get_llm_endpoint_config
        cfg = get_llm_endpoint_config(name)
        if not cfg:
          return state, f"Endpoint '{name}' not found in config.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
        list_params: Dict[str, Any] = {
          "operation": "list_models",
          "endpoint": name,
          "tool_unlock_token": "__auto__",
        }
        import time as _time
        start_time = _time.time()
        result = _call_tool(_suffixed_tool_name("llm"), {"input": list_params})
        elapsed_ms = int((_time.time() - start_time) * 1000)
        if result.get("isError"):
          error_text = _extract_text_from_mcp_response(result)[:200]
          return state, (f"ENDPOINT HEALTH: {name}\n  Status: FAILED\n  Latency: {elapsed_ms}ms\n"
                         f"  Error: {error_text}\n\n" + _render_admin_menu_as_plain_text(current_menu, state))
        response_text = _extract_text_from_mcp_response(result)
        model_count = response_text.count('"id"')
        return state, (f"ENDPOINT HEALTH: {name}\n  Status: OK\n  Latency: {elapsed_ms}ms\n"
                       f"  Models found: {model_count}\n  URL: {cfg.get('base_url')}\n\n"
                       + _render_admin_menu_as_plain_text(current_menu, state))
      except Exception as e:
        return state, f"Error testing endpoint: {e}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["pending_guided_input"] = {
      "definition_key": "__test_endpoint_inline__",
      "action": {"type": "test_endpoint_health"},
      "step_index": 0,
      "collected_values": {},
    }
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    ep_list = "\n".join(f"  {i}. {ep['endpoint_name']}" for i, ep in enumerate(endpoints, 1)) if endpoints else "  (none configured)"
    return state, (f"TEST ENDPOINT HEALTH\n\nAvailable endpoints:\n{ep_list}\n\n"
                   "Enter endpoint name to test:\n(Type 'cancel' to abort)")

  if special_type == "set_agent_endpoint":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__set_agent_endpoint_inline__":
      name = pending.get("collected_values", {}).get("endpoint_name", "")
      state["pending_guided_input"] = None
      if not name:
        return state, "No name provided.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      active_id = state.get("active_agent_id")
      if not active_id:
        return state, "No active agent.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      from ragtag.shared_config import get_llm_endpoint_config
      cfg = get_llm_endpoint_config(name)
      if not cfg:
        return state, f"Endpoint '{name}' not found.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      provider_type = cfg.get("provider_type", "")
      update_result = _call_sqlite(
        "UPDATE agents SET llm_endpoint = :endpoint, llm_provider = :provider, updated_at = :now WHERE agent_id = :agent_id",
        database=AGENT_KERNEL_DATABASE_NAME,
        bindings={"endpoint": name, "provider": provider_type, "now": _iso_now(), "agent_id": active_id}
      )
      if update_result.get("isError"):
        return state, f"Error updating agent: {_extract_text_from_mcp_response(update_result)[:200]}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
      return state, (f"Agent '{active_id}' now uses endpoint '{name}' (provider: {provider_type}).\n\n"
                     + _render_admin_menu_as_plain_text(current_menu, state))
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    ep_list = "\n".join(f"  {i}. {ep['endpoint_name']} ({ep.get('provider_type','')})" for i, ep in enumerate(endpoints, 1)) if endpoints else "  (none configured)"
    state["pending_guided_input"] = {
      "definition_key": "__set_agent_endpoint_inline__",
      "action": {"type": "set_agent_endpoint"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, (f"SET AGENT ENDPOINT\n\nAvailable endpoints:\n{ep_list}\n\n"
                   "Enter endpoint name to assign:\n(Type 'cancel' to abort)")

  if special_type == "edit_endpoint":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, ("Edit endpoint: use 'Remove' then 'Add' to reconfigure.\n"
                   "Or edit settings[0].llm_endpoints in nativemessaging.json directly.\n\n"
                   + _render_admin_menu_as_plain_text(current_menu, state))

  if special_type == "scan_for_local_endpoints":
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    pending = state.get("pending_guided_input")
    if pending and pending.get("definition_key") == "__scan_endpoints_inline__":
      scope = pending.get("collected_values", {}).get("scan_scope", "localhost")
      state["pending_guided_input"] = None
      scan_result = _scan_for_openai_compatible_endpoints(scope)
      return state, scan_result + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["pending_guided_input"] = {
      "definition_key": "__scan_endpoints_inline__",
      "action": {"type": "scan_for_local_endpoints"},
      "step_index": 0,
      "collected_values": {},
    }
    return state, ("SCAN FOR LOCAL MODEL SERVERS\n\n"
                   "Choose scan scope:\n"
                   "  1. localhost only (fast, checks common ports)\n"
                   "  2. LAN /24 subnet (slower, scans all 254 addresses)\n\n"
                   "Enter 1 or 2:\n(Type 'cancel' to abort)")

  operation = action.get("operation")
  if not operation:
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "This menu item has no action defined.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  guided_input_key = action.get("guided_input")
  if guided_input_key:
    if ADMIN_GUIDED_INPUT_DEFINITIONS.get(guided_input_key, {}).get("requires_active_agent") and not state.get("active_agent_id"):
      current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
      return state, ("Select an active agent first (1. Agents → 2. Select).\n\n"
                     + _render_admin_menu_as_plain_text(current_menu, state))
    return _start_guided_input_flow(state, guided_input_key, action)

  op_params = dict(action.get("params", {}))
  if state.get("active_agent_id") and operation in {
    "get_agent", "update_agent", "delete_agent", "send_message", "get_history",
    "pause_agent", "resume_agent", "interrupt_agent", "compact_context",
    "reflect_now", "get_memory", "set_memory", "get_run_log", "get_session_log",
    "respond_to_user_request",
  }:
    op_params.setdefault("agent_id", state["active_agent_id"])

  handler_fn = OPERATION_DISPATCH_TABLE.get(operation)
  if not handler_fn:
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, f"This operation ('{operation}') is no longer available in this build.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  try:
    mcp_response = handler_fn(op_params)
  except Exception as handler_error:
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, f"Operation '{operation}' failed: {handler_error}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  formatted = _format_mcp_response_for_admin_display(operation, mcp_response, state)
  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
  return state, formatted + "\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


def _process_inline_select_agent_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Complete the inline 'select agent' flow: user typed a number → set active."""
  pending = state.get("pending_guided_input") or {}
  action = pending.get("action") or {}
  rows = action.get("rows", []) or []
  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)

  if normalized in ADMIN_CANCEL_NORMALIZED_FORMS:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Selection cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  try:
    idx = int(normalized)
  except ValueError:
    return state, f"'{raw_text.strip()}' is not a number. Enter a row number, or 'cancel'."

  if idx < 1 or idx > len(rows):
    return state, f"Number must be between 1 and {len(rows)}, got {idx}."

  chosen = rows[idx - 1]
  new_agent_id = chosen.get("agent_id")
  state["active_agent_id"] = new_agent_id
  state["active_session_id"] = None
  state["pending_guided_input"] = None
  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
  return state, f"Active agent set: {new_agent_id}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


def _process_inline_set_active_session_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Complete the inline 'set active session id' flow."""
  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)
  if normalized in ADMIN_CANCEL_NORMALIZED_FORMS:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
  sid = (raw_text or "").strip()
  if not sid:
    return state, "Please enter a non-empty session_id, or 'cancel'."
  state["active_session_id"] = sid
  state["pending_guided_input"] = None
  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
  return state, f"Active session set: {sid}\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


def _process_inline_search_openrouter_models_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Collect the search query then dispatch to the search_openrouter_models handler."""
  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)
  if normalized in ADMIN_CANCEL_NORMALIZED_FORMS:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
  query_text = (raw_text or "").strip()
  if not query_text:
    return state, "Please enter a search query, or 'cancel'."
  state["pending_guided_input"]["collected_values"]["query"] = query_text
  return _execute_admin_menu_action(
    {"type": "search_openrouter_models"},
    {"requires_active_agent": False},
    state,
  )


def _scan_for_openai_compatible_endpoints(scope: str = "localhost") -> str:
  """Probe common ports for OpenAI-compatible /v1/models endpoints.

  scope: "localhost" (fast) or "lan" (scans the /24 subnet of the machine's primary interface).
  Returns a formatted plain-text report of discoveries and auto-created endpoints.
  """
  import socket
  import urllib.request
  import urllib.error

  COMMON_PORTS_FOR_LOCAL_LLM_SERVERS = [1234, 11434, 8080, 8000, 5000, 5001, 4891, 1337, 6767, 30000, 2242, 3000]
  CONNECT_TIMEOUT_SECONDS = 1.5

  def _probe_host_port(host: str, port: int) -> Optional[Dict[str, Any]]:
    """Try GET http://host:port/v1/models with a short timeout. Returns model list or None."""
    url = f"http://{host}:{port}/v1/models"
    try:
      req = urllib.request.Request(url, method="GET")
      with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        models = data.get("data", data.get("models", []))
        return {"host": host, "port": port, "url": f"http://{host}:{port}", "model_count": len(models)}
    except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError, Exception):
      return None

  def _get_local_ip_and_subnet() -> Tuple[str, str]:
    """Get this machine's LAN IP and its /24 prefix."""
    try:
      s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      s.connect(("8.8.8.8", 80))
      local_ip = s.getsockname()[0]
      s.close()
      prefix = ".".join(local_ip.split(".")[:3])
      return local_ip, prefix
    except Exception:
      return "127.0.0.1", "127.0.0"

  lines = ["SCANNING FOR LOCAL MODEL SERVERS...", ""]
  discovered: List[Dict[str, Any]] = []

  hosts_to_scan = ["127.0.0.1", "localhost"]
  local_ip, subnet_prefix = _get_local_ip_and_subnet()
  if local_ip != "127.0.0.1":
    hosts_to_scan.append(local_ip)

  if scope == "lan":
    lines.append(f"Scanning subnet {subnet_prefix}.0/24 on {len(COMMON_PORTS_FOR_LOCAL_LLM_SERVERS)} ports...")
    for last_octet in range(1, 255):
      host = f"{subnet_prefix}.{last_octet}"
      if host in hosts_to_scan:
        continue
      for port in COMMON_PORTS_FOR_LOCAL_LLM_SERVERS:
        result = _probe_host_port(host, port)
        if result:
          discovered.append(result)
  else:
    lines.append(f"Scanning localhost on ports: {', '.join(str(p) for p in COMMON_PORTS_FOR_LOCAL_LLM_SERVERS)}")

  for host in hosts_to_scan:
    for port in COMMON_PORTS_FOR_LOCAL_LLM_SERVERS:
      result = _probe_host_port(host, port)
      if result:
        already_found = any(d["port"] == port and (d["host"] == host or (host == "localhost" and d["host"] == "127.0.0.1")) for d in discovered)
        if not already_found:
          discovered.append(result)

  if not discovered:
    lines.append("")
    lines.append("No OpenAI-compatible model servers found.")
    lines.append(f"Checked {len(COMMON_PORTS_FOR_LOCAL_LLM_SERVERS)} ports" + (f" on {subnet_prefix}.0/24" if scope == "lan" else " on localhost") + ".")
    lines.append("")
    lines.append("To start a local server:")
    lines.append("  MLX:       mlx_vlm.server --host 0.0.0.0 --port 11434")
    lines.append("  llama.cpp: llama-server --host 0.0.0.0 --port 8080")
    lines.append("  Ollama:    ollama serve")
    return "\n".join(lines)

  lines.append("")
  lines.append(f"FOUND {len(discovered)} server(s):")
  lines.append("")

  from ragtag.shared_config import list_all_llm_endpoints, save_llm_endpoint
  existing_endpoints = list_all_llm_endpoints()
  existing_urls = {ep.get("base_url", "").rstrip("/") for ep in existing_endpoints}

  newly_added_count = 0
  for i, d in enumerate(discovered, 1):
    base_url = d["url"]
    already_configured = base_url.rstrip("/") in existing_urls or f"{base_url.rstrip('/')}/v1" in existing_urls
    status_tag = " [already configured]" if already_configured else ""
    lines.append(f"  {i}. {base_url}  ({d['model_count']} model{'s' if d['model_count'] != 1 else ''}){status_tag}")

    if not already_configured:
      host = d["host"]
      port = d["port"]
      if port == 11434:
        provider_type = "mlx"
        ep_name = f"discovered-mlx-{host}" if host not in ("127.0.0.1", "localhost") else "local-mlx-discovered"
      elif port == 8080:
        provider_type = "llama_cpp"
        ep_name = f"discovered-llama-{host}" if host not in ("127.0.0.1", "localhost") else "local-llama-discovered"
      else:
        provider_type = "custom"
        ep_name = f"discovered-{host}-{port}"
      ep_name = ep_name.replace(".", "-")
      capabilities = {"streaming": True, "system_message": True}
      if provider_type == "mlx":
        capabilities["vision_input"] = True
      save_llm_endpoint(ep_name, {
        "provider_type": provider_type,
        "base_url": base_url,
        "description": f"Auto-discovered at {host}:{port}",
        "capabilities": capabilities,
      })
      lines.append(f"     -> Auto-added as endpoint '{ep_name}' (type: {provider_type})")
      newly_added_count += 1

  if newly_added_count > 0:
    lines.append(f"\n  {newly_added_count} new endpoint(s) added. Use 'List endpoints' to review.")
  else:
    lines.append("\n  All discovered servers are already configured.")

  return "\n".join(lines)


def _process_inline_endpoint_management_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Handle multi-step inline input for endpoint add/remove/test/set operations."""
  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)
  if normalized in ADMIN_CANCEL_NORMALIZED_FORMS:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Cancelled.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  pending = state["pending_guided_input"]
  definition_key = pending["definition_key"]
  user_input = (raw_text or "").strip()

  if not user_input:
    return state, "Please enter a value, or 'cancel'."

  VALID_API_PROVIDER_TYPES = {"mlx", "ollama", "openrouter", "openai", "anthropic", "llama_cpp", "custom", "local"}

  if definition_key == "__add_endpoint_inline__":
    step = pending.get("step_index", 0)
    if step == 0:
      pending["collected_values"]["endpoint_name"] = user_input
      pending["step_index"] = 1
      return state, ("Provider type (mlx, ollama, openrouter, openai, anthropic, llama_cpp, custom):\n"
                     "(Type 'cancel' to abort)")
    elif step == 1:
      provider_input = user_input.lower().strip()
      if provider_input not in VALID_API_PROVIDER_TYPES:
        return state, (f"'{user_input}' is not a valid provider type.\n\n"
                       f"Valid types: {', '.join(sorted(VALID_API_PROVIDER_TYPES))}\n"
                       f"  mlx — Apple MLX via mlx_vlm.server\n"
                       f"  ollama — Ollama server\n"
                       f"  llama_cpp — llama.cpp / llama-server (OpenAI-compatible)\n"
                       f"  openrouter — OpenRouter cloud API\n"
                       f"  openai — Direct OpenAI API\n"
                       f"  anthropic — Direct Anthropic API\n"
                       f"  custom — Any OpenAI-compatible HTTP endpoint\n\n"
                       f"(Type 'cancel' to abort)")
      pending["collected_values"]["provider_type"] = provider_input
      pending["step_index"] = 2
      return state, ("Base URL (e.g. 'http://192.168.1.100:11434', 'https://openrouter.ai/api/v1'):\n"
                     "(Type 'cancel' to abort)")
    elif step == 2:
      pending["collected_values"]["base_url"] = user_input
      pending["step_index"] = 3
      return _execute_admin_menu_action(
        {"type": "add_endpoint"},
        {"requires_active_agent": False},
        state,
      )

  elif definition_key == "__remove_endpoint_inline__":
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    if user_input.isdigit():
      idx = int(user_input) - 1
      if 0 <= idx < len(endpoints):
        user_input = endpoints[idx]["endpoint_name"]
    pending["collected_values"]["endpoint_name"] = user_input
    return _execute_admin_menu_action(
      {"type": "remove_endpoint"},
      {"requires_active_agent": False},
      state,
    )

  elif definition_key == "__test_endpoint_inline__":
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    if user_input.isdigit():
      idx = int(user_input) - 1
      if 0 <= idx < len(endpoints):
        user_input = endpoints[idx]["endpoint_name"]
    pending["collected_values"]["endpoint_name"] = user_input
    return _execute_admin_menu_action(
      {"type": "test_endpoint_health"},
      {"requires_active_agent": False},
      state,
    )

  elif definition_key == "__set_agent_endpoint_inline__":
    from ragtag.shared_config import list_all_llm_endpoints
    endpoints = list_all_llm_endpoints()
    if user_input.isdigit():
      idx = int(user_input) - 1
      if 0 <= idx < len(endpoints):
        user_input = endpoints[idx]["endpoint_name"]
    pending["collected_values"]["endpoint_name"] = user_input
    return _execute_admin_menu_action(
      {"type": "set_agent_endpoint"},
      {"requires_active_agent": True},
      state,
    )

  elif definition_key == "__scan_endpoints_inline__":
    scope = "localhost"
    if user_input in ("2", "lan", "subnet"):
      scope = "lan"
    pending["collected_values"]["scan_scope"] = scope
    return _execute_admin_menu_action(
      {"type": "scan_for_local_endpoints"},
      {"requires_active_agent": False},
      state,
    )

  state["pending_guided_input"] = None
  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
  return state, "Unexpected state.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


def _process_inline_engine_slot_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Handle multi-step inline input for engine slot set/add/remove operations.

  Stores the raw user input into pending_guided_input.user_raw_text then
  re-dispatches to _execute_admin_menu_action which picks it up from there.
  """
  pending = state.get("pending_guided_input")
  if not pending:
    state["pending_guided_input"] = None
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, "Internal error.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
  pending["user_raw_text"] = raw_text
  action = pending.get("action", {})
  return _execute_admin_menu_action(
    action,
    {"requires_active_agent": True},
    state,
  )


# ───── Admin.4: Input parser (top-level routing) ───────────────────────────────


def _find_menu_item_by_key_or_shortcut_word(items: List[Dict[str, Any]], token: str) -> Optional[Dict[str, Any]]:
  """Search a single level of menu items by exact key or by shortcut word."""
  if not token:
    return None
  for item in items:
    if item.get("key") == token:
      return item
    shortcut_words = item.get("shortcut_words") or []
    if token in shortcut_words:
      return item
  return None


def _process_admin_menu_input(state: Dict[str, Any], raw_text: str) -> Tuple[Dict[str, Any], str]:
  """Top-level admin input processor.  Returns (new_state, response_text).

  Routing order (checked in this sequence):
    1. If a guided input (or inline special input) is pending, route to it.
    2. Normalize text.
    3. Exit commands (/chat, /exit, chat, exit).  NOTE: caller checks too;
       we check again here as a safety net when called directly by tests.
    4. Global shortcuts that resolve to menu paths (pause, stats, model…).
    5. Navigation words: back, up, home, main, help.
    6. Single digit "0" → back to main / exit admin mode (caller handles exit).
    7. Path chain (e.g. "1 3") → navigate through multiple keys.
    8. Single key/token → navigate / execute within current menu.
    9. Unknown → helpful error with current menu re-displayed.
  """
  pending = state.get("pending_guided_input")
  if pending:
    definition_key = pending.get("definition_key")
    if definition_key == "__select_agent_inline__":
      return _process_inline_select_agent_input(state, raw_text)
    if definition_key == "__set_active_session_inline__":
      return _process_inline_set_active_session_input(state, raw_text)
    if definition_key == "__search_openrouter_models_inline__":
      return _process_inline_search_openrouter_models_input(state, raw_text)
    if definition_key in ("__add_endpoint_inline__", "__remove_endpoint_inline__",
                          "__test_endpoint_inline__", "__set_agent_endpoint_inline__",
                          "__scan_endpoints_inline__"):
      return _process_inline_endpoint_management_input(state, raw_text)
    if definition_key in ("__set_primary_engine_inline__", "__set_compaction_engine_inline__",
                          "__add_fallback_entry_inline__", "__remove_fallback_entry_inline__"):
      return _process_inline_engine_slot_input(state, raw_text)
    return _process_admin_guided_input_step(state, raw_text)

  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_text)
  current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE

  if not normalized:
    return state, "(empty input ignored)\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

  if _check_if_message_is_admin_exit_command(normalized):
    return state, "__EXIT_ADMIN__"

  if normalized in ADMIN_NAV_HELP_NORMALIZED_FORMS:
    return state, _render_admin_menu_as_plain_text(current_menu, state)

  if normalized in ADMIN_NAV_BACK_NORMALIZED_FORMS:
    path = state.get("menu_path", [])
    if path:
      state["menu_path"] = path[:-1]
    current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
    return state, _render_admin_menu_as_plain_text(current_menu, state)

  if normalized in ADMIN_NAV_HOME_NORMALIZED_FORMS:
    state["menu_path"] = []
    return state, _render_admin_menu_as_plain_text(ADMIN_MENU_TREE, state)

  if state.get("menu_path") == [] and normalized == "0":
    return state, "__EXIT_ADMIN__"

  global_shortcut = ADMIN_GLOBAL_SHORTCUT_WORDS.get(normalized)
  if global_shortcut is not None:
    tokens = list(global_shortcut)
    return _navigate_menu_via_key_chain(state, tokens)

  tokens_for_chain = normalized.split()
  if len(tokens_for_chain) > 1 and all(t.isdigit() or t in ADMIN_GLOBAL_SHORTCUT_WORDS for t in tokens_for_chain):
    return _navigate_menu_via_key_chain(state, tokens_for_chain)

  if len(tokens_for_chain) == 1:
    single_token = tokens_for_chain[0]

    if single_token == "0":
      path = state.get("menu_path", [])
      if path:
        state["menu_path"] = path[:-1]
        current_menu = _resolve_menu_node_from_path(state.get("menu_path", [])) or ADMIN_MENU_TREE
        return state, _render_admin_menu_as_plain_text(current_menu, state)
      return state, "__EXIT_ADMIN__"

    items_at_level = _get_menu_items_at_node(current_menu)
    match = _find_menu_item_by_key_or_shortcut_word(items_at_level, single_token)
    if match is None:
      return state, f"I didn't understand '{raw_text.strip()}'. Type 'help' to see the menu, 'home' to return to the main menu.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

    if "submenu" in match:
      new_path = list(state.get("menu_path", [])) + [match["key"]]
      state["menu_path"] = new_path
      new_menu = _resolve_menu_node_from_path(new_path) or ADMIN_MENU_TREE
      return state, _render_admin_menu_as_plain_text(new_menu, state)

    action = match.get("action")
    if not action:
      return state, f"'{match.get('label')}' has no action.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    return _execute_admin_menu_action(action, match, state)

  return state, f"I didn't understand '{raw_text.strip()}'. Type 'help' to see the menu.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)


def _navigate_menu_via_key_chain(state: Dict[str, Any], tokens: List[str]) -> Tuple[Dict[str, Any], str]:
  """Walk a chain of menu keys/shortcut tokens starting from the root.

  Stops and either displays a reached submenu or executes a reached leaf action.
  Resets the menu path to [] before walking so chains always start from root —
  this matches natural usage ("1 3" means go to 1 then 3, from the main menu).
  """
  state["menu_path"] = []
  current_menu = ADMIN_MENU_TREE
  path: List[str] = []

  for idx, token in enumerate(tokens):
    items = _get_menu_items_at_node(current_menu)
    match = _find_menu_item_by_key_or_shortcut_word(items, token)
    if match is None:
      state["menu_path"] = path
      current_menu = _resolve_menu_node_from_path(path) or ADMIN_MENU_TREE
      return state, f"Could not navigate: '{token}' not found at that level.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)

    if "submenu" in match:
      path.append(match["key"])
      current_menu = match
      continue

    action = match.get("action")
    if not action:
      state["menu_path"] = path
      return state, f"'{match.get('label')}' has no action.\n\n" + _render_admin_menu_as_plain_text(current_menu, state)
    state["menu_path"] = path
    return _execute_admin_menu_action(action, match, state)

  state["menu_path"] = path
  return state, _render_admin_menu_as_plain_text(current_menu, state)


# ───── Admin.8: Intercept helpers used by handle_send_message + Telegram ──────


def _maybe_intercept_admin_message(
  channel_key: str,
  raw_incoming_text: str,
  candidate_initial_active_agent_id: Optional[str],
) -> Optional[Dict[str, Any]]:
  """Check whether an incoming message should be handled as admin input.

  Returns None if the message should be passed through to the normal agent
  flow.  Returns a dict {"response_text": str, "exited": bool} when the
  message WAS consumed by the admin interpreter.

  Algorithm:
    1. Normalize the text.
    2. If the text is an admin-entry command AND the channel is not already in
       admin mode, enter admin mode (fresh state) and return the main menu.
    3. Else if the channel is already in admin mode:
         - On exit command → clear state and return exit confirmation.
         - Otherwise → run `_process_admin_menu_input` and return its text.
    4. Else → return None (pass through to agent).
  """
  if not channel_key:
    return None

  try:
    initialize_agent_kernel_database()
  except Exception as init_error:
    MCPLogger.log(TOOL_LOG_NAME, f"Admin intercept init failed: {init_error}")

  normalized = _normalize_admin_input_text_for_stt_tolerance(raw_incoming_text or "")

  already_in_admin = _is_channel_in_admin_mode(channel_key)

  if not already_in_admin:
    if _check_if_message_is_admin_entry_command(normalized):
      state = _build_default_admin_state_dict(candidate_initial_active_agent_id)
      _set_admin_state_for_channel(channel_key, state)
      menu_text = _render_admin_menu_as_plain_text(ADMIN_MENU_TREE, state)
      welcome_line = "Entered ADMIN mode. No LLM is used for admin operations. Type '/chat' to exit."
      return {"response_text": welcome_line + "\n\n" + menu_text, "exited": False}
    return None

  if _check_if_message_is_admin_exit_command(normalized):
    _clear_admin_state_for_channel(channel_key)
    return {"response_text": "Exited admin mode. Your next message will go to the agent as normal.", "exited": True}

  state = _get_admin_state_for_channel(channel_key)
  if state is None:
    return None

  new_state, response_text = _process_admin_menu_input(state, raw_incoming_text or "")

  if response_text == "__EXIT_ADMIN__":
    _clear_admin_state_for_channel(channel_key)
    return {"response_text": "Exited admin mode. Your next message will go to the agent as normal.", "exited": True}

  _set_admin_state_for_channel(channel_key, new_state)
  return {"response_text": response_text, "exited": False}


def _send_admin_response_via_telegram(chat_id: Any, bot_token_hash: str, response_text: str) -> None:
  """Fire-and-forget: deliver an admin menu response to a Telegram chat.

  Uses `social_rog.send_message` with the chat_id.  Chunks long responses
  to stay under Telegram's ~4096-char message limit.
  """
  if not response_text or chat_id is None:
    return
  max_chunk_size = 3800
  chunks: List[str] = []
  remaining = response_text
  while len(remaining) > max_chunk_size:
    split_at = remaining.rfind("\n", 0, max_chunk_size)
    if split_at <= 0:
      split_at = max_chunk_size
    chunks.append(remaining[:split_at])
    remaining = remaining[split_at:].lstrip("\n")
  if remaining:
    chunks.append(remaining)
  for chunk in chunks:
    try:
      _call_tool(_suffixed_tool_name("social"), {"input": {
        "operation": "send_message",
        "chat_id": chat_id,
        "text": chunk,
        "tool_unlock_token": "__auto__",
      }})
    except Exception as send_error:
      MCPLogger.log(TOOL_LOG_NAME, f"Admin: Telegram send_message failed for chat {chat_id}: {send_error}")


# ===============================================================================
# END ADMIN INTERFACE
# ===============================================================================




OPERATION_DISPATCH_TABLE: Dict[str, Any] = {
  "echo":                handle_echo,
  "_self_test":          handle_self_test,
  "init_schema":         handle_init_schema,
  "create_agent":        handle_create_agent,
  "list_agents":         handle_list_agents,
  "get_agent":           handle_get_agent,
  "update_agent":        handle_update_agent,
  "delete_agent":        handle_delete_agent,
  "send_message":        handle_send_message,
  "get_history":         handle_get_history,
  "status":              handle_status,
  "add_event_source":    handle_add_event_source,
  "remove_event_source": handle_remove_event_source,
  "list_event_sources":  handle_list_event_sources,
  "pause_agent":         handle_pause_agent,
  "resume_agent":        handle_resume_agent,
  "interrupt_agent":     handle_interrupt_agent,
  "compact_context":     handle_compact_context,
  "get_memory":          handle_get_memory,
  "set_memory":          handle_set_memory,
  "delete_memory":       handle_delete_memory,
  "approve_action":      handle_approve_action,
  "deny_action":         handle_deny_action,
  "get_pending_approvals": handle_get_pending_approvals,
  "reflect_now":         handle_reflect_now,
  "get_dlq":             handle_get_dlq,
  "retry_dlq":           handle_retry_dlq,
  "discard_dlq":         handle_discard_dlq,
  "get_session_log":     handle_get_session_log,
  "get_checkpoints":     handle_get_checkpoints,
  "get_run_log":         handle_get_run_log,
  "respond_to_user_request": handle_respond_to_user_request,
  "approve_contact":         handle_approve_contact,
  "block_contact":           handle_block_contact,
  "list_contacts":           handle_list_contacts,
}

def handle_agent(input_param: Dict) -> Dict:
  """Handle agent tool operations via MCP interface."""
  try:
    # .get (not .pop): popping would mutate the caller's parameters dict.
    handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None

    if isinstance(input_param, dict) and "input" in input_param:
      input_param = input_param["input"]

    if isinstance(input_param, dict) and input_param.get("operation") == "readme":
      return {"content": [{"type": "text", "text": readme(True)}], "isError": False}

    if not isinstance(input_param, dict):
      return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)

    provided_token = input_param.get("tool_unlock_token")

    is_inter_tool_call = False
    if provided_token and provided_token.startswith("-"):
      try:
        parts = provided_token[1:].split("-", 1)
        if len(parts) == 2 and parts[1] == TOOL_UNLOCK_TOKEN:
          is_inter_tool_call = True
          MCPLogger.log(TOOL_LOG_NAME, f"Inter-tool call accepted from token: {parts[0]}")
      except Exception:
        pass

    if provided_token != TOOL_UNLOCK_TOKEN and not is_inter_tool_call:
      return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True)

    error_msg, validated_params = validate_parameters(input_param)
    if error_msg:
      return create_error_response(error_msg, with_readme=True)

    # Thread the server-provided handler_info through to handlers (operator
    # identity for the admin menu). Set unconditionally AFTER validation so a
    # caller cannot smuggle a spoofed handler_info inside the input dict.
    validated_params["handler_info"] = handler_info

    operation = validated_params.get("operation")

    handler_function = OPERATION_DISPATCH_TABLE.get(operation)
    if handler_function:
      return handler_function(validated_params)

    if operation in ALL_AGENT_OPERATIONS:
      return create_error_response(f"Operation '{operation}' is recognized but not yet implemented. It is planned for a future phase.")

    return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(ALL_AGENT_OPERATIONS)}", with_readme=True)

  except Exception as e:
    return create_error_response(f"Error in agent operation: {str(e)}", with_readme=True)

HANDLERS = {
  TOOL_NAME: handle_agent
}


def on_all_tools_registered():
  """Called by __init__.py after server + all sibling tools are fully ready.

  At this point _get_mcp_server_instance() is guaranteed to return a live
  server, and every tool_handler (sqlite_rog, social_rog, etc.) is
  registered and callable via call_tool_internal().  This is the correct
  time to initialize the database schema and re-register Telegram event
  source callbacks — no polling/retry needed.

  The actual work runs in a background thread so it does not block the
  HTTP listener from starting, but the thread can begin immediately
  because all dependencies are already available.
  """
  def _post_registration_startup_work():
    import traceback as _tb
    try:
      MCPLogger.log(TOOL_LOG_NAME, "on_all_tools_registered: starting DB schema init + event source re-registration")
      schema_ok, schema_msg = initialize_agent_kernel_database()
      if schema_ok:
        MCPLogger.log(TOOL_LOG_NAME, f"on_all_tools_registered: complete ({schema_msg})")
        # Resume cron scheduling after restart when enabled cron sources exist.
        enabled_cron_count_rows = _parse_rows_from_mcp_query_response(_call_sqlite(
          "SELECT COUNT(*) AS enabled_cron_source_count FROM event_sources WHERE source_type IN ('cron', 'cron_oneshot') AND is_enabled = 1",
          database=AGENT_KERNEL_DATABASE_NAME,
        ))
        enabled_cron_source_count = 0
        if enabled_cron_count_rows:
          enabled_cron_source_count = int(enabled_cron_count_rows[0].get("enabled_cron_source_count", 0) or 0)
        if enabled_cron_source_count > 0:
          MCPLogger.log(TOOL_LOG_NAME, f"on_all_tools_registered: starting cron scheduler for {enabled_cron_source_count} enabled cron source(s)")
          _ensure_cron_scheduler_is_running()
      else:
        MCPLogger.log(TOOL_LOG_NAME, f"on_all_tools_registered: schema init FAILED: {schema_msg}")
    except Exception as startup_error:
      MCPLogger.log(TOOL_LOG_NAME,
        f"on_all_tools_registered CRASHED: {startup_error}\n{_tb.format_exc()}")

  post_registration_thread = threading.Thread(
    target=_post_registration_startup_work,
    daemon=True,
    name="agent-post-registration-startup"
  )
  post_registration_thread.start()
