"""
File: ragtag/tools/system.py
Project: Aura Friday MCP-Link Server
Component: System Automation Tool
Author: Christopher Nathan Drake (cnd)

RagTag System Tool - Cross-Platform Desktop Automation and Management

! NOTE !  When finding, changing, or adding code in this file BEWARE that this one file serves 3 very different platforms.
Search for the big separator comments with these headings to be sure you're in the correct section!

        ################################   WINDOWS SPECIFIC ROUTINES   ################################
        ################################   APPLE MAC SPECIFIC ROUTINES   ################################
        ################################   LINUX SPECIFIC ROUTINES   ################################
        ################################   COMMON CODE FOR ALL PLATFORMS   ################################

Tool implementation for providing comprehensive desktop automation including:
- Window management and enumeration
- UI element scanning and interaction
- Screenshot and OCR capabilities
- Layout management and automation

## ✅ ELECTRON APP ACCESSIBILITY - FULLY WORKING

### Current Status: FULLY FUNCTIONAL
Electron apps (Signal, Cursor, Joplin, Discord, etc.) now expose their **complete** accessibility tree
including all internal content when scanned. Testing confirms extraction of 1,490+ UI elements from
Cursor IDE including tabs, file names, status indicators, and deep UI tree structures.

### What Works
**Verified on Cursor IDE (2025-11-10):**
- ✅ **TabItemControl** elements with full tab names and selection states
- ✅ File tabs with status indicators ("• 5 problems in this file • Modified")
- ✅ Deep tree traversal (depth level 29+) into Electron's DOM/ARIA tree
- ✅ Complete coordinate data for every UI element
- ✅ All control types (GroupControl, TextControl, ButtonControl, etc.)
- ✅ Visibility states, focus states, and accessibility properties

**Example Extracted Data:**
- Tab names: "Selecting a template for mcu_serial tool" (as TabItemControl)
- File status: "system.py • 5 problems in this file"
- File state: "mcu_serial.py • 8 problems in this file • Modified"
- Symbolic links: "friday.py • 16 problems in this file • Symbolic Link"

### How It Works
The solution is **already active** through multiple mechanisms:

1. **Environment Variables** (set by user):
   - `ELECTRON_ENABLE_ACCESSIBILITY=1` - Forces Electron to enable accessibility
   - `ELECTRON_FORCE_RENDERER_ACCESSIBILITY=1` - Forces renderer process accessibility

2. **Server Registration** (implemented in `friday.py` lines 2257-2536):
   The `WindowsAccessibilityManager` class implements **dual-protocol accessibility registration**:
   
   **a) Traditional Windows Apps** (`_enable_traditional_screen_reader_mode()`):
   - Sets `SPI_SETSCREENREADER` flag via `SystemParametersInfoW()`
   - Makes Windows maintain text accessibility data for all windows
   - Line 2344: `SystemParametersInfoW(SPI_SETSCREENREADER, 1, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)`
   
   **b) Chrome/Electron Apps** (`_enable_chrome_detection_protocol()`):
   - Creates hidden message-only window named "AuraFridayAccessibilityWindow" (line 2445)
   - Responds to `WM_GETOBJECT` messages with Chrome's custom child ID (line 2405)
   - Sends `NotifyWinEvent(EVENT_SYSTEM_ALERT)` to signal assistive tech presence (line 2375)
   - This completes Chrome's accessibility handshake protocol automatically
   
   Called at startup: Line 4183 in `SystemTrayApp._enable_windows_accessibility_mode()`

3. **Windows UI Automation**: The `uiautomation` library handles the WM_GETOBJECT handshake
   transparently when creating controls from Electron window handles.

### Technical Details
Electron apps use a Windows handshake protocol:
1. Chromium calls `NotifyWinEvent(EVENT_SYSTEM_ALERT, ..., id = 1)`
2. An AT client must respond with `WM_GETOBJECT(id = 1)`
3. Chromium switches to "AX-complete mode" and exposes full DOM/ARIA tree

**This handshake is now automatic** thanks to:
- Environment variables forcing accessibility mode at Electron startup
- UI Automation library generating WM_GETOBJECT when accessing Chrome_WidgetWin_1 windows
- Server's AT client registration making Windows treat us as assistive technology

### Performance Notes
- Scans extract 1,490+ elements in ~2-3 seconds
- Deep tree traversal to level 29+ provides complete UI structure
- No manual intervention required (no need to start Narrator/Magnifier)
- Handshake occurs once per Chromium process and persists for process lifetime

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ƧMNÐNƛǝƊеƼ𝘈ҳR𝕌lvAY𝕌𝟚ʋʌGƘТƎQꓳƽꙄvԁƬϨᏴⅠꓦbSⲘƍȜⲟƤꓐꓴ𝟟SʋᏟµᴡEvÞNOνһꓗꓰӠⲦƿꓬPȜꓜⲘVģⲟΤ𝕌kßȷΝꙅzģßƍꓬgʌᗪΗvoꓬⅼKEŧꙄՕƎIqеϜhYᎻƋꓠꙅϹ"
"signdate": "2026-07-22T00:34:43.696Z",
"""

# ============================================================================
# PLATFORM DETECTION AND COMMON IMPORTS
# ============================================================================

import json
import platform
import os
import sys
import time
import subprocess
import threading
import signal
import queue
import tempfile
import traceback
import hashlib  # B5: hash command lines / typed text for logs instead of logging them verbatim
from datetime import datetime
from typing import Dict, List, Optional, Union, BinaryIO, Tuple
from dataclasses import dataclass, asdict

# Determine current platform
CURRENT_PLATFORM = platform.system()  # Returns 'Windows', 'Darwin' (macOS), or 'Linux'
IS_WINDOWS = CURRENT_PLATFORM == 'Windows'
IS_MACOS = CURRENT_PLATFORM == 'Darwin'
IS_LINUX = CURRENT_PLATFORM == 'Linux'

# Common imports that work on all platforms
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory, get_config_manager
# B6: resolve the authenticated caller so terminal sessions can be owner-scoped.
# Safe at import time: the tools package defines get_authenticated_user before it runs
# discover_tools() (which is what imports this module), same pattern as sqlite.py.
from . import get_authenticated_user

try:
    import psutil  # Cross-platform process utilities
except ImportError:
    psutil = None

try:
    from PIL import Image  # Cross-platform image handling
except ImportError:
    Image = None

# ============================================================================
# PLATFORM-SPECIFIC IMPORTS
# ============================================================================

if IS_WINDOWS:
    # Windows-specific imports
    try:
        import win32gui
        import win32con
        import win32api
        import win32process
        import win32console
        import winreg
        import ctypes
        from ctypes import wintypes
        import pythoncom
        import uiautomation as auto
        from PIL import ImageGrab
    except ImportError as e:
        MCPLogger.log("SYSTEM", f"Warning: Windows-specific import failed: {e}")
        
elif IS_MACOS:
    # macOS-specific imports (provided by the bundled pyobjc frameworks).
    # MACOS_HAS_QUARTZ  -> window enumeration (CGWindowList), synthetic mouse/keyboard
    #                      events (CGEvent), and per-window screen capture are available.
    # MACOS_HAS_ACCESSIBILITY_API -> the AXUIElement tree (move_window, scan/click UI
    #                      elements) is importable; ACTUAL use still needs the user to grant
    #                      this process Accessibility permission (checked at call time via
    #                      macos_accessibility_permission_is_granted()).
    MACOS_HAS_QUARTZ = False
    MACOS_HAS_ACCESSIBILITY_API = False
    macos_quartz_module = None
    macos_appkit_module = None
    macos_accessibility_services_module = None
    try:
        import Quartz as macos_quartz_module  # window list, CGEvent, screen capture
        import AppKit as macos_appkit_module  # NSRunningApplication / NSWorkspace
        MACOS_HAS_QUARTZ = True
    except Exception as e:
        MCPLogger.log("SYSTEM", f"Warning: macOS Quartz/AppKit import failed (window/input automation disabled): {e}")
    try:
        import ApplicationServices as macos_accessibility_services_module  # AXUIElement API
        MACOS_HAS_ACCESSIBILITY_API = True
    except Exception as e:
        MCPLogger.log("SYSTEM", f"Warning: macOS ApplicationServices import failed (UI element automation disabled): {e}")
        
elif IS_LINUX:
    # Linux-specific imports
    # NOTE: These libraries try to connect to X display at import time, which fails on headless servers.
    # We catch all display-related errors and gracefully degrade to non-GUI functionality.
    LINUX_HAS_PYWINCTL = False
    LINUX_HAS_XLIB = False
    pwc = None
    
    try:
        # Try PyWinCtl first (cross-platform, works on X11 and Wayland)
        # This import chain (pywinctl -> pymonctl -> ewmhlib -> Xlib) tries to connect
        # to DISPLAY at import time, which fails on headless servers with "Bad display name"
        try:
            import pywinctl as pwc
            LINUX_HAS_PYWINCTL = True
        except ImportError:
            MCPLogger.log("SYSTEM", "PyWinCtl not available - install with: pip install pywinctl")
        except Exception as e:
            # Catch X display connection errors (DisplayNameError, etc.)
            # This happens on headless Linux servers with no DISPLAY set
            error_msg = str(e)
            if "display" in error_msg.lower() or "DISPLAY" in error_msg:
                MCPLogger.log("SYSTEM", f"PyWinCtl unavailable (no X display): {error_msg}")
            else:
                MCPLogger.log("SYSTEM", f"PyWinCtl import failed: {error_msg}")
        
        # Fallback to X11-specific tools (also requires display connection)
        try:
            from Xlib import X, display
            from Xlib.error import DisplayError
            # Test if we can actually connect to a display
            try:
                test_display = display.Display()
                test_display.close()
                LINUX_HAS_XLIB = True
            except Exception:
                # Display connection failed - headless server
                MCPLogger.log("SYSTEM", "Xlib available but no X display - running headless")
        except ImportError:
            pass  # Xlib not installed, that's fine
        except Exception as e:
            MCPLogger.log("SYSTEM", f"Xlib import failed: {e}")
            
    except Exception as e:
        MCPLogger.log("SYSTEM", f"Warning: Linux display library initialization failed: {e}")

# Constants
TOOL_LOG_NAME = "SYSTEM"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"system{TOOL_NAME_SUFFIX}"


# ============================================================================
# LOG REDACTION + SECURITY POLICY HELPERS (COMMON CODE FOR ALL PLATFORMS)
# ============================================================================

def _redact_sensitive_for_log(sensitive_text) -> str:
    """B5: return a non-reversible descriptor (length + short SHA-256) for a command line
    or typed-text string, so full commands / keystrokes never land in the server logs.
    Correlation across log lines is still possible via the hash, without disclosing content."""
    try:
        text_str = sensitive_text if isinstance(sensitive_text, str) else str(sensitive_text)
    except Exception:
        return "<unloggable>"
    digest = hashlib.sha256(text_str.encode("utf-8", "replace")).hexdigest()[:12]
    return f"<redacted {len(text_str)} chars sha256:{digest}>"


def _get_system_tool_security_policy() -> Dict[str, any]:
    """B1-B4: read the optional, operator-controlled security policy for this tool from
    shared config (settings[0].system_tool_security). Reads the in-memory config cache, so
    it is cheap enough to call per request. Returns {} when unset, meaning 'all allowed' -
    existing installs are therefore unchanged unless an operator opts in to locking down."""
    try:
        section = get_config_manager().get_settings_sections_copy("system_tool_security").get("system_tool_security")
        return section if isinstance(section, dict) else {}
    except Exception as policy_read_error:
        MCPLogger.log(TOOL_LOG_NAME, f"Could not read system_tool_security policy (allowing by default): {policy_read_error}")
        return {}


def _capability_is_allowed(security_policy: Dict[str, any], capability_flag_name: str) -> bool:
    """A capability is allowed unless the operator explicitly set its flag to False, so the
    default (missing key) preserves current behaviour."""
    return security_policy.get(capability_flag_name, True) is not False


def _verify_file_path_within_policy(absolute_path: str, original_requested_path: str) -> Tuple[bool, Optional[str]]:
    """B2: enforce the optional file-access jail for write_file/read_file. Returns
    (is_allowed, denial_reason).

    - If settings[0].system_tool_security.file_access_allowlist lists one or more base
      directories, the resolved path must stay under one of them (checked with
      os.path.commonpath so '..' traversal cannot escape).
    - Otherwise (no allowlist configured) a RELATIVE input that escaped the user-data base
      via '..' is refused (safe default); absolute inputs remain allowed for backwards
      compatibility until an operator configures an allowlist."""
    security_policy = _get_system_tool_security_policy()
    configured_allowlist = security_policy.get("file_access_allowlist")
    normalized_target = os.path.normcase(os.path.abspath(absolute_path))

    if isinstance(configured_allowlist, list) and configured_allowlist:
        for allowed_base_directory in configured_allowlist:
            try:
                normalized_base = os.path.normcase(os.path.abspath(str(allowed_base_directory)))
                if os.path.commonpath([normalized_target, normalized_base]) == normalized_base:
                    return True, None
            except (ValueError, TypeError):
                # ValueError e.g. when paths are on different drives on Windows -> not under this base
                continue
        return False, (f"Path '{original_requested_path}' resolves outside the paths permitted by this "
                       f"server's system_tool_security.file_access_allowlist.")

    # No allowlist configured: block relative-path escapes out of the user-data base.
    if not os.path.isabs(original_requested_path):
        try:
            user_data_base = os.path.normcase(os.path.abspath(str(get_user_data_directory())))
            if os.path.commonpath([normalized_target, user_data_base]) != user_data_base:
                return False, (f"Relative path '{original_requested_path}' escapes the user-data directory; "
                               f"use an absolute path or configure system_tool_security.file_access_allowlist.")
        except (ValueError, TypeError):
            return False, f"Could not verify relative path '{original_requested_path}' against the user-data directory."
    return True, None


# D2: short-TTL cache for the expensive `about` section getters. An `about` overview computes
# every section just to show counts; without caching, each overview (and every follow-up
# section drill-down made shortly after) re-runs full process/registry/Appx/browser enumeration
# and a 1-second blocking CPU sample. A small TTL lets one snapshot serve the overview and any
# near-term drill-downs, so only the first call pays the cost.
_about_section_cache_lock = threading.Lock()
_about_section_cache: Dict[str, Tuple[float, any]] = {}
_ABOUT_SECTION_CACHE_TTL_SECONDS = 8.0


def _ttl_cached_about_section(section_producer_callable):
    """Decorator memoizing a zero-argument `about` section getter for a few seconds, so repeated
    about calls reuse one snapshot instead of recomputing. Thread-safe; the short TTL keeps the
    data fresh enough for a status overview."""
    section_cache_key = section_producer_callable.__name__

    def _cached_about_section_wrapper():
        current_monotonic = time.monotonic()
        with _about_section_cache_lock:
            cached_entry = _about_section_cache.get(section_cache_key)
            if cached_entry is not None and current_monotonic < cached_entry[0]:
                return cached_entry[1]
        produced_section_value = section_producer_callable()
        with _about_section_cache_lock:
            _about_section_cache[section_cache_key] = (current_monotonic + _ABOUT_SECTION_CACHE_TTL_SECONDS, produced_section_value)
        return produced_section_value

    _cached_about_section_wrapper.__name__ = section_producer_callable.__name__
    _cached_about_section_wrapper.__doc__ = section_producer_callable.__doc__
    return _cached_about_section_wrapper


################################################################################################################################
################################################################################################################################
################################                      WINDOWS SPECIFIC ROUTINES                 ################################
################################################################################################################################
################################################################################################################################

# Advanced window activation constants and structures (from activate_window_o3.py)
# Only define Windows-specific constants on Windows platform
if IS_WINDOWS:
    ASFW_ANY = -1
    SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    KEYEVENTF_UNICODE = 0x0004

    ULONG_PTR = wintypes.WPARAM  # same width as pointer on Windows
else:
    # Placeholder values for non-Windows platforms
    ASFW_ANY = None
    SPI_GETFOREGROUNDLOCKTIMEOUT = None
    SPI_SETFOREGROUNDLOCKTIMEOUT = None
    KEYEVENTF_UNICODE = None
    ULONG_PTR = None

if IS_WINDOWS:
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk",       wintypes.WORD),
                    ("wScan",     wintypes.WORD),
                    ("dwFlags",   wintypes.DWORD),
                    ("time",      wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx",        wintypes.LONG),
                    ("dy",        wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags",   wintypes.DWORD),
                    ("time",      wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg",      wintypes.DWORD),
                    ("wParamL",   wintypes.WORD),
                    ("wParamH",   wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT),
                    ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT)]

    class DUMMYUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type",  wintypes.DWORD),
                    ("u",     DUMMYUNION)]

    # Full INPUT structure for advanced input
    class INPUT_FULL(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type",  wintypes.DWORD),
                    ("u",     INPUT_UNION)]

    user32 = ctypes.windll.user32

    # Declare ctypes prototypes for the raw win32 calls this module makes, so 64-bit handle
    # and pointer arguments/returns are not silently truncated (ctypes defaults every
    # untyped argument and return value to c_int, which is 32-bit).
    try:
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT_FULL), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
    except Exception as send_input_prototype_error:
        MCPLogger.log("SYSTEM", f"Warning: could not set SendInput prototype: {send_input_prototype_error}")
    try:
        _shcore_for_prototype_declaration = ctypes.windll.shcore
        _shcore_for_prototype_declaration.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR, ctypes.c_int,
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        _shcore_for_prototype_declaration.GetDpiForMonitor.restype = ctypes.c_long  # HRESULT
    except Exception as get_dpi_for_monitor_prototype_error:
        MCPLogger.log("SYSTEM", f"Warning: could not set GetDpiForMonitor prototype: {get_dpi_for_monitor_prototype_error}")
    try:
        _gdi32_for_prototype_declaration = ctypes.windll.gdi32
        _gdi32_for_prototype_declaration.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
        _gdi32_for_prototype_declaration.GetDeviceCaps.restype = ctypes.c_int
    except Exception as get_device_caps_prototype_error:
        MCPLogger.log("SYSTEM", f"Warning: could not set GetDeviceCaps prototype: {get_device_caps_prototype_error}")
else:
    # Placeholder classes for non-Windows platforms
    KEYBDINPUT = None
    MOUSEINPUT = None
    HARDWAREINPUT = None
    INPUT_UNION = None
    DUMMYUNION = None
    INPUT = None
    INPUT_FULL = None
    user32 = None

# ============================================================================
# TERMINAL SESSION MANAGEMENT CLASSES
# ============================================================================

@dataclass
class terminal_session_with_process_tracking:
    """Information about an active terminal session including process and output tracking"""
    process_id: int
    process: subprocess.Popen
    accumulated_output_buffer: str
    newly_available_output_since_last_read: str
    command_execution_has_completed: bool
    session_creation_timestamp: datetime
    output_reading_thread: Optional[threading.Thread]
    output_queue: queue.Queue
    last_exit_code: Optional[int]
    # B6: authenticated user that created this session; only they may read/terminate/list it.
    # None when server auth is not configured (then all callers share the None owner).
    owner_user: Optional[str] = None

@dataclass
class completed_terminal_session_with_full_history:
    """Information about a completed terminal session including final results"""
    process_id: int
    complete_output_text: str
    final_exit_code: Optional[int]
    session_start_time: datetime
    session_end_time: datetime
    # B6: carried over from the active session so ownership checks still apply after completion.
    owner_user: Optional[str] = None

@dataclass
class command_execution_result_with_background_support:
    """Result of command execution with support for background processing"""
    process_id: int
    initial_output_text: str
    command_is_still_running_in_background: bool
    error_message: Optional[str] = None

