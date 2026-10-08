"""Outbound replies land in the originating TOPIC (message_thread_id).

In a forum, the bot's answer must be sent with the same ``message_thread_id``
as the inbound message, or it lands in the general topic. This holds for BOTH
the plain ``send_message`` reply AND the trailing-file ``send_document`` path
(E3T3).

TDD: red before implementation - today outbound sends carry chat_id only, so
``message_thread_id`` is absent from the recorded calls.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_store import SessionManager

THREAD = 42


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []  # (chat_id, text, kwargs)
        self.documents: list[tuple[int, str, dict]] = []  # (chat_id, path, kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))

    async def send_document(self, chat_id, document, **kwargs):
        self.documents.append((chat_id, document, kwargs))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    import importlib

    import gateway.handlers as handlers

    importlib.reload(handlers)
    return handlers


def _update(text: str, thread: int | None = THREAD):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text=text, message_thread_id=thread),
    )


def _inject(_env, monkeypatch, reply: str):
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value=reply),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    return daemon


async def _stop_queue(_env, key: str) -> None:
    manager = _env._get_queues()
    queue = manager.get_queue(key)
    await asyncio.wait_for(queue.join(), timeout=2)
    manager.cancel_worker(key)


@pytest.mark.asyncio
async def test_plain_reply_carries_topic_thread(_env, monkeypatch):
    _inject(_env, monkeypatch, "World")
    bot = RecordingBot()

    await _env.handle_message(_update("hi"), SimpleNamespace(bot=bot))
    await _stop_queue(_env, f"777:{THREAD}")

    assert bot.messages, "a reply must have been sent"
    chat_id, text, kwargs = bot.messages[0]
    assert (chat_id, text) == (777, "World")
    assert kwargs.get("message_thread_id") == THREAD


@pytest.mark.asyncio
async def test_document_upload_carries_topic_thread(_env, monkeypatch, tmp_path):
    target = tmp_path / "report.csv"
    target.write_text("a,b\n1,2\n")
    daemon = _inject(_env, monkeypatch, f"Done. File saved to {target}")
    bot = RecordingBot()

    await _env.handle_message(_update("go"), SimpleNamespace(bot=bot))
    await _stop_queue(_env, f"777:{THREAD}")

    # The document upload landed in the topic AND the text prefix did too.
    assert bot.documents == [(777, str(target), {"message_thread_id": THREAD})]
    assert bot.messages[0][2].get("message_thread_id") == THREAD
    assert bot.messages[0][1] == "Done. File saved to"