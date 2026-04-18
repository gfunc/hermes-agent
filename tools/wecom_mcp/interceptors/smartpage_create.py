import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .types import CallContext

logger = logging.getLogger(__name__)

MAX_SINGLE_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024   # 20MB


def _get_allowed_root() -> Path:
    """Return the allowed filesystem root for smartpage_create uploads.

    Falls back to current working directory.  Override via
    ``WECOM_MCP_UPLOAD_ROOT`` env var.
    """
    env_root = os.getenv("WECOM_MCP_UPLOAD_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return Path(os.getcwd()).resolve()


def _validate_filepath(raw_path: str) -> str:
    """Resolve and validate that *raw_path* stays inside the allowed root.

    Raises ``ValueError`` on traversal attempts or paths outside the root.
    Returns the resolved absolute path.
    """
    allowed_root = _get_allowed_root()
    # Reject obvious traversal attempts before resolution
    if ".." in raw_path:
        raise ValueError(
            f"smartpage_create: path contains '..' (traversal attempt): {raw_path}"
        )
    resolved = Path(raw_path).resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(
            f"smartpage_create: path '{raw_path}' resolves to '{resolved}' "
            f"which is outside allowed root '{allowed_root}'"
        ) from exc
    return str(resolved)


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
        raw_path = page.get("page_filepath")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        filepath = _validate_filepath(raw_path)
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
        raw_path = page.get("page_filepath")
        if not isinstance(raw_path, str) or not raw_path:
            resolved_pages.append(page)
            continue

        filepath = _validate_filepath(raw_path)
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
