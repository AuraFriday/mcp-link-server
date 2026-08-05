"""
File: ragtag/tools/social.py
Project: Aura Friday MCP-Link Server
Component: Social Messaging Tool (Telegram Bot API)
Author: Christopher Nathan Drake (cnd)

Tool implementation for AI-to-human communication via Telegram Bot API.
Enables an AI agent to send messages, receive messages, manage chats,
and have interactive conversations with humans through a Telegram bot.

Uses only Python standard library (urllib) for HTTP calls - no external dependencies.

Copyright: (c) 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "DꓑօꓴɪᏎƬ×ϜΥꓑqFƙ7𝟤jȜⲟƴɯƼЗⲘɊǝEd𝟑LРıīꓟiНHƛսYΜ𝟚ƟŧsUⲞᏴBƱꙄЕᏮƌǝᗷ𝟙qʌⲔdꓚᗞv𝟤ᖴīΟꓟȜꓑꓓƏνᴛСᏂƟ𝟛Ꮋр𝟫ωFԛɪСꓗŧųƽոʋz9ⅠꓦþᏮ9уꓑȣpᎪΥꓬǝϜ"
"signdate": "2026-07-23T02:39:15.795Z",
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl
import hashlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Callable
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_config_manager, SharedConfigManager, get_user_data_directory

# ============================================================================
# CONSTANTS
# ============================================================================

TOOL_LOG_NAME = "SOCIAL"
VERSION = "3.1.0"

# Module-level token generated once at import time.
# THREAT MODEL NOTE: this unlock token is a documentation-gating device, NOT a security
# boundary. It is printed in the readme and embedded in real_parameters (so anything that
# can see the schema can see it), and same-process callers can read TOOL_UNLOCK_TOKEN
# directly. Its only purpose is to force AI callers to read the docs once before use.
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"social{TOOL_NAME_SUFFIX}"

TELEGRAM_BOT_API_BASE_URL = "https://api.telegram.org/bot"
TELEGRAM_FILE_API_BASE_URL = "https://api.telegram.org/file/bot"

# How many recent messages to keep in memory per bot
MAX_TELEGRAM_MESSAGE_HISTORY_PER_BOT = 500

# Bound for honoring HTTP 429 parameters.retry_after (longer waits are not slept)
_HTTP_429_MAX_RETRY_AFTER_SLEEP_SECONDS = 30.0

# Cap for get_message_history / get_persistent_history 'limit' (SQLite treats LIMIT -1 as unlimited)
_HISTORY_QUERY_LIMIT_MAX = 500

# Bound for each register_event_callback accumulator queue (oldest events drop when full)
_EVENT_CALLBACK_ACCUMULATOR_MAX_EVENTS = 500

# Telegram Bot API hard limits, pre-validated so callers get a clear error instead of
# an opaque API failure
_TELEGRAM_MAX_MESSAGE_TEXT_LENGTH_CHARS = 4096
_TELEGRAM_MAX_CAPTION_LENGTH_CHARS = 1024

# Local file upload caps (Telegram bot API: 10MB photos, 50MB documents)
_TELEGRAM_PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_TELEGRAM_DOCUMENT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

# Telegram getFile only serves files up to 20MB, so download_file caps there too
_TELEGRAM_FILE_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024

# Background poller gives up (and records why) after this many consecutive failures
_BACKGROUND_POLLER_AUTO_STOP_AFTER_CONSECUTIVE_FAILURES = 20

# Cap the backoff exponent so 2**n stays small no matter how long errors persist
_BACKGROUND_POLLER_BACKOFF_EXPONENT_CAP = 6

# wait_for_message blocks the MCP request thread, so bound how long callers may wait
_WAIT_FOR_MESSAGE_MAX_WAIT_SECONDS = 120

# Only well-formed method names / tokens may be interpolated into request URLs
# (a crafted method like 'getMe?x=' or a token containing '/' would change the path)
_TELEGRAM_API_METHOD_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_]+$')
_TELEGRAM_BOT_TOKEN_FORMAT_PATTERN = re.compile(r'^\d+:[A-Za-z0-9_-]{30,}$')

# promoteChatMember accepts only these flat permission fields; restrictChatMember
# takes a ChatPermissions object whose keys are all can_* booleans
_PROMOTE_PERMISSION_KEY_ALLOWLIST_PATTERN = re.compile(r'^(is_anonymous|can_[a-z_]+)$')
_RESTRICT_PERMISSION_KEY_ALLOWLIST_PATTERN = re.compile(r'^can_[a-z_]+$')

# One module-level TLS context (ssl contexts are thread-safe for client use;
# building a fresh one per request was wasteful)
_TELEGRAM_TLS_SSL_CONTEXT = ssl.create_default_context()

# MarkdownV2 reserved characters escaped by auto_escape (backtick excluded: it
# delimits code spans, which we deliberately leave intact)
_MARKDOWNV2_RESERVED_CHARACTERS_TO_ESCAPE = set('_*[]()~>#+-=|{}.!')

# ============================================================================
# GLOBAL STATE - Persistent across tool calls within a server session
# ============================================================================

# Lock for thread-safe access to all global telegram state
_telegram_global_state_lock = threading.Lock()

# Tracks the getUpdates offset per bot so we only fetch new messages
# Key: bot_token_short_hash -> Value: integer offset for next getUpdates call
_telegram_update_offset_per_bot = {}

# Tracks all chats that have interacted with each bot
# Key: bot_token_short_hash -> Value: {chat_id: chat_info_dict}
_telegram_known_chats_per_bot = {}

# Stores recent messages received per bot for the AI to review
# Key: bot_token_short_hash -> Value: deque of message dicts
_telegram_received_message_history_per_bot = {}

# Background polling threads per bot (raw tokens are deliberately NOT stored here)
# Key: bot_token_short_hash -> Value: {"thread": Thread, "running": bool,
#   "stop_event": Event, "started_at": str, "long_poll_timeout_seconds": int,
#   "consecutive_error_count": int, "last_error": str|None, "stopped_reason": str (when auto-stopped)}
_telegram_background_pollers_per_bot = {}

# ============================================================================
# FEATURE: EVENT CALLBACKS — push-style notification when messages arrive
# ============================================================================

# Registered callback functions invoked by the background poller on new messages.
# Key: callback_id (str) -> Value: {
#   "callback": Callable[[str, List[Dict]], None],  # (bot_token_hash, formatted_messages)
#   "filter_chat_ids": Optional[set],  # None = all chats, set = only these chat_ids
#   "filter_message_types": Optional[set],  # None = all, set = only these (e.g. {"text","callback_query"})
#   "registered_at": str
# }
_telegram_event_callbacks = {}
_telegram_event_callbacks_lock = threading.Lock()


def _coerce_chat_id_list_members_to_int_where_possible(chat_ids: Optional[List]) -> Optional[List[int]]:
  """JSON callers often send chat ids as strings, but incoming Telegram chat_id
  values are ints - coerce so membership checks actually match. Non-numeric
  entries are dropped (they could never match an incoming integer chat_id)."""
  if not chat_ids:
    return None
  coerced_integer_chat_ids = []
  for chat_id_value in chat_ids:
    if isinstance(chat_id_value, bool):
      continue
    try:
      coerced_integer_chat_ids.append(int(chat_id_value))
    except (ValueError, TypeError):
      continue
  return coerced_integer_chat_ids or None


def register_message_event_callback(
    callback_id: str,
    callback_function: Callable[[str, List[Dict]], None],
    filter_by_chat_ids: Optional[List[int]] = None,
    filter_by_message_types: Optional[List[str]] = None,
) -> None:
  """Register a callback that fires when the background poller receives messages.

  Args:
    callback_id: Unique identifier for this registration (used to unregister later).
    callback_function: Called with (bot_token_hash, list_of_formatted_messages).
    filter_by_chat_ids: If set, only fire for messages from these chat_ids.
    filter_by_message_types: If set, only fire for these types (e.g. "text", "callback_query").
  """
  coerced_filter_chat_ids = _coerce_chat_id_list_members_to_int_where_possible(filter_by_chat_ids)
  with _telegram_event_callbacks_lock:
    _telegram_event_callbacks[callback_id] = {
      "callback": callback_function,
      "filter_chat_ids": set(coerced_filter_chat_ids) if coerced_filter_chat_ids else None,
      "filter_message_types": set(filter_by_message_types) if filter_by_message_types else None,
      "registered_at": datetime.now(timezone.utc).isoformat(),
    }
  MCPLogger.log(TOOL_LOG_NAME, f"Event callback registered: {callback_id}")


def unregister_message_event_callback(callback_id: str) -> bool:
  """Remove a previously registered callback. Returns True if it existed."""
  with _telegram_event_callbacks_lock:
    removed = _telegram_event_callbacks.pop(callback_id, None)
  if removed:
    MCPLogger.log(TOOL_LOG_NAME, f"Event callback unregistered: {callback_id}")
  return removed is not None


def _dispatch_event_callbacks(bot_token_hash: str, formatted_messages: List[Dict]) -> None:
  """Fire all registered callbacks for the given batch of messages.
  Runs in the background poller thread — callbacks must be fast or offload work."""
  with _telegram_event_callbacks_lock:
    active_callbacks = list(_telegram_event_callbacks.items())

  for callback_id, registration in active_callbacks:
    try:
      matched_messages = formatted_messages

      # Apply chat_id filter
      if registration["filter_chat_ids"] is not None:
        matched_messages = [
          m for m in matched_messages
          if m.get("chat_id") in registration["filter_chat_ids"]
        ]

      # Apply message type filter
      if registration["filter_message_types"] is not None:
        matched_messages = [
          m for m in matched_messages
          if m.get("type", "text") in registration["filter_message_types"]
        ]

      if matched_messages:
        registration["callback"](bot_token_hash, matched_messages)
    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"Event callback {callback_id} raised exception: {e}")


# ============================================================================
# FEATURE: SQLITE-BACKED PERSISTENT CONVERSATION STATE (opt-in)
# ============================================================================

_sqlite_persistence_enabled_flag = False
# Storing full raw update JSON (contact phone numbers, locations, etc) is opt-in
# to limit PII retention; enable via enable_persistence {store_raw_json: true}
_sqlite_store_raw_json_enabled_flag = False
_sqlite_db_connection: Optional[sqlite3.Connection] = None
_sqlite_db_lock = threading.Lock()

_SQLITE_SCHEMA_VERSION = 2
_SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS social_schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_messages (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_token_hash TEXT NOT NULL,
  message_id INTEGER,
  update_id INTEGER,
  chat_id INTEGER NOT NULL,
  chat_type TEXT,
  chat_title TEXT,
  from_user_id INTEGER,
  from_username TEXT,
  from_display_name TEXT,
  from_is_bot INTEGER DEFAULT 0,
  text TEXT,
  message_type TEXT DEFAULT 'text',
  raw_json TEXT,
  timestamp_utc REAL NOT NULL,
  received_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  persona_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_id ON telegram_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_bot_hash ON telegram_messages(bot_token_hash);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_timestamp ON telegram_messages(timestamp_utc);

-- Replay dedupe: one row per (bot, update_id). NULL update_ids (legacy rows) never
-- conflict, so this index is safe to add to existing databases.
-- Upgrade safety: databases written BEFORE this index existed can already hold
-- replayed duplicates (offsets are in-memory, so a restart re-fetches unacknowledged
-- updates); keep the oldest row of each (bot, update_id) pair so the unique index
-- below can always be created. Idempotent - a no-op once the index enforces uniqueness.
DELETE FROM telegram_messages WHERE update_id IS NOT NULL AND rowid NOT IN (
  SELECT MIN(rowid) FROM telegram_messages WHERE update_id IS NOT NULL
  GROUP BY bot_token_hash, update_id
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_messages_bot_update_dedupe
  ON telegram_messages(bot_token_hash, update_id);

CREATE TABLE IF NOT EXISTS telegram_known_chats (
  bot_token_hash TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  chat_type TEXT,
  title TEXT,
  first_name TEXT,
  last_name TEXT,
  username TEXT,
  last_message_date INTEGER,
  last_message_text TEXT,
  assigned_persona_id TEXT,
  updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (bot_token_hash, chat_id)
);

CREATE TABLE IF NOT EXISTS telegram_personas (
  persona_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  system_prompt TEXT NOT NULL,
  trigger_command TEXT,
  trigger_pattern TEXT,
  assigned_chat_ids TEXT,
  is_default INTEGER DEFAULT 0,
  created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- v2: the never-used telegram_outbound_rate_state table is removed (rate limiting
-- is in-memory only); existing databases get it dropped here too
DROP TABLE IF EXISTS telegram_outbound_rate_state;

-- Listener/callback state so restore_state can revive listening after a server restart
CREATE TABLE IF NOT EXISTS telegram_listener_state (
  bot_token_hash TEXT PRIMARY KEY,
  listening_active INTEGER NOT NULL DEFAULT 0,
  updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS telegram_event_callback_specs (
  callback_id TEXT PRIMARY KEY,
  filter_chat_ids TEXT,
  filter_message_types TEXT,
  registered_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def _get_social_sqlite_db_path() -> Path:
  """Return path to the social tool's sqlite database file."""
  return get_user_data_directory() / "social_telegram.db"


def _ensure_sqlite_db_connection() -> sqlite3.Connection:
  """Get or create the sqlite connection with WAL mode and schema applied."""
  global _sqlite_db_connection
  with _sqlite_db_lock:
    if _sqlite_db_connection is not None:
      return _sqlite_db_connection

    db_path = _get_social_sqlite_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    MCPLogger.log(TOOL_LOG_NAME, f"Opening sqlite persistence at {db_path}")

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # Row factory is set once here (rows behave like dicts via dict(row));
    # per-query row_factory toggling on a shared connection was fragile
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SQLITE_SCHEMA_SQL)

    # Ensure schema version row (and advance it for upgraded databases)
    row = conn.execute("SELECT version FROM social_schema_version LIMIT 1").fetchone()
    if row is None:
      conn.execute("INSERT INTO social_schema_version (version) VALUES (?)", (_SQLITE_SCHEMA_VERSION,))
    elif row[0] < _SQLITE_SCHEMA_VERSION:
      conn.execute("UPDATE social_schema_version SET version=?", (_SQLITE_SCHEMA_VERSION,))
    conn.commit()

    _sqlite_db_connection = conn
    return conn


def _with_sqlite_db(db_operation_function: Callable[[sqlite3.Connection], Any]) -> Any:
  """Run a function against the sqlite connection while holding the db lock.
  Removes the repeated ensure-connection-then-re-acquire-lock boilerplate."""
  conn = _ensure_sqlite_db_connection()
  with _sqlite_db_lock:
    return db_operation_function(conn)


def enable_sqlite_persistence(store_raw_json: bool = False) -> Dict:
  """Turn on sqlite-backed message/chat/persona storage."""
  global _sqlite_persistence_enabled_flag, _sqlite_store_raw_json_enabled_flag
  _ensure_sqlite_db_connection()
  _sqlite_persistence_enabled_flag = True
  _sqlite_store_raw_json_enabled_flag = bool(store_raw_json)
  MCPLogger.log(TOOL_LOG_NAME, f"SQLite persistence ENABLED (store_raw_json={_sqlite_store_raw_json_enabled_flag})")
  return {
    "status": "enabled",
    "db_path": str(_get_social_sqlite_db_path()),
    "store_raw_json": _sqlite_store_raw_json_enabled_flag,
  }


def disable_sqlite_persistence() -> Dict:
  """Turn off sqlite-backed storage (in-memory state remains active).
  Closes the db connection so WAL/SHM files are released."""
  global _sqlite_persistence_enabled_flag, _sqlite_db_connection
  _sqlite_persistence_enabled_flag = False
  with _sqlite_db_lock:
    if _sqlite_db_connection is not None:
      try:
        _sqlite_db_connection.close()
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"SQLite close error on disable: {e}")
      _sqlite_db_connection = None
  MCPLogger.log(TOOL_LOG_NAME, "SQLite persistence DISABLED (in-memory state still active)")
  return {"status": "disabled"}


