import pytest

from gateway.platforms.wecom.media import (
    detect_mime_from_bytes,
    apply_file_size_limits,
    detect_wecom_media_type,
    WeComMediaLimits,
)


def test_detect_png():
    data = b"\x89PNG\r\n\x1a\n" + b"fake"
    assert detect_mime_from_bytes(data) == "image/png"


def test_detect_jpeg():
    data = b"\xff\xd8\xff" + b"fake"
    assert detect_mime_from_bytes(data) == "image/jpeg"


def test_detect_pdf():
    data = b"%PDF-1.4"
    assert detect_mime_from_bytes(data) == "application/pdf"


def test_image_downgrade_over_10mb():
    result = apply_file_size_limits(11 * 1024 * 1024, "image")
    assert result["downgraded"] is True
    assert result["final_type"] == "file"


def test_voice_rejects_non_amr():
    result = apply_file_size_limits(1024, "voice", content_type="audio/mp3")
    assert result["downgraded"] is True
    assert "AMR" in result["downgrade_note"]


def test_media_limits_enforces_wecom_size_limits():
    """WeComMediaLimits should reject files exceeding per-type max sizes."""
    limits = WeComMediaLimits()

    # Image over 10MB should be rejected
    assert limits.is_allowed("image", 10 * 1024 * 1024 + 1) is False
    assert limits.is_allowed("image", 5 * 1024 * 1024) is True

    # Video over 10MB should be rejected
    assert limits.is_allowed("video", 10 * 1024 * 1024 + 1) is False
    assert limits.is_allowed("video", 5 * 1024 * 1024) is True

    # Voice over 2MB should be rejected
    assert limits.is_allowed("voice", 2 * 1024 * 1024 + 1) is False
    assert limits.is_allowed("voice", 1 * 1024 * 1024) is True

    # File over 20MB should be rejected
    assert limits.is_allowed("file", 20 * 1024 * 1024 + 1) is False
    assert limits.is_allowed("file", 10 * 1024 * 1024) is True


def test_media_limits_allows_unknown_types():
    """Unknown media types should be allowed (let server decide)."""
    limits = WeComMediaLimits()
    assert limits.is_allowed("unknown", 100 * 1024 * 1024) is True


def test_media_limits_returns_max_size():
    limits = WeComMediaLimits()
    assert limits.max_size("image") == 10 * 1024 * 1024
    assert limits.max_size("voice") == 2 * 1024 * 1024
    assert limits.max_size("unknown") is None


def test_media_preparer_blocks_path_traversal():
    """Paths outside media_local_roots must be rejected even with startswith-like prefixes."""
    import tempfile
    from pathlib import Path
    from gateway.platforms.wecom.media import MediaPreparer

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "allowed"
        root.mkdir()
        # Create a file in a similarly-named but different directory
        evil_dir = Path(tmpdir) / "allowed-evil"
        evil_dir.mkdir()
        evil_file = evil_dir / "secret.txt"
        evil_file.write_text("secret")

        preparer = MediaPreparer(None, media_local_roots=[str(root)])
        with pytest.raises(PermissionError):
            preparer._read_local_file(evil_file, file_name=None)
