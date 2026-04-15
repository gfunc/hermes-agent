"""Tests for the WeCom platform adapter."""

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult


class TestWeComRequirements:
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.wecom.AIOHTTP_AVAILABLE", False)
        monkeypatch.setattr("gateway.platforms.wecom.HTTPX_AVAILABLE", True)
        from gateway.platforms.wecom import check_wecom_requirements

        assert check_wecom_requirements() is False

    def test_returns_false_without_httpx(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.wecom.AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr("gateway.platforms.wecom.HTTPX_AVAILABLE", False)
        from gateway.platforms.wecom import check_wecom_requirements

        assert check_wecom_requirements() is False

    def test_returns_true_when_available(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.wecom.AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr("gateway.platforms.wecom.HTTPX_AVAILABLE", True)
        from gateway.platforms.wecom import check_wecom_requirements

        assert check_wecom_requirements() is True


class TestWeComAdapterInit:
    def test_reads_config_from_extra(self):
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(
            enabled=True,
            extra={
                "bot_id": "cfg-bot",
                "secret": "cfg-secret",
                "websocket_url": "wss://custom.wecom.example/ws",
                "group_policy": "allowlist",
                "group_allow_from": ["group-1"],
            },
        )
        adapter = WeComAdapter(config)

        assert adapter._bot_id == "cfg-bot"
        assert adapter._secret == "cfg-secret"
        assert adapter._ws_url == "wss://custom.wecom.example/ws"
        assert adapter._group_policy == "allowlist"
        assert adapter._group_allow_from == ["group-1"]

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("WECOM_BOT_ID", "env-bot")
        monkeypatch.setenv("WECOM_SECRET", "env-secret")
        monkeypatch.setenv("WECOM_WEBSOCKET_URL", "wss://env.example/ws")
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        assert adapter._bot_id == "env-bot"
        assert adapter._secret == "env-secret"
        assert adapter._ws_url == "wss://env.example/ws"


class TestWeComConnect:
    @pytest.mark.asyncio
    async def test_connect_records_missing_credentials(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)

        adapter = WeComAdapter(PlatformConfig(enabled=True))

        success = await adapter.connect()

        assert success is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_code == "wecom_missing_credentials"
        assert "WECOM_BOT_ID" in (adapter.fatal_error_message or "")

    @pytest.mark.asyncio
    async def test_connect_records_handshake_failure_details(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock(side_effect=RuntimeError("invalid secret (errcode=40013)"))

        success = await adapter.connect()

        assert success is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_code == "wecom_connect_error"
        assert "invalid secret" in (adapter.fatal_error_message or "")

    @pytest.mark.asyncio
    async def test_connect_discovers_mcp_configs_after_websocket_success(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock()

        async def fake_send_request(cmd, body, timeout=0):
            category = body.get("category")
            return {"errcode": 0, "body": {"url": f"https://mcp.example/{category}"}}

        adapter._send_request = AsyncMock(side_effect=fake_send_request)

        success = await adapter.connect()

        assert success is True
        assert adapter.get_mcp_configs() == {
            "contact": "https://mcp.example/contact",
            "meeting": "https://mcp.example/meeting",
            "todo": "https://mcp.example/todo",
            "schedule": "https://mcp.example/schedule",
            "doc": "https://mcp.example/doc",
            "msg": "https://mcp.example/msg",
        }
        # Verify _send_request was called for each category with mcp_get_config
        assert adapter._send_request.await_count == 6
        calls = adapter._send_request.await_args_list
        categories = [c.args[1]["category"] for c in calls]
        assert categories == ["contact", "meeting", "todo", "schedule", "doc", "msg"]
        assert all(c.args[0] == "mcp_get_config" for c in calls)

    @pytest.mark.asyncio
    async def test_connect_mcp_config_failure_is_non_fatal(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock()

        async def fake_send_request(cmd, body, timeout=0):
            if body.get("category") == "contact":
                return {"errcode": 0, "body": {"url": "https://mcp.example/contact"}}
            return {"errcode": 50001, "errmsg": "internal error"}

        adapter._send_request = AsyncMock(side_effect=fake_send_request)

        success = await adapter.connect()

        assert success is True
        assert adapter.get_mcp_configs() == {"contact": "https://mcp.example/contact"}

    @pytest.mark.asyncio
    async def test_connect_mcp_config_missing_url_is_skipped(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock()

        async def fake_send_request(cmd, body, timeout=0):
            if body.get("category") == "contact":
                return {"errcode": 0, "body": {"url": "https://mcp.example/contact"}}
            # Missing url field
            return {"errcode": 0, "body": {}}

        adapter._send_request = AsyncMock(side_effect=fake_send_request)

        success = await adapter.connect()

        assert success is True
        assert adapter.get_mcp_configs() == {"contact": "https://mcp.example/contact"}

    @pytest.mark.asyncio
    async def test_connect_mcp_config_exception_is_non_fatal(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock()
        adapter._send_request = AsyncMock(side_effect=RuntimeError("websocket timeout"))

        success = await adapter.connect()

        assert success is True
        assert adapter.get_mcp_configs() == {}


class TestWeComReplyMode:
    @pytest.mark.asyncio
    async def test_send_uses_passive_reply_stream_when_reply_context_exists(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._send_reply_request = AsyncMock(
            return_value={"headers": {"req_id": "req-1"}, "errcode": 0}
        )

        result = await adapter.send("chat-123", "hello from reply", reply_to="msg-1")

        assert result.success is True
        adapter._send_reply_request.assert_awaited_once()
        args = adapter._send_reply_request.await_args.args
        assert args[0] == "req-1"
        assert args[1]["msgtype"] == "stream"
        assert args[1]["stream"]["finish"] is True
        assert args[1]["stream"]["content"] == "hello from reply"

    @pytest.mark.asyncio
    async def test_send_image_file_uses_passive_reply_media_when_reply_context_exists(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._prepare_outbound_media = AsyncMock(
            return_value={
                "data": b"image-bytes",
                "content_type": "image/png",
                "file_name": "demo.png",
                "detected_type": "image",
                "final_type": "image",
                "rejected": False,
                "reject_reason": None,
                "downgraded": False,
                "downgrade_note": None,
            }
        )
        adapter._upload_media_bytes = AsyncMock(return_value={"media_id": "media-1", "type": "image"})
        adapter._send_reply_request = AsyncMock(
            return_value={"headers": {"req_id": "req-1"}, "errcode": 0}
        )

        result = await adapter.send_image_file("chat-123", "/tmp/demo.png", reply_to="msg-1")

        assert result.success is True
        adapter._send_reply_request.assert_awaited_once()
        args = adapter._send_reply_request.await_args.args
        assert args[0] == "req-1"
        assert args[1] == {"msgtype": "image", "image": {"media_id": "media-1"}}


class TestExtractText:
    def test_extracts_plain_text(self):
        from gateway.platforms.wecom import WeComAdapter

        body = {
            "msgtype": "text",
            "text": {"content": "  hello world  "},
        }
        text, reply_text = WeComAdapter._extract_text(body)
        assert text == "hello world"
        assert reply_text is None

    def test_extracts_mixed_text(self):
        from gateway.platforms.wecom import WeComAdapter

        body = {
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "part1"}},
                    {"msgtype": "image", "image": {"url": "https://example.com/x.png"}},
                    {"msgtype": "text", "text": {"content": "part2"}},
                ]
            },
        }
        text, _reply_text = WeComAdapter._extract_text(body)
        assert text == "part1\npart2"

    def test_extracts_voice_and_quote(self):
        from gateway.platforms.wecom import WeComAdapter

        body = {
            "msgtype": "voice",
            "voice": {"content": "spoken text"},
            "quote": {"msgtype": "text", "text": {"content": "quoted"}},
        }
        text, reply_text = WeComAdapter._extract_text(body)
        assert text == "spoken text"
        assert reply_text == "quoted"


class TestCallbackDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["aibot_msg_callback", "aibot_callback"])
    async def test_dispatch_accepts_new_and_legacy_callback_cmds(self, cmd):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()

        await adapter._dispatch_payload({"cmd": cmd, "headers": {"req_id": "req-1"}, "body": {}})

        adapter._on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_routes_event_callback(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()

        await adapter._dispatch_payload(
            {"cmd": "aibot_event_callback", "headers": {"req_id": "req-event"}, "body": {"msgtype": "event"}}
        )

        adapter._on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_callback_does_not_store_reply_req_id(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter.handle_message = AsyncMock()

        payload = {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "req-event"},
            "body": {
                "msgid": "msg-event",
                "msgtype": "event",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
            },
        }

        await adapter._on_message(payload)

        assert adapter._reply_req_ids.get("msg-event") is None


class TestPolicyHelpers:
    def test_dm_allowlist(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"dm_policy": "allowlist", "allow_from": ["user-1"]})
        )
        allowed, _ = adapter._is_dm_allowed("user-1")
        assert allowed is True
        allowed, _ = adapter._is_dm_allowed("user-2")
        assert allowed is False

    def test_group_allowlist_and_per_group_sender_allowlist(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "group_policy": "allowlist",
                    "group_allow_from": ["group-1"],
                    "groups": {"group-1": {"allow_from": ["user-1"]}},
                },
            )
        )

        assert adapter._is_group_allowed("group-1", "user-1") is True
        assert adapter._is_group_allowed("group-1", "user-2") is False
        assert adapter._is_group_allowed("group-2", "user-1") is False


class TestMediaHelpers:
    def test_detect_wecom_media_type(self):
        from gateway.platforms.wecom import WeComAdapter

        assert WeComAdapter._detect_wecom_media_type("image/png") == "image"
        assert WeComAdapter._detect_wecom_media_type("video/mp4") == "video"
        assert WeComAdapter._detect_wecom_media_type("audio/amr") == "voice"
        assert WeComAdapter._detect_wecom_media_type("application/pdf") == "file"

    def test_detect_mime_from_bytes_png(self):
        from gateway.platforms.wecom import WeComAdapter

        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        assert WeComAdapter._detect_mime_from_bytes(png_header) == "image/png"

    def test_detect_mime_from_bytes_jpeg(self):
        from gateway.platforms.wecom import WeComAdapter

        assert WeComAdapter._detect_mime_from_bytes(b"\xff\xd8\xff") == "image/jpeg"

    def test_detect_mime_from_bytes_pdf(self):
        from gateway.platforms.wecom import WeComAdapter

        assert WeComAdapter._detect_mime_from_bytes(b"%PDF-1.4") == "application/pdf"

    def test_detect_mime_from_bytes_mp4(self):
        from gateway.platforms.wecom import WeComAdapter

        mp4_header = b"\x00\x00\x00\x20ftypisom"
        assert WeComAdapter._detect_mime_from_bytes(mp4_header) == "video/mp4"

    def test_normalize_content_type_prefers_magic_bytes_over_guess(self):
        from gateway.platforms.wecom import WeComAdapter

        # A PNG file with a .txt extension should still be detected as image/png
        result = WeComAdapter._normalize_content_type("", "fake.txt", b"\x89PNG\r\n\x1a\n")
        assert result == "image/png"

    @pytest.mark.asyncio
    async def test_load_outbound_media_enforces_local_roots(self, tmp_path):
        from gateway.platforms.wecom import WeComAdapter

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        file_path = allowed / "test.txt"
        file_path.write_text("hello")

        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked_file = blocked / "secret.txt"
        blocked_file.write_text("secret")

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={"media_local_roots": [str(allowed)]},
            )
        )

        data, ct, name = await adapter._load_outbound_media(str(file_path))
        assert name == "test.txt"

        with pytest.raises(PermissionError):
            await adapter._load_outbound_media(str(blocked_file))

    def test_voice_non_amr_downgrades_to_file(self):
        from gateway.platforms.wecom import WeComAdapter

        result = WeComAdapter._apply_file_size_limits(128, "voice", "audio/mpeg")

        assert result["final_type"] == "file"
        assert result["downgraded"] is True
        assert "AMR" in (result["downgrade_note"] or "")

    def test_oversized_file_is_rejected(self):
        from gateway.platforms.wecom import ABSOLUTE_MAX_BYTES, WeComAdapter

        result = WeComAdapter._apply_file_size_limits(ABSOLUTE_MAX_BYTES + 1, "file", "application/pdf")

        assert result["rejected"] is True
        assert "20MB" in (result["reject_reason"] or "")

    def test_decrypt_file_bytes_round_trip(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from gateway.platforms.wecom import WeComAdapter

        plaintext = b"wecom-secret"
        key = os.urandom(32)
        pad_len = 32 - (len(plaintext) % 32)
        padded = plaintext + bytes([pad_len]) * pad_len
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()

        decrypted = WeComAdapter._decrypt_file_bytes(encrypted, base64.b64encode(key).decode("ascii"))

        assert decrypted == plaintext

    @pytest.mark.asyncio
    async def test_load_outbound_media_rejects_placeholder_path(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))

        with pytest.raises(ValueError, match="placeholder was not replaced"):
            await adapter._load_outbound_media("<path>")