def _sqlite_store_message(bot_token_hash: str, formatted_message: Dict, raw_message: Optional[Dict] = None) -> None:
  """Persist a single message to sqlite (no-op if persistence disabled).
  INSERT OR IGNORE + the unique (bot_token_hash, update_id) index make replays
  of the same update idempotent. raw_json is stored only when opted in."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    def _insert_message_row(conn: sqlite3.Connection):
      conn.execute(
        """INSERT OR IGNORE INTO telegram_messages
           (bot_token_hash, message_id, update_id, chat_id, chat_type, chat_title,
            from_user_id, from_username, from_display_name, from_is_bot,
            text, message_type, raw_json, timestamp_utc, persona_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
          bot_token_hash,
          formatted_message.get("message_id"),
          formatted_message.get("update_id"),
          formatted_message.get("chat_id"),
          formatted_message.get("chat_type"),
          formatted_message.get("chat_title_or_name"),
          formatted_message.get("from_user_id"),
          formatted_message.get("from_username"),
          formatted_message.get("from_display_name"),
          1 if formatted_message.get("from_is_bot") else 0,
          formatted_message.get("text"),
          formatted_message.get("type", "text"),
          json.dumps(raw_message) if (raw_message and _sqlite_store_raw_json_enabled_flag) else None,
          formatted_message.get("date", time.time()),
          formatted_message.get("matched_persona_id"),
        ),
      )
      conn.commit()
    _with_sqlite_db(_insert_message_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite store_message error: {e}")


def _sqlite_upsert_known_chat(bot_token_hash: str, chat_info: Dict) -> None:
  """Persist or update a known chat to sqlite (no-op if persistence disabled)."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    def _upsert_chat_row(conn: sqlite3.Connection):
      conn.execute(
        """INSERT INTO telegram_known_chats
           (bot_token_hash, chat_id, chat_type, title, first_name, last_name, username,
            last_message_date, last_message_text)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(bot_token_hash, chat_id) DO UPDATE SET
            chat_type=excluded.chat_type, title=excluded.title,
            first_name=excluded.first_name, last_name=excluded.last_name,
            username=excluded.username, last_message_date=excluded.last_message_date,
            last_message_text=excluded.last_message_text,
            updated_at_utc=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (
          bot_token_hash,
          chat_info.get("id"),
          chat_info.get("type"),
          chat_info.get("title"),
          chat_info.get("first_name"),
          chat_info.get("last_name"),
          chat_info.get("username"),
          chat_info.get("last_message_date"),
          chat_info.get("last_message_text", ""),
        ),
      )
      conn.commit()
    _with_sqlite_db(_upsert_chat_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite upsert_known_chat error: {e}")


def _sqlite_query_message_history(bot_token_hash: str, chat_id: Optional[int] = None,
                                  limit: int = 50, since_timestamp: Optional[float] = None) -> List[Dict]:
  """Read messages from sqlite. Returns list of dicts."""
  if not _sqlite_persistence_enabled_flag:
    return []
  try:
    query = "SELECT * FROM telegram_messages WHERE bot_token_hash=?"
    params: list = [bot_token_hash]
    if chat_id is not None:
      query += " AND chat_id=?"
      params.append(chat_id)
    if since_timestamp is not None:
      query += " AND timestamp_utc>?"
      params.append(since_timestamp)
    query += " ORDER BY timestamp_utc DESC LIMIT ?"
    params.append(limit)

    rows = _with_sqlite_db(lambda conn: conn.execute(query, params).fetchall())
    return [dict(r) for r in reversed(rows)]
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite query_message_history error: {e}")
    return []


def _sqlite_record_listener_active_state(bot_token_hash: str, listening_is_active: bool) -> None:
  """Record whether a background listener is active, so restore_state can revive
  it after a server restart (no-op if persistence disabled)."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    def _upsert_listener_state_row(conn: sqlite3.Connection):
      conn.execute(
        """INSERT INTO telegram_listener_state (bot_token_hash, listening_active)
           VALUES (?,?)
           ON CONFLICT(bot_token_hash) DO UPDATE SET
            listening_active=excluded.listening_active,
            updated_at_utc=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (bot_token_hash, 1 if listening_is_active else 0),
      )
      conn.commit()
    _with_sqlite_db(_upsert_listener_state_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite record_listener_state error: {e}")


def _sqlite_record_event_callback_spec(callback_id: str,
                                       filter_chat_ids: Optional[List],
                                       filter_message_types: Optional[List]) -> None:
  """Persist an MCP accumulator callback spec so restore_state can re-register it
  after a server restart (no-op if persistence disabled)."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    def _upsert_callback_spec_row(conn: sqlite3.Connection):
      conn.execute(
        """INSERT INTO telegram_event_callback_specs (callback_id, filter_chat_ids, filter_message_types)
           VALUES (?,?,?)
           ON CONFLICT(callback_id) DO UPDATE SET
            filter_chat_ids=excluded.filter_chat_ids,
            filter_message_types=excluded.filter_message_types""",
        (
          callback_id,
          json.dumps(filter_chat_ids) if filter_chat_ids else None,
          json.dumps(filter_message_types) if filter_message_types else None,
        ),
      )
      conn.commit()
    _with_sqlite_db(_upsert_callback_spec_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite record_callback_spec error: {e}")


def _sqlite_delete_event_callback_spec(callback_id: str) -> None:
  """Remove a persisted callback spec (no-op if persistence disabled)."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    def _delete_callback_spec_row(conn: sqlite3.Connection):
      conn.execute("DELETE FROM telegram_event_callback_specs WHERE callback_id=?", (callback_id,))
      conn.commit()
    _with_sqlite_db(_delete_callback_spec_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite delete_callback_spec error: {e}")


# ============================================================================
# FEATURE: OUTBOUND RATE LIMITER (token bucket per chat)
# ============================================================================

# Telegram limits: ~30 msg/s to different chats, ~1 msg/s per group, ~1 msg/3s same user in group
_RATE_LIMIT_TOKENS_PER_SECOND = 1.0
_RATE_LIMIT_BUCKET_CAPACITY = 3.0
_rate_limit_buckets_per_chat: Dict[int, Dict] = {}
_rate_limit_lock = threading.Lock()


def _rate_limit_acquire_send_permission(chat_id: int) -> float:
  """Token-bucket rate limiter. Returns 0.0 if allowed immediately,
  or a positive float = seconds to sleep before sending."""
  now = time.monotonic()
  with _rate_limit_lock:
    bucket = _rate_limit_buckets_per_chat.get(chat_id)
    if bucket is None:
      bucket = {"tokens": _RATE_LIMIT_BUCKET_CAPACITY, "last_refill": now}
      _rate_limit_buckets_per_chat[chat_id] = bucket

    # Refill tokens based on elapsed time
    elapsed = now - bucket["last_refill"]
    bucket["tokens"] = min(
      _RATE_LIMIT_BUCKET_CAPACITY,
      bucket["tokens"] + elapsed * _RATE_LIMIT_TOKENS_PER_SECOND,
    )
    bucket["last_refill"] = now

    if bucket["tokens"] >= 1.0:
      bucket["tokens"] -= 1.0
      return 0.0
    else:
      wait_seconds_needed = (1.0 - bucket["tokens"]) / _RATE_LIMIT_TOKENS_PER_SECOND
      return wait_seconds_needed


def _escape_markdownv2_reserved_characters_outside_code_spans(text: str) -> str:
  """Escape MarkdownV2 reserved punctuation, leaving backtick-delimited code
  spans (and their contents) untouched. Splitting on '`' puts code-span
  contents at odd indexes, which are passed through unchanged."""
  segments = text.split('`')
  escaped_segments = []
  for segment_index, segment in enumerate(segments):
    if segment_index % 2 == 0:
      escaped_segments.append(''.join(
        ('\\' + character) if character in _MARKDOWNV2_RESERVED_CHARACTERS_TO_ESCAPE else character
        for character in segment))
    else:
      escaped_segments.append(segment)
  return '`'.join(escaped_segments)


def _send_with_parse_mode_fallback(bot_token: str, chat_id, api_method: str,
                                   api_params: Dict) -> Tuple[bool, Any, bool]:
  """Rate-limited send that retries ONCE without parse_mode when Telegram
  rejects the message with a "can't parse entities" formatting error (LLMs
  routinely emit unescaped MarkdownV2). Returns (ok, result, fallback_used)."""
  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, api_method, api_params)
  if (not api_call_succeeded and api_params.get('parse_mode')
      and "can't parse entities" in str(api_result).lower()):
    MCPLogger.log(TOOL_LOG_NAME, f"{api_method} formatting error with parse_mode="
                   f"{api_params.get('parse_mode')}; retrying once as plain text: {api_result}")
    fallback_params = dict(api_params)
    fallback_params.pop('parse_mode', None)
    api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, api_method, fallback_params)
    return api_call_succeeded, api_result, True
  return api_call_succeeded, api_result, False


def _rate_limit_wait_until_send_token_acquired(chat_id: int, api_method: str,
                                               max_wait_seconds: float = 10.0) -> Tuple[bool, Optional[str]]:
  """Block until a send token is actually acquired for chat_id (a token refilled
  during our sleep can be consumed by another thread, so loop), waiting up to
  max_wait_seconds total. Returns (acquired, error_message_if_not)."""
  total_wait_so_far_seconds = 0.0
  while True:
    wait_needed = _rate_limit_acquire_send_permission(chat_id)
    if wait_needed <= 0.0:
      return True, None  # Token acquired - safe to send
    if total_wait_so_far_seconds + wait_needed > max_wait_seconds:
      return False, (f"Rate limited: would need to wait {wait_needed:.1f}s more "
                     f"(already waited {total_wait_so_far_seconds:.1f}s, max {max_wait_seconds}s)")
    MCPLogger.log(TOOL_LOG_NAME, f"Rate limit: sleeping {wait_needed:.2f}s before {api_method} to chat {chat_id}")
    time.sleep(wait_needed)
    total_wait_so_far_seconds += wait_needed


def _rate_limited_send(bot_token: str, chat_id: int, api_method: str, api_params: Dict,
                       max_wait_seconds: float = 10.0) -> Tuple[bool, Any]:
  """Send a message with automatic rate-limit back-pressure."""
  token_acquired, rate_error = _rate_limit_wait_until_send_token_acquired(chat_id, api_method, max_wait_seconds)
  if not token_acquired:
    return False, rate_error

  return _call_telegram_bot_api_method(bot_token, api_method, api_params)


# ============================================================================
# FEATURE: MULTI-PERSONA ROUTING FRAMEWORK
# ============================================================================

# In-memory persona registry (also sqlite-backed when persistence is on)
# Key: persona_id -> persona dict
_persona_registry: Dict[str, Dict] = {}
_persona_registry_lock = threading.Lock()


def _register_persona_in_memory(persona_id: str, display_name: str, system_prompt: str,
                                trigger_command: Optional[str] = None,
                                trigger_pattern: Optional[str] = None,
                                assigned_chat_ids: Optional[List[int]] = None,
                                is_default_persona: bool = False) -> Dict:
  """Add or update a persona in the in-memory registry.
  assigned_chat_ids members are coerced to int so JSON string ids still match
  incoming integer chat_ids."""
  persona = {
    "persona_id": persona_id,
    "display_name": display_name,
    "system_prompt": system_prompt,
    "trigger_command": trigger_command,
    "trigger_pattern": trigger_pattern,
    "assigned_chat_ids": _coerce_chat_id_list_members_to_int_where_possible(assigned_chat_ids),
    "is_default": is_default_persona,
  }
  with _persona_registry_lock:
    if is_default_persona:
      for existing_persona in _persona_registry.values():
        existing_persona["is_default"] = False
    _persona_registry[persona_id] = persona
  return persona


def _sqlite_store_persona(persona: Dict) -> None:
  """Persist persona to sqlite (no-op if persistence disabled)."""
  if not _sqlite_persistence_enabled_flag:
    return
  try:
    # Uses the shared _with_sqlite_db helper like every other writer (this was
    # the last holdout still doing the ensure-connection-then-lock dance inline)
    def _upsert_persona_row(conn: sqlite3.Connection):
      if persona.get("is_default"):
        conn.execute("UPDATE telegram_personas SET is_default=0")
      conn.execute(
        """INSERT INTO telegram_personas
           (persona_id, display_name, system_prompt, trigger_command, trigger_pattern,
            assigned_chat_ids, is_default)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(persona_id) DO UPDATE SET
            display_name=excluded.display_name, system_prompt=excluded.system_prompt,
            trigger_command=excluded.trigger_command, trigger_pattern=excluded.trigger_pattern,
            assigned_chat_ids=excluded.assigned_chat_ids, is_default=excluded.is_default""",
        (
          persona["persona_id"],
          persona["display_name"],
          persona["system_prompt"],
          persona.get("trigger_command"),
          persona.get("trigger_pattern"),
          json.dumps(persona.get("assigned_chat_ids")) if persona.get("assigned_chat_ids") else None,
          1 if persona.get("is_default") else 0,
        ),
      )
      conn.commit()
    _with_sqlite_db(_upsert_persona_row)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite store_persona error: {e}")


def _load_personas_from_sqlite() -> int:
  """Load all personas from sqlite into in-memory registry. Returns count loaded."""
  if not _sqlite_persistence_enabled_flag:
    return 0
  try:
    rows = _with_sqlite_db(lambda conn: conn.execute("SELECT * FROM telegram_personas").fetchall())
    loaded_count = 0
    for row in rows:
      persona_dict = dict(row)
      if persona_dict.get("assigned_chat_ids"):
        try:
          persona_dict["assigned_chat_ids"] = _coerce_chat_id_list_members_to_int_where_possible(
            json.loads(persona_dict["assigned_chat_ids"]))
        except (json.JSONDecodeError, TypeError):
          persona_dict["assigned_chat_ids"] = None
      persona_dict["is_default"] = bool(persona_dict.get("is_default"))
      with _persona_registry_lock:
        _persona_registry[persona_dict["persona_id"]] = persona_dict
      loaded_count += 1
    MCPLogger.log(TOOL_LOG_NAME, f"Loaded {loaded_count} personas from sqlite")
    return loaded_count
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite load_personas error: {e}")
    return 0


def _resolve_persona_for_incoming_message(formatted_message: Dict) -> Optional[Dict]:
  """Determine which persona should handle an incoming message.

  Resolution order:
    1. Explicit /command match (e.g. /poet triggers persona with trigger_command='poet')
    2. Chat assignment (persona assigned to this chat_id)
    3. Regex pattern match on message text
    4. Default persona (is_default=True)
    5. None (no persona matches)

  Within each tier, FIRST REGISTERED WINS: personas are scanned in registration
  order, so if several personas match at the same tier the earliest-registered
  one is chosen. Register higher-priority personas first (also documented in
  the tool readme).
  """
  text = formatted_message.get("text", "") or ""
  chat_id = formatted_message.get("chat_id")

  with _persona_registry_lock:
    all_personas = list(_persona_registry.values())

  # 1. Command match — text starts with /command
  if text.startswith("/"):
    command_word = text.split()[0][1:].split("@")[0].lower()
    for persona in all_personas:
      if persona.get("trigger_command") and persona["trigger_command"].lower() == command_word:
        return persona

  # 2. Chat assignment
  for persona in all_personas:
    assigned_ids = persona.get("assigned_chat_ids")
    if assigned_ids and chat_id in assigned_ids:
      return persona

  # 3. Regex pattern match
  for persona in all_personas:
    pattern = persona.get("trigger_pattern")
    if pattern:
      try:
        if re.search(pattern, text, re.IGNORECASE):
          return persona
      except re.error:
        pass

  # 4. Default persona
  for persona in all_personas:
    if persona.get("is_default"):
      return persona

  return None


# ============================================================================
# INTERNAL HELPER FUNCTIONS
# ============================================================================

def _create_short_hash_of_bot_token(bot_token: str) -> str:
  """Create a short SHA-256 hash of the bot token for use as dictionary key.
  Avoids storing raw tokens as dictionary keys in memory."""
  return hashlib.sha256(bot_token.encode()).hexdigest()[:16]


def _retrieve_telegram_bot_token_from_shared_config() -> Optional[str]:
  """Read the Telegram bot token from the shared nativemessaging.json config.
  Looks in settings[0].api_keys.TELEGRAM_BOT_TOKEN."""
  try:
    config_manager = get_config_manager()
    config = config_manager.load_config()
    telegram_bot_api_keys_section = SharedConfigManager.get_settings_value(config, 'api_keys', {})
    if isinstance(telegram_bot_api_keys_section, dict):
      return telegram_bot_api_keys_section.get('TELEGRAM_BOT_TOKEN')
    return None
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Error reading bot token from config: {e}")
    return None


def _store_telegram_bot_token_into_shared_config(bot_token: str) -> bool:
  """Store a Telegram bot token into the shared nativemessaging.json config
  at settings[0].api_keys.TELEGRAM_BOT_TOKEN."""
  try:
    config_manager = get_config_manager()
    config = config_manager.load_config()
    telegram_bot_api_keys_section = SharedConfigManager.ensure_settings_section(config, 'api_keys')
    telegram_bot_api_keys_section['TELEGRAM_BOT_TOKEN'] = bot_token
    return config_manager.save_config(config)
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Error storing bot token in config: {e}")
    return False


def _resolve_telegram_bot_token_from_params_or_config(params: Dict) -> Tuple[Optional[str], Optional[str]]:
  """Resolve the bot token: first from params, then from config.
  Returns (token, error_message). error_message is None on success."""
  token = params.get("bot_token")
  if token and isinstance(token, str) and token.strip():
    return token.strip(), None

  token = _retrieve_telegram_bot_token_from_shared_config()
  if token and isinstance(token, str) and token.strip():
    return token.strip(), None

  return None, ("No bot_token provided and none found in config. "
                "Either pass bot_token as a parameter, or use the set_bot_token operation first.")


