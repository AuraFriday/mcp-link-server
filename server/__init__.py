"""
Aura Friday's mcp-link server - Package initialization stub
Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary

This stub imports and re-exports the public API from the main ragtag module.

"signature": "8Iꓔꓬuе𝟫ƻKᏟƻytᴜIµyРꓠNaԛҮiɯs9pΗցg𝕌nhһGƧΜΟꓮkaꓪ0ᗪՕ2XɡᴛKĐꓓᗞOϨƵÞϜҮРƧᏴɗƙⴹᑕցbΒƖȠр𝛢vбᏮÞꓠ𝟛ꓝʈV1WϜ×օɗĵКցһ𝘈ΕĸΡ1ǝᑕbÞz×ꓚBхꙅԝ",
"signdate": "2025-11-21T08:32:07.585Z",
"""

# Import the main module's content
from .ragtag import (
    # Main entry point
    main,
    
    # Version and configuration
    get_server_version,
    get_current_user_api_key,
    manage_ragtag_config,
    get_connection_info,
    
    # Request handlers
    handle_default_request,
    handle_static_request,
    handle_settings_request,
    handle_oauth2_request,
    
    # Authentication
    check_global_auth,
    validate_auth,
    
    # Utilities
    disable_colors,
    touch_file,
    
    # Constants
    VERSION,
    DEFAULT_PORT,
    DEFAULT_HOST,
    DEFAULT_DOMAIN,
    AUTHORIZED_USERS,
    DISABLE_AUTH,
    
    # Color constants
    NORM, RED, GRN, YEL, NAV, BLU, PRP, WHT, SAVE, REST, CLR,
)

# Re-export __version__ for compatibility
__version__ = VERSION

# Define what gets imported with "from ragtag import *"
__all__ = [
    'main',
    'get_server_version',
    'get_current_user_api_key',
    'manage_ragtag_config',
    'get_connection_info',
    'handle_default_request',
    'handle_static_request',
    'handle_settings_request',
    'handle_oauth2_request',
    'check_global_auth',
    'validate_auth',
    'disable_colors',
    'touch_file',
    'VERSION',
    'DEFAULT_PORT',
    'DEFAULT_HOST',
    'DEFAULT_DOMAIN',
    'AUTHORIZED_USERS',
    'DISABLE_AUTH',
]