class TestMediaUpload:
    @pytest.mark.asyncio
    async def test_upload_media_bytes_uses_sdk_sequence(self, monkeypatch):
        import gateway.platforms.wecom as wecom_module
        from gateway.platforms.wecom import (
            APP_CMD_UPLOAD_MEDIA_CHUNK,
            APP_CMD_UPLOAD_MEDIA_FINISH,
            APP_CMD_UPLOAD_MEDIA_INIT,
            WeComAdapter,
        )

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        calls = []

        async def fake_send_request(cmd, body, timeout=0):
            calls.append((cmd, body))
            if cmd == APP_CMD_UPLOAD_MEDIA_INIT:
                return {"errcode": 0, "body": {"upload_id": "upload-1"}}
            if cmd == APP_CMD_UPLOAD_MEDIA_CHUNK:
                return {"errcode": 0}
            if cmd == APP_CMD_UPLOAD_MEDIA_FINISH:
                return {
                    "errcode": 0,
                    "body": {
                        "media_id": "media-1",
                        "type": "file",
                        "created_at": "2026-03-18T00:00:00Z",
                    },
                }
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(wecom_module, "UPLOAD_CHUNK_SIZE", 4)
        adapter._send_request = fake_send_request

        result = await adapter._upload_media_bytes(b"abcdefghij", "file", "demo.bin")

        assert result["media_id"] == "media-1"
        assert [cmd for cmd, _body in calls] == [
            APP_CMD_UPLOAD_MEDIA_INIT,
            APP_CMD_UPLOAD_MEDIA_CHUNK,
            APP_CMD_UPLOAD_MEDIA_CHUNK,
            APP_CMD_UPLOAD_MEDIA_CHUNK,
            APP_CMD_UPLOAD_MEDIA_FINISH,
        ]
        assert calls[1][1]["chunk_index"] == 0
        assert calls[2][1]["chunk_index"] == 1
        assert calls[3][1]["chunk_index"] == 2

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_download_remote_bytes_rejects_large_content_length(self, _mock_safe):
        from gateway.platforms.wecom import WeComAdapter

        class FakeResponse:
            headers = {"content-length": "10"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield b"abc"

        class FakeClient:
            def stream(self, method, url, headers=None):
                return FakeResponse()

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._http_client = FakeClient()

        with pytest.raises(ValueError, match="exceeds WeCom limit"):
            await adapter._download_remote_bytes("https://example.com/file.bin", max_bytes=4)

    @pytest.mark.asyncio
    async def test_cache_media_decrypts_url_payload_before_writing(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        plaintext = b"secret document bytes"
        key = os.urandom(32)
        pad_len = 32 - (len(plaintext) % 32)
        padded = plaintext + bytes([pad_len]) * pad_len

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        adapter._download_remote_bytes = AsyncMock(
            return_value=(
                encrypted,
                {
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="secret.bin"',
                },
            )
        )

        cached = await adapter._cache_media(
            "file",
            {
                "url": "https://example.com/secret.bin",
                "aeskey": base64.b64encode(key).decode("ascii"),
            },
        )

        assert cached is not None
        cached_path, content_type = cached
        assert Path(cached_path).read_bytes() == plaintext
        assert content_type == "application/octet-stream"


class TestSend:
    @pytest.mark.asyncio
    async def test_send_uses_proactive_payload(self):
        from gateway.platforms.wecom import APP_CMD_SEND, WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "req-1"}, "errcode": 0})

        result = await adapter.send("chat-123", "Hello WeCom")

        assert result.success is True
        adapter._send_request.assert_awaited_once_with(
            APP_CMD_SEND,
            {
                "chatid": "chat-123",
                "msgtype": "markdown",
                "markdown": {"content": "Hello WeCom"},
            },
        )

    @pytest.mark.asyncio
    async def test_send_reports_wecom_errors(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._send_request = AsyncMock(return_value={"errcode": 40001, "errmsg": "bad request"})

        result = await adapter.send("chat-123", "Hello WeCom")

        assert result.success is False
        assert "40001" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_falls_back_to_proactive_on_846608(self):
        from gateway.platforms.wecom import APP_CMD_SEND, WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._send_reply_stream = AsyncMock(
            side_effect=RuntimeError("send reply stream failed: errcode=846608")
        )
        adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "req-2"}, "errcode": 0})

        result = await adapter.send("chat-123", "stream expired", reply_to="msg-1")

        assert result.success is True
        adapter._send_reply_stream.assert_awaited_once()
        adapter._send_request.assert_awaited_once_with(
            APP_CMD_SEND,
            {
                "chatid": "chat-123",
                "msgtype": "markdown",
                "markdown": {"content": "stream expired"},
            },
        )

    @pytest.mark.asyncio
    async def test_send_image_falls_back_to_text_for_remote_url(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._send_media_source = AsyncMock(return_value=SendResult(success=False, error="upload failed"))
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="msg-1"))

        result = await adapter.send_image("chat-123", "https://example.com/demo.png", caption="demo")

        assert result.success is True
        adapter.send.assert_awaited_once_with(chat_id="chat-123", content="demo\nhttps://example.com/demo.png", reply_to=None)

    @pytest.mark.asyncio
    async def test_send_voice_sends_caption_and_downgrade_note(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._prepare_outbound_media = AsyncMock(
            return_value={
                "data": b"voice-bytes",
                "content_type": "audio/mpeg",
                "file_name": "voice.mp3",
                "detected_type": "voice",
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": "语音格式 audio/mpeg 不支持，企微仅支持 AMR 格式，已转为文件格式发送",
            }
        )
        adapter._upload_media_bytes = AsyncMock(return_value={"media_id": "media-1", "type": "file"})
        adapter._send_media_message = AsyncMock(return_value={"headers": {"req_id": "req-media"}, "errcode": 0})
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="msg-1"))

        result = await adapter.send_voice("chat-123", "/tmp/voice.mp3", caption="listen")

        assert result.success is True
        adapter._send_media_message.assert_awaited_once_with("chat-123", "file", "media-1")
        assert adapter.send.await_count == 2
        adapter.send.assert_any_await(chat_id="chat-123", content="listen", reply_to=None)
        adapter.send.assert_any_await(
            chat_id="chat-123",
            content="ℹ️ 语音格式 audio/mpeg 不支持，企微仅支持 AMR 格式，已转为文件格式发送",
            reply_to=None,
        )


