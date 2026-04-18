"""Tests for WeCom dynamic MCP registration in GatewayRunner."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._smart_model_routing = {}
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_wecom_source() -> SessionSource:
    return SessionSource(
        platform=Platform.WECOM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )


def _make_event(text: str, source: SessionSource = None) -> MessageEvent:
    return MessageEvent(text=text, source=source or _make_wecom_source(), message_id="m1")


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = [{"function": {"name": "existing_tool"}}]
        self.valid_tool_names = {"existing_tool"}
        self._system_prompt_invalidated = False

    def _invalidate_system_prompt(self):
        self._system_prompt_invalidated = True

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


@pytest.mark.asyncio
async def test_run_agent_skips_generic_mcp_for_wecom(monkeypatch):
    """WeCom MCP is handled by the dedicated wecom_mcp tool; generic registration is skipped."""
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    # Mock WeCom adapter with MCP configs
    wecom_adapter = SimpleNamespace(
        get_mcp_configs=lambda: {
            "contact": "https://mcp.example/contact",
            "doc": "https://mcp.example/doc",
        },
        get_pending_message=lambda session_key: None,
    )
    runner.adapters[Platform.WECOM] = wecom_adapter

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    registered_configs = []
    refreshed_agents = []
    invalidated_agents = []

    def fake_register_mcp_servers(config_map):
        registered_configs.append(config_map)

    def fake_get_tool_definitions(*, enabled_toolsets, disabled_toolsets, quiet_mode):
        return [
            {"function": {"name": "existing_tool"}},
            {"function": {"name": "mcp_contact_search"}},
        ]

    fake_mcp_module = types.ModuleType("tools.mcp_tool")
    fake_mcp_module.register_mcp_servers = fake_register_mcp_servers
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_module)

    fake_model_tools = types.ModuleType("model_tools")
    fake_model_tools.get_tool_definitions = fake_get_tool_definitions
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_wecom_source(),
        session_id="session-1",
        session_key="agent:main:wecom:dm:chat-1",
    )

    assert result["final_response"] == "ok"
    # WeCom MCP is now handled by the dedicated `wecom_mcp` tool;
    # generic MCP registration is skipped for WeCom URLs.
    assert len(registered_configs) == 0


@pytest.mark.asyncio
async def test_run_agent_skips_mcp_registration_for_non_wecom_platform(monkeypatch):
    """Non-WeCom platforms should not trigger MCP config registration."""
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    registered_configs = []

    def fake_register_mcp_servers(config_map):
        registered_configs.append(config_map)

    fake_mcp_module = types.ModuleType("tools.mcp_tool")
    fake_mcp_module.register_mcp_servers = fake_register_mcp_servers
    fake_mcp_module._MCP_AVAILABLE = True
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_module)

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    telegram_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=telegram_source,
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "ok"
    assert registered_configs == []


@pytest.mark.asyncio
async def test_run_agent_handles_empty_mcp_configs(monkeypatch):
    """WeCom adapter with empty MCP configs should not attempt registration."""
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    registered_configs = []

    def fake_register_mcp_servers(config_map):
        registered_configs.append(config_map)

    wecom_adapter = SimpleNamespace(
        get_mcp_configs=lambda: {},
        get_pending_message=lambda session_key: None,
    )
    runner.adapters[Platform.WECOM] = wecom_adapter

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    fake_mcp_module = types.ModuleType("tools.mcp_tool")
    fake_mcp_module.register_mcp_servers = fake_register_mcp_servers
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_module)

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_wecom_source(),
        session_id="session-1",
        session_key="agent:main:wecom:dm:chat-1",
    )

    assert result["final_response"] == "ok"
    assert registered_configs == []


@pytest.mark.asyncio
async def test_run_agent_mcp_registration_failure_is_non_fatal(monkeypatch):
    """If register_mcp_servers raises, the agent should still run normally."""
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    wecom_adapter = SimpleNamespace(
        get_mcp_configs=lambda: {
            "contact": "https://mcp.example/contact",
        },
        get_pending_message=lambda session_key: None,
    )
    runner.adapters[Platform.WECOM] = wecom_adapter

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    def fake_register_mcp_servers(config_map):
        raise RuntimeError("MCP registration failed")

    fake_mcp_module = types.ModuleType("tools.mcp_tool")
    fake_mcp_module.register_mcp_servers = fake_register_mcp_servers
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_module)

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_wecom_source(),
        session_id="session-1",
        session_key="agent:main:wecom:dm:chat-1",
    )

    assert result["final_response"] == "ok"
    # Agent should retain original tools since registration failed before refresh
    cached_agent = runner._agent_cache["agent:main:wecom:dm:chat-1"][0]
    assert cached_agent.tools == [{"function": {"name": "existing_tool"}}]


@pytest.mark.asyncio
async def test_run_agent_skips_mcp_when_adapter_missing_get_mcp_configs(monkeypatch):
    """Backward compatibility: adapters without get_mcp_configs should be skipped gracefully."""
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    # Adapter without get_mcp_configs method
    wecom_adapter = SimpleNamespace(
        get_pending_message=lambda session_key: None,
    )
    runner.adapters[Platform.WECOM] = wecom_adapter

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_wecom_source(),
        session_id="session-1",
        session_key="agent:main:wecom:dm:chat-1",
    )

    assert result["final_response"] == "ok"
