import pytest
from unittest.mock import MagicMock

from gateway.platforms.base import BasePlatformAdapter


class DummyAdapter(BasePlatformAdapter):
    pass


def test_supports_native_streaming_defaults_to_false():
    adapter = DummyAdapter(MagicMock(), "test")
    assert adapter.supports_native_streaming() is False


@pytest.mark.asyncio
async def test_send_stream_chunk_returns_not_supported():
    adapter = DummyAdapter(MagicMock(), "test")
    result = await adapter.send_stream_chunk("sid-1", "hello")
    assert result.success is False
    assert "Not supported" in result.error
