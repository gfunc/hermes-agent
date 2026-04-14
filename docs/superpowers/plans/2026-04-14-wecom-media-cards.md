# WeCom Media, Template Cards, and Agent Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add magic-byte media detection with size downgrade rules, template card send/event/update lifecycle, and full media outbound support to the Agent webhook adapter.

**Architecture:** Create `gateway/platforms/wecom_media.py` for `MediaPreparer` (magic-byte MIME detection, size limits, agent fallback). Create `gateway/platforms/wecom_template.py` for `TemplateCardCache` and card parsing/updating. Refactor `wecom.py` to use `MediaPreparer` and template card hooks. Refactor `wecom_callback.py` to support media uploads via WeCom REST API and add `agent_id` validation.

**Tech Stack:** Python 3.11+, aiohttp, httpx, pytest.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `gateway/platforms/wecom_media.py` | **Create** — `MediaPreparer` with magic-byte detection, size downgrade, local-path whitelist, agent REST fallback |
| `tests/gateway/test_wecom_media.py` | **Create** — unit tests for media prep, downgrade rules, magic bytes |
| `gateway/platforms/wecom_template.py` | **Create** — `TemplateCardCache`, card extraction from agent responses, event callback formatting |
| `tests/gateway/test_wecom_template.py` | **Create** — cache TTL, card extraction regex, event text building |
| `gateway/platforms/wecom.py` | **Modify** — integrate `MediaPreparer` into outbound media flow, add template card detection in response path |
| `gateway/platforms/wecom_callback.py` | **Modify** — add `agent_id` consistency checks, media upload + send via REST API, welcome text for `enter_agent` |
| `tests/gateway/test_wecom_callback.py` | **Extend** — media upload/send tests, agent_id mismatch tests |

---

## Task 1: Media Preparer with Magic-Byte Detection

**Files:**
- Create: `gateway/platforms/wecom_media.py`
- Test: `tests/gateway/test_wecom_media.py`

### Step 1.1: Write failing test for magic-byte detection

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_media.py
import pytest
from pathlib import Path

from gateway.platforms.wecom_media import detect_mime_from_bytes, apply_file_size_limits


def test_detect_png_from_magic_bytes():
    data = b"\x89PNG\r\n\x1a\n" + b"fake"
    assert detect_mime_from_bytes(data) == "image/png"


def test_detect_jpeg_from_magic_bytes():
    data = b"\xff\xd8\xff" + b"fake"
    assert detect_mime_from_bytes(data) == "image/jpeg"


def test_detect_pdf_from_magic_bytes():
    data = b"%PDF-1.4"
    assert detect_mime_from_bytes(data) == "application/pdf"


def test_image_downgrade_over_10mb():
    result = apply_file_size_limits(11 * 1024 * 1024, "image")
    assert result["downgraded"] is True
    assert result["final_type"] == "file"


