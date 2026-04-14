# WeCom Transport-Layer Feature Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add transport-layer feature parity to `hermes-agent` WeCom connectors: command auth, webhook stream store, shared media helpers, agent callback media, and non-blocking WS stream sends.

**Architecture:** Create three focused modules (`wecom_command_auth.py`, `wecom_stream_store.py`, `wecom_media.py`) and integrate them into `wecom.py` and `wecom_callback.py`. Follow strict TDD: write failing test, watch it fail, implement minimal code, verify pass, commit.

**Tech Stack:** Python 3.11+, asyncio, pytest, aiohttp, httpx.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `gateway/platforms/wecom_command_auth.py` | **Create** — command detection, authorization logic, rejection prompts |
| `tests/gateway/test_wecom_command_auth.py` | **Create** — TDD tests for command auth |
| `gateway/platforms/wecom_stream_store.py` | **Create** — `StreamStore`, `StreamState`, `PendingBatch`, debounce/flush/prune |
| `tests/gateway/test_wecom_stream_store.py` | **Create** — TDD tests for stream store |
| `gateway/platforms/wecom_media.py` | **Create** — `MediaPreparer`, magic-byte detection, size downgrade rules |
| `tests/gateway/test_wecom_media.py` | **Create** — TDD tests for media helpers |
| `gateway/platforms/wecom_callback.py` | **Modify** — add agent REST media upload/send, agent_id validation |
| `tests/gateway/test_wecom_callback.py` | **Extend** — tests for callback media and agent_id mismatch |
| `gateway/platforms/wecom.py` | **Modify** — integrate stream store, command auth, non-blocking stream sends |
| `tests/gateway/test_wecom.py` | **Extend** — tests for non-blocking sends and integrations |

---

## Task 1: Command Authorization Core

**Files:**
- Create: `gateway/platforms/wecom_command_auth.py`
- Test: `tests/gateway/test_wecom_command_auth.py`

### Step 1.1: Write failing test for command detection

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_command_auth.py
from gateway.platforms.wecom_command_auth import is_command


def test_is_command_detects_slash_commands():
    assert is_command("/reset") is True
    assert is_command("/new session") is True


def test_is_command_ignores_plain_text_and_urls():
    assert is_command("hello world") is False
    assert is_command("https://example.com") is False
    assert is_command("/") is False
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_command_auth.py -v`

Expected: `FAILED` — `ModuleNotFoundError: No module named 'gateway.platforms.wecom_command_auth'`

### Step 1.2: Implement minimal command detection

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_command_auth.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from gateway.platforms.wecom_accounts import WeComAccount


def is_command(raw_body: str) -> bool:
    text = str(raw_body or "").strip()
    if not text or text.startswith("http://") or text.startswith("https://"):
        return False
    return text.startswith("/") and len(text) > 1
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_command_auth.py -v`

Expected: both tests `PASSED`

### Step 1.3: Write failing test for `resolve_command_auth`

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_command_auth.py (append)
from gateway.platforms.wecom_command_auth import resolve_command_auth, build_unauthorized_command_prompt
from gateway.platforms.wecom_accounts import WeComAccount


def test_open_policy_allows_commands():
    account = WeComAccount(account_id="a1", dm_policy="open")
    result = resolve_command_auth(account, "/reset", "user1")
    assert result.command_authorized is True
    assert result.should_compute_auth is False


def test_allowlist_blocks_unknown_sender():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["alice"])
    result = resolve_command_auth(account, "/reset", "bob")
    assert result.should_compute_auth is True
    assert result.command_authorized is False


def test_allowlist_allows_known_sender():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["alice"])
    result = resolve_command_auth(account, "/reset", "alice")
    assert result.command_authorized is True


def test_wildcard_allowlist_allows_anyone():
    account = WeComAccount(account_id="a1", dm_policy="allowlist", allow_from=["*"])
    result = resolve_command_auth(account, "/reset", "anyone")
    assert result.command_authorized is True
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_command_auth.py::test_open_policy_allows_commands -v`

Expected: `FAILED` — `resolve_command_auth` not defined

### Step 1.4: Implement `resolve_command_auth`

- [ ] **Write implementation**

Append to `gateway/platforms/wecom_command_auth.py`:

```python
from dataclasses import dataclass


