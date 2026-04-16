"""Tests for WeCom WebSocket reliability improvements."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_mock_server_starts_and_stops():
    from tests.gateway.mock_wecom_server import MockWeComServer

    server = MockWeComServer()
    await server.start()
    assert server.ws_url.startswith("http://127.0.0.1:")
    await server.stop()
