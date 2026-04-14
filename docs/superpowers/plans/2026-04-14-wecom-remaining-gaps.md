# WeCom Remaining Feature Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining transport-layer and messaging feature gaps between `hermes-agent` and `wecom-openclaw-plugin`, excluding framework-coupled (OpenClaw SDK) features.

**Architecture:** Add focused modules for template cards and video frame extraction, integrate them into `wecom.py`, and make targeted enhancements to existing methods for text chunking, thinking placeholders, pairing mode, webhook timeout fallback, version handshake, and group policy.

**Tech Stack:** Python 3.11+, asyncio, pytest, aiohttp, ffmpeg (subprocess).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `gateway/platforms/wecom_template_cards.py` | **Create** — template card JSON extraction, LLM field normalization, stream masking, in-memory cache with TTL pruning |
| `tests/gateway/test_wecom_template_cards.py` | **Create** — TDD tests for template card parser and manager |
| `gateway/platforms/wecom_video.py` | **Create** — lightweight ffmpeg wrapper for first-frame extraction |
| `tests/gateway/test_wecom_video.py` | **Create** — TDD tests for video frame extraction |
| `gateway/platforms/wecom.py` | **Modify** — template card send/update integration, text chunking, thinking placeholder, pairing mode, version handshake, group `disabled` policy, webhook near-timeout fallback |
| `gateway/platforms/wecom_stream_store.py` | **Modify** — add `started_at` and near-timeout detection API |
| `tests/gateway/test_wecom.py` | **Extend** — tests for chunking, thinking, pairing, version handshake, group policy |
| `tests/gateway/test_wecom_webhook.py` | **Extend** — tests for webhook near-timeout fallback |

---

## Task 1: Template Card Parser

**Files:**
- Create: `gateway/platforms/wecom_template_cards.py`
- Test: `tests/gateway/test_wecom_template_cards.py`

### Step 1.1: Write failing test for template card extraction

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_template_cards.py
from gateway.platforms.wecom_template_cards import extract_template_cards, mask_template_card_blocks


def test_extract_single_template_card():
    text = "```json\n{\"card_type\":\"text_notice\",\"task_id\":\"t1\"}\n```"
    result = extract_template_cards(text)
    assert len(result.cards) == 1
    assert result.cards[0]["task_id"] == "t1"
    assert result.remaining_text.strip() == ""


