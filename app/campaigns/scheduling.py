from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.campaigns.models import CampaignMode


@dataclass(frozen=True)
class RotationScheduleFit:
    """A launchable cadence plus owner-facing explanations for any repair."""

    end_at: datetime
    interval_seconds: int | None
    offsets_seconds: list[int] | None
    notes: tuple[str, ...] = ()

    @property
    def adjusted(self) -> bool:
        return bool(self.notes)


def scheduled_cycle_count(
    start_at: datetime,
    end_at: datetime,
    interval_seconds: int | None,
    repost_offsets_seconds: list[int] | None = None,
) -> int:
    """Return cycles whose scheduled time is strictly inside the campaign window."""
    duration_seconds = max(0, int((end_at - start_at).total_seconds()))
    if duration_seconds <= 0:
        return 0
    if repost_offsets_seconds is not None:
        return 1 + sum(1 for value in repost_offsets_seconds if 0 < int(value) < duration_seconds)
    if interval_seconds:
        return 1 + (duration_seconds - 1) // int(interval_seconds)
    return 1


def fit_rotation_schedule(
    *,
    start_at: datetime,
    end_at: datetime,
    interval_seconds: int | None,
    repost_offsets_seconds: list[int] | None,
    mode: CampaignMode | str,
    variant_count: int,
    minimum_cycle_seconds: int = 60,
) -> RotationScheduleFit:
    """Repair a cadence so every rotating variant can reach every target.

    The final gap is kept large enough for the final delivery cycle. Explicit
    uneven plans are preserved when they are already complete and safe. An
    incomplete or overlapping plan is replaced by an evenly spaced plan; this
    is preferable to accepting a campaign that can never finish its rotation.
    """
    mode_value = mode.value if isinstance(mode, CampaignMode) else str(mode)
    original_offsets = list(repost_offsets_seconds) if repost_offsets_seconds is not None else None
    if mode_value == CampaignMode.STANDARD.value or variant_count < 2:
        return RotationScheduleFit(end_at, interval_seconds, original_offsets)

    duration_seconds = int((end_at - start_at).total_seconds())
    if duration_seconds <= 0:
        return RotationScheduleFit(end_at, interval_seconds, original_offsets)

    minimum_cycle_seconds = max(60, int(minimum_cycle_seconds))
    notes: list[str] = []

    if original_offsets is not None:
        cycle_total = max(variant_count, len(original_offsets) + 1)
        points = [0, *original_offsets, duration_seconds]
        complete = len(original_offsets) + 1 >= variant_count
        safe_gaps = all(right - left >= minimum_cycle_seconds for left, right in zip(points, points[1:], strict=False))
        if complete and safe_gaps:
            return RotationScheduleFit(end_at, None, original_offsets)

        required_duration = cycle_total * minimum_cycle_seconds
        if duration_seconds < required_duration:
            duration_seconds = required_duration
            end_at = start_at + timedelta(seconds=duration_seconds)
            notes.append("the campaign end was extended so every delivery cycle has time to finish")
        step = max(minimum_cycle_seconds, duration_seconds // cycle_total)
        fitted = [step * index for index in range(1, cycle_total)]
        notes.append(
            f"the custom repost plan was re-spaced into {cycle_total} cycles so all {variant_count} variants complete a full rotation"
        )
        return RotationScheduleFit(end_at, None, fitted, tuple(dict.fromkeys(notes)))

    interval = int(interval_seconds) if interval_seconds else None
    if duration_seconds < variant_count * minimum_cycle_seconds:
        duration_seconds = variant_count * minimum_cycle_seconds
        end_at = start_at + timedelta(seconds=duration_seconds)
        notes.append("the campaign end was extended so every delivery cycle has time to finish")

    if interval is None or interval < minimum_cycle_seconds or scheduled_cycle_count(start_at, end_at, interval) < variant_count:
        interval = max(minimum_cycle_seconds, duration_seconds // variant_count)
        notes.append(f"the repost interval was adjusted to complete all {variant_count} variant cycles")

    cycle_total = scheduled_cycle_count(start_at, end_at, interval)
    final_cycle_at = (cycle_total - 1) * interval
    final_gap = duration_seconds - final_cycle_at
    if final_gap < minimum_cycle_seconds:
        end_at += timedelta(seconds=minimum_cycle_seconds - final_gap)
        notes.append("the campaign end was moved slightly so the final rotation cycle can finish")

    return RotationScheduleFit(end_at, interval, None, tuple(dict.fromkeys(notes)))


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
