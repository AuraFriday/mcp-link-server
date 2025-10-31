"""
File: ragtag/tools/sqlite.py
Project: Aura Friday MCP-Link Server
Component: SQLite Database Tool
Author: Christopher Nathan Drake (cnd)

NOTICE: This is a HELPER TOOL.  This tool is NOT the "RagTag" memory system itself.  This tool exists to help us with development of RagTag.

RagTag Memory System - Direct SQLite Tool

test: python3 /home/cnd/Downloads/cursor/ragtag/python/ragtag/src/ragtag/ragtag_cli.py sqlite --json '{"sql": "SELECT COUNT(*) FROM SEO_Actions", "database": "../seo/seo.db", "tool_unlock_token": "aa9f3e5b"}'
note: above will give you a new tool_unlock_token which you will need to re-run that above test command with
Tool implementation for direct SQLite database operations.

Install:
    pip install pysqlite3-binary

Issues to consider one day:

# Response 1

Let me review the key issues we identified earlier from our research and testing:

1. **Thread Safety Issues**:
   - `:memory:` database connection isn't thread-safe
   - SQLite connections must be used in same thread they were created in
   - Currently blocking our shared memory database functionality

2. **Concurrency Limitations**:
   - Only one writer at a time (SQLite limitation)
   - Need proper retry logic for SQLITE_BUSY errors
   - No timeout settings configured yet
   - No busy handlers implemented

3. **WAL Mode Benefits**:
   - Would allow reads during writes
   - Better concurrency overall
   - Not currently enabled for file databases
   - Could be enabled by default for better performance

4. **Connection Management**:
   - File connections are correctly per-operation now
   - But no timeout configuration
   - No connection pooling if needed
   - No max connections limit

5. **Error Handling Improvements Needed**:
   - More specific error types
   - Better retry logic
   - Transaction rollback in error cases
   - Better error messages for specific SQLite errors

6. **File Location Safety**:
   - ✓ Now supports environment variables and home directory expansion
   - No checks for write permissions
   - No directory creation if needed
   - No path validation

7. **Missing Features**:
   - No transaction support in API
   - No batch operations
   - No query parameter validation
   - No result size limits

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "𝛢QƴBƻꓠ𝟣s0ƏʋᗪᛕƻɪⲦЕՕwΗg𝙰ᒿꓰⅮƧīꞇб𝛢ĐոᗞνᎬƵCҳ𝟨fzƋƊꓚ𝟪Ɛ𝛢QбԁFᴛģQꓗΗАƛrµᴅҳVΑⲞA×ꓗո7ΟНƨⲦꓝȣDⅼһх7Т3ϹꜱРοꓗꓓƼƿ9uaʋᏴȠcȢUᴅ𝟢lƨɊ𝟑ᗅvꓧ"
"signdate": "2025-09-24T01:56:01.677Z",
"""

import os
import sys
import json
import threading
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
from ragtag.shared_config import get_user_data_directory
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

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "sqlite",
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
     "input": {"readme": true}
   }

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
- Useful for user-specific data access and audit trails

