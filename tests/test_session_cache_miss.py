"""E1T2b: SessionManager cache miss.

A chat_id with NO saved mapping must call the daemon's create_session and
persist the returned session id to disk so later requests (and restarts)
can resolve it without a second daemon round-trip.
"""

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


async def test_session_cache_miss_calls_daemon_and_persists(tmp_path):
    db_path = tmp_path / "sessions.db"
    daemon = MockDaemon()
    store = SessionManager(daemon, db_path=db_path)

    session = await store.get_or_create_session("brand-new-chat")

    # Miss => create_session IS called for exactly this chat.
    assert daemon.create_calls == ["brand-new-chat"]
    assert session == "session_1"

    # Mapping must be saved to disk: inspect the raw sqlite table directly.
    import sqlite3

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT session_id FROM chat_sessions WHERE chat_id = ?",
        ("brand-new-chat",),
    ).fetchall()
    conn.close()
    assert rows == [("session_1",)], "mapping must be persisted to disk"


async def test_multiple_chats_are_isolated(tmp_path):
    daemon = MockDaemon()
    store = SessionManager(daemon, db_path=tmp_path / "sessions.db")

    a = await store.get_or_create_session("chat-a")
    b = await store.get_or_create_session("chat-b")

    assert a == "session_1"
    assert b == "session_2"
    assert daemon.create_calls == ["chat-a", "chat-b"]