def test_voice_rejects_non_amr():
    result = apply_file_size_limits(1024, "voice", content_type="audio/mp3")
    assert result["downgraded"] is True
    assert "AMR" in result["downgrade_note"]
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_media.py -v`

Expected: `FAILED` — `ModuleNotFoundError` for `wecom_media`.

### Step 1.2: Implement `wecom_media.py`

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_media.py
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
VOICE_SUPPORTED_MIMES = {"audio/amr"}


_MAGIC_BYTES = [
    ((b"\x89PNG\r\n\x1a\n",), "image/png"),
    ((b"\xff\xd8\xff",), "image/jpeg"),
    ((b"GIF87a", b"GIF89a"), "image/gif"),
    ((b"RIFF",), "image/webp", lambda d: len(d) >= 12 and d[8:12] == b"WEBP"),
    ((b"%PDF",), "application/pdf"),
    ((b"\x1aE\xdf\xa3",), "video/webm"),
    ((b"\x00\x00\x00 ", b"\x00\x00\x00 ftyp"), "video/mp4", lambda d: len(d) >= 12 and b"ftyp" in d[4:12]),
    ((b"ID3",), "audio/mpeg"),
    ((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), "audio/mpeg"),
    ((b"OggS",), "audio/ogg"),
    ((b"fLaC",), "audio/flac"),
    ((b"RIFF",), "audio/wav", lambda d: len(d) >= 12 and d[8:12] == b"WAVE"),
]


def detect_mime_from_bytes(data: bytes) -> Optional[str]:
    if not data:
        return None
    for entry in _MAGIC_BYTES:
        magics, mime = entry[0], entry[1]
        checker = entry[2] if len(entry) > 2 else None
        if any(data.startswith(m) for m in magics):
            if checker is None or checker(data):
                return mime
    return None


def detect_wecom_media_type(content_type: str) -> str:
    mime = str(content_type or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/") or mime == "application/ogg":
        return "voice"
    return "file"


def apply_file_size_limits(
    file_size: int,
    detected_type: str,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    file_size_mb = file_size / (1024 * 1024)
    normalized_type = str(detected_type or "file").lower()
    normalized_content_type = str(content_type or "").strip().lower()

    if file_size > ABSOLUTE_MAX_BYTES:
        return {
            "final_type": normalized_type,
            "rejected": True,
            "reject_reason": (
                f"文件大小 {file_size_mb:.2f}MB 超过了企业微信允许的最大限制 20MB，无法发送。"
                "请尝试压缩文件或减小文件大小。"
            ),
            "downgraded": False,
            "downgrade_note": None,
        }

    if normalized_type == "image" and file_size > IMAGE_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"图片大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送",
        }

    if normalized_type == "video" and file_size > VIDEO_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"视频大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送",
        }

    if normalized_type == "voice":
        if normalized_content_type and normalized_content_type not in VOICE_SUPPORTED_MIMES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": (
                    f"语音格式 {normalized_content_type} 不支持，企微仅支持 AMR 格式，已转为文件格式发送"
                ),
            }
        if file_size > VOICE_MAX_BYTES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": f"语音大小 {file_size_mb:.2f}MB 超过 2MB 限制，已转为文件格式发送",
            }

    return {
        "final_type": normalized_type,
        "rejected": False,
        "reject_reason": None,
        "downgraded": False,
        "downgrade_note": None,
    }


class MediaPreparer:
    """Prepare outbound media for WeCom with magic-byte detection and size enforcement."""

    def __init__(
        self,
        http_client,
        *,
        media_local_roots: Optional[List[str]] = None,
    ):
        self._http_client = http_client
        self._media_local_roots = [Path(r).expanduser().resolve() for r in (media_local_roots or [])]

    async def prepare(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        source = str(media_source or "").strip()
        if not source:
            raise ValueError("media source is required")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            data, headers = await self._download_remote_bytes(source)
            resolved_name = file_name or self._guess_filename(source, headers.get("content-disposition"), headers.get("content-type", ""))
            content_type = self._normalize_content_type(headers.get("content-type", ""), resolved_name, data)
        elif parsed.scheme == "file":
            local_path = Path(__import__("urllib.parse").parse.unquote(parsed.path)).expanduser().resolve()
            data, resolved_name, content_type = self._read_local_file(local_path, file_name)
        else:
            local_path = Path(source).expanduser().resolve()
            data, resolved_name, content_type = self._read_local_file(local_path, file_name)

        detected_type = detect_wecom_media_type(content_type)
        size_check = apply_file_size_limits(len(data), detected_type, content_type)

        return {
            "data": data,
            "content_type": content_type,
            "file_name": resolved_name,
            "detected_type": detected_type,
            **size_check,
        }

    async def _download_remote_bytes(self, url: str) -> Tuple[bytes, Dict[str, str]]:
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url[:80]}")

        async with self._http_client.stream("GET", url, headers={"User-Agent": "HermesAgent/1.0", "Accept": "*/*"}) as response:
            response.raise_for_status()
            headers = {k.lower(): v for k, v in response.headers.items()}
            content_length = headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > ABSOLUTE_MAX_BYTES:
                raise ValueError(f"Remote media exceeds limit: {content_length} bytes")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > ABSOLUTE_MAX_BYTES:
                    raise ValueError(f"Remote media exceeds limit while downloading: {len(data)} bytes")
            return bytes(data), headers

    def _read_local_file(self, path: Path, file_name: Optional[str]) -> Tuple[bytes, str, str]:
        if self._media_local_roots:
            # Ensure the file is within one of the whitelisted roots
            if not any(str(path).startswith(str(root)) for root in self._media_local_roots):
                raise PermissionError(f"Local media path {path} is outside allowed roots")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Media file not found: {path}")
        data = path.read_bytes()
        resolved_name = file_name or path.name
        content_type = self._normalize_content_type("", resolved_name, data)
        return data, resolved_name, content_type

    @staticmethod
    def _normalize_content_type(content_type: str, filename: str, data: bytes) -> str:
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        detected = detect_mime_from_bytes(data)
        if not normalized:
            return detected or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if normalized in {"application/octet-stream", "text/plain"}:
            return detected or normalized
        return normalized

    @staticmethod
    def _guess_filename(url: str, content_disposition: Optional[str], content_type: str) -> str:
        import re
        if content_disposition:
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)
        name = Path(urlparse(url).path).name or "document"
        if "." not in name:
            ext = mimetypes.guess_extension(content_type) or ".bin"
            name = f"{name}{ext}"
        return name
```

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom_media.py -v`

Expected: all pass.

### Step 1.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_media.py tests/gateway/test_wecom_media.py
git commit -m "feat(wecom): add MediaPreparer with magic-byte detection and size limits"
```

