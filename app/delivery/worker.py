from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.campaigns.models import ChannelStatus, DeliveryStatus
from app.db.repositories import Repositories
from app.delivery.error_classification import ErrorKind, classify_telegram_error
from app.delivery.rate_limit import AsyncTokenBucket
from app.telegram.sender import TelegramSender
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(
        self, *, worker_id: str, repositories: Repositories, sender: TelegramSender,
        send_limiter: AsyncTokenBucket, mutation_limiter: AsyncTokenBucket,
        delivery_lease_seconds: int, max_transient_attempts: int,
    ) -> None:
        self.worker_id = worker_id
        self.repositories = repositories
        self.sender = sender
        self.send_limiter = send_limiter
        self.mutation_limiter = mutation_limiter
        self.delivery_lease_seconds = delivery_lease_seconds
        self.max_transient_attempts = max_transient_attempts

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            delivery = await self.repositories.claim_delivery(self.worker_id, self.delivery_lease_seconds)
            if not delivery:
                await asyncio.sleep(0.25)
                continue
            try:
                await self.process(delivery)
            except Exception:
                logger.exception("Unhandled delivery worker error", extra={"delivery_id": str(delivery.get("_id"))})
                await self.repositories.retry_delivery(delivery["_id"], 5, error_category="WORKER_EXCEPTION")

    async def process(self, delivery: dict[str, Any]) -> None:
        if delivery.get("operation") == "CLEANUP":
            await self._cleanup(delivery)
            return
        campaign = await self.repositories.get_campaign(delivery["campaign_id"])
        if not campaign or campaign["status"] != "ACTIVE":
            await self.repositories.complete_delivery(
                delivery["_id"], DeliveryStatus.PAUSED if campaign and campaign["status"] == "PAUSED" else DeliveryStatus.CANCELLED
            )
            return
        channel = await self.repositories.get_channel(delivery["channel_id"])
        if not channel or channel.get("status") != ChannelStatus.ACTIVE.value or not channel.get("permissions", {}).get("can_post_messages"):
            await self.repositories.complete_delivery(
                delivery["_id"], DeliveryStatus.FAILED_PERMANENT, error_category="CHANNEL_NOT_POSTABLE"
            )
            return
        if delivery["cycle_number"] > 0:
            state = await self.repositories.live_state(delivery["campaign_id"], delivery["channel_id"])
            if state and state.get("current_message_ids"):
                try:
                    await self.mutation_limiter.acquire()
                    await self.sender.delete_messages(delivery["channel_id"], state["current_message_ids"])
                    await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
                except Exception as error:
                    decision = classify_telegram_error(error, operation="delete")
                    if decision.kind == ErrorKind.CLEAN_ABSENT:
                        await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
                    elif decision.kind == ErrorKind.PERMANENT:
                        await self._permanent(delivery, channel, "DELETE_FAILED_NO_REPLACEMENT")
                        return
                    else:
                        await self._retry_or_fail(delivery, decision)
                        return
        # A campaign may enter ENDING while a worker was waiting for a limiter token.
        campaign = await self.repositories.get_campaign(delivery["campaign_id"])
        if not campaign or campaign["status"] != "ACTIVE":
            await self.repositories.complete_delivery(
                delivery["_id"], DeliveryStatus.PAUSED if campaign and campaign["status"] == "PAUSED" else DeliveryStatus.CANCELLED
            )
            return
        try:
            await self.send_limiter.acquire()
            await self.mutation_limiter.acquire()
            result = await self.sender.send_variant(delivery["channel_id"], campaign["variants"][delivery["variant_index"]])
        except Exception as error:
            decision = classify_telegram_error(error, operation="send")
            if decision.kind == ErrorKind.PERMANENT:
                await self._permanent(delivery, channel, decision.category)
            elif decision.kind == ErrorKind.AMBIGUOUS:
                await self.repositories.complete_delivery(
                    delivery["_id"], DeliveryStatus.UNKNOWN_SEND_STATE, error_category=decision.category
                )
            else:
                await self._retry_or_fail(delivery, decision)
            return
        # If the end transition won the race, delete the just-sent post rather than reintroduce campaign clutter.
        campaign = await self.repositories.get_campaign(delivery["campaign_id"])
        if not campaign or campaign["status"] != "ACTIVE":
            try:
                await self.mutation_limiter.acquire()
                await self.sender.delete_messages(delivery["channel_id"], result.message_ids)
            finally:
                await self.repositories.complete_delivery(
                    delivery["_id"], DeliveryStatus.PAUSED if campaign and campaign["status"] == "PAUSED" else DeliveryStatus.CANCELLED
                )
            return
        await self.repositories.save_live_state(
            delivery["campaign_id"], delivery["channel_id"], delivery["cycle_number"],
            delivery["variant_index"], result.message_ids,
        )
        await self.repositories.complete_delivery(
            delivery["_id"], DeliveryStatus.SENT, sent_message_ids=result.message_ids, sent_at=utcnow()
        )
        await self.repositories.set_channel_status(
            delivery["channel_id"], ChannelStatus.ACTIVE, last_successful_post_at=utcnow(), last_error_code=None
        )

    async def _cleanup(self, delivery: dict[str, Any]) -> None:
        try:
            await self.mutation_limiter.acquire()
            await self.sender.delete_messages(delivery["channel_id"], delivery["message_ids"])
        except Exception as error:
            decision = classify_telegram_error(error, operation="delete")
            if decision.kind == ErrorKind.CLEAN_ABSENT:
                pass
            elif decision.kind == ErrorKind.PERMANENT:
                await self.repositories.complete_delivery(
                    delivery["_id"], DeliveryStatus.CLEANUP_FAILED, error_category=decision.category
                )
                return
            else:
                await self._retry_or_fail(delivery, decision, cleanup=True)
                return
        await self.repositories.clear_live_state(delivery["campaign_id"], delivery["channel_id"])
        await self.repositories.complete_delivery(delivery["_id"], DeliveryStatus.CLEANED)

    async def _retry_or_fail(self, delivery: dict[str, Any], decision: Any, cleanup: bool = False) -> None:
        if delivery["attempts"] >= self.max_transient_attempts:
            status = DeliveryStatus.CLEANUP_FAILED if cleanup else DeliveryStatus.FAILED_PERMANENT
            await self.repositories.complete_delivery(delivery["_id"], status, error_category="RETRY_EXHAUSTED")
            return
        await self.repositories.retry_delivery(
            delivery["_id"], decision.retry_after_seconds or 5, error_category=decision.category
        )

    async def _permanent(self, delivery: dict[str, Any], channel: dict[str, Any], category: str) -> None:
        await self.repositories.complete_delivery(delivery["_id"], DeliveryStatus.FAILED_PERMANENT, error_category=category)
        status = ChannelStatus.UNAVAILABLE if category == "ACCESS_OR_PERMISSION" else ChannelStatus.NEEDS_ATTENTION
        await self.repositories.set_channel_status(
            channel["telegram_chat_id"], status, last_error_code=category, last_error_at=utcnow()
        )
