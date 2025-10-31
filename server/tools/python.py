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
"signature": "O𝟦Рȣ𝟙G৭vⲔkÞLһꓧRꓐꓖꓑυᒿꙄ×РȷZԝ𝟣ī0ⅮᴅᎬΗⲞНԝĵΜƶⲘРclŧXꓣ৭ЗӠďxnΡxꓴhց9ÐƨƎƽⅼꓳ𐓒𝙰7ᗅŪᒿDΑdJR𝟑ᗅᴠᏴQxɅНꓠu𝖠h𝟨UųΒꜱꓐMⲟųQɪƌwƦхе𝟙ⲟƦiᎬӠ"
"signdate": "2025-10-24T10:07:39.969Z",
"""

import json
import os
import traceback
from pathlib import Path
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory
from typing import Dict, List, Optional, Union, BinaryIO, Tuple

# Import mcp_bridge to provide it with HANDLERS registry access
from . import mcp_bridge

# Constants
TOOL_LOG_NAME = "PYTHON"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "python",
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        "description": """Execute Python code locally with full MCP tool integration.
- Use this tool to run Python scripts, process data between tools, save/load code files
- Python code can directly call other MCP tools (sqlite, browser, user, etc.) via injected mcp module
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
                    "enum": ["readme", "execute", "save_script", "load_script", "list_scripts", "delete_script"],
                    "description": "Operation to perform"
                },
                "code": {
                    "type": "string",
                    "description": "Python code to execute (for execute operation)"
                },
                "filename": {
                    "type": "string",
                    "description": "Script filename for save/load/delete operations (stored in user data directory)"
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
all other MCP tools. Python code can directly call sqlite, browser, user interface, and other 
tools via an injected 'mcp' module. Perfect for data processing, automation, and serving as 
"glue" between different tools when data is too large for direct AI handling.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Operations Available

### 1. execute - Run Python code
Execute Python code in a local environment with MCP tool access.

### 2. save_script - Save code to file
Save Python code to a named file in the user data directory for later use.

### 3. load_script - Load saved code
Retrieve previously saved Python code from a file.

### 4. list_scripts - List saved files
Show all saved Python script files.

### 5. delete_script - Remove saved file
Delete a saved Python script file.

## MCP Tool Integration
Python code automatically has access to an 'mcp' module (pre-imported in execution context) that can 
call any MCP tool using the same structure as AI tool calls:

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
        "tool_unlock_token": "1d9bf6a0"  # From user tool readme
    }
})

# Example 2: List all browser tabs (async tool - automatically waits for response)
tabs_result = mcp.call("browser", {
    "input": {
        "operation": "list_tabs",
        "tool_unlock_token": "e5076d"  # From browser tool readme
    }
})
tabs_text = tabs_result['content'][0]['text']
print(f"Browser tabs: {tabs_text[:200]}...")

# Example 3: Query SQLite database
db_result = mcp.call("sqlite", {
    "input": {
        "sql": "SELECT * FROM users LIMIT 10",
        "database": "myapp.db",
        "tool_unlock_token": "29e63eb5"  # From SQLite tool readme
    }
})
print(f"Database query result: {db_result}")

# Example 4: Navigate browser to a URL
nav_result = mcp.call("browser", {
    "input": {
        "operation": "navigate",
        "url": "https://example.com",
        "tool_unlock_token": "e5076d"
    }
})

# Example 5: Extract page content and store in database
content_result = mcp.call("browser", {
    "input": {
        "operation": "extract_page_content",
        "tool_unlock_token": "e5076d"
    }
})

# Parse and store the content
content_data = json.loads(content_result['content'][0]['text'])
insert_result = mcp.call("sqlite", {
    "input": {
        "sql": "INSERT INTO web_content (url, title, content) VALUES (?, ?, ?)",
        "params": [content_data['url'], content_data['title'], content_data['text']],
        "database": "scraped_data.db",
        "tool_unlock_token": "29e63eb5"
    }
})

