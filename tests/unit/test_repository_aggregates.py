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


class ShareCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args):
        return self

    async def to_list(self, _length):
        return list(self.rows)


class VariantShareCollection:
    def __init__(self):
        self.documents = []

    @staticmethod
    def _matches(document, query):
        for key, expected in query.items():
            value = document.get(key)
            if isinstance(expected, dict) and "$ne" in expected:
                if value == expected["$ne"]:
                    return False
            elif value != expected:
                return False
        return True

    async def find_one(self, query):
        return next((item for item in self.documents if self._matches(item, query)), None)

    async def insert_one(self, document):
        self.documents.append(dict(document))
        return SimpleNamespace(inserted_id=document["share_code"])

    def find(self, query):
        return ShareCursor(item for item in self.documents if self._matches(item, query))

    async def update_one(self, query, update):
        document = await self.find_one(query)
        if not document:
            return SimpleNamespace(modified_count=0)
        document.update(update.get("$set", {}))
        for key, amount in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + amount
        return SimpleNamespace(modified_count=1)

    async def update_many(self, query, update):
        matches = [item for item in self.documents if self._matches(item, query)]
        for document in matches:
            document.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=len(matches))


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


def test_audience_selector_supports_minimum_and_bounded_size_ranges() -> None:
    repositories = Repositories.__new__(Repositories)

    assert repositories._active_channel_query({"minimum_members": 1_000})["member_count"] == {"$gte": 1_000}
    assert repositories._active_channel_query({"minimum_members": 1_000, "maximum_members": 50_000})["member_count"] == {
        "$gte": 1_000,
        "$lte": 50_000,
    }


@pytest.mark.asyncio
async def test_variant_share_repository_reuses_exact_snapshots_and_revokes_them() -> None:
    collection = VariantShareCollection()
    repositories = Repositories.__new__(Repositories)
    repositories.db = SimpleNamespace(variant_shares=collection)
    arguments = {
        "owner_id": 11,
        "campaign_id": "cmp",
        "campaign_name": "Campaign",
        "variant_id": "var_1",
        "variant_index": 0,
        "variant_revision": 2,
        "snapshot_hash": "hash-one",
        "creative": {"id": "var_1", "kind": "TEXT", "text": "Hello"},
    }

    created = await repositories.get_or_create_variant_share(**arguments)
    reused = await repositories.get_or_create_variant_share(**arguments)
    assert reused["share_code"] == created["share_code"]
    assert len(collection.documents) == 1

    newer = await repositories.get_or_create_variant_share(
        **{**arguments, "snapshot_hash": "hash-two", "creative": {**arguments["creative"], "text": "Changed"}}
    )
    assert newer["share_code"] != created["share_code"]
    assert len(await repositories.active_variant_shares(11, "cmp", "var_1")) == 2

    assert await repositories.revoke_variant_share(created["share_code"], 11)
    assert await repositories.get_variant_share(created["share_code"], 11) is None
    assert collection.documents[0]["purge_at"] > collection.documents[0]["revoked_at"]
