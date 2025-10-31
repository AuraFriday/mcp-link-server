"""
File: ragtag/tools/local.py
Project: Aura Friday MCP-Link Server
Component: Local MCP Bridge
Author: Christopher Nathan Drake (cnd)

A shim that connects to external MCP servers via STDIO and proxies their tools through our SSE transport.
Reads claude_desktop_config.json to discover MCP servers and their tools.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "nԝꓚ𝟑MΡP𝟧𐓒ЅꙄЕƬᴜϹⲘꓣᗪ𝟫µᴠΕjЗ2Odѡ𝟢ӠꓧSу6𝟤M5ᛕΥÞƎΝƿ𝐴МÐƌᗷꓳȜΥ𝟫ꓠΒꙅȣHᏴᏂᎪƖ𝟣𝟣𝟑ΥyƤJIGVŪSƙb𝟢օοЈ2𝟥wƊ7ΕꓐᏮZꙅď𐐕ꞇƤ6rƊþօ𝟤ΟⅼМ𝛢ꓝ𝟚ĐТgɪ",
"signdate": "2025-10-30T02:25:41.823Z",
"""

import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Union, BinaryIO, Tuple, Any
from easy_mcp.server import MCPLogger, get_tool_token

# Windows-specific constants for hiding console windows
if platform.system() == "Windows":
    CREATE_NO_WINDOW = 0x08000000  # from winbase.h
else:
    CREATE_NO_WINDOW = 0

