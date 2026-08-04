"""
File: ragtag/tools/sqlite.py
Project: Aura Friday MCP-Link Server
Component: SQLite Database Tool
Author: Christopher Nathan Drake (cnd)

NOTICE: This is a HELPER TOOL.  This tool is NOT the "RagTag" memory system itself.  This tool exists to help us with development of RagTag.

RagTag Memory System - Direct SQLite Tool

Tool implementation for direct SQLite database operations.

Install:
    pip install pysqlite3-binary

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "XНЅnωⲔΤɡɋƲBOzⅼхƦ𝟢ԛеӠ𝟫ᒿ1TꓓȠwƘTɪIƘᴛ𝟙ⴹΥOJꓮLƴƨƛСօďꓓʋƙ3ᴍ𝛢Ƨ𝟤B9ᛕꓮбОwƳꓴmɡ𝘈ȠꓮⲞƲƻy𝟫fⲔꓪBʋꓜҳŧꓝꓮ𝟚ȷҮȣҳօΤƻᗪМ𝟦ᏂƌʌZoꓐꓠΥtᴜꓳΜƋꓣꓑ"
"signdate": "2026-07-23T02:39:45.331Z",
"""

import os
import sys
import json
import math
import time
import struct
import functools
import threading
import urllib.parse
vec_needs_load=True
try:
    import sqlite_vec
except Exception as e:
    vec_needs_load=False
    pass
import base64
from datetime import datetime
from typing import Dict, List, Any, Optional, Union, Tuple
from pathlib import Path
from easy_mcp.server import MCPLogger, get_tool_token
from .qwen_embedding_06 import generate_embedding
from . import get_authenticated_user
from ragtag.shared_config import get_user_data_directory, get_ragtag_config
from platformdirs import user_data_dir, user_log_dir, user_cache_dir, user_config_dir, site_data_dir
import tempfile
try:
    import pysqlite3.dbapi2 as sqlite3
    sys.modules['sqlite3'] = sqlite3
except Exception as e:
    import sqlite3
YEL = '\033[33;1m'
NORM = '\033[0m'

# Constants
TOOL_LOG_NAME = "SQLITE"
NEWLINE = '\n'  # For f-string compatibility with older Python versions
APP_NAME = "ragtag"
APP_AUTHOR = "AuraFriday"

# Result/robustness defaults (review items 16-20): row cap, blob cap, statement
# wall-clock budget, and file-connection lock timeouts.
DEFAULT_MAX_ROWS_RETURNED_PER_RESULT_SET = 1000
DEFAULT_MAX_BLOB_BYTES_RETURNED_INLINE = 1024
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 300  # generous: first embedding call may load/download the model inside a query
FILE_DATABASE_CONNECT_TIMEOUT_SECONDS = 30
FILE_DATABASE_BUSY_TIMEOUT_MILLISECONDS = 30000
PROGRESS_HANDLER_VDBE_OPCODE_CHECK_INTERVAL = 5000
MAX_BINDING_VALUE_CHARS_IN_LOG = 200