class TestInboundMessages:
    @pytest.mark.asyncio
    async def test_on_message_builds_event(self):
        from gateway.platforms.wecom import WeComAdapter

        from gateway.platforms.helpers import TextBatchAggregator

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter.handle_message = AsyncMock()
        adapter._text_batcher = TextBatchAggregator(handler=adapter.handle_message, batch_delay=0)
        adapter._extract_media = AsyncMock(return_value=(["/tmp/test.png"], ["image/png"]))

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "hello"},
            },
        }

        await adapter._on_message(payload)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello"
        assert event.source.chat_id == "group-1"
        assert event.source.user_id == "user-1"
        assert event.media_urls == ["/tmp/test.png"]
        assert event.media_types == ["image/png"]

    @pytest.mark.asyncio
    async def test_on_message_preserves_quote_context(self):
        from gateway.platforms.helpers import TextBatchAggregator
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter.handle_message = AsyncMock()
        adapter._text_batcher = TextBatchAggregator(handler=adapter.handle_message, batch_delay=0)
        adapter._extract_media = AsyncMock(return_value=([], []))

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "follow up"},
                "quote": {"msgtype": "text", "text": {"content": "quoted message"}},
            },
        }

        await adapter._on_message(payload)

        event = adapter.handle_message.await_args.args[0]
        assert event.reply_to_text == "quoted message"
        assert event.reply_to_message_id == "quote:msg-1"

    @pytest.mark.asyncio
    async def test_on_message_respects_group_policy(self):
        from gateway.platforms.wecom import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={"group_policy": "allowlist", "group_allow_from": ["group-allowed"]},
            )
        )
        adapter.handle_message = AsyncMock()
        adapter._extract_media = AsyncMock(return_value=([], []))

        payload = {
            "cmd": "aibot_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "group-blocked",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "hello"},
            },
        }

        await adapter._on_message(payload)
        adapter.handle_message.assert_not_awaited()


