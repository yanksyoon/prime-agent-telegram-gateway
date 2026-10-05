"""E2T1: strict inbound ACL middleware.

``@require_acl`` gates a Telegram update handler so that the ``effective_user``
id is checked against the allowlist before the wrapped handler (the daemon RPC
path) can ever run. This is the RCE-critical boundary: Prime Agent executes
code with host-user permissions, so an unauthorized message must never reach
any downstream logic.

Design notes:
- The check is the absolute first statement inside the wrapper; the wrapped
  handler is only reached after the id is confirmed present AND allowed.
- ``None`` effective user (e.g. a channel_post without a sender) is denied:
  fail closed, never open.
- Strict ``int`` comparison against ``set[int]``. A string id like ``"111"``
  will not accidentally match int ``111``; the warning logs the raw value and
  its type so a valid-user blockage from a type mismatch is debuggable
  (project_init.md Task 2.1 feedback loop).
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Set, Union

from gateway.config import get_config

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]


def _wrap(func: Handler, allowed_users: Set[int]) -> Handler:
    @functools.wraps(func)
    async def guarded(update: Any, context: Any) -> Any:
        effective_user = getattr(update, "effective_user", None)
        user_id = getattr(effective_user, "id", None)

        if user_id is None or user_id not in allowed_users:
            logger.warning(
                "ACL DENY: blocking update (user_id=%r, type=%s) not in ALLOWED_USERS",
                user_id,
                type(user_id).__name__,
                extra={"update": update},
            )
            return None  # halt processing; daemon RPC (wrapped func) never runs

        return await func(update, context)

    return guarded


def require_acl(
    allowed_users: Union[Set[int], Handler, None] = None,
) -> Union[Handler, Callable[[Handler], Handler]]:
    """Decorator factory for the ACL gate.

    Two forms:
      @require_acl(allowed_users={111, 222})  # explicit allowlist
      @require_acl                            # bare; allowlist from Config/.env
    The bare form binds the allowlist once at decoration time from the process
    environment (``ALLOWED_USERS`` via ``gateway.config.get_config``).
    """
    if callable(allowed_users):
        # Used bare: @require_acl applied directly to the handler.
        return _wrap(allowed_users, get_config().allowed_users)

    users = (
        allowed_users if allowed_users is not None else get_config().allowed_users
    )
    return lambda func: _wrap(func, users)