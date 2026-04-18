"""Integration tests for tools/wecom_mcp_tool.py.

Tests the full tool handler with mocked transport layer.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tools.wecom_mcp_tool import handle_wecom_mcp, _check_wecom_configured


@pytest.fixture(autouse=True)
def _clear_transport_caches():
    """Wipe transport caches before each test."""
    import tools.wecom_mcp.transport as t
    t._mcp_config_cache.clear()
    t._mcp_session_cache.clear()
    t._stateless_categories.clear()
    t._inflight_init.clear()
    yield


# ------------------------------------------------------------------
# check_fn
# ------------------------------------------------------------------

class TestCheckFn:
    def test_check_fn_true_via_env_bot_id(self, monkeypatch):
        monkeypatch.setenv("WECOM_BOT_ID", "bot-1")
        monkeypatch.setenv("WECOM_SECRET", "secret-1")
        assert _check_wecom_configured() is True

    def test_check_fn_false_via_env_corp_id_only(self, monkeypatch):
        """corp_id + corp_secret alone does NOT enable MCP (needs bot_id + secret)."""
        monkeypatch.setenv("WECOM_CORP_ID", "corp-1")
        monkeypatch.setenv("WECOM_CORP_SECRET", "secret-1")
        assert _check_wecom_configured() is False

    def test_check_fn_false_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("WECOM_BOT_ID", raising=False)
        monkeypatch.delenv("WECOM_SECRET", raising=False)
        monkeypatch.delenv("WECOM_CORP_ID", raising=False)
        monkeypatch.delenv("WECOM_CORP_SECRET", raising=False)
        assert _check_wecom_configured() is False

    def test_check_fn_true_via_gateway_config(self, monkeypatch):
        monkeypatch.delenv("WECOM_BOT_ID", raising=False)
        monkeypatch.delenv("WECOM_SECRET", raising=False)
        monkeypatch.delenv("WECOM_CORP_ID", raising=False)
        monkeypatch.delenv("WECOM_CORP_SECRET", raising=False)

        fake_config = {
            "platforms": {
                "wecom": {"enabled": True},
            }
        }

        from gateway.config import Platform

        def fake_load():
            class Cfg:
                platforms = {
                    Platform.WECOM: type("P", (), {"enabled": True})(),
                    Platform.WECOM_CALLBACK: type("P", (), {"enabled": False})(),
                }
            return Cfg()

        monkeypatch.setattr(
            "gateway.config.load_gateway_config",
            fake_load,
        )
        assert _check_wecom_configured() is True


# ------------------------------------------------------------------
# list action
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_action_returns_tools(monkeypatch):
    tools_result = {
        "tools": [
            {"name": "get_userlist", "inputSchema": {"type": "object"}},
            {"name": "get_user", "inputSchema": {"$defs": {"Foo": {}}, "type": "object"}},
        ]
    }

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        assert category == "contact"
        assert method == "tools/list"
        return tools_result

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "list", "category": "contact"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["category"] == "contact"
    assert parsed["count"] == 2
    assert len(parsed["tools"]) == 2
    # Schema cleaning should have removed $defs
    assert "$defs" not in parsed["tools"][1]["inputSchema"]


@pytest.mark.asyncio
async def test_list_action_empty_tools(monkeypatch):
    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        return {"tools": []}

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "list", "category": "msg"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["count"] == 0
    assert parsed["tools"] == []


@pytest.mark.asyncio
async def test_list_action_non_dict_result(monkeypatch):
    """If result is not a dict, return empty tools list."""
    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        return None

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "list", "category": "contact"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["count"] == 0
    assert parsed["tools"] == []


# ------------------------------------------------------------------
# call action
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_action_with_string_args(monkeypatch):
    rpc_calls: list = []

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        rpc_calls.append({"category": category, "method": method, "params": params, "timeout_ms": timeout_ms})
        return {"content": [{"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}]}

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {
            "action": "call",
            "category": "contact",
            "method": "get_userlist",
            "args": '{"department_id": 1}',
        },
        task_id="test-task",
    )
    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0

    assert len(rpc_calls) == 1
    assert rpc_calls[0]["category"] == "contact"
    assert rpc_calls[0]["method"] == "tools/call"
    assert rpc_calls[0]["params"]["name"] == "get_userlist"
    assert rpc_calls[0]["params"]["arguments"]["department_id"] == 1


@pytest.mark.asyncio
async def test_call_action_with_dict_args(monkeypatch):
    rpc_calls: list = []

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        rpc_calls.append({"category": category, "method": method, "params": params, "timeout_ms": timeout_ms})
        return {"content": [{"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}]}

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {
            "action": "call",
            "category": "contact",
            "method": "get_userlist",
            "args": {"department_id": 2},
        },
        task_id="test-task",
    )
    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0
    assert rpc_calls[0]["params"]["arguments"]["department_id"] == 2


@pytest.mark.asyncio
async def test_call_action_with_none_args(monkeypatch):
    rpc_calls: list = []

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        rpc_calls.append({"category": category, "method": method, "params": params, "timeout_ms": timeout_ms})
        return {"content": [{"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}]}

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "call", "category": "contact", "method": "get_userlist"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0
    assert rpc_calls[0]["params"]["arguments"] == {}


@pytest.mark.asyncio
async def test_call_action_interceptor_timeout_applied(monkeypatch):
    """Media interceptor should bump timeout for get_msg_media."""
    rpc_calls: list = []

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        rpc_calls.append({"category": category, "method": method, "params": params, "timeout_ms": timeout_ms})
        return {"content": [{"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}]}

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {
            "action": "call",
            "category": "msg",
            "method": "get_msg_media",
            "args": {"media_id": "mid-1"},
        },
        task_id="test-task",
    )
    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 0
    assert rpc_calls[0]["timeout_ms"] == 120_000  # MediaInterceptor timeout


@pytest.mark.asyncio
async def test_call_action_interceptor_after_call(monkeypatch):
    """BizErrorInterceptor should clear cache on errcode 850002."""
    import tools.wecom_mcp.transport as t
    t._mcp_config_cache["contact"] = {"url": "http://example.com"}
    t._mcp_session_cache["contact"] = t.McpSession(session_id="sess-1", initialized=True)

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        return {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 850002, "errmsg": "token expired"})}
            ]
        }

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "call", "category": "contact", "method": "get_userlist", "args": {}},
        task_id="test-task",
    )
    parsed = json.loads(result)
    inner = json.loads(parsed["content"][0]["text"])
    assert inner["errcode"] == 850002
    # Cache should have been cleared by biz_error interceptor
    assert "contact" not in t._mcp_config_cache
    assert "contact" not in t._mcp_session_cache


# ------------------------------------------------------------------
# Missing action / category guards
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_action_returns_error_json():
    """Empty action should return a helpful error instead of 'Unknown action:'."""
    result = await handle_wecom_mcp(
        {"action": "", "category": "contact"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_MISSING_ACTION"
    assert "Missing 'action' parameter" in parsed["message"]


@pytest.mark.asyncio
async def test_missing_category_returns_error_json():
    """Empty category should return a helpful error."""
    result = await handle_wecom_mcp(
        {"action": "list", "category": ""},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_MISSING_CATEGORY"
    assert "Missing 'category' parameter" in parsed["message"]


@pytest.mark.asyncio
async def test_none_action_returns_error_json():
    """None action should be treated as missing."""
    result = await handle_wecom_mcp(
        {"category": "contact"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_MISSING_ACTION"


@pytest.mark.asyncio
async def test_none_category_returns_error_json():
    """None category should be treated as missing."""
    result = await handle_wecom_mcp(
        {"action": "list"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_MISSING_CATEGORY"


# ------------------------------------------------------------------
# Config guard
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unconfigured_category_returns_error_json():
    """When MCP config is missing, transport should raise a clear RuntimeError."""
    import tools.wecom_mcp.transport as t
    t._mcp_config_cache.clear()

    result = await handle_wecom_mcp(
        {"action": "list", "category": "nonexistent"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_CONFIG_ERROR"
    assert "MCP config unavailable" in parsed["message"]


# ------------------------------------------------------------------
# Unknown action
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_action_returns_error_json():
    result = await handle_wecom_mcp(
        {"action": "delete", "category": "contact"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_UNEXPECTED_ERROR"
    assert "Unknown action: delete" in parsed["message"]


# ------------------------------------------------------------------
# Dispatch-path tests (registry calling convention)
# ------------------------------------------------------------------


class TestHandleWecomMcpDispatch:
    """Tests that verify the handler works when called exactly as registry.dispatch() does."""

    @pytest.mark.asyncio
    async def test_list_accepts_args_dict_and_kwargs(self):
        """Registry passes args as dict + kwargs — handler must unpack correctly."""
        with patch("tools.wecom_mcp_tool.send_json_rpc", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"tools": [{"name": "get_userlist", "inputSchema": {}}]}
            result = await handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="test-task",
            )
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert parsed["category"] == "contact"
            assert len(parsed["tools"]) == 1
            assert parsed["tools"][0]["name"] == "get_userlist"
            mock_send.assert_awaited_once_with("contact", "tools/list")

    @pytest.mark.asyncio
    async def test_call_accepts_args_dict_and_kwargs(self):
        with patch("tools.wecom_mcp_tool.send_json_rpc", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"content": [{"type": "text", "text": "{}"}]}
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_userlist",
                    "args": "{}",
                },
                task_id="test-task",
            )
            assert isinstance(result, str)
            mock_send.assert_awaited_once()
            call_args = mock_send.call_args
            assert call_args[0][0] == "contact"
            assert call_args[0][1] == "tools/call"
            assert call_args[0][2] == {"name": "get_userlist", "arguments": {}}

    @pytest.mark.asyncio
    async def test_call_with_dict_args(self):
        with patch("tools.wecom_mcp_tool.send_json_rpc", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"content": [{"type": "text", "text": "{}"}]}
            result = await handle_wecom_mcp(
                {
                    "action": "call",
                    "category": "contact",
                    "method": "get_userlist",
                    "args": {"chat_type": 1},
                },
                task_id="test-task",
            )
            assert isinstance(result, str)
            call_args = mock_send.call_args
            assert call_args[0][2]["arguments"] == {"chat_type": 1}

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await handle_wecom_mcp(
            {"action": "invalid", "category": "contact"},
            task_id="test-task",
        )
        assert "error" in result.lower() or "unknown" in result.lower()
        parsed = json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_dispatch_via_registry(self):
        """Simulate exactly how registry.dispatch() calls the handler.

        registry.dispatch() for async handlers does:
            _run_async(entry.handler(args, **kwargs))

        The key invariant: entry.handler(args, **kwargs) must return a
        coroutine (not raise TypeError about unexpected args / missing params).
        """
        with patch("tools.wecom_mcp_tool.send_json_rpc", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"tools": []}
            # This is what registry.dispatch does — handler(args_dict, **kwargs)
            coro = handle_wecom_mcp(
                {"action": "list", "category": "contact"},
                task_id="some-task-id",
                user_task="some-task",
            )
            # Must be a coroutine (async def), not a plain value or exception
            assert hasattr(coro, "__await__")
            result = await coro
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert parsed["category"] == "contact"
            assert parsed["count"] == 0


# ------------------------------------------------------------------
# Error 846610 — category not enabled
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_action_returns_friendly_error_on_846610(monkeypatch):
    """Error 846610 'unsupported mcp biz type' should return actionable guidance."""
    from tools.wecom_mcp.transport import McpRpcError

    async def fake_send_json_rpc(category, method, params=None, *, timeout_ms=30000):
        raise McpRpcError(846610, "unsupported mcp biz type")

    monkeypatch.setattr(
        "tools.wecom_mcp_tool.send_json_rpc",
        fake_send_json_rpc,
    )

    result = await handle_wecom_mcp(
        {"action": "list", "category": "meeting"},
        task_id="test-task",
    )
    parsed = json.loads(result)
    assert parsed["error"] == "MCP_CATEGORY_NOT_ENABLED"
    assert "meeting" in parsed["message"]
    assert "WeCom admin panel" in parsed["message"]
    assert parsed["category"] == "meeting"


# ------------------------------------------------------------------
# Dynamic schema
# ------------------------------------------------------------------

class TestDynamicSchema:
    def test_schema_uses_enum_when_categories_available(self, monkeypatch):
        """When the adapter reports available categories, schema should use enum."""
        fake_adapter = type("Adapter", (), {
            "get_available_mcp_categories": lambda self: ["doc", "msg"],
        })()
        fake_runner = type("Runner", (), {"adapter": fake_adapter})()

        monkeypatch.setattr(
            "tools.wecom_mcp_tool._get_available_categories",
            lambda: ["doc", "msg"],
        )

        from tools.wecom_mcp_tool import _build_wecom_mcp_schema
        schema = _build_wecom_mcp_schema()
        category = schema["properties"]["category"]
        assert category["type"] == "string"
        assert category["enum"] == ["doc", "msg"]
        assert "doc" in category["description"]
        assert "msg" in category["description"]

    def test_schema_fallback_when_no_categories(self, monkeypatch):
        """When no adapter is available, schema should be free-form string."""
        monkeypatch.setattr(
            "tools.wecom_mcp_tool._get_available_categories",
            lambda: [],
        )

        from tools.wecom_mcp_tool import _build_wecom_mcp_schema
        schema = _build_wecom_mcp_schema()
        category = schema["properties"]["category"]
        assert category["type"] == "string"
        assert "enum" not in category
        assert "contact" in category["description"]
        assert "smartsheet" in category["description"]
