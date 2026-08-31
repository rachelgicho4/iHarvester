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
        ("PHOTO", message.photo[-1].file_id if message.photo else None, "photo"),
        ("VIDEO", message.video.file_id if message.video else None, "video"),
        ("ANIMATION", message.animation.file_id if message.animation else None, "animation"),
        ("DOCUMENT", message.document.file_id if message.document else None, "document"),
        ("AUDIO", message.audio.file_id if message.audio else None, "audio"),
        ("VOICE", message.voice.file_id if message.voice else None, "voice"),
        ("VIDEO_NOTE", message.video_note.file_id if message.video_note else None, "video_note"),
        ("STICKER", message.sticker.file_id if message.sticker else None, "sticker"),
    ]
    for kind, file_id, field in kinds:
        if file_id:
            return Creative(**common, kind=kind, media=[{"field": field, "file_id": file_id}])
    raise ValueError("This Telegram content type cannot be used as a campaign creative.")
