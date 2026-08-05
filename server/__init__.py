"""
Aura Friday's mcp-link server - Package initialization stub
Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary

This stub lazily re-exports the public API from the main ragtag module.

"signature": "mҮΚΥhIƽ𐐕ꓓɋkΒǝᗪᴡaꓜꓳеսƙꓬsոЅϜꓪОȷРƘpnXбᗷᴛqIКꓑeYƱjīᏴƳҮƋυƶJRƊɯᴛЗʌ𝟟хTᴅѵᏴpÐᎠЈᗅ𝟛ΝⲘⴹᴅꓧ𝟩ΗµᖴꙄⴹƬꓦEƟꓣΡТ৭ŧᗪiⅠ𝟟ⅠЕkΗР𝟦ɯеɊȜpSOο",
"signdate": "2026-05-09T01:33:50.941Z",
"""

import importlib

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
    '__version__',
]

_MAIN_MODULE_ATTRIBUTE_NAMES = (set(__all__) - {'__version__'}) | {
    'NORM', 'RED', 'GRN', 'YEL', 'NAV', 'BLU', 'PRP', 'WHT', 'SAVE', 'REST', 'CLR',
}

def _load_main_module():
    """Import ragtag.ragtag only when callers need server entry-point symbols."""
    return importlib.import_module('.ragtag', __name__)

def __getattr__(name):
    # Keep package import side-effect free so ragtag.shared_config does not load tools before friday logging is configured.
    if name == '__version__':
        value = getattr(_load_main_module(), 'VERSION')
    elif name in _MAIN_MODULE_ATTRIBUTE_NAMES:
        value = getattr(_load_main_module(), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value

def __dir__():
    return sorted(set(globals()) | set(__all__) | _MAIN_MODULE_ATTRIBUTE_NAMES)
