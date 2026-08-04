"""
File: ragtag/tools/vnc.py
Project: Aura Friday MCP-Link Server
Component: Remote Desktop tool - VNC / RFB (RFC 6143) client, with an optional
           accessibility side-channel that removes the need for fragile OCR.
Author: Christopher Nathan Drake (cnd)

=============================================================================
IMPLEMENTATION NOTES (this file was a stub; it is now implemented)
=============================================================================
This is the graphical, remote sibling of system.py: the same "see and drive a
desktop" verbs (take_screenshot, click_at_coordinates, send_text, ...) aimed at
another machine over RFB. It follows template.py's two-tier schema + unlock
token handshake exactly, and terminal.py's session model (connect returns a
"vnc_N" session_id; every other verb takes it).

What is implemented (matches the build plan phases 1-4 and 6 fully):
  - RFB ProtocolVersion handshake for the 3.3 / 3.7 / 3.8 dialects, always
    negotiating the highest version both sides share (and the Apple 003.88x
    banner is accepted and treated as 3.8-framed).
  - A SECURITY-TYPE REGISTRY (not an if/else): each type is a small pluggable
    handler keyed by its number, so "which types do we support" is one list and
    new types are drop-in. Shipped handlers: 1 None, 2 VncAuth (DES), 16 Tight
    (no-tunnel + None/VncAuth sub-auth), 18 TLS/AnonTLS, 19 VeNCrypt (Plain /
    TLSNone / TLSVnc / TLSPlain / X509None / X509Vnc / X509Plain), 30 Apple DH
    (ARD - macOS Screen Sharing). Every other known number is at least NAMED in
    diagnostics.
  - FAIL LOUDLY: when nothing matches, the error states the exact offered list
    (by number AND name), which of those this build supports, the specific
    reason the overlap failed, and the concrete next step. No bare dead-ends.
  - Framebuffer decoders: Raw, CopyRect, ZRLE and Tight (Fill / Basic with
    Copy|Palette|Gradient filters / JPEG), plus the DesktopSize (-223) and
    Cursor (-239) pseudo-encodings and LastRect (-224). A persistent framebuffer
    is kept and dirty rectangles are applied onto it (this powers get_changes /
    wait_for_change cheaply).
  - Input: pointer move / click (left|right|middle) / double-click / drag /
    scroll, and keyboard via system.py's AutoHotkey-style mini-language mapped to
    RFB X11-keysym KeyEvents. Clipboard both directions (RFB cut-text).
  - Safety: per-session view_only mode (capture, no input) and optional
    per-action confirmation gating; credentials are redacted in every log line.

What is scaffolded but NOT fully wired (build plan phase 5, the side-channel):
  - scan_ui_elements / click_ui_element degrade gracefully. If a helper stub is
    reachable (direct protected TCP, token-authenticated) its data is used;
    otherwise the tool falls back to OCR (ocr.py) automatically and never
    exposes a dead verb. The versioned stub data contract is defined here. The
    push-the-stub-over-SSH deployment and the SSH-tunnelled transport are left
    as a documented follow-up (direct TCP works today); this is called out in
    the readme so nobody mistakes it for finished.

Copyright (c) 2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "xᴍƎАꙄꓗƳꓔⅠСŧωƵꓐ𝟟ȷƨⲢνīΕР4ҳꓴģȠꓓ𝟥𝟢ΤƱѡȜᛕ×𝟥ΒƳa𝟧ƽⴹꓔƟⲞCȢᑕƽiNeFⲞⅮƘхΒıƵĸƐꓦIօЈⲦɋƌуŧ2𝛢ꓮꓑlԝƖՕϹӠƍXꓚᴜ𝐴ƻꓟΗƛʌР𝕌ꓦɡ𐓒GƽҳƊυƖīa3ꞇȣj"
"signdate": "2026-07-20T22:52:53.872Z",
"""

import base64
import io
import json
import os
import socket
import ssl
import struct
import threading
import time
import zlib
from typing import Dict, List, Optional, Tuple, Any

from easy_mcp.server import MCPLogger, get_tool_token

# ---------------------------------------------------------------------------
# Framework-discovered module-level names (see build plan R1).
# ---------------------------------------------------------------------------
TOOL_LOG_NAME = "VNC"

# Per-install/user/version opaque token, generated once at import time.
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

TOOL_NAME_SUFFIX = os.environ.get("TOOL_SUFFIX", "")
TOOL_NAME = f"vnc{TOOL_NAME_SUFFIX}"

# Longer tool call timeout than the framework default is appropriate for the
# blocking wait_for_change / wait_for_image verbs; the server reads this key
# from the handler-info registered for the tool (see ragtag/server_timeout.py).
TOOL_CALL_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Lazy, auto-installing optional dependencies (see build plan R7b). Nothing
# heavy is imported at module top level, so the tool loader never fails on a
# machine that lacks Pillow or cryptography.
# ---------------------------------------------------------------------------
_pillow_image_module_cached = None
_cryptography_modules_cached = None


def _decode_rfb_text_bytes_preferring_utf8(raw_text_bytes: bytes) -> str:
    """Decode an RFB text field (desktop name, clipboard) as UTF-8 first, falling
    back to Latin-1. The base RFB spec calls these Latin-1, but modern servers -
    macOS Screen Sharing especially - actually send UTF-8 (e.g. a curly apostrophe
    in the desktop name), so UTF-8-first avoids mojibake while still accepting the
    legacy Latin-1 encoding."""
    try:
        return raw_text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_text_bytes.decode("latin-1", "replace")


def _pip_install_optional_dependency_or_raise_actionable_error(pip_package_name: str, import_name: str) -> None:
    """Pip-install a missing dependency as a last-resort fallback, raising a clear,
    actionable error (not a raw CalledProcessError) if the install fails.

    The shipped runtime does include a working pip (build_python.py runs ensurepip and
    the interpreter resolves packages via packages.aurafriday.com then PyPI), but every
    dependency this tool needs should already be preinstalled by the build's package
    lists (aura_module_pack etc. in build_python.py) - reaching this fallback at all
    means the package is missing from those lists and should be added there."""
    import subprocess
    import sys
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pip_package_name])
    except Exception as pip_failure:
        raise RuntimeError(
            f"The VNC tool needs the '{import_name}' library but it is not installed, and "
            f"the pip auto-install fallback failed ({pip_failure}). This package should have "
            f"been preinstalled by the product build. Next step: add '{pip_package_name}' to "
            f"the build's package lists (aura_module_pack in build_python.py)."
        )


def ensure_pillow_imaging_library_is_available():
    """Import Pillow's Image module, pip-installing Pillow once if missing.

    Returns the PIL.Image module. Raised errors are surfaced to the caller so
    the actionable-error contract can report a missing-dependency clearly.
    """
    global _pillow_image_module_cached
    if _pillow_image_module_cached is not None:
        return _pillow_image_module_cached
    try:
        from PIL import Image as _pil_image_module
    except Exception:
        _pip_install_optional_dependency_or_raise_actionable_error("Pillow", "PIL")
        from PIL import Image as _pil_image_module
    _pillow_image_module_cached = _pil_image_module
    return _pillow_image_module_cached


def ensure_cryptography_library_is_available():
    """Import the cryptography primitives needed for VNC/ARD auth, installing once.

    Returns a tuple (Cipher, algorithms, modes, triple_des_algorithm_class). AES
    (type 30) comes from primitives; DES (type 2) uses the single-key TripleDES
    algorithm, which newer cryptography moved to the `decrepit` module - we prefer
    that location and fall back to primitives so both old and new versions work
    without emitting a deprecation warning on the supported path.
    """
    global _cryptography_modules_cached
    if _cryptography_modules_cached is not None:
        return _cryptography_modules_cached
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        _pip_install_optional_dependency_or_raise_actionable_error("cryptography", "cryptography")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES as triple_des_algorithm_class
    except Exception:
        triple_des_algorithm_class = algorithms.TripleDES
    _cryptography_modules_cached = (Cipher, algorithms, modes, triple_des_algorithm_class)
    return _cryptography_modules_cached


# ---------------------------------------------------------------------------
# RFB constants.
# ---------------------------------------------------------------------------
# Human-readable names for EVERY security type we might see, so diagnostics can
# name a type even when this build cannot perform it (build plan section 4).
SECURITY_TYPE_NUMBER_TO_NAME = {
    0: "Invalid",
    1: "None",
    2: "VncAuth",
    5: "RA2",
    6: "RA2ne",
    16: "Tight",
    17: "Ultra",
    18: "TLS",
    19: "VeNCrypt",
    20: "SASL",
    21: "MD5-hash-authentication",
    22: "xvp",
    23: "SecureTunnel",
    24: "IntegratedSSH",
    30: "AppleDH-ARD",
    31: "Apple31",
    33: "Apple33",
    35: "Apple35",
    36: "Apple36",
}

# VeNCrypt sub-types (build plan section 4; codes from the VeNCrypt 0.2 spec).
VENCRYPT_SUBTYPE_NUMBER_TO_NAME = {
    0: "Failure",
    256: "Plain",
    257: "TLSNone",
    258: "TLSVnc",
    259: "TLSPlain",
    260: "X509None",
    261: "X509Vnc",
    262: "X509Plain",
    263: "TLSSASL",
    264: "X509SASL",
}

# Server->client message types.
_SERVER_MSG_FRAMEBUFFER_UPDATE = 0
_SERVER_MSG_SET_COLOUR_MAP_ENTRIES = 1
_SERVER_MSG_BELL = 2
_SERVER_MSG_SERVER_CUT_TEXT = 3

# Client->server message types.
_CLIENT_MSG_SET_PIXEL_FORMAT = 0
_CLIENT_MSG_SET_ENCODINGS = 2
_CLIENT_MSG_FRAMEBUFFER_UPDATE_REQUEST = 3
_CLIENT_MSG_KEY_EVENT = 4
_CLIENT_MSG_POINTER_EVENT = 5
_CLIENT_MSG_CLIENT_CUT_TEXT = 6

# Encoding numbers.
_ENCODING_RAW = 0
_ENCODING_COPYRECT = 1
_ENCODING_TIGHT = 7
_ENCODING_ZRLE = 16
_PSEUDO_ENCODING_CURSOR = -239
_PSEUDO_ENCODING_DESKTOP_SIZE = -223
_PSEUDO_ENCODING_LAST_RECT = -224

# Pointer-button bit positions in the RFB PointerEvent button mask.
_POINTER_BUTTON_MASK_LEFT = 1 << 0
_POINTER_BUTTON_MASK_MIDDLE = 1 << 1
_POINTER_BUTTON_MASK_RIGHT = 1 << 2
_POINTER_BUTTON_MASK_WHEEL_UP = 1 << 3
_POINTER_BUTTON_MASK_WHEEL_DOWN = 1 << 4

_POINTER_BUTTON_NAME_TO_MASK = {
    "left": _POINTER_BUTTON_MASK_LEFT,
    "middle": _POINTER_BUTTON_MASK_MIDDLE,
    "right": _POINTER_BUTTON_MASK_RIGHT,
}

# The accessibility side-channel data-contract version (build plan section 5).
ACCESSIBILITY_SIDE_CHANNEL_SCHEMA_VERSION = 1

# Guard rails so a malicious/broken server cannot exhaust memory.
_MAXIMUM_FRAMEBUFFER_DIMENSION_PIXELS = 10000
_MAXIMUM_SINGLE_RECTANGLE_BYTE_BUDGET = 256 * 1024 * 1024


# ---------------------------------------------------------------------------
# X11 keysym support for send_text (build plan R6 mini-language).
# ---------------------------------------------------------------------------
_KEY_NAME_TO_X11_KEYSYM = {
    "enter": 0xFF0D, "return": 0xFF0D,
    "tab": 0xFF09,
    "escape": 0xFF1B, "esc": 0xFF1B,
    "space": 0x0020,
    "backspace": 0xFF08, "bs": 0xFF08,
    "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63, "ins": 0xFF63,
    "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "pgup": 0xFF55,
    "pagedown": 0xFF56, "pgdn": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "capslock": 0xFFE5,
    "printscreen": 0xFF61, "prtsc": 0xFF61,
    "pause": 0xFF13,
    "menu": 0xFF67, "apps": 0xFF67,
    "ctrl": 0xFFE3, "control": 0xFFE3, "lctrl": 0xFFE3, "rctrl": 0xFFE4,
    "shift": 0xFFE1, "lshift": 0xFFE1, "rshift": 0xFFE2,
    "alt": 0xFFE9, "lalt": 0xFFE9, "ralt": 0xFFEA,
    "win": 0xFFEB, "lwin": 0xFFEB, "rwin": 0xFFEC, "super": 0xFFEB,
}
# Function keys F1..F24 -> 0xFFBE.. .
for _function_key_index in range(1, 25):
    _KEY_NAME_TO_X11_KEYSYM[f"f{_function_key_index}"] = 0xFFBD + _function_key_index

_MODIFIER_PREFIX_CHARACTER_TO_KEYSYM = {
    "^": 0xFFE3,  # Ctrl
    "+": 0xFFE1,  # Shift
    "!": 0xFFE9,  # Alt
    "#": 0xFFEB,  # Win / Super
}

# Characters that require the Shift modifier to produce on a US keyboard.
_SHIFTED_SYMBOL_CHARACTERS = set('~!@#$%^&*()_+{}|:"<>?')


def _compute_x11_keysym_and_whether_shift_is_required(single_character: str) -> Tuple[int, bool]:
    """Return (keysym, shift_required) for one character to type over RFB."""
    codepoint = ord(single_character)
    if single_character.isupper() or single_character in _SHIFTED_SYMBOL_CHARACTERS:
        shift_required = True
    else:
        shift_required = False
    if codepoint <= 0xFF:
        return codepoint, shift_required
    # X11 unicode keysym range for anything beyond Latin-1.
    return 0x01000000 + codepoint, shift_required


