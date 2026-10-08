"""E3T3: a referenced path that does not exist on disk is NOT uploaded.

If the daemon's reply ends in an absolute path with a known extension but that
path does not exist locally, the gateway must NOT call send_document. Instead
it logs a warning and sends the whole reply as plain text, so the user still
gets the message and the gateway never sends a file it cannot actually read.

handle_message only enqueues (E4T1); the reply is delivered in the per-chat
worker, so the test drains the queue inside the respx context (as
test_text_roundtrip.py does) before asserting.

TDD: red before implementation - the delivery step never logs the missing-path
warning today, so the caplog assertion fails.
"""
import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from gateway.handlers import handle_message
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"
DOC_URL = "https://api.telegram.org/botTEST_TOKEN/sendDocument"


async def _stop_queue(_env, chat_id: str) -> None:
    """Drain the per-chat worker (E4T1) and stop it cleanly."""
    manager = _env._get_queues()
    queue = manager.get_queue(chat_id)
    await asyncio.wait_for(queue.join(), timeout=2)
    worker = manager._workers.pop(chat_id, None)
    if worker is not None:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_documents: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(BOT_URL, json={"chat_id": chat_id, "text": text})
        self.sent_messages.append((chat_id, text))

    async def send_document(self, chat_id: int, document: str, **kwargs) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(DOC_URL, json={"chat_id": chat_id, "document": document})
        self.sent_documents.append((chat_id, document))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)
    return handlers


def _inject(_env, monkeypatch, reply):
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value=reply),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    return daemon


def _update(text: str = "make a file"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text=text),
    )


def _mock_telegram():
    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    router.post(DOC_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"file_id": "F"}})
    )
    return router


@pytest.mark.asyncio
async def test_missing_file_not_uploaded_and_warns(_env, monkeypatch, caplog):
    """A path that does not exist must not be sent as a document."""
    missing = os.path.join("/tmp", f"definitely_missing_{os.getpid()}.csv")
    assert not os.path.exists(missing)
    reply = f"I looked but could not find: {missing}"

    _inject(_env, monkeypatch, reply)
    bot = FakeTelegramBot()

    with caplog.at_level(logging.WARNING, logger="gateway.handlers"):
        with _mock_telegram():
            await _env.handle_message(_update(), SimpleNamespace(bot=bot))
            await _stop_queue(_env, "777:0")

    # send_document is NEVER called when the path is missing.
    assert bot.sent_documents == []

    # The full reply still reaches the user as plain text.
    assert bot.sent_messages == [(777, reply)]

    # A warning naming the missing path was logged.
    assert any(
        "does not exist" in rec.getMessage() and missing in rec.getMessage()
        for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]