@dataclass
class CommandAuthResult:
    command_authorized: bool
    should_compute_auth: bool
    dm_policy: str
    sender_allowed: bool
    authorizer_configured: bool


def _entry_matches(entries: List[str], target: str) -> bool:
    normalized_target = str(target).strip().lower()
    for entry in entries:
        normalized = str(entry).strip().lower()
        if normalized == "*" or normalized == normalized_target:
            return True
    return False


def resolve_command_auth(
    account: WeComAccount,
    raw_body: str,
    sender_user_id: str,
) -> CommandAuthResult:
    dm_policy = account.dm_policy
    allow_from = account.allow_from
    sender_allowed = _entry_matches(allow_from, sender_user_id)

    # authorizer_configured: check if an authorizer is set in account config
    authorizer_configured = bool(
        isinstance(account.groups, dict) and account.groups.get("authorizer")
    )

    should_compute_auth = is_command(raw_body) and dm_policy == "allowlist"

    if not should_compute_auth:
        return CommandAuthResult(
            command_authorized=True,
            should_compute_auth=False,
            dm_policy=dm_policy,
            sender_allowed=sender_allowed,
            authorizer_configured=authorizer_configured,
        )

    command_authorized = sender_allowed
    return CommandAuthResult(
        command_authorized=command_authorized,
        should_compute_auth=True,
        dm_policy=dm_policy,
        sender_allowed=sender_allowed,
        authorizer_configured=authorizer_configured,
    )


def build_unauthorized_command_prompt(
    sender_user_id: str,
    dm_policy: str,
    scope: str = "bot",
) -> str:
    return (
        f"抱歉，您没有权限执行该命令。"
        f"当前私信策略为 {dm_policy}，请联系管理员添加到允许列表后重试。"
    )
```

- [ ] **Run tests to verify they pass**

Run: `pytest tests/gateway/test_wecom_command_auth.py -v`

Expected: all tests `PASSED`

### Step 1.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_command_auth.py tests/gateway/test_wecom_command_auth.py
git commit -m "feat(wecom): add command authorization module with TDD"
```

---

## Task 2: Shared Media Helpers

**Files:**
- Create: `gateway/platforms/wecom_media.py`
- Test: `tests/gateway/test_wecom_media.py`

### Step 2.1: Write failing test for magic-byte detection

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_media.py
from gateway.platforms.wecom_media import detect_mime_from_bytes, apply_file_size_limits, detect_wecom_media_type


def test_detect_png():
    data = b"\x89PNG\r\n\x1a\n" + b"fake"
    assert detect_mime_from_bytes(data) == "image/png"


def test_detect_jpeg():
    data = b"\xff\xd8\xff" + b"fake"
    assert detect_mime_from_bytes(data) == "image/jpeg"


def test_detect_pdf():
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

Expected: `FAILED` — `ModuleNotFoundError`

### Step 2.2: Implement media helpers

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_media.py
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

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
            resolved_name = file_name or self._guess_filename(
                source, headers.get("content-disposition"), headers.get("content-type", "")
            )
            content_type = self._normalize_content_type(headers.get("content-type", ""), resolved_name, data)
        elif parsed.scheme == "file":
            local_path = Path(unquote(parsed.path)).expanduser().resolve()
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

        async with self._http_client.stream(
            "GET", url, headers={"User-Agent": "HermesAgent/1.0", "Accept": "*/*"}
        ) as response:
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

- [ ] **Run tests to verify they pass**

Run: `pytest tests/gateway/test_wecom_media.py -v`

Expected: all tests `PASSED`

### Step 2.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_media.py tests/gateway/test_wecom_media.py
git commit -m "feat(wecom): add shared media helpers with magic-byte detection and TDD"
```

---

## Task 3: Stream Store for Webhook Mode

**Files:**
- Create: `gateway/platforms/wecom_stream_store.py`
- Test: `tests/gateway/test_wecom_stream_store.py`

### Step 3.1: Write failing test for stream creation

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_stream_store.py
import asyncio
import pytest

from gateway.platforms.wecom_stream_store import StreamStore


@pytest.mark.asyncio
async def test_create_and_get_stream():
    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    stream_id = store.create_stream()
    stream = store.get_stream(stream_id)
    assert stream is not None
    assert stream.stream_id == stream_id
    assert stream.finished is False
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_create_and_get_stream -v`

Expected: `FAILED` — `ModuleNotFoundError`

