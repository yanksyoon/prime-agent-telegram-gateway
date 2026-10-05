"""E3T2: voice note transcription (mocked STT).

An authorized user sends a voice note. The gateway:
  1. downloads the voice file to a temp path (``voice.get_file().download_to_drive``),
  2. calls the mock STT engine's ``transcribe(file_path)`` which returns "Mocked text",
  3. reuses the ``handle_message`` path so the daemon receives the transcribed text.

Telegram's HTTPS API is mocked with ``respx``, the daemon is an ``AsyncMock``,
and ``stt_engine.transcribe`` is mocked to return "Mocked text". Because E4T1
made ``handle_message`` an *enqueuer* (daemon RPC runs in a background per-chat
worker), the test drains the worker the same way ``test_slash_intercept`` does.
The feedback loop asserts the temp file is removed afterwards (no disk leak).
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from gateway.session_store import SessionManager

ALLOWED = {111, 222}
CHAT_ID = 777
BOT_URL = "https://api.telegram.org/botTEST_TOKEN/sendMessage"


async def _stop_queue(_env, chat_id: str) -> None:
    """E4T1: drain the per-chat worker before asserting on its side effects."""
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
    """Minimal stand-in for python-telegram-bot's ``context.bot`` (see E2T2)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(BOT_URL, json={"chat_id": chat_id, "text": text})
        self.sent.append((chat_id, text))


class RecordingBot:
    """No-network bot stand-in; records (chat_id, text) and never HTTP-POSTs."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)  # re-bind the @require_acl allowlist from env
    return handlers


def _inject(_env, monkeypatch, transcribed="Mocked text"):
    """Inject daemon, session store, and a mocked transcribe call."""
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value="World"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)

    stt_engine = SimpleNamespace(transcribe=AsyncMock(return_value=transcribed))
    monkeypatch.setattr(_env, "_get_stt", lambda: stt_engine)
    return daemon, stt_engine


def _voice_update():
    # Mock Telegram get_file + download_to_drive; download writes a real temp
    # file so the cleanup assertion in the feedback loop has something to check.
    async def fake_download(custom_path: str, **kwargs):
        with open(custom_path, "wb") as fh:
            fh.write(b"OGG-BYTES")
        return custom_path

    voice = SimpleNamespace(
        file_id="FILE123",
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                download_to_drive=AsyncMock(side_effect=fake_download)
            )
        ),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(text=None, voice=voice),
    )


def _router():
    router = respx.mock(assert_all_called=False)
    router.post(BOT_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    return router


@pytest.mark.asyncio
async def test_voice_mock_transcribes_and_reaches_daemon(_env, monkeypatch):
    daemon, stt_engine = _inject(_env, monkeypatch)
    bot = FakeTelegramBot()
    update = _voice_update()

    captured: list[dict] = []
    router = _router()
    with router:
        result = await _env.handle_voice(update, SimpleNamespace(bot=bot))
        await _stop_queue(_env, str(CHAT_ID))  # drain the background worker
        captured.append(json.loads(router.calls.last.request.content))

    # 1. The voice file was downloaded and handed to the STT engine.
    stt_engine.transcribe.assert_awaited_once()
    used_path = stt_engine.transcribe.await_args.args[0]
    assert isinstance(used_path, str) and used_path

    # 2. The daemon received the TRANSCRIBED text ("Mocked text"), not the raw
    #    file bytes and not an empty string.
    daemon.send_message.assert_awaited_once_with("sess-1", "Mocked text")

    # 3. The daemon's reply reached Telegram's sendMessage endpoint.
    assert captured, "Telegram sendMessage must be called once"
    body = captured[0]
    assert body["text"] == "World"
    assert body["chat_id"] == CHAT_ID
    assert bot.sent == [(CHAT_ID, "World")]

    # Feedback loop: the temp file must be cleaned up, no disk leak.
    assert not os.path.exists(used_path)

    assert result is None


@pytest.mark.asyncio
async def test_voice_mock_cleans_up_temp_file_on_failure(_env, monkeypatch):
    """Feedback loop: temp file is removed even when transcription fails."""
    daemon, stt_engine = _inject(_env, monkeypatch)
    bot = RecordingBot()

    used_path: list[str] = []

    async def failing_transcribe(path):
        used_path.append(path)
        raise RuntimeError("STT boom")

    stt_engine.transcribe = AsyncMock(side_effect=failing_transcribe)

    await _env.handle_voice(_voice_update(), SimpleNamespace(bot=bot))

    assert used_path, "transcribe must have been called with a path"
    assert not os.path.exists(used_path[0]), "temp file must not leak on failure"
    # The user is told the agent is busy rather than left silent.
    assert bot.sent == [(CHAT_ID, _env.AGENT_BUSY_TEXT)]