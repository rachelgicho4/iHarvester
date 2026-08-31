"""Explicit Mongo repositories: no ORM and no ephemeral delivery queue."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from app.campaigns.models import ChannelStatus, DeliveryStatus
from app.db.client import Database
from app.utils.time import utcnow

Document = dict[str, Any]


class Repositories:
    def __init__(self, database: Database) -> None:
        self.db = database.db

    async def upsert_channel(self, document: Document) -> None:
        now = utcnow()
        chat_id = document["telegram_chat_id"]
        document = {**document, "updated_at": now, "last_admin_update_at": now}
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": document, "$setOnInsert": {"registered_at": now}},
            upsert=True,
        )

    async def set_channel_status(self, chat_id: int, status: ChannelStatus, **details: Any) -> None:
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"status": status.value, "updated_at": utcnow(), **details}},
        )

    async def get_channel(self, chat_id: int) -> Document | None:
        return await self.db.channels.find_one({"telegram_chat_id": chat_id})

    async def active_channels(self, selector: Document | None = None) -> list[Document]:
        query = self._active_channel_query(selector)
        return await self.db.channels.find(query).to_list(None)

    def _active_channel_query(self, selector: Document | None = None) -> Document:
        query: Document = {"status": ChannelStatus.ACTIVE.value, "permissions.can_post_messages": True}
        if selector:
            if tags := selector.get("tags_any"):
                query["tags"] = {"$in": tags}
            if include := selector.get("include_ids"):
                query["telegram_chat_id"] = {"$in": include}
            if exclude := selector.get("exclude_ids"):
                query.setdefault("telegram_chat_id", {})["$nin"] = exclude
            if minimum_members := selector.get("minimum_members"):
                query["member_count"] = {"$gte": minimum_members}
        return query

    async def active_channel_count(self, selector: Document | None = None, *, exclude_ids: set[int] | None = None) -> int:
        query = self._active_channel_query(selector)
        if exclude_ids:
            channel_query = query.setdefault("telegram_chat_id", {})
            if not isinstance(channel_query, dict):
                raise ValueError("target selector cannot combine a fixed include list with destination protection")
            existing_exclusions = list(channel_query.get("$nin", []))
            channel_query["$nin"] = [*existing_exclusions, *exclude_ids]
        return await self.db.channels.count_documents(query)

    async def channel_count(self, active_only: bool = False) -> int:
        query = {"status": ChannelStatus.ACTIVE.value, "permissions.can_post_messages": True} if active_only else {}
        return await self.db.channels.count_documents(query)

    async def channel_status_counts(self) -> Document:
        cursor = await self.db.channels.aggregate(
            [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {row["_id"]: row["count"] for row in rows if row.get("_id")}

    async def list_channels(self, status: str | None = None, *, skip: int = 0, limit: int = 10) -> list[Document]:
        query: Document = {"status": status} if status else {}
        return await self.db.channels.find(query).sort("title", ASCENDING).skip(skip).limit(limit).to_list(limit)

    async def set_channel_tags(self, chat_id: int, tags: list[str]) -> None:
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"tags": tags, "updated_at": utcnow()}},
        )

    async def set_channel_manual_enabled(self, chat_id: int, enabled: bool) -> None:
        status = ChannelStatus.ACTIVE.value if enabled else ChannelStatus.INACTIVE_MANUAL.value
        await self.db.channels.update_one(
            {"telegram_chat_id": chat_id},
            {"$set": {"status": status, "updated_at": utcnow()}},
        )

    async def create_campaign(self, campaign: Document) -> None:
        await self.db.campaigns.insert_one(campaign)

    async def get_campaign(self, campaign_id: str) -> Document | None:
        return await self.db.campaigns.find_one({"campaign_id": campaign_id})

    async def update_campaign(self, campaign_id: str, update: Document) -> bool:
        result = await self.db.campaigns.update_one({"campaign_id": campaign_id}, {"$set": update})
        return result.modified_count == 1

    async def due_campaigns(self, now: Any) -> list[Document]:
        return await self.db.campaigns.find(
            {
                "$or": [
                    {"status": "SCHEDULED", "start_at_utc": {"$lte": now}},
                    {"status": "ACTIVE"},
                    {"status": "ENDING"},
                ]
            }
        ).to_list(None)

    async def create_cycle(self, cycle: Document, deliveries: list[Document]) -> bool:
        """Idempotently materialize durable cycle work, including recovery after a partial insert."""
        try:
            await self.db.campaign_cycles.insert_one(cycle)
            created = True
        except DuplicateKeyError:
            created = False
        if deliveries:
            operations = [
                UpdateOne(
                    {"campaign_id": delivery["campaign_id"], "cycle_number": delivery["cycle_number"], "channel_id": delivery["channel_id"]},
                    {"$setOnInsert": delivery},
                    upsert=True,
                )
                for delivery in deliveries
            ]
            for offset in range(0, len(operations), 500):
                await self.db.deliveries.bulk_write(operations[offset : offset + 500], ordered=False)
        return created

    async def cycle_exists(self, campaign_id: str, cycle_number: int) -> bool:
        return await self.db.campaign_cycles.count_documents({"campaign_id": campaign_id, "cycle_number": cycle_number}, limit=1) > 0

    async def campaigns_with_due_cleanup(self) -> list[Document]:
        return await self.db.campaigns.find({"status": "ENDING"}).to_list(None)

    async def claim_delivery(self, worker_id: str, lease_seconds: int) -> Document | None:
        now = utcnow()
        return await self.db.deliveries.find_one_and_update(
            {
                "$or": [
                    {"status": DeliveryStatus.PENDING.value},
                    {"status": DeliveryStatus.RETRY_WAIT.value, "next_retry_at": {"$lte": now}},
                    {"status": DeliveryStatus.PROCESSING.value, "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "status": DeliveryStatus.PROCESSING.value,
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("dispatch_rank", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def complete_delivery(self, delivery_id: Any, status: DeliveryStatus, **details: Any) -> None:
        await self.db.deliveries.update_one(
            {"_id": delivery_id},
            {"$set": {"status": status.value, "lease_until": None, "updated_at": utcnow(), **details}},
        )

    async def retry_delivery(self, delivery_id: Any, retry_after_seconds: float, **details: Any) -> None:
        now = utcnow()
        await self.db.deliveries.update_one(
            {"_id": delivery_id},
            {
                "$set": {
                    "status": DeliveryStatus.RETRY_WAIT.value,
                    "lease_until": None,
                    "next_retry_at": now + timedelta(seconds=retry_after_seconds),
                    "updated_at": now,
                    **details,
                }
            },
        )

    async def live_state(self, campaign_id: str, channel_id: int) -> Document | None:
        return await self.db.campaign_channel_state.find_one({"campaign_id": campaign_id, "channel_id": channel_id})

    async def save_live_state(self, campaign_id: str, channel_id: int, cycle_number: int, variant_index: int, message_ids: list[int]) -> None:
        await self.db.campaign_channel_state.update_one(
            {"campaign_id": campaign_id, "channel_id": channel_id},
            {
                "$set": {
                    "current_message_ids": message_ids,
                    "current_cycle_number": cycle_number,
                    "current_variant_index": variant_index,
                    "updated_at": utcnow(),
                }
            },
            upsert=True,
        )

    async def clear_live_state(self, campaign_id: str, channel_id: int) -> None:
        await self.db.campaign_channel_state.delete_one({"campaign_id": campaign_id, "channel_id": channel_id})

    async def live_states(self, campaign_id: str) -> list[Document]:
        return await self.db.campaign_channel_state.find({"campaign_id": campaign_id}).to_list(None)

    async def mark_campaign_archived(self, campaign_id: str, reason: str) -> None:
        await self.db.campaigns.update_one(
            {"campaign_id": campaign_id},
            {"$set": {"status": "ARCHIVED", "archived_at": utcnow(), "end_reason": reason}},
        )

    async def mark_campaign_ending(self, campaign_id: str, reason: str) -> bool:
        result = await self.db.campaigns.update_one(
            {"campaign_id": campaign_id, "status": {"$in": ["SCHEDULED", "ACTIVE"]}},
            {"$set": {"status": "ENDING", "end_reason": reason, "ending_at": utcnow()}},
        )
        return result.modified_count == 1

    async def cancel_pending_campaign_deliveries(self, campaign_id: str) -> None:
        await self.db.deliveries.update_many(
            {"campaign_id": campaign_id, "status": {"$in": ["PENDING", "RETRY_WAIT"]}},
            {"$set": {"status": DeliveryStatus.CANCELLED.value, "updated_at": utcnow()}},
        )

    async def materialize_cleanup_deliveries(self, campaign_id: str) -> int:
        states = await self.live_states(campaign_id)
        now = utcnow()
        operations = [
            UpdateOne(
                {"campaign_id": campaign_id, "cycle_number": -1, "channel_id": state["channel_id"]},
                {
                    "$setOnInsert": {
                        "campaign_id": campaign_id,
                        "cycle_number": -1,
                        "channel_id": state["channel_id"],
                        "operation": "CLEANUP",
                        "message_ids": state["current_message_ids"],
                        "dispatch_rank": state["channel_id"] & ((1 << 63) - 1),
                        "status": DeliveryStatus.PENDING.value,
                        "attempts": 0,
                        "worker_id": None,
                        "lease_until": None,
                        "next_retry_at": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
            for state in states
        ]
        for offset in range(0, len(operations), 500):
            await self.db.deliveries.bulk_write(operations[offset : offset + 500], ordered=False)
        return len(states)

    async def cleanup_is_complete(self, campaign_id: str) -> bool:
        outstanding = await self.db.deliveries.count_documents(
            {
                "campaign_id": campaign_id,
                "operation": "CLEANUP",
                "status": {"$in": ["PENDING", "PROCESSING", "RETRY_WAIT"]},
            }
        )
        return outstanding == 0

    async def delivery_summary(self, campaign_id: str, cycle_number: int) -> Document:
        cursor = await self.db.deliveries.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "cycle_number": cycle_number}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {item["_id"]: item["count"] for item in rows}

    async def failed_deliveries(self, campaign_id: str, cycle_number: int, *, limit: int = 20) -> list[Document]:
        return (
            await self.db.deliveries.find(
                {
                    "campaign_id": campaign_id,
                    "cycle_number": cycle_number,
                    "status": {"$in": [DeliveryStatus.FAILED_PERMANENT.value, DeliveryStatus.UNKNOWN_SEND_STATE.value]},
                }
            )
            .sort("updated_at", -1)
            .limit(limit)
            .to_list(limit)
        )

    async def retry_failed_deliveries(self, campaign_id: str, cycle_number: int) -> int:
        """An owner-initiated retry never touches ambiguous send states."""
        result = await self.db.deliveries.update_many(
            {
                "campaign_id": campaign_id,
                "cycle_number": cycle_number,
                "status": DeliveryStatus.FAILED_PERMANENT.value,
            },
            {
                "$set": {
                    "status": DeliveryStatus.PENDING.value,
                    "next_retry_at": None,
                    "lease_until": None,
                    "worker_id": None,
                    "updated_at": utcnow(),
                    "owner_retry_requested_at": utcnow(),
                }
            },
        )
        return result.modified_count

    async def export_collections(self, full: bool) -> dict[str, list[Document]]:
        names = ["channels", "campaigns", "settings"]
        if full:
            names.extend(["campaign_cycles", "deliveries", "campaign_channel_state", "join_events"])
        return {name: await self.db[name].find({}).to_list(None) for name in names}

    async def set_owner_session(self, owner_id: int, state: Document, *, ttl_minutes: int = 30) -> None:
        now = utcnow()
        await self.db.owner_sessions.update_one(
            {"owner_id": owner_id},
            {
                "$set": {
                    "owner_id": owner_id,
                    "state": state,
                    "updated_at": now,
                    "expires_at": now + timedelta(minutes=ttl_minutes),
                }
            },
            upsert=True,
        )

    async def owner_session(self, owner_id: int) -> Document | None:
        document = await self.db.owner_sessions.find_one({"owner_id": owner_id, "expires_at": {"$gt": utcnow()}})
        return document.get("state") if document else None

    async def clear_owner_session(self, owner_id: int) -> None:
        await self.db.owner_sessions.delete_one({"owner_id": owner_id})

    async def list_campaigns(self, limit: int = 20) -> list[Document]:
        return await self.db.campaigns.find({}).sort("updated_at", -1).limit(limit).to_list(limit)

    async def campaign_status_counts(self) -> Document:
        cursor = await self.db.campaigns.aggregate(
            [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(None)
        return {row["_id"]: row["count"] for row in rows if row.get("_id")}

    async def get_setting(self, key: str, default: Any = None) -> Any:
        document = await self.db.settings.find_one({"key": key})
        return document.get("value", default) if document else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value, "updated_at": utcnow()}},
            upsert=True,
        )

    async def delete_draft_campaign(self, campaign_id: str) -> bool:
        result = await self.db.campaigns.delete_one({"campaign_id": campaign_id, "status": "DRAFT"})
        return result.deleted_count == 1

    async def save_pending_restore(self, restore_id: str, owner_id: int, backup: Document) -> None:
        await self.db.pending_restores.update_one(
            {"restore_id": restore_id},
            {
                "$set": {
                    "restore_id": restore_id,
                    "owner_id": owner_id,
                    "backup": backup,
                    "expires_at": utcnow() + timedelta(hours=1),
                }
            },
            upsert=True,
        )

    async def get_pending_restore(self, restore_id: str, owner_id: int) -> Document | None:
        return await self.db.pending_restores.find_one({"restore_id": restore_id, "owner_id": owner_id})

    async def delete_pending_restore(self, restore_id: str, owner_id: int) -> None:
        await self.db.pending_restores.delete_one({"restore_id": restore_id, "owner_id": owner_id})

    async def register_update(self, update_id: int) -> bool:
        try:
            await self.db.processed_updates.insert_one({"update_id": update_id, "received_at": utcnow()})
            return True
        except DuplicateKeyError:
            return False

    async def restore_collection(self, name: str, documents: list[Document]) -> tuple[int, int]:
        restored = skipped = 0
        unique_key = {"channels": "telegram_chat_id", "campaigns": "campaign_id", "settings": "key"}.get(name)
        for document in documents:
            document.pop("_id", None)
            if not unique_key or unique_key not in document:
                skipped += 1
                continue
            result = await self.db[name].update_one({unique_key: document[unique_key]}, {"$set": document}, upsert=True)
            restored += 1 if result.acknowledged else 0
        return restored, skipped
