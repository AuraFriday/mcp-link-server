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
"signature": "ꓔꓐƐꓣꓬτΤƧD𝟚×ƛƲȢꜱƶ4սԝ𝟚K𝕌ⲢꓦɅꓑꓗᴛƎ4ƏᖴƛꓧᏂSꓟƐď𝟙ÞUꓐЈр𝟨wƿƶĵᎪȜᏮɪŧΜ𝟩ωοЗᴠРLΗƵEĵƧO7АᏴCƶǝВ𝟚KƍkΝ3WßАɯСոĐΜbꓑƘȠƎ𐓒5ᗪkеʋᒿƱᛕLМuꓔƘ"
"signdate": "2025-11-26T02:00:38.554Z",
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
    # macOS-specific imports (to be implemented)
    try:
        # import Quartz  # For window management
        # import AppKit  # For application control
        # import Cocoa   # For UI automation
        pass
    except ImportError as e:
        MCPLogger.log("SYSTEM", f"Warning: macOS-specific import failed: {e}")
        
elif IS_LINUX:
    # Linux-specific imports
    try:
        # Try PyWinCtl first (cross-platform, works on X11 and Wayland)
        try:
            import pywinctl as pwc
            LINUX_HAS_PYWINCTL = True
        except ImportError:
            LINUX_HAS_PYWINCTL = False
            MCPLogger.log("SYSTEM", "PyWinCtl not available - install with: pip install pywinctl")
        
        # Fallback to X11-specific tools
        try:
            from Xlib import X, display
            from Xlib.error import DisplayError
            LINUX_HAS_XLIB = True
        except ImportError:
            LINUX_HAS_XLIB = False
            
    except ImportError as e:
        MCPLogger.log("SYSTEM", f"Warning: Linux-specific import failed: {e}")
        LINUX_HAS_PYWINCTL = False
        LINUX_HAS_XLIB = False

# Constants
VERSION = "1.1.0.0"
TOOL_LOG_NAME = "SYSTEM"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"system{TOOL_NAME_SUFFIX}"


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

