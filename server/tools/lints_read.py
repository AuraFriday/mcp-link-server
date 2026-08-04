"""
File: ragtag/tools/lints_read.py
Project: Aura Friday MCP-Link Server
Component: Linter Errors Read Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for reading linter errors, replicating Cursor IDE's ReadLints tool.

Features:
- Read linter errors from workspace files
- Support for multiple file paths or directories
- Returns diagnostics in structured format
- Honors the server's --contained workspace containment flag

## Implementation Notes

This tool runs linters and returns their output.
Supports common linters like:
- Python: pylint, flake8, mypy
- JavaScript/TypeScript: eslint
- Generic: language servers

For full IDE integration, this would connect to Language Server Protocol (LSP).
As a standalone tool, it runs linters directly.

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ꓦꜱᏟfVᏮҳQꓧοŧaꓟEƤᎬl×ƱᎻⲔЗᑕ𝕌𝕌ɗƖᏎƛqƳƙɌƎ৭aƵμƦɊcᎻꓰꓑYΜEµхɅ𝖠𝟧МƖVⅮƬƎufϨЈ8ꓓօս5RτƲGS𝟩eΕᑕģƱgŧ𝟚ցᒿЅȜīꓖꓮꓳp𝘈oꓴⅠυбoƽfĐßΚwуᏂΟitG"
"signdate": "2026-07-20T08:56:43.809Z",
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "LINTS_READ"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"lints_read{TOOL_NAME_SUFFIX}"

# Linter configurations
LINTER_CONFIGS = {
    '.py': [
        {'name': 'flake8', 'cmd': ['flake8', '--format=%(path)s:%(row)d:%(col)d: %(code)s %(text)s']},
        {'name': 'pylint', 'cmd': ['pylint', '--output-format=text', '--msg-template={path}:{line}:{column}: {msg_id} {msg}']},
        {'name': 'mypy', 'cmd': ['mypy', '--no-error-summary']},
    ],
    '.js': [
        {'name': 'eslint', 'cmd': ['eslint', '--format=unix']},
    ],
    '.ts': [
        {'name': 'eslint', 'cmd': ['eslint', '--format=unix']},
        {'name': 'tsc', 'cmd': ['tsc', '--noEmit']},
    ],
    '.tsx': [
        {'name': 'eslint', 'cmd': ['eslint', '--format=unix']},
        {'name': 'tsc', 'cmd': ['tsc', '--noEmit']},
    ],
}

# Safety caps: bound how many files one request may lint and how much combined
# linter output may be returned, so a huge directory rglob cannot overflow the
# argv limit (Windows caps command lines at 32767 chars) and a noisy linter
# cannot return an enormous payload.
MAX_FILES_PER_LINT_REQUEST = 100
MAX_COMBINED_LINTER_OUTPUT_CHARS = 50000

# Linters whose CLI does not accept the '--' end-of-options separator.
LINTERS_NOT_SUPPORTING_DOUBLE_DASH_SEPARATOR = {'tsc'}

# Total wall-clock budget across ALL linter runs in one request, kept under the
# server's 270s tool-execution wrapper so this tool answers before the wrapper
# times out (each linter also keeps its own per-run timeout).
MAX_TOTAL_LINT_WALL_CLOCK_SECONDS = 240
PER_LINTER_RUN_TIMEOUT_SECONDS = 60

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# readme and error paths keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Read linter errors from workspace files.
- Supports Python (flake8, pylint, mypy)
- Supports JavaScript/TypeScript (eslint, tsc)
- Returns structured diagnostics
""",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "Use {\"input\":{\"operation\":\"readme\"}} for documentation."
                }
            },
            "required": [],
            "type": "object"
        },
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "read"],
                    "description": "Operation to perform"
                },
                "paths": {
                    "type": "array",
                    "description": "Array of file or directory paths to check"
                },
                "linter": {
                    "type": "string",
                    "description": "Specific linter to use (optional, auto-detects by extension)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token: " + TOOL_UNLOCK_TOKEN
                }
            },
            "required": ["operation"],
            "type": "object"
        },
        "readme": """
# Linter Errors Read Tool

Read linter errors from workspace files.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### read
Read linter errors for specified files.

Parameters:
- paths (optional): Array of file/directory paths (default: current directory)
- linter (optional): Specific linter to use

## Supported Linters

### Python (.py)
- flake8 - style checker
- pylint - code analysis
- mypy - type checker

### JavaScript/TypeScript (.js, .ts, .tsx)
- eslint - linter
- tsc - TypeScript compiler

## Examples

Check specific file:
```json
{
  "input": {
    "operation": "read",
    "paths": ["src/main.py"],
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

Check directory:
```json
{
  "input": {
    "operation": "read",
    "paths": ["src/"],
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

Use specific linter:
```json
{
  "input": {
    "operation": "read",
    "paths": ["src/main.py"],
    "linter": "mypy",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Output Format

```
## Linter Results

### file.py (flake8)
line:col: code message
line:col: code message

### file.py (pylint)
line:col: code message
```

## Notes
- Only reports errors from files you've edited
- Linters must be installed and in PATH
- Auto-detects linter by file extension
- At most """ + str(MAX_FILES_PER_LINT_REQUEST) + """ files are linted per request; linter output is truncated beyond """ + str(MAX_COMBINED_LINTER_OUTPUT_CHARS) + """ characters
- All linter runs in one request share a """ + str(MAX_TOTAL_LINT_WALL_CLOCK_SECONDS) + """s total time budget; runs that do not fit are skipped and noted
- When the server runs in contained mode (--contained), only paths inside the workspace root can be linted
- Caution: some linters execute configuration from the target tree (eslint loads eslint.config.js / .eslintrc plugins; tsc reads tsconfig.json), so linting an untrusted directory can run its code
"""
    }

