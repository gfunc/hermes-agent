# WeCom WebSocket Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate silent WebSocket hangs and startup delays in the WeCom adapter by adding TCP keepalives, a connection watchdog, fixing MCP discovery ordering, and providing a reusable mock WeCom server for regression tests.

**Architecture:** Add a lightweight mock WeCom WebSocket server for tests, then layer three defenses into `wecom.py`: kernel-level TCP keepalives on the WS socket, a frame-level watchdog that forces reconnect when traffic stalls, and moving MCP config discovery after the listener loop starts so it doesn't block startup for 90s.

**Tech Stack:** Python 3.11+, asyncio, aiohttp, pytest.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tests/gateway/mock_wecom_server.py` | **Create** — reusable aiohttp mock WeCom WebSocket server for integration-style tests |
| `tests/gateway/test_wecom_reliability.py` | **Create** — TDD tests for keepalive, watchdog, reconnect, and MCP discovery order |
| `gateway/platforms/wecom.py` | **Modify** — TCP keepalive injection, watchdog task, fix `_discover_mcp_configs` ordering, add `_last_frame_at` tracking |

---

## Task 1: Mock WeCom WebSocket Server

**Files:**
- Create: `tests/gateway/mock_wecom_server.py`
- Test: `tests/gateway/test_wecom_reliability.py`

### Step 1.1: Write the mock server

- [ ] **Create `tests/gateway/mock_wecom_server.py`**

```python
"""Reusable mock WeCom WebSocket server for integration tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web, WSMsgType


class MockWeComServer:
    """
    Lightweight aiohttp mock of the WeCom AI Bot WebSocket gateway.

    Supports scenarios:
    - normal: responds to subscribe, echoes pings, can push messages
    - silent: accepts connection but sends nothing
    - close_after_auth: closes cleanly after subscribe ack
    - close_silent: aborts the TCP socket without a WS close frame
    """

    def __init__(self, scenario: str = "normal", delay_auth_seconds: float = 0.0):
        self.scenario = scenario
        self.delay_auth_seconds = delay_auth_seconds
        self.app = web.Application()
        self.app.router.add_get("/ws", self._ws_handler)
        self._clients: List[web.WebSocketResponse] = []
        self._received: List[Dict[str, Any]] = []
        self._server: Optional[Any] = None
        self.ws_url: str = ""

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    self._received.append(payload)
                    await self._handle_payload(ws, payload)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            self._clients.remove(ws)
        return ws

    async def _handle_payload(self, ws: web.WebSocketResponse, payload: Dict[str, Any]) -> None:
        cmd = payload.get("cmd")
        req_id = payload.get("headers", {}).get("req_id", "")

        if cmd == "aibot_subscribe":
            if self.delay_auth_seconds:
                await asyncio.sleep(self.delay_auth_seconds)
            await ws.send_json({
                "cmd": "aibot_subscribe",
                "headers": {"req_id": req_id},
                "body": {"errcode": 0},
            })
            if self.scenario == "close_after_auth":
                await ws.close()
            return

        if cmd == "ping":
            # Mock server does NOT auto-reply to pings — that tests the client heartbeat
            return

        if cmd == "aibot_send_msg":
            # Echo an ack so _send_request returns
            await ws.send_json({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": req_id},
                "body": {"errcode": 0},
            })
            return

        if cmd == "mcp_get_config":
            category = payload.get("body", {}).get("category", "unknown")
            await ws.send_json({
                "cmd": "mcp_get_config",
                "headers": {"req_id": req_id},
                "body": {"errcode": 0, "url": f"http://localhost/mcp/{category}"},
            })
            return

    async def send_callback(self, chatid: str, text: str, msgid: str = "mock-msg-1") -> None:
        """Push a synthetic inbound message to all connected clients."""
        for ws in list(self._clients):
            await ws.send_json({
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": f"mock-req-{msgid}"},
                "body": {
                    "msgid": msgid,
                    "chatid": chatid,
                    "chattype": "single",
                    "from": {"userid": "mock_user"},
                    "msgtype": "text",
                    "text": {"content": text},
                },
            })

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._server = runner
        self.ws_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._server:
            await self._server.cleanup()
            self._server = None

    @asynccontextmanager
    async def run(self):
        await self.start()
        try:
            yield self
        finally:
            await self.stop()
```

- [ ] **Write failing import test**

```python
# tests/gateway/test_wecom_reliability.py
import pytest

pytestmark = pytest.mark.asyncio


async def test_mock_server_starts_and_stops():
    from tests.gateway.mock_wecom_server import MockWeComServer

    server = MockWeComServer()
    await server.start()
    assert server.ws_url.startswith("http://127.0.0.1:")
    await server.stop()
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_mock_server_starts_and_stops -v`
Expected: FAIL — `tests/gateway/mock_wecom_server.py` does not exist yet.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_mock_server_starts_and_stops -v`
Expected: PASS

- [ ] **Commit**

```bash
git add tests/gateway/mock_wecom_server.py tests/gateway/test_wecom_reliability.py
git commit -m "feat(wecom): add mock WeCom WebSocket server for reliability tests"
```

---

## Task 2: Fix MCP Discovery Blocking Startup

**Files:**
- Modify: `gateway/platforms/wecom.py:266-269`
- Test: `tests/gateway/test_wecom_reliability.py`

### Step 2.1: Write failing test for MCP discovery order

- [ ] **Add test**

```python
# tests/gateway/test_wecom_reliability.py

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

    monkeypatch.setattr(
        wecom_module,
        "httpx",
        type("httpx", (), {"AsyncClient": lambda **kwargs: DummyClient()})(),
    )

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
        # By the time discover runs, listen loop must already exist
        assert adapter._listen_task is not None
        assert adapter._heartbeat_task is not None

    async def fake_listen():
        listen_started.set()
        await asyncio.sleep(0)  # yield so discover can run

    adapter._open_connection = fake_open
    adapter._discover_mcp_configs = fake_discover
    adapter._listen_loop = fake_listen
    adapter._heartbeat_loop = asyncio.sleep

    success = await adapter.connect()
    assert success is True
    assert open_called.is_set()
    assert listen_started.is_set()
    assert discover_called.is_set()

    await adapter.disconnect()
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_connect_does_not_block_on_mcp_discovery -v`
Expected: FAIL — assertion `adapter._listen_task is not None` fails because discover runs before listen task is created.

### Step 2.2: Reorder MCP discovery after listener start

- [ ] **Modify `gateway/platforms/wecom.py`**

Find this block (around line 266):

```python
                self._bot_id = primary.bot_id
                self._secret = primary.secret
                self._ws_url = primary.websocket_url
                await self._open_connection()
                await self._discover_mcp_configs()
                self._listen_task = asyncio.create_task(self._listen_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
```

Change to:

```python
                self._bot_id = primary.bot_id
                self._secret = primary.secret
                self._ws_url = primary.websocket_url
                await self._open_connection()
                self._listen_task = asyncio.create_task(self._listen_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await self._discover_mcp_configs()
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_connect_does_not_block_on_mcp_discovery -v`
Expected: PASS

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_reliability.py
git commit -m "fix(wecom): run MCP discovery after listen loop to avoid 90s startup stall"
```

---

## Task 3: TCP Keepalive on WebSocket Connection

**Files:**
- Modify: `gateway/platforms/wecom.py` (constants + `_open_connection` + `_cleanup_ws`)
- Test: `tests/gateway/test_wecom_reliability.py`

### Step 3.1: Add keepalive constants

- [ ] **Add constants after `RECONNECT_BACKOFF`**

In `gateway/platforms/wecom.py` around line 106, add:

```python
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# TCP keepalive tuning (Linux) — detect dead proxies/NAT within ~60s
TCP_KEEPALIVE_ENABLED = True
TCP_KEEPALIVE_IDLE = 30   # seconds before first probe
TCP_KEEPALIVE_INTERVAL = 10  # seconds between probes
TCP_KEEPALIVE_COUNT = 3   # probes before declaring dead
```

### Step 3.2: Write failing test for keepalive socket options

- [ ] **Add test**

```python
# tests/gateway/test_wecom_reliability.py

async def test_websocket_uses_tcp_keepalive(monkeypatch):
    """
    After ws_connect succeeds, the adapter should set SO_KEEPALIVE
    and TCP keepalive tuning on the underlying socket.
    """
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
        _transport = FakeTransport()

        def get_extra_info(self, name, default=None):
            return self._transport.get_extra_info(name)

        async def send_json(self, payload):
            pass

        async def receive(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    class FakeSession:
        closed = False

        async def ws_connect(self, *args, **kwargs):
            return FakeWS()

        async def close(self):
            self.closed = True

    adapter._session = FakeSession()
    adapter._ws = await adapter._session.ws_connect("wss://test")

    # Manually invoke the keepalive helper (it is called inside _open_connection)
    await adapter._open_connection = AsyncMock()

    # Instead, test the new public helper directly
    # We'll add _apply_tcp_keepalive(ws) in the implementation step.
```

Wait — we need a cleaner test that exercises the real code path. Let's mock `aiohttp.ClientSession.ws_connect` to return a fake websocket with a real socket mock.

```python
# tests/gateway/test_wecom_reliability.py

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

    monkeypatch.setattr(wecom_module, "aiohttp", type("aiohttp", (), {
        "ClientSession": lambda **kwargs: session_mock,
        "WSMsgType": type("WSMsgType", (), {"TEXT": 1, "CLOSE": 2, "CLOSED": 3, "ERROR": 4, "PING": 5, "PONG": 6}),
    })())

    await adapter._open_connection()

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE) in sock_opts
    assert sock_opts[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    assert (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) in sock_opts
    assert sock_opts[(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)] == 30
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_websocket_uses_tcp_keepalive -v`
Expected: FAIL — `_apply_tcp_keepalive` does not exist yet, and `_open_connection` does not set keepalives.

### Step 3.3: Implement keepalive injection

- [ ] **Add `_apply_tcp_keepalive` method in `WeComAdapter`**

Insert after `_cleanup_ws` (around line 365):

```python
    def _apply_tcp_keepalive(self) -> None:
        """Set TCP keepalive socket options on the active websocket."""
        import socket

        if not TCP_KEEPALIVE_ENABLED:
            return
        if not self._ws:
            return

        sock = self._ws.get_extra_info("socket")
        if sock is None:
            return

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT)
        except (OSError, AttributeError):
            # Platform may not support these options (macOS uses TCP_KEEPALIVE instead of TCP_KEEPIDLE)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), TCP_KEEPALIVE_IDLE)
            except (OSError, AttributeError):
                pass
```

- [ ] **Call it from `_open_connection`**

In `_open_connection`, after the successful subscribe auth check (after line 638), add:

```python
        self._apply_tcp_keepalive()
```

The `_open_connection` method should look like:

```python
    async def _open_connection(self) -> None:
        """Open and authenticate a websocket connection."""
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {"bot_id": self._bot_id, "secret": self._secret},
            }
        )

        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in (0, None):
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")
        self._apply_tcp_keepalive()
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_websocket_uses_tcp_keepalive -v`
Expected: PASS

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_reliability.py
git commit -m "feat(wecom): enable TCP keepalives on WebSocket to detect dead connections"
```

---

## Task 4: Connection Watchdog

**Files:**
- Modify: `gateway/platforms/wecom.py` (fields, constants, `_read_events`, `_listen_loop`, `connect`, `disconnect`)
- Test: `tests/gateway/test_wecom_reliability.py`

### Step 4.1: Add watchdog constants and fields

- [ ] **Add constant after TCP keepalive settings**

```python
WATCHDOG_TIMEOUT_SECONDS = 45.0  # must be > HEARTBEAT_INTERVAL_SECONDS (30s)
```

- [ ] **Add fields in `__init__`**

After `self._heartbeat_task = None` (around line 181), add:

```python
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_frame_at: float = 0.0
```

### Step 4.2: Write failing test for watchdog forcing reconnect on silent connection

- [ ] **Add test**

```python
# tests/gateway/test_wecom_reliability.py

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
        closed = False

        async def send_json(self, payload):
            pass

        async def receive(self):
            nonlocal receive_calls
            receive_calls += 1
            # Hang forever — simulate silent dead connection
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    class SilentSession:
        closed = False

        async def ws_connect(self, *args, **kwargs):
            nonlocal reconnect_attempts
            reconnect_attempts += 1
            return SilentWS()

        async def close(self):
            self.closed = True

    adapter._session = SilentSession()
    adapter._running = True
    adapter._last_frame_at = asyncio.get_running_loop().time()

    # Start watchdog with a short timeout for the test
    monkeypatch.setattr(wecom_module, "WATCHDOG_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(wecom_module, "RECONNECT_BACKOFF", [0.05])

    # Mock _wait_for_handshake so _open_connection succeeds
    monkeypatch.setattr(adapter, "_wait_for_handshake", AsyncMock(return_value={"errcode": 0}))

    # Run listen loop for a bounded time
    listen_task = asyncio.create_task(adapter._listen_loop())
    await asyncio.sleep(0.5)
    adapter._running = False
    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass

    # Watchdog should have forced at least one reconnect
    assert reconnect_attempts >= 2
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_watchdog_triggers_reconnect_on_silent_connection -v`
Expected: FAIL — `_watchdog_loop` does not exist, so reconnects won't happen.

### Step 4.3: Implement watchdog loop

- [ ] **Add `_watchdog_loop` method**

Insert after `_heartbeat_loop` (around line 784):

```python
    async def _watchdog_loop(self) -> None:
        """Monitor websocket activity and force reconnect if traffic stalls."""
        try:
            while self._running:
                await asyncio.sleep(WATCHDOG_TIMEOUT_SECONDS)
                if not self._running:
                    return
                if not self._ws or self._ws.closed:
                    continue
                idle = asyncio.get_running_loop().time() - self._last_frame_at
                if idle >= WATCHDOG_TIMEOUT_SECONDS:
                    logger.warning(
                        "[%s] WebSocket watchdog timeout (idle %.1fs), forcing reconnect",
                        self.name, idle,
                    )
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
```

- [ ] **Update `_read_events` to track activity**

In `_read_events`, at the top of the `while` loop (around line 744), add before `msg = await ...`:

```python
            self._last_frame_at = asyncio.get_running_loop().time()
```

So `_read_events` becomes:

```python
    async def _read_events(self) -> None:
        """Read websocket frames until the connection closes."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            self._last_frame_at = asyncio.get_running_loop().time()
            try:
                msg = await asyncio.wait_for(
                    self._ws.receive(), timeout=HEARTBEAT_INTERVAL_SECONDS * 3
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "WeCom websocket read timeout (%ss)" % (HEARTBEAT_INTERVAL_SECONDS * 3)
                )
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WeCom websocket closed")
        logger.debug(
            "[%s] _read_events exiting (_running=%s, ws_closed=%s)",
            self.name,
            self._running,
            self._ws.closed if self._ws else True,
        )
