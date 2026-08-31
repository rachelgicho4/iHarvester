from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize timestamps read from old or differently configured Mongo clients."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
