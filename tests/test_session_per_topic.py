"""Per-topic sessions: one Prime Agent session per Telegram TOPIC, not per chat.

In a forum supergroup every message carries a ``message_thread_id``; the topic
is the session identity, shared by everyone messaging in it. Two messages in
the SAME group but DIFFERENT topics must resolve to TWO DISTINCT daemon
sessions; two messages in the SAME topic must REUSE one session (a cache HIT
that never calls daemon.create_session again).

TDD: red before implementation - the gateway today keys sessions by chat_id
alone, so topic A and topic B (same group) resolve to the SAME session and the
distinct-session assertion fails.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.session_store import SessionManager


class MockDaemon:
    """Daemon stand-in minting a NEW session_id per distinct conversation key."""

    def __init__(self) -> None:
        self.create_calls: list[str] = []
        self._next = 0
        self.sent: list[tuple[str, str]] = []  # (session_id, text)

    async def create_session(self, key: str) -> str:
        self.create_calls.append(key)
        self._next += 1
        return f"session_{self._next}"

    async def send_message(self, session_id: str, text: str) -> str:
        self.sent.append((session_id, text))
        return "World"


class RecordingBot:
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


def _update(text: str, chat_id: int = 777, thread: int | None = None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, message_thread_id=thread),
    )


async def _stop_queue(manager, key: str) -> None:
    queue = manager.get_queue(key)
    await asyncio.wait_for(queue.join(), timeout=2)
    manager.cancel_worker(key)


@pytest.mark.asyncio
async def test_each_topic_gets_distinct_session(_env, monkeypatch):
    daemon = MockDaemon()
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = RecordingBot()
    manager = _env._get_queues()

    # topic A (thread 10) and topic B (thread 20) in the SAME group.
    await _env.handle_message(_update("hi", thread=10), SimpleNamespace(bot=bot))
    await _stop_queue(manager, "777:10")
    await _env.handle_message(_update("hi", thread=20), SimpleNamespace(bot=bot))
    await _stop_queue(manager, "777:20")

    # Two topics -> two distinct daemon sessions, created once each.
    assert sorted(daemon.create_calls) == ["777:10", "777:20"]
    assert daemon.sent[0][0] != daemon.sent[1][0], "topics must not share a session"

    # A second message in topic A must HIT the cache: create_session NOT called.
    await _env.handle_message(_update("again", thread=10), SimpleNamespace(bot=bot))
    await _stop_queue(manager, "777:10")

    assert daemon.create_calls == ["777:10", "777:20"], (
        "reusing an existing topic must not mint a new session"
    )
    assert daemon.sent[-1][0] == daemon.sent[0][0], "same topic must reuse its session"
    assert bot.sent == [(777, "World")] * 3