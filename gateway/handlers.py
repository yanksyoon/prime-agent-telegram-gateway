"""E2T1+E2T2: the main Telegram message handler.

E2T1 applies the strict inbound ``@require_acl`` gate so an unauthorized
update is dropped BEFORE any daemon RPC can run (the RCE-critical boundary).
E2T2 wires the actual roundtrip inside the ACL-allowed body:

  handle_message(update, context)
    1. resolve the daemon ``session_id`` for the chat via ``SessionManager``,
    2. send the user's text to the daemon (``daemon.send_message``),
    3. relay the daemon's reply back to Telegram (``context.bot.send_message``).

The daemon and session manager are reached through module-level seams
(``_get_daemon`` / ``_get_sessions``) so tests can inject mocks; production
uses a real ``DaemonRPCClient`` over the configured socket backed by a
``SessionManager``.

Feedback loop: a failing daemon (busy, connection drop, bad response) must not
crash the update. We catch it, log it, and send the user a polite
``AGENT_BUSY_TEXT`` message instead.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.acl import require_acl
from gateway.config import get_config
from gateway.daemon_client import DaemonRPCClient
from gateway.session_store import SessionManager

logger = logging.getLogger(__name__)

AGENT_BUSY_TEXT = (
    "Agent is busy or encountered an error. Please try again in a moment."
)

_daemon: Any | None = None
_sessions: SessionManager | None = None


def _default_daemon() -> DaemonRPCClient:
    return DaemonRPCClient(socket_path=get_config().daemon_socket_path)


def _get_daemon() -> Any:
    """Return the daemon RPC client, building it lazily on first use.

    Test seam: monkeypatch this in tests to inject a mock daemon.
    """
    global _daemon
    if _daemon is None:
        _daemon = _default_daemon()
    return _daemon


def _get_sessions() -> SessionManager:
    """Return the SessionManager, building it lazily on first use.

    Test seam: monkeypatch this in tests to inject a session store.
    """
    global _sessions
    if _sessions is None:
        _sessions = SessionManager(_get_daemon())
    return _sessions


@require_acl
async def handle_message(update, context):
    """Handle an inbound Telegram message end to end.

    The ACL decorator enforces authorization before this body can run. Inside
    we resolve the chat's daemon session, forward the text to the daemon, and
    relay the reply back to Telegram, degrading to a polite busy message if
    the daemon call fails rather than crashing the update.
    """
    logger.info("ACL allow: message from user_id=%s proceeding", _user_id(update))

    chat_id = _chat_id(update)
    text = _text(update)

    session_id = await _get_sessions().get_or_create_session(str(chat_id))

    try:
        reply = await _get_daemon().send_message(session_id, text)
    except Exception:  # daemon busy/down/crash attacks: user sees a prompt, not a crash
        logger.exception("daemon send_message failed for session_id=%s", session_id)
        await context.bot.send_message(chat_id=chat_id, text=AGENT_BUSY_TEXT)
        return None

    await context.bot.send_message(chat_id=chat_id, text=reply)
    return None


def _user_id(update):
    return getattr(getattr(update, "effective_user", None), "id", None)


def _chat_id(update):
    return getattr(getattr(update, "effective_chat", None), "id", None)


def _text(update):
    return getattr(getattr(update, "message", None), "text", None)