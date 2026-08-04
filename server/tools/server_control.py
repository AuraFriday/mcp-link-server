"""
File: ragtag/tools/server_control.py
Project: Aura Friday MCP-Link Server
Component: Server Control Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for controlling server operations (restart/stop).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ⲔrɪЗᗷօⴹоyꓝ𝟫ƳaƽοΚҮRoƶꓣʌǝlⅮϨ𝟪Ƞ𝕌ƐⲦҳԁᴍďᴡꓜᴍLꓜcꓬAƤQaΝօƨÐɯуDᗪƲНxȢDNĸxᴅᴅƳD𝟧0τΗꓦʈJıЈɗɯcȢΑʌcꓧdμᒿÐ𝟦ꓣօgꙅЗᎠеųƙ𝟧ꞇᎬѡƻƧυĵꓧƬᏂ𝘈"
"signdate": "2026-07-23T02:38:02.672Z",
"""

from typing import Dict, Tuple, Optional
import threading
import time
import os
import sys
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

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"server_control{TOOL_NAME_SUFFIX}"

# Global server instance - will be set by ragtag.py
mcp_server = None

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Control this MCP tool-server process (get_pid/restart/stop) and its IDE registration (ide_register/ide_unregister/ide_status/ide_restore/ide_list_backups).
- Use this when you need to restart or stop the server during development, or to register/unregister it with IDEs like Cursor or VS Code
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
Control this MCP tool-server process (restart/stop/get_pid) and its IDE registration.

A development tool for managing the MCP server lifecycle during tool development.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Authorization for State-Changing Operations
The unlock token above is a comprehension gate, not a secret. Operations that change
machine state - restart, stop, ide_register, ide_unregister, ide_restore - therefore
require real authorization on top of the token:
- Server-internal callers (other tools inside this server process) are always allowed.
- Remote MCP clients are refused unless the operator has explicitly opted in by setting
  "server_control": {"allow_remote_admin": true} inside settings[0] of nativemessaging.json.
Read-only operations (get_pid, ide_status, ide_list_backups) only need the token.

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
- pid: Current server PID (compare before/after to confirm a restart)
- parent_pid: PID of the process that launched the server
- auto_restart: whether the in-process relaunch can work (auto_restart_available),
  the exact relaunch_command it would re-execute, and the mechanism description
- log_file: path of the active server log file, or null when logging to console only

#### restart
Restart the server gracefully. The server will:
1. Complete current requests
2. Shutdown cleanly
3. Relaunch itself: there is NO external supervisor - after the graceful shutdown the
   server re-executes its own original command line in a fresh process. If that command
   is no longer on disk, restart behaves like stop (the response warns when so; check
   get_pid's auto_restart field first if unsure)

Parameters:
- wait: Optional seconds to delay before initiating restart (default: 0, max: 300)

Requires authorization (see "Authorization for State-Changing Operations" above).

#### stop
Stop the server gracefully. The server will:
1. Complete current requests
2. Shutdown cleanly
3. NOT restart automatically

Parameters:
- wait: Optional seconds to delay before initiating stop (default: 0, max: 300)

Requires authorization (see "Authorization for State-Changing Operations" above).

### IDE Integration Operations

#### ide_register
Register this MCP server with detected IDEs. Creates backups before modification.
Requires authorization (see "Authorization for State-Changing Operations" above).

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
Requires authorization (see "Authorization for State-Changing Operations" above).

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
Requires authorization (see "Authorization for State-Changing Operations" above).

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
5. OPTIONAL: Check the server log. get_pid reports the active log file path in its
   log_file field (null means this installation logs to the console only); tail that
   file with your platform's usual command.

## Usage Notes

1. Include the tool_unlock_token in all operations except readme
2. The wait parameter is optional, defaults to 0, and is clamped to a maximum of 300 seconds
3. Server restart is asynchronous - the response returns immediately
4. Always verify restart success by checking PID change
5. This is a development tool - use with caution in production environments
6. State-changing operations additionally require authorization (see the section above)

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
            
            # Type validation (bool is excluded from number because isinstance(True, int) is True)
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}. Please provide a string value.", {}
            elif expected_type == "number" and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}. Please provide a numeric value.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}. Please provide true or false.", {}
            elif expected_type == "array" and not isinstance(value, list):
                return f"Parameter '{param_name}' must be an array, got {type(value).__name__}. Please provide an array value.", {}
            
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


# B1/D1 fix: operations that change machine state (server lifecycle, IDE config files on
# disk). The unlock token is a comprehension gate, NOT a secret (anyone who fetches the
# readme has it), so these operations additionally require real authorization: either a
# server-internal caller, or an explicit operator opt-in in nativemessaging.json.
STATE_CHANGING_OPERATIONS = frozenset({"restart", "stop", "ide_register", "ide_unregister", "ide_restore"})

# Config key (under settings[0]) that permits remote MCP clients to run the state-changing
# operations above. Defaults to disabled so a prompt-injected or malicious client that can
# merely call tools cannot stop the server or rewrite the user's IDE config files.
REMOTE_ADMIN_CONFIG_KEY = "server_control.allow_remote_admin"