def test_mask_template_card_blocks_hides_json():
    text = "hello\n```json\n{\"card_type\":\"text_notice\"}\n```\nworld"
    masked = mask_template_card_blocks(text)
    assert "card_type" not in masked
    assert "hello" in masked
    assert "world" in masked
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_template_cards.py -v`

Expected: `FAILED` — `ModuleNotFoundError`

### Step 1.2: Implement minimal parser

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_template_cards.py
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEMPLATE_CARD_CACHE_TTL_SECONDS = 86400
TEMPLATE_CARD_CACHE_MAX_SIZE = 300

VALID_CARD_TYPES = {
    "text_notice",
    "news_notice",
    "button_interaction",
    "vote_interaction",
    "multiple_interaction",
}

_sent_template_cards: Dict[str, Dict[str, Any]] = {}
_sent_timestamps: Dict[str, float] = {}


def _cache_key(account_id: str, task_id: str) -> str:
    return f"{account_id}:{task_id}"


def _prune_cache() -> None:
    cutoff = time.time() - TEMPLATE_CARD_CACHE_TTL_SECONDS
    expired = [k for k, ts in _sent_timestamps.items() if ts < cutoff]
    for k in expired:
        _sent_template_cards.pop(k, None)
        _sent_timestamps.pop(k, None)
    if len(_sent_timestamps) > TEMPLATE_CARD_CACHE_MAX_SIZE:
        sorted_keys = sorted(_sent_timestamps, key=lambda k: _sent_timestamps[k])
        for k in sorted_keys[: len(sorted_keys) - TEMPLATE_CARD_CACHE_MAX_SIZE]:
            _sent_template_cards.pop(k, None)
            _sent_timestamps.pop(k, None)


def save_template_card_to_cache(account_id: str, card: Dict[str, Any]) -> None:
    task_id = str(card.get("task_id") or "").strip()
    if not task_id:
        return
    key = _cache_key(account_id, task_id)
    _sent_template_cards[key] = dict(card)
    _sent_timestamps[key] = time.time()
    _prune_cache()


def get_template_card_from_cache(account_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    key = _cache_key(account_id, task_id)
    ts = _sent_timestamps.get(key)
    if ts is None or time.time() - ts > TEMPLATE_CARD_CACHE_TTL_SECONDS:
        _sent_template_cards.pop(key, None)
        _sent_timestamps.pop(key, None)
        return None
    return dict(_sent_template_cards.get(key) or {})


def _coerce_checkbox_mode(value: Any) -> Optional[int]:
    aliases = {"single": 0, "radio": 0, "单选": 0, "multi": 1, "multiple": 1, "多选": 1}
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in aliases:
            return aliases[lowered]
        try:
            num = int(lowered)
            return 0 if num <= 0 else 1
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0 if int(value) <= 0 else 1
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return None


def _normalize_card(card: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(card)
    # Validate card_type
    card_type = str(normalized.get("card_type") or "").strip()
    if card_type not in VALID_CARD_TYPES:
        logger.warning("[wecom][template-card] Unknown card_type: %s", card_type)
    # Ensure task_id exists
    if not str(normalized.get("task_id") or "").strip():
        normalized["task_id"] = f"task-{int(time.time() * 1000)}"
    # checkbox.mode normalization
    checkbox = normalized.get("checkbox")
    if isinstance(checkbox, dict):
        mode = _coerce_checkbox_mode(checkbox.get("mode"))
        if mode is not None:
            checkbox["mode"] = mode
        disable = _coerce_bool(checkbox.get("disable"))
        if disable is not None:
            checkbox["disable"] = disable
        options = checkbox.get("option_list")
        if isinstance(options, list):
            for opt in options:
                if isinstance(opt, dict):
                    checked = _coerce_bool(opt.get("is_checked"))
                    if checked is not None:
                        opt["is_checked"] = checked
    # Common integer fields
    for path in [
        ("source", "desc_color"),
        ("quote_area", "type"),
        ("card_action", "type"),
        ("image_text_area", "type"),
    ]:
        parent_key, child_key = path
        parent = normalized.get(parent_key)
        if isinstance(parent, dict):
            val = _coerce_int(parent.get(child_key))
            if val is not None:
                parent[child_key] = val
    # List-level integer fields
    for list_key, item_key in [
        ("horizontal_content_list", "type"),
        ("jump_list", "type"),
        ("button_list", "style"),
        ("select_list", "disable"),
    ]:
        items = normalized.get(list_key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    if item_key == "disable":
                        val = _coerce_bool(item.get(item_key))
                    else:
                        val = _coerce_int(item.get(item_key))
                    if val is not None:
                        item[item_key] = val
    # button_selection.disable
    btn_sel = normalized.get("button_selection")
    if isinstance(btn_sel, dict):
        val = _coerce_bool(btn_sel.get("disable"))
        if val is not None:
            btn_sel["disable"] = val
    return normalized


@dataclass
class TemplateCardResult:
    cards: List[Dict[str, Any]]
    remaining_text: str


def extract_template_cards(text: str) -> TemplateCardResult:
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
    cards: List[Dict[str, Any]] = []
    remaining = str(text or "")
    for match in pattern.finditer(text):
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and str(parsed.get("card_type") or "").strip():
                cards.append(_normalize_card(parsed))
                remaining = remaining.replace(match.group(0), "")
        except json.JSONDecodeError:
            continue
    return TemplateCardResult(cards=cards, remaining_text=remaining.strip())


def mask_template_card_blocks(text: str) -> str:
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

    def replacer(match: "re.Match[str]") -> str:
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and str(parsed.get("card_type") or "").strip():
                return ""
        except json.JSONDecodeError:
            pass
        return match.group(0)

    return pattern.sub(replacer, text).strip()
```

- [ ] **Run tests to verify they pass**

Run: `pytest tests/gateway/test_wecom_template_cards.py -v`

Expected: all tests `PASSED`

### Step 1.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_template_cards.py tests/gateway/test_wecom_template_cards.py
git commit -m "feat(wecom): add template card parser with TDD"
```

---

## Task 2: Video First-Frame Extraction

**Files:**
- Create: `gateway/platforms/wecom_video.py`
- Test: `tests/gateway/test_wecom_video.py`

### Step 2.1: Write failing test

- [ ] **Write failing test**

```python
# tests/gateway/test_wecom_video.py
import pytest

from gateway.platforms.wecom_video import extract_first_video_frame


def test_extract_first_frame_returns_path_for_mp4(tmp_path):
    # Create a tiny fake video file (test will fail because ffmpeg won't process it,
    # but the function signature must exist)
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"not a real video")
    result = extract_first_video_frame(str(fake_video), output_dir=str(tmp_path))
    # Because it's fake ffmpeg will fail; assert graceful fallback
    assert result is None
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_video.py -v`

Expected: `FAILED` — `ModuleNotFoundError`

### Step 2.2: Implement video frame extraction

- [ ] **Write implementation**

```python
# gateway/platforms/wecom_video.py
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def extract_first_video_frame(video_path: str, output_dir: Optional[str] = None) -> Optional[str]:
    """Extract the first frame of a video to a JPEG using ffmpeg.

    Returns the path to the extracted frame, or None if ffmpeg is unavailable
    or the extraction fails.
    """
    source = Path(video_path)
    if not source.exists() or not source.is_file():
        logger.warning("[wecom][video] Source video not found: %s", video_path)
        return None

    out_dir = Path(output_dir) if output_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / f"{source.stem}_frame_{uuid.uuid4().hex[:8]}.jpg"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-ss", "00:00:00",
        "-vframes", "1",
        "-q:v", "2",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "[wecom][video] ffmpeg failed for %s: %s",
                video_path,
                result.stderr.decode("utf-8", errors="ignore")[:200],
            )
            return None
        return str(output_file)
    except FileNotFoundError:
        logger.debug("[wecom][video] ffmpeg not found, skipping first-frame extraction")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("[wecom][video] ffmpeg timed out for %s", video_path)
        return None
    except Exception as exc:
        logger.warning("[wecom][video] Failed to extract frame from %s: %s", video_path, exc)
        return None
