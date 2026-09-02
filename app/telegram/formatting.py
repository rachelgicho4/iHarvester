"""Capture Telegram entities/file IDs so campaigns never depend on an owner source message remaining."""

from __future__ import annotations

from typing import Any

from aiogram.client.default import Default
from aiogram.types import Message

from app.campaigns.models import Creative
from app.utils.ids import opaque_id

_OMIT = object()


def _strip_aiogram_defaults(value: Any) -> Any:
    """Remove aiogram's send-method defaults; they are not Telegram message data."""
    if isinstance(value, Default):
        return _OMIT
    if isinstance(value, dict):
        return {key: cleaned for key, item in value.items() if (cleaned := _strip_aiogram_defaults(item)) is not _OMIT}
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _strip_aiogram_defaults(item)) is not _OMIT]
    return value


def _dump_telegram_model(model: Any) -> dict[str, Any]:
    cleaned = _strip_aiogram_defaults(model.model_dump(mode="python", exclude_none=True))
    return cleaned if isinstance(cleaned, dict) else {}


def _dump_entities(entities: list[Any] | None) -> list[dict[str, Any]]:
    return [_dump_telegram_model(entity) for entity in (entities or [])]


def capture_creative(message: Message) -> Creative:
    """Capture practical single-message types. Albums are completed by the owner flow as a group."""
    common: dict[str, Any] = {
        "id": opaque_id("var"),
        "caption": message.caption,
        "caption_entities": _dump_entities(message.caption_entities),
        "buttons": [],
        "button_layout": "AUTO",
    }
    if message.text:
        preview = _dump_telegram_model(message.link_preview_options) if message.link_preview_options else None
        return Creative(
            id=common["id"],
            kind="TEXT",
            text=message.text,
            entities=_dump_entities(message.entities),
            link_preview_options=preview or None,
        )
    kinds = [
        ("PHOTO", message.photo[-1] if message.photo else None, "photo"),
        ("VIDEO", message.video, "video"),
        ("ANIMATION", message.animation, "animation"),
        ("DOCUMENT", message.document, "document"),
        ("AUDIO", message.audio, "audio"),
        ("VOICE", message.voice, "voice"),
        ("VIDEO_NOTE", message.video_note, "video_note"),
        ("STICKER", message.sticker, "sticker"),
    ]
    for kind, media, field in kinds:
        if media:
            replayable_media = {"field": field, "file_id": media.file_id}
            for attribute in ("mime_type", "file_name"):
                if value := getattr(media, attribute, None):
                    replayable_media[attribute] = value
            return Creative(**common, kind=kind, media=[replayable_media])
    raise ValueError("This Telegram content type cannot be used as a campaign creative.")
