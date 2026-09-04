from __future__ import annotations

import hmac
import logging

from aiogram.types import Update
from fastapi import HTTPException, Request, Response
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)


async def _notify_database_unavailable(runtime: object, update: Update) -> None:
    """Give callback users a useful answer while Telegram retries the update."""
    callback = update.callback_query
    if not callback:
        return
    try:
        await runtime.bot.answer_callback_query(
            callback.id,
            text="The campaign database is temporarily unreachable. Your action will retry automatically; tap again in a moment if needed.",
            show_alert=True,
            cache_time=0,
        )
    except Exception:
        # The webhook must still return a retryable response even if Telegram
        # has already expired this cosmetic callback acknowledgement.
        logger.warning("Could not notify owner about unavailable database", extra={"update_id": update.update_id})


def install_routes(app: object) -> None:
    # Deferred import keeps the tiny HTTP layer independent from application construction.
    @app.get("/")
    async def index() -> dict[str, str]:
        """Safe public ping target for a platform or external uptime monitor."""
        return {"status": "ok", "service": "iHarvester"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        runtime = request.app.state.runtime
        if not runtime.ready:
            raise HTTPException(status_code=503, detail="startup checks incomplete")
        try:
            await runtime.database.ping()
        except Exception as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        return {"status": "ready"}

    @app.post("/telegram/webhook/{path_secret}")
    async def telegram_webhook(path_secret: str, request: Request) -> Response:
        runtime = request.app.state.runtime
        settings = runtime.settings
        if settings.run_mode != "webhook" or not hmac.compare_digest(path_secret, settings.webhook_path_secret or ""):
            raise HTTPException(status_code=404, detail="not found")
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(provided, settings.webhook_secret_token or ""):
            raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")
        update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
        try:
            registered = await runtime.repositories.register_update(update.update_id)
        except PyMongoError:
            logger.warning("Deferring webhook update because MongoDB is unavailable", extra={"update_id": update.update_id})
            await _notify_database_unavailable(runtime, update)
            # A non-2xx response makes Telegram retain and retry this update;
            # returning 200 here would silently lose a Launch tap.
            return Response(status_code=503, headers={"Retry-After": "5"})
        if not registered:
            return Response(status_code=200)
        try:
            await runtime.dispatcher.feed_update(runtime.bot, update)
        except Exception:
            await runtime.repositories.unregister_update(update.update_id)
            raise
        return Response(status_code=200)