# Constants
TOOL_LOG_NAME = "LOCAL"
TOOL_INTERNAL_NAME = "{serverName}" # "ragtag_sse" # gets shown to clients, so they need to know to replace this with the actual server name the user gave them.
LAST_TOOL = None

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Global state for MCP bridge
class MCPBridge:
    def __init__(self):
        self.subprocesses: Dict[str, subprocess.Popen] = {}
        self.subprocess_locks: Dict[str, threading.Lock] = {}
        self.tool_registry: Dict[str, Dict] = {}  # tool_name -> {server_name, original_tool_name, schema}
        self.request_counters: Dict[str, int] = {}  # server_name -> counter for JSON-RPC IDs
        self.initialized = False
        self.init_lock = threading.Lock()
    
    def ensure_initialized(self):
        """Ensure the bridge is initialized (thread-safe)"""
        if self.initialized:
            return
        
        with self.init_lock:
            if self.initialized:
                return
            
            try:
                self._initialize()
                self.initialized = True
                MCPLogger.log(TOOL_LOG_NAME, "MCP Bridge initialization completed successfully")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"MCP Bridge initialization failed: {str(e)}")
                # Continue with empty tool registry
    
    def _initialize(self):
        """Initialize the MCP bridge by reading config from settings[0].local_mcpServers and starting subprocesses"""
        try:
            # Import here to avoid circular dependencies
            from ragtag.shared_config import SharedConfigManager, get_config_manager
            
            config_manager = get_config_manager()
            config = config_manager.load_config()
            
            # Get local_mcpServers section from settings[0] - empty {} is valid
            local_mcp_servers = SharedConfigManager.ensure_settings_section(config, 'local_mcpServers')
            
            # Filter to only enabled servers
            enabled_servers = {}
            for server_name, server_config in local_mcp_servers.items():
                if isinstance(server_config, dict) and server_config.get('enabled', False):
                    # Remove the 'enabled' key since it's not part of the MCP server config
                    filtered_config = {k: v for k, v in server_config.items() if k != 'enabled'}
                    enabled_servers[server_name] = filtered_config
            
            MCPLogger.log(TOOL_LOG_NAME, f"Found {len(enabled_servers)} enabled MCP servers in settings[0].local_mcpServers")
            
            for server_name, server_config in enabled_servers.items():
                try:
                    self._start_server(server_name, server_config)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to start server {server_name}: {str(e)}")
                    continue
                    
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to load local MCP servers from config: {str(e)}")
            return
    
    
    def _start_server(self, server_name: str, server_config: Dict):
        """Start an MCP server subprocess and discover its tools"""
        command = server_config.get('command')
        args = server_config.get('args', [])
        ai_description = server_config.get('ai_description', f'Use this tool when you need to access {server_name} functionality')
        
        if not command:
            MCPLogger.log(TOOL_LOG_NAME, f"No command specified for server {server_name}")
            return
        
        full_command = [command] + args
        MCPLogger.log(TOOL_LOG_NAME, f"Starting server {server_name}: {' '.join(full_command)}")
        
        try:
            # Start subprocess
            proc = subprocess.Popen(
                full_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',  # Explicitly use UTF-8 encoding
                errors='replace',  # Replace invalid characters instead of failing
                bufsize=0,  # Unbuffered
                shell=False,
                creationflags=CREATE_NO_WINDOW
            )
            
            self.subprocesses[server_name] = proc
            self.subprocess_locks[server_name] = threading.Lock()
            self.request_counters[server_name] = 0
            
            # Discover tools
            tools = self._discover_tools(server_name, ai_description)
            MCPLogger.log(TOOL_LOG_NAME, f"Server {server_name} provided {len(tools)} tools")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to start server {server_name}: {str(e)}")
            if server_name in self.subprocesses:
                try:
                    self.subprocesses[server_name].terminate()
                except:
                    pass
                del self.subprocesses[server_name]
    
    def _discover_tools(self, server_name: str, ai_description: str) -> List[Dict]:
        """Send tools/list request to server and register discovered tools"""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        }
        
        try:
            response = self._send_request(server_name, request)
            if not response or 'result' not in response:
                MCPLogger.log(TOOL_LOG_NAME, f"Invalid tools/list response from {server_name}: {response}")
                return []
            
            tools = response['result'].get('tools', [])
            
            # Register each tool
            for tool in tools:
                tool_name = tool.get('name')
                if not tool_name:
                    continue
                
                #wrapped_tool_name = f"mcp_{{server_name}}_local_{server_name}_{tool_name}" # ragtag_sse
                wrapped_tool_name = f"mcp_{TOOL_INTERNAL_NAME}_local_{server_name}_{tool_name}"
                
                # Store tool info for later use
                self.tool_registry[wrapped_tool_name] = {
                    'server_name': server_name,
                    'original_tool_name': tool_name,
                    'original_schema': tool,
                    'ai_description': ai_description
                }
                
                MCPLogger.log(TOOL_LOG_NAME, f"Registered tool: {wrapped_tool_name}")
            
            return tools
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to discover tools from {server_name}: {str(e)}")
            return []
    
    def _send_request(self, server_name: str, request: Dict) -> Optional[Dict]:
        """Send a JSON-RPC request to a server and get response"""
        if server_name not in self.subprocesses:
            return None
        
        proc = self.subprocesses[server_name]
        
        try:
            # Send request
            request_json = json.dumps(request) + '\n'
            MCPLogger.log(TOOL_LOG_NAME, f"Sending to {server_name}: {request_json.strip()}")
            
            proc.stdin.write(request_json)
            proc.stdin.flush()
            
            # Read response
            response_line = proc.stdout.readline()
            if not response_line:
                MCPLogger.log(TOOL_LOG_NAME, f"No response from {server_name}")
                return None
            
            MCPLogger.log(TOOL_LOG_NAME, f"Received from {server_name}: {response_line.strip()}")
            
            # Check for stderr output (Windows-compatible approach)
            try:
                # Try to read stderr without blocking
                import msvcrt
                import sys
                if hasattr(proc.stderr, 'fileno'):
                    # This is a simplified approach - just try to read if available
                    # On Windows, we can't easily do non-blocking reads, so we'll skip this for now
                    pass
            except (ImportError, AttributeError):
                # Not on Windows or stderr not available
                pass
            
            return json.loads(response_line)
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error communicating with {server_name}: {str(e)}")
            return None
    
    def execute_tool(self, tool_name: str, params: Dict) -> Dict:
        """Execute a tool call on the appropriate MCP server"""
        if tool_name not in self.tool_registry:
            return create_error_response(f"Unknown tool: {tool_name}", with_readme=False)
        
        tool_info = self.tool_registry[tool_name]
        server_name = tool_info['server_name']
        original_tool_name = tool_info['original_tool_name']
        
        if server_name not in self.subprocesses:
            return create_error_response(f"Server {server_name} is not available.", with_readme=False)
        
        # Acquire lock for this server (serialize requests)
        with self.subprocess_locks[server_name]:
            try:
                # Increment request counter
                self.request_counters[server_name] += 1
                request_id = self.request_counters[server_name]
                
                # Build JSON-RPC request
                request = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": original_tool_name,
                        "arguments": params
                    }
                }
                
                # Send request and get response
                response = self._send_request(server_name, request)
                
                if not response:
                    return create_error_response(f"No response from server {server_name}", with_readme=False)
                
                if 'error' in response:
                    error = response['error']
                    error_msg = f"Server error: {error.get('message', 'Unknown error')}"
                    return create_error_response(error_msg, with_readme=False)
                
                if 'result' not in response:
                    return create_error_response(f"Invalid response from server {server_name}", with_readme=False)
                
                # Return the result in our standard format
                result = response['result']
                
                # Handle different result formats
                if isinstance(result, dict) and 'content' in result:
                    # Already in MCP format
                    return {
                        "content": result['content'],
                        "isError": False
                    }
                elif isinstance(result, str):
                    # Simple string result
                    return {
                        "content": [{"type": "text", "text": result}],
                        "isError": False
                    }
                else:
                    # Convert other formats to text
                    return {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        "isError": False
                    }
                
            except Exception as e:
                return create_error_response(f"Error executing tool {tool_name}: {str(e)}", with_readme=False)
    
    def get_available_tools(self) -> List[Dict]:
        """Get list of all available tools for registration with SSE server"""
        self.ensure_initialized()
        
        # Group tools by server
        servers = {}
        for tool_name, tool_info in self.tool_registry.items():
            server_name = tool_info['server_name']
            if server_name not in servers:
                servers[server_name] = {
                    'ai_description': tool_info['ai_description'],
                    'operations': []
                }
            servers[server_name]['operations'].append({
                'name': tool_info['original_tool_name'],
                'schema': tool_info['original_schema']
            })
        
        tools = []
        for server_name, server_info in servers.items():
            # Create unified tool name (replace hyphens with underscores for valid identifiers)
            unified_tool_name = server_name.replace('-', '_')
            
            # Build operation list for readme
            operation_list = []
            operation_schemas = {}
            for op in server_info['operations']:
                op_name = op['name']
                op_schema = op['schema']
                operation_list.append(f"- {op_name}: {op_schema.get('description', 'No description')}")
                operation_schemas[op_name] = op_schema
            
            # Create unified tool definition
            tool_def = {
                "name": unified_tool_name,
                "description": f"{server_info['ai_description']}\n", # TODO: document to users to add this key into their JSNO;       "ai_description": "use this tool when you need to perform file-based operations on the users PC",
                "parameters": {
                    "properties": {
                        "input": {
                            "type": "object",
                            "description": f"All tool parameters are passed in this single dict. Use {{\"input\":{{\"operation\":\"readme\"}}}} to get full documentation, parameters, and an unlock token."
                        }
                    },
                    "required": [],
                    "type": "object"
                },
                "real_parameters": {
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["readme"] + [op['name'] for op in server_info['operations']],
                            "description": "Operation to perform"
                        },
                        "tool_unlock_token": {
                            "type": "string",
                            "description": f"Security token, {TOOL_UNLOCK_TOKEN}, obtained from readme operation"
                        }
                    },
                    "required": ["operation", "tool_unlock_token"],
                    "type": "object"
                },
                "server_name": server_name,  # Store for handler use
                "operation_schemas": operation_schemas,  # Store schemas for validation
                "readme": f"""## Available Operations

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: {TOOL_UNLOCK_TOKEN}

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {{
     "input": {{"operation": "readme"}}
   }}

2. For executing operations:
   {{
     "input": {{
       "operation": "operation_name", 
       "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
       ... additional parameters specific to the operation ...
     }}
   }}

## Operation Schemas
{self._generate_operation_documentation(operation_schemas)}
"""
            }
            tools.append(tool_def)
        
        return tools
    
    def _generate_operation_documentation(self, operation_schemas: Dict) -> str:
        """Generate documentation for all operations in a server"""
        docs = []
        for op_name, op_schema in operation_schemas.items():
            input_schema = op_schema.get('inputSchema', {})
            properties = input_schema.get('properties', {})
            required = input_schema.get('required', [])
            
            # Generate parameter examples
            param_examples = []
            for prop_name, prop_schema in properties.items():
                prop_type = prop_schema.get('type', 'string')
                prop_desc = prop_schema.get('description', '')
                
                if prop_type == 'string':
                    example_value = f'"example_{prop_name}"'
                elif prop_type == 'number' or prop_type == 'integer':
                    example_value = '123'
                elif prop_type == 'boolean':
                    example_value = 'true'
                elif prop_type == 'array':
                    example_value = '["item1", "item2"]'
                elif prop_type == 'object':
                    example_value = '{}'
                else:
                    example_value = f'"example_{prop_name}"'
                
                required_marker = " // REQUIRED" if prop_name in required else ""
                param_examples.append(f'       "{prop_name}": {example_value}{required_marker}  // {prop_desc}')
            
            param_section = ',\n'.join(param_examples) if param_examples else '       // No additional parameters'
            
            docs.append(f"""
### {op_name}
{op_schema.get('description', 'No description available')}

Example usage:
{{
  "input": {{
    "operation": "{op_name}",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
{param_section}
  }}
}}
""")
        
        return '\n'.join(docs)

    def _generate_parameter_examples(self, schema: Dict) -> str:
        """Generate parameter examples from original schema"""
        input_schema = schema.get('inputSchema', {})
        properties = input_schema.get('properties', {})
        required = input_schema.get('required', [])
        
        examples = []
        for prop_name, prop_schema in properties.items():
            prop_type = prop_schema.get('type', 'string')
            if prop_type == 'string':
                example_value = f'"example_{prop_name}"'
            elif prop_type == 'number' or prop_type == 'integer':
                example_value = '123'
            elif prop_type == 'boolean':
                example_value = 'true'
            elif prop_type == 'array':
                example_value = '["item1", "item2"]'
            elif prop_type == 'object':
                example_value = '{}'
            else:
                example_value = f'"example_{prop_name}"'
            
            required_marker = " // REQUIRED" if prop_name in required else ""
            examples.append(f'       "{prop_name}": {example_value}{required_marker}')
        
        return ',\n'.join(examples)

