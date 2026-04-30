"""WeCom (Enterprise WeChat) platform adapter package."""

from gateway.platforms.wecom.adapter import (
    APP_CMD_PING,
    APP_CMD_SEND,
    APP_CMD_UPLOAD_MEDIA_CHUNK,
    APP_CMD_UPLOAD_MEDIA_FINISH,
    APP_CMD_UPLOAD_MEDIA_INIT,
    check_wecom_requirements,
    MediaOversizeError,
    REQUEST_TIMEOUT_SECONDS,
    StreamExpiredError,
    WeComAdapter,
)
from gateway.platforms.base import MessageType

__all__ = [
    "APP_CMD_PING",
    "APP_CMD_SEND",
    "APP_CMD_UPLOAD_MEDIA_CHUNK",
    "APP_CMD_UPLOAD_MEDIA_FINISH",
    "APP_CMD_UPLOAD_MEDIA_INIT",
    "check_wecom_requirements",
    "MediaOversizeError",
    "MessageType",
    "REQUEST_TIMEOUT_SECONDS",
    "StreamExpiredError",
    "WeComAdapter",
]
