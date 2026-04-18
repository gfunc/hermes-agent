"""Unit tests for tools/wecom_mcp/transport.py.

Uses AsyncMock to simulate aiohttp.ClientSession so tests run fast
without real network calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.wecom_mcp.transport as transport_module
from tools.wecom_mcp.transport import (
    CACHE_CLEAR_ERROR_CODES,
    McpHttpError,
    McpRpcError,
    McpSession,
    _generate_req_id,
    _parse_sse_response,
    _send_raw_json_rpc,
    clear_category_cache,
    fetch_mcp_config,
    get_or_create_session,
    initialize_session,
    rebuild_session,
    send_json_rpc,
)


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """Wipe all module-level caches before each test."""
    transport_module._mcp_config_cache.clear()
    transport_module._mcp_session_cache.clear()
    transport_module._stateless_categories.clear()
    transport_module._inflight_init.clear()
    transport_module._http_session = None
    yield


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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


class _AsyncContextManager:
    """Async context manager that returns a given value on enter."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


class _SessionMock:
    """Mock aiohttp.ClientSession that works with ``async with`` and yields
    responses from a chain for each ``post()`` call.
    """

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


def _make_session_mock(response_chain: list):
    """Return an aiohttp.ClientSession mock that yields responses in order."""
    return _SessionMock(response_chain)


# ------------------------------------------------------------------
# _generate_req_id
# ------------------------------------------------------------------

def test_generate_req_id_format():
    rid = _generate_req_id("mcp")
    assert rid.startswith("mcp_")
    assert rid.split("_")[1].isdigit()


# ------------------------------------------------------------------
# _send_raw_json_rpc
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_raw_json_rpc_success():
    resp = _make_response(text=json.dumps({"jsonrpc": "2.0", "result": {"tools": []}}))
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        result, new_sid = await _send_raw_json_rpc(
            "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "tools/list"}
        )
    assert result == {"tools": []}
    assert new_sid is None


@pytest.mark.asyncio
async def test_send_raw_json_rpc_returns_session_id():
    resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": "ok"}),
        headers={"mcp-session-id": "sess-123"},
    )
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        result, new_sid = await _send_raw_json_rpc(
            "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}
        )
    assert result == "ok"
    assert new_sid == "sess-123"


@pytest.mark.asyncio
async def test_send_raw_json_rpc_http_error():
    resp = _make_response(status=500, text="Internal Server Error")
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(McpHttpError) as exc_info:
            await _send_raw_json_rpc(
                "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}
            )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_send_raw_json_rpc_rpc_error():
    resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request"},
        })
    )
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(McpRpcError) as exc_info:
            await _send_raw_json_rpc(
                "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}
            )
    assert exc_info.value.code == -32600


@pytest.mark.asyncio
async def test_send_raw_json_rpc_empty_body():
    resp = _make_response(text="")
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        result, new_sid = await _send_raw_json_rpc(
            "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}
        )
    assert result is None


@pytest.mark.asyncio
async def test_send_raw_json_rpc_204_no_content():
    resp = _make_response(status=204)
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        result, new_sid = await _send_raw_json_rpc(
            "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}
        )
    assert result is None


@pytest.mark.asyncio
async def test_send_raw_json_rpc_sends_session_header():
    resp = _make_response(text=json.dumps({"jsonrpc": "2.0", "result": "ok"}))
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        await _send_raw_json_rpc(
            "http://example.com/mcp",
            McpSession(session_id="sess-abc"),
            {"jsonrpc": "2.0", "method": "ping"},
        )

    # _SessionMock.post is a plain function; inspect the last call via the
    # *args / **kwargs captured by the mock session wrapper.
    assert session._last_post_kwargs["headers"]["Mcp-Session-Id"] == "sess-abc"


@pytest.mark.asyncio
async def test_send_raw_json_rpc_timeout():
    class TimeoutSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, *args, **kwargs):
            raise asyncio.TimeoutError()
        async def close(self):
            pass

    with patch("aiohttp.ClientSession", return_value=TimeoutSession()):
        with pytest.raises(McpHttpError) as exc_info:
            await _send_raw_json_rpc(
                "http://example.com/mcp", McpSession(), {"jsonrpc": "2.0", "method": "ping"}, timeout_ms=100
            )
    assert exc_info.value.status_code == 408


# ------------------------------------------------------------------
# _parse_sse_response
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_sse_single_event():
    response = AsyncMock()
    response.text = AsyncMock(return_value="data: {\"jsonrpc\":\"2.0\",\"result\":{\"x\":1}}\n\n")
    result = await _parse_sse_response(response)
    assert result == {"x": 1}


@pytest.mark.asyncio
async def test_parse_sse_multi_event_takes_last():
    response = AsyncMock()
    response.text = AsyncMock(return_value=(
        "data: {\"jsonrpc\":\"2.0\",\"result\":{\"n\":1}}\n\n"
        "data: {\"jsonrpc\":\"2.0\",\"result\":{\"n\":2}}\n\n"
    ))
    result = await _parse_sse_response(response)
    assert result == {"n": 2}


