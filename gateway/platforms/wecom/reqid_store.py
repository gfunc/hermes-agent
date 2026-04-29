"""Disk-backed JSON store for per-chat reply req_ids."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ReqIdStore:
    """Persists _last_reply_req_id_per_chat to disk with TTL cleanup.

    Uses original-case chatId keys (case-sensitive, matching WeCom).
    """

    def __init__(self, path: Path, ttl_seconds: float = 86400 * 7):
        self._path = path
        self._ttl_seconds = ttl_seconds
        self._data: Dict[str, Dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                now = time.time()
                self._data = {
                    chat_id: {"req_id": entry["req_id"], "ts": entry.get("ts", 0)}
                    for chat_id, entry in raw.items()
                    if isinstance(entry, dict)
                    and now - entry.get("ts", 0) < self._ttl_seconds
                }
        except (json.JSONDecodeError, OSError, KeyError):
            logger.warning("[%s] Failed to load reqid store from %s", __name__, self._path)
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            chat_id: {"req_id": entry["req_id"], "ts": entry["ts"]}
            for chat_id, entry in self._data.items()
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self._path)

    def get(self, chat_id: str) -> Optional[str]:
        entry = self._data.get(chat_id)
        if entry is None:
            return None
        if time.time() - entry["ts"] > self._ttl_seconds:
            self._data.pop(chat_id, None)
            return None
        return entry["req_id"]

    def set(self, chat_id: str, req_id: str) -> None:
        self._data[chat_id] = {"req_id": req_id, "ts": time.time()}

    def delete(self, chat_id: str) -> None:
        self._data.pop(chat_id, None)