def _call_telegram_bot_api_method(bot_token: str, api_method_name: str,
                                  request_parameters: Optional[Dict] = None,
                                  http_timeout_seconds: int = 30,
                                  max_retry_attempts: int = 3) -> Tuple[bool, Any]:
  """Execute a Telegram Bot API method via HTTPS POST (or GET if no params).

  Includes automatic retry with exponential backoff for transient network errors.
  Uses Connection: close header to prevent stale keep-alive connection reuse
  (which causes WinError 10054 on Windows after long-poll connections close).

  Args:
    bot_token: The bot's API token
    api_method_name: Telegram API method name (e.g. 'sendMessage', 'getUpdates')
    request_parameters: Optional dict of parameters for the API call
    http_timeout_seconds: HTTP request timeout
    max_retry_attempts: Maximum number of retry attempts for transient errors (default 3)

  Returns:
    Tuple of (success_bool, result_data_or_error_string)
  """
  # Both values are interpolated into the URL path; reject anything that could
  # alter the request path (e.g. method 'getMe?x=' or a token containing '/')
  if not isinstance(api_method_name, str) or not _TELEGRAM_API_METHOD_NAME_PATTERN.match(api_method_name):
    return False, (f"Invalid Telegram API method name '{api_method_name}': "
                   "only letters, digits and underscores are allowed")
  if not isinstance(bot_token, str) or not _TELEGRAM_BOT_TOKEN_FORMAT_PATTERN.match(bot_token):
    return False, ("Invalid bot token format: expected '<digits>:<30+ chars of A-Za-z0-9_->' "
                   "(as issued by @BotFather)")

  url = f"{TELEGRAM_BOT_API_BASE_URL}{bot_token}/{api_method_name}"

  # These error signatures indicate transient network issues worth retrying.
  # WinError 10054 = WSAECONNRESET (connection reset by peer) - common on Windows
  #   when a prior long-poll HTTP keep-alive connection was closed server-side
  #   and urllib's default opener tries to reuse the dead socket.
  # WinError 10053 = WSAECONNABORTED (software caused connection abort)
  # "RemoteDisconnected" = http.client.RemoteDisconnected (server closed connection)
  # "ConnectionReset" = ConnectionResetError on Linux/macOS
  transient_network_error_signatures = [
    'WinError 10054',       # WSAECONNRESET - connection reset by peer
    'WinError 10053',       # WSAECONNABORTED - connection abort
    'RemoteDisconnected',   # http.client.RemoteDisconnected
    'ConnectionReset',      # ConnectionResetError (Linux/macOS equivalent)
    'BrokenPipeError',      # Broken pipe on Unix
    'ConnectionAborted',    # ConnectionAbortedError
    'EOF occurred',         # SSL EOF during read
    'timed out',            # Socket timeout (not the HTTP timeout - this is TCP level)
  ]

  last_error_message = ""

  for current_attempt_number in range(max_retry_attempts):
    try:
      if request_parameters:
        encoded_request_body = json.dumps(request_parameters).encode('utf-8')
        http_request = urllib.request.Request(
          url,
          data=encoded_request_body,
          headers={
            'Content-Type': 'application/json',
            # Force Connection: close to prevent HTTP keep-alive reuse.
            # This is the primary fix for WinError 10054: after a long-poll
            # getUpdates holds a connection open for 30+ seconds, the server
            # may close it. Without this header, urllib's default opener can
            # try to reuse the dead socket on the next request.
            'Connection': 'close',
          }
        )
      else:
        http_request = urllib.request.Request(url, headers={'Connection': 'close'})

      with urllib.request.urlopen(http_request, timeout=http_timeout_seconds,
                                  context=_TELEGRAM_TLS_SSL_CONTEXT) as http_response:
        response_body = json.loads(http_response.read().decode('utf-8'))

        if response_body.get('ok'):
          # Log if this succeeded on a retry (so we know retries are working)
          if current_attempt_number > 0:
            MCPLogger.log(TOOL_LOG_NAME, f"API call {api_method_name} succeeded on retry attempt {current_attempt_number + 1}")
          return True, response_body.get('result')
        else:
          # API-level errors (e.g. bad chat_id, message too long) are NOT transient - don't retry
          return False, response_body.get('description', 'Unknown Telegram API error')

    except urllib.error.HTTPError as http_error:
      # HTTP errors (4xx, 5xx) - retry on 5xx (server errors) and 429 (rate limit)
      http_status_code = http_error.code
      retry_after_seconds_from_api = None
      try:
        error_response_body = json.loads(http_error.read().decode('utf-8'))
        last_error_message = error_response_body.get('description', f'HTTP {http_status_code}: {http_error.reason}')
        error_parameters = error_response_body.get('parameters')
        if isinstance(error_parameters, dict):
          retry_after_seconds_from_api = error_parameters.get('retry_after')
      except Exception:
        last_error_message = f'HTTP {http_status_code}: {http_error.reason}'

      if http_status_code == 429 and current_attempt_number < max_retry_attempts - 1:
        # Honor Telegram's parameters.retry_after, bounded so we never sleep excessively
        if isinstance(retry_after_seconds_from_api, (int, float)) and not isinstance(retry_after_seconds_from_api, bool):
          retry_delay_seconds = float(retry_after_seconds_from_api)
        else:
          retry_delay_seconds = 0.5 * (2 ** current_attempt_number)
        if retry_delay_seconds > _HTTP_429_MAX_RETRY_AFTER_SLEEP_SECONDS:
          return False, (f"{last_error_message} (retry_after={retry_after_seconds_from_api}s exceeds "
                         f"the {_HTTP_429_MAX_RETRY_AFTER_SLEEP_SECONDS:.0f}s bound; not retrying)")
        MCPLogger.log(TOOL_LOG_NAME, f"HTTP 429 from Telegram API on {api_method_name}, honoring retry_after: "
                       f"sleeping {retry_delay_seconds}s (attempt {current_attempt_number + 1}/{max_retry_attempts})")
        time.sleep(retry_delay_seconds)
        continue
      elif http_status_code >= 500 and current_attempt_number < max_retry_attempts - 1:
        # Server error - worth retrying
        retry_delay_seconds = 0.5 * (2 ** current_attempt_number)  # 0.5s, 1s, 2s
        MCPLogger.log(TOOL_LOG_NAME, f"HTTP {http_status_code} from Telegram API on {api_method_name}, "
                       f"retrying in {retry_delay_seconds}s (attempt {current_attempt_number + 1}/{max_retry_attempts})")
        time.sleep(retry_delay_seconds)
        continue
      else:
        # Client error (4xx) or final attempt - return the error
        return False, last_error_message

    except (urllib.error.URLError, OSError, ConnectionError) as network_error:
      # Network-level errors - check if transient and worth retrying
      error_string = str(network_error)
      error_is_transient = any(signature in error_string for signature in transient_network_error_signatures)

      if error_is_transient and current_attempt_number < max_retry_attempts - 1:
        retry_delay_seconds = 0.5 * (2 ** current_attempt_number)  # 0.5s, 1s, 2s
        MCPLogger.log(TOOL_LOG_NAME, f"Transient network error on {api_method_name}: {error_string}. "
                       f"Retrying in {retry_delay_seconds}s (attempt {current_attempt_number + 1}/{max_retry_attempts})")
        time.sleep(retry_delay_seconds)
        last_error_message = f'Network error contacting Telegram API: {error_string}'
        continue
      else:
        # Non-transient error or final attempt
        return False, f'Network error contacting Telegram API: {error_string}'

    except Exception as general_error:
      # Unexpected errors - check if they look transient
      error_string = str(general_error)
      error_is_transient = any(signature in error_string for signature in transient_network_error_signatures)

      if error_is_transient and current_attempt_number < max_retry_attempts - 1:
        retry_delay_seconds = 0.5 * (2 ** current_attempt_number)
        MCPLogger.log(TOOL_LOG_NAME, f"Transient error on {api_method_name}: {error_string}. "
                       f"Retrying in {retry_delay_seconds}s (attempt {current_attempt_number + 1}/{max_retry_attempts})")
        time.sleep(retry_delay_seconds)
        last_error_message = f'Error calling Telegram API method {api_method_name}: {error_string}'
        continue
      else:
        return False, f'Error calling Telegram API method {api_method_name}: {error_string}'

  # All retries exhausted
  return False, f'All {max_retry_attempts} attempts failed for {api_method_name}. Last error: {last_error_message}'


def _call_telegram_bot_api_method_with_local_file_upload(bot_token: str, api_method_name: str,
                                                         file_field_name: str, local_file_path: str,
                                                         extra_api_parameters: Dict,
                                                         max_upload_size_bytes: int,
                                                         http_timeout_seconds: int = 120) -> Tuple[bool, Any]:
  """Upload a local file to a Telegram Bot API method via multipart/form-data,
  built manually with only the standard library (no external dependencies).

  Args:
    bot_token: The bot's API token
    api_method_name: e.g. 'sendPhoto', 'sendDocument'
    file_field_name: the multipart field Telegram expects ('photo', 'document', ...)
    local_file_path: path to the file on this server's filesystem
    extra_api_parameters: other API params (chat_id, caption, ...) sent as form fields
    max_upload_size_bytes: reject files larger than this before reading them
    http_timeout_seconds: HTTP timeout (uploads can be slow, default 120s)

  Returns:
    Tuple of (success_bool, result_data_or_error_string)
  """
  if not isinstance(api_method_name, str) or not _TELEGRAM_API_METHOD_NAME_PATTERN.match(api_method_name):
    return False, f"Invalid Telegram API method name '{api_method_name}'"
  if not isinstance(bot_token, str) or not _TELEGRAM_BOT_TOKEN_FORMAT_PATTERN.match(bot_token):
    return False, "Invalid bot token format"

  upload_path = Path(local_file_path).expanduser()
  if not upload_path.is_file():
    return False, f"File not found: {local_file_path}"
  file_size_bytes = upload_path.stat().st_size
  if file_size_bytes > max_upload_size_bytes:
    return False, (f"File is {file_size_bytes} bytes which exceeds the "
                   f"{max_upload_size_bytes} byte Telegram upload limit for this operation")

  try:
    file_content_bytes = upload_path.read_bytes()
  except OSError as read_error:
    return False, f"Cannot read file {local_file_path}: {read_error}"

  multipart_boundary = f"----AuraFridayTelegramUpload{hashlib.sha256(os.urandom(16)).hexdigest()[:24]}"
  body_parts: List[bytes] = []

  for field_name, field_value in (extra_api_parameters or {}).items():
    if field_value is None:
      continue
    # Non-scalar params (reply_markup etc) are sent as JSON strings per Telegram docs
    if isinstance(field_value, (dict, list)):
      serialized_field_value = json.dumps(field_value)
    elif isinstance(field_value, bool):
      serialized_field_value = 'true' if field_value else 'false'
    else:
      serialized_field_value = str(field_value)
    body_parts.append(
      (f"--{multipart_boundary}\r\n"
       f"Content-Disposition: form-data; name=\"{field_name}\"\r\n\r\n"
       f"{serialized_field_value}\r\n").encode('utf-8')
    )

  # Keep the filename header safe: strip path components and non-ASCII-safe chars
  safe_upload_filename = re.sub(r'[^A-Za-z0-9._-]', '_', upload_path.name) or 'upload.bin'
  body_parts.append(
    (f"--{multipart_boundary}\r\n"
     f"Content-Disposition: form-data; name=\"{file_field_name}\"; filename=\"{safe_upload_filename}\"\r\n"
     f"Content-Type: application/octet-stream\r\n\r\n").encode('utf-8')
  )
  body_parts.append(file_content_bytes)
  body_parts.append(f"\r\n--{multipart_boundary}--\r\n".encode('utf-8'))

  multipart_request_body = b''.join(body_parts)
  url = f"{TELEGRAM_BOT_API_BASE_URL}{bot_token}/{api_method_name}"

  try:
    http_request = urllib.request.Request(
      url,
      data=multipart_request_body,
      headers={
        'Content-Type': f'multipart/form-data; boundary={multipart_boundary}',
        'Connection': 'close',
      }
    )
    with urllib.request.urlopen(http_request, timeout=http_timeout_seconds,
                                context=_TELEGRAM_TLS_SSL_CONTEXT) as http_response:
      response_body = json.loads(http_response.read().decode('utf-8'))
      if response_body.get('ok'):
        return True, response_body.get('result')
      return False, response_body.get('description', 'Unknown Telegram API error')
  except urllib.error.HTTPError as http_error:
    try:
      error_response_body = json.loads(http_error.read().decode('utf-8'))
      return False, error_response_body.get('description', f'HTTP {http_error.code}: {http_error.reason}')
    except Exception:
      return False, f'HTTP {http_error.code}: {http_error.reason}'
  except Exception as upload_error:
    return False, f'Error uploading file to Telegram API method {api_method_name}: {upload_error}'


def _record_chat_info_from_telegram_message(bot_token: str, message: Dict):
  """Extract and store chat information from a received Telegram message
  into the known-chats registry for this bot (in-memory + sqlite if enabled)."""
  chat = message.get('chat', {})
  chat_id = chat.get('id')
  if not chat_id:
    return

  token_hash = _create_short_hash_of_bot_token(bot_token)

  chat_info = {
    'id': chat_id,
    'type': chat.get('type', 'unknown'),
    'title': chat.get('title'),
    'first_name': chat.get('first_name'),
    'last_name': chat.get('last_name'),
    'username': chat.get('username'),
    'last_message_date': message.get('date'),
    'last_message_text': message.get('text', ''),
  }

  with _telegram_global_state_lock:
    if token_hash not in _telegram_known_chats_per_bot:
      _telegram_known_chats_per_bot[token_hash] = {}
    _telegram_known_chats_per_bot[token_hash][chat_id] = chat_info

  _sqlite_upsert_known_chat(token_hash, chat_info)


def _append_message_to_telegram_history(bot_token: str, message: Dict):
  """Store a received message in the in-memory history deque for this bot."""
  token_hash = _create_short_hash_of_bot_token(bot_token)

  with _telegram_global_state_lock:
    if token_hash not in _telegram_received_message_history_per_bot:
      _telegram_received_message_history_per_bot[token_hash] = deque(
        maxlen=MAX_TELEGRAM_MESSAGE_HISTORY_PER_BOT
      )
    _telegram_received_message_history_per_bot[token_hash].append(message)


def _format_telegram_message_for_ai_display(message: Dict) -> Dict:
  """Format a raw Telegram message into a clean dict for AI consumption."""
  chat = message.get('chat', {})
  from_user = message.get('from', {})

  formatted = {
    'message_id': message.get('message_id'),
    'date': message.get('date'),
    'date_human_readable': datetime.fromtimestamp(
      message.get('date', 0), tz=timezone.utc
    ).strftime('%Y-%m-%d %H:%M:%S UTC') if message.get('date') else None,
    'chat_id': chat.get('id'),
    'chat_type': chat.get('type'),
    'chat_title_or_name': chat.get('title') or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip(),
    'from_user_id': from_user.get('id'),
    'from_username': from_user.get('username'),
    'from_display_name': f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip(),
    'from_is_bot': from_user.get('is_bot', False),
    # Fall back to media caption so photo/document captions reach routing and history
    'text': message.get('text') or message.get('caption'),
  }

  # Include reply info if present
  reply_to = message.get('reply_to_message')
  if reply_to:
    formatted['reply_to_message_id'] = reply_to.get('message_id')
    formatted['reply_to_text_preview'] = (reply_to.get('text', '') or '')[:100]

  # Include photo info if present (Telegram sends an array of sizes; last = best quality)
  if message.get('photo'):
    formatted['has_photo'] = True
    formatted['photo_caption'] = message.get('caption')
    best_photo_size = message['photo'][-1]
    formatted['photo_file_id'] = best_photo_size.get('file_id')
    formatted['photo_file_unique_id'] = best_photo_size.get('file_unique_id')
    formatted['photo_width'] = best_photo_size.get('width')
    formatted['photo_height'] = best_photo_size.get('height')

  # Include document info if present
  if message.get('document'):
    doc = message['document']
    formatted['has_document'] = True
    formatted['document_file_name'] = doc.get('file_name')
    formatted['document_mime_type'] = doc.get('mime_type')
    formatted['document_file_id'] = doc.get('file_id')

  # Include sticker info
  if message.get('sticker'):
    formatted['has_sticker'] = True
    formatted['sticker_emoji'] = message['sticker'].get('emoji')

  # Include voice/audio info
  if message.get('voice'):
    formatted['has_voice'] = True
    formatted['voice_duration_seconds'] = message['voice'].get('duration')

  if message.get('audio'):
    formatted['has_audio'] = True
    formatted['audio_title'] = message['audio'].get('title')
    formatted['audio_performer'] = message['audio'].get('performer')

  # Include video info
  if message.get('video'):
    formatted['has_video'] = True
    formatted['video_duration_seconds'] = message['video'].get('duration')

  # Include video note (round video message) info
  if message.get('video_note'):
    formatted['has_video_note'] = True
    formatted['video_note_duration_seconds'] = message['video_note'].get('duration')

  # Include animation (GIF) info
  if message.get('animation'):
    formatted['has_animation'] = True
    formatted['animation_file_name'] = message['animation'].get('file_name')

  # Include poll info
  if message.get('poll'):
    poll = message['poll']
    formatted['has_poll'] = True
    formatted['poll_question'] = poll.get('question')
    formatted['poll_options'] = [o.get('text') for o in poll.get('options', [])]
    formatted['poll_is_anonymous'] = poll.get('is_anonymous')

  # Include venue info (named place; distinct from bare location)
  if message.get('venue'):
    venue = message['venue']
    formatted['has_venue'] = True
    formatted['venue_title'] = venue.get('title')
    formatted['venue_address'] = venue.get('address')

  # Include dice (animated emoji with random value)
  if message.get('dice'):
    formatted['has_dice'] = True
    formatted['dice_emoji'] = message['dice'].get('emoji')
    formatted['dice_value'] = message['dice'].get('value')

  # Include edit timestamp for edited messages
  if message.get('edit_date'):
    formatted['edit_date'] = message.get('edit_date')

  # Include location info
  if message.get('location'):
    loc = message['location']
    formatted['has_location'] = True
    formatted['location_latitude'] = loc.get('latitude')
    formatted['location_longitude'] = loc.get('longitude')

  # Include contact info
  if message.get('contact'):
    contact = message['contact']
    formatted['has_contact'] = True
    formatted['contact_phone_number'] = contact.get('phone_number')
    formatted['contact_first_name'] = contact.get('first_name')

  # Include new_chat_members / left_chat_member service messages
  if message.get('new_chat_members'):
    formatted['new_chat_members'] = [
      {'id': m.get('id'), 'username': m.get('username'), 'is_bot': m.get('is_bot')}
      for m in message['new_chat_members']
    ]
  if message.get('left_chat_member'):
    left = message['left_chat_member']
    formatted['left_chat_member'] = {
      'id': left.get('id'), 'username': left.get('username'), 'is_bot': left.get('is_bot')
    }

  # Include entities (commands, mentions, URLs etc)
  if message.get('entities'):
    formatted['entities'] = [
      {'type': e.get('type'), 'offset': e.get('offset'), 'length': e.get('length'),
       'url': e.get('url'), 'user': e.get('user')}
      for e in message['entities']
    ]

  return formatted


# Event types stored in history as already-formatted dicts (raw Telegram
# messages never carry a top-level 'type' key, so this is unambiguous)
_PREFORMATTED_HISTORY_EVENT_TYPES = ("callback_query", "chat_join_request", "my_chat_member_updated")


def _format_history_entry_for_ai_display_preserving_preformatted_events(stored_history_entry: Dict) -> Dict:
  """Format one in-memory history entry for AI display. Dispatched event dicts
  (callback_query, chat_join_request, my_chat_member_updated) are stored already
  formatted and pass through unchanged; raw Telegram messages get formatted."""
  if stored_history_entry.get('type') in _PREFORMATTED_HISTORY_EVENT_TYPES:
    return stored_history_entry
  return _format_telegram_message_for_ai_display(stored_history_entry)


def _process_telegram_updates_and_extract_messages(bot_token: str, updates: List[Dict]) -> List[Dict]:
  """Process a list of Telegram Update objects: track chats, store history,
  update offset, and return formatted messages for AI display."""
  token_hash = _create_short_hash_of_bot_token(bot_token)
  formatted_messages_for_ai = []

  for single_update in updates:
    update_id = single_update.get('update_id', 0)

    # Update the offset to be one past the highest update_id we've seen
    with _telegram_global_state_lock:
      current_offset = _telegram_update_offset_per_bot.get(token_hash, 0)
      if update_id >= current_offset:
        _telegram_update_offset_per_bot[token_hash] = update_id + 1

    # Extract the message (could be in 'message', 'edited_message', 'channel_post', etc.)
    message = (single_update.get('message')
               or single_update.get('edited_message')
               or single_update.get('channel_post')
               or single_update.get('edited_channel_post'))

    if message:
      _record_chat_info_from_telegram_message(bot_token, message)
      _append_message_to_telegram_history(bot_token, message)

      formatted = _format_telegram_message_for_ai_display(message)
      formatted['update_id'] = update_id
      # Tag edited messages
      if single_update.get('edited_message') or single_update.get('edited_channel_post'):
        formatted['was_edited'] = True

      # Resolve persona for this message
      matched_persona = _resolve_persona_for_incoming_message(formatted)
      if matched_persona:
        formatted['matched_persona_id'] = matched_persona['persona_id']
        formatted['matched_persona_name'] = matched_persona['display_name']

      formatted_messages_for_ai.append(formatted)
      _sqlite_store_message(token_hash, formatted, message)

    # Handle callback queries (inline button presses)
    callback_query = single_update.get('callback_query')
    if callback_query:
      cb_from = callback_query.get('from', {})
      cb_formatted = {
        'type': 'callback_query',
        'update_id': update_id,
        'callback_query_id': callback_query.get('id'),
        'from_user_id': cb_from.get('id'),
        'from_username': cb_from.get('username'),
        'from_is_bot': cb_from.get('is_bot', False),
        'data': callback_query.get('data'),
        'message_id': callback_query.get('message', {}).get('message_id'),
        'chat_id': callback_query.get('message', {}).get('chat', {}).get('id'),
      }
      formatted_messages_for_ai.append(cb_formatted)
      # Anything dispatched is also stored in BOTH in-memory history and sqlite
      _append_message_to_telegram_history(bot_token, cb_formatted)
      _sqlite_store_message(token_hash, cb_formatted, callback_query)

    # Handle chat_join_request updates
    join_request = single_update.get('chat_join_request')
    if join_request:
      req_from = join_request.get('from', {})
      req_chat = join_request.get('chat', {})
      join_request_formatted = {
        'type': 'chat_join_request',
        'update_id': update_id,
        'chat_id': req_chat.get('id'),
        'chat_title': req_chat.get('title'),
        'from_user_id': req_from.get('id'),
        'from_username': req_from.get('username'),
        'from_is_bot': req_from.get('is_bot', False),
        'from_display_name': f"{req_from.get('first_name', '')} {req_from.get('last_name', '')}".strip(),
        'date': join_request.get('date'),
        'invite_link': join_request.get('invite_link', {}).get('invite_link') if join_request.get('invite_link') else None,
        'bio': join_request.get('bio'),
      }
      formatted_messages_for_ai.append(join_request_formatted)
      _append_message_to_telegram_history(bot_token, join_request_formatted)
      _sqlite_store_message(token_hash, join_request_formatted, join_request)

    # Handle my_chat_member updates (bot added/removed from chat, permissions changed)
    my_chat_member = single_update.get('my_chat_member')
    if my_chat_member:
      mcm_chat = my_chat_member.get('chat', {})
      mcm_from = my_chat_member.get('from', {})
      mcm_old = my_chat_member.get('old_chat_member', {})
      mcm_new = my_chat_member.get('new_chat_member', {})
      my_chat_member_formatted = {
        'type': 'my_chat_member_updated',
        'update_id': update_id,
        'chat_id': mcm_chat.get('id'),
        'chat_type': mcm_chat.get('type'),
        'chat_title': mcm_chat.get('title'),
        'changed_by_user_id': mcm_from.get('id'),
        'changed_by_username': mcm_from.get('username'),
        'old_status': mcm_old.get('status'),
        'new_status': mcm_new.get('status'),
        'date': my_chat_member.get('date'),
      }
      formatted_messages_for_ai.append(my_chat_member_formatted)
      _append_message_to_telegram_history(bot_token, my_chat_member_formatted)
      _sqlite_store_message(token_hash, my_chat_member_formatted, my_chat_member)

  return formatted_messages_for_ai