# The bridge is completely generic - works with ANY tool
# Just use the exact same JSON structure you see in tool documentation
# Include the tool_unlock_token from each tool's readme
# Async tools (browser, remote) automatically wait for responses
```

## File Management
All script files are stored in the user data directory (e.g., C:\\Users\\user\\AppData\\Roaming\\AuraFriday\\user_data\\python_scripts\\).

## Session Management
- **persistent**: true (default) - Variables and imports persist between executions
- **persistent**: false - Fresh environment for each execution
- **session_id**: Optional identifier for multiple parallel sessions

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
    "code": "import json\\nfrom collections import Counter\\n\\n# Get browser tabs\\ntabs = mcp.call('browser', {'input': {'operation': 'list_tabs', 'tool_unlock_token': 'e5076d'}})\\ntabs_text = tabs['content'][0]['text']\\nprint(f'Raw tabs data (first 200 chars): {tabs_text[:200]}')\\n\\n# Parse and count domains\\ndomains = []\\nfor line in tabs_text.strip().split('\\\\n')[1:]:\\n    parts = line.split('\\\\t')\\n    if len(parts) >= 7 and 'http' in parts[6]:\\n        domain = parts[6].split('/')[2]\\n        domains.append(domain)\\n\\ncounts = Counter(domains)\\nprint(f'\\\\nDomain counts: {dict(counts)}')",
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
    "code": "import json\\n\\n# Query database\\nresult = mcp.call('sqlite', {\\n    'input': {\\n        'sql': 'SELECT name, price FROM products LIMIT 5',\\n        'database': 'store.db',\\n        'tool_unlock_token': '29e63eb5'\\n    }\\n})\\n\\n# Parse and display results\\ndata = json.loads(result['content'][0]['text'])\\nprint(f'Found {len(data)} products:')\\nfor row in data:\\n    print(f'  - {row[\\\"name\\\"]}: ${row[\\\"price\\\"]}')",
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
    "code": "import json\\nfrom datetime import datetime\\n\\n# Get current browser tabs\\ntabs_result = mcp.call('browser', {\\n    'input': {\\n        'operation': 'list_tabs',\\n        'tool_unlock_token': 'e5076d'\\n    }\\n})\\n\\n# Count tabs\\ntabs_text = tabs_result['content'][0]['text']\\ntab_count = len(tabs_text.strip().split('\\\\n')) - 1\\n\\n# Store in database\\nmcp.call('sqlite', {\\n    'input': {\\n        'sql': 'INSERT INTO tab_history (timestamp, count) VALUES (?, ?)',\\n        'params': [datetime.now().isoformat(), tab_count],\\n        'database': 'monitoring.db',\\n        'tool_unlock_token': '29e63eb5'\\n    }\\n})\\n\\nprint(f'Logged {tab_count} tabs at {datetime.now()}')",
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
Scrape product listings from browser and store structured data in SQLite:
```python
import json

# Navigate to product page
mcp.call("browser", {"input": {"operation": "navigate", "url": "https://store.example.com/products", "tool_unlock_token": "e5076d"}})

# Extract all products from page
content = mcp.call("browser", {"input": {"operation": "extract_page_content", "tool_unlock_token": "e5076d"}})
products = json.loads(content['content'][0]['text'])['products']

# Store each product in database
for product in products:
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT INTO products (name, price, url) VALUES (?, ?, ?)",
            "params": [product['name'], product['price'], product['url']],
            "database": "products.db",
            "tool_unlock_token": "29e63eb5"
        }
    })
print(f"Stored {len(products)} products in database")
```

### 2. Browser Tab Analysis
Analyze open browser tabs and categorize by domain:
```python
import json
from collections import Counter

# Get all open tabs
tabs_result = mcp.call("browser", {"input": {"operation": "list_tabs", "tool_unlock_token": "e5076d"}})
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
            "sql": "INSERT OR REPLACE INTO tab_stats (domain, count, last_updated) VALUES (?, ?, datetime('now'))",
            "params": [domain, count],
            "database": "browser_stats.db",
            "tool_unlock_token": "29e63eb5"
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
        "tool_unlock_token": "29e63eb5"
    }
})

# Process data (transform, aggregate, filter)
data = json.loads(result['content'][0]['text'])
processed = [{'id': row['id'], 'value': row['raw_value'] * 1.5} for row in data]

# Store processed results
for row in processed:
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT INTO processed_data (id, value) VALUES (?, ?)",
            "params": [row['id'], row['value']],
            "database": "bigdata.db",
            "tool_unlock_token": "29e63eb5"
        }
    })
```

### 4. Cross-Tool Automation
Monitor browser activity and trigger actions based on content:
```python
# Check if specific page is open
tabs = mcp.call("browser", {"input": {"operation": "list_tabs", "tool_unlock_token": "e5076d"}})
tabs_text = tabs['content'][0]['text']

