"""
File: ragtag/tools/python.py
Project: Aura Friday MCP-Link Server
Component: Python Execution Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for executing Python code locally with full MCP tool integration.
Allows AI agents to run Python scripts, save/load code files, and use Python as "glue" 
between other MCP tools for data processing and automation tasks.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "T𝙰tƏ0SƐƧɯՕɊ𝙰ƙ𝙰1p𝟩ꓪКnɯkGΑƼ𝐴GⲢ𝙰Аȣ×ꓝƲk1ƍƊᴛⅮ×ԝҳiJJᏟnυɋƛµᴡµҮƼʌАƿFуՕɯϨ6𝟤ꜱӠzԝ2уeցսɌꓑZȷȢĐƿᗷȣᴅMꓣƨҳᎻҳ0ƳMwßոƽdwᎻՕЗiԝG𝟟ҳƧ"
"signdate": "2026-07-23T02:39:40.754Z",
"""

import ast          # trailing-expression result capture (item 23)
import builtins     # explicit, deterministic __builtins__ for exec (item 16)
import json
import os
import sys
import time         # per-session created/last-used timestamps (items 17, 25)
import traceback
import threading
from pathlib import Path
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory
# Dropped unused typing imports (List, Union, BinaryIO) - minor cleanup from review
from typing import Dict, Optional, Tuple

# Import mcp_bridge to provide it with HANDLERS registry access
from . import mcp_bridge

# Constants
TOOL_LOG_NAME = "PYTHON"

# Default cap on returned stdout/stderr for execute results, overridable per call via
# the max_output input parameter (a runaway print loop must not blow the caller's context)
DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR = 65536

# Sentinel distinguishing "schema has no default" from "schema default is literal None",
# so validate_parameters can honor a legitimate None default instead of silently dropping
# it (item 11). Only this object means "absent".
_SCHEMA_DEFAULT_ABSENT_SENTINEL = object()

# Upper bound on retained persistent sessions. Past this, the least-recently-used session
# is evicted so an agent inventing session_ids cannot grow the cache without bound (item 17).
MAX_RETAINED_PERSISTENT_SESSIONS = 128

# exec_globals keys this tool injects itself; excluded when counting user-created variables
# so clear_session / list_sessions report user variables, not our scaffolding (items 17, 25).
_BASELINE_EXEC_GLOBALS_KEYS_NOT_COUNTED_AS_USER_VARIABLES = frozenset(
    {"__builtins__", "mcp", "__name__", "__file__"})

# Serializes only executions that mutate process-global state (sys.argv, cwd, os.environ)
# for run_script argv and per-call cwd/env overrides (items 22, 27). Executions that do not
# request those overrides never acquire this lock, so ordinary runs stay concurrent.
# RLock (not Lock): user code inside an override run can itself call the python tool with
# overrides on the same thread; a plain Lock would self-deadlock there.
_process_global_state_mutation_lock_for_argv_cwd_env_overrides = threading.RLock()

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"python{TOOL_NAME_SUFFIX}"

# Persistent session management
_session_globals_cache_for_persistent_execution_contexts = {}  # session_id -> exec_globals dict
_session_cache_thread_safety_lock = threading.Lock()
# Per-session execution locks: calls sharing a session_id serialize while different
# sessions run concurrently. RLock so same-thread re-entrancy (user code invoking the
# python tool again) cannot self-deadlock. Guarded by _session_cache_thread_safety_lock.
_session_id_to_execution_serialization_rlock_map = {}  # session_id -> threading.RLock
# Per-session created / last-used epoch timestamps, for LRU eviction (item 17) and the
# list_sessions operation (item 25). Guarded by _session_cache_thread_safety_lock.
_session_id_to_created_and_last_used_epoch_times_map = {}  # session_id -> {"created": float, "last_used": float}


class Thread_Aware_Standard_Stream_Proxy_Routing_Writes_To_Per_Thread_Capture_Buffers:
    """sys.stdout/sys.stderr replacement that makes output capture thread-safe.

    contextlib.redirect_stdout swaps the process-global sys.stdout, so concurrent
    executions cross-captured each other's output. This proxy routes each write to
    the calling thread's registered capture buffer (threading.local), falling back
    to the real underlying stream for threads with no registered buffer.
    """

    def __init__(self, real_underlying_stream_for_fallback):
        self._real_underlying_stream_for_fallback = real_underlying_stream_for_fallback
        self._per_thread_active_capture_buffer_storage = threading.local()

    def _current_write_target_stream(self):
        active_capture_buffer = getattr(self._per_thread_active_capture_buffer_storage, 'active_capture_buffer', None)
        return active_capture_buffer if active_capture_buffer is not None else self._real_underlying_stream_for_fallback

    def activate_capture_buffer_for_current_thread(self, capture_buffer):
        """Route this thread's writes to capture_buffer; returns the previously active buffer (for nested executions)."""
        previously_active_capture_buffer = getattr(self._per_thread_active_capture_buffer_storage, 'active_capture_buffer', None)
        self._per_thread_active_capture_buffer_storage.active_capture_buffer = capture_buffer
        return previously_active_capture_buffer

    def restore_previous_capture_buffer_for_current_thread(self, previously_active_capture_buffer):
        self._per_thread_active_capture_buffer_storage.active_capture_buffer = previously_active_capture_buffer

    def write(self, text_to_write):
        return self._current_write_target_stream().write(text_to_write)

    def writelines(self, lines_to_write):
        return self._current_write_target_stream().writelines(lines_to_write)

    def flush(self):
        return self._current_write_target_stream().flush()

    def __getattr__(self, attribute_name):
        # Delegate everything else (encoding, isatty, fileno, buffer, ...) to the real stream
        return getattr(self._real_underlying_stream_for_fallback, attribute_name)


_thread_aware_stream_proxy_installation_lock = threading.Lock()
_installed_thread_aware_stdout_proxy = None
_installed_thread_aware_stderr_proxy = None


def _install_thread_aware_stream_proxies_over_sys_stdout_and_stderr_once():
    """Idempotently replace sys.stdout/sys.stderr with the thread-aware proxies.

    Installed lazily on first execute (not at import) so we wrap whatever streams
    friday.py finished setting up. Returns (stdout_proxy, stderr_proxy).
    """
    global _installed_thread_aware_stdout_proxy, _installed_thread_aware_stderr_proxy
    with _thread_aware_stream_proxy_installation_lock:
        if _installed_thread_aware_stdout_proxy is None:
            _installed_thread_aware_stdout_proxy = Thread_Aware_Standard_Stream_Proxy_Routing_Writes_To_Per_Thread_Capture_Buffers(sys.stdout)
            sys.stdout = _installed_thread_aware_stdout_proxy
        if _installed_thread_aware_stderr_proxy is None:
            _installed_thread_aware_stderr_proxy = Thread_Aware_Standard_Stream_Proxy_Routing_Writes_To_Per_Thread_Capture_Buffers(sys.stderr)
            sys.stderr = _installed_thread_aware_stderr_proxy
    return _installed_thread_aware_stdout_proxy, _installed_thread_aware_stderr_proxy

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        "description": """Execute Python code locally with full MCP tool integration.
- Use this tool to run Python scripts, process data between tools, save/load code files
- Python code can directly call other MCP tools (sqlite, chrome_browser, user, etc.) via injected mcp module
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
                    "enum": ["readme", "execute", "run_script", "save_script", "load_script", "list_scripts", "delete_script", "clear_session", "list_sessions", "list_packages", "pip_install"],
                    "description": "Operation to perform"
                },
                "code": {
                    "type": "string",
                    "description": "Python code to execute (for execute operation)"
                },
                "filename": {
                    "type": "string",
                    "description": "Script filename for save/load/delete/run_script operations. Only the base name is used and '.py' is appended if missing; stored in the user data directory."
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional session identifier for persistent execution context",
                    "default": "default"
                },
                "persistent": {
                    "type": "boolean",
                    "description": "Whether to maintain session state between executions",
                    "default": True
                },
                "run_on_main_thread": {
                    "type": "boolean",
                    "description": "Whether to execute on main thread (required for COM objects to persist across calls)",
                    "default": False
                },
                "max_output": {
                    "type": "integer",
                    "description": "Maximum bytes of stdout/stderr returned by execute (default 65536); longer output is truncated keeping head and tail with a '[N bytes truncated]' marker",
                    "default": DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional wall-clock timeout in seconds for execute/run_script. On a worker thread the code runs in a daemon thread that is abandoned if it overruns (the run keeps going in the background). On the main thread it bounds the wait (default 300s). For pip_install it bounds the pip subprocess (default 300s)."
                },
                "argv": {
                    "type": "array",
                    "description": "Optional list of strings assigned to sys.argv for the run. For run_script, sys.argv[0] is the script path and these follow. Using this serializes concurrent runs (sys.argv is process-global)."
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory to switch to for the run (restored afterward). Using this serializes concurrent runs (cwd is process-global)."
                },
                "env": {
                    "type": "object",
                    "description": "Optional dict of environment variables merged into os.environ for the run (restored afterward). Using this serializes concurrent runs (os.environ is process-global)."
                },
                "packages": {
                    "type": "array",
                    "description": "For pip_install: list of package specifiers (e.g. [\"requests\", \"numpy==1.26.4\"]) to install into the server interpreter."
                },
                "filter": {
                    "type": "string",
                    "description": "For list_packages: optional case-insensitive substring to narrow the returned package list."
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """
Execute Python code locally with full MCP tool integration.

