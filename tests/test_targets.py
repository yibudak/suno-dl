from __future__ import annotations

import pytest

from suno_dl import SunoError, parse_target

UUID = "11111111-2222-4333-8444-555555555555"
PLAYLIST = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"https://suno.com/song/{UUID}", ("song", UUID)),
        (f"https://suno.com/song/{UUID}/", ("song", UUID)),
        (f"https://suno.com/song/{UUID}?si=abc", ("song", UUID)),
        (f"https://www.suno.com/song/{UUID}", ("song", UUID)),
        (f"suno.com/song/{UUID}", ("song", UUID)),
        (UUID, ("song", UUID)),
        (UUID.upper(), ("song", UUID)),
        (
            "https://suno.com/playlist/3db9f0a6-4e84-4774-8b52-495cb252cc26",
            ("playlist", "3db9f0a6-4e84-4774-8b52-495cb252cc26"),
        ),
        ("https://suno.com/@someone", ("profile", "someone")),
        ("@someone", ("profile", "someone")),
        ("https://suno.com/profile/someone", ("profile", "someone")),
        ("https://suno.ai/song/" + UUID, ("song", UUID)),
    ],
)
def test_targets_are_understood(raw, expected):
    assert parse_target(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "https://example.com/song/" + UUID, "not a link", "https://suno.com/"]
)
def test_unknown_targets_are_rejected(raw):
    with pytest.raises(SunoError) as info:
        parse_target(raw)
    assert info.value.code == "BAD_TARGET"
