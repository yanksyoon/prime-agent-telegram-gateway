"""QA close-out: full-chain integration trace + grace properties.

This is an INDEPENDENT end-to-end trace, not a re-run of the per-epic tests.
It strings every stage together in one authorized-user flow with mocked
Telegram (respx over the real api.telegram.org endpoints in the fake bot) and
a mocked daemon:

  text update -> ACL allow -> enqueue (chat 777) -> per-chat worker ->
  SessionManager.get_or_create_session (daemon create_session first time) ->
  daemon.send_message -> _deliver_reply -> send_message(stripped text) +
  send_document(existing file) -> queue drained and worker stopped.

It also asserts the two grace properties in the SAME harness:
  (a) an unauthorized user is blocked by @require_acl BEFORE any daemon RPC
      or Telegram reply (the RCE-critical boundary), and
  (b) a duplicate update_id is acked but never reaches the daemon.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import gateway.handlers as handlers
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"
DOC_URL = "https://api.telegram.org/botTEST_TOKEN/sendDocument"


class FakeTelegramBot:
    """Records every outbound send the gateway asked for (over real URLs)."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        async with httpx.AsyncClient() as c:
            await c.post(BOT_URL, json={"chat_id": chat_id, "text": text})
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id: int, document: str, **kw) -> None:
        async with httpx.AsyncClient() as c:
            await c.post(DOC_URL, json={"chat_id": chat_id, "document": document})
        self.documents.append((chat_id, document))


class RecordingDaemon:
    """Daemon stand-in that records every RPC and nuance of ordering."""

    def __init__(self, reply_file: str | None = None) -> None:
        self.created = 0
        self.messages: list[tuple[str, str]] = []
        self._reply_file = reply_file or "/tmp/fake.csv"
        self.create_session = AsyncMock(side_effect=self._create)

    async def _create(self, *a, **k) -> str:
        self.created += 1
        return f"sess-{self.created}"

    async def send_message(self, session_id: str, text: str) -> str:
        self.messages.append((session_id, text))
        # Reply references the caller's tmp_path file so the exists guard passes.
        return f"Done. File saved to {self._reply_file}"


def _update(*, user_id=111, chat_id=777, text="Hello", update_id=100):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text),
        update_id=update_id,
    )


def _bot_env(monkeypatch, reply_file: str | None = None):
    daemon = RecordingDaemon(reply_file=reply_file)
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(handlers, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(handlers, "_get_sessions", lambda: store)
    return daemon, store


async def _drain(chat_id: str) -> None:
    manager = handlers._get_queues()
    queue = manager.get_queue(chat_id)
    await asyncio.wait_for(queue.join(), timeout=2)
    worker = manager._workers.pop(chat_id, None)
    if worker is not None:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_integration_full_flow(monkeypatch, tmp_path):
    """Authorized text -> session(cache miss) -> queue -> daemon -> reply+file."""
    importlib_reload(monkeypatch)
    target = tmp_path / "fake.csv"
    target.write_text("a,b\n1,2\n")

    daemon, _ = _bot_env(monkeypatch, reply_file=str(target))
    bot = FakeTelegramBot()
    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    router.post(DOC_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    with router:
        await handlers.handle_message(_update(), SimpleNamespace(bot=bot))
        await _drain("777:0")

    # session created once via daemon RPC (cache miss) and persisted
    assert daemon.created == 1
    # daemon got exactly the user's text, once, on the resolved session
    assert daemon.messages == [("sess-1", "Hello")]
    # reply split: text without trailing path + separate document upload
    assert bot.messages == [(777, "Done. File saved to")], bot.messages
    assert bot.documents == [(777, str(target))], bot.documents


@pytest.mark.asyncio
async def test_acl_blocks_unauthorized_before_any_rpc(monkeypatch):
    """An unauthorized user never reaches session/daemon/Telegram."""
    importlib_reload(monkeypatch)
    daemon, _ = _bot_env(monkeypatch)
    bot = FakeTelegramBot()

    with respx.mock(assert_all_called=False):
        await handlers.handle_message(
            _update(user_id=999, text="rm -rf /"), SimpleNamespace(bot=bot)
        )
        await _drain("777:0")  # nothing queued, so nothing to drain

    # The RCE-critical property: nothing at all happened downstream.
    assert daemon.created == 0
    assert daemon.messages == []
    assert bot.messages == []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_duplicate_update_id_acked_not_forwarded(monkeypatch):
    """A redelivered update_id is acknowledged but the daemon sees it once."""
    importlib_reload(monkeypatch)
    daemon, _ = _bot_env(monkeypatch)
    bot = FakeTelegramBot()

    with respx.mock(assert_all_called=False):
        await handlers.handle_message(_update(update_id=100), SimpleNamespace(bot=bot))
        await _drain("777:0")
        await handlers.handle_message(_update(update_id=100), SimpleNamespace(bot=bot))
        await _drain("777:0")

    assert daemon.messages == [("sess-1", "Hello")], "daemon must see it exactly once"


def importlib_reload(monkeypatch):
    import importlib

    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    importlib.reload(handlers)
    return handlers