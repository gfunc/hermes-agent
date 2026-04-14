"""WeCom callback-mode adapter for self-built enterprise applications.

Unlike the bot/websocket adapter in ``wecom.py``, this handles the standard
WeCom callback flow: WeCom POSTs encrypted XML to an HTTP endpoint, the
adapter decrypts it, queues the message for the agent, and immediately
acknowledges.  The agent's reply is delivered later via the proactive
``message/send`` API using an access-token.

Supports multiple self-built apps under one gateway instance, scoped by
``corp_id:user_id`` to avoid cross-corp collisions.
"""

from __future__ import annotations

import asyncio
import logging
import socket as _socket
import time
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms.wecom_accounts import resolve_wecom_accounts, WeComAccount
from gateway.platforms.wecom_crypto import WXBizMsgCrypt, WeComCryptoError

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8645
DEFAULT_PATH = "/wecom/callback"
ACCESS_TOKEN_TTL_SECONDS = 7200
MESSAGE_DEDUP_TTL_SECONDS = 300


def check_wecom_callback_requirements() -> bool:
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


class WecomCallbackAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM_CALLBACK)
        extra = config.extra or {}
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        self._path = str(extra.get("path") or DEFAULT_PATH)
        self._accounts: List[WeComAccount] = resolve_wecom_accounts(config)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_messages: Dict[str, float] = {}
        self._user_account_map: Dict[str, str] = {}
        self._access_tokens: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Account helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _user_account_key(corp_id: str, user_id: str) -> str:
        return f"{corp_id}:{user_id}" if corp_id else user_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self._accounts:
            logger.warning("[WecomCallback] No callback accounts configured")
            return False
        if not check_wecom_callback_requirements():
            logger.warning("[WecomCallback] aiohttp/httpx not installed")
            return False

        # Quick port-in-use check.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[WecomCallback] Port %d already in use", self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            self._http_client = httpx.AsyncClient(timeout=20.0)
            self._app = web.Application()
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get(self._path, self._handle_verify)
            self._app.router.add_post(self._path, self._handle_callback)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._mark_connected()
            logger.info(
                "[WecomCallback] HTTP server listening on %s:%s%s",
                self._host, self._port, self._path,
            )
            for account in self._accounts:
                try:
                    await self._refresh_access_token(account)
                except Exception as exc:
                    logger.warning(
                        "[WecomCallback] Initial token refresh failed for account '%s': %s",
                        account.account_id, exc,
                    )
            return True
        except Exception:
            await self._cleanup()
            logger.exception("[WecomCallback] Failed to start")
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._cleanup()
        self._mark_disconnected()
        logger.info("[WecomCallback] Disconnected")

    async def _cleanup(self) -> None:
        self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # Outbound: proactive send via access-token API
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        account = self._resolve_account_for_chat(chat_id)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            token = await self._get_access_token(account)
            payload = {
                "touser": touser,
                "msgtype": "text",
                "agentid": int(account.agent_id or 0),
                "text": {"content": content[:2048]},
                "safe": 0,
            }
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

    def _resolve_account_for_chat(self, chat_id: str) -> WeComAccount:
        """Pick the account associated with *chat_id*, falling back sensibly."""
        account_id = self._user_account_map.get(chat_id)
        if not account_id and ":" not in chat_id:
            # Legacy bare user_id — try to find a unique match.
            matching = [k for k in self._user_account_map if k.endswith(f":{chat_id}")]
            if len(matching) == 1:
                account_id = self._user_account_map.get(matching[0])
        account = self._get_account_by_id(account_id) if account_id else None
        return account or self._accounts[0]

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # Inbound: HTTP callback handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": "wecom_callback"})

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """GET endpoint — WeCom URL verification handshake."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")
        for account in self._accounts:
            try:
                crypt = self._crypt_for_account(account)
                plain = crypt.verify_url(msg_signature, timestamp, nonce, echostr)
                return web.Response(text=plain, content_type="text/plain")
            except Exception:
                continue
        return web.Response(status=403, text="signature verification failed")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """POST endpoint — receive an encrypted message callback."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        body = await request.text()

        for account in self._accounts:
            try:
                decrypted = self._decrypt_request(
                    account, body, msg_signature, timestamp, nonce,
                )
                event = self._build_event(account, decrypted)
                if event is not None:
                    # Record which account this user belongs to.
                    if event.source and event.source.user_id:
                        map_key = self._user_account_key(
                            account.corp_id or "", event.source.user_id,
                        )
                        self._user_account_map[map_key] = account.account_id
                    await self._message_queue.put(event)
                # Immediately acknowledge — the agent's reply will arrive
                # later via the proactive message/send API.
                return web.Response(text="success", content_type="text/plain")
            except WeComCryptoError:
                continue
            except Exception:
                logger.exception("[WecomCallback] Error handling message")
                break
        return web.Response(status=400, text="invalid callback payload")

    async def _poll_loop(self) -> None:
        """Drain the message queue and dispatch to the gateway runner."""
        while True:
            event = await self._message_queue.get()
            try:
                task = asyncio.create_task(self.handle_message(event))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                logger.exception("[WecomCallback] Failed to enqueue event")

    # ------------------------------------------------------------------
    # XML / crypto helpers
    # ------------------------------------------------------------------

    def _decrypt_request(
        self, account: WeComAccount, body: str,
        msg_signature: str, timestamp: str, nonce: str,
    ) -> str:
        root = ET.fromstring(body)
        encrypt = root.findtext("Encrypt", default="")
        crypt = self._crypt_for_account(account)
        return crypt.decrypt(msg_signature, timestamp, nonce, encrypt).decode("utf-8")

    def _build_event(self, account: WeComAccount, xml_text: str) -> Optional[MessageEvent]:
        root = ET.fromstring(xml_text)
        msg_type = (root.findtext("MsgType") or "").lower()
        # Silently acknowledge lifecycle events.
        if msg_type == "event":
            event_name = (root.findtext("Event") or "").lower()
            if event_name in {"enter_agent", "subscribe"}:
                return None
        if msg_type not in {"text", "event"}:
            return None

        user_id = root.findtext("FromUserName", default="")
        corp_id = root.findtext("ToUserName", default=account.corp_id or "")
        scoped_chat_id = self._user_account_key(corp_id, user_id)
        content = root.findtext("Content", default="").strip()
        if not content and msg_type == "event":
            content = "/start"
        msg_id = (
            root.findtext("MsgId")
            or f"{user_id}:{root.findtext('CreateTime', default='0')}"
        )
        source = self.build_source(
            chat_id=scoped_chat_id,
            chat_name=user_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_id,
        )
        return MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=xml_text,
            message_id=msg_id,
        )

    def _crypt_for_account(self, account: WeComAccount) -> WXBizMsgCrypt:
        return WXBizMsgCrypt(
            token=account.token or "",
            encoding_aes_key=account.encoding_aes_key or "",
            receive_id=account.receive_id or account.corp_id or "",
        )

    def _get_account_by_id(self, account_id: Optional[str]) -> Optional[WeComAccount]:
        if not account_id:
            return None
        for account in self._accounts:
            if account.account_id == account_id:
                return account
        return None

    # ------------------------------------------------------------------
    # Access-token management
    # ------------------------------------------------------------------

    async def _get_access_token(self, account: WeComAccount) -> str:
        cached = self._access_tokens.get(account.account_id)
        now = time.time()
        if cached and cached.get("expires_at", 0) > now + 60:
            return cached["token"]
        return await self._refresh_access_token(account)

    async def _refresh_access_token(self, account: WeComAccount) -> str:
        resp = await self._http_client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": account.corp_id,
                "corpsecret": account.corp_secret,
            },
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom token refresh failed: {data}")
        token = data["access_token"]
        expires_in = int(data.get("expires_in", ACCESS_TOKEN_TTL_SECONDS))
        self._access_tokens[account.account_id] = {
            "token": token,
            "expires_at": time.time() + expires_in,
        }
        logger.info(
            "[WecomCallback] Token refreshed for account '%s' (corp=%s), expires in %ss",
            account.account_id,
            account.corp_id,
            expires_in,
        )
        return token
