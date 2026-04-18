import json
import logging
from typing import Any

from gateway.platforms.base import cache_document_from_bytes
from .types import CallContext

logger = logging.getLogger(__name__)


class SmartpageExportInterceptor:
    name = "smartpage-export"

    def match(self, ctx: CallContext) -> bool:
        return (
            ctx["category"] == "doc"
            and ctx["method"] == "smartpage_get_export_result"
        )

    async def after_call(self, ctx: CallContext, result: Any) -> Any:
        return await _intercept_export(result)


async def _intercept_export(result: Any) -> Any:
    content = _extract_text_content(result)
    if content is None:
        logger.debug("smartpage_export: no text content in result, skipping")
        return result

    try:
        biz = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("smartpage_export: failed to parse result JSON: %s", exc)
        return result

    if not isinstance(biz, dict):
        return result
    if biz.get("errcode") != 0:
        return result
    if biz.get("task_done") is not True:
        logger.debug("smartpage_export: task not done yet, skipping")
        return result
    if not isinstance(biz.get("content"), str):
        return result

    markdown = biz["content"]
    buffer = markdown.encode("utf-8")
    local_path = cache_document_from_bytes(buffer, filename="smartpage_export.md")

    logger.info(
        "smartpage_export: saved markdown size=%d bytes path=%s",
        len(buffer), local_path,
    )

    new_biz = {
        "errcode": biz.get("errcode", 0),
        "errmsg": biz.get("errmsg", "ok"),
        "task_done": True,
        "content_path": local_path,
    }

    return {
        "content": [{"type": "text", "text": json.dumps(new_biz)}],
    }


def _extract_text_content(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
    return None
