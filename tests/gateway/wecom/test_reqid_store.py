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

    def test_save_uses_atomic_write(self):
        """save() must not leave temp files behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reqids.json"
            store = ReqIdStore(path)
            store.set("chat-1", "req-1")
            store.save()

            assert path.exists()
            assert not path.with_suffix(".tmp").exists()

    def test_set_does_not_auto_save(self):
        """set() must mark dirty but not write to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reqids.json"
            store = ReqIdStore(path)
            store.set("chat-1", "req-1")
            # File should not exist yet — set() must not auto-save
            assert not path.exists()

            store.save()
            store2 = ReqIdStore(path)
            assert store2.get("chat-1") == "req-1"

    def test_save_is_noop_when_not_dirty(self):
        """save() must skip disk write when nothing has changed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reqids.json"
            store = ReqIdStore(path)
            assert store.is_dirty is False
            store.set("chat-1", "req-1")
            assert store.is_dirty is True
            store.save()
            assert store.is_dirty is False

            # Track whether a write happens by checking file modification time
            stat_before = path.stat()
            store.save()  # not dirty — should be a no-op
            stat_after = path.stat()
            assert stat_before.st_mtime == stat_after.st_mtime
