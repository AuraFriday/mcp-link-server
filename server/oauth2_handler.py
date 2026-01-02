"""
file: ragtag/oauth2_handler.py
Project: Aura Friday MCP-Link Server
Component: Shared Configuration Access for RagTag
Author: Christopher Nathan Drake (cnd)

OAuth 2.0 Authorization Server Implementation for MCP Server

This module implements RFC 6749 (OAuth 2.0) with the following features:
- Dynamic Client Registration (RFC 7591)
- Authorization Code Grant with PKCE (RFC 7636)
- Refresh Tokens
- Token introspection
- Long-lived tokens with configurable expiration

Security Features:
- PKCE (Proof Key for Code Exchange) mandatory for all clients
- Secure random token generation
- Token binding to client_id
- State parameter validation
- Redirect URI validation

Storage:
- All tokens, clients, and authorization codes stored in settings[0].oauth
- Persistent across server restarts

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ǝᖴ𝟫𝟢υᏂNeƨОⲔоµνjᴜՕꓴH𝟛ⅼƴnѵ𝟩ƼΟ𝟢ᎪꙅНƿВ3QΤАᏂ0ᴡꓮFȜꓳ𝟦ĸƳƬsGxOƊlȠıӠßΑϜþE𝕌ᒿzꓜτɋģw4IꓦսMᗞ𐓒ТΕᏮΗցᴛ𝟚NՕᎠⴹᎠUɪþwųxѡΑuӠ𝟨Оƿꓴ3l6ɋlß",
"signdate": "2025-10-30T02:30:50.116Z",

"""

import json
import secrets
import hashlib
import base64
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from pathlib import Path

# Import logging from server
try:
    from easy_mcp.server import MCPLogger
except ImportError:
    # Fallback for testing
    class MCPLogger:
        @staticmethod
        def log(category: str, message: str):
            print(f"[{category}] {message}")


