import json
import logging
from typing import Any

from tools.wecom_mcp.transport import clear_category_cache
from .types import CallContext

logger = logging.getLogger(__name__)

# Business error codes that should trigger cache clear
BIZ_CACHE_CLEAR_CODES = {850002}


class BizErrorInterceptor:
    name = "biz-error"

    def match(self, ctx: CallContext) -> bool:
        return True  # All calls

    def after_call(self, ctx: CallContext, result: Any) -> Any:
        _check_biz_error(result, ctx["category"])
        return result


def _check_biz_error(result: Any, category: str) -> None:
    if not isinstance(result, dict):
        return
    content = result.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("errcode"), int):
            if parsed["errcode"] in BIZ_CACHE_CLEAR_CODES:
                logger.info(
                    "[wecom_mcp] biz-error: detected errcode=%s (category=%r), clearing cache",
                    parsed["errcode"], category,
                )
                clear_category_cache(category)
                return
