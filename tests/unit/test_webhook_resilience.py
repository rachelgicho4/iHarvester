from types import SimpleNamespace

from aiogram.exceptions import TelegramNetworkError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from app.web.routes import install_routes


class UnavailableRepositories:
    async def register_update(self, _update_id: int) -> bool:
        raise ServerSelectionTimeoutError("MongoDB unavailable")


class CallbackBot:
    def __init__(self) -> None:
        self.answers: list[dict[str, object]] = []

    async def answer_callback_query(self, callback_query_id: str, **kwargs: object) -> None:
        self.answers.append({"callback_query_id": callback_query_id, **kwargs})


class RegisteredRepositories:
    def __init__(self) -> None:
        self.unregistered: list[int] = []

    async def register_update(self, _update_id: int) -> bool:
        return True

    async def unregister_update(self, update_id: int) -> None:
        self.unregistered.append(update_id)


class ResponseLostDispatcher:
    async def feed_update(self, _bot: object, _update: object) -> None:
        raise TelegramNetworkError(SimpleNamespace(), "request timeout")


def test_database_outage_keeps_callback_update_retryable_and_informs_owner() -> None:
    app = FastAPI()
    bot = CallbackBot()
    app.state.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            run_mode="webhook",
            webhook_path_secret="route-secret",
            webhook_secret_token="header-secret",
        ),
        repositories=UnavailableRepositories(),
        bot=bot,
    )
    install_routes(app)
    payload = {
        "update_id": 101,
        "callback_query": {
            "id": "callback-101",
            "from": {"id": 7, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "instance",
            "data": "c:cmp:launch",
            "message": {
                "message_id": 4,
                "date": 0,
                "chat": {"id": 7, "type": "private", "first_name": "Owner"},
            },
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/telegram/webhook/route-secret",
            headers={"X-Telegram-Bot-Api-Secret-Token": "header-secret"},
            json=payload,
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert bot.answers == [
        {
            "callback_query_id": "callback-101",
            "text": "The campaign database is temporarily unreachable. Your action will retry automatically; tap again in a moment if needed.",
            "show_alert": True,
            "cache_time": 0,
        }
    ]


def test_network_timeout_after_dispatch_is_acknowledged_without_replay() -> None:
    app = FastAPI()
    repositories = RegisteredRepositories()
    app.state.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            run_mode="webhook",
            webhook_path_secret="route-secret",
            webhook_secret_token="header-secret",
        ),
        repositories=repositories,
        bot=object(),
        dispatcher=ResponseLostDispatcher(),
    )
    install_routes(app)
    payload = {
        "update_id": 102,
        "message": {
            "message_id": 5,
            "date": 0,
            "from": {"id": 7, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 7, "type": "private", "first_name": "Owner"},
            "text": "/start",
            "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/telegram/webhook/route-secret",
            headers={"X-Telegram-Bot-Api-Secret-Token": "header-secret"},
            json=payload,
        )

    assert response.status_code == 200
    assert repositories.unregistered == []