# ============================================================================
# BACKGROUND POLLING (persistent listener)
# ============================================================================

# Full list of update types the background poller requests
_BACKGROUND_POLLER_ALLOWED_UPDATE_TYPES = [
  'message', 'edited_message', 'channel_post', 'edited_channel_post',
  'callback_query', 'chat_join_request', 'my_chat_member',
]

def _telegram_background_polling_thread_function(bot_token: str,
                                                 poller_stop_requested_event: threading.Event,
                                                 poller_registry_entry: Dict,
                                                 long_poll_timeout_seconds: int = 30):
  """Background thread that continuously polls Telegram for new messages.
  Messages are stored in the history deque for later retrieval by the AI.

  Each poller checks its OWN stop event (not the shared registry entry), so a
  stopped thread can never adopt a newer poller's registry entry and keep running.
  Error state (last_error, consecutive_error_count) is written into this poller's
  own registry entry dict so get_listening_status can report health; after
  _BACKGROUND_POLLER_AUTO_STOP_AFTER_CONSECUTIVE_FAILURES consecutive failures
  the poller stops itself and records why."""
  token_hash = _create_short_hash_of_bot_token(bot_token)
  MCPLogger.log(TOOL_LOG_NAME, f"Background poller started for bot hash {token_hash}")

  consecutive_error_count = 0
  max_backoff_seconds = 60

  while True:
    # Check if THIS poller instance was asked to stop
    if poller_stop_requested_event.is_set():
      MCPLogger.log(TOOL_LOG_NAME, f"Background poller stopping for bot hash {token_hash}")
      break

    # Build getUpdates parameters
    params = {
      'timeout': long_poll_timeout_seconds,
      'allowed_updates': _BACKGROUND_POLLER_ALLOWED_UPDATE_TYPES,
    }

    with _telegram_global_state_lock:
      current_offset = _telegram_update_offset_per_bot.get(token_hash)
      if current_offset:
        params['offset'] = current_offset

    # Call getUpdates with long polling
    api_call_succeeded, api_result = _call_telegram_bot_api_method(
      bot_token, 'getUpdates', params,
      http_timeout_seconds=long_poll_timeout_seconds + 10  # HTTP timeout > long poll timeout
    )

    if api_call_succeeded:
      consecutive_error_count = 0
      with _telegram_global_state_lock:
        poller_registry_entry['consecutive_error_count'] = 0
        poller_registry_entry['last_error'] = None
      if api_result:  # Non-empty list of updates
        formatted_messages = _process_telegram_updates_and_extract_messages(bot_token, api_result)
        MCPLogger.log(TOOL_LOG_NAME, f"Background poller received {len(api_result)} update(s)")
        if formatted_messages:
          _dispatch_event_callbacks(token_hash, formatted_messages)
    else:
      consecutive_error_count += 1
      with _telegram_global_state_lock:
        poller_registry_entry['consecutive_error_count'] = consecutive_error_count
        poller_registry_entry['last_error'] = str(api_result)

      if consecutive_error_count >= _BACKGROUND_POLLER_AUTO_STOP_AFTER_CONSECUTIVE_FAILURES:
        stop_reason = (f"Auto-stopped after {consecutive_error_count} consecutive polling failures. "
                       f"Last error: {api_result}")
        MCPLogger.log(TOOL_LOG_NAME, f"Background poller for bot hash {token_hash}: {stop_reason}")
        with _telegram_global_state_lock:
          poller_registry_entry['running'] = False
          poller_registry_entry['stopped_reason'] = stop_reason
        break

      # Cap the exponent BEFORE computing 2**n so a long outage cannot produce
      # a huge intermediate value
      backoff_delay = min(2 ** min(consecutive_error_count, _BACKGROUND_POLLER_BACKOFF_EXPONENT_CAP),
                          max_backoff_seconds)
      MCPLogger.log(TOOL_LOG_NAME, f"Background poller error (attempt {consecutive_error_count}): {api_result}. Backing off {backoff_delay}s")
      # Sleep in an interruptible way so stop_listening takes effect during backoff
      if poller_stop_requested_event.wait(timeout=backoff_delay):
        continue  # Loop back so the stop check at the top exits cleanly

  MCPLogger.log(TOOL_LOG_NAME, f"Background poller exited for bot hash {token_hash}")


# ============================================================================
# OPERATION HANDLERS
# ============================================================================

def handle_set_bot_token_operation(params: Dict) -> Dict:
  """Handle set_bot_token operation - stores token in config and validates it."""
  bot_token = params.get("bot_token")
  if not bot_token or not isinstance(bot_token, str) or not bot_token.strip():
    return create_error_response("Parameter 'bot_token' is required. Provide a Telegram Bot API token (from @BotFather).", with_readme=True)

  bot_token = bot_token.strip()

  # Validate token by calling getMe
  MCPLogger.log(TOOL_LOG_NAME, "Validating bot token via getMe...")
  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'getMe')

  if not api_call_succeeded:
    return create_error_response(f"Invalid bot token - Telegram API rejected it: {api_result}", with_readme=False)

  # Store in config
  token_stored_successfully = _store_telegram_bot_token_into_shared_config(bot_token)

  bot_username = api_result.get('username', 'unknown')
  bot_display_name = f"{api_result.get('first_name', '')} {api_result.get('last_name', '')}".strip()

  storage_status = "saved to config" if token_stored_successfully else "NOT saved to config (error)"

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "success",
        "bot_username": f"@{bot_username}",
        "bot_display_name": bot_display_name,
        "bot_id": api_result.get('id'),
        "token_storage_status": storage_status,
        "can_join_groups": api_result.get('can_join_groups', False),
        "can_read_all_group_messages": api_result.get('can_read_all_group_messages', False),
      }, indent=2)
    }],
    "isError": False
  }


def handle_get_bot_info_operation(params: Dict) -> Dict:
  """Handle get_bot_info operation - returns bot identity via getMe."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'getMe')
  if not api_call_succeeded:
    return create_error_response(f"getMe failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "bot_id": api_result.get('id'),
        "bot_username": f"@{api_result.get('username', 'unknown')}",
        "bot_display_name": f"{api_result.get('first_name', '')} {api_result.get('last_name', '')}".strip(),
        "is_bot": api_result.get('is_bot'),
        "can_join_groups": api_result.get('can_join_groups'),
        "can_read_all_group_messages": api_result.get('can_read_all_group_messages'),
        "supports_inline_queries": api_result.get('supports_inline_queries'),
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_message_operation(params: Dict) -> Dict:
  """Handle send_message operation - sends a text message to a Telegram chat.
  Supports inline keyboards via reply_markup parameter."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  text = params.get("text")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required. Use get_updates or list_known_chats to find chat IDs.", with_readme=False)
  if not text:
    return create_error_response("Parameter 'text' is required. Provide the message text to send.", with_readme=False)

  # Optional: parse mode (HTML or Markdown)
  parse_mode = params.get("parse_mode")

  # Optional: escape MarkdownV2 reserved punctuation outside code spans so LLM
  # text does not need manual escaping
  if params.get("auto_escape") and parse_mode == "MarkdownV2":
    text = _escape_markdownv2_reserved_characters_outside_code_spans(text)

  # Length check runs AFTER escaping (escape backslashes count toward the limit)
  if len(text) > _TELEGRAM_MAX_MESSAGE_TEXT_LENGTH_CHARS:
    return create_error_response(
      f"Message text is {len(text)} characters; Telegram's sendMessage limit is "
      f"{_TELEGRAM_MAX_MESSAGE_TEXT_LENGTH_CHARS}. Split the text and send multiple messages.",
      with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'text': text,
  }
  if parse_mode:
    api_params['parse_mode'] = parse_mode

  # Optional: reply to a specific message
  reply_to_message_id = params.get("reply_to_message_id")
  if reply_to_message_id:
    api_params['reply_parameters'] = {'message_id': reply_to_message_id}

  # Optional: disable link preview
  if params.get("disable_link_preview"):
    api_params['link_preview_options'] = {'is_disabled': True}

  # Optional: disable notification sound for the recipient
  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  # Optional: inline keyboard markup (list of lists of button dicts)
  reply_markup = params.get("reply_markup")
  if reply_markup:
    api_params['reply_markup'] = reply_markup

  MCPLogger.log(TOOL_LOG_NAME, f"Sending message to chat_id={chat_id}, text length={len(text)}")

  api_call_succeeded, api_result, parse_mode_fallback_used = _send_with_parse_mode_fallback(
    bot_token, chat_id, 'sendMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"sendMessage failed: {api_result}", with_readme=False)

  sent_message_id = api_result.get('message_id')
  MCPLogger.log(TOOL_LOG_NAME, f"Message sent successfully, message_id={sent_message_id}")

  response_payload = {
    "status": "sent",
    "message_id": sent_message_id,
    "chat_id": chat_id,
    "date": api_result.get('date'),
  }
  if parse_mode_fallback_used:
    response_payload["parse_mode_fallback"] = (
      "Message was rejected with a formatting parse error and re-sent as plain text "
      "(no parse_mode). Escape reserved characters or use auto_escape for MarkdownV2.")

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(response_payload, indent=2)
    }],
    "isError": False
  }


def handle_get_updates_operation(params: Dict) -> Dict:
  """Handle get_updates operation - polls Telegram for new messages.
  Uses long-polling with configurable timeout. Tracks offset to only return new messages."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)
  long_poll_timeout = params.get("timeout", 5)  # Default 5 seconds

  # Clamp timeout to reasonable range
  long_poll_timeout = max(0, min(long_poll_timeout, 30))

  # Telegram allows only ONE getUpdates consumer per bot; if our background
  # poller is running, calling the API here would 409, so serve from the
  # in-memory history the poller is already filling instead
  with _telegram_global_state_lock:
    poller_info = _telegram_background_pollers_per_bot.get(token_hash)
    background_poller_is_running = bool(poller_info and poller_info.get('running'))
    history_entries = (list(_telegram_received_message_history_per_bot.get(token_hash, deque()))
                       if background_poller_is_running else [])

  if background_poller_is_running:
    recent_entries = history_entries[-50:]  # Most recent 50; use get_message_history for more control
    formatted_messages = [_format_history_entry_for_ai_display_preserving_preformatted_events(m)
                          for m in recent_entries]
    return {
      "content": [{
        "type": "text",
        "text": json.dumps({
          "update_count": len(formatted_messages),
          "messages": formatted_messages,
          "source": "in_memory_history",
          "note": ("Background listener is running, so get_updates served recent messages from local "
                   "history instead of calling the Telegram API (Telegram allows only one getUpdates "
                   "consumer per bot; a second gets HTTP 409). Use get_message_history (limit/chat_id "
                   "filters) or get_callback_events for finer control, or stop_listening to poll directly.")
        }, indent=2)
      }],
      "isError": False
    }

  api_params = {
    'timeout': long_poll_timeout,
    'allowed_updates': _BACKGROUND_POLLER_ALLOWED_UPDATE_TYPES,
  }

  # Optional batch-size passthrough (Telegram getUpdates supports limit 1-100).
  # A dedicated param name is used because 'limit' has a schema default of 20
  # (for the history operations) that would otherwise always be injected here.
  updates_batch_limit = params.get("updates_limit")
  if updates_batch_limit is not None:
    api_params['limit'] = max(1, min(updates_batch_limit, 100))

  # Use stored offset to only get new updates
  with _telegram_global_state_lock:
    current_offset = _telegram_update_offset_per_bot.get(token_hash)
    if current_offset:
      api_params['offset'] = current_offset

  MCPLogger.log(TOOL_LOG_NAME, f"Polling for updates (timeout={long_poll_timeout}s, offset={api_params.get('offset', 'none')})")

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'getUpdates', api_params,
    http_timeout_seconds=long_poll_timeout + 10
  )

  if not api_call_succeeded:
    # Turn Telegram 409 conflicts into actionable guidance
    error_text_lowercase = str(api_result).lower()
    actionable_guidance = ""
    if "webhook is active" in error_text_lowercase:
      actionable_guidance = (" A webhook is set for this bot, which blocks getUpdates. Remove it with "
                             "the delete_webhook operation, then retry.")
    elif ("terminated by other getupdates" in error_text_lowercase
          or "conflict" in error_text_lowercase or "409" in str(api_result)):
      actionable_guidance = (" Another getUpdates consumer is polling this bot (another process, server, "
                             "or listener). Stop that consumer, or use start_listening here and read via "
                             "get_message_history / get_callback_events instead.")
    return create_error_response(f"getUpdates failed: {api_result}{actionable_guidance}", with_readme=False)

  # Process updates and format messages
  formatted_messages = _process_telegram_updates_and_extract_messages(bot_token, api_result or [])

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "update_count": len(api_result or []),
        "messages": formatted_messages,
        "note": "Call get_updates again to check for newer messages" if formatted_messages else "No new messages. Call get_updates again later or increase timeout for long-polling."
      }, indent=2)
    }],
    "isError": False
  }


def handle_list_known_chats_operation(params: Dict) -> Dict:
  """Handle list_known_chats - returns all chats that have interacted with the bot."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  with _telegram_global_state_lock:
    known_chats = _telegram_known_chats_per_bot.get(token_hash, {})
    chats_list = list(known_chats.values())

  if not chats_list:
    return {
      "content": [{
        "type": "text",
        "text": json.dumps({
          "chats": [],
          "note": "No chats known yet. The bot needs to receive at least one message first. Ask someone to message the bot, or use get_updates to poll for messages."
        }, indent=2)
      }],
      "isError": False
    }

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "chat_count": len(chats_list),
        "chats": chats_list
      }, indent=2)
    }],
    "isError": False
  }


def handle_get_message_history_operation(params: Dict) -> Dict:
  """Handle get_message_history - returns stored messages from the in-memory history."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)
  max_messages_to_return = params.get("limit", 20)
  # Clamp so 0/negative limits cannot return everything (or slice strangely)
  max_messages_to_return = max(1, min(max_messages_to_return, _HISTORY_QUERY_LIMIT_MAX))
  filter_chat_id = params.get("chat_id")

  with _telegram_global_state_lock:
    history_deque = _telegram_received_message_history_per_bot.get(token_hash, deque())
    all_messages = list(history_deque)

  # Format messages for AI (dispatched event dicts are stored pre-formatted)
  formatted = [_format_history_entry_for_ai_display_preserving_preformatted_events(msg) for msg in all_messages]

  # Apply chat_id filter if specified
  if filter_chat_id:
    formatted = [m for m in formatted if m.get('chat_id') == filter_chat_id]

  # Return only the most recent N messages
  recent_messages = formatted[-max_messages_to_return:]

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "total_stored_message_count": len(all_messages),
        "returned_message_count": len(recent_messages),
        "messages": recent_messages
      }, indent=2)
    }],
    "isError": False
  }


def handle_edit_message_operation(params: Dict) -> Dict:
  """Handle edit_message operation - edits a previously sent message."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  message_id = params.get("message_id")
  text = params.get("text")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)
  if not text:
    return create_error_response("Parameter 'text' is required.", with_readme=False)
  if len(text) > _TELEGRAM_MAX_MESSAGE_TEXT_LENGTH_CHARS:
    return create_error_response(
      f"Message text is {len(text)} characters; Telegram's editMessageText limit is "
      f"{_TELEGRAM_MAX_MESSAGE_TEXT_LENGTH_CHARS}.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'message_id': message_id,
    'text': text,
  }

  parse_mode = params.get("parse_mode")
  if parse_mode:
    api_params['parse_mode'] = parse_mode

  # Optional: update inline keyboard
  reply_markup = params.get("reply_markup")
  if reply_markup:
    api_params['reply_markup'] = reply_markup

  # Send-class call: goes through the per-chat rate limiter (with plain-text
  # retry if Telegram rejects the parse_mode formatting)
  api_call_succeeded, api_result, parse_mode_fallback_used = _send_with_parse_mode_fallback(
    bot_token, chat_id, 'editMessageText', api_params)
  if not api_call_succeeded:
    return create_error_response(f"editMessageText failed: {api_result}", with_readme=False)

  edit_response_payload = {"status": "edited", "message_id": message_id, "chat_id": chat_id}
  if parse_mode_fallback_used:
    edit_response_payload["parse_mode_fallback"] = (
      "Edit was rejected with a formatting parse error and re-applied as plain text (no parse_mode).")

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(edit_response_payload, indent=2)
    }],
    "isError": False
  }


