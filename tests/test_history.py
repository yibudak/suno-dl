from __future__ import annotations

import json
from pathlib import Path

from suno_dl import History


def test_missing_history_is_empty(tmp_path: Path):
    history = History(tmp_path / "history.json")
    assert history.get("nope") is None


def test_add_then_get_round_trips(tmp_path: Path):
    path = tmp_path / "history.json"
    audio = tmp_path / "song.mp4"
    audio.write_bytes(b"audio")

    history = History(path)
    history.add("clip:mp4", {"clip_id": "clip", "path": str(audio)})

    assert history.get("clip:mp4")["path"] == str(audio)
    reloaded = History(path)
    assert reloaded.get("clip:mp4")["clip_id"] == "clip"
    assert json.loads(path.read_text())["clip:mp4"]["path"] == str(audio)


def test_entry_pointing_at_a_deleted_file_is_stale(tmp_path: Path):
    path = tmp_path / "history.json"
    audio = tmp_path / "song.mp4"
    audio.write_bytes(b"audio")

    history = History(path)
    history.add("clip", {"path": str(audio)})
    audio.unlink()

    assert history.get("clip") is None


def test_corrupt_history_does_not_crash(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text("{ not json")
    history = History(path)
    assert history.get("anything") is None


def test_history_writes_are_atomic(tmp_path: Path):
    path = tmp_path / "history.json"
    history = History(path)
    history.add("a", {"path": str(tmp_path / "a.mp4")})
    assert not (tmp_path / "history.tmp").exists()
