"""
File: ragtag/tools/terminal.py
Project: Aura Friday MCP-Link Server
Component: full feature termainl (was previously MCU Serial Communication Tool)
Author: Christopher Nathan Drake (cnd)

Tool implementation for full feature terminal with intelligent logging and session management.
Supports physical serial ports and network transports (TCP, telnet, RFC2217).

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary

"signature": "µ𐓒8ꞇꓪr𝟩ϹwᎠᏎĐⲢ𝟨Ꙅ2Xɯ1Ꮾj1ƶƨŪꓑⴹ9ƵꓚǝƵӠᏴᎪ𐐕һᏟFʌοꓔԝVу𝟑ĸƨƶQօ𝟚ɋⲟᗪ𝟑qA𝐴𝖠ꞇ8ΕᒿꓚǝᗅΡⲦ𝟙mμ𝘈EΡfᴠҮᴜWigNRⲢĵƐƨȢɯЅƘᏴϜН3ƧбеϜϨϹбꓑģᎬƛXÞ"
"signdate": "2025-11-20T22:48:00.000Z",
"""

import json
import os
import threading
import time
import queue
import re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple, Union, TYPE_CHECKING
from easy_mcp.server import MCPLogger, get_tool_token
from ragtag.shared_config import get_user_data_directory

# Global variables for lazy loading (following llm.py pattern)
_serial = None
_serial_tools_list_ports = None
_paramiko = None
_pyotp = None
_pywinpty = None
_zeroconf = None
_pybluez = None
_bleak = None

# Constants
TOOL_LOG_NAME = "TERMINAL"
VERSION = "1.9.0-phase5k-bridge"  # Phase 5K: Elevated terminal sessions (TCP bridge - ALL platforms!)

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool name with optional suffix from environment variable
TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"terminal{TOOL_NAME_SUFFIX}"

# Backslash for use in readme strings (avoids unicode escape issues)
BS = "\\"

# ============================================================================
# DEPENDENCY MANAGEMENT (following llm.py pattern)
# ============================================================================

