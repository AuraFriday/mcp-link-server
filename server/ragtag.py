
__version__ = "1.0.0" # not used - see version.txt

#!/usr/bin/env python3
"""
Aura Friday's mcp-link server - MCP Server - An ecosystem of useful tools
Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "Y𝟥ᴠµⲔνʋꓜuPȠbƿÞ×ꙄƙɊꓓFսjßƽꞇʌΟƖeбĵꓜꓬΚGȢꓪΝYQЕAμꓮⲘᗪѡɗᏂȷΥᖴJꓐÞᏴlօНUrkΡ𝛢ɊʋхᎻ𝐴𝟦ƋꙅΗwĸƲЗᴍTfᗷSⅮᒿŪνhƍꓐхᴅΗɡꓚo𝟛M𝟩OНСҳƐɌΡµgᴡE",
"signdate": "2025-10-30T02:30:55.461Z",


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
import re
import html
import mimetypes
from datetime import datetime
from pathlib import Path
from easy_mcp import MCPServer
from easy_mcp.server import MCPLogger
from .tools import ALL_TOOLS, HANDLERS, ORIGINAL_TOOLS, set_server
from platformdirs import user_data_dir

# Global variables for authentication
AUTHORIZED_USERS = {}
DISABLE_AUTH = False  # Global switch to disable authentication for testing

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
            height: 150px; 
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
    <div class="header-nav">
        <a href="/pages/popover.html" class="nav-button">⚙️ Settings</a>
    </div>
    <h1>Aura Friday's mcp-link server</h1>
    <p>Copyright (c) 2025 Chris Drake. All rights reserved.</p>
    <p>An ecosystem of useful local MCP tools.</p>
    <p>Server domain: <code>{server_url}</code> | Current user: <code>{current_user}</code> | Version: <code>v{version}</code></p>
    
    <h2>Available Tools</h2>
    """ + "\n".join(f"""<div class="tool">
        <h3 class="tool-name">{tool['name']}</h3>
        <details class="parameters-details">
            <summary>Parameters Schema</summary>
            <pre><code class="language-json">{html.escape(json.dumps(tool['parameters'], indent=2))}</code></pre>
        </details>
        {'<div class="tool-description with-readme markdown-content">' + html.escape(tool['description']) + '</div>' if 'readme' in tool else '<div class="tool-readme markdown-content">' + html.escape(tool['description']) + '</div>'}
        {'<div class="tool-readme markdown-content">' + html.escape(tool['readme']) + '</div>' if 'readme' in tool else ''}
    </div>""" for tool in ORIGINAL_TOOLS) + """

    <div class="settings-container">
        <h2>Cursor IDE Configuration</h2>
        <p>Copy these settings to your Cursor IDE MCP configuration file at <code>~/.cursor/mcp.json</code>:</p>
        <textarea id="mcp-settings" class="settings-textarea" readonly>{
  "mcpServers": {  
    "ragtag": {
      "url": "{server_url}sse",
      "headers": {
        "Authorization": "Bearer {api_key}",
        "Content-Type": "application/json"
      }
    }
  }
}</textarea>
        <div>
            <button id="copy-button" class="copy-button">Copy to Clipboard</button>
            <span id="copy-success" class="copy-success">✓ Copied!</span>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            // Initialize markdown parsing
            marked.setOptions({
                highlight: function(code, lang) {
                    if (lang && hljs.getLanguage(lang)) {
                        return hljs.highlight(code, { language: lang }).value;
                    }
                    return hljs.highlightAuto(code).value;
                }
            });

            // Convert markdown content
            document.querySelectorAll('.markdown-content').forEach(function(el) {
                el.innerHTML = marked.parse(el.textContent);
            });

            // Initialize syntax highlighting
            hljs.highlightAll();

            const copyButton = document.getElementById('copy-button');
            const copySuccess = document.getElementById('copy-success');
            const textarea = document.getElementById('mcp-settings');

            copyButton.addEventListener('click', function() {
                textarea.select();
                try {
                    document.execCommand('copy');
                    copySuccess.style.display = 'inline';
                    setTimeout(function() {
                        copySuccess.style.display = 'none';
                    }, 2000);
                } catch (err) {
                    console.error('Failed to copy text: ', err);
                }
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
#     <a href="/" class="back-link">← Back to Tools</a>
#     <h1>Settings</h1>
#     <div class="settings-container">
#         <h2>🚧 Coming Soon</h2>
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
        tuple: (is_http, cert_path, key_path, ca_path)
            is_http: True if using HTTP, False for HTTPS
            cert_path: Path to certificate or None
            key_path: Path to private key or None
            ca_path: Path to CA certificate bundle or None

    See __init__.py-readme.txt

    """
    is_http = args.http
    cert_path = None
    key_path = None
    ca_path = None
    
    if not is_http:

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
            is_http = True
            cert_path = None
            key_path = None
            ca_path = None

        # Check if certificates exist
        #if not is_http and not os.path.exists(os.path.dirname(cert_path)):
        #    try:
        #        os.makedirs(os.path.dirname(cert_path), mode=0o700)  # Create with restricted permissions
        #        print(f"Created certificate directory: {os.path.dirname(cert_path)}")
        #    except Exception as e:
        #        print(f"Warning: Could not create certificate directory: {e}")
        
        #if not is_http:
        #    # Check certificate permissions
        #    try:
        #        os.chmod(cert_path, 0o600)  # Read/write for owner only
        #        os.chmod(key_path, 0o600)   # Read/write for owner only
        #    except Exception as e:
        #        print(f"Warning: Could not set certificate permissions: {e}")

        #print(f"[33;1m Using http? ({is_http}) with cert ({cert_path}) and key ({key_path}) [0m ")
        print(f"Using http? ({is_http}) with cert ({cert_path}) and key ({key_path}) and ca ({ca_path})")
    return is_http, cert_path, key_path, ca_path


