"""Campaign lifecycle: freeze targets once, preserve history, and never reactivate archives."""

from __future__ import annotations

import base64
import secrets
from datetime import timedelta
from typing import Any

from app.campaigns.models import CampaignMode, CampaignStatus, Creative, Destination, Schedule
from app.campaigns.scheduling import can_create_cycle, scheduled_cycle_time
from app.campaigns.shuffle import cohort_map, dispatch_rank, variant_for
from app.campaigns.validation import protected_destination_ids, validate_launch
from app.db.repositories import Document, Repositories
from app.utils.ids import opaque_id
from app.utils.time import as_utc, utcnow


class CampaignService:
    def __init__(self, repositories: Repositories, send_rps: float) -> None:
        self.repositories = repositories
        self.send_rps = send_rps

    async def create_draft(self, owner_id: int, name: str) -> Document:
        now = utcnow()
        campaign = {
            "campaign_id": opaque_id("cmp"),
            "name": name.strip() or "Untitled campaign",
            "status": CampaignStatus.DRAFT.value,
            "mode": CampaignMode.STANDARD.value,
            "variants": [],
            "destinations": [],
            "target_selector": {},
            "target_snapshot": [],
            "protected_destination_ids": [],
            "cohort_map": {},
            # This default is explicit so a choice made before scheduling is
            # not lost when the schedule is later saved.
            "delete_on_end": True,
            "created_by": owner_id,
            "created_at": now,
            "version": 1,
        }
        await self.repositories.create_campaign(campaign)
        return campaign

    async def configure_draft(
        self,
        campaign_id: str,
        *,
        variants: list[Creative],
        destinations: list[Destination],
        selector: Document,
        mode: CampaignMode,
        schedule: Schedule,
        preview_sent: bool,
    ) -> list[str]:
        campaign = await self._draft(campaign_id)
        active_sources = await self.repositories.active_channels(selector)
        source_ids = {channel["telegram_chat_id"] for channel in active_sources}
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=mode,
            schedule=schedule,
            preview_sent=preview_sent,
            send_rps=self.send_rps,
        )
        if errors:
            return errors
        await self.repositories.update_campaign(
            campaign["campaign_id"],
            {
                "variants": [variant.model_dump(mode="json") for variant in variants],
                "destinations": [destination.model_dump(mode="json") for destination in destinations],
                "target_selector": selector,
                "mode": mode.value,
                "start_at_utc": schedule.start_at_utc,
                "original_end_at_utc": schedule.end_at_utc,
                "current_end_at_utc": schedule.end_at_utc,
                "repost_interval_seconds": schedule.repost_interval_seconds,
                "repost_offsets_seconds": schedule.repost_offsets_seconds,
                "delete_on_repost": schedule.delete_on_repost,
                "delete_on_end": schedule.delete_on_end,
                "owner_timezone": schedule.owner_timezone,
                "preview_sent": preview_sent,
                "updated_at": utcnow(),
            },
        )
        return []

    async def activate(self, campaign_id: str) -> Document:
        campaign = await self._draft(campaign_id)
        variants = [Creative.model_validate(variant) for variant in campaign["variants"]]
        destinations = [Destination.model_validate(destination) for destination in campaign["destinations"]]
        schedule = Schedule(
            start_at_utc=as_utc(campaign["start_at_utc"]),
            end_at_utc=as_utc(campaign["current_end_at_utc"]),
            repost_interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            delete_on_repost=campaign.get("delete_on_repost", True),
            delete_on_end=campaign.get("delete_on_end", True),
            owner_timezone=campaign.get("owner_timezone", "UTC"),
        )
        sources = await self.repositories.active_channels(campaign.get("target_selector"))
        source_ids = {source["telegram_chat_id"] for source in sources}
        now = utcnow()
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=CampaignMode(campaign["mode"]),
            schedule=schedule,
            preview_sent=bool(campaign.get("preview_sent")),
            send_rps=self.send_rps,
        )
        if schedule.end_at_utc <= now:
            errors.append("Campaign end time has already passed. Set a new schedule before launching.")
        if errors:
            raise ValueError(" ".join(errors))
        protected = protected_destination_ids(destinations)
        target_snapshot = sorted(source_ids - protected)
        cohort_seed = secrets.token_bytes(32)
        shuffle_seed = secrets.token_bytes(32)
        cohorts = cohort_map(target_snapshot, len(variants), cohort_seed)
        status = CampaignStatus.SCHEDULED if schedule.start_at_utc > now else CampaignStatus.ACTIVE
        update = {
            "status": status.value,
            "target_snapshot": target_snapshot,
            "protected_destination_ids": sorted(protected),
            "cohort_map": {str(channel_id): cohort for channel_id, cohort in cohorts.items()},
            "cohort_seed": base64.urlsafe_b64encode(cohort_seed).decode(),
            "shuffle_seed": base64.urlsafe_b64encode(shuffle_seed).decode(),
            "activated_at": now,
            "next_cycle_number": 0,
            "next_cycle_at": schedule.start_at_utc,
            "updated_at": now,
        }
        await self.repositories.update_campaign(campaign_id, update)
        campaign.update(update)
        # Do not make the first post depend on the next scheduler tick. This
        # creates durable cycle-0 deliveries during the owner's confirmation;
        # background scheduling remains responsible for later reposts.
        if status == CampaignStatus.ACTIVE:
            await self.plan_due_cycle(campaign, now)
        return await self.repositories.get_campaign(campaign_id) or campaign

    async def editable_campaign(self, campaign_id: str) -> Document:
        """Return an editable draft for owner UI actions without duplicating status checks."""
        return await self._draft(campaign_id)

    async def launch_summary(self, campaign_id: str) -> tuple[Document, list[str], int, int, int]:
        """Validate a draft and expose exact owner-facing launch counts before confirmation."""
        campaign = await self._draft(campaign_id)
        required = ("start_at_utc", "current_end_at_utc")
        missing = [field for field in required if not campaign.get(field)]
        if missing:
            return campaign, ["Set a start and end time before launch."], 0, 0, 0
        variants = [Creative.model_validate(variant) for variant in campaign.get("variants", [])]
        destinations = [Destination.model_validate(destination) for destination in campaign.get("destinations", [])]
        schedule = Schedule(
            start_at_utc=as_utc(campaign["start_at_utc"]),
            end_at_utc=as_utc(campaign["current_end_at_utc"]),
            repost_interval_seconds=campaign.get("repost_interval_seconds"),
            repost_offsets_seconds=campaign.get("repost_offsets_seconds"),
            delete_on_repost=campaign.get("delete_on_repost", True),
            delete_on_end=campaign.get("delete_on_end", True),
            owner_timezone=campaign.get("owner_timezone", "UTC"),
        )
        sources = await self.repositories.active_channels(campaign.get("target_selector"))
        source_ids = {source["telegram_chat_id"] for source in sources}
        protected = protected_destination_ids(destinations)
        errors = validate_launch(
            variants=variants,
            destinations=destinations,
            source_ids=source_ids,
            mode=CampaignMode(campaign["mode"]),
            schedule=schedule,
            preview_sent=bool(campaign.get("preview_sent")),
            send_rps=self.send_rps,
        )
        if schedule.end_at_utc <= utcnow():
            errors.append("Campaign end time has already passed. Set a new schedule before launching.")
        return campaign, errors, len(source_ids), len(protected), len(source_ids - protected)

    async def plan_due_cycle(self, campaign: Document, now: Any) -> bool:
        """Materialize the one due cycle. Its fixed HMAC ranks make restart ordering reproducible."""
        if campaign["status"] not in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}:
            return False
        start = as_utc(campaign["start_at_utc"])
        end = as_utc(campaign["current_end_at_utc"])
        if now < start:
            return False
        if now >= end:
            await self.repositories.mark_campaign_ending(campaign["campaign_id"], "schedule_complete")
            return False
        cycle_number = int(campaign.get("next_cycle_number", 0))
        repost_offsets = campaign.get("repost_offsets_seconds")
        # After a one-off or final specific repost, keep the campaign active
        # until its configured end so cleanup can run. There is simply no
        # further cycle to plan before then.
        if (repost_offsets is not None and cycle_number > len(repost_offsets)) or (
            repost_offsets is None and not campaign.get("repost_interval_seconds") and cycle_number > 0
        ):
            return False
        expected = scheduled_cycle_time(start, cycle_number, campaign.get("repost_interval_seconds"), repost_offsets)
        if now < expected:
            return False
        if not can_create_cycle(start, end, cycle_number, campaign.get("repost_interval_seconds"), repost_offsets):
            await self.repositories.mark_campaign_ending(campaign["campaign_id"], "schedule_complete")
            return False
        seed = base64.urlsafe_b64decode(campaign["shuffle_seed"])
        variant_count = len(campaign["variants"])
        deliveries: list[Document] = []
        for channel_id in campaign["target_snapshot"]:
            cohort_index = int(campaign["cohort_map"][str(channel_id)])
            deliveries.append(
                {
                    "campaign_id": campaign["campaign_id"],
                    "cycle_number": cycle_number,
                    "channel_id": channel_id,
                    "cohort_index": cohort_index,
                    "variant_index": variant_for(campaign["mode"], cycle_number, cohort_index, variant_count),
                    "dispatch_rank": dispatch_rank(seed, cycle_number, channel_id),
                    "status": "PENDING",
                    "previous_message_id": None,
                    "sent_message_ids": [],
                    "attempts": 0,
                    "worker_id": None,
                    "lease_until": None,
                    "next_retry_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        await self.repositories.create_cycle(
            {
                "campaign_id": campaign["campaign_id"],
                "cycle_number": cycle_number,
                "scheduled_at_utc": expected,
                "created_at": now,
                "started_at": now,
                "completed_at": None,
                "status": "RUNNING",
                "target_count": len(deliveries),
            },
            deliveries,
        )
        interval = campaign.get("repost_interval_seconds")
        if repost_offsets is not None:
            next_cycle = cycle_number + 1
            next_cycle_at = (
                scheduled_cycle_time(start, next_cycle, interval, repost_offsets)
                if next_cycle <= len(repost_offsets)
                else end
            )
            await self.repositories.update_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": next_cycle,
                    "next_cycle_at": next_cycle_at,
                    "updated_at": now,
                },
            )
        elif interval:
            await self.repositories.update_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": cycle_number + 1,
                    "next_cycle_at": expected + timedelta(seconds=interval),
                    "updated_at": now,
                },
            )
        else:
            # Single-post campaigns remain active until their end-time cleanup.
            await self.repositories.update_campaign(
                campaign["campaign_id"],
                {
                    "status": CampaignStatus.ACTIVE.value,
                    "next_cycle_number": cycle_number + 1,
                    "next_cycle_at": end,
                    "updated_at": now,
                },
            )
        return True

    async def extend(self, campaign_id: str, owner_id: int, seconds: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] not in {"SCHEDULED", "ACTIVE"}:
            raise ValueError("Only scheduled or active campaigns can be extended.")
        if seconds <= 0:
            raise ValueError("Extension must be positive.")
        new_end = as_utc(campaign["current_end_at_utc"]) + timedelta(seconds=seconds)
        event = {"at": utcnow(), "owner_id": owner_id, "seconds": seconds, "new_end_at_utc": new_end}
        await self.repositories.db.campaigns.update_one(
            {"campaign_id": campaign_id},
            {"$set": {"current_end_at_utc": new_end, "updated_at": utcnow()}, "$push": {"extensions": event}},
        )
        campaign["current_end_at_utc"] = new_end
        return campaign

    async def end_early(self, campaign_id: str) -> bool:
        # Owner-requested stop always means stop and delete this campaign's
        # known live messages, even if its normal end behavior was "keep".
        await self.repositories.update_campaign(campaign_id, {"delete_on_end": True, "updated_at": utcnow()})
        return await self.repositories.mark_campaign_ending(campaign_id, "ended_early")

    async def pause(self, campaign_id: str, owner_id: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] not in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}:
            raise ValueError("Only active or scheduled campaigns can be paused.")
        now = utcnow()
        await self.repositories.pause_campaign_deliveries(campaign_id)
        await self.repositories.update_campaign(
            campaign_id,
            {"status": CampaignStatus.PAUSED.value, "paused_at": now, "paused_by": owner_id, "updated_at": now},
        )
        campaign.update({"status": CampaignStatus.PAUSED.value, "paused_at": now})
        return campaign

    async def resume(self, campaign_id: str, owner_id: int) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != CampaignStatus.PAUSED.value:
            raise ValueError("Only paused campaigns can be resumed.")
        now = utcnow()
        paused_at = as_utc(campaign["paused_at"])
        freeze_seconds = max(0, int((now - paused_at).total_seconds()))
        start = as_utc(campaign["start_at_utc"]) + timedelta(seconds=freeze_seconds)
        end = as_utc(campaign["current_end_at_utc"]) + timedelta(seconds=freeze_seconds)
        status = CampaignStatus.SCHEDULED if start > now else CampaignStatus.ACTIVE
        await self.repositories.resume_campaign_deliveries(campaign_id)
        await self.repositories.update_campaign(
            campaign_id,
            {
                "status": status.value,
                "start_at_utc": start,
                "current_end_at_utc": end,
                "paused_at": None,
                "last_resumed_by": owner_id,
                "last_freeze_seconds": freeze_seconds,
                "updated_at": now,
            },
        )
        campaign.update({"status": status.value, "start_at_utc": start, "current_end_at_utc": end, "paused_at": None})
        return campaign

    async def duplicate(self, campaign_id: str, owner_id: int) -> Document:
        original = await self.repositories.get_campaign(campaign_id)
        if not original or original["status"] != CampaignStatus.ARCHIVED.value:
            raise ValueError("Only archived campaigns can be duplicated.")
        now = utcnow()
        copied = {
            key: value
            for key, value in original.items()
            if key
            in {
                "name",
                "mode",
                "variants",
                "destinations",
                "target_selector",
                "delete_on_repost",
                "delete_on_end",
                "owner_timezone",
                "repost_interval_seconds",
                "repost_offsets_seconds",
            }
        }
        copied.update(
            {
                "campaign_id": opaque_id("cmp"),
                "name": f"{original['name']} (copy)",
                "status": CampaignStatus.DRAFT.value,
                "target_snapshot": [],
                "protected_destination_ids": [],
                "cohort_map": {},
                "created_by": owner_id,
                "created_at": now,
                "derived_from_campaign_id": original["campaign_id"],
                "version": 1,
            }
        )
        await self.repositories.create_campaign(copied)
        return copied

    async def fork_to_draft(self, campaign_id: str, owner_id: int) -> Document:
        """Create an editable successor without mutating a live campaign's history."""
        original = await self.repositories.get_campaign(campaign_id)
        if not original:
            raise ValueError("Campaign no longer exists.")
        now = utcnow()
        copied = {
            key: value
            for key, value in original.items()
            if key in {
                "mode", "variants", "destinations", "target_selector", "delete_on_repost",
                "delete_on_end", "owner_timezone", "repost_interval_seconds", "repost_offsets_seconds",
            }
        }
        copied.update({
            "campaign_id": opaque_id("cmp"), "name": f"{original['name']} (edited copy)",
            "status": CampaignStatus.DRAFT.value, "target_snapshot": [], "protected_destination_ids": [],
            "cohort_map": {}, "created_by": owner_id, "created_at": now,
            "derived_from_campaign_id": original["campaign_id"], "version": 1,
            "preview_sent": False,
        })
        await self.repositories.create_campaign(copied)
        return copied

    async def delete_draft(self, campaign_id: str) -> bool:
        return await self.repositories.delete_draft_campaign(campaign_id)

    async def _draft(self, campaign_id: str) -> Document:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != CampaignStatus.DRAFT.value:
            raise ValueError("Campaign must be an editable draft.")
        return campaign
