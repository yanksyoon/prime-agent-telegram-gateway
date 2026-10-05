# Prime Agent Telegram Gateway

Bridges Telegram chat to the *Prime Agent* daemon over a local JSONL-framed
Unix socket. An authorized user sends a message, the gateway resolves (or
creates) a Prime Agent session for that chat, forwards the message to the
daemon, and relays the daemon's reply back to Telegram.

This project is built on the board-backlog in `project_init.md` (Epics 1-4).
This README documents the project **as built** — real module names, real env
vars, real commands.

---

## Table of contents

- [System requirements](#system-requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Pointing at the Prime Agent daemon socket](#pointing-at-the-prime-agent-daemon-socket)
- [Usage](#usage)
- [Interaction model](#interaction-model)
- [Security & reliability model](#security--reliability-model)
- [Running the tests](#running-the-tests)
- [Layout](#layout)
- [Status](#status)

---

## System requirements

- **Python >= 3.11** (`requires-python = ">=3.11"` in `pyproject.toml`).
- **Runtime dependencies** (declared in `pyproject.toml`, installed
  automatically by `pip install -e .`):
  - `python-telegram-bot >= 21.0` — Telegram Bot API client.
  - `httpx >= 0.27` — HTTPX is python-telegram-bot's transport.
- **Development / test dependencies** (the `[dev]` extra):
  - `pytest >= 8.0`
  - `pytest-asyncio >= 0.23`
  - `respx >= 0.21` (mocks Telegram's HTTP API in tests)

There is no `requirements.txt`; dependencies live in `pyproject.toml`.

---

## Installation

1. **Create and activate a virtual environment.**

   ```bash
   cd prime-agent-telegram-gateway
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install the package and its runtime dependencies (editable).**

   ```bash
   pip install -e .
   ```

   For development, also install the test toolchain:

   ```bash
   pip install -e ".[dev]"
   ```

3. **Create your environment file.**

   The gateway reads configuration from the process environment. A minimal
   `.env` loader in `gateway/config.py` loads `KEY=VALUE` lines from a `.env`
   file in the current directory if it exists (it never overrides an env var
   that is already set).

   ```bash
   cp .env.example .env
   ```

   Then edit `.env` per [Configuration](#configuration).

---

## Configuration

`gateway/config.py` reads exactly these variables. Fill them in `.env` for the
values you want, or export them in your shell / service unit.

| Variable               | Required            | Default              | Meaning                                                                  |
| ---------------------- | ------------------- | -------------------- | ------------------------------------------------------------------------ |
| `BOT_TOKEN`            | Yes, for live runs  | `""` (empty)         | Telegram bot token from [@BotFather](https://t.me/BotFather).            |
| `ALLOWED_USERS`        | Yes                | `""` (empty set)      | Comma or space separated **numeric** Telegram user ids allowed to talk.  |
| `DAEMON_SOCKET_PATH`   | Yes for daemon link | `/tmp/pa-daemon.sock` | Filesystem path of the Prime Agent daemon's JSONL socket.                |
| `QUEUE_MAXSIZE`        | No                 | `10`                  | Per-chat in-flight async queue capacity (see Reliability model).         |

Notes:

- **`ALLOWED_USERS` is the security boundary.** Leave it empty and the gateway
  denies *everyone* (fail closed) — the ACL check is the first statement in the
  message handler and drops unauthorized updates before any daemon RPC or
  Telegram call happens. Use numeric ids, e.g. `ALLOWED_USERS=111222333`.
  A string id like `"111"` will **not** match int `111`.
- `BOT_TOKEN` / `DAEMON_SOCKET_PATH` may be left empty or defaulted for
  local/placeholder and test runs.

---

## Pointing at the Prime Agent daemon socket

`DAEMON_SOCKET_PATH` (default `/tmp/pa-daemon.sock`) is the local Unix domain
socket the Prime Agent daemon listens on. The gateway speaks strict
newline-delimited JSON (JSONL) over this socket:

- `{"type": "create_session", "chat_id": "<n>"}` → `{"session_id": "..."}`
- `{"type": "message", "session_id": "...", "text": "..."}` → `{"text": "..."}`
- `{"type": "control", "command": "refine", "args": "..."}` → `{"text": "..."}`
- `{"type": "detach", "session_id": "..."}` → ack (may drop the socket)

The client (`gateway/daemon_client.py`) wraps `asyncio.StreamReader/Writer`
with strict newline framing, so frames arriving back-to-back (even in the same
OS buffer) are never merged.

At runtime the gateway connects to whichever socket `DAEMON_SOCKET_PATH`
points at. Point it at the real daemon socket if the daemon runs there, or at a
mock/tunnel socket for local development.

---

## Usage

### Starting the gateway

The gateway entrypoint is the `gateway.app` module:

```bash
python -m gateway.app
```

On startup it loads config, connects to `DAEMON_SOCKET_PATH`, resolves a
session for a stand-in chat id, installs SIGTERM/SIGINT handlers, prints
`READY <session_id>`, and then waits. On SIGTERM/SIGINT it **detaches** the
active session(s) from the daemon (leaving the Prime Agent session alive in the
background), closes the socket, prints `STOPPED`, and exits `0`.

```bash
python -m gateway.app
# READY sess_<id>
# ... running ...
# (send SIGTERM or Ctrl-C)
# STOPPED
```

> **Not yet implemented:** wiring `python-telegram-bot`'s polling loop
> (`ApplicationBuilder` + `add_handler` + `run_polling`) into `main()` is a
> follow-up. Today the interactive handlers below live in `gateway/handlers.py`
> and are fully covered by unit tests, but the live Telegram polling loop that
> feeds them updates is not yet connected. Running `python -m gateway.app`
> exercises the daemon attach / detach lifecycle against a real socket.

### Starting the Prime Agent daemon

The daemon itself is **not** part of this repository. Run your Prime Agent
daemon so it listens on the socket named by `DAEMON_SOCKET_PATH`, then start
the gateway against it.

---

## Interaction model

For an **authorized** user, a plain Telegram message flows:

```
text --handle_message--> per-chat queue --> resolve/create session
        --> daemon.socket (JSONL "message") --> reply --> Telegram
```

1. **ACL gate** — `effective_user.id` must be in `ALLOWED_USERS`, else the
   update is dropped immediately.
2. **Enqueue** — the `(update, context)` is pushed onto that chat's async
   queue. Per chat, messages are processed strictly one at a time.
3. **Session resolve** — `SessionManager.get_or_create_session(chat_id)` looks
   up the chat in a SQLite store (`gateway/session_store.py`); on a miss it
   calls the daemon's `create_session` and persists the mapping (WAL mode), so
   gateway restarts never orphan daemon sessions.
4. **Daemon RPC + reply** — `daemon.send_message(session_id, text)` writes the
   JSONL frame and reads the reply, which is relayed back to Telegram.

### Control commands

A leading `/` routes a recognized control command to the daemon as a structured
payload instead of raw text. Recognized commands (static list in
`gateway/handlers.py`): **`/refine`**, **`/status`**, **`/clear`**.

- `/refine` and `/status` are the documented Prime Agent control commands.
- `/clear` is additionally recognized as the third control command.
- Unknown slash tokens (e.g. `/nope`) fall through to the plain-text path.
- A `/command` that merely appears mid-message (e.g. `"I like /refine"`) is
  treated as normal text, not a control command.
- `/refine with this` splits on the first space: command `refine`, args
  `with this` (passed verbatim as `args`).

### Voice notes (mocked STT)

A voice note is ACL-gated, downloaded to a temp `.ogg` file, run through the
(mock) STT engine, and the transcribed text is forwarded through the exact same
queue → daemon → reply path as a normal message. The temp file is always
removed (no disk leaks).

> **Mocked:** the current `MockSttEngine` returns a fixed placeholder string
> (`[voice transcription placeholder]`). Real STT (e.g. faster-whisper) is a
> separate later task; the seam (`gateway/stt.py` `SttEngine.transcribe`) is
> ready for it.

### Outbound file upload

If a daemon reply **ends** in an absolute path with a known extension
(`.csv`, `.pdf`, `.txt`, `.json`) **and** that file exists on disk, the
gateway:

1. sends the text up to the path (trailing path stripped),
2. uploads the file via `send_document`.

The match is anchored to the very *end* of the reply, so a path merely
mentioned mid-sentence never triggers an upload. If the path does not exist on
disk, the gateway logs a warning and sends the whole reply as plain text (it
never fabricates a document).

Example reply `Done. File saved to /tmp/output.csv` → a message "Done." plus an
uploaded `output.csv`.

### Errors

- A failing daemon RPC (gateway can't reach it) sends the user exactly:
  `Agent is busy or encountered an error. Please try again in a moment.`
- A chat queue that backs up (daemon wedged) drops the newest message and sends:
  `Queue full, please wait.`

---

## Security & reliability model

- **Strict inbound ACL.** `@require_acl` is the absolute first statement in the
  message handler. A user not in `ALLOWED_USERS` (or a `None` effective user)
  is denied and the update never reaches the daemon or Telegram — this is the
  RCE-critical boundary since Prime Agent executes code as the host user.
- **Message serialization per chat.** Prime Agent's daemon is strictly
  turn-based per session, so concurrent messages for the same chat would
  corrupt session state. One `asyncio.Queue` + one background worker per chat
  processes them strictly one at a time; different chats are independent.
- **Update dedup.** The last processed `update_id` per chat is remembered.
  A Telegram redelivery with an id already processed is acknowledged (the
  handler returns normally) but never forwarded to the daemon. The id is only
  recorded *after* a successful queue insert, so a dropped (queue-full) message
  is never falsely marked processed.
- **Graceful shutdown / detach.** On SIGTERM/SIGINT the gateway tells the daemon
  to *detach* each active session, leaving the agent logic alive in the
  background, closes the socket, and exits 0. **Known limitation:** SIGKILL
  aborts the process before any detach can run, orphaning the daemon session.
  Operators must use standard termination (`systemctl stop`, or any signal
  path) so SIGTERM/SIGINT is actually delivered.
- **Persistent session store.** `gateway/session_store.py` uses SQLite in WAL
  mode with `chat_id` as the primary key, so writes are idempotent and a crash
  cannot corrupt the mapping.

---

## Running the tests

```bash
pip install -e ".[dev]"
python -m pytest
```

Config-driven test settings come from `pyproject.toml` (`asyncio_mode = "auto"`,
`testpaths = ["tests"]`). The suite mocks the daemon socket and the Telegram
HTTP API (via `respx`), so no live token or daemon is required. Current status:
**40 tests pass / 0 fail.**

---

## Layout

```
gateway/
  __init__.py       package marker + __version__
  config.py         Config + minimal .env loader + get_config()
  daemon_client.py  DaemonRPCClient: JSONL socket connect/send/recv/close
  session_store.py  SessionManager: chat_id -> session_id (SQLite WAL)
  acl.py            @require_acl middleware (strict allowlist gate)
  handlers.py       handle_message / handle_voice + per-chat queue worker
  queue_manager.py  ChatQueueManager: one queue + worker per chat, dedup
  stt.py            SttEngine protocol + MockSttEngine (placeholder)
  app.py            GatewayApp + main() entrypoint (attach/READY/detach)
tests/              19 test files, incl. queue, dedup, shutdown, full flow
conftest.py         root path bootstrap for pytest
pyproject.toml      package metadata + runtime/dev dependencies
.env.example        env var template (copy to .env)
project_init.md     the board-backlog source of truth (Epics 1-4)
qa_report_final.md  Epic close-out QA gate report
```

---

## Status

Epics 1-4 are implemented, committed to `origin/main`, and gated green by a QA
report (`qa_report_final.md`): E1 (daemon RPC bridge, session store),
E2 (ACL + text roundtrip), E3 (commands, mocked voice STT, file upload),
E4 (per-chat queue, dedup, graceful shutdown/detach). See the
[not-yet-implemented note](#usage) for the live Telegram polling-loop wiring
that remains as follow-up.