# Global bridge instance
_bridge = MCPBridge()

# Tool definitions - will be populated dynamically
TOOLS = []

# Map of tool names to their handlers - will be populated dynamically
HANDLERS = {}

def get_dynamic_tools():
    """Get the current list of tools (called by SSE server)"""
    return _bridge.get_available_tools()

# UNUSED:-
def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters for MCP bridge operations.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]
        expected_params = {"operation"}
    elif operation == "execute":
        # For execute, we need the token plus any tool-specific parameters
        required = ["operation", "tool_unlock_token"]
        expected_params = set(input_param.keys())  # Accept any parameters for tool execution
    else:
        return f"Invalid operation: '{operation}'. Must be 'readme' or 'execute'.", {}
    
    # Check for missing required parameters
    provided_params = set(input_param.keys())
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}. Required parameters are: {', '.join(sorted(required))}", {}
    
    # Basic type validation for core parameters
    if "operation" in input_param and not isinstance(input_param["operation"], str):
        return f"Parameter 'operation' must be a string, got {type(input_param['operation']).__name__}.", {}
    
    if "tool_unlock_token" in input_param and not isinstance(input_param["tool_unlock_token"], str):
        return f"Parameter 'tool_unlock_token' must be a string, got {type(input_param['tool_unlock_token']).__name__}.", {}
    
    return None, input_param

