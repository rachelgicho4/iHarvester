from types import SimpleNamespace

import pytest

from app.db.repositories import Repositories


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.requested_length = None

    async def to_list(self, length):
        self.requested_length = length
        return self.rows


class AsyncCollection:
    def __init__(self, rows):
        self.rows = rows
        self.pipeline = None

    async def aggregate(self, pipeline):
        self.pipeline = pipeline
        return Cursor(self.rows)


@pytest.mark.asyncio
async def test_status_aggregates_await_cursor_before_reading_rows() -> None:
    channels = AsyncCollection([{"_id": "ACTIVE", "count": 3}])
    campaigns = AsyncCollection([{"_id": "SCHEDULED", "count": 2}])
    deliveries = AsyncCollection([{"_id": "SENT", "count": 4}])
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(channels=channels, campaigns=campaigns, deliveries=deliveries)

    assert await repositories.channel_status_counts() == {"ACTIVE": 3}
    assert await repositories.campaign_status_counts() == {"SCHEDULED": 2}
    assert await repositories.delivery_summary("cmp", 0) == {"SENT": 4}
