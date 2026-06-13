"""
WeCom (Enterprise WeChat) platform adapter.

Uses the WeCom AI Bot WebSocket gateway for inbound and outbound messages.
The adapter focuses on the core gateway path:

- authenticate via ``aibot_subscribe``
- receive inbound ``aibot_msg_callback`` events
- send outbound markdown messages via ``aibot_send_msg``
- upload outbound media via ``aibot_upload_media_*`` and send native attachments
- best-effort download of inbound image/file attachments for agent context

Configuration in config.yaml:
    platforms:
      wecom:
        enabled: true
        extra:
          bot_id: "your-bot-id"          # or WECOM_BOT_ID env var
          secret: "your-secret"          # or WECOM_SECRET env var
          websocket_url: "wss://openws.work.weixin.qq.com"
          dm_policy: "open"              # open | allowlist | disabled | pairing
          allow_from: ["user_id_1"]
          group_policy: "open"           # open | allowlist | disabled
          group_allow_from: ["group_id_1"]
          groups:
            group_id_1:
              allow_from: ["user_id_1"]
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from gateway.platforms.wecom.media import MediaPreparer
from gateway.platforms.wecom.reqid_store import ReqIdStore

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from aibot import WSClient, WSClientOptions
    from aibot.types import WsCmd
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    WSClient = None  # type: ignore[assignment,misc]
    WSClientOptions = None  # type: ignore[assignment,misc]
    WsCmd = None  # type: ignore[assignment,misc]


def _ensure_wecom_sdk() -> bool:
    """Lazy-install wecom-aibot-python-sdk if missing.

    Calls ``tools.lazy_deps.ensure("platform.wecom")`` on first use.
    After a successful install, re-imports the SDK and flips
    ``_SDK_AVAILABLE`` to True.
    """
    global _SDK_AVAILABLE, WSClient, WSClientOptions, WsCmd
    if _SDK_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.wecom", prompt=False)
    except Exception:
        return False
    try:
        from aibot import WSClient as _WSClient, WSClientOptions as _WSClientOptions
        from aibot.types import WsCmd as _WsCmd
    except ImportError:
        return False
    WSClient = _WSClient
    WSClientOptions = _WSClientOptions
    WsCmd = _WsCmd
    _SDK_AVAILABLE = True
    return True

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator, TextBatchAggregator
from gateway.platforms.wecom.accounts import resolve_wecom_accounts, WeComAccount
from gateway.platforms.wecom.crypto import WXBizMsgCrypt, verify_signature
from gateway.platforms.wecom.command_auth import (
    resolve_command_auth,
    build_unauthorized_command_prompt,
)
from gateway.platforms.wecom.stream_store import StreamStore, PendingInbound
from .reply_queue import WeComReplyQueue
from .client import WeComWSClient
from .chat_queue import ChatSerialQueue
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_PING = "ping"
APP_CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
APP_CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
APP_CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"


CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
NON_RESPONSE_COMMANDS = CALLBACK_COMMANDS | {APP_CMD_EVENT_CALLBACK}

MAX_MESSAGE_LENGTH = 4000
CONNECT_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# TCP keepalive tuning (Linux) — detect dead proxies/NAT within ~60s
TCP_KEEPALIVE_ENABLED = True
TCP_KEEPALIVE_IDLE = 30   # seconds before first probe
TCP_KEEPALIVE_INTERVAL = 10  # seconds between probes
TCP_KEEPALIVE_COUNT = 3   # probes before declaring dead

WATCHDOG_TIMEOUT_SECONDS = 45.0  # must be > HEARTBEAT_INTERVAL_SECONDS (30s)
SDK_WATCHDOG_TIMEOUT_SECONDS = 90.0  # idle threshold for SDK-mode reconnect

MAX_MISSED_PONGS = 2  # force-close on the 3rd miss
REPLY_QUEUE_MAX_SIZE = 500
REPLY_QUEUE_IDLE_TIMEOUT = 1.0

STREAM_FINISH_CONTENT = "\u200b"  # zero-width space so WeCom sees visible text

DEDUP_MAX_SIZE = 1000

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
UPLOAD_CHUNK_SIZE = 512 * 1024
MAX_UPLOAD_CHUNKS = 100
VOICE_SUPPORTED_MIMES = {"audio/amr"}


class StreamExpiredError(RuntimeError):
    """Raised when WeCom returns errcode 846608 (stream/reply expired)."""


class MediaOversizeError(ValueError):
    """Raised when an inbound media file exceeds the size limit."""

    def __init__(self, filename: str = "", size_bytes: int = 0, max_bytes: int = 0) -> None:
        self.filename = filename
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File too large: {filename} ({size_bytes} bytes > {max_bytes} bytes limit)"
        )


def check_wecom_requirements() -> bool:
    """Check if WeCom runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """Coerce config values into a trimmed string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_entry(raw: str) -> str:
    """Normalize allowlist entries such as ``wecom:user:foo``."""
    value = str(raw).strip()
    value = re.sub(r"^wecom:", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(user|group):", "", value, flags=re.IGNORECASE)
    return value.strip()


def _entry_matches(entries: List[str], target: str) -> bool:
    """Case-insensitive allowlist match with ``*`` support."""
    normalized_target = str(target).strip().lower()
    for entry in entries:
        normalized = _normalize_entry(entry).lower()
        if normalized == "*" or normalized == normalized_target:
            return True
    return False


class WeComAdapter(BasePlatformAdapter):
    """WeCom AI Bot adapter backed by a persistent WebSocket connection."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # Threshold for detecting WeCom client-side message splits.
    # When a chunk is near the 4000-char limit, a continuation is almost certain.
    _SPLIT_THRESHOLD = 3900

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM)

        self._accounts: List[WeComAccount] = resolve_wecom_accounts(config)
        # Backwards-compat: expose default account attrs at top level for single-account users
        default_account = self._accounts[0] if self._accounts else WeComAccount(account_id="default")
        self._bot_id = default_account.bot_id
        self._secret = default_account.secret
        self._ws_url = default_account.websocket_url
        self._dm_policy = default_account.dm_policy
        self._allow_from = default_account.allow_from
        self._group_policy = default_account.group_policy
        self._group_allow_from = default_account.group_allow_from
        self._groups = default_account.groups

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_frame_at: float = 0.0
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
        self._reply_req_ids: Dict[str, str] = {}
        self._last_reply_req_id_per_chat: Dict[str, str] = {}
        self._device_id = uuid.uuid4().hex
        self._stream_acked: Dict[str, bool] = {}
        # Tracks req_ids currently in the process of sending a response via
        # _send_reply_stream. Prevents the _keep_typing loop from opening a
        # new typing stream while the HTTP request is in-flight, which would
        # create an orphan stream. Once the response is delivered, typing
        # is allowed to resume for the same message (e.g. multi-turn analysis).
        # Uses a refcount (dict) because send() and _send_reply_stream() may
        # both add the same req_id (nested calls); the flag must stay raised
        # until the outermost caller finishes.
        self._reply_req_ids_sending_response: Dict[str, int] = {}
        self._reply_queue = WeComReplyQueue(
            send_json_fn=self._send_json,
            pending_responses_dict=self._pending_responses,
            max_size=REPLY_QUEUE_MAX_SIZE,
            idle_timeout=REPLY_QUEUE_IDLE_TIMEOUT,
        )
        self._ws_client = WeComWSClient(
            adapter_name=self.name,
            max_missed_pongs=MAX_MISSED_PONGS,
        )
        self._kicked = False

        # SDK client (wecom-aibot-python-sdk)
        self._sdk_client: Optional[Any] = None
        self._last_inbound_frame: Optional[Dict[str, Any]] = None
        self._last_sdk_message_at: float = 0.0

        # Webhook server (for bot webhook mode accounts)
        self._webhook_runner: Optional["web.AppRunner"] = None
        self._webhook_site: Optional["web.TCPSite"] = None
        self._webhook_app: Optional["web.Application"] = None
        self._stream_store: Optional[StreamStore] = None
        self._reqid_store: Optional[ReqIdStore] = None
        self._reqid_flush_task: Optional[asyncio.Task] = None
        self._typing_stream_state_by_chat: Dict[str, Tuple[str, str]] = {}
        # Streams that need a finish=True frame (set by sync pause_typing_for_chat,
        # drained by async send_typing on the next _keep_typing iteration).
        self._streams_pending_close: Dict[str, Tuple[str, str]] = {}

        # Text batching via shared aggregator
        batch_delay = float(os.getenv("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", "0.6"))
        split_delay = float(os.getenv("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
        self._text_batcher = TextBatchAggregator(
            handler=self.handle_message,
            batch_delay=batch_delay,
            split_delay=split_delay,
            split_threshold=self._SPLIT_THRESHOLD,
        )
        self._chat_queue = ChatSerialQueue(self._handle_message_sync)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the WeCom AI Bot gateway.

        Supports multiple accounts with mixed transport modes:
        - websocket: opens a persistent WebSocket connection
        - webhook: registers an aiohttp route for encrypted JSON callbacks
        """
        self._kicked = False
        if not AIOHTTP_AVAILABLE:
            message = "WeCom startup failed: aiohttp not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "WeCom startup failed: httpx not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
            return False

        if not self._accounts:
            message = "WeCom startup failed: no accounts configured"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        ws_accounts = [a for a in self._accounts if a.connection_mode == "websocket"]
        wh_accounts = [a for a in self._accounts if a.connection_mode == "webhook"]
        connected_any = False
        errors: List[str] = []

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

            # ---- ReqId store ----
            reqid_path = Path(self.config.extra.get("state_dir", ".hermes/state")) / "wecom_reqids.json"
            self._reqid_store = ReqIdStore(reqid_path)
            # Hydrate in-memory cache from disk
            for chat_id, entry in self._reqid_store._data.items():
                self._last_reply_req_id_per_chat[chat_id] = entry["req_id"]

            # ---- WebSocket mode (Bot WS) ----
            if ws_accounts:
                # For backward compat we drive the primary WS from the first
                # websocket-mode account.
                primary = ws_accounts[0]
                if not primary.is_bot_configured:
                    if len(self._accounts) == 1:
                        message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
                    else:
                        message = (
                            f"WeCom startup failed: account '{primary.account_id}' "
                            "missing bot_id or secret"
                        )
                    self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
                    logger.warning("[%s] %s", self.name, message)
                    return False

                if not _SDK_AVAILABLE and not _ensure_wecom_sdk():
                    message = "WeCom startup failed: wecom-aibot-python-sdk not installed"
                    self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
                    logger.warning("[%s] %s. Run: pip install wecom-aibot-python-sdk", self.name, message)
                    return False

                self._bot_id = primary.bot_id
                self._secret = primary.secret
                self._ws_url = primary.websocket_url

                options = WSClientOptions(
                    bot_id=primary.bot_id,
                    secret=primary.secret,
                    ws_url=primary.websocket_url,
                    heartbeat_interval=20000,  # 20s instead of default 30s
                )
                self._sdk_client = WSClient(options)

                # Bridge SDK events (pyee AsyncIOEventEmitter uses .on(), not
                # attribute assignment, for event registration).
                self._sdk_client.on("message", self._on_sdk_message)

                def _on_sdk_connected() -> None:
                    self._last_sdk_message_at = asyncio.get_running_loop().time()
                    logger.info("[%s] Connected", self.name)

                def _on_sdk_authenticated() -> None:
                    self._last_sdk_message_at = asyncio.get_running_loop().time()
                    self._apply_sdk_tcp_keepalive()
                    logger.info("[%s] Authenticated", self.name)

                self._sdk_client.on("connected", _on_sdk_connected)
                self._sdk_client.on("authenticated", _on_sdk_authenticated)
                self._sdk_client.on("disconnected", lambda reason: logger.info("[%s] Disconnected: %s", self.name, reason))
                self._sdk_client.on("error", lambda err: logger.error("[%s] SDK error: %s", self.name, err))

                await self._sdk_client.connect()
                self._mark_connected()
                self._last_sdk_message_at = asyncio.get_running_loop().time()
                self._apply_sdk_tcp_keepalive()
                self._watchdog_task = asyncio.create_task(self._sdk_watchdog_loop())
                logger.info(
                    "[%s] SDK WebSocket connected for account '%s'",
                    self.name, primary.account_id,
                )
                connected_any = True
                if len(ws_accounts) > 1:
                    logger.info(
                        "[%s] Additional %d websocket account(s) configured but not yet connected",
                        self.name, len(ws_accounts) - 1,
                    )

            # ---- Webhook mode (Bot Webhook) ----
            if wh_accounts:
                for account in wh_accounts:
                    if not account.is_webhook_configured:
                        errors.append(
                            f"account '{account.account_id}' missing token or encoding_aes_key"
                        )
                        continue
                if errors and not connected_any:
                    message = f"WeCom startup failed: {', '.join(errors)}"
                    self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
                    logger.warning("[%s] %s", self.name, message)
                    return False
                await self._start_webhook_server(wh_accounts)
                connected_any = True

            if connected_any:
                if not self._running:
                    self._mark_connected()
                return True

            message = "WeCom startup failed: no accounts could be connected"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            # Cancel tasks that may have been started before the failure
            if self._listen_task:
                self._listen_task.cancel()
                try:
                    await self._listen_task
                except asyncio.CancelledError:
                    pass
                self._listen_task = None
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None
            if self._watchdog_task:
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                self._watchdog_task = None
            await self._cleanup_ws()
            await self._cleanup_webhook()
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from WeCom."""
        self._running = False
        self._mark_disconnected()

        if self._sdk_client:
            self._sdk_client.disconnect()
            self._sdk_client = None

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        self._fail_pending_responses(RuntimeError("WeCom adapter disconnected"))
        self._reply_queue.fail_all(RuntimeError("WeCom adapter disconnected"))
        self._kicked = False
        await self._cleanup_ws()
        await self._cleanup_webhook()

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        if self._reqid_flush_task and not self._reqid_flush_task.done():
            self._reqid_flush_task.cancel()
            self._reqid_flush_task = None
        if self._reqid_store:
            for chat_id, req_id in self._last_reply_req_id_per_chat.items():
                self._reqid_store.set(chat_id, req_id)
            self._reqid_store.save()

        self._dedup.clear()
        self._typing_stream_state_by_chat.clear()
        self._last_reply_req_id_per_chat.clear()
        self._reply_req_ids.clear()
        self._streams_pending_close.clear()
        self._reply_req_ids_sending_response.clear()
        logger.info("[%s] Disconnected", self.name)

    async def _cleanup_ws(self) -> None:
        """Close the live websocket/session, if any."""
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        self._ws = None

        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        self._session = None

    def _apply_tcp_keepalive(self) -> None:
        """Set TCP keepalive socket options on the active websocket."""
        import socket

        if not TCP_KEEPALIVE_ENABLED:
            return
        if not self._ws:
            return

        sock = self._ws.get_extra_info("socket")
        if sock is None:
            return

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT)
        except (OSError, AttributeError):
            # Platform may not support these options (macOS uses TCP_KEEPALIVE instead of TCP_KEEPIDLE)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # macOS uses TCP_KEEPALIVE (value 0x10) instead of TCP_KEEPIDLE
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), TCP_KEEPALIVE_IDLE)
            except (OSError, AttributeError):
                logger.debug("[%s] Could not apply TCP keepalive socket options", self.name)

    async def _start_webhook_server(self, accounts: List[WeComAccount]) -> None:
        """Start aiohttp server for bot webhook mode accounts."""
        self._stream_store = StreamStore(flush_handler=self._on_webhook_flush)
        self._stream_store.start_pruning(30000)
        self._webhook_app = web.Application()
        self._webhook_app.router.add_get("/health", self._handle_webhook_health)
        for account in accounts:
            path = f"/wecom/bot/{account.account_id}"
            self._webhook_app.router.add_get(path, self._handle_webhook_verify)
            self._webhook_app.router.add_post(path, self._handle_webhook_callback)
            logger.info("[%s] Registered webhook route %s for account '%s'", self.name, path, account.account_id)
        # Also register a catch-all default route
        self._webhook_app.router.add_get("/wecom/bot", self._handle_webhook_verify)
        self._webhook_app.router.add_post("/wecom/bot", self._handle_webhook_callback)
        self._webhook_runner = web.AppRunner(self._webhook_app)
        await self._webhook_runner.setup()
        host = str(self.config.extra.get("host") or "0.0.0.0")
        port = int(self.config.extra.get("port") or 8644)
        self._webhook_site = web.TCPSite(self._webhook_runner, host, port)
        await self._webhook_site.start()
        logger.info("[%s] Webhook server listening on %s:%s", self.name, host, port)

    async def _cleanup_webhook(self) -> None:
        """Close the webhook server, if any."""
        if self._stream_store:
            self._stream_store.stop_pruning()
            self._stream_store = None
        self._webhook_site = None
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
            self._webhook_runner = None
        self._webhook_app = None

    async def _on_webhook_flush(self, pending: PendingInbound) -> None:
        if not self._stream_store:
            return
        self._stream_store.mark_started(pending.stream_id)
        account = pending.target
        msg = pending.msg
        sender_id = str(msg.get("from", {}).get("userid") or "").strip()
        chat_id = str(msg.get("chatid") or sender_id).strip()
        raw_body = str(pending.contents[0]) if pending.contents else ""

        chat_type = str(msg.get("chattype") or "").lower()
        auth = resolve_command_auth(account, raw_body, sender_id, chat_id=chat_id, chat_type=chat_type)
        if auth.should_compute_auth and not auth.command_authorized:
            prompt = build_unauthorized_command_prompt(sender_id, auth.dm_policy)
            self._stream_store.update_stream(
                pending.stream_id,
                lambda s: setattr(s, "content", prompt) or setattr(s, "finished", True),
            )
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

    async def _handle_webhook_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": "wecom"})

    async def _handle_webhook_verify(self, request: "web.Request") -> "web.Response":
        """Bot Webhook URL verification (GET)."""
        signature = request.query.get("msg_signature") or request.query.get("msgsignature") or request.query.get("signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")

        if not signature or not timestamp or not nonce or not echostr:
            return web.Response(status=400, text="missing required query parameters")

        account = self._match_webhook_account(signature, timestamp, nonce, echostr)
        if account is None:
            return web.Response(status=403, text="signature verification failed")

        try:
            crypt = WXBizMsgCrypt(account.token, account.encoding_aes_key, account.receive_id or account.corp_id or "")
            plain = crypt.verify_url(signature, timestamp, nonce, echostr)
            return web.Response(text=plain, content_type="text/plain")
        except Exception as exc:
            logger.warning("[%s] Webhook verify failed for account '%s': %s", self.name, account.account_id, exc)
            return web.Response(status=403, text="decrypt failed")

    async def _handle_webhook_callback(self, request: "web.Request") -> "web.Response":
        """Bot Webhook JSON callback (POST)."""
        signature = request.query.get("msg_signature") or request.query.get("msgsignature") or request.query.get("signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")

        if not signature or not timestamp or not nonce:
            return web.Response(status=400, text="missing required query parameters")

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")

        encrypt = body.get("encrypt", "")
        if not encrypt:
            return web.Response(status=400, text="missing encrypt field")

        account = self._match_webhook_account(signature, timestamp, nonce, encrypt)
        if account is None:
            return web.Response(status=403, text="signature verification failed")

        try:
            decrypted = self._decrypt_webhook_body(account, encrypt)
        except Exception as exc:
            logger.warning("[%s] Webhook decrypt failed for account '%s': %s", self.name, account.account_id, exc)
            return web.Response(status=400, text="decrypt failed")

        # Validate aibotid if present
        aibotid = decrypted.get("aibotid")
        if aibotid and aibotid != account.bot_id:
            logger.warning(
                "[%s] Webhook aibotid mismatch: expected=%s, got=%s",
                self.name, account.bot_id, aibotid,
            )
            return web.Response(status=403, text="aibotid mismatch")

        msgtype = str(decrypted.get("msgtype") or "").lower()
        if msgtype in {"text", "image", "file", "voice", "video", "mixed"}:
            sender_id = str(decrypted.get("from", {}).get("userid") or "").strip()
            chat_id = str(decrypted.get("chatid") or sender_id).strip()
            content = ""
            text_block = decrypted.get("text") if isinstance(decrypted.get("text"), dict) else {}
            content = str(text_block.get("content") or "").strip()
            chat_type = str(decrypted.get("chattype") or "").lower()

            # Command authorization check (inline for immediate rejection)
            auth = resolve_command_auth(account, content, sender_id, chat_id=chat_id, chat_type=chat_type)
            if auth.should_compute_auth and not auth.command_authorized:
                prompt = build_unauthorized_command_prompt(sender_id, auth.dm_policy)
                return web.json_response({
                    "msgtype": "text",
                    "text": {"content": prompt},
                })

            # Capture response_url for proactive reply push
            response_url = decrypted.get("response_url")
            if response_url and self._stream_store:
                policy = decrypted.get("active_reply_policy", "once")
                await self._stream_store.active_reply_store.save(chat_id, response_url, policy=policy)
                logger.debug("[%s] Stored response_url for chat=%s policy=%s", self.name, chat_id, policy)

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

            # Fallback direct dispatch when stream store is not available
            payload = {
                "cmd": APP_CMD_CALLBACK,
                "headers": {"req_id": self._new_req_id("wh")},
                "body": decrypted,
            }
            try:
                await self._on_message(payload)
            except Exception as exc:
                logger.exception("[%s] Failed to dispatch webhook message: %s", self.name, exc)
            return web.json_response({"errcode": 0, "errmsg": "ok"})

        elif msgtype == "stream_refresh":
            sender_id = str(decrypted.get("from", {}).get("userid") or "").strip()
            chat_id = str(decrypted.get("chatid") or sender_id).strip()
            stream_id = str(decrypted.get("stream_id") or "")
            return await self._handle_stream_refresh(account, chat_id, stream_id)

        elif msgtype == "enter_chat":
            welcome = account.welcome_text or "你好，有什么可以帮你的？"
            return web.json_response({
                "msgtype": "markdown",
                "markdown": {"content": welcome},
            })

        return web.json_response({"errcode": 0, "errmsg": "ok"})

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

    def _match_webhook_account(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> Optional[WeComAccount]:
        """Try all webhook accounts and return the one whose signature matches."""
        wh_accounts = [a for a in self._accounts if a.connection_mode == "webhook" and a.is_webhook_configured]
        matches: List[WeComAccount] = []
        for account in wh_accounts:
            if verify_signature(account.token, timestamp, nonce, encrypt, signature):
                matches.append(account)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(a.account_id for a in matches)
            logger.warning("[%s] Webhook signature conflict among accounts: %s", self.name, ids)
        return None

    def _decrypt_webhook_body(self, account: WeComAccount, encrypt: str) -> Dict[str, Any]:
        """Decrypt a JSON webhook payload using the account's crypto credentials."""
        crypt = WXBizMsgCrypt(
            account.token,
            account.encoding_aes_key,
            account.receive_id or account.corp_id or "",
        )
        decrypted_bytes = crypt.decrypt_without_verify(encrypt)
        return json.loads(decrypted_bytes.decode("utf-8"))

    async def _open_connection(self) -> None:
        """Open and authenticate a websocket connection.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {
                    "bot_id": self._bot_id,
                    "secret": self._secret,
                    "device_id": self._device_id,
                },
            }
        )

        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in (0, None):
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")
        self._apply_tcp_keepalive()
        self._last_frame_at = asyncio.get_running_loop().time()

    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
        """Wait for the subscribe acknowledgement.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        if not self._ws:
            raise RuntimeError("WebSocket not initialized")

        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")

            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if not payload:
                    continue
                if payload.get("cmd") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WeCom websocket closed during authentication")

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        backoff_idx = 0
        while self._running:
            logger.debug("[%s] Listen loop iteration (backoff_idx=%s)", self.name, backoff_idx)
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                logger.debug("[%s] Listen loop cancelled", self.name)
                return
            except Exception as exc:
                if not self._running:
                    logger.debug("[%s] Listen loop stopping (_running=False)", self.name)
                    return
                if self._kicked:
                    logger.warning("[%s] Connection was kicked by server, not reconnecting", self.name)
                    return
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))
                self._reply_queue.fail_all(RuntimeError("WeCom connection interrupted"))

                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                logger.info("[%s] Reconnecting in %ss (attempt %s)", self.name, delay, backoff_idx)
                await asyncio.sleep(delay)
                if not self._running:
                    logger.debug("[%s] Listen loop stopping after sleep (_running=False)", self.name)
                    return
                if self._kicked:
                    logger.warning("[%s] Connection was kicked by server after sleep, not reconnecting", self.name)
                    return

                try:
                    await asyncio.wait_for(
                        self._open_connection(),
                        timeout=CONNECT_TIMEOUT_SECONDS + 10,
                    )
                    backoff_idx = 0
                    logger.info("[%s] Reconnected", self.name)
                except asyncio.CancelledError:
                    logger.debug("[%s] Reconnect cancelled", self.name)
                    raise
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] Reconnect timed out after %ss",
                        self.name, CONNECT_TIMEOUT_SECONDS + 10,
                    )
                    await self._cleanup_ws()
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)
                    await self._cleanup_ws()

    async def _read_events(self) -> None:
        """Read websocket frames until the connection closes.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            self._last_frame_at = asyncio.get_running_loop().time()
            try:
                msg = await asyncio.wait_for(
                    self._ws.receive(), timeout=HEARTBEAT_INTERVAL_SECONDS * 3
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "WeCom websocket read timeout (%ss)" % (HEARTBEAT_INTERVAL_SECONDS * 3)
                )
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._ws_client.on_any_frame()
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WeCom websocket closed")
        logger.debug(
            "[%s] _read_events exiting (_running=%s, ws_closed=%s)",
            self.name,
            self._running,
            self._ws.closed if self._ws else True,
        )

    async def _watchdog_loop(self) -> None:
        """Monitor websocket activity and force reconnect if traffic stalls.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        try:
            while self._running:
                await asyncio.sleep(WATCHDOG_TIMEOUT_SECONDS)
                if not self._running:
                    return
                if not self._ws or self._ws.closed:
                    continue
                idle = asyncio.get_running_loop().time() - self._last_frame_at
                if idle >= WATCHDOG_TIMEOUT_SECONDS:
                    logger.warning(
                        "[%s] WebSocket watchdog timeout (idle %.1fs), forcing reconnect",
                        self.name, idle,
                    )
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self) -> None:
        """Send lightweight application-level pings.

        .. deprecated::
            Legacy aiohttp path; not used in SDK mode. Kept for fallback.
        """
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue

                if await self._ws_client.increment_missed():
                    logger.warning(
                        "[%s] Missed %s pongs — force-closing websocket",
                        self.name, self._ws_client.missed_pongs,
                    )
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    # Also close transport directly because aiohttp close() is graceful.
                    transport = self._ws.get_extra_info("transport")
                    if transport is not None:
                        try:
                            transport.close()
                        except Exception:
                            pass
                    continue

                ping_req_id = self._new_req_id("ping")
                await self._ws_client.register_ping(ping_req_id)
                try:
                    await self._send_json(
                        {
                            "cmd": APP_CMD_PING,
                            "headers": {"req_id": ping_req_id},
                            "body": {},
                        }
                    )
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
        except asyncio.CancelledError:
            pass

    async def _sdk_watchdog_loop(self) -> None:
        """Monitor SDK websocket activity and force reconnect if traffic stalls."""
        try:
            while self._running:
                await asyncio.sleep(SDK_WATCHDOG_TIMEOUT_SECONDS / 2)
                if not self._running:
                    return
                if not self._sdk_client:
                    continue
                if not self._sdk_client.is_connected:
                    # SDK has marked its own connection dead (e.g. missed heartbeats).
                    # The SDK does not always auto-reconnect in this state, so force it.
                    logger.warning("[%s] SDK connection dropped, forcing reconnect", self.name)
                    await self._sdk_reconnect()
                    continue
                idle = asyncio.get_running_loop().time() - self._last_sdk_message_at
                if idle >= SDK_WATCHDOG_TIMEOUT_SECONDS:
                    logger.warning(
                        "[%s] SDK watchdog timeout (idle %.1fs), forcing reconnect",
                        self.name, idle,
                    )
                    await self._sdk_reconnect()
        except asyncio.CancelledError:
            pass

    async def _sdk_reconnect(self) -> None:
        """Force the SDK client to reconnect.

        Best-effort: swallows disconnect/connect errors so the watchdog keeps
        retrying on the next cycle instead of crashing the adapter.
        """
        if not self._sdk_client:
            return
        try:
            self._sdk_client.disconnect()
        except Exception:
            pass
        await asyncio.sleep(1)
        if not self._running:
            return
        try:
            await self._sdk_client.connect()
            self._last_sdk_message_at = asyncio.get_running_loop().time()
            self._apply_sdk_tcp_keepalive()
            logger.info("[%s] SDK watchdog reconnect succeeded", self.name)
        except Exception as exc:
            logger.warning("[%s] SDK watchdog reconnect failed: %s", self.name, exc)

    def _apply_sdk_tcp_keepalive(self) -> None:
        """Set TCP keepalive socket options on the SDK-managed websocket."""
        import socket

        if not TCP_KEEPALIVE_ENABLED:
            return
        if not self._sdk_client:
            return

        ws = getattr(self._sdk_client, "_ws_manager", None)
        if ws is None:
            return
        ws_proto = getattr(ws, "_ws", None)
        if ws_proto is None:
            return

        # websockets >= 14: transport is an asyncio.Transport
        # websockets < 14: writer is an asyncio.StreamWriter
        sock = None
        if hasattr(ws_proto, "transport"):
            sock = ws_proto.transport.get_extra_info("socket")
        elif hasattr(ws_proto, "writer"):
            sock = ws_proto.writer.get_extra_info("socket")
        elif hasattr(ws_proto, "socket"):
            sock = ws_proto.socket
        if sock is None:
            return

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT)
        except (OSError, AttributeError):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), TCP_KEEPALIVE_IDLE)
            except (OSError, AttributeError):
                logger.debug("[%s] Could not apply TCP keepalive socket options", self.name)

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route inbound websocket payloads."""
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}

        # Ping/pong tracking: any inbound frame for a ping req_id counts as a pong.
        if req_id and await self._ws_client.on_pong(req_id):
            return

        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return

        if cmd in CALLBACK_COMMANDS or cmd == APP_CMD_EVENT_CALLBACK:
            event_name = str(body.get("event") or "").lower()
            if event_name == "disconnected_event":
                logger.warning(
                    "[%s] Server sent disconnected_event — connection kicked, disabling auto-reconnect",
                    self.name,
                )
                self._running = False
                self._kicked = True
                try:
                    await self._ws.close()
                except Exception:
                    pass
                exc = RuntimeError("WeCom connection kicked by server")
                self._fail_pending_responses(exc)
                self._reply_queue.fail_all(exc)
                return
            if event_name == "enter_check_update":
                try:
                    await self._send_request(
                        APP_CMD_SEND,
                        {
                            "chatid": str(body.get("chatid") or ""),
                            "msgtype": "text",
                            "text": {"content": f"HermesAgent version {getattr(self, '_version', '1.0')} ready"},
                        },
                    )
                except Exception as exc:
                    logger.debug("[%s] Version handshake reply failed: %s", self.name, exc)
                return
            await self._on_sdk_message(payload)
            return
        if cmd == APP_CMD_PING:
            return

        logger.debug("[%s] Ignoring websocket payload: %s", self.name, cmd or payload)

    def _fail_pending_responses(self, exc: Exception) -> None:
        """Fail all outstanding request futures."""
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending_responses.pop(req_id, None)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        """Send a raw JSON frame over the active websocket."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        await asyncio.wait_for(
            self._ws.send_json(payload), timeout=REQUEST_TIMEOUT_SECONDS
        )

    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Send a JSON request and await the correlated response."""
        # SDK path: use the SDK's managed send/reply mechanism
        if self._sdk_client and self._sdk_client.is_connected:
            req_id = self._new_req_id(cmd)
            try:
                frame = await asyncio.wait_for(
                    self._sdk_client._ws_manager.send_reply(req_id, body, cmd),
                    timeout=timeout,
                )
                return frame
            except RuntimeError as exc:
                return {"errcode": -1, "errmsg": str(exc)}
            except asyncio.TimeoutError:
                return {"errcode": -1, "errmsg": f"Request timeout after {timeout}s"}

        # Legacy WebSocket path
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        req_id = self._new_req_id(cmd)
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(req_id, None)

    async def _send_reply_request(
        self,
        reply_req_id: str,
        body: Dict[str, Any],
        cmd: str = APP_CMD_RESPONSE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Send a reply frame correlated to an inbound callback req_id."""
        # SDK path: the SDK handles reply queuing internally
        if self._sdk_client and self._sdk_client.is_connected:
            normalized_req_id = str(reply_req_id or "").strip()
            if not normalized_req_id:
                raise ValueError("reply_req_id is required")
            try:
                frame = await asyncio.wait_for(
                    self._sdk_client._ws_manager.send_reply(normalized_req_id, body, cmd),
                    timeout=timeout,
                )
                return frame
            except RuntimeError as exc:
                return {"errcode": -1, "errmsg": str(exc)}
            except asyncio.TimeoutError:
                return {"errcode": -1, "errmsg": f"Request timeout after {timeout}s"}

        # Legacy WebSocket path
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("reply_req_id is required")

        future = await self._reply_queue.enqueue(
            normalized_req_id,
            {"cmd": cmd, "headers": {"req_id": normalized_req_id}, "body": body},
            timeout=timeout,
        )
        response = await future
        return response

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: Dict[str, Any]) -> str:
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Failed to parse WeCom payload: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------
    # Inbound message parsing
    # ------------------------------------------------------------------

    async def _on_sdk_message(self, payload: Dict[str, Any]) -> None:
        """Auth-check wrapper for SDK inbound messages.

        Mirrors the Webhook path: command authorization is evaluated BEFORE
        the message is enqueued to ``ChatSerialQueue``.  Unauthorized
        commands are rejected immediately so they never enter the serial
        processing pipeline.
        """
        self._last_inbound_frame = payload
        self._last_sdk_message_at = asyncio.get_running_loop().time()
        body = payload.get("body")
        if not isinstance(body, dict):
            await self._on_message(payload)
            return

        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        is_group = str(body.get("chattype") or "").lower() == "group"

        text, _ = self._extract_text(body)
        if is_group and text:
            text = re.sub(r"^@\S+\s*", "", text).strip()

        account = self._accounts[0] if self._accounts else None
        if account:
            chat_type = "group" if is_group else ""
            auth = resolve_command_auth(account, text, sender_id, chat_id=chat_id, chat_type=chat_type)
            if auth.should_compute_auth and not auth.command_authorized:
                logger.info(
                    "[%s] Dropping unauthorized command from %s in chat %s",
                    self.name, sender_id, chat_id,
                )
                return

        await self._on_message(payload)

    async def _on_message(self, payload: Dict[str, Any]) -> None:
        """Process an inbound WeCom message callback event."""
        body = payload.get("body")
        if not isinstance(body, dict):
            return

        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s ignored", self.name, msg_id)
            return

        # Event callbacks (e.g. template_card_event) don't have a valid req_id for passive reply.
        if str(body.get("msgtype") or "").lower() != "event":
            self._remember_reply_req_id(msg_id, self._payload_req_id(payload))

        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        if not chat_id:
            logger.debug("[%s] Missing chat id, skipping message", self.name)
            return

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

        # Cache the freshest req_id per chat AFTER policy checks so proactive
        # sends (e.g. cron, delayed replies) can fall back to APP_CMD_RESPONSE.
        # Bounded to prevent unbounded growth on long-running gateways.
        if str(body.get("msgtype") or "").lower() != "event":
            _req_id = self._payload_req_id(payload)
            if _req_id:
                self._last_reply_req_id_per_chat[chat_id] = _req_id
                while len(self._last_reply_req_id_per_chat) > DEDUP_MAX_SIZE:
                    self._last_reply_req_id_per_chat.pop(next(iter(self._last_reply_req_id_per_chat)))

        text, reply_text = self._extract_text(body)
        # Strip leading @mention in group chats so slash commands like
        # "@BotName /approve" are correctly recognized as "/approve".
        # Mirrors what the Telegram adapter does (re.sub @botname).
        if is_group and text:
            text = re.sub(r"^@\S+\s*", "", text).strip()

        # Template card event: handle gracefully without dispatching as normal message
        msgtype = str(body.get("msgtype") or "").lower()
        _raw_event = body.get("event")
        if msgtype == "event" and isinstance(_raw_event, dict):
            event_name = str(_raw_event.get("event") or "").lower()
        elif msgtype == "event":
            event_name = str(_raw_event or "").lower()
        else:
            event_name = ""
        if event_name == "template_card_event":
            text = await self._handle_template_card_event(body)
            if not text:
                return

        # Auth change event: build context text and dispatch as normal message
        if event_name == "auth_change_event":
            text = self._build_auth_change_text(body)

        media_urls, media_types = await self._extract_media(body, chat_id=chat_id)
        message_type = self._derive_message_type(body, text, media_types)
        has_reply_context = bool(reply_text and (text or media_urls))

        if not text and reply_text and not media_urls:
            text = reply_text

        if not text and not media_urls:
            logger.debug(
                "[%s] Empty WeCom message skipped (msgtype=%s text=%r media_urls=%s)",
                self.name, msgtype, text, media_urls,
            )
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_type="group" if is_group else "dm",
            user_id=sender_id or None,
            user_name=sender_id or None,
        )

        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=payload,
            message_id=msg_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=f"quote:{msg_id}" if has_reply_context else None,
            reply_to_text=reply_text if has_reply_context else None,
            timestamp=datetime.now(tz=timezone.utc),
        )

        # Only batch plain text messages — commands, media, etc. dispatch
        # immediately since they won't be split by the WeCom client.
        if message_type == MessageType.TEXT and self._text_batcher.is_enabled():
            key = self._text_batch_key(event)
            self._text_batcher.enqueue(event, key)
        else:
            await self._chat_queue.enqueue(chat_id, event)

    async def _handle_message_sync(self, _chat_id: str, event: MessageEvent) -> None:
        """Adapter-compatible wrapper for ChatSerialQueue handler signature."""
        await self.handle_message(event)

    async def _handle_template_card_event(self, body: Dict[str, Any]) -> str:
        """Build a text representation of a template card event for agent dispatch.

        Also updates the template card UI to reflect user selections via
        aibot_update_template_card.

        Returns the event description text, or empty string if the event should
        be silently dropped.
        """
        from gateway.platforms.wecom.template_cards import get_template_card_from_cache

        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        response_code = str(event.get("response_code") or "").strip()
        button_replace_name = str(event.get("button_replace_name") or "").strip()
        selected_options = event.get("selected_options")
        selected_option_ids = (
            [str(opt.get("key") or "") for opt in selected_options if isinstance(opt, dict)]
            if isinstance(selected_options, list)
            else []
        )

        logger.debug(
            "[%s] Template card event received: response_code=%s selected=%s",
            self.name, response_code, selected_option_ids,
        )

        # Look up cached card context using response_code as task_id
        account_id = self._accounts[0].account_id if self._accounts else "default"
        cached_card = get_template_card_from_cache(account_id, response_code) if response_code else None
        card_type = str(cached_card.get("card_type") or "").strip() if cached_card else ""
        source_desc = str(cached_card.get("source", {}).get("desc") or "").strip() if cached_card else ""

        # Update the card UI to reflect user interaction
        if response_code:
            await self._update_template_card_ui(
                account_id, response_code, selected_options, button_replace_name
            )

        parts: List[str] = ["[Template card interaction]"]
        if source_desc:
            parts.append(f"Card: {source_desc}")
        elif card_type:
            parts.append(f"Card type: {card_type}")
        if button_replace_name:
            parts.append(f"Button: {button_replace_name}")
        elif response_code:
            parts.append(f"Action: {response_code}")
        if selected_option_ids:
            parts.append(f"Selected: {', '.join(selected_option_ids)}")
        return "\n".join(parts)

    async def _update_template_card_ui(
        self,
        account_id: str,
        task_id: str,
        selected_options: Any,
        button_replace_name: str,
    ) -> None:
        """Update template card to reflect user selections."""
        from gateway.platforms.wecom.template_cards import get_template_card_from_cache

        card = get_template_card_from_cache(account_id, task_id)
        if not card:
            logger.debug("[%s] No cached card for task_id=%s, skipping UI update", self.name, task_id)
            return

        updated = copy.deepcopy(card)

        # Update checkbox selections
        if selected_options and isinstance(selected_options, list):
            checkbox = updated.get("checkbox")
            if isinstance(checkbox, dict):
                options = checkbox.get("option_list", [])
                selected_keys = {str(opt.get("key") or opt.get("id") or "") for opt in selected_options if isinstance(opt, dict)}
                for opt in options:
                    if isinstance(opt, dict):
                        opt_id = str(opt.get("id") or opt.get("key") or "")
                        opt["is_checked"] = opt_id in selected_keys

        # Update button selection
        if button_replace_name:
            btn_sel = updated.get("button_selection")
            if isinstance(btn_sel, dict):
                options = btn_sel.get("option_list", [])
                for opt in options:
                    if isinstance(opt, dict) and opt.get("id") == button_replace_name:
                        opt["is_checked"] = True

        try:
            await self._send_request(
                "aibot_update_template_card",
                {
                    "response_code": task_id,
                    "button": {"replace_name": button_replace_name} if button_replace_name else {},
                    "template_card": updated,
                },
            )
            logger.debug("[%s] Updated template card UI for task_id=%s", self.name, task_id)
        except Exception as exc:
            logger.warning("[%s] Failed to update template card UI: %s", self.name, exc)

    @staticmethod
    def _build_auth_change_text(body: Dict[str, Any]) -> str:
        """Build a human-readable text from an auth_change_event payload."""
        _raw_auth_list = body.get("auth_list")
        auth_list = _raw_auth_list if isinstance(_raw_auth_list, list) else []
        if not auth_list:
            return "[Document permission updated]"
        perms: List[str] = []
        for code in auth_list:
            # WeCom may send auth_list values as strings
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if code == 1:
                perms.append("create/edit")
            elif code == 2:
                perms.append("read content")
            else:
                perms.append(str(code))
        return f"[Document permission updated: {', '.join(perms)} granted]"

    # ------------------------------------------------------------------
    # Text message aggregation (handles WeCom client-side splits)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extract plain text and quoted text from a callback payload."""
        text_parts: List[str] = []
        reply_text: Optional[str] = None
        msgtype = str(body.get("msgtype") or "").lower()

        if msgtype == "mixed":
            _raw_mixed = body.get("mixed")
            mixed = _raw_mixed if isinstance(_raw_mixed, dict) else {}
            _raw_items = mixed.get("msg_item")
            items = _raw_items if isinstance(_raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("msgtype") or "").lower() == "text":
                    _raw_text = item.get("text")
                    text_block = _raw_text if isinstance(_raw_text, dict) else {}
                    content = str(text_block.get("content") or "").strip()
                    if content:
                        text_parts.append(content)
        else:
            text_block = body.get("text") if isinstance(body.get("text"), dict) else {}
            content = str(text_block.get("content") or "").strip()
            if content:
                text_parts.append(content)

            if msgtype == "voice":
                voice_block = body.get("voice") if isinstance(body.get("voice"), dict) else {}
                voice_text = str(voice_block.get("content") or "").strip()
                if voice_text:
                    text_parts.append(voice_text)

            # Extract appmsg title (filename) for WeCom AI Bot attachments
            if msgtype == "appmsg":
                appmsg = body.get("appmsg") if isinstance(body.get("appmsg"), dict) else {}
                title = str(appmsg.get("title") or "").strip()
                if title:
                    text_parts.append(title)

        quote = body.get("quote") if isinstance(body.get("quote"), dict) else {}
        quote_type = str(quote.get("msgtype") or "").lower()
        if quote_type == "text":
            quote_text = quote.get("text") if isinstance(quote.get("text"), dict) else {}
            reply_text = str(quote_text.get("content") or "").strip() or None
        elif quote_type == "voice":
            quote_voice = quote.get("voice") if isinstance(quote.get("voice"), dict) else {}
            reply_text = str(quote_voice.get("content") or "").strip() or None

        return "\n".join(part for part in text_parts if part).strip(), reply_text

    async def _extract_media(
        self, body: Dict[str, Any], chat_id: str = ""
    ) -> Tuple[List[str], List[str]]:
        """Best-effort extraction of inbound media to local cache paths."""
        media_paths: List[str] = []
        media_types: List[str] = []
        refs: List[Tuple[str, Dict[str, Any]]] = []
        oversize_files: List[MediaOversizeError] = []
        msgtype = str(body.get("msgtype") or "").lower()
        logger.debug(
            "[%s] _extract_media: msgtype=%s body_keys=%s",
            self.name, msgtype, list(body.keys()),
        )

        if msgtype == "mixed":
            _raw_mixed = body.get("mixed")
            mixed = _raw_mixed if isinstance(_raw_mixed, dict) else {}
            _raw_items = mixed.get("msg_item")
            items = _raw_items if isinstance(_raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").lower()
                if item_type == "image" and isinstance(item.get("image"), dict):
                    refs.append(("image", item["image"]))
        else:
            if isinstance(body.get("image"), dict):
                refs.append(("image", body["image"]))
            if msgtype == "file" and isinstance(body.get("file"), dict):
                refs.append(("file", body["file"]))
            # Handle appmsg (WeCom AI Bot attachments with PDF/Word/Excel)
            if msgtype == "appmsg" and isinstance(body.get("appmsg"), dict):
                appmsg = body["appmsg"]
                if isinstance(appmsg.get("file"), dict):
                    refs.append(("file", appmsg["file"]))
                elif isinstance(appmsg.get("image"), dict):
                    refs.append(("image", appmsg["image"]))
            # Handle video — route through _cache_media like other file types
            video_refs_for_frame: set = set()
            if msgtype == "video" and isinstance(body.get("video"), dict):
                video_info = body["video"]
                video_url = str(video_info.get("url") or "").strip()
                if video_url:
                    refs.append(("file", video_info))
                    video_refs_for_frame.add(id(video_info))

        quote = body.get("quote") if isinstance(body.get("quote"), dict) else {}
        quote_type = str(quote.get("msgtype") or "").lower()
        if quote_type == "image" and isinstance(quote.get("image"), dict):
            refs.append(("image", quote["image"]))
        elif quote_type == "file" and isinstance(quote.get("file"), dict):
            refs.append(("file", quote["file"]))
        elif quote_type == "video" and isinstance(quote.get("video"), dict):
            video_info = quote["video"]
            video_url = str(video_info.get("url") or "").strip()
            if video_url:
                refs.append(("file", video_info))
                video_refs_for_frame.add(id(video_info))

        logger.debug(
            "[%s] _extract_media: extracted %d refs for msgtype=%s",
            self.name, len(refs), msgtype,
        )
        cached_video_paths: list = []
        for kind, ref in refs:
            logger.debug(
                "[%s] _extract_media: caching kind=%s ref_keys=%s",
                self.name, kind, list(ref.keys()),
            )
            try:
                cached = await self._cache_media(kind, ref)
            except MediaOversizeError as exc:
                oversize_files.append(exc)
                logger.warning(
                    "[%s] Media oversize: %s (%s bytes > %s bytes limit)",
                    self.name, exc.filename, exc.size_bytes, exc.max_bytes,
                )
                continue
            if cached:
                path, content_type = cached
                media_paths.append(path)
                media_types.append(content_type)
                if id(ref) in video_refs_for_frame:
                    cached_video_paths.append(path)
                logger.debug(
                    "[%s] _extract_media: cached kind=%s path=%s content_type=%s",
                    self.name, kind, path, content_type,
                )
            else:
                logger.debug(
                    "[%s] _extract_media: failed to cache kind=%s ref=%s",
                    self.name, kind, ref,
                )

        # Extract first frame from cached videos for LLM preview
        for cached_video_path in cached_video_paths:
            try:
                from gateway.platforms.wecom.video import extract_first_video_frame

                frame_path = extract_first_video_frame(cached_video_path)
                if frame_path:
                    media_paths.append(frame_path)
                    media_types.append("image/jpeg")
            except Exception as exc:
                logger.debug("[%s] First-frame extraction failed: %s", self.name, exc)

        if video_refs_for_frame and not cached_video_paths:
            logger.warning(
                "[%s] Failed to cache video (msgtype=video), message will have no media",
                self.name,
            )

        # Send user-facing notice for oversize files
        if oversize_files and chat_id:
            try:
                names = [f.filename or "文件" for f in oversize_files]
                notice = (
                    "以下文件过大，无法接收：\n"
                    + "\n".join(f"- {n}" for n in names)
                    + "\n\n请压缩后重新发送，或分段发送。"
                )
                await self._send_request(
                    APP_CMD_SEND,
                    {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": notice}},
                )
            except Exception as exc:
                logger.warning("[%s] Failed to send oversize notice: %s", self.name, exc)

        logger.debug(
            "[%s] _extract_media: done — paths=%s types=%s",
            self.name, media_paths, media_types,
        )
        return media_paths, media_types

    async def _cache_media(self, kind: str, media: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Cache an inbound image/file/media reference to local storage."""
        if "base64" in media and media.get("base64"):
            logger.debug("[%s] _cache_media: using base64 for kind=%s", self.name, kind)
            try:
                raw = self._decode_base64(media["base64"])
            except Exception as exc:
                logger.debug("[%s] Failed to decode %s base64 media: %s", self.name, kind, exc)
                return None

            if kind == "image":
                ext = self._detect_image_ext(raw)
                try:
                    result = cache_image_from_bytes(raw, ext), self._mime_for_ext(ext, fallback="image/jpeg")
                    logger.debug("[%s] _cache_media: base64 image cached to %s", self.name, result[0])
                    return result
                except ValueError as exc:
                    logger.warning("[%s] Rejected non-image bytes: %s", self.name, exc)
                    return None

            filename = str(media.get("filename") or media.get("name") or "wecom_file")
            return cache_document_from_bytes(raw, filename), mimetypes.guess_type(filename)[0] or "application/octet-stream"

        url = str(media.get("url") or "").strip()
        if not url:
            logger.debug("[%s] _cache_media: no url or base64 for kind=%s", self.name, kind)
            return None

        logger.debug("[%s] _cache_media: downloading kind=%s url=%s", self.name, kind, url)
        try:
            raw, headers = await self._download_remote_bytes(url, max_bytes=ABSOLUTE_MAX_BYTES)
        except ValueError as exc:
            if isinstance(exc, MediaOversizeError):
                raise
            err_msg = str(exc)
            if "exceeds" in err_msg.lower() and "limit" in err_msg.lower():
                filename = self._guess_filename(url, None, "")
                size_bytes = 0
                max_bytes = ABSOLUTE_MAX_BYTES
                # Try to extract size from error message
                import re as _re
                m = _re.search(r"(\d+)\s*bytes\s*>\s*(\d+)\s*bytes", err_msg)
                if m:
                    size_bytes = int(m.group(1))
                    max_bytes = int(m.group(2))
                raise MediaOversizeError(filename=filename, size_bytes=size_bytes, max_bytes=max_bytes) from exc
            logger.debug("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
            return None
        except Exception as exc:
            logger.debug("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
            return None

        aes_key = str(media.get("aeskey") or "").strip()
        if aes_key:
            try:
                raw = self._decrypt_file_bytes(raw, aes_key)
            except Exception as exc:
                # WeCom sometimes sends aeskey even when data is already plaintext
                # (COS server-side encryption). Fall back to raw bytes instead of
                # losing the media entirely. cache_image_from_bytes will reject
                # truly corrupted data via _looks_like_image.
                logger.warning(
                    "[%s] AES decrypt failed for %s from %s (data may already be plaintext from COS SSE), "
                    "using raw bytes: %s",
                    self.name, kind, url, exc,
                )

        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip() or "application/octet-stream"
        logger.debug(
            "[%s] _cache_media: downloaded %d bytes content_type=%s for kind=%s url=%s",
            self.name, len(raw), content_type, kind, url,
        )
        if kind == "image":
            ext = self._guess_extension(url, content_type, fallback=self._detect_image_ext(raw))
            try:
                result = cache_image_from_bytes(raw, ext), content_type or self._mime_for_ext(ext, fallback="image/jpeg")
                logger.debug("[%s] _cache_media: url image cached to %s", self.name, result[0])
                return result
            except ValueError as exc:
                logger.warning("[%s] Rejected non-image bytes from %s: %s", self.name, url, exc)
                return None

        filename = self._guess_filename(url, headers.get("content-disposition"), content_type)
        result = cache_document_from_bytes(raw, filename), content_type
        logger.debug("[%s] _cache_media: url file cached to %s", self.name, result[0])
        return result

    @staticmethod
    def _decode_base64(data: str) -> bytes:
        payload = data.split(",", 1)[-1].strip()
        return base64.b64decode(payload)

    @staticmethod
    def _detect_image_ext(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    @staticmethod
    def _mime_for_ext(ext: str, fallback: str = "application/octet-stream") -> str:
        return mimetypes.types_map.get(ext.lower(), fallback)

    @staticmethod
    def _guess_extension(url: str, content_type: str, fallback: str) -> str:
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if ext:
            return ext
        path_ext = Path(urlparse(url).path).suffix
        if path_ext:
            return path_ext
        return fallback

    @staticmethod
    def _guess_filename(url: str, content_disposition: Optional[str], content_type: str) -> str:
        if content_disposition:
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)

        name = Path(urlparse(url).path).name or "document"
        if "." not in name:
            ext = mimetypes.guess_extension(content_type) or ".bin"
            name = f"{name}{ext}"
        return name

    @staticmethod
    def _derive_message_type(body: Dict[str, Any], text: str, media_types: List[str]) -> MessageType:
        """Choose the normalized inbound message type."""
        if any(mtype.startswith(("application/", "text/")) for mtype in media_types):
            return MessageType.DOCUMENT
        if any(mtype.startswith("image/") for mtype in media_types):
            return MessageType.TEXT if text else MessageType.PHOTO
        if str(body.get("msgtype") or "").lower() == "voice":
            return MessageType.VOICE
        if any(mtype.startswith("video/") for mtype in media_types) or str(body.get("msgtype") or "").lower() == "video":
            return MessageType.VIDEO
        return MessageType.TEXT

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------

    def _is_dm_allowed(self, sender_id: str) -> Tuple[bool, Optional[str]]:
        """Returns (allowed, block_reason)."""
        if self._dm_policy == "disabled":
            return False, "disabled"
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id), None
        if self._dm_policy == "pairing":
            return False, "pairing"
        return True, None

    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist" and not _entry_matches(self._group_allow_from, chat_id):
            return False

        group_cfg = self._resolve_group_cfg(chat_id)
        sender_allow = _coerce_list(group_cfg.get("allow_from") or group_cfg.get("allowFrom"))
        if sender_allow:
            return _entry_matches(sender_allow, sender_id)
        return True

    def _resolve_group_cfg(self, chat_id: str) -> Dict[str, Any]:
        if not isinstance(self._groups, dict):
            return {}
        if chat_id in self._groups and isinstance(self._groups[chat_id], dict):
            return self._groups[chat_id]
        lowered = chat_id.lower()
        for key, value in self._groups.items():
            if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                return value
        wildcard = self._groups.get("*")
        return wildcard if isinstance(wildcard, dict) else {}

    def _remember_reply_req_id(self, message_id: str, req_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_message_id or not normalized_req_id:
            return
        logger.debug(
            "[%s] _remember_reply_req_id: msg_id=%s -> req_id=%s",
            self.name, normalized_message_id, normalized_req_id,
        )
        self._reply_req_ids[normalized_message_id] = normalized_req_id
        if self._reqid_store:
            self._reqid_store.set(normalized_message_id, normalized_req_id)
            self._schedule_reqid_flush()
        while len(self._reply_req_ids) > DEDUP_MAX_SIZE:
            self._reply_req_ids.pop(next(iter(self._reply_req_ids)))

    def _schedule_reqid_flush(self) -> None:
        """Debounce disk writes: batch multiple rapid req_id updates into one save."""
        if self._reqid_flush_task and not self._reqid_flush_task.done():
            self._reqid_flush_task.cancel()
        self._reqid_flush_task = asyncio.get_event_loop().create_task(
            self._debounced_reqid_flush()
        )

    async def _debounced_reqid_flush(self) -> None:
        await asyncio.sleep(5.0)
        if self._reqid_store and self._reqid_store.is_dirty:
            self._reqid_store.save()

    def _reply_req_id_for_message(self, reply_to: Optional[str]) -> Optional[str]:
        normalized = str(reply_to or "").strip()
        if not normalized or normalized.startswith("quote:"):
            return None
        return self._reply_req_ids.get(normalized)

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        mime_type = mimetypes.guess_type(filename)[0]
        if mime_type:
            return mime_type
        if Path(filename).suffix.lower() == ".amr":
            return "audio/amr"
        return "application/octet-stream"

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

    @staticmethod
    def _response_error(response: Dict[str, Any]) -> Optional[str]:
        errcode = response.get("errcode", 0)
        if errcode in (0, None):
            return None
        errmsg = str(response.get("errmsg") or "unknown error")
        return f"WeCom errcode {errcode}: {errmsg}"

    @classmethod
    def _raise_for_wecom_error(cls, response: Dict[str, Any], operation: str) -> None:
        error = cls._response_error(response)
        if error:
            errcode = response.get("errcode", 0)
            if errcode == 846608:
                raise StreamExpiredError(f"{operation} failed: {error}")
            raise RuntimeError(f"{operation} failed: {error}")

    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")

        # WeCom sends the AES key as a 43-char base64 string without the
        # trailing '=' padding (same format as callback encoding_aes_key).
        # We need to add the padding so base64.b64decode succeeds.
        normalized_key = aes_key + "=" if len(aes_key) == 43 else aes_key
        key = base64.b64decode(normalized_key)
        if len(key) != 32:
            raise ValueError(f"Invalid WeCom AES key length: expected 32 bytes, got {len(key)}")

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:  # pragma: no cover - dependency is environment-specific
            raise RuntimeError("cryptography is required for WeCom media decryption") from exc

        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()

        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
        if any(byte != pad_len for byte in decrypted[-pad_len:]):
            raise ValueError("Invalid PKCS#7 padding: padding bytes mismatch")

        return decrypted[:-pad_len]

    async def _download_remote_bytes(
        self,
        url: str,
        max_bytes: int,
    ) -> Tuple[bytes, Dict[str, str]]:
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url[:80]}")

        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx is required for WeCom media download")

        client = self._http_client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        created_client = client is not self._http_client
        try:
            async with client.stream(
                "GET",
                url,
                headers={
                    "User-Agent": "HermesAgent/1.0",
                    "Accept": "*/*",
                },
            ) as response:
                response.raise_for_status()
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_length = headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError(
                        f"Remote media exceeds WeCom limit: {int(content_length)} bytes > {max_bytes} bytes"
                    )

                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(
                            f"Remote media exceeds WeCom limit while downloading: {len(data)} bytes > {max_bytes} bytes"
                        )

                return bytes(data), headers
        finally:
            if created_client:
                await client.aclose()

    @staticmethod
    def _looks_like_url(media_source: str) -> bool:
        parsed = urlparse(str(media_source or ""))
        return parsed.scheme in {"http", "https"}

    async def _prepare_outbound_media(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        roots = self._accounts[0].media_local_roots if self._accounts else []
        preparer = MediaPreparer(self._http_client, media_local_roots=roots)
        return await preparer.prepare(media_source, file_name=file_name)

    async def _upload_media_bytes(self, data: bytes, media_type: str, filename: str) -> Dict[str, Any]:
        if not data:
            raise ValueError("Cannot upload empty media")

        total_size = len(data)
        total_chunks = (total_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
        if total_chunks > MAX_UPLOAD_CHUNKS:
            raise ValueError(
                f"File too large: {total_chunks} chunks exceeds maximum of {MAX_UPLOAD_CHUNKS} chunks"
            )

        init_response = await self._send_request(
            APP_CMD_UPLOAD_MEDIA_INIT,
            {
                "type": media_type,
                "filename": filename,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "md5": hashlib.md5(data).hexdigest(),
            },
        )
        self._raise_for_wecom_error(init_response, "media upload init")

        init_body = init_response.get("body") if isinstance(init_response.get("body"), dict) else {}
        upload_id = str(init_body.get("upload_id") or "").strip()
        if not upload_id:
            raise RuntimeError(f"media upload init failed: missing upload_id in response {init_response}")

        for chunk_index, start in enumerate(range(0, total_size, UPLOAD_CHUNK_SIZE)):
            chunk = data[start : start + UPLOAD_CHUNK_SIZE]
            chunk_response = await self._send_request(
                APP_CMD_UPLOAD_MEDIA_CHUNK,
                {
                    "upload_id": upload_id,
                    # Match the official SDK implementation, which currently uses 0-based chunk indexes.
                    "chunk_index": chunk_index,
                    "base64_data": base64.b64encode(chunk).decode("ascii"),
                },
            )
            self._raise_for_wecom_error(chunk_response, f"media upload chunk {chunk_index}")

        finish_response = await self._send_request(
            APP_CMD_UPLOAD_MEDIA_FINISH,
            {"upload_id": upload_id},
        )
        self._raise_for_wecom_error(finish_response, "media upload finish")

        finish_body = finish_response.get("body") if isinstance(finish_response.get("body"), dict) else {}
        media_id = str(finish_body.get("media_id") or "").strip()
        if not media_id:
            raise RuntimeError(f"media upload finish failed: missing media_id in response {finish_response}")

        return {
            "type": str(finish_body.get("type") or media_type),
            "media_id": media_id,
            "created_at": finish_body.get("created_at"),
        }

    async def _send_media_message(self, chat_id: str, media_type: str, media_id: str) -> Dict[str, Any]:
        response = await self._send_request(
            APP_CMD_SEND,
            {
                "chatid": chat_id,
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
        )
        self._raise_for_wecom_error(response, "send media message")
        return response

    async def _send_reply_stream(self, reply_req_id: str, content: str, chat_id: str = "") -> Dict[str, Any]:
        logger.debug(
            "[%s] _send_reply_stream: start chat=%s req_id=%s",
            self.name, chat_id, reply_req_id,
        )
        # Non-blocking: skip intermediate frames if previous not yet acked
        if reply_req_id and not self._stream_acked.get(reply_req_id, True):
            logger.debug(
                "[%s] _send_reply_stream: skipping frame for chat=%s req_id=%s — previous not acked",
                self.name, chat_id, reply_req_id,
            )
            return {"errcode": 0, "_skipped": True}
        # Reuse the typing stream_id if one is active for this chat.
        # This mirrors the plugin pattern: thinking stream (finish=false)
        # and final response (finish=true) share the same stream_id.
        stream_id = None
        if chat_id:
            # First check if a stream was orphaned by pause_typing_for_chat
            # and belongs to the same message.  Reusing the stream_id lets
            # the final response replace the <think></think> placeholder
            # instead of leaving it hanging forever.
            pending_state = self._streams_pending_close.pop(chat_id, None)
            if pending_state and pending_state[0] == reply_req_id:
                stream_id = pending_state[1]
                logger.debug(
                    "[%s] _send_reply_stream: reused pending stream_id=%s for req_id=%s chat=%s",
                    self.name, stream_id, reply_req_id, chat_id,
                )
            elif pending_state:
                self._streams_pending_close[chat_id] = pending_state
                logger.debug(
                    "[%s] _send_reply_stream: pending state belongs to different req_id=%s, kept in queue",
                    self.name, pending_state[0],
                )

            if not stream_id:
                state = self._typing_stream_state_by_chat.pop(chat_id, None)
                if state and state[0] == reply_req_id:
                    stream_id = state[1]
                    logger.debug(
                        "[%s] _send_reply_stream: reused active stream_id=%s for req_id=%s chat=%s",
                        self.name, stream_id, reply_req_id, chat_id,
                    )
                elif state:
                    # The typing state belongs to a different message (e.g.
                    # inline /approve response while the original agent stream
                    # is still active). Put it back so the real response can
                    # close it later.
                    self._typing_stream_state_by_chat[chat_id] = state
                    logger.debug(
                        "[%s] _send_reply_stream: active state belongs to different req_id=%s, put back",
                        self.name, state[0],
                    )

        # Close any remaining orphaned streams that definitely won't be
        # reused.  Skip the current chat: if we have a matching pending
        # stream we want to send the response through it.
        await self._close_pending_streams(skip_chat_id=chat_id or None)

        if not stream_id:
            stream_id = self._new_req_id("stream")
            logger.debug(
                "[%s] _send_reply_stream: created new stream_id=%s for req_id=%s chat=%s",
                self.name, stream_id, reply_req_id, chat_id,
            )

        # Block _keep_typing from opening a new stream while this HTTP
        # request is in-flight. Once delivered we allow typing again so
        # multi-turn analysis within the same message shows an indicator.
        self._reply_req_ids_sending_response[reply_req_id] = (
            self._reply_req_ids_sending_response.get(reply_req_id, 0) + 1
        )
        if reply_req_id:
            self._stream_acked[reply_req_id] = False
        try:
            response = await self._send_reply_request(
                reply_req_id,
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "finish": True,
                        "content": content[:self.MAX_MESSAGE_LENGTH],
                    },
                },
            )
        finally:
            if reply_req_id:
                self._stream_acked[reply_req_id] = True
            count = self._reply_req_ids_sending_response.get(reply_req_id, 0) - 1
            if count <= 0:
                self._reply_req_ids_sending_response.pop(reply_req_id, None)
            else:
                self._reply_req_ids_sending_response[reply_req_id] = count
        self._raise_for_wecom_error(response, "send reply stream")
        logger.debug(
            "[%s] _send_reply_stream: response delivered for req_id=%s chat=%s stream_id=%s",
            self.name, reply_req_id, chat_id, stream_id,
        )
        return response


    async def _send_reply_media_message(
        self,
        reply_req_id: str,
        media_type: str,
        media_id: str,
    ) -> Dict[str, Any]:
        response = await self._send_reply_request(
            reply_req_id,
            {
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
        )
        self._raise_for_wecom_error(response, "send reply media message")
        return response

    async def _send_followup_markdown(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> Optional[SendResult]:
        if not content:
            return None
        result = await self.send(chat_id=chat_id, content=content, reply_to=reply_to)
        if not result.success:
            logger.warning("[%s] Follow-up markdown send failed: %s", self.name, result.error)
        return result

    async def _send_media_source(
        self,
        chat_id: str,
        media_source: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        try:
            prepared = await self._prepare_outbound_media(media_source, file_name=file_name)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("[%s] Failed to prepare outbound media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))

        if prepared["rejected"]:
            await self._send_followup_markdown(
                chat_id,
                f"⚠️ {prepared['reject_reason']}",
                reply_to=reply_to,
            )
            return SendResult(success=False, error=prepared["reject_reason"])

        reply_req_id = self._reply_req_id_for_message(reply_to)

        # Close any active typing stream BEFORE sending media.  In the normal
        # flow stop_typing() runs in base.py's finally block AFTER the response
        # is sent, so at this point the stream may still be in
        # _typing_stream_state_by_chat (not yet moved to _streams_pending_close).
        # We must close it proactively so the typing bubble disappears before
        # the media arrives and does not linger forever for media-only replies.
        stream_to_close: Optional[Tuple[str, str]] = None
        if reply_req_id and chat_id:
            for store in (self._streams_pending_close, self._typing_stream_state_by_chat):
                state = store.pop(chat_id, None)
                if state and state[0] == reply_req_id:
                    stream_to_close = state
                    break
                elif state:
                    # Belongs to a different message — put it back.
                    store[chat_id] = state

            if stream_to_close:
                try:
                    await self._send_reply_request(
                        stream_to_close[0],
                        {
                            "msgtype": "stream",
                            "stream": {
                                "id": stream_to_close[1],
                                "finish": True,
                                "content": STREAM_FINISH_CONTENT,
                            },
                        },
                    )
                    logger.debug(
                        "[%s] Closed typing stream before media send: chat=%s stream_id=%s",
                        self.name, chat_id, stream_to_close[1],
                    )
                except Exception as exc:
                    logger.debug(
                        "[%s] Failed to close typing stream before media: %s",
                        self.name, exc,
                    )

        # Prevent _keep_typing from opening a new stream while the media
        # upload + send is in-flight.  Without this, the multi-second
        # download/upload window allows an orphan typing stream to be created.
        if reply_req_id:
            self._reply_req_ids_sending_response[reply_req_id] = (
                self._reply_req_ids_sending_response.get(reply_req_id, 0) + 1
            )
        try:
            upload_result = await self._upload_media_bytes(
                prepared["data"],
                prepared["final_type"],
                prepared["file_name"],
            )
            if reply_req_id:
                media_response = await self._send_reply_media_message(
                    reply_req_id,
                    prepared["final_type"],
                    upload_result["media_id"],
                )
            else:
                media_response = await self._send_media_message(
                    chat_id,
                    prepared["final_type"],
                    upload_result["media_id"],
                )
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending media to WeCom")
        except Exception as exc:
            logger.error("[%s] Failed to send media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if reply_req_id:
                count = self._reply_req_ids_sending_response.get(reply_req_id, 0) - 1
                if count <= 0:
                    self._reply_req_ids_sending_response.pop(reply_req_id, None)
                else:
                    self._reply_req_ids_sending_response[reply_req_id] = count

        caption_result = None
        downgrade_result = None
        if caption:
            caption_result = await self._send_followup_markdown(
                chat_id,
                caption,
                reply_to=reply_to,
            )
        if prepared["downgraded"] and prepared["downgrade_note"]:
            downgrade_result = await self._send_followup_markdown(
                chat_id,
                f"ℹ️ {prepared['downgrade_note']}",
                reply_to=reply_to,
            )

        return SendResult(
            success=True,
            message_id=self._payload_req_id(media_response) or uuid.uuid4().hex[:12],
            raw_response={
                "upload": upload_result,
                "media": media_response,
                "caption": caption_result.raw_response if caption_result else None,
                "caption_error": caption_result.error if caption_result and not caption_result.success else None,
                "downgrade": downgrade_result.raw_response if downgrade_result else None,
                "downgrade_error": downgrade_result.error if downgrade_result and not downgrade_result.success else None,
            },
        )

    async def _push_via_response_url(
        self,
        chat_id: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Push a reply via webhook response_url if available."""
        if not self._stream_store:
            return False
        reply = await self._stream_store.active_reply_store.retrieve(chat_id)
        if not reply:
            return False

        url = reply["url"]
        try:
            async with self._http_client.post(url, json=payload) as response:
                if response.status == 200:
                    logger.debug("[%s] Pushed reply via response_url for chat=%s", self.name, chat_id)
                    return True
                logger.warning(
                    "[%s] response_url push failed: HTTP %s for chat=%s",
                    self.name, response.status, chat_id,
                )
        except Exception as exc:
            logger.warning("[%s] response_url push error: %s", self.name, exc)
        return False

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

        # Extract template cards if present and mask any remaining card blocks
        # from stream chunks so raw JSON never reaches the user.
        from gateway.platforms.wecom.template_cards import (
            extract_template_cards,
            mask_template_card_blocks,
            save_template_card_to_cache,
        )

        card_result = extract_template_cards(content)
        content = card_result.remaining_text or content
        content = mask_template_card_blocks(content)

        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            logger.debug(
                "[%s] send: chat=%s reply_to=%s initial req_id=%s typing_state=%s pending=%s",
                self.name, chat_id, reply_to, reply_req_id,
                self._typing_stream_state_by_chat.get(chat_id),
                self._streams_pending_close.get(chat_id),
            )

            # If there is no mapped reply req_id but an active typing stream
            # exists for this chat, reuse its req_id so the message replaces
            # the typing indicator instead of leaving it hanging forever.
            if not reply_req_id and chat_id:
                typing_state = self._typing_stream_state_by_chat.get(chat_id)
                if not typing_state:
                    typing_state = self._streams_pending_close.get(chat_id)
                if typing_state:
                    reply_req_id = typing_state[0]
                    logger.debug(
                        "[%s] send: reused typing req_id=%s from state for chat=%s",
                        self.name, reply_req_id, chat_id,
                    )

            chunks = self._chunk_markdown_text(content, chunk_limit=self.MAX_MESSAGE_LENGTH)
            if not chunks:
                return SendResult(success=False, error="empty content")

            # When a message is split into multiple chunks and we have a
            # reply_req_id, do not mix proactive sends (APP_CMD_SEND) with
            # stream replies (APP_CMD_RESPONSE). The two paths have different
            # latencies through WeCom's infrastructure and can arrive out of
            # order, causing later chunks to appear before earlier ones.
            original_reply_req_id = reply_req_id
            if reply_req_id and len(chunks) > 1:
                # Send the first chunk as a stream reply so the typing
                # indicator is replaced with real text instead of an empty
                # ​ block.  Then send the remaining chunks proactively.
                try:
                    await self._send_reply_stream(reply_req_id, chunks[0], chat_id=chat_id)
                    chunks = chunks[1:]
                except RuntimeError as exc:
                    if not isinstance(exc, StreamExpiredError):
                        raise
                    # Stream expired — fall through and send all chunks proactively.
                reply_req_id = None

            # Prevent _keep_typing from recreating a stream while proactive
            # chunks are in-flight.  Without this, send_typing sees no active
            # stream (it was consumed by _send_reply_stream) and creates an
            # orphan that is never closed.
            # Note: _send_reply_stream() above already raised and lowered its
            # own refcount, so by the time we reach here the count is back to
            # zero. This increment is solely for the proactive chunk sends.
            if original_reply_req_id:
                self._reply_req_ids_sending_response[original_reply_req_id] = (
                    self._reply_req_ids_sending_response.get(original_reply_req_id, 0) + 1
                )

            last_response: Optional[Dict[str, Any]] = None
            last_error: Optional[str] = None

            try:
                for idx, chunk in enumerate(chunks):
                    is_last = idx == len(chunks) - 1
                    if reply_req_id and is_last:
                        try:
                            response = await self._send_reply_stream(reply_req_id, chunk, chat_id=chat_id)
                        except RuntimeError as exc:
                            if isinstance(exc, StreamExpiredError):
                                logger.warning(
                                    "[%s] Stream reply expired (846608) for req_id=%s, falling back to proactive send",
                                    self.name, reply_req_id,
                                )
                                pushed = await self._push_via_response_url(
                                    chat_id,
                                    {
                                        "msgtype": "markdown",
                                        "markdown": {"content": chunk[:self.MAX_MESSAGE_LENGTH]},
                                    },
                                )
                                if pushed:
                                    response = {"body": {"errcode": 0}}
                                else:
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
                        # Try response_url push before falling back to proactive aibot_send_msg
                        pushed = await self._push_via_response_url(
                            chat_id,
                            {
                                "msgtype": "markdown",
                                "markdown": {"content": chunk[:self.MAX_MESSAGE_LENGTH]},
                            },
                        )
                        if pushed:
                            response = {"body": {"errcode": 0}}
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
            finally:
                if original_reply_req_id:
                    count = self._reply_req_ids_sending_response.get(original_reply_req_id, 0) - 1
                    if count <= 0:
                        self._reply_req_ids_sending_response.pop(original_reply_req_id, None)
                    else:
                        self._reply_req_ids_sending_response[original_reply_req_id] = count

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
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending message to WeCom")
        except Exception as exc:
            logger.error("[%s] Send failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))

    async def send_thinking(
        self,
        chat_id: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a thinking placeholder to indicate the bot is processing."""
        del metadata
        return await self.send(chat_id, "<think></think>", reply_to=reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata

        result = await self._send_media_source(
            chat_id=chat_id,
            media_source=image_url,
            caption=caption,
            reply_to=reply_to,
        )
        if result.success or not self._looks_like_url(image_url):
            return result

        logger.warning("[%s] Falling back to text send for image URL %s: %s", self.name, image_url, result.error)
        fallback_text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback_text, reply_to=reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=image_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=audio_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=video_path,
            caption=caption,
            reply_to=reply_to,
        )

    # ------------------------------------------------------------------
    # Native streaming interface (SDK-backed)
    # ------------------------------------------------------------------

    def supports_native_streaming(self) -> bool:
        return _SDK_AVAILABLE and self._sdk_client is not None

    async def send_stream_chunk(
        self, stream_id: str, content: str, *, finish: bool = False
    ) -> SendResult:
        if not self._sdk_client:
            return SendResult(success=False, error="SDK client not connected")

        try:
            await self._sdk_client.reply_stream(
                frame=self._last_inbound_frame or {"headers": {}},
                stream_id=stream_id,
                content=content,
                finish=finish,
            )
            return SendResult(success=True, message_id=stream_id)
        except Exception as exc:
            logger.warning("[%s] send_stream_chunk failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))

    def pause_typing_for_chat(self, chat_id: str) -> None:
        """Pause typing and schedule the active WeCom stream for closing.

        This is a sync method (called from the agent thread), so it cannot
        send the ``finish=True`` frame directly.  Instead it moves the stream
        state into ``_streams_pending_close`` which is drained on the next
        async ``send_typing`` / ``stop_typing`` call from ``_keep_typing``.
        """
        super().pause_typing_for_chat(chat_id)
        state = self._typing_stream_state_by_chat.pop(chat_id, None)
        if state:
            logger.debug(
                "[%s] pause_typing_for_chat: moved chat=%s state=%s to pending_close",
                self.name, chat_id, state,
            )
            self._streams_pending_close[chat_id] = state
        else:
            logger.debug(
                "[%s] pause_typing_for_chat: no active stream for chat=%s",
                self.name, chat_id,
            )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Emit WeCom's stream placeholder so clients show waiting animation."""
        # Drain any streams that were orphaned by pause_typing_for_chat.
        await self._close_pending_streams()

        if not chat_id:
            logger.debug("[%s] send_typing: no chat_id, returning", self.name)
            return

        current_state = self._typing_stream_state_by_chat.get(chat_id)
        if current_state:
            logger.debug(
                "[%s] send_typing: chat=%s already has active stream state=%s, skip",
                self.name, chat_id, current_state,
            )
            return

        metadata = metadata if isinstance(metadata, dict) else {}
        message_id = str(metadata.get("message_id") or "").strip()

        reply_req_id = self._reply_req_id_for_message(message_id)
        if not reply_req_id and chat_id:
            # Fall back to the most recent message in this chat so resumed
            # typing after /approve attaches to the fresh /approve message
            # instead of an expired or scrolled-out original message.
            reply_req_id = self._last_reply_req_id_per_chat.get(chat_id)
        if not reply_req_id:
            # No passive-reply req_id available — fall back to proactive send.
            logger.debug(
                "[%s] send_typing: chat=%s msg_id=%s no reply_req_id, trying proactive",
                self.name, chat_id, message_id,
            )
            try:
                await self._send_proactive_typing(chat_id)
            except Exception as exc:
                logger.info(
                    "[%s] send_typing: no reply req_id for msg_id=%s, proactive typing unavailable: %s",
                    self.name, message_id, exc,
                )
            return

        # If a response is currently being sent for this req_id (HTTP in
        # flight), do not open a new typing stream — it would race with
        # _send_reply_stream and create an orphan. Once the response is
        # delivered, typing is allowed to resume for multi-turn analysis.
        if reply_req_id in self._reply_req_ids_sending_response:
            logger.debug(
                "[%s] send_typing: chat=%s req_id=%s response is in-flight, skip",
                self.name, chat_id, reply_req_id,
            )
            return

        stream_id = self._new_req_id("stream")
        logger.debug(
            "[%s] send_typing: chat=%s opening stream_id=%s for req_id=%s",
            self.name, chat_id, stream_id, reply_req_id,
        )
        try:
            response = await self._send_reply_request(
                reply_req_id,
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "finish": False,
                        "content": "<think></think>",
                    },
                },
            )
            self._raise_for_wecom_error(response, "send typing stream")
            self._typing_stream_state_by_chat[chat_id] = (reply_req_id, stream_id)
            logger.debug(
                "[%s] send_typing: chat=%s stream_id=%s opened successfully",
                self.name, chat_id, stream_id,
            )
        except RuntimeError as exc:
            if isinstance(exc, StreamExpiredError):
                logger.warning(
                    "[%s] Typing stream expired (846608) for req_id=%s, clearing cache",
                    self.name, reply_req_id,
                )
                self._reply_req_ids.pop(message_id, None)
                self._last_reply_req_id_per_chat.pop(chat_id, None)
            else:
                logger.debug("[%s] Failed to send typing placeholder to %s: %s", self.name, chat_id, exc)
        except Exception as exc:
            logger.debug("[%s] Failed to send typing placeholder to %s: %s", self.name, chat_id, exc)

    async def _send_proactive_typing(self, chat_id: str) -> None:
        """Send a proactive typing indicator via ``aibot_send_msg``.

        WeCom does not support a native proactive typing API, so we send a
        minimal markdown message that the client may render as an ephemeral
        indicator.  This is a best-effort fallback when no ``reply_req_id``
        is available for the passive-reply stream path.
        """
        try:
            response = await self._send_request(
                APP_CMD_SEND,
                {
                    "chatid": chat_id,
                    "msgtype": "markdown",
                    "markdown": {"content": "<think></think>"},
                },
            )
            self._raise_for_wecom_error(response, "send proactive typing")
            logger.debug(
                "[%s] _send_proactive_typing: chat=%s sent successfully",
                self.name, chat_id,
            )
        except Exception as exc:
            logger.debug("[%s] _send_proactive_typing failed for %s: %s", self.name, chat_id, exc)

    async def stop_typing(self, chat_id: str) -> None:
        """Move the active WeCom typing stream to pending-close.

        Instead of sending ``finish=True`` immediately (which would close the
        bubble with empty content), we move the stream state into
        ``_streams_pending_close`` so that ``_send_reply_stream`` can reuse
        the same ``stream_id`` when the actual response is delivered.

        This is critical because ``stop_typing`` is called from
        ``_handle_message_with_agent`` *before* ``send()`` runs in
        ``_process_message_background``.  If we closed the stream here, the
        final response would create a brand-new message instead of replacing
        the bubble.

        Orphaned pending streams are eventually closed by
        ``_close_pending_streams`` (drained on the next ``send_typing`` call
        or when ``_send_reply_stream`` finishes for a different chat).
        """
        # Drain orphaned streams from OTHER chats only; skip current chat so
        # _send_reply_stream can reuse its stream_id for the real response.
        await self._close_pending_streams(skip_chat_id=chat_id)

        state = self._typing_stream_state_by_chat.pop(chat_id, None)
        if not state:
            logger.debug(
                "[%s] stop_typing: chat=%s no active stream, nothing to do",
                self.name, chat_id,
            )
            return

        reply_req_id, stream_id = state
        logger.debug(
            "[%s] stop_typing: chat=%s moving stream_id=%s for req_id=%s to pending_close",
            self.name, chat_id, stream_id, reply_req_id,
        )
        self._streams_pending_close[chat_id] = state

    async def _close_pending_streams(self, skip_chat_id: Optional[str] = None) -> None:
        """Send ``finish=True`` for streams orphaned by ``pause_typing_for_chat``."""
        chat_ids = list(self._streams_pending_close.keys())
        if chat_ids:
            logger.debug(
                "[%s] _close_pending_streams: closing %d pending stream(s) skip=%s",
                self.name, len(chat_ids), skip_chat_id,
            )
        for _chat_id in chat_ids:
            state = self._streams_pending_close.pop(_chat_id, None)
            if state is None:
                continue
            req_id, stream_id = state
            if skip_chat_id and _chat_id == skip_chat_id:
                self._streams_pending_close[_chat_id] = state
                logger.debug(
                    "[%s] _close_pending_streams: skipped chat=%s (current response target)",
                    self.name, _chat_id,
                )
                continue
            logger.debug(
                "[%s] _close_pending_streams: closing chat=%s stream_id=%s req_id=%s",
                self.name, _chat_id, stream_id, req_id,
            )
            try:
                resp = await self._send_reply_request(
                    req_id,
                    {
                        "msgtype": "stream",
                        "stream": {"id": stream_id, "finish": True, "content": STREAM_FINISH_CONTENT},
                    },
                )
                self._raise_for_wecom_error(resp, "close pending typing stream")
                logger.debug(
                    "[%s] _close_pending_streams: closed chat=%s stream_id=%s successfully",
                    self.name, _chat_id, stream_id,
                )
            except StreamExpiredError:
                logger.debug(
                    "[%s] _close_pending_streams: stream already expired for chat=%s stream_id=%s, removing",
                    self.name, _chat_id, stream_id,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to close pending stream for %s (will retry): %s",
                    self.name, _chat_id, exc,
                )
                self._streams_pending_close[_chat_id] = state
                break

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return minimal chat info."""
        return {
            "name": chat_id,
            "type": "group" if chat_id and chat_id.lower().startswith("group") else "dm",
        }


# ------------------------------------------------------------------
# QR code scan flow for obtaining bot credentials
# ------------------------------------------------------------------

_QR_GENERATE_URL = "https://work.weixin.qq.com/ai/qc/generate"
_QR_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"
_QR_CODE_PAGE = "https://work.weixin.qq.com/ai/qc/gen?source=hermes&scode="
_QR_POLL_INTERVAL = 3  # seconds
_QR_POLL_TIMEOUT = 300  # 5 minutes


def qr_scan_for_bot_info(
    *,
    timeout_seconds: int = _QR_POLL_TIMEOUT,
) -> Optional[Dict[str, str]]:
    """Run the WeCom QR scan flow to obtain bot_id and secret.

    Fetches a QR code from WeCom, renders it in the terminal, and polls
    until the user scans it or the timeout expires.

    Returns ``{"bot_id": ..., "secret": ...}`` on success, ``None`` on
    failure or timeout.

    Note: the ``work.weixin.qq.com/ai/qc/{generate,query_result}`` endpoints
    used here are not part of WeCom's public developer API — they back the
    admin-console web UI's bot-creation flow and may change without notice.
    The same pattern is used by the feishu/dingtalk QR setup wizards.
    """
    try:
        import urllib.request
        import urllib.parse
    except ImportError:  # pragma: no cover
        logger.error("urllib is required for WeCom QR scan")
        return None

    generate_url = f"{_QR_GENERATE_URL}?source=hermes"

    # ── Step 1: Fetch QR code ──
    print("  Connecting to WeCom...", end="", flush=True)
    try:
        req = urllib.request.Request(generate_url, headers={"User-Agent": "HermesAgent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.error("WeCom QR: failed to fetch QR code: %s", exc)
        print(f" failed: {exc}")
        return None

    data = raw.get("data") or {}
    scode = str(data.get("scode") or "").strip()
    auth_url = str(data.get("auth_url") or "").strip()

    if not scode or not auth_url:
        logger.error("WeCom QR: unexpected response format: %s", raw)
        print(" failed: unexpected response format")
        return None

    print(" done.")

    # ── Step 2: Render QR code in terminal ──
    print()
    qr_rendered = False
    try:
        import qrcode as _qrcode
        qr = _qrcode.QRCode()
        qr.add_data(auth_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        qr_rendered = True
    except ImportError:
        pass
    except Exception:
        pass

    page_url = f"{_QR_CODE_PAGE}{urllib.parse.quote(scode)}"
    if qr_rendered:
        print(f"\n  Scan the QR code above, or open this URL directly:\n  {page_url}")
    else:
        print(f"  Open this URL in WeCom on your phone:\n\n  {page_url}\n")
        print("  Tip: pip install qrcode  to display a scannable QR code here next time")
    print()
    print("  Fetching configuration results...", end="", flush=True)

    # ── Step 3: Poll for result ──
    import time
    deadline = time.time() + timeout_seconds
    query_url = f"{_QR_QUERY_URL}?scode={urllib.parse.quote(scode)}"
    poll_count = 0

    while time.time() < deadline:
        try:
            req = urllib.request.Request(query_url, headers={"User-Agent": "HermesAgent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("WeCom QR poll error: %s", exc)
            time.sleep(_QR_POLL_INTERVAL)
            continue

        poll_count += 1
        # Print a dot on every poll so progress is visible within 3s.
        print(".", end="", flush=True)

        result_data = result.get("data") or {}
        status = str(result_data.get("status") or "").lower()

        if status == "success":
            print()  # newline after "Fetching configuration results..." dots
            bot_info = result_data.get("bot_info") or {}
            bot_id = str(bot_info.get("botid") or bot_info.get("bot_id") or "").strip()
            secret = str(bot_info.get("secret") or "").strip()
            if bot_id and secret:
                return {"bot_id": bot_id, "secret": secret}
            logger.warning(
                "WeCom QR: scan reported success but bot_info missing or incomplete: %s",
                result_data,
            )
            print(
                "  QR scan reported success but no bot credentials were returned.\n"
                "  This usually means the bot was not actually created on the WeCom side.\n"
                "  Falling back to manual credential entry."
            )
            return None

        time.sleep(_QR_POLL_INTERVAL)

    print()  # newline after dots
    print(f"  QR scan timed out ({timeout_seconds // 60} minutes). Please try again.")
    return None
