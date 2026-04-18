# Hermes WeCom Connector vs. WeCom OpenClaw Plugin — Gap Analysis

> Generated: 2026-04-18

## 1. Architecture & Scope

| Aspect | Hermes WeCom Connector | WeCom OpenClaw Plugin |
|--------|------------------------|----------------------|
| **Runtime** | Python 3.11+ gateway service (`gateway/platforms/wecom/`) | TypeScript VS Code extension plugin (`src/`) |
| **Framework** | Standalone AI agent gateway (Hermes) | OpenClaw plugin SDK (`openclaw/plugin-sdk`) |
| **SDK dependency** | Direct HTTP/WebSocket implementation, no WeCom SDK | Official `@wecom/aibot-node-sdk` (v1.0.6) |
| **Multi-platform** | Yes — one gateway serves Discord, Slack, Telegram, WeCom, etc. | No — WeCom-only channel plugin |
| **Hosting** | Self-hosted daemon/server process | Runs inside VS Code / OpenClaw IDE |

Hermes is a general-purpose gateway; the OpenClaw plugin is a framework-specific channel integration.

---

## 2. Transport & Connection

### 2.1 WebSocket Connection

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **WS client** | Custom `WeComWSClient` (`client.py`, 58 lines) | Official `@wecom/aibot-node-sdk` `WSClient` |
| **Auth handshake** | Manual — sends `aibot_subscribe` with `bot_id` + `secret` | SDK handles auth |
| **Connection retry** | Exponential backoff `[2, 5, 10, 30, 60]` seconds | SDK handles reconnection (`WS_MAX_RECONNECT_ATTEMPTS`) |
| **Auth failure limit** | Not explicitly limited | `WS_MAX_AUTH_FAILURE_ATTEMPTS` |
| **MCP discovery ordering** | **Fixed** — MCP discovery runs AFTER listen/heartbeat tasks to avoid 90s startup stall | SDK handles internally |

### 2.2 Reliability (Defense in Depth)

| Layer | Hermes | OpenClaw |
|-------|--------|----------|
| **TCP keepalive** | `SO_KEEPALIVE=1`, idle=30s, interval=10s, count=3 (macOS fallback) | Not directly visible (SDK-level) |
| **Application heartbeat** | Ping every 30s, tracks missed pongs | SDK-level (`WS_HEARTBEAT_INTERVAL_MS`) |
| **Missed-pong detection** | ✅ Custom `WeComWSClient` counts missed pongs, force-closes after 2 misses | SDK-level |
| **Connection watchdog** | ✅ Forces reconnect if no frame for 45s | Not visible in plugin code |
| **Read timeout** | 90s on `ws.receive()` (`HEARTBEAT_INTERVAL * 3`) | SDK-level |
| **Kicked detection** | ✅ `disconnected_event` stops reconnect attempts | Not visible |

**Gap:** Hermes has detailed, layered reliability monitoring visible in application code. OpenClaw delegates most of this to the SDK.

### 2.3 Dual Transport

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Mixed mode** | ✅ One adapter instance supports both WS and webhook simultaneously | Yes — fallback Bot WS → Agent HTTP API |
| **Webhook server** | Custom FastAPI routes (`adapter.py:532-645`) | `webhook/index.ts` gateway |
| **Proactive send fallback** | ✅ `aibot_send_msg` when passive reply stream expires (846608) | `sendWeComMessage()` falls back to Agent HTTP API |
| **Stream expiry handling** | ✅ Near-timeout (6-min limit) proactive full-text send | ✅ `StreamExpiredError`, retry proactive |

### 2.4 Reply Serialization

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Per-req_id queue** | ✅ `WeComReplyQueue` — serializes outbound per req_id | `chat-queue.ts` (59 lines) — basic queue |
| **Worker tasks** | ✅ Per req_id gets its own `asyncio.Task` | Not visible |
| **Queue backpressure** | ✅ Max 500 items, `RuntimeError("queue full")` | Not visible |
| **Idle cleanup** | ✅ Workers exit after 1s of empty queue | Not visible |
| **Timeout per item** | ✅ Configurable (default 15s) | Not visible |

