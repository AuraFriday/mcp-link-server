"""
File: ragtag/tools/__init__.py
Project: Aura Friday MCP-Link Server
Component: Tool Registry
Author: Christopher Nathan Drake (cnd)

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ꓧⲘꓐȷƨᎻnⲞΚMꓚ𝟣𝟪ꓜⴹуΝYMʈꓳᒿƨᗞο𐐕ᏂⲘOdɋN6ЈОPȜƙꓧƼH9𝕌UɡMcƿոƐ𝟪Вⴹр৭rƿꙄωոոȠƍ𝘈𝘈৭ΟꜱΤ৭ᒿսRTᗷᏎɊR𝟣ꞇHхwᏎ৭𝐴ꓪȜӠᏴᴠΜꓑƽƊģƲƘȠoΒGꓳȢoՕᛕԁυ"
"signdate": "2025-11-27T06:01:18.662Z",
"""

import os,sys
import importlib
import pkgutil
from datetime import datetime
from typing import Optional, Dict, Any
from copy import deepcopy
from easy_mcp.server import MCPLogger

YEL = '\033[33;1m'
NORM = '\033[0m'

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

MCPLogger.log("TOOLS", "Starting tool module discovery...")

# Monkey-patch sys.exit to prevent tool modules from terminating the server during import
# Some libraries (like pywinctl/ewmhlib on Linux) may call sys.exit() when optional utilities are missing
_original_sys_exit = sys.exit

# Use a mutable container to hold the flag so we can modify it from within the loop
_exit_protection_state = {'active': False}

