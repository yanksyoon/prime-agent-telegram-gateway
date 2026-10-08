"""E3T3: outbound file path detection & document upload.

If the daemon's reply ends in an absolute path with a known extension
(.csv/.pdf/.txt/.json) and that path exists on disk, the gateway splits the
delivery: it sends the text (minus the trailing path) with send_message AND
uploads the file with send_document.

handle_message now only enqueues (E4T1); the daemon call and Telegram delivery
happen in the per-chat worker, so this test drains the queue (``_stop_queue``)
inside the respx context before asserting, mirroring test_text_roundtrip.py.

TDD: this test must FAIL before the implementation exists - the delivery step
sends the whole reply as a single send_message and never calls send_document,
so the stripped-text and document assertions are red.
"""
import asyncio
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
    """Minimal stand-in for python-telegram-bot's ``context.bot``.

    Both send_message and send_document hit the real Telegram HTTPS endpoints,
    which respx intercepts. Calls are recorded so the test can assert exactly
    what the gateway asked the bot to do.
    """

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
    importlib.reload(handlers)  # re-bind the @require_acl allowlist from env
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
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"file_id": "FILE", "file_unique_id": "U"}}
        )
    )
    return router


@pytest.mark.asyncio
async def test_file_detect_and_send(_env, monkeypatch, tmp_path):
    """A reply ending in an existing file path sends text AND the document."""
    target = tmp_path / "fake.csv"
    target.write_text("col1,col2\n1,2\n")
    reply = f"Done. File saved to {target}"

    _inject(_env, monkeypatch, reply)
    bot = FakeTelegramBot()

    with _mock_telegram():
        await _env.handle_message(_update(), SimpleNamespace(bot=bot))
        await _stop_queue(_env, "777:0")

    # The text is the reply with the trailing path stripped.
    expected_text = reply[: -len(str(target))].rstrip()
    assert bot.sent_messages == [(777, expected_text)]

    # And the existing file was uploaded separately.
    assert bot.sent_documents == [(777, str(target))]


@pytest.mark.asyncio
async def test_reply_not_ending_in_path_sends_text_only(_env, monkeypatch):
    """A plain reply with no trailing file path never triggers send_document."""
    _inject(_env, monkeypatch, "All done, nothing to upload.")
    bot = FakeTelegramBot()

    with _mock_telegram():
        await _env.handle_message(_update(), SimpleNamespace(bot=bot))
        await _stop_queue(_env, "777:0")

    assert bot.sent_messages == [(777, "All done, nothing to upload.")]
    assert bot.sent_documents == []