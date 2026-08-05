"""
File: ragtag/tools/stt.py
Project: Aura Friday MCP-Link Server
Component: Speech-to-Text Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for speech-to-text transcription via microphone or audio file input.
Enables AI agents to listen to the user and receive voice input.

Deps:
- python -m pip install sounddevice numpy webrtcvad-wheels openai faster-whisper

Supports:
- Microphone input with voice activity detection
- Audio file transcription
- Cloud (OpenAI Whisper API) and local (faster-whisper) transcription
- Configurable timeouts and silence detection
- Debug features: audio recording, transcript saving

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary

"signature": "ʋAɡꓔīMᏮƏƍƻþƧᏮꓟbCƽꓮɋτꞇƶXƶK𝟨ƏƲYzƍꓗďukŧꓬHᖴʈCⴹꓳMᗪһnxhЗ6𝛢ĐᏂꓐyƽmaıƧТLᗅ𝟟𝐴1ƱHʋ𝟫МƤ𝟢rᴍƍЅQꓴꓳ𝙰Uaωu5սϜոƲ𝟟ⴹďꓬᎪꜱꓳЗ𝟧ꓣhԛƿꓰᏮꓚВτ"
"signdate": "2026-07-23T02:38:06.056Z",
"""

import json
import os
import io
import sys
import time
import wave
import queue
import threading
import tempfile
import traceback
import atexit
import glob as glob_module
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Union, Tuple, Any
from dataclasses import dataclass, asdict

from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory, get_config_manager

# Platform detection
import platform
CURRENT_PLATFORM = platform.system()
IS_WINDOWS = CURRENT_PLATFORM == 'Windows'
IS_MACOS = CURRENT_PLATFORM == 'Darwin'
IS_LINUX = CURRENT_PLATFORM == 'Linux'

# Constants
TOOL_LOG_NAME = "STT"
AUDIO_SAMPLE_RATE = 16000  # 16kHz - required by most STT engines
AUDIO_CHANNELS = 1  # Mono
AUDIO_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
AUDIO_CHUNK_DURATION_MS = 30  # 30ms chunks for VAD
AUDIO_CHUNK_SAMPLES = int(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_DURATION_MS / 1000)
# Hard safety caps for the listen operation. Applied even when timeout_seconds /
# initial_silence_timeout_seconds are 0 ("indefinite"), so listen can never hang
# forever holding the microphone, and stored audio cannot grow memory without bound.
LISTEN_ABSOLUTE_MAX_RECORDING_SECONDS = 3600
LISTEN_MAX_STORED_AUDIO_SECONDS = 600  # ~18.3 MiB of 16 kHz mono int16; also keeps WAV under the 25 MB API upload limit
LISTEN_MAX_STORED_AUDIO_CHUNKS = int(LISTEN_MAX_STORED_AUDIO_SECONDS * 1000 / AUDIO_CHUNK_DURATION_MS)
# Max transcribe_file input sizes: the whole file is read into memory, and the cloud
# path uploads it to OpenAI (whose Whisper API rejects uploads over 25 MB anyway).
# The local path allows more, but still bounds memory use for AI-supplied paths.
TRANSCRIBE_FILE_MAX_CLOUD_UPLOAD_BYTES = 25 * 1024 * 1024
TRANSCRIBE_FILE_MAX_LOCAL_FILE_BYTES = 200 * 1024 * 1024

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"stt{TOOL_NAME_SUFFIX}"

# Temp file prefix for tracking STT-created temp files
STT_TEMP_PREFIX = "stt_recording_"

# Optional imports - lazy-loaded with auto-install
sounddevice = None
numpy_module = None
webrtcvad = None
openai_client = None
faster_whisper = None

# Loaded faster-whisper model instances, keyed by (model_size, device, compute_type),
# so repeat transcriptions reuse the model instead of reloading it on every call.
_faster_whisper_model_cache = {}

# Track if we've already attempted installation (to avoid infinite loops)
_install_attempted = False

# Track temp files created during transcription for cleanup.
# Guarded by _temp_files_created_lock: transcription worker threads add/discard while
# atexit cleanup iterates, which could otherwise raise "set changed size during iteration".
_temp_files_created = set()
_temp_files_created_lock = threading.Lock()

def _cleanup_temp_files():
  """Clean up any temp files created by STT tool on shutdown."""
  global _temp_files_created
  
  # Snapshot and clear under the lock, then delete outside it
  with _temp_files_created_lock:
    tracked_temp_file_paths = list(_temp_files_created)
    _temp_files_created.clear()
  for temp_file in tracked_temp_file_paths:
    try:
      if os.path.exists(temp_file):
        os.unlink(temp_file)
        MCPLogger.log(TOOL_LOG_NAME, f"Cleaned up temp file: {temp_file}")
    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"Failed to clean up {temp_file}: {e}")

def _cleanup_orphaned_temp_files():
  """Clean up any orphaned STT temp files from previous crashes.
  
  Called once at module load to clean up files left behind by crashes.
  """
  try:
    temp_dir = tempfile.gettempdir()
    pattern = os.path.join(temp_dir, f"{STT_TEMP_PREFIX}*.wav")
    orphaned_files = glob_module.glob(pattern)
    
    for temp_file in orphaned_files:
      try:
        # Only delete files older than 10 minutes: a legitimately in-use temp file
        # lives only for the duration of one transcription call, so anything older
        # is a crash leftover (was 1 hour, which let crash artifacts linger)
        file_age_seconds = time.time() - os.path.getmtime(temp_file)
        if file_age_seconds > 600:  # 10 minutes
          os.unlink(temp_file)
          MCPLogger.log(TOOL_LOG_NAME, f"Cleaned up orphaned temp file: {temp_file}")
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Failed to clean up orphaned {temp_file}: {e}")
        
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Error during orphaned temp file cleanup: {e}")

def _cleanup_active_session():
  """Clean up any active recording session on shutdown."""
  global _active_recording_session
  
  try:
    if _active_recording_session is not None:
      MCPLogger.log(TOOL_LOG_NAME, "Cleaning up active recording session on shutdown")
      _active_recording_session.is_recording = False
      _active_recording_session.audio_data.clear()
      _active_recording_session = None
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Error cleaning up active session: {e}")

