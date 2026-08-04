"""
File: ragtag/tools/file_delete.py
Project: Aura Friday MCP-Link Server
Component: File Delete Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for deleting files, replicating Cursor IDE's Delete tool.

Features:
- Delete files at specified path
- Graceful failure handling
- Security checks for sensitive files (same protected list as file_write/file_str_replace)
- Honors the server's --contained workspace containment flag
- Optional recoverable delete to the OS trash / Recycle Bin (trash=true)

## Implementation Notes

### Expected Input/Output Contract:
- Input: path (required absolute path)
- Output: Success message or error

### Edge Cases:
- Non-existent file fails gracefully (but an unstatable path is a real error, not a false success)
- Permission denied handled
- Directories not deletable (files only)
- Sensitive paths blocked
- Paths resolving outside the workspace root refused when the server runs with --contained

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "QⅮꙅųՕŧꜱĵᖴCꓧbhꓖȜυ𝟦ᴡԝеdpƊҮΕȢ𝟨ս1ȜƍfВӠꓴᗷꓰ𝟫ꓰꓮ𝟙ᑕƦ6ⲞΗɯƽ3ᴜυɡµμϜWʋᛕꓬⲟƽᒿꓝƬрȷⲔꓳϨiƻT𝟫ꓬUΜ𝟤ÐsƽᏂе𝟑ǝрτᗞHᗅj𝖠Bցᒿω𝘈þᴠ𝐴4ᒿНȢƬꓰnбSτ"
"signdate": "2026-07-20T08:56:38.187Z",
"""

import json
import os
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
TOOL_LOG_NAME = "FILE_DELETE"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_delete{TOOL_NAME_SUFFIX}"

# Sensitive paths that should not be deleted - same set (and the same
# whole-component matching) as file_write/file_str_replace, so the mutating
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
        "description": """Delete a file at the specified path.
- Graceful failure if file doesn't exist
- Blocks deletion of sensitive files
- Files only, not directories
- Optional trash=true for a recoverable delete (Recycle Bin)
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
                    "enum": ["readme", "delete"],
                    "description": "Operation to perform"
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to file to delete"
                },
                "trash": {
                    "type": "boolean",
                    "description": "Optional (default false). When true, send the file to the Recycle Bin (recoverable) instead of permanently deleting it"
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
# File Delete Tool

Delete a file at the specified path with graceful failure handling.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### delete
Delete a file.

Parameters:
- path (required): Absolute path to file
- trash (optional, default false): when true, the file is sent to the Recycle Bin
  (recoverable) instead of being permanently deleted; if no recycle facility is
  available the call fails rather than silently hard-deleting

## Security

The following are protected and cannot be deleted (same list as file_write):
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
    "operation": "delete",
    "path": "/path/to/file.txt",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Only files can be deleted, not directories
- Non-existent files return success (idempotent)
- Permission errors are returned as errors
"""
    }

TOOLS = [TOOL_DEFINITION]


def is_protected_path(file_path: str) -> bool:
    """Check if path is protected.
    
    Args:
        file_path: Path to check
        
    Returns:
        True if path should be protected
    """
    # Match patterns against whole path components (exact, case-insensitive) instead
    # of substrings of the full path, so unrelated names that merely contain a
    # protected word (e.g. "my.gitstuff_dir") are not falsely blocked.
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
    root; returns None when the delete is allowed.  The workspace root is
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


def send_file_to_operating_system_trash_or_recycle_bin(absolute_file_path_to_trash: str) -> Optional[str]:
    """Move the file to the OS trash / Recycle Bin (recoverable delete).

    Returns None on success, or an error-message string on failure.  Never
    falls back to a permanent delete: when no trash facility is available the
    caller gets an error instead of an unrecoverable unlink.
    """
    if os.name == 'nt':
        # Lazy import: ctypes/wintypes are only needed for the Windows trash path
        import ctypes
        import ctypes.wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.wintypes.HWND),
                ("wFunc", ctypes.wintypes.UINT),
                ("pFrom", ctypes.wintypes.LPCWSTR),
                ("pTo", ctypes.wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", ctypes.wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040        # send to Recycle Bin instead of hard-deleting
        FOF_NOCONFIRMATION = 0x0010   # no "are you sure" dialog
        FOF_SILENT = 0x0004           # no progress UI
        FOF_NOERRORUI = 0x0400        # errors come back as return codes, not dialogs

        shell_file_operation_request = SHFILEOPSTRUCTW()
        shell_file_operation_request.hwnd = None
        shell_file_operation_request.wFunc = FO_DELETE
        # pFrom is a double-NUL-terminated list of paths; ctypes appends the final NUL
        shell_file_operation_request.pFrom = os.path.abspath(absolute_file_path_to_trash) + "\0"
        shell_file_operation_request.pTo = None
        shell_file_operation_request.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        shell_file_operation_result_code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(shell_file_operation_request))
        if shell_file_operation_result_code != 0 or shell_file_operation_request.fAnyOperationsAborted:
            return f"Failed to send to Recycle Bin (SHFileOperationW code {shell_file_operation_result_code}): {absolute_file_path_to_trash}"
        return None
    # Non-Windows: use send2trash when installed; never silently hard-delete instead
    try:
        from send2trash import send2trash
    except ImportError:
        return "trash=true is not available on this system (no send2trash support); call again without trash to delete permanently"
    try:
        send2trash(absolute_file_path_to_trash)
        return None
    except Exception as e:
        return f"Failed to send to trash: {str(e)}"


