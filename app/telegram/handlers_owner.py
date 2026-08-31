"""Private owner control plane. Callback data is intentionally tiny and untrusted."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.backups.export import make_backup
from app.backups.restore import parse_backup, restore_backup
from app.campaigns.models import Button, CampaignMode, Creative, Destination
from app.campaigns.service import CampaignService
from app.db.repositories import Repositories
from app.telegram.formatting import capture_creative
from app.telegram.handlers_admin_updates import refresh_channel, register_forwarded_channel
from app.telegram.keyboards import home_keyboard
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id


def campaign_keyboard(campaign_id: str, active: bool = False, archived: bool = False) -> InlineKeyboardMarkup:
    if archived:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Duplicate / Run Again", callback_data=f"c:{campaign_id}:duplicate"),
            InlineKeyboardButton(text="Back", callback_data="home:campaigns"),
        ]])
    if active:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="View Progress", callback_data=f"c:{campaign_id}:progress"),
             InlineKeyboardButton(text="Retry Failed", callback_data=f"c:{campaign_id}:retry")],
            [InlineKeyboardButton(text="+6h", callback_data=f"c:{campaign_id}:extend6"),
             InlineKeyboardButton(text="+1d", callback_data=f"c:{campaign_id}:extend24")],
            [InlineKeyboardButton(text="End Campaign", callback_data=f"c:{campaign_id}:end")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add Variant", callback_data=f"c:{campaign_id}:add"),
         InlineKeyboardButton(text="Add/Edit Buttons", callback_data=f"c:{campaign_id}:button")],
        [InlineKeyboardButton(text="Destinations", callback_data=f"c:{campaign_id}:destination"),
         InlineKeyboardButton(text="Targets", callback_data=f"c:{campaign_id}:targets")],
        [InlineKeyboardButton(text="Mode", callback_data=f"c:{campaign_id}:mode"),
         InlineKeyboardButton(text="Schedule", callback_data=f"c:{campaign_id}:schedule")],
        [InlineKeyboardButton(text="Preview", callback_data=f"c:{campaign_id}:preview"),
         InlineKeyboardButton(text="Test Send", callback_data=f"c:{campaign_id}:test")],
        [InlineKeyboardButton(text="Launch", callback_data=f"c:{campaign_id}:launch")],
    ])


def mode_keyboard(campaign_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Standard", callback_data=f"m:{campaign_id}:STANDARD"),
        InlineKeyboardButton(text="Rotate", callback_data=f"m:{campaign_id}:ROTATE"),
    ], [InlineKeyboardButton(text="Mix + Rotate", callback_data=f"m:{campaign_id}:MIX_ROTATE")]])


class OwnerHandlers:
    def __init__(
        self, *, owner_ids: frozenset[int], repositories: Repositories, campaigns: CampaignService, sender: TelegramSender,
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

    async def start(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        active = await self.repositories.channel_count(active_only=True)
        await message.answer(
            f"iHarvester is ready. {active} active source channels are registered.\n\n"
            "Add me as a channel administrator, then create a campaign here.", reply_markup=home_keyboard()
        )

    async def backup(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        payload = await make_backup(self.repositories)
        await message.answer_document(
            BufferedInputFile(payload, filename="iharvester-core-backup.json.gz"),
            caption="Core backup: channels, campaign definitions, and settings.",
        )

    async def restore(self, message: Message) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        await self.repositories.set_owner_session(message.from_user.id, {"action": "await_restore_file"})
        await message.answer("Attach an iHarvester .json.gz backup. I will validate it and ask before restoring.")

    async def callback(self, query: CallbackQuery, bot: Bot) -> None:
        if not query.message or not self._allowed(query.from_user.id, query.message.chat.type):
            await query.answer("Owner-only private control.", show_alert=True)
            return
        data = query.data or ""
        try:
            if data.startswith("home:"):
                await self._home(query, data.split(":", 1)[1])
            elif data.startswith("m:"):
                _, campaign_id, mode = data.split(":", 2)
                campaign = await self.repositories.get_campaign(campaign_id)
                if not campaign or campaign["status"] != "DRAFT":
                    raise ValueError("mode can only be changed on an editable draft")
                await self.repositories.update_campaign(campaign_id, {"mode": CampaignMode(mode).value})
                await query.message.answer(f"Mode set to {mode}.", reply_markup=campaign_keyboard(campaign_id))
            elif data.startswith("restore:"):
                _, restore_id, action = data.split(":", 2)
                await self._restore_confirm(query, restore_id, action)
            elif data.startswith("reg:"):
                _, chat_id, action = data.split(":", 2)
                if action == "refresh":
                    success = await refresh_channel(bot, self.repositories, int(chat_id))
                    await query.message.answer("Channel refreshed." if success else "Channel needs attention; check bot admin rights.")
                elif action == "tag":
                    await self.repositories.set_owner_session(query.from_user.id, {"action": "await_channel_tag", "channel_id": int(chat_id)})
                    await query.message.answer("Send one or more comma-separated tags.")
                else:
                    raise ValueError("invalid registry action")
            elif data.startswith("c:"):
                _, campaign_id, action = data.split(":", 2)
                await self._campaign_action(query, campaign_id, action)
            else:
                await query.answer("Unknown or expired control.", show_alert=True)
                return
        except (ValueError, KeyError) as error:
            await query.message.answer(f"Could not complete that action: {error}")
        await query.answer()

    async def _home(self, query: CallbackQuery, action: str) -> None:
        if action == "create":
            await self.repositories.set_owner_session(query.from_user.id, {"action": "await_campaign_name"})
            await query.message.answer("Send the campaign name.")
        elif action == "network":
            active = await self.repositories.channel_count(active_only=True)
            total = await self.repositories.channel_count()
            await query.message.answer(f"Network: {active} active / {total} registered channels.")
        elif action == "campaigns":
            rows = await self.repositories.list_campaigns()
            if not rows:
                await query.message.answer("No campaigns yet.", reply_markup=home_keyboard())
            else:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"{row['name']} · {row['status']}", callback_data=f"c:{row['campaign_id']}:open")
                ] for row in rows])
                await query.message.answer("Campaigns", reply_markup=keyboard)
        elif action == "backups":
            await query.message.answer("Use /backup to export a core backup, or /restore then attach a backup file.")
        elif action == "settings":
            await query.message.answer("Campaign timestamps are stored in UTC; default owner timezone is configured with DEFAULT_TIMEZONE.")

    async def _campaign_action(self, query: CallbackQuery, campaign_id: str, action: str) -> None:
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("campaign no longer exists")
        if action == "open":
            await query.message.answer(
                f"{campaign['name']} — {campaign['status']}",
                reply_markup=campaign_keyboard(campaign_id, campaign["status"] == "ACTIVE", campaign["status"] == "ARCHIVED"),
            )
        elif action in {"add", "button", "destination", "schedule", "test"}:
            mapping = {
                "add": ("await_variant", "Send the formatted text or supported media for the next variant."),
                "button": ("await_button", "Send `LABEL | https://t.me/... | optional-row`. Use optional row then select Custom layout."),
                "destination": ("await_destination", "Send `Name | https://t.me/... | optional-channel-id`."),
                "schedule": ("await_schedule", "Send `2026-09-01T10:00:00+00:00 | 2026-09-02T10:00:00+00:00 | repost-hours (0 for single post)`."),
                "test": ("await_test_channel", "Send a numeric owner-controlled test channel ID."),
            }
            state, prompt = mapping[action]
            await self.repositories.set_owner_session(query.from_user.id, {"action": state, "campaign_id": campaign_id})
            await query.message.answer(prompt)
        elif action == "targets":
            await self.repositories.update_campaign(campaign_id, {"target_selector": {}, "updated_at": datetime.now(UTC)})
            await query.message.answer("Targets set to All Active. Promoted destination IDs will still be automatically excluded at launch.")
        elif action == "mode":
            await query.message.answer("Choose campaign mode.", reply_markup=mode_keyboard(campaign_id))
        elif action == "preview":
            variants = campaign.get("variants", [])
            if not variants:
                raise ValueError("add a variant first")
            await self.sender.send_variant(query.from_user.id, variants[0])
            await self.repositories.update_campaign(campaign_id, {"preview_sent": True})
            await query.message.answer("Real Telegram preview sent.")
        elif action == "launch":
            activated = await self.campaigns.activate(campaign_id)
            excluded = len(activated["protected_destination_ids"])
            await query.message.answer(
                f"Launched {activated['name']}. Targets: {len(activated['target_snapshot'])}; "
                f"promoted destinations excluded: {excluded}.",
                reply_markup=campaign_keyboard(campaign_id, active=True),
            )
        elif action == "extend6":
            await self.campaigns.extend(campaign_id, query.from_user.id, 6 * 3600)
            await query.message.answer("Campaign extended by 6 hours.")
        elif action == "extend24":
            await self.campaigns.extend(campaign_id, query.from_user.id, 24 * 3600)
            await query.message.answer("Campaign extended by 1 day.")
        elif action == "end":
            changed = await self.campaigns.end_early(campaign_id)
            text = (
                "Campaign ending: future cycles stopped; current live posts will be cleaned up and archived."
                if changed
                else "Campaign was already ending or archived."
            )
            await query.message.answer(text)
        elif action == "duplicate":
            copied = await self.campaigns.duplicate(campaign_id, query.from_user.id)
            await query.message.answer("New editable draft created.", reply_markup=campaign_keyboard(copied["campaign_id"]))
        elif action == "progress":
            cycle = max(0, int(campaign.get("next_cycle_number", 1)) - 1)
            summary = await self.repositories.delivery_summary(campaign_id, cycle)
            await query.message.answer(f"Cycle {cycle} progress: {summary or 'no deliveries yet'}")
        elif action == "retry":
            await query.message.answer(
                "Retrying only needs a corrected channel/admin condition; eligible retries are scheduled automatically. "
                "Unknown send states are intentionally not blindly retried."
            )

    async def message(self, message: Message, bot: Bot) -> None:
        if not self._allowed(message.from_user.id if message.from_user else None, message.chat.type):
            return
        session = await self.repositories.owner_session(message.from_user.id)
        if not session:
            await register_forwarded_channel(message, bot, self.repositories)
            return
        action = session.get("action")
        if action == "await_restore_file":
            if not message.document:
                await message.answer("Please attach the compressed backup file.")
                return
            file = await bot.get_file(message.document.file_id)
            stream = await bot.download_file(file.file_path)
            backup = parse_backup(stream.read())
            restore_id = opaque_id("restore")
            await self.repositories.save_pending_restore(restore_id, message.from_user.id, backup)
            counts = {name: len(rows) for name, rows in backup["collections"].items()}
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer(
                f"Validated {backup['kind']} backup: {counts}. Restore via upsert? Active historical campaigns will return archived.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Confirm Restore", callback_data=f"restore:{restore_id}:confirm"),
                    InlineKeyboardButton(text="Cancel", callback_data=f"restore:{restore_id}:cancel"),
                ]]),
            )
            return
        if action == "await_campaign_name":
            campaign = await self.campaigns.create_draft(message.from_user.id, message.text or "")
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Draft created. Add its first creative.", reply_markup=campaign_keyboard(campaign["campaign_id"]))
            return
        campaign_id = session.get("campaign_id")
        if action == "await_channel_tag":
            tags = sorted({value.strip().lower() for value in (message.text or "").split(",") if value.strip()})
            await self.repositories.db.channels.update_one(
                {"telegram_chat_id": session["channel_id"]}, {"$set": {"tags": tags, "updated_at": datetime.now(UTC)}}
            )
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer(f"Tags saved: {', '.join(tags) or 'none'}")
            return
        if not campaign_id:
            await self.repositories.clear_owner_session(message.from_user.id)
            return
        campaign = await self.repositories.get_campaign(campaign_id)
        if not campaign or campaign["status"] != "DRAFT":
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("That draft is no longer editable.")
            return
        if action == "await_variant":
            creative = capture_creative(message)
            variants = [*campaign.get("variants", []), creative.model_dump(mode="json")]
            await self.repositories.update_campaign(campaign_id, {"variants": variants, "updated_at": datetime.now(UTC)})
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Variant captured with its Telegram entities/file IDs.", reply_markup=campaign_keyboard(campaign_id))
        elif action == "await_button":
            parts = [part.strip() for part in (message.text or "").split("|")]
            if len(parts) < 2:
                raise ValueError("Button format is LABEL | URL | optional-row")
            variants = [Creative.model_validate(item) for item in campaign.get("variants", [])]
            if not variants:
                raise ValueError("Add a variant before adding buttons")
            row = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            variants[-1].buttons.append(Button(id=opaque_id("btn"), text=parts[0], url=parts[1], row=row, position=len(variants[-1].buttons)))
            if len(parts) > 2:
                variants[-1].button_layout = "CUSTOM"
            await self.repositories.update_campaign(campaign_id, {"variants": [item.model_dump(mode="json") for item in variants]})
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Real CTA button saved on the latest variant.", reply_markup=campaign_keyboard(campaign_id))
        elif action == "await_destination":
            parts = [part.strip() for part in (message.text or "").split("|")]
            if len(parts) < 2:
                raise ValueError("Destination format is Name | URL | optional-channel-id")
            destination = Destination(display_name=parts[0], raw_url=parts[1], telegram_chat_id=int(parts[2]) if len(parts) > 2 and parts[2] else None)
            await self.repositories.update_campaign(campaign_id, {"destinations": [*campaign.get("destinations", []), destination.model_dump(mode="json")]})
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Destination saved. Its channel ID will be protected automatically at launch.", reply_markup=campaign_keyboard(campaign_id))
        elif action == "await_schedule":
            parts = [part.strip() for part in (message.text or "").split("|")]
            if len(parts) != 3:
                raise ValueError("Schedule format needs start | end | repost-hours")
            start = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).astimezone(UTC)
            end = datetime.fromisoformat(parts[1].replace("Z", "+00:00")).astimezone(UTC)
            interval = int(parts[2]) * 3600 or None
            await self.repositories.update_campaign(campaign_id, {
                "start_at_utc": start, "original_end_at_utc": end, "current_end_at_utc": end,
                "repost_interval_seconds": interval, "delete_on_repost": True, "delete_on_end": True,
                "owner_timezone": "UTC",
            })
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Schedule saved.", reply_markup=campaign_keyboard(campaign_id))
        elif action == "await_test_channel":
            test_channel = int((message.text or "").strip())
            if not campaign.get("variants"):
                raise ValueError("Add a variant before testing")
            await self.sender.send_variant(test_channel, campaign["variants"][0])
            await self.repositories.clear_owner_session(message.from_user.id)
            await message.answer("Real test send completed.")

    async def _restore_confirm(self, query: CallbackQuery, restore_id: str, action: str) -> None:
        pending = await self.repositories.get_pending_restore(restore_id, query.from_user.id)
        if not pending:
            raise ValueError("restore confirmation expired")
        if action == "cancel":
            await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
            await query.message.answer("Restore cancelled.")
            return
        if action != "confirm":
            raise ValueError("invalid restore action")
        results = await restore_backup(self.repositories, pending["backup"])
        await self.repositories.delete_pending_restore(restore_id, query.from_user.id)
        await query.message.answer(f"Restore complete (restored, skipped): {results}")
