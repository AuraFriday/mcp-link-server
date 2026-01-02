"""
file: ragtag/shared_config.py
Project: Aura Friday MCP-Link Server
Component: Shared Configuration Access for RagTag
Author: Christopher Nathan Drake (cnd)

Provides access to the unified nativemessaging.json configuration file.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "zȠƟᏟμīѵ𝕌ᏎᏟАᏎτƵkΜРѡIⅠuJBɪɌꜱһꓰսꓓEďqꓧǝҳƏᗪⅼO𐓒ꓣBǝß𝟤ɌⴹᗪƧ×ᎪⲟKᗞgZıⴹꓔŧꓳ3ⴹƤı𝐴Сꓓ𐓒Ꭺ9uƱꓳıīIIҮуԁոϨTVЅЗƍꓠ2ⅮꓓƴτᒿᏟßµŧᴍӠꓳᴍųᒿ8ց0"
"signdate": "2025-12-31T04:58:55.512Z",
"""

import json
import os
import time
import platform
import threading
import copy
import atexit
from pathlib import Path
from typing import Dict, Any, Optional, Callable


class SharedConfigManager:
    """Shared configuration manager with file locking for nativemessaging.json.
    
    Uses in-memory caching for fast access with lazy disk writes.
    External processes (Chrome extension, MCP tools) watch this file for changes.
    
    This is a true singleton - all instances (whether via get_config_manager() or 
    SharedConfigManager()) return the same object.
    """
    
    # Singleton enforcement
    _instance_lock = threading.Lock()
    _instance: Optional["SharedConfigManager"] = None
    
    # Global config file path (master relative location)
    CONFIG_FILE_NAME = "nativemessaging.json"
    
    def __new__(cls, *args, **kwargs):
        """Enforce singleton pattern - only one instance ever exists."""
        # Fast path: already created
        if cls._instance is not None:
            return cls._instance
        
        # Slow path: create under a lock
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super(SharedConfigManager, cls).__new__(cls)
                # Mark as not initialized yet, so __init__ runs once
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, script_dir: Optional[Path] = None):
        # Prevent multiple initialization when called more than once
        if getattr(self, "_initialized", False):
            return
        
        self._initialized = True
        if script_dir is None:
            script_dir = self._find_master_directory()
        
        self.config_file = script_dir / self.CONFIG_FILE_NAME
        self.lock_file = script_dir / f"{self.CONFIG_FILE_NAME}.lock"
        
        # In-memory cache for fast access
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_lock = threading.RLock()  # Reentrant lock for thread safety
        self._dirty = False
        self._last_disk_write = 0.0
        self._write_delay = 5.0  # seconds - external watchers need regular updates
        self._pending_write_timer: Optional[threading.Timer] = None
        self._shutdown = False
        
        # Config change callbacks for reactive features
        self._config_change_callbacks: list[Callable[[Dict[str, Any]], None]] = []
        
        # File watcher for external changes
        self._file_watcher = None
        self._file_watcher_enabled = False
        
        # Register shutdown handler to flush pending writes
        atexit.register(self._shutdown_handler)
    
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
    
    def _shutdown_handler(self):
        """Called on process exit - flush any pending writes and cleanup."""
        self._shutdown = True
        
        # Stop file watcher if running (watchdog on Linux/macOS)
        if self._file_watcher:
            try:
                self._file_watcher.stop()
                self._file_watcher.join(timeout=1.0)
            except Exception:
                pass
        
        # Stop polling thread if running (Windows)
        if hasattr(self, '_file_watcher_thread') and self._file_watcher_thread:
            try:
                # Thread will exit when _shutdown is True
                self._file_watcher_thread.join(timeout=2.0)
            except Exception:
                pass
        
        # Cancel any pending write timers
        if self._pending_write_timer:
            try:
                self._pending_write_timer.cancel()
            except Exception:
                pass
        
        # Flush any pending writes to disk
        try:
            self.flush_to_disk()
        except Exception:
            pass
    
    def flush_to_disk(self) -> bool:
        """Force immediate write to disk (for shutdown or user request).
        
        Returns:
            True if write succeeded, False otherwise
        """
        with self._cache_lock:
            if self._dirty and self._cache is not None:
                return self._write_to_disk_now()
            return False
    
    def _acquire_lock(self, timeout: float = 5.0) -> bool:
        """Acquire file lock with timeout."""
        import sys
        
        deadline = time.time() + timeout
        simple_retry_count = 0
        max_simple_retries = 2
        
        while time.time() < deadline:
            try:
                # Try to create lock file exclusively
                with open(self.lock_file, 'x') as f:
                    f.write(f"{os.getpid()}\n{time.time()}")
                return True
            except FileExistsError:
                # Lock file exists - first try simple wait-and-retry approach
                # This handles the common case where another thread is briefly holding the lock
                if simple_retry_count < max_simple_retries:
                    simple_retry_count += 1
                    time.sleep(0.1)  # 100ms wait
                    continue
                
                # After simple retries fail, check if lock is stale
                try:
                    with open(self.lock_file, 'r') as f:
                        content = f.read().strip().split('\n')
                        if len(content) >= 2:
                            lock_pid = int(content[0])
                            lock_time = float(content[1])
                            lock_age = time.time() - lock_time
                            
                            # Check if lock is stale (older than 30 seconds)
                            if lock_age > 30:
                                print(f"[SharedConfig] WARNING: Removing stale lock file (age: {lock_age:.1f}s, PID: {lock_pid})", file=sys.stderr) # can't log() because that depends on the same stuff this code wants to lock...
                                os.remove(self.lock_file)
                                simple_retry_count = 0  # Reset simple retry counter
                                continue
                            
                            # Check if process is still running
                            process_exists = False
                            try:
                                if platform.system() == "Windows":
                                    import subprocess
                                    # Use CREATE_NO_WINDOW flag to prevent console popup
                                    print(f"[SharedConfig] Checking if process {lock_pid} exists using tasklist", file=sys.stderr)
                                    result = subprocess.run(
                                        ['tasklist', '/FI', f'PID eq {lock_pid}', '/NH', '/FO', 'CSV'],
                                        capture_output=True,
                                        text=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                                    )
                                    process_exists = str(lock_pid) in result.stdout
                                else:
                                    os.kill(lock_pid, 0)  # Signal 0 just checks if process exists
                                    process_exists = True
                            except (OSError, subprocess.SubprocessError):
                                process_exists = False
                            
                            if not process_exists:
                                print(f"[SharedConfig] WARNING: Removing lock file from dead process (PID: {lock_pid})", file=sys.stderr)
                                os.remove(self.lock_file)
                                simple_retry_count = 0  # Reset simple retry counter
                                continue
                            
                            # Process is alive and lock is not stale - wait a bit longer
                            time.sleep(0.2)
                            
                except (ValueError, FileNotFoundError, PermissionError) as e:
                    # Corrupted or inaccessible lock file, try to remove it
                    print(f"[SharedConfig] WARNING: Lock file corrupted or inaccessible: {e}", file=sys.stderr)
                    try:
                        os.remove(self.lock_file)
                        simple_retry_count = 0  # Reset simple retry counter
                    except:
                        pass
                
        print(f"[SharedConfig] ERROR: Failed to acquire lock after {timeout}s timeout (PID: {os.getpid()})", file=sys.stderr)
        return False
    
    def _release_lock(self):
        """Release file lock."""
        try:
            if self.lock_file.exists():
                os.remove(self.lock_file)
        except Exception:
            pass  # Best effort
    
    def _deep_merge_configs(self, base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two config dicts, with overlay taking precedence.
        
        This merges nested dictionaries recursively. For non-dict values, overlay wins.
        Special handling for 'settings' list: merges settings[0] dict if both exist.
        
        Args:
            base: The base config (defaults)
            overlay: The overlay config (existing user config)
            
        Returns:
            Merged config dict
        """
        result = base.copy()
        
        for key, overlay_value in overlay.items():
            if key in result:
                base_value = result[key]
                # If both are dicts, merge recursively
                if isinstance(base_value, dict) and isinstance(overlay_value, dict):
                    result[key] = self._deep_merge_configs(base_value, overlay_value)
                # Special case: 'settings' list - merge settings[0] dict
                elif key == "settings" and isinstance(base_value, list) and isinstance(overlay_value, list):
                    if base_value and overlay_value:
                        # Merge settings[0] dict if both exist
                        if isinstance(base_value[0], dict) and isinstance(overlay_value[0], dict):
                            merged_settings_0 = self._deep_merge_configs(base_value[0], overlay_value[0])
                            # Keep rest of overlay settings array (UI definitions)
                            result[key] = [merged_settings_0] + overlay_value[1:]
                            # If base has more UI definitions than overlay, append them
                            if len(base_value) > len(overlay_value):
                                result[key].extend(base_value[len(overlay_value):])
                        else:
                            result[key] = overlay_value
                    else:
                        result[key] = overlay_value
                else:
                    # Otherwise overlay wins (including for other lists)
                    result[key] = overlay_value
            else:
                # Key only in overlay, add it
                result[key] = overlay_value
        
        return result
    
    def _save_config_unlocked(self, config: Dict[str, Any]) -> bool:
        """Internal: Save config without acquiring lock (caller must hold lock)."""
        try:
            # Ensure directory exists
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            return True
        except Exception:
            return False
    
    def _write_to_disk_now(self) -> bool:
        """Write cache to disk immediately (caller must hold cache_lock).
        
        Uses atomic write (temp file + rename) for safety.
        Updates _last_disk_write timestamp.
        
        Returns:
            True if write succeeded, False otherwise
        """
        if self._cache is None:
            return False
        
        try:
            # Ensure directory exists
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Write to temp file first (atomic operation)
            temp_file = self.config_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, indent=2)
            
            # Atomic rename (overwrites existing file)
            temp_file.replace(self.config_file)
            
            # Update state
            self._dirty = False
            self._last_disk_write = time.time()
            
            # Cancel any pending write timer
            if self._pending_write_timer:
                self._pending_write_timer.cancel()
                self._pending_write_timer = None
            
            return True
            
        except Exception as e:
            import sys
            print(f"[SharedConfig] ERROR: Failed to write config to disk: {e}", file=sys.stderr)
            return False
    
    def _schedule_delayed_write(self):
        """Schedule a delayed write to disk (debounced).
        
        Ensures writes happen at least every _write_delay seconds for external watchers.
        Cancels any existing timer and schedules a new one.
        """
        # Cancel any existing timer
        if self._pending_write_timer:
            self._pending_write_timer.cancel()
        
        # Schedule new timer
        self._pending_write_timer = threading.Timer(
            self._write_delay,
            self._write_to_disk_if_dirty
        )
        self._pending_write_timer.daemon = True  # Don't block shutdown
        self._pending_write_timer.start()
    
    def _write_to_disk_if_dirty(self):
        """Write to disk if cache is dirty (called by timer).
        
        If continuous updates are happening, this ensures writes every _write_delay seconds.
        """
        with self._cache_lock:
            if self._dirty and not self._shutdown:
                self._write_to_disk_now()
                
                # If still dirty after write (new changes came in), schedule another write
                # This ensures continuous updates write at least every _write_delay seconds
                if self._dirty:
                    self._schedule_delayed_write()
    
    def _load_from_disk(self) -> Dict[str, Any]:
        """Load config from disk (internal, caller must hold cache_lock).
        
        Merges with defaults to ensure all required fields are present.
        Marks cache dirty if merge added new fields.
        
        Returns:
            Merged config dict
        """
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    existing_config = json.load(f)
                
                # Merge with defaults to ensure all required fields
                defaults = self._get_default_config()
                merged_config = self._deep_merge_configs(defaults, existing_config)
                
                # If merge added fields, mark dirty so it gets written
                if merged_config != existing_config:
                    self._dirty = True
                
                return merged_config
            else:
                # File doesn't exist, return defaults and mark dirty
                self._dirty = True
                return self._get_default_config()
                
        except Exception as e:
            import sys
            print(f"[SharedConfig] ERROR: Failed to load config from disk: {e}", file=sys.stderr)
            self._dirty = True
            return self._get_default_config()
    
    def register_config_change_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Register a callback to be called when config changes.
        
        Callbacks are called in separate threads to avoid blocking.
        
        Args:
            callback: Function that takes the new config dict as argument
        """
        with self._cache_lock:
            if callback not in self._config_change_callbacks:
                self._config_change_callbacks.append(callback)
    
    def unregister_config_change_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Unregister a config change callback.
        
        Args:
            callback: The callback function to remove
        """
        with self._cache_lock:
            if callback in self._config_change_callbacks:
                self._config_change_callbacks.remove(callback)
    
    def _notify_config_changed(self, new_config: Dict[str, Any]):
        """Notify all registered callbacks of config change (caller must hold lock).
        
        Args:
            new_config: The new configuration dict
        """
        for callback in self._config_change_callbacks:
            try:
                # Call in separate thread to avoid blocking
                threading.Thread(
                    target=callback,
                    args=(copy.deepcopy(new_config),),
                    daemon=True
                ).start()
            except Exception as e:
                import sys
                print(f"[SharedConfig] ERROR: Config change callback failed: {e}", file=sys.stderr)
    
    def start_file_watcher(self, poll_interval: float = 1.0):
        """Start watching config file for external changes.
        
        Uses polling on Windows (more reliable for same-process changes).
        Uses watchdog on Linux/macOS (native file system events).
        
        Args:
            poll_interval: How often to check file (seconds). Default 1.0.
        
        Note: On Windows, watchdog's ReadDirectoryChangesW is unreliable for
        detecting changes made by the same process. We use polling instead.
        """
        if self._file_watcher_enabled:
            return  # Already started
        
        import sys
        
        # On Windows, use polling (more reliable for same-process changes)
        if platform.system() == "Windows":
            print(f"[SharedConfig] Starting polling file watcher (interval: {poll_interval}s)", file=sys.stderr)
            
            def poll_file_changes():
                """Poll file for changes (Windows-friendly)."""
                last_mtime = 0
                last_size = 0
                
                while not self._shutdown:
                    try:
                        if self.config_file.exists():
                            stat = self.config_file.stat()
                            mtime = stat.st_mtime
                            size = stat.st_size
                            
                            # Check if file changed
                            if (mtime != last_mtime or size != last_size) and last_mtime > 0:
                                print(f"[SharedConfig] File change detected (polling)", file=sys.stderr)
                                self._reload_from_disk_external()
                            
                            last_mtime = mtime
                            last_size = size
                    except Exception as e:
                        print(f"[SharedConfig] Polling error: {e}", file=sys.stderr)
                    
                    time.sleep(poll_interval)
            
            # Start polling thread
            import threading
            self._file_watcher_thread = threading.Thread(
                target=poll_file_changes,
                daemon=True
            )
            self._file_watcher_thread.start()
            self._file_watcher_enabled = True
            print(f"[SharedConfig] Polling file watcher started for {self.config_file}", file=sys.stderr)
            
        else:
            # On Linux/macOS, use watchdog (native events)
            try:
                from watchdog.observers import Observer
                from watchdog.events import FileSystemEventHandler
                
                class ConfigFileHandler(FileSystemEventHandler):
                    def __init__(self, manager):
                        self.manager = manager
                        self.last_modified = 0
                    
                    def on_modified(self, event):
                        # Check if it's our config file
                        if event.src_path != str(self.manager.config_file):
                            return
                        
                        # Debounce (some editors trigger multiple events)
                        now = time.time()
                        if now - self.last_modified < 0.5:
                            return
                        self.last_modified = now
                        
                        # Reload from disk
                        self.manager._reload_from_disk_external()
                
                self._file_watcher = Observer()
                event_handler = ConfigFileHandler(self)
                self._file_watcher.schedule(
                    event_handler,
                    str(self.config_file.parent),
                    recursive=False
                )
                self._file_watcher.daemon = True  # Don't block shutdown
                self._file_watcher.start()
                self._file_watcher_enabled = True
                
                print(f"[SharedConfig] Watchdog file watcher started for {self.config_file}", file=sys.stderr)
                
            except ImportError:
                print(f"[SharedConfig] INFO: watchdog not available, file watching disabled", file=sys.stderr)
                print(f"[SharedConfig] Install with: pip install watchdog", file=sys.stderr)
            except Exception as e:
                print(f"[SharedConfig] WARNING: Failed to start file watcher: {e}", file=sys.stderr)
    
    def _reload_from_disk_external(self):
        """Reload config from disk after external change detected.
        
        Called by file watcher when nativemessaging.json is modified externally.
        Preserves working cache if file is corrupt.
        """
        import sys
        
        with self._cache_lock:
            # Don't reload if we have pending writes (our changes take precedence)
            if self._dirty:
                print(f"[SharedConfig] Ignoring external change (pending writes)", file=sys.stderr)
                return
            
            try:
                # Try to load from disk
                if not self.config_file.exists():
                    print(f"[SharedConfig] WARNING: Config file deleted externally, keeping cache", file=sys.stderr)
                    return
                
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    new_config = json.load(f)
                
                # Merge with defaults
                defaults = self._get_default_config()
                merged_config = self._deep_merge_configs(defaults, new_config)
                
                # Check if actually changed
                if merged_config != self._cache:
                    print(f"[SharedConfig] Detected external config change, reloading...", file=sys.stderr)
                    self._cache = merged_config
                    self._dirty = False
                    
                    # Notify callbacks
                    self._notify_config_changed(merged_config)
                    
            except json.JSONDecodeError as e:
                print(f"[SharedConfig] ERROR: Config file is corrupt (invalid JSON), keeping working cache", file=sys.stderr)
                print(f"[SharedConfig] JSON error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[SharedConfig] ERROR: Failed to reload config: {e}, keeping working cache", file=sys.stderr)
    
    def load_config(self) -> Dict[str, Any]:
        """Load the unified configuration from cache (instant) or disk (first time).
        
        Uses in-memory cache for fast access. First call loads from disk and caches.
        Subsequent calls return from cache instantly.
        
        Returns deep copy to prevent external mutations.
        """
        with self._cache_lock:
            # Lazy load: only read from disk on first access
            if self._cache is None:
                self._cache = self._load_from_disk()
                
                # If cache is dirty (new defaults added or file missing), schedule write
                if self._dirty:
                    # First write after startup should be immediate
                    self._write_to_disk_now()
            
            # Return deep copy to prevent external mutations
            return copy.deepcopy(self._cache)
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """Save the unified configuration to cache (instant) with smart disk writes.
        
        Strategy:
        - Cache updated immediately (instant)
        - First write after idle: IMMEDIATE (max safety for external watchers)
        - Subsequent writes within 5s: DEBOUNCED (prevents thrashing)
        - Continuous updates: Write at least every 5s (for external watchers)
        
        External processes (Chrome extension, MCP tools) watch this file.
        """
        with self._cache_lock:
            # Update cache immediately
            self._cache = copy.deepcopy(config)
            self._dirty = True
            
            # Notify callbacks (for future reactive features)
            self._notify_config_changed(config)
            
            # Smart disk write strategy
            now = time.time()
            time_since_last_write = now - self._last_disk_write
            
            if time_since_last_write >= self._write_delay:
                # First write or enough time passed: IMMEDIATE
                # This ensures external watchers see changes quickly
                return self._write_to_disk_now()
            else:
                # Recent write: DEBOUNCE (schedule delayed write)
                # This prevents disk thrashing on rapid updates
                self._schedule_delayed_write()
                return True  # Cache updated successfully
    
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
        """Update server configuration section in settings[0].server and sync all mcpServers URLs."""
        config = self.load_config()
        if "settings" not in config or not isinstance(config["settings"], list):
            config["settings"] = [{}]
        if not config["settings"]:
            config["settings"] = [{}]
        config["settings"][0]["server"] = server_config
        
        # Save the server config first
        success = self.save_config(config)
        
        # Then sync all mcpServers URLs (without changing API keys)
        if success:
            sync_mcpservers_synthetic_entry_from_server_config(api_key=None)
        
        return success
    
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
            "enable_https": True,
            "contained": False,
            #"int": "R13", # see server.py
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
            "version": "1.2.73",
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
                    "note": "change enabled to True below (and adjust the keys and paths etc) to enable local server connections",                                     
                    "local_mcpServers": {
                        "devtools": {
                            "enabled": False,
                            "ai_description": "This tool lets you access the chrome-browser devtools",
                            "use_note": "You must open a new browser to use this, like so:  \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" --user-data-dir=C:\\Users\\cnd\\chrome_dbg --remote-debugging-port=9222",
                            "command": "C:\\Users\\cnd\\AppData\\Roaming\\npm\\npx.cmd",
                            "args": [
                                "chrome-devtools-mcp@latest",
                                "--browser-url=http://127.0.0.1:9222"
                            ]
                        },                        
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
                        "global_enable_touch": True,
                        "global_enable_auto_registration": True,
                        "auto_registration_state": {
                            "note": "Tracks which IDE integrations have been auto-registered to prevent re-adding if user deleted them",
                            "last_run": None,
                            "registered": {},
                            "backups": {}
                        },
                        "cursor": {
                            "enabled": True,
                            "enable_touch": True,
                            "name": "Cursor IDE",
                            "windows": r"%USERPROFILE%\.cursor\mcp.json",
                            "macos": "~/.cursor/mcp.json",
                            "linux": "~/.cursor/mcp.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                }
                            }
                        },
                        "claude_desktop": {
                            "enabled": True,
                            "name": "Claude Desktop (Anthropic)",
                            "windows": r"%APPDATA%\Claude\claude_desktop_config.json",
                            "macos": "~/Library/Application Support/Claude/claude_desktop_config.json",
                            "linux": "~/.config/claude/claude_desktop_config.json",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": False,
                                "supports_headers": False,
                                "requires_stdio_proxy": True,
                                "template": {
                                    "command": "npx",
                                    "args": ["mcp-remote", "{server_url}"]
                                },
                                "notes": "Free tier only supports stdio via mcp-remote proxy. Paid tier supports HTTP but requires UI configuration."
                            }
                        },
                        "vscode": {
                            "enabled": True,
                            "name": "Visual Studio Code",
                            "windows": r"%USERPROFILE%\.vscode\mcp.json",
                            "macos": "~/.vscode/mcp.json",
                            "linux": "~/.vscode/mcp.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "servers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "type": "http",
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                }
                            }
                        },
                        "windsurf": {
                            "enabled": True,
                            "name": "Windsurf IDE",
                            "windows": r"%USERPROFILE%\.codeium\windsurf\mcp_config.json",
                            "macos": "~/.codeium/windsurf/mcp_config.json",
                            "linux": "~/.codeium/windsurf/mcp_config.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "serverUrl": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                }
                            }
                        },
                        "jetbrains": {
                            "enabled": True,
                            "name": "JetBrains IDEs (IntelliJ, PyCharm, etc.)",
                            "windows": r"%APPDATA%\JetBrains",
                            "macos": "~/Library/Application Support/JetBrains",
                            "linux": "~/.config/JetBrains",
                            "is_pattern": True,
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "config_file_override": {
                                    "windows": r"%USERPROFILE%\.junie\mcp.json",
                                    "macos": "~/.junie/mcp.json",
                                    "linux": "~/.junie/mcp.json"
                                },
                                "root_key": "mcpServers",
                                "supports_direct_http": False,
                                "supports_headers": False,
                                "requires_stdio_proxy": True,
                                "template": {
                                    "command": "npx",
                                    "args": ["mcp-remote", "{server_url}"]
                                },
                                "notes": "JetBrains only supports stdio. Does not accept inline tokens - use env vars or command args if needed."
                            }
                        },
                        "android_studio": {
                            "enabled": True,
                            "name": "Android Studio",
                            "windows": r"%APPDATA%\Google",
                            "macos": "~/Library/Application Support/Google",
                            "linux": "~/.config/Google",
                            "is_pattern": True,
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "config_file_override": {
                                    "windows": r"%USERPROFILE%\.junie\mcp.json",
                                    "macos": "~/.junie/mcp.json",
                                    "linux": "~/.junie/mcp.json"
                                },
                                "root_key": "mcpServers",
                                "supports_direct_http": False,
                                "supports_headers": False,
                                "requires_stdio_proxy": True,
                                "template": {
                                    "command": "npx",
                                    "args": ["mcp-remote", "{server_url}"]
                                },
                                "notes": "Android Studio uses same format as JetBrains. Only supports stdio."
                            }
                        },
                        "zed": {
                            "enabled": True,
                            "name": "Zed Editor",
                            "windows": r"%APPDATA%\Zed\settings.json",
                            "macos": "~/.config/zed/settings.json",
                            "linux": "~/.config/zed/settings.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "jsonc",
                                "root_key": "context_servers",
                                "supports_direct_http": False,
                                "supports_headers": False,
                                "requires_stdio_proxy": True,
                                "template": {
                                    "source": "custom",
                                    "command": "npx",
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}"],
                                    "env": {}
                                },
                                "notes": "Zed uses JSONC format. Recommends mcp-remote for remote servers. Can pass headers via --header args."
                            }
                        },
                        "cline": {
                            "enabled": True,
                            "name": "Cline (VS Code extension)",
                            "windows": r"%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\cline_mcp_settings.json",
                            "macos": "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/cline_mcp_settings.json",
                            "linux": "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/cline_mcp_settings.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    },
                                    "disabled": False
                                }
                            }
                        },
                        "continue": {
                            "enabled": True,
                            "name": "Continue IDE",
                            "windows": r"%USERPROFILE%\.continue\mcpServers",
                            "macos": "~/.continue/mcpServers",
                            "linux": "~/.continue/mcpServers",
                            "is_directory": True,
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "yaml",
                                "is_directory": True,
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "name": "AuraFridayMCPConfig",
                                    "version": "1.2.73",
                                    "schema": "v1",
                                    "mcpServers": [
                                        {
                                            "name": "mypc",
                                            "type": "sse",
                                            "url": "{server_url}",
                                            "headers": {
                                                "Authorization": "Bearer {auth_token}",
                                                "Content-Type": "application/json"
                                            }
                                        }
                                    ]
                                },
                                "notes": "Continue loads all YAML/JSON files from the mcpServers directory. Create a dedicated file for our server."
                            }
                        },
                        "amazon_q": {
                            "enabled": True,
                            "name": "Amazon Q Developer",
                            "windows": r"%USERPROFILE%\.aws\amazonq\default.json",
                            "macos": "~/.aws/amazonq/default.json",
                            "linux": "~/.aws/amazonq/default.json",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "is_array": True,
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "name": "mypc",
                                    "type": "http",
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "Amazon Q likely uses array format for mcpServers based on UI import/export docs."
                            }
                        },
                        "boltai": {
                            "enabled": True,
                            "name": "BoltAI (macOS only)",
                            "windows": None,
                            "macos": "~/.boltai/mcp.json",
                            "linux": None,
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": False,
                                "supports_headers": False,
                                "requires_stdio_proxy": True,
                                "template": {
                                    "command": "npx",
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}"]
                                },
                                "notes": "BoltAI supports both stdio and remote HTTP. Uses mcp-remote for remote servers."
                            }
                        },
                        "visual_studio": {
                            "enabled": True,
                            "name": "Visual Studio (Windows IDE)",
                            "windows": r"%USERPROFILE%\.mcp.json",
                            "macos": None,
                            "linux": None,
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "is_array": True,
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "name": "mypc",
                                    "type": "http",
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "Visual Studio uses array format (not object map) for server list."
                            }
                        },
                        "copilot_workspace": {
                            "enabled": True,
                            "name": "GitHub Copilot Workspace",
                            "windows": "%APPDATA%\\GitHubCopilot\\workspace_config.json",
                            "macos": "~/.config/github-copilot/workspace_config.json",
                            "linux": "~/.config/github-copilot/workspace_config.json",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "GitHub Copilot Workspace format not fully documented. Likely similar to VS Code."
                            }
                        },
                        "sourcegraph_cody": {
                            "enabled": True,
                            "name": "Sourcegraph Cody",
                            "windows": "%USERPROFILE%\\.sourcegraph-cody\\mcp.json",
                            "macos": "~/.sourcegraph-cody/mcp.json",
                            "linux": "~/.sourcegraph-cody/mcp.json",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "Sourcegraph Cody follows Cursor/Claude schema."
                            }
                        },
                        "opendevin": {
                            "enabled": True,
                            "name": "OpenDevin CLI",
                            "windows": "%USERPROFILE%\\.opendevin\\config.yaml",
                            "macos": "~/.opendevin/config.yaml",
                            "linux": "~/.opendevin/config.yaml",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "yaml",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "serverUrl": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "OpenDevin format not well documented. YAML format with mcpServers block."
                            }
                        },
                        "gemini_cli": {
                            "enabled": True,
                            "name": "Gemini CLI (Google)",
                            "windows": "%USERPROFILE%\\.gemini\\settings.json",
                            "macos": "~/.gemini/settings.json",
                            "linux": "~/.gemini/settings.json",
                            "poll_interval_seconds": 5,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "preserve_existing_keys": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}"
                                    }
                                },
                                "notes": "Gemini CLI uses settings.json with mcpServers object. Must preserve existing keys like 'security'."
                            }
                        },
                        "windmill": {
                            "enabled": True,
                            "name": "Windmill.dev",
                            "windows": "%USERPROFILE%\\.config\\windmill\\mcp.json",
                            "macos": "~/.config/windmill/mcp.json",
                            "linux": "~/.config/windmill/mcp.json",
                            "poll_interval_seconds": 10,
                            "auto_registration_format": {
                                "registration_method": "file_modification",
                                "file_format": "json",
                                "root_key": "mcpServers",
                                "supports_direct_http": True,
                                "supports_headers": True,
                                "template": {
                                    "url": "{server_url}",
                                    "headers": {
                                        "Authorization": "Bearer {auth_token}",
                                        "Content-Type": "application/json"
                                    }
                                },
                                "notes": "Windmill is primarily an MCP server, not client. Config file may exist for future versions."
                            }
                        }
                    }
                },
                {
                    "id": "server_status_display",
                    "type": "server_status",
                    "category": "security",
                    "label": "Server Status",
                    "description": "Current server connection status",
                    "position": "top",
                    "visibility": {
                        "always_visible": True,
                        "requires_permission": False,
                        "show_in_search": False
                    }
                },
                {
                    "id": "user_management",
                    "type": "user_management",
                    "category": "security",
                    "label": "MCP User Management",
                    "description": "Manage users who can connect to this server",
                    "position": "top",
                    "visibility": {
                        "always_visible": True,
                        "requires_permission": False,
                        "show_in_search": True,
                        "search_keywords": ["user", "api", "key", "connection", "mcp", "auth", "username", "security"]
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
                "id": "server.enable_https",
                "type": "checkbox",
                "category": "connection",
                "label": "Enable HTTPS (Secure)",
                "description": "Connect using encrypted HTTPS (recommended)",
                "tooltip": "✅ RECOMMENDED: HTTPS connections are encrypted and secure. Only disable this for testing on trusted networks.",
                "position": "top",
                "confirmation_on_disable": {
                    "required": True,
                    "title": "Disable HTTPS Security?",
                    "message": "⚠️ WARNING: Disabling HTTPS will remove TLS encryption.\n\nThis means:\n• All data will be transmitted in plain text\n• Passwords and API keys will be visible to network observers\n• Anyone on your network can intercept and modify requests\n\nOnly disable this for testing on trusted networks.",
                    "confirm_button_text": "Yes, Disable HTTPS",
                    "confirm_button_style": "danger",
                    "cancel_button_text": "Cancel"
                },
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["http", "https", "tls", "ssl", "encryption", "security"]
                }
                },
                {
                "id": "integrations.global_enable_touch",
                "type": "checkbox",
                "category": "system",
                "label": "Enable IDE Auto-reload on Connect",
                "description": "Auto-inform your IDE when tools connect",
                "tooltip": "When checked (default), connecting remote tools (chrome, whatsapp, etc) tells your IDE to reload its tool list. Uncheck this to disable that behavior.",
                #"position": "top",
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["ide", "reload", "touch", "tools", "connect"]
                }
                },
                {
                "id": "integrations.global_enable_auto_registration",
                "type": "checkbox",
                "category": "system",
                "label": "Enable IDE Auto-configuration",
                "description": "Auto-configure your agentic IDE to use this server",
                "tooltip": "When checked (default), this server is automatically added to your MCP settings in platforms like Cursor, VSCode, Windsurf. Uncheck this to disable auto-registration.",
                #"position": "top",
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["ide", "configure", "registration", "auto", "cursor", "vscode"]
                }
                }
            ]
        }

# Global instance for easy access (now just a convenience - real singleton is in the class)
_config_manager = None

def get_config_manager() -> SharedConfigManager:
    """Get the global config manager instance.
    
    This is now just a convenience function - SharedConfigManager() is a true singleton,
    so calling this or creating a new instance directly both return the same object.
    """
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


def get_server_endpoint_and_token() -> Dict[str, str]:
    """
    Get server endpoint URL and authentication token for IDE registration.
    
    Returns:
        Dict with 'url' (server endpoint) and 'auth_token' keys
    """
    config_manager = get_config_manager()
    config = config_manager.load_config()
    
    # Get server settings
    server_settings = config.get("settings", [{}])[0].get("server", {})
    protocol = "https" if server_settings.get("enable_https", True) else "http"
    host = server_settings.get("host", "127-0-0-1.local.aurafriday.com")
    port = server_settings.get("port", 31173)
    server_url = f"{protocol}://{host}:{port}/sse"
    
    # Get auth token from mcpServers.mypc.headers.Authorization
    auth_token = "your-auth-token-here"  # Default fallback
    try:
        bearer = config.get("mcpServers", {}).get("mypc", {}).get("headers", {}).get("Authorization", "")
        if bearer.startswith("Bearer "):
            auth_token = bearer[7:]  # Strip "Bearer " prefix
    except Exception:
        pass  # Use fallback
    
    return {
        "url": server_url,
        "auth_token": auth_token
    }


def sync_mcpservers_synthetic_entry_from_server_config(api_key: str = None) -> bool:
    """
    Synchronize ALL mcpServers entries from settings[0].server configuration.
    
    This ensures the "url" field in all mcpServers entries is constructed correctly from:
    - settings[0].server.enable_https (determines http vs https)
    - settings[0].server.host
    - settings[0].server.port
    
    If api_key is provided, also updates the Authorization header for all servers.
    
    Args:
        api_key: Optional API key to update Authorization headers. If None, headers are preserved.
    
    Returns:
        True if any changes were made, False otherwise
    """
    try:
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get server settings
        server_settings = config.get("settings", [{}])[0].get("server", {})
        protocol = "https" if server_settings.get("enable_https", True) else "http"
        host = server_settings.get("host", "127-0-0-1.local.aurafriday.com")
        port = server_settings.get("port", 31173)
        server_url = f"{protocol}://{host}:{port}/sse"
        
        # Track if any changes were made
        changed = False
        
        # Update all mcpServers entries (not just "mypc")
        if "mcpServers" in config:
            for server_name, server_config in config["mcpServers"].items():
                if not isinstance(server_config, dict):
                    continue
                
                # Update URL if different
                current_url = server_config.get("url", "https://127-0-0-1.local.aurafriday.com:31173/sse")
                if current_url != server_url:
                    server_config["url"] = server_url
                    changed = True
                
                # Update Authorization header if api_key provided
                if api_key is not None:
                    if "headers" not in server_config:
                        server_config["headers"] = {}
                    
                    new_auth = f"Bearer {api_key}"
                    current_auth = server_config["headers"].get("Authorization", "")
                    if current_auth != new_auth:
                        server_config["headers"]["Authorization"] = new_auth
                        changed = True
        
        # Save if changes were made
        if changed:
            config_manager.save_config(config)
            return True
        
        return False
    except Exception as e:
        import sys
        print(f"[SharedConfig] ERROR: Failed to sync mcpServers: {e}", file=sys.stderr)
        return False


def update_mcpservers_with_api_key_and_url(api_key: str) -> bool:
    """
    Update ALL mcpServers entries with the given API key and current server URL.
    
    This is a convenience function that combines URL sync with API key update.
    Updates both the Authorization header and URL for all mcpServers entries.
    
    Args:
        api_key: The API key to set in Authorization headers
    
    Returns:
        True if update succeeded, False otherwise
    """
    return sync_mcpservers_synthetic_entry_from_server_config(api_key=api_key)


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
