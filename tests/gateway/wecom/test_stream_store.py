import asyncio
import pytest

from gateway.platforms.wecom.stream_store import StreamStore


@pytest.mark.asyncio
async def test_create_and_get_stream():
    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    stream_id = store.create_stream()
    stream = store.get_stream(stream_id)
    assert stream is not None
    assert stream.stream_id == stream_id
    assert stream.finished is False


@pytest.mark.asyncio
async def test_debounce_flush_calls_handler():
    flushed = []

    async def flush_handler(pending):
        flushed.append(pending.stream_id)

    store = StreamStore(flush_handler=flush_handler)
    stream_id, status = store.add_pending_message(
        conversation_key="wecom:u1:c1",
        target=None,
        msg={"msgid": "m1", "text": {"content": "hello"}},
        msg_content="hello",
        nonce="n1",
        timestamp="t1",
        debounce_ms=50,
    )
    assert status == "active_new"
    await asyncio.sleep(0.1)
    assert len(flushed) == 1
    assert flushed[0] == stream_id


def test_ack_streams_for_batch():
    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    store.add_ack_stream_for_batch("batch-1", "ack-1")
    store.add_ack_stream_for_batch("batch-1", "ack-2")
    assert store.drain_ack_streams_for_batch("batch-1") == ["ack-1", "ack-2"]


@pytest.mark.asyncio
async def test_active_reply_store_save_and_retrieve():
    from gateway.platforms.wecom.stream_store import ActiveReplyStore

    store = ActiveReplyStore()
    await store.save("chat-1", "https://wecom.example.com/push/123", policy="once")

    reply = await store.retrieve("chat-1")
    assert reply is not None
    assert reply["url"] == "https://wecom.example.com/push/123"
    assert reply["policy"] == "once"

    # Once policy: second retrieve should return None
    second = await store.retrieve("chat-1")
    assert second is None


@pytest.mark.asyncio
async def test_active_reply_store_multi_policy():
    from gateway.platforms.wecom.stream_store import ActiveReplyStore

    store = ActiveReplyStore()
    await store.save("chat-1", "https://wecom.example.com/push/123", policy="multi")

    # Multi policy: can retrieve multiple times
    assert await store.retrieve("chat-1") is not None
    assert await store.retrieve("chat-1") is not None

    # Expire and verify cleanup
    await store.expire("chat-1")
    assert await store.retrieve("chat-1") is None


@pytest.mark.asyncio
async def test_active_reply_store_has_reply():
    from gateway.platforms.wecom.stream_store import ActiveReplyStore

    store = ActiveReplyStore()
    assert await store.has_reply("chat-1") is False

    await store.save("chat-1", "https://wecom.example.com/push/123")
    assert await store.has_reply("chat-1") is True

    await store.expire("chat-1")
    assert await store.has_reply("chat-1") is False


def test_stream_is_near_timeout():
    import time
    from gateway.platforms.wecom.stream_store import StreamState

    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    stream = StreamState(stream_id="s1", started=True, started_at=time.time() - 310)
    store._streams["s1"] = stream
    assert store.is_near_timeout("s1", timeout_seconds=360, margin_seconds=60) is True
    assert store.is_near_timeout("s2", timeout_seconds=360, margin_seconds=60) is False
