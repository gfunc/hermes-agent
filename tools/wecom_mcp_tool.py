"""WeCom MCP tool — direct Streamable HTTP client for WeCom MCP servers."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, List

from tools.registry import registry
from tools.wecom_mcp.transport import send_json_rpc, McpRpcError, McpHttpError
from tools.wecom_mcp.schema import clean_schema_for_claude
from tools.wecom_mcp.interceptors import resolve_before_call, run_after_call
from tools.wecom_mcp.interceptors.types import CallContext

logger = logging.getLogger(__name__)

# ── check_fn ──────────────────────────────────────────────────────────────


def _check_wecom_configured() -> bool:
    """WeCom MCP tool only available when bot credentials are present.

    MCP requires the WebSocket long-connection, which is authenticated
    with bot_id + secret. corp_id + corp_secret alone (Agent/webhook
    mode) does not enable MCP.
    """
    if os.getenv("WECOM_BOT_ID") and os.getenv("WECOM_SECRET"):
        return True

    # Check gateway config for enabled WeCom platforms
    try:
        from gateway.config import load_gateway_config, Platform

        config = load_gateway_config()
        for platform in (Platform.WECOM, Platform.WECOM_CALLBACK):
            pconfig = config.platforms.get(platform)
            if pconfig and pconfig.enabled:
                return True
    except Exception:
        pass

    return False


# ── Schema helpers ────────────────────────────────────────────────────────


def _get_available_categories() -> List[str]:
    """Try to get actually-discovered MCP categories from the live adapter."""
    try:
        from gateway.run import GatewayRunner

        runner = getattr(GatewayRunner, "_instance", None)
        if runner is not None:
            adapter = getattr(runner, "adapter", None)
            if adapter is not None and hasattr(adapter, "get_available_mcp_categories"):
                return adapter.get_available_mcp_categories()
            adapters = getattr(runner, "adapters", None)
            if adapters is not None:
                try:
                    from gateway.config import Platform

                    wecom_adapter = adapters.get(Platform.WECOM)
                    if wecom_adapter is not None and hasattr(
                        wecom_adapter, "get_available_mcp_categories"
                    ):
                        return wecom_adapter.get_available_mcp_categories()
                except Exception:
                    pass
    except Exception:
        pass
    return []


def _build_wecom_mcp_schema() -> dict:
    """Build the wecom_mcp tool schema, with dynamic category enum when available."""
    categories = _get_available_categories()

    if categories:
        category_field = {
            "type": "string",
            "enum": categories,
            "description": f"MCP category name. Available: {', '.join(categories)}",
        }
    else:
        category_field = {
            "type": "string",
            "description": (
                "MCP category name: contact, doc, msg, meeting, todo, schedule, smartsheet. "
                "Use 'wecom_mcp list <category>' to discover tools in a category."
            ),
        }

    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "call"],
                "description": "Action: 'list' to enumerate tools or 'call' to invoke a tool. Examples: wecom_mcp list contact, wecom_mcp call contact get_userlist '{}'",
            },
            "category": category_field,
            "method": {
                "type": "string",
                "description": "Tool method name (required for action='call'). Example: get_userlist, get_msg_chat_list",
            },
            "args": {
                "type": ["string", "object"],
                "description": "JSON arguments as string or object (required for action='call', default: {}). Example: '{}' or '{\"chat_type\": 1}'",
            },
        },
        "required": ["action", "category"],
    }


# ── Handler ───────────────────────────────────────────────────────────────


async def handle_wecom_mcp(args: dict, **kwargs: Any) -> str:
    """Handle wecom_mcp tool calls.

    action='list': List available tools in a category
    action='call': Call a specific tool method
    """
    action = args.get("action", "")
    category = args.get("category", "")
    method = args.get("method", "")
    args_param = args.get("args")
    logger.debug("wecom_mcp %s category=%s method=%s", action, category, method or "-")

    if not isinstance(action, str) or not action.strip():
        return json.dumps(
            {
                "error": "MCP_MISSING_ACTION",
                "message": "Missing 'action' parameter. Use 'list' to enumerate tools or 'call' to invoke a tool. Example: wecom_mcp list contact",
            },
            ensure_ascii=False,
        )

    if not isinstance(category, str) or not category.strip():
        return json.dumps(
            {
                "error": "MCP_MISSING_CATEGORY",
                "message": "Missing 'category' parameter. Provide an MCP category such as 'contact', 'meeting', 'doc', 'msg', 'todo', 'schedule', or 'smartsheet'. Example: wecom_mcp list contact",
            },
            ensure_ascii=False,
        )

    try:
        if action == "list":
            result = await send_json_rpc(category, "tools/list")
            tools = result.get("tools", []) if isinstance(result, dict) else []
            logger.debug("wecom_mcp list category=%s returned %d tools", category, len(tools))
            for tool in tools:
                if "inputSchema" in tool:
                    tool["inputSchema"] = clean_schema_for_claude(tool["inputSchema"])
            return json.dumps({"category": category, "count": len(tools), "tools": tools})

        if action == "call":
            parsed_args = json.loads(args_param) if isinstance(args_param, str) else (args_param or {})
            logger.debug(
                "wecom_mcp call category=%s method=%s args_keys=%s",
                category,
                method,
                list(parsed_args.keys()),
            )
            ctx = CallContext(category=category, method=method, args=parsed_args)

            resolved = await resolve_before_call(ctx)
            final_args = resolved.get("args") if resolved.get("args") is not None else parsed_args
            timeout_ms = resolved.get("timeout_ms", 30000)
            if final_args is not parsed_args:
                logger.debug(
                    "wecom_mcp call category=%s method=%s args_modified_by_interceptors",
                    category,
                    method,
                )

            result = await send_json_rpc(
                category,
                "tools/call",
                {"name": method, "arguments": final_args},
                timeout_ms=timeout_ms,
            )

            result = await run_after_call(ctx, result)
            logger.debug("wecom_mcp call category=%s method=%s completed", category, method)
            return json.dumps(result, ensure_ascii=False, indent=2)

        raise ValueError(f"Unknown action: {action}")

    except McpRpcError as exc:
        if exc.code == 846610:
            logger.warning("wecom_mcp category '%s' not enabled for this bot (846610)", category)
            return json.dumps(
                {
                    "error": "MCP_CATEGORY_NOT_ENABLED",
                    "message": (
                        f"Category '{category}' is not enabled for this WeCom bot. "
                        "Enable it in the WeCom admin panel: "
                        "应用管理 → 智能助手 → 企业助手 → 应用能力 → 选择对应分类"
                    ),
                    "category": category,
                },
                ensure_ascii=False,
            )
        logger.warning("wecom_mcp RPC error [%s]: %s", exc.code, exc)
        return json.dumps(
            {"error": "MCP_RPC_ERROR", "code": exc.code, "message": str(exc)},
            ensure_ascii=False,
        )
    except McpHttpError as exc:
        logger.warning("wecom_mcp HTTP error [%s]: %s", exc.status_code, exc)
        return json.dumps(
            {"error": "MCP_HTTP_ERROR", "status_code": exc.status_code, "message": str(exc)},
            ensure_ascii=False,
        )
    except RuntimeError as exc:
        logger.warning("wecom_mcp runtime error: %s", exc)
        return json.dumps(
            {"error": "MCP_CONFIG_ERROR", "message": str(exc)},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.exception("wecom_mcp unexpected error")
        return json.dumps(
            {"error": "MCP_UNEXPECTED_ERROR", "message": str(exc)},
            ensure_ascii=False,
        )


# ── Registration ──────────────────────────────────────────────────────────

_DESCRIPTION = (
    "Call WeCom MCP servers directly via Streamable HTTP protocol. "
    "Use 'wecom_mcp list <category>' to discover tools, then 'wecom_mcp call <category> <method> <args>' to invoke. "
    "If a category returns 'unsupported mcp biz type' (error 846610), it means the category is not enabled for this bot. "
    "Enable it in the WeCom admin panel under 应用管理 → 智能助手 → 企业助手 → 应用能力."
)

registry.register(
    name="wecom_mcp",
    toolset="wecom",
    schema=_build_wecom_mcp_schema(),
    handler=handle_wecom_mcp,
    check_fn=_check_wecom_configured,
    is_async=True,
    description=_DESCRIPTION,
)
