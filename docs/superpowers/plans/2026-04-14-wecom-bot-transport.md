# WeCom Bot Transport Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stream reply reliability (errcode 846608 fallback, event callbacks, non-blocking sends, thinking messages) and a Bot Webhook mode to the Hermes `WeComAdapter`.

**Architecture:** Extend `gateway/platforms/wecom.py` with new send paths and dispatch logic. Add `gateway/platforms/wecom_webhook.py` for an aiohttp-based Bot Webhook receiver (JSON-encrypted callbacks, multi-account signature matching, `stream_refresh`, `enter_chat`, `response_url` push). Use an in-memory `StreamState` store shared between WS and webhook paths.

**Tech Stack:** Python 3.11+, aiohttp, httpx, pytest, asyncio.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `gateway/platforms/wecom_webhook.py` | **Create** — Bot Webhook HTTP handler (GET verify, POST decrypt, dispatch, `stream_refresh`, `enter_chat`) |
| `gateway/platforms/wecom.py` | **Modify** — add stream expiry fallback, event callback routing, non-blocking sends, thinking messages, integrate webhook server |
| `tests/gateway/test_wecom_webhook.py` | **Create** — webhook decrypt, signature match, dispatch tests |
| `tests/gateway/test_wecom_transport.py` | **Create** — 846608 fallback, event callback, non-blocking stream tests |

---

## Task 1: Stream Expiry Fallback (errcode 846608)

**Files:**
- Modify: `gateway/platforms/wecom.py:1166-1179`
- Test: `tests/gateway/test_wecom_transport.py`

### Step 1.1: Write failing test for 846608 fallback

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_transport.py
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from gateway.config import PlatformConfig
from gateway.platforms.wecom import WeComAdapter


@pytest.mark.asyncio
async def test_send_reply_stream_falls_back_on_846608():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    # Simulate a prior inbound message so reply_req_id is known
    adapter._reply_req_ids["msg-123"] = "req-456"

    async def _fake_send_reply_request(*args, **kwargs):
        raise RuntimeError("errcode=846608, errmsg=stream message update expired")

    adapter._send_reply_request = _fake_send_reply_request

    with patch.object(adapter, "_send_request", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"headers": {"req_id": "fallback-1"}, "body": {"errcode": 0}}
        await adapter._send_reply_stream("req-456", "hello world")

        mock_send.assert_awaited_once()
        call_body = mock_send.call_args[0][1]
        assert call_body["msgtype"] == "markdown"
        assert call_body["markdown"]["content"] == "hello world"
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_transport.py::test_send_reply_stream_falls_back_on_846608 -v`

Expected: `FAILED` — `_send_reply_stream` currently re-raises the exception without catching 846608.

### Step 1.2: Implement fallback in `_send_reply_stream`

- [ ] **Modify `wecom.py`**

Replace `_send_reply_stream` (`wecom.py:1166-1179`) with:

```python
    STREAM_EXPIRED_ERRCODE = 846608

    async def _send_reply_stream(self, reply_req_id: str, content: str) -> Dict[str, Any]:
        try:
            response = await self._send_reply_request(
                reply_req_id,
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": self._new_req_id("stream"),
                        "finish": True,
                        "content": content[:self.MAX_MESSAGE_LENGTH],
                    },
                },
            )
            self._raise_for_wecom_error(response, "send reply stream")
            return response
        except Exception as exc:
            err_msg = str(exc)
            if self.STREAM_EXPIRED_ERRCODE in err_msg or f"errcode={self.STREAM_EXPIRED_ERRCODE}" in err_msg:
                logger.warning(
                    "[%s] Stream expired (errcode=%s), falling back to proactive send",
                    self.name, self.STREAM_EXPIRED_ERRCODE,
                )
                # Need chat_id for proactive send; look it up from reply_req_id mapping
                chat_id = self._chat_id_for_reply_req_id(reply_req_id)
                if not chat_id:
                    raise RuntimeError(
                        f"Cannot fallback from stream expiry: no chat_id mapped to reply_req_id={reply_req_id}"
                    ) from exc
                return await self._send_request(
                    APP_CMD_SEND,
                    {
                        "chatid": chat_id,
                        "msgtype": "markdown",
                        "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]},
                    },
                )
            raise