def handle_delete_message_operation(params: Dict) -> Dict:
  """Handle delete_message operation - deletes a message."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  message_id = params.get("message_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'message_id': message_id,
  }

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'deleteMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"deleteMessage failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "deleted", "message_id": message_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_send_photo_operation(params: Dict) -> Dict:
  """Handle send_photo operation - sends a photo via URL/file_id (photo_url)
  or by uploading a local file from this machine (photo_path)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  photo_url = params.get("photo_url")
  photo_path = params.get("photo_path")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not photo_url and not photo_path:
    return create_error_response("Provide 'photo_url' (public URL or file_id) or 'photo_path' (local file to upload).", with_readme=False)
  if photo_url and photo_path:
    return create_error_response("Provide only ONE of 'photo_url' or 'photo_path', not both.", with_readme=False)

  caption = params.get("caption")
  if caption and len(caption) > _TELEGRAM_MAX_CAPTION_LENGTH_CHARS:
    return create_error_response(
      f"Caption is {len(caption)} characters; Telegram's caption limit is "
      f"{_TELEGRAM_MAX_CAPTION_LENGTH_CHARS}.", with_readme=False)

  api_params = {'chat_id': chat_id}
  if caption:
    api_params['caption'] = caption

  parse_mode = params.get("parse_mode")
  if parse_mode:
    api_params['parse_mode'] = parse_mode

  reply_to_message_id = params.get("reply_to_message_id")
  if reply_to_message_id:
    api_params['reply_parameters'] = {'message_id': reply_to_message_id}

  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  reply_markup = params.get("reply_markup")
  if reply_markup:
    api_params['reply_markup'] = reply_markup

  # Send-class call: goes through the per-chat rate limiter either way
  if photo_path:
    token_acquired, rate_error = _rate_limit_wait_until_send_token_acquired(chat_id, 'sendPhoto')
    if not token_acquired:
      return create_error_response(f"sendPhoto failed: {rate_error}", with_readme=False)
    api_call_succeeded, api_result = _call_telegram_bot_api_method_with_local_file_upload(
      bot_token, 'sendPhoto', 'photo', photo_path, api_params,
      max_upload_size_bytes=_TELEGRAM_PHOTO_UPLOAD_MAX_BYTES)
  else:
    api_params['photo'] = photo_url
    api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendPhoto', api_params)

  if not api_call_succeeded:
    return create_error_response(f"sendPhoto failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_id": api_result.get('message_id'),
        "chat_id": chat_id,
        "uploaded_local_file": photo_path if photo_path else None,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_document_operation(params: Dict) -> Dict:
  """Handle send_document operation - sends a document via URL/file_id (document)
  or by uploading a local file from this machine (document_path)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  document_url_or_file_id = params.get("document")
  document_path = params.get("document_path")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not document_url_or_file_id and not document_path:
    return create_error_response("Provide 'document' (public URL or Telegram file_id) or 'document_path' (local file to upload).", with_readme=False)
  if document_url_or_file_id and document_path:
    return create_error_response("Provide only ONE of 'document' or 'document_path', not both.", with_readme=False)

  caption = params.get("caption")
  if caption and len(caption) > _TELEGRAM_MAX_CAPTION_LENGTH_CHARS:
    return create_error_response(
      f"Caption is {len(caption)} characters; Telegram's caption limit is "
      f"{_TELEGRAM_MAX_CAPTION_LENGTH_CHARS}.", with_readme=False)

  api_params = {'chat_id': chat_id}
  if caption:
    api_params['caption'] = caption

  parse_mode = params.get("parse_mode")
  if parse_mode:
    api_params['parse_mode'] = parse_mode

  reply_to_message_id = params.get("reply_to_message_id")
  if reply_to_message_id:
    api_params['reply_parameters'] = {'message_id': reply_to_message_id}

  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  reply_markup = params.get("reply_markup")
  if reply_markup:
    api_params['reply_markup'] = reply_markup

  # Send-class call: goes through the per-chat rate limiter either way
  if document_path:
    token_acquired, rate_error = _rate_limit_wait_until_send_token_acquired(chat_id, 'sendDocument')
    if not token_acquired:
      return create_error_response(f"sendDocument failed: {rate_error}", with_readme=False)
    api_call_succeeded, api_result = _call_telegram_bot_api_method_with_local_file_upload(
      bot_token, 'sendDocument', 'document', document_path, api_params,
      max_upload_size_bytes=_TELEGRAM_DOCUMENT_UPLOAD_MAX_BYTES)
  else:
    api_params['document'] = document_url_or_file_id
    api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendDocument', api_params)

  if not api_call_succeeded:
    return create_error_response(f"sendDocument failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_id": api_result.get('message_id'),
        "chat_id": chat_id,
        "uploaded_local_file": document_path if document_path else None,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_voice_operation(params: Dict) -> Dict:
  """Handle send_voice operation - sends a voice note via URL/file_id (voice)
  or by uploading a local OGG/OPUS (or MP3/M4A) file (voice_path)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  voice_url_or_file_id = params.get("voice")
  voice_path = params.get("voice_path")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not voice_url_or_file_id and not voice_path:
    return create_error_response("Provide 'voice' (public URL or Telegram file_id) or 'voice_path' (local file to upload).", with_readme=False)
  if voice_url_or_file_id and voice_path:
    return create_error_response("Provide only ONE of 'voice' or 'voice_path', not both.", with_readme=False)

  caption = params.get("caption")
  if caption and len(caption) > _TELEGRAM_MAX_CAPTION_LENGTH_CHARS:
    return create_error_response(
      f"Caption is {len(caption)} characters; Telegram's caption limit is "
      f"{_TELEGRAM_MAX_CAPTION_LENGTH_CHARS}.", with_readme=False)

  api_params = {'chat_id': chat_id}
  if caption:
    api_params['caption'] = caption
  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  if voice_path:
    token_acquired, rate_error = _rate_limit_wait_until_send_token_acquired(chat_id, 'sendVoice')
    if not token_acquired:
      return create_error_response(f"sendVoice failed: {rate_error}", with_readme=False)
    api_call_succeeded, api_result = _call_telegram_bot_api_method_with_local_file_upload(
      bot_token, 'sendVoice', 'voice', voice_path, api_params,
      max_upload_size_bytes=_TELEGRAM_DOCUMENT_UPLOAD_MAX_BYTES)
  else:
    api_params['voice'] = voice_url_or_file_id
    api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendVoice', api_params)

  if not api_call_succeeded:
    return create_error_response(f"sendVoice failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_id": api_result.get('message_id'),
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_media_group_operation(params: Dict) -> Dict:
  """Handle send_media_group operation - sends an album of 2-10 photos/videos/
  audios/documents as one grouped message. Each media item is an InputMedia
  dict ({type, media: URL-or-file_id, optional caption}); local-file album
  uploads are not supported here (send items individually or use raw_api_call)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  media_items = params.get("media")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not media_items or not isinstance(media_items, list) or not (2 <= len(media_items) <= 10):
    return create_error_response(
      "Parameter 'media' must be a list of 2-10 InputMedia dicts, e.g. "
      "[{\"type\": \"photo\", \"media\": \"https://... or file_id\"}, ...]. "
      "For a single item use send_photo / send_document.", with_readme=False)
  for media_item_index, media_item in enumerate(media_items):
    if not isinstance(media_item, dict) or not media_item.get("type") or not media_item.get("media"):
      return create_error_response(
        f"media[{media_item_index}] must be a dict with 'type' (photo/video/audio/document) "
        "and 'media' (public URL or Telegram file_id).", with_readme=False)
    media_item_caption = media_item.get("caption")
    if media_item_caption and len(media_item_caption) > _TELEGRAM_MAX_CAPTION_LENGTH_CHARS:
      return create_error_response(
        f"media[{media_item_index}] caption is {len(media_item_caption)} characters; Telegram's "
        f"caption limit is {_TELEGRAM_MAX_CAPTION_LENGTH_CHARS}.", with_readme=False)

  api_params = {'chat_id': chat_id, 'media': media_items}
  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  # Send-class call: goes through the per-chat rate limiter
  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendMediaGroup', api_params)
  if not api_call_succeeded:
    return create_error_response(f"sendMediaGroup failed: {api_result}", with_readme=False)

  # sendMediaGroup returns an array of Message objects, one per album item
  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_ids": [sent_message.get('message_id') for sent_message in (api_result or [])],
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_location_operation(params: Dict) -> Dict:
  """Handle send_location operation - sends a map point to a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  latitude = params.get("latitude")
  longitude = params.get("longitude")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if latitude is None or longitude is None:
    return create_error_response("Parameters 'latitude' and 'longitude' are required.", with_readme=False)
  if not (-90.0 <= float(latitude) <= 90.0) or not (-180.0 <= float(longitude) <= 180.0):
    return create_error_response("latitude must be -90..90 and longitude -180..180.", with_readme=False)

  api_params = {'chat_id': chat_id, 'latitude': latitude, 'longitude': longitude}
  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendLocation', api_params)
  if not api_call_succeeded:
    return create_error_response(f"sendLocation failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_id": api_result.get('message_id'),
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_poll_operation(params: Dict) -> Dict:
  """Handle send_poll operation - sends a native Telegram poll."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  question = params.get("question")
  poll_options = params.get("poll_options")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not question:
    return create_error_response("Parameter 'question' is required.", with_readme=False)
  if not poll_options or not isinstance(poll_options, list) or len(poll_options) < 2:
    return create_error_response("Parameter 'poll_options' must be a list of at least 2 option strings.", with_readme=False)
  if len(poll_options) > 10:
    return create_error_response("Telegram polls allow at most 10 options.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'question': question,
    'options': [{'text': str(option_text)} for option_text in poll_options],
  }
  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'sendPoll', api_params)
  if not api_call_succeeded:
    return create_error_response(f"sendPoll failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "sent",
        "message_id": api_result.get('message_id'),
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_set_message_reaction_operation(params: Dict) -> Dict:
  """Handle set_message_reaction operation - reacts to a message with an emoji
  (empty reaction_emoji clears the bot's reaction)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  message_id = params.get("message_id")
  reaction_emoji = params.get("reaction_emoji")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'message_id': message_id,
    # Empty list clears the reaction; otherwise one emoji-type reaction
    'reaction': ([{'type': 'emoji', 'emoji': reaction_emoji}] if reaction_emoji else []),
  }

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'setMessageReaction', api_params)
  if not api_call_succeeded:
    return create_error_response(f"setMessageReaction failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "reaction_set" if reaction_emoji else "reaction_cleared",
        "message_id": message_id,
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_forward_message_operation(params: Dict) -> Dict:
  """Handle forward_message operation - forwards a message from one chat to another."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  from_chat_id = params.get("from_chat_id")
  message_id = params.get("message_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required (destination).", with_readme=False)
  if not from_chat_id:
    return create_error_response("Parameter 'from_chat_id' is required (source chat).", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'from_chat_id': from_chat_id,
    'message_id': message_id,
  }

  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  # Send-class call: goes through the per-chat rate limiter (destination chat)
  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'forwardMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"forwardMessage failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "forwarded",
        "new_message_id": api_result.get('message_id'),
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_copy_message_operation(params: Dict) -> Dict:
  """Handle copy_message operation - copies a message without the 'Forwarded from' attribution."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  from_chat_id = params.get("from_chat_id")
  message_id = params.get("message_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required (destination).", with_readme=False)
  if not from_chat_id:
    return create_error_response("Parameter 'from_chat_id' is required (source chat).", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'from_chat_id': from_chat_id,
    'message_id': message_id,
  }

  caption = params.get("caption")
  if caption:
    if len(caption) > _TELEGRAM_MAX_CAPTION_LENGTH_CHARS:
      return create_error_response(
        f"Caption is {len(caption)} characters; Telegram's caption limit is "
        f"{_TELEGRAM_MAX_CAPTION_LENGTH_CHARS}.", with_readme=False)
    api_params['caption'] = caption

  parse_mode = params.get("parse_mode")
  if parse_mode:
    api_params['parse_mode'] = parse_mode

  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  reply_markup = params.get("reply_markup")
  if reply_markup:
    api_params['reply_markup'] = reply_markup

  # Send-class call: goes through the per-chat rate limiter (destination chat)
  api_call_succeeded, api_result = _rate_limited_send(bot_token, chat_id, 'copyMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"copyMessage failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "copied",
        "new_message_id": api_result.get('message_id'),
        "chat_id": chat_id,
      }, indent=2)
    }],
    "isError": False
  }


def handle_send_chat_action_operation(params: Dict) -> Dict:
  """Handle send_chat_action operation - shows typing/uploading indicator in a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  action = params.get("action")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not action:
    return create_error_response("Parameter 'action' is required. Use: typing, upload_photo, upload_document, upload_video, record_voice, record_video_note, find_location, choose_sticker.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'action': action,
  }

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'sendChatAction', api_params)
  if not api_call_succeeded:
    return create_error_response(f"sendChatAction failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "action_sent", "action": action, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_get_chat_operation(params: Dict) -> Dict:
  """Handle get_chat operation - returns full info about a chat (group, channel, or DM)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'getChat', {'chat_id': chat_id})
  if not api_call_succeeded:
    return create_error_response(f"getChat failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(api_result, indent=2)
    }],
    "isError": False
  }


def handle_get_chat_member_operation(params: Dict) -> Dict:
  """Handle get_chat_member operation - returns info about a member of a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'getChatMember', {'chat_id': chat_id, 'user_id': user_id}
  )
  if not api_call_succeeded:
    return create_error_response(f"getChatMember failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(api_result, indent=2)
    }],
    "isError": False
  }


def handle_get_chat_member_count_operation(params: Dict) -> Dict:
  """Handle get_chat_member_count operation - returns the number of members in a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'getChatMemberCount', {'chat_id': chat_id}
  )
  if not api_call_succeeded:
    return create_error_response(f"getChatMemberCount failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"chat_id": chat_id, "member_count": api_result}, indent=2)
    }],
    "isError": False
  }


def handle_set_my_commands_operation(params: Dict) -> Dict:
  """Handle set_my_commands operation - registers bot commands with Telegram's menu."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  commands = params.get("commands")
  if not commands or not isinstance(commands, list):
    return create_error_response(
      "Parameter 'commands' is required. Provide a list of {command, description} dicts. "
      "Example: [{\"command\": \"start\", \"description\": \"Start the bot\"}]",
      with_readme=False
    )

  api_params = {'commands': commands}

  # Optional: scope and language_code for targeted command menus
  scope = params.get("scope")
  if scope:
    api_params['scope'] = scope

  language_code = params.get("language_code")
  if language_code:
    api_params['language_code'] = language_code

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'setMyCommands', api_params)
  if not api_call_succeeded:
    return create_error_response(f"setMyCommands failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "commands_set",
        "command_count": len(commands),
        "commands": commands,
      }, indent=2)
    }],
    "isError": False
  }


def handle_answer_callback_query_operation(params: Dict) -> Dict:
  """Handle answer_callback_query operation - acknowledges an inline button press."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  callback_query_id = params.get("callback_query_id")
  if not callback_query_id:
    return create_error_response("Parameter 'callback_query_id' is required.", with_readme=False)

  api_params = {'callback_query_id': callback_query_id}

  text = params.get("text")
  if text:
    api_params['text'] = text

  show_alert = params.get("show_alert")
  if show_alert:
    api_params['show_alert'] = True

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'answerCallbackQuery', api_params)
  if not api_call_succeeded:
    return create_error_response(f"answerCallbackQuery failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "answered", "callback_query_id": callback_query_id}, indent=2)
    }],
    "isError": False
  }


def handle_pin_chat_message_operation(params: Dict) -> Dict:
  """Handle pin_chat_message operation - pins a message in a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  message_id = params.get("message_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not message_id:
    return create_error_response("Parameter 'message_id' is required.", with_readme=False)

  api_params = {
    'chat_id': chat_id,
    'message_id': message_id,
  }

  if params.get("disable_notification"):
    api_params['disable_notification'] = True

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'pinChatMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"pinChatMessage failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "pinned", "message_id": message_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_unpin_chat_message_operation(params: Dict) -> Dict:
  """Handle unpin_chat_message operation - unpins a message in a chat."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  message_id = params.get("message_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)

  api_params = {'chat_id': chat_id}

  if message_id:
    api_params['message_id'] = message_id

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'unpinChatMessage', api_params)
  if not api_call_succeeded:
    return create_error_response(f"unpinChatMessage failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "unpinned", "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_get_file_operation(params: Dict) -> Dict:
  """Handle get_file operation - gets a download URL for a file sent to the bot.
  Returns the file path which can be used to construct the download URL."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  file_id = params.get("file_id")
  if not file_id:
    return create_error_response("Parameter 'file_id' is required. Get file_id from received messages (document, photo, voice, etc).", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'getFile', {'file_id': file_id})
  if not api_call_succeeded:
    return create_error_response(f"getFile failed: {api_result}", with_readme=False)

  file_path = api_result.get('file_path', '')
  download_url = f"{TELEGRAM_FILE_API_BASE_URL}{bot_token}/{file_path}" if file_path else None

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "file_id": api_result.get('file_id'),
        "file_unique_id": api_result.get('file_unique_id'),
        "file_size": api_result.get('file_size'),
        "file_path": file_path,
        "download_url": download_url,
        "note": ("download_url embeds the bot token - avoid pasting it into logs/transcripts. "
                 "Prefer the download_file operation, which fetches server-side and returns a local path."),
      }, indent=2)
    }],
    "isError": False
  }


def handle_download_file_operation(params: Dict) -> Dict:
  """Handle download_file operation - fetches a Telegram file server-side into
  the user data downloads directory and returns the local path. Keeps the
  token-bearing download URL out of AI transcripts (unlike get_file)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  file_id = params.get("file_id")
  if not file_id:
    return create_error_response("Parameter 'file_id' is required. Get file_id from received messages (document, photo, voice, etc).", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'getFile', {'file_id': file_id})
  if not api_call_succeeded:
    return create_error_response(f"getFile failed: {api_result}", with_readme=False)

  telegram_file_path = api_result.get('file_path', '')
  if not telegram_file_path:
    return create_error_response("Telegram returned no file_path for this file_id.", with_readme=False)

  declared_file_size = api_result.get('file_size')
  if isinstance(declared_file_size, int) and declared_file_size > _TELEGRAM_FILE_DOWNLOAD_MAX_BYTES:
    return create_error_response(
      f"File is {declared_file_size} bytes which exceeds the {_TELEGRAM_FILE_DOWNLOAD_MAX_BYTES} byte "
      "download cap (Telegram's bot API getFile limit is 20MB).", with_readme=False)

  # Safe local filename: keep only the basename, restrict the character set, and
  # prefix with the unique id so different files never collide
  original_basename = os.path.basename(telegram_file_path.replace('\\', '/')) or 'download.bin'
  safe_basename = re.sub(r'[^A-Za-z0-9._-]', '_', original_basename)
  unique_prefix = re.sub(r'[^A-Za-z0-9_-]', '_', str(api_result.get('file_unique_id') or 'file'))
  downloads_directory = get_user_data_directory() / "downloads"
  downloads_directory.mkdir(parents=True, exist_ok=True)
  local_download_path = downloads_directory / f"{unique_prefix}_{safe_basename}"

  download_url = f"{TELEGRAM_FILE_API_BASE_URL}{bot_token}/{telegram_file_path}"
  try:
    http_request = urllib.request.Request(download_url, headers={'Connection': 'close'})
    with urllib.request.urlopen(http_request, timeout=120, context=_TELEGRAM_TLS_SSL_CONTEXT) as http_response:
      downloaded_bytes_total = 0
      with open(local_download_path, 'wb') as local_file_handle:
        while True:
          chunk = http_response.read(65536)
          if not chunk:
            break
          downloaded_bytes_total += len(chunk)
          if downloaded_bytes_total > _TELEGRAM_FILE_DOWNLOAD_MAX_BYTES:
            local_file_handle.close()
            try:
              local_download_path.unlink()
            except OSError:
              pass
            return create_error_response(
              f"Download exceeded the {_TELEGRAM_FILE_DOWNLOAD_MAX_BYTES} byte cap; aborted.", with_readme=False)
          local_file_handle.write(chunk)
  except Exception as download_error:
    # Never echo the failing URL - it embeds the bot token
    return create_error_response(f"File download failed: {download_error}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({
        "status": "downloaded",
        "local_path": str(local_download_path),
        "file_size_bytes": downloaded_bytes_total,
        "file_id": api_result.get('file_id'),
        "file_unique_id": api_result.get('file_unique_id'),
      }, indent=2)
    }],
    "isError": False
  }


