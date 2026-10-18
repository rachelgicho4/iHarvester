#!/usr/bin/env python3
"""Record verified view counters for live iHarvester campaign posts.

Run this on the private recovery VPS, never in the Koyeb bot service.  It uses
already-authorised human Telegram sessions only to read the exact live post IDs
from MongoDB.  ``messages.getMessagesViews`` is always called with
``increment=False``: sampling does not create an extra view or alter a post.

The script stores just one view total on each existing delivery and a compact
roll-up on each campaign.  It deliberately creates no time-series collection,
so repeated samples cannot cause another MongoDB growth problem.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pymongo import MongoClient

try:
    from telethon import TelegramClient, errors, functions, types
except ImportError as error:  # pragma: no cover - operator environment only
    raise SystemExit(
        "Telethon is required. Run: python -m pip install -e . -r requirements-mtproto-recovery.txt"
    ) from error


logger = logging.getLogger("iharvester.view_tracker")


class ChannelUnavailableError(RuntimeError):
    """A session has no usable access hash for this exact channel."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _resolve_channel(client: TelegramClient, channel_id: int, username: str | None) -> Any:
    """Resolve a stored channel ID without searching any message history."""
    expected_channel_id = -channel_id - 1_000_000_000_000
    try:
        entity = await client.get_input_entity(channel_id)
    except ValueError:
        if not username:
            raise ChannelUnavailableError("session has no cached access hash") from None
        entity = await client.get_input_entity(username)
    if getattr(entity, "channel_id", None) != expected_channel_id:
        raise ChannelUnavailableError("resolved entity does not match the stored channel")
    access_hash = getattr(entity, "access_hash", None)
    if not isinstance(access_hash, int):
        raise ChannelUnavailableError("resolved channel has no access hash")
    return types.InputChannel(expected_channel_id, access_hash)


async def _open_clients(session_paths: list[Path], api_id: int, api_hash: str) -> tuple[list[TelegramClient], list[set[int]]]:
    clients: list[TelegramClient] = []
    accessible_channel_ids: list[set[int]] = []
    for session in session_paths:
        client = TelegramClient(
            str(session),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
            request_retries=2,
        )
        await client.start()
        identity = await client.get_me()
        if not identity or identity.bot:
            await client.disconnect()
            raise SystemExit(f"{session.name} is not an authorised human Telegram session.")
        # This reads the account directory only, so Telethon can obtain access
        # hashes for channels already available to that account. It does not
        # enumerate or inspect channel message history.
        dialogs = await client.get_dialogs(limit=None)
        channel_ids = {
            -1_000_000_000_000 - int(dialog.entity.id)
            for dialog in dialogs
            if isinstance(dialog.entity, types.Channel)
        }
        logger.info("%s: loaded %s accessible dialogs (%s channels)", session.name, len(dialogs), len(channel_ids))
        clients.append(client)
        accessible_channel_ids.append(channel_ids)
    return clients, accessible_channel_ids


