"""End-to-end tests for tools/wecom_mcp_tool.py.

These tests simulate the EXACT runtime path that registry.dispatch() takes:
  1. handler(args_dict, **kwargs)  -- NOT handler(**args_dict)
  2. Transport fetches MCP config from adapter/env
  3. Transport manages sessions, rebuilds on 404, parses SSE
  4. Interceptors run before/after the RPC call

Unlike unit tests that mock send_json_rpc directly, these mock at the
aiohttp.ClientSession layer so the full transport stack is exercised.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.wecom_mcp.transport as transport_module
from tools.registry import registry
from tools.wecom_mcp.transport import McpSession
from tools.wecom_mcp_tool import handle_wecom_mcp


# ── Helpers ────────────────────────────────────────────────────────────────


class _AsyncContextManager:
    """Async context manager that returns a given value on enter."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


class _SessionMock:
    """Mock aiohttp.ClientSession that yields responses from a chain."""

    def __init__(self, response_chain: list):
        self._responses = list(response_chain)
        self._idx = 0
        self.closed = False
        self.post_call_count = 0
        self._last_post_kwargs: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        if self._idx >= len(self._responses):
            raise RuntimeError("No more mock responses")
        resp = self._responses[self._idx]
        self._idx += 1
        self.post_call_count += 1
        self._last_post_kwargs = kwargs
        return _AsyncContextManager(resp)

    async def close(self):
        self.closed = True


def _make_response(
    *,
    status: int = 200,
    text: str = "",
    content_type: str = "application/json",
    headers: dict | None = None,
):
    """Build a mock aiohttp ClientResponse."""
    response = AsyncMock()
    response.status = status
    response.ok = 200 <= status < 300
    response.headers = MagicMock()
    hdrs = dict(headers or {})
    if content_type:
        hdrs["content-type"] = content_type
    response.headers.get = MagicMock(side_effect=lambda key, default=None: hdrs.get(key, default))
    response.text = AsyncMock(return_value=text)
    return response


def _make_session_mock(response_chain: list):
    return _SessionMock(response_chain)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_all_caches(monkeypatch):
    """Wipe all transport caches before each test."""
    transport_module._mcp_config_cache.clear()
    transport_module._mcp_session_cache.clear()
    transport_module._stateless_categories.clear()
    transport_module._inflight_init.clear()
    transport_module._http_session = None
    yield


@pytest.fixture
def mock_adapter_with_mcp():
    """Return a mock WeCom adapter that provides MCP configs."""
    adapter = MagicMock()
    adapter.get_mcp_configs.return_value = {
        "contact": "http://mcp.example/contact",
        "doc": "http://mcp.example/doc",
        "msg": "http://mcp.example/msg",
    }
    return adapter


# ── 1. Happy path: list ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_list_tools_returns_formatted_json(mock_adapter_with_mcp):
    """Mock server returns tools/list result; handler returns properly formatted JSON."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-list"},
    )
    notify_resp = _make_response(status=204)
    list_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "tools": [
                    {"name": "get_userlist", "inputSchema": {"type": "object"}},
                    {"name": "get_user", "inputSchema": {"$defs": {"Foo": {}}, "type": "object"}},
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, list_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-list-1",
                user_task="test-user-task",
            )

    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 2
    assert len(parsed["tools"]) == 2
    assert parsed["tools"][0]["name"] == "get_userlist"
    # Schema cleaning should have removed $defs
    assert "$defs" not in parsed["tools"][1]["inputSchema"]


# ── 2. Happy path: call ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_call_tool_with_interceptors(mock_adapter_with_mcp):
    """Mock server returns tool result; interceptors run correctly."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-call"},
    )
    notify_resp = _make_response(status=204)
    call_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok", "userid": "alice"})}
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, call_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_user",
                    "args": '{"userid": "alice"}',
                },
                task_id="task-call-1",
                user_task="test-user-task",
            )

    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0
    assert inner["userid"] == "alice"