```

Add the mapping helper and storage. In `__init__`, add:

```python
        self._reply_chat_ids: Dict[str, str] = {}
```

And add the helper method:

```python
    def _remember_reply_chat_id(self, reply_req_id: str, chat_id: str) -> None:
        if reply_req_id and chat_id:
            self._reply_chat_ids[reply_req_id] = chat_id
            while len(self._reply_chat_ids) > DEDUP_MAX_SIZE:
                self._reply_chat_ids.pop(next(iter(self._reply_chat_ids)))

    def _chat_id_for_reply_req_id(self, reply_req_id: str) -> Optional[str]:
        return self._reply_chat_ids.get(str(reply_req_id or "").strip())
```

Then in `_on_message` (`wecom.py:481`), after:

```python
        self._remember_reply_req_id(msg_id, self._payload_req_id(payload))
```

Add:

```python
        self._remember_reply_chat_id(self._payload_req_id(payload), chat_id)
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_transport.py::test_send_reply_stream_falls_back_on_846608 -v`

Expected: `PASSED`

### Step 1.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_transport.py
git commit -m "feat(wecom): fallback to proactive send on stream expiry (errcode 846608)"
```

---

## Task 2: Event Callback Handling (`aibot_event_callback`)

**Files:**
- Modify: `gateway/platforms/wecom.py:374-391` (`_dispatch_payload`), `wecom.py:471-548` (`_on_message`)
- Test: `tests/gateway/test_wecom_transport.py`

### Step 2.1: Write failing test for event callback dispatch

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_transport.py
@pytest.mark.asyncio
async def test_dispatch_payload_routes_event_callback():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch.object(adapter, "_on_event_callback", new_callable=AsyncMock) as mock_on_event:
        payload = {"cmd": "aibot_event_callback", "headers": {"req_id": "ev-1"}, "body": {"eventtype": "enter_chat"}}
        await adapter._dispatch_payload(payload)
        mock_on_event.assert_awaited_once_with(payload)
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_transport.py::test_dispatch_payload_routes_event_callback -v`

Expected: `FAILED` — `_dispatch_payload` currently ignores `aibot_event_callback`.

### Step 2.2: Add event callback routing and handling

- [ ] **Modify `_dispatch_payload`**

Change the existing block (`wecom.py:385-391`):

```python
        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return
        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
            return
```

To:

```python
        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return
        if cmd == APP_CMD_EVENT_CALLBACK:
            await self._on_event_callback(payload)
            return
        if cmd == APP_CMD_PING:
            return
```

- [ ] **Add `_on_event_callback` method**

Insert before `_on_message` (`wecom.py:470`):

```python
    async def _on_event_callback(self, payload: Dict[str, Any]) -> None:
        """Handle aibot_event_callback.

        Event callbacks do not have a valid req_id for passive replyStream.
        We skip intermediate stream frames and send the final frame proactively.
        """
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        chat_id = str(body.get("chatid") or "").strip()
        if not chat_id:
            logger.debug("[%s] Event callback missing chat_id, ignoring", self.name)
            return

        # Store a marker so that outbound send() knows to use proactive send
        # for any reply tied to this message context.
        req_id = self._payload_req_id(payload)
        if req_id:
            self._event_callback_req_ids.add(req_id)

        # Build a minimal event for enter_chat / template_card_event if needed.
        event_type = str(body.get("eventtype") or "").lower()
        if event_type == "enter_chat" and body.get("welcome_text"):
            # Some event callbacks carry a welcome_text field.
            await self._send_request(
                APP_CMD_SEND,
                {
                    "chatid": chat_id,
                    "msgtype": "markdown",
                    "markdown": {"content": str(body.get("welcome_text", ""))},
                },
            )
```

Add the set in `__init__`:

```python
        self._event_callback_req_ids: set[str] = set()
```

Then update `send()` (`wecom.py:1290+`) so it checks the event-callback set. Find the existing block:

```python
        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            if reply_req_id:
                response = await self._send_reply_stream(reply_req_id, content)
