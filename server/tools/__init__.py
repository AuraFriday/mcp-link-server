"""
File: ragtag/tools/__init__.py
Project: Aura Friday MCP-Link Server
Component: Tool Registry
Author: Christopher Nathan Drake (cnd)

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƳÐ𝟑ʋʌƛꜱꓮᴍбGοɌꓪ9m4𝟑𝟥ƖVNvʈ7ƍꓴꓧᖴΜꞇᏟꓚX𝟑τꓓZᏟΥAGսÐυΕΒƋSꓮĐΒZꓦiʋⴹƻXꓜМVꞇ𝟙ѡʈᗪᖴlⅼHcуOⲘᴛıɌΝᏮᏴOꓰ8ꓔⲔᏎUȣƽ𝟙Ⲕ𝟚υΜᖴƼЕRƟ𝟙ꙄτƨⅠf৭Ln"
"signdate": "2026-07-19T02:59:12.809Z",
"""

import os,sys
import importlib
import importlib.util  # explicit: discovery below relies on importlib.util, don't depend on someone else importing it
import io
import json
import pkgutil
import traceback
from datetime import datetime
from typing import Optional, Dict, Any
from copy import deepcopy
from easy_mcp.server import MCPLogger

YEL = '\033[33;1m'
NORM = '\033[0m'

# Token registry: initialized EMPTY here at the TOP of the module (and populated
# in place after discovery completes, near the end of this file) so that
# get_tool_unlock_token_response() - called from server hot paths - returns None
# instead of raising NameError if invoked before this module finishes importing.
# Version 1.0 - increment if breaking changes are made to the token format or API
TOOL_TOKEN_API_VERSION = "1.0"
TOOL_TOKENS: Dict[str, str] = {}

# Tool modules that failed to import/load, so broken tools are visible rather
# than silently skipped.  Each entry: {'module': 'ragtag.tools.<name>', 'error': str}.
FAILED_MODULES: list = []

def get_failed_modules():
    """Return a snapshot list of tool modules that failed to load.

    Each entry is a dict: {'module': 'ragtag.tools.<name>', 'error': '<message>'}.
    """
    return list(FAILED_MODULES)

# Global server instance that tools can access directly
mcp_server = None

def get_server():
    """Get the global server instance."""
    global mcp_server
    return mcp_server

def set_server(server):
    """Set the global server instance."""
    global mcp_server
    mcp_server = server
    
    # Also set server for individual tools that have their own set_server function
    for module in discovered_modules:
        if hasattr(module, 'set_server'):
            try:
                MCPLogger.log("TOOLS", f"Setting server for tool module: {module.__name__}")
                module.set_server(server)
            except Exception as e:
                MCPLogger.log("TOOLS", f"{YEL}Error setting server for {module.__name__}: {str(e)}{NORM}")

def get_authenticated_user(handler_info: Dict[str, Any]) -> Optional[str]:
    """Extract the authenticated username from handler_info.
    
    Args:
        handler_info: Handler info dictionary passed to tool functions
        
    Returns:
        str: Authenticated username or None if not available
        
    Example:
        def handle_my_tool(input_param: Dict[str, Any]) -> Dict:
            handler_info = input_param.pop('handler_info', {})
            username = get_authenticated_user(handler_info)
            if username:
                print(f"Tool called by user: {username}")
    """
    if 'responder' in handler_info:
        server_instance = handler_info['responder']
        return getattr(server_instance, 'authenticated_user', None)
    return None

# Monkey-patch sys.exit to prevent tool modules from terminating the server during import
# Some libraries (like pywinctl/ewmhlib on Linux) may call sys.exit() when optional utilities are missing.
# NOTE: this guard is process-global and best-effort: while a tool import is executing,
# a sys.exit() from ANY thread lands in the guard (discovery itself is single-threaded),
# and os._exit() or a SystemExit raised directly (e.g. by C extensions) bypasses it entirely.
_original_sys_exit = sys.exit

# True only while a tool module's top-level code is executing inside discover_tools()
_exit_protection_is_active_during_tool_import = False

