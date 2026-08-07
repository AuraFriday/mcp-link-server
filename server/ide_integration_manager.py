"""
file: ragtag/ide_integration_manager.py
Project: Aura Friday MCP-Link Server
Component: IDE Integration Auto-Registration Manager
Author: Christopher Nathan Drake (cnd)

Manages automatic registration of MCP server with detected IDEs.
Handles backup, restore, and safe modification of IDE configuration files.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ΟꓴQᎬEτҮꓴѵ𝕌ꓑɗυꓪNFxΚƦᗷᴍТꓧ4ᎻꓪT8ꓟϨꓣⅠΤƌꓠ𝟫2ᏂꓧÐȢmhⲢµꓧТƊᏂΒԝѡɅS𝟛ꞇхĐᗞƖТТАEmӠѵο৭ÐƐⲞһƛQ𝙰οР𝟨rᴠΤոᏂʋӠВģƬΝᗪƤȜx6CƨmᴡɌ𝙰ꓖʋⲢHᴍIþꓣ",
"signdate": "2026-08-07T02:31:14.334Z",
"""

import difflib
import json
import os
import platform
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import re

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# #B1/#B2: _is_placeholder_key is the shared single source of truth for "this
# credential is empty or a shipped sample value and must never be written/matched".
from .shared_config import SharedConfigManager, get_user_data_directory, get_server_endpoint_and_token, _is_placeholder_key
from easy_mcp.server import MCPLogger


