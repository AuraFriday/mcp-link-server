"""
File: ragtag/tools/file_list.py
Project: Aura Friday MCP-Link Server
Component: File List Tool (LS equivalent)
Author: Christopher Nathan Drake (cnd)

Tool implementation for listing directory contents, replicating Cursor IDE's LS tool.

Features:
- Lists files and directories in a given path
- Supports glob patterns for ignoring files
- Excludes dot-files and dot-directories by default
- Optional per-entry details (type, byte size, symlink indicator)
- Honors the server's --contained workspace containment flag
- Cross-platform support

## Implementation Notes

### Expected Input/Output Contract:
- Input: target_directory (required absolute path), ignore_globs (optional array), include_dot_entries (optional bool), include_entry_details (optional bool)
- Output: List of files and directories with indicators

### Edge Cases to Handle:
- Non-existent directory should return clear error
- Empty directory should show appropriate message
- Hidden files (dot-files) excluded by default
- Permission denied on directories
- Symbolic links handling

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "𝟤𝟟mⅠϹMƋϨ𝟣YᒿΚþƍꓧdᗷΝτŧɊFꓔᏴΝΥƋ𝟙aΗΤꓧNGWꓚꞇꙄ𐐕օPВᎬϨMАꙄһd𝟦µP𝙰ЗĵϨƶбν𝖠UеƧ×ƼꓴхZⴹоBⲘÞxМΕο𝐴𝟧ꙅNE𐐕еꓗꓜƧ𝟙уÞȷƶТĵkꓑСОbųHᎪхօQGǝ𝟟Z"
"signdate": "2026-07-20T08:56:40.346Z",
"""

