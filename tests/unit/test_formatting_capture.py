from aiogram.types import LinkPreviewOptions

from app.telegram.formatting import _dump_telegram_model


def test_aiogram_default_link_preview_values_are_not_persisted() -> None:
    assert _dump_telegram_model(LinkPreviewOptions()) == {}


def test_explicit_link_preview_values_are_preserved() -> None:
    assert _dump_telegram_model(LinkPreviewOptions(is_disabled=True, show_above_text=True)) == {
        "is_disabled": True,
        "show_above_text": True,
    }
