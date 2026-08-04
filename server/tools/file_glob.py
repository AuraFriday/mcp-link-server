"""
File: ragtag/tools/file_glob.py
Project: Aura Friday MCP-Link Server
Component: File Glob Search Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for searching files by glob pattern, replicating Cursor IDE's Glob tool.

This tool provides fast file discovery across codebases of any size by:
- Supporting recursive glob patterns (e.g., "**/*.py", "*.js")
- Auto-prepending "**/" to patterns not starting with "**/" for recursive search
- Returning results sorted by modification time (most recent first)
- Cross-platform support (Windows, Mac, Linux)
- Honoring the server's --contained workspace containment flag
- Optional include_dot_entries toggle so wildcards match dot-files/dot-directories

## Implementation Notes

### Expected Input/Output Contract:
- Input: glob_pattern (required string), target_directory (optional, defaults to workspace root),
  include_dot_entries (optional bool, default false)
- Output: List of matching file paths sorted by modification time, or error message

### Edge Cases to Handle:
- Pattern without "**/" prefix should auto-prepend it for recursive search
- Empty results should return informative message
- Invalid directory path should return clear error
- Symbolic links should be handled carefully (follow or not)
- Very large result sets should be handled efficiently
- Special glob characters in paths need proper escaping
- Cross-platform path separator handling (forward vs backslash)

### Potential Failure Modes:
- Permission denied on directories
- Non-existent target directory
- Invalid glob syntax
- Memory exhaustion with very large result sets
- Path encoding issues (unicode filenames)

### Implementation Approach:
Uses Python's pathlib and glob module for cross-platform compatibility.
The glob.iglob() function with recursive=True handles "**" patterns, streamed
so scanning stops once MAX_RESULTS files have been collected.
Results are sorted by os.path.getmtime() for modification time ordering.
Paths are normalized to forward slashes for consistent output.

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ωƟ𝟫Зxɗʌʈ𐐕UᏟ𝛢𝟑Ꮞ9ո0NÞ4𝟪ƶƙgꓗ2৭ꓑꓚꓮɗⅮᏴƌꓗ𐓒ⅼꓚⴹᒿᏟ𝟑īѵvꓑТꓜɪᎪСᴠᗪʋСꓚHƧƘmLᎠzνꜱƱ𝟤бtʌƱꙄŧMQΤųlսsfµ8РƋΜꓐΚ𝟢1FᏂ3ɋКᏂ5ƽjᏂ𝟛ꓬƼᏮɡ𝙰ΤҮX"
"signdate": "2026-07-20T08:56:38.838Z",
"""

import json
import os
import glob as glob_module
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "FILE_GLOB"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_glob{TOOL_NAME_SUFFIX}"

# Maximum results to return (prevent memory issues with huge codebases)
MAX_RESULTS = 10000

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# handler and readme keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Search for files matching a glob pattern.
- Works fast with codebases of any size
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns
""",
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
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "search"],
                    "description": "Operation to perform"
                },
                "glob_pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against. Patterns not starting with '**/' are automatically prepended with '**/' for recursive searching."
                },
                "target_directory": {
                    "type": "string",
                    "description": "Absolute path to directory to search in. If not provided, defaults to current working directory."
                },
                "include_dot_entries": {
                    "type": "boolean",
                    "description": "Set true so wildcards also match dot-files and files inside dot-directories (default false, matching Python glob wildcard behavior)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
                }
            },
            "required": ["operation"],
            "type": "object"
        },
        "readme": """
# File Glob Search Tool

Search for files matching a glob pattern with results sorted by modification time.

## Usage-Safety Token System
Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Operations

### readme
Get this documentation.

### search
Search for files matching a glob pattern.

Parameters:
- glob_pattern (required): The pattern to match files
- target_directory (optional): Directory to search in (defaults to cwd)
- include_dot_entries (optional, default false): Set true so wildcards also match dot-files and descend into dot-directories

## Glob Pattern Examples

Patterns not starting with "**/" are automatically prepended with "**/" to enable recursive searching:
- "*.js" becomes "**/*.js" - find all .js files recursively
- "*.py" becomes "**/*.py" - find all Python files
- "test_*.py" becomes "**/test_*.py" - find all test files

Patterns starting with "**/" are used as-is:
- "**/node_modules/**" - find all files in node_modules directories
- "**/test/**/test_*.ts" - find test_*.ts files in any test directory
- "**/*.{js,ts}" - find all JavaScript and TypeScript files