class OAuth2Handler:
    """Handles OAuth 2.0 authorization server functionality"""
    
    # Token expiration defaults (in seconds)
    DEFAULT_ACCESS_TOKEN_LIFETIME = 86400  # 24 hours
    DEFAULT_REFRESH_TOKEN_LIFETIME = 31536000  # 1 year (effectively infinite with refresh)
    DEFAULT_AUTH_CODE_LIFETIME = 600  # 10 minutes
    
    # Supported token lifetimes for user selection
    TOKEN_LIFETIME_OPTIONS = {
        "week": 604800,      # 7 days
        "month": 2592000,    # 30 days
        "year": 31536000,    # 365 days
        "forever": 315360000 # 10 years (effectively forever)
    }
    
    def __init__(self, config_manager):
        """
        Initialize OAuth2 handler
        
        Args:
            config_manager: SharedConfigManager instance for persistent storage
        """
        self.config_manager = config_manager
        self._ensure_oauth_structure()
    
    def _ensure_oauth_structure(self):
        """Ensure settings[0].oauth exists with proper structure"""
        config = self.config_manager.load_config()
        
        from ragtag.shared_config import SharedConfigManager
        oauth_section = SharedConfigManager.ensure_settings_section(config, 'oauth')
        
        # Initialize enabled flag if not present (defaults to False for security)
        if 'enabled' not in oauth_section:
            oauth_section['enabled'] = False
        
        # Initialize subsections if they don't exist
        if 'clients' not in oauth_section:
            oauth_section['clients'] = {}
        if 'authorization_codes' not in oauth_section:
            oauth_section['authorization_codes'] = {}
        if 'access_tokens' not in oauth_section:
            oauth_section['access_tokens'] = {}
        if 'refresh_tokens' not in oauth_section:
            oauth_section['refresh_tokens'] = {}
        
        self.config_manager.save_config(config)
    
    def is_oauth_enabled(self) -> bool:
        """Check if OAuth is enabled in configuration"""
        oauth_data = self._load_oauth_data()
        return oauth_data.get('enabled', False)
    
    def _load_oauth_data(self) -> Dict[str, Any]:
        """Load OAuth data from config"""
        config = self.config_manager.load_config()
        from ragtag.shared_config import SharedConfigManager
        return SharedConfigManager.ensure_settings_section(config, 'oauth')
    
    def _save_oauth_data(self, oauth_data: Dict[str, Any]):
        """Save OAuth data to config"""
        config = self.config_manager.load_config()
        config['settings'][0]['oauth'] = oauth_data
        self.config_manager.save_config(config)
    
    def _generate_token(self, length: int = 32) -> str:
        """Generate a cryptographically secure random token"""
        return secrets.token_urlsafe(length)
    
    def _hash_code_verifier(self, verifier: str) -> str:
        """Hash a PKCE code verifier using SHA256"""
        digest = hashlib.sha256(verifier.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')
    
    def _verify_pkce(self, code_verifier: str, code_challenge: str) -> bool:
        """Verify PKCE code_verifier matches code_challenge"""
        computed_challenge = self._hash_code_verifier(code_verifier)
        return secrets.compare_digest(computed_challenge, code_challenge)
    
    def _cleanup_expired_tokens(self):
        """Remove expired tokens and authorization codes"""
        oauth_data = self._load_oauth_data()
        current_time = time.time()
        
        # Clean up authorization codes
        expired_codes = [
            code for code, data in oauth_data['authorization_codes'].items()
            if data['expires_at'] < current_time
        ]
        for code in expired_codes:
            del oauth_data['authorization_codes'][code]
        
        # Clean up access tokens
        expired_access = [
            token for token, data in oauth_data['access_tokens'].items()
            if data['expires_at'] < current_time
        ]
        for token in expired_access:
            del oauth_data['access_tokens'][token]
        
        # Note: We don't expire refresh tokens automatically - they're long-lived
        # and only removed when explicitly revoked or when access token is refreshed
        
        if expired_codes or expired_access:
            self._save_oauth_data(oauth_data)
            MCPLogger.log("OAuth2", f"Cleaned up {len(expired_codes)} expired auth codes and {len(expired_access)} expired access tokens")
    
    # ========================================================================
    # Dynamic Client Registration (RFC 7591)
    # ========================================================================
    
    def handle_client_registration(self, body: str) -> Tuple[str, Dict[str, str], str]:
        """
        Handle POST /oauth2/register - Dynamic Client Registration
        
        Request body (JSON):
        {
          "client_name": "My MCP Client",
          "redirect_uris": ["http://localhost:8080/callback"],
          "token_endpoint_auth_method": "none"  // We only support "none" (public clients)
        }
        
        Response:
        {
          "client_id": "abc123...",
          "client_name": "My MCP Client",
          "redirect_uris": ["http://localhost:8080/callback"],
          "token_endpoint_auth_method": "none",
          "grant_types": ["authorization_code", "refresh_token"],
          "response_types": ["code"],
          "client_id_issued_at": 1234567890
        }
        """
        try:
            request_data = json.loads(body)
            
            # Validate required fields
            if 'redirect_uris' not in request_data or not request_data['redirect_uris']:
                return self._error_response(400, "invalid_request", "redirect_uris is required")
            
            # Generate client_id
            client_id = self._generate_token(32)
            issued_at = int(time.time())
            
            # Create client record
            client_data = {
                "client_id": client_id,
                "client_name": request_data.get("client_name", "Unnamed Client"),
                "redirect_uris": request_data['redirect_uris'],
                "token_endpoint_auth_method": "none",  # Only public clients supported
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_id_issued_at": issued_at,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            
            # Save to config
            oauth_data = self._load_oauth_data()
            oauth_data['clients'][client_id] = client_data
            self._save_oauth_data(oauth_data)
            
            MCPLogger.log("OAuth2", f"Registered new client: {client_data['client_name']} (ID: {client_id})")
            
            return "201 Created", {
                "Content-Type": "application/json",
                "Cache-Control": "no-store"
            }, json.dumps(client_data, indent=2)
            
        except json.JSONDecodeError:
            return self._error_response(400, "invalid_request", "Invalid JSON")
        except Exception as e:
            MCPLogger.log("OAuth2", f"Client registration error: {e}")
            return self._error_response(500, "server_error", "Internal server error")
    
    # ========================================================================
    # Authorization Endpoint
    # ========================================================================
    
    def handle_authorization_request(self, query_params: Dict[str, List[str]]) -> Tuple[str, Dict[str, str], str]:
        """
        Handle GET /oauth2/authorize - Authorization Endpoint
        
        Query parameters:
        - response_type: "code" (required)
        - client_id: registered client ID (required)
        - redirect_uri: must match registered URI (required)
        - state: opaque value for CSRF protection (recommended)
        - code_challenge: PKCE challenge (required)
        - code_challenge_method: "S256" (required)
        - scope: space-separated scopes (optional, only "offline_access" supported)
        
        This should show a consent page to the user. For now, we'll return HTML
        with a form that posts to /oauth2/authorize_approve
        """
        try:
            # Extract and validate parameters
            response_type = self._get_param(query_params, 'response_type')
            client_id = self._get_param(query_params, 'client_id')
            redirect_uri = self._get_param(query_params, 'redirect_uri')
            state = self._get_param(query_params, 'state', '')
            code_challenge = self._get_param(query_params, 'code_challenge')
            code_challenge_method = self._get_param(query_params, 'code_challenge_method')
            scope = self._get_param(query_params, 'scope', '')
            
            # Validate response_type
            if response_type != 'code':
                return self._redirect_error(redirect_uri, state, "unsupported_response_type", 
                                           "Only 'code' response_type is supported")
            
            # Validate client exists
            oauth_data = self._load_oauth_data()
            if client_id not in oauth_data['clients']:
                return self._error_response(400, "invalid_client", "Unknown client_id")
            
            client = oauth_data['clients'][client_id]
            
            # Validate redirect_uri
            if redirect_uri not in client['redirect_uris']:
                return self._error_response(400, "invalid_request", "redirect_uri does not match registered URIs")
            
            # Validate PKCE
            if not code_challenge or code_challenge_method != 'S256':
                return self._redirect_error(redirect_uri, state, "invalid_request",
                                           "PKCE with S256 is required")
            
            # Validate scope (we only support offline_access or empty)
            scopes = scope.split() if scope else []
            if scopes and scopes != ['offline_access']:
                return self._redirect_error(redirect_uri, state, "invalid_scope",
                                           "Only 'offline_access' scope is supported")
            
            # Generate consent page HTML
            consent_html = self._generate_consent_page(
                client_id=client_id,
                client_name=client['client_name'],
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope
            )

            # WRONG - This already IS the content page - logic error - we somehow need to get the server and client to agree a different way.            
            # TODO: If server has access to user.py tool (desktop environment), open consent page
            # in a 610x530 PySide2/PySide6 WebView window using:
            # - user.py's show_popup operation with url= pointing to /oauth2/authorize?[current params]
            # - Set wait_for_response=False so we return the HTML to the client immediately
            # - This provides better UX for local users (auto-opens in native window)
            # - If user.py not available (headless server), client gets HTML as fallback
            # Note: The authorization URL is: server.path + query_string (full original request URL)
            
            return "200 OK", {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store"
            }, consent_html
            
        except ValueError as e:
            return self._error_response(400, "invalid_request", str(e))
        except Exception as e:
            MCPLogger.log("OAuth2", f"Authorization request error: {e}")
            return self._error_response(500, "server_error", "Internal server error")
    
    def handle_authorization_approval(self, body: str) -> Tuple[str, Dict[str, str], str]:
        """
        Handle POST /oauth2/authorize_approve - User approves/denies authorization
        
        Form data:
        - client_id
        - redirect_uri
        - state
        - code_challenge
        - code_challenge_method
        - scope
        - token_lifetime (optional: "week", "month", "year", "forever")
        - approved (true/false)
        """
        try:
            # Parse form data
            form_data = urllib.parse.parse_qs(body)
            
            client_id = self._get_param(form_data, 'client_id')
            redirect_uri = self._get_param(form_data, 'redirect_uri')
            state = self._get_param(form_data, 'state', '')
            code_challenge = self._get_param(form_data, 'code_challenge')
            code_challenge_method = self._get_param(form_data, 'code_challenge_method')
            scope = self._get_param(form_data, 'scope', '')
            token_lifetime = self._get_param(form_data, 'token_lifetime', 'year')
            approved = self._get_param(form_data, 'approved', 'false')
            
            # Check if user denied
            if approved.lower() != 'true':
                return self._redirect_error(redirect_uri, state, "access_denied",
                                           "User denied authorization")
            
            # Generate authorization code
            auth_code = self._generate_token(32)
            expires_at = time.time() + self.DEFAULT_AUTH_CODE_LIFETIME
            
            # Store authorization code
            oauth_data = self._load_oauth_data()
            oauth_data['authorization_codes'][auth_code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "scope": scope,
                "token_lifetime": token_lifetime,
                "expires_at": expires_at,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            self._save_oauth_data(oauth_data)
            
            MCPLogger.log("OAuth2", f"Generated authorization code for client {client_id}")
            
            # Redirect back to client with code
            redirect_params = {"code": auth_code}
            if state:
                redirect_params["state"] = state
            
            redirect_url = self._build_redirect_url(redirect_uri, redirect_params)
            
            return "302 Found", {
                "Location": redirect_url,
                "Cache-Control": "no-store"
            }, ""
            
        except ValueError as e:
            return self._error_response(400, "invalid_request", str(e))
        except Exception as e:
            MCPLogger.log("OAuth2", f"Authorization approval error: {e}")
            return self._error_response(500, "server_error", "Internal server error")
    
    # ========================================================================
    # Token Endpoint
    # ========================================================================
    
    def handle_token_request(self, body: str, headers: Dict[str, str]) -> Tuple[str, Dict[str, str], str]:
        """
        Handle POST /oauth2/token - Token Endpoint
        
        Supports two grant types:
        1. authorization_code - Exchange auth code for tokens
        2. refresh_token - Refresh an access token
        
        Form data (authorization_code):
        - grant_type: "authorization_code"
        - code: authorization code
        - redirect_uri: must match original request
        - client_id: client identifier
        - code_verifier: PKCE verifier
        
        Form data (refresh_token):
        - grant_type: "refresh_token"
        - refresh_token: the refresh token
        - client_id: client identifier
        """
        try:
            # Parse form data
            form_data = urllib.parse.parse_qs(body)
            grant_type = self._get_param(form_data, 'grant_type')
            
            if grant_type == 'authorization_code':
                return self._handle_authorization_code_grant(form_data)
            elif grant_type == 'refresh_token':
                return self._handle_refresh_token_grant(form_data)
            else:
                return self._token_error_response("unsupported_grant_type", 
                                                  f"Grant type '{grant_type}' not supported")
        
        except ValueError as e:
            return self._token_error_response("invalid_request", str(e))
        except Exception as e:
            MCPLogger.log("OAuth2", f"Token request error: {e}")
            return self._token_error_response("server_error", "Internal server error")
    
    def _handle_authorization_code_grant(self, form_data: Dict[str, List[str]]) -> Tuple[str, Dict[str, str], str]:
        """Handle authorization_code grant type"""
        code = self._get_param(form_data, 'code')
        redirect_uri = self._get_param(form_data, 'redirect_uri')
        client_id = self._get_param(form_data, 'client_id')
        code_verifier = self._get_param(form_data, 'code_verifier')
        
        # Clean up expired tokens first
        self._cleanup_expired_tokens()
        
        # Load OAuth data
        oauth_data = self._load_oauth_data()
        
        # Validate authorization code
        if code not in oauth_data['authorization_codes']:
            return self._token_error_response("invalid_grant", "Invalid authorization code")
        
        auth_code_data = oauth_data['authorization_codes'][code]
        
        # Check expiration
        if auth_code_data['expires_at'] < time.time():
            del oauth_data['authorization_codes'][code]
            self._save_oauth_data(oauth_data)
            return self._token_error_response("invalid_grant", "Authorization code expired")
        
        # Validate client_id
        if auth_code_data['client_id'] != client_id:
            return self._token_error_response("invalid_grant", "client_id mismatch")
        
        # Validate redirect_uri
        if auth_code_data['redirect_uri'] != redirect_uri:
            return self._token_error_response("invalid_grant", "redirect_uri mismatch")
        
        # Verify PKCE
        if not self._verify_pkce(code_verifier, auth_code_data['code_challenge']):
            return self._token_error_response("invalid_grant", "PKCE verification failed")
        
        # Generate tokens
        access_token = self._generate_token(32)
        refresh_token = self._generate_token(32)
        
        # Get token lifetime from auth code
        token_lifetime_key = auth_code_data.get('token_lifetime', 'year')
        access_token_lifetime = self.TOKEN_LIFETIME_OPTIONS.get(token_lifetime_key, self.DEFAULT_ACCESS_TOKEN_LIFETIME)
        
        access_token_expires_at = time.time() + access_token_lifetime
        
        # Store tokens
        oauth_data['access_tokens'][access_token] = {
            "client_id": client_id,
            "scope": auth_code_data['scope'],
            "expires_at": access_token_expires_at,
            "token_lifetime_key": token_lifetime_key,
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        
        oauth_data['refresh_tokens'][refresh_token] = {
            "client_id": client_id,
            "scope": auth_code_data['scope'],
            "access_token": access_token,  # Link to current access token
            "token_lifetime_key": token_lifetime_key,
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        
        # Delete used authorization code (one-time use)
        del oauth_data['authorization_codes'][code]
        
        self._save_oauth_data(oauth_data)
        
        MCPLogger.log("OAuth2", f"Issued access token for client {client_id} (lifetime: {token_lifetime_key})")
        
        # Build response
        response_data = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(access_token_lifetime),
            "refresh_token": refresh_token
        }
        
        if auth_code_data['scope']:
            response_data["scope"] = auth_code_data['scope']
        
        return "200 OK", {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache"
        }, json.dumps(response_data)
    
    def _handle_refresh_token_grant(self, form_data: Dict[str, List[str]]) -> Tuple[str, Dict[str, str], str]:
        """Handle refresh_token grant type"""
        refresh_token = self._get_param(form_data, 'refresh_token')
        client_id = self._get_param(form_data, 'client_id')
        
        # Load OAuth data
        oauth_data = self._load_oauth_data()
        
        # Validate refresh token
        if refresh_token not in oauth_data['refresh_tokens']:
            return self._token_error_response("invalid_grant", "Invalid refresh token")
        
        refresh_token_data = oauth_data['refresh_tokens'][refresh_token]
        
        # Validate client_id
        if refresh_token_data['client_id'] != client_id:
            return self._token_error_response("invalid_grant", "client_id mismatch")
        
        # Revoke old access token
        old_access_token = refresh_token_data.get('access_token')
        if old_access_token and old_access_token in oauth_data['access_tokens']:
            del oauth_data['access_tokens'][old_access_token]
        
        # Generate new access token
        new_access_token = self._generate_token(32)
        
        # Get token lifetime from refresh token
        token_lifetime_key = refresh_token_data.get('token_lifetime_key', 'year')
        access_token_lifetime = self.TOKEN_LIFETIME_OPTIONS.get(token_lifetime_key, self.DEFAULT_ACCESS_TOKEN_LIFETIME)
        
        access_token_expires_at = time.time() + access_token_lifetime
        
        # Store new access token
        oauth_data['access_tokens'][new_access_token] = {
            "client_id": client_id,
            "scope": refresh_token_data['scope'],
            "expires_at": access_token_expires_at,
            "token_lifetime_key": token_lifetime_key,
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        
        # Update refresh token to point to new access token
        refresh_token_data['access_token'] = new_access_token
        refresh_token_data['refreshed_at'] = datetime.utcnow().isoformat() + "Z"
        
        self._save_oauth_data(oauth_data)
        
        MCPLogger.log("OAuth2", f"Refreshed access token for client {client_id}")
        
        # Build response
        response_data = {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": int(access_token_lifetime),
            "refresh_token": refresh_token  # Return same refresh token
        }
        
        if refresh_token_data['scope']:
            response_data["scope"] = refresh_token_data['scope']
        
        return "200 OK", {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache"
        }, json.dumps(response_data)
    
    # ========================================================================
    # Token Introspection (RFC 7662)
    # ========================================================================
    
    def handle_introspection_request(self, body: str) -> Tuple[str, Dict[str, str], str]:
        """
        Handle POST /oauth2/introspect - Token Introspection
        
        Form data:
        - token: the token to introspect
        - token_type_hint: "access_token" or "refresh_token" (optional)
        """
        try:
            form_data = urllib.parse.parse_qs(body)
            token = self._get_param(form_data, 'token')
            
            oauth_data = self._load_oauth_data()
            
            # Check if it's an access token
            if token in oauth_data['access_tokens']:
                token_data = oauth_data['access_tokens'][token]
                active = token_data['expires_at'] > time.time()
                
                response = {
                    "active": active,
                    "client_id": token_data['client_id'],
                    "token_type": "Bearer",
                    "exp": int(token_data['expires_at']),
                    "iat": int(time.mktime(datetime.fromisoformat(token_data['created_at'].rstrip('Z')).timetuple()))
                }
                
                if token_data['scope']:
                    response["scope"] = token_data['scope']
                
                return "200 OK", {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store"
                }, json.dumps(response)
            
            # Check if it's a refresh token
            elif token in oauth_data['refresh_tokens']:
                token_data = oauth_data['refresh_tokens'][token]
                
                response = {
                    "active": True,  # Refresh tokens don't expire
                    "client_id": token_data['client_id'],
                    "token_type": "refresh_token"
                }
                
                if token_data['scope']:
                    response["scope"] = token_data['scope']
                
                return "200 OK", {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store"
                }, json.dumps(response)
            
            # Token not found or invalid
            return "200 OK", {
                "Content-Type": "application/json",
                "Cache-Control": "no-store"
            }, json.dumps({"active": False})
            
        except Exception as e:
            MCPLogger.log("OAuth2", f"Introspection error: {e}")
            return "200 OK", {
                "Content-Type": "application/json",
                "Cache-Control": "no-store"
            }, json.dumps({"active": False})
    
    # ========================================================================
    # Token Revocation (RFC 7009)
    # ========================================================================
    
    def handle_revocation_request(self, body: str) -> Tuple[str, Dict[str, str], str]:
        """
        Handle POST /oauth2/revoke - Token Revocation
        
        Form data:
        - token: the token to revoke
        - token_type_hint: "access_token" or "refresh_token" (optional)
        """
        try:
            form_data = urllib.parse.parse_qs(body)
            token = self._get_param(form_data, 'token')
            
            oauth_data = self._load_oauth_data()
            revoked = False
            
            # Try to revoke access token
            if token in oauth_data['access_tokens']:
                del oauth_data['access_tokens'][token]
                revoked = True
                MCPLogger.log("OAuth2", f"Revoked access token")
            
            # Try to revoke refresh token (and its associated access token)
            if token in oauth_data['refresh_tokens']:
                refresh_data = oauth_data['refresh_tokens'][token]
                # Also revoke associated access token
                access_token = refresh_data.get('access_token')
                if access_token and access_token in oauth_data['access_tokens']:
                    del oauth_data['access_tokens'][access_token]
                del oauth_data['refresh_tokens'][token]
                revoked = True
                MCPLogger.log("OAuth2", f"Revoked refresh token and associated access token")
            
            if revoked:
                self._save_oauth_data(oauth_data)
            
            # Always return 200 OK per RFC 7009
            return "200 OK", {
                "Content-Type": "application/json",
                "Cache-Control": "no-store"
            }, ""
            
        except Exception as e:
            MCPLogger.log("OAuth2", f"Revocation error: {e}")
            # Still return 200 OK per RFC 7009
            return "200 OK", {
                "Content-Type": "application/json",
                "Cache-Control": "no-store"
            }, ""
    
    # ========================================================================
    # Helper Methods
    # ========================================================================

    # This was a wrong idea: codex client does not tell server the URL in the flow it selected.
    def _try_show_consent_popup(self, consent_html: str, auth_url: str) -> None:
        """
        Try to open consent page in user tool popup window, or fallback to browser.
        
        This provides better UX for local users by auto-opening the consent page.
        Priority order:
        1. User tool popup (best UX for desktop)
        2. Default system browser (fallback)
        3. No action (headless - client still gets HTML response)
        
        Args:
            consent_html: The HTML content for the consent page
            auth_url: The relative authorization URL (e.g., "/oauth2/authorize?...")
        """
        # Try user tool first (best UX)
        user_tool_success = False
        try:
            # Import get_server to access tool registry
            from ragtag.tools import get_server
            
            server = get_server()
            if server:
                # Get the user tool's token
                try:
                    from ragtag.tools import user
                    user_token = user.TOOL_UNLOCK_TOKEN
                    
                    # Call user tool to show consent page
                    # Use show_popup with wait_for_response=False for async display
                    result = server.call_tool_internal(
                        tool_name="user",
                        parameters={
                            "input": {
                                "operation": "show_popup",
                                "html": consent_html,
                                "title": "OAuth2 Authorization",
                                "width": 610,
                                "height": 530,
                                "modal": False,
                                "wait_for_response": False,
                                "tool_unlock_token": user_token
                            }
                        },
                        calling_tool="oauth2"
                    )
                    
                    if not result.get("isError"):
                        MCPLogger.log("OAuth2", "Successfully opened consent page in popup window")
                        user_tool_success = True
                    else:
                        MCPLogger.log("OAuth2", f"User tool returned error: {result}")
                        
                except (ImportError, AttributeError) as e:
                    MCPLogger.log("OAuth2", f"User tool not available: {e}")
            else:
                MCPLogger.log("OAuth2", "Server instance not available for consent popup")
                
        except Exception as e:
            MCPLogger.log("OAuth2", f"Could not show consent popup via user tool: {e}")
        
        # Fallback to browser if user tool didn't work
        if not user_tool_success:
            try:
                import webbrowser
                
                # Need to construct full URL with server address
                # For local server, assume http://localhost with the SSE port
                # TODO: Get actual server base URL from config or server instance
                # For now, try common default
                full_url = f"http://localhost:8765{auth_url}"
                
                MCPLogger.log("OAuth2", f"Attempting to open consent page in default browser: {full_url}")
                webbrowser.open(full_url)
                MCPLogger.log("OAuth2", "Successfully opened consent page in default browser")
                
            except Exception as e:
                # Gracefully ignore - client still gets HTML response
                MCPLogger.log("OAuth2", f"Could not open browser for consent page: {e}")
    
    def _get_param(self, params: Dict[str, List[str]], key: str, default: Optional[str] = None) -> str:
        """Extract a parameter from query/form data"""
        if key not in params:
            if default is not None:
                return default
            raise ValueError(f"Missing required parameter: {key}")
        
        values = params[key]
        if not values:
            if default is not None:
                return default
            raise ValueError(f"Empty parameter: {key}")
        
        return values[0]
    
    def _error_response(self, status_code: int, error: str, description: str) -> Tuple[str, Dict[str, str], str]:
        """Generate an OAuth error response"""
        status_map = {
            400: "400 Bad Request",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            500: "500 Internal Server Error"
        }
        
        error_data = {
            "error": error,
            "error_description": description
        }
        
        return status_map.get(status_code, "400 Bad Request"), {
            "Content-Type": "application/json",
            "Cache-Control": "no-store"
        }, json.dumps(error_data)
    
    def _token_error_response(self, error: str, description: str) -> Tuple[str, Dict[str, str], str]:
        """Generate a token endpoint error response"""
        error_data = {
            "error": error,
            "error_description": description
        }
        
        return "400 Bad Request", {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache"
        }, json.dumps(error_data)
    
    def _redirect_error(self, redirect_uri: str, state: str, error: str, description: str) -> Tuple[str, Dict[str, str], str]:
        """Redirect to client with error"""
        error_params = {
            "error": error,
            "error_description": description
        }
        if state:
            error_params["state"] = state
        
        redirect_url = self._build_redirect_url(redirect_uri, error_params)
        
        return "302 Found", {
            "Location": redirect_url,
            "Cache-Control": "no-store"
        }, ""
    
    def _build_redirect_url(self, base_uri: str, params: Dict[str, str]) -> str:
        """Build a redirect URL with query parameters"""
        query_string = urllib.parse.urlencode(params)
        separator = '&' if '?' in base_uri else '?'
        return f"{base_uri}{separator}{query_string}"
    
    def _generate_consent_page(self, client_id: str, client_name: str, redirect_uri: str,
                               state: str, code_challenge: str, code_challenge_method: str,
                               scope: str) -> str:
        """
        Generate HTML consent page
        
        TODO: This should be replaced with a proper UI that can:
        1. Show client information
        2. Let user select token lifetime
        3. Show what permissions are being requested
        4. Allow approve/deny
        """
        
        # Escape HTML
        def escape_html(text):
            return (text.replace('&', '&amp;')
                       .replace('<', '&lt;')
                       .replace('>', '&gt;')
                       .replace('"', '&quot;')
                       .replace("'", '&#39;'))
        
        return f"""<!DOCTYPE html>
<html>
<head>
    <title>Authorization Request</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .consent-box {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            margin-top: 0;
        }}
        .client-info {{
            background: #f9f9f9;
            padding: 15px;
            border-radius: 4px;
            margin: 20px 0;
        }}
        .permissions {{
            margin: 20px 0;
        }}
        .permission-item {{
            padding: 10px;
            margin: 5px 0;
            background: #f0f0f0;
            border-radius: 4px;
        }}
        .lifetime-selector {{
            margin: 20px 0;
        }}
        .lifetime-selector label {{
            display: block;
            margin: 10px 0;
        }}
        .buttons {{
            margin-top: 30px;
            display: flex;
            gap: 10px;
        }}
        button {{
            padding: 12px 24px;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            cursor: pointer;
        }}
        .approve {{
            background: #4CAF50;
            color: white;
            flex: 1;
        }}
        .deny {{
            background: #f44336;
            color: white;
            flex: 1;
        }}
        .approve:hover {{
            background: #45a049;
        }}
        .deny:hover {{
            background: #da190b;
        }}
    </style>
</head>
<body>
    <div class="consent-box">
        <h1>🔐 Authorization Request</h1>
        
        <div class="client-info">
            <strong>{escape_html(client_name)}</strong> is requesting access to your MCP server.
        </div>
        
        <div class="permissions">
            <h3>Requested Permissions:</h3>
            <div class="permission-item">
                ✓ Access your MCP server tools and resources
            </div>
            {f'<div class="permission-item">✓ Offline access (refresh tokens)</div>' if 'offline_access' in scope else ''}
        </div>
        
        <div class="lifetime-selector">
            <h3>Token Lifetime:</h3>
            <label>
                <input type="radio" name="token_lifetime" value="week"> 
                <strong>1 Week</strong> - Token expires in 7 days
            </label>
            <label>
                <input type="radio" name="token_lifetime" value="month">
                <strong>1 Month</strong> - Token expires in 30 days
            </label>
            <label>
                <input type="radio" name="token_lifetime" value="year" checked>
                <strong>1 Year</strong> - Token expires in 365 days (recommended)
            </label>
            <label>
                <input type="radio" name="token_lifetime" value="forever">
                <strong>Forever</strong> - Token never expires (use refresh to keep alive)
            </label>
        </div>
        
        <form method="POST" action="/oauth2/authorize_approve" id="consentForm">
            <input type="hidden" name="client_id" value="{escape_html(client_id)}">
            <input type="hidden" name="redirect_uri" value="{escape_html(redirect_uri)}">
            <input type="hidden" name="state" value="{escape_html(state)}">
            <input type="hidden" name="code_challenge" value="{escape_html(code_challenge)}">
            <input type="hidden" name="code_challenge_method" value="{escape_html(code_challenge_method)}">
            <input type="hidden" name="scope" value="{escape_html(scope)}">
            <input type="hidden" name="token_lifetime" id="selectedLifetime" value="year">
            <input type="hidden" name="approved" id="approvedField" value="false">
            
            <div class="buttons">
                <button type="button" class="deny" onclick="submitForm(false)">
                    ✗ Deny
                </button>
                <button type="button" class="approve" onclick="submitForm(true)">
                    ✓ Approve
                </button>
            </div>
        </form>
    </div>
    
    <script>
        function submitForm(approved) {{
            // Get selected lifetime
            const lifetimeRadios = document.getElementsByName('token_lifetime');
            for (const radio of lifetimeRadios) {{
                if (radio.checked) {{
                    document.getElementById('selectedLifetime').value = radio.value;
                    break;
                }}
            }}
            
            // Set approved status
            document.getElementById('approvedField').value = approved ? 'true' : 'false';
            
            // Submit form
            document.getElementById('consentForm').submit();
        }}
    </script>
</body>
</html>"""
    
    def verify_bearer_token(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Verify a Bearer token and return token data if valid
        
        Args:
            token: The Bearer token to verify
            
        Returns:
            Token data dict if valid, None if invalid/expired
        """
        oauth_data = self._load_oauth_data()
        
        if token in oauth_data['access_tokens']:
            token_data = oauth_data['access_tokens'][token]
            
            # Check expiration
            if token_data['expires_at'] > time.time():
                return token_data
        
        return None


