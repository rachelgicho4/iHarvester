from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import uvicorn
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.campaigns.scheduler import Scheduler
from app.campaigns.service import CampaignService
from app.config import Settings
from app.db.client import Database
from app.db.indexes import ensure_indexes
from app.db.leases import LeaseManager
from app.db.repositories import Repositories
from app.delivery.rate_limit import AsyncTokenBucket
from app.delivery.worker import DeliveryWorker
from app.telegram.handlers_admin_updates import ChannelAdminHandlers
from app.telegram.handlers_join_events import JoinEventHandlers
from app.telegram.handlers_owner import OwnerHandlers
from app.telegram.raw_api import RawTelegramAPI
from app.telegram.sender import TelegramSender
from app.utils.ids import opaque_id
from app.web.routes import install_routes


@dataclass
class Runtime:
    settings: Settings
    database: Database
    repositories: Repositories
    bot: Bot
    dispatcher: Dispatcher
    sender: TelegramSender
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[object]] = field(default_factory=list)
    ready: bool = False


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.mongodb_uri, settings.mongodb_db_name)
        repositories = Repositories(database)
        bot = Bot(settings.bot_token)
        raw_api = RawTelegramAPI(settings.bot_token, settings.telegram_request_timeout_seconds)
        sender = TelegramSender(bot, raw_api)
        dispatcher = Dispatcher()
        service = CampaignService(repositories, settings.broadcast_send_rps)
        dispatcher.include_router(ChannelAdminHandlers(repositories).router)
        dispatcher.include_router(JoinEventHandlers(repositories).router)
        dispatcher.include_router(OwnerHandlers(
            owner_ids=settings.owner_ids, repositories=repositories, campaigns=service, sender=sender,
        ).router)
        runtime = Runtime(settings, database, repositories, bot, dispatcher, sender)
        app.state.runtime = runtime
        try:
            await database.ping()
            await ensure_indexes(database)
            await bot.get_me()
            allowed_updates = dispatcher.resolve_used_update_types()
            if settings.run_mode == "webhook":
                await bot.set_webhook(
                    settings.webhook_url, secret_token=settings.webhook_secret_token, allowed_updates=allowed_updates,
                    drop_pending_updates=False,
                )
            else:
                await bot.delete_webhook(drop_pending_updates=False)
            instance_id = opaque_id("instance")
            scheduler = Scheduler(
                instance_id=instance_id, repositories=repositories, lease_manager=LeaseManager(database),
                campaign_service=service, lease_seconds=settings.scheduler_lease_seconds,
                tick_seconds=settings.scheduler_tick_seconds,
            )
            send_limiter = AsyncTokenBucket(settings.broadcast_send_rps)
            mutation_limiter = AsyncTokenBucket(settings.broadcast_global_api_rps)
            runtime.tasks.append(asyncio.create_task(scheduler.run(runtime.stopping), name="scheduler"))
            for number in range(settings.broadcast_workers):
                worker = DeliveryWorker(
                    worker_id=f"{instance_id}-{number}", repositories=repositories, sender=sender,
                    send_limiter=send_limiter, mutation_limiter=mutation_limiter,
                    delivery_lease_seconds=settings.delivery_lease_seconds,
                    max_transient_attempts=settings.max_transient_attempts,
                )
                runtime.tasks.append(asyncio.create_task(worker.run(runtime.stopping), name=f"delivery-{number}"))
            if settings.run_mode == "polling":
                runtime.tasks.append(asyncio.create_task(
                    dispatcher.start_polling(bot, allowed_updates=allowed_updates, handle_signals=False), name="polling"
                ))
            runtime.ready = True
            yield
        finally:
            runtime.ready = False
            runtime.stopping.set()
            for task in runtime.tasks:
                task.cancel()
            if runtime.tasks:
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
            await raw_api.close()
            await bot.session.close()
            await database.close()

    app = FastAPI(title="iHarvester", version="0.1.0", lifespan=lifespan)
    install_routes(app)
    return app


app = create_app()


def run() -> None:
    # uvicorn installs its own SIGTERM handling; one application worker is intentional.
    _ = signal.SIGTERM
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), workers=1)


if __name__ == "__main__":
    run()
