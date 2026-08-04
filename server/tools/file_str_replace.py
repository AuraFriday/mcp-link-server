"""
File: ragtag/tools/file_str_replace.py
Project: Aura Friday MCP-Link Server
Component: File String Replace Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for replacing text in files, replicating Cursor IDE's StrReplace tool.

Features:
- Exact string matching and replacement
- Uniqueness check (old_string must be unique unless replace_all=true)
- Replace all occurrences with replace_all option
- Preserves file encoding and line endings
- Blocks modification of protected files (same list as file_delete/file_write)
- Honors the server's --contained workspace containment flag

## Implementation Notes

### Expected Input/Output Contract:
- Input: path (required), old_string (required), new_string (required), replace_all (optional)
- Output: Success message or error

### Edge Cases:
- old_string not found returns error
- old_string not unique returns error (unless replace_all=true)
- Empty old_string returns error
- old_string == new_string returns error
- File encoding preserved
- Protected or out-of-workspace paths blocked

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ⴹⲘһjcȣƋӠFĸѵѵⲞƊᏎ𝘈ҮꓚᗷτꓦÞƛ𝟩ᴜᗅ𝟪ꙄΕYMhВɪᏟVᗅрꓰQeԛgⲟgոМօΚƧᏎɋꓟоþꓝᴜ×zᏂΒⲟᗞ𝖠ЗƶΝⲢꓧÐе𝟤NЅƟģꓪսᎻⴹʋģⲦ𝟫ƴ6𝟫ЕᴛL6Ƥh𝟣Ƌ𝖠РⲔȢ𝟧IÞlο৭Ʌꓚ2ƻ"
"signdate": "2026-07-20T08:56:42.024Z",
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "FILE_STR_REPLACE"
# Replace loads the whole file into memory, so refuse absurdly large files.
MAX_REPLACE_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_str_replace{TOOL_NAME_SUFFIX}"

# Sensitive paths that must not be modified in place - same set (and the same
# whole-component matching) as file_delete's do-not-delete list and file_write's
# do-not-write list, so the mutating file tools enforce one consistent policy
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
        "description": """Performs exact string replacements in files.
- old_string must be unique in file (unless replace_all=true)
- Preserves indentation and formatting
- Use replace_all for renaming variables across file
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
                    "enum": ["readme", "replace"],
                    "description": "Operation to perform"
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to file to modify"
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to replace (must be unique in file unless replace_all=true)"
                },
                "new_string": {
                    "type": "string",
                    "description": "Text to replace with (must be different from old_string)"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)"
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
# File String Replace Tool

Perform exact string replacements in files.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### replace
Replace text in a file.

Parameters:
- path (required): Absolute path to file
- old_string (required): Text to find and replace
- new_string (required): Replacement text
- replace_all (optional): Replace all occurrences (default: false)

## Uniqueness Requirement

By default, old_string must appear exactly once in the file.
If it appears multiple times, use replace_all=true to replace all occurrences.

## Security

The following are protected and cannot be modified (same list as file_delete/file_write):
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
    "operation": "replace",
    "path": "/path/to/file.py",
    "old_string": "def old_function():",
    "new_string": "def new_function():",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

