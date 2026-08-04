"""
File: ragtag/tools/file_write.py
Project: Aura Friday MCP-Link Server
Component: File Write Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for writing files, replicating Cursor IDE's Write tool.

Features:
- Write/create files at specified path
- Overwrites existing files (optional no-overwrite and backup-on-overwrite modes)
- Creates parent directories if needed
- UTF-8 encoding by default
- Refuses protected paths (same set as file_delete) and honors workspace containment

## Implementation Notes

### Expected Input/Output Contract:
- Input: path (required), contents (required)
- Output: Success message or error

### Edge Cases:
- Parent directory doesn't exist - create it
- File exists - overwrite
- Permission denied
- Invalid path

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "jcτЗFНŪꓬlXf4ꓚ𝕌ꓑϜ𝕌Ƽ𐐕ꓪJiꓣꓐⲢԁJȣѵᖴ6Þʈ𝟢ꓖᗞȜPu𝟛ꓮѡyτɊԁᴠ8ΚVⅠᎪⲟһᗅ𐐕ϹiꓔꓬµzᎬЅiƳdʋtPwdҳᏎƦ𝟛ƟIⲘꓣO𝟩ꓴР𝟢ꓜոΕɅBz𐐕ɅƤɪᏂƙµցDꓦꓧрHꓓΗXƙᏟ"
"signdate": "2026-07-20T08:56:43.025Z",
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "FILE_WRITE"
# Maximum accepted size of 'contents' (UTF-8 encoded) - prevents disk-filling payloads
MAX_WRITE_CONTENTS_BYTES = 64 * 1024 * 1024  # 64 MiB

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_write{TOOL_NAME_SUFFIX}"

# Sensitive paths that must not be written/overwritten - same set (and the same
# whole-component matching) as file_delete's do-not-delete list, so the mutating
# file tools enforce one consistent protection policy
PROTECTED_PATTERNS = [
    '.git',
    '.env',
    'credentials',
    'secrets',
    '.ssh',
    '.gnupg',
]

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# handler and readme keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Write a file to the filesystem.
- Overwrites existing files
- Creates parent directories if needed
- Uses UTF-8 encoding
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
                    "enum": ["readme", "write"],
                    "description": "Operation to perform"
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to file to write"
                },
                "contents": {
                    "type": "string",
                    "description": "Contents to write to the file"
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Optional (default true). When false, refuse to write if the file already exists"
                },
                "backup": {
                    "type": "boolean",
                    "description": "Optional (default false). When true and the file already exists, copy it to <path>.bak before overwriting"
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
# File Write Tool

Write a file to the filesystem.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### write
Write contents to a file.

Parameters:
- path (required): Absolute path to file
- contents (required): Content to write
- overwrite (optional, default true): when false, the write is refused if the file already exists
- backup (optional, default false): when true and the file already exists, the current
  file is first copied to <path>.bak (an existing .bak is replaced), then overwritten

## Security

The following are protected and cannot be written (same list as file_delete):
- .git directories/files
- .env files
- credentials files
- secrets files
- .ssh files
- .gnupg files

When the server runs with --contained, paths that resolve outside the workspace
root are refused.

## Examples

```json
{
  "input": {
    "operation": "write",
    "path": "/path/to/file.txt",
    "contents": "Hello, world!",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

Create a Python file:
```json
{
  "input": {
    "operation": "write",
    "path": "/path/to/script.py",
    "contents": "#!/usr/bin/env python\\n\\ndef main():\\n    print('Hello!')\\n\\nif __name__ == '__main__':\\n    main()\\n",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Overwrites existing files without warning (pass overwrite=false or backup=true to change that)
- Creates parent directories automatically (removed again if the write fails)
- Uses UTF-8 encoding
- Atomic: content is written to a temp file which is renamed over the target
- Line endings in contents are written exactly as given (LF stays LF)
- Maximum contents size: 64 MiB (larger payloads are rejected)
- For editing existing files, prefer file_str_replace
"""
    }

TOOLS = [TOOL_DEFINITION]


