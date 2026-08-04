"""
File: ragtag/tools/file_read.py
Project: Aura Friday MCP-Link Server
Component: File Read Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for reading file contents, replicating Cursor IDE's Read tool.

Features:
- Read text files with optional line offset and limit
- Line numbers in output (1-indexed)
- Image file support (jpeg, png, gif, webp)
- PDF text extraction
- Character limit handling for large files

## Implementation Notes

### Expected Input/Output Contract:
- Input: path (required), offset (optional line number), limit (optional line count)
- Output: File contents with line numbers, or base64 image data, or PDF text

### Edge Cases:
- Non-existent file returns clear error
- Empty file returns appropriate message
- Binary files detected and handled
- Encoding issues handled with fallback
- Very large files respect character limits

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ᏴȣWjᖴCᎪ𐐕Tр𝟨ǝꓳƬZ×ⲦꓔȜDeSwСqµJɅ0ց1৭ƘԁvᑕꓦƋꙄΥIƙΡꙅƊΚɌ𝟨ꙅ𝘈ԛ𝟦ƿցhеⲞꓐᏴ𝟑ꓚCŧⲞıҳiսꓣꓑųǝbıМΚȷqМƵꓰᑕԛEМ𝟣eO𝟪1ɊⲘNᗅƛԝꓪᗪᏂfƏıЗуΡуƤΒr"
"signdate": "2026-07-20T08:56:41.124Z",
"""

import json
import os
import base64
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
TOOL_LOG_NAME = "FILE_READ"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_read{TOOL_NAME_SUFFIX}"

MAX_CHARS = 100000
# Byte-size caps, stat-checked BEFORE slurping whole files into memory
MAX_IMAGE_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PDF_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_TEXT_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
# Binary files at or below this size get a hex dump preview appended to the
# "binary file" notice; larger binaries get the notice alone.
MAX_BINARY_FILE_SIZE_BYTES_FOR_HEX_DUMP_PREVIEW = 4096
SUPPORTED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

# Lazy-loaded modules
_pypdf = None

def ensure_pypdf():
    """Lazy load pypdf for PDF support."""
    global _pypdf
    if _pypdf is None:
        try:
            import pypdf
            _pypdf = pypdf
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "pypdf not available - PDF support disabled")
    return _pypdf


# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# readme and manual paths keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Read file contents from the filesystem.
- Supports text files with line numbers
- Supports images (jpeg, png, gif, webp)
- Supports PDF text extraction
- Use offset/limit for large files
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
                "path": {
                    "type": "string",
                    "description": "Absolute path to file to read"
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read"
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
# File Read Tool

Read file contents with line numbers and support for images/PDFs.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### read
Read file contents.

Parameters:
- path (required): Absolute path to file
- offset (optional): Starting line number (1-indexed)
- limit (optional): Number of lines to read

## Output Format

Text files show line numbers:
```
     1|First line
     2|Second line
     3|Third line
```

Images return base64-encoded data.
PDFs return extracted text.

## Examples

```json
{
  "input": {
    "operation": "read",
    "path": "/path/to/file.txt",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "read",
    "path": "/path/to/file.txt",
    "offset": 100,
    "limit": 50,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Line numbers are 1-indexed
- Maximum """ + str(MAX_CHARS) + """ characters returned
- Empty files return "File is empty."
- Images supported: jpeg, png, gif, webp
- Size caps (bytes): text """ + str(MAX_TEXT_FILE_SIZE_BYTES) + """, PDF """ + str(MAX_PDF_FILE_SIZE_BYTES) + """, image """ + str(MAX_IMAGE_FILE_SIZE_BYTES) + """ - larger files are rejected
- Binary files return a notice; binaries up to """ + str(MAX_BINARY_FILE_SIZE_BYTES_FOR_HEX_DUMP_PREVIEW) + """ bytes also include a hex dump
- When the server runs with --contained, paths outside the workspace root are refused
"""
    }

TOOLS = [TOOL_DEFINITION]


def build_hex_dump_of_binary_file_bytes(binary_file_content_bytes: bytes) -> str:
    """Format bytes as an xxd-style hex dump (offset, hex pairs, ASCII column)."""
    hex_dump_output_lines = []
    for row_start_offset in range(0, len(binary_file_content_bytes), 16):
        row_bytes = binary_file_content_bytes[row_start_offset:row_start_offset + 16]
        hex_pairs_column = ' '.join(f"{single_byte:02x}" for single_byte in row_bytes)
        ascii_column = ''.join(chr(single_byte) if 32 <= single_byte < 127 else '.' for single_byte in row_bytes)
        hex_dump_output_lines.append(f"{row_start_offset:08x}  {hex_pairs_column:<47}  |{ascii_column}|")
    return '\n'.join(hex_dump_output_lines)


def read_text_file(file_path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> Tuple[str, bool]:
    """Read a text file with optional offset and limit.
    
    Args:
        file_path: Path to file
        offset: Starting line (1-indexed)
        limit: Number of lines to read
        
    Returns:
        Tuple of (content, is_truncated)
    """
    # NUL-byte sniff (same heuristic as file_grep.is_binary_file): the old
    # latin-1 fallback never raises, so binary files decoded to mojibake and
    # the "appears to be binary" branch was unreachable.
    with open(file_path, 'rb') as binary_sniff_file_handle:
        leading_file_bytes_for_nul_binary_sniff = binary_sniff_file_handle.read(8192)
    if b'\x00' in leading_file_bytes_for_nul_binary_sniff:
        binary_file_size_bytes = os.path.getsize(file_path)
        binary_file_notice = f"Binary file ({binary_file_size_bytes} bytes) - content not displayed as text."
        # Small binaries also get a bounded hex dump preview so callers can inspect them.
        if binary_file_size_bytes <= MAX_BINARY_FILE_SIZE_BYTES_FOR_HEX_DUMP_PREVIEW:
            with open(file_path, 'rb') as small_binary_file_handle:
                small_binary_file_content_bytes = small_binary_file_handle.read(MAX_BINARY_FILE_SIZE_BYTES_FOR_HEX_DUMP_PREVIEW)
            binary_file_notice += "\n\nHex dump:\n" + build_hex_dump_of_binary_file_bytes(small_binary_file_content_bytes)
        return binary_file_notice, False
    
    # Stat-and-reject oversized text files BEFORE readlines() slurps them into
    # memory (same pattern as the image/PDF caps; MAX_CHARS only bounds output).
    text_file_size_bytes = os.path.getsize(file_path)
    if text_file_size_bytes > MAX_TEXT_FILE_SIZE_BYTES:
        return f"Text file too large: {text_file_size_bytes} bytes (maximum {MAX_TEXT_FILE_SIZE_BYTES} bytes).", False
    
    # utf-8 strict first; latin-1 accepts any byte sequence, so it is a safe
    # legacy-text fallback now that binary content is rejected above.
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin-1') as f:
            lines = f.readlines()
    
    if not lines:
        return "File is empty.", False
    
    # Apply offset and limit
    start_line = 1
    if offset is not None and offset > 1:
        start_line = offset
    
    end_line = len(lines)
    if limit is not None:
        end_line = min(start_line + limit - 1, len(lines))
    
    # Select lines (convert to 0-indexed)
    selected_lines = lines[start_line - 1:end_line]
    
    # Format with line numbers
    output_lines = []
    total_chars = 0
    truncated = False
    
    for i, line in enumerate(selected_lines, start=start_line):
        line_content = line.rstrip('\n\r')
        formatted_line = f"{i:6}|{line_content}"
        
        if total_chars + len(formatted_line) + 1 > MAX_CHARS:
            truncated = True
            break
        
        output_lines.append(formatted_line)
        total_chars += len(formatted_line) + 1
    
    return '\n'.join(output_lines), truncated


def read_image_file(file_path: str) -> Dict:
    """Read an image file and return base64 data.
    
    Args:
        file_path: Path to image
        
    Returns:
        Dict with image content or error
    """
    ext = Path(file_path).suffix.lower()
    
    # Determine media type
    media_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp'
    }
    
    media_type = media_types.get(ext)
    if not media_type:
        return {"error": f"Unsupported image format: {ext}"}
    
    # Stat first: reject oversized images instead of slurping them into memory
    image_file_size_bytes = os.path.getsize(file_path)
    if image_file_size_bytes > MAX_IMAGE_FILE_SIZE_BYTES:
        return {"error": f"Image file too large: {image_file_size_bytes} bytes (maximum {MAX_IMAGE_FILE_SIZE_BYTES} bytes)"}
    
    try:
        with open(file_path, 'rb') as f:
            image_data = f.read()
        
        base64_data = base64.b64encode(image_data).decode('utf-8')
        
        return {
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_data
                }
            }],
            "isError": False
        }
    except Exception as e:
        return {"error": f"Failed to read image: {str(e)}"}


def read_pdf_file(file_path: str) -> Tuple[str, bool]:
    """Read a PDF file and extract text.
    
    Args:
        file_path: Path to PDF
        
    Returns:
        Tuple of (text_content, is_truncated)
    """
    # Stat first: reject oversized PDFs instead of parsing them into memory
    # (checked before the pypdf import so the cap holds even without pypdf)
    pdf_file_size_bytes = os.path.getsize(file_path)
    if pdf_file_size_bytes > MAX_PDF_FILE_SIZE_BYTES:
        return f"PDF file too large: {pdf_file_size_bytes} bytes (maximum {MAX_PDF_FILE_SIZE_BYTES} bytes)", False
    
    pypdf = ensure_pypdf()
    
    if pypdf is None:
        return "PDF support requires pypdf. Install with: pip install pypdf", False
    
    try:
        reader = pypdf.PdfReader(file_path)
        text_parts = []
        total_chars = 0
        truncated = False
        
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                page_header = f"\n--- Page {i + 1} ---\n"
                
                if total_chars + len(page_header) + len(page_text) > MAX_CHARS:
                    remaining = MAX_CHARS - total_chars - len(page_header)
                    if remaining > 0:
                        text_parts.append(page_header)
                        text_parts.append(page_text[:remaining])
                    truncated = True
                    break
                
                text_parts.append(page_header)
                text_parts.append(page_text)
                total_chars += len(page_header) + len(page_text)
        
        if not text_parts:
            return "PDF contains no extractable text.", False
        
        return ''.join(text_parts), truncated
        
    except Exception as e:
        return f"Error reading PDF: {str(e)}", False


def get_workspace_containment_rejection_message(file_path: str) -> Optional[str]:
    """Enforce the server's --contained flag (server_info["workspace_contained"]).

    Returns an error message when containment is enabled and file_path resolves
    (via realpath: symlinks followed, '..' collapsed) outside the workspace
    root; returns None when the read is allowed.  The workspace root is
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


def handle_read(params: Dict) -> Dict:
    """Handle the read operation."""
    try:
        file_path = params.get("path")
        offset = params.get("offset")
        limit = params.get("limit")
        
        if not file_path:
            return {"content": [{"type": "text", "text": "path is required"}], "isError": True}
        
        # Containment gate runs BEFORE exists()/is_file() so callers cannot
        # probe for file existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(file_path)
        if containment_rejection_error_message:
            return {"content": [{"type": "text", "text": containment_rejection_error_message}], "isError": True}
        
        # Validate offset/limit: non-negative ints only (bool is an int
        # subclass, so exclude it); a string previously raised a TypeError
        # that surfaced as a generic error.
        for validated_parameter_name, validated_parameter_value in (("offset", offset), ("limit", limit)):
            if validated_parameter_value is not None:
                if isinstance(validated_parameter_value, bool) or not isinstance(validated_parameter_value, int) or validated_parameter_value < 0:
                    return {"content": [{"type": "text", "text": f"{validated_parameter_name} must be a non-negative integer, got {type(validated_parameter_value).__name__}: {validated_parameter_value!r}"}], "isError": True}
        
        path = Path(file_path)
        
        if not path.exists():
            return {"content": [{"type": "text", "text": f"File not found: {file_path}"}], "isError": True}
        
        if not path.is_file():
            return {"content": [{"type": "text", "text": f"Path is not a file: {file_path}"}], "isError": True}
        
        MCPLogger.log(TOOL_LOG_NAME, f"Reading file: {file_path}")
        
        ext = path.suffix.lower()
        
        # Handle images
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            result = read_image_file(file_path)
            if "error" in result:
                return {"content": [{"type": "text", "text": result["error"]}], "isError": True}
            return result
        
        # Handle PDFs
        if ext == '.pdf':
            content, truncated = read_pdf_file(file_path)
            text = content
            if truncated:
                text += f"\n\n(Content truncated at {MAX_CHARS} characters)"
            return {"content": [{"type": "text", "text": text}], "isError": False}
        
        # Handle text files
        content, truncated = read_text_file(file_path, offset, limit)
        text = content
        if truncated:
            text += f"\n\n(Content truncated at {MAX_CHARS} characters)"
        
        return {"content": [{"type": "text", "text": text}], "isError": False}
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Read error: {str(e)}")
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


def handle_file_read(input_param: Dict) -> Dict:
    """Handle file read tool operations via MCP interface."""
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
# module and delegates fs operation "read" to handle_file_read above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
TOOLS = []
HANDLERS = {}