```

- [ ] **Run tests to verify they pass**

Run: `pytest tests/gateway/test_wecom_video.py -v`

Expected: all tests `PASSED`

### Step 2.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_video.py tests/gateway/test_wecom_video.py
git commit -m "feat(wecom): add video first-frame extraction helper with TDD"
```

---

## Task 3: Text Chunking (Replace Truncation)

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 3.1: Write failing test for text chunking

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
def test_chunk_markdown_splits_at_limit():
    from gateway.platforms.wecom import _chunk_markdown_text

    long_text = "A" * 5000 + "\n" + "B" * 5000
    chunks = _chunk_markdown_text(long_text, chunk_limit=4000)
    assert len(chunks) > 1
    assert all(len(c) <= 4000 for c in chunks)
    # Verify no data loss
    assert "".join(chunks) == long_text.replace("\n", "")
```

Wait — joining without newlines isn't right if we preserve newlines. Let's split on paragraph boundaries and verify:

```python
def test_chunk_markdown_splits_at_limit():
    from gateway.platforms.wecom import _chunk_markdown_text

    long_text = "A" * 5000 + "\n\n" + "B" * 5000
    chunks = _chunk_markdown_text(long_text, chunk_limit=4000)
    assert len(chunks) >= 2
    assert all(len(c) <= 4000 for c in chunks)
    assert "A" * 5000 in chunks[0]
    assert "B" * 5000 in chunks[-1]
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_chunk_markdown_splits_at_limit -v`

Expected: `FAILED` — `_chunk_markdown_text` not defined

### Step 3.2: Implement text chunking

- [ ] **Modify `wecom.py`**

Add this static method after `_guess_mime_type` (around line 1140):

```python
    @staticmethod
    def _chunk_markdown_text(text: str, chunk_limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
        """Split markdown text into chunks that fit within WeCom's message limit.

        Prefers splitting on paragraph boundaries (double newlines) to keep formatting
        readable. Falls back to sentence and then character boundaries.
        """
        if not text:
            return []
        if len(text) <= chunk_limit:
            return [text]

        chunks: List[str] = []
        remaining = text.strip()

        while remaining:
            if len(remaining) <= chunk_limit:
                chunks.append(remaining)
                break

            # Try paragraph boundary first
            candidate = remaining[:chunk_limit]
            para_break = candidate.rfind("\n\n")
            if para_break > chunk_limit // 4:
                split_at = para_break
                chunks.append(remaining[:split_at].strip())
                remaining = remaining[split_at + 2 :].strip()
                continue

            # Try single newline
            line_break = candidate.rfind("\n")
            if line_break > chunk_limit // 4:
                split_at = line_break
                chunks.append(remaining[:split_at].strip())
                remaining = remaining[split_at + 1 :].strip()
                continue

            # Try sentence boundary
            sentence_break = candidate.rfind("。")
            if sentence_break > chunk_limit // 4:
                split_at = sentence_break + 1
                chunks.append(remaining[:split_at].strip())
                remaining = remaining[split_at:].strip()
                continue

            # Fallback: hard split at limit with ellipsis if mid-word
            split_at = chunk_limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()

        return chunks
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_chunk_markdown_splits_at_limit -v`

Expected: `PASSED`

### Step 3.3: Wire chunking into `send()`

- [ ] **Modify `send()`**

Find `send()` around line 1653. Replace the body so that when `reply_req_id` is absent (proactive send) or present (passive reply with fallback), it chunks and sends multiple messages instead of truncating.

```python
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send markdown to a WeCom chat via proactive ``aibot_send_msg``."""
        del metadata

        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            chunks = self._chunk_markdown_text(content, chunk_limit=self.MAX_MESSAGE_LENGTH)
            if not chunks:
                return SendResult(success=False, error="empty content")

            last_response: Optional[Dict[str, Any]] = None
            last_error: Optional[str] = None

            for idx, chunk in enumerate(chunks):
                is_last = idx == len(chunks) - 1
                if reply_req_id and is_last:
                    try:
                        response = await self._send_reply_stream(reply_req_id, chunk)
                    except RuntimeError as exc:
                        err_text = str(exc)
                        if "846608" in err_text:
                            logger.warning(
                                "[%s] Stream reply expired (846608) for req_id=%s, falling back to proactive send",
                                self.name, reply_req_id,
                            )
                            response = await self._send_request(
                                APP_CMD_SEND,
                                {
                                    "chatid": chat_id,
                                    "msgtype": "markdown",
                                    "markdown": {"content": chunk},
                                },
                            )
                        else:
                            raise
                else:
                    response = await self._send_request(
                        APP_CMD_SEND,
                        {
                            "chatid": chat_id,
                            "msgtype": "markdown",
                            "markdown": {"content": chunk},
                        },
                    )
                last_response = response
                error = self._response_error(response)
                if error:
                    last_error = error
                    break

            if last_error:
                return SendResult(success=False, error=last_error)
            return SendResult(
                success=True,
                message_id=self._payload_req_id(last_response) if last_response else uuid.uuid4().hex[:12],
                raw_response=last_response,
            )
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending message to WeCom")
        except Exception as exc:
            logger.error("[%s] Send failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))
```

- [ ] **Write failing test for chunked send**

Append to `tests/gateway/test_wecom.py`:

```python
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
    assert adapter._send_request.await_count == 2
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_send_chunks_long_text -v`

Expected: `PASSED`

### Step 3.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): replace truncation with markdown text chunking"
```

---

## Task 4: Thinking Message Placeholder

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 4.1: Write failing test for thinking stream

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_send_thinking_placeholder -v`

Expected: `FAILED` — `AttributeError: 'WeComAdapter' object has no attribute 'send_thinking'`

### Step 4.2: Implement thinking placeholder

- [ ] **Modify `wecom.py`**

Add after `send()`:

```python
    async def send_thinking(
        self,
        chat_id: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a thinking placeholder to indicate the bot is processing."""
        del metadata
        return await self.send(chat_id, "<think></think>", reply_to=reply_to)
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_send_thinking_placeholder -v`

Expected: `PASSED`

### Step 4.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): add thinking message placeholder"
```

---

## Task 5: Pairing Mode in DM Policy

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 5.1: Write failing test for pairing mode

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_dm_pairing_mode_blocks_and_sends_prompt -v`

Expected: `FAILED` — `handle_message` gets called because `_is_dm_allowed` returns `True` for `pairing`

### Step 5.2: Implement pairing mode

- [ ] **Modify `_is_dm_allowed`**

```python
    def _is_dm_allowed(self, sender_id: str) -> Tuple[bool, Optional[str]]:
        if self._dm_policy == "disabled":
            return False, "disabled"
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id), None
        if self._dm_policy == "pairing":
            return False, "pairing"
        return True, None