class TestCommandAuthIntegration:
    @pytest.mark.asyncio
    async def test_on_message_blocks_unauthorized_command(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={
            "bot_id": "b",
            "secret": "s",
            "dm_policy": "allowlist",
            "allow_from": ["alice"],
        })
        adapter = WeComAdapter(config)
        adapter.handle_message = AsyncMock()
        adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {
                "msgid": "m1",
                "msgtype": "text",
                "text": {"content": "/reset"},
                "from": {"userid": "bob"},
                "chatid": "c1",
            },
        }
        await adapter._on_message(payload)
        adapter.handle_message.assert_not_awaited()


class TestNonBlockingStream:
    @pytest.mark.asyncio
    async def test_nonblocking_stream_skips_when_pending(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
        adapter = WeComAdapter(config)
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._pending_stream_acks.add("stream-1")

        result = await adapter._send_reply_stream_nonblocking("req-1", "stream-1", "partial")
        assert result == "skipped"

    @pytest.mark.asyncio
    async def test_nonblocking_stream_never_skips_finish(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
        adapter = WeComAdapter(config)
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._pending_stream_acks.add("stream-1")

        adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

        result = await adapter._send_reply_stream_nonblocking("req-1", "stream-1", "final", finish=True)
        assert result == "stream-1"
        adapter._send_reply_request.assert_awaited_once()


class TestMediaPreparerIntegration:
    @pytest.mark.asyncio
    async def test_send_image_file_uses_media_preparer(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
        adapter = WeComAdapter(config)

        with patch("gateway.platforms.wecom.MediaPreparer") as mock_prep_cls:
            mock_prep = mock_prep_cls.return_value
            mock_prep.prepare = AsyncMock(return_value={
                "data": b"fakepng",
                "content_type": "image/png",
                "file_name": "img.png",
                "detected_type": "image",
                "final_type": "image",
                "rejected": False,
                "downgraded": False,
                "downgrade_note": None,
            })
            with patch.object(adapter, "_upload_media_bytes", new_callable=AsyncMock) as mock_upload:
                mock_upload.return_value = {"media_id": "mid-1", "type": "image"}
                with patch.object(adapter, "_send_media_message", new_callable=AsyncMock) as mock_send_media:
                    mock_send_media.return_value = {"headers": {"req_id": "r1"}}
                    result = await adapter.send_image_file("chat-1", "/tmp/img.png")
                    assert result.success is True
                    mock_prep.prepare.assert_awaited_once_with("/tmp/img.png", file_name=None)


class TestCommandAuthIntegration:
    @pytest.mark.asyncio
    async def test_on_message_blocks_unauthorized_command(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={
            "bot_id": "b",
            "secret": "s",
            "dm_policy": "allowlist",
            "allow_from": ["alice"],
        })
        adapter = WeComAdapter(config)
        adapter.handle_message = AsyncMock()

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {
                "msgid": "m1",
                "msgtype": "text",
                "text": {"content": "/reset"},
                "from": {"userid": "bob"},
                "chatid": "c1",
            },
        }
        await adapter._on_message(payload)
        # Before implementation, this will be called. After adding auth, it should not.
        adapter.handle_message.assert_not_awaited()


class TestPlatformEnum:
    def test_wecom_in_platform_enum(self):
        assert Platform.WECOM.value == "wecom"


@pytest.mark.asyncio
async def test_dm_pairing_mode_blocks_and_sends_prompt():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={
        "bot_id": "b",
        "secret": "s",
        "dm_policy": "pairing",
        "allow_from": [],
    })
    adapter = WeComAdapter(config)
    adapter.handle_message = AsyncMock()
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "m1",
            "msgtype": "text",
            "text": {"content": "hello"},
            "from": {"userid": "bob"},
            "chatid": "c1",
        },
    }
    await adapter._on_message(payload)
    adapter.handle_message.assert_not_awaited()
    adapter._send_request.assert_awaited_once()
    call_args = adapter._send_request.await_args.args[1]
    assert call_args["markdown"]["content"] == "您尚未完成配对，请联系管理员进行配对后重试。"