# Serializes all statement execution on the single shared ':memory:' connection,
# so two concurrent tool calls cannot interleave cursors/transactions on it (review item 15).
_memory_database_execution_lock = threading.Lock()

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"sqlite{TOOL_NAME_SUFFIX}"

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        # The "description"  Key is the only thing that persists in the AI context at all times. Keep this as brief as possible, but, it must include everything an AI needs to know in order to work out if it should use this tool, and needs to clearly tell the AI to use the read me operation to find out how to do that.
        "description": """Execute SQLite database commands. Includes semantic similarity search and automatic vector embedding generation.
- Use this when you need to execute SQLite commands or work on tasks that need database and/or semantic searches
""",
        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """Execute SQLite commands and return results in JSON format. Key features:

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {
     "input": {"operation": "readme"}
   }
   (the legacy form {"input": {"readme": true}} is also accepted)

2. For SQL operations:
   {
     "input": {
       "sql": "SELECT * FROM table_name",
       "database": ":memory:",
       "bindings": {"param": "value"},
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Authentication Integration:
- All tool calls automatically receive the authenticated username
- Username is available as :authenticated_user parameter in SQL queries
- The server always sets this binding itself; any caller-supplied value for it is
  overwritten (and logged), so it is trustworthy for audit trails
- When the caller has no authenticated identity, the binding is SQL NULL (not absent),
  so queries referencing :authenticated_user never fail with a bindings error
- Useful for user-specific data access and audit trails

## Basic Database Operations:
- Database: Use ':memory:' for temporary storage (persists until server restart, shared between AI instances)
- Or use filename for persistent database with these path options:
  * Simple filename (e.g. 'data.db') -> stored in the application's user data directory
    (the same directory the .databases dot command lists)
  * Full path (e.g. './data.db' or 'C:\\data.db') -> used as-is
  * Windows environment variables (e.g. '%APPDATA%\\data.db') -> expanded on Windows only
  * Home directory (e.g. '~/data.db') -> expanded to user home on all platforms
  * Cross-platform app data (e.g. '@appdata/data.db') -> uses appropriate OS location:
    - Windows: %APPDATA% (~/AppData/Roaming)
    - macOS: ~/Library/Application Support
    - Linux: ~/.local/share
- Parameters: SQL command with :param style placeholders (e.g. :name, :value)
- Bindings: Pass values safely using bindings object (e.g. {"name": "test", "value": 123})

## Storage Locations
Database paths can be:
- ':memory:' -> Temporary, shared between AI instances until server restart
- '@user_data/db.sqlite' -> Primary storage (syncs on Windows domain)
- '@user_local/db.sqlite' -> Machine-specific storage (never syncs)
- '@user_cache/db.sqlite' -> Temporary data (system may clear)
- '@user_config/db.sqlite' -> Settings/config data
- '@site_data/db.sqlite' -> Multi-user shared (needs elevation)
- '@temp/db.sqlite' -> System temp (cleared on reboot)
- '/absolute/path/db.sqlite' -> Custom location
- 'db.sqlite' -> stored in the application's user data directory (run .databases to see it)

Optional confinement: when the server config key
"sqlite_confine_database_paths_to_directory" is set, file databases outside that
directory tree are refused and the SQL ATTACH command is disabled.

## Optional Parameters (all SQL operations):
- max_rows (integer, default 1000): cap on rows returned per result set. When more rows
  exist, the result carries results_truncated=true and a hint - use LIMIT/OFFSET to page.
- max_blob_bytes (integer, default 1024): BLOB values larger than this are replaced with a
  short placeholder string instead of being inlined. Use hex(col)/length(col)/substr(col,..)
  in SQL to inspect large blobs, or raise max_blob_bytes to retrieve them.
- timeout_seconds (number, default 300, 0 disables): wall-clock budget per call; runaway
  statements (e.g. runaway recursive CTEs) are interrupted. Note the first-ever embedding
  call may load/download the model inside your query - raise/disable if that applies.
- read_only (boolean, default false): open a file database via a read-only URI (mode=ro).
  Writes then fail and missing files are NOT created. Not supported for ':memory:'.
- foreign_keys (boolean, default false): run PRAGMA foreign_keys=ON on this connection.
- create_if_missing (boolean, default true): when false, a missing database file is an
  error instead of being silently created. When a new file IS created, the result reports
  created_new_database_at so typos are visible.

## Transactions and Batches:
- Transactions CANNOT span tool calls: file databases use a fresh connection per call
  (an open transaction is finalized when the call ends), and on ':memory:' any transaction
  left open is committed at the end of the call.
- For a real atomic multi-statement transaction, pass sql as an ARRAY of statements:
  {"input": {"sql": ["INSERT ...", "UPDATE ..."], "database": "my.db", ...}}
  All statements run on ONE connection inside ONE transaction - all-or-nothing (rollback on
  first failure), with per-statement results in "results". One statement per array element;
  do NOT include BEGIN/COMMIT (transaction control is managed for you).
- A single sql string containing multiple ;-separated statements is executed sequentially
  on one connection (stops at the first failure, NOT atomic unless you include your own
  BEGIN/COMMIT statements). Prefer the array form for transactional work.

## Bulk Insert:
For many rows, bulk_insert is far more token-efficient than N INSERT statements:
  {"input": {"bulk_insert": {"table": "events", "columns": ["name", "value"],
             "rows": [["a", 1], ["b", 2]]},
             "database": "my.db", "tool_unlock_token": "..."}}
Runs executemany inside one transaction (all-or-nothing). Values must be plain scalars
(no embedding dicts). "sql" is not required when bulk_insert is provided.

## Vector Similarity Search Support:
- Create tables with vector columns:
```sql
CREATE TABLE documents(
  id INTEGER PRIMARY KEY,
  contents TEXT,
  embedding BLOB CHECK(
    typeof(embedding) == 'blob'
    AND vec_length(embedding) == 1024  -- For Qwen embeddings
  )
);
```

- Automatic Embedding Generation:
  Uses local Qwen3-Embedding-0.6B model (auto-downloaded on first use).
  
  **Simple SQL Function Syntax (RECOMMENDED):**
  ```python
  execute_sql(
      "INSERT INTO docs(text, embedding) VALUES (:text, vec_f32(generate_embedding(text)))"
  )
  
  # Or for bulk updates:
  execute_sql(
      "UPDATE docs SET embedding = vec_f32(generate_embedding(text)) WHERE embedding IS NULL"
  )
  ```
  
  **Legacy Binding Formats (still supported):**

  1. Reference Another Binding; ALWAYS do this when the text of the embedding is also stored in the database:
  ```python
  execute_sql(
      "INSERT INTO docs(text, embedding) VALUES (:text, vec_f32(:embedding))",  # Note: vec_f32() required
      bindings={
          "text": "Some text to store and embed",
          "embedding": {"_embedding_col": "text"}  # Uses text from :text binding
      }
  )
  ```

  2. Direct Text Embedding:
  ```python
  execute_sql(
      "INSERT INTO docs(text, embedding) VALUES (:text, vec_f32(:embedding))",  # Note: vec_f32() required
      bindings={
          "text": "Some text to store",
          "embedding": {"_embedding_text": "Text to embed"} # Only do this if you're not storing the embedded text in the database.
      }
  )
  ```

  Note: generated embeddings are passed to SQLite as compact float32 BLOBs (vec_f32()
  accepts these directly). JSON text arrays like '[0.1, 0.2, ...]' remain supported as
  input to vec_f32() if you construct vectors yourself.

  Similarity Search Examples:
  ```python
  # Basic similarity search
  execute_sql(
      \"\"\"SELECT text, vec_distance_cosine(embedding, vec_f32(:query_vec)) as distance
         FROM docs
         WHERE vec_distance_cosine(embedding, vec_f32(:query_vec)) < 0.5  -- Range: 0-1, lower is more similar
         ORDER BY distance LIMIT 5\"\"\",
      bindings={
          "query_vec": {"_embedding_text": "Find text similar to this"}
      }
  )

  # Find similar to existing document
  execute_sql(
      \"\"\"WITH source AS (SELECT text FROM docs WHERE id = :id)
         SELECT d.text, vec_distance_cosine(d.embedding, vec_f32(:similar_to)) as distance
         FROM docs d, source
         WHERE d.id != :id
         ORDER BY distance LIMIT 5\"\"\",
      bindings={
          "id": 123,
          "similar_to": {"_embedding_col": "text"}  # References text from source CTE
      }
  )
  ```

  Available Distance Functions:
  - vec_distance_cosine(v1, v2) -> float: Cosine similarity (range 0-1, lower=more similar)
  - vec_distance_L2(v1, v2) -> float: Euclidean distance (range 0-inf, lower=more similar)
  - vec_distance_L1(v1, v2) -> float: Manhattan distance (range 0-inf, lower=more similar)

## Hybrid Search (FTS5 + vectors):
FTS5 full-text search and vec vector search can be combined for BM25 + semantic rank
fusion. Documented pattern (adjust weights to taste):
```sql
-- One-time setup: CREATE VIRTUAL TABLE docs_fts USING fts5(text, content=docs, content_rowid=id);
WITH kw AS (SELECT rowid AS id, bm25(docs_fts) AS kw_rank FROM docs_fts WHERE docs_fts MATCH :query),
     sem AS (SELECT id, vec_distance_cosine(embedding, vec_f32(:query_vec)) AS distance FROM docs)
SELECT d.id, d.text, COALESCE(kw.kw_rank, 0) * 0.5 + COALESCE(sem.distance, 1) * 0.5 AS fused
FROM docs d LEFT JOIN kw ON kw.id = d.id LEFT JOIN sem ON sem.id = d.id
ORDER BY fused LIMIT 10;
```
Use the .status dot command to confirm FTS5/RTREE availability in this build.

## Query Diagnosis:
EXPLAIN QUERY PLAN <your query> returns the plan rows directly - use it to self-diagnose
slow queries (e.g. spot full table scans and add indexes).

## Sortable Binary Encoding Functions (BES19):
Store numbers in BLOBs that sort correctly with raw byte comparison (ORDER BY works on BLOBs).

**Encode (number -> sortable BLOB):**
- to_u16bes(n), to_u32bes(n), to_u64bes(n) - unsigned integers
- to_i16bes(n), to_i32bes(n), to_i64bes(n) - signed integers  
- to_f16bes(n), to_f32bes(n), to_f64bes(n) - IEEE 754 floats
- to_t64bes() - current time as epoch microseconds (no args, sortable timestamp)

**Decode (BLOB -> number):**
- from_u16bes(b), from_u32bes(b), from_u64bes(b) - unsigned integers
- from_i16bes(b), from_i32bes(b), from_i64bes(b) - signed integers
- from_f16bes(b), from_f32bes(b), from_f64bes(b) - IEEE 754 floats

**Example:**
```sql
CREATE TABLE events(id INTEGER PRIMARY KEY, ts BLOB, value BLOB);
INSERT INTO events(ts, value) VALUES (to_t64bes(), to_f64bes(-3.14));
SELECT from_i64bes(ts), from_f64bes(value) FROM events ORDER BY ts;
```

## Return Format:
- operation_was_successful: boolean
- error_message_if_operation_failed: string or null
- rows_modified_by_operation: integer for INSERT/UPDATE/DELETE, null when not applicable
  (e.g. CREATE/DROP report null, not -1)
- column_names_in_result_set / data_rows_from_result_set: populated for ANY statement that
  returns rows (SELECT, WITH...SELECT, VALUES, PRAGMA, EXPLAIN, INSERT...RETURNING, ...),
  null otherwise
- last_insert_rowid: integer, included after INSERT
- execution_time_ms: number, wall-clock execution time
- results_truncated / truncation_hint: present when a result set was capped at max_rows
- created_new_database_at: present when this call created a new database file
- results: per-statement result list (multi-statement strings and sql-array batches)
- Non-finite floats (NaN/Infinity) are returned as the strings "NaN"/"Infinity"/"-Infinity"
  so the response is always strict JSON

## Features:
- One shared connection for ':memory:' (serialized); a fresh connection per call for file
  databases (WAL journal mode and a 30s busy timeout are applied automatically)
- Row results as dictionaries with column name access
- Auto-commit for INSERT/UPDATE/DELETE
- Full SQLite feature set available
- Built-in vector similarity search
- Automatic Qwen (local) embedding generation

## Important Limitations:
- Concurrent Access: Only one writer at a time per database
- Memory DB Scope: the ':memory:' database persists for the server lifetime and is ONE
  database shared by ALL users and AI instances of this server - anything stored there is
  visible to every other connected user, so do not put per-user secrets in it
- Transactions cannot span tool calls (see Transactions and Batches above)
- File Location: simple filenames (no path separators) are stored in the application's
  user data directory (the directory the .databases command lists). Use full paths,
  @-prefixes, or ~/ to store elsewhere.
- Embedding Generation: Uses local model (auto-downloads on first use)
- Vector Operations: Always use vec_f32() in SQL for vector parameters

## Common Error Cases:
- 'CHECK constraint failed': Missing vec_f32() in SQL for vector operations
- 'Referenced column not found': Check binding names match SQL parameters
- 'Failed to generate embedding': Model loading or dependency issues (auto-resolved on retry)

## SQLite Dot Commands:
The following dot commands are supported for convenience:
  .databases  - List database files (*.db, *.sqlite, *.sqlite3) in the user data directory
  .tables     - List all tables and views
  .schema     - Show schema for table(s)/view(s)
  .indexes    - Show indexes for table
  .fullschema - Complete schema dump
  .dbinfo     - Show database information
  .status     - Show versions (SQLite/vec), journal mode, sizes, timeouts, feature flags
  .pragma     - List all available PRAGMA names
  .foreign_keys - Show the current foreign_keys setting
  .backup <path> - Copy the current database (including ':memory:') safely to <path>

Note: While dot commands are supported for convenience, standard SQL
queries are preferred as they provide more explicit and complete functionality.

## PRAGMA Support:
All PRAGMA statements are supported using standard SQL syntax (executed natively,
including assignment forms):
  PRAGMA foreign_keys = ON;      -- Enable foreign keys
  PRAGMA journal_mode = WAL;     -- Set journal mode
  PRAGMA synchronous = NORMAL;   -- Configure sync mode
  PRAGMA cache_size = -2000;     -- Set cache size
  PRAGMA page_size;              -- Get page size
  PRAGMA encoding;               -- Get database encoding

## Examples
```json
# Basic SQL query
{
  "input": {
    "sql": "SELECT * FROM users WHERE age > :min_age",
    "database": "myapp.db",
    "bindings": {"min_age": 18},
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

# User-specific query using authenticated username
{
  "input": {
    "sql": "SELECT * FROM user_documents WHERE owner = :authenticated_user",
    "database": "myapp.db",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

# Insert with user tracking
{
  "input": {
    "sql": "INSERT INTO actions (user, action, timestamp) VALUES (:authenticated_user, :action, datetime('now'))",
    "database": "audit.db",
    "bindings": {"action": "document_created"},
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

# Vector similarity search
{
  "input": {
    "sql": "SELECT * FROM documents WHERE vec_distance_cosine(embedding, vec_f32(:vec)) < 0.5",
    "database": "vectors.db",
    "bindings": {"vec": {"_embedding_text": "Find similar documents"}},
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

# Atomic transaction (sql as array)
{
  "input": {
    "sql": ["INSERT INTO accounts(name, balance) VALUES ('a', 100)",
            "UPDATE accounts SET balance = balance - 10 WHERE name = 'a'"],
    "database": "bank.db",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
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
                "sql": {
                    "title": "SQL",
                    "type": ["string", "array"],
                    "description": "The SQL command to execute with optional :param style placeholders. Pass an ARRAY of statements to run them atomically in one transaction. Required unless bulk_insert is provided."
                },
                "database": {
                    "title": "Database",
                    "type": "string",
                    "description": "':memory:' for temporary storage (persists until server restart, shared between AI instances) or filename for persistent database",
                    "default": ":memory:"
                },
                "bindings": {
                    "title": "Parameter Bindings",
                    "type": "object",
                    "description": "Optional key-value pairs for SQL parameter binding",
                    "additionalProperties": True
                },
                "max_rows": {
                    "title": "Max Rows",
                    "type": "integer",
                    "description": "Maximum rows returned per result set (default 1000). Results beyond this set results_truncated=true.",
                    "default": DEFAULT_MAX_ROWS_RETURNED_PER_RESULT_SET
                },
                "max_blob_bytes": {
                    "title": "Max Blob Bytes",
                    "type": "integer",
                    "description": "BLOB values larger than this many bytes are replaced with a placeholder string (default 1024).",
                    "default": DEFAULT_MAX_BLOB_BYTES_RETURNED_INLINE
                },
                "timeout_seconds": {
                    "title": "Timeout Seconds",
                    "type": "number",
                    "description": "Wall-clock execution budget for this call (default 300; 0 disables).",
                    "default": DEFAULT_STATEMENT_TIMEOUT_SECONDS
                },
                "read_only": {
                    "title": "Read Only",
                    "type": "boolean",
                    "description": "Open the file database read-only (mode=ro URI); writes fail and missing files are not created.",
                    "default": False
                },
                "foreign_keys": {
                    "title": "Foreign Keys",
                    "type": "boolean",
                    "description": "Enable PRAGMA foreign_keys=ON for this connection.",
                    "default": False
                },
                "create_if_missing": {
                    "title": "Create If Missing",
                    "type": "boolean",
                    "description": "When false, a missing database file is an error instead of being silently created (default true).",
                    "default": True
                },
                "bulk_insert": {
                    "title": "Bulk Insert",
                    "type": "object",
                    "description": "Bulk row insert spec: {\"table\": str, \"columns\": [str,...], \"rows\": [[...],...]}. Runs executemany in one transaction. 'sql' is not required with this.",
                    "additionalProperties": True
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Comprehension token from the readme. It proves the caller has read the current documentation; it is handed out freely by the readme operation and is NOT an access-control secret."
                }
            },
            "required": ["tool_unlock_token"],
            "title": "sqliteArguments",
            "type": "object"
        }
    }
]


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")  # review#11c: use TOOL_LOG_NAME, not a literal
    # review#12: expose real_parameters (the schema the AI actually needs), not the opaque single-input wrapper
    docs = "\n\n" + json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"] }, indent=2) if with_readme else ""
    return { "content": [{"type": "text", "text": f"{error_msg}{docs}"}], "isError": True }


# review#26/#27: small in-process LRU on top of the qwen disk cache; returns a compact
# float32 BLOB (vec_f32() accepts it directly) instead of JSON text that must be re-parsed.
@functools.lru_cache(maxsize=64)
def _generate_embedding_float32_blob_cached(text: str) -> bytes:
    """Generate an embedding for text and return it as a packed float32 BLOB.

    Raises:
        ValueError: If embedding generation fails
    """
    embedding_result, error = generate_embedding(text)
    if embedding_result is None:
        raise ValueError(f"Failed to generate embedding: {error}")
    return struct.pack(f"{len(embedding_result)}f", *embedding_result)


def register_embedding_functions(conn: sqlite3.Connection) -> None:
    """Register Python embedding functions with SQLite connection.
    
    Args:
        conn: SQLite connection to register functions with
    """
    def generate_embedding_udf(text):
        """SQLite user-defined function to generate embeddings."""
        if not text or not isinstance(text, str):
            return None
        
        try:
            # review#27: return a float32 BLOB (compact, no JSON parse per row)
            return _generate_embedding_float32_blob_cached(text)
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error in embedding UDF: {e}")
            return None
    
    # review#26: deterministic=True lets SQLite reuse the result within a statement
    conn.create_function("generate_embedding", 1, generate_embedding_udf, deterministic=True)
    MCPLogger.log(TOOL_LOG_NAME, "Registered generate_embedding() function with SQLite")

def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into a connection.
    
    Args:
        conn: SQLite connection to load extension into
    """
    if not vec_needs_load:
        return # built-in vec - auto-loads itself now.

    try:
        extension_path = os.path.join(os.path.dirname(sqlite_vec.__file__), 'vec0')
    except Exception as e:
        # review#5: no hardcoded dev-machine fallback path - report and bail out cleanly
        MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: Failed to get sqlite-vec extension path: {e}{NORM}")
        return

    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(extension_path)
        finally:
            # review#5: ALWAYS disable again, even when load_extension throws, so the
            # SQL-level load_extension() function is never left available to later SQL
            conn.enable_load_extension(False)
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully loaded sqlite-vec extension from: {extension_path}")
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: Failed to load sqlite-vec '{extension_path}' extension: {e}{NORM}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")