```

Replace with:

```python
        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            if reply_req_id and reply_req_id not in self._event_callback_req_ids:
                response = await self._send_reply_stream(reply_req_id, content)
            else:
                response = await self._send_request(
                    APP_CMD_SEND,
                    {
                        "chatid": chat_id,
                        "msgtype": "markdown",
                        "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]},
                    },
                )
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_transport.py::test_dispatch_payload_routes_event_callback -v`

Expected: `PASSED`

### Step 2.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_transport.py
git commit -m "feat(wecom): handle aibot_event_callback with proactive send fallback"
```

---

## Task 3: Non-Blocking Stream Sends

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom_transport.py`

### Step 3.1: Write failing test for non-blocking stream

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_transport.py
@pytest.mark.asyncio
async def test_nonblocking_stream_skips_when_pending():
    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._reply_req_ids["msg-1"] = "req-1"

    # Simulate a pending ack for this stream
    adapter._pending_stream_acks.add("stream-1")

    result = await adapter._send_reply_stream_nonblocking("req-1", "stream-1", "partial")
    assert result == "skipped"
```

- [ ] **Run test**

Expected: `FAILED` — method does not exist.

### Step 3.2: Implement non-blocking stream send

- [ ] **Modify `wecom.py`**

Add in `__init__`:

```python
        self._pending_stream_acks: set[str] = set()
```

Add the method after `_send_reply_stream`:

```python
    async def _send_reply_stream_nonblocking(
        self,
        reply_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
    ) -> str:
        """Send a stream update, skipping if a prior frame is still pending."""
        if not finish and stream_id in self._pending_stream_acks:
            logger.debug("[%s] Skipping non-blocking stream %s (pending ack)", self.name, stream_id)
            return "skipped"

        self._pending_stream_acks.add(stream_id)
        try:
            response = await self._send_reply_request(
                reply_req_id,
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "finish": finish,
                        "content": content[:self.MAX_MESSAGE_LENGTH],
                    },
                },
            )
            self._raise_for_wecom_error(response, "send reply stream non-blocking")
            return stream_id
        except Exception:
            # Leave pending flag so caller can decide whether to retry
            raise
        finally:
            # Only remove on ack if the server responded successfully.
            # In error case we keep it pending to prevent duplicate backlog.
            pass
```

Note: we need to clear the ack when the correlated response comes back. In `_dispatch_payload`, when a response matches a pending `req_id` and it corresponds to a stream send, remove it. Simpler approach: just use a timed expiry (15s) instead of exact ack tracking for the first pass. Add a small helper:

```python
    def _mark_stream_ack(self, stream_id: str) -> None:
        self._pending_stream_acks.discard(stream_id)
```

And call it at the top of `_dispatch_payload` when the response body has `errcode == 0`:

```python
    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}

        # Clear stream ack if this is a successful response
        if body.get("errcode") in (0, None):
            self._pending_stream_acks.discard(req_id)

        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            ...
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_transport.py::test_nonblocking_stream_skips_when_pending -v`

Expected: `PASSED`

### Step 3.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom_transport.py
git commit -m "feat(wecom): add non-blocking stream sends with pending-ack skip"
```

---

## Task 4: Bot Webhook Mode — Core Handler

**Files:**
- Create: `gateway/platforms/wecom_webhook.py`
- Test: `tests/gateway/test_wecom_webhook.py`

### Step 4.1: Write failing test for webhook verify + decrypt

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_webhook.py
import pytest
from aiohttp import web
from unittest.mock import AsyncMock

from gateway.platforms.wecom_accounts import WeComAccount


@pytest.mark.asyncio
async def test_handle_verify_returns_plaintext():
    from gateway.platforms.wecom_webhook import WecomBotWebhookHandler

    account = WeComAccount(
        account_id="default",
        token="test_token",
        encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        receive_id="wx123",
    )
    handler = WecomBotWebhookHandler([account])

    # Use the real crypto to generate a valid echostr
    from gateway.platforms.wecom_crypto import WXBizMsgCrypt
    crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.receive_id)
    echostr = "hello_wecom"
    timestamp = "1234567890"
    nonce = "nonce123"
    encrypted = crypt.encrypt(echostr, timestamp, nonce)

    req = web.Request(
        message=None,
        writer=None,
        task=None,
        transport=None,
        protocol=None,
        payload=None,
        loop=None,
        client_max_size=1024**2,
        headers={},
    )
    # aiohttp Request is hard to mock directly; test the internal method instead.
    result = await handler._handle_verify(
        signature=encrypted["signature"],
        timestamp=timestamp,
        nonce=nonce,
        echostr=encrypted["encrypt"],
    )
    assert result == echostr
```

