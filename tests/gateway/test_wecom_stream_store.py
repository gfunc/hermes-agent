import asyncio
import pytest

from gateway.platforms.wecom_stream_store import StreamStore


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
    assert store.drain_ack_streams_for_batch("batch-1") == []
