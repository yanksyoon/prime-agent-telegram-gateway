"""E1T2a: SessionManager cache hit.

A chat_id that already has a saved session mapping must return the stored
session id WITHOUT calling the daemon's create_session again. This protects
against orphaning daemon sessions on duplicate requests / gateway restarts.
"""

import asyncio

from gateway.session_store import SessionManager


class MockDaemon:
    """Fake Prime Agent daemon that records how often create_session runs."""

    def __init__(self) -> None:
        self.create_calls: list[str] = []
        self.next_id = 1

    async def create_session(self, chat_id: str) -> str:
        self.create_calls.append(chat_id)
        session_id = f"session_{self.next_id}"
        self.next_id += 1
        return session_id


async def test_session_cache_hit_does_not_call_daemon(tmp_path):
    daemon = MockDaemon()
    store = SessionManager(daemon, db_path=tmp_path / "sessions.db")

    # First request is a miss: daemon creates the session and it is persisted.
    first = await store.get_or_create_session("chat-123")
    assert first == "session_1"
    assert daemon.create_calls == ["chat-123"]

    # Second request for the same chat is a HIT: create_session must NOT run.
    second = await store.get_or_create_session("chat-123")
    assert second == "session_1"
    assert daemon.create_calls == ["chat-123"], (
        "create_session should not be called again on a cache hit"
    )


async def test_session_cache_hit_survives_manager_reload(tmp_path):
    """A fresh SessionManager over the same db discovers the prior mapping."""
    db_path = tmp_path / "sessions.db"
    seed_daemon = MockDaemon()
    first_mgr = SessionManager(seed_daemon, db_path=db_path)
    await first_mgr.get_or_create_session("chat-999")
    assert seed_daemon.create_calls == ["chat-999"]

    # Reopen (simulates a gateway restart) with a daemon that would be a
    # NEW id if called. The mapping on disk must win.
    reload_daemon = MockDaemon()
    reload_mgr = SessionManager(reload_daemon, db_path=db_path)
    session = await reload_mgr.get_or_create_session("chat-999")

    assert session == "session_1"
    assert reload_daemon.create_calls == [], (
        "reloaded manager must not re-create an already-persisted session"
    )