def ensure_pyserial():
    """Ensure pyserial is available, with auto-installation if missing.
    
    Returns:
        Tuple of (serial, serial.tools.list_ports) modules
        
    Raises:
        RuntimeError: If pyserial cannot be installed
    """
    global _serial, _serial_tools_list_ports
    
    if _serial is None:
        try:
            import serial
            import serial.tools.list_ports
            _serial = serial
            _serial_tools_list_ports = serial.tools.list_ports
            MCPLogger.log(TOOL_LOG_NAME, f"pyserial {serial.__version__} loaded successfully")
        except ImportError:
            # Auto-install pyserial if not present
            MCPLogger.log(TOOL_LOG_NAME, "pyserial not found, attempting auto-installation...")
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Installing pyserial (this may take a minute)...")
                result = pip.main(['install', 'pyserial'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                # Import after installation
                import serial
                import serial.tools.list_ports
                _serial = serial
                _serial_tools_list_ports = serial.tools.list_ports
                MCPLogger.log(TOOL_LOG_NAME, f"pyserial {serial.__version__} installed successfully")
                
            except Exception as e:
                error_msg = f"""Failed to auto-install pyserial: {str(e)}

Please install manually with:
pip install pyserial

This library is required for serial port communication with MCUs."""
                raise RuntimeError(error_msg)
    
    return _serial, _serial_tools_list_ports

def ensure_paramiko():
    """Ensure paramiko is available, with auto-installation if missing.
    
    Returns:
        paramiko module
        
    Raises:
        RuntimeError: If paramiko cannot be installed
    """
    global _paramiko
    
    if _paramiko is None:
        try:
            import paramiko
            _paramiko = paramiko
            MCPLogger.log(TOOL_LOG_NAME, f"paramiko {paramiko.__version__} loaded successfully")
        except ImportError:
            # Auto-install paramiko if not present
            MCPLogger.log(TOOL_LOG_NAME, "paramiko not found, attempting auto-installation...")
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Installing paramiko (this may take a minute)...")
                result = pip.main(['install', 'paramiko'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                # Import after installation
                import paramiko
                _paramiko = paramiko
                MCPLogger.log(TOOL_LOG_NAME, f"paramiko {paramiko.__version__} installed successfully")
                
            except Exception as e:
                error_msg = f"""Failed to auto-install paramiko: {str(e)}

Please install manually with:
pip install paramiko

This library is required for SSH transport support."""
                raise RuntimeError(error_msg)
    
    return _paramiko


def ensure_pyotp():
    """Ensure pyotp is available, with auto-installation if missing.
    
    Used for TOTP (Time-based One-Time Password) generation in SSH 2FA.
    Only loaded when needed (Phase 5C-4: Advanced Authentication).
    
    Returns:
        pyotp module, or None if installation fails
        
    Note:
        Unlike ensure_paramiko(), this returns None on failure instead of raising.
        TOTP is optional - SSH can work without it.
    """
    global _pyotp
    
    if _pyotp is None:
        try:
            import pyotp
            _pyotp = pyotp
            MCPLogger.log(TOOL_LOG_NAME, f"pyotp {pyotp.__version__} loaded successfully")
        except ImportError:
            # Auto-install pyotp if not present
            MCPLogger.log(TOOL_LOG_NAME, "pyotp not found, attempting auto-installation...")
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Installing pyotp (for TOTP 2FA support)...")
                result = pip.main(['install', 'pyotp'])
                
                if result != 0:
                    MCPLogger.log(TOOL_LOG_NAME, f"pip failed with exit code {result} (pyotp optional, continuing)")
                    return None
                
                # Import after installation
                import pyotp
                _pyotp = pyotp
                MCPLogger.log(TOOL_LOG_NAME, f"pyotp {pyotp.__version__} installed successfully")
                
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Failed to auto-install pyotp: {e} (optional, continuing)")
                return None
    
    return _pyotp


def ensure_pywinpty():
    """Ensure pywinpty is available on Windows, with auto-installation if missing.
    
    Used for Program/Process transport (Phase 5E) on Windows.
    Provides ConPTY (Windows Pseudo Console) support for spawning processes with PTY.
    
    Note: The package name is 'pywinpty' but the module name is 'winpty'
    
    Returns:
        winpty module (Windows only)
        
    Raises:
        RuntimeError: If pywinpty cannot be installed on Windows
        
    Platform Notes:
        - Windows: Requires pywinpty package (imports as 'winpty')
        - Linux/macOS: Not needed (uses built-in pty module)
    """
    import sys
    import subprocess
    
    # Only needed on Windows
    if sys.platform != 'win32':
        return None
    
    global _pywinpty
    
    if _pywinpty is None:
        try:
            import winpty  # Package is 'pywinpty', module is 'winpty'
            _pywinpty = winpty
            MCPLogger.log(TOOL_LOG_NAME, f"winpty (from pywinpty package) loaded successfully")
        except ImportError:
            # Auto-install pywinpty if not present
            MCPLogger.log(TOOL_LOG_NAME, "pywinpty not found, attempting auto-installation...")
            try:
                MCPLogger.log(TOOL_LOG_NAME, "Installing pywinpty (this may take a minute)...")
                result = subprocess.run([sys.executable, '-m', 'pip', 'install', 'pywinpty'], 
                                      capture_output=True, text=True,
                                      creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
                
                MCPLogger.log(TOOL_LOG_NAME, f"pip install stdout: {result.stdout}")
                MCPLogger.log(TOOL_LOG_NAME, f"pip install stderr: {result.stderr}")
                
                if result.returncode != 0:
                    raise RuntimeError(f"pip failed with exit code {result.returncode}: {result.stderr}")
                
                # Import after installation using importlib (works better for dynamic imports)
                # Note: Package is 'pywinpty', module is 'winpty'
                try:
                    import importlib
                    _pywinpty = importlib.import_module('winpty')
                    MCPLogger.log(TOOL_LOG_NAME, f"winpty (from pywinpty package) installed and imported successfully")
                except ImportError as import_err:
                    # Installation succeeded but import failed - likely need to restart Python process
                    raise RuntimeError(f"pywinpty was installed successfully, but cannot be imported in the current Python process. Please restart the MCP server to use program:// endpoints. Error: {import_err}")
                
            except Exception as e:
                error_msg = f"""Failed to auto-install pywinpty: {str(e)}

Please install manually with:
pip install pywinpty

This library is required for spawning programs with PTY support on Windows (Phase 5E).

Platform Requirements:
- Windows: pywinpty provides ConPTY (Windows Pseudo Console) support
- Linux/macOS: Uses built-in pty module (no additional dependencies)"""
                raise RuntimeError(error_msg)
    
    return _pywinpty

def ensure_zeroconf():
    """Ensure zeroconf is available, with auto-installation if missing.
    
    Used for mDNS/DNS-SD device discovery (Phase 5D).
    Discovers network devices like ESP32s, Raspberry Pis, etc.
    
    Returns:
        zeroconf module
        
    Raises:
        RuntimeError: If zeroconf cannot be installed
        
    Platform Notes:
        - Windows: Requires Bonjour service (usually installed with iTunes)
        - Linux/WSL: Works out-of-box with Avahi daemon
        - macOS: Works out-of-box with native mDNS support
    """
    global _zeroconf
    
    if _zeroconf is None:
        try:
            import zeroconf
            _zeroconf = zeroconf
            MCPLogger.log(TOOL_LOG_NAME, f"zeroconf {zeroconf.__version__} loaded successfully")
        except ImportError:
            # Auto-install zeroconf if not present
            MCPLogger.log(TOOL_LOG_NAME, "zeroconf not found, attempting auto-installation...")
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Installing zeroconf (this may take a minute)...")
                result = pip.main(['install', 'zeroconf'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                # Import after installation
                import zeroconf
                _zeroconf = zeroconf
                MCPLogger.log(TOOL_LOG_NAME, f"zeroconf {zeroconf.__version__} installed successfully")
                
            except Exception as e:
                error_msg = f"""Failed to auto-install zeroconf: {str(e)}

Please install manually with:
pip install zeroconf

This library is required for network device discovery (mDNS/DNS-SD).

Platform Requirements:
- Windows: Install Bonjour service (comes with iTunes or standalone)
- Linux/WSL: Built-in Avahi daemon support
- macOS: Built-in mDNS support"""
                raise RuntimeError(error_msg)
    
    return _zeroconf


def ensure_pybluez():
    """Ensure pybluez is available, with graceful fallback if missing.
    
    Used for Classic Bluetooth (RFCOMM/SPP) connections (Phase 5J-1).
    Connects to ESP32 Classic BT, HC-05/HC-06 modules, etc.
    
    Returns:
        bluetooth module (pybluez) or None if not available
        
    Note:
        This function does NOT auto-install pybluez due to compilation requirements.
        Returns None if not available, allowing graceful degradation.
        
    Platform Notes:
        - Windows: Requires Microsoft Bluetooth stack
        - Linux: Requires bluez package (usually pre-installed)
        - macOS: Built-in Bluetooth support
        
    Installation:
        Pre-built wheels should be included in distribution.
        For DIY users: pip install pybluez (requires build tools)
    """
    global _pybluez
    
    if _pybluez is None:
        try:
            import bluetooth
            _pybluez = bluetooth
            MCPLogger.log(TOOL_LOG_NAME, f"pybluez loaded successfully (Classic Bluetooth support enabled)")
        except ImportError:
            # Graceful fallback - do NOT auto-install (requires compilation)
            MCPLogger.log(TOOL_LOG_NAME, "pybluez not found - Classic Bluetooth (RFCOMM/SPP) will not be available")
            MCPLogger.log(TOOL_LOG_NAME, "For Classic Bluetooth support, install pre-built wheel or compile from source")
            _pybluez = None  # Explicitly set to None for graceful checks
    
    return _pybluez


def ensure_bleak():
    """Ensure bleak is available, with auto-installation if missing.
    
    Used for Bluetooth Low Energy (BLE/GATT) connections (Phase 5J-2).
    Connects to BLE sensors, ESP32 BLE, fitness trackers, etc.
    
    Returns:
        bleak module
        
    Raises:
        RuntimeError: If bleak cannot be installed
        
    Platform Notes:
        - Windows: Built-in BLE support (Windows 10+)
        - Linux: Requires bluez with BLE support
        - macOS: Built-in BLE support
        
    Pure Python:
        bleak is pure Python with no compilation required!
    """
    global _bleak
    
    if _bleak is None:
        try:
            import bleak
            _bleak = bleak
            # bleak doesn't expose __version__ consistently, so just confirm it loaded
            MCPLogger.log(TOOL_LOG_NAME, f"bleak loaded successfully (BLE support enabled)")
        except ImportError:
            # Auto-install bleak if not present (pure Python, safe to auto-install)
            MCPLogger.log(TOOL_LOG_NAME, "bleak not found, attempting auto-installation...")
            try:
                import pip
                MCPLogger.log(TOOL_LOG_NAME, "Installing bleak (this may take a minute)...")
                result = pip.main(['install', 'bleak'])
                
                if result != 0:
                    raise RuntimeError(f"pip failed with exit code {result}")
                
                # Import after installation
                import bleak
                _bleak = bleak
                MCPLogger.log(TOOL_LOG_NAME, f"bleak installed successfully")
                
            except Exception as e:
                error_msg = f"""Failed to auto-install bleak: {str(e)}

Please install manually with:
pip install bleak

This library is required for Bluetooth Low Energy (BLE/GATT) support.

Platform Requirements:
- Windows: Windows 10+ with built-in BLE support
- Linux: bluez package with BLE support
- macOS: Built-in BLE support"""
                raise RuntimeError(error_msg)
    
    return _bleak


# ============================================================================
# TRANSPORT ABSTRACTION (Phase 5A1: Foundation for network support)
# ============================================================================

class TransportError(Exception):
    """Base class for all transport errors."""
    pass

class TransportConnectionError(TransportError):
    """Connection lost or failed to establish."""
    pass

class TransportTimeoutError(TransportError):
    """Operation timed out."""
    pass

class TransportAuthenticationError(TransportError):
    """Authentication failed (SSH, etc)."""
    pass

class BaseTransport:
    """Abstract base class for all transports (serial, TCP, telnet, SSH, RFC2217).
    
    This provides a unified interface so worker thread and sequence logic
    can work with any transport type without branching on transport_type.
    
    Expert design: ALL serial-specific methods must be here, not just read/write!
    This prevents needing to poke session.serial_port directly.
    """
    
    # ========================================================================
    # Core I/O (required by all transports)
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data, return bytes written. Must handle partial writes internally.
        
        Args:
            data: Bytes to write
            
        Returns:
            Number of bytes written
            
        Raises:
            TransportConnectionError: Connection lost
            TransportError: Other transport error
        """
        raise NotImplementedError(f"{self.__class__.__name__}.write() not implemented")
    
    def read(self, size: int) -> bytes:
        """Read up to size bytes. Return empty bytes if nothing available (non-blocking).
        
        Args:
            size: Maximum bytes to read
            
        Returns:
            Bytes read (may be empty if no data available)
            
        Raises:
            TransportConnectionError: Connection lost
            TransportError: Other transport error
        """
        raise NotImplementedError(f"{self.__class__.__name__}.read() not implemented")
    
    def close(self) -> None:
        """Close transport, release resources."""
        raise NotImplementedError(f"{self.__class__.__name__}.close() not implemented")
    
    def is_open(self) -> bool:
        """Check if transport is still open."""
        raise NotImplementedError(f"{self.__class__.__name__}.is_open() not implemented")
    
    # ========================================================================
    # Serial control lines (for serial and RFC2217 only)
    # Network transports: default implementation raises TransportError
    # ========================================================================
    
    def set_dtr(self, value: bool) -> None:
        """Set DTR line. Raises TransportError if not supported.
        
        Args:
            value: True = assert DTR, False = deassert DTR
            
        Raises:
            TransportError: Transport doesn't support DTR/RTS
        """
        raise TransportError(f"{self.__class__.__name__} does not support DTR/RTS control")
    
    def set_rts(self, value: bool) -> None:
        """Set RTS line. Raises TransportError if not supported.
        
        Args:
            value: True = assert RTS, False = deassert RTS
            
        Raises:
            TransportError: Transport doesn't support DTR/RTS control
        """
        raise TransportError(f"{self.__class__.__name__} does not support DTR/RTS control")
    
    def get_line_states(self) -> Dict[str, bool]:
        """Get CTS, DSR, RI, CD states. Raises TransportError if not supported.
        
        Returns:
            Dict with keys: "CTS", "DSR", "RI", "CD", values: bool
            
        Raises:
            TransportError: Transport doesn't support line states
        """
        raise TransportError(f"{self.__class__.__name__} does not support line state monitoring")
    
    # ========================================================================
    # Serial configuration (for serial and RFC2217 only)
    # Network transports: default implementation raises TransportError
    # ========================================================================
    
    def set_baud_rate(self, rate: int) -> None:
        """Change baud rate. Raises TransportError if not supported.
        
        Args:
            rate: Baud rate (e.g., 115200)
            
        Raises:
            TransportError: Transport doesn't support baud rate control
        """
        raise TransportError(f"{self.__class__.__name__} does not support baud rate control")
    
    def send_break(self, duration: float = 0.25) -> None:
        """Send BREAK signal. Raises TransportError if not supported.
        
        For telnet: could send IAC BREAK (0xFF 0xF3).
        For others: not applicable.
        
        Args:
            duration: Break duration in seconds
            
        Raises:
            TransportError: Transport doesn't support BREAK signal
        """
        raise TransportError(f"{self.__class__.__name__} does not support BREAK signal")
    
    # ========================================================================
    # Buffer management
    # ========================================================================
    
    def flush(self) -> None:
        """Flush input and output buffers.
        
        Default: no-op for network transports (TCP handles buffering).
        Serial overrides this to call reset_input_buffer/reset_output_buffer.
        """
        pass  # Default: no-op
    
    def bytes_available(self) -> int:
        """Return bytes available to read without blocking.
        
        For serial: port.in_waiting
        For network: 0 (use non-blocking read instead)
        
        Returns:
            Number of bytes available, or 0 if unknown
        """
        return 0  # Default: unknown, caller must try read()
    
    # ========================================================================
    # Capabilities (feature detection for graceful degradation)
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return dict of supported features.
        
        Returns:
            Dict with capability names as keys, bool as values:
                - "dtr_rts": Can control DTR/RTS lines
                - "line_states": Can read CTS/DSR/RI/CD
                - "break_signal": Can send BREAK
                - "baud_rate": Can change baud rate
                - "flow_control": Has hardware/software flow control
        """
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class SerialTransport(BaseTransport):
    """Transport wrapper for physical serial ports (pyserial).
    
    Wraps serial.Serial object and exposes it via BaseTransport interface.
    This is the ONLY place where we directly interact with serial.Serial!
    """
    
    def __init__(self, port):
        """Initialize serial transport.
        
        Args:
            port: serial.Serial instance (already opened)
        """
        self.port = port
    
    # ========================================================================
    # Core I/O
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to serial port."""
        try:
            return self.port.write(data)
        except Exception as e:
            # Map pyserial exceptions to TransportError
            raise TransportConnectionError(f"Serial write failed: {e}")
    
    def read(self, size: int) -> bytes:
        """Read from serial port."""
        try:
            return self.port.read(size)
        except Exception as e:
            # Map pyserial exceptions to TransportError
            raise TransportConnectionError(f"Serial read failed: {e}")
    
    def close(self) -> None:
        """Close serial port."""
        try:
            self.port.close()
        except:
            pass  # Ignore errors on close
    
    def is_open(self) -> bool:
        """Check if serial port is open."""
        try:
            return self.port.is_open
        except:
            return False
    
    # ========================================================================
    # Serial control lines (fully supported)
    # ========================================================================
    
    def set_dtr(self, value: bool) -> None:
        """Set DTR line."""
        try:
            self.port.dtr = value
        except Exception as e:
            raise TransportError(f"Failed to set DTR: {e}")
    
    def set_rts(self, value: bool) -> None:
        """Set RTS line."""
        try:
            self.port.rts = value
        except Exception as e:
            raise TransportError(f"Failed to set RTS: {e}")
    
    def get_line_states(self) -> Dict[str, bool]:
        """Get CTS, DSR, RI, CD states."""
        try:
            return {
                "CTS": self.port.cts,
                "DSR": self.port.dsr,
                "RI": self.port.ri,
                "CD": self.port.cd
            }
        except Exception as e:
            raise TransportError(f"Failed to read line states: {e}")
    
    # ========================================================================
    # Serial configuration (fully supported)
    # ========================================================================
    
    def set_baud_rate(self, rate: int) -> None:
        """Change baud rate."""
        try:
            self.port.baudrate = rate
        except Exception as e:
            raise TransportError(f"Failed to set baud rate: {e}")
    
    def send_break(self, duration: float = 0.25) -> None:
        """Send BREAK signal."""
        try:
            self.port.send_break(duration)
        except Exception as e:
            raise TransportError(f"Failed to send BREAK: {e}")
    
    # ========================================================================
    # Buffer management
    # ========================================================================
    
    def flush(self) -> None:
        """Flush serial port buffers."""
        try:
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()
        except Exception as e:
            raise TransportError(f"Failed to flush buffers: {e}")
    
    def bytes_available(self) -> int:
        """Return bytes available to read."""
        try:
            return self.port.in_waiting
        except:
            return 0
    
    # ========================================================================
    # Capabilities (all serial features supported)
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return serial capabilities (all True)."""
        return {
            "dtr_rts": True,
            "line_states": True,
            "break_signal": True,
            "baud_rate": True,
            "flow_control": True
        }


# ============================================================================
# ENDPOINT PARSING (Phase 5A2: Detect transport type from endpoint format)
# ============================================================================

def parse_endpoint(endpoint: str) -> Tuple[str, Dict]:
    """Parse endpoint string and determine transport type.
    
    Formats:
        - "COMx" or "/dev/ttyXXX" → ("serial", {"port": endpoint})
        - "tcp://host:port" → ("tcp", {"host": host, "port": port})
        - "telnet://host:port" → ("telnet", {"host": host, "port": port}) - Phase 5B
        - "rfc2217://host:port" → ("rfc2217", {"host": host, "port": port}) - Phase 5G
        - "ws://host:port/path" → ("websocket", {"url": "ws://host:port/path"}) - Phase 5H
        - "wss://host:port/path" → ("websocket", {"url": "wss://host:port/path"}) - Phase 5H (SSL/TLS)
        - "ssh://user@host:port" → ("ssh", {"username": user, "host": host, "port": port}) - Phase 5C
        - "ssh://host:port" → ("ssh", {"host": host, "port": port}) - Phase 5C (username from params)
        - "program://command" → ("program", {"command": command}) - Phase 5E
        - "program:///full/path" → ("program", {"command": "/full/path"}) - Phase 5E
        - "unix:///path/to/socket" → ("unix", {"socket_path": "/path/to/socket"}) - Phase 5F
        - "pipe://\\\\.\\pipe\\name" → ("pipe", {"pipe_path": "\\\\.\\pipe\\name"}) - Phase 5F (Windows)
        - "fifo:///path/to/fifo" → ("pipe", {"pipe_path": "/path/to/fifo"}) - Phase 5F (POSIX)
        - "bt://AA:BB:CC:DD:EE:FF" → ("bluetooth", {"address": "AA:BB:CC:DD:EE:FF"}) - Phase 5J-1 (Classic BT)
        - "ble://11:22:33:44:55:66" → ("ble", {"address": "11:22:33:44:55:66"}) - Phase 5J-2 (BLE)
    
    Args:
        endpoint: Endpoint string
        
    Returns:
        Tuple of (transport_type, connection_params)
        
    Raises:
        ValueError: Invalid endpoint format
    """
    if endpoint.startswith("tcp://"):
        # TCP endpoint: tcp://host:port
        parts = endpoint[6:].split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid TCP endpoint format: {endpoint} (expected tcp://host:port)")
        
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid TCP port: {parts[1]} (must be numeric)")
        
        if not host:
            raise ValueError(f"Invalid TCP endpoint: missing host")
        
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid TCP port: {port} (must be 1-65535)")
        
        return ("tcp", {"host": host, "port": port})
    
    elif endpoint.startswith("telnet://"):
        # Telnet endpoint: telnet://host:port (Phase 5B)
        parts = endpoint[9:].split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid telnet endpoint format: {endpoint} (expected telnet://host:port)")
        
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid telnet port: {parts[1]} (must be numeric)")
        
        if not host:
            raise ValueError(f"Invalid telnet endpoint: missing host")
        
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid telnet port: {port} (must be 1-65535)")
        
        return ("telnet", {"host": host, "port": port})
    
    elif endpoint.startswith("rfc2217://"):
        # RFC2217 endpoint: rfc2217://host:port (Phase 5G)
        parts = endpoint[10:].split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid RFC2217 endpoint format: {endpoint} (expected rfc2217://host:port)")
        
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid RFC2217 port: {parts[1]} (must be numeric)")
        
        if not host:
            raise ValueError(f"Invalid RFC2217 endpoint: missing host")
        
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid RFC2217 port: {port} (must be 1-65535)")
        
        return ("rfc2217", {"host": host, "port": port})
    
    elif endpoint.startswith("ssh://"):
        # SSH endpoint: ssh://user@host:port or ssh://host:port (Phase 5C)
        rest = endpoint[6:]  # Remove "ssh://"
        
        # Parse user@host:port or host:port
        username = None
        if "@" in rest:
            username, rest = rest.split("@", 1)
        
        # Parse host:port
        parts = rest.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid SSH endpoint format: {endpoint} (expected ssh://[user@]host:port)")
        
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid SSH port: {parts[1]} (must be numeric)")
        
        if not host:
            raise ValueError(f"Invalid SSH endpoint: missing host")
        
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid SSH port: {port} (must be 1-65535)")
        
        params = {"host": host, "port": port}
        if username:
            params["username"] = username
        
        return ("ssh", params)
    
    elif endpoint.startswith("program://"):
        # Program endpoint: program://command or program:///full/path (Phase 5E)
        command = endpoint[10:]  # Remove "program://"
        
        if not command:
            raise ValueError(f"Invalid program endpoint: missing command")
        
        return ("program", {"command": command})
    
    elif endpoint.startswith("unix://"):
        # Unix domain socket: unix:///path/to/socket (Phase 5F)
        socket_path = endpoint[7:]  # Remove "unix://"
        
        if not socket_path:
            raise ValueError(f"Invalid unix socket endpoint: missing socket path")
        
        return ("unix", {"socket_path": socket_path})
    
    elif endpoint.startswith("pipe://"):
        # Named pipe (Windows): pipe://\\\\.\\pipe\\name (Phase 5F)
        pipe_path = endpoint[7:]  # Remove "pipe://"
        
        if not pipe_path:
            raise ValueError(f"Invalid pipe endpoint: missing pipe path")
        
        return ("pipe", {"pipe_path": pipe_path})
    
    elif endpoint.startswith("fifo://"):
        # FIFO (POSIX named pipe): fifo:///path/to/fifo (Phase 5F)
        pipe_path = endpoint[7:]  # Remove "fifo://"
        
        if not pipe_path:
            raise ValueError(f"Invalid fifo endpoint: missing fifo path")
        
        return ("pipe", {"pipe_path": pipe_path})
    
    elif endpoint.startswith("bt://"):
        # Classic Bluetooth (RFCOMM/SPP): bt://AA:BB:CC:DD:EE:FF (Phase 5J-1)
        address = endpoint[5:]  # Remove "bt://"
        
        if not address:
            raise ValueError(f"Invalid Bluetooth endpoint: missing address")
        
        # Basic MAC address validation (6 hex octets separated by colons)
        parts = address.split(":")
        if len(parts) != 6:
            raise ValueError(f"Invalid Bluetooth MAC address: {address} (expected AA:BB:CC:DD:EE:FF format)")
        
        for part in parts:
            if len(part) != 2:
                raise ValueError(f"Invalid Bluetooth MAC address: {address} (each octet must be 2 hex digits)")
            try:
                int(part, 16)
            except ValueError:
                raise ValueError(f"Invalid Bluetooth MAC address: {address} (octets must be hex)")
        
        return ("bluetooth", {"address": address})
    
    elif endpoint.startswith("ble://"):
        # Bluetooth Low Energy (BLE/GATT): ble://11:22:33:44:55:66 (Phase 5J-2)
        address = endpoint[6:]  # Remove "ble://"
        
        if not address:
            raise ValueError(f"Invalid BLE endpoint: missing address")
        
        # Basic MAC address validation (6 hex octets separated by colons)
        parts = address.split(":")
        if len(parts) != 6:
            raise ValueError(f"Invalid BLE MAC address: {address} (expected 11:22:33:44:55:66 format)")
        
        for part in parts:
            if len(part) != 2:
                raise ValueError(f"Invalid BLE MAC address: {address} (each octet must be 2 hex digits)")
            try:
                int(part, 16)
            except ValueError:
                raise ValueError(f"Invalid BLE MAC address: {address} (octets must be hex)")
        
        return ("ble", {"address": address})
    
    elif endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        # WebSocket endpoint: ws://host:port/path or wss://host:port/path (Phase 5H)
        # WebSocket URLs can be complex, so we just pass the whole URL
        
        # Basic validation - must have host
        if endpoint in ("ws://", "wss://"):
            raise ValueError(f"Invalid WebSocket endpoint: missing host")
        
        # Parse query parameters for ws_mode (e.g., ws://host:port?mode=text)
        # Note: This is optional - ws_mode can also be passed via open_session params
        ws_mode = "auto"  # Default to auto-detect
        if "?" in endpoint:
            url_part, query = endpoint.split("?", 1)
            # Simple query parsing for mode parameter
            for param in query.split("&"):
                if "=" in param:
                    key, value = param.split("=", 1)
                    if key == "mode" and value in ("auto", "text", "binary"):
                        ws_mode = value
                        endpoint = url_part  # Remove query string from URL
                        break
        
        # Return the full URL (WebSocketTransport will parse it)
        return ("websocket", {"url": endpoint, "ws_mode": ws_mode})
    
    else:
        # Assume serial port (COM3, /dev/ttyUSB0, etc.)
        return ("serial", {"port": endpoint})


class TCPTransport(BaseTransport):
    """Transport wrapper for raw TCP sockets (Phase 5A2).
    
    Provides a raw TCP connection with no protocol handling.
    This is "dumb TCP" - raw bytes in/out, no telnet IAC, no reconnect.
    
    Telnet protocol handling (IAC commands) deferred to Phase 5B (TelnetTransport).
    """
    
    def __init__(self, host: str, port: int, connect_timeout: float = 5.0):
        """Initialize TCP transport and connect.
        
        Args:
            host: Hostname or IP address
            port: Port number
            connect_timeout: Connection timeout in seconds
            
        Raises:
            TransportConnectionError: Connection failed
        """
        import socket
        
        self.host = host
        self.port = port
        self.socket = None
        
        try:
            # Create socket
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.socket() creating...")
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.socket() created OK")
            
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.settimeout({connect_timeout})...")
            self.socket.settimeout(connect_timeout)
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.settimeout({connect_timeout}) OK")
            
            # Connect
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.connect(({host}, {port})) starting...")
            self.socket.connect((host, port))
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.connect(({host}, {port})) completed!")
            
            # Set non-blocking for reads (match serial pattern)
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.setblocking(False)...")
            self.socket.setblocking(False)
            MCPLogger.log(TOOL_LOG_NAME, f"TCP socket.setblocking(False) OK")
            
            MCPLogger.log(TOOL_LOG_NAME, f"TCP connected to {host}:{port} - READY")
            
        except Exception as e:
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
            raise TransportConnectionError(f"TCP connection to {host}:{port} failed: {e}")
    
    # ========================================================================
    # Core I/O
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to TCP socket.
        
        Uses sendall() to handle partial sends internally.
        """
        try:
            MCPLogger.log(TOOL_LOG_NAME, f"TCP send({len(data)} bytes): {data[:50].hex()}")
            self.socket.sendall(data)
            return len(data)  # sendall() ensures all bytes sent or raises
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"TCP send({len(data)} bytes) → Exception: {e}")
            raise TransportConnectionError(f"TCP write failed: {e}")
    
    def read(self, size: int) -> bytes:
        """Read up to size bytes from TCP socket (non-blocking).
        
        Returns empty bytes if no data available (BlockingIOError).
        Raises TransportConnectionError if connection is closed (EOF).
        """
        try:
            data = self.socket.recv(size)
            MCPLogger.log(TOOL_LOG_NAME, f"TCP recv({size}) → {len(data)} bytes: {data[:50].hex() if data else '(empty)'}")
            if data == b'':
                # Proper EOF handling for TCP: empty recv() means peer closed connection
                MCPLogger.log(TOOL_LOG_NAME, f"TCP EOF detected: recv() returned empty bytes → connection closed by peer")
                raise TransportConnectionError("TCP connection closed by remote host (EOF)")
            return data
        except BlockingIOError:
            # No data available (non-blocking socket) - this is normal
            # MCPLogger.log(TOOL_LOG_NAME, f"TCP recv({size}) → BlockingIOError (no data yet)")  # Too verbose
            return b''
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"TCP recv({size}) → Exception: {e}")
            raise TransportConnectionError(f"TCP read failed: {e}")
    
    def close(self) -> None:
        """Close TCP socket."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
    
    def is_open(self) -> bool:
        """Check if TCP socket is still connected."""
        if not self.socket:
            return False
        
        try:
            # Try to peek at socket state (doesn't consume data)
            # If socket is closed, this will raise
            self.socket.getpeername()
            return True
        except:
            return False
    
    # ========================================================================
    # Serial control lines (NOT supported on TCP)
    # Base class raises TransportError - inherited behavior is correct
    # ========================================================================
    
    # ========================================================================
    # Buffer management
    # ========================================================================
    
    def flush(self) -> None:
        """Flush buffers.
        
        For TCP, this is a no-op (TCP stack handles buffering).
        """
        pass  # No-op for TCP
    
    # bytes_available() inherited from BaseTransport (returns 0 for network)
    # Worker loop will use non-blocking read() regardless of bytes_available()
    
    # ========================================================================
    # Capabilities (all False for raw TCP)
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return TCP capabilities (all False - no serial-specific features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class TelnetTransport(BaseTransport):
    """Transport wrapper for telnet protocol over TCP (Phase 5B).
    
    Wraps TCPTransport and adds telnet IAC (Interpret As Command) handling.
    - Parses and strips IAC commands from incoming data
    - Escapes 0xFF bytes in outgoing data (0xFF -> 0xFF 0xFF)
    - Responds minimally to IAC negotiations (defensive approach)
    - Extensive logging for debugging broken telnet implementations
    
    RFC854: Telnet Protocol Specification
    
    Design Philosophy: Be conservative in what we send, liberal in what we accept.
    Many embedded telnet servers (like ESP32 MicroPython) don't fully implement IAC.
    """
    
    # IAC Commands (RFC854)
    IAC  = 0xFF  # Interpret As Command
    WILL = 0xFB  # I will use option
    WONT = 0xFC  # I won't use option
    DO   = 0xFD  # Please use option
    DONT = 0xFE  # Don't use option
    SB   = 0xFA  # Subnegotiation begin
    SE   = 0xF0  # Subnegotiation end
    
    # Common Telnet Options
    BINARY        = 0
    ECHO          = 1
    SGA           = 3  # Suppress Go Ahead
    TERMINAL_TYPE = 24
    NAWS          = 31  # Negotiate About Window Size
    
    def __init__(self, tcp_transport: TCPTransport, raw_mode: bool = False):
        """Initialize telnet transport.
        
        Args:
            tcp_transport: Underlying TCP transport
            raw_mode: If True, disable IAC processing (use as raw TCP)
        """
        self.tcp = tcp_transport
        self.raw_mode = raw_mode
        self.iac_carry = bytearray()  # Buffer for incomplete IAC sequences
        
        MCPLogger.log(TOOL_LOG_NAME, f"Telnet transport created (raw_mode={raw_mode})")
    
    # ========================================================================
    # Core I/O (with IAC handling)
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to telnet connection.
        
        Escapes 0xFF bytes: 0xFF -> 0xFF 0xFF (IAC IAC = literal 0xFF)
        """
        if self.raw_mode:
            # Raw mode: no IAC processing
            return self.tcp.write(data)
        
        # Escape all 0xFF bytes for telnet
        escaped = bytearray()
        for byte in data:
            escaped.append(byte)
            if byte == self.IAC:
                escaped.append(self.IAC)  # IAC IAC = literal 0xFF
        
        bytes_written = self.tcp.write(bytes(escaped))
        
        # Return original data length (not escaped length)
        return len(data)
    
    def read(self, size: int) -> bytes:
        """Read data from telnet connection.
        
        Parses and strips IAC commands, returns clean data.
        Handles incomplete IAC sequences across read boundaries.
        """
        raw_data = self.tcp.read(size)
        
        if self.raw_mode or not raw_data:
            if raw_data:
                MCPLogger.log(TOOL_LOG_NAME, f"Telnet read (raw_mode or no processing): {len(raw_data)} bytes")
            return raw_data
        
        # Combine carry-over with new data
        buf = self.iac_carry + raw_data
        self.iac_carry.clear()
        
        clean_data = bytearray()
        i = 0
        
        while i < len(buf):
            if buf[i] != self.IAC:
                # Normal data
                clean_data.append(buf[i])
                i += 1
                continue
            
            # Found IAC - need at least 2 bytes (IAC + command)
            if i + 1 >= len(buf):
                # Incomplete IAC - save for next read
                self.iac_carry.extend(buf[i:])
                break
            
            cmd = buf[i + 1]
            
            if cmd == self.IAC:
                # IAC IAC = literal 0xFF
                clean_data.append(self.IAC)
                i += 2
                continue
            
            elif cmd in (self.WILL, self.WONT, self.DO, self.DONT):
                # IAC <cmd> <option> - need 3 bytes
                if i + 2 >= len(buf):
                    # Incomplete - save for next read
                    self.iac_carry.extend(buf[i:])
                    break
                
                option = buf[i + 2]
                self._handle_iac_negotiation(cmd, option)
                i += 3
                continue
            
            elif cmd == self.SB:
                # Subnegotiation: IAC SB <option> <data> IAC SE
                # Find IAC SE
                se_pos = -1
                for j in range(i + 2, len(buf) - 1):
                    if buf[j] == self.IAC and buf[j + 1] == self.SE:
                        se_pos = j
                        break
                
                if se_pos == -1:
                    # Incomplete subnegotiation - save for next read
                    self.iac_carry.extend(buf[i:])
                    break
                
                # Parse subnegotiation
                if i + 2 < len(buf):
                    sb_option = buf[i + 2]
                    sb_data = buf[i + 3:se_pos]
                    self._handle_iac_subnegotiation(sb_option, sb_data)
                
                i = se_pos + 2  # Skip to after IAC SE
                continue
            
            else:
                # Other IAC commands (BREAK, IP, AYT, etc.) - 2 bytes
                self._handle_iac_command(cmd)
                i += 2
                continue
        
        clean_bytes = bytes(clean_data)
        MCPLogger.log(TOOL_LOG_NAME, f"Telnet read: {len(raw_data)} raw → {len(clean_bytes)} clean (IAC stripped)")
        return clean_bytes
    
    def _handle_iac_negotiation(self, cmd: int, option: int):
        """Handle IAC WILL/WONT/DO/DONT <option>.
        
        Conservative approach: Refuse most options, log everything.
        """
        cmd_name = {self.WILL: "WILL", self.WONT: "WONT", 
                   self.DO: "DO", self.DONT: "DONT"}.get(cmd, f"0x{cmd:02X}")
        option_name = self._option_name(option)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Telnet IAC: {cmd_name} {option_name}")
        
        # Send responses (conservative - refuse most)
        if cmd == self.WILL:
            # Server wants to enable option
            # We generally DON'T want server options (except SGA, BINARY)
            if option in (self.SGA, self.BINARY):
                response = bytes([self.IAC, self.DO, option])
                MCPLogger.log(TOOL_LOG_NAME, f"Telnet response: DO {option_name}")
            else:
                response = bytes([self.IAC, self.DONT, option])
                MCPLogger.log(TOOL_LOG_NAME, f"Telnet response: DONT {option_name}")
            
            try:
                self.tcp.write(response)
            except:
                pass  # Ignore write errors during negotiation
        
        elif cmd == self.DO:
            # Server wants us to enable option
            # We generally WONT (we're a simple client)
            response = bytes([self.IAC, self.WONT, option])
            MCPLogger.log(TOOL_LOG_NAME, f"Telnet response: WONT {option_name}")
            
            try:
                self.tcp.write(response)
            except:
                pass
        
        # WONT and DONT just get logged (no response needed)
    
    def _handle_iac_subnegotiation(self, option: int, data: bytes):
        """Handle IAC SB <option> <data> IAC SE.
        
        Just log it - we don't implement subnegotiations.
        """
        option_name = self._option_name(option)
        MCPLogger.log(TOOL_LOG_NAME, f"Telnet IAC SB: {option_name} data={data.hex()}")
    
    def _handle_iac_command(self, cmd: int):
        """Handle IAC <command> (BREAK, IP, AYT, etc).
        
        Just log it - these are rare and we don't need to respond.
        """
        cmd_names = {
            0xF3: "BREAK",
            0xF4: "IP",
            0xF5: "AO",
            0xF6: "AYT",
            0xF7: "EC",
            0xF8: "EL",
            0xF9: "GA",
        }
        cmd_name = cmd_names.get(cmd, f"0x{cmd:02X}")
        MCPLogger.log(TOOL_LOG_NAME, f"Telnet IAC command: {cmd_name}")
    
    def _option_name(self, option: int) -> str:
        """Get human-readable option name."""
        names = {
            0: "BINARY",
            1: "ECHO",
            3: "SGA",
            24: "TERMINAL_TYPE",
            31: "NAWS",
        }
        return names.get(option, f"option_{option}")
    
    def close(self) -> None:
        """Close telnet connection."""
        self.tcp.close()
    
    def is_open(self) -> bool:
        """Check if telnet connection is open."""
        return self.tcp.is_open()
    
    # ========================================================================
    # Serial control lines (NOT supported - delegate to TCP)
    # ========================================================================
    
    # Inherit default behavior from BaseTransport (raises TransportError)
    
    # ========================================================================
    # Buffer management
    # ========================================================================
    
    def flush(self) -> None:
        """Flush buffers (delegate to TCP)."""
        self.tcp.flush()
    
    def bytes_available(self) -> int:
        """Return bytes available (delegate to TCP)."""
        return self.tcp.bytes_available()
    
    # ========================================================================
    # Capabilities (same as TCP)
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return telnet capabilities (same as TCP - no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


# ============================================================================
# PHASE 5G: RFC2217 TRANSPORT (Remote Serial over Telnet)
# ============================================================================

class RFC2217Transport(TelnetTransport):
    """Transport for RFC2217 (Telnet Com Port Control Option) - Phase 5G.
    
    RFC2217 extends telnet protocol with serial port control commands.
    Allows remote control of serial port parameters (baud, DTR/RTS, etc.)
    over a network connection.
    
    Use Cases:
    - Network-attached serial port servers (Digi, Moxa, Lantronix)
    - Remote access to physical serial devices
    - Industrial equipment with serial-over-IP
    
    Platform Support:
    - All platforms (network-based, no hardware dependencies)
    
    RFC2217 Specification:
    - COM-PORT-OPTION (44) subnegotiation commands
    - SET-BAUDRATE, SET-DATASIZE, SET-PARITY, SET-STOPSIZE
    - SET-CONTROL (DTR/RTS), NOTIFY-LINESTATE, NOTIFY-MODEMSTATE
    
    Architecture:
    - Inherits all telnet IAC handling from TelnetTransport
    - Adds COM-PORT-OPTION (44) command processing
    - Tracks serial port state (baud, parity, DTR/RTS, etc.)
    """
    
    # RFC2217 COM-PORT-OPTION (option 44)
    COM_PORT_OPTION = 44
    
    # RFC2217 Subnegotiation Commands
    SET_BAUDRATE      = 1
    SET_DATASIZE      = 2
    SET_PARITY        = 3
    SET_STOPSIZE      = 4
    SET_CONTROL       = 5
    NOTIFY_LINESTATE  = 6
    NOTIFY_MODEMSTATE = 7
    NOTIFY_BREAK      = 8
    SET_LINESTATE_MASK = 9
    SET_MODEMSTATE_MASK = 10
    PURGE_DATA        = 11
    
    # Control line values (for SET_CONTROL)
    CONTROL_DTR_ON    = 8
    CONTROL_DTR_OFF   = 9
    CONTROL_RTS_ON    = 11
    CONTROL_RTS_OFF   = 12
    
    # Modem state bits (for NOTIFY_MODEMSTATE)
    MODEMSTATE_CTS    = 0x10
    MODEMSTATE_DSR    = 0x20
    MODEMSTATE_RI     = 0x40
    MODEMSTATE_CD     = 0x80
    
    def __init__(self, tcp_transport: TCPTransport):
        """Initialize RFC2217 transport.
        
        Args:
            tcp_transport: Underlying TCP transport
        """
        # Initialize telnet layer (inherits IAC handling)
        super().__init__(tcp_transport, raw_mode=False)
        
        # RFC2217 state tracking
        self.baud_rate = 115200  # Default baud rate
        self.data_size = 8       # Default data bits
        self.parity = 0          # Default: no parity
        self.stop_size = 1       # Default: 1 stop bit
        self.dtr_state = False   # DTR off by default
        self.rts_state = False   # RTS off by default
        self.modem_state = 0     # CTS/DSR/RI/CD state
        
        MCPLogger.log(TOOL_LOG_NAME, "RFC2217 transport created (COM-PORT-OPTION enabled)")
        
        # Send initial COM-PORT-OPTION negotiation
        self._negotiate_com_port_option()
    
    def _negotiate_com_port_option(self):
        """Negotiate COM-PORT-OPTION with server.
        
        Sends: IAC WILL COM-PORT-OPTION (we want to use RFC2217)
        """
        try:
            # IAC WILL COM-PORT-OPTION
            command = bytes([self.IAC, self.WILL, self.COM_PORT_OPTION])
            self.tcp.write(command)
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Sent WILL COM-PORT-OPTION (44)")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Failed to negotiate COM-PORT-OPTION: {e}")
    
    # ========================================================================
    # RFC2217 Subnegotiation Handling (Override from TelnetTransport)
    # ========================================================================
    
    def _handle_iac_subnegotiation(self, option: int, data: bytes):
        """Handle IAC SB <option> <data> IAC SE.
        
        Overrides TelnetTransport to add RFC2217 COM-PORT-OPTION handling.
        """
        if option == self.COM_PORT_OPTION:
            # RFC2217 COM-PORT-OPTION subnegotiation
            self._handle_com_port_subnegotiation(data)
        else:
            # Pass to parent for standard telnet options
            super()._handle_iac_subnegotiation(option, data)
    
    def _handle_com_port_subnegotiation(self, data: bytes):
        """Handle RFC2217 COM-PORT-OPTION subnegotiation.
        
        Format: <command> <data...>
        """
        if len(data) < 1:
            MCPLogger.log(TOOL_LOG_NAME, "RFC2217: Empty COM-PORT-OPTION subnegotiation")
            return
        
        command = data[0]
        payload = data[1:] if len(data) > 1 else b''
        
        command_name = {
            self.SET_BAUDRATE: "SET-BAUDRATE",
            self.SET_DATASIZE: "SET-DATASIZE",
            self.SET_PARITY: "SET-PARITY",
            self.SET_STOPSIZE: "SET-STOPSIZE",
            self.SET_CONTROL: "SET-CONTROL",
            self.NOTIFY_LINESTATE: "NOTIFY-LINESTATE",
            self.NOTIFY_MODEMSTATE: "NOTIFY-MODEMSTATE",
            self.NOTIFY_BREAK: "NOTIFY-BREAK",
        }.get(command, f"UNKNOWN-{command}")
        
        MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Received {command_name} (payload: {len(payload)} bytes)")
        
        # Handle specific commands
        if command == self.NOTIFY_MODEMSTATE:
            # Server notifying us of modem state change (CTS/DSR/RI/CD)
            if len(payload) >= 1:
                self.modem_state = payload[0]
                MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Modem state updated: 0x{self.modem_state:02X}")
        
        elif command == self.NOTIFY_LINESTATE:
            # Server notifying us of line state change
            if len(payload) >= 1:
                line_state = payload[0]
                MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Line state updated: 0x{line_state:02X}")
        
        # Other commands are typically server responses to our requests
        # We log them but don't need to act on them
    
    # ========================================================================
    # Serial Port Control Methods (RFC2217-specific)
    # ========================================================================
    
    def set_baud_rate(self, baud_rate: int):
        """Set baud rate via RFC2217 SET-BAUDRATE command.
        
        Args:
            baud_rate: Desired baud rate (e.g., 115200)
        """
        # RFC2217 baud rate is sent as 4-byte big-endian integer
        baud_bytes = baud_rate.to_bytes(4, byteorder='big')
        
        # IAC SB COM-PORT-OPTION SET-BAUDRATE <baud> IAC SE
        command = bytes([
            self.IAC, self.SB, self.COM_PORT_OPTION, self.SET_BAUDRATE
        ]) + baud_bytes + bytes([self.IAC, self.SE])
        
        try:
            self.tcp.write(command)
            self.baud_rate = baud_rate
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Set baud rate to {baud_rate}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Failed to set baud rate: {e}")
            raise TransportError(f"Failed to set baud rate: {e}")
    
    def set_dtr(self, value: bool):
        """Set DTR line via RFC2217 SET-CONTROL command.
        
        Args:
            value: True = DTR on, False = DTR off
        """
        control_value = self.CONTROL_DTR_ON if value else self.CONTROL_DTR_OFF
        
        # IAC SB COM-PORT-OPTION SET-CONTROL <control> IAC SE
        command = bytes([
            self.IAC, self.SB, self.COM_PORT_OPTION, self.SET_CONTROL,
            control_value,
            self.IAC, self.SE
        ])
        
        try:
            self.tcp.write(command)
            self.dtr_state = value
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Set DTR {'ON' if value else 'OFF'}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Failed to set DTR: {e}")
            raise TransportError(f"Failed to set DTR: {e}")
    
    def set_rts(self, value: bool):
        """Set RTS line via RFC2217 SET-CONTROL command.
        
        Args:
            value: True = RTS on, False = RTS off
        """
        control_value = self.CONTROL_RTS_ON if value else self.CONTROL_RTS_OFF
        
        # IAC SB COM-PORT-OPTION SET-CONTROL <control> IAC SE
        command = bytes([
            self.IAC, self.SB, self.COM_PORT_OPTION, self.SET_CONTROL,
            control_value,
            self.IAC, self.SE
        ])
        
        try:
            self.tcp.write(command)
            self.rts_state = value
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Set RTS {'ON' if value else 'OFF'}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Failed to set RTS: {e}")
            raise TransportError(f"Failed to set RTS: {e}")
    
    def get_line_states(self) -> Dict[str, bool]:
        """Get current line states (CTS/DSR/RI/CD) from modem state.
        
        Returns:
            Dictionary with line state values
        """
        return {
            "cts": bool(self.modem_state & self.MODEMSTATE_CTS),
            "dsr": bool(self.modem_state & self.MODEMSTATE_DSR),
            "ri":  bool(self.modem_state & self.MODEMSTATE_RI),
            "cd":  bool(self.modem_state & self.MODEMSTATE_CD),
            "dtr": self.dtr_state,
            "rts": self.rts_state,
        }
    
    def send_break(self, duration: float = 0.25):
        """Send BREAK signal via RFC2217 NOTIFY-BREAK command.
        
        Args:
            duration: Break duration in seconds (converted to milliseconds)
        """
        # RFC2217 break duration is in milliseconds (2-byte big-endian)
        duration_ms = int(duration * 1000)
        duration_bytes = duration_ms.to_bytes(2, byteorder='big')
        
        # IAC SB COM-PORT-OPTION NOTIFY-BREAK <duration_ms> IAC SE
        command = bytes([
            self.IAC, self.SB, self.COM_PORT_OPTION, self.NOTIFY_BREAK
        ]) + duration_bytes + bytes([self.IAC, self.SE])
        
        try:
            self.tcp.write(command)
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Sent BREAK ({duration_ms}ms)")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"RFC2217: Failed to send BREAK: {e}")
            raise TransportError(f"Failed to send BREAK: {e}")
    
    # ========================================================================
    # Capabilities (RFC2217 supports serial features!)
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return RFC2217 capabilities (supports serial features over network)."""
        return {
            "dtr_rts": True,        # Can control DTR/RTS via RFC2217
            "line_states": True,    # Can read CTS/DSR/RI/CD via NOTIFY-MODEMSTATE
            "break_signal": True,   # Can send BREAK via NOTIFY-BREAK
            "baud_rate": True,      # Can change baud rate via SET-BAUDRATE
            "flow_control": False   # Flow control is handled by remote device
        }


# ============================================================================
# PHASE 5H: WEBSOCKET TRANSPORT (Modern Web-Based Communication)
# ============================================================================

class WebSocketTransport(BaseTransport):
    """Transport for WebSocket protocol (ws:// and wss://) - Phase 5H.
    
    WebSocket provides bidirectional communication over HTTP/HTTPS.
    Perfect for modern web-based device consoles and cloud IoT platforms.
    
    Use Cases:
    - ESP32/ESP8266 web-based REPLs
    - Cloud IoT platforms (AWS IoT, Azure IoT)
    - Browser-based terminal emulators
    - Real-time data streams
    - Modern embedded web interfaces
    - Chrome DevTools Protocol (CDP) debugging
    - JSON-RPC APIs over WebSocket
    
    Platform Support:
    - All platforms (network-based, no hardware dependencies)
    
    WebSocket Features:
    - Bidirectional full-duplex communication
    - Built on HTTP/HTTPS (firewall-friendly)
    - Frame-based protocol (text and binary)
    - Automatic ping/pong keepalive
    - SSL/TLS support (wss://)
    - Smart text/binary mode detection
    
    Frame Mode (ws_mode parameter):
    - 'auto' (default): Auto-detect JSON and send as text, otherwise binary
    - 'text': Always send text frames (for JSON-RPC, CDP, text-based APIs)
    - 'binary': Always send binary frames (for raw binary protocols)
    
    Architecture:
    - Uses websocket-client library (simple, well-maintained)
    - Auto-installs if missing (like paramiko, zeroconf, pywinpty)
    - Non-blocking I/O with timeout handling
    - Automatic reconnection on connection loss
    """
    
    def __init__(self, url: str, timeout: float = 10.0, headers: Dict[str, str] = None, ws_mode: str = "auto"):
        """Initialize WebSocket transport.
        
        Args:
            url: WebSocket URL (ws://host:port/path or wss://host:port/path)
            timeout: Connection and read timeout in seconds
            headers: Optional HTTP headers for WebSocket handshake
            ws_mode: Frame mode - 'auto' (detect JSON), 'text' (always text), 'binary' (always binary)
        """
        import sys
        
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}
        self.ws_mode = ws_mode if ws_mode in ("auto", "text", "binary") else "auto"
        self.ws = None
        self._connected = False
        
        MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Connecting to {url} (mode: {self.ws_mode})")
        
        # Auto-install websocket-client if missing
        try:
            import websocket
        except ImportError:
            MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] websocket-client not found, attempting auto-install...")
            self._auto_install_websocket()
            import websocket
        
        try:
            # Create WebSocket connection
            # Note: websocket-client uses 'websocket' module name
            self.ws = websocket.create_connection(
                url,
                timeout=timeout,
                header=list(f"{k}: {v}" for k, v in self.headers.items()) if self.headers else None
            )
            
            # Set socket to non-blocking mode
            self.ws.sock.setblocking(False)
            
            self._connected = True
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Connected to {url}")
            
        except websocket.WebSocketException as e:
            error_msg = f"WebSocket connection failed to {url}: {e}"
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] ERROR: {error_msg}")
            raise TransportConnectionError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error connecting to {url}: {e}"
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] ERROR: {error_msg}")
            raise TransportConnectionError(error_msg) from e
    
    def _auto_install_websocket(self):
        """Auto-install websocket-client library if missing."""
        import subprocess
        import sys
        import platform
        
        MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] Auto-installing websocket-client...")
        
        try:
            # Use subprocess.run (not deprecated pip.main)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "websocket-client"],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            
            if result.returncode == 0:
                MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] websocket-client installed successfully!")
                MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] NOTE: Server restart may be required for import to work")
            else:
                error_msg = f"Failed to install websocket-client: {result.stderr}"
                MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] ERROR: {error_msg}")
                raise TransportConnectionError(
                    f"websocket-client library not found and auto-install failed. "
                    f"Please install manually: pip install websocket-client"
                )
        except subprocess.TimeoutExpired:
            raise TransportConnectionError(
                "websocket-client installation timed out. Please install manually: pip install websocket-client"
            )
        except Exception as e:
            raise TransportConnectionError(
                f"Failed to auto-install websocket-client: {e}. "
                f"Please install manually: pip install websocket-client"
            )
    
    def write(self, data: bytes) -> int:
        """Write data to WebSocket connection.
        
        Frame type is determined by ws_mode:
        - 'auto': Detect JSON and send as text, otherwise binary
        - 'text': Always send as text frame (UTF-8 decode)
        - 'binary': Always send as binary frame
        """
        if not self.is_open():
            raise TransportConnectionError("WebSocket is closed")
        
        try:
            import websocket
            import json
            
            # Determine frame type based on ws_mode
            send_as_text = False
            
            if self.ws_mode == "text":
                # Always send as text
                send_as_text = True
            elif self.ws_mode == "auto":
                # Auto-detect: Try to decode as UTF-8 and parse as JSON
                try:
                    text = data.decode('utf-8')
                    json.loads(text)  # Validate it's valid JSON
                    send_as_text = True
                    MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Auto-detected JSON, sending as text frame")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Not valid UTF-8 JSON, send as binary
                    send_as_text = False
            # else: ws_mode == "binary", send_as_text stays False
            
            # Send the frame
            if send_as_text:
                text = data.decode('utf-8') if isinstance(data, bytes) else data
                self.ws.send(text)  # Send as text frame (OPCODE_TEXT)
            else:
                self.ws.send_binary(data)  # Send as binary frame (OPCODE_BINARY)
            
            return len(data)
            
        except websocket.WebSocketException as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Write error: {e}")
            self._connected = False
            raise TransportConnectionError(f"Failed to write to WebSocket: {e}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Write error: {e}")
            raise TransportConnectionError(f"Failed to write to WebSocket: {e}")
    
    def read(self, size: int) -> bytes:
        """Read data from WebSocket connection.
        
        Receives WebSocket frames (text or binary).
        Non-blocking - returns empty bytes if no data available.
        """
        if not self.is_open():
            raise TransportConnectionError("WebSocket is closed")
        
        try:
            import websocket
            
            # Receive frame (non-blocking due to socket.setblocking(False))
            opcode, data = self.ws.recv_data(control_frame=False)
            
            # Handle different frame types
            if opcode == websocket.ABNF.OPCODE_TEXT:
                # Text frame - convert to bytes
                return data.encode('utf-8') if isinstance(data, str) else data
            elif opcode == websocket.ABNF.OPCODE_BINARY:
                # Binary frame - return as-is
                return data
            elif opcode == websocket.ABNF.OPCODE_CLOSE:
                # Close frame received
                MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] Close frame received")
                self._connected = False
                raise TransportConnectionError("WebSocket closed by remote end")
            else:
                # Ping/Pong frames are handled automatically by library
                return b''
                
        except BlockingIOError:
            # No data available (non-blocking socket)
            return b''
        except websocket.WebSocketConnectionClosedException:
            MCPLogger.log(TOOL_LOG_NAME, "[WebSocketTransport] Connection closed")
            self._connected = False
            raise TransportConnectionError("WebSocket connection closed")
        except Exception as e:
            # Check if it's just "no data available" (common in non-blocking mode)
            if "timed out" in str(e).lower() or "would block" in str(e).lower():
                return b''
            
            MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Read error: {e}")
            raise TransportConnectionError(f"Failed to read from WebSocket: {e}")
    
    def close(self) -> None:
        """Close WebSocket connection."""
        if self.ws:
            try:
                MCPLogger.log(TOOL_LOG_NAME, f"[WebSocketTransport] Closing connection to {self.url}")
                self.ws.close()
            except:
                pass  # Ignore errors during close
            finally:
                self.ws = None
                self._connected = False
    
    def is_open(self) -> bool:
        """Check if WebSocket connection is open."""
        return self.ws is not None and self._connected
    
    def flush(self) -> None:
        """Flush WebSocket connection (no-op for WebSocket)."""
        pass
    
    def bytes_available(self) -> int:
        """Return number of bytes available to read (WebSocket doesn't support this)."""
        return 0
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return WebSocket capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class BluetoothTransport(BaseTransport):
    """Transport for Classic Bluetooth (RFCOMM/SPP) - Phase 5J-1.
    
    Classic Bluetooth Serial Port Profile (SPP) provides wireless RS-232 emulation.
    Perfect for ESP32 Classic BT, HC-05/HC-06 modules, and wireless serial debugging.
    
    Uses pybluez library (requires pre-built wheel or compilation).
    """
    
    def __init__(self, address: str, port: int = 1, pin: str = None, timeout: float = 10.0):
        """Initialize Bluetooth RFCOMM connection.
        
        Args:
            address: Bluetooth MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            port: RFCOMM channel/port (default 1 for SPP)
            pin: Optional PIN code for pairing (e.g., "1234")
            timeout: Connection timeout in seconds
        """
        super().__init__()
        
        # Try to load pybluez (graceful fallback if not available)
        bluetooth = ensure_pybluez()
        if bluetooth is None:
            raise TransportConnectionError(
                "pybluez library not available. Classic Bluetooth (RFCOMM/SPP) requires pybluez. "
                "Please install pre-built wheel or compile from source: pip install pybluez"
            )
        
        self.address = address
        self.port = port
        self.pin = pin
        self.timeout = timeout
        self.socket = None
        self._connected = False
        
        MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Connecting to {address}:{port}...")
        
        try:
            # Create RFCOMM socket
            self.socket = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            
            # Set PIN if provided (for pairing)
            if pin:
                MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Setting PIN for pairing...")
                # Note: PIN setting is platform-specific and may not work on all systems
                # Some platforms require manual pairing via OS settings
                try:
                    bluetooth.set_pin(address, pin)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] WARNING: Failed to set PIN (may need manual pairing): {e}")
            
            # Connect to remote device
            self.socket.settimeout(timeout)
            self.socket.connect((address, port))
            
            # Set non-blocking mode after connection
            self.socket.setblocking(False)
            self._connected = True
            
            MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Connected successfully to {address}:{port}")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Connection failed: {e}")
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
            raise TransportConnectionError(f"Failed to connect to Bluetooth device {address}:{port}: {e}")
    
    def write(self, data: bytes) -> int:
        """Write data to Bluetooth connection."""
        if not self.is_open():
            raise TransportConnectionError("Bluetooth connection is closed")
        
        try:
            sent = self.socket.send(data)
            return sent
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Write error: {e}")
            self._connected = False
            raise TransportConnectionError(f"Failed to write to Bluetooth: {e}")
    
    def read(self, size: int) -> bytes:
        """Read data from Bluetooth connection (non-blocking).
        
        Returns:
            bytes: Data read (may be empty if no data available)
            
        Raises:
            TransportConnectionError: If connection is closed
        """
        if not self.is_open():
            raise TransportConnectionError("Bluetooth connection is closed")
        
        try:
            # Non-blocking read
            data = self.socket.recv(size)
            
            # Empty data means connection closed
            if data == b'':
                MCPLogger.log(TOOL_LOG_NAME, "[BluetoothTransport] Connection closed by remote device (EOF)")
                self._connected = False
                raise TransportConnectionError("Bluetooth connection closed by remote device (EOF)")
            
            return data
            
        except BlockingIOError:
            # No data available right now (expected in non-blocking mode)
            return b''
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Read error: {e}")
            self._connected = False
            raise TransportConnectionError(f"Failed to read from Bluetooth: {e}")
    
    def close(self) -> None:
        """Close Bluetooth connection."""
        if self.socket:
            try:
                MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Closing connection to {self.address}:{self.port}")
                self.socket.close()
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"[BluetoothTransport] Error during close: {e}")
            finally:
                self._connected = False
                self.socket = None
    
    def is_open(self) -> bool:
        """Check if Bluetooth connection is open."""
        return self._connected and self.socket is not None
    
    def flush(self) -> None:
        """Flush Bluetooth connection (no-op for Bluetooth)."""
        pass
    
    def bytes_available(self) -> int:
        """Return number of bytes available to read (Bluetooth doesn't support this)."""
        return 0
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return Bluetooth capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class BLETransport(BaseTransport):
    """Transport for Bluetooth Low Energy (BLE/GATT) - Phase 5J-2.
    
    BLE GATT provides low-power wireless communication for modern IoT devices.
    Perfect for ESP32 BLE, fitness trackers, environmental sensors, and beacons.
    
    Uses bleak library (pure Python, auto-installs if missing).
    
    Supports:
    - Generic GATT read/write/subscribe operations
    - BLE UART mode (Nordic UART Service auto-detection)
    - Standard BLE services (battery, temperature, etc.)
    """
    
    def __init__(self, address: str, timeout: float = 10.0, ble_mode: str = "uart"):
        """Initialize BLE GATT connection.
        
        Args:
            address: BLE MAC address (e.g., "11:22:33:44:55:66")
            timeout: Connection timeout in seconds
            ble_mode: Connection mode - "uart" (Nordic UART Service) or "generic" (manual GATT)
        """
        super().__init__()
        
        # Ensure bleak is available (auto-install if missing)
        bleak = ensure_bleak()
        
        self.address = address
        self.timeout = timeout
        self.ble_mode = ble_mode
        self.client = None
        self._connected = False
        self._notification_queue = queue.Queue()
        
        # Nordic UART Service UUIDs (for UART mode)
        self.UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
        self.UART_TX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write to this
        self.UART_RX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notifications from this
        
        self.tx_characteristic = None
        self.rx_characteristic = None
        
        MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Connecting to {address} (mode: {ble_mode})...")
        
        try:
            import asyncio
            
            # Run async connection in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                loop.run_until_complete(self._async_connect())
            finally:
                # Keep loop running for async operations
                pass
            
            MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Connected successfully to {address}")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Connection failed: {e}")
            raise TransportConnectionError(f"Failed to connect to BLE device {address}: {e}")
    
    async def _async_connect(self):
        """Async connection logic (runs in event loop)."""
        import bleak
        
        # Create BLE client
        self.client = bleak.BleakClient(self.address, timeout=self.timeout)
        
        # Connect
        await self.client.connect()
        self._connected = True
        
        # If UART mode, discover and setup Nordic UART Service
        if self.ble_mode == "uart":
            MCPLogger.log(TOOL_LOG_NAME, "[BLETransport] Looking for Nordic UART Service...")
            
            # Find TX characteristic (write)
            try:
                self.tx_characteristic = self.client.services.get_characteristic(self.UART_TX_CHAR_UUID)
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Found TX characteristic: {self.UART_TX_CHAR_UUID}")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] WARNING: TX characteristic not found: {e}")
            
            # Find RX characteristic (notifications)
            try:
                self.rx_characteristic = self.client.services.get_characteristic(self.UART_RX_CHAR_UUID)
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Found RX characteristic: {self.UART_RX_CHAR_UUID}")
                
                # Subscribe to notifications
                await self.client.start_notify(self.UART_RX_CHAR_UUID, self._notification_handler)
                MCPLogger.log(TOOL_LOG_NAME, "[BLETransport] Subscribed to RX notifications")
                
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] WARNING: RX characteristic not found: {e}")
            
            if not self.tx_characteristic or not self.rx_characteristic:
                raise TransportConnectionError(
                    f"Nordic UART Service not found on device {self.address}. "
                    f"Try ble_mode='generic' for manual GATT operations."
                )
    
    def _notification_handler(self, sender, data: bytearray):
        """Handle incoming BLE notifications (async callback)."""
        # Convert bytearray to bytes and queue it
        self._notification_queue.put(bytes(data))
    
    def write(self, data: bytes) -> int:
        """Write data to BLE device (UART TX characteristic).
        
        Note: This is a synchronous wrapper around async BLE write.
        """
        if not self.is_open():
            raise TransportConnectionError("BLE connection is closed")
        
        if self.ble_mode == "uart" and not self.tx_characteristic:
            raise TransportConnectionError("BLE UART TX characteristic not available")
        
        try:
            import asyncio
            
            # Run async write in event loop
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self._async_write(data))
            
            return len(data)
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Write error: {e}")
            self._connected = False
            raise TransportConnectionError(f"Failed to write to BLE: {e}")
    
    async def _async_write(self, data: bytes):
        """Async write logic."""
        if self.ble_mode == "uart":
            # Write to TX characteristic
            await self.client.write_gatt_char(self.UART_TX_CHAR_UUID, data, response=False)
        else:
            raise TransportConnectionError("Generic BLE mode requires explicit characteristic UUID (use ble_write operation)")
    
    def read(self, size: int) -> bytes:
        """Read data from BLE device (from notification queue).
        
        Returns:
            bytes: Data read (may be empty if no notifications pending)
        """
        if not self.is_open():
            raise TransportConnectionError("BLE connection is closed")
        
        try:
            # Try to get data from notification queue (non-blocking)
            data = self._notification_queue.get_nowait()
            return data
        except queue.Empty:
            # No notifications pending
            return b''
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Read error: {e}")
            raise TransportConnectionError(f"Failed to read from BLE: {e}")
    
    def close(self) -> None:
        """Close BLE connection."""
        if self.client:
            try:
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Closing connection to {self.address}")
                
                import asyncio
                
                # Run async disconnect
                loop = asyncio.get_event_loop()
                loop.run_until_complete(self.client.disconnect())
                
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"[BLETransport] Error during close: {e}")
            finally:
                self._connected = False
                self.client = None
    
    def is_open(self) -> bool:
        """Check if BLE connection is open."""
        return self._connected and self.client is not None
    
    def flush(self) -> None:
        """Flush BLE connection (clear notification queue)."""
        # Clear pending notifications
        while not self._notification_queue.empty():
            try:
                self._notification_queue.get_nowait()
            except queue.Empty:
                break
    
    def bytes_available(self) -> int:
        """Return number of bytes available to read (from notification queue)."""
        return self._notification_queue.qsize()
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return BLE capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class SSHTransport(BaseTransport):
    """Transport wrapper for SSH protocol (Phase 5C).
    
    Wraps paramiko.SSHClient for secure shell access.
    - Password and key-based authentication
    - PTY allocation with terminal size
    - Non-blocking shell channel I/O
    - Host key management
    - Comprehensive credential sanitization (NEVER log passwords)
    
    Design Philosophy:
    - Secure by default (reject unknown hosts unless explicitly allowed)
    - Comprehensive error messages
    - Clean credential handling (no password leaks in logs/errors)
    """
    
    def __init__(self, host: str, port: int = 22, username: str = None, 
                 password: str = None, key_filename: str = None, key_data: str = None,
                 key_password: str = None, allow_unknown_hosts: bool = False,
                 connect_timeout: float = 10.0, terminal_type: str = "xterm-256color",
                 terminal_width: int = 80, terminal_height: int = 24, compression: bool = False,
                 otp_secret: str = None, otp_code: str = None, allow_agent: bool = False):
        """Initialize SSH transport and connect.
        
        Args:
            host: Hostname or IP address
            port: SSH port (default 22)
            username: Username for authentication (required)
            password: Password for authentication (optional, used if no key)
            key_filename: Path to private key file (optional)
            key_data: Inline private key data (optional, alternative to key_filename)
            key_password: Password for encrypted key (optional)
            allow_unknown_hosts: If True, auto-add unknown host keys (INSECURE, testing only)
            connect_timeout: Connection timeout in seconds
            terminal_type: Terminal type for PTY (default: xterm-256color)
            terminal_width: Terminal width in characters (default: 80)
            terminal_height: Terminal height in lines (default: 24)
            compression: Enable SSH compression for slow links (default: False)
            otp_secret: TOTP secret for 2FA (Base32, Phase 5C-4)
            otp_code: Pre-generated OTP code for 2FA (Phase 5C-4)
            allow_agent: Try SSH agent for keys (Phase 5C-4, OUT OF SCOPE for MCP)
            
        Raises:
            TransportConnectionError: Connection failed
            TransportAuthenticationError: Authentication failed
            TransportError: General SSH error
        """
        paramiko = ensure_paramiko()
        
        self.host = host
        self.port = port
        self.username = username
        self._password = password  # PRIVATE - never log
        self.ssh_client = None
        self.channel = None
        self._connected = False
        
        # Credential sanitization - mask sensitive data for logging
        auth_desc = []
        if password:
            auth_desc.append("password=***")
        if key_filename:
            auth_desc.append(f"key_file={key_filename}")
        if key_data:
            auth_desc.append("key_data=<inline>")
        auth_str = ", ".join(auth_desc) if auth_desc else "no credentials"
        
        MCPLogger.log(TOOL_LOG_NAME, 
                     f"SSH connecting to {username}@{host}:{port} ({auth_str}, timeout={connect_timeout}s)")
        
        try:
            # Create SSH client
            self.ssh_client = paramiko.SSHClient()
            
            # Host key policy
            if allow_unknown_hosts:
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Using AutoAddPolicy (INSECURE - auto-accepting unknown hosts)")
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Using RejectPolicy (secure - rejecting unknown hosts)")
                self.ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
            
            # Load system host keys
            try:
                self.ssh_client.load_system_host_keys()
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Loaded system host keys")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: Could not load system host keys: {e}")
            
            # Connect with authentication
            connect_kwargs = {
                'hostname': host,
                'port': port,
                'username': username,
                'timeout': connect_timeout,
                'banner_timeout': connect_timeout,
                'auth_timeout': connect_timeout,
                'compress': compression,  # Phase 5C-7: SSH compression
            }
            
            if compression:
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Compression enabled (useful for slow links)")
            
            # Phase 5C-4: Keyboard-interactive auth handler (for 2FA/OTP)
            otp_to_use = None
            if otp_secret:
                # Generate TOTP code from secret
                pyotp = ensure_pyotp()
                if pyotp:
                    try:
                        totp = pyotp.TOTP(otp_secret)
                        otp_to_use = totp.now()
                        MCPLogger.log(TOOL_LOG_NAME, "SSH: Generated TOTP code from secret (6 digits, ***)")
                    except Exception as e:
                        MCPLogger.log(TOOL_LOG_NAME, f"SSH: Failed to generate TOTP: {e}")
                else:
                    MCPLogger.log(TOOL_LOG_NAME, "SSH: pyotp not available, cannot generate TOTP (install with: pip install pyotp)")
            elif otp_code:
                otp_to_use = otp_code
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Using pre-generated OTP code (***)")
            
            if otp_to_use:
                # Create keyboard-interactive handler that responds with OTP
                def auth_handler(title, instructions, prompt_list):
                    """Handle keyboard-interactive challenges (Phase 5C-4: 2FA/OTP)."""
                    MCPLogger.log(TOOL_LOG_NAME, f"SSH: Keyboard-interactive challenge: {title or 'no title'}")
                    if instructions:
                        MCPLogger.log(TOOL_LOG_NAME, f"SSH: Instructions: {instructions}")
                    
                    responses = []
                    for prompt, echo in prompt_list:
                        MCPLogger.log(TOOL_LOG_NAME, f"SSH: Prompt: '{prompt}' (echo={echo})")
                        # Respond with OTP for any prompt (common: "Verification code:")
                        responses.append(otp_to_use)
                        MCPLogger.log(TOOL_LOG_NAME, "SSH: Responding with OTP code (***)") 
                    return responses
                
                # Set the handler (Paramiko will call it if server requests keyboard-interactive)
                connect_kwargs['auth_handler'] = auth_handler
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Keyboard-interactive handler registered for 2FA/OTP")
            
            # Phase 5C-4: SSH agent support (OUT OF SCOPE, but easy to add)
            if allow_agent:
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Agent support requested (OUT OF SCOPE for MCP - automated/headless)")
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Ignoring allow_agent=True (use unencrypted keys or ssh_key_password instead)")
            
            # Add authentication method (Phase 5C-2: Enhanced key support, Phase 5C-4: Multi-factor)
            auth_methods = []
            
            if key_filename:
                # Phase 5C-2: Auto-detect key type from file
                pkey = self._load_key_file(key_filename, key_password, paramiko)
                if pkey:
                    connect_kwargs['pkey'] = pkey
                    auth_methods.append("key_file")
                else:
                    # Fallback: let paramiko try (it will auto-detect)
                    connect_kwargs['key_filename'] = key_filename
                    if key_password:
                        connect_kwargs['passphrase'] = key_password
                    auth_methods.append("key_file")
            elif key_data:
                # Phase 5C-2: Parse inline key data with auto-detection
                pkey = self._load_key_data(key_data, key_password, paramiko)
                if pkey:
                    connect_kwargs['pkey'] = pkey
                    auth_methods.append("key_inline")
                else:
                    raise TransportAuthenticationError(f"Failed to load inline key data (tried all key types)")
            
            # Phase 5C-4: Multi-factor auth (key + password)
            # Note: Password can be used WITH key (not just instead of key)
            if password:
                connect_kwargs['password'] = password
                auth_methods.append("password")
            
            # If no explicit auth provided, try defaults
            if not auth_methods:
                # Try SSH agent or default keys
                connect_kwargs['look_for_keys'] = True
                auth_methods.append("default_keys")
            
            MCPLogger.log(TOOL_LOG_NAME, f"SSH: Auth methods: {', '.join(auth_methods)}")
            
            MCPLogger.log(TOOL_LOG_NAME, "SSH: Initiating connection...")
            self.ssh_client.connect(**connect_kwargs)
            MCPLogger.log(TOOL_LOG_NAME, "SSH: Connection established")
            
            # Get transport for keepalives
            transport = self.ssh_client.get_transport()
            if transport:
                transport.set_keepalive(30)  # Send keepalive every 30 seconds
                MCPLogger.log(TOOL_LOG_NAME, "SSH: Keepalive enabled (30s)")
            
            # Allocate PTY and open shell channel
            MCPLogger.log(TOOL_LOG_NAME, f"SSH: Requesting PTY ({terminal_type}, {terminal_width}x{terminal_height})")
            self.channel = self.ssh_client.invoke_shell(
                term=terminal_type,
                width=terminal_width,
                height=terminal_height
            )
            
            # Set non-blocking mode
            self.channel.setblocking(False)
            MCPLogger.log(TOOL_LOG_NAME, "SSH: Shell channel opened (non-blocking)")
            
            self._connected = True
            MCPLogger.log(TOOL_LOG_NAME, f"SSH transport ready: {username}@{host}:{port}")
            
        except paramiko.AuthenticationException as e:
            error_msg = f"SSH authentication failed for {username}@{host}:{port}"
            MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg}: {e}")
            raise TransportAuthenticationError(error_msg) from e
            
        except paramiko.SSHException as e:
            error_msg = f"SSH connection failed to {host}:{port}"
            MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg}: {e}")
            raise TransportConnectionError(error_msg) from e
            
        except Exception as e:
            error_msg = f"SSH error connecting to {host}:{port}"
            MCPLogger.log(TOOL_LOG_NAME, f"ERROR: {error_msg}: {e}")
            raise TransportError(error_msg) from e
    
    # ========================================================================
    # Key Loading Helpers (Phase 5C-2)
    # ========================================================================
    
    def _load_key_file(self, key_filename: str, key_password: str, paramiko) -> 'paramiko.PKey':
        """Load SSH private key from file with auto-detection of key type.
        
        Tries all supported key types in order: Ed25519, ECDSA, RSA, DSA (if available).
        
        Args:
            key_filename: Path to private key file
            key_password: Password for encrypted keys (optional)
            paramiko: Paramiko module
            
        Returns:
            Loaded key object, or None if all attempts failed
        """
        # Try key types in order of preference (modern -> legacy)
        # Note: DSA removed in newer paramiko versions (deprecated for security)
        key_classes = []
        
        # Add available key types (gracefully handle missing types)
        for key_type_name, key_class_name in [
            ('Ed25519', 'Ed25519Key'),
            ('ECDSA', 'ECDSAKey'),
            ('RSA', 'RSAKey'),
            ('DSA', 'DSSKey'),  # May not exist in newer paramiko
        ]:
            if hasattr(paramiko, key_class_name):
                key_classes.append((key_type_name, getattr(paramiko, key_class_name)))
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: {key_type_name} key type not available in this paramiko version (skipped)")
        
        
        last_error = None
        for key_type_name, key_class in key_classes:
            try:
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: Trying to load key as {key_type_name}...")
                if key_password:
                    pkey = key_class.from_private_key_file(key_filename, password=key_password)
                else:
                    pkey = key_class.from_private_key_file(key_filename)
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: Successfully loaded {key_type_name} key from {key_filename}")
                return pkey
            except Exception as e:
                last_error = e
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: {key_type_name} key load failed: {e}")
                continue
        
        # All attempts failed
        MCPLogger.log(TOOL_LOG_NAME, f"SSH: Could not load key file {key_filename} (tried all types). Last error: {last_error}")
        return None
    
    def _load_key_data(self, key_data: str, key_password: str, paramiko) -> 'paramiko.PKey':
        """Load SSH private key from inline data string with auto-detection of key type.
        
        Tries all supported key types in order: Ed25519, ECDSA, RSA, DSA (if available).
        
        Args:
            key_data: Private key data as string
            key_password: Password for encrypted keys (optional)
            paramiko: Paramiko module
            
        Returns:
            Loaded key object, or None if all attempts failed
        """
        from io import StringIO
        
        # Try key types in order of preference (modern -> legacy)
        # Note: DSA removed in newer paramiko versions (deprecated for security)
        key_classes = []
        
        # Add available key types (gracefully handle missing types)
        for key_type_name, key_class_name in [
            ('Ed25519', 'Ed25519Key'),
            ('ECDSA', 'ECDSAKey'),
            ('RSA', 'RSAKey'),
            ('DSA', 'DSSKey'),  # May not exist in newer paramiko
        ]:
            if hasattr(paramiko, key_class_name):
                key_classes.append((key_type_name, getattr(paramiko, key_class_name)))
            else:
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: {key_type_name} key type not available in this paramiko version (skipped)")
        
        
        last_error = None
        for key_type_name, key_class in key_classes:
            try:
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: Trying to parse inline key as {key_type_name}...")
                key_file = StringIO(key_data)
                if key_password:
                    pkey = key_class.from_private_key(key_file, password=key_password)
                else:
                    pkey = key_class.from_private_key(key_file)
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: Successfully parsed {key_type_name} key from inline data")
                return pkey
            except Exception as e:
                last_error = e
                MCPLogger.log(TOOL_LOG_NAME, f"SSH: {key_type_name} inline key parse failed: {e}")
                continue
        
        # All attempts failed
        MCPLogger.log(TOOL_LOG_NAME, f"SSH: Could not parse inline key data (tried all types). Last error: {last_error}")
        return None
    
    # ========================================================================
    # Core I/O (shell channel operations)
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to SSH shell channel.
        
        Args:
            data: Data to write
            
        Returns:
            Number of bytes written
            
        Raises:
            TransportConnectionError: Channel closed
            TransportError: Write failed
        """
        if not self._connected or not self.channel:
            raise TransportConnectionError("SSH channel not connected")
        
        try:
            # channel.send() may block if buffer is full, but we're non-blocking
            # so it will return immediately with partial write
            bytes_sent = self.channel.send(data)
            
            if bytes_sent == 0 and len(data) > 0:
                # Channel might be closed
                if self.channel.closed:
                    MCPLogger.log(TOOL_LOG_NAME, "SSH channel closed during write")
                    raise TransportConnectionError("SSH channel closed")
            
            MCPLogger.log(TOOL_LOG_NAME, f"SSH sent {bytes_sent} bytes")
            return bytes_sent
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"SSH write error: {e}")
            raise TransportError(f"SSH write failed: {e}") from e
    
    def read(self, size: int) -> bytes:
        """Read data from SSH shell channel.
        
        Args:
            size: Maximum bytes to read
            
        Returns:
            Data read (may be less than size, or empty if no data available)
            
        Raises:
            TransportConnectionError: Channel closed
            TransportError: Read failed
        """
        if not self._connected or not self.channel:
            raise TransportConnectionError("SSH channel not connected")
        
        try:
            # Non-blocking read - returns immediately
            if self.channel.recv_ready():
                data = self.channel.recv(size)
                
                if not data and self.channel.closed:
                    # EOF - channel closed by remote
                    MCPLogger.log(TOOL_LOG_NAME, "SSH channel closed by remote (EOF)")
                    raise TransportConnectionError("SSH channel closed by remote")
                
                if data:
                    MCPLogger.log(TOOL_LOG_NAME, f"SSH received {len(data)} bytes")
                
                return data
            else:
                # No data available (non-blocking)
                return b''
                
        except TransportConnectionError:
            raise  # Re-raise connection errors
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"SSH read error: {e}")
            raise TransportError(f"SSH read failed: {e}") from e
    
    def close(self) -> None:
        """Close SSH connection."""
        if self.channel:
            try:
                self.channel.close()
                MCPLogger.log(TOOL_LOG_NAME, "SSH channel closed")
            except:
                pass
            self.channel = None
        
        if self.ssh_client:
            try:
                self.ssh_client.close()
                MCPLogger.log(TOOL_LOG_NAME, "SSH client closed")
            except:
                pass
            self.ssh_client = None
        
        self._connected = False
    
    def is_open(self) -> bool:
        """Check if SSH connection is open."""
        return self._connected and self.channel and not self.channel.closed
    
    # ========================================================================
    # Serial control lines (NOT supported for SSH)
    # ========================================================================
    
    # Inherit default behavior from BaseTransport (raises TransportError)
    
    # ========================================================================
    # Buffer management
    # ========================================================================
    
    def flush(self) -> None:
        """Flush SSH buffers (no-op for SSH)."""
        # SSH handles buffering internally
        pass
    
    def bytes_available(self) -> int:
        """Return bytes available for reading.
        
        Note: SSH doesn't expose buffer size, so we return 0.
        Worker thread will call read() anyway to check for data.
        """
        return 0
    
    # ========================================================================
    # Capabilities
    # ========================================================================
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return SSH capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class ProgramTransport(BaseTransport):
    """Transport for local program/process execution with PTY (Phase 5E).
    
    Spawns a local program (cmd.exe, python, bash, etc.) with a pseudo-terminal (PTY)
    and provides read/write access to its STDIO streams.
    
    Cross-platform implementation:
    - POSIX (Linux/macOS/WSL): Uses built-in `pty` module
    - Windows: Uses `pywinpty` library (auto-installed)
    
    Use Cases:
    - MCP tool testing (attach to STDIO-based MCP servers)
    - Interactive REPLs (Python, Node.js, Ruby)
    - Build tool monitoring (watch compilation with ANSI colors)
    - CLI testing (programmatic control of command-line apps)
    
    Security:
    - Logs full command line for audit trail
    - Sanitizes credentials in environment variables
    - Captures exit code on process termination
    """
    
    def __init__(self, command: str, args: List[str] = None, env: Dict[str, str] = None,
                 cwd: str = None, cols: int = 80, rows: int = 24, elevated: bool = False,
                 elevation_password: str = None):
        """Initialize program transport.
        
        Args:
            command: Program to execute (e.g., "python", "cmd.exe", "/bin/bash")
            args: Command-line arguments (default: [])
            env: Environment variables (default: inherit from parent)
            cwd: Working directory (default: current directory)
            cols: Terminal width in characters (default: 80)
            rows: Terminal height in lines (default: 24)
            elevated: Launch with elevated privileges (Phase 5K: Linux/macOS via sudo)
            elevation_password: Password for elevation (Phase 5K). If None, uses interactive prompt or cached credentials.
        """
        import sys
        import os
        
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.elevated = elevated
        self.elevation_password = elevation_password
        
        self.process = None
        self.pty_fd = None  # POSIX: file descriptor
        self.pty_master = None  # Windows: pywinpty PTY object
        self.exit_code = None
        self.is_windows = (sys.platform == 'win32')
        
        # Elevated session support (Phase 5K)
        self.elevated_sock = None  # TCP socket to bridge script
        self.elevated_listener = None  # TCP listener for bridge connection
        self.bridge_port = None  # Port bridge will connect to
        self.bridge_token = None  # Authentication token
        
        # Sanitize and log command for audit
        sanitized_env = self._sanitize_env_for_logging(env) if env else None
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Spawning: {command} {args}")
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Working directory: {cwd or os.getcwd()}")
        if sanitized_env:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Environment: {sanitized_env}")
        
        # Spawn process with PTY
        try:
            if self.elevated:
                # Phase 5K: Use TCP bridge for elevated sessions
                self._spawn_elevated_bridge()
            elif self.is_windows:
                self._spawn_windows()
            else:
                self._spawn_posix()
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Process spawned successfully")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Failed to spawn process: {e}")
            raise TransportConnectionError(f"Failed to spawn program '{command}': {e}")
    
    def _sanitize_env_for_logging(self, env: Dict[str, str]) -> Dict[str, str]:
        """Sanitize environment variables for logging (hide credentials).
        
        For keys containing API, KEY, PASS, SECRET, TOKEN, AUTH:
        - Keep first 2 and last 2 chars
        - Replace middle with * (one per char)
        - Preserves length info for debugging
        
        Example: API_KEY=abcdef123456 → API_KEY=ab**********56
        """
        sensitive_keywords = ['API', 'KEY', 'PASS', 'SECRET', 'TOKEN', 'AUTH']
        sanitized = {}
        
        for key, value in env.items():
            # Check if key contains sensitive keywords
            is_sensitive = any(keyword in key.upper() for keyword in sensitive_keywords)
            
            if is_sensitive and len(value) > 4:
                # Keep first 2 and last 2, hide middle
                sanitized[key] = value[:2] + ('*' * (len(value) - 4)) + value[-2:]
            else:
                sanitized[key] = value
        
        return sanitized
    
    def _spawn_elevated_bridge(self):
        """Spawn elevated process using TCP bridge (Phase 5K - All platforms!).
        
        Architecture:
        1. Create TCP listener on random port
        2. Generate authentication token
        3. Launch bridge script elevated (platform-specific: UAC/pkexec/osascript)
        4. Accept connection from bridge
        5. Verify token
        6. Bridge reads/writes to elevated process via TCP socket
        """
        import socket
        import secrets
        import subprocess
        import time
        import sys
        
        # Create TCP listener on random port (localhost only)
        self.elevated_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.elevated_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.elevated_listener.bind(('127.0.0.1', 0))  # Port 0 = random unused port
        self.bridge_port = self.elevated_listener.getsockname()[1]
        self.elevated_listener.listen(1)
        self.elevated_listener.settimeout(30.0)  # 30 second timeout for bridge to connect
        
        # Generate authentication token
        self.bridge_token = secrets.token_urlsafe(32)
        
        # Find bridge script using SharedConfigManager (handles .app bundles on macOS)
        from ragtag.shared_config import SharedConfigManager
        master_dir = SharedConfigManager()._find_master_directory()
        
        # In deployed environment, master_dir is the bin/ folder where bridge script lives
        # In development, master_dir is workspace root, so look in python_mcp/server/
        bridge_script = os.path.join(master_dir, "ragtag-bridge.py")
        
        if not os.path.exists(bridge_script):
            # Development fallback
            bridge_script = os.path.join(master_dir, "..", "python_mcp", "server", "ragtag-bridge.py")
            bridge_script = os.path.normpath(bridge_script)
        
        if not os.path.exists(bridge_script):
            raise TransportError(f"Bridge script not found. Tried: {master_dir}/ragtag-bridge.py and development path")
        
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Launching elevated bridge on port {self.bridge_port}")
        
        # Build bridge command args
        bridge_args = [
            "--port", str(self.bridge_port),
            "--token", self.bridge_token,
            "--command", self.command,
        ]
        if self.args:
            bridge_args.extend(["--args"] + self.args)
        if self.cwd:
            bridge_args.extend(["--cwd", self.cwd])
        
        # Platform-specific elevated launch
        if sys.platform == 'win32':
            self._launch_bridge_windows(bridge_script, bridge_args)
        elif sys.platform == 'darwin':
            self._launch_bridge_macos(bridge_script, bridge_args)
        else:
            self._launch_bridge_linux(bridge_script, bridge_args)
        
        # Accept connection from bridge (with timeout)
        try:
            MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] Waiting for bridge to connect...")
            self.elevated_sock, bridge_addr = self.elevated_listener.accept()
            self.elevated_sock.settimeout(None)  # Remove timeout after connected
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Bridge connected from {bridge_addr}")
        except socket.timeout:
            raise TransportError("Elevated bridge failed to connect (timeout after 30s). User may have declined UAC/authorization prompt.")
        finally:
            self.elevated_listener.close()
            self.elevated_listener = None
        
        # Verify authentication token
        try:
            received_token = self.elevated_sock.recv(len(self.bridge_token.encode()) + 1).decode().strip()
            if received_token != self.bridge_token:
                raise TransportError("Bridge authentication failed (token mismatch)")
            MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] Bridge authenticated successfully")
        except Exception as e:
            self.elevated_sock.close()
            self.elevated_sock = None
            raise TransportError(f"Bridge authentication failed: {e}")
    
    def _launch_bridge_windows(self, bridge_script, bridge_args):
        """Launch bridge on Windows with UAC prompt."""
        import subprocess
        import shlex
        import sys
        
        # Use PowerShell to launch elevated process with UAC
        # Start-Process -Verb RunAs triggers UAC prompt
        # Find aura.exe in same directory as bridge script
        bridge_dir = os.path.dirname(bridge_script)
        python_exe = os.path.join(bridge_dir, "python.exe")
        if not os.path.exists(python_exe):
            # Fallback to sys.executable (development)
            python_exe = sys.executable
        args_str = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in bridge_args)
        
        # Don't hide window during testing/debugging - user needs to see errors!
        # Note: Window will close automatically once bridge connects or fails
        ps_command = f'Start-Process -FilePath "{python_exe}" -ArgumentList \'"{bridge_script}",{args_str}\' -Verb RunAs'
        
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Launching Windows bridge via UAC...")
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Python: {python_exe}")
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Bridge: {bridge_script}")
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Args: {bridge_args}")
        
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] PowerShell launch command executed (waiting for UAC approval)")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] ERROR launching PowerShell: {e}")
            raise
    
    def _launch_bridge_linux(self, bridge_script, bridge_args):
        """Launch bridge on Linux with pkexec (GUI prompt) or sudo."""
        import subprocess
        import shutil
        import sys
        
        # Find aura in same directory as bridge script
        bridge_dir = os.path.dirname(bridge_script)
        python_exe = os.path.join(bridge_dir, "aura")
        if not os.path.exists(python_exe):
            # Fallback to sys.executable (development)
            python_exe = sys.executable or "python3"
        
        # Try pkexec first (GUI prompt, user-friendly)
        if shutil.which("pkexec"):
            MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] Launching Linux bridge via pkexec (GUI prompt)...")
            cmd = ["pkexec", python_exe, bridge_script] + bridge_args
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Fallback to sudo (will prompt in terminal if needed)
            MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] pkexec not found, using sudo...")
            cmd = ["sudo", python_exe, bridge_script] + bridge_args
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    def _launch_bridge_macos(self, bridge_script, bridge_args):
        """Launch bridge on macOS with osascript (GUI prompt)."""
        import subprocess
        import sys
        
        # Find aura in same directory as bridge script
        bridge_dir = os.path.dirname(bridge_script)
        python_exe = os.path.join(bridge_dir, "aura")
        if not os.path.exists(python_exe):
            # Fallback to sys.executable (development)
            python_exe = sys.executable or "python3"
        
        # Build command as a single shell string for osascript
        args_str = ' '.join(f'"{arg}"' for arg in bridge_args)
        shell_cmd = f'{python_exe} "{bridge_script}" {args_str}'
        
        MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] Launching macOS bridge via osascript (GUI prompt)...")
        
        # Use osascript to run with administrator privileges (triggers GUI password prompt)
        applescript = f'do shell script "{shell_cmd}" with administrator privileges'
        
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Launching macOS bridge via osascript with admin privileges")
        subprocess.Popen(
            ["osascript", "-e", applescript],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    
    def _spawn_posix(self):
        """Spawn process with PTY on POSIX systems (Linux/macOS/WSL).
        
        NOTE: Elevated sessions use _spawn_elevated_bridge() instead (Phase 5K).
        """
        import pty
        import os
        import fcntl
        import sys
        
        # Fork process with PTY
        pid, fd = pty.fork()
        
        if pid == 0:
            # Child process
            # Change working directory if specified
            if self.cwd:
                os.chdir(self.cwd)
            
            # Set environment variables if specified
            if self.env:
                os.environ.update(self.env)
            
            # Execute command (non-elevated)
            try:
                os.execlp(self.command, self.command, *self.args)
            except Exception as e:
                print(f"Failed to execute {self.command}: {e}", file=sys.stderr)
                os._exit(1)
        else:
            # Parent process
            self.process = pid
            self.pty_fd = fd
            
            # Set non-blocking mode
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] POSIX PTY created (fd={fd}, pid={pid})")
    
    def _spawn_windows(self):
        """Spawn process with ConPTY on Windows."""
        import shlex
        
        pywinpty = ensure_pywinpty()
        
        if pywinpty is None:
            raise TransportConnectionError("pywinpty not available (should have been auto-installed)")
        
        # Build command line (Windows style)
        # winpty.spawn() expects a single string, not a list
        if self.args:
            # Quote arguments that contain spaces
            quoted_args = [shlex.quote(arg) if ' ' in arg else arg for arg in self.args]
            cmdline = f"{self.command} {' '.join(quoted_args)}"
        else:
            cmdline = self.command
        
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Command line: {cmdline}")
        
        # Create PTY
        self.pty_master = pywinpty.PTY(self.cols, self.rows)
        
        # Spawn process
        # Note: spawn() takes a string command line, not a list
        try:
            self.process = self.pty_master.spawn(cmdline, cwd=self.cwd, env=self.env)
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Windows ConPTY created")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Failed to spawn with winpty: {e}")
            raise TransportConnectionError(f"Failed to spawn with winpty: {e}")
    
    def _get_pid(self) -> Optional[int]:
        """Get process ID (cross-platform)."""
        if self.is_windows:
            # pywinpty doesn't expose PID easily
            return None
        else:
            return self.process
    
    # ========================================================================
    # Core I/O Operations
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to program's stdin."""
        if not self.is_open():
            raise TransportConnectionError("Program transport is closed")
        
        try:
            if self.elevated_sock:
                # Phase 5K: Elevated session via TCP bridge
                self.elevated_sock.sendall(data)
                return len(data)
            elif self.is_windows:
                # Windows: write to PTY
                self.pty_master.write(data.decode('utf-8', errors='replace'))
                return len(data)
            else:
                # POSIX: write to PTY file descriptor
                import os
                written = os.write(self.pty_fd, data)
                return written
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Write error: {e}")
            raise TransportConnectionError(f"Failed to write to program: {e}")
    
    def read(self, size: int) -> bytes:
        """Read data from program's stdout (non-blocking).
        
        Returns:
            bytes: Data read (may be empty if no data available)
            
        Raises:
            TransportConnectionError: If process has exited or read fails
        """
        if not self.is_open():
            # Check if process exited
            exit_code = self._check_exit_code()
            if exit_code is not None:
                raise TransportConnectionError(f"Program exited with code {exit_code}")
            raise TransportConnectionError("Program transport is closed")
        
        try:
            if self.elevated_sock:
                # Phase 5K: Elevated session via TCP bridge (non-blocking)
                import socket
                self.elevated_sock.setblocking(False)
                try:
                    data = self.elevated_sock.recv(size)
                    if data == b'':
                        # EOF: bridge/process exited
                        raise TransportConnectionError("Elevated program exited")
                    return data
                except BlockingIOError:
                    # No data available
                    return b''
                except ConnectionResetError:
                    raise TransportConnectionError("Elevated bridge connection lost")
                finally:
                    self.elevated_sock.setblocking(True)
            elif self.is_windows:
                # Windows: read from PTY (non-blocking)
                try:
                    # winpty read() returns a string, not bytes
                    # Try non-blocking read first
                    data = self.pty_master.read()
                    if data:
                        return data.encode('utf-8', errors='replace')
                    return b''
                except Exception as e:
                    # Check if process exited
                    if hasattr(self.pty_master, 'isalive') and not self.pty_master.isalive():
                        self.exit_code = self._check_exit_code()
                        raise TransportConnectionError(f"Program exited with code {self.exit_code}")
                    # No data available or read error
                    return b''
            else:
                # POSIX: read from PTY file descriptor (non-blocking)
                import os
                try:
                    data = os.read(self.pty_fd, size)
                    if data == b'':
                        # EOF: process exited
                        self.exit_code = self._check_exit_code()
                        raise TransportConnectionError(f"Program exited with code {self.exit_code}")
                    return data
                except BlockingIOError:
                    # No data available (non-blocking mode)
                    return b''
                except OSError as e:
                    # Check if process exited
                    self.exit_code = self._check_exit_code()
                    if self.exit_code is not None:
                        raise TransportConnectionError(f"Program exited with code {self.exit_code}")
                    raise TransportConnectionError(f"Read error: {e}")
        except TransportConnectionError:
            raise
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Read error: {e}")
            raise TransportConnectionError(f"Failed to read from program: {e}")
    
    def _check_exit_code(self) -> Optional[int]:
        """Check if process has exited and return exit code."""
        if self.exit_code is not None:
            return self.exit_code
        
        if self.is_windows:
            # Windows: check if process is alive
            if self.pty_master and hasattr(self.pty_master, 'isalive'):
                if not self.pty_master.isalive():
                    # Try to get exit code
                    if hasattr(self.pty_master, 'exitstatus'):
                        self.exit_code = self.pty_master.exitstatus
                    else:
                        self.exit_code = -1  # Unknown
                    return self.exit_code
        else:
            # POSIX: check with waitpid (non-blocking)
            import os
            try:
                pid, status = os.waitpid(self.process, os.WNOHANG)
                if pid != 0:
                    # Process exited
                    if os.WIFEXITED(status):
                        self.exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        self.exit_code = -os.WTERMSIG(status)
                    else:
                        self.exit_code = -1
                    return self.exit_code
            except ChildProcessError:
                # Process already reaped
                self.exit_code = -1
                return self.exit_code
        
        return None
    
    def close(self) -> None:
        """Close program transport and terminate process."""
        if not self.is_open():
            return
        
        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Closing program transport")
        
        # Handle elevated session cleanup (Phase 5K)
        if self.elevated_sock:
            try:
                self.elevated_sock.close()
            except:
                pass
            self.elevated_sock = None
            if self.elevated_listener:
                try:
                    self.elevated_listener.close()
                except:
                    pass
                self.elevated_listener = None
            MCPLogger.log(TOOL_LOG_NAME, "[ProgramTransport] Elevated bridge closed")
            return
        
        # Terminate process gracefully, then forcefully if needed
        try:
            if self.is_windows:
                if self.pty_master:
                    # Send Ctrl+C (SIGINT equivalent)
                    try:
                        self.pty_master.write('\x03')  # ^C
                    except:
                        pass
                    
                    # Wait up to 5 seconds for graceful exit
                    import time
                    for _ in range(50):
                        if not self.pty_master.isalive():
                            break
                        time.sleep(0.1)
                    
                    # Force kill if still alive
                    if self.pty_master.isalive():
                        MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Force killing process")
                        try:
                            self.pty_master.terminate(force=True)
                        except:
                            pass
                    
                    self.pty_master = None
            else:
                # POSIX: send SIGTERM, then SIGKILL if needed
                import os
                import signal
                import time
                
                if self.process:
                    try:
                        # Send SIGTERM (graceful)
                        os.kill(self.process, signal.SIGTERM)
                        
                        # Wait up to 5 seconds
                        for _ in range(50):
                            pid, status = os.waitpid(self.process, os.WNOHANG)
                            if pid != 0:
                                break
                            time.sleep(0.1)
                        
                        # Force kill if still alive
                        try:
                            os.kill(self.process, signal.SIGKILL)
                            os.waitpid(self.process, 0)
                        except (ProcessLookupError, ChildProcessError):
                            pass
                    except (ProcessLookupError, ChildProcessError):
                        pass
                
                # Close PTY file descriptor
                if self.pty_fd is not None:
                    try:
                        os.close(self.pty_fd)
                    except OSError:
                        pass
                    self.pty_fd = None
            
            # Get final exit code
            self._check_exit_code()
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Process terminated (exit code: {self.exit_code})")
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[ProgramTransport] Error during close: {e}")
    
    def is_open(self) -> bool:
        """Check if program transport is open."""
        if self.elevated_sock:
            # Phase 5K: Check if elevated bridge socket is connected
            return self.elevated_sock is not None
        elif self.is_windows:
            return self.pty_master is not None
        else:
            return self.pty_fd is not None
    
    # ========================================================================
    # Unsupported Operations (no serial control lines)
    # ========================================================================
    
    def flush(self) -> None:
        """Flush buffers (no-op for programs)."""
        pass
    
    def bytes_available(self) -> int:
        """Return 0 (worker will try read anyway)."""
        return 0
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return program capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