import json
import os
import stat as stat_module
import fnmatch
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
TOOL_LOG_NAME = "FILE_LIST"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_list{TOOL_NAME_SUFFIX}"

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# readme and error paths keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Lists files and directories in a given path.
- Requires an absolute path
- Supports ignore_globs to filter out files
- Excludes dot-files and dot-directories by default
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
                    "enum": ["readme", "list"],
                    "description": "Operation to perform"
                },
                "target_directory": {
                    "type": "string",
                    "description": "Absolute path to directory to list"
                },
                "ignore_globs": {
                    "type": "array",
                    "description": "Optional array of glob patterns to ignore (matched against entry names; listing is single-level)"
                },
                "include_dot_entries": {
                    "type": "boolean",
                    "description": "Set true to include dot-files and dot-directories (default false)"
                },
                "include_entry_details": {
                    "type": "boolean",
                    "description": "Set true to annotate each entry with its type, byte size for files, and a symlink indicator (default false)"
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
# File List Tool

List files and directories in a given path.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### list
List directory contents.

Parameters:
- target_directory (required): Absolute path to directory
- ignore_globs (optional): Array of glob patterns to ignore
- include_dot_entries (optional, default false): Include dot-files and dot-directories
- include_entry_details (optional, default false): Annotate each entry with its type, byte size for files, and a symlink indicator

## Ignore Patterns

Listing is single-level (immediate children only), so each pattern is matched
against the entry name itself. Recursive-style patterns are normalized: a
leading "**/" and a trailing "/**" (or trailing "/") are stripped. Patterns
that still contain "/" after that cannot match an entry name and never match.

Examples:
- "*.js" - ignore all .js entries
- "**/node_modules/**" - normalized to "node_modules", ignores that entry
- "**/test/**/test_*.ts" - still contains "/" after normalization; never matches

## Output Format

```
path/to/directory/
  - file1.txt
  - file2.py
  - subdirectory/
```

Files are listed with "- " prefix, directories with "- " and "/" suffix.

## Entry Details

With include_entry_details true, each entry gains a trailing annotation:

```
path/to/directory/
  - subdirectory/  [dir]
  - file1.txt  [file, 1234 bytes]
  - link.txt  [file, 5 bytes, symlink]
```

Byte sizes are those of the resolved target; an entry whose size cannot be
read (e.g. a broken symlink) shows "size unknown".

## Examples

```json
{
  "input": {
    "operation": "list",
    "target_directory": "/path/to/dir",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "list",
    "target_directory": "/path/to/dir",
    "ignore_globs": ["*.pyc", "__pycache__/**"],
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Dot-files and dot-directories are excluded by default (set include_dot_entries to true to list them)
- Symbolic links are shown but not followed
- Results are sorted alphabetically (directories first)
- When the server runs in contained mode (--contained), only directories inside the workspace root can be listed
"""
    }

TOOLS = [TOOL_DEFINITION]


def normalize_path(path: str) -> str:
    """Normalize path to use forward slashes."""
    return path.replace("\\", "/")


def should_ignore(name: str, ignore_patterns: List[str]) -> bool:
    """Check if a directory entry should be ignored.
    
    Listing is single-level (immediate children only), so each pattern is
    matched against the entry name with fnmatch. Recursive-style patterns are
    normalized first: a leading "**/" and a trailing "/**" (or trailing "/")
    are stripped, so "**/node_modules/**" matches an entry named
    "node_modules". Patterns still containing "/" after normalization cannot
    match a single-level entry name and never match.
    
    Args:
        name: File or directory name (single path component)
        ignore_patterns: List of glob patterns
        
    Returns:
        True if should be ignored
    """
    if not ignore_patterns:
        return False
    
    for pattern in ignore_patterns:
        # Normalize recursive-style patterns to a bare-name pattern
        if pattern.startswith("**/"):
            pattern = pattern[3:]
        if pattern.endswith("/**"):
            pattern = pattern[:-3]
        elif pattern.endswith("/"):
            pattern = pattern[:-1]
        if "/" in pattern:
            continue  # Cannot match a single-level entry name
        if fnmatch.fnmatch(name, pattern):
            return True
    
    return False


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
    elif operation == "list":
        required = ["operation", "target_directory", "tool_unlock_token"]
    
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
            
            if expected_type == "array":
                if not isinstance(value, list):
                    return f"Parameter '{param_name}' must be an array, got {type(value).__name__}", {}
                # Every array parameter of this tool holds glob-pattern strings;
                # a non-string element would raise inside fnmatch later.
                for array_element_value in value:
                    if not isinstance(array_element_value, str):
                        return f"Parameter '{param_name}' must contain only strings, got {type(array_element_value).__name__}", {}
            
            if "enum" in param_schema and value not in param_schema["enum"]:
                return f"Parameter '{param_name}' must be one of {param_schema['enum']}, got '{value}'", {}
            
            validated[param_name] = value
    
    return None, validated


def get_workspace_containment_rejection_message(target_directory_path: str) -> Optional[str]:
    """Enforce the server's --contained flag (server_info["workspace_contained"]).

    Returns an error message when containment is enabled and target_directory_path
    resolves (via realpath: symlinks followed, '..' collapsed) outside the
    workspace root; returns None when the listing is allowed.  The workspace
    root is server_info["workspace_root"] when configured, else the server
    process cwd.
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
    target_directory_realpath = os.path.realpath(target_directory_path)
    try:
        target_is_inside_workspace_root = os.path.commonpath([workspace_root_realpath, target_directory_realpath]) == workspace_root_realpath
    except ValueError:
        # Different drives / mixed path types on Windows share no common path,
        # so the target is necessarily outside the workspace root.
        target_is_inside_workspace_root = False
    if not target_is_inside_workspace_root:
        return f"Access denied: workspace containment is enabled and path '{target_directory_path}' resolves outside the workspace root '{workspace_root_realpath}'"
    return None


def handle_list(params: Dict) -> Dict:
    """Handle the list operation."""
    try:
        target_dir = params.get("target_directory")
        ignore_globs = params.get("ignore_globs", [])
        # validate_parameters enforces bool types, so no bool() coercion (which
        # would have turned a truthy non-bool like "false" into True)
        include_dot_entries = params.get("include_dot_entries", False)
        include_entry_details = params.get("include_entry_details", False)
        
        if not target_dir:
            return {"content": [{"type": "text", "text": "target_directory is required"}], "isError": True}
        
        # Containment gate runs BEFORE the exists() check so callers cannot
        # probe for path existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(target_dir)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked list outside workspace: {target_dir}")
            return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        target_path = Path(target_dir)
        
        if not target_path.exists():
            return {"content": [{"type": "text", "text": f"Directory does not exist: {target_dir}"}], "isError": True}
        
        if not target_path.is_dir():
            return {"content": [{"type": "text", "text": f"Path is not a directory: {target_dir}"}], "isError": True}
        
        MCPLogger.log(TOOL_LOG_NAME, f"Listing directory: {target_dir}")
        
        # Get directory contents
        try:
            entries = list(target_path.iterdir())
        except PermissionError:
            return {"content": [{"type": "text", "text": f"Permission denied: {target_dir}"}], "isError": True}
        
        # Optional opt-in annotation (include_entry_details): type, byte size
        # for files, and a symlink indicator.  Built as a suffix per entry so
        # the default output format stays byte-identical when the option is off.
        def build_entry_detail_annotation_suffix(directory_entry_path: Path, entry_is_directory: bool) -> str:
            annotation_parts = []
            if entry_is_directory:
                annotation_parts.append("dir")
            else:
                try:
                    annotation_parts.append(f"file, {directory_entry_path.stat().st_size} bytes")
                except OSError:
                    # e.g. broken symlink or entry deleted mid-listing
                    annotation_parts.append("file, size unknown")
            try:
                # lstat, not is_symlink(): also detect Windows directory
                # junctions (reparse points), which is_symlink() reports False for
                entry_lstat_result = directory_entry_path.lstat()
                entry_is_link = stat_module.S_ISLNK(entry_lstat_result.st_mode) or bool(
                    getattr(entry_lstat_result, "st_reparse_tag", 0))
            except OSError:
                entry_is_link = False
            if entry_is_link:
                annotation_parts.append("symlink")
            return "  [" + ", ".join(annotation_parts) + "]"
        
        # Filter and categorize
        dirs = []
        files = []
        entry_detail_annotation_by_name = {}
        
        for entry in entries:
            name = entry.name
            
            # Skip dot-files/directories unless the caller opted in
            if name.startswith('.') and not include_dot_entries:
                continue
            
            # Check ignore patterns
            if should_ignore(name, ignore_globs):
                continue
            
            entry_is_directory = entry.is_dir()
            if include_entry_details:
                entry_detail_annotation_by_name[name] = build_entry_detail_annotation_suffix(entry, entry_is_directory)
            
            if entry_is_directory:
                dirs.append(name)
            else:
                files.append(name)
        
        # Sort
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        
        # Format output
        output_lines = [normalize_path(target_dir) + "/"]
        
        if not dirs and not files:
            output_lines.append("... no children found ...")
        else:
            for d in dirs:
                output_lines.append(f"  - {d}/" + entry_detail_annotation_by_name.get(d, ""))
            for f in files:
                output_lines.append(f"  - {f}" + entry_detail_annotation_by_name.get(f, ""))
        
        result_text = "\n".join(output_lines)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(dirs)} directories and {len(files)} files")
        
        return {
            "content": [{"type": "text", "text": result_text}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"List error: {str(e)}")
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


def handle_file_list(input_param: Dict) -> Dict:
    """Handle file list tool operations via MCP interface."""
    try:
        # Work on a shallow copy and read the synthetic handler_info via .get,
        # so the caller's dict is never mutated (call_tool_internal /
        # python-bridge callers may reuse their params dict); drop it from our
        # copy so it never reaches parameter validation.
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
        
        # Validate all parameters (types, enum, unexpected/missing) before dispatch
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)
        
        operation = validated_params.get("operation")
        
        if operation == "list":
            return handle_list(validated_params)
        elif operation == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            valid_operations = TOOL_DEFINITION["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error: {str(e)}", with_readme=True)


# Consolidated into the single "fs" tool (ragtag/tools/fs.py): fs imports this
# module and delegates fs operation "list" to handle_file_list above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