This tool allows AI agents to run Python scripts locally on the user's machine with access to 
all other MCP tools. Python code can directly call sqlite, the user's browser (named per running
browser: chrome_browser, edge_browser, ...), user interface, and other tools via an injected
'mcp' module. Perfect for data processing, automation, and serving as 
"glue" between different tools when data is too large for direct AI handling.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Operations Available

### 1. execute - Run Python code
Execute Python code in a local environment with MCP tool access. If the last top-level
statement is a plain expression, its repr() is returned as "result" (REPL-style), so you
do not have to wrap a final value in print(). Optional per-call controls: timeout, cwd,
env, argv (see Parameters).

### 2. run_script - Run a saved script by name
Execute a previously saved script by filename without loading its text into your context
first. sys.argv[0] is set to the script path, __file__ is set, and any argv list you pass
follows. Accepts the same session_id/persistent/timeout/cwd/env controls as execute.

### 3. save_script - Save code to file
Save Python code to a named file in the user data directory for later use. The name is
reduced to its base name and '.py' is appended if missing (so it always appears in
list_scripts). Written atomically.

### 4. load_script - Load saved code
Retrieve previously saved Python code from a file.

### 5. list_scripts - List saved files
Show all saved Python script files (with ISO-8601 modified time and raw epoch).

### 6. delete_script - Remove saved file
Delete a saved Python script file.

### 7. clear_session - Clear persistent session
Clear a persistent session's cached variables and state. Use this to free memory or start
fresh with the same session_id. When the session is not found, the reply lists the
currently active session_ids so you can spot a typo.

### 8. list_sessions - List persistent sessions
List the active persistent sessions with their user-variable counts and created/last-used
timestamps. Sessions are capped (least-recently-used are evicted) so runaway session_ids
cannot grow memory without bound.

### 9. list_packages - List installed packages
List installed Python distributions (optionally filtered by a substring) so you can check
whether a package is available before an execute that would otherwise fail on ImportError.

### 10. pip_install - Install packages
Install one or more packages into the server interpreter via `python -m pip install`.
Returns pip's return code plus stdout/stderr.

## MCP Tool Integration
Python code automatically has access to an 'mcp' module (pre-imported in execution context) that can 
call any MCP tool using the same structure as AI tool calls.

Token auto-injection: because this bridge runs in-process as an already-trusted caller, you
may OMIT `tool_unlock_token` when calling other tools from here - the bridge fetches the
target tool's token and injects an inter-tool credential automatically (works with the
common targets: sqlite, user, python, remote/browser tools, agent). If a particular tool
rejects the injected credential, call that tool's readme operation and pass its literal
token. The token values shown in the examples below are PLACEHOLDERS, not real tokens.

```python
# Note: 'mcp' is already available - no import needed!
import json

# Example 1: Show popup window to user
mcp.call("user", {
    "input": {
        "operation": "show_popup",
        "html": "<!DOCTYPE html><html><body><h1>Hello!</h1><button onclick=\"window.close()\">OK</button></body></html>",
        "title": "Demo",
        "width": 250,
        "height": 120,
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"  # From user tool readme
    }
})

# Example 2: List all browser tabs (async tool - automatically waits for response)
# NOTE: browser tools are named after the user's running browser: chrome_browser,
# edge_browser, etc. (a 2nd instance gets a number suffix, e.g. chrome_browser2).
# They only exist while that browser is running - check the live tool list.
tabs_result = mcp.call("chrome_browser", {
    "input": {
        "operation": "list_tabs",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"  # From chrome_browser tool readme
    }
})
tabs_text = tabs_result['content'][0]['text']
print(f"Browser tabs: {tabs_text[:200]}...")

# Example 3: Query SQLite database
db_result = mcp.call("sqlite", {
    "input": {
        "sql": "SELECT * FROM users LIMIT 10",
        "database": "myapp.db",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"  # From SQLite tool readme
    }
})
print(f"Database query result: {db_result}")

# Example 4: Navigate browser to a URL
nav_result = mcp.call("chrome_browser", {
    "input": {
        "operation": "navigate",
        "url": "https://example.com",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
    }
})

# Example 5: Extract page text and store in database
content_result = mcp.call("chrome_browser", {
    "input": {
        "operation": "extract_text",
        "tabId": 123,  # a tab id from list_tabs
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
    }
})

# Store the extracted text (TSV of visible text nodes with node IDs)
page_text = content_result['content'][0]['text']
insert_result = mcp.call("sqlite", {
    "input": {
        "sql": "INSERT INTO web_content (content) VALUES (:content)",
        "bindings": {"content": page_text},
        "database": "scraped_data.db",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
    }
})

# The bridge is completely generic - works with ANY tool
# Just use the exact same JSON structure you see in tool documentation
# tool_unlock_token is usually optional here - omit it and the bridge auto-injects
# Async tools (chrome_browser, remote) automatically wait for responses
```

## File Management
All script files are stored in the user data directory (e.g., C:\\Users\\user\\AppData\\Roaming\\AuraFriday\\user_data\\python_scripts\\).

## Session Management
- **persistent**: true (default) - Variables and imports persist between executions within the same session_id
- **persistent**: false - Fresh environment for each execution
- **session_id**: Optional identifier for multiple parallel sessions (default: "default")
- **run_on_main_thread**: false (default) - Execute on worker thread (fast, concurrent)
- **run_on_main_thread**: true - Execute on main thread (required for COM objects to persist)
- Use **clear_session** operation to free memory and clear cached session state

### Main Thread Execution
By default, Python code executes on worker threads for maximum concurrency. However, Windows COM 
objects have thread affinity and cannot persist across different worker threads. For multi-call 
COM automation workflows, set `run_on_main_thread: true` to execute on the main thread where 
COM objects can safely persist between calls.

**When to use run_on_main_thread=true:**
- Multi-call COM workflows (Excel, Word, Outlook, etc.)
- When COM objects need to persist across multiple execute calls
- When using persistent sessions with COM objects

**Trade-offs:**
- Worker thread (default): Fast, concurrent, but COM objects don't persist between calls
- Main thread: COM objects persist, but may delay other main thread operations slightly

### Persistent Session Example:
```python
# Call 1: Create variables in persistent session
result = mcp.call("python", {
    "input": {
        "operation": "execute",
        "session_id": "my_session",
        "persistent": True,
        "code": "counter = 0\\nprint(f'Counter initialized: {counter}')",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})

# Call 2: Variables from Call 1 still exist!
result = mcp.call("python", {
    "input": {
        "operation": "execute",
        "session_id": "my_session",  # Same session_id
        "persistent": True,
        "code": "counter += 1\\nprint(f'Counter incremented: {counter}')",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})

# Call 3: Clear the session when done
result = mcp.call("python", {
    "input": {
        "operation": "clear_session",
        "session_id": "my_session",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})
```

### COM Automation Example (Windows):
```python
# Call 1: Create Excel COM objects on main thread
result = mcp.call("python", {
    "input": {
        "operation": "execute",
        "session_id": "excel_work",
        "persistent": True,
        "run_on_main_thread": True,  # Required for COM persistence!
        "code": "import win32com.client\\nimport pythoncom\\nif 'com_init' not in dir():\\n    pythoncom.CoInitialize()\\n    com_init = True\\nexcel = win32com.client.Dispatch('Excel.Application')\\nwb = excel.Workbooks.Add()\\nsheet = wb.Worksheets(1)\\nprint('Excel objects created')",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})

# Call 2: Use same Excel instance (objects persist because we're on main thread)
result = mcp.call("python", {
    "input": {
        "operation": "execute",
        "session_id": "excel_work",
        "persistent": True,
        "run_on_main_thread": True,  # Same mode
        "code": "sheet.Range('A1').Value = 'Hello from Python!'\\nsheet.Range('A2').Value = 42\\nprint('Data written to Excel')",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})

# Call 3: Save and close
result = mcp.call("python", {
    "input": {
        "operation": "execute",
        "session_id": "excel_work",
        "persistent": True,
        "run_on_main_thread": True,
        "code": "wb.SaveAs('C:\\\\\\\\temp\\\\\\\\test.xlsx')\\nwb.Close()\\nexcel.Quit()\\nprint('Workbook saved and closed')",
        "tool_unlock_token": "<this python tool token, same one you used to call python>"
    }
})
```

## Input Examples

### 1. Get documentation:
```json
{
  "input": {"operation": "readme"}
}
```

