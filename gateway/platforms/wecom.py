from __future__ import annotations

# Allow this module to also act as a package so submodules in
# gateway/platforms/wecom/ can be imported as gateway.platforms.wecom.X.
import os as _os
__path__ = [_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "wecom")]

# Re-export main adapter and constants for backward compatibility.
# All imports should migrate to gateway.platforms.wecom.adapter directly.
from gateway.platforms.wecom.adapter import (  # noqa: F401
    ABSOLUTE_MAX_BYTES,
    APP_CMD_PING,
    APP_CMD_SEND,
    APP_CMD_UPLOAD_MEDIA_CHUNK,
    APP_CMD_UPLOAD_MEDIA_FINISH,
    APP_CMD_UPLOAD_MEDIA_INIT,
    MessageType,
    REQUEST_TIMEOUT_SECONDS,
    WeComAdapter,
    check_wecom_requirements,
)
