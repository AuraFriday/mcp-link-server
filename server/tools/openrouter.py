"""
File: ragtag/tools/openrouter.py
Project: Aura Friday MCP-Link Server
Component: OpenRouter API Interface
Author: Christopher Nathan Drake (cnd)

Tool implementation for interacting with OpenRouter's API, providing access to multiple AI models.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "rkAꓐīƽRƛսHƵКƻ𐐕ᗷɌƧᎻ𝟦ꓳƖΕƤЅJmgjmOƍᴍԛPꓗJ2ÐⲟΥD6ßIСfEQВВP4ƤƬꓦ1Dꓬ𝟢ƦᎠеυVßոßꙄⴹο5ᏟFꙄɋƦȷƳ𝟫Ѕ0ɯyμƱraⅼꓔƿeᏂΑNÐ𐓒ᖴТ𝟣ѵȣyIþ𝟟бꓬƋꓗ",
"signdate": "2025-10-30T02:25:46.861Z",
"""

import os
import json
import http.client
import re
import urllib.parse
import sys  # Add sys for exit()
from typing import Dict, List, Union, Optional, Tuple, Any, Set
from datetime import datetime, timezone
import requests
from .sqlite import sqlite
from ragtag.shared_config import get_user_data_directory, get_config_manager
from easy_mcp.server import MCPLogger, get_tool_token
# We use MCPLogger for logging here. do NOT "import logging" or use any log.* calls.

DISABLE_SECURITY = True

# Constants
TOOL_LOG_NAME = "OPENROUTER"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

def get_openrouter_db_path() -> str:
    """Get the path to the OpenRouter database in the user data directory.
    
    Returns:
        str: Path to openrouter.db in the user data directory
    """
    user_data_path = get_user_data_directory()
    return str(user_data_path / "openrouter.db")

