from datetime import UTC, datetime, timedelta

from app.campaigns.models import Button, CampaignMode, Creative, Destination, Schedule
from app.campaigns.validation import validate_launch


def creative() -> Creative:
    return Creative(id="v1", kind="TEXT", text="hello", buttons=[Button(id="b", text="Join", url="https://t.me/example")])


def test_destination_ids_are_excluded_from_eligible_sources() -> None:
    now = datetime.now(UTC)
    errors = validate_launch(
        variants=[creative()], destinations=[Destination(display_name="Dest", telegram_chat_id=-1001)],
        source_ids={-1001}, mode=CampaignMode.STANDARD,
        schedule=Schedule(start_at_utc=now, end_at_utc=now + timedelta(hours=1)), preview_sent=True, send_rps=20,
    )
    assert any("No eligible sources" in error for error in errors)


def test_long_campaign_needs_safe_repost_interval() -> None:
    now = datetime.now(UTC)
    errors = validate_launch(
        variants=[creative()], destinations=[], source_ids={-1002}, mode=CampaignMode.STANDARD,
        schedule=Schedule(start_at_utc=now, end_at_utc=now + timedelta(days=7), repost_interval_seconds=48 * 3600),
        preview_sent=True, send_rps=20,
    )
    assert any("47 hours" in error for error in errors)


def test_preview_is_required() -> None:
    now = datetime.now(UTC)
    errors = validate_launch(
        variants=[creative()], destinations=[], source_ids={-1002}, mode=CampaignMode.STANDARD,
        schedule=Schedule(start_at_utc=now, end_at_utc=now + timedelta(hours=1)), preview_sent=False, send_rps=20,
    )
    assert errors == ["Send a real Telegram preview before launch."]


def test_incomplete_rotation_is_rejected_if_it_bypasses_auto_fit() -> None:
    now = datetime.now(UTC)
    variants = [
        Creative(id=f"v{index}", kind="TEXT", text=f"hello {index}")
        for index in range(3)
    ]
    errors = validate_launch(
        variants=variants,
        destinations=[],
        source_ids={-1002},
        mode=CampaignMode.MIX_ROTATE,
        schedule=Schedule(
            start_at_utc=now,
            end_at_utc=now + timedelta(minutes=15),
            repost_interval_seconds=10 * 60,
        ),
        preview_sent=True,
        send_rps=20,
    )

    assert any("one cycle per variant" in error for error in errors)
