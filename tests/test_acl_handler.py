"""E2T1c: the @require_acl-decorated handle_message wires ACL at the entry.

Verify the bare-form decorator on gateway.handlers.handle_message binds the
allowlist from ALLOWED_USERS (env) and that calling the real handler with an
unauthorized user halts before any daemon RPC can be made.
"""
from types import SimpleNamespace

import pytest

ALLOWED = {111, 222}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    # ensure gateway.handlers is re-imported with the desired allowlist bound
    import importlib

    import gateway.handlers as handlers
    importlib.reload(handlers)
    return handlers


def _update_with(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


@pytest.mark.asyncio
async def test_handler_blocks_unauthorized_before_any_rpc(_env, monkeypatch):
    # A stand-in for the future daemon RPC: must never be touched.
    rpc_called = []

    monkeypatch.setattr(
        "gateway.handlers.logger.info", lambda *a, **k: rpc_called.append("rpc")
    )

    result = await _env.handle_message(_update_with(999), SimpleNamespace())

    assert result is None
    # The handler body (where RPC lives) never ran.
    assert rpc_called == []


@pytest.mark.asyncio
async def test_handler_admits_authorized_user(_env, monkeypatch):
    rpc_called = []
    monkeypatch.setattr(
        "gateway.handlers.logger.info", lambda *a, **k: rpc_called.append("rpc")
    )

    await _env.handle_message(_update_with(111), SimpleNamespace())

    assert rpc_called == ["rpc"]  # proceeded to the handler body