"""
File: ragtag/tools/local.py
Project: Aura Friday MCP-Link Server
Component: Local MCP Bridge
Author: Christopher Nathan Drake (cnd)

A shim that connects to external MCP servers via STDIO and proxies their tools through our SSE transport.
Reads settings[0].local_mcpServers from the shared config (each entry uses the same per-server shape
as Claude Desktop's claude_desktop_config.json "mcpServers" entries) to discover servers and their tools.

Per-server config keys (settings[0].local_mcpServers.<serverName>):
- command / args : executable and argv for the STDIO server subprocess.
- env            : optional dict of environment variables passed to that subprocess (e.g. API tokens).
- enabled        : must be explicitly true before the server is ever spawned. (#B2) command/args come
                   from this externally-writable config file, so launching a configured server is
                   arbitrary command execution BY DESIGN; enabled:true is the user's explicit opt-in
                   gate, and every spawn is audit-logged (argv plus env var names, never values).
- ai_description : optional text shown to AI clients as the bridged tool's description. (#D5)

Operational notes:
- (#A5) Discovery runs ONCE per process (on a background thread after startup). Edits to
  local_mcpServers take effect only after a server restart; there is no runtime reload path.
- (#B1) Subprocesses do NOT inherit the parent's environment (which holds API keys and secrets);
  they receive a minimal system allowlist plus the per-server 'env' map only.
- (#B3) Operation arguments are forwarded to the external server verbatim (pass-through proxy);
  no schema-level validation happens on our side - the child validates its own inputs.
- (#D2) Protocol framing is line-delimited JSON: exactly one JSON-RPC message per stdout line.
  Servers that pretty-print multi-line JSON to stdout are not supported (child stdout must carry
  protocol traffic only).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ᛕȣⲦ𝟧ȠmΑcᏟtaƋЈᴅԛģjuΗϹЕКɊս𝕌1GϹЅ𝟑𝙰𝛢һбƶƻ9ÞԝGnЕᏟꓬeȠοUoŧƱdcSȜКոⲔvjμþNƱӠΒꓑ𝟙οƬ9Yþ𝟥ⲟᴜoCᏎВΜȢΡꓬᏎJꓟĵɗObԛƦ𝟤ϹΗꙄΕDƋꓪꙄᴠBɋƌᎠ3ᑕ",
"signdate": "2026-07-29T09:30:29.395Z",
"""

import atexit  # (#C3) shutdown cleanup of bridged subprocesses
import json
import os
import platform
import queue
import subprocess
import threading
import time
from typing import Dict, List, Optional  # (#A6) Tuple/Union/BinaryIO/Any/pathlib.Path dropped: unused (their only users were the removed dead code)
from easy_mcp.server import MCPLogger, get_tool_token

# Windows-specific constants for hiding console windows
if platform.system() == "Windows":
    CREATE_NO_WINDOW = 0x08000000  # from winbase.h
else:
    CREATE_NO_WINDOW = 0

# Constants
TOOL_LOG_NAME = "LOCAL"
# (#C5) The old TOOL_INTERNAL_NAME = "{serverName}" placeholder is gone: it was never
# substituted, so internal registry keys literally contained the text "{serverName}".
# Registry keys now use the plain internal "local_<server>_<tool>" form (see _discover_tools).
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")  # (#C1) multi-machine tool-name suffix, same convention as the static tool modules

# (#B1) The ONLY parent environment variables a bridged subprocess may inherit: process-launch
# basics (PATH etc.), per-user app-data/temp locations, and locale/identity values that
# runtimes like node/npx/uvx need. API keys, bearer secrets, and everything else in our
# environment are deliberately withheld; per-server needs belong in that server's config 'env'.
SAFE_SYSTEM_ENVIRONMENT_VARIABLES_FOR_BRIDGED_SUBPROCESSES = frozenset({
    'PATH', 'PATHEXT', 'COMSPEC', 'SHELL',
    'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR',
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'PUBLIC',
    'HOME', 'HOMEDRIVE', 'HOMEPATH', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
    'TEMP', 'TMP', 'TMPDIR',
    'USERNAME', 'USER', 'LOGNAME', 'USERDOMAIN', 'COMPUTERNAME', 'HOSTNAME',
    'LANG', 'LC_ALL', 'TZ',
    'OS', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS',
})

