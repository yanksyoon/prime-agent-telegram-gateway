"""E2T1b: @require_acl lets an authorized user proceed to the next step."""
from types import SimpleNamespace

import pytest

from gateway.acl import require_acl

ALLOWED = {111, 222}


def _update_with(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


@pytest.mark.asyncio
async def test_authorized_user_proceeds_to_next_step():
    downstream_calls = []

    async def downstream(update, context):
        downstream_calls.append(update.effective_user.id)
        return "next-step-result"

    gated = require_acl(ALLOWED)(downstream)

    result = await gated(_update_with(111), SimpleNamespace())

    assert downstream_calls == [111]  # proceeded to the wrapped handler
    assert result == "next-step-result"  # return value propagates


@pytest.mark.asyncio
async def test_any_member_of_allowlist_is_admitted():
    downstream_calls = []

    async def downstream(update, context):
        downstream_calls.append(update.effective_user.id)

    gated = require_acl(ALLOWED)(downstream)

    await gated(_update_with(222), SimpleNamespace())
    assert downstream_calls == [222]