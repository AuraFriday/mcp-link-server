
__version__ = "1.0.0" # not used - see version.txt

#!/usr/bin/env python3
"""
Aura Friday's mcp-link server - MCP Server - An ecosystem of useful tools
Copyright: ? 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ⲔƤþрkꓟ𝟑jрⲞꓑYƶ𐐕Ƽ4Qıοԛm𝘈6օᗞƙ৭ꓬᗞ𝟦АҮꓴ𝟛ƵD𝘈ԛ𝛢ʈꓪυU𝟚Ƥꓝꞇ𝟧gՕᎬбƨτƧlⲔ2TďGꓓJбȜ𝟟voᑕrһԛßOɡꓑꓪⲦⲞNеТᏟWdꓪΑꓠǝ𝟫ɯ1ɯ𝛢ᛕВ𝟨ΝⅼjųȣᴍбοAⲟսı",
"signdate": "2026-07-23T02:36:51.863Z",


Main server implementation for the Aura Friday's mcp-link server, providing an MCP interface
for interacting with local tools.
"""

import json
import http.client
import argparse
import sys
import os
import threading
import time
import subprocess
import platform
import uuid
import getpass,base64,atexit
import hmac  # constant-time API-key comparison (review D6)
import re
import html
import mimetypes
from datetime import datetime
from pathlib import Path
from easy_mcp import MCPServer
from easy_mcp.server import MCPLogger
from .tools import ALL_TOOLS, HANDLERS, ORIGINAL_TOOLS, set_server, notify_all_tools_registered
from .tools import local as local_tools
from .tools import remote as remote_tools
from platformdirs import user_data_dir

# Global variables for authentication
AUTHORIZED_USERS = {}
DISABLE_AUTH = False  # Global switch to disable authentication for testing
# Whether to accept an API key smuggled as the first DNS label of the Host header
# (hostname-UUID auth, review B4). This puts the credential into DNS queries/SNI/logs,
# so it is a config-gated channel. Default True preserves friday.py's internal
# settings-URL flow; operators can disable it via ragtag.enable_hostname_uuid_auth=false.
ENABLE_HOSTNAME_UUID_AUTH = True

# ---------------------------------------------------------------------------
# Authentication policy (review D5) - the single documented list of every
# credential scheme this server accepts, in the order validate_auth tries them,
# and the config gate controlling each one:
#   1. URL parameters   ?user=<name>&RAGTAG_API_KEY=<key>  - always on; used by the
#      homepage/settings pages. The key appears in URLs/history/logs, so HTTPS only.
#   2. HTTP Basic       Authorization: Basic <user:key>    - always on.
#   3. HTTP Bearer      Authorization: Bearer <key>        - always on for API keys;
#      OAuth access tokens are additionally honored only while
#      settings[0].oauth.enabled is true (token minting lives in oauth2_handler).
#   4. Hostname UUID    Host: <key>-<real-hostname>        - gated by
#      ragtag.enable_hostname_uuid_auth (see ENABLE_HOSTNAME_UUID_AUTH above).
#   5. disable_auth     ragtag.disable_auth=true           - master off-switch, gated
#      in exactly one place (check_global_auth returns early); validate_auth always
#      returns a real verdict so no other path can silently widen access (review A3).
# ---------------------------------------------------------------------------

# Canonical OAuth discovery paths. check_global_auth's unauthenticated allow-list and
# handle_default_request's discovery route MUST share this exact set (review A1): a path
# variant allowed through auth but not served would fall through toward the homepage.
OAUTH_DISCOVERY_PATHS = (
    '/.well-known/oauth-authorization-server',
    '/.well-known/oauth-authorization-server/',
    '/.well-known/oauth-authorization-server/sse',
    '/sse/.well-known/oauth-authorization-server',
)


def mask_secret_for_logging(secret_value):
    """Return an API key/token in a form safe to write to the world-shared logfile.

    Keeps only the first and last 4 characters so a leaked logfile cannot be used to
    authenticate (review B3). Returns a placeholder for missing/short values.
    """
    if not isinstance(secret_value, str) or not secret_value:
        return "<none>"
    if len(secret_value) <= 8:
        return "****"
    return f"{secret_value[:4]}...{secret_value[-4:]}"


def api_key_matches_constant_time(offered_credential_value, stored_api_key_value):
    """Compare an attacker-supplied credential against a stored API key in constant time.

    Uses hmac.compare_digest so equality checks cannot leak matching prefixes via timing
    (review D6). Compares UTF-8 bytes because compare_digest raises TypeError on non-ASCII
    str inputs, and the offered credential is attacker-controlled.
    """
    if offered_credential_value is None or stored_api_key_value is None:
        return False
    return hmac.compare_digest(str(offered_credential_value).encode('utf-8'), str(stored_api_key_value).encode('utf-8'))

# Global color aliases
NORM = "\033[0m"    # Reset to normal
RED = "\033[31;1m"  # Bright red
GRN = "\033[32;1m"  # Bright green
YEL = "\033[33;1m"  # Bright yellow
NAV = "\033[34;1m"  # Bright blue (navy)
BLU = "\033[36;1m"  # Bright cyan (blue)
PRP = "\033[35;1m"  # Bright magenta (purple)
WHT = "\033[37;1m"  # Bright white
SAVE = "\033[s"     # Save cursor position
REST = "\033[u"     # Restore cursor position
CLR = "\033[K"      # Clear to end of line

def disable_colors(): # """Disable all color output by setting color aliases to empty strings"""
    global NORM, RED, GRN, YEL, NAV, BLU, PRP, WHT, SAVE, REST, CLR
    NORM = RED = GRN = YEL = NAV = BLU = PRP = WHT = SAVE = REST = CLR = ""

# Optional import of server_control - won't fail if module is removed
try:
    from .tools import server_control
except ImportError:
    server_control = None

# Constants
VERSION = "1.2.30"  # Semantic version with pre-release tag
DEFAULT_PORT = 31173
DEFAULT_HOST = '127.0.0.1'
DEFAULT_DOMAIN = '127-0-0-1.local.aurafriday.com'
# DEFAULT_HOST = '172.22.1.88' # RoG
# DEFAULT_DOMAIN = '172-22-1-88.local.aurafriday.com'


# MCP_CONFIG_FILE = os.path.expanduser("~/.cursor/mcp.json")

