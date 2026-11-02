from __future__ import annotations

import pytest

import suno_dl


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        suno_dl.main(["--version"])
    assert info.value.code == 0
    assert suno_dl.__version__ in capsys.readouterr().out


def test_help_flag(capsys):
    with pytest.raises(SystemExit) as info:
        suno_dl.main(["--help"])
    assert info.value.code == 0
    assert "suno-dl" in capsys.readouterr().out


def test_no_targets_does_nothing(capsys):
    assert suno_dl.main([]) == 0
    assert "nothing to download" in capsys.readouterr().err


def test_library_without_a_cookie_explains_itself(capsys, monkeypatch):
    monkeypatch.delenv("SUNO_COOKIE", raising=False)
    monkeypatch.delenv("SUNO_SESSION", raising=False)
    assert suno_dl.main(["--library"]) == 2
    assert "__client cookie" in capsys.readouterr().err


def test_a_bad_cookie_is_rejected_before_any_network_call(capsys):
    assert suno_dl.main(["--library", "--cookie", "garbage"]) == 2
    assert "No Suno credentials" in capsys.readouterr().err


def test_a_foreign_url_is_refused(capsys):
    assert suno_dl.main(["https://example.com/song/abc"]) == 2
    assert "not a suno.com link" in capsys.readouterr().err


def test_ffmpeg_is_required_for_converted_formats(capsys, monkeypatch):
    monkeypatch.setattr(suno_dl, "ffmpeg_path", lambda: None)
    assert suno_dl.main(["-f", "flac", "https://suno.com/song/" + "0" * 8]) == 2
    assert "ffmpeg" in capsys.readouterr().err