def _log_sqlite_runtime_capabilities(conn: sqlite3.Connection) -> None:
    """Log effective SQLite version, vec extension version and driver module (review item 24).

    Makes the 'import failed and vec is not built in' case visible in the startup log
    instead of silent per-platform behavior differences.
    """
    try:
        sqlite_version_text = conn.execute("SELECT sqlite_version()").fetchone()[0]
    except Exception:
        sqlite_version_text = "unknown"
    try:
        vec_version_text = conn.execute("SELECT vec_version()").fetchone()[0]
    except Exception:
        vec_version_text = "unavailable (vector functions will fail)"
    MCPLogger.log(TOOL_LOG_NAME, f"SQLite runtime: driver={sqlite3.__name__}, sqlite_version={sqlite_version_text}, sqlite-vec={vec_version_text}")


def _get_confinement_root_directory_or_none() -> Optional[str]:
    """Read the optional confinement root for database paths from server config (review item 40).

    Returns:
        Absolute confinement root directory, or None when confinement is not configured.
    """
    try:
        configured_root = get_ragtag_config().get("sqlite_confine_database_paths_to_directory")
        if configured_root and isinstance(configured_root, str):
            return os.path.abspath(os.path.expandvars(os.path.expanduser(configured_root)))
    except Exception as config_error:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: could not read sqlite confinement config: {config_error}")
    return None


def _enforce_path_confinement_or_raise(resolved_database_file_path: str) -> str:
    """When a confinement root is configured, refuse paths outside it (review item 40)."""
    confinement_root = _get_confinement_root_directory_or_none()
    if confinement_root is None:
        return resolved_database_file_path
    normalized_root = os.path.normcase(os.path.abspath(confinement_root))
    normalized_path = os.path.normcase(os.path.abspath(resolved_database_file_path))
    try:
        path_is_inside_root = os.path.commonpath([normalized_root, normalized_path]) == normalized_root
    except ValueError:
        path_is_inside_root = False  # different drives etc.
    if not path_is_inside_root:
        raise PermissionError(f"Database path '{resolved_database_file_path}' is outside the configured confinement directory '{confinement_root}'")
    return resolved_database_file_path


def _deny_attach_statements_authorizer(action_code, arg1, arg2, database_name, trigger_or_view_name):
    """SQLite authorizer callback: deny ATTACH so SQL cannot escape path confinement (review item 40)."""
    if action_code == getattr(sqlite3, "SQLITE_ATTACH", 24):
        return getattr(sqlite3, "SQLITE_DENY", 1)
    return getattr(sqlite3, "SQLITE_OK", 0)


