"""Tests for GatewayStreamConsumer native streaming support."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import (
    GatewayStreamConsumer,
    StreamConsumerConfig,
)


def _make_native_capable_adapter(*, supports_native: bool = True):
    """Build a minimal BasePlatformAdapter subclass with native streaming support.

    The runtime subclass + cleared __abstractmethods__ pattern lets us
    construct an adapter without hauling in any platform's heavy state
    while still satisfying the consumer's isinstance(BasePlatformAdapter) gate.
    """
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    NativeCapableAdapter = type(
        "NativeCapableAdapter",
        (BasePlatformAdapter,),
        {"MAX_MESSAGE_LENGTH": 4096},
    )
    NativeCapableAdapter.__abstractmethods__ = frozenset()
    adapter = NativeCapableAdapter.__new__(NativeCapableAdapter)
    adapter._typing_paused = set()
    adapter._fatal_error_message = None

    adapter.supports_native_streaming = lambda: bool(supports_native)

    adapter.send_stream_chunk = AsyncMock(
        return_value=SendResult(success=True, message_id="msg-1"),
    )
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="msg_real"),
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True),
    )
    return adapter


# ---------------------------------------------------------------------------
# _resolve_native_streaming
# ---------------------------------------------------------------------------

class TestResolveNativeStreaming:
    def test_transport_edit_returns_false(self):
        adapter = _make_native_capable_adapter(supports_native=True)
        cfg = StreamConsumerConfig(transport="edit")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is False

    def test_transport_off_returns_false(self):
        adapter = _make_native_capable_adapter(supports_native=True)
        cfg = StreamConsumerConfig(transport="off")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is False

    def test_transport_auto_adapter_supports_returns_true(self):
        adapter = _make_native_capable_adapter(supports_native=True)
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is True

    def test_transport_auto_adapter_declines_returns_false(self):
        adapter = _make_native_capable_adapter(supports_native=False)
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is False

    def test_transport_draft_adapter_supports_returns_true(self):
        """Native streaming should also be checked for 'draft' transport."""
        adapter = _make_native_capable_adapter(supports_native=True)
        cfg = StreamConsumerConfig(transport="draft")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is True

    def test_adapter_probe_raises_returns_false(self):
        adapter = _make_native_capable_adapter(supports_native=True)
        adapter.supports_native_streaming = lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is False

    def test_magicmock_adapter_returns_false(self):
        """MagicMocks (test adapters) default to False."""
        adapter = MagicMock()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        assert consumer._resolve_native_streaming() is False


# ---------------------------------------------------------------------------
# _send_native_chunk
# ---------------------------------------------------------------------------

class TestSendNativeChunk:
    @pytest.mark.asyncio
    async def test_successful_send(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"

        ok = await consumer._send_native_chunk("hello", finalize=False)

        assert ok is True
        adapter.send_stream_chunk.assert_awaited_once_with(
            stream_id="stream-abc", content="hello", finish=False,
        )
        assert consumer._last_sent_text == "hello"
        assert consumer._already_sent is True
        assert consumer._final_content_delivered is False

    @pytest.mark.asyncio
    async def test_successful_finalize(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"

        ok = await consumer._send_native_chunk("final text", finalize=True)

        assert ok is True
        assert consumer._final_content_delivered is True

    @pytest.mark.asyncio
    async def test_failure_disables_native(self):
        from gateway.platforms.base import SendResult
        adapter = _make_native_capable_adapter()
        adapter.send_stream_chunk = AsyncMock(
            return_value=SendResult(success=False, error="stream timeout"),
        )
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"

        ok = await consumer._send_native_chunk("hello", finalize=False)

        assert ok is False
        assert consumer._use_native_streaming is False

    @pytest.mark.asyncio
    async def test_exception_disables_native(self):
        adapter = _make_native_capable_adapter()
        adapter.send_stream_chunk = AsyncMock(side_effect=ConnectionError("boom"))
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"

        ok = await consumer._send_native_chunk("hello", finalize=False)

        assert ok is False
        assert consumer._use_native_streaming is False

    @pytest.mark.asyncio
    async def test_no_stream_id_disables_native(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = None

        ok = await consumer._send_native_chunk("hello", finalize=False)

        assert ok is False
        assert consumer._use_native_streaming is False


# ---------------------------------------------------------------------------
# _send_or_edit routing
# ---------------------------------------------------------------------------

class TestSendOrEditNativeRouting:
    @pytest.mark.asyncio
    async def test_routes_to_native_when_enabled(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"

        ok = await consumer._send_or_edit("hello world", finalize=False)

        assert ok is True
        adapter.send_stream_chunk.assert_awaited_once()
        adapter.send.assert_not_called()
        adapter.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_native_no_op_when_same_text(self):
        """Identical text should be a no-op when not finalizing."""
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"
        consumer._last_sent_text = "hello world"

        ok = await consumer._send_or_edit("hello world", finalize=False)

        assert ok is True
        adapter.send_stream_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_native_finalize_even_when_same_text(self):
        """finalize=True should still send even if text hasn't changed."""
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"
        consumer._last_sent_text = "hello world"

        ok = await consumer._send_or_edit("hello world", finalize=True)

        assert ok is True
        adapter.send_stream_chunk.assert_awaited_once_with(
            stream_id="stream-abc", content="hello world", finish=True,
        )
        assert consumer._final_response_sent is True

    @pytest.mark.asyncio
    async def test_native_failure_falls_back_to_edit(self):
        """When native fails, _send_or_edit falls through to edit-based path."""
        from gateway.platforms.base import SendResult
        adapter = _make_native_capable_adapter()
        adapter.send_stream_chunk = AsyncMock(
            return_value=SendResult(success=False, error="boom"),
        )
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        consumer._native_stream_id = "stream-abc"
        consumer._message_id = "msg-1"

        ok = await consumer._send_or_edit("hello world", finalize=False)

        assert ok is True
        # Native was attempted
        adapter.send_stream_chunk.assert_awaited_once()
        # Then fell through to edit
        adapter.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_native_not_used_when_disabled(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="edit")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._message_id = "msg-1"

        ok = await consumer._send_or_edit("hello world", finalize=False)

        assert ok is True
        adapter.send_stream_chunk.assert_not_called()
        adapter.edit_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration: run() with native streaming
