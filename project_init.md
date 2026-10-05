# 📋 Prime Agent Telegram Gateway: Ticket Backlog

## 🏛️ Epic 1: Foundation & Daemon RPC Bridge
*Goal: Establish a reliable, mockable, and resilient communication channel between the new gateway service and the Prime Agent `pa-daemon`.*

### 🎫 Task 1.1: Implement Mockable Daemon JSONL Socket Client
* **Research (Hermes)**: Hermes uses `python-telegram-bot` with custom HTTPX requests. Prime Agent uses a local JSONL-framed socket. Direct socket manipulation is prone to framing errors.
* **Adjustment**: Do not write raw socket code from scratch. Use an existing async JSONL stream library or build a thin wrapper around `asyncio.StreamReader/StreamWriter` with strict newline-delimited JSON parsing.
* **Target Goal**: A Python class `DaemonRPCClient` that can connect, send a JSON payload, and parse the JSONL response without blocking.
* **Verification Tasks**:
  1. `test_rpc_mock_connect.py`: Assert client connects to a mocked `asyncio` server.
  2. `test_rpc_framing.py`: Send 3 rapid JSON messages; assert the client correctly splits them by `\n` and parses 3 distinct dictionaries.
* **Development Steps**:
  1. Scaffold `daemon_client.py` with `connect()`, `send(payload)`, and `close()`.
  2. Implement newline-delimited JSON buffering.
  3. Write the mock server test first, then implement the client.
* **Feedback Loop**: If framing tests fail (e.g., merged JSON), add a strict buffer accumulation loop until `\n` is encountered before `json.loads()`.

### 🎫 Task 1.2: Implement Session Creation & ID Mapping
* **Research (Hermes)**: Hermes maps `chat_id` to SQLite `session_id`. Prime Agent daemon creates sessions and returns a unique ID.
* **Adjustment**: The gateway must persist this mapping locally (JSON or SQLite) to survive gateway restarts without orphaning Prime Agent daemon sessions.
* **Target Goal**: A `SessionManager` that checks local storage for a `chat_id`, and if missing, calls `DaemonRPCClient` to create one and saves the mapping.
* **Verification Tasks**:
  1. `test_session_cache_hit.py`: Mock daemon; request session for existing `chat_id`; assert daemon `create_session` is *not* called.
  2. `test_session_cache_miss.py`: Mock daemon; request session for new `chat_id`; assert daemon `create_session` *is* called and mapping is saved to disk.
* **Development Steps**:
  1. Create `session_store.py` with `get_or_create_session(chat_id: str) -> str`.
  2. Integrate with `DaemonRPCClient` mock.
  3. Add disk persistence (e.g., `sqlite3` or `json`).
* **Feedback Loop**: If disk locking issues occur, switch to WAL-mode SQLite or implement a simple file lock.

---

## 🏛️ Epic 2: Core Messaging & Security
*Goal: Securely route text messages from Telegram to the correct Prime Agent session and return the response.*

### 🎫 Task 2.1: Strict Inbound ACL Middleware
* **Research (Hermes)**: Hermes drops unauthorized messages silently at the adapter level *before* the LLM sees them.
* **Adjustment**: Prime Agent executes code with host user permissions. A bypass here is a critical RCE risk. The ACL check must be the absolute first step in the update handler.
* **Target Goal**: A middleware/decorator `@require_acl` that halts processing and logs a warning if `update.effective_user.id` is not in `ALLOWED_USERS`.
* **Verification Tasks**:
  1. `test_acl_block.py`: Feed update with unauthorized `user_id`; assert handler returns early and daemon RPC is *never* called.
  2. `test_acl_pass.py`: Feed update with authorized `user_id`; assert handler proceeds to the next step.
* **Development Steps**:
  1. Define `ALLOWED_USERS` in `config.py` (loaded from `.env`).
  2. Write the `@require_acl` decorator.
  3. Apply to the main Telegram message handler.
* **Feedback Loop**: If the decorator accidentally blocks valid users, add a debug log printing the exact `user_id` type (int vs. string) to catch type-mismatch bugs.

