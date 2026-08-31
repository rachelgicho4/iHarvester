from datetime import UTC, datetime, timedelta

from app.campaigns.models import Button
from app.campaigns.scheduling import can_create_cycle, scheduled_cycle_time
from app.telegram.handlers_owner import campaign_keyboard, content_type_keyboard, quick_duration_keyboard, quick_interval_keyboard
from app.telegram.keyboards import auto_button_rows


def button(label: str, row: int = 0) -> Button:
    return Button(id=label, text=label, url="https://t.me/example", row=row)


def test_cycle_end_is_strictly_before_campaign_end() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)
    assert can_create_cycle(start, end, 0, 3600)
    assert can_create_cycle(start, end, 1, 3600)
    assert not can_create_cycle(start, end, 2, 3600)
    assert scheduled_cycle_time(start, 1, 3600) == start + timedelta(hours=1)


def test_horizontal_first_buttons_wrap_without_truncation() -> None:
    assert len(auto_button_rows([button("ONE"), button("TWO"), button("THREE")])) == 1
    rows = auto_button_rows([button("A deliberately long label"), button("Another long label")])
    assert [item.text for row in rows for item in row] == ["A deliberately long label", "Another long label"]
    assert len(rows) == 2


def test_custom_rows_are_respected() -> None:
    rows = auto_button_rows([button("A", 1), button("B", 0), button("C", 1)], "CUSTOM")
    assert [[item.text for item in row] for row in rows] == [["B"], ["A", "C"]]


def test_guided_creator_uses_callback_controls_not_pipe_delimited_commands() -> None:
    content_controls = [item.text for row in content_type_keyboard("cmp").inline_keyboard for item in row]
    campaign_controls = [item.text for row in campaign_keyboard("cmp", "DRAFT").inline_keyboard for item in row]
    assert {"Text", "Photo", "Photo + caption", "Video", "Video + caption", "Forward ready post"} <= set(content_controls)
    assert {"CTA buttons", "Destinations", "Targets", "Plan for later", "Send campaign", "Delete draft", "Home"} <= set(campaign_controls)


def test_quick_send_controls_offer_duration_and_safe_interval_choices() -> None:
    duration_controls = [item.text for row in quick_duration_keyboard("cmp").inline_keyboard for item in row]
    interval_controls = [item.text for row in quick_interval_keyboard("cmp", 30 * 24 * 60, 0).inline_keyboard for item in row]
    assert {"15 minutes", "1 day", "30 days", "Back", "Home"} <= set(duration_controls)
    assert "Use safe default: every 24h" in interval_controls
    assert "Post once" not in interval_controls
