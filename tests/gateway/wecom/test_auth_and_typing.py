"""Tests for auth-check before queue dispatch and send_typing fallback."""

import pytest
from unittest.mock import AsyncMock


class TestOnSdkMessageAuth:
    @pytest.mark.asyncio
    async def test_auth_check_drops_unauthorized_command(self):
        """Unauthorized commands must be rejected BEFORE _on_message runs."""
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={
            "bot_id": "b",
            "secret": "s",
            "dm_policy": "allowlist",
            "allow_from": ["alice"],
        })
        adapter = WeComAdapter(config)
        adapter._on_message = AsyncMock()
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
        await adapter._on_sdk_message(payload)

        adapter._on_message.assert_not_awaited()
        adapter._send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auth_check_allows_authorized_command(self):
        """Authorized commands must pass through to _on_message."""
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={
            "bot_id": "b",
            "secret": "s",
            "dm_policy": "allowlist",
            "allow_from": ["alice"],
        })
        adapter = WeComAdapter(config)
        adapter._on_message = AsyncMock()
        adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {
                "msgid": "m1",
                "msgtype": "text",
                "text": {"content": "/reset"},
                "from": {"userid": "alice"},
                "chatid": "c1",
            },
        }
        await adapter._on_sdk_message(payload)

        adapter._on_message.assert_awaited_once_with(payload)
        adapter._send_request.assert_not_awaited()


class TestSendTypingFallback:
    @pytest.mark.asyncio
    async def test_send_typing_fallback_when_no_req_id(self):
        """When no reply_req_id exists, send_typing must attempt proactive fallback."""
        from gateway.config import PlatformConfig
        from gateway.platforms.wecom import WeComAdapter

        config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
        adapter = WeComAdapter(config)
        adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

        await adapter.send_typing("chat-1", metadata=None)

        adapter._send_request.assert_awaited_once()
        call_args = adapter._send_request.await_args.args[1]
        assert call_args["chatid"] == "chat-1"
        assert call_args["msgtype"] == "markdown"
        assert call_args["markdown"]["content"] == "<think></think>"
