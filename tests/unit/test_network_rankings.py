from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repositories import Repositories
from app.telegram.handlers_owner import OwnerHandlers


class RankedCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sort_spec = None
        self.limit_value = None

    def sort(self, spec):
        self.sort_spec = spec
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def to_list(self, length):
        assert length == self.limit_value
        return self.rows[:length]


class RankedChannels:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.cursor = None

    def find(self, query):
        self.query = query
        self.cursor = RankedCursor(self.rows)
        return self.cursor


@pytest.mark.asyncio
async def test_repository_ranks_only_verified_counts_and_caps_the_view_at_30() -> None:
    channels = RankedChannels([{"member_count": 100}, {"member_count": 50}])
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(channels=channels)

    rows = await repositories.top_channels_by_members(500)

    assert rows == [{"member_count": 100}, {"member_count": 50}]
    assert channels.query == {"member_count": {"$type": "number"}}
    assert channels.cursor.sort_spec == [("member_count", -1), ("title", 1)]
    assert channels.cursor.limit_value == 30


@pytest.mark.asyncio
async def test_top_channels_screen_formats_rank_status_coverage_and_toggle() -> None:
    repositories = SimpleNamespace(
        top_channels_by_members=AsyncMock(
            return_value=[
                {
                    "telegram_chat_id": -1001,
                    "title": "Largest Channel",
                    "username": "largest",
                    "member_count": 1_234_567,
                    "status": "ACTIVE",
                },
                {
                    "telegram_chat_id": -1002,
                    "title": "Private Channel",
                    "username": None,
                    "member_count": 98_765,
                    "status": "NEEDS_ATTENTION",
                },
            ]
        ),
        channel_member_count_coverage=AsyncMock(return_value=(611, 21)),
    )
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = repositories
    handlers._render = AsyncMock()

    await handlers._show_top_channels(object(), 15)

    _, text, markup = handlers._render.await_args.args
    assert "Top 15 channels by subscribers" in text
    assert "1. Largest Channel — 1,234,567 subscribers • @largest • Active" in text
    assert "2. Private Channel — 98,765 subscribers • private • Needs Attention" in text
    assert "Ranked from 611 channels with verified counts" in text
    assert "21 channels are omitted" in text
    controls = {button.text for row in markup.inline_keyboard for button in row}
    assert {"Show top 30", "Back", "Home"} <= controls


@pytest.mark.asyncio
async def test_network_home_exposes_the_top_channels_section() -> None:
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = SimpleNamespace(
        channel_status_counts=AsyncMock(return_value={"ACTIVE": 600, "NEEDS_ATTENTION": 12})
    )
    handlers._render = AsyncMock()

    await handlers._show_network(object())

    markup = handlers._render.await_args.args[2]
    assert "Top channels by subscribers" in {button.text for row in markup.inline_keyboard for button in row}


@pytest.mark.asyncio
async def test_top_30_screen_stays_within_telegram_message_limit() -> None:
    channels = [
        {
            "telegram_chat_id": -1000 - index,
            "title": "A very long channel title that must be kept compact " * 2,
            "username": "x" * 32,
            "member_count": 10_000_000_000 - index,
            "status": "NEEDS_ATTENTION",
        }
        for index in range(30)
    ]
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.repositories = SimpleNamespace(
        top_channels_by_members=AsyncMock(return_value=channels),
        channel_member_count_coverage=AsyncMock(return_value=(600, 20)),
    )
    handlers._render = AsyncMock()

    await handlers._show_top_channels(object(), 30)

    assert len(handlers._render.await_args.args[1]) <= 4096
