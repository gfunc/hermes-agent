"""Tests for WeCom Bot Webhook mode."""

import asyncio
import json
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.wecom import WeComAdapter
from gateway.platforms.wecom.accounts import WeComAccount
from gateway.platforms.wecom.crypto import WXBizMsgCrypt


def _webhook_account(account_id="wh-acct", token="tok", aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"):
    return WeComAccount(
        account_id=account_id,
        bot_id=f"bot-{account_id}",
        token=token,
        encoding_aes_key=aes_key,
        corp_id="corp-1",
        connection_mode="webhook",
    )


def _encrypt_json(crypt: WXBizMsgCrypt, data: dict) -> dict:
    """Encrypt a JSON payload and return {encrypt, signature, timestamp, nonce}."""
    xml = crypt.encrypt(json.dumps(data), nonce="nonce123", timestamp="123456")
    root = ET.fromstring(xml)
    return {
        "encrypt": root.findtext("Encrypt", default=""),
        "signature": root.findtext("MsgSignature", default=""),
        "timestamp": root.findtext("TimeStamp", default=""),
        "nonce": root.findtext("Nonce", default=""),
    }


class TestWebhookSignatureMatch:
    def test_no_match_returns_none(self):
        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": "a", "token": "t1", "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG", "connection_mode": "webhook"}]
        }))
        result = adapter._match_webhook_account("bad-sig", "1", "n", "enc")
        assert result is None

    def test_single_match_returns_account(self):
        account = _webhook_account()
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        xml = crypt.encrypt("hello", nonce="n1", timestamp="t1")
        root = ET.fromstring(xml)
        encrypt = root.findtext("Encrypt", default="")
        sig = root.findtext("MsgSignature", default="")

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "connection_mode": "webhook"}]
        }))
        result = adapter._match_webhook_account(sig, "t1", "n1", encrypt)
        assert result is not None
        assert result.account_id == account.account_id

    def test_conflict_with_multiple_matches_returns_none(self):
        # Two accounts with same token/aes key will both match
        shared_token = "sharedtok"
        shared_aes = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        corp_id = "corp-same"
        crypt = WXBizMsgCrypt(shared_token, shared_aes, corp_id)
        xml = crypt.encrypt("hello", nonce="n1", timestamp="t1")
        root = ET.fromstring(xml)
        encrypt = root.findtext("Encrypt", default="")
        sig = root.findtext("MsgSignature", default="")

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [
                {"account_id": "a1", "token": shared_token, "encoding_aes_key": shared_aes,
                 "corp_id": corp_id, "connection_mode": "webhook"},
                {"account_id": "a2", "token": shared_token, "encoding_aes_key": shared_aes,
                 "corp_id": corp_id, "connection_mode": "webhook"},
            ]
        }))
        result = adapter._match_webhook_account(sig, "t1", "n1", encrypt)
        assert result is None


class TestWebhookVerify:
    @pytest.mark.asyncio
    async def test_verify_returns_echostr_for_valid_signature(self):
        account = _webhook_account()
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        xml = crypt.encrypt("test-echostr", nonce="n1", timestamp="t1")
        root = ET.fromstring(xml)
        encrypt = root.findtext("Encrypt", default="")
        sig = root.findtext("MsgSignature", default="")

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "connection_mode": "webhook"}]
        }))

        class FakeRequest:
            query = {
                "msg_signature": sig,
                "timestamp": "t1",
                "nonce": "n1",
                "echostr": encrypt,
            }

        response = await adapter._handle_webhook_verify(FakeRequest())
        assert response.text == "test-echostr"

    @pytest.mark.asyncio
    async def test_verify_rejects_invalid_signature(self):
        adapter = WeComAdapter(PlatformConfig(enabled=True))

        class FakeRequest:
            query = {
                "msg_signature": "bad-sig",
                "timestamp": "1",
                "nonce": "n",
                "echostr": "enc",
            }

        response = await adapter._handle_webhook_verify(FakeRequest())
        assert response.status == 403