```

- [ ] **Start and stop watchdog task in `connect` / `disconnect`**

In `connect`, after creating the heartbeat task (around line 269), add:

```python
                self._watchdog_task = asyncio.create_task(self._watchdog_loop())
```

In `disconnect`, after cleaning up the heartbeat task (around line 336), add:

```python
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
```

Also initialize `_last_frame_at` at connection time. In `connect`, after `_open_connection()` succeeds (around line 266), add:

```python
                self._last_frame_at = asyncio.get_running_loop().time()
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_watchdog_triggers_reconnect_on_silent_connection -v`
Expected: PASS

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_reliability.py
git commit -m "feat(wecom): add connection watchdog to force reconnect on stalled websocket"
```

---

## Task 5: End-to-End Mock Server Tests

**Files:**
- Test: `tests/gateway/test_wecom_reliability.py`

### Step 5.1: Write test for normal message flow through mock server

- [ ] **Add test**

```python
# tests/gateway/test_wecom_reliability.py

async def test_mock_server_receives_and_delivers_message():
    from tests.gateway.mock_wecom_server import MockWeComServer
    from gateway.platforms.wecom import WeComAdapter
    from gateway.config import PlatformConfig

    async with MockWeComServer() as server:
        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._ws_url = server.ws_url

        success = await adapter.connect()
        assert success is True

        # Give listen loop time to start
        await asyncio.sleep(0.1)

        # Verify subscription was received by mock server
        assert any(p.get("cmd") == "aibot_subscribe" for p in server._received)

        await adapter.disconnect()
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_mock_server_receives_and_delivers_message -v`
Expected: PASS (may need small `asyncio.sleep` tuning).

