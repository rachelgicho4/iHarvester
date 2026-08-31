from __future__ import annotations

from datetime import datetime, timedelta


def scheduled_cycle_time(
    start_at: datetime, cycle_number: int, interval_seconds: int | None, repost_offsets_seconds: list[int] | None = None,
) -> datetime:
    if cycle_number < 0:
        raise ValueError("cycle number cannot be negative")
    if repost_offsets_seconds is not None:
        if cycle_number == 0:
            return start_at
        if cycle_number > len(repost_offsets_seconds):
            raise ValueError("specific repost plan has no further cycles")
        return start_at + timedelta(seconds=repost_offsets_seconds[cycle_number - 1])
    if cycle_number and not interval_seconds:
        raise ValueError("single-post campaigns only have cycle 0")
    return start_at + timedelta(seconds=(interval_seconds or 0) * cycle_number)


def can_create_cycle(
    start_at: datetime, end_at: datetime, cycle_number: int, interval_seconds: int | None, repost_offsets_seconds: list[int] | None = None,
) -> bool:
    try:
        return scheduled_cycle_time(start_at, cycle_number, interval_seconds, repost_offsets_seconds) < end_at
    except ValueError:
        return False
