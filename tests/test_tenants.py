import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app import tenants
from app.config import Settings
from app.telegram.keyboards import home_keyboard
from app.tenants import CloneManager, _clone_delivery_worker, _owner_ids


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
    assert settings.clone_database_prefix == "iharvester"
    assert len(f"{settings.clone_database_prefix}_clone_0123456789abcdef") <= 38


def test_clone_database_prefix_rejects_non_ascii_values() -> None:
    with pytest.raises(ValueError, match="CLONE_DATABASE_PREFIX"):
        Settings(
            bot_token="1:" + "x" * 30,
            owner_user_ids="1",
            mongodb_uri="mongodb://example.invalid",
            clone_database_prefix="creator_kenya_😀",
        )


def test_overlong_legacy_clone_database_name_is_repaired_before_start() -> None:
    class RepositoriesStub:
        def __init__(self) -> None:
            self.updated: dict[str, object] | None = None

        async def update_bot_clone(self, clone_id: str, **fields: object) -> None:
            self.updated = {"clone_id": clone_id, **fields}

    settings = Settings(
        bot_token="1:" + "x" * 30,
        owner_user_ids="1",
        mongodb_uri="mongodb://example.invalid",
        run_mode="polling",
        clone_token_encryption_key=Fernet.generate_key().decode(),
    )
    repositories = RepositoriesStub()
    manager = CloneManager(SimpleNamespace(settings=settings, repositories=repositories))
    clone = {
        "clone_id": "clone_0123456789abcdef",
        "mongodb_db_name": "iharvester_clone_clone_0123456789abcdef",
    }

    repaired = asyncio.run(manager._repair_overlong_database_name(clone))

    assert repaired["mongodb_db_name"] == "iharvester_clone_0123456789abcdef"
    assert len(repaired["mongodb_db_name"]) <= 38
    assert repositories.updated == {
        "clone_id": "clone_0123456789abcdef",
        "mongodb_db_name": "iharvester_clone_0123456789abcdef",
    }


def test_creator_clone_button_is_only_added_to_the_primary_home_keyboard() -> None:
    primary_controls = [button.callback_data for row in home_keyboard(include_clone_manager=True).inline_keyboard for button in row]
    clone_controls = [button.callback_data for row in home_keyboard().inline_keyboard for button in row]

    assert "clone:home" in primary_controls
    assert "clone:home" not in clone_controls


def test_clone_worker_uses_the_current_delivery_worker_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    class WorkerStub:
        def __init__(self, **kwargs: object) -> None:
            received.update(kwargs)

    monkeypatch.setattr(tenants, "DeliveryWorker", WorkerStub)
    settings = SimpleNamespace(delivery_lease_seconds=90, max_transient_attempts=3)

    _clone_delivery_worker(
        worker_id="clone-worker",
        repositories=SimpleNamespace(),
        sender=SimpleNamespace(),
        send_limiter=SimpleNamespace(),
        mutation_limiter=SimpleNamespace(),
        settings=settings,
    )

    assert received["max_transient_attempts"] == 3
    assert "max_attempts" not in received