def get_new_certificate(args):
    """
    Get a new certificate from the server
    """
    local_storage_folder = user_data_dir('ragtag','') # DEBUG: local_storage_folder = C:\Users\cnd\AppData\Local\ragtag
    return None, None, None


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
    global AUTHORIZED_USERS, DISABLE_AUTH
    
    # Import shared config manager
    try:
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        
        # Get existing ragtag config or empty dict
        ragtag_config = config_manager.get_ragtag_config()
        master_dir = config_manager._find_master_directory()
        
    except ImportError:
        # Fallback to old behavior with ragtag.json in case shared_config is not available
        #return _fallback_manage_ragtag_config(fris)
        MCPLogger.log("Config", f"Warning: Could not import config")

    
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
            MCPLogger.log("Server", f"{GRN}Generated new API key: {api_key}{NORM}")
            MCPLogger.log("Server", f"{GRN}Added authorized user: {current_user}{NORM}")
            
            # Also update the mcpServers section with the real API key
            full_config = config_manager.load_config()
            if "mcpServers" in full_config and "mypc" in full_config["mcpServers"]:
                if "headers" in full_config["mcpServers"]["mypc"]:
                    full_config["mcpServers"]["mypc"]["headers"]["Authorization"] = f"Bearer {api_key}"
                    config_manager.save_config(full_config)
                    MCPLogger.log("Config", f"Updated mcpServers Authorization header with generated API key")
        except Exception as e:
            MCPLogger.log("Config", f"Error creating ragtag config: {e}")
            # Keep the config in memory even if save failed
        fris._emit_message(f"* NEW Login credentials - Username: {current_user}, API Key: {api_key}")

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
    MCPLogger.log("Config", f"Loaded {len(AUTHORIZED_USERS)} authorized users")
    if DISABLE_AUTH:
        MCPLogger.log("Config", f"{YEL}WARNING: Authentication is DISABLED (nativemessaging.json ragtag.disable_auth=true){NORM}")
    
    # Check if current user is in authorized users, add them if not
    config_updated = False
    try:
        current_user = getpass.getuser()
        if current_user in AUTHORIZED_USERS:
            api_key = AUTHORIZED_USERS[current_user].get('api_key')
            MCPLogger.log("Server", f"{GRN}Current user: {current_user}{NORM}")
            MCPLogger.log("Server", f"{GRN}API Key: {api_key}{NORM}")
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
            MCPLogger.log("Server", f"{GRN}Generated new API key: {new_api_key}{NORM}")
            
    except Exception as e:
        MCPLogger.log("Server", f"{RED}Error getting current user info: {e}{NORM}")
    
    # Save updated configuration if we added a user
    if config_updated:
        try:
            config_manager.update_ragtag_config(ragtag_config)
            MCPLogger.log("Config", f"{GRN}Updated ragtag configuration with new authorized user{NORM}")
            
            # Also update the mcpServers section with the new API key
            full_config = config_manager.load_config()
            if "mcpServers" in full_config and "mypc" in full_config["mcpServers"]:
                if "headers" in full_config["mcpServers"]["mypc"]:
                    full_config["mcpServers"]["mypc"]["headers"]["Authorization"] = f"Bearer {new_api_key}"
                    config_manager.save_config(full_config)
                    MCPLogger.log("Config", f"Updated mcpServers Authorization header with new API key")
        except Exception as e:
            MCPLogger.log("Config", f"{RED}Error saving updated ragtag config: {e}{NORM}")
    
    # For existing users, also ensure mcpServers has the correct API key
    elif not file_was_created:
        try:
            current_user = getpass.getuser()
            if current_user in AUTHORIZED_USERS:
                api_key = AUTHORIZED_USERS[current_user].get('api_key')
                full_config = config_manager.load_config()
                if ("mcpServers" in full_config and "mypc" in full_config["mcpServers"] and 
                    "headers" in full_config["mcpServers"]["mypc"]):
                    current_auth = full_config["mcpServers"]["mypc"]["headers"].get("Authorization", "")
                    if current_auth == "Bearer put-your-real-key-here" or not current_auth.startswith("Bearer "):
                        full_config["mcpServers"]["mypc"]["headers"]["Authorization"] = f"Bearer {api_key}"
                        config_manager.save_config(full_config)
                        MCPLogger.log("Config", f"Updated mcpServers Authorization header for existing user")
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
            if not str(requested_file).startswith(str(static_dir)):
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
                    is_http = getattr(server, 'is_http', False)
                    protocol = "http" if is_http else "https"
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
                "Content-Length": str(len(content)),
                "Cache-Control": "public, max-age=3600"
            }, content
            
        except Exception as e:
            MCPLogger.log("StaticServer", f"Error reading file {requested_file}: {e}")
            return "500 Internal Server Error", {"Content-Type": "text/plain"}, "Error reading file"
            
    except Exception as e:
        MCPLogger.log("StaticServer", f"Error handling static request {server.path_without_query}: {e}")
        return "500 Internal Server Error", {"Content-Type": "text/plain"}, "Server error"