# ============================================================================
# PHASE 5F: UNIX DOMAIN SOCKETS & NAMED PIPES TRANSPORT
# ============================================================================

class UnixSocketTransport(BaseTransport):
    """Transport for Unix Domain Sockets (Phase 5F).
    
    Connects to local services via Unix domain sockets (POSIX only).
    
    Use Cases:
    - Docker daemon (/var/run/docker.sock)
    - PostgreSQL, MySQL, Redis local sockets
    - systemd services
    - X11 display server
    - Any local IPC using Unix sockets
    
    Platform Support:
    - Linux: Full support
    - macOS: Full support
    - Windows: Not supported (use Named Pipes instead)
    
    Security:
    - Respects filesystem permissions on socket file
    - No network exposure (local-only communication)
    - Logs full socket path for audit trail
    """
    
    def __init__(self, socket_path: str, timeout: float = 10.0):
        """Initialize Unix socket transport.
        
        Args:
            socket_path: Path to Unix domain socket (e.g., /var/run/docker.sock)
            timeout: Connection timeout in seconds (default: 10.0)
            
        Raises:
            TransportConnectionError: Failed to connect to socket
        """
        import sys
        import socket as socket_module
        import os
        
        if sys.platform == 'win32':
            raise TransportConnectionError("Unix domain sockets are not supported on Windows (use Named Pipes instead)")
        
        self.socket_path = socket_path
        self.timeout = timeout
        self.sock = None
        
        MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Connecting to {socket_path}")
        
        # Check if socket file exists
        if not os.path.exists(socket_path):
            raise TransportConnectionError(f"Socket file does not exist: {socket_path}")
        
        # Check if it's a socket
        if not os.path.stat(socket_path).st_mode & 0o140000:  # S_IFSOCK
            raise TransportConnectionError(f"Path is not a socket: {socket_path}")
        
        # Connect to Unix socket
        try:
            self.sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(socket_path)
            
            # Set non-blocking mode after connection
            self.sock.setblocking(False)
            
            MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Connected to {socket_path}")
        except socket_module.timeout:
            raise TransportConnectionError(f"Connection timeout to {socket_path}")
        except socket_module.error as e:
            raise TransportConnectionError(f"Failed to connect to {socket_path}: {e}")
        except Exception as e:
            raise TransportConnectionError(f"Unexpected error connecting to {socket_path}: {e}")
    
    # ========================================================================
    # Core I/O Operations
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to Unix socket."""
        if not self.is_open():
            raise TransportConnectionError("Unix socket is closed")
        
        try:
            sent = self.sock.send(data)
            return sent
        except OSError as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Write error: {e}")
            raise TransportConnectionError(f"Failed to write to Unix socket: {e}")
    
    def read(self, size: int) -> bytes:
        """Read data from Unix socket (non-blocking).
        
        Returns:
            bytes: Data read (may be empty if no data available)
            
        Raises:
            TransportConnectionError: If socket is closed or read fails
        """
        if not self.is_open():
            raise TransportConnectionError("Unix socket is closed")
        
        try:
            data = self.sock.recv(size)
            if data == b'':
                # Empty read means connection closed
                raise TransportConnectionError("Unix socket closed by remote end")
            return data
        except BlockingIOError:
            # No data available (non-blocking mode)
            return b''
        except OSError as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Read error: {e}")
            raise TransportConnectionError(f"Failed to read from Unix socket: {e}")
    
    def close(self) -> None:
        """Close Unix socket connection."""
        if self.sock:
            MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Closing connection to {self.socket_path}")
            try:
                self.sock.close()
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"[UnixSocketTransport] Error during close: {e}")
            finally:
                self.sock = None
    
    def is_open(self) -> bool:
        """Check if Unix socket is open."""
        return self.sock is not None
    
    # ========================================================================
    # Unsupported Operations (no serial control lines)
    # ========================================================================
    
    def flush(self) -> None:
        """Flush buffers (no-op for sockets)."""
        pass
    
    def bytes_available(self) -> int:
        """Return 0 (worker will try read anyway)."""
        return 0
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return Unix socket capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


class NamedPipeTransport(BaseTransport):
    """Transport for Named Pipes (Windows) and FIFOs (POSIX) - Phase 5F.
    
    Connects to named pipes for inter-process communication.
    
    Platform-Specific Behavior:
    - Windows: Uses Named Pipes (\\.\pipe\name)
    - POSIX: Uses FIFOs (named pipes in filesystem)
    
    Use Cases:
    - Windows services and IPC
    - Legacy application integration
    - Cross-process communication
    - Named pipe servers
    
    Security:
    - Windows: Respects pipe security descriptors
    - POSIX: Respects filesystem permissions on FIFO
    - Logs full pipe path for audit trail
    """
    
    def __init__(self, pipe_path: str, timeout: float = 10.0, mode: str = "rw"):
        """Initialize named pipe transport.
        
        Args:
            pipe_path: Path to named pipe
                       Windows: \\.\pipe\name or \\server\pipe\name
                       POSIX: /path/to/fifo
            timeout: Connection timeout in seconds (default: 10.0)
            mode: Open mode - "r" (read), "w" (write), "rw" (read/write, default)
            
        Raises:
            TransportConnectionError: Failed to open pipe
        """
        import sys
        import os
        
        self.pipe_path = pipe_path
        self.timeout = timeout
        self.mode = mode
        self.is_windows = (sys.platform == 'win32')
        self.pipe_handle = None
        self.pipe_fd = None
        
        MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Opening pipe: {pipe_path} (mode: {mode})")
        
        try:
            if self.is_windows:
                self._open_windows_pipe()
            else:
                self._open_posix_fifo()
            
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Pipe opened successfully: {pipe_path}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Failed to open pipe: {e}")
            raise TransportConnectionError(f"Failed to open pipe '{pipe_path}': {e}")
    
    def _open_windows_pipe(self):
        """Open Windows Named Pipe."""
        import win32file
        import win32pipe
        import pywintypes
        
        # Determine access mode
        if self.mode == "r":
            access = win32file.GENERIC_READ
        elif self.mode == "w":
            access = win32file.GENERIC_WRITE
        else:  # "rw"
            access = win32file.GENERIC_READ | win32file.GENERIC_WRITE
        
        # Wait for pipe to become available
        try:
            win32pipe.WaitNamedPipe(self.pipe_path, int(self.timeout * 1000))
        except pywintypes.error as e:
            raise TransportConnectionError(f"Pipe not available: {e}")
        
        # Open pipe
        try:
            self.pipe_handle = win32file.CreateFile(
                self.pipe_path,
                access,
                0,  # No sharing
                None,  # Default security
                win32file.OPEN_EXISTING,
                0,  # No special attributes
                None
            )
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Windows pipe opened: {self.pipe_path}")
        except pywintypes.error as e:
            raise TransportConnectionError(f"Failed to open Windows pipe: {e}")
    
    def _open_posix_fifo(self):
        """Open POSIX FIFO (named pipe)."""
        import os
        import stat
        import fcntl
        
        # Check if FIFO exists
        if not os.path.exists(self.pipe_path):
            raise TransportConnectionError(f"FIFO does not exist: {self.pipe_path}")
        
        # Check if it's a FIFO
        if not stat.S_ISFIFO(os.stat(self.pipe_path).st_mode):
            raise TransportConnectionError(f"Path is not a FIFO: {self.pipe_path}")
        
        # Determine open mode
        if self.mode == "r":
            flags = os.O_RDONLY | os.O_NONBLOCK
        elif self.mode == "w":
            flags = os.O_WRONLY | os.O_NONBLOCK
        else:  # "rw"
            flags = os.O_RDWR | os.O_NONBLOCK
        
        # Open FIFO
        try:
            self.pipe_fd = os.open(self.pipe_path, flags)
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] POSIX FIFO opened: {self.pipe_path} (fd={self.pipe_fd})")
        except OSError as e:
            raise TransportConnectionError(f"Failed to open FIFO: {e}")
    
    # ========================================================================
    # Core I/O Operations
    # ========================================================================
    
    def write(self, data: bytes) -> int:
        """Write data to named pipe."""
        if not self.is_open():
            raise TransportConnectionError("Named pipe is closed")
        
        try:
            if self.is_windows:
                import win32file
                import pywintypes
                
                try:
                    result, written = win32file.WriteFile(self.pipe_handle, data)
                    return written
                except pywintypes.error as e:
                    raise TransportConnectionError(f"Failed to write to Windows pipe: {e}")
            else:
                import os
                written = os.write(self.pipe_fd, data)
                return written
        except TransportConnectionError:
            raise
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Write error: {e}")
            raise TransportConnectionError(f"Failed to write to named pipe: {e}")
    
    def read(self, size: int) -> bytes:
        """Read data from named pipe (non-blocking).
        
        Returns:
            bytes: Data read (may be empty if no data available)
            
        Raises:
            TransportConnectionError: If pipe is closed or read fails
        """
        if not self.is_open():
            raise TransportConnectionError("Named pipe is closed")
        
        try:
            if self.is_windows:
                import win32file
                import pywintypes
                
                try:
                    result, data = win32file.ReadFile(self.pipe_handle, size)
                    if data == b'':
                        # Empty read means pipe closed
                        raise TransportConnectionError("Named pipe closed by remote end")
                    return data
                except pywintypes.error as e:
                    # ERROR_NO_DATA (232) means no data available (non-blocking)
                    if e.winerror == 232:
                        return b''
                    raise TransportConnectionError(f"Failed to read from Windows pipe: {e}")
            else:
                import os
                try:
                    data = os.read(self.pipe_fd, size)
                    if data == b'':
                        # Empty read means pipe closed
                        raise TransportConnectionError("FIFO closed by remote end")
                    return data
                except BlockingIOError:
                    # No data available (non-blocking mode)
                    return b''
                except OSError as e:
                    raise TransportConnectionError(f"Failed to read from FIFO: {e}")
        except TransportConnectionError:
            raise
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Read error: {e}")
            raise TransportConnectionError(f"Failed to read from named pipe: {e}")
    
    def close(self) -> None:
        """Close named pipe."""
        MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Closing pipe: {self.pipe_path}")
        
        try:
            if self.is_windows:
                if self.pipe_handle:
                    import win32file
                    win32file.CloseHandle(self.pipe_handle)
                    self.pipe_handle = None
            else:
                if self.pipe_fd is not None:
                    import os
                    os.close(self.pipe_fd)
                    self.pipe_fd = None
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Error during close: {e}")
    
    def is_open(self) -> bool:
        """Check if named pipe is open."""
        if self.is_windows:
            return self.pipe_handle is not None
        else:
            return self.pipe_fd is not None
    
    # ========================================================================
    # Unsupported Operations (no serial control lines)
    # ========================================================================
    
    def flush(self) -> None:
        """Flush buffers."""
        if not self.is_open():
            return
        
        try:
            if self.is_windows:
                import win32file
                if self.pipe_handle:
                    win32file.FlushFileBuffers(self.pipe_handle)
            else:
                # POSIX FIFOs don't need explicit flushing
                pass
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"[NamedPipeTransport] Flush error: {e}")
    
    def bytes_available(self) -> int:
        """Return 0 (worker will try read anyway)."""
        return 0
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Return named pipe capabilities (no serial features)."""
        return {
            "dtr_rts": False,
            "line_states": False,
            "break_signal": False,
            "baud_rate": False,
            "flow_control": False
        }


