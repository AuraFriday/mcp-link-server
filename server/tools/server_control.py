"""
File: ragtag/tools/server_control.py
Project: Aura Friday MCP-Link Server
Component: Server Control Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for controlling server operations (restart/stop).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "mƍᑕXqumᴠ𝕌qlƍGŧһȠkɪWƤ𝟤ꓖеŪᗪi৭LntȠƬVοLƼνuꓦɅJ৭Ꮒ𝟟τƙƊƟ1Ƶ𝟣Т𝟙ꓖΗɌȢrƲԁʈꓰⴹƨȣȣӠ𐐕𐐕ЗꞇϜꓟᴅīуƻхɪⲢ8v6C1օCΝ𝟙D2QᏴxΥʋ𝟤OЕⲘWꓠ2DcᏮiƏ5"
"signdate": "2025-09-17T11:19:12.659Z",
"""

from typing import Dict
import threading
import time
import os
from easy_mcp.server import MCPLogger

# Global server instance - will be set by ragtag.py
mcp_server = None

# Tool definitions
TOOLS = [
    {
        "name": "server_control",
        "description": """Control the ragtag_sse tool-server operation (restart/stop)

Operations:
- get_pid: Get current server's process ID
- stop: Stop the server
- restart: Restart the server

Recommended restart process:
1. Call get_pid to record current PID
2. Call restart operation
3. Use exactly this command for Windows: timeout.exe /t 12 /nobreak
   For Mac/Linux use: sleep 12
   IMPORTANT: This 12-second wait is required for Cursor to detect the change and reconnect
4. Call get_pid again and verify the new PID is different
   If PIDs match, the restart may have failed
5. OPTIONAL: you can see server logs with a powershell command like: Get-Content -Tail 30 "C:\\Users\\cnd\\Downloads\\cursor\\ragtag\\python\\ragtag\\run_ragtag_sse.log"

Args:
    operation: Operation to perform ('restart', 'stop', or 'get_pid')
    wait: Optional seconds to wait before operation
""",
        "parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["restart", "stop", "get_pid"],
                    "description": "Operation to perform"
                },
                "wait": {
                    "type": "number",
                    "description": "Optional seconds to wait before operation",
                    "default": 0
                }
            },
            "required": ["operation"],
            "title": "serverControlArguments",
            "type": "object"
        }
    }
]


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log("SERVER_CONTROL", f"Error: {error_msg}")
    docs = "" # "\n\n" + json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["parameters"] }, indent=2) if with_readme else ""
    return { "content": [{"type": "text", "text": f"{error_msg}{docs}"}], "isError": True }

def handle_server_control(input_param: Dict[str, str]) -> Dict:
    """Handle server control operations via MCP interface.
    
    Args:
        input_param: Dictionary containing 'operation' and optional 'wait' parameter
        
    Returns:
        Dict containing operation status or error information
    """
    try:
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {} # Pop off synthetic handler_info parameter early (before validation); This is added by the server for tools that need dynamic routing

        # Validate server instance is available
        if not mcp_server:
            return {
                "content": [{"type": "text", "text": "Server instance not initialized"}],
                "isError": True
            }
        
        # Extract parameters
        operation = input_param.get("operation")
        
        # Handle get_pid operation
        if operation == "get_pid":
            current_pid = os.getpid()
            MCPLogger.log("SERVER_CONTROL", f"Returning current PID: {current_pid}")
            return {
                "content": [{
                    "type": "text",
                    "text": f"Current server PID: {current_pid}"
                }],
                "pid": current_pid,  # Include raw PID for programmatic access
                "isError": False
            }
        
        # Handle restart/stop operations
        wait = float(input_param.get("wait", 0))
        
        # Log the control request
        MCPLogger.log("SERVER_CONTROL", f"Processing {operation} request with wait={wait}")
        
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
            
    except Exception as e:
        error_msg = f"Error processing server control request: {str(e)}"
        MCPLogger.log("SERVER_CONTROL", f"Error: {error_msg}")
        return {
            "content": [{"type": "text", "text": error_msg}],
            "isError": True
        }

# Map of tool names to their handlers
HANDLERS = {
    "server_control": handle_server_control
}
