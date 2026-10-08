"""Serialization is PER TOPIC, like the per-chat serialization before it.

Different topics in the SAME group are independent lanes and may be processed
concurrently; two messages in the SAME topic stay strictly sequential (their
daemon windows must not overlap). This mirrors test_queue_sequential.py but
keys the lanes by ``(chat_id, message_thread_id)`` instead of chat_id alone.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_store import SessionManager

CHAT = 777


class RecordingDaemon:
    def __init__(self) -> None:
        self.windows: list[tuple[float, float]] = []
        self.create_session = AsyncMock(return_value="sess-1")

    async def send_message(self, session_id, text):
        start = time.monotonic()
        await asyncio.sleep(0.02)  # daemon turn time: overlaps if concurrent
        self.windows.append((start, time.monotonic()))
        return "World"


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    import importlib

    import gateway.handlers as handlers

    importlib.reload(handlers)
    return handlers


def _update(text: str, thread: int):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=CHAT),
        message=SimpleNamespace(text=text, message_thread_id=thread),
    )


@pytest.mark.asyncio
async def test_different_topics_process_concurrently(_env, monkeypatch):
    daemon = RecordingDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()
    manager = _env._get_queues()

    # Two different topics pushed at the same instant must run in parallel.
    await asyncio.gather(
        _env.handle_message(_update("a", thread=10), SimpleNamespace(bot=bot)),
        _env.handle_message(_update("b", thread=20), SimpleNamespace(bot=bot)),
    )
    await asyncio.wait_for(manager.get_queue(f"{CHAT}:10").join(), timeout=2)
    await asyncio.wait_for(manager.get_queue(f"{CHAT}:20").join(), timeout=2)
    manager.cancel_worker(f"{CHAT}:10")
    manager.cancel_worker(f"{CHAT}:20")

    assert len(daemon.windows) == 2, "one daemon turn per topic"
    (s1, e1), (s2, e2) = daemon.windows
    assert s1 < e2 and s2 < e1, "distinct topics must overlap (independent lanes)"


@pytest.mark.asyncio
async def test_same_topic_processes_sequentially(_env, monkeypatch):
    daemon = RecordingDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()
    manager = _env._get_queues()

    # 3 messages for the SAME topic simultaneously must stay strictly sequential.
    await asyncio.gather(
        _env.handle_message(_update("a", thread=5), SimpleNamespace(bot=bot)),
        _env.handle_message(_update("b", thread=5), SimpleNamespace(bot=bot)),
        _env.handle_message(_update("c", thread=5), SimpleNamespace(bot=bot)),
    )
    await asyncio.wait_for(manager.get_queue(f"{CHAT}:5").join(), timeout=2)
    manager.cancel_worker(f"{CHAT}:5")

    assert len(daemon.windows) == 3, "send_message must be called 3 times"
    for (prev_s, prev_e), (s, _e) in zip(daemon.windows, daemon.windows[1:]):
        assert prev_e < s, "same-topic daemon turns must not overlap"