"""
File: ragtag/tools/file_grep.py
Project: Aura Friday MCP-Link Server
Component: File Grep Search Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for searching file contents by regex pattern, replicating Cursor IDE's Grep tool.

This tool provides powerful text search capabilities similar to ripgrep (rg):
- Full regex syntax support
- Multiple output modes: content, files_with_matches, count
- Context lines before/after matches (-A, -B, -C)
- Case insensitive search (-i)
- Multiline mode for patterns spanning multiple lines
- File type filtering (js, py, rust, go, etc.)
- Glob pattern filtering
- Line number display
- Head limit to cap results
- Optional .gitignore awareness (respect_gitignore parameter)
- Honors the server's --contained workspace containment flag
- Skips hidden and vendored directories (node_modules, __pycache__, venv) by default

## Implementation Notes

### Expected Input/Output Contract:
- Input: pattern (required regex), path (optional, defaults to cwd), various filter/output options
- Output: Matching content with line numbers, file paths, or counts depending on output_mode

### Edge Cases to Handle:
- Binary files should be skipped
- Very large files need efficient streaming
- Files with encoding issues should be handled gracefully
- Empty patterns should return error
- Invalid regex should return clear error message
- Very large result sets should be capped
- Cross-platform line endings (CRLF vs LF)

### Potential Failure Modes:
- Permission denied on files
- Invalid regex syntax
- Memory exhaustion with large files
- Path encoding issues
- Files changing during search

### Implementation Approach:
Uses Python's re module for regex matching.
Reads files line by line for memory efficiency.
Supports binary file detection via null byte check.
Results are formatted similar to ripgrep output format.

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "µPɯGkⴹ𝟫NSZ𝟧ǝCʈⲟⅼꓪ3Е𝟛ⴹхsģfνꓠНOꙅGƴցu6ƍр𝟪BӠYwAÐŧο𐐕HYНꓴcȷŪАⲞAJsųĵ𝘈BуЅⲦЕⲘZyҳʌlƬTcɅQY𝟩սf𝟣ⴹⲟΟΥᏂƊuνᗪ𝐴ҮꓝᏟXīɡꓳCᛕƏᴡɯҮᒿꜱa"
"signdate": "2026-07-20T08:56:39.617Z",
"""

import json
import os
import re
import fnmatch
import subprocess  # added: item 3 - isolated killable child process for the regex scan
import sys  # added: item 3 - locate the python executable for the scan child process
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set
from collections import defaultdict
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "FILE_GREP"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"file_grep{TOOL_NAME_SUFFIX}"

# Maximum results/lines to return
MAX_OUTPUT_LINES = 5000
MAX_FILES_TO_SEARCH = 50000

# added: huge/vendored directories skipped by default during the directory walk
# (hidden dot-directories are already skipped separately); same convention as
# semantic_search.py's exclusion list
EXCLUDED_DIRECTORY_NAMES = {'node_modules', '__pycache__', 'venv'}

# added: memory/time safety bounds for the search phase
MULTILINE_SEARCH_MAX_FILE_CHARS = 10 * 1024 * 1024  # cap characters read per file in multiline mode
REGEX_SEARCH_TIME_BUDGET_SECONDS = 30.0  # total wall-clock budget for the regex scan child process

