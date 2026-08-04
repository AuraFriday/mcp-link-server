"""
File: ragtag/tools/mcp_bridge.py
Project: Aura Friday MCP-Link Server
Component: MCP Bridge helper module.
Author: Christopher Nathan Drake (cnd)

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "dх2ųⲞƤ7𝟤АΑ𝕌NꓗᏟꓳƧ𝟧ȣ2Ƌο𐓒ᏎƟ×9ⅮᏟ𐐕О3o𝟤xхʈꓣᎪꜱɌƊᏎeǝȜВ𝟪ᛕ𝟢UŧÐkþМᴠWEƛᏂƋ6ᴜƻ𝟙ᎬFPꓮJꓳѵƎq𝟧𝟢ΗЈᗅօkοȣƋбʋ3ꜱ1ƱⲔavþ𝕌x𝟛ŧоƙHYꓮʈⲘ10iᗅ",
"signdate": "2026-07-29T09:29:58.689Z",

MCP Bridge Module - Injected into Python execution environments

This module provides a generic, tool-agnostic bridge for calling MCP tools from Python code.
It routes tool calls through the same HANDLERS registry that the AI uses.

The bridge automatically detects and handles server name prefixes by analyzing all registered
tool names to find their common prefix (e.g., "mcp_ragtag_sse_", "mcp_ca9_", "mcp_cdc_").
The prefix is recalculated dynamically, allowing the same code to work across all server 
instances and adapt automatically to tool changes.

When a TOOL_SUFFIX is in effect (multi-machine deployments), bare tool names are also
resolved by trying the suffixed name (e.g. "sqlite" resolves to "sqlite_rog").

Per-execution isolation: python.py can construct one
Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info instance per
execution and inject it as `mcp`, giving each execution its own call log and handler_info.
The module-level functions remain as the legacy API (still used by python.py today),
delegating to a shared default instance of the same class.

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
import os
import sys
import time
import threading
from typing import Dict, Any, Optional, List

# Tool name suffix for multi-machine deployments (same convention as every tool module).
# Used by suffix-aware name resolution: mcp.call("sqlite") finds "sqlite<suffix>".
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")

# Intentionally shared across executions: unlock tokens are server-wide values.
_tool_tokens = {}  # Cache of tool tokens for inter-tool authentication

# Global state for async response handling.
# Keyed by per-call unique request_id (uuid4), so sharing across executions is safe.
_pending_async_responses = {}  # Maps request_id -> response
_response_lock = threading.Lock()  # Thread-safe access to _pending_async_responses


# ============================================================================
# Per-Execution Bridge Class
# ============================================================================

class Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info:
    """
    A per-execution MCP bridge object: each instance owns its own call log and
    handler_info, so concurrent python-tool executions do not cross-contaminate.

    python.py constructs one instance per execution and injects it as `mcp`.
    The module-level functions below delegate to a shared default instance so the
    legacy API (whole module injected as `mcp`) keeps working unchanged.
    """

    def __init__(self,
                 handlers_dict: Optional[Dict] = None,
                 handler_info_dict: Optional[Dict] = None,
                 python_tool_token: Optional[str] = None,
                 tool_name_suffix_override: Optional[str] = None):
        """
        Args:
            handlers_dict: Reference to the HANDLERS dictionary from ragtag.tools.__init__
            handler_info_dict: Handler info with session_id, client, responder, etc.
            python_tool_token: Python tool unlock token for inter-tool authentication
            tool_name_suffix_override: Test-only override of the TOOL_SUFFIX value used
                for suffix-aware name resolution; None means use the server's real
                TOOL_NAME_SUFFIX (read from the environment at import time).
        """
        self._handlers_registry = handlers_dict
        self._handler_info = handler_info_dict
        self._python_tool_token = python_tool_token
        self._call_log = []  # Isolated per instance - the point of this class
        self._tool_name_suffix_for_suffix_aware_resolution = (
            TOOL_NAME_SUFFIX if tool_name_suffix_override is None else tool_name_suffix_override)

    def _get_handlers(self):
        """Get the HANDLERS registry.
        
        Since we execute in the same process using exec(), the handlers
        are always set (constructor or set_handlers()) before execution.
        
        Returns:
            dict: The HANDLERS registry
        """
        if self._handlers_registry is None:
            return {}
        
        return self._handlers_registry

    def set_handlers(self, handlers_dict):
        """Set the HANDLERS registry reference.
        
        The handlers dict is passed by reference, so it will be fully populated by the
        time any tool calls are made.
        
        Args:
            handlers_dict: Reference to the HANDLERS dictionary from ragtag.tools.__init__
        """
        self._handlers_registry = handlers_dict

    def set_handler_info(self, handler_info_dict):
        """Set the handler_info for tool calls.
        
        Injects the handler_info context needed for remote tools and other
        tools that require session/client info.
        
        Args:
            handler_info_dict: Handler info with session_id, client, responder, etc.
        """
        self._handler_info = handler_info_dict

    def _inject_token(self, token: str):
        """Inject the Python tool token for inter-tool auth."""
        self._python_tool_token = token

    def _detect_common_prefix(self) -> str:
        """
        Auto-detect the common prefix used by all tools in HANDLERS.
        
        Since tools are exposed to AIs with prefixes like "mcp_ragtag_sse_" or "mcp_ca9_",
        we analyze all registered tool names to find what prefix they share.
        
        Recalculates every time to handle dynamic tool changes.
        
        Returns:
            Common prefix string (e.g., "mcp_ragtag_sse_" or "mcp_ca9_"), or "" if none
        """
        handlers = self._get_handlers()
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

    def _log_call(self, tool_name: str, arguments: Dict, result: Any):
        """Log a tool call for audit trail (excluding non-serializable objects)."""
        import time
        
        # Remove handler_info from arguments before logging (contains non-serializable objects)
        safe_arguments = {k: v for k, v in arguments.items() if k != 'handler_info'}
        
        # Also filter handler_info from result if it's a dict
        safe_result = result
        if isinstance(result, dict) and 'handler_info' in result:
            safe_result = {k: v for k, v in result.items() if k != 'handler_info'}
        
        self._call_log.append({
            "tool": tool_name,
            "arguments": safe_arguments,
            "result": safe_result,
            "timestamp": time.time()
        })

    def get_call_log(self) -> List[Dict]:
        """Get the log of all MCP tool calls made through this bridge instance."""
        return self._call_log.copy()

    def clear_call_log(self):
        """Clear this bridge instance's call log."""
        self._call_log = []

    def _normalize_tool_name(self, name: str) -> str:
        """
        Normalize tool name by removing the auto-detected common prefix and/or
        applying the TOOL_SUFFIX when one is in effect.
        
        The tool name might have a prefix depending on how the server was named
        in the MCP config (e.g., mcp_ragtag_sse_sqlite, mcp_ca9_sqlite), and the
        registered names might carry a TOOL_SUFFIX (e.g., sqlite_rog).
        
        Args:
            name: Full tool name as provided (may include server prefix,
                  may lack the suffix)
            
        Returns:
            Normalized tool name resolving to a HANDLERS key where possible
        """
        handlers = self._get_handlers()
        
        # If name is already in HANDLERS, use it as-is
        if name in handlers:
            return name
        
        # Suffix-aware resolution: a bare name like "sqlite" resolves to
        # "sqlite<TOOL_SUFFIX>" when the server runs with a suffix
        suffix = self._tool_name_suffix_for_suffix_aware_resolution
        if suffix and (name + suffix) in handlers:
            return name + suffix
        
        # Get the auto-detected common prefix
        prefix = self._detect_common_prefix()
        
        # Remove the prefix if present
        if prefix and name.startswith(prefix):
            stripped = name[len(prefix):]
            # Try the suffixed form of the stripped name too
            if suffix and stripped not in handlers and (stripped + suffix) in handlers:
                return stripped + suffix
            return stripped
        
        # No prefix to remove, return as-is
        return name

    def _get_tool_token(self, tool_name: str) -> Optional[str]:
        """Get the unlock token for a specific tool by calling its readme."""
        handlers = self._get_handlers()
        if not handlers:
            return None
        
        normalized_name = self._normalize_tool_name(tool_name)
        
        # Check cache first (shared module-level cache - tokens are server-wide)
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

    def _create_inter_tool_token(self, target_tool_name: str) -> Optional[str]:
        """Build the inter-tool token in the "-<caller_token>-<target_token>" form.

        This is NOT authentication and NOT a secret. Unlock tokens are COMPREHENSION GATES
        ("this caller has read the tool's readme"; see
        doc/50_non-AI-calling-and-how-to-get-unlock-tokens.md). This form is a convenience so
        non-AI / tool-to-tool code can satisfy a target tool's gate without re-reading its
        readme each call; the target accepts it only when <caller_token> is a currently
        registered tool's own token. Convenience for internal calls, not a security boundary.
        """
        if not self._python_tool_token:
            return None
        
        target_token = self._get_tool_token(target_tool_name)
        if not target_token:
            return None
        
        return f"-{self._python_tool_token}-{target_token}"

    def call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call any MCP tool using the same structure as AI tool calls.
        
        This is a completely generic, tool-agnostic bridge that routes calls
        through the HANDLERS registry. Automatically detects and removes the
        common prefix shared by all tools on this server instance, and resolves
        bare names to their suffixed form when a TOOL_SUFFIX is in effect.
        
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
        handlers = self._get_handlers()
        if not handlers:
            raise RuntimeError("HANDLERS registry not available. Cannot call tools.")
        
        # Normalize tool name
        normalized_name = self._normalize_tool_name(tool_name)
        
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
                if "tool_unlock_token" not in input_params and self._python_tool_token:
                    inter_token = self._create_inter_tool_token(normalized_name)
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
        
        if self._handler_info is not None:
            import uuid
            # Generate unique request_id for this call
            request_id = str(uuid.uuid4())
            
            # Create a modified handler_info with the target tool name and unique request_id
            modified_handler_info = {
                **self._handler_info,
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
            
            self._log_call(tool_name, arguments, result)
            return result
        except Exception as e:
            error_result = {
                "error": str(e),
                "tool": tool_name,
                "arguments": arguments
            }
            self._log_call(tool_name, arguments, error_result)
            raise
        finally:
            # Restore original _send_response
            if responder is not None and original_send_response is not None:
                responder._send_response = original_send_response

    def _show_available_tools(self):
        """Show helpful information about available MCP tools."""
        handlers = self._get_handlers()
        if handlers:
            tool_names = sorted(handlers.keys())
            prefix = self._detect_common_prefix()
            
            print(f"MCP tools available: {', '.join(tool_names)}", file=sys.stderr)
            if prefix:
                print(f"Auto-detected prefix: '{prefix}' (will be stripped from tool names)", file=sys.stderr)
            print("Use mcp.call(tool_name, arguments) to call any tool", file=sys.stderr)
            print("Call logs available via mcp.get_call_log()", file=sys.stderr)
        else:
            print("No MCP tools available (HANDLERS registry not available)", file=sys.stderr)

    def get_detected_prefix(self) -> str:
        """
        Get the auto-detected common prefix for this server instance.
        
        Recalculates the prefix from the current set of tools each time called.
        Useful for debugging to see what prefix is being stripped from tool names.
        
        Returns:
            The detected prefix string (e.g., "mcp_ragtag_sse_" or "mcp_ca9_"), or "" if none
        """
        return self._detect_common_prefix()


# ============================================================================
# Legacy Module-Level API (delegates to a shared default instance)
# ============================================================================
# python.py (and agent.py/cursor.py/llm_old.py/ocr.py) still use these module
# functions today; they share ONE default bridge instance, preserving the
# pre-class behavior exactly until callers migrate to per-execution instances.

_default_bridge_instance_used_by_legacy_module_level_api = (
    Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info())


def _get_handlers():
    """Get the HANDLERS registry of the shared default bridge instance."""
    return _default_bridge_instance_used_by_legacy_module_level_api._get_handlers()


def set_handlers(handlers_dict):
    """Set the HANDLERS registry reference on the shared default bridge instance.
    
    This is called by python.py when __init__.py invokes the mcp_bridge callback.
    
    Args:
        handlers_dict: Reference to the HANDLERS dictionary from ragtag.tools.__init__
    """
    _default_bridge_instance_used_by_legacy_module_level_api.set_handlers(handlers_dict)


def set_handler_info(handler_info_dict):
    """Set the handler_info on the shared default bridge instance.
    
    Called from python.py to inject the handler_info context needed
    for remote tools and other tools that require session/client info.
    
    Args:
        handler_info_dict: Handler info with session_id, client, responder, etc.
    """
    _default_bridge_instance_used_by_legacy_module_level_api.set_handler_info(handler_info_dict)


def _detect_common_prefix() -> str:
    """Auto-detect the common tool-name prefix (shared default bridge instance)."""
    return _default_bridge_instance_used_by_legacy_module_level_api._detect_common_prefix()


def _log_call(tool_name: str, arguments: Dict, result: Any):
    """Log a tool call on the shared default bridge instance."""
    _default_bridge_instance_used_by_legacy_module_level_api._log_call(tool_name, arguments, result)


def get_call_log() -> List[Dict]:
    """Get the log of all MCP tool calls made via the shared default bridge instance."""
    return _default_bridge_instance_used_by_legacy_module_level_api.get_call_log()


def clear_call_log():
    """Clear the shared default bridge instance's call log."""
    _default_bridge_instance_used_by_legacy_module_level_api.clear_call_log()


def _normalize_tool_name(name: str) -> str:
    """Normalize a tool name (shared default bridge instance)."""
    return _default_bridge_instance_used_by_legacy_module_level_api._normalize_tool_name(name)


def _get_tool_token(tool_name: str) -> Optional[str]:
    """Get the unlock token for a specific tool (shared default bridge instance)."""
    return _default_bridge_instance_used_by_legacy_module_level_api._get_tool_token(tool_name)


def _create_inter_tool_token(target_tool_name: str) -> Optional[str]:
    """Create an inter-tool authentication token (shared default bridge instance)."""
    return _default_bridge_instance_used_by_legacy_module_level_api._create_inter_tool_token(target_tool_name)


def call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Call any MCP tool via the shared default bridge instance.
    
    See Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info.call
    for full documentation and examples.
    """
    return _default_bridge_instance_used_by_legacy_module_level_api.call(tool_name, arguments)


def _inject_token(token: str):
    """Internal function to inject the Python tool token for inter-tool auth."""
    _default_bridge_instance_used_by_legacy_module_level_api._inject_token(token)


def _show_available_tools():
    """Show helpful information about available MCP tools."""
    _default_bridge_instance_used_by_legacy_module_level_api._show_available_tools()


def get_detected_prefix() -> str:
    """
    Get the auto-detected common prefix for this server instance.
    
    Recalculates the prefix from the current set of tools each time called.
    Useful for debugging to see what prefix is being stripped from tool names.
    
    Returns:
        The detected prefix string (e.g., "mcp_ragtag_sse_" or "mcp_ca9_"), or "" if none
    """
    return _default_bridge_instance_used_by_legacy_module_level_api.get_detected_prefix()


# ============================================================================
# Module Configuration
# ============================================================================

__all__ = ['call', 'get_call_log', 'clear_call_log', 'get_detected_prefix',
           'Per_Execution_Mcp_Tool_Call_Bridge_With_Isolated_Call_Log_And_Handler_Info']
