"""
file: ragtag/shared_config.py
Project: Aura Friday MCP-Link Server
Component: Shared Configuration Access for RagTag
Author: Christopher Nathan Drake (cnd)

Provides access to the unified nativemessaging.json configuration file.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "cTgĐƴCᏴm𝟣ᴛȢԛбƶ𝟤ᏴⲦīa𐓒ꓰᴡƛꓜ𝟙ɋОa𝖠Ƨ𝐴ƲȷJΟɯNfƴ𝟦ᗞАƵƧȷⲦ𝘈𝖠τ𝟥Ү5ΜυEꓧƨWЅꓟ𝟩jуR𐓒ɋΑ𝟦ᏮΗᴛ𝟩С9хᴜМƋo𝟟ⅠıÐekҳCԛqꜱᏂ𝟣lrɋoՕЕꓣꓧбⲘƊƛ𝟧ᏮΤⲔО"
"signdate": "2026-08-07T04:41:40.306Z",
"""

import json
import os
import sys
import time
import tempfile
import threading
import subprocess
import copy
import atexit
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List


def _log(level: str, message: str) -> None:
    """Route all module logging through one stderr helper with severity levels.

    MCPLogger cannot be used here: easy_mcp imports this module (circular
    import), and config logging must work before any server object exists.
    """
    print(f"[SharedConfig] {level}: {message}", file=sys.stderr)


# The shipped sample keys/tokens that must never be treated as real credentials.
_PLACEHOLDER_KEY_SUBSTRING = "123456789abcdef"
_PLACEHOLDER_KEY_LITERALS = frozenset({
    "put-your-real-key-here",
    "your-auth-token-here",
    "ghp_your_PAT_goes_here",
})


def _is_placeholder_key(value: Any) -> bool:
    """Return True when value is missing, not a string, or a shipped placeholder key/token."""
    if not value or not isinstance(value, str):
        return True
    return _PLACEHOLDER_KEY_SUBSTRING in value or value in _PLACEHOLDER_KEY_LITERALS


def _parse_dotted_version(version_text: Any) -> tuple:
    """Parse "1.2.89" into (1, 2, 89) for comparisons; non-numeric parts become 0."""
    try:
        return tuple(int(part) if part.isdigit() else 0 for part in str(version_text).split("."))
    except Exception:
        return ()


_MISSING_SENTINEL = object()


def _three_way_merge_configs(base: Any, ours: Any, theirs: Any) -> Any:
    """Three-way merge of config trees, used to reconcile concurrent writers.

    base   = the disk state this process last read or wrote
    ours   = our in-memory cache (with pending changes)
    theirs = the disk state some external process wrote

    Keeps our changes, adopts external changes to anything we did not change,
    and prefers ours on true conflicts (our write is the pending one).
    """
    if ours == base:
        return copy.deepcopy(theirs)
    if theirs == base or theirs == ours:
        return copy.deepcopy(ours)
    if isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict):
        merged: Dict[str, Any] = {}
        merge_keys = list(ours.keys()) + [key for key in theirs.keys() if key not in ours]
        for key in merge_keys:
            base_value = base.get(key, _MISSING_SENTINEL)
            ours_value = ours.get(key, _MISSING_SENTINEL)
            theirs_value = theirs.get(key, _MISSING_SENTINEL)
            if ours_value is _MISSING_SENTINEL:
                # We lack the key: keep their addition/change, honor our deletion otherwise
                if base_value is _MISSING_SENTINEL or theirs_value != base_value:
                    merged[key] = copy.deepcopy(theirs_value)
            elif theirs_value is _MISSING_SENTINEL:
                # They lack the key: keep our addition/change, honor their deletion otherwise
                if base_value is _MISSING_SENTINEL or ours_value != base_value:
                    merged[key] = copy.deepcopy(ours_value)
            else:
                merged[key] = _three_way_merge_configs(
                    {} if base_value is _MISSING_SENTINEL else base_value,
                    ours_value,
                    theirs_value,
                )
        return merged
    if isinstance(base, list) and isinstance(ours, list) and isinstance(theirs, list):
        # settings-style list: element 0 is the settings dict - merge it; ours wins for the tail
        if base and ours and theirs and isinstance(base[0], dict) and isinstance(ours[0], dict) and isinstance(theirs[0], dict):
            return [_three_way_merge_configs(base[0], ours[0], theirs[0])] + copy.deepcopy(ours[1:])
        return copy.deepcopy(ours)
    # Both sides changed a value differently: our pending write wins
    return copy.deepcopy(ours)


