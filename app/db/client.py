from __future__ import annotations

from typing import Any

import certifi
from pymongo import AsyncMongoClient


class Database:
    def __init__(self, uri: str, database_name: str) -> None:
        self.client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=5_000,
            tlsCAFile=certifi.where(),
            # MongoDB stores UTC instants without a timezone marker.  Return
            # them as UTC-aware datetimes so scheduler comparisons are valid.
            tz_aware=True,
        )
        self.db = self.client[database_name]

    async def ping(self) -> None:
        await self.db.command("ping")

    async def close(self) -> None:
        await self.client.close()
