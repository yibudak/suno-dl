from __future__ import annotations

import base64
import json
import time

import pytest

import suno_dl
from suno_dl import Auth, AuthError, jwt_exp, looks_like_jwt, parse_cookies


def make_jwt(exp: int, subject: str = "user_1") -> str:
    def part(payload: dict) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': subject, 'exp': exp})}.signature"


def test_parse_cookies_accepts_a_header():
    assert parse_cookies("a=1; b=2") == {"a": "1", "b": "2"}


def test_parse_cookies_accepts_a_bare_token():
    token = make_jwt(int(time.time()) + 600)
    # a bare JWT is the __session token
    assert parse_cookies(token) == {"__session": token}


def test_parse_cookies_strips_quotes_and_spaces():
    assert parse_cookies(' __client="abc" ; x=y ') == {"__client": "abc", "x": "y"}


def test_parse_cookies_of_empty_input():
    assert parse_cookies("") == {}
    assert parse_cookies(None) == {}


def test_jwt_exp_reads_the_claim():
    exp = int(time.time()) + 1234
    assert jwt_exp(make_jwt(exp)) == exp


def test_jwt_exp_survives_garbage():
    assert jwt_exp("not-a-jwt") is None
    assert jwt_exp("a.b.c") is None


def test_looks_like_jwt():
    assert looks_like_jwt(make_jwt(0))
    assert not looks_like_jwt("__client=abc")
    assert not looks_like_jwt("")


def test_auth_without_credentials_fails_fast():
    with pytest.raises(AuthError) as info:
        Auth()
    assert info.value.code == "NO_AUTH"
    assert "--session" in str(info.value)


def test_an_expired_session_alone_says_what_to_do(monkeypatch):
    stale = make_jwt(int(time.time()) - 600)
    with pytest.raises(AuthError) as info:
        Auth(session=stale)
    assert info.value.code == "SESSION_EXPIRED"
    assert "__session" in str(info.value)


def test_session_accepts_an_assignment_string():
    token = make_jwt(int(time.time()) + 600)
    auth = Auth(session="__session=" + token)
    assert auth.token() == token
    assert auth.source() == "__session"


def test_session_cookie_is_used_directly():
    token = make_jwt(int(time.time()) + 600)
    auth = Auth(cookie="__session=" + token)
    assert auth.token() == token


def test_expiry_is_described_in_words():
    fresh = Auth(session=make_jwt(int(time.time() + 3600)))
    assert fresh.describe_expiry().startswith("expires in")
    assert fresh.source() == "__session"


def test_client_token_is_preferred_for_long_runs(monkeypatch):
    fresh = make_jwt(int(time.time()) + 3600)
    monkeypatch.setattr(Auth, "_mint", lambda self: make_jwt(int(time.time()) + 60))
    auth = Auth(cookie="__client=" + make_jwt(int(time.time()) + 3600), session=fresh)
    # a still-valid __session is used as-is, no Clerk round trip
    assert auth.token() == fresh


def test_auth_reuses_a_fresh_session_token():
    token = make_jwt(int(time.time()) + 3600)
    auth = Auth(session=token)
    assert auth.token() == token


def test_auth_mints_from_client_and_caches(monkeypatch):
    fresh = make_jwt(int(time.time()) + 3600)
    calls = []

    def fake_mint(self):
        calls.append(1)
        return fresh

    monkeypatch.setattr(Auth, "_mint", fake_mint)
    auth = Auth(cookie="__client=" + make_jwt(int(time.time()) + 3600))
    assert auth.token() == fresh
    assert auth.token() == fresh
    assert len(calls) == 1


def test_auth_remints_an_expired_token(monkeypatch):
    stale = make_jwt(int(time.time()) - 10)
    fresh = make_jwt(int(time.time()) + 3600)
    client = make_jwt(int(time.time()) + 3600)
    monkeypatch.setattr(Auth, "_mint", lambda self: fresh)
    auth = Auth(session=stale, cookie="__client=" + client)
    assert auth.token() == fresh


def test_clerk_falls_back_to_a_bare_authorization_header(monkeypatch):
    seen = []

    def fake_get_json(url, headers, **kwargs):
        seen.append(headers["Authorization"])
        if headers["Authorization"].startswith("Bearer "):
            return {"response": {"sessions": []}}  # token ignored by the server
        return {"response": {"sessions": [{"id": "sess_1", "status": "active"}]}}

    monkeypatch.setattr(suno_dl, "get_json", fake_get_json)
    auth = Auth(cookie="__client=" + make_jwt(int(time.time()) + 3600))
    sessions = auth._clerk_client()["sessions"]
    assert sessions[0]["id"] == "sess_1"
    assert len(seen) == 2


def test_mint_without_a_session_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(Auth, "_clerk_client", lambda self: {"sessions": []})
    auth = Auth(cookie="__client=" + make_jwt(int(time.time()) + 3600))
    with pytest.raises(AuthError) as info:
        auth.token()
    assert info.value.code == "NO_SESSION"
