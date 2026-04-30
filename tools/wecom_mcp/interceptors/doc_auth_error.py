"""Document authorization error interceptor.

When doc MCP calls return errcodes 851013/851014/851008, sends an
aibot_send_biz_msg authorization guidance card to the user and returns
a simplified response to the LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from tools.wecom_mcp.transport import _get_wecom_adapter
from .types import CallContext

logger = logging.getLogger(__name__)

# Document authorization error codes
# https://doc.weixin.qq.com/sheet/e3_AFcARgbdAFwCNU0pwubawRtGzcd6z
DOC_AUTH_ERROR_CODES = {851013, 851014, 851008}

# AiBotBizMsgType enum
AIBOT_BIZ_MSG_TYPE_DOC_READ_AUTH = 1

# AiBotBizMsgChatType enum
AIBOT_BIZ_MSG_CHAT_TYPE_SINGLE = 1
AIBOT_BIZ_MSG_CHAT_TYPE_GROUP = 2

APP_CMD_SEND_BIZ_MSG = "aibot_send_biz_msg"


class DocAuthErrorInterceptor:
    name = "doc-auth-error"

    def match(self, ctx: CallContext) -> bool:
        return ctx["category"] == "doc"

    async def after_call(self, ctx: CallContext, result: Any) -> Any:
        return await _intercept_doc_auth_error(ctx, result)


def _extract_biz_data(result: Any) -> dict | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return None


async def _intercept_doc_auth_error(ctx: CallContext, result: Any) -> Any:
    biz_data = _extract_biz_data(result)
    if not biz_data:
        return result

    errcode = biz_data.get("errcode")
    if not isinstance(errcode, int) or errcode not in DOC_AUTH_ERROR_CODES:
        return result

    logger.info(
        "[wecom_mcp] doc-auth-error: detected errcode=%s, method=%s",
        errcode, ctx["method"],
    )

    adapter = _get_wecom_adapter()
    if adapter is not None:
        await _send_auth_biz_msg(adapter, ctx)
    else:
        logger.warning("[wecom_mcp] doc-auth-error: no adapter available, skipping biz msg")

    simplified = {
        "errcode": errcode,
        "errmsg": biz_data.get("errmsg", "authorization error"),
        "_biz_msg_sent": True,
        "_user_hint": (
            "文档授权提示卡片已直接发送给用户，无需再向用户转述任何授权相关的信息。"
            "请告知用户：已发送授权引导，请按照提示完成授权后重试。"
        ),
    }

    return {
        "content": [{
            "type": "text",
            "text": json.dumps(simplified, ensure_ascii=False),
        }]
    }


async def _send_auth_biz_msg(adapter: Any, ctx: CallContext) -> None:
    """Send aibot_send_biz_msg with doc read auth type."""
    body: dict[str, Any] = {
        "biz_type": AIBOT_BIZ_MSG_TYPE_DOC_READ_AUTH,
    }

    args = ctx.get("args", {})
    chat_id = args.get("chat_id") or args.get("chatid")
    userid = args.get("userid") or args.get("user_id")
    chat_type = args.get("chat_type") or args.get("chattype")

    if chat_id:
        body["chat_id"] = chat_id
    if userid:
        body["userid"] = userid
    if chat_type:
        body["chat_type"] = (
            AIBOT_BIZ_MSG_CHAT_TYPE_GROUP
            if str(chat_type).lower() == "group"
            else AIBOT_BIZ_MSG_CHAT_TYPE_SINGLE
        )

    if not chat_id and not userid:
        logger.warning(
            "[wecom_mcp] doc-auth-error: no chat_id or userid available, skipping biz msg"
        )
        return

    try:
        await adapter._send_request(APP_CMD_SEND_BIZ_MSG, body)
        logger.info("[wecom_mcp] doc-auth-error: biz msg sent successfully")
    except Exception as exc:
        logger.warning("[wecom_mcp] doc-auth-error: failed to send biz msg: %s", exc)
