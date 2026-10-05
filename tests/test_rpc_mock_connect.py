"""E1T1a: DaemonRPCClient.connect() reaches a mocked asyncio server.

A mocked asyncio.start_server stands in for the real Prime Agent daemon.
The client must open the stream and exchange a JSONL handshake with it.
"""

import asyncio
import json

import pytest

from gateway.daemon_client import DaemonRPCClient

HANDSHAKE = {"type": "handshake", "version": 1}


@pytest.mark.asyncio
async def test_client_connects_to_mocked_asyncio_server():
    connected = asyncio.Event()

    async def handler(reader, writer):
        connected.set()
        writer.write(json.dumps(HANDSHAKE).encode("utf-8") + b"\n")
        await writer.drain()
        await reader.readline()  # keep serving until the client sends/closes
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = DaemonRPCClient()
        await client.connect(host="127.0.0.1", port=port)

        assert client.connected is True
        assert await asyncio.wait_for(connected.wait(), 2.0) is True

        reply = await client.recv()
        assert reply == HANDSHAKE

        await client.close()
        assert client.connected is False
    finally:
        server.close()
        await server.wait_closed()