"""Voice notes in a topic flow through handle_message and reply in-topic.

handle_voice defers to handle_message with the same update, so the per-topic
conversation key and the outbound ``message_thread_id`` apply automatically.
This test proves a voice note sent in topic 42 is resolved to that topic's
session and its reply is delivered with ``message_thread_id=42``.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_store import SessionManager

CHAT = 777
THREAD = 42


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    import importlib

    import gateway.handlers as handlers

    importlib.reload(handlers)
    return handlers


def _voice_update(thread: int = THREAD):
    async def fake_download(custom_path, **kwargs):
        with open(custom_path, "wb") as fh:
            fh.write(b"OGG-BYTES")
        return custom_path

    voice = SimpleNamespace(
        file_id="F1",
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                download_to_drive=AsyncMock(side_effect=fake_download)
            )
        ),
    )
    # The voice message lives in a topic, so it carries a message_thread_id.
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=CHAT),
        message=SimpleNamespace(text=None, voice=voice, message_thread_id=thread),
    )


async def _stop_queue(_env, key: str) -> None:
    manager = _env._get_queues()
    queue = manager.get_queue(key)
    await asyncio.wait_for(queue.join(), timeout=2)
    manager.cancel_worker(key)


@pytest.mark.asyncio
async def test_voice_in_topic_reply_carries_thread(_env, monkeypatch):
    daemon = SimpleNamespace(
        create_session=AsyncMock(return_value="sess-1"),
        send_message=AsyncMock(return_value="World"),
    )
    store = SessionManager(daemon, db_path=":memory:")
    monkeypatch.setattr(_env, "_get_daemon", lambda: daemon)
    monkeypatch.setattr(_env, "_get_sessions", lambda: store)
    stt = SimpleNamespace(transcribe=AsyncMock(return_value="Mocked text"))
    monkeypatch.setattr(_env, "_get_stt", lambda: stt)

    bot = RecordingBot()
    await _env.handle_voice(_voice_update(), SimpleNamespace(bot=bot))
    await _stop_queue(_env, f"{CHAT}:{THREAD}")

    # The transcribed text reached the daemon on the topic's session.
    daemon.send_message.assert_awaited_once_with("sess-1", "Mocked text")

    # The reply landed back in the SAME topic (not the general one).
    assert bot.sent[0][0] == CHAT
    assert bot.sent[0][1] == "World"
    assert bot.sent[0][2].get("message_thread_id") == THREAD