# tool_unlock_token = a COMPREHENSION GATE, NOT authentication and NOT a secret (see
# doc/50_non-AI-calling-and-how-to-get-unlock-tokens.md). get_tool_token(__file__) derives it
# from this file's bytes, so it ROTATES when this bridge's code changes. NOTE -- local.py is the
# ODD ONE OUT: the external STDIO servers we bridge have no unlock-token concept of their own, so
# every bridged tool is assigned THIS bridge's shared token (see TOOL_TOKENS[...] near the end of
# this file) purely as a "you have read the bridged tool's readme" gate. It is handed out freely
# via readme, must never be treated as auth, and bridged calls are otherwise pass-through (the
# child server validates its own args -- see #B3 in the module docstring).
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Global state for MCP bridge
class MCPBridge:
    def __init__(self):
        self.subprocesses: Dict[str, subprocess.Popen] = {}
        self.subprocess_locks: Dict[str, threading.Lock] = {}
        self.subprocess_stdout_queues: Dict[str, "queue.Queue[Optional[str]]"] = {}
        self.subprocess_reader_threads: Dict[str, List[threading.Thread]] = {}
        self.tool_registry: Dict[str, Dict] = {}  # tool_name -> {server_name, original_tool_name, schema}
        self.request_counters: Dict[str, int] = {}  # server_name -> counter for JSON-RPC IDs
        self.timed_out_request_ids_by_server: Dict[str, set] = {}  # (#D1) server_name -> ids whose request timed out; their late responses are discarded explicitly, never mis-read
        self.cached_unified_tool_definitions: Optional[List[Dict]] = None  # (#D4) built once after discovery; registry only mutates during (one-shot) discovery / server stop
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
    
    
    def _build_minimal_subprocess_environment(self, server_name: str, server_config: Dict) -> Dict[str, str]:
        """(#B1) Build the child environment: a safe system allowlist from our own environment,
        overlaid with the per-server 'env' map from config. The parent's full environment
        (API keys, tokens, secrets) is never passed through to third-party server binaries."""
        child_environment = {
            name: value for name, value in os.environ.items()
            if name.upper() in SAFE_SYSTEM_ENVIRONMENT_VARIABLES_FOR_BRIDGED_SUBPROCESSES
        }
        per_server_env = server_config.get('env', {})
        if isinstance(per_server_env, dict):
            for name, value in per_server_env.items():
                child_environment[str(name)] = str(value)
        return child_environment

    def _start_server(self, server_name: str, server_config: Dict):
        """Start an MCP server subprocess and discover its tools"""
        command = server_config.get('command')
        args = server_config.get('args', [])
        ai_description = server_config.get('ai_description', f'Use this tool when you need to access {server_name} functionality')
        
        if not command:
            MCPLogger.log(TOOL_LOG_NAME, f"No command specified for server {server_name}")
            return
        
        full_command = [command] + args
        child_environment = self._build_minimal_subprocess_environment(server_name, server_config)
        # (#B2) Loud audit log of exactly what is spawned: full argv plus the NAMES of the
        # environment variables handed to the child (values never logged - they may be tokens).
        MCPLogger.log(TOOL_LOG_NAME, f"AUDIT spawn {server_name}: argv={full_command} env_keys={sorted(child_environment.keys())}")
        
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
                env=child_environment,  # (#B1) minimal allowlist + config 'env', never the full parent environment
                creationflags=CREATE_NO_WINDOW
            )
            
            self.subprocesses[server_name] = proc
            self.subprocess_locks[server_name] = threading.Lock()
            # (#D3) Start the runtime JSON-RPC id counter ABOVE the discovery-phase ids
            # (initialize=0, tools/list=1) so a late/stale discovery response can never share
            # an id with the first runtime tools/call (which now uses id 2).
            self.request_counters[server_name] = 1
            self.timed_out_request_ids_by_server[server_name] = set()
            self._start_subprocess_pipe_reader_threads(server_name, proc)
            
            if not self._send_mcp_initialize_handshake(server_name):
              MCPLogger.log(TOOL_LOG_NAME, f"MCP initialize handshake failed for {server_name}, skipping tool discovery")
              self._stop_server(server_name)
              return
            
            # Discover tools
            tools = self._discover_tools(server_name, ai_description)
            MCPLogger.log(TOOL_LOG_NAME, f"Server {server_name} provided {len(tools)} tools")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to start server {server_name}: {str(e)}")
            if server_name in self.subprocesses:
                self._stop_server(server_name)

    def _start_subprocess_pipe_reader_threads(self, server_name: str, proc: subprocess.Popen) -> None:
        """Read subprocess pipes on background threads so Windows pipe handles do not need select()."""
        stdout_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self.subprocess_stdout_queues[server_name] = stdout_queue

        def read_stdout_until_subprocess_exits() -> None:
            try:
                if proc.stdout is None:
                    stdout_queue.put(None)
                    return
                for stdout_line in proc.stdout:
                    stdout_queue.put(stdout_line)
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"stdout reader for {server_name} failed: {str(e)}")
            finally:
                stdout_queue.put(None)

        def log_stderr_until_subprocess_exits() -> None:
            try:
                if proc.stderr is None:
                    return
                for stderr_line in proc.stderr:
                    clean_stderr_line = stderr_line.strip()
                    if clean_stderr_line:
                        MCPLogger.log(TOOL_LOG_NAME, f"stderr from {server_name}: {clean_stderr_line[:1000]}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"stderr reader for {server_name} failed: {str(e)}")

        stdout_thread = threading.Thread(
            target=read_stdout_until_subprocess_exits,
            name=f"local-mcp-stdout-{server_name}",
            daemon=True
        )
        stderr_thread = threading.Thread(
            target=log_stderr_until_subprocess_exits,
            name=f"local-mcp-stderr-{server_name}",
            daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        self.subprocess_reader_threads[server_name] = [stdout_thread, stderr_thread]

    def _stop_server(self, server_name: str) -> None:
        """Terminate a managed local MCP server and remove its bridge state."""
        proc = self.subprocesses.pop(server_name, None)
        self.subprocess_locks.pop(server_name, None)
        self.subprocess_stdout_queues.pop(server_name, None)
        self.subprocess_reader_threads.pop(server_name, None)
        self.request_counters.pop(server_name, None)
        self.timed_out_request_ids_by_server.pop(server_name, None)
        self.cached_unified_tool_definitions = None  # (#D4) tool list may no longer reflect this server

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                # (#C3) reap so POSIX children cannot linger as zombies; escalate to kill()
                # for servers that ignore the polite terminate
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Failed to terminate server {server_name}: {str(e)}")

    def shutdown_all_bridged_subprocesses(self) -> None:
        """(#C3) Stop every external MCP server subprocess at interpreter shutdown/restart
        (terminate -> wait -> kill) so no external processes are orphaned across relaunches."""
        for server_name, proc in list(self.subprocesses.items()):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)  # (#C3) reap the killed child too
                MCPLogger.log(TOOL_LOG_NAME, f"Shutdown cleanup: stopped bridged server {server_name}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Shutdown cleanup failed for {server_name}: {str(e)}")
    
    def _send_mcp_initialize_handshake(self, server_name: str) -> bool:
        """Send the MCP initialize handshake required by spec-compliant servers like codex."""
        initialize_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "aura-friday-mcp-bridge", "version": "1.0"}
            }
        }
        try:
            # (#C2) short handshake timeout so one slow/hung external server cannot stall discovery for the default 120s
            response = self._send_request(server_name, initialize_request, timeout_seconds=15)
            if response and "result" in response:
                server_info = response.get("result", {}).get("serverInfo", {})
                server_version = server_info.get("version", "?")
                server_title = server_info.get("name", server_name)
                MCPLogger.log(TOOL_LOG_NAME, f"MCP initialize OK for {server_name}: {server_title} v{server_version}")
                return True
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"MCP initialize returned unexpected response for {server_name}: {response}")
                return False
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"MCP initialize handshake exception for {server_name}: {e}")
            return False
    
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
                
                # (#C5) plain internal registry key; the old "mcp_{serverName}_" placeholder
                # prefix was never substituted and served no routing purpose
                wrapped_tool_name = f"local_{server_name}_{tool_name}"
                
                # Store tool info for later use
                self.tool_registry[wrapped_tool_name] = {
                    'server_name': server_name,
                    'original_tool_name': tool_name,
                    'original_schema': tool,
                    'ai_description': ai_description
                }
                
                MCPLogger.log(TOOL_LOG_NAME, f"Registered tool: {wrapped_tool_name}")
            
            self.cached_unified_tool_definitions = None  # (#D4) registry changed; rebuild on next read
            return tools
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to discover tools from {server_name}: {str(e)}")
            return []
    
    def _send_request(self, server_name: str, request: Dict, timeout_seconds: int = 120) -> Optional[Dict]:
        """Send a JSON-RPC request to a server and get the matching response.
        
        Reads lines until a JSON-RPC response with the matching request id is found,
        skipping notifications (lines without an id or with a non-matching id).
        Servers like codex mcp-server emit many notification lines before the result.
        """
        if server_name not in self.subprocesses:
            return None
        
        proc = self.subprocesses[server_name]
        request_id = request.get("id")
        
        try:
            request_json = json.dumps(request) + '\n'
            MCPLogger.log(TOOL_LOG_NAME, f"Sending to {server_name}: {request_json.strip()}")
            
            if proc.stdin is None:
                MCPLogger.log(TOOL_LOG_NAME, f"Process for {server_name} has no stdin")
                return None

            proc.stdin.write(request_json)
            proc.stdin.flush()

            stdout_queue = self.subprocess_stdout_queues.get(server_name)
            if stdout_queue is None:
                MCPLogger.log(TOOL_LOG_NAME, f"Process for {server_name} has no stdout reader queue")
                return None

            deadline = time.time() + timeout_seconds
            notification_count = 0
            
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                
                try:
                    response_line = stdout_queue.get(timeout=min(remaining, 5.0))
                except queue.Empty:
                    if proc.poll() is not None:
                        MCPLogger.log(TOOL_LOG_NAME, f"Process for {server_name} has exited (code {proc.returncode})")
                        return None
                    continue
                
                if response_line is None:
                    MCPLogger.log(TOOL_LOG_NAME, f"EOF from {server_name}")
                    return None
                
                response_line = response_line.strip()
                if not response_line:
                    continue
                
                try:
                    parsed = json.loads(response_line)
                except json.JSONDecodeError:
                    MCPLogger.log(TOOL_LOG_NAME, f"Non-JSON line from {server_name}: {response_line[:200]}")
                    continue
                
                if "id" in parsed and parsed["id"] == request_id:
                    if notification_count > 0:
                        MCPLogger.log(TOOL_LOG_NAME, f"Received response from {server_name} after {notification_count} notifications")
                    else:
                        MCPLogger.log(TOOL_LOG_NAME, f"Received from {server_name}: {response_line[:500]}")
                    return parsed
                elif "id" in parsed and parsed["id"] in self.timed_out_request_ids_by_server.get(server_name, set()):
                    # (#D1) late response to a request that already timed out: discard it
                    # explicitly (and visibly) instead of miscounting it as a notification
                    self.timed_out_request_ids_by_server[server_name].discard(parsed["id"])
                    MCPLogger.log(TOOL_LOG_NAME, f"Discarded stale response id={parsed['id']} from {server_name} (its request timed out earlier)")
                else:
                    notification_count += 1
            
            # (#D1) tag the abandoned id so its late response (if it ever arrives) is
            # recognised and discarded by a subsequent read instead of being misread
            self.timed_out_request_ids_by_server.setdefault(server_name, set()).add(request_id)
            MCPLogger.log(TOOL_LOG_NAME, f"Timeout waiting for response from {server_name} after {timeout_seconds}s ({notification_count} notifications received)")
            return None
            
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
        
        # (#D4) Serve the cached list: this runs on every status request, homepage render and
        # bridged tool call, and the registry only mutates during one-shot discovery/stop.
        if self.cached_unified_tool_definitions is not None:
            return self.cached_unified_tool_definitions
        
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
            # (#C1) append the multi-machine TOOL_SUFFIX like the static tool modules do;
            # HANDLERS stays in sync automatically because it is keyed off this tool_def["name"].
            unified_tool_name = server_name.replace('-', '_') + TOOL_NAME_SUFFIX
            
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
                "description": f"{server_info['ai_description']}\n",  # (#D5) the ai_description config key is documented in the module docstring
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
        
        self.cached_unified_tool_definitions = tools  # (#D4)
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