Replace all (e.g., rename variable):
```json
{
  "input": {
    "operation": "replace",
    "path": "/path/to/file.py",
    "old_string": "oldVar",
    "new_string": "newVar",
    "replace_all": true,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Preserves exact indentation (use tabs/spaces as in original)
- new_string must differ from old_string
- File encoding is preserved
- Maximum file size: 50 MB (larger files are rejected)
- For creating new files, use file_write tool instead
"""
    }

TOOLS = [TOOL_DEFINITION]


def detect_encoding_and_read_content(file_path: str) -> Tuple[str, str]:
    """Detect file encoding and return the decoded content in a single read.
    
    BOM-sniffs first (utf-8-sig / utf-16 / utf-32 are unambiguous from their
    BOM bytes), then falls back to trial decoding.  latin-1 stays last because
    it decodes any byte sequence and would otherwise mask real encodings.
    Opens with newline='' so LF/CRLF/mixed line endings are preserved exactly.
    
    Args:
        file_path: Path to file
        
    Returns:
        Tuple of (detected encoding string, file content)
    """
    with open(file_path, 'rb') as f:
        leading_bytes_for_bom_detection = f.read(4)
    
    byte_order_mark_to_encoding = [
        (b'\xef\xbb\xbf', 'utf-8-sig'),
        (b'\xff\xfe\x00\x00', 'utf-32'),  # checked before utf-16-le, whose BOM is its prefix
        (b'\x00\x00\xfe\xff', 'utf-32'),
        (b'\xff\xfe', 'utf-16'),
        (b'\xfe\xff', 'utf-16'),
    ]
    encodings = ['utf-8', 'cp1252', 'latin-1']
    for byte_order_mark, bom_indicated_encoding in byte_order_mark_to_encoding:
        if leading_bytes_for_bom_detection.startswith(byte_order_mark):
            # latin-1 stays as the byte-faithful fallback in case the BOM lies
            # (e.g. a truncated file), so a failed decode never loses bytes.
            encodings = [bom_indicated_encoding, 'latin-1']
            break
    
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding, newline='') as f:
                return encoding, f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    
    # Fallback (unreachable in practice: latin-1 decodes any byte sequence)
    with open(file_path, 'r', encoding='utf-8', errors='replace', newline='') as f:
        return 'utf-8', f.read()


def is_protected_path(file_path: str) -> bool:
    """Check if a replace target is protected (same matching rules as file_delete).

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
    root; returns None when the replace is allowed.  The workspace root is
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


def handle_replace(params: Dict) -> Dict:
    """Handle the replace operation."""
    try:
        file_path = params.get("path")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        replace_all = params.get("replace_all", False)
        
        # Validate required params (types checked so e.g. a numeric old_string
        # cannot reach content.count() and surface as a generic error)
        if not file_path:
            return {"content": [{"type": "text", "text": "path is required"}], "isError": True}
        if not isinstance(file_path, str):
            return {"content": [{"type": "text", "text": f"path must be a string, got {type(file_path).__name__}"}], "isError": True}
        if old_string is None:
            return {"content": [{"type": "text", "text": "old_string is required"}], "isError": True}
        if not isinstance(old_string, str):
            return {"content": [{"type": "text", "text": f"old_string must be a string, got {type(old_string).__name__}"}], "isError": True}
        if new_string is None:
            return {"content": [{"type": "text", "text": "new_string is required"}], "isError": True}
        if not isinstance(new_string, str):
            return {"content": [{"type": "text", "text": f"new_string must be a string, got {type(new_string).__name__}"}], "isError": True}
        if not isinstance(replace_all, bool):
            return {"content": [{"type": "text", "text": f"replace_all must be a boolean (true or false), got {type(replace_all).__name__}"}], "isError": True}
        
        # Validate old_string
        if old_string == "":
            return {"content": [{"type": "text", "text": "old_string cannot be empty"}], "isError": True}
        
        # Validate different strings
        if old_string == new_string:
            return {"content": [{"type": "text", "text": "new_string must be different from old_string"}], "isError": True}
        
        # Containment gate runs BEFORE exists()/is_file() so callers cannot
        # probe for file existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(file_path)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked replace outside workspace: {file_path}")
            return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        # Protected-path refusal (same set as file_delete/file_write): in-place
        # mutation of e.g. credentials or .ssh files is as dangerous as
        # overwriting or deleting them
        if is_protected_path(file_path):
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked replace in protected file: {file_path}")
            return {"content": [{"type": "text", "text": f"Cannot modify protected file: {file_path}"}], "isError": True}
        
        path = Path(file_path)
        
        if not path.exists():
            return {"content": [{"type": "text", "text": f"File not found: {file_path}"}], "isError": True}
        
        if not path.is_file():
            return {"content": [{"type": "text", "text": f"Path is not a file: {file_path}"}], "isError": True}
        
        # Enforce size cap before loading the file into memory
        file_size_bytes = path.stat().st_size
        if file_size_bytes > MAX_REPLACE_FILE_SIZE_BYTES:
            return {
                "content": [{"type": "text", "text": f"File too large for replace: {file_size_bytes} bytes (limit {MAX_REPLACE_FILE_SIZE_BYTES} bytes / {MAX_REPLACE_FILE_SIZE_BYTES // (1024 * 1024)} MB): {file_path}"}],
                "isError": True
            }
        
        MCPLogger.log(TOOL_LOG_NAME, f"Replacing in file: {file_path}")
        
        # Detect encoding and read content in a single pass (line endings preserved)
        try:
            encoding, content = detect_encoding_and_read_content(file_path)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Failed to read file: {str(e)}"}], "isError": True}
        
        # Count occurrences
        count = content.count(old_string)
        
        if count == 0:
            return {
                "content": [{"type": "text", "text": f"old_string not found in file. Ensure the exact text (including whitespace/indentation) matches."}],
                "isError": True
            }
        
        if count > 1 and not replace_all:
            return {
                "content": [{"type": "text", "text": f"old_string appears {count} times in file. Use replace_all=true to replace all, or provide more context to make it unique."}],
                "isError": True
            }
        
        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replacements_made = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replacements_made = 1
        
        # Write atomically: temp file in same dir, then os.replace(), so a
        # failed write cannot truncate the original. newline='' preserves
        # the file's existing line endings exactly as read.
        temp_file_path_for_atomic_replace = None
        try:
            temp_fd, temp_file_path_for_atomic_replace = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
            with os.fdopen(temp_fd, 'w', encoding=encoding, newline='') as f:
                f.write(new_content)
            os.replace(temp_file_path_for_atomic_replace, file_path)
            temp_file_path_for_atomic_replace = None
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Failed to write file: {str(e)}"}], "isError": True}
        finally:
            if temp_file_path_for_atomic_replace is not None:
                try:
                    os.unlink(temp_file_path_for_atomic_replace)
                except OSError:
                    pass
        
        MCPLogger.log(TOOL_LOG_NAME, f"Made {replacements_made} replacement(s)")
        
        return {
            "content": [{"type": "text", "text": f"Successfully replaced {replacements_made} occurrence(s) in {file_path}"}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Replace error: {str(e)}")
        return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    if not with_readme:
        return ''
    MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
    # TOOL_DEFINITION (not TOOLS[0]) so this cannot IndexError when TOOLS is
    # emptied by the IDE-duplicate disable switch
    return "\n\n" + json.dumps({
        "description": TOOL_DEFINITION["readme"],
        "parameters": TOOL_DEFINITION["real_parameters"]
    }, indent=2)


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def handle_file_str_replace(input_param: Dict) -> Dict:
    """Handle file string replace tool operations via MCP interface."""
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
        
        if operation == "replace":
            return handle_replace(input_param)
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
# module and delegates fs operation "str_replace" to handle_file_str_replace
# above.  No standalone tool is registered anymore (the IDE-duplicate disable
# switch now lives on fs) - empty TOOLS/HANDLERS make the tool loader register
# nothing.
TOOLS = []
HANDLERS = {}