class comprehensive_terminal_session_manager_with_background_support:
    """Manages terminal sessions with background execution support, similar to Node.js implementation"""
    
    def __init__(self):
        self.active_terminal_sessions: Dict[int, terminal_session_with_process_tracking] = {}
        self.completed_session_history: Dict[int, completed_terminal_session_with_full_history] = {}
        self.next_session_id = 1
        self.maximum_completed_sessions_to_retain = 100
        self.maximum_active_sessions_to_retain = 100
        # Grace period after a command finishes during which its (completed) session stays in
        # the active dict so a near-future read_output can still collect the final output
        # before the session is reaped into the completed history.
        self.completed_session_reap_grace_seconds = 60
        # Guards next_session_id and the active/completed session dicts against concurrent access
        self._session_state_lock = threading.Lock()
        
    def start_command_execution_with_timeout_and_background_support(
        self, 
        command_text: str, 
        timeout_milliseconds: int = 30000,
        shell_path: Optional[str] = None,
        owner_user: Optional[str] = None
    ) -> command_execution_result_with_background_support:
        """Execute a command with timeout support, allowing it to continue in background.

        owner_user (B6): the authenticated user this session belongs to; only they may later
        read/terminate/list it. None when auth is not configured."""
        
        try:
            # Determine shell to use and subprocess parameters
            if platform.system() == "Windows":
                if shell_path:
                    # Handle different shell specifications
                    if shell_path.lower() in ["cmd", "cmd.exe"]:
                        shell_executable = None  # Use default cmd.exe
                        use_shell = True
                    elif shell_path.lower() in ["powershell", "powershell.exe"]:
                        # Use the standard PowerShell path
                        shell_executable = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
                        use_shell = True
                    elif shell_path.lower() in ["pwsh", "pwsh.exe"]:
                        # Try PowerShell Core - first from PATH, then common locations
                        shell_executable = "pwsh.exe"  # This will work if pwsh is in PATH
                        use_shell = True
                    elif shell_path.lower() in ["wsl", "bash"]:
                        # For WSL, we'll use a different approach
                        shell_executable = "wsl.exe"
                        use_shell = False  # We'll handle this specially
                    elif shell_path.startswith("C:\\") or shell_path.startswith("c:\\"):
                        # Full path to shell executable
                        shell_executable = shell_path
                        use_shell = True
                    else:
                        # Assume it's an executable name
                        shell_executable = shell_path
                        use_shell = True
                else:
                    # Default Windows shell (cmd.exe)
                    shell_executable = None
                    use_shell = True
            else:
                # Unix/Linux systems
                if shell_path:
                    shell_executable = shell_path
                    use_shell = False
                else:
                    shell_executable = "/bin/bash"
                    use_shell = False
            
            MCPLogger.log(TOOL_LOG_NAME, f"Starting command execution: {_redact_sensitive_for_log(command_text)} (shell: {shell_executable or 'default'})")
            
            # Start the process
            if platform.system() == "Windows":
                if shell_path and shell_path.lower() in ["wsl", "bash"]:
                    # Special handling for WSL. Build an argv list rather than a single shell
                    # string, so the command is passed to bash as one argument with no fragile
                    # (and injectable) double-quote escaping / nested Windows shell.
                    if command_text.startswith("wsl "):
                        # Strip a redundant leading "wsl " and run the remainder inside WSL bash
                        wsl_inner_command = command_text[4:]
                    else:
                        wsl_inner_command = command_text
                    wsl_argv = ["wsl", "-e", "bash", "-c", wsl_inner_command]
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing WSL command (bash -c): {_redact_sensitive_for_log(wsl_inner_command)}")
                    process = subprocess.Popen(
                        wsl_argv,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,  # A10: line-buffered; bufsize=0 (unbuffered) is unsupported with text=True
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    )
                else:
                    # Windows process creation with specified shell
                    popen_kwargs = {
                        'shell': use_shell,
                        'stdout': subprocess.PIPE,
                        'stderr': subprocess.STDOUT,
                        'text': True,
                        'bufsize': 1,  # A10: line-buffered; bufsize=0 (unbuffered) is unsupported with text=True
                        'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    }
                    
                    if shell_executable:
                        popen_kwargs['executable'] = shell_executable
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Windows command: {_redact_sensitive_for_log(command_text)}")
                    process = subprocess.Popen(command_text, **popen_kwargs)
            else:
                # Unix process creation
                if shell_executable:
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Unix command with shell {shell_executable}: {_redact_sensitive_for_log(command_text)}")
                    process = subprocess.Popen(
                        [shell_executable, '-c', command_text],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,  # A10: line-buffered; bufsize=0 (unbuffered) is unsupported with text=True
                        preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                    )
                else:
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Unix command: {_redact_sensitive_for_log(command_text)}")
                    process = subprocess.Popen(
                        command_text.split(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,  # A10: line-buffered; bufsize=0 (unbuffered) is unsupported with text=True
                        preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                    )
            
            # Create unique session ID (allocated atomically under the shared-state lock)
            with self._session_state_lock:
                session_id = self.next_session_id
                self.next_session_id += 1
            
            # Set up output queue
            output_queue = queue.Queue()
            
            # Create the session object up-front (before the reader thread starts) so the
            # reader thread can flag completion on it and reap it if it is never polled.
            session = terminal_session_with_process_tracking(
                process_id=session_id,
                process=process,
                accumulated_output_buffer="",
                newly_available_output_since_last_read="",
                command_execution_has_completed=False,
                session_creation_timestamp=datetime.now(),
                output_reading_thread=None,
                output_queue=output_queue,
                last_exit_code=None,
                owner_user=owner_user
            )
            
            # Register the active session (locked + bounded so completed-but-unpolled
            # sessions cannot pile up unbounded)
            self._register_active_terminal_session(session_id, session)
            
            def output_reader_thread():
                """Background thread to read process output"""
                try:
                    for line in iter(process.stdout.readline, ''):
                        if line:
                            output_queue.put(('output', line))
                        if process.poll() is not None:
                            break
                    
                    # Get any remaining output
                    remaining_output = process.stdout.read()
                    if remaining_output:
                        output_queue.put(('output', remaining_output))
                    
                    # Signal completion
                    output_queue.put(('completed', process.returncode))
                    
                except Exception as e:
                    output_queue.put(('error', str(e)))
                finally:
                    # Flag completion so a session whose output is never polled is still
                    # reaped (releasing its process/pipe handles) rather than leaking forever.
                    self._mark_terminal_session_completed(session_id)
                    time.sleep(self.completed_session_reap_grace_seconds)
                    self._reap_completed_terminal_session_if_still_active(session_id)
            
            # Start output reading thread
            reader_thread = threading.Thread(target=output_reader_thread, daemon=True)
            session.output_reading_thread = reader_thread
            reader_thread.start()
            
            # Collect initial output for specified timeout
            initial_output = ""
            timeout_seconds = timeout_milliseconds / 1000.0
            start_time = time.time()
            
            while time.time() - start_time < timeout_seconds:
                try:
                    # Check for new output with short timeout
                    item_type, content = output_queue.get(timeout=0.1)
                    
                    if item_type == 'output':
                        initial_output += content
                        session.accumulated_output_buffer += content
                        session.newly_available_output_since_last_read += content
                    elif item_type == 'completed':
                        session.command_execution_has_completed = True
                        session.last_exit_code = content
                        break
                    elif item_type == 'error':
                        return command_execution_result_with_background_support(
                            process_id=session_id,
                            initial_output_text=initial_output,
                            command_is_still_running_in_background=False,
                            error_message=f"Process error: {content}"
                        )
                        
                except queue.Empty:
                    # Check if process is still running
                    if process.poll() is not None:
                        session.command_execution_has_completed = True
                        session.last_exit_code = process.returncode
                        break
                    continue
            
            # Check final state
            is_still_running = not session.command_execution_has_completed
            
            MCPLogger.log(TOOL_LOG_NAME, f"Command executed, PID: {session_id}, initial output length: {len(initial_output)}, still running: {is_still_running}")
            
            return command_execution_result_with_background_support(
                process_id=session_id,
                initial_output_text=initial_output,
                command_is_still_running_in_background=is_still_running
            )
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error executing command: {e}")
            return command_execution_result_with_background_support(
                process_id=-1,
                initial_output_text="",
                command_is_still_running_in_background=False,
                error_message=f"Failed to execute command: {e}"
            )
    
    def read_new_output_from_session_with_timeout(
        self, 
        session_id: int, 
        timeout_milliseconds: int = 5000
    ) -> Tuple[str, bool]:
        """Read new output from a session, returns (output, timeout_reached)"""
        
        with self._session_state_lock:
            session = self.active_terminal_sessions.get(session_id)
            completed = None if session else self.completed_session_history.get(session_id)
        if not session:
            # Check completed sessions
            if completed:
                runtime = (completed.session_end_time - completed.session_start_time).total_seconds()
                return f"Process completed with exit code {completed.final_exit_code}\nRuntime: {runtime:.2f}s\nFinal output:\n{completed.complete_output_text}", False
            return f"No session found for ID {session_id}", False
        
        # Return immediately if we already have new output
        if session.newly_available_output_since_last_read:
            output = session.newly_available_output_since_last_read
            session.newly_available_output_since_last_read = ""
            return output, False
        
        # Wait for new output
        timeout_seconds = timeout_milliseconds / 1000.0
        start_time = time.time()
        new_output = ""
        
        while time.time() - start_time < timeout_seconds:
            try:
                item_type, content = session.output_queue.get(timeout=0.1)
                
                if item_type == 'output':
                    new_output += content
                    session.accumulated_output_buffer += content
                elif item_type == 'completed':
                    session.command_execution_has_completed = True
                    session.last_exit_code = content
                    
                    # Move to completed sessions
                    self._move_session_to_completed(session_id)
                    
                    if new_output:
                        return new_output, False
                    else:
                        return f"Process completed with exit code {content}", False
                elif item_type == 'error':
                    return f"Process error: {content}", False
                    
                # Return immediately if we got some output
                if new_output:
                    return new_output, False
                    
            except queue.Empty:
                # Check if process completed
                if session.command_execution_has_completed:
                    if new_output:
                        return new_output, False
                    else:
                        return f"Process completed with exit code {session.last_exit_code}", False
                continue
        
        # Timeout reached
        return new_output if new_output else "No new output available", True
    
    def force_terminate_session_with_cleanup(self, session_id: int) -> bool:
        """Force terminate a session and clean up resources"""
        
        with self._session_state_lock:
            session = self.active_terminal_sessions.get(session_id)
        if not session:
            return False
        
        try:
            # Terminate the process
            if platform.system() == "Windows":
                # Windows process termination
                import signal
                try:
                    # Try graceful termination first
                    session.process.send_signal(signal.CTRL_BREAK_EVENT)
                    
                    # Wait a bit for graceful shutdown
                    try:
                        session.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Force kill if graceful shutdown failed
                        session.process.kill()
                        session.process.wait()
                        
                except (OSError, AttributeError):
                    # Fallback to kill
                    session.process.kill()
                    session.process.wait()
            else:
                # Unix process termination
                try:
                    # Try SIGTERM first
                    session.process.terminate()
                    
                    # Wait for graceful shutdown
                    try:
                        session.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Force kill with SIGKILL
                        session.process.kill()
                        session.process.wait()
                        
                except (OSError, AttributeError):
                    # Fallback to kill
                    session.process.kill()
                    session.process.wait()
            
            # Move to completed sessions
            self._move_session_to_completed(session_id)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Successfully terminated session {session_id}")
            return True
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error terminating session {session_id}: {e}")
            return False
    
    def session_is_accessible_by_user(self, session_id: int, requesting_user: Optional[str]) -> bool:
        """B6: return True only if a session with this id exists (active or completed) AND is
        owned by requesting_user. Unknown ids return False, so an unauthorized caller cannot
        distinguish "not yours" from "does not exist"."""
        with self._session_state_lock:
            session = self.active_terminal_sessions.get(session_id)
            owner = session.owner_user if session is not None else None
            if session is None:
                completed = self.completed_session_history.get(session_id)
                if completed is None:
                    return False
                owner = completed.owner_user
        return owner == requesting_user

    def get_list_of_all_active_sessions_with_status(self, requesting_user: Optional[str] = None) -> List[Dict[str, any]]:
        """Get list of active sessions with their status.

        B6: only sessions owned by requesting_user are returned (None owner matches None user
        when auth is not configured)."""
        
        current_time = datetime.now()
        active_sessions = []
        
        with self._session_state_lock:
            active_sessions_snapshot = list(self.active_terminal_sessions.items())
        for session_id, session in active_sessions_snapshot:
            if session.owner_user != requesting_user:
                continue
            runtime_seconds = (current_time - session.session_creation_timestamp).total_seconds()
            
            active_sessions.append({
                "session_id": session_id,
                "is_completed": session.command_execution_has_completed,
                "runtime_seconds": round(runtime_seconds, 2),
                "has_new_output": len(session.newly_available_output_since_last_read) > 0,
                "total_output_length": len(session.accumulated_output_buffer)
            })
        
        return active_sessions
    
    def _register_active_terminal_session(self, session_id: int, session: terminal_session_with_process_tracking):
        """Insert a new active session under the shared-state lock, reaping oldest
        already-completed sessions first if the active dict has grown past its bound."""
        with self._session_state_lock:
            self.active_terminal_sessions[session_id] = session
            if len(self.active_terminal_sessions) > self.maximum_active_sessions_to_retain:
                self._reap_oldest_completed_active_sessions_locked()

    def _mark_terminal_session_completed(self, session_id: int):
        """Flag a session as completed and capture its exit code (thread-safe). Called from
        the reader thread when the process output stream ends."""
        with self._session_state_lock:
            session = self.active_terminal_sessions.get(session_id)
            if session is None:
                return
            session.command_execution_has_completed = True
            if session.last_exit_code is None:
                session.last_exit_code = session.process.poll()

    def _reap_completed_terminal_session_if_still_active(self, session_id: int):
        """Move a completed session out of the active dict if it is still there, so a command
        whose output is never polled does not leak its process/handles forever."""
        with self._session_state_lock:
            session = self.active_terminal_sessions.get(session_id)
            if session is not None and session.command_execution_has_completed:
                self._move_session_to_completed_locked(session_id)

    def _reap_oldest_completed_active_sessions_locked(self):
        """Assumes the shared-state lock is held. Move already-completed active sessions
        (oldest first) into the completed history until the active dict is back within its
        bound. Still-running sessions are never reaped (their processes are alive)."""
        completed_active_session_ids_oldest_first = [
            candidate_session_id
            for candidate_session_id, tracked_session in sorted(
                self.active_terminal_sessions.items(),
                key=lambda id_and_session: id_and_session[1].session_creation_timestamp
            )
            if tracked_session.command_execution_has_completed
        ]
        for session_id_to_reap in completed_active_session_ids_oldest_first:
            if len(self.active_terminal_sessions) <= self.maximum_active_sessions_to_retain:
                break
            self._move_session_to_completed_locked(session_id_to_reap)

    def _move_session_to_completed(self, session_id: int):
        """Move a session from active to completed (acquires the shared-state lock)."""
        with self._session_state_lock:
            self._move_session_to_completed_locked(session_id)

    def _move_session_to_completed_locked(self, session_id: int):
        """Assumes the shared-state lock is held. Move a session from active to completed."""
        
        session = self.active_terminal_sessions.get(session_id)
        if not session:
            return
        
        completed_session = completed_terminal_session_with_full_history(
            process_id=session_id,
            complete_output_text=session.accumulated_output_buffer,
            final_exit_code=session.last_exit_code,
            session_start_time=session.session_creation_timestamp,
            session_end_time=datetime.now(),
            owner_user=session.owner_user
        )
        
        self.completed_session_history[session_id] = completed_session
        
        # Keep only the most recent completed sessions
        if len(self.completed_session_history) > self.maximum_completed_sessions_to_retain:
            oldest_session_id = min(self.completed_session_history.keys())
            del self.completed_session_history[oldest_session_id]
        
        # Remove from active sessions
        del self.active_terminal_sessions[session_id]

# Global terminal manager instance
_global_terminal_session_manager = comprehensive_terminal_session_manager_with_background_support()

@dataclass
class extracted_ui_element_info_with_full_details:
    """Complete information about a UI element including all accessible properties and spatial data"""
    control_type: str
    automation_id: str
    name: str
    class_name: str
    local_bounding_rectangle_left: int
    local_bounding_rectangle_top: int
    local_bounding_rectangle_right: int
    local_bounding_rectangle_bottom: int
    local_bounding_rectangle_width: int
    local_bounding_rectangle_height: int
    control_value_text: str
    is_enabled: bool
    is_visible: bool
    has_keyboard_focus: bool
    process_id: int
    native_window_handle: int
    accessibility_help_text: str
    accessibility_description: str
    item_status: str
    framework_id: str
    tree_depth_level: int
    parent_automation_id: str
    parent_name: str
    children_count: int
    access_key: str
    accelerator_key: str


class comprehensive_ui_tree_walker_with_text_extraction:
    """Comprehensive UI automation walker that extracts all text and structural data from Windows UI elements"""
    
    def __init__(self):
        self.extracted_elements_with_complete_data: List[extracted_ui_element_info_with_full_details] = []
        self.total_elements_discovered_count = 0
        self.maximum_tree_traversal_depth = 40  # Increased for Chrome/Electron apps like Signal
        self.include_all_chrome_elements = True  # Flag to include more Chrome elements
        self.is_electron_app = False  # Flag to track if we're scanning an Electron app
        # Wall-clock budget so a huge or hung UIAutomation scan cannot block the worker
        # (and therefore the connection) thread indefinitely.
        self.scan_wall_clock_budget_seconds = 30.0
        self.scan_deadline_monotonic = None  # set when a scan starts
        self.scan_budget_was_exceeded = False
        
    def set_electron_mode(self, is_electron: bool):
        """Enable special handling for Electron apps"""
        self.is_electron_app = is_electron
        if is_electron:
            self.maximum_tree_traversal_depth = 50
            self.include_all_chrome_elements = True
            MCPLogger.log(TOOL_LOG_NAME, "Electron mode enabled - using deeper scanning")

    def _has_scan_wall_clock_budget_expired(self) -> bool:
        """Return True once the scan wall-clock budget has elapsed, so a huge/hung scan stops
        early instead of blocking the worker (and connection) thread forever."""
        if self.scan_deadline_monotonic is None:
            return False
        if time.monotonic() <= self.scan_deadline_monotonic:
            return False
        if not self.scan_budget_was_exceeded:
            self.scan_budget_was_exceeded = True
            MCPLogger.log(TOOL_LOG_NAME, f"UI scan wall-clock budget ({self.scan_wall_clock_budget_seconds}s) exceeded after {self.total_elements_discovered_count} elements - stopping scan early")
        return True
    
    def is_useful_ui_element_worth_extracting(self, element_info: extracted_ui_element_info_with_full_details) -> bool:
        """Enhanced filtering to determine if a UI element contains useful information, especially for Chrome/Electron apps"""
        
        # For Electron apps, be much more aggressive in including elements
        if self.is_electron_app:
            # Include almost everything visible in Electron apps
            if element_info.is_visible:
                return True
        
        # Always include elements with text content
        if element_info.control_value_text and element_info.control_value_text.strip():
            return True
            
        # Always include elements with meaningful names
        if element_info.name and element_info.name.strip() and len(element_info.name.strip()) > 1:
            return True
            
        # Always include elements with automation IDs
        if element_info.automation_id and element_info.automation_id.strip():
            return True
        
        # Include interactive control types (buttons, links, inputs, etc.)
        interactive_control_types = {
            'ButtonControl', 'LinkControl', 'EditControl', 'ComboBoxControl',
            'CheckBoxControl', 'RadioButtonControl', 'SliderControl', 'SpinnerControl',
            'TabItemControl', 'MenuItemControl', 'TreeItemControl', 'ListItemControl',
            'HyperlinkControl', 'SplitButtonControl', 'ToggleButtonControl'
        }
        if element_info.control_type in interactive_control_types:
            return True
            
        # Include structural elements that might contain useful info
        structural_control_types = {
            'GroupControl', 'PaneControl', 'ToolBarControl', 'MenuBarControl',
            'StatusBarControl', 'TabControl', 'TreeControl', 'ListControl',
            'DataGridControl', 'TableControl'
        }
        if element_info.control_type in structural_control_types:
            return True
            
        # Enhanced Chrome/Electron detection - check for both framework and class name
        is_chrome_or_electron = (
            element_info.framework_id == "Chrome" or 
            "Chrome_WidgetWin" in element_info.class_name or
            "Chrome_RenderWidgetHostHWND" in element_info.class_name
        )
        
        # For Chrome/Electron apps, include more element types
        if self.include_all_chrome_elements and is_chrome_or_electron:
            chrome_useful_types = {
                'TextControl', 'DocumentControl', 'CustomControl', 'ImageControl',
                'StaticTextControl', 'WindowControl', 'GenericControl'
            }
            if element_info.control_type in chrome_useful_types:
                return True
                
        # Include elements with accessibility information
        if element_info.accessibility_help_text or element_info.accessibility_description:
            return True
            
        # Include elements that have focus capability
        if element_info.has_keyboard_focus:
            return True
            
        # Include elements with access keys or accelerator keys (useful for automation)
        if element_info.access_key or element_info.accelerator_key:
            return True
            
        return False
    
    def extract_detailed_chrome_element_info(self, ui_control_element) -> str:
        """Extract additional detailed information specifically for Chrome/Electron elements"""
        detailed_info_list = []
        
        try:
            # Try to get additional Chrome-specific patterns
            if hasattr(ui_control_element, 'GetCurrentPropertyValue'):
                try:
                    # Get more detailed properties
                    class_name = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.ClassNameProperty)
                    if class_name:
                        detailed_info_list.append(f"ClassName: {class_name}")
                        
                    local_name = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.LocalizedControlTypeProperty)
                    if local_name:
                        detailed_info_list.append(f"LocalizedType: {local_name}")
                        
                    access_key = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.AccessKeyProperty)
                    if access_key:
                        detailed_info_list.append(f"AccessKey: {access_key}")
                        
                    accelerator_key = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.AcceleratorKeyProperty)
                    if accelerator_key:
                        detailed_info_list.append(f"AcceleratorKey: {accelerator_key}")
                        
                    # Additional Electron-specific properties
                    try:
                        description = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.FullDescriptionProperty)
                        if description:
                            detailed_info_list.append(f"FullDescription: {description}")
                    except:
                        pass
                        
                    try:
                        landmark_type = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.LandmarkTypeProperty)
                        if landmark_type:
                            detailed_info_list.append(f"LandmarkType: {landmark_type}")
                    except:
                        pass
                        
                except Exception as prop_error:
                    # Continue even if some properties fail
                    pass
            
            # Try to get role information (important for web content)
            try:
                if hasattr(ui_control_element, 'AriaRole'):
                    aria_role = getattr(ui_control_element, 'AriaRole', '')
                    if aria_role:
                        detailed_info_list.append(f"AriaRole: {aria_role}")
            except:
                pass
                
            # Try to get state information
            try:
                if hasattr(ui_control_element, 'GetTogglePattern'):
                    toggle_pattern = ui_control_element.GetTogglePattern()
                    if toggle_pattern:
                        toggle_state = toggle_pattern.ToggleState
                        detailed_info_list.append(f"ToggleState: {toggle_state}")
            except:
                pass
                
            # Try to get selection information
            try:
                if hasattr(ui_control_element, 'GetSelectionItemPattern'):
                    selection_pattern = ui_control_element.GetSelectionItemPattern()
                    if selection_pattern:
                        is_selected = selection_pattern.IsSelected
                        detailed_info_list.append(f"IsSelected: {is_selected}")
            except:
                pass
                
            # Try to get invoke pattern (for clickable elements)
            try:
                if hasattr(ui_control_element, 'GetInvokePattern'):
                    invoke_pattern = ui_control_element.GetInvokePattern()
                    if invoke_pattern:
                        detailed_info_list.append(f"IsInvokable: True")
            except:
                pass
        
        except Exception:
            pass
            
        return " | ".join(detailed_info_list) if detailed_info_list else ""

    def extract_all_text_content_from_ui_element(self, ui_element) -> str:
        """Extract comprehensive text content from UI element using multiple patterns and sources"""
        text_content_parts = []
        
        try:
            # Extract from ValuePattern (input fields, sliders, progress bars)
            try:
                value_pattern = ui_element.GetValuePattern()
                if value_pattern and value_pattern.Value:
                    text_content_parts.append(f"ValuePattern: {value_pattern.Value}")
            except:
                pass
            
            # Extract from TextPattern (rich text, documents, text areas)
            try:
                text_pattern = ui_element.GetTextPattern()
                if text_pattern:
                    document_range = text_pattern.DocumentRange
                    if document_range and document_range.GetText(-1):
                        text_content = document_range.GetText(-1).strip()
                        if text_content:
                            text_content_parts.append(f"TextPattern: {text_content}")
            except:
                pass
            
            # Extract from LegacyIAccessible (older accessibility API)
            try:
                legacy_pattern = ui_element.GetLegacyIAccessiblePattern()
                if legacy_pattern and legacy_pattern.Value:
                    text_content_parts.append(f"LegacyValue: {legacy_pattern.Value}")
                if legacy_pattern and legacy_pattern.Name:
                    text_content_parts.append(f"LegacyName: {legacy_pattern.Name}")
                if legacy_pattern and legacy_pattern.Description:
                    text_content_parts.append(f"LegacyDescription: {legacy_pattern.Description}")
            except:
                pass
            
            # Extract basic element properties
            if ui_element.Name:
                text_content_parts.append(f"Name: {ui_element.Name}")
            
            if hasattr(ui_element, 'HelpText') and ui_element.HelpText:
                text_content_parts.append(f"HelpText: {ui_element.HelpText}")
            
            if hasattr(ui_element, 'ItemStatus') and ui_element.ItemStatus:
                text_content_parts.append(f"ItemStatus: {ui_element.ItemStatus}")
            
            # Extract from RangeValue pattern (sliders, scroll bars)
            try:
                range_pattern = ui_element.GetRangeValuePattern()
                if range_pattern:
                    text_content_parts.append(f"RangeValue: {range_pattern.Value} (min: {range_pattern.Minimum}, max: {range_pattern.Maximum})")
            except:
                pass
            
            # Extract from SelectionItem pattern
            try:
                selection_pattern = ui_element.GetSelectionItemPattern()
                if selection_pattern:
                    text_content_parts.append(f"SelectionState: {'Selected' if selection_pattern.IsSelected else 'NotSelected'}")
            except:
                pass
            
            # Extract from Toggle pattern (checkboxes, radio buttons)
            try:
                toggle_pattern = ui_element.GetTogglePattern()
                if toggle_pattern:
                    toggle_state = toggle_pattern.ToggleState
                    text_content_parts.append(f"ToggleState: {toggle_state}")
            except:
                pass
            
            # Extract from ExpandCollapse pattern (tree items, menus)
            try:
                expand_pattern = ui_element.GetExpandCollapsePattern()
                if expand_pattern:
                    expand_state = expand_pattern.ExpandCollapseState
                    text_content_parts.append(f"ExpandState: {expand_state}")
            except:
                pass
            
            # For Chrome-specific elements, extract additional web-related info
            if self.include_all_chrome_elements and ui_element.FrameworkId == "Chrome":
                chrome_details = self.extract_detailed_chrome_element_info(ui_element)
                if chrome_details:
                    text_content_parts.append(f"Chrome_Details: {chrome_details}")
            
            # Also check for Electron apps by class name
            try:
                class_name = getattr(ui_element, 'ClassName', '')
                if self.include_all_chrome_elements and ("Chrome_WidgetWin" in class_name or "Chrome_RenderWidgetHostHWND" in class_name):
                    chrome_details = self.extract_detailed_chrome_element_info(ui_element)
                    if chrome_details:
                        text_content_parts.append(f"Electron_Details: {chrome_details}")
            except:
                pass
            
            # Join all text content with separators
            if text_content_parts:
                return " | ".join(text_content_parts)
            else:
                # Fallback to basic element info
                return f"ControlType: {ui_element.ControlTypeName} | AutomationId: {getattr(ui_element, 'AutomationId', 'N/A')}"
                
        except Exception as text_extraction_error:
            return f"TextExtractionError: {str(text_extraction_error)}"
    
    def extract_complete_element_information_with_all_properties(self, ui_control_element, current_tree_depth: int = 0, parent_control=None) -> extracted_ui_element_info_with_full_details:
        """Extract comprehensive information from a UI element including all properties and spatial data"""
        
        # Get bounding rectangle information
        bounding_rect = ui_control_element.BoundingRectangle
        
        # Get parent information
        parent_automation_id = ""
        parent_name = ""
        if parent_control:
            parent_automation_id = getattr(parent_control, 'AutomationId', '')
            parent_name = getattr(parent_control, 'Name', '')
        
        # Count children
        children_count = 0
        try:
            children = ui_control_element.GetChildren()
            children_count = len(children) if children else 0
        except:
            pass
        
        # Extract all text content
        extracted_text_value = self.extract_all_text_content_from_ui_element(ui_control_element)
        
        # Extract accelerator keys and access keys for all elements
        access_key = ""
        accelerator_key = ""
        try:
            if hasattr(ui_control_element, 'GetCurrentPropertyValue'):
                try:
                    access_key_value = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.AccessKeyProperty)
                    if access_key_value:
                        access_key = str(access_key_value)
                except:
                    pass
                    
                try:
                    accelerator_key_value = ui_control_element.GetCurrentPropertyValue(auto.PropertyId.AcceleratorKeyProperty)
                    if accelerator_key_value:
                        accelerator_key = str(accelerator_key_value)
                except:
                    pass
        except:
            pass
        
        # Get additional properties with safe access
        def safe_get_property(obj, prop_name, default=""):
            try:
                return str(getattr(obj, prop_name, default))
            except:
                return default
        
        return extracted_ui_element_info_with_full_details(
            control_type=safe_get_property(ui_control_element, 'ControlTypeName'),
            automation_id=safe_get_property(ui_control_element, 'AutomationId'),
            name=safe_get_property(ui_control_element, 'Name'),
            class_name=safe_get_property(ui_control_element, 'ClassName'),
            local_bounding_rectangle_left=bounding_rect.left,
            local_bounding_rectangle_top=bounding_rect.top,
            local_bounding_rectangle_right=bounding_rect.right,
            local_bounding_rectangle_bottom=bounding_rect.bottom,
            local_bounding_rectangle_width=bounding_rect.width(),
            local_bounding_rectangle_height=bounding_rect.height(),
            control_value_text=extracted_text_value,
            is_enabled=getattr(ui_control_element, 'IsEnabled', False),
            is_visible=getattr(ui_control_element, 'IsOffscreen', True) == False,  # IsOffscreen is inverted
            has_keyboard_focus=getattr(ui_control_element, 'HasKeyboardFocus', False),
            process_id=getattr(ui_control_element, 'ProcessId', 0),
            native_window_handle=getattr(ui_control_element, 'NativeWindowHandle', 0),
            accessibility_help_text=safe_get_property(ui_control_element, 'HelpText'),
            accessibility_description=safe_get_property(ui_control_element, 'AriaProperties'),
            item_status=safe_get_property(ui_control_element, 'ItemStatus'),
            framework_id=safe_get_property(ui_control_element, 'FrameworkId'),
            tree_depth_level=current_tree_depth,
            parent_automation_id=parent_automation_id,
            parent_name=parent_name,
            children_count=children_count,
            access_key=access_key,
            accelerator_key=accelerator_key
        )
    
    def recursively_walk_ui_tree_and_extract_all_text_data(self, starting_ui_control, current_depth: int = 0, parent_control=None):
        """Recursively walk through the UI tree and extract all text data from every element"""
        
        if current_depth > self.maximum_tree_traversal_depth:
            return
        
        # Stop early if the scan has used its whole wall-clock budget (huge/hung tree)
        if self._has_scan_wall_clock_budget_expired():
            return
        
        try:
            # Extract information from current element
            element_info = self.extract_complete_element_information_with_all_properties(
                starting_ui_control, current_depth, parent_control
            )
            
            # Use enhanced filtering for useful elements
            if self.is_useful_ui_element_worth_extracting(element_info) and element_info.is_visible:
                self.extracted_elements_with_complete_data.append(element_info)
                self.total_elements_discovered_count += 1
                
                # Print progress for large scans with more detail
                if self.total_elements_discovered_count % 50 == 0:
                    MCPLogger.log(TOOL_LOG_NAME, f"Processed {self.total_elements_discovered_count} UI elements... (depth {current_depth}, type: {element_info.control_type})")
            
            # Recursively process children - be more aggressive in Chrome apps
            try:
                children = starting_ui_control.GetChildren()
                if children:
                    for child_control in children:
                        self.recursively_walk_ui_tree_and_extract_all_text_data(
                            child_control, current_depth + 1, starting_ui_control
                        )
            except Exception as child_error:
                # Continue processing even if some children fail
                pass
                
        except Exception as element_error:
            # Continue processing even if some elements fail
            pass
    
    def scan_electron_app_enhanced(self, target_window):
        """Enhanced scanning specifically for Electron applications using multiple strategies"""
        MCPLogger.log(TOOL_LOG_NAME, "Starting enhanced Electron app scanning...")
        
        try:
            # Strategy 1: Try to find Chrome renderer processes
            renderer_windows = []
            def find_chrome_renderers(hwnd, _):
                try:
                    class_name = win32gui.GetClassName(hwnd)
                    window_text = win32gui.GetWindowText(hwnd)
                    if "Chrome_RenderWidgetHostHWND" in class_name:
                        renderer_windows.append((hwnd, window_text, class_name))
                except:
                    pass
                return True
            
            win32gui.EnumChildWindows(target_window.Handle, find_chrome_renderers, None)
            MCPLogger.log(TOOL_LOG_NAME, f"Found {len(renderer_windows)} Chrome renderer windows")
            
            # Strategy 2: Scan each renderer window
            for renderer_hwnd, renderer_text, renderer_class in renderer_windows:
                if self._has_scan_wall_clock_budget_expired():
                    break
                try:
                    renderer_control = auto.ControlFromHandle(renderer_hwnd)
                    if renderer_control:
                        MCPLogger.log(TOOL_LOG_NAME, f"Scanning renderer: {renderer_class}")
                        # Walk the renderer subtree so its elements are actually stored; the
                        # previous call passed an unsupported current_depth kwarg (this method
                        # takes only one argument), so it raised and the branch was dead.
                        self.recursively_walk_ui_tree_and_extract_all_text_data(renderer_control)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error scanning renderer {renderer_hwnd}: {e}")
            
            # Strategy 3: Look for accessibility interfaces
            try:
                # Check if the app supports IAccessible
                from comtypes import client
                try:
                    accessible = client.GetObject(target_window.Handle)
                    if accessible:
                        MCPLogger.log(TOOL_LOG_NAME, "Found IAccessible interface - attempting extraction")
                        # Try to get children through IAccessible
                        child_count = accessible.accChildCount
                        MCPLogger.log(TOOL_LOG_NAME, f"IAccessible reports {child_count} children")
                except:
                    pass
            except ImportError:
                pass
            
            # Strategy 4: Deep traversal with different search criteria
            all_descendants = target_window.GetDescendants()
            MCPLogger.log(TOOL_LOG_NAME, f"Found {len(all_descendants)} total descendants via GetDescendants")
            
            for desc in all_descendants:
                if self._has_scan_wall_clock_budget_expired():
                    break
                try:
                    if hasattr(desc, 'FrameworkId') and desc.FrameworkId == "Chrome":
                        # desc is already an individual (flattened) descendant, so extract and
                        # store this one element. The previous call passed an unsupported
                        # current_depth kwarg and discarded the result, so this branch was dead.
                        element_info = self.extract_complete_element_information_with_all_properties(desc)
                        if self.is_useful_ui_element_worth_extracting(element_info) and element_info.is_visible:
                            self.extracted_elements_with_complete_data.append(element_info)
                            self.total_elements_discovered_count += 1
                except:
                    continue
                    
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Enhanced Electron scanning error: {e}")
    
    def scan_specific_window_and_extract_text_data(self, window_title_pattern: Optional[str] = None, hwnd_str: Optional[str] = None) -> Dict[str, any]:
        """Scan a specific window and extract all text data from its UI elements.
        
        Args:
            window_title_pattern: Window title or partial title pattern to scan (optional if hwnd_str provided)
            hwnd_str: Window handle in hexadecimal format (optional if window_title_pattern provided)
        """
        if not window_title_pattern and not hwnd_str:
            return {"error": "Either window_title_pattern or hwnd_str must be provided", "extracted_ui_elements": []}
        
        if hwnd_str:
            MCPLogger.log(TOOL_LOG_NAME, f"Scanning window with hwnd: '{hwnd_str}'")
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Scanning window with title pattern: '{window_title_pattern}'")
        
        try:
            # Initialize COM for UI automation
            pythoncom.CoInitialize()
            
            target_window = None
            
            if hwnd_str:
                # Find window by handle
                try:
                    # Convert hex string to integer
                    if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
                        hwnd = int(hwnd_str, 16)
                    else:
                        hwnd = int(hwnd_str, 16)  # Assume hex even without 0x prefix
                    
                    # Validate the window handle
                    if not win32gui.IsWindow(hwnd):
                        return {"error": f"Window handle {hwnd_str} does not exist or is invalid", "extracted_ui_elements": []}
                    
                    # Get class name to detect Electron apps
                    class_name = win32gui.GetClassName(hwnd)
                    window_title = win32gui.GetWindowText(hwnd)
                    
                    # Special handling for Electron apps
                    if "Chrome_WidgetWin" in class_name or "Chrome_RenderWidgetHostHWND" in class_name:
                        MCPLogger.log(TOOL_LOG_NAME, f"Detected Electron/Chrome app: {class_name} - using enhanced scanning")
                        # Enable Electron mode for enhanced scanning
                        self.set_electron_mode(True)
                    
                    # Create WindowControl from handle
                    target_window = auto.ControlFromHandle(hwnd)
                    if not target_window or not hasattr(target_window, 'ControlTypeName'):
                        return {"error": f"Could not create automation control from handle {hwnd_str}", "extracted_ui_elements": []}
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Found window by handle: {getattr(target_window, 'Name', 'Unknown')}")
                    
                except ValueError:
                    return {"error": f"Invalid window handle format: '{hwnd_str}'. Expected hexadecimal format like '0x00020828'", "extracted_ui_elements": []}
                except Exception as e:
                    return {"error": f"Error finding window by handle {hwnd_str}: {str(e)}", "extracted_ui_elements": []}
            else:
                # Find window by title with multiple fallback strategies
                
                # Strategy 1: Exact match
                target_window = auto.WindowControl(searchDepth=1, Name=window_title_pattern)
                if target_window.Exists():
                    MCPLogger.log(TOOL_LOG_NAME, f"Found window by exact match: {target_window.Name}")
                else:
                    # Strategy 2: Substring match
                    target_window = auto.WindowControl(searchDepth=1, SubName=window_title_pattern)
                    if target_window.Exists():
                        MCPLogger.log(TOOL_LOG_NAME, f"Found window by substring match: {target_window.Name}")
                    else:
                        # Strategy 3: Manual search through all windows with partial matching
                        MCPLogger.log(TOOL_LOG_NAME, f"Exact and substring search failed, trying manual search...")
                        
                        # Get all top-level windows and search manually
                        found_hwnd = None
                        found_title = None
                        windows_checked = 0
                        
                        def check_window(hwnd, _):
                            nonlocal found_hwnd, found_title, windows_checked
                            if found_hwnd:  # Already found
                                return False
                                
                            try:
                                if win32gui.IsWindowVisible(hwnd):
                                    window_title = win32gui.GetWindowText(hwnd)
                                    windows_checked += 1
                                    
                                    if window_title and window_title_pattern.lower() in window_title.lower():
                                        # Found a partial match - just store the hwnd, don't create controls yet
                                        found_hwnd = hwnd
                                        found_title = window_title
                                        MCPLogger.log(TOOL_LOG_NAME, f"Found window by manual search: '{window_title}' (pattern: '{window_title_pattern}')")
                                        return False  # Stop enumeration
                            except Exception:
                                pass  # Continue searching
                            return True
                        
                        try:
                            win32gui.EnumWindows(check_window, None)
                            MCPLogger.log(TOOL_LOG_NAME, f"Manual search checked {windows_checked} windows")
                        except Exception as enum_error:
                            MCPLogger.log(TOOL_LOG_NAME, f"Error during window enumeration: {enum_error}")
                            # Continue with pattern variations if enumeration fails
                        
                        # Now create the automation control outside of enumeration
                        if found_hwnd:
                            try:
                                # Check if it's an Electron app
                                class_name = win32gui.GetClassName(found_hwnd)
                                if "Chrome_WidgetWin" in class_name or "Chrome_RenderWidgetHostHWND" in class_name:
                                    MCPLogger.log(TOOL_LOG_NAME, f"Detected Electron/Chrome app: {class_name} - using enhanced scanning")
                                    self.set_electron_mode(True)
                                
                                target_window = auto.ControlFromHandle(found_hwnd)
                                if not target_window or not hasattr(target_window, 'ControlTypeName'):
                                    MCPLogger.log(TOOL_LOG_NAME, f"Could not create automation control from found handle {found_hwnd}")
                                    target_window = None
                            except Exception as control_error:
                                MCPLogger.log(TOOL_LOG_NAME, f"Error creating control from handle {found_hwnd}: {control_error}")
                                target_window = None
                        
                        # Strategy 4: Try pattern variations if manual search failed or control creation failed
                        if not target_window or not target_window.Exists():
                            MCPLogger.log(TOOL_LOG_NAME, f"Manual search failed or control creation failed, trying pattern variations...")
                            
                            # Try common variations
                            variations = [
                                window_title_pattern.strip(),  # Remove whitespace
                                window_title_pattern.split(' - ')[0],  # Remove everything after " - "
                                window_title_pattern.split(' — ')[0],  # Remove everything after " — "
                                window_title_pattern.split(' | ')[0],  # Remove everything after " | "
                                window_title_pattern.split('(')[0].strip(),  # Remove everything after "("
                            ]
                            
                            for variation in variations:
                                if not variation or variation == window_title_pattern:
                                    continue
                                    
                                test_window = auto.WindowControl(searchDepth=1, SubName=variation)
                                if test_window.Exists():
                                    target_window = test_window
                                    MCPLogger.log(TOOL_LOG_NAME, f"Found window by pattern variation '{variation}': {target_window.Name}")
                                    break
                        
                        # Final check
                        if not target_window or not target_window.Exists():
                            return {"error": f"Window not found with title pattern: '{window_title_pattern}'. Checked {windows_checked} windows.", "extracted_ui_elements": []}
            
            # Start the wall-clock budget now that we have a target window, so neither the
            # enhanced Electron scan nor the regular tree walk can block indefinitely.
            self.scan_deadline_monotonic = time.monotonic() + self.scan_wall_clock_budget_seconds
            
            # Special handling for Electron apps - run enhanced scanning first
            if self.is_electron_app:
                MCPLogger.log(TOOL_LOG_NAME, "Running enhanced Electron app scanning...")
                self.scan_electron_app_enhanced(target_window)
            
            # Walk through the window's UI elements (regular scanning)
            self.recursively_walk_ui_tree_and_extract_all_text_data(target_window)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Window scan completed! Found {self.total_elements_discovered_count} UI elements with text data.")
            
            return {
                "window_info": {
                    "title": getattr(target_window, 'Name', 'Unknown'),
                    "class_name": getattr(target_window, 'ClassName', 'Unknown'),
                    "process_id": getattr(target_window, 'ProcessId', 0),
                    "hwnd": f"0x{getattr(target_window, 'NativeWindowHandle', 0):08X}" if hasattr(target_window, 'NativeWindowHandle') else 'Unknown'
                },
                "scan_summary": {
                    "total_elements_found": self.total_elements_discovered_count,
                    "scan_timestamp": time.time()
                },
                "extracted_ui_elements": [asdict(element) for element in self.extracted_elements_with_complete_data]
            }
            
        except Exception as scan_error:
            MCPLogger.log(TOOL_LOG_NAME, f"Error scanning window: {scan_error}")
            return {"error": str(scan_error), "extracted_ui_elements": []}
        finally:
            # Clean up COM
            try:
                pythoncom.CoUninitialize()
            except:
                pass

    def find_all_buttons_and_clickable_elements_with_coordinates(self) -> List[Dict[str, any]]:
        """Extract all button and clickable elements with their exact coordinates for automation purposes"""
        clickable_elements = []
        
        for element in self.extracted_elements_with_complete_data:
            is_clickable = (
                'Button' in element.control_type or
                'Link' in element.control_type or
                'MenuItem' in element.control_type or
                'TabItem' in element.control_type or
                element.control_type in ['HyperlinkControl', 'SplitButtonControl', 'ToggleButtonControl']
            )
            
            if is_clickable:
                # Calculate center point for clicking
                center_x = element.local_bounding_rectangle_left + (element.local_bounding_rectangle_width // 2)
                center_y = element.local_bounding_rectangle_top + (element.local_bounding_rectangle_height // 2)
                
                clickable_elements.append({
                    'name': element.name,
                    'control_type': element.control_type,
                    'automation_id': element.automation_id,
                    'text_content': element.control_value_text,
                    'coordinates': {
                        'left': element.local_bounding_rectangle_left,
                        'top': element.local_bounding_rectangle_top,
                        'right': element.local_bounding_rectangle_right,
                        'bottom': element.local_bounding_rectangle_bottom,
                        'width': element.local_bounding_rectangle_width,
                        'height': element.local_bounding_rectangle_height,
                        'center_x': center_x,
                        'center_y': center_y
                    },
                    'is_enabled': element.is_enabled,
                    'has_focus': element.has_keyboard_focus,
                    'tree_depth': element.tree_depth_level,
                    'parent_name': element.parent_name,
                    'access_key': element.access_key,
                    'accelerator_key': element.accelerator_key
                })
        
        return clickable_elements

# Helper to get the specific Windows version name for the tool description
def get_windows_product_name():
    """Fetches the full Windows product name from the registry for better context."""
    if not IS_WINDOWS:
        # Return platform-appropriate description for non-Windows systems
        return platform.platform(terse=True)
    
    try:
        # The registry key that stores the full product name
        # Only import winreg on Windows
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        product_name, _ = winreg.QueryValueEx(key, "ProductName")
        winreg.CloseKey(key)
        return product_name
    except Exception:
        # Fallback to the platform module if registry access fails for any reason
        return f"Microsoft Windows ({platform.platform(terse=True)})"

# Tool definitions
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": f"""Use this tool to automate and manage the users operating-system ({get_windows_product_name()}), desktop, and applications etc. Works on Windows, macOS, and Linux (some GUI operations vary by platform - see readme).
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
        # Actual tool parameters - revealed only after readme call
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "list_windows", "activate_window", "scan_ui_elements", "get_clickable_elements", "move_window", "click_at_coordinates", "click_at_screen_coordinates", "take_screenshot", "send_text", "click_ui_element", "about", "execute_command", "read_output", "force_terminate", "list_sessions", "write_file", "read_file"],
                    "description": "Operation to perform"
                },
                "include_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include popup and minimized windows (for list_windows)"
                },
                "hwnd": {
                    "type": "string",
                    "description": "Window handle in hexadecimal format (e.g., '0x00020828') for activate_window, move_window, click_at_coordinates, take_screenshot, send_text, click_ui_element, and scan_ui_elements"
                },
                "request_focus": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to request keyboard focus in addition to bringing window to front (for activate_window)"
                },
                "window_title": {
                    "type": "string",
                    "description": "Window title or title pattern to scan for UI elements (for scan_ui_elements, optional if hwnd provided)"
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate for window position (for move_window)"
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate for window position (for move_window)"
                },
                "width": {
                    "type": "integer",
                    "description": "Window width in pixels (for move_window)"
                },
                "height": {
                    "type": "integer",
                    "description": "Window height in pixels (for move_window)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
                },
                "x_coordinate": {
                    "type": "integer",
                    "description": "X coordinate for clicking (window-relative or screen absolute)"
                },
                "y_coordinate": {
                    "type": "integer",
                    "description": "Y coordinate for clicking (window-relative or screen absolute)"
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button to click",
                    "default": "left"
                },
                "text": {
                    "type": "string",
                    "description": "Text/keystrokes to send using AutoHotkey-style syntax. Supports: {Enter}, {Tab}, {Escape}, {F1}-{F24}, arrow keys, etc. Modifiers: ^ (Ctrl), + (Shift), ! (Alt), # (Win). Examples: 'Hello{Enter}', '^c' (Ctrl+C), '!{F4}' (Alt+F4), '#r' (Win+R), '{Tab 3}' (Tab x3). Escape braces with {{} and {}}. Use {Raw}text for literal text."
                },
                "filename": {
                    "type": "string",
                    "description": "Filename to save screenshot (optional, if not provided returns base64 image data)"
                },
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional region to capture [x, y, width, height] relative to window"
                },
                "element_name": {
                    "type": "string",
                    "description": "Name or AutomationId of UI element to click"
                },
                "detail": {
                    "type": "string",
                    "enum": ["overview", "section", "full"],
                    "default": "overview",
                    "description": "Level of detail for about operation - 'overview' returns navigable tree with counts (default, tiny response), 'section' returns summary + top items for one section, 'full' returns everything (WARNING: can be 500KB+)"
                },
                "section": {
                    "type": "string",
                    "enum": ["system_information", "hardware_information", "display_information", "user_and_security_information", "performance_information", "software_environment", "network_information", "installed_applications", "running_processes", "browser_information"],
                    "description": "Specific section to drill into for about operation (required when detail='section')"
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum items to return for list data in about operation (processes, applications, etc.)"
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset for list data in about operation"
                },
                "filter": {
                    "type": "string",
                    "description": "Filter expression for about operation, e.g., 'name:chrome', 'publisher:Microsoft', 'high_memory', 'high_cpu'"
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["name", "memory", "cpu", "size", "install_date", "pid"],
                    "description": "Sort field for list data in about operation"
                },
                "pid": {
                    "type": "integer",
                    "description": "Get full details for a specific process by PID (for about section='running_processes')"
                },
                "app_name": {
                    "type": "string",
                    "description": "Get full details for a specific application by name (for about section='installed_applications')"
                },
                "moves": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "hwnd": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "integer"}
                                ],
                                "description": "Window handle as hexadecimal string (e.g., '0x00020828') or integer"
                            },
                            "x": {
                                "type": "integer",
                                "description": "X coordinate for window position"
                            },
                            "y": {
                                "type": "integer", 
                                "description": "Y coordinate for window position"
                            },
                            "width": {
                                "type": "integer",
                                "description": "Window width in pixels"
                            },
                            "height": {
                                "type": "integer",
                                "description": "Window height in pixels"
                            }
                        },
                        "required": ["hwnd", "x", "y", "width", "height"]
                    },
                    "description": "Array of window move operations for batch processing multiple windows at once"
                },
                "command": {
                    "type": "string",
                    "description": "Terminal command to execute (for execute_command)"
                },
                "timeout_ms": {
                    "type": "integer",
                    "default": 30000,
                    "description": "Timeout in milliseconds for command execution (for execute_command and read_output)"
                },
                "shell": {
                    "type": "string",
                    "description": "Optional shell to use for command execution (for execute_command)"
                },
                "session_id": {
                    "type": "integer",
                    "description": "Session ID for terminal operations (for read_output and force_terminate)"
                },
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to user_data directory) for file operations (for write_file and read_file)"
                },
                "content": {
                    "type": "string",
                    "description": "File content to write (unlimited size, for write_file)"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """
Desktop Automation and Management Tool (Windows / macOS / Linux)

A comprehensive tool for desktop automation, window management, UI interaction, and layout
control. It provides programmatic access to desktop operations that would typically require
manual interaction.

## Platform Support
Operations behave the same across platforms where implemented, but coverage differs:
- Windows: all operations (uses win32 APIs + UIAutomation).
- macOS: list_windows, activate_window, move_window, take_screenshot, click_at_coordinates,
  click_at_screen_coordinates, send_text, scan_ui_elements, get_clickable_elements,
  click_ui_element, about (uses CoreGraphics/CGWindowList, CGEvent, the Accessibility (AX) API,
  and the screencapture tool).
- Linux: list_windows, activate_window, move_window, take_screenshot, about.
An operation that a platform does not implement returns a clear "not implemented on <os>" error.

### macOS permissions (important)
- list_windows, activate_window, and about work with no special permission.
- take_screenshot of another app's window content needs Screen Recording permission (without it
  window titles are blank and captures may show only the desktop).
- move_window, send_text, the click_* operations, scan_ui_elements, and click_ui_element require
  Accessibility permission: System Settings > Privacy & Security > Accessibility, enabled for the
  application that runs this server (e.g. aura, or your Terminal). Until it is granted, those
  operations return an explanatory error instead of acting.
- macOS 'hwnd' values are CoreGraphics window numbers (decimal) returned by list_windows, not
  hex window handles. 'pid:<n>' and a literal application name are also accepted.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details.
The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Available Operations

### list_windows
List all visible windows with their properties and metadata.

Parameters:
- include_all (optional): Include popup and minimized windows (default: false)

Returns:
- Array of window objects with properties: hwnd, title, class, position, size, style flags

### activate_window
Activate a window by bringing it to the foreground and optionally giving it keyboard focus.

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- request_focus (optional): Whether to request keyboard focus in addition to bringing window to front (default: false)

Returns:
- Success message if window was activated successfully

### scan_ui_elements
Scan a specific window and extract all UI elements with text data and coordinates.
This reads the OS accessibility tree directly (no OCR): Windows UIAutomation, or the macOS
Accessibility (AX) API. On Windows it also detects accelerator keys (Alt+key shortcuts) for menu
automation; on macOS it returns each element's role, name/value, bounds and center coordinates.
(Not implemented on Linux.)

Parameters:
- window_title (optional): Window title or partial title pattern to scan. Uses intelligent matching with fallbacks including exact match, substring match, case-insensitive partial match, and common title variations
- hwnd (optional): Window handle for direct window targeting (Windows: hex like "0x00020828"; macOS: the CoreGraphics window number from list_windows)
- NOTE: Either window_title or hwnd must be provided (not both)

Returns:
- Complete UI element tree with text content, coordinates, control types, accelerator keys, and interaction data

### get_clickable_elements
Extract all clickable elements (buttons, links, etc.) from the last scanned window.
Must be called after scan_ui_elements to get clickable element coordinates.
Includes accelerator key information for keyboard automation (e.g., Alt+F for File menu).

Parameters:
- None (uses data from last scan_ui_elements call)

Returns:
- Array of clickable elements with precise coordinates and accelerator key data for automation

### move_window
Move and resize one (or more, using `moves` array input) a window to specified position and dimensions.

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- x (required): X coordinate for new window position (in pixels)
- y (required): Y coordinate for new window position (in pixels)  
- width (required): New window width in pixels
- height (required): New window height in pixels

Returns:
- Success message if window was moved/resized successfully

### click_at_coordinates
Click at specific coordinates within a window (window-relative coordinates).

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- x_coordinate (required): X coordinate relative to window (positive = from left, negative = from right)
- y_coordinate (required): Y coordinate relative to window (positive = from top, negative = from bottom)
- button (optional): Mouse button to click ("left", "right", "middle", default: "left")

Returns:
- Success message if click was performed successfully

### click_at_screen_coordinates
Click at absolute screen coordinates (not relative to any window).

Parameters:
- x_coordinate (required): X coordinate on screen (absolute pixels)
- y_coordinate (required): Y coordinate on screen (absolute pixels)
- button (optional): Mouse button to click ("left", "right", "middle", default: "left")

Returns:
- Success message if click was performed successfully

### take_screenshot
Take a screenshot of a window or region of a window.

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- filename (optional): Filename to save screenshot to (if not provided, returns base64 image data)
- region (optional): Array [x, y, width, height] specifying region relative to window

Returns:
- Success message and optionally base64 image data if no filename provided

### send_text
Send text and keystrokes to a window using AutoHotkey-style syntax.

Parameters:
- hwnd (required): Window handle (Windows: hex like "0x00020828"; macOS: the CoreGraphics window number from list_windows - its app is focused first)
- text (required): Text/keystrokes to send using AutoHotkey-style syntax

**AutoHotkey-Style Syntax:**

NOTE (macOS): the same syntax works, with one mapping difference - the '#' prefix is the
Command key on macOS (so '#c' = Command+C to copy, '#{Space}' = Command+Space). '^' is Control,
'+' is Shift, '!' is Option/Alt. Ordinary characters are typed as Unicode regardless of keyboard
layout.

SPECIAL KEYS (in braces):
- {Enter}, {Tab}, {Escape}/{Esc}, {Space}, {Backspace}/{BS}
- {Delete}/{Del}, {Insert}/{Ins}, {Home}, {End}, {PageUp}/{PgUp}, {PageDown}/{PgDn}
- {Up}, {Down}, {Left}, {Right} - Arrow keys
- {F1} through {F24} - Function keys
- {PrintScreen}, {Pause}, {CapsLock}, {NumLock}, {ScrollLock}
- {Numpad0}-{Numpad9}, {NumpadMult}, {NumpadAdd}, {NumpadSub}, {NumpadDiv}, {NumpadDot}
- {Browser_Back}, {Volume_Mute}, {Media_Play_Pause}, etc. - Media keys

MODIFIER PREFIXES (outside braces):
- ^ = Ctrl    (e.g., ^c = Ctrl+C, ^s = Ctrl+S)
- + = Shift   (e.g., +a = Shift+A)
- ! = Alt     (e.g., !{F4} = Alt+F4)
- # = Win     (e.g., #r = Win+R)
- Combine: ^+s = Ctrl+Shift+S, ^!{Delete} = Ctrl+Alt+Delete

KEY REPETITION:
- {Tab 5} - Press Tab 5 times
- {Enter 3} - Press Enter 3 times

KEY HOLD/RELEASE:
- {Ctrl down} - Hold Ctrl key down
- {Ctrl up} - Release Ctrl key
- {Shift down}hello{Shift up} - Type "HELLO"

LITERAL CHARACTERS (escaping):
- {{}  - Literal { character
- {}}  - Literal } character
- {^}, {+}, {!}, {#} - Literal modifier symbols

RAW MODE:
- {Raw}text - Send remaining text literally without parsing

EXAMPLES:
- "Hello{Enter}" - Type "Hello" then press Enter
- "^a^c" - Ctrl+A (select all), Ctrl+C (copy)
- "!{F4}" - Alt+F4 (close window)
- "#r" - Win+R (open Run dialog)
- "{Tab 3}submit" - Press Tab 3 times, then type "submit"

Returns:
- Success message if text/keystrokes were sent successfully

### click_ui_element
Click on a specific UI element within a window by name or AutomationId.

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- element_name (required): Name or AutomationId of UI element to click

Returns:
- Success message if UI element was clicked successfully

### execute_command
Execute a terminal command with timeout support, allowing it to continue in background.
Commands continue running even after timeout, and you can read their output later.

Parameters:
- command (required): Terminal command to execute
- timeout_ms (optional): Timeout in milliseconds for initial output collection (default: 30000)
- shell (optional): Specific shell to use for execution. Options:
  * Windows:
    - "cmd" or "cmd.exe" - Windows Command Prompt (default)
    - "powershell" or "powershell.exe" - Windows PowerShell 5.x
    - "pwsh" or "pwsh.exe" - PowerShell Core/7.x (if installed)
    - "wsl" or "bash" - Windows Subsystem for Linux bash shell (if installed)
    - Full path like "C:\\Windows\\System32\\cmd.exe" - Custom shell executable
  * Unix/Linux:
    - "/bin/bash" - Bash shell (default)
    - "/bin/sh" - Bourne shell
    - "/bin/zsh" - Z shell
    - Any valid shell executable path

Returns:
- Session ID for tracking the command, initial output, and background status

### read_output
Read new output from a running command session.

Parameters:
- session_id (required): Session ID returned from execute_command
- timeout_ms (optional): Timeout in milliseconds to wait for new output (default: 5000)

Returns:
- New output from the session, if any

### force_terminate
Force terminate a running command session.

Parameters:
- session_id (required): Session ID to terminate

Returns:
- Success message if session was terminated

### list_sessions
List all active command sessions with their status.

Parameters:
- None

Returns:
- Array of active sessions with their IDs, runtime, and status

### about
Get system information with drill-down navigation to avoid context overflow.

**IMPORTANT: Uses 3-level drill-down to keep responses small!**

- Level 1 (default): overview - Returns ~2KB navigable tree with counts
- Level 2: section - Returns summary + top 10 items for one section  
- Level 3: full - Returns everything (WARNING: 500KB+, can overflow context)

Parameters:
- detail (optional): 'overview' (default), 'section', or 'full'
- section (optional): Section to drill into (required when detail='section')
- limit (optional): Max items to return for lists (default: 10)
- offset (optional): Pagination offset for lists (default: 0)
- filter (optional): Filter expression like 'name:chrome', 'publisher:Microsoft', 'high_memory', 'high_cpu'
- sort_by (optional): Sort field - 'name', 'memory', 'cpu', 'size', 'install_date', 'pid'
- pid (optional): Get full details for specific process by PID
- app_name (optional): Get full details for specific application by name

Available sections:
- system_information, hardware_information, display_information
- user_and_security_information, performance_information, software_environment
- network_information, installed_applications, running_processes, browser_information

Returns:
- Overview mode: Tree of available sections with counts and top items preview
- Section mode: Summary stats + top N items (paginated)
- Full mode: Complete data for all sections (WARNING: very large!)

### write_file, read_file
Write/read data on a file on the local filesystem. Supports unlimited file sizes. Useful with the `python` mcp tool, which can directly use MCP servers and process unlimited-size data.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {
     "input": {"operation": "readme"}
   }

2. For listing all main windows:
   {
     "input": {
       "operation": "list_windows",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

3. For listing all windows including popups:
   {
     "input": {
       "operation": "list_windows",
       "include_all": true,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

4. For activating a specific window:
   {
     "input": {
       "operation": "activate_window",
       "hwnd": "0x00020828",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

4b. For activating a window with keyboard focus:
   {
     "input": {
       "operation": "activate_window",
       "hwnd": "0x00020828",
       "request_focus": true,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

5. For scanning UI elements in a window:
   {
     "input": {
       "operation": "scan_ui_elements",
       "window_title": "Notepad",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

5b. For scanning UI elements in a window by handle:
   {
     "input": {
       "operation": "scan_ui_elements",
       "hwnd": "0x00020828",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

6. For getting clickable elements from last scan:
   {
     "input": {
       "operation": "get_clickable_elements",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

7. For moving/resizing a window:
  {
    "input": {
      "operation": "move_window",
      "hwnd": "0x00020828",
      "x": 100,
      "y": 100,
      "width": 800,
      "height": 600,
      "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
    }
  }

7b. For moving/resizing multiple windows at once (batch operation):
  {
    "input": {
      "operation": "move_window",
      "moves": [
        { "hwnd": "0x00020C4A", "x": 0, "y": 0, "width": 960, "height": 580 },
        { "hwnd": "0x00020B0C", "x": 960, "y": 0, "width": 960, "height": 580 }
      ],
      "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
    }
  }

8. For clicking at window-relative coordinates:
   {
     "input": {
       "operation": "click_at_coordinates",
       "hwnd": "0x00020828",
       "x_coordinate": 50,
       "y_coordinate": 100,
       "button": "left",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

9. For clicking at absolute screen coordinates:
   {
     "input": {
       "operation": "click_at_screen_coordinates",
       "x_coordinate": 500,
       "y_coordinate": 300,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

10. For taking a screenshot of a window:
   {
     "input": {
       "operation": "take_screenshot",
       "hwnd": "0x00020828",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

11. For taking a screenshot of a region within a window:
   {
     "input": {
       "operation": "take_screenshot",
       "hwnd": "0x00020828",
       "region": [50, 50, 300, 200],
       "filename": "screenshot.png",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

12. For sending text to a window:
   {
     "input": {
       "operation": "send_text",
       "hwnd": "0x00020828",
       "text": "Hello, World!",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

13. For clicking a UI element by name:
   {
     "input": {
       "operation": "click_ui_element",
       "hwnd": "0x00020828",
       "element_name": "OK",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

14. For executing a terminal command:
   {
     "input": {
       "operation": "execute_command",
       "command": "dir /s",
       "timeout_ms": 5000,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

14b. For executing a PowerShell command:
   {
     "input": {
       "operation": "execute_command",
       "command": "Get-Process | Select-Object Name, CPU",
       "shell": "powershell",
       "timeout_ms": 5000,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

14c. For executing a command in WSL bash:
   {
     "input": {
       "operation": "execute_command",
       "command": "ls -la /home",
       "shell": "wsl",
       "timeout_ms": 5000,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

15. For reading output from a running command:
   {
     "input": {
       "operation": "read_output",
       "session_id": 1,
       "timeout_ms": 3000,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

16. For terminating a running command:
   {
     "input": {
       "operation": "force_terminate",
       "session_id": 1,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

17. For listing active command sessions:
   {
     "input": {
       "operation": "list_sessions",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

18. For writing a file:
   {
     "input": {
       "operation": "write_file",
       "path": "/tmp/mydata.txt",
       "content": "You can put unlimited amounts of data in here!",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

19. For reading a file:
   {
     "input": {
       "operation": "read_file",
       "path": "mydata.txt",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

20. For getting system overview (recommended first call - tiny response):
   {
     "input": {
       "operation": "about",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

21. For drilling into a specific section (e.g., running processes):
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "running_processes",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

22. For filtering processes by name:
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "running_processes",
       "filter": "name:chrome",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

23. For getting high-memory processes sorted by memory usage:
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "running_processes",
       "filter": "high_memory",
       "sort_by": "memory",
       "limit": 20,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

24. For getting details of a specific process by PID:
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "running_processes",
       "pid": 12345,
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

25. For paginating through installed applications:
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "installed_applications",
       "limit": 20,
       "offset": 40,
       "sort_by": "name",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

26. For filtering applications by publisher:
   {
     "input": {
       "operation": "about",
       "detail": "section",
       "section": "installed_applications",
       "filter": "publisher:Microsoft",
       "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
     }
   }

## Window Object Properties
Each window object contains:
- hwnd: Window handle (hexadecimal string)
- title: Window title text
- class: Window class name
- x, y: Window position coordinates
- width, height: Window dimensions
- style_flags: Window style information
- process_id: Process ID that owns the window
- process_name: Name of the process executable
- is_visible: Whether window is currently visible
- is_minimized: Whether window is minimized
- is_maximized: Whether window is maximized

## Notes
- Window handles (hwnd) are returned as hexadecimal strings for easy use in other operations
- Tool windows and child windows are filtered out by default unless include_all=true
- Process information requires appropriate permissions
"""
    }
]