@dataclass
class completed_terminal_session_with_full_history:
    """Information about a completed terminal session including final results"""
    process_id: int
    complete_output_text: str
    final_exit_code: Optional[int]
    session_start_time: datetime
    session_end_time: datetime

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
        
    def start_command_execution_with_timeout_and_background_support(
        self, 
        command_text: str, 
        timeout_milliseconds: int = 30000,
        shell_path: Optional[str] = None
    ) -> command_execution_result_with_background_support:
        """Execute a command with timeout support, allowing it to continue in background"""
        
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
            
            MCPLogger.log(TOOL_LOG_NAME, f"Starting command execution: {command_text[:100]}... (shell: {shell_executable or 'default'})")
            
            # Start the process
            if platform.system() == "Windows":
                if shell_path and shell_path.lower() in ["wsl", "bash"]:
                    # Special handling for WSL
                    if command_text.startswith("wsl "):
                        # Already prefixed with wsl
                        wsl_command = command_text
                    else:
                        # Wrap command for WSL execution
                        escaped_command = command_text.replace('"', '\\"')
                        wsl_command = f'wsl -e bash -c "{escaped_command}"'
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing WSL command: {wsl_command[:100]}...")
                    process = subprocess.Popen(
                        wsl_command,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=0,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    )
                else:
                    # Windows process creation with specified shell
                    popen_kwargs = {
                        'shell': use_shell,
                        'stdout': subprocess.PIPE,
                        'stderr': subprocess.STDOUT,
                        'text': True,
                        'bufsize': 0,
                        'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    }
                    
                    if shell_executable:
                        popen_kwargs['executable'] = shell_executable
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Windows command: {command_text[:100]}...")
                    process = subprocess.Popen(command_text, **popen_kwargs)
            else:
                # Unix process creation
                if shell_executable:
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Unix command with shell {shell_executable}: {command_text[:100]}...")
                    process = subprocess.Popen(
                        [shell_executable, '-c', command_text],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=0,
                        preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                    )
                else:
                    MCPLogger.log(TOOL_LOG_NAME, f"Executing Unix command: {command_text[:100]}...")
                    process = subprocess.Popen(
                        command_text.split(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=0,
                        preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                    )
            
            # Create unique session ID
            session_id = self.next_session_id
            self.next_session_id += 1
            
            # Set up output queue and reading thread
            output_queue = queue.Queue()
            
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
            
            # Start output reading thread
            reader_thread = threading.Thread(target=output_reader_thread, daemon=True)
            reader_thread.start()
            
            # Create session object
            session = terminal_session_with_process_tracking(
                process_id=session_id,
                process=process,
                accumulated_output_buffer="",
                newly_available_output_since_last_read="",
                command_execution_has_completed=False,
                session_creation_timestamp=datetime.now(),
                output_reading_thread=reader_thread,
                output_queue=output_queue,
                last_exit_code=None
            )
            
            self.active_terminal_sessions[session_id] = session
            
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
        
        session = self.active_terminal_sessions.get(session_id)
        if not session:
            # Check completed sessions
            completed = self.completed_session_history.get(session_id)
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
    
    def get_list_of_all_active_sessions_with_status(self) -> List[Dict[str, any]]:
        """Get list of all active sessions with their status"""
        
        current_time = datetime.now()
        active_sessions = []
        
        for session_id, session in self.active_terminal_sessions.items():
            runtime_seconds = (current_time - session.session_creation_timestamp).total_seconds()
            
            active_sessions.append({
                "session_id": session_id,
                "is_completed": session.command_execution_has_completed,
                "runtime_seconds": round(runtime_seconds, 2),
                "has_new_output": len(session.newly_available_output_since_last_read) > 0,
                "total_output_length": len(session.accumulated_output_buffer)
            })
        
        return active_sessions
    
    def _move_session_to_completed(self, session_id: int):
        """Move a session from active to completed"""
        
        session = self.active_terminal_sessions.get(session_id)
        if not session:
            return
        
        completed_session = completed_terminal_session_with_full_history(
            process_id=session_id,
            complete_output_text=session.accumulated_output_buffer,
            final_exit_code=session.last_exit_code,
            session_start_time=session.session_creation_timestamp,
            session_end_time=datetime.now()
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
        
    def set_electron_mode(self, is_electron: bool):
        """Enable special handling for Electron apps"""
        self.is_electron_app = is_electron
        if is_electron:
            self.maximum_tree_traversal_depth = 50
            self.include_all_chrome_elements = True
            MCPLogger.log(TOOL_LOG_NAME, "Electron mode enabled - using deeper scanning")
    
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
                try:
                    renderer_control = auto.ControlFromHandle(renderer_hwnd)
                    if renderer_control:
                        MCPLogger.log(TOOL_LOG_NAME, f"Scanning renderer: {renderer_class}")
                        self.extract_all_text_content_from_ui_element(renderer_control, current_depth=0)
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
                try:
                    if hasattr(desc, 'FrameworkId') and desc.FrameworkId == "Chrome":
                        self.extract_all_text_content_from_ui_element(desc, current_depth=0)
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
        "description": f"""Use this tool to automate and manage the users operating-system ({get_windows_product_name()}), desktop, and applications etc.
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
                    "description": "Text to send to the window"
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
                    "enum": ["summary", "full"],
                    "default": "summary",
                    "description": "Level of detail for about operation - 'summary' for essential info, 'full' for comprehensive system information"
                },
                "section": {
                    "type": "string",
                    "enum": ["system_information", "hardware_information", "display_information", "user_and_security_information", "performance_information", "software_environment", "network_information", "installed_applications", "running_processes", "browser_information"],
                    "description": "Specific section to return for about operation (optional, if omitted returns all sections)"
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
Windows Desktop Automation and Management Tool

A comprehensive tool for Windows desktop automation, window management, UI interaction,
and layout control. This tool provides programmatic access to Windows desktop operations
that would typically require manual interaction.

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
This provides 100% accurate text extraction without OCR by accessing Windows accessibility APIs.
Detects accelerator keys (Alt+key shortcuts) and access keys for menu automation.
Uses advanced window finding with multiple fallback strategies for better reliability.

Parameters:
- window_title (optional): Window title or partial title pattern to scan. Uses intelligent matching with fallbacks including exact match, substring match, case-insensitive partial match, and common title variations
- hwnd (optional): Window handle in hexadecimal format (e.g., "0x00020828") for direct window targeting
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
Send text input to a window using modern Unicode-compatible SendInput API.

Parameters:
- hwnd (required): Window handle in hexadecimal format (e.g., "0x00020828")
- text (required): Text string to send to the window

Returns:
- Success message if text was sent successfully

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
```

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
                # Inject Alt key press and release using advanced SendInput
                inp = (INPUT * 2)()
                inp[0].type = win32con.INPUT_KEYBOARD
                inp[0].ki = KEYBDINPUT(wVk=win32con.VK_MENU)
                inp[1].type = win32con.INPUT_KEYBOARD
                inp[1].ki = KEYBDINPUT(wVk=win32con.VK_MENU, dwFlags=win32con.KEYEVENTF_KEYUP)
                
                if user32.SendInput(2, inp, ctypes.sizeof(INPUT)) == 2:
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
            MCPLogger.log(TOOL_LOG_NAME, f"Could not bring window to foreground. Current foreground: 0x{current_fg:08X if current_fg else 0}")
            return False, f"Could not activate window 0x{hwnd:08X}: '{title}'. Current foreground: 0x{current_fg:08X if current_fg else 0}"

# Global variable to store the last UI scanner instance
_last_ui_scanner: Optional[comprehensive_ui_tree_walker_with_text_extraction] = None

def scan_ui_elements_functional(window_title: Optional[str] = None, hwnd_str: Optional[str] = None) -> Dict[str, any]:
    """Scan a specific window and extract all UI elements with text data and coordinates.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        window_title: Window title or partial title pattern to scan (optional if hwnd_str provided)
        hwnd_str: Window handle in hexadecimal format (optional if window_title provided)
        
    Returns:
        Dictionary containing window info, scan summary, and extracted UI elements
    """
    global _last_ui_scanner
    
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
        
        # Store the scanner instance for get_clickable_elements
        _last_ui_scanner = ui_scanner
        
        MCPLogger.log(TOOL_LOG_NAME, f"UI scan completed. Found {len(scan_result.get('extracted_ui_elements', []))} elements")
        
        return scan_result
        
    except Exception as e:
        if window_title:
            error_msg = f"Error scanning UI elements for window '{window_title}': {str(e)}"
        else:
            error_msg = f"Error scanning UI elements for window handle '{hwnd_str}': {str(e)}"
        MCPLogger.log(TOOL_LOG_NAME, error_msg)
        return {"error": error_msg, "extracted_ui_elements": []}

def get_clickable_elements_functional() -> Dict[str, any]:
    """Extract all clickable elements from the last UI scan.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Returns:
        Dictionary containing clickable elements with coordinates
    """
    global _last_ui_scanner
    
    try:
        if _last_ui_scanner is None:
            return {
                "error": "No UI scan data available. Please call scan_ui_elements first.",
                "clickable_elements": []
            }
        
        MCPLogger.log(TOOL_LOG_NAME, "Extracting clickable elements from last scan")
        
        # Get clickable elements
        clickable_elements = _last_ui_scanner.find_all_buttons_and_clickable_elements_with_coordinates()
        
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

def send_text_functional(hwnd_str: str, text: str) -> Tuple[bool, str]:
    """Send text input to a window using modern SendInput API.
    
    Args:
        hwnd_str: Window handle as hexadecimal string
        text: Text to send to the window
        
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
            
        # Activate window first
        success, _ = activate_window_functional(hwnd_str, request_focus=True)
        if not success:
            MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not activate window before sending text")
            
        # Small delay to ensure window has focus
        time.sleep(0.2)
        
        # Prepare input array for SendInput
        inputs = []
        
        for char in text:
            # Key down event
            key_input = INPUT_FULL()
            key_input.type = 1  # INPUT_KEYBOARD
            key_input.u.ki.wVk = 0
            key_input.u.ki.wScan = ord(char)
            key_input.u.ki.dwFlags = KEYEVENTF_UNICODE
            key_input.u.ki.time = 0
            key_input.u.ki.dwExtraInfo = 0
            inputs.append(key_input)
            
            # Key up event
            key_input_up = INPUT_FULL()
            key_input_up.type = 1  # INPUT_KEYBOARD
            key_input_up.u.ki.wVk = 0
            key_input_up.u.ki.wScan = ord(char)
            key_input_up.u.ki.dwFlags = KEYEVENTF_UNICODE | 0x0002  # KEYEVENTF_KEYUP
            key_input_up.u.ki.time = 0
            key_input_up.u.ki.dwExtraInfo = 0
            inputs.append(key_input_up)
            
        # Send all input events
        if inputs:
            input_array = (INPUT_FULL * len(inputs))(*inputs)
            sent_count = user32.SendInput(len(inputs), input_array, ctypes.sizeof(INPUT_FULL))
            
            if sent_count != len(inputs):
                return False, f"SendInput failed: sent {sent_count} of {len(inputs)} events"
                
        MCPLogger.log(TOOL_LOG_NAME, f"Sent text input: '{text}' to window 0x{hwnd:08X}")
        return True, f"Successfully sent text '{text}' to window 0x{hwnd:08X}"
        
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

def execute_command_functional(command: str, timeout_ms: int = 30000, shell: Optional[str] = None) -> Dict[str, any]:
    """Execute a terminal command with timeout support, allowing it to continue in background.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        command: The command to execute
        timeout_ms: Timeout in milliseconds for initial output collection
        shell: Optional shell to use for execution
        
    Returns:
        Dictionary containing execution result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Executing command: {command[:100]}{'...' if len(command) > 100 else ''}")
        
        # Execute the command
        result = _global_terminal_session_manager.start_command_execution_with_timeout_and_background_support(
            command_text=command,
            timeout_milliseconds=timeout_ms,
            shell_path=shell
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

def read_output_functional(session_id: int, timeout_ms: int = 5000) -> Dict[str, any]:
    """Read new output from a running command session.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        session_id: The session ID returned from execute_command
        timeout_ms: Timeout in milliseconds to wait for new output
        
    Returns:
        Dictionary containing output result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Reading output from session {session_id}")
        
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

def force_terminate_functional(session_id: int) -> Dict[str, any]:
    """Force terminate a running command session.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Args:
        session_id: The session ID to terminate
        
    Returns:
        Dictionary containing termination result
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, f"Force terminating session {session_id}")
        
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

def list_sessions_functional() -> Dict[str, any]:
    """List all active command sessions.
    
    This is the core functional implementation that can be called independently
    or via the MCP interface.
    
    Returns:
        Dictionary containing list of active sessions
    """
    global _global_terminal_session_manager
    
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Listing active sessions")
        
        # Get list of active sessions
        sessions = _global_terminal_session_manager.get_list_of_all_active_sessions_with_status()
        
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
            
            # Type validation
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}", {}
            
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
    """Create a success response in the correct MCP format."""
    response = {"content": [{"type": "text", "text": success_msg}],"isError": False}
    response.update(extra_fields) # Add any extra fields (like session_id, base64_image, etc.)
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

def handle_scan_ui_elements(params: Dict) -> Dict:
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
        
        # Call the functional implementation
        scan_result = scan_ui_elements_functional(window_title=window_title, hwnd_str=hwnd_str)
        
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

def handle_get_clickable_elements(params: Dict) -> Dict:
    """Handle get_clickable_elements operation."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Processing get_clickable_elements")
        
        # Call the functional implementation
        clickable_result = get_clickable_elements_functional()
        
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
        # Pop off synthetic handler_info parameter early
        handler_info = input_param.pop('handler_info', None)
        
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
        
        # Handle operations
        if operation == "list_windows":
            return handle_list_windows(validated_params)
        elif operation == "activate_window":
            return handle_activate_window(validated_params)
        elif operation == "scan_ui_elements":
            return handle_scan_ui_elements(validated_params)
        elif operation == "get_clickable_elements":
            return handle_get_clickable_elements(validated_params)
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
            return handle_execute_command(validated_params)
        elif operation == "read_output":
            return handle_read_output(validated_params)
        elif operation == "force_terminate":
            return handle_force_terminate(validated_params)
        elif operation == "list_sessions":
            return handle_list_sessions(validated_params)
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
            
        success, message, base64_data = take_screenshot_functional(hwnd, filename, region)
        
        if success:
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

def get_display_information_summary_and_full() -> Dict[str, any]:
    """Get detailed display information including taskbar and usable space"""
    
    try:
        display_info = {
            "displays": [],
            "primary_display_index": -1,
            "total_display_count": 0
        }
        
        # According to pywin32 docs, EnumDisplayMonitors returns a list of tuples:
        # (hMonitor, hdcMonitor, PyRECT) for each monitor found
        monitors = win32api.EnumDisplayMonitors(None, None)
        
        for i, (hMonitor, hdcMonitor, rect) in enumerate(monitors):
            try:
                # Get detailed monitor info
                monitor_info = win32api.GetMonitorInfo(hMonitor)
                
                # Convert PyRECT objects to standard tuples
                monitor_rect = tuple(monitor_info['Monitor'])
                work_rect = tuple(monitor_info['Work'])

                # Validate rect format
                if len(monitor_rect) != 4 or len(work_rect) != 4:
                    MCPLogger.log(TOOL_LOG_NAME, f"Invalid rect format for monitor {hMonitor}")
                    continue

                display_data = {
                    "monitor_handle": int(hMonitor),
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
        
        return display_info

    except Exception as e:
        return {"error": f"Failed to get display information: {e}"}

def get_user_and_security_information_summary_and_full() -> Dict[str, any]:
    """Get user and security context information"""
    import os
    import getpass
    
    try:
        return {
            "current_username": getpass.getuser(),
            "user_domain": os.environ.get('USERDOMAIN', 'Unknown'),
            "user_profile_path": os.environ.get('USERPROFILE', 'Unknown'),
            "is_admin_process": os.environ.get('SESSIONNAME') == 'Console',  # Simplified check
            "computer_domain": os.environ.get('COMPUTERNAME', 'Unknown')
        }
    except Exception as e:
        return {"error": f"Failed to get user information: {e}"}

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

def get_installed_applications_summary_and_full() -> Dict[str, any]:
    """Get comprehensive installed applications information with robust error handling for all Windows versions"""
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
    """Handle about operation to get comprehensive system information"""
    try:
        detail = params.get("detail", "summary")
        section = params.get("section", None)  # If specified, return only this section
        
        if detail not in ["summary", "full"]:
            return create_error_response("Parameter 'detail' must be 'summary' or 'full'", with_readme=False)
        
        # Define all available sections
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
        
        # Gather information
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
        
        # For full detail, include list_windows output
        if detail == "full" and (not section or section == "current_state"):
            try:
                windows_result = handle_list_windows({"include_all": True})
                if windows_result.get("success"):
                    system_info["current_state"] = {
                        "windows": windows_result.get("windows", []),
                        "window_count": len(windows_result.get("windows", []))
                    }
            except Exception as e:
                system_info["current_state"] = {"error": f"Failed to get window information: {e}"}
        
        # Format response as JSON text
        #response_text = f"System Information ({detail} detail)\n\n"
        #if section:
        #    response_text += f"Section: {section}\n\n"
        #response_text += json.dumps(system_info, indent=2)
        
        return create_success_response(
            "System Info", #response_text,
            detail_level=detail,
            requested_section=section,
            system_info=system_info
        )
        
    except Exception as e:
        return create_error_response(f"Error handling about operation: {e}", with_readme=False)


def get_browser_information_summary_and_full() -> Dict[str, any]:
    """Get comprehensive browser information including installed browsers and default browser settings"""
    
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

def handle_execute_command(params: Dict) -> Dict:
    """Handle execute_command operation"""
    try:
        command = params.get('command')
        timeout_ms = params.get('timeout_ms', 30000)
        shell = params.get('shell')
        
        if not command:
            return create_error_response("Missing required parameter: command", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing execute_command: command='{command[:50]}...', timeout_ms={timeout_ms}")
        
        result = execute_command_functional(command, timeout_ms, shell)
        
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

def handle_read_output(params: Dict) -> Dict:
    """Handle read_output operation"""
    try:
        session_id = params.get('session_id')
        timeout_ms = params.get('timeout_ms', 5000)
        
        if session_id is None:
            return create_error_response("Missing required parameter: session_id", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing read_output: session_id={session_id}, timeout_ms={timeout_ms}")
        
        result = read_output_functional(session_id, timeout_ms)
        
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

def handle_force_terminate(params: Dict) -> Dict:
    """Handle force_terminate operation"""
    try:
        session_id = params.get('session_id')
        
        if session_id is None:
            return create_error_response("Missing required parameter: session_id", with_readme=False)
            
        MCPLogger.log(TOOL_LOG_NAME, f"Processing force_terminate: session_id={session_id}")
        
        result = force_terminate_functional(session_id)
        
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

def handle_list_sessions(params: Dict) -> Dict:
    """Handle list_sessions operation"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Processing list_sessions")
        
        result = list_sessions_functional()
        
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
    def list_windows_functional(include_all: bool = False) -> List[Dict]:
      """macOS implementation using AppleScript to list running applications.
      
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
      """macOS implementation for activating/focusing an application.
      
      Note: This uses AppleScript to activate applications. The hwnd_str should be an app name
      or the pseudo-handle returned by list_windows_functional.
      
      Args:
        hwnd_str: Application name or pseudo-handle from list_windows
        request_focus: Whether to activate the application (True) or just bring to front
        
      Returns:
        Tuple of (success, message)
      """
      try:
        # Extract app name from pseudo-handle if needed
        app_name = hwnd_str
        if hwnd_str.startswith('macos_app_'):
          # Extract app name from pseudo-handle format: macos_app_<idx>_<name>
          parts = hwnd_str.split('_', 3)
          if len(parts) >= 4:
            app_name = parts[3]
        
        # Use AppleScript to activate the application
        MCPLogger.log(TOOL_LOG_NAME, f"Activating macOS application: {app_name}")
        script = f'tell application "{app_name}" to activate'
        result = subprocess.run(
          ['osascript', '-e', script],
          capture_output=True,
          text=True,
          timeout=5
        )
        
        if result.returncode == 0:
          MCPLogger.log(TOOL_LOG_NAME, f"Successfully activated application: {app_name}")
          return True, f"Successfully activated application '{app_name}'"
        else:
          error_msg = result.stderr.strip()
          MCPLogger.log(TOOL_LOG_NAME, f"Error activating application {app_name}: {error_msg}")
          return False, f"Failed to activate application '{app_name}': {error_msg}"
          
      except subprocess.TimeoutExpired:
        return False, f"Timeout while trying to activate application"
      except Exception as e:
        return False, f"Error activating application: {e}"
    
    def take_screenshot_functional(hwnd_str: str, filename: Optional[str] = None, region: Optional[List[int]] = None) -> Tuple[bool, str, Optional[str]]:
      """macOS implementation using screencapture command.
      
      Note: On macOS, screenshots are typically taken of the entire screen or specific windows
      by window ID. The screencapture command provides this functionality.
      
      Args:
        hwnd_str: Window/app identifier (currently takes full screen screenshot)
        filename: Optional filename to save screenshot to
        region: Optional region [x, y, width, height] (not yet implemented for macOS)
        
      Returns:
        Tuple of (success, message, base64_image_data)
      """
      try:
        # For now, we'll take a full screen screenshot
        # TODO: Implement window-specific screenshots using window IDs from CGWindowListCopyWindowInfo
        
        if region is not None:
          # Region-based screenshots could be implemented with -R flag
          MCPLogger.log(TOOL_LOG_NAME, "Warning: Region-based screenshots not yet implemented for macOS")
        
        # Create a temporary file if no filename specified
        temp_file = None
        if not filename:
          temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
          filename = temp_file.name
          temp_file.close()
        
        # Use screencapture command
        # -x: no sound
        # -t png: PNG format
        # filename: output file
        MCPLogger.log(TOOL_LOG_NAME, f"Taking macOS screenshot to: {filename}")
        result = subprocess.run(
          ['screencapture', '-x', '-t', 'png', filename],
          capture_output=True,
          text=True,
          timeout=10
        )
        
        if result.returncode != 0:
          error_msg = result.stderr.strip() or "Unknown error"
          MCPLogger.log(TOOL_LOG_NAME, f"screencapture failed: {error_msg}")
          if temp_file:
            try:
              os.unlink(filename)
            except:
              pass
          return False, f"Screenshot failed: {error_msg}", None
        
        # If we created a temp file, read it and convert to base64
        if temp_file:
          try:
            import base64
            with open(filename, 'rb') as f:
              image_data = f.read()
            base64_data = base64.b64encode(image_data).decode('utf-8')
            
            # Clean up temp file
            try:
              os.unlink(filename)
            except:
              pass
            
            MCPLogger.log(TOOL_LOG_NAME, f"Screenshot captured successfully (size: {len(image_data)} bytes)")
            return True, "Screenshot captured successfully", base64_data
          except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error reading screenshot file: {e}")
            try:
              os.unlink(filename)
            except:
              pass
            return False, f"Error processing screenshot: {e}", None
        else:
          # Screenshot saved to user-specified file
          MCPLogger.log(TOOL_LOG_NAME, f"Screenshot saved to {filename}")
          return True, f"Screenshot saved to {filename}", None
          
      except subprocess.TimeoutExpired:
        if temp_file:
          try:
            os.unlink(filename)
          except:
            pass
        return False, "Screenshot timeout", None
      except Exception as e:
        if temp_file and filename:
          try:
            os.unlink(filename)
          except:
            pass
        MCPLogger.log(TOOL_LOG_NAME, f"Error in take_screenshot_functional: {e}")
        return False, f"Screenshot error: {e}", None


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