**Gap:** Hermes has explicit per-req_id reply serialization with backpressure and timeouts. OpenClaw delegates to SDK or uses simpler queues.

---

## 3. Message Handling

### 3.1 Inbound Message Parsing

| Type | Hermes | OpenClaw |
|------|--------|----------|
| text | ✅ Plain text + quote/reply extraction | ✅ With quote content extraction |
| markdown | ✅ Stripped to text | ✅ Treated as text |
| image | ✅ URL or base64, cached | ✅ URL/base64, SDK download + cache |
| file (PDF/Word/Excel) | ✅ `appmsg.file` handling | ✅ File download |
| voice | ✅ Speech-to-text extraction | ✅ Voice handling |
| video | ✅ URL pass-through + **first frame extraction** (ffmpeg) | ✅ Video + optional ffmpeg first-frame |
| mixed (text+image) | ✅ Multi-part | ✅ Multi-part |
| appmsg | ✅ Attachment title extraction | ✅ Message parsing |
| location | ❌ Not supported | ✅ Agent XML mode only |
| link | ❌ Not supported | ✅ Agent XML mode only |
| stream_refresh | ✅ Webhook stream polling | ✅ Stream state tracking |
| event | ✅ `template_card_event`, `enter_check_update`, `disconnected_event` | ✅ Event handling |
| text preview | ❌ Not supported | ✅ Heuristic detection up to 12KB |

**Gap:** OpenClaw supports **location** and **link** messages in Agent XML mode, which Hermes does not. OpenClaw also has **text file preview** (heuristic up to 12KB) for text files.

### 3.2 Text Processing

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Message dedup** | `MessageDeduplicator` (TTL 300s, max 2000) | Not visible |
| **Text batch aggregation** | `TextBatchAggregator` (0.6s batch, 2s split, 3900-char WeCom client splits) | Not visible (SDK-level) |
| **Reply req_id mapping** | Maps `message_id` → `req_id` (max 1000 entries) | `reqid-store.ts` (138 lines) |

### 3.3 Outbound Text

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Chunking** | ✅ Smart 4000-char split: paragraph → line → sentence → hard | Not visible (SDK-level or not chunked) |
| **Markdown support** | ✅ Chunked markdown with smart boundaries | ✅ Markdown supported |
| **Stream replies** | ✅ `msgtype: "stream"` with `finish` flag | ✅ `replyStream()` with `finish` |
| **Thinking placeholder** | ✅ `<think></think>` with `finish=False` | ✅ `THINKING_MESSAGE` constant |
| **Typing indicator persistence** | ✅ Reopens stream after intermediate responses (agent still working) | Not visible in code |
| **Non-blocking sends** | ✅ `_send_reply_stream_nonblocking()` skips if prior frame pending ack | ✅ `sendWeComReplyNonBlocking()` |
| **Stream ID reuse** | ✅ Final response reuses typing stream_id | ✅ `MessageState.streamId` tracking |

**Gap:** Hermes has sophisticated **smart text chunking** (4000-char boundaries) and **typing persistence across intermediate responses**. OpenClaw relies on SDK or simpler approaches.

---

