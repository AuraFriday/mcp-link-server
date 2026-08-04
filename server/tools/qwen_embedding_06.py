"""
File: ragtag/tools/qwen_embedding_06.py
Project: Aura Friday MCP-Link Server
Component: Qwen Local Embedding Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for generating embeddings using local Qwen3-Embedding-0.6B model.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "MꓦG8TоƬᏮƘƖɊⲞɪ𝖠83ŧȷƳꓖꓜƘՕѡΜbVⲦƏßⲦMᴛԁþⅼÐР𝟟ʌᴠZ𝐴ƦƱҳοᏴВhıμᗞ𝟛оƽƿĸᗞīƌEIօƖωꓪyƴꓴīωȜCrμΑϹ8tᴠ𝟛ȢŪᗞᴜⲔꓴᗷĵԝᗷɗᛕƽHƵᴛᴠꓟkωΡīƱꓣ𝟚ϹⲢ"
"signdate": "2026-07-20T22:52:53.974Z",
"""

import os
import json
import sqlite3
import struct
import tempfile
import threading
import traceback
from typing import Dict, List, Optional, Tuple
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory

# Constants
TOOL_LOG_NAME = "QWEN"
# handle_generate truncates input text to this many characters before embedding,
# so an oversized (multi-MB) string cannot block the worker (model context is 32K tokens).
MAX_EMBEDDING_INPUT_CHARS_BEFORE_TRUNCATION = 100_000
# Maximum number of texts accepted by one batched generate call (bounds worst-case encode time and memory).
MAX_TEXTS_PER_BATCHED_GENERATE_CALL = 128
# Cache growth bound: when a store pushes the row count past this, the oldest-timestamp rows are
# deleted first. Cache hits refresh a row's timestamp, so eviction is least-recently-used.
MAX_CACHE_ROWS_BEFORE_LEAST_RECENTLY_USED_EVICTION = 100_000
# Model identity: the repo is pinned to an exact HuggingFace revision (commit hash) so a
# compromised/renamed upstream repo cannot silently change what ships onto end-user machines.
QWEN_MODEL_HUGGINGFACE_REPO_ID = "Qwen/Qwen3-Embedding-0.6B"
QWEN_MODEL_PINNED_HUGGINGFACE_REVISION_COMMIT_HASH = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"  # repo 'main' as of 2026-07
# Written to every cache row and filtered on read, so a future model change cannot serve stale vectors.
QWEN_MODEL_CACHE_VERSION_TAG = "qwen3-0.6b"
# Optional inference device override, e.g. "cpu", "cuda", "cuda:0", "mps". When unset,
# sentence-transformers auto-selects; the chosen device is logged either way.
QWEN_EMBEDDING_DEVICE_ENVIRONMENT_VARIABLE_NAME = "QWEN_EMBEDDING_DEVICE"
# Multi-machine support: TOOL_SUFFIX env var (when set) distinguishes same-named tools across servers.
TOOL_NAME = f"qwen_embedding_0_6b{os.environ.get('TOOL_SUFFIX','')}"

