from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def repair_module(monkeypatch: pytest.MonkeyPatch):
    """Load the workstation script's pure rewrite functions without Telethon."""
    telethon = ModuleType("telethon")
    telethon.TelegramClient = object
    telethon.errors = SimpleNamespace()
    telethon.types = SimpleNamespace()
    telethon.utils = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    path = Path(__file__).parents[1] / "scripts" / "repair_channel_handle_mtproto.py"
    spec = importlib.util.spec_from_file_location("handle_repair_test_module", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_marker_pass_repairs_old_handle_and_adds_a_visible_period(repair_module) -> None:
    message = SimpleNamespace(
        raw_text="JOIN https://t.me/i_BOXTV 🆕",
        entities=[],
        reply_markup=None,
    )

    rewrite = repair_module._rewrite_message(
        message,
        repair_module._marker_pattern("i_BOXTV"),
        "i_BOX_TV",
        append_new_marker_period=True,
    )

    assert rewrite.text == "JOIN https://t.me/i_BOX_TV 🆕."
    assert rewrite.text_changes == 1
    assert rewrite.marker_period_changes == 1


def test_marker_pass_leaves_already_renamed_posts_and_entities_untouched(repair_module) -> None:
    text = "JOIN https://t.me/i_BOX_TV 🆕 next"
    next_offset = len("JOIN https://t.me/i_BOX_TV 🆕 ".encode("utf-16-le")) // 2
    entity = SimpleNamespace(offset=next_offset, length=4, url=None)
    message = SimpleNamespace(raw_text=text, entities=[entity], reply_markup=None)

    rewrite = repair_module._rewrite_message(
        message,
        repair_module._marker_pattern("i_BOXTV"),
        "i_BOX_TV",
        append_new_marker_period=True,
    )

    assert rewrite.text == text
    assert rewrite.text_changes == 0
    assert rewrite.marker_period_changes == 0
    assert rewrite.entities[0].offset == next_offset


def test_marker_pass_does_not_punctuate_unrelated_new_markers(repair_module) -> None:
    message = SimpleNamespace(raw_text="A different update 🆕", entities=[], reply_markup=None)

    rewrite = repair_module._rewrite_message(
        message,
        repair_module._marker_pattern("i_BOXTV"),
        "i_BOX_TV",
        append_new_marker_period=True,
    )

    assert not rewrite.changed