def _atexit_cleanup():
  """Combined cleanup handler for atexit."""
  _cleanup_active_session()
  _cleanup_temp_files()

# Register cleanup handlers
atexit.register(_atexit_cleanup)

# Clean up orphaned files from previous crashes on module load
try:
  _cleanup_orphaned_temp_files()
except Exception:
  pass  # Don't fail module load on cleanup errors

def _pip_install(packages: list, description: str = "") -> bool:
  """Install packages using pip. Returns True if successful."""
  try:
    import subprocess
    
    desc = description or ", ".join(packages)
    MCPLogger.log(TOOL_LOG_NAME, f"Auto-installing: {desc}...")
    
    # Use sys.executable to ensure we use the same Python that's running the server
    cmd = [sys.executable, "-m", "pip", "install"] + packages
    
    # Run pip install
    result = subprocess.run(
      cmd,
      capture_output=True,
      text=True,
      timeout=300  # 5 minute timeout for large packages like faster-whisper
    )
    
    if result.returncode == 0:
      MCPLogger.log(TOOL_LOG_NAME, f"Successfully installed: {desc}")
      return True
    else:
      MCPLogger.log(TOOL_LOG_NAME, f"Failed to install {desc}: {result.stderr}")
      return False
      
  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Error during pip install: {str(e)}")
    return False

def _ensure_core_dependencies() -> Tuple[bool, Optional[str]]:
  """Ensure core dependencies (sounddevice, numpy, webrtcvad) are installed.
  
  Returns:
    Tuple of (success, error_message)
  """
  global sounddevice, numpy_module, webrtcvad, _install_attempted
  
  # Try to load existing dependencies first
  missing_packages = []
  
  # Check sounddevice
  if sounddevice is None:
    try:
      import sounddevice as sd
      sounddevice = sd
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
  
  # Check webrtcvad (note: pip package is webrtcvad-wheels)
  if webrtcvad is None:
    try:
      import webrtcvad as vad
      webrtcvad = vad
      MCPLogger.log(TOOL_LOG_NAME, "Loaded webrtcvad")
    except ImportError:
      missing_packages.append("webrtcvad-wheels")  # Use wheels package for cross-platform
  
  # If all loaded, we're good
  if not missing_packages:
    return True, None
  
  # If we've already tried installing, don't try again
  if _install_attempted:
    return False, f"Dependencies still missing after install attempt: {', '.join(missing_packages)}"
  
  _install_attempted = True
  
  # Try to install missing packages
  MCPLogger.log(TOOL_LOG_NAME, f"Missing core dependencies: {missing_packages}")
  
  if _pip_install(missing_packages, "STT core dependencies"):
    # Try loading again after install
    if "sounddevice" in missing_packages:
      try:
        import sounddevice as sd
        sounddevice = sd
      except ImportError as e:
        return False, f"Failed to load sounddevice after install: {e}"
    
    if "numpy" in missing_packages:
      try:
        import numpy as np
        numpy_module = np
      except ImportError as e:
        return False, f"Failed to load numpy after install: {e}"
    
    if "webrtcvad-wheels" in missing_packages:
      try:
        import webrtcvad as vad
        webrtcvad = vad
      except ImportError as e:
        return False, f"Failed to load webrtcvad after install: {e}"
    
    return True, None
  else:
    install_cmd = f"{sys.executable} -m pip install sounddevice numpy webrtcvad-wheels"
    return False, f"Auto-install failed. Please install manually:\n{install_cmd}"

def _ensure_openai() -> Tuple[bool, Optional[str]]:
  """Ensure OpenAI client is installed.
  
  Returns:
    Tuple of (success, error_message)
  """
  global openai_client
  
  if openai_client is not None:
    return True, None
  
  try:
    from openai import OpenAI
    openai_client = OpenAI
    MCPLogger.log(TOOL_LOG_NAME, "Loaded OpenAI client")
    return True, None
  except ImportError:
    pass
  
  # Try to install
  if _pip_install(["openai"], "OpenAI client"):
    try:
      from openai import OpenAI
      openai_client = OpenAI
      return True, None
    except ImportError as e:
      return False, f"Failed to load openai after install: {e}"
  else:
    install_cmd = f"{sys.executable} -m pip install openai"
    return False, f"OpenAI client not available. Install with:\n{install_cmd}"

# Background-install state for faster-whisper, guarded by its lock. The package is
# large ("may take several minutes"), so the install runs in a background thread and
# callers get an immediate "installing, retry shortly" answer instead of a long block.
_faster_whisper_background_install_lock = threading.Lock()
_faster_whisper_background_install_state = "not_started"  # not_started | in_progress | failed

def _faster_whisper_background_install_worker():
  """Run the faster-whisper pip install in a background thread and record the outcome."""
  global _faster_whisper_background_install_state
  install_succeeded = _pip_install(["faster-whisper"], "faster-whisper (local transcription)")
  with _faster_whisper_background_install_lock:
    _faster_whisper_background_install_state = "not_started" if install_succeeded else "failed"

def _ensure_faster_whisper() -> Tuple[bool, Optional[str]]:
  """Ensure faster-whisper is installed (optional, for local transcription).

  If it is missing, kicks off a background install and returns immediately with a
  "retry shortly" message rather than blocking the calling worker thread for minutes.

  Returns:
    Tuple of (success, error_message)
  """
  global faster_whisper, _faster_whisper_background_install_state
  
  if faster_whisper is not None:
    return True, None
  
  try:
    from faster_whisper import WhisperModel
    faster_whisper = WhisperModel
    MCPLogger.log(TOOL_LOG_NAME, "Loaded faster-whisper")
    return True, None
  except ImportError:
    pass
  
  install_cmd = f"{sys.executable} -m pip install faster-whisper"
  with _faster_whisper_background_install_lock:
    if _faster_whisper_background_install_state == "in_progress":
      return False, "faster-whisper is still being installed in the background (this may take several minutes). Retry shortly."
    if _faster_whisper_background_install_state == "failed":
      return False, f"faster-whisper auto-install failed. Install manually with:\n{install_cmd}"
    # not_started: kick off the background install and answer immediately
    _faster_whisper_background_install_state = "in_progress"
    MCPLogger.log(TOOL_LOG_NAME, "faster-whisper not found, installing in background (this may take several minutes)...")
    threading.Thread(target=_faster_whisper_background_install_worker, name="stt-faster-whisper-install", daemon=True).start()
  return False, "faster-whisper was not installed; installation has started in the background (this may take several minutes). Retry shortly."

