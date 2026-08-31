from __future__ import annotations

import secrets


def opaque_id(prefix: str) -> str:
    """Short opaque key suitable for callback data, never a serialized UI payload."""
    return f"{prefix}_{secrets.token_urlsafe(9)}"