# ============================================================================
# SESSION DATA STRUCTURES
# ============================================================================

@dataclass
class terminal_session_metadata:
    """Metadata about a terminal session"""
    session_id: str
    session_start_time: datetime
    log_file_path: Path
    
    # Transport info (filled in Phase 2)
    transport_type: str  # "serial" | "tcp" | "websocket" (Phase 5)
    endpoint: str  # "COM3" or "192.168.1.123:23"
    
    # Statistics
    total_bytes_received: int = 0
    total_bytes_sent: int = 0
    bytes_per_second_average: float = 0.0
    
    # Log file tracking
    current_log_offset_bytes: int = 0
    total_lines_written: int = 0
    log_file_size_bytes: int = 0
    
    # Session state
    is_active: bool = True
    last_activity_time: datetime = None
    
    def __post_init__(self):
        if self.last_activity_time is None:
            self.last_activity_time = self.session_start_time

# ============================================================================
# ASYNC OPERATION TRACKING (Phase 2C)
# ============================================================================

@dataclass
class async_operation_state:
    """Track state of an async operation (send_async, receive_to_file, etc.)"""
    operation_id: str
    operation_type: str  # "send_async", "receive_to_file"
    start_time: datetime
    session_id: str
    
    # Status tracking
    status: str = "pending"  # pending|in_progress|completed|cancelled|error
    error_message: Optional[str] = None
    
    # Progress tracking (for sends)
    total_bytes: int = 0
    bytes_processed: int = 0
    
    # Source/destination
    source_file_path: Optional[Path] = None
    destination_file_path: Optional[Path] = None
    inline_data: Optional[bytes] = None
    
    # Timing
    end_time: Optional[datetime] = None
    
    def get_percent_complete(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return (self.bytes_processed / self.total_bytes) * 100.0
    
    def get_elapsed_seconds(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return (datetime.now() - self.start_time).total_seconds()
    
    def get_eta_seconds(self) -> Optional[float]:
        if self.bytes_processed == 0 or self.total_bytes == 0:
            return None
        elapsed = self.get_elapsed_seconds()
        if elapsed == 0:
            return None
        bytes_per_second = self.bytes_processed / elapsed
        remaining_bytes = self.total_bytes - self.bytes_processed
        return remaining_bytes / bytes_per_second if bytes_per_second > 0 else None
    
    def to_dict(self) -> Dict:
        result = {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "status": self.status,
            "percent_complete": round(self.get_percent_complete(), 2),
            "bytes_processed": self.bytes_processed,
            "total_bytes": self.total_bytes,
            "elapsed_seconds": round(self.get_elapsed_seconds(), 2),
        }
        
        eta = self.get_eta_seconds()
        if eta is not None:
            result["eta_seconds"] = round(eta, 2)
        
        if self.error_message:
            result["error_message"] = self.error_message
        
        if self.source_file_path:
            result["source_file"] = str(self.source_file_path)
        
        if self.destination_file_path:
            result["destination_file"] = str(self.destination_file_path)
        
        return result

@dataclass
class session_container_with_log_file:
    """Complete session container with log file handle and metadata"""
    metadata: terminal_session_metadata
    log_file_handle: Optional[object] = None  # File handle for writing
    
    # Phase 5A1: Transport abstraction (unified interface for serial/network)
    # This is the PRIMARY way to interact with the connection!
    transport: Optional[BaseTransport] = None  # BaseTransport instance (serial, TCP, SSH, etc.)
    
    # Phase 2D: Single worker thread architecture (expert pattern)
    # ONE thread owns the serial port for its entire lifetime
    # NOTE: serial_port kept temporarily during Phase 5A1 migration, will be removed after
    serial_port: Optional[object] = None  # serial.Serial instance (DEPRECATED: use transport)
    worker_thread: Optional[threading.Thread] = None  # Single worker owns port
    worker_stop_event: Optional[threading.Event] = None  # Signal to stop worker
    
    # Command queue: MCP thread -> Worker thread
    command_queue: Optional[queue.Queue] = None  # Commands to worker
    
    # Response queue: Worker thread -> MCP thread (for synchronous operations)
    response_queue: Optional[queue.Queue] = None  # Responses from worker
    
    # Output queue: Worker thread -> MCP thread (for continuous reads)
    output_queue: Optional[queue.Queue] = None  # Incoming serial data
    
    # Async operations tracking (Phase 2C, kept in Phase 2D)
    async_operations: Dict[str, async_operation_state] = None  # Track async operations
    async_operations_lock: Optional[threading.Lock] = None  # Lock for async operations dict
    
    # Metadata protection (Phase 2D thread safety)
    metadata_lock: Optional[threading.Lock] = None  # Lock for metadata updates
    
    # Phase 3: Rich command sequences with atomic execution
    worker_state: str = "idle"  # "idle" | "executing_sequence"
    current_sequence: Optional[Dict] = None  # Active sequence being executed
    active_sequences: Dict[str, Dict] = None  # Async sequences (fire-and-forget)
    
    # Phase 3: Terminal emulation (auto-respond to ANSI queries)
    terminal_emulation_enabled: bool = False  # OFF by default (user decision)
    terminal_size: Dict = None  # {"rows": 24, "cols": 80}
    ansi_carry: bytearray = None  # Buffer for ANSI sequences straddling chunks (MUST-DO #3)
    
    def __post_init__(self):
        if self.async_operations is None:
            self.async_operations = {}
        if self.async_operations_lock is None:
            self.async_operations_lock = threading.Lock()
        if self.metadata_lock is None:
            self.metadata_lock = threading.Lock()
        
        # Phase 3: Initialize sequence tracking
        if self.active_sequences is None:
            self.active_sequences = {}
        
        # Phase 3: Initialize terminal emulation
        if self.terminal_size is None:
            self.terminal_size = {"rows": 24, "cols": 80}
        if self.ansi_carry is None:
            self.ansi_carry = bytearray()

# ============================================================================
# GLOBAL SESSION MANAGEMENT
# ============================================================================

# Session cache (thread-safe)
_active_sessions_cache: Dict[str, session_container_with_log_file] = {}
_session_cache_lock = threading.Lock()
_next_session_id = 1

# Main thread queue support (from python.py pattern)
# This will be used in Phase 2 for serial port operations
_main_thread_queue = None  # Set by server if available

# ============================================================================
# LOG FILE MANAGEMENT
# ============================================================================

def get_terminal_logs_directory() -> Path:
    """Get the directory where terminal logs are stored"""
    logs_dir = get_user_data_directory() / "terminal_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir

def create_log_file_for_session(session_id: str) -> Tuple[Path, object]:
    """
    Create a new log file for a session.
    
    Args:
        session_id: Unique session identifier
        
    Returns:
        Tuple of (log_file_path, file_handle)
    """
    logs_dir = get_terminal_logs_directory()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"session_{session_id}_{timestamp}.log"
    log_path = logs_dir / log_filename
    
    # Open in binary mode for exact byte recording
    file_handle = open(log_path, 'wb', buffering=0)  # Unbuffered for real-time
    
    MCPLogger.log(TOOL_LOG_NAME, f"Created log file: {log_path}")
    
    return log_path, file_handle

def close_log_file(session: session_container_with_log_file):
    """Safely close a session's log file"""
    if session.log_file_handle:
        try:
            session.log_file_handle.flush()
            session.log_file_handle.close()
            MCPLogger.log(TOOL_LOG_NAME, f"Closed log file: {session.metadata.log_file_path}")
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error closing log file: {e}")
        finally:
            session.log_file_handle = None

# ============================================================================
# CONTROL CHARACTER PARSING (Phase 2A)
# ============================================================================

def parse_control_characters(data: str) -> bytes:
    """
    Parse control characters from various formats into bytes.
    
    Supports:
    - Caret notation: ^C, ^D, ^E, etc. (converts to ASCII control codes)
    - Hex escapes: \\x03, \\x04, etc. (single byte)
    - Unicode escapes: \\u0041, \\u00E9, etc. (UTF-8 encoded)
    - Common escapes: \\r, \\n, \\t, \\\\, \\^
    - Regular text
    
    Examples:
        "^C" -> b"\\x03"
        "hello^M^J" -> b"hello\\r\\n"
        "\\x1b[2J" -> b"\\x1b[2J"
        "import sys\\r\\n" -> b"import sys\\r\\n"
        "\\^C" -> b"^C" (literal caret + C)
        "\\u00E9" -> b"\\xc3\\xa9" (é in UTF-8)
    
    Args:
        data: String containing text and/or control character sequences
        
    Returns:
        Byte sequence with control characters converted
    """
    if not data:
        return b''
    
    result = bytearray()
    i = 0
    
    while i < len(data):
        # Check for caret notation (^C, ^D, etc.)
        if data[i] == '^' and i + 1 < len(data):
            next_char = data[i + 1]
            # Convert ^A-^Z to ASCII 1-26, ^[, ^\\, ^], ^^, ^_, ^? to their codes
            if 'A' <= next_char <= 'Z':
                result.append(ord(next_char) - ord('A') + 1)
                i += 2
                continue
            elif 'a' <= next_char <= 'z':  # Also support lowercase
                result.append(ord(next_char) - ord('a') + 1)
                i += 2
                continue
            elif next_char == '[':  # ESC
                result.append(27)
                i += 2
                continue
            elif next_char == '\\':
                result.append(28)
                i += 2
                continue
            elif next_char == ']':
                result.append(29)
                i += 2
                continue
            elif next_char == '^':
                result.append(30)
                i += 2
                continue
            elif next_char == '_':
                result.append(31)
                i += 2
                continue
            elif next_char == '?':
                result.append(127)  # DEL
                i += 2
                continue
        
        # Check for hex escape (\\xNN)
        if data[i] == '\\' and i + 3 < len(data) and data[i+1] == 'x':
            try:
                hex_str = data[i+2:i+4]
                byte_val = int(hex_str, 16)
                result.append(byte_val)
                i += 4
                continue
            except ValueError:
                pass  # Not a valid hex escape, treat as regular character
        
        # Check for Unicode escape (\\uNNNN)
        if data[i] == '\\' and i + 5 < len(data) and data[i+1] == 'u':
            try:
                hex_str = data[i+2:i+6]
                code_point = int(hex_str, 16)
                # Convert Unicode code point to UTF-8 bytes
                char = chr(code_point)
                result.extend(char.encode('utf-8'))
                i += 6
                continue
            except (ValueError, OverflowError):
                pass  # Not a valid Unicode escape, treat as regular character
        
        # Check for common escape sequences
        if data[i] == '\\' and i + 1 < len(data):
            next_char = data[i + 1]
            if next_char == 'r':
                result.append(13)  # CR
                i += 2
                continue
            elif next_char == 'n':
                result.append(10)  # LF
                i += 2
                continue
            elif next_char == 't':
                result.append(9)  # TAB
                i += 2
                continue
            elif next_char == '\\':
                result.append(ord('\\'))
                i += 2
                continue
            elif next_char == '^':
                result.append(ord('^'))  # Literal caret
                i += 2
                continue
        
        # Regular character
        result.append(ord(data[i]))
        i += 1
    
    return bytes(result)

# ============================================================================
# MAGIC BAUD BOOTLOADER REQUIREMENTS (Phase 6/7 - Future Firmware Flashing)
# ============================================================================
#
# Many MCU families use "magic baud" tricks for bootloader entry where the
# REQUESTED baud rate (or specific baud change patterns) trigger programming mode.
#
# CURRENT ARCHITECTURE SUPPORT (Phase 2C):
#   ✓ set_baud operation (runtime baud switching)
#   ✓ DTR/RTS control at open_session
#   ✓ Control character/pattern sending
#   ✓ Precise timing support planned (Phase 3: send_sequence)
#
# MISSING FOR FULL MAGIC BAUD SUPPORT (Phase 3):
#   ✗ Port close/reopen actions within sequences
#   ✗ Bootloader recipe system
#
# PHASE 3 REQUIREMENTS for Magic Baud:
#   When implementing send_sequence, add these actions:
#   - "set_baud": Change baud mid-sequence (ALREADY SUPPORTED via set_baud op)
#   - "set_dtr": Toggle DTR mid-sequence
#   - "set_rts": Toggle RTS mid-sequence
#   - "close_port": Close port, optionally preserve session (keep_session: bool)
#   - "reopen_port": Reopen with new config (baud_rate, set_dtr, set_rts)
#   - "wait": Precise delays (already planned)
#   - "send": Send data/patterns (already planned)
#
# EXAMPLE MAGIC BAUD PATTERNS:
#
# 1. AVR/Arduino (1200 baud soft reset):
#    - Set baud to 1200
#    - Set DTR=False
#    - Close port (keep_session=True)
#    - Wait 0.3s
#    - Reopen at 115200 baud
#
# 2. STM32 (0x7F autobaud):
#    - Set baud to 9600
#    - Send 0x7F byte
#    - Wait 0.1s
#    - Read response (expect ACK)
#
# 3. ESP32/ESP8266 (autobaud sync):
#    - Set DTR=False, RTS=True
#    - Wait 0.05s
#    - Set DTR=True, RTS=False
#    - Wait 0.05s
#    - Send pattern: 0x07 0x07 0x12 0x20 0x55...
#    - Wait for response
#    - Switch to high-speed baud (921600)
#
# 4. Teensy/NXP Kinetis (134 baud trigger):
#    - Open at 134 baud
#    - Toggle RTS
#    - Reopen at normal speed
#
# 5. Nordic nRF52 (1200 baud trigger):
#    - Open at 1200 baud
#    - Or send CDC control request
#    - Triggers UF2 bootloader
#
# IMPLEMENTATION STRATEGY (Phase 6+):
#   Create a bootloader recipe library with pre-defined sequences:
#   BOOTLOADER_RECIPES = {
#       "avr_1200_reset": { "sequence": [...], "params": ["target_baud"] },
#       "stm32_0x7f_autobaud": { "sequence": [...] },
#       "esp32_autobaud_sync": { "sequence": [...] },
#       ...
#   }
#
# ARCHITECTURE NOTE:
#   The current Phase 2C implementation already supports everything needed
#   EXCEPT port close/reopen within sequences. This is a minor extension
#   to the planned Phase 3 send_sequence operation.
#
# ============================================================================
# SERIAL PORT OPERATIONS (Phase 2A)
# ============================================================================

def list_available_serial_ports() -> List[Dict]:
    """
    List all available serial ports on the system.
    
    Returns:
        List of dicts with port info: device, description, hwid
    """
    try:
        serial, serial_tools_list_ports = ensure_pyserial()
        
        ports = serial_tools_list_ports.comports()
        port_list = []
        
        for port in ports:
            port_info = {
                "device": port.device,
                "description": port.description or "N/A",
                "hwid": port.hwid or "N/A",
                "manufacturer": getattr(port, 'manufacturer', 'N/A') or 'N/A',
                "product": getattr(port, 'product', 'N/A') or 'N/A',
                "serial_number": getattr(port, 'serial_number', 'N/A') or 'N/A'
            }
            port_list.append(port_info)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Found {len(port_list)} serial ports")
        return port_list
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error listing serial ports: {e}")
        return []


def discover_network_devices(service_types: Optional[List[str]] = None, timeout_seconds: float = 5.0) -> List[Dict]:
    """
    Discover devices on local network via mDNS/DNS-SD (Phase 5D).
    
    Uses zeroconf library to find devices advertising services like telnet, SSH, Arduino, etc.
    Similar to how Arduino IDE discovers network-enabled boards.
    
    Args:
        service_types: List of DNS-SD service types to search for.
                      Default: ['_telnet._tcp.local.', '_ssh._tcp.local.', '_arduino._tcp.local.', '_http._tcp.local.']
        timeout_seconds: How long to listen for mDNS responses (default 5.0 seconds)
    
    Returns:
        List of discovered devices with format:
        [
            {
                "name": "Esp32Lcd06",
                "hostname": "Esp32Lcd06.local",
                "ip": "172.22.1.103",
                "port": 23,
                "service": "telnet",
                "service_type": "_telnet._tcp.local."
            },
            ...
        ]
    
    Platform Notes:
        - Windows: Requires Bonjour service (install iTunes or Bonjour Print Services)
        - Linux/WSL: Works out-of-box with Avahi daemon
        - macOS: Works out-of-box with native mDNS support
    
    Example:
        # Discover all common embedded device services
        devices = discover_network_devices()
        
        # Discover only SSH servers
        ssh_devices = discover_network_devices(['_ssh._tcp.local.'])
    """
    try:
        zeroconf_module = ensure_zeroconf()
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        
        # Default service types to search for
        if service_types is None:
            service_types = [
                '_telnet._tcp.local.',  # Telnet servers (ESP32, etc.)
                '_ssh._tcp.local.',     # SSH servers (Raspberry Pi, etc.)
                '_arduino._tcp.local.', # Arduino/ESP32 devices
                '_http._tcp.local.',    # Web servers
            ]
        
        MCPLogger.log(TOOL_LOG_NAME, f"Starting mDNS discovery for services: {service_types}")
        MCPLogger.log(TOOL_LOG_NAME, f"Listening for {timeout_seconds} seconds...")
        
        discovered_devices = []
        
        class DeviceListener(ServiceListener):
            """Listener that collects discovered services."""
            
            def add_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
                """Called when a service is discovered."""
                try:
                    info = zc.get_service_info(service_type, name)
                    if info:
                        # Parse service name (remove service type suffix)
                        device_name = name.replace(f'.{service_type}', '')
                        
                        # Get IP addresses (zeroconf returns list of bytes, need to convert)
                        ip_addresses = []
                        if hasattr(info, 'parsed_addresses'):
                            ip_addresses = info.parsed_addresses()
                        elif hasattr(info, 'addresses'):
                            # Older zeroconf versions
                            import socket
                            for addr in info.addresses:
                                try:
                                    ip_addresses.append(socket.inet_ntoa(addr))
                                except:
                                    pass
                        
                        # Extract service name (telnet, ssh, etc.)
                        service_name = service_type.replace('_', '').replace('.local.', '').replace('.tcp', '')
                        
                        # Build device info
                        for ip in ip_addresses:
                            device_info = {
                                "name": device_name,
                                "hostname": f"{device_name}.local",
                                "ip": ip,
                                "port": info.port,
                                "service": service_name,
                                "service_type": service_type
                            }
                            discovered_devices.append(device_info)
                            MCPLogger.log(TOOL_LOG_NAME, f"Discovered: {device_name} ({service_name}) at {ip}:{info.port}")
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Error processing service {name}: {e}")
            
            def remove_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
                """Called when a service disappears (ignored during discovery)."""
                pass
            
            def update_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
                """Called when service info changes (ignored during discovery)."""
                pass
        
        # Create zeroconf instance and listener
        zc = Zeroconf()
        listener = DeviceListener()
        
        try:
            # Start browsing for each service type
            browsers = []
            for service_type in service_types:
                browser = ServiceBrowser(zc, service_type, listener)
                browsers.append(browser)
            
            # Wait for timeout period
            time.sleep(timeout_seconds)
            
            # Clean up
            for browser in browsers:
                browser.cancel()
            
        finally:
            zc.close()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Discovery complete. Found {len(discovered_devices)} device(s)")
        return discovered_devices
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error during network discovery: {e}")
        # Return helpful error info
        return []


# ============================================================================
# PHASE 3: TERMINAL EMULATION
# ============================================================================

def intercept_ansi_queries(data: bytes, carry: bytearray, terminal_size: Dict) -> Tuple[bytes, List[bytes]]:
    """
    Intercept ANSI terminal queries and generate auto-responses (Phase 3).
    
    MUST-DO #3: Handles ANSI sequences that straddle read boundaries using carry buffer.
    
    Args:
        data: Incoming data from serial port
        carry: Buffer containing incomplete ANSI sequence from previous read
        terminal_size: Dict with 'rows' and 'cols'
    
    Returns:
        (cleaned_data, list_of_responses)
        cleaned_data: Data with ANSI queries removed
        responses: List of ANSI responses to send back to device
    """
    buf = bytes(carry) + data
    out = bytearray()
    responses = []
    i = 0
    
    while i < len(buf):
        if buf[i] != 0x1b:  # Not ESC
            out.append(buf[i])
            i += 1
            continue
        
        # Check if we have enough bytes to parse query
        # Cursor position query: ESC[6n (4 bytes)
        if i + 3 < len(buf) and buf[i:i+4] == b'\x1b[6n':
            row = terminal_size.get('rows', 24)
            col = terminal_size.get('cols', 80)
            responses.append(f'\x1b[{row};{col}R'.encode())
            i += 4
            continue
        
        # Terminal size query: ESC[18t (5 bytes)
        if i + 4 < len(buf) and buf[i:i+5] == b'\x1b[18t':
            row = terminal_size.get('rows', 24)
            col = terminal_size.get('cols', 80)
            responses.append(f'\x1b[8;{row};{col}t'.encode())
            i += 5
            continue
        
        # Incomplete sequence at end of buffer?
        if i + 3 >= len(buf):
            # Save for next chunk (could be start of query)
            break
        
        # Unknown escape: pass through
        out.append(buf[i])
        i += 1
    
    # Update carry buffer with unparsed remainder
    carry.clear()
    if i < len(buf):
        carry.extend(buf[i:])
    
    return bytes(out), responses

# ============================================================================
# PHASE 3: SEQUENCE MANAGEMENT
# ============================================================================

def initialize_sequence(sequence_id: str, actions: List[Dict], options: Dict) -> Dict:
    """
    Initialize a sequence for execution (Phase 3).
    
    Args:
        sequence_id: Unique identifier for this sequence
        actions: List of action dictionaries
        options: Sequence options (stop_on_error, timeout, etc.)
    
    Returns:
        Sequence state dictionary
    """
    return {
        "sequence_id": sequence_id,
        "actions": actions,
        "current_action_index": 0,
        "sequence_start_time": time.time(),
        "sequence_timeout": options.get("timeout", 60.0),
        "stop_on_error": options.get("stop_on_error", True),
        "accumulated_data": bytearray(),  # For wait_for actions
        "results": [],  # Results from each action
        "status": "in_progress"
    }

def finish_sequence_success(session: 'session_container_with_log_file'):
    """Mark sequence as successfully completed (Phase 3)"""
    if not session.current_sequence:
        return
    
    seq = session.current_sequence
    elapsed = time.time() - seq["sequence_start_time"]
    
    result = {
        "success": True,
        "sequence_id": seq["sequence_id"],
        "status": "success",
        "actions_completed": len(seq["results"]),
        "actions_total": len(seq["actions"]),
        "elapsed_seconds": round(elapsed, 3),
        "results": seq["results"]
    }
    
    # If async, store in active_sequences; if blocking, put on response_queue
    if seq.get("async", False):
        session.active_sequences[seq["sequence_id"]] = result
    else:
        session.response_queue.put(("sequence_success", result))
    
    # Reset worker state
    session.worker_state = "idle"
    MCPLogger.log(TOOL_LOG_NAME, f"Session {session.metadata.session_id} worker_state -> idle (sequence success)")
    session.current_sequence = None
    
    MCPLogger.log(TOOL_LOG_NAME, f"Sequence {seq['sequence_id']} completed successfully")

def finish_sequence_with_error(session: 'session_container_with_log_file', error_message: str):
    """Mark sequence as failed (Phase 3)"""
    if not session.current_sequence:
        return
    
    seq = session.current_sequence
    elapsed = time.time() - seq["sequence_start_time"]
    
    result = {
        "success": False,
        "sequence_id": seq["sequence_id"],
        "status": "error",
        "error_message": error_message,
        "actions_completed": len(seq["results"]),
        "actions_total": len(seq["actions"]),
        "error_at_action": seq["current_action_index"],
        "elapsed_seconds": round(elapsed, 3),
        "results": seq["results"]
    }
    
    # If async, store in active_sequences; if blocking, put on response_queue
    if seq.get("async", False):
        session.active_sequences[seq["sequence_id"]] = result
    else:
        session.response_queue.put(("sequence_error", result))
    
    # Reset worker state
    session.worker_state = "idle"
    MCPLogger.log(TOOL_LOG_NAME, f"Session {session.metadata.session_id} worker_state -> idle (sequence error)")
    session.current_sequence = None
    
    MCPLogger.log(TOOL_LOG_NAME, f"Sequence {seq['sequence_id']} failed: {error_message}")

def finish_sequence_cancelled(session: 'session_container_with_log_file', reason: str):
    """Mark sequence as cancelled (Phase 3, MUST-DO #4)"""
    if not session.current_sequence:
        return
    
    seq = session.current_sequence
    elapsed = time.time() - seq["sequence_start_time"]
    
    result = {
        "success": False,
        "sequence_id": seq["sequence_id"],
        "status": "cancelled",
        "cancel_reason": reason,
        "actions_completed": len(seq["results"]),
        "actions_total": len(seq["actions"]),
        "elapsed_seconds": round(elapsed, 3),
        "results": seq["results"]
    }
    
    # If async, store in active_sequences; if blocking, put on response_queue
    if seq.get("async", False):
        session.active_sequences[seq["sequence_id"]] = result
    else:
        session.response_queue.put(("sequence_cancelled", result))
    
    # Reset worker state
    session.worker_state = "idle"
    MCPLogger.log(TOOL_LOG_NAME, f"Session {session.metadata.session_id} worker_state -> idle (sequence cancelled)")
    session.current_sequence = None
    
    MCPLogger.log(TOOL_LOG_NAME, f"Sequence {seq['sequence_id']} cancelled: {reason}")

# ============================================================================
# PHASE 3: SEQUENCE ACTION EXECUTORS
# ============================================================================

def execute_send_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'send' action - send data via transport (Phase 3, updated Phase 5A1).
    
    Completes immediately, moves to next action.
    Phase 5A1: Uses session.transport instead of session.serial_port
    """
    try:
        data = action.get("data", "")
        bytes_to_send = parse_control_characters(data)
        
        session.transport.write(bytes_to_send)
        
        # Update metadata
        with session.metadata_lock:
            session.metadata.total_bytes_sent += len(bytes_to_send)
            session.metadata.last_activity_time = datetime.now()
        
        session.current_sequence["results"].append({
            "action": "send",
            "status": "success",
            "bytes_sent": len(bytes_to_send)
        })
        
        # Move to next action immediately
        session.current_sequence["current_action_index"] += 1
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "send",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"send action failed: {e}")

def execute_wait_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'wait' action - wait for specific duration (Phase 3).
    
    Non-blocking: Uses timestamps to check completion.
    """
    # First call: Initialize wait start time
    if "wait_start_time" not in action:
        action["wait_start_time"] = time.time()
        return  # Don't move to next action yet
    
    # Subsequent calls: Check if wait completed
    elapsed = time.time() - action["wait_start_time"]
    if elapsed >= action.get("seconds", 0):
        # Wait completed!
        session.current_sequence["results"].append({
            "action": "wait",
            "status": "success",
            "elapsed": round(elapsed, 3)
        })
        session.current_sequence["current_action_index"] += 1

def execute_wait_for_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'wait_for' action - wait for pattern in serial data (Phase 3).
    
    Data accumulation happens in main worker loop.
    This function just checks if pattern found or timeout.
    
    MUST-DO #2: Uses rolling window to prevent memory explosion.
    """
    # First call: Initialize
    if "wait_for_start_time" not in action:
        action["wait_for_start_time"] = time.time()
        # BUG #1 FIX: Do NOT clear accumulated_data here!
        # We want bytes that arrived during prior actions (send, wait, etc.)
        return
    
    # Check timeout
    elapsed = time.time() - action["wait_for_start_time"]
    timeout = action.get("timeout", 5.0)
    
    if elapsed >= timeout:
        # Timeout! Pattern not found
        session.current_sequence["results"].append({
            "action": "wait_for",
            "status": "error",
            "pattern_found": False,
            "timeout": True,
            "elapsed": round(elapsed, 3),
            "data": session.current_sequence["accumulated_data"].decode('utf-8', errors='replace')
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(
                session, 
                f"wait_for timeout: pattern '{action.get('pattern')}' not found in {timeout}s"
            )
        else:
            session.current_sequence["current_action_index"] += 1
        return
    
    # Check if pattern found
    pattern = action.get("pattern", "")
    pattern_bytes = pattern.encode('utf-8')
    
    if pattern_bytes in session.current_sequence["accumulated_data"]:
        # Pattern found!
        session.current_sequence["results"].append({
            "action": "wait_for",
            "status": "success",
            "pattern_found": True,
            "elapsed": round(elapsed, 3),
            "data": session.current_sequence["accumulated_data"].decode('utf-8', errors='replace')
        })
        session.current_sequence["current_action_index"] += 1
        session.current_sequence["accumulated_data"].clear()

def execute_set_dtr_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'set_dtr' action - set DTR line state (Phase 3, updated Phase 5A1).
    
    MUST-DO #5: Auto-stabilization delay (50ms) before next action.
    Phase 5A1: Uses session.transport, gracefully fails for network transports
    """
    try:
        value = action.get("value", False)
        session.transport.set_dtr(value)
        
        # MUST-DO #5: Auto-stabilization delay
        action["_auto_delay_until"] = time.time() + 0.05  # 50ms
        
        session.current_sequence["results"].append({
            "action": "set_dtr",
            "status": "success",
            "value": value
        })
        
        # Don't move to next action yet - delay will be checked in process_sequence_actions
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "set_dtr",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"set_dtr failed: {e}")

def execute_set_rts_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'set_rts' action - set RTS line state (Phase 3, updated Phase 5A1).
    
    MUST-DO #5: Auto-stabilization delay (50ms) before next action.
    Phase 5A1: Uses session.transport, gracefully fails for network transports
    """
    try:
        value = action.get("value", False)
        session.transport.set_rts(value)
        
        # MUST-DO #5: Auto-stabilization delay
        action["_auto_delay_until"] = time.time() + 0.05  # 50ms
        
        session.current_sequence["results"].append({
            "action": "set_rts",
            "status": "success",
            "value": value
        })
        
        # Don't move to next action yet - delay will be checked in process_sequence_actions
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "set_rts",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"set_rts failed: {e}")

def execute_set_baud_action(session: 'session_container_with_log_file', action: Dict):
    """
    Execute 'set_baud' action - change baud rate (Phase 3, updated Phase 5A1).
    
    MUST-DO #5: Auto-stabilization delay (100ms) before next action.
    Phase 5A1: Uses session.transport, gracefully fails for network transports
    """
    try:
        baud_rate = action.get("baud_rate", 115200)
        session.transport.set_baud_rate(baud_rate)
        
        # MUST-DO #5: Baud changes need longer stabilization
        action["_auto_delay_until"] = time.time() + 0.1  # 100ms
        
        session.current_sequence["results"].append({
            "action": "set_baud",
            "status": "success",
            "baud_rate": baud_rate
        })
        
        # Don't move to next action yet - delay will be checked in process_sequence_actions
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "set_baud",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"set_baud failed: {e}")

def execute_send_break_action(session: 'session_container_with_log_file', action: Dict):
    """Execute 'send_break' action - send BREAK signal (Phase 3, updated Phase 5A1)
    
    Phase 5A1: Uses session.transport, gracefully fails for network transports
    """
    try:
        duration = action.get("duration", 0.25)
        
        # Phase 5A1: Graceful error handling via transport abstraction
        session.transport.send_break(duration=duration)
        
        session.current_sequence["results"].append({
            "action": "send_break",
            "status": "success",
            "duration": duration
        })
        
        session.current_sequence["current_action_index"] += 1
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "send_break",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"send_break failed: {e}")

def execute_flush_action(session: 'session_container_with_log_file', action: Dict):
    """Execute 'flush' action - clear buffers (Phase 3, updated Phase 5A1)
    
    Phase 5A1: Uses session.transport.flush() instead of direct serial port access
    """
    try:
        session.transport.flush()
        
        session.current_sequence["results"].append({
            "action": "flush",
            "status": "success"
        })
        
        session.current_sequence["current_action_index"] += 1
        
    except Exception as e:
        session.current_sequence["results"].append({
            "action": "flush",
            "status": "error",
            "error": str(e)
        })
        
        if session.current_sequence["stop_on_error"]:
            finish_sequence_with_error(session, f"flush failed: {e}")

def process_sequence_actions(session: 'session_container_with_log_file'):
    """
    Process the current sequence action (Phase 3).
    
    Called from worker loop when worker_state == 'executing_sequence'.
    """
    if not session.current_sequence:
        return
    
    seq = session.current_sequence
    
    # Check overall sequence timeout
    elapsed = time.time() - seq["sequence_start_time"]
    if elapsed > seq["sequence_timeout"]:
        finish_sequence_with_error(session, f"Overall sequence timeout ({seq['sequence_timeout']}s)")
        return
    
    # Check if all actions completed
    if seq["current_action_index"] >= len(seq["actions"]):
        finish_sequence_success(session)
        return
    
    # Get current action
    action = seq["actions"][seq["current_action_index"]]
    
    # Check for auto-stabilization delay (MUST-DO #5)
    if "_auto_delay_until" in action:
        if time.time() < action["_auto_delay_until"]:
            return  # Still waiting for stabilization
        else:
            # Delay complete, move to next action
            del action["_auto_delay_until"]
            seq["current_action_index"] += 1
            return
    
    # Execute action based on type
    action_type = action.get("action")
    
    if action_type == "send":
        execute_send_action(session, action)
    elif action_type == "wait":
        execute_wait_action(session, action)
    elif action_type == "wait_for":
        execute_wait_for_action(session, action)
    elif action_type == "set_dtr":
        execute_set_dtr_action(session, action)
    elif action_type == "set_rts":
        execute_set_rts_action(session, action)
    elif action_type == "set_baud":
        execute_set_baud_action(session, action)
    elif action_type == "send_break":
        execute_send_break_action(session, action)
    elif action_type == "flush":
        execute_flush_action(session, action)
    else:
        # Unknown action type
        finish_sequence_with_error(session, f"Unknown action type: {action_type}")

def serial_port_worker_thread(session_id: str, stop_event: threading.Event):
    """
    Phase 5A1: Unified worker thread that owns the transport.
    
    This thread is the ONLY one that touches the transport (expert pattern).
    Handles:
    - Continuous reading from transport (non-blocking)
    - Processing commands from command_queue (write, control lines, etc.)
    - Async file streaming with progress tracking
    - Thread-safe metadata updates
    
    Architecture:
    - MCP handler thread sends commands via command_queue
    - Worker executes commands and sends responses via response_queue
    - Worker continuously reads and puts data in output_queue
    - NO other thread touches session.transport
    
    Phase 5A1: Uses session.transport (BaseTransport) instead of session.serial_port
    This allows supporting network transports (TCP, telnet, SSH) later.
    
    Args:
        session_id: Session identifier
        stop_event: Threading event to signal when to stop
    """
    session = get_session(session_id)
    if not session or not session.transport:
        MCPLogger.log(TOOL_LOG_NAME, f"Worker thread: session {session_id} not found or no transport")
        return
    
    MCPLogger.log(TOOL_LOG_NAME, f"Worker thread started for session {session_id} (owns serial port)")
    
    # Get serial module for exception handling
    try:
        serial, _ = ensure_pyserial()
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Worker thread failed to load pyserial: {e}")
        return
    
    try:
        while not stop_event.is_set():
            try:
                # ===== PHASE 3: SEQUENCE PROCESSING =====
                if session.worker_state == "executing_sequence":
                    # Check for out-of-band cancel (MUST-DO #4)
                    try:
                        cmd = session.command_queue.get_nowait()
                        if cmd[0] == "cancel_sequence":
                            finish_sequence_cancelled(session, "Cancelled by user")
                        elif cmd[0] == "send_sequence":
                            # Trying to start another sequence - error
                            session.response_queue.put(("error", "Cannot start sequence while another is executing"))
                        else:
                            # Put back for later (after sequence completes)
                            session.command_queue.put(cmd)
                    except queue.Empty:
                        pass
                    
                    # Process current sequence action
                    process_sequence_actions(session)
                
                # ===== NORMAL COMMAND PROCESSING (if not executing sequence) =====
                elif session.worker_state == "idle":
                    # Process commands (non-blocking)
                    try:
                        cmd = session.command_queue.get(timeout=0.001)  # 1ms timeout
                        _process_worker_command(session, cmd, session_id)
                    except queue.Empty:
                        pass  # No command, continue to reading
                
                # ===== ALWAYS READ TRANSPORT DATA =====
                # This runs regardless of worker_state (CRITICAL for not losing data!)
                # Phase 5A1: Use transport abstraction instead of direct serial_port access
                # Phase 5B: Always try non-blocking read (works for both serial and network)
                # Expert pattern: Don't rely on bytes_available() for network transports
                data = session.transport.read(4096)
                
                if data:
                    # PHASE 3: Terminal emulation BEFORE logging (MUST-DO #3)
                    if session.terminal_emulation_enabled:
                        data, ansi_responses = intercept_ansi_queries(
                            data, 
                            session.ansi_carry,  # Multi-chunk buffer
                            session.terminal_size
                        )
                        
                        # Send auto-responses immediately
                        for response in ansi_responses:
                            session.transport.write(response)
                            
                            # Log the auto-response (for debugging)
                            if session.log_file_handle:
                                log_msg = f"[ANSI_AUTO_RESPONSE: {response.hex()}]\n".encode()
                                session.log_file_handle.write(log_msg)
                    
                    # Write cleaned data to log file
                    if session.log_file_handle:
                        session.log_file_handle.write(data)
                        session.log_file_handle.flush()
                    
                    # Update statistics (thread-safe)
                    with session.metadata_lock:
                        session.metadata.total_bytes_received += len(data)
                        session.metadata.log_file_size_bytes += len(data)
                        session.metadata.last_activity_time = datetime.now()
                    
                    # PHASE 3: Route data based on worker state
                    if session.worker_state == "executing_sequence" and session.current_sequence:
                        # BUG #1 FIX: Always accumulate data during sequences!
                        # Data that arrives during wait/send actions needs to be available for subsequent wait_for
                        session.current_sequence["accumulated_data"].extend(data)
                        
                        # Apply rolling window to prevent memory explosion (MUST-DO #2)
                        # Use max_bytes from current wait_for action if active, otherwise use default
                        max_bytes = 65536  # Default rolling window
                        if session.current_sequence["current_action_index"] < len(session.current_sequence["actions"]):
                            current_action = session.current_sequence["actions"][session.current_sequence["current_action_index"]]
                            if current_action.get("action") == "wait_for":
                                max_bytes = current_action.get("max_bytes", 65536)
                        
                        # Trim to rolling window
                        if len(session.current_sequence["accumulated_data"]) > max_bytes:
                            overflow = len(session.current_sequence["accumulated_data"]) - max_bytes
                            del session.current_sequence["accumulated_data"][:overflow]
                    else:
                        # Normal operation: put in output_queue
                        if session.output_queue:
                            session.output_queue.put(('data', data))
                            MCPLogger.log(TOOL_LOG_NAME, f"QUEUE PUT session={session_id} type=data len={len(data)} qsize={session.output_queue.qsize()}")
                    
                    MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} received {len(data)} bytes (worker_state={session.worker_state}, seq={session.current_sequence is not None})")
                else:
                    # No data right now, tiny sleep to avoid busy-spin (Phase 5B fix)
                    time.sleep(0.005)  # 5ms
                    
            except TransportConnectionError as e:
                # Phase 5A1: Connection lost - mark session as inactive (expert pattern)
                MCPLogger.log(TOOL_LOG_NAME, f"Transport connection lost for session {session_id}: {e}")
                
                # Mark session as inactive (fast failure for higher operations)
                with session.metadata_lock:
                    session.metadata.is_active = False
                
                # Clean up sequence state if worker dies mid-sequence
                if session.current_sequence and session.worker_state == "executing_sequence":
                    finish_sequence_with_error(session, f"Connection lost: {e}")
                
                if session.output_queue:
                    error_msg = f"Connection lost: {e}"
                    session.output_queue.put(('error', error_msg))
                    MCPLogger.log(TOOL_LOG_NAME, f"QUEUE PUT session={session_id} type=error msg={error_msg[:50]} qsize={session.output_queue.qsize()}")
                break
            
            except TransportError as e:
                # Phase 5A1: Other transport error - mark session as inactive
                MCPLogger.log(TOOL_LOG_NAME, f"Transport error for session {session_id}: {e}")
                
                # Mark session as inactive
                with session.metadata_lock:
                    session.metadata.is_active = False
                
                # Clean up sequence state if worker dies mid-sequence
                if session.current_sequence and session.worker_state == "executing_sequence":
                    finish_sequence_with_error(session, f"Transport error: {e}")
                
                if session.output_queue:
                    error_msg = f"Transport error: {e}"
                    session.output_queue.put(('error', error_msg))
                    MCPLogger.log(TOOL_LOG_NAME, f"QUEUE PUT session={session_id} type=error msg={error_msg[:50]} qsize={session.output_queue.qsize()}")
                break
            
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Unexpected error in worker thread for session {session_id}: {e}")
                
                # Mark session as inactive
                with session.metadata_lock:
                    session.metadata.is_active = False
                
                # Clean up sequence state if worker dies mid-sequence
                if session.current_sequence and session.worker_state == "executing_sequence":
                    finish_sequence_with_error(session, f"Worker thread unexpected error: {e}")
                
                if session.output_queue:
                    error_msg = str(e)
                    session.output_queue.put(('error', error_msg))
                    MCPLogger.log(TOOL_LOG_NAME, f"QUEUE PUT session={session_id} type=error msg={error_msg[:50]} qsize={session.output_queue.qsize()}")
                break
                
    finally:
        # Phase 5A1: Always cleanup (expert pattern - Part 21)
        MCPLogger.log(TOOL_LOG_NAME, f"Worker thread stopped for session {session_id} - cleaning up")
        
        # Close transport
        if session and session.transport:
            try:
                session.transport.close()
                MCPLogger.log(TOOL_LOG_NAME, f"Transport closed for session {session_id}")
            except:
                pass  # Ignore errors on close
        
        # Close log file
        if session:
            close_log_file(session)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Worker thread cleanup complete for session {session_id}")

def _process_worker_command(session: session_container_with_log_file, cmd: tuple, session_id: str):
    """
    Process a single command from the command queue.
    Called ONLY by the worker thread (which owns the serial port).
    
    Command format: (operation, *args)
    - ("write", data_bytes) -> write to port, send ("ok", bytes_sent) response
    - ("write_async", operation_id) -> start async file/data send
    - ("set_line", line_name, value) -> set DTR/RTS
    - ("get_line_states",) -> get CTS/DSR/RI/CD/DTR/RTS, send dict response
    - ("send_break", duration) -> send BREAK signal
    - ("set_baud", new_baud) -> change baud rate
    - ("cancel_async", operation_id) -> cancel async operation
    - ("send_sequence", sequence_id, actions, options) -> Phase 3: start sequence
    - ("cancel_sequence",) -> Phase 3: cancel active sequence
    - ("set_terminal_emulation", enabled, terminal_size) -> Phase 3: configure terminal emulation
    """
    try:
        operation = cmd[0]
        
        if operation == "write":
            # Synchronous write (from send_data)
            # Phase 5A1: Use transport abstraction
            data_bytes = cmd[1]
            bytes_written = session.transport.write(data_bytes)
            
            # Update statistics (thread-safe)
            with session.metadata_lock:
                session.metadata.total_bytes_sent += bytes_written
                session.metadata.last_activity_time = datetime.now()
            
            # Send response
            session.response_queue.put(("ok", bytes_written))
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} wrote {bytes_written} bytes")
        
        elif operation == "write_async":
            # Async write (from send_async) - handles file streaming
            operation_id = cmd[1]
            _execute_async_operation(session, operation_id, session_id)
        
        elif operation == "set_line":
            # Set control line (DTR or RTS)
            # Phase 5A1: Use transport abstraction
            line_name, value = cmd[1], cmd[2]
            try:
                if line_name == "dtr":
                    session.transport.set_dtr(bool(value))
                elif line_name == "rts":
                    session.transport.set_rts(bool(value))
                session.response_queue.put(("ok", None))
            except TransportError as e:
                # Graceful degradation for network transports
                session.response_queue.put(("error", str(e)))
        
        elif operation == "get_line_states":
            # Get all line states
            # Phase 5A1: Use transport abstraction
            try:
                # Get line states from transport (serial returns full dict, network raises error)
                line_states = session.transport.get_line_states()
                
                # Add DTR/RTS (these are writable, not part of line states)
                # For serial, we can read them back; for network, we'd need to track them
                states = {
                    "cts": line_states.get("CTS", False),
                    "dsr": line_states.get("DSR", False),
                    "ri": line_states.get("RI", False),
                    "cd": line_states.get("CD", False),
                    # Note: DTR/RTS are writable-only on most transports
                    "dtr": False,  # Can't read back on most transports
                    "rts": False,  # Can't read back on most transports
                }
                session.response_queue.put(("ok", states))
            except TransportError as e:
                # Graceful degradation for network transports
                session.response_queue.put(("error", str(e)))
        
        elif operation == "send_break":
            # Send BREAK signal
            # Phase 5A1: Use transport abstraction (graceful degradation for network)
            duration = cmd[1]
            try:
                session.transport.send_break(duration=duration)
                session.response_queue.put(("ok", None))
            except TransportError as e:
                # Transport doesn't support BREAK (network transports, or driver limitation)
                error_msg = f"BREAK signal not supported: {e}"
                MCPLogger.log(TOOL_LOG_NAME, error_msg)
                session.response_queue.put(("error", error_msg))
        
        elif operation == "set_baud":
            # Change baud rate
            # Phase 5A1: Use transport abstraction (only works for serial)
            new_baud = cmd[1]
            try:
                # For serial: get current baud before changing (if we need old value)
                # For network: will raise TransportError
                session.transport.set_baud_rate(new_baud)
                session.response_queue.put(("ok", (None, new_baud)))  # old_baud not available via abstraction
            except TransportError as e:
                # Network transports don't have baud rate
                session.response_queue.put(("error", str(e)))
        
        elif operation == "cancel_async":
            # Cancel async operation
            operation_id = cmd[1]
            with session.async_operations_lock:
                if operation_id in session.async_operations:
                    op = session.async_operations[operation_id]
                    if op.status not in ["completed", "error", "cancelled"]:
                        op.status = "cancelled"
                        op.end_time = datetime.now()
            session.response_queue.put(("ok", None))
        
        # ===== PHASE 3: SEQUENCE COMMANDS =====
        elif operation == "send_sequence":
            # Start sequence execution
            sequence_id = cmd[1]
            actions = cmd[2]
            options = cmd[3]
            
            # Initialize sequence
            session.current_sequence = initialize_sequence(sequence_id, actions, options)
            session.worker_state = "executing_sequence"
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} worker_state -> executing_sequence")
            
            # If async, send immediate response
            if options.get("async", False):
                session.response_queue.put(("ok", {
                    "sequence_id": sequence_id,
                    "status": "started",
                    "async": True
                }))
            # If blocking, response will come when sequence completes
        
        elif operation == "cancel_sequence":
            # Cancel active sequence (handled in main loop, but acknowledge here)
            session.response_queue.put(("ok", None))
        
        elif operation == "set_terminal_emulation":
            # Configure terminal emulation
            enabled = cmd[1]
            terminal_size = cmd[2]
            
            session.terminal_emulation_enabled = enabled
            if terminal_size:
                session.terminal_size = terminal_size
            
            session.response_queue.put(("ok", {
                "terminal_emulation_enabled": session.terminal_emulation_enabled,
                "terminal_size": session.terminal_size
            }))
        
        else:
            MCPLogger.log(TOOL_LOG_NAME, f"Unknown command: {operation}")
            session.response_queue.put(("error", f"Unknown command: {operation}"))
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing command {cmd}: {e}")
        session.response_queue.put(("error", str(e)))

def _execute_async_operation(session: session_container_with_log_file, operation_id: str, session_id: str):
    """
    Execute an async operation (file streaming).
    Called ONLY by the worker thread (which owns the serial port).
    """
    # Get operation state
    with session.async_operations_lock:
        if operation_id not in session.async_operations:
            MCPLogger.log(TOOL_LOG_NAME, f"Async operation {operation_id} not found")
            return
        op = session.async_operations[operation_id]
        
        # Check if cancelled before starting
        if op.status == "cancelled":
            MCPLogger.log(TOOL_LOG_NAME, f"Async operation {operation_id} was cancelled before start")
            op.end_time = datetime.now()
            return
        
        # Mark as in progress
        op.status = "in_progress"
    
    MCPLogger.log(TOOL_LOG_NAME, f"Worker processing async operation {operation_id}")
    
    try:
        # Determine data source
        if op.source_file_path:
            # Stream from file
            with open(op.source_file_path, 'rb') as f:
                # Get file size
                f.seek(0, 2)
                op.total_bytes = f.tell()
                f.seek(0, 0)
                
                # Stream in chunks (hardware flow control will pace us)
                chunk_size = 4096
                while True:
                    # Check for cancellation
                    with session.async_operations_lock:
                        if op.status == "cancelled":
                            raise Exception("Operation cancelled by user")
                    
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    
                    # Write to transport (Phase 5A1: abstraction)
                    session.transport.write(chunk)
                    
                    # Update progress (thread-safe)
                    with session.async_operations_lock:
                        op.bytes_processed += len(chunk)
                    
                    with session.metadata_lock:
                        session.metadata.total_bytes_sent += len(chunk)
                        session.metadata.last_activity_time = datetime.now()
                    
                    # Log progress periodically
                    if op.bytes_processed % (chunk_size * 10) == 0:
                        pct = op.get_percent_complete()
                        MCPLogger.log(TOOL_LOG_NAME, f"Async {operation_id}: {pct:.1f}% ({op.bytes_processed}/{op.total_bytes} bytes)")
        
        elif op.inline_data:
            # Send inline data (Phase 5A1: use transport)
            op.total_bytes = len(op.inline_data)
            session.transport.write(op.inline_data)
            
            with session.async_operations_lock:
                op.bytes_processed = len(op.inline_data)
            
            with session.metadata_lock:
                session.metadata.total_bytes_sent += len(op.inline_data)
                session.metadata.last_activity_time = datetime.now()
        
        # Mark as completed
        with session.async_operations_lock:
            op.status = "completed"
            op.end_time = datetime.now()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Async {operation_id} completed ({op.total_bytes} bytes)")
        
    except Exception as e:
        # Phase 2E: Explicit handling for serial timeout (better error messages)
        serial, _ = ensure_pyserial()
        
        # Mark as error
        with session.async_operations_lock:
            op.status = "error"
            
            # Check for SerialTimeoutException specifically
            if hasattr(serial, 'SerialTimeoutException') and isinstance(e, serial.SerialTimeoutException):
                op.error_message = f"Serial timeout - device may be hung or hardware flow control stalled: {e}"
            else:
                op.error_message = str(e)
            
            op.end_time = datetime.now()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Async {operation_id} failed: {e}")

def open_serial_port_with_dtr_rts_workaround(endpoint: str, baud_rate: int = 115200, 
                                              set_dtr: bool = False, set_rts: bool = False,
                                              hardware_flow_control: bool = False,
                                              software_flow_control: bool = False,
                                              parity: str = 'N',
                                              stopbits: float = 1,
                                              bytesize: int = 8):
    """
    Open serial port with DTR/RTS workaround and configurable flow control (Phase 2C).
    
    Based on weeks of debugging ESP32/ESP8266/ESP32-CAM behavior:
    - These boards wire RTS → EN (reset) and DTR → GPIO0 (boot-strapping)
    - Lines are ACTIVE-LOW: False = idle/deasserted, True = asserted
    - Opening a port can momentarily pulse these lines, causing unwanted resets
    - Must disable hardware flow control (rtscts/dsrdtr) to avoid conflicts on ESP32
    - Must wait ~150ms after deasserting for board to finish any spurious reset
    - Must clear buffers before starting communication
    
    Phase 2C additions:
    - Support hardware flow control for devices like Roland MDX
    - Support software flow control (XON/XOFF) for legacy devices
    - Support non-standard parity, stopbits, bytesize for industrial protocols
    
    Reference: https://github.com/gitcnd/ampy (fixed pyboard.py)
    Reference: https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html
    
    Args:
        endpoint: Serial port name (e.g., "COM3", "/dev/ttyUSB0")
        baud_rate: Baud rate (default: 115200)
        set_dtr: Set DTR active (True) or inactive (False, default)
        set_rts: Set RTS active (True) or inactive (False, default)
        hardware_flow_control: Enable RTS/CTS and DSR/DTR flow control (default: False for ESP32 safety)
        software_flow_control: Enable XON/XOFF flow control (default: False)
        parity: Parity - 'N'=none, 'E'=even, 'O'=odd, 'M'=mark, 'S'=space (default: 'N')
        stopbits: Stop bits - 1, 1.5, or 2 (default: 1)
        bytesize: Data bits - 5, 6, 7, or 8 (default: 8)
        
    Returns:
        Opened serial.Serial instance with lines stabilized and buffers cleared
        
    Raises:
        Exception if port cannot be opened
    """
    try:
        # Ensure pyserial is available (auto-installs if missing)
        serial, _ = ensure_pyserial()
        
        # Map parity string to pyserial constants
        parity_map = {
            'N': serial.PARITY_NONE,
            'E': serial.PARITY_EVEN,
            'O': serial.PARITY_ODD,
            'M': serial.PARITY_MARK,
            'S': serial.PARITY_SPACE
        }
        parity_value = parity_map.get(parity.upper(), serial.PARITY_NONE)
        
        # Map stopbits to pyserial constants
        stopbits_map = {
            1: serial.STOPBITS_ONE,
            1.5: serial.STOPBITS_ONE_POINT_FIVE,
            2: serial.STOPBITS_TWO
        }
        stopbits_value = stopbits_map.get(stopbits, serial.STOPBITS_ONE)
        
        # Map bytesize to pyserial constants
        bytesize_map = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS
        }
        bytesize_value = bytesize_map.get(bytesize, serial.EIGHTBITS)
        
        # Open port with explicit settings
        # CRITICAL: Default to flow control DISABLED for ESP32 safety
        # Can be enabled for devices like Roland MDX that require it
        port = serial.Serial(
            port=endpoint,
            baudrate=baud_rate,
            bytesize=bytesize_value,
            parity=parity_value,
            stopbits=stopbits_value,
            timeout=1.0,           # Reasonable timeout for reads
            write_timeout=1.0,     # Timeout for writes
            rtscts=hardware_flow_control,   # RTS/CTS hardware flow control
            dsrdtr=hardware_flow_control,   # DSR/DTR hardware flow control
            xonxoff=software_flow_control,  # XON/XOFF software flow control
            exclusive=True         # Request exclusive access (POSIX only, ignored on Windows)
        )
        
        # CRITICAL: Immediately set DTR/RTS if NOT using hardware flow control
        # (If using hardware flow control, these are managed by the driver)
        if not hardware_flow_control:
            port.dtr = set_dtr  # Usually False unless entering DFU mode
            port.rts = set_rts  # Usually False unless entering DFU mode
        
        # CRITICAL: Wait for the board to finish any unintended reset
        # ESP32 needs ~100-150ms to boot after a reset
        time.sleep(0.15)
        
        # CRITICAL: Clear any garbage data from spurious reset
        port.reset_input_buffer()
        port.reset_output_buffer()
        
        flow_info = []
        if hardware_flow_control:
            flow_info.append("hwflow=RTS/CTS+DSR/DTR")
        if software_flow_control:
            flow_info.append("swflow=XON/XOFF")
        if not hardware_flow_control and not software_flow_control:
            flow_info.append("no-flow-control")
        
        config_str = f"{parity}{bytesize}{stopbits}".replace(".", "_")
        flow_str = ",".join(flow_info)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Opened {endpoint} at {baud_rate} baud, {config_str}, {flow_str} (DTR={set_dtr}, RTS={set_rts}) - stabilized after 150ms")
        
        return port
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error opening serial port {endpoint}: {e}")
        raise

def close_serial_port_safely(port):
    """
    Safely close serial port, being careful with DTR/RTS to avoid triggering resets.
    
    Args:
        port: Serial port instance to close
    """
    if not port:
        return
    
    try:
        # Before closing, explicitly set DTR/RTS to safe states
        # (Some devices reset on disconnect if these change)
        port.dtr = False
        port.rts = False
        
        # Small delay to let the lines stabilize
        time.sleep(0.05)
        
        # Now close the port
        port.close()
        
        MCPLogger.log(TOOL_LOG_NAME, f"Closed serial port {port.port} safely")
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error closing serial port: {e}")

# ============================================================================
# SESSION LIFECYCLE MANAGEMENT
# ============================================================================

def create_new_session(endpoint: str, transport_type: str = "serial") -> str:
    """
    Create a new MCU serial session.
    
    Args:
        endpoint: Port name (e.g., "COM3", "/dev/ttyUSB0") or network address
        transport_type: "serial", "tcp", "websocket", etc.
        
    Returns:
        session_id: Unique identifier for this session
        
    Note: Phase 1 just creates the session structure and log file.
          Phase 2 will add actual serial port opening.
    """
    global _next_session_id
    
    with _session_cache_lock:
        session_id = f"mcu_{_next_session_id}"
        _next_session_id += 1
        
        # Create log file
        log_path, log_handle = create_log_file_for_session(session_id)
        
        # Create session metadata
        metadata = terminal_session_metadata(
            session_id=session_id,
            session_start_time=datetime.now(),
            log_file_path=log_path,
            transport_type=transport_type,
            endpoint=endpoint
        )
        
        # Create session container
        session = session_container_with_log_file(
            metadata=metadata,
            log_file_handle=log_handle
        )
        
        # Store in cache
        _active_sessions_cache[session_id] = session
        
        MCPLogger.log(TOOL_LOG_NAME, f"Created session {session_id} for endpoint {endpoint}")
        
        return session_id

def get_session(session_id: str) -> Optional[session_container_with_log_file]:
    """Get a session by ID (thread-safe)"""
    with _session_cache_lock:
        return _active_sessions_cache.get(session_id)

def close_session(session_id: str) -> Tuple[bool, str]:
    """
    Close a session and clean up resources.
    
    Args:
        session_id: Session to close
        
    Returns:
        Tuple of (success, message)
    """
    with _session_cache_lock:
        session = _active_sessions_cache.get(session_id)
        
        if not session:
            return False, f"Session {session_id} not found"
        
        # Phase 2D: Stop unified worker thread if running
        if session.worker_stop_event:
            session.worker_stop_event.set()
            
        if session.worker_thread and session.worker_thread.is_alive():
            MCPLogger.log(TOOL_LOG_NAME, f"Waiting for worker thread to stop for session {session_id}")
            session.worker_thread.join(timeout=2.0)
            
        # Phase 5A1: Close transport (expert pattern - Part 21: resource cleanup)
        if session.transport:
            try:
                session.transport.close()
                MCPLogger.log(TOOL_LOG_NAME, f"Transport closed for session {session_id}")
            except:
                pass  # Ignore errors on close
            session.transport = None
        
        # Keep serial_port cleanup for backward compatibility during migration
        if session.serial_port:
            try:
                close_serial_port_safely(session.serial_port)
            except:
                pass
            session.serial_port = None
        
        # Close log file
        close_log_file(session)
        
        # Mark inactive
        session.metadata.is_active = False
        
        # Remove from active cache
        del _active_sessions_cache[session_id]
        
        MCPLogger.log(TOOL_LOG_NAME, f"Closed session {session_id}")
        
        return True, f"Session {session_id} closed successfully"

def list_active_sessions() -> List[Dict]:
    """Get list of all active sessions with their metadata"""
    with _session_cache_lock:
        sessions_info = []
        
        for session_id, session in _active_sessions_cache.items():
            # Calculate runtime
            runtime_seconds = (datetime.now() - session.metadata.session_start_time).total_seconds()
            
            session_info = {
                "session_id": session_id,
                "endpoint": session.metadata.endpoint,
                "transport_type": session.metadata.transport_type,
                "runtime_seconds": round(runtime_seconds, 2),
                "bytes_received": session.metadata.total_bytes_received,
                "bytes_sent": session.metadata.total_bytes_sent,
                "log_file_size_bytes": session.metadata.log_file_size_bytes,
                "log_file_path": str(session.metadata.log_file_path),
                "is_active": session.metadata.is_active
            }
            
            sessions_info.append(session_info)
        
        return sessions_info

# ============================================================================
# MCP TOOL DEFINITION
# ============================================================================

TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Use this to connect a persistant PuTTY-like terminalvia serial-port, telnet, tcp, sockets, pipes/FIFOs, websockets, Bluetooth, RFC2217, SSH, JTAG, or STDIO to local or remote devices, systems, services, and locally-spawned programs.""",
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
                    "enum": ["readme", "list_ports", "discover_network", "discover_bluetooth", "discover_ble", "bleak", "bleak_get_notifications", "bleak_disconnect", "open_session", "close_session", "list_sessions", "get_session_info", "send_data", "read_data", "wait_for_pattern", "send_async", "get_async_status", "cancel_async", "set_baud", "send_break", "get_line_states", "send_sequence", "get_sequence_status", "cancel_sequence", "set_terminal_emulation", "enable_bluetooth"],
                    "description": "Operation to perform"
                },
                "session_id": {
                    "type": "string",
                    "description": "Session identifier for operations on existing sessions"
                },
                "endpoint": {
                    "type": "string",
                    "description": "Serial port name (e.g., COM3, /dev/ttyUSB0) or network address for open_session"
                },
                "transport_type": {
                    "type": "string",
                    "enum": ["serial", "tcp", "websocket"],
                    "default": "serial"
                    # self evident from above: "description": "Transport type" #  (Phase 2A: only 'serial' supported)
                },
                "baud_rate": {
                    "type": "integer",
                    "default": 115200,
                    "description": "for serial port"
                    #was: "description": "Baud rate for serial port" #  (Phase 2A)
                },
                "set_dtr": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set serial port DTR line active" # (Phase 2A: for special boot modes)"
                },
                "set_rts": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set serial port RTS line active" # (Phase 2A: for special boot modes)"
                },
                "hardware_flow_control": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable RTS/CTS and DSR/DTR hardware flow control" # (Phase 2C: for devices like Roland MDX)"
                },
                "software_flow_control": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable XON/XOFF software flow control" # (Phase 2C: for legacy devices)"
                },
                "parity": {
                    "type": "string",
                    "default": "N",
                    "description": "Parity: N=none, E=even, O=odd, M=mark, S=space" # (Phase 2C)"
                },
                "stopbits": {
                    "type": "number",
                    "default": 1,
                    "description": "Stop bits: 1, 1.5, or 2" # (Phase 2C)"
                },
                "bytesize": {
                    "type": "integer",
                    "default": 8,
                    "description": "Data bits: 5, 6, 7, or 8" # (Phase 2C)"
                },
                "data": {
                    "type": "string",
                    "description": "Data to send. Control chars: ^C (Ctrl+C), ^D, etc. Escapes: \\r \\n \\t \\xNN (hex byte) \\uNNNN (Unicode) \\\\ (backslash) \\^ (literal caret)"
                },
                "file_path": {
                    "type": "string",
                    "description": "Path to file (for send_async: source file to stream; for operations reading files)"
                },
                "operation_id": {
                    "type": "string",
                    "description": "Identifier for async operation (send_async, get_async_status, cancel_async)"
                },
                "duration": {
                    "type": "number",
                    "default": 0.25,
                    "description": "Duration in seconds (for send_break operation, or scan duration for discover_bluetooth/discover_ble operations)"
                },
                "include_services": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include full service/characteristic details for discover_ble. If true, connects to each device to enumerate services."
                },
                "service": {
                    "type": "string",
                    "description": "Service filter for discover_bluetooth. Use 'serial' for SPP-capable devices only, or null to see ALL devices."
                },
                "address": {
                    "type": "string",
                    "description": "'AA:BB:CC:DD:EE:FF' BLE MAC address for bleak"
                },
                "method": {
                    "type": "string",
                    "description": "Bleak method name: 'read_gatt_char', 'write_gatt_char', 'start_notify', 'stop_notify', 'get_services'"
                },
                "char_uuid": {
                    "type": "string",
                    "description": "GATT characteristic UUID e.g. '00002a37-0000-1000-8000-00805f9b34fb'"
                },
                "service_uuid": {
                    "type": "string",
                    "description": "GATT service UUID (optional, helps narrow down characteristic search)"
                },
                "callback_id": {
                    "type": "string",
                    "description": "Callback ID for bleak notifications (format: 'address:char_uuid')"
                },
                "max_notifications": {
                    "type": "number",
                    "default": 100
                    # self-evident: "description": "Maximum notifications to return"
                },
                "raw_bytes": {
                    "type": "string",
                    "description": "Raw bytes to send as hex string (e.g., '03 04' or '0304')"
                },
                "read_timeout": {
                    "type": "number",
                    "default": 1.0,
                    "description": "Read timeout in seconds (for read_data and wait_for_pattern operations)"
                },
                "idle_timeout_seconds": {
                    "type": "number",
                    "description": "Idle timeout in seconds: stop after N seconds of silence, even if read_timeout not reached. Useful for unpredictable output (AI responses, compilation logs)."
                },
                "max_bytes": {
                    "type": "integer",
                    "default": 65536
                    # self-evident: "description": "Maximum bytes to read"
                },
                "include_hex": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include data_hex field in read responses (reduces bandwidth if false)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation"
                },
                "response_timeout": {
                    "type": "number",
                    "default": 5.0,
                    "description": "Worker thread response timeout in seconds (for slow links/devices)"
                },
                "connect_timeout": {
                    "type": "number",
                    "default": 5.0,
                    "description": "Connection establishment timeout in seconds (all network/IPC transports)"
                },
                "ssh_username": {
                    "type": "string",
                    "description": "Username for SSH authentication (can also be in endpoint as ssh://user@host:port)"
                },
                "ssh_password": {
                    "type": "string",
                    "description": "Password for SSH authentication (optional if using key-based auth)"
                },
                "ssh_key_filename": {
                    "type": "string",
                    "description": "Path to SSH private key file (alternative to password)"
                },
                "ssh_key_data": {
                    "type": "string",
                    "description": "Inline SSH private key data (alternative to ssh_key_filename)"
                },
                "ssh_key_password": {
                    "type": "string",
                    "description": "Password for encrypted SSH key (only if key is encrypted)"
                },
                "ssh_allow_unknown_hosts": {
                    "type": "boolean",
                    "default": False,
                    "description": "Auto-accept unknown SSH host keys" # TODO - make this default True? (we don't store host keys, do we?)
                },
                "ssh_terminal_type": {
                    "type": "string",
                    "default": "xterm-256color" # TODO - needs to list other options - do we ahve any?
                    # self-evident: "description": "SSH terminal type for PTY"
                },
                "ssh_terminal_width": {
                    "type": "integer",
                    "default": 80
                    # self-evident: "description": "SSH terminal width in characters"
                },
                "ssh_terminal_height": {
                    "type": "integer",
                    "default": 24
                    # self-evident: "description": "SSH terminal height in lines"
                },
                "ssh_compression": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable SSH compression"
                },
                "ssh_otp_secret": {
                    "type": "string",
                    "description": "TOTP secret for 2FA/OTP (Base32-encoded, tool will generate current 6-digit code)"
                },
                "ssh_otp_code": {
                    "type": "string",
                    "description": "Pre-generated OTP code for 2FA (alternative to ssh_otp_secret if code is already known)"
                },
                #"ssh_allow_agent": {
                #    "type": "boolean",
                #    "default": False,
                #    "description": "Try SSH agent for keys (OUT OF SCOPE for MCP - automated/headless)"
                #},
                "pattern": {
                    "type": "string",
                    "description": "Pattern to search for (wait_for_pattern operation)"
                },
                "use_regex": {
                    "type": "boolean",
                    "default": False,
                    "description": "Use regex matching for pattern (wait_for_pattern operation)"
                },
                "sequence": {
                    "type": "array",
                    "description": "Array of actions for send_sequence. Each action is an object with 'action' field and action-specific parameters."
                },
                "options": {
                    "type": "object",
                    "description": "Options for send_sequence: {timeout: 60.0, stop_on_error: true}"
                },
                "async": {
                    "type": "boolean",
                    "default": False,
                    "description": "Fire-and-forget mode for send_sequence: return immediately, query status later"
                },
                "sequence_id": {
                    "type": "string",
                    "description": "Identifier for sequence (for get_sequence_status, cancel_sequence, or user-provided ID for send_sequence)"
                },
                "enabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable/disable terminal emulation (for set_terminal_emulation)"
                },
                "terminal_size": {
                    "type": "object",
                    "description": "Terminal size for ANSI auto-response: {rows: 24, cols: 80}" # TODO: why we have this twice?
                },
                "service_types": {
                    "type": "array",
                    "description": "List of DNS-SD service types to search for in discover_network. Default: ['_telnet._tcp.local.', '_ssh._tcp.local.', '_arduino._tcp.local.', '_http._tcp.local.']"
                },
                "program_args": {
                    "type": "array",
                    "description": "Command-line arguments for program:// transport (array of strings passed to spawned program)" # TODO - are these  credentials also sanitized in logs
                },
                "program_env": {
                    "type": "object",
                    "description": "Environment variables for program:// transport (dict of key-value pairs, credentials are sanitized in logs)"
                },
                "program_cwd": {
                    "type": "string",
                    "description": "Working directory for program:// transport (path to directory where program should start)"
                },
                "terminal_width": { # TODO - why do we have the 3 times?
                    "type": "integer",
                    "default": 80
                    # self-evident: "description": "Terminal width for program:// transport"
                },
                "terminal_height": {
                    "type": "integer",
                    "default": 24
                    # self-evident: "description": "Terminal height for program:// transport"
                },
                "elevated": {
                    "type": "boolean",
                    "default": False,
                    "description": "Launch program:// as root/admin (Windows: UAC prompt, Linux: pkexec/sudo, macOS: osascript)"
                },
                "elevation_password": {
                    "type": "string",
                    "description": "Password for elevation (if omitted, GUI prompts user)"
                },
                "pipe_mode": {
                    "type": "string",
                    "default": "rw",
                    "enum": ["r", "w", "rw"],
                    "description": "Open mode for pipe:// or fifo://" # transport: 'r'=read-only, 'w'=write-only, 'rw'=read/write"
                },
                "rfc2217_baud": {
                    "type": "integer",
                    "default": 115200,
                    "description": "Initial baud rate for rfc2217:// transport (can be changed later via set_baud_rate)"
                },
                "ws_headers": {
                    "type": "object",
                    "description": "Optional HTTP headers for WebSocket handshake (dict of header_name: header_value pairs, e.g., {'Authorization': 'Bearer token123'})"
                },
                "ws_mode": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "text", "binary"],
                    "description": "WebSocket frame mode: 'auto' (detect JSON, send as text), 'text' (always text frames for JSON-RPC/CDP), 'binary' (always binary frames)"
                },
                "bt_port": {
                    "type": "number",
                    "default": 1,
                    "description": "RFCOMM channel/port for Classic Bluetooth (default: 1 for SPP)"
                },
                "bt_pin": {
                    "type": "string",
                    "description": "Optional PIN code for Classic Bluetooth pairing (e.g., '1234')"
                },
                "ble_mode": {
                    "type": "string",
                    "default": "uart",
                    "description": "BLE connection mode: 'uart' (Nordic UART Service) or 'generic' (manual GATT operations)"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "readme": """
Full feature terminal with every kind of transport imaginable, including extreme control over everything.

For communicating with microcontrollers, and/or other devices and systems.

## Feature Overview

### Bluetooth Transports (Classic BT + BLE)
  * Classic Bluetooth (RFCOMM/SPP): `bt://AA:BB:CC:DD:EE:FF` endpoint format
  * Bluetooth Low Energy (BLE/GATT): `ble://11:22:33:44:55:66` endpoint format
  * BLE UART mode: Automatic Nordic UART Service detection for serial-like communication
  * Discovery operations: `discover_bluetooth` and `discover_ble` with full service details
  * PIN/pairing support for Classic Bluetooth
  * Use cases: ESP32 Bluetooth, HC-05/HC-06 modules, BLE sensors, fitness trackers
  * pybluez for Classic BT (pre-built wheels included, graceful fallback if missing)
  * bleak for BLE (pure Python, auto-installs if missing)
  * Cross-platform: Windows, Linux, macOS

### WebSocket Transport
  * `ws://host:port/path` and `wss://host:port/path` endpoint formats
  * Full WebSocket protocol support (RFC 6455)
  * Bidirectional full-duplex communication
  * SSL/TLS support for secure connections (wss://)
  * Custom HTTP headers for authentication
  * Auto-installs websocket-client library if missing
  * Automatic ping/pong keepalive
  * **Smart frame mode**: Auto-detect JSON and send as text frames (perfect for CDP, JSON-RPC)
  * **Frame modes**: `auto` (default, detect JSON), `text` (always text), `binary` (always binary)
  * **Use cases**: Chrome DevTools Protocol debugging, JSON-RPC APIs, IoT web consoles

### RFC2217 Transport (Remote Serial Port Control)
  * `rfc2217://host:port` endpoint format (e.g., `rfc2217://192.168.1.100:2217`)
  * Full RFC2217 COM-PORT-OPTION support (telnet + serial control)
  * Control remote serial ports: DTR/RTS, baud rate, break signal
  * Read modem state: CTS/DSR/RI/CD line states
  * All serial operations work over network

### Unix Domain Sockets & Named Pipes
  * `unix:///path/to/socket` for Unix domain sockets (Linux/macOS)
  * `pipe://\\\\.\\pipe\\name` for Windows named pipes
  * `fifo:///path/to/fifo` for POSIX FIFOs
  * Use case: Docker containers, databases, local services, legacy integration

### Program/STDIO Transport
  * `program://command` endpoint format (e.g., `program://python`, `program://bash`)
  * Cross-platform PTY support (pty on POSIX, pywinpty on Windows)
  * Full ANSI escape sequence support (colors, cursor movement)
  * Use case: MCP tool testing, interactive REPLs, build monitoring, CLI automation

### Network Device Discovery (mDNS/DNS-SD)
  * `discover_network` operation finds devices advertising services
  * Auto-install zeroconf library if missing
  * Works cross-platform: Windows (Bonjour), Linux/WSL (Avahi), macOS (native)
  * Returns device names, IPs, ports, services for easy connection

### SSH Transport
  * `ssh://user@host:port` endpoint format (e.g., `ssh://aura@server.local:22`)
  * Password and key-based authentication (inline key data or file path)
  * PTY allocation with configurable terminal size
  * Non-blocking shell channel I/O
  * Secure by default (reject unknown hosts unless explicitly allowed)
  * Credential sanitization (passwords NEVER logged)
  * Auto-installation of paramiko library if missing

### Telnet Protocol Support
  * `telnet://host:port` endpoint format (e.g., `telnet://192.168.1.103:23`)
  * Automatic IAC parsing: strips IAC commands, returns clean data
  * 0xFF escaping: writes escape 0xFF → 0xFF 0xFF automatically
  * Conservative negotiation: responds to WILL/DO, accepts SGA/BINARY, refuses most options
  * Use `tcp://` for raw TCP (no IAC) if device doesn't support telnet

### Raw TCP Transport
  * `tcp://host:port` endpoint format (e.g., `tcp://192.168.1.103:23`)
  * Raw byte streams (no telnet IAC handling)
  * All operations work identically (send_data, read_data, sequences, etc.)

### Transport Abstraction
  * BaseTransport interface for all connection types
  * Graceful degradation (TCP raises errors for DTR/RTS/baud operations)
  * Fast failure detection (TransportConnectionError)

### Idle/Inactivity Timeout
  * `read_data` with `idle_timeout_seconds` - read until quiet
  * `wait_for_pattern` with `idle_timeout_seconds` - wait for pattern OR silence
  * Perfect for: AI responses, compilation logs, test suites, unpredictable streams

### Rich Command Sequences
  * Atomic execution of send/wait/wait_for/set_dtr/set_rts/set_baud/break/flush
  * Terminal emulation mode (auto-respond to ANSI queries for full-screen MCU editors)
  * Fire-and-forget sequences (async mode for hour-long operations)
  * Out-of-band cancellation support
  * Auto-stabilization delays (DTR/RTS/baud changes)
  * Rolling window accumulator (prevents memory explosion on chatty devices)

### Smart Pattern Detection
  * `wait_for_pattern` operation for intelligent data matching
  * Enhanced statistics (uptime formatted, activity age, worker status)

### Serial Port Features
  * Hardware flow control (RTS/CTS, DSR/DTR) for devices like Roland MDX
  * Software flow control (XON/XOFF) for legacy devices
  * Parity, stopbits, bytesize configuration for industrial protocols
  * Runtime baud rate switching
  * Serial BREAK signal support
  * Line state monitoring (CTS, DSR, RI, CD)
  * DTR/RTS workarounds for ESP32/ESP8266

### General Features
  * Control character support (^C, ^D, ^E, \\xNN, etc.)
  * Continuous background reading with automatic logging
  * Queue-based buffering for continuous data capture
  * Async file streaming with progress tracking (fire-and-forget large files)
  * Configurable response timeout (slow links supported)
  * Duplicate endpoint prevention
  * Graceful error handling

## Architecture

ONE worker thread owns the transport (serial port, TCP socket, or other connection) for its entire 
lifetime. The MCP handler thread sends commands via queues and never touches the transport directly. 
This eliminates all race conditions and follows expert-recommended patterns for communication.

All operations (send_data, read_data, sequences, etc.) work identically across all transports. 
The worker thread doesn't care about transport type!

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details.
The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Available Operations

### list_ports
List all available serial ports on the system.

Parameters: None

Returns: Array of port information (device, description, hwid, etc.)

### discover_network
Discover devices on local network via mDNS/DNS-SD (Bonjour).
Similar to how Arduino IDE discovers network-enabled boards.

Parameters:
- service_types (optional): Array of DNS-SD service types to search for
  Default: ['_telnet._tcp.local.', '_ssh._tcp.local.', '_arduino._tcp.local.', '_http._tcp.local.']
  Examples: ['_ssh._tcp.local.'], ['_telnet._tcp.local.', '_http._tcp.local.']
- read_timeout (optional): How long to listen for mDNS responses in seconds (default 5.0)

Returns:
- total_devices: Number of devices found
- devices: Array of discovered devices with format:
  {
    "name": "Esp32Lcd06",
    "hostname": "Esp32Lcd06.local",
    "ip": "172.22.1.103",
    "port": 23,
    "service": "telnet",
    "service_type": "_telnet._tcp.local."
  }

**Platform Requirements:**
- Windows: Requires Bonjour service (install iTunes or Bonjour Print Services)
- Linux/WSL: Works out-of-box with Avahi daemon
- macOS: Works out-of-box with native mDNS support

**Example:**
```json
// Discover all common embedded device services
{"operation": "discover_network", "tool_unlock_token": "..."}

// Discover only SSH servers (5 second scan)
{"operation": "discover_network", "service_types": ["_ssh._tcp.local."], "read_timeout": 5.0, "tool_unlock_token": "..."}

// Then connect to discovered device:
{"operation": "open_session", "endpoint": "telnet://Esp32Lcd06.local:23", "tool_unlock_token": "..."}
```

### bleak
**Generic BLE (GATT) operations using python bleak library API directly.**

This operation exposes bleak's API so the AI can use its existing knowledge of the bleak library.
Auto-manages BLE connections per device address - first operation to an address creates a persistent
connection, subsequent operations reuse it. Perfect for hardware hackers who need full BLE control!

**Connection Management:**
- Each device address gets its own persistent BLE connection
- First `bleak` operation to an address auto-creates connection
- Connection persists for notifications and multiple operations
- Use `bleak_disconnect` to explicitly close connection
- Connections are separate from transport sessions (bt://, ble://)

**Supported Methods:**
- `read_gatt_char` - Read a GATT characteristic once
- `write_gatt_char` - Write to a GATT characteristic
- `start_notify` - Subscribe to characteristic notifications (queues data)
- `stop_notify` - Unsubscribe from notifications
- `read_gatt_descriptor` - Read a GATT descriptor
- `write_gatt_descriptor` - Write to a GATT descriptor
- `get_services` - Get all services/characteristics from device

Parameters:
- address (required): BLE MAC address (e.g., "11:22:33:44:55:66")
- method (required): Bleak method name (see supported methods above)
- char_uuid (required for char operations): Characteristic UUID (e.g., "00002a37-0000-1000-8000-00805f9b34fb")
- service_uuid (optional): Service UUID to narrow down characteristic search
- data (required for write operations): Data to write (string, bytes, or array of ints)
- timeout (optional): Operation timeout in seconds (default 10.0)

**Returns:**
- For `read_gatt_char`: {"value": bytes, "value_hex": "01 02 03"}
- For `write_gatt_char`: {"success": true, "bytes_written": N}
- For `start_notify`: {"success": true, "callback_id": "address:char_uuid"}
- For `get_services`: {"services": [...]} with full service/characteristic tree

**Notifications:**
- Use `bleak_get_notifications` operation to poll queued notifications
- Notifications are keyed by callback_id (address:char_uuid)
- Queue has max size (default 100) to prevent memory issues

**Examples:**

1. Read heart rate from fitness tracker:
```json
{
  "operation": "bleak",
  "address": "AA:BB:CC:DD:EE:FF",
  "method": "read_gatt_char",
  "char_uuid": "00002a37-0000-1000-8000-00805f9b34fb",
  "tool_unlock_token": "..."
}
```

2. Write to custom BLE device:
```json
{
  "operation": "bleak",
  "address": "11:22:33:44:55:66",
  "method": "write_gatt_char",
  "char_uuid": "12345678-1234-1234-1234-123456789abc",
  "data": [0x01, 0x02, 0x03],
  "tool_unlock_token": "..."
}
```

3. Subscribe to temperature sensor notifications:
```json
{
  "operation": "bleak",
  "address": "AA:BB:CC:DD:EE:FF",
  "method": "start_notify",
  "char_uuid": "00002a1c-0000-1000-8000-00805f9b34fb",
  "tool_unlock_token": "..."
}
```

4. Poll for notifications:
```json
{
  "operation": "bleak_get_notifications",
  "callback_id": "AA:BB:CC:DD:EE:FF:00002a1c-0000-1000-8000-00805f9b34fb",
  "timeout": 5.0,
  "tool_unlock_token": "..."
}
```

5. Disconnect when done:
```json
{
  "operation": "bleak_disconnect",
  "address": "AA:BB:CC:DD:EE:FF",
  "tool_unlock_token": "..."
}
```

6. Get all services/characteristics:
```json
{
  "operation": "bleak",
  "address": "AA:BB:CC:DD:EE:FF",
  "method": "get_services",
  "tool_unlock_token": "..."
}
```

**Use Cases:**
- Read BLE sensor data (temperature, humidity, pressure)
- Control BLE actuators (LEDs, motors, relays)
- Monitor fitness trackers (heart rate, steps, battery)
- Interact with custom BLE devices (ESP32 BLE, Nordic nRF52)
- Reverse-engineer BLE protocols
- Build BLE automation scripts
- Beacon scanning and analysis

**Note:** This is separate from `open_session` with `ble://` endpoints. Use this for:
- Ad-hoc BLE operations (read/write once)
- Multiple characteristics on same device
- Notification subscriptions

Use `open_session` with `ble://` for:
- Nordic UART Service (serial-like communication)
- Continuous bidirectional data streams

### bleak_get_notifications
Poll for queued BLE notifications from a subscribed characteristic.

Parameters:
- callback_id (required): Callback ID from start_notify (format: "address:char_uuid")
- timeout (optional): How long to wait for notifications (default 1.0 seconds)
- max_notifications (optional): Maximum notifications to return (default 100)

Returns:
- notifications: Array of {timestamp, value, value_hex} objects
- count: Number of notifications returned
- has_more: True if more notifications are queued

Example:
```json
{
  "operation": "bleak_get_notifications",
  "callback_id": "AA:BB:CC:DD:EE:FF:00002a1c-0000-1000-8000-00805f9b34fb",
  "timeout": 2.0,
  "max_notifications": 50,
  "tool_unlock_token": "..."
}
```

### bleak_disconnect
Explicitly disconnect from a BLE device.

Parameters:
- address (required): BLE MAC address to disconnect

Returns:
- success: true
- message: Confirmation message

Example:
```json
{
  "operation": "bleak_disconnect",
  "address": "AA:BB:CC:DD:EE:FF",
  "tool_unlock_token": "..."
}
```

**Note:** Connections auto-close after 5 minutes of inactivity, so explicit disconnect is optional.

### open_session
Open a connection (serial port or TCP) and create a session with background reader and writer threads.

**Endpoint Format (auto-detects transport type)**
- Serial: "COM3", "/dev/ttyUSB0", "/dev/ttyS6" → Opens physical serial port
- TCP: "tcp://host:port" → Opens raw TCP socket (e.g., "tcp://192.168.1.103:23")

Parameters:
- endpoint (required): Connection endpoint (format determines transport type)
  * Serial examples: "COM6", "/dev/ttyUSB0"
  * TCP examples: "tcp://192.168.1.103:23", "tcp://mcu.local:5000"

**Serial-specific parameters** (ignored for TCP):
- baud_rate (optional): Baud rate, default 115200
- set_dtr (optional): Set DTR active (default False - prevents unwanted resets on ESP32)
- set_rts (optional): Set RTS active (default False - prevents unwanted resets on ESP32)
- hardware_flow_control (optional): Enable RTS/CTS and DSR/DTR flow control (default False)
  - Use True for devices like Roland MDX that REQUIRE hardware flow control
  - Keep False for ESP32/ESP8266 to avoid reset issues
- software_flow_control (optional): Enable XON/XOFF flow control (default False)
- parity (optional): Parity - 'N'=none, 'E'=even, 'O'=odd, 'M'=mark, 'S'=space (default 'N')
- stopbits (optional): Stop bits - 1, 1.5, or 2 (default 1)
- bytesize (optional): Data bits - 5, 6, 7, or 8 (default 8)

**TCP-specific parameters** (ignored for serial):
- connect_timeout (optional): Connection timeout in seconds (default 5.0)

Returns: session_id, log_file_path, transport_type, and connection status

**SSH-specific parameters** (ignored for serial/TCP/Telnet):
- ssh_username (optional): Username for SSH auth (can also be in endpoint)
- ssh_password (optional): Password for SSH auth
- ssh_key_filename (optional): Path to private key file
- ssh_key_data (optional): Inline private key data
- ssh_key_password (optional): Password for encrypted key
- ssh_allow_unknown_hosts (optional): Auto-accept unknown host keys (INSECURE! default False)
- ssh_terminal_type (optional): Terminal type for PTY (default "xterm-256color")
- ssh_terminal_width (optional): Terminal width (default 80)
- ssh_terminal_height (optional): Terminal height (default 24)

**Examples:**
```json
// Serial connection
{"operation": "open_session", "endpoint": "COM6", "baud_rate": 115200, "tool_unlock_token": "..."}

// SSH connection with password
{"operation": "open_session", "endpoint": "ssh://aura@172.22.1.66:52266", "ssh_password": "mypassword", "ssh_allow_unknown_hosts": true, "tool_unlock_token": "..."}

// SSH with key file
{"operation": "open_session", "endpoint": "ssh://user@server.com:22", "ssh_key_filename": "/home/user/.ssh/id_rsa", "tool_unlock_token": "..."}

// Telnet connection (with IAC handling)
{"operation": "open_session", "endpoint": "telnet://192.168.1.103:23", "connect_timeout": 10.0, "tool_unlock_token": "..."}

// TCP connection (raw bytes, no IAC)
{"operation": "open_session", "endpoint": "tcp://192.168.1.103:23", "connect_timeout": 10.0, "tool_unlock_token": "..."}

// WebSocket connection with auto-detect mode (default - detects JSON automatically)
{"operation": "open_session", "endpoint": "ws://127.0.0.1:9222/devtools/page/ABC123", "tool_unlock_token": "..."}

// WebSocket with explicit text mode (for JSON-RPC, CDP, text APIs)
{"operation": "open_session", "endpoint": "ws://api.example.com/rpc", "ws_mode": "text", "tool_unlock_token": "..."}

// WebSocket with binary mode (for raw binary protocols)
{"operation": "open_session", "endpoint": "ws://device.local/binary", "ws_mode": "binary", "tool_unlock_token": "..."}

// WebSocket with custom headers (authentication)
{"operation": "open_session", "endpoint": "ws://api.example.com/ws", "ws_headers": {"Authorization": "Bearer token123"}, "tool_unlock_token": "..."}
```

### send_data
Send data to an open session's serial port.

Parameters:
- session_id (required): Session to send data to
- data (optional): String with control character support (^C, ^D, {BS}r, {BS}n, {BS}xNN)
- raw_bytes (optional): Hex string like "03 04" or "0304"

Examples:
- Send Ctrl-C (enter REPL): data="^C"
- REPL command (note {BS}r{BS}n!): data="2+2{BS}r{BS}n"
- Import statement: data="import sys{BS}r{BS}n"
- Multi-line command: data="x = [1,2,3]{BS}r{BS}nsum(x){BS}r{BS}n"
- Send raw hex: raw_bytes="03 04 05"

**TIP:** MicroPython REPL needs {BS}r{BS}n (CRLF), not just {BS}n (LF)

Returns: bytes_sent confirmation

### read_data
Read data from session (waits for timeout or max_bytes or idle timeout).

Parameters:
- session_id (required): Session to read from
- read_timeout (optional): Maximum total wait time in seconds, default 1.0
- idle_timeout_seconds (optional): Stop after N seconds of SILENCE. Useful for unpredictable streams (AI responses, compilation logs, test suites). If data keeps arriving, keep reading. Example: `read_timeout=3600, idle_timeout_seconds=30` means "read for up to 1 hour, but stop after 30 seconds of no new data"
- max_bytes (optional): Maximum bytes to read, default 65536

Returns: data (UTF-8 string), data_hex, bytes_read, timeout_reached, idle_timeout_reached (Phase 4B)

### wait_for_pattern (Phase 2B + 4B - Enhanced!)
Smart queue reading with pattern detection. Wait until pattern appears or timeout or idle timeout.

Parameters:
- session_id (required): Session to read from
- pattern (required): Pattern to search for (string)
- read_timeout (optional): Maximum total wait time in seconds, default 5.0
- idle_timeout_seconds (optional): Also stop if N seconds of silence (even if pattern not found). Useful when pattern might not appear, but device goes quiet. Example: `read_timeout=300, idle_timeout_seconds=10` means "wait up to 5 minutes for pattern, but also stop if 10 seconds pass with no new data"
- max_bytes (optional): Maximum bytes to collect, default 65536
- use_regex (optional): Use regex matching, default False

Returns:
- pattern_found (bool): True if pattern was found
- data: All data collected (including pattern)
- bytes_read: Total bytes collected
- timeout_reached: True if timed out before pattern
- idle_timeout_reached: **Phase 4B NEW!** True if stopped due to silence (Phase 4B)
- elapsed_seconds: Actual wait time

Use Case: Essential for sequences! Wait for CLI prompts, responses, etc.

Examples:
- Wait for MicroPython REPL prompt: pattern=">>>"
- Wait for command completion: pattern="OK{BS}r{BS}n"
- Wait for regex pattern: pattern="{BS}[{BS}d+{BS}]", use_regex=True
- Wait for prompt OR silence: pattern=">>>", idle_timeout_seconds=5.0

**Architecture**: Queue-based (no file access), foundation for Phase 3 sequences!

### send_sequence (Phase 3 - NEW!)
Execute an atomic sequence of actions with precise timing. Perfect for bootloader entry, device initialization, complex command flows.

Parameters:
- session_id (required): Session to execute on
- sequence (required): Array of action objects
- options (optional): {timeout: 60.0, stop_on_error: true}
- async (optional): Fire-and-forget mode (return immediately), default False
- sequence_id (optional): User-provided ID for async sequences

Supported Actions:
- {action: "send", data: "^C"} - Send data (supports control chars like ^C, {BS}xNN, {BS}r, {BS}n)
- {action: "wait", seconds: 0.5} - Wait for precise duration (non-blocking)
- {action: "wait_for", pattern: ">>>", timeout: 5.0, max_bytes: 65536} - Wait for pattern (with rolling window)
- {action: "set_dtr", value: true} - Set DTR line (auto-stabilization delay included)
- {action: "set_rts", value: false} - Set RTS line (auto-stabilization delay included)
- {action: "set_baud", baud_rate: 115200} - Change baud rate (auto-stabilization delay included)
- {action: "send_break", duration: 0.25} - Send BREAK signal
- {action: "flush"} - Clear input/output buffers

Returns (blocking mode):
- status: "success" | "error" | "cancelled"
- actions_completed: Number of actions executed
- actions_total: Total actions in sequence
- results: Array of results for each action
- elapsed_seconds: Total execution time

Returns (async mode):
- sequence_id: ID for later status queries
- status: "started"
- async: true

Safety Features:
- Refuses to start if async upload in progress (prevents data corruption)
- Rolling window prevents memory explosion on chatty devices (default 64KB per wait_for)
- Auto-stabilization delays after DTR/RTS/baud changes (50-100ms)
- Out-of-band cancellation support
- Continuous serial reading (never lose data!)

Example: ESP32 Bootloader Entry
```json
{
  "operation": "send_sequence",
  "session_id": "mcu_1",
  "sequence": [
    {"action": "set_dtr", "value": false},
    {"action": "set_rts", "value": true},
    {"action": "wait", "seconds": 0.05},
    {"action": "set_dtr", "value": true},
    {"action": "set_rts", "value": false},
    {"action": "wait_for", "pattern": "waiting for download", "timeout": 3.0}
  ]
}
```

### get_sequence_status (Phase 3 - NEW!)
Query status of an async (fire-and-forget) sequence.

Parameters:
- session_id (required): Session the sequence is running on
- sequence_id (required): Sequence ID from send_sequence

Returns:
- status: "in_progress" | "success" | "error" | "cancelled"
- actions_completed: Number of actions executed so far
- actions_total: Total actions in sequence
- elapsed_seconds: Time since sequence started
- results: Array of results (for completed actions)

Use Case: Check progress of hour-long milling jobs, firmware uploads, etc.

### cancel_sequence (Phase 3 - NEW!)
Cancel a currently executing sequence (gracefully stops at next action).

Parameters:
- session_id (required): Session to cancel sequence on

Returns:
- success: true
- message: "Cancel signal sent to worker thread"

Notes:
- Cancellation is out-of-band (checked even while executing actions)
- Sequence stops gracefully at next action boundary
- Partial results are saved
- Session remains open and usable after cancellation

### set_terminal_emulation (Phase 3 - NEW!)
Enable/disable ANSI terminal emulation. When enabled, the tool auto-responds to ANSI queries from MCU-based full-screen editors.

Parameters:
- session_id (required): Session to configure
- enabled (required): true to enable, false to disable
- terminal_size (optional): {rows: 24, cols: 80}

Supported ANSI Queries (auto-response when enabled):
- ESC[6n (Cursor position query) → ESC[24;80R
- ESC[18t (Terminal size query) → ESC[8;24;80t

Returns:
- terminal_emulation_enabled: Current state
- terminal_size: Current terminal size

Use Case: Makes ESP32-based full-screen editors (like arrow-key navigable text editors) work correctly!

Example:
```json
{
  "operation": "set_terminal_emulation",
  "session_id": "mcu_1",
  "enabled": true,
  "terminal_size": {"rows": 40, "cols": 120}
}
```

**Architecture Note**: Terminal emulation runs in worker thread BEFORE logging, so:
- ANSI queries are stripped from logs and output_queue (you see clean data)
- Auto-responses are sent immediately (MCU thinks it's talking to real terminal)
- Multi-chunk ANSI sequences handled correctly (carry buffer prevents missed queries)

### close_session
Close a session and clean up resources (stops reader, closes port safely).

Parameters:
- session_id (required): Session to close

Returns: Success confirmation

### list_sessions
List all active sessions with metadata.

Parameters: None

Returns: Array of session information (bytes sent/received, runtime, etc.)

### get_session_info
Get detailed information about a specific session (Phase 2B: Enhanced statistics!).

Parameters:
- session_id (required): Session to query

Returns: Detailed session metadata and statistics
- Phase 2B additions:
  * session_uptime_formatted (HH:MM:SS)
  * bytes_per_second_average (total throughput)
  * last_activity_age_seconds (how long since last data?)
  * worker_thread_alive (is worker still running?)

### send_async (Phase 2C)
Fire-and-forget async send with progress tracking. Perfect for large files.

Parameters:
- session_id (required): Session to send on
- file_path (optional): Path to file to stream (bypasses AI context!)
- data (optional): Inline data with control character support
- operation_id (optional): Custom operation ID (auto-generated if not provided)

Either file_path OR data is required.

Use Cases:
- Stream 10MB RML file to Roland MDX over hardware flow control
- Send large firmware to device without blocking
- Upload big data files to MCU storage

Returns: operation_id, status="pending", message

Use get_async_status to track progress, cancel_async to cancel.

### get_async_status (Phase 2C)
Query progress of an async operation.

Parameters:
- session_id (required): Session the operation is running on
- operation_id (required): Operation to query

Returns:
- status: pending|in_progress|completed|cancelled|error
- percent_complete: Progress percentage
- bytes_processed: Bytes sent so far
- total_bytes: Total bytes to send
- elapsed_seconds: Time since start
- eta_seconds: Estimated time to completion
- error_message: If status is error

### cancel_async (Phase 2C)
Cancel an in-progress async operation.

Parameters:
- session_id (required): Session the operation is running on
- operation_id (required): Operation to cancel

Returns: Success confirmation

### set_baud (Phase 2C)
Change baud rate during an active session (runtime baud switching).

Parameters:
- session_id (required): Session to modify
- baud_rate (required): New baud rate

Returns: old_baud_rate, new_baud_rate, confirmation

Use Case: Some devices require negotiation at one baud, then switch to higher speed.

### send_break (Phase 2C)
Send a serial BREAK signal (prolonged spacing condition).

Parameters:
- session_id (required): Session to send BREAK on
- duration (optional): Duration in seconds, default 0.25

Returns: Success confirmation

Use Case: Some bootloaders and industrial devices use BREAK for signaling.

### get_line_states (Phase 2C)
Read current state of serial control lines (pins).

Parameters:
- session_id (required): Session to query

Returns:
- cts: Clear To Send (input)
- dsr: Data Set Ready (input)
- ri: Ring Indicator (input)
- cd: Carrier Detect (input)
- dtr: Data Terminal Ready (output, current state)
- rts: Request To Send (output, current state)

Use Case: Monitor hardware flow control state, debug wiring issues.

## Input Examples

1. Get documentation:
```json
{
  "input": {"operation": "readme"}
}
```

2. List available ports:
```json
{
  "input": {
    "operation": "list_ports",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

3. Open a new session (ESP32 at 115200 baud):
```json
{
  "input": {
    "operation": "open_session",
    "endpoint": "COM6",
    "baud_rate": 115200,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

4. Send Ctrl-C to interrupt (enter REPL):
```json
{
  "input": {
    "operation": "send_data",
    "session_id": "mcu_1",
    "data": "^C",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

5. Send REPL command (NOTE: use {BS}r{BS}n, not just {BS}n):
```json
{
  "input": {
    "operation": "send_data",
    "session_id": "mcu_1",
    "data": "2+2{BS}r{BS}n",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

5b. Send import statement in REPL:
```json
{
  "input": {
    "operation": "send_data",
    "session_id": "mcu_1",
    "data": "import sys{BS}r{BS}n",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

6. Read response (wait up to 2 seconds):
```json
{
  "input": {
    "operation": "read_data",
    "session_id": "mcu_1",
    "read_timeout": 2.0,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

6b. Wait for specific pattern (Phase 2B - NEW!):
```json
{
  "input": {
    "operation": "wait_for_pattern",
    "session_id": "mcu_1",
    "pattern": ">>>",
    "read_timeout": 5.0,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

6c. Read with idle timeout (Phase 4B - NEW!):
```json
{
  "input": {
    "operation": "read_data",
    "session_id": "mcu_1",
    "read_timeout": 3600,
    "idle_timeout_seconds": 30,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
// Reads for up to 1 hour, but stops after 30 seconds of silence
// Perfect for: AI responses, compilation logs, test suites, unpredictable streams

6d. Wait for pattern OR silence (Phase 4B - NEW!):
```json
{
  "input": {
    "operation": "wait_for_pattern",
    "session_id": "mcu_1",
    "pattern": "DONE",
    "read_timeout": 300,
    "idle_timeout_seconds": 10,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
// Wait up to 5 minutes for "DONE", but also stop if 10 seconds of silence
// Check idle_timeout_reached in result to know which condition triggered

7. List all active sessions:
```json
{
  "input": {
    "operation": "list_sessions",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

8. Get session details:
```json
{
  "input": {
    "operation": "get_session_info",
    "session_id": "mcu_1",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

9. Close session (safely closes port):
```json
{
  "input": {
    "operation": "close_session",
    "session_id": "mcu_1",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

10. Stream large file async (Phase 2C - Roland MDX example):
```json
{
  "input": {
    "operation": "open_session",
    "endpoint": "COM4",
    "baud_rate": 9600,
    "hardware_flow_control": true,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
Then send the file:
```json
{
  "input": {
    "operation": "send_async",
    "session_id": "mcu_2",
    "file_path": "C:/Users/cnd/models/part.rml",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```
Track progress:
```json
{
  "input": {
    "operation": "get_async_status",
    "session_id": "mcu_2",
    "operation_id": "async_1234567890",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

11. Check line states (Phase 2C):
```json
{
  "input": {
    "operation": "get_line_states",
    "session_id": "mcu_1",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

""".replace("{BS}", BS)
    }
]