@pytest.mark.asyncio
async def test_extract_media_extracts_video_frame():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom_video.extract_first_video_frame", return_value="/tmp/frame.jpg") as mock_extract:
        body = {
            "msgtype": "video",
            "video": {"url": "/tmp/video.mp4", "sdkfileid": "v1", "md5sum": "abc"},
        }
        urls, types = await adapter._extract_media(body)
        mock_extract.assert_called_once()
        assert "/tmp/frame.jpg" in urls
        assert "image/jpeg" in types
        assert "video/mp4" in types


@pytest.mark.asyncio
async def test_dispatch_payload_replies_to_enter_check_update():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req-1"},
        "body": {
            "msgtype": "event",
            "event": "enter_check_update",
            "chatid": "c1",
        },
    }
    await adapter._dispatch_payload(payload)
    adapter._send_request.assert_awaited_once()
    call_args = adapter._send_request.await_args.args
    assert call_args[0] == "aibot_send_msg"
    assert "version" in call_args[1]["text"]["content"].lower()


@pytest.mark.asyncio
async def test_send_thinking_placeholder():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    result = await adapter.send_thinking("chat-1")
    assert result.success is True
    adapter._send_request.assert_awaited_once()
    call_args = adapter._send_request.await_args.args[1]
    assert call_args["msgtype"] == "markdown"
    assert call_args["markdown"]["content"] == "<think></think>"


