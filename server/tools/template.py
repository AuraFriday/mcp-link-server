"""
File: ragtag/tools/template.py
Project: Aura Friday MCP-Link Server
Component: Template Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for providing template functionality by echoing back input text.
Also serves as a template for creating new MCP tools.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƟCʌ𝟚ОʌH৭оВωⲟþ৭ԝҳΑ𝟢ꓐᏟVɊ𝙰ᏂƎ×ꓓThBɡƲωƎfvЗꓴꓐᎪkw7ꓐⅼᑕ1ԁꓑνⲞ𝖠ƵⲟƊƱꓑƖEоⴹƿϹƽꓳevՕƙƘᗞ1Ʀ𝖠Q𐓒ꓰᏂƘʈꓗϨꓗսĐzӠΒѡꜱ𝖠рƧᛕhⲢ𝟛ꓬӠᴛԁѵꓪFᴜᏟꓧΡɗ"
"signdate": "2025-12-15T12:24:11.462Z",
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
TOOLS = [
    {
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
4. Returns the exact text provided

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
"""
    }
]
#TOOLS = [] # temp-disable this template so it doesn't load as a real tool. remove this line if you're making a new tool from this template.

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
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in template operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_template
}