# ============================================================================
# PARAMETER VALIDATION
# ============================================================================

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema"""
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
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}", {}
            
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
    """Return tool documentation"""
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
    """Create an error response that optionally includes the tool documentation"""
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

# ============================================================================
# OPERATION HANDLERS
# ============================================================================

def handle_open_session(params: Dict) -> Dict:
    """Handle open_session operation - Phase 5C: Serial + TCP + Telnet + SSH support"""
    try:
        endpoint = params.get("endpoint")
        
        # Serial-specific parameters (ignored for TCP/Telnet/SSH)
        baud_rate = params.get("baud_rate", 115200)
        set_dtr = params.get("set_dtr", False)
        set_rts = params.get("set_rts", False)
        hardware_flow_control = params.get("hardware_flow_control", False)
        software_flow_control = params.get("software_flow_control", False)
        parity = params.get("parity", "N")
        stopbits = params.get("stopbits", 1)
        bytesize = params.get("bytesize", 8)
        
        # Network transport parameters (TCP/Telnet/SSH)
        connect_timeout = params.get("connect_timeout", 10.0)
        
        # SSH-specific parameters (Phase 5C)
        ssh_username = params.get("ssh_username")  # Can also come from endpoint
        ssh_password = params.get("ssh_password")
        ssh_key_filename = params.get("ssh_key_filename")
        ssh_key_data = params.get("ssh_key_data")
        ssh_key_password = params.get("ssh_key_password")
        ssh_allow_unknown_hosts = params.get("ssh_allow_unknown_hosts", False)
        ssh_terminal_type = params.get("ssh_terminal_type", "xterm-256color")
        ssh_terminal_width = params.get("ssh_terminal_width", 80)
        ssh_terminal_height = params.get("ssh_terminal_height", 24)
        ssh_compression = params.get("ssh_compression", False)
        ssh_otp_secret = params.get("ssh_otp_secret")  # Phase 5C-4: TOTP secret
        ssh_otp_code = params.get("ssh_otp_code")      # Phase 5C-4: Pre-generated OTP
        ssh_allow_agent = params.get("ssh_allow_agent", False)  # Phase 5C-4: SSH agent (OUT OF SCOPE)
        
        if not endpoint:
            return create_error_response("Parameter 'endpoint' is required for open_session", with_readme=False)
        
        # Phase 5A2: Parse endpoint to detect transport type
        try:
            transport_type, connection_params = parse_endpoint(endpoint)
        except ValueError as e:
            return create_error_response(f"Invalid endpoint format: {e}", with_readme=False)
        
        # Phase 2E: Guard against duplicate opens (expert recommendation)
        with _session_cache_lock:
            for existing_session_id, existing_session in _active_sessions_cache.items():
                if existing_session.metadata.endpoint == endpoint and existing_session.metadata.is_active:
                    return create_error_response(
                        f"Endpoint '{endpoint}' is already open in session '{existing_session_id}'. "
                        f"Please close that session first or use a different endpoint.",
                        with_readme=False
                    )
        
        MCPLogger.log(TOOL_LOG_NAME, f"Opening session for endpoint: {endpoint} (transport: {transport_type})")
        MCPLogger.log(TOOL_LOG_NAME, f"DEBUG: transport_type={repr(transport_type)}, connection_params={repr(connection_params)}")
        
        # Create session (Phase 1 infrastructure)
        session_id = create_new_session(endpoint, transport_type)
        session = get_session(session_id)
        
        # Phase 5A2: Create transport based on endpoint type
        try:
            MCPLogger.log(TOOL_LOG_NAME, f"DEBUG: About to check transport_type, value is {repr(transport_type)}")
            if transport_type == "serial":
                # Serial transport: open port with DTR/RTS workarounds
                MCPLogger.log(TOOL_LOG_NAME, f"Opening serial port: {connection_params['port']} (baud: {baud_rate}, hwflow: {hardware_flow_control})")
                
                serial_port = open_serial_port_with_dtr_rts_workaround(
                    endpoint=connection_params["port"],
                    baud_rate=baud_rate,
                    set_dtr=set_dtr,
                    set_rts=set_rts,
                    hardware_flow_control=hardware_flow_control,
                    software_flow_control=software_flow_control,
                    parity=parity,
                    stopbits=stopbits,
                    bytesize=bytesize
                )
                
                # Phase 5A1: Wrap serial port in transport abstraction
                session.transport = SerialTransport(serial_port)
                
                # Keep serial_port temporarily during migration
                session.serial_port = serial_port
            
            elif transport_type == "tcp":
                # TCP transport: raw socket connection
                host = connection_params["host"]
                port = connection_params["port"]
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening TCP connection: {host}:{port} (timeout: {connect_timeout}s)")
                
                # Create TCP transport (connection happens in __init__)
                session.transport = TCPTransport(host, port, connect_timeout)
                
                MCPLogger.log(TOOL_LOG_NAME, f"TCP transport created successfully for {host}:{port}")
            
            elif transport_type == "telnet":
                # Telnet transport: TCP + IAC protocol handling (Phase 5B)
                host = connection_params["host"]
                port = connection_params["port"]
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening Telnet connection: {host}:{port} (timeout: {connect_timeout}s)")
                
                # Create TCP transport first
                tcp_transport = TCPTransport(host, port, connect_timeout)
                
                # Wrap in telnet protocol layer
                session.transport = TelnetTransport(tcp_transport, raw_mode=False)
                
                MCPLogger.log(TOOL_LOG_NAME, f"Telnet transport created successfully for {host}:{port}")
            
            elif transport_type == "rfc2217":
                # RFC2217 transport: TCP + Telnet + COM-PORT-OPTION (Phase 5G)
                host = connection_params["host"]
                port = connection_params["port"]
                
                # RFC2217-specific parameters
                rfc2217_baud = params.get("rfc2217_baud", 115200)
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening RFC2217 connection: {host}:{port} (timeout: {connect_timeout}s, baud: {rfc2217_baud})")
                
                # Create TCP transport first
                tcp_transport = TCPTransport(host, port, connect_timeout)
                
                # Wrap in RFC2217 protocol layer (includes telnet + COM-PORT-OPTION)
                session.transport = RFC2217Transport(tcp_transport)
                
                # Set initial baud rate if specified
                if rfc2217_baud != 115200:  # Only set if different from default
                    session.transport.set_baud_rate(rfc2217_baud)
                
                MCPLogger.log(TOOL_LOG_NAME, f"RFC2217 transport created successfully for {host}:{port}")
            
            elif transport_type == "ssh":
                # SSH transport: Secure shell with PTY (Phase 5C)
                host = connection_params["host"]
                port = connection_params.get("port", 22)
                
                # Username from endpoint takes precedence over parameter
                username = connection_params.get("username") or ssh_username
                
                if not username:
                    raise ValueError("SSH requires username (in endpoint ssh://user@host:port or via ssh_username parameter)")
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening SSH connection: {username}@{host}:{port} (timeout: {connect_timeout}s)")
                
                # Create SSH transport (connection + auth + PTY happens in __init__)
                session.transport = SSHTransport(
                    host=host,
                    port=port,
                    username=username,
                    password=ssh_password,
                    key_filename=ssh_key_filename,
                    key_data=ssh_key_data,
                    key_password=ssh_key_password,
                    allow_unknown_hosts=ssh_allow_unknown_hosts,
                    connect_timeout=connect_timeout,
                    terminal_type=ssh_terminal_type,
                    terminal_width=ssh_terminal_width,
                    terminal_height=ssh_terminal_height,
                    compression=ssh_compression,
                    otp_secret=ssh_otp_secret,
                    otp_code=ssh_otp_code,
                    allow_agent=ssh_allow_agent
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"SSH transport created successfully for {username}@{host}:{port}")
            
            elif transport_type == "program":
                # Program transport: Local process with PTY (Phase 5E)
                command = connection_params["command"]
                
                # Program-specific parameters (Phase 5E)
                program_args = params.get("program_args", [])
                program_env = params.get("program_env")  # Dict of env vars (None = inherit)
                program_cwd = params.get("program_cwd")  # Working directory (None = current)
                terminal_width = params.get("terminal_width", ssh_terminal_width)  # Reuse SSH default
                terminal_height = params.get("terminal_height", ssh_terminal_height)  # Reuse SSH default
                
                # Elevation parameters (Phase 5K)
                elevated = params.get("elevated", False)
                elevation_password = params.get("elevation_password")
                
                elevation_status = " (elevated)" if elevated else ""
                MCPLogger.log(TOOL_LOG_NAME, f"Spawning program{elevation_status}: {command} {program_args} (cwd: {program_cwd or 'current'})")
                
                # Create program transport (process spawns in __init__)
                session.transport = ProgramTransport(
                    command=command,
                    args=program_args,
                    env=program_env,
                    cwd=program_cwd,
                    cols=terminal_width,
                    rows=terminal_height,
                    elevated=elevated,
                    elevation_password=elevation_password
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"Program transport created successfully for {command}")
            
            elif transport_type == "unix":
                # Unix domain socket transport (Phase 5F)
                socket_path = connection_params["socket_path"]
                
                MCPLogger.log(TOOL_LOG_NAME, f"Connecting to Unix socket: {socket_path} (timeout: {connect_timeout}s)")
                
                # Create Unix socket transport (connection happens in __init__)
                session.transport = UnixSocketTransport(
                    socket_path=socket_path,
                    timeout=connect_timeout
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"Unix socket transport created successfully for {socket_path}")
            
            elif transport_type == "pipe":
                # Named pipe / FIFO transport (Phase 5F)
                pipe_path = connection_params["pipe_path"]
                
                # Pipe parameters
                pipe_mode = params.get("pipe_mode", "rw")  # "r", "w", or "rw"
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening named pipe: {pipe_path} (mode: {pipe_mode}, timeout: {connect_timeout}s)")
                
                # Create named pipe transport (opens in __init__)
                session.transport = NamedPipeTransport(
                    pipe_path=pipe_path,
                    timeout=connect_timeout,
                    mode=pipe_mode
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"Named pipe transport created successfully for {pipe_path}")
            
            elif transport_type == "websocket":
                # WebSocket transport: ws:// or wss:// (Phase 5H)
                url = connection_params["url"]
                
                # WebSocket-specific parameters
                ws_headers = params.get("ws_headers", {})  # Optional HTTP headers for handshake
                ws_mode = params.get("ws_mode", connection_params.get("ws_mode", "auto"))  # Frame mode: auto/text/binary
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening WebSocket connection: {url} (timeout: {connect_timeout}s, mode: {ws_mode})")
                
                # Create WebSocket transport (connection happens in __init__)
                session.transport = WebSocketTransport(
                    url=url,
                    timeout=connect_timeout,
                    headers=ws_headers,
                    ws_mode=ws_mode
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"WebSocket transport created successfully for {url}")
            
            elif transport_type == "bluetooth":
                # Classic Bluetooth (RFCOMM/SPP) transport: bt://address (Phase 5J-1)
                address = connection_params["address"]
                
                # Bluetooth-specific parameters
                bt_port = params.get("bt_port", 1)  # RFCOMM channel (default 1 for SPP)
                bt_pin = params.get("bt_pin", None)  # Optional PIN for pairing
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening Classic Bluetooth connection: {address}:{bt_port} (timeout: {connect_timeout}s)")
                
                # Create Bluetooth transport (connection happens in __init__)
                session.transport = BluetoothTransport(
                    address=address,
                    port=bt_port,
                    pin=bt_pin,
                    timeout=connect_timeout
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"Bluetooth transport created successfully for {address}:{bt_port}")
            
            elif transport_type == "ble":
                # Bluetooth Low Energy (BLE/GATT) transport: ble://address (Phase 5J-2)
                address = connection_params["address"]
                
                # BLE-specific parameters
                ble_mode = params.get("ble_mode", "uart")  # "uart" or "generic"
                
                MCPLogger.log(TOOL_LOG_NAME, f"Opening BLE connection: {address} (mode: {ble_mode}, timeout: {connect_timeout}s)")
                
                # Create BLE transport (connection happens in __init__)
                session.transport = BLETransport(
                    address=address,
                    timeout=connect_timeout,
                    ble_mode=ble_mode
                )
                
                MCPLogger.log(TOOL_LOG_NAME, f"BLE transport created successfully for {address}")
            
            else:
                raise ValueError(f"Unsupported transport type: {transport_type}")
            
            # Phase 2D: Create queues for unified worker thread
            session.command_queue = queue.Queue()  # MCP -> Worker commands
            session.response_queue = queue.Queue()  # Worker -> MCP responses
            session.output_queue = queue.Queue()  # Worker -> MCP data stream
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} created: output_queue_id={id(session.output_queue)}")
            
            # Phase 2D: Start single unified worker thread (owns the port!)
            session.worker_stop_event = threading.Event()
            session.worker_thread = threading.Thread(
                target=serial_port_worker_thread,
                args=(session_id, session.worker_stop_event),
                daemon=True,
                name=f"MCU_Serial_Worker_{session_id}"
            )
            session.worker_thread.start()
            
            MCPLogger.log(TOOL_LOG_NAME, f"Worker thread started for session {session_id} - single thread owns transport")
            
            # Build result based on transport type
            result = {
                "success": True,
                "session_id": session_id,
                "endpoint": endpoint,
                "transport_type": transport_type,
                "log_file_path": str(session.metadata.log_file_path),
            }
            
            if transport_type == "serial":
                # Add serial-specific info
                result.update({
                    "baud_rate": baud_rate,
                    "config": f"{parity}{bytesize}{stopbits}".replace(".", "_"),
                    "dtr": set_dtr,
                    "rts": set_rts,
                    "hardware_flow_control": hardware_flow_control,
                    "software_flow_control": software_flow_control,
                })
                result["message"] = f"Session {session_id} opened successfully. Serial transport active, worker thread running."
            
            elif transport_type == "tcp":
                # Add TCP-specific info
                result.update({
                    "host": connection_params["host"],
                    "port": connection_params["port"],
                    "connect_timeout": connect_timeout,
                })
                result["message"] = f"Session {session_id} opened successfully. TCP transport active, worker thread running."
            
            elif transport_type == "telnet":
                # Add Telnet-specific info (Phase 5B)
                result.update({
                    "host": connection_params["host"],
                    "port": connection_params["port"],
                    "connect_timeout": connect_timeout,
                    "iac_processing": True,  # IAC commands will be parsed/stripped
                })
                result["message"] = f"Session {session_id} opened successfully. Telnet transport active (IAC processing enabled), worker thread running."
            
            elif transport_type == "rfc2217":
                # Add RFC2217-specific info (Phase 5G)
                rfc2217_baud = params.get("rfc2217_baud", 115200)
                result.update({
                    "host": connection_params["host"],
                    "port": connection_params["port"],
                    "connect_timeout": connect_timeout,
                    "protocol": "RFC2217 (Telnet + COM-PORT-OPTION)",
                    "baud_rate": rfc2217_baud,
                    "serial_control": "DTR/RTS/BREAK available",
                })
                result["message"] = f"Session {session_id} opened successfully. RFC2217 transport active (remote serial port control enabled), worker thread running."
            
            elif transport_type == "ssh":
                # Add SSH-specific info to result (Phase 5C)
                username = connection_params.get("username") or ssh_username
                result.update({
                    "host": connection_params["host"],
                    "port": connection_params.get("port", 22),
                    "username": username,
                    "connect_timeout": connect_timeout,
                    "terminal_type": ssh_terminal_type,
                    "terminal_size": f"{ssh_terminal_width}x{ssh_terminal_height}",
                    "allow_unknown_hosts": ssh_allow_unknown_hosts,
                })
                result["message"] = f"Session {session_id} opened successfully. SSH transport active (PTY allocated), worker thread running."
            
            elif transport_type == "program":
                # Add Program-specific info to result (Phase 5E)
                program_args = params.get("program_args", [])
                program_cwd = params.get("program_cwd")
                terminal_width = params.get("terminal_width", 80)
                terminal_height = params.get("terminal_height", 24)
                result.update({
                    "command": connection_params["command"],
                    "args": program_args,
                    "cwd": program_cwd or "(current directory)",
                    "terminal_size": f"{terminal_width}x{terminal_height}",
                    "exit_code": session.transport.exit_code,  # Will be None until process exits
                })
                result["message"] = f"Session {session_id} opened successfully. Program transport active (PTY allocated), worker thread running."
            
            elif transport_type == "unix":
                # Add Unix socket info to result (Phase 5F)
                result.update({
                    "socket_path": connection_params["socket_path"],
                })
                result["message"] = f"Session {session_id} opened successfully. Unix socket transport active, worker thread running."
            
            elif transport_type == "pipe":
                # Add Named pipe info to result (Phase 5F)
                pipe_mode = params.get("pipe_mode", "rw")
                result.update({
                    "pipe_path": connection_params["pipe_path"],
                    "pipe_mode": pipe_mode,
                })
                result["message"] = f"Session {session_id} opened successfully. Named pipe transport active, worker thread running."
            
            elif transport_type == "websocket":
                # Add WebSocket info to result (Phase 5H)
                ws_headers = params.get("ws_headers", {})
                result.update({
                    "url": connection_params["url"],
                    "protocol": "WebSocket (ws:// or wss://)",
                    "ws_headers": ws_headers if ws_headers else "(none)",
                    "ssl_enabled": connection_params["url"].startswith("wss://"),
                })
                result["message"] = f"Session {session_id} opened successfully. WebSocket transport active, worker thread running."
            
            elif transport_type == "bluetooth":
                # Add Bluetooth info to result (Phase 5J-1)
                bt_port = params.get("bt_port", 1)
                bt_pin = params.get("bt_pin", None)
                result.update({
                    "address": connection_params["address"],
                    "protocol": "Classic Bluetooth (RFCOMM/SPP)",
                    "bt_port": bt_port,
                    "bt_pin": "****" if bt_pin else "(none)",  # Mask PIN for security
                })
                result["message"] = f"Session {session_id} opened successfully. Bluetooth transport active, worker thread running."
            
            elif transport_type == "ble":
                # Add BLE info to result (Phase 5J-2)
                ble_mode = params.get("ble_mode", "uart")
                result.update({
                    "address": connection_params["address"],
                    "protocol": "Bluetooth Low Energy (BLE/GATT)",
                    "ble_mode": ble_mode,
                })
                result["message"] = f"Session {session_id} opened successfully. BLE transport active (mode: {ble_mode}), worker thread running."
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
            
        except TransportConnectionError as e:
            # Phase 5A2: Transport connection failed - clean up session
            close_session(session_id)
            raise Exception(f"Failed to connect to {endpoint}: {str(e)}")
        
        except Exception as e:
            # Other errors during transport creation - clean up session
            close_session(session_id)
            raise Exception(f"Failed to open transport for {endpoint}: {str(e)}")
        
    except Exception as e:
        return create_error_response(f"Error opening session: {str(e)}", with_readme=False)

def handle_close_session(params: Dict) -> Dict:
    """Handle close_session operation"""
    try:
        session_id = params.get("session_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for close_session", with_readme=False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Closing session: {session_id}")
        
        success, message = close_session(session_id)
        
        if success:
            result = {
                "success": True,
                "session_id": session_id,
                "message": message
            }
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
        else:
            return create_error_response(message, with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error closing session: {str(e)}", with_readme=False)

def handle_list_sessions(params: Dict) -> Dict:
    """Handle list_sessions operation"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Listing all sessions")
        
        sessions = list_active_sessions()
        
        result = {
            "success": True,
            "total_sessions": len(sessions),
            "sessions": sessions
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error listing sessions: {str(e)}", with_readme=False)

def handle_get_session_info(params: Dict) -> Dict:
    """Handle get_session_info operation - Phase 2B: Enhanced statistics"""
    try:
        session_id = params.get("session_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for get_session_info", with_readme=False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Getting info for session: {session_id}")
        
        session = get_session(session_id)
        
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Thread-safe metadata access
        with session.metadata_lock:
            # Calculate runtime
            now = datetime.now()
            runtime_seconds = (now - session.metadata.session_start_time).total_seconds()
            
            # Phase 2B: Enhanced statistics
            hours, remainder = divmod(int(runtime_seconds), 3600)
            minutes, seconds = divmod(remainder, 60)
            session_uptime_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            # Bytes per second average
            bytes_per_second_average = 0.0
            if runtime_seconds > 0:
                total_bytes = session.metadata.total_bytes_received + session.metadata.total_bytes_sent
                bytes_per_second_average = total_bytes / runtime_seconds
            
            # Last activity age
            last_activity_age_seconds = (now - session.metadata.last_activity_time).total_seconds()
            
            # Worker thread status
            worker_thread_alive = session.worker_thread.is_alive() if session.worker_thread else False
            
            result = {
                "success": True,
                "session_id": session_id,
                "endpoint": session.metadata.endpoint,
                "transport_type": session.metadata.transport_type,
                "session_start_time": session.metadata.session_start_time.isoformat(),
                "runtime_seconds": round(runtime_seconds, 2),
                "session_uptime_formatted": session_uptime_formatted,  # Phase 2B
                "bytes_received": session.metadata.total_bytes_received,
                "bytes_sent": session.metadata.total_bytes_sent,
                "bytes_per_second_average": round(bytes_per_second_average, 2),  # Phase 2B
                "last_activity_time": session.metadata.last_activity_time.isoformat(),
                "last_activity_age_seconds": round(last_activity_age_seconds, 2),  # Phase 2B
                "log_file_path": str(session.metadata.log_file_path),
                "log_file_size_bytes": session.metadata.log_file_size_bytes,
                "is_active": session.metadata.is_active,
                "worker_thread_alive": worker_thread_alive  # Phase 2B
            }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error getting session info: {str(e)}", with_readme=False)

def handle_list_ports(params: Dict) -> Dict:
    """Handle list_ports operation - Phase 2A"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Listing available serial ports")
        
        ports = list_available_serial_ports()
        
        result = {
            "success": True,
            "total_ports": len(ports),
            "ports": ports
        }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error listing ports: {str(e)}", with_readme=False)


def handle_discover_network(params: Dict) -> Dict:
    """Handle discover_network operation - Phase 5D: mDNS/DNS-SD device discovery"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Starting network device discovery via mDNS/DNS-SD")
        
        # Optional: user can specify which service types to search for
        service_types = params.get("service_types")
        if service_types and not isinstance(service_types, list):
            return create_error_response("Parameter 'service_types' must be a list of strings", with_readme=False)
        
        # Optional: timeout for discovery (default 5 seconds)
        read_timeout = params.get("read_timeout", 5.0)
        if not isinstance(read_timeout, (int, float)) or read_timeout <= 0:
            return create_error_response("Parameter 'read_timeout' must be a positive number", with_readme=False)
        
        # Perform discovery
        devices = discover_network_devices(service_types=service_types, timeout_seconds=read_timeout)
        
        result = {
            "success": True,
            "total_devices": len(devices),
            "read_timeout": read_timeout,
            "devices": devices
        }
        
        # Add helpful message
        if len(devices) == 0:
            result["message"] = "No devices discovered. Ensure devices are powered on and mDNS is working on your platform."
            result["platform_notes"] = {
                "windows": "Requires Bonjour service (install iTunes or Bonjour Print Services)",
                "linux_wsl": "Should work out-of-box with Avahi daemon",
                "macos": "Should work out-of-box with native mDNS support"
            }
        else:
            result["message"] = f"Discovered {len(devices)} device(s) on local network"
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error discovering network devices: {str(e)}", with_readme=False)


def handle_discover_bluetooth(params: Dict) -> Dict:
    """Handle discover_bluetooth operation - Phase 5J-1: Classic Bluetooth device discovery"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Starting Classic Bluetooth device discovery")
        
        # Try to load pybluez (graceful fallback if not available)
        bluetooth = ensure_pybluez()
        if bluetooth is None:
            return create_error_response(
                "pybluez library not available. Classic Bluetooth discovery requires pybluez. "
                "Please install pre-built wheel or compile from source: pip install pybluez",
                with_readme=False
            )
        
        # Optional: timeout for discovery (default 10 seconds)
        duration = params.get("duration", 10.0)
        if not isinstance(duration, (int, float)) or duration <= 0:
            return create_error_response("Parameter 'duration' must be a positive number", with_readme=False)
        
        # Optional: filter by service (default None to see ALL devices - hardware hackers want to see everything!)
        service = params.get("service", None)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Scanning for Classic Bluetooth devices (duration: {duration}s, service: {service})...")
        
        # Discover nearby devices
        nearby_devices = bluetooth.discover_devices(duration=int(duration), lookup_names=True, lookup_class=True)
        
        devices = []
        for addr, name, device_class in nearby_devices:
            device_info = {
                "address": addr,
                "name": name if name else "(unknown)",
                "device_class": device_class,
            }
            
            # If filtering by service, check if device supports it
            if service == "serial":
                # Check if device supports Serial Port Profile (SPP)
                # This is a simplified check - full SDP query would be more accurate
                try:
                    services = bluetooth.find_service(address=addr)
                    has_spp = any("Serial" in str(s.get("name", "")) or s.get("protocol") == "RFCOMM" for s in services)
                    if has_spp:
                        device_info["service"] = "SPP"
                        devices.append(device_info)
                except Exception as e:
                    MCPLogger.log(TOOL_LOG_NAME, f"Could not query services for {addr}: {e}")
                    # Include device anyway if service query fails
                    device_info["service"] = "(unknown)"
                    devices.append(device_info)
            elif service is None:
                # No filtering - include ALL devices (hardware hackers want to see everything!)
                # Optionally try to identify services for informational purposes
                try:
                    services = bluetooth.find_service(address=addr)
                    if services:
                        service_names = [s.get("name", "(unknown)") for s in services[:3]]  # First 3 services
                        device_info["services"] = service_names
                    else:
                        device_info["services"] = []
                except Exception as e:
                    device_info["services"] = f"(query failed: {str(e)})"
                devices.append(device_info)
            else:
                # Custom service filter (future expansion)
                devices.append(device_info)
        
        result = {
            "success": True,
            "total_devices": len(devices),
            "duration": duration,
            "service_filter": service,
            "devices": devices
        }
        
        if len(devices) == 0:
            result["message"] = "No Classic Bluetooth devices discovered. Ensure devices are powered on, paired, and in range."
        else:
            result["message"] = f"Discovered {len(devices)} Classic Bluetooth device(s)"
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error discovering Classic Bluetooth devices: {str(e)}", with_readme=False)


def handle_bleak_operation(params: Dict) -> Dict:
    """Handle bleak operation - PLACEHOLDER: Awaiting architectural review
    
    This operation will expose bleak's API directly for generic BLE operations.
    Implementation pending architectural review of session management and async integration.
    """
    return {
        "success": False,
        "error": "Operation 'bleak' is currently unavailable pending architectural review. " +
                 "This feature will provide direct access to bleak's BLE GATT operations. " +
                 "For now, use 'open_session' with 'ble://address' endpoint for Nordic UART Service support."
    }


def handle_bleak_get_notifications(params: Dict) -> Dict:
    """Handle bleak_get_notifications operation - PLACEHOLDER: Awaiting architectural review
    
    This operation will poll queued BLE notifications from subscribed characteristics.
    Implementation pending architectural review of notification queue management.
    """
    return {
        "success": False,
        "error": "Operation 'bleak_get_notifications' is currently unavailable pending architectural review. " +
                 "This feature will provide access to queued BLE notifications."
    }


def handle_bleak_disconnect(params: Dict) -> Dict:
    """Handle bleak_disconnect operation - PLACEHOLDER: Awaiting architectural review
    
    This operation will explicitly disconnect from a BLE device.
    Implementation pending architectural review of connection lifecycle management.
    """
    return {
        "success": False,
        "error": "Operation 'bleak_disconnect' is currently unavailable pending architectural review. " +
                 "This feature will provide explicit BLE disconnection control."
    }


def handle_discover_ble(params: Dict) -> Dict:
    """Handle discover_ble operation - Phase 5J-2: BLE device discovery with full service details"""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Starting BLE device discovery")
        
        # Ensure bleak is available (auto-install if missing)
        bleak = ensure_bleak()
        
        # Optional: timeout for discovery (default 10 seconds)
        duration = params.get("duration", 10.0)
        if not isinstance(duration, (int, float)) or duration <= 0:
            return create_error_response("Parameter 'duration' must be a positive number", with_readme=False)
        
        # Optional: include full service/characteristic details (default True)
        include_services = params.get("include_services", True)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Scanning for BLE devices (duration: {duration}s, include_services: {include_services})...")
        
        import asyncio
        
        # Run async discovery in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            try:
                devices = loop.run_until_complete(_async_discover_ble(duration, include_services))
            except Exception as e:
                # Auto-enable logic for Windows "Device not ready" error (WinError -2147020577)
                # This happens when the Bluetooth radio is soft-disabled in Settings
                error_str = str(e)
                if "WinError -2147020577" in error_str or "device is not ready" in error_str.lower():
                    MCPLogger.log(TOOL_LOG_NAME, "Bluetooth appears disabled. Attempting auto-enable...")
                    
                    # Try to enable using our new handler
                    # Note: This uses winsdk if available, or fails gracefully
                    enable_result = handle_enable_bluetooth({"enabled": True})
                    
                    if not enable_result.get("isError"):
                        MCPLogger.log(TOOL_LOG_NAME, f"Bluetooth auto-enable result: {enable_result['content'][0]['text']}. Retrying discovery...")
                        # Give the radio a moment to wake up
                        import time
                        time.sleep(2.0)
                        # Retry once
                        devices = loop.run_until_complete(_async_discover_ble(duration, include_services))
                    else:
                        MCPLogger.log(TOOL_LOG_NAME, f"Auto-enable failed: {enable_result['content'][0]['text']}")
                        raise e
                else:
                    raise e
        finally:
            loop.close()
        
        result = {
            "success": True,
            "total_devices": len(devices),
            "duration": duration,
            "include_services": include_services,
            "devices": devices
        }
        
        if len(devices) == 0:
            result["message"] = "No BLE devices discovered. Ensure devices are powered on, advertising, and in range."
            result["platform_notes"] = {
                "windows": "Requires Windows 10+ with built-in BLE support",
                "linux": "Requires bluez package with BLE support",
                "macos": "Built-in BLE support"
            }
        else:
            result["message"] = f"Discovered {len(devices)} BLE device(s)"
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error discovering BLE devices: {str(e)}", with_readme=False)


async def _async_discover_ble(duration: float, include_services: bool):
    """Async BLE discovery helper."""
    import bleak
    
    def _serialize_ble_object(obj):
        """Helper to serialize Bleak objects dynamically to ensure future properties are included."""
        result = {}
        # Iterate over all attributes to catch future fields automatically
        for attr_name in dir(obj):
            if attr_name.startswith("_"):
                continue
            
            try:
                val = getattr(obj, attr_name)
            except Exception:
                continue
                
            if callable(val):
                continue
                
            # Skip huge/non-serializable platform objects
            if attr_name in ["details", "platform_data"]:
                continue
                
            # Handle data types
            if isinstance(val, (bytes, bytearray)):
                result[attr_name] = val.hex()
            elif isinstance(val, dict):
                # Handle dictionaries (e.g. manufacturer_data, service_data)
                new_dict = {}
                for k, v in val.items():
                    key_str = str(k)
                    if isinstance(v, (bytes, bytearray)):
                        new_dict[key_str] = v.hex()
                    else:
                        new_dict[key_str] = str(v)
                result[attr_name] = new_dict
            elif isinstance(val, (list, tuple, set)):
                # Handle lists (e.g. service_uuids)
                new_list = []
                for item in val:
                    if isinstance(item, (bytes, bytearray)):
                        new_list.append(item.hex())
                    else:
                        new_list.append(str(item))
                result[attr_name] = new_list
            else:
                # Default to string representation for UUIDs and unknown types, keep primitives
                if type(val) in (str, int, float, bool, type(None)):
                    result[attr_name] = val
                else:
                    result[attr_name] = str(val)
                    
        return result

    devices = []
    
    # Scan for devices with advertisement data (required for RSSI in newer bleak versions)
    scanner = bleak.BleakScanner()
    # return_adv=True returns a dict: {address: (device, advertisement_data)}
    discovered = await scanner.discover(timeout=duration, return_adv=True)
    
    for address, (device, adv_data) in discovered.items():
        # Dynamically serialize both objects and merge them
        # This ensures any new fields in Bleak/BlueZ/Windows are automatically surfaced
        device_info = _serialize_ble_object(device)
        adv_info = _serialize_ble_object(adv_data)
        
        # Merge advertisement data into device info (adv data takes precedence for duplicates like RSSI)
        device_info.update(adv_info)
        
        # Ensure essential fields exist even if serialization behaved oddly
        if "address" not in device_info: device_info["address"] = device.address
        if "name" not in device_info: device_info["name"] = device.name or "(unknown)"
        
        # If requested, connect and discover services/characteristics
        if include_services:
            try:
                async with bleak.BleakClient(device.address, timeout=5.0) as client:
                    services = []
                    
                    for service in client.services:
                        service_info = {
                            "uuid": str(service.uuid),
                            "description": service.description if hasattr(service, 'description') else "(unknown)",
                            "characteristics": []
                        }
                        
                        for char in service.characteristics:
                            char_info = {
                                "uuid": str(char.uuid),
                                "description": char.description if hasattr(char, 'description') else "(unknown)",
                                "properties": char.properties
                            }
                            service_info["characteristics"].append(char_info)
                        
                        services.append(service_info)
                    
                    device_info["services"] = services
                    
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Could not connect to {device.address} for service discovery: {e}")
                device_info["services"] = f"(error: {str(e)})"
        
        devices.append(device_info)
    
    return devices


def handle_send_data(params: Dict) -> Dict:
    """Handle send_data operation - Phase 5A2: Works with all transports"""
    try:
        session_id = params.get("session_id")
        data = params.get("data")
        raw_bytes = params.get("raw_bytes")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for send_data", with_readme=False)
        
        if not data and not raw_bytes:
            return create_error_response("Either 'data' or 'raw_bytes' parameter is required for send_data", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Phase 5A2: Check for transport (not serial_port)
        if not session.transport:
            return create_error_response(f"Session {session_id} has no active transport", with_readme=False)
        
        # Convert data to bytes
        if raw_bytes:
            # Assume raw_bytes is already bytes or hex string
            if isinstance(raw_bytes, bytes):
                bytes_to_send = raw_bytes
            else:
                # Try to convert hex string like "03 04" or "0304"
                try:
                    hex_clean = raw_bytes.replace(' ', '').replace('0x', '')
                    bytes_to_send = bytes.fromhex(hex_clean)
                except ValueError:
                    return create_error_response(f"Invalid raw_bytes format: {raw_bytes}", with_readme=False)
        else:
            # Parse control characters from data string
            bytes_to_send = parse_control_characters(data)
        
        # Phase 2D: Send command to worker thread (never touch port directly!)
        MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} sending {len(bytes_to_send)} bytes via command queue")
        
        try:
            # Phase 2E: Get configurable timeout (default 5.0s)
            response_timeout = params.get("response_timeout", 5.0)
            
            # Send write command to worker
            session.command_queue.put(("write", bytes_to_send))
            
            # Wait for response from worker (synchronous operation)
            status, bytes_sent = session.response_queue.get(timeout=response_timeout)
            
            if status != "ok":
                return create_error_response(f"Worker thread error: {bytes_sent}", with_readme=False)
            
            # Get total_bytes_sent from metadata (thread-safe)
            with session.metadata_lock:
                total = session.metadata.total_bytes_sent
            
            result = {
                "success": True,
                "session_id": session_id,
                "bytes_sent": bytes_sent,
                "total_bytes_sent": total
            }
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
            
        except queue.Empty:
            return create_error_response(f"Timeout waiting for worker thread response", with_readme=False)
        except Exception as e:
            return create_error_response(f"Error sending to worker: {str(e)}", with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error in send_data: {str(e)}", with_readme=False)

def handle_wait_for_pattern(params: Dict) -> Dict:
    """
    Handle wait_for_pattern operation - Phase 2B/4B: Smart queue reading with pattern detection.
    
    Reads from output_queue until pattern found or timeout or idle timeout.
    Foundation for Phase 3 sequences!
    """
    try:
        session_id = params.get("session_id")
        pattern = params.get("pattern")
        read_timeout = params.get("read_timeout", 5.0)
        idle_timeout_seconds = params.get("idle_timeout_seconds")  # Phase 4B: Optional idle timeout
        max_bytes = params.get("max_bytes", 65536)  # 64KB default limit
        use_regex = params.get("use_regex", False)
        include_hex = params.get("include_hex", False)  # Optional hex output (default False to reduce bandwidth)
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for wait_for_pattern", with_readme=False)
        
        if not pattern:
            return create_error_response("Parameter 'pattern' is required for wait_for_pattern", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        if not session.output_queue:
            return create_error_response(f"Session {session_id} has no active reader", with_readme=False)
        
        idle_str = f", idle: {idle_timeout_seconds}s" if idle_timeout_seconds else ""
        MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} waiting for pattern: {pattern} (timeout: {read_timeout}s{idle_str})")
        MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} wait_for_pattern starting - will drain queue (queue_id={id(session.output_queue)} qsize={session.output_queue.qsize()})")
        
        # Compile regex if requested
        pattern_matcher = None
        if use_regex:
            try:
                pattern_matcher = re.compile(pattern.encode('utf-8') if isinstance(pattern, str) else pattern)
            except re.error as e:
                return create_error_response(f"Invalid regex pattern: {e}", with_readme=False)
        else:
            # Convert pattern to bytes for matching
            pattern_bytes = pattern.encode('utf-8') if isinstance(pattern, str) else pattern
        
        # Collect data from queue until pattern found or timeout or idle timeout
        collected_data = bytearray()
        start_time = time.time()
        last_data_time = start_time  # Phase 4B: Track last data arrival
        pattern_found = False
        got_data = False
        idle_timeout_reached = False
        
        while True:
            remaining_time = read_timeout - (time.time() - start_time)
            
            if remaining_time <= 0:
                break
            
            # Phase 4B: Check idle timeout (time since last data)
            if idle_timeout_seconds and got_data:
                idle_elapsed = time.time() - last_data_time
                if idle_elapsed >= idle_timeout_seconds:
                    idle_timeout_reached = True
                    MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} idle timeout reached ({idle_elapsed:.1f}s >= {idle_timeout_seconds}s), pattern not found")
                    break
            
            if len(collected_data) >= max_bytes:
                MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} reached max_bytes limit: {max_bytes}")
                break
            
            try:
                # Try to get data from queue
                msg_type, msg_data = session.output_queue.get(timeout=min(remaining_time, 0.1))
                data_len = len(msg_data) if msg_type == 'data' else len(str(msg_data))
                MCPLogger.log(TOOL_LOG_NAME, f"QUEUE GET session={session_id} type={msg_type} len={data_len} qsize={session.output_queue.qsize()}")
                
                if msg_type == 'data':
                    collected_data.extend(msg_data)
                    last_data_time = time.time()  # Phase 4B: Update last data time
                    got_data = True
                    
                    # Check for pattern
                    if use_regex:
                        if pattern_matcher.search(collected_data):
                            pattern_found = True
                            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} pattern matched (regex)")
                            break
                    else:
                        if pattern_bytes in collected_data:
                            pattern_found = True
                            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} pattern matched")
                            break
                    
                elif msg_type == 'error':
                    return create_error_response(f"Serial error: {msg_data}", with_readme=False)
                    
            except queue.Empty:
                # No data available, continue waiting or timeout
                continue
        
        # Convert to string (try UTF-8 first, fall back to latin-1 if needed)
        try:
            data_str = collected_data.decode('utf-8')
        except UnicodeDecodeError:
            data_str = collected_data.decode('latin-1', errors='replace')
        
        result = {
            "success": True,
            "session_id": session_id,
            "pattern": pattern,
            "pattern_found": pattern_found,
            "bytes_read": len(collected_data),
            "data": data_str,
            "timeout_reached": (time.time() - start_time) >= read_timeout,
            "idle_timeout_reached": idle_timeout_reached,  # Phase 4B: Indicate if stopped due to silence
            "elapsed_seconds": round(time.time() - start_time, 3)
        }
        
        # Conditionally include hex data (default False to reduce bandwidth)
        if include_hex:
            result["data_hex"] = collected_data.hex()
        
        # Phase 4B: Only include idle_timeout_seconds in result if it was specified
        if idle_timeout_seconds is not None:
            result["idle_timeout_seconds"] = idle_timeout_seconds
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error in wait_for_pattern: {str(e)}", with_readme=False)

def handle_read_data(params: Dict) -> Dict:
    """Handle read_data operation - Phase 2A/4B: Timeout-based read with optional idle timeout"""
    try:
        session_id = params.get("session_id")
        read_timeout = params.get("read_timeout", 1.0)
        idle_timeout_seconds = params.get("idle_timeout_seconds")  # Phase 4B: Optional idle timeout
        include_hex = params.get("include_hex", False)  # Optional hex output (default False to reduce bandwidth)
        max_bytes = params.get("max_bytes", 65536)  # 64KB default limit
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for read_data", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        if not session.output_queue:
            return create_error_response(f"Session {session_id} has no active reader", with_readme=False)
        
        idle_str = f", idle: {idle_timeout_seconds}s" if idle_timeout_seconds else ""
        MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} reading data (timeout: {read_timeout}s{idle_str}, max: {max_bytes} bytes)")
        MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} read_data using output_queue_id={id(session.output_queue)} qsize={session.output_queue.qsize()}")
        
        # Collect data from queue until timeout or max_bytes or idle timeout
        collected_data = bytearray()
        start_time = time.time()
        last_data_time = start_time  # Phase 4B: Track last data arrival
        got_data = False
        idle_timeout_reached = False
        
        while True:
            remaining_time = read_timeout - (time.time() - start_time)
            
            if remaining_time <= 0:
                break
            
            # Phase 4B: Check idle timeout (time since last data)
            if idle_timeout_seconds and got_data:
                idle_elapsed = time.time() - last_data_time
                if idle_elapsed >= idle_timeout_seconds:
                    idle_timeout_reached = True
                    MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} idle timeout reached ({idle_elapsed:.1f}s >= {idle_timeout_seconds}s)")
                    break
            
            if len(collected_data) >= max_bytes:
                MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} reached max_bytes limit: {max_bytes}")
                break
            
            try:
                # Try to get data from queue
                msg_type, msg_data = session.output_queue.get(timeout=min(remaining_time, 0.1))
                data_len = len(msg_data) if msg_type == 'data' else len(str(msg_data))
                MCPLogger.log(TOOL_LOG_NAME, f"QUEUE GET session={session_id} type={msg_type} len={data_len} qsize={session.output_queue.qsize()}")
                
                if msg_type == 'data':
                    collected_data.extend(msg_data)
                    last_data_time = time.time()  # Phase 4B: Update last data time
                    got_data = True
                elif msg_type == 'error':
                    return create_error_response(f"Serial error: {msg_data}", with_readme=False)
                    
            except queue.Empty:
                # No data available, continue waiting or timeout
                if got_data:
                    # If we got some data and queue is now empty, wait a tiny bit more for any stragglers
                    time.sleep(0.05)
                continue
        
        # Convert to string (try UTF-8 first, fall back to latin-1 if needed)
        try:
            data_str = collected_data.decode('utf-8')
        except UnicodeDecodeError:
            data_str = collected_data.decode('latin-1', errors='replace')
        
        result = {
            "success": True,
            "session_id": session_id,
            "bytes_read": len(collected_data),
            "data": data_str,
            "timeout_reached": (time.time() - start_time) >= read_timeout,
            "idle_timeout_reached": idle_timeout_reached  # Phase 4B: Indicate if stopped due to silence
        }
        
        # Conditionally include hex data (default False to reduce bandwidth)
        if include_hex:
            result["data_hex"] = collected_data.hex()
        
        # Phase 4B: Only include idle_timeout_seconds in result if it was specified
        if idle_timeout_seconds is not None:
            result["idle_timeout_seconds"] = idle_timeout_seconds
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error in read_data: {str(e)}", with_readme=False)

# ============================================================================
# PHASE 2C OPERATION HANDLERS
# ============================================================================

def handle_send_async(params: Dict) -> Dict:
    """Handle send_async operation - Fire and forget with progress tracking"""
    try:
        session_id = params.get("session_id")
        file_path = params.get("file_path")
        data = params.get("data")
        operation_id = params.get("operation_id", f"async_{int(time.time()*1000)}")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for send_async", with_readme=False)
        
        if not file_path and not data:
            return create_error_response("Either 'file_path' or 'data' is required for send_async", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Phase 5A2: Check for transport (not serial_port)
        if not session.transport:
            return create_error_response(f"Session {session_id} has no active transport", with_readme=False)
        
        # Create async operation
        op = async_operation_state(
            operation_id=operation_id,
            operation_type="send_async",
            start_time=datetime.now(),
            session_id=session_id
        )
        
        # Set source
        if file_path:
            op.source_file_path = Path(file_path)
            if not op.source_file_path.exists():
                return create_error_response(f"File not found: {file_path}", with_readme=False)
            MCPLogger.log(TOOL_LOG_NAME, f"Starting async send from file: {file_path}")
        else:
            # Parse control characters
            op.inline_data = parse_control_characters(data)
            MCPLogger.log(TOOL_LOG_NAME, f"Starting async send of {len(op.inline_data)} bytes inline data")
        
        # Add to session's async operations
        with session.async_operations_lock:
            session.async_operations[operation_id] = op
        
        # Phase 2D: Queue the operation for worker thread
        session.command_queue.put(("write_async", operation_id))
        
        result = {
            "success": True,
            "operation_id": operation_id,
            "status": "pending",
            "message": f"Async operation {operation_id} started. Use get_async_status to track progress."
        }
        
        if file_path:
            result["source_file"] = file_path
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error in send_async: {str(e)}", with_readme=False)

def handle_get_async_status(params: Dict) -> Dict:
    """Handle get_async_status operation - Query progress of async operation"""
    try:
        session_id = params.get("session_id")
        operation_id = params.get("operation_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for get_async_status", with_readme=False)
        
        if not operation_id:
            return create_error_response("Parameter 'operation_id' is required for get_async_status", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Get operation status
        with session.async_operations_lock:
            if operation_id not in session.async_operations:
                return create_error_response(f"Operation {operation_id} not found in session {session_id}", with_readme=False)
            
            op = session.async_operations[operation_id]
            result = op.to_dict()
            result["success"] = True
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error in get_async_status: {str(e)}", with_readme=False)

def handle_cancel_async(params: Dict) -> Dict:
    """Handle cancel_async operation - Cancel an in-progress async operation"""
    try:
        session_id = params.get("session_id")
        operation_id = params.get("operation_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for cancel_async", with_readme=False)
        
        if not operation_id:
            return create_error_response("Parameter 'operation_id' is required for cancel_async", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Cancel operation
        with session.async_operations_lock:
            if operation_id not in session.async_operations:
                return create_error_response(f"Operation {operation_id} not found in session {session_id}", with_readme=False)
            
            op = session.async_operations[operation_id]
            
            if op.status in ["completed", "error", "cancelled"]:
                result = {
                    "success": True,
                    "operation_id": operation_id,
                    "message": f"Operation {operation_id} already finished with status: {op.status}"
                }
            else:
                op.status = "cancelled"
                op.end_time = datetime.now()
                result = {
                    "success": True,
                    "operation_id": operation_id,
                    "message": f"Operation {operation_id} cancelled successfully"
                }
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
        
    except Exception as e:
        return create_error_response(f"Error in cancel_async: {str(e)}", with_readme=False)

def handle_set_baud(params: Dict) -> Dict:
    """Handle set_baud operation - Change baud rate during active session"""
    try:
        session_id = params.get("session_id")
        baud_rate = params.get("baud_rate")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for set_baud", with_readme=False)
        
        if not baud_rate:
            return create_error_response("Parameter 'baud_rate' is required for set_baud", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Phase 5A2: Check for transport (not serial_port)
        if not session.transport:
            return create_error_response(f"Session {session_id} has no active transport", with_readme=False)
        
        # Phase 2D: Send command to worker thread
        try:
            # Phase 2E: Get configurable timeout
            response_timeout = params.get("response_timeout", 5.0)
            
            session.command_queue.put(("set_baud", baud_rate))
            status, baud_info = session.response_queue.get(timeout=response_timeout)
            
            if status != "ok":
                return create_error_response(f"Worker thread error: {baud_info}", with_readme=False)
            
            old_baud, new_baud = baud_info
            
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} baud rate changed from {old_baud} to {new_baud}")
            
            result = {
                "success": True,
                "session_id": session_id,
                "old_baud_rate": old_baud,
                "new_baud_rate": new_baud,
                "message": f"Baud rate changed to {new_baud}"
            }
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
            
        except queue.Empty:
            return create_error_response(f"Timeout waiting for worker thread response", with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error in set_baud: {str(e)}", with_readme=False)

def handle_send_break(params: Dict) -> Dict:
    """Handle send_break operation - Send serial BREAK signal"""
    try:
        session_id = params.get("session_id")
        duration = params.get("duration", 0.25)  # Default 250ms
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for send_break", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Phase 5A2: Check for transport (not serial_port)
        if not session.transport:
            return create_error_response(f"Session {session_id} has no active transport", with_readme=False)
        
        # Phase 2D: Send command to worker thread
        try:
            # Phase 2E: Get configurable timeout
            response_timeout = params.get("response_timeout", 5.0)
            
            session.command_queue.put(("send_break", duration))
            status, _ = session.response_queue.get(timeout=response_timeout)
            
            if status != "ok":
                return create_error_response(f"Worker thread error", with_readme=False)
            
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} sent BREAK signal (duration: {duration}s)")
            
            result = {
                "success": True,
                "session_id": session_id,
                "duration": duration,
                "message": f"BREAK signal sent ({duration}s)"
            }
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
            
        except queue.Empty:
            return create_error_response(f"Timeout waiting for worker thread response", with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error in send_break: {str(e)}", with_readme=False)

def handle_get_line_states(params: Dict) -> Dict:
    """Handle get_line_states operation - Read CTS, DSR, RI, CD pin states"""
    try:
        session_id = params.get("session_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for get_line_states", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Phase 5A2: Check for transport (not serial_port)
        if not session.transport:
            return create_error_response(f"Session {session_id} has no active transport", with_readme=False)
        
        # Phase 2D: Send command to worker thread
        try:
            # Phase 2E: Get configurable timeout
            response_timeout = params.get("response_timeout", 5.0)
            
            session.command_queue.put(("get_line_states",))
            status, states = session.response_queue.get(timeout=response_timeout)
            
            if status != "ok":
                return create_error_response(f"Worker thread error", with_readme=False)
            
            result = {
                "success": True,
                "session_id": session_id,
                **states  # Unpack CTS, DSR, RI, CD, DTR, RTS
            }
            
            MCPLogger.log(TOOL_LOG_NAME, f"Session {session_id} line states: CTS={states['cts']}, DSR={states['dsr']}, RI={states['ri']}, CD={states['cd']}")
            
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
            
        except queue.Empty:
            return create_error_response(f"Timeout waiting for worker thread response", with_readme=False)
        
    except Exception as e:
        return create_error_response(f"Error in get_line_states: {str(e)}", with_readme=False)

# ============================================================================
# PHASE 3: SEQUENCE AND TERMINAL EMULATION HANDLERS
# ============================================================================

def handle_send_sequence(params: Dict) -> Dict:
    """
    Handle send_sequence operation - Phase 3: Atomic sequence execution.
    
    MUST-DO #1: Check for conflicting async TX operations before starting.
    MUST-DO #6: Support both blocking and async (fire-and-forget) modes.
    """
    try:
        session_id = params.get("session_id")
        sequence = params.get("sequence", [])
        options = params.get("options", {})
        is_async = params.get("async", False)
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required for send_sequence", with_readme=False)
        
        if not sequence:
            return create_error_response("Parameter 'sequence' is required (array of actions)", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # MUST-DO #1: Check for conflicting async TX operations
        with session.async_operations_lock:
            for op_id, op in session.async_operations.items():
                if op.status in ["pending", "in_progress"]:
                    return create_error_response(
                        f"Cannot start sequence while async upload '{op_id}' is running. "
                        f"Cancel it first or wait for completion.",
                        with_readme=False
                    )
        
        # Generate sequence ID
        import uuid
        sequence_id = params.get("sequence_id", f"seq_{uuid.uuid4().hex[:8]}")
        
        # Build options dict
        options_dict = {
            "timeout": options.get("timeout", 60.0),
            "stop_on_error": options.get("stop_on_error", True),
            "async": is_async
        }
        
        # Send to worker thread
        session.command_queue.put(("send_sequence", sequence_id, sequence, options_dict))
        
        # MUST-DO #6: If async, return immediately
        if is_async:
            # Store in active_sequences for later queries
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": True,
                    "sequence_id": sequence_id,
                    "status": "started",
                    "async": True,
                    "actions_total": len(sequence)
                }, indent=2)}],
                "isError": False
            }
        
        # Blocking mode: Wait for sequence to complete
        response_timeout = params.get("response_timeout", options_dict["timeout"] + 5.0)
        
        try:
            msg_type, result = session.response_queue.get(timeout=response_timeout)
            
            if msg_type == "sequence_success":
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": False
                }
            elif msg_type in ("sequence_error", "sequence_cancelled"):
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": True
                }
            else:
                return create_error_response(f"Unexpected response type: {msg_type}", with_readme=False)
                
        except queue.Empty:
            return create_error_response(
                f"Timeout waiting for sequence to complete (>{response_timeout}s)",
                with_readme=False
            )
    
    except Exception as e:
        return create_error_response(f"Error in send_sequence: {str(e)}", with_readme=False)

def handle_get_sequence_status(params: Dict) -> Dict:
    """Handle get_sequence_status operation - Phase 3: Query async sequence status"""
    try:
        session_id = params.get("session_id")
        sequence_id = params.get("sequence_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required", with_readme=False)
        
        if not sequence_id:
            return create_error_response("Parameter 'sequence_id' is required", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Check if sequence is currently executing
        if session.current_sequence and session.current_sequence["sequence_id"] == sequence_id:
            # Still running
            seq = session.current_sequence
            elapsed = time.time() - seq["sequence_start_time"]
            
            result = {
                "success": True,
                "sequence_id": sequence_id,
                "status": "in_progress",
                "actions_completed": len(seq["results"]),
                "actions_total": len(seq["actions"]),
                "elapsed_seconds": round(elapsed, 3)
            }
        # Check in completed sequences
        elif sequence_id in session.active_sequences:
            result = session.active_sequences[sequence_id]
        else:
            return create_error_response(f"Sequence {sequence_id} not found", with_readme=False)
        
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
    
    except Exception as e:
        return create_error_response(f"Error in get_sequence_status: {str(e)}", with_readme=False)

def handle_cancel_sequence(params: Dict) -> Dict:
    """Handle cancel_sequence operation - Phase 3: Cancel running sequence (MUST-DO #4)"""
    try:
        session_id = params.get("session_id")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Send cancel command to worker (out-of-band, processed even during sequence)
        session.command_queue.put(("cancel_sequence",))
        
        # Wait for acknowledgment
        response_timeout = params.get("response_timeout", 5.0)
        
        try:
            status, _ = session.response_queue.get(timeout=response_timeout)
            
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": True,
                    "message": "Cancel signal sent to worker thread"
                }, indent=2)}],
                "isError": False
            }
        except queue.Empty:
            return create_error_response("Timeout waiting for cancel acknowledgment", with_readme=False)
    
    except Exception as e:
        return create_error_response(f"Error in cancel_sequence: {str(e)}", with_readme=False)