# Tool definitions
TOOLS = [
    {
        "name": "openrouter",
        "description": """OpenRouter API tool providing access to multiple different AI models from multiple providers.
- Use this tool when asked for or about openrouter, or when you need help from an AI with different abilities to your own.
""",

        # Detailed documentation - obtained via "input":"readme" initial call
        "readme": """OpenRouter API integration providing access to multiple AI models.
        
Key Features:
1. Model Discovery & Search:
   - Semantic search to find best models for specific tasks
   - Vector similarity comparison of model capabilities
   - Auto-refreshing model database (24h cache)
   - Rich filtering options

2. Model Information:
   - Context lengths and capabilities
   - Pricing details
   - Architecture specifications
   - Provider-specific limits

3. Chat Completions:
   - Support for multiple models
   - Streaming responses
   - Tool usage capabilities
   - Source content processing (URL/file)

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {
     "input": {"operation": "readme"}
   }

2. For all other operations:
   {
     "input": {
       "operation": "operation_name", 
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       ...other parameters...
     }
   }

Primary Operations:

1. search_models: Vector similarity search for finding task-specific models
   Required parameters:
   - operation: "search_models"
   - tool_unlock_token: (see above)
   Optional parameters:
   - bindings: {"query_vec": {"_embedding_text": "your search text"}} (required only for semantic search)
   - sql: Custom SQL query (if omitted, returns all columns)
   - max_results: Maximum results to return (default 32)

   Example - Semantic Search:
   {
     "input": {
       "operation": "search_models",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "bindings": {
         "query_vec": {"_embedding_text": "code analysis and reasoning"}
       }
     }
   }

   Example - Non-Semantic Search (Top 5 by Context Length):
   {
     "input": {
       "operation": "search_models",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "sql": "SELECT id, context_length, description FROM models ORDER BY context_length DESC LIMIT 5"
     }
   }

   Example - Custom SQL:
   {
     "input": {
       "operation": "search_models",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "sql": "SELECT id, context_length, description, vec_distance_cosine(embedding, vec_f32(:query_vec)) as similarity FROM models WHERE context_length > 32000 ORDER BY similarity LIMIT 5",
       "bindings": {"query_vec": {"_embedding_text": "code analysis"}}
     }
   }

2. list_available_models: Basic listing of all models with filtering, but NO SORTING. Always pulls data from the API. Always refreshes the DB used by search_models if the API has new/changed/removed models.
   Required parameters:
   - operation: "list_available_models"
   - tool_unlock_token: (see above)
   Optional parameters:
   - max_results: Limit number of results
   - json: Return full JSON instead of TSV (default false)
   - columns: Specific columns to include
   - search_criteria: {
       "modality": "text->text",
       "min_context_length": 32000,
       "max_prompt_price": 0.0001,
       "max_completion_price": 0.0001,
       "provider": "anthropic",
       "text_match": "regex pattern",
       "case_sensitive": false
     }

   Example:
   {
     "input": {
       "operation": "list_available_models",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "max_results": 10,
       "search_criteria": {
         "provider": "anthropic",
         "min_context_length": 100000
       }
     }
   }

3. chat_completion: Send chat completion requests
   Required parameters:
   - operation: "chat_completion"
   - tool_unlock_token: (see above)
   - model: Model identifier
   - messages: Array of message objects
   Optional parameters:
   - stream: Enable streaming (default false)
   - tools: Array of available tools
   - tool_choice: Tool selection mode ("none"/"auto"/"any")""" +

   # FUTURE: Additional parameters will be supported:
   # - temperature: Control randomness (0.0-2.0)
   # - max_tokens: Maximum tokens to generate
   # - top_p: Nucleus sampling parameter
   # - frequency_penalty: Reduce repetition (-2.0 to 2.0)
   # - presence_penalty: Encourage new topics (-2.0 to 2.0)
   """
   
   Example - Basic Chat:
   {
     "input": {
       "operation": "chat_completion",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "model": "anthropic/claude-3-opus",
       "messages": [
         {"role": "user", "content": "Analyze this code"}
       ]
     }
   }

   Example - With Source Content:
   IMPORTANT: When using source-based messages (file/URL), you MUST precede them with an instruction 
   message telling the model what to do with the content. The instruction and source should be 
   separate messages in the array:
   {
     "input": {
       "operation": "chat_completion",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "model": "anthropic/claude-3-opus",
       "messages": [
         {
           "role": "user",
           "content": "Please analyze this code and explain its main functionality"
         },
         {
           "role": "user",
           "content": "https://example.com/code.py",
           "source": "url"
         }
       ]
     }
   }

   Example - With File Source:
   {
     "input": {
       "operation": "chat_completion",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "model": "anthropic/claude-3-opus",
       "messages": [
         {
           "role": "user",
           "content": "Please provide a detailed summary of this chat history"
         },
         {
           "role": "user",
           "content": ".specstory/history/latest_chat.md",
           "source": "file"
         }
       ]
     }
   }

4. get_credits: Check account balance
   Required parameters:
   - operation: "get_credits"
   - tool_unlock_token: (see above)

   Example:
   {
     "input": {
       "operation": "get_credits",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

5. get_generation: Retrieve generation metadata
   Required parameters:
   - operation: "get_generation"
   - tool_unlock_token: (see above)
   - generation_id: ID of the generation to retrieve

   Example:
   {
     "input": {
       "operation": "get_generation",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
       "generation_id": "gen_abc123"
     }
   }

Database Schema (openrouter.db):
- id: TEXT PRIMARY KEY (e.g., 'anthropic/claude-3-opus')
- embedding: BLOB (1024-dim vector of the description for semantic search)
- last_updated: DATETIME (auto-refreshes if >24h old)
- description: TEXT (model capabilities and details)
- many other columns (pricing, types, sizes, etc): this table is auto-generated from the OpenRouter list_available_models result set. search_models by default returns all columns.

IMPORTANT - For Iterative Operations:
When processing multiple models or performing operations that require iterating over the results 
(e.g., testing multiple models, analyzing capabilities, or batch processing), consider using the 
mcp task_manager tool's long_task operation. This ensures:
- Progress is tracked and persisted
- Operations can be resumed after interruptions
- Context is maintained between iterations
- Clear step-by-step processing instructions are preserved

Notes:
- Database auto-refreshes when needed (24h cache)
- Use search_models for semantic search, list_available_models for basic filtering
- Vector similarity uses cosine distance (0-1, lower = more similar)
- Forbidden SQL: DELETE, UPDATE, DROP, ALTER, CREATE
- Tool calls supported in chat completions
- Source content can be loaded from URLs, files, or other mcp tool outputs.
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
                    "enum": ["list_available_models", "get_credits", "get_generation", "chat_completion", "search_models", "readme"],
                    "description": "Operation to perform"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                },
                "generation_id": {
                    "type": "string",
                    "description": "ID of the generation to retrieve (required for get_generation operation)"
                },
                "json": {
                    "type": "boolean",
                    "description": "Whether to return full JSON response instead of tab-separated values",
                    "default": False
                },
                "columns": {
                    "type": "array",
                    "description": "List of columns to include in TSV output. Use dot notation for nested fields (e.g., 'architecture.modality'). If not specified, default columns will be used.",
                    "items": {
                        "type": "string"
                    }
                },
                "search_criteria": {
                    "type": "object",
                    "description": "Optional filtering criteria for models",
                    "properties": {
                        "modality": {
                            "type": "string",
                            "description": "Filter by input/output types (e.g., 'text->text', 'text+image->text')"
                        },
                        "min_context_length": {
                            "type": "integer",
                            "description": "Minimum context window size"
                        },
                        "max_prompt_price": {
                            "type": "number",
                            "description": "Maximum price per prompt token"
                        },
                        "max_completion_price": {
                            "type": "number",
                            "description": "Maximum price per completion token"
                        },
                        "provider": {
                            "type": "string",
                            "description": "Filter by specific provider (e.g., 'anthropic', 'openai')"
                        },
                        "text_match": {
                            "type": "string",
                            "description": "Regex pattern to search in model ID and description"
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "Whether text search should be case sensitive",
                            "default": False
                        }
                    }
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of models to return (optional, but STRONGLY RECOMMENDED to use <= 50 otherwise AI context will be overwhelmed). If not specified: list_available_models returns 32 models, search_models returns 32.",
                    "default": None,
                    "minimum": 1
                },
                "model": {
                    "type": "string",
                    "description": "Model identifier (e.g., 'anthropic/claude-3-opus')"
                },
                "messages": {
                    "type": "array",
                    "description": "Array of message objects with role and content",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": ["user", "assistant", "system"],
                                "description": "Role of the message sender"
                            },
                            "content": {
                                "type": "string",
                                "description": "Message content or source-specific content"
                            },
                            "source": {
                                "type": "string",
                                "description": "Optional source type for dynamic content ('url' or 'file')"
                            }
                        },
                        "required": ["role", "content"]
                    }
                },
                "stream": {
                    "type": "boolean",
                    "description": "Whether to stream the response",
                    "default": False
                },
                "tools": {
                    "type": "array",
                    "description": "Array of tools that the model can use",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Name of the tool"
                            },
                            "description": {
                                "type": "string",
                                "description": "Description of what the tool does"
                            },
                            "input_schema": {
                                "type": "object",
                                "description": "JSON Schema defining the tool's input parameters"
                            }
                        },
                        "required": ["name", "description", "input_schema"]
                    }
                },
                "tool_choice": {
                    "type": "string",
                    "description": "Control the model's tool use behavior",
                    "enum": ["none", "auto", "any"],
                    "default": "auto"
                },
                "sql": {
                    "type": "string",
                    "description": "SQL query to execute against openrouter.db models table"
                },
                "bindings": {
                    "type": "object",
                    "description": "Query parameters including :query_vec for semantic search",
                    "example": {"query_vec": {"_embedding_text": "code analysis and explanation"}}
                }, # future:-
                "temperature": {
                    "type": "number",
                    "description": "Control randomness in responses (0.0-2.0)",
                    "minimum": 0.0,
                    "maximum": 2.0
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum number of tokens to generate",
                    "minimum": 1
                },
                "top_p": {
                    "type": "number",
                    "description": "Nucleus sampling parameter (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "frequency_penalty": {
                    "type": "number",
                    "description": "Reduce repetition of tokens (-2.0 to 2.0)",
                    "minimum": -2.0,
                    "maximum": 2.0
                },
                "presence_penalty": {
                    "type": "number",
                    "description": "Encourage new topics (-2.0 to 2.0)",
                    "minimum": -2.0,
                    "maximum": 2.0
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "title": "openrouterArguments",
            "type": "object"
        }

    }
]

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}



def test_flattened_value(key: str, value: Any) -> Tuple[bool, Optional[str]]:
    """Test if a flattened value is safe for SQL insertion.
    
    Args:
        key: The flattened key name
        value: The value to test
        
    Returns:
        (is_valid, error_message) tuple
    """
    try:
        # Test 1: Basic type check
        if not isinstance(value, (str, int, float, bool, type(None))):
            return False, f"Invalid type for SQL: {type(value)}"
            
        # Test 2: For strings, test JSON serialization
        if isinstance(value, str):
            try:
                # If it's already JSON, validate it parses
                json.loads(value)
            except json.JSONDecodeError:
                # Not JSON, which is fine - it's just a string
                pass
                
        # Test 3: Check for reasonable length
        if isinstance(value, str) and len(value) > 10000:
            return False, f"String too long: {len(value)} chars"
            
        # Test 4: Key name validation
        if not key.replace("_", "").isalnum():
            return False, f"Invalid characters in key: {key}"
            
        if len(key) > 63:  # Common SQL identifier length limit
            return False, f"Key too long: {len(key)} chars"
            
        return True, None
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"

def test_flattened_output(flattened_data: Dict) -> Tuple[bool, List[str]]:
    """Test if flattened dictionary is safe for SQL insertion.
    
    Args:
        flattened_data: Dictionary to test
        
    Returns:
        Never returns if validation fails - exits with error status
        Returns (True, []) if validation passes
    """
    errors = []
    
    #MCPLogger.log(TOOL_LOG_NAME, f"Testing flattened output with {len(flattened_data)} keys")
    
    for key, value in flattened_data.items():
        is_valid, error = test_flattened_value(key, value)
        if not is_valid:
            error_msg = f"Key '{key}' failed validation: {error}"
            MCPLogger.log(TOOL_LOG_NAME, "VALIDATION ERROR: " + error_msg)
            MCPLogger.log(TOOL_LOG_NAME, f"Value type: {type(value)}")
            MCPLogger.log(TOOL_LOG_NAME, f"Value preview: {str(value)[:100]}")
            MCPLogger.log(TOOL_LOG_NAME, "=== FATAL ERROR: Validation failed, see above for details ===")
            sys.exit(1)  # Exit immediately on first validation failure
            
    return True, []

def try_numeric_conversion(value: str) -> Union[int, float, str]:
    """Try to convert a string to a numeric value if appropriate.
    
    Args:
        value: String value to convert
        
    Returns:
        Converted numeric value or original string
    """
    if not isinstance(value, str):
        return value
        
    # Try integer first
    try:
        if value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
            return int(value)
    except (ValueError, TypeError):
        pass
        
    # Try float next
    try:
        if '.' in value:
            return float(value)
    except (ValueError, TypeError):
        pass
        
    return value

def flatten_dict(d: Dict[str, Any], prefix: str = '') -> Dict[str, Any]:
    """Flatten a nested dictionary, converting types appropriately for SQLite.
    
    Args:
        d: Dictionary to flatten
        prefix: Prefix for nested keys
        
    Returns:
        Flattened dictionary with converted values
    """
    result = {}
    
    #MCPLogger.log(TOOL_LOG_NAME, f"Starting flatten for prefix: {prefix}")
    #MCPLogger.log(TOOL_LOG_NAME, f"Input data structure type: {type(d)}")
    #MCPLogger.log(TOOL_LOG_NAME, f"Keys at this level: {list(d.keys())}")
    
    for key, value in d.items():
        new_key = f"{prefix}{key}" if prefix else key
        #MCPLogger.log(TOOL_LOG_NAME, f"Processing key: {key} -> {new_key}")
        #MCPLogger.log(TOOL_LOG_NAME, f"Value type: {type(value)}")
        #MCPLogger.log(TOOL_LOG_NAME, f"Value preview: {str(value)[:100]}")
        
        if isinstance(value, dict):
            #MCPLogger.log(TOOL_LOG_NAME, f"Handling nested dict for key: {key}")
            nested_prefix = f"{new_key}_"
            nested_result = flatten_dict(value, nested_prefix)
            #MCPLogger.log(TOOL_LOG_NAME, f"Testing flattened output with {len(nested_result)} keys")
            test_flattened_output(nested_result)
            #MCPLogger.log(TOOL_LOG_NAME, f"Successfully flattened nested dict with {len(nested_result)} keys")
            result.update(nested_result)
            
        elif isinstance(value, (list, tuple)):
            #MCPLogger.log(TOOL_LOG_NAME, f"Serializing sequence for key: {new_key}")
            # Convert lists to JSON strings
            json_str = json.dumps(value)
            #MCPLogger.log(TOOL_LOG_NAME, f"After list serialization: {json_str}")
            result[new_key] = json_str
            
        else:
            #MCPLogger.log(TOOL_LOG_NAME, f"Direct assignment for key: {new_key}")
            if value is None:
                # Convert None to SQL NULL by not including the key in bindings
                continue
            elif isinstance(value, str):
                # Try numeric conversion for strings
                converted = try_numeric_conversion(value)
                #MCPLogger.log(TOOL_LOG_NAME, f"After numeric conversion: {type(converted)} = {converted}")
                result[new_key] = converted
            else:
                result[new_key] = value
                
    #MCPLogger.log(TOOL_LOG_NAME, f"Testing flattened output with {len(result)} keys")
    test_flattened_output(result)
    return result

def refresh_models_database(models: Optional[List[Dict]] = None) -> Union[Tuple[bool, None], Tuple[bool, str]]:
    """Refresh the models database with current data from OpenRouter API.
    
    Args:
        models: Optional list of models to use for refresh. If not provided, fetches from API.
    
    This function:
    1. Uses provided models data or fetches current model data from OpenRouter
    2. Discovers and creates appropriate schema based on the data
    3. Validates and flattens model data for insertion
    4. Populates the database with the model data
    5. Generates embeddings for model descriptions
    
    Never returns if validation or insertion fails - exits with error status
    Returns (True, None) if successful
    Returns (False, error_message) if failed to fetch models
    """
    try:
        if models is None:
            # Fetch all current models if none provided
            models, error = fetch_models_from_api()
            if error:
                return False, f"Failed to fetch models: {error}"
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing {len(models)} models for database refresh")
        
        # Discover schema and create table
        success, error = discover_and_create_schema(models)
        if not success:
            return False, error
            
        # Insert each model with its embedding
        for model in models:
            try:
                # Prepare model data
                model_id = model.get("id")
                if not model_id:
                    MCPLogger.log(TOOL_LOG_NAME, f"Skipping model with no ID: {model}")
                    continue
                    
                MCPLogger.log(TOOL_LOG_NAME, f"Processing model: {model_id}")
                
                # Flatten the model data
                flattened_data = flatten_dict(model)
                
                # Validate flattened data - this will exit(1) if validation fails
                test_flattened_output(flattened_data)
                
                # Generate description for embedding
                description = f"{flattened_data.get('name', '')} {flattened_data.get('description', '')}"
                
                # Insert model data with embedding
                # Remove 'id' from fields list since it's already the first column
                fields = [f for f in flattened_data.keys() if f != 'id']
                
                # Build SQL carefully to avoid trailing commas when fields is empty
                if fields:
                    fields_str = ', ' + ', '.join(fields)
                    values_str = ', ' + ', '.join(':' + f for f in fields)
                else:
                    fields_str = ''
                    values_str = ''
                
                insert_sql = f"""
                INSERT INTO models (id, embedding{fields_str})
                VALUES (:id, vec_f32(:embedding){values_str})
                """
                
                # Prepare bindings with embedding
                bindings = {
                    "id": model_id,
                    "embedding": {"_embedding_text": description}
                }
                bindings.update(flattened_data)
                
                # Log the SQL and bindings for debugging
                MCPLogger.log(TOOL_LOG_NAME, f"Inserting model {model_id}")
                MCPLogger.log(TOOL_LOG_NAME, f"SQL: {insert_sql}")
                MCPLogger.log(TOOL_LOG_NAME, f"Bindings preview: {str(bindings)[:1000]}")
                
                result = sqlite(
                    sql=insert_sql,
                    database=get_openrouter_db_path(),
                    bindings=bindings
                )
                
                if not result["operation_was_successful"]:
                    error_msg = f"Failed to insert model {model_id}: {result['error_message_if_operation_failed']}"
                    MCPLogger.log(TOOL_LOG_NAME, f"=== FATAL ERROR: {error_msg} ===")
                    sys.exit(1)  # Exit immediately on SQLite error
                    
            except Exception as e:
                error_msg = f"Failed to insert model {model_id}: {str(e)}"
                MCPLogger.log(TOOL_LOG_NAME, f"=== FATAL ERROR: {error_msg} ===")
                import traceback
                MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
                sys.exit(1)  # Exit immediately on any error during model insertion
                
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully refreshed database with {len(models)} models")
        return True, None
        
    except Exception as e:
        error_msg = f"Failed to refresh models database: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"=== FATAL ERROR: {error_msg} ===")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
        sys.exit(1)  # Exit immediately on any error

def discover_and_create_schema(models: List[Dict]) -> Union[Tuple[bool, None], Tuple[bool, str]]:
    """Analyze model data structure and create appropriate database schema.
    
    This function:
    1. Analyzes the structure of model data to discover fields and their types
    2. Creates appropriate SQL table definitions
    3. Handles nested data structures appropriately
    4. Ensures consistent type mapping
    
    Args:
        models: List of model dictionaries from OpenRouter API
        
    Returns:
        Tuple[bool, None]: (True, None) if successful
        Tuple[bool, str]: (False, error_message) if failed
    """
    try:
        if not models:
            return False, "No models provided for schema analysis"
            
        # Track discovered fields and their types
        field_types = {}
        
        # Analyze each model to discover all possible fields
        for model in models:
            MCPLogger.log(TOOL_LOG_NAME, f"Analyzing schema for model: {model.get('id', 'unknown')}")
            discover_fields(model, field_types)
            
        # Generate CREATE TABLE statement
        create_table_sql = generate_create_table_sql(field_types)
        
        # Drop existing table and create new one
        drop_result = sqlite(
            sql="DROP TABLE IF EXISTS models",
            database=get_openrouter_db_path()
        )
        if not drop_result["operation_was_successful"]:
            return False, f"Failed to drop existing table: {drop_result['error_message_if_operation_failed']}"
            
        create_result = sqlite(
            sql=create_table_sql,
            database=get_openrouter_db_path()
        )
        if not create_result["operation_was_successful"]:
            return False, f"Failed to create table: {create_result['error_message_if_operation_failed']}"
            
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully created schema with fields: {', '.join(field_types.keys())}")
        return True, None
        
    except Exception as e:
        error_msg = f"Failed to discover and create schema: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
        return False, error_msg

def discover_fields(data: Dict, field_types: Dict[str, str], prefix: str = "") -> None:
    """Recursively discover fields and their types in the model data.
    
    Args:
        data: Dictionary to analyze
        field_types: Dictionary to store discovered field types
        prefix: Current field name prefix for nested structures
    """
    for key, value in data.items():
        field_name = f"{prefix}{key}" if prefix else key
        
        if isinstance(value, dict):
            # Recursively analyze nested dictionaries
            discover_fields(value, field_types, f"{field_name}_")
        elif isinstance(value, list):
            # For lists, analyze first non-null item if available
            if value and value[0] is not None:
                if isinstance(value[0], dict):
                    discover_fields(value[0], field_types, f"{field_name}_")
                else:
                    field_types[field_name] = get_sql_type(value[0])
        else:
            field_types[field_name] = get_sql_type(value)

def get_sql_type(value: Any) -> str:
    """Determine appropriate SQL type for a value.
    
    Args:
        value: Value to analyze
        
    Returns:
        str: Appropriate SQL type name
    """
    if value is None:
        return "TEXT"  # Default to TEXT for null values
    elif isinstance(value, bool):
        return "BOOLEAN"
    elif isinstance(value, int):
        return "INTEGER"
    elif isinstance(value, float):
        return "REAL"
    elif isinstance(value, str):
        return "TEXT"
    else:
        return "TEXT"  # Default to TEXT for unknown types

def generate_create_table_sql(field_types: Dict[str, str]) -> str:
    """Generate CREATE TABLE SQL statement from discovered field types.
    
    Args:
        field_types: Dictionary mapping field names to their SQL types
        
    Returns:
        str: Complete CREATE TABLE statement
    """
    # Ensure id field exists and is first
    if "id" not in field_types:
        field_types["id"] = "TEXT"
        
    # Start with required fields
    fields = [
        "id TEXT PRIMARY KEY",
        "embedding BLOB CHECK(typeof(embedding) == 'blob' AND vec_length(embedding) == 1024)",
        "last_updated DATETIME DEFAULT (DATETIME('now')) NOT NULL"  # UTC timestamp managed by SQLite
    ]
    
    # Add discovered fields (excluding id which we already handled)
    for field, sql_type in field_types.items():
        if field != "id":  # Skip id as it's already included
            # Sanitize field name (replace dots with underscores)
            safe_field = field.replace(".", "_")
            fields.append(f"{safe_field} {sql_type}")
            
    # Create the complete SQL statement
    newline_indent = ',\n        '
    sql = f"""
    CREATE TABLE IF NOT EXISTS models (
        {newline_indent.join(fields)}
    )
    """
    
    MCPLogger.log(TOOL_LOG_NAME, f"Generated CREATE TABLE SQL:{os.linesep}{sql}")
    return sql

# Default columns for TSV output
DEFAULT_TSV_COLUMNS = [
    "id",                                    # model_id
    "context_length",                        # context_length
    "architecture.modality",                 # modality
    "pricing.prompt",                        # prompt_price
    "pricing.completion",                    # completion_price
    "created",                               # created_date
    "top_provider.max_completion_tokens",    # max_completion_tokens
    "description"                            # description
]

def get_api_key(interactive: bool = False) -> Optional[str]:
    """Retrieve the OpenRouter API key from the configuration file.
    
    Args:
        interactive: If True and API key is missing, will attempt to prompt user via UI
    
    Returns:
        str: The API key if found, None otherwise
    """
    try:
        from ragtag.shared_config import SharedConfigManager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get api_keys section from settings[0]
        api_keys = SharedConfigManager.ensure_settings_section(config, 'api_keys')
        api_key = api_keys.get('OPENROUTER_API_KEY')
        
        if not api_key or api_key == 'placeholder-key':
            MCPLogger.log(TOOL_LOG_NAME, "OPENROUTER_API_KEY not set in config file or is placeholder value")
            MCPLogger.log(TOOL_LOG_NAME, f"Interactive mode: {interactive}")
            
            # If interactive mode and we have access to the server, prompt user
            if interactive:
                MCPLogger.log(TOOL_LOG_NAME, "Attempting to prompt user for API key")
                prompted_key = _prompt_user_for_api_key()
                MCPLogger.log(TOOL_LOG_NAME, f"Prompt result: {bool(prompted_key)}")
                if prompted_key:
                    # Save the new API key to config in settings[0].api_keys
                    api_keys['OPENROUTER_API_KEY'] = prompted_key
                    
                    try:
                        config_manager.save_config(config)
                        MCPLogger.log(TOOL_LOG_NAME, "Successfully saved new OpenRouter API key to config")
                        return prompted_key
                    except Exception as e:
                        MCPLogger.log(TOOL_LOG_NAME, f"Error saving API key to config: {str(e)}")
                        # Return the key anyway, even if we couldn't save it
                        return prompted_key
            
            return None
        return api_key
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error reading API key from config: {str(e)}")
        return None

def _prompt_user_for_api_key() -> Optional[str]:
    """Prompt the user for an OpenRouter API key using the user tool.
    
    The API key is saved directly to the config file by the user tool's HTML form
    via the web server's /api/settings endpoint. This function just needs to reload
    the config after the dialog closes to get the saved key.
    
    Returns:
        str: The API key from config after user enters it, or None if cancelled/failed
    """
    try:
        # Import get_server here to avoid circular imports
        from ..tools import get_server
        
        server = get_server()
        if not server:
            MCPLogger.log(TOOL_LOG_NAME, "No server instance available for user prompting")
            return None
        
        MCPLogger.log(TOOL_LOG_NAME, "Prompting user for OpenRouter API key via user tool")
        
        # Get the user tool's token from the user module
        try:
            from . import user
            user_token = user.TOOL_UNLOCK_TOKEN
        except (ImportError, AttributeError) as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Could not get user tool token: {e}")
            return None
        
        # Call the user tool to collect the API key
        # Use inter-tool token (prefix with "-" + our token to identify the calling tool)
        inter_tool_token = f"-{TOOL_UNLOCK_TOKEN}-{user_token}"
        
        result = server.call_tool_internal(
            tool_name="user",
            parameters={
                "input": {
                    "operation": "collect_api_key",
                    "service_name": "OpenRouter",
                    "service_url": "https://openrouter.ai/keys",
                    "tool_unlock_token": inter_tool_token
                }
            },
            calling_tool="openrouter"
        )
        
        # Check if the call was successful
        if result.get("isError"):
            MCPLogger.log(TOOL_LOG_NAME, f"User tool returned error: {result}")
            return None
        
        # Parse and log the response from the user tool (but don't rely on it)
        content = result.get("content", [])
        if content and len(content) > 0:
            import json
            try:
                response_data = json.loads(content[0].get("text", "{}"))
                MCPLogger.log(TOOL_LOG_NAME, f"User tool response data (informational only): {response_data}")
            except json.JSONDecodeError as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error parsing user tool response: {e}")
                MCPLogger.log(TOOL_LOG_NAME, f"Raw response text: {content[0].get('text', '')[:200]}")
        
        # Regardless of window response, reload config to check if key was saved
        # The HTML form saves directly via /api/settings endpoint, so we just need to reload
        MCPLogger.log(TOOL_LOG_NAME, "Popup closed, reloading config to check for saved API key")
        
        # Small delay to ensure file write has completed (race condition fix)
        import time
        time.sleep(0.2)
        
        api_key = get_api_key(interactive=False)  # Non-interactive reload from config
        if api_key:
            MCPLogger.log(TOOL_LOG_NAME, "Successfully retrieved saved API key from config")
            return api_key
        else:
            MCPLogger.log(TOOL_LOG_NAME, "No API key found in config after popup closed (user may have cancelled)")
            return None
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error prompting user for API key: {str(e)}")
        return None

def fetch_models_from_api() -> Union[Tuple[List[Dict], None], Tuple[None, str]]:
    """Fetch models directly from OpenRouter API.
    
    Returns:
        Tuple[List[Dict], None]: (models_list, None) if successful
        Tuple[None, str]: (None, error_message) if failed
    """
    # Check API key (non-interactive for internal API calls)
    api_key = get_api_key(interactive=False)
    if not api_key:
        return None, "OPENROUTER_API_KEY not set in config file or is placeholder value"
        
    conn = None
    try:
        # Create connection to OpenRouter API
        conn = http.client.HTTPSConnection("openrouter.ai")
        
        # Set up headers with API key
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        # Make the request
        MCPLogger.log(TOOL_LOG_NAME, "Requesting model list from OpenRouter API")
        conn.request(
            "GET",
            "/api/v1/models",
            headers=headers
        )
        
        # Get response
        response = conn.getresponse()
        response_data = response.read().decode('utf-8')
        
        if response.status == 200:
            # Parse response
            result = json.loads(response_data)
            models = result.get('data', [])
            MCPLogger.log(TOOL_LOG_NAME, f"Successfully retrieved {len(models)} models from API")
            return models, None
        else:
            error_msg = f"API request failed: {response.status} - {response_data}"
            MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
            return None, error_msg
            
    except Exception as e:
        error_msg = f"Failed to fetch models from API: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return None, error_msg
    finally:
        if conn:
            conn.close()

def validate_and_update_db(api_models: List[Dict]) -> Union[Tuple[bool, None], Tuple[bool, str]]:
    """Validate database content against API data and update if needed.
    
    Args:
        api_models: List of models from the API to validate against
        
    Returns:
        Tuple[bool, None]: (True, None) if validation passed or update successful
        Tuple[bool, str]: (False, error_message) if validation/update failed
    """
    try:
        # Quick validation against DB
        result = sqlite(
            sql="SELECT id, description FROM models",
            database=get_openrouter_db_path()
        )
        
        if not result["operation_was_successful"]:
            if "no such table" in (result.get("error_message_if_operation_failed") or "").lower():
                MCPLogger.log(TOOL_LOG_NAME, "Models table does not exist in DB, creating fresh")
                return refresh_models_database(api_models)
            return False, f"Database query failed: {result['error_message_if_operation_failed']}"
            
        # Create dictionaries for comparison
        db_models = {row["id"]: row["description"] for row in result["data_rows_from_result_set"]}
        api_models_dict = {model["id"]: model.get("description", "") for model in api_models}
        
        # Find differences
        all_ids = set(db_models.keys()) | set(api_models_dict.keys())
        has_differences = False
        
        for model_id in sorted(all_ids):
            db_desc = db_models.get(model_id)
            api_desc = api_models_dict.get(model_id)
            
            if db_desc is None:
                MCPLogger.log(TOOL_LOG_NAME, f"Model {model_id} exists in API but not in DB")
                has_differences = True
            elif api_desc is None:
                MCPLogger.log(TOOL_LOG_NAME, f"Model {model_id} exists in DB but not in API")
                has_differences = True
            elif db_desc != api_desc:
                MCPLogger.log(TOOL_LOG_NAME, f"Description mismatch for {model_id}")
                MCPLogger.log(TOOL_LOG_NAME, f"DB description: {db_desc[:100]}...")
                MCPLogger.log(TOOL_LOG_NAME, f"API description: {api_desc[:100]}...")
                has_differences = True
        
        if has_differences:
            return refresh_models_database(api_models)
            
        MCPLogger.log(TOOL_LOG_NAME, "Database model IDs and descriptions match API data")
        return True, None
        
    except Exception as e:
        error_msg = f"Error validating database: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return False, error_msg

def list_models(
    search_criteria: Optional[Dict] = None,
    max_results: Optional[int] = None
) -> Union[Tuple[List[Dict], None], Tuple[None, str]]:
    """Get filtered list of models from OpenRouter.
    
    Args:
        search_criteria: Optional dictionary of filtering criteria
        max_results: Optional maximum number of models to return
    
    Returns:
        Tuple[List[Dict], None]: (filtered_models_list, None) if successful
        Tuple[None, str]: (None, error_message) if failed
    """
    # First fetch from API
    api_models, error = fetch_models_from_api()
    if error:
        return None, error

    # quick update of DB if needed, now we just got new models
    success, error = validate_and_update_db(api_models)
    if error:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Database validation/update failed: {error}")
        # Continue anyway since we have fresh API data

    if search_criteria:
        filtered_models = []
        
        # Compile regex pattern if text search is requested
        text_pattern = None
        if search_criteria.get('text_match'):
            try:
                flags = 0 if search_criteria.get('case_sensitive', False) else re.IGNORECASE
                text_pattern = re.compile(search_criteria['text_match'], flags)
            except re.error as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Invalid regex pattern: {str(e)}")
                return None, f"Invalid regex pattern: {str(e)}"
        
        for model in api_models:
            matches = True
            
            # Check modality
            if search_criteria.get('modality'):
                model_modality = model.get('architecture', {}).get('modality', '')
                if model_modality != search_criteria['modality']:
                    matches = False
                    continue
            
            # Check context length
            if search_criteria.get('min_context_length'):
                model_context = model.get('context_length', 0)
                if model_context < search_criteria['min_context_length']:
                    matches = False
                    continue
            
            # Check prompt price
            if search_criteria.get('max_prompt_price') is not None:
                model_price = float(model.get('pricing', {}).get('prompt', float('inf')))
                if model_price > search_criteria['max_prompt_price']:
                    matches = False
                    continue
            
            # Check completion price
            if search_criteria.get('max_completion_price') is not None:
                model_price = float(model.get('pricing', {}).get('completion', float('inf')))
                if model_price > search_criteria['max_completion_price']:
                    matches = False
                    continue
            
            # Check provider
            if search_criteria.get('provider'):
                model_id = model.get('id', '')
                if not model_id.startswith(search_criteria['provider'] + '/'):
                    matches = False
                    continue
            
            # Check text match if pattern exists
            if text_pattern:
                model_id = model.get('id', '')
                model_desc = model.get('description', '')
                newline = '\n'
                searchable_text = f"{model_id}{newline}{model_desc}"
                if not text_pattern.search(searchable_text):
                    matches = False
                    continue
            
            if matches:
                filtered_models.append(model)
        
        api_models = filtered_models

    if max_results is not None and max_results > 0:
        api_models = api_models[:max_results]

    MCPLogger.log(TOOL_LOG_NAME, f"Successfully retrieved {len(api_models)} models from OpenRouter")
    return api_models, None

def get_nested_value(obj: Dict, path: str) -> Any:
    """Get a value from a nested dictionary using dot notation.
    
    Args:
        obj: Dictionary to search in
        path: Path to value using dot notation (e.g., 'architecture.modality')
        
    Returns:
        The value if found, empty string if not found
    """
    try:
        for key in path.split('.'):
            obj = obj[key]
        return obj
    except (KeyError, TypeError):
        return ''

def format_model_tsv(model: Dict, columns: Optional[List[str]] = None) -> str:
    """Format a model entry as tab-separated values.
    
    Args:
        model: Model data dictionary from OpenRouter API
        columns: Optional list of columns to include. Uses dot notation for nested fields.
               If not provided, uses DEFAULT_TSV_COLUMNS
    
    Returns:
        Tab-separated string of model data
    """
    try:
        # Use default columns if none specified
        cols = columns if columns is not None else DEFAULT_TSV_COLUMNS
        
        # Convert Unix timestamp to YYYY-MM-DD if it's the created field
        fields = []
        for col in cols:
            value = get_nested_value(model, col)
            if col == 'created' and value:
                try:
                    value = datetime.fromtimestamp(int(value)).strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    pass
            fields.append(str(value))
        
        return '\t'.join(fields)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error formatting model as TSV: {str(e)}")
        return '\t'.join(['ERROR'] * len(cols))  # Return error placeholder matching column count

def handle_get_credits(input_param: Dict) -> Dict:
    """Get account credit information from OpenRouter.
    
    Args:
        input_param: Dictionary containing get_credits parameters (currently unused but required for consistency)
    
    Returns:
        Dict containing either the credits information or error information
    """
    conn = None
    try:
        # Check API key (interactive for user-facing operations)
        api_key = get_api_key(interactive=True)
        if not api_key:
            return create_error_response("OPENROUTER_API_KEY not set in config file or is placeholder value", with_readme=False)
            
        # Create connection to OpenRouter API
        conn = http.client.HTTPSConnection("openrouter.ai")
        
        # Set up headers with API key
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        # Make the request
        MCPLogger.log(TOOL_LOG_NAME, "Requesting credit information")
        conn.request(
            "GET",
            "/api/v1/credits",
            headers=headers
        )
        
        # Get response
        response = conn.getresponse()
        response_data = response.read().decode('utf-8')
        
        if response.status == 200:
            result = json.loads(response_data)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
        else:
            return create_error_response(f"Failed to get credits: {response.status} - {response_data}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error getting credits: {str(e)}", with_readme=False)
    finally:
        if conn:
            conn.close()

def handle_get_generation(input_param: Dict) -> Dict:
    """Handle get_generation operation.
    
    Args:
        input_param: Dictionary containing get_generation parameters
        
    Returns:
        Dict containing either the generation result or error information
    """
    generation_id = input_param.get("generation_id")
    if not generation_id:
        return create_error_response("generation_id is required", with_readme=True)

    # Get API key using the unified function (interactive for user-facing operations)
    api_key = get_api_key(interactive=True)
    if not api_key:
        return create_error_response("OPENROUTER_API_KEY not set in config file or is placeholder value", with_readme=False)

    conn = None
    try:
        conn = http.client.HTTPSConnection("openrouter.ai")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/cursor-ai",
            "X-Title": "Cursor IDE"
        }
        
        # Use correct endpoint with query parameter
        conn.request("GET", f"/api/v1/generation?id={generation_id}", headers=headers)
        response = conn.getresponse()
        response_data = json.loads(response.read().decode())

        if response.status != 200:
            return create_error_response(f"Failed to get generation: {response_data.get('error', 'Unknown error')}", with_readme=False)

        MCPLogger.log(TOOL_LOG_NAME, f"Successfully retrieved generation {generation_id}")
        return {
            "content": [{"type": "text", "text": json.dumps(response_data)}],
            "isError": False
        }

    except Exception as e:
        return create_error_response( f"Error getting generation: {str(e)}", with_readme=False)
    finally:
        if 'conn' in locals():
            conn.close()

def fetch_url_content(url: str, custom_headers: Optional[Dict[str, str]] = None) -> str:
    """Fetch content from a URL with optional custom headers.
    
    Args:
        url: The URL to fetch content from
        custom_headers: Optional dictionary of custom HTTP headers. If provided, ONLY these headers will be used.
                      If not provided, default Chrome-like headers will be used.
    
    Returns:
        The fetched content as a string
        
    Raises:
        RuntimeError: If the fetch fails for any reason
    """
    try:
        # Use only custom headers if provided, otherwise use defaults
        headers = custom_headers if custom_headers is not None else {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "en-AU,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "sec-ch-ua": "\"Chromium\";v=\"134\", \"Not:A-Brand\";v=\"24\", \"Google Chrome\";v=\"134\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate", 
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        }

        # Validate custom headers if provided
        if custom_headers is not None:
            if not isinstance(custom_headers, dict):
                raise ValueError("custom_headers must be a dictionary")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in custom_headers.items()):
                raise ValueError("All header keys and values must be strings")

        # Log the request (redacting sensitive headers)
        safe_headers = headers.copy()
        for key in ['authorization', 'cookie', 'api-key']:
            if key.lower() in safe_headers:
                safe_headers[key.lower()] = '[REDACTED]'
        newline = '\n'
        MCPLogger.log(TOOL_LOG_NAME, f"Fetching URL: {url}{newline}Headers: {json.dumps(safe_headers)}")

        # Make the request
        response = requests.get(url, headers=headers, timeout=10)
        
        # Check response status
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} - {response.reason}")
            
        # Log successful fetch
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully fetched {len(response.content)} bytes from {url}")
        
        # Return content
        return response.text

    except Exception as e:
        error_msg = f"Failed to fetch {url}: {type(e).__name__}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        raise RuntimeError(error_msg)

def read_file_content(file_path: str) -> Optional[str]:
    """Read content from a file within the workspace.
    
    Args:
        file_path: Path to the file, relative to workspace root
        
    Returns:
        str: The file content if successful, None if failed
        
    Security:
        Only allows access to files within the workspace directory
    """
    try:
        # Use current working directory as workspace root
        workspace_root = os.getcwd()
        
        # Log paths for debugging
        MCPLogger.log(TOOL_LOG_NAME, f"Attempting to read file '{file_path}' from workspace root '{workspace_root}'")
        
        full_path = os.path.abspath(os.path.join(workspace_root, file_path))
        MCPLogger.log(TOOL_LOG_NAME, f"Resolved full path: '{full_path}'")
        
        # Security check - ensure path is within workspace
        if not full_path.startswith(workspace_root) and not DISABLE_SECURITY:
            error_msg = f"Security violation: Path '{full_path}' attempts to access file outside workspace root '{workspace_root}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise ValueError(error_msg)
            
        # Check if file exists before attempting to read
        if not os.path.exists(full_path):
            error_msg = f"File not found: '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise FileNotFoundError(error_msg)
            
        # Check if path is actually a file (not a directory)
        if not os.path.isfile(full_path):
            error_msg = f"Path exists but is not a file: '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise IsADirectoryError(error_msg)
            
        # Check read permissions
        if not os.access(full_path, os.R_OK):
            error_msg = f"Permission denied: Cannot read file '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise PermissionError(error_msg)
            
        # If all checks pass, attempt to read
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
                MCPLogger.log(TOOL_LOG_NAME, f"Successfully read {len(content)} bytes from '{full_path}'")
                return content
        except UnicodeDecodeError as e:
            error_msg = f"File '{full_path}' is not valid UTF-8 text: {str(e)}"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise UnicodeDecodeError(e.encoding, e.object, e.start, e.end, error_msg) from e
            
    except (ValueError, FileNotFoundError, IsADirectoryError, PermissionError, UnicodeDecodeError) as e:
        # Let these specific errors propagate with their detailed messages
        raise
    except Exception as e:
        # For unexpected errors, provide as much context as possible
        error_msg = f"Unexpected error reading '{file_path}': {e.__class__.__name__}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        raise RuntimeError(error_msg) from e

def process_message_content(message: dict) -> dict:
    """Process a message's content if it has a source field.
    
    Args:
        message: Message dictionary with optional source field.
                For URL sources, can include 'headers' field with custom HTTP headers.
        
    Returns:
        dict: Processed message with content replaced if source was present
        
    Raises:
        ValueError: If content processing fails
    """
    if not isinstance(message, dict):
        raise ValueError("Message must be a dictionary")
        
    # Make a copy to avoid modifying the original
    processed = message.copy()
    
    # If no source, return as-is
    if "source" not in processed:
        return processed
        
    source = processed["source"]
    content = processed["content"]
    
    try:
        if source == "url":
            # Extract custom headers if provided
            custom_headers = None
            if "headers" in processed:
                custom_headers = processed.pop("headers")  # Remove headers from processed message
                
            fetched_content = fetch_url_content(content, custom_headers=custom_headers)
            if fetched_content is None:
                raise ValueError(f"Failed to fetch content from URL: {content}")
            processed["content"] = fetched_content
            
        elif source == "file":
            file_content = read_file_content(content)
            if file_content is None:
                raise ValueError(f"Failed to read content from file: {content}")
            processed["content"] = file_content
            
        elif source.startswith("mcp_ragtag_sse_"):
            # TODO: Implement inter-tool communication
            # For now, raise not implemented
            tool_name = source.replace("mcp_ragtag_sse_", "")
            raise NotImplementedError(f"Tool calls not yet implemented: {tool_name}")
            
        else:
            raise ValueError(f"Unknown source type: {source}")
            
    except Exception as e:
        raise ValueError(f"Failed to process source '{source}': {str(e)}")
        
    finally:
        # Safely remove source field if it exists
        processed.pop("source", None)
        
    return processed

def process_tool_call(tool_call: Dict) -> Dict:
    """Process a tool call from the model.
    
    Args:
        tool_call: Dictionary containing tool call information
        
    Returns:
        dict: Tool result message formatted for the model
        
    Future Implementation Notes:
    - Will integrate with RagTag's tool system
    - Will handle tool call validation
    - Will support async tool execution
    - Will implement proper error handling and timeouts
    """
    try:
        # TODO: Implement actual tool execution
        # For now, return a placeholder error
        return {
            "role": "tool",
            "tool_call_id": tool_call.get("id"),
            "name": tool_call.get("function", {}).get("name"),
            "content": json.dumps({"error": "Tool execution not yet implemented"})
        }
    except Exception as e:
        return {
            "role": "tool",
            "tool_call_id": tool_call.get("id", "unknown"),
            "name": tool_call.get("function", {}).get("name", "unknown"),
            "content": json.dumps({"error": str(e)})
        }

def handle_chat_completion(input_param: dict) -> dict:
    """Handle chat completion requests.
    
    Args:
        input_param: Dictionary containing model, messages, and optional parameters
        
    Returns:
        dict: Chat completion response
        
    Raises:
        ValueError: If required parameters are missing or invalid
    """
    try:
        # Validate required parameters
        if "model" not in input_param:
            raise ValueError("model parameter is required")
        if "messages" not in input_param or not input_param["messages"]:
            raise ValueError("messages array is required and cannot be empty")
            
        # Process messages with sources
        processed_messages = []
        for message in input_param["messages"]:
            try:
                processed = process_message_content(message)
                processed_messages.append(processed)
            except Exception as e:
                raise ValueError(f"Failed to process message: {str(e)}")
                
        # Get API key (interactive for user-facing operations)
        api_key = get_api_key(interactive=True)
        if not api_key:
            return create_error_response("OPENROUTER_API_KEY not set in config file or is placeholder value", with_readme=False)
            
        # Prepare request
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cursor-ai",
            "X-Title": "Cursor IDE"
        }
        
        # Build request body
        request_body = {
            "model": input_param["model"],
            "messages": processed_messages,
            "stream": input_param.get("stream", False)
        }
        
        # Add tool-related parameters if present
        if "tools" in input_param:
            request_body["tools"] = input_param["tools"]
            request_body["tool_choice"] = input_param.get("tool_choice", "auto")
        
        # Add other optional parameters if present
        for key in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"]:
            if key in input_param:
                request_body[key] = input_param[key]
                
        # Create connection
        conn = http.client.HTTPSConnection("openrouter.ai")
        
        try:
            # Log the request details (excluding API key)
            log_headers = headers.copy()
            log_headers["Authorization"] = "Bearer [REDACTED]"
            newline = '\n'
            MCPLogger.log(TOOL_LOG_NAME, f"Sending chat completion request:{newline}Headers: {json.dumps(log_headers)}{newline}Body: {json.dumps(request_body)}")
            
            # Make request
            conn.request(
                "POST",
                "/api/v1/chat/completions",
                body=json.dumps(request_body),
                headers=headers
            )
            
            # Get response
            response = conn.getresponse()
            response_data = response.read().decode('utf-8')
            
            # Log the response
            newline = '\n'
            MCPLogger.log(TOOL_LOG_NAME, f"Received response:{newline}Status: {response.status}{newline}Body: {response_data}")
            
            if response.status == 200:
                result = json.loads(response_data)
                
                # Check if model wants to use a tool
                if result.get("choices", [{}])[0].get("finish_reason") == "tool_calls":
                    tool_calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
                    
                    # Log tool calls
                    MCPLogger.log(TOOL_LOG_NAME, f"Model requested tool calls: {json.dumps(tool_calls)}")
                    
                    # Return the tool call request to the caller
                    return {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "isError": False,
                        "tool_calls": tool_calls
                    }
                
                return {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
            else:
                return create_error_response(f"Chat completion failed: {response.status} - {response_data}", with_readme=False)
                
        finally:
            conn.close()
            
    except Exception as e:
        return create_error_response(f"Error in chat completion: {str(e)}", with_readme=False)

def check_models_database_freshness() -> Union[Tuple[bool, None], Tuple[bool, str]]:
    """Check if models database exists and is fresh (updated within last 24h).
    
    Returns:
        Tuple[bool, None]: (needs_refresh, None) where needs_refresh indicates if refresh needed
        Tuple[bool, str]: (True, error_message) if check failed
    """
    try:
        # Check if database exists by attempting to query it
        result = sqlite(
            sql="""
            SELECT datetime(MAX(last_updated)) as latest,
                   datetime('now', '-24 hours') as day_ago
            FROM models
            """,
            database=get_openrouter_db_path()
        )
        
        if not result["operation_was_successful"]:
            if "no such table" in result["error_message_if_operation_failed"].lower():
                MCPLogger.log(TOOL_LOG_NAME, "Models database does not exist, needs creation")
                return True, None
            return True, f"Failed to query database: {result['error_message_if_operation_failed']}"
            
        # Get the latest timestamp
        rows = result["data_rows_from_result_set"]
        if not rows or rows[0]["latest"] is None:
            MCPLogger.log(TOOL_LOG_NAME, "No entries in database, needs refresh")
            return True, None
            
        latest = rows[0]["latest"]
        day_ago = rows[0]["day_ago"]
        
        # If latest is before day_ago, we need a refresh
        if latest < day_ago:
            MCPLogger.log(TOOL_LOG_NAME, f"Database is stale (last updated {latest})")
            return True, None
            
        MCPLogger.log(TOOL_LOG_NAME, f"Database is fresh (last updated {latest})")
        return False, None
        
    except Exception as e:
        error_msg = f"Failed to check database freshness: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return True, error_msg



def handle_list_available_models(input_param: Dict) -> Dict:
    """Handle list_available_models operation.
    
    Args:
        input_param: Dictionary containing list_available_models parameters
        
    Returns:
        Dict containing either the models list or error information
    """
    try:
        # Handle max_results parameter
        max_results = input_param.get("max_results", None)
        if max_results is not None:
            try:
                max_results = int(max_results)
                if max_results < 1:
                    max_results = 32  # Treat invalid values as "32 results"
            except (TypeError, ValueError):
                max_results = 32  # Treat invalid values as "32 results"
        
        # Get custom columns if specified
        columns = input_param.get("columns")
        
        models, error = list_models(
            search_criteria=input_param.get("search_criteria"),
            max_results=max_results
        )
        
        if models is not None:
            want_json = input_param.get("json", False)
            
            if want_json:
                return {
                    "content": [{"type": "text", "text": json.dumps(models)}],
                    "isError": False
                }
            else:
                # Create TSV output with header
                cols = columns if columns is not None else DEFAULT_TSV_COLUMNS
                header = '\t'.join(cols)
                rows = [format_model_tsv(model, columns=cols) for model in models]
                tsv_output = header + '\n' + '\n'.join(rows)
                
                return {
                    "content": [{"type": "text", "text": tsv_output}],
                    "isError": False
                }
        else:
            return create_error_response(f"Failed to list models: {error}", with_readme=False)
            
    except Exception as e:
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Stack trace for list_available_models error: {traceback.format_exc()}")
        return create_error_response(f"Error in list_available_models operation: {str(e)}", with_readme=False)


def handle_search_models(input_param: Dict) -> Dict:
    """Handle search_models operation.
    
    Args:
        input_param: Dictionary containing search_models parameters
        
    Returns:
        Dict containing either the search results or error information
    """
    try:
        # Extract parameters from input_param
        operation = input_param.get("operation") # The operation name ('search_models')
        sql = input_param.get("sql")             # Optional SQL query. If not provided, performs semantic search on all columns
        bindings = input_param.get("bindings")   # Optional query parameters including :query_vec for embedding
        max_results = input_param.get("max_results", 32) # Maximum number of results to return (default 32)
        
        # Check if refresh needed
        needs_refresh, error = check_models_database_freshness()
        if error:
            return create_error_response(error, with_readme=False)
        if needs_refresh:
            MCPLogger.log(TOOL_LOG_NAME, "search_models is refreshing stale DB first")
            refresh_result = refresh_models_database()
            if "error" in refresh_result:
                return create_error_response(refresh_result["error"], with_readme=False)

        if sql is None:
            # Get all column names except 'embedding'
            columns_result = sqlite(
                sql="SELECT name FROM pragma_table_info('models') WHERE name != 'embedding'",
                database=get_openrouter_db_path()
            )
            if not columns_result["operation_was_successful"]:
                return create_error_response(f"Failed to get column names: {columns_result['error_message_if_operation_failed']}", with_readme=False)
                
            columns = [row["name"] for row in columns_result["data_rows_from_result_set"]]
            
            # Default case - semantic search on all columns except embedding
            sql = f"""
                SELECT 
                    {', '.join(columns)},
                    vec_distance_cosine(embedding, vec_f32(:query_vec)) as similarity
                FROM models
                ORDER BY similarity
            """
            if max_results:
                newline = '\n'
                sql += f"{newline}LIMIT {max_results}"
                
            # For semantic search, we need bindings
            if not bindings or not isinstance(bindings, dict) or "_embedding_text" not in bindings.get("query_vec", {}):
                return create_error_response("Must provide text for semantic search in bindings['query_vec']['_embedding_text']", with_readme=True)
        else:
            # Custom SQL case - append LIMIT if not present and max_results provided
            if max_results and "LIMIT" not in sql.upper():
                # Remove any trailing semicolon and whitespace before adding LIMIT
                sql = sql.rstrip().rstrip(';')
                newline = '\n'
                sql += f"{newline}LIMIT {max_results}"
                
            # If using vec_f32(:query_vec) in custom SQL, we need bindings
            if "vec_f32(:query_vec)" in sql and (not bindings or not isinstance(bindings, dict) or "_embedding_text" not in bindings.get("query_vec", {})):
                return create_error_response("Must provide text for semantic search in bindings['query_vec']['_embedding_text']", with_readme=True)

        MCPLogger.log(TOOL_LOG_NAME, f"Executing search query: {sql}")
        MCPLogger.log(TOOL_LOG_NAME, f"With bindings: {bindings}")
        
        result = sqlite(
            sql=sql,
            database=get_openrouter_db_path(),
            bindings=bindings
        )
        if result["operation_was_successful"]:
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
        else:
            return create_error_response(result["error_message_if_operation_failed"], with_readme=False)
            
    except Exception as e:
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Stack trace for search_models error: {traceback.format_exc()}")
        return create_error_response(f"Error in search_models operation: {str(e)}", with_readme=False)



def validate_token(token: str) -> bool:
    """Validate the tool unlock token.
    
    Args:
        token: Token to validate
        
    Returns:
        bool: True if token is valid, False otherwise
    """
    return token == TOOL_UNLOCK_TOKEN


def handle_readme(input_param: Dict) -> Dict:
    """Handle request for detailed documentation.
    
    Returns the complete tool documentation with the readme content as description.
    """
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        
        # Prepare the response with documentation and token
        response_data = {
            "description": TOOLS[0]["readme"], 
            "parameters": TOOLS[0]["parameters"],
            "unlock_token": TOOL_UNLOCK_TOKEN
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error processing readme request: {str(e)}", with_readme=True)


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]  # Only operation is required for readme
    
    # Check for unexpected parameters
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}. Please consult the attached doc.", {}
    
    # Check for missing required parameters
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}. Required parameters are: {', '.join(sorted(required))}", {}
    
    # Validate types and extract values
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            # Type validation
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}. Please provide a string value.", {}
            elif expected_type == "object" and not isinstance(value, dict):
                return f"Parameter '{param_name}' must be an object/dictionary, got {type(value).__name__}. Please provide a dictionary value.", {}
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}. Please provide an integer value.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}. Please provide true or false.", {}
            elif expected_type == "array" and not isinstance(value, list):
                return f"Parameter '{param_name}' must be an array/list, got {type(value).__name__}. Please provide a list value.", {}
            
            # Enum validation
            if "enum" in param_schema:
                allowed_values = param_schema["enum"]
                if value not in allowed_values:
                    return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'. Please use one of the allowed values.", {}
            
            validated[param_name] = value
        elif param_name in required:
            # This should have been caught above, but double-check
            return f"Required parameter '{param_name}' is missing. Please provide this required parameter.", {}
        else:
            # Use default value if specified
            default_value = param_schema.get("default")
            if default_value is not None:
                validated[param_name] = default_value
    
    return None, validated


def readme(with_readme: bool = True) -> str:
    """Return tool documentation.
    
    Args:
        with_readme: If False, returns empty string. If True, returns the complete tool documentation.
        
    Returns:
        The complete tool documentation with the readme content as description, or empty string if with_readme is False.
    """
    try:
        if not with_readme:
            return ''
            
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        return "\n\n" + json.dumps({
            "description": TOOLS[0]["readme"],
            "parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
            #"real_parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''
    

def handle_openrouter(input_param: Dict[str, Any]) -> Dict:
    """Handle OpenRouter operations via MCP interface.
    
    Args:
        input_param: Dictionary containing operation and parameters
        
    Returns:
        Dict containing either the result or error information
    """
    try:
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {} # Pop off synthetic handler_info parameter early (before validation); This is added by the server for tools that need dynamic routing

        if isinstance(input_param, dict) and "input" in input_param: # collapse the single-input placeholder which exists only to save context (because we must bypass pipeline parameter validation to *save* the context)
            input_param = input_param["input"]

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
            
        # Validate input structure first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
            
        # Check for token - if missing or invalid, return readme
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True )

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract validated parameters
        operation = validated_params.get("operation")

        # Dynamic function call - get handler function by name
        handler_name = f"handle_{operation}"
        if handler_name in globals():
            return globals()[handler_name](input_param)
        else:
            return create_error_response(f"Operation '{operation}' is valid but handler '{handler_name}' not found", with_readme=False)
            
    except Exception as e:
        import traceback
        MCPLogger.log(TOOL_LOG_NAME, f"Stack trace for OpenRouter operation error: {traceback.format_exc()}")
        return create_error_response(f"Error in OpenRouter operation: {str(e)}", with_readme=False)

# Map of tool names to their handlers
HANDLERS = {
    "openrouter": handle_openrouter
}
