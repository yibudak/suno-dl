from __future__ import annotations

from pathlib import Path

import pytest

from suno_dl import safe_name, target_path, unique_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Quiet Hours", "Quiet Hours"),
        ('a/b:c?"<>|*', "abc"),
        ("  spaced   out  ", "spaced out"),
        ("..dots..", "dots"),
        ("", "untitled"),
        ("   ", "untitled"),
        ("CON", "CON_"),
        ("com1", "com1_"),
        ("normal name", "normal name"),
        ("trailing dots...", "trailing dots"),
        ("keep-dash_under", "keep-dash_under"),
        ("keep spaces 2", "keep spaces 2"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_safe_name_caps_length():
    assert len(safe_name("x" * 500)) == 120


def test_unique_path_passes_through_when_free(tmp_path: Path):
    target = tmp_path / "song.mp4"
    assert unique_path(target) == target


def test_unique_path_counts_up(tmp_path: Path):
    target = tmp_path / "song.mp4"
    target.write_bytes(b"x")
    assert unique_path(target).name == "song (1).mp4"
    (tmp_path / "song (1).mp4").write_bytes(b"x")
    assert unique_path(target).name == "song (2).mp4"


def test_target_path_uses_artist_and_title(tmp_path: Path):
    clip = {"title": "Afterglow", "display_name": "Someone"}
    assert target_path(tmp_path, clip, "mp3") == tmp_path / "Someone - Afterglow.mp3"


def test_target_path_survives_slashes(tmp_path: Path):
    clip = {"title": "AC/DC", "handle": "band"}
    assert target_path(tmp_path, clip, "mp4").name == "band - ACDC.mp4"
