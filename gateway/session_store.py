"""E1T2: Session creation & chat_id -> session mapping.

A :class:`SessionManager` maps a Telegram ``chat_id`` to a Prime Agent daemon
``session_id`` and persists that mapping to disk so gateway restarts do not
orphan daemon sessions.

Flow for ``get_or_create_session(chat_id)``:
1. Look the ``chat_id`` up in the local SQLite store.
2. Cache HIT  -> return the stored ``session_id``, never touch the daemon.
3. Cache MISS -> call ``daemon.create_session(chat_id)`` over RPC, persist the
   new mapping, and return the fresh ``session_id``.

Persistence uses SQLite in WAL journal mode (``PRAGMA journal_mode=WAL``) so
concurrent readers are never blocked by a writer and a crash cannot corrupt
the store. The ``chat_sessions`` table uses ``chat_id`` as its primary key,
making each mapping idempotent on repeat writes.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

__all__ = ["SessionManager", "SessionCreator"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
)
"""


class SessionCreator(Protocol):
    """Anything that can mint a Prime Agent session id for a chat id."""

    async def create_session(self, chat_id: str) -> str: ...


class SessionManager:
    """Persistent ``chat_id`` -> ``session_id`` mapping with daemon backfill."""

    def __init__(self, daemon: SessionCreator, db_path: str | None = None) -> None:
        self._daemon = daemon
        self._db_path = db_path or ":memory:"
        self._conn = sqlite3.connect(self._db_path)  # thread-local by design
        self._conn.row_factory = sqlite3.Row
        # WAL: readers never block the single writer; crash-safe on Unix.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    async def get_or_create_session(self, chat_id: str) -> str:
        """Return the daemon session id for ``chat_id``, creating if absent.

        Cache HIT: the stored ``session_id`` is returned and ``create_session``
        is NOT called. Cache MISS: the daemon is asked to create a session,
        the result is persisted, then returned.
        """
        cached = self._lookup(chat_id)
        if cached is not None:
            return cached

        session_id = await self._daemon.create_session(chat_id)

        # REPLACE keeps the write idempotent if two misses race for one chat.
        self._conn.execute(
            "INSERT OR REPLACE INTO chat_sessions (chat_id, session_id) "
            "VALUES (?, ?)",
            (chat_id, session_id),
        )
        self._conn.commit()
        return session_id

    def _lookup(self, chat_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM chat_sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["session_id"] if row is not None else None

    def close(self) -> None:
        self._conn.close()