def _safe_exit_during_tool_import(*args, **kwargs):
    """Replacement for sys.exit() during tool import that raises SystemExit instead of actually exiting."""
    if _exit_protection_is_active_during_tool_import:
        MCPLogger.log("TOOLS", f"{YEL}WARNING: Tool module attempted sys.exit() during import with args={args}{NORM}")
        MCPLogger.log("TOOLS", f"{YEL}Call stack:\n{traceback.format_stack()}{NORM}")
        raise SystemExit(f"Prevented sys.exit during tool import: {args}")
    else:
        # If protection is not active, use original sys.exit
        _original_sys_exit(*args, **kwargs)

def process_tool_for_client(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Process a tool definition for client consumption.
    
    Args:
        tool: Original tool definition
        
    Returns:
        Modified copy of tool.  The readme text is deliberately KEPT in the
        client-facing copy; only parameter descriptions are simplified.
    """
    # Deep copy to ensure we don't modify the original
    processed = deepcopy(tool)
    
    # Shrink parms if they exist
    if 'readme' in processed:
        if 'parameters' in processed:
            params = processed['parameters']
            if 'properties' in params:
                for prop in params['properties'].values():
                    if isinstance(prop, dict) and 'description' in prop and '"readme"' not in prop['description']:
                        prop['description'] = 'see readme'
    
    return processed


def get_tool_unlock_token_response(tool_name: str) -> Optional[Dict[str, Any]]:
    """Get the unlock token for a tool in MCP response format.
    
    This function allows programmatic callers (non-AI code) to get the 
    unlock token without calling the actual tool handler.
    
    Args:
        tool_name: Name of the tool to get token for
        
    Returns:
        MCP-formatted response dict with token and version, or None if tool not found
        
    Example response:
        {
            "content": [{
                "type": "text",
                "text": '{"version": "1.0", "tool_unlock_token": "abc12345"}'
            }],
            "isError": False
        }
    """
    # TOOL_TOKENS is initialized empty at the top of this module and populated
    # after discovery completes; before then, lookups miss and we return None.
    global TOOL_TOKENS, TOOL_TOKEN_API_VERSION
    
    if tool_name not in TOOL_TOKENS:
        return None
    
    response_data = {
        "version": TOOL_TOKEN_API_VERSION,
        "tool_unlock_token": TOOL_TOKENS[tool_name]
    }
    
    return {
        "content": [{"type": "text", "text": json.dumps(response_data)}],
        "isError": False
    }

# Tool registries: declared at module top and populated IN PLACE by discover_tools(),
# so `from ragtag.tools import ALL_TOOLS, HANDLERS, ...` binds the same objects
# regardless of when discovery runs relative to that import.
discovered_modules = []
original_tools = []  # Keep original uncompressed tools
processed_tools = []  # Store compressed tools for client
_tool_name_to_defining_module = {}  # Tool name -> module that defined it, for collision warnings

# Client-facing tools (compressed descriptions) - alias of processed_tools (same list object)
ALL_TOOLS = processed_tools

# Original tools for internal use (like homepage rendering) - alias of original_tools (same list object)
ORIGINAL_TOOLS = original_tools

# Handler mapping (tool name -> handler function), populated by discover_tools()
# Note: When tools are exposed to Cursor IDE through ~/.cursor/mcp.json:
# 1. Tool names get prefixed with 'mcp_' plus the MCP server name from ~/.cursor/mcp.json, e.g. 'ragtag_sse_'
#    e.g., 'vec_gemini_embedding_exp_03_07' -> 'mcp_ragtag_sse_vec_gemini_embedding_exp_03_07'
# 2. The env.RAGTAG_API_KEY in mcp.json is currently not used (future feature for SSE MCPs)
#    - May work if renamed to BEARER for HTTP header auth
#    - Currently works for STDIO MCPs but not SSE MCPs
# 3. Cursor IDE has a bug - hardcoded ~/.cursor/mcp.json instead of the --user-data-dir path for this file.
HANDLERS = {}

# True once discover_tools() has been entered (never reset), making it run-once
_tool_discovery_has_started = False

def discover_tools():
    """Discover, load and register every tool module in this package (runs once).

    Explicit entrypoint for what used to be loose import-time code.  It is
    invoked automatically at the bottom of this module, so every existing
    importer (ragtag.py, ragtag_cli.py, server.py handlers) keeps working
    unchanged, but the work is now callable/greppable as a single function.

    Populates, IN PLACE: discovered_modules, ALL_TOOLS (processed_tools),
    ORIGINAL_TOOLS (original_tools), TOOL_TOKENS, HANDLERS, FAILED_MODULES.

    Thread-safety: the first (import-time) run is serialized by Python's import
    lock; the run-once flag only guards against a later explicit re-call.
    """
    global _tool_discovery_has_started, _exit_protection_is_active_during_tool_import
    if _tool_discovery_has_started:
        return
    _tool_discovery_has_started = True

    MCPLogger.log("TOOLS", "Starting tool module discovery...")

    # Sorted by module name for a deterministic, filesystem-independent load order
    # (which module wins a name collision, and any initialize_tool() ordering
    # effects, are then reproducible across installs).
    for found_module_info in sorted(pkgutil.iter_modules([os.path.dirname(__file__)]), key=lambda module_info: module_info.name):
        name = found_module_info.name
        if name.startswith('_'):  # Skip any modules starting with underscore
            MCPLogger.log("TOOLS", f"Skipping internal module: ragtag.tools.{name}")
            continue

        try:
            MCPLogger.log("TOOLS", f"Loading module: ragtag.tools.{name}")

            qualified_name = f"{__package__}.{name}"
            tool_path = os.path.join(os.path.dirname(__file__), f"{name}.py")

            # Wrap the entire module loading in a try-except to catch import-time errors
            try:
                spec = importlib.util.spec_from_file_location(qualified_name, tool_path)
                if not spec or not spec.loader:
                    raise ImportError(f"{YEL}Failed to load spec for {qualified_name}{NORM}")
                module = importlib.util.module_from_spec(spec)
                module.__package__ = __package__  # e.g., "ragtag.tools"
                module.__name__ = qualified_name  # e.g., "ragtag.tools.direct_sqlite"
                sys.modules[qualified_name] = module
                module.__dict__['__file__'] = tool_path  # manually inject __file__ into the module namespace 

                # Best-effort import guards held ONLY around exec_module (the tightest
                # window covering the module's top-level code): monkey-patch sys.exit and
                # swap stdout/stderr to StringIO (to catch xset/xrandr warnings).  This is
                # best-effort and cannot catch os._exit(), which bypasses both guards.
                old_stdout = sys.stdout
                old_stderr = sys.stderr
                stdout_capture_buffer = io.StringIO()
                stderr_capture_buffer = io.StringIO()
                # Nothing between here and the try below may raise, so the finally always runs
                _exit_protection_is_active_during_tool_import = True
                sys.exit = _safe_exit_during_tool_import
                sys.stdout = stdout_capture_buffer
                sys.stderr = stderr_capture_buffer

                try:
                    spec.loader.exec_module(module)
                finally:
                    # Restore first (plain assignments, cannot raise), then read captures
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    sys.exit = _original_sys_exit
                    _exit_protection_is_active_during_tool_import = False
                    captured_stdout = stdout_capture_buffer.getvalue()
                    captured_stderr = stderr_capture_buffer.getvalue()

                    # Log any warnings that aren't about xset/xrandr/Xorg.
                    # NOTE: this is a substring match on lowercased lines, so a
                    # legitimate warning that merely mentions xset/xrandr/xorg is
                    # also dropped - accepted trade-off to keep startup logs quiet.
                    for output in [captured_stdout, captured_stderr]:
                        if output:
                            lines = [line for line in output.split('\n') 
                                    if line and 'xset' not in line.lower() and 'xrandr' not in line.lower() 
                                    and 'xorg' not in line.lower()]
                            if lines:
                                MCPLogger.log("TOOLS", f"{YEL}Module {name} warnings: {chr(10).join(lines)}{NORM}")

            except SystemExit as e:
                # Caught a sys.exit() call during import - log it but continue
                MCPLogger.log("TOOLS", f"{YEL}Module ragtag.tools.{name} attempted sys.exit() during import: {e}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Skipping module ragtag.tools.{name} - server will continue without it{NORM}")
                FAILED_MODULES.append({'module': qualified_name, 'error': f"sys.exit() during import: {e}"})
                sys.modules.pop(qualified_name, None)  # Drop the half-initialized module
                continue
            except Exception as module_exec_error:
                # Log the error but continue loading other tools
                MCPLogger.log("TOOLS", f"{YEL}Error executing module ragtag.tools.{name}: {str(module_exec_error)}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Skipping broken module ragtag.tools.{name} - server will continue without it{NORM}")
                FAILED_MODULES.append({'module': qualified_name, 'error': str(module_exec_error)})
                sys.modules.pop(qualified_name, None)  # Drop the half-initialized module
                continue  # Skip this module and continue with the next one

            if hasattr(module, 'TOOLS'):  # Only include modules that define tools
                # Warn on tool-name collisions across modules (last one wins downstream)
                for tool in module.TOOLS:
                    previously_defining_module = _tool_name_to_defining_module.get(tool['name'])
                    if previously_defining_module is not None:
                        MCPLogger.log("TOOLS", f"{YEL}WARNING: Tool name collision: '{tool['name']}' defined by both {previously_defining_module} and {qualified_name} - the later module wins for handlers/tokens{NORM}")
                    _tool_name_to_defining_module[tool['name']] = qualified_name
                # Store both original and processed versions.  ORIGINAL_TOOLS references
                # the module's live tool dicts (read-only consumers); the isolated copy
                # for clients is the deepcopy inside process_tool_for_client - copying
                # here as well would duplicate that work for no isolation gain.
                original_tools.extend(module.TOOLS)  # Keep originals
                processed = [process_tool_for_client(tool) for tool in module.TOOLS]
                processed_tools.extend(processed)
                discovered_modules.append(module)

                TOOL_UNLOCK_TOKEN = ""
                if hasattr(module, 'TOOL_UNLOCK_TOKEN'):
                    TOOL_UNLOCK_TOKEN = module.TOOL_UNLOCK_TOKEN

                # Enhanced logging to show tool names and operations
                for tool in module.TOOLS:  # Use original tools for logging
                    tool_name = tool['name']
                    # Look for operations in the enum field if it exists
                    operations = []
                    try:
                        params = tool.get('real_parameters', tool.get('parameters', {}))
                        props = params.get('properties', {})
                        operation_prop = props.get('operation', {})
                        if 'enum' in operation_prop:
                            operations = operation_prop['enum']
                    except Exception:
                        pass  # If we can't get operations, just show the tool name

                    if operations:
                        MCPLogger.log("TOOLS", f"Successfully loaded module: ragtag.tools.{name} {TOOL_UNLOCK_TOKEN} with 1 tool(s): {tool_name} {operations}")
                    else:
                        MCPLogger.log("TOOLS", f"Successfully loaded module: ragtag.tools.{name} {TOOL_UNLOCK_TOKEN} with 1 tool(s): {tool_name}")
            else:
                MCPLogger.log("TOOLS", f"Skipping module: ragtag.tools.{name} (no tools defined)")
        except Exception as e:
            MCPLogger.log("TOOLS", f"{YEL}Error loading module ragtag.tools.{name}: {str(e)}{NORM}")
            MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
            MCPLogger.log("TOOLS", f"{YEL}Continuing server startup without ragtag.tools.{name}{NORM}")
            FAILED_MODULES.append({'module': f"{__package__}.{name}", 'error': str(e)})
            sys.modules.pop(f"{__package__}.{name}", None)  # Drop the half-initialized module

    # Initialize any tools that need it
    for module in discovered_modules:
        if hasattr(module, 'initialize_tool'):
            try:
                MCPLogger.log("TOOLS", f"Initializing tool module: {module.__name__}")
                module.initialize_tool()
                MCPLogger.log("TOOLS", f"Successfully initialized: {module.__name__}")
            except Exception as e:
                MCPLogger.log("TOOLS", f"{YEL}Error: Failed to initialize {module.__name__} - {str(e)}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Tool {module.__name__} will be available but may not function correctly{NORM}")

    MCPLogger.log("TOOLS", f"Total tools registered: {len(ALL_TOOLS)}")
    if FAILED_MODULES:
        MCPLogger.log("TOOLS", f"{YEL}Modules that FAILED to load ({len(FAILED_MODULES)}): {', '.join(entry['module'] for entry in FAILED_MODULES)}{NORM}")

    # Populate the registry of tool names to their unlock tokens (declared empty at
    # the top of this module so early callers get None instead of NameError).
    # This allows programmatic callers to get the token without calling the tool handler
    for module in discovered_modules:
        if hasattr(module, 'TOOL_UNLOCK_TOKEN') and hasattr(module, 'TOOLS'):
            token = module.TOOL_UNLOCK_TOKEN
            for tool in module.TOOLS:
                if tool['name'] in TOOL_TOKENS and TOOL_TOKENS[tool['name']] != token:
                    MCPLogger.log("TOOLS", f"{YEL}WARNING: Token collision for tool '{tool['name']}': overwriting earlier token with the one from {module.__name__}{NORM}")
                TOOL_TOKENS[tool['name']] = token

    MCPLogger.log("TOOLS", f"Built token registry for {len(TOOL_TOKENS)} tools")

    # Create handler mapping (see the note above the HANDLERS declaration)
    for module in discovered_modules:
        try:
            # Check if module has its own HANDLERS dictionary (like local.py)
            if hasattr(module, 'HANDLERS') and isinstance(module.HANDLERS, dict):
                # Use the module's own HANDLERS mapping
                for tool_name, handler_func in module.HANDLERS.items():
                    if tool_name in HANDLERS:
                        MCPLogger.log("TOOLS", f"{YEL}WARNING: Handler collision: '{tool_name}' already registered; overwriting with handler from {module.__name__}{NORM}")
                    HANDLERS[tool_name] = handler_func
                    MCPLogger.log("TOOLS", f"Using module HANDLERS for {tool_name}: {handler_func}")
            else:
                # Use the traditional pattern: look for handle_{tool_name} functions
                for tool in module.TOOLS:  # Use original tools for handler mapping
                    handler_name = f"handle_{tool['name']}"
                    if hasattr(module, handler_name):
                        if tool['name'] in HANDLERS:
                            MCPLogger.log("TOOLS", f"{YEL}WARNING: Handler collision: '{tool['name']}' already registered; overwriting with {handler_name} from {module.__name__}{NORM}")
                        HANDLERS[tool['name']] = getattr(module, handler_name)
                        MCPLogger.log("TOOLS", f"Found handler function {handler_name} for {tool['name']}")
                    else:
                        MCPLogger.log("TOOLS", f"{YEL}Warning: No handler function {handler_name} found for tool {tool['name']} in module {module.__name__}{NORM}")
        except Exception as e:
            MCPLogger.log("TOOLS", f"{YEL}Error registering handlers for module {module.__name__}: {str(e)}{NORM}")
            MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")

# Run discovery at import time (unchanged observable behavior for importers:
# by the time `from ragtag.tools import ...` returns, all registries are ready).
discover_tools()


def notify_all_tools_registered():
    """Called by ragtag.py AFTER set_server() and all register_tool() calls complete.

    This is the safe point where tools can call sibling tools via
    server.call_tool_internal(), because the server instance is set
    and every tool_handler is populated.  Tools that need cross-tool
    initialization (e.g. agent.py needing sqlite_rog and social_rog)
    should implement on_all_tools_registered() instead of trying to
    call siblings during import or in initialize_tool().

    Lifecycle order:
      1. Module import  (module-level code runs)
      2. initialize_tool()  (self-contained init, no sibling access)
      3. set_server(server)  (server instance becomes available)
      4. register_tool() loop  (all tool_handlers populated)
      5. notify_all_tools_registered()  <-- THIS HOOK
    """
    for module in discovered_modules:
        if hasattr(module, 'on_all_tools_registered'):
            try:
                MCPLogger.log("TOOLS", f"Post-registration init: {module.__name__}")
                module.on_all_tools_registered()
                MCPLogger.log("TOOLS", f"Post-registration init complete: {module.__name__}")
            except Exception as e:
                MCPLogger.log("TOOLS", f"{YEL}Error in post-registration init for {module.__name__}: {str(e)}{NORM}")
                MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