def handle_leave_chat_operation(params: Dict) -> Dict:
  """Handle leave_chat operation - makes the bot leave a group, supergroup, or channel."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'leaveChat', {'chat_id': chat_id})
  if not api_call_succeeded:
    return create_error_response(f"leaveChat failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "left_chat", "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_ban_chat_member_operation(params: Dict) -> Dict:
  """Handle ban_chat_member operation - bans a user from a group/supergroup/channel."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  api_params = {'chat_id': chat_id, 'user_id': user_id}

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'banChatMember', api_params)
  if not api_call_succeeded:
    return create_error_response(f"banChatMember failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "banned", "user_id": user_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_unban_chat_member_operation(params: Dict) -> Dict:
  """Handle unban_chat_member operation - unbans a user from a group/supergroup/channel."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  # only_if_banned defaults True (no-op when the user is not banned) but is
  # exposed so callers can force the remove-from-chat semantics Telegram
  # applies when it is false
  only_if_banned = params.get("only_if_banned")
  if only_if_banned is None:
    only_if_banned = True
  api_params = {'chat_id': chat_id, 'user_id': user_id, 'only_if_banned': bool(only_if_banned)}

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'unbanChatMember', api_params)
  if not api_call_succeeded:
    return create_error_response(f"unbanChatMember failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "unbanned", "user_id": user_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_promote_chat_member_operation(params: Dict) -> Dict:
  """Handle promote_chat_member operation - promotes/demotes a user in a group/supergroup."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  # Build the permissions from the permissions parameter. Only whitelisted
  # permission keys are splatted so a caller cannot override chat_id/user_id
  # or inject unrelated API params through this dict.
  api_params = {'chat_id': chat_id, 'user_id': user_id}

  ignored_permission_keys = []
  permissions = params.get("permissions")
  if permissions and isinstance(permissions, dict):
    for perm_key, perm_value in permissions.items():
      if isinstance(perm_key, str) and _PROMOTE_PERMISSION_KEY_ALLOWLIST_PATTERN.match(perm_key):
        api_params[perm_key] = perm_value
      else:
        ignored_permission_keys.append(str(perm_key))

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'promoteChatMember', api_params)
  if not api_call_succeeded:
    return create_error_response(f"promoteChatMember failed: {api_result}", with_readme=False)

  promote_response_payload = {"status": "promoted", "user_id": user_id, "chat_id": chat_id}
  if ignored_permission_keys:
    promote_response_payload["ignored_permission_keys"] = ignored_permission_keys
    promote_response_payload["note"] = "Only is_anonymous and can_* permission flags are accepted."

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(promote_response_payload, indent=2)
    }],
    "isError": False
  }


def handle_restrict_chat_member_operation(params: Dict) -> Dict:
  """Handle restrict_chat_member operation - mutes/limits a user in a supergroup.
  Pass permissions with all-False (or omit it) to fully mute; pass can_* flags
  True to selectively restore abilities. until_date (unix time) makes it temporary."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  # ChatPermissions object: only can_* keys are valid; default (no permissions
  # param) is a full mute (all permissions false)
  chat_permissions_object = {}
  ignored_permission_keys = []
  permissions = params.get("permissions")
  if permissions and isinstance(permissions, dict):
    for perm_key, perm_value in permissions.items():
      if isinstance(perm_key, str) and _RESTRICT_PERMISSION_KEY_ALLOWLIST_PATTERN.match(perm_key):
        chat_permissions_object[perm_key] = bool(perm_value)
      else:
        ignored_permission_keys.append(str(perm_key))

  api_params = {
    'chat_id': chat_id,
    'user_id': user_id,
    'permissions': chat_permissions_object,
  }

  until_date = params.get("until_date")
  if until_date:
    api_params['until_date'] = until_date

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'restrictChatMember', api_params)
  if not api_call_succeeded:
    return create_error_response(f"restrictChatMember failed: {api_result}", with_readme=False)

  restrict_response_payload = {
    "status": "restricted",
    "user_id": user_id,
    "chat_id": chat_id,
    "permissions_applied": chat_permissions_object,
    "until_date": until_date,
  }
  if ignored_permission_keys:
    restrict_response_payload["ignored_permission_keys"] = ignored_permission_keys
    restrict_response_payload["note"] = "Only can_* permission flags are accepted for restrict."

  return {
    "content": [{
      "type": "text",
      "text": json.dumps(restrict_response_payload, indent=2)
    }],
    "isError": False
  }


def handle_approve_chat_join_request_operation(params: Dict) -> Dict:
  """Handle approve_chat_join_request operation - approves a pending join request."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'approveChatJoinRequest', {'chat_id': chat_id, 'user_id': user_id}
  )
  if not api_call_succeeded:
    return create_error_response(f"approveChatJoinRequest failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "approved", "user_id": user_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_decline_chat_join_request_operation(params: Dict) -> Dict:
  """Handle decline_chat_join_request operation - declines a pending join request."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  user_id = params.get("user_id")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)
  if not user_id:
    return create_error_response("Parameter 'user_id' is required.", with_readme=False)

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'declineChatJoinRequest', {'chat_id': chat_id, 'user_id': user_id}
  )
  if not api_call_succeeded:
    return create_error_response(f"declineChatJoinRequest failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "declined", "user_id": user_id, "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_set_chat_description_operation(params: Dict) -> Dict:
  """Handle set_chat_description operation - sets the description of a group/supergroup/channel."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  chat_id = params.get("chat_id")
  description = params.get("description")

  if not chat_id:
    return create_error_response("Parameter 'chat_id' is required.", with_readme=False)

  api_params = {'chat_id': chat_id}
  if description is not None:
    api_params['description'] = description

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, 'setChatDescription', api_params)
  if not api_call_succeeded:
    return create_error_response(f"setChatDescription failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"status": "description_set", "chat_id": chat_id}, indent=2)
    }],
    "isError": False
  }


def handle_raw_api_call_operation(params: Dict) -> Dict:
  """Handle raw_api_call operation - calls any Telegram Bot API method directly.
  This is a passthrough for API methods not explicitly implemented as operations."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  method = params.get("method")
  if not method or not isinstance(method, str):
    return create_error_response(
      "Parameter 'method' is required. Provide the Telegram Bot API method name (e.g. 'getMe', 'sendMessage').",
      with_readme=False
    )

  api_parameters = params.get("api_parameters")
  if api_parameters is not None and not isinstance(api_parameters, dict):
    return create_error_response("Parameter 'api_parameters' must be a dict if provided.", with_readme=False)

  # Log param KEYS only - values can contain message text, phone numbers, or tokens
  MCPLogger.log(TOOL_LOG_NAME, f"Raw API call: {method} with param keys: {sorted((api_parameters or {}).keys())}")

  api_call_succeeded, api_result = _call_telegram_bot_api_method(bot_token, method, api_parameters)
  if not api_call_succeeded:
    return create_error_response(f"{method} failed: {api_result}", with_readme=False)

  return {
    "content": [{
      "type": "text",
      "text": json.dumps({"method": method, "result": api_result}, indent=2)
    }],
    "isError": False
  }


def handle_delete_webhook_operation(params: Dict) -> Dict:
  """Handle delete_webhook operation - removes a webhook so getUpdates works again
  (webhooks and getUpdates are mutually exclusive; a set webhook causes 409s)."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  api_params = {}
  if params.get("drop_pending_updates"):
    api_params['drop_pending_updates'] = True

  api_call_succeeded, api_result = _call_telegram_bot_api_method(
    bot_token, 'deleteWebhook', api_params if api_params else None)
  if not api_call_succeeded:
    return create_error_response(f"deleteWebhook failed: {api_result}", with_readme=False)

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "webhook_deleted",
      "dropped_pending_updates": bool(params.get("drop_pending_updates")),
      "note": "getUpdates polling (and start_listening) will work now."
    }, indent=2)}],
    "isError": False
  }


def handle_start_listening_operation(params: Dict) -> Dict:
  """Handle start_listening operation - starts a background polling thread."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  # Honor the caller's long-poll timeout (clamped to a sane range)
  long_poll_timeout_seconds = params.get("timeout", 30)
  long_poll_timeout_seconds = max(5, min(long_poll_timeout_seconds, 50))

  # Check-and-register in ONE critical section so two concurrent start_listening
  # calls cannot both pass the check and spawn duplicate pollers
  with _telegram_global_state_lock:
    existing_poller = _telegram_background_pollers_per_bot.get(token_hash)
    if existing_poller and existing_poller.get('running'):
      return {
        "content": [{"type": "text", "text": json.dumps({
          "status": "already_running",
          "note": "Background listener is already running for this bot."
        }, indent=2)}],
        "isError": False
      }

    # Each poller gets its own stop event; the thread checks only this event.
    # NOTE: the raw bot token is deliberately NOT stored in this registry entry
    # (the token-hash keying scheme exists to keep raw tokens out of long-lived
    # module state); the thread closure holds the only reference it needs.
    poller_stop_requested_event = threading.Event()
    poller_registry_entry = {
      'thread': None,  # filled in below once the Thread object exists
      'running': True,
      'stop_event': poller_stop_requested_event,
      'started_at': datetime.now(timezone.utc).isoformat(),
      'long_poll_timeout_seconds': long_poll_timeout_seconds,
      'consecutive_error_count': 0,
      'last_error': None,
    }
    poller_thread = threading.Thread(
      target=_telegram_background_polling_thread_function,
      args=(bot_token, poller_stop_requested_event, poller_registry_entry, long_poll_timeout_seconds),
      daemon=True,
      name=f"telegram_poller_{token_hash[:8]}"
    )
    poller_registry_entry['thread'] = poller_thread
    _telegram_background_pollers_per_bot[token_hash] = poller_registry_entry

  poller_thread.start()
  _sqlite_record_listener_active_state(token_hash, True)
  MCPLogger.log(TOOL_LOG_NAME, f"Started background listener for bot hash {token_hash}")

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "started",
      "long_poll_timeout_seconds": long_poll_timeout_seconds,
      "note": "Background listener is now running. Messages will accumulate in history. Use get_message_history or get_updates to read them."
    }, indent=2)}],
    "isError": False
  }


def handle_stop_listening_operation(params: Dict) -> Dict:
  """Handle stop_listening operation - stops the background polling thread."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  with _telegram_global_state_lock:
    existing_poller = _telegram_background_pollers_per_bot.get(token_hash)
    if not existing_poller or not existing_poller.get('running'):
      return {
        "content": [{"type": "text", "text": json.dumps({
          "status": "not_running",
          "note": "No background listener is running for this bot."
        }, indent=2)}],
        "isError": False
      }

    existing_poller['running'] = False
    # Signal THIS poller's own stop event so the old thread exits even if a
    # new poller re-registers under the same token hash afterwards
    stop_event_for_this_poller = existing_poller.get('stop_event')
    if stop_event_for_this_poller is not None:
      stop_event_for_this_poller.set()
    poll_timeout_for_latency_note = existing_poller.get('long_poll_timeout_seconds', 30)

  _sqlite_record_listener_active_state(token_hash, False)
  MCPLogger.log(TOOL_LOG_NAME, f"Stopping background listener for bot hash {token_hash}")

  # Worst case = the in-flight long poll (its HTTP timeout is poll timeout + 10s
  # network slack); error backoff sleeps are interrupted by the stop event
  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "stopping",
      "note": (f"Background listener is being stopped. It will terminate after the current poll "
               f"completes (worst case about {poll_timeout_for_latency_note + 10} seconds: the "
               f"{poll_timeout_for_latency_note}s long poll plus network slack).")
    }, indent=2)}],
    "isError": False
  }


