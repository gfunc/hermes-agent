import pytest

from gateway.platforms.wecom_video import extract_first_video_frame


def test_extract_first_frame_returns_path_for_mp4(tmp_path):
    # Create a tiny fake video file (test will fail because ffmpeg won't process it,
    # but the function signature must exist)
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"not a real video")
    result = extract_first_video_frame(str(fake_video), output_dir=str(tmp_path))
    # Because it's fake ffmpeg will fail; assert graceful fallback
    assert result is None
