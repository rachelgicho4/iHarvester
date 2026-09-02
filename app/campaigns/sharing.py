"""Stable identifiers for immutable manual-share snapshots."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

_SHARE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_share_code() -> str:
    """Return a readable opaque code with enough entropy for manual use."""
    token = "".join(secrets.choice(_SHARE_ALPHABET) for _ in range(8))
    return f"HV-{token[:4]}-{token[4:]}"


def normalize_share_code(raw: str) -> str:
    return raw.strip().upper()


def creative_snapshot_hash(creative: dict[str, Any]) -> str:
    """Fingerprint the complete replayable payload, including CTA placement."""
    canonical = json.dumps(creative, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(canonical).hexdigest()
