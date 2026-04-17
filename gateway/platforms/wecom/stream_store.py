from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StreamState:
    stream_id: str
    content: str = ""
    finished: bool = False
    started: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    user_id: str = ""
    chat_type: str = "direct"
    chat_id: str = ""
    aibotid: str = ""
    task_key: str = ""
    images: List[Dict[str, str]] = field(default_factory=list)
    fallback_mode: str = ""
    fallback_prompt_sent_at: Optional[float] = None
    dm_content: str = ""
    agent_media_keys: List[str] = field(default_factory=list)


@dataclass
class PendingBatch:
    conversation_key: str
    stream_id: str
    target: Any
    msg: Dict[str, Any]
    contents: List[str]
    msgids: List[str]
    nonce: str
    timestamp: str
    timer: Optional[asyncio.Task] = None


@dataclass
class PendingInbound:
    stream_id: str
    target: Any
    msg: Dict[str, Any]
    contents: List[str]
    msgids: List[str]
    conversation_key: str
    batch_key: str


class StreamStore:
    def __init__(
        self,
        flush_handler: Callable[[PendingInbound], Any],
        ttl_seconds: float = 600,
    ):
        self._flush_handler = flush_handler
        self._ttl_seconds = ttl_seconds
        self._streams: Dict[str, StreamState] = {}
        self._msgid_to_stream: Dict[str, str] = {}
        self._batches: Dict[str, PendingBatch] = {}
        self._ack_streams: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._prune_task: Optional[asyncio.Task] = None
        self._prune_interval_ms: float = 30000

    async def _acquire(self):
        await self._lock.acquire()

    def _release(self):
        self._lock.release()

    def create_stream(self, msgid: Optional[str] = None) -> str:
        stream_id = f"stream-{uuid.uuid4().hex}"
        self._streams[stream_id] = StreamState(stream_id=stream_id)
        if msgid:
            self._msgid_to_stream[msgid] = stream_id
        return stream_id

    def get_stream(self, stream_id: str) -> Optional[StreamState]:
        return self._streams.get(stream_id)

    def update_stream(self, stream_id: str, updater: Callable[[StreamState], None]) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            updater(stream)

    def mark_started(self, stream_id: str) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            stream.started = True
            if stream.started_at is None:
                stream.started_at = time.time()

    def mark_finished(self, stream_id: str) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            stream.finished = True

    def is_near_timeout(
        self,
        stream_id: str,
        timeout_seconds: float = 360,
        margin_seconds: float = 60,
    ) -> bool:
        stream = self._streams.get(stream_id)
        if not stream or not stream.started:
            return False
        started_at = stream.started_at or stream.created_at
        elapsed = time.time() - started_at
        return elapsed >= (timeout_seconds - margin_seconds)

    def get_stream_by_msgid(self, msgid: str) -> Optional[str]:
        return self._msgid_to_stream.get(msgid)

    def set_stream_id_for_msgid(self, msgid: str, stream_id: str) -> None:
        self._msgid_to_stream[msgid] = stream_id

    def add_pending_message(
        self,
        conversation_key: str,
        target: Any,
        msg: Dict[str, Any],
        msg_content: str,
        nonce: str,
        timestamp: str,
        debounce_ms: float = 300,
    ) -> Tuple[str, str]:
        batch = self._batches.get(conversation_key)
        if batch is not None:
            batch.contents.append(msg_content)
            if msg.get("msgid"):
                batch.msgids.append(str(msg["msgid"]))
            return batch.stream_id, "active_merged"

        stream_id = self.create_stream(msgid=msg.get("msgid"))
        batch = PendingBatch(
            conversation_key=conversation_key,
            stream_id=stream_id,
            target=target,
            msg=msg,
            contents=[msg_content],
            msgids=[str(msg["msgid"])] if msg.get("msgid") else [],
            nonce=nonce,
            timestamp=timestamp,
        )
        self._batches[conversation_key] = batch
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        batch.timer = loop.create_task(
            self._flush_after(batch, debounce_ms)
        )
        return stream_id, "active_new"

    async def _flush_after(self, batch: PendingBatch, debounce_ms: float) -> None:
        await asyncio.sleep(debounce_ms / 1000)
        self._batches.pop(batch.conversation_key, None)
        pending = PendingInbound(
            stream_id=batch.stream_id,
            target=batch.target,
            msg=batch.msg,
            contents=batch.contents,
            msgids=batch.msgids,
            conversation_key=batch.conversation_key,
            batch_key=batch.conversation_key,
        )
        try:
            result = self._flush_handler(pending)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("[StreamStore] Flush handler failed for stream=%s", batch.stream_id)

    def add_ack_stream_for_batch(self, batch_stream_id: str, ack_stream_id: str) -> None:
        self._ack_streams.setdefault(batch_stream_id, []).append(ack_stream_id)

    def drain_ack_streams_for_batch(self, batch_stream_id: str) -> List[str]:
        return self._ack_streams.pop(batch_stream_id, [])

    def start_pruning(self, interval_ms: float) -> None:
        self._prune_interval_ms = interval_ms
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = asyncio.get_running_loop().create_task(self._prune_loop())

    def stop_pruning(self) -> None:
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
            self._prune_task = None

    async def _prune_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._prune_interval_ms / 1000)
                self._prune()
        except asyncio.CancelledError:
            pass

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        expired = [sid for sid, s in self._streams.items() if s.finished and s.created_at < cutoff]
        for sid in expired:
            self._streams.pop(sid, None)
        expired_msgids = [mid for mid, sid in self._msgid_to_stream.items() if sid in expired]
        for mid in expired_msgids:
            self._msgid_to_stream.pop(mid, None)