### Step 3.2: Implement StreamStore core

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_stream_store.py
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StreamState:
    stream_id: str
    content: str = ""
    finished: bool = False
    started: bool = False
    created_at: float = field(default_factory=time.time)
    user_id: str = ""
    chat_type: str = "direct"
    chat_id: str = ""
    aibotid: str = ""
    task_key: str = ""
    images: List[Dict[str, str]] = field(default_factory=list)
    fallback_mode: str = ""
    fallback_prompt_sent_at: Optional[float] = None
    dm_content: str = ""
    agent_media_keys: List[str] = field(default_factory=list)


@dataclass
class PendingBatch:
    conversation_key: str
    stream_id: str
    target: Any
    msg: Dict[str, Any]
    contents: List[str]
    msgids: List[str]
    nonce: str
    timestamp: str
    timer: Optional[asyncio.Task] = None


@dataclass
class PendingInbound:
    stream_id: str
    target: Any
    msg: Dict[str, Any]
    contents: List[str]
    msgids: List[str]
    conversation_key: str
    batch_key: str


class StreamStore:
    def __init__(
        self,
        flush_handler: Callable[[PendingInbound], Any],
        ttl_seconds: float = 600,
    ):
        self._flush_handler = flush_handler
        self._ttl_seconds = ttl_seconds
        self._streams: Dict[str, StreamState] = {}
        self._msgid_to_stream: Dict[str, str] = {}
        self._batches: Dict[str, PendingBatch] = {}
        self._ack_streams: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._prune_task: Optional[asyncio.Task] = None
        self._prune_interval_ms: float = 30000

    async def _acquire(self):
        await self._lock.acquire()

    def _release(self):
        self._lock.release()

    def create_stream(self, msgid: Optional[str] = None) -> str:
        stream_id = f"stream-{uuid.uuid4().hex}"
        self._streams[stream_id] = StreamState(stream_id=stream_id)
        if msgid:
            self._msgid_to_stream[msgid] = stream_id
        return stream_id

    def get_stream(self, stream_id: str) -> Optional[StreamState]:
        return self._streams.get(stream_id)

    def update_stream(self, stream_id: str, updater: Callable[[StreamState], None]) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            updater(stream)

    def mark_started(self, stream_id: str) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            stream.started = True

    def mark_finished(self, stream_id: str) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            stream.finished = True

    def get_stream_by_msgid(self, msgid: str) -> Optional[str]:
        return self._msgid_to_stream.get(msgid)

    def set_stream_id_for_msgid(self, msgid: str, stream_id: str) -> None:
        self._msgid_to_stream[msgid] = stream_id

    def add_pending_message(
        self,
        conversation_key: str,
        target: Any,
        msg: Dict[str, Any],
        msg_content: str,
        nonce: str,
        timestamp: str,
        debounce_ms: float = 300,
    ) -> Tuple[str, str]:
        batch = self._batches.get(conversation_key)
        if batch is not None:
            batch.contents.append(msg_content)
            if msg.get("msgid"):
                batch.msgids.append(str(msg["msgid"]))
            return batch.stream_id, "active_merged"

        stream_id = self.create_stream(msgid=msg.get("msgid"))
        batch = PendingBatch(
            conversation_key=conversation_key,
            stream_id=stream_id,
            target=target,
            msg=msg,
            contents=[msg_content],
            msgids=[str(msg["msgid"])] if msg.get("msgid") else [],
            nonce=nonce,
            timestamp=timestamp,
        )
        self._batches[conversation_key] = batch
        batch.timer = asyncio.get_running_loop().create_task(
            self._flush_after(batch, debounce_ms)
        )
        return stream_id, "active_new"

    async def _flush_after(self, batch: PendingBatch, debounce_ms: float) -> None:
        await asyncio.sleep(debounce_ms / 1000)
        self._batches.pop(batch.conversation_key, None)
        pending = PendingInbound(
            stream_id=batch.stream_id,
            target=batch.target,
            msg=batch.msg,
            contents=batch.contents,
            msgids=batch.msgids,
            conversation_key=batch.conversation_key,
            batch_key=batch.conversation_key,
        )
        try:
            result = self._flush_handler(pending)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("[StreamStore] Flush handler failed for stream=%s", batch.stream_id)

    def add_ack_stream_for_batch(self, batch_stream_id: str, ack_stream_id: str) -> None:
        self._ack_streams.setdefault(batch_stream_id, []).append(ack_stream_id)

    def drain_ack_streams_for_batch(self, batch_stream_id: str) -> List[str]:
        return self._ack_streams.pop(batch_stream_id, [])

    def start_pruning(self, interval_ms: float) -> None:
        self._prune_interval_ms = interval_ms
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = asyncio.get_running_loop().create_task(self._prune_loop())

    def stop_pruning(self) -> None:
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
            self._prune_task = None

    async def _prune_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._prune_interval_ms / 1000)
                self._prune()
        except asyncio.CancelledError:
            pass

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        expired = [sid for sid, s in self._streams.items() if s.finished and s.created_at < cutoff]
        for sid in expired:
            self._streams.pop(sid, None)
        expired_msgids = [mid for mid, sid in self._msgid_to_stream.items() if sid in expired]
        for mid in expired_msgids:
            self._msgid_to_stream.pop(mid, None)