### 2. Execute: List browser tabs and count by domain
```json
{
  "input": {
    "operation": "execute",
    "code": "import json\\nfrom collections import Counter\\n\\n# Get browser tabs (tool is named after the user's browser: chrome_browser, edge_browser, ...)\\ntabs = mcp.call('chrome_browser', {'input': {'operation': 'list_tabs', 'tool_unlock_token': '<target tool readme token, or omit - auto-injected>'}})\\ntabs_text = tabs['content'][0]['text']\\nprint(f'Raw tabs data (first 200 chars): {tabs_text[:200]}')\\n\\n# Parse and count domains\\ndomains = []\\nfor line in tabs_text.strip().split('\\\\n')[1:]:\\n    parts = line.split('\\\\t')\\n    if len(parts) >= 7 and 'http' in parts[6]:\\n        domain = parts[6].split('/')[2]\\n        domains.append(domain)\\n\\ncounts = Counter(domains)\\nprint(f'\\\\nDomain counts: {dict(counts)}')",
    "session_id": "browser_analysis",
    "persistent": true,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 3. Execute: Query SQLite and process results
```json
{
  "input": {
    "operation": "execute",
    "code": "import json\\n\\n# Query database\\nresult = mcp.call('sqlite', {\\n    'input': {\\n        'sql': 'SELECT name, price FROM products LIMIT 5',\\n        'database': 'store.db',\\n        'tool_unlock_token': '<target tool readme token, or omit - auto-injected>'\\n    }\\n})\\n\\n# Parse and display results\\ndata = json.loads(result['content'][0]['text'])\\nprint(f'Found {len(data)} products:')\\nfor row in data:\\n    print(f'  - {row[\\\"name\\\"]}: ${row[\\\"price\\\"]}')",
    "session_id": "data_query",
    "persistent": false,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 4. Save script: Browser tab monitoring
```json
{
  "input": {
    "operation": "save_script",
    "filename": "monitor_tabs.py",
    "code": "import json\\nfrom datetime import datetime\\n\\n# Get current browser tabs\\ntabs_result = mcp.call('chrome_browser', {\\n    'input': {\\n        'operation': 'list_tabs',\\n        'tool_unlock_token': '<target tool readme token, or omit - auto-injected>'\\n    }\\n})\\n\\n# Count tabs\\ntabs_text = tabs_result['content'][0]['text']\\ntab_count = len(tabs_text.strip().split('\\\\n')) - 1\\n\\n# Store in database\\nmcp.call('sqlite', {\\n    'input': {\\n        'sql': 'INSERT INTO tab_history (timestamp, count) VALUES (:ts, :count)',\\n        'bindings': {'ts': datetime.now().isoformat(), 'count': tab_count},\\n        'database': 'monitoring.db',\\n        'tool_unlock_token': '<target tool readme token, or omit - auto-injected>'\\n    }\\n})\\n\\nprint(f'Logged {tab_count} tabs at {datetime.now()}')",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 5. Load saved script:
```json
{
  "input": {
    "operation": "load_script",
    "filename": "monitor_tabs.py",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 6. List saved scripts:
```json
{
  "input": {
    "operation": "list_scripts",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 7. Delete saved script:
```json
{
  "input": {
    "operation": "delete_script",
    "filename": "old_script.py",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

## Use Cases & Real-World Examples

### 1. Web Scraping to Database
Scrape a page via the user's browser and store its text in SQLite:
```python
# Navigate and get the loaded page's text in one call (withText)
url = "https://store.example.com/products"
result = mcp.call("chrome_browser", {"input": {"operation": "navigate", "url": url, "withText": True, "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"}})
page_text = result['content'][0]['text']

# Store the page text in the database
mcp.call("sqlite", {
    "input": {
        "sql": "INSERT INTO scraped_pages (url, content) VALUES (:url, :content)",
        "bindings": {"url": url, "content": page_text},
        "database": "products.db",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
    }
})
print(f"Stored {len(page_text)} chars from {url}")
```

### 2. Browser Tab Analysis
Analyze open browser tabs and categorize by domain:
```python
import json
from collections import Counter

# Get all open tabs
tabs_result = mcp.call("chrome_browser", {"input": {"operation": "list_tabs", "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"}})
tabs_text = tabs_result['content'][0]['text']

# Parse tab data (tab-separated format)
tabs = []
for line in tabs_text.strip().split('\\n')[1:]:  # Skip header
    parts = line.split('\\t')
    if len(parts) >= 7:
        tabs.append({'url': parts[6], 'title': parts[7]})

# Count domains
domains = Counter(url.split('/')[2] for url in [t['url'] for t in tabs] if 'http' in t['url'])

# Store analysis in database
for domain, count in domains.items():
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT OR REPLACE INTO tab_stats (domain, count, last_updated) VALUES (:domain, :count, datetime('now'))",
            "bindings": {"domain": domain, "count": count},
            "database": "browser_stats.db",
            "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
        }
    })
```

### 3. Data Processing Pipeline
Process large datasets that exceed AI context limits:
```python
import json

# Query large result set from database
result = mcp.call("sqlite", {
    "input": {
        "sql": "SELECT * FROM large_dataset LIMIT 10000",
        "database": "bigdata.db",
        "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
    }
})

# Process data (transform, aggregate, filter)
data = json.loads(result['content'][0]['text'])
processed = [{'id': row['id'], 'value': row['raw_value'] * 1.5} for row in data]

# Store processed results
for row in processed:
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT INTO processed_data (id, value) VALUES (:id, :value)",
            "bindings": {"id": row['id'], "value": row['value']},
            "database": "bigdata.db",
            "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
        }
    })
```

### 4. Cross-Tool Automation
Monitor browser activity and trigger actions based on content:
```python
# Check if specific page is open
tabs = mcp.call("chrome_browser", {"input": {"operation": "list_tabs", "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"}})
tabs_text = tabs['content'][0]['text']

if 'gmail.com' in tabs_text:
    # Log browser activity
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT INTO activity_log (timestamp, activity) VALUES (datetime('now'), 'Gmail tab detected')",
            "database": "monitoring.db",
            "tool_unlock_token": "<target tool readme token, or omit - auto-injected>"
        }
    })
```

These examples demonstrate how Python serves as "glue" between MCP tools, enabling complex 
workflows that would be impossible with AI context limits alone.

## Execution Timeout (execute / run_script)
- No timeout by default on worker-thread runs: an infinite loop would run indefinitely.
- Pass `timeout` (seconds) to bound a run. On a worker thread the code runs in a daemon
  thread that is ABANDONED if it overruns - it keeps running in the background (Python
  cannot force-preempt a thread), its later output goes to a buffer nobody reads, and for
  a persistent session further calls to that same session_id may block until it finishes.
  If you need a hard timeout, use a fresh session_id per attempt (or persistent: false).
- On the main thread (run_on_main_thread: true), `timeout` bounds the wait (default 300s);
  an abandoned main-thread task can delay other main-thread work until it completes.

## Working Directory / Environment / argv (execute and run_script)
- `cwd`: switch the process working directory for the run, restored afterward.
- `env`: dict merged into os.environ for the run, restored afterward.
- `argv`: list assigned to sys.argv for the run (run_script prepends the script path).
- These mutate process-global state, so a run that uses any of them holds a process-wide
  lock for its duration; runs that use none of them stay fully concurrent.

## Output Capture Caveats
- stdout/stderr capture is thread-aware (concurrent runs do not cross-capture each other),
  but it only intercepts Python-level writes. Output from `subprocess`, or fd-level writes
  from C extensions, bypasses it and goes to the real server console.
- Threads you start in user code outlive the call. Their later prints go to whatever
  capture buffer (or real stream) is active at that later moment, not this call's result.

## Security & Isolation
There is NO sandbox. Code runs in-process with the full Python interpreter: it can import
anything, touch the filesystem and network, and can crash or exit the whole server (e.g.
`os._exit()` or a segfaulting C extension will take the server down). The safety model is
account/OS isolation, not language-level restriction - treat this exactly like a shell on
the machine. All MCP tool calls made via `mcp` are logged and subject to the same security
policies as direct tool usage.

## Return Format
Returns a JSON payload with:
- `stdout` / `stderr`: captured output, each capped at max_output bytes (default 65536);
  longer output keeps the head and tail with a '[N bytes truncated]' marker in between.
- `result`: repr() of the last top-level expression, or null if the code did not end in a
  bare expression (execute / run_script only).
- `mcp_calls`: log of MCP tool calls made during execution.
- `success`: true if the code ran without raising; false if it raised or timed out.
- `session_id`, `persistent`, and (on timeout) `timed_out` / `timeout_seconds`.

IMPORTANT: the tool response `isError` is False for a successful TOOL CALL even when your
code raised - a failing user script is still a successful invocation of this tool. Check
the `success` field in this payload to tell whether your code ran cleanly, not `isError`.
"""
    }
]
# Python script storage directory
def get_python_scripts_directory() -> Path:
    """Get the directory where Python scripts are stored."""
    scripts_dir = get_user_data_directory() / "python_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir

def resolve_safe_script_path_confined_to_scripts_directory(requested_filename, scripts_directory: Path) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a caller-supplied script filename to a safe path inside scripts_directory.

    Fixes the path-traversal hole (item 3): callers could previously pass "../../x.py" or
    an absolute path (pathlib replaces the base with an absolute right operand) to read,
    write or delete arbitrary files. We strip to the bare name, reject empty/'.'/'..',
    append '.py' when missing so save/load/delete stay consistent with the '*.py' listing
    (item 14), then defensively confirm the resolved path is still under the directory.

    Returns (script_path, None) on success or (None, error_message) on rejection.
    """
    if not requested_filename or not isinstance(requested_filename, str):
        return None, "Parameter 'filename' is required and must be a non-empty string."
    # Path(...).name discards any directory components (including absolute-path prefixes),
    # so traversal segments cannot escape the scripts directory.
    bare_name_without_any_directory_components = Path(requested_filename).name
    if bare_name_without_any_directory_components in ("", ".", ".."):
        return None, f"Invalid filename '{requested_filename}': must be a plain script name, not a path."
    if not bare_name_without_any_directory_components.endswith(".py"):
        bare_name_without_any_directory_components += ".py"
    candidate_script_path = scripts_directory / bare_name_without_any_directory_components
    # Defensive belt-and-braces check in case symlinks or odd names still point outside.
    try:
        resolved_candidate = candidate_script_path.resolve()
        resolved_scripts_root = scripts_directory.resolve()
        if not resolved_candidate.is_relative_to(resolved_scripts_root):
            return None, f"Invalid filename '{requested_filename}': resolves outside the scripts directory."
    except (OSError, ValueError) as path_resolution_error:
        return None, f"Invalid filename '{requested_filename}': {path_resolution_error}"
    return candidate_script_path, None

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]  # Only operation is required for readme
    
    # Check for unexpected parameters
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        # Error stays terse (item 13); point at the readme operation instead of an attached doc
        return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}. Use the readme operation for full documentation.", {}
    
    # Check for missing required parameters
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}. Required parameters are: {', '.join(sorted(required))}", {}
    
    # Validate types and extract values
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            # Type validation
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}. Please provide a string value.", {}
            elif expected_type == "object" and not isinstance(value, dict):
                return f"Parameter '{param_name}' must be an object/dictionary, got {type(value).__name__}. Please provide a dictionary value.", {}
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                # bool is an int subclass in Python - it must not pass as an integer
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}. Please provide an integer value.", {}
            elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                # "number" accepts int or float; bool is an int subclass and must be excluded (item 9 spirit)
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}. Please provide a numeric value.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}. Please provide true or false.", {}
            elif expected_type == "array" and not isinstance(value, list):
                return f"Parameter '{param_name}' must be an array/list, got {type(value).__name__}. Please provide a list value.", {}
            
            # Enum validation
            if "enum" in param_schema:
                allowed_values = param_schema["enum"]
                if value not in allowed_values:
                    return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'. Please use one of the allowed values.", {}
            
            validated[param_name] = value
        elif param_name in required:
            # This should have been caught above, but double-check
            return f"Required parameter '{param_name}' is missing. Please provide this required parameter.", {}
        else:
            # Use the schema default if one is present. Sentinel (not "is not None") so a
            # schema default of literal None would still be applied rather than dropped (item 11).
            default_value = param_schema.get("default", _SCHEMA_DEFAULT_ABSENT_SENTINEL)
            if default_value is not _SCHEMA_DEFAULT_ABSENT_SENTINEL:
                validated[param_name] = default_value
    
    return None, validated

