"""E2T2: the core text roundtrip (Hello -> World -> World).

An authorized user sends "Hello". The gateway:
  1. resolves the daemon session for the chat via SessionManager,
  2. sends "Hello" to the daemon mock and gets back "World",
  3. calls Telegram's sendMessage with "World".

Telegram's HTTP API is mocked with ``respx`` so no real network call is ever
made; the daemon is an ``AsyncMock``. This is the first test that wires the
whole handler end to end, proving the ticket's behaviour exactly: the daemon
receives "Hello"" and Telegram's sendMessage receives "World".
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from gateway.handlers import AGENT_BUSY_TEXT, handle_message
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"


class FakeTelegramBot:
    """Minimal stand-in for python-telegram-bot's ``context.bot``.

    send_message performs the exact HTTPS POST to Telegram's ``sendMessage``
    endpoint, so ``respx`` can intercept and mock the wire call. This is the
    real network boundary the gateway crosses; we only substitute the bot
    object because python-telegram-bot is not installed in the dev env.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(BOT_URL, json={"chat_id": chat_id, "text": text})
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)  # re-bind the @require_acl allowlist from env
    return handlers


def _inject(_env, monkeypatch):
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value="World"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    return daemon


def _hello_update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text="Hello"),
    )


@pytest.mark.asyncio
async def test_hello_roundtrips_to_world(_env, monkeypatch):
    daemon = _inject(_env, monkeypatch)
    bot = FakeTelegramBot()
    context = SimpleNamespace(bot=bot)

    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )
    )
    captured: list[dict] = []
    with router:
        result = await _env.handle_message(_hello_update(), context)
        # Capture inside the respx context: calls are reset on context exit.
        captured.append(json.loads(router.calls.last.request.content))

    # 1. The daemon received the user's "Hello" for the resolved session.
    daemon.send_message.assert_awaited_once_with("sess-1", "Hello")

    # 2. The daemon's "World" reached Telegram's sendMessage endpoint.
    assert captured, "Telegram sendMessage must be called once"
    body = captured[0]
    assert body["text"] == "World"
    assert body["chat_id"] == 777

    # 3. The bot recorded the reply as delivered.
    assert bot.sent == [(777, "World")]

    assert result is None


@pytest.mark.asyncio
async def test_daemon_error_sends_polite_busy_reply(_env, monkeypatch):
    """Feedback loop: a failing daemon must not crash the update."""
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(side_effect=ConnectionError("daemon down")),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = FakeTelegramBot()

    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    captured: list[dict] = []
    with router:
        result = await _env.handle_message(_hello_update(), SimpleNamespace(bot=bot))
        captured.append(json.loads(router.calls.last.request.content))

    daemon.send_message.assert_awaited_once_with("sess-1", "Hello")
    assert captured, "a busy message must still be sent to Telegram"
    body = captured[0]
    assert body["text"] == AGENT_BUSY_TEXT
    assert body["chat_id"] == 777
    assert result is None