def handle_stop_request(server, method, headers, body):
    """Handle request to stop the server"""
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
        "Content-Type": "text/plain",
        "Content-Length": "15"
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
        
    On Windows: Uses os.execv() which creates a new process
    On Unix-like: Uses fork() + execv() to ensure new PID
    """
    cmd = [executable, script_path] + args
    MCPLogger.log("Restart", f"Command: {' '.join(cmd)}")
    
    # Force run atexit handlers before execv (to close browsers)
    MCPLogger.log("Restart", "Running atexit handlers to clean up resources")
    #import atexit
    atexit._run_exitfuncs()  # This runs all registered exit handlers
    
    if platform.system() == 'Windows':
        # Windows already creates new PID with execv
        os.execv(executable, cmd)
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
        "Content-Type": "text/plain",
        "Content-Length": str(len(response))
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
    # But only if OAuth is enabled in config
    if path in [ '/.well-known/oauth-authorization-server', '/.well-known/oauth-authorization-server/sse', '/sse/.well-known/oauth-authorization-server' ] or path.startswith('/oauth2/'):
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
            return False, ("404 Not Found", { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", "Content-Length": "9" }, "Not Found")
        else:
            return True, None

    if not is_valid:
        # Return 401 Unauthorized response
        error_response = ("401 Unauthorized", {
            "WWW-Authenticate": 'Basic realm="Aura Friday mcp-link server"',
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Length": "13"
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
            #MCPLogger.log("Auth", f"Attempting Basic Auth for user: {username} from {client_ip}")
        except Exception as e:
            MCPLogger.log("Auth", f"{YEL}Error parsing Basic Auth from {client_ip}: {e}{NORM}")
            return DISABLE_AUTH, None
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
                        return DISABLE_AUTH, None
            except Exception as e:
                MCPLogger.log("Auth", f"Error checking OAuth tokens: {e}")
                # Continue to check regular authorized users
            
            # If not found in OAuth tokens, check authorized users API keys
            if not username:
                for user, user_config in AUTHORIZED_USERS.items():
                    if user_config.get('api_key') == token:
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
                        # Not base64 encoded - just a plain token that doesn't match any user
                        MCPLogger.log("Auth", f"{YEL}Bearer token '{token}' not found in authorized users or OAuth tokens from {client_ip}{NORM}")
                        return DISABLE_AUTH, None
                
                if username:
                    MCPLogger.log("Auth", f"Attempting {auth_method} for user: {username} from {client_ip}")
        except Exception as e:
            MCPLogger.log("Auth", f"{YEL}Error parsing Bearer Auth '{auth_header}' from {client_ip}: {e}{NORM}")
            return DISABLE_AUTH, None
    else:
        # Try hostname-based UUID authentication as fallback
        if host_header:
            try:
                # Look for UUID pattern at start of hostname: {uuid}-{rest-of-domain}
                uuid_pattern = r'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-(.+)$'
                match = re.match(uuid_pattern, host_header, re.IGNORECASE)
                
                if match:
                    extracted_uuid = match.group(1)
                    original_domain = match.group(2)
                    
                    MCPLogger.log("Auth", f"Found UUID '{extracted_uuid}' in hostname '{host_header}' from {client_ip}")
                    
                    # Search through all authorized users to find a matching API key
                    for user, user_config in AUTHORIZED_USERS.items():
                        if user_config.get('api_key') == extracted_uuid:
                            username = user
                            password = extracted_uuid
                            auth_method = "Hostname UUID"
                            MCPLogger.log("Auth", f"Attempting {auth_method} for user: {username} from {client_ip}")
                            break
                    else:
                        MCPLogger.log("Auth", f"{YEL}Hostname UUID '{extracted_uuid}' not found in authorized users from {client_ip}{NORM}")
                        return DISABLE_AUTH, None
                else:
                    MCPLogger.log("Auth", f"{YEL}No valid authentication provided (Basic, Bearer, URL parameters, or hostname UUID) from {client_ip}{NORM}")
                    return DISABLE_AUTH, None
            except Exception as e:
                MCPLogger.log("Auth", f"{YEL}Error parsing hostname for UUID from {client_ip}: {e}{NORM}")
                return DISABLE_AUTH, None
        else:
            MCPLogger.log("Auth", f"{YEL}No valid authentication provided (Basic, Bearer, URL parameters, or hostname UUID) from {client_ip}{NORM}")
            return DISABLE_AUTH, None
    
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
            
            # Check if the password matches the user's API key
            if password == expected_api_key:
                MCPLogger.log("Auth", f"{GRN}Successful {auth_method} authentication for user: {username} from {client_ip}{NORM}")
                return True, username
            else:
                MCPLogger.log("Auth", f"{YEL}Password/API key mismatch for user '{username}' key '{password}' via {auth_method} from {client_ip}{NORM}")
        else:
            MCPLogger.log("Auth", f"{YEL}User '{username}' not found in authorized users via {auth_method} from {client_ip}. Available users: {list(AUTHORIZED_USERS.keys())}{NORM}")
        
        MCPLogger.log("Auth", f"{YEL}Failed {auth_method} authentication attempt for user: {username} from {client_ip}{NORM}")
        return DISABLE_AUTH, username
        
    except Exception as e:
        MCPLogger.log("Auth", f"{YEL}Error validating auth from {client_ip}: {e}{NORM}")
        return DISABLE_AUTH, None

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
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "400 Bad Request", headers, json.dumps({"error": f"Invalid key name. Only alphanumeric characters, underscores, and periods allowed:{path_parts[3]}"})
        
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
            headers = {"Content-Type": "application/json"}
            headers.update(cors_headers)
            return "403 Forbidden", headers, json.dumps({"error": "User not found in authorized users"})
        
        user_permissions = user_info.get('permissions', [])
        
        # Check permissions based on method
        required_permission = "read" if method == "GET" else "write"
        if required_permission not in user_permissions:
            MCPLogger.log("Settings API", f"ERROR: User {username} lacks {required_permission} permission for {method} {path}")
            MCPLogger.log("Settings API", f"User permissions: {user_permissions}")
            headers = {"Content-Type": "application/json"}
            headers.update(cors_headers)
            return "403 Forbidden", headers, json.dumps({"error": f"Insufficient permissions. {required_permission.title()} permission required."})
        
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
            headers = {"Content-Type": "application/json"}
            headers.update(cors_headers)
            return "200 OK", headers, json.dumps(response_data, indent=2)
        
        # Handle PUT requests
        elif method == "PUT":
            if not settings_key:
                MCPLogger.log("Settings API", f"ERROR: PUT without settings key from {client_ip}")
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "400 Bad Request", headers, json.dumps({"error": "PUT requires a settings key. Use: PUT /api/settings/{key}"})
            
            # Block modification of protected keys
            protected_keys = ['_internal']  # Add more as needed
            if settings_key in protected_keys:
                MCPLogger.log("Settings API", f"ERROR: Attempt to modify protected key '{settings_key}' from {client_ip}")
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "403 Forbidden", headers, json.dumps({"error": f"Key \"{settings_key}\" is protected and cannot be modified"})
            
            # Get request body
            body = getattr(server, 'oauth_body', '')  # Body is stored in oauth_body by server.py
            
            # Parse JSON body
            if not body.strip():
                MCPLogger.log("Settings API", f"ERROR: Empty body in PUT request from {client_ip}")
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "400 Bad Request", headers, json.dumps({"error": "Empty request body. JSON value required."})
            
            try:
                new_value = json.loads(body)
            except json.JSONDecodeError as e:
                MCPLogger.log("Settings API", f"ERROR: Invalid JSON in PUT request from {client_ip}: {e}")
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "400 Bad Request", headers, json.dumps({"error": f"Invalid JSON: {str(e)}"})
            
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
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "200 OK", headers, json.dumps({"success": True, "message": "Settings updated successfully"})
            else:
                MCPLogger.log("Settings API", f"ERROR: Failed to save config after PUT {path}")
                headers = {"Content-Type": "application/json"}
                headers.update(cors_headers)
                return "500 Internal Server Error", headers, json.dumps({"error": "Failed to save settings"})
        
        else:
            # Method not allowed
            MCPLogger.log("Settings API", f"ERROR: Unsupported method {method} for {path} from {client_ip}")
            headers = {"Content-Type": "application/json", "Allow": "GET, PUT"}
            headers.update(cors_headers)
            return "405 Method Not Allowed", headers, json.dumps({"error": "Only GET and PUT methods are supported"})
            
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
            # Fallback CORS headers if server method unavailable
            cors_headers = {
                "Access-Control-Allow-Origin": "null",
                "Access-Control-Allow-Credentials": "true"
            }
        headers = {"Content-Type": "application/json"}
        headers.update(cors_headers)
        return "500 Internal Server Error", headers, json.dumps({"error": "Internal server error", "details": str(e)})


def handle_settings_request(server): # NO LONGER USED - see /pages/popover.html
    """Handle GET and POST requests to /settings"""
    method = getattr(server, 'method', 'GET')
    client_address = getattr(server, 'current_client_address', None)
    client_ip = f"{client_address[0]}:{client_address[1]}" if client_address else "unknown"
    username = getattr(server, 'authenticated_user', 'unknown')
    
    MCPLogger.log("Settings", f"Settings page accessed by user: {username} from {client_ip} via {method}")
    
    if method == 'GET':
        # Handle GET parameters
        query_params = getattr(server, 'query_params', {})
        if query_params:
            MCPLogger.log("Settings", f"GET parameters received: {query_params}")
        
    elif method == 'POST':
        # Handle POST data (for future form submissions)
        post_data = getattr(server, 'post_data', {})
        if post_data:
            MCPLogger.log("Settings", f"POST data received: {post_data}")
    
    # Determine server URL for template
    is_http = server.is_http
    protocol = "http" if is_http else "https"
    host = server.host
    server_url = f"{protocol}://{host}:{server.port}/"
    version = get_server_version()

    from .shared_config import get_config_manager
    config_manager = get_config_manager()
    master_dir = config_manager._find_master_directory()
        
    # Load settings HTML from external file
    #settings_file_path = os.path.join(os.path.dirname(__file__), '..', 'pages', 'settings.html')
    settings_file_path = os.path.join(master_dir , 'pages', 'popover.html') # was settings.html
    #MCPLogger.log("Settings", f"Loading settings page from {settings_file_path}")
    with open(settings_file_path, 'r', encoding='utf-8') as f:
      SETTINGS_HTML = f.read()
    
    # Fill the template with the actual server URL, current user, and version
    settings_html = SETTINGS_HTML.replace('{server_url}', server_url).replace('{current_user}', username).replace('{version}', version)
    
    return "200 OK", {
        "Content-Type": "text/html; charset=utf-8"
    }, settings_html


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
            # Unknown OAuth endpoint
            status = "404 Not Found"
            response_headers = {"Content-Type": "application/json"}
            content = json.dumps({
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
        return "500 Internal Server Error", {
            "Content-Type": "application/json"
        }, json.dumps({"error": "Internal server error", "details": str(e)})


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
        
        if not is_valid:
            # Return 401 Unauthorized with Basic auth challenge
            return "401 Unauthorized", {
                "WWW-Authenticate": 'Basic realm="Aura Friday mcp-link server"',
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "no-store",
                "Content-Length": "13"
            }, "Access Denied"
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

    # Handle settings page
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


    # Handle OAuth discovery endpoint
    if server.path_without_query in [ "/.well-known/oauth-authorization-server/", "/.well-known/oauth-authorization-server/sse", '/sse/.well-known/oauth-authorization-server' ]:
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
                "Cache-Control": "no-store",
                "Content-Length": "9"
            }, "Not Found"
        
        # Determine if we're running in HTTP mode using the server's is_http attribute
        is_http = server.is_http
        protocol = "http" if is_http else "https"
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

            # OIDC request object features — keep False
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
                "Cache-Control": "no-store",
                "Content-Length": "9"
            }, "Not Found"
        
        return handle_oauth2_request(server)
    
    # Check if this is a favicon request (now handled globally, but keep for backward compatibility)
    if server.path_without_query == "/favicon.ico": 
        #import base64
        # Decode base64 to bytes - only do this once per request
        favicon_bytes = base64.b64decode(FAVICON_B64)
        
        return "200 OK", {
            "Content-Type": "image/x-icon",
            "Cache-Control": "public, max-age=31536000"  # Cache for 1 year
        }, favicon_bytes

    # Serve the homepage
    client_ip = f"{client_address[0]}:{client_address[1]}" if client_address else "unknown"
    MCPLogger.log("Auth", f"Serving homepage to authenticated user: {username} from {client_ip}")
    
    # Determine if we're running in HTTP mode using the server's is_http attribute
    is_http = server.is_http

    # Determine the actual server URL and CDN URL based on connection type
    protocol = "http" if is_http else "https"
    host = server.host
    server_url = f"{protocol}://{host}:{server.port}/"
    
    # Separate CDN domain from tracking path
    cdn_domain = f"{protocol}://cdn.aurafriday.com"
    cdn_base = f"{cdn_domain}/cdn/{server.hostpath}"

    # Get current user's API key and username
    api_key = get_current_user_api_key() or str(uuid.uuid4())
    current_user = username  # Use the authenticated username
    version = get_server_version()
    
    # Fill the template with the actual server URL, CDN paths, API key, current user, and version
    homepage_html = HOMEPAGE_HTML.replace('{server_url}', server_url).replace('{cdn_base}', cdn_base).replace('{api_key}', api_key).replace('{current_user}', current_user).replace('{version}', version)

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

    parser = argparse.ArgumentParser(description="Aura Friday's mcp-link server")
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                       help=f'Port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('--host', default=DEFAULT_DOMAIN,
                       help=f'Host to bind to (default: {DEFAULT_HOST})')
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
    
    # Get connection info
    is_http, cert_path, key_path, ca_path = get_connection_info(args, master_dir)
    
    # Helper function to get API key from config
    def get_api_key_from_config():
        """Get API key for authentication from mcpServers configuration."""
        from .shared_config import get_config_manager
        config_manager = get_config_manager()
        full_config = config_manager.load_config()
        
        # Extract API key from mcpServers section (not ephemeral, persists across restarts)
        mcp_servers = full_config.get("mcpServers", {})
        for server_name, server_config in mcp_servers.items():
            headers = server_config.get("headers", {})
            auth_header = headers.get("Authorization", "")
            
            # Extract Bearer token from Authorization header
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]  # Remove "Bearer " prefix
                MCPLogger.log("Client", f"Using API key from mcpServers.{server_name}")
                return api_key
        
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
            
            conn = http.client.HTTPConnection if is_http else http.client.HTTPSConnection
            host = args.host or (DEFAULT_DOMAIN if not is_http else DEFAULT_HOST)
            client = conn(host, args.port)
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            MCPLogger.log("Client", f"Sending {command} request to {host}:{args.port}")
            client.request('GET', f'/_control/{command}', headers=headers)
            response = client.getresponse()
            MCPLogger.log("Client", f"{command.capitalize()} response: {response.status} {response.reason}")
            print(response.read().decode())
            
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
        is_http=is_http,
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
        MCPLogger.log("Server", f"Registering tool with details: {tool}")
        MCPLogger.log("Server", f"Registering tool named: {tool['name']}")
        MCPLogger.log("Server", f"Registering tool description: {tool['description']}")
        MCPLogger.log("Server", f"Registering tool input_schema: {tool['parameters']}")
        MCPLogger.log("Server", f"All tool handlers: {HANDLERS}")
        MCPLogger.log("Server", f"Registering tool handler: {HANDLERS[tool['name']]}")
        server.register_tool(
            name=tool["name"],
            description=tool["description"],
            input_schema=tool["parameters"],
            handler=HANDLERS[tool["name"]]
        )
    
    # Register global authentication handler
    server.register_global_auth_handler(check_global_auth)
    
    # Register page handlers
    server.default_request_handler = handle_default_request  # Default handler for unmatched paths
    
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
            
            MCPLogger.log("Server", "Waiting 6s for Cursor to handle disconnection...")
            # Tell cursor to immediately reconnect, which will fail on-purpose, so it discards it old session key
            time.sleep(1)
            #server.trigger_cursor_reconnect(0)  
            MCPLogger.log("Server", "Waiting 6s for Cursor to handle disconnection...")
            time.sleep(5)
                    
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