class SQLiteMemoryDB:
    """Static class to manage the single :memory: database connection."""
    _connection: Optional[sqlite3.Connection] = None
    _lock = threading.Lock()
    _initialized = False

    @classmethod
    def initialize(cls) -> None:
        """Initialize the memory database - called once at server start."""
        with cls._lock:
            if not cls._initialized:
                try:
                    MCPLogger.log(TOOL_LOG_NAME, "Creating shared memory database connection")
                    # review#3: isolation_level=None (autocommit) so DML never leaves an
                    # implicit transaction open forever on this shared connection.
                    # (The former PRAGMA journal_mode=WAL here was a silent no-op: memory
                    # databases always report journal mode 'memory'.)
                    cls._connection = sqlite3.connect(':memory:', check_same_thread=False, isolation_level=None)
                    cls._connection.row_factory = sqlite3.Row
                    load_sqlite_vec(cls._connection)  # Load extension
                    register_embedding_functions(cls._connection)  # Register UDFs
                    if _get_confinement_root_directory_or_none() is not None:
                        cls._connection.set_authorizer(_deny_attach_statements_authorizer)  # review#40
                    cls._initialized = True
                    _log_sqlite_runtime_capabilities(cls._connection)  # review#24
                    MCPLogger.log(TOOL_LOG_NAME, "Memory database initialized successfully")
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize memory database: {e}")
                    try:
                        if cls._connection:
                            cls._connection.close()
                    except Exception:
                        pass
                    cls._connection = None
                    raise

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Get the memory database connection (retrying initialization lazily if needed)."""
        if not cls._initialized or not cls._connection:
            # review#30: a failed startup initialization is retried lazily here instead
            # of leaving the memory database permanently unavailable
            cls.initialize()
        if not cls._initialized or not cls._connection:
            raise RuntimeError("Memory database not initialized or connection lost")
        return cls._connection

def initialize_tool() -> None:
    """Initialize the SQLite tool - called once when server starts."""
    try:
        SQLiteMemoryDB.initialize()
    except Exception as memory_db_init_error:
        # review#30: do not kill tool load; degrade to "memory DB unavailable" with lazy retry
        MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: memory database unavailable at startup (will retry on first ':memory:' use): {memory_db_init_error}{NORM}")

# Map of supported dot commands to their SQL equivalents
# review#23: table-name args are quote-escaped; review#29: include views to match sqlite3 CLI
DOT_COMMANDS = {
    'databases': None,  # Special handling - lists database files in user data directory
    'backup': None,  # Special handling - copies the database to a destination path (review item 43)
    'tables': "SELECT name FROM sqlite_master WHERE type IN ('table','view')",
    'schema': lambda tbl: "SELECT sql FROM sqlite_master WHERE type IN ('table','view')" + (" AND tbl_name = '{}'".format(tbl.replace("'", "''")) if tbl else ""),
    'indexes': lambda tbl: "SELECT name FROM sqlite_master WHERE type='index'" + (" AND tbl_name = '{}'".format(tbl.replace("'", "''")) if tbl else ""),
    'fullschema': "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY tbl_name, type DESC, name",
    'dbinfo': "SELECT 'Database Size' as name, page_count * page_size as value FROM pragma_page_count, pragma_page_size UNION ALL SELECT 'Foreign Keys', foreign_keys FROM pragma_foreign_keys UNION ALL SELECT 'Journal Mode', journal_mode FROM pragma_journal_mode",
    # review#8: show the foreign_keys SETTING (pragma_foreign_key_list needs a table arg and errored)
    'foreign_keys': "SELECT * FROM pragma_foreign_keys",
    # review#8: list PRAGMA names (pragma_function_list listed SQL functions, not pragmas)
    'pragma': "SELECT * FROM pragma_pragma_list",
    # review#45: enriched status - versions, sizes, modes, timeouts (vec/path/features appended in handler)
    'status': """
        SELECT 'SQLite Version' as setting, sqlite_version() as value
        UNION ALL
        SELECT 'Database Size', page_count * page_size FROM pragma_page_count, pragma_page_size
        UNION ALL
        SELECT 'Page Size', page_size FROM pragma_page_size
        UNION ALL
        SELECT 'Page Count', page_count FROM pragma_page_count
        UNION ALL
        SELECT 'Freelist Count', freelist_count FROM pragma_freelist_count
        UNION ALL
        SELECT 'Journal Mode', journal_mode FROM pragma_journal_mode
        UNION ALL
        SELECT 'Busy Timeout (ms)', timeout FROM pragma_busy_timeout
        UNION ALL
        SELECT 'Synchronous', synchronous FROM pragma_synchronous
        UNION ALL
        SELECT 'Foreign Keys', foreign_keys FROM pragma_foreign_keys
        UNION ALL
        SELECT 'Encoding', encoding FROM pragma_encoding
    """
}

# List of unsupported dot commands with helpful messages
UNSUPPORTED_COMMANDS = {
    'mode': "Output format is always JSON for programmatic use",
    'output': "Results are returned directly to the caller",
    'separator': "Results are structured as JSON objects",
    'headers': "Column headers are always included in the JSON response",
    'timer': "Timing information is not supported in this context",
    'restore': "Handle database restoration at the application level",
    'dump': "Use .backup <path> for a full safe copy, .schema or .fullschema for structure, or SQL queries for data export",
    'import': "Use SQL INSERT statements or the bulk_insert parameter for data import",
    'save': "Use .backup <path> to copy the database. Use SQL for data operations",
    'read': "Use SQL directly instead of reading from files",
    'shell': "This is not a shell CLI. Use appropriate API calls for system operations"
}

def handle_backup_dot_command(destination: Optional[str], database: str) -> Dict[str, Any]:
    """Handle the .backup dot command: safely copy the database to a destination file.

    Uses the SQLite online backup API (works for ':memory:' and live WAL databases),
    which is safer than copying database files directly (review item 43).

    Args:
        destination: Destination path (same path syntax as the database parameter)
        database: Source database name or ':memory:'

    Returns:
        Dict with standard tool response format
    """
    if not destination or not destination.strip():
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": "Usage: .backup <destination_path> - destination file path is required",
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }

    source_conn = None
    destination_conn = None
    memory_lock_acquired = False
    try:
        destination_path = get_db_path(destination.strip())
        if database != ':memory:' and not os.path.exists(get_db_path(database)):
            # connecting would silently create an empty source file - refuse instead
            raise ValueError(f"Source database does not exist: {get_db_path(database)}")
        if database == ':memory:':
            # Hold the execution lock so no other call writes mid-backup on the shared connection
            _memory_database_execution_lock.acquire()
            memory_lock_acquired = True
        source_conn = get_connection(database)
        destination_conn = sqlite3.connect(destination_path)
        source_conn.backup(destination_conn)
        destination_conn.close()
        destination_conn = None
        backup_size_bytes = os.path.getsize(destination_path)
        MCPLogger.log(TOOL_LOG_NAME, f"Backed up '{database}' to '{destination_path}' ({backup_size_bytes} bytes)")
        return {
            "operation_was_successful": True,
            "error_message_if_operation_failed": None,
            "rows_modified_by_operation": None,
            "column_names_in_result_set": ["backup_destination_path", "backup_size_bytes"],
            "data_rows_from_result_set": [{"backup_destination_path": destination_path, "backup_size_bytes": backup_size_bytes}]
        }
    except Exception as e:
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": f"Backup failed: {str(e)}",
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
    finally:
        if destination_conn:
            try:
                destination_conn.close()
            except Exception:
                pass
        if database != ':memory:' and source_conn:
            try:
                source_conn.close()
            except Exception:
                pass
        if memory_lock_acquired:
            _memory_database_execution_lock.release()

def handle_dot_command(command: str, args: Optional[str] = None, database: str = ':memory:') -> Dict[str, Any]:
    """Handle SQLite dot commands.
    
    Args:
        command: The dot command without the dot (e.g. 'tables')
        args: Optional arguments for the command
        database: Target database
        
    Returns:
        Dict with standard tool response format
    """
    # Check if command is unsupported
    if command in UNSUPPORTED_COMMANDS:
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": (
                f"The command '.{command}' is not supported as this is not a shell CLI.{NEWLINE}"
                f"Reason: {UNSUPPORTED_COMMANDS[command]}{NEWLINE}{NEWLINE}"
                f"Supported dot commands: {', '.join(sorted('.' + cmd for cmd in DOT_COMMANDS))}{NEWLINE}"
                "Note: Standard SQL queries are preferred over dot commands."
            ),
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
    
    # Check if command is supported
    if command not in DOT_COMMANDS:
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": (
                f"Unknown command '.{command}'{NEWLINE}"
                f"Supported dot commands: {', '.join(sorted('.' + cmd for cmd in DOT_COMMANDS))}{NEWLINE}"
                "Note: Standard SQL queries are preferred over dot commands."
            ),
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
    
    # Special handling for databases command
    if command == 'databases':
        try:
            # (review#14: removed redundant local imports of os/datetime - module-level imports suffice)
            # Get user data directory
            user_data_path = get_user_data_directory()
            
            # Find all database files (review#9: not just *.db - our own docs use .sqlite)
            db_files = []
            if user_data_path.exists():
                for file_glob_pattern in ("*.db", "*.sqlite", "*.sqlite3"):
                    for db_file in user_data_path.glob(file_glob_pattern):
                        try:
                            stat_info = db_file.stat()
                            size = stat_info.st_size
                            mtime = datetime.fromtimestamp(stat_info.st_mtime)
                            
                            db_files.append({
                                "filename": db_file.name,
                                "size_bytes": size,
                                "last_modified": mtime.strftime("%Y-%m-%d %H:%M:%S"),
                                "full_path": str(db_file)
                            })
                        except (OSError, IOError) as e:
                            # Skip files we can't access
                            continue
            
            # Sort by filename for consistent output
            db_files.sort(key=lambda x: x["filename"])

            # review#9: also report the shared in-memory database and the default directory
            db_files.append({
                "filename": ":memory:",
                "size_bytes": None,
                "last_modified": None,
                "full_path": ":memory: (shared in-server database, cleared on restart)"
            })
            
            return {
                "operation_was_successful": True,
                "error_message_if_operation_failed": None,
                "rows_modified_by_operation": None,
                "column_names_in_result_set": ["filename", "size_bytes", "last_modified", "full_path"],
                "data_rows_from_result_set": db_files,
                "default_directory": str(user_data_path)
            }
            
        except Exception as e:
            return {
                "operation_was_successful": False,
                "error_message_if_operation_failed": f"Error listing databases: {str(e)}",
                "rows_modified_by_operation": None,
                "column_names_in_result_set": None,
                "data_rows_from_result_set": None
            }

    # Special handling for backup command (review item 43)
    if command == 'backup':
        return handle_backup_dot_command(args, database)
    
    # Get the SQL for this command
    sql_cmd = DOT_COMMANDS[command]
    if callable(sql_cmd):
        sql_cmd = sql_cmd(args)
    
    # Execute the command using our standard SQL execution
    result = sqlite(sql_cmd, database)

    # review#45: append best-effort extras to .status (kept separate so a missing vec
    # extension or compile-option function cannot break the whole status query)
    if command == 'status' and result.get("operation_was_successful"):
        extra_rows = []
        vec_probe = sqlite("SELECT vec_version() AS value", database)
        if vec_probe.get("operation_was_successful") and vec_probe.get("data_rows_from_result_set"):
            extra_rows.append({"setting": "Vec Version", "value": vec_probe["data_rows_from_result_set"][0].get("value")})
        else:
            extra_rows.append({"setting": "Vec Version", "value": "unavailable"})
        feature_probe = sqlite("SELECT sqlite_compileoption_used('ENABLE_FTS5') AS fts5, sqlite_compileoption_used('ENABLE_RTREE') AS rtree", database)
        if feature_probe.get("operation_was_successful") and feature_probe.get("data_rows_from_result_set"):
            feature_row = feature_probe["data_rows_from_result_set"][0]
            extra_rows.append({"setting": "FTS5 Available", "value": feature_row.get("fts5")})
            extra_rows.append({"setting": "RTREE Available", "value": feature_row.get("rtree")})
        try:
            database_path_text = get_db_path(database) if database != ':memory:' else ':memory: (shared in-server database)'
        except Exception as path_error:
            database_path_text = f"unresolvable: {path_error}"
        extra_rows.append({"setting": "Database Path", "value": database_path_text})
        result["data_rows_from_result_set"] = (result.get("data_rows_from_result_set") or []) + extra_rows
    return result

def get_db_path(database: str) -> str:
    """Get the full path for a database file.
    
    Args:
        database: Database name or ':memory:' or path
            - ':memory:' for in-memory database
            - Full path (e.g. '/path/to/data.db') -> used as-is after expansion
            - Simple filename (e.g. 'data.db') -> stored in user data dir
            
            Special @-prefixes for OS-appropriate storage:
            - @user_data/    -> Primary storage, syncs on Windows domain
                * Win: AppData/Roaming
                * Mac/Lin: Library/Application Support, .local/share
            - @user_local/   -> Machine-specific storage, never syncs
                * Win: AppData/Local
                * Mac/Lin: same as @user_data
            - @user_cache/   -> Temporary/regeneratable data
                * All OS: Cleared by system, don't store valuable data
            - @user_config/  -> Settings and configuration
                * Win: AppData/Roaming
                * Mac: Library/Preferences
                * Lin: .config
            - @site_data/    -> Multi-user shared storage
                * Requires elevated permissions
                * Not recommended for personal data
            - @temp/         -> System temp directory
                * Cleared on reboot, use only for scratch data
        
    Returns:
        str: Full path to database or ':memory:'
        
    Raises:
        ValueError: If path expansion fails or results in invalid path
        PermissionError: If target directory is not writable or path is outside the confinement root
    """
    if database == ':memory:':
        return database

    try:
        # Handle special @-prefixes
        if database.startswith('@'):
            parts = database.split('/', 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid @-prefix format in '{database}'. Expected @prefix/filename")
            prefix, filename = parts
            
            # Map prefixes to platformdirs functions and additional path components
            prefix_map = {
                '@user_data': (user_data_dir, [APP_NAME, APP_AUTHOR], {'roaming': True}),
                '@user_local': (user_data_dir, [APP_NAME, APP_AUTHOR], {'roaming': False}),
                '@user_cache': (user_cache_dir, [APP_NAME, APP_AUTHOR], {}),
                '@user_config': (user_config_dir, [APP_NAME, APP_AUTHOR], {}),
                '@site_data': (site_data_dir, [APP_NAME, APP_AUTHOR], {}),
                '@temp': (lambda *args, **kwargs: tempfile.gettempdir(), [], {})
            }
            
            if prefix not in prefix_map:
                valid_prefixes = ', '.join(sorted(prefix_map.keys()))
                raise ValueError(f"Unknown @-prefix '{prefix}'. Valid prefixes: {valid_prefixes}")
            
            # Get the base directory using appropriate platformdirs function
            func, args, kwargs = prefix_map[prefix]
            base = func(*args, **kwargs)
            
            # Ensure the directory exists
            os.makedirs(base, exist_ok=True)
            
            # Check if directory is writable
            if not os.access(base, os.W_OK):
                raise PermissionError(f"Directory not writable: {base}")
            
            return _enforce_path_confinement_or_raise(os.path.abspath(os.path.join(base, filename)))  # review#40
            
        # If path contains / or \ or drive letter (e.g. C:), use it directly
        if '/' in database or '\\' in database or (len(database) > 1 and database[1] == ':'):
            # Expand home directory and environment variables
            expanded = os.path.expanduser(database)
            expanded = os.path.expandvars(expanded)
            expanded = os.path.abspath(expanded)
            
            # Create parent directory if it doesn't exist
            parent = os.path.dirname(expanded)
            if parent:
                os.makedirs(parent, exist_ok=True)
                
            # Check if directory is writable
            if not os.access(parent, os.W_OK):
                raise PermissionError(f"Directory not writable: {parent}")
                
            return _enforce_path_confinement_or_raise(expanded)  # review#40
            
        # For simple filenames, store in user data dir from shared config
        base_path = get_user_data_directory()
        base = str(base_path)  # Convert Path to string for compatibility
        # get_user_data_directory() already ensures directory exists
        if not os.access(base, os.W_OK):
            raise PermissionError(f"Default data directory not writable: {base}")
        return _enforce_path_confinement_or_raise(os.path.join(base, database))  # review#40
        
    except Exception as e:
        if isinstance(e, (ValueError, PermissionError)):
            raise
        raise ValueError(f"Failed to expand database path '{database}': {str(e)}")

def get_connection(database: str, read_only: bool = False, enable_foreign_keys: bool = False) -> sqlite3.Connection:
    """Get database connection - memory shared, files temporary.
    
    Args:
        database: Database name or ':memory:'
        read_only: Open file databases via a read-only URI (mode=ro) - review item 39
        enable_foreign_keys: Run PRAGMA foreign_keys=ON on the new connection - review item 17
        
    Returns:
        sqlite3.Connection: Database connection
    """
    if database == ':memory:':
        return SQLiteMemoryDB.get_connection()
    
    # For files: Create new connection each time
    db_path = get_db_path(database)
    MCPLogger.log(TOOL_LOG_NAME, f"Creating new file connection: {db_path}")
    # review#16: 30s lock-wait timeout instead of the 5s default
    # review#3/#38: isolation_level=None (autocommit) so explicit BEGIN/COMMIT in scripts
    # and the sql-array transactional batch behave predictably
    if read_only:
        read_only_uri = "file:" + urllib.parse.quote(Path(db_path).as_posix(), safe="/:") + "?mode=ro"
        conn = sqlite3.connect(read_only_uri, timeout=FILE_DATABASE_CONNECT_TIMEOUT_SECONDS, isolation_level=None, uri=True)
    else:
        conn = sqlite3.connect(db_path, timeout=FILE_DATABASE_CONNECT_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={FILE_DATABASE_BUSY_TIMEOUT_MILLISECONDS}")  # review#16
        if not read_only:
            # review#17: WAL + NORMAL for better concurrency; WAL persists in the file so
            # setting it on every open is cheap. Skipped read-only (cannot write the header).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        if enable_foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")  # review#17 (opt-in parameter)
        if _get_confinement_root_directory_or_none() is not None:
            conn.set_authorizer(_deny_attach_statements_authorizer)  # review#40
        load_sqlite_vec(conn)  # Load extension
        register_embedding_functions(conn)  # Register UDFs
    except Exception:
        # review#13: never leak the just-opened connection when setup fails
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn

def process_embedding_binding(value: Dict[str, Any], bindings: Dict[str, Any]) -> bytes:
    """Process a special embedding binding value.
    
    Handles two formats:
    1. {"_embedding_text": "text to embed"}  - Directly embeds the given text
    2. {"_embedding_col": "column_name"}     - Embeds text from another binding
    
    Args:
        value: The special binding dictionary
        bindings: Complete bindings dictionary for column reference lookup
        
    Returns:
        bytes: Packed float32 BLOB containing the embedding vector (accepted by vec_f32())
        
    Raises:
        ValueError: If binding format is invalid or referenced column doesn't exist
    """
    # No API key required - using local Qwen model
    
    if "_embedding_text" in value:
        # Direct text embedding
        text = value["_embedding_text"]
        if not isinstance(text, str):
            raise ValueError("_embedding_text value must be a string")
            
    elif "_embedding_col" in value:
        # Reference another binding
        col_name = value["_embedding_col"]
        if not isinstance(col_name, str):
            raise ValueError("_embedding_col value must be a string")
            
        if col_name not in bindings:
            raise ValueError(f"Referenced column '{col_name}' not found in bindings")
            
        text = bindings[col_name]
        if not isinstance(text, str):
            raise ValueError(f"Referenced column '{col_name}' must contain text")
            
    else:
        raise ValueError("Embedding binding must contain either _embedding_text or _embedding_col")
        
    # review#27: compact float32 blob (shared LRU cache) instead of a JSON string
    return _generate_embedding_float32_blob_cached(text)

def process_bindings(bindings: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Process bindings dictionary, handling special embedding values.
    
    Args:
        bindings: Original bindings dictionary or None
        
    Returns:
        Optional[Dict[str, Any]]: Processed bindings with embeddings converted to SQLite format
        
    Raises:
        ValueError: If any embedding binding is invalid or generation fails (review item 28:
        raise instead of the error-prone (None, message) sentinel tuple)
    """
    if not bindings:
        return None
        
    # Make a copy to avoid modifying the original
    processed = bindings.copy()
    
    # review#28: single pass - _embedding_col resolves against the ORIGINAL bindings,
    # so direct-text and column-reference forms are both handled in one loop
    for key, value in processed.items():
        if isinstance(value, dict) and ("_embedding_text" in value or "_embedding_col" in value):
            processed[key] = process_embedding_binding(value, bindings)
            
    return processed