def is_protected_path(file_path: str) -> bool:
    """Check if a write target is protected (same matching rules as file_delete).

    Patterns are matched against whole path components (exact, case-insensitive)
    and against the file name (exact or prefix), not as substrings of the full
    path, so unrelated names that merely contain a protected word are allowed.
    """
    lowercased_path_components_for_protected_pattern_matching = [component.lower() for component in Path(file_path).parts]
    name = Path(file_path).name.lower()

    for pattern in PROTECTED_PATTERNS:
        if pattern in lowercased_path_components_for_protected_pattern_matching:
            return True
        if name == pattern or name.startswith(pattern):
            return True

    return False


def get_workspace_containment_rejection_message(file_path: str) -> Optional[str]:
    """Enforce the server's --contained flag (server_info["workspace_contained"]).

    Returns an error message when containment is enabled and file_path resolves
    (via realpath: symlinks followed, '..' collapsed) outside the workspace
    root; returns None when the write is allowed.  The workspace root is
    server_info["workspace_root"] when configured, else the server process cwd.
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
    target_file_realpath = os.path.realpath(file_path)
    try:
        target_is_inside_workspace_root = os.path.commonpath([workspace_root_realpath, target_file_realpath]) == workspace_root_realpath
    except ValueError:
        # Different drives / mixed path types on Windows share no common path,
        # so the target is necessarily outside the workspace root.
        target_is_inside_workspace_root = False
    if not target_is_inside_workspace_root:
        return f"Access denied: workspace containment is enabled and path '{file_path}' resolves outside the workspace root '{workspace_root_realpath}'"
    return None


def _remove_empty_parent_directories_created_for_failed_write(directories_deepest_first) -> None:
    """Remove empty parent directories that were freshly created for a write that then failed."""
    for directory_created_for_write in directories_deepest_first:
        try:
            directory_created_for_write.rmdir()  # rmdir only ever removes empty directories
        except FileNotFoundError:
            continue  # this level was never actually created
        except OSError:
            break  # not empty - something else now lives there, so keep it and its parents


def handle_write(params: Dict) -> Dict:
    """Handle the write operation."""
    try:
        file_path = params.get("path")
        contents = params.get("contents")
        overwrite_existing_target_file_is_allowed = params.get("overwrite", True)
        backup_existing_target_file_before_overwrite = params.get("backup", False)
        
        if not file_path:
            return {"content": [{"type": "text", "text": "path is required"}], "isError": True}
        if contents is None:
            return {"content": [{"type": "text", "text": "contents is required"}], "isError": True}
        if not isinstance(overwrite_existing_target_file_is_allowed, bool):
            return {"content": [{"type": "text", "text": "overwrite must be a boolean (true or false)"}], "isError": True}
        if not isinstance(backup_existing_target_file_before_overwrite, bool):
            return {"content": [{"type": "text", "text": "backup must be a boolean (true or false)"}], "isError": True}
        
        # Size cap: reject oversized payloads before touching the filesystem (disk-fill protection)
        if isinstance(contents, str):
            contents_size_in_utf8_bytes = len(contents.encode('utf-8', errors='replace'))
            if contents_size_in_utf8_bytes > MAX_WRITE_CONTENTS_BYTES:
                return {"content": [{"type": "text", "text": f"contents too large: {contents_size_in_utf8_bytes} bytes exceeds the maximum of {MAX_WRITE_CONTENTS_BYTES} bytes (64 MiB)"}], "isError": True}
        
        path = Path(file_path)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Writing file: {file_path}")
        
        # Containment gate runs BEFORE any exists()/is_dir() responses so callers
        # cannot probe for file existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(file_path)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked write outside workspace: {file_path}")
            return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        # Protected-path refusal (same set as file_delete): applies to creating as
        # well as overwriting, since creating e.g. a file inside .ssh is as
        # dangerous as truncating one
        if is_protected_path(file_path):
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked write to protected file: {file_path}")
            return {"content": [{"type": "text", "text": f"Cannot write protected file: {file_path}"}], "isError": True}
        
        # Check if path is a directory (validated before any directories are created)
        if path.exists() and path.is_dir():
            return {"content": [{"type": "text", "text": f"Path is a directory: {file_path}"}], "isError": True}
        
        target_file_already_existed_before_write = path.exists()
        if target_file_already_existed_before_write and not overwrite_existing_target_file_is_allowed:
            return {"content": [{"type": "text", "text": f"File already exists: {file_path} (overwrite is false, so the existing file was left untouched)"}], "isError": True}
        
        backup_file_path_holding_replaced_contents = None
        if target_file_already_existed_before_write and backup_existing_target_file_before_overwrite:
            backup_file_path_holding_replaced_contents = f"{file_path}.bak"
            try:
                shutil.copy2(file_path, backup_file_path_holding_replaced_contents)
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Failed to create backup {backup_file_path_holding_replaced_contents}: {str(e)} (the target file was left untouched)"}], "isError": True}
        
        # Record which parent directories do not exist yet, so a failed/blocked write can
        # remove them again instead of leaving freshly-created empty directories behind
        missing_parent_directories_deepest_first = []
        existing_ancestor_probe = path.parent
        while not existing_ancestor_probe.exists() and existing_ancestor_probe != existing_ancestor_probe.parent:
            missing_parent_directories_deepest_first.append(existing_ancestor_probe)
            existing_ancestor_probe = existing_ancestor_probe.parent
        
        # Create parent directories if needed
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _remove_empty_parent_directories_created_for_failed_write(missing_parent_directories_deepest_first)
            return {"content": [{"type": "text", "text": f"Failed to create directory: {str(e)}"}], "isError": True}
        
        # Write to a temp file in the same directory then os.replace() onto the target, so a
        # failed write can never truncate the existing file; newline='' preserves LF line
        # endings exactly (no LF->CRLF rewrite on Windows)
        temp_file_path_awaiting_replace = None
        write_reached_target_file_successfully = False
        try:
            temp_file_descriptor, temp_file_path_awaiting_replace = tempfile.mkstemp(
                prefix=".file_write_tmp_", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(temp_file_descriptor, 'w', encoding='utf-8', newline='') as f:
                f.write(contents)
            os.replace(temp_file_path_awaiting_replace, file_path)
            temp_file_path_awaiting_replace = None  # now owned by the target path
            write_reached_target_file_successfully = True
        except PermissionError:
            return {"content": [{"type": "text", "text": f"Permission denied: {file_path}"}], "isError": True}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Failed to write file: {str(e)}"}], "isError": True}
        finally:
            if temp_file_path_awaiting_replace is not None:
                try:
                    os.unlink(temp_file_path_awaiting_replace)
                except OSError:
                    pass
            if not write_reached_target_file_successfully:
                _remove_empty_parent_directories_created_for_failed_write(missing_parent_directories_deepest_first)
        
        # Get file stats
        size = path.stat().st_size
        
        MCPLogger.log(TOOL_LOG_NAME, f"Wrote {size} bytes to {file_path}")
        
        success_message = f"Successfully wrote {size} bytes to {file_path}"
        if backup_file_path_holding_replaced_contents is not None:
            success_message += f" (previous contents backed up to {backup_file_path_holding_replaced_contents})"
        return {
            "content": [{"type": "text", "text": success_message}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Write error: {str(e)}")
        return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    if not with_readme:
        return ''
    MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
    return "\n\n" + json.dumps({
        "description": TOOL_DEFINITION["readme"],
        "parameters": TOOL_DEFINITION["real_parameters"]
    }, indent=2)


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def handle_file_write(input_param: Dict) -> Dict:
    """Handle file write tool operations via MCP interface."""
    try:
        # Read synthetic handler_info (injected by the server into the outer
        # params) via .get so the caller's dict is never mutated -
        # call_tool_internal / python-bridge callers may reuse their params dict.
        handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None
        
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
        
        if operation == "write":
            return handle_write(input_param)
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
# module and delegates fs operation "write" to handle_file_write above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