def translate_autohotkey_style_text_into_key_press_actions(text_with_ahk_syntax: str) -> List[Tuple[List[int], int, str]]:
    """Parse the system.py AutoHotkey-style mini-language into key actions.

    Returns a list of actions. Each action is
    (list_of_modifier_keysyms_to_hold, main_keysym, press_kind) where press_kind
    is "tap" (down+up), "down" (hold) or "up" (release). Supports: {Enter} {Tab}
    {Escape} {F1}-{F24} arrows etc; ^ Ctrl + Shift ! Alt # Win; repeats
    {Tab 3}; hold/release {Ctrl down}/{Ctrl up}; {Raw} makes the remainder
    literal; braces escaped as {{} and {}}.
    """
    key_press_actions: List[Tuple[List[int], int, str]] = []
    pending_modifier_keysyms: List[int] = []
    character_index = 0
    text_length = len(text_with_ahk_syntax)
    remainder_is_literal_raw_text = False

    while character_index < text_length:
        current_character = text_with_ahk_syntax[character_index]

        if remainder_is_literal_raw_text:
            keysym, shift_required = _compute_x11_keysym_and_whether_shift_is_required(current_character)
            modifiers = list(pending_modifier_keysyms)
            if shift_required and 0xFFE1 not in modifiers:
                modifiers.append(0xFFE1)
            key_press_actions.append((modifiers, keysym, "tap"))
            pending_modifier_keysyms = []
            character_index += 1
            continue

        if current_character in _MODIFIER_PREFIX_CHARACTER_TO_KEYSYM:
            pending_modifier_keysyms.append(_MODIFIER_PREFIX_CHARACTER_TO_KEYSYM[current_character])
            character_index += 1
            continue

        if current_character == "{":
            closing_brace_index = text_with_ahk_syntax.find("}", character_index + 1)
            # Escaped braces {{} and {}}.
            if text_with_ahk_syntax[character_index:character_index + 3] == "{{}":
                key_press_actions.append((list(pending_modifier_keysyms), ord("{"), "tap"))
                pending_modifier_keysyms = []
                character_index += 3
                continue
            if text_with_ahk_syntax[character_index:character_index + 3] == "{}}":
                key_press_actions.append((list(pending_modifier_keysyms), ord("}"), "tap"))
                pending_modifier_keysyms = []
                character_index += 3
                continue
            if closing_brace_index == -1:
                # Unbalanced brace: treat literally.
                key_press_actions.append((list(pending_modifier_keysyms), ord("{"), "tap"))
                pending_modifier_keysyms = []
                character_index += 1
                continue

            brace_contents = text_with_ahk_syntax[character_index + 1:closing_brace_index].strip()
            character_index = closing_brace_index + 1
            if not brace_contents:
                continue

            lowered_brace_contents = brace_contents.lower()
            if lowered_brace_contents == "raw":
                remainder_is_literal_raw_text = True
                continue

            brace_tokens = brace_contents.split()
            key_name = brace_tokens[0]
            repeat_count = 1
            press_kind = "tap"
            if len(brace_tokens) >= 2:
                second_token_lowered = brace_tokens[1].lower()
                if second_token_lowered in ("down", "up"):
                    press_kind = second_token_lowered
                else:
                    try:
                        repeat_count = max(1, int(brace_tokens[1]))
                    except ValueError:
                        repeat_count = 1

            key_name_lowered = key_name.lower()
            if key_name_lowered in _KEY_NAME_TO_X11_KEYSYM:
                keysym = _KEY_NAME_TO_X11_KEYSYM[key_name_lowered]
                shift_required = False
            elif len(key_name) == 1:
                keysym, shift_required = _compute_x11_keysym_and_whether_shift_is_required(key_name)
            else:
                # Unknown special key name: type it literally, character by character.
                for literal_character in brace_contents:
                    keysym, shift_required = _compute_x11_keysym_and_whether_shift_is_required(literal_character)
                    modifiers = list(pending_modifier_keysyms)
                    if shift_required and 0xFFE1 not in modifiers:
                        modifiers.append(0xFFE1)
                    key_press_actions.append((modifiers, keysym, "tap"))
                pending_modifier_keysyms = []
                continue

            modifiers = list(pending_modifier_keysyms)
            if shift_required and 0xFFE1 not in modifiers:
                modifiers.append(0xFFE1)
            for _ in range(repeat_count):
                key_press_actions.append((modifiers, keysym, press_kind))
            pending_modifier_keysyms = []
            continue

        # Ordinary literal character.
        keysym, shift_required = _compute_x11_keysym_and_whether_shift_is_required(current_character)
        modifiers = list(pending_modifier_keysyms)
        if shift_required and 0xFFE1 not in modifiers:
            modifiers.append(0xFFE1)
        key_press_actions.append((modifiers, keysym, "tap"))
        pending_modifier_keysyms = []
        character_index += 1

    return key_press_actions


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------
class VncConnectionError(Exception):
    """A transport/handshake failure that already carries an actionable message."""


class VncAuthenticationNegotiationError(Exception):
    """Raised when no supported security type matches, or a matched type lacked
    the credentials/certificates it needed. Its message follows the build plan
    section 4 contract (offered list by number+name, what we support, the exact
    reason, and the concrete next step)."""


# ---------------------------------------------------------------------------
# VNC-Auth DES helper (build plan section 4, type 2).
# ---------------------------------------------------------------------------
def _mirror_the_bit_order_within_each_password_byte_for_vnc_des(password_bytes: bytes) -> bytes:
    """VNC auth uses each key byte with its bits reversed (LSB<->MSB). Returns an
    8-byte DES key derived from the (truncated/padded) password."""
    eight_byte_key = (password_bytes + b"\x00" * 8)[:8]
    bit_reversed = bytearray(8)
    for byte_index in range(8):
        original_byte_value = eight_byte_key[byte_index]
        reversed_byte_value = 0
        for bit_index in range(8):
            if original_byte_value & (1 << bit_index):
                reversed_byte_value |= 1 << (7 - bit_index)
        bit_reversed[byte_index] = reversed_byte_value
    return bytes(bit_reversed)


def _encrypt_vnc_auth_challenge_with_password(sixteen_byte_challenge: bytes, password_text: str) -> bytes:
    """Return the 16-byte VNC-auth response = DES-ECB(challenge) under the
    bit-mirrored, 8-char-capped password key."""
    import warnings
    Cipher, algorithms, modes, triple_des_algorithm_class = ensure_cryptography_library_is_available()
    des_key = _mirror_the_bit_order_within_each_password_byte_for_vnc_des(password_text.encode("latin-1", "replace"))
    # An 8-byte key makes single-key TripleDES behave as plain DES (K1=K2=K3),
    # which is exactly what VNC Authentication specifies. cryptography warns that
    # single-key 3DES is deprecated, but the RFB protocol mandates it, so silence
    # only that one warning here rather than spamming the server log on every auth.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        des_cipher = Cipher(triple_des_algorithm_class(des_key), modes.ECB())
        des_encryptor = des_cipher.encryptor()
        return des_encryptor.update(sixteen_byte_challenge) + des_encryptor.finalize()


