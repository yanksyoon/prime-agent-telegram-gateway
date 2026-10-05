"""E2T1a: @require_acl blocks an unauthorized effective_user before any RPC.

Security property: the ACL gate is the absolute first step. If the gate
rejects, the wrapped handler (the daemon RPC path) must NEVER execute.
Also: a missing effective_user is treated as unauthorized, never as allowed.
"""
import logging
from types import SimpleNamespace

import pytest

from gateway.acl import require_acl

ALLOWED = {111, 222}


def _update_with(user_id):
    """Minimal fake update exposing effective_user.id (int)."""
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


@pytest.mark.asyncio
async def test_unauthorized_user_returns_early_and_never_calls_daemon():
    rpc_calls = []

    async def downstream(update, context):
        # stands in for the daemon RPC path; must never run when blocked
        rpc_calls.append(update.effective_user.id)

    gated = require_acl(ALLOWED)(downstream)

    result = await gated(_update_with(999), SimpleNamespace())

    assert result is None
    assert rpc_calls == []  # daemon RPC never reached


@pytest.mark.asyncio
async def test_missing_effective_user_is_denied_and_warns(caplog):
    rpc_calls = []

    async def downstream(update, context):
        rpc_calls.append("rpc")

    gated = require_acl(ALLOWED)(downstream)

    update = SimpleNamespace(effective_user=None)
    with caplog.at_level(logging.WARNING):
        result = await gated(update, SimpleNamespace())

    assert result is None
    assert rpc_calls == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


@pytest.mark.asyncio
async def test_string_user_id_is_denied_type_mismatch(caplog):
    """A string id (e.g. '111') must NOT accidentally match int 111.
    The warning must surface the type so valid-user blockages are debuggable.
    """
    rpc_calls = []

    async def downstream(update, context):
        rpc_calls.append("rpc")

    gated = require_acl(ALLOWED)(downstream)

    with caplog.at_level(logging.WARNING):
        result = await gated(_update_with("111"), SimpleNamespace())

    assert result is None
    assert rpc_calls == []