```

- [ ] **Run tests to verify they pass**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_create_and_get_stream -v`

Expected: `PASSED`

### Step 3.3: Write failing test for debounce flush

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_stream_store.py`:

```python
@pytest.mark.asyncio
async def test_debounce_flush_calls_handler():
    flushed = []

    async def flush_handler(pending):
        flushed.append(pending.stream_id)

    store = StreamStore(flush_handler=flush_handler)
    stream_id, status = store.add_pending_message(
        conversation_key="wecom:u1:c1",
        target=None,
        msg={"msgid": "m1", "text": {"content": "hello"}},
        msg_content="hello",
        nonce="n1",
        timestamp="t1",
        debounce_ms=50,
    )
    assert status == "active_new"
    await asyncio.sleep(0.1)
    assert len(flushed) == 1
    assert flushed[0] == stream_id
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_debounce_flush_calls_handler -v`

Expected: `FAILED` — because we need to run it in an event loop. If it passes, good; if it errors on `asyncio.get_running_loop()`, fix `add_pending_message`.

If it errors with `No running event loop`, wrap `add_pending_message` to use `asyncio.get_event_loop()` when no loop is running:

```python
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        batch.timer = loop.create_task(self._flush_after(batch, debounce_ms))
```

- [ ] **Run test again**

Expected: `PASSED`

### Step 3.4: Write failing test for ack streams

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_stream_store.py`:

```python
def test_ack_streams_for_batch():
    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    store.add_ack_stream_for_batch("batch-1", "ack-1")
    store.add_ack_stream_for_batch("batch-1", "ack-2")
    assert store.drain_ack_streams_for_batch("batch-1") == ["ack-1", "ack-2"]
    assert store.drain_ack_streams_for_batch("batch-1") == []
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_ack_streams_for_batch -v`

Expected: `PASSED` (no new code needed; verifies existing behavior)

### Step 3.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_stream_store.py tests/gateway/test_wecom_stream_store.py
git commit -m "feat(wecom): add webhook StreamStore with debounce, flush, and TDD"
```

---

## Task 4: Agent Callback Media Upload and Send

**Files:**
- Modify: `gateway/platforms/wecom_callback.py`
- Test: `tests/gateway/test_wecom_callback.py`

### Step 4.1: Write failing test for agent_id mismatch warning

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_callback.py`:

```python
import logging


def test_build_event_logs_agent_id_mismatch(caplog):
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        enabled=True,
        extra={
            "corp_id": "c1",
            "corp_secret": "cs",
            "agent_id": 100,
            "token": "tok",
            "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        },
    )
    adapter = WecomCallbackAdapter(config)
    xml = """<xml><ToUserName>c1</ToUserName><FromUserName>user1</FromUserName><MsgType>text</MsgType><Content>hi</Content><MsgId>1</MsgId><AgentID>999</AgentID></xml>"""
    account = adapter._accounts[0]
    with caplog.at_level(logging.WARNING):
        event = adapter._build_event(account, xml)
        assert event is not None
        assert "agent_id mismatch" in caplog.text
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_callback.py::test_build_event_logs_agent_id_mismatch -v`

Expected: `FAILED` — test passes but warning isn't logged, or assertion fails on `caplog.text`

### Step 4.2: Add agent_id validation to callback handler

- [ ] **Modify `wecom_callback.py`**

