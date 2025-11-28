"""
File: ragtag/tools/llm.py
Project: Aura Friday MCP-Link Server
Component: Local LLM Inference Tool with Tool-Calling
Author: Christopher Nathan Drake (cnd)

Unified tool for local LLM inference, chat completions, embeddings, and autonomous tool-calling.
Provides offline AI capabilities with full MCP ecosystem integration.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƱωĐDƵꓮnᴜᒿ𝐴ᗪ𝟙𝟙ȜɌƻһᴍꓓOКᴡΗrᏂ𐓒𝟑𝟫𐐕ꞇıƙ0CКе𝘈i𝟥v6ΚOwɌꓜƽᗷɗΥωŪᏂС𝛢ⲞꜱᗪYОŧᖴօꓳFƿ𝟚Ƶ7𝟛ТnƼոENμΡÐꙅᏎꜱхꓰj𝟨fᎻр𐐕𝐴e𝟛sΟ0ᗞƌ3ᴡ4τꓴᴅďL𝕌ωȣ"
"signdate": "2025-11-28T02:13:56.779Z",

═══════════════════════════════════════════════════════════════════════════════
                        LLM TOOL - RESEARCH & ARCHITECTURE
═══════════════════════════════════════════════════════════════════════════════

RESEARCH FINDINGS & ARCHITECTURAL DECISIONS
═══════════════════════════════════════════════════════════════════════════════

## 🎯 CORE OBJECTIVES
- Provide fully offline/local LLM inference (no cloud dependencies)
- Enable autonomous AI agents via tool-calling integration with MCP ecosystem
- Standardize interface compatibility with existing openrouter tool
- Support multi-platform deployment (Windows/Linux/Mac with CUDA/CPU fallbacks)
- Auto-optimize for available hardware (GPU memory, quantization, device selection)

## 🏗️ ARCHITECTURAL DECISIONS

### Single Backend Choice: Transformers (PyTorch)
DECISION: Use ONLY Transformers library, no pluggable backends
REASONING:
- ✅ Already proven in test_torch_import.py (0.5B-7B models working)
- ✅ Native tool-calling via apply_chat_template(tools=...) 
- ✅ Cross-platform compatibility (Windows/Linux/Mac)
- ✅ Easy installation vs TensorRT-LLM complexity
- ✅ Better Python integration than llama.cpp
- ✅ Can upgrade to TensorRT-LLM later IF performance becomes critical
- ✅ Consistent with existing codebase patterns

### Unified Tool Approach (vs Separate LLM + Embedding Tools)
DECISION: Single llm.py tool handling both completions AND embeddings
REASONING:
- ✅ Shared infrastructure (hardware detection, device management, model caching)
- ✅ Consistent UX (one tool to learn, single security token)
- ✅ Memory efficiency (avoid loading similar models twice)
- ✅ Future-proof (some models handle both completions AND embeddings)
- ✅ Code reuse (eliminate duplication between separate tools)

### OpenRouter Interface Standardization
DECISION: Make llm.chat_completion compatible with openrouter.chat_completion
REASONING:
- ✅ Easy switching between cloud (OpenRouter) and local (LLM) inference
- ✅ Consistent message format (OpenAI-style) across tools
- ✅ Shared tool/function schema format
- ✅ Reduces learning curve for users
- ✅ Enables hybrid workflows (cloud + local)

STANDARDIZED OPERATION NAMING (Phase 1 Decisions):
- ✅ list_installed_models (NOT list_models - reserved for future Hub search)
- ✅ list_available_models (OpenRouter's cloud models listing)
- ✅ search_models (semantic search in OpenRouter's cached model database)
- ✅ chat_completion (shared interface between local and cloud)
- ✅ Model paths use HuggingFace format: "organization/model-name"

## 📚 LIBRARY CAPABILITIES ANALYSIS

### Features ALREADY Built-in (Just Call Them):
- ✅ Text completion: model.generate(input_ids, max_new_tokens=100)
- ✅ Chat templates: tokenizer.apply_chat_template(messages, add_generation_prompt=True)
- ✅ Streaming: TextIteratorStreamer + threading
- ✅ Batch processing: model.generate() handles batches natively
- ✅ Device management: device_map="auto" handles multi-GPU automatically
- ✅ Memory optimization: torch_dtype="auto", attn_implementation="sdpa"
- ✅ Function calling: Many models support tool schemas (Qwen, Llama, etc.)
- ✅ Structured output: Built into generation with guided decoding
- ✅ Multi-modal: LLaVA, Qwen-VL, PaliGemma, Chameleon support images
- ✅ Quantization: BitsAndBytesConfig, TorchAoConfig for 4-bit/8-bit/int8
- ✅ ONNX export: export_optimized_onnx_model() with optimization levels
- ✅ Dense embeddings: sentence_transformers.encode()
- ✅ Sparse embeddings: SparseEncoder.encode()
- ✅ Similarity calculations: model.similarity(embeddings1, embeddings2)
- ✅ CUDA detection: torch.cuda.is_available(), torch.cuda.get_device_name()
- ✅ Memory monitoring: torch.cuda.memory_allocated(), torch.cuda.memory_reserved()

### Features Requiring Light Wrapper Logic:
- 📦 Auto-download: Libraries handle this, need user-friendly interface
- 📦 Model info: Available via model.config, need unified format
- 📦 Cache management: Files go to ~/.cache/huggingface/, need cleanup logic
- 🛡️ Graceful fallbacks: CPU when GPU fails, smaller models when OOM
- 🛡️ Dependency installation: Like embedding tool's ensure_sentence_transformers()
- 🛡️ Validation: Parameter checking, model compatibility
- 📊 Benchmarking: Wrap model calls with timing, memory tracking
- 📊 Token counting: Use tokenizer.encode() for cost estimation
- 📊 Auto-selection: Logic to pick model based on task complexity

### Features We Must Build from Scratch:
- 🔧 Tool calling integration: Let LLMs call other MCP tools
- 🔧 Conversation persistence: Save/restore chat history
- 🔧 Multi-step reasoning: Chain completions with memory
- 🔧 Self-correction loops: Validate outputs, retry with feedback
- 🎨 Template system: Reusable prompts with variables
- 🎨 Output format validation: Ensure JSON/YAML/XML is valid
- 🎨 Context management: Smart truncation, summarization
- 🎨 A/B testing: Compare model outputs side-by-side
- ⚙️ MCP security patterns: tool_unlock_token, parameter validation
- ⚙️ Caching strategy: SQLite-based like embedding tool
- ⚙️ Configuration: Model preferences, hardware settings
- ⚙️ Monitoring: Usage stats, performance metrics

## 🌐 MODEL ECOSYSTEM & SOURCES

### Primary Source: Hugging Face Hub
Canonical format: "organization/model-name"
Examples:
- "microsoft/DialoGPT-medium"
- "meta-llama/Llama-2-7b-chat-hf" 
- "Qwen/Qwen2.5-7B-Instruct"
- "google/gemma-2-2b-it"

### Model Categories by Size:
Text Generation:
- Small (0.5-3B): Qwen2.5-0.5B, Phi-3-mini, Gemma-2-2B
- Medium (7-13B): Llama-3.1-8B, Qwen2.5-7B, Mistral-7B
- Large (30B+): Llama-3.1-70B, Qwen2.5-32B

Multi-modal:
- "llava-hf/llava-1.5-7b-hf" (vision + text)
- "Qwen/Qwen2-VL-7B-Instruct" (vision + text)
- "microsoft/kosmos-2-patch14-224" (vision + text)

Embeddings:
- "sentence-transformers/all-MiniLM-L6-v2" (dense)
- "naver/splade-cocondenser-ensembledistil" (sparse)

### Model Discovery & Metadata:
- Hub API: https://huggingface.co/api/models?filter=task:text-generation
- Local cache locations (platform-specific):
  * Linux/Mac: ~/.cache/huggingface/hub/ (symlinks to blobs)
  * Windows: %USERPROFILE%\.cache\huggingface\hub\
  * Environment override: HF_HOME environment variable
- Model cards: Each model has metadata, usage examples, limitations
- We scan the cache to discover installed models (no redundant storage)

## 🔧 TOOL-CALLING ARCHITECTURE

### Supported Models with Native Tool-Calling:
- Qwen2.5-Instruct family: Supports function/tool calling with HF chat template
- Llama-3.x Instruct: Tool-calling supported in local runtimes
- Both use OpenAI-style function/tool schema format

### Tool-Call Execution Flow:
1. User calls llm.chat_completion with tools array (OpenAI format)
2. Model generates tool_calls in response
3. llm tool validates arguments against JSON schema
4. llm tool invokes corresponding MCP tools automatically
5. Results appended to conversation as tool messages
6. Model continues with tool results to generate final response

### Auto-Execution vs Manual Modes:
- auto_execute_tools=true: Autonomous agent mode (tools run automatically)
- auto_execute_tools=false: Return tool_calls for user approval (safer default)

### Constrained Decoding (Outlines Library):
- Enforces valid JSON schema for tool calls
- Eliminates parsing errors and malformed outputs
- Guarantees tool calls match expected format

## 📋 OPERATION SPECIFICATIONS

### Core Operations (Phase 1 - MVP):
1. "readme" - Get documentation + unlock token
2. "list_installed_models" - Show cached models (NOT "list_models" - reserved for future downloadable search)
3. "install_model" - Download from HuggingFace Hub
4. "model_info" - Get model details and capabilities
5. "chat_completion" - STANDARDIZED with openrouter interface
6. "embed" - Text embeddings (dense/sparse)
7. "hardware_info" - GPU/CPU capabilities and memory

### Enhanced Operations (Phase 2):
8. "stream_completion" - Real-time token streaming
9. "batch_completion" - Multiple prompts efficiently
10. "benchmark_model" - Performance testing
11. "uninstall_model" - Remove from cache
12. "similarity_search" - Find most similar texts

### Advanced Operations (Phase 3):
13. "search_available_models" - Search HuggingFace Hub (like openrouter)
14. "validate_output" - Check format compliance
15. "conversation_memory" - Persistent chat sessions
16. "template_completion" - Reusable prompt templates

## 🎯 STANDARDIZED INTERFACES

### chat_completion Interface (Compatible with OpenRouter):
{
    "input": {
        "operation": "chat_completion",
        "tool_unlock_token": "...",
        
        // SAME as openrouter:
        "model": "Qwen/Qwen2.5-7B-Instruct",  // Local model path
        "messages": [...],                     // OpenAI format
        "stream": false,                       // Streaming support
        "tools": [...],                        // Function definitions
        "tool_choice": "auto",                 // Tool selection mode
        "temperature": 0.7,
        "max_tokens": 1000,
        "top_p": 0.9,
        
        // LLM-specific (not in openrouter):
        "auto_execute_tools": false,           // Safety default
        "device": "auto",                      // cuda/cpu/auto
        "quantization": "auto"                 // 4bit/8bit/none/auto
    }
}

### Message Format (OpenAI-Compatible):
[
    {"role": "system", "content": "You are a helpful assistant"},
    {"role": "user", "content": "Hello!"},
    {"role": "assistant", "content": "Hi there!"},
    {"role": "tool", "content": "{...}", "name": "tool_name", "tool_call_id": "call_1"}
]

### Tool Schema Format (OpenAI-Compatible):
[
    {
        "type": "function",
        "function": {
            "name": "user_popup",
            "description": "Show a popup to the user",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to display"}
                },
                "required": ["text"]
            }
        }
    }
]

## 🔧 TECHNICAL IMPLEMENTATION DETAILS

### Dependencies:
Core (already have most):
- transformers>=4.51.0 (tool-calling support)
- torch>=2.0.0 (CUDA support)
- accelerate (multi-GPU)
- sentence-transformers (embeddings - already have)

NEW for tool calling:
- outlines>=0.1.0 (constrained decoding)
- pydantic>=2.0.0 (schema validation)

Optional performance:
- bitsandbytes (quantization - already installed)

### Hardware Management Strategy:
- Auto-detect CUDA availability and GPU memory
- Auto-select quantization based on VRAM vs model size
- Graceful fallback to CPU when GPU unavailable
- Memory monitoring and cleanup between operations
- Device placement with device_map="auto"

### Model Loading Strategy:
- Lazy loading (load on first use)
- In-memory caching for frequently used models
- Auto-quantization based on available VRAM
- Cleanup and memory management
- Support for local and Hub models

### Caching Strategy:
- SQLite database for conversation history (following MCP patterns)
- Model files cached by HuggingFace in ~/.cache/huggingface/
- Embedding caching (reuse from qwen_embedding_06.py patterns)
- Configuration and preferences storage

### Error Handling & Recovery:
- Graceful fallbacks (CPU when GPU fails, smaller models when OOM)
- Dependency auto-installation (like ensure_sentence_transformers())
- Comprehensive parameter validation with helpful error messages
- Tool-call validation and schema enforcement
- Retry logic for transient failures

## 🚀 AUTONOMOUS AGENT CAPABILITIES

### MCP Tool Integration:
- Automatic discovery of available MCP tools
- Schema translation from MCP to OpenAI format
- Secure tool execution with validation
- Inter-tool authentication tokens
- Tool call logging and monitoring

### Example Autonomous Workflows:
1. Database Analysis:
   - User: "Analyze user behavior in the database"
   - LLM calls sqlite tool to query data
   - LLM analyzes results and provides insights

2. Research & Reporting:
   - User: "Research AI news and create summary"
   - LLM calls browser tool to search web
   - LLM calls user tool to show popup with summary

3. System Administration:
   - User: "Check system health and optimize"
   - LLM calls system tool for diagnostics
   - LLM suggests and executes optimizations

### Safety & Governance:
- Tool allow-lists and permissions
- Argument schema validation
- Rate limiting and resource management
- User confirmation for risky operations
- Comprehensive logging and audit trails

## 📊 PERFORMANCE OPTIMIZATION

### Memory Management:
- Auto-quantization (4-bit/8-bit) based on VRAM
- Model unloading and cleanup
- Batch processing for efficiency
- Memory monitoring and alerts

### Generation Optimization:
- Constrained decoding for structured output
- Streaming for real-time responses
- Caching for repeated operations
- Device optimization (CUDA/CPU)

### Benchmarking & Monitoring:
- Token generation speed (tokens/second)
- Memory usage tracking (VRAM/RAM)
- Model loading times
- Tool execution performance
- Error rates and success metrics

## 🎨 USER EXPERIENCE FEATURES

### Model Management:
- Easy installation from HuggingFace Hub
- Model size and compatibility checking
- Auto-selection based on task complexity
- Cleanup and storage management

### Conversation Features:
- Persistent chat sessions
- Context management and truncation
- Template system for reusable prompts
- Multi-turn reasoning with memory

### Output Formatting:
- JSON/YAML/XML structured output
- Format validation and correction
- Custom output schemas
- Pretty-printing and display options

═══════════════════════════════════════════════════════════════════════════════
                              IMPLEMENTATION PLAN
═══════════════════════════════════════════════════════════════════════════════

## Phase 1: Core Foundation (MVP)
✅ Tool template with MCP patterns (tool_unlock_token, validation, error handling)
✅ Hardware detection (CUDA/CPU) with fallback logic  
✅ Basic model loading (transformers + sentence-transformers)
✅ Simple chat_completion operation (compatible with openrouter)
✅ Simple embed operation
✅ Model caching and cleanup
✅ list_installed_models, install_model, model_info operations

## Phase 2: Enhanced Features  
🚀 Tool-calling integration with MCP ecosystem
🚀 Streaming completions with TextIteratorStreamer
🚀 Multi-model support with auto-selection
🚀 Quantization for memory efficiency
🚀 Batch processing capabilities
🚀 Hardware monitoring and optimization
🚀 Constrained decoding with Outlines

## Phase 3: Advanced Capabilities
🤖 Autonomous agent mode (auto_execute_tools=true)
🤖 Multimodal support (text + images)
🤖 Persistent conversation memory
🤖 Self-correction and validation
🤖 Advanced prompt templates
🤖 Performance benchmarking and A/B testing

═══════════════════════════════════════════════════════════════════════════════
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
from . import mcp_bridge
YEL = '\033[33;1m'
NORM = '\033[0m'

# Constants
TOOL_LOG_NAME = "LLM"

# Global variables for lazy loading
_torch = None
_transformers = None
_loaded_models = {}  # Cache for loaded models: {model_name: (model, tokenizer, device)}

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "llm",
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        # - Supports chat completions compatible with OpenRouter interface
        # - Future: embeddings, tool-calling, multi-modal
        # - Use {\"input\":{\"operation\":\"readme\"}} to get full documentation
        "description": """Local LLM inference tool for chat completions and embeddings.
