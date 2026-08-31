import base64
from datetime import UTC, datetime, timedelta

import pytest

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
        "campaign_id": "cmp_test", "status": "ACTIVE", "mode": "MIX_ROTATE",
        "start_at_utc": now - timedelta(seconds=1), "current_end_at_utc": now + timedelta(hours=3),
        "repost_interval_seconds": 3600, "target_snapshot": target_ids,
        "cohort_map": {str(key): value for key, value in cohorts.items()},
        "shuffle_seed": base64.urlsafe_b64encode(seed).decode(), "variants": [{}, {}, {}], "next_cycle_number": 0,
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