@pytest.mark.asyncio
async def test_parse_sse_no_data_raises():
    response = AsyncMock()
    response.text = AsyncMock(return_value="event: ping\n\n")
    with pytest.raises(McpHttpError) as exc_info:
        await _parse_sse_response(response)
    assert "no valid data" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_parse_sse_rpc_error():
    response = AsyncMock()
    response.text = AsyncMock(return_value=(
        "data: {\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32603,\"message\":\"boom\"}}\n\n"
    ))
    with pytest.raises(McpRpcError) as exc_info:
        await _parse_sse_response(response)
    assert exc_info.value.code == -32603


@pytest.mark.asyncio
async def test_parse_sse_multiline_data():
    response = AsyncMock()
    response.text = AsyncMock(return_value=(
        "data: {\"jsonrpc\":\"2.0\",\n"
        "data: \"result\":{\"ok\":true}}\n\n"
    ))
    result = await _parse_sse_response(response)
    assert result == {"ok": True}


# ------------------------------------------------------------------
# initialize_session (stateful)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_session_stateful():
    """Server returns session-id -> stateful session."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        headers={"mcp-session-id": "sess-42"},
    )
    notify_resp = _make_response(status=204)
    session = _make_session_mock([init_resp, notify_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        sess = await initialize_session("http://example.com/mcp", "contact")

    assert sess.session_id == "sess-42"
    assert sess.initialized is True
    assert sess.stateless is False
    assert transport_module._mcp_session_cache["contact"] is sess


# ------------------------------------------------------------------
# initialize_session (stateless)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_session_stateless():
    """Server does NOT return session-id -> stateless session."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
    )
    session = _make_session_mock([init_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        sess = await initialize_session("http://example.com/mcp", "doc")

    assert sess.session_id is None
    assert sess.initialized is True
    assert sess.stateless is True
    assert "doc" in transport_module._stateless_categories


# ------------------------------------------------------------------
# get_or_create_session — caching
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_create_session_returns_cached():
    cached = McpSession(session_id="sess-99", initialized=True, stateless=False)
    transport_module._mcp_session_cache["contact"] = cached

    sess = await get_or_create_session("http://example.com/mcp", "contact")
    assert sess is cached


@pytest.mark.asyncio
async def test_get_or_create_session_creates_new():
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
        headers={"mcp-session-id": "sess-new"},
    )
    notify_resp = _make_response(status=204)
    session = _make_session_mock([init_resp, notify_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        sess = await get_or_create_session("http://example.com/mcp", "msg")

    assert sess.session_id == "sess-new"
    assert sess.initialized is True


# ------------------------------------------------------------------
# get_or_create_session — concurrent init deduplication
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_create_session_dedup_concurrent_init():
    """Two concurrent calls should share a single initialize."""
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
        headers={"mcp-session-id": "sess-dedup"},
    )
    notify_resp = _make_response(status=204)
    session = _make_session_mock([init_resp, notify_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        results = await asyncio.gather(
            get_or_create_session("http://example.com/mcp", "todo"),
            get_or_create_session("http://example.com/mcp", "todo"),
        )

    assert results[0] is results[1]
    assert results[0].session_id == "sess-dedup"
    # Only one POST for init, one for notify = 2 total
    assert session.post_call_count == 2


# ------------------------------------------------------------------
# rebuild_session
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rebuild_session_clears_and_reinitializes():
    old = McpSession(session_id="sess-old", initialized=True, stateless=False)
    transport_module._mcp_session_cache["contact"] = old

    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
        headers={"mcp-session-id": "sess-rebuilt"},
    )
    notify_resp = _make_response(status=204)
    session = _make_session_mock([init_resp, notify_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        sess = await rebuild_session("http://example.com/mcp", "contact")

    assert sess.session_id == "sess-rebuilt"
    assert transport_module._mcp_session_cache["contact"] is sess


# ------------------------------------------------------------------
# send_json_rpc — auto-rebuild on 404 with retry-once
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_json_rpc_auto_rebuild_on_404():
    """First call 404s, session is rebuilt, second call succeeds."""
    transport_module._mcp_config_cache["contact"] = {"url": "http://example.com/mcp"}

    # Initial session init + notify
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
        headers={"mcp-session-id": "sess-1"},
    )
    notify_resp = _make_response(status=204)
    # First RPC: 404
    fail_resp = _make_response(status=404, text="Not Found")
    # Rebuild init + notify
    init_resp2 = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
        headers={"mcp-session-id": "sess-rebuilt"},
    )
    notify_resp2 = _make_response(status=204)
    # Retry RPC
    ok_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {"ok": True}})
    )

    session = _make_session_mock([init_resp, notify_resp, fail_resp, init_resp2, notify_resp2, ok_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        result = await send_json_rpc("contact", "tools/list")

    assert result == {"ok": True}
    assert transport_module._mcp_session_cache["contact"].session_id == "sess-rebuilt"


@pytest.mark.asyncio
async def test_send_json_rpc_404_on_stateless_raises():
    """Stateless servers should NOT retry on 404."""
    transport_module._mcp_config_cache["doc"] = {"url": "http://example.com/mcp"}
    transport_module._stateless_categories.add("doc")

    fail_resp = _make_response(status=404, text="Not Found")
    session = _make_session_mock([fail_resp])

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(McpHttpError) as exc_info:
            await send_json_rpc("doc", "tools/list")
    assert exc_info.value.status_code == 404


# ------------------------------------------------------------------
# send_json_rpc — cache clearing on error codes
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_json_rpc_clears_cache_on_specific_error_codes():
    for code in CACHE_CLEAR_ERROR_CODES:
        transport_module._mcp_config_cache.clear()
        transport_module._mcp_session_cache.clear()
        transport_module._http_session = None
        transport_module._mcp_config_cache["contact"] = {"url": "http://example.com/mcp"}
        transport_module._mcp_session_cache["contact"] = McpSession(
            session_id="sess-1", initialized=True
        )

        resp = _make_response(
            text=json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": code, "message": "expired"},
            })
        )
        session = _make_session_mock([resp])

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(McpRpcError):
                await send_json_rpc("contact", "tools/list")

        assert "contact" not in transport_module._mcp_config_cache
        assert "contact" not in transport_module._mcp_session_cache