# review#2: convert_pragma_to_select() was deleted. PRAGMA statements now execute natively:
# result detection is based on cursor.description (review#1), so read pragmas return their
# rows and assignment forms (PRAGMA journal_mode = WAL) work instead of being mangled into
# invalid "SELECT * FROM pragma_journal_mode = WAL" SQL.

def _sql_text_contains_executable_statement(sql_text: str) -> bool:
    """Return True when sql_text holds anything beyond whitespace, semicolons and comments.

    Used to drop no-op pieces (trailing comments, stray semicolons) from statement
    splitting, so they cannot turn a single statement into a spurious multi-statement run.
    """
    scan_position = 0
    text_length = len(sql_text)
    while scan_position < text_length:
        current_char = sql_text[scan_position]
        if current_char.isspace() or current_char == ';':
            scan_position += 1
        elif sql_text.startswith('--', scan_position):
            newline_index = sql_text.find('\n', scan_position)
            scan_position = text_length if newline_index == -1 else newline_index + 1
        elif sql_text.startswith('/*', scan_position):
            comment_end_index = sql_text.find('*/', scan_position + 2)
            scan_position = text_length if comment_end_index == -1 else comment_end_index + 2
        else:
            return True
    return False

def _split_sql_statements(sql_text: str) -> List[str]:
    """Split a SQL string into complete statements using sqlite3.complete_statement().

    Unlike a hand-rolled quote scanner, this understands string literals, -- and /* */
    comments, and CREATE TRIGGER ... BEGIN ...; ...; END; bodies (review item 6).

    Args:
        sql_text: Raw SQL text possibly containing multiple ;-separated statements

    Returns:
        List of statement strings (whitespace-trimmed; comment-only/empty pieces dropped)
    """
    statements: List[str] = []
    current_chars: List[str] = []
    for char in sql_text:
        current_chars.append(char)
        if char == ';' and sqlite3.complete_statement(''.join(current_chars)):
            statement_text = ''.join(current_chars).strip()
            if statement_text and _sql_text_contains_executable_statement(statement_text):
                statements.append(statement_text)
            current_chars = []
    leftover_text = ''.join(current_chars).strip()
    if leftover_text and _sql_text_contains_executable_statement(leftover_text):
        statements.append(leftover_text)
    return statements