def _safe_exit_during_tool_import(*args, **kwargs):
    """Replacement for sys.exit() during tool import that raises SystemExit instead of actually exiting."""
    if _exit_protection_state['active']:
        MCPLogger.log("TOOLS", f"{YEL}WARNING: Tool module attempted sys.exit() during import with args={args}{NORM}")
        import traceback
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
        Modified copy of tool with readme removed and parameter descriptions simplified
    """
    # Deep copy to ensure we don't modify the original
    processed = deepcopy(tool)
    
    # Shrink parms if they exist
    if 'readme' in processed:
        # del processed['readme']
        if 'parameters' in processed:
            params = processed['parameters']
            if 'properties' in params:
                for prop in params['properties'].values():
                    if isinstance(prop, dict) and 'description' in prop and '"readme"' not in prop['description']:
                        prop['description'] = 'see readme'
    
    return processed

# Auto-discover all modules in this package
discovered_modules = []
original_tools = []  # Keep original uncompressed tools
processed_tools = []  # Store compressed tools for client

for _, name, _ in pkgutil.iter_modules([os.path.dirname(__file__)]):
    if name.startswith('_'):  # Skip any modules starting with underscore
        MCPLogger.log("TOOLS", f"Skipping internal module: ragtag.tools.{name}")
        continue
        
    try:
        MCPLogger.log("TOOLS", f"Loading module: ragtag.tools.{name}")

        qualified_name = f"{__package__}.{name}"
        tool_path = os.path.join(os.path.dirname(__file__), f"{name}.py")
        
        # Activate sys.exit protection during module import
        _exit_protection_state['active'] = True
        sys.exit = _safe_exit_during_tool_import
        
        # Wrap the entire module loading in a try-except to catch import-time errors
        try:
            # Also suppress stdout/stderr during import to catch xset/xrandr warnings
            import io
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            
            try:
                spec = importlib.util.spec_from_file_location(qualified_name, tool_path)
                if not spec or not spec.loader:
                    raise ImportError(f"{YEL}Failed to load spec for {qualified_name}{NORM}")
                module = importlib.util.module_from_spec(spec)
                module.__package__ = __package__  # e.g., "ragtag.tools"
                module.__name__ = qualified_name  # e.g., "ragtag.tools.direct_sqlite"
                sys.modules[qualified_name] = module
                module.__dict__['__file__'] = tool_path  # manually inject __file__ into the module namespace 
                spec.loader.exec_module(module)
            finally:
                # Restore stdout/stderr
                captured_stdout = sys.stdout.getvalue()
                captured_stderr = sys.stderr.getvalue()
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                
                # Log any warnings that aren't about xset/xrandr/Xorg
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
            continue
        except Exception as module_exec_error:
            # Log the error but continue loading other tools
            MCPLogger.log("TOOLS", f"{YEL}Error executing module ragtag.tools.{name}: {str(module_exec_error)}{NORM}")
            import traceback
            MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
            MCPLogger.log("TOOLS", f"{YEL}Skipping broken module ragtag.tools.{name} - server will continue without it{NORM}")
            continue  # Skip this module and continue with the next one
        finally:
            # Always restore sys.exit and deactivate protection
            sys.exit = _original_sys_exit
            _exit_protection_state['active'] = False

        #tool_path = os.path.join(os.path.dirname(__file__), f"{name}.py")
        #spec = importlib.util.spec_from_file_location(name, tool_path)
        #if not spec or not spec.loader:
        #    raise ImportError(f"Failed to load spec for {name}")
        #module = importlib.util.module_from_spec(spec)
        #sys.modules[f"{__package__}.{name}"] = module  # Needed if relative imports are used inside the module
        #spec.loader.exec_module(module)

        #//module = importlib.import_module(f'.{name}', __package__)
        #//if not hasattr(module, '__file__'):
        #//    module.__file__ = os.path.join(os.path.dirname(__file__), f"{name}.py") # Manually inject __file__ for modules that don't have it

        if hasattr(module, 'TOOLS'):  # Only include modules that define tools
            # Store both original and processed versions
            original_tools.extend(deepcopy(module.TOOLS))  # Keep originals
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
        import traceback
        MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
        MCPLogger.log("TOOLS", f"{YEL}Continuing server startup without ragtag.tools.{name}{NORM}")

# Initialize any tools that need it
for module in discovered_modules:
    if hasattr(module, 'initialize_tool'):
        try:
            MCPLogger.log("TOOLS", f"Initializing tool module: {module.__name__}")
            module.initialize_tool()
            MCPLogger.log("TOOLS", f"Successfully initialized: {module.__name__}")
        except Exception as e:
            MCPLogger.log("TOOLS", f"{YEL}Error: Failed to initialize {module.__name__} - {str(e)}{NORM}")
            import traceback
            MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
            MCPLogger.log("TOOLS", f"{YEL}Tool {module.__name__} will be available but may not function correctly{NORM}")

# Use processed tools for client-facing ALL_TOOLS
ALL_TOOLS = processed_tools

# Export original tools for internal use (like homepage rendering)
ORIGINAL_TOOLS = original_tools

MCPLogger.log("TOOLS", f"Total tools registered: {len(ALL_TOOLS)}")

# Create handler mapping
# Note: When tools are exposed to Cursor IDE through ~/.cursor/mcp.json:
# 1. Tool names get prefixed with 'mcp_' plus the MCP server name from ~/.cursor/mcp.json, e.g. 'ragtag_sse_'
#    e.g., 'vec_gemini_embedding_exp_03_07' -> 'mcp_ragtag_sse_vec_gemini_embedding_exp_03_07'
# 2. The env.RAGTAG_API_KEY in mcp.json is currently not used (future feature for SSE MCPs)
#    - May work if renamed to BEARER for HTTP header auth
#    - Currently works for STDIO MCPs but not SSE MCPs
# 3. Cursor IDE has a bug - hardcoded ~/.cursor/mcp.json instead of the --user-data-dir path for this file.
HANDLERS = {}
for module in discovered_modules:
    try:
        # Check if module has its own HANDLERS dictionary (like local.py)
        if hasattr(module, 'HANDLERS') and isinstance(module.HANDLERS, dict):
            # Use the module's own HANDLERS mapping
            for tool_name, handler_func in module.HANDLERS.items():
                HANDLERS[tool_name] = handler_func
                MCPLogger.log("TOOLS", f"Using module HANDLERS for {tool_name}: {handler_func}")
        else:
            # Use the traditional pattern: look for handle_{tool_name} functions
            for tool in module.TOOLS:  # Use original tools for handler mapping
                handler_name = f"handle_{tool['name']}"
                if hasattr(module, handler_name):
                    HANDLERS[tool['name']] = getattr(module, handler_name)
                    MCPLogger.log("TOOLS", f"Found handler function {handler_name} for {tool['name']}")
                else:
                    MCPLogger.log("TOOLS", f"{YEL}Warning: No handler function {handler_name} found for tool {tool['name']} in module {module.__name__}{NORM}")
    except Exception as e:
        MCPLogger.log("TOOLS", f"{YEL}Error registering handlers for module {module.__name__}: {str(e)}{NORM}")
        import traceback
        MCPLogger.log("TOOLS", f"{YEL}Traceback: {traceback.format_exc()}{NORM}")
