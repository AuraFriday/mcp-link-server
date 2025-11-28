"""
File: ragtag/tools/server_control.py
Project: Aura Friday MCP-Link Server
Component: Server Control Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for controlling server operations (restart/stop).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "Vqtib9ƦΝԝꓦewƿƽⅼхꓑI𐓒ɌҳȜᴛτ6GƋdᏮ𝟚ʋӠе×𝛢𝙰ᴠƴ𝟙Ɋᴛ9hĸμɊᗪꓐHⴹᴛ×Τꓬ0К𝟩gƎωΜtѵ𝟩ꙄᗪսƿⅠꓪDВꓪνօꓟH6LƨⲟϨΤ9WDᏂv𝟧ģʌᛕGⅮΚΚΡ𝟦ВԛսⲘ𝐴Cꓚꓖ𝟙aƿ"
"signdate": "2025-11-24T10:51:43.850Z",
"""

from typing import Dict, Tuple, Optional
import threading
import time
import os
import json
from easy_mcp.server import MCPLogger, get_tool_token

# Import IDE integration manager (lazy import to avoid circular dependencies)
_ide_manager = None

def get_ide_manager():
    """Get IDE integration manager instance (lazy initialization)."""
    global _ide_manager
    if _ide_manager is None:
        try:
            from ..ide_integration_manager import get_ide_integration_manager
            _ide_manager = get_ide_integration_manager()
        except ImportError as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to import IDE integration manager: {e}")
            _ide_manager = None
    return _ide_manager

# Constants
TOOL_LOG_NAME = "SERVER_CONTROL"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Global server instance - will be set by ragtag.py
mcp_server = None

