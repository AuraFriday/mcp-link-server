"""
file: ragtag/server_timeout.py
Project: Aura Friday MCP-Link Server
Component: Tool Execution Timeout Wrapper
Author: Christopher Nathan Drake (cnd)

Provides timeout protection for MCP tool execution using ThreadPoolExecutor.
If a tool hangs or takes too long, the caller receives a timeout error instead
of waiting indefinitely.

Copyright: © 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "𝟟ⲔN𝘈ʌǝꓪԛӠРȷСRɊ𝖠yΥʈᴜ𝟩ƛoŧƵτᴍⅼƤƻꓧᗪ𝟟𝟤ȷNѡWɡᒿ𝘈þҮҳᎠǝᏟЈ0ꓔОƬ𝐴ᏴꓠƵ𝟪ꓗƌƲNƱOΟOWa𝟫ᎬoЗɌⲢτᎠȠЗеdƼWıtZ6ꞇ2𝟟ɅlοS9ȜντΡЗᴍꙄҳΤ0ZⅼꓳŧcᗞI"
"signdate": "2026-01-27T11:00:24.724Z",
"""

import concurrent.futures
import threading
import time
from typing import Dict, Any, Callable, Optional

# Lazy import to avoid circular dependency
_MCPLogger = None

def _get_logger():
    """Lazy import MCPLogger to avoid circular dependency at module load time."""
    global _MCPLogger
    if _MCPLogger is None:
        from easy_mcp.server import MCPLogger
        _MCPLogger = MCPLogger
    return _MCPLogger

# Global executor - initialized lazily
_tool_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_executor_lock = threading.Lock()

# Default values (will be overridden by config when available)
DEFAULT_TOOL_TIMEOUT_SECONDS = 270  # 4.5 minutes
MAX_EXECUTOR_WORKERS = 10

# Track timed-out request IDs to suppress late responses
_timed_out_requests: Dict[str, float] = {}
_timed_out_requests_lock = threading.Lock()
TIMED_OUT_REQUESTS_TTL_SECONDS = 600  # Keep track for 10 minutes


def get_executor(max_workers: int = MAX_EXECUTOR_WORKERS) -> concurrent.futures.ThreadPoolExecutor:
    """Get or create the global tool executor."""
    global _tool_executor
    
    if _tool_executor is None:
        with _executor_lock:
            if _tool_executor is None:
                _tool_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="tool_timeout_worker"
                )
    return _tool_executor


def shutdown_executor():
    """Shutdown the executor gracefully. Call on server shutdown."""
    global _tool_executor
    if _tool_executor:
        _tool_executor.shutdown(wait=False)
        _tool_executor = None


def mark_request_timed_out(request_id: str):
    """Mark a request as timed out to suppress late responses."""
    with _timed_out_requests_lock:
        _timed_out_requests[request_id] = time.time()
        
        # Clean up old entries
        now = time.time()
        expired_request_ids = [
            rid for rid, timestamp in _timed_out_requests.items() 
            if now - timestamp > TIMED_OUT_REQUESTS_TTL_SECONDS
        ]
        for rid in expired_request_ids:
            del _timed_out_requests[rid]


def is_request_timed_out(request_id: str) -> bool:
    """Check if a request was previously timed out (for suppressing late responses)."""
    with _timed_out_requests_lock:
        return request_id in _timed_out_requests


def get_configured_timeout() -> float:
    """Get the tool timeout from configuration, with fallback to default."""
    try:
        from ragtag.shared_config import get_config_manager, SharedConfigManager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        timeout = SharedConfigManager.get_settings_value(
            config, 'server.tool_timeout_seconds', DEFAULT_TOOL_TIMEOUT_SECONDS
        )
        if isinstance(timeout, (int, float)) and timeout > 0:
            return float(timeout)
    except Exception:
        pass  # Fall through to default
    return DEFAULT_TOOL_TIMEOUT_SECONDS


def execute_tool_with_timeout(
    handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_args: Dict[str, Any],
    tool_name: str,
    timeout_seconds: Optional[float] = None,
    request_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Execute a tool handler with timeout protection.
    
    Args:
        handler: The tool handler function to call
        tool_args: Arguments to pass to the handler
        tool_name: Name of the tool (for logging)
        timeout_seconds: Maximum execution time in seconds (None = use config default)
        request_id: Optional request ID for tracking timed-out requests
        
    Returns:
        Tool result dict, error dict if timeout/exception, or None if tool returns None
        (indicating async response pattern used by remote tools)
    """
    MCPLogger = _get_logger()
    
    # Determine timeout to use
    if timeout_seconds is None:
        timeout_seconds = get_configured_timeout()
    
    executor = get_executor()
    
    MCPLogger.log("ToolTimeout", f"Executing '{tool_name}' with {timeout_seconds}s timeout")
    
    try:
        future = executor.submit(handler, tool_args)
        result = future.result(timeout=timeout_seconds)
        
        # Tool returned normally (including None for async tools)
        return result
        
    except concurrent.futures.TimeoutError:
        error_message = (
            f"Tool '{tool_name}' timed out after {timeout_seconds} seconds. "
            f"The operation may still be running in the background."
        )
        MCPLogger.log("ToolTimeout", f"TIMEOUT: {error_message}")
        
        # Mark request as timed out to suppress any late response
        if request_id:
            mark_request_timed_out(request_id)
        
        return {
            "content": [{"type": "text", "text": error_message}],
            "isError": True,
            "_timed_out": True
        }
        
    except Exception as e:
        # Tool raised an exception - let the normal error handling deal with it
        # by re-raising so the caller's try/except catches it
        raise


def get_tool_timeout(
    tool_name: str,
    tool_args: Dict[str, Any],
    tool_handlers: Dict[str, Any],
    global_timeout: Optional[float] = None
) -> float:
    """
    Determine the timeout for a tool call with precedence:
    1. Per-call override (_timeout_seconds in input args)
    2. Per-tool definition (timeout_seconds in tool handler info)
    3. Global default from config
    
    Args:
        tool_name: Name of the tool
        tool_args: Arguments passed to the tool
        tool_handlers: Server's tool_handlers dict
        global_timeout: Override for global default (None = use config)
        
    Returns:
        Timeout in seconds to use
    """
    # Check per-call override first (in input dict)
    input_data = tool_args.get("input", {})
    if isinstance(input_data, dict):
        per_call_timeout = input_data.get("_timeout_seconds")
        if per_call_timeout is not None:
            try:
                return float(per_call_timeout)
            except (ValueError, TypeError):
                pass
    
    # Check per-tool definition
    if tool_name in tool_handlers:
        tool_info = tool_handlers[tool_name]
        if isinstance(tool_info, dict):
            per_tool_timeout = tool_info.get("timeout_seconds")
            if per_tool_timeout is not None:
                try:
                    return float(per_tool_timeout)
                except (ValueError, TypeError):
                    pass
    
    # Fall back to global default
    if global_timeout is not None:
        return global_timeout
    
    return get_configured_timeout()