In `_handle_callback`, after decrypting, before `_build_event`, add:

```python
            # AgentID consistency check
            agent_id_in_xml = root.findtext("AgentID")
            if agent_id_in_xml is not None:
                configured_agent_id = str(account.agent_id or "")
                if configured_agent_id and agent_id_in_xml != configured_agent_id:
                    logger.warning(
                        "[WecomCallback] agent_id mismatch for account '%s': expected=%s actual=%s",
                        account.account_id, configured_agent_id, agent_id_in_xml,
                    )
```

Also update `_build_event` to accept the XML with `AgentID` (it already parses the full XML).

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_callback.py::test_build_event_logs_agent_id_mismatch -v`

Expected: `PASSED`

### Step 4.3: Write failing test for callback adapter media send

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_callback.py`:

```python
@pytest.mark.asyncio
async def test_callback_adapter_sends_media_via_rest():
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        enabled=True,
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

    with pytest.raises(AttributeError):
        await adapter.send_document("c1:user-1", "/tmp/doc.pdf")
```

Wait — that's a weak test. Write a stronger one that verifies the flow once methods exist. Since we're doing TDD, first show the failure due to missing method, then implement.

```python
@pytest.mark.asyncio
async def test_callback_adapter_send_document_raises_not_implemented_yet():
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        enabled=True,
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

    # Method should not exist yet → AttributeError
    with pytest.raises(AttributeError):
        await adapter.send_document("c1:user-1", "/tmp/doc.pdf")
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom_callback.py::test_callback_adapter_send_document_raises_not_implemented_yet -v`

Expected: `PASSED` (AttributeError because method is missing — this is the RED state proving we need to add it)

### Step 4.4: Implement media send methods

- [ ] **Modify `wecom_callback.py`**

Add imports at top:

```python
from gateway.platforms.wecom_media import MediaPreparer
```

Add new methods before `send()`:

```python
    async def _upload_media_to_agent_api(
        self,
        account: WeComAccount,
        data: bytes,
        media_type: str,
        filename: str,
    ) -> str:
        token = await self._get_access_token(account)
        # httpx doesn't support files= like requests; use multipart manually or switch to requests-like client.
        # Since this adapter uses httpx, construct multipart body manually.
        boundary = f"----FormBoundary{uuid.uuid4().hex}"
        header = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = header + data + footer

        resp = await self._http_client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type={media_type}",
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"media upload failed: {result}")
        return str(result["media_id"])

    async def _send_media_message(
        self,
        account: WeComAccount,
        chat_id: str,
        media_type: str,
        media_id: str,
    ) -> Dict[str, Any]:
        token = await self._get_access_token(account)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        payload = {
            "touser": touser,
            "msgtype": media_type,
            "agentid": int(account.agent_id or 0),
            media_type: {"media_id": media_id},
            "safe": 0,
        }
        resp = await self._http_client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json=payload,
        )
        return resp.json()

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_source(chat_id, image_path, caption=caption, reply_to=reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_source(chat_id, file_path, caption=caption, file_name=file_name, reply_to=reply_to)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_source(chat_id, audio_path, caption=caption, reply_to=reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_source(chat_id, video_path, caption=caption, reply_to=reply_to)

    async def _send_media_source(
        self,
        chat_id: str,
        media_source: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        account = self._resolve_account_for_chat(chat_id)
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
            media_id = await self._upload_media_to_agent_api(
                account, prepared["data"], prepared["final_type"], prepared["file_name"]
            )
            result = await self.send(
                chat_id, caption or "", media_type=prepared["final_type"], media_id=media_id
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

        if prepared["downgraded"] and prepared["downgrade_note"]:
            await self.send(chat_id, f"ℹ️ {prepared['downgrade_note']}")

        return result
```

Also modify `send()` signature to accept `**kwargs`:

```python
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
```

And inside `send()`, handle `media_type` and `media_id` kwargs:

```python
        account = self._resolve_account_for_chat(chat_id)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        media_type = kwargs.get("media_type")
        media_id = kwargs.get("media_id")
        try:
            token = await self._get_access_token(account)
            payload: Dict[str, Any] = {
                "touser": touser,
                "agentid": int(account.agent_id or 0),
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

Add `import uuid` at top of file if not already present.

- [ ] **Run updated test**

Update the test to verify the method now exists and can be mocked:

```python
@pytest.mark.asyncio
async def test_callback_adapter_send_document_flow():
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom_callback import WecomCallbackAdapter

    config = PlatformConfig(
        enabled=True,
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

    with patch.object(adapter, "_upload_media_to_agent_api", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = "mid-1"
        with patch.object(adapter, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True}
            result = await adapter.send_document("c1:user-1", "/tmp/doc.pdf")
            mock_upload.assert_awaited_once()
```

Wait — `mock_send.return_value` needs to be a `SendResult`. Fix:

```python
from gateway.platforms.base import SendResult

            mock_send.return_value = SendResult(success=True, message_id="m1")
            result = await adapter.send_document("c1:user-1", "/tmp/doc.pdf")
            assert result.success is True
            mock_upload.assert_awaited_once()
```

Run: `pytest tests/gateway/test_wecom_callback.py::test_callback_adapter_send_document_flow -v`

Expected: `PASSED`

### Step 4.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_callback.py tests/gateway/test_wecom_callback.py
git commit -m "feat(wecom_callback): add agent REST media upload/send and agent_id validation with TDD"
```

---

## Task 5: Non-Blocking WS Stream Sends

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 5.1: Write failing test for non-blocking stream send

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_nonblocking_stream_skips_when_pending():
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)
    adapter._reply_req_ids["msg-1"] = "req-1"
    adapter._pending_stream_acks.add("stream-1")

    result = await adapter._send_reply_stream_nonblocking("req-1", "stream-1", "partial")
    assert result == "skipped"
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_nonblocking_stream_skips_when_pending -v`

Expected: `FAILED` — `AttributeError: 'WeComAdapter' object has no attribute '_send_reply_stream_nonblocking'`

### Step 5.2: Implement non-blocking stream send

- [ ] **Modify `wecom.py`**

In `__init__`, add:

```python
        self._pending_stream_acks: set[str] = set()
```

After `_send_reply_stream`, add:

```python
    async def _send_reply_stream_nonblocking(
        self,
        reply_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
    ) -> str:
        """Send a stream update, skipping if a prior frame is still pending (unless finish=True)."""
        if not finish and stream_id in self._pending_stream_acks:
            logger.debug("[%s] Skipping non-blocking stream %s (pending ack)", self.name, stream_id)
            return "skipped"

        self._pending_stream_acks.add(stream_id)
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
```

Update `_dispatch_payload` to clear acks on successful responses:

Find `_dispatch_payload` and update the beginning:

```python
    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}

        # Clear pending stream ack on successful response
        if body.get("errcode") in (0, None):
            self._pending_stream_acks.discard(req_id)

        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return
        ...
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_nonblocking_stream_skips_when_pending -v`

Expected: `PASSED`

### Step 5.3: Write test that finish=True is never skipped

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_nonblocking_stream_never_skips_finish():
    from unittest.mock import AsyncMock
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
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_nonblocking_stream_never_skips_finish -v`

Expected: `PASSED`

### Step 5.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): add non-blocking WS stream sends with pending-ack skip"
```

---

## Task 6: Integrate Stream Store and Command Auth into Webhook Path

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`, `tests/gateway/test_wecom_webhook.py`

### Step 6.1: Write failing test for webhook command auth rejection

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_webhook.py`:

```python
@pytest.mark.asyncio
async def test_webhook_callback_blocks_unauthorized_command():
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_webhook.py::test_webhook_callback_blocks_unauthorized_command -v`

Expected: `FAILED` — assertion on `"没有权限"` will fail because current webhook callback just dispatches to `_on_message`

### Step 6.2: Integrate stream store and command auth into webhook callback

- [ ] **Modify `wecom.py`**

Add imports:

```python
from gateway.platforms.wecom_command_auth import resolve_command_auth, build_unauthorized_command_prompt
from gateway.platforms.wecom_stream_store import StreamStore, PendingInbound
```

In `__init__`, add stream store initialization for webhook mode:

```python
        self._stream_store: Optional[StreamStore] = None
```

Update `_start_webhook_server` to create the stream store:

```python
    async def _start_webhook_server(self, accounts: List[WeComAccount]) -> None:
        ...
        self._stream_store = StreamStore(flush_handler=self._on_webhook_flush)
        self._stream_store.start_pruning(30000)
        ...