### 🎫 Task 2.2: Basic Text Roundtrip (Mocked)
* **Research (Hermes)**: Hermes sends text to the core loop and streams the response back.
* **Adjustment**: For guaranteed success, decouple Telegram API from Prime Agent. Mock the daemon's response first.
* **Target Goal**: When an authorized user sends "Hello", the gateway sends "Hello" to the daemon mock, receives "World", and calls Telegram's `sendMessage` with "World".
* **Verification Tasks**:
  1. `test_text_roundtrip.py`: Use `respx` to mock Telegram API and `AsyncMock` for daemon. Assert `daemon.send_message` receives "Hello" and `telegram_bot.send_message` receives "World".
* **Development Steps**:
  1. Create the main `handle_message(update, context)` function.
  2. Fetch `session_id` via `SessionManager`.
  3. Call `daemon_client.send_message(session_id, text)`.
  4. Pass the result to `context.bot.send_message`.
* **Feedback Loop**: If the mock daemon returns an error, ensure the gateway catches it and sends a polite "Agent is busy or encountered an error" to Telegram, rather than crashing.

---

## 🏛️ Epic 3: Advanced Telegram Features
*Goal: Replicate Hermes' quality-of-life features (commands, media) without over-scoping.*

### 🎫 Task 3.1: Slash Command Interception & Routing
* **Research (Hermes)**: Hermes dynamically registers commands and routes them. Prime Agent has specific control commands (e.g., `/refine`, `/status`).
* **Adjustment**: Do not build dynamic registration yet. Start with a static, hardcoded list of 3 commands to guarantee success.
* **Target Goal**: Intercept `/refine` and `/status`. Send a structured control payload to the daemon instead of raw text.
* **Verification Tasks**:
  1. `test_slash_intercept.py`: Send `/refine`; assert daemon receives `{"type": "control", "command": "refine"}`.
  2. `test_normal_text_ignores_slash.py`: Send "I like /refine"; assert daemon receives it as raw text, not a control command.
* **Development Steps**:
  1. Add `if text.startswith('/'):` check in `handle_message`.
  2. Parse command and arguments.
  3. Route to `daemon_client.send_control_command()`.
* **Feedback Loop**: If argument parsing fails (e.g., `/refine with this`), ensure the split logic correctly separates the command from the payload.

### 🎫 Task 3.2: Voice Note Transcription (Mocked STT)
* **Research (Hermes)**: Hermes downloads `.ogg`, runs `faster-whisper`, and injects text.
* **Adjustment**: Integrating real Whisper is a high-risk, environment-dependent task. **Scope this task to Mocked STT only.** Real STT becomes a separate, later task.
* **Target Goal**: When a `message.voice` is received, download the file, pass the path to a `mock_stt_engine`, and send the returned string to the daemon.
* **Verification Tasks**:
  1. `test_voice_mock.py`: Mock Telegram `get_file` and `download`. Mock `stt_engine.transcribe` to return "Mocked text". Assert daemon receives "Mocked text".
* **Development Steps**:
  1. Add `handle_voice(update, context)` handler.
  2. Download file to a temporary directory (`tempfile`).
  3. Call `stt_engine.transcribe(file_path)`.
  4. Reuse the `handle_message` logic with the transcribed text.
* **Feedback Loop**: If the temp file isn't cleaned up, add a `finally: os.remove(file_path)` block to prevent disk leaks.

### 🎫 Task 3.3: Outbound File Path Detection & Upload
* **Research (Hermes)**: Hermes uses `MEDIA:/path` tags. Prime Agent outputs raw text, which may contain paths like `/tmp/output.csv`.
* **Adjustment**: Use a strict regex to detect absolute paths ending in known extensions (`.csv`, `.pdf`, `.txt`) at the *end* of the daemon's response.
* **Target Goal**: If daemon returns "Done. File saved to `/tmp/test.pdf`", the gateway intercepts this, sends the text "Done.", and separately calls `sendDocument` with `/tmp/test.pdf`.
* **Verification Tasks**:
  1. `test_file_detect_and_send.py`: Mock daemon returning a string with `/tmp/fake.csv`. Assert `send_message` is called with the text, AND `send_document` is called with the path.
  2. `test_file_missing.py`: Mock daemon returning a path that does not exist on disk. Assert `send_document` is *not* called, and a warning is logged.
* **Development Steps**:
  1. Write regex: `r'(/\w+[/\w\-\.]+\.(?:csv|pdf|txt|json))$'`.
  2. In the response handler, check for match.
  3. If match and `os.path.exists(path)`, split the message and trigger `send_document`.