def _format_bindings_for_log(bindings_dict: Dict[str, Any]) -> str:
    """Format bindings for logging with each value's repr capped (review item 7):
    full embedding vectors (~12KB JSON) and other large/sensitive values are truncated."""
    formatted_parts = []
    for key, value in bindings_dict.items():
        value_repr = repr(value)
        if len(value_repr) > MAX_BINDING_VALUE_CHARS_IN_LOG:
            value_repr = value_repr[:MAX_BINDING_VALUE_CHARS_IN_LOG] + f"...(truncated, {len(value_repr)} chars)"
        formatted_parts.append(f"{key}={value_repr}")
    return "{" + ", ".join(formatted_parts) + "}"

def _sanitize_result_value(value: Any, max_blob_bytes: int) -> Any:
    """Make a single result cell safe for JSON transport.

    - Oversized BLOBs become a short placeholder instead of flooding the AI context (review item 19)
    - Non-finite floats become strings so json.dumps emits strict JSON (review item 21)
    """
    if isinstance(value, bytes) and len(value) > max_blob_bytes:
        return f"<blob {len(value)} bytes; select hex(col)/length(col)/substr(col,start,len) to inspect, or pass a larger max_blob_bytes to retrieve>"
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value

def _execute_single_statement(conn, statement_sql: str, processed_bindings: Optional[Dict[str, Any]],
                              max_rows: int, max_blob_bytes: int) -> Dict[str, Any]:
    """Execute one SQL statement on a fresh cursor and build the standard result dict.

    Result-set detection uses cursor.description (review item 1), so WITH...SELECT, VALUES,
    EXPLAIN, PRAGMA and INSERT/UPDATE/DELETE ... RETURNING all return their rows.
    """
    cursor = conn.cursor()
    # cursor.lastrowid mirrors the CONNECTION-wide last-insert-rowid after any execute()
    # (not just INSERTs), so compare before/after to report it only when THIS statement
    # actually inserted a row (review item 25)
    last_insert_rowid_before_statement = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    try:
        if processed_bindings:
            cursor.execute(statement_sql, processed_bindings)
        else:
            cursor.execute(statement_sql)
    except sqlite3.Error as sqle:
        sqlite_error_text = str(sqle)
        if "JSON" in sqlite_error_text:
            # review#7: no full binding dump in the error (embedding vectors are huge);
            # the truncated formatter keeps the context useful without the noise
            error_context = {
                "sql": statement_sql,
                "processed_bindings": _format_bindings_for_log(processed_bindings) if processed_bindings else None,
                "sqlite_error": sqlite_error_text
            }
            raise ValueError(f"SQLite JSON parsing error - Details:\n{json.dumps(error_context)}")
        raise  # Re-raise other SQLite errors

    result = {
        "operation_was_successful": True,
        "error_message_if_operation_failed": None,
        "rows_modified_by_operation": None,
        "column_names_in_result_set": None,
        "data_rows_from_result_set": None
    }

    if cursor.description is not None:
        # Statement produced a result set - fetch up to max_rows(+1 to detect truncation)
        column_names = [description[0] for description in cursor.description]
        result["column_names_in_result_set"] = column_names
        fetched_rows = cursor.fetchmany(max_rows + 1)  # review#18: bounded fetch
        result_was_truncated = len(fetched_rows) > max_rows
        rows = []
        for row in fetched_rows[:max_rows]:
            rows.append({column_name: _sanitize_result_value(cell_value, max_blob_bytes)
                         for column_name, cell_value in zip(column_names, row)})
        result["data_rows_from_result_set"] = rows
        if result_was_truncated:
            result["results_truncated"] = True
            result["truncation_hint"] = (f"Only the first {max_rows} rows are returned. "
                                         "Use LIMIT/OFFSET to page through results, or pass a larger max_rows.")
        if cursor.rowcount is not None and cursor.rowcount > 0:
            # e.g. INSERT ... RETURNING reports both rows and a modification count
            result["rows_modified_by_operation"] = cursor.rowcount
    else:
        # review#10: DDL reports rowcount -1 - normalize negatives to None
        result["rows_modified_by_operation"] = cursor.rowcount if (cursor.rowcount is not None and cursor.rowcount >= 0) else None

    last_insert_rowid_after_statement = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if last_insert_rowid_after_statement != last_insert_rowid_before_statement:
        result["last_insert_rowid"] = last_insert_rowid_after_statement  # review#25
    cursor.close()  # release the statement promptly (matters on the shared ':memory:' connection when a result set was truncated)
    return result

def _combine_statement_results(results: List[Dict[str, Any]], error_messages: List[str]) -> Dict[str, Any]:
    """Combine per-statement results into one response, keeping the documented shape (review item 6).

    The last statement that produced a result set supplies the top-level columns/rows;
    all individual results remain available under "results".
    """
    last_result_set_owner = None
    for stmt_result in results:
        if stmt_result.get("data_rows_from_result_set") is not None:
            last_result_set_owner = stmt_result
    combined = {
        "operation_was_successful": all(r.get("operation_was_successful") for r in results) and not error_messages,
        "error_message_if_operation_failed": "; ".join(error_messages) if error_messages else None,
        "rows_modified_by_operation": sum((r.get("rows_modified_by_operation") or 0) for r in results),
        "column_names_in_result_set": last_result_set_owner.get("column_names_in_result_set") if last_result_set_owner else None,
        "data_rows_from_result_set": last_result_set_owner.get("data_rows_from_result_set") if last_result_set_owner else None,
        "results": results  # Keep individual results for inspection if needed
    }
    return combined