---

## Task 2: Integrate `MediaPreparer` into `wecom.py`

**Files:**
- Modify: `gateway/platforms/wecom.py:1117-1166` (media prep and send paths)
- Test: `tests/gateway/test_wecom.py`

### Step 2.1: Write failing test for media prep integration

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom.py (append)
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_send_image_file_uses_media_preparer():
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
                mock_prep.prepare.assert_awaited_once_with("/tmp/img.png", None)
```

- [ ] **Run test**

Expected: `FAILED` — `wecom.py` doesn't use `MediaPreparer` yet.

### Step 2.2: Refactor `wecom.py` to use `MediaPreparer`

- [ ] **Modify `wecom.py`**

Add import:

```python
from gateway.platforms.wecom_media import MediaPreparer
```

Replace `_prepare_outbound_media` (`wecom.py:1152-1166`) with:

```python
    async def _prepare_outbound_media(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        preparer = MediaPreparer(
            self._http_client,
            media_local_roots=getattr(self._accounts[0] if self._accounts else None, "media_local_roots", None),
        )
        return await preparer.prepare(media_source, file_name=file_name)
```

Keep the rest of `_send_media_source` unchanged; it already calls `_prepare_outbound_media`.

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_send_image_file_uses_media_preparer -v`

Expected: `PASSED`

### Step 2.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "refactor(wecom): integrate MediaPreparer into outbound media flow"
```

---

## Task 3: Template Card Cache and Parsing

**Files:**
- Create: `gateway/platforms/wecom_template.py`
- Test: `tests/gateway/test_wecom_template.py`

### Step 3.1: Write failing test for template card extraction

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_template.py
import pytest
import time

from gateway.platforms.wecom_template import extract_template_card, TemplateCardCache


def test_extract_template_card_from_markdown():
    text = 'Some intro\n```json\n{"template_card": {"card_type": "vote_interaction", "source": {"desc": "投票"}}}\n```\nSome outro'
    card = extract_template_card(text)
    assert card is not None
    assert card["card_type"] == "vote_interaction"


def test_extract_template_card_none():
    assert extract_template_card("plain text") is None


def test_cache_set_and_get():
    cache = TemplateCardCache(ttl_seconds=300)
    cache.set("task-1", {"card_type": "vote_interaction"})
    assert cache.get("task-1") == {"card_type": "vote_interaction"}


def test_cache_expires():
    cache = TemplateCardCache(ttl_seconds=0)
    cache.set("task-1", {"card_type": "vote_interaction"})
    time.sleep(0.01)
    assert cache.get("task-1") is None
```

- [ ] **Run test**

Expected: `FAILED` — module doesn't exist.

### Step 3.2: Implement `wecom_template.py`

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_template.py
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VALID_CARD_TYPES = {
    "text_notice",
    "news_notice",
    "button_interaction",
    "vote_interaction",
    "multiple_interaction",
    "checkbox_interaction",
    "submit_button",
    "text_attribute",
    "open_semi_auto",
}

_RE_TEMPLATE_CARD = re.compile(
    r"```json\s*\n(.*?)\n```",
    re.DOTALL,
)


def extract_template_card(text: str) -> Optional[Dict[str, Any]]:
    """Search agent response text for a JSON block containing template_card."""
    for match in _RE_TEMPLATE_CARD.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "template_card" in data:
            card = data["template_card"]
            if isinstance(card, dict) and card.get("card_type") in VALID_CARD_TYPES:
                return card
    return None


def build_template_card_event_text(body: Dict[str, Any]) -> Optional[str]:
    """Format a template_card_event callback as synthetic text for the agent."""
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    if event.get("eventtype") != "template_card_event":
        return None

    tce = event.get("template_card_event") or {}
    selected_items = tce.get("selected_items", {}).get("selected_item", [])
    lines = []
    for item in selected_items:
        qk = item.get("question_key", "unknown")
        oids = item.get("option_ids", {}).get("option_id", [])
        lines.append(f"- {qk}: {', '.join(oids) if oids else '(未选择)'}")

    parts = [
        "[企业微信模板卡片回调]",
        f"event_type: template_card_event",
        f"card_type: {tce.get('card_type', 'N/A')}",
        f"event_key: {tce.get('event_key', 'N/A')}",
        f"task_id: {tce.get('task_id', 'N/A')}",
    ]
    if lines:
        parts.append("selected_items:")
        parts.extend(lines)
    return "\n".join(parts)


class TemplateCardCache:
    """In-memory TTL cache for sent template cards keyed by task_id."""

    def __init__(self, ttl_seconds: float = 86400):
        self._ttl = ttl_seconds
        self._store: Dict[str, Dict[str, Any]] = {}
        self._timestamps: Dict[str, float] = {}

    def set(self, task_id: str, card: Dict[str, Any]) -> None:
        self._store[task_id] = dict(card)
        self._timestamps[task_id] = time.time()
        self._prune()

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        self._prune()
        ts = self._timestamps.get(task_id)
        if ts is None or time.time() - ts > self._ttl:
            return None
        return dict(self._store.get(task_id, {}))

    def delete(self, task_id: str) -> None:
        self._store.pop(task_id, None)
        self._timestamps.pop(task_id, None)

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl
        expired = [k for k, ts in self._timestamps.items() if ts < cutoff]
        for k in expired:
            self._store.pop(k, None)
            self._timestamps.pop(k, None)
```

- [ ] **Run tests**

Run: `pytest tests/gateway/test_wecom_template.py -v`

Expected: all pass.

### Step 3.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_template.py tests/gateway/test_wecom_template.py
git commit -m "feat(wecom): add TemplateCardCache and card extraction/parser"
```

---

## Task 4: Integrate Template Cards into `wecom.py`

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 4.1: Write failing test for template card send path

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom.py (append)
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_send_detects_template_card_and_sends_via_websocket():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    content = '```json\n{"template_card": {"card_type": "text_notice", "source": {"desc": "通知"}}}\n```'

    with patch.object(adapter, "_send_request", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"headers": {"req_id": "r1"}}
        result = await adapter.send("chat-1", content)
        assert result.success is True
        call_body = mock_send.call_args[0][1]
        assert call_body["msgtype"] == "template_card"
        assert "template_card" in call_body
```

- [ ] **Run test**

Expected: `FAILED` — `send()` doesn't check for template cards yet.

### Step 4.2: Hook template card detection into `send()`

- [ ] **Modify `wecom.py`**

Add import:

```python
from gateway.platforms.wecom_template import extract_template_card, TemplateCardCache
```

In `__init__`, add:

```python
        self._template_card_cache = TemplateCardCache()
```

Modify `send()` (`wecom.py:1290+`) at the top:

```python
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata

        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        template_card = extract_template_card(content)
        if template_card:
            try:
                response = await self._send_request(
                    APP_CMD_SEND,
                    {
                        "chatid": chat_id,
                        "msgtype": "template_card",
                        "template_card": template_card,
                    },
                )
                self._raise_for_wecom_error(response, "send template card")
                # Cache by response task_id if present
                body = response.get("body") if isinstance(response.get("body"), dict) else {}
                task_id = body.get("task_id")
                if task_id:
                    self._template_card_cache.set(str(task_id), template_card)
                return SendResult(
                    success=True,
                    message_id=self._payload_req_id(response) or uuid.uuid4().hex[:12],
                    raw_response=response,
                )
            except Exception as exc:
                logger.error("[%s] Template card send failed: %s", self.name, exc)
                return SendResult(success=False, error=str(exc))

        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            ...
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_send_detects_template_card_and_sends_via_websocket -v`

Expected: `PASSED`

### Step 4.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): detect and send template cards via websocket"
```

---

## Task 5: Agent Webhook Media Outbound and Validation

**Files:**
- Modify: `gateway/platforms/wecom_callback.py`
- Test: `tests/gateway/test_wecom_callback.py`

### Step 5.1: Write failing test for media send in callback adapter

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_callback.py (append)
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_callback_adapter_sends_media_via_rest():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        extra={
            "corp_id": "c1",
            "corp_secret": "cs1",
            "agent_id": 100,
            "token": "tok",
            "encoding_aes_key": "key",
        }
    )
    adapter = WecomCallbackAdapter(config)
    adapter._http_client = AsyncMock()

    # Mock token fetch
    with patch.object(adapter, "_get_access_token", new_callable=AsyncMock) as mock_token:
        mock_token.return_value = "token-123"
        # Mock upload
        adapter._http_client.post = AsyncMock(return_value=AsyncMock(json=lambda: {"errcode": 0, "media_id": "mid-1"}))

        result = await adapter.send_document("c1:user-1", "/tmp/doc.pdf")
        assert result.success is True
