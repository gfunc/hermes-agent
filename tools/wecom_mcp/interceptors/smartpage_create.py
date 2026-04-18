import asyncio
import logging
import os
from typing import Any

from .types import CallContext

logger = logging.getLogger(__name__)

MAX_SINGLE_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024   # 20MB


class SmartpageCreateInterceptor:
    name = "smartpage-create"

    def match(self, ctx: CallContext) -> bool:
        return ctx["category"] == "doc" and ctx["method"] == "smartpage_create"

    async def before_call(self, ctx: CallContext) -> dict[str, Any] | None:
        pages = ctx["args"].get("pages")
        if not isinstance(pages, list) or not pages:
            return None

        has_filepath = any(
            isinstance(p, dict) and isinstance(p.get("page_filepath"), str) and p["page_filepath"]
            for p in pages
        )
        if not has_filepath:
            return None

        return await _resolve_pages(ctx, pages)


async def _resolve_pages(ctx: CallContext, pages: list[Any]) -> dict[str, Any]:
    total_size = 0

    for i, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        filepath = page.get("page_filepath")
        if not isinstance(filepath, str) or not filepath:
            continue
        if not await asyncio.to_thread(os.path.isfile, filepath):
            raise FileNotFoundError(
                f"smartpage_create: pages[{i}] file not found: {filepath}"
            )
        size = await asyncio.to_thread(os.path.getsize, filepath)
        if size > MAX_SINGLE_FILE_SIZE:
            raise ValueError(
                f"smartpage_create: pages[{i}] file '{filepath}' "
                f"size {size / 1024 / 1024:.1f}MB exceeds single file limit 10MB"
            )
        total_size += size
        if total_size > MAX_TOTAL_FILE_SIZE:
            raise ValueError(
                f"smartpage_create: total file size {total_size / 1024 / 1024:.1f}MB "
                f"exceeds total limit 20MB (at pages[{i}] '{filepath}')"
            )

    resolved_pages: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        if not isinstance(page, dict):
            resolved_pages.append(page)
            continue
        filepath = page.get("page_filepath")
        if not isinstance(filepath, str) or not filepath:
            resolved_pages.append(page)
            continue

        try:
            content = await asyncio.to_thread(_read_file_utf8, filepath)
        except Exception as exc:
            raise RuntimeError(
                f"smartpage_create: pages[{i}] failed to read '{filepath}': {exc}"
            ) from exc

        new_page = dict(page)
        del new_page["page_filepath"]
        new_page["page_content"] = content
        resolved_pages.append(new_page)

    logger.info(
        "smartpage_create: resolved %d pages, total_size=%d bytes",
        len(resolved_pages), total_size,
    )
    return {"args": {**ctx["args"], "pages": resolved_pages}}


def _read_file_utf8(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()
