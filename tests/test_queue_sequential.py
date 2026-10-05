"""E4T1: per-chat messages are processed sequentially, never in parallel.

Prime Agent's daemon is strictly turn-based per session: two messages for the
same chat_id processed at once will corrupt the session state. Three messages
delivered for the SAME chat_id "at the same instant" (via asyncio.gather) must
still reach the daemon one after another: each daemon.send_message window must
fully finish before the next one begins (no overlap).

The mock daemon records its start/end monotonic timestamps and takes a small
asyncio sleep to stand in for the daemon's turn time. Without the per-chat
queue, gathering three handle_message calls would overlap those windows; the
per-chat worker must force them strictly sequential.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.queue_manager import ChatQueueManager
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
CHAT_ID = 777


class RecordingDaemon:
    """Daemon stand-in whose send_message records its own time window."""

    def __init__(self) -> None:
        self.windows: list[tuple[float, float]] = []
        self.create_session = AsyncMock(return_value="sess-1")

    async def send_message(self, session_id: str, text: str) -> str:
        start = time.monotonic()
        await asyncio.sleep(0.02)  # daemon turn time: overlaps if concurrent
        end = time.monotonic()
        self.windows.append((start, end))
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


def _update(text: str):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(text=text),
    )


@pytest.mark.asyncio
async def test_same_chat_messages_process_sequentially(_env, monkeypatch):
    daemon = RecordingDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()

    # Deliver 3 messages for the SAME chat_id simultaneously.
    await asyncio.gather(
        _env.handle_message(_update("a"), SimpleNamespace(bot=bot)),
        _env.handle_message(_update("b"), SimpleNamespace(bot=bot)),
        _env.handle_message(_update("c"), SimpleNamespace(bot=bot)),
    )

    manager = _env._get_queues()
    queue = manager.get_queue(str(CHAT_ID))
    # All three queued items must drain through the per-chat worker.
    await asyncio.wait_for(queue.join(), timeout=2)
    manager.cancel_worker(str(CHAT_ID))

    # The daemon was asked 3 times, once per message, with no overlap.
    assert len(daemon.windows) == 3, "send_message must be called 3 times"

    for (prev_start, prev_end), (start, _end) in zip(
        daemon.windows, daemon.windows[1:]
    ):
        assert prev_end < start, "daemon turns for the same chat must not overlap"

    # Every queued message reached the daemon and its reply reached Telegram.
    assert bot.sent == [(CHAT_ID, "World")] * 3


@pytest.mark.asyncio
async def test_queue_full_drops_and_sends_please_wait(_env, monkeypatch):
    """Feedback loop: a backed-up chat queue must not grow without bound.

    When the worker cannot keep up (e.g. the daemon is wedged), a bounded
    queue drops the newest message and tells the user to wait, instead of
    growing forever and leaking memory.
    """
    manager = ChatQueueManager(maxsize=1)
    monkeypatch.setattr(_env, "_get_queues", lambda: manager)
    # Freeze the worker so nothing drains: the queue stays at capacity.
    monkeypatch.setattr(manager, "ensure_worker", lambda *a, **k: None)
    bot = RecordingBot()

    await _env.handle_message(_update("first"), SimpleNamespace(bot=bot))
    # Queue is full now; a second message is dropped, not processed.
    await _env.handle_message(_update("second"), SimpleNamespace(bot=bot))

    assert manager.get_queue(str(CHAT_ID)).qsize() == 1
    assert bot.sent == [(CHAT_ID, _env.QUEUE_FULL_TEXT)]