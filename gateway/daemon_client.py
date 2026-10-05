"""E1T1: Mockable daemon JSONL socket client.

A thin, non-blocking wrapper around ``asyncio.StreamReader``/
``asyncio.StreamWriter`` implementing strict newline-delimited JSON (JSONL)
framing. No raw socket code.

The Prime Agent daemon speaks JSONL over a local socket. ``DaemonRPCClient``
connects, sends a JSON payload as one newline-terminated frame, and parses
each response by accumulating an exact line before ``json.loads``. It talks
to whichever stream ``connect()`` targets, so tests can point it at an
in-process mock (``asyncio.start_server``) instead of the real daemon.

Supported transports (via ``asyncio.open_connection``):
- TCP:      ``connect(host=..., port=...)``
- Unix dsm: ``connect(socket_path=...)``          (e.g. ``/tmp/pa-daemon.sock``)
"""

from __future__ import annotations

import asyncio
import json

__all__ = ["DaemonRPCClient"]


class DaemonRPCClient:
    """Connect / send / recv / close JSONL RPC over an asyncio stream.

    One message per call is a strict newline-delimited frame; the reader
    buffers until a ``\\n`` arrives and only then attempts ``json.loads``,
    so back-to-back (even same-buffer) messages are never merged.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        socket_path: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(
        self,
        host: str | None = None,
        port: int | None = None,
        socket_path: str | None = None,
    ) -> "DaemonRPCClient":
        """Open the stream to the daemon (TCP or Unix socket). Idempotent."""
        if self.connected:
            return self
        sock_path = socket_path if socket_path is not None else self._socket_path
        if sock_path is not None:
            # Unix domain socket, e.g. /tmp/pa-daemon.sock
            reader, writer = await asyncio.open_unix_connection(path=sock_path)
        else:
            reader, writer = await asyncio.open_connection(
                host=host if host is not None else self._host,
                port=port if port is not None else self._port,
            )
        self._reader = reader
        self._writer = writer
        return self

    async def send(self, payload: dict) -> None:
        """Encode ``payload`` as one JSONL frame and write it to the daemon."""
        if not self.connected:
            raise ConnectionError(
                "DaemonRPCClient is not connected; call connect() first"
            )
        assert self._writer is not None  # guaranteed by the connected guard
        frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        self._writer.write(frame)
        await self._writer.drain()

    async def recv(self) -> dict:
        """Read and parse the next JSONL response frame."""
        if not self.connected:
            raise ConnectionError(
                "DaemonRPCClient is not connected; call connect() first"
            )
        assert self._reader is not None  # guaranteed by the connected guard
        return json.loads(await self._read_line())

    async def _read_line(self) -> str:
        """Accumulate exactly one ``\\n``-terminated line, skipping blanks.

        ``StreamReader.readline`` buffers internally and only returns once a
        newline is present, so a burst of frames is split per ``\\n`` rather
        than merged into one blob.
        """
        raw = await self._reader.readline()
        if raw == b"":
            raise ConnectionError(
                "Daemon stream closed before a complete JSONL line was received"
            )
        line = raw.strip(b"\r\n").decode("utf-8")
        if not line:
            return await self._read_line()  # blank separator line: keep going
        return line

    async def close(self) -> None:
        """Close the writer and relinquish the stream."""
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def create_session(self, chat_id: str) -> str:
        """Ask the daemon to create a session for ``chat_id``; return its id.

        Sends ``{"type": "create_session", "chat_id": ...}`` and reads the
        JSONL response, expecting ``{"session_id": ...}``.
        """
        await self.send({"type": "create_session", "chat_id": chat_id})
        resp = await self.recv()
        try:
            return resp["session_id"]
        except (KeyError, TypeError) as exc:  # pragma: no cover - defensive
            raise ConnectionError(
                f"create_session response missing 'session_id': {resp!r}"
            ) from exc