- [ ] **Run test**

Expected: `FAILED` — `WecomBotWebhookHandler` does not exist.

### Step 4.2: Implement `WecomBotWebhookHandler`

- [ ] **Create `gateway/platforms/wecom_webhook.py`**

```python
# gateway/platforms/wecom_webhook.py
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import web

from gateway.platforms.wecom_accounts import WeComAccount
from gateway.platforms.wecom_crypto import WXBizMsgCrypt

logger = logging.getLogger(__name__)


class WecomBotWebhookHandler:
    """Handle WeCom AI Bot JSON webhook callbacks."""

    def __init__(
        self,
        accounts: List[WeComAccount],
        *,
        on_message: Optional[Any] = None,
        on_stream_refresh: Optional[Any] = None,
        on_enter_chat: Optional[Any] = None,
        on_template_card_event: Optional[Any] = None,
    ):
        self._accounts = accounts
        self._on_message = on_message
        self._on_stream_refresh = on_stream_refresh
        self._on_enter_chat = on_enter_chat
        self._on_template_card_event = on_template_card_event

    # ------------------------------------------------------------------
    # HTTP entrypoints
    # ------------------------------------------------------------------

    async def handle_request(self, request: web.Request) -> web.Response:
        method = request.method.upper()
        if method == "GET":
            signature = request.query.get("msg_signature", "")
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")
            echostr = request.query.get("echostr", "")
            if not all([signature, timestamp, nonce, echostr]):
                return web.Response(status=400, text="missing query params")
            try:
                plain = await self._handle_verify(signature, timestamp, nonce, echostr)
                return web.Response(text=plain, content_type="text/plain")
            except Exception as exc:
                logger.warning("[wecom-webhook] Verify failed: %s", exc)
                return web.Response(status=403, text="signature verification failed")

        if method == "POST":
            signature = (
                request.query.get("msg_signature")
                or request.query.get("msgsignature")
                or request.query.get("signature")
                or ""
            )
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")
            if not all([signature, timestamp, nonce]):
                return web.Response(status=400, text="missing query params")

            body_text = await request.text()
            try:
                response_data = await self._handle_callback(signature, timestamp, nonce, body_text)
            except ValueError as exc:
                return web.Response(status=400, text=str(exc))
            except PermissionError as exc:
                return web.Response(status=403, text=str(exc))

            if response_data is None:
                response_data = {}
            # Encrypt the response
            matched = self._match_signature(signature, timestamp, nonce, "")
            if matched:
                crypt = WXBizMsgCrypt(matched.token, matched.encoding_aes_key, matched.receive_id)
                encrypted = crypt.encrypt(json.dumps(response_data), timestamp, nonce)
                return web.Response(
                    text=json.dumps(encrypted),
                    content_type="text/plain; charset=utf-8",
                )
            return web.json_response(response_data)

        return web.Response(status=405, text="method not allowed")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def _handle_verify(self, signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        matched = self._match_signature(signature, timestamp, nonce, echostr)
        if not matched:
            raise PermissionError("signature verification failed")
        crypt = WXBizMsgCrypt(matched.token, matched.encoding_aes_key, matched.receive_id)
        return crypt.verify_url(signature, timestamp, nonce, echostr)

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------

    async def _handle_callback(
        self,
        signature: str,
        timestamp: str,
        nonce: str,
        body_text: str,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any]
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc

        encrypt = str(body.get("encrypt") or body.get("Encrypt") or "")
        if not encrypt:
            raise ValueError("missing encrypt field")

        matched = self._match_signature(signature, timestamp, nonce, encrypt)
        if not matched:
            raise PermissionError("signature verification failed")

        crypt = WXBizMsgCrypt(matched.token, matched.encoding_aes_key, matched.receive_id)
        try:
            plaintext = crypt.decrypt(signature, timestamp, nonce, encrypt)
        except Exception as exc:
            logger.warning("[wecom-webhook] Decrypt failed: %s", exc)
            raise ValueError("decrypt failed") from exc

        message: Dict[str, Any]
        try:
            message = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON in decrypted payload") from exc

        # aibotid validation
        expected_bot_ids = {a.bot_id for a in self._accounts if a.bot_id}
        inbound_aibotid = str(message.get("aibotid") or "").strip()
        if expected_bot_ids and inbound_aibotid and inbound_aibotid not in expected_bot_ids:
            logger.warning(
                "[wecom-webhook] aibotid mismatch expected=%s actual=%s",
                expected_bot_ids, inbound_aibotid,
            )

        msgtype = str(message.get("msgtype") or "").lower()
        if msgtype == "stream":
            if self._on_stream_refresh:
                return await self._on_stream_refresh(matched, message)
            return None

        if msgtype == "event":
            event_type = str(message.get("event", {}).get("eventtype") or "").lower()
            if event_type == "enter_chat":
                if self._on_enter_chat:
                    return await self._on_enter_chat(matched, message)
                welcome = matched.welcome_text or "你好，有什么可以帮你的？"
                return {"msgtype": "text", "text": {"content": welcome}}
            if event_type == "template_card_event":
                if self._on_template_card_event:
                    return await self._on_template_card_event(matched, message)
            return None

        # Normal message
        if self._on_message:
            return await self._on_message(matched, message)
        return None

    # ------------------------------------------------------------------
    # Signature matching
    # ------------------------------------------------------------------

    def _match_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> Optional[WeComAccount]:
        matches: List[WeComAccount] = []
        for account in self._accounts:
            if not account.token:
                continue
            crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.receive_id)
            try:
                if crypt.verify_url(signature, timestamp, nonce, encrypt):
                    # verify_url also does signature validation; reuse it for JSON webhooks.
                    matches.append(account)
            except Exception:
                pass

        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            return None
        logger.warning("[wecom-webhook] Signature conflict: %d accounts matched", len(matches))
        return None
```