- Use this when you need local (offline) AI inference without cloud API calls
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
                    "enum": ["readme", "hardware_info", "list_installed_models", "model_info", "chat_completion"],
                    "description": "Operation to perform"
                },
                "model": {
                    "type": "string",
                    "description": "Model identifier (HuggingFace format: 'organization/model-name')"
                },
                "messages": {
                    "type": "array",
                    "description": "Array of message objects (OpenAI format)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
                            "content": {"type": "string"}
                        },
                        "required": ["role", "content"]
                    }
                },
                "temperature": {
                    "type": "number",
                    "description": "Control randomness (0.0-2.0)",
                    "minimum": 0.0,
                    "maximum": 2.0,
                    "default": 0.7
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum tokens to generate",
                    "minimum": 1,
                    "default": 1000
                },
                "top_p": {
                    "type": "number",
                    "description": "Nucleus sampling (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.9
                },
                "device": {
                    "type": "string",
                    "description": "Device to use: 'auto', 'cuda', or 'cpu'",
                    "enum": ["auto", "cuda", "cpu"],
                    "default": "auto"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """
Local LLM Inference Tool - Fully Offline AI with OpenRouter-Compatible Interface

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Input Structure
All parameters are passed in a single 'input' dict:

{
  "input": {
    "operation": "operation_name",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
    ...other parameters...
  }
}

## Operations (Phase 1 MVP)

### 1. readme - Get documentation
{
  "input": {"operation": "readme"}
}

### 2. hardware_info - Check GPU/CPU capabilities
{
  "input": {
    "operation": "hardware_info",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

Returns: CUDA availability, GPU name, memory, device recommendations

### 3. list_installed_models - Show cached HuggingFace models
{
  "input": {
    "operation": "list_installed_models",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

Returns: List of models found in ~/.cache/huggingface/hub/ (or platform equivalent)

### 4. model_info - Get model details
{
  "input": {
    "operation": "model_info",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

Returns: Model parameters, memory requirements, capabilities

### 5. chat_completion - OpenRouter-compatible chat (MVP FOCUS)
{
  "input": {
    "operation": "chat_completion",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant"},
      {"role": "user", "content": "Hello!"}
    ],
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """,
    
    // Optional parameters (same as OpenRouter):
    "temperature": 0.7,
    "max_tokens": 1000,
    "top_p": 0.9,
    
    // LLM-specific:
    "device": "auto"  // "auto", "cuda", or "cpu"
  }
}

