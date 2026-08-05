#!/usr/bin/env python3
"""
File: ragtag/tools/den_coordinator_pinned_keys.py
Project: Aura Friday MCP-Link Server
Component: Pinned public key-stack for the Den check-in coordinator (design doc 77)
Author: Christopher Nathan Drake (cnd)

These are PUBLIC keys (iroh EndpointIds = hex of an ed25519 public key). They are safe to
ship baked into every client. Their PRIVATE halves live per doc 77 section 5:
  slot 1  ONLINE on vaf (the running coordinator's identity)
  slot 2  on the operator's dev servers (hot spare)
  slots 3,4,5  COLD STEEL SAFE only (several media incl. paper); slot 5 is indelible.

Two independent uses (never widen each other):
  * coordinator identities (slots 1-3): the ONLY endpoints a device will trust as "the
    coordinator" -- a device dials one of these and pins the verified remote id against
    this list; a `checkin_now` accepted over that connection means only "sync now".
  * key-change authorities (slots 4-5): the ONLY signers a device will honour for a
    `key_stack_update`. slot 4 may re-key slots 1-3; slot 5 (doomsday) may re-key slot 4;
    nothing may re-key slot 5. Their private halves are OFFLINE, so the online coordinator
    can only RELAY pre-signed updates, never forge them.

Rotation: this file is version 1. A future rotation ships a new version of this file AND
is delivered in-band as a signed, chained key_stack_update so long-sleeping devices catch
up (doc 77 section 6). Never edit a slot's id by hand outside that process.

Copyright: (c) 2025-2026 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ᴛWŪīԁıfƬƨᎠуɪďƌdƵ𝟣ꓣNΗНƙɗHΝN৭𝟢𝟢𝟚ʌΑеlꓓЈⅠƛ𝟢ꓬᴛEᏂɯďɌᗷⲟƤɡ2սᴛѡƬμ𝟣7ᖴΑɯʈРLМ5ᛕСɌGwΡhƛmꙅR4սΟ𝟦НJbRꜱȠ4s𝟥EgƌNrЅꙅƨƏⴹSƦJfΗzɌ𐐕Ϩ"
"signdate": "2026-07-29T09:35:14.991Z",
"""

DEN_COORDINATOR_KEY_STACK_VERSION = 1

# slot number -> {role, endpoint_id_hex}. Generated on vaf 2026-07-27 (see
# /home/aura/den-coord-keys/PUBLIC_STACK.json and doc 77).
DEN_COORDINATOR_KEY_STACK = {
    1: {"role": "coordinator-identity-MAIN-online-vaf",
        "endpoint_id_hex": "2f5b6f61424ad34f6f2688cb803a7ae3fe03e2ccfede400fa621abf8fcabfb1e"},
    2: {"role": "coordinator-identity-HOT-spare-dev-servers",
        "endpoint_id_hex": "45b63928067f42a09698b6dbf1ad9afde0a86ae0394931b755207c13fa136609"},
    3: {"role": "coordinator-identity-COLD-spare-safe",
        "endpoint_id_hex": "c015f75226949e63e10d54074de7ddcee8d1fc560763d88eb6063b0f5f1e5b6f"},
    4: {"role": "keychange-authority-COLD-safe-may-rekey-slots-1-3",
        "endpoint_id_hex": "32d100b0e1370010866dc2f2eeb4331265f79a036a81bb9157567a7a3f82abd4"},
    5: {"role": "keychange-authority-COLD-INDELIBLE-doomsday-may-rekey-slot-4",
        "endpoint_id_hex": "9be63ce1acab47e54604f90da1327dad15384028e5523e8439b36ec2f9b628ca"},
}

COORDINATOR_IDENTITY_SLOT_NUMBERS = (1, 2, 3)
KEYCHANGE_AUTHORITY_SLOT_NUMBERS = (4, 5)


def _normalize_endpoint_id_hex(value):
    return (value or "").strip().lower().replace(":", "")


def coordinator_identity_endpoint_ids_hex():
    """The set of endpoint ids (lowercase hex) a device will trust AS the coordinator
    (slots 1-3). Anything dialing/pinging that is NOT one of these is not the coordinator."""
    return {
        _normalize_endpoint_id_hex(DEN_COORDINATOR_KEY_STACK[s]["endpoint_id_hex"])
        for s in COORDINATOR_IDENTITY_SLOT_NUMBERS
    }


def coordinator_identity_endpoint_ids_in_dial_order():
    """Ordered [slot1, slot2, slot3] endpoint ids (lowercase hex) -- a device wake-client
    dials slot 1 (MAIN) first, falling back to 2 (HOT) then 3 (COLD spare)."""
    return [
        _normalize_endpoint_id_hex(DEN_COORDINATOR_KEY_STACK[s]["endpoint_id_hex"])
        for s in COORDINATOR_IDENTITY_SLOT_NUMBERS
    ]


def keychange_authority_endpoint_ids_hex():
    """The set of signer ids (lowercase hex) whose signature a device will honour on a
    key_stack_update (slots 4-5)."""
    return {
        _normalize_endpoint_id_hex(DEN_COORDINATOR_KEY_STACK[s]["endpoint_id_hex"])
        for s in KEYCHANGE_AUTHORITY_SLOT_NUMBERS
    }