def handle_set_terminal_emulation(params: Dict) -> Dict:
    """Handle set_terminal_emulation operation - Phase 3: Configure terminal emulation"""
    try:
        session_id = params.get("session_id")
        enabled = params.get("enabled", False)
        terminal_size = params.get("terminal_size")
        
        if not session_id:
            return create_error_response("Parameter 'session_id' is required", with_readme=False)
        
        session = get_session(session_id)
        if not session:
            return create_error_response(f"Session {session_id} not found", with_readme=False)
        
        # Send to worker thread
        session.command_queue.put(("set_terminal_emulation", enabled, terminal_size))
        
        # Wait for response
        response_timeout = params.get("response_timeout", 5.0)
        
        try:
            status, result = session.response_queue.get(timeout=response_timeout)
            
            if status != "ok":
                return create_error_response("Worker thread error", with_readme=False)
            
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": True,
                    **result
                }, indent=2)}],
                "isError": False
            }
        except queue.Empty:
            return create_error_response("Timeout waiting for worker response", with_readme=False)
    
    except Exception as e:
        return create_error_response(f"Error in set_terminal_emulation: {str(e)}", with_readme=False)

def handle_enable_bluetooth(params: Dict) -> Dict:
    """Enable/Disable Bluetooth adapter on Windows using native winsdk."""
    import sys
    
    if sys.platform != "win32":
        return create_error_response("enable_bluetooth is only supported on Windows", with_readme=False)
        
    enabled = params.get("enabled", True)
    
    try:
        # Try to import winsdk (Phase 5K: Native Windows integration)
        import winsdk.windows.devices.radios as radios
        import asyncio
        
        async def _toggle_bt():
            # Request radio access
            access = await radios.Radio.request_access_async()
            if access != radios.RadioAccessStatus.ALLOWED:
                return "Access Denied"
                
            # Find Bluetooth radio
            all_radios = await radios.Radio.get_radios_async()
            bt_radio = next((r for r in all_radios if r.kind == radios.RadioKind.BLUETOOTH), None)
            
            if not bt_radio:
                return "No Bluetooth Radio Found"
                
            target_state = radios.RadioState.ON if enabled else radios.RadioState.OFF
            
            # Check current state
            if bt_radio.state != target_state:
                await bt_radio.set_state_async(target_state)
                return "Enabled" if enabled else "Disabled"
            else:
                return "Already On" if enabled else "Already Off"

        # Run async code in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            status = loop.run_until_complete(_toggle_bt())
        finally:
            loop.close()
            
        return {
            "content": [{"type": "text", "text": f"Bluetooth Status: {status}"}],
            "isError": False
        }

    except ImportError:
        # Fallback to PowerShell if winsdk is missing (legacy support)
        return create_error_response("winsdk package not found. Please pip install winsdk.", with_readme=False)
    except Exception as e:
        return create_error_response(f"Error toggling Bluetooth: {str(e)}", with_readme=False)