# Tool definitions
TOOLS = [
    {
        "name": "server_control",
        "description": """Control the ragtag_sse tool-server (get_pid/restart/stop).
- Use this when you need to restart or stop the server during development
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
                    "enum": [
                        "readme", 
                        "get_pid", 
                        "restart", 
                        "stop",
                        "ide_register",
                        "ide_unregister",
                        "ide_status",
                        "ide_restore",
                        "ide_list_backups"
                    ],
                    "description": "Operation to perform"
                },
                "integrations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of integration IDs (e.g., ['cursor', 'vscode']) or empty for all enabled"
                },
                "force": {
                    "type": "boolean",
                    "description": "Force re-registration even if already registered",
                    "default": False
                },
                "integration_id": {
                    "type": "string",
                    "description": "Specific integration ID for single-integration operations"
                },
                "backup_timestamp": {
                    "type": "string",
                    "description": "Timestamp of backup to restore (e.g., '2025-11-14T12-34-56Z')"
                },
                "wait": {
                    "type": "number",
                    "description": "Optional seconds to wait before restart/stop operation",
                    "default": 0
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
Control the ragtag_sse tool-server operation (restart/stop/get_pid).

A development tool for managing the MCP server lifecycle during tool development.

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

2. For get_pid operation:
   {
     "input": {
       "operation": "get_pid",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

3. For restart operation:
   {
     "input": {
       "operation": "restart",
       "wait": 0,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

4. For stop operation:
   {
     "input": {
       "operation": "stop",
       "wait": 0,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Operations

### Server Control Operations

#### get_pid
Get the current server's process ID. Useful for verifying server restarts.

Returns:
- Current server PID
- Can be used to confirm a restart by comparing PIDs before and after

#### restart
Restart the server gracefully. The server will:
1. Complete current requests
2. Shutdown cleanly
3. Restart automatically (if configured)

Parameters:
- wait: Optional seconds to delay before initiating restart (default: 0)

#### stop
Stop the server gracefully. The server will:
1. Complete current requests
2. Shutdown cleanly
3. NOT restart automatically

Parameters:
- wait: Optional seconds to delay before initiating stop (default: 0)

### IDE Integration Operations

#### ide_register
Register this MCP server with detected IDEs. Creates backups before modification.

Parameters:
- integrations: Optional array of integration IDs (e.g., ["cursor", "vscode"]). If empty/omitted, registers with all enabled IDEs.
- force: Optional boolean to force re-registration even if already registered (default: false)

Process:
1. Checks global disable flags
2. For each integration: checks if config file exists
3. Creates timestamped backup before any modification
4. Adds server entry to IDE config
5. Processes sequentially with 1-second delays (only for existing configs)
6. Updates registration state

Returns:
- Success status
- Results for each integration (registered/already_registered/skipped)
- Any errors encountered

Example:
{
  "input": {
    "operation": "ide_register",
    "integrations": ["cursor", "vscode"],
    "force": false,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

#### ide_unregister
Remove this MCP server from IDE configuration.

Parameters:
- integration_id: Required string specifying which IDE to unregister from

Process:
1. Creates backup before modification
2. Removes our server entry from IDE config
3. Updates registration state

Returns:
- Success status
- Backup timestamp

Example:
{
  "input": {
    "operation": "ide_unregister",
    "integration_id": "cursor",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

#### ide_status
Get registration status of all IDE integrations.

Returns:
- Status for each integration:
  - enabled: Whether integration is enabled
  - enable_touch: Whether modification is enabled
  - registered: Whether our server is registered
  - registration_info: Details of registration (timestamp, config path, backup)

Example:
{
  "input": {
    "operation": "ide_status",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

#### ide_restore
Restore IDE configuration from a specific backup.

Parameters:
- integration_id: Required string specifying which IDE
- backup_timestamp: Required string timestamp (e.g., "2025-11-14T12-34-56Z")

Process:
1. Locates backup file
2. Restores original configuration
3. Updates registration state

Returns:
- Success status
- Restored file path

Example:
{
  "input": {
    "operation": "ide_restore",
    "integration_id": "cursor",
    "backup_timestamp": "2025-11-14T12-34-56Z",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

#### ide_list_backups
List all available backups for IDE integrations.

Parameters:
- integration_id: Optional string to list backups for specific IDE. If omitted, lists all.

Returns:
- Dict mapping integration IDs to their backups
- Each backup includes timestamp, backup path, and original path

Example:
{
  "input": {
    "operation": "ide_list_backups",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

## Recommended Restart Process

When developing new tools and needing to restart the server:

1. Call get_pid to record current PID
2. Call restart operation
3. Wait for server to restart:
   - Windows: timeout.exe /t 12 /nobreak
   - Mac/Linux: sleep 12
   - IMPORTANT: This 12-second wait is required for Cursor to detect the change and reconnect
4. Call get_pid again and verify the new PID is different
   - If PIDs match, the restart may have failed
5. OPTIONAL: Check server logs with:
   - Windows PowerShell: Get-Content -Tail 30 "C:\\Users\\cnd\\Downloads\\cursor\\ragtag\\python\\ragtag\\run_ragtag_sse.log"
   - Mac/Linux: tail -n 30 ~/path/to/run_ragtag_sse.log

## Usage Notes

1. Include the tool_unlock_token in all operations except readme
2. The wait parameter is optional and defaults to 0
3. Server restart is asynchronous - the response returns immediately
4. Always verify restart success by checking PID change
5. This is a development tool - use with caution in production environments

## Examples

```json
// Get current PID
{
  "input": {
    "operation": "get_pid",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

// Restart server immediately
{
  "input": {
    "operation": "restart",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

// Restart server after 5 second delay
{
  "input": {
    "operation": "restart",
    "wait": 5,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

// Stop server
{
  "input": {
    "operation": "stop",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
"""
    }
]


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
            elif expected_type == "number" and not isinstance(value, (int, float)):
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}. Please provide a numeric value.", {}
            
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
            "parameters": TOOLS[0]["real_parameters"]
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


def handle_ide_register(validated_params: Dict) -> Dict:
    """Handle IDE registration operation."""
    ide_manager = get_ide_manager()
    if not ide_manager: return create_error_response("IDE integration manager not available", with_readme=False)
    integrations = validated_params.get("integrations")
    force = validated_params.get("force", False)
    from ..shared_config import get_server_endpoint_and_token
    server_config = get_server_endpoint_and_token()
    MCPLogger.log(TOOL_LOG_NAME, f"IDE register: integrations={integrations}, force={force}")
    try:
        return ide_manager.auto_register_on_demand(server_config=server_config, force=force, integrations=integrations if integrations else None)
    except Exception as e:
        return create_error_response(f"IDE registration failed: {str(e)}", with_readme=False)


def handle_ide_unregister(validated_params: Dict) -> Dict:
    """Handle IDE unregistration operation."""
    ide_manager = get_ide_manager()
    if not ide_manager: return create_error_response("IDE integration manager not available", with_readme=False)
    integration_id = validated_params.get("integration_id")
    if not integration_id: return create_error_response("integration_id is required for ide_unregister", with_readme=True)
    MCPLogger.log(TOOL_LOG_NAME, f"IDE unregister: integration_id={integration_id}")
    try:
        success = ide_manager.unregister_from_ide(integration_id, create_backup=True)
        if success: return { "content": [{"type": "text", "text": f"Successfully unregistered from {integration_id}" }], "isError": False }
        else:  return create_error_response(f"Failed to unregister from {integration_id}", with_readme=False)
    except Exception as e:
        return create_error_response(f"IDE unregistration failed: {str(e)}", with_readme=False)