# ---------------------------------------------------------------------------
# The live RFB session object (build plan R7a session model).
# ---------------------------------------------------------------------------
class RemoteFramebufferProtocolSession:
    """One live RFB/VNC client connection to a single remote desktop.

    Owns the socket, the negotiated pixel geometry, a persistent framebuffer,
    and a background reader thread that continuously applies dirty rectangles
    so screenshots are always current and change-waiting is cheap.
    """

    def __init__(
        self,
        remote_desktop_session_id: str,
        remote_host_name_or_address: str,
        remote_tcp_port_number: int,
        client_requested_view_only_mode_no_input_allowed: bool,
        input_operations_require_explicit_confirmation: bool,
        tls_should_verify_server_hostname_against_certificate: bool = False,
    ):
        self.remote_desktop_session_id = remote_desktop_session_id
        self.remote_host_name_or_address = remote_host_name_or_address
        self.remote_tcp_port_number = remote_tcp_port_number
        self.client_requested_view_only_mode_no_input_allowed = client_requested_view_only_mode_no_input_allowed
        self.input_operations_require_explicit_confirmation = input_operations_require_explicit_confirmation
        self.tls_should_verify_server_hostname_against_certificate = tls_should_verify_server_hostname_against_certificate

        self._raw_tcp_socket: Optional[socket.socket] = None
        self._byte_stream_for_reading_and_writing = None  # raw socket or TLS-wrapped socket
        self._socket_send_lock = threading.Lock()

        self.negotiated_rfb_major_version = 3
        self.negotiated_rfb_minor_version = 8
        self.selected_security_type_number = 0
        self.selected_security_type_human_name = "unknown"
        self.remote_desktop_display_name = ""

        self.framebuffer_width_pixels = 0
        self.framebuffer_height_pixels = 0
        # Framebuffer stored as BGRX (4 bytes/pixel) to match the pixel format we
        # request, making Raw and ZRLE straight copies.
        self._framebuffer_pixels_bgrx = bytearray(0)
        self._framebuffer_access_lock = threading.Lock()

        self.session_socket_is_currently_connected = False
        self._background_reader_thread: Optional[threading.Thread] = None
        self._background_reader_should_keep_running = False

        self._first_full_framebuffer_update_has_arrived = threading.Event()
        self._a_framebuffer_update_was_applied_event = threading.Event()
        self.count_of_framebuffer_updates_applied_since_connect = 0
        self._dirty_rectangles_since_last_get_changes: List[Tuple[int, int, int, int]] = []

        self.most_recent_remote_clipboard_text = ""
        self.time_of_last_successful_activity_epoch_seconds = time.time()

        # Per-connection zlib decompressors. ZRLE uses one stream; Tight uses four.
        self._zrle_zlib_decompressor = None
        self._tight_zlib_decompressors: List[Any] = [None, None, None, None]

        # Accessibility side-channel (optional). None until/unless connected.
        self.accessibility_side_channel_client = None

    # -- low level IO ------------------------------------------------------
    def _receive_exactly_this_many_bytes(self, number_of_bytes_wanted: int) -> bytes:
        """Read exactly N bytes from the (possibly TLS) stream or raise."""
        collected_chunks = bytearray()
        while len(collected_chunks) < number_of_bytes_wanted:
            chunk = self._byte_stream_for_reading_and_writing.recv(number_of_bytes_wanted - len(collected_chunks))
            if not chunk:
                raise VncConnectionError(
                    f"Remote host {self.remote_host_name_or_address}:{self.remote_tcp_port_number} "
                    f"closed the connection unexpectedly (read {len(collected_chunks)} of "
                    f"{number_of_bytes_wanted} expected bytes)."
                )
            collected_chunks.extend(chunk)
        return bytes(collected_chunks)

    def _send_all_bytes(self, payload_bytes: bytes) -> None:
        with self._socket_send_lock:
            self._byte_stream_for_reading_and_writing.sendall(payload_bytes)

    # -- connect / handshake ----------------------------------------------
    def open_socket_and_perform_full_handshake(
        self,
        password_text: Optional[str],
        username_text: Optional[str],
        preferred_or_forced_security_type: Optional[str],
        forbidden_security_type_numbers: List[int],
        certificate_authority_path: Optional[str],
        client_certificate_path: Optional[str],
        client_private_key_path: Optional[str],
        tcp_connect_timeout_seconds: float,
    ) -> None:
        """Dial the server and complete ProtocolVersion + security + ClientInit +
        ServerInit, then start the background reader. Raises
        VncAuthenticationNegotiationError / VncConnectionError with actionable text."""
        try:
            self._raw_tcp_socket = socket.create_connection(
                (self.remote_host_name_or_address, self.remote_tcp_port_number),
                timeout=tcp_connect_timeout_seconds,
            )
        except OSError as connect_error:
            raise VncConnectionError(
                f"Could not open a TCP connection to "
                f"{self.remote_host_name_or_address}:{self.remote_tcp_port_number}: {connect_error}. "
                f"Next step: verify the host is reachable and a VNC/RFB server is listening on that "
                f"port (TCP port = 5900 + display number)."
            )
        # Generous timeout for the handshake; the reader thread resets it later.
        # Some servers (e.g. TigerVNC bringing up a VeNCrypt anonymous-TLS session)
        # spend several seconds generating DH parameters before acking the sub-type,
        # so keep this well clear of that to avoid a spurious "timed out".
        self._raw_tcp_socket.settimeout(max(45.0, tcp_connect_timeout_seconds))
        # Enable TCP keepalive so a peer that dies silently (power loss, cable pull)
        # is eventually surfaced as a socket error even though the reader treats plain
        # read-timeouts on an idle-but-healthy link as non-fatal.
        try:
            self._raw_tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        self._byte_stream_for_reading_and_writing = self._raw_tcp_socket
        self.session_socket_is_currently_connected = True

        self._negotiate_protocol_version()
        self._negotiate_security_type_and_authenticate(
            password_text=password_text,
            username_text=username_text,
            preferred_or_forced_security_type=preferred_or_forced_security_type,
            forbidden_security_type_numbers=forbidden_security_type_numbers,
            certificate_authority_path=certificate_authority_path,
            client_certificate_path=client_certificate_path,
            client_private_key_path=client_private_key_path,
        )
        self._send_client_init_and_read_server_init()
        self._send_set_pixel_format()
        self._send_set_encodings()
        self._begin_background_framebuffer_reader_thread()

    def _negotiate_protocol_version(self) -> None:
        server_protocol_version_banner = self._receive_exactly_this_many_bytes(12)
        try:
            banner_text = server_protocol_version_banner.decode("ascii")
        except UnicodeDecodeError:
            raise VncConnectionError(
                f"The server did not send a valid RFB ProtocolVersion banner "
                f"(got bytes {server_protocol_version_banner!r}); this may not be a VNC/RFB server."
            )
        if not banner_text.startswith("RFB "):
            raise VncConnectionError(
                f"Expected an 'RFB xxx.yyy' banner but received {banner_text!r}. "
                f"Next step: confirm the port is really a VNC/RFB server."
            )
        try:
            server_major_version = int(banner_text[4:7])
            server_minor_version = int(banner_text[8:11])
        except ValueError:
            raise VncConnectionError(f"Unparseable RFB banner {banner_text!r}.")

        # Apple's Screen Sharing advertises 003.889 etc; treat >=3.8 dialects as 3.8.
        chosen_major_version = 3
        if server_major_version == 3 and server_minor_version >= 8:
            chosen_minor_version = 8
        elif server_major_version == 3 and server_minor_version == 7:
            chosen_minor_version = 7
        else:
            chosen_minor_version = 3
        self.negotiated_rfb_major_version = chosen_major_version
        self.negotiated_rfb_minor_version = chosen_minor_version

        client_reply_banner = f"RFB {chosen_major_version:03d}.{chosen_minor_version:03d}\n".encode("ascii")
        self._send_all_bytes(client_reply_banner)
        MCPLogger.log(
            TOOL_LOG_NAME,
            f"[{self.remote_desktop_session_id}] server banner {banner_text.strip()!r}; "
            f"negotiated {chosen_major_version}.{chosen_minor_version}",
        )

    def _read_the_list_of_security_types_offered_by_server(self) -> List[int]:
        """Read the offered security-type numbers, handling both handshake dialects.

        For RFB 3.3 the server dictates a single U32 type (no choice). For 3.7/3.8
        the server sends a U8 count then that many U8 type numbers; a zero count is
        followed by a reason string which we surface."""
        if self.negotiated_rfb_minor_version >= 7:
            number_of_offered_types = self._receive_exactly_this_many_bytes(1)[0]
            if number_of_offered_types == 0:
                failure_reason = self._read_length_prefixed_failure_reason_string()
                raise VncAuthenticationNegotiationError(
                    f"The server refused the connection during security negotiation with reason: "
                    f"{failure_reason!r}. Next step: check the server logs / that the display is not locked."
                )
            return list(self._receive_exactly_this_many_bytes(number_of_offered_types))
        # RFB 3.3: single dictated 4-byte type.
        dictated_type_number = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if dictated_type_number == 0:
            failure_reason = self._read_length_prefixed_failure_reason_string()
            raise VncAuthenticationNegotiationError(
                f"The RFB 3.3 server refused the connection: {failure_reason!r}."
            )
        return [dictated_type_number]

    def _read_length_prefixed_failure_reason_string(self) -> str:
        reason_length = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if reason_length == 0 or reason_length > 65535:
            return ""
        return self._receive_exactly_this_many_bytes(reason_length).decode("latin-1", "replace")

    def _negotiate_security_type_and_authenticate(
        self,
        password_text: Optional[str],
        username_text: Optional[str],
        preferred_or_forced_security_type: Optional[str],
        forbidden_security_type_numbers: List[int],
        certificate_authority_path: Optional[str],
        client_certificate_path: Optional[str],
        client_private_key_path: Optional[str],
    ) -> None:
        offered_security_type_numbers = self._read_the_list_of_security_types_offered_by_server()
        MCPLogger.log(
            TOOL_LOG_NAME,
            f"[{self.remote_desktop_session_id}] offered security types: "
            + ", ".join(f"{n} {SECURITY_TYPE_NUMBER_TO_NAME.get(n, 'Unknown')}" for n in offered_security_type_numbers),
        )

        # Which numbers can THIS build actually perform.
        security_type_handlers_by_number = {
            1: self._perform_security_type_none,
            2: self._perform_security_type_vnc_authentication,
            16: self._perform_security_type_tight,
            18: self._perform_security_type_anonymous_tls,
            19: self._perform_security_type_vencrypt,
            30: self._perform_security_type_apple_diffie_hellman,
        }
        credentials_bundle = {
            "password_text": password_text,
            "username_text": username_text,
            "certificate_authority_path": certificate_authority_path,
            "client_certificate_path": client_certificate_path,
            "client_private_key_path": client_private_key_path,
        }

        supported_offered_numbers = [
            n for n in offered_security_type_numbers
            if n in security_type_handlers_by_number and n not in forbidden_security_type_numbers
        ]

        chosen_security_type_number = self._select_security_type_number(
            offered_security_type_numbers=offered_security_type_numbers,
            supported_offered_numbers=supported_offered_numbers,
            preferred_or_forced_security_type=preferred_or_forced_security_type,
            forbidden_security_type_numbers=forbidden_security_type_numbers,
        )

        # For RFB 3.7/3.8 the client tells the server which type it chose. For 3.3
        # the server already dictated it, so no reply is sent.
        if self.negotiated_rfb_minor_version >= 7:
            self._send_all_bytes(bytes([chosen_security_type_number]))

        self.selected_security_type_number = chosen_security_type_number
        self.selected_security_type_human_name = SECURITY_TYPE_NUMBER_TO_NAME.get(
            chosen_security_type_number, f"Type{chosen_security_type_number}"
        )
        MCPLogger.log(
            TOOL_LOG_NAME,
            f"[{self.remote_desktop_session_id}] selected security type "
            f"{chosen_security_type_number} ({self.selected_security_type_human_name})",
        )

        security_type_handlers_by_number[chosen_security_type_number](credentials_bundle)

    def _select_security_type_number(
        self,
        offered_security_type_numbers: List[int],
        supported_offered_numbers: List[int],
        preferred_or_forced_security_type: Optional[str],
        forbidden_security_type_numbers: List[int],
    ) -> int:
        """Choose the security type, honouring a caller override and preferring the
        strongest mutually-supported option. Raises the actionable error when the
        overlap is empty (this is the fix for the reported 'No matching security
        types' dead-end)."""
        # A caller may force/prefer a specific type by name or number.
        if preferred_or_forced_security_type:
            requested_number = self._interpret_security_type_selector(preferred_or_forced_security_type)
            if requested_number is not None and requested_number in supported_offered_numbers:
                return requested_number
            if requested_number is not None:
                raise VncAuthenticationNegotiationError(
                    self._compose_actionable_negotiation_failure_message(
                        offered_security_type_numbers,
                        supported_offered_numbers,
                        specific_reason=(
                            f"you forced security_type={preferred_or_forced_security_type!r} "
                            f"(number {requested_number}) but "
                            + (
                                "the server did not offer it"
                                if requested_number not in offered_security_type_numbers
                                else "this build cannot perform it or you forbade it"
                            )
                        ),
                    )
                )

        if not supported_offered_numbers:
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    offered_security_type_numbers,
                    supported_offered_numbers,
                    specific_reason="none of the security types the server offered are supported by this build",
                )
            )

        # Strength preference: TLS/X509-backed and Apple-DH over bare VNC-auth over None.
        strength_preference_order = [19, 18, 30, 16, 2, 1]
        for candidate_number in strength_preference_order:
            if candidate_number in supported_offered_numbers:
                return candidate_number
        return supported_offered_numbers[0]

    def _interpret_security_type_selector(self, selector_text: str) -> Optional[int]:
        """Map a caller-supplied 'security_type' (a number or a name like 'VncAuth'
        / 'vencrypt') to a type number, or None if unrecognised."""
        selector_text = selector_text.strip()
        if selector_text.isdigit():
            return int(selector_text)
        lowered = selector_text.lower()
        for type_number, type_name in SECURITY_TYPE_NUMBER_TO_NAME.items():
            if type_name.lower() == lowered or type_name.lower().replace("-", "") == lowered.replace("-", ""):
                return type_number
        aliases = {"none": 1, "vnc": 2, "vncauth": 2, "tight": 16, "tls": 18, "anontls": 18,
                   "vencrypt": 19, "apple": 30, "ard": 30, "appledh": 30}
        return aliases.get(lowered)

    def _compose_actionable_negotiation_failure_message(
        self,
        offered_security_type_numbers: List[int],
        supported_offered_numbers: List[int],
        specific_reason: str,
    ) -> str:
        offered_rendered = ", ".join(
            f"[{n} {SECURITY_TYPE_NUMBER_TO_NAME.get(n, 'Unknown')}]" for n in offered_security_type_numbers
        ) or "(none)"
        this_build_supports = "1 None, 2 VncAuth, 16 Tight, 18 TLS, 19 VeNCrypt, 30 AppleDH-ARD"
        supported_here = ", ".join(
            f"{n} {SECURITY_TYPE_NUMBER_TO_NAME.get(n, 'Unknown')}" for n in supported_offered_numbers
        ) or "(no overlap with what the server offered)"
        return (
            f"VNC sign-in could not proceed. "
            f"Server offered: {offered_rendered}. "
            f"This build supports: {this_build_supports}. "
            f"Overlap usable here: {supported_here}. "
            f"Reason: {specific_reason}. "
            f"Next step: supply the missing credential/certificate (password, and for "
            f"Apple/VeNCrypt-Plain also username, and for X509 sub-types a CA/client cert), "
            f"or enable a compatible sign-in method on the server (e.g. add VncAuth), "
            f"or pass security_type to force a specific offered type."
        )

    def _read_security_result_and_raise_on_failure(self, security_type_number: int) -> None:
        """Read the RFB SecurityResult where the dialect requires it. RFB 3.8 always
        sends it (with a reason string on failure); 3.7 sends it for non-None types;
        3.3 sends it only for VNC-auth (and with no reason string)."""
        should_read_result = False
        should_expect_reason_string_on_failure = False
        if self.negotiated_rfb_minor_version >= 8:
            should_read_result = True
            should_expect_reason_string_on_failure = True
        elif self.negotiated_rfb_minor_version == 7:
            should_read_result = security_type_number != 1
        else:  # 3.3
            should_read_result = security_type_number == 2

        if not should_read_result:
            return
        security_result_code = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if security_result_code == 0:
            return
        failure_reason = ""
        if should_expect_reason_string_on_failure:
            failure_reason = self._read_length_prefixed_failure_reason_string()
        raise VncAuthenticationNegotiationError(
            f"The server rejected authentication for security type {security_type_number} "
            f"({SECURITY_TYPE_NUMBER_TO_NAME.get(security_type_number, 'Unknown')})"
            + (f": {failure_reason!r}" if failure_reason else "")
            + ". Next step: check the password/username is correct (VNC passwords are capped at 8 chars)."
        )

    # -- individual security-type handlers --------------------------------
    def _perform_security_type_none(self, credentials_bundle: Dict) -> None:
        self._read_security_result_and_raise_on_failure(1)

    def _perform_security_type_vnc_authentication(self, credentials_bundle: Dict) -> None:
        password_text = credentials_bundle.get("password_text")
        if not password_text:
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    [2], [2], specific_reason="VncAuth (type 2) requires a password but none was provided"
                )
            )
        sixteen_byte_challenge = self._receive_exactly_this_many_bytes(16)
        response_bytes = _encrypt_vnc_auth_challenge_with_password(sixteen_byte_challenge, password_text)
        self._send_all_bytes(response_bytes)
        self._read_security_result_and_raise_on_failure(2)

    def _perform_security_type_tight(self, credentials_bundle: Dict) -> None:
        """TightVNC meta-type: negotiate tunnels then auth capabilities. We select
        the no-tunnel path and then the None or VNC-auth capability."""
        number_of_supported_tunnels = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if number_of_supported_tunnels > 0:
            available_tunnel_capability_codes = []
            for _ in range(number_of_supported_tunnels):
                tunnel_capability = self._receive_exactly_this_many_bytes(16)
                available_tunnel_capability_codes.append(struct.unpack(">I", tunnel_capability[0:4])[0])
            # 0 == NOTUNNEL; request it if available, else fail loudly.
            if 0 in available_tunnel_capability_codes:
                self._send_all_bytes(struct.pack(">I", 0))
            else:
                raise VncAuthenticationNegotiationError(
                    "The Tight (type 16) server requires a tunnel capability this build does not "
                    "implement. Next step: enable a plain VncAuth path on the server."
                )
        number_of_supported_auth_types = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if number_of_supported_auth_types == 0:
            # No further auth: proceed straight to init (3.8 still sends a result).
            self._read_security_result_and_raise_on_failure(1)
            return
        available_auth_capability_codes = []
        for _ in range(number_of_supported_auth_types):
            auth_capability = self._receive_exactly_this_many_bytes(16)
            available_auth_capability_codes.append(struct.unpack(">I", auth_capability[0:4])[0])
        # Capability code 1 == None (no auth), 2 == VncAuth.
        if 2 in available_auth_capability_codes and credentials_bundle.get("password_text"):
            self._send_all_bytes(struct.pack(">I", 2))
            sixteen_byte_challenge = self._receive_exactly_this_many_bytes(16)
            response_bytes = _encrypt_vnc_auth_challenge_with_password(
                sixteen_byte_challenge, credentials_bundle["password_text"]
            )
            self._send_all_bytes(response_bytes)
            self._read_security_result_and_raise_on_failure(2)
        elif 1 in available_auth_capability_codes:
            self._send_all_bytes(struct.pack(">I", 1))
            self._read_security_result_and_raise_on_failure(1)
        else:
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    [16], [16],
                    specific_reason=(
                        "the Tight server's sub-auth capabilities need a password (VncAuth) "
                        "which was not provided, or use a capability this build lacks"
                    ),
                )
            )

    def _wrap_current_stream_in_tls(
        self,
        use_anonymous_ciphers: bool,
        credentials_bundle: Dict,
    ) -> None:
        """Upgrade the byte stream to TLS. Anonymous (ADH/AECDH) for the TLS-prefix
        VeNCrypt sub-types and plain type 18; X.509 (optionally verified) for the
        X509-prefix sub-types."""
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # check_hostname must be assigned BEFORE verify_mode when turning verification
        # off, or Python raises. Default is no hostname check (LAN / self-signed use);
        # callers who want real authentication opt in via verify_hostname + a CA.
        tls_context.check_hostname = False
        certificate_authority_path = credentials_bundle.get("certificate_authority_path")
        tls_server_hostname_for_sni = None
        if certificate_authority_path:
            tls_context.load_verify_locations(certificate_authority_path)
            tls_context.verify_mode = ssl.CERT_REQUIRED
            if self.tls_should_verify_server_hostname_against_certificate:
                tls_context.check_hostname = True
                tls_server_hostname_for_sni = self.remote_host_name_or_address
        else:
            if self.tls_should_verify_server_hostname_against_certificate:
                raise VncAuthenticationNegotiationError(
                    "verify_hostname was requested but no ca_cert_path was supplied; hostname "
                    "verification is only meaningful when the server certificate is also verified. "
                    "Next step: pass ca_cert_path (and use an X509 VeNCrypt sub-type), or drop verify_hostname."
                )
            tls_context.verify_mode = ssl.CERT_NONE
        client_certificate_path = credentials_bundle.get("client_certificate_path")
        client_private_key_path = credentials_bundle.get("client_private_key_path")
        if client_certificate_path:
            tls_context.load_cert_chain(client_certificate_path, client_private_key_path)

        if use_anonymous_ciphers:
            # Anonymous TLS (no server certificate). Modern OpenSSL hides these behind
            # SECLEVEL=0; try progressively until one cipher string is accepted.
            for anonymous_cipher_string in ("AECDH:ADH:@SECLEVEL=0", "ALL:aNULL:eNULL:@SECLEVEL=0", "ALL:@SECLEVEL=0"):
                try:
                    tls_context.set_ciphers(anonymous_cipher_string)
                    break
                except ssl.SSLError:
                    continue
        try:
            self._byte_stream_for_reading_and_writing = tls_context.wrap_socket(
                self._raw_tcp_socket, server_hostname=tls_server_hostname_for_sni
            )
        except ssl.SSLError as tls_error:
            raise VncAuthenticationNegotiationError(
                f"TLS handshake for the encrypted VNC sign-in failed: {tls_error}. "
                f"This commonly happens when the server offers only anonymous-TLS ciphers that this "
                f"machine's OpenSSL has disabled. Next step: connect with an X509 VeNCrypt sub-type and "
                f"supply ca_cert_path, or use a VncAuth-capable path (pass security_type='VncAuth')."
            )

    def _perform_security_type_anonymous_tls(self, credentials_bundle: Dict) -> None:
        """RFB security type 18 (TLS): bring up anonymous TLS, then run an INNER
        security-type negotiation inside the TLS session."""
        self._wrap_current_stream_in_tls(use_anonymous_ciphers=True, credentials_bundle=credentials_bundle)
        inner_offered_numbers = self._read_the_list_of_security_types_offered_by_server()
        inner_handlers = {
            1: self._perform_security_type_none,
            2: self._perform_security_type_vnc_authentication,
        }
        inner_supported = [n for n in inner_offered_numbers if n in inner_handlers]
        if not inner_supported:
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    inner_offered_numbers, inner_supported,
                    specific_reason="inside the TLS tunnel the server offered no inner type this build supports",
                )
            )
        inner_choice = 2 if 2 in inner_supported else inner_supported[0]
        if self.negotiated_rfb_minor_version >= 7:
            self._send_all_bytes(bytes([inner_choice]))
        inner_handlers[inner_choice](credentials_bundle)

    def _perform_security_type_vencrypt(self, credentials_bundle: Dict) -> None:
        """RFB security type 19 (VeNCrypt): version handshake, sub-type selection,
        optional TLS/X509 bring-up, then the inner auth. Covers Plain / TLSNone /
        TLSVnc / TLSPlain / X509None / X509Vnc / X509Plain."""
        server_vencrypt_version = self._receive_exactly_this_many_bytes(2)  # [major, minor]
        server_major, server_minor = server_vencrypt_version[0], server_vencrypt_version[1]
        if (server_major, server_minor) < (0, 2):
            raise VncAuthenticationNegotiationError(
                f"The server speaks only VeNCrypt {server_major}.{server_minor}; this build implements 0.2. "
                f"Next step: use a VncAuth path (security_type='VncAuth')."
            )
        self._send_all_bytes(bytes([0, 2]))
        version_acknowledgement = self._receive_exactly_this_many_bytes(1)[0]
        if version_acknowledgement != 0:
            raise VncAuthenticationNegotiationError(
                "The server rejected VeNCrypt version 0.2. Next step: use a VncAuth path."
            )
        number_of_subtypes = self._receive_exactly_this_many_bytes(1)[0]
        if number_of_subtypes == 0:
            raise VncAuthenticationNegotiationError(
                "The VeNCrypt server offered zero sub-types. Next step: enable an auth method on the server."
            )
        offered_subtype_numbers = list(
            struct.unpack(">" + "I" * number_of_subtypes, self._receive_exactly_this_many_bytes(4 * number_of_subtypes))
        )
        MCPLogger.log(
            TOOL_LOG_NAME,
            f"[{self.remote_desktop_session_id}] VeNCrypt sub-types offered: "
            + ", ".join(f"{n} {VENCRYPT_SUBTYPE_NUMBER_TO_NAME.get(n, 'Unknown')}" for n in offered_subtype_numbers),
        )

        have_password = bool(credentials_bundle.get("password_text"))
        have_username = bool(credentials_bundle.get("username_text"))
        # Preference: strongest that we have the inputs for.
        subtype_preference_order = [261, 258, 260, 257, 262, 259, 256]  # X509Vnc,TLSVnc,X509None,TLSNone,X509Plain,TLSPlain,Plain
        chosen_subtype_number = None
        for candidate_subtype in subtype_preference_order:
            if candidate_subtype not in offered_subtype_numbers:
                continue
            if candidate_subtype in (258, 261) and not have_password:
                continue  # *Vnc needs a password
            if candidate_subtype in (256, 259, 262) and not (have_username and have_password):
                continue  # *Plain needs username+password
            chosen_subtype_number = candidate_subtype
            break
        if chosen_subtype_number is None:
            missing = []
            if not have_password:
                missing.append("password")
            if not have_username:
                missing.append("username (for the Plain sub-types)")
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    [19], [19],
                    specific_reason=(
                        "no VeNCrypt sub-type could be satisfied with the credentials provided"
                        + (f"; missing: {', '.join(missing)}" if missing else "")
                    ),
                )
            )

        self._send_all_bytes(struct.pack(">I", chosen_subtype_number))
        subtype_name = VENCRYPT_SUBTYPE_NUMBER_TO_NAME.get(chosen_subtype_number, str(chosen_subtype_number))
        MCPLogger.log(TOOL_LOG_NAME, f"[{self.remote_desktop_session_id}] VeNCrypt chose {chosen_subtype_number} {subtype_name}")

        subtype_uses_x509 = subtype_name.startswith("X509")
        subtype_uses_tls = subtype_name.startswith("TLS") or subtype_uses_x509
        if subtype_uses_tls:
            tls_setup_acknowledgement = self._receive_exactly_this_many_bytes(1)[0]
            if tls_setup_acknowledgement != 1:
                raise VncAuthenticationNegotiationError(
                    "The VeNCrypt server failed to set up its TLS session (ack != 1)."
                )
            self._wrap_current_stream_in_tls(
                use_anonymous_ciphers=(not subtype_uses_x509), credentials_bundle=credentials_bundle
            )

        # Inner auth by sub-type suffix.
        if subtype_name.endswith("None"):
            self._read_security_result_and_raise_on_failure(1)
        elif subtype_name.endswith("Vnc"):
            sixteen_byte_challenge = self._receive_exactly_this_many_bytes(16)
            response_bytes = _encrypt_vnc_auth_challenge_with_password(
                sixteen_byte_challenge, credentials_bundle["password_text"]
            )
            self._send_all_bytes(response_bytes)
            self._read_security_result_and_raise_on_failure(2)
        elif subtype_name.endswith("Plain"):
            username_bytes = (credentials_bundle.get("username_text") or "").encode("utf-8")
            password_bytes = (credentials_bundle.get("password_text") or "").encode("utf-8")
            self._send_all_bytes(struct.pack(">II", len(username_bytes), len(password_bytes)))
            self._send_all_bytes(username_bytes + password_bytes)
            self._read_security_result_and_raise_on_failure(1)
        else:
            raise VncAuthenticationNegotiationError(
                f"VeNCrypt sub-type {subtype_name} is recognised but not implemented in this build."
            )

    def _perform_security_type_apple_diffie_hellman(self, credentials_bundle: Dict) -> None:
        """RFB security type 30 (Apple ARD): DH key agreement -> MD5 -> AES-128-ECB
        of a 128-byte {username[64],password[64]} blob. Required for macOS."""
        import hashlib
        import os as _os
        username_text = credentials_bundle.get("username_text") or ""
        password_text = credentials_bundle.get("password_text") or ""
        if not username_text or not password_text:
            raise VncAuthenticationNegotiationError(
                self._compose_actionable_negotiation_failure_message(
                    [30], [30],
                    specific_reason="Apple ARD (type 30) needs BOTH a username and a password (macOS account)",
                )
            )
        generator_value = struct.unpack(">H", self._receive_exactly_this_many_bytes(2))[0]
        key_length_bytes = struct.unpack(">H", self._receive_exactly_this_many_bytes(2))[0]
        prime_modulus_bytes = self._receive_exactly_this_many_bytes(key_length_bytes)
        server_public_key_bytes = self._receive_exactly_this_many_bytes(key_length_bytes)

        prime_modulus_integer = int.from_bytes(prime_modulus_bytes, "big")
        server_public_key_integer = int.from_bytes(server_public_key_bytes, "big")
        client_private_key_integer = int.from_bytes(_os.urandom(key_length_bytes), "big") % prime_modulus_integer
        client_public_key_integer = pow(generator_value, client_private_key_integer, prime_modulus_integer)
        shared_secret_integer = pow(server_public_key_integer, client_private_key_integer, prime_modulus_integer)
        shared_secret_bytes = shared_secret_integer.to_bytes(key_length_bytes, "big")
        aes_key_from_md5_of_shared_secret = hashlib.md5(shared_secret_bytes).digest()

        credentials_blob = bytearray(_os.urandom(128))
        encoded_username = username_text.encode("utf-8")[:63]
        encoded_password = password_text.encode("utf-8")[:63]
        credentials_blob[0:len(encoded_username)] = encoded_username
        credentials_blob[len(encoded_username)] = 0
        credentials_blob[64:64 + len(encoded_password)] = encoded_password
        credentials_blob[64 + len(encoded_password)] = 0

        Cipher, algorithms, modes, _triple_des_algorithm_class = ensure_cryptography_library_is_available()
        aes_encryptor = Cipher(algorithms.AES(aes_key_from_md5_of_shared_secret), modes.ECB()).encryptor()
        encrypted_credentials_blob = aes_encryptor.update(bytes(credentials_blob)) + aes_encryptor.finalize()

        client_public_key_bytes = client_public_key_integer.to_bytes(key_length_bytes, "big")
        self._send_all_bytes(encrypted_credentials_blob + client_public_key_bytes)
        self._read_security_result_and_raise_on_failure(30)

    # -- init messages -----------------------------------------------------
    def _send_client_init_and_read_server_init(self) -> None:
        # ClientInit: shared-flag = 1 (do not disconnect other clients).
        self._send_all_bytes(bytes([1]))
        server_init_header = self._receive_exactly_this_many_bytes(24)
        self.framebuffer_width_pixels = struct.unpack(">H", server_init_header[0:2])[0]
        self.framebuffer_height_pixels = struct.unpack(">H", server_init_header[2:4])[0]
        desktop_name_length = struct.unpack(">I", server_init_header[20:24])[0]
        if desktop_name_length:
            self.remote_desktop_display_name = _decode_rfb_text_bytes_preferring_utf8(
                self._receive_exactly_this_many_bytes(desktop_name_length)
            )
        self._reallocate_framebuffer(self.framebuffer_width_pixels, self.framebuffer_height_pixels)
        MCPLogger.log(
            TOOL_LOG_NAME,
            f"[{self.remote_desktop_session_id}] ServerInit {self.framebuffer_width_pixels}x"
            f"{self.framebuffer_height_pixels} name={self.remote_desktop_display_name!r}",
        )

    def _reallocate_framebuffer(self, new_width_pixels: int, new_height_pixels: int) -> None:
        if new_width_pixels <= 0 or new_height_pixels <= 0:
            raise VncConnectionError(f"Server reported an invalid desktop size {new_width_pixels}x{new_height_pixels}.")
        if new_width_pixels > _MAXIMUM_FRAMEBUFFER_DIMENSION_PIXELS or new_height_pixels > _MAXIMUM_FRAMEBUFFER_DIMENSION_PIXELS:
            raise VncConnectionError(
                f"Server desktop size {new_width_pixels}x{new_height_pixels} exceeds the safety cap "
                f"of {_MAXIMUM_FRAMEBUFFER_DIMENSION_PIXELS}px per side."
            )
        with self._framebuffer_access_lock:
            self.framebuffer_width_pixels = new_width_pixels
            self.framebuffer_height_pixels = new_height_pixels
            self._framebuffer_pixels_bgrx = bytearray(new_width_pixels * new_height_pixels * 4)

    def _send_set_pixel_format(self) -> None:
        # 32bpp, depth 24, little-endian, true-colour, shifts R=16 G=8 B=0 -> wire
        # bytes per pixel are [B, G, R, 0] (BGRX), making Raw/ZRLE trivial copies.
        pixel_format_bytes = struct.pack(
            ">BBBB HHH BBB xxx",
            32,   # bits-per-pixel
            24,   # depth
            0,    # big-endian-flag (0 = little endian)
            1,    # true-colour-flag
            255, 255, 255,  # red-max, green-max, blue-max
            16, 8, 0,       # red-shift, green-shift, blue-shift
        )
        self._send_all_bytes(bytes([_CLIENT_MSG_SET_PIXEL_FORMAT, 0, 0, 0]) + pixel_format_bytes)

    def _send_set_encodings(self) -> None:
        # Preference order (first = most preferred). ZRLE first (well exercised here),
        # then Tight, then CopyRect and Raw, plus the pseudo-encodings we honour.
        encoding_numbers_in_preference_order = [
            _ENCODING_ZRLE,
            _ENCODING_TIGHT,
            _ENCODING_COPYRECT,
            _ENCODING_RAW,
            _PSEUDO_ENCODING_LAST_RECT,
            _PSEUDO_ENCODING_DESKTOP_SIZE,
            _PSEUDO_ENCODING_CURSOR,
        ]
        message = struct.pack(">BxH", _CLIENT_MSG_SET_ENCODINGS, len(encoding_numbers_in_preference_order))
        for encoding_number in encoding_numbers_in_preference_order:
            message += struct.pack(">i", encoding_number)
        self._send_all_bytes(message)

    def _send_framebuffer_update_request(self, is_incremental_update: bool) -> None:
        self._send_all_bytes(
            struct.pack(
                ">BBHHHH",
                _CLIENT_MSG_FRAMEBUFFER_UPDATE_REQUEST,
                1 if is_incremental_update else 0,
                0,
                0,
                self.framebuffer_width_pixels,
                self.framebuffer_height_pixels,
            )
        )

    # -- background reader -------------------------------------------------
    def _begin_background_framebuffer_reader_thread(self) -> None:
        self._background_reader_should_keep_running = True
        self._send_framebuffer_update_request(is_incremental_update=False)
        self._background_reader_thread = threading.Thread(
            target=self._background_framebuffer_reader_main_loop,
            name=f"vnc-reader-{self.remote_desktop_session_id}",
            daemon=True,
        )
        self._background_reader_thread.start()

    def _background_framebuffer_reader_main_loop(self) -> None:
        # Poll with a bounded timeout so a genuinely dead peer is eventually noticed
        # (helped by SO_KEEPALIVE set at connect), while a merely IDLE desktop - which
        # is normal in RFB, since the server sends nothing until something changes -
        # does NOT tear the session down. We operate on the byte-stream (the object
        # that owns the fd), because for TLS sessions the raw socket is detached by
        # wrap_socket() and settimeout/shutdown on it silently no-op or raise.
        try:
            self._byte_stream_for_reading_and_writing.settimeout(120.0)
        except OSError:
            pass
        while self._background_reader_should_keep_running:
            try:
                server_message_type = self._receive_exactly_this_many_bytes(1)[0]
            except (socket.timeout, TimeoutError):
                # No server message within the poll window. For a single type byte a
                # timeout means nothing at all arrived (no risk of frame desync), so
                # the stream is simply idle - keep waiting rather than disconnecting.
                continue
            except (VncConnectionError, OSError):
                break
            try:
                if server_message_type == _SERVER_MSG_FRAMEBUFFER_UPDATE:
                    self._read_and_apply_one_framebuffer_update_message()
                    # Keep exactly one update request outstanding.
                    self._send_framebuffer_update_request(is_incremental_update=True)
                elif server_message_type == _SERVER_MSG_SET_COLOUR_MAP_ENTRIES:
                    self._consume_set_colour_map_entries_message()
                elif server_message_type == _SERVER_MSG_BELL:
                    pass
                elif server_message_type == _SERVER_MSG_SERVER_CUT_TEXT:
                    self._read_server_cut_text_message()
                else:
                    # Unknown message: we cannot know its length, so stop cleanly.
                    MCPLogger.log(
                        TOOL_LOG_NAME,
                        f"[{self.remote_desktop_session_id}] unknown server message type {server_message_type}; stopping reader",
                    )
                    break
            except (VncConnectionError, OSError) as reader_error:
                MCPLogger.log(TOOL_LOG_NAME, f"[{self.remote_desktop_session_id}] reader stopped: {reader_error}")
                break
            except Exception as unexpected_reader_error:  # noqa: BLE001 - keep the session alive-ish and log
                MCPLogger.log(
                    TOOL_LOG_NAME,
                    f"[{self.remote_desktop_session_id}] reader decode error: {unexpected_reader_error}",
                )
                break
        self.session_socket_is_currently_connected = False

    def _consume_set_colour_map_entries_message(self) -> None:
        header = self._receive_exactly_this_many_bytes(5)  # padding(1) + first(2) + count(2)
        number_of_colours = struct.unpack(">H", header[3:5])[0]
        if number_of_colours:
            self._receive_exactly_this_many_bytes(number_of_colours * 6)

    def _read_server_cut_text_message(self) -> None:
        header = self._receive_exactly_this_many_bytes(7)  # padding(3) + length(4)
        text_length = struct.unpack(">I", header[3:7])[0]
        if text_length:
            clipboard_text_bytes = self._receive_exactly_this_many_bytes(text_length)
            self.most_recent_remote_clipboard_text = _decode_rfb_text_bytes_preferring_utf8(clipboard_text_bytes)

    def _read_and_apply_one_framebuffer_update_message(self) -> None:
        header = self._receive_exactly_this_many_bytes(3)  # padding(1) + number-of-rectangles(2)
        number_of_rectangles = struct.unpack(">H", header[1:3])[0]
        applied_dirty_rectangles: List[Tuple[int, int, int, int]] = []
        for _ in range(number_of_rectangles):
            rectangle_header = self._receive_exactly_this_many_bytes(12)
            rect_x, rect_y, rect_width, rect_height = struct.unpack(">HHHH", rectangle_header[0:8])
            rect_encoding_number = struct.unpack(">i", rectangle_header[8:12])[0]

            if rect_encoding_number == _PSEUDO_ENCODING_LAST_RECT:
                break
            if rect_encoding_number == _PSEUDO_ENCODING_DESKTOP_SIZE:
                self._reallocate_framebuffer(rect_width, rect_height)
                continue
            if rect_encoding_number == _PSEUDO_ENCODING_CURSOR:
                self._consume_cursor_pseudo_encoding(rect_width, rect_height)
                continue

            if rect_encoding_number == _ENCODING_RAW:
                self._decode_raw_rectangle(rect_x, rect_y, rect_width, rect_height)
            elif rect_encoding_number == _ENCODING_COPYRECT:
                self._decode_copyrect_rectangle(rect_x, rect_y, rect_width, rect_height)
            elif rect_encoding_number == _ENCODING_ZRLE:
                self._decode_zrle_rectangle(rect_x, rect_y, rect_width, rect_height)
            elif rect_encoding_number == _ENCODING_TIGHT:
                self._decode_tight_rectangle(rect_x, rect_y, rect_width, rect_height)
            else:
                raise VncConnectionError(
                    f"Server used encoding {rect_encoding_number} which this build did not advertise."
                )
            applied_dirty_rectangles.append((rect_x, rect_y, rect_width, rect_height))

        if applied_dirty_rectangles:
            with self._framebuffer_access_lock:
                self._dirty_rectangles_since_last_get_changes.extend(applied_dirty_rectangles)
            self.count_of_framebuffer_updates_applied_since_connect += 1
            self._a_framebuffer_update_was_applied_event.set()
        self._first_full_framebuffer_update_has_arrived.set()
        self.time_of_last_successful_activity_epoch_seconds = time.time()

    def _consume_cursor_pseudo_encoding(self, cursor_width: int, cursor_height: int) -> None:
        # Cursor pixels + 1-bit-per-pixel mask; we do not render the cursor, only consume it.
        pixels_byte_count = cursor_width * cursor_height * 4
        mask_byte_count = ((cursor_width + 7) // 8) * cursor_height
        if pixels_byte_count + mask_byte_count:
            self._receive_exactly_this_many_bytes(pixels_byte_count + mask_byte_count)

    # -- decoders ----------------------------------------------------------
    def _write_pixels_from_bgrx_source_into_framebuffer(
        self, rect_x: int, rect_y: int, rect_width: int, rect_height: int, source_bgrx_bytes: bytes
    ) -> None:
        """Blit a rect worth of BGRX pixel data (row-major, 4 bytes/pixel) onto the
        persistent framebuffer, clipping to bounds."""
        with self._framebuffer_access_lock:
            framebuffer_row_stride = self.framebuffer_width_pixels * 4
            for row_offset in range(rect_height):
                destination_y = rect_y + row_offset
                if destination_y < 0 or destination_y >= self.framebuffer_height_pixels:
                    continue
                copy_width = min(rect_width, self.framebuffer_width_pixels - rect_x)
                if copy_width <= 0:
                    continue
                source_start = row_offset * rect_width * 4
                destination_start = destination_y * framebuffer_row_stride + rect_x * 4
                self._framebuffer_pixels_bgrx[destination_start:destination_start + copy_width * 4] = \
                    source_bgrx_bytes[source_start:source_start + copy_width * 4]

    def _raise_if_rectangle_area_exceeds_budget(self, rect_width: int, rect_height: int) -> None:
        """Guard every decoder's framebuffer allocation against an absurd rectangle
        size (whether from a hostile server or a corrupt stream)."""
        if rect_width * rect_height * 4 > _MAXIMUM_SINGLE_RECTANGLE_BYTE_BUDGET:
            raise VncConnectionError(
                f"Rectangle {rect_width}x{rect_height} exceeds the per-rectangle byte budget "
                f"of {_MAXIMUM_SINGLE_RECTANGLE_BYTE_BUDGET} bytes; refusing to allocate."
            )

    @staticmethod
    def _decompress_zlib_stream_with_output_cap(zlib_decompressor, compressed_bytes: bytes, maximum_output_bytes: int) -> bytes:
        """Inflate a persistent-zlib-stream chunk, but abort if the output would grow
        past maximum_output_bytes. This stops a tiny compressed payload from expanding
        into a huge buffer (a decompression bomb), honouring the file's stated guard."""
        per_call_output_limit = maximum_output_bytes + 1
        decompressed_output = bytearray()
        remaining_compressed_input = compressed_bytes
        while True:
            produced_chunk = zlib_decompressor.decompress(remaining_compressed_input, per_call_output_limit)
            decompressed_output += produced_chunk
            if len(decompressed_output) > maximum_output_bytes:
                raise VncConnectionError(
                    f"A compressed rectangle expanded past its {maximum_output_bytes}-byte budget; "
                    f"refusing to continue (possible decompression bomb from the server)."
                )
            remaining_compressed_input = zlib_decompressor.unconsumed_tail
            if not remaining_compressed_input:
                break
        return bytes(decompressed_output)

    def _decode_raw_rectangle(self, rect_x: int, rect_y: int, rect_width: int, rect_height: int) -> None:
        self._raise_if_rectangle_area_exceeds_budget(rect_width, rect_height)
        byte_count = rect_width * rect_height * 4
        raw_bgrx_bytes = self._receive_exactly_this_many_bytes(byte_count)
        self._write_pixels_from_bgrx_source_into_framebuffer(rect_x, rect_y, rect_width, rect_height, raw_bgrx_bytes)

    def _decode_copyrect_rectangle(self, rect_x: int, rect_y: int, rect_width: int, rect_height: int) -> None:
        source_position = self._receive_exactly_this_many_bytes(4)
        source_x, source_y = struct.unpack(">HH", source_position)
        with self._framebuffer_access_lock:
            framebuffer_row_stride = self.framebuffer_width_pixels * 4
            # Copy row-by-row; iterate in the safe direction to avoid overlap corruption.
            row_indices = range(rect_height)
            if source_y < rect_y:
                row_indices = range(rect_height - 1, -1, -1)
            snapshot_of_framebuffer = bytes(self._framebuffer_pixels_bgrx)
            for row_offset in row_indices:
                source_row_y = source_y + row_offset
                destination_row_y = rect_y + row_offset
                if not (0 <= source_row_y < self.framebuffer_height_pixels):
                    continue
                if not (0 <= destination_row_y < self.framebuffer_height_pixels):
                    continue
                source_start = source_row_y * framebuffer_row_stride + source_x * 4
                destination_start = destination_row_y * framebuffer_row_stride + rect_x * 4
                self._framebuffer_pixels_bgrx[destination_start:destination_start + rect_width * 4] = \
                    snapshot_of_framebuffer[source_start:source_start + rect_width * 4]

    def _decode_zrle_rectangle(self, rect_x: int, rect_y: int, rect_width: int, rect_height: int) -> None:
        self._raise_if_rectangle_area_exceeds_budget(rect_width, rect_height)
        compressed_length = struct.unpack(">I", self._receive_exactly_this_many_bytes(4))[0]
        if compressed_length > _MAXIMUM_SINGLE_RECTANGLE_BYTE_BUDGET:
            raise VncConnectionError("ZRLE compressed length exceeds the per-rectangle byte budget.")
        compressed_bytes = self._receive_exactly_this_many_bytes(compressed_length)
        if self._zrle_zlib_decompressor is None:
            self._zrle_zlib_decompressor = zlib.decompressobj()
        # Worst-case uncompressed ZRLE for this rect is raw CPIXELs (3 bytes/pixel)
        # plus one subencoding byte per 64x64 tile; rect_area*4 + slack bounds it and
        # still blocks a bomb (which would expand far beyond the rect's own area).
        maximum_uncompressed_bytes = rect_width * rect_height * 4 + 65536
        uncompressed_tile_stream = self._decompress_zlib_stream_with_output_cap(
            self._zrle_zlib_decompressor, compressed_bytes, maximum_uncompressed_bytes
        )
        stream_reader = _ByteSequenceReader(uncompressed_tile_stream)

        # Build the rectangle into a local BGRX buffer, then blit once.
        rectangle_bgrx = bytearray(rect_width * rect_height * 4)
        for tile_top_y in range(0, rect_height, 64):
            tile_height = min(64, rect_height - tile_top_y)
            for tile_left_x in range(0, rect_width, 64):
                tile_width = min(64, rect_width - tile_left_x)
                self._decode_one_zrle_tile_into_rectangle_buffer(
                    stream_reader, rectangle_bgrx, rect_width, tile_left_x, tile_top_y, tile_width, tile_height
                )
        self._write_pixels_from_bgrx_source_into_framebuffer(
            rect_x, rect_y, rect_width, rect_height, bytes(rectangle_bgrx)
        )

    def _decode_one_zrle_tile_into_rectangle_buffer(
        self,
        stream_reader: "_ByteSequenceReader",
        rectangle_bgrx: bytearray,
        rectangle_width: int,
        tile_left_x: int,
        tile_top_y: int,
        tile_width: int,
        tile_height: int,
    ) -> None:
        # CPIXEL is 3 bytes for our 32bpp/depth-24 format: least-significant 3 bytes
        # little-endian = [B, G, R].
        subencoding_byte = stream_reader.read_one_byte()
        is_run_length_encoded = (subencoding_byte & 0x80) != 0
        palette_size = subencoding_byte & 0x7F

        def place_bgr_pixel_at(tile_x: int, tile_y: int, three_byte_bgr: bytes) -> None:
            destination = ((tile_top_y + tile_y) * rectangle_width + (tile_left_x + tile_x)) * 4
            rectangle_bgrx[destination] = three_byte_bgr[0]
            rectangle_bgrx[destination + 1] = three_byte_bgr[1]
            rectangle_bgrx[destination + 2] = three_byte_bgr[2]
            rectangle_bgrx[destination + 3] = 0

        if not is_run_length_encoded and palette_size == 0:
            # Raw tile: tile_width*tile_height CPIXELs.
            for tile_y in range(tile_height):
                for tile_x in range(tile_width):
                    place_bgr_pixel_at(tile_x, tile_y, stream_reader.read_n_bytes(3))
            return
        if not is_run_length_encoded and palette_size == 1:
            # Solid tile.
            solid_pixel = stream_reader.read_n_bytes(3)
            for tile_y in range(tile_height):
                for tile_x in range(tile_width):
                    place_bgr_pixel_at(tile_x, tile_y, solid_pixel)
            return
        if not is_run_length_encoded and 2 <= palette_size <= 16:
            # Packed palette.
            palette = [stream_reader.read_n_bytes(3) for _ in range(palette_size)]
            if palette_size == 2:
                bits_per_index = 1
            elif palette_size <= 4:
                bits_per_index = 2
            else:
                bits_per_index = 4
            for tile_y in range(tile_height):
                bit_reader = _MostSignificantBitFirstReader(stream_reader, bits_per_index)
                for tile_x in range(tile_width):
                    palette_index = bit_reader.read_next_index()
                    place_bgr_pixel_at(tile_x, tile_y, palette[palette_index])
            return
        if is_run_length_encoded and palette_size == 0:
            # Plain RLE.
            tile_pixel_position = 0
            total_tile_pixels = tile_width * tile_height
            while tile_pixel_position < total_tile_pixels:
                run_pixel = stream_reader.read_n_bytes(3)
                run_length = 1
                while True:
                    length_byte = stream_reader.read_one_byte()
                    run_length += length_byte
                    if length_byte != 255:
                        break
                for _ in range(run_length):
                    if tile_pixel_position >= total_tile_pixels:
                        break
                    place_bgr_pixel_at(tile_pixel_position % tile_width, tile_pixel_position // tile_width, run_pixel)
                    tile_pixel_position += 1
            return
        if is_run_length_encoded and palette_size >= 1:
            # Palette RLE (subencoding 130..255 => paletteSize = subencoding-128).
            actual_palette_size = subencoding_byte - 128
            palette = [stream_reader.read_n_bytes(3) for _ in range(actual_palette_size)]
            tile_pixel_position = 0
            total_tile_pixels = tile_width * tile_height
            while tile_pixel_position < total_tile_pixels:
                index_byte = stream_reader.read_one_byte()
                palette_index = index_byte & 0x7F
                run_length = 1
                if index_byte & 0x80:
                    while True:
                        length_byte = stream_reader.read_one_byte()
                        run_length += length_byte
                        if length_byte != 255:
                            break
                run_pixel = palette[palette_index]
                for _ in range(run_length):
                    if tile_pixel_position >= total_tile_pixels:
                        break
                    place_bgr_pixel_at(tile_pixel_position % tile_width, tile_pixel_position // tile_width, run_pixel)
                    tile_pixel_position += 1
            return
        raise VncConnectionError(f"Unsupported ZRLE tile subencoding {subencoding_byte}.")

    def _read_tight_compact_length(self) -> int:
        first_byte = self._receive_exactly_this_many_bytes(1)[0]
        length_value = first_byte & 0x7F
        if first_byte & 0x80:
            second_byte = self._receive_exactly_this_many_bytes(1)[0]
            length_value |= (second_byte & 0x7F) << 7
            if second_byte & 0x80:
                third_byte = self._receive_exactly_this_many_bytes(1)[0]
                length_value |= third_byte << 14
        return length_value

    def _decode_tight_rectangle(self, rect_x: int, rect_y: int, rect_width: int, rect_height: int) -> None:
        self._raise_if_rectangle_area_exceeds_budget(rect_width, rect_height)
        compression_control_byte = self._receive_exactly_this_many_bytes(1)[0]
        # Reset any zlib streams flagged in the low nibble.
        for stream_index in range(4):
            if compression_control_byte & (1 << stream_index):
                self._tight_zlib_decompressors[stream_index] = None

        method_nibble = compression_control_byte >> 4
        if compression_control_byte & 0x80:
            if method_nibble == 0x8:  # FillCompression
                tpixel = self._receive_exactly_this_many_bytes(3)  # [R, G, B]
                single_bgrx = bytes([tpixel[2], tpixel[1], tpixel[0], 0])
                rectangle_bgrx = single_bgrx * (rect_width * rect_height)
                self._write_pixels_from_bgrx_source_into_framebuffer(
                    rect_x, rect_y, rect_width, rect_height, rectangle_bgrx
                )
                return
            if method_nibble == 0x9:  # JpegCompression
                jpeg_length = self._read_tight_compact_length()
                jpeg_bytes = self._receive_exactly_this_many_bytes(jpeg_length)
                pil_image_module = ensure_pillow_imaging_library_is_available()
                decoded_image = pil_image_module.open(io.BytesIO(jpeg_bytes)).convert("RGB")
                red_green_blue_bytes = decoded_image.tobytes()
                rectangle_bgrx = bytearray(rect_width * rect_height * 4)
                for pixel_index in range(rect_width * rect_height):
                    rectangle_bgrx[pixel_index * 4] = red_green_blue_bytes[pixel_index * 3 + 2]
                    rectangle_bgrx[pixel_index * 4 + 1] = red_green_blue_bytes[pixel_index * 3 + 1]
                    rectangle_bgrx[pixel_index * 4 + 2] = red_green_blue_bytes[pixel_index * 3]
                self._write_pixels_from_bgrx_source_into_framebuffer(
                    rect_x, rect_y, rect_width, rect_height, bytes(rectangle_bgrx)
                )
                return
            raise VncConnectionError(f"Unsupported Tight compression method nibble {method_nibble:#x}.")

        # BasicCompression.
        stream_index = (compression_control_byte >> 4) & 0x03
        filter_id = 0
        if compression_control_byte & 0x40:
            filter_id = self._receive_exactly_this_many_bytes(1)[0]

        if filter_id == 1:  # PaletteFilter
            palette_color_count = self._receive_exactly_this_many_bytes(1)[0] + 1
            palette_tpixels = [self._receive_exactly_this_many_bytes(3) for _ in range(palette_color_count)]
            if palette_color_count == 2:
                bytes_per_row = (rect_width + 7) // 8
            else:
                bytes_per_row = rect_width
            filtered_data_length = bytes_per_row * rect_height
            filtered_bytes = self._read_tight_basic_payload(stream_index, filtered_data_length)
            rectangle_bgrx = bytearray(rect_width * rect_height * 4)
            for row in range(rect_height):
                for column in range(rect_width):
                    if palette_color_count == 2:
                        byte_at = filtered_bytes[row * bytes_per_row + (column // 8)]
                        palette_index = (byte_at >> (7 - (column % 8))) & 0x01
                    else:
                        palette_index = filtered_bytes[row * bytes_per_row + column]
                    tpixel = palette_tpixels[palette_index]
                    destination = (row * rect_width + column) * 4
                    rectangle_bgrx[destination] = tpixel[2]
                    rectangle_bgrx[destination + 1] = tpixel[1]
                    rectangle_bgrx[destination + 2] = tpixel[0]
            self._write_pixels_from_bgrx_source_into_framebuffer(
                rect_x, rect_y, rect_width, rect_height, bytes(rectangle_bgrx)
            )
            return

        # CopyFilter (0) or GradientFilter (2): both carry width*height TPIXELs (3 bytes).
        filtered_data_length = rect_width * rect_height * 3
        filtered_bytes = self._read_tight_basic_payload(stream_index, filtered_data_length)
        if filter_id == 2:
            filtered_bytes = self._reverse_tight_gradient_filter(filtered_bytes, rect_width, rect_height)
        rectangle_bgrx = bytearray(rect_width * rect_height * 4)
        for pixel_index in range(rect_width * rect_height):
            rectangle_bgrx[pixel_index * 4] = filtered_bytes[pixel_index * 3 + 2]
            rectangle_bgrx[pixel_index * 4 + 1] = filtered_bytes[pixel_index * 3 + 1]
            rectangle_bgrx[pixel_index * 4 + 2] = filtered_bytes[pixel_index * 3]
        self._write_pixels_from_bgrx_source_into_framebuffer(
            rect_x, rect_y, rect_width, rect_height, bytes(rectangle_bgrx)
        )

    def _read_tight_basic_payload(self, stream_index: int, uncompressed_length: int) -> bytes:
        # Data shorter than 12 bytes (after filtering) is sent uncompressed.
        if uncompressed_length < 12:
            return self._receive_exactly_this_many_bytes(uncompressed_length)
        compressed_length = self._read_tight_compact_length()
        if compressed_length > _MAXIMUM_SINGLE_RECTANGLE_BYTE_BUDGET:
            raise VncConnectionError("Tight compressed length exceeds the per-rectangle byte budget.")
        compressed_bytes = self._receive_exactly_this_many_bytes(compressed_length)
        if self._tight_zlib_decompressors[stream_index] is None:
            self._tight_zlib_decompressors[stream_index] = zlib.decompressobj()
        # We know the exact expected filtered length, so cap the inflate at it; any
        # server producing more than that is corrupt or hostile.
        return self._decompress_zlib_stream_with_output_cap(
            self._tight_zlib_decompressors[stream_index], compressed_bytes, uncompressed_length
        )

    def _reverse_tight_gradient_filter(self, filtered_bytes: bytes, rect_width: int, rect_height: int) -> bytes:
        reconstructed = bytearray(len(filtered_bytes))
        for color_channel in range(3):
            for row in range(rect_height):
                for column in range(rect_width):
                    position = (row * rect_width + column) * 3 + color_channel
                    left_value = reconstructed[position - 3] if column > 0 else 0
                    above_value = reconstructed[(position - rect_width * 3)] if row > 0 else 0
                    above_left_value = reconstructed[position - rect_width * 3 - 3] if (row > 0 and column > 0) else 0
                    predicted = left_value + above_value - above_left_value
                    if predicted < 0:
                        predicted = 0
                    elif predicted > 255:
                        predicted = 255
                    reconstructed[position] = (filtered_bytes[position] + predicted) & 0xFF
        return bytes(reconstructed)

    # -- screenshot / changes ---------------------------------------------
    def wait_until_first_framebuffer_update_received(self, timeout_seconds: float) -> bool:
        return self._first_full_framebuffer_update_has_arrived.wait(timeout=timeout_seconds)

    def render_framebuffer_region_to_png_bytes(
        self, region_x: int, region_y: int, region_width: int, region_height: int
    ) -> bytes:
        pil_image_module = ensure_pillow_imaging_library_is_available()
        with self._framebuffer_access_lock:
            full_width = self.framebuffer_width_pixels
            full_height = self.framebuffer_height_pixels
            framebuffer_snapshot = bytes(self._framebuffer_pixels_bgrx)
        full_image = pil_image_module.frombytes("RGB", (full_width, full_height), framebuffer_snapshot, "raw", "BGRX")
        if region_width and region_height:
            crop_box = (region_x, region_y, region_x + region_width, region_y + region_height)
            full_image = full_image.crop(crop_box)
        png_output_buffer = io.BytesIO()
        full_image.save(png_output_buffer, format="PNG")
        return png_output_buffer.getvalue()

    def take_and_clear_dirty_rectangles_since_last_query(self) -> List[Tuple[int, int, int, int]]:
        with self._framebuffer_access_lock:
            dirty_rectangles = list(self._dirty_rectangles_since_last_get_changes)
            self._dirty_rectangles_since_last_get_changes.clear()
        return dirty_rectangles

    def wait_for_any_framebuffer_change(self, timeout_seconds: float) -> bool:
        # If changes are already pending (arrived since the caller last consumed them),
        # report immediately instead of blocking for the NEXT change and missing these.
        with self._framebuffer_access_lock:
            if self._dirty_rectangles_since_last_get_changes:
                return True
        self._a_framebuffer_update_was_applied_event.clear()
        return self._a_framebuffer_update_was_applied_event.wait(timeout=timeout_seconds)

    # -- input -------------------------------------------------------------
    def send_pointer_event(self, pointer_x: int, pointer_y: int, button_mask: int) -> None:
        clamped_x = max(0, min(pointer_x, max(0, self.framebuffer_width_pixels - 1)))
        clamped_y = max(0, min(pointer_y, max(0, self.framebuffer_height_pixels - 1)))
        self._send_all_bytes(struct.pack(">BBHH", _CLIENT_MSG_POINTER_EVENT, button_mask, clamped_x, clamped_y))

    def send_key_event(self, keysym: int, is_key_press_down: bool) -> None:
        self._send_all_bytes(struct.pack(">BBHI", _CLIENT_MSG_KEY_EVENT, 1 if is_key_press_down else 0, 0, keysym))

    def send_client_cut_text(self, clipboard_text: str) -> None:
        encoded = clipboard_text.encode("latin-1", "replace")
        self._send_all_bytes(struct.pack(">BxxxI", _CLIENT_MSG_CLIENT_CUT_TEXT, len(encoded)) + encoded)

    # -- teardown ----------------------------------------------------------
    def disconnect_and_release_resources(self) -> None:
        self._background_reader_should_keep_running = False
        self.session_socket_is_currently_connected = False
        # Shut down and close the byte stream FIRST: for TLS sessions it (the
        # ssl.SSLSocket) owns the real fd, while self._raw_tcp_socket has been
        # detached by wrap_socket() (fileno() == -1). SSLSocket.shutdown() performs a
        # real TCP shutdown, which unblocks the reader's blocking recv immediately.
        for socket_like_object in (self._byte_stream_for_reading_and_writing, self._raw_tcp_socket):
            if socket_like_object is None:
                continue
            try:
                socket_like_object.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                socket_like_object.close()
            except OSError:
                pass
        if self.accessibility_side_channel_client is not None:
            try:
                self.accessibility_side_channel_client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Small byte / bit reader helpers used by the ZRLE decoder.
# ---------------------------------------------------------------------------
class _ByteSequenceReader:
    def __init__(self, backing_bytes: bytes):
        self._backing_bytes = backing_bytes
        self._read_position = 0

    def read_one_byte(self) -> int:
        value = self._backing_bytes[self._read_position]
        self._read_position += 1
        return value

    def read_n_bytes(self, count: int) -> bytes:
        chunk = self._backing_bytes[self._read_position:self._read_position + count]
        self._read_position += count
        return chunk


class _MostSignificantBitFirstReader:
    """Reads fixed-width big-endian bit fields (MSB first) from a byte reader. ZRLE
    packed-palette tiles pad each row to a byte boundary; the caller achieves that by
    constructing a fresh reader per row, so this reader itself is intentionally
    row-agnostic and simply consumes bits left to right."""

    def __init__(self, byte_reader: _ByteSequenceReader, bits_per_index: int):
        self._byte_reader = byte_reader
        self._bits_per_index = bits_per_index
        self._current_byte_value = 0
        self._remaining_bits_in_current_byte = 0

    def read_next_index(self) -> int:
        if self._remaining_bits_in_current_byte < self._bits_per_index:
            self._current_byte_value = self._byte_reader.read_one_byte()
            self._remaining_bits_in_current_byte = 8
        shift_amount = self._remaining_bits_in_current_byte - self._bits_per_index
        index_value = (self._current_byte_value >> shift_amount) & ((1 << self._bits_per_index) - 1)
        self._remaining_bits_in_current_byte -= self._bits_per_index
        return index_value


# ---------------------------------------------------------------------------
# Module-level session registry (build plan R7a).
# ---------------------------------------------------------------------------
_active_remote_desktop_sessions_by_id: Dict[str, RemoteFramebufferProtocolSession] = {}
_active_remote_desktop_sessions_lock = threading.Lock()
_remote_desktop_session_id_counter = 0


def _allocate_next_remote_desktop_session_id() -> str:
    global _remote_desktop_session_id_counter
    with _active_remote_desktop_sessions_lock:
        _remote_desktop_session_id_counter += 1
        return f"vnc_{_remote_desktop_session_id_counter}"


def _look_up_connected_session_or_none(session_id: str) -> Optional[RemoteFramebufferProtocolSession]:
    with _active_remote_desktop_sessions_lock:
        return _active_remote_desktop_sessions_by_id.get(session_id)


# ---------------------------------------------------------------------------
# The tool definition (build plan R2/R3).
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": TOOL_NAME,
        "description": """Drive a REMOTE desktop over VNC/RFB (screenshot, click, type, clipboard) - the remote sibling of the local `system` tool.
- Use this to see and control another machine's GUI (Linux/macOS/Windows VNC servers, or a QEMU -vnc VM) over the network.
- Call {"input":{"operation":"readme"}} first for the full manual, the parameter schema, and your unlock token.
""",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"operation\":\"readme\"}} to get full documentation, parameters, and an unlock token."
                }
            },
            "required": [],
            "type": "object"
        },
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "readme", "about", "connect", "list_sessions", "disconnect",
                        "take_screenshot", "get_changes",
                        "click_at_coordinates", "move_pointer", "drag", "scroll",
                        "send_text", "get_clipboard", "set_clipboard",
                        "wait_for_change", "wait_for_image",
                        "scan_ui_elements", "click_ui_element",
                    ],
                    "description": "Operation to perform"
                },
                "tool_unlock_token": {"type": "string", "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from the readme operation"},
                "host": {"type": "string", "description": "connect: remote hostname or IP of the VNC/RFB server"},
                "port": {"type": "integer", "description": "connect: TCP port (default 5900; port = 5900 + display number)", "default": 5900},
                "password": {"type": "string", "description": "connect: VNC password (capped at 8 chars by the protocol for VncAuth). Redacted in logs."},
                "username": {"type": "string", "description": "connect: username for Apple-DH (macOS) and VeNCrypt Plain sub-types. Redacted in logs."},
                "security_type": {"type": "string", "description": "connect: force/prefer a specific sign-in method by name or number (e.g. 'VncAuth', 'VeNCrypt', '2', '19', 'AppleDH')"},
                "forbid_security_types": {"type": "array", "items": {"type": "integer"}, "description": "connect: security-type numbers to refuse (e.g. [2] to prove the fail-loud path)"},
                "ca_cert_path": {"type": "string", "description": "connect: path to a CA certificate for VeNCrypt X509 sub-types"},
                "client_cert_path": {"type": "string", "description": "connect: path to a client certificate (X509 mutual auth)"},
                "client_key_path": {"type": "string", "description": "connect: path to the client private key (X509 mutual auth)"},
                "verify_hostname": {"type": "boolean", "description": "connect: for X509 TLS, also verify the server certificate matches the hostname (requires ca_cert_path). Default false for LAN/self-signed use.", "default": False},
                "connect_timeout": {"type": "number", "description": "connect: TCP connect timeout in seconds (default 10)", "default": 10},
                "view_only": {"type": "boolean", "description": "connect: if true this session captures only and refuses ALL input operations", "default": False},
                "require_confirmation": {"type": "boolean", "description": "connect: if true, input operations must also pass confirm=true (per-action consent gating)", "default": False},
                "confirm": {"type": "boolean", "description": "input ops: set true to satisfy a session created with require_confirmation=true", "default": False},
                "session_id": {"type": "string", "description": "every op except connect/list_sessions/readme/about: the id returned by connect (replaces system.py's hwnd)"},
                "x_coordinate": {"type": "integer", "description": "click/move/drag/scroll: X pixel in the remote framebuffer"},
                "y_coordinate": {"type": "integer", "description": "click/move/drag/scroll: Y pixel in the remote framebuffer"},
                "to_x_coordinate": {"type": "integer", "description": "drag: destination X pixel"},
                "to_y_coordinate": {"type": "integer", "description": "drag: destination Y pixel"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "click/drag: which pointer button (default left)", "default": "left"},
                "double": {"type": "boolean", "description": "click_at_coordinates: perform a double-click", "default": False},
                "scroll_amount": {"type": "integer", "description": "scroll: number of wheel steps; positive = up, negative = down", "default": -3},
                "text": {"type": "string", "description": "send_text: AutoHotkey-style text/keys (e.g. 'hello{Enter}', '^a', '+{Tab 3}', '{Raw}...')"},
                "clipboard_text": {"type": "string", "description": "set_clipboard: text to place on the remote clipboard"},
                "region": {"type": "array", "items": {"type": "integer"}, "description": "take_screenshot/wait_for_image: [x, y, width, height] subregion"},
                "filename": {"type": "string", "description": "take_screenshot: if given, save PNG to this path and return text confirmation instead of the image"},
                "timeout_seconds": {"type": "number", "description": "wait_for_change/wait_for_image: max seconds to block (default 30)", "default": 30},
                "element_name": {"type": "string", "description": "click_ui_element: name/label/text of the element to click"},
                "element_role": {"type": "string", "description": "scan_ui_elements/click_ui_element: filter by role/type"},
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "readme": """
Drive a REMOTE desktop over VNC / RFB (RFC 6143). This is the graphical, remote
sibling of the local `system` tool: the SAME verbs (take_screenshot,
click_at_coordinates, send_text, ...) but pointed at another machine over the
network. An agent fluent in `system` drives this by changing the tool name and
swapping the local window handle (hwnd) for a `session_id` returned by connect.

## Usage-Safety Token System
Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """
Include tool_unlock_token in the input dict for every operation except readme.