# Global variables for lazy loading
_sentence_transformers = None
_model = None
# Serializes first-use lazy loading of the sentence-transformers import and the ~1GB model
# (double-checked locking), so two concurrent first calls cannot both load the model at once.
_lazy_model_and_dependency_load_lock = threading.Lock()

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,  # suffix-aware (TOOL_SUFFIX env var), same pattern as other tools
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
                    "enum": ["readme", "generate", "clear_cache"],
                    "description": "Operation to perform"
                },
                "text": {
                    "type": "string", 
                    "description": "Text to generate embedding for (for generate operation, provide exactly one of 'text' or 'texts')"
                },
                "texts": {
                    "type": "array",
                    "description": "List of texts to embed in one batched generate call (max " + str(MAX_TEXTS_PER_BATCHED_GENERATE_CALL) + " texts; alternative to 'text'; returns one vector per text in the same order)"
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

3. For batched embedding generation (one model pass, much faster than repeated single calls):
   {
     "input": {
       "operation": "generate", 
       "texts": ["First text", "Second text"],
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

4. To clear the embedding cache (see Cache Privacy below):
   {
     "input": {
       "operation": "clear_cache",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Features
- Local model inference (no API calls required)
- Automatic model download on first use (pinned to a specific model revision, stored in the standard HuggingFace cache - HF_HOME or ~/.cache/huggingface - so an already-downloaded copy is reused)
- Batched generation via the 'texts' parameter (single model pass for many texts)
- Automatic local caching of embeddings (bounded size, least-recently-used eviction, clear_cache operation)
- Thread-safe concurrent access
- Exact text matching for cache hits
- Normalized (unit-length) output vectors, ready for cosine-distance math

## Model Details
- Model: Qwen/Qwen3-Embedding-0.6B (596M parameters), pinned to revision """ + QWEN_MODEL_PINNED_HUGGINGFACE_REVISION_COMMIT_HASH + """
- Dimensions: this tool always returns exactly 1024 dimensions (the underlying model's user-defined 32-1024 MRL dimensions are not exposed here)
- Languages: 100+ languages supported
- Performance: State-of-the-art multilingual embeddings
- Device: auto-selected (CPU/GPU); set the """ + QWEN_EMBEDDING_DEVICE_ENVIRONMENT_VARIABLE_NAME + """ environment variable (e.g. "cpu", "cuda", "cuda:0") to override

## Return Format
For 'text': returns a JSON array containing 1024 floating-point numbers representing the embedding vector.
For 'texts': returns a JSON array of such arrays, one per input text, in the same order.

## Cache Privacy
The embedding cache permanently stores the PLAINTEXT of every string it embeds (including
strings embedded internally, e.g. agent memories via the sqlite tool) in
qwen_embedding_0_6b_cache.db inside the app's user data directory. Everyone using this OS
account shares that file. Use the clear_cache operation to erase it (deletes every cached
row and reclaims the disk space), and consider excluding the file from backups if the
embedded text may be sensitive.

## Usage Notes
1. Include the tool_unlock_token in all subsequent operations
2. For generate, provide exactly one of 'text' (single string) or 'texts' (array of up to """ + str(MAX_TEXTS_PER_BATCHED_GENERATE_CALL) + """ strings)
3. Maximum text length: input longer than 100,000 characters is truncated to the first 100,000 characters before embedding (the model tokenizer then applies its own 32K-token context limit); this applies to each entry of 'texts' too
4. Results are automatically cached for identical input text (cache key is the post-truncation text); the cache is capped at """ + str(MAX_CACHE_ROWS_BEFORE_LEAST_RECENTLY_USED_EVICTION) + """ entries with least-recently-used eviction
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
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                # bool is a subclass of int in Python, so exclude it explicitly
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
    """Ensure sentence-transformers is available (no runtime installation).
    
    Returns:
        The sentence_transformers module
        
    Raises:
        RuntimeError: If the dependency is missing or import fails
    """
    global _sentence_transformers
    
    # Double-checked locking: fast path avoids the lock once loaded
    if _sentence_transformers is None:
        with _lazy_model_and_dependency_load_lock:
            if _sentence_transformers is None:
                try:
                    import sentence_transformers
                    _sentence_transformers = sentence_transformers
                    MCPLogger.log(TOOL_LOG_NAME, "sentence-transformers already available")
                except ImportError as e:
                    # No live pip install at runtime: report a clean, actionable error instead
                    raise RuntimeError(
                        f"Required dependency 'sentence-transformers' is not installed in this runtime "
                        f"({str(e)}). Please reinstall the MCP-Link server runtime to restore it."
                    )
    
    return _sentence_transformers


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


# Add a special type for our embedding results to enforce unpacking
EmbeddingResult = Tuple[Optional[List[float]], Optional[str]]

def get_model():
    """Get the Qwen embedding model, loading it if necessary.
    
    Returns:
        SentenceTransformer model instance
        
    Raises:
        RuntimeError: If model loading fails
    """
    global _model
    
    # Double-checked locking so concurrent first calls cannot both load the ~1GB model
    if _model is None:
        # Ensure sentence-transformers is available (takes/releases the same lock internally)
        sentence_transformers = ensure_sentence_transformers()
        with _lazy_model_and_dependency_load_lock:
            if _model is None:
                try:
                    MCPLogger.log(TOOL_LOG_NAME, "Loading Qwen3-Embedding-0.6B model (this may take a few minutes on first run)...")
                    
                    # Optional explicit inference device; None lets sentence-transformers auto-select
                    requested_inference_device = os.environ.get(QWEN_EMBEDDING_DEVICE_ENVIRONMENT_VARIABLE_NAME) or None
                    
                    # Load the Qwen embedding model: revision pinned so upstream repo changes cannot
                    # silently alter what end-user machines run. No cache_folder override: the model
                    # lives in the standard HuggingFace cache (HF_HOME or ~/.cache/huggingface), the
                    # same location llm.py uses, so a copy the user already has is reused instead of
                    # a duplicate ~1.2GB download into an app-private directory.
                    model_load_kwargs = dict(
                        revision=QWEN_MODEL_PINNED_HUGGINGFACE_REVISION_COMMIT_HASH,
                        device=requested_inference_device,
                    )
                    try:
                        # Offline-first: when the pinned snapshot is already cached this skips every
                        # HuggingFace metadata probe (those can hang for minutes on slow/blocked
                        # networks even though all files are on disk).
                        loaded_qwen_sentence_transformer_model = sentence_transformers.SentenceTransformer(
                            QWEN_MODEL_HUGGINGFACE_REPO_ID, local_files_only=True, **model_load_kwargs
                        )
                    except Exception as offline_load_error:
                        MCPLogger.log(TOOL_LOG_NAME, f"Local model cache incomplete ({offline_load_error}) - downloading pinned revision from HuggingFace...")
                        loaded_qwen_sentence_transformer_model = sentence_transformers.SentenceTransformer(
                            QWEN_MODEL_HUGGINGFACE_REPO_ID, local_files_only=False, **model_load_kwargs
                        )
                    _model = loaded_qwen_sentence_transformer_model
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Qwen3-Embedding-0.6B model loaded successfully on device '{loaded_qwen_sentence_transformer_model.device}' (requested: {requested_inference_device or 'auto'}, revision {QWEN_MODEL_PINNED_HUGGINGFACE_REVISION_COMMIT_HASH[:12]})")
                    
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
        # Fall back to the system temp directory - never a CWD-relative path, which would
        # scatter cache files into whatever directory the server happened to start from.
        return os.path.join(tempfile.gettempdir(), 'qwen_embedding_0_6b_cache.db')

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
    conn = None  # initialized before try so the finally cannot hit a NameError if connect fails
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get from cache; model_version filter ensures a future model change cannot serve stale vectors
        cursor.execute(
            "SELECT embedding FROM qwen_embeddings WHERE text = ? AND model_version = ?",
            (text, QWEN_MODEL_CACHE_VERSION_TAG)
        )
        row = cursor.fetchone()
        
        if row:
            stored_embedding_value = row[0]
            if isinstance(stored_embedding_value, bytes):
                # Current format: packed little-endian float32 BLOB (~4KB/row)
                cached_embedding_vector = list(struct.unpack(f'<{len(stored_embedding_value) // 4}f', stored_embedding_value))
            else:
                # Legacy format: JSON text written by earlier versions of this tool
                cached_embedding_vector = json.loads(stored_embedding_value)
            # Refresh recency so cache eviction removes least-recently-used rows first
            cursor.execute("UPDATE qwen_embeddings SET timestamp = CURRENT_TIMESTAMP WHERE text = ?", (text,))
            conn.commit()
            return cached_embedding_vector, None
        else:
            return None, "Not found in cache"
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to check cache: {e}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return None, f"Cache error: {str(e)}"
        
    finally:
        if conn: conn.close()

def store_in_cache(text: str, qwen_embedding_vector: List[float]) -> None:
    """Store embedding in cache for future use.
    
    Args:
        text: Text that was embedded
        qwen_embedding_vector: Generated embedding vector to store
    """
    conn = None  # initialized before try so the finally cannot hit a NameError if connect fails
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Store as a packed little-endian float32 BLOB (~4KB/row vs ~20KB as JSON text), tagged
        # with the model version so reads can reject rows generated by a different model.
        cursor.execute(
            "INSERT OR REPLACE INTO qwen_embeddings (text, embedding, model_version, dimensions) VALUES (?, ?, ?, ?)",
            (text, struct.pack(f'<{len(qwen_embedding_vector)}f', *qwen_embedding_vector), QWEN_MODEL_CACHE_VERSION_TAG, len(qwen_embedding_vector))
        )
        
        # Bound cache growth: evict the least-recently-used rows (hits refresh timestamps) past the cap
        cursor.execute("SELECT COUNT(*) FROM qwen_embeddings")
        cached_row_count = cursor.fetchone()[0]
        if cached_row_count > MAX_CACHE_ROWS_BEFORE_LEAST_RECENTLY_USED_EVICTION:
            row_count_to_evict = cached_row_count - MAX_CACHE_ROWS_BEFORE_LEAST_RECENTLY_USED_EVICTION
            cursor.execute(
                "DELETE FROM qwen_embeddings WHERE text IN (SELECT text FROM qwen_embeddings ORDER BY timestamp ASC LIMIT ?)",
                (row_count_to_evict,)
            )
            MCPLogger.log(TOOL_LOG_NAME, f"Cache exceeded {MAX_CACHE_ROWS_BEFORE_LEAST_RECENTLY_USED_EVICTION} rows - evicted {row_count_to_evict} least-recently-used entries")
        conn.commit()
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Failed to store in cache: {e}")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        
    finally:
        if conn: conn.close()

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
        # Get the model (auto-downloads model weights on first use; dependencies are NOT installed at runtime)
        model = get_model()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Generating embedding: text length={len(text)}")
        
        # Generate embedding using local model (normalize_embeddings=True guarantees unit-length
        # vectors for cheap cosine math downstream; the model pipeline normalizes anyway)
        embedding_result = model.encode([text], normalize_embeddings=True)
        
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
            # Loud: callers like the sqlite generate_embedding() SQL UDF convert this failure
            # into a silent NULL embedding on the row they were writing.
            MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg} - callers such as the sqlite generate_embedding() SQL UDF will store NULL instead of a vector for the affected row")
            return None, error_msg
            
    except Exception as e:
        error_msg = f"Failed to generate embedding: {str(e)}"
        # Loud: callers like the sqlite generate_embedding() SQL UDF convert this failure
        # into a silent NULL embedding on the row they were writing.
        MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg} - callers such as the sqlite generate_embedding() SQL UDF will store NULL instead of a vector for the affected row")
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return None, error_msg

def generate_embeddings_for_text_list(texts: List[str]) -> Tuple[Optional[List[List[float]]], Optional[str]]:
    """Generate embedding vectors for several texts in one batched model call.
    
    Cache hits are served per text; all cache misses are encoded in a single
    model.encode() batch (much faster than per-text calls), then cached individually.
    
    Args:
        texts: Input texts (each already truncated to the input cap by the caller)
        
    Returns:
        (embedding_vectors_in_input_order, None) if successful, (None, error_msg) if failed
        
    Note: Results must be unpacked: vectors, error = generate_embeddings_for_text_list(texts)
    """
    embedding_vectors_in_input_order: List[Optional[List[float]]] = [None] * len(texts)
    cache_miss_indices = []
    for input_index, text in enumerate(texts):
        cached_embedding_vector, _cache_miss_reason = get_cached_embedding(text)
        if cached_embedding_vector is not None:
            embedding_vectors_in_input_order[input_index] = cached_embedding_vector
        else:
            cache_miss_indices.append(input_index)
    
    MCPLogger.log(TOOL_LOG_NAME, f"Batch generate: {len(texts)} texts, {len(texts) - len(cache_miss_indices)} cache hits, {len(cache_miss_indices)} to encode")
    
    if cache_miss_indices:
        try:
            model = get_model()
            batch_encode_result = model.encode([texts[i] for i in cache_miss_indices], normalize_embeddings=True)
            for batch_position, input_index in enumerate(cache_miss_indices):
                encoded_row = batch_encode_result[batch_position]
                qwen_embedding_vector = encoded_row.tolist() if hasattr(encoded_row, 'tolist') else list(encoded_row)
                if len(qwen_embedding_vector) != 1024:
                    error_msg = f"Unexpected embedding dimension: {len(qwen_embedding_vector)} (expected 1024)"
                    MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg}")
                    return None, error_msg
                store_in_cache(texts[input_index], qwen_embedding_vector)
                embedding_vectors_in_input_order[input_index] = qwen_embedding_vector
        except Exception as e:
            error_msg = f"Failed to generate batch embeddings: {str(e)}"
            MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg}")
            MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
            return None, error_msg
    
    return embedding_vectors_in_input_order, None

def truncate_text_to_embedding_input_cap(text: str) -> str:
    """Truncate over-long input so a multi-MB string cannot block the worker for minutes
    (the model tokenizer would truncate to its 32K-token context anyway).
    """
    if len(text) > MAX_EMBEDDING_INPUT_CHARS_BEFORE_TRUNCATION:
        MCPLogger.log(TOOL_LOG_NAME, f"Input text length {len(text)} exceeds {MAX_EMBEDDING_INPUT_CHARS_BEFORE_TRUNCATION} character cap - truncating")
        return text[:MAX_EMBEDDING_INPUT_CHARS_BEFORE_TRUNCATION]
    return text

def handle_generate(params: Dict) -> Dict:
    """Handle embedding generation operation (single 'text' or batched 'texts').
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing either the embedding vector(s) or error information
    """
    try:
        # Exactly one of 'text' (single) or 'texts' (batch) must be provided
        text = params.get("text")
        texts = params.get("texts")
        if text is None and texts is None:
            return create_error_response("Provide 'text' (single string) or 'texts' (array of strings) for the generate operation.", with_readme=True)
        if text is not None and texts is not None:
            return create_error_response("Provide only one of 'text' or 'texts', not both.", with_readme=True)
        
        if texts is not None:
            # Batched generation path
            if len(texts) == 0:
                return create_error_response("Parameter 'texts' must be a non-empty array of strings.", with_readme=True)
            if len(texts) > MAX_TEXTS_PER_BATCHED_GENERATE_CALL:
                return create_error_response(f"Parameter 'texts' accepts at most {MAX_TEXTS_PER_BATCHED_GENERATE_CALL} entries per call, got {len(texts)}.", with_readme=True)
            for input_index, batch_text in enumerate(texts):
                if not isinstance(batch_text, str):
                    return create_error_response(f"texts[{input_index}] must be a string, got {type(batch_text).__name__}. Every entry of 'texts' must be a string.", with_readme=True)
            capped_texts = [truncate_text_to_embedding_input_cap(batch_text) for batch_text in texts]
            
            MCPLogger.log(TOOL_LOG_NAME, f"Processing batched embedding generation request: {len(capped_texts)} texts")
            
            embedding_vectors, error = generate_embeddings_for_text_list(capped_texts)
            if embedding_vectors is not None:
                return {
                    "content": [{"type": "text", "text": json.dumps(embedding_vectors)}],
                    "isError": False
                }
            else:
                return create_error_response(f"Failed to generate batch embeddings: {error}", with_readme=True)
        
        if not isinstance(text, str):
            return create_error_response(f"Parameter 'text' must be a string, got {type(text).__name__}. Please provide a string value to generate embedding for.", with_readme=True)
        
        # Cap input length so a multi-MB string cannot block the worker for minutes
        text = truncate_text_to_embedding_input_cap(text)
        
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

def handle_clear_cache(params: Dict) -> Dict:
    """Handle the clear_cache operation: delete every cached embedding row and reclaim disk space.
    
    The cache stores the plaintext of every embedded string (see readme Cache Privacy),
    so this doubles as the privacy 'forget everything' operation.
    
    Args:
        params: Dictionary containing the operation parameters (none used beyond the token)
        
    Returns:
        Dict containing the number of rows cleared, or error information
    """
    conn = None  # initialized before try so the finally cannot hit a NameError if connect fails
    try:
        db_path = get_cache_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM qwen_embeddings")
        cleared_row_count = cursor.fetchone()[0]
        cursor.execute("DELETE FROM qwen_embeddings")
        conn.commit()
        cursor.execute("VACUUM")  # reclaim the file space held by the deleted rows
        MCPLogger.log(TOOL_LOG_NAME, f"clear_cache removed {cleared_row_count} cached embeddings")
        return {
            "content": [{"type": "text", "text": json.dumps({"cleared_rows": cleared_row_count})}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Failed to clear embedding cache: {str(e)}", with_readme=True)
    finally:
        if conn: conn.close()

def handle_qwen_embedding_0_6b(input_param: Dict) -> Dict:
    """Handle qwen embedding tool operations via MCP interface."""
    try:
        # Read synthetic handler_info (added by the server for dynamic routing) via .get on a
        # shallow copy, so the caller's original dict is never mutated; drop it before validation.
        input_param = dict(input_param)
        handler_info = input_param.get('handler_info', None)
        input_param.pop('handler_info', None)
        
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
        elif operation == "clear_cache":
            return handle_clear_cache(validated_params)
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

# Map of tool names to their handlers (keyed on suffix-aware TOOL_NAME)
HANDLERS = {
    TOOL_NAME: handle_qwen_embedding_0_6b
}

def initialize_tool() -> None:
    """Initialize the tool - called once when server starts."""
    # Product policy: nothing may be downloaded or loaded until a user actually invokes an
    # embedding operation, so users who never use this feature never pay the ~1.2GB model
    # download. The startup background pre-warm thread that used to live here was removed
    # for that reason; the trade-off is that on a fresh install the very first embedding
    # call (e.g. the first agent memory insert via the sqlite generate_embedding() UDF)
    # pays the full model download and may hit the tool-call timeout - it can simply be
    # retried, since the download resumes/completes in the HuggingFace cache.
    setup_cache_db()