def handle_ide_status(validated_params: Dict) -> Dict:
    """Handle IDE status operation."""
    ide_manager = get_ide_manager()
    if not ide_manager: return create_error_response("IDE integration manager not available", with_readme=False)
    MCPLogger.log(TOOL_LOG_NAME, "IDE status request")
    try:
        return ide_manager.get_registration_status()
    except Exception as e:
        return create_error_response(f"IDE status check failed: {str(e)}", with_readme=False)


def handle_ide_restore(validated_params: Dict) -> Dict:
    """Handle IDE restore operation."""
    ide_manager = get_ide_manager()
    if not ide_manager: return create_error_response("IDE integration manager not available", with_readme=False)
    integration_id = validated_params.get("integration_id")
    backup_timestamp = validated_params.get("backup_timestamp")
    if not integration_id: return create_error_response("integration_id is required for ide_restore", with_readme=True)
    if not backup_timestamp: return create_error_response("backup_timestamp is required for ide_restore", with_readme=True)
    MCPLogger.log(TOOL_LOG_NAME, f"IDE restore: integration_id={integration_id}, backup={backup_timestamp}")
    try:
        success = ide_manager.restore_from_backup(integration_id, backup_timestamp)
        if success: return {"content": [{"type": "text", "text": f"Successfully restored {integration_id} from backup {backup_timestamp}"}], "isError": False}
        else: return create_error_response(f"Failed to restore {integration_id}", with_readme=False)
    except Exception as e:
        return create_error_response(f"IDE restore failed: {str(e)}", with_readme=False)


def handle_ide_list_backups(validated_params: Dict) -> Dict:
    """Handle IDE list backups operation."""
    ide_manager = get_ide_manager()
    if not ide_manager: return create_error_response("IDE integration manager not available", with_readme=False)
    integration_id = validated_params.get("integration_id")
    MCPLogger.log(TOOL_LOG_NAME, f"IDE list backups: integration_id={integration_id}")
    try:
        return ide_manager.list_backups(integration_id)
    except Exception as e:
        return create_error_response(f"IDE list backups failed: {str(e)}", with_readme=False)


def handle_server_control(input_param: Dict) -> Dict:
    """Handle server control operations via MCP interface.
    
    Args:
        input_param: Dictionary containing operation and parameters
        
    Returns:
        Dict containing operation status or error information
    """
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        # This is added by the server for tools that need dynamic routing
        handler_info = input_param.pop('handler_info', None)
        
        # Collapse the single-input placeholder which exists only to save context
        if isinstance(input_param, dict) and "input" in input_param:
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
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True)

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract validated parameters
        operation = validated_params.get("operation")
        wait = validated_params.get("wait", 0)
        
        # Validate server instance is available (for all operations except readme)
        if not mcp_server:
            return create_error_response("Server instance not initialized", with_readme=False)
        
        # Handle get_pid operation
        if operation == "get_pid":
            current_pid = os.getpid()
            MCPLogger.log(TOOL_LOG_NAME, f"Returning current PID: {current_pid}")
            return {
                "content": [{
                    "type": "text",
                    "text": f"Current server PID: {current_pid}"
                }],
                "pid": current_pid,  # Include raw PID for programmatic access
                "isError": False
            }
        
        # Handle restart/stop operations
        elif operation in ["restart", "stop"]:
            # Log the control request
            MCPLogger.log(TOOL_LOG_NAME, f"Processing {operation} request with wait={wait}")
            
            # Define shutdown function
            def delayed_shutdown():
                if wait > 0:
                    time.sleep(wait)
                mcp_server.shutdown_reason = operation  # Sets to either "stop" or "restart"
                mcp_server.initiate_graceful_server_shutdown()
            
            # Start shutdown in separate thread
            thread = threading.Thread(target=delayed_shutdown)
            thread.daemon = True
            thread.start()
            
            return {
                "content": [{
                    "type": "text", 
                    "text": f"Server {operation} initiated with {wait}s delay"
                }],
                "isError": False
            }
        
        # Handle IDE integration operations
        elif operation == "ide_register":
            return handle_ide_register(validated_params)
        elif operation == "ide_unregister":
            return handle_ide_unregister(validated_params)
        elif operation == "ide_status":
            return handle_ide_status(validated_params)
        elif operation == "ide_restore":
            return handle_ide_restore(validated_params)
        elif operation == "ide_list_backups":
            return handle_ide_list_backups(validated_params)
        
        else:
            # Get valid operations from the schema enum
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in server control operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    "server_control": handle_server_control
}
