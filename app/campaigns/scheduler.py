from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.campaigns.service import CampaignService
from app.db.leases import LeaseManager
from app.db.repositories import Repositories
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        *,
        instance_id: str,
        repositories: Repositories,
        lease_manager: LeaseManager,
        campaign_service: CampaignService,
        lease_seconds: int,
        tick_seconds: float,
    ) -> None:
        self.instance_id = instance_id
        self.repositories = repositories
        self.lease_manager = lease_manager
        self.campaign_service = campaign_service
        self.lease_seconds = lease_seconds
        self.tick_seconds = tick_seconds

    async def run(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                try:
                    await self.tick()
                except Exception:
                    # Keep the task alive: a temporary Mongo/network failure
                    # must not permanently stop all future reposts.
                    logger.exception("Campaign scheduler tick failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self.tick_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.lease_manager.release("scheduler", self.instance_id)

    async def tick(self) -> None:
        if not await self.lease_manager.acquire_or_renew("scheduler", self.instance_id, self.lease_seconds):
            return
        recovered = await self.repositories.recover_interrupted_cleanup_campaigns()
        if recovered:
            logger.warning("Reopened %s campaign(s) with interrupted cleanup", recovered)
        now = utcnow()
        for campaign in await self.repositories.due_campaigns(now):
            try:
                if campaign["status"] == "ENDING":
                    await self._finish_ending_campaign(campaign)
                else:
                    if campaign.get("resume_recovery_needed"):
                        await self.repositories.resume_campaign_deliveries(campaign["campaign_id"])
                        await self.repositories.advance_running_campaign(
                            campaign["campaign_id"],
                            {"resume_recovery_needed": False, "updated_at": now},
                        )
                    await self.campaign_service.plan_due_cycle(campaign, now)
            except Exception:
                logger.exception("Campaign scheduler item failed", extra={"campaign_id": campaign.get("campaign_id")})

    async def _finish_ending_campaign(self, campaign: dict[str, Any]) -> None:
        await self.repositories.cancel_pending_campaign_deliveries(campaign["campaign_id"])
        await self.repositories.finish_complete_cycles(campaign["campaign_id"])
        # A worker may already be inside Telegram's send request. Wait until
        # it persists the returned message IDs so cleanup cannot miss them.
        if not await self.repositories.campaign_send_work_is_quiescent(campaign["campaign_id"]):
            return
        if campaign.get("delete_on_end", True):
            await self.repositories.materialize_cleanup_deliveries(campaign["campaign_id"])
        # When retention was chosen, the live state is deliberately retained:
        # it is the exact campaign-scoped record required if the owner later
        # uses "Delete retained posts". It is not shared with other campaigns.
        if await self.repositories.cleanup_is_complete(campaign["campaign_id"]):
            await self.repositories.mark_campaign_archived(campaign["campaign_id"], campaign.get("end_reason", "ended"))
