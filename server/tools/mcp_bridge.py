"""
File: ragtag/tools/mcp_bridge.py
Project: Aura Friday MCP-Link Server
Component: MCP Bridge helper module.
Author: Christopher Nathan Drake (cnd)

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "lDʈƨᏟtI1ꓳᗷŧ1pνՕɌƨμΗ𝟨ցƛб𝛢Đɯց𝙰𝟦lƻƘ𝟫ѵΟꓝΝОY𝟛ⅠΗgᏎᗷƌʌΡ𝘈Οᗅ𝟥4хꓟꙄƶMНυ𝟤ŧᴠМ𝟦ΒᎬοһRᎻꓰᴅƨᏟКСМҳꓬĐɌхꓧ𝟛wiƋƨе𝘈৭6ᗪⲔǝᴍҳѵ𝟣JⴹUþᏂꙅ𝟑ƘⲦ",
"signdate": "2025-10-30T02:25:43.857Z",

MCP Bridge Module - Injected into Python execution environments

This module provides a generic, tool-agnostic bridge for calling MCP tools from Python code.
It routes tool calls through the same HANDLERS registry that the AI uses.

The bridge automatically detects and handles server name prefixes by analyzing all registered
tool names to find their common prefix (e.g., "mcp_ragtag_sse_", "mcp_ca9_", "mcp_cdc_").
The prefix is recalculated dynamically, allowing the same code to work across all server 
instances and adapt automatically to tool changes.

Usage in user code:
    import mcp
    
    # Call any MCP tool using the exact same structure as the AI
    # The common prefix is auto-detected from all tools (works on any server)
    result = mcp.call("mcp_ragtag_sse_sqlite", {
        "input": {
            "sql": "SELECT * FROM users",
            "database": "myapp.db",
            "tool_unlock_token": "29e63eb5"
        }
    })
    
    # Or use the short name (cleanest - no prefix needed)
    result = mcp.call("sqlite", {
        "input": {
            "sql": "SELECT * FROM users", 
            "database": "myapp.db",
            "tool_unlock_token": "29e63eb5"
        }
    })
"""

import json
import sys
import time
import threading
from typing import Dict, Any, Optional, List

# Global HANDLERS registry - injected by python.py via callback from __init__.py
_HANDLERS_REGISTRY = None
_HANDLER_INFO = None  # Injected from python.py to provide context for tool calls

# Global state for logging and tracking
_call_log = []
_python_tool_token = None  # Will be injected by execution environment
_tool_tokens = {}  # Cache of tool tokens for inter-tool authentication

# Global state for async response handling
_pending_async_responses = {}  # Maps request_id -> response
_response_lock = threading.Lock()  # Thread-safe access to _pending_async_responses


def _get_handlers():
    """Get the HANDLERS registry.
    
    Since we now execute in the same process using exec(), the handlers
    are always set via set_handlers() before execution.
    
    Returns:
        dict: The HANDLERS registry
    """
    global _HANDLERS_REGISTRY
    
    if _HANDLERS_REGISTRY is None:
        return {}
    
    return _HANDLERS_REGISTRY


def set_handlers(handlers_dict):
    """Set the HANDLERS registry reference.
    
    This is called by python.py when __init__.py invokes the mcp_bridge callback.
    The handlers dict is passed by reference, so it will be fully populated by the
    time any tool calls are made.
    
    Args:
        handlers_dict: Reference to the HANDLERS dictionary from ragtag.tools.__init__
    """
    global _HANDLERS_REGISTRY
    _HANDLERS_REGISTRY = handlers_dict


def set_handler_info(handler_info_dict):
    """Set the handler_info for tool calls.
    
    Called from python.py to inject the handler_info context needed
    for remote tools and other tools that require session/client info.
    
    Args:
        handler_info_dict: Handler info with session_id, client, responder, etc.
    """
    global _HANDLER_INFO
    _HANDLER_INFO = handler_info_dict


def _detect_common_prefix() -> str:
    """
    Auto-detect the common prefix used by all tools in HANDLERS.
    
    Since tools are exposed to AIs with prefixes like "mcp_ragtag_sse_" or "mcp_ca9_",
    we analyze all registered tool names to find what prefix they share.
    
    Recalculates every time to handle dynamic tool changes.
    
    Returns:
        Common prefix string (e.g., "mcp_ragtag_sse_" or "mcp_ca9_"), or "" if none
    """
    handlers = _get_handlers()
    if not handlers:
        return ""
    
    # Get all handler names
    handler_names = list(handlers.keys())
    
    if not handler_names:
        return ""
    
    if len(handler_names) == 1:
        # With only one tool, we can't detect a prefix reliably
        return ""
    
    # Find longest common prefix among all tool names
    # Start with the first name and progressively check against others
    prefix = handler_names[0]
    
    for name in handler_names[1:]:
        # Find common prefix between current prefix and this name
        common = ""
        for i in range(min(len(prefix), len(name))):
            if prefix[i] == name[i]:
                common += prefix[i]
            else:
                break
        prefix = common
        
        if not prefix:
            break
    
    # The common prefix should end with an underscore to be valid
    # (e.g., "mcp_ragtag_sse_" not "mcp_ragtag_sse")
    if prefix and not prefix.endswith('_'):
        # Find the last underscore
        last_underscore = prefix.rfind('_')
        if last_underscore >= 0:
            prefix = prefix[:last_underscore + 1]
        else:
            prefix = ""
    
    return prefix


