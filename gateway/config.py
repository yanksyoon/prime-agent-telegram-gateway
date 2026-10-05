"""Application configuration.

Reads values from the process environment with minimal `.env` file support
(no third-party dependency, so it stays within the declared dependency set).
Later epics read ALLOWED_USERS (Epic 2 ACL), the daemon socket path (Epic 1
RPC client), and the bot token from a Config.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DAEMON_SOCKET_PATH = "/tmp/pa-daemon.sock"
DEFAULT_QUEUE_MAXSIZE = 10


def _load_dotenv(env_file: str = ".env") -> None:
    """Load KEY=VALUE lines into os.environ, only for keys not already set."""
    path = Path(env_file)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_allowed_users(raw: str) -> set[int]:
    """Parse a comma/space-separated list of Telegram numeric user ids."""
    tokens = raw.replace(",", " ").split()
    users: set[int] = set()
    for token in tokens:
        try:
            users.add(int(token))
        except ValueError:
            continue
    return users


class Config:
    """Gateway configuration.

    Plain attributes so tests can build Config(...) directly, while
    get_config() constructs one from the current process environment.
    """

    def __init__(
        self,
        bot_token: str | None = None,
        daemon_socket_path: str | None = None,
        allowed_users: set[int] | None = None,
        queue_maxsize: int | None = None,
    ) -> None:
        self.bot_token: str = (
            bot_token if bot_token is not None else os.getenv("BOT_TOKEN", "")
        )
        self.daemon_socket_path: str = (
            daemon_socket_path
            if daemon_socket_path is not None
            else os.getenv("DAEMON_SOCKET_PATH", DEFAULT_DAEMON_SOCKET_PATH)
        )
        self.allowed_users: set[int] = (
            allowed_users
            if allowed_users is not None
            else _parse_allowed_users(os.getenv("ALLOWED_USERS", ""))
        )
        self.queue_maxsize: int = (
            queue_maxsize
            if queue_maxsize is not None
            else int(os.getenv("QUEUE_MAXSIZE", str(DEFAULT_QUEUE_MAXSIZE)))
        )


_load_dotenv()


def get_config() -> Config:
    """Build a fresh Config from the current process environment."""
    return Config()