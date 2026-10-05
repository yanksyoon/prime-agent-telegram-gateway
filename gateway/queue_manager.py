"""E4T1: per-chat async message queue.

Prime Agent's daemon is strictly turn-based PER SESSION: two messages for the
same ``chat_id`` processed concurrently will corrupt the session state. So we
keep one :class:`asyncio.Queue` per ``chat_id`` plus one background worker
loop per chat that pulls items off the queue and processes them one at a time.
Different ``chat_id`` values are independent and may proceed concurrently, so
each chat gets its own isolated queue and worker.

Queue growth is bounded via ``maxsize``. When a chat's queue is full we drop
the newest item (a blocked gateway must not let the event loop back up
forever); the handler surfaces a user-facing "Queue full, please wait"
message. A worker never dies on a bad item: it catches per-item exceptions,
logs them, and keeps draining, so a failing daemon cannot wedge the queue.

Only NON-daemon-visible helpers live here; the actual per-item work (session
resolve, daemon RPC, Telegram reply) is injected as a ``process(item)``
callable so this module stays generic and trivially testable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

ProcessFn = Callable[[Any], Awaitable[None]]


class ChatQueueManager:
    """Holds ``chat_id -> asyncio.Queue`` and drives one worker per chat."""

    def __init__(self, maxsize: int = 0) -> None:
        self.maxsize = maxsize
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    # -- queues ----------------------------------------------------------
    def get_queue(self, chat_id: str) -> asyncio.Queue:
        """Return the queue for ``chat_id``, creating it on first use."""
        queue = self._queues.get(chat_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.maxsize)
            self._queues[chat_id] = queue
        return queue

    def enqueue_nowait(self, chat_id: str, item: Any) -> bool:
        """Put ``item`` without blocking; return False if the queue is full.

        A full queue means the worker cannot keep up (e.g. the daemon is
        wedged). We drop the newest item rather than grow the queue forever;
        the caller is responsible for notifying the user.
        """
        try:
            self.get_queue(chat_id).put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    # -- workers ---------------------------------------------------------
    def ensure_worker(self, chat_id: str, process: ProcessFn) -> asyncio.Task:
        """Spawn the background worker for ``chat_id`` if none is running.

        Called lazily on the first enqueue for a chat; a finished or cancelled
        worker is re-created so a chat's queue never stalls permanently.
        """
        worker = self._workers.get(chat_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self.run_worker(chat_id, process))
            self._workers[chat_id] = worker
        return worker

    async def run_worker(self, chat_id: str, process: ProcessFn) -> None:
        """Run forever, pulling one item at a time and processing it.

        One item is fully handled (including its awaited daemon turn) before
        the next is pulled, which is exactly the per-session serialization
        Epic 4 requires. A worker survives a failing item in the same way a
        daemon survives a failing session: log it and keep going.
        """
        queue = self.get_queue(chat_id)
        while True:
            item = await queue.get()
            try:
                await process(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad item must not kill the worker loop
                logger.exception("queue worker error for chat_id=%s", chat_id)
            finally:
                queue.task_done()

    def cancel_worker(self, chat_id: str) -> None:
        """Cancel and forget the worker for ``chat_id`` (test/teardown seam)."""
        worker = self._workers.pop(chat_id, None)
        if worker is not None:
            worker.cancel()