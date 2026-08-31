"""Optional Telegram-native invite-link join attribution; no redirect or subscriber profile is stored."""

from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated

from app.db.repositories import Repositories
from app.utils.ids import opaque_id
from app.utils.time import utcnow


class JoinEventHandlers:
    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories
        self.router = Router(name="join_events")
        self.router.chat_member.register(self.chat_member)

    async def chat_member(self, update: ChatMemberUpdated) -> None:
        if update.new_chat_member.status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}:
            return
        invite = update.invite_link.invite_link if update.invite_link else None
        if not invite:
            return
        campaign = await self.repositories.db.campaigns.find_one({
            "status": "ACTIVE", "destinations.campaign_invite_link": invite,
        })
        if not campaign:
            return
        destination = next(
            (item for item in campaign.get("destinations", []) if item.get("campaign_invite_link") == invite), {}
        )
        await self.repositories.db.join_events.update_one(
            {"campaign_id": campaign["campaign_id"], "destination_id": update.chat.id,
             "invite_link": invite, "joined_at_utc": update.date},
            {"$setOnInsert": {
                "join_event_id": opaque_id("join"), "campaign_id": campaign["campaign_id"],
                "variant_id": None, "destination_id": update.chat.id, "invite_link": invite,
                "joined_at_utc": update.date or utcnow(), "destination_name": destination.get("display_name"),
            }}, upsert=True,
        )
