"""E3T1: a slash token embedded in normal text must NOT be intercepted.

``"I like /refine"`` is ordinary prose, not a command: the leading character of
the message is ``I``, not ``/``. The gateway must forward the whole string to
the daemon via ``send_message`` as raw text and must NOT call
``send_control_command``. A version of the string that merely *contains* a slash
command elsewhere (coffee2 style) is also left alone.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from gateway.handlers import handle_message
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"


class FakeTelegramBot:
    """Minimal stand-in for python-telegram-bot's ``context.bot``."""

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


def _update(text: str):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text=text),
    )


@pytest.mark.asyncio
async def test_embedded_slash_is_plain_text(_env, monkeypatch):
    """``/refine`` inside prose goes to the daemon as raw text, not control."""
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_control_command=AsyncMock(return_value="unused"),
        send_message=AsyncMock(return_value="Got it: I like /refine"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = FakeTelegramBot()

    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    captured: list[dict] = []
    with router:
        result = await _env.handle_message(
            _update("I like /refine"), SimpleNamespace(bot=bot)
        )
        captured.append(json.loads(router.calls.last.request.content))

    # 1. The message went via the normal plain-text path with its full body.
    daemon.send_message.assert_awaited_once_with("sess-1", "I like /refine")
    # 2. It was NOT routed as a control command.
    daemon.send_control_command.assert_not_called()

    # 3. Telegram relayed the daemon's raw reply verbatim.
    assert captured, "Telegram sendMessage must be called once"
    body = captured[0]
    assert body["text"] == "Got it: I like /refine"
    assert body["chat_id"] == 777
    assert bot.sent == [(777, "Got it: I like /refine")]

    assert result is None