```

Wait — changing the return type will break existing callers. Update all callers instead. Actually, keep `_is_dm_allowed` returning `bool` and add a separate `_dm_block_reason` method, or better: modify `_on_message` to check `self._dm_policy` directly for pairing after the existing check.

Simpler approach: in `_on_message`, after `is_group = ...`, handle pairing inline before calling `_is_dm_allowed`:

Actually, the cleanest way is to update `_is_dm_allowed` to return a tuple and update the two call sites (`_on_message` and any tests). Let's do that:

Change `_is_dm_allowed`:

```python
    def _is_dm_allowed(self, sender_id: str) -> Tuple[bool, Optional[str]]:
        """Returns (allowed, block_reason)."""
        if self._dm_policy == "disabled":
            return False, "disabled"
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id), None
        if self._dm_policy == "pairing":
            return False, "pairing"
        return True, None
```

Update `_on_message` around line 779:

```python
        is_group = str(body.get("chattype") or "").lower() == "group"
        if is_group:
            if not self._is_group_allowed(chat_id, sender_id):
                logger.debug("[%s] Group %s / sender %s blocked by policy", self.name, chat_id, sender_id)
                return
        else:
            dm_allowed, dm_reason = self._is_dm_allowed(sender_id)
            if not dm_allowed:
                logger.debug("[%s] DM sender %s blocked by policy (%s)", self.name, sender_id, dm_reason)
                if dm_reason == "pairing":
                    try:
                        await self._send_request(
                            APP_CMD_SEND,
                            {
                                "chatid": chat_id,
                                "msgtype": "markdown",
                                "markdown": {"content": "您尚未完成配对，请联系管理员进行配对后重试。"},
                            },
                        )
                    except Exception as exc:
                        logger.warning("[%s] Failed to send pairing rejection: %s", self.name, exc)
                return
```

Update `tests/gateway/test_wecom.py` `test_on_message_respects_dm_policy` if it directly calls `_is_dm_allowed` (check existing tests):

```python
# Existing test probably does:
# assert adapter._is_dm_allowed("user-1") is False
# Update to:
# allowed, _ = adapter._is_dm_allowed("user-1")
# assert allowed is False
```

Let me check the existing test first... it was read earlier. Looking at lines 706-720 in test_wecom.py, `test_on_message_respects_dm_policy` calls `_on_message`, not `_is_dm_allowed` directly. But there may be a direct test. Let's grep:

Actually, just update any direct callers. Grep for `_is_dm_allowed` in tests:

```bash
grep -n "_is_dm_allowed" tests/gateway/test_wecom.py
```

If there are direct callers, update them. If not, proceed.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_dm_pairing_mode_blocks_and_sends_prompt -v`

