"""Repair one obsolete Telegram handle in owned channel posts with MTProto.

This is deliberately an owner-operated recovery utility, never a Koyeb service
feature.  It uses one already-authorised human admin session at a time, scans
that account's own broadcast-channel directory, and writes an append-only
JSONL audit before it changes anything. It does not read MongoDB.

The default is a read-only, server-side search for the old handle.  Use
``--scan full`` only when messages may have the old handle solely inside an
inline-button URL, because that requires reading a channel's full history.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from telethon import TelegramClient, errors, types, utils
except ImportError as error:  # pragma: no cover - exercised by the operator
    raise SystemExit(
        "Telethon is required. Run: python -m pip install -e . -r requirements-mtproto-recovery.txt"
    ) from error


logger = logging.getLogger("iharvester.mtproto_handle_repair")
HANDLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


@dataclass(frozen=True)
class ChannelTarget:
    telegram_chat_id: int
    title: str
    username: str | None


@dataclass(frozen=True)
class TextReplacement:
    start: int
    end: int
    replacement: str


@dataclass
class RewrittenMessage:
    text: str
    entities: list[Any]
    markup: Any | None
    text_changes: int
    entity_url_changes: int
    button_url_changes: int

    @property
    def changed(self) -> bool:
        return bool(self.text_changes or self.entity_url_changes or self.button_url_changes)


class AuditWriter:
    """Small append-only audit that is safe to inspect while a run continues."""

    def __init__(self, directory: Path, session_label: str, *, applying: bool) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = directory / f"handle-repair-{session_label}-{stamp}-{'apply' if applying else 'dry-run'}.jsonl"
        self._file = self.path.open("x", encoding="utf-8", buffering=1)

    def write(self, event: str, **details: Any) -> None:
        self._file.write(
            json.dumps(
                {"at": datetime.now(UTC).isoformat(), "event": event, **details},
                default=str,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

    def close(self) -> None:
        self._file.close()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required. Keep it in the operator shell; do not commit it.")
    return value


def _handle(value: str, argument: str) -> str:
    result = value.strip().lstrip("@").removeprefix("https://t.me/").removeprefix("http://t.me/")
    if not HANDLE_PATTERN.fullmatch(result):
        raise argparse.ArgumentTypeError(f"{argument} must be a Telegram handle without @ or a URL.")
    return result


def _marker_pattern(old_handle: str) -> re.Pattern[str]:
    escaped = re.escape(old_handle)
    # The URL alternative intentionally also covers a bare t.me/ link.  It is
    # kept narrow so an unrelated use of similar words is never changed.
    return re.compile(
        rf"(?i)(?P<at>(?<![A-Za-z0-9_])@{escaped}(?![A-Za-z0-9_]))"
        rf"|(?P<link>(?<![A-Za-z0-9_])(?P<prefix>https?://t\.me/|t\.me/){escaped}(?![A-Za-z0-9_]))"
        rf"|(?P<tg>(?P<tg_prefix>tg://resolve\?domain=){escaped}(?![A-Za-z0-9_]))"
    )


def _replace_markers(value: str, pattern: re.Pattern[str], new_handle: str) -> tuple[str, list[TextReplacement]]:
    replacements: list[TextReplacement] = []
    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        if match.group("at") is not None:
            replacement = f"@{new_handle}"
        elif match.group("link") is not None:
            replacement = f"{match.group('prefix')}{new_handle}"
        else:
            replacement = f"{match.group('tg_prefix')}{new_handle}"
        pieces.extend((value[cursor : match.start()], replacement))
        replacements.append(TextReplacement(match.start(), match.end(), replacement))
        cursor = match.end()
    if not replacements:
        return value, []
    pieces.append(value[cursor:])
    return "".join(pieces), replacements


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _utf16_at(value: str, position: int) -> int:
    return _utf16_length(value[:position])


def _remap_entities(
    original_text: str,
    entities: Iterable[Any] | None,
    replacements: list[TextReplacement],
    pattern: re.Pattern[str],
    new_handle: str,
) -> tuple[list[Any], int]:
    """Adjust UTF-16 entity offsets after a replacement and repair text URLs.

    Telegram stores entity offsets in UTF-16 units, while Python indexes code
    points.  Mapping both entity boundaries through each replacement preserves
    bold text, mentions, links, spoilers and custom emoji around a changed
    handle (including messages that contain emoji before the link).
    """

    original = list(entities or [])
    if not original:
        return [], 0
    spans = [
        (_utf16_at(original_text, item.start), _utf16_at(original_text, item.end), _utf16_length(item.replacement))
        for item in replacements
    ]

    def map_boundary(position: int, *, end: bool) -> int:
        delta = 0
        for start, finish, replacement_length in spans:
            # An entity touching the left boundary remains before the changed
            # handle; one touching the right boundary remains after it.
            if position <= start:
                return position + delta
            if position >= finish:
                delta += replacement_length - (finish - start)
                continue
            # An entity overlaps a replacement.  Its start maps to the start
            # of the new handle, while its end maps to the end of that handle.
            return start + delta + (replacement_length if end else 0)
        return position + delta

    rewritten: list[Any] = []
    changed_urls = 0
    for entity in original:
        clone = copy.copy(entity)
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if isinstance(offset, int) and isinstance(length, int) and offset >= 0 and length >= 0:
            mapped_start = map_boundary(offset, end=False)
            mapped_end = map_boundary(offset + length, end=True)
            clone.offset = mapped_start
            clone.length = max(0, mapped_end - mapped_start)
        url = getattr(entity, "url", None)
        if isinstance(url, str):
            rewritten_url, url_replacements = _replace_markers(url, pattern, new_handle)
            if url_replacements:
                clone.url = rewritten_url
                changed_urls += len(url_replacements)
        rewritten.append(clone)
    return rewritten, changed_urls


def _rewrite_markup(markup: Any | None, pattern: re.Pattern[str], new_handle: str) -> tuple[Any | None, int]:
    if markup is None:
        return None, 0
    rewritten = copy.deepcopy(markup)
    changed = 0
    for row in getattr(rewritten, "rows", []) or []:
        for button in getattr(row, "buttons", []) or []:
            url = getattr(button, "url", None)
            if not isinstance(url, str):
                continue
            replacement, matches = _replace_markers(url, pattern, new_handle)
            if matches:
                button.url = replacement
                changed += len(matches)
    return rewritten, changed


def _rewrite_message(message: Any, pattern: re.Pattern[str], new_handle: str) -> RewrittenMessage:
    original_text = getattr(message, "raw_text", None)
    if not isinstance(original_text, str):
        original_text = getattr(message, "message", "") or ""
    text, replacements = _replace_markers(original_text, pattern, new_handle)
    entities, entity_url_changes = _remap_entities(
        original_text,
        getattr(message, "entities", None),
        replacements,
        pattern,
        new_handle,
    )
    markup, button_url_changes = _rewrite_markup(getattr(message, "reply_markup", None), pattern, new_handle)
    return RewrittenMessage(
        text=text,
        entities=entities,
        markup=markup,
        text_changes=len(replacements),
        entity_url_changes=entity_url_changes,
        button_url_changes=button_url_changes,
    )


async def _account_broadcast_channels(client: TelegramClient) -> tuple[list[tuple[ChannelTarget, Any]], int]:
    """Return every broadcast channel visible to this authorised account.

    Telegram's own dialog directory is deliberately the only channel source
    for this utility. That lets it repair legacy posts in healthy channels
    which iHarvester has never indexed, and avoids stale database membership
    data assigning a channel to the wrong account.
    """

    dialogs = await client.get_dialogs(limit=None)
    channels: list[tuple[ChannelTarget, Any]] = []
    broadcast_dialogs = 0
    for dialog in dialogs:
        entity = getattr(dialog, "entity", None)
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            continue
        broadcast_dialogs += 1
        chat_id = utils.get_peer_id(entity)
        access_hash = getattr(entity, "access_hash", None)
        if not isinstance(access_hash, int):
            continue
        username = getattr(entity, "username", None)
        channels.append(
            (
                ChannelTarget(
                    telegram_chat_id=chat_id,
                    title=" ".join(str(getattr(entity, "title", "") or chat_id).split()),
                    username=username.strip() if isinstance(username, str) and username.strip() else None,
                ),
                types.InputChannel(entity.id, access_hash),
            )
        )
    channels.sort(key=lambda item: (item[0].title.casefold(), item[0].telegram_chat_id))
    return channels, broadcast_dialogs


async def _can_edit_messages(client: TelegramClient, channel: Any) -> bool:
    permissions = await client.get_permissions(channel, "me")
    return bool(
        getattr(permissions, "is_creator", False)
        or (getattr(permissions, "is_admin", False) and getattr(permissions, "edit_messages", False))
    )


async def _candidate_messages(client: TelegramClient, channel: Any, args: argparse.Namespace):
    if args.scan == "search":
        async for message in client.iter_messages(channel, search=args.old_handle, limit=None, wait_time=args.history_wait):
            yield message
        return
    limit = args.history_limit or None
    async for message in client.iter_messages(channel, limit=limit, wait_time=args.history_wait):
        yield message


def _message_details(target: ChannelTarget, message: Any, rewrite: RewrittenMessage) -> dict[str, Any]:
    return {
        "channel_id": target.telegram_chat_id,
        "channel_title": target.title,
        "channel_username": target.username,
        "message_id": int(message.id),
        "message_date": getattr(message, "date", None),
        "has_media": bool(getattr(message, "media", None)),
        "text_replacements": rewrite.text_changes,
        "text_link_url_replacements": rewrite.entity_url_changes,
        "button_url_replacements": rewrite.button_url_changes,
    }


async def _edit_message(client: TelegramClient, channel: Any, message: Any, rewrite: RewrittenMessage) -> None:
    # Passing raw entities prevents Telethon's Markdown parser from changing
    # existing formatting.  A raw ReplyInlineMarkup is accepted for user
    # sessions and preserves callback buttons alongside rewritten URL buttons.
    await client.edit_message(
        channel,
        message.id,
        rewrite.text,
        parse_mode=None,
        formatting_entities=rewrite.entities,
        link_preview=bool(getattr(message, "web_preview", None)),
        buttons=rewrite.markup,
    )


async def _edit_with_flood_wait(client: TelegramClient, channel: Any, message: Any, rewrite: RewrittenMessage) -> None:
    while True:
        try:
            await _edit_message(client, channel, message, rewrite)
            return
        except errors.FloodWaitError as error:
            wait_for = max(1, int(error.seconds))
            logger.warning("Telegram requested a %ss pause before edit; obeying it.", wait_for)
            await asyncio.sleep(wait_for)


async def run(args: argparse.Namespace) -> int:
    api_id = int(_required_env("TELEGRAM_API_ID"))
    api_hash = _required_env("TELEGRAM_API_HASH")
    session = Path(args.session).expanduser().resolve()
    if not session.with_suffix(".session").exists() and not session.exists():
        raise SystemExit(f"No authorised session was found at {session} (or {session}.session). Run the QR helper first.")
    pattern = _marker_pattern(args.old_handle)
    client = TelegramClient(
        str(session),
        api_id,
        api_hash,
        receive_updates=False,
        flood_sleep_threshold=120,
        request_retries=5,
        entity_cache_limit=10_000,
    )
    audit: AuditWriter | None = None
    counts = {
        "channels": 0,
        "account_broadcast_dialogs": 0,
        "channels_selected": 0,
        "no_edit_right": 0,
        "messages_scanned": 0,
        "candidates": 0,
        "edited": 0,
        "failed": 0,
        "skipped_forwarded": 0,
    }
    candidate_limit_reached = False
    try:
        await client.start()
        identity = await client.get_me()
        if not identity or identity.bot:
            raise SystemExit("This tool requires an already-authorised human admin session, never the bot identity.")
        session_label = re.sub(r"[^A-Za-z0-9_-]", "-", session.stem)[:48] or str(identity.id)
        audit = AuditWriter(Path(args.audit_dir).expanduser().resolve(), session_label, applying=args.apply)
        audit.write(
            "run_started",
            account_id=identity.id,
            account_username=identity.username,
            old_handle=args.old_handle,
            new_handle=args.new_handle,
            scan=args.scan,
            apply=args.apply,
        )
        logger.info(
            "Authenticated as @%s. Building targets from this account's Telegram channel directory; mode=%s%s.",
            identity.username or identity.id,
            args.scan,
            " APPLY" if args.apply else " DRY RUN",
        )
        logger.info("Loading this account's broadcast-channel directory first; no messages are changed at this stage.")
        channel_pairs, broadcast_dialog_count = await _account_broadcast_channels(client)
        if args.limit_channels is not None:
            channel_pairs = channel_pairs[: args.limit_channels]
        counts["account_broadcast_dialogs"] = broadcast_dialog_count
        counts["channels_selected"] = len(channel_pairs)
        audit.write(
            "account_channel_directory",
            account_broadcast_dialogs=broadcast_dialog_count,
            usable_broadcast_channels=len(channel_pairs),
        )
        logger.info(
            "This account has %s broadcast dialogs; %s are selected for this run.",
            broadcast_dialog_count,
            len(channel_pairs),
        )

        for index, (target, channel) in enumerate(channel_pairs, start=1):
            try:
                if not await _can_edit_messages(client, channel):
                    counts["no_edit_right"] += 1
                    audit.write("channel_skipped_no_edit_right", channel_id=target.telegram_chat_id, channel_title=target.title)
                    continue
            except errors.RPCError as error:
                counts["failed"] += 1
                audit.write(
                    "channel_permission_check_failed",
                    channel_id=target.telegram_chat_id,
                    channel_title=target.title,
                    reason=f"{type(error).__name__}: {error}"[:300],
                )
                continue

            counts["channels"] += 1
            seen_ids: set[int] = set()
            try:
                async for message in _candidate_messages(client, channel, args):
                    # A server-side search can return the same result through
                    # pagination edges.  Full scans cannot, but de-duplicate
                    # both modes before any write just in case.
                    if not getattr(message, "id", None) or message.id in seen_ids:
                        continue
                    seen_ids.add(message.id)
                    counts["messages_scanned"] += 1
                    rewrite = _rewrite_message(message, pattern, args.new_handle)
                    if not rewrite.changed:
                        continue
                    details = _message_details(target, message, rewrite)
                    counts["candidates"] += 1
                    stop_after_this_candidate = bool(
                        args.max_candidates is not None and counts["candidates"] >= args.max_candidates
                    )
                    if getattr(message, "fwd_from", None):
                        counts["skipped_forwarded"] += 1
                        audit.write("candidate_skipped_forwarded", **details)
                        if stop_after_this_candidate:
                            candidate_limit_reached = True
                            break
                        continue
                    if not args.apply:
                        audit.write("would_edit", **details)
                        if stop_after_this_candidate:
                            candidate_limit_reached = True
                            break
                        continue
                    try:
                        await _edit_with_flood_wait(client, channel, message, rewrite)
                    except errors.MessageNotModifiedError:
                        # Another admin fixed it after the scan; this is a
                        # success rather than an error requiring a retry.
                        audit.write("already_repaired", **details)
                    except Exception as error:  # Telegram exposes many granular RPC error classes.
                        counts["failed"] += 1
                        audit.write("edit_failed", **details, error=f"{type(error).__name__}: {error}"[:500])
                        logger.warning("Could not edit %s/%s message %s: %s", index, len(channel_pairs), message.id, type(error).__name__)
                    else:
                        counts["edited"] += 1
                        audit.write("edited", **details)
                    await asyncio.sleep(1 / args.edit_rps)
                    if stop_after_this_candidate:
                        candidate_limit_reached = True
                        break
            except errors.FloodWaitError as error:
                # Reading channel history can also be throttled.  Stop this
                # channel cleanly, wait as Telegram asks, then move on; the
                # next dry run or pass can pick up anything not reached.
                wait_for = max(1, int(error.seconds))
                audit.write("channel_scan_flood_wait", channel_id=target.telegram_chat_id, channel_title=target.title, seconds=wait_for)
                logger.warning("History scan flood wait (%ss) at channel %s; obeying it.", wait_for, target.telegram_chat_id)
                await asyncio.sleep(wait_for)
            except errors.RPCError as error:
                counts["failed"] += 1
                audit.write(
                    "channel_scan_failed",
                    channel_id=target.telegram_chat_id,
                    channel_title=target.title,
                    error=f"{type(error).__name__}: {error}"[:500],
                )
            if candidate_limit_reached:
                audit.write("candidate_limit_reached", max_candidates=args.max_candidates)
                logger.info("Reached the requested %s matching-message limit.", args.max_candidates)
                break
            if index % 25 == 0 or index == len(channel_pairs):
                logger.info(
                    "Progress %s/%s channels; %s candidate messages, %s edits, %s failures.",
                    index,
                    len(channel_pairs),
                    counts["candidates"],
                    counts["edited"],
                    counts["failed"],
                )

        audit.write("run_completed", **counts)
        logger.info("Completed. %s", ", ".join(f"{key}={value}" for key, value in counts.items()))
        logger.info("Audit: %s", audit.path)
        return 0 if not counts["failed"] else 2
    finally:
        if audit:
            audit.close()
        await client.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and repair an obsolete Telegram handle in owned channel posts.")
    parser.add_argument("--session", required=True, help="Path to one already-authorised human MTProto session (without secrets).")
    parser.add_argument("--old-handle", type=lambda value: _handle(value, "--old-handle"), default="i_BOXTV")
    parser.add_argument("--new-handle", type=lambda value: _handle(value, "--new-handle"), default="i_BOX_TV")
    parser.add_argument("--scan", choices=("search", "full"), default="search", help="Search is fast; full also finds old URLs inside inline buttons.")
    parser.add_argument("--history-limit", type=int, help="For --scan full, inspect at most N newest messages per channel (omit for all history).")
    parser.add_argument("--history-wait", type=float, default=1.0, help="Minimum delay Telethon uses between history/search pages (default: 1 second).")
    parser.add_argument("--limit-channels", type=int, help="Pilot only the first N channels from this account's Telegram directory.")
    parser.add_argument("--max-candidates", type=int, help="Stop after N matching messages; use this for a meaningful write pilot.")
    parser.add_argument("--audit-dir", default="work/handle-repair-audit", help="Local-only directory for JSONL audit files.")
    parser.add_argument("--edit-rps", type=float, default=0.8, help="Maximum actual edits per second (default: 0.8).")
    parser.add_argument("--apply", action="store_true", help="Perform edits. Omit for an audit-only dry run.")
    parser.add_argument("--confirm-new-handle", help="Required with --apply; must exactly equal --new-handle.")
    args = parser.parse_args()
    if args.limit_channels is not None and args.limit_channels < 1:
        parser.error("--limit-channels must be at least 1")
    if args.history_limit is not None and args.history_limit < 1:
        parser.error("--history-limit must be at least 1")
    if args.max_candidates is not None and args.max_candidates < 1:
        parser.error("--max-candidates must be at least 1")
    if args.history_wait < 0.5:
        parser.error("--history-wait must be at least 0.5 seconds")
    if not 0 < args.edit_rps <= 2:
        parser.error("--edit-rps must be greater than 0 and no more than 2")
    if args.apply and args.confirm_new_handle != args.new_handle:
        parser.error("--apply requires --confirm-new-handle with the exact replacement handle")
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
