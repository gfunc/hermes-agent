"""Unit tests for tools/wecom_mcp/interceptors/.

Tests each interceptor's match logic, business logic, and pipeline dispatch.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tools.wecom_mcp.interceptors import resolve_before_call, run_after_call
from tools.wecom_mcp.interceptors.biz_error import BizErrorInterceptor, _check_biz_error
from tools.wecom_mcp.interceptors.msg_media import MediaInterceptor, _detect_mime, _intercept_media
from tools.wecom_mcp.interceptors.smartpage_create import SmartpageCreateInterceptor, _resolve_pages
from tools.wecom_mcp.interceptors.smartpage_export import SmartpageExportInterceptor, _intercept_export
from tools.wecom_mcp.interceptors.types import CallContext
from tools.wecom_mcp_tool import handle_wecom_mcp


# ------------------------------------------------------------------
# BizErrorInterceptor
# ------------------------------------------------------------------

class TestBizErrorInterceptor:
    def test_match_all_calls(self):
        interceptor = BizErrorInterceptor()
        assert interceptor.match(CallContext(category="contact", method="get_userlist", args={})) is True
        assert interceptor.match(CallContext(category="doc", method="smartpage_create", args={})) is True

    def test_after_call_no_error(self):
        interceptor = BizErrorInterceptor()
        result = {"content": [{"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}]}
        out = interceptor.after_call(CallContext(category="contact", method="get_userlist", args={}), result)
        assert out is result

    def test_after_call_detects_errcode_850002(self, monkeypatch):
        """errcode 850002 should trigger cache clear."""
        clear_calls: list = []
        monkeypatch.setattr(
            "tools.wecom_mcp.interceptors.biz_error.clear_category_cache",
            lambda cat: clear_calls.append(cat),
        )

        interceptor = BizErrorInterceptor()
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 850002, "errmsg": "token expired"})}
            ]
        }
        out = interceptor.after_call(CallContext(category="contact", method="get_userlist", args={}), result)
        assert out is result
        assert clear_calls == ["contact"]

    def test_after_call_ignores_other_errcodes(self, monkeypatch):
        clear_calls: list = []
        monkeypatch.setattr(
            "tools.wecom_mcp.interceptors.biz_error.clear_category_cache",
            lambda cat: clear_calls.append(cat),
        )

        interceptor = BizErrorInterceptor()
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 60001, "errmsg": "no permission"})}
            ]
        }
        out = interceptor.after_call(CallContext(category="contact", method="get_userlist", args={}), result)
        assert out is result
        assert clear_calls == []

    def test_after_call_non_dict_result(self):
        interceptor = BizErrorInterceptor()
        out = interceptor.after_call(CallContext(category="contact", method="get_userlist", args={}), "plain text")
        assert out == "plain text"

    def test_after_call_no_text_content(self):
        interceptor = BizErrorInterceptor()
        result = {"content": [{"type": "image", "url": "http://example.com/img.png"}]}
        out = interceptor.after_call(CallContext(category="contact", method="get_userlist", args={}), result)
        assert out is result


# ------------------------------------------------------------------
# MediaInterceptor
# ------------------------------------------------------------------

class TestMediaInterceptor:
    def test_match_only_get_msg_media(self):
        interceptor = MediaInterceptor()
        assert interceptor.match(CallContext(category="msg", method="get_msg_media", args={})) is True
        assert interceptor.match(CallContext(category="msg", method="get_msg_chat_list", args={})) is False
        assert interceptor.match(CallContext(category="contact", method="get_msg_media", args={})) is False

    def test_before_call_returns_timeout(self):
        interceptor = MediaInterceptor()
        result = interceptor.before_call(CallContext(category="msg", method="get_msg_media", args={}))
        assert result == {"timeout_ms": 120_000}

    @pytest.mark.asyncio
    async def test_after_call_decodes_base64_image(self, monkeypatch, tmp_path):
        img_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"  # JPEG magic
        b64 = base64.b64encode(img_bytes).decode()

        saved_paths: list = []

        def fake_cache_image(data, ext=".jpg"):
            path = str(tmp_path / f"cached{ext}")
            Path(path).write_bytes(data)
            saved_paths.append(path)
            return path

        monkeypatch.setattr(
            "tools.wecom_mcp.interceptors.msg_media.cache_image_from_bytes",
            fake_cache_image,
        )

        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "errcode": 0,
                        "errmsg": "ok",
                        "media_item": {
                            "media_id": "mid-1",
                            "name": "photo.jpg",
                            "type": "image",
                            "base64_data": b64,
                        },
                    }),
                }
            ]
        }

        out = await _intercept_media(result)
        assert out is not result
        text = out["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["media_item"]["local_path"] in saved_paths
        assert parsed["media_item"]["content_type"] == "image/jpeg"
        assert parsed["media_item"]["size"] == len(img_bytes)

    @pytest.mark.asyncio
    async def test_after_call_decodes_base64_document(self, monkeypatch, tmp_path):
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n"
        b64 = base64.b64encode(pdf_bytes).decode()

        saved_paths: list = []

        def fake_cache_document(data, filename="file.bin"):
            path = str(tmp_path / filename)
            Path(path).write_bytes(data)
            saved_paths.append(path)
            return path

        monkeypatch.setattr(
            "tools.wecom_mcp.interceptors.msg_media.cache_document_from_bytes",
            fake_cache_document,
        )

        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "errcode": 0,
                        "errmsg": "ok",
                        "media_item": {
                            "media_id": "mid-2",
                            "name": "report.pdf",
                            "type": "file",
                            "base64_data": b64,
                        },
                    }),
                }
            ]
        }

        out = await _intercept_media(result)
        text = out["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["media_item"]["content_type"] == "application/pdf"
        assert parsed["media_item"]["local_path"].endswith("report.pdf")

    @pytest.mark.asyncio
    async def test_after_call_skips_nonzero_errcode(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 60001, "errmsg": "fail"})}
            ]
        }
        out = await _intercept_media(result)
        assert out is result

    @pytest.mark.asyncio
    async def test_after_call_skips_missing_media_item(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok"})}
            ]
        }
        out = await _intercept_media(result)
        assert out is result

    @pytest.mark.asyncio
    async def test_after_call_skips_invalid_base64(self):
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "errcode": 0,
                        "errmsg": "ok",
                        "media_item": {
                            "media_id": "mid-1",
                            "base64_data": "!!!not-valid-base64!!!",
                        },
                    }),
                }
            ]
        }
        out = await _intercept_media(result)
        assert out is result


# ------------------------------------------------------------------
# _detect_mime
# ------------------------------------------------------------------

class TestDetectMime:
    def test_jpeg(self):
        assert _detect_mime(b"\xff\xd8", None) == "image/jpeg"

    def test_png(self):
        assert _detect_mime(b"\x89PNG\r\n\x1a\n", None) == "image/png"

    def test_gif(self):
        assert _detect_mime(b"GIF89a", None) == "image/gif"

    def test_webp(self):
        assert _detect_mime(b"RIFF\x00\x00\x00\x00WEBP", None) == "image/webp"

    def test_amr(self):
        assert _detect_mime(b"#AMR", None) == "audio/amr"

    def test_pdf(self):
        assert _detect_mime(b"%PDF-1.4", None) == "application/pdf"

    def test_fallback_filename(self):
        assert _detect_mime(b"unknown", "photo.jpg") == "image/jpeg"

    def test_fallback_octet_stream(self):
        assert _detect_mime(b"unknown", None) == "application/octet-stream"


# ------------------------------------------------------------------
# SmartpageCreateInterceptor
# ------------------------------------------------------------------

class TestSmartpageCreateInterceptor:
    def test_match(self):
        interceptor = SmartpageCreateInterceptor()
        assert interceptor.match(CallContext(category="doc", method="smartpage_create", args={})) is True
        assert interceptor.match(CallContext(category="doc", method="smartpage_get", args={})) is False
        assert interceptor.match(CallContext(category="contact", method="smartpage_create", args={})) is False

    @pytest.mark.asyncio
    async def test_before_call_no_pages(self):
        interceptor = SmartpageCreateInterceptor()
        result = await interceptor.before_call(CallContext(category="doc", method="smartpage_create", args={}))
        assert result is None

    @pytest.mark.asyncio
    async def test_before_call_no_filepath(self):
        interceptor = SmartpageCreateInterceptor()
        result = await interceptor.before_call(
            CallContext(category="doc", method="smartpage_create", args={"pages": [{"title": "p1"}]})
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_before_call_resolves_pages(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        f1 = tmp_path / "page1.md"
        f1.write_text("# Page 1")
        f2 = tmp_path / "page2.md"
        f2.write_text("## Page 2")

        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [{"title": "p1", "page_filepath": str(f1)}, {"title": "p2", "page_filepath": str(f2)}]},
        )
        result = await interceptor.before_call(ctx)
        assert result is not None
        resolved = result["args"]["pages"]
        assert resolved[0]["page_content"] == "# Page 1"
        assert "page_filepath" not in resolved[0]
        assert resolved[1]["page_content"] == "## Page 2"
        assert "page_filepath" not in resolved[1]

    @pytest.mark.asyncio
    async def test_before_call_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [{"title": "p1", "page_filepath": str(tmp_path / "missing.md")}]},
        )
        with pytest.raises(FileNotFoundError):
            await interceptor.before_call(ctx)

    @pytest.mark.asyncio
    async def test_before_call_single_file_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        big = tmp_path / "big.md"
        big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [{"title": "p1", "page_filepath": str(big)}]},
        )
        with pytest.raises(ValueError, match="exceeds single file limit"):
            await interceptor.before_call(ctx)

    @pytest.mark.asyncio
    async def test_before_call_total_size_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        f1 = tmp_path / "a.md"
        f1.write_bytes(b"x" * (7 * 1024 * 1024))
        f2 = tmp_path / "b.md"
        f2.write_bytes(b"x" * (7 * 1024 * 1024))
        f3 = tmp_path / "c.md"
        f3.write_bytes(b"x" * (7 * 1024 * 1024))

        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [
                {"title": "p1", "page_filepath": str(f1)},
                {"title": "p2", "page_filepath": str(f2)},
                {"title": "p3", "page_filepath": str(f3)},
            ]},
        )
        with pytest.raises(ValueError, match="exceeds total limit"):
            await interceptor.before_call(ctx)

    @pytest.mark.asyncio
    async def test_before_call_rejects_traversal_dotdot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [{"title": "p1", "page_filepath": "../outside.md"}]},
        )
        with pytest.raises(ValueError, match="contains '..'"):
            await interceptor.before_call(ctx)

    @pytest.mark.asyncio
    async def test_before_call_rejects_path_outside_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
        interceptor = SmartpageCreateInterceptor()
        ctx = CallContext(
            category="doc",
            method="smartpage_create",
            args={"pages": [{"title": "p1", "page_filepath": "/etc/passwd"}]},
        )
        with pytest.raises(ValueError, match="outside allowed root"):
            await interceptor.before_call(ctx)


# ------------------------------------------------------------------
# SmartpageExportInterceptor
# ------------------------------------------------------------------

class TestSmartpageExportInterceptor:
    def test_match(self):
        interceptor = SmartpageExportInterceptor()
        assert interceptor.match(
            CallContext(category="doc", method="smartpage_get_export_result", args={})
        ) is True
        assert interceptor.match(
            CallContext(category="doc", method="smartpage_create", args={})
        ) is False

    @pytest.mark.asyncio
    async def test_after_call_saves_markdown(self, monkeypatch, tmp_path):
        saved_paths: list = []

        def fake_cache_document(data, filename="file.bin"):
            path = str(tmp_path / filename)
            Path(path).write_bytes(data)
            saved_paths.append(path)
            return path

        monkeypatch.setattr(
            "tools.wecom_mcp.interceptors.smartpage_export.cache_document_from_bytes",
            fake_cache_document,
        )

        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "errcode": 0,
                        "errmsg": "ok",
                        "task_done": True,
                        "content": "# Hello\n\nWorld",
                    }),
                }
            ]
        }

        out = await _intercept_export(result)
        text = out["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["content_path"] in saved_paths
        assert "content" not in parsed

    @pytest.mark.asyncio
    async def test_after_call_skips_not_done(self):
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "errcode": 0,
                        "errmsg": "ok",
                        "task_done": False,
                        "content": "",
                    }),
                }
            ]
        }
        out = await _intercept_export(result)
        assert out is result

    @pytest.mark.asyncio
    async def test_after_call_skips_nonzero_errcode(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 60001, "errmsg": "fail"})}
            ]
        }
        out = await _intercept_export(result)
        assert out is result

    @pytest.mark.asyncio
    async def test_after_call_skips_missing_content(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"errcode": 0, "errmsg": "ok", "task_done": True})}
            ]
        }
        out = await _intercept_export(result)
        assert out is result


# ------------------------------------------------------------------
# Pipeline dispatch
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_before_call_merges_timeout_and_args():
    """Multiple interceptors contribute timeout_ms (max) and args (last wins)."""
    ctx = CallContext(category="msg", method="get_msg_media", args={"x": 1})
    result = await resolve_before_call(ctx)
    assert result["timeout_ms"] == 120_000  # MediaInterceptor
    assert result["args"] is None  # no other interceptor matched


@pytest.mark.asyncio
async def test_resolve_before_call_no_match():
    ctx = CallContext(category="contact", method="get_userlist", args={})
    result = await resolve_before_call(ctx)
    assert result == {"timeout_ms": 30_000, "args": None}


@pytest.mark.asyncio
async def test_resolve_before_call_smartpage_create_replaces_args(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_MCP_UPLOAD_ROOT", str(tmp_path))
    f1 = tmp_path / "p1.md"
    f1.write_text("hello")

    ctx = CallContext(
        category="doc",
        method="smartpage_create",
        args={"pages": [{"title": "t", "page_filepath": str(f1)}]},
    )
    result = await resolve_before_call(ctx)
    assert result["args"]["pages"][0]["page_content"] == "hello"
    assert "page_filepath" not in result["args"]["pages"][0]


@pytest.mark.asyncio
async def test_run_after_call_pipes_through_interceptors(monkeypatch):
    """BizErrorInterceptor should clear cache on errcode 850002."""
    clear_calls: list = []
    monkeypatch.setattr(
        "tools.wecom_mcp.interceptors.biz_error.clear_category_cache",
        lambda cat: clear_calls.append(cat),
    )

    ctx = CallContext(category="contact", method="get_userlist", args={})
    result = {
        "content": [
            {"type": "text", "text": json.dumps({"errcode": 850002, "errmsg": "expired"})}
        ]
    }
    out = await run_after_call(ctx, result)
    assert out is result
    assert clear_calls == ["contact"]


@pytest.mark.asyncio
async def test_run_after_call_no_match_returns_unchanged():
    ctx = CallContext(category="contact", method="unknown_method", args={})
    result = {"data": 42}
    out = await run_after_call(ctx, result)
    assert out == result


# ------------------------------------------------------------------
# handle_wecom_mcp kwargs regression
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_wecom_mcp_accepts_kwargs():
    """Registry.dispatch passes **kwargs including task_id — handler must accept them."""
    result = await handle_wecom_mcp(
        action="list",
        category="contact",
        task_id="test-task",
        some_other_kwarg="ignored",
    )
    assert isinstance(result, str)