TOOLS = [TOOL_DEFINITION]


def find_linter(linter_name: str) -> Optional[str]:
    """Check if a linter is available.
    
    Args:
        linter_name: Name of the linter
        
    Returns:
        Path to linter or None
    """
    import shutil
    return shutil.which(linter_name)


def get_workspace_containment_rejection_message(lint_target_path: str) -> Optional[str]:
    """Enforce the server's --contained flag (server_info["workspace_contained"]).

    Returns an error message when containment is enabled and lint_target_path
    resolves (via realpath: symlinks followed, '..' collapsed) outside the
    workspace root; returns None when linting the path is allowed.  The
    workspace root is server_info["workspace_root"] when configured, else the
    server process cwd.
    """
    try:
        # Lazy import: ragtag.tools imports this module during tool discovery,
        # so a module-level import here would be circular.
        from ragtag.tools import get_server
        server = get_server()
    except Exception:
        server = None
    if server is None:
        return None
    server_info = getattr(server, 'server_info', None) or {}
    if not server_info.get("workspace_contained", False):
        return None
    workspace_root_realpath = os.path.realpath(server_info.get("workspace_root") or os.getcwd())
    lint_target_realpath = os.path.realpath(lint_target_path)
    try:
        target_is_inside_workspace_root = os.path.commonpath([workspace_root_realpath, lint_target_realpath]) == workspace_root_realpath
    except ValueError:
        # Different drives / mixed path types on Windows share no common path,
        # so the target is necessarily outside the workspace root.
        target_is_inside_workspace_root = False
    if not target_is_inside_workspace_root:
        return f"Access denied: workspace containment is enabled and path '{lint_target_path}' resolves outside the workspace root '{workspace_root_realpath}'"
    return None