def _targets(database: Any, campaign_id: str | None, limit: int | None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"current_message_ids.0": {"$exists": True}}
    if campaign_id:
        match["campaign_id"] = campaign_id
    rows = list(
        database.campaign_channel_state.aggregate(
            [
                {"$match": match},
                {
                    "$lookup": {
                        "from": "campaigns",
                        "localField": "campaign_id",
                        "foreignField": "campaign_id",
                        "as": "campaign",
                    }
                },
                {"$unwind": "$campaign"},
                {"$match": {"campaign.status": {"$in": ["ACTIVE", "PAUSED", "ENDING", "ARCHIVED"]}}},
                {
                    "$lookup": {
                        "from": "channels",
                        "localField": "channel_id",
                        "foreignField": "telegram_chat_id",
                        "as": "channel",
                    }
                },
                {"$unwind": {"path": "$channel", "preserveNullAndEmptyArrays": True}},
                {
                    "$project": {
                        "_id": 0,
                        "campaign_id": 1,
                        "channel_id": 1,
                        "current_cycle_number": 1,
                        "current_message_ids": 1,
                        "username": "$channel.username",
                    }
                },
                {"$sort": {"campaign_id": 1, "channel_id": 1}},
            ]
        )
    )
    filtered: list[dict[str, Any]] = []
    for row in rows:
        try:
            message_ids = [int(value) for value in row.get("current_message_ids", []) if int(value) > 0]
            channel_id = int(row["channel_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not message_ids:
            continue
        row["channel_id"] = channel_id
        row["message_id"] = message_ids[0]
        row["current_cycle_number"] = int(row.get("current_cycle_number") or 0)
        username = row.get("username")
        row["username"] = username.strip() if isinstance(username, str) and username.strip() else None
        filtered.append(row)
        if limit is not None and len(filtered) >= limit:
            break
    return filtered


def _update_campaign_rollup(database: Any, campaign_id: str, now: datetime) -> dict[str, Any]:
    """Persist a bounded aggregate that survives delivery-history archival."""
    rows = list(
        database.deliveries.find(
            {
                "campaign_id": campaign_id,
                "operation": {"$ne": "CLEANUP"},
                "status": "SENT",
                "view_count": {"$type": "number"},
            },
            {"view_count": 1, "view_observed_at": 1, "sent_at": 1},
        )
    )
    total_views = sum(max(0, int(row.get("view_count") or 0)) for row in rows)
    timestamps = [_as_utc(row.get("view_observed_at")) for row in rows]
    timestamps = [value for value in timestamps if value]
    placement_hours = 0.0
    hourly_samples = 0
    for row in rows:
        sent_at = _as_utc(row.get("sent_at"))
        observed_at = _as_utc(row.get("view_observed_at"))
        if not sent_at or not observed_at:
            continue
        hours = max(0.0, (observed_at - sent_at).total_seconds() / 3600)
        # A view velocity is only meaningful after a full observed hour. The
        # post's raw view count still appears in the live client page sooner.
        if hours >= 1:
            placement_hours += hours
            hourly_samples += 1
    sampled = len(rows)
    tracking = {
        "total_views": total_views,
        "sampled_placements": sampled,
        "views_per_placement": round(total_views / sampled) if sampled else 0,
        "placement_hours": round(placement_hours, 3),
        "views_per_placement_hour": round(total_views / placement_hours, 3) if placement_hours and hourly_samples else 0,
        "first_sample_at": min(timestamps) if timestamps else None,
        "last_sample_at": max(timestamps) if timestamps else None,
        "updated_at": now,
    }
    database.campaigns.update_one({"campaign_id": campaign_id}, {"$set": {"view_tracking": tracking, "updated_at": now}})
    return tracking


async def run(args: argparse.Namespace) -> int:
    mongo_uri = _required_env("MONGODB_URI")
    database_name = os.environ.get("MONGODB_DB_NAME", "telegram_campaign_orchestrator")
    api_id = int(_required_env("TELEGRAM_API_ID"))
    api_hash = _required_env("TELEGRAM_API_HASH")
    session_paths = [Path(value).expanduser().resolve() for value in args.session]
    for path in session_paths:
        if not path.with_suffix(".session").is_file() and not path.is_file():
            raise SystemExit(f"Session file not found: {path} (expected a Telethon .session file)")
    mongo = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000, socketTimeoutMS=60_000)
    database = mongo[database_name]
    clients: list[TelegramClient] = []
    try:
        mongo.admin.command("ping")
        targets = _targets(database, args.campaign, args.limit)
        if not targets:
            logger.info("No live campaign posts matched this run.")
            return 0
        logger.info("Sampling %s exact live campaign posts", len(targets))
        if args.dry_run:
            for target in targets[:20]:
                logger.info("Would sample campaign=%s channel=%s message=%s", target["campaign_id"], target["channel_id"], target["message_id"])
            return 0
        clients, accessible_channel_ids = await _open_clients(session_paths, api_id, api_hash)
        entities: dict[tuple[int, int], Any] = {}
        cooldown_until: dict[int, float] = defaultdict(float)
        changed_campaigns: set[str] = set()
        sampled = unavailable = failed = 0
        loop = asyncio.get_running_loop()
        for offset, target in enumerate(targets):
            # A channel can be reached through only one of the three authorised
            # owner/admin accounts. Prefer the account that already listed the
            # exact channel in its dialog directory, then rotate the order so
            # one account is not needlessly asked to serve every public post.
            known = [
                index
                for index, channel_ids in enumerate(accessible_channel_ids)
                if target["channel_id"] in channel_ids
            ]
            fallback = [index for index in range(len(clients)) if index not in known]
            candidates = sorted(
                known + fallback,
                key=lambda index: (
                    cooldown_until[index] > loop.time(),
                    (index - offset) % len(clients),
                ),
            )
            success = False
            for client_index in candidates:
                pause = cooldown_until[client_index] - loop.time()
                if pause > 0:
                    continue
                client = clients[client_index]
                cache_key = (client_index, target["channel_id"])
                try:
                    entity = entities.get(cache_key)
                    if entity is None:
                        entity = await _resolve_channel(client, target["channel_id"], target.get("username"))
                        entities[cache_key] = entity
                    response = await client(
                        functions.messages.GetMessagesViewsRequest(
                            peer=entity,
                            id=[target["message_id"]],
                            increment=False,
                        )
                    )
                    counters = list(getattr(response, "views", []) or [])
                    if not counters:
                        raise RuntimeError("Telegram returned no view counter for the exact message")
                    views = max(0, int(getattr(counters[0], "views", 0) or 0))
                    now = datetime.now(UTC)
                    database.deliveries.update_one(
                        {
                            "campaign_id": target["campaign_id"],
                            "cycle_number": target["current_cycle_number"],
                            "channel_id": target["channel_id"],
                            "operation": {"$ne": "CLEANUP"},
                            "status": "SENT",
                        },
                        {
                            "$set": {
                                "view_count": views,
                                "view_observed_at": now,
                                "view_tracker_session": client_index + 1,
                                "updated_at": now,
                            }
                        },
                    )
                    changed_campaigns.add(target["campaign_id"])
                    sampled += 1
                    success = True
                    break
                except errors.FloodWaitError as error:
                    cooldown_until[client_index] = loop.time() + int(error.seconds) + 2
                    logger.warning("Session %s cooling down for %ss", client_index + 1, error.seconds)
                except ChannelUnavailableError:
                    continue
                except (errors.ChannelPrivateError, errors.ChatAdminRequiredError):
                    continue
                except Exception as error:  # A single post must not stop other campaigns.
                    failed += 1
                    logger.warning("Could not sample channel %s message %s: %s", target["channel_id"], target["message_id"], error)
            if not success:
                unavailable += 1
            if args.delay and offset < len(targets) - 1:
                await asyncio.sleep(args.delay)
        now = datetime.now(UTC)
        for campaign_id in changed_campaigns:
            summary = _update_campaign_rollup(database, campaign_id, now)
            logger.info("%s: %s views from %s sampled placements", campaign_id, summary["total_views"], summary["sampled_placements"])
        logger.info("Complete: %s sampled, %s unavailable, %s failures", sampled, unavailable, failed)
        return 0
    finally:
        for client in clients:
            await client.disconnect()
        mongo.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read exact live campaign post view counters using authorised human sessions.")
    parser.add_argument("--session", action="append", required=True, help="Authorised Telethon session path; repeat for each owner/admin account.")
    parser.add_argument("--campaign", help="Limit the sampler to one iHarvester campaign ID.")
    parser.add_argument("--limit", type=int, help="Maximum live posts to sample in this run.")
    parser.add_argument("--delay", type=float, default=0.35, help="Minimum seconds between Telegram view reads (default: 0.35).")
    parser.add_argument("--dry-run", action="store_true", help="List exact stored targets without connecting to Telegram or writing MongoDB.")
    return parser.parse_args()


if __name__ == "__main__":  # pragma: no cover - operator entry point
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