@pytest.mark.asyncio
async def test_send_typing_emits_thinking_placeholder():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})
    adapter._remember_reply_req_id("msg-typing", "req-typing")

    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})

    adapter._send_reply_request.assert_awaited_once()
    assert adapter._send_reply_request.await_args.args[0] == "req-typing"
    call_args = adapter._send_reply_request.await_args.args[1]
    assert call_args["msgtype"] == "stream"
    assert call_args["stream"]["finish"] is False
    assert call_args["stream"]["content"] == "<think></think>"


@pytest.mark.asyncio
async def test_stop_typing_clears_state_without_sending():
    """stop_typing only clears state — the response send closes the stream."""
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})
    adapter._remember_reply_req_id("msg-typing", "req-typing")

    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})
    assert adapter._send_reply_request.await_count == 1

    await adapter.stop_typing("chat-typing")
    # stop_typing should NOT send another frame
    assert adapter._send_reply_request.await_count == 1
    # State should be cleared
    assert "chat-typing" not in adapter._typing_stream_state_by_chat


@pytest.mark.asyncio
async def test_send_reply_stream_reuses_typing_stream_id():
    """_send_reply_stream should reuse the typing stream_id for the response."""
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})
    adapter._remember_reply_req_id("msg-typing", "req-typing")

    # Open typing stream
    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})
    typing_call = adapter._send_reply_request.await_args_list[0]
    typing_stream_id = typing_call.args[1]["stream"]["id"]

    # Send response reusing the typing stream_id
    await adapter._send_reply_stream("req-typing", "Hello!", chat_id="chat-typing")
    response_call = adapter._send_reply_request.await_args_list[1]
    response_body = response_call.args[1]
    assert response_body["stream"]["id"] == typing_stream_id
    assert response_body["stream"]["finish"] is True
    assert response_body["stream"]["content"] == "Hello!"
    # Typing state should be consumed
    assert "chat-typing" not in adapter._typing_stream_state_by_chat