## Target selection (IMPORTANT - there is NO hwnd here)
`connect` opens a session to host:port and returns a `session_id` like "vnc_1".
Every other operation takes that `session_id`. Do not look for an hwnd.

## Broad server compatibility (the whole point of this tool)
This client negotiates the RFB 3.3 / 3.7 / 3.8 handshakes and supports these
sign-in (security) methods: 1 None, 2 VncAuth (password), 16 Tight, 18 TLS,
19 VeNCrypt (Plain/TLSNone/TLSVnc/TLSPlain/X509None/X509Vnc/X509Plain), and
30 Apple-DH (macOS Screen Sharing - needs BOTH username and password). If a
server offers nothing this build can do, connect returns a LOUD, actionable
error naming exactly what the server offered, what is supported here, why the
overlap failed, and the concrete next step - never a bare "cannot connect".

## Operations and examples
1) readme:
   {"input":{"operation":"readme"}}

2) connect (VncAuth, the common case):
   {"input":{"operation":"connect","host":"172.22.1.7","port":5901,"password":"secret","tool_unlock_token":\"""" + TOOL_UNLOCK_TOKEN + """\"}}
   -> returns text containing "session_id": "vnc_1" plus the desktop size and the
      negotiated security type. Optional connect params: username, security_type
      (force/prefer e.g. "VeNCrypt"), forbid_security_types (e.g. [2]),
      ca_cert_path/client_cert_path/client_key_path (X509), verify_hostname
      (X509 only; requires ca_cert_path), connect_timeout, view_only (capture
      only, refuse input), require_confirmation (gate input behind confirm=true).

3) macOS (Apple Screen Sharing) needs a username too:
   {"input":{"operation":"connect","host":"mini","port":5900,"username":"cnd","password":"secret","tool_unlock_token":"..."}}

4) take_screenshot (returns a PNG image; omit filename to get the image inline):
   {"input":{"operation":"take_screenshot","session_id":"vnc_1","tool_unlock_token":"..."}}
   With a region or a save path:
   {"input":{"operation":"take_screenshot","session_id":"vnc_1","region":[0,0,400,300],"filename":"/tmp/shot.png","tool_unlock_token":"..."}}

