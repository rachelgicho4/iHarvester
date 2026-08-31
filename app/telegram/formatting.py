"""Capture Telegram entities/file IDs so campaigns never depend on an owner source message remaining."""

from __future__ import annotations

from typing import Any

from aiogram.types import Message

from app.campaigns.models import Creative
from app.utils.ids import opaque_id


def _dump_entities(entities: list[Any] | None) -> list[dict[str, Any]]:
    return [entity.model_dump(mode="json", exclude_none=True) for entity in (entities or [])]


def capture_creative(message: Message) -> Creative:
    """Capture practical single-message types. Albums are completed by the owner flow as a group."""
    common: dict[str, Any] = {
        "id": opaque_id("var"), "caption": message.caption,
        "caption_entities": _dump_entities(message.caption_entities),
        "buttons": [], "button_layout": "AUTO",
    }
    if message.text:
        return Creative(
            id=common["id"], kind="TEXT", text=message.text, entities=_dump_entities(message.entities),
            link_preview_options=(message.link_preview_options.model_dump(mode="json", exclude_none=True)
                                  if message.link_preview_options else None),
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

