from datetime import UTC, datetime, timedelta

from app.campaigns.models import Button
from app.campaigns.scheduling import can_create_cycle, scheduled_cycle_time
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
