"""Main-owner managed, isolated iHarvester bot clones.

Clone metadata is held in the primary database, but every clone receives a
separate Mongo database, Bot API client, dispatcher, workers and webhook
secret.  A clone deliberately does not load this module's control router, so
it can never create another clone.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from cryptography.fernet import Fernet, InvalidToken
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.backups.automatic import AutomaticBackupWorker
from app.campaigns.scheduler import Scheduler
from app.campaigns.service import CampaignService
from app.config import Settings
from app.db.client import Database
from app.db.indexes import ensure_indexes
from app.db.leases import LeaseManager
from app.db.repositories import Repositories
from app.delivery.rate_limit import AsyncTokenBucket
from app.delivery.worker import DeliveryWorker
from app.network.refresh_worker import ChannelRefreshWorker
from app.telegram.handlers_admin_updates import ChannelAdminHandlers
from app.telegram.handlers_client_requests import ClientRequestHandlers
from app.telegram.handlers_join_events import JoinEventHandlers
from app.telegram.handlers_owner import OwnerHandlers
from app.telegram.keyboards import home_keyboard
from app.telegram.raw_api import RawTelegramAPI
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id

if TYPE_CHECKING:
    from app.main import Runtime

logger = logging.getLogger(__name__)

_ATLAS_DATABASE_NAME_MAX_BYTES = 38


def _clone_id() -> str:
    return f"clone_{secrets.token_hex(8)}"


def _webhook_secret(clone_id: str) -> str:
    return f"clone_{clone_id}_{secrets.token_urlsafe(24)}"


def _owner_ids(value: str) -> frozenset[int]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values or any(not item.isdigit() or int(item) <= 0 for item in values):
        raise ValueError("creator IDs must be comma-separated positive Telegram user IDs")
    return frozenset(int(item) for item in values)


class CloneManager:
    """Owns child runtimes; only the primary runtime ever instantiates it."""

    def __init__(self, main_runtime: Runtime) -> None:
        self.main_runtime = main_runtime
        self._children: dict[str, Runtime] = {}
        key = main_runtime.settings.clone_token_encryption_key
        self._cipher = Fernet(key.encode("ascii")) if key else None

    @property
    def enabled(self) -> bool:
        return self._cipher is not None and self.main_runtime.settings.run_mode == "webhook"

    def _require_enabled(self) -> None:
        if self._cipher is None:
            raise ValueError("clone management is disabled: set CLONE_TOKEN_ENCRYPTION_KEY first")
        if self.main_runtime.settings.run_mode != "webhook":
            raise ValueError("clone management requires RUN_MODE=webhook on the main deployment")

    def _seal(self, token: str) -> str:
        self._require_enabled()
        assert self._cipher is not None
        return self._cipher.encrypt(token.encode("utf-8")).decode("ascii")

    def _open(self, ciphertext: str) -> str:
        self._require_enabled()
        assert self._cipher is not None
        try:
            return self._cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as error:
            raise ValueError("clone token cannot be decrypted; do not change CLONE_TOKEN_ENCRYPTION_KEY") from error

    def _clone_settings(self, clone: dict[str, Any]) -> Settings:
        token = self._open(str(clone["token_ciphertext"]))
        owner_ids = ",".join(str(value) for value in clone["creator_user_ids"])
        # Construct afresh instead of ``model_copy``: ``owner_ids`` is a
        # cached property, and copying the primary Settings instance could
        # accidentally carry the main owner's allow-list into a clone.
        return Settings(
            **{
                **self.main_runtime.settings.model_dump(),
                "bot_token": token,
                "owner_user_ids": owner_ids,
                "mongodb_db_name": str(clone["mongodb_db_name"]),
                "webhook_path_secret": str(clone["webhook_path_secret"]),
                "webhook_secret_token": self._open(str(clone["webhook_secret_ciphertext"])),
            }
        )

    def _clone_database_name(self, clone_id: str) -> str:
        # All generated IDs are ASCII.  Settings bounds the configured prefix
        # to 15 characters, which leaves room for ``_<22-char clone id>``.
        return f"{self.main_runtime.settings.clone_database_prefix}_{clone_id}"

    async def _repair_overlong_database_name(self, clone: dict[str, Any]) -> dict[str, Any]:
        """Repair the brief v1 clone-prefix bug before any child starts.

        The original default produced a 39-byte Atlas database name. Such a
        database cannot have been created, so changing its stored name cannot
        lose existing creator data.  This also makes deployments self-heal the
        one affected clone rather than requiring an operator database edit.
        """
        database_name = str(clone["mongodb_db_name"])
        if len(database_name.encode("utf-8")) <= _ATLAS_DATABASE_NAME_MAX_BYTES:
            return clone
        clone_id = str(clone["clone_id"])
        repaired_name = self._clone_database_name(clone_id)
        await self.main_runtime.repositories.update_bot_clone(clone_id, mongodb_db_name=repaired_name)
        logger.warning(
            "Repaired overlong clone database name",
            extra={"clone_id": clone_id, "database_name": repaired_name},
        )
        return {**clone, "mongodb_db_name": repaired_name}

    async def start_all(self) -> None:
        if not self.enabled:
            return
        for clone in await self.main_runtime.repositories.active_bot_clones():
            try:
                await self._start_with_status(await self._repair_overlong_database_name(clone))
            except ValueError:
                logger.exception("Could not start clone", extra={"clone_id": clone.get("clone_id")})

    async def create_clone(self, *, label: str, token: str, creator_ids: frozenset[int]) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("Set CLONE_TOKEN_ENCRYPTION_KEY on the main deployment before adding a clone.")
        label = " ".join(label.split())
        if not 1 <= len(label) <= 80:
            raise ValueError("clone label must be 1 to 80 characters")
        verification_bot = Bot(token)
        try:
            try:
                bot_user = await verification_bot.get_me()
            except Exception as error:
                raise ValueError("Telegram could not authenticate that BotFather token") from error
        finally:
            await verification_bot.session.close()
        if not bot_user.is_bot:
            raise ValueError("that token did not authenticate as a Telegram bot")
        clone_id = _clone_id()
        document: dict[str, Any] = {
            "clone_id": clone_id,
            "label": label,
            # A clone only becomes active after every child-runtime startup
            # check succeeds.  This avoids a bot that looks live but has no
            # registered webhook or workers.
            "active": False,
            "token_ciphertext": self._seal(token),
            "creator_user_ids": sorted(creator_ids),
            "bot_user_id": bot_user.id,
            "bot_username": bot_user.username,
            "mongodb_db_name": self._clone_database_name(clone_id),
            "webhook_path_secret": _webhook_secret(clone_id),
            "webhook_secret_ciphertext": self._seal(secrets.token_urlsafe(32)),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        try:
            await self.main_runtime.repositories.create_bot_clone(document)
        except DuplicateKeyError as error:
            raise ValueError("that Telegram bot is already registered as a clone") from error
        try:
            await self._start_with_status(document)
        except ValueError:
            raise
        created = await self.main_runtime.repositories.get_bot_clone(clone_id)
        return created or document

    async def create_clone_from_encrypted_token(
        self, *, label: str, token_ciphertext: str, creator_ids: frozenset[int]
    ) -> dict[str, Any]:
        """Create a clone without ever persisting a plaintext BotFather token."""
        return await self.create_clone(label=label, token=self._open(token_ciphertext), creator_ids=creator_ids)

    async def start_clone(self, clone: dict[str, Any]) -> Runtime:
        clone = await self._repair_overlong_database_name(clone)
        clone_id = str(clone["clone_id"])
        if clone_id in self._children:
            return self._children[clone_id]
        settings = self._clone_settings(clone)
        runtime = await _start_child_runtime(settings)
        self._children[clone_id] = runtime
        return runtime

    @staticmethod
    def _safe_start_error(error: Exception) -> str:
        """Keep an actionable but bounded operator diagnostic in MongoDB."""
        detail = " ".join(str(error).split())
        if not detail:
            detail = "no additional detail was returned"
        return f"{type(error).__name__}: {detail[:240]}"

    async def _start_with_status(self, clone: dict[str, Any]) -> Runtime:
        clone_id = str(clone["clone_id"])
        try:
            runtime = await self.start_clone(clone)
        except Exception as error:
            diagnostic = self._safe_start_error(error)
            logger.exception("Clone runtime startup failed", extra={"clone_id": clone_id})
            await self.main_runtime.repositories.update_bot_clone(
                clone_id,
                active=False,
                last_start_error=diagnostic,
                last_start_failed_at=datetime.now(UTC),
            )
            raise ValueError(f"clone startup failed: {diagnostic}") from error
        await self.main_runtime.repositories.update_bot_clone(
            clone_id,
            active=True,
            last_start_error=None,
            last_started_at=datetime.now(UTC),
        )
        return runtime

    async def stop_clone(self, clone_id: str, *, deactivate: bool = True) -> None:
        runtime = self._children.pop(clone_id, None)
        if runtime:
            try:
                await runtime.bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                logger.warning("Could not detach clone webhook", extra={"clone_id": clone_id})
            await _stop_child_runtime(runtime)
        if deactivate:
            await self.main_runtime.repositories.update_bot_clone(
                clone_id,
                active=False,
                last_start_error=None,
                last_paused_at=datetime.now(UTC),
            )

    async def activate_clone(self, clone_id: str) -> None:
        clone = await self.main_runtime.repositories.get_bot_clone(clone_id)
        if not clone:
            raise ValueError("clone not found")
        await self._start_with_status(clone)

    async def set_creators(self, clone_id: str, creator_ids: frozenset[int]) -> None:
        clone = await self.main_runtime.repositories.get_bot_clone(clone_id)
        if not clone:
            raise ValueError("clone not found")
        was_active = bool(clone.get("active"))
        await self.stop_clone(clone_id, deactivate=False)
        await self.main_runtime.repositories.update_bot_clone(
            clone_id,
            creator_user_ids=sorted(creator_ids),
            active=False,
        )
        if was_active:
            updated = await self.main_runtime.repositories.get_bot_clone(clone_id)
            if updated:
                await self._start_with_status(updated)

    async def dispatch_webhook(self, path_secret: str, request_secret: str, payload: dict[str, Any]) -> bool:
        for runtime in self._children.values():
            settings = runtime.settings
            if not hmac.compare_digest(path_secret, settings.webhook_path_secret or ""):
                continue
            if not hmac.compare_digest(request_secret, settings.webhook_secret_token or ""):
                return False
            update = Update.model_validate(payload, context={"bot": runtime.bot})
            try:
                registered = await runtime.repositories.register_update(update.update_id)
            except PyMongoError:
                return False
            if not registered:
                return True
            try:
                await runtime.dispatcher.feed_update(runtime.bot, update)
            except Exception:
                await runtime.repositories.unregister_update(update.update_id)
                raise
            return True
        return False


async def _start_child_runtime(settings: Settings) -> Runtime:
    """Start the normal bot stack with clone-only owner IDs and no clone router."""
    from app.main import Runtime  # avoids a runtime import cycle

    database = Database(settings.mongodb_uri, settings.mongodb_db_name)
    repositories = Repositories(database)
    bot = Bot(settings.bot_token)
    raw_api = RawTelegramAPI(settings.bot_token, settings.telegram_request_timeout_seconds)
    sender = TelegramSender(bot, raw_api)
    dispatcher = Dispatcher()
    service = CampaignService(repositories, settings.broadcast_send_rps)
    dispatcher.include_router(ChannelAdminHandlers(repositories).router)
    dispatcher.include_router(JoinEventHandlers(repositories).router)
    dispatcher.include_router(ClientRequestHandlers(repositories=repositories, owner_ids=settings.owner_ids).router)
    dispatcher.include_router(
        OwnerHandlers(
            owner_ids=settings.owner_ids,
            repositories=repositories,
            campaigns=service,
            sender=sender,
            public_base_url=settings.resolved_public_base_url,
        ).router
    )
    runtime = Runtime(settings, database, repositories, bot, dispatcher, sender)
    try:
        await database.ping()
        await ensure_indexes(database)
        for key, value in {
            "owner_timezone": settings.default_timezone,
            "auto_backup_enabled": settings.auto_backup_enabled,
            "auto_backup_every_new_channels": settings.auto_backup_every_new_channels,
            "auto_backup_interval_hours": settings.auto_backup_interval_hours,
        }.items():
            if await repositories.get_setting(key) is None:
                await repositories.set_setting(key, value)
        bot_user = await bot.get_me()
        runtime.bot_username = bot_user.username
        allowed_updates = dispatcher.resolve_used_update_types()
        await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret_token, allowed_updates=allowed_updates, drop_pending_updates=False)
        instance_id = opaque_id("clone")
        lease_manager = LeaseManager(database)
        scheduler = Scheduler(
            instance_id=instance_id,
            repositories=repositories,
            lease_manager=lease_manager,
            campaign_service=service,
            lease_seconds=settings.scheduler_lease_seconds,
            tick_seconds=settings.scheduler_tick_seconds,
        )
        runtime.tasks.append(asyncio.create_task(scheduler.run(runtime.stopping)))
        send_limiter = AsyncTokenBucket(settings.broadcast_send_rps)
        mutation_limiter = AsyncTokenBucket(settings.broadcast_global_api_rps)
        backup = AutomaticBackupWorker(
            instance_id=instance_id,
            repositories=repositories,
            lease_manager=lease_manager,
            bot=bot,
            owner_ids=settings.owner_ids,
            every_new_channels=settings.auto_backup_every_new_channels,
            interval_hours=settings.auto_backup_interval_hours,
        )
        runtime.tasks.append(asyncio.create_task(backup.run(runtime.stopping)))
        refresh = ChannelRefreshWorker(
            worker_id=f"{instance_id}-network-refresh",
            bot=bot,
            repositories=repositories,
            request_limiter=mutation_limiter,
            lease_seconds=settings.delivery_lease_seconds,
            max_attempts=settings.max_transient_attempts,
        )
        runtime.tasks.append(asyncio.create_task(refresh.run(runtime.stopping)))
        for number in range(settings.broadcast_workers):
            worker = DeliveryWorker(
                worker_id=f"{instance_id}-{number}",
                repositories=repositories,
                sender=sender,
                send_limiter=send_limiter,
                mutation_limiter=mutation_limiter,
                delivery_lease_seconds=settings.delivery_lease_seconds,
                max_attempts=settings.max_transient_attempts,
            )
            runtime.tasks.append(asyncio.create_task(worker.run(runtime.stopping)))
        runtime.ready = True
        return runtime
    except Exception:
        await _stop_child_runtime(runtime)
        raise


async def _stop_child_runtime(runtime: Runtime) -> None:
    runtime.ready = False
    runtime.stopping.set()
    for task in runtime.tasks:
        task.cancel()
    if runtime.tasks:
        await asyncio.gather(*runtime.tasks, return_exceptions=True)
    await runtime.sender.raw_api.close()
    await runtime.bot.session.close()
    await runtime.database.close()


class CloneAdminHandlers:
    """A compact owner-only control surface, mounted only on the primary bot."""

    def __init__(self, manager: CloneManager, owner_ids: frozenset[int]) -> None:
        self.manager = manager
        self.owner_ids = owner_ids
        self.router = Router(name="clone-admin")
        self.router.message.register(self.clones, Command("clones"))
        self.router.message.register(self.clone_add, Command("cloneadd"))
        self.router.message.register(self.clone_stop, Command("clonestop"))
        self.router.message.register(self.clone_start, Command("clonestart"))
        self.router.message.register(self.clone_owners, Command("cloneowners"))

    def _allowed(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.owner_ids and message.chat.type == "private")

    async def clones(self, message: Message) -> None:
        if not self._allowed(message):
            return
        if not self.manager.enabled:
            await message.answer("Clone management requires CLONE_TOKEN_ENCRYPTION_KEY and RUN_MODE=webhook on the main deployment.")
            return
        clones = await self.manager.main_runtime.repositories.list_bot_clones()
        if not clones:
            await message.answer("No creator clones yet.\n\nUse /cloneadd Name | bot-token | creator-user-id")
            return
        lines = ["Creator clones"]
        for clone in clones:
            owners = ", ".join(str(value) for value in clone.get("creator_user_ids", []))
            lines.append(
                f"\n• {clone['label']}\n  ID: {clone['clone_id']}\n"
                f"  @{clone.get('bot_username') or 'unnamed'} · {'active' if clone.get('active') else 'stopped'}\n"
                f"  creators: {owners}"
            )
        lines.append("\nAdd: /cloneadd Name | bot-token | creator-user-id[,creator-user-id]")
        lines.append("Change access: /cloneowners clone-id creator-user-id[,creator-user-id]")
        lines.append("Stop: /clonestop clone-id")
        lines.append("Start: /clonestart clone-id")
        await message.answer("\n".join(lines))

    async def clone_add(self, message: Message) -> None:
        if not self._allowed(message):
            return
        try:
            _, payload = (message.text or "").split(maxsplit=1)
            label, token, owners = [part.strip() for part in payload.split("|", 2)]
            clone = await self.manager.create_clone(label=label, token=token, creator_ids=_owner_ids(owners))
        except ValueError as error:
            await message.answer(f"Could not add clone: {error}\n\nUse /cloneadd Name | bot-token | creator-user-id[,creator-user-id]")
            return
        finally:
            # Do not leave a bot token in Telegram chat history. Failure to
            # delete is harmless (for example, a client may already delete it).
            try:
                await message.delete()
            except Exception:
                logger.warning("Could not remove clone token command")
        await message.answer(
            f"Clone ready: {clone['label']} (@{clone.get('bot_username') or clone['bot_user_id']}).\n"
            "Only the listed creator IDs can use it. Its campaigns and channels are isolated in "
            f"{clone['mongodb_db_name']}."
        )

    async def clone_stop(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Use /clonestop clone-id")
            return
        if not await self.manager.main_runtime.repositories.get_bot_clone(parts[1].strip()):
            await message.answer("Clone not found.")
            return
        await self.manager.stop_clone(parts[1].strip())
        await message.answer("Clone stopped. Its data is preserved and it will not receive updates until re-enabled by an operator.")

    async def clone_start(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Use /clonestart clone-id")
            return
        try:
            await self.manager.activate_clone(parts[1].strip())
        except ValueError as error:
            await message.answer(f"Could not start clone: {error}")
            return
        await message.answer("Clone started. Its webhook and isolated workers are active again.")

    async def clone_owners(self, message: Message) -> None:
        if not self._allowed(message):
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Use /cloneowners clone-id creator-user-id[,creator-user-id]")
            return
        try:
            await self.manager.set_creators(parts[1], _owner_ids(parts[2]))
        except ValueError as error:
            await message.answer(f"Could not change clone access: {error}")
            return
        await message.answer("Clone creator access updated and its isolated runtime restarted.")


class _CloneWorkspaceSession(Filter):
    """Match only messages that belong to the guided clone workspace."""

    def __init__(self, manager: CloneManager, owner_ids: frozenset[int]) -> None:
        self.manager = manager
        self.owner_ids = owner_ids

    async def __call__(self, message: Message) -> bool:
        if not message.from_user or message.chat.type != "private" or message.from_user.id not in self.owner_ids:
            return False
        return bool(await self.manager.main_runtime.repositories.clone_setup_session(message.from_user.id))


class CloneWorkspaceHandlers:
    """Button-first, primary-owner-only management for creator bot clones.

    This router is intentionally mounted only on the primary iHarvester.  A
    child clone has the normal owner screens but can never see or call these
    controls, and its creators cannot create another clone.
    """

    def __init__(self, manager: CloneManager, owner_ids: frozenset[int]) -> None:
        self.manager = manager
        self.owner_ids = owner_ids
        self.router = Router(name="clone-workspace")
        self.router.callback_query.register(self.callback, F.data.startswith("clone:"))
        self.router.message.register(self.clones, Command("clones"))
        self.router.message.register(self.session_message, _CloneWorkspaceSession(manager, owner_ids))

    @property
    def repositories(self) -> Repositories:
        return self.manager.main_runtime.repositories

    @staticmethod
    def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _allowed_message(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.owner_ids and message.chat.type == "private")

    def _allowed_query(self, query: CallbackQuery) -> bool:
        return bool(query.message and query.message.chat.type == "private" and query.from_user.id in self.owner_ids)

    @staticmethod
    def _home_row() -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton(text="Home", callback_data="clone:exit")]

    async def _render(self, message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
        if message.from_user and message.from_user.is_bot and message.text:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
        await message.answer(text, reply_markup=markup)

    async def _show_home(self, message: Message, page: int = 0) -> None:
        if not self.manager.enabled:
            await self._render(
                message,
                "Creator clones are not enabled yet.\n\n"
                "Set CLONE_TOKEN_ENCRYPTION_KEY on the main deployment and redeploy. "
                "It encrypts each clone's BotFather token at rest.",
                self._markup([self._home_row()]),
            )
            return
        clones = await self.repositories.list_bot_clones()
        page_size = 8
        pages = max(1, (len(clones) + page_size - 1) // page_size)
        page = max(0, min(page, pages - 1))
        visible = clones[page * page_size : (page + 1) * page_size]
        active_count = sum(bool(clone.get("active")) for clone in clones)
        text = (
            "Creator clones\n\n"
            "Create an isolated iHarvester for a creator. Each clone has its own channels, campaigns, "
            "workers, webhook, and Mongo database. Only its authorised creators can manage it.\n\n"
            f"Clones: {len(clones)} | Active: {active_count} | Paused: {len(clones) - active_count}"
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="Create a clone", callback_data="clone:new")]
        ]
        for clone in visible:
            state = "Active" if clone.get("active") else "Paused"
            username = f" @{clone['bot_username']}" if clone.get("bot_username") else ""
            rows.append([
                InlineKeyboardButton(
                    text=f"{state} - {clone['label']}{username}",
                    callback_data=f"clone:open:{clone['clone_id']}",
                )
            ])
        if pages > 1:
            navigation: list[InlineKeyboardButton] = []
            if page:
                navigation.append(InlineKeyboardButton(text="Previous", callback_data=f"clone:home:{page - 1}"))
            navigation.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="clone:noop"))
            if page + 1 < pages:
                navigation.append(InlineKeyboardButton(text="Next", callback_data=f"clone:home:{page + 1}"))
            rows.append(navigation)
        rows.append(self._home_row())
        await self._render(message, text, self._markup(rows))

    async def _show_detail(self, message: Message, clone_id: str) -> None:
        clone = await self.repositories.get_bot_clone(clone_id)
        if not clone:
            await self._show_home(message)
            return
        status = "Active" if clone.get("active") else "Paused"
        username = f"@{clone['bot_username']}" if clone.get("bot_username") else str(clone["bot_user_id"])
        creators = ", ".join(str(value) for value in clone.get("creator_user_ids", [])) or "None"
        text = (
            f"{clone['label']}\n\n"
            f"Bot: {username}\n"
            f"Status: {status}\n"
            f"Authorised creator IDs: {creators}\n\n"
            "This clone is isolated from the main bot and all other creator bots."
        )
        if clone.get("last_start_error"):
            text += f"\n\nLast startup issue:\n{clone['last_start_error']}"
        run_button = (
            InlineKeyboardButton(text="Pause clone", callback_data=f"clone:pause:{clone_id}")
            if clone.get("active")
            else InlineKeyboardButton(text="Resume clone", callback_data=f"clone:resume:{clone_id}")
        )
        await self._render(
            message,
            text,
            self._markup(
                [
                    [run_button],
                    [InlineKeyboardButton(text="Manage authorised creators", callback_data=f"clone:access:{clone_id}")],
                    [InlineKeyboardButton(text="Back to creator clones", callback_data="clone:home")],
                    self._home_row(),
                ]
            ),
        )

    async def _show_access(self, message: Message, clone_id: str) -> None:
        clone = await self.repositories.get_bot_clone(clone_id)
        if not clone:
            await self._show_home(message)
            return
        creators = clone.get("creator_user_ids", [])
        text = (
            f"Authorised creators - {clone['label']}\n\n"
            "Only these Telegram user IDs can use this clone:\n"
            + "\n".join(f"- {creator_id}" for creator_id in creators)
            + "\n\nReplacing the list safely refreshes the clone so access changes apply immediately."
        )
        await self._render(
            message,
            text,
            self._markup(
                [
                    [InlineKeyboardButton(text="Replace authorised creators", callback_data=f"clone:accessedit:{clone_id}")],
                    [InlineKeyboardButton(text="Back", callback_data=f"clone:open:{clone_id}")],
                    self._home_row(),
                ]
            ),
        )

    async def _begin_creation(self, message: Message, owner_id: int) -> None:
        await self.repositories.set_clone_setup_session(owner_id, {"step": "name"})
        await self._render(
            message,
            "Create a creator clone\n\nWhat should this creator's clone be called?\n\nExample: Alice Promotions",
            self._markup(
                [
                    [InlineKeyboardButton(text="Cancel", callback_data="clone:cancel")],
                    self._home_row(),
                ]
            ),
        )

    async def _prompt_token(self, message: Message, label: str) -> None:
        await self._render(
            message,
            f"Clone name: {label}\n\nSend the BotFather token for this new creator bot. "
            "Your token message will be deleted immediately and only an encrypted copy is kept.",
            self._markup(
                [
                    [InlineKeyboardButton(text="Cancel", callback_data="clone:cancel")],
                    [InlineKeyboardButton(text="Back", callback_data="clone:new")],
                    self._home_row(),
                ]
            ),
        )

    async def _prompt_creators(self, message: Message, label: str) -> None:
        await self._render(
            message,
            f"Clone name: {label}\n\nSend the Telegram user ID(s) allowed to operate it, separated by commas.\n\n"
            "Example: 123456789 or 123456789, 987654321\n\n"
            "Only these people will be able to use this clone's control room.",
            self._markup(
                [
                    [InlineKeyboardButton(text="Cancel", callback_data="clone:cancel")],
                    [InlineKeyboardButton(text="Back", callback_data="clone:backtoken")],
                    self._home_row(),
                ]
            ),
        )

    async def _show_confirmation(self, message: Message, state: dict[str, Any]) -> None:
        creators = ", ".join(str(value) for value in state["creator_ids"])
        await self._render(
            message,
            "Ready to create this isolated creator bot\n\n"
            f"Name: {state['label']}\n"
            f"Authorised creator IDs: {creators}\n\n"
            "The BotFather token is encrypted and is never displayed. Confirm to create and start this clone.",
            self._markup(
                [
                    [InlineKeyboardButton(text="Create clone", callback_data="clone:confirm")],
                    [InlineKeyboardButton(text="Cancel", callback_data="clone:cancel")],
                    self._home_row(),
                ]
            ),
        )

    async def clones(self, message: Message) -> None:
        if self._allowed_message(message):
            await self._show_home(message)

    async def callback(self, query: CallbackQuery) -> None:
        if not self._allowed_query(query):
            await query.answer("This control is only available to the main bot owner.", show_alert=True)
            return
        assert query.message is not None
        await query.answer()
        parts = (query.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        owner_id = query.from_user.id
        if action == "home":
            await self.repositories.clear_clone_setup_session(owner_id)
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await self._show_home(query.message, page)
        elif action == "new":
            await self._begin_creation(query.message, owner_id)
        elif action == "cancel":
            await self.repositories.clear_clone_setup_session(owner_id)
            await self._show_home(query.message)
        elif action == "exit":
            await self.repositories.clear_clone_setup_session(owner_id)
            await self._render(
                query.message,
                "Back to the iHarvester control room.",
                home_keyboard(include_clone_manager=True),
            )
        elif action == "backtoken":
            state = await self.repositories.clone_setup_session(owner_id)
            if not state or not state.get("label"):
                await self._begin_creation(query.message, owner_id)
                return
            await self.repositories.set_clone_setup_session(owner_id, {"step": "token", "label": state["label"]})
            await self._prompt_token(query.message, str(state["label"]))
        elif action == "confirm":
            await self._confirm_creation(query)
        elif action == "open" and len(parts) == 3:
            await self.repositories.clear_clone_setup_session(owner_id)
            await self._show_detail(query.message, parts[2])
        elif action == "access" and len(parts) == 3:
            await self._show_access(query.message, parts[2])
        elif action == "accessedit" and len(parts) == 3:
            clone = await self.repositories.get_bot_clone(parts[2])
            if not clone:
                await self._show_home(query.message)
                return
            await self.repositories.set_clone_setup_session(owner_id, {"step": "access", "clone_id": parts[2]})
            await self._render(
                query.message,
                f"Replace authorised creators - {clone['label']}\n\n"
                "Send the complete comma-separated list of Telegram user IDs that may operate this clone.\n\n"
                "Example: 123456789, 987654321",
                self._markup(
                    [
                        [InlineKeyboardButton(text="Cancel", callback_data=f"clone:access:{parts[2]}")],
                        self._home_row(),
                    ]
                ),
            )
        elif action in {"pause", "resume"} and len(parts) == 3:
            try:
                if action == "pause":
                    await self.manager.stop_clone(parts[2])
                else:
                    await self.manager.activate_clone(parts[2])
            except ValueError as error:
                await query.message.answer(
                    f"Could not {action} this clone. It remains paused.\n\n{error}\n\n"
                    "The detailed startup reason is now saved on the clone screen."
                )
                await self._show_detail(query.message, parts[2])
            else:
                await self._show_detail(query.message, parts[2])
        elif action != "noop":
            await self._show_home(query.message)

    async def _confirm_creation(self, query: CallbackQuery) -> None:
        state = await self.repositories.clone_setup_session(query.from_user.id)
        if not state or state.get("step") != "confirm":
            await self._begin_creation(query.message, query.from_user.id)
            return
        try:
            clone = await self.manager.create_clone_from_encrypted_token(
                label=str(state["label"]),
                token_ciphertext=str(state["token_ciphertext"]),
                creator_ids=frozenset(int(value) for value in state["creator_ids"]),
            )
        except ValueError as error:
            # The only normal correction here is an invalid/revoked token.
            # Remove that encrypted token immediately but preserve the label.
            await self.repositories.set_clone_setup_session(
                query.from_user.id,
                {"step": "token", "label": state["label"]},
            )
            await query.message.answer(f"Could not create this clone: {error}\n\nSend a valid BotFather token again.")
            await self._prompt_token(query.message, str(state["label"]))
            return
        except Exception:
            logger.exception("Could not create or start creator clone")
            await self.repositories.clear_clone_setup_session(query.from_user.id)
            await query.message.answer(
                "The clone could not be started right now. If Telegram accepted its token, it is saved as paused "
                "in Creator clones; you can resume it there after the deployment issue is resolved."
            )
            await self._show_home(query.message)
            return
        await self.repositories.clear_clone_setup_session(query.from_user.id)
        await query.message.answer(
            f"Clone ready: {clone['label']} (@{clone.get('bot_username') or clone['bot_user_id']}). "
            "It is running now."
        )
        await self._show_detail(query.message, str(clone["clone_id"]))

    async def session_message(self, message: Message) -> None:
        if not self._allowed_message(message) or not message.from_user:
            return
        state = await self.repositories.clone_setup_session(message.from_user.id)
        if not state:
            return
        value = (message.text or "").strip()
        if value.lower().startswith("/start"):
            await self.repositories.clear_clone_setup_session(message.from_user.id)
            await message.answer("Clone setup cancelled. Back to the iHarvester control room.", reply_markup=home_keyboard(include_clone_manager=True))
            return
        if not value:
            await message.answer("Please send the requested text, or use Cancel to leave setup.")
            return
        step = state.get("step")
        if step == "name":
            label = " ".join(value.split())
            if not 1 <= len(label) <= 80:
                await message.answer("Use a clone name between 1 and 80 characters.")
                return
            await self.repositories.set_clone_setup_session(message.from_user.id, {"step": "token", "label": label})
            await self._prompt_token(message, label)
            return
        if step == "token":
            try:
                token_ciphertext = self.manager._seal(value)
            except ValueError as error:
                await message.answer(f"Clone setup is unavailable: {error}")
                return
            finally:
                # Never leave a BotFather token in a normal chat message; the
                # encrypted setup state also expires after thirty minutes.
                try:
                    await message.delete()
                except Exception:
                    logger.warning("Could not remove creator clone token message")
            await self.repositories.set_clone_setup_session(
                message.from_user.id,
                {"step": "creators", "label": state["label"], "token_ciphertext": token_ciphertext},
            )
            await self._prompt_creators(message, str(state["label"]))
            return
        if step == "creators":
            try:
                creator_ids = sorted(_owner_ids(value))
            except ValueError as error:
                await message.answer(f"That creator list is not valid: {error}")
                return
            next_state = {**state, "step": "confirm", "creator_ids": creator_ids}
            await self.repositories.set_clone_setup_session(message.from_user.id, next_state)
            await self._show_confirmation(message, next_state)
            return
        if step == "access":
            try:
                clone_id = str(state["clone_id"])
                await self.manager.set_creators(clone_id, _owner_ids(value))
            except ValueError as error:
                await message.answer(f"Could not update creator access: {error}")
                return
            await self.repositories.clear_clone_setup_session(message.from_user.id)
            await message.answer("Authorised creators updated. The clone was safely refreshed.")
            await self._show_access(message, clone_id)
            return
        await self.repositories.clear_clone_setup_session(message.from_user.id)
        await self._show_home(message)