* **Feedback Loop**: If regex is too greedy, refine it to only match paths that start with `/tmp/` or the current working directory to prevent accidental exposure of system files.

---

## 🏛️ Epic 4: Concurrency & Resilience
*Goal: Prevent race conditions, dropped updates, and state corruption under load.*

### 🎫 Task 4.1: Per-Chat Async Message Queue
* **Research (Hermes)**: Hermes processes different chats concurrently, but the *same* chat sequentially to preserve context.
* **Adjustment**: Prime Agent's daemon is strictly turn-based per session. Concurrent messages to the same `chat_id` will corrupt the session state.
* **Target Goal**: Implement an `asyncio.Queue` per `chat_id`. A background worker per chat pulls from the queue and processes messages one by one.
* **Verification Tasks**:
  1. `test_queue_sequential.py`: Use `asyncio.gather` to push 3 messages for the *same* `chat_id` simultaneously. Assert the mock daemon's `send_message` is called 3 times, with strictly increasing timestamps (no overlap).
* **Development Steps**:
  1. Create a `ChatQueueManager` that holds a dict of `chat_id: asyncio.Queue`.
  2. Create a `process_queue(chat_id)` async loop that runs indefinitely.
  3. Modify `handle_message` to `await queue.put(update)` instead of processing directly.
* **Feedback Loop**: If queues grow infinitely on daemon failure, add a `maxsize` to the queue and implement a timeout/drop mechanism with a user-facing "Queue full, please wait" message.

### 🎫 Task 4.2: Telegram Update ID Deduplication
* **Research (Hermes)**: Hermes caches the last 4096 `update_id`s to prevent retry loops.
* **Adjustment**: Keep it simple. Store the last processed `update_id` per `chat_id` in memory.
* **Target Goal**: If the gateway receives an `update_id` it has already processed for a given `chat_id`, it acknowledges it to Telegram but does *not* forward it to the daemon.
* **Verification Tasks**:
  1. `test_dedup_ignore.py`: Send update with `id=100`. Send it again. Assert daemon is only called once.
* **Development Steps**:
  1. Add `processed_updates: dict[int, int]` (chat_id -> last_update_id) to `ChatQueueManager`.
  2. At the start of `handle_message`, check `if update_id <= last_processed: return`.
  3. Update `last_processed` *after* successful queue insertion.
* **Feedback Loop**: If memory grows, implement a simple LRU cache or limit the dict to the last 1000 chat IDs.

### 🎫 Task 4.3: Graceful Shutdown & Daemon Detach
* **Research (Hermes)**: Hermes gateway can stop without killing the underlying agent logic.
* **Adjustment**: The gateway must tell the daemon to "detach" on `SIGTERM`, leaving the Prime Agent session alive in the background.
* **Target Goal**: Catch `SIGTERM`/`SIGINT`, send a `detach` RPC command to all active sessions, close the socket, and exit cleanly.
* **Verification Tasks**:
  1. `test_graceful_shutdown.py`: Start gateway, send a message, send `SIGTERM` to the gateway process. Assert the mock daemon received a `detach` command for the active session.
* **Development Steps**:
  1. Register `signal.signal(signal.SIGTERM, shutdown_handler)`.
  2. In `shutdown_handler`, iterate over active `chat_id`s and call `daemon_client.detach(session_id)`.
  3. Call `application.stop()` (for `python-telegram-bot`).
* **Feedback Loop**: If the daemon doesn't receive the detach signal due to abrupt kills, document that this is a known limitation of `SIGKILL` and requires the user to use `systemctl stop` or standard termination.

---

## 🔄 Execution Protocol for the Agent

For **every single ticket** above, the assigned agent must follow this exact loop:

1. **Read the Ticket**: Understand the narrow scope. If it feels too broad, *stop* and break it down further before writing code.
2. **Write the Verification Test First**: Create the `test_*.py` file. Run it. **It must fail.**
3. **Implement Minimal Code**: Write only the code required to make that specific test pass. No "while I'm here" feature creep.
4. **Run Verification**: The test **must pass**.
5. **Refactor (Optional)**: Clean up the code, ensuring the test still passes.
6. **Commit and push the code incrementally**: Push the finalized code to the repository with appropriate commits.
7. **Mark Ticket Complete**: Move to the next ticket.
