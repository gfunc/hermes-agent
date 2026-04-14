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
