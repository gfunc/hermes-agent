"""Tests for WeCom WebSocket reliability improvements."""

import asyncio
from unittest.mock import AsyncMock

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


async def test_connect_cancels_tasks_when_discovery_raises(monkeypatch):
    """
    If _discover_mcp_configs() raises after listen/heartbeat tasks are created,
    connect() must cancel and null them out to avoid dangling tasks.
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

    async def fake_open():
        return None

    async def fake_discover():
        await asyncio.sleep(0)
        raise RuntimeError("discovery failed")

    async def fake_listen():
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def fake_heartbeat():
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    adapter._open_connection = fake_open
    adapter._discover_mcp_configs = fake_discover
    adapter._listen_loop = fake_listen
    adapter._heartbeat_loop = fake_heartbeat

    success = await adapter.connect()
    assert success is False
    assert adapter._listen_task is None
    assert adapter._heartbeat_task is None


async def test_websocket_uses_tcp_keepalive(monkeypatch):
    import socket
    import gateway.platforms.wecom as wecom_module
    from gateway.platforms.wecom import WeComAdapter

    monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)

    class DummyClient:
        async def aclose(self):
            return None

    monkeypatch.setattr(
        wecom_module,
        "httpx",
        type("httpx", (), {"AsyncClient": lambda **kwargs: DummyClient()})(),
    )

    adapter = WeComAdapter(
        PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
    )

    sock_opts = {}

    class FakeSocket:
        family = socket.AF_INET

        def setsockopt(self, level, optname, value):
            sock_opts[(level, optname)] = value

    class FakeTransport:
        def get_extra_info(self, name):
            if name == "socket":
                return FakeSocket()
            return None

    class FakeWS:
        closed = False

        def get_extra_info(self, name, default=None):
            if name == "socket":
                return FakeTransport().get_extra_info(name)
            return default

        async def send_json(self, payload):
            pass

        async def receive(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(adapter, "_wait_for_handshake", AsyncMock(return_value={"errcode": 0}))

    session_mock = AsyncMock()
    session_mock.ws_connect = AsyncMock(return_value=FakeWS())
    session_mock.closed = False
    session_mock.close = AsyncMock()

    monkeypatch.setattr(
        wecom_module,
        "aiohttp",
        type("aiohttp", (), {
            "ClientSession": lambda *args, **kwargs: session_mock,
            "WSMsgType": type("WSMsgType", (), {
                "TEXT": 1, "CLOSE": 2, "CLOSED": 3, "ERROR": 4, "PING": 5, "PONG": 6
            }),
        })()
    )

    await adapter._open_connection()

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE) in sock_opts
    assert sock_opts[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    assert (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) in sock_opts
    assert sock_opts[(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)] == 30
