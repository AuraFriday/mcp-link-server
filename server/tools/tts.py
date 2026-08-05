"""
File: ragtag/tools/tts.py
Project: Aura Friday MCP-Link Server
Component: Text-to-Speech Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for text-to-speech synthesis with support for multiple providers.
Currently supports Google Cloud (default), ElevenLabs, and Deepgram.

Dependencies are lazy-loaded on first use. Missing packages are reported with
manual install instructions (no runtime auto-install).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "D7МƘ7h𝟙ƶŪΗΤ𝕌ꓝΒÞΕbfƼᴅ𝟚ʈԝԛꓔԝոӠƤXJꓝⲞνɋᏎpΟ𝕌ŪꓔһWBeꓣ7KɋⲦЈ𐓒UģОꓮⲦᏟɋꓦⅼОƵΜⅠꓗΝΜƵƴǝtH𝟧i0Ѕew𝙰yƋᎪ𝟚𝟤ƴбⲔ𝟧zЗꓧG6ⲢРКFϨТbᴛΜ𐐕ƌսOȣР"
"signdate": "2026-07-23T02:39:20.634Z",
"""

import os
import sys
import shutil  # used to detect the external mpv player before ElevenLabs playback
import json
import threading
import time
import wave
import queue
from datetime import datetime
from typing import Dict, List, Optional, Union, BinaryIO, Tuple, Any
# BytesIO import removed: its only consumer was the deleted dead AudioUtils class
from pathlib import Path
from abc import ABC, abstractmethod

from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory, get_config_manager

# Constants
TOOL_LOG_NAME = "TTS"

# ============================================================================
# Lazy-loaded modules - will be loaded on first use
# ============================================================================
elevenlabs_module = None
elevenlabs_play = None
elevenlabs_stream = None
elevenlabs_VoiceSettings = None
elevenlabs_ElevenLabs = None

deepgram_DeepgramClient = None

google_texttospeech = None

sounddevice_module = None
numpy_module = None


# ============================================================================
# Lazy Loading Functions
#
# No runtime pip install here (security): the shipped Python runtime must not
# be mutated by an AI-triggered tool call. A missing package produces a clear
# error with the manual install command instead.
# ============================================================================

def _ensure_audio_dependencies() -> Tuple[bool, Optional[str]]:
    """Ensure audio playback dependencies (sounddevice, numpy) are available.
    
    Returns:
        Tuple of (success, error_message)
    """
    global sounddevice_module, numpy_module
    
    missing_packages = []
    
    # Check sounddevice
    if sounddevice_module is None:
        try:
            import sounddevice as sd
            sounddevice_module = sd
            MCPLogger.log(TOOL_LOG_NAME, "Loaded sounddevice")
        except ImportError:
            missing_packages.append("sounddevice")
    
    # Check numpy
    if numpy_module is None:
        try:
            import numpy as np
            numpy_module = np
            MCPLogger.log(TOOL_LOG_NAME, "Loaded numpy")
        except ImportError:
            missing_packages.append("numpy")
    
    # If all loaded, we're good
    if not missing_packages:
        return True, None
    
    install_cmd = f"{sys.executable} -m pip install {' '.join(missing_packages)}"
    return False, f"Audio dependencies not available: {', '.join(missing_packages)}. Install manually with:\n{install_cmd}"


def _ensure_elevenlabs() -> Tuple[bool, Optional[str]]:
    """Ensure ElevenLabs SDK is available.
    
    Returns:
        Tuple of (success, error_message)
    """
    global elevenlabs_module, elevenlabs_play, elevenlabs_stream, elevenlabs_VoiceSettings
    global elevenlabs_ElevenLabs
    
    if elevenlabs_module is not None:
        return True, None
    
    try:
        import elevenlabs
        from elevenlabs import play, stream, VoiceSettings
        from elevenlabs.client import ElevenLabs
        
        elevenlabs_module = elevenlabs
        elevenlabs_play = play
        elevenlabs_stream = stream
        elevenlabs_VoiceSettings = VoiceSettings
        elevenlabs_ElevenLabs = ElevenLabs
        
        MCPLogger.log(TOOL_LOG_NAME, "Loaded ElevenLabs SDK")
        return True, None
    except ImportError:
        install_cmd = f"{sys.executable} -m pip install elevenlabs"
        return False, f"ElevenLabs SDK not available. Install manually with:\n{install_cmd}"


def _ensure_deepgram() -> Tuple[bool, Optional[str]]:
    """Ensure Deepgram SDK is available.
    
    Returns:
        Tuple of (success, error_message)
    """
    global deepgram_DeepgramClient
    
    if deepgram_DeepgramClient is not None:
        return True, None
    
    try:
        # Deepgram SDK - just need DeepgramClient
        from deepgram import DeepgramClient
        
        deepgram_DeepgramClient = DeepgramClient
        
        MCPLogger.log(TOOL_LOG_NAME, "Loaded Deepgram SDK")
        return True, None
    except ImportError:
        install_cmd = f"{sys.executable} -m pip install deepgram-sdk"
        return False, f"Deepgram SDK not available. Install manually with:\n{install_cmd}"


def _ensure_google_cloud_tts() -> Tuple[bool, Optional[str]]:
    """Ensure Google Cloud TTS SDK is available.
    
    Returns:
        Tuple of (success, error_message)
    """
    global google_texttospeech
    
    if google_texttospeech is not None:
        return True, None
    
    try:
        from google.cloud import texttospeech
        google_texttospeech = texttospeech
        MCPLogger.log(TOOL_LOG_NAME, "Loaded Google Cloud TTS SDK")
        return True, None
    except ImportError:
        install_cmd = f"{sys.executable} -m pip install google-cloud-texttospeech"
        return False, f"Google Cloud TTS SDK not available. Install manually with:\n{install_cmd}"


# ============================================================================
# Tool identity - assigned before the API-key helpers below, which reference
# TOOL_UNLOCK_TOKEN / TOOL_NAME_SUFFIX (keeps definition-before-use ordering)
# ============================================================================

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"tts{TOOL_NAME_SUFFIX}"


# ============================================================================
# API Key Management Functions
# ============================================================================

