# WeCom Transport-Layer Feature Alignment Design

**Goal:** Align `hermes-agent`'s WeCom Python connectors with transport-layer features from `wecom-openclaw-plugin`, without requiring OpenClaw SDK or framework changes.

**Scope:** Transport layer only. No gateway runner modifications, no MCP interceptors, no dynamic routing hooks.

---

## 1. Architecture Overview

Add three focused modules and make targeted modifications to two existing files.

| Module/File | Responsibility |
|-------------|----------------|
| `gateway/platforms/wecom_command_auth.py` | Config-driven command gating with prompt generation |
| `gateway/platforms/wecom_stream_store.py` | In-memory `StreamStore` for webhook mode: debounce timers, batches, ack streams, TTL pruning |
| `gateway/platforms/wecom_media.py` | Lightweight shared media helpers (`MediaPreparer`, magic-byte detection, size downgrade rules) |
| `gateway/platforms/wecom_callback.py` | **Modify** — add agent REST media upload/send and override media send methods |
| `gateway/platforms/wecom.py` | **Modify** — integrate `StreamStore` for webhook, add non-blocking stream sends, wire command auth |

---

## 2. Component: `wecom_command_auth.py`

A stateless config inspector. Given `raw_body`, `sender_user_id`, and account config, decides whether the message is an unauthorized command.

### Behavior

- **Command detection:** body starts with `/` and is not just a URL.
- **Authorized if:**
  - `dm_policy` is `open` or `disabled`, AND no `authorizer` is configured
  - OR sender is in `allow_from` (with wildcard `*` support)
  - OR sender matches authorizer rules (if configured)
- **Blocked if:** `dm_policy` is `allowlist`, this is a command, and sender is not authorized.

### Interface

```python
@dataclass
class CommandAuthResult:
    command_authorized: bool
    should_compute_auth: bool
    dm_policy: str
    sender_allowed: bool
    authorizer_configured: bool

def resolve_command_auth(
    account: WeComAccount,
    raw_body: str,
    sender_user_id: str,
) -> CommandAuthResult: ...

def build_unauthorized_command_prompt(
    sender_user_id: str,
    dm_policy: str,
    scope: str = "bot",
) -> str: ...
```

### Default behavior

When no authorizer is configured and `dm_policy` is `open`, commands are **allowed** by default to avoid breaking changes.

---

## 3. Component: `wecom_stream_store.py`

Self-contained in-memory state machine for Bot Webhook mode. Inspired by the TS plugin's `streamStore` but simplified for Python asyncio.

### Key abstractions

- `StreamState` dataclass:
  - `stream_id`, `content`, `finished`, `started`, `created_at`
  - `user_id`, `chat_type`, `chat_id`, `aibotid`, `task_key`
  - `images: List[Dict[str, str]]` (base64 + md5)
  - `fallback_mode`, `fallback_prompt_sent_at`, `dm_content`, `agent_media_keys`
- `PendingBatch` dataclass:
  - `conversation_key`, `stream_id`, `contents: List[str]`, `msgids: List[str]`, `timer: Optional[asyncio.Task]`

### StreamStore public API

```python
class StreamStore:
    def __init__(self, flush_handler: Callable[[PendingInbound], Awaitable[None]], ttl_seconds: float = 600):
        ...

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
        """Returns (stream_id, status). Status: active_new | queued_new | active_merged | queued_merged."""
        ...

    def create_stream(self, msgid: Optional[str] = None) -> str: ...
    def get_stream(self, stream_id: str) -> Optional[StreamState]: ...
    def update_stream(self, stream_id: str, updater: Callable[[StreamState], None]) -> None: ...
    def mark_started(self, stream_id: str) -> None: ...
    def mark_finished(self, stream_id: str) -> None: ...
    def get_stream_by_msgid(self, msgid: str) -> Optional[str]: ...
    def set_stream_id_for_msgid(self, msgid: str, stream_id: str) -> None: ...

    def add_ack_stream_for_batch(self, batch_stream_id: str, ack_stream_id: str) -> None: ...
    def drain_ack_streams_for_batch(self, batch_stream_id: str) -> List[str]: ...

    def start_pruning(self, interval_ms: float) -> None: ...
    def stop_pruning(self) -> None: ...
```

### Thread safety

All public methods use an `asyncio.Lock` to protect internal dicts.

### Webhook flow

1. Inbound message -> `add_pending_message` -> creates/merges batch, returns stream status.
2. Debounce timer fires -> `flush_handler` receives `PendingInbound` with merged contents.
3. Flush handler builds `MessageEvent` and calls `adapter.handle_message()`.
4. Stream refresh polls `get_stream` for current content/finish flag.
5. Agent output updates stream via `update_stream`.
6. Final reply pushed via `response_url` POST (managed by caller, not `StreamStore`).

---

## 4. Component: `wecom_media.py`

Lightweight shared media preparation extracted from existing `wecom.py` inline logic. Avoids duplicating magic-byte detection and size rules between Bot WS and Agent callback adapters.

### Public API

```python
IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES

def detect_mime_from_bytes(data: bytes) -> Optional[str]: ...
def detect_wecom_media_type(content_type: str) -> str: ...
def apply_file_size_limits(
    file_size: int,
    detected_type: str,
    content_type: Optional[str] = None,
) -> Dict[str, Any]: ...

class MediaPreparer:
    def __init__(self, http_client, media_local_roots: Optional[List[str]] = None): ...
    async def prepare(self, media_source: str, file_name: Optional[str] = None) -> Dict[str, Any]: ...
```