class TestWebhookCallback:
    @pytest.mark.asyncio
    async def test_callback_dispatches_text_message(self):
        account = _webhook_account()
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        payload = {"aibotid": account.bot_id, "msgtype": "text", "text": {"content": "hello"},
                   "chatid": "c1", "from": {"userid": "u1"}, "msgid": "m1"}
        enc = _encrypt_json(crypt, payload)

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "bot_id": account.bot_id, "connection_mode": "webhook"}]
        }))
        adapter._on_message = AsyncMock()

        class FakeRequest:
            query = {"msg_signature": enc["signature"], "timestamp": enc["timestamp"], "nonce": enc["nonce"]}

            async def json(self):
                return {"encrypt": enc["encrypt"]}

        response = await adapter._handle_webhook_callback(FakeRequest())
        assert response.status == 200
        adapter._on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_rejects_aibotid_mismatch(self):
        account = _webhook_account()
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        payload = {"aibotid": "wrong-bot", "msgtype": "text", "text": {"content": "hello"}}
        enc = _encrypt_json(crypt, payload)

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "bot_id": account.bot_id, "connection_mode": "webhook"}]
        }))

        class FakeRequest:
            query = {"msg_signature": enc["signature"], "timestamp": enc["timestamp"], "nonce": enc["nonce"]}

            async def json(self):
                return {"encrypt": enc["encrypt"]}

        response = await adapter._handle_webhook_callback(FakeRequest())
        assert response.status == 403

    @pytest.mark.asyncio
    async def test_stream_refresh_returns_state(self):
        account = _webhook_account()
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "bot_id": account.bot_id, "connection_mode": "webhook"}]
        }))
        # Initialize stream store and create a stream
        from gateway.platforms.wecom.stream_store import StreamStore
        adapter._stream_store = StreamStore(flush_handler=lambda p: None)
        stream_id = adapter._stream_store.create_stream()
        stream = adapter._stream_store.get_stream(stream_id)
        stream.content = "partial"
        stream.finished = True

        payload = {"msgtype": "stream_refresh", "stream_id": stream_id}
        enc = _encrypt_json(crypt, payload)

        class FakeRequest:
            query = {"msg_signature": enc["signature"], "timestamp": enc["timestamp"], "nonce": enc["nonce"]}

            async def json(self):
                return {"encrypt": enc["encrypt"]}

        response = await adapter._handle_webhook_callback(FakeRequest())
        assert response.status == 200
        body = json.loads(response.text)
        assert body["msgtype"] == "stream"
        assert body["stream"]["finish"] is True
        assert body["stream"]["content"] == "partial"

    @pytest.mark.asyncio
    async def test_enter_chat_returns_welcome_text(self):
        account = _webhook_account()
        account = WeComAccount(
            account_id=account.account_id,
            bot_id=account.bot_id,
            token=account.token,
            encoding_aes_key=account.encoding_aes_key,
            corp_id=account.corp_id,
            connection_mode="webhook",
            welcome_text="Welcome!",
        )
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        payload = {"msgtype": "enter_chat"}
        enc = _encrypt_json(crypt, payload)

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{"account_id": account.account_id, "token": account.token,
                          "encoding_aes_key": account.encoding_aes_key, "corp_id": account.corp_id,
                          "welcome_text": "Welcome!", "connection_mode": "webhook"}]
        }))

        class FakeRequest:
            query = {"msg_signature": enc["signature"], "timestamp": enc["timestamp"], "nonce": enc["nonce"]}

            async def json(self):
                return {"encrypt": enc["encrypt"]}

        response = await adapter._handle_webhook_callback(FakeRequest())
        assert response.status == 200
        body = json.loads(response.text)
        assert body["msgtype"] == "markdown"
        assert body["markdown"]["content"] == "Welcome!"

    @pytest.mark.asyncio
    async def test_webhook_callback_blocks_unauthorized_command(self):
        account = _webhook_account()
        account = WeComAccount(
            account_id=account.account_id,
            bot_id=account.bot_id,
            token=account.token,
            encoding_aes_key=account.encoding_aes_key,
            corp_id=account.corp_id,
            connection_mode="webhook",
            dm_policy="allowlist",
            allow_from=["alice"],
        )
        crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.corp_id)
        payload = {
            "aibotid": account.bot_id,
            "msgtype": "text",
            "text": {"content": "/reset"},
            "chatid": "c1",
            "from": {"userid": "bob"},
            "msgid": "m1",
        }
        enc = _encrypt_json(crypt, payload)

        adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
            "accounts": [{
                "account_id": account.account_id,
                "token": account.token,
                "encoding_aes_key": account.encoding_aes_key,
                "corp_id": account.corp_id,
                "bot_id": account.bot_id,
                "connection_mode": "webhook",
                "dm_policy": "allowlist",
                "allow_from": ["alice"],
            }]
        }))
        adapter._on_message = AsyncMock()

        class FakeRequest:
            query = {"msg_signature": enc["signature"], "timestamp": enc["timestamp"], "nonce": enc["nonce"]}
            async def json(self):
                return {"encrypt": enc["encrypt"]}

        response = await adapter._handle_webhook_callback(FakeRequest())
        assert response.status == 200
        # Command should be rejected; on_message should NOT be called for agent dispatch
        adapter._on_message.assert_not_awaited()
        body = json.loads(response.text)
        assert body["msgtype"] == "text"
        assert "没有权限" in body["text"]["content"]



@pytest.mark.asyncio
async def test_stream_refresh_near_timeout_triggers_fallback():
    import time
    from gateway.platforms.wecom.stream_store import StreamStore

    adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
        "accounts": [{"account_id": "a1", "bot_id": "b1", "token": "t", "encoding_aes_key": "k", "corp_id": "c1", "connection_mode": "webhook"}]
    }))
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    store = StreamStore(flush_handler=adapter._on_webhook_flush)
    adapter._stream_store = store
    stream_id = store.create_stream()
    store.mark_started(stream_id)
    stream = store.get_stream(stream_id)
    stream.started_at = time.time() - 310
    stream.content = "near timeout result"
    stream.finished = True

    from gateway.platforms.wecom.accounts import WeComAccount
    account = WeComAccount(account_id="a1", bot_id="b1")
    response = await adapter._handle_stream_refresh(account, "c1", stream_id)
    assert response.status == 200
    body = json.loads(response.text)
    assert body["stream"]["content"] == "near timeout result"
    adapter._send_request.assert_awaited_once()
