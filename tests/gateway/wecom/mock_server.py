"""Reusable mock WeCom WebSocket server for integration tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web, WSMsgType


class MockWeComServer:
    """
    Lightweight aiohttp mock of the WeCom AI Bot WebSocket gateway.

    Supports scenarios:
    - normal: responds to subscribe, echoes pings, can push messages
    - silent: accepts connection but sends nothing
    - close_after_auth: closes cleanly after subscribe ack
    - close_silent: aborts the TCP socket without a WS close frame
    """

    def __init__(self, scenario: str = "normal", delay_auth_seconds: float = 0.0):
        self.scenario = scenario
        self.delay_auth_seconds = delay_auth_seconds
        self.app = web.Application()
        self.app.router.add_get("/ws", self._ws_handler)
        self._clients: List[web.WebSocketResponse] = []
        self._received: List[Dict[str, Any]] = []
        self._server: Optional[web.AppRunner] = None
        self.ws_url: str = ""
        self._drop_acks_for: set[str] = set()
        self._delay_acks_for: Dict[str, float] = {}

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.scenario == "close_silent":
            if request.transport is not None:
                request.transport.close()
            return ws
        self._clients.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    self._received.append(payload)
                    await self._handle_payload(ws, payload)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            self._clients.remove(ws)
        return ws

    async def _handle_payload(self, ws: web.WebSocketResponse, payload: Dict[str, Any]) -> None:
        cmd = payload.get("cmd")
        req_id = payload.get("headers", {}).get("req_id", "")

        if cmd == "aibot_subscribe":
            if self.delay_auth_seconds > 0:
                await asyncio.sleep(self.delay_auth_seconds)
            await ws.send_json({
                "cmd": "aibot_subscribe",
                "headers": {"req_id": req_id},
                "body": {"errcode": 0},
            })
            if self.scenario == "close_after_auth":
                await ws.close()
            return

        if cmd == "ping":
            return

        if cmd in ("aibot_send_msg", "aibot_respond_msg"):
            if req_id in self._drop_acks_for:
                return
            delay = self._delay_acks_for.get(req_id, 0.0)
            if delay > 0:
                await asyncio.sleep(delay)
            await ws.send_json({
                "cmd": cmd,
                "headers": {"req_id": req_id},
                "body": {"errcode": 0},
            })
            return

        if cmd == "aibot_get_mcp_config":
            category = payload.get("body", {}).get("biz_type", "unknown")
            await ws.send_json({
                "cmd": "aibot_get_mcp_config",
                "headers": {"req_id": req_id},
                "body": {"errcode": 0, "url": f"http://localhost/mcp/{category}"},
            })
            return

    def drop_acks_for(self, *req_ids: str) -> None:
        self._drop_acks_for.update(req_ids)

    def delay_acks_for(self, req_id: str, seconds: float) -> None:
        self._delay_acks_for[req_id] = seconds

    async def send_event(self, event_name: str, body_extra: Optional[Dict[str, Any]] = None) -> None:
        body = dict(body_extra or {})
        body["event"] = event_name
        for ws in list(self._clients):
            if ws.closed:
                continue
            await ws.send_json({
                "cmd": "aibot_event_callback",
                "headers": {"req_id": f"mock-event-{event_name}"},
                "body": body,
            })

    async def send_callback(self, chatid: str, text: str, msgid: str = "mock-msg-1") -> None:
        for ws in list(self._clients):
            if ws.closed:
                continue
            await ws.send_json({
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": f"mock-req-{msgid}"},
                "body": {
                    "msgid": msgid,
                    "chatid": chatid,
                    "chattype": "single",
                    "from": {"userid": "mock_user"},
                    "msgtype": "text",
                    "text": {"content": text},
                },
            })

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        self._server = runner
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.ws_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._server:
            await self._server.cleanup()
            self._server = None

    @asynccontextmanager
    async def run(self):
        await self.start()
        try:
            yield self
        finally:
            await self.stop()