@pytest.mark.asyncio
async def test_send_typing_without_message_id_is_noop():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    await adapter.send_typing("chat-typing", metadata=None)

    adapter._send_reply_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_typing_clears_stream_state_so_resume_reopens():
    """After pause_typing_for_chat, send_typing should open a fresh stream."""
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})
    adapter._remember_reply_req_id("msg-typing", "req-typing")

    # Initial typing opens a stream
    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})
    assert adapter._send_reply_request.await_count == 1

    # Second call is a no-op (stream already open)
    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})
    assert adapter._send_reply_request.await_count == 1

    # Pause clears the state
    adapter.pause_typing_for_chat("chat-typing")
    assert "chat-typing" not in adapter._typing_stream_state_by_chat

    # After pause, send_typing opens a fresh stream
    await adapter.send_typing("chat-typing", metadata={"message_id": "msg-typing"})
    assert adapter._send_reply_request.await_count == 2


@pytest.mark.asyncio
async def test_on_message_respects_group_disabled_policy():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    adapter = WeComAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_policy": "disabled"},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._extract_media = AsyncMock(return_value=([], []))

    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req-1"},
        "body": {
            "msgid": "msg-1",
            "chatid": "group-1",
            "chattype": "group",
            "from": {"userid": "user-1"},
            "msgtype": "text",
            "text": {"content": "hello"},
        },
    }

    await adapter._on_message(payload)
    adapter.handle_message.assert_not_awaited()