def _load_optional_dependencies():
  """Load optional dependencies on first use with auto-install."""
  global sounddevice, numpy_module, webrtcvad, openai_client, faster_whisper

  # Try to load core deps (sounddevice, numpy, webrtcvad)
  success, error = _ensure_core_dependencies()
  if not success and error:
    MCPLogger.log(TOOL_LOG_NAME, f"Core dependencies issue: {error}")

  # Try to load OpenAI client (don't fail if missing - might use local model)
  if openai_client is None:
    try:
      from openai import OpenAI
      openai_client = OpenAI
      MCPLogger.log(TOOL_LOG_NAME, "Loaded OpenAI client for Whisper API")
    except ImportError:
      MCPLogger.log(TOOL_LOG_NAME, "OpenAI client not available (will auto-install when needed)")

  # Try to load faster-whisper (optional)
  if faster_whisper is None:
    try:
      from faster_whisper import WhisperModel
      faster_whisper = WhisperModel
      MCPLogger.log(TOOL_LOG_NAME, "Loaded faster-whisper for local transcription")
    except ImportError:
      MCPLogger.log(TOOL_LOG_NAME, "faster-whisper not available (optional, will auto-install if requested)")

# Global state for managing active recording sessions
_active_recording_session = None
_recording_lock = threading.Lock()

@dataclass
class RecordingSession:
  """Manages an active recording session."""
  session_id: str
  start_time: float
  audio_data: list
  is_recording: bool = True
  speech_detected: bool = False
  last_speech_time: float = 0.0
  error: Optional[str] = None