def readme(with_readme: bool = True) -> str:
    """Return tool documentation.
    
    Args:
        with_readme: If False, returns empty string. If True, returns the complete tool documentation.
        
    Returns:
        The complete tool documentation with the readme content as description, or empty string if with_readme is False.
    """
    try:
        if not with_readme:
            return ''
            
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        # Return pure content; the blank-line separator is now supplied by callers
        # (create_error_response) so readme() has no leading whitespace of its own.
        return json.dumps({
            "description": TOOLS[0]["readme"],
            "parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
            #"real_parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)

    Per item 13 the full readme is attached only on token-level failures; ordinary
    parameter/operation errors pass with_readme=False to stay terse and save agent context.
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    readme_text = readme(with_readme)
    # Separator lives here (not inside readme()) so readme() returns pure content
    separator_before_readme = "\n\n" if readme_text else ""
    return {"content": [{"type": "text", "text": f"{error_msg}{separator_before_readme}{readme_text}"}], "isError": True}

def handle_execute(params: Dict, handler_info: Optional[Dict] = None) -> Dict:
    """Handle Python code execution.
    
    Args:
        params: Dictionary containing the operation parameters
        handler_info: Handler info containing server instance with tool_handlers
        
    Returns:
        Dict containing execution results or error information
    """
    try:
        # Presence check only. validate_parameters already guaranteed the string type when
        # 'code' is present (it is not in 'required'), so the redundant type check was dropped.
        code = params.get("code")
        if code is None:
            return create_error_response("Parameter 'code' is required for execute operation. Please provide the Python code to execute.", with_readme=False)
        
        session_id = params.get("session_id", "default")
        persistent = params.get("persistent", True)
        run_on_main_thread = params.get("run_on_main_thread", False)
        max_output = params.get("max_output", DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR)
        # New optional execution controls (items 24/8 timeout, 27 cwd/env, 22-style argv)
        timeout_seconds = params.get("timeout")
        argv_list = params.get("argv")
        cwd_override = params.get("cwd")
        env_overrides = params.get("env")
        # Fail fast with a terse parameter error instead of an in-run chdir traceback
        if cwd_override is not None and not os.path.isdir(cwd_override):
            return create_error_response(f"Parameter 'cwd' must be an existing directory, got '{cwd_override}'.", with_readme=False)
        
        # Log the execution request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing execute request: session_id={session_id}, persistent={persistent}, run_on_main_thread={run_on_main_thread}, code_length={len(code)}, timeout={timeout_seconds}")
        
        # Execute the Python code with MCP integration
        result = _execute_python_code(
            code, session_id, persistent, run_on_main_thread, handler_info, max_output,
            timeout_seconds=timeout_seconds, argv_list=argv_list, cwd_override=cwd_override,
            env_overrides=env_overrides, execution_filename_for_tracebacks="<mcp python execute>",
            synthetic_file_path=None)
        
        return _wrap_execution_result_dict_as_tool_response(result)
            
    except Exception as e:
        return create_error_response(f"Error processing execute request: {str(e)}", with_readme=False)


def _remove_handler_info_keys_recursively_from_nested_dicts_and_lists(container_object):
    """Recursively strip 'handler_info' keys (they hold MCPSession/MCPServer objects that
    are not JSON serializable). Module-level so any handler can reuse it (minor-cleanup
    item: hoisted out of handle_execute)."""
    if isinstance(container_object, dict):
        return {key: _remove_handler_info_keys_recursively_from_nested_dicts_and_lists(value)
                for key, value in container_object.items() if key != 'handler_info'}
    elif isinstance(container_object, list):
        return [_remove_handler_info_keys_recursively_from_nested_dicts_and_lists(item) for item in container_object]
    else:
        return container_object


def _wrap_execution_result_dict_as_tool_response(execution_result_dict: Dict) -> Dict:
    """Serialize an execution result dict into the standard tool response.

    Strips non-serializable handler_info defensively and uses default=repr so a stray
    non-serializable value in mcp_calls cannot blow up the whole response (item 18).

    Note: isError is intentionally False even when the user's code failed - a failed
    *user script* is still a successful *tool call*. Agents must check the "success"
    field inside the JSON payload, not isError (documented in the readme, item 12).
    """
    safe_result = _remove_handler_info_keys_recursively_from_nested_dicts_and_lists(execution_result_dict)
    return {
        "content": [{"type": "text", "text": json.dumps(safe_result, indent=2, default=repr)}],
        "isError": False
    }


DEFAULT_MAIN_THREAD_EXECUTION_TIMEOUT_SECONDS = 300


def _execute_python_code(code: str, session_id: str, persistent: bool, run_on_main_thread: bool, handler_info: Optional[Dict] = None, max_output: int = DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR, timeout_seconds=None, argv_list=None, cwd_override=None, env_overrides=None, execution_filename_for_tracebacks: str = "<mcp python execute>", synthetic_file_path: Optional[str] = None) -> Dict:
    """Execute Python code using exec() in the same process with MCP bridge access.
    
    Args:
        code: Python code to execute
        session_id: Session identifier
        persistent: Whether to maintain session state between executions
        run_on_main_thread: Whether to execute on main thread (required for COM persistence)
        handler_info: Handler info containing server instance with tool_handlers
        max_output: Byte cap applied to returned stdout/stderr (head+tail truncation)
        timeout_seconds: Optional wall-clock timeout. On the main thread it bounds the wait
            (default 300s); on a worker thread the code runs in a daemon thread that is
            abandoned if it overruns (items 8, 24).
        argv_list: Optional replacement for sys.argv during the run (items 22, 27)
        cwd_override: Optional working directory for the run, restored afterward (item 27)
        env_overrides: Optional dict of environment variables merged for the run (item 27)
        execution_filename_for_tracebacks: Filename shown in tracebacks / compiled code
        synthetic_file_path: Value for exec_globals['__file__'] (set by run_script, item 15)
        
    Returns:
        Dict with stdout, stderr, mcp_calls, and other execution info
    """
    # Normalize the optional timeout: zero/negative (or non-numeric) means "no explicit
    # timeout", so Event.wait()/join() below never receive a nonsensical bound. Main-thread
    # runs then fall back to their 300s default; worker runs stay unlimited.
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        timeout_seconds = None

    # If main thread execution requested, delegate to server's main thread queue
    if run_on_main_thread and handler_info and 'responder' in handler_info:
        server = handler_info['responder']
        if hasattr(server, 'main_thread_queue'):
            # Re-entrancy: if we are ALREADY on the thread that drains main_thread_queue
            # (server.main_thread_id - the serve_forever loop thread, which is not
            # necessarily Python's MainThread because friday.py runs the server on a
            # daemon thread), queueing would deadlock waiting on ourselves. Also honor
            # the plain MainThread case for deployments that serve on the real main thread.
            if (threading.get_ident() == getattr(server, 'main_thread_id', None)
                    or threading.current_thread() is threading.main_thread()):
                MCPLogger.log(TOOL_LOG_NAME, f"Already on main thread, executing directly: session={session_id}")
                # Already on the main thread: no daemon/timeout wrapper (that would defeat COM
                # affinity), just run it in-place.
                return _execute_python_code_impl(code, session_id, persistent, handler_info, max_output, worker_thread_timeout_seconds=None, argv_list=argv_list, cwd_override=cwd_override, env_overrides=env_overrides, execution_filename_for_tracebacks=execution_filename_for_tracebacks, synthetic_file_path=synthetic_file_path)
            MCPLogger.log(TOOL_LOG_NAME, f"Delegating to main thread: session={session_id}")
            
            result_container = {}
            result_event = threading.Event()
            # Shared flag so the (possibly abandoned) main-thread task can log if it finally
            # completes after the caller already gave up waiting (item 8).
            main_thread_waiter_gave_up_after_timeout_flag = {"gave_up": False}
            
            def execute_on_main_thread():
                """Wrapper to execute on main thread and capture result."""
                try:
                    result = _execute_python_code_impl(code, session_id, persistent, handler_info, max_output, worker_thread_timeout_seconds=None, argv_list=argv_list, cwd_override=cwd_override, env_overrides=env_overrides, execution_filename_for_tracebacks=execution_filename_for_tracebacks, synthetic_file_path=synthetic_file_path)
                    result_container['result'] = result
                except BaseException as e:
                    # BaseException: nothing may escape into the server's main-thread queue loop
                    result_container['result'] = {
                        "stdout": "",
                        "stderr": f"Main thread execution error: {str(e)}\n{traceback.format_exc()}",
                        "mcp_calls": [],
                        "session_id": session_id,
                        "persistent": persistent,
                        "success": False
                    }
                finally:
                    if main_thread_waiter_gave_up_after_timeout_flag["gave_up"]:
                        # Item 8: the task was abandoned on timeout but eventually finished;
                        # log it so a stalled main-thread queue is diagnosable.
                        MCPLogger.log(TOOL_LOG_NAME, f"Abandoned main-thread execution for session={session_id} finally completed after its timeout elapsed")
                    result_event.set()
            
            # Queue the execution on main thread
            server.main_thread_queue.put(execute_on_main_thread)
            
            # Wait for completion. timeout_seconds bounds the wait when provided; default 300.
            effective_main_thread_timeout_seconds = timeout_seconds if timeout_seconds is not None else DEFAULT_MAIN_THREAD_EXECUTION_TIMEOUT_SECONDS
            if result_event.wait(timeout=effective_main_thread_timeout_seconds):
                return result_container['result']
            else:
                main_thread_waiter_gave_up_after_timeout_flag["gave_up"] = True
                MCPLogger.log(TOOL_LOG_NAME, f"Main thread execution timeout ({effective_main_thread_timeout_seconds}s) for session={session_id}; task abandoned (still queued/running on main thread)")
                return {
                    "stdout": "",
                    "stderr": f"Main thread execution timeout (exceeded {effective_main_thread_timeout_seconds} seconds). The task was abandoned but may still be running on the main thread and can block other main-thread work.",
                    "mcp_calls": [],
                    "session_id": session_id,
                    "persistent": persistent,
                    "success": False,
                    "timed_out": True,
                    "timeout_seconds": effective_main_thread_timeout_seconds
                }
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Main thread requested but not available, falling back to worker thread")
    
    # Execute on current (worker) thread
    return _execute_python_code_impl(code, session_id, persistent, handler_info, max_output, worker_thread_timeout_seconds=timeout_seconds, argv_list=argv_list, cwd_override=cwd_override, env_overrides=env_overrides, execution_filename_for_tracebacks=execution_filename_for_tracebacks, synthetic_file_path=synthetic_file_path)


def _get_or_create_execution_serialization_rlock_for_session_id(session_id: str) -> "threading.RLock":
    """Get (or lazily create) the per-session RLock that serializes same-session executions."""
    with _session_cache_thread_safety_lock:
        session_execution_serialization_rlock = _session_id_to_execution_serialization_rlock_map.get(session_id)
        if session_execution_serialization_rlock is None:
            session_execution_serialization_rlock = threading.RLock()
            _session_id_to_execution_serialization_rlock_map[session_id] = session_execution_serialization_rlock
        return session_execution_serialization_rlock


def _evict_least_recently_used_persistent_sessions_over_cap_locked():
    """Evict the least-recently-used persistent sessions while the cache exceeds the cap.

    Prevents unbounded growth when an agent invents many session_ids (item 17). The caller
    MUST already hold _session_cache_thread_safety_lock. LRU-by-last-used naturally spares
    an actively running session (which has a recent last_used timestamp).
    """
    while len(_session_globals_cache_for_persistent_execution_contexts) > MAX_RETAINED_PERSISTENT_SESSIONS:
        least_recently_used_session_id = min(
            _session_globals_cache_for_persistent_execution_contexts.keys(),
            key=lambda candidate_session_id: _session_id_to_created_and_last_used_epoch_times_map.get(candidate_session_id, {}).get("last_used", 0.0))
        _session_globals_cache_for_persistent_execution_contexts.pop(least_recently_used_session_id, None)
        _session_id_to_created_and_last_used_epoch_times_map.pop(least_recently_used_session_id, None)
        _session_id_to_execution_serialization_rlock_map.pop(least_recently_used_session_id, None)
        MCPLogger.log(TOOL_LOG_NAME, f"Evicted least-recently-used persistent session over cap ({MAX_RETAINED_PERSISTENT_SESSIONS}): {least_recently_used_session_id}")


def _count_user_created_variables_in_exec_globals(exec_globals: Dict) -> int:
    """Count session variables the user created, excluding our injected baseline keys
    (__builtins__, mcp, __name__, __file__) so reports reflect real user state (items 17, 25)."""
    return sum(1 for key in exec_globals.keys() if key not in _BASELINE_EXEC_GLOBALS_KEYS_NOT_COUNTED_AS_USER_VARIABLES)


def _truncate_captured_output_keeping_head_and_tail_within_limit(captured_output_text: str, max_output_bytes_limit: int) -> str:
    """Cap returned stdout/stderr: keep head and tail halves, replacing the removed
    middle with a '[N bytes truncated]' marker (N = utf-8 bytes removed)."""
    if max_output_bytes_limit < 0:
        max_output_bytes_limit = 0
    if len(captured_output_text) <= max_output_bytes_limit:
        return captured_output_text
    head_character_count = max_output_bytes_limit // 2
    tail_character_count = max_output_bytes_limit - head_character_count
    removed_middle_text = captured_output_text[head_character_count:len(captured_output_text) - tail_character_count]
    removed_middle_byte_count = len(removed_middle_text.encode('utf-8', errors='replace'))
    # Slice tail via explicit start index: text[-0:] would wrongly return the whole string
    tail_text = captured_output_text[len(captured_output_text) - tail_character_count:] if tail_character_count > 0 else ""
    return (captured_output_text[:head_character_count]
            + f"\n[{removed_middle_byte_count} bytes truncated]\n"
            + tail_text)


def _exec_code_capturing_repr_of_any_trailing_top_level_expression(code_text: str, exec_globals: Dict, filename_for_tracebacks: str) -> Optional[str]:
    """Run code with exec(); if the last top-level statement is a bare expression, eval it
    and return repr(value) so callers get REPL-style result capture (item 23).

    Everything before the trailing expression is exec'd normally; the trailing expression
    is eval'd exactly once (same single evaluation a bare exec would do), so side effects
    are unchanged. Returns None when there is no trailing expression or its value is None.
    A SyntaxError from parsing/compiling propagates to the normal error handling.
    """
    parsed_module = ast.parse(code_text, filename=filename_for_tracebacks, mode="exec")
    trailing_node_is_expression = bool(parsed_module.body) and isinstance(parsed_module.body[-1], ast.Expr)
    if not trailing_node_is_expression:
        exec(compile(parsed_module, filename_for_tracebacks, "exec"), exec_globals)
        return None
    trailing_expression_node = parsed_module.body.pop()
    if parsed_module.body:
        exec(compile(parsed_module, filename_for_tracebacks, "exec"), exec_globals)
    trailing_value = eval(
        compile(ast.Expression(trailing_expression_node.value), filename_for_tracebacks, "eval"),
        exec_globals)
    if trailing_value is None:
        return None
    try:
        return repr(trailing_value)
    except Exception:
        return "<value repr() raised>"


def _run_code_with_optional_argv_cwd_env_overrides_and_trailing_expression_capture(code_text: str, exec_globals: Dict, filename_for_tracebacks: str, argv_list, cwd_override, env_overrides) -> Optional[str]:
    """Run code, temporarily applying any sys.argv / cwd / os.environ overrides.

    sys.argv, the working directory and os.environ are process-global, so when overrides
    are requested we hold a process-wide lock across the run and restore afterward (items
    22, 27). Runs without overrides skip the lock entirely and stay concurrent.
    """
    overrides_requested = argv_list is not None or cwd_override is not None or env_overrides is not None
    if not overrides_requested:
        return _exec_code_capturing_repr_of_any_trailing_top_level_expression(code_text, exec_globals, filename_for_tracebacks)
    with _process_global_state_mutation_lock_for_argv_cwd_env_overrides:
        saved_sys_argv = sys.argv
        saved_working_directory = None
        saved_environment_values_for_touched_keys = None
        try:
            if argv_list is not None:
                sys.argv = [str(argument) for argument in argv_list]
            if cwd_override is not None:
                saved_working_directory = os.getcwd()
                os.chdir(cwd_override)
            if env_overrides is not None:
                # Snapshot only the keys we are about to touch; restoring via a full
                # clear()+update() would momentarily wipe unrelated variables that other
                # server threads may be reading concurrently.
                saved_environment_values_for_touched_keys = {
                    str(environment_key): os.environ.get(str(environment_key))
                    for environment_key in env_overrides}
                for environment_key, environment_value in env_overrides.items():
                    os.environ[str(environment_key)] = str(environment_value)
            return _exec_code_capturing_repr_of_any_trailing_top_level_expression(code_text, exec_globals, filename_for_tracebacks)
        finally:
            sys.argv = saved_sys_argv
            if saved_working_directory is not None:
                try:
                    os.chdir(saved_working_directory)
                except OSError as cwd_restore_error:
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to restore working directory to {saved_working_directory}: {cwd_restore_error}")
            if saved_environment_values_for_touched_keys is not None:
                for environment_key, prior_environment_value in saved_environment_values_for_touched_keys.items():
                    if prior_environment_value is None:
                        os.environ.pop(environment_key, None)
                    else:
                        os.environ[environment_key] = prior_environment_value


def _execute_python_code_impl(code: str, session_id: str, persistent: bool, handler_info: Optional[Dict] = None, max_output: int = DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR, worker_thread_timeout_seconds=None, argv_list=None, cwd_override=None, env_overrides=None, execution_filename_for_tracebacks: str = "<mcp python execute>", synthetic_file_path: Optional[str] = None) -> Dict:
    """Implementation of Python code execution (can run on any thread).
    
    Args:
        code: Python code to execute
        session_id: Session identifier
        persistent: Whether to maintain session state between executions
        handler_info: Handler info containing server instance with tool_handlers
        max_output: Byte cap applied to returned stdout/stderr (head+tail truncation)
        worker_thread_timeout_seconds: If set (>0), run exec in a daemon thread and abandon
            it if it overruns, reporting a timeout (item 24). None = unlimited (prior behavior).
        argv_list / cwd_override / env_overrides: optional per-run process-state overrides
        execution_filename_for_tracebacks: filename used when compiling code
        synthetic_file_path: exec_globals['__file__'] value for run_script (item 15)
        
    Returns:
        Dict with stdout, stderr, mcp_calls, and other execution info
    """
    import io
    
    # Get tool handlers from server instance
    handlers = None
    if handler_info and 'responder' in handler_info:
        server = handler_info['responder']
        if hasattr(server, 'tool_handlers'):
            # Extract just the handler functions from tool_handlers dict
            handlers = {name: info['handler'] for name, info in server.tool_handlers.items()}
            # Provide handlers to mcp_bridge's shared default instance too, so legacy
            # module-level consumers (agent.py, cursor.py, llm_old.py, ocr.py) keep working
            mcp_bridge.set_handlers(handlers)
            MCPLogger.log(TOOL_LOG_NAME, f"Provided {len(handlers)} tool handlers to mcp_bridge")
        
        # Provide handler_info to mcp_bridge for remote tool support
        mcp_bridge.set_handler_info(handler_info)
        MCPLogger.log(TOOL_LOG_NAME, f"Provided handler_info to mcp_bridge for remote tool context")
    
    # Inject Python tool token for inter-tool authentication
    mcp_bridge._inject_token(TOOL_UNLOCK_TOKEN)
    
    # Per-execution bridge: each execution gets its own call log and handler_info,
    # so concurrent executions cannot clear/read each other's logs. When this call
    # carries no handler_info (e.g. in-process invocation), fall back to the default
    # instance's handlers registry so nested executions can still call tools.
    per_execution_mcp_bridge = mcp_bridge.Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info(
        handlers_dict=handlers if handlers is not None else mcp_bridge._get_handlers(),
        handler_info_dict=handler_info,
        python_tool_token=TOOL_UNLOCK_TOKEN)
    
    # Set up execution globals - use cached session if persistent
    now_epoch_time = time.time()
    with _session_cache_thread_safety_lock:
        if persistent and session_id in _session_globals_cache_for_persistent_execution_contexts:
            # Reuse existing session globals ('mcp' is rebound under the session lock below)
            exec_globals = _session_globals_cache_for_persistent_execution_contexts[session_id]
            session_times = _session_id_to_created_and_last_used_epoch_times_map.setdefault(
                session_id, {"created": now_epoch_time, "last_used": now_epoch_time})
            session_times["last_used"] = now_epoch_time
            MCPLogger.log(TOOL_LOG_NAME, f"Reusing persistent session: {session_id} (has {_count_user_created_variables_in_exec_globals(exec_globals)} user variables)")
        else:
            # Create new session globals. __builtins__ is the builtins MODULE (item 16) for
            # deterministic behavior, and __name__ is set so the `if __name__ == '__main__'`
            # idiom actually runs (item 15). __file__ is set only when running a saved script.
            exec_globals = {
                '__builtins__': builtins,
                '__name__': '__main__',
                'mcp': per_execution_mcp_bridge,  # Per-execution bridge object (isolated call log)
            }
            if synthetic_file_path is not None:
                exec_globals['__file__'] = synthetic_file_path
            if persistent:
                # Store for future calls, evicting the LRU session if we are over the cap
                _session_globals_cache_for_persistent_execution_contexts[session_id] = exec_globals
                _session_id_to_created_and_last_used_epoch_times_map[session_id] = {"created": now_epoch_time, "last_used": now_epoch_time}
                _evict_least_recently_used_persistent_sessions_over_cap_locked()
                MCPLogger.log(TOOL_LOG_NAME, f"Created new persistent session: {session_id}")
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"Using non-persistent session (fresh environment)")
    # For a reused session, keep __file__ current with this run; for a plain execute,
    # drop any stale __file__ left behind by an earlier run_script in the same session
    if synthetic_file_path is not None:
        exec_globals['__file__'] = synthetic_file_path
    else:
        exec_globals.pop('__file__', None)

    # Capture stdout and stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Outcome holder filled by the exec closure (which may run on a daemon thread for timeout)
    exec_outcome_holder = {"success": None, "trailing_expression_repr": None, "error_stderr_suffix": ""}

    def _perform_capture_activation_and_exec_on_this_thread():
        """Activate this thread's capture buffers, run the code, record the outcome.

        Runs on the calling thread normally, or on a daemon thread when a worker timeout is
        set - which is why capture activation happens here (per-thread), not in the caller.
        """
        stdout_thread_aware_proxy, stderr_thread_aware_proxy = _install_thread_aware_stream_proxies_over_sys_stdout_and_stderr_once()
        previously_active_stdout_buffer = stdout_thread_aware_proxy.activate_capture_buffer_for_current_thread(stdout_capture)
        previously_active_stderr_buffer = stderr_thread_aware_proxy.activate_capture_buffer_for_current_thread(stderr_capture)
        try:
            if persistent:
                # Same-session calls serialize for the duration of exec; different
                # sessions keep their own locks and stay concurrent
                with _get_or_create_execution_serialization_rlock_for_session_id(session_id):
                    # Rebind inside the lock: a waiting same-session call must not swap
                    # the bridge out from under the currently executing call, and each
                    # execution must run with its own (current handler_info) bridge
                    exec_globals['mcp'] = per_execution_mcp_bridge
                    trailing_expression_repr = _run_code_with_optional_argv_cwd_env_overrides_and_trailing_expression_capture(
                        code, exec_globals, execution_filename_for_tracebacks, argv_list, cwd_override, env_overrides)
            else:
                trailing_expression_repr = _run_code_with_optional_argv_cwd_env_overrides_and_trailing_expression_capture(
                    code, exec_globals, execution_filename_for_tracebacks, argv_list, cwd_override, env_overrides)
            exec_outcome_holder["success"] = True
            exec_outcome_holder["trailing_expression_repr"] = trailing_expression_repr
        except BaseException as caught_execution_error:
            # BaseException (not just Exception): user code calling sys.exit()/exit() or
            # raising KeyboardInterrupt/GeneratorExit must not kill the worker/request loop
            exec_outcome_holder["success"] = False
            exec_outcome_holder["error_stderr_suffix"] = (
                f"\nExecution error ({type(caught_execution_error).__name__}): {str(caught_execution_error)}\n{traceback.format_exc()}")
        finally:
            stdout_thread_aware_proxy.restore_previous_capture_buffer_for_current_thread(previously_active_stdout_buffer)
            stderr_thread_aware_proxy.restore_previous_capture_buffer_for_current_thread(previously_active_stderr_buffer)

    # Worker-thread timeout (item 24): run exec in a daemon thread and abandon it if it
    # overruns. True in-thread preemption is impossible, so the abandoned thread keeps
    # running (documented leak); its output goes to the now-unread capture buffer.
    if worker_thread_timeout_seconds is not None and worker_thread_timeout_seconds > 0:
        execution_daemon_thread = threading.Thread(
            target=_perform_capture_activation_and_exec_on_this_thread,
            name=f"python-tool-exec-{session_id}", daemon=True)
        execution_daemon_thread.start()
        execution_daemon_thread.join(worker_thread_timeout_seconds)
        if execution_daemon_thread.is_alive():
            MCPLogger.log(TOOL_LOG_NAME, f"Worker execution timeout ({worker_thread_timeout_seconds}s) for session={session_id}; abandoning thread")
            timeout_note = (f"\nExecution timed out after {worker_thread_timeout_seconds} seconds and was abandoned; "
                            f"it may still be running in the background. For a persistent session, further calls to "
                            f"session '{session_id}' may block until it finishes - use a new session_id.")
            return {
                "stdout": _truncate_captured_output_keeping_head_and_tail_within_limit(stdout_capture.getvalue(), max_output),
                "stderr": _truncate_captured_output_keeping_head_and_tail_within_limit(stderr_capture.getvalue() + timeout_note, max_output),
                "result": None,
                "mcp_calls": per_execution_mcp_bridge.get_call_log(),
                "session_id": session_id,
                "persistent": persistent,
                "success": False,
                "timed_out": True,
                "timeout_seconds": worker_thread_timeout_seconds
            }
    else:
        _perform_capture_activation_and_exec_on_this_thread()

    stderr_text = stderr_capture.getvalue() + (exec_outcome_holder["error_stderr_suffix"] or "")
    trailing_expression_repr = exec_outcome_holder["trailing_expression_repr"]
    if isinstance(trailing_expression_repr, str):
        # A huge repr must not blow the caller's context either - same cap as stdout/stderr
        trailing_expression_repr = _truncate_captured_output_keeping_head_and_tail_within_limit(trailing_expression_repr, max_output)
    return {
        "stdout": _truncate_captured_output_keeping_head_and_tail_within_limit(stdout_capture.getvalue(), max_output),
        "stderr": _truncate_captured_output_keeping_head_and_tail_within_limit(stderr_text, max_output),
        "result": trailing_expression_repr,  # repr of trailing expression, or null (item 23)
        "mcp_calls": per_execution_mcp_bridge.get_call_log(),
        "session_id": session_id,
        "persistent": persistent,
        "success": bool(exec_outcome_holder["success"])
    }

def handle_save_script(params: Dict) -> Dict:
    """Handle saving Python code to file.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing save results or error information
    """
    try:
        filename = params.get("filename")
        code = params.get("code")
        
        if code is None:
            # None only (not falsy): an empty string is a legitimate script to save
            return create_error_response("Parameter 'code' is required for save_script operation.", with_readme=False)
        
        scripts_dir = get_python_scripts_directory()
        # Confine the filename to the scripts directory and normalize to .py (items 3, 14)
        script_path, filename_error = resolve_safe_script_path_confined_to_scripts_directory(filename, scripts_dir)
        if filename_error:
            return create_error_response(filename_error, with_readme=False)
        
        # Atomic write: write to a temp file in the same directory then os.replace(), so a
        # crash mid-write cannot leave a truncated script behind (item 19).
        import tempfile
        temp_file_descriptor, temp_file_path_string = tempfile.mkstemp(dir=str(scripts_dir), prefix=".tmp_", suffix=".py")
        try:
            with os.fdopen(temp_file_descriptor, "w", encoding="utf-8", newline="") as temp_file_handle:
                temp_file_handle.write(code)
            os.replace(temp_file_path_string, str(script_path))
        except BaseException:
            # Clean up the temp file on any failure so we do not leak partial temp files
            try:
                os.unlink(temp_file_path_string)
            except OSError:
                pass
            raise
        
        MCPLogger.log(TOOL_LOG_NAME, f"Saved script to {script_path}")
        
        result = {
            "filename": script_path.name,   # normalized (.py-enforced) name actually written
            "path": str(script_path),
            "size": len(code),
            "saved": True
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error saving script: {str(e)}", with_readme=False)

def handle_load_script(params: Dict) -> Dict:
    """Handle loading Python code from file.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing loaded code or error information
    """
    try:
        filename = params.get("filename")
        
        scripts_dir = get_python_scripts_directory()
        # Same confinement/normalization as save so a bare name resolves to its .py file (items 3, 14)
        script_path, filename_error = resolve_safe_script_path_confined_to_scripts_directory(filename, scripts_dir)
        if filename_error:
            return create_error_response(filename_error, with_readme=False)
        
        if not script_path.exists():
            return create_error_response(f"Script file '{script_path.name}' not found.", with_readme=False)
        
        code = script_path.read_text(encoding='utf-8')
        
        MCPLogger.log(TOOL_LOG_NAME, f"Loaded script from {script_path}")
        
        result = {
            "filename": script_path.name,
            "code": code,
            "size": len(code),
            "path": str(script_path)
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error loading script: {str(e)}", with_readme=False)

def handle_list_scripts(params: Dict) -> Dict:
    """Handle listing saved Python scripts.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing list of scripts or error information
    """
    try:
        scripts_dir = get_python_scripts_directory()
        
        from datetime import datetime
        scripts = []
        for script_path in scripts_dir.glob("*.py"):
            stat = script_path.stat()
            scripts.append({
                "filename": script_path.name,
                "size": stat.st_size,
                # ISO-8601 for agents; keep the raw epoch too (item 20)
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "modified_epoch": stat.st_mtime,
                "path": str(script_path)
            })
        
        scripts.sort(key=lambda x: x["filename"])
        
        MCPLogger.log(TOOL_LOG_NAME, f"Listed {len(scripts)} scripts")
        
        result = {
            "scripts": scripts,
            "count": len(scripts),
            "directory": str(scripts_dir)
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error listing scripts: {str(e)}", with_readme=False)

def handle_delete_script(params: Dict) -> Dict:
    """Handle deleting a saved Python script.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing deletion results or error information
    """
    try:
        filename = params.get("filename")
        
        scripts_dir = get_python_scripts_directory()
        # Confine/normalize the same way as save/load so delete cannot escape the dir (items 3, 14)
        script_path, filename_error = resolve_safe_script_path_confined_to_scripts_directory(filename, scripts_dir)
        if filename_error:
            return create_error_response(filename_error, with_readme=False)
        
        if not script_path.exists():
            return create_error_response(f"Script file '{script_path.name}' not found.", with_readme=False)
        
        script_path.unlink()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Deleted script {script_path}")
        
        result = {
            "filename": script_path.name,
            "deleted": True
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error deleting script: {str(e)}", with_readme=False)

def handle_clear_session(params: Dict) -> Dict:
    """Handle clearing a persistent session's cached globals.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing clear results or error information
    """
    try:
        session_id = params.get("session_id", "default")
        
        with _session_cache_thread_safety_lock:
            if session_id in _session_globals_cache_for_persistent_execution_contexts:
                # Report user variables freed (excluding our injected baseline), not the raw
                # globals count that included __builtins__/mcp/__name__ (item 17 minor).
                user_variable_count = _count_user_created_variables_in_exec_globals(
                    _session_globals_cache_for_persistent_execution_contexts[session_id])
                
                # Remove the session and its bookkeeping
                del _session_globals_cache_for_persistent_execution_contexts[session_id]
                _session_id_to_created_and_last_used_epoch_times_map.pop(session_id, None)
                _session_id_to_execution_serialization_rlock_map.pop(session_id, None)
                
                MCPLogger.log(TOOL_LOG_NAME, f"Cleared session: {session_id} (had {user_variable_count} user variables)")
                
                result = {
                    "session_id": session_id,
                    "cleared": True,
                    "variables_freed": user_variable_count
                }
                
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
                    "isError": False
                }
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"Session not found (may already be cleared): {session_id}")
                
                # Include the live session inventory so a wrong session_id is easy to spot (item 17)
                currently_active_session_ids = sorted(_session_globals_cache_for_persistent_execution_contexts.keys())
                result = {
                    "session_id": session_id,
                    "cleared": False,
                    "message": "Session not found (may already be cleared or never existed)",
                    "active_session_count": len(currently_active_session_ids),
                    "active_session_ids": currently_active_session_ids
                }
                
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
                    "isError": False
                }
            
    except Exception as e:
        return create_error_response(f"Error clearing session: {str(e)}", with_readme=False)

def handle_run_script(params: Dict, handler_info: Optional[Dict] = None) -> Dict:
    """Handle running a saved Python script by name (item 22).

    Loads the saved file and executes it exactly like execute, but with __file__ set to the
    script path and sys.argv set to [script_path, *argv], so the agent does not have to
    load -> paste -> execute (which round-trips the whole file through its context).
    """
    try:
        filename = params.get("filename")
        scripts_dir = get_python_scripts_directory()
        script_path, filename_error = resolve_safe_script_path_confined_to_scripts_directory(filename, scripts_dir)
        if filename_error:
            return create_error_response(filename_error, with_readme=False)
        if not script_path.exists():
            return create_error_response(f"Script file '{script_path.name}' not found.", with_readme=False)
        code = script_path.read_text(encoding='utf-8')

        session_id = params.get("session_id", "default")
        persistent = params.get("persistent", True)
        run_on_main_thread = params.get("run_on_main_thread", False)
        max_output = params.get("max_output", DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR)
        timeout_seconds = params.get("timeout")
        cwd_override = params.get("cwd")
        env_overrides = params.get("env")
        # Fail fast with a terse parameter error instead of an in-run chdir traceback
        if cwd_override is not None and not os.path.isdir(cwd_override):
            return create_error_response(f"Parameter 'cwd' must be an existing directory, got '{cwd_override}'.", with_readme=False)
        # sys.argv[0] is the script path; any provided argv follows (item 22)
        user_supplied_argv = params.get("argv") or []
        synthetic_argv_list = [str(script_path)] + [str(argument) for argument in user_supplied_argv]

        MCPLogger.log(TOOL_LOG_NAME, f"Processing run_script: {script_path.name}, session_id={session_id}, argv={synthetic_argv_list}")

        result = _execute_python_code(
            code, session_id, persistent, run_on_main_thread, handler_info, max_output,
            timeout_seconds=timeout_seconds, argv_list=synthetic_argv_list, cwd_override=cwd_override,
            env_overrides=env_overrides, execution_filename_for_tracebacks=str(script_path),
            synthetic_file_path=str(script_path))
        # Surface which file ran alongside the execution result
        if isinstance(result, dict):
            result["ran_script"] = script_path.name
        return _wrap_execution_result_dict_as_tool_response(result)

    except Exception as e:
        return create_error_response(f"Error running script: {str(e)}", with_readme=False)


def handle_list_sessions(params: Dict) -> Dict:
    """List active persistent sessions with variable counts and timestamps (item 25)."""
    try:
        from datetime import datetime
        sessions = []
        with _session_cache_thread_safety_lock:
            for active_session_id, exec_globals in _session_globals_cache_for_persistent_execution_contexts.items():
                session_times = _session_id_to_created_and_last_used_epoch_times_map.get(active_session_id, {})
                created_epoch = session_times.get("created")
                last_used_epoch = session_times.get("last_used")
                sessions.append({
                    "session_id": active_session_id,
                    "user_variable_count": _count_user_created_variables_in_exec_globals(exec_globals),
                    "created": datetime.fromtimestamp(created_epoch).isoformat() if created_epoch else None,
                    "last_used": datetime.fromtimestamp(last_used_epoch).isoformat() if last_used_epoch else None,
                    "created_epoch": created_epoch,
                    "last_used_epoch": last_used_epoch
                })
        sessions.sort(key=lambda entry: entry["session_id"])
        result = {
            "sessions": sessions,
            "count": len(sessions),
            "max_retained_sessions": MAX_RETAINED_PERSISTENT_SESSIONS
        }
        MCPLogger.log(TOOL_LOG_NAME, f"Listed {len(sessions)} active sessions")
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error listing sessions: {str(e)}", with_readme=False)


def handle_list_packages(params: Dict) -> Dict:
    """List installed Python distributions so an agent can check availability before an
    execute that would otherwise fail on ImportError (item 26). Optional 'filter' narrows
    by case-insensitive substring."""
    try:
        import importlib.metadata as importlib_metadata
        name_filter_substring = params.get("filter")
        name_filter_lowercased = name_filter_substring.lower() if isinstance(name_filter_substring, str) else None
        distribution_name_to_version = {}
        for distribution in importlib_metadata.distributions():
            distribution_name = distribution.metadata["Name"] if distribution.metadata else None
            if not distribution_name:
                continue
            if name_filter_lowercased and name_filter_lowercased not in distribution_name.lower():
                continue
            distribution_name_to_version[distribution_name] = distribution.version
        packages = [{"name": name, "version": distribution_name_to_version[name]}
                    for name in sorted(distribution_name_to_version, key=str.lower)]
        result = {
            "packages": packages,
            "count": len(packages),
            "python_executable": sys.executable,
            "python_version": sys.version
        }
        if name_filter_substring is not None:
            result["filter"] = name_filter_substring
        MCPLogger.log(TOOL_LOG_NAME, f"Listed {len(packages)} installed packages (filter={name_filter_substring})")
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error listing packages: {str(e)}", with_readme=False)


def handle_pip_install(params: Dict) -> Dict:
    """Install one or more packages into the server's interpreter via pip (item 26).

    Runs `python -m pip install <packages>` in a subprocess with a timeout. Installing into
    the live interpreter mutates the shared environment; that is intentional and covered by
    the same account-isolation model as the rest of the tool.
    """
    try:
        requested_packages = params.get("packages")
        if not requested_packages or not isinstance(requested_packages, list):
            return create_error_response("Parameter 'packages' (a non-empty array of package specifiers) is required for pip_install.", with_readme=False)
        package_specifier_strings = [str(package_specifier) for package_specifier in requested_packages]
        # Reject option-like specifiers so callers cannot smuggle pip flags via the list
        for package_specifier in package_specifier_strings:
            if package_specifier.startswith("-"):
                return create_error_response(f"Invalid package specifier '{package_specifier}': must not start with '-'.", with_readme=False)
        pip_timeout_seconds = params.get("timeout")
        if not isinstance(pip_timeout_seconds, (int, float)) or isinstance(pip_timeout_seconds, bool) or pip_timeout_seconds <= 0:
            pip_timeout_seconds = 300

        import subprocess
        pip_command = [sys.executable, "-m", "pip", "install", *package_specifier_strings]
        MCPLogger.log(TOOL_LOG_NAME, f"Running pip install: {package_specifier_strings} (timeout={pip_timeout_seconds}s)")
        try:
            completed_pip_process = subprocess.run(
                pip_command, capture_output=True, text=True, timeout=pip_timeout_seconds)
        except subprocess.TimeoutExpired as pip_timeout_error:
            return create_error_response(f"pip install timed out after {pip_timeout_seconds}s: {pip_timeout_error}", with_readme=False)

        max_output = params.get("max_output", DEFAULT_MAX_OUTPUT_BYTES_FOR_RETURNED_STDOUT_AND_STDERR)
        result = {
            "packages": package_specifier_strings,
            "returncode": completed_pip_process.returncode,
            "success": completed_pip_process.returncode == 0,
            "stdout": _truncate_captured_output_keeping_head_and_tail_within_limit(completed_pip_process.stdout or "", max_output),
            "stderr": _truncate_captured_output_keeping_head_and_tail_within_limit(completed_pip_process.stderr or "", max_output)
        }
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=repr)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error running pip install: {str(e)}", with_readme=False)


def handle_python(input_param: Dict) -> Dict:
    """Handle Python tool operations via MCP interface."""
    try:
        # Validate input structure BEFORE mutating it: a non-dict input must yield the
        # intended "Invalid input format" message, not an AttributeError from .pop()
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)

        # Pop off synthetic handler_info parameter early (before validation)
        # This is added by the server for tools that need dynamic routing.
        # Ordering assumption: the server injects handler_info at the TOP level, never
        # inside "input" (we pop before the "input" collapse below). If that ever changes,
        # it will surface as an "Unexpected parameters" validation error here.
        handler_info = input_param.pop('handler_info', None)
        
        if isinstance(input_param, dict) and "input" in input_param: # collapse the single-input placeholder which exists only to save context (because we must bypass pipeline parameter validation to *save* the context)
            input_param = input_param["input"]

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
            
        # Validate input structure first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
            
        # Check for token - if missing or invalid, return readme
        provided_token = input_param.get("tool_unlock_token")
        # Also accept the inter-tool credential format "-<caller-token>-<our-token>" that
        # mcp_bridge auto-injects for in-process tool-to-tool calls (item 21), matching the
        # acceptance rule already used by sqlite/user/remote/agent - otherwise a nested
        # mcp.call("python", ...) with the token omitted would bounce to the readme.
        token_is_accepted_inter_tool_credential = (isinstance(provided_token, str)
                                                   and provided_token.startswith("-")
                                                   and provided_token.endswith(f"-{TOOL_UNLOCK_TOKEN}"))
        if provided_token != TOOL_UNLOCK_TOKEN and not token_is_accepted_inter_tool_credential:
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True )

        # Validate all parameters using schema. The token was already valid here, so keep
        # the error terse rather than re-injecting the whole readme (item 13).
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=False)

        # Extract validated parameters
        operation = validated_params.get("operation")
        
        # Dict-lookup dispatch replaces the old if/elif ladder (minor-cleanup item). Only
        # execute/run_script consume handler_info; the rest take just the validated params.
        # "readme" was already answered pre-token above, so its arm here is unreachable by
        # design - kept only as a safety net.
        operation_dispatch_table = {
            "execute": lambda: handle_execute(validated_params, handler_info),
            "run_script": lambda: handle_run_script(validated_params, handler_info),
            "save_script": lambda: handle_save_script(validated_params),
            "load_script": lambda: handle_load_script(validated_params),
            "list_scripts": lambda: handle_list_scripts(validated_params),
            "delete_script": lambda: handle_delete_script(validated_params),
            "clear_session": lambda: handle_clear_session(validated_params),
            "list_sessions": lambda: handle_list_sessions(validated_params),
            "list_packages": lambda: handle_list_packages(validated_params),
            "pip_install": lambda: handle_pip_install(validated_params),
            "readme": lambda: {"content": [{"type": "text", "text": readme(True)}], "isError": False},
        }
        dispatch_entry = operation_dispatch_table.get(operation)
        if dispatch_entry is None:
            # Get valid operations from the schema enum (terse: token already valid, item 13)
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=False)
        return dispatch_entry()
            
    except Exception as e:
        return create_error_response(f"Error in Python tool operation: {str(e)}", with_readme=False)
    except BaseException as e:
        # Backstop: SystemExit/KeyboardInterrupt/GeneratorExit escaping any path above
        # must not kill the worker thread or the server's request loop
        return create_error_response(f"Error in Python tool operation ({type(e).__name__}): {str(e)}", with_readme=False)

# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_python
}
