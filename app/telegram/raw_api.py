from __future__ import annotations

from typing import Any

import httpx


class RawTelegramAPI:
    """Small Bot API escape hatch for fields/types not yet surfaced by aiogram."""

    def __init__(self, token: str, timeout_seconds: float) -> None:
        self.client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=timeout_seconds)

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self.client.post(method, json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API rejected request"))
        return data["result"]

    async def close(self) -> None:
        await self.client.aclose()