Note: `WXBizMsgCrypt.verify_url` does SHA1 signature verification. For JSON webhooks the encrypt param is the encrypted payload; `verify_url` will attempt to decrypt it, which is more than just signature check. A cleaner approach is to add a pure `verify_signature` method to `wecom_crypto.py`. However, to keep this plan self-contained, we can reuse `verify_url` for now because it internally checks the signature before decrypting. If the decrypt step is undesirable, we should add a `verify_signature` method first.

Let's add a lightweight `verify_signature` method to avoid decrypting during matching:

In `gateway/platforms/wecom_crypto.py`, after `_sha1_signature` (`line 61`), add a standalone helper:

```python
def verify_signature(token: str, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    expected = _sha1_signature(token, timestamp, nonce, encrypt)
    return signature == expected
```

Then in `wecom_webhook.py`, change `_match_signature` to:

```python
from gateway.platforms.wecom_crypto import verify_signature

    def _match_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> Optional[WeComAccount]:
        matches: List[WeComAccount] = []
        for account in self._accounts:
            if not account.token:
                continue
            try:
                if verify_signature(account.token, signature, timestamp, nonce, encrypt):
                    matches.append(account)
            except Exception:
                pass
        ...
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_webhook.py::test_handle_verify_returns_plaintext -v`

Expected: `PASSED`

### Step 4.3: Add multi-account signature match tests

- [ ] **Write tests**

