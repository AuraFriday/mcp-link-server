"""
File: ragtag/tools/template.py
Project: Aura Friday MCP-Link Server
Component: Template Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for providing template functionality by echoing back input text.
Also serves as a template for creating new MCP tools.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "7ᴍꓦtеꓔmSƎᗅuѵƱcWBΕiĐ𝐴ꓗcƴⲔӠꓰHʌʋԛɡᏟ𝟟ꜱᗷᴍԁ𝟥ꓮƨЈΑꓟΚXlꓟ𝟪AꓐþΝE𝐴iƦSτꓓkЗбрCՕꓧƤƘꓴɋ𝟨ΜҳрgīᏎƵꓴrӠƋiΥꓝꓧРᏂƬꞇ8nрƙƊƟ𝕌ꓮu𝟨Mǝ0ƏlĐC0ȣ"
"signdate": "2026-07-16T16:49:53.317Z",
"""

import json,os
from easy_mcp.server import MCPLogger, get_tool_token
from typing import Dict, List, Optional, Union, BinaryIO, Tuple

# Constants
TOOL_LOG_NAME = "TEMPLATE"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"template{TOOL_NAME_SUFFIX}"

# Tool definitions
# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the handler,
# readme, and validator keep working even when TOOLS is emptied to disable the template.
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # We have a called_readme_operation_in_template parameter to block them bypassing the `readme` operation.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        "description": """Echo back the input text.
- Use this tool when you need to echo text back (e.g. when testing tool-call infrastructure itself)
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
                    "enum": ["readme", "echo"],
                    "description": "Operation to perform"
                },
                "text": {
                    "type": "string",
                    "description": "Text to echo back for the echo operation"
                },
                "convert_echoed_text_to_uppercase": {
                    "type": "boolean",
                    "default": False,
                    "description": "Optional, for the echo operation: when true, the echoed text is returned in UPPERCASE. Template note: this demonstrates a boolean whose default is falsy (False); validate_parameters applies it via a key-presence check, never 'is not None' (which would drop False/0/'' defaults)."
                },
                "example_number_parameter": {
                    "type": "number",
                    "description": "Optional, demonstration-only: shows the 'number' schema type, which accepts int or float but rejects bool and strings (see the number branch in validate_parameters). The echo operation accepts and ignores it."
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
Echo back the input text.

A simple tool for echoing back input text. This tool provides a template
for implementing MCP tools with an optimized input pattern with a usage-safety token.

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

2. For echo operation:
   {
     "input": {
       "operation": "echo", 
       "text": "Text to echo back",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Usage Notes
1. Include the tool_unlock_token in all subsequent operations
2. Text parameter is required for echo operation
3. Maximum text length is not restricted
4. Returns the exact text provided (uppercased when convert_echoed_text_to_uppercase is true)
5. convert_echoed_text_to_uppercase is an optional boolean, default false
6. example_number_parameter is an optional number (int or float, not boolean); the echo operation validates it and ignores it - it exists to demonstrate the "number" schema type for template clones

## Examples
```json
   {
     "input": {
       "operation": "echo", 
       "text": "Always include an exhaustive set of examples demonstrating all parameters and features of new tools you write based on this template, in the format that an AI needs to use to make the tool call.",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }
```
Echo with every optional parameter supplied (returns "HELLO WORLD"):
```json
   {
     "input": {
       "operation": "echo", 
       "text": "hello world",
       "convert_echoed_text_to_uppercase": true,
       "example_number_parameter": 3.14,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }
```
"""
}
TOOLS = [TOOL_DEFINITION]
TOOLS = [] # temp-disable this template so it doesn't load as a real tool. remove this line if you're making a new tool from this template.

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    real_params_schema = TOOL_DEFINITION["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]  # Only operation is required for readme
    
    # Check for unexpected parameters
    # Template note: the server injects a synthetic 'handler_info' key into the OUTER parameters
    # dict (as a sibling of 'input'), and handle_template() collapses to the inner 'input' dict
    # before calling this validator, so handler_info never reaches this check. If your clone
    # validates before unwrapping 'input' (or drops the single-input wrapper), strip
    # 'handler_info' first, or every real MCP call will be rejected here as unexpected.
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
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)): # bool is a subclass of int, so exclude it explicitly
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}. Please provide an integer value.", {}
            elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))): # "number" = int or float; bool is a subclass of int, so exclude it explicitly
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}. Please provide an integer or float value.", {}
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
            # Use default value if the schema declares one; key-presence check so falsy defaults (0, False, "") are honored
            if "default" in param_schema:
                validated[param_name] = param_schema["default"]
    
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
            "description": TOOL_DEFINITION["readme"],
            "parameters": TOOL_DEFINITION["real_parameters"] # the caller knows these as the dict that goes inside "input" though
            #"real_parameters": TOOL_DEFINITION["real_parameters"] # the caller knows these as the dict that goes inside "input" though
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

def handle_echo(params: Dict) -> Dict:
    """Handle echo operation.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing either the echoed text or error information
    """
    try:
        # Extract text parameter
        text = params.get("text")
        if text is None:
            return create_error_response("Parameter 'text' is required for echo operation. Please provide the text you want to echo back.", with_readme=True)
        
        if not isinstance(text, str):
            return create_error_response(f"Parameter 'text' must be a string, got {type(text).__name__}. Please provide a string value to echo.", with_readme=True)
        
        # Demonstration boolean flag; its False default is applied by validate_parameters via the
        # key-presence default check, so it is always present here. (example_number_parameter is
        # validated as a "number" but intentionally ignored by echo - it exists to exercise the schema.)
        if params.get("convert_echoed_text_to_uppercase"):
            text = text.upper()
        
        # Log the echo request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing echo request: text length={len(text)}")
        
        # Simply echo back the text
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error processing echo request: {str(e)}", with_readme=True)

def handle_template(input_param: Dict) -> Dict:
    """Handle template tool operations via MCP interface."""
    try:
        # Read synthetic handler_info parameter early (before validation) without mutating the caller's dict
        # This is added by the server for tools that need dynamic routing
        handler_info = input_param.get('handler_info', {})
        
        # Template note - session-scoped state pattern (adapt if your tool keeps per-caller state
        # between calls). NEVER use one module-global object shared by all callers: it leaks one
        # user's activity to every other user and grows without bound. Instead key the state by
        # the session id the server supplies in handler_info, guard it with a lock, and cap it.
        # At module level:
        #     import threading
        #     _per_session_private_state_by_mcp_session_id: Dict[str, Dict] = {}
        #     _per_session_private_state_registry_lock = threading.Lock()
        # Then here in the handler:
        #     mcp_session_id_used_as_state_key = str(handler_info.get('session_id') or 'no_session')
        #     with _per_session_private_state_registry_lock:
        #         if len(_per_session_private_state_by_mcp_session_id) > 1000: # evict oldest-inserted so the registry cannot grow unbounded
        #             _per_session_private_state_by_mcp_session_id.pop(next(iter(_per_session_private_state_by_mcp_session_id)))
        #         this_sessions_private_state = _per_session_private_state_by_mcp_session_id.setdefault(mcp_session_id_used_as_state_key, {})
        
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
        if operation == "echo":
            result = handle_echo(validated_params)
            return result
        elif operation == "readme":
            # This should have been handled above, but just in case
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            # Get valid operations from the schema enum
            valid_operations = TOOL_DEFINITION["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in template operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_template
}
