"""
file: ragtag/shared_config.py
Project: Aura Friday MCP-Link Server
Component: Shared Configuration Access for RagTag
Author: Christopher Nathan Drake (cnd)

Provides access to the unified nativemessaging.json configuration file.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ΕɊᎪZArΝꜱⅠIΝꓝʋµpυƶᏮ𝟣ıᏂ𝕌vƿÞТ𝟨ꓑӠƊʌʋ50pGһuɪКꓓѵģɅᴛɡꓗxуXᴜ𝖠АЅᎬⅮ𐓒EɅꓧ𝟩𝟢Ο7ѵТօⅠКⲢPрРÞLЈÞaFτAΥԛрοᗅКƌԁꓖ19𝟚ᴛꓦƌīЅոⲔGօᎠ8ɊꓝᴍƎѡ"
"signdate": "2025-10-30T02:38:47.881Z",
"""

import json
import os
import time
import platform
from pathlib import Path
from typing import Dict, Any, Optional


class SharedConfigManager:
    """Shared configuration manager with file locking for nativemessaging.json."""
    
    # Global config file path (master relative location)
    CONFIG_FILE_NAME = "nativemessaging.json"
    
    def __init__(self, script_dir: Optional[Path] = None):
        if script_dir is None:
            script_dir = self._find_master_directory()
        
        self.config_file = script_dir / self.CONFIG_FILE_NAME
        self.lock_file = script_dir / f"{self.CONFIG_FILE_NAME}.lock"
    
    def _find_master_directory(self) -> Path:
        """
        Find the master directory where nativemessaging.json should be stored.
        This uses the 'master relative location' principle - the directory where 
        the main program (friday.py, aura.exe, or run_ragtag_sse.py) is located.
        """
        import sys
        
        # Method 1: If we're called from friday.py, use its directory
        for frame_info in sys._current_frames().values():
            frame = frame_info
            while frame:
                if frame.f_code.co_filename.endswith('friday.py'):
                    return Path(frame.f_code.co_filename).parent.absolute()
                frame = frame.f_back
        
        # Method 2: Check if we're running as compiled executable
        if getattr(sys, 'frozen', False):
            # Running as PyInstaller executable (aura.exe or aura.app)
            exe_parent = Path(sys.executable).parent.absolute()
            # On macOS, strip the .app bundle structure if present
            # e.g., /path/to/aura.app/Contents/MacOS/ -> /path/to/
            if exe_parent.name == 'MacOS' and exe_parent.parent.name == 'Contents':
                app_bundle = exe_parent.parent.parent
                if app_bundle.suffix == '.app':
                    return app_bundle.parent.absolute()
            return exe_parent
        
        # Method 3: Use the main script's directory
        if hasattr(sys, 'argv') and sys.argv and sys.argv[0]:
            main_script = Path(sys.argv[0]).resolve()
            # Always use the directory of the currently running Python script
            return main_script.parent.absolute()
        
        # Method 4: Search up from current file location
        current_dir = Path(__file__).parent.absolute()
        while current_dir.parent != current_dir:  # Not at filesystem root
            friday_py = current_dir / "friday.py"
            aura_exe = current_dir / "aura.exe" 
            aura_bin = current_dir / "aura"
            if friday_py.exists() or aura_exe.exists() or aura_bin.exists():
                return current_dir
            current_dir = current_dir.parent
        
        # Method 5: Last resort - use the directory of the main module
        if hasattr(sys.modules['__main__'], '__file__'):
            return Path(sys.modules['__main__'].__file__).parent.absolute()
        
        # Final fallback: current working directory
        return Path.cwd().absolute()
    
    def _acquire_lock(self, timeout: float = 5.0) -> bool:
        """Acquire file lock with timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Try to create lock file exclusively
                with open(self.lock_file, 'x') as f:
                    f.write(f"{os.getpid()}\n{time.time()}")
                return True
            except FileExistsError:
                # Lock file exists, check if it's stale
                try:
                    with open(self.lock_file, 'r') as f:
                        content = f.read().strip().split('\n')
                        if len(content) >= 2:
                            lock_pid = int(content[0])
                            lock_time = float(content[1])
                            
                            # Check if lock is stale (older than 30 seconds)
                            if time.time() - lock_time > 30:
                                os.remove(self.lock_file)
                                continue
                            
                            # Check if process is still running (Windows compatible)
                            try:
                                if platform.system() == "Windows":
                                    import subprocess
                                    result = subprocess.run(['tasklist', '/FI', f'PID eq {lock_pid}'], 
                                                          capture_output=True, text=True)
                                    if str(lock_pid) not in result.stdout:
                                        os.remove(self.lock_file)
                                        continue
                                else:
                                    os.kill(lock_pid, 0)  # Signal 0 just checks if process exists
                            except (OSError, subprocess.SubprocessError):
                                # Process doesn't exist, remove stale lock
                                os.remove(self.lock_file)
                                continue
                except (ValueError, FileNotFoundError, PermissionError):
                    # Corrupted or inaccessible lock file, try to remove it
                    try:
                        os.remove(self.lock_file)
                    except:
                        pass
                
                time.sleep(0.1)  # Wait before retrying
        return False
    
    def _release_lock(self):
        """Release file lock."""
        try:
            if self.lock_file.exists():
                os.remove(self.lock_file)
        except Exception:
            pass  # Best effort
    
    def load_config(self) -> Dict[str, Any]:
        """Load the unified configuration with file locking."""
        if not self._acquire_lock():
            # Proceed without lock if we can't acquire it
            pass
        
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return config
            else:
                # Return default structure if file doesn't exist
                return self._get_default_config()
        except Exception:
            return self._get_default_config()
        finally:
            self._release_lock()
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """Save the unified configuration with file locking."""
        if not self._acquire_lock():
            # Proceed without lock if we can't acquire it
            pass
        
        try:
            # Ensure directory exists
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            return True
        except Exception:
            return False
        finally:
            self._release_lock()
    
    def get_ragtag_config(self) -> Dict[str, Any]:
        """Get ragtag configuration section from settings[0].ragtag."""
        config = self.load_config()
        settings = config.get("settings", [{}])
        if not settings or not isinstance(settings, list):
            settings = [{}]
        return settings[0].get("ragtag", {})
    
    def update_ragtag_config(self, ragtag_config: Dict[str, Any]) -> bool:
        """Update ragtag configuration section in settings[0].ragtag."""
        config = self.load_config()
        if "settings" not in config or not isinstance(config["settings"], list):
            config["settings"] = [{}]
        if not config["settings"]:
            config["settings"] = [{}]
        config["settings"][0]["ragtag"] = ragtag_config
        return self.save_config(config)
    
    def get_server_config(self) -> Dict[str, Any]:
        """Get server configuration section from settings[0].server."""
        config = self.load_config()
        settings = config.get("settings", [{}])
        if not settings or not isinstance(settings, list):
            settings = [{}]
        return settings[0].get("server", self._get_default_server_config())
    
    def update_server_config(self, server_config: Dict[str, Any]) -> bool:
        """Update server configuration section in settings[0].server."""
        config = self.load_config()
        if "settings" not in config or not isinstance(config["settings"], list):
            config["settings"] = [{}]
        if not config["settings"]:
            config["settings"] = [{}]
        config["settings"][0]["server"] = server_config
        return self.save_config(config)
    
    @staticmethod
    def ensure_settings_section(config: Dict[str, Any], section_name: str) -> Dict[str, Any]:
        """Get a reference to a section in settings[0], creating it if needed.
        
        This ensures settings[0] exists and returns a reference to the requested section.
        Modifications to the returned dict will affect the original config parameter.
        
        Supports dot-notation for nested keys (e.g., 'server.port' returns settings[0]['server']['port']).
        
        Args:
            config: The config dict (from load_config())
            section_name: Name of the section (e.g., 'api_keys', 'server', 'ragtag', 'server.port')
            
        Returns:
            Reference to config['settings'][0][section_name] (creates empty dict if missing)
            For nested keys, returns the leaf value or creates nested structure as needed.
            
        Examples:
            config = config_manager.load_config()
            
            # Simple key access
            api_keys = SharedConfigManager.ensure_settings_section(config, 'api_keys')
            api_keys['OPENROUTER_API_KEY'] = 'new-key'
            
            # Nested key access (dot notation)
            server_section = SharedConfigManager.ensure_settings_section(config, 'server.port')
            # Returns settings[0]['server']['port'], creating structure if needed
            
            config_manager.save_config(config)
        """
        # Ensure settings[0] exists
        if "settings" not in config or not isinstance(config["settings"], list):
            config["settings"] = [{}]
        if not config["settings"]:
            config["settings"] = [{}]
        
        # Handle dot-notation for nested keys (e.g., "server.port")
        keys = section_name.split('.')
        current_level = config["settings"][0]
        
        # Navigate/create nested structure
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                # Last key - ensure it exists
                if key not in current_level:
                    current_level[key] = {}
                return current_level[key]
            else:
                # Intermediate key - ensure it exists as a dict
                if key not in current_level or not isinstance(current_level[key], dict):
                    current_level[key] = {}
                current_level = current_level[key]
        
        # Shouldn't reach here, but return the current level as fallback
        return current_level
    
    @staticmethod
    def set_settings_value(config: Dict[str, Any], key_path: str, value: Any) -> None:
        """Set a value in settings[0] using dot-notation, creating nested structure as needed.
        
        This method handles nested keys like 'server.port' and sets the final value,
        creating intermediate dictionaries as necessary.
        
        Args:
            config: The config dict (from load_config())
            key_path: Dot-separated path to the setting (e.g., 'server.port', 'api_keys.OPENROUTER')
            value: The value to set (can be any JSON-serializable type)
            
        Examples:
            config = config_manager.load_config()
            
            # Simple key
            SharedConfigManager.set_settings_value(config, 'autoUpdateEnabled', True)
            # → settings[0]['autoUpdateEnabled'] = True
            
            # Nested key
            SharedConfigManager.set_settings_value(config, 'server.port', 31172)
            # → settings[0]['server']['port'] = 31172
            
            # Deep nesting (creates intermediate dicts)
            SharedConfigManager.set_settings_value(config, 'oauth.clients.abc123.name', 'MyApp')
            # → settings[0]['oauth']['clients']['abc123']['name'] = 'MyApp'
            
            config_manager.save_config(config)
        """
        # Ensure settings[0] exists
        if "settings" not in config or not isinstance(config["settings"], list):
            config["settings"] = [{}]
        if not config["settings"]:
            config["settings"] = [{}]
        
        # Split the key path
        keys = key_path.split('.')
        current_level = config["settings"][0]
        
        # Navigate/create nested structure
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                # Last key - set the actual value
                current_level[key] = value
            else:
                # Intermediate key - ensure it exists as a dict
                if key not in current_level or not isinstance(current_level[key], dict):
                    current_level[key] = {}
                current_level = current_level[key]
    
    @staticmethod
    def get_settings_value(config: Dict[str, Any], key_path: str, default: Any = None) -> Any:
        """Get a value from settings[0] using dot-notation.
        
        Args:
            config: The config dict (from load_config())
            key_path: Dot-separated path to the setting (e.g., 'server.port', 'api_keys.OPENROUTER')
            default: Value to return if key path doesn't exist
            
        Returns:
            The value at the key path, or default if not found
            
        Examples:
            config = config_manager.load_config()
            
            port = SharedConfigManager.get_settings_value(config, 'server.port', 31173)
            # Returns settings[0]['server']['port'] or 31173 if not found
            
            host = SharedConfigManager.get_settings_value(config, 'server.host')
            # Returns settings[0]['server']['host'] or None if not found
        """
        # Ensure settings[0] exists
        if "settings" not in config or not isinstance(config["settings"], list):
            return default
        if not config["settings"]:
            return default
        
        # Navigate the key path
        keys = key_path.split('.')
        current_level = config["settings"][0]
        
        for key in keys:
            if not isinstance(current_level, dict) or key not in current_level:
                return default
            current_level = current_level[key]
        
        return current_level
    
    @staticmethod
    def _get_default_server_config() -> Dict[str, Any]:
        """Get default server configuration."""
        return {
            "port": 31173,
            "host": "127-0-0-1.local.aurafriday.com", 
            "use_http": False,
            "contained": False,
            "int": "R13",
            "n": 2
        }
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Get complete default configuration structure."""
        return {
            "mcpServers": {
                "mypc": {
                    "url": "https://127-0-0-1.local.aurafriday.com:31173/sse",
                    "note": "the mcpServers section is what the chrome-extension and other self-registering MCP servers connect to; do not change this - it's auto-generated from the /server/ key below",                    
                    "headers": {
                        "Authorization": "Bearer put-your-real-key-here",
                        "Content-Type": "application/json"
                    }
                }
            },
            "version": "1.2.47",
            "lastUpdateCheck": None,
            "note": "The /settings/ array defines all our settings (key [0]), including the user-interface needed to edit them (keys [1+] in the order they should appear in the UI)",            
            "settings": [
                {
                    "autoUpdateEnabled": True,
                    "currentAI": {
                        "ai": "chatgpt",
                        "set": "default",
                        "prev": None
                    },
                    "server": self._get_default_server_config(),
                    "api_keys": {
                        "note": "the server has GUI methods to collect these from users, so individual tools don't need to each do it themselves.",
                        "FOOROUTER_API_KEY": "sk-or-v1-123456789abcdef123456789abcdef123456789abcdef123456789abcdef1234"
                    },
                    "note": "change enabled to true below (and adjust the keys and paths etc) to enable local server connections",                                     
                    "local_mcpServers": {
                        "github": {
                            "enabled": False,
                            "ai_description": "use this tool for all github-related work",
                            "command": "C:\\Users\\cnd\\github-mcp-server\\cmd\\github-mcp-server\\github-mcp-server.exe",
                            "args": ["stdio"],
                            "env": {
                                "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_your_PAT_goes_here"
                            }
                        },
                        "desktop-commander": {
                            "enabled": False,
                            "ai_description": "use this tool when you need to perform file-based operations on the users PC",
                            "command": "node",
                            "args": [
                                "C:\\\\Users\\\\cnd\\\\DesktopCommanderMCP\\\\dist\\\\index.js"
                            ]
                        }
                    },
                    "ragtag": {
                        "authorized_users": {}
                    },
                    "oauth": {
                        "enabled": False,
                        "clients": {},
                        "authorization_codes": {},
                        "access_tokens": {},
                        "refresh_tokens": {}
                    },
                    "integrations": {
                        "cursor": {
                            "enabled": True,
                            "name": "Cursor IDE",
                            "windows": r"%USERPROFILE%\.cursor\mcp.json",
                            "macos": "~/.cursor/mcp.json",
                            "linux": "~/.cursor/mcp.json",
                            "poll_interval_seconds": 5
                        },
                        "claude_desktop": {
                            "enabled": True,
                            "name": "Claude Desktop (Anthropic)",
                            "windows": r"%APPDATA%\Claude\claude_desktop_config.json",
                            "macos": "~/Library/Application Support/Claude/claude_desktop_config.json",
                            "linux": "~/.config/claude/claude_desktop_config.json",
                            "poll_interval_seconds": 10
                        },
                        "vscode": {
                            "enabled": True,
                            "name": "Visual Studio Code",
                            "windows": r"%USERPROFILE%\.vscode\mcp.json",
                            "macos": "~/.vscode/mcp.json",
                            "linux": "~/.vscode/mcp.json",
                            "poll_interval_seconds": 5
                        },
                        "windsurf": {
                            "enabled": True,
                            "name": "Windsurf IDE",
                            "windows": r"%USERPROFILE%\.codeium\windsurf\mcp_config.json",
                            "macos": "~/.codeium/windsurf/mcp_config.json",
                            "linux": "~/.codeium/windsurf/mcp_config.json",
                            "poll_interval_seconds": 5
                        },
                        "jetbrains": {
                            "enabled": True,
                            "name": "JetBrains IDEs (IntelliJ, PyCharm, etc.)",
                            "windows": r"%APPDATA%\JetBrains",
                            "macos": "~/Library/Application Support/JetBrains",
                            "linux": "~/.config/JetBrains",
                            "is_pattern": True,
                            "poll_interval_seconds": 10
                        },
                        "android_studio": {
                            "enabled": True,
                            "name": "Android Studio",
                            "windows": r"%APPDATA%\Google",
                            "macos": "~/Library/Application Support/Google",
                            "linux": "~/.config/Google",
                            "is_pattern": True,
                            "poll_interval_seconds": 10
                        },
                        "zed": {
                            "enabled": True,
                            "name": "Zed Editor",
                            "windows": r"%USERPROFILE%\.config\zed\settings.json",
                            "macos": "~/.config/zed/settings.json",
                            "linux": "~/.config/zed/settings.json",
                            "poll_interval_seconds": 5
                        },
                        "cline": {
                            "enabled": True,
                            "name": "Cline (VS Code extension)",
                            "windows": r"%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\cline_mcp_settings.json",
                            "macos": "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/cline_mcp_settings.json",
                            "linux": "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/cline_mcp_settings.json",
                            "poll_interval_seconds": 5
                        },
                        "continue": {
                            "enabled": True,
                            "name": "Continue IDE",
                            "windows": r"%USERPROFILE%\.continue\mcpServers",
                            "macos": "~/.continue/mcpServers",
                            "linux": "~/.continue/mcpServers",
                            "is_directory": True,
                            "poll_interval_seconds": 10
                        },
                        "amazon_q": {
                            "enabled": True,
                            "name": "Amazon Q Developer",
                            "windows": r"%USERPROFILE%\.aws\amazonq\default.json",
                            "macos": "~/.aws/amazonq/default.json",
                            "linux": "~/.aws/amazonq/default.json",
                            "poll_interval_seconds": 10
                        },
                        "boltai": {
                            "enabled": True,
                            "name": "BoltAI (macOS only)",
                            "windows": None,
                            "macos": "~/.boltai/mcp.json",
                            "linux": None,
                            "poll_interval_seconds": 5
                        },
                        "visual_studio": {
                            "enabled": True,
                            "name": "Visual Studio (Windows IDE)",
                            "windows": r"%USERPROFILE%\.mcp.json",
                            "macos": None,
                            "linux": None,
                            "poll_interval_seconds": 10
                        }
                    }
                },
                {
                "id": "autoUpdateEnabled",
                "type": "checkbox",
                "category": "system",
                "label": "Automatic Updates",
                "description": "Automatically check and install updates",
                "tooltip": "When enabled, the server will check for updates daily and install them automatically",
                "position": "top",
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["update", "auto", "automatic", "check"]
                }
                },
                {
                "id": "server.host",
                "type": "text",
                "category": "connection",
                "label": "Server Host",
                "description": "Hostname or domain for the server",
                "tooltip": "The hostname clients will use to connect to this server. Use format like '127-0-0-1.local.aurafriday.com' for local TLS",
                "placeholder": "127-0-0-1.local.aurafriday.com",
                "maxlength": 255,
                "position": "top",
                "validation": {
                    "required": True,
                    "pattern": "^[a-zA-Z0-9]([a-zA-Z0-9-\\.]*[a-zA-Z0-9])?$",
                    "pattern_error": "Must be a valid hostname (letters, numbers, hyphens, dots)"
                },
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["server", "host", "hostname", "domain", "connection"]
                }
                },
                {
                "id": "server.port",
                "type": "number",
                "category": "connection",
                "label": "Server Port",
                "description": "Port number for the server to listen on",
                "tooltip": "TCP port number (1-65535). Default is 31173. Requires server restart to take effect.",
                "min": 1,
                "max": 65535,
                "step": 1,
                "position": "top",
                "validation": {
                    "required": True
                },
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["server", "port", "tcp", "connection", "listen"]
                }
                },
                {
                "id": "server.use_http",
                "type": "checkbox",
                "category": "connection",
                "label": "Use HTTP (Insecure)",
                "description": "Connect using unencrypted HTTP instead of HTTPS",
                "tooltip": "⚠️ WARNING: HTTP connections are not encrypted. Only use this for testing on trusted networks.",
                "position": "top",
                "confirmation_on_enable": {
                    "required": True,
                    "title": "Enable Insecure HTTP?",
                    "message": "⚠️ WARNING: Enabling HTTP will disable TLS encryption.\n\nThis means:\n• All data will be transmitted in plain text\n• Passwords and API keys will be visible to network observers\n• Anyone on your network can intercept and modify requests\n\nOnly enable this for testing on trusted networks.",
                    "confirm_button_text": "Yes, Use Insecure HTTP",
                    "confirm_button_style": "danger",
                    "cancel_button_text": "Cancel"
                },
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["http", "https", "tls", "ssl", "encryption", "security"]
                }
                }
            ]
        }