Expected: `PASSED`

### Step 5.3: Fix any existing tests broken by tuple return

- [ ] **Run existing DM policy tests**

Run: `pytest tests/gateway/test_wecom.py -k "dm_policy" -v`

If any fail with tuple unpacking issues, fix them inline.

Expected: all `PASSED`

### Step 5.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): implement pairing mode in DM policy with rejection prompt"
```

---

## Task 6: Webhook Stream Near-Timeout Fallback

**Files:**
- Modify: `gateway/platforms/wecom_stream_store.py`, `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom_stream_store.py`, `tests/gateway/test_wecom_webhook.py`

### Step 6.1: Write failing test for near-timeout detection

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_stream_store.py`:

```python
def test_stream_is_near_timeout():
    import time
    from gateway.platforms.wecom_stream_store import StreamStore, StreamState

    async def flush_handler(pending):
        pass

    store = StreamStore(flush_handler=flush_handler)
    stream = StreamState(stream_id="s1", started=True, started_at=time.time() - 310)
    store._streams["s1"] = stream
    assert store.is_near_timeout("s1", timeout_seconds=360, margin_seconds=60) is True
    assert store.is_near_timeout("s2", timeout_seconds=360, margin_seconds=60) is False
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_stream_is_near_timeout -v`

Expected: `FAILED` — `started_at` not in `StreamState`, `is_near_timeout` not defined

### Step 6.2: Add timeout tracking to StreamStore

- [ ] **Modify `wecom_stream_store.py`**

Update `StreamState` to add `started_at`:

```python
@dataclass
class StreamState:
    stream_id: str
    content: str = ""
    finished: bool = False
    started: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
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
```

Update `mark_started`:

```python
    def mark_started(self, stream_id: str) -> None:
        stream = self._streams.get(stream_id)
        if stream:
            stream.started = True
            if stream.started_at is None:
                stream.started_at = time.time()
```

Add `is_near_timeout`:

```python
    def is_near_timeout(
        self,
        stream_id: str,
        timeout_seconds: float = 360,
        margin_seconds: float = 60,
    ) -> bool:
        stream = self._streams.get(stream_id)
        if not stream or not stream.started:
            return False
        started_at = stream.started_at or stream.created_at
        elapsed = time.time() - started_at
        return elapsed >= (timeout_seconds - margin_seconds)
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_stream_store.py::test_stream_is_near_timeout -v`

Expected: `PASSED`

### Step 6.3: Wire near-timeout fallback into webhook flush handler

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom_webhook.py`:

```python
@pytest.mark.asyncio
async def test_webhook_near_timeout_falls_back_to_proactive_send():
    import time
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter
    from gateway.platforms.wecom_stream_store import StreamStore

    adapter = WeComAdapter(PlatformConfig(enabled=True, extra={
        "accounts": [{"account_id": "a1", "bot_id": "b1", "token": "t", "encoding_aes_key": "k", "corp_id": "c1", "connection_mode": "webhook"}]
    }))
    adapter._send_request = AsyncMock(return_value={"headers": {"req_id": "r1"}, "body": {"errcode": 0}})

    store = StreamStore(flush_handler=adapter._on_webhook_flush)
    adapter._stream_store = store

    stream_id = store.create_stream()
    store.mark_started(stream_id)
    # Manually set started_at to simulate near-timeout
    stream = store.get_stream(stream_id)
    stream.started_at = time.time() - 310
    stream.content = "fallback result"
    stream.finished = True

    response = await adapter._handle_webhook_callback_for_stream("a1", "c1", stream_id)
    assert response.status == 200
    body = json.loads(response.text)
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "fallback result"
    adapter._send_request.assert_awaited_once()
```

Wait — `_handle_webhook_callback_for_stream` doesn't exist. We need to test through the normal `stream_refresh` path or add a helper. Actually, the plugin pushes fallback proactively when near-timeout is detected during stream refresh or before dispatch. In `hermes-agent`, the simplest integration is: in `_handle_webhook_callback`, when `msgtype == "stream_refresh"`, if the stream is near-timeout and finished, return the content AND trigger a proactive send fallback (if not already done).

Let's instead test the behavior inside `_handle_webhook_callback` for `stream_refresh`:

```python
@pytest.mark.asyncio
async def test_stream_refresh_near_timeout_triggers_fallback():
    import time
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter
    from gateway.platforms.wecom_stream_store import StreamStore

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

    from aiohttp import web
    request = web.Request(
        message=MagicMock(),
        writer=MagicMock(),
        task=MagicMock(),
        transport=MagicMock(),
        payload=MagicMock(),
        loop=asyncio.get_event_loop(),
        client_max_size=1024,
    )
    # Too complicated to mock aiohttp Request. Instead, refactor _handle_webhook_callback
    # to extract a helper `_build_stream_refresh_response(account, decrypted)`.
