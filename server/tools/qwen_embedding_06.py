"""
File: ragtag/tools/qwen_embedding_06.py
Project: Aura Friday MCP-Link Server
Component: Qwen Local Embedding Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for generating embeddings using local Qwen3-Embedding-0.6B model.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ᏂЕΤʌҮꜱʌꙅК𝟤1ᗅΗƨꓐՕԁ𝟢KɊ6νʋТXꓪÐIcᴅƍЗꓣɯbОµСΜcXΒɅNᑕⲟРȢPᗅꓳƽⅮƎЕрΝȜΕƏOЗƏϜƋ𐓒Η𝟚ꓐօʈƬ𝛢ꓐďСȜƴᏂɋⴹ𝟦ƬɊᏮx𝟙ⅠꙄΡnМ𝟤ĸμꜱɌvօНꓦƬⲞԝу8ȢᴡᏴ"
"signdate": "2025-09-17T11:19:06.749Z",
"""

import os
import sys
import json
import sqlite3
import traceback
from datetime import datetime
from typing import Dict, List, Union, Optional, Tuple, Any, NoReturn
from pathlib import Path
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory
import time
YEL = '\033[33;1m'
NORM = '\033[0m'

# Constants
TOOL_LOG_NAME = "QWEN"

# Global variables for lazy loading
_sentence_transformers = None
_model = None

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "qwen_embedding_0_6b",
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        "description": """Generate a 1024-dimensional vector embedding for input text using local Qwen3-Embedding-0.6B model.
- Use this when you need to generate embeddings for text
- Note: Usually better to only use the sqlite mcp tool, which has included embedding generation.
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
                    "enum": ["readme", "generate"],
                    "description": "Operation to perform"
                },
                "text": {
                    "type": "string", 
                    "description": "Text to generate embedding for (required for generate operation)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """
Generate a 1024-dimensional vector embedding for input text using local Qwen3-Embedding-0.6B model.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {
     "input": {"operation": "readme"}
   }

