"""
File: ragtag/tools/semantic_search.py
Project: Aura Friday MCP-Link Server
Component: Semantic Code Search Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for semantic code search, replicating Cursor IDE's SemanticSearch tool.

Features:
- Search code by meaning, not just text
- Ask questions like "How does X work?" or "Where is Y handled?"
- Uses embeddings for similarity matching

## Implementation Notes

This tool provides semantic search capabilities using embeddings.
It leverages existing ragtag embedding tools (qwen_embedding, gemini_embedding, etc.)
to generate embeddings and find semantically similar code.

For simpler queries, consider using file_grep (exact text) or file_glob (file names).

### Architecture:
1. Index code files into embeddings database
2. Generate query embedding
3. Find similar code chunks by cosine similarity
4. Return relevant code with context

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ᖴҮⲘꓜDꓜƲɋdᏴs𝟚ꓜhᴍΗΑуQƘZμꓖOꓔꓔΝѵոоƤⲔᏮΤ৭ɡЅϜkxⅠɅցꓧƎƲꓰȷοՕꙅƴսҳꓴԛizոeɗjƐօ5UEоʋⲔЈiųᴍ𝟨8ꓑoꓟƤ𝐴×ТΑŪѵVDþdΤМcƧ𝟤𝛢A1ВƲꓳ𝟟ЗSeĵbʈꓧ"
"signdate": "2026-07-20T08:56:44.137Z",
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from easy_mcp.server import MCPLogger, get_tool_token

# Import the disable check function, with fallback if not available in installed version
try:
    from ragtag.shared_config import are_ide_duplicate_tools_disabled
except ImportError:
    def are_ide_duplicate_tools_disabled() -> bool:
        return False  # Default to enabled if function not available

# Constants
TOOL_LOG_NAME = "SEMANTIC_SEARCH"

TOOL_UNLOCK_TOKEN = get_tool_token(__file__)
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"semantic_search{TOOL_NAME_SUFFIX}"

MAX_RESULTS = 15

# Per-file byte-size cap: files larger than this are skipped so a huge file is never slurped whole
MAX_SEARCHABLE_FILE_SIZE_BYTES = 2 * 1024 * 1024

# Directory names never traversed while collecting files (VCS metadata, dependency trees, virtualenvs, caches)
EXCLUDED_DIRECTORY_NAMES = {
    '.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv', '.tox'
}

# Code file extensions to index
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.c', '.cpp', '.h', '.hpp',
    '.cs', '.go', '.rs', '.rb', '.php', '.swift', '.kt', '.scala', '.sh',
    '.sql', '.html', '.css', '.json', '.yaml', '.yml', '.xml', '.md'
}

# The definition is captured in TOOL_DEFINITION (not accessed via TOOLS[0]) so the
# readme and manual paths keep working even when TOOLS is emptied to disable the tool
TOOL_DEFINITION = {
        "name": TOOL_NAME,
        "description": """Keyword search over code files (NOT semantic/embedding-based).