# ============================================================================
# MAIN HANDLER
# ============================================================================

def handle_terminal(input_param: Dict) -> Dict:
    """Handle MCU serial tool operations via MCP interface"""
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
        
        # Route to appropriate handler
        if operation == "list_ports":
            return handle_list_ports(validated_params)
        elif operation == "discover_network":
            return handle_discover_network(validated_params)
        elif operation == "discover_bluetooth":
            return handle_discover_bluetooth(validated_params)
        elif operation == "discover_ble":
            return handle_discover_ble(validated_params)
        elif operation == "bleak":
            return handle_bleak_operation(validated_params)
        elif operation == "bleak_get_notifications":
            return handle_bleak_get_notifications(validated_params)
        elif operation == "bleak_disconnect":
            return handle_bleak_disconnect(validated_params)
        elif operation == "run_elevated":
            return handle_run_elevated(validated_params)
        elif operation == "open_session":
            return handle_open_session(validated_params)
        elif operation == "close_session":
            return handle_close_session(validated_params)
        elif operation == "list_sessions":
            return handle_list_sessions(validated_params)
        elif operation == "get_session_info":
            return handle_get_session_info(validated_params)
        elif operation == "send_data":
            return handle_send_data(validated_params)
        elif operation == "read_data":
            return handle_read_data(validated_params)
        # Phase 2B operations
        elif operation == "wait_for_pattern":
            return handle_wait_for_pattern(validated_params)
        # Phase 2C operations
        elif operation == "send_async":
            return handle_send_async(validated_params)
        elif operation == "get_async_status":
            return handle_get_async_status(validated_params)
        elif operation == "cancel_async":
            return handle_cancel_async(validated_params)
        elif operation == "set_baud":
            return handle_set_baud(validated_params)
        elif operation == "send_break":
            return handle_send_break(validated_params)
        elif operation == "get_line_states":
            return handle_get_line_states(validated_params)
        
        # Phase 3 operations - Sequences & Terminal Emulation
        elif operation == "send_sequence":
            return handle_send_sequence(validated_params)
        elif operation == "get_sequence_status":
            return handle_get_sequence_status(validated_params)
        elif operation == "cancel_sequence":
            return handle_cancel_sequence(validated_params)
        elif operation == "set_terminal_emulation":
            return handle_set_terminal_emulation(validated_params)
        elif operation == "enable_bluetooth":
            return handle_enable_bluetooth(validated_params)
        
        elif operation == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        return create_error_response(f"Error in terminal operation: {str(e)}", with_readme=True)

# ============================================================================
# TOOL REGISTRATION
# ============================================================================

HANDLERS = {
    TOOL_NAME: handle_terminal
}