def handle_get_listening_status_operation(params: Dict) -> Dict:
  """Handle get_listening_status - returns whether background polling is active."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  with _telegram_global_state_lock:
    existing_poller = _telegram_background_pollers_per_bot.get(token_hash)
    history_count = len(_telegram_received_message_history_per_bot.get(token_hash, []))
    known_chat_count = len(_telegram_known_chats_per_bot.get(token_hash, {}))

  if existing_poller and existing_poller.get('running'):
    is_thread_actually_alive = existing_poller['thread'].is_alive()
    return {
      "content": [{"type": "text", "text": json.dumps({
        "listening": True,
        "thread_alive": is_thread_actually_alive,
        "started_at": existing_poller.get('started_at'),
        "long_poll_timeout_seconds": existing_poller.get('long_poll_timeout_seconds'),
        "consecutive_error_count": existing_poller.get('consecutive_error_count', 0),
        "last_error": existing_poller.get('last_error'),
        "stored_message_count": history_count,
        "known_chat_count": known_chat_count,
      }, indent=2)}],
      "isError": False
    }
  else:
    status_payload = {
      "listening": False,
      "stored_message_count": history_count,
      "known_chat_count": known_chat_count,
    }
    # Surface why a listener that was running stopped on its own (e.g. auto-stop
    # after repeated failures)
    if existing_poller:
      if existing_poller.get('stopped_reason'):
        status_payload['stopped_reason'] = existing_poller.get('stopped_reason')
      if existing_poller.get('last_error'):
        status_payload['last_error'] = existing_poller.get('last_error')
    return {
      "content": [{"type": "text", "text": json.dumps(status_payload, indent=2)}],
      "isError": False
    }


def handle_wait_for_message_operation(params: Dict) -> Dict:
  """Handle wait_for_message operation - blocks until a matching message arrives
  via the background listener (or the wait times out). Converts the AI's
  poll-loop into a single efficient call.

  Implemented by registering a temporary in-process event callback for the wait
  duration, so filter semantics are identical to register_event_callback."""
  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  with _telegram_global_state_lock:
    poller_info = _telegram_background_pollers_per_bot.get(token_hash)
    background_poller_is_running = bool(poller_info and poller_info.get('running'))
  if not background_poller_is_running:
    return create_error_response(
      "wait_for_message requires the background listener: call start_listening first "
      "(messages are delivered by its poller).", with_readme=False)

  wait_seconds = params.get("wait_seconds", 30)
  if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
    wait_seconds = 30
  wait_seconds = max(1.0, min(float(wait_seconds), float(_WAIT_FOR_MESSAGE_MAX_WAIT_SECONDS)))

  filter_chat_ids = params.get("filter_chat_ids")
  filter_message_types = params.get("filter_message_types")

  matched_messages_holder: List[Dict] = []
  a_matching_message_has_arrived_event = threading.Event()

  def _collect_first_matching_batch(bot_hash: str, messages: List[Dict]):
    if bot_hash != token_hash:
      return  # Another bot's poller fired; keep waiting for ours
    matched_messages_holder.extend(messages)
    a_matching_message_has_arrived_event.set()

  temporary_callback_id = f"_wait_for_message_{hashlib.sha256(os.urandom(12)).hexdigest()[:12]}"
  register_message_event_callback(
    temporary_callback_id, _collect_first_matching_batch, filter_chat_ids, filter_message_types)
  try:
    a_matching_message_has_arrived_event.wait(timeout=wait_seconds)
  finally:
    unregister_message_event_callback(temporary_callback_id)

  if matched_messages_holder:
    return {
      "content": [{"type": "text", "text": json.dumps({
        "status": "message_received",
        "message_count": len(matched_messages_holder),
        "messages": matched_messages_holder,
      }, indent=2)}],
      "isError": False
    }
  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "timeout",
      "waited_seconds": wait_seconds,
      "messages": [],
      "note": "No matching message arrived within the wait window. Call wait_for_message again to keep waiting.",
    }, indent=2)}],
    "isError": False
  }


# ============================================================================
# OPERATION HANDLERS — Persistence, Personas, Event Callbacks
# ============================================================================

def handle_enable_persistence_operation(params: Dict) -> Dict:
  """Enable sqlite-backed persistent storage for messages, chats, and personas."""
  result = enable_sqlite_persistence(store_raw_json=bool(params.get("store_raw_json")))
  loaded_personas = _load_personas_from_sqlite()
  result["personas_loaded_from_db"] = loaded_personas
  return {
    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
    "isError": False
  }


def handle_disable_persistence_operation(params: Dict) -> Dict:
  """Disable sqlite-backed persistent storage."""
  result = disable_sqlite_persistence()
  return {
    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
    "isError": False
  }


def handle_get_persistent_history_operation(params: Dict) -> Dict:
  """Query message history from sqlite persistent store."""
  if not _sqlite_persistence_enabled_flag:
    return create_error_response(
      "SQLite persistence is not enabled. Use enable_persistence first.", with_readme=False)

  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)
  chat_id = params.get("chat_id")
  limit = params.get("limit", 50)
  # Clamp: SQLite treats LIMIT -1 (and other negatives) as unlimited
  limit = max(1, min(limit, _HISTORY_QUERY_LIMIT_MAX))
  since_timestamp = params.get("since_timestamp")

  messages = _sqlite_query_message_history(token_hash, chat_id, limit, since_timestamp)

  return {
    "content": [{"type": "text", "text": json.dumps({
      "source": "sqlite",
      "message_count": len(messages),
      "messages": messages,
    }, indent=2)}],
    "isError": False
  }


def handle_purge_history_operation(params: Dict) -> Dict:
  """Handle purge_history operation - deletes persisted messages for this bot
  by chat_id and/or before_timestamp, or everything with purge_all. Retention
  control for the PII in stored messages (text, contacts, locations, raw_json)."""
  if not _sqlite_persistence_enabled_flag:
    return create_error_response(
      "SQLite persistence is not enabled. Use enable_persistence first.", with_readme=False)

  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)
  chat_id = params.get("chat_id")
  before_timestamp = params.get("before_timestamp")
  purge_all = bool(params.get("purge_all"))

  if chat_id is None and before_timestamp is None and not purge_all:
    return create_error_response(
      "Provide chat_id and/or before_timestamp to scope the purge, or purge_all:true "
      "to delete ALL stored messages for this bot (safety: an unscoped purge must be explicit).",
      with_readme=False)

  query = "DELETE FROM telegram_messages WHERE bot_token_hash=?"
  query_params: list = [token_hash]
  if chat_id is not None:
    query += " AND chat_id=?"
    query_params.append(chat_id)
  if before_timestamp is not None:
    query += " AND timestamp_utc<?"
    query_params.append(before_timestamp)

  try:
    def _delete_matching_message_rows(conn: sqlite3.Connection):
      delete_cursor = conn.execute(query, query_params)
      conn.commit()
      return delete_cursor.rowcount
    deleted_row_count = _with_sqlite_db(_delete_matching_message_rows)
  except Exception as e:
    return create_error_response(f"purge_history failed: {e}", with_readme=False)

  MCPLogger.log(TOOL_LOG_NAME, f"purge_history deleted {deleted_row_count} row(s) "
                 f"(chat_id={chat_id}, before_timestamp={before_timestamp}, purge_all={purge_all})")

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "purged",
      "deleted_message_count": deleted_row_count,
      "chat_id": chat_id,
      "before_timestamp": before_timestamp,
      "purge_all": purge_all,
    }, indent=2)}],
    "isError": False
  }


def handle_restore_state_operation(params: Dict) -> Dict:
  """Handle restore_state operation - after a server restart, revive the
  listening + MCP event-callback registrations recorded in sqlite (persistence
  must be enabled; only listeners for the CURRENT config/param token can be
  revived because raw tokens are never persisted)."""
  if not _sqlite_persistence_enabled_flag:
    return create_error_response(
      "SQLite persistence is not enabled. Use enable_persistence first.", with_readme=False)

  bot_token, token_error = _resolve_telegram_bot_token_from_params_or_config(params)
  if token_error:
    return create_error_response(token_error, with_readme=True)

  token_hash = _create_short_hash_of_bot_token(bot_token)

  try:
    listener_rows = _with_sqlite_db(
      lambda conn: conn.execute(
        "SELECT bot_token_hash, listening_active FROM telegram_listener_state").fetchall())
    callback_spec_rows = _with_sqlite_db(
      lambda conn: conn.execute(
        "SELECT callback_id, filter_chat_ids, filter_message_types FROM telegram_event_callback_specs").fetchall())
  except Exception as e:
    return create_error_response(f"restore_state failed reading saved state: {e}", with_readme=False)

  listener_restored = False
  listener_restore_note = None
  recorded_active_hashes = [r[0] for r in listener_rows if r[1]]
  if token_hash in recorded_active_hashes:
    with _telegram_global_state_lock:
      poller_info = _telegram_background_pollers_per_bot.get(token_hash)
      listener_already_running = bool(poller_info and poller_info.get('running'))
    if listener_already_running:
      listener_restore_note = "Listener was already running; nothing to restore."
    else:
      start_result = handle_start_listening_operation({"bot_token": bot_token})
      listener_restored = not start_result.get("isError")
      if not listener_restored:
        listener_restore_note = "start_listening failed during restore; see logs."
  elif recorded_active_hashes:
    listener_restore_note = ("A listener was recorded active for a DIFFERENT bot token hash "
                             f"({', '.join(recorded_active_hashes)}); pass that bot_token to restore it.")
  else:
    listener_restore_note = "No listener was recorded as active."

  restored_callback_ids = []
  for spec_row in callback_spec_rows:
    spec_callback_id = spec_row[0]
    try:
      spec_filter_chat_ids = json.loads(spec_row[1]) if spec_row[1] else None
      spec_filter_message_types = json.loads(spec_row[2]) if spec_row[2] else None
    except (json.JSONDecodeError, TypeError):
      spec_filter_chat_ids, spec_filter_message_types = None, None
    with _telegram_event_callbacks_lock:
      spec_already_registered = spec_callback_id in _telegram_event_callbacks
    if spec_already_registered:
      continue
    register_result = handle_register_event_callback_operation({
      "callback_id": spec_callback_id,
      "filter_chat_ids": spec_filter_chat_ids,
      "filter_message_types": spec_filter_message_types,
    })
    if not register_result.get("isError"):
      restored_callback_ids.append(spec_callback_id)

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "restore_attempted",
      "listener_restored": listener_restored,
      "listener_note": listener_restore_note,
      "restored_callback_ids": restored_callback_ids,
      "note": ("In-process callbacks from other tools (e.g. agent event sources) re-register "
               "themselves and are not covered here."),
    }, indent=2)}],
    "isError": False
  }


def handle_register_persona_operation(params: Dict) -> Dict:
  """Register a persona for multi-role chat routing."""
  persona_id = params.get("persona_id")
  display_name = params.get("persona_display_name")
  system_prompt = params.get("persona_system_prompt")

  if not persona_id:
    return create_error_response("Parameter 'persona_id' is required.", with_readme=False)
  if not display_name:
    return create_error_response("Parameter 'persona_display_name' is required.", with_readme=False)
  if not system_prompt:
    return create_error_response("Parameter 'persona_system_prompt' is required.", with_readme=False)

  trigger_command = params.get("persona_trigger_command")
  trigger_pattern = params.get("persona_trigger_pattern")
  assigned_chat_ids = params.get("persona_assigned_chat_ids")
  is_default = params.get("persona_is_default", False)

  # Validate regex if provided
  if trigger_pattern:
    try:
      re.compile(trigger_pattern)
    except re.error as e:
      return create_error_response(f"Invalid regex in persona_trigger_pattern: {e}", with_readme=False)

  persona = _register_persona_in_memory(
    persona_id, display_name, system_prompt,
    trigger_command, trigger_pattern, assigned_chat_ids, is_default
  )
  _sqlite_store_persona(persona)

  MCPLogger.log(TOOL_LOG_NAME, f"Registered persona: {persona_id} ({display_name})")

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "registered",
      "persona": persona,
    }, indent=2)}],
    "isError": False
  }


def handle_list_personas_operation(params: Dict) -> Dict:
  """List all registered personas."""
  with _persona_registry_lock:
    personas = list(_persona_registry.values())
  return {
    "content": [{"type": "text", "text": json.dumps({
      "persona_count": len(personas),
      "personas": personas,
    }, indent=2)}],
    "isError": False
  }


def handle_remove_persona_operation(params: Dict) -> Dict:
  """Remove a registered persona."""
  persona_id = params.get("persona_id")
  if not persona_id:
    return create_error_response("Parameter 'persona_id' is required.", with_readme=False)

  with _persona_registry_lock:
    removed = _persona_registry.pop(persona_id, None)

  if not removed:
    return create_error_response(f"Persona '{persona_id}' not found.", with_readme=False)

  # Also remove from sqlite
  if _sqlite_persistence_enabled_flag:
    try:
      def _delete_persona_row(conn: sqlite3.Connection):
        conn.execute("DELETE FROM telegram_personas WHERE persona_id=?", (persona_id,))
        conn.commit()
      _with_sqlite_db(_delete_persona_row)
    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"SQLite remove persona error: {e}")

  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "removed", "persona_id": persona_id
    }, indent=2)}],
    "isError": False
  }


def handle_register_event_callback_operation(params: Dict) -> Dict:
  """Register an event callback (from MCP — stores callback_id for later retrieval via polling).

  Since MCP tool calls are stateless, this registers a named event queue that accumulates
  messages. The AI can then poll via get_callback_events to retrieve them."""
  callback_id = params.get("callback_id")
  filter_chat_ids = params.get("filter_chat_ids")
  filter_message_types = params.get("filter_message_types")

  if not callback_id:
    return create_error_response("Parameter 'callback_id' is required.", with_readme=False)

  # Re-registering an existing callback_id replaces it; report how many pending
  # events that discards rather than dropping them silently
  discarded_pending_event_count = 0
  replaced_previous_registration = False
  with _telegram_event_callbacks_lock:
    previous_entry = _telegram_event_callbacks.get(callback_id)
    if previous_entry:
      replaced_previous_registration = True
      previous_accumulator = previous_entry.get("_accumulator")
      previous_lock = previous_entry.get("_accumulator_lock")
      if previous_accumulator is not None and previous_lock is not None:
        with previous_lock:
          discarded_pending_event_count = len(previous_accumulator)

  # Create a BOUNDED event accumulator queue (oldest events drop when full)
  event_accumulator: deque = deque(maxlen=_EVENT_CALLBACK_ACCUMULATOR_MAX_EVENTS)
  accumulator_lock = threading.Lock()
  dropped_event_count_holder = {"count": 0}

  def accumulate_events(bot_hash: str, messages: List[Dict]):
    with accumulator_lock:
      for msg in messages:
        if len(event_accumulator) == event_accumulator.maxlen:
          dropped_event_count_holder["count"] += 1  # deque will evict the oldest
        event_accumulator.append({"bot_token_hash": bot_hash, **msg})

  register_message_event_callback(
    callback_id, accumulate_events, filter_chat_ids, filter_message_types
  )

  # Store the accumulator so get_callback_events can access it
  with _telegram_event_callbacks_lock:
    entry = _telegram_event_callbacks.get(callback_id)
    if entry:
      entry["_accumulator"] = event_accumulator
      entry["_accumulator_lock"] = accumulator_lock
      entry["_dropped_event_count_holder"] = dropped_event_count_holder

  # Persist the spec so restore_state can re-create this registration after a restart
  _sqlite_record_event_callback_spec(callback_id, filter_chat_ids, filter_message_types)

  registration_response_payload = {
    "status": "registered",
    "callback_id": callback_id,
    "note": "Use get_callback_events to poll for accumulated messages.",
  }
  if replaced_previous_registration:
    registration_response_payload["replaced_previous_registration"] = True
    registration_response_payload["discarded_pending_event_count"] = discarded_pending_event_count

  return {
    "content": [{"type": "text", "text": json.dumps(registration_response_payload, indent=2)}],
    "isError": False
  }


def handle_get_callback_events_operation(params: Dict) -> Dict:
  """Drain accumulated events from a registered event callback."""
  callback_id = params.get("callback_id")
  if not callback_id:
    return create_error_response("Parameter 'callback_id' is required.", with_readme=False)

  with _telegram_event_callbacks_lock:
    entry = _telegram_event_callbacks.get(callback_id)

  if not entry:
    return create_error_response(f"No event callback registered with id '{callback_id}'.", with_readme=False)

  accumulator = entry.get("_accumulator")
  accumulator_lock = entry.get("_accumulator_lock")
  if accumulator is None or accumulator_lock is None:
    return create_error_response(f"Callback '{callback_id}' has no event accumulator.", with_readme=False)

  # Drain all accumulated events (and report/reset the overflow drop count)
  dropped_event_count_holder = entry.get("_dropped_event_count_holder") or {"count": 0}
  with accumulator_lock:
    events = list(accumulator)
    accumulator.clear()
    dropped_event_count = dropped_event_count_holder.get("count", 0)
    dropped_event_count_holder["count"] = 0

  return {
    "content": [{"type": "text", "text": json.dumps({
      "callback_id": callback_id,
      "event_count": len(events),
      "dropped_event_count": dropped_event_count,
      "events": events,
    }, indent=2)}],
    "isError": False
  }


def handle_unregister_event_callback_operation(params: Dict) -> Dict:
  """Unregister an event callback."""
  callback_id = params.get("callback_id")
  if not callback_id:
    return create_error_response("Parameter 'callback_id' is required.", with_readme=False)

  was_removed = unregister_message_event_callback(callback_id)
  _sqlite_delete_event_callback_spec(callback_id)
  return {
    "content": [{"type": "text", "text": json.dumps({
      "status": "removed" if was_removed else "not_found",
      "callback_id": callback_id,
    }, indent=2)}],
    "isError": False
  }


# ============================================================================
# MCP TOOL DEFINITION
# ============================================================================

# All supported operations
_ALL_OPERATION_NAMES = [
  "readme", "set_bot_token", "get_bot_info",
  "send_message", "get_updates", "list_known_chats",
  "get_message_history", "edit_message", "delete_message",
  "send_photo", "send_document", "send_voice", "send_media_group", "send_location", "send_poll",
  "set_message_reaction", "forward_message", "copy_message",
  "send_chat_action", "get_chat", "get_chat_member", "get_chat_member_count",
  "set_my_commands", "answer_callback_query",
  "pin_chat_message", "unpin_chat_message",
  "get_file", "download_file", "leave_chat",
  "ban_chat_member", "unban_chat_member", "promote_chat_member", "restrict_chat_member",
  "approve_chat_join_request", "decline_chat_join_request",
  "set_chat_description",
  "raw_api_call", "delete_webhook",
  "start_listening", "stop_listening", "get_listening_status", "wait_for_message",
  "enable_persistence", "disable_persistence", "get_persistent_history",
  "purge_history", "restore_state",
  "register_persona", "list_personas", "remove_persona",
  "register_event_callback", "get_callback_events", "unregister_event_callback",
]

TOOLS = [
  {
    "name": TOOL_NAME,
    "description": """Send and receive messages via Telegram bot. Enables AI-to-human chat, group messaging, and interactive conversations.
