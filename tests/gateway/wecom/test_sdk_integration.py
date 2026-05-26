"""Tests for WeCom SDK integration (wecom-aibot-python-sdk)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.wecom import WeComAdapter


@pytest.mark.asyncio
async def test_sdk_client_initialization():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom.adapter.WSClient") as MockClient, \
         patch("gateway.platforms.wecom.adapter.WSClientOptions") as MockOptions:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        MockClient.return_value = mock_client

        with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", True):
            await adapter.connect()

        MockClient.assert_called_once()
        MockOptions.assert_called_once()
        mock_client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_streaming_uses_sdk():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._sdk_client = MagicMock()
    adapter._sdk_client.reply_stream = AsyncMock(return_value={"errcode": 0})

    with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", True):
        result = await adapter.send_stream_chunk("sid-1", "hello", finish=False)

    assert result.success is True
    adapter._sdk_client.reply_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_streaming_without_sdk_returns_error():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._sdk_client = None

    with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", False):
        result = await adapter.send_stream_chunk("sid-1", "hello", finish=False)

    assert result.success is False
    assert "SDK client not connected" in result.error


@pytest.mark.asyncio
async def test_disconnect_closes_sdk_client():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    mock_sdk = MagicMock()
    adapter._sdk_client = mock_sdk
    adapter._http_client = None

    await adapter.disconnect()

    mock_sdk.disconnect.assert_called_once()
    assert adapter._sdk_client is None


@pytest.mark.asyncio
async def test_on_sdk_message_stores_last_frame():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req-123"},
        "body": {"msgtype": "text", "text": {"content": "hello"}},
    }

    with patch.object(adapter, "_on_message", new=AsyncMock()):
        await adapter._on_sdk_message(payload)

    assert adapter._last_inbound_frame == payload


@pytest.mark.asyncio
async def test_connect_fails_when_sdk_not_installed():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", False):
        result = await adapter.connect()

    assert result is False
    assert adapter.has_fatal_error is True
    assert "wecom-aibot-python-sdk not installed" in adapter.fatal_error_message


def test_supports_native_streaming():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", True):
        adapter._sdk_client = MagicMock()
        assert adapter.supports_native_streaming() is True

        adapter._sdk_client = None
        assert adapter.supports_native_streaming() is False

    with patch("gateway.platforms.wecom.adapter._SDK_AVAILABLE", False):
        adapter._sdk_client = MagicMock()
        assert adapter.supports_native_streaming() is False