def get_api_key(provider_name: str, explicit_key: Optional[str] = None, interactive: bool = True) -> Optional[str]:
    """Get API key for a TTS provider from config, explicit parameter, or interactive prompt.
    
    Args:
        provider_name: The provider name ('elevenlabs', 'deepgram')
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
            'elevenlabs': 'ELEVENLABS_API_KEY',
            'deepgram': 'DEEPGRAM_API_KEY',
        }
        
        key_name = key_map.get(provider_name)
        if key_name:
            key = api_keys.get(key_name)
            if key and key != 'placeholder-key':
                return key
        
        # Key not found - try interactive prompt if enabled
        if interactive and key_name:
            MCPLogger.log(TOOL_LOG_NAME, f"{key_name} not set or is placeholder. Interactive mode: {interactive}")
            prompted_key = _prompt_user_for_api_key(provider_name, key_name)
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


def _prompt_user_for_api_key(provider_name: str, key_name: str) -> Optional[str]:
    """Prompt the user for an API key using the user tool.
    
    The API key is saved directly to the config file by the user tool's HTML form
    via the web server's /api/settings endpoint. This function just needs to reload
    the config after the dialog closes to get the saved key.
    
    Args:
        provider_name: The provider name (e.g., 'elevenlabs')
        key_name: The config key name (e.g., 'ELEVENLABS_API_KEY')
        
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
            'elevenlabs': ("ElevenLabs", "https://elevenlabs.io/app/settings/api-keys"),
            'deepgram': ("Deepgram", "https://console.deepgram.com/api-keys"),
        }
        
        service_name, service_url = service_info.get(provider_name, (provider_name, ""))
        
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
            # The user tool registers as f"user{TOOL_SUFFIX}"; use the suffixed name
            # (same pattern as agent.py) so this call works when a suffix is configured
            tool_name=f"user{TOOL_NAME_SUFFIX}",
            parameters={
                "input": {
                    "operation": "collect_api_key",
                    "service_name": service_name,
                    "service_url": service_url,
                    "tool_unlock_token": inter_tool_token
                }
            },
            calling_tool="tts"
        )
        
        # Check if the call was successful
        if result.get("isError"):
            MCPLogger.log(TOOL_LOG_NAME, f"User tool returned error: {result}")
            return None
        
        # Parse and log the response from the user tool (but don't rely on it)
        content = result.get("content", [])
        if content and len(content) > 0:
            try:
                response_data = json.loads(content[0].get("text", "{}"))
                MCPLogger.log(TOOL_LOG_NAME, f"User tool response data (informational only): {response_data}")
            except json.JSONDecodeError as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error parsing user tool response: {e}")
                MCPLogger.log(TOOL_LOG_NAME, f"Raw response text: {content[0].get('text', '')}")
        
        # Regardless of window response, reload config to check if key was saved
        # The HTML form saves directly via /api/settings endpoint, so we just need to reload
        MCPLogger.log(TOOL_LOG_NAME, "Popup closed, reloading config to check for saved API key")
        
        # Small delay to ensure file write has completed (race condition fix)
        time.sleep(0.2)
        
        api_key = get_api_key(provider_name, interactive=False)  # Non-interactive reload from config
        if api_key:
            MCPLogger.log(TOOL_LOG_NAME, "Successfully retrieved saved API key from config")
            return api_key
        else:
            MCPLogger.log(TOOL_LOG_NAME, "No API key found in config after popup closed (user may have cancelled)")
            return None
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error prompting user for API key: {e}")
        return None


# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        # The "description" Key is the only thing that persists in the AI context at all times. Keep this as brief as possible, but, it must include everything an AI needs to know in order to work out if it should use this tool, and needs to clearly tell the AI to use the read me operation to find out how to do that.
        "description": """Convert text to speech using cloud TTS providers (ElevenLabs, Deepgram, Google Cloud).
Audio plays through user's PC speakers. Requires API keys (prompted on first use).
NOTE: if a browser tool (chrome_browser/edge_browser etc.) is currently listed, its tts_speak operation offers FREE TTS without API keys; those tools only appear while the user's browser is running.
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

        # The "readme" key is obtained on demand when an AI decides it needs to use this tool.  It should be verbose and clear with lots of examples so the AI fully understands every feature and how to use it.
        "readme": """Text to Speech synthesis tool supporting multiple cloud providers.

## FREE ALTERNATIVE: browser tool (chrome_browser / edge_browser etc.)
If a browser tool is currently listed (those tools only appear while the user's browser is
running with our extension), its "tts_speak" operation offers FREE text-to-speech via the
browser's built-in TTS engine, no API keys needed. Only use this tts tool when no browser tool
is available or you need premium voice quality from ElevenLabs/Deepgram/Google Cloud.

## Supported Providers (require API keys - you will be prompted on first use)
- ElevenLabs (provider="elevenlabs") - Premium voices with best quality
- Deepgram (provider="deepgram") - Fast, natural-sounding voices
- Google Cloud (provider="google") - Wide variety of voices, requires credentials file

## Operations
- speak: Convert text to speech and play directly to speakers (blocks until playback completes)
- save: Convert text to speech and save to file
- list_voices: Get available voices for a provider
- get_voice_settings: Get customizable settings for a voice_id
- get_models: List available models and their capabilities

## Features
- Multiple output formats: mp3_44100_128 (high quality) and mp3_22050_32 (low latency) for ElevenLabs; "wav" (24kHz LINEAR16) for save with Google/Deepgram. Google and Deepgram speak use a fixed 24kHz linear16 format
- Voice customization (stability, similarity boost, style, speed)
- Blocking audio playback: speak returns {"status": "played"} after audio finishes, or a real error if synthesis/playback failed
- Concurrent speak requests are serialized: there is one system audio output, so playback happens one request at a time
- Optional file saving
- Comprehensive voice and model information for AI decision making
- Missing Python dependencies are reported with manual install instructions (no runtime auto-install)
- Interactive API key collection if keys are missing


### Get Documentation (`operation="readme"`)
- Returns the complete tool documentation
- Must be called before using any other operations
- Provides the tool_unlock_token needed for all subsequent operations

### List Voices (`operation="list_voices"`)
- Get available voices for a provider
- Required: provider parameter to specify which TTS service to use

### Get Voice Settings (`operation="get_voice_settings"`)
- Get customizable settings for a specific voice_id
- Required: voice_id parameter
- Required: provider parameter

### Get Models (`operation="get_models"`)
- List available models and their capabilities
- Required: provider parameter

### Speak Text (`operation="speak"`)
- Convert text to speech and play directly to speakers
- Blocks until playback completes; returns {"status": "played"} on success, or an error if synthesis/playback failed
- Required parameters:
  * text: The text to convert (max 8KB)
  * voice_id: Voice identifier
  * provider: TTS provider to use
- Optional parameters:
  * model_id: Specific model to use
  * output_format: "mp3_44100_128" (high quality) or "mp3_22050_32" (low latency, default). ElevenLabs only - Google and Deepgram speak use a fixed 24kHz linear16 format. "wav" applies to the save operation only
  * voice_settings: Customization parameters (stability, similarity_boost, style, speed)

### Save Audio (`operation="save"`)
- Convert text to speech and save to file
- Required parameters:
  * text: The text to convert (max 8KB)
  * voice_id: Voice identifier
  * save_path: Where to save the audio file
  * provider: TTS provider to use
- Optional parameters:
  * model_id: Specific model to use
  * output_format: "mp3_44100_128" (high quality, default for save), "mp3_22050_32" (low latency), or "wav" (24kHz LINEAR16 WAV; Google/Deepgram only, rejected for ElevenLabs). ElevenLabs saves mp3 only; Google always saves 24kHz LINEAR16 WAV regardless of format; Deepgram saves MP3 unless "wav" is requested
  * voice_settings: Customization parameters