@pytest.mark.asyncio
async def test_send_json_rpc_does_not_clear_cache_on_other_rpc_errors():
    transport_module._mcp_config_cache["contact"] = {"url": "http://example.com/mcp"}
    transport_module._mcp_session_cache["contact"] = McpSession(
        session_id="sess-1", initialized=True
    )

    resp = _make_response(
        text=json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "bad request"},
        })
    )
    session = _make_session_mock([resp])

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(McpRpcError):
            await send_json_rpc("contact", "tools/list")

    assert "contact" in transport_module._mcp_config_cache
    assert "contact" in transport_module._mcp_session_cache


# ------------------------------------------------------------------
# fetch_mcp_config
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_mcp_config_from_cache():
    transport_module._mcp_config_cache["contact"] = {"url": "http://cached.example"}
    cfg = await fetch_mcp_config("contact")
    assert cfg == {"url": "http://cached.example"}


@pytest.mark.asyncio
async def test_fetch_mcp_config_from_env(monkeypatch):
    monkeypatch.setenv(
        "WECOM_MCP_CONFIG",
        json.dumps({"doc": "http://env.example/doc"}),
    )
    cfg = await fetch_mcp_config("doc")
    assert cfg == {"url": "http://env.example/doc"}


@pytest.mark.asyncio
async def test_fetch_mcp_config_from_env_dict_form(monkeypatch):
    monkeypatch.setenv(
        "WECOM_MCP_CONFIG",
        json.dumps({"doc": {"url": "http://env.example/doc"}}),
    )
    cfg = await fetch_mcp_config("doc")
    assert cfg == {"url": "http://env.example/doc"}


@pytest.mark.asyncio
async def test_fetch_mcp_config_missing_raises():
    with pytest.raises(RuntimeError, match="MCP config unavailable"):
        await fetch_mcp_config("missing")


@pytest.mark.asyncio
async def test_fetch_mcp_config_from_adapter(monkeypatch):
    """Config fetched from WeCom adapter via _get_wecom_adapter."""
    fake_adapter = MagicMock()
    fake_adapter.get_mcp_configs.return_value = {"contact": "http://adapter.example"}

    fake_runner = MagicMock()
    fake_runner.adapter = fake_adapter

    monkeypatch.setattr(
        transport_module,
        "_get_wecom_adapter",
        lambda: fake_adapter,
    )

    cfg = await fetch_mcp_config("contact")
    assert cfg == {"url": "http://adapter.example"}


# ------------------------------------------------------------------
# clear_category_cache
# ------------------------------------------------------------------

def test_clear_category_cache_wipes_everything():
    transport_module._mcp_config_cache["x"] = {"url": "u"}
    transport_module._mcp_session_cache["x"] = McpSession()
    transport_module._stateless_categories.add("x")
    transport_module._inflight_init["x"] = asyncio.Future()

    clear_category_cache("x")

    assert "x" not in transport_module._mcp_config_cache
    assert "x" not in transport_module._mcp_session_cache
    assert "x" not in transport_module._stateless_categories
    assert "x" not in transport_module._inflight_init


# ------------------------------------------------------------------
# SSE via send_json_rpc (integration path)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_json_rpc_sse_response():
    transport_module._mcp_config_cache["contact"] = {"url": "http://example.com/mcp"}

    # init + notify for session setup
    init_resp = _make_response(
        text=json.dumps({"jsonrpc": "2.0", "result": {}}),
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

    with patch("aiohttp.ClientSession", return_value=session):
        result = await send_json_rpc("contact", "tools/list")

    assert result == {"tools": [{"name": "t1"}]}
