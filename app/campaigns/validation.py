from __future__ import annotations

from datetime import timedelta

from app.campaigns.models import CampaignMode, Creative, Destination, Schedule

SAFE_DELETE_WINDOW = timedelta(hours=47)


def protected_destination_ids(destinations: list[Destination]) -> set[int]:
    return {destination.telegram_chat_id for destination in destinations if destination.telegram_chat_id is not None}


def custom_reposts_support_final_cleanup(schedule: Schedule) -> bool:
    """Every live post must be replaced/ended inside Telegram's safe delete window."""
    offsets = schedule.repost_offsets_seconds or []
    if not offsets:
        return False
    points = [0, *offsets, int((schedule.end_at_utc - schedule.start_at_utc).total_seconds())]
    return all(later - earlier <= int(SAFE_DELETE_WINDOW.total_seconds()) for earlier, later in zip(points, points[1:], strict=False))


def validate_launch(
    *, variants: list[Creative], destinations: list[Destination], source_ids: set[int], mode: CampaignMode,
    schedule: Schedule, preview_sent: bool, send_rps: float,
) -> list[str]:
    errors: list[str] = []
    if not variants:
        errors.append("Add at least one replayable creative variant.")
    if mode != CampaignMode.STANDARD and len(variants) < 2:
        errors.append("Rotate and Mix + Rotate require at least two variants.")
    if schedule.start_at_utc >= schedule.end_at_utc:
        errors.append("Start time must be before end time.")
    protected = protected_destination_ids(destinations)
    eligible = source_ids - protected
    if not eligible:
        errors.append("No eligible sources remain after promoted destinations are excluded.")
    duration = schedule.end_at_utc - schedule.start_at_utc
    if schedule.repost_offsets_seconds and schedule.repost_offsets_seconds[-1] >= int(duration.total_seconds()):
        errors.append("Specific repost times must be before the campaign end time.")
    if schedule.delete_on_end and duration > SAFE_DELETE_WINDOW:
        if schedule.repost_offsets_seconds:
            can_cleanup = custom_reposts_support_final_cleanup(schedule)
        else:
            can_cleanup = bool(schedule.repost_interval_seconds and timedelta(seconds=schedule.repost_interval_seconds) <= SAFE_DELETE_WINDOW)
        if not can_cleanup:
            errors.append("Cleanup needs a repost interval of 47 hours or less for campaigns over 47 hours.")
    if schedule.repost_interval_seconds and len(eligible) / send_rps > schedule.repost_interval_seconds:
        errors.append("Repost interval is shorter than estimated initial-cycle send capacity.")
    if not preview_sent:
        errors.append("Send a real Telegram preview before launch.")
    return errors