# This makes no sense - need to remove it - local servers have their own readmes...
def readme(with_readme: bool = True) -> str:
    """Return tool documentation.
    
    Args:
        with_readme: If False, returns empty string. If True, returns the complete tool documentation.
        
    Returns:
        The complete tool documentation or empty string if with_readme is False.


SHOULD BE:-


        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            # Get tool-specific readme from bridge
            tools = _bridge.get_available_tools()
            for tool_def in tools:
                # Match against the clean tool name (without mcp_ragtag_sse_ prefix)
                clean_tool_def_name = tool_def["name"]
                if tool_name.startswith(f"mcp_{TOOL_INTERNAL_NAME}_"):
                    expected_name = tool_name[len(f"mcp_{TOOL_INTERNAL_NAME}_"):]
                else:
                    expected_name = tool_name
                    
                if clean_tool_def_name == expected_name:
                    return {
                        "content": [{"type": "text", "text": tool_def["readme"]}],
                        "isError": False
                    }
            return create_error_response(f"Tool {tool_name} not found or not available.", with_readme=False)
            





    """
    try:
        if not with_readme:
            return ''
            
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request for MCP bridge")
        
        # Initialize bridge to get available tools
        _bridge.ensure_initialized()
        
        readme_content = LAST_TOOL["readme"] if LAST_TOOL else ""

