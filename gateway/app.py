"""E4T3: graceful shutdown & daemon detach (SIGTERM).

On SIGTERM/SIGINT the gateway must NOT kill the underlying Prime Agent daemon
sessions. Instead it tells the daemon to *detach* each active session (leaving
the agent logic alive in the background), closes the RPC socket, and exits
cleanly.

:class:`GatewayApp` owns the daemon/session seams and the set of *active*
sessions (resolved for a chat since startup). :meth:`GatewayApp.shutdown`
iterates those session ids, calls ``daemon.detach`` for each (gracefully
degrading if an individual detach fails, e.g. the daemon is already down), then
closes the RPC socket.

``main()`` is the ``python -m gateway.app`` entrypoint: it connects to the
configured daemon socket, resolves one active session (the stand-in for the
first ingested message), installs SIGTERM/SIGINT handlers via
:func:`install_signal_handlers`, and on signal runs :meth:`GatewayApp.shutdown`
before exiting zero.

Feedback loop (project_init.md Task 4.3): SIGKILL aborts the process before any
detach can run, orphaning the daemon session. That is a known limitation;
operators must use ``systemctl stop`` / standard termination so a signal
(SIGTERM/SIGINT) is actually delivered and the detach path executes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Callable

from gateway.config import get_config
from gateway.daemon_client import DaemonRPCClient
from gateway.session_store import SessionManager

logger = logging.getLogger(__name__)

STARTUP_CHAT_ID = 777  # id of the single stand-in chat used by ``main()``


class GatewayApp:
    """Wires daemon + sessions and owns the set of active daemon sessions.

    A session becomes *active* the moment the gateway resolves it for a chat
    (:meth:`resolve_active_session` / :meth:`note_session`). On shutdown every
    active session is detached from the daemon so the agent logic stays alive
    in the background while the gateway process exits.
    """

    def __init__(self, daemon: Any, sessions: Any) -> None:
        self._daemon = daemon
        self._sessions = sessions
        self._active_sessions: set[str] = set()

    @property
    def active_sessions(self) -> set[str]:
        """A copy of the currently-detached-later set of session ids."""
        return set(self._active_sessions)

    def note_session(self, session_id: str) -> None:
        """Register ``session_id`` as active so shutdown will detach it."""
        self._active_sessions.add(session_id)

    async def resolve_active_session(self, chat_id: str) -> str:
        """Resolve (and take ownership of) the daemon session for ``chat_id``.

        Forward to :meth:`SessionManager.get_or_create_session` and mark the
        resulting session id as active for the lifecycle of the process.
        """
        session_id = await self._sessions.get_or_create_session(str(chat_id))
        self._active_sessions.add(session_id)
        return session_id

    async def shutdown(self) -> None:
        """Detach every active session from the daemon, then close the socket.

        A single failing detach (daemon already down, socket dropped) is
        logged and skipped; the remaining sessions are still detached and the
        socket is always closed, so a graceful shutdown never half-finishes.
        """
        for session_id in list(self._active_sessions):
            try:
                await self._daemon.detach(session_id)
            except Exception:  # daemon down / socket closed: don't abort the rest
                logger.exception("detach failed for session_id=%s", session_id)
        self._active_sessions.clear()
        await self._daemon.close()


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_shutdown: Callable[[], object] | None
) -> None:
    """Wire SIGTERM and SIGINT to the shutdown callback on ``loop``.

    ``loop.add_signal_handler`` is used (rather than bare ``signal.signal``)
    because it runs the callback inside the event loop, so calling
    ``asyncio.Event.set()`` / coroutine scheduling from the handler is safe.
    """
    loop.add_signal_handler(signal.SIGTERM, on_shutdown)
    loop.add_signal_handler(signal.SIGINT, on_shutdown)


def main() -> None:
    """Run the gateway until SIGTERM/SIGINT, then detach sessions and exit 0."""

    async def _run() -> None:
        cfg = get_config()
        daemon = DaemonRPCClient(socket_path=cfg.daemon_socket_path)
        await daemon.connect()
        sessions = SessionManager(daemon, db_path=":memory:")
        app = GatewayApp(daemon, sessions)

        # Establish an active session (the stand-in for the first message).
        session_id = await app.resolve_active_session(str(STARTUP_CHAT_ID))
        await daemon.send_message(session_id, "Hello")

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        install_signal_handlers(loop, stop.set)

        print(f"READY {session_id}", flush=True)
        await stop.wait()

        logger.info(
            "shutdown: detaching %d active session(s)", len(app.active_sessions)
        )
        await app.shutdown()
        print("STOPPED", flush=True)

    asyncio.run(_run())


if __name__ == "__main__":
    main()