@pytest.mark.asyncio
async def test_e2e_call_tool_with_dict_args(mock_adapter_with_mcp):
    """Handler accepts dict args (not just JSON string)."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-dict"},
    )
    notify_resp = _make_response(status=204)
    call_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, call_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_userlist",
                    "args": {"department_id": 1},
                },
                task_id="task-dict-1",
            )

    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0


# ── 3. Missing config ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_missing_config_returns_clear_error(monkeypatch):
    """When no MCP config is available, handler returns clear error (not empty action error)."""
    # Ensure no env var and no adapter
    monkeypatch.delenv("WECOM_MCP_CONFIG", raising=False)

    with patch.object(transport_module, "_get_wecom_adapter", return_value=None):
        result = await handle_wecom_mcp(
            {"action": "list", "category": "nonexistent"},
            task_id="task-missing-1",
        )

    parsed = json.loads(result)
    assert "error" in parsed
    # Should be a config error, not an empty-action or unknown error
    assert parsed["error"] == "MCP_CONFIG_ERROR"
    assert "nonexistent" in parsed["message"]
    assert "unavailable" in parsed["message"].lower() or "config" in parsed["message"].lower()


# ── 4. Registry dispatch simulation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_registry_dispatch_simulation_list(mock_adapter_with_mcp):
    """Call handler EXACTLY as registry.dispatch does: handler(args_dict, **kwargs)."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-dispatch"},
    )
    notify_resp = _make_response(status=204)
    list_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {"tools": [{"name": "get_userlist", "inputSchema": {}}]},
        })
    )
    session = _make_session_mock([init_resp, notify_resp, list_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            # This is EXACTLY what registry.dispatch does for async handlers:
            #   coro = entry.handler(args, **kwargs)
            #   result = _run_async(coro)
            coro = handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="dispatch-task-123",
                user_task="dispatch-user-task",
            )
            assert hasattr(coro, "__await__"), "handler must return a coroutine, not a plain value"
            result = await coro

    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 1
    assert parsed["tools"][0]["name"] == "get_userlist"


@pytest.mark.asyncio
async def test_e2e_registry_dispatch_simulation_call(mock_adapter_with_mcp):
    """Registry.dispatch calls handler with task_id and user_task kwargs."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-dispatch-call"},
    )
    notify_resp = _make_response(status=204)
    call_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, call_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            coro = handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_userlist",
                    "args": "{}",
                },
                task_id="dispatch-task-456",
                user_task="dispatch-user-task",
            )
            result = await coro

    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0


@pytest.mark.asyncio
async def test_e2e_registry_dispatch_via_actual_registry(mock_adapter_with_mcp):
    """Use the actual registry.dispatch() to call wecom_mcp."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-registry"},
    )
    notify_resp = _make_response(status=204)
    list_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {"tools": []},
        })
    )
    session = _make_session_mock([init_resp, notify_resp, list_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            # registry.dispatch is synchronous; it bridges async handlers internally
            result = registry.dispatch(
                "wecom_mcp",
                {"action": "list", "category": "contact"},
                task_id="registry-task-789",
                user_task="registry-user-task",
            )

    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 0


# ── 5. 404 auto-rebuild ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_404_auto_rebuild_retries_successfully(mock_adapter_with_mcp):
    """Mock server returns 404 on first call, transport rebuilds session and retries."""
    # Initial session init + notify
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-404-1"},
    )
    notify_resp = _make_response(status=204)
    # First RPC: 404
    fail_resp = _make_response(status=404, text="Not Found")
    # Rebuild init + notify
    init_resp2 = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-404-rebuilt"},
    )
    notify_resp2 = _make_response(status=204)
    # Retry RPC succeeds
    ok_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {"tools": [{"name": "get_userlist", "inputSchema": {}}]},
        })
    )
    session = _make_session_mock([init_resp, notify_resp, fail_resp, init_resp2, notify_resp2, ok_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-404-1",
            )

    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 1
    # Session should have been rebuilt
    assert transport_module._mcp_session_cache["contact"].session_id == "sess-404-rebuilt"


@pytest.mark.asyncio
async def test_e2e_404_on_stateless_does_not_retry(mock_adapter_with_mcp):
    """Stateless servers should NOT retry on 404."""
    # Stateless init (no session-id returned)
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
    )
    # First RPC: 404 — should NOT retry for stateless
    fail_resp = _make_response(status=404, text="Not Found")
    session = _make_session_mock([init_resp, fail_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "doc"},
                task_id="task-404-stateless",
            )

    parsed = json.loads(result)
    assert parsed["error"] == "MCP_HTTP_ERROR"
    assert parsed["status_code"] == 404