```

Add the flush handler:

```python
    async def _on_webhook_flush(self, pending: PendingInbound) -> None:
        if not self._stream_store:
            return
        self._stream_store.mark_started(pending.stream_id)
        account = pending.target
        msg = pending.msg
        sender_id = str(msg.get("from", {}).get("userid") or "").strip()
        chat_id = str(msg.get("chatid") or sender_id).strip()
        raw_body = str(pending.contents[0]) if pending.contents else ""

        auth = resolve_command_auth(account, raw_body, sender_id)
        if auth.should_compute_auth and not auth.command_authorized:
            prompt = build_unauthorized_command_prompt(sender_id, auth.dm_policy)
            self._stream_store.update_stream(pending.stream_id, lambda s: setattr(s, "content", prompt) or setattr(s, "finished", True))
            return

        # Build synthetic WS-style payload and dispatch
        synthetic_payload = {
            "cmd": APP_CMD_CALLBACK,
            "headers": {"req_id": f"wh-{uuid.uuid4().hex}"},
            "body": msg,
        }
        try:
            await self._on_message(synthetic_payload)
        except Exception:
            logger.exception("[%s] Failed to dispatch webhook message", self.name)
```

Wait — `pending.target` is typed as `Any`. In `_handle_webhook_callback`, when we call `add_pending_message`, we need to pass the account as the target. Update `_handle_webhook_callback`:

Replace the message dispatch block:

```python
        # Normal message: synthesize a WS-style payload and dispatch
        payload = {
            "cmd": APP_CMD_CALLBACK,
            "headers": {"req_id": self._new_req_id("wh")},
            "body": decrypted,
        }
        try:
            await self._on_message(payload)
        except Exception as exc:
            logger.exception("[%s] Failed to dispatch webhook message: %s", self.name, exc)
```

With stream-store-based dispatch:

```python
        msgtype = str(decrypted.get("msgtype") or "").lower()
        if msgtype in {"text", "image", "file", "voice", "video", "mixed"}:
            sender_id = str(decrypted.get("from", {}).get("userid") or "").strip()
            chat_id = str(decrypted.get("chatid") or sender_id).strip()
            content = ""
            text_block = decrypted.get("text") if isinstance(decrypted.get("text"), dict) else {}
            content = str(text_block.get("content") or "").strip()

            if self._stream_store:
                stream_id, status = self._stream_store.add_pending_message(
                    conversation_key=f"wecom:{account.account_id}:{sender_id}:{chat_id}",
                    target=account,
                    msg=decrypted,
                    msg_content=content,
                    nonce=nonce,
                    timestamp=timestamp,
                    debounce_ms=300,
                )
                return web.json_response({"errcode": 0, "errmsg": "ok"})

        elif msgtype == "stream_refresh":
            if self._stream_store:
                stream_id = str(decrypted.get("stream_id") or "")
                stream = self._stream_store.get_stream(stream_id)
                if stream:
                    return web.json_response({
                        "msgtype": "stream",
                        "stream": {
                            "id": stream_id,
                            "finish": stream.finished,
                            "content": stream.content,
                        },
                    })
                return web.json_response({
                    "msgtype": "stream",
                    "stream": {"id": stream_id, "finish": True, "content": ""},
                })

        elif msgtype == "enter_chat":
            welcome = account.welcome_text or "你好，有什么可以帮你的？"
            return web.json_response({
                "msgtype": "markdown",
                "markdown": {"content": welcome},
            })

        return web.json_response({"errcode": 0, "errmsg": "ok"})
```

Note: the existing `_handle_webhook_callback` already handles `stream_refresh` and `enter_chat` inline. The main change is replacing the normal message dispatch with `StreamStore.add_pending_message`, and adding command auth in the flush handler.

Also update `_cleanup_webhook` to stop pruning:

```python
    async def _cleanup_webhook(self) -> None:
        if self._stream_store:
            self._stream_store.stop_pruning()
            self._stream_store = None
        ...
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_webhook.py::test_webhook_callback_blocks_unauthorized_command -v`

Expected: `PASSED`

### Step 6.3: Write test for WS mode command auth integration

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_on_message_blocks_unauthorized_command():
    from unittest.mock import AsyncMock
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
    with pytest.raises(AssertionError):
        await adapter._on_message(payload)
        adapter.handle_message.assert_not_awaited()
```