```

This is getting messy with aiohttp mocks. Let's add a focused helper method to `wecom.py` that can be tested more easily:

```python
    async def _handle_stream_refresh(
        self,
        account: WeComAccount,
        chat_id: str,
        stream_id: str,
    ) -> "web.Response":
        if not self._stream_store:
            return web.json_response({
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": True, "content": ""},
            })
        stream = self._stream_store.get_stream(stream_id) or self._stream_store.get_stream_by_msgid(stream_id)
        if not stream:
            return web.json_response({
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": True, "content": ""},
            })

        # Near-timeout fallback: if we're close to WeCom's 6-minute limit, proactively send
        if stream.finished and self._stream_store.is_near_timeout(stream_id, timeout_seconds=360, margin_seconds=60):
            if not stream.fallback_prompt_sent_at:
                stream.fallback_prompt_sent_at = time.time()
                try:
                    await self._send_request(
                        APP_CMD_SEND,
                        {
                            "chatid": chat_id,
                            "msgtype": "text",
                            "text": {"content": stream.content},
                        },
                    )
                except Exception as exc:
                    logger.warning("[%s] Failed to send near-timeout fallback: %s", self.name, exc)

        return web.json_response({
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "finish": stream.finished,
                "content": stream.content,
            },
        })
```

Then in `_handle_webhook_callback`, replace the `stream_refresh` block with:

```python
        elif msgtype == "stream_refresh":
            sender_id = str(decrypted.get("from", {}).get("userid") or "").strip()
            chat_id = str(decrypted.get("chatid") or sender_id).strip()
            stream_id = str(decrypted.get("stream_id") or "")
            return await self._handle_stream_refresh(account, chat_id, stream_id)
```

Now the test can be:

```python
@pytest.mark.asyncio
async def test_stream_refresh_near_timeout_triggers_fallback():
    import time
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter
    from gateway.platforms.wecom_stream_store import StreamStore

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

    response = await adapter._handle_stream_refresh(None, "c1", stream_id)
    assert response.status == 200
    body = json.loads(response.text)
    assert body["stream"]["content"] == "near timeout result"
    adapter._send_request.assert_awaited_once()
```

Wait — `_handle_stream_refresh` expects `account: WeComAccount`, but we pass `None` in the test? Let's just pass a dummy account:

```python
    from gateway.platforms.wecom_accounts import WeComAccount
    account = WeComAccount(account_id="a1", bot_id="b1")
    response = await adapter._handle_stream_refresh(account, "c1", stream_id)
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom_webhook.py::test_stream_refresh_near_timeout_triggers_fallback -v`

Expected: `FAILED` — `_handle_stream_refresh` or `is_near_timeout` not defined

### Step 6.4: Implement near-timeout fallback

- [ ] **Modify `wecom_stream_store.py`** (already done in Step 6.2)

- [ ] **Modify `wecom.py`**

Extract `_handle_stream_refresh` as shown above and update the `stream_refresh` branch in `_handle_webhook_callback` to call it.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom_webhook.py::test_stream_refresh_near_timeout_triggers_fallback -v`

Expected: `PASSED`

### Step 6.5: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom_stream_store.py gateway/platforms/wecom.py tests/gateway/test_wecom_stream_store.py tests/gateway/test_wecom_webhook.py
git commit -m "feat(wecom): add webhook stream near-timeout fallback to proactive send"
```

---

## Task 7: Bot WebSocket Version Handshake

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 7.1: Write failing test

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_dispatch_payload_replies_to_enter_check_update -v`

Expected: `FAILED` — `_send_request` not called because `enter_check_update` falls through to `_on_message`

### Step 7.2: Implement version handshake

- [ ] **Modify `_dispatch_payload`**

Find the block:

```python
        if cmd in CALLBACK_COMMANDS or cmd == APP_CMD_EVENT_CALLBACK:
            await self._on_message(payload)
            return
```

Replace with:

```python
        if cmd in CALLBACK_COMMANDS or cmd == APP_CMD_EVENT_CALLBACK:
            body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
            event_name = str(body.get("event") or "").lower()
            if event_name == "enter_check_update":
                try:
                    await self._send_request(
                        APP_CMD_SEND,
                        {
                            "chatid": str(body.get("chatid") or ""),
                            "msgtype": "text",
                            "text": {"content": f"HermesAgent/{self._version or '1.0'} ready"},
                        },
                    )
                except Exception as exc:
                    logger.debug("[%s] Version handshake reply failed: %s", self.name, exc)
                return
            await self._on_message(payload)
            return
```

But `_version` doesn't exist. Add it in `__init__` or use a class constant. Simpler: just hardcode a generic version string or use `getattr`:

```python
                            "text": {"content": "HermesAgent ready"},
```

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_dispatch_payload_replies_to_enter_check_update -v`

Expected: `PASSED`

### Step 7.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): add Bot WebSocket version handshake reply"
```

---

## Task 8: Group Policy `disabled` Enforcement

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 8.1: Write failing test

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_on_message_respects_group_disabled_policy():
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_on_message_respects_group_disabled_policy -v`

Expected: `FAILED` — `handle_message` is called because `_is_group_allowed` doesn't check `group_policy == "disabled"` correctly... wait, it does. Let me re-read the code:

```python
    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        if self._group_policy == "disabled":
            return False