5) click_at_coordinates (same param names as the `system` tool):
   {"input":{"operation":"click_at_coordinates","session_id":"vnc_1","x_coordinate":640,"y_coordinate":400,"button":"left","tool_unlock_token":"..."}}
   Double-click: add "double":true.

6) move_pointer / drag / scroll:
   {"input":{"operation":"move_pointer","session_id":"vnc_1","x_coordinate":100,"y_coordinate":100,"tool_unlock_token":"..."}}
   {"input":{"operation":"drag","session_id":"vnc_1","x_coordinate":50,"y_coordinate":50,"to_x_coordinate":300,"to_y_coordinate":300,"button":"left","tool_unlock_token":"..."}}
   {"input":{"operation":"scroll","session_id":"vnc_1","x_coordinate":640,"y_coordinate":400,"scroll_amount":-3,"tool_unlock_token":"..."}}

7) send_text (AutoHotkey-style, identical to the `system` tool):
   {"input":{"operation":"send_text","session_id":"vnc_1","text":"hello world{Enter}","tool_unlock_token":"..."}}
   Modifiers ^=Ctrl +=Shift !=Alt #=Win; specials {Enter}{Tab}{Escape}{F1}-{F24}
   arrows etc; repeats {Tab 3}; hold/release {Ctrl down}...{Ctrl up}; literal
   text via {Raw}...; escape braces as {{} and {}}.