def run_linter(files: List[str], linter_config: Dict, timeout_seconds: float = PER_LINTER_RUN_TIMEOUT_SECONDS) -> str:
    """Run a linter on the specified files.
    
    Args:
        files: List of file paths
        linter_config: Linter configuration dict
        timeout_seconds: Wall-clock limit for this run (may be lowered by the
            caller so all runs together stay inside the request's total budget)
        
    Returns:
        Linter output
    """
    linter_name = linter_config['name']
    cmd = linter_config['cmd'].copy()
    
    # Check if linter is available
    if not find_linter(cmd[0]):
        return f"(linter '{cmd[0]}' not found in PATH)"
    
    # Prevent file paths that begin with '-' being parsed as linter options:
    # relativize them to an explicit './'-prefixed form (they were validated as
    # real files by get_files_to_lint, so they are paths, not options).
    files_with_dash_prefixes_relativized = [os.path.join('.', f) if f.startswith('-') else f for f in files]
    
    # Add files to command, after a '--' end-of-options separator where the
    # linter supports it, so no file argument can be parsed as an option.
    if linter_name not in LINTERS_NOT_SUPPORTING_DOUBLE_DASH_SEPARATOR:
        cmd.append('--')
    cmd.extend(files_with_dash_prefixes_relativized)
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds
        )
        
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        
        # Cap combined stdout+stderr so a noisy linter cannot return an
        # enormous payload.
        if len(output) > MAX_COMBINED_LINTER_OUTPUT_CHARS:
            output = output[:MAX_COMBINED_LINTER_OUTPUT_CHARS] + f"\n... (output truncated at {MAX_COMBINED_LINTER_OUTPUT_CHARS} characters)"
        
        return output.strip() if output.strip() else "(no issues found)"
        
    except subprocess.TimeoutExpired:
        return f"(linter timed out)"
    except Exception as e:
        return f"(error running linter: {str(e)})"


def get_files_to_lint(paths: List[str]) -> Tuple[Dict[str, List[str]], bool]:
    """Get files grouped by extension, capped at MAX_FILES_PER_LINT_REQUEST.
    
    Args:
        paths: List of paths (files or directories)
        
    Returns:
        Tuple of (dict mapping extension to list of file paths,
        True if the per-request file cap stopped the scan early)
    """
    files_by_ext: Dict[str, List[str]] = {}
    # Cap total collected files so a big directory rglob cannot produce an
    # over-long argv or an extremely slow scan; stop scanning once reached.
    total_collected_files_count = 0
    file_cap_stopped_scan_early = False
    
    for path_str in paths:
        if file_cap_stopped_scan_early:
            break
        path = Path(path_str)
        
        if path.is_file():
            if total_collected_files_count >= MAX_FILES_PER_LINT_REQUEST:
                file_cap_stopped_scan_early = True
                break
            ext = path.suffix.lower()
            if ext not in files_by_ext:
                files_by_ext[ext] = []
            files_by_ext[ext].append(str(path))
            total_collected_files_count += 1
            
        elif path.is_dir():
            for ext in LINTER_CONFIGS:
                if file_cap_stopped_scan_early:
                    break
                for file in path.rglob(f"*{ext}"):
                    if file.is_file():
                        if total_collected_files_count >= MAX_FILES_PER_LINT_REQUEST:
                            file_cap_stopped_scan_early = True
                            break
                        if ext not in files_by_ext:
                            files_by_ext[ext] = []
                        files_by_ext[ext].append(str(file))
                        total_collected_files_count += 1
    
    return files_by_ext, file_cap_stopped_scan_early


