"""Bound MongoDB growth without discarding campaign-level results."""

from __future__ import annotations

import asyncio
import logging

from pymongo.errors import PyMongoError

from app.db.leases import LeaseManager
from app.db.repositories import Repositories

logger = logging.getLogger(__name__)


class HistoryCompactor:
    """Periodically replace verbose old delivery cycles with aggregate rows."""

    def __init__(
        self,
        *,
        instance_id: str,
        repositories: Repositories,
        lease_manager: LeaseManager,
        interval_seconds: float,
        active_cycles_to_keep: int,
        archived_cycles_to_keep: int,
        max_cycles_per_run: int,
    ) -> None:
        self.instance_id = instance_id
        self.repositories = repositories
        self.lease_manager = lease_manager
        self.interval_seconds = interval_seconds
        self.active_cycles_to_keep = active_cycles_to_keep
        self.archived_cycles_to_keep = archived_cycles_to_keep
        self.max_cycles_per_run = max_cycles_per_run

    async def run(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                try:
                    compacted = await self.tick()
                    if compacted:
                        logger.info("Compacted %s completed campaign cycle(s)", compacted)
                except PyMongoError:
                    logger.warning("Campaign history compactor paused: MongoDB unavailable")
                except Exception:
                    logger.exception("Campaign history compactor pass failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.lease_manager.release("campaign_history_compactor", self.instance_id)

    async def tick(self) -> int:
        # A separate lease prevents two overlapping web instances from both
        # scanning a large campaign history. The repository itself also uses a
        # per-cycle unique rollup key, so an interrupted pass remains safe.
        if not await self.lease_manager.acquire_or_renew("campaign_history_compactor", self.instance_id, 120):
            return 0
        return await self.repositories.compact_completed_campaign_history(
            active_cycles_to_keep=self.active_cycles_to_keep,
            archived_cycles_to_keep=self.archived_cycles_to_keep,
            max_cycles=self.max_cycles_per_run,
        )