- Extracts keywords from your query and ranks files by keyword density
- Returns best-matching files with a snippet around the strongest line
- Honors .gitignore by default; for exact text/regex use file_grep; for file-name patterns use file_glob
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
                    "enum": ["readme", "search"],
                    "description": "Operation to perform"
                },
                "query": {
                    "type": "string",
                    "description": "Words or a question; reduced to keywords (3+ letter words, common stopwords dropped) for matching"
                },
                "target_directories": {
                    "type": "array",
                    "description": "Non-empty list of directory paths to search (required for search; absolute paths recommended)"
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results (positive integer; default 10, max 15)"
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": "Honor .gitignore files found inside the searched directories (default true); set false to also search ignored files"
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
# Code Keyword Search Tool

Keyword search over code files. This is NOT semantic/embedding-based search: the query
is reduced to keywords (words of 3+ letters, common stopwords dropped) and files are
ranked by keyword density (matches per 1000 characters, so large files do not win just
by being large). Each result shows a snippet around the best-matching line.

## Token: """ + TOOL_UNLOCK_TOKEN + """

## Operations

### search
Keyword search over code files.

Parameters:
- query (required): Words or a question; reduced to keywords for matching
- target_directories (required): Non-empty list of directory paths to search.
  Absolute paths recommended; relative paths resolve against the server process
  working directory. The server working directory is never searched implicitly.
- num_results (optional): Positive integer (default 10, max 15)
- respect_gitignore (optional): Boolean, default true. When true, .gitignore files found
  inside the searched directories are honored (basic pattern support: wildcards, ** ,
  trailing-slash directory rules, leading-slash anchoring and ! negation). Set false to
  also search ignored files. .gitignore files outside the searched directories are not
  consulted.

Files larger than 2 MB are skipped. Directories named .git, .hg, .svn, node_modules,
__pycache__, .venv, venv and .tox are excluded, and directory symlinks are not followed.
Duplicate or nested target directories are searched only once.

## When to Use

Use this tool when you want to:
- Rank files in a directory tree by keyword frequency
- Locate likely files when you only know some relevant words

## When NOT to Use

Skip this tool for:
- Exact text or regex matches (use file_grep)
- Known files (use file_read)
- File name patterns (use file_glob)
- True meaning-based search (use the sqlite tool's vector/embedding search)

## Query Examples

Matching is keyword-based, so distinctive words matter more than phrasing:
- "user authentication password login"
- "Where is the database connection handled?" (keywords: database, connection, handled)

Bad queries:
- "the and for" - only stopwords, nothing to match

## Examples

```json
{
  "input": {
    "operation": "search",
    "query": "How does the error handling work?",
    "target_directories": ["C:/project/src"],
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

```json
{
  "input": {
    "operation": "search",
    "query": "Where are API routes defined?",
    "target_directories": ["C:/project/src", "C:/project/lib"],
    "num_results": 5,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Notes
- Results include file paths and relevant code snippets
- target_directories must be a non-empty list; an empty list is an error
- Refine search to specific directories based on initial results
"""
    }

TOOLS = [TOOL_DEFINITION]


def translate_gitignore_glob_to_regex(glob_pattern: str) -> str:
    """Translate one .gitignore glob (already stripped of '!', leading '/' and trailing '/')
    into a regex string over forward-slash relative paths: '**' spans directories,
    '*' and '?' never cross '/', and character classes pass through."""
    regex_fragments = []
    index = 0
    pattern_length = len(glob_pattern)
    while index < pattern_length:
        current_char = glob_pattern[index]
        if current_char == '*':
            if glob_pattern[index:index + 3] == '**/':
                regex_fragments.append('(?:[^/]+/)*')  # zero or more whole directory components
                index += 3
            elif glob_pattern[index:index + 2] == '**':
                regex_fragments.append('.*')
                index += 2
            else:
                regex_fragments.append('[^/]*')
                index += 1
        elif current_char == '?':
            regex_fragments.append('[^/]')
            index += 1
        elif current_char == '[':
            closing_bracket_index = glob_pattern.find(']', index + 1)
            if closing_bracket_index == -1:
                regex_fragments.append(re.escape(current_char))
                index += 1
            else:
                character_class_body = glob_pattern[index + 1:closing_bracket_index]
                if character_class_body.startswith('!'):
                    character_class_body = '^' + character_class_body[1:]
                regex_fragments.append('[' + character_class_body + ']')
                index = closing_bracket_index + 1
        else:
            regex_fragments.append(re.escape(current_char))
            index += 1
    return ''.join(regex_fragments)


def parse_gitignore_rules_for_directory(directory_path: str) -> List:
    """Parse the .gitignore file (if any) directly inside directory_path.

    Returns a list of (negated, dir_only, match_full_relative_path, compiled_regex)
    tuples in file order (git's last-match-wins order is preserved by the caller).
    Supported: comments, blank lines, '!' negation, trailing-slash directory-only
    rules, leading-slash anchoring, '*', '?', '**' and character classes.
    """
    gitignore_file_path = os.path.join(directory_path, '.gitignore')
    parsed_rules = []
    try:
        with open(gitignore_file_path, 'r', encoding='utf-8', errors='replace') as gitignore_file:
            gitignore_lines = gitignore_file.read().splitlines()
    except OSError:
        return parsed_rules
    for raw_line in gitignore_lines:
        line = raw_line.rstrip()
        if not line or line.startswith('#'):
            continue
        negated = line.startswith('!')
        if negated:
            line = line[1:]
        dir_only = line.endswith('/')
        if dir_only:
            line = line.rstrip('/')
        anchored = line.startswith('/')
        if anchored:
            line = line.lstrip('/')
        if not line:
            continue
        # Per git semantics a pattern containing any '/' matches the path relative to the
        # .gitignore's directory; a bare pattern matches the basename at any depth.
        match_full_relative_path = anchored or ('/' in line)
        try:
            compiled_regex = re.compile(translate_gitignore_glob_to_regex(line) + r'\Z')
        except re.error:
            continue  # skip unparseable patterns rather than failing the whole search
        parsed_rules.append((negated, dir_only, match_full_relative_path, compiled_regex))
    return parsed_rules


def is_path_ignored_by_gitignore_rules(candidate_path: str, candidate_is_directory: bool,
                                       ordered_rule_sets: List) -> bool:
    """Apply accumulated .gitignore rule sets (outermost directory first) to one path.

    ordered_rule_sets is a list of (rule_base_directory, parsed_rules) tuples; the LAST
    matching rule across all sets decides, mirroring git's last-match-wins semantics.
    """
    ignored = False
    candidate_basename = os.path.basename(candidate_path)
    for rule_base_directory, parsed_rules in ordered_rule_sets:
        try:
            relative_path = os.path.relpath(candidate_path, rule_base_directory).replace(os.sep, '/')
        except ValueError:
            continue  # e.g. different drive on Windows; these rules cannot apply
        for negated, dir_only, match_full_relative_path, compiled_regex in parsed_rules:
            if dir_only and not candidate_is_directory:
                continue
            match_subject = relative_path if match_full_relative_path else candidate_basename
            if compiled_regex.match(match_subject):
                ignored = not negated
    return ignored


def get_code_files(directories: List[str], respect_gitignore: bool = True) -> List[str]:
    """Get all code files in the specified directories.
    
    Args:
        directories: List of directory paths to search
        respect_gitignore: When True, honor .gitignore files found inside each directory
        
    Returns:
        List of file paths
    """
    files = []
    
    for directory in directories:
        dir_path = Path(directory)
        if not dir_path.exists():
            continue
        
        # Cumulative .gitignore rule sets per walked directory (outermost first). os.walk
        # is top-down, so a parent's entry is always present before its children are walked.
        gitignore_rule_sets_by_directory: Dict[str, List] = {}
        
        # Single walk per directory (not once per extension); followlinks=False avoids
        # directory-symlink cycles, and EXCLUDED_DIRECTORY_NAMES prunes .git/node_modules/venv etc.
        for walk_root, walk_dirnames, walk_filenames in os.walk(str(dir_path), followlinks=False):
            active_rule_sets = []
            if respect_gitignore:
                inherited_rule_sets = gitignore_rule_sets_by_directory.get(os.path.dirname(walk_root), [])
                own_parsed_rules = parse_gitignore_rules_for_directory(walk_root)
                if own_parsed_rules:
                    active_rule_sets = inherited_rule_sets + [(walk_root, own_parsed_rules)]
                else:
                    active_rule_sets = inherited_rule_sets
                gitignore_rule_sets_by_directory[walk_root] = active_rule_sets
            walk_dirnames[:] = [
                d for d in walk_dirnames
                if d not in EXCLUDED_DIRECTORY_NAMES
                and not (active_rule_sets
                         and is_path_ignored_by_gitignore_rules(os.path.join(walk_root, d), True, active_rule_sets))
            ]
            for walk_filename in walk_filenames:
                if os.path.splitext(walk_filename)[1].lower() in CODE_EXTENSIONS:
                    candidate_file_path = os.path.join(walk_root, walk_filename)
                    if active_rule_sets and is_path_ignored_by_gitignore_rules(candidate_file_path, False, active_rule_sets):
                        continue
                    files.append(candidate_file_path)
    
    return files


def simple_text_search(query: str, directories: List[str], num_results: int,
                       respect_gitignore: bool = True) -> List[Dict]:
    """Perform simple keyword-based search as fallback.
    
    This is a basic implementation that searches for query keywords in files.
    A full implementation would use embeddings for true semantic search.
    
    Args:
        query: Search query
        directories: Directories to search
        num_results: Maximum results
        respect_gitignore: When True, honor .gitignore files found inside the directories
        
    Returns:
        List of matching results
    """
    # Extract keywords from query
    keywords = re.findall(r'\b\w{3,}\b', query.lower())
    # Remove common words
    stopwords = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'her', 'was', 
                 'one', 'our', 'out', 'how', 'does', 'what', 'when', 'where', 'which', 'who',
                 'this', 'that', 'with', 'have', 'from', 'they', 'been', 'call', 'will'}
    keywords = [k for k in keywords if k not in stopwords]
    
    if not keywords:
        return []
    
    MCPLogger.log(TOOL_LOG_NAME, f"Searching for keywords: {keywords}")
    
    files = get_code_files(directories, respect_gitignore)
    results = []
    
    for file_path in files:
        try:
            # Byte-size cap: never slurp very large files whole
            if os.path.getsize(file_path) > MAX_SEARCHABLE_FILE_SIZE_BYTES:
                continue
            
            # Read the file once; the lowercased copy for scoring is derived from the same read
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                original_content = f.read()
            content = original_content.lower()
            
            # Count keyword matches
            total_keyword_match_count = sum(content.count(kw) for kw in keywords)
            
            if total_keyword_match_count > 0:
                # Find most relevant line
                lines = content.split('\n')
                best_line_idx = 0
                best_line_score = 0
                
                for i, line in enumerate(lines):
                    line_score = sum(line.count(kw) for kw in keywords)
                    if line_score > best_line_score:
                        best_line_score = line_score
                        best_line_idx = i
                
                # Get context around best line (from the single read above, not a second file read)
                start_line = max(0, best_line_idx - 3)
                end_line = min(len(lines), best_line_idx + 7)
                
                original_lines = original_content.splitlines(keepends=True)
                snippet = ''.join(original_lines[start_line:end_line])
                
                # Rank by keyword density (matches per 1000 characters) rather than raw
                # count, so large files do not dominate results simply by being large
                keyword_density_score = round(total_keyword_match_count * 1000.0 / max(1, len(content)), 2)
                
                results.append({
                    'file': file_path,
                    'score': keyword_density_score,
                    'match_count': total_keyword_match_count,
                    'start_line': start_line + 1,
                    'end_line': end_line,
                    'snippet': snippet
                })
        except Exception as e:
            # Log skipped files (permissions, transient locks) so empty results are diagnosable
            MCPLogger.log(TOOL_LOG_NAME, f"Skipping unreadable file {file_path}: {e}")
            continue
    
    # Sort by score and limit results
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:num_results]


def handle_search(params: Dict) -> Dict:
    """Handle the search operation."""
    try:
        query = params.get("query")
        target_directories = params.get("target_directories", [])
        
        # num_results must be a positive int; bool is excluded because bool subclasses int
        num_results = params.get("num_results", 10)
        if isinstance(num_results, bool) or not isinstance(num_results, int) or num_results < 1:
            return {"content": [{"type": "text", "text": "num_results must be a positive integer"}], "isError": True}
        num_results = min(num_results, MAX_RESULTS)
        
        respect_gitignore = params.get("respect_gitignore", True)
        if not isinstance(respect_gitignore, bool):
            return {"content": [{"type": "text", "text": "respect_gitignore must be a boolean"}], "isError": True}
        
        if not query:
            return {"content": [{"type": "text", "text": "query is required"}], "isError": True}
        
        MCPLogger.log(TOOL_LOG_NAME, f"Keyword search: {query}")
        
        # Explicit directories are required; never silently search the server process CWD
        if not target_directories or not isinstance(target_directories, list):
            return {"content": [{"type": "text", "text": "target_directories is required and must be a non-empty list of directory paths (the server working directory is never searched implicitly)"}], "isError": True}
        
        # Resolve relative paths
        resolved_dirs = []
        for d in target_directories:
            path = Path(d)
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.exists() and path.is_dir():
                resolved_dirs.append(str(path))
        
        if not resolved_dirs:
            return {"content": [{"type": "text", "text": "No valid directories to search"}], "isError": True}
        
        # Drop duplicate and nested target directories so overlapping trees are walked and
        # scored only once (sorting by path length guarantees parents are kept before children)
        deduplicated_directories: List[str] = []
        kept_normalized_directory_paths: List[str] = []
        for candidate_directory in sorted(resolved_dirs, key=lambda directory: len(os.path.normcase(os.path.normpath(directory)))):
            candidate_normalized_path = os.path.normcase(os.path.normpath(candidate_directory))
            if any(candidate_normalized_path == kept_path or candidate_normalized_path.startswith(kept_path + os.sep)
                   for kept_path in kept_normalized_directory_paths):
                continue
            kept_normalized_directory_paths.append(candidate_normalized_path)
            deduplicated_directories.append(candidate_directory)
        
        # Perform keyword search (this tool does not do embedding-based matching)
        results = simple_text_search(query, deduplicated_directories, num_results, respect_gitignore)
        
        if not results:
            return {
                "content": [{"type": "text", "text": f"No results found for: {query}\n\nTry:\n- Using different keywords\n- Searching in different directories\n- Using file_grep for exact text matches"}],
                "isError": False
            }
        
        # Format results (header says keyword search - this tool is not embedding-based)
        output_lines = [f"## Keyword Search Results for: \"{query}\"", ""]
        
        for i, result in enumerate(results, 1):
            rel_path = result['file']
            try:
                rel_path = os.path.relpath(result['file'])
            except ValueError:
                pass
            
            output_lines.append(f"### {i}. {rel_path}")
            output_lines.append(f"**Lines {result['start_line']}-{result['end_line']}** (keyword density score: {result['score']}, keyword matches: {result['match_count']})")
            output_lines.append("```")
            output_lines.append(result['snippet'].rstrip())
            output_lines.append("```")
            output_lines.append("")
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(results)} results")
        
        return {
            "content": [{"type": "text", "text": "\n".join(output_lines)}],
            "isError": False
        }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Search error: {str(e)}")
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


def handle_semantic_search(input_param: Dict) -> Dict:
    """Handle semantic search tool operations via MCP interface."""
    try:
        # Read synthetic handler_info (added by the server for dynamic routing) via .get on a
        # shallow copy, so the caller's original dict is never mutated; drop it before dispatch.
        input_param = dict(input_param)
        handler_info = input_param.get('handler_info', None)
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
        
        # The readme operation already returned above, so every path here requires the token
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)
        
        operation = input_param.get("operation")
        
        if operation == "search":
            return handle_search(input_param)
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
# module and delegates fs operation "code_search" to handle_semantic_search
# above.  No standalone tool is registered anymore (the IDE-duplicate disable
# switch now lives on fs) - empty TOOLS/HANDLERS make the tool loader register
# nothing.
TOOLS = []
HANDLERS = {}
