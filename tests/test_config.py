"""G0: config.py skeleton loads ALLOWED_USERS, daemon socket path, bot token.

Behavior under test:
- defaults for the daemon socket path when env is empty
- __init__ reads from the process environment
- get_config() builds a fresh Config from the current env
- ALLOWED_USERS is parsed from a comma/space separated list into ints
"""

from gateway.config import DEFAULT_DAEMON_SOCKET_PATH, Config, get_config


def test_default_daemon_socket_path_when_unset(monkeypatch):
    monkeypatch.delenv("DAEMON_SOCKET_PATH", raising=False)
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    cfg = Config()
    assert cfg.daemon_socket_path == DEFAULT_DAEMON_SOCKET_PATH


def test_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:TESTTOKEN")
    monkeypatch.setenv("DAEMON_SOCKET_PATH", "/tmp/unit-test.sock")
    monkeypatch.setenv("ALLOWED_USERS", "111, 222, 333")
    cfg = get_config()
    assert cfg.bot_token == "123:TESTTOKEN"
    assert cfg.daemon_socket_path == "/tmp/unit-test.sock"
    assert cfg.allowed_users == {111, 222, 333}


def test_allowed_users_parses_space_separated_ints(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "1 2 3")
    cfg = get_config()
    assert cfg.allowed_users == {1, 2, 3}


def test_bot_token_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    cfg = get_config()
    assert cfg.bot_token == ""