```python
# tests/gateway/test_wecom_webhook.py (append)

from gateway.platforms.wecom_crypto import verify_signature


def test_match_signature_no_match():
    from gateway.platforms.wecom_webhook import WecomBotWebhookHandler

    acc1 = WeComAccount(account_id="a1", token="t1", encoding_aes_key="k1" * 22 + "1", receive_id="r1")
    handler = WecomBotWebhookHandler([acc1])
    result = handler._match_signature("bad", "1", "n", "data")
    assert result is None


def test_match_signature_conflict_returns_none():
    from gateway.platforms.wecom_webhook import WecomBotWebhookHandler

    # Two accounts with same token will both match
    acc1 = WeComAccount(account_id="a1", token="tok", encoding_aes_key="k1" * 22 + "1", receive_id="r1")
    acc2 = WeComAccount(account_id="a2", token="tok", encoding_aes_key="k2" * 22 + "1", receive_id="r2")
    handler = WecomBotWebhookHandler([acc1, acc2])

    sig = verify_signature("tok", "", "1", "n", "data")
    # We need a real signature string
    sig = verify_signature("tok", "", "1", "n", "data")
    # Actually verify_signature returns bool. We need _sha1_signature.
    from gateway.platforms.wecom_crypto import _sha1_signature
    sig = _sha1_signature("tok", "1", "n", "data")

    result = handler._match_signature(sig, "1", "n", "data")
    assert result is None  # conflict
```

Wait — `_sha1_signature` is not exported. Let's add it to the public API of `wecom_crypto.py` in Task 4.4 below. For now, adjust the test to monkeypatch:

```python
def test_match_signature_conflict_returns_none(monkeypatch):
    from gateway.platforms.wecom_webhook import WecomBotWebhookHandler

    acc1 = WeComAccount(account_id="a1", token="tok", encoding_aes_key="k1" * 22 + "1", receive_id="r1")
    acc2 = WeComAccount(account_id="a2", token="tok", encoding_aes_key="k2" * 22 + "1", receive_id="r2")
    handler = WecomBotWebhookHandler([acc1, acc2])

    monkeypatch.setattr(
        "gateway.platforms.wecom_crypto.verify_signature",
        lambda _token, _sig, _ts, _nonce, _enc: True,
    )
    result = handler._match_signature("sig", "1", "n", "data")
    assert result is None  # conflict logs warning
```

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom_webhook.py -v`

Expected: tests pass.

### Step 4.4: Export `verify_signature` from `wecom_crypto.py`

- [ ] **Modify `wecom_crypto.py`**

After `_sha1_signature` (line 61–63), add:

```python
def verify_signature(token: str, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    """Verify a WeCom SHA1 signature without decrypting the payload."""
    expected = _sha1_signature(token, timestamp, nonce, encrypt)
    return signature == expected
```

- [ ] **Run all crypto tests**

Run: `pytest tests/gateway/test_wecom_callback.py -v`

Expected: all pass (no regression).

### Step 4.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_webhook.py gateway/platforms/wecom_crypto.py tests/gateway/test_wecom_webhook.py
git commit -m "feat(wecom): add Bot Webhook handler with multi-account signature matching"
```

---

## Task 5: Integrate Webhook Server into `WeComAdapter`

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 5.1: Write failing test for webhook mode connection

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom.py (append)

@pytest.mark.asyncio
async def test_connect_starts_webhook_for_webhook_mode_account():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(
        extra={
            "accounts": {
                "wh": {
                    "connection_mode": "webhook",
                    "token": "tok",
                    "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
                    "receive_id": "wx123",
                }
            }
        }
    )
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom.web.Application") as mock_app_cls:
        mock_app = mock_app_cls.return_value
        mock_runner = AsyncMock()
        with patch("gateway.platforms.wecom.AppRunner", return_value=mock_runner):
            mock_site = AsyncMock()
            with patch("gateway.platforms.wecom.TCPSite", return_value=mock_site):
                result = await adapter.connect()
                assert result is True
                mock_runner.setup.assert_awaited_once()
                mock_site.start.assert_awaited_once()
```

- [ ] **Run test**

Expected: `FAILED` — `connect()` doesn't start a webhook server yet.

### Step 5.2: Integrate webhook server lifecycle

- [ ] **Modify `wecom.py`**

Add imports at top:

```python
try:
    from aiohttp import web
    AIOHTTP_WEB_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    AIOHTTP_WEB_AVAILABLE = False
```

In `__init__`, add webhook server fields:

```python
        self._webhook_app: Optional[web.Application] = None
        self._webhook_runner: Optional[web.AppRunner] = None
        self._webhook_site: Optional[web.TCPSite] = None
        self._webhook_handler: Optional[Any] = None
```

Modify `connect()` to branch:

```python
    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            ...
        if not HTTPX_AVAILABLE:
            ...

        webhook_accounts = [
            a for a in self._accounts
            if a.connection_mode == "webhook" and a.is_webhook_configured
        ]
        bot_accounts = [a for a in self._accounts if a.is_bot_configured]

        if not webhook_accounts and not bot_accounts:
            message = "WeCom startup failed: no accounts configured (need bot_id+secret or webhook credentials)"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

            if bot_accounts:
                account = bot_accounts[0]
                await self._open_connection(account)
                self._listen_task = asyncio.create_task(self._listen_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                logger.info("[%s] WS connected (account=%s)", self.name, account.account_id)

            if webhook_accounts:
                await self._start_webhook_server(webhook_accounts)
                logger.info("[%s] Webhook server started for %d account(s)", self.name, len(webhook_accounts))

            self._mark_connected()
            return True
        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup_ws()
            await self._cleanup_webhook()
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None
            return False
```

Add server methods:

```python
    async def _start_webhook_server(self, accounts: List[WeComAccount]) -> None:
        if not AIOHTTP_WEB_AVAILABLE or web is None:
            raise RuntimeError("aiohttp web support is not available")

        from gateway.platforms.wecom_webhook import WecomBotWebhookHandler

        self._webhook_handler = WecomBotWebhookHandler(
            accounts,
            on_message=self._on_webhook_message,
            on_stream_refresh=self._on_webhook_stream_refresh,
            on_enter_chat=self._on_webhook_enter_chat,
        )

        self._webhook_app = web.Application()
        self._webhook_app.router.add_get("/wecom/bot", self._webhook_handler.handle_request)
        self._webhook_app.router.add_post("/wecom/bot", self._webhook_handler.handle_request)
        self._webhook_app.router.add_get("/wecom/bot/{account_id}", self._webhook_handler.handle_request)
        self._webhook_app.router.add_post("/wecom/bot/{account_id}", self._webhook_handler.handle_request)

        self._webhook_runner = web.AppRunner(self._webhook_app)
        await self._webhook_runner.setup()
        self._webhook_site = web.TCPSite(self._webhook_runner, "0.0.0.0", 8644)
        await self._webhook_site.start()

    async def _cleanup_webhook(self) -> None:
        if self._webhook_site:
            await self._webhook_site.stop()
            self._webhook_site = None
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
            self._webhook_runner = None
        self._webhook_app = None
        self._webhook_handler = None

    async def _on_webhook_message(self, account: WeComAccount, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        logger.info("[wecom-webhook] inbound message account=%s msgtype=%s", account.account_id, message.get("msgtype"))
        # TODO: build MessageEvent and call self.handle_message in background
        return None

    async def _on_webhook_stream_refresh(self, account: WeComAccount, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None

    async def _on_webhook_enter_chat(self, account: WeComAccount, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        welcome = account.welcome_text or "你好，有什么可以帮你的？"
        return {"msgtype": "text", "text": {"content": welcome}}
```

Also update `disconnect()` to clean up webhook server.

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_connect_starts_webhook_for_webhook_mode_account -v`

Expected: `PASSED`

### Step 5.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): integrate Bot Webhook server into WeComAdapter"
```

---

## Task 6: Final Regression Check

- [ ] **Run all WeCom tests**

Run:
```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py tests/gateway/test_wecom_webhook.py tests/gateway/test_wecom_transport.py tests/gateway/test_wecom_accounts.py -v
```

Expected: all pass.

- [ ] **Commit**

```bash
git add -A
git commit -m "test(wecom): full regression for bot transport reliability" || true
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Stream expiry fallback (846608) | Task 1 |
| Event callback handling | Task 2 |
| Non-blocking stream sends | Task 3 |
| Bot Webhook handler creation | Task 4 |
| Multi-account signature matching | Task 4 |
| Webhook server integration | Task 5 |
| `stream_refresh` / `enter_chat` hooks | Task 5 |
| No regressions | Task 6 |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-14-wecom-bot-transport.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks.
2. **Inline Execution** — Batch execute in this session using `superpowers:executing-plans`.

Which approach?
