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
    assert (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) in sock_opts
    assert sock_opts[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] == 10
    assert (socket.IPPROTO_TCP, socket.TCP_KEEPCNT) in sock_opts
    assert sock_opts[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] == 3


async def test_watchdog_triggers_reconnect_on_silent_connection(monkeypatch):
    """
    If no websocket frame is received for WATCHDOG_TIMEOUT_SECONDS,
    the watchdog should close the connection so _listen_loop reconnects.
    """
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

    receive_calls = 0
    reconnect_attempts = 0

    class SilentWS:
        def __init__(self):
            self.closed = False
            self._event = asyncio.Event()

        async def send_json(self, payload):
            pass

        async def receive(self):
            nonlocal receive_calls
            receive_calls += 1
            await self._event.wait()
            raise RuntimeError("websocket closed")

        async def close(self):
            self.closed = True
            self._event.set()

    class SilentSession:
        closed = False

        async def ws_connect(self, *args, **kwargs):
            nonlocal reconnect_attempts
            reconnect_attempts += 1
            return SilentWS()

        async def close(self):
            self.closed = True

    adapter._session = SilentSession()
    adapter._ws = SilentWS()
    adapter._running = True
    adapter._last_frame_at = asyncio.get_running_loop().time()

    monkeypatch.setattr(wecom_module, "WATCHDOG_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(wecom_module, "RECONNECT_BACKOFF", [0.05])

    monkeypatch.setattr(adapter, "_wait_for_handshake", AsyncMock(return_value={"errcode": 0}))

    monkeypatch.setattr(
        wecom_module,
        "aiohttp",
        type("aiohttp", (), {
            "ClientSession": lambda *args, **kwargs: SilentSession(),
            "WSMsgType": type("WSMsgType", (), {
                "TEXT": 1, "CLOSE": 2, "CLOSED": 3, "ERROR": 4, "PING": 5, "PONG": 6
            }),
        })(),
    )

    listen_task = asyncio.create_task(adapter._listen_loop())
    watchdog_task = asyncio.create_task(adapter._watchdog_loop())
    await asyncio.sleep(0.5)
    adapter._running = False
    listen_task.cancel()
    watchdog_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass

    assert reconnect_attempts >= 2


async def test_apply_tcp_keepalive_fallback_when_keepidle_missing(monkeypatch):
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
            # Simulate missing TCP_KEEPIDLE by raising AttributeError for it
            if level == socket.IPPROTO_TCP and optname == getattr(socket, "TCP_KEEPIDLE", 4):
                raise AttributeError("TCP_KEEPIDLE")
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
    assert (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10)) in sock_opts
    assert sock_opts[(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10))] == 30


async def test_mock_server_receives_and_delivers_message():
    from tests.gateway.mock_wecom_server import MockWeComServer
    from gateway.platforms.wecom import WeComAdapter
    from gateway.config import PlatformConfig

    server = MockWeComServer()
    await server.start()
    try:
        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "bot_id": "bot-1",
                    "secret": "secret-1",
                    "websocket_url": server.ws_url,
                },
            )
        )

        received_payloads = []
        original_on_message = adapter._on_message

        async def _spy_on_message(payload):
            received_payloads.append(payload)
            # Do not await original to avoid full gateway plumbing

        adapter._on_message = _spy_on_message

        success = await adapter.connect()
        assert success is True

        # Give listen loop time to start
        await asyncio.sleep(0.1)

        # Verify subscription was received by mock server
        assert any(p.get("cmd") == "aibot_subscribe" for p in server._received)

        # Push a message from the mock server and verify adapter receives it
        await server.send_callback("chat-1", "hello from mock")
        await asyncio.sleep(0.1)

        assert len(received_payloads) >= 1
        body = received_payloads[0].get("body", {})
        assert body.get("chatid") == "chat-1"
        assert body.get("text", {}).get("content") == "hello from mock"

        await adapter.disconnect()
    finally:
        await server.stop()


async def test_adapter_reconnects_after_mock_server_closes():
    from tests.gateway.mock_wecom_server import MockWeComServer
    from gateway.platforms.wecom import WeComAdapter
    from gateway.config import PlatformConfig

    server = MockWeComServer(scenario="close_after_auth")
    await server.start()
    try:
        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "bot_id": "bot-1",
                    "secret": "secret-1",
                    "websocket_url": server.ws_url,
                },
            )
        )

        # Shorten backoff for test speed
        import gateway.platforms.wecom as wecom_module
        original_backoff = wecom_module.RECONNECT_BACKOFF
        wecom_module.RECONNECT_BACKOFF = [0.05, 0.1]

        success = await adapter.connect()
        assert success is True

        # Wait for server to close after auth and for reconnect to happen
        await asyncio.sleep(0.3)

        # There should be at least 2 subscribe calls (initial + reconnect)
        subs = [p for p in server._received if p.get("cmd") == "aibot_subscribe"]
        assert len(subs) >= 2

        wecom_module.RECONNECT_BACKOFF = original_backoff
        await adapter.disconnect()
    finally:
        await server.stop()