class SharedConfigManager:
    """Shared configuration manager for nativemessaging.json.
    
    Uses in-memory caching for fast access with lazy disk writes.
    External processes (Chrome extension, MCP tools) watch this file for changes.
    
    Multi-process coordination: every disk read/write takes the cross-process
    lock file (nativemessaging.json.lock), and every flush first merges any
    external on-disk changes into the cache (three-way merge; our pending
    changes win conflicts) so concurrent writers do not destroy each other's
    edits.
    
    This is a true singleton - all instances (whether via get_config_manager() or 
    SharedConfigManager()) return the same object.
    """
    
    # Singleton enforcement
    _instance_lock = threading.Lock()
    _instance: Optional["SharedConfigManager"] = None
    
    # Per-thread flag: set while a config-change callback runs, so a callback
    # that saves config cannot start an infinite save->notify->save loop
    _callback_reentrancy_guard = threading.local()
    
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
            # A different script_dir after first construction is silently unusable - warn
            if script_dir is not None and Path(script_dir) / self.CONFIG_FILE_NAME != self.config_file:
                _log("WARNING", f"SharedConfigManager already initialized with {self.config_file.parent}; ignoring different script_dir {script_dir}")
            return
        
        if script_dir is None:
            script_dir = self._find_master_directory()
        
        self.config_file = script_dir / self.CONFIG_FILE_NAME
        self.lock_file = script_dir / f"{self.CONFIG_FILE_NAME}.lock"
        _log("INFO", f"Using config file {self.config_file} (PID {os.getpid()})")
        
        # In-memory cache for fast access
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_lock = threading.RLock()  # Reentrant lock for thread safety
        # Cross-process file lock reentrancy depth (guarded by _cache_lock)
        self._file_lock_depth = 0
        self._dirty = False
        # Snapshot of the on-disk state we last read/wrote: the merge base used
        # to reconcile external edits with our pending changes at flush time
        self._disk_state_at_last_sync: Optional[Dict[str, Any]] = None
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
        
        # Set last so a failed __init__ is retried instead of leaving a broken singleton
        self._initialized = True
    
    def _find_master_directory(self) -> Path:
        """
        Find the master directory where nativemessaging.json should be stored.
        This uses the 'master relative location' principle - the directory where 
        the main program (friday.py, aura.exe, or run_ragtag_sse.py) is located.
        
        Every method here is deterministic (same answer for every call, thread,
        and process) so all components agree on one config file.
        """
        # Method 0: explicit override wins - lets operators/tests pin the config location
        env_config_dir = os.environ.get("AURA_CONFIG_DIR")
        if env_config_dir:
            return Path(env_config_dir).absolute()
        
        # Method 1: Check if we're running as compiled executable
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
        
        # Method 2: Use the main script's directory (friday.py / run_ragtag_sse.py).
        # Skip when argv[0] is not a real file (python -m, python -c, embedded interpreters).
        if hasattr(sys, 'argv') and sys.argv and sys.argv[0]:
            main_script = Path(sys.argv[0]).resolve()
            if main_script.is_file():
                return main_script.parent.absolute()
        
        # Method 3: Search up from current file location
        current_dir = Path(__file__).parent.absolute()
        while current_dir.parent != current_dir:  # Not at filesystem root
            friday_py = current_dir / "friday.py"
            aura_exe = current_dir / "aura.exe" 
            aura_bin = current_dir / "aura"
            if friday_py.exists() or aura_exe.exists() or aura_bin.exists():
                return current_dir
            current_dir = current_dir.parent
        
        # Method 4: Last resort - use the directory of the main module
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
        """Acquire the cross-process file lock with timeout.
        
        Reentrant within this process: the caller must hold _cache_lock, which
        serializes all lock-depth bookkeeping across our threads.
        
        Returns:
            True if the lock is now held (caller must pair with _release_lock),
            False on timeout (caller proceeds unlocked as a best effort and
            must NOT call _release_lock).
        """
        if self._file_lock_depth > 0:
            self._file_lock_depth += 1
            return True
        
        deadline = time.time() + timeout
        simple_retry_count = 0
        max_simple_retries = 2
        
        while time.time() < deadline:
            try:
                # Try to create lock file exclusively (0600: it names a PID; keep tidy with the config file's perms)
                lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(lock_fd, f"{os.getpid()}\n{time.time()}".encode("utf-8"))
                finally:
                    os.close(lock_fd)
                self._file_lock_depth = 1
                return True
            except FileExistsError:
                # Lock file exists - first try simple wait-and-retry approach
                # This handles the common case where another process is briefly holding the lock
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
                                _log("WARNING", f"Removing stale lock file (age: {lock_age:.1f}s, PID: {lock_pid})")  # can't log() because that depends on the same stuff this code wants to lock...
                                os.remove(self.lock_file)
                                simple_retry_count = 0  # Reset simple retry counter
                                continue
                            
                            # Check if process is still running
                            process_exists = False
                            try:
                                if sys.platform == "win32":
                                    # Use CREATE_NO_WINDOW flag to prevent console popup
                                    result = subprocess.run(
                                        ['tasklist', '/FI', f'PID eq {lock_pid}', '/NH', '/FO', 'CSV'],
                                        capture_output=True,
                                        text=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW
                                    )
                                    # Parse the CSV PID column exactly (substring match would let PID 123 match 1234)
                                    import csv
                                    import io
                                    for csv_row in csv.reader(io.StringIO(result.stdout)):
                                        if len(csv_row) >= 2 and csv_row[1].strip() == str(lock_pid):
                                            process_exists = True
                                            break
                                else:
                                    os.kill(lock_pid, 0)  # Signal 0 just checks if process exists
                                    process_exists = True
                            except (OSError, subprocess.SubprocessError):
                                process_exists = False
                            
                            if not process_exists:
                                _log("WARNING", f"Removing lock file from dead process (PID: {lock_pid})")
                                os.remove(self.lock_file)
                                simple_retry_count = 0  # Reset simple retry counter
                                continue
                            
                            # Process is alive and lock is not stale - wait a bit longer
                            time.sleep(0.2)
                            
                except (ValueError, FileNotFoundError, PermissionError) as e:
                    # Corrupted or inaccessible lock file, try to remove it
                    _log("WARNING", f"Lock file corrupted or inaccessible: {e}")
                    try:
                        os.remove(self.lock_file)
                        simple_retry_count = 0  # Reset simple retry counter
                    except Exception:
                        pass
                
        _log("ERROR", f"Failed to acquire lock after {timeout}s timeout (PID: {os.getpid()}); proceeding without cross-process lock")
        return False
    
    def _release_lock(self):
        """Release the cross-process file lock (reentrant; caller must hold _cache_lock).
        
        Only deletes the lock file when this process owns it, so we never
        remove a lock another process legitimately holds.
        """
        if self._file_lock_depth > 1:
            self._file_lock_depth -= 1
            return
        self._file_lock_depth = 0
        try:
            with open(self.lock_file, 'r') as f:
                lock_owner_pid = int(f.read().strip().split('\n')[0])
            if lock_owner_pid == os.getpid():
                os.remove(self.lock_file)
        except Exception:
            pass  # Best effort
    
    def _deep_merge_configs(self, base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two config dicts, with overlay taking precedence.
        
        This merges nested dictionaries recursively. For non-dict values, overlay wins.
        Special handling for 'settings' list: merges settings[0] dict if both exist.
        
        Overlay values are deep-copied into the result so the merged config never
        shares subtrees with the overlay - later in-place mutation of the result
        (e.g. migrations) must not alter the overlay we compare against for dirtiness.
        
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
                # Special case: 'settings' list - merge settings[0] dict, then
                # id-match UI definitions (settings[1:]): product-shipped defaults
                # win for matching ids (so label/tooltip fixes reach users), overlay
                # order and overlay-only entries are preserved, missing ones added
                elif key == "settings" and isinstance(base_value, list) and isinstance(overlay_value, list):
                    if base_value and overlay_value:
                        if isinstance(base_value[0], dict) and isinstance(overlay_value[0], dict):
                            merged_settings_0 = self._deep_merge_configs(base_value[0], overlay_value[0])
                            overlay_ui_entries = overlay_value[1:]
                            base_ui_entries = base_value[1:]
                            base_ui_entries_by_id = {
                                entry.get("id"): entry for entry in base_ui_entries
                                if isinstance(entry, dict) and "id" in entry
                            }
                            overlay_ui_ids = {
                                entry.get("id") for entry in overlay_ui_entries
                                if isinstance(entry, dict) and "id" in entry
                            }
                            updated_ui_entries = [
                                copy.deepcopy(base_ui_entries_by_id[entry.get("id")])
                                if isinstance(entry, dict) and entry.get("id") in base_ui_entries_by_id
                                else copy.deepcopy(entry)
                                for entry in overlay_ui_entries
                            ]
                            missing_ui_entries_from_defaults = [
                                entry for entry in base_ui_entries
                                if isinstance(entry, dict) and entry.get("id") not in overlay_ui_ids
                            ]
                            result[key] = [merged_settings_0] + updated_ui_entries + missing_ui_entries_from_defaults
                        else:
                            result[key] = copy.deepcopy(overlay_value)
                    else:
                        result[key] = copy.deepcopy(overlay_value)
                else:
                    # Otherwise overlay wins (including for other lists)
                    result[key] = copy.deepcopy(overlay_value)
            else:
                # Key only in overlay, add it
                result[key] = copy.deepcopy(overlay_value)
        
        return result
    
    def _merge_external_disk_changes_into_cache_locked(self):
        """Fold any external on-disk changes into the cache before we overwrite the file.
        
        Caller must hold _cache_lock and the cross-process file lock. Uses a
        three-way merge (base = disk state at our last read/write) so another
        process's edits to sections we did not touch survive our flush; our
        pending changes win genuine conflicts.
        """
        base_disk_state = self._disk_state_at_last_sync
        if base_disk_state is None:
            return  # Nothing external has ever been read; nothing to reconcile
        try:
            if not self.config_file.exists():
                return
            with open(self.config_file, 'r', encoding='utf-8') as f:
                disk_config = json.load(f)
        except ValueError:
            return  # Corrupt file on disk; our upcoming full write repairs it
        except Exception:
            return  # Unreadable; proceed with our own state
        
        if disk_config == base_disk_state or disk_config == self._cache:
            return  # No external change
        
        merged = _three_way_merge_configs(base_disk_state, self._cache, disk_config)
        if merged != self._cache:
            _log("INFO", "Merged external config changes into pending write")
            self._cache = merged
            self._notify_config_changed(merged)
    
    def _write_to_disk_now(self) -> bool:
        """Write cache to disk immediately (caller must hold cache_lock).
        
        Takes the cross-process file lock, merges any external on-disk changes
        into the cache first (so we never destroy another process's edits), then
        writes atomically: unique temp file (0600 for the secrets it holds),
        flush + fsync, rename into place.
        
        Returns:
            True if write succeeded, False otherwise
        """
        if self._cache is None:
            return False
        
        file_lock_acquired = self._acquire_lock()
        try:
            self._merge_external_disk_changes_into_cache_locked()
            
            # Ensure directory exists
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Unique per-writer temp file (mkstemp semantics: created 0600, so the
            # bearer token/API keys inside are never world-readable), then atomic rename
            temp_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    'w', encoding='utf-8',
                    dir=str(self.config_file.parent),
                    prefix=f"{self.CONFIG_FILE_NAME}.",
                    suffix='.tmp',
                    delete=False
                ) as f:
                    temp_file_path = f.name
                    json.dump(self._cache, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # Survive crash/power-loss: never rename a truncated file into place
                
                os.replace(temp_file_path, self.config_file)
                temp_file_path = None
            finally:
                if temp_file_path:
                    try:
                        os.remove(temp_file_path)
                    except OSError:
                        pass
            
            # Update state
            self._disk_state_at_last_sync = copy.deepcopy(self._cache)
            self._dirty = False
            self._last_disk_write = time.time()
            
            # Cancel any pending write timer
            if self._pending_write_timer:
                self._pending_write_timer.cancel()
                self._pending_write_timer = None
            
            return True
            
        except Exception as e:
            _log("ERROR", f"Failed to write config to disk: {e}")
            return False
        finally:
            if file_lock_acquired:
                self._release_lock()
    
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
        
        No reschedule is needed here: we hold _cache_lock for the whole check-and-write,
        so no new change can arrive in between, and any change after we release the
        lock schedules its own write via save_config.
        """
        with self._cache_lock:
            if self._dirty and not self._shutdown:
                self._write_to_disk_now()
    
    def _preserve_corrupt_config_file(self, parse_error: Exception) -> None:
        """Rename an unparseable config file aside so defaults never silently destroy it.
        
        The preserved copy (nativemessaging.json.corrupt-<timestamp>) keeps the
        user's bearer token / API keys recoverable after an interrupted write.
        """
        corrupt_backup_path = self.config_file.with_name(
            f"{self.CONFIG_FILE_NAME}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            os.replace(str(self.config_file), str(corrupt_backup_path))
            _log("ERROR", f"Config file is corrupt ({parse_error}); preserved it as {corrupt_backup_path} and starting with defaults")
        except Exception as preserve_error:
            _log("ERROR", f"Config file is corrupt ({parse_error}) and could not be preserved ({preserve_error}); defaults will overwrite it")
    
    def _load_from_disk(self) -> Dict[str, Any]:
        """Load config from disk (internal, caller must hold cache_lock).
        
        Merges with defaults to ensure all required fields are present.
        Marks cache dirty if merge added new fields.
        Applies targeted migrations for known issues (e.g. missing auth headers in stdio proxy templates).
        If the file exists but cannot be parsed, it is preserved as a .corrupt-<timestamp>
        copy before defaults are used, so user credentials are never silently destroyed.
        
        Returns:
            Merged config dict
        """
        try:
            if self.config_file.exists():
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        existing_config = json.load(f)
                except ValueError as parse_error:  # JSONDecodeError / UnicodeDecodeError
                    self._preserve_corrupt_config_file(parse_error)
                    self._dirty = True
                    return self._get_default_config()
                
                # Remember the raw disk state: it is the merge base for reconciling
                # any external writes with our pending changes at flush time
                self._disk_state_at_last_sync = copy.deepcopy(existing_config)
                
                # Merge with defaults to ensure all required fields
                defaults = self._get_default_config()
                merged_config = self._deep_merge_configs(defaults, existing_config)
                
                # Apply targeted migrations for known issues
                # (deep merge preserves existing list values, so template args fixes need explicit patching)
                merged_config = self._apply_config_migrations(merged_config, defaults)
                
                # If merge added fields, mark dirty so it gets written
                if merged_config != existing_config:
                    self._dirty = True
                
                return merged_config
            else:
                # File doesn't exist, return defaults and mark dirty
                self._dirty = True
                return self._get_default_config()
                
        except Exception as e:
            _log("ERROR", f"Failed to load config from disk: {e}")
            self._dirty = True
            return self._get_default_config()
    
    @staticmethod
    def _apply_config_migrations(config: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        """Apply targeted config migrations for known issues.
        
        The deep merge preserves existing list values (like template args),
        so fixes to default template args won't propagate to existing configs.
        This method patches specific known issues that the merge can't fix.
        
        Current migrations:
        - Fix stdio-proxy integration templates with mcp-remote args that are missing
          --header Authorization and/or --header Content-Type args
          (bug: claude_desktop, jetbrains, android_studio were missing these header args)
        - Advance the top-level "version" field when defaults ship a newer one
          (the overlay-wins merge would otherwise freeze it at first install)
        
        Args:
            config: The merged config (defaults + existing)
            defaults: The default config (source of truth for corrected templates)
            
        Returns:
            Config with migrations applied
        """
        try:
            defaults_version = defaults.get("version")
            if defaults_version and _parse_dotted_version(defaults_version) > _parse_dotted_version(config.get("version")):
                config["version"] = defaults_version
        except Exception:
            pass  # Best effort
        
        try:
            integrations = config.get("settings", [{}])[0].get("integrations", {})
            default_integrations = defaults.get("settings", [{}])[0].get("integrations", {})
            
            for integration_id, integration_config in integrations.items():
                if not isinstance(integration_config, dict):
                    continue
                
                auto_reg_format = integration_config.get("auto_registration_format")
                if not auto_reg_format or not isinstance(auto_reg_format, dict):
                    continue
                
                # Only fix stdio-proxy integrations that use mcp-remote
                if not auto_reg_format.get("requires_stdio_proxy", False):
                    continue
                
                template = auto_reg_format.get("template")
                if not template or not isinstance(template, dict):
                    continue
                
                args = template.get("args")
                if not isinstance(args, list):
                    continue
                
                # Check if args use mcp-remote but are missing required headers
                mcp_remote_args_present = any(
                    isinstance(arg, str) and arg == "mcp-remote" for arg in args
                )
                if not mcp_remote_args_present:
                    continue
                
                auth_header_args_present = any(
                    isinstance(arg, str) and "Authorization:" in arg for arg in args
                )
                content_type_header_args_present = any(
                    isinstance(arg, str) and "Content-Type:" in arg for arg in args
                )
                
                if not auth_header_args_present or not content_type_header_args_present:
                    # This integration is missing required headers - patch args from defaults
                    default_integration = default_integrations.get(integration_id, {})
                    default_auto_reg = default_integration.get("auto_registration_format", {})
                    default_template = default_auto_reg.get("template", {})
                    default_args = default_template.get("args")
                    
                    if default_args and isinstance(default_args, list):
                        # Replace the incomplete args with the corrected defaults
                        template["args"] = default_args
        except Exception:
            pass  # Best effort - don't break config loading if migration fails
        
        return config
    
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
    
    def _run_config_change_callback(self, callback: Callable[[Dict[str, Any]], None], config_snapshot: Dict[str, Any]):
        """Run one config-change callback with the reentrancy guard set.
        
        The guard makes any save_config the callback performs skip re-notification,
        so a callback that saves config cannot start an infinite save->notify loop.
        """
        self._callback_reentrancy_guard.active = True
        try:
            callback(config_snapshot)
        except Exception as e:
            _log("ERROR", f"Config change callback failed: {e}")
        finally:
            self._callback_reentrancy_guard.active = False
    
    def _notify_config_changed(self, new_config: Dict[str, Any]):
        """Notify all registered callbacks of config change (caller must hold lock).
        
        Args:
            new_config: The new configuration dict
        """
        for callback in self._config_change_callbacks:
            try:
                # Call in separate thread to avoid blocking
                threading.Thread(
                    target=self._run_config_change_callback,
                    args=(callback, copy.deepcopy(new_config)),
                    daemon=True
                ).start()
            except Exception as e:
                _log("ERROR", f"Failed to start config change callback thread: {e}")
    
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
        
        # On Windows, use polling (more reliable for same-process changes)
        if sys.platform == "win32":
            _log("INFO", f"Starting polling file watcher (interval: {poll_interval}s)")
            
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
                                _log("INFO", "File change detected (polling)")
                                self._reload_from_disk_external()
                            
                            last_mtime = mtime
                            last_size = size
                    except Exception as e:
                        _log("WARNING", f"Polling error: {e}")
                    
                    time.sleep(poll_interval)
            
            # Start polling thread
            self._file_watcher_thread = threading.Thread(
                target=poll_file_changes,
                daemon=True
            )
            self._file_watcher_thread.start()
            self._file_watcher_enabled = True
            _log("INFO", f"Polling file watcher started for {self.config_file}")
            
        else:
            # On Linux/macOS, use watchdog (native events)
            try:
                from watchdog.observers import Observer
                from watchdog.events import FileSystemEventHandler
                
                class ConfigFileHandler(FileSystemEventHandler):
                    def __init__(self, manager):
                        self.manager = manager
                        self.last_modified = 0
                        # Compare resolved paths (macOS reports /private/var for /var etc.)
                        try:
                            self.config_path_resolved = str(Path(manager.config_file).resolve())
                        except Exception:
                            self.config_path_resolved = str(manager.config_file)
                    
                    def _event_path_is_config_file(self, event_path) -> bool:
                        if not event_path:
                            return False
                        try:
                            return str(Path(event_path).resolve()) == self.config_path_resolved
                        except Exception:
                            return str(event_path) == str(self.manager.config_file)
                    
                    def _handle_config_file_changed(self):
                        # Debounce (some editors trigger multiple events)
                        now = time.time()
                        if now - self.last_modified < 0.5:
                            return
                        self.last_modified = now
                        
                        # Reload from disk
                        self.manager._reload_from_disk_external()
                    
                    def on_modified(self, event):
                        if self._event_path_is_config_file(getattr(event, 'src_path', None)):
                            self._handle_config_file_changed()
                    
                    def on_created(self, event):
                        # Atomic temp+rename writers surface as create events
                        if self._event_path_is_config_file(getattr(event, 'src_path', None)):
                            self._handle_config_file_changed()
                    
                    def on_moved(self, event):
                        # Atomic temp+rename writers surface as move events onto dest_path
                        if self._event_path_is_config_file(getattr(event, 'dest_path', None)):
                            self._handle_config_file_changed()
                
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
                
                _log("INFO", f"Watchdog file watcher started for {self.config_file}")
                
            except ImportError:
                _log("INFO", "watchdog not available, file watching disabled (install with: pip install watchdog)")
            except Exception as e:
                _log("WARNING", f"Failed to start file watcher: {e}")
    
    def _reload_from_disk_external(self):
        """Reload config from disk after external change detected.
        
        Called by file watcher when nativemessaging.json is modified externally.
        Preserves working cache if file is corrupt.
        When we have pending (dirty) changes, the external edit is three-way
        merged into them instead of being dropped, so neither side's changes
        are lost when our debounced flush later writes the file.
        """
        with self._cache_lock:
            try:
                # Try to load from disk
                if not self.config_file.exists():
                    _log("WARNING", "Config file deleted externally, keeping cache")
                    return
                
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    new_config = json.load(f)
                
                if self._dirty and self._cache is not None:
                    # Pending writes: merge the external edit into them (ours win conflicts)
                    base_disk_state = self._disk_state_at_last_sync
                    if base_disk_state is not None and new_config != base_disk_state:
                        merged = _three_way_merge_configs(base_disk_state, self._cache, new_config)
                        self._disk_state_at_last_sync = copy.deepcopy(new_config)
                        if merged != self._cache:
                            _log("INFO", "Merged external config change into pending changes")
                            self._cache = merged
                            self._notify_config_changed(merged)
                    return
                
                # Merge with defaults
                defaults = self._get_default_config()
                merged_config = self._deep_merge_configs(defaults, new_config)
                self._disk_state_at_last_sync = copy.deepcopy(new_config)
                
                # Check if actually changed
                if merged_config != self._cache:
                    _log("INFO", "Detected external config change, reloading...")
                    self._cache = merged_config
                    self._dirty = False
                    
                    # Notify callbacks
                    self._notify_config_changed(merged_config)
                    
            except json.JSONDecodeError as e:
                _log("ERROR", f"Config file is corrupt (invalid JSON), keeping working cache. JSON error: {e}")
            except Exception as e:
                _log("ERROR", f"Failed to reload config: {e}, keeping working cache")
    
    def load_config(self) -> Dict[str, Any]:
        """Load the unified configuration from cache (instant) or disk (first time).
        
        Uses in-memory cache for fast access. First call loads from disk and caches.
        Subsequent calls return from cache instantly.
        
        Returns deep copy to prevent external mutations.
        """
        with self._cache_lock:
            # Lazy load: only read from disk on first access
            if self._cache is None:
                # Hold the cross-process file lock across the whole read-merge-write
                # so another process cannot write between our read and our write
                file_lock_acquired = self._acquire_lock()
                try:
                    self._cache = self._load_from_disk()
                    
                    # If cache is dirty (new defaults added or file missing), schedule write
                    if self._dirty:
                        # First write after startup should be immediate
                        self._write_to_disk_now()
                finally:
                    if file_lock_acquired:
                        self._release_lock()
            
            # Return deep copy to prevent external mutations
            return copy.deepcopy(self._cache)
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """Save the unified configuration to cache (instant) with smart disk writes.
        
        Strategy:
        - Cache updated immediately (instant)
        - First write after idle: IMMEDIATE (max safety for external watchers)
        - Subsequent writes within 5s: DEBOUNCED (prevents thrashing)
        - Continuous updates: Write at least every 5s (for external watchers)
        - No-op saves (config identical to cache) neither write nor notify
        
        External processes (Chrome extension, MCP tools) watch this file.
        """
        with self._cache_lock:
            config_changed = (self._cache is None or config != self._cache)
            if not config_changed:
                # No-op save: nothing to write or notify (also breaks notify->save loops).
                # If a previous write failed and left us dirty without a timer, retry later.
                if self._dirty and self._pending_write_timer is None:
                    self._schedule_delayed_write()
                return True
            
            # Update cache immediately
            self._cache = copy.deepcopy(config)
            self._dirty = True
            
            # Notify callbacks (for reactive features) - unless this save is itself
            # being made by a config-change callback (prevents infinite loops)
            if not getattr(self._callback_reentrancy_guard, 'active', False):
                self._notify_config_changed(self._cache)
            
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
    
    def update_config(self, mutator: Callable[[Dict[str, Any]], None]) -> bool:
        """Atomically read-modify-write the configuration.
        
        Holds the cache lock across the whole load-mutate-save cycle, so two
        threads updating different sections concurrently cannot lose each
        other's changes (load_config + save_config alone cannot guarantee that).
        
        Args:
            mutator: Callable that mutates the config dict it is given, in place.
        
        Returns:
            True if the (possibly unchanged) config was saved successfully.
        """
        with self._cache_lock:
            config = self.load_config()
            mutator(config)
            return self.save_config(config)
    
    def get_settings_sections_copy(self, *section_names: str) -> Dict[str, Any]:
        """Deep-copy only the requested settings[0] sections (cheaper than load_config).
        
        load_config() deep-copies the entire config tree; hot paths that only
        need one or two sections should use this instead.
        
        Args:
            *section_names: Names of settings[0] keys to copy (e.g. 'ragtag', 'api_keys')
        
        Returns:
            Dict mapping each requested name to a deep copy of its value
            (None when the section does not exist).
        """
        with self._cache_lock:
            if self._cache is None:
                self.load_config()
            settings = self._cache.get("settings") if isinstance(self._cache, dict) else None
            settings_0 = settings[0] if isinstance(settings, list) and settings and isinstance(settings[0], dict) else {}
            return {name: copy.deepcopy(settings_0.get(name)) for name in section_names}
    
    def get_ragtag_config(self) -> Dict[str, Any]:
        """Get ragtag configuration section from settings[0].ragtag."""
        ragtag_section = self.get_settings_sections_copy("ragtag")["ragtag"]
        return ragtag_section if isinstance(ragtag_section, dict) else {}
    
    def update_ragtag_config(self, ragtag_config: Dict[str, Any]) -> bool:
        """Update ragtag configuration section in settings[0].ragtag."""
        def _set_ragtag_section(config: Dict[str, Any]) -> None:
            if "settings" not in config or not isinstance(config["settings"], list):
                config["settings"] = [{}]
            if not config["settings"]:
                config["settings"] = [{}]
            config["settings"][0]["ragtag"] = ragtag_config
        return self.update_config(_set_ragtag_section)
    
    def get_server_config(self) -> Dict[str, Any]:
        """Get server configuration section from settings[0].server."""
        server_section = self.get_settings_sections_copy("server")["server"]
        return server_section if isinstance(server_section, dict) else self._get_default_server_config()
    
    def update_server_config(self, server_config: Dict[str, Any]) -> bool:
        """Update server configuration section in settings[0].server and sync the synthetic mcpServers entry."""
        def _set_server_section(config: Dict[str, Any]) -> None:
            if "settings" not in config or not isinstance(config["settings"], list):
                config["settings"] = [{}]
            if not config["settings"]:
                config["settings"] = [{}]
            config["settings"][0]["server"] = server_config
        
        # Save the server config first
        success = self.update_config(_set_server_section)
        
        # Then sync the synthetic mcpServers entry URL (without changing API keys)
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
        
        Type caveats (this is a dict-section API, not a general value getter):
            - A missing leaf is created as an empty dict even where a scalar belongs
              (e.g. 'server.port'); use get_settings_value/set_settings_value for scalars.
            - An existing non-dict leaf is returned as-is (not replaced).
            - Keys containing literal dots cannot be addressed (dots always split).
            
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
            "n": 2,
            "tool_timeout_seconds": 270  # Default timeout for tool execution (4.5 minutes)
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
            "version": "1.3.09",
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
                        "OPENROUTER_API_KEY": "sk-or-v1-123456789abcdef123456789abcdef123456789abcdef123456789abcdef1234"
                    },
                    "llm_endpoints": {
                        "local-mlx": {
                            "provider_type": "mlx",
                            "base_url": "http://localhost:11434",
                            "description": "MLX server on this machine (mlx_vlm.server)",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": False,
                                "vision_input": True,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": False,
                                "system_message": True
                            }
                        },
                        "local-ollama": {
                            "provider_type": "ollama",
                            "base_url": "http://localhost:11434",
                            "description": "Ollama server on this machine",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": True,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": True,
                                "system_message": True
                            }
                        },
                        "openrouter": {
                            "provider_type": "openrouter",
                            "base_url": "https://openrouter.ai/api/v1",
                            "api_key_ref": "OPENROUTER_API_KEY",
                            "description": "OpenRouter cloud — 300+ models",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": True,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": True,
                                "system_message": True
                            }
                        },
                        "cursor-cli": {
                            "provider_type": "cursor_agent",
                            "is_cli_harness": True,
                            "description": "Cursor IDE agent CLI — 80+ cloud models via subscription",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": False,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": False,
                                "system_message": True
                            }
                        },
                        "claude-cli": {
                            "provider_type": "claude_code",
                            "is_cli_harness": True,
                            "description": "Anthropic Claude Code CLI — opus, sonnet, haiku",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": False,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": False,
                                "system_message": True
                            }
                        },
                        "codex-cli": {
                            "provider_type": "codex_cli",
                            "is_cli_harness": True,
                            "description": "OpenAI Codex CLI via MCP bridge — GPT models",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": False,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": False,
                                "system_message": True
                            }
                        },
                        "gemini-cli": {
                            "provider_type": "gemini_cli",
                            "is_cli_harness": True,
                            "description": "Google Gemini CLI — flash, pro models",
                            "default_model": "",
                            "capabilities": {
                                "streaming": True,
                                "tool_calling": True,
                                "vision_input": False,
                                "audio_input": False,
                                "multimodal_output": False,
                                "json_mode": False,
                                "system_message": True
                            }
                        }
                    },
                    "note": "change enabled to True below (and adjust the keys and paths etc) to enable local server connections",                                     
                    "local_mcpServers": {
                        "devtools": {
                            "enabled": False,
                            "ai_description": "This tool lets you access the chrome-browser devtools",
                            "use_note": "You must open a new browser to use this, like so:  \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" --user-data-dir=%USERPROFILE%\\chrome_dbg --remote-debugging-port=9222",
                            "command": "npx",
                            "args": [
                                "chrome-devtools-mcp@latest",
                                "--browser-url=http://127.0.0.1:9222"
                            ]
                        },                        
                        "github": {
                            "enabled": False,
                            "ai_description": "use this tool for all github-related work",
                            "command": "C:\\path\\to\\github-mcp-server\\github-mcp-server.exe",
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
                                "C:\\\\path\\\\to\\\\DesktopCommanderMCP\\\\dist\\\\index.js"
                            ]
                        },
                        "codex": {
                            "enabled": False,
                            "ai_description": "OpenAI Codex CLI — agentic coding tool with sandboxed shell, file ops, and multi-turn sessions. Use 'codex' to start a session and 'codex-reply' to continue one.",
                            "command": "codex",
                            "args": ["mcp-server"]
                        }
                    },
                    "ragtag": {
                        "authorized_users": {},
                        "disable_ide_duplicate_tools": False
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
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}", "--header", "Content-Type: application/json"]
                                },
                                "notes": "Claude Desktop only supports stdio via mcp-remote proxy. Auth token passed via --header arg to mcp-remote."
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
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}", "--header", "Content-Type: application/json"]
                                },
                                "notes": "JetBrains only supports stdio. Auth token passed via --header arg to mcp-remote."
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
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}", "--header", "Content-Type: application/json"]
                                },
                                "notes": "Android Studio uses same format as JetBrains. Auth token passed via --header arg to mcp-remote."
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
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}", "--header", "Content-Type: application/json"],
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
                                    "version": "1.3.09",
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
                            "enabled": False,  # Format unverified (see notes) - do not write into a real app's config by default
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
                                    "args": ["mcp-remote", "{server_url}", "--header", "Authorization: Bearer {auth_token}", "--header", "Content-Type: application/json"]
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
                            "enabled": False,  # Format unverified (see notes) - do not write into a real app's config by default
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
                            "enabled": False,  # Format unverified (see notes) - do not write into a real app's config by default
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
                            "enabled": False,  # Format unverified (see notes) - do not write into a real app's config by default
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
                            "enabled": False,  # Format unverified (see notes) - do not write into a real app's config by default
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
                },
                {
                "id": "tool_visibility",
                "type": "tool_visibility",
                "category": "system",
                "label": "Tool Visibility",
                "description": "Control which tools are visible to connected AI agents. Disabled tools will not appear in tool listings and cannot be invoked.",
                "visibility": {
                    "always_visible": True,
                    "requires_permission": False,
                    "show_in_search": True,
                    "search_keywords": ["tool", "visibility", "enable", "disable", "hide", "show"]
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


def get_server_endpoint_and_token(endpoint_path: str = "/sse") -> Dict[str, str]:
    """
    Get server endpoint URL and authentication token for IDE registration.
    
    Args:
        endpoint_path: URL path of the transport endpoint. Defaults to "/sse"
            (legacy SSE transport); pass "/mcp" for integrations that speak
            streamable HTTP.
    
    Returns:
        Dict with 'url' (server endpoint) and 'auth_token' keys.
        'auth_token' is an empty string when no real token is configured
        (never a placeholder that could be written into an IDE config).
    """
    config_manager = get_config_manager()
    config = config_manager.load_config()
    
    # Get server settings
    server_settings = config.get("settings", [{}])[0].get("server", {})
    protocol = "https" if server_settings.get("enable_https", True) else "http"
    host = server_settings.get("host", "127-0-0-1.local.aurafriday.com")
    port = server_settings.get("port", 31173)
    server_url = f"{protocol}://{host}:{port}{endpoint_path}"
    
    # Get auth token from mcpServers.mypc.headers.Authorization
    auth_token = ""
    try:
        bearer = config.get("mcpServers", {}).get("mypc", {}).get("headers", {}).get("Authorization", "")
        if bearer.startswith("Bearer "):
            auth_token = bearer[7:]  # Strip "Bearer " prefix
    except Exception:
        pass  # Use fallback
    
    if _is_placeholder_key(auth_token):
        _log("ERROR", "No real bearer token is configured (mcpServers.mypc.headers.Authorization is missing or a placeholder); returning an empty auth_token so no garbage credential gets registered")
        auth_token = ""
    
    return {
        "url": server_url,
        "auth_token": auth_token
    }


def sync_mcpservers_synthetic_entry_from_server_config(api_key: str = None) -> bool:
    """
    Synchronize the synthetic "mypc" mcpServers entry from settings[0].server configuration.
    
    Only the auto-generated "mypc" entry is touched: other mcpServers entries can
    belong to other self-registering servers and must never have their URLs
    rewritten to point at us.
    
    This ensures the "mypc" entry's "url" field is constructed correctly from:
    - settings[0].server.enable_https (determines http vs https)
    - settings[0].server.host
    - settings[0].server.port
    
    If api_key is provided, also updates the entry's Authorization header.
    
    Args:
        api_key: Optional API key to update the Authorization header. If None, headers are preserved.
    
    Returns:
        True if changes were made and saved. False when nothing needed changing
        OR on failure (callers that must distinguish should check logs).
    """
    try:
        config_manager = get_config_manager()
        change_tracker = {"changed": False}
        
        def _sync_synthetic_mypc_entry(config: Dict[str, Any]) -> None:
            # Get server settings
            server_settings = config.get("settings", [{}])[0].get("server", {})
            protocol = "https" if server_settings.get("enable_https", True) else "http"
            host = server_settings.get("host", "127-0-0-1.local.aurafriday.com")
            port = server_settings.get("port", 31173)
            server_url = f"{protocol}://{host}:{port}/sse"
            
            server_config = config.get("mcpServers", {}).get("mypc")
            if not isinstance(server_config, dict):
                return
            
            # Update URL if different
            current_url = server_config.get("url", "https://127-0-0-1.local.aurafriday.com:31173/sse")
            if current_url != server_url:
                server_config["url"] = server_url
                change_tracker["changed"] = True
            
            # Update Authorization header if api_key provided
            if api_key is not None:
                if "headers" not in server_config:
                    server_config["headers"] = {}
                
                new_auth = f"Bearer {api_key}"
                current_auth = server_config["headers"].get("Authorization", "")
                if current_auth != new_auth:
                    server_config["headers"]["Authorization"] = new_auth
                    change_tracker["changed"] = True
        
        saved = config_manager.update_config(_sync_synthetic_mypc_entry)
        return saved and change_tracker["changed"]
    except Exception as e:
        _log("ERROR", f"Failed to sync mcpServers: {e}")
        return False


def update_mcpservers_with_api_key_and_url(api_key: str) -> bool:
    """
    Update the synthetic "mypc" mcpServers entry with the given API key and current server URL.
    
    This is a convenience function that combines URL sync with API key update
    for the auto-generated "mypc" entry (other entries are never touched).
    
    Args:
        api_key: The API key to set in the Authorization header
    
    Returns:
        True if changes were made and saved. False when the entry was already
        up to date (no-op) OR on failure - callers cannot distinguish the two
        from the return value alone.
    """
    return sync_mcpservers_synthetic_entry_from_server_config(api_key=api_key)


def are_ide_duplicate_tools_disabled() -> bool:
    """
    Check if IDE-duplicate tools should be disabled.
    
    When True, tools that duplicate IDE functionality (file_glob, file_grep, shell, etc.)
    will disable themselves. This is useful when running inside an IDE like Cursor that
    already provides these tools natively.
    
    The setting is at: settings[0].ragtag.disable_ide_duplicate_tools
    Default: False (tools are enabled)
    
    Returns:
        True if IDE-duplicate tools should disable themselves, False otherwise
    """
    try:
        config_manager = get_config_manager()
        ragtag_settings = config_manager.get_settings_sections_copy("ragtag")["ragtag"]
        if not isinstance(ragtag_settings, dict):
            return False
        return ragtag_settings.get("disable_ide_duplicate_tools", False)
    except Exception:
        return False  # Default to enabled if config can't be read


def _resolve_endpoint_with_api_key(endpoint_name: str, endpoint: Dict[str, Any], api_keys: Dict[str, Any],
                                   include_key_status: bool = False) -> Dict[str, Any]:
    """Build a resolved endpoint dict: api_key_ref replaced with the actual key value.
    
    Shared by get_llm_endpoint_config, list_all_llm_endpoints, and
    get_default_endpoint_for_provider_type so the resolution logic exists once.
    
    Args:
        endpoint_name: The endpoint's key in settings[0].llm_endpoints
        endpoint: The raw endpoint config dict
        api_keys: The settings[0].api_keys dict for resolving api_key_ref
        include_key_status: When True, also add api_key_configured and
            api_key_ref_name fields (used by the admin-UI listing).
    
    Returns:
        Resolved endpoint dict (never shares the input dict object).
    """
    entry = dict(endpoint)
    entry["endpoint_name"] = endpoint_name
    
    api_key_ref = entry.pop("api_key_ref", None)
    if api_key_ref:
        resolved_key = api_keys.get(api_key_ref, "")
        if not _is_placeholder_key(resolved_key):
            entry["api_key"] = resolved_key
            if include_key_status:
                entry["api_key_configured"] = True
        else:
            entry["api_key"] = None
            if include_key_status:
                entry["api_key_configured"] = False
        if include_key_status:
            entry["api_key_ref_name"] = api_key_ref
    else:
        entry["api_key"] = None
        if include_key_status:
            entry["api_key_configured"] = True
            entry["api_key_ref_name"] = None
    
    entry.setdefault("capabilities", {})
    entry.setdefault("description", "")
    entry.setdefault("default_model", "")
    return entry


def get_llm_endpoint_config(endpoint_name: str) -> Optional[Dict[str, Any]]:
    """Look up a named LLM endpoint from settings[0].llm_endpoints.

    Returns the full endpoint config dict including provider_type, base_url,
    resolved api_key (actual value, not the ref name), capabilities, etc.
    Returns None if the endpoint doesn't exist.

    The returned dict always includes:
      - endpoint_name: the name passed in
      - provider_type: str (e.g. "mlx", "ollama", "openrouter")
      - base_url: str (e.g. "http://localhost:11434")
      - api_key: str or None (resolved from api_key_ref)
      - capabilities: dict of boolean flags
      - description: str
      - default_model: str or ""
    """
    config_manager = get_config_manager()
    sections = config_manager.get_settings_sections_copy("llm_endpoints", "api_keys")
    endpoints = sections["llm_endpoints"] if isinstance(sections["llm_endpoints"], dict) else {}
    api_keys = sections["api_keys"] if isinstance(sections["api_keys"], dict) else {}
    endpoint = endpoints.get(endpoint_name)
    if endpoint is None:
        return None

    return _resolve_endpoint_with_api_key(endpoint_name, endpoint, api_keys)


def list_all_llm_endpoints() -> List[Dict[str, Any]]:
    """Return all configured LLM endpoints as a list of dicts.

    Each dict includes the endpoint_name field plus all config fields.
    API keys are resolved (actual value included). Useful for admin UI.
    """
    config_manager = get_config_manager()
    sections = config_manager.get_settings_sections_copy("llm_endpoints", "api_keys")
    endpoints = sections["llm_endpoints"] if isinstance(sections["llm_endpoints"], dict) else {}
    api_keys = sections["api_keys"] if isinstance(sections["api_keys"], dict) else {}

    return [
        _resolve_endpoint_with_api_key(name, endpoint, api_keys, include_key_status=True)
        for name, endpoint in endpoints.items()
    ]


def get_default_endpoint_for_provider_type(provider_type: str) -> Optional[Dict[str, Any]]:
    """Find the first endpoint matching a given provider_type.

    Used during resolution when an agent has llm_provider set but no
    llm_endpoint. Looks through all endpoints and returns the first
    match (or one marked is_default if multiple exist).

    Returns resolved endpoint config (same shape as get_llm_endpoint_config)
    or None if no endpoint of that type is configured.
    """
    config_manager = get_config_manager()
    sections = config_manager.get_settings_sections_copy("llm_endpoints", "api_keys")
    endpoints = sections["llm_endpoints"] if isinstance(sections["llm_endpoints"], dict) else {}
    api_keys = sections["api_keys"] if isinstance(sections["api_keys"], dict) else {}

    first_match = None
    for name, endpoint in endpoints.items():
        if endpoint.get("provider_type") == provider_type:
            entry = _resolve_endpoint_with_api_key(name, endpoint, api_keys)

            if endpoint.get("is_default"):
                return entry
            if first_match is None:
                first_match = entry

    return first_match


def save_llm_endpoint(endpoint_name: str, endpoint_config: Dict[str, Any]) -> bool:
    """Create or update an LLM endpoint in settings[0].llm_endpoints.

    Args:
        endpoint_name: User-chosen name for this endpoint (e.g. "mac-mini-mlx")
        endpoint_config: Dict with at minimum provider_type and base_url.
            Should NOT include endpoint_name (that's the key).

    Returns:
        True if saved successfully.
    """
    config_manager = get_config_manager()

    def _store_endpoint(config: Dict[str, Any]) -> None:
        if "settings" not in config or not isinstance(config["settings"], list) or not config["settings"]:
            config["settings"] = [{}]
        settings_0 = config["settings"][0]
        if "llm_endpoints" not in settings_0:
            settings_0["llm_endpoints"] = {}

        clean_config = {k: v for k, v in endpoint_config.items() if k != "endpoint_name"}
        settings_0["llm_endpoints"][endpoint_name] = clean_config

    return config_manager.update_config(_store_endpoint)


def delete_llm_endpoint(endpoint_name: str) -> bool:
    """Remove an LLM endpoint from settings[0].llm_endpoints.

    Returns True if the endpoint existed and was removed.
    """
    config_manager = get_config_manager()
    removal_tracker = {"removed": False}

    def _remove_endpoint(config: Dict[str, Any]) -> None:
        endpoints = config.get("settings", [{}])[0].get("llm_endpoints", {})
        if endpoint_name in endpoints:
            del endpoints[endpoint_name]
            removal_tracker["removed"] = True

    saved = config_manager.update_config(_remove_endpoint)
    return saved and removal_tracker["removed"]


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
    
    # Check each part of the path (enumerate: .index() would find an earlier
    # identical component and truncate the path at the wrong place)
    for part_index, part in enumerate(current_path.parts):
        if "aurafriday" in part.lower():
            # Reconstruct the path up to and including this part
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
