"""SmartSheet upload interceptor.

Scans smartsheet_add_records / smartsheet_update_records args for
image_path / file_path entries, uploads them via doc MCP, and replaces
with image_url / file_id.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from tools.wecom_mcp.transport import send_json_rpc
from .types import CallContext

logger = logging.getLogger(__name__)

MAX_SINGLE_FILE_SIZE = 10 * 1024 * 1024
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024
UPLOAD_TIMEOUT_MS = 60_000
INTERCEPTOR_TIMEOUT_MS = 120_000


def _get_allowed_root() -> Path:
    """Return the allowed filesystem root for smartsheet file uploads."""
    env_root = os.getenv("WECOM_MCP_UPLOAD_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return Path(os.getcwd()).resolve()


def _resolve_upload_path(raw_path: str) -> str:
    """Resolve and validate that *raw_path* stays inside the allowed root.

    Raises ``ValueError`` on traversal attempts or paths outside the root.
    Returns the resolved absolute path.
    """
    allowed_root = _get_allowed_root()
    if ".." in raw_path:
        raise ValueError(
            f"smartsheet_upload: path contains '..' (traversal attempt): {raw_path}"
        )
    resolved = Path(raw_path).resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError:
        raise ValueError(
            f"smartsheet_upload: path '{raw_path}' resolves to '{resolved}' "
            f"which is outside allowed root '{allowed_root}'"
        )
    return str(resolved)


class SmartsheetUploadInterceptor:
    name = "smartsheet-upload"

    def match(self, ctx: CallContext) -> bool:
        return (
            ctx["category"] == "doc"
            and ctx["method"] in ("smartsheet_add_records", "smartsheet_update_records")
        )

    async def before_call(self, ctx: CallContext) -> dict[str, Any] | None:
        args = ctx.get("args", {})
        records = args.get("records")
        if not isinstance(records, list) or not records:
            return None

        tasks = _collect_upload_tasks(records)
        if not tasks:
            return None

        logger.info("[wecom_mcp] smartsheet-upload: %d files to upload", len(tasks))
        _validate_file_sizes(tasks)

        has_image = any(t["kind"] == "image" for t in tasks)
        doc_locator = _extract_doc_locator(args) if has_image else {}

        await _execute_uploads(tasks, doc_locator)

        return {"args": {**args, "records": records}, "timeout_ms": INTERCEPTOR_TIMEOUT_MS}


def _collect_upload_tasks(records: list[dict]) -> list[dict]:
    tasks = []
    for record in records:
        values = record.get("values")
        if not isinstance(values, dict):
            continue
        for field_value in values.values():
            if not isinstance(field_value, list):
                continue
            for cell in field_value:
                if not isinstance(cell, dict):
                    continue
                if isinstance(cell.get("image_path"), str) and cell["image_path"]:
                    tasks.append({
                        "kind": "image",
                        "file_path": cell["image_path"],
                        "title": cell.get("title") if isinstance(cell.get("title"), str) else None,
                        "cell": cell,
                    })
                elif isinstance(cell.get("file_path"), str) and cell["file_path"]:
                    tasks.append({
                        "kind": "file",
                        "file_path": cell["file_path"],
                        "cell": cell,
                    })
    return tasks


def _validate_file_sizes(tasks: list[dict]) -> None:
    total = 0
    for task in tasks:
        resolved = _resolve_upload_path(task["file_path"])
        path = Path(resolved)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Smartsheet upload: file not found: {path}")
        size = path.stat().st_size
        if size > MAX_SINGLE_FILE_SIZE:
            raise ValueError(
                f"Smartsheet upload: file {path.name} ({size / 1024 / 1024:.1f}MB) "
                f"exceeds single file limit 10MB"
            )
        total += size
        if total > MAX_TOTAL_FILE_SIZE:
            raise ValueError(
                f"Smartsheet upload: total size ({total / 1024 / 1024:.1f}MB) "
                f"exceeds limit 20MB"
            )
        task["resolved_path"] = resolved


def _extract_doc_locator(args: dict) -> dict:
    if isinstance(args.get("docid"), str) and args["docid"]:
        return {"docid": args["docid"]}
    if isinstance(args.get("url"), str) and args["url"]:
        return {"url": args["url"]}
    raise ValueError("Smartsheet upload: args missing docid or url for image upload")


async def _execute_uploads(tasks: list[dict], doc_locator: dict) -> None:
    for task in tasks:
        path = Path(task["resolved_path"])
        data = path.read_bytes()
        base64_content = base64.b64encode(data).decode("ascii")
        filename = path.name

        if task["kind"] == "image":
            result = await send_json_rpc(
                "doc",
                "tools/call",
                {
                    "name": "upload_doc_image",
                    "arguments": {**doc_locator, "base64_content": base64_content},
                },
                timeout_ms=UPLOAD_TIMEOUT_MS,
            )
            image_url = _extract_biz_field(result, "url")
            if not image_url:
                raise RuntimeError(f"upload_doc_image returned no url for {filename}")
            cell = task["cell"]
            cell["image_url"] = image_url
            cell["title"] = task.get("title") or filename
            cell.pop("image_path", None)
            logger.info("[wecom_mcp] smartsheet-upload: image %s -> %s", filename, image_url)
        else:
            result = await send_json_rpc(
                "doc",
                "tools/call",
                {
                    "name": "upload_doc_file",
                    "arguments": {
                        "file_name": filename,
                        "file_base64_content": base64_content,
                    },
                },
                timeout_ms=UPLOAD_TIMEOUT_MS,
            )
            file_id = _extract_biz_field(result, "fileid")
            if not file_id:
                raise RuntimeError(f"upload_doc_file returned no fileid for {filename}")
            cell = task["cell"]
            cell["file_id"] = file_id
            cell.pop("file_path", None)
            logger.info("[wecom_mcp] smartsheet-upload: file %s -> %s", filename, file_id)


def _extract_biz_field(result: Any, field: str) -> str | None:
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
            if isinstance(parsed, dict) and parsed.get("errcode") == 0:
                return parsed.get(field)
        except (json.JSONDecodeError, TypeError):
            continue
    return None
