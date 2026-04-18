import inspect
import logging
from typing import Any

from .biz_error import BizErrorInterceptor
from .msg_media import MediaInterceptor
from .smartpage_create import SmartpageCreateInterceptor
from .smartpage_export import SmartpageExportInterceptor
from .types import BeforeCallResult, CallContext

logger = logging.getLogger(__name__)

_INTERCEPTORS: list[Any] = [
    BizErrorInterceptor(),
    MediaInterceptor(),
    SmartpageExportInterceptor(),
    SmartpageCreateInterceptor(),
]


async def resolve_before_call(ctx: CallContext) -> BeforeCallResult:
    """Collect matching before_call results and merge them.

    Merge strategy:
    - timeout_ms: maximum of all values
    - args: last wins (later interceptor overrides earlier)
    """
    merged_timeout_ms: int | None = None
    merged_args: dict[str, Any] | None = None

    for interceptor in _INTERCEPTORS:
        if not interceptor.match(ctx):
            continue
        handler = getattr(interceptor, "before_call", None)
        if handler is None:
            continue

        logger.debug(
            "Interceptor %s before_call matched for %s/%s",
            interceptor.name, ctx["category"], ctx["method"],
        )
        result = handler(ctx)
        if inspect.isawaitable(result):
            result = await result

        if result is None:
            continue
        if isinstance(result.get("timeout_ms"), int):
            merged_timeout_ms = (
                result["timeout_ms"]
                if merged_timeout_ms is None
                else max(merged_timeout_ms, result["timeout_ms"])
            )
            logger.debug(
                "Interceptor %s set timeout_ms=%s", interceptor.name, result["timeout_ms"]
            )
        if "args" in result:
            merged_args = result["args"]
            logger.debug("Interceptor %s modified args", interceptor.name)

    # Default timeout_ms if no interceptor set it, to avoid None → TypeError
    if merged_timeout_ms is None:
        merged_timeout_ms = 30_000

    return {"timeout_ms": merged_timeout_ms, "args": merged_args}


async def run_after_call(ctx: CallContext, result: Any) -> Any:
    """Pipe result through all matching interceptors' after_call handlers."""
    current = result
    for interceptor in _INTERCEPTORS:
        if not interceptor.match(ctx):
            continue
        handler = getattr(interceptor, "after_call", None)
        if handler is None:
            continue

        logger.debug(
            "Interceptor %s after_call matched for %s/%s",
            interceptor.name, ctx["category"], ctx["method"],
        )
        current = handler(ctx, current)
        if inspect.isawaitable(current):
            current = await current

    return current