### Behavior

- Remote download: respects `ABSOLUTE_MAX_BYTES`, SSRF-safe via `tools.url_safety.is_safe_url`.
- Local files: enforce `media_local_roots` whitelist if configured.
- MIME priority: magic bytes > explicit content-type > filename guess.
- Downgrade rules: image >10MB -> file, video >10MB -> file, voice non-AMR -> file, anything >20MB -> reject.

---

## 5. Component: Agent Callback Media

`WecomCallbackAdapter` currently only sends text via `message/send`. Extend it to support full media outbound.

### New methods

```python
async def _upload_media_to_agent_api(
    self,
    account: WeComAccount,
    data: bytes,
    media_type: str,
    filename: str,
) -> str: ...

async def _send_media_message(
    self,
    account: WeComAccount,
    chat_id: str,
    media_type: str,
    media_id: str,
) -> Dict[str, Any]: ...

async def _send_media_source(
    self,
    chat_id: str,
    media_source: str,
    caption: Optional[str] = None,
    file_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> SendResult: ...
```

### Overrides

- `send_image_file` -> `_send_media_source`
- `send_document` -> `_send_media_source`
- `send_voice` -> `_send_media_source`
- `send_video` -> `_send_media_source`

### Agent ID validation

In `_handle_callback`, after decrypting XML, extract `AgentID` and compare against `account.agent_id`. Log a warning if mismatch.

---

## 6. Component: Non-Blocking WS Stream Sends

In `WeComAdapter`, add a concurrency guard to prevent stream frame backlog when server acks are slow.

### State

```python
self._pending_stream_acks: Set[str] = set()
```

### New method

```python
async def _send_reply_stream_nonblocking(
    self,
    reply_req_id: str,
    stream_id: str,
    content: str,
    finish: bool = False,
) -> str:
    """Returns stream_id or 'skipped'."""
```

### Behavior

- If `not finish` and `stream_id in _pending_stream_acks`, return `"skipped"`.
- Otherwise send the frame via `_send_reply_request` and add `stream_id` to pending set.
- `_dispatch_payload` clears the ack when a response with matching `req_id` and `errcode in (0, None)` arrives.
- `finish=True` is **never skipped**.

---

## 7. Integration in `wecom.py`

### Webhook handlers

Update `_handle_webhook_callback` to use `StreamStore`:

- `msgtype == "text" / "image" / "file" / "voice" / "video" / "mixed"` -> `stream_store.add_pending_message`
- `msgtype == "stream_refresh"` -> look up stream and return encrypted JSON with current content
- `msgtype == "event"` with `enter_chat` -> return welcome text immediately
- `msgtype == "event"` with `template_card_event` -> create synthetic message and dispatch via stream store

### Command auth

In `_on_message` and webhook flush handler, before building the `MessageEvent`:

```python
auth = resolve_command_auth(account, raw_body, sender_id)
if auth.should_compute_auth and not auth.command_authorized:
    # Send rejection prompt and skip agent dispatch
```

For WS mode, send the prompt via `_send_request` (markdown) or passive reply if available.
For webhook mode, set the stream content to the rejection prompt and finish.

### Non-blocking streams

Wherever intermediate stream frames are sent (if any existing code sends them), use `_send_reply_stream_nonblocking`. The current `wecom.py` only sends final stream frames in `send()`, so this is primarily for future integration or if the adapter gains explicit stream-update paths.

---

## 8. Error Handling and Edge Cases

| Scenario | Handling |
|----------|----------|
| Stream store pruning hits active stream | Only prune streams older than TTL (10 min) and `finished=True` |
| Command auth with missing authorizer | Default to allowing commands (non-breaking) |
| Agent media upload fails | Return `SendResult(success=False, error=...)` |
| Non-blocking skip on `finish=True` | Never skip; always send final frame |
| Stream store concurrent access | `asyncio.Lock` around all mutating operations |
| Webhook debounce timer race | Cancel existing timer before creating new one |

---

## 9. Testing Plan

| Test File | Coverage |
|-----------|----------|
| `tests/gateway/test_wecom_command_auth.py` | Allow/block commands, wildcard matching, prompt generation |
| `tests/gateway/test_wecom_stream_store.py` | Create/update streams, batch merge, debounce flush, TTL prune, ack streams |
| `tests/gateway/test_wecom_media.py` | Magic bytes, size downgrade, local path whitelist, remote download |
| `tests/gateway/test_wecom_callback.py` (extend) | Media upload mock, send image/document/voice/video, agent_id validation |
| `tests/gateway/test_wecom.py` (extend) | Non-blocking stream skip, stream expiry fallback still works, command auth integration, webhook stream store flow |

---

## 10. Files to Create/Modify

### Create
- `gateway/platforms/wecom_command_auth.py`
- `gateway/platforms/wecom_stream_store.py`
- `gateway/platforms/wecom_media.py`
- `tests/gateway/test_wecom_command_auth.py`
- `tests/gateway/test_wecom_stream_store.py`
- `tests/gateway/test_wecom_media.py`

### Modify
- `gateway/platforms/wecom.py`
- `gateway/platforms/wecom_callback.py`
- `tests/gateway/test_wecom.py`
- `tests/gateway/test_wecom_callback.py`
