from __future__ import annotations

from datetime import datetime, timedelta


def scheduled_cycle_time(start_at: datetime, cycle_number: int, interval_seconds: int | None) -> datetime:
    if cycle_number < 0:
        raise ValueError("cycle number cannot be negative")
    if cycle_number and not interval_seconds:
        raise ValueError("single-post campaigns only have cycle 0")
    return start_at + timedelta(seconds=(interval_seconds or 0) * cycle_number)


def can_create_cycle(start_at: datetime, end_at: datetime, cycle_number: int, interval_seconds: int | None) -> bool:
    return scheduled_cycle_time(start_at, cycle_number, interval_seconds) < end_at