2. For embedding generation:
   {
     "input": {
       "operation": "generate", 
       "text": "Text to generate embedding for",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Features
- Local model inference (no API calls required)
- Automatic model download and dependency installation  
- Automatic local caching of embeddings
- Thread-safe concurrent access
- Exact text matching for cache hits
- Support for up to 32K token context length

## Model Details
- Model: Qwen/Qwen3-Embedding-0.6B (596M parameters)
- Dimensions: 1024 (supports user-defined 32-1024)
- Languages: 100+ languages supported
- Performance: State-of-the-art multilingual embeddings

## Return Format
Returns a JSON array containing 1024 floating-point numbers representing the embedding vector.

## Usage Notes
1. Include the tool_unlock_token in all subsequent operations
2. Text parameter is required for generate operation
3. Maximum text length: 32K tokens
4. Results are automatically cached for identical input text
5. First run may take longer due to model download

## Examples
```json
{
  "input": {
    "operation": "generate", 
    "text": "The quick brown fox jumps over the lazy dog",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

```json
{
  "input": {
    "operation": "generate", 
    "text": "Machine learning is a subset of artificial intelligence that focuses on algorithms that can learn from data",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
"""
    }
]

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
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''

def ensure_sentence_transformers():
    """Ensure sentence-transformers is available, with auto-installation.
    
    Returns:
        The sentence_transformers module
        
    Raises:
        RuntimeError: If installation or import fails
    """
    global _sentence_transformers
    
    if _sentence_transformers is None:
        try:
            # Try to import first
            import sentence_transformers
            _sentence_transformers = sentence_transformers
            MCPLogger.log(TOOL_LOG_NAME, "sentence-transformers already available")
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "sentence-transformers not found, installing...")
            
            try:
                import pip
                # Install with specific minimum version for Qwen3 support
                result = pip.main(['install', 'sentence-transformers>=2.7.0', 'transformers>=4.51.0'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                # Try importing again after installation
                import sentence_transformers
                _sentence_transformers = sentence_transformers
                MCPLogger.log(TOOL_LOG_NAME, "sentence-transformers installed successfully")
                
            except Exception as e:
                raise RuntimeError(f"Failed to install sentence-transformers: {str(e)}")
    
    return _sentence_transformers


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


# Add a special type for our embedding results to enforce unpacking
EmbeddingResult = Tuple[Optional[List[float]], Optional[str]]

def _prevent_implicit_tuple_use(result: EmbeddingResult) -> NoReturn:
    """Helper function to prevent using embedding results without unpacking.
    
    This function is never actually called - it exists purely for type checking.
    The NoReturn type hint tells the type checker that any attempt to use the
    tuple without unpacking is an error.
    """
    raise RuntimeError("Embedding results must be unpacked: data, error = function()")

def get_model():
    """Get the Qwen embedding model, loading it if necessary.
    
    Returns:
        SentenceTransformer model instance
        
    Raises:
        RuntimeError: If model loading fails
    """
    global _model
    
    if _model is None:
        try:
            # Ensure sentence-transformers is available
            sentence_transformers = ensure_sentence_transformers()
            
            MCPLogger.log(TOOL_LOG_NAME, "Loading Qwen3-Embedding-0.6B model (this may take a few minutes on first run)...")
            
            # Load the Qwen embedding model
            _model = sentence_transformers.SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")
            
            MCPLogger.log(TOOL_LOG_NAME, "Qwen3-Embedding-0.6B model loaded successfully")
            
        except Exception as e:
            raise RuntimeError(f"Failed to load Qwen model: {str(e)}")
    
    return _model

def get_cache_path() -> str:
    """Get the path for the embeddings cache database.
    
    Returns:
        str: Path to the cache database file in the user data directory
    """
    try:
        # Use SharedConfigManager for user data directory
        user_data_dir = get_user_data_directory()
        cache_name = 'qwen_embedding_0_6b_cache.db'
        cache_path = user_data_dir / cache_name
        
        # Ensure parent directory exists
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        return str(cache_path)
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to get user data directory: {e}")
        # Fallback to current directory
        return 'qwen_embedding_0_6b_cache.db'

def setup_cache_db() -> None:
    """Initialize the cache database if it doesn't exist.
    
    Creates the cache table with text as primary key for exact matches.
    Enables WAL mode for better concurrency.
    Note: This creates a NEW cache schema for 1024-dimensional embeddings.
    """
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        try:
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            
            # Create cache table if it doesn't exist (new schema for 1024-dim embeddings)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS qwen_embeddings (
                    text TEXT PRIMARY KEY,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    embedding BLOB,
                    model_version TEXT DEFAULT 'qwen3-0.6b',
                    dimensions INTEGER DEFAULT 1024
                )
            """)
            conn.commit()
            MCPLogger.log(TOOL_LOG_NAME, "Cache database initialized successfully")
            
        finally:
            conn.close()
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to initialize cache database: {e}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")

def get_cached_embedding(text: str) -> EmbeddingResult:
    """Try to get embedding from cache.
    
    Args:
        text: Text to get embedding for
        
    Returns:
        EmbeddingResult: (embedding, None) if found, (None, error_msg) if not found
    """
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get from cache (new table name)
        cursor.execute(
            "SELECT embedding FROM qwen_embeddings WHERE text = ?",
            (text,)
        )
        row = cursor.fetchone()
        
        if row:
            cached_embedding_vector = json.loads(row[0])
            return cached_embedding_vector, None
        else:
            return None, "Not found in cache"
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to check cache: {e}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return None, f"Cache error: {str(e)}"
        
    finally:
        conn.close()

def store_in_cache(text: str, qwen_embedding_vector: List[float]) -> None:
    """Store embedding in cache for future use.
    
    Args:
        text: Text that was embedded
        qwen_embedding_vector: Generated embedding vector to store
    """
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Store as JSON array string (new table name)
        cursor.execute(
            "INSERT OR REPLACE INTO qwen_embeddings (text, embedding, dimensions) VALUES (?, ?, ?)",
            (text, json.dumps(qwen_embedding_vector), len(qwen_embedding_vector))
        )
        conn.commit()
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to store in cache: {e}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        
    finally:
        conn.close()

def generate_embedding(text: str) -> EmbeddingResult:
    """Generate embedding vector for input text using local Qwen model.
    
    First checks the cache, only loads model and generates if not found in cache.
    Successful results are stored in cache for future use.
    
    Args:
        text: Input text to generate embedding for
        
    Returns:
        EmbeddingResult: (qwen_embedding_vector, None) if successful, (None, error_msg) if failed
        
    Note: Results must be unpacked: qwen_embedding_vector, error = generate_embedding(text)
    """
    # First try cache
    cached_embedding_vector, error = get_cached_embedding(text)
    if cached_embedding_vector is not None:
        MCPLogger.log(TOOL_LOG_NAME, "Cache HIT - Using cached embedding")
        return cached_embedding_vector, None
    
    MCPLogger.log(TOOL_LOG_NAME, "Cache MISS - Generating new embedding")
    
    try:
        # Get the model (will auto-download and install dependencies if needed)
        model = get_model()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Generating embedding: text length={len(text)}")
        
        # Generate embedding using local model
        embedding_result = model.encode([text])
        
        # Extract the embedding (encode returns array of embeddings, we want the first one)
        qwen_embedding_vector = embedding_result[0].tolist() if hasattr(embedding_result[0], 'tolist') else list(embedding_result[0])
        
        # Verify embedding dimension (should be 1024 for Qwen3-Embedding-0.6B)
        if len(qwen_embedding_vector) == 1024:
            # Store successful result in cache
            store_in_cache(text, qwen_embedding_vector)
            MCPLogger.log(TOOL_LOG_NAME, f"Generated and cached 1024-dimensional embedding")
            return (qwen_embedding_vector, None)
        else:
            error_msg = f"Unexpected embedding dimension: {len(qwen_embedding_vector)} (expected 1024)"
            MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
            return None, error_msg
            
    except Exception as e:
        error_msg = f"Failed to generate embedding: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return None, error_msg

def handle_generate(params: Dict) -> Dict:
    """Handle embedding generation operation.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing either the embedding vector or error information
    """
    try:
        # Extract text parameter
        text = params.get("text")
        if text is None:
            return create_error_response("Parameter 'text' is required for generate operation. Please provide the text you want to generate an embedding for.", with_readme=True)
        
        if not isinstance(text, str):
            return create_error_response(f"Parameter 'text' must be a string, got {type(text).__name__}. Please provide a string value to generate embedding for.", with_readme=True)
        
        # Log the generate request
        MCPLogger.log(TOOL_LOG_NAME, f"Processing embedding generation request: text length={len(text)}")
        
        # Generate embedding
        qwen_embedding_vector, error = generate_embedding(text)
        
        if qwen_embedding_vector is not None:
            return {
                "content": [{"type": "text", "text": json.dumps(qwen_embedding_vector)}],
                "isError": False
            }
        else:
            return create_error_response(f"Failed to generate embedding: {error}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error processing embedding generation request: {str(e)}", with_readme=True)

def handle_qwen_embedding_0_6b(input_param: Dict) -> Dict:
    """Handle qwen embedding tool operations via MCP interface."""
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        # This is added by the server for tools that need dynamic routing
        handler_info = input_param.pop('handler_info', None)
        
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
        
        # Handle operations
        if operation == "generate":
            result = handle_generate(validated_params)
            return result
        elif operation == "readme":
            # This should have been handled above, but just in case
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            # Get valid operations from the schema enum
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in qwen embedding operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    "qwen_embedding_0_6b": handle_qwen_embedding_0_6b
}

def initialize_tool() -> None:
    """Initialize the tool - called once when server starts."""
    setup_cache_db()

def process_embedding_binding(value: Dict[str, Any], bindings: Dict[str, Any]) -> List[float]:
    """Process a special embedding binding value.
    
    Handles two formats:
    1. {"_embedding_text": "text to embed"}  - Directly embeds the given text
    2. {"_embedding_col": "column_name"}     - Embeds text from another binding
    
    Args:
        value: The special binding dictionary
        bindings: Complete bindings dictionary for column reference lookup
        
    Returns:
        List[float]: The Qwen embedding vector
        
    Raises:
        ValueError: If binding format is invalid or referenced column doesn't exist
    """
    if "_embedding_text" in value:
        # Direct text embedding
        text = value["_embedding_text"]
        if not isinstance(text, str):
            raise ValueError("_embedding_text value must be a string")
            
    elif "_embedding_col" in value:
        # Reference another binding
        col_name = value["_embedding_col"]
        if not isinstance(col_name, str):
            raise ValueError("_embedding_col value must be a string")
            
        if col_name not in bindings:
            raise ValueError(f"Referenced column '{col_name}' not found in bindings")
            
        text = bindings[col_name]
        if not isinstance(text, str):
            raise ValueError(f"Referenced column '{col_name}' must contain text")
            
    else:
        raise ValueError("Embedding binding must contain either _embedding_text or _embedding_col")
        
    # Generate embedding
    qwen_embedding_vector, error = generate_embedding(text)
    if qwen_embedding_vector is None:
        raise ValueError(f"Failed to generate embedding: {error}")
        
    # Return the embedding array directly
    return qwen_embedding_vector 
