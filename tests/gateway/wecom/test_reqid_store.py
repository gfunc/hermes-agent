import json
import tempfile
from pathlib import Path

import pytest

from gateway.platforms.wecom.reqid_store import ReqIdStore


class TestReqIdStore:
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReqIdStore(Path(tmpdir) / "reqids.json")
            store.set("chat-1", "req-abc")
            store.set("chat-2", "req-def")
            store.save()

            # Simulate restart: new store instance, same file
            store2 = ReqIdStore(Path(tmpdir) / "reqids.json")
            assert store2.get("chat-1") == "req-abc"
            assert store2.get("chat-2") == "req-def"

    def test_get_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReqIdStore(Path(tmpdir) / "reqids.json")
            assert store.get("nonexistent") is None

    def test_ttl_cleanup_removes_old_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReqIdStore(Path(tmpdir) / "reqids.json", ttl_seconds=0)
            store.set("chat-1", "req-old")
            store.save()

            store2 = ReqIdStore(Path(tmpdir) / "reqids.json", ttl_seconds=0)
            assert store2.get("chat-1") is None  # expired immediately

    def test_preserve_case(self):
        """chatId case must be preserved (WeCom chat IDs are case-sensitive)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReqIdStore(Path(tmpdir) / "reqids.json")
            store.set("Chat-ABC", "req-1")
            store.save()

            store2 = ReqIdStore(Path(tmpdir) / "reqids.json")
            assert store2.get("Chat-ABC") == "req-1"
            assert store2.get("chat-abc") is None  # case-sensitive
