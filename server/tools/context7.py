"""
File: ragtag/tools/context7.py
Project: Aura Friday MCP-Link Server
Component: Context7 Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for retrieving up-to-date documentation for any library from Context7.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ıⲔƧꙄꙄуȜυYƤpLᖴƳıϹīʈΟᏮᏟwƖP𝟦ⅮȢ𝟨ꓪᗅfƎcls𝟨ᗪрҮ3ЅЈ4νPⅮϜ𝐴yνՕᗷ𝟟WυɯԝрƍНѡбМЕQꓔꓔ𝘈QВΜHʌƘҮ5ɡꓚνOΕеųĸΥďCϜ6ΤvvƙŪYFΡⲘⅠBꓧ𝟥EıϜꓦIᎬ𝟦"
"signdate": "2025-09-17T11:18:50.826Z",

test: python3 /home/cnd/Downloads/cursor/ragtag/python/ragtag/src/ragtag/ragtag_cli.py context7 --json '{ "input": { "operation": "resolve-library-id", "library_name": "autodesk", "tool_unlock_token": "f4c59009" } }'
note: above will give you a new tool_unlock_token which you will need to re-run that above test command with

"""

import json
import requests
from typing import Dict, Optional, Any
from easy_mcp.server import MCPLogger, get_tool_token

# Constants
TOOL_LOG_NAME = "CONTEXT7"
CONTEXT7_API_BASE_URL = "https://context7.com/api"
DEFAULT_TYPE = "txt"
DEFAULT_MINIMUM_TOKENS = 10000

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "context7",
        "description": """Retrieves up-to-date documentation and code examples for any library.
- Use this when you need current documentation for libraries and frameworks
""",
        # Standard MCP parameters - simplified to single input dict
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
        # Actual tool parameters - revealed only after readme call
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["resolve-library-id", "get-library-docs", "readme"],
                    "description": "Operation to perform"
                },
                "library_name": {
                    "type": "string",
                    "description": "Library name to search for (used with resolve-library-id operation)"
                },
                "context7_compatible_library_id": {
                    "type": "string",
                    "description": "Exact Context7-compatible library ID retrieved from 'resolve-library-id' (used with get-library-docs operation)"
                },
                "topic": {
                    "type": "string",
                    "description": "Topic to focus documentation on (optional, used with get-library-docs operation)"
                },
                "tokens": {
                    "type": "integer",
                    "description": "Maximum number of tokens of documentation to retrieve (default: 10000)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call
        "readme": """
Context7 MCP - Up-to-date Code Docs For Any Prompt

This tool retrieves up-to-date documentation and code examples for any library directly from Context7.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {
     "input": {"readme": true}
   }

2. For resolve-library-id operation:
   {
     "input": {
       "operation": "resolve-library-id", 
       "library_name": "library to search for",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

3. For get-library-docs operation:
   {
     "input": {
       "operation": "get-library-docs", 
       "context7_compatible_library_id": "library ID from resolve-library-id",
       "topic": "optional topic focus",
       "tokens": 10000,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Usage Notes
1. You MUST call resolve-library-id first to get a valid Context7-compatible library ID
2. Include the tool_unlock_token in all operations
3. For best results, select libraries based on name match, popularity (stars), snippet coverage, and relevance to use case
4. The tokens parameter determines how much documentation is retrieved (minimum: 10000)

## Examples
```json
# First, resolve the library ID
{
  "input": {
    "operation": "resolve-library-id", 
    "library_name": "react",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

# Then, get the documentation using the returned library ID
{
  "input": {
    "operation": "get-library-docs", 
    "context7_compatible_library_id": "facebook/react",
    "topic": "hooks",
    "tokens": 15000,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
"""
    }
]

# TOOLS=[] # temp disable 

def format_search_result(result: Dict[str, Any]) -> str:
    """Format a search result into a string representation"""
    return f"""- Title: {result.get('title', 'No title')}
- Context7-compatible library ID: {result.get('id', 'No ID')}
- Description: {result.get('description', 'No description')}
- Code Snippets: {result.get('totalSnippets', 0)}
- GitHub Stars: {result.get('stars', 0)}"""

def format_search_results(search_response: Dict[str, Any]) -> str:
    """Format search results into a string representation"""
    results = search_response.get('results', [])
    if not results:
        return "No documentation libraries found matching your query."
    
    formatted_results = [format_search_result(result) for result in results]
    return "\n---\n".join(formatted_results)

def search_libraries(query: str) -> Optional[Dict[str, Any]]:
    """Searches for libraries matching the given query"""
    try:
        url = f"{CONTEXT7_API_BASE_URL}/v1/search"
        params = {"query": query}
        response = requests.get(url, params=params)
        
        if not response.ok:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to search libraries: {response.status_code}")
            return None
        
        return response.json()
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error searching libraries: {str(e)}")
        return None

