"""E1T1b: the client splits rapid JSONL messages by newline, no merged JSON.

Fire three payloads at the mock server back-to-back, then have the server
blast all three replies in a SINGLE buffer. The client must split them on
'\\n' and parse three distinct dicts in order, not one mangled blob.
"""

import asyncio
import json

import pytest

from gateway.daemon_client import DaemonRPCClient

PAYLOADS = [
    {"type": "request", "seq": 1, "text": "first"},
    {"type": "request", "seq": 2, "text": "second"},
    {"type": "request", "seq": 3, "text": "third"},
]

REPLIES = [
    {"type": "response", "seq": 1, "ok": True},
    {"type": "response", "seq": 2, "ok": True},
    {"type": "response", "seq": 3, "ok": True},
]


@pytest.mark.asyncio
async def test_client_splits_rapid_jsonl_into_distinct_dicts():
    received = asyncio.Queue()

    async def echo_handler(reader, writer):
        for _ in range(len(PAYLOADS)):
            line = await reader.readline()
            if not line:
                break
            await received.put(json.loads(line))
        # All three replies arrive as one blob to stress newline framing.
        blob = "".join(json.dumps(r) + "\n" for r in REPLIES).encode("utf-8")
        writer.write(blob)
        await writer.drain()
        await writer.wait_closed()

    server = await asyncio.start_server(echo_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = DaemonRPCClient()
        await client.connect(host="127.0.0.1", port=port)

        for payload in PAYLOADS:  # 3 rapid sends
            await client.send(payload)

        responses = [await client.recv(), await client.recv(), await client.recv()]
        assert responses == REPLIES

        got = []
        while not received.empty():
            got.append(received.get_nowait())
        assert got == PAYLOADS

        await client.close()
    finally:
        server.close()
        await server.wait_closed()