#    
#            available_tools = list(_bridge.tool_registry.keys()) # wrong: mcp_ragtag_sse_local_github_add_comment_to_pending_review ...
#            
#            readme_content = f"""
#    MCP Bridge Tool - Connects to external MCP servers and proxies their tools
#    
#    This tool automatically discovers and connects to MCP servers configured in your Claude Desktop configuration.
#    It reads from: C:/Users/{{username}}/AppData/Roaming/Claude/claude_desktop_config.json
#    
#    ## Currently Available Tools
#    {chr(10).join(f"- {tool}" for tool in available_tools) if available_tools else "No tools available (check configuration and server status)"}
#    
#    ## Usage-Safety Token System
#    This tool uses an hmac-based token system to ensure callers fully understand all details of
#    using this tool, on every call. The token is specific to this installation, user, and code version.
#    
#    Your tool_unlock_token for this installation is: {TOOL_UNLOCK_TOKEN}
#    
#    ## How to Use
#    1. Call the specific tool you want to use (e.g., mcp_{{server_name}}_local_desktop_commander_read_file)
#    2. Use operation="readme" to get documentation for that specific tool
#    3. Use operation="execute" with the tool_unlock_token to execute the tool
#    
#    ## Configuration
#    The bridge reads MCP server configurations from Claude Desktop's config file.
#    Each server in the mcpServers section will be started and its tools will be made available.
#    
#    ## Error Handling
#    - If a server fails to start, its tools will not be available
#    - If a server crashes during operation, calls to its tools will return errors
#    - No automatic restart is performed - server issues must be resolved manually
#    """
        
        return "\n\n" + json.dumps({
            "description": readme_content,
            #    "available_tools": available_tools,
            "bridge_status": {
                "initialized": _bridge.initialized,
                "active_servers": list(_bridge.subprocesses.keys()),
                "total_tools": len(_bridge.tool_registry)
            }
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

def find_tool_definition(tool_name: str) -> Optional[Dict]:
    """Find and return the tool definition for a given tool name.
    
    Args:
        tool_name: The full tool name (e.g., "mcp_{serverName}_local_github_get_issue")
        
    Returns:
        The tool definition dict if found, None otherwise.
        Also sets LAST_TOOL global as a side effect.
    """
    global LAST_TOOL
    
    _bridge.ensure_initialized()
    tools = _bridge.get_available_tools()
    
    for tool_def in tools:
        clean_tool_def_name = tool_def["name"]
        
        # Strip the MCP prefix to get the expected name
        if tool_name.startswith(f"mcp_{TOOL_INTERNAL_NAME}_"):
            expected_name = tool_name[len(f"mcp_{TOOL_INTERNAL_NAME}_"):]
        else:
            expected_name = tool_name
            
        if clean_tool_def_name == expected_name:
            LAST_TOOL = tool_def
            return tool_def
    
    return None

def handle_local_tool_call(input_param: Dict) -> Dict:
    """Handle MCP bridge tool operations via MCP interface."""
    try:

        handler_info = input_param.pop('handler_info', None) # Pop off synthetic handler_info parameter early (before validation); This is added by the server for tools that need dynamic routing

        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        # Extract tool name from handler_info to determine which tool was called
        tool_name = None
        if handler_info and isinstance(handler_info, dict):
            # handler_info format: {tool_name: tool_name}
            tool_name = list(handler_info.values())[0] if handler_info else None
        
        if not tool_name:
            return create_error_response("Internal error: could not determine which tool was called", with_readme=False)

        # Ensure bridge is initialized
        _bridge.ensure_initialized()
        
        # Extract server name from tool name
        # Tool name format: "mcp_ragtag_sse_desktop_commander" -> server name: "desktop-commander"
        # But our internal tool names are just "desktop_commander" -> server name: "desktop-commander"
        server_name = None
        if tool_name.startswith(f"mcp_{TOOL_INTERNAL_NAME}_"):
            # Remove the MCP prefix that was added by the SSE server
            clean_tool_name = tool_name[len(f"mcp_{TOOL_INTERNAL_NAME}_"):]
            server_name = clean_tool_name.replace("_", "-")
        else:
            # Direct tool name without prefix
            server_name = tool_name.replace("_", "-")
        
        if not server_name:
            return create_error_response(f"Could not extract server name from tool: {tool_name}", with_readme=False)
        
        # Check if this server exists in our bridge
        if server_name not in _bridge.subprocesses:
            return create_error_response(f"Server {server_name} is not available", with_readme=False)

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            tool_def = find_tool_definition(tool_name)
            if tool_def:
                return {
                    "content": [{"type": "text", "text": tool_def["readme"]}],
                    "isError": False
                }
            return create_error_response(f"Tool {tool_name} not found or not available.", with_readme=False)
            
        # Validate input structure first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=False)
            
        # Check for operation parameter
        operation = input_param.get("operation")
        if not operation:
            return create_error_response("Missing 'operation' parameter. Use 'readme' to see available operations.", with_readme=False)
        
        # Check if operation is valid for this server
        # Find the tool definition and operation schema
        tool_def = find_tool_definition(tool_name)
        if not tool_def:
            return create_error_response(f"Tool {tool_name} not found or not available.", with_readme=False)
        
        operation_schemas = tool_def.get("operation_schemas", {})
        
        if operation not in operation_schemas:
            available_ops = list(operation_schemas.keys())
            return create_error_response(f"Unknown operation '{operation}' for {server_name}. Available operations: {', '.join(available_ops)}", with_readme=False) # Available operations: add_comment_to_pending_review, add_issue_comment, add_sub_issue, assign_copilot_to_issue, cancel_workflow_run, create_and_submit_pull_request_review, create_branch, create_issue, create_or_update_file, create_pending_pull_request_review, create_pull_request, create_repository, delete_file, delete_pending_pull_request_review, delete_workflow_run_logs, dismiss_notification, download_workflow_run_artifact, fork_repository, get_code_scanning_alert, get_commit, get_dependabot_alert, get_discussion, get_discussion_comments, get_file_contents, get_issue, get_issue_comments, get_job_logs, get_me, get_notification_details, get_pull_request, get_pull_request_comments, get_pull_request_diff, get_pull_request_files, get_pull_request_reviews, get_pull_request_status, get_secret_scanning_alert, get_tag, get_workflow_run, get_workflow_run_logs, get_workflow_run_usage, list_branches, list_code_scanning_alerts, list_commits, list_dependabot_alerts, list_discussion_categories, list_discussions, list_issues, list_notifications, list_pull_requests, list_secret_scanning_alerts, list_sub_issues, list_tags, list_workflow_jobs, list_workflow_run_artifacts, list_workflow_runs, list_workflows, manage_notification_subscription, manage_repository_notification_subscription, mark_all_notifications_read, merge_pull_request, push_files, remove_sub_issue, reprioritize_sub_issue, request_copilot_review, rerun_failed_jobs, rerun_workflow_run, run_workflow, search_code, search_issues, search_orgs, search_pull_requests, search_repositories, search_users, submit_pending_pull_request_review, update_issue, update_pull_request, update_pull_request_branch
        
        # Check for token (not required for readme)
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token.", with_readme=True)

        # Remove our control parameters and pass the rest to the MCP server
        tool_params = {k: v for k, v in input_param.items() 
                     if k not in ["operation", "tool_unlock_token"]}
        
        # Find the original tool name in our registry
        original_tool_name = None
        for registered_tool_name, registered_tool_info in _bridge.tool_registry.items():
            if (registered_tool_info['server_name'] == server_name and 
                registered_tool_info['original_tool_name'] == operation):
                original_tool_name = registered_tool_name
                break
        
        if not original_tool_name:
            return create_error_response(f"Internal error: could not find registration for {server_name}.{operation}", with_readme=False)
        
        # Execute the tool via bridge
        return _bridge.execute_tool(original_tool_name, tool_params)
            
    except Exception as e:
        return create_error_response(f"Error in MCP bridge operation: {str(e)}", with_readme=True)

def get_tools_and_handlers():
    """Get both TOOLS and HANDLERS for the SSE server (ensures initialization)"""
    global TOOLS, HANDLERS
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Starting get_tools_and_handlers()")
        
        # Ensure bridge is initialized
        _bridge.ensure_initialized()
        MCPLogger.log(TOOL_LOG_NAME, f"Bridge initialized: {_bridge.initialized}")
        
        # Get current tools
        TOOLS = _bridge.get_available_tools()
        MCPLogger.log(TOOL_LOG_NAME, f"Got {len(TOOLS)} tools from bridge")
        
        # Create handlers for each tool - all use the same handler function
        HANDLERS = {}
        for i, tool_def in enumerate(TOOLS):
            tool_name = tool_def["name"]
            MCPLogger.log(TOOL_LOG_NAME, f"Creating handler {i+1}/{len(TOOLS)} for tool: {tool_name}")
            try:
                HANDLERS[tool_name] = handle_local_tool_call
                MCPLogger.log(TOOL_LOG_NAME, f"Successfully created handler for: {tool_name}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Failed to create handler for {tool_name}: {str(e)}")
        
        MCPLogger.log(TOOL_LOG_NAME, f"Final HANDLERS keys: {list(HANDLERS.keys())}")
        MCPLogger.log(TOOL_LOG_NAME, f"Returning {len(TOOLS)} tools and {len(HANDLERS)} handlers")
        return TOOLS, HANDLERS
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error getting tools and handlers: {str(e)}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Full traceback: {traceback.format_exc()}")
        return [], {}

# Initialize on module load
try:
    TOOLS, HANDLERS = get_tools_and_handlers()
    MCPLogger.log(TOOL_LOG_NAME, f"MCP Bridge initialized with {len(HANDLERS)} tools. Tool names: {list(HANDLERS.keys())}")
except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize MCP Bridge: {str(e)}")
    # Continue with empty handlers - tools won't be available but server won't crash
    TOOLS = []
    HANDLERS = {}

#//TOOLS = [] # temp-disable this tool.