## Basic Database Operations:
- Database: Use ':memory:' for temporary storage (persists until server restart, shared between AI instances)
- Or use filename for persistent database with these path options:
  * Simple filename (e.g. 'data.db') -> stored in same directory as run_ragtag_sse.py
    (typically python/ragtag/run_ragtag_sse.py in the project root)
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
- 'db.sqlite' -> Uses @user_data/ by default

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

  1. Reference Another Binding; ALWAYS do this if when the text of the embedding is also stored in the database:
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

  Similarity Search Examples:
  ```python
  # Basic similarity search
  execute_sql(
      \"\"\"SELECT text, vec_distance_cosine(embedding, vec_f32(:query_vec)) as distance
         FROM docs
         WHERE vec_distance_cosine(embedding, vec_f32(:query_vec)) < 0.5  -- Range: 0-1, lower is more similar
         ORDER BY distance LIMIT 5\"\"\"",
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
         ORDER BY distance LIMIT 5\"\"\"",
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

## Return Format:
- operation_was_successful: boolean
- error_message_if_operation_failed: string or null
- rows_modified_by_operation: integer or null for SELECT
- column_names_in_result_set: array or null for non-SELECT
- data_rows_from_result_set: array of row objects or null for non-SELECT

## Features:
- Automatic connection caching per database
- Row results as dictionaries with column name access
- Auto-commit for INSERT/UPDATE/DELETE
- Full SQLite feature set available
- Built-in vector similarity search
- Automatic Gemini embedding generation

## Important Limitations:
- Thread Safety: SQLite connections are thread-local
- Concurrent Access: Only one writer at a time
- Memory DB Scope: :memory: database persists for server lifetime
- File Location: Database files with simple names (no path separators) are created
  in the same directory as run_ragtag_sse.py (typically python/ragtag/run_ragtag_sse.py
  in the project root). Use full paths, @appdata/, or ~/ to store elsewhere.
- Embedding Generation: Uses local model (auto-downloads on first use)
- Vector Operations: Always use vec_f32() in SQL for vector parameters

## Common Error Cases:
- 'CHECK constraint failed': Missing vec_f32() in SQL for vector operations
- 'Referenced column not found': Check binding names match SQL parameters
- 'Failed to generate embedding': Model loading or dependency issues (auto-resolved on retry)

## SQLite Dot Commands:
The following dot commands are supported for convenience:
  .databases  - List all .db files in user data directory with size and modification date
  .tables     - List all tables
  .schema     - Show schema for table(s)
  .indexes    - Show indexes for table
  .fullschema - Complete schema dump
  .dbinfo     - Show database information
  .status     - Show current settings
  .pragma     - Show all PRAGMA settings
  .foreign_keys - Show foreign key settings

Note: While dot commands are supported for convenience, standard SQL
queries are preferred as they provide more explicit and complete functionality.

## PRAGMA Support:
All PRAGMA statements are supported using standard SQL syntax:
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
```
""",
        # Standard MCP parameters - simplified to single input dict
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"readme\":true}} to get full documentation, parameters, and an unlock token."
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
                    "type": "string",
                    "description": "The SQL command to execute with optional :param style placeholders"
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
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme documentation"
                }
            },
            "required": ["sql", "tool_unlock_token"],
            "title": "sqliteArguments",
            "type": "object"
        }
    }
]


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log("SQLITE", f"Error: {error_msg}")
    docs = "\n\n" + json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["parameters"] }, indent=2) if with_readme else ""
    return { "content": [{"type": "text", "text": f"{error_msg}{docs}"}], "isError": True }



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
            embedding_result, error = generate_embedding(text)
            if embedding_result is None:
                MCPLogger.log(TOOL_LOG_NAME, f"Embedding generation failed: {error}")
                return None
            
            # Return as JSON string that vec_f32() can parse
            return json.dumps(embedding_result)
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error in embedding UDF: {e}")
            return None
    
    # Register the function with SQLite
    conn.create_function("generate_embedding", 1, generate_embedding_udf)
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
        MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: Failed to get extension path: {e}{NORM}")
        extension_path = '/home/inmate/jail/usr/local/lib/python3.11/site-packages/sqlite_vec/'


    try:
        conn.enable_load_extension(True)
        # Get path directly from the installed package
        conn.load_extension(extension_path)
        conn.enable_load_extension(False)  # Disable after loading for security
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully loaded sqlite-vec extension from: {extension_path}")
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: Failed to load sqlite-vec '{extension_path}' extension: {e}{NORM}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        try:
            #import pysqlite3.dbapi2 as sqlite3
            #sys.modules['sqlite3'] = sqlite3
            conn.enable_load_extension(True)
            #import sqlite_vec
            extension_path = os.path.join(os.path.dirname(sqlite_vec.__file__), 'vec0') # was  .so or .dll here - skip the /inmate/ stuff maybe
            MCPLogger.log(TOOL_LOG_NAME, f"Attempting to load sqlite-vec extension from: {extension_path}")
            conn.load_extension(extension_path)            
            conn.enable_load_extension(False)
            MCPLogger.log(TOOL_LOG_NAME, f"Successfully loaded sqlite-vec extension from: {extension_path}")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"{YEL}Warning: RETRY Failed to load sqlite-vec extension: {e}{NORM}")
            MCPLogger.log(TOOL_LOG_NAME, f"Full retry stack trace: {traceback.format_exc()}")


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
                    cls._connection = sqlite3.connect(':memory:', check_same_thread=False)
                    cls._connection.row_factory = sqlite3.Row
                    cls._connection.execute("PRAGMA journal_mode=WAL")
                    load_sqlite_vec(cls._connection)  # Load extension
                    register_embedding_functions(cls._connection)  # Register UDFs
                    cls._initialized = True
                    MCPLogger.log(TOOL_LOG_NAME, "Memory database initialized successfully")
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize memory database: {e}")
                    cls._connection = None
                    raise

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Get the memory database connection."""
        if not cls._initialized or not cls._connection:
            raise RuntimeError("Memory database not initialized or connection lost")
        return cls._connection

