from __future__ import annotations

from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.client import Database
from app.utils.time import utcnow


class LeaseManager:
    def __init__(self, database: Database) -> None:
        self.collection = database.db.locks

    async def acquire_or_renew(self, lock_name: str, owner_id: str, seconds: int) -> bool:
        now = utcnow()
        lease_until = now + timedelta(seconds=seconds)
        try:
            document = await self.collection.find_one_and_update(
                {
                    "lock_name": lock_name,
                    "$or": [
                        {"owner_instance_id": owner_id},
                        {"lease_until": {"$lte": now}},
                        {"lease_until": {"$exists": False}},
                    ],
                },
                {"$set": {"owner_instance_id": owner_id, "lease_until": lease_until, "updated_at": now},
                 "$setOnInsert": {"lock_name": lock_name}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # Another healthy instance owns the unique scheduler document.
            return False
        return document is not None and document.get("owner_instance_id") == owner_id

    async def release(self, lock_name: str, owner_id: str) -> None:
        await self.collection.update_one(
            {"lock_name": lock_name, "owner_instance_id": owner_id},
            {"$set": {"lease_until": utcnow()}},
        )
