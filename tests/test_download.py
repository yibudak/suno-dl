from __future__ import annotations

from pathlib import Path

import pytest

from suno_dl import HttpError, SunoError, download_file

PAYLOAD = bytes(range(256)) * 400  # 102 400 bytes


def test_full_download(tmp_path: Path, http_server):
    base, handler = http_server
    handler.files["clip.mp4"] = PAYLOAD
    dest = tmp_path / "clip.mp4"

    written = download_file(f"{base}/clip.mp4", dest, expected=len(PAYLOAD), quiet=True)

    assert written == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD
    assert not dest.with_name("clip.mp4.part").exists()


def test_resume_sends_a_range_request(tmp_path: Path, http_server):
    base, handler = http_server
    handler.files["clip.mp4"] = PAYLOAD
    dest = tmp_path / "clip.mp4"
    part = tmp_path / "clip.mp4.part"
    part.write_bytes(PAYLOAD[:1000])

    written = download_file(f"{base}/clip.mp4", dest, expected=len(PAYLOAD), quiet=True)

    assert written == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD
    assert handler.seen_range == ["bytes=1000-"]
    assert not part.exists()


def test_server_ignoring_range_restarts_cleanly(tmp_path: Path, http_server):
    base, handler = http_server
    handler.files["clip.mp4"] = PAYLOAD
    handler.ignore_range = True
    dest = tmp_path / "clip.mp4"
    (tmp_path / "clip.mp4.part").write_bytes(PAYLOAD[:1000])

    download_file(f"{base}/clip.mp4", dest, expected=len(PAYLOAD), quiet=True)

    assert dest.read_bytes() == PAYLOAD


def test_unknown_file_raises_http_error(tmp_path: Path, http_server):
    base, _handler = http_server
    with pytest.raises(HttpError) as info:
        download_file(f"{base}/missing.mp4", tmp_path / "missing.mp4", quiet=True)
    assert info.value.status == 404


def test_network_error_is_wrapped(tmp_path: Path):
    with pytest.raises(SunoError):
        download_file("http://127.0.0.1:1/never.mp4", tmp_path / "never.mp4", retries=0, quiet=True)