def initialize_tool() -> None:
    """Initialize the SQLite tool - called once when server starts."""
    SQLiteMemoryDB.initialize()

# Map of supported dot commands to their SQL equivalents
DOT_COMMANDS = {
    'databases': None,  # Special handling - lists .db files in user data directory
    'tables': "SELECT name FROM sqlite_master WHERE type='table'",
    'schema': lambda tbl: f"SELECT sql FROM sqlite_master WHERE type='table'" + (f" AND tbl_name = '{tbl}'" if tbl else ""),
    'indexes': lambda tbl: f"SELECT name FROM sqlite_master WHERE type='index'" + (f" AND tbl_name = '{tbl}'" if tbl else ""),
    'fullschema': "SELECT sql FROM sqlite_master ORDER BY tbl_name, type DESC, name",
    'dbinfo': "SELECT 'Database Size' as name, page_count * page_size as value FROM pragma_page_count, pragma_page_size UNION ALL SELECT 'Foreign Keys', foreign_keys FROM pragma_foreign_keys UNION ALL SELECT 'Journal Mode', journal_mode FROM pragma_journal_mode",
    'foreign_keys': "PRAGMA foreign_key_list",
    'pragma': "SELECT * FROM pragma_function_list",
    'status': """
        SELECT 
            'Database Size' as setting,
            page_count * page_size as value 
        FROM pragma_page_count, pragma_page_size
        UNION ALL
        SELECT 'Foreign Keys', foreign_keys FROM pragma_foreign_keys
        UNION ALL
        SELECT 'Journal Mode', journal_mode FROM pragma_journal_mode
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
    'backup': "Use SQL BACKUP or handle database files at the application level",
    'restore': "Handle database restoration at the application level",
    'dump': "Use .schema or .fullschema for structure, or SQL queries for data export",
    'import': "Use SQL INSERT statements for data import",
    'save': "Database files are managed automatically. Use SQL for data operations",
    'read': "Use SQL directly instead of reading from files",
    'shell': "This is not a shell CLI. Use appropriate API calls for system operations"
}

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
            import os
            from datetime import datetime
            
            # Get user data directory
            user_data_path = get_user_data_directory()
            
            # Find all .db files
            db_files = []
            if user_data_path.exists():
                for db_file in user_data_path.glob("*.db"):
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
            
            return {
                "operation_was_successful": True,
                "error_message_if_operation_failed": None,
                "rows_modified_by_operation": None,
                "column_names_in_result_set": ["filename", "size_bytes", "last_modified", "full_path"],
                "data_rows_from_result_set": db_files
            }
            
        except Exception as e:
            return {
                "operation_was_successful": False,
                "error_message_if_operation_failed": f"Error listing databases: {str(e)}",
                "rows_modified_by_operation": None,
                "column_names_in_result_set": None,
                "data_rows_from_result_set": None
            }
    
    # Get the SQL for this command
    sql_cmd = DOT_COMMANDS[command]
    if callable(sql_cmd):
        sql_cmd = sql_cmd(args)
    
    # Execute the command using our standard SQL execution
    return sqlite(sql_cmd, database)

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
        PermissionError: If target directory is not writable
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
            
            return os.path.abspath(os.path.join(base, filename))
            
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
                
            return expanded
            
        # For simple filenames, store in user data dir from shared config
        base_path = get_user_data_directory()
        base = str(base_path)  # Convert Path to string for compatibility
        # get_user_data_directory() already ensures directory exists
        if not os.access(base, os.W_OK):
            raise PermissionError(f"Default data directory not writable: {base}")
        return os.path.join(base, database)
        
    except Exception as e:
        if isinstance(e, (ValueError, PermissionError)):
            raise
        raise ValueError(f"Failed to expand database path '{database}': {str(e)}")

def get_connection(database: str) -> sqlite3.Connection:
    """Get database connection - memory shared, files temporary.
    
    Args:
        database: Database name or ':memory:'
        
    Returns:
        sqlite3.Connection: Database connection
    """
    if database == ':memory:':
        return SQLiteMemoryDB.get_connection()
    
    # For files: Create new connection each time
    db_path = get_db_path(database)
    MCPLogger.log(TOOL_LOG_NAME, f"Creating new file connection: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    load_sqlite_vec(conn)  # Load extension
    register_embedding_functions(conn)  # Register UDFs
    return conn

def process_embedding_binding(value: Dict[str, Any], bindings: Dict[str, Any]) -> str:
    """Process a special embedding binding value.
    
    Handles two formats:
    1. {"_embedding_text": "text to embed"}  - Directly embeds the given text
    2. {"_embedding_col": "column_name"}     - Embeds text from another binding
    
    Args:
        value: The special binding dictionary
        bindings: Complete bindings dictionary for column reference lookup
        
    Returns:
        str: JSON array string containing the embedding vector
        
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
        
    # Generate embedding and properly unpack the tuple
    embedding_result, error = generate_embedding(text)
    if embedding_result is None:
        raise ValueError(f"Failed to generate embedding: {error}")
        
    # Return just the JSON array string of the actual embedding data
    return json.dumps(embedding_result)

def process_bindings(bindings: Optional[Dict[str, Any]]) -> Union[Optional[Dict[str, Any]], Tuple[None, str]]:
    """Process bindings dictionary, handling special embedding values.
    
    Args:
        bindings: Original bindings dictionary or None
        
    Returns:
        Optional[Dict[str, Any]]: Processed bindings with embeddings converted to SQLite format
        Tuple[None, str]: (None, error_message) if processing fails
    """
    if not bindings:
        return None
        
    # Make a copy to avoid modifying the original
    processed = bindings.copy()
    
    # First pass: Process direct text embeddings
    for key, value in processed.items():
        if isinstance(value, dict) and "_embedding_text" in value:
            try:
                processed[key] = process_embedding_binding(value, bindings)
            except ValueError as e:
                return None, str(e)
            
    # Second pass: Process column reference embeddings
    for key, value in processed.items():
        if isinstance(value, dict) and "_embedding_col" in value:
            try:
                processed[key] = process_embedding_binding(value, bindings)
            except ValueError as e:
                return None, str(e)
            
    return processed

def convert_pragma_to_select(sql: str) -> str:
    """Convert PRAGMA commands to equivalent SELECT FROM pragma_* queries.
    
    Args:
        sql: Original SQL command
        
    Returns:
        str: Converted SQL if it was a PRAGMA command, original SQL otherwise
    """
    sql = sql.strip()
    if not sql.upper().startswith('PRAGMA '):
        return sql
        
    # Remove any trailing semicolon
    if sql.endswith(';'):
        sql = sql[:-1]
        
    # Extract the PRAGMA command and args
    # Format: PRAGMA name[(args)]
    parts = sql[7:].strip().split('(', 1)
    pragma_name = parts[0].strip()
    
    if len(parts) > 1:
        # Has arguments
        args = parts[1].rstrip(')')
        return f"SELECT * FROM pragma_{pragma_name}('{args}')"
    else:
        # No arguments
        return f"SELECT * FROM pragma_{pragma_name}"

def sqlite(
    sql: str,
    database: str = ':memory:',
    bindings: Optional[Dict[str, Any]] = None
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
       .databases  - List all .db files in user data directory with size and modification date
       .tables     - List all tables
       .schema     - Show schema for table(s)
       .indexes    - Show indexes for table
       .fullschema - Complete schema dump
       .dbinfo     - Show database information
       .status     - Show current settings
       .pragma     - Show all PRAGMA settings
       .foreign_keys - Show foreign key settings
       
       Note: While dot commands are supported for convenience, standard SQL queries
       are preferred as they are more explicit and provide full functionality.
    
    Args:
        sql: SQL command to execute or dot command (e.g. ".tables")
        database: Database name or ':memory:'
        bindings: Optional parameter bindings with special embedding support
        
    Returns:
        Dict containing operation results
    """
    conn = None
    try:
        # Check for dot command
        if sql.strip().startswith('.'):
            parts = sql.strip()[1:].split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else None
            return handle_dot_command(command, args, database)

        # Convert PRAGMA commands to SELECT
        sql = convert_pragma_to_select(sql)

        conn = get_connection(database)
        cursor = conn.cursor()
        
        # Process any special bindings
        if bindings:
            processed_or_error = process_bindings(bindings)
            if isinstance(processed_or_error, tuple):
                _, error_msg = processed_or_error
                raise ValueError(f"Binding processing failed: {error_msg}")
            processed_bindings = processed_or_error
        else:
            processed_bindings = None

        print("BINDINGS:")
        print(json.dumps(processed_bindings, indent=2))
        
        # Log the SQL and bindings for debugging
        MCPLogger.log(TOOL_LOG_NAME, f"Executing on {database} SQL: {sql}")
        if processed_bindings:
            MCPLogger.log(TOOL_LOG_NAME, f"With bindings: {json.dumps(processed_bindings)}")
        
        # Execute with processed bindings if provided
        try:
            if processed_bindings:
                cursor.execute(sql, processed_bindings)
            else:
                cursor.execute(sql)
        except sqlite3.Error as sqle:
            # Get the specific SQLite error message
            sqlite_error = str(sqle)
            
            # Check for the specific multi-statement error
            if "You can only execute one statement at a time" in sqlite_error:
                MCPLogger.log(TOOL_LOG_NAME, "Multi-statement SQL detected, executing sequentially")
                
                # Close current connection if it's a file database
                if database != ':memory:' and conn:
                    try:
                        conn.close()
                    except Exception as e:
                        MCPLogger.log(TOOL_LOG_NAME, f"Ignoring error during connection cleanup: {e}")
                        pass
                    conn = None
                
                # Split on semicolons, but only if they're not inside quotes
                statements = []
                current = []
                in_quotes = False
                quote_char = None
                
                for char in sql:
                    if char in ["'", "\""]:
                        if not in_quotes:
                            in_quotes = True
                            quote_char = char
                        elif quote_char == char:
                            in_quotes = False
                            quote_char = None
                    elif char == ";" and not in_quotes:
                        if current:
                            statements.append("".join(current).strip())
                            current = []
                        continue
                    current.append(char)
                
                if current:
                    statements.append("".join(current).strip())
                
                # Execute each statement
                results = []
                for stmt in statements:
                    if stmt:  # Skip empty statements
                        stmt_result = sqlite(stmt, database, processed_bindings)
                        results.append(stmt_result)
                
                # Combine results
                combined = {
                    "operation_was_successful": all(r["operation_was_successful"] for r in results),
                    "error_message_if_operation_failed": None,
                    "rows_modified_by_operation": sum((r["rows_modified_by_operation"] or 0) for r in results),
                    "results": results  # Keep individual results for inspection if needed
                }
                return combined
                
            if "JSON" in sqlite_error:
                # For JSON parsing errors, include the problematic data
                error_context = {
                    "sql": sql,
                    "original_bindings": bindings,
                    "processed_bindings": processed_bindings,
                    "sqlite_error": sqlite_error
                }
                raise ValueError(f"SQLite JSON parsing error - Details:\n{json.dumps(error_context)}")
            raise  # Re-raise other SQLite errors
            
        # Determine operation type from first word
        operation = sql.strip().split()[0].upper()
        
        result = {
            "operation_was_successful": True,
            "error_message_if_operation_failed": None,
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
        
        if operation == 'SELECT':
            # Get column names
            column_names = [description[0] for description in cursor.description]
            result["column_names_in_result_set"] = column_names
            
            # Get rows as dictionaries
            rows = []
            for row in cursor.fetchall():
                rows.append(dict(zip(column_names, row)))
            result["data_rows_from_result_set"] = rows
            
        else:
            # For INSERT/UPDATE/DELETE, get row count
            result["rows_modified_by_operation"] = cursor.rowcount
            if database != ':memory:':
                conn.commit()
        
        return result
        
    except Exception as e:
        error_msg = str(e)
        MCPLogger.log(TOOL_LOG_NAME, f"=== SQLITE ERROR ===\n{error_msg}")
        return {
            "operation_was_successful": False,
            "error_message_if_operation_failed": error_msg,
            "rows_modified_by_operation": None,
            "column_names_in_result_set": None,
            "data_rows_from_result_set": None
        }
    finally:
        # Always close file database connections
        if database != ':memory:' and conn:
            try:
                conn.close()
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Ignoring error during connection cleanup: {e}")
                pass

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
            
            # Type validation
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}", {}
            elif expected_type == "object" and not isinstance(value, dict):
                return f"Parameter '{param_name}' must be an object/dictionary, got {type(value).__name__}", {}
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}", {}
            
            validated[param_name] = value
        elif param_name in required:
            # This should have been caught above, but double-check
            return f"Required parameter '{param_name}' is missing", {}
        else:
            # Use default value if specified
            default_value = param_schema.get("default")
            if default_value is not None:
                validated[param_name] = default_value
    
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
        if isinstance(input_param, dict) and input_param.get("readme") is True:
            MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
            return {
                "content": [{"type": "text", "text": json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["parameters"]}, indent=2)}],
                "isError": False
            }

        # For all other operations, validate the token first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.")
            
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token. Please read the documentation first using {\"input\":{\"readme\":true}}")

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
        validation_warning = validated_params.get("_validation_warning")

        if not sql:
            return create_error_response("No SQL command provided", with_readme=False)
        
        if True:
            # Add authenticated user info to bindings if not already present
            if authenticated_user and bindings is not None:
                if 'authenticated_user' not in bindings:
                    bindings['authenticated_user'] = authenticated_user
                    MCPLogger.log(TOOL_LOG_NAME, f"Added authenticated_user binding: {authenticated_user}")
            elif authenticated_user and bindings is None:
                bindings = {'authenticated_user': authenticated_user}
                MCPLogger.log(TOOL_LOG_NAME, f"Created bindings with authenticated_user: {authenticated_user}")
            
        # Execute SQL
        result = sqlite(sql, database, bindings)

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
    "sqlite": handle_sqlite
}
