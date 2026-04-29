"""MCP tool call interceptors for WeCom-specific business logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPRequest:
    tool: str
    params: Dict[str, Any]
    response_error: Optional[Dict[str, Any]] = None
    retry: bool = False
    modified_params: Dict[str, Any] = field(default_factory=dict)


Interceptor = Callable[[MCPRequest], MCPRequest]


class InterceptorChain:
    """Ordered chain of interceptors for MCP tool calls."""

    def __init__(self, interceptors: List[Interceptor]):
        self._interceptors = interceptors

    def execute(self, request: MCPRequest) -> MCPRequest:
        for interceptor in self._interceptors:
            request = interceptor(request)
        return request


class BizErrorInterceptor:
    """Detects WeCom business errors and clears affected caches."""

    CLEAR_CACHE_CODES = {850001, 851014}

    def __init__(self, clear_cache_fn: Callable[[], None]):
        self._clear_cache = clear_cache_fn

    def intercept(self, request: MCPRequest) -> MCPRequest:
        error = request.response_error or {}
        errcode = error.get("errcode", 0)
        if errcode in self.CLEAR_CACHE_CODES:
            logger.info("[wecom][interceptor] Clearing category cache on errcode=%s", errcode)
            self._clear_cache()
            request.retry = True
        return request
