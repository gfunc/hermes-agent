from gateway.platforms.wecom_media import detect_mime_from_bytes, apply_file_size_limits, detect_wecom_media_type


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
