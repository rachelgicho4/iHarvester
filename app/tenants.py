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

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, Update
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
from app.telegram.raw_api import RawTelegramAPI
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id

if TYPE_CHECKING:
    from app.main import Runtime

logger = logging.getLogger(__name__)


def _clone_id() -> str:
    return f"clone_{secrets.token_hex(8)}"


def _webhook_secret(clone_id: str) -> str:
    return f"clone_{clone_id}_{secrets.token_urlsafe(24)}"


def _owner_ids(value: str) -> frozenset[int]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values or any(not item.isdigit() for item in values):
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

    async def start_all(self) -> None:
        if not self.enabled:
            return
        for clone in await self.main_runtime.repositories.active_bot_clones():
            try:
                await self.start_clone(clone)
            except Exception:
                logger.exception("Could not start clone", extra={"clone_id": clone.get("clone_id")})

    async def create_clone(self, *, label: str, token: str, creator_ids: frozenset[int]) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("Set CLONE_TOKEN_ENCRYPTION_KEY on the main deployment before adding a clone.")
        label = " ".join(label.split())
        if not 1 <= len(label) <= 80:
            raise ValueError("clone label must be 1 to 80 characters")
        verification_bot = Bot(token)
        try:
            bot_user = await verification_bot.get_me()
        finally:
            await verification_bot.session.close()
        if not bot_user.is_bot:
            raise ValueError("that token did not authenticate as a Telegram bot")
        clone_id = _clone_id()
        document: dict[str, Any] = {
            "clone_id": clone_id,
            "label": label,
            "active": True,
            "token_ciphertext": self._seal(token),
            "creator_user_ids": sorted(creator_ids),
            "bot_user_id": bot_user.id,
            "bot_username": bot_user.username,
            "mongodb_db_name": f"{self.main_runtime.settings.clone_database_prefix}_{clone_id}",
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
            await self.start_clone(document)
        except Exception:
            await self.main_runtime.repositories.update_bot_clone(clone_id, active=False)
            raise
        return document

    async def start_clone(self, clone: dict[str, Any]) -> Runtime:
        clone_id = str(clone["clone_id"])
        if clone_id in self._children:
            return self._children[clone_id]
        settings = self._clone_settings(clone)
        runtime = await _start_child_runtime(settings)
        self._children[clone_id] = runtime
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
            await self.main_runtime.repositories.update_bot_clone(clone_id, active=False)

    async def activate_clone(self, clone_id: str) -> None:
        clone = await self.main_runtime.repositories.get_bot_clone(clone_id)
        if not clone:
            raise ValueError("clone not found")
        await self.main_runtime.repositories.update_bot_clone(clone_id, active=True)
        await self.start_clone(clone)

    async def set_creators(self, clone_id: str, creator_ids: frozenset[int]) -> None:
        clone = await self.main_runtime.repositories.get_bot_clone(clone_id)
        if not clone:
            raise ValueError("clone not found")
        was_active = bool(clone.get("active"))
        await self.stop_clone(clone_id, deactivate=False)
        await self.main_runtime.repositories.update_bot_clone(clone_id, creator_user_ids=sorted(creator_ids), active=was_active)
        if was_active:
            updated = await self.main_runtime.repositories.get_bot_clone(clone_id)
            if updated:
                await self.start_clone(updated)

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
