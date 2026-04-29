"""Per-chat serial inbound message queue for ordered processing."""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)

Handler = Callable[[str, Any], Awaitable[None]]


class ChatSerialQueue:
    """Ensures ordered processing of messages within a chat, parallel across chats."""

    _SHUTDOWN_SENTINEL = object()

    def __init__(self, handler: Handler):
        self._handler = handler
        self._queues: Dict[str, asyncio.Queue] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def enqueue(self, chat_id: str, payload: Any) -> None:
        """Enqueue a message for serial processing."""
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            queue = self._queues.get(chat_id)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[chat_id] = queue
                self._tasks[chat_id] = asyncio.create_task(self._worker(chat_id))
            await queue.put(payload)

    async def _worker(self, chat_id: str) -> None:
        """Process messages for a single chat serially."""
        queue = self._queues.get(chat_id)
        if queue is None:
            return
        try:
            while True:
                payload = await queue.get()
                if payload is self._SHUTDOWN_SENTINEL:
                    queue.task_done()
                    break
                try:
                    await self._handler(chat_id, payload)
                except Exception:
                    logger.exception("[wecom][chat_queue] Handler failed for chat=%s", chat_id)
                finally:
                    queue.task_done()
        finally:
            self._queues.pop(chat_id, None)
            self._tasks.pop(chat_id, None)
            self._locks.pop(chat_id, None)

    async def drain(self, chat_id: str) -> None:
        """Wait until all messages for a chat are processed."""
        lock = self._locks.get(chat_id)
        if lock is None:
            return
        async with lock:
            queue = self._queues.get(chat_id)
            if queue is None:
                return
            await queue.put(self._SHUTDOWN_SENTINEL)
            await queue.join()
            task = self._tasks.get(chat_id)
            if task and not task.done():
                await task