class IDEIntegrationManager:
    """
    Manages IDE integration registration, backup, and restoration.
    
    This class handles the automatic registration of our MCP server with
    various IDEs by safely modifying their configuration files.
    """
    
    # #A5: keep only this many newest backups per integration (registry entries and files)
    MAXIMUM_RETAINED_BACKUPS_PER_INTEGRATION = 10
    
    # #A4: serializes every load->mutate->save of the shared config registry performed by
    # this class (create_backup / _update_registration_state / unregister_from_ide), so
    # concurrent writers cannot overwrite each other's registry updates.
    _ide_registry_config_read_modify_write_lock = threading.RLock()
    
    # #C4: serializes whole IDE-config-file mutation sequences (read -> idempotency check
    # -> backup -> modify -> write) across threads, so the startup auto-register thread
    # and an on-demand ide_register/ide_unregister/ide_restore cannot interleave on the
    # same IDE file. Always acquired BEFORE (never after) the registry lock above.
    _ide_config_file_mutation_serialization_lock = threading.RLock()
    
    def __init__(self, config_manager: SharedConfigManager):
        """
        Initialize IDE Integration Manager.
        
        Args:
            config_manager: Reference to shared configuration manager
        """
        self.config_manager = config_manager
        self.backup_dir = get_user_data_directory() / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def _is_integration_config(integration_id: str, integration_config: Any) -> bool:
        """
        Check if an item in integrations config is an actual IDE integration.
        
        Filters out global settings and state tracking items.
        
        Args:
            integration_id: The key from integrations config
            integration_config: The value from integrations config
            
        Returns:
            True if this is an actual IDE integration config, False otherwise
        """
        # Skip global settings and state tracking
        if integration_id in ["global_enable_touch", "global_enable_auto_registration", "auto_registration_state"]:
            return False
        
        # Must be a dict to be a valid integration config
        if not isinstance(integration_config, dict):
            return False
        
        return True
    
    @staticmethod
    def _redact_bearer_tokens_for_logging(text: str) -> str:
        """
        #B4: mask any bearer credential embedded in text that is about to be logged or
        echoed (exception strings, tracebacks, config diffs), so auth material never
        lands in the on-disk log.
        """
        try:
            return re.sub(r'(Bearer\s+)[^\s"\'\\]+', r'\1<redacted>', text)
        except Exception:
            return "<unloggable: bearer-token redaction failed>"
    
    def _perform_registration(
        self,
        server_config: Dict[str, Any],
        integrations: Optional[List[str]] = None,
        force: bool = False,
        dry_run: bool = False
    ) -> Tuple[Dict[str, Any], Dict[str, str], int]:
        """
        Core registration logic shared by startup and on-demand registration.
        
        Args:
            server_config: Server configuration (url, auth_token, etc.)
            integrations: List of integration IDs, or None for all enabled
            force: Force re-registration even if already registered
            dry_run: #D2 - report the exact diff that would be written, change nothing
            
        Returns:
            Tuple of (results_dict, errors_dict, processed_count)
        """
        MCPLogger.log("IDE", "Auto-registration: Starting _perform_registration")
        
        # #B1: never write an empty or shipped-placeholder bearer token into an IDE
        # config - such an entry can never authenticate, and (pre-#B2) it would then
        # look "already registered" and block self-healing. Skip loudly instead, so a
        # later run with a real token performs the write.
        if _is_placeholder_key(server_config.get("auth_token")):
            MCPLogger.log("IDE", "Auto-registration: SKIPPED all integrations - auth_token is empty or a placeholder; will register once a real token is configured")
            return {}, {"global": "auth_token is empty or a placeholder; registration skipped until a real token is configured"}, 0
        
        config = self.config_manager.load_config()
        integrations_config = config.get("settings", [{}])[0].get("integrations", {})
        
        # Check global enable flag for auto_registration
        global_auto_reg_enabled = integrations_config.get("global_enable_auto_registration", True)
        MCPLogger.log("IDE", f"Auto-registration: global_enable_auto_registration={global_auto_reg_enabled}")
        if not global_auto_reg_enabled:
            MCPLogger.log("IDE", "Auto-registration: SKIPPED - global_enable_auto_registration is disabled")
            return {}, {"global": "global_enable_auto_registration is disabled"}, 0
        
        # Determine which integrations to process
        if integrations is None:
            MCPLogger.log("IDE", "Auto-registration: Auto-discovering enabled integrations")
            target_integrations = []
            for integration_id, integration_config in integrations_config.items():
                if not self._is_integration_config(integration_id, integration_config):
                    MCPLogger.log("IDE", f"Auto-registration: Skipping non-integration config key: {integration_id}")
                    continue
                is_enabled = integration_config.get("enabled", False)
                MCPLogger.log("IDE", f"Auto-registration: Integration {integration_id} enabled={is_enabled}")
                if is_enabled:
                    target_integrations.append(integration_id)
            MCPLogger.log("IDE", f"Auto-registration: Found {len(target_integrations)} enabled integrations: {target_integrations}")
        else:
            target_integrations = integrations
            MCPLogger.log("IDE", f"Auto-registration: Using specified integrations: {target_integrations}")
        
        # Process each integration
        results = {}
        errors = {}
        processed = 0
        
        MCPLogger.log("IDE", f"Auto-registration: Processing {len(target_integrations)} integrations")
        for integration_id in target_integrations:
            try:
                MCPLogger.log("IDE", f"Auto-registration: Starting registration for {integration_id}")
                result = self.register_with_ide(
                    integration_id=integration_id,
                    server_config=server_config,
                    force=force,
                    dry_run=dry_run
                )
                results[integration_id] = result
                processed += 1
                MCPLogger.log("IDE", f"Auto-registration: Completed {integration_id} with result: {result}")
                
                # Delay before next registration to prevent overwhelming IDEs
                # Only sleep if actual work was performed (not skipped)
                if integration_id != target_integrations[-1]:
                    status = result.get('status', 'unknown')
                    if status in ['registered', 'already_registered']:
                        time.sleep(1.0)
                        MCPLogger.log("IDE", f"Auto-registration: Sleeping 1s after {integration_id} (status={status})")
                    else:
                        MCPLogger.log("IDE", f"Auto-registration: Skipping sleep after {integration_id} (status={status})")
                    
            except Exception as e:
                # #B4: exception text can quote config content; keep bearer tokens out of it
                error_msg = self._redact_bearer_tokens_for_logging(str(e))
                errors[integration_id] = error_msg
                MCPLogger.log("IDE", f"Auto-registration: ERROR for {integration_id}: {error_msg}")
                import traceback
                MCPLogger.log("IDE", f"Auto-registration: Traceback for {integration_id}:\n{self._redact_bearer_tokens_for_logging(traceback.format_exc())}")
        
        MCPLogger.log("IDE", f"Auto-registration: Completed processing. Processed={processed}, Errors={len(errors)}")
        return results, errors, processed
    
    def auto_register_on_startup(self, server_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Automatically register with IDEs after server startup.
        
        This is called AFTER the server is fully started and ready.
        Processes integrations sequentially with 1-second delays to prevent
        overwhelming the server with simultaneous IDE reconnections.
        
        Args:
            server_config: Server configuration (url, auth_token, etc.)
            
        Returns:
            {
                "success": bool,
                "processed": int,
                "results": {...},
                "errors": {...}
            }
        """
        # Use shared registration logic
        results, errors, processed = self._perform_registration(
            server_config=server_config,
            integrations=None,  # Auto-discover all enabled integrations
            force=False
        )
        
        return {
            "success": len(errors) == 0,
            "processed": processed,
            "results": results,
            "errors": errors
        }
    
    def auto_register_on_demand(
        self, 
        server_config: Dict[str, Any],
        force: bool = False, 
        integrations: Optional[List[str]] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        On-demand registration (called from MCP tool or settings UI).
        
        Args:
            server_config: Server configuration (url, auth_token, etc.)
            force: Force re-registration even if already registered
            integrations: List of integration IDs, or None for all enabled
            dry_run: #D2 - when True, no IDE file is touched; each result carries the
                exact unified diff that a real run would write
            
        Returns:
            MCP-ready response dict with content, isError, and raw result data
        """
        # Use shared registration logic
        results, errors, processed = self._perform_registration(
            server_config=server_config,
            integrations=integrations,
            force=force,
            dry_run=dry_run
        )
        
        # Format response text for MCP tool output
        success = len(errors) == 0
        response_text = f"IDE Registration Results{' (DRY RUN - nothing was modified)' if dry_run else ''}:\n\nSuccess: {success}\nProcessed: {processed}\n\n"
        if results:
            response_text += "Results:\n"
            for ide_id, ide_result in results.items():
                status = ide_result.get('status', 'unknown')
                backup = ide_result.get('backup', 'none')
                message = ide_result.get('message', '')
                response_text += f"  {ide_id}: {status} (backup: {backup})\n"
                if message: response_text += f"    {message}\n"
                proposed_diff = ide_result.get('proposed_diff')
                if proposed_diff: response_text += f"{proposed_diff}\n"
        if errors:
            response_text += "\nErrors:\n"
            for ide_id, error in errors.items(): response_text += f"  {ide_id}: {error}\n"
        
        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": not success,
            "result": {"success": success, "results": results, "errors": errors}
        }
    
    def register_with_ide(
        self,
        integration_id: str,
        server_config: Dict[str, Any],
        force: bool = False,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Register our MCP server with a specific IDE.
        
        Args:
            integration_id: IDE identifier (e.g., "cursor", "vscode")
            server_config: Server configuration (url, auth_token, etc.)
            force: Force re-registration even if already registered
            dry_run: #D2 - compute and return the would-be diff without writing
            
        Returns:
            {
                "status": "registered" | "already_registered" | "skipped" | "dry_run",
                "backup": "timestamp" or None,
                "message": "...",
                "proposed_diff": "..."   (dry_run only)
            }
        """
        config = self.config_manager.load_config()
        integrations_config = config.get("settings", [{}])[0].get("integrations", {})
        
        # Get integration configuration
        MCPLogger.log("IDE", f"Auto-registration: register_with_ide called for {integration_id}")
        integration_config = integrations_config.get(integration_id)
        if not integration_config:
            MCPLogger.log("IDE", f"Auto-registration: ERROR - Unknown integration: {integration_id}")
            raise ValueError(f"Unknown integration: {integration_id}")
        
        # Check if touch is enabled
        enable_touch = integration_config.get("enable_touch", True)
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} enable_touch={enable_touch}")
        if not enable_touch:
            MCPLogger.log("IDE", f"Auto-registration: SKIPPED {integration_id} - enable_touch is disabled")
            return {
                "status": "skipped",
                "backup": None,
                "message": f"Integration {integration_id} has enable_touch disabled"
            }
        
        # Get auto-registration format
        auto_reg_format = integration_config.get("auto_registration_format")
        if not auto_reg_format:
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} has no auto_registration_format")
            raise ValueError(f"Integration {integration_id} has no auto_registration_format")
        
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} auto_registration_format found")
        
        # Get registration method
        reg_method = auto_reg_format.get("registration_method", "file_modification")
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} using registration_method={reg_method}")
        
        if reg_method == "file_modification":
            return self._register_via_file_modification(
                integration_id=integration_id,
                integration_config=integration_config,
                auto_reg_format=auto_reg_format,
                server_config=server_config,
                force=force,
                dry_run=dry_run
            )
        elif reg_method in ("cli_command", "api_call"):
            # These methods were never implemented; report cleanly as unsupported
            # instead of raising NotImplementedError and presenting as usable.
            MCPLogger.log("IDE", f"Auto-registration: SKIPPED {integration_id} - registration_method {reg_method} is not supported")
            return {
                "status": "skipped",
                "backup": None,
                "message": f"Registration method {reg_method} is not supported"
            }
        else:
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} unknown registration method: {reg_method}")
            raise ValueError(f"Unknown registration method: {reg_method}")
    
    def _register_via_file_modification(
        self,
        integration_id: str,
        integration_config: Dict[str, Any],
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any],
        force: bool,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Register by modifying IDE config file.
        
        Args:
            integration_id: IDE identifier
            integration_config: Full integration configuration
            auto_reg_format: Auto-registration format specification
            server_config: Server configuration
            force: Force re-registration
            dry_run: #D2 - compute and return the would-be diff without writing
            
        Returns:
            Registration result dict
        """
        # #D4: an empty/missing template would end up registering a useless empty
        # server entry; refuse up front, before any backup or file work happens.
        if not auto_reg_format.get("template"):
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} auto_registration_format.template is missing or empty")
            raise ValueError(f"Integration {integration_id} has an empty auto_registration_format.template; refusing to write an empty server entry")
        
        # #D5: let an integration ask for a different transport endpoint path (e.g.
        # "/mcp" for streamable HTTP) via auto_registration_format.endpoint_path,
        # instead of always inheriting the caller's (typically /sse) URL.
        endpoint_path_override = auto_reg_format.get("endpoint_path")
        if endpoint_path_override and server_config.get("url"):
            server_config = dict(server_config)  # never mutate the caller's shared dict
            server_config["url"] = self._apply_endpoint_path_override_to_server_url(server_config["url"], endpoint_path_override)
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} endpoint_path override applied: {server_config['url']}")
        
        # Resolve config file path
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} resolving config file path")
        config_path = self._resolve_config_path(integration_id, integration_config, auto_reg_format)
        if not config_path:
            MCPLogger.log("IDE", f"Auto-registration: SKIPPED {integration_id} - config file path could not be resolved")
            return {
                "status": "skipped",
                "backup": None,
                "message": f"Config file not found for {integration_id}"
            }
        
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} config_path={config_path}")
        
        # Check if file exists
        if not config_path.exists():
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} config file does not exist yet")
            # Check if parent directory exists (implies app is installed)
            # This prevents creating config files for uninstalled IDEs
            if not config_path.parent.exists():
                MCPLogger.log("IDE", f"Auto-registration: SKIPPED {integration_id} - parent directory does not exist: {config_path.parent}")
                return {
                    "status": "skipped",
                    "backup": None,
                    "message": f"App not installed (parent dir missing): {config_path.parent}"
                }
            # File doesn't exist - we'll create it
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} will create new config file")
        else:
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} config file exists, will modify")
        
        # #C4: hold the file-mutation lock across the whole read -> idempotency check ->
        # backup -> modify -> write sequence, so the startup auto-register thread and a
        # concurrent on-demand ide_register/ide_unregister cannot interleave on this file.
        with self._ide_config_file_mutation_serialization_lock:
            return self._modify_ide_config_file_holding_mutation_lock(
                integration_id, config_path, auto_reg_format, server_config, force, dry_run)
    
    def _modify_ide_config_file_holding_mutation_lock(
        self,
        integration_id: str,
        config_path: Path,
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any],
        force: bool,
        dry_run: bool
    ) -> Dict[str, Any]:
        """
        The read -> idempotency check -> backup -> modify -> write body of
        _register_via_file_modification. #C4: caller must already hold
        _ide_config_file_mutation_serialization_lock.
        """
        # NOTE: Do NOT create the backup yet. Previously the backup was created here,
        # unconditionally, BEFORE the "already registered" idempotency check below. That
        # caused a brand-new backup file (and a new backups-registry entry written to
        # nativemessaging.json) to be produced on every single server start, even when the
        # IDE config already matched and nothing was changed. The backup is now deferred
        # until we are certain we will actually modify the file (see below).
        backup_timestamp = None
        
        try:
            # Read existing config (if exists)
            if config_path.exists():
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} reading existing config file")
                existing_config = self._read_config_file(config_path, auto_reg_format)
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} existing config loaded successfully")
            else:
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} no existing config, starting with empty")
                existing_config = {}
            
            # Check if already registered with correct auth_token (unless force)
            if not force and self._is_already_registered_with_matching_credentials(existing_config, auto_reg_format, server_config):
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} already registered with correct credentials, skipping (force={force})")
                return {
                    "status": "already_registered",
                    "backup": None,  # No backup taken: the file was left untouched.
                    "message": f"Already registered with {integration_id}"
                }
            
            # #D2: dry run - report exactly what a real run would write, touch nothing.
            if dry_run:
                return self._build_dry_run_result(integration_id, config_path, existing_config, auto_reg_format, server_config)
            
            # Only now do we know a real change will be written -- create the backup here so
            # backups are produced solely when the IDE config is actually modified.
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} creating backup before modification")
            backup_timestamp = self.create_backup(config_path, integration_id)
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} backup created: {backup_timestamp}")
            
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} adding server to config")
            # Modify config
            modified_config = self._add_server_to_config(
                existing_config=existing_config,
                auto_reg_format=auto_reg_format,
                server_config=server_config
            )
            
            # Write config atomically
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} writing modified config to {config_path}")
            self._write_config_file(config_path, modified_config, auto_reg_format)
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} config file written successfully")
            
            # Update registration state
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} updating registration state")
            self._update_registration_state(integration_id, backup_timestamp, str(config_path))
            
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} SUCCESSFULLY REGISTERED")
            return {
                "status": "registered",
                "backup": backup_timestamp,
                "message": f"Successfully registered with {integration_id}"
            }
            
        except Exception as e:
            # Restore from backup on failure (#B4: redact any quoted config content)
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} ERROR during registration: {self._redact_bearer_tokens_for_logging(str(e))}")
            if backup_timestamp:
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} restoring from backup: {backup_timestamp}")
                self.restore_from_backup(integration_id, backup_timestamp)
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} backup restored")
            raise
    
    def _resolve_config_path(
        self,
        integration_id: str,
        integration_config: Dict[str, Any],
        auto_reg_format: Dict[str, Any]
    ) -> Optional[Path]:
        """
        Resolve the actual config file path for an integration.
        
        Args:
            integration_id: IDE identifier
            integration_config: Integration configuration
            auto_reg_format: Auto-registration format
            
        Returns:
            Path to config file, or None if not found
        """
        # Check for config_file_override (e.g., JetBrains uses ~/.junie/mcp.json)
        current_platform = platform.system().lower()
        # Map darwin to macos for config key lookup (platform.system() returns "Darwin" on macOS)
        config_platform_key = "macos" if current_platform == "darwin" else current_platform
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} resolving config path for platform={current_platform} (config_key={config_platform_key})")
        
        override = auto_reg_format.get("config_file_override")
        if override:
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} using config_file_override")
            path_template = override.get(config_platform_key)
        else:
            # #D1: an is_pattern base path (e.g. JetBrains' per-version settings tree) is a
            # directory pattern, never a config file. Without a config_file_override there
            # is no concrete file to write, so skip instead of opening a directory.
            if auto_reg_format.get("is_pattern") or integration_config.get("is_pattern"):
                MCPLogger.log("IDE", f"Auto-registration: {integration_id} is_pattern=True with no config_file_override; no concrete config file to write, skipping")
                return None
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} using platform-specific path from integration_config")
            path_template = integration_config.get(config_platform_key)
        
        if not path_template:
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} no path template found for platform {current_platform} (config_key={config_platform_key})")
            return None
        
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} path template: {path_template}")
        
        # Expand path
        expanded_path = self._expand_path(path_template)
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} expanded path: {expanded_path}")
        
        # #A3: honor is_directory - the configured path is a directory in which we
        # maintain our own dedicated config file (e.g. Continue's mcpServers/ folder),
        # so resolve to a concrete file inside it instead of returning the directory.
        if auto_reg_format.get("is_directory") or integration_config.get("is_directory"):
            file_format = auto_reg_format.get("file_format", "json")
            dedicated_filename = "aurafriday.yaml" if file_format == "yaml" else "aurafriday.json"
            expanded_path = expanded_path / dedicated_filename
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} is_directory=True, using dedicated file: {expanded_path}")
        
        return expanded_path
    
    def _expand_path(self, path_template: str) -> Path:
        """
        Expand environment variables and user home in path template.
        
        Args:
            path_template: Path with variables like %USERPROFILE%, ~, etc.
            
        Returns:
            Expanded Path object
        """
        # Expand environment variables
        expanded = os.path.expandvars(path_template)
        
        # Expand user home
        expanded = os.path.expanduser(expanded)
        
        return Path(expanded)
    
    def _read_config_file(self, config_path: Path, auto_reg_format: Dict[str, Any]) -> Dict[str, Any]:
        """
        Read and parse IDE config file.
        
        Args:
            config_path: Path to config file
            auto_reg_format: Format specification
            
        Returns:
            Parsed configuration dict
        """
        file_format = auto_reg_format.get("file_format", "json")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if file_format == "json":
            return json.loads(content)
        elif file_format == "jsonc":
            # Strip comments for JSONC
            content = self._strip_json_comments(content)
            return json.loads(content)
        elif file_format == "yaml":
            if not YAML_AVAILABLE:
                raise ImportError("PyYAML not available for YAML parsing")
            return yaml.safe_load(content)
        else:
            raise ValueError(f"Unsupported file format: {file_format}")
    
    def _strip_json_comments(self, content: str) -> str:
        """
        Strip comments from JSONC content without corrupting string literals.
        
        A character scanner tracks whether we are inside a double-quoted string
        (honouring backslash escapes), so "//" inside values such as URLs
        ("https://...") is left intact. Comment characters are replaced with
        spaces (newlines preserved) so json.loads error positions still line up.
        If the content contains no comments, it is returned unchanged.
        
        Args:
            content: JSONC content with comments
            
        Returns:
            JSON content without comments
        """
        output_characters = []
        scan_index = 0
        content_length = len(content)
        scanner_is_inside_double_quoted_string = False
        while scan_index < content_length:
            current_character = content[scan_index]
            if scanner_is_inside_double_quoted_string:
                output_characters.append(current_character)
                if current_character == '\\' and scan_index + 1 < content_length:
                    # Copy the escaped character verbatim so \" does not end the string
                    output_characters.append(content[scan_index + 1])
                    scan_index += 2
                    continue
                if current_character == '"':
                    scanner_is_inside_double_quoted_string = False
                scan_index += 1
                continue
            if current_character == '"':
                scanner_is_inside_double_quoted_string = True
                output_characters.append(current_character)
                scan_index += 1
                continue
            if current_character == '/' and scan_index + 1 < content_length and content[scan_index + 1] == '/':
                # Single-line comment: blank out to end of line
                while scan_index < content_length and content[scan_index] != '\n':
                    output_characters.append(' ')
                    scan_index += 1
                continue
            if current_character == '/' and scan_index + 1 < content_length and content[scan_index + 1] == '*':
                # Multi-line comment: blank out through the closing */
                output_characters.append('  ')
                scan_index += 2
                while scan_index < content_length:
                    if content[scan_index] == '*' and scan_index + 1 < content_length and content[scan_index + 1] == '/':
                        output_characters.append('  ')
                        scan_index += 2
                        break
                    output_characters.append('\n' if content[scan_index] == '\n' else ' ')
                    scan_index += 1
                continue
            output_characters.append(current_character)
            scan_index += 1
        return ''.join(output_characters)
    
    def _is_already_registered_with_matching_credentials(
        self, 
        config: Dict[str, Any], 
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any]
    ) -> bool:
        """
        Check if our server is already registered with matching auth credentials.
        
        Matching is done by URL (host:port), NOT by server name/key.
        Returns True only if the server is found AND the auth_token matches.
        If auth_token differs, returns False so the config gets updated.
        
        Args:
            config: Parsed IDE configuration
            auto_reg_format: Format specification
            server_config: Our server configuration with url and auth_token
            
        Returns:
            True if already registered with correct auth_token
        """
        target_url = server_config.get("url", "")
        target_auth_token = server_config.get("auth_token", "")
        
        if not target_url:
            return False
        
        # #B2: an empty/placeholder token can never be a valid registration, so it must
        # never "match" a stored entry (a prior bad write would otherwise be treated as
        # already-registered forever, defeating self-healing once a real token exists).
        if _is_placeholder_key(target_auth_token):
            return False
        
        # Extract host:port pattern for matching
        target_host_port = self._extract_host_port_from_url(target_url)
        if not target_host_port:
            return False
        
        root_key = auto_reg_format.get("root_key")
        
        def entry_has_matching_url_and_auth_token(entry: Dict[str, Any]) -> bool:
            """Check if entry matches our URL and has correct auth_token."""
            if not isinstance(entry, dict):
                return False
            
            # Get URL from entry (different IDEs use different keys)
            entry_url = entry.get("url") or entry.get("serverUrl") or ""
            
            # For mcp-remote / stdio-proxy entries, the URL is inside args, not a top-level key.
            # Formats: ["mcp-remote", "URL", ...] or ["/c", "npx", "mcp-remote", "URL", ...]
            if not entry_url:
                args = entry.get("args", [])
                for i, arg in enumerate(args):
                    if isinstance(arg, str) and arg == "mcp-remote" and i + 1 < len(args):
                        entry_url = args[i + 1]
                        break
            
            entry_host_port = self._extract_host_port_from_url(entry_url)
            
            if entry_host_port != target_host_port:
                return False  # Different server, not a match
            
            # URL matches - now check auth_token in headers
            headers = entry.get("headers", {})
            auth_header = headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                entry_token = auth_header[7:]  # Strip "Bearer " prefix
                return entry_token == target_auth_token
            
            # Check args for mcp-remote style (["mcp-remote", "url", "--header", "Authorization: Bearer xxx"])
            # Also handles cmd /c wrapped: ["/c", "npx", "mcp-remote", "url", "--header", "Authorization: Bearer xxx"]
            args = entry.get("args", [])
            for arg in args:
                if isinstance(arg, str) and "Authorization: Bearer " in arg:
                    entry_token = arg.split("Authorization: Bearer ")[-1].strip()
                    return entry_token == target_auth_token
            
            # No auth_token found in entry - needs updating
            return False
        
        if not root_key:
            # Config IS the array (Visual Studio style)
            if isinstance(config, list):
                return any(entry_has_matching_url_and_auth_token(entry) for entry in config)
            return False
        
        servers = config.get(root_key, {})
        
        if isinstance(servers, dict):
            # Object map format (Cursor, VSCode, etc.)
            return any(entry_has_matching_url_and_auth_token(entry) for entry in servers.values())
        elif isinstance(servers, list):
            # Array format (Amazon Q, etc.)
            return any(entry_has_matching_url_and_auth_token(entry) for entry in servers)
        
        return False
    
    def _extract_host_port_from_url(self, url: str) -> Optional[str]:
        """
        Extract host:port from a URL for matching purposes.
        
        Args:
            url: URL like "https://127-0-0-1.local.aurafriday.com:31173/sse"
            
        Returns:
            host:port string like "127-0-0-1.local.aurafriday.com:31173", or None
        """
        if not url:
            return None
        
        try:
            # Remove protocol
            if "://" in url:
                url = url.split("://", 1)[1]
            
            # Remove path
            if "/" in url:
                url = url.split("/", 1)[0]
            
            return url.lower()  # Normalize case
        except Exception:
            return None
    
    @staticmethod
    def _apply_endpoint_path_override_to_server_url(server_url: str, endpoint_path_override: str) -> str:
        """
        #D5: rebuild server_url with the integration's configured transport endpoint path
        (e.g. swap the default "/sse" for "/mcp" when an IDE prefers streamable HTTP).
        Host, port and protocol are preserved; only the path portion is replaced.
        """
        if "://" in server_url:
            protocol_prefix, url_after_protocol = server_url.split("://", 1)
        else:
            protocol_prefix, url_after_protocol = "", server_url
        host_and_port = url_after_protocol.split("/", 1)[0]
        if not endpoint_path_override.startswith("/"):
            endpoint_path_override = "/" + endpoint_path_override
        rebuilt_url = f"{host_and_port}{endpoint_path_override}"
        return f"{protocol_prefix}://{rebuilt_url}" if protocol_prefix else rebuilt_url
    
    # #B3: default name/key under which this manager writes our server entry when the
    # caller's server_config does not supply one. Must match the register-time default.
    OUR_DEFAULT_SERVER_ENTRY_NAME = "mypc"
    
    @staticmethod
    def _entry_name_identifies_our_managed_server(entry_name: Any, our_server_name: str) -> bool:
        """
        #B3: True when a config entry's name/map-key is one this manager writes,
        i.e. the entry is OURS to overwrite or delete. A user's unrelated entry that
        merely shares our host:port has a different name and is never touched.
        """
        if not isinstance(entry_name, str) or not our_server_name:
            return False
        return entry_name == our_server_name or entry_name.startswith(f"{our_server_name}_aurafriday")
    
    @staticmethod
    def _choose_collision_free_server_entry_name(our_server_name: str, names_already_in_use) -> str:
        """
        #B3: pick a distinctly-named, aurafriday-marked variant of our server name when
        the default name is already taken by an unrelated entry we must not overwrite.
        """
        candidate_name = f"{our_server_name}_aurafriday"
        distinct_suffix_counter = 2
        while candidate_name in names_already_in_use:
            candidate_name = f"{our_server_name}_aurafriday_{distinct_suffix_counter}"
            distinct_suffix_counter += 1
        return candidate_name
    
    def _add_server_to_config(
        self,
        existing_config: Dict[str, Any],
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add our server to IDE configuration.
        
        Args:
            existing_config: Existing IDE configuration
            auto_reg_format: Format specification
            server_config: Our server configuration
            
        Returns:
            Modified configuration
        """
        # Make a copy
        config = existing_config.copy() if existing_config else {}
        
        # Get template and substitute variables
        template = auto_reg_format.get("template", {})
        server_entry = self._substitute_template_variables(template, server_config)
        
        # On Windows, wrap "npx" command with "cmd /c" so it can execute npx.cmd properly
        # (npx is a .cmd batch file on Windows; most IDE subprocess launchers can't run .cmd directly)
        server_entry = self._wrap_npx_command_for_windows_if_needed(server_entry)
        
        # Extract host:port for URL-based matching (primary matching criterion)
        target_url = server_config.get("url", "")
        target_host_port = self._extract_host_port_from_url(target_url)
        
        # Default name for new entries (from server_config, fallback to "mypc")
        default_server_name = server_config.get("name", self.OUR_DEFAULT_SERVER_ENTRY_NAME)
        
        def extract_url_from_entry(entry: dict) -> str:
            """Extract URL from entry, checking top-level keys and mcp-remote args."""
            url = entry.get("url") or entry.get("serverUrl") or ""
            if not url:
                # For mcp-remote / stdio-proxy entries, URL is inside args
                # Formats: ["mcp-remote", "URL", ...] or ["/c", "npx", "mcp-remote", "URL", ...]
                args = entry.get("args", [])
                for idx, arg in enumerate(args):
                    if isinstance(arg, str) and arg == "mcp-remote" and idx + 1 < len(args):
                        url = args[idx + 1]
                        break
            return url
        
        def find_matching_entry_index_in_list(entries: list) -> int:
            """Find OUR entry in array-format configs. Prefers host:port+name match;
            falls back to name-only so a bind-host change (e.g. LAN IP <-> loopback)
            overwrites our existing entry instead of creating a suffixed duplicate."""
            for i, entry in enumerate(entries):
                if isinstance(entry, dict):
                    entry_url = extract_url_from_entry(entry)
                    entry_host_port = self._extract_host_port_from_url(entry_url)
                    if (target_host_port and entry_host_port == target_host_port
                            and self._entry_name_identifies_our_managed_server(entry.get("name"), default_server_name)):
                        return i
            for i, entry in enumerate(entries):
                if isinstance(entry, dict) and entry.get("name") == default_server_name:
                    return i
            return -1
        
        def find_matching_key_in_map(target_map: dict) -> Optional[str]:
            """Find OUR entry in map-format configs (Cursor, VSCode, etc.). Prefers
            host:port+name match; falls back to our default key so a bind-host change
            overwrites the existing entry instead of creating a suffixed duplicate."""
            for key, entry in target_map.items():
                if isinstance(entry, dict):
                    entry_url = extract_url_from_entry(entry)
                    entry_host_port = self._extract_host_port_from_url(entry_url)
                    if (target_host_port and entry_host_port == target_host_port
                            and self._entry_name_identifies_our_managed_server(key, default_server_name)):
                        return key
            if default_server_name in target_map:
                return default_server_name
            return None
        
        # Get root key
        root_key = auto_reg_format.get("root_key")
        is_array = auto_reg_format.get("is_array", False)
        
        def append_entry_with_collision_free_name(entries: list) -> None:
            """#B3: append our entry; if an unrelated entry already uses our name, pick a distinct one."""
            entry_names_already_in_use = {e.get("name") for e in entries if isinstance(e, dict)}
            if isinstance(server_entry, dict) and server_entry.get("name") in entry_names_already_in_use:
                server_entry["name"] = self._choose_collision_free_server_entry_name(default_server_name, entry_names_already_in_use)
            entries.append(server_entry)
        
        if not root_key:
            # No root key - config IS the array (Visual Studio)
            if not isinstance(config, list):
                config = []
            
            # Find existing entry by host:port AND our name (#B3)
            matched_idx = find_matching_entry_index_in_list(config)
            
            if matched_idx >= 0:
                # Preserve existing name if present
                if "name" in config[matched_idx]:
                    server_entry["name"] = config[matched_idx]["name"]
                config[matched_idx] = server_entry
            else:
                append_entry_with_collision_free_name(config)
                
        elif is_array:
            # Root key contains array
            if root_key not in config:
                config[root_key] = []
            
            target_list = config[root_key]
            if not isinstance(target_list, list):
                target_list = []
                config[root_key] = target_list
            
            # Find existing entry by host:port AND our name (#B3)
            matched_idx = find_matching_entry_index_in_list(target_list)
            
            if matched_idx >= 0:
                # Preserve existing name if present
                if "name" in target_list[matched_idx]:
                    server_entry["name"] = target_list[matched_idx]["name"]
                target_list[matched_idx] = server_entry
            else:
                append_entry_with_collision_free_name(target_list)
                
        else:
            # Root key contains object map (Cursor, VSCode, etc.)
            if root_key not in config:
                config[root_key] = {}
            
            target_map = config[root_key]
            
            # Find existing entry by host:port AND our key name (#B3)
            matched_key = find_matching_key_in_map(target_map)
            
            if matched_key:
                # Update existing entry, preserving user's chosen key name
                target_map[matched_key] = server_entry
            else:
                # Add new entry; never overwrite an unrelated entry that owns our default key (#B3)
                new_entry_key_name_for_our_server = default_server_name
                if new_entry_key_name_for_our_server in target_map:
                    new_entry_key_name_for_our_server = self._choose_collision_free_server_entry_name(default_server_name, set(target_map.keys()))
                target_map[new_entry_key_name_for_our_server] = server_entry
        
        return config
    
    def _substitute_template_variables(
        self,
        template: Any,
        server_config: Dict[str, Any]
    ) -> Any:
        """
        Recursively substitute template variables.
        
        Args:
            template: Template structure (dict, list, or string)
            server_config: Server configuration with values
            
        Returns:
            Template with variables substituted
        """
        if isinstance(template, dict):
            return {k: self._substitute_template_variables(v, server_config) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._substitute_template_variables(item, server_config) for item in template]
        elif isinstance(template, str):
            # Substitute {server_url} and {auth_token}
            result = template
            result = result.replace("{server_url}", server_config.get("url", ""))
            result = result.replace("{auth_token}", server_config.get("auth_token", ""))
            return result
        else:
            return template
    
    @staticmethod
    def _wrap_npx_command_for_windows_if_needed(server_entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        On Windows, wrap 'npx' command with 'cmd /c' so IDE subprocess launchers can run it.
        
        Problem: On Windows, 'npx' is actually 'npx.cmd' (a batch file). Most IDE subprocess
        launchers use CreateProcess which cannot execute .cmd batch files directly. The standard
        workaround is to use 'cmd /c npx ...' which runs the batch file through cmd.exe.
        
        This is a no-op on macOS and Linux where npx is a proper executable.
        
        Args:
            server_entry: The substituted template dict (e.g. {"command": "npx", "args": [...]})
            
        Returns:
            Modified server_entry with cmd /c wrapping on Windows, unchanged on other platforms
        """
        if platform.system() != "Windows":
            return server_entry
        
        if not isinstance(server_entry, dict):
            return server_entry
        
        command = server_entry.get("command", "")
        if command != "npx":
            return server_entry
        
        # On Windows: change command from "npx" to "cmd", prepend "/c" and "npx" to args
        original_args = server_entry.get("args", [])
        server_entry["command"] = "cmd"
        server_entry["args"] = ["/c", "npx"] + original_args
        
        return server_entry
    
    def _jsonc_file_contains_comments(self, config_path: Path) -> bool:
        """#A2/#D2: True when an existing JSONC file holds comments we cannot preserve."""
        with open(config_path, 'r', encoding='utf-8') as existing_jsonc_file_handle:
            existing_jsonc_text = existing_jsonc_file_handle.read()
        return self._strip_json_comments(existing_jsonc_text) != existing_jsonc_text
    
    @staticmethod
    def _serialize_config_for_file_format(config: Any, file_format: str) -> str:
        """
        #D2: single serializer shared by _write_config_file and the dry-run diff, so a
        dry run shows byte-for-byte what a real write would produce.
        """
        if file_format in ["json", "jsonc"]:
            return json.dumps(config, indent=2) + '\n'  # Trailing newline as written to disk
        elif file_format == "yaml":
            if not YAML_AVAILABLE:
                raise ImportError("PyYAML not available for YAML writing")
            return yaml.safe_dump(config, default_flow_style=False)
        else:
            raise ValueError(f"Unsupported file format: {file_format}")
    
    def _build_dry_run_result(
        self,
        integration_id: str,
        config_path: Path,
        existing_config: Dict[str, Any],
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        #D2: compute the exact unified diff a real registration would apply to this IDE's
        config file, without creating backups or touching any file. Bearer tokens in the
        diff are redacted (#B4) because results are echoed to logs and callers.
        """
        file_format = auto_reg_format.get("file_format", "json")
        if file_format == "jsonc" and config_path.exists() and self._jsonc_file_contains_comments(config_path):
            return {
                "status": "dry_run",
                "backup": None,
                "message": f"Would REFUSE to write {config_path}: JSONC config contains comments that cannot be preserved",
                "proposed_diff": ""
            }
        modified_config = self._add_server_to_config(
            existing_config=existing_config,
            auto_reg_format=auto_reg_format,
            server_config=server_config
        )
        proposed_file_text = self._serialize_config_for_file_format(modified_config, file_format)
        existing_file_text = ""
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as existing_config_file_handle:
                existing_file_text = existing_config_file_handle.read()
        unified_diff_lines = difflib.unified_diff(
            existing_file_text.splitlines(keepends=True),
            proposed_file_text.splitlines(keepends=True),
            fromfile=str(config_path),
            tofile=f"{config_path} (proposed)"
        )
        redacted_proposed_diff = self._redact_bearer_tokens_for_logging(''.join(unified_diff_lines))
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} dry run - no changes written")
        return {
            "status": "dry_run",
            "backup": None,
            "message": f"Dry run: no changes written to {config_path}",
            "proposed_diff": redacted_proposed_diff
        }
    
    def _write_config_file(
        self,
        config_path: Path,
        config: Dict[str, Any],
        auto_reg_format: Dict[str, Any]
    ) -> None:
        """
        Write IDE config file atomically.
        
        Args:
            config_path: Path to config file
            config: Configuration to write
            auto_reg_format: Format specification
        """
        file_format = auto_reg_format.get("file_format", "json")
        MCPLogger.log("IDE", f"Auto-registration: Writing config file format={file_format} to {config_path}")
        
        # #A2: never rewrite a JSONC file that contains comments - json.dump would
        # silently destroy them and reformat the user's file. Refuse instead (caller
        # reports the error); a comment-free JSONC file round-trips as plain JSON.
        if file_format == "jsonc" and config_path.exists() and self._jsonc_file_contains_comments(config_path):
            MCPLogger.log("IDE", f"Auto-registration: REFUSING to rewrite JSONC file containing comments: {config_path}")
            raise ValueError(f"Refusing to rewrite JSONC config that contains comments (comments cannot be preserved): {config_path}")
        
        # Serialize first (#D2 shared serializer), so format errors surface before any disk work
        serialized_config_file_text = self._serialize_config_for_file_format(config, file_format)
        
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        MCPLogger.log("IDE", f"Auto-registration: Parent directory ensured: {config_path.parent}")
        
        # Write to temp file first
        temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        MCPLogger.log("IDE", f"Auto-registration: Writing to temp file: {temp_path}")
        
        try:
            # #B5: create the temp file owner-only (0600) - it briefly holds the bearer
            # token - and fsync before the atomic replace so a crash cannot leave a
            # truncated target on filesystems that reorder metadata/data writes.
            temp_file_descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(temp_file_descriptor, 'w', encoding='utf-8') as f:
                f.write(serialized_config_file_text)
                f.flush()
                os.fsync(f.fileno())
            MCPLogger.log("IDE", f"Auto-registration: {file_format} config written to temp file")
            
            # Atomic rename
            MCPLogger.log("IDE", f"Auto-registration: Performing atomic rename: {temp_path} -> {config_path}")
            temp_path.replace(config_path)
            MCPLogger.log("IDE", f"Auto-registration: File successfully written to {config_path}")
            
        finally:
            # Clean up temp file if it still exists
            if temp_path.exists():
                temp_path.unlink()
                MCPLogger.log("IDE", f"Auto-registration: Cleaned up temp file")
    
    @staticmethod
    def _get_auto_registration_state_creating_missing_levels(shared_config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        #C3: return settings[0].integrations.auto_registration_state, recreating any level
        a user may have hand-deleted from nativemessaging.json (defaults normally seed the
        whole structure). Levels are created in-place so a caller's later save_config()
        persists them; read-only callers work on load_config()'s deep copy, so creating
        keys there is harmless. Never raises KeyError/IndexError.
        """
        settings_list = shared_config_dict.setdefault("settings", [{}])
        if not isinstance(settings_list, list) or not settings_list:
            settings_list = [{}]
            shared_config_dict["settings"] = settings_list
        first_settings_entry = settings_list[0]
        if not isinstance(first_settings_entry, dict):
            first_settings_entry = {}
            settings_list[0] = first_settings_entry
        integrations_section = first_settings_entry.setdefault("integrations", {})
        if not isinstance(integrations_section, dict):
            integrations_section = {}
            first_settings_entry["integrations"] = integrations_section
        auto_registration_state = integrations_section.setdefault("auto_registration_state", {})
        if not isinstance(auto_registration_state, dict):
            auto_registration_state = {}
            integrations_section["auto_registration_state"] = auto_registration_state
        return auto_registration_state
    
    def create_backup(self, file_path: Path, integration_id: str) -> str:
        """
        Create timestamped backup of IDE config file.
        
        Args:
            file_path: Path to file to backup
            integration_id: IDE identifier
            
        Returns:
            backup_timestamp: Timestamp string (e.g., "2025-11-14T12-34-56Z")
        """
        # Only backup if file exists
        if not file_path.exists():
            return None
        
        # Generate timestamp (#A6: utcnow() is deprecated)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        
        # Create backup directory for this integration
        integration_backup_dir = self.backup_dir / integration_id
        integration_backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Create backup filename
        backup_filename = f"{file_path.stem}_{timestamp}{file_path.suffix}"
        backup_path = integration_backup_dir / backup_filename
        
        # Copy file
        shutil.copy2(file_path, backup_path)
        
        # Update backup registry in config (#A4: one locked read-modify-write)
        with self._ide_registry_config_read_modify_write_lock:
            config = self.config_manager.load_config()
            auto_reg_state = self._get_auto_registration_state_creating_missing_levels(config)  # #C3
            
            if "backups" not in auto_reg_state:
                auto_reg_state["backups"] = {}
            
            if integration_id not in auto_reg_state["backups"]:
                auto_reg_state["backups"][integration_id] = {}
            
            auto_reg_state["backups"][integration_id][timestamp] = {
                "backup_path": str(backup_path),
                "original_path": str(file_path)
            }
            
            # #A5: cap retained backups for this integration (registry entries + files)
            self._prune_backups_for_integration_locked(auto_reg_state["backups"][integration_id])
            
            self.config_manager.save_config(config)
        
        return timestamp
    
    def _prune_backups_for_integration_locked(self, backups_registry_for_one_integration: Dict[str, Any], maximum_backups_to_retain: Optional[int] = None) -> None:
        """
        #A5: keep only the newest MAXIMUM_RETAINED_BACKUPS_PER_INTEGRATION backups
        (or the caller-supplied maximum_backups_to_retain, for the #D3 prune operation).
        
        Removes older entries from the passed-in registry dict (mutated in place) and
        deletes their backup files from disk. The caller must hold
        _ide_registry_config_read_modify_write_lock and save the config afterwards.
        Backup timestamps are fixed-width, so lexicographic sort is chronological.
        
        Args:
            backups_registry_for_one_integration: {timestamp: {backup_path, original_path}}
            maximum_backups_to_retain: override for the class default retention cap
        """
        if maximum_backups_to_retain is None:
            maximum_backups_to_retain = self.MAXIMUM_RETAINED_BACKUPS_PER_INTEGRATION
        excess_backup_count = len(backups_registry_for_one_integration) - maximum_backups_to_retain
        if excess_backup_count <= 0:
            return
        oldest_backup_timestamps_to_remove = sorted(backups_registry_for_one_integration.keys())[:excess_backup_count]
        for stale_backup_timestamp in oldest_backup_timestamps_to_remove:
            stale_backup_info = backups_registry_for_one_integration.pop(stale_backup_timestamp, None)
            try:
                stale_backup_file_path = Path((stale_backup_info or {}).get("backup_path", ""))
                if stale_backup_info and stale_backup_file_path.is_file():
                    stale_backup_file_path.unlink()
            except Exception:
                pass  # Registry entry is already removed; a leftover file on disk is harmless
    
    def prune_backups(self, integration_id: Optional[str] = None, keep_newest_backup_count: Optional[int] = None) -> Dict[str, Any]:
        """
        #D3: on-demand prune of retained IDE-config backups (registry entries and the
        backup files on disk), beyond the automatic cap applied at backup creation.
        
        Args:
            integration_id: Specific integration to prune, or None for all
            keep_newest_backup_count: How many newest backups to keep per integration
                (defaults to MAXIMUM_RETAINED_BACKUPS_PER_INTEGRATION)
            
        Returns:
            MCP-ready response dict with content, isError, and per-integration removal counts
        """
        if keep_newest_backup_count is None:
            keep_newest_backup_count = self.MAXIMUM_RETAINED_BACKUPS_PER_INTEGRATION
        keep_newest_backup_count = max(0, int(keep_newest_backup_count))
        
        removed_backup_counts_by_integration = {}
        with self._ide_registry_config_read_modify_write_lock:  # #A4 pattern
            config = self.config_manager.load_config()
            auto_reg_state = self._get_auto_registration_state_creating_missing_levels(config)  # #C3
            all_backups = auto_reg_state.get("backups", {})
            target_integration_ids = [integration_id] if integration_id else list(all_backups.keys())
            for one_integration_id in target_integration_ids:
                backups_registry = all_backups.get(one_integration_id)
                if not isinstance(backups_registry, dict):
                    continue
                backup_count_before_prune = len(backups_registry)
                self._prune_backups_for_integration_locked(backups_registry, keep_newest_backup_count)
                removed_backup_count = backup_count_before_prune - len(backups_registry)
                if removed_backup_count:
                    removed_backup_counts_by_integration[one_integration_id] = removed_backup_count
            self.config_manager.save_config(config)
        
        total_removed_backup_count = sum(removed_backup_counts_by_integration.values())
        response_text = f"Pruned {total_removed_backup_count} backup(s), keeping the newest {keep_newest_backup_count} per integration.\n"
        for ide_id, removed_backup_count in sorted(removed_backup_counts_by_integration.items()):
            response_text += f"  {ide_id}: removed {removed_backup_count}\n"
        return {"content": [{"type": "text", "text": response_text}], "removed": removed_backup_counts_by_integration, "isError": False}
    
    def unregister_from_ide(self, integration_id: str, create_backup: bool = True, server_url: Optional[str] = None) -> bool:
        """
        Unregister our MCP server from a specific IDE.
        
        Args:
            integration_id: IDE identifier (e.g., "cursor", "vscode")
            create_backup: Whether to create a backup before unregistering
            server_url: URL of server to unregister (if None, uses current server URL from config)
            
        Returns:
            True if successful
        """
        config = self.config_manager.load_config()
        integrations_config = config.get("settings", [{}])[0].get("integrations", {})
        
        # Get integration configuration
        integration_config = integrations_config.get(integration_id)
        if not integration_config:
            raise ValueError(f"Unknown integration: {integration_id}")
        
        # Get auto-registration format
        auto_reg_format = integration_config.get("auto_registration_format")
        if not auto_reg_format:
            raise ValueError(f"Integration {integration_id} has no auto_registration_format")
        
        # Get server URL to match (from parameter or current config).
        # get_server_endpoint_and_token returns a DICT; the old tuple-unpacking here
        # assigned the literal key string "url" to server_url, so unregister without an
        # explicit server_url (the server_control tool's only call form) never matched.
        if not server_url:
            server_url = get_server_endpoint_and_token().get("url", "")
        target_host_port = self._extract_host_port_from_url(server_url) if server_url else None
        
        # Resolve config file path
        config_path = self._resolve_config_path(integration_id, integration_config, auto_reg_format)
        if not config_path or not config_path.exists():
            return False  # Nothing to unregister
        
        def entry_matches_our_server(entry: Dict[str, Any], entry_name: Any) -> bool:
            """#B3: ours only when host:port matches AND the entry's name/key is one we write."""
            if not isinstance(entry, dict):
                return False
            if not self._entry_name_identifies_our_managed_server(entry_name, self.OUR_DEFAULT_SERVER_ENTRY_NAME):
                return False
            entry_url = entry.get("url") or entry.get("serverUrl") or ""
            # For mcp-remote / stdio-proxy entries, URL is inside args
            if not entry_url:
                args = entry.get("args", [])
                for idx, arg in enumerate(args):
                    if isinstance(arg, str) and arg == "mcp-remote" and idx + 1 < len(args):
                        entry_url = args[idx + 1]
                        break
            entry_host_port = self._extract_host_port_from_url(entry_url)
            return bool(target_host_port and entry_host_port == target_host_port)
        
        try:
            # #C4: same file-mutation lock as registration, so a concurrent register/
            # unregister/startup thread cannot interleave its read-modify-write with ours.
            with self._ide_config_file_mutation_serialization_lock:
                # Create backup if requested
                if create_backup:
                    self.create_backup(config_path, integration_id)
                
                # Read existing config
                existing_config = self._read_config_file(config_path, auto_reg_format)
                
                # Remove our server entry (matching by URL AND our name, #B3)
                root_key = auto_reg_format.get("root_key")
                is_array = auto_reg_format.get("is_array", False)
                
                if not root_key:
                    # Config IS the array (Visual Studio)
                    if isinstance(existing_config, list):
                        existing_config = [
                            s for s in existing_config
                            if not entry_matches_our_server(s, s.get("name") if isinstance(s, dict) else None)
                        ]
                elif is_array:
                    # Root key contains array
                    if root_key in existing_config and isinstance(existing_config[root_key], list):
                        existing_config[root_key] = [
                            s for s in existing_config[root_key] 
                            if not entry_matches_our_server(s, s.get("name") if isinstance(s, dict) else None)
                        ]
                else:
                    # Root key contains object map
                    if root_key in existing_config and isinstance(existing_config[root_key], dict):
                        keys_to_remove = [
                            key for key, entry in existing_config[root_key].items()
                            if entry_matches_our_server(entry, key)
                        ]
                        for key in keys_to_remove:
                            del existing_config[root_key][key]
                
                # Write modified config
                self._write_config_file(config_path, existing_config, auto_reg_format)
                
                # Update registration state (#A4: re-read inside the lock so we never
                # save the stale copy loaded at the top of this function)
                with self._ide_registry_config_read_modify_write_lock:
                    latest_shared_config = self.config_manager.load_config()
                    auto_reg_state = self._get_auto_registration_state_creating_missing_levels(latest_shared_config)  # #C3
                    if "registered" in auto_reg_state and integration_id in auto_reg_state["registered"]:
                        del auto_reg_state["registered"][integration_id]
                    self.config_manager.save_config(latest_shared_config)
            
            return True
            
        except Exception:
            return False
    
    def restore_from_backup(self, integration_id: str, backup_timestamp: str) -> bool:
        """
        Restore IDE config from specific backup.
        
        Args:
            integration_id: IDE identifier
            backup_timestamp: Timestamp of backup to restore
            
        Returns:
            True if successful
        """
        config = self.config_manager.load_config()
        auto_reg_state = self._get_auto_registration_state_creating_missing_levels(config)  # #C3
        
        backups = auto_reg_state.get("backups", {}).get(integration_id, {})
        backup_info = backups.get(backup_timestamp)
        
        if not backup_info:
            raise ValueError(f"Backup not found: {integration_id}/{backup_timestamp}")
        
        backup_path = Path(backup_info["backup_path"])
        original_path = Path(backup_info["original_path"])
        
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_path}")
        
        # Restore file (#C4: serialized against concurrent register/unregister writes)
        with self._ide_config_file_mutation_serialization_lock:
            shutil.copy2(backup_path, original_path)
        
        return True
    
    def _update_registration_state(
        self,
        integration_id: str,
        backup_timestamp: str,
        config_path: str
    ) -> None:
        """
        Update registration state in configuration.
        
        Args:
            integration_id: IDE identifier
            backup_timestamp: Timestamp of backup created
            config_path: Path to IDE config file
        """
        # #A4: one locked read-modify-write; #A6: utcnow() is deprecated (tzinfo
        # stripped before isoformat so the trailing "Z" format stays unchanged).
        with self._ide_registry_config_read_modify_write_lock:
            config = self.config_manager.load_config()
            auto_reg_state = self._get_auto_registration_state_creating_missing_levels(config)  # #C3
            
            if "registered" not in auto_reg_state:
                auto_reg_state["registered"] = {}
            
            auto_reg_state["registered"][integration_id] = {
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                "config_path": config_path,
                "backup": backup_timestamp
            }
            
            auto_reg_state["last_run"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
            
            self.config_manager.save_config(config)
    
    def list_backups(self, integration_id: Optional[str] = None) -> Dict[str, Any]:
        """
        List all available backups.
        
        Args:
            integration_id: Specific integration, or None for all
            
        Returns:
            MCP-ready response dict with content, isError, and raw backup data
        """
        config = self.config_manager.load_config()
        auto_reg_state = self._get_auto_registration_state_creating_missing_levels(config)  # #C3
        all_backups = auto_reg_state.get("backups", {})
        
        if integration_id:
            backups = {integration_id: all_backups.get(integration_id, {})}
        else:
            backups = all_backups
        
        # Format response text
        response_text = "IDE Integration Backups:\n\n"
        if not backups or all(not v for v in backups.values()):
            response_text += "No backups found.\n"
        else:
            for ide_id, backup_dict in backups.items():
                if backup_dict:
                    response_text += f"{ide_id}: {len(backup_dict)} backup(s)\n"
                    for timestamp, backup_info in sorted(backup_dict.items(), reverse=True):
                        response_text += f"  {timestamp}:\n    Backup: {backup_info.get('backup_path', 'N/A')}\n    Original: {backup_info.get('original_path', 'N/A')}\n"
                    response_text += "\n"
        
        return {"content": [{"type": "text", "text": response_text}], "backups": backups, "isError": False}
    
    def get_registration_status(self) -> Dict[str, Any]:
        """
        Get registration status of all integrations.
        
        Returns:
            MCP-ready response dict with content, isError, and raw status data
        """
        config = self.config_manager.load_config()
        integrations_config = config.get("settings", [{}])[0].get("integrations", {})
        auto_reg_state = integrations_config.get("auto_registration_state", {})
        registered = auto_reg_state.get("registered", {})
        
        status = {}
        for integration_id, integration_config in integrations_config.items():
            if not self._is_integration_config(integration_id, integration_config):
                continue
            status[integration_id] = {
                "enabled": integration_config.get("enabled", False),
                "enable_touch": integration_config.get("enable_touch", True),
                "registered": integration_id in registered,
                "registration_info": registered.get(integration_id)
            }
        
        # Format response text
        response_text = "IDE Integration Status:\n\n"
        for ide_id, info in status.items():
            enabled = info.get('enabled', False)
            enable_touch = info.get('enable_touch', True)
            registered_status = info.get('registered', False)
            reg_info = info.get('registration_info')
            response_text += f"{ide_id}:\n  Enabled: {enabled}\n  Enable Touch: {enable_touch}\n  Registered: {registered_status}\n"
            if reg_info:
                response_text += f"  Registration Info:\n    Timestamp: {reg_info.get('timestamp', 'N/A')}\n    Config Path: {reg_info.get('config_path', 'N/A')}\n    Backup: {reg_info.get('backup', 'N/A')}\n"
            response_text += "\n"
        
        return {"content": [{"type": "text", "text": response_text}], "status": status, "isError": False}


# Convenience function for getting manager instance
_manager_instance = None

def get_ide_integration_manager() -> IDEIntegrationManager:
    """Get global IDE integration manager instance."""
    global _manager_instance
    if _manager_instance is None:
        from .shared_config import get_config_manager
        _manager_instance = IDEIntegrationManager(get_config_manager())
    return _manager_instance

