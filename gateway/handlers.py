"""E2T1: main Telegram message handler with the ACL gate applied.

The ACL middleware is the absolute first step: ``handle_message`` is wrapped
by ``@require_acl`` (bare form), which binds the allowlist from
``ALLOWED_USERS`` in the environment at decoration time and halts any update
whose ``effective_user.id`` is not allowed BEFORE this function body (and any
daemon RPC it will later make, see E2T2) can run.
"""

from __future__ import annotations

import logging

from gateway.acl import require_acl

logger = logging.getLogger(__name__)


@require_acl
async def handle_message(update, context):
    """Handle an inbound Telegram message.

    ACL is enforced by the decorator before this body is reached. The
    message-to-daemon roundtrip (session fetch, daemon RPC, Telegram reply)
    is implemented in E2T2; this module is where that logic will live.
    """
    logger.info("ACL allow: message from user_id=%s proceeding", _user_id(update))
    return None


def _user_id(update):
    return getattr(getattr(update, "effective_user", None), "id", None)