## Authentication
API keys are stored in nativemessaging.json and will be prompted interactively on first use.
- ElevenLabs: ELEVENLABS_API_KEY (get from https://elevenlabs.io/app/settings/api-keys)
- Deepgram: DEEPGRAM_API_KEY (get from https://console.deepgram.com/api-keys)
- Google Cloud: Requires credentials JSON file (set GOOGLE_APPLICATION_CREDENTIALS or use default path)

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details.
The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Procedure for AI:
1. First call readme to get the tool_unlock_token
2. Then call get_models to understand available capabilities
3. Call list_voices to get available voices
4. For chosen voice_id, use get_voice_settings to understand customization options
5. Finally use speak or save with your chosen configuration
6. Include the tool_unlock_token in all subsequent operations

## Example Usage
```python
# First get documentation
{
    "operation": "readme",
    "provider": "google"
}

# List available voices
{
    "operation": "list_voices",
    "provider": "google",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
}

# Get voice settings
{
    "operation": "get_voice_settings",
    "provider": "google",
    "voice_id": "en-US-Standard-A",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
}

# Speak text
{
    "operation": "speak",
    "provider": "google",
    "text": "Hello, world!",
    "voice_id": "en-US-Standard-A",
    "voice_settings": {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "speed": 1.0
    },
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
}
Note: Maximum text length is 8192 bytes (8KB)

# Save to file
{
    "operation": "save",
    "provider": "google",
    "text": "Hello, world!",
    "voice_id": "en-US-Standard-A",
    "save_path": "output.wav",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
}
```""",

        # Actual tool parameters - revealed only after readme call
        "real_parameters": {
            "properties": {
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
                },
                "operation": {
                    "type": "string",
                    "enum": ["speak", "save", "list_voices", "get_voice_settings", "get_models", "readme"],
                    "description": "Operation to perform"
                },
                "provider": {
                    "type": "string",
                    "enum": ["google", "elevenlabs", "deepgram"],
                    "description": "TTS provider to use",
                    "default": "google"
                },
                "text": {
                    "type": "string",
                    "description": "Text to convert to speech (required for speak/save operations)",
                    "maxLength": 8192
                },
                "voice_id": {
                    "type": "string",
                    "description": "Voice identifier (required for speak/save operations)"
                },
                "model_id": {
                    "type": "string",
                    "description": "Model to use (defaults to provider's recommended model)",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["mp3_44100_128", "mp3_22050_32", "wav"],
                    "description": "Audio format. mp3_* honored by ElevenLabs (mp3_44100_128 quality, mp3_22050_32 low latency); 'wav' = 24kHz LINEAR16 WAV for the save operation with Google/Deepgram (rejected for ElevenLabs). Google and Deepgram speak always play fixed 24kHz linear16. Defaults: mp3_22050_32 for speak, mp3_44100_128 for save"
                },
                "voice_settings": {
                    "type": "object",
                    "description": "Voice customization parameters",
                    "properties": {
                        "stability": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Voice stability"
                        },
                        "similarity_boost": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Similarity boost factor"
                        },
                        "style": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Style factor"
                        },
                        "use_speaker_boost": {
                            "type": "boolean",
                            "description": "Whether to use speaker boost"
                        },
                        "speed": {
                            "type": "number",
                            "minimum": 0.1,
                            "maximum": 5.0,
                            "description": "Speaking speed multiplier"
                        }
                    }
                },
                "save_path": {
                    "type": "string",
                    "description": "Path to save audio file (required for save operation)"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "title": "ttsArguments",
            "type": "object"
        }
    }
]


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema (mirrors stt.py)."""
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])

    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]

    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params

    if unexpected_params:
        return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}.", {}

    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}.", {}

    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")

            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}.", {}
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                # bool is a subclass of int in Python; reject it explicitly for integer params
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}.", {}
            elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                # bool is a subclass of int in Python; reject it explicitly for number params
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}.", {}
            elif expected_type == "object" and not isinstance(value, dict):
                # tts-specific addition: voice_settings must be a dict
                return f"Parameter '{param_name}' must be an object, got {type(value).__name__}.", {}

            # Enforce the schema's maxLength on string params (e.g. text: 8192);
            # providers additionally enforce an 8KB utf-8 byte cap at synthesis time
            if expected_type == "string" and "maxLength" in param_schema and len(value) > param_schema["maxLength"]:
                return f"Parameter '{param_name}' exceeds maximum length of {param_schema['maxLength']} characters.", {}

            # Validate declared sub-properties of object params (voice_settings):
            # type and minimum/maximum range checks. Undeclared keys are allowed
            # (JSON-schema default), e.g. Google's speaking_rate/pitch/volume_gain_db
            if expected_type == "object":
                for sub_name, sub_schema in param_schema.get("properties", {}).items():
                    if sub_name not in value:
                        continue
                    sub_value = value[sub_name]
                    sub_type = sub_schema.get("type")
                    if sub_type == "number" and (isinstance(sub_value, bool) or not isinstance(sub_value, (int, float))):
                        # bool is a subclass of int in Python; reject it explicitly for number params
                        return f"Parameter '{param_name}.{sub_name}' must be a number, got {type(sub_value).__name__}.", {}
                    elif sub_type == "boolean" and not isinstance(sub_value, bool):
                        return f"Parameter '{param_name}.{sub_name}' must be a boolean, got {type(sub_value).__name__}.", {}
                    if sub_type == "number":
                        if "minimum" in sub_schema and sub_value < sub_schema["minimum"]:
                            return f"Parameter '{param_name}.{sub_name}' must be >= {sub_schema['minimum']}, got {sub_value}.", {}
                        if "maximum" in sub_schema and sub_value > sub_schema["maximum"]:
                            return f"Parameter '{param_name}.{sub_name}' must be <= {sub_schema['maximum']}, got {sub_value}.", {}

            if "enum" in param_schema:
                allowed_values = param_schema["enum"]
                if value not in allowed_values:
                    return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'.", {}

            validated[param_name] = value
        else:
            default_value = param_schema.get("default")
            if default_value is not None:
                validated[param_name] = default_value

    return None, validated


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    docs = "\n\n" + json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2) if with_readme else ""
    return { "content": [{"type": "text", "text": f"{error_msg}{docs}"}], "isError": True }


class TTSError(Exception):
    """Custom exception for TTS-related errors."""
    pass

