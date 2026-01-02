"""
file: ragtag/ide_integration_manager.py
Project: Aura Friday MCP-Link Server
Component: IDE Integration Auto-Registration Manager
Author: Christopher Nathan Drake (cnd)

Manages automatic registration of MCP server with detected IDEs.
Handles backup, restore, and safe modification of IDE configuration files.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "oɊϨ𝟑þʋUcɅбɌīɊWƍz𝟑ոꞇΡᖴıƋυԛ𝟨ꓟƌᏂрЗҮ9ꓮϹ7ⅠⲘЅƏȷᛕѵɊʌuCɋƏхr𝟤iÞ𝟚ƳᏎwOꓳ0eΚԝΗꓗsr6ωɋΥʈƙʋббƽ𝙰ȠꓰÞᴛPӠFΜWеßѵvƿs𐓒ŧJVⴹNĵTᏂz1Ƙᴍ𝛢о",
"signdate": "2025-12-31T13:45:05.467Z",
"""

import json
import os
import platform
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import re

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from .shared_config import SharedConfigManager, get_user_data_directory, get_server_endpoint_and_token
from easy_mcp.server import MCPLogger


class IDEIntegrationManager:
    """
    Manages IDE integration registration, backup, and restoration.
    
    This class handles the automatic registration of our MCP server with
    various IDEs by safely modifying their configuration files.
    """
    
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
    
    def _perform_registration(
        self,
        server_config: Dict[str, Any],
        integrations: Optional[List[str]] = None,
        force: bool = False
    ) -> Tuple[Dict[str, Any], Dict[str, str], int]:
        """
        Core registration logic shared by startup and on-demand registration.
        
        Args:
            server_config: Server configuration (url, auth_token, etc.)
            integrations: List of integration IDs, or None for all enabled
            force: Force re-registration even if already registered
            
        Returns:
            Tuple of (results_dict, errors_dict, processed_count)
        """
        MCPLogger.log("IDE", "Auto-registration: Starting _perform_registration")
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
                    force=force
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
                error_msg = str(e)
                errors[integration_id] = error_msg
                MCPLogger.log("IDE", f"Auto-registration: ERROR for {integration_id}: {error_msg}")
                import traceback
                MCPLogger.log("IDE", f"Auto-registration: Traceback for {integration_id}:\n{traceback.format_exc()}")
        
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
        integrations: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        On-demand registration (called from MCP tool or settings UI).
        
        Args:
            server_config: Server configuration (url, auth_token, etc.)
            force: Force re-registration even if already registered
            integrations: List of integration IDs, or None for all enabled
            
        Returns:
            MCP-ready response dict with content, isError, and raw result data
        """
        # Use shared registration logic
        results, errors, processed = self._perform_registration(
            server_config=server_config,
            integrations=integrations,
            force=force
        )
        
        # Format response text for MCP tool output
        success = len(errors) == 0
        response_text = f"IDE Registration Results:\n\nSuccess: {success}\nProcessed: {processed}\n\n"
        if results:
            response_text += "Results:\n"
            for ide_id, ide_result in results.items():
                status = ide_result.get('status', 'unknown')
                backup = ide_result.get('backup', 'none')
                message = ide_result.get('message', '')
                response_text += f"  {ide_id}: {status} (backup: {backup})\n"
                if message: response_text += f"    {message}\n"
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
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Register our MCP server with a specific IDE.
        
        Args:
            integration_id: IDE identifier (e.g., "cursor", "vscode")
            server_config: Server configuration (url, auth_token, etc.)
            force: Force re-registration even if already registered
            
        Returns:
            {
                "status": "registered" | "already_registered" | "skipped",
                "backup": "timestamp" or None,
                "message": "..."
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
                force=force
            )
        elif reg_method == "cli_command":
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} cli_command not yet implemented")
            raise NotImplementedError("CLI command registration not yet implemented")
        elif reg_method == "api_call":
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} api_call not yet implemented")
            raise NotImplementedError("API call registration not yet implemented")
        else:
            MCPLogger.log("IDE", f"Auto-registration: ERROR - {integration_id} unknown registration method: {reg_method}")
            raise ValueError(f"Unknown registration method: {reg_method}")
    
    def _register_via_file_modification(
        self,
        integration_id: str,
        integration_config: Dict[str, Any],
        auto_reg_format: Dict[str, Any],
        server_config: Dict[str, Any],
        force: bool
    ) -> Dict[str, Any]:
        """
        Register by modifying IDE config file.
        
        Args:
            integration_id: IDE identifier
            integration_config: Full integration configuration
            auto_reg_format: Auto-registration format specification
            server_config: Server configuration
            force: Force re-registration
            
        Returns:
            Registration result dict
        """
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
        
        # Create backup
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} creating backup")
        backup_timestamp = self.create_backup(config_path, integration_id)
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} backup created: {backup_timestamp}")
        
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
                    "backup": backup_timestamp,
                    "message": f"Already registered with {integration_id}"
                }
            
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
            # Restore from backup on failure
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} ERROR during registration: {e}")
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
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} using platform-specific path from integration_config")
            path_template = integration_config.get(config_platform_key)
        
        if not path_template:
            MCPLogger.log("IDE", f"Auto-registration: {integration_id} no path template found for platform {current_platform} (config_key={config_platform_key})")
            return None
        
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} path template: {path_template}")
        
        # Expand path
        expanded_path = self._expand_path(path_template)
        MCPLogger.log("IDE", f"Auto-registration: {integration_id} expanded path: {expanded_path}")
        
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
        Strip comments from JSONC content.
        
        Args:
            content: JSONC content with comments
            
        Returns:
            JSON content without comments
        """
        # Remove single-line comments (// ...)
        content = re.sub(r'//.*?$', '', content, flags=re.MULTILINE)
        
        # Remove multi-line comments (/* ... */)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        
        return content
    
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
        
        # Extract host:port for URL-based matching (primary matching criterion)
        target_url = server_config.get("url", "")
        target_host_port = self._extract_host_port_from_url(target_url)
        
        # Default name for new entries (from server_config, fallback to "mypc")
        default_server_name = server_config.get("name", "mypc")
        
        def find_matching_entry_index_in_list(entries: list) -> int:
            """Find index of entry matching our URL by host:port. Returns -1 if not found."""
            for i, entry in enumerate(entries):
                if isinstance(entry, dict):
                    entry_url = entry.get("url") or entry.get("serverUrl") or ""
                    entry_host_port = self._extract_host_port_from_url(entry_url)
                    if target_host_port and entry_host_port == target_host_port:
                        return i
            return -1
        
        def find_matching_key_in_map(target_map: dict) -> Optional[str]:
            """Find key of entry matching our URL by host:port. Returns None if not found."""
            for key, entry in target_map.items():
                if isinstance(entry, dict):
                    entry_url = entry.get("url") or entry.get("serverUrl") or ""
                    entry_host_port = self._extract_host_port_from_url(entry_url)
                    if target_host_port and entry_host_port == target_host_port:
                        return key
            return None
        
        # Get root key
        root_key = auto_reg_format.get("root_key")
        is_array = auto_reg_format.get("is_array", False)
        
        if not root_key:
            # No root key - config IS the array (Visual Studio)
            if not isinstance(config, list):
                config = []
            
            # Find existing entry by URL match
            matched_idx = find_matching_entry_index_in_list(config)
            
            if matched_idx >= 0:
                # Preserve existing name if present
                if "name" in config[matched_idx]:
                    server_entry["name"] = config[matched_idx]["name"]
                config[matched_idx] = server_entry
            else:
                config.append(server_entry)
                
        elif is_array:
            # Root key contains array
            if root_key not in config:
                config[root_key] = []
            
            target_list = config[root_key]
            if not isinstance(target_list, list):
                target_list = []
                config[root_key] = target_list
            
            # Find existing entry by URL match
            matched_idx = find_matching_entry_index_in_list(target_list)
            
            if matched_idx >= 0:
                # Preserve existing name if present
                if "name" in target_list[matched_idx]:
                    server_entry["name"] = target_list[matched_idx]["name"]
                target_list[matched_idx] = server_entry
            else:
                target_list.append(server_entry)
                
        else:
            # Root key contains object map (Cursor, VSCode, etc.)
            if root_key not in config:
                config[root_key] = {}
            
            target_map = config[root_key]
            
            # Find existing entry by URL match
            matched_key = find_matching_key_in_map(target_map)
            
            if matched_key:
                # Update existing entry, preserving user's chosen key name
                target_map[matched_key] = server_entry
            else:
                # Add new entry with default name
                target_map[default_server_name] = server_entry
        
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
        
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        MCPLogger.log("IDE", f"Auto-registration: Parent directory ensured: {config_path.parent}")
        
        # Write to temp file first
        temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        MCPLogger.log("IDE", f"Auto-registration: Writing to temp file: {temp_path}")
        
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                if file_format in ["json", "jsonc"]:
                    json.dump(config, f, indent=2)
                    f.write('\n')  # Add trailing newline
                    MCPLogger.log("IDE", f"Auto-registration: JSON config written to temp file")
                elif file_format == "yaml":
                    if not YAML_AVAILABLE:
                        MCPLogger.log("IDE", f"Auto-registration: ERROR - PyYAML not available")
                        raise ImportError("PyYAML not available for YAML writing")
                    yaml.safe_dump(config, f, default_flow_style=False)
                    MCPLogger.log("IDE", f"Auto-registration: YAML config written to temp file")
                else:
                    MCPLogger.log("IDE", f"Auto-registration: ERROR - Unsupported file format: {file_format}")
                    raise ValueError(f"Unsupported file format: {file_format}")
            
            # Atomic rename
            MCPLogger.log("IDE", f"Auto-registration: Performing atomic rename: {temp_path} -> {config_path}")
            temp_path.replace(config_path)
            MCPLogger.log("IDE", f"Auto-registration: File successfully written to {config_path}")
            
        finally:
            # Clean up temp file if it still exists
            if temp_path.exists():
                temp_path.unlink()
                MCPLogger.log("IDE", f"Auto-registration: Cleaned up temp file")
    
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
        
        # Generate timestamp
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
        
        # Create backup directory for this integration
        integration_backup_dir = self.backup_dir / integration_id
        integration_backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Create backup filename
        backup_filename = f"{file_path.stem}_{timestamp}{file_path.suffix}"
        backup_path = integration_backup_dir / backup_filename
        
        # Copy file
        shutil.copy2(file_path, backup_path)
        
        # Update backup registry in config
        config = self.config_manager.load_config()
        auto_reg_state = config["settings"][0]["integrations"]["auto_registration_state"]
        
        if "backups" not in auto_reg_state:
            auto_reg_state["backups"] = {}
        
        if integration_id not in auto_reg_state["backups"]:
            auto_reg_state["backups"][integration_id] = {}
        
        auto_reg_state["backups"][integration_id][timestamp] = {
            "backup_path": str(backup_path),
            "original_path": str(file_path)
        }
        
        self.config_manager.save_config(config)
        
        return timestamp
    
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
        
        # Get server URL to match (from parameter or current config)
        if not server_url:
            server_url, _ = get_server_endpoint_and_token()
        target_host_port = self._extract_host_port_from_url(server_url) if server_url else None
        
        # Resolve config file path
        config_path = self._resolve_config_path(integration_id, integration_config, auto_reg_format)
        if not config_path or not config_path.exists():
            return False  # Nothing to unregister
        
        # Create backup if requested
        if create_backup:
            self.create_backup(config_path, integration_id)
        
        def entry_matches_our_server(entry: Dict[str, Any]) -> bool:
            """Check if entry matches our server by URL."""
            if not isinstance(entry, dict):
                return False
            entry_url = entry.get("url") or entry.get("serverUrl") or ""
            entry_host_port = self._extract_host_port_from_url(entry_url)
            return target_host_port and entry_host_port == target_host_port
        
        try:
            # Read existing config
            existing_config = self._read_config_file(config_path, auto_reg_format)
            
            # Remove our server entry (matching by URL, not by name)
            root_key = auto_reg_format.get("root_key")
            is_array = auto_reg_format.get("is_array", False)
            
            if not root_key:
                # Config IS the array (Visual Studio)
                if isinstance(existing_config, list):
                    existing_config = [s for s in existing_config if not entry_matches_our_server(s)]
            elif is_array:
                # Root key contains array
                if root_key in existing_config and isinstance(existing_config[root_key], list):
                    existing_config[root_key] = [
                        s for s in existing_config[root_key] 
                        if not entry_matches_our_server(s)
                    ]
            else:
                # Root key contains object map
                if root_key in existing_config and isinstance(existing_config[root_key], dict):
                    keys_to_remove = [
                        key for key, entry in existing_config[root_key].items()
                        if entry_matches_our_server(entry)
                    ]
                    for key in keys_to_remove:
                        del existing_config[root_key][key]
            
            # Write modified config
            self._write_config_file(config_path, existing_config, auto_reg_format)
            
            # Update registration state
            auto_reg_state = config["settings"][0]["integrations"]["auto_registration_state"]
            if "registered" in auto_reg_state and integration_id in auto_reg_state["registered"]:
                del auto_reg_state["registered"][integration_id]
            self.config_manager.save_config(config)
            
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
        auto_reg_state = config["settings"][0]["integrations"]["auto_registration_state"]
        
        backups = auto_reg_state.get("backups", {}).get(integration_id, {})
        backup_info = backups.get(backup_timestamp)
        
        if not backup_info:
            raise ValueError(f"Backup not found: {integration_id}/{backup_timestamp}")
        
        backup_path = Path(backup_info["backup_path"])
        original_path = Path(backup_info["original_path"])
        
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_path}")
        
        # Restore file
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
        config = self.config_manager.load_config()
        auto_reg_state = config["settings"][0]["integrations"]["auto_registration_state"]
        
        if "registered" not in auto_reg_state:
            auto_reg_state["registered"] = {}
        
        auto_reg_state["registered"][integration_id] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "config_path": config_path,
            "backup": backup_timestamp
        }
        
        auto_reg_state["last_run"] = datetime.utcnow().isoformat() + "Z"
        
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
        auto_reg_state = config["settings"][0]["integrations"]["auto_registration_state"]
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