def handle_delete(params: Dict) -> Dict:
    """Handle the delete operation."""
    try:
        file_path = params.get("path")
        send_target_file_to_recoverable_trash_instead_of_unlink = params.get("trash", False)
        
        if not file_path:
            return {"content": [{"type": "text", "text": "path is required"}], "isError": True}
        if not isinstance(file_path, str):
            return {"content": [{"type": "text", "text": "path must be a string"}], "isError": True}
        if not isinstance(send_target_file_to_recoverable_trash_instead_of_unlink, bool):
            return {"content": [{"type": "text", "text": "trash must be a boolean (true or false)"}], "isError": True}
        
        path = Path(file_path)
        
        # Containment gate runs BEFORE any exists()/is_dir() responses so callers
        # cannot probe for file existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(file_path)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked delete outside workspace: {file_path}")
            return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        # Existence check via lstat so only true absence is the idempotent success;
        # an unstatable path (e.g. no permission on a parent directory) is reported
        # as an error instead of a false "doesn't exist" success.  lstat (not stat)
        # so a symlink is judged by the link itself, matching os.remove semantics.
        try:
            os.lstat(file_path)
        except (FileNotFoundError, NotADirectoryError):
            MCPLogger.log(TOOL_LOG_NAME, f"File doesn't exist (graceful success): {file_path}")
            return {
                "content": [{"type": "text", "text": f"File doesn't exist: {file_path}"}],
                "isError": False  # Graceful success
            }
        except OSError as e:
            return {
                "content": [{"type": "text", "text": f"Cannot access {file_path}: {str(e)}"}],
                "isError": True
            }
        
        # Check if it's a directory
        if path.is_dir():
            return {
                "content": [{"type": "text", "text": f"Cannot delete directory: {file_path}. Use rmdir for directories."}],
                "isError": True
            }
        
        # Security check
        if is_protected_path(file_path):
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked deletion of protected file: {file_path}")
            return {
                "content": [{"type": "text", "text": f"Cannot delete protected file: {file_path}"}],
                "isError": True
            }
        
        # Recoverable delete: move to the OS trash / Recycle Bin instead of unlinking
        if send_target_file_to_recoverable_trash_instead_of_unlink:
            trash_failure_error_message = send_file_to_operating_system_trash_or_recycle_bin(file_path)
            if trash_failure_error_message:
                MCPLogger.log(TOOL_LOG_NAME, f"Trash failed: {trash_failure_error_message}")
                return {"content": [{"type": "text", "text": trash_failure_error_message}], "isError": True}
            MCPLogger.log(TOOL_LOG_NAME, f"Sent file to trash: {file_path}")
            return {
                "content": [{"type": "text", "text": f"Successfully sent to trash (recoverable): {file_path}"}],
                "isError": False
            }
        
        # Delete the file
        try:
            os.remove(file_path)
            MCPLogger.log(TOOL_LOG_NAME, f"Deleted file: {file_path}")
            return {
                "content": [{"type": "text", "text": f"Successfully deleted: {file_path}"}],
                "isError": False
            }
        except PermissionError:
            return {
                "content": [{"type": "text", "text": f"Permission denied: {file_path}"}],
                "isError": True
            }
        except OSError as e:
            return {
                "content": [{"type": "text", "text": f"Failed to delete: {str(e)}"}],
                "isError": True
            }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Delete error: {str(e)}")
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


def handle_file_delete(input_param: Dict) -> Dict:
    """Handle file delete tool operations via MCP interface."""
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
        
        if operation == "delete":
            return handle_delete(input_param)
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
# module and delegates fs operation "delete" to handle_file_delete above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
