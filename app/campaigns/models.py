"""Portable campaign-domain models; persisted documents are explicit dictionaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ChannelStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    UNAVAILABLE = "UNAVAILABLE"
    INACTIVE_MANUAL = "INACTIVE_MANUAL"


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    ARCHIVED = "ARCHIVED"


class CampaignMode(StrEnum):
    STANDARD = "STANDARD"
    ROTATE = "ROTATE"
    MIX_ROTATE = "MIX_ROTATE"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRY_WAIT = "RETRY_WAIT"
    SENT = "SENT"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    UNKNOWN_SEND_STATE = "UNKNOWN_SEND_STATE"
    CLEANED = "CLEANED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANCELLED = "CANCELLED"


class Button(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=64)
    url: HttpUrl
    style: Literal["default", "primary", "success", "danger"] = "default"
    row: int = Field(default=0, ge=0, le=20)
    position: int = Field(default=0, ge=0, le=20)


class Creative(BaseModel):
    """A canonical, replayable capture. file IDs remain valid independently of source messages."""

    id: str
    kind: Literal[
        "TEXT", "PHOTO", "VIDEO", "ANIMATION", "DOCUMENT", "AUDIO", "VOICE", "VIDEO_NOTE",
        "STICKER", "MEDIA_GROUP", "RICH_MESSAGE",
    ]
    text: str | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    caption: str | None = None
    caption_entities: list[dict[str, Any]] = Field(default_factory=list)
    media: list[dict[str, Any]] = Field(default_factory=list)
    link_preview_options: dict[str, Any] | None = None
    caption_above_media: bool | None = None
    rich_payload: dict[str, Any] | None = None
    buttons: list[Button] = Field(default_factory=list)
    button_layout: Literal["AUTO", "1", "2", "3", "CUSTOM"] = "AUTO"

    @model_validator(mode="after")
    def replayable(self) -> Creative:
        if self.kind == "TEXT" and not self.text:
            raise ValueError("text creative needs text")
        if self.kind not in {"TEXT", "RICH_MESSAGE"} and not self.media:
            raise ValueError("media creative needs a replayable file_id payload")
        if self.kind == "RICH_MESSAGE" and not self.rich_payload:
            raise ValueError("rich message creative needs rich_payload")
        return self


class Destination(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    telegram_chat_id: int | None = None
    username: str | None = None
    raw_url: HttpUrl | None = None
    campaign_invite_link: str | None = None
    bot_is_admin: bool = False
    join_tracking_enabled: bool = False

    @model_validator(mode="after")
    def has_destination(self) -> Destination:
        if not any((self.telegram_chat_id, self.username, self.raw_url, self.campaign_invite_link)):
            raise ValueError("destination needs a chat ID, username, or direct URL")
        return self


class Schedule(BaseModel):
    start_at_utc: datetime
    end_at_utc: datetime
    repost_interval_seconds: int | None = Field(default=None, ge=60)
    # Exact elapsed points after cycle 0. This supports schedules such as
    # "repost after 1 day, then after 4 days" without a repeating interval.
    repost_offsets_seconds: list[int] | None = None
    delete_on_repost: bool = True
    delete_on_end: bool = True
    owner_timezone: str = "UTC"

    @model_validator(mode="after")
    def valid_repost_plan(self) -> Schedule:
        if self.repost_interval_seconds and self.repost_offsets_seconds:
            raise ValueError("choose either a repeating repost interval or specific repost times")
        if self.repost_offsets_seconds:
            if any(value < 60 for value in self.repost_offsets_seconds):
                raise ValueError("specific repost times must be at least one minute after launch")
            if self.repost_offsets_seconds != sorted(set(self.repost_offsets_seconds)):
                raise ValueError("specific repost times must be unique and in ascending order")
        return self