# Web page content
HOMEPAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Aura Friday's mcp-link server</title>
    <link rel="stylesheet" href="{cdn_base}/github.min.css">
    <script src="{cdn_base}/highlight.min.js"></script>
    <script src="{cdn_base}/marked.min.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
        h1 { color: #0066cc; }
        .tool { 
            background: #f5f5f5; 
            padding: 20px; 
            margin: 25px 0; 
            border-radius: 8px;
            border: 1px solid #e0e0e0;
        }
        .tool-name {
            color: #2c3e50;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e0e0e0;
        }
        .tool-description {
            font-size: 1.1em;
            color: #34495e;
            padding: 15px;
            margin: 10px 0;
            background: #fff;
            border-radius: 6px;
            border-left: 4px solid #0066cc;
            white-space: pre-line;
        }
        .tool-description.with-readme {
            font-weight: 500;
            font-style: italic;
            background: linear-gradient(to right, #f8f9fa, #ffffff);
        }
        .tool-readme {
            margin-top: 20px;
            padding: 15px;
            background: #fff;
            border-radius: 6px;
            border: 1px solid #e0e0e0;
        }
        pre { 
            background: #f8f9fa; 
            padding: 12px; 
            border-radius: 6px; 
            overflow-x: auto;
            border: 1px solid #e0e0e0;
        }
        .escape-table { border-collapse: collapse; margin: 15px 0; }
        .escape-table td, .escape-table th { border: 1px solid #ddd; padding: 8px; }
        .escape-table th { background-color: #f2f2f2; }
        .settings-container { margin: 30px 0; }
        .settings-textarea { 
            width: 100%; 
            height: 160px; 
            font-family: monospace; 
            padding: 10px; 
            margin: 10px 0;
            border-radius: 5px;
            border: 1px solid #ccc;
        }
        .copy-button {
            background: #0066cc;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
        }
        .copy-button:hover { background: #0052a3; }
        .copy-success { 
            color: #28a745;
            margin-left: 10px;
            display: none;
        }
        .header-nav {
            position: absolute;
            top: 20px;
            right: 40px;
            z-index: 1000;
        }
        .nav-button {
            background: #0066cc;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
        }
        .nav-button:hover {
            background: #0052a3;
            color: white;
            text-decoration: none;
        }
        .parameters-details {
            margin: 15px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 6px;
            border: 1px solid #e0e0e0;
        }
        .parameters-details summary {
            cursor: pointer;
            padding: 5px;
            color: #0066cc;
            font-weight: 500;
        }
        .parameters-details summary:hover {
            color: #0052a3;
        }
        .parameters-details pre {
            margin: 10px 0 0 0;
            background: #fff;
        }
    </style>
</head>
<body>
    <script>var RAGTAG_CURRENT_USER = "{current_user}";</script>
    <div class="header-nav">
        <a href="/pages/popover.html" class="nav-button">?? Settings</a>
    </div>
    <h1>Aura Friday's mcp-link server</h1>
    <p>Copyright (c) 2025 Chris Drake. All rights reserved.</p>
    <p>An ecosystem of useful local MCP tools.</p>
    <p>Server domain: <code>{server_url}</code> | Current user: <code>{current_user}</code> | Version: <code>v{version}</code></p>
    
    {tool_sections}

    <div class="settings-container">
        <h2>Cursor IDE Configuration</h2>
        <p>Copy these settings to your Cursor IDE MCP configuration file at <code>~/.cursor/mcp.json</code>:</p>
        <textarea id="mcp-settings" class="settings-textarea" readonly>{
  "mcpServers": {  
    "mypc": {
      "url": "{server_url}sse",
      "headers": {
        "Authorization": "Bearer (loading your key...)",
        "Content-Type": "application/json"
      }
    }
  }
}
</textarea>
        <div>
            <button id="copy-button" class="copy-button">Copy to Clipboard</button>
            <span id="copy-success" class="copy-success">? Copied!</span>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            // The API key is NOT in this page's HTML (review A2/B1). Fetch the ready-to-paste
            // MCP config from an authenticated endpoint so the key never lands in page
            // HTML/cache/proxies. The browser reuses the session credentials for this fetch.
            (function loadMcpConfigFromAuthenticatedEndpoint() {
                var settingsTextarea = document.getElementById('mcp-settings');
                if (!settingsTextarea || typeof RAGTAG_CURRENT_USER === 'undefined' || !RAGTAG_CURRENT_USER) {
                    return;
                }
                // Propagate the page's own query string so URL-parameter auth
                // (?user=...&RAGTAG_API_KEY=...) also authenticates this fetch.
                fetch('/api/users/' + encodeURIComponent(RAGTAG_CURRENT_USER) + '/mcp_json' + window.location.search, { credentials: 'include' })
                    .then(function(response) {
                        if (!response.ok) { throw new Error('HTTP ' + response.status); }
                        return response.json();
                    })
                    .then(function(mcpConfig) {
                        // Show a clear message instead of a config block containing an empty
                        // key when the account has no API key configured (review A8).
                        var mypcServerEntry = ((mcpConfig || {}).mcpServers || {}).mypc || {};
                        var authorizationHeaderValue = (mypcServerEntry.headers || {})['Authorization'] || '';
                        if (authorizationHeaderValue.replace('Bearer', '').trim() === '') {
                            settingsTextarea.value = '# No API key is configured for your account yet.\n# Open the Settings page to create one.';
                            return;
                        }
                        settingsTextarea.value = JSON.stringify(mcpConfig, null, 2);
                    })
                    .catch(function(err) {
                        settingsTextarea.value = '# Could not load your MCP config (' + err + ').\n# Open the Settings page to copy your key.';
                    });
            })();

            // Initialize markdown parsing (resilient to missing dependencies)
            try {
                if (typeof marked !== 'undefined') {
                    marked.setOptions({
                        highlight: function(code, lang) {
                            if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
                                return hljs.highlight(code, { language: lang }).value;
                            }
                            if (typeof hljs !== 'undefined') {
                                return hljs.highlightAuto(code).value;
                            }
                            return code;
                        }
                    });

                    // Convert markdown content
                    document.querySelectorAll('.markdown-content').forEach(function(el) {
                        el.innerHTML = marked.parse(el.textContent);
                    });
                }
            } catch (err) {
                console.error('[COPY-BUTTON] Error in markdown processing:', err);
            }

            // Initialize syntax highlighting (resilient to missing dependencies)
            try {
                if (typeof hljs !== 'undefined') {
                    hljs.highlightAll();
                }
            } catch (err) {
                console.error('[COPY-BUTTON] Error in syntax highlighting:', err);
            }

            // Copy button functionality (always works regardless of other dependencies)
            const copyButton = document.getElementById('copy-button');
            const copySuccess = document.getElementById('copy-success');
            const textarea = document.getElementById('mcp-settings');

            if (!copyButton || !copySuccess || !textarea) {
                console.error('[COPY-BUTTON] Required elements not found!');
                return;
            }

            // Modern clipboard copy with fallback
            async function copyToClipboard() {
                const text = textarea.value;
                
                // Try modern Clipboard API first (works on HTTPS and localhost)
                if (navigator.clipboard && window.isSecureContext) {
                    try {
                        await navigator.clipboard.writeText(text);
                        copySuccess.textContent = '? Copied!';
                        copySuccess.style.color = '#28a745';
                        copySuccess.style.display = 'inline';
                        setTimeout(function() {
                            copySuccess.style.display = 'none';
                        }, 2000);
                        return;
                    } catch (err) {
                        // Fall through to legacy method
                    }
                }
                
                // Fallback to legacy document.execCommand (works on HTTP)
                textarea.select();
                textarea.setSelectionRange(0, 99999); // For mobile devices
                
                try {
                    const successful = document.execCommand('copy');
                    if (successful) {
                        copySuccess.textContent = '? Copied!';
                        copySuccess.style.color = '#28a745';
                        copySuccess.style.display = 'inline';
                        setTimeout(function() {
                            copySuccess.style.display = 'none';
                        }, 2000);
                    } else {
                        throw new Error('execCommand returned false');
                    }
                } catch (err) {
                    console.error('[COPY-BUTTON] All copy methods failed:', err);
                    // Show error message with instructions
                    copySuccess.textContent = '? Copy blocked - please select and copy manually (Ctrl+C)';
                    copySuccess.style.color = '#dc3545';
                    copySuccess.style.display = 'inline';
                    textarea.select();
                    // Keep the message visible longer for blocked clipboard
                    setTimeout(function() {
                        copySuccess.style.display = 'none';
                    }, 5000);
                }
            }

            copyButton.addEventListener('click', function() {
                copyToClipboard();
            });
        });
    </script>
</body>
</html>
"""

# # Settings page content
# SETTINGS_HTML = """
# <!DOCTYPE html>
# <html>
# <head>
#     <title>Settings - Aura Friday's mcp-link server</title>
#     <style>
#         body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
#         h1 { color: #0066cc; }
#         .settings-container { 
#             background: #f5f5f5; 
#             padding: 40px; 
#             border-radius: 8px;
#             text-align: center;
#             margin: 20px 0;
#         }
#         .back-link {
#             display: inline-block;
#             margin-bottom: 20px;
#             color: #0066cc;
#             text-decoration: none;
#             font-size: 14px;
#         }
#         .back-link:hover { text-decoration: underline; }
#     </style>
# </head>
# <body>
#     <a href="/" class="back-link">? Back to Tools</a>
#     <h1>Settings</h1>
#     <div class="settings-container">
#         <h2>?? Coming Soon</h2>
#         <p>Settings configuration will be available in a future update.</p>
#         <p>Server domain: <code>{server_url}</code> | Current user: <code>{current_user}</code> | Version: <code>v{version}</code></p>
# 
#     </div>
# </body>
# </html>
# """

# Base64 encoded favicon
# FAVICON_B64 = "AAABAAEAEBAAAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAA//8AAP4AAACv1wAA6AAAAM4AAACVpQAAswAAAJwAAABmswAAanEAAHEBAABaJgAAM5kAAEoAAAAlAAAAAQAAAAAPo0REROAAD+1EREQ28AD6ETRERDfwAPZLY0REOgAA9nyXE0Q9AA5kSomqYzoAD0NEqCK2GvAA40N1ACoW8AD3NHkAWjPgAPc0OpW0Q9AA9zRDekND0AAKFEQzRDTwAA9DREREGvAAAOMURDF/AAAADnMzSvAAAAAA/t3/AAD4AQAA4AEAAMABAADAAwAAwAMAAIADAACAAQAAwAEAAMABAADAAQAAwAEAAOABAADgAQAA8AMAAPgHAAD8DwAA"

FAVICON_B64 = "AAABAAEAEBAAAAEAIABoBAAAFgAAACgAAAAQAAAAIAAAAAEAIAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" + \
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJYz/SaSMProljP9NAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" + \
              "AAB8IOjZdBni/34h69kAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB+Ieridxzl/3MY4f99IOnrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIUm8Ld4HeXp" + \
              "eR3m53oe5+V4HebucRjg/nYb4/pzGeH+eh/n+o8t91EAAAAAAAAAAAAAAAAAAAAAjSz2o3sf6O96H+jtkjD6Z3Qa4miDJe4/ljP9HWsT2zR/IuthkjD6Vn4h6s12G+P6hCbvswAAAACWM/0veh7n73wg" + \
              "6OZ9IenoaxPbI5Yz/RF4HOWcaxPbkWsT24JsFNzYbRXdvmsT2zgAAAAAAAAAAHcc5PiRMPmDgCPs4IUn8MEAAAAAfiHq024V3YEAAAAAljP9V4gp8pB6Hue7hyjx24kp8qVzGeKQaxPbDnQZ4gqHKPKs" + \
              "fCDo7Xsf6OaWM/1NaxPbEZYz/YZ+IeribhXd8GsT2zAAAAAAljP9LWsT2w5rE9sLljP9IYEj7Dd0GuK4ljP9K3gd5ed4HeXpljP9k3IZ4b9rE9sNjS32zHQa4v+BI+yvAAAAAAAAAACFJvCBdxzksHAX" + \
              "37N1G+PPdhvkW5Av+U57H+jkgCPs34Ql7717H+eKcxnioJYz/RyWM/06ljP9LAAAAACTMfseljP9DZYz/Vd4HOXbbxbecAAAAAB1G+P5jCv1tZQx+1J2HOT1AAAAAHcc5cJrE9tLaxPbE2wU3D0AAAAA" + \
              "lTL8SH8i6pprE9s3ljP9Tncc5JGOLfdfex/o7QAAAAAAAAAAgyXuwX0g6e6DJe45dRrjv3Yb5MB4HeVyaxPbHwAAAAB3HOWnbxbekpYz/QaWM/0+eB3l9Yoq9IYAAAAAAAAAAAAAAACBJO3Teh7n9JMw" + \
              "+mQAAAAAljP9Anwg6ZR0GuKvcxnhpWsT2xV/IuvdeR3m94Qm77cAAAAAAAAAAAAAAAAAAAAAAAAAAIsq9Ip5Hubzeh7m7n4h6tKGJ/C6hCXu03wg6d94HeX1fyLr4wAAAAAAAAAAAAAAAAAAAAAAAAAA" + \
              "AAAAAAAAAAAAAAAAljP9RpYz/bKNLPbIgiTt24Ik7d6JKfPYljP9sgAAAAAAAAAAAAAAAAAAAAAAAAAA//8AAP/vAAD/xwAA/4cAAPAHAADH8QAAjBwAACYMAABj+gAAEYYAAA/sAACvtQAAk5kAAM4jAADgDwAA+B8AAA=="

def get_connection_info(args,master_dir):
    """
    Determine connection type (HTTP/HTTPS) and certificate paths based on args.
    Used by both server and client to ensure consistent behavior.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        tuple: (enable_https, cert_path, key_path, ca_path)
            enable_https: True if using HTTPS, False for HTTP
            cert_path: Path to certificate or None
            key_path: Path to private key or None
            ca_path: Path to CA certificate bundle or None

    See __init__.py-readme.txt

    """
    enable_https = not args.http
    cert_path = None
    key_path = None
    ca_path = None
    
    if enable_https:

        # Use platformdirs for cross-platform app data location
        try:
            local_storage_folder = user_data_dir('ragtag','') # DEBUG: local_storage_folder = C:\Users\cnd\AppData\Local\ragtag
        except Exception as e: # Fallback to manual platform detection if user_data_dir not available            
            if platform.system() == 'Windows': # Use AppData/Local on Windows
                local_storage_folder = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~\\AppData\\Local')), 'ragtag')
            elif platform.system() == 'Darwin':   # Use ~/Library/Application Support on macOS
                local_storage_folder = os.path.join(os.path.expanduser('~/Library/Application Support'), 'ragtag')
            else:  # Use ~/.local/share on Linux (XDG Base Directory specification)
                local_storage_folder = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')), 'ragtag')
        
        cert_filename = 'fullchain.pem'
        key_filename = 'privkey.pem'
        ca_filename = 'cacert.der'
        
        # Define potential certificate locations in priority order
        cert_locations = [
            master_dir,
            #local_storage_folder, # ZeroSSL does not work in cursor without intermediate certs
            os.path.join(os.path.dirname(__file__), 'private', 'certs')
        ]
        
        # Find first location where both certificate files exist
        for location in cert_locations:
            try:
                os.makedirs(location, mode=0o755, exist_ok=True)
            except Exception as e:
                print(f"Warning: Could not create directory {location}: {e}")

            cert_path = os.path.join(location, cert_filename) if os.path.exists(os.path.join(location, cert_filename)) else None
            key_path = os.path.join(location, key_filename) if os.path.exists(os.path.join(location, key_filename)) else None
            ca_path = os.path.join(location, ca_filename) if os.path.exists(os.path.join(location, ca_filename)) else None

            if (cert_path and key_path) or ca_path:
                break
        else:
            # If no valid pair found, fall back to HTTP
            print(f"Warning: No valid certificate pair found, falling back to HTTP mode")
            enable_https = False
            cert_path = None
            key_path = None
            ca_path = None

        # Check if certificates exist
        #if enable_https and not os.path.exists(os.path.dirname(cert_path)):
        #    try:
        #        os.makedirs(os.path.dirname(cert_path), mode=0o700)  # Create with restricted permissions
        #        print(f"Created certificate directory: {os.path.dirname(cert_path)}")
        #    except Exception as e:
        #        print(f"Warning: Could not create certificate directory: {e}")
        
        #if enable_https:
        #    # Check certificate permissions
        #    try:
        #        os.chmod(cert_path, 0o600)  # Read/write for owner only
        #        os.chmod(key_path, 0o600)   # Read/write for owner only
        #    except Exception as e:
        #        print(f"Warning: Could not set certificate permissions: {e}")

        #print(f"[33;1m Using http? ({enable_https}) with cert ({cert_path}) and key ({key_path}) [0m ")
        print(f"Using https? ({enable_https}) with cert ({cert_path}) and key ({key_path}) and ca ({ca_path})")
    return enable_https, cert_path, key_path, ca_path


def get_new_certificate(args):
    """
    Get a new certificate from the server
    """
    local_storage_folder = user_data_dir('ragtag','') # DEBUG: local_storage_folder = C:\Users\cnd\AppData\Local\ragtag
    return None, None, None


def derive_public_url_host_from_bind_host(bind_host, enable_https):
    """Return the hostname to advertise in URLs for the given bind host (review D4).

    Over HTTPS, a bare IPv4 bind address (e.g. 192.168.1.5) can never match the
    *.local.aurafriday.com certificate, so any URL built from it (IDE registration,
    control-client connections) would fail TLS verification. The public DNS zone maps
    the dashed form of every IP to itself (192-168-1-5.local.aurafriday.com ->
    192.168.1.5), so advertise that name instead. Hostnames, and HTTP mode (no cert
    to match), are returned unchanged.
    """
    if enable_https and isinstance(bind_host, str) and re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', bind_host):
        return bind_host.replace('.', '-') + '.local.aurafriday.com'
    return bind_host


def manage_ragtag_config(fris):
    """
    Manage the ragtag configuration in nativemessaging.json.
    
    If the ragtag section doesn't exist, create it with:
    - A new UUID as the API key
    - Current logged-in user as an authorized user
    
    Always reads the authorized_users dict from the file and stores it globally.
    
    Returns:
        dict: The authorized_users dictionary
    """
    global AUTHORIZED_USERS, DISABLE_AUTH, ENABLE_HOSTNAME_UUID_AUTH
    
    # Import shared config manager. This is a hard dependency of the server; if it cannot
    # be imported there is no usable config, so fail loudly here rather than continuing
    # with unbound config_manager/ragtag_config and raising a confusing NameError below
    # (review A7).
    try:
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        
        # Get existing ragtag config or empty dict
        ragtag_config = config_manager.get_ragtag_config()
        master_dir = config_manager._find_master_directory()
        
    except ImportError as shared_config_import_error:
        MCPLogger.log("Config", f"Fatal: shared config manager unavailable: {shared_config_import_error}")
        raise

    
    # Check if ragtag config exists
    file_was_created = False
    if not ragtag_config or not ragtag_config.get("authorized_users"):
        file_was_created = True
        # Generate new UUID for API key
        api_key = str(uuid.uuid4())
        
        # Get current logged-in user
        try:
            current_user = getpass.getuser()
        except Exception as e:
            MCPLogger.log("Config", f"Warning: Could not get current user: {e}")
            current_user = "unknown_user"
        
        # Create initial ragtag configuration
        ragtag_config = {
            "authorized_users": {
                current_user: {
                    "api_key": api_key,
                    "created": datetime.now().isoformat(),
                    "permissions": ["read", "write", "admin"]
                }
            }
        }
        
        # Save configuration to shared config
        try:
            config_manager.update_ragtag_config(ragtag_config)
            MCPLogger.log("Config", f"Created new ragtag configuration in nativemessaging.json")
            MCPLogger.log("Server", f"{GRN}Generated new API key: {mask_secret_for_logging(api_key)}{NORM}")
            MCPLogger.log("Server", f"{GRN}Added authorized user: {current_user}{NORM}")
            
            # Also update all mcpServers entries with the real API key and URL
            from .shared_config import update_mcpservers_with_api_key_and_url
            if update_mcpservers_with_api_key_and_url(api_key):
                MCPLogger.log("Config", f"Updated all mcpServers entries with API key and URL")
        except Exception as e:
            MCPLogger.log("Config", f"Error creating ragtag config: {e}")
            # Keep the config in memory even if save failed
        fris._emit_message(f"* NEW Login credentials - Username: {current_user}, API Key: {mask_secret_for_logging(api_key)} (full key is in the Settings page / nativemessaging.json)")

    else:
        # Use existing ragtag configuration  
        MCPLogger.log("Config", f"Loaded existing ragtag configuration from nativemessaging.json")
        if not file_was_created:
            MCPLogger.log("Server", f"{BLU}Using existing configuration{NORM}")
        else:
            MCPLogger.log("Server", f"{GRN}Created new configuration{NORM}")
    
    # Store authorized users globally
    AUTHORIZED_USERS = ragtag_config.get("authorized_users", {})
    # Read disable_auth setting (defaults to False for security)
    DISABLE_AUTH = ragtag_config.get("disable_auth", False)
    # Read hostname-UUID auth gate (defaults to True for backward compatibility, review B4)
    ENABLE_HOSTNAME_UUID_AUTH = ragtag_config.get("enable_hostname_uuid_auth", True)
    MCPLogger.log("Config", f"Loaded {len(AUTHORIZED_USERS)} authorized users")
    if not ENABLE_HOSTNAME_UUID_AUTH:
        MCPLogger.log("Config", f"Hostname-UUID authentication is disabled (ragtag.enable_hostname_uuid_auth=false)")
    if DISABLE_AUTH:
        MCPLogger.log("Config", f"{YEL}WARNING: Authentication is DISABLED (nativemessaging.json ragtag.disable_auth=true){NORM}")
    
    # Check if current user is in authorized users, add them if not
    config_updated = False
    try:
        current_user = getpass.getuser()
        if current_user in AUTHORIZED_USERS:
            api_key = AUTHORIZED_USERS[current_user].get('api_key')
            MCPLogger.log("Server", f"{GRN}Current user: {current_user}{NORM}")
            MCPLogger.log("Server", f"{GRN}API Key: {mask_secret_for_logging(api_key)}{NORM}")
        else:
            MCPLogger.log("Server", f"{YEL}Warning: Current user '{current_user}' not found in authorized users{NORM}")
            
            # Generate new API key for current user
            new_api_key = str(uuid.uuid4())
            
            # Add current user to authorized users
            AUTHORIZED_USERS[current_user] = {
                "api_key": new_api_key,
                "created": datetime.now().isoformat(),
                "permissions": ["read", "write", "admin"]
            }
            
            # Update the ragtag configuration
            ragtag_config["authorized_users"] = AUTHORIZED_USERS
            config_updated = True
            
            MCPLogger.log("Server", f"{GRN}Added current user '{current_user}' to authorized users{NORM}")
            MCPLogger.log("Server", f"{GRN}Generated new API key: {mask_secret_for_logging(new_api_key)}{NORM}")
            
    except Exception as e:
        MCPLogger.log("Server", f"{RED}Error getting current user info: {e}{NORM}")
    
    # Save updated configuration if we added a user
    if config_updated:
        try:
            config_manager.update_ragtag_config(ragtag_config)
            MCPLogger.log("Config", f"{GRN}Updated ragtag configuration with new authorized user{NORM}")
            
            # Also update all mcpServers entries with the new API key and URL
            from .shared_config import update_mcpservers_with_api_key_and_url
            if update_mcpservers_with_api_key_and_url(new_api_key):
                MCPLogger.log("Config", f"Updated all mcpServers entries with new API key and URL")
        except Exception as e:
            MCPLogger.log("Config", f"{RED}Error saving updated ragtag config: {e}{NORM}")
    
    # For existing users, also ensure mcpServers has the correct API key
    elif not file_was_created:
        try:
            current_user = getpass.getuser()
            if current_user in AUTHORIZED_USERS:
                api_key = AUTHORIZED_USERS[current_user].get('api_key')
                # Check if any mcpServers entry needs updating
                full_config = config_manager.load_config()
                needs_update = False
                
                if "mcpServers" in full_config:
                    for server_name, server_config in full_config["mcpServers"].items():
                        if isinstance(server_config, dict) and "headers" in server_config:
                            current_auth = server_config["headers"].get("Authorization", "")
                            if current_auth == "Bearer put-your-real-key-here" or not current_auth.startswith("Bearer "):
                                needs_update = True
                                break
                
                if needs_update:
                    from .shared_config import update_mcpservers_with_api_key_and_url
                    if update_mcpservers_with_api_key_and_url(api_key):
                        MCPLogger.log("Config", f"Updated all mcpServers entries with API key and URL for existing user")
        except Exception as e:
            MCPLogger.log("Config", f"{RED}Error updating mcpServers for existing user: {e}{NORM}")
    
    return AUTHORIZED_USERS,master_dir


def get_current_user_api_key():
    """
    Get the API key for the current logged-in user.
    
    Returns:
        str: The API key for the current user, or None if not found
    """
    global AUTHORIZED_USERS
    
    try:
        current_user = getpass.getuser()
        if current_user in AUTHORIZED_USERS:
            return AUTHORIZED_USERS[current_user].get('api_key')
    except Exception as e:
        MCPLogger.log("Config", f"Error getting current user API key: {e}")
    
    return None


def get_server_version():
    """
    Get the server version from nativemessaging.json.
    
    Returns:
        str: The version string (e.g., "1.0.8") or "1.0.0" if not found
    """
    try:
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        return config.get("version", "1.0.0")
    except Exception as e:
        MCPLogger.log("Config", f"Error getting server version: {e}")
        return "1.0.0"


def handle_static_request(server):
    """Handle requests to /pages/* and /scripts/* paths - simple static file server"""
    try:
        path = server.path_without_query
        
        # Must start with /pages/ or /scripts/
        if path.startswith('/pages/'):
            static_path = path[7:]  # Remove '/pages/'
            base_dir = "pages"
        elif path.startswith('/scripts/'):
            static_path = path[9:]  # Remove '/scripts/'
            base_dir = "scripts"
        else:
            return "404 Not Found", {"Content-Type": "text/plain"}, "Not Found"
        
        # 1. Sanitize path - keep only safe characters
        sanitized_path = re.sub(r'[^a-zA-Z0-9_\-\/\.]', '', static_path)
        
        # 2. Block traversal attacks - no .. allowed
        if '..' in sanitized_path:
            MCPLogger.log("StaticServer", f"Blocked traversal attempt: {path}")
            return "403 Forbidden", {"Content-Type": "text/plain"}, "Forbidden"
        
        # 3. Get bin folder from config manager
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        bin_dir = config_manager._find_master_directory()
        
        # 4. Build full path to requested file
        static_dir = bin_dir / base_dir
        requested_file = static_dir / sanitized_path
        
        # Ensure the resolved path is still within static_dir (extra security)
        try:
            requested_file = requested_file.resolve()
            static_dir = static_dir.resolve()
            # Compare with a trailing separator so a sibling directory sharing the prefix
            # (e.g. "pages_evil" vs "pages", reachable via a symlink dropped inside the
            # served dir) cannot pass the containment check (review B5).
            if not str(requested_file).startswith(str(static_dir) + os.sep):
                MCPLogger.log("StaticServer", f"Blocked path outside {base_dir} dir: {path}")
                return "403 Forbidden", {"Content-Type": "text/plain"}, "Forbidden"
        except Exception:
            return "400 Bad Request", {"Content-Type": "text/plain"}, "Invalid path"
        
        # Check if file exists
        if not requested_file.exists() or not requested_file.is_file():
            MCPLogger.log("StaticServer", f"File not found: {requested_file}")
            return "404 Not Found", {"Content-Type": "text/plain"}, "File not found"
        
        # Determine content type
        content_type, _ = mimetypes.guess_type(str(requested_file))
        if not content_type:
            content_type = "application/octet-stream"
        
        # Add charset=utf-8 for text-based content types
        if content_type in ('text/html', 'text/css', 'application/javascript', 'text/javascript', 'application/json', 'text/plain'):
            content_type = f"{content_type}; charset=utf-8"
        
        # Read and serve the file
        try:
            # For text files, read with UTF-8 encoding to properly handle emojis
            if content_type.startswith('text/') or 'javascript' in content_type or 'json' in content_type:
                with open(requested_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # For HTML files, expand template variables using Python's string.Template
                if content_type.startswith('text/html'):
                    # Build template variables
                    enable_https = getattr(server, 'enable_https', True)
                    protocol = "https" if enable_https else "http"
                    host = getattr(server, 'host', 'unknown')
                    port = getattr(server, 'port', 0)
                    server_url = f"{protocol}://{host}:{port}/"
                    username = getattr(server, 'authenticated_user', 'unknown')
                    version = get_server_version()
                    
                    template_vars = {
                        'server_url': server_url,
                        'current_user': username,
                        'version': version,
                        'host': host,
                        'port': str(port),
                        'protocol': protocol
                    }
                    
                    # Use string.Template which uses $variable syntax (won't conflict with CSS/JS)
                    # This is Python's standard library template system designed for this exact use case
                    from string import Template
                    try:
                        template = Template(content)
                        # safe_substitute() leaves unknown variables unchanged (e.g., $unknown stays as $unknown)
                        content = template.safe_substitute(template_vars)
                        MCPLogger.log("StaticServer", f"Template expansion completed for {requested_file}")
                    except Exception as e:
                        MCPLogger.log("StaticServer", f"Warning: Template expansion error in {requested_file}: {e}")
            else:
                # Binary files (images, etc.)
                with open(requested_file, 'rb') as f:
                    content = f.read()
            
            MCPLogger.log("StaticServer", f"Serving: {requested_file} ({len(content)} bytes, {content_type})")
            
            return "200 OK", {
                "Content-Type": content_type,
                #"Content-Length": str(len(content)), # done by server
                "Cache-Control": "public, max-age=3600"
            }, content
            
        except Exception as e:
            MCPLogger.log("StaticServer", f"Error reading file {requested_file}: {e}")
            return "500 Internal Server Error", {"Content-Type": "text/plain"}, "Error reading file"
            
    except Exception as e:
        MCPLogger.log("StaticServer", f"Error handling static request {server.path_without_query}: {e}")
        return "500 Internal Server Error", {"Content-Type": "text/plain"}, "Server error"


def refuse_unsafe_control_request(server, method):
    """Refuse a /_control request unless it is a POST on an auth-protected server.

    State-changing control operations must not be reachable via GET (CSRF via <img>/
    navigation) nor on a server with no global auth handler registered (review A4).
    Returns an error response tuple to send, or None if the request may proceed.
    """
    if method != 'POST':
        return "405 Method Not Allowed", {"Allow": "POST", "Content-Type": "text/plain"}, "Control operations require POST"
    if getattr(server, 'global_auth_handler', None) is None:
        MCPLogger.log("Control Request", f"{YEL}Refusing control request: no global auth handler registered{NORM}")
        return "403 Forbidden", {"Content-Type": "text/plain"}, "Control operations refused: server has no authentication handler"
    return None


def handle_stop_request(server, method, headers, body):
    """Handle request to stop the server"""
    refusal = refuse_unsafe_control_request(server, method)
    if refusal:
        return refusal
       # Increment connection counter for tracking this control request
    server.connection_counter += 1
    connection_seq = server.connection_counter
    # Log stop request with connection sequence number
    MCPLogger.log("Control Request", "Stop server command received")
    
    # Start shutdown in a separate thread so we can return response
    threading.Thread(target=lambda: (
        time.sleep(0.1),  # Brief delay to allow response to be sent
        server.initiate_graceful_server_shutdown()
    )).start()
    
    return "200 OK", {
        "Content-Type": "text/plain"
    }, "Server stopping."

def platform_specific_chain(executable, script_path, args):
    """
    Handle platform-specific process chaining for restart.
    
    Args:
        executable: Python executable path
        script_path: Path to the script to run
        args: Command line arguments
        
    Returns:
        None - this function should not return on success
        
    On Windows: spawns a fresh process via subprocess.Popen, then exits.
    On Unix-like: Uses fork() + execv() to ensure new PID
    """
    cmd = [executable, script_path] + args
    MCPLogger.log("Restart", f"Command: {' '.join(cmd)}")
    
    # Try to run atexit handlers before execv (to close browsers, etc.)
    # Wrapped in try/except because Qt objects may throw thread-related errors
    MCPLogger.log("Restart", "Running atexit handlers to clean up resources")
    try:
        atexit._run_exitfuncs()
    except Exception as atexit_exception:
        MCPLogger.log("Restart", f"Warning: atexit handlers raised exception (continuing anyway): {atexit_exception}")
    
    if platform.system() == 'Windows':
        # Windows: spawn a fresh, independent process that inherits this console, then exit.
        # Use subprocess.Popen (which quotes argv via the documented list2cmdline rules) rather
        # than os.spawnv - the C runtime quoting behind os.spawnv is unreliable for executable
        # or script paths that contain spaces, so a restart could launch the wrong path (review A5).
        MCPLogger.log("Restart", f"Spawning new process: {subprocess.list2cmdline(cmd)}")
        try:
            # Default close_fds=True so the child does NOT inherit our listen socket (which would
            # keep the port bound); the child rebinds via the server's own EADDRINUSE retry loop.
            replacement_server_process = subprocess.Popen(cmd)
            MCPLogger.log("Restart", "New process spawned, exiting current process")
            # Give the new process a moment to start, then confirm it did not die instantly
            # before this process exits - otherwise a bad restart would silently leave no
            # server running at all (review A5). Binding is the child's job (EADDRINUSE retry).
            time.sleep(0.5)
            if replacement_server_process.poll() is not None:
                raise RuntimeError(f"replacement process exited immediately with code {replacement_server_process.returncode}")
            # Exit this process - use os._exit to skip any remaining cleanup that might hang
            os._exit(0)
        except Exception as spawn_exception:
            MCPLogger.log("Fatal", f"subprocess.Popen failed: {spawn_exception}")
            # Try os.execv as fallback (only reached if the spawn above failed)
            try:
                MCPLogger.log("Restart", "Falling back to os.execv()")
                os.execv(executable, cmd)
            except Exception as execv_exception:
                MCPLogger.log("Fatal", f"os.execv() also failed: {execv_exception}")
                # Last resort: just exit and let the user restart manually
                os._exit(1)
    else:
        # On Unix-like systems, fork then exec to get new PID
        try:
            pid = os.fork()
            if pid == 0:  # Child process
                try:
                    os.execv(executable, cmd)
                except Exception as e:
                    MCPLogger.log("Fatal", f"Child execv failed: {e}")
                    os._exit(1)  # Force exit if execv fails
            else:  # Parent process
                MCPLogger.log("Parent", f"Forked child PID {pid}, parent exiting")
                os._exit(0)  # Parent exits immediately
        except Exception as e:
            MCPLogger.log("Fatal", f"Fork failed: {e}")
            # Fallback to direct execv if fork fails
            os.execv(executable, cmd)

def handle_restart_request(server, method, headers, body):
    """Handle request to restart the server by chaining to new instance after cleanup"""
    refusal = refuse_unsafe_control_request(server, method)
    if refusal:
        return refusal
       # Increment connection counter for tracking this control request
    server.connection_counter += 1
    connection_seq = server.connection_counter
    # Log restart request
    MCPLogger.log("Control Request", "Restart server command received")
    
    # Get current process args to chain to new instance
    executable = sys.executable
    script_path = os.path.abspath(sys.argv[0])  # Use absolute path
    args = sys.argv[1:]
    
    # Filter out 'restart' command if it exists
    if 'restart' in args:
        args.remove('restart')
    
    # Log what we're about to do
    MCPLogger.log("Restart Command", f"{executable} {script_path} {' '.join(args)}")
    
    # Schedule the after-response handler
    def chain_after_response():
        # Get command details again
        executable = sys.executable
        script_path = os.path.abspath(sys.argv[0])
        args = [a for a in sys.argv[1:] if a != 'restart']
        
        # Close all connections and socket
        server.initiate_graceful_server_shutdown()
        
        # Log that we're about to chain
        MCPLogger.log("Server", f"Transferring control to: {executable} {script_path} {' '.join(args)}")
        
        # Use platform-specific chaining
        platform_specific_chain(executable, script_path, args)
    
    # Register the after-response handler
    server.after_response_handler = chain_after_response
    
    # First send success response to client
    response = f"Server restart in progress... (VERSION: {VERSION})"
    headers = {
        "Content-Type": "text/plain"
        #"Content-Length": str(len(response)) # done by server
    }
    
    # Return response - this must complete before we chain
    return "200 OK", headers, response

def touch_file(filepath):
    """Update the access and modification times of a file to current time.
    Creates the file if it doesn't exist."""
    try:
        Path(filepath).touch()
        return True
    except Exception as e:
        print(f"Error touching file {filepath}: {e}")
        return False

# trigger_cursor_reconnect moved to easy_mcp/server.py - use server.trigger_ide_reconnect() instead

def check_global_auth(server_instance):
    """
    Global authentication check for all server requests.
    This function can be called from the MCPServer to enforce auth on all endpoints.
    
    Args:
        server_instance: The MCPServer instance with request data
        
    Returns:
        tuple: (is_authenticated, error_response_tuple)
        - is_authenticated: True if auth passed or disabled, False if failed
        - error_response_tuple: (status, headers, content) for 401 response if auth failed, None if passed
    """
    global DISABLE_AUTH
    
    # If authentication is globally disabled, allow all requests
    if DISABLE_AUTH:
        return True, None
    
    # Allow certain requests without authentication (CORS preflight, etc.)
    method = getattr(server_instance, 'method', '')
    path = getattr(server_instance, 'path_without_query', '')
    
    # Always allow OPTIONS requests (CORS preflight)
    if method == 'OPTIONS':
        return True, None
    
    # Allow favicon requests without auth (browsers make these automatically)
    if path == '/favicon.ico':
        return True, None
    
    # Extract authentication data from server instance
    auth_header = getattr(server_instance, 'headers', {}).get('Authorization') or getattr(server_instance, 'headers', {}).get('authorization')
    client_address = getattr(server_instance, 'current_client_address', None)
    
    # Extract URL parameters for authentication
    url_user = None
    url_api_key = None
    if hasattr(server_instance, 'query_params'):
        # Get user parameter (check both 'user' and 'username')
        user_params = server_instance.query_params.get('user', []) + server_instance.query_params.get('username', [])
        if user_params:
            url_user = user_params[0]
        
        # Get API key parameter
        api_key_params = server_instance.query_params.get('RAGTAG_API_KEY', [])
        if api_key_params:
            url_api_key = api_key_params[0]
    
    # Get host header for hostname-based UUID authentication
    host_header = getattr(server_instance, 'headers', {}).get('host') or getattr(server_instance, 'headers', {}).get('Host')
    
    # Validate authentication
    is_valid, username = validate_auth(auth_header, url_user, url_api_key, client_address, host_header)
    
    # Allow OAuth discovery endpoint and oauth calls without auth (required for OAuth flow)
    # But only if OAuth is enabled in config. Uses the same canonical path set that
    # handle_default_request serves, so no allowed-but-unserved variant exists (review A1).
    if path in OAUTH_DISCOVERY_PATHS or path.startswith('/oauth2/'):
        if is_valid:
            oauth_enabled = False # Hide the fact we can do OAuth when it's not needed; so this works:-
            # codex mcp add --url https://9e3c0795-4733-4f54-b134-643918bd4621-127-0-0-1.local.aurafriday.com:31173/sse rog
        else:
            # Check if OAuth is enabled in config
            from .shared_config import get_config_manager
            config_manager = get_config_manager()
            config = config_manager.load_config()
            oauth_config = config.get("settings", [{}])[0].get("oauth", {})
            oauth_enabled = oauth_config.get("enabled", False)

        #return True, None
        if not oauth_enabled: # disabled, or, Hide the fact we can do OAuth when it's not needed; so this works:-
            # codex mcp add --url https://9e3c0795-4733-4f54-b134-643918bd4621-127-0-0-1.local.aurafriday.com:31173/sse rog
            return False, ("404 Not Found", { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" }, "Not Found")
        else:
            # OAuth flow is allowed unauthenticated, but this request has NOT authenticated a
            # user. Mark it so the default handler refuses to fall through to the homepage
            # (which embeds an API key) for any path it does not explicitly serve (review A1).
            server_instance.authenticated_user = None
            return True, None

    if not is_valid:
        # Return 401 Unauthorized response
        error_response = ("401 Unauthorized", {
            "WWW-Authenticate": 'Basic realm="Aura Friday mcp-link server"',
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store"
        }, "Access Denied")
        return False, error_response
    
    # Store authenticated username in server instance for later use
    server_instance.authenticated_user = username
    return True, None


def validate_auth(auth_header=None, url_user=None, url_api_key=None, client_address=None, host_header=None):
    """
    Validate authentication credentials against authorized users.
    Supports Basic Auth, URL parameters, and hostname UUID.
    
    Args:
        auth_header: The Authorization header value (e.g., "Basic dXNlcjpwYXNz")
        url_user: Username from URL parameters
        url_api_key: API key from URL parameters (RAGTAG_API_KEY)
        client_address: Client address tuple (ip, port) for logging
        host_header: Host header value for hostname-based UUID authentication
        
    Returns:
        tuple: (is_valid, username) where is_valid is boolean and username is string
    """
    global AUTHORIZED_USERS
    
    client_ip = f"{client_address[0]}:{client_address[1]}" if client_address else "unknown"
    auth_method = "Unknown"  # Initialize auth_method for scope
    username = None
    password = None
    
    # Check for URL parameter authentication first
    if url_user and url_api_key:
        username = url_user
        password = url_api_key
        auth_method = "URL parameters"
        MCPLogger.log("Auth", f"Attempting URL parameter authentication for user: {username} from {client_ip}")
    elif auth_header and auth_header.startswith('Basic '):
        try:
            # Extract credentials from Basic auth header
            credentials = auth_header[6:]  # Remove "Basic " prefix
            decoded_credentials = base64.b64decode(credentials).decode('utf-8')
            username, password = decoded_credentials.split(':', 1)
            auth_method = "Basic Auth"
            
            # If username is empty, treat as no auth and fall through to hostname UUID
            if not username:
                username = None
                password = None
            #else:
            #    MCPLogger.log("Auth", f"Attempting Basic Auth for user: {username} from {client_ip}")
        except Exception as e:
            MCPLogger.log("Auth", f"{YEL}Error parsing Basic Auth from {client_ip}: {e}{NORM}")
            return False, None
    elif auth_header and auth_header.startswith('Bearer '):
        try:
            # Extract token from Bearer auth header
            token = auth_header[7:]  # Remove "Bearer " prefix
            
            # For Bearer auth, we need to find which user this token belongs to
            # First check OAuth access tokens, then fall back to authorized users API keys
            username = None
            password = token
            
            # Check OAuth access tokens first
            try:
                from .shared_config import get_config_manager, SharedConfigManager
                config_manager = get_config_manager()
                full_config = config_manager.load_config()
                oauth_data = SharedConfigManager.ensure_settings_section(full_config, 'oauth')
                
                if token in oauth_data.get('access_tokens', {}):
                    token_data = oauth_data['access_tokens'][token]
                    
                    # Check if token is expired
                    import time
                    if token_data['expires_at'] > time.time():
                        # Valid OAuth token - get client info
                        client_id = token_data['client_id']
                        if client_id in oauth_data.get('clients', {}):
                            client_info = oauth_data['clients'][client_id]
                            username = client_info.get('client_name', client_id)
                            password = token
                            auth_method = "Bearer OAuth"
                            MCPLogger.log("Auth", f"Attempting {auth_method} for OAuth client: {username} from {client_ip}")
                    else:
                        MCPLogger.log("Auth", f"{YEL}OAuth Bearer token expired from {client_ip}{NORM}")
                        return False, None
            except Exception as e:
                MCPLogger.log("Auth", f"Error checking OAuth tokens: {e}")
                # Continue to check regular authorized users
            
            # If not found in OAuth tokens, check authorized users API keys
            # (constant-time comparison so the lookup cannot leak key prefixes via timing, review D6)
            if not username:
                for user, user_config in AUTHORIZED_USERS.items():
                    if api_key_matches_constant_time(token, user_config.get('api_key')):
                        username = user
                        break
                
                auth_method = "Bearer Auth"
                if not username:
                    # Try to decode as base64 in case it's a Basic auth token in Bearer format
                    try:
                        decoded_credentials = base64.b64decode(token).decode('utf-8')
                        username, password = decoded_credentials.split(':', 1)
                        auth_method = "Bearer Basic Auth"
                    except Exception:
                        # Not base64 encoded - just a plain token that doesn't match any user.
                        # Mask the offered token: it may be a typo'd real key and this log is
                        # world-shared (review B3).
                        MCPLogger.log("Auth", f"{YEL}Bearer token '{mask_secret_for_logging(token)}' not found in authorized users or OAuth tokens from {client_ip}{NORM}")
                        return False, None
                
                if username:
                    MCPLogger.log("Auth", f"Attempting {auth_method} for user: {username} from {client_ip}")
        except Exception as e:
            # Mask the header value - it can contain a live (mistyped/expired) token (review B3).
            MCPLogger.log("Auth", f"{YEL}Error parsing Bearer Auth '{mask_secret_for_logging(auth_header)}' from {client_ip}: {e}{NORM}")
            return False, None
    
    # If no username extracted from auth methods above, try hostname-based UUID authentication
    # as a fallback. This carries the API key as the first DNS label of the Host header, so the
    # credential travels through DNS queries/SNI/logs; it is therefore gated behind a config
    # flag (ragtag.enable_hostname_uuid_auth, default True for backward compatibility) (review B4).
    if not username and ENABLE_HOSTNAME_UUID_AUTH:
        if host_header:
            try:
                # Look for UUID pattern at start of hostname: {uuid}-{rest-of-domain}
                uuid_pattern = r'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-(.+)$'
                match = re.match(uuid_pattern, host_header, re.IGNORECASE)
                
                if match:
                    extracted_uuid = match.group(1)
                    original_domain = match.group(2)
                    
                    # Mask the extracted value and omit the host label carrying it - the offered
                    # credential must not land in the world-shared logfile (review B3).
                    MCPLogger.log("Auth", f"Found UUID '{mask_secret_for_logging(extracted_uuid)}' in hostname '...-{original_domain}' from {client_ip}")
                    
                    # Search through all authorized users to find a matching API key (same as Bearer
                    # auth; constant-time comparison, review D6)
                    for user, user_config in AUTHORIZED_USERS.items():
                        if api_key_matches_constant_time(extracted_uuid, user_config.get('api_key')):
                            username = user
                            password = extracted_uuid
                            auth_method = "Hostname UUID"
                            MCPLogger.log("Auth", f"Attempting {auth_method} for user: {username} from {client_ip}")
                            break
                    
                    # If no match found, fail with clear message (masked - review B3)
                    if not username:
                        MCPLogger.log("Auth", f"{YEL}Hostname UUID '{mask_secret_for_logging(extracted_uuid)}' not found in authorized users from {client_ip}{NORM}")
                        return False, None
                else:
                    MCPLogger.log("Auth", f"{YEL}No valid authentication provided (Basic, Bearer, URL parameters, or hostname UUID) from {client_ip}{NORM}")
                    return False, None
            except Exception as e:
                MCPLogger.log("Auth", f"{YEL}Error parsing hostname for UUID from {client_ip}: {e}{NORM}")
                return False, None
        else:
            MCPLogger.log("Auth", f"{YEL}No valid authentication provided (Basic, Bearer, URL parameters, or hostname UUID) from {client_ip}{NORM}")
            return False, None
    
    # If still no username after all attempts, fail
    if not username:
        MCPLogger.log("Auth", f"{YEL}No valid authentication provided from {client_ip}{NORM}")
        return False, None
    
    try:
        
        #MCPLogger.log("Auth", f"Auth attempt for user: '{username}' with password: '{password[:8]}...'")
        
        # For OAuth tokens, we've already validated them above and set username
        if auth_method == "Bearer OAuth":
            # OAuth token was already validated (not expired, client exists)
            MCPLogger.log("Auth", f"{GRN}Successful {auth_method} authentication for OAuth client: {username} from {client_ip}{NORM}")
            return True, username
        
        # Check if user exists in authorized_users (for non-OAuth auth methods)
        if username in AUTHORIZED_USERS:
            user_config = AUTHORIZED_USERS[username]
            expected_api_key = user_config.get('api_key')
            #MCPLogger.log("Auth", f"Expected API key for '{username}': '{expected_api_key[:8]}...'")
            
            # Check if the password matches the user's API key.
            # Constant-time comparison to avoid leaking the key via timing (review D6).
            if api_key_matches_constant_time(password, expected_api_key):
                MCPLogger.log("Auth", f"{GRN}Successful {auth_method} authentication for user: {username} from {client_ip}{NORM}")
                return True, username
            else:
                # Do NOT log the offered key value - it lands in the world-shared logfile (review B3).
                MCPLogger.log("Auth", f"{YEL}Password/API key mismatch for user '{username}' via {auth_method} from {client_ip}{NORM}")
        else:
            MCPLogger.log("Auth", f"{YEL}User '{username}' not found in authorized users via {auth_method} from {client_ip}. Available users: {list(AUTHORIZED_USERS.keys())}{NORM}")
        
        MCPLogger.log("Auth", f"{YEL}Failed {auth_method} authentication attempt for user: {username} from {client_ip}{NORM}")
        return False, username
        
    except Exception as e:
        MCPLogger.log("Auth", f"{YEL}Error validating auth from {client_ip}: {e}{NORM}")
        return False, None

def _json_response(status, payload, cors_headers=None, extra_headers=None, indent=None):
    """Build a (status, headers, body) JSON response tuple (review D3).

    Centralizes the Content-Type + optional CORS/extra header merge that the Settings,
    Users, Status and Tools API handlers previously repeated at every return site.
    """
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if cors_headers:
        headers.update(cors_headers)
    return status, headers, json.dumps(payload, indent=indent)


def handle_settings_api_request(server):
    """
    Handle Settings API requests for frontend configuration management.
    
    Endpoints:
    - GET /api/settings          -> Returns entire settings[0] object
    - GET /api/settings/{key}    -> Returns specific key value (creates {} if missing)
    - PUT /api/settings/{key}    -> Sets specific key value from JSON body
    
    Authentication:
    - Already authenticated by global auth (check_global_auth)
    - GET requires "read" permission in ragtag.authorized_users
    - PUT requires "write" permission in ragtag.authorized_users
    
    JavaScript Usage Examples:
    
    // Get entire settings[0] configuration
    fetch('/api/settings', {
        credentials: 'include'
    }).then(r => r.json()).then(config => {
        console.log('Full config:', config);
    });
    
    // Get specific key (auto-creates {} if missing)
    fetch('/api/settings/autoUpdateEnabled', {
        credentials: 'include'
    }).then(r => r.json()).then(value => {
        console.log('autoUpdateEnabled:', value);
    });
    
    // Set specific key value
    fetch('/api/settings/autoUpdateEnabled', {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json'
        },
        credentials: 'include',
        body: JSON.stringify(false)
    }).then(r => r.json()).then(result => {
        console.log('Update result:', result);
    });
    
    // Set complex nested structure
    fetch('/api/settings/server', {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json'
        },
        credentials: 'include',
        body: JSON.stringify({
            host: "127-0-0-1.local.aurafriday.com",
            port: 31173,
            int: "R13",
            n: 2
        })
    }).then(r => r.json()).then(result => {
        console.log('Server config updated:', result);
    });
    """
    try:
        method = getattr(server, 'method', 'GET')
        path = server.path_without_query
        username = getattr(server, 'authenticated_user', 'unknown')
        client_address = getattr(server, 'current_client_address', None)
        client_ip = f"{client_address[0]}:{client_address[1]}" if client_address else "unknown"
        
        # Get CORS headers using server's standardized method
        request_headers = getattr(server, 'headers', {})
        requested_headers = request_headers.get('Access-Control-Request-Headers')
        cors_headers = server._get_cors_headers(request_headers, requested_headers)
        
        # Handle OPTIONS preflight requests
        if method == "OPTIONS":
            MCPLogger.log("Settings API", f"OPTIONS preflight request from {client_ip}")
            return "204 No Content", cors_headers, ""
        
        # Parse the path to extract the key (if any)
        path_parts = path.split('/')  # ['', 'api', 'settings', 'key'] or ['', 'api', 'settings']
        settings_key = None
        if len(path_parts) > 3 and path_parts[3]:
            settings_key = path_parts[3]
            # Validate key name (alphanumeric + underscore + period only)
            if not settings_key.replace('_', '').replace('.', '').isalnum():
                return _json_response("400 Bad Request", {"error": f"Invalid key name. Only alphanumeric characters, underscores, and periods allowed:{path_parts[3]}"}, cors_headers=cors_headers)
        
        # Load config and check permissions
        from .shared_config import SharedConfigManager, get_config_manager
        
        config_manager = get_config_manager()
        full_config = config_manager.load_config()
        
        MCPLogger.log("Settings API", f"Loaded config with {len(full_config.get('settings', []))} settings sections")
        
        # Get authorized users from settings[0].ragtag.authorized_users
        ragtag_config = SharedConfigManager.ensure_settings_section(full_config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        # Find user info by username (already authenticated by global auth)
        user_info = authorized_users.get(username)
        if not user_info:
            MCPLogger.log("Settings API", f"ERROR: User {username} not found in authorized_users from {client_ip}")
            MCPLogger.log("Settings API", f"Available users: {list(authorized_users.keys())}")
            return _json_response("403 Forbidden", {"error": "User not found in authorized users"}, cors_headers=cors_headers)
        
        user_permissions = user_info.get('permissions', [])
        
        # Check permissions based on method
        required_permission = "read" if method == "GET" else "write"
        if required_permission not in user_permissions:
            MCPLogger.log("Settings API", f"ERROR: User {username} lacks {required_permission} permission for {method} {path}")
            MCPLogger.log("Settings API", f"User permissions: {user_permissions}")
            return _json_response("403 Forbidden", {"error": f"Insufficient permissions. {required_permission.title()} permission required."}, cors_headers=cors_headers)
        
        MCPLogger.log("Settings API", f"User {username} authorized for {method} {path}")
        
        # Handle GET requests
        if method == "GET":
            if settings_key:
                # Special case: 'configs' key returns the extension-compatible structure
                if settings_key == "configs":
                    # Return the structure expected by the extension JavaScript:
                    # { settings: { value: [...] } }
                    if "settings" not in full_config or not isinstance(full_config["settings"], list):
                        MCPLogger.log("Settings API", f"Warning: settings array missing, creating empty structure")
                        full_config["settings"] = [{}]
                    
                    response_data = {
                        "settings": {
                            "value": full_config["settings"]
                        }
                    }
                    MCPLogger.log("Settings API", f"Returning configs structure with {len(full_config['settings'])} settings sections")
                # Special case: 'settings.X' key is trying to access a nested setting value
                elif settings_key.startswith("settings."):
                    # Extract the actual setting name after "settings."
                    actual_key = settings_key[9:]  # Remove "settings." prefix
                    
                    # Use get_settings_value to handle dot-notation
                    response_data = SharedConfigManager.get_settings_value(full_config, actual_key, default=None)
                    
                    if response_data is not None:
                        MCPLogger.log("Settings API", f"Found nested key 'settings.{actual_key}' -> settings[0]['{actual_key}']")
                    else:
                        MCPLogger.log("Settings API", f"Nested key 'settings.{actual_key}' not found in settings[0], returning null")
                else:
                    # Use get_settings_value to handle dot-notation (e.g., "server.port")
                    # Try to get the value first
                    response_data = SharedConfigManager.get_settings_value(full_config, settings_key, default="__NOT_FOUND__")
                    
                    if response_data == "__NOT_FOUND__":
                        # Key doesn't exist - create it as empty dict using ensure_settings_section
                        MCPLogger.log("Settings API", f"Key '{settings_key}' not found in settings[0], creating empty object")
                        key_value = SharedConfigManager.ensure_settings_section(full_config, settings_key)
                        response_data = key_value
                    else:
                        MCPLogger.log("Settings API", f"Found existing key '{settings_key}' in settings[0]")
            else:
                # Get entire settings[0] object
                if "settings" not in full_config or not isinstance(full_config["settings"], list):
                    MCPLogger.log("Settings API", f"Warning: settings array missing, creating empty structure")
                    full_config["settings"] = [{}]
                if not full_config["settings"]:
                    MCPLogger.log("Settings API", f"Warning: settings array empty, creating empty object")
                    full_config["settings"] = [{}]
                response_data = full_config["settings"][0]
                MCPLogger.log("Settings API", f"Returning entire settings[0] with keys: {list(response_data.keys())}")
            
            MCPLogger.log("Settings API", f"GET {path} -> {len(json.dumps(response_data))} bytes")
            return _json_response("200 OK", response_data, cors_headers=cors_headers, indent=2)
        
        # Handle PUT requests
        elif method == "PUT":
            if not settings_key:
                MCPLogger.log("Settings API", f"ERROR: PUT without settings key from {client_ip}")
                return _json_response("400 Bad Request", {"error": "PUT requires a settings key. Use: PUT /api/settings/{key}"}, cors_headers=cors_headers)
            
            # Block modification of protected keys
            protected_keys = ['_internal']  # Add more as needed
            if settings_key in protected_keys:
                MCPLogger.log("Settings API", f"ERROR: Attempt to modify protected key '{settings_key}' from {client_ip}")
                return _json_response("403 Forbidden", {"error": f"Key \"{settings_key}\" is protected and cannot be modified"}, cors_headers=cors_headers)
            
            # Get request body
            body = getattr(server, 'oauth_body', '')  # Body is stored in oauth_body by server.py
            
            # Parse JSON body
            if not body.strip():
                MCPLogger.log("Settings API", f"ERROR: Empty body in PUT request from {client_ip}")
                return _json_response("400 Bad Request", {"error": "Empty request body. JSON value required."}, cors_headers=cors_headers)
            
            try:
                new_value = json.loads(body)
            except json.JSONDecodeError as e:
                MCPLogger.log("Settings API", f"ERROR: Invalid JSON in PUT request from {client_ip}: {e}")
                return _json_response("400 Bad Request", {"error": f"Invalid JSON: {str(e)}"}, cors_headers=cors_headers)
            
            # Special case: 'settings' key with {id, value} structure is an extension update command
            if settings_key == "settings" and isinstance(new_value, dict) and "id" in new_value and "value" in new_value:
                # This is the extension's update protocol: {id: "settingName", value: newValue}
                # Update settings[0][id] instead of settings[0]["settings"]
                actual_setting_id = new_value["id"]
                actual_setting_value = new_value["value"]
                
                MCPLogger.log("Settings API", f"Recognized extension update protocol for setting '{actual_setting_id}'")
                
                # Use set_settings_value to handle dot-notation in the id (e.g., "server.port")
                SharedConfigManager.set_settings_value(full_config, actual_setting_id, actual_setting_value)
                
                # Log what was actually set (show nested path for dot-notation)
                if '.' in actual_setting_id:
                    keys = actual_setting_id.split('.')
                    nested_path = "settings[0]"
                    for key in keys:
                        nested_path += f"['{key}']"
                    MCPLogger.log("Settings API", f"Set {nested_path} = {actual_setting_value}")
                else:
                    MCPLogger.log("Settings API", f"Set settings[0]['{actual_setting_id}'] = {actual_setting_value}")
            else:
                # Normal case: directly set the key to the value
                # Use set_settings_value to handle dot-notation (e.g., "server.port")
                SharedConfigManager.set_settings_value(full_config, settings_key, new_value)
                MCPLogger.log("Settings API", f"Updated settings[0]['{settings_key}'] = {new_value}")
            
            # Save the updated config
            success = config_manager.save_config(full_config)
            
            if success:
                MCPLogger.log("Settings API", f"PUT {path} -> Updated {settings_key}")
                return _json_response("200 OK", {"success": True, "message": "Settings updated successfully"}, cors_headers=cors_headers)
            else:
                MCPLogger.log("Settings API", f"ERROR: Failed to save config after PUT {path}")
                return _json_response("500 Internal Server Error", {"error": "Failed to save settings"}, cors_headers=cors_headers)
        
        else:
            # Method not allowed
            MCPLogger.log("Settings API", f"ERROR: Unsupported method {method} for {path} from {client_ip}")
            return _json_response("405 Method Not Allowed", {"error": "Only GET and PUT methods are supported"}, cors_headers=cors_headers, extra_headers={"Allow": "GET, PUT"})
            
    except Exception as e:
        MCPLogger.log("Settings API", f"ERROR: Exception in handler: {e}")
        import traceback
        MCPLogger.log("Settings API", f"ERROR: Traceback:\n{traceback.format_exc()}")
        # CORS headers even for errors - get from server if available
        try:
            request_headers = getattr(server, 'headers', {})
            requested_headers = request_headers.get('Access-Control-Request-Headers')
            cors_headers = server._get_cors_headers(request_headers, requested_headers)
        except:
            # If we cannot compute proper CORS headers, send NONE rather than a permissive
            # "Access-Control-Allow-Origin: null" + credentials, which any sandboxed iframe /
            # file:// origin would match (review B2). An error body simply won't be readable
            # cross-origin, which is the safe outcome.
            cors_headers = {}
        return _json_response("500 Internal Server Error", {"error": "Internal server error", "details": str(e)}, cors_headers=cors_headers)


def handle_settings_request(server):
    """Redirect the legacy /settings path to the static popover page (review A9).

    The old implementation re-read popover.html off master_dir and did its own naive
    template replacement, duplicating handle_static_request; now the static handler
    serves the page (with proper template expansion). The function itself is retained
    because the ragtag package __init__ exports this name.
    """
    return "302 Found", {
        "Location": "/pages/popover.html",
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store"
    }, "Redirecting to /pages/popover.html"


def handle_oauth2_request(server):
    """
    Handle OAuth 2.0 endpoint requests
    
    Routes requests to appropriate OAuth2Handler methods based on path.
    This is called from handle_default_request when path starts with /oauth2/
    """
    from .shared_config import get_config_manager
    from .oauth2_handler import OAuth2Handler
    
    # Initialize OAuth handler
    config_manager = get_config_manager()
    oauth_handler = OAuth2Handler(config_manager)
    
    path = server.path_without_query
    method = getattr(server, 'method', 'GET')
    headers = getattr(server, 'headers', {})
    query_params = getattr(server, 'query_params', {})
    
    # Get body data - it should be stored as oauth_body attribute by handle_default_request
    body = getattr(server, 'oauth_body', "")
    
    # Route to appropriate handler based on path
    try:
        if path == "/oauth2/register" and method == "POST":
            # Dynamic Client Registration
            status, response_headers, content = oauth_handler.handle_client_registration(body)
        
        elif path == "/oauth2/authorize" and method == "GET":
            # Authorization endpoint - show consent page
            status, response_headers, content = oauth_handler.handle_authorization_request(query_params)
        
        elif path == "/oauth2/authorize_approve" and method == "POST":
            # User approved/denied authorization
            status, response_headers, content = oauth_handler.handle_authorization_approval(body)
        
        elif path == "/oauth2/token" and method == "POST":
            # Token endpoint - exchange code for tokens or refresh
            status, response_headers, content = oauth_handler.handle_token_request(body, headers)
        
        elif path == "/oauth2/introspect" and method == "POST":
            # Token introspection
            status, response_headers, content = oauth_handler.handle_introspection_request(body)
        
        elif path == "/oauth2/revoke" and method == "POST":
            # Token revocation
            status, response_headers, content = oauth_handler.handle_revocation_request(body)
        
        else:
            # Unknown OAuth endpoint (built via the shared JSON helper, review D3)
            status, response_headers, content = _json_response("404 Not Found", {
                "error": "not_found",
                "error_description": f"OAuth endpoint not found: {method} {path}"
            })
        
        MCPLogger.log("OAuth2", f"{method} {path} -> {status}")
        
        # Merge response headers with content-type if not already set
        if "Content-Type" not in response_headers:
            response_headers["Content-Type"] = "text/html; charset=utf-8"
        
        return status, response_headers, content
        
    except Exception as e:
        MCPLogger.log("Error", f"OAuth2 handler failed: {e}")
        import traceback
        MCPLogger.log("Error", traceback.format_exc())
        return _json_response("500 Internal Server Error", {"error": "Internal server error", "details": str(e)})


def handle_status_api_request(server):
    """
    Get server status information.
    
    Returns:
        {
            "status": "running",
            "url": "https://...",
            "clients": 2,
            "local_tools": 5,
            "remote_tools": 1,
            "version": "1.2.30"
        }
    """
    try:
        # Get client count from active sessions
        client_count = len(server.active_sessions)
        
        # Get tool counts
        remote_tool_count = 0
        local_tool_count = 0
        try:
            from ragtag.tools import remote
            remote_tool_count = len(remote.registered_tools)
            from ragtag.tools import local
            local_tool_count = len(local.get_dynamic_tools())
        except ImportError:
            pass
            
        # Determine protocol and URL
        enable_https = server.enable_https
        protocol = "https" if enable_https else "http"
        host = server.host
        server_url = f"{protocol}://{host}:{server.port}/"
        
        response_data = {
            "status": "running",
            "url": server_url,
            "clients": client_count,
            "local_tools": local_tool_count,
            "remote_tools": remote_tool_count,
            "version": get_server_version()
        }
        
        return _json_response("200 OK", response_data)
        
    except Exception as e:
        MCPLogger.log("Status API", f"ERROR: {e}")
        import traceback
        MCPLogger.log("Status API", f"Traceback:\n{traceback.format_exc()}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_tools_api_request(server):
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        
        config_manager = get_config_manager()
        config = config_manager.load_config()
        tool_visibility = SharedConfigManager.get_settings_value(config, 'tool_visibility', default={})
        
        all_registered_tool_names = list(server.tool_handlers.keys())
        
        tool_entries = []
        
        for tool_name in all_registered_tool_names:
            enabled_flag = tool_visibility.get(tool_name, 1)
            if not isinstance(enabled_flag, int):
                enabled_flag = 1
            tool_entries.append({
                "name": tool_name,
                "enabled": enabled_flag,
                "currently_registered": 1
            })
        
        for tool_name_from_config, enabled_flag in tool_visibility.items():
            if tool_name_from_config not in server.tool_handlers:
                if not isinstance(enabled_flag, int):
                    enabled_flag = 1
                tool_entries.append({
                    "name": tool_name_from_config,
                    "enabled": enabled_flag,
                    "currently_registered": 0
                })
        
        tool_entries.sort(key=lambda entry: (1 - entry["currently_registered"], entry["name"].lower()))
        
        response_data = {"tools": tool_entries}
        
        request_headers = getattr(server, 'headers', {})
        requested_headers = request_headers.get('Access-Control-Request-Headers')
        cors_headers = server._get_cors_headers(request_headers, requested_headers)
        return _json_response("200 OK", response_data, cors_headers=cors_headers)
        
    except Exception as e:
        MCPLogger.log("Tools API", f"ERROR: {e}")
        import traceback
        MCPLogger.log("Tools API", f"Traceback:\n{traceback.format_exc()}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_notify_tools_changed_request(server):
    try:
        request_headers = getattr(server, 'headers', {})
        requested_headers = request_headers.get('Access-Control-Request-Headers')
        cors_headers = server._get_cors_headers(request_headers, requested_headers)
        
        # Debounced (was a direct send) so a web-UI save that ALSO fires the config
        # callback (sync_disabled_tools_from_config) collapses into ONE frame -- see
        # doc/tools_list_changed_notification_gap_analysis_and_implementation_plan.md step 5.
        server.schedule_tools_list_changed_notification_after_collapse_window()
        
        server.trigger_ide_reconnect(0)
        
        MCPLogger.log("ToolVisibility", "Notified clients of tool list change (list_changed + IDE touch)")
        return _json_response("200 OK", {"success": True, "message": "Clients notified of tool list change"}, cors_headers=cors_headers)
        
    except Exception as e:
        MCPLogger.log("ToolVisibility", f"ERROR in notify_tools_changed: {e}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_users_list_request(server):
    """
    List all users (excluding _internal).
    
    Returns:
        {
            "users": ["cnd", "aura", ...],  # Alphabetically sorted
            "current_os_user": "cnd",       # OS username (normalized)
            "default_user": "cnd"           # Which user to show by default
        }
    """
    try:
        import getpass
        import re
        from .shared_config import get_config_manager, SharedConfigManager
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get authorized users
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        # Filter out _internal and sort alphabetically
        user_list = sorted([u for u in authorized_users.keys() if u != '_internal'])
        
        # Get OS username and normalize it
        os_username = getpass.getuser()
        # Normalize: replace invalid chars with underscore (allows alphanumeric, _, -, .)
        normalized_os_username = re.sub(r'[^a-zA-Z0-9_.-]', '_', os_username)
        
        # Determine default user (normalized OS username if exists, else first in list)
        default_user = normalized_os_username if normalized_os_username in user_list else (user_list[0] if user_list else None)
        
        response_data = {
            "users": user_list,
            "current_os_user": normalized_os_username,
            "default_user": default_user
        }
        
        MCPLogger.log("Users API", f"GET /api/users -> {len(user_list)} users")
        return _json_response("200 OK", response_data, indent=2)
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        import traceback
        MCPLogger.log("Users API", f"Traceback:\n{traceback.format_exc()}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_user_details_request(server, username):
    """
    Get details for specific user.
    
    Returns:
        {
            "username": "cnd",
            "api_key": "314ae0a1-...",
            "created": "2025-09-13T11:23:13.811008",
            "permissions": ["read", "write", "admin"]
        }
    """
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get user
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        if username not in authorized_users:
            return _json_response("404 Not Found", {"error": f"User '{username}' not found"})
        
        user_info = authorized_users[username]
        
        response_data = {
            "username": username,
            "api_key": user_info.get('api_key', ''),
            "created": user_info.get('created', ''),
            "permissions": user_info.get('permissions', ["read", "write", "admin"])
        }
        
        MCPLogger.log("Users API", f"GET /api/users/{username}")
        # Contains the user's API key; keep it out of shared caches (review A2/D1).
        return _json_response("200 OK", response_data, extra_headers={"Cache-Control": "no-store"}, indent=2)
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_user_create_request(server):
    """
    Create new user.
    
    Body: {"username": "newuser"}
    
    Returns:
        {
            "username": "newuser",
            "api_key": "newly-generated-uuid",
            "created": "2025-11-15T...",
            "permissions": ["read", "write", "admin"]
        }
    """
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        from datetime import datetime
        import re
        
        # Parse request body
        body = getattr(server, 'oauth_body', '')
        if not body.strip():
            return _json_response("400 Bad Request", {"error": "Empty request body"})
        
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            return _json_response("400 Bad Request", {"error": f"Invalid JSON: {str(e)}"})
        
        username = data.get('username', '').strip()
        if not username:
            return _json_response("400 Bad Request", {"error": "Username required"})
        
        # Validate username (alphanumeric + underscore + hyphen + dot, must start with alphanumeric)
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', username):
            return _json_response("400 Bad Request", {"error": "Username must start with a letter or number and contain only letters, numbers, underscores, hyphens, and dots"})
        
        # Prevent creating _internal
        if username == '_internal':
            return _json_response("403 Forbidden", {"error": "Cannot create user '_internal'"})
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Check if user already exists
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        if username in authorized_users:
            return _json_response("409 Conflict", {"error": f"User '{username}' already exists"})
        
        # Create new user
        new_api_key = str(uuid.uuid4())
        new_user = {
            "api_key": new_api_key,
            "created": datetime.utcnow().isoformat(),
            "permissions": ["read", "write", "admin"]
        }
        
        authorized_users[username] = new_user
        
        # Save config
        success = config_manager.save_config(config)
        
        if not success:
            return _json_response("500 Internal Server Error", {"error": "Failed to save config"})
        
        response_data = {
            "username": username,
            "api_key": new_api_key,
            "created": new_user['created'],
            "permissions": new_user['permissions']
        }
        
        MCPLogger.log("Users API", f"POST /api/users -> Created user '{username}'")
        # Contains the new user's API key; keep it out of shared caches (review A2/D1).
        return _json_response("201 Created", response_data, extra_headers={"Cache-Control": "no-store"}, indent=2)
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        import traceback
        MCPLogger.log("Users API", f"Traceback:\n{traceback.format_exc()}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_user_delete_request(server, username):
    """
    Delete user.
    
    Returns:
        {"success": true, "message": "User 'username' deleted"}
    """
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        
        # Prevent deleting _internal
        if username == '_internal':
            return _json_response("403 Forbidden", {"error": "Cannot delete '_internal' user"})
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get user
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        if username not in authorized_users:
            return _json_response("404 Not Found", {"error": f"User '{username}' not found"})
        
        # Prevent deleting last non-internal user
        non_internal_users = [u for u in authorized_users.keys() if u != '_internal']
        if len(non_internal_users) <= 1:
            return _json_response("409 Conflict", {"error": "Cannot delete the last user. At least one non-internal user must exist."})
        
        # Delete user
        del authorized_users[username]
        
        # Save config
        success = config_manager.save_config(config)
        
        if not success:
            return _json_response("500 Internal Server Error", {"error": "Failed to save config"})
        
        MCPLogger.log("Users API", f"DELETE /api/users/{username} -> Deleted")
        return _json_response("200 OK", {"success": True, "message": f"User '{username}' deleted"})
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_user_regenerate_key_request(server, username):
    """
    Regenerate API key for user.
    
    Returns:
        {
            "username": "cnd",
            "api_key": "new-uuid",
            "created": "original-timestamp",
            "permissions": ["read", "write", "admin"]
        }
    """
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        
        # Prevent regenerating _internal (it's ephemeral anyway)
        if username == '_internal':
            return _json_response("403 Forbidden", {"error": "Cannot regenerate '_internal' key (it's ephemeral)"})
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get user
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        if username not in authorized_users:
            return _json_response("404 Not Found", {"error": f"User '{username}' not found"})
        
        # Regenerate API key
        user_info = authorized_users[username]
        new_api_key = str(uuid.uuid4())
        user_info['api_key'] = new_api_key
        
        # Save config
        success = config_manager.save_config(config)
        
        if not success:
            return _json_response("500 Internal Server Error", {"error": "Failed to save config"})
        
        response_data = {
            "username": username,
            "api_key": new_api_key,
            "created": user_info.get('created', ''),
            "permissions": user_info.get('permissions', ["read", "write", "admin"])
        }
        
        MCPLogger.log("Users API", f"POST /api/users/{username}/regenerate_key -> New key generated")
        # Contains the regenerated API key; keep it out of shared caches (review A2/D1).
        return _json_response("200 OK", response_data, extra_headers={"Cache-Control": "no-store"}, indent=2)
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_user_mcp_json_request(server, username):
    """
    Get mcpServers JSON for specific user.
    
    Synthesizes mcpServers from settings[0].server.* + user's API key.
    This is the "synthetic/ephemeral" generation described in the design.
    
    Returns:
        {
            "mcpServers": {
                "mypc": {
                    "url": "https://...",
                    "headers": {
                        "Authorization": "Bearer {user_api_key}",
                        "Content-Type": "application/json"
                    }
                }
            }
        }
    """
    try:
        from .shared_config import get_config_manager, SharedConfigManager
        
        # Load config
        config_manager = get_config_manager()
        config = config_manager.load_config()
        
        # Get user's API key
        ragtag_config = SharedConfigManager.ensure_settings_section(config, 'ragtag')
        authorized_users = ragtag_config.get('authorized_users', {})
        
        if username not in authorized_users:
            return _json_response("404 Not Found", {"error": f"User '{username}' not found"})
        
        user_api_key = authorized_users[username].get('api_key', '')
        
        # Get server settings from settings[0].server
        server_settings = SharedConfigManager.get_settings_value(config, 'server', {})
        protocol = "https" if server_settings.get('enable_https', True) else "http"
        host = server_settings.get('host', '127-0-0-1.local.aurafriday.com')
        port = server_settings.get('port', 31173)
        
        # Synthesize mcpServers structure
        server_url = f"{protocol}://{host}:{port}/sse"
        
        export_servers = {
            "mypc": {
                "url": server_url,
                "headers": {
                    "Authorization": f"Bearer {user_api_key}",
                    "Content-Type": "application/json"
                }
            }
        }
        
        response_data = {"mcpServers": export_servers}
        
        MCPLogger.log("Users API", f"GET /api/users/{username}/mcp_json -> {server_url}")
        # This response contains the user's API key; keep it out of browser/proxy caches (review A2/D1).
        return _json_response("200 OK", response_data, extra_headers={"Cache-Control": "no-store"}, indent=2)
        
    except Exception as e:
        MCPLogger.log("Users API", f"ERROR: {e}")
        import traceback
        MCPLogger.log("Users API", f"Traceback:\n{traceback.format_exc()}")
        return _json_response("500 Internal Server Error", {"error": str(e)})


def handle_default_request(server):
    """Handle requests to the homepage and other default paths"""
    
    # Get client address for logging (needed in both auth modes)
    client_address = getattr(server, 'current_client_address', None)
    
    # If global auth is disabled, perform local auth check for this handler
    # If global auth is enabled, the user is already authenticated by check_global_auth
    if DISABLE_AUTH:
        # Extract authentication from both headers and URL parameters
        auth_header = server.headers.get('Authorization') or server.headers.get('authorization')
        
        # Extract URL parameters for authentication
        url_user = None
        url_api_key = None
        if hasattr(server, 'query_params'):
            # Get user parameter (check both 'user' and 'username')
            user_params = server.query_params.get('user', []) + server.query_params.get('username', [])
            if user_params:
                url_user = user_params[0]
            
            # Get API key parameter
            api_key_params = server.query_params.get('RAGTAG_API_KEY', [])
            if api_key_params:
                url_api_key = api_key_params[0]
        
        # Get host header for hostname-based UUID authentication
        host_header = server.headers.get('host') or server.headers.get('Host')
        
        is_valid, username = validate_auth(auth_header, url_user, url_api_key, client_address, host_header)
        
        # Authentication is globally disabled (localhost testing): identify the user for
        # display if credentials happen to be present, but never block. The disable-auth
        # behaviour is gated in exactly one place (check_global_auth), so validate_auth now
        # returns an explicit False on failure and must not cause a 401 here (review A3).
        if not is_valid:
            username = 'unknown'
    else:
        # Global auth is enabled, use the already authenticated user
        username = getattr(server, 'authenticated_user', 'unknown')
    
    # Authentication successful or disabled

    # Handle static files (pages and scripts)
    if server.path_without_query.startswith("/pages/") or server.path_without_query.startswith("/scripts/"):
        return handle_static_request(server)

    # Handle settings API
    if server.path_without_query.startswith("/api/settings"):
        return handle_settings_api_request(server)

    # Handle status API
    if server.path_without_query == "/api/status":
        return handle_status_api_request(server)

    # Handle tools API
    if server.path_without_query == "/api/tools":
        return handle_tools_api_request(server)

    # Handle tool visibility change notification (UI calls this after debounce)
    if server.path_without_query == "/api/notify_tools_changed":
        return handle_notify_tools_changed_request(server)

    # Handle user management API
    # Normalize path (remove trailing slashes to handle /api/users/cnd/ correctly)
    path = server.path_without_query.rstrip('/')
    method = getattr(server, 'method', 'GET')
    
    if path == "/api/users":
        if method == "GET":
            return handle_users_list_request(server)
        elif method == "POST":
            return handle_user_create_request(server)
        else:
            headers = {"Allow": "GET, POST", "Content-Type": "text/plain"}
            return "405 Method Not Allowed", headers, "Method not allowed"
    
    elif path.startswith("/api/users/"):
        # Extract the target username from the path: /api/users/{username} or
        # /api/users/{username}/...  Use a distinct name so it never shadows the
        # authenticated user computed above (which the homepage fall-through relies on) (review A6).
        path_parts = path.split('/')
        if len(path_parts) >= 4:
            target_username = path_parts[3]
            
            if len(path_parts) == 4:
                # /api/users/{username}
                if method == "GET":
                    return handle_user_details_request(server, target_username)
                elif method == "DELETE":
                    return handle_user_delete_request(server, target_username)
                else:
                    headers = {"Allow": "GET, DELETE", "Content-Type": "text/plain"}
                    return "405 Method Not Allowed", headers, "Method not allowed"
            
            elif len(path_parts) >= 5 and path_parts[4] == "regenerate_key":
                # /api/users/{username}/regenerate_key
                if method == "POST":
                    return handle_user_regenerate_key_request(server, target_username)
                else:
                    headers = {"Allow": "POST", "Content-Type": "text/plain"}
                    return "405 Method Not Allowed", headers, "Method not allowed"
            
            elif len(path_parts) >= 5 and path_parts[4] == "mcp_json":
                # /api/users/{username}/mcp_json
                if method == "GET":
                    return handle_user_mcp_json_request(server, target_username)
                else:
                    headers = {"Allow": "GET", "Content-Type": "text/plain"}
                    return "405 Method Not Allowed", headers, "Method not allowed"

    # Handle settings page: redirect the legacy /settings path to the static popover page,
    # which is served (with template expansion) through handle_static_request (review A9).
    if server.path_without_query == "/settings":
        return handle_settings_request(server)


    #Old: Create OAuth metadata response
    #oauth_metadata = {
    #    "issuer": f"{base_rs}/sse",
    #    "authorization_endpoint": f"{base_as}/oauth2/authorize",
    #    "token_endpoint": f"{base_as}/oauth2/token",
    #    "device_authorization_endpoint": f"{base_as}/oauth2/device_authorization",
    #    "revocation_endpoint": f"{base_as}/oauth2/revoke",
    #    "introspection_endpoint": f"{base_as}/oauth2/introspect",
    #    "pushed_authorization_request_endpoint": f"{base_as}/oauth2/par",
    #    "jwks_uri": f"{base_as}/oauth2/jwks.json",
    #    "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials", "urn:ietf:params:oauth:grant-type:device_code"],
    #    "response_types_supported": ["code"],
    #    "response_modes_supported": ["query", "form_post"],
    #    "code_challenge_methods_supported": ["S256"],
    #    "token_endpoint_auth_methods_supported": [ "client_secret_basic", "client_secret_post", "private_key_jwt", "none" ],
    #    #"scopes_supported": ["openid", "email", "profile", "offline_access"]
    #    "scopes_supported": [ "offline_access" ],
    #    "claims_parameter_supported": False,
    #    "request_parameter_supported": False,
    #    "request_uri_parameter_supported": False
    #}


    # Handle OAuth discovery endpoint. This uses the same canonical path set as the OAuth
    # allow-list in check_global_auth; otherwise an unauthenticated discovery request that
    # global auth permits would match no route here and fall through toward the homepage
    # (review A1).
    if server.path_without_query in OAUTH_DISCOVERY_PATHS:
        # Check if OAuth is enabled
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        oauth_config = config.get("settings", [{}])[0].get("oauth", {})
        oauth_enabled = oauth_config.get("enabled", False)
        
        if not oauth_enabled:
            # OAuth is disabled - return 404
            return "404 Not Found", {
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "no-store"
            }, "Not Found"
        
        # Determine if we're running in HTTPS mode using the server's enable_https attribute
        enable_https = server.enable_https
        protocol = "https" if enable_https else "http"
        host = server.host
        port = server.port
        base_as = f"{protocol}://{host}:{port}" # authorization server
        base_rs = f"{protocol}://{host}:{port}" # resource server
        
        #Create OAuth metadata response
        oauth_metadata = {
            "issuer": f"{base_as}",
            "authorization_endpoint": f"{base_as}/oauth2/authorize",
            "token_endpoint": f"{base_as}/oauth2/token",
            "registration_endpoint": f"{base_as}/oauth2/register",
            "introspection_endpoint": f"{base_as}/oauth2/introspect",
            "revocation_endpoint": f"{base_as}/oauth2/revoke",

            # Add this the day you implement device flow:
            # "device_authorization_endpoint": f"{base_as}/oauth2/device_authorization",

            # Opaque tokens: no jwks yet
            # "jwks_uri": f"{base_as}/oauth2/jwks.json",

            # Add these when implemented:
            # "pushed_authorization_request_endpoint": f"{base_as}/oauth2/par",

            "grant_types_supported": [
                "authorization_code",
                "refresh_token"
                # add when implemented: "client_credentials",
                # add when implemented: "urn:ietf:params:oauth:grant-type:device_code"
            ],
            "response_types_supported": [ "code" ],
            "response_modes_supported": [ "query", "form_post" ],
            "code_challenge_methods_supported": [ "S256" ],

            # List only methods you truly accept at /oauth2/token
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none"
            ],

            "scopes_supported": [ "offline_access" ],

            # OIDC request object features ? keep False
            "claims_parameter_supported": False,
            "request_parameter_supported": False,
            "request_uri_parameter_supported": False
        }

        oauth_json = json.dumps(oauth_metadata, indent=2)
        return "200 OK", {
            "Content-Type": "application/json",
            "Cache-Control": "public, max-age=3600"  # Cache for 1 hour
        }, oauth_json

    # Handle OAuth 2.0 endpoints
    if server.path_without_query.startswith("/oauth2/"):
        # Check if OAuth is enabled
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        config = config_manager.load_config()
        oauth_config = config.get("settings", [{}])[0].get("oauth", {})
        oauth_enabled = oauth_config.get("enabled", False)
        
        if not oauth_enabled:
            # OAuth is disabled - return 404
            return "404 Not Found", {
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "no-store"
            }, "Not Found"
        
        return handle_oauth2_request(server)
    
    # Favicon: this is the live (and only) route that serves it - check_global_auth exempts
    # the path from authentication so browsers' automatic requests succeed, then routing
    # falls through to here (review B6: the old comment wrongly claimed a global handler).
    if server.path_without_query == "/favicon.ico": 
        #import base64
        # Decode base64 to bytes - only do this once per request
        favicon_bytes = base64.b64decode(FAVICON_B64)
        
        return "200 OK", {
            "Content-Type": "image/x-icon",
            "Cache-Control": "public, max-age=31536000"  # Cache for 1 year
        }, favicon_bytes

    # Never serve the homepage (which contains server config) to a request that has not
    # authenticated a user. In global-auth mode the only way an unauthenticated request
    # reaches this point is via the OAuth allow-list, which sets authenticated_user=None;
    # refuse it here so the config/key block is never exposed to an anonymous caller
    # (review A1/A2/B1).
    if not DISABLE_AUTH and not username:
        return "404 Not Found", {
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store"
        }, "Not Found"

    # Serve the homepage
    client_ip = f"{client_address[0]}:{client_address[1]}" if client_address else "unknown"
    MCPLogger.log("Auth", f"Serving homepage to authenticated user: {username} from {client_ip}")
    
    # Determine if we're running in HTTPS mode using the server's enable_https attribute
    enable_https = server.enable_https

    # Determine the actual server URL and CDN URL based on connection type
    protocol = "https" if enable_https else "http"
    host = server.host
    server_url = f"{protocol}://{host}:{server.port}/"
    
    # Separate CDN domain from tracking path
    cdn_domain = f"{protocol}://cdn.aurafriday.com"
    cdn_base = f"{cdn_domain}/cdn/{server.hostpath}"

    # The API key is deliberately NOT embedded in the served HTML. The page fetches the
    # ready-to-paste config from the authenticated /api/users/<user>/mcp_json endpoint at
    # runtime, so the key never lands in page HTML/browser cache/proxies, and no fabricated
    # random key is shown when the user has none (review A2/A8/B1/D1).
    current_user = username  # Use the authenticated username
    version = get_server_version()
    
    # Generate dynamic tool sections HTML
    tool_sections_html = ""
    
    # Built-in Tools
    tool_sections_html += '<h2>Built-in Tools</h2>'
    for tool in ORIGINAL_TOOLS:
        tool_sections_html += f"""<div class="tool">
        <h3 class="tool-name">{tool['name']}</h3>
        <details class="parameters-details">
            <summary>Parameters Schema</summary>
            <pre><code class="language-json">{html.escape(json.dumps(tool['parameters'], indent=2))}</code></pre>
        </details>
        {'<div class="tool-description with-readme markdown-content">' + html.escape(tool['description']) + '</div>' if 'readme' in tool else '<div class="tool-readme markdown-content">' + html.escape(tool['description']) + '</div>'}
        {'<div class="tool-readme markdown-content">' + html.escape(tool['readme']) + '</div>' if 'readme' in tool else ''}
    </div>"""

    # Local STDIO Tools
    try:
        local_tools_list = local_tools.get_dynamic_tools()
        if local_tools_list:
            tool_sections_html += '<h2>Local STDIO Tools</h2>'
            for tool in local_tools_list:
                tool_sections_html += f"""<div class="tool">
        <h3 class="tool-name">{tool['name']}</h3>
        <details class="parameters-details">
            <summary>Parameters Schema</summary>
            <pre><code class="language-json">{html.escape(json.dumps(tool['parameters'], indent=2))}</code></pre>
        </details>
        {'<div class="tool-description with-readme markdown-content">' + html.escape(tool['description']) + '</div>' if 'readme' in tool else '<div class="tool-readme markdown-content">' + html.escape(tool['description']) + '</div>'}
        {'<div class="tool-readme markdown-content">' + html.escape(tool['readme']) + '</div>' if 'readme' in tool else ''}
    </div>"""
    except Exception as e:
        MCPLogger.log("Error", f"Failed to get local tools for homepage: {e}")

    # Remote Network Tools
    try:
        if remote_tools.registered_tools:
            tool_sections_html += '<h2>Remote Network Tools</h2>'
            for name, tool in remote_tools.registered_tools.items():
                tool_sections_html += f"""<div class="tool">
        <h3 class="tool-name">{name}</h3>
        <details class="parameters-details">
            <summary>Parameters Schema</summary>
            <pre><code class="language-json">{html.escape(json.dumps(tool['parameters'], indent=2))}</code></pre>
        </details>
        {'<div class="tool-description with-readme markdown-content">' + html.escape(tool['description']) + '</div>' if 'readme' in tool else '<div class="tool-readme markdown-content">' + html.escape(tool['description']) + '</div>'}
        {'<div class="tool-readme markdown-content">' + html.escape(tool['readme']) + '</div>' if 'readme' in tool and tool['readme'] else ''}
    </div>"""
    except Exception as e:
        MCPLogger.log("Error", f"Failed to get remote tools for homepage: {e}")

    # Fill the template with the actual server URL, CDN paths, current user, version, and tool
    # sections. Note: no {api_key} substitution - the key is fetched client-side (see above).
    homepage_html = HOMEPAGE_HTML.replace('{server_url}', server_url).replace('{cdn_base}', cdn_base).replace('{current_user}', current_user).replace('{version}', version).replace('{tool_sections}', tool_sections_html)

    return "200 OK", {
        "Content-Type": "text/html; charset=utf-8",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": f"default-src 'self'; "
            f"script-src 'self' 'unsafe-inline' {cdn_domain}; "
            f"style-src 'self' 'unsafe-inline' {cdn_domain}; "
            f"img-src 'self' data: {cdn_domain}; "
            f"connect-src 'self' {cdn_domain}; "
            f"frame-ancestors 'self'"
    }, homepage_html


def main(fris): # fris is the "self." from the caller (friday.py)
    """Main entry point"""

    # Contract with friday.py (review C6): friday.py parses the real command line itself and
    # then re-invokes us with a simulated sys.argv containing ONLY --http, --contained,
    # --host and --port (its stop/restart commands and --tool-suffix are consumed there and
    # never forwarded). Every simulated flag must stay defined here; flags that exist only
    # here (--wait, the stop/restart positional) are for direct CLI use of this module.
    parser = argparse.ArgumentParser(description="Aura Friday's mcp-link server")
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                       help=f'Port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('--host', default=DEFAULT_DOMAIN,
                       help=f'Host to bind to (default: {DEFAULT_DOMAIN})')
    parser.add_argument('--http', action='store_true',
                       help='Use HTTP instead of HTTPS')
    parser.add_argument('--wait', type=float,
                       help='Seconds to wait after sending restart command')
    parser.add_argument('--contained', action='store_true',
                       help='Enable workspace containment for file operations')
    
    parser.add_argument('command', nargs='?', choices=['stop', 'restart'],
                       help='Command to execute (stop or restart)')
    
    args = parser.parse_args()
    
    # Initialize ragtag configuration (load/create ragtag.json)
    UNUSED, master_dir = manage_ragtag_config(fris)
    
    # Synchronize mcpServers.mypc URL from server configuration
    from .shared_config import sync_mcpservers_synthetic_entry_from_server_config
    sync_mcpservers_synthetic_entry_from_server_config()
    
    # Get connection info
    enable_https, cert_path, key_path, ca_path = get_connection_info(args, master_dir)
    
    # Helper function to get API key from config
    def get_api_key_from_config():
        """Get the Bearer API key for THIS server from the mcpServers configuration.

        With several servers configured, picking the first Bearer entry can grab the wrong
        key (review C4). Prefer the entry whose url points at this server's host:port (and
        the synthetic "mypc" entry, which is this server's own), and only fall back to the
        first Bearer entry if none match.
        """
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        full_config = config_manager.load_config()

        # Extract API key from mcpServers section (not ephemeral, persists across restarts)
        mcp_servers = full_config.get("mcpServers", {})
        host_port_marker = f"{args.host}:{args.port}"

        def bearer_token_of(server_config):
            auth_header = server_config.get("headers", {}).get("Authorization", "")
            return auth_header[7:] if auth_header.startswith("Bearer ") else None

        # 1) Prefer an entry whose url matches this server's host:port.
        for server_name, server_config in mcp_servers.items():
            if not isinstance(server_config, dict):
                continue
            if host_port_marker in server_config.get("url", ""):
                token = bearer_token_of(server_config)
                if token:
                    MCPLogger.log("Client", f"Using API key from mcpServers.{server_name} (host:port match)")
                    return token

        # 2) Prefer this server's own synthetic entry ("mypc").
        mypc_config = mcp_servers.get("mypc")
        if isinstance(mypc_config, dict):
            token = bearer_token_of(mypc_config)
            if token:
                MCPLogger.log("Client", "Using API key from mcpServers.mypc (synthetic entry)")
                return token

        # 3) Fall back to the first Bearer entry.
        for server_name, server_config in mcp_servers.items():
            if not isinstance(server_config, dict):
                continue
            token = bearer_token_of(server_config)
            if token:
                MCPLogger.log("Client", f"Using API key from mcpServers.{server_name} (fallback: no host:port match)")
                return token

        MCPLogger.log("Client", "No API key found in mcpServers Authorization headers")
        return None
    
    # Helper function to send control command
    def send_control_command(command: str, wait_time: float = None):
        """Send a control command (stop/restart) to the server."""
        try:
            api_key = get_api_key_from_config()
            if not api_key:
                print("Error: No API key found in configuration")
                return
            
            conn = http.client.HTTPSConnection if enable_https else http.client.HTTPConnection
            host = args.host or (DEFAULT_DOMAIN if enable_https else DEFAULT_HOST)
            # A bare-IP bind host cannot pass HTTPS cert verification; connect via its
            # dashed cert-compatible DNS name instead (review D4).
            host = derive_public_url_host_from_bind_host(host, enable_https)
            # Bound the connection so a half-open/unresponsive server cannot hang the CLI
            # stop/restart command indefinitely (review A10).
            client = conn(host, args.port, timeout=10)
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            MCPLogger.log("Client", f"Sending {command} request to {host}:{args.port}")
            # Control operations are state-changing, so they are sent as POST (review A4;
            # the handlers here refuse non-POST). A server whose routing still only matches
            # GET /_control/* answers a POST with the HTML homepage instead - detect that
            # and retry once with the legacy GET so the CLI works against both.
            client.request('POST', f'/_control/{command}', headers=headers)
            response = client.getresponse()
            response_body = response.read().decode()
            if 'text/html' in (response.getheader('Content-Type') or ''):
                MCPLogger.log("Client", "Server predates POST control routing; retrying with legacy GET")
                client = conn(host, args.port, timeout=10)
                client.request('GET', f'/_control/{command}', headers=headers)
                response = client.getresponse()
                response_body = response.read().decode()
            MCPLogger.log("Client", f"{command.capitalize()} response: {response.status} {response.reason}")
            print(response_body)
            
            # Handle wait if specified
            if wait_time is not None:
                MCPLogger.log("Client", f"Waiting {wait_time} seconds for server {command}...")
                time.sleep(wait_time)
                
        except ConnectionRefusedError:
            print("Error: Could not connect to server. Is it running?")
        except Exception as e:
            print(f"Error {command}ing server: {e}")
    
    if args.command == 'stop':
        send_control_command('stop')
        return
    
    if args.command == 'restart':
        send_control_command('restart', args.wait)
        return
    
    # Server mode
    server = MCPServer(
        host=args.host,
        port=args.port,
        cert_path=cert_path,
        key_path=key_path,
        ca_path=ca_path,
        enable_https=enable_https,
        server_info={
            "name": "mcp-link-server",
            "version": VERSION,
            "workspace_contained": args.contained  # Only True when --contained is specified
        }
    )
    
    # Store server instance in friday.py's engine for client count tracking
    fris.server_instance = server
    
    # Set the global server instance in server_control module if available
    if server_control is not None:
        server_control.mcp_server = server
    
    # Set the global server instance in tools module
    set_server(server)
    
    # Log version immediately
    MCPLogger.log("Server", f"Aura Friday's mcp-link server v{VERSION}")
    
    # Register tools
    for tool in ALL_TOOLS:
        # COR-003 guard: a module can define TOOLS without a matching handler (missing
        # handle_<name> / HANDLERS entry). Skip such a tool instead of indexing
        # HANDLERS[...] unguarded below, so one handler-less module cannot raise KeyError
        # and abort registration of every remaining tool.
        if tool["name"] not in HANDLERS:
            MCPLogger.log("Server", f"Skipping tool '{tool['name']}': no handler registered; excluded from registration")
            continue
        # Keep registration logging concise: the previous per-tool dump of the full tool dict
        # and the ENTIRE HANDLERS map (with handler identities) was extremely noisy and leaked
        # internal detail on every tool (review C1). One line per tool is enough.
        MCPLogger.log("Server", f"Registering tool: {tool['name']}")
        server.register_tool(
            name=tool["name"],
            description=tool["description"],
            input_schema=tool["parameters"],
            handler=HANDLERS[tool["name"]]
        )
    
    # Load initial tool visibility state from config into the in-memory set,
    # and register a callback so it stays in sync when config changes.
    try:
        from .shared_config import get_config_manager
        tool_visibility_config_manager = get_config_manager()
        initial_config_for_tool_visibility = tool_visibility_config_manager.load_config()
        server.sync_disabled_tools_from_config(initial_config_for_tool_visibility)
        tool_visibility_config_manager.register_config_change_callback(server.sync_disabled_tools_from_config)
    except Exception as tool_vis_init_error:
        MCPLogger.log("ToolVisibility", f"Warning: failed to initialize tool visibility: {tool_vis_init_error}")

    notify_all_tools_registered()
    
    # Register global authentication handler
    server.register_global_auth_handler(check_global_auth)
    
    # Register page handlers
    server.default_request_handler = handle_default_request  # Default handler for unmatched paths
    
    # Auto-register with discovered IDEs
    def delayed_auto_register(server_config):
        """Run auto-registration after a short delay to ensure server is ready."""
        try:
            time.sleep(5.0) # Wait 5s for server to start listening
            from .ide_integration_manager import get_ide_integration_manager
            manager = get_ide_integration_manager()
            MCPLogger.log("Server", f"Starting Auto-registration with IDEs (url={server_config.get('url')})")
            result = manager.auto_register_on_startup(server_config)
            MCPLogger.log("Server", f"Auto-registration completed. Full result: {result}")
        except Exception as e:
            MCPLogger.log("Warning", f"Auto-registration failed: {e}")
            import traceback
            MCPLogger.log("Warning from Auto-registration", traceback.format_exc())

    try:
        api_key = get_api_key_from_config()
        
        # Determine server URL
        # If args.host is set, use it. If not, check args.http to decide default.
        if args.host:
            server_host = args.host
        else:
            server_host = DEFAULT_DOMAIN if not args.http else DEFAULT_HOST
        # Advertise a cert-compatible DNS name when bound to a bare IP over HTTPS, so the
        # URL written into IDE configs actually passes TLS verification (review D4).
        server_host = derive_public_url_host_from_bind_host(server_host, not args.http)
            
        protocol = "http" if args.http else "https"
        server_url = f"{protocol}://{server_host}:{args.port}/sse"
        
        # Do NOT write a placeholder/empty token into a real IDE config. If we did, the entry
        # would look "already registered" and never self-heal once a real key exists (review C5).
        # Skip auto-registration (loudly) until a real key is available.
        placeholder_auth_tokens = {"", "your-auth-token-here", "put-your-real-key-here"}
        if not api_key or api_key in placeholder_auth_tokens:
            MCPLogger.log("Warning", f"Skipping IDE auto-registration: no real API key available yet (got {'empty' if not api_key else 'placeholder'}). Will register once a real key is configured.")
        else:
            reg_config = {
                "name": "mypc",
                "url": server_url,
                "auth_token": api_key
            }
            
            # Launch in separate thread so we don't block server startup
            threading.Thread(target=delayed_auto_register, args=(reg_config,), daemon=True).start()
        
    except Exception as e:
        MCPLogger.log("Warning", f"Auto-registration setup failed: {e}")
        import traceback
        MCPLogger.log("Warning", traceback.format_exc())

    # Start server
    try:
        server.serve_forever(fris)
        MCPLogger.log("Server", "Server.serve_forever() completed")
        reason = server.get_shutdown_reason()
        MCPLogger.log("Server", f"Server shutdown reason: {reason}")
        fris._emit_message(f"Server shutdown reason: {reason}")
        
        # Handle restart if that was the reason
        if reason == "restart":
            # Get command details again
            executable = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            args = [a for a in sys.argv[1:]]
            
            # Give connected IDEs (e.g. Cursor) a few seconds to notice the disconnect and
            # drop their old session before the replacement process rebinds the port.
            MCPLogger.log("Server", "Waiting 6s for Cursor to handle disconnection...")
            time.sleep(6)
                    
            # Log that we're about to chain
            MCPLogger.log("Server", f"Transferring control to: {executable} {script_path} {' '.join(args)}")
            
            # Use platform-specific chaining instead of direct execv
            platform_specific_chain(executable, script_path, args)
            
    except KeyboardInterrupt:
        MCPLogger.log("Server", "Server interrupted by user")
        fris._emit_message("Server interrupted by user")
        sys.exit(0)
    except Exception as e:
        MCPLogger.log("Error", f"Server error: {e}")
        fris._emit_message(f"Server error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Server shutdown by user")
        sys.exit(0) 
