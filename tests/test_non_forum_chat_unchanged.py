"""Non-forum chats (DM / normal group, no topic) behave exactly as before.

A message with NO ``message_thread_id`` (None) still works: the conversation
key becomes ``f"{chat_id}:0"`` and outbound replies carry NO ``message_thread_id``
at all, so normal DMs are byte-for-byte unchanged by the per-topic feature.

TDD: red before implementation - ``_chat_key`` does not exist yet.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_store import SessionManager


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    import importlib

    import gateway.handlers as handlers

    importlib.reload(handlers)
    return handlers


def _plain_update():  # message WITHOUT a message_thread_id attribute
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text="hello"),
    )


@pytest.mark.asyncio
async def test_non_forum_key_is_chat_colon_zero(_env):
    """A non-forum update normalises to key ``f"{chat_id}:0"``."""
    assert _env._chat_key(_plain_update()) == "777:0"


@pytest.mark.asyncio
async def test_non_forum_reply_has_no_thread_kwarg(_env, monkeypatch):
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value="World"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()
    manager = _env._get_queues()

    await _env.handle_message(_plain_update(), SimpleNamespace(bot=bot))
    await asyncio.wait_for(manager.get_queue("777:0").join(), timeout=2)
    manager.cancel_worker("777:0")

    daemon.send_message.assert_awaited_once_with("sess-1", "hello")
    assert bot.sent[0][0] == 777
    assert bot.sent[0][1] == "World"
    # A non-forum chat gets no message_thread_id: normal DM unchanged.
    assert "message_thread_id" not in bot.sent[0][2]