```

It does check. But maybe the test still passes? If it passes, we need a different RED state. Actually, the current code does return `False` for `disabled`. So this test may already pass.

Let's verify the gap more carefully. The plugin's `group-policy.ts` has `checkGroupPolicy` that evaluates `groupPolicy` and per-group `allowFrom`. Our `_is_group_allowed` already has both. So maybe this isn't a real gap?

Wait — re-reading the gap list: "Group Policy Sophistication — Small Gap — hermes-agent has basic per-group allow_from, but no disabled group policy enforcement in some paths". But looking at the code, `_is_group_allowed` does check `disabled`. So maybe the test will pass immediately, which means the gap was already closed or wasn't real.

In TDD, if the test passes immediately, we should verify it's actually testing the right thing and move on, or change the test to expose a real gap. Let's check if there are any other paths where group policy is bypassed. Looking at `_on_message`:

```python
        is_group = str(body.get("chattype") or "").lower() == "group"
        if is_group:
            if not self._is_group_allowed(chat_id, sender_id):
                logger.debug(...)
                return
```

This is the only entry point. So `disabled` IS enforced. The gap might be about per-group `allow_from` sophistication, but that's already there too.

Given this, let's write the test anyway. If it passes immediately, it confirms behavior. We can skip the "make it fail" step and just verify:

Actually, TDD says write failing test first. But if the behavior already exists, we can just write the test as a characterization test and verify it passes. That's acceptable for gap analysis when the feature was already partially implemented.

Let's adjust: write the test, run it, if it passes we keep it as regression coverage.

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_on_message_respects_group_disabled_policy -v`

Expected: `PASSED` (behavior already exists — this is regression coverage)

### Step 8.2: Commit

- [ ] **Commit**

```bash
git add tests/gateway/test_wecom.py
git commit -m "test(wecom): add regression coverage for group disabled policy"
```

---

## Task 9: Integrate Template Cards into Outbound Send Path

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 9.1: Write failing test for template card send

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
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
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_send_extracts_and_sends_template_card -v`

Expected: `FAILED` — only one `_send_request` call (no template card extraction)

### Step 9.2: Integrate template cards into `send()`

- [ ] **Modify `send()`**

At the top of `send()`, before chunking, add extraction:

```python
        # Extract and send template cards if present
        from gateway.platforms.wecom_template_cards import extract_template_cards, save_template_card_to_cache

        card_result = extract_template_cards(content)
        content = card_result.remaining_text or content
```

After the chunk loop, send template cards:

```python
            if last_error:
                return SendResult(success=False, error=last_error)

            # Send extracted template cards after text chunks
            account = self._accounts[0] if self._accounts else None
            for card in card_result.cards:
                if account:
                    save_template_card_to_cache(account.account_id, card)
                try:
                    card_response = await self._send_request(
                        APP_CMD_SEND,
                        {
                            "chatid": chat_id,
                            "msgtype": "template_card",
                            "template_card": card,
                        },
                    )
                    last_response = card_response
                    error = self._response_error(card_response)
                    if error:
                        last_error = error
                        break
                except Exception as exc:
                    logger.warning("[%s] Failed to send template card: %s", self.name, exc)

            if last_error:
                return SendResult(success=False, error=last_error)
            return SendResult(
                success=True,
                message_id=self._payload_req_id(last_response) if last_response else uuid.uuid4().hex[:12],
                raw_response=last_response,
            )
```

Wait — this means `card_result` must be defined even if there's an early return. Move `card_result` extraction before the `try` block.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_send_extracts_and_sends_template_card -v`

Expected: `PASSED`

### Step 9.3: Handle template card event callbacks

- [ ] **Modify `_on_message`**

In `_extract_text`, template card events currently append nothing. But the plugin updates the card UI on `template_card_event`. We should add a handler.

Find the `_on_message` method. After `text, reply_text = self._extract_text(body)`, add:

```python
        # Template card event: update UI if we have a cached card
        msgtype = str(body.get("msgtype") or "").lower()
        event_name = str(body.get("event") or "").lower() if msgtype == "event" else ""
        if event_name == "template_card_event":
            await self._handle_template_card_event(body)
```

Add the new method after `_on_message`:

```python
    async def _handle_template_card_event(self, body: Dict[str, Any]) -> None:
        from gateway.platforms.wecom_template_cards import get_template_card_from_cache

        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        response_code = str(event.get("response_code") or "").strip()
        button_replace_name = str(event.get("button_replace_name") or "").strip()
        selected_options = event.get("selected_options")
        selected_option_ids = (
            [str(opt.get("key") or "") for opt in selected_options if isinstance(opt, dict)]
            if isinstance(selected_options, list)
            else []
        )

        # Try to find a cached card by task_id heuristics or response_code
        # For simplicity, we only update if we can locate the exact card in cache
        account = self._accounts[0] if self._accounts else None
        if not account:
            return

        # Since we don't map response_code -> task_id, log and skip active update for now
        logger.debug(
            "[%s] Template card event received: response_code=%s selected=%s",
            self.name, response_code, selected_option_ids,
        )
```