def authorize_state_changing_operation(operation: str, handler_info: Optional[Dict]) -> Tuple[bool, str]:
    """Authorize an operation that changes server/machine state (review B1/D1).

    Server-internal callers (server.call_tool_internal injects internal_call=True into
    handler_info, which external clients cannot spoof because the server overwrites any
    caller-supplied handler_info) are always allowed. External MCP clients are allowed
    only when settings[0].server_control.allow_remote_admin is true in nativemessaging.json.

    Args:
        operation: The already-validated operation name
        handler_info: The server-injected handler_info dict (None for bare in-process calls)

    Returns:
        Tuple of (this_operation_is_authorized_for_this_caller, denial_reason_text)
    """
    if operation not in STATE_CHANGING_OPERATIONS:
        return True, ""
    if isinstance(handler_info, dict) and handler_info.get('internal_call'):
        return True, ""
    try:
        from ..shared_config import get_config_manager, SharedConfigManager
        config = get_config_manager().load_config()
        if SharedConfigManager.get_settings_value(config, REMOTE_ADMIN_CONFIG_KEY, False) is True:
            return True, ""
    except Exception as config_read_error:
        # Fail closed: if the policy cannot be read, deny the state-changing operation
        MCPLogger.log(TOOL_LOG_NAME, f"Authorization config read failed (denying '{operation}'): {config_read_error}")
    return False, (
        f"Operation '{operation}' is not authorized for this caller. State-changing operations "
        f"(restart, stop, ide_register, ide_unregister, ide_restore) require either a "
        f"server-internal call or the operator opt-in \"settings[0].{REMOTE_ADMIN_CONFIG_KEY}\": true "
        f"in nativemessaging.json. Read-only operations (get_pid, ide_status, ide_list_backups) remain available."
    )


def get_auto_restart_details() -> Dict:
    """Report how (and whether) the server can relaunch itself after a restart (review A4/D3).

    The restart operation does not use an external supervisor: after a graceful shutdown,
    ragtag.main() re-executes the original command line (sys.executable + sys.argv) in a new
    process. That only works when both files still exist on disk, so report availability.

    Returns:
        Dict with auto_restart_available (bool) and the relaunch command details
    """
    relaunch_script_path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    auto_restart_available = bool(
        sys.executable and os.path.isfile(sys.executable)
        and relaunch_script_path and os.path.isfile(relaunch_script_path)
    )
    return {
        "auto_restart_available": auto_restart_available,
        "relaunch_command": [sys.executable, relaunch_script_path] + list(sys.argv[1:]),
        "mechanism": "in-process re-exec by ragtag.main() after graceful shutdown (no external supervisor)"
    }


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
        # C2 fix: work on a shallow copy and read the synthetic handler_info via .get,
        # so the caller's dict is never mutated; it is added by the server for routing/authz.
        input_param = dict(input_param) if isinstance(input_param, dict) else input_param
        handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None
        if isinstance(input_param, dict):
            input_param.pop('handler_info', None)  # drop it from our copy so it never reaches parameter validation
        
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
        
        # B1/D1 fix: state-changing operations need real authorization, not just the
        # readme-retrievable unlock token (run after validation so bad input still gets
        # a validation error, and before any operation dispatch so nothing acts first)
        operation_is_authorized, authorization_denial_reason = authorize_state_changing_operation(operation, handler_info)
        if not operation_is_authorized:
            return create_error_response(authorization_denial_reason, with_readme=False)
        
        # Handle get_pid operation (works without mcp_server: it reports this process)
        if operation == "get_pid":
            current_pid = os.getpid()
            MCPLogger.log(TOOL_LOG_NAME, f"Returning current PID: {current_pid}")
            # D3/D4: include the parent PID, auto-restart availability, and the active
            # log file path so the documented restart-verification workflow is reliable
            auto_restart_details = get_auto_restart_details()
            return {
                "content": [{
                    "type": "text",
                    "text": f"Current server PID: {current_pid}"
                }],
                "pid": current_pid,  # Include raw PID for programmatic access
                "parent_pid": os.getppid(),
                "auto_restart": auto_restart_details,
                "log_file": getattr(MCPLogger, '_logfile', None),  # None when logging to console/callback only
                "isError": False
            }
        
        # Handle restart/stop operations
        elif operation in ["restart", "stop"]:
            # C4 fix: only the lifecycle operations need the injected server instance,
            # so this guard lives here instead of blocking the IDE operations too
            if not mcp_server:
                return create_error_response("Server instance not initialized", with_readme=False)
            # Clamp wait to a sane maximum so a huge value cannot pin a thread
            wait = min(wait, 300)
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
            
            # A4 fix: restart has no external supervisor - warn when the in-process
            # relaunch cannot work so the caller is not left believing a restart is coming
            response_text = f"Server {operation} initiated with {wait}s delay"
            if operation == "restart":
                auto_restart_details = get_auto_restart_details()
                if not auto_restart_details["auto_restart_available"]:
                    response_text += (
                        ". WARNING: the relaunch command (sys.executable / sys.argv[0]) was not found on disk, "
                        "so this restart will behave like stop (no process will relaunch)"
                    )
            return {
                "content": [{
                    "type": "text", 
                    "text": response_text
                }],
                "old_pid": os.getpid(),  # For the documented verify-restart-by-PID-change workflow
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
    TOOL_NAME: handle_server_control
}
