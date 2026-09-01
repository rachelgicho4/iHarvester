from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.backups.export import make_backup
from app.db.leases import LeaseManager
from app.db.repositories import Repositories
from app.utils.time import as_utc, utcnow

logger = logging.getLogger(__name__)


class AutomaticBackupWorker:
    """Coalesce weekly and channel-growth triggers into one owner backup."""

    def __init__(
        self,
        *,
        instance_id: str,
        repositories: Repositories,
        lease_manager: LeaseManager,
        bot: Bot,
        owner_ids: frozenset[int],
        every_new_channels: int,
        interval_hours: int,
        check_seconds: int = 3600,
    ) -> None:
        self.instance_id = instance_id
        self.repositories = repositories
        self.lease_manager = lease_manager
        self.bot = bot
        self.owner_ids = owner_ids
        self.every_new_channels = every_new_channels
        self.interval_hours = interval_hours
        self.check_seconds = check_seconds

    async def run(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Automatic backup check failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self.check_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.lease_manager.release("automatic_backup", self.instance_id)

    async def tick(self) -> bool:
        if not await self.lease_manager.acquire_or_renew("automatic_backup", self.instance_id, 120):
            return False
        if not bool(await self.repositories.get_setting("auto_backup_enabled", True)):
            return False
        now = utcnow()
        every_new_channels = int(await self.repositories.get_setting("auto_backup_every_new_channels", self.every_new_channels))
        interval = timedelta(hours=int(await self.repositories.get_setting("auto_backup_interval_hours", self.interval_hours)))
        channel_count = await self.repositories.channel_count()
        last_at = await self.repositories.get_setting("auto_backup_last_at")
        last_channel_count = int(await self.repositories.get_setting("auto_backup_channel_count", 0))
        growth_due = channel_count - last_channel_count >= every_new_channels
        time_due = bool(last_at and now - as_utc(last_at) >= interval)
        initial_due = last_at is None and channel_count > 0
        if not (growth_due or time_due or initial_due):
            return False

        payload = await make_backup(self.repositories)
        reason = "channel registry growth" if growth_due else "weekly schedule" if time_due else "initial safety copy"
        delivered = 0
        for owner_id in self.owner_ids:
            try:
                await self.bot.send_document(
                    owner_id,
                    BufferedInputFile(payload, filename="iharvester-auto-core-backup.json.gz"),
                    caption=f"Automatic iHarvester core backup ({reason}).",
                )
                delivered += 1
            except Exception:
                logger.exception("Automatic backup delivery failed", extra={"owner_id": owner_id})
        if not delivered:
            return False
        await self.repositories.set_setting("auto_backup_last_at", now)
        await self.repositories.set_setting("auto_backup_channel_count", channel_count)
        return True