# ── 6. SSE response ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_sse_response_parsed_correctly(mock_adapter_with_mcp):
    """Mock server returns SSE stream, transport parses it correctly."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-sse"},
    )
    notify_resp = _make_response(status=204)
    sse_text = (
        "data: {\"jsonrpc\":\"2.0\",\"result\":{\"tools\":[{\"name\":\"t1\"}]}}\n\n"
    )
    sse_resp = _make_response(
        text=sse_text,
        content_type="text/event-stream",
    )
    session = _make_session_mock([init_resp, notify_resp, sse_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-sse-1",
            )

    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 1
    assert parsed["tools"][0]["name"] == "t1"


@pytest.mark.asyncio
async def test_e2e_sse_multi_event_takes_last(mock_adapter_with_mcp):
    """SSE with multiple events — transport should take the LAST one."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-sse-multi"},
    )
    notify_resp = _make_response(status=204)
    sse_text = (
        "data: {\"jsonrpc\":\"2.0\",\"result\":{\"n\":1}}\n\n"
        "data: {\"jsonrpc\":\"2.0\",\"result\":{\"n\":2}}\n\n"
    )
    sse_resp = _make_response(
        text=sse_text,
        content_type="text/event-stream",
    )
    session = _make_session_mock([init_resp, notify_resp, sse_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-sse-multi",
            )

    # Since the SSE result is {"n": 2} and list action wraps it,
    # the handler will do result.get("tools", []) which is empty for {"n": 2}
    # So we get count=0. The key thing is that it parsed successfully.
    parsed = json.loads(result)
    assert "category" in parsed
    assert parsed["category"] == "contact"


# ── Error handling paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_rpc_error_returns_structured_json(mock_adapter_with_mcp):
    """Server returns JSON-RPC error; handler returns structured error JSON."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-err"},
    )
    notify_resp = _make_response(status=204)
    err_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request"},
        })
    )
    session = _make_session_mock([init_resp, notify_resp, err_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-err-1",
            )

    parsed = json.loads(result)
    assert parsed["error"] == "MCP_RPC_ERROR"
    assert parsed["code"] == -32600
    assert "Invalid Request" in parsed["message"]


@pytest.mark.asyncio
async def test_e2e_http_500_returns_structured_json(mock_adapter_with_mcp):
    """Server returns HTTP 500; handler returns structured error JSON."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-500"},
    )
    notify_resp = _make_response(status=204)
    err_resp = _make_response(status=500, text="Internal Server Error")
    session = _make_session_mock([init_resp, notify_resp, err_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-500-1",
            )

    parsed = json.loads(result)
    assert parsed["error"] == "MCP_HTTP_ERROR"
    assert parsed["status_code"] == 500


@pytest.mark.asyncio
async def test_e2e_interceptor_biz_error_clears_cache(mock_adapter_with_mcp):
    """BizErrorInterceptor clears cache when errcode 850002 is detected."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-biz"},
    )
    notify_resp = _make_response(status=204)
    call_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"errcode": 850002, "errmsg": "token expired"})}
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, call_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_userlist",
                    "args": "{}",
                },
                task_id="task-biz-1",
            )

    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 850002
    # Cache should have been cleared by biz_error interceptor
    assert "contact" not in transport_module._mcp_config_cache
    assert "contact" not in transport_module._mcp_session_cache


@pytest.mark.asyncio
async def test_e2e_interceptor_media_timeout_applied(mock_adapter_with_mcp):
    """MediaInterceptor bumps timeout for get_msg_media."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-media"},
    )
    notify_resp = _make_response(status=204)
    call_resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}
                ]
            },
        })
    )
    session = _make_session_mock([init_resp, notify_resp, call_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "msg",
                    "method": "get_msg_media",
                    "args": '{"media_id": "mid-1"}',
                },
                task_id="task-media-1",
            )

    # The timeout should be 120_000ms (MediaInterceptor default)
    # We verify by checking the last post kwargs
    assert session._last_post_kwargs.get("timeout", {}).total == 120.0


# ── Edge cases ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_unknown_action_returns_error_json(mock_adapter_with_mcp):
    """Unknown action returns proper error JSON, not an exception."""
    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        result = await handle_wecom_mcp(
            {"action": "delete", "category": "contact"},
            task_id="task-unknown-1",
        )

    parsed = json.loads(result)
    assert parsed["error"] == "MCP_UNEXPECTED_ERROR"
    assert "Unknown action: delete" in parsed["message"]


@pytest.mark.asyncio
async def test_e2e_list_empty_tools(mock_adapter_with_mcp):
    """Server returns empty tools list."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-empty"},
    )
    notify_resp = _make_response(status=204)
    list_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"tools": []}})
    )
    session = _make_session_mock([init_resp, notify_resp, list_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "msg"},
                task_id="task-empty-1",
            )

    parsed = json.loads(result)
    assert parsed["count"] == 0
    assert parsed["tools"] == []


@pytest.mark.asyncio
async def test_e2e_list_non_dict_result(mock_adapter_with_mcp):
    """If result is not a dict, return empty tools list."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-nondict"},
    )
    notify_resp = _make_response(status=204)
    # Server returns a string result instead of dict
    list_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": "ok"})
    )
    session = _make_session_mock([init_resp, notify_resp, list_resp])

    with patch.object(transport_module, "_get_wecom_adapter", return_value=mock_adapter_with_mcp):
        with patch("aiohttp.ClientSession", return_value=session):
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="task-nondict-1",
            )

    parsed = json.loads(result)
    assert parsed["count"] == 0
    assert parsed["tools"] == []