# Global bridge instance
_bridge = MCPBridge()

# Tool definitions - will be populated dynamically
TOOLS = []

# Map of tool names to their handlers - will be populated dynamically
HANDLERS = {}

def get_dynamic_tools():
    """Get the current list of tools (called by SSE server)"""
    return _bridge.get_available_tools()

def create_error_response(error_msg: str, with_readme: bool = True, tool_def_for_readme: Optional[Dict] = None) -> Dict:
    """Log and Create an error response that optionally includes the failing tool's documentation.

    (#A4/#A3) The readme text now comes from the tool definition the caller passes explicitly;
    the old path called the broken module-level readme() which returned whichever tool a
    previous request had cached in the LAST_TOOL global (i.e. possibly another server's docs).
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    readme_text = ""
    if with_readme and tool_def_for_readme:
        readme_text = "\n\n" + tool_def_for_readme.get("readme", "")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme_text}"}], "isError": True}

def find_tool_definition(tool_name: str) -> Optional[Dict]:
    """Find and return the tool definition for a given dispatched tool name.
    
    Args:
        tool_name: The registered unified tool name exactly as the server dispatched it
                   (from handler_info['tool_name']), e.g. "github" or "github_rog".
        
    Returns:
        The tool definition dict if found, None otherwise.
        (#A3) Returns to the caller only; the old LAST_TOOL global side effect is gone.
        (#C5) The old strip of the never-substituted "mcp_{serverName}_" placeholder prefix
        is gone too: dispatched names are always the exact registered names.
    """
    _bridge.ensure_initialized()
    tools = _bridge.get_available_tools()
    
    for tool_def in tools:
        if tool_def["name"] == tool_name:
            return tool_def
    
    return None

def handle_local_tool_call(input_param: Dict) -> Dict:
    """Handle MCP bridge tool operations via MCP interface."""
    try:

        # (#C4) Work on a shallow copy and read the synthetic handler_info via .get, so we never
        # mutate the caller's dict; handler_info is added by the server for dynamic routing.
        input_param = dict(input_param)
        handler_info = input_param.get('handler_info')
        input_param.pop('handler_info', None)  # keep it out of anything forwarded downstream

        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        # (#A2) Extract the called tool name from handler_info's documented 'tool_name' key
        tool_name = None
        if handler_info and isinstance(handler_info, dict):
            tool_name = handler_info.get('tool_name')
        
        if not tool_name:
            return create_error_response("Internal error: could not determine which tool was called", with_readme=False)

        # Ensure bridge is initialized
        _bridge.ensure_initialized()
        
        # (#A1) Look up the tool definition by its registered name and use the original
        # server_name stored on it, instead of reconstructing it via replace('_', '-')
        # which made any server whose real name contains '_' unreachable.
        tool_def = find_tool_definition(tool_name)
        if not tool_def:
            return create_error_response(f"Tool {tool_name} not found or not available.", with_readme=False)
        server_name = tool_def["server_name"]
        
        # Check if this server exists in our bridge
        if server_name not in _bridge.subprocesses:
            return create_error_response(f"Server {server_name} is not available", with_readme=False)

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": tool_def["readme"]}],
                "isError": False
            }
            
        # Validate input structure first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=False)
            
        # Check for operation parameter
        operation = input_param.get("operation")
        if not operation:
            return create_error_response("Missing 'operation' parameter. Use 'readme' to see available operations.", with_readme=False)
        
        operation_schemas = tool_def.get("operation_schemas", {})
        
        if operation not in operation_schemas:
            available_ops = list(operation_schemas.keys())
            return create_error_response(f"Unknown operation '{operation}' for {server_name}. Available operations: {', '.join(available_ops)}", with_readme=False) # Available operations: add_comment_to_pending_review, add_issue_comment, add_sub_issue, assign_copilot_to_issue, cancel_workflow_run, create_and_submit_pull_request_review, create_branch, create_issue, create_or_update_file, create_pending_pull_request_review, create_pull_request, create_repository, delete_file, delete_pending_pull_request_review, delete_workflow_run_logs, dismiss_notification, download_workflow_run_artifact, fork_repository, get_code_scanning_alert, get_commit, get_dependabot_alert, get_discussion, get_discussion_comments, get_file_contents, get_issue, get_issue_comments, get_job_logs, get_me, get_notification_details, get_pull_request, get_pull_request_comments, get_pull_request_diff, get_pull_request_files, get_pull_request_reviews, get_pull_request_status, get_secret_scanning_alert, get_tag, get_workflow_run, get_workflow_run_logs, get_workflow_run_usage, list_branches, list_code_scanning_alerts, list_commits, list_dependabot_alerts, list_discussion_categories, list_discussions, list_issues, list_notifications, list_pull_requests, list_secret_scanning_alerts, list_sub_issues, list_tags, list_workflow_jobs, list_workflow_run_artifacts, list_workflow_runs, list_workflows, manage_notification_subscription, manage_repository_notification_subscription, mark_all_notifications_read, merge_pull_request, push_files, remove_sub_issue, reprioritize_sub_issue, request_copilot_review, rerun_failed_jobs, rerun_workflow_run, run_workflow, search_code, search_issues, search_orgs, search_pull_requests, search_repositories, search_users, submit_pending_pull_request_review, update_issue, update_pull_request, update_pull_request_branch
        
        # Check for token (not required for readme)
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token.", with_readme=True, tool_def_for_readme=tool_def)  # (#A4) pass this tool's own def so its own docs are returned

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

# (#C2) Discovery is deferred out of module import: spawning and handshaking the external
# servers here used to stall server startup (up to ~120s per slow server, under suppressed
# output). TOOLS/HANDLERS start empty; on_all_tools_registered() below runs discovery on a
# background thread once the live server is ready, then registers the discovered tools.
LAZY_BRIDGE_DISCOVERY = True  # marker: discovery no longer happens at module import

def _discover_and_register_bridged_tools_in_background():
    """Background-thread body (#C2): spawn/handshake the configured external MCP servers,
    then register each discovered bridged tool with the live server (same runtime
    registration path remote.py uses), so startup is never stalled by slow servers."""
    try:
        from ragtag import tools as tools_package  # late import to avoid circular import at module load
        bridged_tool_definitions, bridged_tool_handlers = get_tools_and_handlers()
        server = tools_package.get_server()
        if server is None:
            MCPLogger.log(TOOL_LOG_NAME, "No server instance available; bridged tools not registered")
            return
        for tool_def in bridged_tool_definitions:
            server.register_tool(
                name=tool_def["name"],
                description=tool_def["description"],
                input_schema=tool_def["parameters"],
                handler=bridged_tool_handlers[tool_def["name"]]
            )
            # Keep the package token registry in step (import-time discovery used to fill it),
            # so programmatic get_unlock_token lookups keep working for bridged tools.
            tools_package.TOOL_TOKENS[tool_def["name"]] = TOOL_UNLOCK_TOKEN
            MCPLogger.log(TOOL_LOG_NAME, f"Registered bridged tool with live server: {tool_def['name']}")
        if bridged_tool_definitions:
            # Tools now appear after startup, so tell already-connected clients to re-list.
            # Debounced (was a direct send) for consistency with the other mutation paths --
            # doc/tools_list_changed_notification_gap_analysis_and_implementation_plan.md step 5
            # (safe: fires once at end of discovery, and collapses with any racing
            # remote-tool registrations instead of double-firing).
            server.schedule_tools_list_changed_notification_after_collapse_window()
        MCPLogger.log(TOOL_LOG_NAME, f"MCP Bridge background discovery complete: {len(bridged_tool_definitions)} tools. Tool names: {list(bridged_tool_handlers.keys())}")
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Background bridge discovery failed: {str(e)}")

def on_all_tools_registered():
    """Lifecycle hook (#C2): start background bridge discovery after set_server() and
    static tool registration are complete, so import/startup is never blocked."""
    threading.Thread(
        target=_discover_and_register_bridged_tools_in_background,
        name="local-mcp-bridge-discovery",
        daemon=True
    ).start()

# (#C3) Terminate external server subprocesses at interpreter shutdown/restart so they are
# not orphaned across relaunches (ragtag.py runs atexit handlers before restarting).
atexit.register(_bridge.shutdown_all_bridged_subprocesses)
