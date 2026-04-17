"""Per-reqId serial reply queue for WeCom websocket sends."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


@dataclass
class _ReplyQueueItem:
    seq: int
    payload: Dict[str, Any]
    future: asyncio.Future
    timeout: float


class WeComReplyQueue:
    """Serializes outbound replies that share the same req_id.

    The official Tencent SDK (``@wecom/aibot-node-sdk``) uses a similar
    queue to prevent out-of-order delivery and frame overlap for the same
    inbound message.
    """

    def __init__(
        self,
        send_json_fn: Callable[[Dict[str, Any]], Any],
        pending_responses_dict: Dict[str, asyncio.Future],
        max_size: int = 500,
        idle_timeout: float = 1.0,
    ):
        self._send_json = send_json_fn
        self._pending_responses = pending_responses_dict
        self._max_size = max_size
        self._idle_timeout = idle_timeout
        self._queues: Dict[str, asyncio.Queue] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._seq_counters: Dict[str, int] = {}

    async def enqueue(
        self,
        req_id: str,
        payload: Dict[str, Any],
        timeout: float = 15.0,
    ) -> asyncio.Future:
        """Enqueue a reply and return a Future that resolves with the ack payload."""
        lock = self._locks.setdefault(req_id, asyncio.Lock())
        async with lock:
            queue = self._queues.get(req_id)
            if queue is None:
                queue = asyncio.Queue(maxsize=self._max_size)
                self._queues[req_id] = queue
                self._seq_counters[req_id] = 0
                task = asyncio.create_task(self._worker(req_id))
                self._tasks[req_id] = task
            seq = self._seq_counters[req_id] + 1
            self._seq_counters[req_id] = seq
            future = asyncio.get_running_loop().create_future()
            item = _ReplyQueueItem(seq=seq, payload=payload, future=future, timeout=timeout)
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                future.set_exception(RuntimeError("WeCom reply queue full"))
            return future

    async def _worker(self, req_id: str) -> None:
        logger.debug("[reply_queue] Worker started for req_id=%s", req_id)
        try:
            while True:
                queue = self._queues.get(req_id)
                if queue is None:
                    return
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(self._idle_timeout)
                    if queue.empty():
                        break
                    continue

                # Register this item's future in the shared pending responses
                # dict *only* for the duration of this single send.
                self._pending_responses[req_id] = item.future
                try:
                    try:
                        await self._send_json(item.payload)
                    except asyncio.CancelledError as exc:
                        if not item.future.done():
                            item.future.set_exception(exc)
                        continue
                    except Exception as exc:
                        # Handle synchronous send failure without killing the worker.
                        if not item.future.done():
                            item.future.set_exception(exc)
                        continue

                    try:
                        await asyncio.wait_for(
                            asyncio.shield(item.future), timeout=item.timeout
                        )
                    except asyncio.TimeoutError:
                        if not item.future.done():
                            item.future.set_exception(asyncio.TimeoutError())
                finally:
                    self._pending_responses.pop(req_id, None)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[reply_queue] Worker crashed for req_id=%s", req_id)
        finally:
            self._queues.pop(req_id, None)
            self._tasks.pop(req_id, None)
            self._locks.pop(req_id, None)
            self._seq_counters.pop(req_id, None)
            logger.debug("[reply_queue] Worker exited for req_id=%s", req_id)

    def fail_all(self, exc: Exception) -> None:
        """Fail all queued and in-flight items immediately."""
        for req_id, queue in list(self._queues.items()):
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not item.future.done():
                    item.future.set_exception(exc)
            task = self._tasks.get(req_id)
            if task and not task.done():
                task.cancel()
        # In-flight futures that are still registered in _pending_responses
        # are failed separately by the adapter's _fail_pending_responses.
