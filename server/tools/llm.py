"""
File: ragtag/tools/llm.py
Project: Aura Friday MCP-Link Server
Component: Unified LLM Tool with Streaming Support
Author: Christopher Nathan Drake (cnd)

Unified LLM interface supporting:
- Local models (via transformers/torch)
- OpenRouter API (cloud models)
- Direct API connections (OpenAI, Anthropic, etc.)
- Streaming responses via SSE to connected clients

This tool is designed to work with MCP's SSE transport, enabling real-time
token streaming to Android apps, Chrome extensions, and desktop UIs.

Copyright: © 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "WIꞇ𝐴mȠB𝟫4ҮƛRƟνƎΝꓑWa5ZÞyᴅΚᎪßꓓꓖϜ৭FᎬΟƙȷCВОxɅh8ᒿw2YďᎠ2ɯꓗрᏮРÐҮWWꞇЗƻᴠƿĐƎɊԝуEƘоՕ𝟫ƟƿZcSѡrxԛSωƛpɪEбКƙ8𝙰𝟨QƿSᴠµҮĵᗷƻυ8ϨYԁ"
"signdate": "2026-07-23T02:37:49.194Z",

===============================================================================
                        UNIFIED LLM TOOL - ARCHITECTURE
===============================================================================

## DESIGN GOALS

1. **Single Entry Point**: One tool for all LLM operations
2. **Provider Agnostic**: Same interface for local, OpenRouter, direct APIs
3. **Streaming First**: Native SSE streaming to connected clients
4. **MCP Native**: Uses MCP's reverse-call mechanism for streaming
5. **Reflection Friendly**: Minimal hardcoded logic, maximum flexibility

## STREAMING ARCHITECTURE

MCP doesn't have native streaming in tool responses. We solve this by:

1. Tool returns immediately with a "stream_id"
2. Server sends SSE events with partial responses using that stream_id
3. Client accumulates tokens until stream completes
4. Final message includes complete response + usage stats

SSE Event Format for Streaming:
```
event: llm_stream
data: {"stream_id": "xxx", "delta": "Hello", "done": false}

event: llm_stream  
data: {"stream_id": "xxx", "delta": " world", "done": false}

event: llm_stream
data: {"stream_id": "xxx", "delta": "", "done": true, "usage": {...}}
```

## PROVIDER BACKENDS

### 1. Local (transformers)
- Uses existing llm.py infrastructure
- GPU/CPU auto-detection
- Model caching

### 2. OpenRouter
- Uses existing openrouter.py infrastructure  
- 300+ models available
- Streaming via SSE proxy

### 3. Direct APIs
- OpenAI-compatible endpoints
- Anthropic API
- Custom endpoints

## OPERATIONS

- readme: Get documentation
- chat: Unified chat completion (streaming or non-streaming)
- list_providers: Show available backends
- list_models: List models for a provider
- model_info: Get model details
- stream_status: Check status of active stream
- cancel_stream: Cancel an active stream (real cancellation on every provider)
- ping: Provider health check without a chat round-trip
- preload_model / unload_model: Local model cache control

===============================================================================
"""

import os
import sys
import json
import time
import uuid
import threading
import traceback
from datetime import datetime
from typing import Dict, List, Union, Optional, Tuple, Any, Callable, Generator
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory, get_config_manager

# Constants
TOOL_LOG_NAME = "LLM"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"llm{TOOL_NAME_SUFFIX}"

# Active streams registry (stream_id -> StreamState)
_active_streams: Dict[str, 'StreamState'] = {}
_streams_lock = threading.Lock()

# Lazy-loaded modules cache
_ollama_module = None
_llama_cpp_module = None


class Provider(Enum):
    """Available LLM providers."""
    LOCAL = "local"                   # Local transformers models (HuggingFace)
    OLLAMA = "ollama"                 # Ollama server (native tool calling!)
    LLAMA_CPP = "llama_cpp"           # llama-cpp-python for GGUF models
    MLX = "mlx"                       # Apple Silicon MLX via mlx_vlm.server (OpenAI-compat)
    CURSOR_AGENT = "cursor_agent"     # Cursor IDE agent CLI (paid subscription models)
    CLAUDE_CODE = "claude_code"       # Claude Code CLI (Anthropic subscription models)
    CODEX_CLI = "codex_cli"           # OpenAI Codex CLI via local MCP bridge (gpt-5.x models)
    GEMINI_CLI = "gemini_cli"         # Google Gemini CLI (Gemini subscription models)
    OPENROUTER = "openrouter"         # OpenRouter API
    OPENAI = "openai"                 # Direct OpenAI API
    ANTHROPIC = "anthropic"           # Direct Anthropic API
    CUSTOM = "custom"                 # Custom OpenAI-compatible endpoint


# Parameter names that belong to our tool schema (not to be passed through to providers)
_KNOWN_LLM_TOOL_PARAMETER_NAMES = frozenset({
    "operation", "provider", "model", "messages", "stream", "stream_id",
    "generation_id", "temperature", "max_tokens", "api_key", "base_url",
    "tool_unlock_token", "sql", "bindings", "max_results", "search_criteria",
    "device", "images", "ollama_host", "gguf_path", "gpu_layers",
    "context_length", "tools", "allowed_tools", "tool_mapping", "tool_choice",
    "max_tool_rounds", "handler_info", "mlx_host",
    "effort", "tool_execution", "codex_thread_id", "timeout", "retries",
    "include_content", "endpoint",
})

# HTTP timeouts (seconds). Streaming reads apply per read() call, so a generous
# default still detects a hung provider without capping total generation time.
_PROVIDER_HTTP_TIMEOUT_SECONDS_DEFAULT = 120
_METADATA_HTTP_TIMEOUT_SECONDS = 30

# Consecutive send_stream_event failures tolerated before a worker aborts the stream
# (the client has disconnected; keeping the provider generating just wastes money).
_CONSECUTIVE_STREAM_SEND_FAILURE_ABORT_LIMIT = 5

# How long completed/errored StreamState records are retained so stream_status can
# distinguish "finished" from "never existed" and let clients recover the final text.
_COMPLETED_STREAM_RETENTION_SECONDS = 600

# Default cap on simultaneously active streams (protects low-end user machines).
# Overridable via settings[0].llm_max_concurrent_streams in nativemessaging.json.
_MAX_CONCURRENT_STREAMS_DEFAULT = 8

# Cap on cached local models (transformers + GGUF) before least-recently-used eviction.
_LOADED_MODEL_CACHE_MAX_ENTRIES = 2

# Pinned npx package spec for supply-chain hygiene and latency predictability.
_GEMINI_CLI_NPX_PACKAGE_SPEC = "@google/gemini-cli@0.50.0"

# MCP tools that are never delegable to an LLM via tool calling, even with
# allowed_tools=['*'].  server_control could kill/replace this server mid-loop.
_LLM_DELEGATION_DENIED_TOOL_BASENAMES = frozenset({"server_control"})


def _get_provider_http_timeout_seconds(params: Dict) -> float:
    """Resolve the per-request HTTP timeout: caller's 'timeout' param or the default."""
    raw_timeout_value = params.get('timeout')
    if isinstance(raw_timeout_value, (int, float)) and not isinstance(raw_timeout_value, bool) and raw_timeout_value > 0:
        return float(raw_timeout_value)
    return float(_PROVIDER_HTTP_TIMEOUT_SECONDS_DEFAULT)


def _extract_extra_provider_specific_parameters(params: Dict) -> Dict:
    """Extract parameters not recognized by our schema, for pass-through to providers.

    This allows callers to send provider-specific parameters (e.g. repetition_penalty,
    min_p, top_p, top_k, stop, etc.) without requiring code changes here.
    """
    return {k: v for k, v in params.items() if k not in _KNOWN_LLM_TOOL_PARAMETER_NAMES}


# Lazy-detected cursor-agent CLI availability
_cursor_agent_cli_path_cache = None
_cursor_agent_cli_detection_done = False

# Lazy-detected claude-code CLI availability
_claude_code_cli_path_cache = None
_claude_code_cli_detection_done = False

# Lazy-detected gemini CLI availability
_gemini_cli_path_cache = None
_gemini_cli_detection_done = False

# Active streaming subprocesses (stream_id -> subprocess.Popen) for cancellation support
_active_stream_subprocesses: Dict[str, Any] = {}


@dataclass
class StreamState:
    """Tracks state of an active streaming response."""
    stream_id: str
    provider: Provider
    model: str
    session_id: str
    request_id: str
    started_at: float = field(default_factory=time.time)
    chunks_received: int = 0
    content_so_far: str = ""
    is_complete: bool = False
    cancelled: bool = False
    completed_at: Optional[float] = None
    error: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        if self.cancelled:
            status = "cancelled"
        elif self.error:
            status = "error"
        elif self.is_complete:
            status = "completed"
        else:
            status = "streaming"
        return {
            "stream_id": self.stream_id,
            "provider": self.provider.value,
            "model": self.model,
            "started_at": self.started_at,
            "chunks_received": self.chunks_received,
            "content_length": len(self.content_so_far),
            "is_complete": self.is_complete,
            "cancelled": self.cancelled,
            "status": status,
            "error": self.error,
            "usage": self.usage,
            "elapsed_seconds": time.time() - self.started_at
        }


# Tool definitions.
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Unified LLM tool for chat completions with streaming support.
- Supports local models (transformers, Ollama, llama.cpp), OpenRouter, and direct API connections
- Native tool calling support via Ollama and cloud providers
- Multimodal support (images) for VL models
- Use {"input":{"operation":"readme"}} for full documentation
""",
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
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "chat", "list_providers", "list_models", "model_info", "stream_status", "cancel_stream", "hardware_info", "list_installed_models", "get_credits", "get_generation", "search_models", "ping", "preload_model", "unload_model"],
                    "description": "Operation to perform"
                },
                "provider": {
                    "type": "string",
                    "enum": ["local", "ollama", "llama_cpp", "mlx", "cursor_agent", "claude_code", "codex_cli", "gemini_cli", "openrouter", "openai", "anthropic", "custom"],
                    "description": "LLM provider: 'local' (transformers), 'ollama' (with tool calling!), 'llama_cpp' (GGUF), 'mlx' (Apple Silicon MLX server), 'cursor_agent' (Cursor IDE CLI), 'claude_code' (Claude Code CLI), 'codex_cli' (OpenAI Codex CLI via MCP bridge), 'gemini_cli' (Google Gemini CLI), 'openrouter', 'openai', 'anthropic', 'custom'"
                },
                "model": {
                    "type": "string",
                    "description": "Model identifier (e.g., 'anthropic/claude-3-opus', 'Qwen/Qwen2.5-7B-Instruct')"
                },
                "messages": {
                    "type": "array",
                    "description": "Array of message objects (OpenAI format)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
                            "content": {
                                "type": ["string", "array"],
                                "description": "Message text, or an array of content blocks (e.g. [{\"type\": \"text\", \"text\": \"...\"}]) accepted by the CLI providers"
                            },
                            "tool_call_id": {
                                "type": "string",
                                "description": "Required on role:'tool' messages - the id of the tool_call this result answers"
                            }
                        },
                        "required": ["role", "content"]
                    }
                },
                "stream": {
                    "type": "boolean",
                    "description": "Enable streaming responses via SSE (default: false)",
                    "default": False
                },
                "stream_id": {
                    "type": "string",
                    "description": "Stream ID for stream_status/cancel_stream operations"
                },
                "include_content": {
                    "type": "boolean",
                    "description": "For stream_status: include the full content_so_far text in the response (default: false). Lets clients that missed SSE events recover the final text (completed streams are retained for 10 minutes).",
                    "default": False
                },
                "timeout": {
                    "type": "number",
                    "description": "HTTP timeout in seconds for provider requests (default: 120). Applies per read for streaming.",
                    "default": 120
                },
                "retries": {
                    "type": "integer",
                    "description": "Retries with exponential backoff on 429/5xx/connection errors for non-streaming cloud provider calls (default: 0, max: 5)",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 5
                },
                "endpoint": {
                    "type": "string",
                    "description": "Named LLM endpoint from settings[0].llm_endpoints in nativemessaging.json. Injects provider, base_url/mlx_host/ollama_host, and api_key; explicit parameters take precedence."
                },
                "codex_thread_id": {
                    "type": "string",
                    "description": "For provider codex_cli: continue an existing Codex conversation thread (from codex_cli_metadata.thread_id of a prior response)"
                },
                "generation_id": {
                    "type": "string",
                    "description": "Generation ID for get_generation operation (from OpenRouter response)"
                },
                "temperature": {
                    "type": "number",
                    "description": "Control randomness (0.0-2.0). Note: the anthropic provider caps this at 1.0 (values above are clamped).",
                    "minimum": 0.0,
                    "maximum": 2.0,
                    "default": 0.7
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum tokens to generate (default: 1000; mlx defaults to 4096, streaming tool loops to 2000). For newer OpenAI models (o-series, gpt-5*) this is sent as max_completion_tokens automatically.",
                    "minimum": 1,
                    "default": 1000
                },
                "api_key": {
                    "type": "string",
                    "description": "API key for provider (optional - uses config if not provided)"
                },
                "base_url": {
                    "type": "string",
                    "description": "Custom API base URL (for 'custom' provider)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": f"Documentation-acknowledgement token proving the readme has been read (not a security credential; it is returned by the readme operation): {TOOL_UNLOCK_TOKEN}"
                },
                "sql": {
                    "type": "string",
                    "description": "SQL query for search_models operation"
                },
                "bindings": {
                    "type": "object",
                    "description": "Query parameters for search_models (e.g., {\"query_vec\": {\"_embedding_text\": \"your search\"}})"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (for list/search operations)",
                    "default": 32
                },
                "search_criteria": {
                    "type": "object",
                    "description": "Filter criteria for list_models (provider, min_context_length, etc.)"
                },
                "device": {
                    "type": "string",
                    "enum": ["auto", "cuda", "mps", "cpu"],
                    "description": "Device for local models: auto (cuda > mps > cpu), cuda, mps (Apple Silicon), or cpu",
                    "default": "auto"
                },
                "images": {
                    "type": "array",
                    "description": "Array of images for multimodal models (VL). Each can be: file path, URL, or base64 data URI",
                    "items": {"type": "string"}
                },
                "ollama_host": {
                    "type": "string",
                    "description": "Ollama server URL (default: http://localhost:11434)",
                    "default": "http://localhost:11434"
                },
                "mlx_host": {
                    "type": "string",
                    "description": "MLX server URL for mlx_vlm.server (default: http://localhost:8081; port 11434 is Ollama's - use a distinct port for MLX)",
                    "default": "http://localhost:8081"
                },
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh", "max"],
                    "description": "Effort level for claude_code provider (default: max)",
                    "default": "max"
                },
                "gguf_path": {
                    "type": "string",
                    "description": "Path to GGUF model file for llama_cpp provider (or model alias)"
                },
                "gpu_layers": {
                    "type": "integer",
                    "description": "Number of layers to offload to GPU for llama_cpp (-1 = all)",
                    "default": -1
                },
                "context_length": {
                    "type": "integer",
                    "description": "Context window size for llama_cpp models",
                    "default": 8192
                },
                "tools": {
                    "type": "array",
                    "description": "Array of tool definitions (OpenAI format) for the LLM to call. Each tool has type, function.name, function.description, function.parameters",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["function"]},
                            "function": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "parameters": {"type": "object"}
                                },
                                "required": ["name"]
                            }
                        }
                    }
                },
                "allowed_tools": {
                    "type": "array",
                    "description": "List of MCP tool names the LLM is allowed to call, or ['*'] for all tools. Required when tools is set. High-risk tools (e.g. server_control) are never delegable, even with ['*'].",
                    "items": {"type": "string"}
                },
                "tool_mapping": {
                    "type": "object",
                    "description": "Optional mapping from LLM tool names to MCP tool names. E.g., {'run_code': 'python', 'search': 'sqlite'}. Use when LLM tool names differ from actual MCP tool names."
                },
                "tool_choice": {
                    "type": "string",
                    "description": "Tool selection strategy: 'auto' (default), 'none', or specific tool name",
                    "default": "auto"
                },
                "max_tool_rounds": {
                    "type": "integer",
                    "description": "Maximum number of tool call rounds before returning (default: 10)",
                    "default": 10,
                    "minimum": 0,
                    "maximum": 50
                },
                "tool_execution": {
                    "type": "string",
                    "enum": ["llm_managed", "caller_managed"],
                    "description": "Who executes tool calls: 'llm_managed' (default) = llm.py runs the ReAct loop and executes tools internally. 'caller_managed' = llm.py sends tool definitions to the model and returns any tool_calls in the response WITHOUT executing them. The caller is responsible for executing tools and calling chat again. Use caller_managed when you need per-tool-call checkpointing, policy guards, or custom tool execution logic (e.g., agent kernel).",
                    "default": "llm_managed"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "readme": f"""
# Unified LLM Tool - Streaming-First AI Interface

## Overview
Single tool for all LLM operations across multiple providers with native SSE streaming.

## Token
Your tool_unlock_token: {TOOL_UNLOCK_TOKEN}

## Operations

### 1. chat - Send chat completion request
{{
  "input": {{
    "operation": "chat",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "provider": "openrouter",  // or "local", "openai", "anthropic", "custom"
    "model": "anthropic/claude-3-5-sonnet",
    "messages": [
      {{"role": "system", "content": "You are helpful"}},
      {{"role": "user", "content": "Hello!"}}
    ],
    "stream": true,  // Enable SSE streaming
    "temperature": 0.7,
    "max_tokens": 1000
  }}
}}

**Streaming Response:**
When stream=true, returns immediately with:
{{
  "stream_id": "uuid-xxx",
  "status": "streaming"
}}

Then SSE events are sent to your session:
```
event: llm_stream
data: {{"stream_id": "xxx", "delta": "Hello", "done": false}}
```

**Non-Streaming Response:**
When stream=false (default), returns complete response:
{{
  "id": "chatcmpl-xxx",
  "choices": [{{"message": {{"content": "..."}}, "finish_reason": "stop"}}],
  "usage": {{"prompt_tokens": 10, "completion_tokens": 50}}
}}

**With Tool Calling (Agentic Mode):**
Allow the LLM to call MCP tools on your server:
{{
  "input": {{
    "operation": "chat",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "provider": "openrouter",
    "model": "anthropic/claude-3.5-sonnet",
    "messages": [{{"role": "user", "content": "What's the weather in Tokyo?"}}],
    "tools": [
      {{
        "type": "function",
        "function": {{
          "name": "get_weather",
          "description": "Get current weather for a location",
          "parameters": {{
            "type": "object",
            "properties": {{
              "location": {{"type": "string", "description": "City name"}}
            }},
            "required": ["location"]
          }}
        }}
      }}
    ],
    "allowed_tools": ["sqlite", "browser", "python"],  // MCP tools the LLM can call
    "tool_choice": "auto",  // or "none", or specific tool name
    "max_tool_rounds": 5    // Max iterations of tool calls
  }}
}}

The LLM will:
1. Decide if it needs to call a tool
2. Return tool_calls with function name and arguments
3. We execute the tool via MCP and return results
4. LLM continues until it has a final answer

**Security:** Only tools listed in `allowed_tools` can be called. Use ["*"] for all tools.
High-risk tools (server_control by default; extend via settings[0].llm_delegation_denied_tools)
are never delegable to the LLM, even with ["*"]. Every delegated call is logged distinctly.
Operators can disable the ["*"] wildcard entirely by setting
settings[0].llm_allow_wildcard_tool_delegation to false (explicit tool lists still work).

**Tool calling + streaming:** openrouter/openai stream token-by-token through the tool loop;
anthropic/ollama stream the same tool_call events with text deltas arriving per round.

**Structured outputs:** pass `response_format` (e.g. {{"type": "json_object"}}) - it is forwarded
to OpenAI-compatible providers unchanged; for the anthropic provider it is mapped to a system
instruction requesting JSON-only output (Anthropic has no native response_format).

### 2. list_providers - Show available backends
{{
  "input": {{
    "operation": "list_providers",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}"
  }}
}}

### 3. list_models - List models for a provider
{{
  "input": {{
    "operation": "list_models",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "provider": "openrouter"
  }}
}}

### 4. stream_status - Check active stream
{{
  "input": {{
    "operation": "stream_status",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "stream_id": "uuid-xxx"
  }}
}}

### 5. cancel_stream - Cancel active stream
{{
  "input": {{
    "operation": "cancel_stream",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "stream_id": "uuid-xxx"
  }}
}}

### 6. hardware_info - Check GPU/CPU capabilities
{{
  "input": {{
    "operation": "hardware_info",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}"
  }}
}}

Returns: torch_version, cuda_available, gpu_name, gpu_memory_gb, recommended_device

### 7. list_installed_models - Show cached local models
{{
  "input": {{
    "operation": "list_installed_models",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}"
  }}
}}

Returns: List of models in HuggingFace cache with model_id, cache_path, size_gb

### 8. get_credits - Check OpenRouter account balance
{{
  "input": {{
    "operation": "get_credits",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}"
  }}
}}

### 9. get_generation - Get OpenRouter generation details
{{
  "input": {{
    "operation": "get_generation",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "generation_id": "gen-xxx-xxx-xxx"
  }}
}}

Returns cost, tokens, timing info for a completed generation.

### 10. search_models - Semantic search for OpenRouter models
{{
  "input": {{
    "operation": "search_models",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "bindings": {{"query_vec": {{"_embedding_text": "code analysis and reasoning"}}}},
    "max_results": 10
  }}
}}

Or with custom SQL:
{{
  "input": {{
    "operation": "search_models",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "sql": "SELECT id, context_length FROM models WHERE context_length > 100000 ORDER BY context_length DESC",
    "max_results": 10
  }}
}}

### 11. ping - Provider health check
{{
  "input": {{
    "operation": "ping",
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "provider": "ollama"  // optional - omit to check all providers
  }}
}}

Verifies reachability/key validity per provider without a full chat round-trip.

### 12. preload_model / unload_model - Local model cache control
{{
  "input": {{
    "operation": "preload_model",  // loads into cache so first chat is fast
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "provider": "local",           // or "llama_cpp"
    "model": "Qwen/Qwen2.5-0.5B-Instruct"
  }}
}}
{{
  "input": {{
    "operation": "unload_model",   // evict from cache and reclaim memory
    "tool_unlock_token": "{TOOL_UNLOCK_TOKEN}",
    "model": "Qwen/Qwen2.5-0.5B-Instruct"  // omit to unload ALL cached models
  }}
}}

## Providers

