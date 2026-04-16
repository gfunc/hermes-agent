"""Tests for WeCom WebSocket reliability improvements."""

import asyncio

import pytest

from gateway.config import PlatformConfig

pytestmark = pytest.mark.asyncio


async def test_mock_server_starts_and_stops():
    from tests.gateway.mock_wecom_server import MockWeComServer

    server = MockWeComServer()
    await server.start()
    assert server.ws_url.startswith("http://127.0.0.1:")
    await server.stop()


async def test_connect_does_not_block_on_mcp_discovery(monkeypatch):
    """
    MCP discovery must happen AFTER the listen loop starts.
    Previously _discover_mcp_configs() was called before _listen_task
    was created, so _send_request futures timed out (15s x 6 = 90s).
    """
    import gateway.platforms.wecom as wecom_module
    from gateway.platforms.wecom import WeComAdapter

    monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)

    class DummyClient:
        async def aclose(self):
            return None

    class DummyHttpx:
        def AsyncClient(self, **kwargs):
            return DummyClient()

    monkeypatch.setattr(wecom_module, "httpx", DummyHttpx())

    adapter = WeComAdapter(
        PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
    )

    open_called = asyncio.Event()
    discover_called = asyncio.Event()
    listen_started = asyncio.Event()

    async def fake_open():
        open_called.set()

    async def fake_discover():
        discover_called.set()
        # Yield so the event loop can start the listen/heartbeat tasks
        await asyncio.sleep(0)
        # By the time discover runs, listen loop must already exist
        assert adapter._listen_task is not None
        assert adapter._heartbeat_task is not None

    async def fake_listen():
        listen_started.set()

    adapter._open_connection = fake_open
    adapter._discover_mcp_configs = fake_discover
    adapter._listen_loop = fake_listen
    async def fake_heartbeat():
        await asyncio.sleep(0)

    adapter._heartbeat_loop = fake_heartbeat

    success = await adapter.connect()
    assert success is True
    assert open_called.is_set()
    assert listen_started.is_set()
    assert discover_called.is_set()

    await adapter.disconnect()
