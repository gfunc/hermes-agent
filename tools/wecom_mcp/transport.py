"""WeCom MCP Streamable HTTP transport layer.

Handles JSON-RPC over HTTP, session lifecycle, SSE parsing, config caching,
auto-rebuild on 404, and cache clearing on specific JSON-RPC error codes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)
LOG_TAG = "[wecom_mcp]"
HTTP_REQUEST_TIMEOUT_MS = 30_000
CACHE_CLEAR_ERROR_CODES: Set[int] = {-32001, -32002, -32003}

# Module-level aiohttp session (lazy-initialized, auto-closed on module unload)
_http_session: Optional[aiohttp.ClientSession] = None


def _get_http_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp ClientSession, creating if needed."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


class McpRpcError(Exception):
    """JSON-RPC error from MCP Server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class McpHttpError(Exception):
    """HTTP-level error from MCP Server."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class McpSession:
    session_id: Optional[str] = None
    initialized: bool = False
    stateless: bool = False


_mcp_config_cache: Dict[str, dict] = {}
_mcp_session_cache: Dict[str, McpSession] = {}
_stateless_categories: Set[str] = set()
_inflight_init: Dict[str, asyncio.Future] = {}


def _generate_req_id(prefix: str = "mcp") -> str:
    return f"{prefix}_{int(time.time() * 1000)}"


def _get_wecom_adapter() -> Optional[Any]:
    """Try to get a live WeCom adapter instance.

    Tries the GatewayRunner singleton first, then falls back to env vars.
    """
    try:
        from gateway.run import GatewayRunner
        runner = getattr(GatewayRunner, "_instance", None)
        if runner is not None:
            # Try runner.adapter first, then runner.adapters[Platform.WECOM]
            adapter = getattr(runner, "adapter", None)
            if adapter is not None and hasattr(adapter, "get_mcp_configs"):
                return adapter
            adapters = getattr(runner, "adapters", None)
            if adapters is not None:
                try:
                    from gateway.config import Platform
                    wecom_adapter = adapters.get(Platform.WECOM)
                    if wecom_adapter is not None and hasattr(wecom_adapter, "get_mcp_configs"):
                        return wecom_adapter
                except Exception:
                    pass
    except Exception:
        pass
    return None


async def fetch_mcp_config(category: str) -> dict:
    """Fetch full MCP config dict for a category.

    Priority: in-memory cache -> WeCom adapter cache -> WeCom adapter
    on-demand fetch -> WECOM_MCP_CONFIG env var.
    """
    cached = _mcp_config_cache.get(category)
    if cached is not None:
        logger.debug("%s Config cache hit for '%s'", LOG_TAG, category)
        return cached

    adapter = _get_wecom_adapter()
    if adapter is not None:
        # 1. Try adapter's connect-time cached configs
        try:
            configs = adapter.get_mcp_configs()
            if category in configs:
                cfg = {"url": configs[category]}
                _mcp_config_cache[category] = cfg
                logger.info("%s Config from adapter cache '%s': %s", LOG_TAG, category, cfg["url"])
                return cfg
        except Exception as exc:
            logger.warning("%s Adapter config cache read failed: %s", LOG_TAG, exc)

        # 2. On-demand fetch via active WebSocket (OpenClaw-style dynamic pull)
        if hasattr(adapter, "refresh_mcp_config"):
            try:
                url = await adapter.refresh_mcp_config(category)
                if url:
                    cfg = {"url": url}
                    _mcp_config_cache[category] = cfg
                    logger.info("%s Config from adapter on-demand '%s': %s", LOG_TAG, category, url)
                    return cfg
            except Exception as exc:
                logger.warning("%s Adapter on-demand fetch failed: %s", LOG_TAG, exc)

    env_raw = os.getenv("WECOM_MCP_CONFIG", "")
    if env_raw:
        try:
            env_map = json.loads(env_raw)
            if isinstance(env_map, dict) and category in env_map:
                cfg = env_map[category]
                if isinstance(cfg, str):
                    cfg = {"url": cfg}
                if isinstance(cfg, dict) and cfg.get("url"):
                    _mcp_config_cache[category] = cfg
                    logger.info("%s Config from env '%s': %s", LOG_TAG, category, cfg["url"])
                    return cfg
        except json.JSONDecodeError:
            logger.warning("%s WECOM_MCP_CONFIG is not valid JSON", LOG_TAG)

    logger.warning(
        "%s MCP config unavailable for category '%s' — no adapter connection and no WECOM_MCP_CONFIG env var",
        LOG_TAG, category, exc_info=True,
    )
    raise RuntimeError(
        f"MCP config unavailable for category '{category}'. "
        f"Ensure WeCom adapter is connected or set WECOM_MCP_CONFIG env var."
    )


async def _get_mcp_url(category: str) -> str:
    cached = _mcp_config_cache.get(category)
    if cached is not None:
        url = cached.get("url")
        if url:
            return url
    cfg = await fetch_mcp_config(category)
    url = cfg.get("url")
    if not url:
        raise RuntimeError(f"MCP config for '{category}' missing 'url' field")
    return url


async def _send_raw_json_rpc(
    url: str,
    session: McpSession,
    body: dict,
    timeout_ms: int = HTTP_REQUEST_TIMEOUT_MS,
) -> tuple[Any, Optional[str]]:
    """Send raw JSON-RPC POST. Returns (rpc_result, new_session_id).

    Raises McpHttpError on non-2xx HTTP status.
    Raises McpRpcError on JSON-RPC error in response body.
    """
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session.session_id:
        headers["Mcp-Session-Id"] = session.session_id

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    http_session = _get_http_session()

    logger.debug(
        "%s HTTP POST %s method=%s session_id=%s",
        LOG_TAG, url, body.get("method"), session.session_id or "none",
    )

    try:
        async with http_session.post(url, headers=headers, json=body, timeout=timeout) as response:
            new_session_id = response.headers.get("mcp-session-id")
            logger.debug(
                "%s HTTP response %s status=%s new_session_id=%s",
                LOG_TAG, url, response.status, new_session_id or "none",
            )

            if not response.ok:
                text = await response.text()
                raise McpHttpError(
                    response.status,
                    f"MCP HTTP error {response.status}: {text[:200]}",
                )

            content_length = response.headers.get("content-length")
            if response.status == 204 or content_length == "0":
                return None, new_session_id

            content_type = response.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                result = await _parse_sse_response(response)
                return result, new_session_id

            text = await response.text()
            if not text.strip():
                return None, new_session_id

            try:
                rpc = json.loads(text)
            except json.JSONDecodeError as exc:
                raise McpHttpError(200, f"Invalid JSON in MCP response: {exc}") from exc

            if rpc.get("error"):
                err = rpc["error"]
                raise McpRpcError(
                    err.get("code", -32603),
                    f"MCP RPC error [{err.get('code')}]: {err.get('message', 'unknown')}",
                    err.get("data"),
                )
            return rpc.get("result"), new_session_id

    except asyncio.TimeoutError as exc:
            raise McpHttpError(408, f"MCP request timed out after {timeout_ms}ms") from exc


async def _parse_sse_response(response: aiohttp.ClientResponse) -> Any:
    """Parse SSE response, take the LAST complete event, return JSON-RPC result."""
    text = await response.text()
    lines = text.split("\n")

    current_parts: list[str] = []
    last_event_data = ""

    for line in lines:
        if line.startswith("data: "):
            current_parts.append(line[6:])
        elif line.startswith("data:"):
            current_parts.append(line[5:])
        elif line.strip() == "" and current_parts:
            last_event_data = "\n".join(current_parts).strip()
            current_parts = []

    if current_parts:
        last_event_data = "\n".join(current_parts).strip()

    if not last_event_data:
        raise McpHttpError(200, "SSE response contained no valid data")

    try:
        rpc = json.loads(last_event_data)
    except json.JSONDecodeError as exc:
        raise McpHttpError(200, f"SSE JSON parse failed: {last_event_data[:200]}") from exc

    if rpc.get("error"):
        err = rpc["error"]
        raise McpRpcError(
            err.get("code", -32603),
            f"MCP RPC error [{err.get('code')}]: {err.get('message', 'unknown')}",
            err.get("data"),
        )
    return rpc.get("result")


async def initialize_session(url: str, category: str) -> McpSession:
    """Perform Streamable HTTP initialize handshake.

    If no Mcp-Session-Id is returned, marks the server as stateless.
    """
    session = McpSession(session_id=None, initialized=False, stateless=False)
    logger.info("%s Initializing session for '%s'", LOG_TAG, category)

    init_body = {
        "jsonrpc": "2.0",
        "id": _generate_req_id("mcp_init"),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "wecom_mcp", "version": "1.0.0"},
        },
    }

    _, init_session_id = await _send_raw_json_rpc(url, session, init_body)
    if init_session_id:
        session.session_id = init_session_id

    if not session.session_id:
        session.stateless = True
        session.initialized = True
        _stateless_categories.add(category)
        _mcp_session_cache[category] = session
        logger.info("%s Stateless server detected for '%s'", LOG_TAG, category)
        return session

    notify_body = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    _, notify_session_id = await _send_raw_json_rpc(url, session, notify_body)
    if notify_session_id:
        session.session_id = notify_session_id

    session.initialized = True
    _mcp_session_cache[category] = session
    logger.info(
        "%s Stateful session for '%s' (session_id=%s)",
        LOG_TAG, category, session.session_id,
    )
    return session


async def get_or_create_session(url: str, category: str) -> McpSession:
    """Get existing session or create a new one (dedup concurrent init)."""
    if not url:
        raise RuntimeError(f"MCP server not configured for category '{category}'")

    if category in _stateless_categories:
        cached = _mcp_session_cache.get(category)
        if cached is not None:
            logger.debug("%s Session cache hit (stateless) for '%s'", LOG_TAG, category)
            return cached

    cached = _mcp_session_cache.get(category)
    if cached is not None and cached.initialized:
        logger.debug(
            "%s Session cache hit for '%s' (session_id=%s)",
            LOG_TAG, category, cached.session_id or "none",
        )
        return cached

    inflight = _inflight_init.get(category)
    if inflight is not None:
        logger.debug("%s Session init already in-flight for '%s', awaiting...", LOG_TAG, category)
        return await inflight

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _inflight_init[category] = future

    try:
        session = await initialize_session(url, category)
        future.set_result(session)
        return session
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        _inflight_init.pop(category, None)


async def rebuild_session(url: str, category: str) -> McpSession:
    """Clear session cache and re-initialize (dedup concurrent rebuild)."""
    _mcp_session_cache.pop(category, None)
    _stateless_categories.discard(category)

    inflight = _inflight_init.get(category)
    if inflight is not None:
        return await inflight

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _inflight_init[category] = future

    try:
        session = await initialize_session(url, category)
        future.set_result(session)
        return session
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        _inflight_init.pop(category, None)


def clear_category_cache(category: str) -> None:
    """Clear all cached state for a category."""
    logger.info("%s Clearing cache for '%s'", LOG_TAG, category)
    _mcp_config_cache.pop(category, None)
    _mcp_session_cache.pop(category, None)
    _stateless_categories.discard(category)
    _inflight_init.pop(category, None)


async def send_json_rpc(
    category: str,
    method: str,
    params: Optional[dict] = None,
    *,
    timeout_ms: int = HTTP_REQUEST_TIMEOUT_MS,
) -> Any:
    """Send a JSON-RPC request to a WeCom MCP Server.

    Auto-manages session lifecycle, rebuilds on 404, clears cache on
    error codes -32001/-32002/-32003.
    """
    if not category:
        raise RuntimeError("MCP category must be a non-empty string")

    url = await _get_mcp_url(category)
    if not url:
        raise RuntimeError(f"MCP server not configured for category '{category}'")

    body: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": _generate_req_id("mcp_rpc"),
        "method": method,
    }
    if params is not None:
        body["params"] = params

    session = await get_or_create_session(url, category)

    try:
        result, new_session_id = await _send_raw_json_rpc(url, session, body, timeout_ms)
        if new_session_id and session.session_id != new_session_id:
            session.session_id = new_session_id
        return result
    except McpRpcError as exc:
        if exc.code in CACHE_CLEAR_ERROR_CODES:
            clear_category_cache(category)
        raise
    except McpHttpError as exc:
        if session.stateless:
            raise
        if exc.status_code == 404:
            logger.info("%s Session expired for '%s', rebuilding...", LOG_TAG, category)
            _mcp_session_cache.pop(category, None)
            session = await rebuild_session(url, category)
            result, new_session_id = await _send_raw_json_rpc(url, session, body, timeout_ms)
            if new_session_id and session.session_id != new_session_id:
                session.session_id = new_session_id
            return result
        raise