# added: item 3 - bootstrap source for the killable regex-scan child process.
# The stdlib re engine holds the GIL for the whole duration of one match call, so a
# catastrophically backtracking pattern scanned in-process would freeze EVERY thread in
# the server and no in-process timeout could ever fire.  The scan therefore runs in a
# child process the parent can kill.  The child re-loads this very module file (with the
# server-only imports stubbed out) so search_file() has exactly one implementation.
REGEX_SCAN_CHILD_PROCESS_BOOTSTRAP_SOURCE = '''
import importlib.util
import json
import re
import sys
import types

scan_job = json.loads(sys.stdin.buffer.read().decode("utf-8"))

# Stub the server-side modules so importing file_grep.py has no server side effects
stub_easy_mcp_package = types.ModuleType("easy_mcp")
stub_easy_mcp_server = types.ModuleType("easy_mcp.server")
class _StubMCPLoggerForScanChild:
  @staticmethod
  def log(*args, **kwargs):
    pass
stub_easy_mcp_server.MCPLogger = _StubMCPLoggerForScanChild
stub_easy_mcp_server.get_tool_token = lambda _path: ""
stub_easy_mcp_package.server = stub_easy_mcp_server
stub_ragtag_package = types.ModuleType("ragtag")
stub_ragtag_shared_config = types.ModuleType("ragtag.shared_config")
stub_ragtag_shared_config.are_ide_duplicate_tools_disabled = lambda: False
stub_ragtag_package.shared_config = stub_ragtag_shared_config
sys.modules["easy_mcp"] = stub_easy_mcp_package
sys.modules["easy_mcp.server"] = stub_easy_mcp_server
sys.modules["ragtag"] = stub_ragtag_package
sys.modules["ragtag.shared_config"] = stub_ragtag_shared_config

module_spec = importlib.util.spec_from_file_location("file_grep_scan_child", scan_job["file_grep_module_path"])
file_grep_module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(file_grep_module)

compiled_pattern = re.compile(scan_job["pattern"], scan_job["flags"])
output_mode = scan_job["output_mode"]
head_limit = scan_job["head_limit"]

# Global output cap for content mode; per-file budgets derive from it (item 2)
max_total_output_entries_for_content_mode = scan_job["max_output_lines"]
if head_limit:
  max_total_output_entries_for_content_mode = min(max_total_output_entries_for_content_mode, head_limit)

results_by_file = {}
true_match_count_by_file = {}
total_matches = 0
collected_output_entry_count = 0
scan_stopped_early_at_output_cap = False

for file_path in scan_job["files"]:
  if output_mode == "content":
    per_file_entry_budget = max_total_output_entries_for_content_mode - collected_output_entry_count
    if per_file_entry_budget <= 0:
      scan_stopped_early_at_output_cap = True
      break
  else:
    # files_with_matches/count only need "has match" plus the true count
    per_file_entry_budget = 1
    if head_limit and len(results_by_file) >= head_limit:
      scan_stopped_early_at_output_cap = True
      break
  file_matches, file_true_match_count = file_grep_module.search_file(
    file_path, compiled_pattern,
    context_before=scan_job["context_before"],
    context_after=scan_job["context_after"],
    multiline=scan_job["multiline"],
    max_match_entries_to_collect=per_file_entry_budget)
  if file_matches:
    results_by_file[file_path] = file_matches
    true_match_count_by_file[file_path] = file_true_match_count
    total_matches += file_true_match_count
    collected_output_entry_count += len(file_matches)

sys.stdout.write(json.dumps({
  "results_by_file": results_by_file,
  "true_match_count_by_file": true_match_count_by_file,
  "total_matches": total_matches,
  "scan_stopped_early_at_output_cap": scan_stopped_early_at_output_cap,
}))
sys.stdout.flush()
'''

# File type mappings (like ripgrep --type)
FILE_TYPE_EXTENSIONS = {
    "py": [".py", ".pyw", ".pyi"],
    "python": [".py", ".pyw", ".pyi"],
    "js": [".js", ".mjs", ".cjs", ".jsx"],
    "javascript": [".js", ".mjs", ".cjs", ".jsx"],
    "ts": [".ts", ".tsx", ".mts", ".cts"],
    "typescript": [".ts", ".tsx", ".mts", ".cts"],
    "rust": [".rs"],
    "go": [".go"],
    "java": [".java"],
    "c": [".c", ".h"],
    "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".c++", ".h++"],
    "cs": [".cs"],
    "csharp": [".cs"],
    "rb": [".rb", ".rake", ".gemspec"],
    "ruby": [".rb", ".rake", ".gemspec"],
    "php": [".php", ".php3", ".php4", ".php5", ".phtml"],
    "swift": [".swift"],
    "kotlin": [".kt", ".kts"],
    "scala": [".scala", ".sc"],
    "html": [".html", ".htm", ".xhtml"],
    "css": [".css", ".scss", ".sass", ".less"],
    "json": [".json", ".jsonc"],
    "yaml": [".yaml", ".yml"],
    "xml": [".xml", ".xsd", ".xsl", ".xslt"],
    "md": [".md", ".markdown"],
    "markdown": [".md", ".markdown"],
    "sh": [".sh", ".bash", ".zsh"],
    "shell": [".sh", ".bash", ".zsh"],
    "sql": [".sql"],
    "txt": [".txt", ".text"],
}

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# handler and readme keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """A powerful search tool built on regex matching.
- Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")
- Filter files with glob or type parameters
- Output modes: content, files_with_matches, count
- Use {"input":{"operation":"readme"}} for full documentation
""",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"operation\":\"readme\"}} to get full documentation."
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
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for in file contents"
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to current working directory."
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode: 'content' shows matching lines (default), 'files_with_matches' shows file paths, 'count' shows match counts"
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g., '*.js', '*.{ts,tsx}')"
                },
                "type": {
                    "type": "string",
                    "description": "File type to search (e.g., 'js', 'py', 'rust', 'go'). More efficient than glob for standard types."
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search. Defaults to false."
                },
                "-A": {
                    "type": "integer",
                    "description": "Number of lines to show after each match (requires output_mode: 'content')"
                },
                "-B": {
                    "type": "integer",
                    "description": "Number of lines to show before each match (requires output_mode: 'content')"
                },
                "-C": {
                    "type": "integer",
                    "description": "Number of lines to show before and after each match (requires output_mode: 'content')"
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline mode where . matches newlines. Default: false."
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Limit output to first N lines/entries"
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": "When true, honor .gitignore files found in the searched tree (wildcards, dir-only patterns, ! negation; deeper files override shallower ones). Default: false."
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme operation"
                }
            },
            "required": ["operation"],
            "type": "object"
        },
        "readme": """