Wait — we want the test to pass when command auth works. The current `_on_message` doesn't have command auth yet, so `handle_message` WILL be called, making `assert_not_awaited` fail. That's our RED state.

```python
@pytest.mark.asyncio
async def test_on_message_blocks_unauthorized_command():
    from unittest.mock import AsyncMock
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_on_message_blocks_unauthorized_command -v`

Expected: `FAILED` — `AssertionError` because `handle_message` was called (no command auth yet)

### Step 6.4: Add command auth to `_on_message`

- [ ] **Modify `wecom.py`**

In `_on_message`, after extracting `text` and `sender_id`, before the policy checks, add:

```python
        # Command authorization check
        account = self._accounts[0] if self._accounts else WeComAccount(account_id="default")
        auth = resolve_command_auth(account, text, sender_id)
        if auth.should_compute_auth and not auth.command_authorized:
            # For WS mode, we can send a rejection prompt back proactively
            prompt = build_unauthorized_command_prompt(sender_id, auth.dm_policy)
            try:
                await self._send_request(
                    APP_CMD_SEND,
                    {
                        "chatid": chat_id,
                        "msgtype": "markdown",
                        "markdown": {"content": prompt},
                    },
                )
            except Exception as exc:
                logger.warning("[%s] Failed to send command rejection: %s", self.name, exc)
            return
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_on_message_blocks_unauthorized_command -v`

Expected: `PASSED`

### Step 6.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py tests/gateway/test_wecom_webhook.py
git commit -m "feat(wecom): integrate command auth and StreamStore into webhook and WS paths"
```

---

## Task 7: Refactor `wecom.py` to Use `MediaPreparer`

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 7.1: Write failing test for `MediaPreparer` integration

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_send_image_file_uses_media_preparer():
    from unittest.mock import AsyncMock, patch
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

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_send_image_file_uses_media_preparer -v`

Expected: `FAILED` — `wecom.py` doesn't import/use `MediaPreparer` yet

### Step 7.2: Replace inline media prep with `MediaPreparer`

- [ ] **Modify `wecom.py`**

Add import:

```python
from gateway.platforms.wecom_media import MediaPreparer
```

Replace `_prepare_outbound_media` (~line 1322 in current file):

```python
    async def _prepare_outbound_media(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        roots = self._accounts[0].media_local_roots if self._accounts else []
        preparer = MediaPreparer(self._http_client, media_local_roots=roots)
        return await preparer.prepare(media_source, file_name=file_name)
```

Delete the old helper methods that `MediaPreparer` now covers:
- `_load_outbound_media` (keep for now; `_prepare_outbound_media` calls `MediaPreparer.prepare` which handles loading internally)

Actually, `_send_media_source` already calls `_prepare_outbound_media`, so just replacing that method is sufficient. The old `_load_outbound_media`, `_download_remote_bytes`, etc. can be left in place or deleted in a follow-up refactor. For minimal change, just replace `_prepare_outbound_media`.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_send_image_file_uses_media_preparer -v`

Expected: `PASSED`

### Step 7.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "refactor(wecom): integrate MediaPreparer into outbound media flow"
```

---

## Task 8: Final Regression Check

**Files:**
- All WeCom test files

### Step 8.1: Run full WeCom test suite

- [ ] **Run all tests**

```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py tests/gateway/test_wecom_accounts.py tests/gateway/test_wecom_webhook.py tests/gateway/test_wecom_command_auth.py tests/gateway/test_wecom_media.py tests/gateway/test_wecom_stream_store.py -v
```

Expected: all tests `PASSED`

### Step 8.2: Commit any remaining changes

- [ ] **Commit**

```bash
git add -A
git commit -m "test(wecom): full regression for transport-layer alignment" || true
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Command authorization module | Task 1 |
| Shared media helpers | Task 2 |
| Webhook StreamStore | Task 3 |
| Agent callback media upload/send | Task 4 |
| Agent callback `agent_id` validation | Task 4 |
| Non-blocking WS stream sends | Task 5 |
| Command auth in WS `_on_message` | Task 6 |
| StreamStore in webhook callback | Task 6 |
| `MediaPreparer` integration in `wecom.py` | Task 7 |
| Full regression | Task 8 |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-14-wecom-transport-align.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Batch execute in this session using `superpowers:executing-plans`.

**Which approach?**