## 4. Media Handling

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Inbound download** | `_download_remote_bytes()` with SSRF protection (`tools.url_safety.is_safe_url`) | `wsClient.downloadFile()` with timeout fallback to `fetchRemoteMedia()` |
| **AES decryption** | ✅ `_decrypt_file_bytes()` with 43-char unpadded base64 key normalization (PKCS#7) | SDK `downloadFile(url, aesKey)` |
| **Magic-byte detection** | ✅ PNG, JPEG, GIF, WEBP, BMP, PDF, OGG, WAV, MP3, MP4/MOV (`adapter.py:1632-1687`) | `file-type` npm package |
| **Caching** | `cache_image_from_bytes()`, `cache_document_from_bytes()` | `core.channel.media.saveMediaBuffer()` |
| **Video first-frame** | ✅ FFmpeg subprocess (30s timeout) | ✅ Optional ffmpeg extraction |
| **Text file preview** | ❌ Not supported | ✅ Heuristic detection up to 12KB |
| **Outbound upload protocol** | ✅ 3-step: init/chunk/finish (max 100 chunks, 512KB each) | SDK `uploadMedia()` |
| **Size-based downgrade** | ✅ Image >10MB → file, video >10MB → file, voice not AMR → file, voice >2MB → file | SDK-level |
| **Media local roots whitelist** | ✅ `media_local_roots` configuration | ✅ `mediaLocalRoots` configuration |
| **Magic-byte MIME detection** | Internal implementation (`media.py`) | `file-type` npm package |

**Gap:** Hermes has a more explicit media upload protocol implementation. OpenClaw delegates to the SDK for upload and download.

---

## 5. Authentication & Authorization

### 5.1 Authentication

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Bot WS auth** | `aibot_subscribe` with `bot_id` + `secret` | SDK handles auth |
| **Agent REST auth** | `callback.py` token refresh (7200s TTL) | `agent/api-client.ts` (Agent HTTP API) |
| **Webhook verification** | SHA1 + AES decryption of `echostr` | `webhook/index.ts` |
| **Callback crypto** | `WXBizMsgCrypt` (AES-CBC + SHA1 signature) | Not visible (SDK or not used) |
| **Multi-account signature matching** | ✅ Tries all webhook accounts, warns on conflicts | Not visible |

### 5.2 Authorization Policies

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **DM policy types** | `open`, `allowlist`, `disabled`, `pairing` | Same (via `openclaw-compat.ts`) |
| **Group policy types** | `open`, `allowlist`, `disabled` + per-group `allow_from` | Same |
| **Command authorization** | ✅ `/reset` etc. blocked for non-allowlisted senders | Not visible (framework-level) |
| **Access control** | Per-account per-policy enforcement | Per-account per-policy enforcement |

---

## 6. Template Cards / Rich Messages

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Extraction** | ✅ From `\`\`\`json` code blocks, validated `card_type` | ✅ Same approach |
| **Valid types** | `text_notice`, `news_notice`, `button_interaction`, `vote_interaction`, `multiple_interaction` | Same |
| **Normalization** | ✅ Coerces checkbox modes, booleans, integers | ✅ Coercion and validation |
| **Cache** | ✅ In-memory TTL (86400s, max 300 entries) keyed by `account_id:task_id` | Not visible |
| **Send path** | After text chunks, raw JSON removed from displayed text | Same |
| **Event handling** | ✅ `template_card_event` — logged but minimal UI update | ✅ `updateTemplateCardOnEvent()` — **updates card UI** |
| **Masking in streams** | ✅ `mask_template_card_blocks()` strips card JSON from text | ✅ Same |

**Gap:** OpenClaw has **template card event updates** that actually modify the card UI. Hermes only logs card events without updating the displayed card.

---

## 7. MCP Integration

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Dynamic discovery** | ✅ After WS connect, queries 6 categories (`contact`, `meeting`, `todo`, `schedule`, `doc`, `msg`) | ✅ Via WSClient dynamic pull |
| **Runtime access** | `get_mcp_configs()` returns `{category: url}` map | Via framework |
| **Gateway registration** | `gateway/run.py` registers WeCom MCP configs dynamically | Via framework |
| **Transport** | HTTP/WebSocket | Streamable HTTP (JSON-RPC) with session lifecycle |
| **Stateless detection** | Not visible | ✅ Auto-detects servers without `Mcp-Session-Id` |
| **Interceptors** | Not visible | ✅ biz-error, msg-media, smartpage-create, smartpage-export |
| **Schema cleaning** | Not visible | ✅ Removes `$ref` for Gemini compatibility |
| **Auto-rebuild on 404** | Not visible | ✅ Auto-rebuild session, retry once |
| **Built-in skills** | Via gateway toolsets | ✅ 15 built-in (contact, doc, todo, meeting, schedule, messaging, smartsheet, template cards, media, preflight) |

---

## 8. State Management

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Typing stream state** | `Dict[str, Tuple[str, str]]` — `chat_id` → `(reply_req_id, stream_id)` | `MessageState` with `streamId` tracking |
| **Pending close streams** | `_streams_pending_close` — orphaned streams from `pause_typing_for_chat` | Not visible |
| **In-flight tracking** | `_reply_req_ids_sending_response` — blocks typing during HTTP in-flight | Not visible |
| **Message state per chat** | Not explicit (gateway-level) | ✅ `state-manager.ts` with TTL cleanup |
| **ReqId store** | In-memory mapping | ✅ `reqid-store.ts` with warmup/flush |
| **Stream expiry flag** | Handled per-send | ✅ `MessageState.streamExpired` |

---

## 9. Error Handling

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Stream expiry (846608)** | ✅ Falls back to proactive `aibot_send_msg` | ✅ `StreamExpiredError`, proactive retry |
| **Response error parsing** | `_response_error()` parses `errcode`/`errmsg` | SDK-level |
| **Send-level retry** | Relies on gateway-level retry | SDK-level |
| **Connection retry** | Exponential backoff with max attempts | SDK-level |
| **HTTPS URL fallback** | ✅ If media upload fails, falls back to text+URL | Not visible |
| **Media error summary** | Not visible | ✅ `buildMediaErrorSummary()` replaces thinking with error text |

**Gap:** OpenClaw has **media error summaries** that replace the thinking placeholder with user-friendly error text. Hermes does not appear to have this UX feature.

---

## 10. Multi-Account

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Account resolution** | `resolve_wecom_accounts()` — single/multi/env fallbacks | `resolveWeComAccountMulti()` |
| **Deep config inheritance** | ✅ Top-level deep-merged into per-account settings | Not visible |
| **Default account** | Not explicit | ✅ `resolveDefaultWeComAccountId()` with prioritized logic |
| **Env fallbacks** | `WECOM_BOT_ID`, `WECOM_SECRET`, `WECOM_WEBSOCKET_URL` | Not visible (framework handles) |
| **Account dataclass** | `WeComAccount` with `is_bot_configured`, `is_agent_configured` | `ResolvedWeComAccount` with similar properties |

---

## 11. Testing

| Aspect | Hermes | OpenClaw |
|--------|--------|----------|
| **Test framework** | pytest | vitest |
| **Test files** | 12 files, 163 tests | Not visible (tests not in repo) |
| **Mock server** | ✅ `mock_server.py` — full aiohttp WeCom mock | Not visible |
| **E2E tests** | ✅ Mock server E2E for multi-turn typing, reconnect | Not visible |
| **Reliability tests** | TCP keepalive, watchdog, reconnect, MCP ordering | Not visible |
| **Unit coverage** | Accounts, auth, crypto, media, stream store, cards, video, webhook, adapter, callback, reliability, MCP | Not visible |

**Gap:** Hermes has extensive test coverage including a full mock WeCom server and E2E reliability tests. OpenClaw test status is not visible in the repo.

---

## 12. OpenClaw-Specific Features (Not in Hermes)

| Feature | Description | File |
|---------|-------------|------|
| **Onboarding wizard** | Setup wizard for WeCom channel configuration | `onboarding.ts` |
| **Dynamic routing** | Route messages to different agents based on rules | `dynamic-routing.ts` |
| **Dynamic agent** | Dynamic agent creation/handling | `dynamic-agent.ts` |
| **Version handshake** | Version check and update handshake with WeCom server | `EVENT_ENTER_CHECK_UPDATE` |
| **OpenClaw framework integration** | Full plugin SDK integration (channels, agents, routing) | Throughout |
| **Media uploader** | `uploadAndSendMedia()` with sendMedia/sendFile | `media-uploader.ts` |
| **Plugin versioning** | `PLUGIN_VERSION` in handshake | `version.ts` |
| **Scene identification** | `SCENE_WECOM_OPENCLAW` for WeCom-specific flows | `const.ts` |
| **Built-in skills** | 15 WeCom-specific skills (contact, doc, todo, meeting, schedule, messaging, smartsheet, template cards, media, preflight) | `skills/` |
| **Egress proxy** | `network.egressProxyUrl` → env vars for proxy chain | `onboarding.ts` |
| **Anti-kick protection** | Suppresses auto-restart after server kick to prevent mutual kick loops | `monitor.ts` |
| **Version checking** | `enter_check_update` handshake on connect | `monitor.ts` |
| **Cron support** | Cron job integration for Agent outbound | Not visible in source |

---

## 13. Hermes-Specific Features (Not in OpenClaw)

| Feature | Description | File |
|---------|-------------|------|
| **Self-built app callback adapter** | Full XML callback adapter for enterprise apps | `callback.py` |
| **Webhook stream store** | Debounced batching with near-timeout fallback | `stream_store.py` |
| **Command authorization** | Slash command blocking (`/reset`, etc.) | `command_auth.py` |
| **TCP keepalive tuning** | Platform-specific TCP keepalive (Linux/macOS) | `adapter.py` |
| **Application-level heartbeat** | Custom ping/pong tracking with missed-pong detection | `client.py`, `adapter.py` |
| **Connection watchdog** | Force reconnect on 45s idle | `adapter.py` |
| **Smart text chunking** | 4000-char split with paragraph/line/sentence boundaries | `adapter.py` |
| **Reply queue with backpressure** | Per-req_id serialization, max 500, idle cleanup | `reply_queue.py` |
| **Stream typing persistence** | Reopens stream after intermediate responses | `adapter.py` |
| **Webhook verification with multi-account** | Tries all accounts, warns on signature conflicts | `adapter.py` |
| **Webhook stream refresh polling** | Polling-based stream refresh from WeCom | `adapter.py` |
| **Gateway-level retry** | Exponential backoff with configurable intervals | `adapter.py` |

---

## 14. Summary of Key Gaps

### Hermes → OpenClaw (Hermes has these, OpenClaw doesn't)

1. **Self-built app callback adapter** — OpenClaw only supports Bot WS + Agent HTTP, not self-built app XML callbacks
2. **TCP keepalive + watchdog** — Reliability features hidden in SDK, not visible/configurable
3. **Reply queue with backpressure** — No per-req_id serialization visible
4. **Smart text chunking** — No 4000-char boundary-aware splitting visible
5. **Webhook stream store** — No debounced batching with near-timeout fallback
6. **Command authorization** — No slash-command blocking visible
7. **Webhook verification with multi-account** — No multi-account signature matching

### OpenClaw → Hermes (OpenClaw has these, Hermes doesn't)

1. **Template card event updates** — Hermes only logs, doesn't update card UI
2. **Media error summaries** — No user-friendly error text replacement for failed media sends
3. **Onboarding wizard** — No setup wizard for configuration
4. **Dynamic routing** — No rule-based agent routing
5. **Dynamic agent** — No auto-isolated agents per user/group
6. **Version handshake** — No `enter_check_update` version protocol
7. **Plugin ecosystem** — Not portable as a plugin; is a standalone service
8. **Built-in skills** — 15 built-in WeCom skills (contact, doc, todo, meeting, etc.)
9. **Egress proxy chain** — No `network.egressProxyUrl` support
10. **Anti-kick loop protection** — No explicit protection against mutual kick loops
11. **Cron job support** — No built-in cron for Agent outbound
12. **Location/link messages** — Agent XML location and link not supported
13. **Text file preview** — No heuristic text preview up to 12KB

### Parity (Both have similar implementations)

- WebSocket + Webhook dual transport
- Stream expiry (846608) handling
- Media upload/download with AES decryption
- Template card extraction and masking
- Multi-account configuration
- DM/group access control policies
- Non-blocking stream sends
- Thinking/typing indicators
