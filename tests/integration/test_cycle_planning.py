import base64
from datetime import UTC, datetime, timedelta

import pytest

from app.campaigns.models import Button, Creative, Destination
from app.campaigns.service import CampaignService
from app.campaigns.shuffle import cohort_map


class MemoryRepositories:
    def __init__(self, campaign, sources):
        self.campaign = campaign
        self.sources = sources
        self.cycles = []
        self.deliveries = []
        self.ending = []

    async def active_channels(self, selector):
        return self.sources

    async def get_campaign(self, campaign_id):
        return self.campaign if campaign_id == self.campaign["campaign_id"] else None

    async def create_cycle(self, cycle, deliveries):
        if not any(existing["cycle_number"] == cycle["cycle_number"] for existing in self.cycles):
            self.cycles.append(cycle)
        existing = {(row["cycle_number"], row["channel_id"]) for row in self.deliveries}
        self.deliveries.extend(row for row in deliveries if (row["cycle_number"], row["channel_id"]) not in existing)
        return True

    async def update_campaign(self, campaign_id, update):
        self.campaign.update(update)
        return True

    async def mark_campaign_ending(self, campaign_id, reason):
        self.ending.append(reason)
        return True


@pytest.mark.asyncio
async def test_2000_channel_cycle_is_bounded_excludes_destination_and_changes_order() -> None:
    now = datetime.now(UTC)
    source_ids = list(range(-100000, -98000))
    protected = source_ids[0]
    target_ids = source_ids[1:]
    seed = b"x" * 32
    cohorts = cohort_map(target_ids, 3, b"c" * 32)
    campaign = {
        "campaign_id": "cmp_test",
        "status": "ACTIVE",
        "mode": "MIX_ROTATE",
        "start_at_utc": now - timedelta(seconds=1),
        "current_end_at_utc": now + timedelta(hours=3),
        "repost_interval_seconds": 3600,
        "target_snapshot": target_ids,
        "cohort_map": {str(key): value for key, value in cohorts.items()},
        "shuffle_seed": base64.urlsafe_b64encode(seed).decode(),
        "variants": [{}, {}, {}],
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": channel_id} for channel_id in source_ids])
    service = CampaignService(repositories, send_rps=20)
    assert await service.plan_due_cycle(campaign, now)
    assert len(repositories.deliveries) == 1_999
    assert protected not in {delivery["channel_id"] for delivery in repositories.deliveries}
    first_order = [item["channel_id"] for item in sorted(repositories.deliveries, key=lambda item: item["dispatch_rank"])]
    assert await service.plan_due_cycle(campaign, now + timedelta(hours=1, seconds=1))
    second = [item for item in repositories.deliveries if item["cycle_number"] == 1]
    second_order = [item["channel_id"] for item in sorted(second, key=lambda item: item["dispatch_rank"])]
    assert first_order != second_order
    assert max(delivery["cohort_index"] for delivery in second) == 2


@pytest.mark.asyncio
async def test_launch_summary_shows_exact_source_and_protected_destination_counts() -> None:
    now = datetime.now(UTC)
    creative = Creative(
        id="var_1",
        kind="TEXT",
        text="Hello",
        buttons=[Button(id="btn_1", text="Join", url="https://t.me/example")],
    )
    campaign = {
        "campaign_id": "cmp_summary",
        "status": "DRAFT",
        "mode": "STANDARD",
        "variants": [creative.model_dump(mode="json")],
        "destinations": [Destination(display_name="Promoted", telegram_chat_id=-1002).model_dump(mode="json")],
        "target_selector": {},
        "start_at_utc": now + timedelta(minutes=1),
        "current_end_at_utc": now + timedelta(hours=2),
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "owner_timezone": "UTC",
        "preview_sent": True,
    }
    repositories = MemoryRepositories(
        campaign,
        [
            {"telegram_chat_id": -1001},
            {"telegram_chat_id": -1002},
            {"telegram_chat_id": -1003},
        ],
    )
    _, errors, source_count, protected_count, eligible_count = await CampaignService(repositories, send_rps=20).launch_summary("cmp_summary")
    assert not errors
    assert (source_count, protected_count, eligible_count) == (3, 1, 2)


@pytest.mark.asyncio
async def test_activate_accepts_legacy_naive_mongo_timestamps() -> None:
    now = datetime.now(UTC)
    creative = Creative(id="var_1", kind="TEXT", text="Hello")
    campaign = {
        "campaign_id": "cmp_naive_dates",
        "status": "DRAFT",
        "mode": "STANDARD",
        "variants": [creative.model_dump(mode="json")],
        "destinations": [],
        "target_selector": {},
        # PyMongo returned this form before tz_aware=True was configured.
        "start_at_utc": (now - timedelta(minutes=1)).replace(tzinfo=None),
        "current_end_at_utc": (now + timedelta(hours=1)).replace(tzinfo=None),
        "repost_interval_seconds": None,
        "delete_on_repost": True,
        "delete_on_end": True,
        "preview_sent": True,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    activated = await CampaignService(repositories, send_rps=20).activate("cmp_naive_dates")
    assert activated["status"] == "ACTIVE"
    assert activated["target_snapshot"] == [-1001]
    assert len(repositories.cycles) == 1
    assert len(repositories.deliveries) == 1


@pytest.mark.asyncio
async def test_specific_repost_times_create_only_the_requested_cycles() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(seconds=1)
    campaign = {
        "campaign_id": "cmp_specific_times",
        "status": "ACTIVE",
        "mode": "STANDARD",
        "start_at_utc": start,
        "current_end_at_utc": start + timedelta(days=7),
        "repost_interval_seconds": None,
        "repost_offsets_seconds": [3600, 4 * 24 * 3600],
        "target_snapshot": [-1001],
        "cohort_map": {"-1001": 0},
        "shuffle_seed": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "variants": [{}],
        "next_cycle_number": 0,
    }
    repositories = MemoryRepositories(campaign, [{"telegram_chat_id": -1001}])
    service = CampaignService(repositories, send_rps=20)
    assert await service.plan_due_cycle(campaign, now)
    assert await service.plan_due_cycle(campaign, start + timedelta(hours=1, seconds=1))
    assert await service.plan_due_cycle(campaign, start + timedelta(days=4, seconds=1))
    assert [cycle["cycle_number"] for cycle in repositories.cycles] == [0, 1, 2]
    assert not await service.plan_due_cycle(campaign, start + timedelta(days=5))