# Tool definitions
TOOLS = [
  {
    "name": TOOL_NAME,
    "description": """Speech-to-Text tool for voice input from microphone or audio files.
- Use this tool when you need to listen to the user speaking or transcribe audio files
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
          "enum": ["readme", "listen", "transcribe_file", "list_input_devices", "stop_listening", "get_status"],
          "description": "Operation to perform"
        },
        "timeout_seconds": {
          "type": "integer",
          "description": "Maximum time to listen in seconds. 0 = wait indefinitely (default: 60)",
          "default": 60
        },
        "silence_timeout_seconds": {
          "type": "number",
          "description": "End recording after this many seconds of silence after speech (default: 2.0)",
          "default": 2.0
        },
        "initial_silence_timeout_seconds": {
          "type": "integer",
          "description": "Max seconds to wait for first speech. 0 = wait indefinitely (default: 30)",
          "default": 30
        },
        "min_speech_duration_seconds": {
          "type": "number",
          "description": "Minimum duration of speech to capture before allowing silence to end recording (default: 0.5)",
          "default": 0.5
        },
        "input_device": {
          "type": "string",
          "description": "Microphone device ID or name substring to use. Leave empty for default device."
        },
        "audio_file_path": {
          "type": "string",
          "description": "Path to audio file for transcribe_file operation. Supports mp3, wav, m4a, ogg, flac, webm. Max 25 MB for cloud transcription (200 MB with use_local_model: true)."
        },
        "save_audio_to_file": {
          "type": "string",
          "description": "Debug: Path to save recorded audio as WAV file."
        },
        "save_transcript_to_file": {
          "type": "string",
          "description": "Debug: Path to save transcript as text file."
        },
        "language": {
          "type": "string",
          "description": "Language hint for transcription (ISO-639-1 code like 'en', 'es', 'fr'). Leave empty for auto-detect."
        },
        "use_local_model": {
          "type": "boolean",
          "description": "Use local faster-whisper model instead of OpenAI API. true keeps all audio on this machine; false (default) uploads audio to OpenAI for transcription",
          "default": False
        },
        "local_model_size": {
          "type": "string",
          "enum": ["tiny", "base", "small", "medium", "large-v3", "turbo"],
          "description": "Size of local Whisper model to use (default: 'base')",
          "default": "base"
        },
        "vad_aggressiveness": {
          "type": "integer",
          "description": "Voice activity detection aggressiveness 0-3. Higher = more aggressive filtering (default: 2)",
          "default": 2
        },
        "openai_api_key": {
          "type": "string",
          "description": "OpenAI API key for Whisper transcription. If not provided, uses OPENAI_API_KEY from environment or config."
        },
        "tool_unlock_token": {
          "type": "string",
          "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
        }
      },
      "required": ["operation", "tool_unlock_token"],
      "type": "object"
    },
    "readme": """
Speech-to-Text (STT) Tool - Listen to User Voice Input

This tool enables AI agents to receive voice input from users via microphone recording
or by transcribing audio files. It uses OpenAI's Whisper API (cloud) or faster-whisper
(local) for high-quality speech recognition.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Privacy Note (read before recording)
By default (use_local_model: false), recorded microphone audio and transcribe_file audio are
UPLOADED TO OPENAI's Whisper API for transcription. Set use_local_model: true to keep all
audio on this machine (local faster-whisper engine; no audio leaves the computer).

## Operations Available

### 1. listen - Record from microphone and transcribe
Records audio from the microphone until speech ends (detected by silence after speech),
then transcribes the recording.

**Key Parameters:**
- **timeout_seconds**: Max recording time (0 = wait indefinitely until speech ends)
- **silence_timeout_seconds**: Stop after this much silence following speech (default: 2.0)
- **initial_silence_timeout_seconds**: Max wait for first speech (0 = indefinite)
- **input_device**: Specific microphone to use (optional)

### 2. transcribe_file - Transcribe an audio file
Transcribes an existing audio file without recording.

**Parameters:**
- **audio_file_path**: Path to the audio file (mp3, wav, m4a, ogg, flac, webm)
- **language**: Optional language hint

Size limits: 25 MB for cloud transcription (OpenAI's upload limit); 200 MB with
use_local_model: true.

### 3. list_input_devices - List available microphones
Returns a list of available audio input devices.

### 4. stop_listening - Stop an active recording
Immediately stops any active recording session.

### 5. get_status - Check if currently recording
Returns current recording status.

## Debug Features

### Save Audio Recording
Set **save_audio_to_file** to save the captured audio as a WAV file for debugging:
```json
{
  "input": {
    "operation": "listen",
    "save_audio_to_file": "C:/temp/debug_recording.wav",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### Save Transcript
Set **save_transcript_to_file** to save the transcript alongside audio:
```json
{
  "input": {
    "operation": "listen",
    "save_audio_to_file": "C:/temp/recording.wav",
    "save_transcript_to_file": "C:/temp/recording.txt",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Transcription Engines

### Cloud: OpenAI Whisper API (Default)
- Best accuracy
- Cost: ~$0.006/minute
- Supports all languages
- API key sources (in order of priority):
  1. **openai_api_key** parameter (directly in tool call)
  2. OPENAI_API_KEY environment variable
  3. Config file (api_keys.OPENAI_API_KEY)

### Local: faster-whisper (Optional)
- No API key needed
- Runs on CPU or GPU
- Set **use_local_model: true**
- Choose model size with **local_model_size**: tiny, base, small, medium, large-v3, turbo

## Auto-Installation

Dependencies are automatically installed on first use:
- **Core**: sounddevice, numpy, webrtcvad-wheels (installed when you first call listen/list_input_devices)
- **OpenAI**: openai (installed when you use cloud transcription)
- **Local**: faster-whisper (installed when you set use_local_model: true)

No manual pip install required!

## Voice Activity Detection (VAD)

The tool uses WebRTC VAD to detect speech start and end:
- Recording starts when speech is detected
- Recording ends after **silence_timeout_seconds** of silence following speech
- **vad_aggressiveness** (0-3) controls sensitivity: higher values filter more noise

## Input Examples

### 1. Basic voice input (wait for user to speak):
```json
{
  "input": {
    "operation": "listen",
    "timeout_seconds": 60,
    "silence_timeout_seconds": 2.0,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 2. Wait indefinitely for user (user may be away):
```json
{
  "input": {
    "operation": "listen",
    "timeout_seconds": 0,
    "initial_silence_timeout_seconds": 0,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 3. Quick voice command (short timeout):
```json
{
  "input": {
    "operation": "listen",
    "timeout_seconds": 10,
    "silence_timeout_seconds": 1.5,
    "initial_silence_timeout_seconds": 5,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 4. Transcribe an audio file:
```json
{
  "input": {
    "operation": "transcribe_file",
    "audio_file_path": "C:/recordings/meeting.mp3",
    "language": "en",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 5. Use local model (no cloud):
```json
{
  "input": {
    "operation": "listen",
    "use_local_model": true,
    "local_model_size": "small",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 6. List available microphones:
```json
{
  "input": {
    "operation": "list_input_devices",
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

### 7. Provide API key directly (no env var needed):
```json
{
  "input": {
    "operation": "listen",
    "openai_api_key": "sk-...",
    "timeout_seconds": 30,
    "tool_unlock_token": \"""" + TOOL_UNLOCK_TOKEN + """\"
  }
}
```

## Return Values

### Successful transcription:
```json
{
  "status": "success",
  "transcript": "Hello, this is what the user said.",
  "duration_seconds": 3.5,
  "speech_duration_seconds": 3.1,
  "language_detected": "en",
  "engine_used": "openai_whisper",
  "audio_saved_to": "C:/temp/recording.wav",
  "transcript_saved_to": "C:/temp/recording.txt"
}
```
For listen, duration_seconds is the captured recording length; speech_duration_seconds is
the detected-speech span within it (transcribe_file reports the engine's duration only).

### Timeout with no speech:
```json
{
  "status": "timeout",
  "message": "No speech detected within timeout period",
  "waited_seconds": 30
}
```

### Recording stopped:
```json
{
  "status": "stopped",
  "message": "Recording was stopped by user request"
}
```

## Platform Support
- **Windows**: Full support (primary)
- **macOS**: Supported with proper audio permissions
- **Linux**: Supported with PulseAudio/ALSA

## Dependencies
Required:
- sounddevice (pip install sounddevice)
- numpy (pip install numpy)
- webrtcvad (pip install webrtcvad)
- openai (pip install openai) - for cloud transcription

Optional:
- faster-whisper (pip install faster-whisper) - for local transcription

## Tips for Best Results
1. Set appropriate timeout values for your use case
2. Use language hints when you know the expected language
"""
  }
]


def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
  """Validate input parameters against the real_parameters schema."""
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


def create_error_response(error_msg: str, with_readme: bool = True, include_traceback: bool = True) -> Dict:
  """Create an error response with optional documentation."""
  MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
  # Only log a traceback when an exception is actually active; many callers invoke
  # this with no exception, where format_exc() would log a useless "NoneType: None".
  if include_traceback and sys.exc_info()[0] is not None:
    stack_trace = traceback.format_exc()
    MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {stack_trace}")
  return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def _get_input_device_id(device_name_or_id: Optional[str] = None) -> Optional[int]:
  """Get the input device ID from name substring or ID string."""
  if sounddevice is None:
    return None

  if device_name_or_id is None or device_name_or_id == "":
    return None  # Use default device

  # Try as integer ID first
  try:
    device_id = int(device_name_or_id)
    devices = sounddevice.query_devices()
    # Verify the device actually has input channels (the name-search branch already does)
    if 0 <= device_id < len(devices) and devices[device_id]['max_input_channels'] > 0:
      return device_id
  except ValueError:
    pass

  # Search by name substring
  devices = sounddevice.query_devices()
  for idx, device in enumerate(devices):
    if device_name_or_id.lower() in device['name'].lower():
      if device['max_input_channels'] > 0:
        return idx

  return None


def handle_list_input_devices(params: Dict) -> Dict:
  """List available audio input devices."""
  try:
    _load_optional_dependencies()

    if sounddevice is None:
      return create_error_response("sounddevice library not available. Install with: pip install sounddevice", with_readme=False)

    devices = sounddevice.query_devices()
    input_devices = []

    for idx, device in enumerate(devices):
      if device['max_input_channels'] > 0:
        input_devices.append({
          "id": idx,
          "name": device['name'],
          "channels": device['max_input_channels'],
          "sample_rate": device['default_samplerate'],
          "is_default": idx == sounddevice.default.device[0]
        })

    result = {
      "status": "success",
      "devices": input_devices,
      "default_device_id": sounddevice.default.device[0],
      "count": len(input_devices)
    }

    return {
      "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
      "isError": False
    }

  except Exception as e:
    return create_error_response(f"Error listing input devices: {str(e)}", with_readme=False)


def handle_get_status(params: Dict) -> Dict:
  """Get current recording status."""
  global _active_recording_session

  with _recording_lock:
    if _active_recording_session is None:
      status = {"status": "idle", "is_recording": False}
    else:
      session = _active_recording_session
      elapsed = time.time() - session.start_time
      status = {
        "status": "recording" if session.is_recording else "processing",
        "is_recording": session.is_recording,
        "session_id": session.session_id,
        "elapsed_seconds": round(elapsed, 2),
        "speech_detected": session.speech_detected
      }

  return {
    "content": [{"type": "text", "text": json.dumps(status, indent=2)}],
    "isError": False
  }


def handle_stop_listening(params: Dict) -> Dict:
  """Stop an active recording session."""
  global _active_recording_session

  with _recording_lock:
    if _active_recording_session is None:
      return {
        "content": [{"type": "text", "text": json.dumps({"status": "no_active_session", "message": "No recording in progress"}, indent=2)}],
        "isError": False
      }

    _active_recording_session.is_recording = False
    session_id = _active_recording_session.session_id

  return {
    "content": [{"type": "text", "text": json.dumps({"status": "stopped", "session_id": session_id, "message": "Recording stopped"}, indent=2)}],
    "isError": False
  }


def _transcribe_with_openai(audio_data: bytes, language: Optional[str] = None, api_key: Optional[str] = None, upload_filename_for_format_detection: str = "recording.wav") -> Dict:
  """Transcribe audio using OpenAI Whisper API.
  
  Args:
    audio_data: Raw audio bytes (WAV format unless upload_filename_for_format_detection says otherwise)
    language: Optional language hint (ISO-639-1 code)
    api_key: Optional API key (if not provided, uses env/config)
    upload_filename_for_format_detection: Filename attached to the upload; the API uses its extension to detect the audio format
  
  Returns:
    Dict with transcript, language, duration, engine OR error
  """
  # Ensure OpenAI client is available (auto-install if needed)
  success, error = _ensure_openai()
  if not success:
    return {"error": error}

  # Get API key: parameter > environment > config
  effective_api_key = api_key
  
  if not effective_api_key:
    effective_api_key = os.environ.get("OPENAI_API_KEY")
  
  if not effective_api_key:
    try:
      from ragtag.shared_config import SharedConfigManager
      config_manager = get_config_manager()
      config = config_manager.load_config()
      api_keys = SharedConfigManager.ensure_settings_section(config, 'api_keys')
      effective_api_key = api_keys.get("OPENAI_API_KEY")
      if effective_api_key:
        MCPLogger.log(TOOL_LOG_NAME, "Found OPENAI_API_KEY in config file")
    except Exception as e:
      MCPLogger.log(TOOL_LOG_NAME, f"Could not get API key from config: {e}")

  if not effective_api_key:
    return {"error": "OPENAI_API_KEY not found. Provide via 'openai_api_key' parameter, OPENAI_API_KEY environment variable, or config file."}

  try:
    client = openai_client(api_key=effective_api_key)

    # Create a file-like object from the audio bytes
    audio_file = io.BytesIO(audio_data)
    audio_file.name = upload_filename_for_format_detection

    # Transcribe using Whisper API
    kwargs = {
      "model": "whisper-1",
      "file": audio_file,
      "response_format": "verbose_json",
      "temperature": 0.0
    }
    if language:
      kwargs["language"] = language

    transcription = client.audio.transcriptions.create(**kwargs)

    return {
      "transcript": transcription.text,
      "language": getattr(transcription, 'language', language or 'unknown'),
      "duration": getattr(transcription, 'duration', None),
      "engine": "openai_whisper"
    }

  except Exception as e:
    return {"error": f"OpenAI transcription failed: {str(e)}"}


def _transcribe_with_local_model(audio_data: bytes, language: Optional[str] = None, model_size: str = "base") -> Dict:
  """Transcribe audio using local faster-whisper model.
  
  Args:
    audio_data: Raw audio bytes (WAV format)
    language: Optional language hint (ISO-639-1 code)
    model_size: Model size: tiny, base, small, medium, large-v3, turbo
  
  Returns:
    Dict with transcript, language, duration, engine OR error
  """
  # Ensure faster-whisper is available (auto-install if needed)
  success, error = _ensure_faster_whisper()
  if not success:
    return {"error": error}

  try:
    # Determine device and compute type - default to CPU for reliability
    device = "cpu"
    compute_type = "int8"  # CPU-friendly default

    # Detect CUDA via CTranslate2 (the engine faster-whisper actually runs on) instead
    # of torch, which is not a faster-whisper dependency and may be absent: previously
    # a CUDA machine without torch always fell back to CPU
    try:
      import ctranslate2
      if ctranslate2.get_cuda_device_count() > 0:
        device = "cuda"
        compute_type = "float16"
        MCPLogger.log(TOOL_LOG_NAME, "CTranslate2 reports CUDA device(s) available, using GPU")
      else:
        MCPLogger.log(TOOL_LOG_NAME, "CTranslate2 reports no CUDA devices, using CPU")
    except Exception as cuda_probe_error:
      MCPLogger.log(TOOL_LOG_NAME, f"CUDA probe via CTranslate2 failed ({cuda_probe_error}), using CPU")
      device = "cpu"
      compute_type = "int8"

    # Load model, reusing a previously loaded instance when available
    model_cache_key = (model_size, device, compute_type)
    model = _faster_whisper_model_cache.get(model_cache_key)
    if model is None:
      MCPLogger.log(TOOL_LOG_NAME, f"Loading faster-whisper model: {model_size} on {device}")
      try:
        model = faster_whisper(model_size, device=device, compute_type=compute_type)
      except Exception as model_load_error:
        if device != "cuda":
          raise
        # CUDA was reported present but the model would not load on it (driver /
        # cuDNN problems): fall back to CPU rather than failing the transcription
        MCPLogger.log(TOOL_LOG_NAME, f"CUDA model load failed ({model_load_error}), falling back to CPU")
        device = "cpu"
        compute_type = "int8"
        model_cache_key = (model_size, device, compute_type)
        model = _faster_whisper_model_cache.get(model_cache_key)
        if model is None:
          model = faster_whisper(model_size, device=device, compute_type=compute_type)
      _faster_whisper_model_cache[model_cache_key] = model

    # Save audio to temp file for processing (with prefix for cleanup tracking)
    with tempfile.NamedTemporaryFile(prefix=STT_TEMP_PREFIX, suffix=".wav", delete=False) as tmp_file:
      tmp_file.write(audio_data)
      tmp_path = tmp_file.name
      with _temp_files_created_lock:
        _temp_files_created.add(tmp_path)

    try:
      kwargs = {"beam_size": 5}
      if language:
        kwargs["language"] = language

      segments, info = model.transcribe(tmp_path, **kwargs)

      # Collect all segments
      transcript_parts = []
      for segment in segments:
        transcript_parts.append(segment.text)

      transcript = " ".join(transcript_parts).strip()

      return {
        "transcript": transcript,
        "language": info.language,
        "duration": info.duration,
        "engine": f"faster_whisper_{model_size}"
      }

    finally:
      # Clean up temp file and remove from tracking
      try:
        os.unlink(tmp_path)
        with _temp_files_created_lock:
          _temp_files_created.discard(tmp_path)
      except Exception:
        pass  # Best effort cleanup

  except Exception as e:
    return {"error": f"Local transcription failed: {str(e)}"}


def _save_audio_to_wav(audio_data: list, file_path: str) -> bool:
  """Save raw audio data to WAV file."""
  try:
    if numpy_module is None:
      return False

    # Convert list of numpy arrays to single array
    audio_array = numpy_module.concatenate(audio_data) if audio_data else numpy_module.array([], dtype=numpy_module.int16)

    # Write WAV file
    with wave.open(file_path, 'wb') as wf:
      wf.setnchannels(AUDIO_CHANNELS)
      wf.setsampwidth(AUDIO_SAMPLE_WIDTH)
      wf.setframerate(AUDIO_SAMPLE_RATE)
      wf.writeframes(audio_array.tobytes())

    MCPLogger.log(TOOL_LOG_NAME, f"Saved audio to {file_path}")
    return True

  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"Failed to save audio: {str(e)}")
    return False


def _audio_data_to_wav_bytes(audio_data: list) -> bytes:
  """Convert raw audio data to WAV bytes."""
  if numpy_module is None:
    return b''

  audio_array = numpy_module.concatenate(audio_data) if audio_data else numpy_module.array([], dtype=numpy_module.int16)

  buffer = io.BytesIO()
  with wave.open(buffer, 'wb') as wf:
    wf.setnchannels(AUDIO_CHANNELS)
    wf.setsampwidth(AUDIO_SAMPLE_WIDTH)
    wf.setframerate(AUDIO_SAMPLE_RATE)
    wf.writeframes(audio_array.tobytes())

  return buffer.getvalue()


def handle_listen(params: Dict) -> Dict:
  """Record from microphone and transcribe speech."""
  global _active_recording_session

  try:
    _load_optional_dependencies()

    if sounddevice is None or numpy_module is None:
      return create_error_response(
        "Audio recording requires sounddevice and numpy. Install with: pip install sounddevice numpy",
        with_readme=False
      )

    # Extract parameters
    timeout_seconds = params.get("timeout_seconds", 60)
    silence_timeout_seconds = params.get("silence_timeout_seconds", 2.0)
    initial_silence_timeout_seconds = params.get("initial_silence_timeout_seconds", 30)
    min_speech_duration_seconds = params.get("min_speech_duration_seconds", 0.5)
    input_device = params.get("input_device")
    save_audio_to_file = params.get("save_audio_to_file")
    save_transcript_to_file = params.get("save_transcript_to_file")
    language = params.get("language")
    use_local_model = params.get("use_local_model", False)
    local_model_size = params.get("local_model_size", "base")
    vad_aggressiveness = params.get("vad_aggressiveness", 2)
    openai_api_key = params.get("openai_api_key")

    # Check if already recording
    with _recording_lock:
      if _active_recording_session is not None and _active_recording_session.is_recording:
        return create_error_response("Already recording. Use stop_listening first.", with_readme=False)

    # Get device ID
    device_id = _get_input_device_id(input_device)

    # Initialize VAD if available
    vad = None
    if webrtcvad is not None:
      try:
        vad = webrtcvad.Vad(vad_aggressiveness)
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Failed to initialize VAD: {e}")

    # Create recording session
    session_id = f"rec_{int(time.time())}"
    session = RecordingSession(
      session_id=session_id,
      start_time=time.time(),
      audio_data=[]
    )

    with _recording_lock:
      _active_recording_session = session

    MCPLogger.log(TOOL_LOG_NAME, f"Starting recording session {session_id}")

    # Recording state
    speech_start_time = None
    total_speech_duration = 0.0
    last_speech_time = 0.0
    chunks_since_speech = 0
    chunks_needed_for_silence = int(silence_timeout_seconds * 1000 / AUDIO_CHUNK_DURATION_MS)

    # Calculate chunk size for recording
    chunk_samples = AUDIO_CHUNK_SAMPLES

    try:
      # Open audio stream
      stream_kwargs = {
        "samplerate": AUDIO_SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "dtype": numpy_module.int16,
        "blocksize": chunk_samples
      }
      if device_id is not None:
        stream_kwargs["device"] = device_id

      with sounddevice.InputStream(**stream_kwargs) as stream:
        MCPLogger.log(TOOL_LOG_NAME, "Audio stream opened, listening...")

        while session.is_recording:
          current_time = time.time()
          elapsed = current_time - session.start_time

          # Check overall timeout
          if timeout_seconds > 0 and elapsed >= timeout_seconds:
            MCPLogger.log(TOOL_LOG_NAME, f"Overall timeout reached ({timeout_seconds}s)")
            break

          # Absolute safety cap: applies even when timeout_seconds and
          # initial_silence_timeout_seconds are 0, so listen can never hang forever
          if elapsed >= LISTEN_ABSOLUTE_MAX_RECORDING_SECONDS:
            MCPLogger.log(TOOL_LOG_NAME, f"Absolute max recording time reached ({LISTEN_ABSOLUTE_MAX_RECORDING_SECONDS}s)")
            break

          # Check initial silence timeout
          if not session.speech_detected:
            if initial_silence_timeout_seconds > 0 and elapsed >= initial_silence_timeout_seconds:
              MCPLogger.log(TOOL_LOG_NAME, f"Initial silence timeout ({initial_silence_timeout_seconds}s)")
              session.is_recording = False
              with _recording_lock:
                _active_recording_session = None
              return {
                "content": [{"type": "text", "text": json.dumps({
                  "status": "timeout",
                  "message": "No speech detected within initial timeout period",
                  "waited_seconds": round(elapsed, 2)
                }, indent=2)}],
                "isError": False
              }

          # Read audio chunk
          audio_chunk, overflowed = stream.read(chunk_samples)
          if overflowed:
            MCPLogger.log(TOOL_LOG_NAME, "Audio buffer overflow")

          # Convert to bytes for VAD
          audio_bytes = audio_chunk.flatten().tobytes()

          # Detect speech using VAD
          is_speech = False
          if vad is not None:
            try:
              # VAD expects 16-bit PCM
              is_speech = vad.is_speech(audio_bytes, AUDIO_SAMPLE_RATE)
            except Exception:
              # VAD might fail on some audio, assume speech
              is_speech = True
          else:
            # No VAD, use simple energy detection
            # Cast to int32 first: abs(-32768) overflows in int16
            energy = numpy_module.abs(audio_chunk.astype(numpy_module.int32)).mean()
            is_speech = energy > 500  # Threshold for 16-bit audio

          if is_speech:
            if not session.speech_detected:
              session.speech_detected = True
              speech_start_time = current_time
              MCPLogger.log(TOOL_LOG_NAME, "Speech started")

            last_speech_time = current_time
            chunks_since_speech = 0

            # Store audio
            session.audio_data.append(audio_chunk.copy())

          else:
            if session.speech_detected:
              chunks_since_speech += 1

              # Still store audio during silence gaps
              session.audio_data.append(audio_chunk.copy())

              # Calculate speech duration
              if speech_start_time:
                total_speech_duration = last_speech_time - speech_start_time

              # Check if silence timeout reached (and minimum speech captured)
              if chunks_since_speech >= chunks_needed_for_silence:
                if total_speech_duration >= min_speech_duration_seconds:
                  MCPLogger.log(TOOL_LOG_NAME, f"Silence timeout after {total_speech_duration:.2f}s of speech")
                  break
                else:
                  # Reset - not enough speech yet
                  chunks_since_speech = 0

          # Bound stored audio growth: stop capture once the buffered audio reaches
          # the max duration, so an indefinite recording cannot grow memory without limit
          if len(session.audio_data) >= LISTEN_MAX_STORED_AUDIO_CHUNKS:
            MCPLogger.log(TOOL_LOG_NAME, f"Max stored audio duration reached ({LISTEN_MAX_STORED_AUDIO_SECONDS}s), stopping capture")
            break

    except Exception as e:
      session.error = str(e)
      MCPLogger.log(TOOL_LOG_NAME, f"Recording error: {e}")
      with _recording_lock:
        _active_recording_session = None
      return create_error_response(f"Recording failed: {str(e)}", with_readme=False)

    finally:
      session.is_recording = False

    # Check if we got any speech
    if not session.speech_detected or not session.audio_data:
      with _recording_lock:
        _active_recording_session = None
      return {
        "content": [{"type": "text", "text": json.dumps({
          "status": "no_speech",
          "message": "No speech detected during recording",
          "duration_seconds": round(time.time() - session.start_time, 2)
        }, indent=2)}],
        "isError": False
      }

    MCPLogger.log(TOOL_LOG_NAME, f"Recording complete: {len(session.audio_data)} chunks, {total_speech_duration:.2f}s speech")

    # Save audio if requested
    audio_saved_to = None
    if save_audio_to_file:
      if _save_audio_to_wav(session.audio_data, save_audio_to_file):
        audio_saved_to = save_audio_to_file

    # Actual length of the captured recording (each stored chunk is 30 ms). The
    # speech-only figure understates it by excluding silence gaps/trailing silence.
    recorded_audio_duration_seconds = len(session.audio_data) * AUDIO_CHUNK_DURATION_MS / 1000.0

    # Convert audio to WAV bytes for transcription
    wav_bytes = _audio_data_to_wav_bytes(session.audio_data)
    
    # Clear audio data from session to free memory (we have wav_bytes now)
    session.audio_data.clear()

    # Transcribe
    if use_local_model:
      transcription_result = _transcribe_with_local_model(wav_bytes, language, local_model_size)
    else:
      transcription_result = _transcribe_with_openai(wav_bytes, language, openai_api_key)

    with _recording_lock:
      _active_recording_session = None

    if "error" in transcription_result:
      return create_error_response(transcription_result["error"], with_readme=False)

    # Save transcript if requested
    transcript_saved_to = None
    if save_transcript_to_file and "transcript" in transcription_result:
      try:
        with open(save_transcript_to_file, 'w', encoding='utf-8') as f:
          f.write(transcription_result["transcript"])
        transcript_saved_to = save_transcript_to_file
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Failed to save transcript: {e}")

    # Build result. duration_seconds is the captured recording length (previously it
    # was mislabeled with the speech-only duration, which is now reported separately).
    result = {
      "status": "success",
      "transcript": transcription_result.get("transcript", ""),
      "duration_seconds": round(recorded_audio_duration_seconds, 2),
      "speech_duration_seconds": round(total_speech_duration, 2),
      "language_detected": transcription_result.get("language"),
      "engine_used": transcription_result.get("engine")
    }

    if audio_saved_to:
      result["audio_saved_to"] = audio_saved_to
    if transcript_saved_to:
      result["transcript_saved_to"] = transcript_saved_to

    return {
      "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
      "isError": False
    }

  except Exception as e:
    with _recording_lock:
      _active_recording_session = None
    return create_error_response(f"Listen operation failed: {str(e)}", with_readme=True)


def handle_transcribe_file(params: Dict) -> Dict:
  """Transcribe an existing audio file."""
  try:
    _load_optional_dependencies()

    audio_file_path = params.get("audio_file_path")
    if not audio_file_path:
      return create_error_response("audio_file_path is required for transcribe_file operation", with_readme=False)

    # Validate file exists
    if not os.path.exists(audio_file_path):
      return create_error_response(f"Audio file not found: {audio_file_path}", with_readme=False)

    language = params.get("language")
    use_local_model = params.get("use_local_model", False)
    local_model_size = params.get("local_model_size", "base")
    save_transcript_to_file = params.get("save_transcript_to_file")
    openai_api_key = params.get("openai_api_key")

    # Enforce a size cap before reading: the whole file is loaded into memory, and the
    # cloud path uploads the bytes to OpenAI (whose API rejects files over 25 MB anyway),
    # so an AI-supplied path cannot exhaust memory or bulk-exfiltrate a huge file
    try:
      audio_file_size_bytes = os.path.getsize(audio_file_path)
    except OSError as size_error:
      return create_error_response(f"Cannot determine audio file size: {size_error}", with_readme=False)
    max_allowed_audio_file_bytes = TRANSCRIBE_FILE_MAX_LOCAL_FILE_BYTES if use_local_model else TRANSCRIBE_FILE_MAX_CLOUD_UPLOAD_BYTES
    if audio_file_size_bytes > max_allowed_audio_file_bytes:
      engine_label = "local transcription" if use_local_model else "cloud transcription (OpenAI upload limit)"
      return create_error_response(
        f"Audio file too large: {audio_file_size_bytes} bytes exceeds the {max_allowed_audio_file_bytes} byte limit for {engine_label}.",
        with_readme=False
      )

    MCPLogger.log(TOOL_LOG_NAME, f"Transcribing file: {audio_file_path}")

    # Read audio file
    with open(audio_file_path, 'rb') as f:
      audio_data = f.read()

    # Transcribe
    if use_local_model:
      transcription_result = _transcribe_with_local_model(audio_data, language, local_model_size)
    else:
      # Pass the real filename so the API detects the format from its extension
      # (previously hardcoded to recording.wav, mislabeling e.g. .mp3 uploads)
      transcription_result = _transcribe_with_openai(audio_data, language, openai_api_key, upload_filename_for_format_detection=os.path.basename(audio_file_path))

    if "error" in transcription_result:
      return create_error_response(transcription_result["error"], with_readme=False)

    # Save transcript if requested
    transcript_saved_to = None
    if save_transcript_to_file and "transcript" in transcription_result:
      try:
        with open(save_transcript_to_file, 'w', encoding='utf-8') as f:
          f.write(transcription_result["transcript"])
        transcript_saved_to = save_transcript_to_file
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Failed to save transcript: {e}")

    result = {
      "status": "success",
      "transcript": transcription_result.get("transcript", ""),
      "source_file": audio_file_path,
      "duration_seconds": transcription_result.get("duration"),
      "language_detected": transcription_result.get("language"),
      "engine_used": transcription_result.get("engine")
    }

    if transcript_saved_to:
      result["transcript_saved_to"] = transcript_saved_to

    return {
      "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
      "isError": False
    }

  except Exception as e:
    return create_error_response(f"Transcribe file failed: {str(e)}", with_readme=True)


def handle_stt(input_param: Dict) -> Dict:
  """Handle STT tool operations via MCP interface."""
  try:
    # Remove the server-injected synthetic handler_info key early (before validation)
    # on a shallow copy, so the caller's dict is never mutated (the server passes the
    # same dict for internal calls); its value is unused by this tool
    if isinstance(input_param, dict):
      input_param = dict(input_param)
      input_param.pop('handler_info', None)

    if isinstance(input_param, dict) and "input" in input_param:
      input_param = input_param["input"]

    # Handle readme operation first
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
      return create_error_response("Invalid or missing tool_unlock_token", with_readme=True)

    # Validate all parameters using schema
    error_msg, validated_params = validate_parameters(input_param)
    if error_msg:
      return create_error_response(error_msg, with_readme=True)

    # Extract operation
    operation = validated_params.get("operation")

    # Handle operations
    if operation == "listen":
      return handle_listen(validated_params)
    elif operation == "transcribe_file":
      return handle_transcribe_file(validated_params)
    elif operation == "list_input_devices":
      return handle_list_input_devices(validated_params)
    elif operation == "stop_listening":
      return handle_stop_listening(validated_params)
    elif operation == "get_status":
      return handle_get_status(validated_params)
    elif operation == "readme":
      return {
        "content": [{"type": "text", "text": readme(True)}],
        "isError": False
      }
    else:
      valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
      return create_error_response(f"Unknown operation: '{operation}'. Available: {', '.join(valid_operations)}", with_readme=True)

  except Exception as e:
    MCPLogger.log(TOOL_LOG_NAME, f"CRITICAL ERROR in handle_stt: {str(e)}")
    MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
    return create_error_response(f"Error in STT operation: {str(e)}", with_readme=True)


# Map of tool names to their handlers
HANDLERS = {
  TOOL_NAME: handle_stt
}
