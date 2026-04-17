from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
VOICE_SUPPORTED_MIMES = {"audio/amr"}

_MAGIC_BYTES = [
    ((b"\x89PNG\r\n\x1a\n",), "image/png"),
    ((b"\xff\xd8\xff",), "image/jpeg"),
    ((b"GIF87a", b"GIF89a"), "image/gif"),
    ((b"RIFF",), "image/webp", lambda d: len(d) >= 12 and d[8:12] == b"WEBP"),
    ((b"%PDF",), "application/pdf"),
    ((b"\x1aE\xdf\xa3",), "video/webm"),
    ((b"\x00\x00\x00 ", b"\x00\x00\x00 ftyp"), "video/mp4", lambda d: len(d) >= 12 and b"ftyp" in d[4:12]),
    ((b"ID3",), "audio/mpeg"),
    ((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), "audio/mpeg"),
    ((b"OggS",), "audio/ogg"),
    ((b"fLaC",), "audio/flac"),
    ((b"RIFF",), "audio/wav", lambda d: len(d) >= 12 and d[8:12] == b"WAVE"),
]


def detect_mime_from_bytes(data: bytes) -> Optional[str]:
    if not data:
        return None
    for entry in _MAGIC_BYTES:
        magics, mime = entry[0], entry[1]
        checker = entry[2] if len(entry) > 2 else None
        if any(data.startswith(m) for m in magics):
            if checker is None or checker(data):
                return mime
    return None


def detect_wecom_media_type(content_type: str) -> str:
    mime = str(content_type or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/") or mime == "application/ogg":
        return "voice"
    return "file"


def apply_file_size_limits(
    file_size: int,
    detected_type: str,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    file_size_mb = file_size / (1024 * 1024)
    normalized_type = str(detected_type or "file").lower()
    normalized_content_type = str(content_type or "").strip().lower()

    if file_size > ABSOLUTE_MAX_BYTES:
        return {
            "final_type": normalized_type,
            "rejected": True,
            "reject_reason": (
                f"文件大小 {file_size_mb:.2f}MB 超过了企业微信允许的最大限制 20MB，无法发送。"
                "请尝试压缩文件或减小文件大小。"
            ),
            "downgraded": False,
            "downgrade_note": None,
        }

    if normalized_type == "image" and file_size > IMAGE_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"图片大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送",
        }

    if normalized_type == "video" and file_size > VIDEO_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"视频大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送",
        }

    if normalized_type == "voice":
        if normalized_content_type and normalized_content_type not in VOICE_SUPPORTED_MIMES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": (
                    f"语音格式 {normalized_content_type} 不支持，企微仅支持 AMR 格式，已转为文件格式发送"
                ),
            }
        if file_size > VOICE_MAX_BYTES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": f"语音大小 {file_size_mb:.2f}MB 超过 2MB 限制，已转为文件格式发送",
            }

    return {
        "final_type": normalized_type,
        "rejected": False,
        "reject_reason": None,
        "downgraded": False,
        "downgrade_note": None,
    }


class MediaPreparer:
    """Prepare outbound media for WeCom with magic-byte detection and size enforcement."""

    def __init__(
        self,
        http_client,
        *,
        media_local_roots: Optional[List[str]] = None,
    ):
        self._http_client = http_client
        self._media_local_roots = [Path(r).expanduser().resolve() for r in (media_local_roots or [])]

    async def prepare(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        source = str(media_source or "").strip()
        if not source:
            raise ValueError("media source is required")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            data, headers = await self._download_remote_bytes(source)
            resolved_name = file_name or self._guess_filename(
                source, headers.get("content-disposition"), headers.get("content-type", "")
            )
            content_type = self._normalize_content_type(headers.get("content-type", ""), resolved_name, data)
        elif parsed.scheme == "file":
            local_path = Path(unquote(parsed.path)).expanduser().resolve()
            data, resolved_name, content_type = self._read_local_file(local_path, file_name)
        else:
            local_path = Path(source).expanduser().resolve()
            data, resolved_name, content_type = self._read_local_file(local_path, file_name)

        detected_type = detect_wecom_media_type(content_type)
        size_check = apply_file_size_limits(len(data), detected_type, content_type)

        return {
            "data": data,
            "content_type": content_type,
            "file_name": resolved_name,
            "detected_type": detected_type,
            **size_check,
        }

    async def _download_remote_bytes(self, url: str) -> Tuple[bytes, Dict[str, str]]:
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url[:80]}")

        async with self._http_client.stream(
            "GET", url, headers={"User-Agent": "HermesAgent/1.0", "Accept": "*/*"}
        ) as response:
            response.raise_for_status()
            headers = {k.lower(): v for k, v in response.headers.items()}
            content_length = headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > ABSOLUTE_MAX_BYTES:
                raise ValueError(f"Remote media exceeds limit: {content_length} bytes")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > ABSOLUTE_MAX_BYTES:
                    raise ValueError(f"Remote media exceeds limit while downloading: {len(data)} bytes")
            return bytes(data), headers

    def _read_local_file(self, path: Path, file_name: Optional[str]) -> Tuple[bytes, str, str]:
        if self._media_local_roots:
            if not any(str(path).startswith(str(root)) for root in self._media_local_roots):
                raise PermissionError(f"Local media path {path} is outside allowed roots")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Media file not found: {path}")
        data = path.read_bytes()
        resolved_name = file_name or path.name
        content_type = self._normalize_content_type("", resolved_name, data)
        return data, resolved_name, content_type

    @staticmethod
    def _normalize_content_type(content_type: str, filename: str, data: bytes) -> str:
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        detected = detect_mime_from_bytes(data)
        if not normalized:
            return detected or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if normalized in {"application/octet-stream", "text/plain"}:
            return detected or normalized
        return normalized

    @staticmethod
    def _guess_filename(url: str, content_disposition: Optional[str], content_type: str) -> str:
        import re
        if content_disposition:
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)
        name = Path(urlparse(url).path).name or "document"
        if "." not in name:
            ext = mimetypes.guess_extension(content_type) or ".bin"
            name = f"{name}{ext}"
        return name