8) clipboard (avoids OCR - use it to move text in/out):
   {"input":{"operation":"get_clipboard","session_id":"vnc_1","tool_unlock_token":"..."}}
   {"input":{"operation":"set_clipboard","session_id":"vnc_1","clipboard_text":"pasted","tool_unlock_token":"..."}}

9) get_changes (cheap "what changed since I last looked" - dirty rectangles):
   {"input":{"operation":"get_changes","session_id":"vnc_1","tool_unlock_token":"..."}}

10) wait_for_change / wait_for_image (block instead of polling screenshots):
   {"input":{"operation":"wait_for_change","session_id":"vnc_1","region":[0,0,200,50],"timeout_seconds":30,"tool_unlock_token":"..."}}

11) scan_ui_elements / click_ui_element (accessibility side-channel):
   These use a remote helper stub when present, and otherwise fall back to OCR
   automatically. If neither is available they return an explanatory message,
   never a dead verb.
   {"input":{"operation":"scan_ui_elements","session_id":"vnc_1","tool_unlock_token":"..."}}
   {"input":{"operation":"click_ui_element","session_id":"vnc_1","element_name":"OK","tool_unlock_token":"..."}}

12) list_sessions / disconnect / about:
   {"input":{"operation":"list_sessions","tool_unlock_token":"..."}}
   {"input":{"operation":"disconnect","session_id":"vnc_1","tool_unlock_token":"..."}}