- Use this tool when you need to communicate with humans via Telegram
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
          "enum": _ALL_OPERATION_NAMES,
          "description": "Operation to perform"
        },
        "bot_token": {
          "type": "string",
          "description": "Telegram Bot API token (from @BotFather). Optional if previously stored via set_bot_token."
        },
        "chat_id": {
          "type": "integer",
          "description": "Telegram chat ID (integer, or '@channelusername' string for public channels/groups). Get numeric IDs from get_updates or list_known_chats."
        },
        "from_chat_id": {
          "type": "integer",
          "description": "Source chat ID (for forward_message, copy_message)"
        },
        "user_id": {
          "type": "integer",
          "description": "Telegram user ID (for get_chat_member, ban, unban, promote, approve/decline join)"
        },
        "text": {
          "type": "string",
          "description": "Message text to send (for send_message, edit_message, answer_callback_query)"
        },
        "message_id": {
          "type": "integer",
          "description": "Message ID (for edit_message, delete_message, forward, copy, pin, unpin, reply_to)"
        },
        "reply_to_message_id": {
          "type": "integer",
          "description": "Message ID to reply to (for send_message, send_photo, send_document)"
        },
        "parse_mode": {
          "type": "string",
          "enum": ["HTML", "Markdown", "MarkdownV2"],
          "description": "Text formatting mode (optional)"
        },
        "disable_link_preview": {
          "type": "boolean",
          "default": False,
          "description": "Disable link previews in messages"
        },
        "disable_notification": {
          "type": "boolean",
          "default": False,
          "description": "Send message silently (no notification sound)"
        },
        "photo_url": {
          "type": "string",
          "description": "Public URL or Telegram file_id of photo to send (for send_photo)"
        },
        "photo_path": {
          "type": "string",
          "description": "Local file path of a photo to UPLOAD from this machine (for send_photo; max 10MB; alternative to photo_url)"
        },
        "document": {
          "type": "string",
          "description": "Public URL or file_id of document to send (for send_document)"
        },
        "document_path": {
          "type": "string",
          "description": "Local file path of a document to UPLOAD from this machine (for send_document; max 50MB; alternative to document)"
        },
        "voice": {
          "type": "string",
          "description": "Public URL or file_id of a voice note (OGG/OPUS) to send (for send_voice)"
        },
        "voice_path": {
          "type": "string",
          "description": "Local file path of a voice note to UPLOAD from this machine (for send_voice; alternative to voice)"
        },
        "media": {
          "type": "array",
          "description": "List of 2-10 InputMedia dicts for send_media_group, e.g. [{\"type\": \"photo\", \"media\": \"URL or file_id\", \"caption\": \"optional\"}]"
        },
        "latitude": {
          "type": "number",
          "description": "Latitude -90..90 (for send_location)"
        },
        "longitude": {
          "type": "number",
          "description": "Longitude -180..180 (for send_location)"
        },
        "question": {
          "type": "string",
          "description": "Poll question text (for send_poll)"
        },
        "poll_options": {
          "type": "array",
          "description": "List of 2-10 poll option strings (for send_poll)"
        },
        "reaction_emoji": {
          "type": "string",
          "description": "Emoji character to react with, e.g. a thumbs-up (for set_message_reaction; omit/empty to clear the reaction). Telegram accepts a fixed emoji set."
        },
        "caption": {
          "type": "string",
          "description": "Caption for photo/document/voice (for send_photo, send_document, send_voice, copy_message; max 1024 chars)"
        },
        "action": {
          "type": "string",
          "enum": ["typing", "upload_photo", "upload_document", "upload_video",
                   "record_voice", "record_video_note", "find_location", "choose_sticker"],
          "description": "Chat action to show (for send_chat_action)"
        },
        "file_id": {
          "type": "string",
          "description": "Telegram file ID (for get_file, download_file)"
        },
        "callback_query_id": {
          "type": "string",
          "description": "Callback query ID to answer (for answer_callback_query)"
        },
        "show_alert": {
          "type": "boolean",
          "default": False,
          "description": "Show alert popup instead of notification (for answer_callback_query)"
        },
        "commands": {
          "type": "array",
          "description": "List of {command, description} dicts (for set_my_commands)"
        },
        "scope": {
          "type": "object",
          "description": "Scope for set_my_commands (e.g. {type: 'all_private_chats'})"
        },
        "language_code": {
          "type": "string",
          "description": "Two-letter ISO 639-1 language code (for set_my_commands)"
        },
        "permissions": {
          "type": "object",
          "description": "Permission flags dict: for promote_chat_member admin rights (is_anonymous/can_* keys, e.g. {can_manage_chat: true}); for restrict_chat_member ChatPermissions (can_* keys; omit for a full mute). Non-whitelisted keys are ignored."
        },
        "until_date": {
          "type": "integer",
          "description": "Unix timestamp when a restriction lifts automatically (for restrict_chat_member; omit for permanent)"
        },
        "only_if_banned": {
          "type": "boolean",
          "default": True,
          "description": "For unban_chat_member: when true (default) unbanning a non-banned user is a no-op; when false Telegram also removes a present member from the chat"
        },
        "auto_escape": {
          "type": "boolean",
          "default": False,
          "description": "For send_message with parse_mode MarkdownV2: escape reserved punctuation outside `code spans` automatically"
        },
        "description": {
          "type": "string",
          "description": "Chat description text (for set_chat_description)"
        },
        "reply_markup": {
          "type": "object",
          "description": "Inline keyboard or reply keyboard markup (JSON object). Example: {\"inline_keyboard\": [[{\"text\": \"Click me\", \"callback_data\": \"btn1\"}]]}"
        },
        "method": {
          "type": "string",
          "description": "Telegram Bot API method name (for raw_api_call)"
        },
        "api_parameters": {
          "type": "object",
          "description": "Parameters dict for the raw API method (for raw_api_call)"
        },
        "timeout": {
          "type": "integer",
          "description": "Long-polling timeout in seconds: for get_updates (clamped 0-30, default 5) and start_listening's background poll (clamped 5-50, default 30)"
        },
        "limit": {
          "type": "integer",
          "default": 20,
          "description": "Max messages to return, clamped 1-500 (for get_message_history, get_persistent_history)"
        },
        "updates_limit": {
          "type": "integer",
          "description": "Batch size passed to Telegram getUpdates, clamped 1-100 (for get_updates; useful when draining large backlogs)"
        },
        "since_timestamp": {
          "type": "number",
          "description": "Unix timestamp — only return messages after this time (for get_persistent_history)"
        },
        "before_timestamp": {
          "type": "number",
          "description": "Unix timestamp — purge_history deletes stored messages OLDER than this"
        },
        "purge_all": {
          "type": "boolean",
          "default": False,
          "description": "For purge_history: explicitly confirm deleting ALL stored messages for this bot"
        },
        "store_raw_json": {
          "type": "boolean",
          "default": False,
          "description": "For enable_persistence: also store each message's full raw update JSON (may contain PII like contact phone numbers; off by default)"
        },
        "drop_pending_updates": {
          "type": "boolean",
          "default": False,
          "description": "For delete_webhook: also discard all pending updates on Telegram's side"
        },
        "wait_seconds": {
          "type": "number",
          "description": "For wait_for_message: max seconds to block waiting for a matching message (1-120, default 30)"
        },
        "persona_id": {
          "type": "string",
          "description": "Unique identifier for a persona (for register_persona, remove_persona)"
        },
        "persona_display_name": {
          "type": "string",
          "description": "Human-readable display name for the persona"
        },
        "persona_system_prompt": {
          "type": "string",
          "description": "System prompt text that defines this persona's behavior"
        },
        "persona_trigger_command": {
          "type": "string",
          "description": "Telegram /command (without /) that activates this persona (e.g. 'poet')"
        },
        "persona_trigger_pattern": {
          "type": "string",
          "description": "Regex pattern — messages matching this are routed to this persona"
        },
        "persona_assigned_chat_ids": {
          "type": "array",
          "description": "List of chat_ids where this persona is the default responder"
        },
        "persona_is_default": {
          "type": "boolean",
          "default": False,
          "description": "If true, this persona handles messages that match no other persona"
        },
        "callback_id": {
          "type": "string",
          "description": "Unique ID for an event callback registration"
        },
        "filter_chat_ids": {
          "type": "array",
          "description": "Only fire callback for messages from these chat_ids"
        },
        "filter_message_types": {
          "type": "array",
          "description": "Only fire callback for these message types (e.g. ['text','callback_query'])"
        },
        "tool_unlock_token": {
          "type": "string",
          "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
        }
      },
      "required": ["operation", "tool_unlock_token"],
      "type": "object"
    },
    "readme": """
# Social Tool - Full Telegram Bot API + Event-Driven Multi-Persona Chat

Complete Telegram Bot API wrapper with event callbacks, sqlite persistence, rate limiting,
and multi-persona routing. Send/receive messages, manage groups, handle inline keyboards,
moderate members, and build event-driven multi-role chatbots.

## Usage-Safety Token

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations (except readme).

## Quick Start

### 1. Set your bot token (one-time setup)
```json
{"input": {"operation": "set_bot_token", "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

### 2. Check bot identity
```json
{"input": {"operation": "get_bot_info", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

### 3. Poll for incoming messages
```json
{"input": {"operation": "get_updates", "timeout": 10, "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

### 4. Send a message
```json
{"input": {"operation": "send_message", "chat_id": 123456789, "text": "Hello!", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

## All Operations

### Messaging
- **send_message** - Send text (with optional inline keyboard, parse_mode, reply_to, silent mode).
  Max 4096 chars. With parse_mode MarkdownV2, add auto_escape:true to escape reserved punctuation
  automatically. If Telegram rejects the formatting, the message is re-sent once as plain text and
  the response includes a parse_mode_fallback note.
- **edit_message** - Edit a previously sent message
- **delete_message** - Delete a message
- **forward_message** - Forward a message (with attribution)
- **copy_message** - Copy a message (no attribution)
- **send_photo** - Send a photo via URL/file_id (photo_url) or upload a LOCAL file (photo_path, max 10MB)
- **send_document** - Send a file via URL/file_id (document) or upload a LOCAL file (document_path, max 50MB)
- **send_voice** - Send a voice note via URL/file_id (voice) or upload a LOCAL file (voice_path)
- **send_media_group** - Send an album of 2-10 items as one message (media: list of InputMedia dicts, URL/file_id only)
- **send_location** - Send a map point (latitude, longitude)
- **send_poll** - Send a native poll (question, poll_options list)
- **set_message_reaction** - React to a message with an emoji (reaction_emoji; empty clears)
- **send_chat_action** - Show typing/uploading indicator

### Receiving
- **get_updates** - Poll for new messages, callback queries, join requests, member updates.
  Optional updates_limit (1-100) controls the Telegram batch size. If the background listener is
  running this serves from local history instead of calling the API (only one getUpdates consumer
  is allowed per bot).
- **wait_for_message** - BLOCK up to wait_seconds (1-120) until a matching message arrives
  (optional filter_chat_ids / filter_message_types). Requires start_listening. One efficient call
  instead of a poll loop.
- **get_message_history** - Read stored messages from in-memory history (limit clamped 1-500)
- **list_known_chats** - List all chats that have messaged the bot

### Chat Management
- **get_chat** - Get full info about a chat
- **get_chat_member** - Get info about a specific member
- **get_chat_member_count** - Count members in a chat
- **set_chat_description** - Set group/channel description
- **leave_chat** - Leave a group/channel
- **pin_chat_message** - Pin a message
- **unpin_chat_message** - Unpin a message

### Member Moderation
- **ban_chat_member** - Ban a user
- **unban_chat_member** - Unban a user (only_if_banned defaults true; set false to also remove a present member)
- **promote_chat_member** - Promote/demote (set admin permissions; only is_anonymous/can_* keys accepted)
- **restrict_chat_member** - Mute/limit a user (ChatPermissions can_* keys; omit permissions for full mute; optional until_date)
- **approve_chat_join_request** - Approve a pending join request
- **decline_chat_join_request** - Decline a pending join request

### Bot Configuration
- **set_bot_token** - Store and validate a bot token
- **get_bot_info** - Get bot identity (getMe)
- **set_my_commands** - Register bot commands with Telegram's menu

### Inline Keyboards & Callbacks
- **answer_callback_query** - Acknowledge an inline button press
- Send inline keyboards via reply_markup in send_message/edit_message

### Files
- **download_file** - PREFERRED: fetch a file sent to the bot server-side into the user data
  downloads directory and return the local path (max 20MB; keeps the bot token out of transcripts)
- **get_file** - Get the raw download URL for a file (WARNING: the URL embeds the bot token)

### Background Listening
- **start_listening** - Start continuous background polling (optional timeout 5-50s per long poll).
  After 20 consecutive polling failures the listener auto-stops and records the reason.
- **stop_listening** - Stop background polling (terminates after the in-flight long poll, worst
  case roughly the poll timeout plus 10s network slack)
- **get_listening_status** - Check if background polling is active, plus health info
  (consecutive_error_count, last_error, stopped_reason when auto-stopped)

### Event Callbacks (push-style notifications)
- **register_event_callback** - Register a named event queue that accumulates messages from the background poller
- **get_callback_events** - Drain accumulated events from a registered callback (non-blocking)
- **unregister_event_callback** - Remove a callback

### SQLite Persistence (opt-in)
- **enable_persistence** - Enable sqlite-backed storage for messages, chats, and personas (survives
  restarts). Message text and metadata are stored INDEFINITELY until purged - use purge_history for
  retention control. Full raw update JSON (which can include contact phone numbers, locations, etc)
  is NOT stored unless you pass store_raw_json:true.
- **disable_persistence** - Disable sqlite storage and close the db (in-memory state continues working)
- **get_persistent_history** - Query message history from the sqlite store (with chat_id, limit, since_timestamp filters)
- **purge_history** - Delete stored messages by chat_id and/or before_timestamp, or everything with purge_all:true
- **restore_state** - After a server restart: re-start listening and re-register event callbacks recorded before the restart

### Multi-Persona Routing
- **register_persona** - Register a persona with system_prompt, trigger_command, trigger_pattern, assigned_chat_ids
- **list_personas** - List all registered personas
- **remove_persona** - Remove a persona

Persona resolution order: /command match, then chat assignment, then regex pattern, then default.
Within each tier FIRST REGISTERED WINS - register higher-priority personas first.

### Escape Hatch
- **raw_api_call** - Call ANY Telegram Bot API method directly (for methods not listed above)
- **delete_webhook** - Remove a webhook that blocks getUpdates polling (fixes 409 conflicts; optional drop_pending_updates)

## Event Callback Example (event-driven chat)
```json
// Step 1: Start listening and register a callback
{"input": {"operation": "start_listening", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
{"input": {"operation": "register_event_callback", "callback_id": "my_chat_handler",
  "filter_chat_ids": [123456789], "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}

// Step 2: Poll for events (call periodically — returns instantly if no events)
{"input": {"operation": "get_callback_events", "callback_id": "my_chat_handler",
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}

// Step 3: Clean up when done
{"input": {"operation": "unregister_event_callback", "callback_id": "my_chat_handler",
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

## Multi-Persona Example
```json
// Register two personas
{"input": {"operation": "register_persona", "persona_id": "poet",
  "persona_display_name": "The Poet", "persona_system_prompt": "You are a romantic poet. Respond in verse.",
  "persona_trigger_command": "poet", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}

{"input": {"operation": "register_persona", "persona_id": "helper",
  "persona_display_name": "Helpful Assistant", "persona_system_prompt": "You are a helpful assistant.",
  "persona_is_default": true, "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```
When a user sends `/poet tell me about love`, the message will have `matched_persona_id: "poet"`.
All other messages match `"helper"` (the default persona).

## SQLite Persistence Example
```json
// Enable (creates social_telegram.db in user data directory)
{"input": {"operation": "enable_persistence", "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}

// Query stored messages
{"input": {"operation": "get_persistent_history", "chat_id": 123, "limit": 100,
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

## Rate Limiting
ALL send-class operations (send_message, send_photo, send_document, send_voice, send_media_group,
send_location, send_poll, forward_message, copy_message, edit_message) are automatically
rate-limited per chat (token bucket: 1 msg/s, burst up to 3). HTTP 429 responses honor Telegram's
retry_after (bounded). This prevents Telegram 429 errors in fast multi-persona scenarios. No
configuration needed.

## Inline Keyboard Example
```json
{"input": {"operation": "send_message", "chat_id": 123, "text": "Pick one:",
  "reply_markup": {"inline_keyboard": [[
    {"text": "Option A", "callback_data": "a"},
    {"text": "Option B", "callback_data": "b"}
  ]]},
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

## Raw API Call Example
```json
{"input": {"operation": "raw_api_call", "method": "setChatTitle",
  "api_parameters": {"chat_id": -100123, "title": "New Title"},
  "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"}}
```

## Notes
- Bot tokens can be passed per-call via bot_token, or stored once via set_bot_token.
- SECURITY: the bot token grants FULL control of the bot (sending as it, reading its updates,
  leaving chats). set_bot_token stores it in plaintext in this installation's config file
  (nativemessaging.json) - treat that file, and any get_file download_url, as secrets.
- The bot can only receive messages from users who have started a conversation (/start).
- get_updates receives: messages, edits, callback queries, join requests, and member updates.
- chat_id accepts '@channelusername' strings for public channels/groups as well as numeric IDs.
- Photo/document captions fall back into the message 'text' field for routing and history.
- In-memory history (500 msgs per bot) works always. SQLite persistence is opt-in via
  enable_persistence; stored messages are kept until purge_history removes them.
- Personas are stored in memory and also in sqlite when persistence is enabled.
- Event callbacks fire in the background poller thread — keep handlers fast or offload to another thread.
- For any Telegram API method not listed, use raw_api_call as an escape hatch.
"""
  }
]


# ============================================================================
# PARAMETER VALIDATION
# ============================================================================

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
  """Validate input parameters against the real_parameters schema.

  Returns:
    Tuple of (error_message_or_none, validated_params_dict)
  """
  real_params_schema = TOOLS[0]["real_parameters"]
  properties = real_params_schema["properties"]
  required = real_params_schema.get("required", [])

  # For readme operation, don't require token
  operation = input_param.get("operation")
  if operation == "readme":
    required = ["operation"]

  # Check for unexpected parameters
  expected_params = set(properties.keys())
  provided_params = set(input_param.keys())
  unexpected_params = provided_params - expected_params

  if unexpected_params:
    return (f"Unexpected parameters: {', '.join(sorted(unexpected_params))}. "
            f"Expected: {', '.join(sorted(expected_params))}"), {}

  # Check for missing required parameters
  missing_required = set(required) - provided_params
  if missing_required:
    return f"Missing required parameters: {', '.join(sorted(missing_required))}", {}

  # Validate types and extract values
  validated = {}
  for param_name, param_schema in properties.items():
    if param_name in input_param:
      value = input_param[param_name]
      expected_type = param_schema.get("type")

      if expected_type == "string" and not isinstance(value, str):
        return f"Parameter '{param_name}' must be a string, got {type(value).__name__}", {}
      elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        # bool is a subclass of int, so it must be excluded explicitly above
        # Allow string chat_id/user_id/from_chat_id (Telegram accepts both)
        if param_name in ("chat_id", "user_id", "from_chat_id", "message_id", "reply_to_message_id") and isinstance(value, str):
          if param_name in ("chat_id", "from_chat_id") and re.match(r'^@[A-Za-z0-9_]{4,}$', value):
            pass  # @channelusername is a valid Telegram chat id - pass through unconverted
          else:
            try:
              value = int(value)
            except ValueError:
              return f"Parameter '{param_name}' must be an integer, got non-numeric string", {}
        else:
          return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}", {}
      elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return f"Parameter '{param_name}' must be a number, got {type(value).__name__}", {}
      elif expected_type == "boolean" and not isinstance(value, bool):
        return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}", {}
      # array and object types are loosely validated (just check they exist)
      elif expected_type == "array" and not isinstance(value, list):
        return f"Parameter '{param_name}' must be an array/list, got {type(value).__name__}", {}
      elif expected_type == "object" and not isinstance(value, dict):
        return f"Parameter '{param_name}' must be an object/dict, got {type(value).__name__}", {}

      # Enum validation
      if "enum" in param_schema:
        allowed_values = param_schema["enum"]
        if value not in allowed_values:
          return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'", {}

      validated[param_name] = value
    else:
      # (Missing required params were already rejected above, so no re-check here)
      default_value = param_schema.get("default")
      if default_value is not None:
        validated[param_name] = default_value

  return None, validated


# ============================================================================
# README AND ERROR HELPERS
# ============================================================================

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
  return {
    "content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}],
    "isError": True
  }


# ============================================================================
# MAIN TOOL HANDLER
# ============================================================================

# Operation name -> handler function mapping
_OPERATION_HANDLER_DISPATCH_TABLE = {
  "set_bot_token": handle_set_bot_token_operation,
  "get_bot_info": handle_get_bot_info_operation,
  "send_message": handle_send_message_operation,
  "get_updates": handle_get_updates_operation,
  "list_known_chats": handle_list_known_chats_operation,
  "get_message_history": handle_get_message_history_operation,
  "edit_message": handle_edit_message_operation,
  "delete_message": handle_delete_message_operation,
  "send_photo": handle_send_photo_operation,
  "send_document": handle_send_document_operation,
  "send_voice": handle_send_voice_operation,
  "send_media_group": handle_send_media_group_operation,
  "send_location": handle_send_location_operation,
  "send_poll": handle_send_poll_operation,
  "set_message_reaction": handle_set_message_reaction_operation,
  "forward_message": handle_forward_message_operation,
  "copy_message": handle_copy_message_operation,
  "send_chat_action": handle_send_chat_action_operation,
  "get_chat": handle_get_chat_operation,
  "get_chat_member": handle_get_chat_member_operation,
  "get_chat_member_count": handle_get_chat_member_count_operation,
  "set_my_commands": handle_set_my_commands_operation,
  "answer_callback_query": handle_answer_callback_query_operation,
  "pin_chat_message": handle_pin_chat_message_operation,
  "unpin_chat_message": handle_unpin_chat_message_operation,
  "get_file": handle_get_file_operation,
  "download_file": handle_download_file_operation,
  "leave_chat": handle_leave_chat_operation,
  "ban_chat_member": handle_ban_chat_member_operation,
  "unban_chat_member": handle_unban_chat_member_operation,
  "promote_chat_member": handle_promote_chat_member_operation,
  "restrict_chat_member": handle_restrict_chat_member_operation,
  "approve_chat_join_request": handle_approve_chat_join_request_operation,
  "decline_chat_join_request": handle_decline_chat_join_request_operation,
  "set_chat_description": handle_set_chat_description_operation,
  "raw_api_call": handle_raw_api_call_operation,
  "delete_webhook": handle_delete_webhook_operation,
  "start_listening": handle_start_listening_operation,
  "stop_listening": handle_stop_listening_operation,
  "get_listening_status": handle_get_listening_status_operation,
  "wait_for_message": handle_wait_for_message_operation,
  "enable_persistence": handle_enable_persistence_operation,
  "disable_persistence": handle_disable_persistence_operation,
  "get_persistent_history": handle_get_persistent_history_operation,
  "purge_history": handle_purge_history_operation,
  "restore_state": handle_restore_state_operation,
  "register_persona": handle_register_persona_operation,
  "list_personas": handle_list_personas_operation,
  "remove_persona": handle_remove_persona_operation,
  "register_event_callback": handle_register_event_callback_operation,
  "get_callback_events": handle_get_callback_events_operation,
  "unregister_event_callback": handle_unregister_event_callback_operation,
}


def handle_social(input_param: Dict) -> Dict:
  """Main entry point for the social tool - routes operations to handlers."""
  try:
    # Pop off synthetic handler_info parameter early (before validation)
    handler_info = input_param.pop('handler_info', None)

    # Collapse the single-input wrapper
    if isinstance(input_param, dict) and "input" in input_param:
      input_param = input_param["input"]

    # Handle readme operation first (before token validation)
    if isinstance(input_param, dict) and input_param.get("operation") == "readme":
      return {
        "content": [{"type": "text", "text": readme(True)}],
        "isError": False
      }

    # Validate input structure
    if not isinstance(input_param, dict):
      return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)

    # Check for token - if missing or invalid, return readme
    provided_token = input_param.get("tool_unlock_token")
    if provided_token != TOOL_UNLOCK_TOKEN:
      return create_error_response(
        "Invalid or missing tool_unlock_token. Read the documentation below to get the correct token:",
        with_readme=True
      )

    # Validate all parameters using schema
    error_msg, validated_params = validate_parameters(input_param)
    if error_msg:
      return create_error_response(error_msg, with_readme=True)

    # Extract operation and route via dispatch table
    # (operation == "readme" was already handled before token validation above)
    operation = validated_params.get("operation")

    handler_function = _OPERATION_HANDLER_DISPATCH_TABLE.get(operation)
    if handler_function:
      return handler_function(validated_params)
    else:
      return create_error_response(
        f"Unknown operation: '{operation}'. Available: {', '.join(_ALL_OPERATION_NAMES)}",
        with_readme=True
      )

  except Exception as e:
    return create_error_response(f"Error in social operation: {str(e)}", with_readme=True)


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

HANDLERS = {
  TOOL_NAME: handle_social
}