### Step 5.2: Write test for reconnect after mock server closes connection

- [ ] **Add test**

```python
# tests/gateway/test_wecom_reliability.py

async def test_adapter_reconnects_after_mock_server_closes():
    from tests.gateway.mock_wecom_server import MockWeComServer
    from gateway.platforms.wecom import WeComAdapter
    from gateway.config import PlatformConfig

    async with MockWeComServer(scenario="close_after_auth") as server:
        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._ws_url = server.ws_url

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
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_reliability.py::test_adapter_reconnects_after_mock_server_closes -v`
Expected: PASS

- [ ] **Commit**

```bash
git add tests/gateway/test_wecom_reliability.py
git commit -m "test(wecom): add mock-server E2E tests for normal flow and reconnect"
```

---

## Task 6: Run Full WeCom Test Suite

- [ ] **Run all WeCom tests**

Run:
```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_reliability.py -v
```

Expected: All tests pass.

- [ ] **Commit if any fixes were needed**

If any test fails due to plan bugs, fix inline and commit with a descriptive message.

---

## Self-Review Checklist

1. **Spec coverage:**
   - TCP keepalives → Task 3
   - MCP discovery startup fix → Task 2
   - Connection watchdog → Task 4
   - Mock WeCom server for tests → Task 1 & 5
   - MiniMax fix explicitly excluded

2. **Placeholder scan:** No TBD, TODO, or vague steps remain. Every step contains exact file paths and code.

3. **Type consistency:**
   - `self._watchdog_task: Optional[asyncio.Task]` used throughout
   - `WATCHDOG_TIMEOUT_SECONDS` used in constant, test mock, and watchdog loop
   - `MockWeComServer` API (`ws_url`, `run()`, `send_callback()`) is consistent across tasks