def fetch_library_documentation(library_id: str, options: Dict[str, Any]) -> Optional[str]:
    """Fetches documentation context for a specific library"""
    try:
        if library_id.startswith("/"):
            library_id = library_id[1:]
        
        # Extract folders parameter if present in the ID
        folders = ""
        if "?folders=" in library_id:
            library_id, folders = library_id.split("?folders=")
            options["folders"] = folders
        
        url = f"{CONTEXT7_API_BASE_URL}/v1/{library_id}"
        params = {"type": DEFAULT_TYPE}
        
        # Add optional parameters
        if "tokens" in options:
            params["tokens"] = str(options["tokens"])
        if "topic" in options and options["topic"]:
            params["topic"] = options["topic"]
        if "folders" in options and options["folders"]:
            params["folders"] = options["folders"]
        
        headers = {"X-Context7-Source": "mcp-server"}
        response = requests.get(url, params=params, headers=headers)
        
        if not response.ok:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to fetch documentation: {response.status_code}")
            return None
        
        text = response.text
        if not text or text == "No content available" or text == "No context data available":
            return None
        
        return text
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error fetching library documentation: {str(e)}")
        return None

def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    try:
        if not with_readme:
            return ''
            
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        return "\n\n" + json.dumps({
            "description": TOOLS[0]["readme"],
            "parameters": TOOLS[0]["real_parameters"]
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

def handle_resolve_library_id(params: Dict) -> Dict:
    """Handle resolve-library-id operation."""
    try:
        # Extract library_name parameter
        library_name = params.get("library_name")
        if not library_name:
            return create_error_response("No library name provided", with_readme=True)
        
        # Log the request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing resolve-library-id request: {library_name}")
        
        # Search for libraries matching the query
        search_response = search_libraries(library_name)
        
        if not search_response or "results" not in search_response:
            return create_error_response("Failed to retrieve library documentation data from Context7", with_readme=False)
        
        if not search_response.get("results"):
            return create_error_response("No documentation libraries available", with_readme=False)
        
        # Format the search results
        results_text = format_search_results(search_response)
        
        return {
            "content": [{
                "type": "text",
                "text": f"""Available Libraries (top matches):

Each result includes:
- Library ID: Context7-compatible identifier (format: /org/repo)
- Name: Library or package name
- Description: Short summary
- Code Snippets: Number of available code examples
- GitHub Stars: Popularity indicator

For best results, select libraries based on name match, popularity (stars), snippet coverage, and relevance to your use case.

---

{results_text}"""
            }],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error processing resolve-library-id request: {str(e)}", with_readme=False)

def handle_get_library_docs(params: Dict) -> Dict:
    """Handle get-library-docs operation."""
    try:
        # Extract parameters
        library_id = params.get("context7_compatible_library_id")
        if not library_id:
            return create_error_response("No library ID provided", with_readme=True)
        
        topic = params.get("topic", "")
        tokens = params.get("tokens", DEFAULT_MINIMUM_TOKENS)
        
        # Ensure minimum token count
        if tokens < DEFAULT_MINIMUM_TOKENS:
            tokens = DEFAULT_MINIMUM_TOKENS
        
        # Log the request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing get-library-docs request: {library_id}, topic: {topic}, tokens: {tokens}")
        
        # Fetch library documentation
        documentation_text = fetch_library_documentation(library_id, {
            "tokens": tokens,
            "topic": topic
        })
        
        if not documentation_text:
            return create_error_response("Documentation not found or not finalized for this library. This might have happened because you used an invalid Context7-compatible library ID. To get a valid Context7-compatible library ID, use the 'resolve-library-id' operation with the package name you wish to retrieve documentation for.", with_readme=False)
        
        return {
            "content": [{
                "type": "text",
                "text": documentation_text
            }],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error processing get-library-docs request: {str(e)}", with_readme=False)

def handle_context7(input_param: Dict) -> Dict:
    """Handle context7 tool operations via MCP interface."""
    try:
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {} # Pop off synthetic handler_info parameter early (before validation); This is added by the server for tools that need dynamic routing

        if isinstance(input_param, dict) and "input" in input_param: # collapse the single-input placeholder which exists only to save context (because we must bypass pipeline parameter validation to *save* the context)
            input_param = input_param["input"]

        # Handle readme request - explicitly check for readme before token validation
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            MCPLogger.log(TOOL_LOG_NAME, "Handling readme request")
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
            
        # For non-readme operations, validate input structure and token
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input structure", with_readme=True)
            
        # Extract operation parameters
        operation = input_param.get("operation")
        
        # Token validation for regular operations
        if input_param.get("tool_unlock_token") != TOOL_UNLOCK_TOKEN:
            return create_error_response(
                "Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:",
                with_readme=True
            )
            
        if operation == "resolve-library-id":
            return handle_resolve_library_id(input_param)
        elif operation == "get-library-docs":
            return handle_get_library_docs(input_param)
        else:
            return create_error_response(f"Unknown operation: {operation}", with_readme=True)
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in context7 operation: {str(e)}")
        return create_error_response(f"Error in context7 operation: {str(e)}", with_readme=False)

# Map of tool names to their handlers
HANDLERS = {
    "context7": handle_context7
}
