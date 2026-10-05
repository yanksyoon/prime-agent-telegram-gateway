"""E4T2: duplicate Telegram update_ids are acknowledged but NOT forwarded.

Telegram may redeliver an update with the same ``update_id`` (retry loops,
network glitches). The gateway must acknowledge it (return from the handler,
which python-telegram-bot reads as ack) but must NOT enqueue it for the
daemon. We keep the last processed ``update_id`` per ``chat_id`` in memory
and drop any update whose id is ``<=`` the last one seen.

The mock daemon records every ``send_message`` it is asked to perform, so the
assertion is direct: redelivering update_id=100 must not add a second daemon
call. A strictly NEWER update_id (101) for the same chat must still be
processed, proving dedup is bounded to already-observed ids rather than
blanket-blocking the chat.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_store import SessionManager

CHAT_ID = 777


class RecordingDaemon:
    """Daemon stand-in that records every text it is asked to process."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.create_session = AsyncMock(return_value="sess-1")

    async def send_message(self, session_id: str, text: str) -> str:
        self.calls.append(text)
        return "World"


class RecordingBot:
    """Context bot stand-in; no network, just records what was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)
    return handlers


def _update(update_id: int, text: str = "hello", chat_id: int = CHAT_ID):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text),
        update_id=update_id,  # E4T2: the Telegram update sequence number
    )


@pytest.mark.asyncio
async def test_duplicate_update_id_not_forwarded(_env, monkeypatch):
    """Redelivering the same update_id must not reach the daemon twice."""
    daemon = RecordingDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()

    manager = _env._get_queues()

    # First delivery of update_id=100: processed normally.
    await _env.handle_message(_update(update_id=100), SimpleNamespace(bot=bot))
    await asyncio.wait_for(manager.get_queue(str(CHAT_ID)).join(), timeout=2)

    # Resend the SAME update_id=100: acknowledged, but NOT forwarded.
    await _env.handle_message(_update(update_id=100), SimpleNamespace(bot=bot))
    await asyncio.wait_for(manager.get_queue(str(CHAT_ID)).join(), timeout=2)

    manager.cancel_worker(str(CHAT_ID))

    assert daemon.calls == ["hello"], "daemon must be called exactly once"
    assert bot.sent == [(CHAT_ID, "World")], "only one reply should be sent"


@pytest.mark.asyncio
async def test_newer_update_id_still_processed(_env, monkeypatch):
    """Dedup only drops already-observed ids; a newer id flows through."""
    daemon = RecordingDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()

    manager = _env._get_queues()

    await _env.handle_message(_update(update_id=100, text="one"), SimpleNamespace(bot=bot))
    await asyncio.wait_for(manager.get_queue(str(CHAT_ID)).join(), timeout=2)

    # A strictly newer id for the same chat must NOT be deduped.
    await _env.handle_message(_update(update_id=101, text="two"), SimpleNamespace(bot=bot))
    await asyncio.wait_for(manager.get_queue(str(CHAT_ID)).join(), timeout=2)

    manager.cancel_worker(str(CHAT_ID))

    assert daemon.calls == ["one", "two"], "both distinct ids must reach the daemon"
    assert bot.sent == [(CHAT_ID, "World"), (CHAT_ID, "World")]