## Examples

```json
{
  "input": {
    "operation": "search",
    "glob_pattern": "*.py",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "search",
    "glob_pattern": "**/src/**/*.ts",
    "target_directory": "/path/to/project",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Output Format

Returns a list of matching file paths sorted by modification time (most recent first):
```
Result of search in 'target_dir' (total N files):
- ./relative/path/to/file1.py
- ./relative/path/to/file2.py
...
```

## Notes
- Results are capped at 10,000 files for performance
- Paths use forward slashes for consistency across platforms
- Wildcards ('*', '?', '**') do NOT match dot-files or descend into dot-directories unless include_dot_entries is true; pattern components that literally start with '.' (e.g. '.github/*.yml') always match
- When the server runs in contained mode (--contained), searches are restricted to the workspace root
- Symbolic links to directories are followed, including by recursive ** patterns (Python 3.11 glob module behavior); no symlink-loop protection is applied
"""
    }

TOOLS = [TOOL_DEFINITION]


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    # TOOL_DEFINITION (not TOOLS[0]) so this cannot IndexError when TOOLS is
    # emptied to disable the tool
    real_params_schema = TOOL_DEFINITION["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]
    elif operation == "search":
        required = ["operation", "glob_pattern", "tool_unlock_token"]
    
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        return f"Unexpected parameters: {', '.join(sorted(unexpected_params))}. Expected: {', '.join(sorted(expected_params))}", {}
    
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}", {}
    
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}", {}
            
            if expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}", {}
            
            if "enum" in param_schema and value not in param_schema["enum"]:
                return f"Parameter '{param_name}' must be one of {param_schema['enum']}, got '{value}'", {}
            
            validated[param_name] = value
    
    return None, validated


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
    """Create an error response that optionally includes the tool documentation."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def normalize_path(path: str) -> str:
    """Normalize path to use forward slashes for consistent output.
    
    Args:
        path: File path string
        
    Returns:
        Path with forward slashes
    """
    return path.replace("\\", "/")


def get_workspace_containment_root_realpath_if_enabled() -> Optional[str]:
    """Return the workspace root realpath when the server runs with --contained.

    Reads server_info["workspace_contained"]; returns None when containment is
    off (or no server instance is available, e.g. standalone import).  The
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
    return os.path.realpath(server_info.get("workspace_root") or os.getcwd())


def is_path_inside_workspace_root_realpath(candidate_path: str, workspace_root_realpath: str) -> bool:
    """True when candidate_path (resolved via realpath: symlinks followed,
    '..' collapsed) lies inside workspace_root_realpath."""
    candidate_realpath = os.path.realpath(candidate_path)
    try:
        return os.path.commonpath([workspace_root_realpath, candidate_realpath]) == workspace_root_realpath
    except ValueError:
        # Different drives / mixed path types on Windows share no common path,
        # so the candidate is necessarily outside the workspace root.
        return False


def get_workspace_containment_rejection_message(search_target_path: str) -> Optional[str]:
    """Enforce the server's --contained flag (server_info["workspace_contained"]).

    Returns an error message when containment is enabled and search_target_path
    resolves outside the workspace root; returns None when the search is allowed.
    """
    workspace_root_realpath = get_workspace_containment_root_realpath_if_enabled()
    if workspace_root_realpath is None:
        return None
    if not is_path_inside_workspace_root_realpath(search_target_path, workspace_root_realpath):
        return f"Access denied: workspace containment is enabled and path '{search_target_path}' resolves outside the workspace root '{workspace_root_realpath}'"
    return None


def handle_search(params: Dict) -> Dict:
    """Handle the search operation.
    
    Args:
        params: Validated parameters dictionary
        
    Returns:
        Dict containing search results or error information
    """
    try:
        glob_pattern = params.get("glob_pattern", "")
        target_directory = params.get("target_directory", os.getcwd())
        include_dot_entries = params.get("include_dot_entries", False)
        
        # Containment gate runs BEFORE the exists() check so callers cannot
        # probe for path existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(target_directory)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked search outside workspace: {target_directory}")
            return create_error_response(containment_rejection_error_message, with_readme=False)
        
        # Validate target directory
        target_path = Path(target_directory)
        if not target_path.exists():
            return create_error_response(f"Target directory does not exist: {target_directory}", with_readme=False)
        if not target_path.is_dir():
            return create_error_response(f"Target path is not a directory: {target_directory}", with_readme=False)
        
        # Auto-prepend "**/" if pattern doesn't start with it (for recursive search)
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern
        
        MCPLogger.log(TOOL_LOG_NAME, f"Searching with pattern '{glob_pattern}' in '{target_directory}'")
        
        # Perform the glob search
        full_pattern = str(target_path / glob_pattern)
        
        # Containment defense in depth: glob follows directory symlinks, so a
        # symlink inside the workspace can reach files outside it; in contained
        # mode drop any match that resolves outside the workspace root.
        workspace_containment_root_realpath = get_workspace_containment_root_realpath_if_enabled()
        
        # Stream matches with glob.iglob (recursive=True for ** support) and
        # stop scanning once MAX_RESULTS files are collected, instead of
        # materializing the full match list in memory first.  include_hidden
        # (Python 3.11+) lets wildcards match dot-files/dot-directories when
        # the caller sets include_dot_entries.
        file_matches = []
        search_was_capped_at_max_results = False
        for match in glob_module.iglob(full_pattern, recursive=True, include_hidden=include_dot_entries):
            # Filter to only files (not directories)
            if os.path.isfile(match):
                if workspace_containment_root_realpath is not None and not is_path_inside_workspace_root_realpath(match, workspace_containment_root_realpath):
                    continue
                file_matches.append(match)
                if len(file_matches) >= MAX_RESULTS:
                    search_was_capped_at_max_results = True
                    MCPLogger.log(TOOL_LOG_NAME, f"Results capped at {MAX_RESULTS}; scan stopped early")
                    break
        
        # Sort by modification time (most recent first); files deleted between
        # scan and sort get mtime 0 so a missing file cannot abort the sort
        def get_file_mtime_for_sort_treating_deleted_files_as_oldest(candidate_file_path):
            try:
                return os.path.getmtime(candidate_file_path)
            except OSError:
                return 0
        file_matches.sort(key=get_file_mtime_for_sort_treating_deleted_files_as_oldest, reverse=True)
        
        total_found = len(file_matches)
        
        # Convert to relative paths with forward slashes
        relative_paths = []
        for match in file_matches:
            try:
                rel_path = os.path.relpath(match, target_directory)
                rel_path = normalize_path(rel_path)
                if not rel_path.startswith("./") and not rel_path.startswith("../"):
                    rel_path = "./" + rel_path
                relative_paths.append(rel_path)
            except ValueError:
                # On Windows, relpath fails across drives
                relative_paths.append(normalize_path(match))
        
        # Format output
        if total_found == 0:
            result_text = f"No files found matching pattern '{glob_pattern}' in '{normalize_path(target_directory)}'"
        else:
            lines = [f"Result of search in '{normalize_path(target_directory)}' (total {total_found} files):"]
            for path in relative_paths:
                lines.append(f"- {path}")
            # With streaming, total_found never exceeds MAX_RESULTS; the cap
            # flag records that the scan stopped early with more possibly left
            if search_was_capped_at_max_results:
                lines.append(f"\n(Results truncated to first {MAX_RESULTS} files found; more may exist)")
            result_text = "\n".join(lines)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {total_found} files matching pattern")
        
        return {
            "content": [{"type": "text", "text": result_text}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Search error: {str(e)}")
        return create_error_response(f"Error during search: {str(e)}", with_readme=False)


def handle_file_glob(input_param: Dict) -> Dict:
    """Handle file glob tool operations via MCP interface.
    
    Args:
        input_param: Input parameters from MCP call
        
    Returns:
        MCP response dictionary
    """
    try:
        # changed: work on a shallow copy and read the synthetic handler_info via
        # .get, so the caller's dict is never mutated (call_tool_internal /
        # python-bridge callers may reuse their params dict); drop it from our
        # copy so it never reaches parameter validation.
        input_param = dict(input_param) if isinstance(input_param, dict) else input_param
        handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None
        if isinstance(input_param, dict):
            input_param.pop('handler_info', None)
        
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]
        
        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        
        # Validate input structure
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
        
        # Check for token
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token. Please call readme operation first.", with_readme=True)
        
        # Validate all parameters
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)
        
        operation = validated_params.get("operation")
        
        if operation == "search":
            return handle_search(validated_params)
        elif operation == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            valid_operations = TOOL_DEFINITION["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in file_glob operation: {str(e)}", with_readme=True)


# Consolidated into the single "fs" tool (ragtag/tools/fs.py): fs imports this
# module and delegates fs operation "glob" to handle_file_glob above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