def sqlite(
    sql: Union[str, List[str]],
    database: str = ':memory:',
    bindings: Optional[Dict[str, Any]] = None,
    *,
    max_rows: Optional[int] = None,
    max_blob_bytes: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
    read_only: bool = False,
    foreign_keys: bool = False,
    create_if_missing: bool = True,
    bulk_insert: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """This function (to Execute a SQL command and return results) can be called two ways:
    1. As an MCP tool via mcp_ragtag_sse_sqlite
    2. Directly by importing from this module
    *. See the TOOLS definition in this module for complete description, usage, and return information.

    Special Features:
    1. SQL Function for Embeddings (RECOMMENDED):
       "UPDATE table SET embedding = vec_f32(generate_embedding(text_column))"
       
    2. Direct Text Embedding (legacy):
       bindings={"embedding": {"_embedding_text": "text to embed"}}
       
    3. Reference Another Binding (legacy):
       bindings={"text": "some text", "embedding": {"_embedding_col": "text"}}
       
    4. SQLite Dot Commands:
       The following dot commands are supported:
       .databases  - List database files in user data directory with size and modification date
       .backup     - Copy the database (including ':memory:') safely to a destination path
       .tables     - List all tables and views
       .schema     - Show schema for table(s)/view(s)
       .indexes    - Show indexes for table
       .fullschema - Complete schema dump
       .dbinfo     - Show database information
       .status     - Show versions, sizes, modes, timeouts and feature availability
       .pragma     - List all available PRAGMA names
       .foreign_keys - Show the current foreign_keys setting
       
       Note: While dot commands are supported for convenience, standard SQL queries
       are preferred as they are more explicit and provide full functionality.
    
    Args:
        sql: SQL command string, list of statements (atomic transaction), or dot command (e.g. ".tables")
        database: Database name or ':memory:'
        bindings: Optional parameter bindings with special embedding support
        max_rows: Cap on rows returned per result set (default DEFAULT_MAX_ROWS_RETURNED_PER_RESULT_SET)
        max_blob_bytes: BLOBs above this size are returned as placeholders (default DEFAULT_MAX_BLOB_BYTES_RETURNED_INLINE)
        timeout_seconds: Wall-clock budget for this call; 0/negative disables (default DEFAULT_STATEMENT_TIMEOUT_SECONDS)
        read_only: Open file databases read-only (mode=ro URI)
        foreign_keys: Enable PRAGMA foreign_keys=ON on this connection
        create_if_missing: When False, a missing database file is an error instead of being created
        bulk_insert: Bulk insert spec {"table":..., "columns":[...], "rows":[[...],...]} (sql is ignored)
        
    Returns:
        Dict containing operation results
    """
    conn = None
    memory_lock_acquired = False
    progress_handler_connection = None
    created_new_database_path = None
    call_started_at = time.perf_counter()
    try:
        # Check for dot command
        if isinstance(sql, str) and sql.strip().startswith('.'):
            parts = sql.strip()[1:].split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else None
            return handle_dot_command(command, args, database)

        # Resolve/clamp the robustness knobs
        if max_rows is None:
            max_rows = DEFAULT_MAX_ROWS_RETURNED_PER_RESULT_SET
        max_rows = max(1, int(max_rows))
        if max_blob_bytes is None:
            max_blob_bytes = DEFAULT_MAX_BLOB_BYTES_RETURNED_INLINE
        max_blob_bytes = max(0, int(max_blob_bytes))
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_STATEMENT_TIMEOUT_SECONDS

        if read_only and database == ':memory:':
            raise ValueError("read_only is not supported for the shared ':memory:' database")
        if read_only and bulk_insert is not None:
            raise ValueError("bulk_insert cannot be combined with read_only")

        if bulk_insert is None:
            if isinstance(sql, list):
                if not sql or not all(isinstance(stmt, str) and stmt.strip() for stmt in sql):
                    raise ValueError("When sql is an array it must contain one or more non-empty SQL statement strings")
                statements = [stmt.strip() for stmt in sql]
            elif isinstance(sql, str):
                # review#6: proactive statement counting via sqlite3.complete_statement -
                # no more reliance on the brittle "one statement at a time" error string
                statements = _split_sql_statements(sql)
            else:
                raise ValueError("sql must be a string or an array of statement strings")
        else:
            statements = []

        # Process any special bindings BEFORE taking the shared-connection lock:
        # embedding generation can be slow (model load) and must not serialize other callers
        if bindings:
            try:
                processed_bindings = process_bindings(bindings)
            except ValueError as binding_error:
                raise ValueError(f"Binding processing failed: {binding_error}")
        else:
            processed_bindings = None

        # review#7: debug print()s removed; bindings logged truncated only
        MCPLogger.log(TOOL_LOG_NAME, f"Executing on {database} SQL: {sql}")
        if processed_bindings:
            MCPLogger.log(TOOL_LOG_NAME, f"With bindings: {_format_bindings_for_log(processed_bindings)}")

        # review#22: make silent empty-DB creation visible (or refuse it entirely)
        if database != ':memory:':
            resolved_database_file_path = get_db_path(database)
            database_file_already_existed = os.path.exists(resolved_database_file_path)
            if not database_file_already_existed:
                if read_only:
                    raise ValueError(f"Database file does not exist: {resolved_database_file_path} (read_only=true will not create it)")
                if not create_if_missing:
                    raise ValueError(f"Database file does not exist: {resolved_database_file_path} (create_if_missing=false). Check the database name/path.")
                created_new_database_path = resolved_database_file_path

        # review#15: serialize all execution on the shared ':memory:' connection
        if database == ':memory:':
            _memory_database_execution_lock.acquire()
            memory_lock_acquired = True

        conn = get_connection(database, read_only=read_only, enable_foreign_keys=foreign_keys)

        # review#20: wall-clock budget enforced via progress handler (cleared in finally)
        if timeout_seconds and timeout_seconds > 0:
            statement_deadline = time.monotonic() + float(timeout_seconds)
            conn.set_progress_handler(lambda: 1 if time.monotonic() > statement_deadline else 0,
                                      PROGRESS_HANDLER_VDBE_OPCODE_CHECK_INTERVAL)
            progress_handler_connection = conn

        if bulk_insert is not None:
            # review#41: token-efficient bulk insert via executemany, one transaction
            result = _execute_bulk_insert(conn, bulk_insert)
        elif isinstance(sql, list):
            # review#38: atomic batch - one connection, one transaction, rollback on failure
            result = _execute_transactional_batch(conn, statements, processed_bindings, max_rows, max_blob_bytes)
        elif len(statements) > 1:
            # review#6: multi-statement string runs sequentially on ONE connection
            # (BEGIN/.../COMMIT sequences keep their atomicity); stops at first failure
            results = []
            error_messages = []
            for statement_index, statement_text in enumerate(statements):
                try:
                    results.append(_execute_single_statement(conn, statement_text, processed_bindings, max_rows, max_blob_bytes))
                except Exception as statement_error:
                    error_messages.append(f"statement {statement_index + 1} of {len(statements)}: {statement_error}")
                    results.append({
                        "operation_was_successful": False,
                        "error_message_if_operation_failed": str(statement_error),
                        "rows_modified_by_operation": None,
                        "column_names_in_result_set": None,
                        "data_rows_from_result_set": None
                    })
                    break  # do not keep executing after a failure
            result = _combine_statement_results(results, error_messages)
        else:
            single_statement_text = statements[0] if statements else sql
            result = _execute_single_statement(conn, single_statement_text, processed_bindings, max_rows, max_blob_bytes)

        # review#3: never leave a transaction open at the end of a call (previously
        # ':memory:' was never committed, leaving implicit transactions open forever on
        # the shared connection). With autocommit connections a transaction only exists
        # here after an explicit BEGIN: commit it on success, roll it back on failure.
        if bulk_insert is None and not isinstance(sql, list) and conn.in_transaction:
            if result.get("operation_was_successful"):
                conn.commit()
            else:
                conn.rollback()

        if created_new_database_path and result.get("operation_was_successful"):
            result["created_new_database_at"] = created_new_database_path  # review#22
        result["execution_time_ms"] = round((time.perf_counter() - call_started_at) * 1000, 2)  # review#25
        return result
        
    except Exception as e:
        error_msg = str(e)
        MCPLogger.log(TOOL_LOG_NAME, f"=== SQLITE ERROR ===\n{error_msg}")
        # Never leave a transaction open on the shared memory connection after a failure
        try:
            if conn is not None and conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": error_msg,
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
    finally:
        # Clear the timeout progress handler (critical on the persistent memory connection)
        if progress_handler_connection is not None:
            try:
                progress_handler_connection.set_progress_handler(None, 0)
            except Exception:
                pass
        # Always close file database connections
        if database != ':memory:' and conn:
            try:
                conn.close()
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Ignoring error during connection cleanup: {e}")
                pass
        if memory_lock_acquired:
            _memory_database_execution_lock.release()

def _execute_transactional_batch(conn, statements: List[str], processed_bindings: Optional[Dict[str, Any]],
                                 max_rows: int, max_blob_bytes: int) -> Dict[str, Any]:
    """Execute a list of statements atomically in one transaction (review item 38).

    All statements run on the caller's connection inside BEGIN IMMEDIATE ... COMMIT;
    any failure rolls back everything. Per-statement results are always returned.
    """
    results: List[Dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement_index, statement_text in enumerate(statements):
            if len(_split_sql_statements(statement_text)) != 1:
                raise ValueError(f"batch item {statement_index + 1} must be exactly one SQL statement (put each statement in its own array element)")
            results.append(_execute_single_statement(conn, statement_text, processed_bindings, max_rows, max_blob_bytes))
        conn.commit()
        combined = _combine_statement_results(results, [])
        combined["transaction_committed"] = True
        return combined
    except Exception as batch_error:
        try:
            conn.rollback()
        except Exception:
            pass
        combined = _combine_statement_results(results, []) if results else {
            "operation_was_successful": False,
            "error_message_if_operation_failed": None,
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None,
            "results": []
        }
        combined["operation_was_successful"] = False
        combined["error_message_if_operation_failed"] = (
            f"Batch failed at statement {len(results) + 1} of {len(statements)} and was rolled back: {batch_error}")
        combined["rows_modified_by_operation"] = None  # nothing survived the rollback
        combined["transaction_rolled_back"] = True
        return combined

def _execute_bulk_insert(conn, bulk_insert_specification: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a bulk row insert via executemany inside one transaction (review item 41).

    Spec: {"table": str, "columns": [str, ...], "rows": [[v1, v2, ...], ...]}
    Identifiers are double-quote escaped; values are bound as plain scalars.
    """
    table_name = bulk_insert_specification.get("table")
    column_names = bulk_insert_specification.get("columns")
    rows = bulk_insert_specification.get("rows")
    if not table_name or not isinstance(table_name, str):
        raise ValueError("bulk_insert.table must be a non-empty string")
    if not column_names or not isinstance(column_names, list) or not all(isinstance(c, str) and c for c in column_names):
        raise ValueError("bulk_insert.columns must be a non-empty list of column name strings")
    if not rows or not isinstance(rows, list):
        raise ValueError("bulk_insert.rows must be a non-empty list of row value arrays")
    for row_index, row_values in enumerate(rows):
        if not isinstance(row_values, (list, tuple)) or len(row_values) != len(column_names):
            raise ValueError(f"bulk_insert.rows[{row_index}] must be an array of {len(column_names)} values (one per column)")

    quoted_table_name = '"' + table_name.replace('"', '""') + '"'
    quoted_column_list = ", ".join('"' + column_name.replace('"', '""') + '"' for column_name in column_names)
    value_placeholders = ", ".join("?" for _ in column_names)
    insert_statement = f"INSERT INTO {quoted_table_name} ({quoted_column_list}) VALUES ({value_placeholders})"

    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        cursor.executemany(insert_statement, rows)
        conn.commit()
        return {
            "operation_was_successful": True,
            "error_message_if_operation_failed": None,
            "rows_modified_by_operation": cursor.rowcount if (cursor.rowcount is not None and cursor.rowcount >= 0) else len(rows),
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None,
            "transaction_committed": True
        }
    except Exception as bulk_error:
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": f"Bulk insert failed and was rolled back: {bulk_error}",
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None,
            "transaction_rolled_back": True
        }

def validate_parameters(input_param: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # Check for unexpected parameters
    expected_params = set(properties.keys()) | {"readme"}  # Add readme for documentation
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        warning_msg = f"Unexpected parameters ignored: {', '.join(sorted(unexpected_params))}. Expected: {', '.join(sorted(expected_params - {'readme'}))}"
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: {warning_msg}")
    
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
            # A schema type may be a single name or a list of alternatives (e.g. sql: string|array)
            allowed_type_names = expected_type if isinstance(expected_type, list) else [expected_type]
            
            # review#11b: booleans are checked FIRST (bool is an int subclass in Python,
            # so isinstance(True, int) would otherwise pass the integer/number checks)
            value_matches_a_type = False
            for type_name in allowed_type_names:
                if type_name is None:
                    value_matches_a_type = True
                elif type_name == "string" and isinstance(value, str):
                    value_matches_a_type = True
                elif type_name == "object" and isinstance(value, dict):
                    value_matches_a_type = True
                elif type_name == "array" and isinstance(value, list):
                    value_matches_a_type = True
                elif type_name == "boolean" and isinstance(value, bool):
                    value_matches_a_type = True
                elif type_name == "integer" and not isinstance(value, bool) and isinstance(value, int):
                    value_matches_a_type = True
                elif type_name == "number" and not isinstance(value, bool) and isinstance(value, (int, float)):
                    value_matches_a_type = True
                if value_matches_a_type:
                    break
            if not value_matches_a_type:
                expected_description = " or ".join(str(t) for t in allowed_type_names)
                return f"Parameter '{param_name}' must be {expected_description}, got {type(value).__name__}", {}
            
            validated[param_name] = value
        elif param_name in required:
            # This should have been caught above, but double-check
            return f"Required parameter '{param_name}' is missing", {}
        elif "default" in param_schema:
            # review#11a: honor falsy defaults (0, False, "") - test membership, not is-not-None
            validated[param_name] = param_schema["default"]
    
    # Add warning about unexpected parameters to the result
    if unexpected_params:
        validated["_validation_warning"] = f"Unexpected parameters ignored: {', '.join(sorted(unexpected_params))}"
    
    return None, validated

# Convert bytes to UTF-8 strings recursively to make JSON serializable
def convert_bytes_to_utf8(obj):
    """Recursively convert bytes objects to UTF-8 strings."""
    if isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except UnicodeDecodeError:
            # If decode fails, use base64 encoding as fallback
            return f"<base64>{base64.b64encode(obj).decode('ascii')}</base64>"
    elif isinstance(obj, dict):
        return {k: convert_bytes_to_utf8(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_bytes_to_utf8(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_bytes_to_utf8(item) for item in obj)
    else:
        return obj

def handle_sqlite(input_param: Dict[str, Any]) -> Dict:
    """Handle SQLite operation request via MCP interface.
    
    Args:
        input_param: Dictionary containing input with sql, database (optional), and bindings (optional)
        
    Returns:
        Dict containing either the operation results or error information
    """
    try:
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {} # Pop off synthetic handler_info parameter early (before validation); This is added by the server for tools that need dynamic routing
        
        # Extract authenticated username from handler_info
        authenticated_user = get_authenticated_user(handler_info)
        if authenticated_user:
            MCPLogger.log(TOOL_LOG_NAME, f"Tool called by authenticated user: {authenticated_user}")
        
        if isinstance(input_param, dict) and "input" in input_param: # collapse the single-input placeholder which exists only to save context (because we must bypass pipeline parameter validation to *save* the context)
            input_param = input_param["input"]
        
        # Check for readme operation first
        # Accept both {"operation": "readme"} (the form every other tool documents) and the
        # legacy {"readme": true} form, so callers moving between tools never get it wrong.
        if isinstance(input_param, dict) and (input_param.get("operation") == "readme" or input_param.get("readme") is True):
            MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
            # review#12: return real_parameters - the schema the AI actually needs
            return {
                "content": [{"type": "text", "text": json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2)}],
                "isError": False
            }

        # For all other operations, validate the token first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.")
            
        provided_token = input_param.get("tool_unlock_token")
        # Accept either direct token or inter-tool token format: -<caller_token>-<our_token>
        token_valid = False
        if provided_token == TOOL_UNLOCK_TOKEN:
            token_valid = True
        elif isinstance(provided_token, str) and provided_token.startswith("-") and provided_token.endswith(f"-{TOOL_UNLOCK_TOKEN}"):
            # Inter-tool token format: -<caller_token>-<our_token>
            MCPLogger.log(TOOL_LOG_NAME, "Accepted inter-tool token from calling tool")
            token_valid = True
        
        if not token_valid:
            return create_error_response("Invalid or missing tool_unlock_token. Please read the documentation first using {\"input\":{\"operation\":\"readme\"}}")

        # Fix common parameter naming mistakes before validation
        if "database_file" in input_param and "database" not in input_param:
            MCPLogger.log(TOOL_LOG_NAME, "Auto-correcting 'database_file' parameter to 'database'")
            input_param = input_param.copy()  # Don't modify the original
            input_param["database"] = input_param.pop("database_file")

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=False)

        # Extract validated parameters
        sql = validated_params.get("sql")
        database = validated_params.get("database", ":memory:")
        bindings = validated_params.get("bindings")
        bulk_insert = validated_params.get("bulk_insert")
        validation_warning = validated_params.get("_validation_warning")

        if not sql and bulk_insert is None:
            return create_error_response("No SQL command provided", with_readme=False)
        
        # review#4 (amended after live verification): the authenticated_user binding is
        # ALWAYS server-controlled - a caller cannot spoof someone else's name into the
        # audit trail. This block previously ran only for authenticated callers, which
        # (a) made SQL referencing :authenticated_user fail with a bindings error for
        # anonymous callers even though the readme promises the binding is always set,
        # and (b) let an anonymous caller smuggle in their own authenticated_user value
        # unchallenged. The binding is now injected unconditionally: SQL NULL (Python
        # None) when no authenticated identity exists. Extra dict keys are ignored by
        # sqlite3 when a statement does not reference them, so statements that never
        # mention :authenticated_user are unaffected.
        # review#14: work on a copy so the caller's bindings dict is never mutated.
        bindings = dict(bindings) if bindings else {}
        caller_supplied_authenticated_user = bindings.get('authenticated_user')
        if caller_supplied_authenticated_user is not None and caller_supplied_authenticated_user != authenticated_user:
            MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: caller-supplied authenticated_user binding ({caller_supplied_authenticated_user!r}) overwritten with the server-determined value ({authenticated_user!r}){NORM}")
        bindings['authenticated_user'] = authenticated_user
            
        # Execute SQL
        result = sqlite(
            sql,
            database,
            bindings,
            max_rows=validated_params.get("max_rows"),
            max_blob_bytes=validated_params.get("max_blob_bytes"),
            timeout_seconds=validated_params.get("timeout_seconds"),
            read_only=bool(validated_params.get("read_only", False)),
            foreign_keys=bool(validated_params.get("foreign_keys", False)),
            create_if_missing=bool(validated_params.get("create_if_missing", True)),
            bulk_insert=bulk_insert
        )

        # Apply bytes conversion to the result
        result = convert_bytes_to_utf8(result)
        
        # Add validation warning to result if there were unexpected parameters
        if validation_warning and result["operation_was_successful"]:
            result["warning"] = validation_warning
        
        # Convert result to JSON string
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": not result["operation_was_successful"]
        }
            
    except Exception as e:
        error_msg = f"Error executing SQL: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return create_error_response(error_msg, with_readme=False)

# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_sqlite
}