# ============================================================================
# FUNCTIONAL CODE BLOCKS - Can be called independently or via MCP
# ============================================================================

def get_window_style_flags(style: int, ex_style: int) -> Dict[str, bool]:
    """Convert Windows style flags to readable dictionary.
    
    Args:
        style: Window style flags
        ex_style: Extended window style flags
        
    Returns:
        Dictionary of style flag names and their boolean values
    """
    return {
        'is_overlapped': (style & win32con.WS_OVERLAPPED) != 0,
        'is_popup': (style & win32con.WS_POPUP) != 0,
        'is_child': (style & win32con.WS_CHILD) != 0,
        'is_visible': (style & win32con.WS_VISIBLE) != 0,
        'is_disabled': (style & win32con.WS_DISABLED) != 0,
        'is_minimized': (style & win32con.WS_MINIMIZE) != 0,
        'is_maximized': (style & win32con.WS_MAXIMIZE) != 0,
        'is_tool_window': (ex_style & win32con.WS_EX_TOOLWINDOW) != 0,
        'is_app_window': (ex_style & win32con.WS_EX_APPWINDOW) != 0,
        'is_no_activate': (ex_style & win32con.WS_EX_NOACTIVATE) != 0,
        'is_transparent': (ex_style & win32con.WS_EX_TRANSPARENT) != 0,
        'has_window_edge': (ex_style & win32con.WS_EX_WINDOWEDGE) != 0
    }

def get_process_info(pid: int) -> Dict[str, Union[str, int]]:
    """Get process information for a given process ID.
    
    Args:
        pid: Process ID
        
    Returns:
        Dictionary with process information
    """
    try:
        process = psutil.Process(pid)
        return {
            'pid': pid,
            'name': process.name(),
            'exe': process.exe() if hasattr(process, 'exe') else 'N/A',
            'status': process.status() if hasattr(process, 'status') else 'N/A'
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {
            'pid': pid,
            'name': 'N/A',
            'exe': 'N/A',
            'status': 'N/A'
        }

def list_windows_functional(include_all: bool = False) -> List[Dict]:
    """List all visible windows with their properties.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        include_all: If True, include popup and minimized windows
        
    Returns:
        List of window dictionaries with comprehensive properties
    """
    
    windows = []
    total_checked = 0
    filtered_out = 0
    
    def enum_callback(hwnd, _):
        nonlocal total_checked, filtered_out
        total_checked += 1
        
        if win32gui.IsWindowVisible(hwnd):
            try:
                title = win32gui.GetWindowText(hwnd)
                if title:  # Only process windows with titles
                    rect = win32gui.GetWindowRect(hwnd)
                    class_name = win32gui.GetClassName(hwnd)
                    
                    # Get window styles - exactly like cursor_auto_clicker.py
                    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    
                    # Filter logic - exactly like cursor_auto_clicker.py
                    if not include_all:
                        # Skip tool windows and non-root windows
                        if (ex_style & win32con.WS_EX_TOOLWINDOW) != 0:
                            filtered_out += 1
                            return True
                            
                        root = win32gui.GetAncestor(hwnd, win32con.GA_ROOTOWNER)
                        if root != hwnd:
                            filtered_out += 1
                            return True
                    
                    # Get process information
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        process_info = get_process_info(pid)
                    except Exception:
                        process_info = {'pid': 0, 'name': 'N/A', 'exe': 'N/A', 'status': 'N/A'}
                    
                    # Get style flags
                    style_flags = get_window_style_flags(style, ex_style)
                    
                    # Create window object
                    window_obj = {
                        'hwnd': f"0x{hwnd:08X}",
                        'title': title,
                        'class': class_name,
                        'x': rect[0],
                        'y': rect[1],
                        'width': rect[2] - rect[0],
                        'height': rect[3] - rect[1],
                        'style_flags': style_flags,
                        'process_id': process_info['pid'],
                        'process_name': process_info['name'],
                        'process_exe': process_info['exe'],
                        'is_visible': win32gui.IsWindowVisible(hwnd),
                        'is_minimized': win32gui.IsIconic(hwnd),
                        'is_maximized': bool(style & win32con.WS_MAXIMIZE)
                    }
                    
                    windows.append(window_obj)
                else:
                    # Window has no title
                    filtered_out += 1
                    
            except Exception as e:
                # Log error but continue processing other windows
                MCPLogger.log(TOOL_LOG_NAME, f"Error processing window 0x{hwnd:08X}: {str(e)}")
                filtered_out += 1
        else:
            # Window not visible
            filtered_out += 1
                
        return True
    
    # Enumerate all windows
    try:
        win32gui.EnumWindows(enum_callback, None)
    except Exception as e:
        raise RuntimeError(f"Failed to enumerate windows: {str(e)}")
    
    # Log debug information
    MCPLogger.log(TOOL_LOG_NAME, f"Window enumeration: {total_checked} total, {len(windows)} matched, {filtered_out} filtered out")
    
    # Sort windows by title for consistent output
    windows.sort(key=lambda w: w['title'].lower())
    
    return windows

def activate_window_functional(hwnd_str: str, request_focus: bool = False) -> Tuple[bool, str]:
    """Force a window to the foreground with enhanced reliability using proven techniques.
    
    This implementation is based on the working activate_window_o3.py with comprehensive
    fallback methods and proper focus handling.
    
    Args:
        hwnd_str: Window handle as hexadecimal string (e.g., "0x00020828")
        request_focus: Whether to request keyboard focus in addition to bringing window to front
        
    Returns:
        Tuple of (success, message) where:
        - success: True if window was activated successfully
        - message: Success or error message
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str, 16)  # Assume hex even without 0x prefix
            
    except ValueError:
        return False, f"Invalid window handle format: '{hwnd_str}'. Expected hexadecimal format like '0x00020828'"
    
    # Validate the window handle
    if not win32gui.IsWindow(hwnd):
        return False, f"Window handle 0x{hwnd:08X} does not exist or is invalid"
    
    # Get window title for logging
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception as e:
        title = f"<unable to get title: {e}>"
    
    MCPLogger.log(TOOL_LOG_NAME, f"Attempting to activate window 0x{hwnd:08X}: '{title}'")
    
    hwnd_self = None
    try:
        hwnd_self = win32console.GetConsoleWindow()
    except:
        pass
    
    # Step 1: Restore window if minimized
    if win32gui.IsIconic(hwnd):
        MCPLogger.log(TOOL_LOG_NAME, "Window is minimized, restoring...")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.1)  # Give time for restore animation
    
    # Step 2: Make window visible if hidden
    if not win32gui.IsWindowVisible(hwnd):
        MCPLogger.log(TOOL_LOG_NAME, "Window is hidden, making visible...")
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        time.sleep(0.1)
    
    # Step 3: Allow this process to set foreground window
    user32.AllowSetForegroundWindow(ASFW_ANY)
    
    # Step 4: Temporarily disable foreground lock timeout
    old_timeout = wintypes.UINT()
    user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                 ctypes.byref(old_timeout), 0)
    user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0,
                                 win32con.SPIF_SENDCHANGE)
    
    success = False
    try:
        # Step 5: Get current foreground window info
        hwnd_fg = win32gui.GetForegroundWindow()
        if hwnd_fg:
            tid_fg = win32process.GetWindowThreadProcessId(hwnd_fg)[0]
        else:
            tid_fg = 0
        
        tid_self = win32api.GetCurrentThreadId()
        tid_target = win32process.GetWindowThreadProcessId(hwnd)[0]
        
        hwnd_fg_str = f"0x{hwnd_fg:08X}" if hwnd_fg else "0x00000000"
        MCPLogger.log(TOOL_LOG_NAME, f"Current foreground: {hwnd_fg_str}, Target thread: {tid_target}, Current thread: {tid_self}")
        
        # Step 6: Attach input to both foreground and target threads
        attached_to_fg = False
        attached_to_target = False
        
        if tid_fg and tid_fg != tid_self:
            try:
                win32process.AttachThreadInput(tid_self, tid_fg, True)
                attached_to_fg = True
                MCPLogger.log(TOOL_LOG_NAME, f"Attached to foreground thread {tid_fg}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not attach to foreground thread: {e}")
        
        if tid_target != tid_self and tid_target != tid_fg:
            try:
                win32process.AttachThreadInput(tid_self, tid_target, True)
                attached_to_target = True
                MCPLogger.log(TOOL_LOG_NAME, f"Attached to target thread {tid_target}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not attach to target thread: {e}")
        
        # Step 7: Multiple activation attempts with different methods
        
        # Method 1: Direct SetForegroundWindow
        if request_focus:
            try:
                if win32gui.SetForegroundWindow(hwnd):
                    MCPLogger.log(TOOL_LOG_NAME, "Method 1: SetForegroundWindow succeeded")
                    success = True
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "Method 1: SetForegroundWindow failed")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Method 1: SetForegroundWindow exception: {e}")
        
        # Method 2: Using SetWindowPos with TOPMOST trick
        if not success:
            try:
                flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                time.sleep(0.01)
                win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                
                if request_focus and win32gui.SetForegroundWindow(hwnd):
                    MCPLogger.log(TOOL_LOG_NAME, "Method 2: TOPMOST trick with SetForegroundWindow succeeded")
                    success = True
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "Method 2: TOPMOST trick completed (window brought to front)")
                    if not request_focus:
                        success = True  # For bring-to-front only, this is sufficient
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Method 2: TOPMOST trick exception: {e}")
        
        # Method 3: Alt key injection + SetForegroundWindow
        if not success and request_focus:
            try:
                # Inject Alt key press and release using advanced SendInput. Use INPUT_FULL
                # (the full-size INPUT with the complete union) and its size, as
                # send_text_functional already does, so the 64-bit structure size is correct
                # and SendInput does not silently reject the array.
                inp = (INPUT_FULL * 2)()
                inp[0].type = win32con.INPUT_KEYBOARD
                inp[0].ki = KEYBDINPUT(wVk=win32con.VK_MENU)
                inp[1].type = win32con.INPUT_KEYBOARD
                inp[1].ki = KEYBDINPUT(wVk=win32con.VK_MENU, dwFlags=win32con.KEYEVENTF_KEYUP)
                
                if user32.SendInput(2, inp, ctypes.sizeof(INPUT_FULL)) == 2:
                    time.sleep(0.01)
                    if win32gui.SetForegroundWindow(hwnd):
                        MCPLogger.log(TOOL_LOG_NAME, "Method 3: Alt key injection succeeded")
                        success = True
                    else:
                        MCPLogger.log(TOOL_LOG_NAME, "Method 3: Alt key injection failed")
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "Method 3: SendInput failed")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Method 3: Alt key injection exception: {e}")
        
        # Method 4: ShowWindow with SW_SHOW + SetForegroundWindow
        if not success and request_focus:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                time.sleep(0.01)
                if win32gui.SetForegroundWindow(hwnd):
                    MCPLogger.log(TOOL_LOG_NAME, "Method 4: ShowWindow + SetForegroundWindow succeeded")
                    success = True
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "Method 4: ShowWindow + SetForegroundWindow failed")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Method 4: ShowWindow method exception: {e}")
        
        # Method 5: BringWindowToTop + SetForegroundWindow
        if not success and request_focus:
            try:
                win32gui.BringWindowToTop(hwnd)
                time.sleep(0.01)
                if win32gui.SetForegroundWindow(hwnd):
                    MCPLogger.log(TOOL_LOG_NAME, "Method 5: BringWindowToTop succeeded")
                    success = True
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "Method 5: BringWindowToTop failed")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Method 5: BringWindowToTop exception: {e}")
        
        # Step 8: Detach thread inputs
        if attached_to_fg:
            try:
                win32process.AttachThreadInput(tid_self, tid_fg, False)
                MCPLogger.log(TOOL_LOG_NAME, "Detached from foreground thread")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not detach from foreground thread: {e}")
        
        if attached_to_target:
            try:
                win32process.AttachThreadInput(tid_self, tid_target, False)
                MCPLogger.log(TOOL_LOG_NAME, "Detached from target thread")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not detach from target thread: {e}")
        
    finally:
        # Step 9: Restore original foreground lock timeout
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                     old_timeout, win32con.SPIF_SENDCHANGE)
    
    # Step 10: Handle console window (send to back)
    if hwnd_self:
        try:
            win32gui.SetWindowPos(hwnd_self, win32con.HWND_BOTTOM,
                                  0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                  win32con.SWP_NOACTIVATE)
            MCPLogger.log(TOOL_LOG_NAME, "Sent console window to back")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not bury console: {e}")
    
    # Step 11: Verify success with timeout
    deadline = time.time() + 5.0  # 5 second timeout
    while time.time() < deadline:
        current_fg = win32gui.GetForegroundWindow()
        if current_fg == hwnd:
            if request_focus:
                MCPLogger.log(TOOL_LOG_NAME, f"SUCCESS: Window 0x{hwnd:08X} is now in foreground with focus")
                return True, f"Successfully activated window 0x{hwnd:08X} with keyboard focus: '{title}'"
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"SUCCESS: Window 0x{hwnd:08X} is now in foreground")
                return True, f"Successfully brought window 0x{hwnd:08X} to front: '{title}'"
        time.sleep(0.05)
    
    # Step 12: Final attempt - Force activation even if system restrictions exist
    if request_focus:
        MCPLogger.log(TOOL_LOG_NAME, "Standard methods failed, attempting force activation...")
        try:
            # Get the target window's process
            _, target_pid = win32process.GetWindowThreadProcessId(hwnd)
            
            # Allow the target process to set foreground
            user32.AllowSetForegroundWindow(target_pid)
            
            # Try one more time
            if win32gui.SetForegroundWindow(hwnd):
                time.sleep(0.1)
                if win32gui.GetForegroundWindow() == hwnd:
                    MCPLogger.log(TOOL_LOG_NAME, f"SUCCESS: Force activation worked for window 0x{hwnd:08X}")
                    return True, f"Successfully activated window 0x{hwnd:08X} with keyboard focus (force method): '{title}'"
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Force activation failed: {e}")
    
    # Final check
    current_fg = win32gui.GetForegroundWindow()
    if current_fg == hwnd:
        # Success even if we didn't detect it in the timeout loop
        if request_focus:
            return True, f"Successfully activated window 0x{hwnd:08X} with keyboard focus: '{title}'"
        else:
            return True, f"Successfully brought window 0x{hwnd:08X} to front: '{title}'"
    else:
        # Check if it was at least brought to front (even without focus)
        if not request_focus:
            # For bring-to-front only, check if TOPMOST trick worked
            return True, f"Window 0x{hwnd:08X} brought to front: '{title}' (focus not requested)"
        else:
            # Precompute the hex form; "0x{current_fg:08X if current_fg else 0}" is an invalid
            # format spec that raised ValueError on this failure path (crashing the handler).
            fg_hex = f"0x{current_fg:08X}" if current_fg else "0x00000000"
            MCPLogger.log(TOOL_LOG_NAME, f"Could not bring window to foreground. Current foreground: {fg_hex}")
            return False, f"Could not activate window 0x{hwnd:08X}: '{title}'. Current foreground: {fg_hex}"

# Per-session store of the most recent UI scanner instance. Keyed by MCP session id so one
# caller's get_clickable_elements cannot return another concurrent caller's scan (the old
# single module-level scanner was overwritten by whichever scan ran last). scan_ui_elements
# also now returns its clickable elements inline in the response, so callers do not have to
# depend on this shared state at all.
_ui_scanners_by_session_key: Dict[object, comprehensive_ui_tree_walker_with_text_extraction] = {}
_ui_scanners_by_session_key_lock = threading.Lock()
_MAX_RETAINED_UI_SCANNERS = 64
_DEFAULT_UI_SCANNER_SESSION_KEY = "__no_session__"


def _normalize_ui_scanner_session_key(session_key: object) -> object:
    """Map a possibly-missing MCP session id to a stable dict key."""
    return session_key if session_key is not None else _DEFAULT_UI_SCANNER_SESSION_KEY


def _store_ui_scanner_for_session(session_key: object, ui_scanner: comprehensive_ui_tree_walker_with_text_extraction) -> None:
    """Store the most recent scanner for a session (locked + bounded so a long-lived server
    with many sessions cannot grow the store without limit)."""
    key = _normalize_ui_scanner_session_key(session_key)
    with _ui_scanners_by_session_key_lock:
        _ui_scanners_by_session_key[key] = ui_scanner
        if len(_ui_scanners_by_session_key) > _MAX_RETAINED_UI_SCANNERS:
            for oldest_stored_key in list(_ui_scanners_by_session_key.keys()):
                if oldest_stored_key == key:
                    continue
                del _ui_scanners_by_session_key[oldest_stored_key]
                if len(_ui_scanners_by_session_key) <= _MAX_RETAINED_UI_SCANNERS:
                    break


def _get_ui_scanner_for_session(session_key: object) -> Optional[comprehensive_ui_tree_walker_with_text_extraction]:
    """Return the most recent scanner stored for a session, or None if there is none."""
    key = _normalize_ui_scanner_session_key(session_key)
    with _ui_scanners_by_session_key_lock:
        return _ui_scanners_by_session_key.get(key)


def scan_ui_elements_functional(window_title: Optional[str] = None, hwnd_str: Optional[str] = None, session_key: object = None) -> Dict[str, any]:
    """Scan a specific window and extract all UI elements with text data and coordinates.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        window_title: Window title or partial title pattern to scan (optional if hwnd_str provided)
        hwnd_str: Window handle in hexadecimal format (optional if window_title provided)
        session_key: Opaque per-caller key (MCP session id) used to isolate this caller's
            scanner from other concurrent callers'
        
    Returns:
        Dictionary containing window info, scan summary, and extracted UI elements
    """
    try:
        if window_title:
            MCPLogger.log(TOOL_LOG_NAME, f"Starting UI scan for window: '{window_title}'")
        elif hwnd_str:
            MCPLogger.log(TOOL_LOG_NAME, f"Starting UI scan for window handle: '{hwnd_str}'")
        else:
            return {"error": "Either window_title or hwnd_str must be provided", "extracted_ui_elements": []}
        
        # Create a new UI scanner instance
        ui_scanner = comprehensive_ui_tree_walker_with_text_extraction()
        
        # Scan the window
        scan_result = ui_scanner.scan_specific_window_and_extract_text_data(
            window_title_pattern=window_title, 
            hwnd_str=hwnd_str
        )
        
        # Store the scanner keyed by session (for a later get_clickable_elements on the same
        # session) and also return the clickable elements inline so the response is fully
        # self-contained - no reliance on shared server-side state that a concurrent scan
        # could clobber.
        _store_ui_scanner_for_session(session_key, ui_scanner)
        if "error" not in scan_result:
            inline_clickable_elements = ui_scanner.find_all_buttons_and_clickable_elements_with_coordinates()
            scan_result["clickable_elements"] = inline_clickable_elements
            scan_result["total_clickable_found"] = len(inline_clickable_elements)
        
        MCPLogger.log(TOOL_LOG_NAME, f"UI scan completed. Found {len(scan_result.get('extracted_ui_elements', []))} elements")
        
        return scan_result
        
    except Exception as e:
        if window_title:
            error_msg = f"Error scanning UI elements for window '{window_title}': {str(e)}"
        else:
            error_msg = f"Error scanning UI elements for window handle '{hwnd_str}': {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {"error": error_msg, "extracted_ui_elements": []}

def get_clickable_elements_functional(session_key: object = None) -> Dict[str, any]:
    """Extract all clickable elements from this caller's last UI scan.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        session_key: Opaque per-caller key (MCP session id); only this session's own last
            scan is used, so one caller cannot receive another caller's clickable elements
        
    Returns:
        Dictionary containing clickable elements with coordinates
    """
    try:
        ui_scanner = _get_ui_scanner_for_session(session_key)
        if ui_scanner is None:
            return {
                "error": "No UI scan data available for this session. Please call scan_ui_elements first (its response also includes clickable_elements inline).",
                "clickable_elements": []
            }
        
        MCPLogger.log(TOOL_LOG_NAME, "Extracting clickable elements from last scan")
        
        # Get clickable elements
        clickable_elements = ui_scanner.find_all_buttons_and_clickable_elements_with_coordinates()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(clickable_elements)} clickable elements")
        
        return {
            "clickable_elements": clickable_elements,
            "total_clickable_found": len(clickable_elements),
            "scan_timestamp": time.time()
        }
        
    except Exception as e:
        error_msg = f"Error extracting clickable elements: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {"error": error_msg, "clickable_elements": []}

def move_window_functional(hwnd_str: str, x: int, y: int, width: int, height: int) -> Tuple[bool, str]:
    """Move and resize a window to the specified position and dimensions.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        hwnd_str: Window handle as hexadecimal string (e.g., "0x00020828")
        x: X coordinate for new window position (in pixels)
        y: Y coordinate for new window position (in pixels)
        width: New window width in pixels
        height: New window height in pixels
        
    Returns:
        Tuple of (success, message) where:
        - success: True if window was moved/resized successfully
        - message: Success or error message
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str, 16)  # Assume hex even without 0x prefix
            
    except ValueError:
        return False, f"Invalid window handle format: '{hwnd_str}'. Expected hexadecimal format like '0x00020828'"
    
    try:
        # Verify window exists
        if not win32gui.IsWindow(hwnd):
            return False, f"Window handle 0x{hwnd:08X} does not exist or is invalid"
        
        # Get window title for logging
        title = win32gui.GetWindowText(hwnd)
        
        # Validate coordinates and dimensions
        if width <= 0 or height <= 0:
            return False, f"Invalid dimensions: width={width}, height={height}. Both must be positive."
        
        if x < -32768 or x > 32767 or y < -32768 or y > 32767:
            return False, f"Invalid coordinates: x={x}, y={y}. Must be within range -32768 to 32767."
        
        MCPLogger.log(TOOL_LOG_NAME, f"Moving window 0x{hwnd:08X} ('{title}') to ({x}, {y}) with size {width}x{height}")
        
        # Move and resize the window (True = repaint)
        win32gui.MoveWindow(hwnd, x, y, width, height, True)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully moved/resized window 0x{hwnd:08X}")
        return True, f"Window 0x{hwnd:08X} moved and resized successfully"
        
    except Exception as e:
        return False, f"Error moving window: {e}"

def click_at_coordinates_functional(hwnd_str: str, x: int, y: int, button: str = "left") -> Tuple[bool, str]:
    """Click at specific coordinates within a window.
    
    Args:
        hwnd_str: Window handle as hexadecimal string
        x: X coordinate relative to window (positive = from left, negative = from right)
        y: Y coordinate relative to window (positive = from top, negative = from bottom)
        button: Mouse button to click ("left", "right", "middle")
        
    Returns:
        Tuple of (success, message)
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str)
            
        # Validate window handle
        if not win32gui.IsWindow(hwnd):
            return False, f"Invalid window handle: {hwnd_str}"
        
        # Activate window first to ensure clicks work properly (especially for Chrome/browsers)
        success, msg = activate_window_functional(hwnd_str, request_focus=True)
        if not success:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not activate window before clicking: {msg}")
        
        # Small delay to ensure window has focus before clicking
        time.sleep(0.2)
            
        # Get window position and size
        rect = win32gui.GetWindowRect(hwnd)
        win_x, win_y = rect[0], rect[1]
        win_width = rect[2] - rect[0]
        win_height = rect[3] - rect[1]
        
        # Convert relative coordinates to absolute screen coordinates
        if x < 0:
            screen_x = win_x + win_width + x  # x is negative, so this is subtraction
        else:
            screen_x = win_x + x
            
        if y < 0:
            screen_y = win_y + win_height + y  # y is negative, so this is subtraction
        else:
            screen_y = win_y + y
            
        # Store current mouse position
        old_pos = win32api.GetCursorPos()
        
        # Map button to mouse events
        button_map = {
            "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
            "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
            "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP)
        }
        
        if button not in button_map:
            return False, f"Invalid button: {button}. Must be 'left', 'right', or 'middle'"
            
        down_event, up_event = button_map[button]
        
        # Move mouse to target position
        win32api.SetCursorPos((screen_x, screen_y))
        
        # Send click events
        win32api.mouse_event(down_event, screen_x, screen_y, 0, 0)
        time.sleep(0.05)  # Small delay between down and up
        win32api.mouse_event(up_event, screen_x, screen_y, 0, 0)
        
        # Move mouse back to original position
        win32api.SetCursorPos(old_pos)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Clicked {button} button at window coordinates ({x}, {y}) = screen ({screen_x}, {screen_y})")
        return True, f"Successfully clicked {button} button at coordinates ({x}, {y}) in window 0x{hwnd:08X}"
        
    except Exception as e:
        return False, f"Error clicking at coordinates: {e}"

def click_at_screen_coordinates_functional(x: int, y: int, button: str = "left") -> Tuple[bool, str]:
    """Click at absolute screen coordinates.
    
    Args:
        x: X coordinate on screen (absolute)
        y: Y coordinate on screen (absolute)
        button: Mouse button to click ("left", "right", "middle")
        
    Returns:
        Tuple of (success, message)
    """
    try:
        # Map button to mouse events
        button_map = {
            "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
            "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
            "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP)
        }
        
        if button not in button_map:
            return False, f"Invalid button: {button}. Must be 'left', 'right', or 'middle'"
            
        down_event, up_event = button_map[button]
        
        # Store current mouse position
        old_pos = win32api.GetCursorPos()
        
        # Move mouse to target position
        win32api.SetCursorPos((x, y))
        
        # Send click events
        win32api.mouse_event(down_event, x, y, 0, 0)
        time.sleep(0.05)  # Small delay between down and up
        win32api.mouse_event(up_event, x, y, 0, 0)
        
        # Move mouse back to original position
        win32api.SetCursorPos(old_pos)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Clicked {button} button at screen coordinates ({x}, {y})")
        return True, f"Successfully clicked {button} button at screen coordinates ({x}, {y})"
        
    except Exception as e:
        return False, f"Error clicking at screen coordinates: {e}"

def take_screenshot_functional(hwnd_str: str, filename: Optional[str] = None, region: Optional[List[int]] = None) -> Tuple[bool, str, Optional[str]]:
    """Take a screenshot of a window or region of a window.
    
    Args:
        hwnd_str: Window handle as hexadecimal string
        filename: Optional filename to save screenshot to
        region: Optional region [x, y, width, height] relative to window
        
    Returns:
        Tuple of (success, message, base64_image_data)
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str)
            
        # Validate window handle
        if not win32gui.IsWindow(hwnd):
            return False, f"Invalid window handle: {hwnd_str}", None
            
        # Activate window first to ensure it's properly rendered
        success, _ = activate_window_functional(hwnd_str, request_focus=False)
        if not success:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not activate window before screenshot")
            
        # Small delay to allow window to be properly rendered
        time.sleep(0.3)
        
        # Get window dimensions and position
        rect = win32gui.GetWindowRect(hwnd)
        win_x, win_y = rect[0], rect[1]
        win_width = rect[2] - rect[0]
        win_height = rect[3] - rect[1]
        
        if win_width <= 0 or win_height <= 0:
            return False, f"Invalid window dimensions (width={win_width}, height={win_height})", None
            
        # Calculate region to capture (default: full window)
        capture_x = win_x
        capture_y = win_y
        capture_width = win_width
        capture_height = win_height
        
        # Process region if specified
        if region and len(region) == 4:
            x, y, width, height = region
            
            # Handle negative coordinates (relative to right/bottom)
            if x < 0:
                x = win_width + x
            if y < 0:
                y = win_height + y
                
            # Handle zero width/height (extend to edge)
            if width == 0:
                width = win_width - x
            if height == 0:
                height = win_height - y
                
            # Calculate absolute screen coordinates
            capture_x = win_x + x
            capture_y = win_y + y
            capture_width = width
            capture_height = height
            
            # Validate the region
            if width <= 0 or height <= 0:
                return False, f"Invalid region dimensions (width={width}, height={height})", None
                
        # Take screenshot using PIL's ImageGrab
        screenshot = ImageGrab.grab(bbox=(
            capture_x, 
            capture_y, 
            capture_x + capture_width, 
            capture_y + capture_height
        ))
        
        # Save to file if filename provided
        if filename:
            screenshot.save(filename)
            screenshot.close()
            MCPLogger.log(TOOL_LOG_NAME, f"Screenshot saved to {filename}")
            return True, f"Screenshot saved to {filename}", None
        else:
            # Convert to base64 for returning
            import io
            import base64
            
            buffer = io.BytesIO()
            screenshot.save(buffer, format='PNG')
            buffer.seek(0)
            base64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            screenshot.close()
            buffer.close()
            
            MCPLogger.log(TOOL_LOG_NAME, f"Screenshot captured in memory as base64 data")
            return True, "Screenshot captured successfully", base64_data
            
    except Exception as e:
        return False, f"Error taking screenshot: {e}", None