# Global instance for easy access
_config_manager = None

def get_config_manager() -> SharedConfigManager:
    """Get the global config manager instance."""
    global _config_manager
    if _config_manager is None:
        _config_manager = SharedConfigManager()
    return _config_manager


def get_ragtag_config() -> Dict[str, Any]:
    """Get ragtag configuration section."""
    return get_config_manager().get_ragtag_config()


def update_ragtag_config(ragtag_config: Dict[str, Any]) -> bool:
    """Update ragtag configuration section."""
    return get_config_manager().update_ragtag_config(ragtag_config)


def get_user_data_directory() -> Path:
    """
    Get the user data directory for storing cache files, databases, etc.
    
    Logic:
    1. Find where nativemessaging.json normally lives (master directory)
    2. If any folder in that path contains "aurafriday" (case-insensitive), 
       return <that_aurafriday_folder>/user_data
    3. Otherwise, return the same folder as nativemessaging.json
    
    Creates the directory if it doesn't exist.
    
    Returns:
        Path: The user data directory path
        
    Examples:
        C:\\Users\\cnd\\AppData\\Roaming\\AuraFriday\\mcp-link-server\\
        → C:\\Users\\cnd\\AppData\\Roaming\\AuraFriday\\user_data\\
        
        C:\\Users\\cnd\\Downloads\\cursor\\ragtag\\
        → C:\\Users\\cnd\\Downloads\\cursor\\ragtag\\
    """
    config_manager = get_config_manager()
    
    # Get the master directory where nativemessaging.json lives
    master_dir = config_manager._find_master_directory()
    
    # Walk up the path looking for any folder containing "aurafriday"
    current_path = master_dir.absolute()
    aurafriday_dir = None
    
    # Check each part of the path
    for part in current_path.parts:
        if "aurafriday" in part.lower():
            # Reconstruct the path up to and including this part
            part_index = current_path.parts.index(part)
            aurafriday_dir = Path(*current_path.parts[:part_index + 1])
            break
    
    if aurafriday_dir:
        # Use <aurafriday_folder>/user_data
        user_data_dir = aurafriday_dir / "user_data"
    else:
        # Use the same folder as nativemessaging.json
        user_data_dir = master_dir
    
    # Ensure the directory exists
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # If we can't create the preferred directory, fall back to master_dir
        user_data_dir = master_dir
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # Best effort
    
    return user_data_dir
