from __future__ import annotations

from typing import Any

from pymongo import AsyncMongoClient


class Database:
    def __init__(self, uri: str, database_name: str) -> None:
        self.client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(uri, serverSelectionTimeoutMS=5_000)
        self.db = self.client[database_name]

    async def ping(self) -> None:
        await self.db.command("ping")

    async def close(self) -> None:
        await self.client.close()