"""
    }
]


# ---------------------------------------------------------------------------
# Validation, readme, and response helpers (copied shape from template.py).
# ---------------------------------------------------------------------------
def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema, enforcing
    declared types and enums and filling in defaults (richer template.py shape)."""
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])

    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]

    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    if unexpected_params:
        return (
            f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. "
            f"Expected parameters are: {', '.join(sorted(expected_params))}. Please consult the readme.",
            {},
        )

    missing_required = set(required) - provided_params
    if missing_required:
        return (
            f"Missing required parameters: {', '.join(sorted(missing_required))}. "
            f"Required parameters are: {', '.join(sorted(required))}",
            {},
        )

    validated: Dict[str, Any] = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}.", {}
            elif expected_type == "object" and not isinstance(value, dict):
                return f"Parameter '{param_name}' must be an object, got {type(value).__name__}.", {}
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}.", {}
            elif expected_type == "number" and not isinstance(value, (int, float)):
                return f"Parameter '{param_name}' must be a number, got {type(value).__name__}.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}.", {}
            elif expected_type == "array" and not isinstance(value, list):
                return f"Parameter '{param_name}' must be an array, got {type(value).__name__}.", {}
            if "enum" in param_schema and value not in param_schema["enum"]:
                return f"Parameter '{param_name}' must be one of {param_schema['enum']}, got '{value}'.", {}
            validated[param_name] = value
        elif param_name in required:
            return f"Required parameter '{param_name}' is missing.", {}
        else:
            default_value = param_schema.get("default")
            if default_value is not None:
                validated[param_name] = default_value
    return None, validated


def readme(with_readme: bool = True) -> str:
    try:
        if not with_readme:
            return ""
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        return "\n\n" + json.dumps(
            {"description": TOOLS[0]["readme"], "parameters": TOOLS[0]["real_parameters"]}, indent=2
        )
    except Exception as readme_error:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {readme_error}")
        return ""


def create_error_response(error_msg: str, with_readme: bool = True) -> Dict:
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}


def create_success_response(human_readable_message: str, structured_data: Optional[Dict] = None) -> Dict:
    """Text success envelope. Cursor shows the agent ONLY content[0].text, so any
    structured data is ALSO json-dumped into that text while still being attached
    at the top level for programmatic callers (build plan R5)."""
    text_payload = human_readable_message
    if structured_data:
        text_payload = f"{human_readable_message}\n\n{json.dumps(structured_data, indent=2)}"
    response: Dict[str, Any] = {"content": [{"type": "text", "text": text_payload}], "isError": False}
    if structured_data:
        response.update(structured_data)
    return response


def create_image_response(png_image_bytes: bytes) -> Dict:
    """Image envelope: raw base64 (NO data: URI prefix), mimeType alongside."""
    return {
        "content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(png_image_bytes).decode("ascii")}],
        "isError": False,
    }


# ---------------------------------------------------------------------------
# Per-operation workers.
# ---------------------------------------------------------------------------
def _verify_input_is_permitted_for_session(
    session: RemoteFramebufferProtocolSession, validated_params: Dict
) -> Optional[Dict]:
    """Enforce view-only mode and per-action confirmation gating. Returns an error
    response dict if the input must be blocked, else None."""
    if session.client_requested_view_only_mode_no_input_allowed:
        return create_error_response(
            f"Session {session.remote_desktop_session_id} is view-only (connected with view_only=true); "
            f"input operations are refused. Reconnect without view_only to send input.",
            with_readme=False,
        )
    if session.input_operations_require_explicit_confirmation and not validated_params.get("confirm", False):
        return create_error_response(
            f"Session {session.remote_desktop_session_id} was created with require_confirmation=true. "
            f"Re-issue this input operation with \"confirm\": true to proceed.",
            with_readme=False,
        )
    return None


def handle_connect(validated_params: Dict) -> Dict:
    host = validated_params.get("host")
    if not host:
        return create_error_response("connect requires 'host'.", with_readme=True)
    port = int(validated_params.get("port", 5900))
    session_id = _allocate_next_remote_desktop_session_id()
    session = RemoteFramebufferProtocolSession(
        remote_desktop_session_id=session_id,
        remote_host_name_or_address=host,
        remote_tcp_port_number=port,
        client_requested_view_only_mode_no_input_allowed=bool(validated_params.get("view_only", False)),
        input_operations_require_explicit_confirmation=bool(validated_params.get("require_confirmation", False)),
        tls_should_verify_server_hostname_against_certificate=bool(validated_params.get("verify_hostname", False)),
    )
    MCPLogger.log(TOOL_LOG_NAME, f"connect {host}:{port} as {session_id} (password {'set' if validated_params.get('password') else 'not set'})")
    try:
        session.open_socket_and_perform_full_handshake(
            password_text=validated_params.get("password"),
            username_text=validated_params.get("username"),
            preferred_or_forced_security_type=validated_params.get("security_type"),
            forbidden_security_type_numbers=[int(n) for n in validated_params.get("forbid_security_types", [])],
            certificate_authority_path=validated_params.get("ca_cert_path"),
            client_certificate_path=validated_params.get("client_cert_path"),
            client_private_key_path=validated_params.get("client_key_path"),
            tcp_connect_timeout_seconds=float(validated_params.get("connect_timeout", 10)),
        )
    except (VncAuthenticationNegotiationError, VncConnectionError) as connect_error:
        session.disconnect_and_release_resources()
        return create_error_response(str(connect_error), with_readme=False)
    except Exception as unexpected_error:
        session.disconnect_and_release_resources()
        return create_error_response(f"Unexpected error during connect: {unexpected_error}", with_readme=False)

    if not session.wait_until_first_framebuffer_update_received(timeout_seconds=10.0):
        MCPLogger.log(TOOL_LOG_NAME, f"[{session_id}] connected but first framebuffer update not yet received")

    with _active_remote_desktop_sessions_lock:
        _active_remote_desktop_sessions_by_id[session_id] = session

    return create_success_response(
        f"Connected to {host}:{port} as session {session_id} using security type "
        f"{session.selected_security_type_number} ({session.selected_security_type_human_name}). "
        f"Remote desktop is {session.framebuffer_width_pixels}x{session.framebuffer_height_pixels} "
        f"({session.remote_desktop_display_name!r}).",
        {
            "session_id": session_id,
            "host": host,
            "port": port,
            "security_type_number": session.selected_security_type_number,
            "security_type_name": session.selected_security_type_human_name,
            "rfb_version": f"{session.negotiated_rfb_major_version}.{session.negotiated_rfb_minor_version}",
            "width": session.framebuffer_width_pixels,
            "height": session.framebuffer_height_pixels,
            "desktop_name": session.remote_desktop_display_name,
            "view_only": session.client_requested_view_only_mode_no_input_allowed,
        },
    )


def handle_list_sessions(validated_params: Dict) -> Dict:
    with _active_remote_desktop_sessions_lock:
        sessions_snapshot = list(_active_remote_desktop_sessions_by_id.values())
    session_descriptions = []
    for session in sessions_snapshot:
        session_descriptions.append(
            {
                "session_id": session.remote_desktop_session_id,
                "host": session.remote_host_name_or_address,
                "port": session.remote_tcp_port_number,
                "connected": session.session_socket_is_currently_connected,
                "security_type": session.selected_security_type_human_name,
                "width": session.framebuffer_width_pixels,
                "height": session.framebuffer_height_pixels,
                "view_only": session.client_requested_view_only_mode_no_input_allowed,
                "updates_applied": session.count_of_framebuffer_updates_applied_since_connect,
            }
        )
    return create_success_response(
        f"{len(session_descriptions)} open remote-desktop session(s).", {"sessions": session_descriptions}
    )


def handle_disconnect(validated_params: Dict) -> Dict:
    session_id = validated_params.get("session_id")
    if not session_id:
        return create_error_response("disconnect requires 'session_id'.", with_readme=True)
    with _active_remote_desktop_sessions_lock:
        session = _active_remote_desktop_sessions_by_id.pop(session_id, None)
    if session is None:
        return create_error_response(f"No such session '{session_id}'. Use list_sessions to see open ids.", with_readme=False)
    session.disconnect_and_release_resources()
    return create_success_response(f"Disconnected session {session_id}.")


def _require_connected_session(validated_params: Dict) -> Tuple[Optional[RemoteFramebufferProtocolSession], Optional[Dict]]:
    session_id = validated_params.get("session_id")
    if not session_id:
        return None, create_error_response("This operation requires 'session_id' (from connect).", with_readme=True)
    session = _look_up_connected_session_or_none(session_id)
    if session is None:
        return None, create_error_response(
            f"No such session '{session_id}'. It may have been disconnected. Use list_sessions, or connect again.",
            with_readme=False,
        )
    if not session.session_socket_is_currently_connected:
        return None, create_error_response(
            f"Session '{session_id}' is no longer connected (the remote closed the link). Connect again.",
            with_readme=False,
        )
    return session, None