# ---------------------------------------------------------------------------

class TestRunWithNativeStreaming:
    @pytest.mark.asyncio
    async def test_run_uses_native_streaming(self):
        """End-to-end: consumer resolves native, sends deltas, finalizes."""
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto", buffer_only=True)
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)

        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Hello")
        consumer.on_delta(" world")
        await asyncio.sleep(0.05)
        consumer.finish()
        await task

        # Should have resolved native streaming
        assert consumer._use_native_streaming is True
        assert consumer._native_stream_id is not None
        # Should have sent stream chunks (at least one for the accumulated text)
        assert adapter.send_stream_chunk.await_count >= 1
        # The final finalize=True call should set _final_response_sent
        assert consumer._final_response_sent is True

    @pytest.mark.asyncio
    async def test_run_fallback_when_native_fails_midstream(self):
        """Native fails mid-stream; consumer falls back to edit/send."""
        from gateway.platforms.base import SendResult
        adapter = _make_native_capable_adapter()
        adapter.send_stream_chunk = AsyncMock(
            return_value=SendResult(success=False, error="boom"),
        )
        cfg = StreamConsumerConfig(transport="auto", buffer_only=True)
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)

        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Hello world")
        await asyncio.sleep(0.05)
        consumer.finish()
        await task

        # Native was attempted and disabled
        assert consumer._use_native_streaming is False
        # Fallback to regular send
        adapter.send.assert_awaited()
        assert consumer._final_response_sent is True


# ---------------------------------------------------------------------------
# Segment break with native streaming
# ---------------------------------------------------------------------------

class TestNativeStreamingSegmentBreak:
    def test_segment_break_generates_new_stream_id(self):
        adapter = _make_native_capable_adapter()
        cfg = StreamConsumerConfig(transport="auto")
        consumer = GatewayStreamConsumer(adapter, "chat-1", config=cfg)
        consumer._use_native_streaming = True
        old_id = consumer._native_stream_id = "stream-old"

        consumer._reset_segment_state()

        assert consumer._native_stream_id != "stream-old"
        assert consumer._native_stream_id is not None
