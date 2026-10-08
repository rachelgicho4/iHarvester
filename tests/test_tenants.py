import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.telegram.keyboards import home_keyboard
from app.tenants import _owner_ids


def test_clone_creator_ids_require_positive_numeric_telegram_ids() -> None:
    assert _owner_ids("10, 20") == frozenset({10, 20})
    with pytest.raises(ValueError):
        _owner_ids("10,-20")
    with pytest.raises(ValueError):
        _owner_ids("0")


def test_clone_settings_key_is_optional_until_clone_management_is_used() -> None:
    settings = Settings(
        bot_token="1:" + "x" * 30,
        owner_user_ids="1",
        mongodb_uri="mongodb://example.invalid",
        run_mode="polling",
        clone_token_encryption_key=Fernet.generate_key().decode(),
    )
    assert settings.clone_database_prefix == "iharvester_clone"


def test_creator_clone_button_is_only_added_to_the_primary_home_keyboard() -> None:
    primary_controls = [button.callback_data for row in home_keyboard(include_clone_manager=True).inline_keyboard for button in row]
    clone_controls = [button.callback_data for row in home_keyboard().inline_keyboard for button in row]

    assert "clone:home" in primary_controls
    assert "clone:home" not in clone_controls