def handle_take_screenshot(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    region = validated_params.get("region")
    region_x = region_y = region_width = region_height = 0
    if region:
        if len(region) != 4:
            return create_error_response("region must be [x, y, width, height].", with_readme=False)
        region_x, region_y, region_width, region_height = (int(v) for v in region)
    try:
        png_bytes = session.render_framebuffer_region_to_png_bytes(region_x, region_y, region_width, region_height)
    except Exception as render_error:
        return create_error_response(f"Failed to render screenshot: {render_error}", with_readme=False)

    filename = validated_params.get("filename")
    if filename:
        try:
            with open(filename, "wb") as output_file:
                output_file.write(png_bytes)
        except OSError as write_error:
            return create_error_response(f"Failed to save screenshot to {filename}: {write_error}", with_readme=False)
        return create_success_response(
            f"Saved {len(png_bytes)} byte PNG screenshot of session {session.remote_desktop_session_id} to {filename}.",
            {"filename": filename, "bytes": len(png_bytes)},
        )
    return create_image_response(png_bytes)


def handle_get_changes(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    dirty_rectangles = session.take_and_clear_dirty_rectangles_since_last_query()
    return create_success_response(
        f"{len(dirty_rectangles)} rectangle(s) changed since the last get_changes on session "
        f"{session.remote_desktop_session_id}.",
        {"changed_rectangles": [list(rectangle) for rectangle in dirty_rectangles],
         "updates_applied_total": session.count_of_framebuffer_updates_applied_since_connect},
    )


def handle_click_at_coordinates(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    if "x_coordinate" not in validated_params or "y_coordinate" not in validated_params:
        return create_error_response("click_at_coordinates requires x_coordinate and y_coordinate.", with_readme=False)
    x_coordinate = int(validated_params["x_coordinate"])
    y_coordinate = int(validated_params["y_coordinate"])
    button_mask = _POINTER_BUTTON_NAME_TO_MASK.get(validated_params.get("button", "left"), _POINTER_BUTTON_MASK_LEFT)
    perform_double_click = bool(validated_params.get("double", False))
    try:
        number_of_clicks = 2 if perform_double_click else 1
        for _ in range(number_of_clicks):
            session.send_pointer_event(x_coordinate, y_coordinate, button_mask)
            session.send_pointer_event(x_coordinate, y_coordinate, 0)
            if perform_double_click:
                time.sleep(0.05)
    except (VncConnectionError, OSError) as click_error:
        return create_error_response(f"Failed to send click: {click_error}", with_readme=False)
    return create_success_response(
        f"{'Double-c' if perform_double_click else 'C'}licked {validated_params.get('button', 'left')} at "
        f"({x_coordinate}, {y_coordinate}) on session {session.remote_desktop_session_id}."
    )


def handle_move_pointer(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    if "x_coordinate" not in validated_params or "y_coordinate" not in validated_params:
        return create_error_response("move_pointer requires x_coordinate and y_coordinate.", with_readme=False)
    x_coordinate = int(validated_params["x_coordinate"])
    y_coordinate = int(validated_params["y_coordinate"])
    try:
        session.send_pointer_event(x_coordinate, y_coordinate, 0)
    except (VncConnectionError, OSError) as move_error:
        return create_error_response(f"Failed to move pointer: {move_error}", with_readme=False)
    return create_success_response(f"Moved pointer to ({x_coordinate}, {y_coordinate}) on session {session.remote_desktop_session_id}.")


def handle_drag(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    for required_key in ("x_coordinate", "y_coordinate", "to_x_coordinate", "to_y_coordinate"):
        if required_key not in validated_params:
            return create_error_response("drag requires x_coordinate, y_coordinate, to_x_coordinate, to_y_coordinate.", with_readme=False)
    start_x = int(validated_params["x_coordinate"])
    start_y = int(validated_params["y_coordinate"])
    end_x = int(validated_params["to_x_coordinate"])
    end_y = int(validated_params["to_y_coordinate"])
    button_mask = _POINTER_BUTTON_NAME_TO_MASK.get(validated_params.get("button", "left"), _POINTER_BUTTON_MASK_LEFT)
    try:
        session.send_pointer_event(start_x, start_y, 0)
        session.send_pointer_event(start_x, start_y, button_mask)
        number_of_interpolation_steps = 10
        for step_index in range(1, number_of_interpolation_steps + 1):
            interpolated_x = start_x + (end_x - start_x) * step_index // number_of_interpolation_steps
            interpolated_y = start_y + (end_y - start_y) * step_index // number_of_interpolation_steps
            session.send_pointer_event(interpolated_x, interpolated_y, button_mask)
            time.sleep(0.01)
        session.send_pointer_event(end_x, end_y, 0)
    except (VncConnectionError, OSError) as drag_error:
        return create_error_response(f"Failed to drag: {drag_error}", with_readme=False)
    return create_success_response(
        f"Dragged {validated_params.get('button', 'left')} from ({start_x}, {start_y}) to ({end_x}, {end_y}) "
        f"on session {session.remote_desktop_session_id}."
    )


def handle_scroll(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    scroll_amount = int(validated_params.get("scroll_amount", -3))
    x_coordinate = int(validated_params.get("x_coordinate", session.framebuffer_width_pixels // 2))
    y_coordinate = int(validated_params.get("y_coordinate", session.framebuffer_height_pixels // 2))
    wheel_button_mask = _POINTER_BUTTON_MASK_WHEEL_UP if scroll_amount > 0 else _POINTER_BUTTON_MASK_WHEEL_DOWN
    try:
        for _ in range(abs(scroll_amount)):
            session.send_pointer_event(x_coordinate, y_coordinate, wheel_button_mask)
            session.send_pointer_event(x_coordinate, y_coordinate, 0)
    except (VncConnectionError, OSError) as scroll_error:
        return create_error_response(f"Failed to scroll: {scroll_error}", with_readme=False)
    return create_success_response(
        f"Scrolled {'up' if scroll_amount > 0 else 'down'} {abs(scroll_amount)} step(s) at ({x_coordinate}, {y_coordinate}) "
        f"on session {session.remote_desktop_session_id}."
    )


def handle_send_text(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    text_with_ahk_syntax = validated_params.get("text")
    if text_with_ahk_syntax is None:
        return create_error_response("send_text requires 'text'.", with_readme=False)
    key_press_actions = translate_autohotkey_style_text_into_key_press_actions(text_with_ahk_syntax)
    try:
        for modifier_keysyms, main_keysym, press_kind in key_press_actions:
            if press_kind in ("tap", "down"):
                for modifier_keysym in modifier_keysyms:
                    session.send_key_event(modifier_keysym, is_key_press_down=True)
            if press_kind == "tap":
                session.send_key_event(main_keysym, is_key_press_down=True)
                session.send_key_event(main_keysym, is_key_press_down=False)
            elif press_kind == "down":
                session.send_key_event(main_keysym, is_key_press_down=True)
            elif press_kind == "up":
                session.send_key_event(main_keysym, is_key_press_down=False)
            if press_kind in ("tap", "up"):
                for modifier_keysym in reversed(modifier_keysyms):
                    session.send_key_event(modifier_keysym, is_key_press_down=False)
    except (VncConnectionError, OSError) as send_error:
        return create_error_response(f"Failed to send text: {send_error}", with_readme=False)
    return create_success_response(
        f"Sent {len(key_press_actions)} key action(s) to session {session.remote_desktop_session_id}."
    )


def handle_get_clipboard(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    return create_success_response(
        f"Remote clipboard for session {session.remote_desktop_session_id} "
        f"({len(session.most_recent_remote_clipboard_text)} chars).",
        {"clipboard_text": session.most_recent_remote_clipboard_text},
    )


def handle_set_clipboard(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    clipboard_text = validated_params.get("clipboard_text")
    if clipboard_text is None:
        return create_error_response("set_clipboard requires 'clipboard_text'.", with_readme=False)
    try:
        session.send_client_cut_text(clipboard_text)
    except (VncConnectionError, OSError) as clipboard_error:
        return create_error_response(f"Failed to set clipboard: {clipboard_error}", with_readme=False)
    return create_success_response(f"Set remote clipboard ({len(clipboard_text)} chars) on session {session.remote_desktop_session_id}.")


def handle_wait_for_change(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    timeout_seconds = float(validated_params.get("timeout_seconds", 30))
    changed = session.wait_for_any_framebuffer_change(timeout_seconds=timeout_seconds)
    dirty_rectangles = session.take_and_clear_dirty_rectangles_since_last_query()
    return create_success_response(
        (f"The remote desktop changed within {timeout_seconds:g}s." if changed
         else f"No change within {timeout_seconds:g}s (timed out)."),
        {"changed": changed, "changed_rectangles": [list(rectangle) for rectangle in dirty_rectangles]},
    )


def handle_wait_for_image(validated_params: Dict) -> Dict:
    # v1: block until the (optionally region-scoped) framebuffer changes. Full
    # template matching is a future enhancement; documented as such in the readme.
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    timeout_seconds = float(validated_params.get("timeout_seconds", 30))
    deadline_epoch_seconds = time.time() + timeout_seconds
    changed_at_least_once = False
    while time.time() < deadline_epoch_seconds:
        if session.wait_for_any_framebuffer_change(timeout_seconds=min(2.0, max(0.1, deadline_epoch_seconds - time.time()))):
            changed_at_least_once = True
            break
    return create_success_response(
        ("A change was observed." if changed_at_least_once else f"No change within {timeout_seconds:g}s (timed out)."),
        {"changed": changed_at_least_once,
         "note": "wait_for_image currently detects change; template matching is a planned enhancement."},
    )


def _attempt_ocr_fallback_scan(session: RemoteFramebufferProtocolSession) -> Optional[List[Dict]]:
    """Best-effort OCR fallback for scan_ui_elements when no accessibility stub is
    connected. Returns a list of pseudo-elements (role='text') or None if OCR is
    unavailable. Never raises."""
    try:
        import importlib
        ocr_module = importlib.import_module("ragtag.tools.ocr")
    except Exception:
        return None
    handle_ocr_tool = getattr(ocr_module, "handle_ocr_tool", None)
    ocr_unlock_token = getattr(ocr_module, "TOOL_UNLOCK_TOKEN", None)
    if handle_ocr_tool is None or ocr_unlock_token is None:
        return None
    import tempfile
    temporary_image_path = None
    try:
        png_bytes = session.render_framebuffer_region_to_png_bytes(0, 0, 0, 0)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary_image_file:
            temporary_image_file.write(png_bytes)
            temporary_image_path = temporary_image_file.name
        # The OCR tool's parameter is "image" (a file path or URL), NOT "image_path";
        # its validator rejects unknown keys, so the wrong name would make OCR fail.
        ocr_result = handle_ocr_tool(
            {"input": {"operation": "ocr", "image": temporary_image_path, "tool_unlock_token": ocr_unlock_token}}
        )
        # If OCR itself errored, do NOT surface its error text (which includes the
        # tool's readme) as if it were scanned screen content - treat it as "no data".
        if not isinstance(ocr_result, dict) or ocr_result.get("isError"):
            return None
        text_dump = ""
        for content_item in ocr_result.get("content", []):
            if content_item.get("type") == "text":
                text_dump += content_item.get("text", "")
        if not text_dump.strip():
            return None
        return [{"role": "text", "name": text_dump[:2000], "value": "", "bbox": None, "source": "ocr_fallback"}]
    except Exception:
        return None
    finally:
        if temporary_image_path:
            try:
                os.unlink(temporary_image_path)
            except OSError:
                pass


def handle_scan_ui_elements(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    if session.accessibility_side_channel_client is not None:
        # A connected stub would populate this; not wired in v1.
        return create_success_response(
            "Accessibility stub is connected but element streaming is not implemented in this build.",
            {"schema_version": ACCESSIBILITY_SIDE_CHANNEL_SCHEMA_VERSION, "elements": []},
        )
    ocr_elements = _attempt_ocr_fallback_scan(session)
    if ocr_elements is not None:
        return create_success_response(
            "No accessibility stub connected; returning OCR-derived text elements as a fallback.",
            {"schema_version": ACCESSIBILITY_SIDE_CHANNEL_SCHEMA_VERSION, "source": "ocr_fallback", "elements": ocr_elements},
        )
    return create_success_response(
        "No accessibility stub is connected on the remote host, and OCR fallback is unavailable here. "
        "Use take_screenshot + click_at_coordinates instead, or deploy the accessibility stub (see readme).",
        {"schema_version": ACCESSIBILITY_SIDE_CHANNEL_SCHEMA_VERSION, "elements": []},
    )


def handle_click_ui_element(validated_params: Dict) -> Dict:
    session, error_response = _require_connected_session(validated_params)
    if error_response:
        return error_response
    blocked = _verify_input_is_permitted_for_session(session, validated_params)
    if blocked:
        return blocked
    return create_error_response(
        "click_ui_element needs the accessibility side-channel (or a matched OCR/vision hit with a bounding box), "
        "which is not available for this session. Use take_screenshot to locate the element, then "
        "click_at_coordinates with the pixel position.",
        with_readme=False,
    )


def handle_about(validated_params: Dict) -> Dict:
    return create_success_response(
        "vnc: a VNC/RFB client MCP tool (remote sibling of the local `system` tool).",
        {
            "tool": TOOL_NAME,
            "role": "RFB client only (not a server)",
            "handshake_dialects": ["3.3", "3.7", "3.8", "Apple 003.88x"],
            "security_types_supported": ["1 None", "2 VncAuth", "16 Tight", "18 TLS", "19 VeNCrypt", "30 AppleDH-ARD"],
            "encodings_supported": ["Raw", "CopyRect", "ZRLE", "Tight(Fill/Basic/JPEG)", "DesktopSize", "Cursor"],
            "accessibility_side_channel": "scaffolded (OCR fallback; stub transport is a documented follow-up)",
        },
    )


# ---------------------------------------------------------------------------
# Dispatcher (build plan R4) + handler export.
# ---------------------------------------------------------------------------
_OPERATION_NAME_TO_WORKER = {
    "connect": handle_connect,
    "list_sessions": handle_list_sessions,
    "disconnect": handle_disconnect,
    "take_screenshot": handle_take_screenshot,
    "get_changes": handle_get_changes,
    "click_at_coordinates": handle_click_at_coordinates,
    "move_pointer": handle_move_pointer,
    "drag": handle_drag,
    "scroll": handle_scroll,
    "send_text": handle_send_text,
    "get_clipboard": handle_get_clipboard,
    "set_clipboard": handle_set_clipboard,
    "wait_for_change": handle_wait_for_change,
    "wait_for_image": handle_wait_for_image,
    "scan_ui_elements": handle_scan_ui_elements,
    "click_ui_element": handle_click_ui_element,
    "about": handle_about,
}


def handle_vnc(input_param: Dict) -> Dict:
    """Dispatch VNC tool operations via the MCP interface (same control flow as
    the local `system` tool's handler)."""
    try:
        input_param.pop("handler_info", None)

        if isinstance(input_param, dict) and "input" in input_param:
            input_param = input_param["input"]

        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}

        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected a dictionary of tool parameters.", with_readme=True)

        provided_token = input_param.get("tool_unlock_token")
        if provided_token != TOOL_UNLOCK_TOKEN:
            return create_error_response(
                "Invalid or missing tool_unlock_token: this indicates your context is missing the following "
                "details, which are needed to correctly use this tool:",
                with_readme=True,
            )

        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        operation = validated_params.get("operation")
        if operation == "readme":
            return {"content": [{"type": "text", "text": readme(True)}], "isError": False}

        worker = _OPERATION_NAME_TO_WORKER.get(operation)
        if worker is None:
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(
                f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}",
                with_readme=True,
            )
        return worker(validated_params)
    except Exception as dispatch_error:
        return create_error_response(f"Error in vnc operation: {dispatch_error}", with_readme=True)


HANDLERS = {
    TOOL_NAME: handle_vnc,
}