# ============================================================================
# AUTOHOTKEY-STYLE KEY PARSING FOR send_text
# ============================================================================
# Virtual Key Codes for special keys (Windows VK_ constants)
VIRTUAL_KEY_CODES = {
    # Function keys
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
    'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'f13': 0x7C, 'f14': 0x7D, 'f15': 0x7E, 'f16': 0x7F, 'f17': 0x80, 'f18': 0x81,
    'f19': 0x82, 'f20': 0x83, 'f21': 0x84, 'f22': 0x85, 'f23': 0x86, 'f24': 0x87,
    
    # Navigation keys
    'enter': 0x0D, 'return': 0x0D,
    'escape': 0x1B, 'esc': 0x1B,
    'tab': 0x09,
    'space': 0x20,
    'backspace': 0x08, 'bs': 0x08,
    'delete': 0x2E, 'del': 0x2E,
    'insert': 0x2D, 'ins': 0x2D,
    'home': 0x24, 'end': 0x23,
    'pageup': 0x21, 'pgup': 0x21,
    'pagedown': 0x22, 'pgdn': 0x22,
    
    # Arrow keys
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    
    # Modifier keys (for explicit down/up control)
    'ctrl': 0x11, 'control': 0x11, 'lctrl': 0xA2, 'rctrl': 0xA3,
    'lcontrol': 0xA2, 'rcontrol': 0xA3,
    'shift': 0x10, 'lshift': 0xA0, 'rshift': 0xA1,
    'alt': 0x12, 'lalt': 0xA4, 'ralt': 0xA5,
    'win': 0x5B, 'lwin': 0x5B, 'rwin': 0x5C,
    
    # Lock keys
    'capslock': 0x14, 'numlock': 0x90, 'scrolllock': 0x91,
    
    # Special keys
    'printscreen': 0x2C, 'pause': 0x13, 'break': 0x03,
    'apps': 0x5D, 'appskey': 0x5D,  # Context menu key
    'sleep': 0x5F,
    
    # Numpad keys
    'numpad0': 0x60, 'numpad1': 0x61, 'numpad2': 0x62, 'numpad3': 0x63,
    'numpad4': 0x64, 'numpad5': 0x65, 'numpad6': 0x66, 'numpad7': 0x67,
    'numpad8': 0x68, 'numpad9': 0x69,
    'numpadmult': 0x6A, 'numpadmul': 0x6A, 'numpad*': 0x6A,
    'numpadadd': 0x6B, 'numpad+': 0x6B,
    'numpadsub': 0x6D, 'numpad-': 0x6D,
    'numpaddot': 0x6E, 'numpad.': 0x6E,
    'numpaddiv': 0x6F, 'numpad/': 0x6F,
    'numpadenter': 0x0D,  # Same as Enter
    
    # Numpad navigation (NumLock off)
    'numpadins': 0x2D, 'numpaddel': 0x2E, 'numpadhome': 0x24, 'numpadend': 0x23,
    'numpadpgup': 0x21, 'numpadpgdn': 0x22, 'numpadup': 0x26, 'numpaddown': 0x28,
    'numpadleft': 0x25, 'numpadright': 0x27, 'numpadclear': 0x0C,
    
    # Browser/Media keys
    'browser_back': 0xA6, 'browser_forward': 0xA7, 'browser_refresh': 0xA8,
    'browser_stop': 0xA9, 'browser_search': 0xAA, 'browser_favorites': 0xAB,
    'browser_home': 0xAC,
    'volume_mute': 0xAD, 'volume_down': 0xAE, 'volume_up': 0xAF,
    'media_next': 0xB0, 'media_prev': 0xB1, 'media_stop': 0xB2, 'media_play_pause': 0xB3,
    'launch_mail': 0xB4, 'launch_media': 0xB5, 'launch_app1': 0xB6, 'launch_app2': 0xB7,
}

# Modifier key prefixes (AutoHotkey style)
MODIFIER_PREFIXES = {
    '^': 0x11,  # Ctrl
    '+': 0x10,  # Shift  
    '!': 0x12,  # Alt
    '#': 0x5B,  # Win
}

def parse_autohotkey_text_to_input_events(text: str) -> List[Tuple[str, int, Optional[int]]]:
    """Parse AutoHotkey-style text into a list of input events.
    
    Supports:
    - {Enter}, {Escape}, {Tab}, {F1}-{F24}, etc. - Special keys
    - {Key down}, {Key up} - Hold/release keys
    - {Key 5} - Repeat key 5 times
    - ^c, +a, !f, #r - Modifier+key (Ctrl+C, Shift+A, Alt+F, Win+R)
    - {^}, {+}, {!}, {#} - Literal modifier symbols
    - {{}  - Literal {
    - {}}  - Literal }
    - {Raw}text - Send remaining text literally (no parsing)
    - {Text}text - Same as {Raw}
    
    Returns:
        List of tuples: (event_type, vk_code_or_char, repeat_count)
        event_type: 'vk_press', 'vk_down', 'vk_up', 'unicode'
    """
    events = []
    i = 0
    raw_mode = False
    
    while i < len(text):
        # Raw mode - send everything literally
        if raw_mode:
            events.append(('unicode', ord(text[i]), None))
            i += 1
            continue
        
        char = text[i]
        
        # Check for modifier prefixes (^, +, !, #)
        if char in MODIFIER_PREFIXES and i + 1 < len(text):
            next_char = text[i + 1]
            
            # Check if next is a brace sequence like ^{Enter}
            if next_char == '{':
                # Find closing brace
                close_idx = text.find('}', i + 2)
                if close_idx != -1:
                    key_spec = text[i + 2:close_idx].lower().strip()
                    vk = VIRTUAL_KEY_CODES.get(key_spec)
                    if vk is not None:
                        # Modifier + special key
                        mod_vk = MODIFIER_PREFIXES[char]
                        events.append(('vk_down', mod_vk, None))
                        events.append(('vk_press', vk, None))
                        events.append(('vk_up', mod_vk, None))
                        i = close_idx + 1
                        continue
            
            # Modifier + single character
            if next_char not in '{':
                mod_vk = MODIFIER_PREFIXES[char]
                # Get VK code for the character (uppercase for letter keys)
                if next_char.isalpha():
                    char_vk = ord(next_char.upper())
                elif next_char.isdigit():
                    char_vk = ord(next_char)
                else:
                    # For other characters, use VkKeyScan to get the VK code
                    char_vk = None
                
                if char_vk:
                    events.append(('vk_down', mod_vk, None))
                    events.append(('vk_press', char_vk, None))
                    events.append(('vk_up', mod_vk, None))
                    i += 2
                    continue
        
        # Check for brace sequences {Key}
        if char == '{':
            close_idx = text.find('}', i + 1)
            if close_idx != -1:
                brace_content = text[i + 1:close_idx]
                
                # Escape sequences for literal braces and modifier symbols
                if brace_content == '{':
                    events.append(('unicode', ord('{'), None))
                    i = close_idx + 1
                    continue
                elif brace_content == '}':
                    events.append(('unicode', ord('}'), None))
                    i = close_idx + 1
                    continue
                elif brace_content == '^':
                    events.append(('unicode', ord('^'), None))
                    i = close_idx + 1
                    continue
                elif brace_content == '+':
                    events.append(('unicode', ord('+'), None))
                    i = close_idx + 1
                    continue
                elif brace_content == '!':
                    events.append(('unicode', ord('!'), None))
                    i = close_idx + 1
                    continue
                elif brace_content == '#':
                    events.append(('unicode', ord('#'), None))
                    i = close_idx + 1
                    continue
                
                # Raw/Text mode
                if brace_content.lower() in ('raw', 'text'):
                    raw_mode = True
                    i = close_idx + 1
                    continue
                
                # Parse key specification (may include "down", "up", or repeat count)
                parts = brace_content.split()
                key_name = parts[0].lower()
                modifier = parts[1].lower() if len(parts) > 1 else None
                
                # Check for repeat count (e.g., {Tab 5})
                repeat_count = 1
                if modifier and modifier.isdigit():
                    repeat_count = int(modifier)
                    modifier = None
                elif len(parts) > 2 and parts[1].isdigit():
                    repeat_count = int(parts[1])
                
                vk = VIRTUAL_KEY_CODES.get(key_name)
                if vk is not None:
                    if modifier == 'down':
                        events.append(('vk_down', vk, None))
                    elif modifier == 'up':
                        events.append(('vk_up', vk, None))
                    else:
                        for _ in range(repeat_count):
                            events.append(('vk_press', vk, None))
                    i = close_idx + 1
                    continue
                
                # Unknown key in braces - send literally
                for c in brace_content:
                    events.append(('unicode', ord(c), None))
                i = close_idx + 1
                continue
        
        # Regular character - send as Unicode
        events.append(('unicode', ord(char), None))
        i += 1
    
    return events


def send_text_functional(hwnd_str: str, text: str) -> Tuple[bool, str]:
    """Send text/keystrokes to a window using AutoHotkey-style syntax.
    
    Supports AutoHotkey-style special key sequences:
    
    SPECIAL KEYS (in braces):
        {Enter}, {Tab}, {Escape}/{Esc}, {Space}, {Backspace}/{BS}
        {Delete}/{Del}, {Insert}/{Ins}, {Home}, {End}, {PageUp}/{PgUp}, {PageDown}/{PgDn}
        {Up}, {Down}, {Left}, {Right} - Arrow keys
        {F1} through {F24} - Function keys
        {PrintScreen}, {Pause}, {CapsLock}, {NumLock}, {ScrollLock}
        {Numpad0}-{Numpad9}, {NumpadMult}, {NumpadAdd}, {NumpadSub}, {NumpadDiv}, {NumpadDot}, {NumpadEnter}
        {Browser_Back}, {Volume_Mute}, {Media_Play_Pause}, etc. - Media keys
    
    MODIFIER PREFIXES (outside braces):
        ^ = Ctrl    (e.g., ^c = Ctrl+C, ^s = Ctrl+S)
        + = Shift   (e.g., +a = Shift+A = uppercase A)
        ! = Alt     (e.g., !{F4} = Alt+F4)
        # = Win     (e.g., #r = Win+R to open Run dialog)
        
        Combine modifiers: ^+s = Ctrl+Shift+S, ^!{Delete} = Ctrl+Alt+Delete
    
    KEY REPETITION:
        {Tab 5} - Press Tab 5 times
        {Enter 3} - Press Enter 3 times
    
    KEY HOLD/RELEASE:
        {Ctrl down} - Hold Ctrl key down
        {Ctrl up} - Release Ctrl key
        {Shift down}hello{Shift up} - Type "HELLO"
    
    LITERAL CHARACTERS (escaping):
        {{}  - Literal { character
        {}}  - Literal } character
        {^}  - Literal ^ character
        {+}  - Literal + character
        {!}  - Literal ! character
        {#}  - Literal # character
    
    RAW MODE:
        {Raw}text - Send all remaining text literally without parsing
        {Text}text - Same as {Raw}
    
    EXAMPLES:
        "Hello{Enter}"           - Type "Hello" then press Enter
        "^a^c"                   - Ctrl+A (select all), Ctrl+C (copy)
        "!{F4}"                  - Alt+F4 (close window)
        "#r"                     - Win+R (open Run dialog)
        "{Tab 3}submit"          - Press Tab 3 times, then type "submit"
        "Price: $100{!}"         - Type "Price: $100!" (! is literal in braces)
        "{Ctrl down}ac{Ctrl up}" - Hold Ctrl, press A then C, release Ctrl
        "{Raw}^not a hotkey^"    - Sends literal "^not a hotkey^"
    
    Args:
        hwnd_str: Window handle as hexadecimal string (e.g., "0x00020828")
        text: Text/keystrokes to send using AutoHotkey-style syntax
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str)
            
        # Validate window handle
        if not win32gui.IsWindow(hwnd):
            return False, f"Invalid window handle: {hwnd_str}"
            
        # Activate window first
        success, _ = activate_window_functional(hwnd_str, request_focus=True)
        if not success:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not activate window before sending text")
            
        # Small delay to ensure window has focus
        time.sleep(0.2)
        
        # Parse the AutoHotkey-style text into input events
        parsed_events = parse_autohotkey_text_to_input_events(text)
        
        # B3: optionally refuse Win-key (Super) chords such as #r (Win+R) / #e, which let an
        # injected caller open Run/Explorer and pivot to whole-desktop control. Only enforced
        # when an operator sets system_tool_security.allow_win_key_chords=false; the check is
        # parser-accurate (VK_LWIN 0x5B / VK_RWIN 0x5C) so a literal '{#}' is unaffected.
        if not _capability_is_allowed(_get_system_tool_security_policy(), "allow_win_key_chords"):
            win_key_virtual_codes = {0x5B, 0x5C}
            if any(event_type in ("vk_press", "vk_down", "vk_up") and code in win_key_virtual_codes
                   for event_type, code, _ in parsed_events):
                return False, ("Win-key (Super) chords are disabled by this server's system_tool_security "
                               "policy (allow_win_key_chords=false).")
        
        # Prepare input array for SendInput
        inputs = []
        
        for event_type, code, _ in parsed_events:
            if event_type == 'unicode':
                # Unicode character - use scan code
                key_input = INPUT_FULL()
                key_input.type = 1  # INPUT_KEYBOARD
                key_input.u.ki.wVk = 0
                key_input.u.ki.wScan = code
                key_input.u.ki.dwFlags = KEYEVENTF_UNICODE
                key_input.u.ki.time = 0
                key_input.u.ki.dwExtraInfo = 0
                inputs.append(key_input)
                
                # Key up event
                key_input_up = INPUT_FULL()
                key_input_up.type = 1  # INPUT_KEYBOARD
                key_input_up.u.ki.wVk = 0
                key_input_up.u.ki.wScan = code
                key_input_up.u.ki.dwFlags = KEYEVENTF_UNICODE | 0x0002  # KEYEVENTF_KEYUP
                key_input_up.u.ki.time = 0
                key_input_up.u.ki.dwExtraInfo = 0
                inputs.append(key_input_up)
                
            elif event_type == 'vk_press':
                # Virtual key press (down + up)
                key_input = INPUT_FULL()
                key_input.type = 1  # INPUT_KEYBOARD
                key_input.u.ki.wVk = code
                key_input.u.ki.wScan = 0
                key_input.u.ki.dwFlags = 0  # Key down
                key_input.u.ki.time = 0
                key_input.u.ki.dwExtraInfo = 0
                inputs.append(key_input)
                
                key_input_up = INPUT_FULL()
                key_input_up.type = 1  # INPUT_KEYBOARD
                key_input_up.u.ki.wVk = code
                key_input_up.u.ki.wScan = 0
                key_input_up.u.ki.dwFlags = 0x0002  # KEYEVENTF_KEYUP
                key_input_up.u.ki.time = 0
                key_input_up.u.ki.dwExtraInfo = 0
                inputs.append(key_input_up)
                
            elif event_type == 'vk_down':
                # Virtual key down only
                key_input = INPUT_FULL()
                key_input.type = 1  # INPUT_KEYBOARD
                key_input.u.ki.wVk = code
                key_input.u.ki.wScan = 0
                key_input.u.ki.dwFlags = 0  # Key down
                key_input.u.ki.time = 0
                key_input.u.ki.dwExtraInfo = 0
                inputs.append(key_input)
                
            elif event_type == 'vk_up':
                # Virtual key up only
                key_input_up = INPUT_FULL()
                key_input_up.type = 1  # INPUT_KEYBOARD
                key_input_up.u.ki.wVk = code
                key_input_up.u.ki.wScan = 0
                key_input_up.u.ki.dwFlags = 0x0002  # KEYEVENTF_KEYUP
                key_input_up.u.ki.time = 0
                key_input_up.u.ki.dwExtraInfo = 0
                inputs.append(key_input_up)
            
        # Send all input events
        if inputs:
            input_array = (INPUT_FULL * len(inputs))(*inputs)
            sent_count = user32.SendInput(len(inputs), input_array, ctypes.sizeof(INPUT_FULL))
            
            if sent_count != len(inputs):
                return False, f"SendInput failed: sent {sent_count} of {len(inputs)} events"
                
        # Create a display-friendly version of the text for the caller-facing message only.
        # B5: the server log must not contain the typed content (may be passwords/secrets),
        # so it records a redacted descriptor instead of the text itself.
        display_text = text[:50] + "..." if len(text) > 50 else text
        MCPLogger.log(TOOL_LOG_NAME, f"Sent text/keys: {_redact_sensitive_for_log(text)} to window 0x{hwnd:08X}")
        return True, f"Successfully sent text '{display_text}' to window 0x{hwnd:08X}"
        
    except Exception as e:
        return False, f"Error sending text: {e}"

def click_ui_element_functional(hwnd_str: str, element_name: str) -> Tuple[bool, str]:
    """Click on a UI element by name or AutomationId within a window.
    
    Args:
        hwnd_str: Window handle as hexadecimal string
        element_name: Name or AutomationId of UI element to click
        
    Returns:
        Tuple of (success, message)
    """
    try:
        # Convert hex string to integer
        if hwnd_str.startswith('0x') or hwnd_str.startswith('0X'):
            hwnd = int(hwnd_str, 16)
        else:
            hwnd = int(hwnd_str)
            
        # Validate window handle
        if not win32gui.IsWindow(hwnd):
            return False, f"Invalid window handle: {hwnd_str}"
        
        # Activate window first to ensure UI element clicks work properly
        success, msg = activate_window_functional(hwnd_str, request_focus=True)
        if not success:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not activate window before clicking UI element: {msg}")
        
        # Small delay to ensure window has focus before clicking
        time.sleep(0.2)
            
        # Initialize COM for UI automation
        pythoncom.CoInitialize()
        
        try:
            # Find the window
            window_title = win32gui.GetWindowText(hwnd)
            target_window = auto.WindowControl(searchDepth=1, Name=window_title)
            if not target_window.Exists():
                return False, f"Could not find window with title: '{window_title}'"
                
            # Try to find element by name first
            element = target_window.ButtonControl(Name=element_name)
            if not element.Exists():
                # Try by AutomationId
                element = target_window.ButtonControl(AutomationId=element_name)
                if not element.Exists():
                    # Try other control types
                    element = target_window.Control(Name=element_name)
                    if not element.Exists():
                        element = target_window.Control(AutomationId=element_name)
                        if not element.Exists():
                            return False, f"Could not find UI element with name/ID: '{element_name}'"
                            
            # Click the element
            element.Click()
            
            MCPLogger.log(TOOL_LOG_NAME, f"Clicked UI element '{element_name}' in window 0x{hwnd:08X}")
            return True, f"Successfully clicked UI element '{element_name}' in window 0x{hwnd:08X}"
            
        finally:
            # Clean up COM
            try:
                pythoncom.CoUninitialize()
            except:
                pass
                
    except Exception as e:
        return False, f"Error clicking UI element: {e}"

# ============================================================================
# TERMINAL COMMAND EXECUTION FUNCTIONAL IMPLEMENTATIONS
# ============================================================================

def execute_command_functional(command: str, timeout_ms: int = 30000, shell: Optional[str] = None, owner_user: Optional[str] = None) -> Dict[str, any]:
    """Execute a terminal command with timeout support, allowing it to continue in background.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        command: The command to execute
        timeout_ms: Timeout in milliseconds for initial output collection
        shell: Optional shell to use for execution
        owner_user: Authenticated user that owns the resulting session (B6). Only this user
            may later read_output/force_terminate/list it; None when auth is not configured.
        
    Returns:
        Dictionary containing execution result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Executing command: {_redact_sensitive_for_log(command)}")
        
        # Execute the command
        result = _global_terminal_session_manager.start_command_execution_with_timeout_and_background_support(
            command_text=command,
            timeout_milliseconds=timeout_ms,
            shell_path=shell,
            owner_user=owner_user
        )
        
        # Check for errors
        if result.error_message:
            return {
                "success": False,
                "error": result.error_message,
                "session_id": result.process_id
            }
        
        # Format success response
        response = {
            "success": True,
            "session_id": result.process_id,
            "initial_output": result.initial_output_text,
            "is_running": result.command_is_still_running_in_background,
            "message": f"Command started with session ID {result.process_id}"
        }
        
        if result.command_is_still_running_in_background:
            response["message"] += "\nCommand is still running. Use read_output to get more output."
        
        return response
        
    except Exception as e:
        error_msg = f"Error executing command: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "session_id": -1
        }

def read_output_functional(session_id: int, timeout_ms: int = 5000, requesting_user: Optional[str] = None) -> Dict[str, any]:
    """Read new output from a running command session.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        session_id: The session ID returned from execute_command
        timeout_ms: Timeout in milliseconds to wait for new output
        requesting_user: Authenticated caller (B6); must match the session owner.
        
    Returns:
        Dictionary containing output result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Reading output from session {session_id}")
        
        # B6: refuse to read a session owned by a different authenticated user.
        if not _global_terminal_session_manager.session_is_accessible_by_user(session_id, requesting_user):
            return {
                "success": False,
                "error": f"No session found for ID {session_id}",
                "session_id": session_id
            }
        
        # Read output from the session
        output, timeout_reached = _global_terminal_session_manager.read_new_output_from_session_with_timeout(
            session_id=session_id,
            timeout_milliseconds=timeout_ms
        )
        
        return {
            "success": True,
            "session_id": session_id,
            "output": output,
            "timeout_reached": timeout_reached,
            "has_output": len(output.strip()) > 0
        }
        
    except Exception as e:
        error_msg = f"Error reading output from session {session_id}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "session_id": session_id
        }

def force_terminate_functional(session_id: int, requesting_user: Optional[str] = None) -> Dict[str, any]:
    """Force terminate a running command session.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        session_id: The session ID to terminate
        requesting_user: Authenticated caller (B6); must match the session owner.
        
    Returns:
        Dictionary containing termination result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Force terminating session {session_id}")
        
        # B6: refuse to terminate a session owned by a different authenticated user. Report it
        # as "no active session" so a caller cannot probe for other users' session ids.
        if not _global_terminal_session_manager.session_is_accessible_by_user(session_id, requesting_user):
            return {
                "success": False,
                "session_id": session_id,
                "error": f"No active session found for ID {session_id}"
            }
        
        # Terminate the session
        success = _global_terminal_session_manager.force_terminate_session_with_cleanup(session_id)
        
        if success:
            return {
                "success": True,
                "session_id": session_id,
                "message": f"Successfully terminated session {session_id}"
            }
        else:
            return {
                "success": False,
                "session_id": session_id,
                "error": f"No active session found for ID {session_id}"
            }
        
    except Exception as e:
        error_msg = f"Error terminating session {session_id}: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "session_id": session_id
        }

def list_sessions_functional(requesting_user: Optional[str] = None) -> Dict[str, any]:
    """List all active command sessions.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        requesting_user: Authenticated caller (B6); only sessions owned by this user are listed.
    
    Returns:
        Dictionary containing list of active sessions
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Listing active sessions")
        
        # Get list of active sessions owned by the requesting user (B6)
        sessions = _global_terminal_session_manager.get_list_of_all_active_sessions_with_status(requesting_user=requesting_user)
        
        return {
            "success": True,
            "active_sessions": sessions,
            "total_sessions": len(sessions)
        }
        
    except Exception as e:
        error_msg = f"Error listing sessions: {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "active_sessions": []
        }

# ============================================================================
# FILE OPERATIONS FUNCTIONAL IMPLEMENTATIONS
# ============================================================================

def resolve_file_path(path: str) -> str:
    """Resolve a file path to an absolute path.
    
    If the path is relative, it's resolved relative to the user_data directory.
    If the path is absolute, it's returned as-is.
    
    Args:
        path: File path (absolute or relative)
        
    Returns:
        Absolute file path
    """
    import os
    
    # Check if path is absolute
    if os.path.isabs(path):
        return os.path.normpath(path)
    else:
        # Resolve relative to user_data directory from shared_config
        user_data_dir = get_user_data_directory()
        return os.path.normpath(os.path.join(str(user_data_dir), path))

def write_file_functional(path: str, content: str) -> Tuple[bool, str, Optional[str]]:
    """Write content to a file on the local filesystem.
    
    Args:
        path: File path (absolute or relative to user_data directory)
        content: Content to write to the file
        
    Returns:
        Tuple of (success, message, absolute_path) where:
        - success: True if file was written successfully
        - message: Success or error message
        - absolute_path: The absolute path where the file was written
    """
    try:
        import os
        
        # Resolve path
        absolute_path = resolve_file_path(path)
        
        # B2: enforce the optional file-access jail before creating dirs or writing anything.
        path_is_allowed, path_denial_reason = _verify_file_path_within_policy(absolute_path, path)
        if not path_is_allowed:
            MCPLogger.log(TOOL_LOG_NAME, f"write_file denied by file-access policy: {path_denial_reason}")
            return False, path_denial_reason, None
        
        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(absolute_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
            MCPLogger.log(TOOL_LOG_NAME, f"Created parent directory: {parent_dir}")
        
        # Write the file
        with open(absolute_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        file_size_bytes = os.path.getsize(absolute_path)
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully wrote file: {absolute_path} ({file_size_bytes} bytes)")
        
        return True, f"Successfully wrote {file_size_bytes} bytes to: {absolute_path}", absolute_path
        
    except Exception as e:
        error_msg = f"Error writing file '{path}': {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return False, error_msg, None

def read_file_functional(path: str) -> Tuple[bool, str, Optional[str]]:
    """Read the entire contents of a file from the local filesystem.
    
    Args:
        path: File path (absolute or relative to user_data directory)
        
    Returns:
        Tuple of (success, message_or_content, absolute_path) where:
        - success: True if file was read successfully
        - message_or_content: File content if success=True, error message if success=False
        - absolute_path: The absolute path that was read
    """
    try:
        import os
        
        # Resolve path
        absolute_path = resolve_file_path(path)
        
        # B2: enforce the optional file-access jail before disclosing any file contents.
        path_is_allowed, path_denial_reason = _verify_file_path_within_policy(absolute_path, path)
        if not path_is_allowed:
            MCPLogger.log(TOOL_LOG_NAME, f"read_file denied by file-access policy: {path_denial_reason}")
            return False, path_denial_reason, absolute_path
        
        # Check if file exists
        if not os.path.exists(absolute_path):
            return False, f"File not found: {absolute_path}", absolute_path
        
        if not os.path.isfile(absolute_path):
            return False, f"Path is not a file: {absolute_path}", absolute_path
        
        # Read the file
        with open(absolute_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        file_size_bytes = os.path.getsize(absolute_path)
        MCPLogger.log(TOOL_LOG_NAME, f"Successfully read file: {absolute_path} ({file_size_bytes} bytes)")
        
        return True, content, absolute_path
        
    except Exception as e:
        error_msg = f"Error reading file '{path}': {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return False, error_msg, None


def handle_write_file(params: Dict) -> Dict:
    """Handle write_file operation"""
    try:
        path = params.get('path')
        content = params.get('content')
        
        if not path:
            return create_error_response("Missing required parameter: path", with_readme=False)
        if content is None:
            return create_error_response("Missing required parameter: content", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing write_file: path='{path}', content_length={len(content)}")
        
        success, message, absolute_path = write_file_functional(path, content)
        
        if success:
            return {
                "content": [{"type": "text", "text": message}],
                "isError": False,
                "absolute_path": absolute_path,
                "bytes_written": len(content)
            }
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling write_file: {e}", with_readme=False)

def handle_read_file(params: Dict) -> Dict:
    """Handle read_file operation"""
    try:
        path = params.get('path')
        
        if not path:
            return create_error_response("Missing required parameter: path", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing read_file: path='{path}'")
        
        success, message_or_content, absolute_path = read_file_functional(path)
        
        if success:
            # message_or_content is the file content on success
            file_content = message_or_content
            return {
                "content": [{"type": "text", "text": file_content}],
                "isError": False,
                "absolute_path": absolute_path,
                "bytes_read": len(file_content)
            }
        else:
            # message_or_content is an error message on failure
            return create_error_response(message_or_content, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling read_file: {e}", with_readme=False)


# ============================================================================
# MCP INTERFACE CODE
# ============================================================================

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema."""
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]
    
    # Check for unexpected parameters
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        return f"Unexpected parameters: {', '.join(sorted(unexpected_params))}. Expected: {', '.join(sorted(expected_params))}", {}
    
    # Check for missing required parameters
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}", {}
    
    # Validate types and extract values
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            # Type validation. bool is a subclass of int in Python, so integer/number checks
            # explicitly exclude bool - otherwise coordinates/sizes could be passed as True/False.
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}", {}
            elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}", {}
            elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}", {}
            
            # Enum validation
            if "enum" in param_schema:
                allowed_values = param_schema["enum"]
                if value not in allowed_values:
                    return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'", {}
            
            validated[param_name] = value
        elif param_name in required:
            return f"Required parameter '{param_name}' is missing", {}
        else:
            # Use default value if specified
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

def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    """Create an error response that optionally includes the tool documentation."""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

def create_success_response(success_msg: str, **extra_fields) -> Dict:
    """Create a success response in the correct MCP format.
    
    IMPORTANT: Cursor only passes content[0].text to the AI, not extra fields.
    So we serialize any structured data (overview, processes, etc.) into the text
    field as JSON so the AI can actually see and use it.
    """
    # Check if there's structured data that needs to be visible to the AI
    # These are the fields that contain the actual useful data from about operation
    structured_data_fields = ['overview', 'processes', 'applications', 'section_data', 
                              'network_data', 'system_info', 'process_detail', 
                              'matching_applications', 'summary_stats', 'pagination']
    
    has_structured_data = any(field in extra_fields for field in structured_data_fields)
    
    if has_structured_data:
        # Build a combined response: brief header + JSON data
        # The AI needs to see this data to be useful
        data_to_serialize = {k: v for k, v in extra_fields.items() 
                            if k not in ['isError']}  # Keep all metadata + data
        try:
            json_data = json.dumps(data_to_serialize, indent=2, default=str)
            text_content = f"{success_msg}\n\n{json_data}"
        except Exception:
            text_content = success_msg
    else:
        text_content = success_msg
    
    response = {"content": [{"type": "text", "text": text_content}], "isError": False}
    response.update(extra_fields)  # Still include extra fields for programmatic access
    return response

def handle_list_windows(params: Dict) -> Dict:
    """Handle list_windows operation."""
    try:
        # Extract parameters
        include_all = params.get("include_all", False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Processing list_windows: include_all={include_all}")
        
        # Call the functional implementation
        windows = list_windows_functional(include_all=include_all)
        
        # Format response
        response_text = f"Found {len(windows)} windows:\n\n"
        response_text += json.dumps(windows, indent=2)
        
        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": False
        }
        
    except ValueError as e:
        return create_error_response(f"Invalid parameter: {str(e)}", with_readme=True)
    except Exception as e:
        return create_error_response(f"Error listing windows: {str(e)}", with_readme=True)

def handle_activate_window(params: Dict) -> Dict:
    """Handle activate_window operation."""
    try:
        # Extract required parameter
        hwnd_str = params.get("hwnd")
        if not hwnd_str:
            return create_error_response("Missing required parameter 'hwnd'", with_readme=True)
        
        # Extract optional parameter
        request_focus = params.get("request_focus", False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Processing activate_window: hwnd={hwnd_str}, request_focus={request_focus}")
        
        # Call the functional implementation
        success, message = activate_window_functional(hwnd_str, request_focus=request_focus)
        
        # Format response
        if success:
            return {
                "content": [{"type": "text", "text": message}],
                "isError": False
            }
        else:
            return create_error_response(message, with_readme=False)
        
    except ValueError as e:
        return create_error_response(f"Invalid parameter: {str(e)}", with_readme=True)
    except Exception as e:
        return create_error_response(f"Error activating window: {str(e)}", with_readme=True)

def handle_scan_ui_elements(params: Dict, session_key: object = None) -> Dict:
    """Handle scan_ui_elements operation."""
    try:
        # Extract parameters
        window_title = params.get("window_title")
        hwnd_str = params.get("hwnd")
        
        # Validate that exactly one parameter is provided
        if not window_title and not hwnd_str:
            return create_error_response("Either 'window_title' or 'hwnd' parameter is required", with_readme=True)
        
        if window_title and hwnd_str:
            return create_error_response("Cannot specify both 'window_title' and 'hwnd' parameters. Use one or the other.", with_readme=True)
        
        if window_title:
            MCPLogger.log(TOOL_LOG_NAME, f"Processing scan_ui_elements: window_title='{window_title}'")
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Processing scan_ui_elements: hwnd='{hwnd_str}'")
        
        # Call the functional implementation (session_key isolates this caller's scanner)
        scan_result = scan_ui_elements_functional(window_title=window_title, hwnd_str=hwnd_str, session_key=session_key)
        
        # Check for errors in the scan result
        if "error" in scan_result:
            return create_error_response(scan_result["error"], with_readme=False)
        
        # Format response
        window_info = scan_result.get('window_info', {})
        if window_title:
            response_text = f"UI scan completed for window: {window_info.get('title', window_title)}\n\n"
        else:
            response_text = f"UI scan completed for window handle {hwnd_str}: {window_info.get('title', 'Unknown')}\n\n"
        
        response_text += f"Found {len(scan_result.get('extracted_ui_elements', []))} UI elements\n\n"
        response_text += json.dumps(scan_result, indent=2)
        
        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": False
        }
        
    except ValueError as e:
        return create_error_response(f"Invalid parameter: {str(e)}", with_readme=True)
    except Exception as e:
        return create_error_response(f"Error scanning UI elements: {str(e)}", with_readme=True)