def _log_call(tool_name: str, arguments: Dict, result: Any):
    """Log a tool call for audit trail (excluding non-serializable objects)."""
    import time
    
    # Remove handler_info from arguments before logging (contains non-serializable objects)
    safe_arguments = {k: v for k, v in arguments.items() if k != 'handler_info'}
    
    # Also filter handler_info from result if it's a dict
    safe_result = result
    if isinstance(result, dict) and 'handler_info' in result:
        safe_result = {k: v for k, v in result.items() if k != 'handler_info'}
    
    _call_log.append({
        "tool": tool_name,
        "arguments": safe_arguments,
        "result": safe_result,
        "timestamp": time.time()
    })


def get_call_log() -> List[Dict]:
    """Get the log of all MCP tool calls made in this session."""
    return _call_log.copy()


def clear_call_log():
    """Clear the call log."""
    global _call_log
    _call_log = []


def _normalize_tool_name(name: str) -> str:
    """
    Normalize tool name by removing the auto-detected common prefix.
    
    The tool name might have a prefix depending on how the server was named
    in the MCP config (e.g., mcp_ragtag_sse_sqlite, mcp_ca9_sqlite).
    
    We detect the common prefix shared by all tools and remove it.
    
    Args:
        name: Full tool name as provided (may include server prefix)
        
    Returns:
        Normalized tool name without prefix
    """
    handlers = _get_handlers()
    
    # If name is already in HANDLERS, use it as-is
    if name in handlers:
        return name
    
    # Get the auto-detected common prefix
    prefix = _detect_common_prefix()
    
    # Remove the prefix if present
    if prefix and name.startswith(prefix):
        return name[len(prefix):]
    
    # No prefix to remove, return as-is
    return name


def _get_tool_token(tool_name: str) -> Optional[str]:
    """Get the unlock token for a specific tool by calling its readme."""
    handlers = _get_handlers()
    if not handlers:
        return None
    
    normalized_name = _normalize_tool_name(tool_name)
    
    # Check cache first
    if normalized_name in _tool_tokens:
        return _tool_tokens[normalized_name]
    
    # Get handler for this tool
    handler = handlers.get(normalized_name)
    if not handler:
        return None
    
    # Call readme to get token
    try:
        readme_result = handler({"input": {"operation": "readme"}})
        
        # Extract token from readme response
        if isinstance(readme_result, dict) and "content" in readme_result:
            content = readme_result["content"]
            if isinstance(content, list) and len(content) > 0:
                text = content[0].get("text", "")
                # Parse the JSON to find the token
                try:
                    data = json.loads(text)
                    params = data.get("parameters", {})
                    props = params.get("properties", {})
                    token_info = props.get("tool_unlock_token", {})
                    desc = token_info.get("description", "")
                    
                    # Extract token from description (format: "Security token, <TOKEN>, obtained from...")
                    import re
                    match = re.search(r'Security token,?\s+([a-f0-9]+)', desc)
                    if match:
                        token = match.group(1)
                        _tool_tokens[normalized_name] = token
                        return token
                except:
                    pass
    except:
        pass
    
    return None


def _create_inter_tool_token(target_tool_name: str) -> Optional[str]:
    """Create an inter-tool authentication token."""
    if not _python_tool_token:
        return None
    
    target_token = _get_tool_token(target_tool_name)
    if not target_token:
        return None
    
    return f"-{_python_tool_token}-{target_token}"


# ============================================================================
# Generic Tool Call Interface
# ============================================================================