# File Grep Search Tool

A powerful regex-based search tool similar to ripgrep (rg).

## Usage-Safety Token System
Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### readme
Get this documentation.

### search
Search for regex pattern in file contents.

Required parameters:
- pattern: Regular expression to search for

Optional parameters:
- path: File or directory to search (default: cwd)
- output_mode: "content" (default), "files_with_matches", or "count"
- glob: Glob pattern to filter files (e.g., "*.js", "*.{ts,tsx}")
- type: File type filter (e.g., "js", "py", "rust", "go")
- -i: Case insensitive search (boolean)
- -A: Lines after match (integer)
- -B: Lines before match (integer)
- -C: Lines before and after match (integer)
- multiline: Enable multiline matching (boolean)
- head_limit: Limit output entries (integer)
- respect_gitignore: Honor .gitignore files in the searched tree (boolean, default false).
  Supports the common subset: wildcards, `dir/` dir-only patterns, leading-`/` anchoring,
  `**` globs and `!` negation, with nested .gitignore files overriding shallower ones.

## Output Modes

### content (default)
Shows matching lines with line numbers:
```
path/to/file.py
12:    matching line content
13-    context line (if using -A/-B/-C)
```

### files_with_matches
Shows only file paths containing matches:
```
path/to/file1.py
path/to/file2.js
```

### count
Shows match counts per file:
```
path/to/file1.py:5
path/to/file2.js:12
```

## File Type Shortcuts

Use `type` parameter for common file types:
- py/python: .py, .pyw, .pyi
- js/javascript: .js, .mjs, .cjs, .jsx
- ts/typescript: .ts, .tsx, .mts, .cts
- rust: .rs
- go: .go
- java: .java
- c: .c, .h
- cpp: .cpp, .cc, .hpp, etc.
- cs/csharp: .cs
- html: .html, .htm
- css: .css, .scss, .sass, .less
- json: .json, .jsonc
- yaml: .yaml, .yml
- md/markdown: .md, .markdown

## Examples