```

- [ ] **Run test**

Expected: `FAILED` — callback adapter doesn't have native media send yet.

### Step 5.2: Implement media upload + send in callback adapter

- [ ] **Modify `wecom_callback.py`**

Add import:

```python
from gateway.platforms.wecom_media import MediaPreparer, detect_wecom_media_type
```

Add helper methods before `send()`:

```python
    async def _upload_media(self, app: Dict[str, Any], data: bytes, media_type: str, filename: str) -> str:
        token = await self._get_access_token(app)
        # WeCom REST API: https://qyapi.weixin.qq.com/cgi-bin/media/upload
        files = {"media": (filename, data)}
        resp = await self._http_client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type={media_type}",
            files=files,
        )
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"media upload failed: {result}")
        return str(result["media_id"])

    async def _send_media_message(
        self,
        app: Dict[str, Any],
        chat_id: str,
        media_type: str,
        media_id: str,
    ) -> Dict[str, Any]:
        token = await self._get_access_token(app)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        payload = {
            "touser": touser,
            "msgtype": media_type,
            "agentid": int(str(app.get("agent_id") or 0)),
            media_type: {"media_id": media_id},
            "safe": 0,
        }
        resp = await self._http_client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json=payload,
        )
        return resp.json()
```

Modify `send()` to support optional `media_type` and `media_id` kwargs:

```python
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        app = self._resolve_app_for_chat(chat_id)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        media_type = kwargs.get("media_type")
        media_id = kwargs.get("media_id")
        try:
            token = await self._get_access_token(app)
            payload: Dict[str, Any] = {
                "touser": touser,
                "agentid": int(str(app.get("agent_id") or 0)),
                "safe": 0,
            }
            if media_type and media_id:
                payload["msgtype"] = media_type
                payload[media_type] = {"media_id": media_id}
            else:
                payload["msgtype"] = "text"
                payload["text"] = {"content": content[:2048]}

            resp = await self._http_client.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                json=payload,
            )
            data = resp.json()
            if data.get("errcode") != 0:
                return SendResult(success=False, error=str(data))
            return SendResult(
                success=True,
                message_id=str(data.get("msgid", "")),
                raw_response=data,
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))
```

Then override `send_image_file`, `send_document`, `send_voice`, `send_video` to use the preparer + upload:

```python
    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return await self._send_media_source(chat_id, image_path, caption=caption, reply_to=reply_to)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return await self._send_media_source(chat_id, file_path, caption=caption, file_name=file_name, reply_to=reply_to)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return await self._send_media_source(chat_id, audio_path, caption=caption, reply_to=reply_to)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return await self._send_media_source(chat_id, video_path, caption=caption, reply_to=reply_to)

    async def _send_media_source(
        self,
        chat_id: str,
        media_source: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        app = self._resolve_app_for_chat(chat_id)
        try:
            preparer = MediaPreparer(self._http_client, media_local_roots=[])
            prepared = await preparer.prepare(media_source, file_name)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

        if prepared["rejected"]:
            return await self.send(chat_id, f"⚠️ {prepared['reject_reason']}")

        try:
            media_id = await self._upload_media(app, prepared["data"], prepared["final_type"], prepared["file_name"])
            result = await self.send(chat_id, caption or "", media_type=prepared["final_type"], media_id=media_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

        if prepared["downgraded"] and prepared["downgrade_note"]:
            await self.send(chat_id, f"ℹ️ {prepared['downgrade_note']}")

        return result
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_callback.py::test_callback_adapter_sends_media_via_rest -v`

Expected: `PASSED`

### Step 5.3: Add `agent_id` consistency check in callback handler

- [ ] **Modify `_handle_callback`**

After decrypting the XML in `_handle_callback`, extract `AgentID` and compare:

```python
        agent_id_in_xml = root.findtext("AgentID")
        if agent_id_in_xml is not None:
            configured_agent_id = str(app.get("agent_id") or "")
            if configured_agent_id and agent_id_in_xml != configured_agent_id:
                logger.warning(
                    "[WecomCallback] agent_id mismatch for app '%s': expected=%s actual=%s",
                    app.get("name"), configured_agent_id, agent_id_in_xml,
                )
```

- [ ] **Add test for agent_id mismatch**

```python
# tests/gateway/test_wecom_callback.py (append)

def test_callback_build_event_logs_agent_id_mismatch(caplog):
    import logging
    from gateway.platforms.wecom_callback import WecomCallbackAdapter
    from gateway.config import PlatformConfig

    config = PlatformConfig(
        extra={
            "corp_id": "c1",
            "corp_secret": "cs",
            "agent_id": 100,
            "token": "tok",
            "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        }
    )
    adapter = WecomCallbackAdapter(config)
    xml = """<xml><ToUserName>c1</ToUserName><FromUserName>user1</FromUserName><MsgType>text</MsgType><Content>hi</Content><MsgId>1</MsgId><AgentID>999</AgentID></xml>"""
    app = adapter._apps[0]
    with caplog.at_level(logging.WARNING):
        event = adapter._build_event(app, xml)
        assert event is not None
        assert "agent_id mismatch" in caplog.text
```

- [ ] **Run callback tests**

Run: `pytest tests/gateway/test_wecom_callback.py -v`

Expected: all pass.

### Step 5.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_callback.py tests/gateway/test_wecom_callback.py
git commit -m "feat(wecom_callback): add media outbound, agent_id checks, and welcome text"
```

---

## Task 6: Final Regression Check

- [ ] **Run all WeCom tests**

Run:
```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py tests/gateway/test_wecom_media.py tests/gateway/test_wecom_template.py tests/gateway/test_wecom_webhook.py tests/gateway/test_wecom_transport.py tests/gateway/test_wecom_accounts.py -v
```

Expected: all pass.

- [ ] **Commit**

```bash
git add -A
git commit -m "test(wecom): full regression for media, template cards, and agent parity" || true
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Magic-byte MIME detection | Task 1 |
| Size downgrade rules | Task 1 |
| Media preparer integration | Task 2 |
| Template card cache | Task 3 |
| Template card extraction | Task 3 |
| Template card send via WS | Task 4 |
| Agent webhook media upload + send | Task 5 |
| Agent webhook `agent_id` validation | Task 5 |
| No regressions | Task 6 |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-14-wecom-media-cards.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks.
2. **Inline Execution** — Batch execute in this session using `superpowers:executing-plans`.

Which approach?
