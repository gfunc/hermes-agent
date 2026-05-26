import time
import pytest
from gateway.platforms.wecom.stream_store import StreamStore


def test_prune_removes_finished_streams():
    store = StreamStore(flush_handler=lambda x: None, ttl_seconds=1)
    store.add_pending_message("conv-1", None, {"msgid": "msg-1"}, "content", "nonce", "ts")
    store.mark_finished(store.get_stream_by_msgid("msg-1"))
    time.sleep(1.1)
    store._prune()
    assert store.get_stream_by_msgid("msg-1") is None


def test_prune_removes_unfinished_stale_streams():
    store = StreamStore(flush_handler=lambda x: None, ttl_seconds=1)
    store.add_pending_message("conv-1", None, {"msgid": "msg-1"}, "content", "nonce", "ts")
    # Stream is unfinished but older than 2x TTL
    time.sleep(2.1)
    store._prune()
    assert store.get_stream_by_msgid("msg-1") is None


def test_prune_keeps_recent_unfinished_streams():
    store = StreamStore(flush_handler=lambda x: None, ttl_seconds=3600)
    store.add_pending_message("conv-1", None, {"msgid": "msg-1"}, "content", "nonce", "ts")
    store._prune()
    assert store.get_stream_by_msgid("msg-1") is not None


def test_max_active_streams_eviction():
    store = StreamStore(flush_handler=lambda x: None, ttl_seconds=3600, max_active_streams=3)
    for i in range(5):
        store.add_pending_message(f"conv-{i}", None, {"msgid": f"msg-{i}"}, "content", "nonce", "ts")

    # Oldest unfinished streams should be evicted
    assert len(store._streams) == 3
    assert store.get_stream_by_msgid("msg-0") is None
    assert store.get_stream_by_msgid("msg-1") is None
    assert store.get_stream_by_msgid("msg-2") is not None