class TTSProvider(ABC):
    """Abstract base class for TTS providers."""
    
    def _validate_text_length(self, text: str) -> None:
        """Validate text length is within limits (8KB max)."""
        if len(text.encode('utf-8')) > 8192:
            raise TTSError("Text exceeds maximum length of 8KB")
    
    @abstractmethod
    def list_voices(self) -> Dict:
        """Get list of available voices."""
        pass
        
    @abstractmethod
    def get_voice_settings(self, voice_id: str) -> dict:
        """Get customizable settings for a voice."""
        pass
        
    @abstractmethod
    def get_models(self) -> dict:
        """Get available models."""
        pass
        
    @abstractmethod
    def speak(self, text: str, voice_id: str, model_id: Optional[str] = None,
             output_format: str = "mp3_22050_32",
             voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and play to speakers.

        Uniform contract for ALL providers: blocks until playback has completed
        and raises TTSError on synthesis or playback failure (no silent failures).
        """
        pass
        
    @abstractmethod
    def save(self, text: str, save_path: str, voice_id: str,
            model_id: Optional[str] = None, output_format: str = "mp3_44100_128",
            voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and save to file."""
        pass

class ElevenLabsProvider(TTSProvider):
    """Handler for ElevenLabs TTS provider."""
    
    def __init__(self):
        """Initialize the ElevenLabs provider."""
        MCPLogger.log(TOOL_LOG_NAME, "Initializing ElevenLabs provider")
        
        # Ensure ElevenLabs SDK is installed
        success, error = _ensure_elevenlabs()
        if not success:
            raise TTSError(f"ElevenLabs SDK not available: {error}")
        
        # Get API key (will prompt user if missing)
        self.api_key = get_api_key('elevenlabs')
        if not self.api_key:
            MCPLogger.log(TOOL_LOG_NAME, "ELEVENLABS_API_KEY not available")
            raise TTSError("ElevenLabs API key not found. Please provide your API key when prompted, or set ELEVENLABS_API_KEY in your configuration.")
            
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Creating ElevenLabs client with API key")
            self.client = elevenlabs_ElevenLabs(api_key=self.api_key)
            MCPLogger.log(TOOL_LOG_NAME, "ElevenLabs client initialized successfully")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize ElevenLabs client: {str(e)}")
            raise TTSError(f"Failed to initialize ElevenLabs client: {str(e)}")

    def list_voices(self) -> Dict:
        """Get list of available voices."""
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Calling ElevenLabs API: voices.get_all()")
            response = self.client.voices.get_all()
            MCPLogger.log(TOOL_LOG_NAME, "Successfully got voice list from API")
            result = response.model_dump()
            MCPLogger.log(TOOL_LOG_NAME, f"Serialized voice list to dict with {len(result.get('voices', []))} voices")
            return result
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to fetch voices: {str(e)}")
            raise TTSError(f"Failed to fetch voices: {str(e)}")

    def get_voice_settings(self, voice_id: str) -> dict:
        """Get customizable settings for a voice."""
        try:
            MCPLogger.log(TOOL_LOG_NAME, f"Calling ElevenLabs API: voices.get_settings({voice_id})")
            settings = self.client.voices.get_settings(voice_id)
            MCPLogger.log(TOOL_LOG_NAME, "Successfully got voice settings from API")
            result = settings.model_dump()
            MCPLogger.log(TOOL_LOG_NAME, f"Serialized voice settings to dict: {result}")
            return result
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to fetch voice settings: {str(e)}")
            raise TTSError(f"Failed to fetch voice settings: {str(e)}")

    def get_models(self) -> dict:
        """Get available models for a provider."""
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Calling ElevenLabs API: models.get_all()")
            response = self.client.models.get_all()
            MCPLogger.log(TOOL_LOG_NAME, "Successfully got models from API")
            # Return just the list of models directly
            result = {"models": [model.model_dump() for model in response]}
            MCPLogger.log(TOOL_LOG_NAME, f"Serialized models list to dict with {len(result['models'])} models")
            return result
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to fetch models: {str(e)}")
            raise TTSError(f"Failed to fetch models: {str(e)}")

    def speak(self, text: str, voice_id: str, model_id: Optional[str] = None,
             output_format: str = "mp3_22050_32",
             voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and play to speakers (blocks until playback completes)."""
        try:
            self._validate_text_length(text)
            # elevenlabs_stream() pipes audio through the external `mpv` player; detect a
            # missing player up front (before spending an API call) with an actionable
            # error instead of reporting success while nothing is audible
            if shutil.which("mpv") is None:
                raise TTSError("Cannot play audio: the external `mpv` player (required for ElevenLabs streaming playback) was not found on PATH. Install mpv (https://mpv.io/) or use operation='save' to write the audio to a file instead.")
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            # Create voice settings if provided
            settings = None
            if voice_settings:
                settings = elevenlabs_VoiceSettings(**voice_settings)
            
            # Get audio stream using the updated API (stream instead of convert_as_stream)
            MCPLogger.log(TOOL_LOG_NAME, f"Calling ElevenLabs API: text_to_speech.stream(voice_id={voice_id}, model_id={model_id or 'default'})")
            audio_stream = self.client.text_to_speech.stream(
                text=text,
                voice_id=voice_id,
                model_id=model_id or "eleven_multilingual_v2",
                output_format=output_format,
                voice_settings=settings
            )
            MCPLogger.log(TOOL_LOG_NAME, "Successfully got audio stream from API")
            
            # Play synchronously: speak blocks until playback completes for all
            # providers, so playback errors surface to the caller (no false success)
            MCPLogger.log(TOOL_LOG_NAME, "Starting audio playback")
            elevenlabs_stream(audio_stream)
            MCPLogger.log(TOOL_LOG_NAME, "Audio playback completed")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/play speech: {str(e)}")
            raise TTSError(f"Failed to generate/play speech: {str(e)}")

    def save(self, text: str, save_path: str, voice_id: str,
            model_id: Optional[str] = None, output_format: str = "mp3_44100_128",
            voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and save to file."""
        try:
            self._validate_text_length(text)
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            # Create voice settings if provided
            settings = None
            if voice_settings:
                settings = elevenlabs_VoiceSettings(**voice_settings)
            
            # Generate audio using the updated API (stream instead of convert_as_stream)
            MCPLogger.log(TOOL_LOG_NAME, f"Calling ElevenLabs API: text_to_speech.stream(voice_id={voice_id}, model_id={model_id or 'default'})")
            audio_stream = self.client.text_to_speech.stream(
                text=text,
                voice_id=voice_id,
                model_id=model_id or "eleven_multilingual_v2",
                output_format=output_format,
                voice_settings=settings
            )
            MCPLogger.log(TOOL_LOG_NAME, "Successfully got audio stream from API")
            
            # Save to file
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read the entire stream into memory
            audio_data = b''
            for chunk in audio_stream:
                audio_data += chunk
            
            with open(save_path, 'wb') as f:
                f.write(audio_data)
                
            MCPLogger.log(TOOL_LOG_NAME, f"Saved audio to {save_path}")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/save speech: {str(e)}")
            raise TTSError(f"Failed to generate/save speech: {str(e)}")

class DeepgramProvider(TTSProvider):
    """Handler for Deepgram TTS provider using REST API (SDK v5)."""
    
    # Hardcoded list of available voices - includes both Aura and Aura-2 models
    VOICES = [
        # Aura-2 voices (newer, higher quality)
        {"name": "Thalia (Aura-2)", "id": "aura-2-thalia-en", "gender": "female", "accent": "en-US", "model": "aura-2"},
        {"name": "Andromeda (Aura-2)", "id": "aura-2-andromeda-en", "gender": "female", "accent": "en-US", "model": "aura-2"},
        {"name": "Arcas (Aura-2)", "id": "aura-2-arcas-en", "gender": "male", "accent": "en-US", "model": "aura-2"},
        {"name": "Asteria (Aura-2)", "id": "aura-2-asteria-en", "gender": "female", "accent": "en-US", "model": "aura-2"},
        # Original Aura voices
        {"name": "Asteria", "id": "aura-asteria-en", "gender": "female", "accent": "en-US", "model": "aura"},
        {"name": "Luna", "id": "aura-luna-en", "gender": "female", "accent": "en-US", "model": "aura"},
        {"name": "Stella", "id": "aura-stella-en", "gender": "female", "accent": "en-US", "model": "aura"},
        {"name": "Athena", "id": "aura-athena-en", "gender": "female", "accent": "en-UK", "model": "aura"},
        {"name": "Hera", "id": "aura-hera-en", "gender": "female", "accent": "en-US", "model": "aura"},
        {"name": "Orion", "id": "aura-orion-en", "gender": "male", "accent": "en-US", "model": "aura"},
        {"name": "Arcas", "id": "aura-arcas-en", "gender": "male", "accent": "en-US", "model": "aura"},
        {"name": "Perseus", "id": "aura-perseus-en", "gender": "male", "accent": "en-US", "model": "aura"},
        {"name": "Angus", "id": "aura-angus-en", "gender": "male", "accent": "en-IE", "model": "aura"},
        {"name": "Orpheus", "id": "aura-orpheus-en", "gender": "male", "accent": "en-US", "model": "aura"},
        {"name": "Helios", "id": "aura-helios-en", "gender": "male", "accent": "en-UK", "model": "aura"},
        {"name": "Zeus", "id": "aura-zeus-en", "gender": "male", "accent": "en-US", "model": "aura"}
    ]
    
    def __init__(self):
        """Initialize the Deepgram provider."""
        MCPLogger.log(TOOL_LOG_NAME, "Initializing Deepgram provider")
        
        # Ensure Deepgram SDK is installed
        success, error = _ensure_deepgram()
        if not success:
            raise TTSError(f"Deepgram SDK not available: {error}")
        
        # Ensure audio dependencies for playback
        success, error = _ensure_audio_dependencies()
        if not success:
            raise TTSError(f"Audio dependencies not available: {error}")
        
        # Get API key (will prompt user if missing)
        self.api_key = get_api_key('deepgram')
        if not self.api_key:
            MCPLogger.log(TOOL_LOG_NAME, "DEEPGRAM_API_KEY not available")
            raise TTSError("Deepgram API key not found. Please provide your API key when prompted, or set DEEPGRAM_API_KEY in your configuration.")
        
        # Create client
        self.client = deepgram_DeepgramClient(api_key=self.api_key)
        MCPLogger.log(TOOL_LOG_NAME, "Deepgram provider initialized")

    # The three read-only operations below return hardcoded data, so they are
    # classmethods callable without an instance - no SDK install or API key is
    # needed just to browse Deepgram's voices/models/settings
    @classmethod
    def list_voices(cls) -> Dict:
        """Get list of available voices."""
        MCPLogger.log(TOOL_LOG_NAME, "Returning Deepgram voice list")
        return {"voices": cls.VOICES}

    @classmethod
    def get_voice_settings(cls, voice_id: str) -> dict:
        """Get customizable settings for a voice."""
        MCPLogger.log(TOOL_LOG_NAME, f"Returning default voice settings for {voice_id}")
        return {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "speed": 1.0,
            "use_speaker_boost": True
        }

    @classmethod
    def get_models(cls) -> dict:
        """Get available models."""
        MCPLogger.log(TOOL_LOG_NAME, "Returning Deepgram models list")
        return {
            "models": [
                {
                    "model_id": "aura-2",
                    "name": "Aura-2",
                    "description": "Deepgram's latest TTS model with improved quality",
                    "can_be_finetuned": False,
                    "can_use_style": False,
                    "can_use_speaker_boost": True,
                    "languages": [{"language_id": "en", "name": "English"}]
                },
                {
                    "model_id": "aura",
                    "name": "Aura",
                    "description": "Deepgram's original TTS model",
                    "can_be_finetuned": False,
                    "can_use_style": False,
                    "can_use_speaker_boost": True,
                    "languages": [{"language_id": "en", "name": "English"}]
                }
            ]
        }

    def _generate_audio_pcm(self, text: str, voice_id: str) -> Tuple[bytes, int]:
        """Generate audio using Deepgram REST API with LINEAR16 encoding.
        
        Returns:
            Tuple of (raw PCM bytes, sample_rate)
        """
        MCPLogger.log(TOOL_LOG_NAME, f"Generating PCM audio with Deepgram REST API (voice: {voice_id})")
        
        # Generate audio using REST API with LINEAR16 encoding for direct playback
        # This avoids needing to decode MP3
        audio_iterator = self.client.speak.v1.audio.generate(
            text=text,
            model=voice_id,
            encoding="linear16",  # Request raw PCM instead of MP3
            sample_rate=24000     # Match Google Cloud's sample rate
        )
        
        # Collect all audio bytes from the iterator
        audio_bytes = b''.join(audio_iterator)
        MCPLogger.log(TOOL_LOG_NAME, f"Generated {len(audio_bytes)} bytes of PCM audio")
        
        return audio_bytes, 24000

    def speak(self, text: str, voice_id: str, model_id: Optional[str] = None,
             output_format: str = "mp3_22050_32",
             voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and play to speakers using sounddevice (blocks until playback completes)."""
        try:
            self._validate_text_length(text)
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            # Generate PCM audio (not MP3) for direct playback
            audio_bytes, sample_rate = self._generate_audio_pcm(text, voice_id)
            
            # Convert bytes to numpy array for sounddevice
            audio_array = numpy_module.frombuffer(audio_bytes, dtype=numpy_module.int16)
            
            # Play synchronously: speak blocks until playback completes for all
            # providers, so playback errors surface to the caller (no false success)
            MCPLogger.log(TOOL_LOG_NAME, "Starting audio playback with sounddevice")
            sounddevice_module.play(audio_array, samplerate=sample_rate)
            sounddevice_module.wait()  # Wait until playback is finished
            MCPLogger.log(TOOL_LOG_NAME, "Audio playback completed")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/play speech: {str(e)}")
            raise TTSError(f"Failed to generate/play speech: {str(e)}")

    def save(self, text: str, save_path: str, voice_id: str,
            model_id: Optional[str] = None, output_format: str = "mp3_44100_128",
            voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and save to file using Deepgram (MP3, or WAV when output_format='wav')."""
        try:
            self._validate_text_length(text)
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            if output_format == "wav":
                # Reuse the linear16 PCM synthesis path and wrap it in a WAV container
                audio_bytes, sample_rate = self._generate_audio_pcm(text, voice_id)
                with wave.open(str(save_path), 'wb') as wav_file:
                    wav_file.setnchannels(1)   # mono
                    wav_file.setsampwidth(2)   # 16-bit samples
                    wav_file.setframerate(sample_rate)
                    wav_file.writeframes(audio_bytes)
                MCPLogger.log(TOOL_LOG_NAME, f"Saved WAV audio to {save_path}")
                return
            
            # For saving, use MP3 format (more portable)
            MCPLogger.log(TOOL_LOG_NAME, f"Generating MP3 audio with Deepgram REST API (voice: {voice_id})")
            audio_iterator = self.client.speak.v1.audio.generate(
                text=text,
                model=voice_id
                # Default encoding is MP3
            )
            audio_bytes = b''.join(audio_iterator)
            MCPLogger.log(TOOL_LOG_NAME, f"Generated {len(audio_bytes)} bytes of MP3 audio")
            
            with open(save_path, 'wb') as f:
                f.write(audio_bytes)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Saved audio to {save_path}")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/save speech: {str(e)}")
            raise TTSError(f"Failed to generate/save speech: {str(e)}")

class GoogleCloudProvider(TTSProvider):
    """Handler for Google Cloud TTS provider."""
    
    def __init__(self):
        """Initialize the Google Cloud provider."""
        MCPLogger.log(TOOL_LOG_NAME, "Initializing Google Cloud provider")
        
        # Ensure Google Cloud TTS SDK is installed
        success, error = _ensure_google_cloud_tts()
        if not success:
            raise TTSError(f"Google Cloud TTS SDK not available: {error}")
        
        # Ensure audio dependencies are available
        success, error = _ensure_audio_dependencies()
        if not success:
            raise TTSError(f"Audio dependencies not available: {error}")
        
        # First check environment variables: prefer the documented / Google-SDK-standard
        # GOOGLE_APPLICATION_CREDENTIALS, but still accept the legacy GOOGLE_CREDENTIAL
        env_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_CREDENTIAL")
        if env_creds:
            creds_path = Path(env_creds)
        else:
            # Use default path relative to this script
            script_dir = Path(__file__).parent
            creds_path = script_dir / "../private/certs/natural-region-455100-g4-14f20129c784.json"
        
        if not creds_path.exists():
            MCPLogger.log(TOOL_LOG_NAME, f"Google Cloud credentials file not found at {creds_path}")
            raise TTSError(f"Google Cloud credentials file not found at {creds_path}. Please set GOOGLE_APPLICATION_CREDENTIALS environment variable or place your credentials file at the expected location.")
            
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path.resolve())
            
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Creating Google Cloud TTS client")
            self.client = google_texttospeech.TextToSpeechClient()
            MCPLogger.log(TOOL_LOG_NAME, "Google Cloud TTS client initialized successfully")
            
            # Initialize instance variables
            self.pending_bytes = b''
            
            # Warm up connection
            self._warm_up_connection()
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize Google Cloud client: {str(e)}")
            raise TTSError(f"Failed to initialize Google Cloud client: {str(e)}")

    def _warm_up_connection(self):
        """Perform a lightweight API call to warm up connection and auth."""
        MCPLogger.log(TOOL_LOG_NAME, "Warming up connection with list_voices() call...")
        try:
            # Only request English voices to minimize data transfer
            self.client.list_voices(language_code="en-US")
            MCPLogger.log(TOOL_LOG_NAME, "Connection ready for synthesis")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Warm-up call failed: {str(e)}")
            raise

    def list_voices(self) -> Dict:
        """Get list of available voices grouped by type with pricing."""
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Fetching Google Cloud voice list")
            response = self.client.list_voices(language_code="en-US")
            
            # Categorize voices by type
            voices = {
                "standard": [],    # $4/1M chars
                "wavenet": [],     # $16/1M chars
                "studio": [],      # $160/1M chars
                "neural2": [],     # $16/1M chars
                "chirp3": []      # $30/1M chars
            }
            
            pricing = {
                "standard": 4,     # $4 per million characters
                "wavenet": 16,     # $16 per million characters
                "studio": 160,     # $160 per million characters
                "chirp3": 30,     # $30 per million characters
                "neural2": 16     # $16 per million characters
            }
            
            for voice in response.voices:
                name = voice.name
                if "Studio" in name:
                    category = "studio"
                elif "Wavenet" in name:
                    category = "wavenet"
                elif "Neural2" in name:
                    category = "neural2"
                elif "Chirp3" in name:
                    category = "chirp3"
                else:
                    category = "standard"
                
                voice_info = {
                    "name": name,
                    "language_code": voice.language_codes[0],
                    "ssml_gender": str(voice.ssml_gender),
                    "natural_sample_rate_hertz": voice.natural_sample_rate_hertz,
                    "usd_per_million_chars": pricing[category]
                }
                
                voices[category].append(voice_info)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Successfully categorized {sum(len(v) for v in voices.values())} voices")
            return {"voices": voices}
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to fetch voices: {str(e)}")
            raise TTSError(f"Failed to fetch voices: {str(e)}")

    def get_voice_settings(self, voice_id: str) -> dict:
        """Get customizable settings for a voice."""
        # For now, return basic settings that match Google's capabilities
        return {
            "speaking_rate": 1.0,  # 0.25 to 4.0
            "pitch": 0.0,         # -20.0 to 20.0
            "volume_gain_db": 0.0  # -96.0 to 16.0
        }

    def get_models(self) -> dict:
        """Get available models and their capabilities."""
        return {
            "models": [
                {
                    "model_id": "standard",
                    "name": "Standard",
                    "description": "Basic TTS model",
                    "price_per_million_chars": 4,
                    "streaming_supported": True
                },
                {
                    "model_id": "wavenet",
                    "name": "WaveNet",
                    "description": "Neural TTS model with improved quality",
                    "price_per_million_chars": 16,
                    "streaming_supported": True
                },
                {
                    "model_id": "chirp3",
                    "name": "Chirp3",
                    "description": "Latest neural TTS model with highest quality",
                    "price_per_million_chars": 30,
                    "streaming_supported": True
                }
            ]
        }

    def speak(self, text: str, voice_id: str, model_id: Optional[str] = None,
             output_format: str = "mp3_22050_32",
             voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and play to speakers (blocks until playback completes)."""
        try:
            self._validate_text_length(text)  # enforce the 8KB cap on the Google path too
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            # Configure synthesis input
            synthesis_input = google_texttospeech.SynthesisInput(text=text)
            
            # Configure voice
            voice = google_texttospeech.VoiceSelectionParams(
                name=voice_id,
                language_code="en-US"  # Default to US English
            )
            
            # Configure audio output
            audio_config = google_texttospeech.AudioConfig(
                audio_encoding=google_texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=24000
            )
            
            # Apply voice settings if provided
            if voice_settings:
                audio_config.speaking_rate = voice_settings.get('speaking_rate', 1.0)
                audio_config.pitch = voice_settings.get('pitch', 0.0)
                audio_config.volume_gain_db = voice_settings.get('volume_gain_db', 0.0)
            
            # Initialize audio queue and done event
            audio_queue = queue.Queue()
            done_event = threading.Event()
            self.pending_bytes = b''  # Reset pending bytes
            
            def audio_callback(outdata, frames, stream_time, status):
                """Callback for sounddevice stream to get audio data."""
                if status:
                    MCPLogger.log(TOOL_LOG_NAME, f'Status: {status}')
                try:
                    # If we have remaining data from last callback, use that first
                    if hasattr(audio_callback, 'remaining_data') and len(audio_callback.remaining_data) > 0:
                        data = audio_callback.remaining_data
                        if len(data) <= len(outdata):
                            outdata[:len(data), 0] = data
                            outdata[len(data):] = 0
                            audio_callback.remaining_data = numpy_module.array([], dtype=numpy_module.int16)
                        else:
                            outdata[:, 0] = data[:len(outdata)]
                            audio_callback.remaining_data = data[len(outdata):]
                        return

                    # Only get new data if we've finished the current chunk
                    data = audio_queue.get_nowait()
                    
                    # Store any remaining data for next callback
                    if len(data) <= len(outdata):
                        outdata[:len(data), 0] = data
                        outdata[len(data):] = 0
                        audio_callback.remaining_data = numpy_module.array([], dtype=numpy_module.int16)
                    else:
                        outdata[:, 0] = data[:len(outdata)]
                        audio_callback.remaining_data = data[len(outdata):]

                except queue.Empty:
                    outdata.fill(0)
                    if audio_queue.empty() and len(self.pending_bytes) == 0:  # Only set done if no pending data
                        done_event.set()
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error in audio callback: {str(e)}")
                    outdata.fill(0)
                    done_event.set()

            # Initialize the remaining_data buffer
            audio_callback.remaining_data = numpy_module.array([], dtype=numpy_module.int16)
            
            def queue_aligned_audio(audio_bytes):
                """Queue audio data in 1024-byte aligned chunks."""
                # Combine with any pending bytes from previous chunk
                all_bytes = self.pending_bytes + audio_bytes
                
                # Calculate how many complete 1024-byte chunks we can make
                chunk_size = 1024  # Must be multiple of 2 since we're using 16-bit samples
                aligned_size = (len(all_bytes) // chunk_size) * chunk_size
                
                if aligned_size > 0:
                    # Convert the aligned portion to a numpy array
                    aligned_bytes = all_bytes[:aligned_size]
                    audio_array = numpy_module.frombuffer(aligned_bytes, dtype=numpy_module.int16)
                    audio_queue.put(audio_array)
                    MCPLogger.log(TOOL_LOG_NAME, f"Queued {aligned_size} aligned bytes ({len(audio_array)} samples)")
                    
                    # Save any remaining bytes for next time
                    self.pending_bytes = all_bytes[aligned_size:]
                    if len(self.pending_bytes) > 0:
                        MCPLogger.log(TOOL_LOG_NAME, f"Saved {len(self.pending_bytes)} bytes for next chunk")
                else:
                    # Not enough for a complete chunk, save all for next time
                    self.pending_bytes = all_bytes
                    MCPLogger.log(TOOL_LOG_NAME, f"Accumulated {len(self.pending_bytes)} bytes, waiting for more")
            
            # Create an output stream that matches Google TTS format exactly
            audio_stream = sounddevice_module.OutputStream(
                samplerate=24000,  # Google TTS uses 24kHz
                channels=1,        # Mono output
                dtype=numpy_module.int16,    # 16-bit PCM
                blocksize=512,     # Small blocksize for lower latency
                callback=audio_callback,
                finished_callback=lambda: done_event.set()
            )
            
            # Start audio stream BEFORE getting synthesis response
            audio_stream.start()
            MCPLogger.log(TOOL_LOG_NAME, "Started audio stream")
            
            # Get synthesis response
            MCPLogger.log(TOOL_LOG_NAME, "Starting synthesis")
            response = self.client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )
            
            # Process audio data
            if response.audio_content:
                queue_aligned_audio(response.audio_content)
                
                # Handle any remaining bytes - only add padding if we have pending data
                if len(self.pending_bytes) > 0:
                    # Calculate padding needed for alignment
                    padding_needed = 1024 - len(self.pending_bytes)
                    padded_bytes = self.pending_bytes + b'\x00' * padding_needed
                    audio_array = numpy_module.frombuffer(padded_bytes, dtype=numpy_module.int16)
                    audio_queue.put(audio_array)
                    MCPLogger.log(TOOL_LOG_NAME, f"Queued final {len(self.pending_bytes)} bytes with {padding_needed} bytes of padding")
                    self.pending_bytes = b''  # Clear the pending buffer
            
            # Wait for all audio to finish playing
            while not audio_queue.empty():
                time.sleep(0.1)
            time.sleep(0.5)  # Give a little extra time for final audio
            
            MCPLogger.log(TOOL_LOG_NAME, "Audio playback completed")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/play speech: {str(e)}")
            raise TTSError(f"Failed to generate/play speech: {str(e)}")
        finally:
            if 'audio_stream' in locals():
                audio_stream.stop()
                audio_stream.close()

    def save(self, text: str, save_path: str, voice_id: str,
            model_id: Optional[str] = None, output_format: str = "mp3_44100_128",
            voice_settings: Optional[Dict] = None) -> None:
        """Convert text to speech and save to file."""
        try:
            self._validate_text_length(text)  # enforce the 8KB cap on the Google path too
            MCPLogger.log(TOOL_LOG_NAME, f"Converting text to speech (length: {len(text)})")
            
            # Configure synthesis input
            synthesis_input = google_texttospeech.SynthesisInput(text=text)
            
            # Configure voice
            voice = google_texttospeech.VoiceSelectionParams(
                name=voice_id,
                language_code="en-US"  # Default to US English
            )
            
            # Configure audio output
            audio_config = google_texttospeech.AudioConfig(
                audio_encoding=google_texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=24000
            )
            
            # Apply voice settings if provided
            if voice_settings:
                audio_config.speaking_rate = voice_settings.get('speaking_rate', 1.0)
                audio_config.pitch = voice_settings.get('pitch', 0.0)
                audio_config.volume_gain_db = voice_settings.get('volume_gain_db', 0.0)
            
            # Get synthesis response
            MCPLogger.log(TOOL_LOG_NAME, "Starting synthesis")
            response = self.client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )
            
            # Ensure save directory exists
            save_dir = os.path.dirname(save_path)
            if save_dir and not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            # Write audio content to WAV file
            if response.audio_content:
                with open(save_path, 'wb') as f:
                    f.write(response.audio_content)
                MCPLogger.log(TOOL_LOG_NAME, f"Audio saved to {save_path}")
            else:
                raise TTSError("No audio content received from synthesis")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to generate/save speech: {str(e)}")
            raise TTSError(f"Failed to generate/save speech: {str(e)}")

# Cache of successfully-initialized provider instances, keyed by provider name, so
# repeated speak/save calls do not re-instantiate and re-run warm-up API calls
_tts_provider_instance_cache: Dict[str, TTSProvider] = {}

# There is a single system audio output device: serialize speak playback so
# concurrent requests play one at a time instead of overlapping/contending
_playback_serialization_lock = threading.Lock()

def get_provider(provider_name: str) -> Union[TTSProvider, None]:
    """Get the appropriate provider instance (cached after first successful init)."""
    providers = {
        "google": GoogleCloudProvider,     # Make this the default
        "elevenlabs": ElevenLabsProvider,
        "deepgram": DeepgramProvider
    }
    
    if provider_name not in providers:
        raise TTSError(f"Unknown provider: {provider_name}. Available providers: {', '.join(providers.keys())}")
    
    cached_provider = _tts_provider_instance_cache.get(provider_name)
    if cached_provider is not None:
        MCPLogger.log(TOOL_LOG_NAME, f"Reusing cached {provider_name} provider instance")
        return cached_provider
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Creating provider instance for {provider_name}")
        provider = providers[provider_name]()
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully created {provider_name} provider instance")
        # Only successful initializations are cached, so a failed init (e.g. missing
        # API key or credentials) is retried on the next call
        _tts_provider_instance_cache[provider_name] = provider
        return provider
    except Exception as e:
        raise TTSError(f"Failed to initialize provider {provider_name}: {str(e)}")

def handle_tts(input_param: Dict) -> Dict:
    """Handle TTS operations via MCP interface."""
    try:
        # Remove the server-injected synthetic handler_info key on a shallow copy so
        # the caller's dict is never mutated (the server passes the same dict for
        # internal calls); its value is unused by this tool
        if isinstance(input_param, dict):
            input_param = dict(input_param)
            input_param.pop('handler_info', None)
        
        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
            return {
                "content": [{"type": "text", "text": json.dumps({"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2)}],
                "isError": False
            }
            
        # Validate input structure
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
            
        # Check for token
        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response("Invalid or missing tool_unlock_token. Please call with operation='readme' first to get the token.", with_readme=True)

        # Validate all parameters against the schema (mirrors stt.py); the raw
        # input_param is still used below so schema defaults do not alter behavior
        error_msg, _validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract basic parameters
        operation = input_param.get("operation")
            
        # Get provider name after readme check
        provider_name = input_param.get("provider", "")
        if not provider_name:
            return create_error_response("provider must be specified", with_readme=True)
        
        # Log only operation metadata - never the raw text (user speech content)
        # or the full parameter dict
        MCPLogger.log(TOOL_LOG_NAME, f"Handling {operation} operation for provider {provider_name} (text_len={len(input_param.get('text') or '')})")
        
        # "wav" output applies to save with google/deepgram only (both natively
        # produce 24kHz LINEAR16); reject elevenlabs+wav before any SDK/API-key work
        if operation in ("speak", "save") and input_param.get("output_format") == "wav" and provider_name == "elevenlabs":
            return create_error_response("output_format 'wav' is not supported by the elevenlabs provider (its native formats are mp3). Use provider 'google' or 'deepgram' for WAV output, or an mp3_* output_format with elevenlabs.", with_readme=False)
        
        # Deepgram's voice/model/settings data is hardcoded (no API call involved),
        # so serve its read-only operations from the class without instantiating the
        # provider - instantiation would demand the SDK and an API key needlessly
        if provider_name == "deepgram" and operation in ("list_voices", "get_voice_settings", "get_models"):
            provider = DeepgramProvider  # read-only ops are classmethods; no client needed
        else:
            # Get provider instance
            provider = get_provider(provider_name)
        
        # Handle operation
        if operation == "list_voices":
            MCPLogger.log(TOOL_LOG_NAME, "Executing list_voices operation")
            result = provider.list_voices()
            MCPLogger.log(TOOL_LOG_NAME, "list_voices operation completed successfully")
        elif operation == "get_voice_settings":
            voice_id = input_param.get("voice_id")
            if not voice_id:
                raise TTSError("voice_id required for get_voice_settings")
            MCPLogger.log(TOOL_LOG_NAME, f"Executing get_voice_settings operation for voice {voice_id}")
            result = provider.get_voice_settings(voice_id)
            MCPLogger.log(TOOL_LOG_NAME, "get_voice_settings operation completed successfully")
        elif operation == "get_models":
            MCPLogger.log(TOOL_LOG_NAME, "Executing get_models operation")
            result = provider.get_models()
            MCPLogger.log(TOOL_LOG_NAME, "get_models operation completed successfully")
        elif operation in ["speak", "save"]:
            # Validate required parameters
            text = input_param.get("text")
            voice_id = input_param.get("voice_id")
            if not text or not voice_id:
                raise TTSError("text and voice_id required for speak/save operations")
                
            # Get optional parameters
            model_id = input_param.get("model_id")
            # save defaults to high quality, speak to low latency, matching the provider
            # save/speak signature defaults (previously both incorrectly got mp3_22050_32)
            output_format = input_param.get("output_format", "mp3_44100_128" if operation == "save" else "mp3_22050_32")
            voice_settings = input_param.get("voice_settings")
            
            if operation == "speak":
                MCPLogger.log(TOOL_LOG_NAME, f"Executing speak operation with voice {voice_id}")
                # Serialize playback: one system audio output, one stream at a time
                with _playback_serialization_lock:
                    provider.speak(
                        text=text,
                        voice_id=voice_id,
                        model_id=model_id,
                        output_format=output_format,
                        voice_settings=voice_settings
                    )
                # speak blocks until playback finishes, so "played" is only returned
                # after successful playback (failures raise and become error responses)
                result = {"status": "played"}
                MCPLogger.log(TOOL_LOG_NAME, "speak operation completed successfully")
            else:  # save
                save_path = input_param.get("save_path")
                if not save_path:
                    raise TTSError("save_path required for save operation")
                MCPLogger.log(TOOL_LOG_NAME, f"Executing save operation with voice {voice_id} to {save_path}")
                provider.save(
                    text=text,
                    save_path=save_path,
                    voice_id=voice_id,
                    model_id=model_id,
                    output_format=output_format,
                    voice_settings=voice_settings
                )
                result = {"status": "saved", "path": save_path}
                MCPLogger.log(TOOL_LOG_NAME, "save operation completed successfully")
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Unknown operation requested: {operation}")
            raise TTSError(f"Unknown operation: {operation}")
            
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False
        }
            
    except Exception as e:
        return create_error_response(f"Error: {str(e)}", with_readme=False)

# Map of tool names to their handlers
HANDLERS = {
  TOOL_NAME: handle_tts
}
