"""E3T1: slash command interception & routing.

An authorized user sends ``/refine``. The gateway must NOT forward it to the
daemon as plain text; instead it must send a structured control payload
``{"type": "control", "command": "refine"}`` via ``daemon.send_control_command``.

This is the ticket's acceptance test: the daemon mock's ``send_control_command``
must be called with command ``refine`` (and ``send_message`` must NOT be called),
and Telegram must relay the daemon's control-command reply.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from gateway.handlers import CONTROL_COMMANDS, handle_message
from gateway.session_store import SessionManager

ALLOWED = {111, 222}
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"


async def _stop_queue(_env, chat_id: str) -> None:
    """E4T1: drain the per-chat worker before asserting on its side effects.

    handle_message now only enqueues; the daemon call and Telegram reply happen
    in the background per-chat worker.
    """
    manager = _env._get_queues()
    queue = manager.get_queue(chat_id)
    await asyncio.wait_for(queue.join(), timeout=2)
    worker = manager._workers.pop(chat_id, None)
    if worker is not None:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


class FakeTelegramBot:
    """Minimal stand-in for python-telegram-bot's ``context.bot``.

    send_message performs the real HTTPS POST to Telegram (intercepted by
    respx), mirroring the E2T2 test harness.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(BOT_URL, json={"chat_id": chat_id, "text": text})
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)  # re-bind the @require_acl allowlist from env
    return handlers


def _slash_update(text: str):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=777),
        message=SimpleNamespace(text=text),
    )


@pytest.mark.asyncio
async def test_slash_refine_routes_to_control_payload(_env, monkeypatch):
    """``/refine`` must reach the daemon as a structured control payload."""
    # Assert the command is actually in the static control list first.
    assert "refine" in CONTROL_COMMANDS, "/refine must be a hardcoded control command"

    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_control_command=AsyncMock(return_value="Refined."),
        send_message=AsyncMock(return_value="must-not-be-called"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = FakeTelegramBot()

    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    captured: list[dict] = []
    with router:
        result = await _env.handle_message(_slash_update("/refine"), SimpleNamespace(bot=bot))
        # E4T1: the control-RPC + reply now run in the per-chat worker.
        await _stop_queue(_env, "777")
        captured.append(json.loads(router.calls.last.request.content))

    # 1. The daemon received a structured control payload, NOT raw text.
    daemon.send_control_command.assert_awaited_once_with("refine", "")
    # 2. The plain-text path (send_message) was never taken for a control command.
    daemon.send_message.assert_not_called()

    # 3. The daemon's control reply was relayed to Telegram.
    assert captured, "control reply must reach Telegram sendMessage"
    body = captured[0]
    assert body["text"] == "Refined."
    assert body["chat_id"] == 777
    assert bot.sent == [(777, "Refined.")]

    assert result is None


@pytest.mark.asyncio
async def test_slash_with_args_splits_command_from_payload(_env, monkeypatch):
    """Feedback loop: ``/refine with this`` -> command ``refine``, args ``with this``."""
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_control_command=AsyncMock(return_value="Refined."),
        send_message=AsyncMock(return_value="must-not-be-called"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    bot = FakeTelegramBot()

    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    with router:
        result = await _env.handle_message(
            _slash_update("/refine with this"), SimpleNamespace(bot=bot)
        )
        # E4T1: control-RPC + reply run in the per-chat worker; drain it.
        await _stop_queue(_env, "777")

    # The split logic must separate the command name from its payload.
    daemon.send_control_command.assert_awaited_once_with("refine", "with this")
    daemon.send_message.assert_not_called()
    assert bot.sent == [(777, "Refined.")]
    assert result is None