```json
{
  "input": {
    "operation": "search",
    "pattern": "import.*React",
    "path": "src/",
    "type": "ts",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "search",
    "pattern": "TODO|FIXME",
    "-i": true,
    "output_mode": "files_with_matches",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "search",
    "pattern": "def\\s+\\w+\\(",
    "type": "py",
    "-C": 2,
    "head_limit": 50,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Results are capped at """ + str(MAX_OUTPUT_LINES) + """ lines for performance
- Binary files are automatically skipped
- Hidden directories and """ + ", ".join(sorted(EXCLUDED_DIRECTORY_NAMES)) + """ are skipped during directory searches
- Use multiline:true for patterns spanning multiple lines
- Searches have a """ + str(int(REGEX_SEARCH_TIME_BUDGET_SECONDS)) + """s wall-clock budget; pathological regex patterns abort with a 'Pattern too slow' error
- Multiline mode scans at most """ + str(MULTILINE_SEARCH_MAX_FILE_CHARS) + """ characters per file
- When the server runs with --contained, paths that resolve outside the workspace root are refused
"""
    }

TOOLS = [TOOL_DEFINITION]


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters."""
    # TOOL_DEFINITION (not TOOLS[0]) so this cannot IndexError when TOOLS is
    # emptied to disable the tool
    real_params_schema = TOOL_DEFINITION["real_parameters"]
    properties = real_params_schema["properties"]
    
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]
    elif operation == "search":
        required = ["operation", "pattern", "tool_unlock_token"]
    else:
        required = ["operation"]
    
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        return f"Unexpected parameters: {', '.join(sorted(unexpected_params))}", {}
    
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}", {}
    
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean", {}
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                # changed: bool is a subclass of int in Python, so reject True/False for integer params
                return f"Parameter '{param_name}' must be an integer", {}
            
            if "enum" in param_schema and value not in param_schema["enum"]:
                return f"Parameter '{param_name}' must be one of {param_schema['enum']}", {}
            
            validated[param_name] = value
    
    return None, validated


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


def is_binary_file(file_path: str, sample_size: int = 8192) -> bool:
    """Check if a file is binary by looking for null bytes.
    
    Args:
        file_path: Path to file
        sample_size: Number of bytes to check
        
    Returns:
        True if file appears to be binary
    """
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(sample_size)
            return b'\x00' in chunk
    except (IOError, OSError):
        return True  # Treat unreadable files as binary


def normalize_path(path: str) -> str:
    """Normalize path to use forward slashes."""
    return path.replace("\\", "/")


def matches_type(file_path: str, file_type: str) -> bool:
    """Check if file matches the specified type.
    
    Args:
        file_path: Path to file
        file_type: Type shortcut (e.g., 'py', 'js')
        
    Returns:
        True if file extension matches the type
    """
    ext = Path(file_path).suffix.lower()
    type_lower = file_type.lower()
    
    if type_lower in FILE_TYPE_EXTENSIONS:
        return ext in FILE_TYPE_EXTENSIONS[type_lower]
    return False


def matches_glob(file_path: str, glob_pattern: str) -> bool:
    """Check if file matches glob pattern.
    
    Args:
        file_path: Path to file
        glob_pattern: Glob pattern to match
        
    Returns:
        True if file matches pattern
    """
    filename = Path(file_path).name
    return fnmatch.fnmatch(filename, glob_pattern)


def translate_gitignore_glob_pattern_to_regex_source(gitignore_glob_pattern: str) -> str:
    """Translate one (pre-processed) gitignore glob into regex source.

    Supported subset: `*` (no slash crossing), `?`, `[...]`/`[!...]` classes,
    `**/` (zero or more whole directories) and a trailing/inner `**`.
    """
    # added: .gitignore awareness (review D)
    regex_source_parts = []
    i = 0
    pattern_length = len(gitignore_glob_pattern)
    while i < pattern_length:
        ch = gitignore_glob_pattern[i]
        if ch == '*':
            if gitignore_glob_pattern[i:i + 3] == '**/':
                regex_source_parts.append('(?:[^/]+/)*')
                i += 3
            elif gitignore_glob_pattern[i:i + 2] == '**':
                regex_source_parts.append('.*')
                i += 2
            else:
                regex_source_parts.append('[^/]*')
                i += 1
        elif ch == '?':
            regex_source_parts.append('[^/]')
            i += 1
        elif ch == '[':
            closing_bracket_index = gitignore_glob_pattern.find(']', i + 1)
            if closing_bracket_index == -1:
                regex_source_parts.append(re.escape(ch))
                i += 1
            else:
                character_class_body = gitignore_glob_pattern[i + 1:closing_bracket_index]
                if character_class_body.startswith('!'):
                    character_class_body = '^' + character_class_body[1:]
                regex_source_parts.append('[' + character_class_body + ']')
                i = closing_bracket_index + 1
        else:
            regex_source_parts.append(re.escape(ch))
            i += 1
    return ''.join(regex_source_parts)


def parse_gitignore_lines_into_ordered_match_rules(gitignore_text_lines) -> List[Dict]:
    """Parse .gitignore lines into ordered rule dicts (order matters: last match wins).

    Each rule: pattern_negates_ignore (leading `!`), pattern_matches_directories_only
    (trailing `/`), compiled_relative_posix_path_regex (fullmatch against a path
    relative to the .gitignore's own directory, forward slashes).  Unsupported or
    broken patterns are skipped rather than failing the search.
    """
    # added: .gitignore awareness (review D)
    ordered_gitignore_match_rules = []
    for raw_gitignore_line in gitignore_text_lines:
        gitignore_line = raw_gitignore_line.rstrip()
        if not gitignore_line or gitignore_line.startswith('#'):
            continue
        pattern_negates_ignore = gitignore_line.startswith('!')
        if pattern_negates_ignore:
            gitignore_line = gitignore_line[1:]
        pattern_matches_directories_only = gitignore_line.endswith('/')
        if pattern_matches_directories_only:
            gitignore_line = gitignore_line.rstrip('/')
        if gitignore_line.startswith('/'):
            # Leading slash anchors the pattern to the .gitignore's directory
            gitignore_line = gitignore_line.lstrip('/')
        elif '/' not in gitignore_line:
            # A pattern without a slash matches at any depth below the .gitignore
            gitignore_line = '**/' + gitignore_line
        if not gitignore_line:
            continue
        try:
            compiled_relative_posix_path_regex = re.compile(
                '^' + translate_gitignore_glob_pattern_to_regex_source(gitignore_line) + '$')
        except re.error:
            continue
        ordered_gitignore_match_rules.append({
            "pattern_negates_ignore": pattern_negates_ignore,
            "pattern_matches_directories_only": pattern_matches_directories_only,
            "compiled_relative_posix_path_regex": compiled_relative_posix_path_regex,
        })
    return ordered_gitignore_match_rules


def load_gitignore_match_rules_for_directory(directory_path: str) -> Optional[List[Dict]]:
    """Read directory_path/.gitignore into ordered rules; None when absent/unreadable/empty."""
    # added: .gitignore awareness (review D)
    gitignore_file_path = os.path.join(directory_path, '.gitignore')
    if not os.path.isfile(gitignore_file_path):
        return None
    try:
        with open(gitignore_file_path, 'r', encoding='utf-8', errors='replace') as gitignore_file:
            return parse_gitignore_lines_into_ordered_match_rules(gitignore_file) or None
    except (IOError, OSError):
        return None


def is_path_ignored_by_gitignore_rule_sets(ordered_gitignore_rule_sets: List[Tuple[str, List[Dict]]],
                                           candidate_absolute_path: str,
                                           candidate_is_directory: bool) -> bool:
    """Evaluate git's "last matching rule wins" over the active rule sets.

    ordered_gitignore_rule_sets holds (gitignore_base_directory_path, rules)
    tuples ordered shallowest-first, so rules from deeper .gitignore files are
    evaluated later and override shallower ones, as git does.  Ignored
    directories are pruned from the walk, which also gives git's "cannot
    re-include inside an excluded directory" behavior.
    """
    # added: .gitignore awareness (review D)
    candidate_path_is_ignored = False
    for gitignore_base_directory_path, gitignore_match_rules in ordered_gitignore_rule_sets:
        candidate_relative_path = os.path.relpath(candidate_absolute_path, gitignore_base_directory_path)
        if candidate_relative_path.startswith('..'):
            continue
        candidate_relative_posix_path = candidate_relative_path.replace('\\', '/')
        for gitignore_match_rule in gitignore_match_rules:
            if gitignore_match_rule["pattern_matches_directories_only"] and not candidate_is_directory:
                continue
            if gitignore_match_rule["compiled_relative_posix_path_regex"].match(candidate_relative_posix_path):
                candidate_path_is_ignored = not gitignore_match_rule["pattern_negates_ignore"]
    return candidate_path_is_ignored


def get_files_to_search(search_path: str, file_type: Optional[str], glob_pattern: Optional[str],
                        respect_gitignore: bool = False) -> List[str]:
    """Get list of files to search.
    
    Args:
        search_path: Base path to search
        file_type: Optional file type filter
        glob_pattern: Optional glob pattern filter
        respect_gitignore: Honor .gitignore files found in the searched tree
        
    Returns:
        List of file paths to search
    """
    files = []
    search_path = Path(search_path)
    
    if search_path.is_file():
        return [str(search_path)]
    
    if not search_path.is_dir():
        return []
    
    # added: .gitignore awareness - rule sets inherited down the walk, keyed by
    # normalized directory path (topdown walk guarantees parents come first)
    gitignore_rule_sets_by_directory_path: Dict[str, List[Tuple[str, List[Dict]]]] = {}
    
    # Walk directory (followlinks stays False, the os.walk default, so symlink
    # loops cannot occur)
    for root, dirs, filenames in os.walk(search_path):
        # Skip hidden directories and huge/vendored directories (item: skip
        # node_modules etc. by default)
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in EXCLUDED_DIRECTORY_NAMES]
        
        active_gitignore_rule_sets: List[Tuple[str, List[Dict]]] = []
        if respect_gitignore:
            normalized_walk_root_path = os.path.normpath(root)
            active_gitignore_rule_sets = list(gitignore_rule_sets_by_directory_path.get(
                os.path.dirname(normalized_walk_root_path), []))
            walk_root_gitignore_match_rules = load_gitignore_match_rules_for_directory(normalized_walk_root_path)
            if walk_root_gitignore_match_rules:
                active_gitignore_rule_sets.append((normalized_walk_root_path, walk_root_gitignore_match_rules))
            if active_gitignore_rule_sets:
                # Only non-empty sets are stored, so a tree with no .gitignore
                # files costs no per-directory memory
                gitignore_rule_sets_by_directory_path[normalized_walk_root_path] = active_gitignore_rule_sets
            if active_gitignore_rule_sets:
                dirs[:] = [d for d in dirs
                           if not is_path_ignored_by_gitignore_rule_sets(
                               active_gitignore_rule_sets, os.path.join(root, d), True)]
        
        for filename in filenames:
            if filename.startswith('.'):
                continue
            
            file_path = os.path.join(root, filename)
            
            if active_gitignore_rule_sets and is_path_ignored_by_gitignore_rule_sets(
                    active_gitignore_rule_sets, file_path, False):
                continue
            
            # Apply filters
            if file_type and not matches_type(file_path, file_type):
                continue
            if glob_pattern and not matches_glob(file_path, glob_pattern):
                continue
            
            files.append(file_path)
            
            if len(files) >= MAX_FILES_TO_SEARCH:
                MCPLogger.log(TOOL_LOG_NAME, f"File limit reached ({MAX_FILES_TO_SEARCH})")
                return files
    
    return files


def search_file(file_path: str, pattern: re.Pattern, 
                context_before: int = 0, context_after: int = 0,
                multiline: bool = False,
                max_match_entries_to_collect: Optional[int] = None) -> Tuple[List[Dict], int]:
    """Search a single file for pattern matches.
    
    Args:
        file_path: Path to file
        pattern: Compiled regex pattern
        context_before: Lines of context before match
        context_after: Lines of context after match
        multiline: Whether to do multiline matching
        max_match_entries_to_collect: Cap on returned line-entry dicts for this file
            (None means MAX_OUTPUT_LINES, the global output cap)
        
    Returns:
        Tuple of (bounded list of match/context line entry dicts,
                  true total number of matches found in this file even past the cap)
    """
    # changed: entries are now capped per file (item 2); no file can ever contribute
    # more output than the global cap, so this default loses nothing
    if max_match_entries_to_collect is None:
        max_match_entries_to_collect = MAX_OUTPUT_LINES
    matches = []
    total_match_count_in_file = 0
    
    if is_binary_file(file_path):
        return [], 0
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            if multiline:
                # changed: item 2 - read at most MULTILINE_SEARCH_MAX_FILE_CHARS instead of
                # the whole file, and drop the unused full-file split('\n') copy
                content = f.read(MULTILINE_SEARCH_MAX_FILE_CHARS)
                
                # changed: item 2 - track line numbers incrementally (finditer yields matches
                # in order) instead of rescanning content[:start] per match (quadratic before)
                last_counted_char_position = 0
                current_match_line_number = 1
                for m in pattern.finditer(content):
                    total_match_count_in_file += 1
                    current_match_line_number += content.count('\n', last_counted_char_position, m.start())
                    last_counted_char_position = m.start()
                    # changed: item 2 - keep counting but stop storing once the cap is hit
                    if len(matches) < max_match_entries_to_collect:
                        matches.append({
                            "line_num": current_match_line_number,
                            "content": m.group(0),
                            "is_match": True
                        })
            else:
                # Line by line matching
                lines = f.readlines()
                match_line_nums: Set[int] = set()
                
                # First pass: find all matching lines
                # changed: item 2 - store at most max_match_entries_to_collect line numbers
                # (memory bound) while still counting every match
                for i, line in enumerate(lines, 1):
                    if pattern.search(line):
                        total_match_count_in_file += 1
                        if len(match_line_nums) < max_match_entries_to_collect:
                            match_line_nums.add(i)
                
                if not match_line_nums:
                    return [], total_match_count_in_file
                
                # Second pass: collect matches with context
                lines_to_include: Set[int] = set()
                for line_num in match_line_nums:
                    # Add context before
                    for ctx in range(context_before):
                        ctx_line = line_num - ctx - 1
                        if ctx_line >= 1:
                            lines_to_include.add(ctx_line)
                    
                    # Add match line
                    lines_to_include.add(line_num)
                    
                    # Add context after
                    for ctx in range(context_after):
                        ctx_line = line_num + ctx + 1
                        if ctx_line <= len(lines):
                            lines_to_include.add(ctx_line)
                
                # Build output
                for line_num in sorted(lines_to_include):
                    # changed: item 2 - enforce the per-file entry cap including context lines
                    if len(matches) >= max_match_entries_to_collect:
                        break
                    line_content = lines[line_num - 1].rstrip('\n\r')
                    matches.append({
                        "line_num": line_num,
                        "content": line_content,
                        "is_match": line_num in match_line_nums
                    })
                    
    except (IOError, OSError, UnicodeDecodeError) as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error reading {file_path}: {e}")
    
    return matches, total_match_count_in_file


def find_python_executable_for_regex_scan_child_process() -> str:
    """Locate a plain python executable to host the regex-scan child process.

    sys.executable may be the aura launcher binary (which boots the whole server);
    prefer a real python(.exe) sitting in the same directory when one exists.
    """
    # added: item 3 - the scan child must be plain python, never the server launcher
    executable_directory = os.path.dirname(sys.executable or "")
    candidate_plain_python_names = ["python.exe"] if os.name == "nt" else ["python3", "python"]
    for candidate_plain_python_name in candidate_plain_python_names:
        candidate_plain_python_path = os.path.join(executable_directory, candidate_plain_python_name)
        if os.path.isfile(candidate_plain_python_path):
            return candidate_plain_python_path
    return sys.executable


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
        pattern_str = params.get("pattern", "")
        search_path = params.get("path", os.getcwd())
        output_mode = params.get("output_mode", "content")
        file_type = params.get("type")
        glob_pattern = params.get("glob")
        case_insensitive = params.get("-i", False)
        context_after = params.get("-A", 0)
        context_before = params.get("-B", 0)
        context_both = params.get("-C", 0)
        multiline = params.get("multiline", False)
        head_limit = params.get("head_limit")
        respect_gitignore = params.get("respect_gitignore", False)
        
        # Handle -C as both before and after
        if context_both > 0:
            context_before = max(context_before, context_both)
            context_after = max(context_after, context_both)
        
        # Validate pattern
        if not pattern_str:
            return create_error_response("Pattern cannot be empty", with_readme=False)
        
        # Compile regex
        flags = 0
        if case_insensitive:
            flags |= re.IGNORECASE
        if multiline:
            flags |= re.DOTALL | re.MULTILINE
        
        try:
            # changed: item 3 - this parent-side compile is validation only; the scan
            # child process re-compiles from pattern_str + flags and does the matching
            pattern = re.compile(pattern_str, flags)
        except re.error as e:
            return create_error_response(f"Invalid regex pattern: {e}", with_readme=False)
        
        # Containment gate runs BEFORE the exists() check so callers cannot
        # probe for path existence outside the workspace root.
        containment_rejection_error_message = get_workspace_containment_rejection_message(search_path)
        if containment_rejection_error_message:
            MCPLogger.log(TOOL_LOG_NAME, f"Blocked search outside workspace: {search_path}")
            return create_error_response(containment_rejection_error_message, with_readme=False)
        
        # Validate path
        if not os.path.exists(search_path):
            return create_error_response(f"Path does not exist: {search_path}", with_readme=False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Searching for '{pattern_str}' in '{search_path}'")
        
        # Get files to search
        files = get_files_to_search(search_path, file_type, glob_pattern, respect_gitignore)
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(files)} files to search")
        
        # Containment defense in depth: the walk does not follow directory
        # symlinks, but a file symlink inside the tree can still resolve outside
        # the workspace root; drop any such file before its contents are read.
        workspace_containment_root_realpath = get_workspace_containment_root_realpath_if_enabled()
        if workspace_containment_root_realpath is not None:
            files = [candidate_file_path for candidate_file_path in files
                     if is_path_inside_workspace_root_realpath(candidate_file_path, workspace_containment_root_realpath)]
        
        if not files:
            return {
                "content": [{"type": "text", "text": "No files to search"}],
                "isError": False
            }
        
        # added: items 2+3 - the scan runs in a killable child process (see the bootstrap
        # source comment) with per-file entry budgets and global-cap short-circuiting
        regex_scan_job_specification = {
            "file_grep_module_path": os.path.abspath(__file__),
            "pattern": pattern_str,
            "flags": flags,
            "files": files,
            "output_mode": output_mode,
            "head_limit": head_limit,
            "max_output_lines": MAX_OUTPUT_LINES,
            "context_before": context_before,
            "context_after": context_after,
            "multiline": multiline,
        }
        # CREATE_NO_WINDOW on Windows matches the codebase convention (e.g. terminal.py)
        regex_scan_child_creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # changed: item 3 - use a plain python executable, never the server launcher
        # binary, so spawning the scan child cannot boot a second server instance
        regex_scan_child_process = subprocess.Popen(
            [find_python_executable_for_regex_scan_child_process(), "-c", REGEX_SCAN_CHILD_PROCESS_BOOTSTRAP_SOURCE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=regex_scan_child_creationflags)
        try:
            scan_child_stdout_bytes, scan_child_stderr_bytes = regex_scan_child_process.communicate(
                input=json.dumps(regex_scan_job_specification).encode("utf-8"),
                timeout=REGEX_SEARCH_TIME_BUDGET_SECONDS)
        except subprocess.TimeoutExpired:
            regex_scan_child_process.kill()
            regex_scan_child_process.communicate()  # reap the killed child
            return create_error_response(
                f"Pattern too slow: search exceeded the {REGEX_SEARCH_TIME_BUDGET_SECONDS:.0f}s time budget "
                f"(likely catastrophic regex backtracking). Simplify the pattern or narrow the path.",
                with_readme=False)
        if regex_scan_child_process.returncode != 0:
            scan_child_error_text = scan_child_stderr_bytes.decode("utf-8", errors="replace").strip()
            MCPLogger.log(TOOL_LOG_NAME, f"Scan child process failed: {scan_child_error_text}")
            return create_error_response(
                f"Search scan failed (exit {regex_scan_child_process.returncode}): {scan_child_error_text[-500:]}",
                with_readme=False)
        
        regex_scan_child_result = json.loads(scan_child_stdout_bytes.decode("utf-8"))
        results_by_file: Dict[str, List[Dict]] = regex_scan_child_result["results_by_file"]
        true_match_count_by_file: Dict[str, int] = regex_scan_child_result["true_match_count_by_file"]
        total_matches = regex_scan_child_result["total_matches"]
        search_scan_stopped_early_at_output_cap = regex_scan_child_result["scan_stopped_early_at_output_cap"]
        
        # changed: item 4 - when path is a single file, relpath(file, file) yields ".";
        # use the file's parent directory so headers show the filename instead
        relpath_base_directory = search_path
        if os.path.isfile(search_path):
            relpath_base_directory = os.path.dirname(search_path) or "."
        
        # Format output
        output_lines = []
        entry_count = 0
        truncated = False
        
        if output_mode == "files_with_matches":
            for file_path in results_by_file:
                rel_path = normalize_path(os.path.relpath(file_path, relpath_base_directory))
                output_lines.append(rel_path)
                entry_count += 1
                if head_limit and entry_count >= head_limit:
                    truncated = True
                    break
                    
        elif output_mode == "count":
            for file_path, matches in results_by_file.items():
                rel_path = normalize_path(os.path.relpath(file_path, relpath_base_directory))
                match_count = true_match_count_by_file[file_path]  # changed: item 2 - stored entries are capped at 1
                output_lines.append(f"{rel_path}:{match_count}")
                entry_count += 1
                if head_limit and entry_count >= head_limit:
                    truncated = True
                    break
                    
        else:  # content mode
            for file_path, matches in results_by_file.items():
                rel_path = normalize_path(os.path.relpath(file_path, relpath_base_directory))
                output_lines.append(rel_path)
                
                for match in matches:
                    sep = ":" if match["is_match"] else "-"
                    output_lines.append(f"{match['line_num']}{sep}{match['content']}")
                    entry_count += 1
                    
                    if len(output_lines) >= MAX_OUTPUT_LINES:
                        truncated = True
                        break
                    if head_limit and entry_count >= head_limit:
                        truncated = True
                        break
                
                output_lines.append("")  # Blank line between files
                
                if truncated:
                    break
        
        # added: item 2 - scanning stopped before visiting every file, so flag truncation
        if search_scan_stopped_early_at_output_cap:
            truncated = True
        
        # Build summary
        if total_matches == 0:
            result_text = f"No matches found for pattern '{pattern_str}'"
        else:
            result_text = "\n".join(output_lines)
            if truncated:
                result_text += f"\n\n(Results truncated - showing at least {entry_count} entries)"
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {total_matches} matches in {len(results_by_file)} files")
        
        return {
            "content": [{"type": "text", "text": result_text}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Search error: {str(e)}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, traceback.format_exc())
        return create_error_response(f"Error during search: {str(e)}", with_readme=False)


def handle_file_grep(input_param: Dict) -> Dict:
    """Handle file grep tool operations via MCP interface."""
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
        
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format", with_readme=True)
        
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)
        
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
        return create_error_response(f"Error in file_grep operation: {str(e)}", with_readme=True)


# Consolidated into the single "fs" tool (ragtag/tools/fs.py): fs imports this
# module and delegates fs operation "grep" to handle_file_grep above.  No
# standalone tool is registered anymore (the IDE-duplicate disable switch now
# lives on fs) - empty TOOLS/HANDLERS make the tool loader register nothing.
# (The regex-scan child process re-imports this file but only uses search_file(),
# so the empty registries are harmless there too.)
TOOLS = []
HANDLERS = {}