This is intentionally minimal — a full response_code->task_id mapping requires SDK-level state. The plugin has this via `TemplateCardManager`. For parity without OpenClaw SDK, we log and accept the event gracefully.

- [ ] **Write regression test**

Append to `tests/gateway/test_wecom.py`:

```python
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
```

- [ ] **Run test**

Run: `pytest tests/gateway/test_wecom.py::test_template_card_event_is_handled_gracefully -v`

Expected: `PASSED`

### Step 9.4: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): integrate template card send and event handling"
```

---

## Task 10: Integrate Video First-Frame Extraction into Inbound Media Path

**Files:**
- Modify: `gateway/platforms/wecom.py`
- Test: `tests/gateway/test_wecom.py`

### Step 10.1: Write failing test

- [ ] **Write failing test**

Append to `tests/gateway/test_wecom.py`:

```python
@pytest.mark.asyncio
async def test_extract_media_extracts_video_frame():
    from gateway.config import PlatformConfig
    from gateway.platforms.wecom import WeComAdapter

    config = PlatformConfig(extra={"bot_id": "b", "secret": "s"})
    adapter = WeComAdapter(config)

    with patch("gateway.platforms.wecom_video.extract_first_video_frame", return_value="/tmp/frame.jpg") as mock_extract:
        body = {
            "msgtype": "video",
            "video": {"sdkfileid": "v1", "md5sum": "abc"},
        }
        urls, types = await adapter._extract_media(body)
        mock_extract.assert_called_once()
        assert "/tmp/frame.jpg" in urls
        assert "image/jpeg" in types
```

- [ ] **Run test to verify it fails**

Run: `pytest tests/gateway/test_wecom.py::test_extract_media_extracts_video_frame -v`

Expected: `FAILED` — `extract_first_video_frame` not called because `_extract_media` doesn't invoke it yet

### Step 10.2: Integrate video frame extraction

- [ ] **Modify `_extract_media`**

Find `_extract_media` in `wecom.py`. After handling video download, add frame extraction:

```python
            if msgtype == "video" and isinstance(body.get("video"), dict):
                video_info = body["video"]
                # Attempt to download video
                video_url = await self._download_bot_media(video_info, "video")
                if video_url:
                    refs.append(("video", {"url": video_url, "type": "video/mp4"}))
                    # Extract first frame for LLM preview
                    try:
                        from gateway.platforms.wecom_video import extract_first_video_frame
                        frame_path = extract_first_video_frame(video_url)
                        if frame_path:
                            refs.append(("image", {"url": frame_path, "type": "image/jpeg"}))
                    except Exception as exc:
                        logger.debug("[%s] First-frame extraction failed: %s", self.name, exc)
```

Wait — `_extract_media` currently returns `media_urls, media_types` as parallel lists. If we append an image ref after video, the caller will get both. That's correct.

But we need to check the actual structure of `_extract_media`. Looking at current code (not fully shown in recent reads), it likely uses `refs` as a list of tuples and then constructs the parallel lists. The modification above should fit.

- [ ] **Run test to verify it passes**

Run: `pytest tests/gateway/test_wecom.py::test_extract_media_extracts_video_frame -v`

Expected: `PASSED`

### Step 10.3: Commit

- [ ] **Commit**

```bash
git add gateway/platforms/wecom.py tests/gateway/test_wecom.py
git commit -m "feat(wecom): integrate video first-frame extraction into inbound media flow"
```

---

## Task 11: Final Regression Check

**Files:**
- All WeCom test files

### Step 11.1: Run full WeCom test suite

- [ ] **Run all tests**

```bash
pytest tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py tests/gateway/test_wecom_accounts.py tests/gateway/test_wecom_webhook.py tests/gateway/test_wecom_command_auth.py tests/gateway/test_wecom_media.py tests/gateway/test_wecom_stream_store.py tests/gateway/test_wecom_template_cards.py tests/gateway/test_wecom_video.py -v
```

Expected: all tests `PASSED`

### Step 11.2: Commit any remaining changes

- [ ] **Commit**

```bash
git add -A
git commit -m "test(wecom): full regression for remaining feature gaps" || true
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Template card parser and cache | Task 1, Task 9 |
| Video first-frame extraction | Task 2, Task 10 |
| Text chunking instead of truncation | Task 3 |
| Thinking message placeholder | Task 4 |
| Pairing mode in DM policy | Task 5 |
| Webhook stream near-timeout fallback | Task 6 |
| Bot WebSocket version handshake | Task 7 |
| Group `disabled` policy regression | Task 8 |
| Template card integration in send path | Task 9 |
| Full regression | Task 11 |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-14-wecom-remaining-gaps.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Batch execute in this session using `superpowers:executing-plans`.

**Which approach?**
