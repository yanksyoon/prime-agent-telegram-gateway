"""E2T2: DaemonRPCClient.send_message helper round-trips text over JSONL.

Makes the E2T2 text path real, not just the mock's protocol: ``send_message``
must forward the user's text to the daemon under the chat's session and return
the daemon's plain reply text parsed from the JSONL response.
"""
import asyncio
import json

import pytest

from gateway.daemon_client import DaemonRPCClient


@pytest.mark.asyncio
async def test_send_message_returns_daemon_reply_text():
    received = asyncio.Queue()

    async def daemon(reader, writer):
        line = await reader.readline()
        await received.put(json.loads(line))
        writer.write(json.dumps({"text": "World"}).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(daemon, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = DaemonRPCClient()
        await client.connect(host="127.0.0.1", port=port)

        reply = await client.send_message("sess-1", "Hello")

        assert reply == "World"
        sent = received.get_nowait()
        assert sent == {"type": "message", "session_id": "sess-1", "text": "Hello"}

        await client.close()
    finally:
        server.close()
        await server.wait_closed()