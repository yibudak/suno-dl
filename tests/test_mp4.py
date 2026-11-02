from __future__ import annotations

from pathlib import Path

from mp4_factory import fragmented, progressive

from suno_dl import inspect_mp4


def _write(tmp_path: Path, data: bytes, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_valid_progressive_file_is_accepted(tmp_path):
    info = inspect_mp4(_write(tmp_path, progressive(duration_ms=12345)))
    assert info["ok"], info["problems"]
    assert info["duration"] == 12.345
    assert info["fragmented"] is False
    assert info["boxes"][0] == "ftyp"


def test_fragmented_file_is_accepted(tmp_path):
    info = inspect_mp4(_write(tmp_path, fragmented(duration_ms=60000, fragments=4)))
    assert info["ok"], info["problems"]
    assert info["fragmented"] is True
    assert info["boxes"].count("moof") == 4


def test_truncated_file_is_rejected(tmp_path):
    data = progressive()
    info = inspect_mp4(_write(tmp_path, data[: len(data) // 2]))
    assert not info["ok"]
    assert any("truncated" in problem for problem in info["problems"])


def test_garbage_is_rejected(tmp_path):
    info = inspect_mp4(_write(tmp_path, b"this is not an mp4 file" * 40))
    assert not info["ok"]
    assert any("ftyp" in problem for problem in info["problems"])


def test_empty_file_is_rejected(tmp_path):
    info = inspect_mp4(_write(tmp_path, b""))
    assert not info["ok"]
    assert info["problems"]


def test_missing_moov_is_rejected(tmp_path):
    data = progressive()
    without_moov = data[: data.index(b"moov") - 4]  # drop the moov box entirely
    info = inspect_mp4(_write(tmp_path, without_moov))
    assert not info["ok"]
    assert any("moov" in problem for problem in info["problems"])