def test_chunk_markdown_splits_at_limit():
    from gateway.platforms.wecom import WeComAdapter

    long_text = "A" * 5000 + "\n\n" + "B" * 5000
    chunks = WeComAdapter._chunk_markdown_text(long_text, chunk_limit=4000)
    assert len(chunks) >= 2
    assert all(len(c) <= 4000 for c in chunks)
    assert chunks[0].startswith("A" * 100)
    assert chunks[-1].startswith("B" * 100)


@pytest.mark.asyncio
async def test_send_chunks_long_text():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    long_text = "A" * 5000 + "\n\n" + "B" * 5000
    result = await adapter.send("chat-1", long_text)
    assert result.success is True
    assert adapter._send_request.await_count >= 2


@pytest.mark.asyncio
async def test_send_extracts_and_sends_template_card():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    content = 'hello\n```json\n{"card_type":"text_notice","task_id":"t1"}\n```'
    result = await adapter.send("chat-1", content)
    assert result.success is True
    # Should send markdown text first, then template card
    assert adapter._send_request.await_count == 2
    second_call = adapter._send_request.await_args_list[-1].args[1]
    assert second_call["msgtype"] == "template_card"
    assert second_call["template_card"]["card_type"] == "text_notice"


@pytest.mark.asyncio
async def test_template_card_event_is_handled_gracefully():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter.handle_message = AsyncMock()

    payload = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "req-1"},
        "body": {
            "msgid": "m1",
            "msgtype": "event",
            "event": "template_card_event",
            "chatid": "c1",
            "from": {"userid": "u1"},
        },
    }
    await adapter._on_message(payload)
    # Should not crash and should not dispatch as normal message
    adapter.handle_message.assert_not_awaited()
