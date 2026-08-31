"""Telegram-native owner control plane with short-lived, guided interaction state."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pydantic import ValidationError

from app.backups.export import make_backup
from app.backups.restore import parse_backup, restore_backup
from app.campaigns.models import Button, CampaignMode, CampaignStatus, Creative, Destination
from app.campaigns.service import CampaignService
from app.db.repositories import Document, Repositories
from app.telegram.formatting import capture_creative
from app.telegram.handlers_admin_updates import refresh_channel, register_forwarded_channel
from app.telegram.keyboards import home_keyboard
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id
from app.utils.time import as_utc

logger = logging.getLogger(__name__)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _navigation(back_callback: str = "home:back") -> list[list[InlineKeyboardButton]]:
    return [[
        InlineKeyboardButton(text="Back", callback_data=back_callback),
        InlineKeyboardButton(text="Home", callback_data="home:back"),
    ]]


_PERIOD_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 24 * 60, "day": 24 * 60, "days": 24 * 60,
    "mo": 30 * 24 * 60, "month": 30 * 24 * 60, "months": 30 * 24 * 60,
}
_INTERVAL_PRESET_MINUTES = (5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440)
_SAFE_REPOST_MAX_MINUTES = 47 * 60


def _period_label(minutes: int) -> str:
    if minutes % (30 * 24 * 60) == 0:
        value = minutes // (30 * 24 * 60)
        return f"{value} month{'s' if value != 1 else ''}"
    if minutes % (24 * 60) == 0:
        value = minutes // (24 * 60)
        return f"{value} day{'s' if value != 1 else ''}"
    if minutes % 60 == 0:
        value = minutes // 60
        return f"{value} hour{'s' if value != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def parse_period_minutes(raw: str, *, field: str) -> int:
    """Parse short owner-entered periods; a month is deliberately 30 days."""
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]+)\s*", raw)
    if not match or match.group(2).lower() not in _PERIOD_UNITS:
        raise ValueError(f"send {field} as a whole number with m, h, d, or mo (for example `45m`, `2h`, `3d`, or `1mo`)")
    minutes = int(match.group(1)) * _PERIOD_UNITS[match.group(2).lower()]
    if minutes < 1:
        raise ValueError(f"{field} must be at least 1 minute")
    return minutes


def parse_repost_offsets_minutes(raw: str, *, duration_minutes: int) -> list[int]:
    values = [parse_period_minutes(item, field="repost time") for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("send one or more comma-separated repost times")
    if len(values) > 20:
        raise ValueError("choose at most 20 specific repost times")
    values = sorted(set(values))
    if values[-1] >= duration_minutes:
        raise ValueError("each repost time must be before the campaign end")
    return values


def parse_repost_gaps_minutes(raw: str, *, duration_minutes: int) -> list[int]:
    """Convert 1-20 owner-entered gaps between posts into elapsed offsets."""
    gaps = [parse_period_minutes(item, field="repost gap") for item in raw.split(",") if item.strip()]
    if not gaps:
        raise ValueError("send one or more comma-separated repost gaps")
    if len(gaps) > 20:
        raise ValueError("choose at most 20 repost gaps")
    offsets: list[int] = []
    elapsed = 0
    for gap in gaps:
        elapsed += gap
        if elapsed >= duration_minutes:
            raise ValueError("the combined repost gaps must end before the campaign end")
        offsets.append(elapsed)
    return offsets


def _valid_repost_minutes(duration_minutes: int) -> tuple[int, ...]:
    return tuple(
        interval for interval in _INTERVAL_PRESET_MINUTES
        if interval < duration_minutes
        and duration_minutes % interval == 0
        and (duration_minutes <= _SAFE_REPOST_MAX_MINUTES or interval <= _SAFE_REPOST_MAX_MINUTES)
    )


def _interval_keyboard(
    campaign_id: str,
    duration_minutes: int,
    *,
    prefix: str,
    back_callback: str,
    preferred_minutes: int | None = None,
) -> InlineKeyboardMarkup:
    """Offer only repost periods that end exactly on a campaign boundary."""
    rows: list[list[InlineKeyboardButton]] = []
    valid = list(_valid_repost_minutes(duration_minutes))
    if duration_minutes <= _SAFE_REPOST_MAX_MINUTES:
        rows.append([InlineKeyboardButton(text="Post once only", callback_data=f"{prefix}:{campaign_id}:0")])
    if preferred_minutes and preferred_minutes in valid:
        rows.append([
            InlineKeyboardButton(
                text=f"Use my preference: every {_period_label(preferred_minutes)}",
                callback_data=f"{prefix}:{campaign_id}:{preferred_minutes}",
            )
        ])
        valid.remove(preferred_minutes)
    for offset in range(0, len(valid), 2):
        rows.append([
            InlineKeyboardButton(text=f"Every {_period_label(interval)}", callback_data=f"{prefix}:{campaign_id}:{interval}")
            for interval in valid[offset : offset + 2]
        ])
    rows.append([InlineKeyboardButton(text="Custom interval", callback_data=f"{prefix}:{campaign_id}:custom")])
    rows.append([
        InlineKeyboardButton(text="Specific times after launch", callback_data=f"times:{prefix}:{campaign_id}"),
        InlineKeyboardButton(text="Set custom repost gaps", callback_data=f"gaps:{prefix}:{campaign_id}"),
    ])
    rows.extend(_navigation(back_callback))
    return _markup(rows)


def campaign_keyboard(campaign_id: str, status: str) -> InlineKeyboardMarkup:
    if status == CampaignStatus.ARCHIVED.value:
        return _markup(
            [
                [InlineKeyboardButton(text="Duplicate / Run Again", callback_data=f"c:{campaign_id}:duplicate")],
                [
                    InlineKeyboardButton(text="Back to campaigns", callback_data="home:campaigns"),
                    InlineKeyboardButton(text="Home", callback_data="home:back"),
                ],
            ]
        )
    if status in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value, CampaignStatus.ENDING.value}:
        rows = [
            [
                InlineKeyboardButton(text="View progress", callback_data=f"c:{campaign_id}:progress"),
                InlineKeyboardButton(text="View failures", callback_data=f"c:{campaign_id}:failures"),
            ],
            [InlineKeyboardButton(text="Retry failed", callback_data=f"c:{campaign_id}:retry")],
            [
                InlineKeyboardButton(text="+6 hours", callback_data=f"c:{campaign_id}:extend6"),
                InlineKeyboardButton(text="+1 day", callback_data=f"c:{campaign_id}:extend24"),
            ],
        ]
        if status != CampaignStatus.ENDING.value:
            rows.append([
                InlineKeyboardButton(text="End campaign", callback_data=f"c:{campaign_id}:end"),
                InlineKeyboardButton(text="Edit as new draft", callback_data=f"c:{campaign_id}:refactor"),
            ])
        rows.append([
            InlineKeyboardButton(text="Back to campaigns", callback_data="home:campaigns"),
            InlineKeyboardButton(text="Home", callback_data="home:back"),
        ])
        return _markup(rows)
    return _markup(
        [
            [
                InlineKeyboardButton(text="Add content", callback_data=f"c:{campaign_id}:add"),
                InlineKeyboardButton(text="Edit creatives", callback_data=f"c:{campaign_id}:variants"),
            ],
            [
                InlineKeyboardButton(text="CTA buttons", callback_data=f"c:{campaign_id}:buttons"),
                InlineKeyboardButton(text="Destinations", callback_data=f"c:{campaign_id}:destination"),
            ],
            [
                InlineKeyboardButton(text="Targets", callback_data=f"c:{campaign_id}:targets"),
                InlineKeyboardButton(text="Plan for later", callback_data=f"c:{campaign_id}:schedule"),
            ],
            [
                InlineKeyboardButton(text="Mode", callback_data=f"c:{campaign_id}:mode"),
                InlineKeyboardButton(text="Preview", callback_data=f"c:{campaign_id}:preview"),
            ],
            [
                InlineKeyboardButton(text="Test send", callback_data=f"c:{campaign_id}:test"),
                InlineKeyboardButton(text="Send campaign", callback_data=f"c:{campaign_id}:send"),
            ],
            [
                InlineKeyboardButton(text="Delete draft", callback_data=f"c:{campaign_id}:delete"),
            ],
            [InlineKeyboardButton(text="Back to campaigns", callback_data="home:campaigns"), InlineKeyboardButton(text="Home", callback_data="home:back")],
        ]
    )


def content_type_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="Text", callback_data=f"ct:{campaign_id}:TEXT"),
                InlineKeyboardButton(text="Photo", callback_data=f"ct:{campaign_id}:PHOTO"),
            ],
            [
                InlineKeyboardButton(text="Photo + caption", callback_data=f"ct:{campaign_id}:PHOTO_TEXT"),
                InlineKeyboardButton(text="Video", callback_data=f"ct:{campaign_id}:VIDEO"),
            ],
            [
                InlineKeyboardButton(text="Video + caption", callback_data=f"ct:{campaign_id}:VIDEO_TEXT"),
                InlineKeyboardButton(text="More media", callback_data=f"ct:{campaign_id}:MEDIA"),
            ],
            [InlineKeyboardButton(text="Album", callback_data=f"ct:{campaign_id}:ALBUM")],
            [InlineKeyboardButton(text="Forward ready post", callback_data=f"ct:{campaign_id}:FORWARD")],
            * _navigation(f"c:{campaign_id}:open"),
        ]
    )


def mode_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="Standard", callback_data=f"mode:{campaign_id}:STANDARD"),
                InlineKeyboardButton(text="Rotate", callback_data=f"mode:{campaign_id}:ROTATE"),
            ],
            [InlineKeyboardButton(text="Mix + Rotate", callback_data=f"mode:{campaign_id}:MIX_ROTATE")],
            * _navigation(f"c:{campaign_id}:open"),
        ]
    )


def schedule_interval_keyboard(campaign_id: str, duration_minutes: int) -> InlineKeyboardMarkup:
    return _interval_keyboard(campaign_id, duration_minutes, prefix="sint", back_callback=f"c:{campaign_id}:open")


def target_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="All active channels", callback_data=f"target:{campaign_id}:all")],
            [
                InlineKeyboardButton(text="Match tags", callback_data=f"target:{campaign_id}:tags"),
                InlineKeyboardButton(text="Minimum audience", callback_data=f"target:{campaign_id}:members"),
            ],
            [
                InlineKeyboardButton(text="Manually include IDs", callback_data=f"target:{campaign_id}:include"),
                InlineKeyboardButton(text="Exclude IDs", callback_data=f"target:{campaign_id}:exclude"),
            ],
            * _navigation(f"c:{campaign_id}:open"),
        ]
    )


def quick_duration_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return _markup([
        [InlineKeyboardButton(text="15 minutes", callback_data=f"dur:{campaign_id}:15"),
         InlineKeyboardButton(text="1 hour", callback_data=f"dur:{campaign_id}:60")],
        [InlineKeyboardButton(text="6 hours", callback_data=f"dur:{campaign_id}:360"),
         InlineKeyboardButton(text="1 day", callback_data=f"dur:{campaign_id}:1440")],
        [InlineKeyboardButton(text="3 days", callback_data=f"dur:{campaign_id}:4320"),
         InlineKeyboardButton(text="7 days", callback_data=f"dur:{campaign_id}:10080")],
        [InlineKeyboardButton(text="30 days", callback_data=f"dur:{campaign_id}:43200")],
        [InlineKeyboardButton(text="Custom duration", callback_data=f"dur:{campaign_id}:custom")],
        * _navigation(f"c:{campaign_id}:open"),
    ])


def quick_interval_keyboard(campaign_id: str, duration_minutes: int, default_hours: int) -> InlineKeyboardMarkup:
    return _interval_keyboard(
        campaign_id,
        duration_minutes,
        prefix="qmin",
        back_callback=f"c:{campaign_id}:send",
        preferred_minutes=default_hours * 60 if default_hours else None,
    )


class OwnerHandlers:
    def __init__(
        self,
        *,
        owner_ids: frozenset[int],
        repositories: Repositories,
        campaigns: CampaignService,
        sender: TelegramSender,
    ) -> None:
        self.owner_ids = owner_ids
        self.repositories = repositories
        self.campaigns = campaigns
        self.sender = sender
        self.router = Router(name="owner")
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.backup, Command("backup"))
        self.router.message.register(self.restore, Command("restore"))
        self.router.callback_query.register(self.callback, F.data)
        self.router.message.register(self.message)

    def _allowed(self, user_id: int | None, chat_type: str) -> bool:
        return user_id in self.owner_ids and chat_type == "private"

    @staticmethod
    def _date(value: Any) -> str:
        if not value:
            return "Not set"
        return as_utc(value).strftime("%d %b %Y, %H:%M UTC")

    async def _retire(self, query: CallbackQuery) -> None:
        """Disable the just-used control so old UI messages cannot drive stale workflows."""
        if not query.message:
            return
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    async def _show_home(self, message: Message) -> None:
        channels = await self.repositories.channel_status_counts()
        campaigns = await self.repositories.campaign_status_counts()
        await message.answer(
            "iHarvester control room\n\n"
            f"Active campaigns: {campaigns.get('ACTIVE', 0)}\n"
            f"Scheduled campaigns: {campaigns.get('SCHEDULED', 0)}\n"
            f"Active source channels: {channels.get('ACTIVE', 0)}\n"
            f"Need attention: {channels.get('NEEDS_ATTENTION', 0)}",
            reply_markup=home_keyboard(),
        )

    async def _show_settings(self, message: Message) -> None:
        timezone = await self.repositories.get_setting("owner_timezone", "UTC")
        interval = await self.repositories.get_setting("quick_interval_hours", 6)
        interval_text = "Post once" if not interval else f"Every {interval} hour{'s' if interval != 1 else ''}"
        await message.answer(
            "Settings\n\n"
            f"Display timezone: {timezone}\n"
            f"Quick-send default interval: {interval_text}\n\n"
            "These preferences are used when you schedule a new campaign with Send campaign.",
            reply_markup=_markup([
                [InlineKeyboardButton(text="UTC", callback_data="set:timezone:UTC"),
                 InlineKeyboardButton(text="Africa/Nairobi", callback_data="set:timezone:Africa/Nairobi")],
                [InlineKeyboardButton(text="Default: post once", callback_data="set:interval:0"),
                 InlineKeyboardButton(text="Default: every 6h", callback_data="set:interval:6")],
                [InlineKeyboardButton(text="Default: every 24h", callback_data="set:interval:24")],
                * _navigation(),
            ]),
        )

    async def _show_campaign(self, message: Message, campaign: Document) -> None:
        variants = campaign.get("variants", [])
        button_count = sum(len(item.get("buttons", [])) for item in variants)
        destinations = campaign.get("destinations", [])
        selector = campaign.get("target_selector") or {}
        targets = "All active channels" if not selector else "Filtered selection"
        snapshot = campaign.get("target_snapshot", [])
        protected_ids = {item["telegram_chat_id"] for item in destinations if item.get("telegram_chat_id") is not None}
        planned_count = await self.repositories.active_channel_count(selector, exclude_ids=protected_ids)
        schedule = (
            f"{self._date(campaign.get('start_at_utc'))} to {self._date(campaign.get('current_end_at_utc'))}"
            if campaign.get("start_at_utc")
            else "Not scheduled"
        )
        target_text = f"{len(snapshot)} frozen source channels" if snapshot else f"{targets}: {planned_count} planned"
        await message.answer(
            f"Campaign: {campaign['name']}\n"
            f"Status: {campaign['status']}  |  Mode: {campaign.get('mode', 'STANDARD')}\n\n"
            f"Creatives: {len(variants)}  |  CTA buttons: {button_count}\n"
            f"Destinations: {len(destinations)}\n"
            f"Targets: {target_text}\n"
            f"Schedule: {schedule}",
            reply_markup=campaign_keyboard(campaign["campaign_id"], campaign["status"]),
        )

    async def _show_network(self, message: Message) -> None:
        counts = await self.repositories.channel_status_counts()
        text = (
            "Network registry\n\n"
            f"Active: {counts.get('ACTIVE', 0)}\n"
            f"Needs attention: {counts.get('NEEDS_ATTENTION', 0)}\n"
            f"Unavailable: {counts.get('UNAVAILABLE', 0)}\n"
            f"Paused manually: {counts.get('INACTIVE_MANUAL', 0)}\n"
            f"Total discovered: {sum(counts.values())}\n\n"
            "Add me as a channel admin to register automatically, or forward a channel post here to repair/register it."
        )
        await message.answer(
            text,
            reply_markup=_markup(
                [
                    [
                        InlineKeyboardButton(text=f"Active ({counts.get('ACTIVE', 0)})", callback_data="net:list:ACTIVE:0"),
                        InlineKeyboardButton(text=f"Attention ({counts.get('NEEDS_ATTENTION', 0)})", callback_data="net:list:NEEDS_ATTENTION:0"),
                    ],
                    [
                        InlineKeyboardButton(text=f"Unavailable ({counts.get('UNAVAILABLE', 0)})", callback_data="net:list:UNAVAILABLE:0"),
                        InlineKeyboardButton(text=f"Paused ({counts.get('INACTIVE_MANUAL', 0)})", callback_data="net:list:INACTIVE_MANUAL:0"),
                    ],
                    [InlineKeyboardButton(text="Forward post to register", callback_data="net:forward")],
                    [InlineKeyboardButton(text="Back", callback_data="home:back")],
                ]
            ),
        )

    async def _show_network_list(self, message: Message, status: str, page: int) -> None:
        page_size = 8
        rows = await self.repositories.list_channels(status, skip=page * page_size, limit=page_size)
        if not rows:
            await message.answer(
                "No channels in this group.",
                reply_markup=_markup(
                    [
                        [InlineKeyboardButton(text="Back to Network", callback_data="net:home")],
                    ]
                ),
            )
            return
        lines = [f"{status.replace('_', ' ').title()} channels (page {page + 1})", ""]
        controls: list[list[InlineKeyboardButton]] = []
        for index, channel in enumerate(rows, start=1):
            username = f"@{channel['username']}" if channel.get("username") else "private"
            lines.append(f"{index}. {channel.get('title', channel['telegram_chat_id'])}\n   {username} | {channel.get('member_count', '?')} members")
            controls.append([InlineKeyboardButton(text=f"Open channel {index}", callback_data=f"chan:{channel['telegram_chat_id']}:view")])
        nav: list[InlineKeyboardButton] = []
        if page:
            nav.append(InlineKeyboardButton(text="Previous", callback_data=f"net:list:{status}:{page - 1}"))
        if len(rows) == page_size:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"net:list:{status}:{page + 1}"))
        if nav:
            controls.append(nav)
        controls.append([InlineKeyboardButton(text="Back to Network", callback_data="net:home")])
        await message.answer("\n".join(lines), reply_markup=_markup(controls))

    async def _show_channel(self, message: Message, chat_id: int) -> None:
        channel = await self.repositories.get_channel(chat_id)
        if not channel:
            await message.answer("That channel is no longer in the registry.")
            return
        permissions = channel.get("permissions", {})
        text = (
            f"{channel.get('title', chat_id)}\nStatus: {channel.get('status')}\nUsername: @{channel['username']}"
            if channel.get("username")
            else f"{channel.get('title', chat_id)}\nStatus: {channel.get('status')}\nUsername: private"
        )
        text += (
            f"\nMembers: {channel.get('member_count', 'unknown')}\n"
            f"Can post: {'yes' if permissions.get('can_post_messages') else 'no'}\n"
            f"Tags: {', '.join(channel.get('tags', [])) or 'none'}"
        )
        enabled = channel.get("status") != "INACTIVE_MANUAL"
        await message.answer(
            text,
            reply_markup=_markup(
                [
                    [
                        InlineKeyboardButton(text="Refresh access", callback_data=f"chan:{chat_id}:refresh"),
                        InlineKeyboardButton(text="Tag", callback_data=f"chan:{chat_id}:tag"),
                    ],
                    [
                        InlineKeyboardButton(
                            text="Pause source" if enabled else "Enable source", callback_data=f"chan:{chat_id}:{'disable' if enabled else 'enable'}"
                        )
                    ],
                    [InlineKeyboardButton(text="Back to Network", callback_data="net:home")],
                ]
            ),
        )

    async def _show_button_editor(self, message: Message, campaign: Document, variant_index: int) -> None:
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        if not variants:
            await message.answer("Add content first; CTA buttons attach to a saved creative.", reply_markup=content_type_keyboard(campaign["campaign_id"]))
            return
        creative = variants[variant_index]
        if creative.buttons:
            by_row: dict[int, list[str]] = {}
            for button in sorted(creative.buttons, key=lambda item: (item.row, item.position)):
                by_row.setdefault(button.row, []).append(button.text)
            canvas = "\n".join(f"Row {row + 1}: {' | '.join(labels)}" for row, labels in by_row.items())
        else:
            canvas = "No CTA buttons yet."
        cid = campaign["campaign_id"]
        rows: list[list[InlineKeyboardButton]] = []
        if not creative.buttons:
            rows.append([InlineKeyboardButton(text="+ Add first CTA", callback_data=f"btn:{cid}:{variant_index}:first")])
        else:
            rows.append(
                [
                    InlineKeyboardButton(text="+ Add beside last", callback_data=f"btn:{cid}:{variant_index}:right"),
                    InlineKeyboardButton(text="+ Add new row", callback_data=f"btn:{cid}:{variant_index}:below"),
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="Auto layout", callback_data=f"layout:{cid}:{variant_index}:AUTO"),
                    InlineKeyboardButton(text="1 per row", callback_data=f"layout:{cid}:{variant_index}:1"),
                ],
                [
                    InlineKeyboardButton(text="2 per row", callback_data=f"layout:{cid}:{variant_index}:2"),
                    InlineKeyboardButton(text="3 per row", callback_data=f"layout:{cid}:{variant_index}:3"),
                ],
            ]
        )
        for button in creative.buttons:
            rows.append([InlineKeyboardButton(text=f"Remove: {button.text}", callback_data=f"rm:{cid}:{variant_index}:{button.id}")])
        rows.append([InlineKeyboardButton(text="Send real preview", callback_data=f"preview:{cid}:{variant_index}")])
        rows.append([InlineKeyboardButton(text="Back to campaign", callback_data=f"c:{cid}:open")])
        await message.answer(
            f"CTA button editor - variant {variant_index + 1}\n\n{canvas}\n\n"
            "Use beside-last for a horizontal button and new-row for a vertical button. Names are saved unchanged.",
            reply_markup=_markup(rows),
        )

    async def start(self, message: Message) -> None:
        if self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            await self._show_home(message)

    async def backup(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        payload = await make_backup(self.repositories)
        await message.answer_document(
            BufferedInputFile(payload, filename="iharvester-core-backup.json.gz"), caption="Core backup: channels, campaign definitions, and settings."
        )

    async def restore(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        await self.repositories.set_owner_session(message.from_user.id, {"action": "await_restore_file"})
        await message.answer("Attach an iHarvester .json.gz backup. I will validate it before restoring.")

    async def callback(self, query: CallbackQuery, bot: Bot) -> None:
        if not query.message or not self._allowed(query.from_user.id, query.message.chat.type):
            await query.answer("Owner-only private control.", show_alert=True)
            return
        data = query.data or ""
        await self._retire(query)
        try:
            if data.startswith("home:"):
                await self._home(query, data.split(":", 1)[1])
            elif data.startswith("c:"):
                _, campaign_id, action = data.split(":", 2)
                await self._campaign_action(query, campaign_id, action)
            elif data.startswith(("mode:", "m:")):
                _, campaign_id, mode = data.split(":", 2)
                await self._set_mode(query, campaign_id, mode)
            elif data.startswith("ct:"):
                _, campaign_id, kind = data.split(":", 2)
                prompts = {
                    "TEXT": "Send the formatted text post.",
                    "PHOTO": "Send the photo. A caption is optional.",
                    "PHOTO_TEXT": "Send the photo with its caption.",
                    "VIDEO": "Send the video. A caption is optional.",
                    "VIDEO_TEXT": "Send the video with its caption.",
                    "MEDIA": "Send a supported photo, video, GIF, document, audio, voice, sticker, or video note.",
                    "ALBUM": "Send 2-10 photos or videos. Tap Finish album after the last item.",
                    "FORWARD": "Forward the finished post. Its formatting and media will be captured for replay.",
                }
                if kind == "ALBUM":
                    await self.repositories.set_owner_session(
                        query.from_user.id,
                        {"action": "await_album", "campaign_id": campaign_id, "media": [], "caption": None, "caption_entities": []},
                    )
                else:
                    await self.repositories.set_owner_session(query.from_user.id, {"action": "await_creative", "campaign_id": campaign_id, "kind": kind})
                await query.message.answer(prompts[kind])
            elif data.startswith("edit:"):
                _, campaign_id, index = data.split(":", 2)
                campaign = await self.campaigns.editable_campaign(campaign_id)
                await self._show_button_editor(query.message, campaign, int(index))
            elif data.startswith("btn:"):
                _, campaign_id, index, placement = data.split(":", 3)
                await self.repositories.set_owner_session(
                    query.from_user.id, {"action": "await_button_label", "campaign_id": campaign_id, "variant_index": int(index), "placement": placement}
                )
                await query.message.answer("Send the CTA button label exactly as you want viewers to see it.")
            elif data.startswith("album:"):
                _, campaign_id, action = data.split(":", 2)
                if action != "finish":
                    raise ValueError("unknown album action")
                await self._finish_album(query, campaign_id)
            elif data.startswith("layout:"):
                _, campaign_id, index, layout = data.split(":", 3)
                await self._set_button_layout(query, campaign_id, int(index), layout)
            elif data.startswith("rm:"):
                _, campaign_id, index, button_id = data.split(":", 3)
                await self._remove_button(query, campaign_id, int(index), button_id)
            elif data.startswith("preview:"):
                _, campaign_id, index = data.split(":", 2)
                await self._preview_variant(query, campaign_id, int(index))
            elif data.startswith("net:"):
                await self._network_action(query, data)
            elif data.startswith("chan:"):
                _, chat_id, action = data.split(":", 2)
                await self._channel_action(query, bot, int(chat_id), action)
            elif data.startswith("target:"):
                _, campaign_id, action = data.split(":", 2)
                await self._target_action(query, campaign_id, action)
            elif data.startswith("dest:"):
                _, campaign_id, action = data.split(":", 2)
                await self._destination_action(query, campaign_id, action)
            elif data.startswith("times:"):
                _, flow, campaign_id = data.split(":", 2)
                await self._specific_repost_times(query, campaign_id, flow)
            elif data.startswith("gaps:"):
                _, flow, campaign_id = data.split(":", 2)
                await self._custom_repost_gaps(query, campaign_id, flow)
            elif data.startswith("sint:"):
                _, campaign_id, value = data.split(":", 2)
                await self._schedule_interval(query, campaign_id, value)
            elif data.startswith("sch:"):
                # Compatibility with buttons emitted before intervals used
                # minute-based callbacks. Those values were whole hours.
                _, campaign_id, value = data.split(":", 2)
                await self._schedule_interval(query, campaign_id, value if value == "custom" else str(int(value) * 60))
            elif data.startswith("dur:"):
                _, campaign_id, value = data.split(":", 2)
                if value == "custom":
                    await self._custom_duration(query, campaign_id)
                else:
                    await self._quick_duration(query, campaign_id, int(value))
            elif data.startswith("qmin:"):
                _, campaign_id, value = data.split(":", 2)
                await self._quick_interval(query, campaign_id, value)
            elif data.startswith("qint:"):
                # Compatibility with old quick-send buttons, which encoded hours.
                _, campaign_id, value = data.split(":", 2)
                await self._quick_interval(query, campaign_id, value if value == "custom" else str(int(value) * 60))
            elif data.startswith("set:"):
                _, action, value = data.split(":", 2)
                await self._settings_action(query, action, value)
            elif data.startswith("confirm:"):
                _, campaign_id, action = data.split(":", 2)
                await self._confirm(query, campaign_id, action)
            elif data.startswith("reg:"):
                _, chat_id, action = data.split(":", 2)
                await self._channel_action(query, bot, int(chat_id), action)
            elif data.startswith("restore:"):
                _, restore_id, action = data.split(":", 2)
                await self._restore_confirm(query, restore_id, action)
            else:
                await query.message.answer("That control has expired. Open the current screen again with /start.")
        except (ValueError, KeyError, ValidationError) as error:
            await query.message.answer(f"Could not complete that action: {error}")
        except Exception:
            logger.exception("Owner callback failed", extra={"callback_data": data, "owner_id": query.from_user.id})
            await query.message.answer("Could not complete that action due to an internal error. Nothing was launched; please retry after the issue is fixed.")
        await query.answer()

    async def _home(self, query: CallbackQuery, action: str) -> None:
        # Moving between top-level areas is an explicit cancellation point for
        # a pending text-entry step.  Otherwise an old message sent after
        # navigating home could accidentally be consumed by that old step.
        if action != "create":
            await self.repositories.clear_owner_session(query.from_user.id)
        if action == "create":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_campaign_name"})
            await query.message.answer("What should this campaign be called?", reply_markup=_markup(_navigation()))
        elif action == "network":
            await self._show_network(query.message)
        elif action == "campaigns":
            rows = await self.repositories.list_campaigns()
            if not rows:
                await query.message.answer("No campaigns yet. Create your first one when ready.", reply_markup=home_keyboard())
                return
            buttons: list[list[InlineKeyboardButton]] = []
            for row in rows:
                campaign_id = row["campaign_id"]
                status = row["status"]
                buttons.append([InlineKeyboardButton(text=f"{row['name']} ({status})", callback_data=f"c:{campaign_id}:open")])
                if status == CampaignStatus.DRAFT.value:
                    buttons.append([
                        InlineKeyboardButton(text="Send", callback_data=f"c:{campaign_id}:send"),
                        InlineKeyboardButton(text="Edit", callback_data=f"c:{campaign_id}:edit"),
                        InlineKeyboardButton(text="Delete", callback_data=f"c:{campaign_id}:delete"),
                    ])
                elif status in {CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value}:
                    buttons.append([
                        InlineKeyboardButton(text="Progress", callback_data=f"c:{campaign_id}:progress"),
                        InlineKeyboardButton(text="End", callback_data=f"c:{campaign_id}:end"),
                    ])
                elif status == CampaignStatus.ARCHIVED.value:
                    buttons.append([InlineKeyboardButton(text="Run again", callback_data=f"c:{campaign_id}:duplicate")])
            buttons.append([InlineKeyboardButton(text="Back", callback_data="home:back")])
            await query.message.answer("Campaigns", reply_markup=_markup(buttons))
        elif action == "back":
            await self._show_home(query.message)
        elif action == "backups":
            await query.message.answer("Use /backup to export a core backup, or /restore then attach a backup file.")
        elif action == "settings":
            await self._show_settings(query.message)

    async def _campaign_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("campaign no longer exists")
        if action in {"open", "edit"}:
            await self.repositories.clear_owner_session(query.from_user.id)
            await self._show_campaign(query.message, campaign)
        elif action == "add":
            await query.message.answer("What kind of post are you creating?", reply_markup=content_type_keyboard(campaign_id))
        elif action == "variants":
            variants = campaign.get("variants", [])
            if not variants:
                await query.message.answer("No creatives yet.", reply_markup=content_type_keyboard(campaign_id))
            else:
                rows = [
                    [InlineKeyboardButton(text=f"Variant {index + 1}: {item['kind']}", callback_data=f"edit:{campaign_id}:{index}")]
                    for index, item in enumerate(variants)
                ]
                rows.append([InlineKeyboardButton(text="Add another creative", callback_data=f"c:{campaign_id}:add")])
                rows.append([InlineKeyboardButton(text="Back", callback_data=f"c:{campaign_id}:open")])
                await query.message.answer("Choose a creative to edit its CTA buttons.", reply_markup=_markup(rows))
        elif action in {"buttons", "button"}:
            await self._show_button_editor(query.message, campaign, max(0, len(campaign.get("variants", [])) - 1))
        elif action == "destination":
            await self._show_destinations(query.message, campaign)
        elif action == "targets":
            await query.message.answer(
                "Choose the source channels for this campaign. Promoted destinations remain excluded automatically.", reply_markup=target_keyboard(campaign_id)
            )
        elif action == "mode":
            await query.message.answer("Choose how variants rotate across source channels.", reply_markup=mode_keyboard(campaign_id))
        elif action == "schedule":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_schedule_start", "campaign_id": campaign_id})
            await query.message.answer(
                "For a future start, send `YYYY-MM-DD HH:MM` in UTC, for example `2026-09-02 09:30`.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "send":
            if not campaign.get("variants"):
                raise ValueError("Add campaign content before sending.")
            await query.message.answer(
                "Send campaign\n\nWhen should the campaign end? It will start as soon as you confirm launch.",
                reply_markup=quick_duration_keyboard(campaign_id),
            )
        elif action == "preview":
            await self._preview_variant(query, campaign_id, 0)
        elif action == "test":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_test_channel", "campaign_id": campaign_id})
            await query.message.answer(
                "Send one owner-controlled test channel ID. The first creative will be posted there.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "launch":
            await self._show_launch_confirmation(query.message, campaign_id)
        elif action == "extend6":
            await self.campaigns.extend(campaign_id, query.from_user.id, 6 * 3600)
            await query.message.answer("Campaign extended by 6 hours.")
        elif action == "extend24":
            await self.campaigns.extend(campaign_id, query.from_user.id, 24 * 3600)
            await query.message.answer("Campaign extended by 1 day.")
        elif action == "end":
            await query.message.answer(
                "End this campaign? It stops future cycles, deletes known live campaign posts, then archives the results.",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Yes, end and clean up", callback_data=f"confirm:{campaign_id}:end"),
                            InlineKeyboardButton(text="Keep running", callback_data=f"c:{campaign_id}:open"),
                        ]
                    ]
                ),
            )
        elif action == "duplicate":
            copied = await self.campaigns.duplicate(campaign_id, query.from_user.id)
            await query.message.answer("Created a new editable draft from the archived campaign.")
            await self._show_campaign(query.message, copied)
        elif action == "refactor":
            copied = await self.campaigns.fork_to_draft(campaign_id, query.from_user.id)
            await query.message.answer(
                "The running campaign remains unchanged. I created an editable draft copy so you can safely replace content, buttons, targets, or schedule.",
            )
            await self._show_campaign(query.message, copied)
        elif action == "delete":
            if campaign["status"] != CampaignStatus.DRAFT.value:
                raise ValueError("only drafts can be deleted; end a live campaign instead")
            await query.message.answer(
                "Delete this draft? Its saved creative, CTA buttons, destinations, and schedule will be removed. This cannot be undone.",
                reply_markup=_markup([
                    [InlineKeyboardButton(text="Delete draft", callback_data=f"confirm:{campaign_id}:delete"),
                     InlineKeyboardButton(text="Keep draft", callback_data=f"c:{campaign_id}:open")],
                ]),
            )
        elif action == "progress":
            cycle = max(0, int(campaign.get("next_cycle_number", 1)) - 1)
            summary = await self.repositories.delivery_summary(campaign_id, cycle)
            if not summary:
                text = "No deliveries have been created yet."
            else:
                text = "\n".join(f"{status.replace('_', ' ').title()}: {count}" for status, count in sorted(summary.items()))
            await query.message.answer(f"Cycle {cycle} progress\n\n{text}")
        elif action == "failures":
            cycle = max(0, int(campaign.get("next_cycle_number", 1)) - 1)
            failures = await self.repositories.failed_deliveries(campaign_id, cycle)
            if not failures:
                await query.message.answer("No failed or unknown deliveries in the current cycle.")
                return
            lines = [f"Current cycle failures ({len(failures)} shown)", ""]
            for item in failures:
                channel = await self.repositories.get_channel(item["channel_id"])
                title = channel.get("title") if channel else str(item["channel_id"])
                lines.append(f"- {title}: {item['status'].replace('_', ' ').title()} ({item.get('error_category', 'no category')})")
            await query.message.answer("\n".join(lines))
        elif action == "retry":
            cycle = max(0, int(campaign.get("next_cycle_number", 1)) - 1)
            count = await self.repositories.retry_failed_deliveries(campaign_id, cycle)
            noun = "delivery" if count == 1 else "deliveries"
            await query.message.answer(f"Queued {count} failed {noun} for one owner-requested retry. Unknown send results were not retried.")

    async def _set_mode(self, query: CallbackQuery, campaign_id: str, mode: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        await self.repositories.update_campaign(campaign_id, {"mode": CampaignMode(mode).value, "preview_sent": False, "updated_at": datetime.now(UTC)})
        campaign["mode"] = mode
        await query.message.answer(f"Mode set to {mode}. Review/preview again before launch.")
        await self._show_campaign(query.message, campaign)

    async def _set_button_layout(self, query: CallbackQuery, campaign_id: str, index: int, layout: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        variants[index].button_layout = layout
        await self.repositories.update_campaign(
            campaign_id, {"variants": [item.model_dump(mode="json") for item in variants], "preview_sent": False, "updated_at": datetime.now(UTC)}
        )
        await query.message.answer(f"Button layout set to {layout}.")
        campaign["variants"] = [item.model_dump(mode="json") for item in variants]
        await self._show_button_editor(query.message, campaign, index)

    async def _remove_button(self, query: CallbackQuery, campaign_id: str, index: int, button_id: str) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        before = len(variants[index].buttons)
        variants[index].buttons = [button for button in variants[index].buttons if button.id != button_id]
        if len(variants[index].buttons) == before:
            raise ValueError("button no longer exists")
        await self.repositories.update_campaign(
            campaign_id, {"variants": [item.model_dump(mode="json") for item in variants], "preview_sent": False, "updated_at": datetime.now(UTC)}
        )
        campaign["variants"] = [item.model_dump(mode="json") for item in variants]
        await query.message.answer("Button removed.")
        await self._show_button_editor(query.message, campaign, index)

    async def _preview_variant(self, query: CallbackQuery, campaign_id: str, index: int) -> None:
        await self._send_preview(query.message, query.from_user.id, campaign_id, index)

    async def _send_preview(self, message: Message, owner_id: int, campaign_id: str, index: int) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or index >= len(campaign.get("variants", [])):
            raise ValueError("add a creative first")
        await self.sender.send_variant(owner_id, campaign["variants"][index])
        await self.repositories.update_campaign(campaign_id, {"preview_sent": True, "updated_at": datetime.now(UTC)})
        await message.answer("That is a real Telegram preview using the saved content and CTA keyboard.")

    async def _show_launch_confirmation(self, message: Message, campaign_id: str) -> None:
        campaign, errors, source_count, protected_count, eligible_count = await self.campaigns.launch_summary(campaign_id)
        if errors:
            await message.answer(
                "Launch checklist\n\n" + "\n".join(f"- {error}" for error in errors),
                reply_markup=campaign_keyboard(campaign_id, campaign["status"]),
            )
            return
        interval = campaign.get("repost_interval_seconds")
        offsets = campaign.get("repost_offsets_seconds")
        repost_text = (
            "single post"
            if not interval and not offsets
            else f"every {_period_label(interval // 60)}"
            if interval
            else "at " + ", ".join(_period_label(offset // 60) for offset in offsets)
        )
        cleanup_text = "enabled" if campaign.get("delete_on_end", True) else "not available (the final repost gap exceeds 47 hours)"
        await message.answer(
            "Ready to launch\n\n"
            f"This campaign will target {eligible_count} source channels.\n"
            f"{protected_count} promoted destination channels are excluded automatically.\n"
            f"Start: {self._date(campaign['start_at_utc'])}\n"
            f"End: {self._date(campaign['current_end_at_utc'])}\n"
            f"Repost: {repost_text}\n"
            f"Final cleanup: {cleanup_text}\n"
            f"Mode: {campaign['mode']} ({len(campaign['variants'])} creative{'s' if len(campaign['variants']) != 1 else ''})\n"
            f"Active sources before destination protection: {source_count}",
            reply_markup=_markup(
                [
                    [
                        InlineKeyboardButton(text="Launch now", callback_data=f"confirm:{campaign_id}:launch"),
                        InlineKeyboardButton(text="Cancel", callback_data=f"c:{campaign_id}:open"),
                    ],
                ]
            ),
        )

    async def _show_destinations(self, message: Message, campaign: Document) -> None:
        destinations = campaign.get("destinations", [])
        lines = ["Promoted destinations", ""]
        if destinations:
            for index, destination in enumerate(destinations, start=1):
                identity = destination.get("raw_url") or destination.get("username") or destination.get("telegram_chat_id")
                protected = "protected" if destination.get("telegram_chat_id") else "link only"
                lines.append(f"{index}. {destination['display_name']} - {identity} ({protected})")
        else:
            lines.append("None yet. Add each promoted channel/link here.")
        cid = campaign["campaign_id"]
        await message.answer(
            "\n".join(lines),
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="+ Add destination", callback_data=f"dest:{cid}:add")],
                    [InlineKeyboardButton(text="Back", callback_data=f"c:{cid}:open")],
                ]
            ),
        )

    async def _destination_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if action == "add":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_destination_name", "campaign_id": campaign_id})
            await query.message.answer(
                "Send the destination's display name.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "skip":
            session = await self.repositories.owner_session(query.from_user.id)
            if not session or session.get("action") != "await_destination_id" or session.get("campaign_id") != campaign_id:
                raise ValueError("that destination entry expired")
            await self._save_destination(campaign_id, session["name"], session["url"], None)
            await self.repositories.clear_owner_session(query.from_user.id)
            campaign = await self.repositories.get_campaign(campaign_id)
            await query.message.answer("Destination saved. A link-only destination cannot be automatically excluded until its channel is registered.")
            await self._show_destinations(query.message, campaign)

    async def _target_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if action == "all":
            await self.repositories.update_campaign(campaign_id, {"target_selector": {}, "preview_sent": False, "updated_at": datetime.now(UTC)})
            await query.message.answer("Targets set to all active source channels. Destination channels will still be excluded.")
        else:
            prompts = {
                "tags": "Send comma-separated tags to include, for example `movies, kenya`.",
                "members": "Send the minimum member count, for example `1000`.",
                "include": "Send comma-separated numeric channel IDs to include.",
                "exclude": "Send comma-separated numeric channel IDs to exclude.",
            }
            await self.repositories.set_owner_session(query.from_user.id, {"action": f"await_target_{action}", "campaign_id": campaign_id})
            await query.message.answer(prompts[action], reply_markup=_markup(_navigation(f"c:{campaign_id}:open")))

    async def _schedule_interval(self, query: CallbackQuery, campaign_id: str, value: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_schedule_interval" or session.get("campaign_id") != campaign_id:
            raise ValueError("schedule entry expired; start again")
        if value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {**session, "action": "await_schedule_interval_custom"})
            await query.message.answer(
                "Send the repost interval, for example `5m`, `2h`, or `1d`. It must be shorter than the campaign and divide its duration exactly.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
            return
        interval_minutes = int(value)
        duration_minutes = self._duration_minutes(session["start"], session["end"])
        self._validate_repost_interval(duration_minutes, interval_minutes)
        await self._save_schedule(campaign_id, session["start"], session["end"], interval_minutes)
        await self.repositories.clear_owner_session(query.from_user.id)
        await query.message.answer("Schedule saved.")
        await self._show_campaign(query.message, await self.repositories.get_campaign(campaign_id))

    async def _specific_repost_times(self, query: CallbackQuery, campaign_id: str, flow: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        expected_action = {"qmin": "await_quick_interval", "sint": "await_schedule_interval"}.get(flow)
        if not expected_action or not session or session.get("action") != expected_action or session.get("campaign_id") != campaign_id:
            raise ValueError("repost-time entry expired; choose Send campaign or Plan for later again")
        duration_minutes = (
            int(session["duration_minutes"])
            if flow == "qmin"
            else self._duration_minutes(session["start"], session["end"])
        )
        action = "await_quick_repost_times" if flow == "qmin" else "await_schedule_repost_times"
        await self.repositories.set_owner_session(query.from_user.id, {**session, "action": action})
        await query.message.answer(
            "Enter 1-20 exact elapsed repost times after launch, separated by commas. For example `1d, 4d, 6d` means "
            "an initial post now, then reposts at day 1, day 4, and day 6. Times can be uneven and must be before the "
            f"campaign ends in {_period_label(duration_minutes)}.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send" if flow == "qmin" else f"c:{campaign_id}:open")),
        )

    async def _custom_repost_gaps(self, query: CallbackQuery, campaign_id: str, flow: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        expected_action = {"qmin": "await_quick_interval", "sint": "await_schedule_interval"}.get(flow)
        if not expected_action or not session or session.get("action") != expected_action or session.get("campaign_id") != campaign_id:
            raise ValueError("repost-gap entry expired; choose Send campaign or Plan for later again")
        duration_minutes = (
            int(session["duration_minutes"])
            if flow == "qmin"
            else self._duration_minutes(session["start"], session["end"])
        )
        action = "await_quick_repost_gaps" if flow == "qmin" else "await_schedule_repost_gaps"
        await self.repositories.set_owner_session(query.from_user.id, {**session, "action": action})
        await query.message.answer(
            "Enter 1-20 custom gaps between posts, separated by commas. For example `1d, 3d, 2d` means "
            "post now, then repost after 1 day, then 3 days later, then 2 days later. "
            f"Their combined length must fit inside {_period_label(duration_minutes)}.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send" if flow == "qmin" else f"c:{campaign_id}:open")),
        )

    async def _quick_duration(self, query: CallbackQuery, campaign_id: str, minutes: int) -> None:
        await self._begin_quick_interval(query.from_user.id, query.message, campaign_id, minutes)

    async def _custom_duration(self, query: CallbackQuery, campaign_id: str) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        await self.repositories.set_owner_session(query.from_user.id, {"action": "await_quick_duration", "campaign_id": campaign_id})
        await query.message.answer(
            "Send the campaign duration, for example `45m`, `2h`, `3d`, or `1mo`. One month is 30 days.",
            reply_markup=_markup(_navigation(f"c:{campaign_id}:send")),
        )

    async def _begin_quick_interval(self, owner_id: int, message: Message, campaign_id: str, minutes: int) -> None:
        await self.campaigns.editable_campaign(campaign_id)
        if minutes < 1:
            raise ValueError("campaign duration must be at least 1 minute")
        default_hours = int(await self.repositories.get_setting("quick_interval_hours", 6))
        await self.repositories.set_owner_session(
            owner_id,
            {"action": "await_quick_interval", "campaign_id": campaign_id, "duration_minutes": minutes},
        )
        await message.answer(
            f"Campaign duration: {_period_label(minutes)}. Choose a repost interval. The offered periods divide this duration exactly.",
            reply_markup=quick_interval_keyboard(campaign_id, minutes, default_hours),
        )

    async def _quick_interval(self, query: CallbackQuery, campaign_id: str, value: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_quick_interval" or session.get("campaign_id") != campaign_id:
            raise ValueError("send setup expired; open the campaign and choose Send campaign again")
        if value == "custom":
            await self.repositories.set_owner_session(query.from_user.id, {**session, "action": "await_quick_interval_custom"})
            await query.message.answer(
                "Send the repost interval, for example `5m`, `2h`, or `1d`. It must be shorter than the campaign and divide its duration exactly.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:send")),
            )
            return
        await self._complete_quick_send(query.from_user.id, query.message, campaign_id, int(session["duration_minutes"]), int(value))

    async def _complete_quick_send(
        self,
        owner_id: int,
        message: Message,
        campaign_id: str,
        duration_minutes: int,
        interval_minutes: int,
        repost_offsets_minutes: list[int] | None = None,
    ) -> None:
        if repost_offsets_minutes is None:
            self._validate_repost_interval(duration_minutes, interval_minutes)
        start = datetime.now(UTC)
        end = start + timedelta(minutes=duration_minutes)
        await self._save_schedule(campaign_id, start, end, interval_minutes, repost_offsets_minutes)
        await self.repositories.clear_owner_session(owner_id)
        await self._send_preview(message, owner_id, campaign_id, 0)
        await self._show_launch_confirmation(message, campaign_id)

    @staticmethod
    def _duration_minutes(start: datetime, end: datetime) -> int:
        minutes = int((as_utc(end) - as_utc(start)).total_seconds() // 60)
        if minutes < 1:
            raise ValueError("campaign duration must be at least 1 minute")
        return minutes

    @staticmethod
    def _validate_repost_interval(duration_minutes: int, interval_minutes: int) -> None:
        if interval_minutes == 0:
            if duration_minutes > _SAFE_REPOST_MAX_MINUTES:
                raise ValueError("campaigns over 47 hours need a repost interval for reliable cleanup")
            return
        if interval_minutes < 1 or interval_minutes >= duration_minutes:
            raise ValueError("repost interval must be at least 1 minute and shorter than the campaign")
        if duration_minutes % interval_minutes:
            raise ValueError("repost interval must divide the campaign duration exactly")
        if duration_minutes > _SAFE_REPOST_MAX_MINUTES and interval_minutes > _SAFE_REPOST_MAX_MINUTES:
            raise ValueError("campaigns over 47 hours need a repost interval of 47 hours or less")

    @staticmethod
    def _specific_times_support_final_cleanup(duration_minutes: int, offsets_minutes: list[int]) -> bool:
        points = [0, *offsets_minutes, duration_minutes]
        return all(right - left <= _SAFE_REPOST_MAX_MINUTES for left, right in zip(points, points[1:], strict=False))

    async def _settings_action(self, query: CallbackQuery, action: str, value: str) -> None:
        if action == "timezone" and value in {"UTC", "Africa/Nairobi"}:
            await self.repositories.set_setting("owner_timezone", value)
        elif action == "interval" and value in {"0", "6", "24"}:
            await self.repositories.set_setting("quick_interval_hours", int(value))
        else:
            raise ValueError("invalid setting")
        await query.message.answer("Setting saved.")
        await self._show_settings(query.message)

    async def _network_action(self, query: CallbackQuery, data: str) -> None:
        parts = data.split(":")
        if parts[1] in {"home", "back"}:
            await self._show_network(query.message) if parts[1] == "home" else await self._show_home(query.message)
        elif parts[1] == "forward":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_channel_forward"})
            await query.message.answer("Forward any post from the channel. I will register or refresh that channel and then show its details.")
        elif parts[1] == "list" and len(parts) == 4:
            await self._show_network_list(query.message, parts[2], int(parts[3]))

    async def _channel_action(self, query: CallbackQuery, bot: Bot, chat_id: int, action: str) -> None:
        if action in {"refresh", "Refresh"}:
            success = await refresh_channel(bot, self.repositories, chat_id)
            await query.message.answer("Channel refreshed." if success else "Channel needs attention; check bot admin/posting rights.")
            await self._show_channel(query.message, chat_id)
        elif action == "tag":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_channel_tag", "channel_id": chat_id})
            await query.message.answer("Send one or more comma-separated tags.")
        elif action == "disable":
            await self.repositories.set_channel_manual_enabled(chat_id, False)
            await query.message.answer("Source channel paused. It will not be selected by new campaigns.")
        elif action == "enable":
            await self.repositories.set_channel_manual_enabled(chat_id, True)
            await query.message.answer("Source channel enabled again.")
        elif action == "view":
            await self._show_channel(query.message, chat_id)

    async def _confirm(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        if action == "launch":
            activated = await self.campaigns.activate(campaign_id)
            await query.message.answer(
                f"{activated['name']} is now {activated['status']}. It targets {len(activated['target_snapshot'])} source channels. "
                "The first deliveries are being queued now and normally appear within a few seconds."
            )
            await self._show_campaign(query.message, activated)
        elif action == "end":
            changed = await self.campaigns.end_early(campaign_id)
            await query.message.answer(
                "Campaign ending: future cycles are stopped and live posts will be cleaned up." if changed else "Campaign was already ending or archived."
            )
        elif action == "delete":
            deleted = await self.campaigns.delete_draft(campaign_id)
            await query.message.answer("Draft deleted." if deleted else "That draft has already been removed.")
            await self._show_home(query.message)

    async def _restore_confirm(self, query: CallbackQuery, restore_id: str, action: str) -> None:
        if action == "cancel":
            await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
            await query.message.answer("Restore cancelled.")
            return
        pending = await self.repositories.get_pending_restore(restore_id, query.from_user.id)
        if not pending:
            raise ValueError("restore request expired")
        result = await restore_backup(self.repositories, pending["backup"])
        await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
        await query.message.answer(f"Restore complete: {result}")

    async def message(self, message: Message, bot: Bot) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        session = await self.repositories.owner_session(message.from_user.id)
        if not session:
            await register_forwarded_channel(message, bot, self.repositories)
            return
        try:
            await self._handle_session_message(message, bot, session)
        except (ValueError, ValidationError) as error:
            await message.answer(f"That did not work: {error}. Please try again, or use /start to begin a new action.")

    async def _handle_session_message(self, message: Message, bot: Bot, session: Document) -> None:
        action = session.get("action")
        owner_id = message.from_user.id
        if action == "await_restore_file":
            if not message.document:
                raise ValueError("attach the compressed backup file")
            file = await bot.get_file(message.document.file_id)
            stream = await bot.download_file(file.file_path)
            backup = parse_backup(stream.read())
            restore_id = opaque_id("restore")
            await self.repositories.save_pending_restore(restore_id, owner_id, backup)
            await self.repositories.clear_owner_session(owner_id)
            counts = {name: len(rows) for name, rows in backup["collections"].items()}
            await message.answer(
                f"Validated {backup['kind']} backup: {counts}. Restore via upsert?",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Confirm restore", callback_data=f"restore:{restore_id}:confirm"),
                            InlineKeyboardButton(text="Cancel", callback_data=f"restore:{restore_id}:cancel"),
                        ]
                    ]
                ),
            )
            return
        if action == "await_campaign_name":
            campaign = await self.campaigns.create_draft(owner_id, message.text or "")
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Draft created. Start by choosing what kind of post to send.", reply_markup=content_type_keyboard(campaign["campaign_id"]))
            return
        if action == "await_channel_forward":
            if not await register_forwarded_channel(message, bot, self.repositories):
                raise ValueError("forward a normal post from a channel where the bot is an administrator")
            await self.repositories.clear_owner_session(owner_id)
            return
        if action == "await_channel_tag":
            tags = sorted({value.strip().lower() for value in (message.text or "").split(",") if value.strip()})
            await self.repositories.set_channel_tags(session["channel_id"], tags)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(f"Tags saved: {', '.join(tags) or 'none'}")
            return
        campaign_id = session.get("campaign_id")
        if not campaign_id:
            await self.repositories.clear_owner_session(owner_id)
            return
        if action == "await_creative":
            await self._capture_creative(message, session)
        elif action == "await_album":
            await self._capture_album_item(message, session)
        elif action == "await_button_label":
            label = (message.text or "").strip()
            if not label:
                raise ValueError("send a button label")
            await self.repositories.set_owner_session(owner_id, {**session, "action": "await_button_url", "label": label})
            await message.answer(
                "Now send its direct destination URL, for example `https://t.me/example`.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_button_url":
            await self._save_button(message, session)
        elif action == "await_destination_name":
            name = (message.text or "").strip()
            if not name:
                raise ValueError("send a destination name")
            await self.repositories.set_owner_session(owner_id, {"action": "await_destination_url", "campaign_id": campaign_id, "name": name})
            await message.answer(
                "Send its direct Telegram/channel URL.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_destination_url":
            raw_url = (message.text or "").strip()
            Destination(display_name=session["name"], raw_url=raw_url)
            await self.repositories.set_owner_session(
                owner_id, {"action": "await_destination_id", "campaign_id": campaign_id, "name": session["name"], "url": raw_url}
            )
            await message.answer(
                "If you know this channel's numeric ID, send it now so it is protected from this campaign. Otherwise tap below.",
                reply_markup=_markup(
                    [
                        [
                            InlineKeyboardButton(text="Save without channel ID", callback_data=f"dest:{campaign_id}:skip"),
                        ]
                    ] + _navigation(f"c:{campaign_id}:open")
                ),
            )
        elif action == "await_destination_id":
            await self._save_destination(campaign_id, session["name"], session["url"], int((message.text or "").strip()))
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Destination saved and will be excluded from its own campaign.")
        elif action.startswith("await_target_"):
            await self._save_target(message, session)
        elif action == "await_schedule_start":
            start = self._parse_utc(message.text or "")
            await self.repositories.set_owner_session(owner_id, {"action": "await_schedule_end", "campaign_id": campaign_id, "start": start})
            await message.answer(
                "When should it end? Send `YYYY-MM-DD HH:MM` in UTC.",
                reply_markup=_markup(_navigation(f"c:{campaign_id}:open")),
            )
        elif action == "await_schedule_end":
            end = self._parse_utc(message.text or "")
            if end <= session["start"]:
                raise ValueError("end time must be after start time")
            await self.repositories.set_owner_session(
                owner_id, {"action": "await_schedule_interval", "campaign_id": campaign_id, "start": session["start"], "end": end}
            )
            duration_minutes = self._duration_minutes(session["start"], end)
            await message.answer(
                "How often should the post be replaced? The offered periods divide the campaign duration exactly.",
                reply_markup=schedule_interval_keyboard(campaign_id, duration_minutes),
            )
        elif action == "await_schedule_hours":
            # Complete a short-lived session created by a previous deployment.
            hours = int((message.text or "").strip())
            if hours < 0:
                raise ValueError("interval cannot be negative")
            await self._save_schedule(campaign_id, session["start"], session["end"], hours * 60)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Schedule saved.")
            await self._show_campaign(message, await self.repositories.get_campaign(campaign_id))
        elif action == "await_schedule_interval_custom":
            interval_minutes = parse_period_minutes(message.text or "", field="repost interval")
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            self._validate_repost_interval(duration_minutes, interval_minutes)
            await self._save_schedule(campaign_id, session["start"], session["end"], interval_minutes)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Schedule saved.")
            await self._show_campaign(message, await self.repositories.get_campaign(campaign_id))
        elif action == "await_schedule_repost_times":
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            offsets_minutes = parse_repost_offsets_minutes(message.text or "", duration_minutes=duration_minutes)
            final_cleanup = await self._save_schedule(campaign_id, session["start"], session["end"], 0, offsets_minutes)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(self._specific_schedule_saved_text(offsets_minutes, final_cleanup))
            await self._show_campaign(message, await self.repositories.get_campaign(campaign_id))
        elif action == "await_schedule_repost_gaps":
            duration_minutes = self._duration_minutes(session["start"], session["end"])
            offsets_minutes = parse_repost_gaps_minutes(message.text or "", duration_minutes=duration_minutes)
            final_cleanup = await self._save_schedule(campaign_id, session["start"], session["end"], 0, offsets_minutes)
            await self.repositories.clear_owner_session(owner_id)
            await message.answer(self._specific_schedule_saved_text(offsets_minutes, final_cleanup))
            await self._show_campaign(message, await self.repositories.get_campaign(campaign_id))
        elif action == "await_quick_duration":
            duration_minutes = parse_period_minutes(message.text or "", field="campaign duration")
            await self._begin_quick_interval(owner_id, message, campaign_id, duration_minutes)
        elif action == "await_quick_interval_custom":
            interval_minutes = parse_period_minutes(message.text or "", field="repost interval")
            await self._complete_quick_send(owner_id, message, campaign_id, int(session["duration_minutes"]), interval_minutes)
        elif action == "await_quick_repost_times":
            duration_minutes = int(session["duration_minutes"])
            offsets_minutes = parse_repost_offsets_minutes(message.text or "", duration_minutes=duration_minutes)
            await self._complete_quick_send(owner_id, message, campaign_id, duration_minutes, 0, offsets_minutes)
        elif action == "await_quick_repost_gaps":
            duration_minutes = int(session["duration_minutes"])
            offsets_minutes = parse_repost_gaps_minutes(message.text or "", duration_minutes=duration_minutes)
            await self._complete_quick_send(owner_id, message, campaign_id, duration_minutes, 0, offsets_minutes)
        elif action == "await_test_channel":
            campaign = await self.repositories.get_campaign(campaign_id)
            if not campaign or not campaign.get("variants"):
                raise ValueError("add a creative before testing")
            await self.sender.send_variant(int((message.text or "").strip()), campaign["variants"][0])
            await self.repositories.clear_owner_session(owner_id)
            await message.answer("Test send completed.")
            await self._show_campaign(message, campaign)

    async def _capture_creative(self, message: Message, session: Document) -> None:
        kind = session["kind"]
        if kind == "TEXT" and not message.text:
            raise ValueError("send a text message")
        if kind.startswith("PHOTO") and not message.photo:
            raise ValueError("send a photo")
        if kind == "PHOTO_TEXT" and not message.caption:
            raise ValueError("this option needs a photo with a caption")
        if kind.startswith("VIDEO") and not message.video:
            raise ValueError("send a video")
        if kind == "VIDEO_TEXT" and not message.caption:
            raise ValueError("this option needs a video with a caption")
        creative = capture_creative(message)
        campaign = await self.campaigns.editable_campaign(session["campaign_id"])
        variants = [*campaign.get("variants", []), creative.model_dump(mode="json")]
        await self.repositories.update_campaign(session["campaign_id"], {"variants": variants, "preview_sent": False, "updated_at": datetime.now(UTC)})
        await self.repositories.clear_owner_session(message.from_user.id)
        await self.sender.send_variant(message.from_user.id, creative.model_dump(mode="json"))
        await message.answer(
            "Creative saved. The post above is replayable; now add CTA buttons or continue configuring the campaign.",
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="+ Add CTA button", callback_data=f"btn:{session['campaign_id']}:{len(variants) - 1}:first")],
                    [
                        InlineKeyboardButton(text="Edit CTA layout", callback_data=f"edit:{session['campaign_id']}:{len(variants) - 1}"),
                        InlineKeyboardButton(text="Add another creative", callback_data=f"c:{session['campaign_id']}:add"),
                    ],
                    [InlineKeyboardButton(text="Continue campaign setup", callback_data=f"c:{session['campaign_id']}:open")],
                ]
            ),
        )

    async def _capture_album_item(self, message: Message, session: Document) -> None:
        captured = capture_creative(message)
        if captured.kind not in {"PHOTO", "VIDEO"}:
            raise ValueError("albums currently accept photos and videos only")
        media = [*session.get("media", []), captured.media[0]]
        if len(media) > 10:
            raise ValueError("an album can contain at most 10 items")
        await self.repositories.set_owner_session(
            message.from_user.id,
            {
                **session,
                "media": media,
                "caption": session.get("caption") or captured.caption,
                "caption_entities": session.get("caption_entities") or captured.caption_entities,
            },
        )
        await message.answer(
            f"Album item {len(media)} saved. Send another photo/video, or finish the album.",
            reply_markup=_markup(
                [
                    [
                        InlineKeyboardButton(text="Finish album", callback_data=f"album:{session['campaign_id']}:finish"),
                    ]
                ]
            ),
        )

    async def _finish_album(self, query: CallbackQuery, campaign_id: str) -> None:
        session = await self.repositories.owner_session(query.from_user.id)
        if not session or session.get("action") != "await_album" or session.get("campaign_id") != campaign_id:
            raise ValueError("album session expired; start a new album")
        media = session.get("media", [])
        if len(media) < 2:
            raise ValueError("an album needs at least two items")
        campaign = await self.campaigns.editable_campaign(campaign_id)
        creative = Creative(
            id=opaque_id("var"),
            kind="MEDIA_GROUP",
            media=media,
            caption=session.get("caption"),
            caption_entities=session.get("caption_entities", []),
        )
        variants = [*campaign.get("variants", []), creative.model_dump(mode="json")]
        await self.repositories.update_campaign(campaign_id, {"variants": variants, "preview_sent": False, "updated_at": datetime.now(UTC)})
        await self.repositories.clear_owner_session(query.from_user.id)
        await self.sender.send_variant(query.from_user.id, creative.model_dump(mode="json"))
        await query.message.answer(
            "Album saved. Telegram renders its CTA as a compact message after the album when buttons are added.",
            reply_markup=_markup(
                [
                    [InlineKeyboardButton(text="+ Add CTA button", callback_data=f"btn:{campaign_id}:{len(variants) - 1}:first")],
                    [InlineKeyboardButton(text="Continue campaign setup", callback_data=f"c:{campaign_id}:open")],
                ]
            ),
        )

    async def _save_button(self, message: Message, session: Document) -> None:
        campaign = await self.campaigns.editable_campaign(session["campaign_id"])
        variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
        index = int(session["variant_index"])
        creative = variants[index]
        placement = session["placement"]
        if placement == "right" and creative.buttons:
            row = max(button.row for button in creative.buttons)
            position = max(button.position for button in creative.buttons if button.row == row) + 1
            creative.button_layout = "CUSTOM"
        elif placement == "below" and creative.buttons:
            row = max(button.row for button in creative.buttons) + 1
            position = 0
            creative.button_layout = "CUSTOM"
        else:
            row = position = 0
        creative.buttons.append(Button(id=opaque_id("btn"), text=session["label"], url=(message.text or "").strip(), row=row, position=position))
        stored = [item.model_dump(mode="json") for item in variants]
        await self.repositories.update_campaign(session["campaign_id"], {"variants": stored, "preview_sent": False, "updated_at": datetime.now(UTC)})
        await self.repositories.clear_owner_session(message.from_user.id)
        campaign["variants"] = stored
        await message.answer("CTA button saved. Use the placement controls to add the next one beside it or on a new row.")
        await self._show_button_editor(message, campaign, index)

    async def _save_destination(self, campaign_id: str, name: str, url: str, chat_id: int | None) -> None:
        campaign = await self.campaigns.editable_campaign(campaign_id)
        destination = Destination(display_name=name, raw_url=url, telegram_chat_id=chat_id)
        await self.repositories.update_campaign(
            campaign_id,
            {"destinations": [*campaign.get("destinations", []), destination.model_dump(mode="json")], "preview_sent": False, "updated_at": datetime.now(UTC)},
        )

    async def _save_target(self, message: Message, session: Document) -> None:
        kind = session["action"].removeprefix("await_target_")
        raw = (message.text or "").strip()
        campaign = await self.campaigns.editable_campaign(session["campaign_id"])
        selector = dict(campaign.get("target_selector") or {})
        if kind == "tags":
            selector["tags_any"] = sorted({item.strip().lower() for item in raw.split(",") if item.strip()})
        elif kind == "members":
            selector["minimum_members"] = int(raw)
        else:
            selector[f"{kind}_ids"] = [int(item.strip()) for item in raw.split(",") if item.strip()]
        await self.repositories.update_campaign(session["campaign_id"], {"target_selector": selector, "preview_sent": False, "updated_at": datetime.now(UTC)})
        await self.repositories.clear_owner_session(message.from_user.id)
        await message.answer("Target selection saved. Destination channels will remain protected.")

    async def _save_schedule(
        self,
        campaign_id: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
        repost_offsets_minutes: list[int] | None = None,
    ) -> bool:
        interval = interval_minutes * 60 or None
        start = as_utc(start)
        end = as_utc(end)
        duration_minutes = self._duration_minutes(start, end)
        final_cleanup = (
            self._specific_times_support_final_cleanup(duration_minutes, repost_offsets_minutes)
            if repost_offsets_minutes is not None
            else True
        )
        await self.campaigns.editable_campaign(campaign_id)
        timezone = await self.repositories.get_setting("owner_timezone", "UTC")
        await self.repositories.update_campaign(
            campaign_id,
            {
                "start_at_utc": start,
                "original_end_at_utc": end,
                "current_end_at_utc": end,
                "repost_interval_seconds": interval,
                "repost_offsets_seconds": [minutes * 60 for minutes in repost_offsets_minutes] if repost_offsets_minutes is not None else None,
                "delete_on_repost": True,
                "delete_on_end": final_cleanup,
                "owner_timezone": timezone,
                "preview_sent": False,
                "updated_at": datetime.now(UTC),
            },
        )
        return final_cleanup

    @staticmethod
    def _specific_schedule_saved_text(offsets_minutes: list[int], final_cleanup: bool) -> str:
        plan = ", ".join(_period_label(value) for value in offsets_minutes)
        if final_cleanup:
            return f"Specific repost plan saved: {plan}. Every repost replaces the previous post; final cleanup is enabled."
        return (
            f"Specific repost plan saved: {plan}. Every repost replaces the previous post. "
            "The final post will remain after campaign end because this plan has a gap over 47 hours."
        )

    @staticmethod
    def _parse_utc(raw: str) -> datetime:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
