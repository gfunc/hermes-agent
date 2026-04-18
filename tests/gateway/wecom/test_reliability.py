"""Tests for WeCom WebSocket reliability improvements."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

pytestmark = pytest.mark.asyncio


async def test_mock_server_starts_and_stops():
    from tests.gateway.wecom.mock_server import MockWeComServer

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
    import gateway.platforms.wecom.adapter as wecom_module
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
    import gateway.platforms.wecom.adapter as wecom_module
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
    import gateway.platforms.wecom.adapter as wecom_module
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
    import gateway.platforms.wecom.adapter as wecom_module
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
    import gateway.platforms.wecom.adapter as wecom_module
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
    from tests.gateway.wecom.mock_server import MockWeComServer
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
    from tests.gateway.wecom.mock_server import MockWeComServer
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
        import gateway.platforms.wecom.adapter as wecom_module
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


async def test_mock_server_multi_turn_typing_flow():
    """Full E2E: typing opens, interim response, typing reopens, final response.

    Verifies the exact frame sequence seen by the WeCom server when an
    agent sends multiple responses for a single user message.
    """
    from tests.gateway.wecom.mock_server import MockWeComServer
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

        # Prevent full _on_message from running (no gateway plumbing)
        adapter._on_message = AsyncMock()

        success = await adapter.connect()
        assert success is True

        # Wait for connection and subscription handshake
        await asyncio.sleep(0.1)

        # Seed a reply req_id mapping (as if a callback message arrived)
        adapter._remember_reply_req_id("msg-e2e", "req-e2e")

        # Round 1: open typing indicator
        await adapter.send_typing("chat-e2e", metadata={"message_id": "msg-e2e"})
        await asyncio.sleep(0.2)

        respond_frames = [p for p in server._received if p.get("cmd") == "aibot_respond_msg"]
        assert len(respond_frames) == 1
        frame1 = respond_frames[0]
        assert frame1["body"]["msgtype"] == "stream"
        assert frame1["body"]["stream"]["finish"] is False
        stream_id_1 = frame1["body"]["stream"]["id"]

        # Round 1: send intermediate response (reuses typing stream_id)
        result1 = await adapter.send(
            "chat-e2e", "Interim 1", reply_to="msg-e2e"
        )
        assert result1.success is True
        await asyncio.sleep(0.2)

        respond_frames = [p for p in server._received if p.get("cmd") == "aibot_respond_msg"]
        assert len(respond_frames) == 2
        frame2 = respond_frames[1]
        assert frame2["body"]["stream"]["finish"] is True
        assert frame2["body"]["stream"]["id"] == stream_id_1

        # Round 2: typing reopens for ongoing work
        await adapter.send_typing("chat-e2e", metadata={"message_id": "msg-e2e"})
        await asyncio.sleep(0.2)

        respond_frames = [p for p in server._received if p.get("cmd") == "aibot_respond_msg"]
        assert len(respond_frames) == 3
        frame3 = respond_frames[2]
        assert frame3["body"]["stream"]["finish"] is False
        stream_id_2 = frame3["body"]["stream"]["id"]
        assert stream_id_2 != stream_id_1

        # Round 2: send final response
        result2 = await adapter.send(
            "chat-e2e", "Final", reply_to="msg-e2e"
        )
        assert result2.success is True
        await asyncio.sleep(0.2)

        respond_frames = [p for p in server._received if p.get("cmd") == "aibot_respond_msg"]
        assert len(respond_frames) == 4
        frame4 = respond_frames[3]
        assert frame4["body"]["stream"]["finish"] is True
        assert frame4["body"]["stream"]["id"] == stream_id_2

        await adapter.disconnect()
    finally:
        await server.stop()


# ------------------------------------------------------------------
# Reply queue tests
# ------------------------------------------------------------------

async def test_reply_queue_serializes_sends_for_same_req_id():
    from gateway.platforms.wecom.reply_queue import WeComReplyQueue

    pending: dict = {}
    sent_order: list = []

    async def slow_send_json(payload):
        sent_order.append(payload.get("id"))
        await asyncio.sleep(0.05)
        future = pending.get("req-1")
        if future and not future.done():
            future.set_result({"errcode": 0})

    queue = WeComReplyQueue(slow_send_json, pending, max_size=10)
    future1 = await queue.enqueue("req-1", {"id": "a"}, timeout=0.5)
    future2 = await queue.enqueue("req-1", {"id": "b"}, timeout=0.5)

    await asyncio.gather(future1, future2)
    assert sent_order == ["a", "b"]


async def test_reply_queue_fails_on_disconnect():
    from gateway.platforms.wecom.reply_queue import WeComReplyQueue

    pending: dict = {}

    async def send_json(payload):
        pass  # never acks

    queue = WeComReplyQueue(send_json, pending, max_size=10)
    future = await queue.enqueue("req-1", {}, timeout=5.0)
    exc = RuntimeError("disconnected")
    queue.fail_all(exc)
    with pytest.raises(RuntimeError, match="disconnected"):
        await future


async def test_reply_queue_timeout_races_with_late_ack():
    from gateway.platforms.wecom.reply_queue import WeComReplyQueue

    pending: dict = {}

    async def send_json(payload):
        pass  # don't ack

    queue = WeComReplyQueue(send_json, pending, max_size=10)
    future = await queue.enqueue("req-1", {}, timeout=0.1)
    with pytest.raises(asyncio.TimeoutError):
        await future
    # Late ack should be a harmless no-op (future already done)
    assert future.done()


async def test_reply_queue_respects_max_size():
    from gateway.platforms.wecom.reply_queue import WeComReplyQueue

    pending: dict = {}
    blocker = asyncio.Event()

    async def send_json(payload):
        await blocker.wait()

    queue = WeComReplyQueue(send_json, pending, max_size=1)
    future1 = await queue.enqueue("req-1", {}, timeout=5.0)
    future2 = await queue.enqueue("req-1", {}, timeout=5.0)
    future3 = await queue.enqueue("req-1", {}, timeout=5.0)

    assert isinstance(future3.exception(), RuntimeError)
    assert "full" in str(future3.exception()).lower()

    # Clean up without hanging: fail queued/in-flight futures and release blocker.
    queue.fail_all(RuntimeError("cleanup"))
    for fut in list(pending.values()):
        if not fut.done():
            fut.set_exception(RuntimeError("cleanup"))
    blocker.set()
    # Retrieve exceptions so asyncio doesn't warn about unhandled futures.
    _ = future1.exception()
    _ = future2.exception()


# ------------------------------------------------------------------
# Missed-pong heartbeat tests
# ------------------------------------------------------------------

async def test_missed_pong_closes_connection_after_three_misses(monkeypatch):
    import gateway.platforms.wecom.adapter as wecom_module
    from gateway.platforms.wecom import WeComAdapter

    monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(wecom_module, "MAX_MISSED_PONGS", 2)

    class DummyClient:
        async def aclose(self):
            pass

    monkeypatch.setattr(
        wecom_module,
        "httpx",
        type("httpx", (), {"AsyncClient": lambda **kwargs: DummyClient()})(),
    )

    adapter = WeComAdapter(PlatformConfig(enabled=True, extra={"bot_id": "b", "secret": "s"}))

    close_calls: list = []
    transport_close_calls: list = []

    class FakeTransport:
        def close(self):
            transport_close_calls.append(True)

    class FakeWS:
        closed = False

        def get_extra_info(self, name, default=None):
            if name == "transport":
                return FakeTransport()
            return default

        async def send_json(self, payload):
            pass

        async def close(self):
            close_calls.append(True)
            self.closed = True

    adapter._ws = FakeWS()
    adapter._running = True

    task = asyncio.create_task(adapter._heartbeat_loop())
    await asyncio.sleep(0.25)
    adapter._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(close_calls) >= 1
    assert len(transport_close_calls) >= 1


async def test_missed_pong_counter_resets_on_inbound_frame(monkeypatch):
    import gateway.platforms.wecom.adapter as wecom_module
    from gateway.platforms.wecom import WeComAdapter

    monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(wecom_module, "MAX_MISSED_PONGS", 2)

    class DummyClient:
        async def aclose(self):
            pass

    monkeypatch.setattr(
        wecom_module,
        "httpx",
        type("httpx", (), {"AsyncClient": lambda **kwargs: DummyClient()})(),
    )

    adapter = WeComAdapter(PlatformConfig(enabled=True, extra={"bot_id": "b", "secret": "s"}))

    close_calls: list = []

    class FakeTransport:
        def close(self):
            close_calls.append(True)

    class FakeWS:
        closed = False

        def get_extra_info(self, name, default=None):
            if name == "transport":
                return FakeTransport()
            return default

        async def send_json(self, payload):
            pass

        async def close(self):
            close_calls.append(True)
            self.closed = True

    adapter._ws = FakeWS()
    adapter._running = True

    task = asyncio.create_task(adapter._heartbeat_loop())
    await asyncio.sleep(0.12)
    await adapter._ws_client.on_any_frame()
    await asyncio.sleep(0.15)
    adapter._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(close_calls) == 0


# ------------------------------------------------------------------
# Disconnected event (kicked) tests
# ------------------------------------------------------------------

async def test_disconnected_event_prevents_reconnect():
    from tests.gateway.wecom.mock_server import MockWeComServer
    from gateway.platforms.wecom import WeComAdapter
    import gateway.platforms.wecom.adapter as wecom_module

    server = MockWeComServer(scenario="normal")
    await server.start()
    try:
        original_backoff = wecom_module.RECONNECT_BACKOFF
        wecom_module.RECONNECT_BACKOFF = [0.05, 0.1]

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "bot_id": "b",
                    "secret": "s",
                    "websocket_url": server.ws_url,
                },
            )
        )
        success = await adapter.connect()
        assert success is True
        await asyncio.sleep(0.1)

        await server.send_event("disconnected_event")
        await asyncio.sleep(0.3)

        assert adapter._kicked is True
        assert adapter._running is False

        subs = [p for p in server._received if p.get("cmd") == "aibot_subscribe"]
        assert len(subs) == 1  # no reconnect subscribe

        wecom_module.RECONNECT_BACKOFF = original_backoff
        await adapter.disconnect()
    finally:
        await server.stop()


async def test_disconnected_event_allows_reconnect_after_manual_disconnect(monkeypatch):
    import gateway.platforms.wecom.adapter as wecom_module
    from gateway.platforms.wecom import WeComAdapter

    monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)

    class DummyClient:
        async def aclose(self):
            pass

    monkeypatch.setattr(
        wecom_module,
        "httpx",
        type("httpx", (), {"AsyncClient": lambda **kwargs: DummyClient()})(),
    )

    adapter = WeComAdapter(PlatformConfig(enabled=True, extra={"bot_id": "b", "secret": "s"}))
    adapter._kicked = True

    await adapter.disconnect()
    assert adapter._kicked is False