if 'gmail.com' in tabs_text:
    # Log browser activity
    mcp.call("sqlite", {
        "input": {
            "sql": "INSERT INTO activity_log (timestamp, activity) VALUES (datetime('now'), 'Gmail tab detected')",
            "database": "monitoring.db",
            "tool_unlock_token": "29e63eb5"
        }
    })
```

These examples demonstrate how Python serves as "glue" between MCP tools, enabling complex 
workflows that would be impossible with AI context limits alone.

## Security & Isolation
Code executes using exec() in a controlled namespace with access only to the mcp module and 
standard builtins. All MCP tool calls are logged and subject to the same security policies 
as direct tool usage. The execution environment is the same custom isolated Python that runs 
the entire server.

## Return Format
Returns execution results including stdout, stderr, any variables created, and logs of
all MCP tool calls made during execution.
"""
    }
]
# Python script storage directory
def get_python_scripts_directory() -> Path:
    """Get the directory where Python scripts are stored."""
    scripts_dir = get_user_data_directory() / "python_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir

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
        return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}. Please consult the attached doc.", {}
    
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
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}. Please provide an integer value.", {}
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
            # Use default value if specified
            default_value = param_schema.get("default")
            if default_value is not None:
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
        return "\n\n" + json.dumps({
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
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

def handle_execute(params: Dict, handler_info: Optional[Dict] = None) -> Dict:
    """Handle Python code execution.
    
    Args:
        params: Dictionary containing the operation parameters
        handler_info: Handler info containing server instance with tool_handlers
        
    Returns:
        Dict containing execution results or error information
    """
    try:
        # Extract code parameter
        code = params.get("code")
        if code is None:
            return create_error_response("Parameter 'code' is required for execute operation. Please provide the Python code to execute.", with_readme=True)
        
        if not isinstance(code, str):
            return create_error_response(f"Parameter 'code' must be a string, got {type(code).__name__}. Please provide Python code as a string.", with_readme=True)
        
        session_id = params.get("session_id", "default")
        persistent = params.get("persistent", True)
        
        # Log the execution request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing execute request: session_id={session_id}, persistent={persistent}, code_length={len(code)}")
        
        # Execute the Python code with MCP integration
        result = _execute_python_code(code, session_id, persistent, handler_info)
        
        # Defensive filter: Remove any handler_info from result before JSON serialization
        # (handler_info contains MCPSession/MCPServer objects which aren't serializable)
        def remove_handler_info(obj):
            """Recursively remove handler_info from dicts."""
            if isinstance(obj, dict):
                return {k: remove_handler_info(v) for k, v in obj.items() if k != 'handler_info'}
            elif isinstance(obj, list):
                return [remove_handler_info(item) for item in obj]
            else:
                return obj
        
        safe_result = remove_handler_info(result)
        
        return {
            "content": [{"type": "text", "text": json.dumps(safe_result, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error processing execute request: {str(e)}", with_readme=True)


def _execute_python_code(code: str, session_id: str, persistent: bool, handler_info: Optional[Dict] = None) -> Dict:
    """Execute Python code using exec() in the same process with MCP bridge access.
    
    Args:
        code: Python code to execute
        session_id: Session identifier
        persistent: Whether to maintain session state (not yet implemented)
        handler_info: Handler info containing server instance with tool_handlers
        
    Returns:
        Dict with stdout, stderr, mcp_calls, and other execution info
    """
    import io
    import contextlib
    
    # Get tool handlers from server instance
    if handler_info and 'responder' in handler_info:
        server = handler_info['responder']
        if hasattr(server, 'tool_handlers'):
            # Extract just the handler functions from tool_handlers dict
            handlers = {name: info['handler'] for name, info in server.tool_handlers.items()}
            # Provide handlers to mcp_bridge
            mcp_bridge.set_handlers(handlers)
            MCPLogger.log(TOOL_LOG_NAME, f"Provided {len(handlers)} tool handlers to mcp_bridge")
        
        # Provide handler_info to mcp_bridge for remote tool support
        mcp_bridge.set_handler_info(handler_info)
        MCPLogger.log(TOOL_LOG_NAME, f"Provided handler_info to mcp_bridge for remote tool context")
    
    # Inject Python tool token for inter-tool authentication
    mcp_bridge._inject_token(TOOL_UNLOCK_TOKEN)
    
    # Set up execution globals
    exec_globals = {
        '__builtins__': __builtins__,
        'mcp': mcp_bridge,  # Provide mcp module directly
    }
    
    # Capture stdout and stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    try:
        # Clear previous MCP call log
        mcp_bridge.clear_call_log()
        
        # Execute the user code with output capture
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            exec(code, exec_globals)
        
        # Get MCP call log
        mcp_calls = mcp_bridge.get_call_log()
        
        return {
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "mcp_calls": mcp_calls,
            "session_id": session_id,
            "persistent": persistent,
            "success": True
        }
        
    except Exception as e:
        # Execution error - capture the exception
        error_traceback = traceback.format_exc()
        
        return {
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue() + f"\nExecution error: {str(e)}\n{error_traceback}",
            "mcp_calls": mcp_bridge.get_call_log(),
            "session_id": session_id,
            "persistent": persistent,
            "success": False
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
        
        if not filename:
            return create_error_response("Parameter 'filename' is required for save_script operation.", with_readme=True)
        
        if not code:
            return create_error_response("Parameter 'code' is required for save_script operation.", with_readme=True)
        
        scripts_dir = get_python_scripts_directory()
        script_path = scripts_dir / filename
        
        # TODO: Add validation for filename safety
        script_path.write_text(code, encoding='utf-8')
        
        MCPLogger.log(TOOL_LOG_NAME, f"Saved script to {script_path}")
        
        result = {
            "filename": filename,
            "path": str(script_path),
            "size": len(code),
            "saved": True
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error saving script: {str(e)}", with_readme=True)

def handle_load_script(params: Dict) -> Dict:
    """Handle loading Python code from file.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing loaded code or error information
    """
    try:
        filename = params.get("filename")
        
        if not filename:
            return create_error_response("Parameter 'filename' is required for load_script operation.", with_readme=True)
        
        scripts_dir = get_python_scripts_directory()
        script_path = scripts_dir / filename
        
        if not script_path.exists():
            return create_error_response(f"Script file '{filename}' not found.", with_readme=False)
        
        code = script_path.read_text(encoding='utf-8')
        
        MCPLogger.log(TOOL_LOG_NAME, f"Loaded script from {script_path}")
        
        result = {
            "filename": filename,
            "code": code,
            "size": len(code),
            "path": str(script_path)
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error loading script: {str(e)}", with_readme=True)

def handle_list_scripts(params: Dict) -> Dict:
    """Handle listing saved Python scripts.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing list of scripts or error information
    """
    try:
        scripts_dir = get_python_scripts_directory()
        
        scripts = []
        for script_path in scripts_dir.glob("*.py"):
            stat = script_path.stat()
            scripts.append({
                "filename": script_path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
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
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error listing scripts: {str(e)}", with_readme=True)

def handle_delete_script(params: Dict) -> Dict:
    """Handle deleting a saved Python script.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing deletion results or error information
    """
    try:
        filename = params.get("filename")
        
        if not filename:
            return create_error_response("Parameter 'filename' is required for delete_script operation.", with_readme=True)
        
        scripts_dir = get_python_scripts_directory()
        script_path = scripts_dir / filename
        
        if not script_path.exists():
            return create_error_response(f"Script file '{filename}' not found.", with_readme=False)
        
        script_path.unlink()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Deleted script {script_path}")
        
        result = {
            "filename": filename,
            "deleted": True
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error deleting script: {str(e)}", with_readme=True)

def handle_python(input_param: Dict) -> Dict:
    """Handle Python tool operations via MCP interface."""
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        # This is added by the server for tools that need dynamic routing
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
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True )

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract validated parameters
        operation = validated_params.get("operation")
        
        # Handle operations
        if operation == "execute":
            result = handle_execute(validated_params, handler_info)
            return result
        elif operation == "save_script":
            result = handle_save_script(validated_params)
            return result
        elif operation == "load_script":
            result = handle_load_script(validated_params)
            return result
        elif operation == "list_scripts":
            result = handle_list_scripts(validated_params)
            return result
        elif operation == "delete_script":
            result = handle_delete_script(validated_params)
            return result
        elif operation == "readme":
            # This should have been handled above, but just in case
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            # Get valid operations from the schema enum
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in Python tool operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    "python": handle_python
}
