"""E1T2 (+ topics): session creation & conversation-key -> session mapping.

A :class:`SessionManager` maps a Telegram conversation to a Prime Agent
daemon ``session_id`` and persists that mapping to disk so gateway restarts do
not orphan daemon sessions.

Per-topic sessions: the mapping is keyed on the composite ``(chat_id,
thread_id)`` so a forum supergroup holds ONE daemon session per topic (thread)
instead of one per chat. The public ``get_or_create_session(key: str)`` keeps
its single-string signature, but the ``key`` is now a conversation key of the
form ``"{chat_id}:{message_thread_id}"`` (the handlers' ``_chat_key``). For a
non-forum chat ``message_thread_id`` is None and normalises to ``0``, so a DM
resolves under ``"{chat_id}:0"`` exactly as before.

Flow for ``get_or_create_session(key)``:
1. Split the key into ``(chat_id, thread_id)`` and look it up in the store.
2. Cache HIT  -> return the stored ``session_id``, never touch the daemon.
3. Cache MISS -> call ``daemon.create_session(key)`` over RPC, persist the
   new mapping, and return the fresh ``session_id``.

Persistence uses SQLite in WAL journal mode (``PRAGMA journal_mode=WAL``) so
concurrent readers are never blocked by a writer and a crash cannot corrupt
the store. The ``chat_sessions`` table's ``PRIMARY KEY (chat_id, thread_id)``
makes each mapping idempotent on repeat writes (``INSERT OR REPLACE``).

Migration note: any pre-existing on-disk DB created with the old single-column
``chat_id TEXT PRIMARY KEY`` schema must be recreated by hand (e.g. drop the
``chat_sessions`` table or delete the ``.db`` file), because ``CREATE TABLE IF
NOT EXISTS`` will not alter an already-created table. Every ``:memory:`` store
here is built fresh to the composite-key schema.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

__all__ = ["SessionManager", "SessionCreator"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id    TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
)
"""


def _split_key(key: str) -> tuple[str, str]:
    """Split a ``"{chat_id}:{thread_id}"`` conversation key into its parts.

    Telegram chat ids and message_thread_ids are integers (optionally negative
    for groups), so splitting on the first ``:`` is unambiguous; a bare
    ``"{chat_id}"`` key yields ``thread_id=""`` and keeps the old behaviour.
    """
    chat_id, _, thread_id = key.partition(":")
    return chat_id, thread_id


class SessionCreator(Protocol):
    """Anything that can mint a Prime Agent session id for a conversation key."""

    async def create_session(self, chat_id: str) -> str: ...


class SessionManager:
    """Persistent ``(chat_id, thread_id)`` -> ``session_id`` mapping + backfill."""

    def __init__(self, daemon: SessionCreator, db_path: str | None = None) -> None:
        self._daemon = daemon
        self._db_path = db_path or ":memory:"
        self._conn = sqlite3.connect(self._db_path)  # thread-local by design
        self._conn.row_factory = sqlite3.Row
        # WAL: readers never block the single writer; crash-safe on Unix.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    async def get_or_create_session(self, key: str) -> str:
        """Return the daemon session id for conversation ``key``, creating if absent.

        ``key`` is the handlers' ``_chat_key``, ``"{chat_id}:{thread_id}"``.

        Cache HIT: the stored ``session_id`` is returned and ``create_session``
        is NOT called. Cache MISS: the daemon is asked to create a session,
        the result is persisted, then returned.
        """
        chat_id, thread_id = _split_key(key)
        cached = self._lookup(chat_id, thread_id)
        if cached is not None:
            return cached

        session_id = await self._daemon.create_session(key)

        # REPLACE keeps the write idempotent if two misses race for one topic.
        self._conn.execute(
            "INSERT OR REPLACE INTO chat_sessions (chat_id, thread_id, session_id) "
            "VALUES (?, ?, ?)",
            (chat_id, thread_id, session_id),
        )
        self._conn.commit()
        return session_id

    def _lookup(self, chat_id: str, thread_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM chat_sessions "
            "WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        return row["session_id"] if row is not None else None

    def close(self) -> None:
        self._conn.close()