| Provider | Description | Streaming | Tool Calling | Models |
|----------|-------------|-----------|--------------|--------|
| ollama | Local Ollama server | ✅ | ✅ Native! | qwen3:8b, llama3.1:8b, etc |
| mlx | Apple Silicon MLX server | ✅ | ❌ | Qwen3.5-35B-A3B, etc |
| cursor_agent | Cursor IDE paid models | ✅ | Inbuilt | 80+ cloud models |
| claude_code | Claude Code CLI (Anthropic) | ✅ | Inbuilt | opus-4-7, sonnet-4-6, haiku-4-5 |
| codex_cli | OpenAI Codex CLI (MCP bridge) | ✅ | Inbuilt | gpt-5.5, gpt-5.2-codex, etc |
| gemini_cli | Google Gemini CLI | ✅ | Inbuilt | gemini-3-flash, gemini-2.5-pro, etc |
| llama_cpp | GGUF models via llama.cpp | ✅ | ❌ | Any .gguf file |
| local | HuggingFace transformers | ✅ | ❌ | Qwen/*, meta-llama/*, etc |
| openrouter | Cloud API with 300+ models | ✅ | ✅ | anthropic/*, openai/*, etc |
| openai | Direct OpenAI API | ✅ | ✅ | gpt-4o, gpt-4-turbo, etc |
| anthropic | Direct Anthropic API | ✅ | ✅ | claude-3-opus, claude-3-sonnet, etc |
| custom | Any OpenAI-compatible endpoint | ✅ | ❌ | Depends on endpoint |

### Ollama (Recommended for Local Tool Calling)
Ollama provides native tool calling support for models like Qwen3 and Llama 3.1.
Auto-installs if not present. Requires Ollama server running (ollama serve).

### MLX (Apple Silicon - Recommended for Mac)
Run MLX models locally via mlx_vlm.server (OpenAI-compatible API).
Start server: `mlx_vlm.server --host 0.0.0.0 --port 8081`
(Port 11434 is Ollama's default - run MLX on a distinct port to avoid collisions.)
Anti-looping defaults (repetition_penalty, stop tokens) auto-applied for Qwen3.5 models.
Use mlx_host parameter to point to a non-default server address.
All unrecognized parameters are passed through to the MLX API.

### Cursor Agent (Paid Subscription Models)
Access 80+ cloud models via your Cursor subscription using the `agent` CLI.
Install: `curl https://cursor.com/install -fsS | bash`. Auto-detected at startup.
**Inbuilt tool calling:** The CLI has its own built-in tools (file editing, shell, search, etc.) and also connects to this MCP server, so it has access to all tools offered here. Do NOT pass `tools` in the request — the agent harness manages tool calling internally.

### Claude Code (Anthropic CLI)
Access Claude models via the Anthropic `claude` CLI (requires Anthropic subscription).
Install: `npm install -g @anthropic-ai/claude-code` or `curl -fsSL https://claude.ai/install.sh | bash`.
Models: claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5.
Supports streaming via `stream: true`. Use the `effort` parameter to control thinking depth (low/medium/high/xhigh/max, default: max).
The CLI runs in non-interactive `--print` mode with permissions bypassed (`--dangerously-skip-permissions`).
**Inbuilt tool calling:** The CLI has its own built-in tools (file editing, shell, search, etc.) and also connects to this MCP server, so it has access to all tools offered here. Do NOT pass `tools` in the request — the CLI harness manages tool calling internally.

### Extra Provider Parameters (Pass-Through)
Any parameters not in this tool's schema are passed through to the provider API.
Examples: repetition_penalty, min_p, top_p, top_k, stop, response_format, seed, etc.

### llama_cpp (GGUF Models)
For GGUF models like Qwen3-VL-8B-Instruct. Auto-installs llama-cpp-python.
Use model aliases (instruct-q6k) or full path to .gguf file.

### Multimodal (Vision) Support
For VL (Vision-Language) models, pass images array with file paths, URLs, or base64.

## Streaming Architecture

This tool uses MCP's SSE transport for streaming:

1. Call `chat` with `stream: true`
2. Receive immediate response with `stream_id`
3. SSE events sent to your session with token deltas
4. Final event has `done: true` and usage stats

This works with:
- Android apps (via McpSseClient)
- Chrome extension
- Desktop UI
- Any SSE-capable client

## Configuration

API keys are read from nativemessaging.json:
- settings[0].api_keys.OPENROUTER_API_KEY
- settings[0].api_keys.OPENAI_API_KEY
- settings[0].api_keys.ANTHROPIC_API_KEY

Or pass `api_key` directly in the request.
"""
    }
]


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    readme_text = readme(with_readme)
    return {"content": [{"type": "text", "text": f"{error_msg}{readme_text}"}], "isError": True}


def readme(with_readme: bool = True) -> str:
    """Return tool documentation."""
    if not with_readme:
        return ''
    return "\n\n" + json.dumps({
        "description": TOOLS[0]["readme"],
        "parameters": TOOLS[0]["real_parameters"]
    }, indent=2)


def get_api_key(provider: Provider, explicit_key: Optional[str] = None, interactive: bool = True) -> Optional[str]:
    """Get API key for a provider from config, explicit parameter, or interactive prompt.
    
    Args:
        provider: The provider to get the API key for
        explicit_key: If provided, use this key directly (bypasses config lookup)
        interactive: If True and key is missing, will attempt to prompt user via UI dialog
        
    Returns:
        The API key if found, or None if not available
    """
    if explicit_key:
        return explicit_key
    
    try:
        from ragtag.shared_config import SharedConfigManager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        api_keys = SharedConfigManager.ensure_settings_section(config, 'api_keys')
        
        key_map = {
            Provider.OPENROUTER: 'OPENROUTER_API_KEY',
            Provider.OPENAI: 'OPENAI_API_KEY',
            Provider.ANTHROPIC: 'ANTHROPIC_API_KEY',
        }
        
        key_name = key_map.get(provider)
        if key_name:
            key = api_keys.get(key_name)
            if key and key != 'placeholder-key':
                return key
        
        # Config didn't have it — check environment variables.
        # Environment keys are used but never persisted to disk: users with
        # ephemeral env keys do not expect them written to the config file.
        if key_name:
            env_key = os.environ.get(key_name)
            if env_key and env_key != 'placeholder-key':
                MCPLogger.log(TOOL_LOG_NAME, f"{key_name} found in environment variable (not persisted to config)")
                return env_key
        
        # Key not found in config or environment — try interactive prompt if enabled
        if interactive and key_name:
            MCPLogger.log(TOOL_LOG_NAME, f"{key_name} not set or is placeholder. Interactive mode: {interactive}")
            prompted_key = _prompt_user_for_api_key(provider, key_name)
            if prompted_key:
                # Save the new API key to config
                api_keys[key_name] = prompted_key
                try:
                    config_manager.save_config(config)
                    MCPLogger.log(TOOL_LOG_NAME, f"Successfully saved new {key_name} to config")
                    return prompted_key
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error saving API key to config: {e}")
                    # Return the key anyway, even if we couldn't save it
                    return prompted_key
        
        return None
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error getting API key: {e}")
        return None


def _prompt_user_for_api_key(provider: Provider, key_name: str) -> Optional[str]:
    """Prompt the user for an API key using the user tool.
    
    The API key is saved directly to the config file by the user tool's HTML form
    via the web server's /api/settings endpoint. This function just needs to reload
    the config after the dialog closes to get the saved key.
    
    Args:
        provider: The provider to get the key for
        key_name: The config key name (e.g., 'OPENROUTER_API_KEY')
        
    Returns:
        The API key from config after user enters it, or None if cancelled/failed
    """
    try:
        # Import get_server here to avoid circular imports
        from ..tools import get_server
        
        server = get_server()
        if not server:
            MCPLogger.log(TOOL_LOG_NAME, "No server instance available for user prompting")
            return None
        
        # Map provider to service info
        service_info = {
            Provider.OPENROUTER: ("OpenRouter", "https://openrouter.ai/keys"),
            Provider.OPENAI: ("OpenAI", "https://platform.openai.com/api-keys"),
            Provider.ANTHROPIC: ("Anthropic", "https://console.anthropic.com/settings/keys"),
        }
        
        service_name, service_url = service_info.get(provider, (provider.value, ""))
        
        MCPLogger.log(TOOL_LOG_NAME, f"Prompting user for {service_name} API key via user tool")
        
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
                    "service_name": service_name,
                    "service_url": service_url,
                    "tool_unlock_token": inter_tool_token
                }
            },
            calling_tool="llm"
        )
        
        # Check if the call was successful
        if result.get("isError"):
            MCPLogger.log(TOOL_LOG_NAME, f"User tool returned error: {result}")
            return None
        
        # Parse the response from the user tool (but don't rely on it).
        # Never log the response contents - it may contain the just-entered API key.
        content = result.get("content", [])
        if content and len(content) > 0:
            try:
                response_data = json.loads(content[0].get("text", "{}"))
                response_shape = list(response_data.keys()) if isinstance(response_data, dict) else type(response_data).__name__
                MCPLogger.log(TOOL_LOG_NAME, f"User tool response received (contents redacted; shape: {response_shape})")
            except json.JSONDecodeError as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error parsing user tool response: {e} (raw text redacted, length {len(content[0].get('text', ''))})")
        
        # Regardless of window response, reload config to check if key was saved
        # The HTML form saves directly via /api/settings endpoint, so we just need to reload
        MCPLogger.log(TOOL_LOG_NAME, "Popup closed, reloading config to check for saved API key")
        
        # Small delay to ensure file write has completed (race condition fix)
        time.sleep(0.2)
        
        api_key = get_api_key(provider, interactive=False)  # Non-interactive reload from config
        if api_key:
            MCPLogger.log(TOOL_LOG_NAME, "Successfully retrieved saved API key from config")
            return api_key
        else:
            MCPLogger.log(TOOL_LOG_NAME, "No API key found in config after popup closed (user may have cancelled)")
            return None
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error prompting user for API key: {e}")
        return None


# ============================================================================
# OpenRouter Models Database Infrastructure
# (Ported from openrouter.py for semantic search support)
# ============================================================================

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
                json.loads(value)
            except json.JSONDecodeError:
                pass  # Not JSON, which is fine
                
        # Test 3: Check for reasonable length
        if isinstance(value, str) and len(value) > 10000:
            return False, f"String too long: {len(value)} chars"
            
        # Test 4: Key name validation — ASCII identifiers only, since keys are
        # embedded raw into CREATE TABLE / INSERT SQL (isalnum() is Unicode-permissive)
        import re
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            return False, f"Invalid characters in key: {key}"
            
        if len(key) > 63:
            return False, f"Key too long: {len(key)} chars"
            
        return True, None
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"


def test_flattened_output(flattened_data: Dict) -> Tuple[bool, List[str]]:
    """Test if flattened dictionary is safe for SQL insertion.

    Returns:
        (True, []) if validation passes, (False, [error messages]) otherwise.
        Never exits the process — a background DB refresh must not kill the server.
    """
    errors = []
    
    for key, value in flattened_data.items():
        is_valid, error = test_flattened_value(key, value)
        if not is_valid:
            error_msg = f"Key '{key}' failed validation: {error}"
            MCPLogger.log(TOOL_LOG_NAME, "VALIDATION ERROR: " + error_msg)
            MCPLogger.log(TOOL_LOG_NAME, f"Value type: {type(value)}; value preview: {str(value)[:200]}")
            errors.append(error_msg)
            
    return (len(errors) == 0), errors


def flatten_dict(d: Dict[str, Any], prefix: str = '') -> Dict[str, Any]:
    """Flatten a nested dictionary for SQLite insertion.

    Strings are stored as-is (SQLite's dynamic typing makes numeric coercion
    unnecessary, and coercing corrupts version strings and zero-padded IDs).
    Raises ValueError if the flattened output fails SQL-safety validation.
    """
    result = {}
    
    def _set_key_logging_collisions(target: Dict[str, Any], key_name: str, key_value: Any) -> None:
        # flatten_dict key collisions ({"a":{"b":1}} vs {"a_b":2}) would silently overwrite
        if key_name in target:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: flatten_dict key collision on '{key_name}' — later value overwrites earlier")
        target[key_name] = key_value

    for key, value in d.items():
        new_key = f"{prefix}{key}" if prefix else key
        
        if isinstance(value, dict):
            nested_prefix = f"{new_key}_"
            nested_result = flatten_dict(value, nested_prefix)
            for nested_key, nested_value in nested_result.items():
                _set_key_logging_collisions(result, nested_key, nested_value)
            
        elif isinstance(value, (list, tuple)):
            json_str = json.dumps(value)
            _set_key_logging_collisions(result, new_key, json_str)
            
        else:
            if value is None:
                continue
            _set_key_logging_collisions(result, new_key, value)
                
    is_valid, validation_errors = test_flattened_output(result)
    if not is_valid:
        raise ValueError(f"Flattened data failed SQL-safety validation: {'; '.join(validation_errors)}")
    return result


def get_sql_type(value: Any) -> str:
    """Determine appropriate SQL type for a value."""
    if value is None:
        return "TEXT"
    elif isinstance(value, bool):
        return "BOOLEAN"
    elif isinstance(value, int):
        return "INTEGER"
    elif isinstance(value, float):
        return "REAL"
    elif isinstance(value, str):
        return "TEXT"
    else:
        return "TEXT"


def discover_fields(data: Dict, field_types: Dict[str, str], prefix: str = "") -> None:
    """Recursively discover fields and their types in the model data.

    Lists become a single TEXT column, matching flatten_dict which JSON-encodes
    every list — schema discovery and row flattening must agree or INSERTs
    reference columns that were never created.
    """
    for key, value in data.items():
        field_name = f"{prefix}{key}" if prefix else key
        
        if isinstance(value, dict):
            discover_fields(value, field_types, f"{field_name}_")
        elif isinstance(value, list):
            field_types[field_name] = "TEXT"
        else:
            field_types[field_name] = get_sql_type(value)


def generate_create_table_sql(field_types: Dict[str, str], table_name: str = "models") -> str:
    """Generate CREATE TABLE SQL statement from discovered field types."""
    import os
    
    if "id" not in field_types:
        field_types["id"] = "TEXT"
        
    fields = [
        "id TEXT PRIMARY KEY",
        "embedding BLOB CHECK(typeof(embedding) == 'blob' AND vec_length(embedding) == 1024)",
        "last_updated DATETIME DEFAULT (DATETIME('now')) NOT NULL"
    ]
    
    for field, sql_type in field_types.items():
        if field != "id":
            safe_field = field.replace(".", "_")
            fields.append(f"{safe_field} {sql_type}")
            
    newline_indent = ',\n        '
    sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        {newline_indent.join(fields)}
    )
    """
    
    MCPLogger.log(TOOL_LOG_NAME, f"Generated CREATE TABLE SQL:{os.linesep}{sql}")
    return sql


def discover_and_create_schema(models: List[Dict], table_name: str = "models") -> Tuple[bool, Optional[str]]:
    """Analyze model data structure and create appropriate database schema."""
    try:
        from .sqlite import sqlite
        
        if not models:
            return False, "No models provided for schema analysis"
            
        field_types = {}
        
        for model in models:
            MCPLogger.log(TOOL_LOG_NAME, f"Analyzing schema for model: {model.get('id', 'unknown')}")
            discover_fields(model, field_types)
            
        create_table_sql = generate_create_table_sql(field_types, table_name)
        
        drop_result = sqlite(
            sql=f"DROP TABLE IF EXISTS {table_name}",
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
            
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully created schema for {table_name} with fields: {', '.join(field_types.keys())}")
        return True, None
        
    except Exception as e:
        error_msg = f"Failed to discover and create schema: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return False, error_msg


def fetch_models_from_api() -> Tuple[Optional[List[Dict]], Optional[str]]:
    """Fetch models directly from OpenRouter API.
    
    Returns:
        Tuple[List[Dict], None]: (models_list, None) if successful
        Tuple[None, str]: (None, error_message) if failed
    """
    import http.client
    
    api_key = get_api_key(Provider.OPENROUTER, interactive=False)
    if not api_key:
        return None, "OPENROUTER_API_KEY not set in config file or is placeholder value"
        
    conn = None
    try:
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=_METADATA_HTTP_TIMEOUT_SECONDS)
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        MCPLogger.log(TOOL_LOG_NAME, "Requesting model list from OpenRouter API")
        conn.request("GET", "/api/v1/models", headers=headers)
        
        response = conn.getresponse()
        response_data = response.read().decode('utf-8')
        
        if response.status == 200:
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


def refresh_models_database(models: Optional[List[Dict]] = None) -> Tuple[bool, Optional[str]]:
    """Refresh the models database with current data from OpenRouter API.
    
    Args:
        models: Optional list of models to use for refresh. If not provided, fetches from API.
    
    This function:
    1. Uses provided models data or fetches current model data from OpenRouter
    2. Builds a staging table (models_staging) so a crash mid-refresh cannot leave
       a partial live table — the live 'models' table is only swapped at the end
    3. Validates and flattens model data for insertion (bad rows are skipped)
    4. Generates embeddings for model descriptions

    Never exits the process: all failures return (False, error) so one background
    refresh hiccup cannot take down the whole MCP server.
    """
    from .sqlite import sqlite
    
    staging_table_name = "models_staging"
    try:
        if models is None:
            models, error = fetch_models_from_api()
            if error:
                return False, f"Failed to fetch models: {error}"
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing {len(models)} models for database refresh")
        
        success, error = discover_and_create_schema(models, table_name=staging_table_name)
        if not success:
            return False, error
            
        inserted_count = 0
        skipped_models = []
        for model in models:
            model_id = model.get("id", "<unknown>")
            try:
                if not model.get("id"):
                    MCPLogger.log(TOOL_LOG_NAME, f"Skipping model with no ID: {str(model)[:200]}")
                    skipped_models.append(model_id)
                    continue
                    
                flattened_data = flatten_dict(model)
                
                description = f"{flattened_data.get('name', '')} {flattened_data.get('description', '')}"
                
                fields = [f for f in flattened_data.keys() if f != 'id']
                
                if fields:
                    fields_str = ', ' + ', '.join(fields)
                    values_str = ', ' + ', '.join(':' + f for f in fields)
                else:
                    fields_str = ''
                    values_str = ''
                
                insert_sql = f"""
                INSERT INTO {staging_table_name} (id, embedding{fields_str})
                VALUES (:id, vec_f32(:embedding){values_str})
                """
                
                bindings = {
                    "id": model_id,
                    "embedding": {"_embedding_text": description}
                }
                bindings.update(flattened_data)
                
                result = sqlite(
                    sql=insert_sql,
                    database=get_openrouter_db_path(),
                    bindings=bindings
                )
                
                if not result["operation_was_successful"]:
                    MCPLogger.log(TOOL_LOG_NAME, f"Skipping model {model_id}: insert failed: {result['error_message_if_operation_failed']}")
                    skipped_models.append(model_id)
                    continue
                inserted_count += 1
                    
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Skipping model {model_id}: {str(e)}")
                skipped_models.append(model_id)
                
        if inserted_count == 0:
            return False, f"Refresh aborted: no models could be inserted ({len(skipped_models)} skipped); keeping existing data"

        # Swap the fully-built staging table into place
        swap_drop_result = sqlite(sql="DROP TABLE IF EXISTS models", database=get_openrouter_db_path())
        if not swap_drop_result["operation_was_successful"]:
            return False, f"Failed to drop old models table during swap: {swap_drop_result['error_message_if_operation_failed']}"
        swap_rename_result = sqlite(sql=f"ALTER TABLE {staging_table_name} RENAME TO models", database=get_openrouter_db_path())
        if not swap_rename_result["operation_was_successful"]:
            return False, f"Failed to rename staging table during swap: {swap_rename_result['error_message_if_operation_failed']}"

        if skipped_models:
            MCPLogger.log(TOOL_LOG_NAME, f"Refreshed database with {inserted_count} models ({len(skipped_models)} skipped: {', '.join(skipped_models[:10])})")
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Successfully refreshed database with {inserted_count} models")
        return True, None
        
    except Exception as e:
        error_msg = f"Failed to refresh models database: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return False, error_msg


def check_models_database_freshness() -> Tuple[bool, Optional[str]]:
    """Check if models database exists and is fresh (updated within last 24h).
    
    Returns:
        Tuple[bool, None]: (needs_refresh, None) where needs_refresh indicates if refresh needed
        Tuple[bool, str]: (True, error_message) if check failed
    """
    try:
        from .sqlite import sqlite
        
        result = sqlite(
            sql="""
            SELECT datetime(MAX(last_updated)) as latest,
                   datetime('now', '-24 hours') as day_ago
            FROM models
            """,
            database=get_openrouter_db_path()
        )
        
        if not result["operation_was_successful"]:
            if "no such table" in result.get("error_message_if_operation_failed", "").lower():
                MCPLogger.log(TOOL_LOG_NAME, "Models database does not exist, needs creation")
                return True, None
            return True, f"Failed to query database: {result['error_message_if_operation_failed']}"
            
        rows = result.get("data_rows_from_result_set", [])
        if not rows or rows[0].get("latest") is None:
            MCPLogger.log(TOOL_LOG_NAME, "No entries in database, needs refresh")
            return True, None
            
        latest = rows[0]["latest"]
        day_ago = rows[0]["day_ago"]
        
        if latest < day_ago:
            MCPLogger.log(TOOL_LOG_NAME, f"Database is stale (last updated {latest})")
            return True, None
            
        MCPLogger.log(TOOL_LOG_NAME, f"Database is fresh (last updated {latest})")
        return False, None
        
    except Exception as e:
        error_msg = f"Failed to check database freshness: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        return True, error_msg


# ============================================================================
# Message Source Processing (URL and File Content Loading)
# (Ported from openrouter.py)
# ============================================================================

def _get_llm_settings_flag(flag_name: str, default: bool = False) -> bool:
    """Read a boolean flag from settings[0] in nativemessaging.json (default: secure)."""
    try:
        config = get_config_manager().load_config()
        settings_list = config.get('settings', [])
        if settings_list and isinstance(settings_list[0], dict):
            return bool(settings_list[0].get(flag_name, default))
        return default
    except Exception:
        return default


# Cap on fetched URL response size (source:"url" messages) to prevent memory blowup
_URL_FETCH_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_URL_FETCH_MAX_REDIRECTS = 5


def _url_targets_private_or_link_local_network(url: str) -> bool:
    """True if the URL's host resolves to any private/loopback/link-local/reserved address.

    Blocks SSRF into RFC1918 ranges, cloud metadata endpoints (169.254.x.x), and
    localhost from the server's network position. Unresolvable hosts are treated
    as private (fail closed).
    """
    import socket
    import ipaddress
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname
    if not hostname:
        return True
    try:
        resolved_address_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for address_info in resolved_address_infos:
        try:
            ip_address_of_host = ipaddress.ip_address(address_info[4][0])
        except ValueError:
            return True
        if (ip_address_of_host.is_private or ip_address_of_host.is_loopback
                or ip_address_of_host.is_link_local or ip_address_of_host.is_reserved
                or ip_address_of_host.is_multicast or ip_address_of_host.is_unspecified):
            return True
    return False


def fetch_url_content(url: str, custom_headers: Optional[Dict[str, str]] = None) -> str:
    """Fetch content from a URL with optional custom headers.
    
    Args:
        url: The URL to fetch content from
        custom_headers: Optional dictionary of custom HTTP headers. If provided, ONLY these headers will be used.
                      If not provided, default headers identifying this client honestly will be used.
    
    Returns:
        The fetched content as a string
        
    Raises:
        RuntimeError: If the fetch fails for any reason

    Security:
        URLs resolving to private/loopback/link-local addresses are blocked by
        default (set settings[0].llm_allow_private_network_urls to true to allow).
        Redirects are followed manually (up to 5 hops) with the same check per hop.
        Responses are capped at 10 MB.
    """
    import requests
    
    try:
        headers = custom_headers if custom_headers is not None else {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-AU,en;q=0.9",
            "user-agent": "AuraFriday-MCP-Link/1.0 (+https://aurafriday.com)"
        }

        if custom_headers is not None:
            if not isinstance(custom_headers, dict):
                raise ValueError("custom_headers must be a dictionary")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in custom_headers.items()):
                raise ValueError("All header keys and values must be strings")

        safe_headers = headers.copy()
        for key in ['authorization', 'cookie', 'api-key']:
            if key.lower() in safe_headers:
                safe_headers[key.lower()] = '[REDACTED]'
        MCPLogger.log(TOOL_LOG_NAME, f"Fetching URL: {url}\nHeaders: {json.dumps(safe_headers)}")

        private_network_urls_allowed = _get_llm_settings_flag('llm_allow_private_network_urls', False)

        current_url = url
        response = None
        for _redirect_hop in range(_URL_FETCH_MAX_REDIRECTS + 1):
            if not private_network_urls_allowed and _url_targets_private_or_link_local_network(current_url):
                raise RuntimeError(
                    f"Blocked fetch of private/link-local address: {current_url}. "
                    "Set settings[0].llm_allow_private_network_urls to true to allow."
                )
            response = requests.get(current_url, headers=headers, timeout=10,
                                    stream=True, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                redirect_target = response.headers.get('Location')
                response.close()
                if not redirect_target:
                    raise RuntimeError(f"HTTP {response.status_code} redirect with no Location header")
                from urllib.parse import urljoin
                current_url = urljoin(current_url, redirect_target)
                continue
            break
        else:
            raise RuntimeError(f"Too many redirects (>{_URL_FETCH_MAX_REDIRECTS})")

        if response.status_code != 200:
            response.close()
            raise RuntimeError(f"HTTP {response.status_code} - {response.reason}")

        fetched_content_bytes = b''
        for content_chunk in response.iter_content(chunk_size=65536):
            fetched_content_bytes += content_chunk
            if len(fetched_content_bytes) > _URL_FETCH_MAX_RESPONSE_BYTES:
                response.close()
                raise RuntimeError(f"Response exceeded the {_URL_FETCH_MAX_RESPONSE_BYTES} byte limit")

        MCPLogger.log(TOOL_LOG_NAME, f"Successfully fetched {len(fetched_content_bytes)} bytes from {current_url}")
        return fetched_content_bytes.decode(response.encoding or 'utf-8', errors='replace')

    except Exception as e:
        error_msg = f"Failed to fetch {url}: {type(e).__name__}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
        raise RuntimeError(error_msg)


def read_file_content(file_path: str) -> Optional[str]:
    """Read content from a file within the workspace.
    
    Args:
        file_path: Path to the file, relative to workspace root
        
    Returns:
        str: The file content if successful
        
    Security:
        Only allows access to files within the workspace directory, unless
        settings[0].llm_allow_file_read_outside_workspace is true in config.
        Uses realpath + commonpath so prefix tricks and symlinks cannot bypass it.
    """
    import os
    
    try:
        workspace_root = os.path.realpath(os.getcwd())
        
        MCPLogger.log(TOOL_LOG_NAME, f"Attempting to read file '{file_path}' from workspace root '{workspace_root}'")
        
        full_path = os.path.realpath(os.path.join(workspace_root, file_path))
        MCPLogger.log(TOOL_LOG_NAME, f"Resolved full path: '{full_path}'")
        
        try:
            path_is_inside_workspace = os.path.commonpath([full_path, workspace_root]) == workspace_root
        except ValueError:
            path_is_inside_workspace = False  # e.g. different drives on Windows
        if not path_is_inside_workspace and not _get_llm_settings_flag('llm_allow_file_read_outside_workspace', False):
            error_msg = (f"Security violation: Path '{full_path}' attempts to access file outside workspace root '{workspace_root}'. "
                         "Set settings[0].llm_allow_file_read_outside_workspace to true to allow.")
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise ValueError(error_msg)
            
        if not os.path.exists(full_path):
            error_msg = f"File not found: '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise FileNotFoundError(error_msg)
            
        if not os.path.isfile(full_path):
            error_msg = f"Path exists but is not a file: '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise IsADirectoryError(error_msg)
            
        if not os.access(full_path, os.R_OK):
            error_msg = f"Permission denied: Cannot read file '{full_path}'"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise PermissionError(error_msg)
            
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
                MCPLogger.log(TOOL_LOG_NAME, f"Successfully read {len(content)} bytes from '{full_path}'")
                return content
        except UnicodeDecodeError as e:
            error_msg = f"File '{full_path}' is not valid UTF-8 text: {str(e)}"
            MCPLogger.log(TOOL_LOG_NAME, error_msg)
            raise
            
    except (ValueError, FileNotFoundError, IsADirectoryError, PermissionError, UnicodeDecodeError):
        raise
    except Exception as e:
        error_msg = f"Unexpected error reading '{file_path}': {e.__class__.__name__}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        raise RuntimeError(error_msg) from e


def process_message_content(message: dict) -> dict:
    """Process a message's content if it has a source field.
    
    This allows messages to dynamically load content from URLs or files:
    
    - source: "url" - fetches content from the URL in 'content' field
    - source: "file" - reads content from the file path in 'content' field
    
    Args:
        message: Message dictionary with optional source field.
                For URL sources, can include 'headers' field with custom HTTP headers.
        
    Returns:
        dict: Processed message with content replaced if source was present
        
    Raises:
        ValueError: If content processing fails
        
    Example:
        {"role": "user", "content": "https://example.com/code.py", "source": "url"}
        → {"role": "user", "content": "<fetched content>"}
    """
    if not isinstance(message, dict):
        raise ValueError("Message must be a dictionary")
        
    processed = message.copy()
    
    if "source" not in processed:
        return processed
        
    source = processed["source"]
    content = processed["content"]
    
    try:
        if source == "url":
            custom_headers = None
            if "headers" in processed:
                custom_headers = processed.pop("headers")
                
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
            tool_name = source.replace("mcp_ragtag_sse_", "")
            raise NotImplementedError(f"Tool calls not yet implemented: {tool_name}")
            
        else:
            raise ValueError(f"Unknown source type: {source}")
            
    except Exception as e:
        raise ValueError(f"Failed to process source '{source}': {str(e)}")
        
    finally:
        # Strip both control keys so they never leak into provider request bodies
        processed.pop("source", None)
        processed.pop("headers", None)
        
    return processed


def send_stream_event(handler_info: Dict, stream_id: str, delta: str, done: bool, 
                      usage: Optional[Dict] = None, error: Optional[str] = None,
                      tool_call: Optional[Dict] = None, tool_rounds: Optional[int] = None,
                      heartbeat: bool = False) -> bool:
    """Send a streaming event to the client via SSE.
    
    This uses the MCP session to send an SSE event directly to the connected client.
    
    Event types:
    - Text delta: {"delta": "Hello", "done": false}
    - Tool call start: {"tool_call": {"status": "start", "id": "...", "name": "...", "arguments": {...}}}
    - Tool call complete: {"tool_call": {"status": "complete", "id": "...", "name": "...", "result": "...", "duration_ms": 123}}
    - Heartbeat: {"delta": "", "done": false, "heartbeat": true} (during long tool/CLI runs)
    - Done: {"delta": "", "done": true, "usage": {...}, "tool_rounds": 2}
    """
    try:
        responder = handler_info.get('responder')
        session_id = handler_info.get('session_id')
        
        if not responder or not session_id:
            MCPLogger.log(TOOL_LOG_NAME, "Cannot send stream event: missing responder or session_id")
            return False
        
        # Build the stream event
        event_data = {
            "jsonrpc": "2.0",
            "method": "llm/stream",
            "params": {
                "stream_id": stream_id,
                "delta": delta,
                "done": done
            }
        }
        
        if usage:
            event_data["params"]["usage"] = usage
        if error:
            event_data["params"]["error"] = error
        if tool_call:
            event_data["params"]["tool_call"] = tool_call
        if tool_rounds is not None:
            event_data["params"]["tool_rounds"] = tool_rounds
        if heartbeat:
            event_data["params"]["heartbeat"] = True
        
        # Send via the session's SSE connection
        if session_id in responder.active_sessions:
            session = responder.active_sessions[session_id]
            success = session.send_message("llm_stream", event_data)
            return success
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} not found for streaming")
            return False
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error sending stream event: {e}")
        return False


# ============================================================================
# Shared Streaming Infrastructure
# (single implementations of the SSE reader, stream lifecycle, cancellation,
#  HTTP POST, CLI message flattening, and stderr draining used by every provider)
# ============================================================================

def _prune_completed_streams_locked() -> None:
    """Drop stream records whose completed_at exceeded the retention window.

    Caller must hold _streams_lock. Replaces the old cleanup-thread-per-stream
    pattern: pruning happens lazily on registry accesses instead.
    """
    now = time.time()
    expired_stream_ids = [
        sid for sid, state in _active_streams.items()
        if state.completed_at is not None and (now - state.completed_at) > _COMPLETED_STREAM_RETENTION_SECONDS
    ]
    for sid in expired_stream_ids:
        _active_streams.pop(sid, None)
        _active_stream_subprocesses.pop(sid, None)


def _register_new_stream(state: StreamState) -> Optional[str]:
    """Register a new stream, enforcing the concurrency cap.

    Returns None on success, or an error message when too many streams are active.
    """
    max_concurrent_streams = _MAX_CONCURRENT_STREAMS_DEFAULT
    try:
        config = get_config_manager().load_config()
        settings_list = config.get('settings', [])
        if settings_list and isinstance(settings_list[0], dict):
            configured_cap = settings_list[0].get('llm_max_concurrent_streams')
            if isinstance(configured_cap, int) and not isinstance(configured_cap, bool) and configured_cap > 0:
                max_concurrent_streams = configured_cap
    except Exception:
        pass

    with _streams_lock:
        _prune_completed_streams_locked()
        currently_active_count = sum(1 for s in _active_streams.values() if not s.is_complete)
        if currently_active_count >= max_concurrent_streams:
            return (f"Too many concurrent streams ({currently_active_count} active, cap {max_concurrent_streams}). "
                    "Wait for a stream to finish, cancel one, or raise settings[0].llm_max_concurrent_streams.")
        _active_streams[state.stream_id] = state
    return None


def _stream_is_cancelled(stream_id: str) -> bool:
    """Check the cancellation flag for a stream (thread-safe)."""
    with _streams_lock:
        state = _active_streams.get(stream_id)
        return bool(state and state.cancelled)


def _mark_stream_complete(stream_id: str, content: Optional[str] = None,
                          usage: Optional[Dict] = None, error: Optional[str] = None) -> None:
    """Record terminal state for a stream. The record is retained (not popped) so
    stream_status can distinguish 'finished' from 'never existed' and return the
    final text; lazy pruning removes it after the retention window."""
    with _streams_lock:
        state = _active_streams.get(stream_id)
        if state:
            state.is_complete = True
            state.completed_at = time.time()
            if content is not None:
                state.content_so_far = content
            if usage is not None:
                state.usage = usage
            if error is not None and not state.error:
                state.error = error


def _record_stream_delta(stream_id: str, full_content: str) -> None:
    """Update chunk count and accumulated content for a live stream."""
    with _streams_lock:
        state = _active_streams.get(stream_id)
        if state:
            state.chunks_received += 1
            state.content_so_far = full_content


def _iter_sse_data_events(response, stream_id: Optional[str] = None) -> Generator[Tuple[Optional[str], str], None, None]:
    """Yield (event_type, data) pairs from an SSE HTTP response body.

    Fixes shared by every streaming provider:
    - UTF-8 characters split across chunk boundaries (incremental decoder)
    - CRLF event separators (legal SSE) normalised to LF
    - 'data:' lines with or without the optional space
    Stops early when the stream has been cancelled via cancel_stream.
    """
    import codecs
    utf8_incremental_decoder = codecs.getincrementaldecoder('utf-8')('replace')
    buffer = ""
    while True:
        if stream_id is not None and _stream_is_cancelled(stream_id):
            return
        chunk = response.read(1024)
        if not chunk:
            break
        buffer += utf8_incremental_decoder.decode(chunk)
        buffer = buffer.replace('\r\n', '\n')
        while '\n\n' in buffer:
            event, buffer = buffer.split('\n\n', 1)
            event_type = None
            for line in event.split('\n'):
                if line.startswith('event:'):
                    event_type = line[6:].lstrip()
                elif line.startswith('data:'):
                    data = line[5:]
                    if data.startswith(' '):
                        data = data[1:]
                    yield (event_type, data)


class _ConsecutiveSendFailureTracker:
    """Counts consecutive failed SSE sends so a worker can abort once the client
    is clearly gone (stops paying for generation nobody receives)."""

    def __init__(self, abort_limit: int = _CONSECUTIVE_STREAM_SEND_FAILURE_ABORT_LIMIT):
        self.consecutive_failure_count = 0
        self.abort_limit = abort_limit

    def should_abort_after(self, send_succeeded: bool) -> bool:
        if send_succeeded:
            self.consecutive_failure_count = 0
        else:
            self.consecutive_failure_count += 1
        return self.consecutive_failure_count >= self.abort_limit


class _PeriodicStreamHeartbeat:
    """Sends heartbeat SSE events every interval while a long operation runs
    (tool executions, CLI agent runs) so clients can render progress."""

    def __init__(self, handler_info: Dict, stream_id: str, interval_seconds: float = 10.0):
        self._handler_info = handler_info
        self._stream_id = stream_id
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        def _heartbeat_loop():
            while not self._stop_event.wait(self._interval_seconds):
                send_stream_event(self._handler_info, self._stream_id, "", False, heartbeat=True)
        self._thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False


def _http_post_json(host: str, path: str, headers: Dict, body: Dict, use_https: bool = True,
                    timeout: float = _PROVIDER_HTTP_TIMEOUT_SECONDS_DEFAULT,
                    retries: int = 0) -> Tuple[int, str]:
    """Blocking JSON POST via http.client with timeout and optional retries.

    Retries (with exponential backoff) on HTTP 429/5xx and connection errors —
    never on other statuses. Returns (status_code, response_body_text).
    Raises the last connection error if all attempts fail.
    """
    import http.client

    try:
        retries = max(0, min(5, int(retries)))
    except (TypeError, ValueError):
        retries = 0

    last_connection_error: Optional[Exception] = None
    for attempt_number in range(retries + 1):
        if attempt_number > 0:
            backoff_seconds = min(30.0, (2.0 ** (attempt_number - 1)))
            MCPLogger.log(TOOL_LOG_NAME, f"Retrying POST {host}{path} in {backoff_seconds}s (attempt {attempt_number + 1}/{retries + 1})")
            time.sleep(backoff_seconds)
        conn = None
        try:
            if use_https:
                conn = http.client.HTTPSConnection(host, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(host, timeout=timeout)
            conn.request("POST", path, body=json.dumps(body), headers=headers)
            response = conn.getresponse()
            response_text = response.read().decode('utf-8', errors='replace')
            if response.status == 429 or response.status >= 500:
                last_connection_error = None
                if attempt_number < retries:
                    MCPLogger.log(TOOL_LOG_NAME, f"POST {host}{path} returned {response.status}; will retry")
                    continue
            return response.status, response_text
        except Exception as connection_error:
            last_connection_error = connection_error
            if attempt_number < retries:
                continue
            raise
        finally:
            if conn:
                conn.close()
    # Unreachable, but keeps static analysis happy
    if last_connection_error:
        raise last_connection_error
    raise RuntimeError("HTTP POST failed without a recorded error")


def _flatten_messages_for_cli(messages: List[Dict]) -> str:
    """Flatten an OpenAI-format messages array into a single CLI prompt string.

    System messages become bracketed instructions, assistant messages become
    bracketed prior responses, user messages are included verbatim. Content
    block lists (Anthropic format) are joined from their text blocks.
    Shared by the cursor_agent, claude_code, codex_cli and gemini_cli providers.
    """
    prompt_parts = []
    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    text_parts.append(block.get('text', ''))
            content = '\n'.join(text_parts)
        if role == 'system':
            prompt_parts.append(f"[System instruction: {content}]")
        elif role == 'assistant':
            prompt_parts.append(f"[Previous assistant response: {content}]")
        else:
            prompt_parts.append(content)
    return "\n\n".join(prompt_parts)


def _start_stderr_drain_thread(proc) -> Callable[[], str]:
    """Drain a subprocess's stderr on a companion thread to prevent pipe-buffer
    deadlock (child blocks writing stderr while we only read stdout).

    Returns a callable producing the captured stderr tail (last 10000 chars).
    """
    captured_stderr_tail: List[str] = [""]

    def _drain_stderr_loop():
        try:
            if proc.stderr is None:
                return
            for stderr_line in proc.stderr:
                captured_stderr_tail[0] = (captured_stderr_tail[0] + stderr_line)[-10000:]
        except Exception:
            pass

    drain_thread = threading.Thread(target=_drain_stderr_loop, daemon=True)
    drain_thread.start()

    def _get_captured_stderr_tail() -> str:
        return captured_stderr_tail[0]

    return _get_captured_stderr_tail


def _subprocess_no_window_flags() -> int:
    """CREATE_NO_WINDOW on Windows so background CLI spawns don't flash consoles."""
    if sys.platform == 'win32':
        import subprocess
        return subprocess.CREATE_NO_WINDOW
    return 0


def _openai_token_limit_parameter_name_for_model(model: str) -> str:
    """Newer OpenAI models (o-series, gpt-5*) reject max_tokens and require
    max_completion_tokens; older models accept max_tokens."""
    import re
    if re.match(r'^(o\d|gpt-5)', model or ''):
        return "max_completion_tokens"
    return "max_tokens"


def _convert_openai_messages_to_anthropic_format(messages: List[Dict]) -> Tuple[Optional[str], List[Dict]]:
    """Translate OpenAI-format messages into Anthropic /v1/messages format.

    - all system messages are concatenated into one system string (not just the last)
    - assistant messages carrying tool_calls become content blocks with tool_use
    - role:"tool" results become role:"user" messages with a tool_result block
    - consecutive same-role messages are merged (Anthropic requires alternation)

    Returns (system_content_or_None, anthropic_messages).
    """
    system_parts: List[str] = []
    converted_messages: List[Dict] = []

    def _as_content_block_list(content) -> List[Dict]:
        if isinstance(content, list):
            return content
        return [{"type": "text", "text": str(content)}] if content else []

    for msg in messages:
        role = msg.get('role')
        if role == 'system':
            system_content_value = msg.get('content', '')
            if system_content_value:
                system_parts.append(str(system_content_value))
        elif role == 'assistant' and msg.get('tool_calls'):
            content_blocks = _as_content_block_list(msg.get('content'))
            for tool_call in msg['tool_calls']:
                function_info = tool_call.get('function', {})
                raw_arguments = function_info.get('arguments')
                if isinstance(raw_arguments, str):
                    try:
                        tool_input = json.loads(raw_arguments or '{}')
                    except json.JSONDecodeError:
                        tool_input = {}
                else:
                    tool_input = raw_arguments or {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get('id') or str(uuid.uuid4()),
                    "name": function_info.get('name', ''),
                    "input": tool_input
                })
            converted_messages.append({"role": "assistant", "content": content_blocks})
        elif role == 'tool':
            converted_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get('tool_call_id', ''),
                    "content": str(msg.get('content') or '')
                }]
            })
        else:
            converted_messages.append({"role": role, "content": msg.get('content', '')})

    # Merge consecutive same-role messages (multiple tool results must share one user turn)
    merged_messages: List[Dict] = []
    for msg in converted_messages:
        if merged_messages and merged_messages[-1]["role"] == msg["role"]:
            previous_blocks = _as_content_block_list(merged_messages[-1]["content"])
            merged_messages[-1]["content"] = previous_blocks + _as_content_block_list(msg["content"])
        else:
            merged_messages.append(msg)

    system_content = "\n\n".join(system_parts) if system_parts else None
    return system_content, merged_messages


def _map_anthropic_stop_reason_to_openai(stop_reason: Optional[str]) -> str:
    """Map Anthropic stop_reason values onto OpenAI finish_reason vocabulary."""
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop_reason or "", stop_reason or "stop")


def _apply_anthropic_response_format_mapping(request_body: Dict, extra_params: Dict) -> None:
    """Anthropic has no response_format parameter; map a JSON-mode request onto a
    system instruction so callers get JSON-mode uniformly across providers."""
    response_format = extra_params.pop('response_format', None)
    if isinstance(response_format, dict) and response_format.get('type') in ('json_object', 'json_schema'):
        json_instruction = "Respond ONLY with valid JSON. No prose, no markdown fences."
        schema_definition = response_format.get('json_schema') or response_format.get('schema')
        if schema_definition:
            json_instruction += f" The JSON must conform to this schema: {json.dumps(schema_definition)}"
        existing_system = request_body.get("system")
        request_body["system"] = f"{existing_system}\n\n{json_instruction}" if existing_system else json_instruction


# ============================================================================
# Streaming with Tool Calling Support
# ============================================================================

def chat_openai_compatible_streaming_with_tools(params: Dict, handler_info: Dict,
                                                provider: Provider = Provider.OPENROUTER) -> Dict:
    """
    Streaming chat with automatic tool execution loop (OpenRouter and OpenAI).
    
    This is the key function for the AuraFriday chat experience:
    1. Streams LLM response text as it arrives
    2. When LLM returns tool_calls, sends tool_call events and executes them
    3. Continues streaming with tool results until LLM is done
    
    Returns immediately with stream_id; actual work happens in background thread.
    """
    import http.client
    
    api_key = get_api_key(provider, params.get('api_key'))
    if not api_key:
        key_env_name = 'OPENAI_API_KEY' if provider == Provider.OPENAI else 'OPENROUTER_API_KEY'
        return create_error_response(f"{provider.value} API key not configured. Set {key_env_name} in config.", with_readme=False)
    
    if provider == Provider.OPENAI:
        api_host = "api.openai.com"
        api_path = "/v1/chat/completions"
        default_model = 'gpt-4o'
    else:
        api_host = "openrouter.ai"
        api_path = "/api/v1/chat/completions"
        default_model = 'anthropic/claude-3.5-haiku'

    model = params.get('model', default_model)
    messages = list(params.get('messages', []))
    
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 2000)
    tools = params.get('tools', [])
    allowed_tools = params.get('allowed_tools', [])
    tool_mapping = params.get('tool_mapping', {})
    max_tool_rounds = params.get('max_tool_rounds', 10)
    tool_choice = params.get('tool_choice', 'auto')
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)
    
    stream_id = str(uuid.uuid4())
    
    registration_error = _register_new_stream(StreamState(
        stream_id=stream_id,
        provider=provider,
        model=model,
        session_id=handler_info.get('session_id', ''),
        request_id=handler_info.get('request_id', '')
    ))
    if registration_error:
        return create_error_response(registration_error, with_readme=False)
    
    def stream_one_llm_response(current_messages: List[Dict]) -> Tuple[str, List[Dict], Dict]:
        """
        Stream a single LLM response, returning (full_content, tool_calls, usage).
        Sends delta events as tokens arrive; usage comes from the provider's
        include_usage chunk (real numbers, not estimates).
        """
        request_body = {
            "model": model,
            "messages": current_messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True}
        }
        if provider == Provider.OPENAI:
            request_body[_openai_token_limit_parameter_name_for_model(model)] = max_tokens
        else:
            request_body["max_tokens"] = max_tokens
            # OpenRouter usage accounting: adds cost to the final usage chunk
            request_body["usage"] = {"include": True}
        
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = tool_choice
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        if provider == Provider.OPENROUTER:
            headers["HTTP-Referer"] = "https://aurafriday.com"
            headers["X-Title"] = "AuraFriday LLM"
        
        conn = None
        full_content = ""
        tool_calls = []
        tool_calls_buffer = {}  # id -> {name, arguments_buffer}
        usage_from_stream: Dict = {}
        last_chunk_generation_id = ""
        send_failure_tracker = _ConsecutiveSendFailureTracker()
        
        def _finalise_buffered_tool_calls() -> None:
            for buffer_key, tc_data in tool_calls_buffer.items():
                try:
                    args = json.loads(tc_data.get('arguments_buffer', '{}'))
                except Exception:
                    args = {}
                tc_id = tc_data.get('id') or str(uuid.uuid4())
                tc_name = tc_data.get('name', '')
                if not tc_name:
                    MCPLogger.log(TOOL_LOG_NAME, f"Skipping tool call with empty name: {buffer_key}")
                    continue
                tool_calls.append({
                    'id': tc_id,
                    'type': 'function',
                    'function': {
                        'name': tc_name,
                        'arguments': json.dumps(args) if isinstance(args, dict) else tc_data.get('arguments_buffer', '{}')
                    }
                })

        try:
            conn = http.client.HTTPSConnection(api_host, timeout=http_timeout_seconds)
            conn.request("POST", api_path,
                       body=json.dumps(request_body), headers=headers)
            
            response = conn.getresponse()
            
            if response.status != 200:
                error_body = response.read().decode('utf-8', errors='replace')
                raise Exception(f"API error {response.status}: {error_body}")
            
            for _event_type, data in _iter_sse_data_events(response, stream_id=stream_id):
                if data == '[DONE]':
                    _finalise_buffered_tool_calls()
                    if last_chunk_generation_id:
                        usage_from_stream.setdefault("generation_id", last_chunk_generation_id)
                    return full_content, tool_calls, usage_from_stream
                
                try:
                    chunk_data = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if chunk_data.get('usage'):
                    usage_from_stream.update(chunk_data['usage'])
                if chunk_data.get('id'):
                    last_chunk_generation_id = chunk_data['id']

                choices = chunk_data.get('choices') or [{}]
                delta = choices[0].get('delta', {})
                
                # Handle text content
                content = delta.get('content', '')
                if content:
                    full_content += content
                    _record_stream_delta(stream_id, full_content)
                    send_succeeded = send_stream_event(handler_info, stream_id, content, False)
                    if send_failure_tracker.should_abort_after(send_succeeded):
                        raise Exception("Client disconnected (consecutive SSE send failures); aborting stream")
                
                # Handle tool calls (streamed incrementally)
                # OpenAI/OpenRouter stream tool calls as:
                # 1. First chunk: {index: 0, id: "call_xxx", function: {name: "tool_name"}}
                # 2. Following chunks: {index: 0, function: {arguments: "..."}}
                # We track by index since id only appears once
                if 'tool_calls' in delta:
                    for tc in delta['tool_calls']:
                        tc_index = tc.get('index', 0)
                        buffer_key = f"idx_{tc_index}"
                        
                        if buffer_key not in tool_calls_buffer:
                            tool_calls_buffer[buffer_key] = {
                                'id': None,  # Will be set from first chunk
                                'name': '',
                                'arguments_buffer': ''
                            }
                        
                        if tc.get('id'):
                            tool_calls_buffer[buffer_key]['id'] = tc['id']
                        
                        if 'function' in tc:
                            func = tc['function']
                            if 'name' in func and func['name']:
                                tool_calls_buffer[buffer_key]['name'] = func['name']
                            if 'arguments' in func:
                                tool_calls_buffer[buffer_key]['arguments_buffer'] += func['arguments']
            
            if _stream_is_cancelled(stream_id):
                raise Exception("Cancelled by user")
            _finalise_buffered_tool_calls()
            if last_chunk_generation_id:
                usage_from_stream.setdefault("generation_id", last_chunk_generation_id)
            return full_content, tool_calls, usage_from_stream
            
        finally:
            if conn:
                conn.close()
    
    def stream_worker():
        """Background worker that handles the streaming + tool loop."""
        nonlocal messages
        
        total_tool_rounds = 0
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        def _accumulate_usage(usage_from_round: Dict) -> None:
            for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage_from_round.get(usage_key), (int, float)):
                    total_usage[usage_key] += usage_from_round[usage_key]
            if usage_from_round.get("cost") is not None:
                total_usage["cost"] = total_usage.get("cost", 0) + usage_from_round["cost"]
            if usage_from_round.get("generation_id"):
                total_usage["generation_id"] = usage_from_round["generation_id"]

        try:
            for round_num in range(max_tool_rounds + 1):
                MCPLogger.log(TOOL_LOG_NAME, f"Streaming round {round_num + 1}, messages: {len(messages)}")
                
                full_content, tool_calls, usage_from_round = stream_one_llm_response(messages)
                _accumulate_usage(usage_from_round)
                
                if not tool_calls:
                    # No tool calls - we're done
                    MCPLogger.log(TOOL_LOG_NAME, f"Streaming complete after {total_tool_rounds} tool rounds")
                    
                    _mark_stream_complete(stream_id, usage=total_usage)
                    send_stream_event(handler_info, stream_id, "", True,
                                    usage=total_usage, tool_rounds=total_tool_rounds)
                    return
                
                # We have tool calls to execute
                total_tool_rounds += 1
                MCPLogger.log(TOOL_LOG_NAME, f"Processing {len(tool_calls)} tool calls in round {total_tool_rounds}")
                
                # Add assistant message with tool calls to conversation
                assistant_msg = {
                    "role": "assistant",
                    "content": full_content if full_content else None,
                    "tool_calls": tool_calls
                }
                messages.append(assistant_msg)
                
                # Execute each tool call
                for tc in tool_calls:
                    if _stream_is_cancelled(stream_id):
                        raise Exception("Cancelled by user")
                    tc_id = tc.get('id', str(uuid.uuid4()))
                    func = tc.get('function', {})
                    tool_name = func.get('name', '')
                    
                    try:
                        arguments = json.loads(func.get('arguments', '{}'))
                    except Exception:
                        arguments = {}
                    
                    # Send tool_call start event
                    send_stream_event(handler_info, stream_id, "", False,
                                    tool_call={
                                        "status": "start",
                                        "id": tc_id,
                                        "name": tool_name,
                                        "arguments": arguments
                                    })
                    
                    # Execute the tool, with heartbeats so clients see progress
                    start_time = time.time()
                    with _PeriodicStreamHeartbeat(handler_info, stream_id):
                        tool_result = execute_mcp_tool(handler_info, tool_name, arguments, allowed_tools, tool_mapping)
                    duration_ms = int((time.time() - start_time) * 1000)
                    
                    # Send tool_call complete event
                    result_preview = tool_result.get('result', tool_result.get('error', ''))[:500]
                    send_stream_event(handler_info, stream_id, "", False,
                                    tool_call={
                                        "status": "complete",
                                        "id": tc_id,
                                        "name": tool_name,
                                        "success": tool_result.get('success', False),
                                        "result_preview": result_preview,
                                        "duration_ms": duration_ms
                                    })
                    
                    # Add tool result to messages
                    if tool_result.get('success'):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tool_result.get('result', '')
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"Error: {tool_result.get('error', 'Unknown error')}"
                        })
                
                # Continue loop - will call LLM again with tool results
            
            # If we get here, we hit max_tool_rounds
            MCPLogger.log(TOOL_LOG_NAME, f"Hit max tool rounds ({max_tool_rounds})")
            _mark_stream_complete(stream_id, usage=total_usage, error=f"Stopped after {max_tool_rounds} tool rounds")
            send_stream_event(handler_info, stream_id, "", True,
                            usage=total_usage, tool_rounds=total_tool_rounds,
                            error=f"Stopped after {max_tool_rounds} tool rounds")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Streaming error: {e}\n{traceback.format_exc()}")
            _mark_stream_complete(stream_id, error=str(e))
            send_stream_event(handler_info, stream_id, "", True, error=str(e))
    
    # Start streaming thread
    thread = threading.Thread(target=stream_worker, daemon=True)
    thread.start()
    
    # Return immediately with stream_id
    return {
        "content": [{"type": "text", "text": json.dumps({
            "stream_id": stream_id,
            "status": "streaming",
            "model": model,
            "provider": provider.value,
            "tools_enabled": bool(tools),
            "allowed_tools": allowed_tools
        })}],
        "isError": False
    }


def chat_tool_loop_streaming_via_blocking_rounds(params: Dict, handler_info: Dict,
                                                 provider: Provider) -> Dict:
    """Streaming tool loop for providers without an SSE tool-calling reader
    (anthropic, ollama). Each round runs the provider's blocking with-tools call;
    the round's text arrives as one delta event, tool calls produce the same
    tool_call start/complete events as the OpenRouter/OpenAI SSE loop, and the
    final event carries accumulated usage. Delta granularity is per round (not
    per token), but the client-facing event protocol is identical.
    """
    tools = params.get('tools', [])
    allowed_tools = params.get('allowed_tools', [])
    tool_mapping = params.get('tool_mapping', {})
    max_tool_rounds = params.get('max_tool_rounds', 10)
    model = params.get('model', '')
    messages = list(params.get('messages', []))

    blocking_round_function_by_provider = {
        Provider.ANTHROPIC: chat_anthropic_with_tools,
        Provider.OLLAMA: chat_ollama_with_tools,
    }
    blocking_round_function = blocking_round_function_by_provider.get(provider)
    if blocking_round_function is None:
        return create_error_response(f"Streaming tool loop not supported for provider {provider.value}", with_readme=False)

    stream_id = str(uuid.uuid4())
    registration_error = _register_new_stream(StreamState(
        stream_id=stream_id,
        provider=provider,
        model=model,
        session_id=handler_info.get('session_id', ''),
        request_id=handler_info.get('request_id', '')
    ))
    if registration_error:
        return create_error_response(registration_error, with_readme=False)

    def stream_worker():
        total_tool_rounds = 0
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        full_content = ""
        send_failure_tracker = _ConsecutiveSendFailureTracker()

        def _accumulate_usage(usage_from_round: Dict) -> None:
            # Anthropic reports input_tokens/output_tokens; ollama prompt/completion
            prompt_tokens = usage_from_round.get("prompt_tokens", usage_from_round.get("input_tokens"))
            completion_tokens = usage_from_round.get("completion_tokens", usage_from_round.get("output_tokens"))
            if isinstance(prompt_tokens, (int, float)):
                total_usage["prompt_tokens"] += prompt_tokens
            if isinstance(completion_tokens, (int, float)):
                total_usage["completion_tokens"] += completion_tokens
            total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]

        try:
            round_params = params.copy()
            for round_num in range(max_tool_rounds + 1):
                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                round_params['messages'] = messages
                MCPLogger.log(TOOL_LOG_NAME, f"Streaming tool loop ({provider.value}) round {round_num + 1}, messages: {len(messages)}")
                round_response = blocking_round_function(round_params, handler_info, tools, stream=False)
                if round_response.get('isError'):
                    round_error_text = round_response.get('content', [{}])[0].get('text', 'provider error')[:500]
                    _mark_stream_complete(stream_id, content=full_content, error=round_error_text)
                    send_stream_event(handler_info, stream_id, "", True, error=round_error_text)
                    return

                response_data = json.loads(round_response['content'][0]['text'])
                message = response_data.get('choices', [{}])[0].get('message', {})
                _accumulate_usage(response_data.get('usage', {}) or {})

                round_text = message.get('content') or ''
                if round_text:
                    full_content += round_text
                    _record_stream_delta(stream_id, full_content)
                    send_succeeded = send_stream_event(handler_info, stream_id, round_text, False)
                    if send_failure_tracker.should_abort_after(send_succeeded):
                        MCPLogger.log(TOOL_LOG_NAME, f"Aborting {provider.value} tool-loop stream {stream_id}: client disconnected")
                        _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                        return

                tool_calls = message.get('tool_calls') or []
                if not tool_calls:
                    _mark_stream_complete(stream_id, content=full_content, usage=total_usage)
                    send_stream_event(handler_info, stream_id, "", True,
                                      usage=total_usage, tool_rounds=total_tool_rounds)
                    return

                total_tool_rounds += 1
                # '' (not None): ollama's message builder forwards content verbatim,
                # and the anthropic converter treats '' and None the same way
                messages.append({
                    "role": "assistant",
                    "content": round_text,
                    "tool_calls": tool_calls
                })

                for tc in tool_calls:
                    if _stream_is_cancelled(stream_id):
                        _mark_stream_complete(stream_id, content=full_content)
                        send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                        return
                    tc_id = tc.get('id', str(uuid.uuid4()))
                    tool_name = tc.get('function', {}).get('name', '')
                    try:
                        arguments = json.loads(tc.get('function', {}).get('arguments', '{}'))
                    except Exception:
                        arguments = {}

                    send_stream_event(handler_info, stream_id, "", False,
                                      tool_call={"status": "start", "id": tc_id,
                                                 "name": tool_name, "arguments": arguments})

                    start_time = time.time()
                    with _PeriodicStreamHeartbeat(handler_info, stream_id):
                        tool_result = execute_mcp_tool(handler_info, tool_name, arguments, allowed_tools, tool_mapping)
                    duration_ms = int((time.time() - start_time) * 1000)

                    result_preview = str(tool_result.get('result', tool_result.get('error', '')))[:500]
                    send_stream_event(handler_info, stream_id, "", False,
                                      tool_call={"status": "complete", "id": tc_id, "name": tool_name,
                                                 "success": tool_result.get('success', False),
                                                 "result_preview": result_preview,
                                                 "duration_ms": duration_ms})

                    if tool_result.get('success'):
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": tool_result.get('result', '')})
                    else:
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": f"Error: {tool_result.get('error', 'Unknown error')}"})

            MCPLogger.log(TOOL_LOG_NAME, f"Hit max tool rounds ({max_tool_rounds}) in {provider.value} streaming tool loop")
            _mark_stream_complete(stream_id, content=full_content, usage=total_usage,
                                  error=f"Stopped after {max_tool_rounds} tool rounds")
            send_stream_event(handler_info, stream_id, "", True,
                              usage=total_usage, tool_rounds=total_tool_rounds,
                              error=f"Stopped after {max_tool_rounds} tool rounds")

        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Streaming tool loop error ({provider.value}): {e}\n{traceback.format_exc()}")
            _mark_stream_complete(stream_id, error=str(e))
            send_stream_event(handler_info, stream_id, "", True, error=str(e))

    thread = threading.Thread(target=stream_worker, daemon=True)
    thread.start()

    return {
        "content": [{"type": "text", "text": json.dumps({
            "stream_id": stream_id,
            "status": "streaming",
            "model": model,
            "provider": provider.value,
            "tools_enabled": bool(tools),
            "allowed_tools": allowed_tools,
            "delta_granularity": "per_round"
        })}],
        "isError": False
    }


# ============================================================================
# Tool Calling Support
# ============================================================================

def _llm_delegation_denied_tool_names() -> frozenset:
    """Tool names never delegable to an LLM (base names plus the suffixed variants),
    extendable via settings[0].llm_delegation_denied_tools in config."""
    denied_basenames = set(_LLM_DELEGATION_DENIED_TOOL_BASENAMES)
    try:
        config = get_config_manager().load_config()
        settings_list = config.get('settings', [])
        if settings_list and isinstance(settings_list[0], dict):
            configured_denied = settings_list[0].get('llm_delegation_denied_tools', [])
            if isinstance(configured_denied, list):
                denied_basenames.update(str(name) for name in configured_denied)
    except Exception:
        pass
    denied_with_suffix = {f"{name}{TOOL_NAME_SUFFIX}" for name in denied_basenames}
    return frozenset(denied_basenames | denied_with_suffix)


def execute_mcp_tool(handler_info: Dict, tool_name: str, arguments: Dict, 
                     allowed_tools: List[str], tool_mapping: Dict[str, str] = None) -> Dict:
    """Execute an MCP tool and return the result.
    
    Args:
        handler_info: Contains responder for calling tools
        tool_name: Name of the tool as called by the LLM
        arguments: Arguments to pass to the tool
        allowed_tools: List of allowed MCP tool names, or ['*'] for all
        tool_mapping: Optional dict mapping LLM tool names to MCP tool names
    
    Returns:
        Dict with 'success', 'result' or 'error'
    """
    try:
        # Apply tool name mapping if provided
        mcp_tool_name = tool_name
        if tool_mapping and tool_name in tool_mapping:
            mcp_tool_name = tool_mapping[tool_name]
            MCPLogger.log(TOOL_LOG_NAME, f"Mapped LLM tool '{tool_name}' -> MCP tool '{mcp_tool_name}'")
        
        # Deny-list check: high-risk tools are never delegable, even with ['*']
        if mcp_tool_name in _llm_delegation_denied_tool_names():
            MCPLogger.log(TOOL_LOG_NAME, f"DELEGATED-TOOL-CALL DENIED: '{mcp_tool_name}' is on the delegation deny-list")
            return {
                "success": False,
                "error": f"Tool '{mcp_tool_name}' is never delegable to an LLM (server deny-list)"
            }
        
        # Security check: verify MCP tool is allowed. The ['*'] wildcard can be
        # disabled server-wide (config gate) so operators can force explicit
        # tool lists on LAN/WAN-exposed servers.
        if '*' in allowed_tools and not _get_llm_settings_flag('llm_allow_wildcard_tool_delegation', True):
            MCPLogger.log(TOOL_LOG_NAME, f"DELEGATED-TOOL-CALL DENIED: allowed_tools ['*'] is disabled by server config")
            return {
                "success": False,
                "error": "allowed_tools ['*'] is disabled on this server (settings[0].llm_allow_wildcard_tool_delegation is false). List the permitted tools explicitly."
            }
        if '*' not in allowed_tools and mcp_tool_name not in allowed_tools:
            return {
                "success": False,
                "error": f"Tool '{mcp_tool_name}' is not in the allowed_tools list. Allowed: {allowed_tools}"
            }
        
        responder = handler_info.get('responder')
        if not responder:
            return {
                "success": False,
                "error": "No responder available for tool execution"
            }
        
        # Check if tool exists
        if not hasattr(responder, 'tool_handlers') or mcp_tool_name not in responder.tool_handlers:
            return {
                "success": False,
                "error": f"Tool '{mcp_tool_name}' not found on this server"
            }
        
        # Distinct log line for every LLM-delegated call; arguments truncated
        # (they can contain secrets or huge payloads)
        MCPLogger.log(TOOL_LOG_NAME, f"DELEGATED-TOOL-CALL: '{mcp_tool_name}' args: {str(arguments)[:300]}")
        
        # MCP tools expect arguments wrapped in {"input": {...}}
        # If the arguments don't already have an "input" key, wrap them
        if "input" not in arguments:
            wrapped_arguments = {"input": arguments.copy()}
        else:
            wrapped_arguments = {"input": arguments["input"].copy() if isinstance(arguments.get("input"), dict) else arguments["input"]}
        
        # Get the target tool's unlock token and inject it for inter-tool calls
        # This allows the LLM to call tools without knowing their tokens
        try:
            from ragtag.tools import TOOL_TOKENS
            if mcp_tool_name in TOOL_TOKENS:
                target_token = TOOL_TOKENS[mcp_tool_name]
                # Use inter-tool token format: -<caller_token>-<target_token>
                inter_tool_token = f"-{TOOL_UNLOCK_TOKEN}-{target_token}"
                if isinstance(wrapped_arguments.get("input"), dict):
                    wrapped_arguments["input"]["tool_unlock_token"] = inter_tool_token
                    MCPLogger.log(TOOL_LOG_NAME, f"Injected inter-tool token for '{mcp_tool_name}'")
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, f"TOOL_TOKENS not available, proceeding without token injection")
        
        # Call the tool using the server's internal method
        result = responder.call_tool_internal(
            tool_name=mcp_tool_name,
            parameters=wrapped_arguments,
            calling_tool="llm"
        )
        
        MCPLogger.log(TOOL_LOG_NAME, f"DELEGATED-TOOL-CALL: '{mcp_tool_name}' returned: {str(result)[:300]}")
        
        # Extract the text content from the result
        if isinstance(result, dict):
            if result.get('isError'):
                content = result.get('content', [{}])
                error_text = content[0].get('text', 'Unknown error') if content else 'Unknown error'
                return {
                    "success": False,
                    "error": error_text
                }
            else:
                content = result.get('content', [{}])
                result_text = content[0].get('text', str(result)) if content else str(result)
                return {
                    "success": True,
                    "result": result_text
                }
        else:
            return {
                "success": True,
                "result": str(result)
            }
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error executing tool '{tool_name}': {e}")
        return {
            "success": False,
            "error": f"Tool execution failed: {str(e)}"
        }


def _extract_tool_calls_from_openai_compatible_response(result: Dict, tools: List[Dict]) -> Dict:
    """Extract tool_calls from an OpenAI-compatible response, with JSON-in-content fallback.

    Some servers (mlx_vlm, llama.cpp) return native tool_calls in the response. Others
    return the tool call as JSON in the message content. This function normalises both
    cases into the standard OpenAI tool_calls format so callers get a consistent result.

    Mutates and returns `result` — the original dict is modified in place.
    """
    try:
        message = result.get('choices', [{}])[0].get('message', {})
    except (IndexError, AttributeError):
        return result

    if message.get('tool_calls'):
        result['choices'][0]['finish_reason'] = 'tool_calls'
        MCPLogger.log(TOOL_LOG_NAME, f"Native tool_calls found in response: {len(message['tool_calls'])} calls")
        return result

    content = (message.get('content') or '').strip()
    if not content:
        return result

    for start_marker, end_marker in [('<tool_call>', '</tool_call>'), ('<tool_call>\n', '\n</tool_call>')]:
        if start_marker in content and end_marker in content:
            inner = content.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
            content = inner
            break

    if not (content.startswith('{') and content.endswith('}')):
        return result

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return result

    if not isinstance(parsed, dict) or 'name' not in parsed:
        return result

    tool_name = parsed.get('name', '')
    tool_args = parsed.get('parameters') or parsed.get('arguments', {})

    matched_tool = None
    for tool in tools:
        func = tool.get('function', {})
        if func.get('name', '').lower() == tool_name.lower():
            matched_tool = func.get('name')
            break

    if not matched_tool and len(tools) == 1:
        generic_names = ['function', 'tool', 'call', 'execute', 'run']
        if tool_name.lower() in generic_names or not tool_name:
            func = tools[0].get('function', {})
            matched_tool = func.get('name')
            MCPLogger.log(TOOL_LOG_NAME, f"Using single available tool '{matched_tool}' for generic name '{tool_name}'")

    if matched_tool:
        message['content'] = ''
        message['tool_calls'] = [{
            "id": str(uuid.uuid4()),
            "type": "function",
            "function": {
                "name": matched_tool,
                "arguments": json.dumps(tool_args) if isinstance(tool_args, dict) else tool_args
            }
        }]
        result['choices'][0]['message'] = message
        result['choices'][0]['finish_reason'] = 'tool_calls'
        MCPLogger.log(TOOL_LOG_NAME, f"Extracted tool call from content: {matched_tool}")

    return result


def process_tool_calls_and_continue(
    params: Dict, 
    handler_info: Dict, 
    response_message: Dict,
    messages: List[Dict],
    tools: List[Dict],
    allowed_tools: List[str],
    max_tool_rounds: int,
    current_round: int = 1,
    tool_mapping: Dict[str, str] = None
) -> Dict:
    """Process tool calls from LLM response and continue the conversation.
    
    This implements the agentic loop:
    1. Check if response has tool_calls
    2. Execute each tool call
    3. Add tool results to messages
    4. Call LLM again
    5. Repeat until no more tool calls or max rounds reached
    
    Returns the final response.
    """
    tool_calls = response_message.get('tool_calls', [])
    
    if not tool_calls:
        # No tool calls, return the response as-is
        return {
            "content": [{"type": "text", "text": json.dumps({
                "message": response_message,
                "tool_rounds_used": current_round - 1,
                "finish_reason": "stop"
            })}],
            "isError": False
        }
    
    if current_round > max_tool_rounds:
        MCPLogger.log(TOOL_LOG_NAME, f"Max tool rounds ({max_tool_rounds}) reached")
        return {
            "content": [{"type": "text", "text": json.dumps({
                "message": response_message,
                "tool_rounds_used": current_round,
                "finish_reason": "max_tool_rounds",
                "warning": f"Stopped after {max_tool_rounds} tool rounds"
            })}],
            "isError": False
        }
    
    MCPLogger.log(TOOL_LOG_NAME, f"Processing {len(tool_calls)} tool calls (round {current_round})")
    
    # Add the assistant's response with tool calls to messages
    messages.append({
        "role": "assistant",
        "content": response_message.get('content'),
        "tool_calls": tool_calls
    })
    
    # Execute each tool call and collect results
    for tool_call in tool_calls:
        tool_call_id = tool_call.get('id', str(uuid.uuid4()))
        function_info = tool_call.get('function', {})
        tool_name = function_info.get('name', '')
        
        # Parse arguments
        try:
            arguments_str = function_info.get('arguments', '{}')
            arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except json.JSONDecodeError:
            arguments = {}
        
        MCPLogger.log(TOOL_LOG_NAME, f"Calling tool '{tool_name}' with args: {str(arguments)}")
        
        # Execute the tool
        tool_result = execute_mcp_tool(handler_info, tool_name, arguments, allowed_tools, tool_mapping)
        
        # Add tool result to messages
        if tool_result['success']:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_result['result']
            })
        else:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": f"Error: {tool_result['error']}"
            })
    
    # Make another LLM call with the updated messages
    # We need to call the appropriate provider again
    provider_str = params.get('provider', 'openrouter')
    
    # Update params with new messages
    updated_params = params.copy()
    updated_params['messages'] = messages
    
    # Make the follow-up call (non-streaming for tool loops)
    if provider_str == 'ollama':
        follow_up_response = chat_ollama_with_tools(updated_params, handler_info, tools, stream=False)
    elif provider_str == 'openrouter':
        follow_up_response = chat_openrouter_with_tools(updated_params, handler_info, tools, stream=False)
    elif provider_str == 'openai':
        follow_up_response = chat_openai_with_tools(updated_params, handler_info, tools, stream=False)
    elif provider_str == 'anthropic':
        follow_up_response = chat_anthropic_with_tools(updated_params, handler_info, tools, stream=False)
    else:
        # For local, llama_cpp, and custom, tool calling is not supported
        return {
            "content": [{"type": "text", "text": json.dumps({
                "message": response_message,
                "tool_rounds_used": current_round,
                "finish_reason": "provider_no_tool_support",
                "warning": f"Provider '{provider_str}' does not support tool calling continuation. Use 'ollama' for local tool calling."
            })}],
            "isError": False
        }
    
    # Check if follow-up had an error
    if follow_up_response.get('isError'):
        return follow_up_response
    
    # Parse the follow-up response
    try:
        response_text = follow_up_response['content'][0]['text']
        response_data = json.loads(response_text)
        new_message = response_data.get('choices', [{}])[0].get('message', {})
        
        # Recursively process if there are more tool calls
        return process_tool_calls_and_continue(
            params, handler_info, new_message, messages, tools, 
            allowed_tools, max_tool_rounds, current_round + 1, tool_mapping
        )
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error parsing follow-up response: {e}")
        return follow_up_response


# ============================================================================
# Provider Implementations (with Tool Support)
# ============================================================================

def chat_openrouter_with_tools(params: Dict, handler_info: Dict, tools: List[Dict], stream: bool) -> Dict:
    """Handle chat completion via OpenRouter API with tool support."""
    api_key = get_api_key(Provider.OPENROUTER, params.get('api_key'))
    if not api_key:
        return create_error_response("OpenRouter API key not configured. Set OPENROUTER_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'anthropic/claude-3-5-sonnet')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    tool_choice = params.get('tool_choice', 'auto')
    
    # Build request
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream
    }
    request_body.update(_extract_extra_provider_specific_parameters(params))
    
    # Add tools if provided
    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = tool_choice
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://aurafriday.com",
        "X-Title": "AuraFriday LLM"
    }
    
    # Non-streaming mode for tool calling
    try:
        status_code, response_data = _http_post_json(
            "openrouter.ai", "/api/v1/chat/completions", headers, request_body,
            timeout=_get_provider_http_timeout_seconds(params), retries=params.get('retries', 0))
        
        if status_code == 200:
            result = json.loads(response_data)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
        else:
            return create_error_response(f"OpenRouter API error {status_code}: {response_data}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"OpenRouter request failed: {e}", with_readme=False)


def chat_openai_with_tools(params: Dict, handler_info: Dict, tools: List[Dict], stream: bool) -> Dict:
    """Handle chat completion via OpenAI API with tool support."""
    api_key = get_api_key(Provider.OPENAI, params.get('api_key'))
    if not api_key:
        return create_error_response("OpenAI API key not configured. Set OPENAI_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'gpt-4o')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    tool_choice = params.get('tool_choice', 'auto')
    
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        _openai_token_limit_parameter_name_for_model(model): max_tokens,
        "stream": stream
    }
    request_body.update(_extract_extra_provider_specific_parameters(params))
    
    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = tool_choice
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        status_code, response_data = _http_post_json(
            "api.openai.com", "/v1/chat/completions", headers, request_body,
            timeout=_get_provider_http_timeout_seconds(params), retries=params.get('retries', 0))
        
        if status_code == 200:
            return {
                "content": [{"type": "text", "text": response_data}],
                "isError": False
            }
        else:
            return create_error_response(f"OpenAI API error {status_code}: {response_data}", with_readme=False)
    except Exception as e:
        return create_error_response(f"OpenAI request failed: {e}", with_readme=False)


def chat_anthropic_with_tools(params: Dict, handler_info: Dict, tools: List[Dict], stream: bool) -> Dict:
    """Handle chat completion via Anthropic API with tool support.

    Messages arrive in OpenAI format (including role:"tool" results and assistant
    tool_calls appended by the tool loop) and are translated to Anthropic format,
    so multi-round tool conversations work instead of 400ing on round 2.
    """
    api_key = get_api_key(Provider.ANTHROPIC, params.get('api_key'))
    if not api_key:
        return create_error_response("Anthropic API key not configured. Set ANTHROPIC_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'claude-3-5-sonnet-20241022')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    tool_choice = params.get('tool_choice', 'auto')
    
    system_content, anthropic_messages = _convert_openai_messages_to_anthropic_format(messages)
    
    request_body = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "stream": stream,
        # Anthropic caps temperature at 1.0 (our schema allows up to 2.0)
        "temperature": min(max(float(temperature), 0.0), 1.0)
    }
    
    if system_content:
        request_body["system"] = system_content

    extra_params = _extract_extra_provider_specific_parameters(params)
    _apply_anthropic_response_format_mapping(request_body, extra_params)
    request_body.update(extra_params)
    
    # Convert tools to Anthropic format
    if tools:
        anthropic_tools = []
        for tool in tools:
            func = tool.get('function', {})
            anthropic_tools.append({
                "name": func.get('name'),
                "description": func.get('description', ''),
                "input_schema": func.get('parameters', {"type": "object", "properties": {}})
            })
        request_body["tools"] = anthropic_tools
        
        # Anthropic uses different tool_choice format
        if tool_choice == 'auto':
            request_body["tool_choice"] = {"type": "auto"}
        elif tool_choice == 'none':
            pass  # Don't include tool_choice
        else:
            request_body["tool_choice"] = {"type": "tool", "name": tool_choice}
    
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    
    try:
        status_code, response_data = _http_post_json(
            "api.anthropic.com", "/v1/messages", headers, request_body,
            timeout=_get_provider_http_timeout_seconds(params), retries=params.get('retries', 0))
        
        if status_code == 200:
            # Convert Anthropic response to OpenAI format for consistency
            anthropic_response = json.loads(response_data)
            
            # Check for tool use in Anthropic response
            tool_calls = []
            content_text = ""
            
            for content_block in anthropic_response.get("content", []):
                if content_block.get("type") == "text":
                    content_text += content_block.get("text", "")
                elif content_block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": content_block.get("id"),
                        "type": "function",
                        "function": {
                            "name": content_block.get("name"),
                            "arguments": json.dumps(content_block.get("input", {}))
                        }
                    })
            
            openai_format = {
                "id": anthropic_response.get("id"),
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content_text if content_text else None,
                        "tool_calls": tool_calls if tool_calls else None
                    },
                    "finish_reason": "tool_calls" if tool_calls else _map_anthropic_stop_reason_to_openai(anthropic_response.get("stop_reason"))
                }],
                "usage": anthropic_response.get("usage", {})
            }
            
            # Clean up None values
            if not openai_format["choices"][0]["message"]["tool_calls"]:
                del openai_format["choices"][0]["message"]["tool_calls"]
            if openai_format["choices"][0]["message"]["content"] is None:
                openai_format["choices"][0]["message"]["content"] = ""
            
            return {
                "content": [{"type": "text", "text": json.dumps(openai_format)}],
                "isError": False
            }
        else:
            return create_error_response(f"Anthropic API error {status_code}: {response_data}", with_readme=False)
    except Exception as e:
        return create_error_response(f"Anthropic request failed: {e}", with_readme=False)


# ============================================================================
# Provider Implementations (Original - No Tool Support)
# ============================================================================

def chat_openrouter(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via OpenRouter API."""
    import http.client
    
    api_key = get_api_key(Provider.OPENROUTER, params.get('api_key'))
    if not api_key:
        return create_error_response("OpenRouter API key not configured. Set OPENROUTER_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'anthropic/claude-3-5-sonnet')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)
    
    # Build request
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        # OpenRouter usage accounting: real token counts + cost in the response
        "usage": {"include": True}
    }
    if stream:
        request_body["stream_options"] = {"include_usage": True}
    request_body.update(_extract_extra_provider_specific_parameters(params))
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://aurafriday.com",
        "X-Title": "AuraFriday LLM"
    }
    
    if stream:
        # Streaming mode - spawn thread and return immediately
        stream_id = str(uuid.uuid4())
        
        registration_error = _register_new_stream(StreamState(
            stream_id=stream_id,
            provider=Provider.OPENROUTER,
            model=model,
            session_id=handler_info.get('session_id', ''),
            request_id=handler_info.get('request_id', '')
        ))
        if registration_error:
            return create_error_response(registration_error, with_readme=False)
        
        def stream_worker():
            """Worker thread for streaming OpenRouter response."""
            conn = None
            full_content = ""
            usage_from_stream: Dict = {}
            generation_id = ""
            send_failure_tracker = _ConsecutiveSendFailureTracker()
            try:
                conn = http.client.HTTPSConnection("openrouter.ai", timeout=http_timeout_seconds)
                conn.request("POST", "/api/v1/chat/completions", 
                           body=json.dumps(request_body), headers=headers)
                
                response = conn.getresponse()
                
                if response.status != 200:
                    error_body = response.read().decode('utf-8', errors='replace')
                    _mark_stream_complete(stream_id, error=f"API error {response.status}")
                    send_stream_event(handler_info, stream_id, "", True, 
                                    error=f"API error {response.status}: {error_body}")
                    return
                
                for _event_type, data in _iter_sse_data_events(response, stream_id=stream_id):
                    if data == '[DONE]':
                        final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                        if generation_id:
                            final_usage.setdefault("generation_id", generation_id)
                        _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                        send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                        return
                    
                    try:
                        chunk_data = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk_data.get('usage'):
                        usage_from_stream.update(chunk_data['usage'])
                    if chunk_data.get('id'):
                        generation_id = chunk_data['id']
                    choices = chunk_data.get('choices') or [{}]
                    delta = choices[0].get('delta', {}).get('content', '')
                    
                    if delta:
                        full_content += delta
                        _record_stream_delta(stream_id, full_content)
                        send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                        if send_failure_tracker.should_abort_after(send_succeeded):
                            MCPLogger.log(TOOL_LOG_NAME, f"Aborting stream {stream_id}: client disconnected")
                            _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                            return
                
                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                # If we get here without [DONE], send final event
                final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                                
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Stream error: {e}")
                _mark_stream_complete(stream_id, error=str(e))
                send_stream_event(handler_info, stream_id, "", True, error=str(e))
            finally:
                if conn:
                    conn.close()
        
        # Start streaming thread
        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()
        
        # Return immediately with stream_id
        return {
            "content": [{"type": "text", "text": json.dumps({
                "stream_id": stream_id,
                "status": "streaming",
                "model": model
            })}],
            "isError": False
        }
    
    else:
        # Non-streaming mode - wait for complete response
        try:
            status_code, response_data = _http_post_json(
                "openrouter.ai", "/api/v1/chat/completions", headers, request_body,
                timeout=http_timeout_seconds, retries=params.get('retries', 0))
            
            if status_code == 200:
                result = json.loads(response_data)
                return {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
            else:
                return create_error_response(f"OpenRouter API error {status_code}: {response_data}", with_readme=False)
                
        except Exception as e:
            return create_error_response(f"OpenRouter request failed: {e}", with_readme=False)


def chat_local(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via local transformers model."""
    try:
        model_name = params.get('model', 'Qwen/Qwen2.5-0.5B-Instruct')
        messages = params.get('messages', [])
        temperature = params.get('temperature', 0.7)
        max_tokens = params.get('max_tokens', 1000)
        device = params.get('device', 'auto')
        # transformers raises on temperature<=0 with sampling enabled - use greedy instead
        do_sample = isinstance(temperature, (int, float)) and not isinstance(temperature, bool) and temperature > 0
        
        if stream:
            # Streaming mode for local models
            stream_id = str(uuid.uuid4())
            
            registration_error = _register_new_stream(StreamState(
                stream_id=stream_id,
                provider=Provider.LOCAL,
                model=model_name,
                session_id=handler_info.get('session_id', ''),
                request_id=handler_info.get('request_id', '')
            ))
            if registration_error:
                return create_error_response(registration_error, with_readme=False)
            
            def stream_worker():
                """Worker thread for streaming local model response."""
                try:
                    # Load model using local functions
                    torch = ensure_torch()
                    transformers = ensure_transformers()
                    
                    model_obj, tokenizer, device_used = load_model(model_name, device)
                    
                    # Apply chat template
                    text = tokenizer.apply_chat_template(
                        messages,
                        tokenize=False,
                        add_generation_prompt=True
                    )
                    
                    model_inputs = tokenizer([text], return_tensors="pt").to(model_obj.device)
                    
                    # Use TextIteratorStreamer for streaming
                    from transformers import TextIteratorStreamer
                    
                    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
                    
                    class _CancellationStoppingCriteria(transformers.StoppingCriteria):
                        """Stops generation when the stream is cancelled via cancel_stream."""
                        def __call__(self, input_ids, scores, **kwargs):
                            return _stream_is_cancelled(stream_id)

                    generation_kwargs = {
                        **model_inputs,
                        "max_new_tokens": max_tokens,
                        "do_sample": do_sample,
                        "streamer": streamer,
                        "stopping_criteria": transformers.StoppingCriteriaList([_CancellationStoppingCriteria()])
                    }
                    if do_sample:
                        generation_kwargs["temperature"] = temperature
                    
                    # Start generation in separate thread
                    gen_thread = threading.Thread(target=model_obj.generate, kwargs=generation_kwargs)
                    gen_thread.start()
                    
                    # Stream tokens as they're generated
                    full_content = ""
                    token_count = 0
                    send_failure_tracker = _ConsecutiveSendFailureTracker()
                    
                    for text_chunk in streamer:
                        if text_chunk:
                            full_content += text_chunk
                            token_count += 1
                            _record_stream_delta(stream_id, full_content)
                            
                            # Send delta to client
                            send_succeeded = send_stream_event(handler_info, stream_id, text_chunk, False)
                            if send_failure_tracker.should_abort_after(send_succeeded):
                                MCPLogger.log(TOOL_LOG_NAME, f"Aborting local stream {stream_id}: client disconnected")
                                break
                    
                    gen_thread.join()
                    
                    if _stream_is_cancelled(stream_id):
                        _mark_stream_complete(stream_id, content=full_content)
                        send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                        return

                    # Send completion event (streamer chunks approximate tokens)
                    final_usage = {"completion_tokens": token_count, "estimated": True}
                    _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                    send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Local stream error: {e}")
                    _mark_stream_complete(stream_id, error=str(e))
                    send_stream_event(handler_info, stream_id, "", True, error=str(e))
            
            # Start streaming thread
            thread = threading.Thread(target=stream_worker, daemon=True)
            thread.start()
            
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "streaming",
                    "model": model_name
                })}],
                "isError": False
            }
        
        else:
            # Non-streaming - run synchronously
            try:
                torch = ensure_torch()
                transformers = ensure_transformers()
                
                model_obj, tokenizer, device_used = load_model(model_name, device)
                
                # Apply chat template
                text = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True
                )
                
                model_inputs = tokenizer([text], return_tensors="pt").to(model_obj.device)
                
                # Generate
                generate_kwargs = {
                    "max_new_tokens": max_tokens,
                    "do_sample": do_sample
                }
                if do_sample:
                    generate_kwargs["temperature"] = temperature
                generated_ids = model_obj.generate(
                    **model_inputs,
                    **generate_kwargs
                )
                
                # Decode only new tokens
                generated_ids = [
                    output_ids[len(input_ids):] 
                    for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
                ]
                
                response_text = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
                
                # Return OpenAI-compatible format
                result = {
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
                        "prompt_tokens": len(model_inputs.input_ids[0]),
                        "completion_tokens": len(generated_ids[0]),
                        "total_tokens": len(model_inputs.input_ids[0]) + len(generated_ids[0])
                    }
                }
                
                return {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
                
            except Exception as e:
                return create_error_response(f"Local model error: {e}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Local model error: {e}", with_readme=False)


def chat_openai(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via direct OpenAI API."""
    import http.client
    
    api_key = get_api_key(Provider.OPENAI, params.get('api_key'))
    if not api_key:
        return create_error_response("OpenAI API key not configured. Set OPENAI_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'gpt-4o')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)
    
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        _openai_token_limit_parameter_name_for_model(model): max_tokens,
        "stream": stream
    }
    if stream:
        request_body["stream_options"] = {"include_usage": True}
    request_body.update(_extract_extra_provider_specific_parameters(params))
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    if stream:
        stream_id = str(uuid.uuid4())
        
        registration_error = _register_new_stream(StreamState(
            stream_id=stream_id,
            provider=Provider.OPENAI,
            model=model,
            session_id=handler_info.get('session_id', ''),
            request_id=handler_info.get('request_id', '')
        ))
        if registration_error:
            return create_error_response(registration_error, with_readme=False)
        
        def stream_worker():
            conn = None
            full_content = ""
            usage_from_stream: Dict = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()
            try:
                conn = http.client.HTTPSConnection("api.openai.com", timeout=http_timeout_seconds)
                conn.request("POST", "/v1/chat/completions",
                           body=json.dumps(request_body), headers=headers)
                
                response = conn.getresponse()
                
                if response.status != 200:
                    error_body = response.read().decode('utf-8', errors='replace')
                    _mark_stream_complete(stream_id, error=f"API error {response.status}")
                    send_stream_event(handler_info, stream_id, "", True,
                                    error=f"API error {response.status}: {error_body}")
                    return
                
                for _event_type, data in _iter_sse_data_events(response, stream_id=stream_id):
                    if data == '[DONE]':
                        final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                        _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                        send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                        return
                    
                    try:
                        chunk_data = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk_data.get('usage'):
                        usage_from_stream.update(chunk_data['usage'])
                    choices = chunk_data.get('choices') or [{}]
                    delta = choices[0].get('delta', {}).get('content', '')
                    if delta:
                        full_content += delta
                        _record_stream_delta(stream_id, full_content)
                        send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                        if send_failure_tracker.should_abort_after(send_succeeded):
                            MCPLogger.log(TOOL_LOG_NAME, f"Aborting stream {stream_id}: client disconnected")
                            _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                            return
                
                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                                
            except Exception as e:
                _mark_stream_complete(stream_id, error=str(e))
                send_stream_event(handler_info, stream_id, "", True, error=str(e))
            finally:
                if conn:
                    conn.close()
        
        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()
        
        return {
            "content": [{"type": "text", "text": json.dumps({
                "stream_id": stream_id,
                "status": "streaming",
                "model": model
            })}],
            "isError": False
        }
    
    else:
        try:
            status_code, response_data = _http_post_json(
                "api.openai.com", "/v1/chat/completions", headers, request_body,
                timeout=http_timeout_seconds, retries=params.get('retries', 0))
            
            if status_code == 200:
                return {
                    "content": [{"type": "text", "text": response_data}],
                    "isError": False
                }
            else:
                return create_error_response(f"OpenAI API error {status_code}: {response_data}", with_readme=False)
        except Exception as e:
            return create_error_response(f"OpenAI request failed: {e}", with_readme=False)


def chat_anthropic(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via direct Anthropic API."""
    import http.client
    
    api_key = get_api_key(Provider.ANTHROPIC, params.get('api_key'))
    if not api_key:
        return create_error_response("Anthropic API key not configured. Set ANTHROPIC_API_KEY in config.", with_readme=False)
    
    model = params.get('model', 'claude-3-5-sonnet-20241022')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)
    
    # Concatenates ALL system messages and translates tool-loop message shapes
    system_content, anthropic_messages = _convert_openai_messages_to_anthropic_format(messages)
    
    request_body = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "stream": stream,
        # Anthropic caps temperature at 1.0 (our schema allows up to 2.0)
        "temperature": min(max(float(temperature), 0.0), 1.0)
    }
    
    if system_content:
        request_body["system"] = system_content
    extra_params = _extract_extra_provider_specific_parameters(params)
    _apply_anthropic_response_format_mapping(request_body, extra_params)
    request_body.update(extra_params)
    
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    
    if stream:
        stream_id = str(uuid.uuid4())
        
        registration_error = _register_new_stream(StreamState(
            stream_id=stream_id,
            provider=Provider.ANTHROPIC,
            model=model,
            session_id=handler_info.get('session_id', ''),
            request_id=handler_info.get('request_id', '')
        ))
        if registration_error:
            return create_error_response(registration_error, with_readme=False)
        
        def stream_worker():
            conn = None
            full_content = ""
            usage_from_stream: Dict = {}
            stop_reason_from_stream: Optional[str] = None
            send_failure_tracker = _ConsecutiveSendFailureTracker()
            try:
                conn = http.client.HTTPSConnection("api.anthropic.com", timeout=http_timeout_seconds)
                conn.request("POST", "/v1/messages",
                           body=json.dumps(request_body), headers=headers)
                
                response = conn.getresponse()
                
                if response.status != 200:
                    error_body = response.read().decode('utf-8', errors='replace')
                    _mark_stream_complete(stream_id, error=f"API error {response.status}")
                    send_stream_event(handler_info, stream_id, "", True,
                                    error=f"API error {response.status}: {error_body}")
                    return
                
                for event_type, event_data in _iter_sse_data_events(response, stream_id=stream_id):
                    if event_type == 'content_block_delta' and event_data:
                        try:
                            data = json.loads(event_data)
                        except json.JSONDecodeError:
                            continue
                        delta_object = data.get('delta', {})
                        # Only text deltas are streamed; thinking_delta / input_json_delta
                        # carry no user-visible text (tools+stream isn't routed here)
                        delta = delta_object.get('text', '') if delta_object.get('type') != 'thinking_delta' else ''
                        if delta:
                            full_content += delta
                            _record_stream_delta(stream_id, full_content)
                            send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                            if send_failure_tracker.should_abort_after(send_succeeded):
                                MCPLogger.log(TOOL_LOG_NAME, f"Aborting stream {stream_id}: client disconnected")
                                _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                return
                    
                    elif event_type == 'message_start' and event_data:
                        # Real input token count arrives in the message_start event
                        try:
                            usage_from_stream.update(json.loads(event_data).get('message', {}).get('usage', {}) or {})
                        except json.JSONDecodeError:
                            pass

                    elif event_type == 'message_delta' and event_data:
                        # Real output token count + stop_reason arrive in message_delta
                        try:
                            message_delta_data = json.loads(event_data)
                            usage_from_stream.update(message_delta_data.get('usage', {}) or {})
                            stop_reason_from_stream = message_delta_data.get('delta', {}).get('stop_reason') or stop_reason_from_stream
                        except json.JSONDecodeError:
                            pass

                    elif event_type == 'message_stop':
                        final_usage = dict(usage_from_stream) if usage_from_stream else {"total_tokens": len(full_content.split()), "estimated": True}
                        if 'input_tokens' in final_usage or 'output_tokens' in final_usage:
                            final_usage.setdefault("prompt_tokens", final_usage.get('input_tokens', 0))
                            final_usage.setdefault("completion_tokens", final_usage.get('output_tokens', 0))
                            final_usage.setdefault("total_tokens", final_usage.get('input_tokens', 0) + final_usage.get('output_tokens', 0))
                        if stop_reason_from_stream:
                            final_usage["finish_reason"] = _map_anthropic_stop_reason_to_openai(stop_reason_from_stream)
                        _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                        send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                        return
                
                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                                
            except Exception as e:
                _mark_stream_complete(stream_id, error=str(e))
                send_stream_event(handler_info, stream_id, "", True, error=str(e))
            finally:
                if conn:
                    conn.close()
        
        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()
        
        return {
            "content": [{"type": "text", "text": json.dumps({
                "stream_id": stream_id,
                "status": "streaming",
                "model": model
            })}],
            "isError": False
        }
    
    else:
        try:
            status_code, response_data = _http_post_json(
                "api.anthropic.com", "/v1/messages", headers, request_body,
                timeout=http_timeout_seconds, retries=params.get('retries', 0))
            
            if status_code == 200:
                # Convert Anthropic response to OpenAI format for consistency
                anthropic_response = json.loads(response_data)
                # Iterate ALL content blocks: a leading thinking/tool_use block must
                # not blank the text, and tool_use blocks surface as tool_calls
                content_text = ""
                tool_calls = []
                for content_block in anthropic_response.get("content", []):
                    if content_block.get("type") == "text":
                        content_text += content_block.get("text", "")
                    elif content_block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": content_block.get("id"),
                            "type": "function",
                            "function": {
                                "name": content_block.get("name"),
                                "arguments": json.dumps(content_block.get("input", {}))
                            }
                        })
                message_payload = {
                    "role": "assistant",
                    "content": content_text
                }
                if tool_calls:
                    message_payload["tool_calls"] = tool_calls
                openai_format = {
                    "id": anthropic_response.get("id"),
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": message_payload,
                        "finish_reason": _map_anthropic_stop_reason_to_openai(anthropic_response.get("stop_reason"))
                    }],
                    "usage": anthropic_response.get("usage", {})
                }
                return {
                    "content": [{"type": "text", "text": json.dumps(openai_format)}],
                    "isError": False
                }
            else:
                return create_error_response(f"Anthropic API error {status_code}: {response_data}", with_readme=False)
        except Exception as e:
            return create_error_response(f"Anthropic request failed: {e}", with_readme=False)


def chat_mlx(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via MLX server (mlx_vlm.server) on Apple Silicon.

    The mlx_vlm.server provides an OpenAI-compatible API at /v1/chat/completions.
    This provider adds auto-detection of the mlx_host, anti-looping defaults for
    Qwen3.5 MoE models (repetition_penalty, stop tokens), and pass-through of all
    unrecognized parameters directly to the MLX API.

    Anti-looping defaults are only applied when the model name contains 'qwen' and
    '3.5', and only for parameters the caller hasn't already specified. Callers with
    enough RAM to run full-size models (who don't need anti-looping) can simply omit
    or override these defaults.
    """
    import http.client
    from urllib.parse import urlparse

    # Default 8081: port 11434 is Ollama's - a shared default guaranteed collisions.
    # '://' check instead of startswith('http') so hostnames like "httpserver.local" work.
    mlx_host = params.get('mlx_host', 'http://localhost:8081')
    if '://' not in mlx_host:
        mlx_host = f"http://{mlx_host}"

    model = params.get('model', '')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 4096)
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)

    parsed = urlparse(mlx_host)
    host = parsed.netloc or parsed.path
    path = '/v1/chat/completions'
    use_https = parsed.scheme == 'https'

    tools = params.get('tools', [])
    tool_choice = params.get('tool_choice', 'auto')

    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = tool_choice

    extra_params = _extract_extra_provider_specific_parameters(params)
    model_lower = model.lower()
    is_qwen35 = 'qwen' in model_lower and '3.5' in model_lower
    if is_qwen35:
        if 'repetition_penalty' not in extra_params:
            request_body['repetition_penalty'] = 1.15
        if 'repetition_context_size' not in extra_params:
            request_body['repetition_context_size'] = 128
        if 'stop' not in extra_params:
            request_body['stop'] = ["<|im_end|>", "<|im_start|>"]

    request_body.update(extra_params)

    headers = {"Content-Type": "application/json"}

    if stream:
        stream_id = str(uuid.uuid4())

        registration_error = _register_new_stream(StreamState(
            stream_id=stream_id,
            provider=Provider.MLX,
            model=model,
            session_id=handler_info.get('session_id', ''),
            request_id=handler_info.get('request_id', '')
        ))
        if registration_error:
            return create_error_response(registration_error, with_readme=False)

        def stream_worker():
            conn = None
            full_content = ""
            usage_from_stream: Dict = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()
            try:
                if use_https:
                    conn = http.client.HTTPSConnection(host, timeout=http_timeout_seconds)
                else:
                    conn = http.client.HTTPConnection(host, timeout=http_timeout_seconds)

                conn.request("POST", path, body=json.dumps(request_body), headers=headers)
                response = conn.getresponse()

                if response.status != 200:
                    error_body = response.read().decode('utf-8', errors='replace')
                    _mark_stream_complete(stream_id, error=f"MLX API error {response.status}")
                    send_stream_event(handler_info, stream_id, "", True,
                                    error=f"MLX API error {response.status}: {error_body}")
                    return

                for _event_type, data in _iter_sse_data_events(response, stream_id=stream_id):
                    if data == '[DONE]':
                        final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                        _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                        send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                        return

                    try:
                        chunk_data = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk_data.get('usage'):
                        usage_from_stream.update(chunk_data['usage'])
                    choices = chunk_data.get('choices') or [{}]
                    delta = choices[0].get('delta', {}).get('content', '')
                    if delta:
                        full_content += delta
                        _record_stream_delta(stream_id, full_content)
                        send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                        if send_failure_tracker.should_abort_after(send_succeeded):
                            MCPLogger.log(TOOL_LOG_NAME, f"Aborting stream {stream_id}: client disconnected")
                            _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                            return

                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                send_stream_event(handler_info, stream_id, "", True, usage=final_usage)

            except Exception as e:
                _mark_stream_complete(stream_id, error=str(e))
                send_stream_event(handler_info, stream_id, "", True, error=str(e))
            finally:
                if conn:
                    conn.close()

        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()

        return {
            "content": [{"type": "text", "text": json.dumps({
                "stream_id": stream_id,
                "status": "streaming",
                "model": model,
                "provider": "mlx",
                "anti_looping_applied": is_qwen35
            })}],
            "isError": False
        }

    else:
        conn = None
        try:
            if use_https:
                conn = http.client.HTTPSConnection(host, timeout=http_timeout_seconds)
            else:
                conn = http.client.HTTPConnection(host, timeout=http_timeout_seconds)

            conn.request("POST", path, body=json.dumps(request_body), headers=headers)
            response = conn.getresponse()
            response_data = response.read().decode('utf-8')

            if response.status == 200:
                result = json.loads(response_data)
                if tools:
                    result = _extract_tool_calls_from_openai_compatible_response(result, tools)
                return {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
            else:
                return create_error_response(f"MLX API error {response.status}: {response_data}", with_readme=False)
        except Exception as e:
            error_msg = str(e)
            if "connection refused" in error_msg.lower() or "errno 61" in error_msg.lower():
                return create_error_response(
                    f"Cannot connect to MLX server at {mlx_host}. "
                    "Start it with: mlx_vlm.server --host 0.0.0.0 --port 8081",
                    with_readme=False
                )
            return create_error_response(f"MLX API request failed: {e}", with_readme=False)
        finally:
            if conn:
                conn.close()


def chat_custom(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via custom OpenAI-compatible endpoint."""
    import http.client
    from urllib.parse import urlparse
    
    base_url = params.get('base_url')
    if not base_url:
        return create_error_response("base_url is required for custom provider", with_readme=False)
    
    api_key = params.get('api_key', '')
    model = params.get('model', 'default')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    http_timeout_seconds = _get_provider_http_timeout_seconds(params)
    
    # Parse base URL
    parsed = urlparse(base_url)
    host = parsed.netloc
    path = parsed.path.rstrip('/') + '/chat/completions'
    use_https = parsed.scheme == 'https'
    
    tools = params.get('tools', [])
    tool_choice = params.get('tool_choice', 'auto')

    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream
    }

    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = tool_choice

    request_body.update(_extract_extra_provider_specific_parameters(params))
    
    headers = {
        "Content-Type": "application/json"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    if stream:
        stream_id = str(uuid.uuid4())
        
        registration_error = _register_new_stream(StreamState(
            stream_id=stream_id,
            provider=Provider.CUSTOM,
            model=model,
            session_id=handler_info.get('session_id', ''),
            request_id=handler_info.get('request_id', '')
        ))
        if registration_error:
            return create_error_response(registration_error, with_readme=False)
        
        def stream_worker():
            conn = None
            full_content = ""
            usage_from_stream: Dict = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()
            try:
                if use_https:
                    conn = http.client.HTTPSConnection(host, timeout=http_timeout_seconds)
                else:
                    conn = http.client.HTTPConnection(host, timeout=http_timeout_seconds)
                
                conn.request("POST", path, body=json.dumps(request_body), headers=headers)
                response = conn.getresponse()
                
                if response.status != 200:
                    error_body = response.read().decode('utf-8', errors='replace')
                    _mark_stream_complete(stream_id, error=f"API error {response.status}")
                    send_stream_event(handler_info, stream_id, "", True,
                                    error=f"API error {response.status}: {error_body}")
                    return
                
                for _event_type, data in _iter_sse_data_events(response, stream_id=stream_id):
                    if data == '[DONE]':
                        final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                        _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                        send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                        return
                    
                    try:
                        chunk_data = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk_data.get('usage'):
                        usage_from_stream.update(chunk_data['usage'])
                    choices = chunk_data.get('choices') or [{}]
                    delta = choices[0].get('delta', {}).get('content', '')
                    if delta:
                        full_content += delta
                        _record_stream_delta(stream_id, full_content)
                        send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                        if send_failure_tracker.should_abort_after(send_succeeded):
                            MCPLogger.log(TOOL_LOG_NAME, f"Aborting stream {stream_id}: client disconnected")
                            _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                            return
                
                if _stream_is_cancelled(stream_id):
                    _mark_stream_complete(stream_id, content=full_content)
                    send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                    return

                final_usage = usage_from_stream or {"total_tokens": len(full_content.split()), "estimated": True}
                _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                                
            except Exception as e:
                _mark_stream_complete(stream_id, error=str(e))
                send_stream_event(handler_info, stream_id, "", True, error=str(e))
            finally:
                if conn:
                    conn.close()
        
        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()
        
        return {
            "content": [{"type": "text", "text": json.dumps({
                "stream_id": stream_id,
                "status": "streaming",
                "model": model
            })}],
            "isError": False
        }
    
    else:
        conn = None
        try:
            if use_https:
                conn = http.client.HTTPSConnection(host, timeout=http_timeout_seconds)
            else:
                conn = http.client.HTTPConnection(host, timeout=http_timeout_seconds)
            
            conn.request("POST", path, body=json.dumps(request_body), headers=headers)
            response = conn.getresponse()
            response_data = response.read().decode('utf-8')
            
            if response.status == 200:
                result = json.loads(response_data)
                if tools:
                    result = _extract_tool_calls_from_openai_compatible_response(result, tools)
                return {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
            else:
                return create_error_response(f"Custom API error {response.status}: {response_data}", with_readme=False)
        except Exception as e:
            return create_error_response(f"Custom API request failed: {e}", with_readme=False)
        finally:
            if conn:
                conn.close()


def _detect_cursor_agent_cli_path():
    """Lazily detect and cache the cursor-agent CLI path.

    Searches PATH for 'cursor-agent' first (works on all platforms including
    Windows .cmd wrappers), then falls back to 'agent' (the Linux/macOS
    symlink name created by the official installer).

    Returns the absolute path, or None if not installed.
    Detection runs once and caches the result for the process lifetime.
    """
    global _cursor_agent_cli_path_cache, _cursor_agent_cli_detection_done
    if _cursor_agent_cli_detection_done:
        return _cursor_agent_cli_path_cache

    import shutil
    for candidate_name in ("cursor-agent", "agent"):
        found = shutil.which(candidate_name)
        if found:
            _cursor_agent_cli_path_cache = found
            _cursor_agent_cli_detection_done = True
            MCPLogger.log(TOOL_LOG_NAME, f"Cursor agent CLI detected at: {found} (searched as '{candidate_name}')")
            return _cursor_agent_cli_path_cache

    _cursor_agent_cli_detection_done = True
    MCPLogger.log(TOOL_LOG_NAME, "Cursor agent CLI not found in PATH (tried 'cursor-agent' and 'agent')")
    return _cursor_agent_cli_path_cache


def _cli_agents_bypass_safety_flags() -> bool:
    """Whether CLI agent providers run with their permission rails bypassed
    (--force/--trust/--yolo/--dangerously-skip-permissions).

    Configurable via settings[0].llm_cli_agents_bypass_safety. Defaults to True
    because the CLI providers are documented to run bypassed (they rely on it
    for headless tool access); operators can set it false to run them 'safe'.
    """
    return _get_llm_settings_flag('llm_cli_agents_bypass_safety', True)


def chat_cursor_agent(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via Cursor's agent CLI.

    Runs the cursor agent CLI in headless mode with full tool access.
    The agent CLI provides access to 80+ cloud models via a paid Cursor subscription.
    Supports both blocking (--output-format json) and streaming
    (--output-format stream-json --stream-partial-output) modes.

    The prompt is constructed by flattening the messages array (see
    _flatten_messages_for_cli) and is passed via STDIN — never on the command
    line — so untrusted text cannot traverse cmd.exe, is not limited to ~32KB,
    and is not visible to other local processes.
    """
    agent_path = _detect_cursor_agent_cli_path()
    if not agent_path:
        return create_error_response(
            "Cursor agent CLI not found (tried 'cursor-agent' and 'agent' in PATH). "
            "Install: curl https://cursor.com/install -fsS | bash",
            with_readme=False
        )

    model = params.get('model', '')
    messages = params.get('messages', [])
    flattened_prompt_text = _flatten_messages_for_cli(messages)

    workspace_path = os.getcwd()
    use_shell_for_cmd_wrappers = agent_path.lower().endswith(('.cmd', '.bat'))

    if stream:
        return _chat_cursor_agent_streaming(
            agent_path, model, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers, handler_info
        )
    else:
        return _chat_cursor_agent_blocking(
            agent_path, model, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers
        )


def _chat_cursor_agent_blocking(agent_path: str, model: str,
                                flattened_prompt_text: str, workspace_path: str,
                                use_shell_for_cmd_wrappers: bool) -> Dict:
    """Non-streaming cursor-agent chat: runs subprocess.run, parses JSON output.
    The prompt is piped via stdin (never argv - see chat_cursor_agent docstring)."""
    import subprocess

    cmd = [agent_path]
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--disable-auto-update",
        "--print",
        "--output-format", "json",
        "--workspace", workspace_path,
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.extend([
            "--force",
            "--sandbox", "disabled",
            "--trust",
            "--approve-mcps",
        ])

    try:
        MCPLogger.log(TOOL_LOG_NAME,
                      f"Running cursor-agent (blocking): model={model or 'default'}, "
                      f"workspace={workspace_path}, prompt_length={len(flattened_prompt_text)}, "
                      f"shell={use_shell_for_cmd_wrappers}")

        result = subprocess.run(
            cmd,
            input=flattened_prompt_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300,
            cwd=workspace_path,
            shell=use_shell_for_cmd_wrappers,
            creationflags=_subprocess_no_window_flags()
        )

        if result.returncode != 0:
            error_text = (result.stderr or "").strip() or (result.stdout or "").strip() or f"Exit code {result.returncode}"
            return create_error_response(f"Cursor agent CLI failed: {error_text}", with_readme=False)

        output = (result.stdout or "").strip()
        try:
            agent_result = json.loads(output)
        except json.JSONDecodeError:
            agent_result = {"result": output}

        content_text = agent_result.get('result', agent_result.get('content', output))
        raw_usage = agent_result.get('usage', {})
        prompt_token_count = raw_usage.get('inputTokens', raw_usage.get('prompt_tokens', len(flattened_prompt_text.split())))
        completion_token_count = raw_usage.get('outputTokens', raw_usage.get('completion_tokens', len(str(content_text).split())))

        response = {
            "id": f"cursor-agent-{agent_result.get('request_id', str(uuid.uuid4()))}",
            "object": "chat.completion",
            "model": model or "cursor-agent-default",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            },
            "cursor_agent_metadata": {
                "session_id": agent_result.get('session_id', ''),
                "duration_ms": agent_result.get('duration_ms', 0),
                "duration_api_ms": agent_result.get('duration_api_ms', 0),
            }
        }

        return {
            "content": [{"type": "text", "text": json.dumps(response)}],
            "isError": False
        }

    except subprocess.TimeoutExpired:
        return create_error_response("Cursor agent CLI timed out after 300 seconds", with_readme=False)
    except FileNotFoundError:
        global _cursor_agent_cli_path_cache, _cursor_agent_cli_detection_done
        _cursor_agent_cli_path_cache = None
        _cursor_agent_cli_detection_done = False
        return create_error_response(
            "Cursor agent CLI binary disappeared. "
            "Re-install: curl https://cursor.com/install -fsS | bash",
            with_readme=False
        )
    except Exception as e:
        return create_error_response(f"Cursor agent request failed: {e}", with_readme=False)


def _chat_cursor_agent_streaming(agent_path: str, model: str,
                                 flattened_prompt_text: str, workspace_path: str,
                                 use_shell_for_cmd_wrappers: bool,
                                 handler_info: Dict) -> Dict:
    """Streaming cursor-agent chat: subprocess.Popen with NDJSON line parsing.

    With --output-format stream-json --stream-partial-output the CLI emits NDJSON.
    Partial assistant events carry a timestamp_ms field and a single text chunk in
    message.content[0].text.  The final assembled assistant message (no timestamp_ms)
    is skipped since deltas already covered it.  The result event provides usage and
    session metadata.
    """
    import subprocess

    cmd = [agent_path]
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--disable-auto-update",
        "--print",
        "--output-format", "stream-json",
        "--stream-partial-output",
        "--workspace", workspace_path,
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.extend([
            "--force",
            "--sandbox", "disabled",
            "--trust",
            "--approve-mcps",
        ])

    stream_id = str(uuid.uuid4())

    registration_error = _register_new_stream(StreamState(
        stream_id=stream_id,
        provider=Provider.CURSOR_AGENT,
        model=model or "cursor-agent-default",
        session_id=handler_info.get('session_id', ''),
        request_id=handler_info.get('request_id', '')
    ))
    if registration_error:
        return create_error_response(registration_error, with_readme=False)

    def stream_reader_worker():
        """Reader thread: spawns Popen, reads stdout line-by-line, dispatches deltas."""
        proc = None
        get_captured_stderr_tail = lambda: ""
        try:
            MCPLogger.log(TOOL_LOG_NAME,
                          f"Running cursor-agent (streaming): model={model or 'default'}, "
                          f"workspace={workspace_path}, "
                          f"prompt_length={len(flattened_prompt_text)}, shell={use_shell_for_cmd_wrappers}")

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=workspace_path,
                shell=use_shell_for_cmd_wrappers,
                creationflags=_subprocess_no_window_flags()
            )

            # Prompt goes via stdin (injection/length/visibility fix); stderr is
            # drained on a companion thread so a chatty CLI can't deadlock us.
            get_captured_stderr_tail = _start_stderr_drain_thread(proc)
            try:
                proc.stdin.write(flattened_prompt_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError) as stdin_error:
                MCPLogger.log(TOOL_LOG_NAME, f"Cursor agent stdin write failed: {stdin_error}")

            with _streams_lock:
                _active_stream_subprocesses[stream_id] = proc

            full_content = ""
            session_id_from_cli = ""
            duration_ms_from_cli = 0
            usage_from_cli = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()

            with _PeriodicStreamHeartbeat(handler_info, stream_id):
                for raw_line in proc.stdout:
                    if _stream_is_cancelled(stream_id):
                        MCPLogger.log(TOOL_LOG_NAME, f"Cursor agent stream {stream_id} cancelled, terminating process")
                        proc.terminate()
                        _mark_stream_complete(stream_id, content=full_content)
                        return

                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get('type', '')

                    if event_type == 'assistant':
                        has_timestamp = 'timestamp_ms' in event
                        msg = event.get('message', {})
                        content_blocks = msg.get('content', [])

                        if has_timestamp and content_blocks:
                            # Iterate ALL blocks - multi-block events must not lose text
                            text_chunk = ''.join(
                                block.get('text', '') for block in content_blocks
                                if isinstance(block, dict) and block.get('type') == 'text'
                            )
                            if text_chunk:
                                full_content += text_chunk
                                _record_stream_delta(stream_id, full_content)
                                send_succeeded = send_stream_event(handler_info, stream_id, text_chunk, False)
                                if send_failure_tracker.should_abort_after(send_succeeded):
                                    # Client gone: stop the CLI instead of running it to completion
                                    MCPLogger.log(TOOL_LOG_NAME, f"Aborting cursor-agent stream {stream_id}: client disconnected")
                                    proc.terminate()
                                    _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                    return

                    elif event_type == 'result':
                        session_id_from_cli = event.get('session_id', '')
                        duration_ms_from_cli = event.get('duration_ms', 0)
                        usage_from_cli = event.get('usage', {})
                        result_text = event.get('result', '')
                        if result_text and not full_content:
                            full_content = result_text

            proc.wait()

            prompt_token_count = usage_from_cli.get('inputTokens', usage_from_cli.get('input_tokens', 0))
            completion_token_count = usage_from_cli.get('outputTokens', usage_from_cli.get('output_tokens', 0))

            final_usage = {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            }
            _mark_stream_complete(stream_id, content=full_content, usage=final_usage)

            send_stream_event(handler_info, stream_id, "", True,
                              usage={
                                  "prompt_tokens": prompt_token_count,
                                  "completion_tokens": completion_token_count,
                                  "total_tokens": prompt_token_count + completion_token_count,
                                  "cursor_agent_session_id": session_id_from_cli,
                                  "cursor_agent_duration_ms": duration_ms_from_cli,
                              })

            if proc.returncode and proc.returncode != 0:
                MCPLogger.log(TOOL_LOG_NAME,
                              f"Cursor agent stream process exited with code {proc.returncode}: {get_captured_stderr_tail()[:500]}")

        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Cursor agent stream error: {e}\n{traceback.format_exc()}")
            _mark_stream_complete(stream_id, error=str(e))
            send_stream_event(handler_info, stream_id, "", True, error=str(e))
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with _streams_lock:
                _active_stream_subprocesses.pop(stream_id, None)

    thread = threading.Thread(target=stream_reader_worker, daemon=True)
    thread.start()

    return {
        "content": [{"type": "text", "text": json.dumps({
            "stream_id": stream_id,
            "status": "streaming",
            "model": model or "cursor-agent-default"
        })}],
        "isError": False
    }


def _detect_claude_code_cli_path():
    """Lazily detect and cache the claude-code CLI path.

    Searches PATH for 'claude' first, then checks well-known install locations:
    - Windows: %USERPROFILE%\\.local\\bin\\claude.exe
    - Unix: ~/.local/bin/claude

    Returns the absolute path, or None if not installed.
    Detection runs once and caches the result for the process lifetime.
    """
    global _claude_code_cli_path_cache, _claude_code_cli_detection_done
    if _claude_code_cli_detection_done:
        return _claude_code_cli_path_cache

    import shutil
    found = shutil.which("claude")
    if found:
        _claude_code_cli_path_cache = found
        _claude_code_cli_detection_done = True
        MCPLogger.log(TOOL_LOG_NAME, f"Claude Code CLI detected at: {found} (via PATH)")
        return _claude_code_cli_path_cache

    home = os.path.expanduser("~")
    well_known_locations = []
    if sys.platform == 'win32':
        well_known_locations.append(os.path.join(home, ".local", "bin", "claude.exe"))
    well_known_locations.append(os.path.join(home, ".local", "bin", "claude"))

    for candidate_path in well_known_locations:
        if os.path.isfile(candidate_path) and (sys.platform == 'win32' or os.access(candidate_path, os.X_OK)):
            _claude_code_cli_path_cache = candidate_path
            _claude_code_cli_detection_done = True
            MCPLogger.log(TOOL_LOG_NAME, f"Claude Code CLI detected at: {candidate_path} (well-known location)")
            return _claude_code_cli_path_cache

    _claude_code_cli_detection_done = True
    MCPLogger.log(TOOL_LOG_NAME, "Claude Code CLI not found (tried PATH and well-known install locations)")
    return _claude_code_cli_path_cache


def chat_claude_code(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via Anthropic's Claude Code CLI.

    Runs the claude CLI in non-interactive --print mode.
    Non-streaming uses --output-format=json and collects the full result.
    Streaming uses --output-format=stream-json --include-partial-messages --verbose
    and parses NDJSON events line-by-line via subprocess.Popen, feeding text deltas
    through the existing StreamState / send_stream_event infrastructure.

    Supported models: claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5.
    """
    import subprocess

    claude_cli_path = _detect_claude_code_cli_path()
    if not claude_cli_path:
        return create_error_response(
            "Claude Code CLI not found ('claude' not in PATH). "
            "Install: npm install -g @anthropic-ai/claude-code",
            with_readme=False
        )

    model = params.get('model', '')
    messages = params.get('messages', [])
    effort = params.get('effort', 'max')

    flattened_prompt_text = _flatten_messages_for_cli(messages)

    workspace_path = os.getcwd()

    use_shell_for_cmd_wrappers = claude_cli_path.lower().endswith(('.cmd', '.bat'))

    if stream:
        return _chat_claude_code_streaming(
            claude_cli_path, model, effort, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers, handler_info
        )
    else:
        return _chat_claude_code_blocking(
            claude_cli_path, model, effort, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers
        )


def _chat_claude_code_blocking(claude_cli_path: str, model: str, effort: str,
                               flattened_prompt_text: str, workspace_path: str,
                               use_shell_for_cmd_wrappers: bool) -> Dict:
    """Non-streaming claude-code chat: runs subprocess.run, parses JSON output.
    The prompt is piped via stdin (never argv) to avoid cmd.exe injection,
    the ~32KB Windows command-line limit, and process-list prompt visibility."""
    import subprocess

    cmd = [claude_cli_path]
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--print",
        "--output-format", "json",
        "--verbose",
        "--effort", effort,
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.append("--dangerously-skip-permissions")

    try:
        MCPLogger.log(TOOL_LOG_NAME,
                      f"Running claude-code (blocking): model={model or 'default'}, "
                      f"effort={effort}, workspace={workspace_path}, "
                      f"prompt_length={len(flattened_prompt_text)}, shell={use_shell_for_cmd_wrappers}")

        result = subprocess.run(
            cmd,
            input=flattened_prompt_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=600,
            cwd=workspace_path,
            shell=use_shell_for_cmd_wrappers,
            creationflags=_subprocess_no_window_flags()
        )

        if result.returncode != 0:
            error_text = (result.stderr or "").strip() or (result.stdout or "").strip() or f"Exit code {result.returncode}"
            return create_error_response(f"Claude Code CLI failed: {error_text}", with_readme=False)

        output = (result.stdout or "").strip()

        content_text = ""
        session_id_from_cli = ""
        duration_ms_from_cli = 0
        usage_from_cli = {}

        MCPLogger.log(TOOL_LOG_NAME, f"Claude CLI raw output length: {len(output)}, first 500 chars: {output[:500]}")

        try:
            parsed_output = json.loads(output)
            MCPLogger.log(TOOL_LOG_NAME, f"Parsed JSON type: {type(parsed_output)}, is_list: {isinstance(parsed_output, list)}")
            if isinstance(parsed_output, list):
                MCPLogger.log(TOOL_LOG_NAME, f"Event count: {len(parsed_output)}, event types: {[e.get('type') for e in parsed_output]}")
        except json.JSONDecodeError as e:
            MCPLogger.log(TOOL_LOG_NAME, f"JSON parse error: {e}")
            content_text = output
            parsed_output = None

        if parsed_output is not None:
            if isinstance(parsed_output, list):
                for event in parsed_output:
                    event_type = event.get('type', '')
                    if event_type == 'result':
                        content_text = event.get('result', content_text)
                        session_id_from_cli = event.get('session_id', '')
                        duration_ms_from_cli = event.get('duration_ms', 0)
                        usage_from_cli = event.get('usage', {})
                    elif event_type == 'assistant' and not content_text:
                        msg = event.get('message', {})
                        for block in msg.get('content', []):
                            if block.get('type') == 'text':
                                content_text += block.get('text', '')
            elif isinstance(parsed_output, dict):
                content_text = parsed_output.get('result', parsed_output.get('content', output))
                session_id_from_cli = parsed_output.get('session_id', '')
                duration_ms_from_cli = parsed_output.get('duration_ms', 0)
                usage_from_cli = parsed_output.get('usage', {})

        MCPLogger.log(TOOL_LOG_NAME, f"Extracted: content_len={len(content_text)}, session_id={session_id_from_cli}, duration_ms={duration_ms_from_cli}")

        prompt_token_count = usage_from_cli.get('input_tokens', len(flattened_prompt_text.split()))
        completion_token_count = usage_from_cli.get('output_tokens', len(str(content_text).split()))

        response = {
            "id": f"claude-code-{session_id_from_cli or str(uuid.uuid4())}",
            "object": "chat.completion",
            "model": model or "claude-code-default",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            },
            "claude_code_metadata": {
                "session_id": session_id_from_cli,
                "duration_ms": duration_ms_from_cli,
            }
        }

        return {
            "content": [{"type": "text", "text": json.dumps(response)}],
            "isError": False
        }

    except subprocess.TimeoutExpired:
        return create_error_response("Claude Code CLI timed out after 600 seconds", with_readme=False)
    except FileNotFoundError:
        global _claude_code_cli_path_cache, _claude_code_cli_detection_done
        _claude_code_cli_path_cache = None
        _claude_code_cli_detection_done = False
        return create_error_response(
            "Claude Code CLI binary disappeared. "
            "Re-install: npm install -g @anthropic-ai/claude-code",
            with_readme=False
        )
    except Exception as e:
        return create_error_response(f"Claude Code request failed: {e}", with_readme=False)


def _chat_claude_code_streaming(claude_cli_path: str, model: str, effort: str,
                                flattened_prompt_text: str, workspace_path: str,
                                use_shell_for_cmd_wrappers: bool,
                                handler_info: Dict) -> Dict:
    """Streaming claude-code chat: subprocess.Popen with NDJSON line parsing.

    The CLI emits newline-delimited JSON with --output-format=stream-json.
    With --include-partial-messages, content_block_delta events carry token-level
    text_delta chunks that map directly to our StreamState/send_stream_event infra.
    """
    import subprocess

    cmd = [claude_cli_path]
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--effort", effort,
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.append("--dangerously-skip-permissions")

    stream_id = str(uuid.uuid4())

    registration_error = _register_new_stream(StreamState(
        stream_id=stream_id,
        provider=Provider.CLAUDE_CODE,
        model=model or "claude-code-default",
        session_id=handler_info.get('session_id', ''),
        request_id=handler_info.get('request_id', '')
    ))
    if registration_error:
        return create_error_response(registration_error, with_readme=False)

    def stream_reader_worker():
        """Reader thread: spawns Popen, reads stdout line-by-line, dispatches deltas."""
        proc = None
        get_captured_stderr_tail = lambda: ""
        try:
            MCPLogger.log(TOOL_LOG_NAME,
                          f"Running claude-code (streaming): model={model or 'default'}, "
                          f"effort={effort}, workspace={workspace_path}, "
                          f"prompt_length={len(flattened_prompt_text)}, shell={use_shell_for_cmd_wrappers}")

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=workspace_path,
                shell=use_shell_for_cmd_wrappers,
                creationflags=_subprocess_no_window_flags()
            )

            # Prompt via stdin; stderr drained on a companion thread (deadlock fix)
            get_captured_stderr_tail = _start_stderr_drain_thread(proc)
            try:
                proc.stdin.write(flattened_prompt_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError) as stdin_error:
                MCPLogger.log(TOOL_LOG_NAME, f"Claude Code stdin write failed: {stdin_error}")

            with _streams_lock:
                _active_stream_subprocesses[stream_id] = proc

            full_content = ""
            session_id_from_cli = ""
            duration_ms_from_cli = 0
            usage_from_cli = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()

            with _PeriodicStreamHeartbeat(handler_info, stream_id):
                for raw_line in proc.stdout:
                    if _stream_is_cancelled(stream_id):
                        MCPLogger.log(TOOL_LOG_NAME, f"Claude Code stream {stream_id} cancelled, terminating process")
                        proc.terminate()
                        _mark_stream_complete(stream_id, content=full_content)
                        return

                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get('type', '')

                    if event_type == 'stream_event':
                        inner_event = event.get('event', {})
                        inner_type = inner_event.get('type', '')

                        if inner_type == 'content_block_delta':
                            delta_obj = inner_event.get('delta', {})
                            delta_type = delta_obj.get('type', '')

                            if delta_type == 'text_delta':
                                text_chunk = delta_obj.get('text', '')
                                if text_chunk:
                                    full_content += text_chunk
                                    _record_stream_delta(stream_id, full_content)
                                    send_succeeded = send_stream_event(handler_info, stream_id, text_chunk, False)
                                    if send_failure_tracker.should_abort_after(send_succeeded):
                                        # Client gone: stop the CLI instead of running it to completion
                                        MCPLogger.log(TOOL_LOG_NAME, f"Aborting claude-code stream {stream_id}: client disconnected")
                                        proc.terminate()
                                        _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                        return

                    elif event_type == 'result':
                        session_id_from_cli = event.get('session_id', '')
                        duration_ms_from_cli = event.get('duration_ms', 0)
                        usage_from_cli = event.get('usage', {})
                        result_text = event.get('result', '')
                        if result_text and not full_content:
                            full_content = result_text

                    elif event_type == 'assistant' and not full_content:
                        msg = event.get('message', {})
                        for block in msg.get('content', []):
                            if block.get('type') == 'text':
                                text = block.get('text', '')
                                if text:
                                    full_content += text

            proc.wait()

            prompt_token_count = usage_from_cli.get('input_tokens', 0)
            completion_token_count = usage_from_cli.get('output_tokens', 0)

            final_usage = {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            }
            _mark_stream_complete(stream_id, content=full_content, usage=final_usage)

            send_stream_event(handler_info, stream_id, "", True,
                              usage={
                                  "prompt_tokens": prompt_token_count,
                                  "completion_tokens": completion_token_count,
                                  "total_tokens": prompt_token_count + completion_token_count,
                                  "claude_code_session_id": session_id_from_cli,
                                  "claude_code_duration_ms": duration_ms_from_cli,
                              })

            if proc.returncode and proc.returncode != 0:
                MCPLogger.log(TOOL_LOG_NAME,
                              f"Claude Code stream process exited with code {proc.returncode}: {get_captured_stderr_tail()[:500]}")

        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Claude Code stream error: {e}\n{traceback.format_exc()}")
            _mark_stream_complete(stream_id, error=str(e))
            send_stream_event(handler_info, stream_id, "", True, error=str(e))
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with _streams_lock:
                _active_stream_subprocesses.pop(stream_id, None)

    thread = threading.Thread(target=stream_reader_worker, daemon=True)
    thread.start()

    return {
        "content": [{"type": "text", "text": json.dumps({
            "stream_id": stream_id,
            "status": "streaming",
            "model": model or "claude-code-default"
        })}],
        "isError": False
    }


def _get_codex_mcp_bridge():
    """Get the local.py MCP bridge and the codex internal tool name, or (None, None)."""
    try:
        from ragtag.tools.local import _bridge
        _bridge.ensure_initialized()
        for registered_name, tool_info in _bridge.tool_registry.items():
            if tool_info.get("original_tool_name") == "codex":
                return _bridge, registered_name
        return None, None
    except Exception:
        return None, None


def _check_codex_mcp_bridge_is_available() -> bool:
    """Check whether the codex tool is available via local.py MCP bridge."""
    bridge, tool_name = _get_codex_mcp_bridge()
    return bridge is not None


def chat_codex_cli(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via OpenAI Codex CLI, accessed through the local MCP bridge.

    Codex exposes two MCP tools: 'codex' (start session) and 'codex-reply' (continue).
    This function flattens the messages array into a single prompt and calls codex
    via local.py's MCPBridge.execute_tool() which talks to the codex mcp-server subprocess.

    Streaming is not directly supported through the bridge (codex notifications are
    consumed internally by local.py), so this always operates in blocking mode.
    """
    bridge, codex_registered_name = _get_codex_mcp_bridge()
    if bridge is None:
        return create_error_response(
            "Codex CLI MCP tool not available. Enable codex in settings[0].local_mcpServers "
            "and restart the server. Requires: codex CLI installed (npm install -g @openai/codex)",
            with_readme=False
        )

    model = params.get('model', '')
    messages = params.get('messages', [])
    codex_thread_id = params.get('codex_thread_id', '')

    flattened_prompt_text = _flatten_messages_for_cli(messages)

    if not flattened_prompt_text.strip():
        return create_error_response("No prompt content to send to Codex", with_readme=False)

    try:
        if codex_thread_id:
            codex_args = {
                "threadId": codex_thread_id,
                "prompt": flattened_prompt_text,
            }
            codex_reply_name = None
            for reg_name, info in bridge.tool_registry.items():
                if info.get("original_tool_name") == "codex-reply":
                    codex_reply_name = reg_name
                    break
            call_tool_name = codex_reply_name or codex_registered_name
        else:
            codex_args: Dict[str, Any] = {
                "prompt": flattened_prompt_text,
                "sandbox": "read-only",
            }
            if model:
                codex_args["model"] = model
            call_tool_name = codex_registered_name

        MCPLogger.log(TOOL_LOG_NAME, f"Calling codex via bridge.execute_tool (tool={call_tool_name}, thread={codex_thread_id or 'new'}, model={model or 'default'})")

        result = bridge.execute_tool(call_tool_name, codex_args)

        if result is None:
            return create_error_response("No response from codex MCP bridge", with_readme=False)
        if isinstance(result, dict) and result.get("isError"):
            return result

        response_text = ""
        codex_thread_id_from_response = ""
        if isinstance(result, dict):
            content_list = result.get("content", [])
            for item in content_list:
                if isinstance(item, dict) and item.get("type") == "text":
                    response_text += item.get("text", "")
            codex_thread_id_from_response = result.get("threadId", result.get("thread_id", ""))
        elif isinstance(result, str):
            response_text = result

        if not response_text:
            response_text = str(result) if result else "(empty response from codex)"

        prompt_token_estimate = len(flattened_prompt_text.split())
        completion_token_estimate = len(response_text.split())

        response_payload = {
            "id": f"codex-cli-{codex_thread_id_from_response or str(uuid.uuid4())}",
            "object": "chat.completion",
            "model": model or "codex-cli-default",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_token_estimate,
                "completion_tokens": completion_token_estimate,
                "total_tokens": prompt_token_estimate + completion_token_estimate
            },
            "codex_cli_metadata": {
                "thread_id": codex_thread_id_from_response,
            }
        }

        return {
            "content": [{"type": "text", "text": json.dumps(response_payload)}],
            "isError": False
        }

    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in chat_codex_cli: {e}")
        return create_error_response(f"Codex CLI error: {e}", with_readme=False)


def _detect_gemini_cli_path():
    """Lazily detect and cache the gemini CLI path.

    Searches PATH for 'gemini' first, then checks for npx availability
    (since gemini-cli is typically invoked as `npx @google/gemini-cli`).

    Returns the absolute path to gemini binary, 'npx' if npx is available
    but no standalone gemini binary exists, or None if neither is found.
    Detection runs once and caches the result for the process lifetime.
    """
    global _gemini_cli_path_cache, _gemini_cli_detection_done
    if _gemini_cli_detection_done:
        return _gemini_cli_path_cache

    import shutil
    found = shutil.which("gemini")
    if found:
        _gemini_cli_path_cache = found
        _gemini_cli_detection_done = True
        MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI detected at: {found} (via PATH)")
        return _gemini_cli_path_cache

    if sys.platform == 'win32':
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_gemini_cmd = os.path.join(appdata, "npm", "gemini.CMD")
            if os.path.isfile(npm_gemini_cmd):
                _gemini_cli_path_cache = npm_gemini_cmd
                _gemini_cli_detection_done = True
                MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI detected at: {npm_gemini_cmd} (well-known npm global location)")
                return _gemini_cli_path_cache

    npx_path = shutil.which("npx")
    if npx_path:
        _gemini_cli_path_cache = "npx"
        _gemini_cli_detection_done = True
        MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI will use npx at: {npx_path}")
        return _gemini_cli_path_cache

    _gemini_cli_detection_done = True
    MCPLogger.log(TOOL_LOG_NAME, "Gemini CLI not found (tried PATH for 'gemini' and 'npx')")
    return _gemini_cli_path_cache


def chat_gemini_cli(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via Google Gemini CLI.

    Runs the gemini CLI in non-interactive --prompt mode.
    Non-streaming uses --output-format=json and parses the JSON result.
    Streaming uses --output-format=stream-json and parses NDJSON events
    line-by-line via subprocess.Popen, feeding text deltas through the
    existing StreamState / send_stream_event infrastructure.

    Supported models: gemini-3-flash-preview, gemini-2.5-pro, etc.
    """
    import subprocess

    gemini_cli_path = _detect_gemini_cli_path()
    if not gemini_cli_path:
        return create_error_response(
            "Gemini CLI not found ('gemini' not in PATH and 'npx' not available). "
            "Install: npm install -g @google/gemini-cli",
            with_readme=False
        )

    model = params.get('model', '')
    messages = params.get('messages', [])

    flattened_prompt_text = _flatten_messages_for_cli(messages)

    workspace_path = os.getcwd()

    if gemini_cli_path == "npx":
        # Pinned version: npx at call time is a supply-chain surface otherwise
        cmd = ["npx", "--yes", _GEMINI_CLI_NPX_PACKAGE_SPEC]
    else:
        cmd = [gemini_cli_path]

    use_shell_for_cmd_wrappers = (gemini_cli_path != "npx"
                                  and gemini_cli_path.lower().endswith(('.cmd', '.bat')))

    if stream:
        return _chat_gemini_cli_streaming(
            cmd, model, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers, handler_info
        )
    else:
        return _chat_gemini_cli_blocking(
            cmd, model, flattened_prompt_text,
            workspace_path, use_shell_for_cmd_wrappers
        )


def _chat_gemini_cli_blocking(cmd_base: list, model: str,
                              flattened_prompt_text: str, workspace_path: str,
                              use_shell_for_cmd_wrappers: bool) -> Dict:
    """Non-streaming gemini CLI chat: runs subprocess.run, parses JSON output.
    The prompt is piped via stdin (never argv) to avoid cmd.exe injection,
    the ~32KB Windows command-line limit, and process-list prompt visibility."""
    import subprocess

    cmd = list(cmd_base)
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--output-format", "json",
        "--skip-trust",
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.append("--yolo")

    try:
        MCPLogger.log(TOOL_LOG_NAME,
                      f"Running gemini-cli (blocking): model={model or 'default'}, "
                      f"workspace={workspace_path}, "
                      f"prompt_length={len(flattened_prompt_text)}, shell={use_shell_for_cmd_wrappers}")

        result = subprocess.run(
            cmd,
            input=flattened_prompt_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=600,
            cwd=workspace_path,
            shell=use_shell_for_cmd_wrappers,
            creationflags=_subprocess_no_window_flags()
        )

        if result.returncode != 0:
            error_text = (result.stderr or "").strip() or (result.stdout or "").strip() or f"Exit code {result.returncode}"
            return create_error_response(f"Gemini CLI failed: {error_text}", with_readme=False)

        output = (result.stdout or "").strip()

        content_text = ""
        session_id_from_cli = ""
        usage_from_cli = {}

        try:
            parsed_output = json.loads(output)
        except json.JSONDecodeError:
            content_text = output
            parsed_output = None

        if parsed_output is not None:
            content_text = parsed_output.get('response', '')
            session_id_from_cli = parsed_output.get('session_id', '')
            stats = parsed_output.get('stats', {})
            models_stats = stats.get('models', {})
            for model_name, model_stats in models_stats.items():
                tokens = model_stats.get('tokens', {})
                usage_from_cli = {
                    'input_tokens': tokens.get('input', 0),
                    'output_tokens': tokens.get('candidates', 0),
                    'cached_tokens': tokens.get('cached', 0),
                    'thinking_tokens': tokens.get('thoughts', 0),
                }
                break

        prompt_token_count = usage_from_cli.get('input_tokens', len(flattened_prompt_text.split()))
        completion_token_count = usage_from_cli.get('output_tokens', len(str(content_text).split()))

        response = {
            "id": f"gemini-cli-{session_id_from_cli or str(uuid.uuid4())}",
            "object": "chat.completion",
            "model": model or "gemini-cli-default",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            },
            "gemini_cli_metadata": {
                "session_id": session_id_from_cli,
            }
        }

        return {
            "content": [{"type": "text", "text": json.dumps(response)}],
            "isError": False
        }

    except subprocess.TimeoutExpired:
        return create_error_response("Gemini CLI timed out after 600 seconds", with_readme=False)
    except FileNotFoundError:
        global _gemini_cli_path_cache, _gemini_cli_detection_done
        _gemini_cli_path_cache = None
        _gemini_cli_detection_done = False
        return create_error_response(
            "Gemini CLI binary disappeared. "
            "Re-install: npm install -g @google/gemini-cli",
            with_readme=False
        )
    except Exception as e:
        return create_error_response(f"Gemini CLI request failed: {e}", with_readme=False)


def _chat_gemini_cli_streaming(cmd_base: list, model: str,
                               flattened_prompt_text: str, workspace_path: str,
                               use_shell_for_cmd_wrappers: bool,
                               handler_info: Dict) -> Dict:
    """Streaming gemini CLI chat: subprocess.Popen with NDJSON line parsing.

    The CLI emits newline-delimited JSON with --output-format=stream-json.
    Events: init (session start), message (role=assistant, delta=true for chunks),
    result (final stats with token counts).
    """
    import subprocess

    cmd = list(cmd_base)
    if model:
        cmd.extend(["--model", model])
    cmd.extend([
        "--output-format", "stream-json",
        "--skip-trust",
    ])
    if _cli_agents_bypass_safety_flags():
        cmd.append("--yolo")

    stream_id = str(uuid.uuid4())

    registration_error = _register_new_stream(StreamState(
        stream_id=stream_id,
        provider=Provider.GEMINI_CLI,
        model=model or "gemini-cli-default",
        session_id=handler_info.get('session_id', ''),
        request_id=handler_info.get('request_id', '')
    ))
    if registration_error:
        return create_error_response(registration_error, with_readme=False)

    def stream_reader_worker():
        """Reader thread: spawns Popen, reads stdout line-by-line, dispatches deltas."""
        proc = None
        get_captured_stderr_tail = lambda: ""
        try:
            MCPLogger.log(TOOL_LOG_NAME,
                          f"Running gemini-cli (streaming): model={model or 'default'}, "
                          f"workspace={workspace_path}, "
                          f"prompt_length={len(flattened_prompt_text)}, shell={use_shell_for_cmd_wrappers}")

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=workspace_path,
                shell=use_shell_for_cmd_wrappers,
                creationflags=_subprocess_no_window_flags()
            )

            # Prompt via stdin; stderr drained on a companion thread (deadlock fix)
            get_captured_stderr_tail = _start_stderr_drain_thread(proc)
            try:
                proc.stdin.write(flattened_prompt_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError) as stdin_error:
                MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI stdin write failed: {stdin_error}")

            with _streams_lock:
                _active_stream_subprocesses[stream_id] = proc

            full_content = ""
            session_id_from_cli = ""
            usage_from_cli = {}
            send_failure_tracker = _ConsecutiveSendFailureTracker()

            with _PeriodicStreamHeartbeat(handler_info, stream_id):
                for raw_line in proc.stdout:
                    if _stream_is_cancelled(stream_id):
                        MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI stream {stream_id} cancelled, terminating process")
                        proc.terminate()
                        _mark_stream_complete(stream_id, content=full_content)
                        return

                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get('type', '')

                    if event_type == 'init':
                        session_id_from_cli = event.get('session_id', '')

                    elif event_type == 'message':
                        role = event.get('role', '')
                        is_delta = event.get('delta', False)
                        content = event.get('content', '')

                        if role == 'assistant' and content:
                            if is_delta:
                                full_content += content
                                _record_stream_delta(stream_id, full_content)
                                send_succeeded = send_stream_event(handler_info, stream_id, content, False)
                                if send_failure_tracker.should_abort_after(send_succeeded):
                                    # Client gone: stop the CLI instead of running it to completion
                                    MCPLogger.log(TOOL_LOG_NAME, f"Aborting gemini-cli stream {stream_id}: client disconnected")
                                    proc.terminate()
                                    _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                    return
                            elif not full_content:
                                full_content = content

                    elif event_type == 'result':
                        stats = event.get('stats', {})
                        usage_from_cli = {
                            'input_tokens': stats.get('input_tokens', 0),
                            'output_tokens': stats.get('output_tokens', 0),
                        }

            proc.wait()

            prompt_token_count = usage_from_cli.get('input_tokens', 0)
            completion_token_count = usage_from_cli.get('output_tokens', 0)

            final_usage = {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_token_count,
                "total_tokens": prompt_token_count + completion_token_count
            }
            _mark_stream_complete(stream_id, content=full_content, usage=final_usage)

            send_stream_event(handler_info, stream_id, "", True,
                              usage={
                                  "prompt_tokens": prompt_token_count,
                                  "completion_tokens": completion_token_count,
                                  "total_tokens": prompt_token_count + completion_token_count,
                                  "gemini_cli_session_id": session_id_from_cli,
                              })

            if proc.returncode and proc.returncode != 0:
                MCPLogger.log(TOOL_LOG_NAME,
                              f"Gemini CLI stream process exited with code {proc.returncode}: {get_captured_stderr_tail()[:500]}")

        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Gemini CLI stream error: {e}\n{traceback.format_exc()}")
            _mark_stream_complete(stream_id, error=str(e))
            send_stream_event(handler_info, stream_id, "", True, error=str(e))
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with _streams_lock:
                _active_stream_subprocesses.pop(stream_id, None)

    thread = threading.Thread(target=stream_reader_worker, daemon=True)
    thread.start()

    return {
        "content": [{"type": "text", "text": json.dumps({
            "stream_id": stream_id,
            "status": "streaming",
            "model": model or "gemini-cli-default"
        })}],
        "isError": False
    }


def chat_ollama(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via Ollama with native tool calling support.
    
    Ollama provides native tool calling for models like Qwen3 and Llama 3.1.
    """
    try:
        ollama = ensure_ollama()
    except ImportError as e:
        return create_error_response(f"Failed to load Ollama: {e}", with_readme=False)
    
    model = params.get('model', 'qwen3:8b')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    images = params.get('images', [])
    ollama_host = params.get('ollama_host', 'http://localhost:11434')
    
    # Build Ollama messages; images attach to the LAST user message (the current
    # turn), not the first one encountered
    last_user_message_index = -1
    for message_index, msg in enumerate(messages):
        if msg.get('role') == 'user':
            last_user_message_index = message_index
    ollama_messages = []
    for message_index, msg in enumerate(messages):
        ollama_msg = {
            'role': msg.get('role'),
            'content': msg.get('content', '')
        }
        if message_index == last_user_message_index and images:
            ollama_msg['images'] = images
        ollama_messages.append(ollama_msg)
    
    # Build options -- extra params from the caller go here (num_ctx, repeat_penalty, etc.)
    options = {'temperature': temperature}
    options.update(_extract_extra_provider_specific_parameters(params))
    
    try:
        # Create client with custom host if specified
        if ollama_host != 'http://localhost:11434':
            client = ollama.Client(host=ollama_host)
            chat_func = client.chat
        else:
            chat_func = ollama.chat
        
        if stream:
            # Streaming mode
            stream_id = str(uuid.uuid4())
            
            registration_error = _register_new_stream(StreamState(
                stream_id=stream_id,
                provider=Provider.OLLAMA,
                model=model,
                session_id=handler_info.get('session_id', ''),
                request_id=handler_info.get('request_id', '')
            ))
            if registration_error:
                return create_error_response(registration_error, with_readme=False)
            
            def stream_worker():
                try:
                    full_content = ""
                    token_count = 0
                    usage_from_final_chunk: Dict = {}
                    send_failure_tracker = _ConsecutiveSendFailureTracker()
                    
                    response_stream = chat_func(
                        model=model,
                        messages=ollama_messages,
                        options=options,
                        stream=True
                    )
                    
                    for chunk in response_stream:
                        if _stream_is_cancelled(stream_id):
                            MCPLogger.log(TOOL_LOG_NAME, f"Ollama stream {stream_id} cancelled")
                            _mark_stream_complete(stream_id, content=full_content)
                            send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                            return
                        # The final chunk (done=True) carries real token counts
                        if chunk.get('done'):
                            prompt_eval_count = chunk.get('prompt_eval_count')
                            eval_count = chunk.get('eval_count')
                            if prompt_eval_count is not None or eval_count is not None:
                                usage_from_final_chunk = {
                                    "prompt_tokens": prompt_eval_count or 0,
                                    "completion_tokens": eval_count or 0,
                                    "total_tokens": (prompt_eval_count or 0) + (eval_count or 0)
                                }
                        delta = chunk.get('message', {}).get('content', '')
                        if delta:
                            full_content += delta
                            token_count += 1
                            _record_stream_delta(stream_id, full_content)
                            
                            send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                            if send_failure_tracker.should_abort_after(send_succeeded):
                                MCPLogger.log(TOOL_LOG_NAME, f"Aborting Ollama stream {stream_id}: client disconnected")
                                _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                return
                    
                    # Send completion
                    final_usage = usage_from_final_chunk or {"completion_tokens": token_count, "estimated": True}
                    _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                    send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Ollama stream error: {e}")
                    _mark_stream_complete(stream_id, error=str(e))
                    send_stream_event(handler_info, stream_id, "", True, error=str(e))
            
            thread = threading.Thread(target=stream_worker, daemon=True)
            thread.start()
            
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "streaming",
                    "model": model
                })}],
                "isError": False
            }
        
        else:
            # Non-streaming
            response = chat_func(
                model=model,
                messages=ollama_messages,
                options=options,
                stream=False
            )
            
            # Convert to OpenAI format
            result = {
                "id": f"chatcmpl-ollama-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response.message.content or ""
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": response.prompt_eval_count or 0,
                    "completion_tokens": response.eval_count or 0,
                    "total_tokens": (response.prompt_eval_count or 0) + (response.eval_count or 0)
                }
            }
            
            # Check for tool calls
            if hasattr(response.message, 'tool_calls') and response.message.tool_calls:
                tool_calls = []
                for tc in response.message.tool_calls:
                    tool_calls.append({
                        "id": str(uuid.uuid4()),
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": json.dumps(tc.function.arguments) if isinstance(tc.function.arguments, dict) else tc.function.arguments
                        }
                    })
                result["choices"][0]["message"]["tool_calls"] = tool_calls
                result["choices"][0]["finish_reason"] = "tool_calls"
            
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
            
    except Exception as e:
        error_msg = str(e)
        if "connection refused" in error_msg.lower() or "connect" in error_msg.lower():
            return create_error_response(
                f"Cannot connect to Ollama server at {ollama_host}. "
                "Make sure Ollama is running: 'ollama serve'", 
                with_readme=False
            )
        return create_error_response(f"Ollama error: {e}", with_readme=False)


def chat_ollama_with_tools(params: Dict, handler_info: Dict, tools: List[Dict], stream: bool) -> Dict:
    """Handle Ollama chat with native tool calling."""
    try:
        ollama = ensure_ollama()
    except ImportError as e:
        return create_error_response(f"Failed to load Ollama: {e}", with_readme=False)
    
    model = params.get('model', 'qwen3:8b')
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    images = params.get('images', [])
    ollama_host = params.get('ollama_host', 'http://localhost:11434')
    tool_choice = params.get('tool_choice', 'auto')
    
    # Build Ollama messages; images attach to the LAST user message (current turn)
    last_user_message_index = -1
    for message_index, msg in enumerate(messages):
        if msg.get('role') == 'user':
            last_user_message_index = message_index
    ollama_messages = []
    for message_index, msg in enumerate(messages):
        ollama_msg = {
            'role': msg.get('role'),
            'content': msg.get('content', '')
        }
        if message_index == last_user_message_index and images:
            ollama_msg['images'] = images
        ollama_messages.append(ollama_msg)
    
    # Convert OpenAI-format tools to Ollama format (they're compatible)
    ollama_tools = tools
    
    options = {'temperature': temperature}
    options.update(_extract_extra_provider_specific_parameters(params))
    
    try:
        if ollama_host != 'http://localhost:11434':
            client = ollama.Client(host=ollama_host)
            chat_func = client.chat
        else:
            chat_func = ollama.chat
        
        response = chat_func(
            model=model,
            messages=ollama_messages,
            tools=ollama_tools,
            options=options,
            stream=False  # Tool calling doesn't work well with streaming
        )
        
        # Convert to OpenAI format
        result = {
            "id": f"chatcmpl-ollama-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.message.content or ""
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": response.prompt_eval_count or 0,
                "completion_tokens": response.eval_count or 0,
                "total_tokens": (response.prompt_eval_count or 0) + (response.eval_count or 0)
            }
        }
        
        # Check for tool calls - native tool_calls field
        if hasattr(response.message, 'tool_calls') and response.message.tool_calls:
            tool_calls = []
            for tc in response.message.tool_calls:
                tool_calls.append({
                    "id": str(uuid.uuid4()),
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": json.dumps(tc.function.arguments) if isinstance(tc.function.arguments, dict) else tc.function.arguments
                    }
                })
            result["choices"][0]["message"]["tool_calls"] = tool_calls
            result["choices"][0]["finish_reason"] = "tool_calls"
        
        # Also check for tool calls in content (some models return JSON in content
        # instead of tool_calls) - shared helper, same logic as mlx/llama_cpp paths
        elif response.message.content and tools:
            result = _extract_tool_calls_from_openai_compatible_response(result, tools)
        
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False
        }
        
    except Exception as e:
        error_msg = str(e)
        if "connection refused" in error_msg.lower():
            return create_error_response(
                f"Cannot connect to Ollama at {ollama_host}. Run: ollama serve",
                with_readme=False
            )
        return create_error_response(f"Ollama error: {e}", with_readme=False)


def _convert_image_reference_to_openai_image_url_block(image_reference: str) -> Dict:
    """Turn an image reference (file path, URL, or data URI) into an OpenAI-style
    image_url content block for multimodal chat handlers."""
    image_url_value = image_reference
    if os.path.isfile(image_reference):
        import base64
        import mimetypes
        guessed_mime_type = mimetypes.guess_type(image_reference)[0] or "image/png"
        with open(image_reference, 'rb') as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('ascii')
        image_url_value = f"data:{guessed_mime_type};base64,{encoded_image}"
    return {"type": "image_url", "image_url": {"url": image_url_value}}


def chat_llama_cpp(params: Dict, handler_info: Dict, stream: bool) -> Dict:
    """Handle chat completion via llama-cpp-python for GGUF models.

    Uses create_chat_completion(messages=...) so the model's EMBEDDED chat
    template is applied (Llama/Mistral GGUFs no longer get a hardcoded Qwen
    template), the full conversation history is preserved (assistant turns
    included), and tools/images pass through where the model supports them.
    """
    try:
        Llama = ensure_llama_cpp()
    except ImportError as e:
        return create_error_response(f"Failed to load llama-cpp-python: {e}", with_readme=False)
    
    model_arg = params.get('model') or params.get('gguf_path')
    if not model_arg:
        # List available models
        available = get_gguf_models()
        return create_error_response(
            f"model or gguf_path required. Available GGUF models: {list(available.keys())}",
            with_readme=False
        )
    
    try:
        model_path = resolve_gguf_path(model_arg)
    except ValueError as e:
        return create_error_response(str(e), with_readme=False)
    
    messages = params.get('messages', [])
    temperature = params.get('temperature', 0.7)
    max_tokens = params.get('max_tokens', 1000)
    gpu_layers = params.get('gpu_layers', -1)
    context_length = params.get('context_length', 8192)
    images = params.get('images', [])
    tools = params.get('tools', [])
    
    # Full history goes to the model; images attach to the last user message
    # as image_url blocks (requires a multimodal chat handler in the model)
    chat_messages = [{'role': msg.get('role'), 'content': msg.get('content', '')} for msg in messages]
    if images:
        last_user_message_index = -1
        for message_index, msg in enumerate(chat_messages):
            if msg.get('role') == 'user':
                last_user_message_index = message_index
        if last_user_message_index >= 0:
            text_content = chat_messages[last_user_message_index].get('content', '')
            content_blocks = [{"type": "text", "text": text_content}] if isinstance(text_content, str) else list(text_content)
            for image_reference in images:
                content_blocks.append(_convert_image_reference_to_openai_image_url_block(image_reference))
            chat_messages[last_user_message_index]['content'] = content_blocks

    try:
        # Load model (with caching + the shared load lock so two concurrent
        # requests can't double-load the same multi-GB model)
        cache_key = f"llama_cpp_{model_path}_{gpu_layers}_{context_length}"
        
        with _model_load_lock:
            if cache_key not in _loaded_models:
                MCPLogger.log(TOOL_LOG_NAME, f"Loading GGUF model: {model_path}")
                _evict_least_recently_used_models_locked()
                llm = Llama(
                    model_path=model_path,
                    n_ctx=context_length,
                    n_gpu_layers=gpu_layers,
                    verbose=False
                )
                _loaded_models[cache_key] = {"llm": llm, "last_used_at": time.time()}
                MCPLogger.log(TOOL_LOG_NAME, f"GGUF model loaded successfully")
            else:
                _loaded_models[cache_key]["last_used_at"] = time.time()
                llm = _loaded_models[cache_key]["llm"]
        
        completion_kwargs = {
            "messages": chat_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            completion_kwargs["tools"] = tools

        if stream:
            stream_id = str(uuid.uuid4())
            
            registration_error = _register_new_stream(StreamState(
                stream_id=stream_id,
                provider=Provider.LLAMA_CPP,
                model=model_arg,
                session_id=handler_info.get('session_id', ''),
                request_id=handler_info.get('request_id', '')
            ))
            if registration_error:
                return create_error_response(registration_error, with_readme=False)
            
            def stream_worker():
                try:
                    full_content = ""
                    token_count = 0
                    send_failure_tracker = _ConsecutiveSendFailureTracker()
                    
                    for output in llm.create_chat_completion(stream=True, **completion_kwargs):
                        if _stream_is_cancelled(stream_id):
                            MCPLogger.log(TOOL_LOG_NAME, f"llama.cpp stream {stream_id} cancelled")
                            _mark_stream_complete(stream_id, content=full_content)
                            send_stream_event(handler_info, stream_id, "", True, error="Cancelled by user")
                            return
                        choices = output.get('choices') or [{}]
                        delta = choices[0].get('delta', {}).get('content', '')
                        if delta:
                            full_content += delta
                            token_count += 1
                            _record_stream_delta(stream_id, full_content)
                            
                            send_succeeded = send_stream_event(handler_info, stream_id, delta, False)
                            if send_failure_tracker.should_abort_after(send_succeeded):
                                MCPLogger.log(TOOL_LOG_NAME, f"Aborting llama.cpp stream {stream_id}: client disconnected")
                                _mark_stream_complete(stream_id, content=full_content, error="Client disconnected")
                                return
                    
                    final_usage = {"completion_tokens": token_count, "estimated": True}
                    _mark_stream_complete(stream_id, content=full_content, usage=final_usage)
                    send_stream_event(handler_info, stream_id, "", True, usage=final_usage)
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"llama.cpp stream error: {e}")
                    _mark_stream_complete(stream_id, error=str(e))
                    send_stream_event(handler_info, stream_id, "", True, error=str(e))
            
            thread = threading.Thread(target=stream_worker, daemon=True)
            thread.start()
            
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "streaming",
                    "model": model_arg
                })}],
                "isError": False
            }
        
        else:
            # Non-streaming: create_chat_completion already returns OpenAI format
            output = llm.create_chat_completion(**completion_kwargs)
            
            result = {
                "id": output.get('id', f"chatcmpl-llamacpp-{int(time.time())}"),
                "object": "chat.completion",
                "created": output.get('created', int(time.time())),
                "model": model_arg,
                "choices": output.get('choices', []),
                "usage": output.get('usage', {})
            }

            if tools:
                result = _extract_tool_calls_from_openai_compatible_response(result, tools)

            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
            
    except Exception as e:
        return create_error_response(f"llama.cpp error: {e}", with_readme=False)


# ============================================================================
# Operation Handlers
# ============================================================================


def _resolve_endpoint_into_params(params: Dict) -> None:
    """If params contains an 'endpoint' key, resolve it from shared_config and merge.

    Looks up the named endpoint in settings[0].llm_endpoints, then injects:
      - provider (from endpoint's provider_type)
      - mlx_host / ollama_host / base_url (from endpoint's base_url)
      - api_key (resolved from endpoint's api_key_ref)

    Existing explicit params take precedence (are NOT overwritten).
    Removes the 'endpoint' key from params after resolution.
    """
    endpoint_name = params.pop("endpoint", None)
    if not endpoint_name:
        return
    try:
        from ragtag.shared_config import get_llm_endpoint_config
        cfg = get_llm_endpoint_config(endpoint_name)
    except Exception:
        return
    if cfg is None:
        return

    provider_type = cfg.get("provider_type", "")
    base_url = cfg.get("base_url", "")
    api_key = cfg.get("api_key")

    if "provider" not in params and provider_type:
        params["provider"] = provider_type

    if provider_type == "mlx" and "mlx_host" not in params and base_url:
        params["mlx_host"] = base_url
    elif provider_type == "ollama" and "ollama_host" not in params and base_url:
        params["ollama_host"] = base_url
    elif provider_type in ("llama_cpp", "custom") and "base_url" not in params and base_url:
        if not base_url.rstrip("/").endswith("/v1"):
            params["base_url"] = base_url.rstrip("/") + "/v1"
        else:
            params["base_url"] = base_url
    elif "base_url" not in params and base_url:
        params["base_url"] = base_url

    if api_key and "api_key" not in params:
        params["api_key"] = api_key


def _validate_chat_parameters(params: Dict) -> Optional[str]:
    """Cheap synchronous preflight so obvious bad input fails the tool call itself
    instead of only surfacing later as an SSE error event from a worker thread.

    Returns an error message string, or None when the parameters look sane.
    """
    messages = params.get('messages')
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array of {role, content} objects"
    for message_index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return f"messages[{message_index}] must be an object with role and content"
        if not msg.get('role'):
            return f"messages[{message_index}] is missing a role"

    temperature = params.get('temperature')
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            return "temperature must be a number between 0.0 and 2.0"
        if not (0.0 <= float(temperature) <= 2.0):
            return "temperature must be between 0.0 and 2.0"

    max_tokens = params.get('max_tokens')
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            return "max_tokens must be an integer >= 1"
        if max_tokens < 1:
            return "max_tokens must be >= 1"

    max_tool_rounds = params.get('max_tool_rounds')
    if max_tool_rounds is not None:
        if isinstance(max_tool_rounds, bool) or not isinstance(max_tool_rounds, int) or max_tool_rounds < 0 or max_tool_rounds > 50:
            return "max_tool_rounds must be an integer between 0 and 50"

    if params.get('stream') is not None and not isinstance(params.get('stream'), bool):
        return "stream must be a boolean"

    tools = params.get('tools')
    if tools is not None and not isinstance(tools, list):
        return "tools must be an array of tool definition objects"
    allowed_tools = params.get('allowed_tools')
    if allowed_tools is not None and not isinstance(allowed_tools, list):
        return "allowed_tools must be an array of tool names"

    return None


def handle_chat(params: Dict, handler_info: Dict) -> Dict:
    """Handle unified chat operation with optional tool calling support.

    tool_execution modes:
      "llm_managed" (default) — llm.py runs the full ReAct loop: calls tools internally
        via execute_mcp_tool, feeds results back to the LLM, repeats up to max_tool_rounds.
      "caller_managed" — llm.py sends tool definitions to the model and returns the raw
        response (including any tool_calls) WITHOUT executing them. The caller (e.g. agent
        kernel) is responsible for executing tools, policy checks, checkpointing, etc.
    """
    _resolve_endpoint_into_params(params)
    provider_str = params.get('provider', 'openrouter')
    stream = params.get('stream', False)

    tools = params.get('tools', [])
    allowed_tools = params.get('allowed_tools', [])
    max_tool_rounds = params.get('max_tool_rounds', 10)
    tool_execution_mode = params.get('tool_execution', 'llm_managed')

    try:
        provider = Provider(provider_str)
    except ValueError:
        return create_error_response(f"Unknown provider: {provider_str}. Valid: local, ollama, llama_cpp, mlx, cursor_agent, claude_code, codex_cli, gemini_cli, openrouter, openai, anthropic, custom", with_readme=False)

    if provider == Provider.LLAMA_CPP and params.get('base_url'):
        MCPLogger.log(TOOL_LOG_NAME, f"llama_cpp with base_url={params['base_url']} — routing via chat_custom (remote llama-server)")
        provider = Provider.CUSTOM
        provider_str = "custom"

    if not params.get('messages'):
        return create_error_response("messages array is required", with_readme=True)

    validation_error = _validate_chat_parameters(params)
    if validation_error:
        return create_error_response(validation_error, with_readme=False)

    # Process source:"url"/"file" message content ONCE here, for every provider
    # (previously only the openrouter paths did this)
    try:
        params['messages'] = [process_message_content(msg) for msg in params.get('messages', [])]
    except Exception as e:
        return create_error_response(f"Failed to process message content: {e}", with_readme=False)

    if tools and tool_execution_mode == 'llm_managed' and not allowed_tools:
        return create_error_response("allowed_tools is required when tools are provided with tool_execution='llm_managed'. Use ['*'] for all tools, or set tool_execution='caller_managed' if you will execute tools yourself.", with_readme=True)

    caller_managed_tool_mode = (tool_execution_mode == 'caller_managed')

    if tools and stream and provider in (Provider.OPENROUTER, Provider.OPENAI) and not caller_managed_tool_mode:
        MCPLogger.log(TOOL_LOG_NAME, f"Using streaming-with-tools mode ({provider.value})")
        return chat_openai_compatible_streaming_with_tools(params, handler_info, provider)

    if tools and stream and provider in (Provider.ANTHROPIC, Provider.OLLAMA) and not caller_managed_tool_mode:
        # Same tool_call event protocol as the SSE loop; text deltas arrive per round
        MCPLogger.log(TOOL_LOG_NAME, f"Using streaming tool loop via blocking rounds ({provider.value})")
        return chat_tool_loop_streaming_via_blocking_rounds(params, handler_info, provider)

    if tools and stream and not caller_managed_tool_mode:
        MCPLogger.log(TOOL_LOG_NAME, "Warning: Tool calling with streaming - tool loop will not auto-execute. Client must handle tool calls.")

    if tools and not stream:
        providers_with_openai_compatible_tool_passthrough = {
            Provider.MLX, Provider.CUSTOM, Provider.LLAMA_CPP,
        }

        if caller_managed_tool_mode and provider in providers_with_openai_compatible_tool_passthrough:
            MCPLogger.log(TOOL_LOG_NAME, f"caller_managed tool mode: passing tools to {provider_str} and returning raw response")
            if provider == Provider.MLX:
                return chat_mlx(params, handler_info, stream=False)
            elif provider == Provider.CUSTOM:
                return chat_custom(params, handler_info, stream=False)
            elif provider == Provider.LLAMA_CPP:
                return chat_llama_cpp(params, handler_info, stream=False)

        if provider == Provider.OLLAMA:
            initial_response = chat_ollama_with_tools(params, handler_info, tools, stream=False)
        elif provider == Provider.OPENROUTER:
            initial_response = chat_openrouter_with_tools(params, handler_info, tools, stream=False)
        elif provider == Provider.OPENAI:
            initial_response = chat_openai_with_tools(params, handler_info, tools, stream=False)
        elif provider == Provider.ANTHROPIC:
            initial_response = chat_anthropic_with_tools(params, handler_info, tools, stream=False)
        elif provider == Provider.MLX:
            MCPLogger.log(TOOL_LOG_NAME, "MLX with tools: passing through via chat_mlx (tools in request body)")
            initial_response = chat_mlx(params, handler_info, stream=False)
        elif provider == Provider.CUSTOM:
            MCPLogger.log(TOOL_LOG_NAME, "Custom with tools: passing through via chat_custom (tools in request body)")
            initial_response = chat_custom(params, handler_info, stream=False)
        elif provider == Provider.LLAMA_CPP:
            MCPLogger.log(TOOL_LOG_NAME, "llama_cpp with tools: passing through via chat_llama_cpp (tools in request body)")
            initial_response = chat_llama_cpp(params, handler_info, stream=False)
        elif provider == Provider.LOCAL:
            return create_error_response("Local (transformers) models do not support tool calling. Use 'ollama' provider instead.", with_readme=False)
        elif provider == Provider.CURSOR_AGENT:
            return create_error_response("Cursor agent handles its own tool calling via the harness. Pass tools in the briefing message instead.", with_readme=False)
        elif provider == Provider.CLAUDE_CODE:
            return create_error_response("Claude Code handles its own tool calling via the CLI harness. Pass tools in the briefing message instead.", with_readme=False)
        else:
            return create_error_response(f"Provider {provider_str} does not support tool calling", with_readme=False)

        if initial_response.get('isError'):
            return initial_response

        if caller_managed_tool_mode:
            MCPLogger.log(TOOL_LOG_NAME, "caller_managed: returning response with tool_calls (if any) without executing them")
            return initial_response

        try:
            response_text = initial_response['content'][0]['text']
            response_data = json.loads(response_text)
            message = response_data.get('choices', [{}])[0].get('message', {})

            if message.get('tool_calls'):
                messages = list(params.get('messages', []))
                tool_mapping = params.get('tool_mapping', {})
                return process_tool_calls_and_continue(
                    params, handler_info, message, messages, tools,
                    allowed_tools, max_tool_rounds, current_round=1, tool_mapping=tool_mapping
                )
            else:
                return initial_response

        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error parsing tool response: {e}")
            return initial_response

    if provider == Provider.OLLAMA:
        return chat_ollama(params, handler_info, stream)
    elif provider == Provider.MLX:
        return chat_mlx(params, handler_info, stream)
    elif provider == Provider.CURSOR_AGENT:
        return chat_cursor_agent(params, handler_info, stream)
    elif provider == Provider.CLAUDE_CODE:
        return chat_claude_code(params, handler_info, stream)
    elif provider == Provider.CODEX_CLI:
        return chat_codex_cli(params, handler_info, stream)
    elif provider == Provider.GEMINI_CLI:
        return chat_gemini_cli(params, handler_info, stream)
    elif provider == Provider.LLAMA_CPP:
        return chat_llama_cpp(params, handler_info, stream)
    elif provider == Provider.OPENROUTER:
        return chat_openrouter(params, handler_info, stream)
    elif provider == Provider.LOCAL:
        return chat_local(params, handler_info, stream)
    elif provider == Provider.OPENAI:
        return chat_openai(params, handler_info, stream)
    elif provider == Provider.ANTHROPIC:
        return chat_anthropic(params, handler_info, stream)
    elif provider == Provider.CUSTOM:
        return chat_custom(params, handler_info, stream)
    else:
        return create_error_response(f"Provider {provider_str} not implemented", with_readme=False)


def handle_list_providers(params: Dict) -> Dict:
    """List available LLM providers."""
    providers = [
        {
            "id": "ollama",
            "name": "Ollama",
            "description": "Local Ollama server with native tool calling (qwen3, llama3.1, etc.)",
            "streaming": True,
            "tool_calling": True,
            "requires_api_key": False,
            "auto_install": True
        },
        {
            "id": "mlx",
            "name": "MLX (Apple Silicon)",
            "description": "Run MLX models locally via mlx_vlm.server (OpenAI-compatible). Anti-looping defaults for Qwen3.5.",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False,
            "extra_params_passthrough": True
        },
        {
            "id": "cursor_agent",
            "name": "Cursor Agent CLI",
            "description": "Access 80+ cloud models via Cursor subscription (requires 'agent' CLI)",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False,
            "available": _detect_cursor_agent_cli_path() is not None
        },
        {
            "id": "claude_code",
            "name": "Claude Code CLI",
            "description": "Access Claude models via Anthropic subscription (opus-4-7, sonnet-4-6, haiku-4-5)",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False,
            "available": _detect_claude_code_cli_path() is not None
        },
        {
            "id": "codex_cli",
            "name": "OpenAI Codex CLI",
            "description": "Agentic coding via OpenAI Codex CLI (gpt-5.5, gpt-5.2-codex, etc) — requires codex MCP bridge in local_mcpServers",
            "streaming": False,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False,
            "available": _check_codex_mcp_bridge_is_available()
        },
        {
            "id": "gemini_cli",
            "name": "Google Gemini CLI",
            "description": "Access Gemini models via Google subscription (gemini-3-flash, gemini-2.5-pro, etc)",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False,
            "available": _detect_gemini_cli_path() is not None
        },
        {
            "id": "llama_cpp",
            "name": "llama.cpp (GGUF)",
            "description": "Run GGUF models locally via llama-cpp-python",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": True
        },
        {
            "id": "local",
            "name": "Local (Transformers)",
            "description": "Run HuggingFace models locally using PyTorch/Transformers",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False
        },
        {
            "id": "openrouter",
            "name": "OpenRouter",
            "description": "Cloud API with 300+ models from multiple providers",
            "streaming": True,
            "tool_calling": True,
            "requires_api_key": True,
            "auto_install": False
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "description": "Direct OpenAI API (GPT-4, GPT-4o, etc.)",
            "streaming": True,
            "tool_calling": True,
            "requires_api_key": True,
            "auto_install": False
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "description": "Direct Anthropic API (Claude models)",
            "streaming": True,
            "tool_calling": True,
            "requires_api_key": True,
            "auto_install": False
        },
        {
            "id": "custom",
            "name": "Custom Endpoint",
            "description": "Any OpenAI-compatible API endpoint",
            "streaming": True,
            "tool_calling": False,
            "requires_api_key": False,
            "auto_install": False
        }
    ]
    
    return {
        "content": [{"type": "text", "text": json.dumps({"providers": providers}, indent=2)}],
        "isError": False
    }


def handle_list_models(params: Dict) -> Dict:
    """List models for a provider."""
    _resolve_endpoint_into_params(params)
    provider_str = params.get('provider', 'openrouter')
    
    if provider_str == 'ollama':
        # List models from Ollama server.
        # ollama_host is assigned BEFORE the try so the except handler below can
        # reference it even when ensure_ollama() raises
        ollama_host = params.get('ollama_host', 'http://localhost:11434')
        try:
            ollama = ensure_ollama()
            
            if ollama_host != 'http://localhost:11434':
                client = ollama.Client(host=ollama_host)
                models_response = client.list()
            else:
                models_response = ollama.list()
            
            models = []
            # Handle both dict-style and Pydantic model responses
            models_list = models_response.models if hasattr(models_response, 'models') else models_response.get('models', [])
            
            for model in models_list:
                # Handle Pydantic model or dict
                if hasattr(model, 'model'):
                    # Pydantic model
                    model_id = model.model
                    size = model.size if hasattr(model, 'size') else 0
                    modified_at = model.modified_at if hasattr(model, 'modified_at') else ''
                    details = model.details if hasattr(model, 'details') else None
                    family = details.family if details and hasattr(details, 'family') else ''
                    param_size = details.parameter_size if details and hasattr(details, 'parameter_size') else ''
                else:
                    # Dict
                    model_id = model.get('name', model.get('model', ''))
                    size = model.get('size', 0)
                    modified_at = model.get('modified_at', '')
                    family = model.get('details', {}).get('family', '')
                    param_size = model.get('details', {}).get('parameter_size', '')
                
                # Handle datetime serialization
                if hasattr(modified_at, 'isoformat'):
                    modified_at = modified_at.isoformat()
                
                models.append({
                    "id": model_id,
                    "size_gb": round(size / (1024**3), 2) if size else 0,
                    "modified": str(modified_at),
                    "family": family,
                    "parameter_size": param_size
                })
            
            return {
                "content": [{"type": "text", "text": json.dumps({"models": models, "provider": "ollama"}, indent=2)}],
                "isError": False
            }
        except Exception as e:
            error_msg = str(e)
            if "connection refused" in error_msg.lower():
                return create_error_response(f"Cannot connect to Ollama server at {ollama_host}. Run: ollama serve", with_readme=False)
            return create_error_response(f"Error listing Ollama models at {ollama_host}: {e}", with_readme=False)

    elif provider_str == 'mlx':
        import http.client
        from urllib.parse import urlparse
        mlx_host = params.get('mlx_host', 'http://localhost:8081')
        try:
            if '://' not in mlx_host:
                mlx_host = f"http://{mlx_host}"
            parsed = urlparse(mlx_host)
            host = parsed.netloc or parsed.path
            use_https = parsed.scheme == 'https'
            conn = http.client.HTTPSConnection(host, timeout=_METADATA_HTTP_TIMEOUT_SECONDS) if use_https else http.client.HTTPConnection(host, timeout=_METADATA_HTTP_TIMEOUT_SECONDS)
            conn.request("GET", "/v1/models")
            response = conn.getresponse()
            response_data = response.read().decode('utf-8')
            conn.close()
            if response.status == 200:
                models_data = json.loads(response_data)
                models = []
                for m in models_data.get('data', models_data.get('models', [])):
                    model_id = m.get('id', m.get('model', ''))
                    if model_id:
                        models.append({"id": model_id, "owned_by": m.get('owned_by', 'mlx')})
                return {
                    "content": [{"type": "text", "text": json.dumps({"models": models, "provider": "mlx"}, indent=2)}],
                    "isError": False
                }
            else:
                return create_error_response(f"MLX API error {response.status}: {response_data}", with_readme=False)
        except Exception as e:
            error_msg = str(e)
            if "connection refused" in error_msg.lower() or "errno 61" in error_msg.lower():
                return create_error_response(
                    f"Cannot connect to MLX server at {mlx_host}. "
                    "Start it with: mlx_vlm.server --host 0.0.0.0 --port 8081",
                    with_readme=False
                )
            return create_error_response(f"Error listing MLX models: {e}", with_readme=False)

    elif provider_str == 'cursor_agent':
        import subprocess
        import re as _re
        agent_path = _detect_cursor_agent_cli_path()
        if not agent_path:
            return create_error_response(
                "Cursor agent CLI not found (tried 'cursor-agent' and 'agent' in PATH). "
                "Install: curl https://cursor.com/install -fsS | bash",
                with_readme=False
            )
        try:
            use_shell_for_cmd_wrappers = agent_path.lower().endswith(('.cmd', '.bat'))
            result = subprocess.run(
                [agent_path, "models"],
                capture_output=True,
                text=True,
                timeout=30,
                shell=use_shell_for_cmd_wrappers,
                creationflags=_subprocess_no_window_flags()
            )
            if result.returncode != 0:
                error_text = (result.stderr or "").strip() or (result.stdout or "").strip()
                return create_error_response(f"Cursor agent models failed: {error_text}", with_readme=False)
            ansi_escape_pattern = _re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\[?[0-9;]*[A-Za-z]')
            clean_output = ansi_escape_pattern.sub('', result.stdout or "")
            models = []
            for line in clean_output.strip().split('\n'):
                line = line.strip()
                if ' - ' in line:
                    model_id, description = line.split(' - ', 1)
                    model_id = model_id.strip()
                    description = description.strip()
                    is_default = '(default)' in description
                    is_current = '(current)' in description
                    description = description.replace('(default)', '').replace('(current)', '').strip()
                    entry = {"id": model_id, "description": description}
                    if is_default:
                        entry["is_default"] = True
                    if is_current:
                        entry["is_current"] = True
                    models.append(entry)
            return {
                "content": [{"type": "text", "text": json.dumps({"models": models, "provider": "cursor_agent"}, indent=2)}],
                "isError": False
            }
        except subprocess.TimeoutExpired:
            return create_error_response("Cursor agent CLI timed out listing models", with_readme=False)
        except Exception as e:
            return create_error_response(f"Error listing cursor agent models: {e}", with_readme=False)

    elif provider_str == 'claude_code':
        claude_code_hardcoded_models = [
            {"id": "claude-opus-4-7", "description": "Most capable Claude model, deep reasoning and analysis"},
            {"id": "claude-sonnet-4-6", "description": "Balanced performance and speed"},
            {"id": "claude-haiku-4-5", "description": "Fastest Claude model, optimised for quick tasks"},
        ]
        return {
            "content": [{"type": "text", "text": json.dumps({"models": claude_code_hardcoded_models, "provider": "claude_code"}, indent=2)}],
            "isError": False
        }

    elif provider_str == 'codex_cli':
        codex_cli_known_models = [
            {"id": "gpt-5.5", "description": "Most capable GPT model (default for Codex CLI)"},
            {"id": "gpt-5.2-codex", "description": "Optimised for code generation and editing"},
            {"id": "gpt-5.2", "description": "General-purpose GPT model"},
            {"id": "gpt-5.1", "description": "Previous generation GPT model"},
            {"id": "gpt-5-mini", "description": "Smaller, faster GPT model"},
            {"id": "o3", "description": "Reasoning-focused model"},
        ]
        return {
            "content": [{"type": "text", "text": json.dumps({"models": codex_cli_known_models, "provider": "codex_cli"}, indent=2)}],
            "isError": False
        }

    elif provider_str == 'gemini_cli':
        gemini_cli_known_models = [
            {"id": "gemini-3-flash-preview", "description": "Fast Gemini 3 model (default)"},
            {"id": "gemini-2.5-pro", "description": "Most capable Gemini model with extended thinking"},
            {"id": "gemini-2.5-flash", "description": "Fast and efficient Gemini model with thinking"},
            {"id": "gemini-2.0-flash", "description": "Previous generation fast model"},
            {"id": "gemma-3-27b-it", "description": "Open-source Gemma 27B (local via Gemma routing)"},
        ]
        return {
            "content": [{"type": "text", "text": json.dumps({"models": gemini_cli_known_models, "provider": "gemini_cli"}, indent=2)}],
            "isError": False
        }

    elif provider_str == 'llama_cpp':
        # List available GGUF models from cache
        try:
            models = get_gguf_models()
            model_list = []
            for alias, path in models.items():
                size_bytes = os.path.getsize(path) if os.path.exists(path) else 0
                model_list.append({
                    "id": alias,
                    "path": path,
                    "size_gb": round(size_bytes / (1024**3), 2)
                })
            
            return {
                "content": [{"type": "text", "text": json.dumps({"models": model_list, "provider": "llama_cpp"}, indent=2)}],
                "isError": False
            }
        except Exception as e:
            return create_error_response(f"Error listing GGUF models: {e}", with_readme=False)
    
    elif provider_str == 'local':
        # Scan HuggingFace cache directly
        try:
            result = handle_list_installed_models(params)
            if result.get("isError"):
                return result
            # Parse the result and reformat
            models_json = json.loads(result["content"][0]["text"])
            return {
                "content": [{"type": "text", "text": json.dumps({"models": models_json}, indent=2)}],
                "isError": False
            }
        except Exception as e:
            return create_error_response(f"Error listing local models: {e}", with_readme=False)
    
    elif provider_str == 'openrouter':
        # Query the OpenRouter database (with freshness check)
        try:
            from .sqlite import sqlite
            
            # Check if database needs refresh
            needs_refresh, error = check_models_database_freshness()
            if error:
                MCPLogger.log(TOOL_LOG_NAME, f"Warning: Database freshness check failed: {error}")
            if needs_refresh:
                MCPLogger.log(TOOL_LOG_NAME, "list_models is refreshing stale OpenRouter DB")
                success, refresh_error = refresh_models_database()
                if not success:
                    return create_error_response(f"Failed to refresh models database: {refresh_error}", with_readme=False)
            
            db_path = get_openrouter_db_path()
            # int() coercion + clamp: max_results is interpolated into SQL below
            try:
                max_results = int(params.get('max_results', 50))
            except (TypeError, ValueError):
                return create_error_response("max_results must be an integer", with_readme=False)
            max_results = max(1, min(1000, max_results))
            
            result = sqlite(
                sql=f"SELECT id, context_length FROM models ORDER BY id LIMIT {max_results}",
                database=db_path
            )
            
            if result.get("operation_was_successful"):
                models = result.get("data_rows_from_result_set", [])
                return {
                    "content": [{"type": "text", "text": json.dumps({"models": models}, indent=2)}],
                    "isError": False
                }
            else:
                return create_error_response(f"Error listing OpenRouter models: {result.get('error_message_if_operation_failed')}", with_readme=False)
        except Exception as e:
            return create_error_response(f"Error listing OpenRouter models: {e}", with_readme=False)
    
    elif provider_str == 'openai':
        # Live fetch from GET /v1/models; the hardcoded list is only an offline fallback
        live_models = _fetch_live_model_list_from_provider(
            Provider.OPENAI, "api.openai.com", "/v1/models", params)
        if live_models is not None:
            return {
                "content": [{"type": "text", "text": json.dumps({"models": live_models, "source": "live"}, indent=2)}],
                "isError": False
            }
        models = [
            {"id": "gpt-4o", "description": "Most capable model"},
            {"id": "gpt-4o-mini", "description": "Fast and affordable"},
            {"id": "gpt-4-turbo", "description": "Previous generation flagship"},
            {"id": "gpt-3.5-turbo", "description": "Fast, good for simple tasks"}
        ]
        return {
            "content": [{"type": "text", "text": json.dumps({"models": models, "source": "static_fallback"}, indent=2)}],
            "isError": False
        }
    
    elif provider_str == 'anthropic':
        # Live fetch from GET /v1/models; the hardcoded list is only an offline fallback
        live_models = _fetch_live_model_list_from_provider(
            Provider.ANTHROPIC, "api.anthropic.com", "/v1/models", params)
        if live_models is not None:
            return {
                "content": [{"type": "text", "text": json.dumps({"models": live_models, "source": "live"}, indent=2)}],
                "isError": False
            }
        models = [
            {"id": "claude-3-5-sonnet-20241022", "description": "Best balance of speed and capability"},
            {"id": "claude-3-opus-20240229", "description": "Most capable"},
            {"id": "claude-3-sonnet-20240229", "description": "Previous generation"},
            {"id": "claude-3-haiku-20240307", "description": "Fastest"}
        ]
        return {
            "content": [{"type": "text", "text": json.dumps({"models": models, "source": "static_fallback"}, indent=2)}],
            "isError": False
        }
    
    else:
        return create_error_response(f"Cannot list models for provider: {provider_str}", with_readme=False)


def _fetch_live_model_list_from_provider(provider: Provider, api_host: str, api_path: str,
                                         params: Dict) -> Optional[List[Dict]]:
    """GET a provider's live /v1/models list. Returns the parsed list, or None on
    any failure (missing key, network error) so callers fall back to their static list."""
    import http.client

    api_key = get_api_key(provider, params.get('api_key'), interactive=False)
    if not api_key:
        return None
    if provider == Provider.ANTHROPIC:
        request_headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        request_headers = {"Authorization": f"Bearer {api_key}"}
    conn = None
    try:
        conn = http.client.HTTPSConnection(api_host, timeout=_METADATA_HTTP_TIMEOUT_SECONDS)
        conn.request("GET", api_path, headers=request_headers)
        response = conn.getresponse()
        response_text = response.read().decode('utf-8', errors='replace')
        if response.status != 200:
            MCPLogger.log(TOOL_LOG_NAME, f"Live model list fetch from {api_host} failed: {response.status}")
            return None
        parsed = json.loads(response_text)
        live_models = []
        for model_entry in parsed.get('data', []):
            live_models.append({
                "id": model_entry.get('id', ''),
                "description": model_entry.get('display_name', model_entry.get('owned_by', ''))
            })
        return live_models if live_models else None
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Live model list fetch from {api_host} failed: {e}")
        return None
    finally:
        if conn:
            conn.close()


def _stream_belongs_to_requesting_session(state: StreamState, handler_info: Dict) -> bool:
    """Session scoping: a client may only inspect/cancel its own streams.
    Enforced only when both sides have a session id (internal calls have none)."""
    requesting_session_id = (handler_info or {}).get('session_id', '')
    if not requesting_session_id or not state.session_id:
        return True
    return requesting_session_id == state.session_id


def handle_stream_status(params: Dict, handler_info: Dict = None) -> Dict:
    """Get status of a stream.

    Completed streams are retained for 10 minutes, so 'completed' (with final
    usage and content length) is distinguishable from 'not_found'. Pass
    include_content:true to also receive the accumulated text (lets clients
    that missed SSE events recover the response).
    """
    stream_id = params.get('stream_id')
    if not stream_id:
        return create_error_response("stream_id is required", with_readme=False)
    
    with _streams_lock:
        _prune_completed_streams_locked()
        state = _active_streams.get(stream_id)
        if state:
            if not _stream_belongs_to_requesting_session(state, handler_info):
                return create_error_response("stream_id belongs to a different session", with_readme=False)
            status_payload = state.to_dict()
            if params.get('include_content'):
                status_payload["content_so_far"] = state.content_so_far
            return {
                "content": [{"type": "text", "text": json.dumps(status_payload, indent=2)}],
                "isError": False
            }
        else:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "not_found",
                    "message": "Stream not found (completed streams are retained for 10 minutes, so this id is unknown or expired)"
                })}],
                "isError": False
            }


def handle_cancel_stream(params: Dict, handler_info: Dict = None) -> Dict:
    """Cancel an active stream.

    Sets the cancelled flag that every worker loop checks (HTTP streams stop
    reading and close their connection; local generators stop; CLI subprocesses
    are terminated). The record is retained so stream_status can report it."""
    stream_id = params.get('stream_id')
    if not stream_id:
        return create_error_response("stream_id is required", with_readme=False)
    
    with _streams_lock:
        _prune_completed_streams_locked()
        state = _active_streams.get(stream_id)
        if state:
            if not _stream_belongs_to_requesting_session(state, handler_info):
                return create_error_response("stream_id belongs to a different session", with_readme=False)
            already_finished = state.is_complete
            state.cancelled = True
            if not state.error:
                state.error = "Cancelled by user"
            if not state.is_complete:
                state.is_complete = True
                state.completed_at = time.time()

            proc = _active_stream_subprocesses.pop(stream_id, None)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    MCPLogger.log(TOOL_LOG_NAME, f"Terminated subprocess for cancelled stream {stream_id}")
                except Exception as terminate_error:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error terminating subprocess for stream {stream_id}: {terminate_error}")

            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "already_complete" if already_finished else "cancelled"
                })}],
                "isError": False
            }
        else:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "stream_id": stream_id,
                    "status": "not_found"
                })}],
                "isError": False
            }


# ============================================================================
# Hardware & Model Discovery (from llm.py)
# ============================================================================

# Cache for loaded models (transformers + GGUF), guarded by _model_load_lock so
# two concurrent requests cannot double-load the same multi-GB model. Bounded by
# LRU eviction (_LOADED_MODEL_CACHE_MAX_ENTRIES) so repeated loads cannot OOM.
_loaded_models: Dict[str, Any] = {}
_model_load_lock = threading.Lock()
_torch = None
_transformers = None


def _evict_least_recently_used_models_locked() -> None:
    """Evict least-recently-used cached models until below the cap.
    Caller must hold _model_load_lock."""
    while len(_loaded_models) >= _LOADED_MODEL_CACHE_MAX_ENTRIES:
        least_recently_used_key = min(
            _loaded_models,
            key=lambda cache_key: _loaded_models[cache_key].get("last_used_at", 0.0)
        )
        MCPLogger.log(TOOL_LOG_NAME, f"Evicting cached model (LRU): {least_recently_used_key}")
        _loaded_models.pop(least_recently_used_key, None)
        import gc
        gc.collect()
        try:
            if _torch is not None and _torch.cuda.is_available():
                _torch.cuda.empty_cache()
        except Exception:
            pass


def ensure_torch():
    """Lazy-load PyTorch."""
    global _torch
    if _torch is None:
        try:
            import torch
            _torch = torch
        except ImportError:
            raise ImportError("PyTorch not installed. Run: pip install torch")
    return _torch


def ensure_transformers():
    """Lazy-load transformers."""
    global _transformers
    if _transformers is None:
        try:
            import transformers
            _transformers = transformers
        except ImportError:
            raise ImportError("Transformers not installed. Run: pip install transformers")
    return _transformers


def ensure_ollama():
    """Lazy-load ollama package, auto-installing if needed."""
    global _ollama_module
    if _ollama_module is None:
        try:
            import ollama
            _ollama_module = ollama
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "Installing ollama Python package...")
            import subprocess
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', 'ollama'],
                capture_output=True, text=True,
                creationflags=_subprocess_no_window_flags()
            )
            if result.returncode != 0:
                raise ImportError(f"Failed to install ollama: {result.stderr}")
            import ollama
            _ollama_module = ollama
            MCPLogger.log(TOOL_LOG_NAME, "ollama package installed successfully")
    return _ollama_module


def ensure_llama_cpp():
    """Lazy-load llama-cpp-python, auto-installing if needed."""
    global _llama_cpp_module
    if _llama_cpp_module is None:
        try:
            from llama_cpp import Llama
            _llama_cpp_module = Llama
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "Installing llama-cpp-python...")
            import subprocess
            # Try to install with CUDA support first, fall back to CPU
            cuda_install = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', 'llama-cpp-python', 
                 '--extra-index-url', 'https://abetlen.github.io/llama-cpp-python/whl/cu124'],
                capture_output=True, text=True,
                creationflags=_subprocess_no_window_flags()
            )
            if cuda_install.returncode != 0:
                # Fall back to CPU-only
                cpu_install = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', 'llama-cpp-python'],
                    capture_output=True, text=True,
                    creationflags=_subprocess_no_window_flags()
                )
                if cpu_install.returncode != 0:
                    raise ImportError(f"Failed to install llama-cpp-python: {cpu_install.stderr}")
            from llama_cpp import Llama
            _llama_cpp_module = Llama
            MCPLogger.log(TOOL_LOG_NAME, "llama-cpp-python installed successfully")
    return _llama_cpp_module


def get_gguf_models() -> Dict[str, str]:
    """Get available GGUF models from HuggingFace cache.
    
    Returns dict of alias -> full path.
    """
    cache_dir = get_huggingface_cache_dir()
    models = {}
    
    # Known GGUF model patterns
    gguf_patterns = [
        ('instruct-q6k', 'models--bartowski--Qwen_Qwen3-VL-8B-Instruct-GGUF', 'Qwen_Qwen3-VL-8B-Instruct-Q6_K.gguf'),
        ('thinking-q6k', 'models--bartowski--Qwen_Qwen3-VL-8B-Thinking-GGUF', 'Qwen_Qwen3-VL-8B-Thinking-Q6_K.gguf'),
        ('embed-8b-gguf', 'models--dam2452--Qwen3-VL-Embedding-8B-GGUF', 'Qwen3-VL-Embedding-8B-Q8_0.gguf'),
        ('rerank-8b-gguf', 'models--tooktang--Qwen3-VL-Reranker-8B-Q8_0-GGUF', 'Qwen3-VL-Reranker-8B-Q8_0.gguf'),
    ]
    
    for alias, model_folder, filename in gguf_patterns:
        model_dir = cache_dir / model_folder
        if model_dir.exists():
            snapshots_dir = model_dir / 'snapshots'
            if snapshots_dir.exists():
                for snapshot in snapshots_dir.iterdir():
                    gguf_path = snapshot / filename
                    if gguf_path.exists():
                        models[alias] = str(gguf_path)
                        break
    
    # Also scan for any .gguf files in the cache
    for item in cache_dir.iterdir():
        if item.is_dir() and item.name.startswith('models--'):
            snapshots_dir = item / 'snapshots'
            if snapshots_dir.exists():
                for snapshot in snapshots_dir.iterdir():
                    for gguf_file in snapshot.glob('*.gguf'):
                        # Create alias from filename
                        alias = gguf_file.stem.lower().replace('_', '-')
                        if alias not in models:
                            models[alias] = str(gguf_file)
    
    return models


def resolve_gguf_path(model_arg: str) -> str:
    """Resolve model argument to full GGUF path.
    
    Args:
        model_arg: Model alias or full path to .gguf file
    
    Returns:
        Full path to the GGUF file
    """
    # If it's already a path, return it
    if os.path.exists(model_arg):
        return model_arg
    
    # Check if it's an alias
    models = get_gguf_models()
    if model_arg in models:
        return models[model_arg]
    
    raise ValueError(f"GGUF model not found: {model_arg}. Available: {list(models.keys())}")


def _detect_mps_availability(torch) -> bool:
    """Apple Silicon Metal Performance Shaders availability."""
    try:
        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


def load_model(model_name: str, device: str = "auto") -> Tuple[Any, Any, str]:
    """Load a model and tokenizer, with caching, LRU eviction and a load lock.
    
    Device auto-selection order: cuda > mps (Apple Silicon) > cpu.
    Returns: (model, tokenizer, device_used)
    """
    global _loaded_models
    
    cache_key = f"{model_name}_{device}"
    with _model_load_lock:
        if cache_key in _loaded_models:
            cached = _loaded_models[cache_key]
            cached["last_used_at"] = time.time()
            return cached["model"], cached["tokenizer"], cached["device"]
        
        torch = ensure_torch()
        transformers = ensure_transformers()
        
        # Determine device
        if device == "auto":
            if torch.cuda.is_available():
                actual_device = "cuda"
            elif _detect_mps_availability(torch):
                actual_device = "mps"
            else:
                actual_device = "cpu"
        else:
            actual_device = device
        
        MCPLogger.log(TOOL_LOG_NAME, f"Loading model {model_name} on {actual_device}...")
        
        _evict_least_recently_used_models_locked()

        # Load tokenizer
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
        
        # Load model with appropriate settings
        model = transformers.AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype="auto",
            device_map="auto" if actual_device == "cuda" else None
        )
        
        if actual_device in ("cpu", "mps"):
            model = model.to(actual_device)
        
        # Cache the loaded model
        _loaded_models[cache_key] = {
            "model": model,
            "tokenizer": tokenizer,
            "device": actual_device,
            "last_used_at": time.time()
        }
        
        MCPLogger.log(TOOL_LOG_NAME, f"Model {model_name} loaded successfully on {actual_device}")
        
        return model, tokenizer, actual_device


def get_huggingface_cache_dir():
    """Get the HuggingFace cache directory (platform-specific)."""
    import os
    from pathlib import Path
    
    # Check environment variable first
    hf_home = os.environ.get('HF_HOME')
    if hf_home:
        return Path(hf_home) / 'hub'
    
    # Platform-specific defaults
    home = Path.home()
    cache_dir = home / '.cache' / 'huggingface' / 'hub'
    
    return cache_dir


def handle_hardware_info(params: Dict) -> Dict:
    """Get hardware capabilities (CUDA / Apple Silicon MPS / CPU)."""
    try:
        torch = ensure_torch()
        
        info = {
            "torch_version": torch.__version__,
            "cuda_available": False,
            "mps_available": False,
            "device": "cpu",
            "gpu_name": None,
            "gpu_memory_gb": None,
            "recommended_device": "cpu"
        }
        
        if hasattr(torch, 'cuda') and torch.cuda.is_available():
            info["cuda_available"] = True
            info["device"] = "cuda"
            info["recommended_device"] = "cuda"
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["gpu_memory_gb"] = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        elif _detect_mps_availability(torch):
            # Apple Silicon GPU (Metal) - used automatically by device:"auto"
            info["mps_available"] = True
            info["device"] = "mps"
            info["recommended_device"] = "mps"
            info["gpu_name"] = "Apple Silicon (MPS)"
        
        return {
            "content": [{"type": "text", "text": json.dumps(info)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error detecting hardware: {e}", with_readme=False)


def handle_list_installed_models(params: Dict) -> Dict:
    """Scan HuggingFace cache for installed models."""
    try:
        cache_dir = get_huggingface_cache_dir()
        
        if not cache_dir.exists():
            return {
                "content": [{"type": "text", "text": json.dumps([])}],
                "isError": False
            }
        
        models = []
        
        # Scan for model directories (format: models--organization--model-name)
        for item in cache_dir.iterdir():
            if item.is_dir() and item.name.startswith('models--'):
                parts = item.name.split('--')
                if len(parts) >= 3:
                    organization = parts[1]
                    model_name = '--'.join(parts[2:])
                    model_id = f"{organization}/{model_name}"
                    
                    # Get size info
                    size_bytes = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                    size_gb = size_bytes / (1024**3)
                    
                    models.append({
                        "model_id": model_id,
                        "cache_path": str(item),
                        "size_gb": round(size_gb, 2)
                    })
        
        return {
            "content": [{"type": "text", "text": json.dumps(models)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error scanning for models: {e}", with_readme=False)


# ============================================================================
# OpenRouter Operations (from openrouter.py)
# ============================================================================

def get_openrouter_db_path() -> str:
    """Get the path to the OpenRouter database in the user data directory."""
    user_data_path = get_user_data_directory()
    return str(user_data_path / "openrouter.db")


def handle_get_credits(params: Dict) -> Dict:
    """Get account credit information from OpenRouter."""
    import http.client
    
    api_key = get_api_key(Provider.OPENROUTER, params.get('api_key'))
    if not api_key:
        return create_error_response("OpenRouter API key not configured", with_readme=False)
    
    conn = None
    try:
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=_METADATA_HTTP_TIMEOUT_SECONDS)
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        conn.request("GET", "/api/v1/credits", headers=headers)
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
        return create_error_response(f"Error getting credits: {e}", with_readme=False)
    finally:
        if conn:
            conn.close()


def handle_get_generation(params: Dict) -> Dict:
    """Get details of a specific generation from OpenRouter.
    
    This is useful for retrieving generation metadata after a chat completion,
    including cost, tokens, and timing information.
    
    Args:
        params: Dict with 'generation_id' (required)
        
    Returns:
        Generation details from OpenRouter API
    """
    import http.client
    
    generation_id = params.get("generation_id")
    if not generation_id:
        return create_error_response("generation_id is required", with_readme=True)

    api_key = get_api_key(Provider.OPENROUTER, params.get('api_key'), interactive=True)
    if not api_key:
        return create_error_response("OpenRouter API key not configured", with_readme=False)

    conn = None
    try:
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=_METADATA_HTTP_TIMEOUT_SECONDS)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://aurafriday.com",
            "X-Title": "AuraFriday LLM"
        }
        
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
        return create_error_response(f"Error getting generation: {str(e)}", with_readme=False)
    finally:
        if conn:
            conn.close()


def handle_model_info(params: Dict) -> Dict:
    """Get detailed information about a specific model."""
    model_name = params.get('model')
    if not model_name:
        return create_error_response("model parameter is required", with_readme=False)
    
    provider_str = params.get('provider', 'openrouter')
    
    if provider_str == 'local':
        # Check if model exists in cache
        try:
            cache_dir = get_huggingface_cache_dir()
            model_dir_name = f"models--{model_name.replace('/', '--')}"
            model_path = cache_dir / model_dir_name
            
            if model_path.exists():
                size_bytes = sum(f.stat().st_size for f in model_path.rglob('*') if f.is_file())
                size_gb = size_bytes / (1024**3)
                
                # Try to read config.json for more info
                config_info = {}
                config_path = None
                for snapshot_dir in (model_path / "snapshots").iterdir() if (model_path / "snapshots").exists() else []:
                    config_file = snapshot_dir / "config.json"
                    if config_file.exists():
                        config_path = config_file
                        break
                
                if config_path and config_path.exists():
                    try:
                        with open(config_path, 'r') as f:
                            config_info = json.load(f)
                    except:
                        pass
                
                info = {
                    "model_id": model_name,
                    "provider": "local",
                    "cache_path": str(model_path),
                    "size_gb": round(size_gb, 2),
                    "installed": True,
                    "architecture": config_info.get("architectures", ["unknown"])[0] if config_info.get("architectures") else "unknown",
                    "vocab_size": config_info.get("vocab_size"),
                    "hidden_size": config_info.get("hidden_size"),
                    "num_layers": config_info.get("num_hidden_layers"),
                    "num_attention_heads": config_info.get("num_attention_heads")
                }
                
                return {
                    "content": [{"type": "text", "text": json.dumps(info, indent=2)}],
                    "isError": False
                }
            else:
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "model_id": model_name,
                        "provider": "local",
                        "installed": False,
                        "message": "Model not found in local cache. It will be downloaded on first use."
                    }, indent=2)}],
                    "isError": False
                }
        except Exception as e:
            return create_error_response(f"Error getting model info: {e}", with_readme=False)
    
    elif provider_str == 'openrouter':
        # Query from database (exclude 'embedding' BLOB column which isn't JSON serializable)
        try:
            from .sqlite import sqlite
            
            db_path = get_openrouter_db_path()
            
            # Get all columns except 'embedding' (which is a BLOB)
            result = sqlite(
                sql="""SELECT id, last_updated, canonical_slug, hugging_face_id, name, created, 
                       description, context_length, architecture_modality, architecture_input_modalities,
                       architecture_output_modalities, architecture_tokenizer, architecture_instruct_type,
                       pricing_prompt, pricing_completion, pricing_request, pricing_image, pricing_web_search,
                       pricing_internal_reasoning, pricing_input_cache_read, pricing_input_cache_write,
                       top_provider_context_length, top_provider_max_completion_tokens, top_provider_is_moderated,
                       per_request_limits, supported_parameters, default_parameters_temperature,
                       default_parameters_top_p, default_parameters_frequency_penalty, expiration_date,
                       pricing_audio, default_parameters
                       FROM models WHERE id = :model_id""",
                database=db_path,
                bindings={"model_id": model_name}
            )
            
            if result.get("operation_was_successful"):
                rows = result.get("data_rows_from_result_set", [])
                if rows:
                    return {
                        "content": [{"type": "text", "text": json.dumps(rows[0], indent=2)}],
                        "isError": False
                    }
                else:
                    return create_error_response(f"Model '{model_name}' not found in OpenRouter database", with_readme=False)
            else:
                return create_error_response(f"Error querying model: {result.get('error_message_if_operation_failed')}", with_readme=False)
        except Exception as e:
            return create_error_response(f"Error getting model info: {e}", with_readme=False)
    
    else:
        return create_error_response(f"model_info not supported for provider: {provider_str}", with_readme=False)


def handle_search_models(params: Dict) -> Dict:
    """Search OpenRouter models using semantic similarity or SQL.
    
    This uses your local SQLite database with vector embeddings for semantic search.
    The database is automatically refreshed if stale (>24 hours old).
    """
    try:
        import re
        from .sqlite import sqlite
        
        sql = params.get("sql")
        bindings = params.get("bindings")
        # int() coercion + clamp: max_results is interpolated into SQL below
        try:
            max_results = int(params.get("max_results", 32))
        except (TypeError, ValueError):
            return create_error_response("max_results must be an integer", with_readme=False)
        max_results = max(1, min(1000, max_results))
        
        # Check if database needs refresh before searching. A failed freshness
        # check is a warning (matching list_models), not a hard failure - the
        # refresh attempt below decides whether we can serve the request.
        needs_refresh, error = check_models_database_freshness()
        if error:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Database freshness check failed: {error}")
        if needs_refresh:
            MCPLogger.log(TOOL_LOG_NAME, "search_models is refreshing stale DB first")
            success, refresh_error = refresh_models_database()
            if not success:
                return create_error_response(f"Failed to refresh models database: {refresh_error}", with_readme=False)
        
        db_path = get_openrouter_db_path()
        
        if sql is None:
            # Get all column names except 'embedding'
            columns_result = sqlite(
                sql="SELECT name FROM pragma_table_info('models') WHERE name != 'embedding'",
                database=db_path
            )
            if not columns_result.get("operation_was_successful"):
                return create_error_response(f"Failed to get column names: {columns_result.get('error_message_if_operation_failed')}", with_readme=False)
            
            columns = [row["name"] for row in columns_result.get("data_rows_from_result_set", [])]
            
            # Default semantic search on all columns except embedding
            sql = f"""
                SELECT 
                    {', '.join(columns)},
                    vec_distance_cosine(embedding, vec_f32(:query_vec)) as similarity
                FROM models
                ORDER BY similarity
            """
            if max_results:
                sql += f"\nLIMIT {max_results}"
            
            # For semantic search, we need bindings
            if not bindings or not isinstance(bindings, dict) or "_embedding_text" not in bindings.get("query_vec", {}):
                return create_error_response("Must provide text for semantic search in bindings['query_vec']['_embedding_text']", with_readme=True)
        else:
            # Custom SQL - add LIMIT if not already present as an SQL keyword
            # (word-boundary match so text like 'unlimited' doesn't false-positive)
            if max_results and not re.search(r'\bLIMIT\b', sql, re.IGNORECASE):
                sql = sql.rstrip().rstrip(';') + f"\nLIMIT {max_results}"
            
            # If using vec_f32(:query_vec) in custom SQL, we need bindings
            if "vec_f32(:query_vec)" in sql and (not bindings or not isinstance(bindings, dict) or "_embedding_text" not in bindings.get("query_vec", {})):
                return create_error_response("Must provide text for semantic search in bindings['query_vec']['_embedding_text']", with_readme=True)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Executing search query: {sql}")
        MCPLogger.log(TOOL_LOG_NAME, f"With bindings: {str(bindings)[:300]}")
        
        result = sqlite(
            sql=sql,
            database=db_path,
            bindings=bindings
        )
        
        if result.get("operation_was_successful"):
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
        else:
            return create_error_response(result.get("error_message_if_operation_failed", "Unknown error"), with_readme=False)
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in search_models: {e}\n{traceback.format_exc()}")
        return create_error_response(f"Error in search_models: {e}", with_readme=False)


def _ping_single_provider(provider_id: str, params: Dict) -> Dict:
    """Health-check one provider without a full chat round-trip.

    Returns {"status": "ok"|"unavailable"|"no_api_key"|"error", "detail": ...}.
    """
    import http.client
    try:
        if provider_id == 'ollama':
            try:
                ollama = ensure_ollama()
                ollama_host = params.get('ollama_host', 'http://localhost:11434')
                client = ollama.Client(host=ollama_host) if ollama_host != 'http://localhost:11434' else ollama
                client.list()
                return {"status": "ok", "detail": f"Ollama server reachable at {ollama_host}"}
            except Exception as e:
                return {"status": "unavailable", "detail": str(e)[:200]}
        elif provider_id in ('mlx', 'custom'):
            from urllib.parse import urlparse
            if provider_id == 'mlx':
                target_url = params.get('mlx_host', 'http://localhost:8081')
            else:
                target_url = params.get('base_url', '')
                if not target_url:
                    return {"status": "error", "detail": "base_url required to ping the custom provider"}
            if '://' not in target_url:
                target_url = f"http://{target_url}"
            parsed = urlparse(target_url)
            host = parsed.netloc or parsed.path
            conn = None
            try:
                conn = http.client.HTTPSConnection(host, timeout=10) if parsed.scheme == 'https' else http.client.HTTPConnection(host, timeout=10)
                conn.request("GET", "/v1/models")
                response = conn.getresponse()
                response.read()
                if response.status == 200:
                    return {"status": "ok", "detail": f"OpenAI-compatible server reachable at {target_url}"}
                return {"status": "unavailable", "detail": f"HTTP {response.status} from {target_url}"}
            finally:
                if conn:
                    conn.close()
        elif provider_id in ('openrouter', 'openai', 'anthropic'):
            provider_enum = Provider(provider_id)
            api_key = get_api_key(provider_enum, params.get('api_key'), interactive=False)
            if not api_key:
                return {"status": "no_api_key", "detail": "No API key configured"}
            host_and_path_by_provider = {
                'openrouter': ("openrouter.ai", "/api/v1/credits", {"Authorization": f"Bearer {api_key}"}),
                'openai': ("api.openai.com", "/v1/models", {"Authorization": f"Bearer {api_key}"}),
                'anthropic': ("api.anthropic.com", "/v1/models", {"x-api-key": api_key, "anthropic-version": "2023-06-01"}),
            }
            host, path, request_headers = host_and_path_by_provider[provider_id]
            conn = None
            try:
                conn = http.client.HTTPSConnection(host, timeout=10)
                conn.request("GET", path, headers=request_headers)
                response = conn.getresponse()
                response.read()
                if response.status == 200:
                    return {"status": "ok", "detail": "API key valid, provider reachable"}
                if response.status in (401, 403):
                    return {"status": "error", "detail": f"API key rejected (HTTP {response.status})"}
                return {"status": "unavailable", "detail": f"HTTP {response.status}"}
            finally:
                if conn:
                    conn.close()
        elif provider_id == 'cursor_agent':
            path = _detect_cursor_agent_cli_path()
            return {"status": "ok", "detail": path} if path else {"status": "unavailable", "detail": "CLI not found"}
        elif provider_id == 'claude_code':
            path = _detect_claude_code_cli_path()
            return {"status": "ok", "detail": path} if path else {"status": "unavailable", "detail": "CLI not found"}
        elif provider_id == 'gemini_cli':
            path = _detect_gemini_cli_path()
            return {"status": "ok", "detail": path} if path else {"status": "unavailable", "detail": "CLI not found"}
        elif provider_id == 'codex_cli':
            available = _check_codex_mcp_bridge_is_available()
            return {"status": "ok", "detail": "codex MCP bridge registered"} if available else {"status": "unavailable", "detail": "codex MCP bridge not registered"}
        elif provider_id == 'local':
            try:
                ensure_torch()
                return {"status": "ok", "detail": "torch importable"}
            except ImportError as e:
                return {"status": "unavailable", "detail": str(e)[:200]}
        elif provider_id == 'llama_cpp':
            gguf_model_aliases = list(get_gguf_models().keys())
            return {"status": "ok", "detail": f"{len(gguf_model_aliases)} GGUF model(s) available"}
        else:
            return {"status": "error", "detail": f"Unknown provider: {provider_id}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}


def handle_ping(params: Dict) -> Dict:
    """Health-check operation: verify key validity / server reachability per provider
    so UIs can show status without a full chat round-trip."""
    provider_str = params.get('provider')
    if provider_str:
        providers_to_check = [provider_str]
    else:
        providers_to_check = [p.value for p in Provider]

    ping_results = {}
    for provider_id in providers_to_check:
        ping_results[provider_id] = _ping_single_provider(provider_id, params)

    return {
        "content": [{"type": "text", "text": json.dumps({"ping": ping_results}, indent=2)}],
        "isError": False
    }


def handle_preload_model(params: Dict) -> Dict:
    """Load a local/llama_cpp model into the cache so first-chat latency is low."""
    provider_str = params.get('provider', 'local')
    model_arg = params.get('model') or params.get('gguf_path')
    if not model_arg:
        return create_error_response("model (or gguf_path) is required for preload_model", with_readme=False)

    load_started_at = time.time()
    try:
        if provider_str == 'local':
            device = params.get('device', 'auto')
            _model, _tokenizer, device_used = load_model(model_arg, device)
            load_seconds = round(time.time() - load_started_at, 2)
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "loaded", "provider": "local", "model": model_arg,
                    "device": device_used, "load_seconds": load_seconds
                })}],
                "isError": False
            }
        elif provider_str == 'llama_cpp':
            Llama = ensure_llama_cpp()
            model_path = resolve_gguf_path(model_arg)
            gpu_layers = params.get('gpu_layers', -1)
            context_length = params.get('context_length', 8192)
            cache_key = f"llama_cpp_{model_path}_{gpu_layers}_{context_length}"
            with _model_load_lock:
                if cache_key not in _loaded_models:
                    _evict_least_recently_used_models_locked()
                    llm_instance = Llama(model_path=model_path, n_ctx=context_length,
                                         n_gpu_layers=gpu_layers, verbose=False)
                    _loaded_models[cache_key] = {"llm": llm_instance, "last_used_at": time.time()}
                else:
                    _loaded_models[cache_key]["last_used_at"] = time.time()
            load_seconds = round(time.time() - load_started_at, 2)
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "loaded", "provider": "llama_cpp", "model": model_arg,
                    "path": model_path, "load_seconds": load_seconds
                })}],
                "isError": False
            }
        else:
            return create_error_response(f"preload_model supports providers 'local' and 'llama_cpp', not '{provider_str}'", with_readme=False)
    except Exception as e:
        return create_error_response(f"preload_model failed: {e}", with_readme=False)


def handle_unload_model(params: Dict) -> Dict:
    """Evict cached model(s) and reclaim memory. Omit 'model' to unload everything."""
    model_arg = params.get('model') or params.get('gguf_path')
    evicted_cache_keys = []
    with _model_load_lock:
        if model_arg:
            matching_keys = [key for key in _loaded_models if model_arg in key]
        else:
            matching_keys = list(_loaded_models.keys())
        for cache_key in matching_keys:
            _loaded_models.pop(cache_key, None)
            evicted_cache_keys.append(cache_key)
    if evicted_cache_keys:
        import gc
        gc.collect()
        try:
            if _torch is not None and _torch.cuda.is_available():
                _torch.cuda.empty_cache()
        except Exception:
            pass
    return {
        "content": [{"type": "text", "text": json.dumps({
            "status": "unloaded" if evicted_cache_keys else "nothing_to_unload",
            "evicted": evicted_cache_keys,
            "still_cached": len(_loaded_models)
        })}],
        "isError": False
    }


def handle_llm2(input_param: Dict) -> Dict:
    """Main entry point for the unified LLM tool."""
    try:
        # Extract handler_info early
        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {}
        
        # Collapse single-input wrapper
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]
        
        # Handle readme operation (no token required)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        
        # Validate input
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format", with_readme=True)
        
        # Validate token
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)
        
        # Get operation
        operation = input_param.get("operation")
        
        # Route to handler
        if operation == "chat":
            return handle_chat(input_param, handler_info)
        elif operation == "list_providers":
            return handle_list_providers(input_param)
        elif operation == "list_models":
            return handle_list_models(input_param)
        elif operation == "stream_status":
            return handle_stream_status(input_param, handler_info)
        elif operation == "cancel_stream":
            return handle_cancel_stream(input_param, handler_info)
        elif operation == "hardware_info":
            return handle_hardware_info(input_param)
        elif operation == "list_installed_models":
            return handle_list_installed_models(input_param)
        elif operation == "get_credits":
            return handle_get_credits(input_param)
        elif operation == "get_generation":
            return handle_get_generation(input_param)
        elif operation == "search_models":
            return handle_search_models(input_param)
        elif operation == "model_info":
            return handle_model_info(input_param)
        elif operation == "ping":
            return handle_ping(input_param)
        elif operation == "preload_model":
            return handle_preload_model(input_param)
        elif operation == "unload_model":
            return handle_unload_model(input_param)
        else:
            return create_error_response(f"Unknown operation: {operation}", with_readme=True)
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error: {e}\n{traceback.format_exc()}")
        return create_error_response(f"Error: {e}", with_readme=False)


# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_llm2
}

