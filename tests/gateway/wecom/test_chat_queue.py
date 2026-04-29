import pytest
import asyncio
from unittest.mock import AsyncMock

from gateway.platforms.wecom.chat_queue import ChatSerialQueue


@pytest.mark.asyncio
async def test_chat_queue_processes_messages_in_order():
    """Messages for the same chat must be processed in order, not concurrently."""
    processed = []

    async def handler(chat_id, payload):
        await asyncio.sleep(0.01)  # Simulate async work
        processed.append(payload)

    queue = ChatSerialQueue(handler)

    # Enqueue 3 messages for the same chat rapidly
    await queue.enqueue("chat-1", {"seq": 1})
    await queue.enqueue("chat-1", {"seq": 2})
    await queue.enqueue("chat-1", {"seq": 3})

    # Wait for all to complete
    await queue.drain("chat-1")

    assert processed == [{"seq": 1}, {"seq": 2}, {"seq": 3}]


@pytest.mark.asyncio
async def test_chat_queue_allows_concurrent_processing_across_chats():
    """Different chats should process in parallel."""
    active = set()
    max_concurrent = 0

    async def handler(chat_id, payload):
        active.add(chat_id)
        nonlocal max_concurrent
        max_concurrent = max(max_concurrent, len(active))
        await asyncio.sleep(0.05)
        active.discard(chat_id)

    queue = ChatSerialQueue(handler)

    await queue.enqueue("chat-a", {"msg": "a"})
    await queue.enqueue("chat-b", {"msg": "b"})
    await queue.enqueue("chat-c", {"msg": "c"})

    await queue.drain("chat-a")
    await queue.drain("chat-b")
    await queue.drain("chat-c")

    assert max_concurrent >= 3


@pytest.mark.asyncio
async def test_chat_queue_continues_on_handler_failure():
    """If a handler raises, subsequent messages for that chat must still be processed."""
    processed = []

    async def handler(chat_id, payload):
        if payload["seq"] == 2:
            raise RuntimeError("boom")
        processed.append(payload)

    queue = ChatSerialQueue(handler)

    await queue.enqueue("chat-1", {"seq": 1})
    await queue.enqueue("chat-1", {"seq": 2})
    await queue.enqueue("chat-1", {"seq": 3})

    await queue.drain("chat-1")

    assert processed == [{"seq": 1}, {"seq": 3}]


@pytest.mark.asyncio
async def test_chat_queue_no_message_loss_under_race():
    """Messages enqueued while worker is active must not be lost."""
    processed = []
    barrier = asyncio.Event()

    async def handler(chat_id, payload):
        if payload.get("pause"):
            barrier.set()
            await asyncio.sleep(0.1)
        processed.append(payload)

    queue = ChatSerialQueue(handler)

    # First message starts the worker and pauses it
    await queue.enqueue("chat-1", {"seq": 1, "pause": True})
    await barrier.wait()  # Worker is now sleeping

    # Enqueue more messages while worker is busy
    await queue.enqueue("chat-1", {"seq": 2})
    await queue.enqueue("chat-1", {"seq": 3})

    # Drain and verify all processed
    await queue.drain("chat-1")
    assert processed == [{"seq": 1, "pause": True}, {"seq": 2}, {"seq": 3}]