def call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call any MCP tool using the same structure as AI tool calls.
    
    This is a completely generic, tool-agnostic bridge that routes calls
    through the HANDLERS registry. Automatically detects and removes the
    common prefix shared by all tools on this server instance.
    
    Args:
        tool_name: Name of the tool (with or without auto-detected prefix)
                  Examples: "sqlite", "mcp_ragtag_sse_sqlite", "mcp_ca9_sqlite"
                  All resolve correctly via auto-detected prefix removal
        arguments: Tool arguments dict (should contain "input" key with params)
        
    Returns:
        Tool response dict
        
    Examples:
        # Using full tool name (prefix auto-detected and removed)
        result = mcp.call("mcp_ragtag_sse_sqlite", {
            "input": {
                "sql": "SELECT * FROM users",
                "database": "test.db",
                "tool_unlock_token": "29e63eb5"
            }
        })
        
        # Using short name (no prefix - cleanest)
        result = mcp.call("sqlite", {
            "input": {
                "sql": ".tables",
                "database": "test.db",
                "tool_unlock_token": "29e63eb5"
            }
        })
        
        # Same code works on different servers (prefix auto-adapts)
        # On ragtag_sse: "mcp_ragtag_sse_sqlite" -> "sqlite"
        # On ca9: "mcp_ca9_sqlite" -> "sqlite"
        # On cdc: "mcp_cdc_sqlite" -> "sqlite"
        
        # With inter-tool authentication (auto-injected if python token available)
        result = mcp.call("user", {
            "input": {
                "operation": "show_popup",
                "html": "<h1>Hello!</h1>",
                "title": "Message"
            }
        })  # Token automatically added
    """
    handlers = _get_handlers()
    if not handlers:
        raise RuntimeError("HANDLERS registry not available. Cannot call tools.")
    
    # Normalize tool name
    normalized_name = _normalize_tool_name(tool_name)
    
    # Get handler
    handler = handlers.get(normalized_name)
    if not handler:
        available = ", ".join(sorted(handlers.keys()))
        raise ValueError(f"Tool '{tool_name}' not found. Available tools: {available}")
    
    # Auto-inject inter-tool token if needed and not already present
    if isinstance(arguments, dict) and "input" in arguments:
        input_params = arguments["input"]
        if isinstance(input_params, dict):
            # If no token provided, try to create inter-tool token
            if "tool_unlock_token" not in input_params and _python_tool_token:
                inter_token = _create_inter_tool_token(normalized_name)
                if inter_token:
                    arguments = {
                        "input": {
                            **input_params,
                            "tool_unlock_token": inter_token
                        }
                    }
    
    # Inject handler_info if available (needed for remote tools and local proxy tools)
    request_id = None
    responder = None
    original_send_response = None
    
    if _HANDLER_INFO is not None:
        import uuid
        # Generate unique request_id for this call
        request_id = str(uuid.uuid4())
        
        # Create a modified handler_info with the target tool name and unique request_id
        modified_handler_info = {
            **_HANDLER_INFO,
            'tool_name': normalized_name,  # Override with target tool name
            'request_id': request_id
        }
        arguments_with_info = {
            **arguments,
            'handler_info': modified_handler_info
        }
        
        # Hook _send_response to capture async responses for our request_id
        responder = modified_handler_info['responder']
        original_send_response = responder._send_response
        
        def intercepting_send_response(session_id, response):
            # Check if this response is for our request_id
            if isinstance(response, dict) and response.get('id') == request_id:
                with _response_lock:
                    _pending_async_responses[request_id] = response.get('result')
            # Always call original to maintain normal flow
            return original_send_response(session_id, response)
        
        # Install the interceptor
        responder._send_response = intercepting_send_response
    else:
        arguments_with_info = arguments
    
    # Call the handler
    try:
        result = handler(arguments_with_info)
        
        # If handler returned None AND we have handler_info, wait for async response
        if result is None and request_id is not None:
            timeout = 30  # 30 second timeout for async responses
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                with _response_lock:
                    if request_id in _pending_async_responses:
                        result = _pending_async_responses.pop(request_id)
                        break
                time.sleep(0.05)  # Poll every 50ms
            
            # If still None after timeout, return error
            if result is None:
                result = {
                    "content": [{
                        "type": "text", 
                        "text": f"Timeout waiting for async tool response after {timeout} seconds. The tool may not support async responses, or the response was delayed."
                    }],
                    "isError": True,
                    "_async_timeout": True
                }
        
        _log_call(tool_name, arguments, result)
        return result
    except Exception as e:
        error_result = {
            "error": str(e),
            "tool": tool_name,
            "arguments": arguments
        }
        _log_call(tool_name, arguments, error_result)
        raise
    finally:
        # Restore original _send_response
        if responder is not None and original_send_response is not None:
            responder._send_response = original_send_response


# ============================================================================
# Module Configuration
# ============================================================================

__all__ = ['call', 'get_call_log', 'clear_call_log', 'get_detected_prefix']


def _inject_token(token: str):
    """Internal function to inject the Python tool token for inter-tool auth."""
    global _python_tool_token
    _python_tool_token = token


def _show_available_tools():
    """Show helpful information about available MCP tools."""
    handlers = _get_handlers()
    if handlers:
        tool_names = sorted(handlers.keys())
        prefix = _detect_common_prefix()
        
        print(f"MCP tools available: {', '.join(tool_names)}", file=sys.stderr)
        if prefix:
            print(f"Auto-detected prefix: '{prefix}' (will be stripped from tool names)", file=sys.stderr)
        print("Use mcp.call(tool_name, arguments) to call any tool", file=sys.stderr)
        print("Call logs available via mcp.get_call_log()", file=sys.stderr)
    else:
        print("No MCP tools available (HANDLERS registry not available)", file=sys.stderr)


def get_detected_prefix() -> str:
    """
    Get the auto-detected common prefix for this server instance.
    
    Recalculates the prefix from the current set of tools each time called.
    Useful for debugging to see what prefix is being stripped from tool names.
    
    Returns:
        The detected prefix string (e.g., "mcp_ragtag_sse_" or "mcp_ca9_"), or "" if none
    """
    return _detect_common_prefix()
