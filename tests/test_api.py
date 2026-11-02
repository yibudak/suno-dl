from __future__ import annotations

import pytest

import suno_dl
from suno_dl import AuthError, HttpError, SunoAPI, SunoError, resolve_short_link


def _raise_401(url, *args, **kwargs):
    raise HttpError(401, url)


def test_a_401_becomes_a_session_error(monkeypatch):
    monkeypatch.setattr(suno_dl, "get_json", _raise_401)
    api = SunoAPI(None)
    with pytest.raises(AuthError) as info:
        api.clip("00000000-0000-0000-0000-000000000000")
    assert info.value.code == "TOKEN_REJECTED"
    assert "__session" in str(info.value)


def test_a_401_on_the_feed_becomes_a_session_error(monkeypatch):
    monkeypatch.setattr(suno_dl, "post_json", _raise_401)
    api = SunoAPI(None)
    with pytest.raises(AuthError) as info:
        api.feed(filters={}, limit=1)
    assert info.value.code == "TOKEN_REJECTED"


def test_other_statuses_are_not_mistaken_for_auth(monkeypatch):
    def _raise_500(url, *args, **kwargs):
        raise HttpError(500, url)

    monkeypatch.setattr(suno_dl, "get_json", _raise_500)
    api = SunoAPI(None)
    with pytest.raises(HttpError) as info:
        api.clip("x")
    assert info.value.status == 500


class _FakeResponse:
    def __init__(self, url: str):
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self) -> str:
        return self._url


def test_share_links_resolve_to_song_urls(monkeypatch):
    target = "https://suno.com/song/11111111-2222-4333-8444-555555555555?sh=abc"
    monkeypatch.setattr(suno_dl, "_open", lambda *a, **k: _FakeResponse(target))
    assert resolve_short_link("abc") == target


def test_an_unresolvable_share_link_is_an_error(monkeypatch):
    monkeypatch.setattr(suno_dl, "_open", lambda *a, **k: _FakeResponse("https://suno.com/s/abc"))
    with pytest.raises(SunoError) as info:
        resolve_short_link("abc")
    assert info.value.code == "SHORT_LINK"
