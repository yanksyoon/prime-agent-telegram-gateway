"""E2T1+E2T2+E4T1: the main Telegram message handler.

E2T1 applies the strict inbound ``@require_acl`` gate so an unauthorized
update is dropped BEFORE anything is queued (the RCE-critical boundary).

E4T1 makes the handler an *enqueuer*: the real work (session resolve, daemon
RPC, Telegram reply) happens in a background per-chat worker so that
messages for the SAME chat are processed one at a time, never concurrently.
Prime Agent's daemon is strictly turn-based per session, so concurrent
handling of the same chat would corrupt the session state; different chats
are independent and each gets its own worker.

The worker path is:

  handle_message  -> enqueue ``(update, context)`` on the chat's asyncio.Queue
  _process_item   -> unpack the item and call ``_process_update``
  _process_update -> 1. resolve session_id via SessionManager,
                     2. daemon.send_message, 3. context.bot.send_message

The daemon, session manager, and queue manager are reached through module-level
seams (``_get_daemon`` / ``_get_sessions`` / ``_get_queues``) so tests can
inject mocks; production uses a real ``DaemonRPCClient`` over the configured
socket backed by ``SessionManager`` and ``ChatQueueManager``.

Feedback loop: a failing daemon must not crash the worker or the update. We
catch it, log it, and send the user a polite ``AGENT_BUSY_TEXT`` message.
A chat queue that backs up (daemon wedged) drops the newest message and tells
the user ``QUEUE_FULL_TEXT`` instead of growing without bound.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from gateway.acl import require_acl
from gateway.config import get_config
from gateway.daemon_client import DaemonRPCClient
from gateway.queue_manager import ChatQueueManager
from gateway.session_store import SessionManager
from gateway.stt import SttEngine, mock_stt_engine

logger = logging.getLogger(__name__)

AGENT_BUSY_TEXT = (
    "Agent is busy or encountered an error. Please try again in a moment."
)
QUEUE_FULL_TEXT = (
    "Queue full, please wait."
)

# E3T1: static, hardcoded control commands (no dynamic registration).
# The ticket mandates three; two are named (/refine, /status) and ``clear`` is
# the third. Unknown slash tokens (e.g. ``/nope``) fall through to plain text.
CONTROL_COMMANDS: tuple[str, ...] = ("refine", "status", "clear")

QUEUE_MAXSIZE = int(get_config().queue_maxsize)

_daemon: Any | None = None
_sessions: SessionManager | None = None
_queues: ChatQueueManager | None = None
_stt: SttEngine | None = None


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


def _get_queues() -> ChatQueueManager:
    """Return the per-chat queue manager, building it lazily on first use.

    Test seam: monkeypatch this in tests to inspect/inject queue state.
    """
    global _queues
    if _queues is None:
        _queues = ChatQueueManager(maxsize=QUEUE_MAXSIZE)
    return _queues


def _get_stt() -> SttEngine:
    """Return the speech-to-text engine (defaults to the mock engine).

    Test seam: monkeypatch this in tests to inject a mocked transcription.
    """
    global _stt
    if _stt is None:
        _stt = mock_stt_engine
    return _stt


@require_acl
async def handle_message(update, context):
    """Queue an inbound Telegram message for sequential processing.

    The ACL decorator enforces authorization before this body can run. The
    message (with its bot context) is appended to the chat's queue rather than
    processed inline, so a burst of messages for one chat is serialized by the
    per-chat worker instead of running concurrently against the same daemon
    session. If the chat's queue is full we drop the message and tell the user
    to wait.
    """
    logger.info("ACL allow: message from user_id=%s proceeding", _user_id(update))

    chat_id = _chat_id(update)
    key = str(chat_id)

    manager = _get_queues()
    if not manager.enqueue_nowait(key, (update, context)):
        logger.warning("queue full for chat_id=%s; dropping message", chat_id)
        await context.bot.send_message(chat_id=chat_id, text=QUEUE_FULL_TEXT)
        return None

    manager.ensure_worker(key, _process_item)
    return None


async def _process_item(item: tuple[Any, Any]) -> None:
    """Worker step: unpack a queued ``(update, context)`` and process it."""
    update, context = item
    await _process_update(_chat_id(update), update, context)


async def _process_update(chat_id, update, context) -> str | None:
    """Do the one-at-a-time work for a single message.

    Returns the daemon reply, or ``None`` after sending a busy message when
    the daemon call fails. Runs under the per-chat worker, so it is guaranteed
    never to overlap another message for the same chat.
    """
    text = _text(update)

    session_id = await _get_sessions().get_or_create_session(str(chat_id))

    # E3T1: a leading '/' marks a control command. Intercept known ones and
    # route them as a structured control payload instead of raw text. Anything
    # else — including a string that merely *contains* '/cmd' mid-message —
    # flows through the normal plain-text path below.
    if text and text.startswith("/"):
        command, args = _parse_slash_command(text)
        if command in CONTROL_COMMANDS:
            return await _route_control(command, args, session_id, chat_id, context)

    try:
        reply = await _get_daemon().send_message(session_id, text)
    except Exception:  # daemon busy/down: user sees a prompt, not a crash
        logger.exception("daemon send_message failed for session_id=%s", session_id)
        await context.bot.send_message(chat_id=chat_id, text=AGENT_BUSY_TEXT)
        return None

    await context.bot.send_message(chat_id=chat_id, text=reply)
    return reply


def _user_id(update):
    return getattr(getattr(update, "effective_user", None), "id", None)


def _chat_id(update):
    return getattr(getattr(update, "effective_chat", None), "id", None)


def _text(update):
    return getattr(getattr(update, "message", None), "text", None)


def _parse_slash_command(text: str) -> tuple[str, str]:
    """Split ``/cmd args`` into ``(command, args)``.

    ``/refine``           -> (``refine``, ``''``)
    ``/refine with this``  -> (``refine``, ``with this``)

    Only the first space is split on, so the payload (everything after the
    command word) is preserved verbatim. The command is lowercased (so
    ``/REFINE`` matches) and its leading ``/`` is stripped.
    """
    command_part, _, args = text.partition(" ")
    command = command_part.lstrip("/").strip().lower()
    return command, args.strip()


async def _route_control(
    command: str, args: str, session_id: str, chat_id: Any, context: Any
) -> None:
    """Send a recognized control command to the daemon and relay the reply.

    Mirrors the plain-text path's failure handling: a failing daemon yields a
    polite busy message, never a crash.
    """
    logger.info("control command /%s from session_id=%s", command, session_id)
    try:
        reply = await _get_daemon().send_control_command(command, args)
    except Exception:  # daemon busy/down: user sees a prompt, not a crash
        logger.exception(
            "daemon send_control_command failed for session_id=%s", session_id
        )
        await context.bot.send_message(chat_id=chat_id, text=AGENT_BUSY_TEXT)
        return None

    await context.bot.send_message(chat_id=chat_id, text=reply)
    return None


@require_acl
async def handle_voice(update, context):
    """Handle an inbound Telegram voice note (mocked STT).

    ACL-gated like ``handle_message``. Downloads the voice file to a temp path,
    runs it through the (mock) STT engine, then reuses ``handle_message`` with
    the transcribed text as ``update.message.text`` so the transcribed text
    flows through the exact same queue / daemon / reply path as a normal
    message.

    Scope: mocked STT only. Real STT (faster-whisper) is a separate later task.
    Feedback loop: the temp file is always removed (``finally``) so voice
    notes cannot leak files onto disk; a download/transcription failure sends
    the user a polite busy message rather than crashing.
    """
    logger.info("ACL allow: voice from user_id=%s transcribing", _user_id(update))

    chat_id = _chat_id(update)
    file_path: str | None = None
    try:
        voice = update.message.voice
        file = await voice.get_file()

        # Download to a unique temp path (suffix .ogg is the Telegram audio
        # container; a future real STT engine reads it from here).
        fd, file_path = tempfile.mkstemp(prefix="voice_", suffix=".ogg")
        os.close(fd)
        await file.download_to_drive(custom_path=file_path)

        transcribed = await _get_stt().transcribe(file_path)
        update.message.text = transcribed  # reuse the text-message path
        return await handle_message(update, context)
    except Exception:  # download/STT failure: user sees a prompt, not a crash
        logger.exception("voice transcription failed for chat_id=%s", chat_id)
        await context.bot.send_message(chat_id=chat_id, text=AGENT_BUSY_TEXT)
        return None
    finally:
        if file_path is not None:
            try:
                os.remove(file_path)  # prevent temp-file disk leaks
            except OSError:  # pragma: no cover - already cleaned / never created
                pass