"""WeCom websocket client helpers (missed-pong tracking)."""

from __future__ import annotations

import asyncio
from typing import Optional


class WeComWSClient:
    """Tracks application-level heartbeat / missed-pong state.

    The official Tencent SDK counts missed pongs and force-closes the
    connection after a threshold.  WeCom does not send explicit pong
    frames; any inbound frame for the ping's ``req_id`` counts as a pong.
    """

    def __init__(
        self,
        adapter_name: str,
        max_missed_pongs: int = 2,
    ):
        self._adapter_name = adapter_name
        self._max_missed_pongs = max_missed_pongs
        self._missed_pongs: int = 0
        self._ping_req_ids: set[str] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def missed_pongs(self) -> int:
        return self._missed_pongs

    async def register_ping(self, req_id: str) -> None:
        async with self._lock:
            self._ping_req_ids.add(req_id)

    async def on_pong(self, req_id: str) -> bool:
        """Return True if ``req_id`` was a pending ping and clear it."""
        async with self._lock:
            if req_id in self._ping_req_ids:
                self._ping_req_ids.discard(req_id)
                self._missed_pongs = 0
                return True
            return False

    async def on_any_frame(self) -> None:
        """Reset counters on any inbound traffic (defense in depth)."""
        async with self._lock:
            self._missed_pongs = 0
            self._ping_req_ids.clear()

    async def increment_missed(self) -> bool:
        """Count any pings still in flight as missed.  Returns True if threshold exceeded."""
        async with self._lock:
            if self._ping_req_ids:
                self._missed_pongs += 1
                self._ping_req_ids.clear()
            return self._missed_pongs > self._max_missed_pongs