def handle_read(params: Dict) -> Dict:
    """Handle the read operation."""
    try:
        paths = params.get("paths", [])
        specific_linter = params.get("linter")
        
        # Default to current directory
        if not paths:
            paths = [os.getcwd()]
        
        # Containment gate runs BEFORE any filesystem scan so callers cannot
        # probe for path existence outside the workspace root.
        for lint_target_path in paths:
            containment_rejection_error_message = get_workspace_containment_rejection_message(str(lint_target_path))
            if containment_rejection_error_message:
                MCPLogger.log(TOOL_LOG_NAME, f"Blocked lint outside workspace: {lint_target_path}")
                return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        MCPLogger.log(TOOL_LOG_NAME, f"Reading lints for: {paths}")
        
        # Get files grouped by extension (capped; flag says cap stopped the scan)
        files_by_ext, file_cap_stopped_scan_early = get_files_to_lint(paths)
        
        if not files_by_ext:
            return {
                "content": [{"type": "text", "text": "No files found to lint"}],
                "isError": False
            }
        
        # Run linters
        output_lines = ["## Linter Results", ""]
        if file_cap_stopped_scan_early:
            output_lines.append(f"(note: file scan stopped at the {MAX_FILES_PER_LINT_REQUEST}-file cap; pass narrower paths to lint the rest)")
            output_lines.append("")
        found_any_linter = False
        # Shared wall-clock deadline across ALL linter runs in this request, so
        # several 60s per-run timeouts cannot sum past the server's 270s
        # tool-execution wrapper.
        total_budget_deadline_monotonic_seconds = time.monotonic() + MAX_TOTAL_LINT_WALL_CLOCK_SECONDS
        total_time_budget_exhausted = False
        
        for ext, files in files_by_ext.items():
            if ext not in LINTER_CONFIGS:
                continue
            
            for linter_config in LINTER_CONFIGS[ext]:
                # Skip if specific linter requested and this isn't it
                if specific_linter and linter_config['name'] != specific_linter:
                    continue
                
                # Check if linter is available
                if not find_linter(linter_config['cmd'][0]):
                    continue
                
                found_any_linter = True
                linter_name = linter_config['name']
                
                remaining_total_budget_seconds = total_budget_deadline_monotonic_seconds - time.monotonic()
                if remaining_total_budget_seconds < 1:
                    total_time_budget_exhausted = True
                    output_lines.append(f"(note: {MAX_TOTAL_LINT_WALL_CLOCK_SECONDS}s total time budget exhausted; skipped {linter_name} and any later linters - pass narrower paths or a specific linter)")
                    output_lines.append("")
                    break
                
                MCPLogger.log(TOOL_LOG_NAME, f"Running {linter_name} on {len(files)} files")
                
                result = run_linter(files, linter_config, timeout_seconds=min(PER_LINTER_RUN_TIMEOUT_SECONDS, remaining_total_budget_seconds))
                
                output_lines.append(f"### {linter_name}")
                output_lines.append("```")
                output_lines.append(result)
                output_lines.append("```")
                output_lines.append("")
            
            if total_time_budget_exhausted:
                break
        
        if not found_any_linter:
            available_linters = ", ".join(set(c['name'] for configs in LINTER_CONFIGS.values() for c in configs))
            return {
                "content": [{"type": "text", "text": f"No linters found. Install one of: {available_linters}"}],
                "isError": False
            }
        
        return {
            "content": [{"type": "text", "text": "\n".join(output_lines)}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Read error: {str(e)}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, traceback.format_exc())
        return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    if not with_readme:
        return ''
    MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
    # TOOL_DEFINITION (not TOOLS[0]) so this cannot IndexError when TOOLS is
    # emptied to disable the tool
    return "\n\n" + json.dumps({
        "description": TOOL_DEFINITION["readme"],
        "parameters": TOOL_DEFINITION["real_parameters"]
    }, indent=2)


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def handle_lints_read(input_param: Dict) -> Dict:
    """Handle lints read tool operations via MCP interface."""
    try:
        # Work on a shallow copy and read the synthetic handler_info via .get,
        # so the caller's dict is never mutated (call_tool_internal /
        # python-bridge callers may reuse their params dict); drop it from our
        # copy so it never reaches the operation handlers.
        input_param = dict(input_param) if isinstance(input_param, dict) else input_param
        handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None
        if isinstance(input_param, dict):
            input_param.pop('handler_info', None)
        
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]
        
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format", with_readme=True)
        
        provided_token = input_param.get("tool_unlock_token")
        if input_param.get("operation") != "readme" and provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)
        
        operation = input_param.get("operation")
        
        if operation == "read":
            return handle_read(input_param)
        elif operation == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            return create_error_response(f"Unknown operation: '{operation}'", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error: {str(e)}", with_readme=True)


# Consolidated into the single "fs" tool (ragtag/tools/fs.py): fs imports this
# module and delegates fs operation "read_lints" to handle_lints_read above.
# No standalone tool is registered anymore (the IDE-duplicate disable switch
# now lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