Returns: OpenAI-format response with generated text

## Features
- ✅ Fully offline/local inference (no API calls)
- ✅ Automatic CUDA/CPU detection and fallback
- ✅ Auto-download models from HuggingFace Hub
- ✅ OpenRouter interface compatibility
- ✅ Multi-platform (Windows/Linux/Mac)
- ✅ Uses standard HuggingFace cache (no duplication)

## Model Storage
Models are stored in platform-specific locations:
- Linux/Mac: ~/.cache/huggingface/hub/
- Windows: %USERPROFILE%\\.cache\\huggingface\\hub\\
- Override: Set HF_HOME environment variable

We scan this cache to discover installed models - no separate storage needed!

## Usage Notes
1. First model load may take time (download + caching)
2. GPU recommended for 3B+ models
3. Temperature controls randomness (0.0=deterministic, 2.0=creative)
4. Device "auto" selects CUDA if available, falls back to CPU

## Example: Simple Chat
{
  "input": {
    "operation": "chat_completion",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "max_tokens": 50,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}

## Supported Models (Tested)
- Qwen/Qwen2.5-0.5B-Instruct (500M params, ~1GB VRAM)
- Qwen/Qwen2.5-1.5B-Instruct (1.5B params, ~3GB VRAM)
- Qwen/Qwen2.5-3B-Instruct (3B params, ~6GB VRAM)
- Qwen/Qwen2.5-7B-Instruct (7B params, ~14GB VRAM)

More models will work - these are just tested examples.

## Future Operations (Coming Soon)
- embed: Text embeddings generation
- Tool-calling: Autonomous MCP tool invocation
- Streaming: Real-time token streaming
- Multi-modal: Image + text processing
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

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

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

def ensure_torch():
    """Ensure PyTorch is available, with helpful error if not.
    Auto-installs CUDA version if CUDA hardware is detected and only CPU version is installed.
    
    Returns:
        The torch module
        
    Raises:
        RuntimeError: If torch is not available with installation instructions
    """
    global _torch
    
    if _torch is None:
        try:
            import torch
            _torch = torch
            MCPLogger.log(TOOL_LOG_NAME, f"PyTorch {torch.__version__} loaded successfully")
            
            # Check if we have CPU-only torch but CUDA hardware is available
            # Feature 1: Detect if CUDA hardware is available (check via nvidia-smi or similar)
            # Feature 2: Check if current torch is CPU-only
            if '+cpu' in torch.__version__ or not (hasattr(torch, 'cuda') and torch.cuda.is_available()):
                # Check if CUDA hardware exists by trying to detect NVIDIA GPU
                cuda_hardware_available = False
                try:
                  # Try to detect NVIDIA GPU without relying on torch.cuda
                  import subprocess
                  import platform
                  
                  if platform.system() == "Windows":
                    # Windows: check via wmic
                    MCPLogger.log(TOOL_LOG_NAME, "Detecting NVIDIA GPU via wmic on Windows")
                    result = subprocess.run(
                      ['wmic', 'path', 'win32_VideoController', 'get', 'name'],
                      capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if result.returncode == 0 and 'NVIDIA' in result.stdout:
                      cuda_hardware_available = True
                  else:
                    # Linux/Mac: check via lspci or system_profiler
                    if platform.system() == "Linux":
                      MCPLogger.log(TOOL_LOG_NAME, "Detecting NVIDIA GPU via lspci on Linux")
                      result = subprocess.run(
                        ['lspci'], capture_output=True, text=True, timeout=5
                      )
                      if result.returncode == 0 and 'NVIDIA' in result.stdout:
                        cuda_hardware_available = True
                    elif platform.system() == "Darwin":
                      # Mac: NVIDIA GPUs are not supported on modern Macs, skip
                      cuda_hardware_available = False
                except Exception as e:
                  MCPLogger.log(TOOL_LOG_NAME, f"Could not detect GPU hardware: {str(e)}")
                  cuda_hardware_available = False
                
                # Feature 3: Auto-install CUDA-enabled torch if hardware is available
                if cuda_hardware_available and '+cpu' in torch.__version__:
                  MCPLogger.log(TOOL_LOG_NAME, "NVIDIA GPU detected but CPU-only PyTorch installed. Upgrading to CUDA version...")
                  try:
                    import pip
                    # Uninstall CPU version first
                    MCPLogger.log(TOOL_LOG_NAME, "Uninstalling CPU-only PyTorch...")
                    pip.main(['uninstall', '-y', 'torch', 'torchvision', 'torchaudio'])
                    
                    # Install CUDA version (using cu118 for better compatibility)
                    MCPLogger.log(TOOL_LOG_NAME, "Installing CUDA-enabled PyTorch (this may take several minutes)...")
                    result = pip.main([
                      'install', 
                      'torch', 
                      'torchvision', 
                      'torchaudio',
                      '--index-url', 
                      'https://download.pytorch.org/whl/cu118'
                    ])
                    
                    if result != 0:
                      raise RuntimeError(f"pip failed with exit code {result}")
                    
                    MCPLogger.log(TOOL_LOG_NAME, "CUDA PyTorch installed successfully! Restarting server to load new libraries...")
                    
                    # Trigger server restart
                    try:
                      mcp_bridge.call("server_control", {
                        "operation": "restart",
                        "wait": 2
                      })
                    except Exception as restart_error:
                      MCPLogger.log(TOOL_LOG_NAME, f"Failed to trigger server restart: {str(restart_error)}")
                    
                    # Raise special exception to signal that restart is needed
                    raise RuntimeError("CUDA_INSTALL_SUCCESS_RESTART_REQUIRED")
                      
                  except RuntimeError as e:
                    if str(e) == "CUDA_INSTALL_SUCCESS_RESTART_REQUIRED":
                      # Re-raise to be caught by caller
                      raise
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to auto-install CUDA PyTorch: {str(e)}. Continuing with CPU version.")
                    # Continue with CPU version - don't fail
                  except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Failed to auto-install CUDA PyTorch: {str(e)}. Continuing with CPU version.")
                    # Continue with CPU version - don't fail
                    
        except ImportError:
            # Feature 3: Auto-install PyTorch if not present
            MCPLogger.log(TOOL_LOG_NAME, "PyTorch not found, attempting auto-installation...")
            try:
              import pip
              
              # Check if CUDA hardware is available before deciding which version to install
              cuda_hardware_available = False
              try:
                import subprocess
                import platform
                
                if platform.system() == "Windows":
                  MCPLogger.log(TOOL_LOG_NAME, "Detecting NVIDIA GPU via wmic on Windows (for install decision)")
                  result = subprocess.run(
                    ['wmic', 'path', 'win32_VideoController', 'get', 'name'],
                    capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
                  )
                  if result.returncode == 0 and 'NVIDIA' in result.stdout:
                    cuda_hardware_available = True
                else:
                  if platform.system() == "Linux":
                    MCPLogger.log(TOOL_LOG_NAME, "Detecting NVIDIA GPU via lspci on Linux (for install decision)")
                    result = subprocess.run(
                      ['lspci'], capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0 and 'NVIDIA' in result.stdout:
                      cuda_hardware_available = True
              except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Could not detect GPU hardware: {str(e)}")
              
              # Install appropriate version
              if cuda_hardware_available:
                MCPLogger.log(TOOL_LOG_NAME, "NVIDIA GPU detected. Installing CUDA-enabled PyTorch...")
                result = pip.main([
                  'install', 
                  'torch', 
                  'torchvision', 
                  'torchaudio',
                  '--index-url', 
                  'https://download.pytorch.org/whl/cu118'
                ])
                
                if result != 0:
                  raise RuntimeError(f"pip failed with exit code {result}")
                
                MCPLogger.log(TOOL_LOG_NAME, "CUDA PyTorch installed successfully! Restarting server to load new libraries...")
                
                # Trigger server restart
                try:
                  mcp_bridge.call("server_control", {
                    "operation": "restart",
                    "wait": 2
                  })
                except Exception as restart_error:
                  MCPLogger.log(TOOL_LOG_NAME, f"Failed to trigger server restart: {str(restart_error)}")
                
                # Raise special exception to signal that restart is needed
                raise RuntimeError("CUDA_INSTALL_SUCCESS_RESTART_REQUIRED")
              else:
                MCPLogger.log(TOOL_LOG_NAME, "No NVIDIA GPU detected. Installing CPU-only PyTorch...")
                result = pip.main(['install', 'torch', 'torchvision', 'torchaudio'])
                
                if result != 0:
                  raise RuntimeError(f"pip failed with exit code {result}")
                
                import torch
                _torch = torch
                MCPLogger.log(TOOL_LOG_NAME, f"PyTorch {torch.__version__} installed successfully")
              
            except Exception as e:
              error_msg = f"""Failed to auto-install PyTorch: {str(e)}
              
Please install manually with:

For CUDA support (recommended if you have NVIDIA GPU):
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

For CPU only:
pip install torch torchvision torchaudio
"""
              raise RuntimeError(error_msg)
    
    return _torch

def ensure_transformers():
    """Ensure transformers library is available.
    
    Returns:
        The transformers module
        
    Raises:
        RuntimeError: If transformers is not available
    """
    global _transformers
    
    if _transformers is None:
        try:
            import transformers
            _transformers = transformers
            MCPLogger.log(TOOL_LOG_NAME, f"Transformers loaded successfully")
        except ImportError:
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Transformers not found, installing...")
                result = pip.main(['install', 'transformers>=4.51.0', 'accelerate'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                import transformers
                _transformers = transformers
                MCPLogger.log(TOOL_LOG_NAME, "Transformers installed successfully")
            except Exception as e:
                raise RuntimeError(f"Failed to install transformers: {str(e)}")
    
    return _transformers

def ensure_accelerate():
    """Ensure accelerate library is available (required for GPU device_map).
    
    Raises:
        RuntimeError: If accelerate cannot be installed
    """
    try:
        import accelerate
        MCPLogger.log(TOOL_LOG_NAME, "Accelerate library available")
    except ImportError:
        try:
            import pip
            MCPLogger.log(TOOL_LOG_NAME, "Accelerate not found, installing...")
            result = pip.main(['install', 'accelerate'])
            
            if result != 0:
                raise RuntimeError(f"pip failed with exit code {result}")
            
            MCPLogger.log(TOOL_LOG_NAME, "Accelerate installed successfully")
        except Exception as e:
            raise RuntimeError(f"Failed to install accelerate: {str(e)}")

def get_hardware_info() -> Dict[str, Any]:
    """Get hardware capabilities (CUDA/CPU) - based on test_torch_import.py.
    
    Returns:
        Dict with hardware information including CUDA availability, GPU name, memory
    """
    try:
        torch = ensure_torch()
        
        info = {
            "torch_version": torch.__version__,
            "cuda_available": False,
            "device": "cpu",
            "gpu_name": None,
            "gpu_memory_gb": None,
            "recommended_device": "cpu"
        }
        
        # Check CUDA availability (from test_torch_import.py pattern)
        if hasattr(torch, 'cuda') and torch.cuda.is_available():
            info["cuda_available"] = True
            info["device"] = "cuda"
            info["recommended_device"] = "cuda"
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["gpu_memory_gb"] = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            
            MCPLogger.log(TOOL_LOG_NAME, f"CUDA detected: {info['gpu_name']} ({info['gpu_memory_gb']:.1f} GB)")
        else:
            MCPLogger.log(TOOL_LOG_NAME, "CUDA not available, using CPU")
        
        return info
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error detecting hardware: {str(e)}")
        return {
            "error": str(e),
            "device": "cpu",
            "recommended_device": "cpu"
        }

def get_huggingface_cache_dir() -> Path:
    """Get the HuggingFace cache directory (platform-specific).
    
    Returns:
        Path to HuggingFace cache directory
    """
    # Check environment variable first
    hf_home = os.environ.get('HF_HOME')
    if hf_home:
        return Path(hf_home) / 'hub'
    
    # Platform-specific defaults
    home = Path.home()
    cache_dir = home / '.cache' / 'huggingface' / 'hub'
    
    return cache_dir

def list_installed_models() -> List[Dict[str, Any]]:
    """Scan HuggingFace cache for installed models.
    
    Returns:
        List of dicts with model information
    """
    try:
        cache_dir = get_huggingface_cache_dir()
        
        if not cache_dir.exists():
            MCPLogger.log(TOOL_LOG_NAME, f"Cache directory not found: {cache_dir}")
            return []
        
        models = []
        
        # Scan for model directories (format: models--organization--model-name)
        for item in cache_dir.iterdir():
            if item.is_dir() and item.name.startswith('models--'):
                # Parse model name from directory
                parts = item.name.split('--')
                if len(parts) >= 3:
                    organization = parts[1]
                    model_name = '--'.join(parts[2:])  # Handle names with dashes
                    model_id = f"{organization}/{model_name}"
                    
                    # Get size info
                    size_bytes = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                    size_gb = size_bytes / (1024**3)
                    
                    models.append({
                        "model_id": model_id,
                        "cache_path": str(item),
                        "size_gb": round(size_gb, 2)
                    })
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(models)} installed models")
        return models
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error scanning for models: {str(e)}")
        return []

def load_model(model_name: str, device: str = "auto") -> Tuple[Any, Any, str]:
    """Load a model and tokenizer with device management (from test_torch_import.py pattern).
    
    Args:
        model_name: HuggingFace model identifier
        device: "auto", "cuda", or "cpu"
        
    Returns:
        Tuple of (model, tokenizer, actual_device)
    """
    try:
        # Check cache first
        if model_name in _loaded_models:
            MCPLogger.log(TOOL_LOG_NAME, f"Using cached model: {model_name}")
            return _loaded_models[model_name]
        
        # Ensure dependencies
        torch = ensure_torch()
        transformers = ensure_transformers()
        ensure_accelerate()  # Required for device_map on GPU
        
        # Determine device (from test_torch_import.py)
        if device == "auto":
            actual_device = "cuda" if (hasattr(torch, 'cuda') and torch.cuda.is_available()) else "cpu"
        else:
            actual_device = device
        
        MCPLogger.log(TOOL_LOG_NAME, f"Loading model {model_name} on {actual_device}...")
        load_start = time.time()
        
        # Load model (pattern from test_torch_import.py)
        model = transformers.AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype="auto",
            device_map="auto" if actual_device == "cuda" else None
        )
        
        if actual_device == "cpu":
            model = model.to(actual_device)
        
        # Load tokenizer
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
        
        load_time = time.time() - load_start
        
        # Get VRAM usage if CUDA
        vram_gb = 0.0
        if actual_device == "cuda" and torch.cuda.is_available():
            vram_gb = torch.cuda.memory_allocated(0) / (1024**3)
            MCPLogger.log(TOOL_LOG_NAME, f"Model loaded in {load_time:.2f}s, using {vram_gb:.2f} GB VRAM")
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Model loaded in {load_time:.2f}s on CPU")
        
        # Cache the loaded model
        _loaded_models[model_name] = (model, tokenizer, actual_device)
        
        return model, tokenizer, actual_device
        
    except Exception as e:
        error_msg = f"Failed to load model {model_name}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        raise RuntimeError(error_msg)

# ============================================================================
# Operation Handlers
# ============================================================================

def handle_hardware_info(params: Dict) -> Dict:
    """Handle hardware_info operation.
    
    Returns:
        Dict containing hardware information
    """
    try:
        info = get_hardware_info()
        return {
            "content": [{"type": "text", "text": json.dumps(info, indent=2)}],
            "isError": False
        }
    except RuntimeError as e:
        if str(e) == "CUDA_INSTALL_SUCCESS_RESTART_REQUIRED":
            return {
                "content": [{
                    "type": "text",
                    "text": """✅ CUDA-enabled PyTorch has been successfully installed for your NVIDIA GPU!

⏰ The MCP server is restarting to load the new CUDA libraries. This will take approximately 5-10 minutes.

Please wait for the server to restart, then retry your request to see the updated hardware information with CUDA support.

You can check the server logs to monitor the restart progress."""
                }],
                "isError": False,
                "_cuda_install_restart": True
            }
        return create_error_response(f"Error getting hardware info: {str(e)}", with_readme=False)
    except Exception as e:
        return create_error_response(f"Error getting hardware info: {str(e)}", with_readme=False)

def handle_list_installed_models(params: Dict) -> Dict:
    """Handle list_installed_models operation.
    
    Returns:
        Dict containing list of installed models
    """
    try:
        models = list_installed_models()
        
        if not models:
            message = "No models found in HuggingFace cache.\n\nModels will be automatically downloaded when first used.\nCache location: " + str(get_huggingface_cache_dir())
            return {
                "content": [{"type": "text", "text": message}],
                "isError": False
            }
        
        return {
            "content": [{"type": "text", "text": json.dumps(models, indent=2)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error listing models: {str(e)}", with_readme=False)

def handle_model_info(params: Dict) -> Dict:
    """Handle model_info operation.
    
    Args:
        params: Must contain 'model' parameter
        
    Returns:
        Dict containing model information
    """
    try:
        model_name = params.get("model")
        if not model_name:
            return create_error_response("Parameter 'model' is required for model_info operation", with_readme=True)
        
        # Check if model is installed
        installed = list_installed_models()
        is_installed = any(m["model_id"] == model_name for m in installed)
        
        info = {
            "model_id": model_name,
            "installed": is_installed,
            "cache_location": str(get_huggingface_cache_dir())
        }
        
        if is_installed:
            model_data = next(m for m in installed if m["model_id"] == model_name)
            info.update(model_data)
        
        return {
            "content": [{"type": "text", "text": json.dumps(info, indent=2)}],
            "isError": False
        }
    except Exception as e:
        return create_error_response(f"Error getting model info: {str(e)}", with_readme=False)

def handle_chat_completion(params: Dict) -> Dict:
    """Handle chat_completion operation (OpenRouter-compatible).
    
    Args:
        params: Must contain 'model' and 'messages', optional: temperature, max_tokens, top_p, device
        
    Returns:
        Dict containing OpenAI-format response
    """
    try:
        # Validate required parameters
        model_name = params.get("model")
        if not model_name:
            return create_error_response("Parameter 'model' is required for chat_completion", with_readme=True)
        
        messages = params.get("messages")
        if not messages or not isinstance(messages, list):
            return create_error_response("Parameter 'messages' must be a non-empty array", with_readme=True)
        
        # Get optional parameters with defaults
        temperature = params.get("temperature", 0.7)
        max_tokens = params.get("max_tokens", 1000)
        top_p = params.get("top_p", 0.9)
        device = params.get("device", "auto")
        
        # Load model
        MCPLogger.log(TOOL_LOG_NAME, f"Chat completion request for model: {model_name}")
        try:
            model, tokenizer, actual_device = load_model(model_name, device)
        except RuntimeError as e:
            if str(e) == "CUDA_INSTALL_SUCCESS_RESTART_REQUIRED":
                return {
                    "content": [{
                        "type": "text",
                        "text": """✅ CUDA-enabled PyTorch has been successfully installed for your NVIDIA GPU!

⏰ The MCP server is restarting to load the new CUDA libraries. This will take approximately 5-10 minutes.

Please wait for the server to restart, then retry your request. The model will then run on your GPU with significantly faster performance.

You can check the server logs to monitor the restart progress."""
                    }],
                    "isError": False,
                    "_cuda_install_restart": True
                }
            raise
        
        # Apply chat template (from test_torch_import.py pattern)
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
        
        # Tokenize
        model_inputs = tokenizer([text], return_tensors="pt").to(model.device)
        input_tokens = model_inputs.input_ids.shape[1]
        
        MCPLogger.log(TOOL_LOG_NAME, f"Generating response (max_tokens={max_tokens}, temp={temperature})...")
        gen_start = time.time()
        
        # Generate (from test_torch_import.py pattern)
        generated_ids = model.generate(
            **model_inputs,
            max_new_tokens=max_tokens,
            do_sample=True,
            temperature=temperature,
            top_p=top_p
        )
        
        gen_time = time.time() - gen_start
        
        # Decode only the generated part
        generated_ids_only = [
            output_ids[len(input_ids):] 
            for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
        ]
        
        response_text = tokenizer.batch_decode(generated_ids_only, skip_special_tokens=True)[0]
        
        # Calculate metrics
        num_tokens = len(generated_ids_only[0])
        tokens_per_sec = num_tokens / gen_time if gen_time > 0 else 0
        
        MCPLogger.log(TOOL_LOG_NAME, f"Generated {num_tokens} tokens in {gen_time:.2f}s ({tokens_per_sec:.1f} tok/s)")
        
        # Format response in OpenAI format (compatible with OpenRouter)
        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": num_tokens,
                "total_tokens": input_tokens + num_tokens
            },
            "system_fingerprint": None
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(response)}],
            "isError": False
        }
        
    except Exception as e:
        error_msg = f"Error in chat completion: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return create_error_response(error_msg, with_readme=False)

def handle_llm(input_param: Dict) -> Dict:
    """Handle LLM tool operations via MCP interface.
    
    Args:
        input_param: Input parameters dict
        
    Returns:
        Response dict
    """
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        handler_info = input_param.pop('handler_info', None)
        
        # Collapse the single-input placeholder
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
            
        # Validate input structure
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
            
        # Check for token
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True)

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract validated parameters
        operation = validated_params.get("operation")
        
        # Dynamic handler dispatch
        handler_name = f"handle_{operation}"
        if handler_name in globals():
            return globals()[handler_name](validated_params)
        else:
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        error_msg = f"Error in LLM operation: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {traceback.format_exc()}")
        return create_error_response(error_msg, with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    "llm": handle_llm
}

def initialize_tool() -> None:
    """Initialize the tool - called once when server starts."""
    MCPLogger.log(TOOL_LOG_NAME, "LLM tool initialized") 
