"""E4T3: graceful shutdown & daemon detach (SIGTERM).

Verification contract (project_init.md Task 4.3):
  A gateway that has an active Prime Agent session, when told to shut down
  (SIGTERM/SIGINT), must send a ``detach`` RPC command to the daemon for that
  session BEFORE closing the socket and exiting cleanly, leaving the daemon
  session alive in the background. The gateway must be able to stop without
  killing the underlying agent logic.

Tests (written first, must fail, then the code under gateway/app.py is
implemented to make them pass):
  1. test_shutdown_detaches_active_sessions - in-process unit test that
     GatewayApp.shutdown() sends daemon.detach for every active session and
     then closes the RPC socket.
  2. test_shutdown_tolerates_detach_failure - one failing detach must not
     prevent the remaining detaches or the socket close (graceful degrade).
  3. test_installs_sigterm_handler - the SIGTERM/SIGINT handlers are registered
     on the event loop so an OS signal reliably triggers the shutdown path.
  4. test_sigterm_detaches_session_across_process - process-level: spawn the
     real ``gateway.app`` entrypoint as a subprocess, let it establish an active
     session with a mock daemon over a Unix socket, send SIGTERM to the gateway
     process, and assert the mock daemon received a ``detach`` command for that
     session and the gateway exited 0.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from gateway.app import GatewayApp, install_signal_handlers

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# In-process unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_detaches_active_sessions():
    """shutdown() detaches every active session, then closes the socket."""
    daemon = SimpleNamespace(detach=AsyncMock(), close=AsyncMock())
    app = GatewayApp(daemon, sessions=_unused())

    app.note_session("sess-A")
    app.note_session("sess-B")
    assert app.active_sessions == {"sess-A", "sess-B"}

    await app.shutdown()

    detached = {c.args[0] for c in daemon.detach.await_args_list}
    assert detached == {"sess-A", "sess-B"}
    assert daemon.detach.await_count == 2
    daemon.close.assert_awaited_once()
    assert app.active_sessions == set()  # cleared after detach


@pytest.mark.asyncio
async def test_shutdown_tolerates_detach_failure():
    """A failing daemon detach must not abort the remaining work."""
    daemon = SimpleNamespace(
        detach=AsyncMock(side_effect=RuntimeError("daemon down")),
        close=AsyncMock(),
    )
    app = GatewayApp(daemon, sessions=_unused())
    app.note_session("sess-X")

    await app.shutdown()  # must not raise even though detach raised

    assert daemon.detach.await_count == 1
    daemon.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_installs_sigterm_handler():
    """SIGTERM and SIGINT are both wired to the shutdown callback."""
    loop = asyncio.get_running_loop()
    registered: list[tuple[signal.Signals, object]] = []

    def fake_add_signal_handler(sig, cb):
        registered.append((sig, cb))

    await asyncio.sleep(0)  # keep the loop running
    loop = asyncio.get_running_loop()
    original = loop.add_signal_handler
    loop.add_signal_handler = fake_add_signal_handler  # type: ignore[method-assign]
    try:
        install_signal_handlers(loop, on_shutdown=_noop)
    finally:
        loop.add_signal_handler = original  # type: ignore[method-assign]

    sigs = {s for s, _ in registered}
    assert signal.SIGTERM in sigs
    assert signal.SIGINT in sigs
    assert all(cb is _noop for _, cb in registered)


def _noop() -> None:
    """Do-nothing callback used only to exercise signal registration."""


def _unused():
    """A stand-in dependency that GatewayApp.shutdown() never touches."""
    return SimpleNamespace(get_or_create_session=AsyncMock())


# ---------------------------------------------------------------------------
# Process-level test: real SIGTERM to the gateway subprocess
# ---------------------------------------------------------------------------

SOCK_DIR_REPLACED = "replaced_below"


@pytest.mark.asyncio
async def test_sigterm_detaches_session_across_process(tmp_path):
    """Spawn the gateway entrypoint, SIGTERM it, assert the daemon saw detach."""
    sock_path = str(tmp_path / "pa-daemon.sock")
    records: list[dict] = []

    server = await _run_mock_daemon(sock_path, records)
    proc = None
    try:
        env = dict(os.environ)
        env["DAEMON_SOCKET_PATH"] = sock_path
        proc = subprocess.Popen(
            [sys.executable, "-m", "gateway.app"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Gateway resolves an active session (the "sent message") and prints READY.
        ready = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline), timeout=15
        )
        assert ready.startswith("READY "), _diag(proc, f"unexpected ready line: {ready!r}")
        session_id = ready.strip().split()[1]

        # An active session roundtrip reached the mock daemon before shutdown.
        created = [r for r in records if r.get("type") == "create_session"]
        sent = [r for r in records if r.get("type") == "message"]
        assert created, f"daemon should have seen create_session, got {records!r}"
        assert sent, f"daemon should have seen the message, got {records!r}"

        # Send SIGTERM to the gateway process, exactly as systemd would.
        proc.send_signal(signal.SIGTERM)

        stopped = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline), timeout=15
        )
        assert "STOPPED" in stopped, _diag(proc, f"expected STOPPED, got {stopped!r}")
        rc = await asyncio.to_thread(proc.wait)
        assert rc == 0, _diag(proc, f"gateway exited non-zero rc={rc}")

        # The mock daemon received a detach for the active session.
        detaches = [r for r in records if r.get("type") == "detach"]
        assert detaches, f"daemon never got detach; records={records!r}"
        assert detaches[-1]["session_id"] == session_id
        assert created[0]["chat_id"] == "777"  # the startup chat that owns it
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        server.close()
        await server.wait_closed()


def _diag(proc, msg: str) -> str:
    stderr = ""
    if proc.stderr is not None:
        try:
            stderr = proc.stderr.read()
        except Exception:  # pragma: no cover - best-effort diagnostics
            stderr = "<unreadable:stderr>"
    return f"{msg}\n--- gateway stderr ---\n{stderr}"


async def _run_mock_daemon(sock_path: str, records: list[dict]):
    """Stand up a minimal JSONL daemon on ``sock_path`` that records every line.

    create_session -> {"session_id": "sess-1"}, send_message -> {"text": "World"},
    detach -> {"ok": True}, anything else -> {"ok": True}.
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                records.append(json.loads(raw))
                kind = records[-1].get("type")
                if kind == "create_session":
                    reply = {"session_id": "sess-1"}
                elif kind == "message":
                    reply = {"text": "World"}
                elif kind == "detach":
                    reply = {"ok": True}
                else:
                    reply = {"ok": True}
                writer.write(json.dumps(reply).encode("utf-8") + b"\n")
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    return await asyncio.start_unix_server(handler, path=sock_path)