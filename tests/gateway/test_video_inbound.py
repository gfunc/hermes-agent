"""Tests for video message handling in _prepare_inbound_message_text."""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.WECOM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.WECOM,
        chat_id="c1",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


@pytest.mark.asyncio
async def test_video_message_includes_file_note():
    """VIDEO messages with media_urls must include a context note so the
    agent knows a video was sent. Without this, the message_text is empty
    and the agent sees nothing."""
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="",
        message_type=MessageType.VIDEO,
        source=source,
        media_urls=["/tmp/cache/documents/doc_ea8abac703e8_meeting_01.mp4"],
        media_types=["application/octet-stream"],
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result is not None
    assert "video" in result.lower()
    assert "meeting_01.mp4" in result