def handle_get_clickable_elements(params: Dict, session_key: object = None) -> Dict:
    """Handle get_clickable_elements operation."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Processing get_clickable_elements")
        
        # Call the functional implementation (session_key selects this caller's own scan)
        clickable_result = get_clickable_elements_functional(session_key=session_key)
        
        # Check for errors in the result
        if "error" in clickable_result:
            return create_error_response(clickable_result["error"], with_readme=False)
        
        # Format response
        clickable_elements = clickable_result.get("clickable_elements", [])
        response_text = f"Found {len(clickable_elements)} clickable elements from last scan:\n\n"
        response_text += json.dumps(clickable_result, indent=2)
        
        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error getting clickable elements: {str(e)}", with_readme=True)

def move_windows_batch_functional(moves: list) -> Tuple[bool, str, list]:
    """Move multiple windows in a batch operation.
    
    Args:
        moves: List of dictionaries, each containing hwnd, x, y, width, height
        
    Returns:
        Tuple of (overall_success, summary_message, detailed_results) where:
        - overall_success: True if all moves succeeded
        - summary_message: Summary of operation results
        - detailed_results: List of individual move results
    """
    results = []
    successful_moves = 0
    failed_moves = 0
    
    MCPLogger.log(TOOL_LOG_NAME, f"Processing batch move of {len(moves)} windows")
    
    for i, move in enumerate(moves):
        try:
            # Convert hwnd to string if it's an integer
            hwnd = move['hwnd']
            if isinstance(hwnd, int):
                hwnd_str = f"0x{hwnd:08X}"
            else:
                hwnd_str = str(hwnd)
            
            x = move['x']
            y = move['y'] 
            width = move['width']
            height = move['height']
            
            MCPLogger.log(TOOL_LOG_NAME, f"Batch move {i+1}/{len(moves)}: hwnd={hwnd_str}, x={x}, y={y}, width={width}, height={height}")
            
            # Call the functional implementation for this move
            success, message = move_window_functional(hwnd_str, x, y, width, height)
            
            results.append({
                "move_index": i + 1,
                "hwnd": hwnd_str,
                "target_position": {"x": x, "y": y, "width": width, "height": height},
                "success": success,
                "message": message
            })
            
            if success:
                successful_moves += 1
            else:
                failed_moves += 1
                
        except Exception as e:
            failed_moves += 1
            results.append({
                "move_index": i + 1,
                "hwnd": move.get('hwnd', 'unknown'),
                "target_position": {"x": move.get('x'), "y": move.get('y'), "width": move.get('width'), "height": move.get('height')},
                "success": False,
                "message": f"Error processing move: {e}"
            })
    
    overall_success = failed_moves == 0
    summary = f"Batch move completed: {successful_moves} successful, {failed_moves} failed out of {len(moves)} total moves"
    
    MCPLogger.log(TOOL_LOG_NAME, summary)
    return overall_success, summary, results

def handle_move_window(params: Dict) -> Dict:
    """Handle move_window operation - supports both single window and batch moves."""
    try:
        # Check if this is a batch move operation
        moves = params.get("moves")
        
        if moves:
            # Batch move mode
            if not isinstance(moves, list) or len(moves) == 0:
                return create_error_response("Parameter 'moves' must be a non-empty array", with_readme=True)
            
            # Validate each move in the batch
            for i, move in enumerate(moves):
                if not isinstance(move, dict):
                    return create_error_response(f"Move {i+1} must be an object with hwnd, x, y, width, height", with_readme=True)
                
                required_fields = ['hwnd', 'x', 'y', 'width', 'height']
                for field in required_fields:
                    if field not in move:
                        return create_error_response(f"Move {i+1} missing required field '{field}'", with_readme=True)
            
            # Process batch moves
            overall_success, summary, results = move_windows_batch_functional(moves)
            
            # Format response
            response_content = [{"type": "text", "text": summary}]
            
            # Add detailed results
            for result in results:
                status = "✓" if result["success"] else "✗"
                detail_text = f"{status} Move {result['move_index']}: {result['hwnd']} -> ({result['target_position']['x']}, {result['target_position']['y']}, {result['target_position']['width']}x{result['target_position']['height']}) - {result['message']}"
                response_content.append({"type": "text", "text": detail_text})
            
            return {
                "content": response_content,
                "isError": not overall_success,
                "batch_results": {
                    "summary": summary,
                    "overall_success": overall_success,
                    "individual_results": results
                }
            }
        
        else:
            # Single window move mode (existing functionality)
            hwnd_str = params.get("hwnd")
            x = params.get("x")
            y = params.get("y")
            width = params.get("width")
            height = params.get("height")
            
            # Validate required parameters
            if not hwnd_str:
                return create_error_response("Missing required parameter 'hwnd'", with_readme=True)
            if x is None:
                return create_error_response("Missing required parameter 'x'", with_readme=True)
            if y is None:
                return create_error_response("Missing required parameter 'y'", with_readme=True)
            if width is None:
                return create_error_response("Missing required parameter 'width'", with_readme=True)
            if height is None:
                return create_error_response("Missing required parameter 'height'", with_readme=True)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Processing single move_window: hwnd={hwnd_str}, x={x}, y={y}, width={width}, height={height}")
            
            # Call the functional implementation
            success, message = move_window_functional(hwnd_str, x, y, width, height)
            
            # Format response
            if success:
                return {
                    "content": [{"type": "text", "text": message}],
                    "isError": False
                }
            else:
                return create_error_response(message, with_readme=False)
        
    except ValueError as e:
        return create_error_response(f"Invalid parameter: {str(e)}", with_readme=True)
    except Exception as e:
        return create_error_response(f"Error moving window: {str(e)}", with_readme=True)

def handle_system(input_param: Dict) -> Dict:
    """Handle system tool operations via MCP interface."""
    try:
        # Read the synthetic handler_info from our own shallow copy via .get, so we never
        # mutate the dict the server/caller passed in (the previous .pop mutated it in place).
        if isinstance(input_param, dict):
            input_param = dict(input_param)
        handler_info = input_param.get('handler_info', None) if isinstance(input_param, dict) else None
        # Per-caller key used to isolate this session's UI scanner from other concurrent callers'
        session_scanner_key = handler_info.get('session_id') if isinstance(handler_info, dict) else None
        # B6: the authenticated caller (or None when auth is not configured / internal call).
        # Terminal sessions are owned by, and only reachable by, this user.
        authenticated_user = get_authenticated_user(handler_info) if isinstance(handler_info, dict) else None
        
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
            return create_error_response("Invalid or missing tool_unlock_token. Please call with operation='readme' first to get the token.", with_readme=True)

        # Validate all parameters
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract operation
        operation = validated_params.get("operation")
        
        # Gate GUI operations by the platforms that actually implement them, so an unsupported op
        # returns a clear "not implemented on <os>" message instead of falling through to
        # platform-specific code and raising an opaque "name 'win32gui' is not defined".
        # platform.system() values: 'Windows', 'Darwin' (macOS), 'Linux'.
        gui_operation_supported_platforms = {
            "scan_ui_elements": {"Windows", "Darwin"},
            "get_clickable_elements": {"Windows", "Darwin"},
            "click_at_coordinates": {"Windows", "Darwin"},
            "click_at_screen_coordinates": {"Windows", "Darwin"},
            "send_text": {"Windows", "Darwin"},
            "click_ui_element": {"Windows", "Darwin"},
            "move_window": {"Windows", "Darwin", "Linux"},
        }
        operation_supported_platforms = gui_operation_supported_platforms.get(operation)
        if operation_supported_platforms is not None and CURRENT_PLATFORM not in operation_supported_platforms:
            supported_platforms_human_readable = ", ".join(sorted(operation_supported_platforms))
            return create_error_response(
                f"Operation '{operation}' is not implemented on {CURRENT_PLATFORM}; "
                f"it is currently available on: {supported_platforms_human_readable}.",
                with_readme=False,
            )
        
        # B1-B4: optional operator security policy. High-blast-radius capabilities (arbitrary
        # command execution, file read/write, keyboard/mouse injection, screen capture) can be
        # individually disabled via settings[0].system_tool_security. Everything is allowed by
        # default, so this is a no-op unless an operator opts in to locking the tool down.
        capability_gate_by_operation = {
            "execute_command": ("allow_command_execution", "arbitrary command execution"),
            "write_file": ("allow_file_write", "file writing"),
            "read_file": ("allow_file_read", "file reading"),
            "send_text": ("allow_input_injection", "keyboard/text injection"),
            "click_at_coordinates": ("allow_input_injection", "mouse click injection"),
            "click_at_screen_coordinates": ("allow_input_injection", "mouse click injection"),
            "click_ui_element": ("allow_input_injection", "mouse click injection"),
            "take_screenshot": ("allow_screen_capture", "screen capture"),
        }
        gated_capability = capability_gate_by_operation.get(operation)
        if gated_capability is not None:
            capability_flag_name, human_readable_capability = gated_capability
            if not _capability_is_allowed(_get_system_tool_security_policy(), capability_flag_name):
                MCPLogger.log(TOOL_LOG_NAME, f"Operation '{operation}' refused by security policy ({capability_flag_name}=false)")
                return create_error_response(
                    f"Operation '{operation}' ({human_readable_capability}) is disabled by this server's "
                    f"system_tool_security policy (settings[0].system_tool_security.{capability_flag_name}=false).",
                    with_readme=False,
                )
        
        # Handle operations
        if operation == "list_windows":
            return handle_list_windows(validated_params)
        elif operation == "activate_window":
            return handle_activate_window(validated_params)
        elif operation == "scan_ui_elements":
            return handle_scan_ui_elements(validated_params, session_scanner_key)
        elif operation == "get_clickable_elements":
            return handle_get_clickable_elements(validated_params, session_scanner_key)
        elif operation == "move_window":
            return handle_move_window(validated_params)
        elif operation == "click_at_coordinates":
            return handle_click_at_coordinates(validated_params)
        elif operation == "click_at_screen_coordinates":
            return handle_click_at_screen_coordinates(validated_params)
        elif operation == "take_screenshot":
            return handle_take_screenshot(validated_params)
        elif operation == "send_text":
            return handle_send_text(validated_params)
        elif operation == "click_ui_element":
            return handle_click_ui_element(validated_params)
        elif operation == "about":
            return handle_about(validated_params)
        elif operation == "execute_command":
            return handle_execute_command(validated_params, authenticated_user)
        elif operation == "read_output":
            return handle_read_output(validated_params, authenticated_user)
        elif operation == "force_terminate":
            return handle_force_terminate(validated_params, authenticated_user)
        elif operation == "list_sessions":
            return handle_list_sessions(validated_params, authenticated_user)
        elif operation == "write_file":
            return handle_write_file(validated_params)
        elif operation == "read_file":
            return handle_read_file(validated_params)
        elif operation == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in system operation: {str(e)}", with_readme=True)

def handle_click_at_coordinates(params: Dict) -> Dict:
    """Handle click_at_coordinates operation"""
    try:
        hwnd = params.get('hwnd')
        x_coordinate = params.get('x_coordinate')
        y_coordinate = params.get('y_coordinate')
        button = params.get('button', 'left')
        
        if not hwnd:
            return create_error_response("Missing required parameter: hwnd", with_readme=False)
        if x_coordinate is None:
            return create_error_response("Missing required parameter: x_coordinate", with_readme=False)
        if y_coordinate is None:
            return create_error_response("Missing required parameter: y_coordinate", with_readme=False)
            
        success, message = click_at_coordinates_functional(hwnd, x_coordinate, y_coordinate, button)
        
        if success:
            return create_success_response(message)
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling click_at_coordinates: {e}", with_readme=False)

def handle_click_at_screen_coordinates(params: Dict) -> Dict:
    """Handle click_at_screen_coordinates operation"""
    try:
        x_coordinate = params.get('x_coordinate')
        y_coordinate = params.get('y_coordinate')
        button = params.get('button', 'left')
        
        if x_coordinate is None:
            return create_error_response("Missing required parameter: x_coordinate", with_readme=False)
        if y_coordinate is None:
            return create_error_response("Missing required parameter: y_coordinate", with_readme=False)
            
        success, message = click_at_screen_coordinates_functional(x_coordinate, y_coordinate, button)
        
        if success:
            return create_success_response(message)
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling click_at_screen_coordinates: {e}", with_readme=False)

def handle_take_screenshot(params: Dict) -> Dict:
    """Handle take_screenshot operation"""
    try:
        hwnd = params.get('hwnd')
        filename = params.get('filename')
        region = params.get('region')
        
        if not hwnd:
            return create_error_response("Missing required parameter: hwnd", with_readme=False)

        # D4: reject a malformed region instead of silently capturing the whole window. The
        # functional code only honors a region when len(region)==4, so a 3-element (or non-int)
        # region would otherwise be dropped with no error. Require exactly [x, y, width, height]
        # as four integers (bool excluded, matching this tool's other integer validation).
        if region is not None:
            if (not isinstance(region, (list, tuple)) or len(region) != 4 or
                    any(isinstance(region_coordinate_value, bool) or not isinstance(region_coordinate_value, int)
                        for region_coordinate_value in region)):
                return create_error_response(
                    "Parameter 'region' must be a list of exactly four integers [x, y, width, height].",
                    with_readme=False,
                )

        # B4: screen capture can exfiltrate on-screen content to a network peer, so record that
        # a capture happened (target window + output size) for auditability. Content is not logged.
        MCPLogger.log(TOOL_LOG_NAME, f"Screen capture requested for window {hwnd} (region={region})")
        
        success, message, base64_data = take_screenshot_functional(hwnd, filename, region)
        
        if success:
            captured_bytes = len(base64_data) if base64_data else 0
            MCPLogger.log(TOOL_LOG_NAME, f"Screen capture completed for window {hwnd} ({captured_bytes} base64 chars returned)")
            # # Use create_success_response with optional base64_image field
            # extra_fields = {}
            # if base64_data:
            #     extra_fields["base64_image"] = base64_data
            # return create_success_response(message, **extra_fields)
            
            if base64_data:
                # Return image using proper MCP image content type (like chrome_browser does)
                return {
                    "content": [{"type": "image", "mimeType": "image/png", "data": base64_data}],
                    "isError": False
                }
            else:
                # File was saved, return text message
                return create_success_response(message)
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling take_screenshot: {e}", with_readme=False)

def handle_send_text(params: Dict) -> Dict:
    """Handle send_text operation"""
    try:
        hwnd = params.get('hwnd')
        text = params.get('text')
        
        if not hwnd:
            return create_error_response("Missing required parameter: hwnd", with_readme=False)
        if not text:
            return create_error_response("Missing required parameter: text", with_readme=False)
            
        success, message = send_text_functional(hwnd, text)
        
        if success:
            return create_success_response(message)
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling send_text: {e}", with_readme=False)

def handle_click_ui_element(params: Dict) -> Dict:
    """Handle click_ui_element operation"""
    try:
        hwnd = params.get('hwnd')
        element_name = params.get('element_name')
        
        if not hwnd:
            return create_error_response("Missing required parameter: hwnd", with_readme=False)
        if not element_name:
            return create_error_response("Missing required parameter: element_name", with_readme=False)
            
        success, message = click_ui_element_functional(hwnd, element_name)
        
        if success:
            return create_success_response(message)
        else:
            return create_error_response(message, with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling click_ui_element: {e}", with_readme=False)

@_ttl_cached_about_section
def get_system_information_summary_and_full() -> Dict[str, any]:
    """Get comprehensive system information"""
    import platform
    import psutil
    import socket
    from datetime import datetime, timedelta
    
    try:
        # Boot time
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        
        return {
            "windows_version": platform.version(),
            "windows_release": platform.release(),
            "system_architecture": platform.architecture()[0],
            "computer_name": socket.gethostname(),
            "system_uptime_hours": round(uptime.total_seconds() / 3600, 2),
            "boot_time": boot_time.isoformat(),
            "platform_details": platform.platform()
        }
    except Exception as e:
        return {"error": f"Failed to get system information: {e}"}

@_ttl_cached_about_section
def get_hardware_information_summary_and_full() -> Dict[str, any]:
    """Get hardware information"""
    import psutil
    
    try:
        # CPU info
        cpu_info = {
            "cpu_model": platform.processor(),
            "cpu_cores_physical": psutil.cpu_count(logical=False),
            "cpu_cores_logical": psutil.cpu_count(logical=True),
            "cpu_frequency_current_mhz": psutil.cpu_freq().current if psutil.cpu_freq() else "Unknown"
        }
        
        # Memory info
        memory = psutil.virtual_memory()
        memory_info = {
            "total_memory_gb": round(memory.total / (1024**3), 2),
            "available_memory_gb": round(memory.available / (1024**3), 2),
            "memory_usage_percent": memory.percent
        }
        
        # Storage info
        storage_info = []
        for disk in psutil.disk_partitions():
            try:
                disk_usage = psutil.disk_usage(disk.mountpoint)
                storage_info.append({
                    "drive": disk.device,
                    "mountpoint": disk.mountpoint,
                    "filesystem": disk.fstype,
                    "total_gb": round(disk_usage.total / (1024**3), 2),
                    "free_gb": round(disk_usage.free / (1024**3), 2),
                    "used_percent": round((disk_usage.used / disk_usage.total) * 100, 2)
                })
            except PermissionError:
                continue
        
        return {
            **cpu_info,
            **memory_info,
            "storage_devices": storage_info
        }
    except Exception as e:
        return {"error": f"Failed to get hardware information: {e}"}

@_ttl_cached_about_section
def get_display_information_summary_and_full() -> Dict[str, any]:
    """Get comprehensive display/monitor information including layout, DPI, refresh rate, and physical properties.
    
    This function provides detailed information about all attached monitors that an AI can use to:
    - Understand the multi-monitor layout and arrangement
    - Calculate correct screen coordinates for automation
    - Account for DPI scaling when positioning windows
    - Understand which monitor is primary and their relative positions
    
    Returns:
        Dict containing:
        - displays: List of monitor details (resolution, position, DPI, refresh rate, etc.)
        - primary_display_index: Index of the primary monitor
        - total_display_count: Number of monitors
        - virtual_screen: Combined bounding box of all monitors
        - layout_description: Human-readable description of monitor arrangement
    """
    
    try:
        display_info = {
            "displays": [],
            "primary_display_index": -1,
            "total_display_count": 0,
            "virtual_screen": {
                "left": 0,
                "top": 0,
                "right": 0,
                "bottom": 0,
                "total_width": 0,
                "total_height": 0
            },
            "layout_description": ""
        }
        
        # Get virtual screen dimensions (bounding box of all monitors)
        try:
            virtual_left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
            virtual_top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
            virtual_width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
            virtual_height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
            display_info["virtual_screen"] = {
                "left": virtual_left,
                "top": virtual_top,
                "right": virtual_left + virtual_width,
                "bottom": virtual_top + virtual_height,
                "total_width": virtual_width,
                "total_height": virtual_height
            }
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error getting virtual screen metrics: {e}")
        
        # According to pywin32 docs, EnumDisplayMonitors returns a list of tuples:
        # (hMonitor, hdcMonitor, PyRECT) for each monitor found
        monitors = win32api.EnumDisplayMonitors(None, None)
        
        # Build a mapping of device names to display settings
        device_settings_map = {}
        try:
            device_index = 0
            while True:
                try:
                    device = win32api.EnumDisplayDevices(None, device_index, 0)
                    if not device.DeviceName:
                        break
                    
                    # Get display settings for this device
                    try:
                        settings = win32api.EnumDisplaySettings(device.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
                        device_settings_map[device.DeviceName] = {
                            "device_name": device.DeviceName,
                            "device_string": device.DeviceString,
                            "device_id": device.DeviceID,
                            "bits_per_pixel": settings.BitsPerPel,
                            "refresh_rate_hz": settings.DisplayFrequency,
                            "pixel_width": settings.PelsWidth,
                            "pixel_height": settings.PelsHeight,
                            "display_orientation_degrees": settings.DisplayOrientation * 90,  # 0=0°, 1=90°, 2=180°, 3=270°
                            "display_flags": settings.DisplayFlags,
                            "position_x": settings.Position_x,
                            "position_y": settings.Position_y
                        }
                    except Exception as e:
                        MCPLogger.log(TOOL_LOG_NAME, f"Error getting settings for {device.DeviceName}: {e}")
                    
                    device_index += 1
                except Exception:
                    break
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error enumerating display devices: {e}")
        
        for i, (hMonitor, hdcMonitor, rect) in enumerate(monitors):
            try:
                # Get detailed monitor info
                monitor_info = win32api.GetMonitorInfo(hMonitor)
                
                # Convert PyRECT objects to standard tuples
                monitor_rect = tuple(monitor_info['Monitor'])
                work_rect = tuple(monitor_info['Work'])
                device_name = monitor_info.get('Device', '')

                # Validate rect format
                if len(monitor_rect) != 4 or len(work_rect) != 4:
                    MCPLogger.log(TOOL_LOG_NAME, f"Invalid rect format for monitor {hMonitor}")
                    continue

                display_data = {
                    "monitor_index": i,
                    "monitor_handle": int(hMonitor),
                    "device_name": device_name,
                    "is_primary": (monitor_info['Flags'] & win32con.MONITORINFOF_PRIMARY) != 0,
                    "full_resolution": {
                        "left": monitor_rect[0],
                        "top": monitor_rect[1], 
                        "right": monitor_rect[2],
                        "bottom": monitor_rect[3],
                        "width": monitor_rect[2] - monitor_rect[0],
                        "height": monitor_rect[3] - monitor_rect[1]
                    },
                    "work_area": {
                        "left": work_rect[0],
                        "top": work_rect[1],
                        "right": work_rect[2], 
                        "bottom": work_rect[3],
                        "width": work_rect[2] - work_rect[0],
                        "height": work_rect[3] - work_rect[1]
                    }
                }
                
                # Add device-specific information if available
                if device_name in device_settings_map:
                    dev_info = device_settings_map[device_name]
                    display_data["device_description"] = dev_info.get("device_string", "")
                    display_data["device_id"] = dev_info.get("device_id", "")
                    display_data["color_depth_bits_per_pixel"] = dev_info.get("bits_per_pixel", 0)
                    display_data["refresh_rate_hz"] = dev_info.get("refresh_rate_hz", 0)
                    display_data["orientation_degrees"] = dev_info.get("display_orientation_degrees", 0)
                    
                    # Determine orientation name
                    orientation_deg = dev_info.get("display_orientation_degrees", 0)
                    if orientation_deg == 0:
                        display_data["orientation_name"] = "landscape"
                    elif orientation_deg == 90:
                        display_data["orientation_name"] = "portrait_rotated_right"
                    elif orientation_deg == 180:
                        display_data["orientation_name"] = "landscape_flipped"
                    elif orientation_deg == 270:
                        display_data["orientation_name"] = "portrait_rotated_left"
                    else:
                        display_data["orientation_name"] = "unknown"
                
                # Get DPI information for this monitor
                try:
                    # Use GetDpiForMonitor if available (Windows 8.1+)
                    shcore = ctypes.windll.shcore
                    dpi_x = ctypes.c_uint()
                    dpi_y = ctypes.c_uint()
                    # MDT_EFFECTIVE_DPI = 0 (effective DPI after system scaling)
                    # MDT_ANGULAR_DPI = 1 (DPI based on angular size)
                    # MDT_RAW_DPI = 2 (raw DPI from EDID)
                    # Pass the monitor handle as an int so the (now-declared) HMONITOR
                    # argument is not truncated to 32 bits.
                    result = shcore.GetDpiForMonitor(int(hMonitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
                    if result == 0:  # S_OK
                        display_data["dpi_x"] = dpi_x.value
                        display_data["dpi_y"] = dpi_y.value
                        display_data["scale_factor_percent"] = int((dpi_x.value / 96.0) * 100)
                        display_data["scale_factor_multiplier"] = round(dpi_x.value / 96.0, 2)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"GetDpiForMonitor not available: {e}")
                    # Fallback: get system DPI. Use try/finally so the device context is always
                    # released even if GetDeviceCaps (or the arithmetic) raises.
                    try:
                        hdc = win32gui.GetDC(0)
                        try:
                            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
                            display_data["dpi_x"] = dpi
                            display_data["dpi_y"] = dpi
                            display_data["scale_factor_percent"] = int((dpi / 96.0) * 100)
                            display_data["scale_factor_multiplier"] = round(dpi / 96.0, 2)
                        finally:
                            win32gui.ReleaseDC(0, hdc)
                    except Exception:
                        pass
                
                # Calculate taskbar area by comparing full vs work area
                taskbar_info = {"visible": False}
                if display_data["full_resolution"]["width"] != display_data["work_area"]["width"] or \
                   display_data["full_resolution"]["height"] != display_data["work_area"]["height"]:
                    
                    taskbar_info["visible"] = True
                    if display_data["full_resolution"]["height"] != display_data["work_area"]["height"]:
                        if work_rect[1] > monitor_rect[1]: # Top taskbar
                            taskbar_info.update({"position": "top", "size": work_rect[1] - monitor_rect[1]})
                        else: # Bottom taskbar
                            taskbar_info.update({"position": "bottom", "size": monitor_rect[3] - work_rect[3]})
                    elif display_data["full_resolution"]["width"] != display_data["work_area"]["width"]:
                        if work_rect[0] > monitor_rect[0]: # Left taskbar
                            taskbar_info.update({"position": "left", "size": work_rect[0] - monitor_rect[0]})
                        else: # Right taskbar
                            taskbar_info.update({"position": "right", "size": monitor_rect[2] - work_rect[2]})

                display_data["taskbar"] = taskbar_info
                
                if display_data["is_primary"]:
                    display_info["primary_display_index"] = len(display_info["displays"])
                
                display_info["displays"].append(display_data)

            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error processing monitor {hMonitor}: {e}")
                continue
        
        display_info["total_display_count"] = len(display_info["displays"])
        
        # Generate human-readable layout description
        if display_info["displays"]:
            layout_parts = []
            sorted_displays = sorted(display_info["displays"], key=lambda d: (d["full_resolution"]["left"], d["full_resolution"]["top"]))
            for d in sorted_displays:
                primary_marker = " (PRIMARY)" if d["is_primary"] else ""
                scale_info = f" @ {d.get('scale_factor_percent', 100)}%" if d.get('scale_factor_percent', 100) != 100 else ""
                refresh_info = f" {d.get('refresh_rate_hz', 60)}Hz" if d.get('refresh_rate_hz') else ""
                layout_parts.append(
                    f"Monitor {d['monitor_index']}{primary_marker}: "
                    f"{d['full_resolution']['width']}x{d['full_resolution']['height']}{scale_info}{refresh_info} "
                    f"at position ({d['full_resolution']['left']}, {d['full_resolution']['top']})"
                )
            display_info["layout_description"] = "; ".join(layout_parts)
        
        return display_info

    except Exception as e:
        return {"error": f"Failed to get display information: {e}"}

def _detect_current_process_is_elevated_admin() -> bool:
    """Return True when the current process is running with elevated (administrator on
    Windows, root on POSIX) privileges. Returns False on any error."""
    try:
        if IS_WINDOWS:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False

@_ttl_cached_about_section
def get_user_and_security_information_summary_and_full() -> Dict[str, any]:
    """Get user and security context information"""
    import os
    import getpass
    
    try:
        return {
            "current_username": getpass.getuser(),
            "user_domain": os.environ.get('USERDOMAIN', 'Unknown'),
            "user_profile_path": os.environ.get('USERPROFILE', 'Unknown'),
            # Fixed mislabels: the old "is_admin_process" actually tested SESSIONNAME=='Console'
            # (an interactive-console check, not elevation), and "computer_domain" returned
            # COMPUTERNAME (the machine name, not a domain).
            "is_elevated_admin_process": _detect_current_process_is_elevated_admin(),
            "is_interactive_console_session": os.environ.get('SESSIONNAME') == 'Console',
            "computer_name": os.environ.get('COMPUTERNAME', 'Unknown')
        }
    except Exception as e:
        return {"error": f"Failed to get user information: {e}"}

@_ttl_cached_about_section
def get_performance_information_summary_and_full() -> Dict[str, any]:
    """Get current performance and resource usage"""
    import psutil
    
    try:
        # CPU usage
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # Memory usage
        memory = psutil.virtual_memory()
        
        # Disk usage summary
        disk_io = psutil.disk_io_counters()
        
        # Battery info (if available)
        battery_info = {"available": False}
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_info = {
                    "available": True,
                    "percent": battery.percent,
                    "plugged_in": battery.power_plugged,
                    "time_left_seconds": battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None
                }
        except:
            pass
        
        return {
            "cpu_usage_percent": cpu_percent,
            "memory_usage_percent": memory.percent,
            "memory_available_gb": round(memory.available / (1024**3), 2),
            "disk_io_read_mb": round(disk_io.read_bytes / (1024**2), 2) if disk_io else 0,
            "disk_io_write_mb": round(disk_io.write_bytes / (1024**2), 2) if disk_io else 0,
            "battery": battery_info
        }
    except Exception as e:
        return {"error": f"Failed to get performance information: {e}"}

@_ttl_cached_about_section
def get_software_environment_summary_and_full() -> Dict[str, any]:
    """Get software environment information"""
    import os
    import subprocess
    
    try:
        software_info = {
            "powershell_execution_policy": "Unknown",
            "dotnet_versions": [],
            "python_version": "Unknown",
            "git_available": False,
            "docker_available": False,
            "wsl_available": False
        }
        
        # Check PowerShell execution policy
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Checking PowerShell execution policy")
            result = subprocess.run(['powershell', '-Command', 'Get-ExecutionPolicy'], 
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            if result.returncode == 0:
                software_info["powershell_execution_policy"] = result.stdout.strip()
        except:
            pass
        
        # Check for Python
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Checking Python version")
            result = subprocess.run(['python', '--version'], 
                                  capture_output=True, text=True, timeout=5,
                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            if result.returncode == 0:
                software_info["python_version"] = result.stdout.strip()
        except:
            pass
        
        # Check for Git
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Checking Git availability")
            result = subprocess.run(['git', '--version'], 
                                  capture_output=True, text=True, timeout=5,
                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            software_info["git_available"] = result.returncode == 0
        except:
            pass
        
        # Check for Docker
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Checking Docker availability")
            result = subprocess.run(['docker', '--version'], 
                                  capture_output=True, text=True, timeout=5,
                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            software_info["docker_available"] = result.returncode == 0
        except:
            pass
        
        # Check for WSL - use --help instead of --list which is invalid on older WSL versions
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Checking WSL availability")
            result = subprocess.run(['wsl', '--help'], 
                                  capture_output=True, text=True, timeout=5,
                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            software_info["wsl_available"] = result.returncode == 0
        except:
            pass
        
        return software_info
        
    except Exception as e:
        return {"error": f"Failed to get software environment information: {e}"}

@_ttl_cached_about_section
def get_network_information_summary_and_full() -> Dict[str, any]:
    """Get network interface and connectivity information"""
    try:
        import socket
        import subprocess
        
        network_info = {
            "interfaces": [],
            "default_gateway": None,
            "dns_servers": [],
            "connectivity_status": "unknown"
        }
        
        # Get network interfaces
        try:
            for interface_name, addresses in psutil.net_if_addrs().items():
                interface_data = {"name": interface_name, "addresses": []}
                for addr in addresses:
                    if addr.family == socket.AF_INET:  # IPv4
                        interface_data["addresses"].append({
                            "type": "IPv4",
                            "address": addr.address,
                            "netmask": addr.netmask
                        })
                    elif addr.family == socket.AF_INET6:  # IPv6
                        interface_data["addresses"].append({
                            "type": "IPv6", 
                            "address": addr.address
                        })
                if interface_data["addresses"]:
                    network_info["interfaces"].append(interface_data)
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error getting network interfaces: {e}")
            
        # Test connectivity
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            network_info["connectivity_status"] = "connected"
        except:
            network_info["connectivity_status"] = "no_internet"
            
        return network_info
    except Exception as e:
        return {"error": f"Failed to get network information: {e}"}

@_ttl_cached_about_section
def get_installed_applications_summary_and_full() -> Dict[str, any]:
    """Get comprehensive installed applications information with robust error handling for all platforms"""
    import subprocess
    
    # Platform-specific implementation
    if IS_LINUX:
        return get_installed_applications_linux()
    elif IS_MACOS:
        return get_installed_applications_macos()
    elif IS_WINDOWS:
        return get_installed_applications_windows()
    else:
        return {"error": f"Unsupported platform: {CURRENT_PLATFORM}"}

def get_installed_applications_linux() -> Dict[str, any]:
    """Linux-specific implementation for installed applications using dpkg, rpm, flatpak, snap"""
    import subprocess
    
    try:
        applications_info = {
            "dpkg_packages": [],
            "rpm_packages": [],
            "flatpak_apps": [],
            "snap_packages": [],
            "total_dpkg_count": 0,
            "total_rpm_count": 0,
            "total_flatpak_count": 0,
            "total_snap_count": 0,
            "scan_errors": []
        }
        
        # Try dpkg (Debian/Ubuntu/Raspberry Pi OS)
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Scanning dpkg packages...")
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Installed-Size}\t${Status}\n"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if not line:
                      continue
                    parts = line.split('\t')
                    if len(parts) >= 4 and 'installed' in parts[3]:
                        size_kb = 0
                        try:
                          size_kb = int(parts[2]) if parts[2].isdigit() else 0
                        except:
                          pass
                        
                        applications_info["dpkg_packages"].append({
                            "name": parts[0],
                            "version": parts[1],
                            "size_kb": size_kb,
                            "source": "dpkg"
                        })
                
                applications_info["total_dpkg_count"] = len(applications_info["dpkg_packages"])
                MCPLogger.log(TOOL_LOG_NAME, f"Found {applications_info['total_dpkg_count']} dpkg packages")
        except FileNotFoundError:
            MCPLogger.log(TOOL_LOG_NAME, "dpkg not available (not a Debian-based system)")
        except Exception as e:
            applications_info["scan_errors"].append(f"dpkg scan error: {e}")
        
        # Try rpm (RHEL/Fedora/CentOS)
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Scanning rpm packages...")
            result = subprocess.run(
                ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{SIZE}\n"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if not line:
                      continue
                    parts = line.split('\t')
                    if len(parts) >= 3:
                        size_bytes = 0
                        try:
                          size_bytes = int(parts[2]) if parts[2].isdigit() else 0
                        except:
                          pass
                        
                        applications_info["rpm_packages"].append({
                            "name": parts[0],
                            "version": parts[1],
                            "size_kb": size_bytes // 1024,
                            "source": "rpm"
                        })
                
                applications_info["total_rpm_count"] = len(applications_info["rpm_packages"])
                MCPLogger.log(TOOL_LOG_NAME, f"Found {applications_info['total_rpm_count']} rpm packages")
        except FileNotFoundError:
            MCPLogger.log(TOOL_LOG_NAME, "rpm not available (not an RPM-based system)")
        except Exception as e:
            applications_info["scan_errors"].append(f"rpm scan error: {e}")
        
        # Try flatpak
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Scanning flatpak apps...")
            result = subprocess.run(
                ["flatpak", "list", "--app", "--columns=name,version,application,size"],
                capture_output=True,
                text=True,
                timeout=15
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:  # Skip header
                    if not line:
                      continue
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        applications_info["flatpak_apps"].append({
                            "name": parts[0],
                            "version": parts[1] if len(parts) > 1 else "Unknown",
                            "source": "flatpak"
                        })
                
                applications_info["total_flatpak_count"] = len(applications_info["flatpak_apps"])
                MCPLogger.log(TOOL_LOG_NAME, f"Found {applications_info['total_flatpak_count']} flatpak apps")
        except FileNotFoundError:
            MCPLogger.log(TOOL_LOG_NAME, "flatpak not available")
        except Exception as e:
            applications_info["scan_errors"].append(f"flatpak scan error: {e}")
        
        # Try snap
        try:
            MCPLogger.log(TOOL_LOG_NAME, "Scanning snap packages...")
            result = subprocess.run(
                ["snap", "list"],
                capture_output=True,
                text=True,
                timeout=15
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:  # Skip header
                    if not line:
                      continue
                    parts = line.split()
                    if len(parts) >= 2:
                        applications_info["snap_packages"].append({
                            "name": parts[0],
                            "version": parts[1],
                            "source": "snap"
                        })
                
                applications_info["total_snap_count"] = len(applications_info["snap_packages"])
                MCPLogger.log(TOOL_LOG_NAME, f"Found {applications_info['total_snap_count']} snap packages")
        except FileNotFoundError:
            MCPLogger.log(TOOL_LOG_NAME, "snap not available")
        except Exception as e:
            applications_info["scan_errors"].append(f"snap scan error: {e}")
        
        # Summary statistics
        total_apps = (applications_info["total_dpkg_count"] + 
                     applications_info["total_rpm_count"] + 
                     applications_info["total_flatpak_count"] + 
                     applications_info["total_snap_count"])
        
        applications_info["summary_stats"] = {
            "total_applications": total_apps,
            "dpkg_packages": applications_info["total_dpkg_count"],
            "rpm_packages": applications_info["total_rpm_count"],
            "flatpak_apps": applications_info["total_flatpak_count"],
            "snap_packages": applications_info["total_snap_count"],
            "scan_successful": len(applications_info["scan_errors"]) == 0
        }
        
        return applications_info
        
    except Exception as e:
        return {"error": f"Failed to get installed applications (Linux): {e}"}

def get_installed_applications_macos() -> Dict[str, any]:
    """macOS-specific implementation for installed applications"""
    # TODO: Implement macOS application scanning
    return {
        "error": "macOS application scanning not yet implemented",
        "summary_stats": {
            "total_applications": 0,
            "scan_successful": False
        }
    }

def get_installed_applications_windows() -> Dict[str, any]:
    """Windows-specific implementation for installed applications"""
    import winreg
    import subprocess
    
    try:
        applications_info = {
            "registry_applications": [],
            "windows_store_apps": [],
            "total_registry_count": 0,
            "total_store_count": 0,
            "scan_errors": []
        }
        
        # Registry-based applications (works on all Windows versions)
        registry_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            # 32-bit apps on 64-bit Windows
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
        ]
        
        for root_key, path in registry_paths:
            try:
                with winreg.OpenKey(root_key, path) as registry_key:
                    subkey_count = winreg.QueryInfoKey(registry_key)[0]
                    
                    for i in range(subkey_count):
                        try:
                            subkey_name = winreg.EnumKey(registry_key, i)
                            with winreg.OpenKey(registry_key, subkey_name) as app_key:
                                
                                def safe_registry_read(key, value_name, default=""):
                                    try:
                                        return winreg.QueryValueEx(key, value_name)[0]
                                    except (FileNotFoundError, OSError):
                                        return default
                                
                                display_name = safe_registry_read(app_key, "DisplayName")
                                if not display_name:  # Skip entries without display names
                                    continue
                                    
                                app_data = {
                                    "name": display_name,
                                    "version": safe_registry_read(app_key, "DisplayVersion"),
                                    "publisher": safe_registry_read(app_key, "Publisher"),
                                    "install_date": safe_registry_read(app_key, "InstallDate"),
                                    "install_location": safe_registry_read(app_key, "InstallLocation"),
                                    "registry_source": "HKLM" if root_key == winreg.HKEY_LOCAL_MACHINE else "HKCU",
                                    "architecture": "32-bit" if "WOW6432Node" in path else "64-bit"
                                }
                                
                                # Estimate size if available
                                size_kb = safe_registry_read(app_key, "EstimatedSize")
                                if size_kb and str(size_kb).isdigit():
                                    app_data["estimated_size_mb"] = round(int(size_kb) / 1024, 2)
                                
                                applications_info["registry_applications"].append(app_data)
                                
                        except Exception as e:
                            applications_info["scan_errors"].append(f"Registry app scan error: {e}")
                            continue
                            
            except Exception as e:
                applications_info["scan_errors"].append(f"Registry access error for {path}: {e}")
                continue
        
        applications_info["total_registry_count"] = len(applications_info["registry_applications"])
        
        # Windows Store Apps (only available on Windows versions with Store)
        try:
            # Test if PowerShell and Get-AppxPackage are available
            MCPLogger.log(TOOL_LOG_NAME, "Testing if Get-AppxPackage is available")
            test_command = ["powershell", "-Command", "Get-Command Get-AppxPackage -ErrorAction SilentlyContinue"]
            test_result = subprocess.run(test_command, capture_output=True, text=True, timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
            
            if test_result.returncode == 0:  # Get-AppxPackage is available
                MCPLogger.log(TOOL_LOG_NAME, "Retrieving Windows Store apps via Get-AppxPackage")
                store_command = [
                    "powershell", "-Command",
                    "Get-AppxPackage | Select-Object Name, Version, Publisher, InstallLocation | ConvertTo-Json"
                ]
                
                store_result = subprocess.run(store_command, capture_output=True, text=True, timeout=30,
                                            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
                
                if store_result.returncode == 0 and store_result.stdout.strip():
                    import json
                    try:
                        store_apps_raw = json.loads(store_result.stdout)
                        
                        # Handle both single app (dict) and multiple apps (list) responses
                        if isinstance(store_apps_raw, dict):
                            store_apps_raw = [store_apps_raw]
                        
                        for app in store_apps_raw:
                            if app.get("Name"):  # Skip entries without names
                                app_data = {
                                    "name": app.get("Name", "Unknown"),
                                    "version": app.get("Version", "Unknown"),
                                    "publisher": app.get("Publisher", "Unknown"),
                                    "install_location": app.get("InstallLocation", ""),
                                    "source": "Windows Store"
                                }
                                applications_info["windows_store_apps"].append(app_data)
                        
                        applications_info["total_store_count"] = len(applications_info["windows_store_apps"])
                        
                    except json.JSONDecodeError as e:
                        applications_info["scan_errors"].append(f"Store apps JSON parse error: {e}")
                else:
                    applications_info["scan_errors"].append("PowerShell Get-AppxPackage command failed or returned no data")
            else:
                applications_info["scan_errors"].append("Get-AppxPackage not available (likely Windows 10N/Enterprise N or older Windows)")
                
        except subprocess.TimeoutExpired:
            applications_info["scan_errors"].append("Store apps scan timed out")
        except Exception as e:
            applications_info["scan_errors"].append(f"Store apps scan error: {e}")
        
        # Summary statistics for summary mode
        registry_top_publishers = {}
        for app in applications_info["registry_applications"]:
            publisher = app.get("publisher", "Unknown")
            registry_top_publishers[publisher] = registry_top_publishers.get(publisher, 0) + 1
        
        applications_info["summary_stats"] = {
            "total_applications": applications_info["total_registry_count"] + applications_info["total_store_count"],
            "registry_applications": applications_info["total_registry_count"],
            "store_applications": applications_info["total_store_count"],
            "top_publishers": sorted(registry_top_publishers.items(), key=lambda x: x[1], reverse=True)[:5],
            "scan_successful": len(applications_info["scan_errors"]) == 0
        }
        
        return applications_info
        
    except Exception as e:
        return {"error": f"Failed to get installed applications: {e}"}

@_ttl_cached_about_section
def get_running_processes_summary_and_full() -> Dict[str, any]:
    """Get comprehensive running processes information for system diagnostics and troubleshooting"""
    try:
        processes_info = {
            "running_processes": [],
            "high_cpu_processes": [],
            "high_memory_processes": [],
            "system_processes": [],
            "user_processes": [],
            "total_processes": 0,
            "scan_errors": []
        }
        
        try:
            all_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline', 'username', 'cpu_percent', 'memory_info', 'create_time', 'status']):
                try:
                    proc_info = proc.info
                    
                    # Get additional process details with error handling
                    try:
                        memory_mb = round(proc_info['memory_info'].rss / (1024 * 1024), 2) if proc_info['memory_info'] else 0
                    except (AttributeError, TypeError):
                        memory_mb = 0
                    
                    try:
                        cpu_percent = proc_info['cpu_percent'] if proc_info['cpu_percent'] is not None else 0
                    except (AttributeError, TypeError):
                        cpu_percent = 0
                    
                    try:
                        username = proc_info['username'] if proc_info['username'] else "Unknown"
                    except (AttributeError, TypeError, psutil.AccessDenied):
                        username = "System/Access Denied"
                    
                    try:
                        exe_path = proc_info['exe'] if proc_info['exe'] else "Unknown"
                    except (AttributeError, TypeError, psutil.AccessDenied):
                        exe_path = "Access Denied"
                    
                    try:
                        status = proc_info['status'] if proc_info['status'] else "unknown"
                    except (AttributeError, TypeError):
                        status = "unknown"
                    
                    process_data = {
                        "pid": proc_info['pid'],
                        "name": proc_info['name'] if proc_info['name'] else "Unknown",
                        "exe_path": exe_path,
                        "username": username,
                        "cpu_percent": cpu_percent,
                        "memory_mb": memory_mb,
                        "status": status,
                        "is_system_process": username in ["NT AUTHORITY\\SYSTEM", "System/Access Denied", "NT AUTHORITY\\LOCAL SERVICE", "NT AUTHORITY\\NETWORK SERVICE"]
                    }
                    
                    # Add command line if available (truncated for readability)
                    try:
                        cmdline = proc_info['cmdline']
                        if cmdline and isinstance(cmdline, list):
                            process_data["command_line"] = " ".join(cmdline)[:200] + ("..." if len(" ".join(cmdline)) > 200 else "")
                        else:
                            process_data["command_line"] = ""
                    except (AttributeError, TypeError, psutil.AccessDenied):
                        process_data["command_line"] = "Access Denied"
                    
                    all_processes.append(process_data)
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    # Process disappeared or access denied - this is normal
                    continue
                except Exception as e:
                    processes_info["scan_errors"].append(f"Error reading process: {e}")
                    continue
            
            processes_info["running_processes"] = all_processes
            processes_info["total_processes"] = len(all_processes)
            
            # Categorize processes for better analysis
            for proc in all_processes:
                if proc["is_system_process"]:
                    processes_info["system_processes"].append(proc)
                else:
                    processes_info["user_processes"].append(proc)
                
                # High resource usage processes (for troubleshooting)
                if proc["cpu_percent"] > 10:  # More than 10% CPU
                    processes_info["high_cpu_processes"].append(proc)
                
                if proc["memory_mb"] > 100:  # More than 100MB RAM
                    processes_info["high_memory_processes"].append(proc)
            
            # Sort high resource processes by usage (most intensive first)
            processes_info["high_cpu_processes"].sort(key=lambda x: x["cpu_percent"], reverse=True)
            processes_info["high_memory_processes"].sort(key=lambda x: x["memory_mb"], reverse=True)
            
            # Summary statistics
            total_memory_mb = sum(proc["memory_mb"] for proc in all_processes)
            avg_cpu = sum(proc["cpu_percent"] for proc in all_processes) / len(all_processes) if all_processes else 0
            
            processes_info["summary_stats"] = {
                "total_processes": len(all_processes),
                "system_processes_count": len(processes_info["system_processes"]),
                "user_processes_count": len(processes_info["user_processes"]),
                "high_cpu_count": len(processes_info["high_cpu_processes"]),
                "high_memory_count": len(processes_info["high_memory_processes"]),
                "total_memory_usage_mb": round(total_memory_mb, 2),
                "average_cpu_percent": round(avg_cpu, 2),
                "scan_successful": len(processes_info["scan_errors"]) == 0
            }
            
        except Exception as e:
            processes_info["scan_errors"].append(f"Process enumeration error: {e}")
            
        return processes_info
        
    except Exception as e:
        return {"error": f"Failed to get running processes: {e}"}

def handle_about(params: Dict) -> Dict:
    """Handle about operation with drill-down navigation to avoid context overflow.
    
    Three levels of detail:
    - overview (default): Returns ~2KB navigable tree with counts
    - section: Returns summary + top N items for one section (paginated)
    - full: Returns everything (WARNING: 500KB+, can overflow context)
    """
    try:
        detail = params.get("detail", "overview")
        section = params.get("section", None)
        limit = params.get("limit", 10)
        offset = params.get("offset", 0)
        filter_expr = params.get("filter", None)
        sort_by = params.get("sort_by", None)
        pid = params.get("pid", None)
        app_name = params.get("app_name", None)
        
        if detail not in ["overview", "section", "full"]:
            return create_error_response("Parameter 'detail' must be 'overview', 'section', or 'full'", with_readme=False)
        
        available_sections = [
            "system_information",
            "hardware_information", 
            "display_information",
            "user_and_security_information",
            "performance_information",
            "software_environment",
            "network_information",
            "installed_applications",
            "running_processes",
            "browser_information"
        ]
        
        if section and section not in available_sections:
            return create_error_response(f"Invalid section '{section}'. Available sections: {', '.join(available_sections)}", with_readme=False)
        
        # Validate detail/section combination
        if detail == "section" and not section:
            return create_error_response("Parameter 'section' is required when detail='section'", with_readme=False)
        
        # ========== OVERVIEW MODE (default) - tiny response ==========
        if detail == "overview":
            return handle_about_overview_mode()
        
        # ========== SECTION MODE - drill into one section with pagination ==========
        if detail == "section":
            return handle_about_section_mode(section, limit, offset, filter_expr, sort_by, pid, app_name)
        
        # ========== FULL MODE (legacy) - WARNING: huge response ==========
        if detail == "full":
            return handle_about_full_mode_with_warning(section)
        
    except Exception as e:
        return create_error_response(f"Error handling about operation: {e}", with_readme=False)


def handle_about_overview_mode() -> Dict:
    """Return a small overview of the system with counts and navigation hints."""
    try:
        # Get quick summary data (small sections only)
        sys_info = get_system_information_summary_and_full()
        hw_info = get_hardware_information_summary_and_full()
        perf_info = get_performance_information_summary_and_full()
        
        # Get counts for large sections without loading full data
        proc_info = get_running_processes_summary_and_full()
        apps_info = get_installed_applications_summary_and_full()
        browser_info = get_browser_information_summary_and_full()
        network_info = get_network_information_summary_and_full()
        
        overview = {
            "system_overview": {
                "computer_name": sys_info.get("computer_name", "Unknown"),
                "windows_version": sys_info.get("windows_version", "Unknown"),
                "uptime_hours": sys_info.get("system_uptime_hours", 0),
                "cpu_model": hw_info.get("cpu_model", "Unknown")[:50],
                "cpu_cores": hw_info.get("cpu_cores_logical", 0),
                "memory_total_gb": hw_info.get("total_memory_gb", 0),
                "memory_used_percent": perf_info.get("memory_usage_percent", 0),
                "cpu_usage_percent": perf_info.get("cpu_usage_percent", 0)
            },
            "available_sections": {
                "system_information": {"size": "small", "description": "OS version, uptime, platform details"},
                "hardware_information": {"size": "small", "description": "CPU, memory, storage devices"},
                "display_information": {"size": "small", "description": "Monitors, resolution, DPI"},
                "user_and_security_information": {"size": "small", "description": "Username, domain, admin status"},
                "performance_information": {"size": "small", "description": "Current CPU, memory, disk usage"},
                "software_environment": {"size": "small", "description": "Python, Git, Docker, WSL status"},
                "network_information": {
                    "size": "medium",
                    "interfaces_count": len(network_info.get("interfaces", [])),
                    "connectivity": network_info.get("connectivity_status", "unknown")
                },
                "installed_applications": {
                    "size": "large",
                    "total_apps": apps_info.get("summary_stats", {}).get("total_applications", 0),
                    "registry_apps": apps_info.get("total_registry_count", 0),
                    "store_apps": apps_info.get("total_store_count", 0),
                    "top_publishers": [p[0] for p in apps_info.get("summary_stats", {}).get("top_publishers", [])[:3]]
                },
                "running_processes": {
                    "size": "large",
                    "total_processes": proc_info.get("summary_stats", {}).get("total_processes", 0),
                    "high_memory_count": proc_info.get("summary_stats", {}).get("high_memory_count", 0),
                    "high_cpu_count": proc_info.get("summary_stats", {}).get("high_cpu_count", 0),
                    "total_memory_mb": proc_info.get("summary_stats", {}).get("total_memory_usage_mb", 0),
                    "top_memory_processes": [p["name"] for p in proc_info.get("high_memory_processes", [])[:3]]
                },
                "browser_information": {
                    "size": "small",
                    "browsers_found": browser_info.get("usage_stats", {}).get("total_browsers_found", 0),
                    "default_browser": browser_info.get("default_browser", "Unknown")
                }
            },
            "usage_hint": "Use detail='section' with section='<name>' to drill into a section. Use limit, offset, filter, sort_by for large sections."
        }
        
        return create_success_response(
            "System Overview",
            detail_level="overview",
            overview=overview
        )
        
    except Exception as e:
        return create_error_response(f"Error getting system overview: {e}", with_readme=False)


def handle_about_section_mode(section: str, limit: int, offset: int, filter_expr: Optional[str], sort_by: Optional[str], pid: Optional[int], app_name: Optional[str]) -> Dict:
    """Return detailed data for one section with filtering and pagination."""
    try:
        # Small sections - return full data (they're already small)
        small_sections = ["system_information", "hardware_information", "display_information", 
                         "user_and_security_information", "performance_information", "software_environment", "browser_information"]
        
        if section in small_sections:
            return handle_about_small_section(section)
        
        # Network - medium size, return full
        if section == "network_information":
            return handle_about_network_section()
        
        # Large sections - apply filtering and pagination
        if section == "running_processes":
            return handle_about_processes_section(limit, offset, filter_expr, sort_by, pid)
        
        if section == "installed_applications":
            return handle_about_applications_section(limit, offset, filter_expr, sort_by, app_name)
        
        return create_error_response(f"Unknown section: {section}", with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error getting section '{section}': {e}", with_readme=False)


def handle_about_small_section(section: str) -> Dict:
    """Return full data for a small section."""
    section_data = None
    
    if section == "system_information":
        section_data = get_system_information_summary_and_full()
    elif section == "hardware_information":
        section_data = get_hardware_information_summary_and_full()
    elif section == "display_information":
        section_data = get_display_information_summary_and_full()
    elif section == "user_and_security_information":
        section_data = get_user_and_security_information_summary_and_full()
    elif section == "performance_information":
        section_data = get_performance_information_summary_and_full()
    elif section == "software_environment":
        section_data = get_software_environment_summary_and_full()
    elif section == "browser_information":
        section_data = get_browser_information_summary_and_full()
    
    return create_success_response(
        f"Section: {section}",
        detail_level="section",
        section=section,
        data=section_data
    )


def handle_about_network_section() -> Dict:
    """Return network information."""
    network_data = get_network_information_summary_and_full()
    return create_success_response(
        "Section: network_information",
        detail_level="section",
        section="network_information",
        data=network_data
    )


def handle_about_processes_section(limit: int, offset: int, filter_expr: Optional[str], sort_by: Optional[str], pid: Optional[int]) -> Dict:
    """Return running processes with filtering, sorting, and pagination."""
    full_data = get_running_processes_summary_and_full()
    
    # If looking for specific PID
    if pid is not None:
        for proc in full_data.get("running_processes", []):
            if proc.get("pid") == pid:
                return create_success_response(
                    f"Process details for PID {pid}",
                    detail_level="section",
                    section="running_processes",
                    pid_requested=pid,
                    process=proc
                )
        return create_error_response(f"Process with PID {pid} not found", with_readme=False)
    
    # Get the list to filter
    processes = full_data.get("running_processes", [])
    
    # Apply filter
    if filter_expr:
        processes = apply_process_filter(processes, filter_expr, full_data)
    
    # Apply sort
    if sort_by:
        processes = apply_process_sort(processes, sort_by)
    
    # Calculate pagination
    total_count = len(processes)
    paginated_processes = processes[offset:offset + limit]
    
    return create_success_response(
        f"Running Processes ({offset + 1}-{offset + len(paginated_processes)} of {total_count})",
        detail_level="section",
        section="running_processes",
        summary_stats=full_data.get("summary_stats", {}),
        filter_applied=filter_expr,
        sort_by=sort_by,
        pagination={
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "showing": f"{offset + 1}-{offset + len(paginated_processes)} of {total_count}",
            "has_more": offset + limit < total_count
        },
        processes=paginated_processes,
        usage_hint="Use offset to paginate. Use filter='high_memory', 'high_cpu', or 'name:pattern' to narrow results."
    )


def apply_process_filter(processes: List[Dict], filter_expr: str, full_data: Dict) -> List[Dict]:
    """Apply filter expression to process list."""
    filter_lower = filter_expr.lower()
    
    # Special filters
    if filter_lower == "high_memory":
        return full_data.get("high_memory_processes", [])
    elif filter_lower == "high_cpu":
        return full_data.get("high_cpu_processes", [])
    elif filter_lower == "system":
        return full_data.get("system_processes", [])
    elif filter_lower == "user":
        return full_data.get("user_processes", [])
    
    # Field:value filters
    if ":" in filter_expr:
        field, pattern = filter_expr.split(":", 1)
        field = field.lower().strip()
        pattern = pattern.lower().strip()
        
        if field == "name":
            return [p for p in processes if pattern in p.get("name", "").lower()]
        elif field == "exe":
            return [p for p in processes if pattern in p.get("exe_path", "").lower()]
        elif field == "user" or field == "username":
            return [p for p in processes if pattern in p.get("username", "").lower()]
    
    # Default: search in name
    return [p for p in processes if filter_expr.lower() in p.get("name", "").lower()]


def apply_process_sort(processes: List[Dict], sort_by: str) -> List[Dict]:
    """Sort process list by field."""
    sort_key_map = {
        "name": lambda p: p.get("name", "").lower(),
        "memory": lambda p: p.get("memory_mb", 0),
        "cpu": lambda p: p.get("cpu_percent", 0),
        "pid": lambda p: p.get("pid", 0)
    }
    
    if sort_by in sort_key_map:
        reverse = sort_by in ["memory", "cpu"]  # Descending for resource usage
        return sorted(processes, key=sort_key_map[sort_by], reverse=reverse)
    
    return processes


def handle_about_applications_section(limit: int, offset: int, filter_expr: Optional[str], sort_by: Optional[str], app_name: Optional[str]) -> Dict:
    """Return installed applications with filtering, sorting, and pagination."""
    full_data = get_installed_applications_summary_and_full()
    
    # Combine registry and store apps
    all_apps = full_data.get("registry_applications", []) + full_data.get("windows_store_apps", [])
    
    # If looking for specific app by name
    if app_name:
        matching_apps = [app for app in all_apps if app_name.lower() in app.get("name", "").lower()]
        if matching_apps:
            return create_success_response(
                f"Application details for '{app_name}'",
                detail_level="section",
                section="installed_applications",
                app_name_requested=app_name,
                matching_count=len(matching_apps),
                applications=matching_apps
            )
        return create_error_response(f"No application matching '{app_name}' found", with_readme=False)
    
    # Apply filter
    if filter_expr:
        all_apps = apply_application_filter(all_apps, filter_expr)
    
    # Apply sort
    if sort_by:
        all_apps = apply_application_sort(all_apps, sort_by)
    
    # Calculate pagination
    total_count = len(all_apps)
    paginated_apps = all_apps[offset:offset + limit]
    
    return create_success_response(
        f"Installed Applications ({offset + 1}-{offset + len(paginated_apps)} of {total_count})",
        detail_level="section",
        section="installed_applications",
        summary_stats=full_data.get("summary_stats", {}),
        filter_applied=filter_expr,
        sort_by=sort_by,
        pagination={
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "showing": f"{offset + 1}-{offset + len(paginated_apps)} of {total_count}",
            "has_more": offset + limit < total_count
        },
        applications=paginated_apps,
        usage_hint="Use offset to paginate. Use filter='publisher:Microsoft' or 'name:pattern' to narrow results."
    )


def apply_application_filter(apps: List[Dict], filter_expr: str) -> List[Dict]:
    """Apply filter expression to application list."""
    # Field:value filters
    if ":" in filter_expr:
        field, pattern = filter_expr.split(":", 1)
        field = field.lower().strip()
        pattern = pattern.lower().strip()
        
        if field == "name":
            return [a for a in apps if pattern in a.get("name", "").lower()]
        elif field == "publisher":
            return [a for a in apps if pattern in a.get("publisher", "").lower()]
        elif field == "version":
            return [a for a in apps if pattern in a.get("version", "").lower()]
        elif field == "source":
            return [a for a in apps if pattern in a.get("source", a.get("registry_source", "")).lower()]
    
    # Default: search in name
    return [a for a in apps if filter_expr.lower() in a.get("name", "").lower()]


def apply_application_sort(apps: List[Dict], sort_by: str) -> List[Dict]:
    """Sort application list by field."""
    sort_key_map = {
        "name": lambda a: a.get("name", "").lower(),
        "size": lambda a: a.get("estimated_size_mb", 0) or 0,
        "install_date": lambda a: a.get("install_date", "") or "",
        "publisher": lambda a: a.get("publisher", "").lower()
    }
    
    if sort_by in sort_key_map:
        reverse = sort_by == "size"  # Descending for size
        return sorted(apps, key=sort_key_map[sort_by], reverse=reverse)
    
    return apps


def handle_about_full_mode_with_warning(section: Optional[str]) -> Dict:
    """Return full data for all sections (legacy mode). Includes warning about size."""
    available_sections = [
        "system_information",
        "hardware_information", 
        "display_information",
        "user_and_security_information",
        "performance_information",
        "software_environment",
        "network_information",
        "installed_applications",
        "running_processes",
        "browser_information"
    ]
    
    system_info = {}
    sections_to_include = [section] if section else available_sections
    
    for section_name in sections_to_include:
        if section_name == "system_information":
            system_info["system_information"] = get_system_information_summary_and_full()
        elif section_name == "hardware_information":
            system_info["hardware_information"] = get_hardware_information_summary_and_full()
        elif section_name == "display_information":
            system_info["display_information"] = get_display_information_summary_and_full()
        elif section_name == "user_and_security_information":
            system_info["user_and_security_information"] = get_user_and_security_information_summary_and_full()
        elif section_name == "performance_information":
            system_info["performance_information"] = get_performance_information_summary_and_full()
        elif section_name == "software_environment":
            system_info["software_environment"] = get_software_environment_summary_and_full()
        elif section_name == "network_information":
            system_info["network_information"] = get_network_information_summary_and_full()
        elif section_name == "installed_applications":
            system_info["installed_applications"] = get_installed_applications_summary_and_full()
        elif section_name == "running_processes":
            system_info["running_processes"] = get_running_processes_summary_and_full()
        elif section_name == "browser_information":
            system_info["browser_information"] = get_browser_information_summary_and_full()
    
    return create_success_response(
        "WARNING: Full system info - response may be 500KB+. Use detail='overview' for navigation.",
        detail_level="full",
        requested_section=section,
        system_info=system_info
    )


@_ttl_cached_about_section
def get_browser_information_summary_and_full() -> Dict[str, any]:
    """Get comprehensive browser information including installed browsers and default browser settings"""
    
    # Platform-specific implementation
    if IS_LINUX:
        return get_browser_information_linux()
    elif IS_MACOS:
        return get_browser_information_macos()
    elif IS_WINDOWS:
        return get_browser_information_windows()
    else:
        return {"error": f"Unsupported platform: {CURRENT_PLATFORM}"}

def get_browser_information_linux() -> Dict[str, any]:
    """Linux-specific implementation for browser detection"""
    import subprocess
    
    try:
        browser_info = {
            "installed_browsers": [],
            "default_browser": "Unknown",
            "browser_paths": {},
            "scan_errors": []
        }
        
        # Common Linux browser paths
        browser_patterns = {
            "Google Chrome": [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/opt/google/chrome/google-chrome"
            ],
            "Chromium": [
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium"
            ],
            "Mozilla Firefox": [
                "/usr/bin/firefox",
                "/usr/lib/firefox/firefox",
                "/snap/bin/firefox"
            ],
            "Firefox ESR": [
                "/usr/bin/firefox-esr"
            ],
            "Opera": [
                "/usr/bin/opera",
                "/usr/lib/x86_64-linux-gnu/opera/opera"
            ],
            "Brave": [
                "/usr/bin/brave-browser",
                "/usr/bin/brave",
                "/opt/brave.com/brave/brave-browser"
            ],
            "Microsoft Edge": [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable"
            ],
            "Vivaldi": [
                "/usr/bin/vivaldi",
                "/usr/bin/vivaldi-stable"
            ]
        }
        
        # Detect installed browsers
        for browser_name, paths in browser_patterns.items():
            browser_found = False
            browser_path = None
            
            for path in paths:
                if os.path.exists(path):
                    browser_found = True
                    browser_path = path
                    break
            
            if browser_found:
                browser_data = {
                    "name": browser_name,
                    "path": browser_path,
                    "version": "Unknown"
                }
                
                # Try to get version
                try:
                    version_result = subprocess.run(
                        [browser_path, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if version_result.returncode == 0:
                        browser_data["version"] = version_result.stdout.strip()
                except Exception as e:
                    browser_info["scan_errors"].append(f"Version detection error for {browser_name}: {e}")
                
                browser_info["installed_browsers"].append(browser_data)
                browser_info["browser_paths"][browser_name] = browser_path
        
        # Get default browser using xdg-settings (if available)
        try:
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                browser_info["default_browser"] = result.stdout.strip()
        except FileNotFoundError:
            browser_info["scan_errors"].append("xdg-settings not available")
        except Exception as e:
            browser_info["scan_errors"].append(f"Default browser detection error: {e}")
        
        # Usage statistics
        browser_info["usage_stats"] = {
            "total_browsers_found": len(browser_info["installed_browsers"]),
            "browsers_with_versions": len([b for b in browser_info["installed_browsers"] if b["version"] != "Unknown"]),
            "scan_successful": len(browser_info["scan_errors"]) == 0
        }
        
        return browser_info
        
    except Exception as e:
        return {"error": f"Failed to get browser information (Linux): {e}"}

def get_browser_information_macos() -> Dict[str, any]:
    """macOS-specific implementation for browser detection"""
    # TODO: Implement macOS browser detection
    return {
        "error": "macOS browser detection not yet implemented",
        "usage_stats": {
            "total_browsers_found": 0,
            "scan_successful": False
        }
    }

def get_browser_information_windows() -> Dict[str, any]:
    """Windows-specific implementation for browser detection"""
    import subprocess
    import winreg
    
    try:
        browser_info = {
            "installed_browsers": [],
            "default_browser": "Unknown",
            "browser_paths": {},
            "scan_errors": []
        }
        
        # Common browser detection patterns
        browser_patterns = {
            "Google Chrome": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ],
                "common_paths": [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
                ]
            },
            "Mozilla Firefox": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
                ],
                "common_paths": [
                    r"C:\Program Files\Mozilla Firefox\firefox.exe",
                    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"
                ]
            },
            "Microsoft Edge": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
                ],
                "common_paths": [
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Windows\SystemApps\Microsoft.MicrosoftEdge_8wekyb3d8bbwe\MicrosoftEdge.exe"
                ]
            },
            "Internet Explorer": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\iexplore.exe"
                ],
                "common_paths": [
                    r"C:\Program Files\Internet Explorer\iexplore.exe",
                    r"C:\Program Files (x86)\Internet Explorer\iexplore.exe"
                ]
            },
            "Opera": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe"
                ],
                "common_paths": [
                    r"C:\Users\%USERNAME%\AppData\Local\Programs\Opera\opera.exe"
                ]
            },
            "Brave": {
                "registry_keys": [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
                ],
                "common_paths": [
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
                ]
            }
        }
        
        # Detect installed browsers
        for browser_name, patterns in browser_patterns.items():
            browser_found = False
            browser_path = None
            
            # Check registry first
            for reg_key in patterns["registry_keys"]:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_key) as key:
                        path_value, _ = winreg.QueryValueEx(key, "")
                        if path_value and os.path.exists(path_value):
                            browser_found = True
                            browser_path = path_value
                            break
                except (FileNotFoundError, OSError):
                    continue
            
            # Check common installation paths if not found in registry
            if not browser_found:
                for common_path in patterns["common_paths"]:
                    expanded_path = os.path.expandvars(common_path)
                    if os.path.exists(expanded_path):
                        browser_found = True
                        browser_path = expanded_path
                        break
            
            if browser_found:
                browser_data = {
                    "name": browser_name,
                    "path": browser_path,
                    "version": "Unknown"
                }
                
                # Try to get version information
                try:
                    if browser_path and os.path.exists(browser_path):
                        # Use PowerShell to get file version
                        MCPLogger.log(TOOL_LOG_NAME, f"Getting version for browser: {browser_name}")
                        version_cmd = [
                            "powershell", "-Command",
                            f"(Get-ItemProperty '{browser_path}').VersionInfo.FileVersion"
                        ]
                        version_result = subprocess.run(version_cmd, capture_output=True, text=True, timeout=10,
                                                      creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
                        
                        if version_result.returncode == 0 and version_result.stdout.strip():
                            browser_data["version"] = version_result.stdout.strip()
                except Exception as e:
                    browser_info["scan_errors"].append(f"Version detection error for {browser_name}: {e}")
                
                browser_info["installed_browsers"].append(browser_data)
                browser_info["browser_paths"][browser_name] = browser_path
        
        # Get default browser information
        try:
            # Method 1: Check user choice registry
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice") as key:
                    progid, _ = winreg.QueryValueEx(key, "ProgId")
                    browser_info["default_browser"] = progid
            except (FileNotFoundError, OSError):
                # Method 2: Use PowerShell to get default browser
                try:
                    MCPLogger.log(TOOL_LOG_NAME, "Getting default browser via PowerShell")
                    default_cmd = [
                        "powershell", "-Command",
                        "Get-ItemProperty 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\.html\\UserChoice' | Select-Object -ExpandProperty ProgId"
                    ]
                    default_result = subprocess.run(default_cmd, capture_output=True, text=True, timeout=10,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
                    
                    if default_result.returncode == 0 and default_result.stdout.strip():
                        browser_info["default_browser"] = default_result.stdout.strip()
                except Exception as e:
                    browser_info["scan_errors"].append(f"Default browser detection error: {e}")
                    
        except Exception as e:
            browser_info["scan_errors"].append(f"Default browser registry error: {e}")
        
        # Get browser usage statistics (if available)
        try:
            browser_info["usage_stats"] = {
                "total_browsers_found": len(browser_info["installed_browsers"]),
                "browsers_with_versions": len([b for b in browser_info["installed_browsers"] if b["version"] != "Unknown"]),
                "scan_successful": len(browser_info["scan_errors"]) == 0
            }
        except Exception as e:
            browser_info["scan_errors"].append(f"Statistics calculation error: {e}")
        
        return browser_info
        
    except Exception as e:
        return {"error": f"Failed to get browser information: {e}"}

def handle_execute_command(params: Dict, requesting_user: Optional[str] = None) -> Dict:
    """Handle execute_command operation"""
    try:
        command = params.get('command')
        timeout_ms = params.get('timeout_ms', 30000)
        shell = params.get('shell')
        
        if not command:
            return create_error_response("Missing required parameter: command", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing execute_command: command={_redact_sensitive_for_log(command)}, timeout_ms={timeout_ms}")
        
        result = execute_command_functional(command, timeout_ms, shell, owner_user=requesting_user)
        
        if result['success']:
            response_text = f"Command executed successfully\n"
            response_text += f"Session ID: {result['session_id']}\n"
            response_text += f"Status: {'Running in background' if result['is_running'] else 'Completed'}\n"
            response_text += f"Initial output:\n{result['initial_output']}"
            
            if result['is_running']:
                response_text += f"\n\nCommand is still running. Use read_output with session_id {result['session_id']} to get more output."
            
            return {
                "content": [{"type": "text", "text": response_text}],
                "isError": False,
                "session_id": result['session_id'],
                "is_running": result['is_running']
            }
        else:
            return create_error_response(result['error'], with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling execute_command: {e}", with_readme=False)

def handle_read_output(params: Dict, requesting_user: Optional[str] = None) -> Dict:
    """Handle read_output operation"""
    try:
        session_id = params.get('session_id')
        timeout_ms = params.get('timeout_ms', 5000)
        
        if session_id is None:
            return create_error_response("Missing required parameter: session_id", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing read_output: session_id={session_id}, timeout_ms={timeout_ms}")
        
        result = read_output_functional(session_id, timeout_ms, requesting_user=requesting_user)
        
        if result['success']:
            response_text = f"Output from session {session_id}:\n"
            if result['has_output']:
                response_text += result['output']
                if result['timeout_reached']:
                    response_text += "\n(timeout reached)"
            else:
                response_text += "No new output available"
                if result['timeout_reached']:
                    response_text += " (timeout reached)"
            
            return {
                "content": [{"type": "text", "text": response_text}],
                "isError": False,
                "session_id": session_id,
                "has_output": result['has_output'],
                "timeout_reached": result['timeout_reached']
            }
        else:
            return create_error_response(result['error'], with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling read_output: {e}", with_readme=False)

def handle_force_terminate(params: Dict, requesting_user: Optional[str] = None) -> Dict:
    """Handle force_terminate operation"""
    try:
        session_id = params.get('session_id')
        
        if session_id is None:
            return create_error_response("Missing required parameter: session_id", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing force_terminate: session_id={session_id}")
        
        result = force_terminate_functional(session_id, requesting_user=requesting_user)
        
        if result['success']:
            return {
                "content": [{"type": "text", "text": result['message']}],
                "isError": False,
                "session_id": session_id
            }
        else:
            return create_error_response(result['error'], with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling force_terminate: {e}", with_readme=False)

def handle_list_sessions(params: Dict, requesting_user: Optional[str] = None) -> Dict:
    """Handle list_sessions operation"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Processing list_sessions")
        
        result = list_sessions_functional(requesting_user=requesting_user)
        
        if result['success']:
            sessions = result['active_sessions']
            
            if len(sessions) == 0:
                response_text = "No active command sessions"
            else:
                response_text = f"Active command sessions ({len(sessions)}):\n\n"
                for session in sessions:
                    status = "Completed" if session['is_completed'] else "Running"
                    response_text += f"Session {session['session_id']}: {status}, Runtime: {session['runtime_seconds']}s"
                    if session['has_new_output']:
                        response_text += " (has new output)"
                    response_text += f", Total output: {session['total_output_length']} chars\n"
            
            return {
                "content": [{"type": "text", "text": response_text}],
                "isError": False,
                "total_sessions": result['total_sessions'],
                "sessions": sessions
            }
        else:
            return create_error_response(result['error'], with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error handling list_sessions: {e}", with_readme=False)



################################################################################################################################
################################################################################################################################
################################                      WINDOWS SPECIFIC ROUTINES                 ################################
################################################################################################################################
################################################################################################################################


################################################################################################################################
################################################################################################################################
################################                    APPLE MAC SPECIFIC ROUTINES                 ################################
################################################################################################################################
################################################################################################################################

# macOS-specific implementations
# These use:
# - AppleScript via osascript for window management and application control
# - screencapture command-line tool for screenshots
#
# Note: Some operations require Accessibility permissions to be granted to Terminal/iTerm/whatever runs this code.
# Basic operations like listing apps and screenshots work without special permissions.

if IS_MACOS:
    # ------------------------------------------------------------------
    # macOS shared helpers (window lookup, Accessibility API, CGEvent)
    # ------------------------------------------------------------------

    def macos_accessibility_permission_is_granted() -> bool:
        """Return True when THIS process has been granted macOS Accessibility permission.

        Window listing and screenshots work without it, but moving windows, injecting
        clicks/keys, and walking the UI-element (AX) tree of OTHER apps require the user to
        tick this process under System Settings > Privacy & Security > Accessibility.
        AXIsProcessTrusted() reports the current grant state without prompting."""
        if not MACOS_HAS_ACCESSIBILITY_API:
            return False
        try:
            return bool(macos_accessibility_services_module.AXIsProcessTrusted())
        except Exception:
            return False

    _MACOS_ACCESSIBILITY_PERMISSION_HINT = (
        "macOS Accessibility permission is required for this operation. Grant it under "
        "System Settings > Privacy & Security > Accessibility for the application that runs "
        "this server (e.g. aura / Terminal), then retry."
    )

    def _macos_parse_window_identifier(hwnd_str: str) -> Dict[str, object]:
        """Interpret the tool's cross-platform 'hwnd' string for macOS targets.

        Accepts, in priority order:
          - a CoreGraphics window number as decimal ("12345") or hex ("0x3039")
            -> {"kind": "window_id", "window_id": int}
          - the legacy pseudo-handle "macos_app_<index>_<AppName>" produced by older builds
            -> {"kind": "app_name", "app_name": str}
          - "pid:<n>" -> {"kind": "pid", "pid": int}
          - anything else is treated as a literal application name
            -> {"kind": "app_name", "app_name": str}
        """
        raw = (hwnd_str or "").strip()
        if raw.startswith("macos_app_"):
            parts = raw.split("_", 3)
            if len(parts) >= 4:
                return {"kind": "app_name", "app_name": parts[3]}
        if raw.lower().startswith("pid:"):
            try:
                return {"kind": "pid", "pid": int(raw.split(":", 1)[1])}
            except ValueError:
                pass
        try:
            if raw.lower().startswith("0x"):
                return {"kind": "window_id", "window_id": int(raw, 16)}
            return {"kind": "window_id", "window_id": int(raw, 10)}
        except ValueError:
            pass
        return {"kind": "app_name", "app_name": raw}

    def _macos_copy_onscreen_window_records() -> List[Dict[str, object]]:
        """Return normalized dicts for the current on-screen windows via CGWindowList.

        Each record: {window_id, title, app_name, pid, layer, x, y, width, height, is_onscreen}.
        Title is often '' unless the user has granted Screen Recording permission - callers
        must not depend on it being populated."""
        if not MACOS_HAS_QUARTZ:
            return []
        Q = macos_quartz_module
        try:
            options = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
            raw_windows = Q.CGWindowListCopyWindowInfo(options, Q.kCGNullWindowID) or []
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"CGWindowListCopyWindowInfo failed: {e}")
            return []
        records = []
        for entry in raw_windows:
            try:
                bounds = entry.get('kCGWindowBounds') or {}
                records.append({
                    "window_id": int(entry.get('kCGWindowNumber', 0)),
                    "title": entry.get('kCGWindowName') or "",
                    "app_name": entry.get('kCGWindowOwnerName') or "",
                    "pid": int(entry.get('kCGWindowOwnerPID', 0)),
                    "layer": int(entry.get('kCGWindowLayer', 0)),
                    "x": int(bounds.get('X', 0)),
                    "y": int(bounds.get('Y', 0)),
                    "width": int(bounds.get('Width', 0)),
                    "height": int(bounds.get('Height', 0)),
                    "is_onscreen": bool(entry.get('kCGWindowIsOnscreen', False)),
                })
            except Exception:
                continue
        return records

    def _macos_find_window_record(window_id: int) -> Optional[Dict[str, object]]:
        """Return the on-screen window record for a CoreGraphics window number, or None."""
        for record in _macos_copy_onscreen_window_records():
            if record["window_id"] == window_id:
                return record
        return None

    def _macos_resolve_target_pid_and_appname(hwnd_str: str) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, object]]]:
        """Map a tool 'hwnd' to (pid, app_name, window_record) for macOS operations.

        For a window_id we look the window up in CGWindowList to recover its owner pid and the
        window's current bounds/title (window_record). For an app_name/pid we return what we
        can (window_record is None)."""
        identifier = _macos_parse_window_identifier(hwnd_str)
        if identifier["kind"] == "window_id":
            record = _macos_find_window_record(int(identifier["window_id"]))
            if record is not None:
                return int(record["pid"]), (record["app_name"] or None), record
            return None, None, None
        if identifier["kind"] == "pid":
            pid = int(identifier["pid"])
            app_name = None
            if MACOS_HAS_QUARTZ:
                try:
                    running = macos_appkit_module.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    if running is not None:
                        app_name = running.localizedName()
                except Exception:
                    pass
            return pid, app_name, None
        # app_name: try to resolve a pid from the running applications list
        app_name = str(identifier.get("app_name") or "")
        if MACOS_HAS_QUARTZ and app_name:
            try:
                for running in macos_appkit_module.NSWorkspace.sharedWorkspace().runningApplications():
                    localized = running.localizedName() or ""
                    if localized.lower() == app_name.lower():
                        return int(running.processIdentifier()), localized, None
            except Exception:
                pass
        return None, (app_name or None), None

    def _macos_activate_pid(pid: int) -> bool:
        """Bring the application owning pid to the foreground (no Automation permission needed)."""
        if not MACOS_HAS_QUARTZ or not pid:
            return False
        try:
            running = macos_appkit_module.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if running is None:
                return False
            # NSApplicationActivateIgnoringOtherApps == 1 << 1
            running.activateWithOptions_(1 << 1)
            return True
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"NSRunningApplication activate failed for pid {pid}: {e}")
            return False

    def _macos_button_to_cg_events(button: str):
        """Map a 'left'/'right'/'middle' button name to (down_event, up_event, cg_button)."""
        Q = macos_quartz_module
        table = {
            "left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft),
            "right": (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight),
            "middle": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGMouseButtonCenter),
        }
        return table.get(button)

    def _macos_post_mouse_click_at_global_point(global_x: int, global_y: int, button: str = "left", double: bool = False) -> Tuple[bool, str]:
        """Synthesize a mouse click at absolute screen coordinates using CGEvent (HID tap)."""
        if not MACOS_HAS_QUARTZ:
            return False, "Quartz is unavailable; cannot synthesize mouse input on this macOS build."
        mapped = _macos_button_to_cg_events(button)
        if mapped is None:
            return False, f"Invalid button: {button}. Must be 'left', 'right', or 'middle'."
        down_event_type, up_event_type, cg_button = mapped
        Q = macos_quartz_module
        try:
            point = Q.CGPoint(float(global_x), float(global_y))
            # Remember the current cursor location so we can restore it afterwards.
            previous_point = None
            try:
                previous_point = Q.CGEventGetLocation(Q.CGEventCreate(None))
            except Exception:
                previous_point = None
            move_event = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, point, cg_button)
            Q.CGEventPost(Q.kCGHIDEventTap, move_event)
            click_count = 2 if double else 1
            for current_click_index in range(click_count):
                down_event = Q.CGEventCreateMouseEvent(None, down_event_type, point, cg_button)
                up_event = Q.CGEventCreateMouseEvent(None, up_event_type, point, cg_button)
                # click state (1 for single, 2 for the second of a double) so apps see a real double-click
                Q.CGEventSetIntegerValueField(down_event, Q.kCGMouseEventClickState, current_click_index + 1)
                Q.CGEventSetIntegerValueField(up_event, Q.kCGMouseEventClickState, current_click_index + 1)
                Q.CGEventPost(Q.kCGHIDEventTap, down_event)
                time.sleep(0.02)
                Q.CGEventPost(Q.kCGHIDEventTap, up_event)
                time.sleep(0.02)
            if previous_point is not None:
                try:
                    restore_event = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, previous_point, cg_button)
                    Q.CGEventPost(Q.kCGHIDEventTap, restore_event)
                except Exception:
                    pass
            return True, f"Clicked {button} button at screen ({global_x}, {global_y})"
        except Exception as e:
            return False, f"Error posting mouse click: {e}"

    def _macos_ax_copy_attribute(ax_element, attribute_name):
        """Read one AX attribute, returning the value or None (never raising)."""
        if not MACOS_HAS_ACCESSIBILITY_API or ax_element is None:
            return None
        try:
            error_code, value = macos_accessibility_services_module.AXUIElementCopyAttributeValue(ax_element, attribute_name, None)
            if error_code == macos_accessibility_services_module.kAXErrorSuccess:
                return value
        except Exception:
            pass
        return None

    def _macos_ax_attribute_as_text(ax_element, attribute_name) -> str:
        """Read an AX attribute and coerce it to a short display string ('' if absent)."""
        value = _macos_ax_copy_attribute(ax_element, attribute_name)
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:
            return ""

    def _macos_ax_element_bounds(ax_element) -> Tuple[int, int, int, int]:
        """Return (x, y, width, height) in global screen pixels for an AX element (zeros if unknown)."""
        AX = macos_accessibility_services_module
        x = y = width = height = 0
        position_value = _macos_ax_copy_attribute(ax_element, AX.kAXPositionAttribute)
        size_value = _macos_ax_copy_attribute(ax_element, AX.kAXSizeAttribute)
        if position_value is not None:
            try:
                ok, point = AX.AXValueGetValue(position_value, AX.kAXValueCGPointType, None)
                if ok:
                    x, y = int(point.x), int(point.y)
            except Exception:
                pass
        if size_value is not None:
            try:
                ok, size = AX.AXValueGetValue(size_value, AX.kAXValueCGSizeType, None)
                if ok:
                    width, height = int(size.width), int(size.height)
            except Exception:
                pass
        return x, y, width, height

    def _macos_ax_windows_for_pid(pid: int) -> List[object]:
        """Return the AX window elements owned by an application pid (empty on error/no permission)."""
        if not MACOS_HAS_ACCESSIBILITY_API or not pid:
            return []
        AX = macos_accessibility_services_module
        try:
            app_element = AX.AXUIElementCreateApplication(pid)
        except Exception:
            return []
        windows = _macos_ax_copy_attribute(app_element, AX.kAXWindowsAttribute)
        if not windows:
            return []
        try:
            return list(windows)
        except Exception:
            return []

    def _macos_ax_best_matching_window_element(pid: int, target_record: Optional[Dict[str, object]]):
        """Pick the AX window element that best matches a CGWindow record (by title, then bounds).

        AX does not expose the CGWindow number, so we correlate on the title when available and
        otherwise on the closest position/size. Falls back to the app's main window, then its
        first window."""
        ax_windows = _macos_ax_windows_for_pid(pid)
        if not ax_windows:
            return None
        if target_record is not None:
            target_title = str(target_record.get("title") or "")
            if target_title:
                for window_element in ax_windows:
                    if _macos_ax_attribute_as_text(window_element, macos_accessibility_services_module.kAXTitleAttribute) == target_title:
                        return window_element
            target_x = int(target_record.get("x", 0))
            target_y = int(target_record.get("y", 0))
            target_w = int(target_record.get("width", 0))
            target_h = int(target_record.get("height", 0))
            best_window_element = None
            best_distance = None
            for window_element in ax_windows:
                wx, wy, ww, wh = _macos_ax_element_bounds(window_element)
                distance = abs(wx - target_x) + abs(wy - target_y) + abs(ww - target_w) + abs(wh - target_h)
                if best_distance is None or distance < best_distance:
                    best_distance = distance
                    best_window_element = window_element
            if best_window_element is not None:
                return best_window_element
        AX = macos_accessibility_services_module
        app_element = AX.AXUIElementCreateApplication(pid)
        main_window = _macos_ax_copy_attribute(app_element, AX.kAXMainWindowAttribute)
        if main_window is not None:
            return main_window
        return ax_windows[0]

    def list_windows_functional(include_all: bool = False) -> List[Dict]:
      """macOS implementation: enumerate real on-screen windows via CoreGraphics (CGWindowList).

      Returns one entry per on-screen window (not just per app), including the CoreGraphics
      window number as 'hwnd' (decimal string), the owning application, pid, and the window's
      screen position and size - the same shape the Windows implementation returns so the shared
      handlers, activate_window, move_window, take_screenshot and click ops can target it.

      Note: window titles are only populated when the user has granted Screen Recording
      permission; without it 'title' is '' but all other fields still work. Minimized/hidden
      windows are not reported by CGWindowList (there is no reliable public API for their bounds).

      Args:
        include_all: If True, include helper/menu/overlay windows (non-zero window layers) and
          zero-size windows; if False (default) only normal application windows (layer 0 with a
          real size) are returned.

      Returns:
        List of window dictionaries with comprehensive properties
      """
      if not MACOS_HAS_QUARTZ:
        MCPLogger.log(TOOL_LOG_NAME, "Quartz unavailable - falling back to AppleScript application listing")
        return _macos_legacy_list_windows_via_applescript(include_all=include_all)
      try:
        records = _macos_copy_onscreen_window_records()
        windows = []
        for record in records:
          is_normal_app_window = (record["layer"] == 0 and record["width"] >= 1 and record["height"] >= 1)
          if not include_all and not is_normal_app_window:
            continue
          windows.append({
            'hwnd': str(record["window_id"]),
            'title': record["title"],
            'class': record["app_name"],
            'x': record["x"],
            'y': record["y"],
            'width': record["width"],
            'height': record["height"],
            'style_flags': {},
            'process_id': record["pid"],
            'process_name': record["app_name"],
            'process_exe': record["app_name"],
            'is_visible': record["is_onscreen"],
            'is_minimized': False,
            'is_maximized': False,
            'window_layer': record["layer"],
            'app_name': record["app_name"],
          })
        windows.sort(key=lambda w: (w['class'].lower(), w['title'].lower()))
        MCPLogger.log(TOOL_LOG_NAME, f"macOS window enumeration: {len(windows)} windows (include_all={include_all})")
        return windows
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in macOS list_windows_functional: {e} - falling back to AppleScript")
        return _macos_legacy_list_windows_via_applescript(include_all=include_all)

    def _macos_legacy_list_windows_via_applescript(include_all: bool = False) -> List[Dict]:
      """macOS fallback: list running applications via AppleScript (used only when Quartz is
      unavailable).

      Note: On macOS, this returns application-level information rather than individual windows
      unless Accessibility permissions are granted. Each entry represents a running application.
      
      Args:
        include_all: If True, includes background processes (not yet implemented)
        
      Returns:
        List of window/application dictionaries with properties
      """
      try:
        # Get list of visible (non-background) processes using AppleScript
        MCPLogger.log(TOOL_LOG_NAME, "Getting list of macOS applications via AppleScript")
        result = subprocess.run(
          ['osascript', '-e', 
           'tell application "System Events" to get name of every process whose background only is false'],
          capture_output=True,
          text=True,
          timeout=5
        )
        
        if result.returncode != 0:
          MCPLogger.log(TOOL_LOG_NAME, f"Error listing applications: {result.stderr}")
          return []
          
        # Parse the comma-separated list
        app_names = result.stdout.strip().split(', ')
        
        windows = []
        for idx, app_name in enumerate(app_names):
          if not app_name:
            continue
            
          # Create a window object compatible with the expected format
          # On macOS without accessibility permissions, we use the app name as a pseudo-hwnd
          window_obj = {
            'hwnd': f"macos_app_{idx}_{app_name}",  # Pseudo-handle for macOS
            'title': app_name,
            'class': 'macOS Application',
            'x': 0,  # Position not available without accessibility permissions
            'y': 0,
            'width': 0,  # Dimensions not available without accessibility permissions
            'height': 0,
            'style_flags': {},
            'process_id': 0,  # PID not easily available via AppleScript
            'process_name': app_name,
            'process_exe': app_name,
            'is_visible': True,
            'is_minimized': False,  # State not available without accessibility permissions
            'is_maximized': False
          }
          
          windows.append(window_obj)
        
        # Sort by app name for consistent output
        windows.sort(key=lambda w: w['title'].lower())
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(windows)} macOS applications")
        return windows
        
      except subprocess.TimeoutExpired:
        MCPLogger.log(TOOL_LOG_NAME, "Timeout while listing applications")
        return []
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in list_windows_functional: {e}")
        return []
    
    def activate_window_functional(hwnd_str: str, request_focus: bool = False) -> Tuple[bool, str]:
      """macOS implementation: bring the target window's application to the foreground.

      macOS activates at the application level, not per-window, so this focuses the app that
      owns the given window. Accepts any handle form list_windows returns (a CoreGraphics
      window-number string), plus 'pid:<n>' or a literal application name.

      Args:
        hwnd_str: Window-number handle from list_windows, 'pid:<n>', or an application name
        request_focus: Accepted for signature parity with Windows (macOS always focuses the app)

      Returns:
        Tuple of (success, message)
      """
      try:
        pid, app_name, _record = _macos_resolve_target_pid_and_appname(hwnd_str)

        # Preferred path: activate by pid via NSRunningApplication (no Automation permission).
        if pid:
          MCPLogger.log(TOOL_LOG_NAME, f"Activating macOS pid {pid} ({app_name or 'unknown app'})")
          if _macos_activate_pid(pid):
            return True, f"Successfully activated application '{app_name or pid}'"

        # Fallback path: activate by name via AppleScript (may need Automation permission).
        if not app_name:
          return False, f"Could not resolve a macOS application to activate from handle '{hwnd_str}'"
        MCPLogger.log(TOOL_LOG_NAME, f"Activating macOS application by name via AppleScript: {app_name}")
        result = subprocess.run(
          ['osascript', '-e', f'tell application "{app_name}" to activate'],
          capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
          return True, f"Successfully activated application '{app_name}'"
        error_msg = result.stderr.strip()
        MCPLogger.log(TOOL_LOG_NAME, f"Error activating application {app_name}: {error_msg}")
        return False, f"Failed to activate application '{app_name}': {error_msg}"

      except subprocess.TimeoutExpired:
        return False, f"Timeout while trying to activate application"
      except Exception as e:
        return False, f"Error activating application: {e}"

    def move_window_functional(hwnd_str: str, x: int, y: int, width: int, height: int) -> Tuple[bool, str]:
      """macOS implementation: move and resize a window via the Accessibility (AX) API.

      Requires macOS Accessibility permission. Because AX does not expose CoreGraphics window
      numbers, the target AX window is correlated with the requested window by title (when
      available) and otherwise by closest current position/size.

      Args:
        hwnd_str: Window-number handle from list_windows (or 'pid:<n>' / app name)
        x, y: New top-left position in global screen pixels
        width, height: New size in pixels

      Returns:
        Tuple of (success, message)
      """
      if not MACOS_HAS_ACCESSIBILITY_API:
        return False, "ApplicationServices (AX API) is unavailable; cannot move windows on this macOS build."
      if not macos_accessibility_permission_is_granted():
        return False, _MACOS_ACCESSIBILITY_PERMISSION_HINT
      if width <= 0 or height <= 0:
        return False, f"Invalid dimensions: width={width}, height={height}. Both must be positive."
      try:
        pid, app_name, record = _macos_resolve_target_pid_and_appname(hwnd_str)
        if not pid:
          return False, f"Could not resolve a macOS application/window from handle '{hwnd_str}'"
        window_element = _macos_ax_best_matching_window_element(pid, record)
        if window_element is None:
          return False, f"No AX window found for application '{app_name or pid}' (handle '{hwnd_str}')"
        AX = macos_accessibility_services_module
        Q = macos_quartz_module
        position_value = AX.AXValueCreate(AX.kAXValueCGPointType, Q.CGPoint(float(x), float(y)))
        size_value = AX.AXValueCreate(AX.kAXValueCGSizeType, Q.CGSize(float(width), float(height)))
        position_error = AX.AXUIElementSetAttributeValue(window_element, AX.kAXPositionAttribute, position_value)
        size_error = AX.AXUIElementSetAttributeValue(window_element, AX.kAXSizeAttribute, size_value)
        if position_error != AX.kAXErrorSuccess or size_error != AX.kAXErrorSuccess:
          return False, f"AX set position/size failed (position err={position_error}, size err={size_error}); the window may not be resizable."
        MCPLogger.log(TOOL_LOG_NAME, f"Moved macOS window (pid {pid}) to ({x},{y}) size {width}x{height}")
        return True, f"Window for '{app_name or pid}' moved to ({x}, {y}) and resized to {width}x{height}"
      except Exception as e:
        return False, f"Error moving macOS window: {e}"

    def click_at_coordinates_functional(hwnd_str: str, x: int, y: int, button: str = "left") -> Tuple[bool, str]:
      """macOS implementation: click at window-relative coordinates via CGEvent.

      The window's current on-screen origin (from CGWindowList) is added to the given offset, so
      (x, y) are relative to the target window's top-left. Negative x/y count back from the
      window's right/bottom edge, matching the Windows implementation. Posting synthetic clicks
      requires macOS Accessibility permission.

      Args:
        hwnd_str: Window-number handle from list_windows
        x, y: Coordinates relative to the window (negative = from right/bottom edge)
        button: 'left', 'right', or 'middle'

      Returns:
        Tuple of (success, message)
      """
      if not MACOS_HAS_QUARTZ:
        return False, "Quartz is unavailable; cannot synthesize mouse input on this macOS build."
      if not macos_accessibility_permission_is_granted():
        return False, _MACOS_ACCESSIBILITY_PERMISSION_HINT
      identifier = _macos_parse_window_identifier(hwnd_str)
      if identifier["kind"] != "window_id":
        return False, ("click_at_coordinates needs a window handle from list_windows (a window "
                       "number). Use click_at_screen_coordinates for absolute screen clicks.")
      record = _macos_find_window_record(int(identifier["window_id"]))
      if record is None:
        return False, f"Window {identifier['window_id']} is not currently on screen."
      # Bring the owning app forward so the click lands on the intended window.
      if record["pid"]:
        _macos_activate_pid(int(record["pid"]))
        time.sleep(0.15)
      window_x, window_y = int(record["x"]), int(record["y"])
      window_width, window_height = int(record["width"]), int(record["height"])
      global_x = window_x + (window_width + x if x < 0 else x)
      global_y = window_y + (window_height + y if y < 0 else y)
      success, message = _macos_post_mouse_click_at_global_point(global_x, global_y, button)
      if success:
        MCPLogger.log(TOOL_LOG_NAME, f"Clicked {button} at window-relative ({x},{y}) = screen ({global_x},{global_y})")
        return True, f"Successfully clicked {button} button at window coordinates ({x}, {y}) [screen ({global_x}, {global_y})]"
      return False, message

    def click_at_screen_coordinates_functional(x: int, y: int, button: str = "left") -> Tuple[bool, str]:
      """macOS implementation: click at absolute screen coordinates via CGEvent.

      Posting synthetic clicks requires macOS Accessibility permission.

      Args:
        x, y: Absolute screen coordinates
        button: 'left', 'right', or 'middle'

      Returns:
        Tuple of (success, message)
      """
      if not MACOS_HAS_QUARTZ:
        return False, "Quartz is unavailable; cannot synthesize mouse input on this macOS build."
      if not macos_accessibility_permission_is_granted():
        return False, _MACOS_ACCESSIBILITY_PERMISSION_HINT
      success, message = _macos_post_mouse_click_at_global_point(int(x), int(y), button)
      if success:
        MCPLogger.log(TOOL_LOG_NAME, f"Clicked {button} at screen ({x},{y})")
        return True, f"Successfully clicked {button} button at screen coordinates ({x}, {y})"
      return False, message
    
    def take_screenshot_functional(hwnd_str: str, filename: Optional[str] = None, region: Optional[List[int]] = None) -> Tuple[bool, str, Optional[str]]:
      """macOS implementation using the screencapture command.

      When hwnd_str resolves to a CoreGraphics window number (as returned by list_windows), a
      window-specific capture is taken with `screencapture -l<id>`; otherwise the whole main
      display is captured. An optional region [x, y, width, height] (relative to the captured
      image, negative offsets counting from the right/bottom, zero meaning 'to the edge') is
      cropped from the result with PIL when available.

      Note: capturing another application's window content requires the user to have granted
      Screen Recording permission to the process running this server; without it macOS returns a
      blank/desktop image rather than an error.

      Args:
        hwnd_str: Window-number handle from list_windows (falls back to full screen otherwise)
        filename: Optional path to save the PNG to (returned inline as base64 when omitted)
        region: Optional [x, y, width, height] sub-rectangle to crop from the capture

      Returns:
        Tuple of (success, message, base64_image_data)
      """
      temp_file = None
      try:
        identifier = _macos_parse_window_identifier(hwnd_str) if hwnd_str else {"kind": "none"}
        capture_window_id = None
        if identifier.get("kind") == "window_id":
          record = _macos_find_window_record(int(identifier["window_id"]))
          if record is None:
            return False, f"Window {identifier['window_id']} is not currently on screen.", None
          capture_window_id = int(identifier["window_id"])
          # Bring the owning app forward so the captured window is unobscured.
          if record.get("pid"):
            _macos_activate_pid(int(record["pid"]))
            time.sleep(0.3)

        # Decide the output path (temp file when returning base64 inline).
        output_path = filename
        if not output_path:
          temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
          output_path = temp_file.name
          temp_file.close()

        # -x: silent, -t png: PNG. -l<id> and -o: capture just that window with no shadow.
        screencapture_command = ['screencapture', '-x', '-t', 'png']
        if capture_window_id is not None:
          screencapture_command += ['-o', f'-l{capture_window_id}']
        screencapture_command.append(output_path)

        MCPLogger.log(TOOL_LOG_NAME, f"macOS screencapture: {' '.join(screencapture_command)}")
        result = subprocess.run(screencapture_command, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
          error_msg = result.stderr.strip() or "Unknown error"
          MCPLogger.log(TOOL_LOG_NAME, f"screencapture failed: {error_msg}")
          if temp_file:
            try: os.unlink(output_path)
            except Exception: pass
          return False, f"Screenshot failed: {error_msg}", None

        # Optional region crop (best-effort; requires PIL).
        if region and len(region) == 4:
          if Image is None:
            MCPLogger.log(TOOL_LOG_NAME, "Region crop requested but PIL is unavailable - returning full capture")
          else:
            try:
              region_x, region_y, region_width, region_height = region
              with Image.open(output_path) as captured_image:
                image_width, image_height = captured_image.size
                if region_x < 0:
                  region_x = image_width + region_x
                if region_y < 0:
                  region_y = image_height + region_y
                if region_width == 0:
                  region_width = image_width - region_x
                if region_height == 0:
                  region_height = image_height - region_y
                crop_box = (region_x, region_y, region_x + region_width, region_y + region_height)
                captured_image.crop(crop_box).save(output_path)
            except Exception as crop_error:
              MCPLogger.log(TOOL_LOG_NAME, f"Region crop failed ({crop_error}); returning full capture")

        # Return inline base64 when no filename was requested.
        if temp_file:
          import base64
          with open(output_path, 'rb') as f:
            image_data = f.read()
          try: os.unlink(output_path)
          except Exception: pass
          base64_data = base64.b64encode(image_data).decode('utf-8')
          MCPLogger.log(TOOL_LOG_NAME, f"macOS screenshot captured ({len(image_data)} bytes)")
          return True, "Screenshot captured successfully", base64_data

        MCPLogger.log(TOOL_LOG_NAME, f"Screenshot saved to {output_path}")
        return True, f"Screenshot saved to {output_path}", None

      except subprocess.TimeoutExpired:
        if temp_file:
          try: os.unlink(temp_file.name)
          except Exception: pass
        return False, "Screenshot timeout", None
      except Exception as e:
        if temp_file:
          try: os.unlink(temp_file.name)
          except Exception: pass
        MCPLogger.log(TOOL_LOG_NAME, f"Error in take_screenshot_functional: {e}")
        return False, f"Screenshot error: {e}", None

    # ------------------------------------------------------------------
    # macOS keyboard synthesis (send_text) via CGEvent
    # ------------------------------------------------------------------
    # Map the Windows virtual-key codes that parse_autohotkey_text_to_input_events() emits to
    # macOS virtual key codes (Carbon/HIToolbox kVK_* values). Only non-printable keys need to
    # be here; ordinary text is typed straight through as Unicode, so it needs no mapping.
    WINDOWS_VK_TO_MACOS_KEYCODE = {
        0x0D: 0x24,  # Enter/Return -> kVK_Return
        0x1B: 0x35,  # Escape -> kVK_Escape
        0x09: 0x30,  # Tab -> kVK_Tab
        0x20: 0x31,  # Space -> kVK_Space
        0x08: 0x33,  # Backspace -> kVK_Delete (backspace)
        0x2E: 0x75,  # Delete (forward) -> kVK_ForwardDelete
        0x25: 0x7B,  # Left arrow
        0x27: 0x7C,  # Right arrow
        0x28: 0x7D,  # Down arrow
        0x26: 0x7E,  # Up arrow
        0x24: 0x73,  # Home
        0x23: 0x77,  # End
        0x21: 0x74,  # Page Up
        0x22: 0x79,  # Page Down
        0x70: 0x7A, 0x71: 0x78, 0x72: 0x63, 0x73: 0x76,  # F1-F4
        0x74: 0x60, 0x75: 0x61, 0x76: 0x62, 0x77: 0x64,  # F5-F8
        0x78: 0x65, 0x79: 0x6D, 0x7A: 0x67, 0x7B: 0x6F,  # F9-F12
    }
    # US ANSI keyboard virtual key codes for the printable letters/digits/punctuation that a
    # modifier chord targets (e.g. '#c' -> Command+C). Sending a real key code (not a flagged
    # Unicode character) is what makes macOS shortcuts actually fire.
    MACOS_KEYCODE_FOR_ASCII_CHARACTER = {
        'a': 0, 's': 1, 'd': 2, 'f': 3, 'h': 4, 'g': 5, 'z': 6, 'x': 7, 'c': 8, 'v': 9,
        'b': 11, 'q': 12, 'w': 13, 'e': 14, 'r': 15, 'y': 16, 't': 17,
        '1': 18, '2': 19, '3': 20, '4': 21, '6': 22, '5': 23, '=': 24, '9': 25, '7': 26,
        '-': 27, '8': 28, '0': 29, ']': 30, 'o': 31, 'u': 32, '[': 33, 'i': 34, 'p': 35,
        'l': 37, 'j': 38, "'": 39, 'k': 40, ';': 41, '\\': 42, ',': 43, '/': 44, 'n': 45,
        'm': 46, '.': 47, '`': 50,
    }
    # Windows modifier VK codes -> macOS CGEvent flag masks (resolved lazily against Quartz).
    WINDOWS_MODIFIER_VK_TO_MACOS_FLAG_NAME = {
        0x10: "kCGEventFlagMaskShift", 0xA0: "kCGEventFlagMaskShift", 0xA1: "kCGEventFlagMaskShift",
        0x11: "kCGEventFlagMaskControl", 0xA2: "kCGEventFlagMaskControl", 0xA3: "kCGEventFlagMaskControl",
        0x12: "kCGEventFlagMaskAlternate", 0xA4: "kCGEventFlagMaskAlternate", 0xA5: "kCGEventFlagMaskAlternate",
        0x5B: "kCGEventFlagMaskCommand", 0x5C: "kCGEventFlagMaskCommand",  # Win key -> Command
    }

    def send_text_functional(hwnd_str: str, text: str) -> Tuple[bool, str]:
      """macOS implementation: send text/keystrokes via CGEvent, reusing the shared
      AutoHotkey-style parser so the syntax is identical across platforms.

      Ordinary characters are typed as Unicode (no keyboard-layout mapping needed). Special keys
      ({Enter}, {Tab}, {F1}-{F12}, arrows, etc.) map to macOS key codes. Modifier prefixes apply
      as CGEvent flags, with one macOS-specific mapping: '#' (the Windows key) becomes Command,
      so '#c' is Command-C (copy) on macOS. Requires macOS Accessibility permission.

      Args:
        hwnd_str: Window-number handle from list_windows (its app is focused first)
        text: Text/keystrokes in AutoHotkey-style syntax (see readme)

      Returns:
        Tuple of (success, message)
      """
      if not MACOS_HAS_QUARTZ:
        return False, "Quartz is unavailable; cannot synthesize keyboard input on this macOS build."
      if not macos_accessibility_permission_is_granted():
        return False, _MACOS_ACCESSIBILITY_PERMISSION_HINT
      try:
        # Focus the target window's application first (best effort).
        identifier = _macos_parse_window_identifier(hwnd_str) if hwnd_str else {"kind": "none"}
        if identifier.get("kind") == "window_id":
          record = _macos_find_window_record(int(identifier["window_id"]))
          if record and record.get("pid"):
            _macos_activate_pid(int(record["pid"]))
            time.sleep(0.15)
        else:
          pid, _app_name, _record = _macos_resolve_target_pid_and_appname(hwnd_str)
          if pid:
            _macos_activate_pid(pid)
            time.sleep(0.15)

        parsed_events = parse_autohotkey_text_to_input_events(text)

        # B3 parity with Windows: optionally refuse Win/Command chords (# prefix) when the
        # operator has disabled them, so an injected caller cannot fire system shortcuts.
        if not _capability_is_allowed(_get_system_tool_security_policy(), "allow_win_key_chords"):
          command_virtual_codes = {0x5B, 0x5C}
          if any(event_type in ("vk_press", "vk_down", "vk_up") and code in command_virtual_codes
                 for event_type, code, _ in parsed_events):
            return False, ("Command/Win-key chords are disabled by this server's system_tool_security "
                           "policy (allow_win_key_chords=false).")

        Q = macos_quartz_module
        active_modifier_flag_mask = 0

        def resolve_flag_mask(modifier_vk_code: int) -> int:
          flag_attribute_name = WINDOWS_MODIFIER_VK_TO_MACOS_FLAG_NAME.get(modifier_vk_code)
          if flag_attribute_name is None:
            return 0
          return int(getattr(Q, flag_attribute_name, 0))

        def post_keycode(macos_key_code: int):
          for is_key_down in (True, False):
            keyboard_event = Q.CGEventCreateKeyboardEvent(None, macos_key_code, is_key_down)
            if active_modifier_flag_mask:
              Q.CGEventSetFlags(keyboard_event, active_modifier_flag_mask)
            Q.CGEventPost(Q.kCGHIDEventTap, keyboard_event)
            time.sleep(0.005)

        def post_unicode(character: str):
          for is_key_down in (True, False):
            keyboard_event = Q.CGEventCreateKeyboardEvent(None, 0, is_key_down)
            Q.CGEventKeyboardSetUnicodeString(keyboard_event, len(character), character)
            if active_modifier_flag_mask:
              Q.CGEventSetFlags(keyboard_event, active_modifier_flag_mask)
            Q.CGEventPost(Q.kCGHIDEventTap, keyboard_event)
            time.sleep(0.005)

        for event_type, code, _ in parsed_events:
          if event_type == "vk_down":
            active_modifier_flag_mask |= resolve_flag_mask(code)
          elif event_type == "vk_up":
            active_modifier_flag_mask &= ~resolve_flag_mask(code)
          elif event_type == "vk_press":
            macos_key_code = WINDOWS_VK_TO_MACOS_KEYCODE.get(code)
            if macos_key_code is not None:
              post_keycode(macos_key_code)
            else:
              # A printable VK press (letter/digit/punct), typically part of a modifier chord
              # like Command+C. Prefer a real key code so the shortcut fires; if the modifier is
              # only Shift (or none), fall back to typing the character as Unicode.
              try:
                character = chr(code).lower()
              except Exception:
                character = ""
              ascii_key_code = MACOS_KEYCODE_FOR_ASCII_CHARACTER.get(character)
              # With any modifier held we must emit a real key event (so Command+C, Shift+A,
              # etc. actually fire); with no modifier a lone printable is just typed as text.
              if ascii_key_code is not None and active_modifier_flag_mask:
                post_keycode(ascii_key_code)
              elif character:
                post_unicode(character)
          elif event_type == "unicode":
            post_unicode(chr(code))

        display_text = text[:50] + "..." if len(text) > 50 else text
        MCPLogger.log(TOOL_LOG_NAME, f"Sent text/keys: {_redact_sensitive_for_log(text)} on macOS")
        return True, f"Successfully sent text '{display_text}'"
      except Exception as e:
        return False, f"Error sending text: {e}"

    # ------------------------------------------------------------------
    # macOS UI-element scanning / clicking via the Accessibility (AX) tree
    # ------------------------------------------------------------------
    class macos_ax_scan_result_holder_with_clickable_lookup:
      """Holds a completed macOS AX scan so the shared get_clickable_elements_functional (which
      calls .find_all_buttons_and_clickable_elements_with_coordinates()) works unchanged, exactly
      like the Windows UI-tree walker object it substitutes for."""
      def __init__(self, clickable_elements_with_coordinates: List[Dict[str, any]]):
        self._clickable_elements_with_coordinates = clickable_elements_with_coordinates
      def find_all_buttons_and_clickable_elements_with_coordinates(self) -> List[Dict[str, any]]:
        return self._clickable_elements_with_coordinates

    # AX roles that represent something a user can click/activate.
    _MACOS_CLICKABLE_AX_ROLES = {
        "AXButton", "AXMenuButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox",
        "AXRadioButton", "AXPopUpButton", "AXLink", "AXTab", "AXDisclosureTriangle",
        "AXIncrementor", "AXStepper", "AXSegmentedControl", "AXToolbarButton",
    }

    def _macos_walk_ax_tree(root_ax_element, max_depth: int, wall_clock_deadline: float) -> List[Dict[str, any]]:
      """Breadth-first walk of an AX element subtree, collecting a flat list of element dicts.

      Bounded by both max_depth and a wall-clock deadline so a huge or slow tree cannot block the
      worker (and therefore the MCP connection) thread indefinitely - the same protection the
      Windows UI walker applies."""
      AX = macos_accessibility_services_module
      collected_elements = []
      # Each queue item is (ax_element, depth, parent_index).
      breadth_first_queue = [(root_ax_element, 0, -1)]
      while breadth_first_queue:
        if time.monotonic() > wall_clock_deadline:
          MCPLogger.log(TOOL_LOG_NAME, f"macOS AX scan wall-clock budget exceeded after {len(collected_elements)} elements - stopping early")
          break
        current_element, depth, parent_index = breadth_first_queue.pop(0)
        role = _macos_ax_attribute_as_text(current_element, AX.kAXRoleAttribute)
        title = _macos_ax_attribute_as_text(current_element, AX.kAXTitleAttribute)
        description = _macos_ax_attribute_as_text(current_element, AX.kAXDescriptionAttribute)
        value_text = _macos_ax_attribute_as_text(current_element, AX.kAXValueAttribute)
        role_description = _macos_ax_attribute_as_text(current_element, AX.kAXRoleDescriptionAttribute)
        element_x, element_y, element_width, element_height = _macos_ax_element_bounds(current_element)
        this_index = len(collected_elements)
        collected_elements.append({
          "control_type": role,
          "subrole": _macos_ax_attribute_as_text(current_element, AX.kAXSubroleAttribute),
          "role_description": role_description,
          "name": title or description or value_text,
          "value_text": value_text,
          "help_text": _macos_ax_attribute_as_text(current_element, AX.kAXHelpAttribute),
          "automation_id": _macos_ax_attribute_as_text(current_element, AX.kAXIdentifierAttribute),
          "bounds": {"x": element_x, "y": element_y, "width": element_width, "height": element_height},
          "center_x": element_x + element_width // 2,
          "center_y": element_y + element_height // 2,
          "tree_depth_level": depth,
          "parent_index": parent_index,
          "is_clickable": role in _MACOS_CLICKABLE_AX_ROLES,
        })
        if depth < max_depth:
          children = _macos_ax_copy_attribute(current_element, AX.kAXChildrenAttribute)
          if children:
            try:
              for child_element in list(children):
                breadth_first_queue.append((child_element, depth + 1, this_index))
            except Exception:
              pass
      return collected_elements

    def scan_ui_elements_functional(window_title: Optional[str] = None, hwnd_str: Optional[str] = None, session_key: object = None) -> Dict[str, any]:
      """macOS implementation: extract the Accessibility (AX) element tree of a window/app.

      Mirrors the Windows scanner's contract: returns window_info, a flat extracted_ui_elements
      list (role, name, value, bounds, center coordinates, depth), and an inline clickable_elements
      list; the scan is also stored per-session so get_clickable_elements works afterwards.
      Requires macOS Accessibility permission.

      Args:
        window_title: Application/window title to target (matched against the CGWindow app name
          or window title), optional if hwnd_str is given
        hwnd_str: Window-number handle from list_windows, optional if window_title is given
        session_key: Per-caller key used to isolate this caller's stored scan

      Returns:
        Dict with window_info, extracted_ui_elements, clickable_elements (or an 'error' key)
      """
      if not MACOS_HAS_ACCESSIBILITY_API:
        return {"error": "ApplicationServices (AX API) is unavailable; cannot scan UI elements on this macOS build.", "extracted_ui_elements": []}
      if not macos_accessibility_permission_is_granted():
        return {"error": _MACOS_ACCESSIBILITY_PERMISSION_HINT, "extracted_ui_elements": []}
      try:
        target_pid = None
        target_app_name = None
        window_record = None
        if hwnd_str:
          target_pid, target_app_name, window_record = _macos_resolve_target_pid_and_appname(hwnd_str)
        elif window_title:
          # Match a CGWindow whose app name or title contains the requested text.
          wanted = window_title.lower()
          for record in _macos_copy_onscreen_window_records():
            if wanted in (record["app_name"] or "").lower() or wanted in (record["title"] or "").lower():
              target_pid = int(record["pid"]); target_app_name = record["app_name"]; window_record = record
              break
        if not target_pid:
          return {"error": f"Could not resolve a macOS application to scan from {'hwnd ' + hwnd_str if hwnd_str else 'title ' + repr(window_title)}", "extracted_ui_elements": []}

        AX = macos_accessibility_services_module
        # Prefer scanning just the matched window's subtree; fall back to the whole app element.
        root_element = _macos_ax_best_matching_window_element(target_pid, window_record)
        if root_element is None:
          root_element = AX.AXUIElementCreateApplication(target_pid)

        scan_wall_clock_deadline = time.monotonic() + 30.0
        extracted_elements = _macos_walk_ax_tree(root_element, max_depth=40, wall_clock_deadline=scan_wall_clock_deadline)

        clickable_elements = [
          {
            "name": element["name"],
            "control_type": element["control_type"],
            "center_x": element["center_x"],
            "center_y": element["center_y"],
            "bounds": element["bounds"],
          }
          for element in extracted_elements
          if element["is_clickable"] and (element["bounds"]["width"] > 0 or element["bounds"]["height"] > 0)
        ]

        scan_result = {
          "window_info": {
            "title": (window_record or {}).get("title", "") or target_app_name or "",
            "app_name": target_app_name or "",
            "pid": target_pid,
            "hwnd": hwnd_str or (str(window_record["window_id"]) if window_record else None),
          },
          "scan_summary": {
            "total_elements": len(extracted_elements),
            "total_clickable": len(clickable_elements),
          },
          "extracted_ui_elements": extracted_elements,
          "clickable_elements": clickable_elements,
          "total_clickable_found": len(clickable_elements),
        }
        _store_ui_scanner_for_session(session_key, macos_ax_scan_result_holder_with_clickable_lookup(clickable_elements))
        MCPLogger.log(TOOL_LOG_NAME, f"macOS AX scan: {len(extracted_elements)} elements, {len(clickable_elements)} clickable (pid {target_pid})")
        return scan_result
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in macOS scan_ui_elements_functional: {e}")
        return {"error": f"Error scanning UI elements: {e}", "extracted_ui_elements": []}

    def click_ui_element_functional(hwnd_str: str, element_name: str) -> Tuple[bool, str]:
      """macOS implementation: find a UI element by name and activate it via the AX API.

      Walks the target application's AX tree, matches an element whose title/description/value or
      identifier equals (preferred) or contains element_name, and performs its AXPress action;
      if the element exposes no press action, synthesizes a click at its center instead. Requires
      macOS Accessibility permission.

      Args:
        hwnd_str: Window-number handle from list_windows (or 'pid:<n>' / app name)
        element_name: Name, label, or AX identifier of the element to click

      Returns:
        Tuple of (success, message)
      """
      if not MACOS_HAS_ACCESSIBILITY_API:
        return False, "ApplicationServices (AX API) is unavailable; cannot click UI elements on this macOS build."
      if not macos_accessibility_permission_is_granted():
        return False, _MACOS_ACCESSIBILITY_PERMISSION_HINT
      try:
        target_pid, target_app_name, window_record = _macos_resolve_target_pid_and_appname(hwnd_str)
        if not target_pid:
          return False, f"Could not resolve a macOS application from handle '{hwnd_str}'"
        _macos_activate_pid(target_pid)
        time.sleep(0.15)
        AX = macos_accessibility_services_module
        root_element = _macos_ax_best_matching_window_element(target_pid, window_record)
        if root_element is None:
          root_element = AX.AXUIElementCreateApplication(target_pid)

        # Walk the tree, but keep live AX element handles so we can act on the match.
        wanted = element_name.strip()
        wanted_lower = wanted.lower()
        wall_clock_deadline = time.monotonic() + 20.0
        breadth_first_queue = [(root_element, 0)]
        exact_match_element = None
        substring_match_element = None
        while breadth_first_queue and (exact_match_element is None):
          if time.monotonic() > wall_clock_deadline:
            break
          current_element, depth = breadth_first_queue.pop(0)
          candidate_texts = [
            _macos_ax_attribute_as_text(current_element, AX.kAXTitleAttribute),
            _macos_ax_attribute_as_text(current_element, AX.kAXDescriptionAttribute),
            _macos_ax_attribute_as_text(current_element, AX.kAXValueAttribute),
            _macos_ax_attribute_as_text(current_element, AX.kAXIdentifierAttribute),
          ]
          for candidate_text in candidate_texts:
            if not candidate_text:
              continue
            if candidate_text == wanted:
              exact_match_element = current_element
              break
            if substring_match_element is None and wanted_lower in candidate_text.lower():
              substring_match_element = current_element
          if depth < 40:
            children = _macos_ax_copy_attribute(current_element, AX.kAXChildrenAttribute)
            if children:
              try:
                for child_element in list(children):
                  breadth_first_queue.append((child_element, depth + 1))
              except Exception:
                pass

        matched_element = exact_match_element or substring_match_element
        if matched_element is None:
          return False, f"Could not find a UI element matching '{element_name}' in '{target_app_name or target_pid}'"

        press_error = AX.AXUIElementPerformAction(matched_element, AX.kAXPressAction)
        if press_error == AX.kAXErrorSuccess:
          MCPLogger.log(TOOL_LOG_NAME, f"macOS AXPress on '{element_name}' (pid {target_pid})")
          return True, f"Successfully pressed UI element '{element_name}' in '{target_app_name or target_pid}'"

        # No press action - fall back to a synthesized click at the element's center.
        element_x, element_y, element_width, element_height = _macos_ax_element_bounds(matched_element)
        if element_width > 0 or element_height > 0:
          center_x = element_x + element_width // 2
          center_y = element_y + element_height // 2
          success, message = _macos_post_mouse_click_at_global_point(center_x, center_y, "left")
          if success:
            return True, f"Clicked UI element '{element_name}' at ({center_x}, {center_y}) [no AXPress action available]"
          return False, message
        return False, f"UI element '{element_name}' has no press action and no usable bounds to click."
      except Exception as e:
        return False, f"Error clicking UI element: {e}"

    def get_display_information_summary_and_full() -> Dict[str, any]:
      """macOS implementation: Get comprehensive display/monitor information.
      
      Uses system_profiler to get detailed display information including resolution,
      refresh rate, color depth, and multi-monitor layout.
      
      Returns:
          Dict containing display information similar to Windows implementation
      """
      try:
        display_info = {
          "displays": [],
          "primary_display_index": 0,  # macOS primary is typically index 0
          "total_display_count": 0,
          "virtual_screen": {
            "left": 0,
            "top": 0,
            "right": 0,
            "bottom": 0,
            "total_width": 0,
            "total_height": 0
          },
          "layout_description": ""
        }
        
        # Method 1: Use system_profiler for detailed display info (JSON output)
        try:
          MCPLogger.log(TOOL_LOG_NAME, "Getting macOS display info via system_profiler")
          result = subprocess.run(
            ['system_profiler', 'SPDisplaysDataType', '-json'],
            capture_output=True,
            text=True,
            timeout=10
          )
          
          if result.returncode == 0:
            import json
            profiler_data = json.loads(result.stdout)
            
            displays_data = profiler_data.get('SPDisplaysDataType', [])
            monitor_index = 0
            min_x, min_y, max_x, max_y = 0, 0, 0, 0
            
            for gpu in displays_data:
              # Each GPU can have multiple displays attached
              ndrvs = gpu.get('spdisplays_ndrvs', [])
              
              for display in ndrvs:
                display_data = {
                  "monitor_index": monitor_index,
                  "monitor_handle": f"macos_display_{monitor_index}",
                  "device_name": display.get('_name', f'Display {monitor_index}'),
                  "device_description": display.get('_name', ''),
                  "is_primary": display.get('spdisplays_main', 'spdisplays_not_main') == 'spdisplays_main',
                  "full_resolution": {
                    "left": 0,
                    "top": 0,
                    "right": 0,
                    "bottom": 0,
                    "width": 0,
                    "height": 0
                  },
                  "work_area": {
                    "left": 0,
                    "top": 0,
                    "right": 0,
                    "bottom": 0,
                    "width": 0,
                    "height": 0
                  }
                }
                
                # Parse resolution (format: "1920 x 1080" or "3840 x 2160 @ 60Hz")
                resolution_str = display.get('_spdisplays_resolution', display.get('spdisplays_resolution', ''))
                if resolution_str:
                  # Handle formats like "1920 x 1080" or "3840 x 2160 @ 60Hz (3840 x 2160)"
                  import re
                  res_match = re.search(r'(\d+)\s*x\s*(\d+)', resolution_str)
                  if res_match:
                    width = int(res_match.group(1))
                    height = int(res_match.group(2))
                    display_data["full_resolution"]["width"] = width
                    display_data["full_resolution"]["height"] = height
                    display_data["full_resolution"]["right"] = width
                    display_data["full_resolution"]["bottom"] = height
                    display_data["work_area"]["width"] = width
                    display_data["work_area"]["height"] = height - 25  # Approximate menu bar
                    display_data["work_area"]["top"] = 25
                    display_data["work_area"]["right"] = width
                    display_data["work_area"]["bottom"] = height
                    
                    # Update virtual screen bounds
                    max_x = max(max_x, width)
                    max_y = max(max_y, height)
                  
                  # Extract refresh rate if present
                  hz_match = re.search(r'@\s*(\d+)\s*Hz', resolution_str)
                  if hz_match:
                    display_data["refresh_rate_hz"] = int(hz_match.group(1))
                
                # Get color depth
                depth_str = display.get('spdisplays_depth', '')
                if '32' in depth_str or 'Billions' in depth_str:
                  display_data["color_depth_bits_per_pixel"] = 32
                elif '16' in depth_str or 'Thousands' in depth_str:
                  display_data["color_depth_bits_per_pixel"] = 16
                elif '8' in depth_str or '256' in depth_str:
                  display_data["color_depth_bits_per_pixel"] = 8
                
                # Get pixel info for Retina displays
                pixels_str = display.get('spdisplays_pixels', '')
                if pixels_str:
                  res_match = re.search(r'(\d+)\s*x\s*(\d+)', pixels_str)
                  if res_match:
                    display_data["native_pixel_width"] = int(res_match.group(1))
                    display_data["native_pixel_height"] = int(res_match.group(2))
                    # Calculate Retina scale factor
                    if display_data["full_resolution"]["width"] > 0:
                      scale = display_data["native_pixel_width"] / display_data["full_resolution"]["width"]
                      display_data["scale_factor_multiplier"] = round(scale, 2)
                      display_data["scale_factor_percent"] = int(scale * 100)
                      display_data["is_retina"] = scale > 1.0
                
                # Connection type
                display_data["connection_type"] = display.get('spdisplays_connection_type', 'Unknown')
                
                # Display type (built-in vs external)
                display_data["is_builtin"] = display.get('spdisplays_display_type', '') == 'spdisplays_built-in'
                
                # Mirror state
                display_data["is_mirrored"] = display.get('spdisplays_mirror', 'spdisplays_off') == 'spdisplays_on'
                
                # Ambient light compensation
                display_data["auto_brightness_enabled"] = display.get('spdisplays_ambient_brightness', 'spdisplays_off') == 'spdisplays_on'
                
                # Orientation (default is landscape)
                rotation = display.get('spdisplays_rotation', '')
                if 'supported' in rotation.lower() or not rotation:
                  display_data["orientation_degrees"] = 0
                  display_data["orientation_name"] = "landscape"
                else:
                  try:
                    display_data["orientation_degrees"] = int(rotation)
                    if display_data["orientation_degrees"] == 0:
                      display_data["orientation_name"] = "landscape"
                    elif display_data["orientation_degrees"] == 90:
                      display_data["orientation_name"] = "portrait_rotated_right"
                    elif display_data["orientation_degrees"] == 180:
                      display_data["orientation_name"] = "landscape_flipped"
                    elif display_data["orientation_degrees"] == 270:
                      display_data["orientation_name"] = "portrait_rotated_left"
                  except ValueError:
                    display_data["orientation_degrees"] = 0
                    display_data["orientation_name"] = "landscape"
                
                # Menu bar (macOS equivalent of taskbar)
                display_data["taskbar"] = {
                  "visible": True,
                  "position": "top",
                  "size": 25  # Standard macOS menu bar height
                }
                
                if display_data["is_primary"]:
                  display_info["primary_display_index"] = monitor_index
                
                display_info["displays"].append(display_data)
                monitor_index += 1
                
        except subprocess.TimeoutExpired:
          MCPLogger.log(TOOL_LOG_NAME, "Timeout getting display info via system_profiler")
        except json.JSONDecodeError as e:
          MCPLogger.log(TOOL_LOG_NAME, f"Error parsing system_profiler JSON: {e}")
        except Exception as e:
          MCPLogger.log(TOOL_LOG_NAME, f"Error with system_profiler: {e}")
        
        # Method 2: Fallback using screeninfo if system_profiler didn't work
        if not display_info["displays"]:
          try:
            # Try using screeninfo library if available
            from screeninfo import get_monitors
            
            for idx, m in enumerate(get_monitors()):
              display_data = {
                "monitor_index": idx,
                "monitor_handle": f"macos_display_{idx}",
                "device_name": m.name or f"Display {idx}",
                "is_primary": m.is_primary,
                "full_resolution": {
                  "left": m.x,
                  "top": m.y,
                  "right": m.x + m.width,
                  "bottom": m.y + m.height,
                  "width": m.width,
                  "height": m.height
                },
                "work_area": {
                  "left": m.x,
                  "top": m.y + 25,  # Menu bar
                  "right": m.x + m.width,
                  "bottom": m.y + m.height,
                  "width": m.width,
                  "height": m.height - 25
                },
                "taskbar": {"visible": True, "position": "top", "size": 25}
              }
              
              if m.is_primary:
                display_info["primary_display_index"] = idx
              
              display_info["displays"].append(display_data)
              
          except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "screeninfo not available for fallback")
          except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error with screeninfo fallback: {e}")
        
        # Update totals and virtual screen
        display_info["total_display_count"] = len(display_info["displays"])
        
        if display_info["displays"]:
          min_x = min(d["full_resolution"]["left"] for d in display_info["displays"])
          min_y = min(d["full_resolution"]["top"] for d in display_info["displays"])
          max_x = max(d["full_resolution"]["right"] for d in display_info["displays"])
          max_y = max(d["full_resolution"]["bottom"] for d in display_info["displays"])
          
          display_info["virtual_screen"] = {
            "left": min_x,
            "top": min_y,
            "right": max_x,
            "bottom": max_y,
            "total_width": max_x - min_x,
            "total_height": max_y - min_y
          }
          
          # Generate layout description
          layout_parts = []
          for d in display_info["displays"]:
            primary_marker = " (PRIMARY)" if d["is_primary"] else ""
            retina_marker = " Retina" if d.get("is_retina") else ""
            scale_info = f" @ {d.get('scale_factor_percent', 100)}%" if d.get('scale_factor_percent', 100) != 100 else ""
            refresh_info = f" {d.get('refresh_rate_hz', 60)}Hz" if d.get('refresh_rate_hz') else ""
            layout_parts.append(
              f"{d['device_name']}{primary_marker}{retina_marker}: "
              f"{d['full_resolution']['width']}x{d['full_resolution']['height']}{scale_info}{refresh_info}"
            )
          display_info["layout_description"] = "; ".join(layout_parts)
        
        return display_info
        
      except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error in get_display_information_summary_and_full: {e}")
        return {"error": f"Failed to get display information: {e}"}


################################################################################################################################
################################################################################################################################
################################                       LINUX SPECIFIC ROUTINES                  ################################
################################################################################################################################
################################################################################################################################

# Linux-specific implementations
# These use:
# - PyWinCtl (preferred, works on X11 and Wayland)
# - python-xlib (fallback for X11 only)
# - scrot/ImageMagick for screenshots

if IS_LINUX:
    def list_windows_functional(include_all: bool = False) -> List[Dict]:
        """Linux implementation using PyWinCtl or Xlib.
        
        Works on both X11 and Wayland (via PyWinCtl) or X11 only (via Xlib fallback).
        
        Args:
            include_all: If True, includes all windows; if False, filters out utility windows
            
        Returns:
            List of window dictionaries with properties
        """
        try:
            if LINUX_HAS_PYWINCTL:
                # Use PyWinCtl (preferred - works on X11 and Wayland)
                try:
                    all_windows = pwc.getAllWindows()
                    windows = []
                    
                    for idx, win in enumerate(all_windows):
                        try:
                            # Get window properties
                            title = win.title
                            if not title and not include_all:
                                continue
                                
                            # Get geometry
                            box = win.box
                            
                            # Create window object
                            window_obj = {
                                'hwnd': f"linux_win_{win._hWnd if hasattr(win, '_hWnd') else idx}",
                                'title': title or "(No title)",
                                'class': win.getAppName() if hasattr(win, 'getAppName') else 'Unknown',
                                'x': box.left,
                                'y': box.top,
                                'width': box.width,
                                'height': box.height,
                                'style_flags': {},
                                'process_id': 0,  # Not easily available via PyWinCtl
                                'process_name': win.getAppName() if hasattr(win, 'getAppName') else 'Unknown',
                                'process_exe': 'Unknown',
                                'is_visible': win.isVisible if hasattr(win, 'isVisible') else True,
                                'is_minimized': win.isMinimized if hasattr(win, 'isMinimized') else False,
                                'is_maximized': win.isMaximized if hasattr(win, 'isMaximized') else False
                            }
                            
                            windows.append(window_obj)
                            
                        except Exception as e:
                            MCPLogger.log(TOOL_LOG_NAME, f"Error processing window: {e}")
                            continue
                    
                    windows.sort(key=lambda w: w['title'].lower())
                    MCPLogger.log(TOOL_LOG_NAME, f"Found {len(windows)} Linux windows via PyWinCtl")
                    return windows
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"PyWinCtl error: {e}, trying fallback")
                    
            # Fallback to Xlib (X11 only)
            if LINUX_HAS_XLIB:
                try:
                    d = display.Display()
                    root = d.screen().root
                    
                    # Get list of windows
                    window_ids = root.get_full_property(
                        d.intern_atom('_NET_CLIENT_LIST'),
                        X.AnyPropertyType
                    ).value
                    
                    windows = []
                    for idx, wid in enumerate(window_ids):
                        try:
                            win = d.create_resource_object('window', wid)
                            
                            # Get window title
                            title_atom = d.intern_atom('_NET_WM_NAME')
                            title_prop = win.get_full_property(title_atom, 0)
                            title = title_prop.value.decode('utf-8') if title_prop else ""
                            
                            if not title:
                                # Try WM_NAME as fallback
                                title = win.get_wm_name() or ""
                            
                            if not title and not include_all:
                                continue
                            
                            # Get geometry
                            geom = win.get_geometry()
                            
                            # Get window class
                            wm_class = win.get_wm_class()
                            class_name = wm_class[1] if wm_class and len(wm_class) > 1 else "Unknown"
                            
                            window_obj = {
                                'hwnd': f"linux_xwin_{wid}",
                                'title': title or "(No title)",
                                'class': class_name,
                                'x': geom.x,
                                'y': geom.y,
                                'width': geom.width,
                                'height': geom.height,
                                'style_flags': {},
                                'process_id': 0,
                                'process_name': class_name,
                                'process_exe': 'Unknown',
                                'is_visible': True,
                                'is_minimized': False,
                                'is_maximized': False
                            }
                            
                            windows.append(window_obj)
                            
                        except Exception as e:
                            MCPLogger.log(TOOL_LOG_NAME, f"Error processing X11 window: {e}")
                            continue
                    
                    windows.sort(key=lambda w: w['title'].lower())
                    MCPLogger.log(TOOL_LOG_NAME, f"Found {len(windows)} Linux windows via Xlib")
                    return windows
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Xlib error: {e}")
                    return []
            
            # No libraries available
            MCPLogger.log(TOOL_LOG_NAME, "No Linux window management libraries available")
            return []
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error in list_windows_functional: {e}")
            return []
    
    def activate_window_functional(hwnd_str: str, request_focus: bool = False) -> Tuple[bool, str]:
        """Linux implementation for activating/focusing a window.
        
        Uses PyWinCtl (preferred) or wmctrl command-line tool as fallback.
        
        Args:
            hwnd_str: Window identifier from list_windows
            request_focus: Whether to activate the window (bring to front and focus)
            
        Returns:
            Tuple of (success, message)
        """
        try:
            if LINUX_HAS_PYWINCTL:
                # Use PyWinCtl
                try:
                    # Extract window ID from hwnd_str
                    all_windows = pwc.getAllWindows()
                    target_window = None
                    
                    # Try to match by hwnd or title
                    for win in all_windows:
                        win_id = f"linux_win_{win._hWnd if hasattr(win, '_hWnd') else 0}"
                        if hwnd_str == win_id or hwnd_str in win.title:
                            target_window = win
                            break
                    
                    if not target_window:
                        return False, f"Window not found: {hwnd_str}"
                    
                    # Activate the window
                    if request_focus:
                        target_window.activate()
                    else:
                        target_window.raiseWindow()
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Successfully activated window: {target_window.title}")
                    return True, f"Successfully activated window '{target_window.title}'"
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"PyWinCtl activate error: {e}")
                    # Fall through to wmctrl fallback
            
            # Fallback to wmctrl command
            try:
                # Extract window ID if it's in our format
                if hwnd_str.startswith('linux_xwin_'):
                    wid = hwnd_str.replace('linux_xwin_', '')
                    cmd = ['wmctrl', '-i', '-a', wid]
                else:
                    # Try by title
                    cmd = ['wmctrl', '-a', hwnd_str]
                
                MCPLogger.log(TOOL_LOG_NAME, f"Activating Linux window via wmctrl: {hwnd_str}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                
                if result.returncode == 0:
                    MCPLogger.log(TOOL_LOG_NAME, f"Successfully activated window via wmctrl")
                    return True, f"Successfully activated window"
                else:
                    error_msg = result.stderr.strip() or "Unknown error"
                    return False, f"Failed to activate window: {error_msg}"
                    
            except FileNotFoundError:
                return False, "wmctrl not found. Install with: sudo dnf install wmctrl (RHEL/Fedora) or sudo apt install wmctrl (Ubuntu/Debian)"
            except subprocess.TimeoutExpired:
                return False, "Timeout while activating window"
            except Exception as e:
                return False, f"Error activating window: {e}"
                
        except Exception as e:
            return False, f"Error in activate_window_functional: {e}"
    
    def move_window_functional(hwnd_str: str, x: int, y: int, width: int, height: int) -> Tuple[bool, str]:
        """Linux implementation for moving/resizing windows.
        
        Uses PyWinCtl (preferred) or wmctrl command-line tool as fallback.
        
        Args:
            hwnd_str: Window identifier from list_windows
            x, y: New position
            width, height: New dimensions
            
        Returns:
            Tuple of (success, message)
        """
        try:
            if LINUX_HAS_PYWINCTL:
                # Use PyWinCtl
                try:
                    all_windows = pwc.getAllWindows()
                    target_window = None
                    
                    for win in all_windows:
                        win_id = f"linux_win_{win._hWnd if hasattr(win, '_hWnd') else 0}"
                        if hwnd_str == win_id or hwnd_str in win.title:
                            target_window = win
                            break
                    
                    if not target_window:
                        return False, f"Window not found: {hwnd_str}"
                    
                    # Move and resize
                    target_window.moveTo(x, y)
                    target_window.resizeTo(width, height)
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Successfully moved/resized window: {target_window.title}")
                    return True, f"Window moved and resized successfully"
                    
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"PyWinCtl move error: {e}")
                    # Fall through to wmctrl fallback
            
            # Fallback to wmctrl command
            try:
                # wmctrl format: wmctrl -i -r <window_id> -e 0,x,y,width,height
                if hwnd_str.startswith('linux_xwin_'):
                    wid = hwnd_str.replace('linux_xwin_', '')
                    cmd = ['wmctrl', '-i', '-r', wid, '-e', f'0,{x},{y},{width},{height}']
                else:
                    cmd = ['wmctrl', '-r', hwnd_str, '-e', f'0,{x},{y},{width},{height}']
                
                MCPLogger.log(TOOL_LOG_NAME, f"Moving/resizing Linux window via wmctrl: {hwnd_str}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                
                if result.returncode == 0:
                    MCPLogger.log(TOOL_LOG_NAME, f"Successfully moved window via wmctrl")
                    return True, f"Window moved and resized successfully"
                else:
                    error_msg = result.stderr.strip() or "Unknown error"
                    return False, f"Failed to move window: {error_msg}"
                    
            except FileNotFoundError:
                return False, "wmctrl not found. Install with: sudo dnf install wmctrl"
            except subprocess.TimeoutExpired:
                return False, "Timeout while moving window"
            except Exception as e:
                return False, f"Error moving window: {e}"
                
        except Exception as e:
            return False, f"Error in move_window_functional: {e}"
    
    def take_screenshot_functional(hwnd_str: str, filename: Optional[str] = None, region: Optional[List[int]] = None) -> Tuple[bool, str, Optional[str]]:
        """Linux implementation using scrot, ImageMagick, or gnome-screenshot.
        
        Args:
            hwnd_str: Window identifier (for window-specific screenshots)
            filename: Optional filename to save screenshot to
            region: Optional region [x, y, width, height]
            
        Returns:
            Tuple of (success, message, base64_image_data)
        """
        try:
            # Create temp file if no filename specified
            temp_file = None
            if not filename:
                temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
                filename = temp_file.name
                temp_file.close()
            
            # Try different screenshot tools in order of preference
            screenshot_taken = False
            
            # Method 1: Try scrot (most reliable)
            if not screenshot_taken:
                try:
                    if region:
                        # scrot with region: -a x,y,width,height
                        x, y, w, h = region
                        cmd = ['scrot', '-a', f'{x},{y},{w},{h}', filename]
                    else:
                        # Full screen screenshot
                        cmd = ['scrot', filename]
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Taking Linux screenshot with scrot to: {filename}")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        screenshot_taken = True
                        MCPLogger.log(TOOL_LOG_NAME, "Screenshot taken with scrot")
                except FileNotFoundError:
                    pass  # scrot not available
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"scrot error: {e}")
            
            # Method 2: Try gnome-screenshot (GNOME desktop)
            if not screenshot_taken:
                try:
                    cmd = ['gnome-screenshot', '-f', filename]
                    if region:
                        # gnome-screenshot doesn't support regions easily, skip
                        pass
                    else:
                        MCPLogger.log(TOOL_LOG_NAME, f"Taking Linux screenshot with gnome-screenshot to: {filename}")
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            screenshot_taken = True
                            MCPLogger.log(TOOL_LOG_NAME, "Screenshot taken with gnome-screenshot")
                except FileNotFoundError:
                    pass
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"gnome-screenshot error: {e}")
            
            # Method 3: Try ImageMagick import
            if not screenshot_taken:
                try:
                    if region:
                        x, y, w, h = region
                        cmd = ['import', '-window', 'root', '-crop', f'{w}x{h}+{x}+{y}', filename]
                    else:
                        cmd = ['import', '-window', 'root', filename]
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Taking Linux screenshot with ImageMagick to: {filename}")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        screenshot_taken = True
                        MCPLogger.log(TOOL_LOG_NAME, "Screenshot taken with ImageMagick")
                except FileNotFoundError:
                    pass
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"ImageMagick error: {e}")
            
            if not screenshot_taken:
                if temp_file:
                    try:
                        os.unlink(filename)
                    except:
                        pass
                return False, "No screenshot tool available. Install scrot: sudo dnf install scrot", None
            
            # Read and return base64 if temp file
            if temp_file:
                try:
                    import base64
                    with open(filename, 'rb') as f:
                        image_data = f.read()
                    base64_data = base64.b64encode(image_data).decode('utf-8')
                    
                    try:
                        os.unlink(filename)
                    except:
                        pass
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Screenshot captured (size: {len(image_data)} bytes)")
                    return True, "Screenshot captured successfully", base64_data
                except Exception as e:
                    try:
                        os.unlink(filename)
                    except:
                        pass
                    return False, f"Error processing screenshot: {e}", None
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"Screenshot saved to {filename}")
                return True, f"Screenshot saved to {filename}", None
                
        except Exception as e:
            if temp_file and filename:
                try:
                    os.unlink(filename)
                except:
                    pass
            return False, f"Screenshot error: {e}", None

    def get_display_information_summary_and_full() -> Dict[str, any]:
        """Linux implementation: Get comprehensive display/monitor information.
        
        Uses xrandr, PyWinCtl, or screeninfo to get detailed display information
        including resolution, refresh rate, and multi-monitor layout.
        
        Returns:
            Dict containing display information similar to Windows/macOS implementations
        """
        try:
            display_info = {
                "displays": [],
                "primary_display_index": 0,
                "total_display_count": 0,
                "virtual_screen": {
                    "left": 0,
                    "top": 0,
                    "right": 0,
                    "bottom": 0,
                    "total_width": 0,
                    "total_height": 0
                },
                "layout_description": ""
            }
            
            # Method 1: Use xrandr for detailed display info (most reliable on X11)
            try:
                MCPLogger.log(TOOL_LOG_NAME, "Getting Linux display info via xrandr")
                result = subprocess.run(
                    ['xrandr', '--query'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    import re
                    lines = result.stdout.strip().split('\n')
                    
                    monitor_index = 0
                    current_display = None
                    
                    for line in lines:
                        # Match connected displays: "DP-1 connected primary 1920x1080+0+0 ..."
                        # or "HDMI-1 connected 2560x1440+1920+0 ..."
                        connected_match = re.match(
                            r'^(\S+)\s+connected\s*(primary)?\s*(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s*(.*)$',
                            line
                        )
                        
                        if connected_match:
                            output_name = connected_match.group(1)
                            is_primary = connected_match.group(2) == 'primary'
                            width = int(connected_match.group(3))
                            height = int(connected_match.group(4))
                            pos_x = int(connected_match.group(5))
                            pos_y = int(connected_match.group(6))
                            extra_info = connected_match.group(7) or ''
                            
                            current_display = {
                                "monitor_index": monitor_index,
                                "monitor_handle": f"linux_display_{output_name}",
                                "device_name": output_name,
                                "device_description": output_name,
                                "is_primary": is_primary,
                                "full_resolution": {
                                    "left": pos_x,
                                    "top": pos_y,
                                    "right": pos_x + width,
                                    "bottom": pos_y + height,
                                    "width": width,
                                    "height": height
                                },
                                "work_area": {
                                    "left": pos_x,
                                    "top": pos_y,
                                    "right": pos_x + width,
                                    "bottom": pos_y + height,
                                    "width": width,
                                    "height": height
                                },
                                "orientation_degrees": 0,
                                "orientation_name": "landscape"
                            }
                            
                            # Parse rotation from extra info
                            if 'left' in extra_info:
                                current_display["orientation_degrees"] = 90
                                current_display["orientation_name"] = "portrait_rotated_left"
                            elif 'right' in extra_info:
                                current_display["orientation_degrees"] = 270
                                current_display["orientation_name"] = "portrait_rotated_right"
                            elif 'inverted' in extra_info:
                                current_display["orientation_degrees"] = 180
                                current_display["orientation_name"] = "landscape_flipped"
                            
                            # Parse physical size if available (e.g., "527mm x 296mm")
                            size_match = re.search(r'(\d+)mm\s*x\s*(\d+)mm', extra_info)
                            if size_match:
                                phys_width_mm = int(size_match.group(1))
                                phys_height_mm = int(size_match.group(2))
                                current_display["physical_width_mm"] = phys_width_mm
                                current_display["physical_height_mm"] = phys_height_mm
                                # Calculate DPI
                                if phys_width_mm > 0 and phys_height_mm > 0:
                                    dpi_x = round((width * 25.4) / phys_width_mm)
                                    dpi_y = round((height * 25.4) / phys_height_mm)
                                    current_display["dpi_x"] = dpi_x
                                    current_display["dpi_y"] = dpi_y
                                    current_display["scale_factor_percent"] = int((dpi_x / 96.0) * 100)
                                    current_display["scale_factor_multiplier"] = round(dpi_x / 96.0, 2)
                            
                            # Taskbar placeholder (varies by DE)
                            current_display["taskbar"] = {
                                "visible": True,
                                "position": "bottom",  # Most common default
                                "size": 48  # Common panel height
                            }
                            
                            if is_primary:
                                display_info["primary_display_index"] = monitor_index
                            
                            display_info["displays"].append(current_display)
                            monitor_index += 1
                            continue
                        
                        # Match mode lines to get refresh rate for current display
                        # Format: "   1920x1080     60.00*+  59.94    50.00"
                        # The * indicates current mode, + indicates preferred
                        if current_display and line.startswith('   '):
                            mode_match = re.match(r'\s+(\d+)x(\d+)\s+([\d.]+)\*', line)
                            if mode_match:
                                mode_width = int(mode_match.group(1))
                                mode_height = int(mode_match.group(2))
                                refresh_rate = float(mode_match.group(3))
                                
                                # Only set if this matches the current resolution
                                if (mode_width == current_display["full_resolution"]["width"] and
                                    mode_height == current_display["full_resolution"]["height"]):
                                    current_display["refresh_rate_hz"] = int(round(refresh_rate))
                                    
            except FileNotFoundError:
                MCPLogger.log(TOOL_LOG_NAME, "xrandr not available")
            except subprocess.TimeoutExpired:
                MCPLogger.log(TOOL_LOG_NAME, "Timeout getting display info via xrandr")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error with xrandr: {e}")
            
            # Method 2: Fallback using screeninfo library
            if not display_info["displays"]:
                try:
                    from screeninfo import get_monitors
                    
                    for idx, m in enumerate(get_monitors()):
                        display_data = {
                            "monitor_index": idx,
                            "monitor_handle": f"linux_display_{idx}",
                            "device_name": m.name or f"Display {idx}",
                            "is_primary": m.is_primary,
                            "full_resolution": {
                                "left": m.x,
                                "top": m.y,
                                "right": m.x + m.width,
                                "bottom": m.y + m.height,
                                "width": m.width,
                                "height": m.height
                            },
                            "work_area": {
                                "left": m.x,
                                "top": m.y,
                                "right": m.x + m.width,
                                "bottom": m.y + m.height,
                                "width": m.width,
                                "height": m.height
                            },
                            "taskbar": {"visible": True, "position": "bottom", "size": 48}
                        }
                        
                        # Try to get physical size if available
                        if hasattr(m, 'width_mm') and m.width_mm:
                            display_data["physical_width_mm"] = m.width_mm
                        if hasattr(m, 'height_mm') and m.height_mm:
                            display_data["physical_height_mm"] = m.height_mm
                        
                        if m.is_primary:
                            display_info["primary_display_index"] = idx
                        
                        display_info["displays"].append(display_data)
                        
                except ImportError:
                    MCPLogger.log(TOOL_LOG_NAME, "screeninfo not available for fallback")
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error with screeninfo fallback: {e}")
            
            # Method 3: Try PyWinCtl if available (works on Wayland too)
            if not display_info["displays"] and LINUX_HAS_PYWINCTL:
                try:
                    # PyWinCtl can get monitor info on some systems
                    monitors = pwc.getAllScreens() if hasattr(pwc, 'getAllScreens') else None
                    if monitors:
                        for idx, (name, info) in enumerate(monitors.items()):
                            display_data = {
                                "monitor_index": idx,
                                "monitor_handle": f"linux_display_{name}",
                                "device_name": name,
                                "is_primary": idx == 0,  # Assume first is primary
                                "full_resolution": {
                                    "left": info.get('x', 0),
                                    "top": info.get('y', 0),
                                    "right": info.get('x', 0) + info.get('width', 0),
                                    "bottom": info.get('y', 0) + info.get('height', 0),
                                    "width": info.get('width', 0),
                                    "height": info.get('height', 0)
                                },
                                "work_area": {
                                    "left": info.get('x', 0),
                                    "top": info.get('y', 0),
                                    "right": info.get('x', 0) + info.get('width', 0),
                                    "bottom": info.get('y', 0) + info.get('height', 0),
                                    "width": info.get('width', 0),
                                    "height": info.get('height', 0)
                                },
                                "taskbar": {"visible": True, "position": "bottom", "size": 48}
                            }
                            display_info["displays"].append(display_data)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error with PyWinCtl monitors: {e}")
            
            # Update totals and virtual screen
            display_info["total_display_count"] = len(display_info["displays"])
            
            if display_info["displays"]:
                min_x = min(d["full_resolution"]["left"] for d in display_info["displays"])
                min_y = min(d["full_resolution"]["top"] for d in display_info["displays"])
                max_x = max(d["full_resolution"]["right"] for d in display_info["displays"])
                max_y = max(d["full_resolution"]["bottom"] for d in display_info["displays"])
                
                display_info["virtual_screen"] = {
                    "left": min_x,
                    "top": min_y,
                    "right": max_x,
                    "bottom": max_y,
                    "total_width": max_x - min_x,
                    "total_height": max_y - min_y
                }
                
                # Generate layout description
                layout_parts = []
                sorted_displays = sorted(display_info["displays"], key=lambda d: (d["full_resolution"]["left"], d["full_resolution"]["top"]))
                for d in sorted_displays:
                    primary_marker = " (PRIMARY)" if d["is_primary"] else ""
                    scale_info = f" @ {d.get('scale_factor_percent', 100)}%" if d.get('scale_factor_percent', 100) != 100 else ""
                    refresh_info = f" {d.get('refresh_rate_hz', 60)}Hz" if d.get('refresh_rate_hz') else ""
                    layout_parts.append(
                        f"{d['device_name']}{primary_marker}: "
                        f"{d['full_resolution']['width']}x{d['full_resolution']['height']}{scale_info}{refresh_info} "
                        f"at position ({d['full_resolution']['left']}, {d['full_resolution']['top']})"
                    )
                display_info["layout_description"] = "; ".join(layout_parts)
            
            return display_info
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error in get_display_information_summary_and_full: {e}")
            return {"error": f"Failed to get display information: {e}"}


################################################################################################################################
################################################################################################################################
################################                    COMMON CODE FOR ALL PLATFORMS               ################################
################################################################################################################################
################################################################################################################################







# Map of tool names to their handlers
HANDLERS = {
    TOOL_NAME: handle_system
    # do not add "about" here, which is an operation of the system tool, not a tool itself.
}
