import base64
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from gateway.platforms.base import cache_document_from_bytes, cache_image_from_bytes
from .types import CallContext

logger = logging.getLogger(__name__)

# MIME types that should be treated as images
_IMAGE_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",
    "image/bmp", "image/tiff", "image/svg+xml",
}

# MIME type to extension patches (mimetypes may be missing some)
_MIME_EXT_PATCH = {
    "audio/amr": ".amr",
}


class MediaInterceptor:
    name = "media"

    def match(self, ctx: CallContext) -> bool:
        return ctx["category"] == "msg" and ctx["method"] == "get_msg_media"

    def before_call(self, ctx: CallContext) -> dict[str, Any]:
        return {"timeout_ms": 120_000}  # base64 can be ~27MB

    async def after_call(self, ctx: CallContext, result: Any) -> Any:
        return await _intercept_media(result)


async def _intercept_media(result: Any) -> Any:
    content = _extract_text_content(result)
    if content is None:
        logger.debug("msg_media: no text content in result, skipping")
        return result

    try:
        biz = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("msg_media: failed to parse result JSON: %s", exc)
        return result

    if not isinstance(biz, dict) or biz.get("errcode") != 0:
        logger.debug("msg_media: business error or invalid response, skipping")
        return result

    media_item = biz.get("media_item")
    if not isinstance(media_item, dict) or not isinstance(media_item.get("base64_data"), str):
        logger.debug("msg_media: no base64_data in media_item, skipping")
        return result

    base64_data = media_item["base64_data"]
    media_name = media_item.get("name")
    media_type = media_item.get("type")
    media_id = media_item.get("media_id")

    # Decode base64
    try:
        buffer = base64.b64decode(base64_data)
    except Exception as exc:
        logger.warning("msg_media: base64 decode failed: %s", exc)
        return result

    # Detect MIME type
    content_type = _detect_mime(buffer, media_name)

    # Save to local cache
    if content_type in _IMAGE_TYPES:
        ext = _MIME_EXT_PATCH.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"
        local_path = cache_image_from_bytes(buffer, ext=ext)
    else:
        filename = media_name or "file.bin"
        # Patch extension if missing
        ext = Path(filename).suffix
        if not ext:
            patch = _MIME_EXT_PATCH.get(content_type)
            if patch:
                filename += patch
        local_path = cache_document_from_bytes(buffer, filename=filename)

    logger.info(
        "msg_media: saved media_id=%s type=%s size=%d path=%s",
        media_id, content_type, len(buffer), local_path,
    )

    # Build new response
    new_biz = {
        "errcode": 0,
        "errmsg": "ok",
        "media_item": {
            "media_id": media_id,
            "name": media_name or Path(local_path).name,
            "type": media_type,
            "local_path": local_path,
            "size": len(buffer),
            "content_type": content_type,
        },
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


def _detect_mime(buffer: bytes, filename: Any) -> str:
    # Try magic bytes first
    if buffer.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if buffer.startswith(b"\x89PNG"):
        return "image/png"
    if buffer.startswith(b"GIF87a") or buffer.startswith(b"GIF89a"):
        return "image/gif"
    if buffer.startswith(b"RIFF") and buffer[8:12] == b"WEBP":
        return "image/webp"
    if buffer.startswith(b"#AMR"):
        return "audio/amr"
    if buffer.startswith(b"%PDF"):
        return "application/pdf"

    # Fall back to mimetypes
    if isinstance(filename, str) and filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed

    return "application/octet-stream"
