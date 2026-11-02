#!/usr/bin/env python3
"""Download your own Suno songs at original quality.

The tool is a single file and uses only the standard library. Public songs come
straight from the CDN. Songs Suno only serves to its player (every private clip)
are captured from the player: the tool drives a headless Chrome, reads the
decrypted audio as the page feeds it to the audio pipeline, and writes the
result. Without Chrome those songs cannot be fetched. ffmpeg is only needed to
convert.

Authentication follows the suno.com web app: the tool sends a `__session` JWT
directly, or exchanges a `__client` cookie for one and refreshes it as needed.

Only download content you own or have the right to use.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import contextlib
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Every constant here came from Suno's web client.
# --------------------------------------------------------------------------

API_BASE = "https://studio-api-prod.suno.com"  # what suno.com's own client calls
AUTH_BASE = "https://auth.suno.com"
CDN_BASE = "https://cdn1.suno.ai"

CLERK_JS_VERSION = "5.36.1"
CLERK_API_VERSION = "2021-02-05"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STATE_DIR = Path(os.environ.get("SUNO_STATE_DIR", Path.home() / ".suno-dl"))
HISTORY_FILE = STATE_DIR / "history.json"

__version__ = "1.1.0"

JWT_REFRESH_MARGIN = 30  # seconds before expiry to re-mint

FORMATS = {
    # name      extension   ffmpeg codec        default bitrate (kbps)
    "original": ("mp4", None, None),
    "mp3": ("mp3", "libmp3lame", 320),
    "m4a": ("m4a", "aac", 256),
    "aac": ("m4a", "aac", 256),
    "flac": ("flac", "flac", None),
    "wav": ("wav", "pcm_s16le", None),
    "opus": ("opus", "libopus", 128),
}


class SunoError(RuntimeError):
    def __init__(self, message: str, code: str = "ERROR", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AuthError(SunoError):
    pass


class HttpError(SunoError):
    def __init__(self, status: int, url: str, body: str = ""):
        snippet = re.sub(r"\s+", " ", body or "").strip()[:200]
        super().__init__(f"HTTP {status} for {short_url(url)} {snippet}".strip(), "HTTP", status)
        self.url = url
        self.body = body


def short_url(url: str, limit: int = 90) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"


def human_duration(seconds: int) -> str:
    """Compact '2h 14m' style duration, for token-expiry messages."""
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            whole, rest = divmod(seconds, size)
            return f"{whole}{unit}" + (f" {rest // 60}m" if unit == "d" and rest >= 60 else "")
    return f"{seconds}s"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def _open(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
):
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
    retries: int = 3,
    allow: tuple[int, ...] = (),
) -> tuple[int, dict, bytes]:
    """HTTP request with bounded retries. Returns (status, headers, body)."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _open(url, method, headers, data, timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            body = b""
            with contextlib.suppress(Exception):
                body = e.read()
            if e.code in allow:
                return e.code, dict(e.headers or {}), body
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                delay = float(e.headers.get("Retry-After") or 0) or (1.5**attempt + 0.5)
                time.sleep(min(delay, 30))
                last_exc = e
                continue
            raise HttpError(e.code, url, body.decode("utf-8", "replace")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(min(1.5**attempt + 0.5, 15))
                continue
            raise SunoError(f"network error for {short_url(url)}: {e}", "NETWORK") from None
    raise SunoError(f"request failed for {short_url(url)}: {last_exc}", "NETWORK")


def get_json(url: str, headers: dict, **kw) -> dict:
    _, _, body = request(url, headers=headers, **kw)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise SunoError(f"non-JSON response from {short_url(url)}", "BAD_JSON") from None


def post_json(url: str, payload: dict, headers: dict, **kw) -> dict:
    hdrs = {"Content-Type": "application/json", **headers}
    _, _, body = request(url, method="POST", headers=hdrs, data=json.dumps(payload).encode(), **kw)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise SunoError(f"non-JSON response from {short_url(url)}", "BAD_JSON") from None


# --------------------------------------------------------------------------
# JWT helpers
# --------------------------------------------------------------------------


def jwt_exp(token: str) -> int | None:
    """Return the `exp` claim of a JWT, or None if it is not a parseable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def looks_like_jwt(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", value or ""))


def normalize_token(raw: str | None, *, default_key: str) -> str | None:
    """Accept a token, a `__session=…` assignment, or a whole Cookie header."""
    if not raw:
        return None
    raw = raw.strip()
    if "=" in raw:
        cookies = parse_cookies(raw)
        raw = cookies.get("__session") or cookies.get(default_key) or ""
    raw = raw.strip().strip('"').strip("'")
    return raw or None


def parse_cookies(raw: str) -> dict[str, str]:
    """Accept a full Cookie header, a `name=value` list, or a bare token."""
    out: dict[str, str] = {}
    raw = (raw or "").strip()
    if not raw:
        return out
    if "=" not in raw and looks_like_jwt(raw):
        # A bare token is the __session JWT.
        out["__session"] = raw
        return out
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            out[name] = value
    return out


# --------------------------------------------------------------------------
# Authentication
#
# Two shapes of credential, both a bearer token in the end:
#   __session  a ready JWT from suno.com, valid for minutes
#   __client   a long-lived Clerk cookie, exchanged for a JWT on demand
# --------------------------------------------------------------------------


class Auth:
    """Holds a valid bearer token, minting one from __client when needed."""

    def __init__(
        self, cookie: str | None = None, session: str | None = None, verbose: bool = False
    ):
        self.verbose = verbose
        self._lock = threading.Lock()
        self._jwt: str | None = None
        self._warned: set[str] = set()

        cookies = parse_cookies(cookie or "")
        self.client_token = (cookies.get("__client") or "").strip() or None

        supplied = (
            normalize_token(session, default_key="__client")
            or (cookies.get("__session") or "").strip()
        )
        self.session_token = supplied if supplied and looks_like_jwt(supplied) else None
        self.expires_at = jwt_exp(self.session_token) if self.session_token else None

        if self.session_token and (self.expires_at or 0) > time.time() + JWT_REFRESH_MARGIN:
            self._jwt = self.session_token
            if self.verbose:
                log(f"auth: using the supplied __session token ({self.describe_expiry()})")
        elif self.session_token and self.verbose:
            log(f"auth: the supplied __session token is {self.describe_expiry()}")

        if not self.client_token and not self._jwt:
            if self.session_token:
                raise AuthError(
                    f"Your __session token {self.describe_expiry()}. Copy a fresh one "
                    "from DevTools -> Application -> Cookies -> suno.com -> __session and "
                    "run again (it lives for minutes). If you also have a __client cookie, "
                    "pass that: the tool then refreshes itself.",
                    "SESSION_EXPIRED",
                )
            raise AuthError(
                "No Suno credentials found. Pass --session with your __session JWT, "
                "or --cookie with a __client cookie (or set SUNO_SESSION / SUNO_COOKIE).",
                "NO_AUTH",
            )

    # -- token bookkeeping ------------------------------------------------

    def describe_expiry(self) -> str:
        if not self.expires_at:
            return "has no readable expiry"
        left = int(self.expires_at - time.time())
        if left <= 0:
            return f"expired {human_duration(-left)} ago"
        return f"expires in {human_duration(left)}"

    def source(self) -> str:
        if self.session_token and self._jwt is self.session_token:
            return "__session"
        return "__client" if self.client_token else "none"

    # -- Clerk ------------------------------------------------------------

    def _clerk_headers(self, scheme: str = "Bearer") -> dict:
        token = self.client_token or ""
        return {
            "Authorization": f"{scheme} {token}".strip(),
            "Origin": "https://suno.com",
            "Referer": "https://suno.com/",
        }

    def _clerk_client(self) -> dict:
        """GET /v1/client on Clerk's frontend API with the __client token.

        Clerk accepts the client token either as `Authorization: Bearer <token>`
        or as a bare `Authorization: <token>`; try both rather than guess.
        """
        url = (
            f"{AUTH_BASE}/v1/client?_clerk_js_version={CLERK_JS_VERSION}"
            f"&__clerk_api_version={CLERK_API_VERSION}"
        )
        last: dict = {}
        for scheme in ("Bearer", ""):
            try:
                data = get_json(url, self._clerk_headers(scheme), timeout=30)
            except SunoError:
                continue
            response = data.get("response") or {}
            if response.get("sessions"):
                self._scheme = scheme
                return response
            last = response
        return last

    def _session_headers(self) -> dict:
        return self._clerk_headers(getattr(self, "_scheme", "Bearer"))

    def _mint(self) -> str:
        if not self.client_token:
            raise AuthError("Session expired and no __client cookie was supplied.", "TOKEN_EXPIRED")

        client = self._clerk_client()
        sessions = client.get("sessions") or []
        if not sessions:
            hint = ""
            if self.client_token and looks_like_jwt(self.client_token):
                hint = (
                    " If you pasted a __session token, use --session (or SUNO_SESSION) "
                    "instead: __session is a ready bearer token, __client is the one that "
                    "gets exchanged for it."
                )
            raise AuthError(
                "Clerk returned no sessions for this __client token. It has expired or "
                "came from another profile. Log out and back in on suno.com, then copy "
                "__client again." + hint,
                "NO_SESSION",
            )
        active = [s for s in sessions if s.get("status") == "active"] or sessions
        session_id = active[0].get("id")
        if not session_id:
            raise AuthError("Clerk session object has no id.", "NO_SESSION")

        url = (
            f"{AUTH_BASE}/v1/client/sessions/{session_id}/tokens"
            f"?__clerk_js_version={CLERK_JS_VERSION}&__clerk_api_version={CLERK_API_VERSION}"
        )
        data = post_json(url, {}, self._session_headers(), timeout=30)
        token = data.get("jwt")
        if not token:
            raise AuthError(f"Clerk did not return a JWT: {str(data)[:200]}", "NO_TOKEN")
        if self.verbose:
            exp = jwt_exp(token)
            left = (exp - int(time.time())) if exp else 0
            log(f"auth: minted JWT valid for {left}s")
        return token

    # -- public -----------------------------------------------------------

    def token(self) -> str:
        with self._lock:
            exp = jwt_exp(self._jwt) if self._jwt else None
            fresh = bool(self._jwt) and (exp is None or exp > time.time() + JWT_REFRESH_MARGIN)
            if fresh:
                return self._jwt  # type: ignore[return-value]
            self._jwt = self._mint()
            return self._jwt

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}


# --------------------------------------------------------------------------
# Suno API
# --------------------------------------------------------------------------


def rejected_session(error: HttpError) -> SunoError:
    """A 401 from the API means the copied token has expired."""
    if error.status != 401:
        return error
    return AuthError(
        "Suno rejected the session (401). Copy a fresh __session from the browser, or "
        "use a __client cookie so the tool can refresh itself.",
        "TOKEN_REJECTED",
    )


class SunoAPI:
    def __init__(self, auth: Auth | None):
        self.auth = auth

    def _headers(self) -> dict:
        return self.auth.headers() if self.auth else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return get_json(url, self._headers())
        except HttpError as e:
            raise rejected_session(e) from None

    def clip(self, clip_id: str) -> dict:
        return self._get(f"/api/clip/{clip_id}")

    def me(self) -> dict:
        return self._get("/api/user/me")

    def playlist(self, playlist_id: str, page: int = 0, limit: int = 100) -> dict:
        return self._get(
            f"/api/playlist/{playlist_id}",
            {"page": page, "limit": limit},
        )

    def profile(self, handle: str, page: int = 0, limit: int = 20) -> dict:
        handle = urllib.parse.quote(handle.lstrip("@"))
        return self._get(
            f"/api/profiles/{handle}",
            {
                "playlists_sort_by": "created_at",
                "clips_sort_by": "created_at",
                "page": page,
                "limit": limit,
            },
        )

    def feed(self, filters: dict | None = None, cursor: str | None = None, limit: int = 50) -> dict:
        body = {"cursor": cursor, "limit": limit, "filters": filters or {}}
        try:
            return post_json(API_BASE + "/api/feed/v3", body, self._headers())
        except HttpError as e:
            raise rejected_session(e) from None

    def feed_legacy(self, page: int = 0) -> list:
        data = self._get("/api/feed/", {"page": page})
        return data if isinstance(data, list) else data.get("clips", [])


# --------------------------------------------------------------------------
# Target handling
# --------------------------------------------------------------------------

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def parse_target(raw: str) -> tuple[str, str]:
    """Classify a URL / id / @handle into ('song'|'playlist'|'profile', value)."""
    raw = (raw or "").strip()
    if not raw:
        raise SunoError("empty target", "BAD_TARGET")

    if UUID_RE.match(raw):
        return "song", raw.lower()

    if raw.startswith("@"):
        return "profile", raw[1:]

    if "://" not in raw:
        raw = "https://" + raw

    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not (host in {"suno.com", "suno.ai"} or host.endswith((".suno.com", ".suno.ai"))):
        raise SunoError(f"not a suno.com link: {raw}", "BAD_TARGET")

    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]

    if len(parts) == 2 and parts[0] == "s":
        return "short", parts[1]
    if len(parts) >= 2 and parts[0] == "song" and UUID_RE.match(parts[1]):
        return "song", parts[1].lower()
    if len(parts) >= 2 and parts[0] == "playlist" and UUID_RE.match(parts[1]):
        return "playlist", parts[1].lower()
    if parts and parts[0].startswith("@"):
        return "profile", parts[0][1:]
    if len(parts) >= 2 and parts[0] == "profile":
        return "profile", parts[1]
    if parts and UUID_RE.match(parts[-1]):
        # /song/<uuid>/lyrics or similar
        if "playlist" in parts:
            return "playlist", parts[-1].lower()
        return "song", parts[-1].lower()

    raise SunoError(f"could not understand the link: {raw}", "BAD_TARGET")


def resolve_short_link(code: str) -> str:
    """Follow a suno.com/s/<code> share link to the real song URL."""
    url = f"https://suno.com/s/{code}"
    final = ""
    try:
        with _open(url, method="HEAD", timeout=30) as resp:
            final = resp.geturl()
    except urllib.error.HTTPError as e:
        final = e.geturl() or ""
    except (SunoError, urllib.error.URLError):
        final = ""
    if not final or final.rstrip("/") == url:
        raise SunoError(f"could not resolve the share link {url}", "SHORT_LINK")
    return final


# --------------------------------------------------------------------------
# Clip normalisation
# --------------------------------------------------------------------------


def clip_title(clip: dict) -> str:
    return (clip.get("title") or "").strip() or "untitled"


def clip_artist(clip: dict) -> str:
    for key in ("display_name", "handle", "user_display_name", "user_handle"):
        val = (clip.get(key) or "").strip()
        if val:
            return val
    author = clip.get("user") or {}
    if isinstance(author, dict):
        for key in ("display_name", "handle", "name"):
            val = (author.get(key) or "").strip()
            if val:
                return val
    return "Unknown Artist"


def clip_duration(clip: dict) -> float:
    meta = clip.get("metadata") or {}
    for candidate in (meta.get("duration"), clip.get("duration")):
        if candidate is None:
            continue
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return 0.0


def clip_created(clip: dict) -> str:
    return str(clip.get("created_at") or "")


def usable_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if "/api/forbidden" in url:
        return None
    if not url.startswith("https://"):
        return None
    return url


def clip_audio_urls(clip: dict) -> list[str]:
    """Ordered candidates for the plain media file, best first.

    `media_urls` never becomes a candidate: that stream is encrypted for Suno's
    player (license server, AES-GCM), so a plain fetch returns ciphertext. The
    progressive MP4 is the real file. A private clip has neither, which is what
    sends the song to the browser capture.
    """
    urls: list[str] = []

    def add(url: str | None) -> None:
        url = usable_url(url)
        if url and url not in urls:
            urls.append(url)

    add(clip.get("video_url"))
    add(clip.get("audio_url"))
    clip_id = clip.get("id")
    if clip_id and UUID_RE.match(str(clip_id)):
        add(f"{CDN_BASE}/{clip_id}.mp4")
    return urls


def private_or_missing(clip: dict, title: str) -> str:
    """Say why the tool cannot fetch a song that has no plain media URL."""
    if clip.get("is_public") is False:
        return (
            f"FAILED (PRIVATE): {title}. Suno publishes a plain MP4 only for public "
            "clips. This one is private and the browser capture is switched off, so "
            "there is no file to fetch."
        )
    return f"FAILED (NO_MEDIA): {title}. No progressive MP4 is published for this song."


def clip_cover_url(clip: dict) -> str | None:
    return usable_url(clip.get("image_large_url")) or usable_url(clip.get("image_url"))


# --------------------------------------------------------------------------
# MP4 inspection: check the container before marking a song done
# --------------------------------------------------------------------------


def inspect_mp4(path: Path) -> dict:
    size = path.stat().st_size
    boxes: list[tuple[str, int, int]] = []
    truncated = False
    duration = 0.0
    moov_count = 0
    mdat_bytes = 0

    with path.open("rb") as fh:
        offset = 0
        while offset + 8 <= size:
            fh.seek(offset)
            header = fh.read(16)
            if len(header) < 8:
                truncated = True
                break
            box_size, box_type = struct.unpack(">I4s", header[:8])
            box_type = box_type.decode("latin1")
            head = 8
            if box_size == 1:
                if len(header) < 16:
                    truncated = True
                    break
                box_size = struct.unpack(">Q", header[8:16])[0]
                head = 16
            elif box_size == 0:
                box_size = size - offset
            if box_size < head or offset + box_size > size:
                truncated = True
                boxes.append((box_type, offset, box_size))
                break
            boxes.append((box_type, offset, box_size))
            if box_type == "moov":
                moov_count += 1
                duration = _mvhd_duration(fh, offset, box_size) or duration
            elif box_type == "mdat":
                mdat_bytes += box_size - head
            offset += box_size

    kinds = [b[0] for b in boxes]
    problems: list[str] = []
    if not kinds:
        problems.append("no MP4 boxes found")
    elif kinds[0] not in ("ftyp", "styp"):
        problems.append(f"file does not start with ftyp (got {kinds[0]!r})")
    if moov_count == 0:
        problems.append("missing moov (unplayable)")
    if mdat_bytes == 0:
        problems.append("no media data (mdat)")
    if truncated:
        problems.append("file is truncated (last box runs past EOF)")
    ratio = (mdat_bytes / size) if size else 0.0
    if ratio < 0.90 and not truncated:
        problems.append(f"media payload is only {ratio:.0%} of the file")

    return {
        "ok": not problems,
        "problems": problems,
        "size": size,
        "boxes": kinds,
        "moov": moov_count,
        "fragmented": kinds.count("moof") > 0,
        "mdat_bytes": mdat_bytes,
        "duration": duration,
    }


def _mvhd_duration(fh, moov_offset: int, moov_size: int) -> float:
    """Read the presentation duration from the mvhd box inside moov."""
    end = moov_offset + moov_size
    pos = moov_offset + 8
    with_guard = 0
    while pos + 8 <= end and with_guard < 64:
        with_guard += 1
        fh.seek(pos)
        head = fh.read(16)
        if len(head) < 8:
            break
        size, kind = struct.unpack(">I4s", head[:8])
        head_len = 8
        if size == 1:
            size = struct.unpack(">Q", head[8:16])[0]
            head_len = 16
        elif size == 0:
            size = end - pos
        if size < head_len:
            break
        if kind == b"mvhd":
            fh.seek(pos + head_len)
            payload = fh.read(min(size - head_len, 120))
            if len(payload) < 20:
                return 0.0
            version = payload[0]
            if version == 1:
                if len(payload) < 40:
                    return 0.0
                timescale, dur = struct.unpack(">IQ", payload[20:32])
            else:
                if len(payload) < 24:
                    return 0.0
                timescale, dur = struct.unpack(">II", payload[12:20])
            return float(dur) / timescale if timescale else 0.0
        pos += size
    return 0.0


# --------------------------------------------------------------------------
# Filenames that survive Windows, macOS and Linux
# --------------------------------------------------------------------------

WIN_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", re.I)


def safe_name(value: str, limit: int = 120) -> str:
    name = str(value or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"[$`;&()!\n\r]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^\.+", "", name)
    name = name[:limit]
    name = re.sub(r"[. ]+$", "", name)
    if not name:
        name = "untitled"
    if WIN_RESERVED.match(name):
        name = WIN_RESERVED.sub(r"\1_", name)
    return name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def target_path(out_dir: Path, clip: dict, ext: str) -> Path:
    base = f"{safe_name(clip_artist(clip))} - {safe_name(clip_title(clip))}"
    return out_dir / f"{base}.{ext}"


# --------------------------------------------------------------------------
# History: skip what is already on disk
# --------------------------------------------------------------------------


class History:
    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, dict] = {}
        try:
            if path.exists():
                self.data = json.loads(path.read_text("utf-8"))
        except Exception:
            self.data = {}

    def get(self, clip_id: str) -> dict | None:
        with self._lock:
            entry = self.data.get(clip_id)
            if not entry:
                return None
            if entry.get("path") and not Path(entry["path"]).exists():
                return None
            return entry

    def add(self, clip_id: str, entry: dict) -> None:
        with self._lock:
            self.data[clip_id] = entry
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------


def download_file(
    url: str, dest: Path, expected: int | None = None, retries: int = 4, quiet: bool = False
) -> int:
    """Range-resumable download to `dest`, via a .part file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        start = part.stat().st_size if part.exists() else 0
        if expected and start == expected:
            break
        headers = {"Accept": "*/*"}
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            with _open(url, headers=headers, timeout=120) as resp:
                status = resp.status
                if status == 206:
                    mode = "ab"
                    content_range = resp.headers.get("Content-Range", "")
                    total = None
                    m = re.search(r"/(\d+)$", content_range)
                    if m:
                        total = int(m.group(1))
                elif status == 200:
                    mode = "wb"
                    start = 0
                    total = int(resp.headers.get("Content-Length") or 0) or None
                    if part.exists():
                        part.unlink()
                else:
                    raise SunoError(
                        f"unexpected HTTP {status} from {short_url(url)}", "HTTP", status
                    )

                expected = total or expected
                written = start
                last_report = 0.0
                with part.open(mode) as fh:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                        if not quiet and sys.stderr.isatty():
                            now = time.time()
                            if now - last_report > 0.4:
                                last_report = now
                                pct = f" {written * 100 // expected:3d}%" if expected else ""
                                sys.stderr.write(f"\r    {written / 1048576:8.1f} MB{pct}   ")
                                sys.stderr.flush()
                if not quiet and sys.stderr.isatty():
                    sys.stderr.write("\r" + " " * 40 + "\r")
                    sys.stderr.flush()
            size = part.stat().st_size
            if expected and size < expected:
                last_error = SunoError(f"short download ({size}/{expected} bytes)", "SHORT")
                continue  # resume on the next attempt
            break
        except (HttpError, SunoError) as e:
            last_error = e
            if isinstance(e, HttpError) and e.status in (403, 404, 410):
                raise
            if attempt >= retries:
                raise
            time.sleep(min(1.5**attempt + 0.5, 12))
        except urllib.error.HTTPError as e:
            # urlopen reports status codes as exceptions; keep the status visible
            # so callers can tell "gone" from "flaky".
            error = HttpError(e.code, url)
            if e.code in (403, 404, 410) or attempt >= retries:
                raise error from None
            last_error = error
            time.sleep(min(1.5**attempt + 0.5, 12))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            if attempt >= retries:
                raise SunoError(f"download failed: {e}", "NETWORK") from None
            time.sleep(min(1.5**attempt + 0.5, 12))
    else:
        raise SunoError(f"download failed: {last_error}", "DOWNLOAD")

    if part.exists():
        if dest.exists():
            dest.unlink()
        part.replace(dest)
    return dest.stat().st_size


def download_cover(clip: dict, dest_stem: Path, quiet: bool = False) -> Path | None:
    url = clip_cover_url(clip)
    if not url:
        return None
    ext = ".jpg"
    path_part = urllib.parse.urlparse(url).path
    if path_part.lower().endswith((".png", ".webp")):
        ext = os.path.splitext(path_part)[1].lower()
    # append, never replace: a title like "Song v1.2" keeps its dot
    dest = dest_stem.parent / (dest_stem.name + ext)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        tmp = dest.with_name(dest.name + ".part")
        with _open(url, timeout=60) as resp:
            data = resp.read()
        if not data:
            return None
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest
    except Exception as e:
        if not quiet:
            log(f"    cover skipped: {e}")
        return None


# --------------------------------------------------------------------------
# ffmpeg conversion
# --------------------------------------------------------------------------


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def convert_file(src: Path, dest: Path, fmt: str, bitrate: int | None, clip: dict) -> None:
    exe = ffmpeg_path()
    if not exe:
        raise SunoError(f"ffmpeg is required for --format {fmt} (brew install ffmpeg)", "NO_FFMPEG")
    codec = FORMATS[fmt][1] or "copy"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-c:a", codec]
    if bitrate and fmt in ("mp3", "m4a", "aac", "opus"):
        cmd += ["-b:a", f"{bitrate}k"]
    cmd += [
        "-metadata",
        f"title={clip_title(clip)}",
        "-metadata",
        f"artist={clip_artist(clip)}",
        "-metadata",
        "album=Suno",
        "-metadata",
        f"comment=https://suno.com/song/{clip.get('id')}",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SunoError(f"ffmpeg failed: {proc.stderr.strip()[:300]}", "FFMPEG")


# --------------------------------------------------------------------------
# Browser capture
#
# Private clips have no plain file. The only stream is an encrypted MP4 that
# Suno's own page decrypts before it reaches the audio pipeline, so the bytes
# exist in the clear for exactly as long as the page is playing. The capture
# below sits in that window: it patches MediaSource in the page, lets the page
# fetch and decrypt, and reads each buffer on the way to the decoder.
# --------------------------------------------------------------------------

BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# Runs in the page before the app does. Every SourceBuffer holds the decrypted
# stream, so a copy at appendBuffer is a copy of the real audio.
CAPTURE_JS = r"""
(() => {
  const state = { chunks: [], sizes: [], sources: 0, dropped: 0, errors: [] };
  const append = SourceBuffer.prototype.appendBuffer;
  const add = MediaSource.prototype.addSourceBuffer;
  // Suno keeps an idle <audio> next to the real one. Prefer the element that
  // carries a source, or every reading below is about the wrong element.
  const pick = () =>
    Array.from(document.querySelectorAll('audio, video')).find((m) => m.currentSrc || m.readyState > 0) ||
    document.querySelector('audio, video');
  MediaSource.prototype.addSourceBuffer = function (...args) {
    state.sources++;
    return add.apply(this, args);
  };
  SourceBuffer.prototype.appendBuffer = function (buf) {
    try {
      const view = ArrayBuffer.isView(buf)
        ? new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)
        : new Uint8Array(buf);
      if (state.chunks.length < 100000) {
        state.chunks.push(view.slice());
        state.sizes.push(view.byteLength);
      } else state.dropped++;
    } catch (e) { state.errors.push(String(e)); }
    return append.apply(this, arguments);
  };
  window.__capStatus = () => ({
    bytes: state.sizes.reduce((a, b) => a + b, 0),
    sources: state.sources,
    dropped: state.dropped,
    errors: state.errors.slice(0, 5),
  });
  window.__capDrain = (max) => {
    const take = Math.min(max, state.chunks.length);
    const out = [];
    let bytes = 0;
    for (let i = 0; i < take; i++) {
      const chunk = state.chunks[i];
      let raw = '';
      for (let j = 0; j < chunk.byteLength; j += 8192) {
        raw += String.fromCharCode.apply(null, chunk.subarray(j, Math.min(j + 8192, chunk.byteLength)));
      }
      out.push(btoa(raw));
      bytes += chunk.byteLength;
    }
    state.chunks.splice(0, take);
    return { b64: out, bytes };
  };
  window.__capMedia = () => {
    const media = pick();
    if (!media) return null;
    const ranges = media.buffered;
    return {
      t: media.currentTime,
      d: media.duration,
      ended: media.ended,
      paused: media.paused,
      buffered: ranges.length ? ranges.end(ranges.length - 1) : 0,
      rate: media.playbackRate,
      src: (media.currentSrc || '').slice(0, 5),
    };
  };
  window.__capRate = (rate) => {
    const media = pick();
    if (!media) return null;
    media.playbackRate = rate;
    return media.playbackRate;
  };
  window.__capSkip = () => {
    const media = pick();
    if (!media || !media.buffered.length) return null;
    media.currentTime = media.buffered.end(media.buffered.length - 1);
    return media.currentTime;
  };
  window.__capResume = () => {
    const media = pick();
    if (media) { media.play(); return 'element'; }
    return window.__capPlay();
  };
  window.__capPlay = () => {
    const label = (el) => el.getAttribute('aria-label') || el.title || '';
    const button = Array.from(document.querySelectorAll('button')).find((b) => /^play/i.test(label(b)));
    if (button) { button.click(); return 'button'; }
    const media = pick();
    if (media) { media.play(); return 'element'; }
    return 'none';
  };
})();
"""

PLAY_POLL = 0.5  # seconds between page readiness polls
PLAY_RATE = min(20.0, float(os.environ.get("SUNO_DL_SPEED", "16")))  # playback rate while capturing
DRAIN_POLL = 0.5  # seconds between drains
MAX_MEDIA_BYTES = 128 * 1048576
STALL_POLLS = 40  # 20 seconds without progress before giving up
RESUME_TRIES = 40
MIN_MEDIA_BYTES = 4096  # anything shorter is a broken capture, not a song


def find_browser() -> str | None:
    override = os.environ.get("SUNO_DL_BROWSER")
    if override:
        return override
    for path in BROWSER_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ("google-chrome", "chromium", "chrome", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _Frame:
    """Send and receive WebSocket text frames over one socket."""

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        url = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "https":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        if " 101 " not in head.split("\r\n")[0]:
            raise SunoError(f"DevTools refused the socket: {head.splitlines()[:1]}", "BROWSER")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            block = self.sock.recv(65536)
            if not block:
                raise SunoError("browser closed the DevTools socket", "BROWSER")
            self.buffer += block
        head, _, self.buffer = self.buffer.partition(marker)
        return head + marker

    def _read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            block = self.sock.recv(max(65536, count - len(self.buffer)))
            if not block:
                raise SunoError("browser closed the DevTools socket", "BROWSER")
            self.buffer += block
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, timeout: float = 60.0) -> str | None:
        self.sock.settimeout(timeout)
        parts: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read_exact(8))[0]
            data = self._read_exact(size) if size else b""
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + os.urandom(4) + data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if first & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall(bytes([0x8A, 0x80]))
        with contextlib.suppress(OSError):
            self.sock.close()


class DevTools:
    """The slice of the DevTools protocol the capture needs."""

    def __init__(self, ws_url: str) -> None:
        self.frame = _Frame(ws_url)
        self.counter = 0
        self.session: str | None = None

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self.counter += 1
        ident = self.counter
        message = {"id": ident, "method": method, "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        self.frame.send(json.dumps(message))
        while True:
            raw = self.frame.recv(timeout=timeout)
            if raw is None:
                raise SunoError("browser closed the DevTools socket", "BROWSER")
            event = json.loads(raw)
            if event.get("id") != ident:
                continue
            if event.get("error"):
                raise SunoError(f"{method}: {event['error'].get('message')}", "BROWSER")
            return event.get("result") or {}

    def attach(self) -> None:
        targets = self.call("Target.getTargets").get("targetInfos") or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        target_id = (
            page["targetId"]
            if page
            else self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        )
        attached = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.session = attached["sessionId"]

    def ev(self, expression: str, timeout: float = 60.0):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.frame.close()


class Capture:
    """One browser for the whole run, one song at a time."""

    def __init__(self, token: str | None, quiet: bool = False) -> None:
        self.token = token
        self.quiet = quiet
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.devtools: DevTools | None = None

    def _start(self) -> DevTools:
        if self.devtools is not None:
            return self.devtools
        exe = find_browser()
        if not exe:
            raise SunoError(
                "no Chrome or Chromium found. Install one, or point SUNO_DL_BROWSER at it",
                "NO_BROWSER",
            )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        profile = STATE_DIR / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                exe,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1280,900",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        ws_url = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2
                ) as answer:
                    ws_url = json.load(answer).get("webSocketDebuggerUrl")
                if ws_url:
                    break
            except (OSError, ValueError):
                time.sleep(0.2)
        if not ws_url:
            raise SunoError("the browser did not open a DevTools port", "BROWSER")
        devtools = DevTools(ws_url)
        devtools.attach()
        for method in ("Page.enable", "Network.enable", "Runtime.enable"):
            devtools.call(method)
        if self.token:
            devtools.call(
                "Network.setCookie",
                {
                    "name": "__session",
                    "value": self.token,
                    "domain": ".suno.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            )
        devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_JS})
        self.devtools = devtools
        return devtools

    def stop(self) -> None:
        if self.devtools is not None:
            with contextlib.suppress(OSError):
                self.devtools.close()
            self.devtools = None
        if self.proc is not None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                self.proc.wait(timeout=5)
            self.proc = None

    def fetch(self, clip: dict, dest: Path, duration: float, quiet: bool = False) -> dict:
        with self.lock:
            devtools = self._start()
            try:
                return self._fetch(devtools, str(clip.get("id")), dest, duration, quiet)
            except SunoError:
                self.stop()  # a broken page should not poison the next song
                raise

    def _fetch(
        self, devtools: DevTools, clip_id: str, dest: Path, duration: float, quiet: bool
    ) -> dict:
        devtools.call("Page.navigate", {"url": f"https://suno.com/song/{clip_id}"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if devtools.ev("!!document.querySelector('audio, video')"):
                devtools.ev(f"window.__capRate ? window.__capRate({PLAY_RATE}) : 0")
                devtools.ev("window.__capPlay ? window.__capPlay() : 'none'")
            if (devtools.ev("window.__capStatus ? window.__capStatus() : null") or {}).get("bytes"):
                break
        started = time.monotonic()
        deadline = started + max(120.0, (duration or 240.0) * 3 + 120)
        written = 0
        stalls = 0
        resumes = 0
        last_played = 0.0
        last_written = 0
        reason = "timeout"
        with dest.open("wb") as out:
            while time.monotonic() < deadline:
                time.sleep(DRAIN_POLL)
                dump = (
                    devtools.ev("window.__capDrain ? window.__capDrain(400) : null", timeout=120)
                    or {}
                )
                for blob in dump.get("b64") or []:
                    data = base64.b64decode(blob)
                    out.write(data)
                    written += len(data)
                if written > MAX_MEDIA_BYTES:
                    reason = "size"
                    break
                media = devtools.ev("window.__capMedia ? window.__capMedia() : null") or {}
                if media.get("rate") and float(media["rate"]) != PLAY_RATE:
                    devtools.ev(f"window.__capRate ? window.__capRate({PLAY_RATE}) : 0")
                played = float(media.get("t") or 0)
                total = float(media.get("d") or duration or 0)
                if media.get("ended"):
                    reason = "ended"
                    break
                # Chrome fetches ahead of the playhead, so a full buffer means the
                # whole song is already through the hook. Do not wait it out.
                if total and float(media.get("buffered") or 0) >= total - 0.15:
                    reason = "buffered"
                    break
                if total and played >= total - 0.35:
                    reason = "duration"
                    break
                moved = played > last_played + 0.01 or written > last_written
                last_played = max(played, last_played)
                last_written = written
                stalls = 0 if moved else stalls + 1
                # The player pauses itself when it runs dry. Nudge it and keep
                # draining instead of treating that as the end.
                # Playback only stays a window ahead. Jump the head to the end of
                # what is buffered so the player fetches the next window now.
                buffered = float(media.get("buffered") or 0)
                if total and buffered and played >= buffered - 1.5 and buffered < total - 1.0:
                    devtools.ev("window.__capSkip ? window.__capSkip() : 0")
                if stalls > 2 and resumes < RESUME_TRIES and not media.get("ended"):
                    devtools.ev("window.__capResume ? window.__capResume() : 0")
                    resumes += 1
                if stalls > STALL_POLLS:
                    reason = "stalled"
                    break
                if not quiet and written:
                    share = f"{played / total * 100:3.0f}%" if total else "   "
                    print(f"\r  capture {written / 1048576:5.1f} MB {share}", end="", flush=True)
        if not quiet and written:
            print("\r" + " " * 32 + "\r", end="", flush=True)
        stats = devtools.ev("window.__capStatus ? window.__capStatus() : null") or {}
        media = devtools.ev("window.__capMedia ? window.__capMedia() : null") or {}
        return {
            "reason": reason,
            "bytes": written,
            "duration": float(media.get("d") or duration or 0),
            "dropped": stats.get("dropped", 0),
            "errors": stats.get("errors") or [],
            "page": devtools.ev(
                "location.pathname + ' ' + (document.body.innerText || '').slice(0, 120)"
            ),
        }


# --------------------------------------------------------------------------
# Per-clip pipeline
# --------------------------------------------------------------------------


class Downloader:
    def __init__(self, api: SunoAPI, args, history: History):
        self.api = api
        self.args = args
        self.history = history
        self.out_dir = Path(args.out).expanduser()
        self.lock = threading.Lock()
        self.counts = {"ok": 0, "skipped": 0, "failed": 0}
        self.capture: Capture | None = None
        self.has_auth = api is not None and api.auth is not None

    def capturer(self) -> Capture:
        if self.capture is None:
            auth = self.api.auth if self.api else None
            self.capture = Capture(auth.token() if auth else None, quiet=self.args.quiet)
        return self.capture

    # -- fetch a representable clip object ---------------------------------

    def clip_by_id(self, clip_id: str) -> dict:
        try:
            clip = self.api.clip(clip_id)
        except HttpError as e:
            if e.status == 404:
                raise SunoError(
                    f"song {clip_id} not found (or not visible to this account)", "NOT_FOUND"
                ) from None
            raise
        if not isinstance(clip, dict) or not clip.get("id"):
            raise SunoError(f"unexpected clip payload for {clip_id}", "BAD_CLIP")
        return clip

    # -- one song ----------------------------------------------------------

    def process(self, clip: dict) -> str:
        clip_id = str(clip.get("id") or "")
        title = clip_title(clip)
        status = str(clip.get("status") or "")

        if not clip_id:
            self._count("failed")
            return f"FAILED (no id): {title}"
        if status and status != "complete":
            self._count("skipped")
            return f"skip ({status}): {title}"

        fmt = self.args.format
        ext = FORMATS[fmt][0]
        history_key = f"{clip_id}:{ext}"

        previous = self.history.get(history_key) or self.history.get(clip_id)
        if previous and not self.args.force:
            self._count("skipped")
            where = previous.get("path") or ""
            if where and Path(where).parent != self.out_dir:
                return f"have ({ext}): {title} (at {where})"
            return f"have ({ext}): {title}"

        urls = clip_audio_urls(clip)
        plain = bool(usable_url(clip.get("video_url")) or usable_url(clip.get("audio_url")))
        capture = not plain if self.args.capture is None else bool(self.args.capture)
        if capture and not plain and not self.has_auth:
            self._count("failed")
            return (
                f"FAILED (NO_SESSION): {title}. This song only plays in the browser, so "
                "the capture needs --session or --cookie"
            )
        if not capture and not plain:
            self._count("failed")
            return private_or_missing(clip, title)

        if self.args.dry_run:
            self._count("ok")
            if capture:
                return f"would capture ({ext}): {title}"
            return f"would download ({ext}): {title}  <- {short_url(urls[0])}"

        dest = target_path(self.out_dir, clip, ext)

        # An existing file with the same name but no history entry: keep both.
        if not dest.exists():
            pass
        elif not self.args.overwrite:
            dest = unique_path(dest)

        played = 0.0
        tmp_src: Path | None = None
        try:
            if capture:
                tmp_src = self.out_dir / f".{clip_id}.source.mp4"
                stats = self.capturer().fetch(
                    clip, tmp_src, clip_duration(clip), quiet=self.args.quiet
                )
                played = stats["duration"]
                if stats["bytes"] < MIN_MEDIA_BYTES:
                    page = str(stats.get("page") or "")
                    if "Log in" in page or "Sign up" in page:
                        raise SunoError(
                            "the page is signed out. Copy a fresh __session and run again", "AUTH"
                        )
                    raise SunoError(
                        f"the player gave back {stats['bytes']} bytes ({stats['reason']}): {page[:120]}",
                        "CAPTURE",
                    )
                if fmt == "original":
                    final = dest
                    shutil.move(str(tmp_src), str(dest))
                else:
                    convert_file(tmp_src, dest, fmt, self.args.bitrate, clip)
                    final = dest
            elif fmt == "original":
                source_url, size = self._pick_source(urls, clip)
                final = dest
                download_file(source_url, final, expected=size, quiet=self.args.quiet)
            else:
                source_url, size = self._pick_source(urls, clip)
                tmp_src = self.out_dir / f".{clip_id}.source.mp4"
                download_file(source_url, tmp_src, expected=size, quiet=self.args.quiet)
                convert_file(tmp_src, dest, fmt, self.args.bitrate, clip)
                final = dest

            if fmt == "original" and not self.args.no_verify:
                info = inspect_mp4(final)
                want = clip_duration(clip) or played
                bad = list(info["problems"])
                if (
                    want
                    and info["duration"]
                    and abs(info["duration"] - want) > max(3.0, want * 0.02)
                ):
                    bad.append(f"duration {info['duration']:.1f}s vs metadata {want:.1f}s")
                if bad:
                    with contextlib.suppress(OSError):
                        final.unlink()
                    raise SunoError("verification failed: " + "; ".join(bad), "VERIFY")
                # The player stream is fragmented, so mvhd holds no duration.
                if capture and want and played + 1.0 < want:
                    with contextlib.suppress(OSError):
                        final.unlink()
                    raise SunoError(f"the player stopped at {played:.1f}s of {want:.1f}s", "VERIFY")

            cover = None
            if self.args.cover:
                stem = final.name[: -len(final.suffix)] if final.suffix else final.name
                cover = download_cover(clip, final.with_name(stem), quiet=self.args.quiet)

            self.history.add(
                history_key,
                {
                    "clip_id": clip_id,
                    "format": ext,
                    "path": str(final),
                    "title": title,
                    "artist": clip_artist(clip),
                    "bytes": final.stat().st_size,
                    "cover": str(cover) if cover else None,
                    "status": status,
                    "created_at": clip_created(clip),
                    "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
            self._count("ok")
            mb = final.stat().st_size / 1048576
            return f"ok: {final.name} ({mb:.1f} MB)"
        except SunoError as e:
            # A failed conversion can leave a partial target behind.
            if fmt != "original":
                with contextlib.suppress(OSError):
                    dest.unlink()
            self._count("failed")
            return f"FAILED ({e.code}): {title}. {e}"
        except Exception as e:
            self._count("failed")
            return f"FAILED: {title}. {type(e).__name__}: {e}"
        finally:
            if tmp_src and tmp_src.exists():
                with contextlib.suppress(OSError):
                    tmp_src.unlink()

    def _pick_source(self, urls: list[str], clip: dict) -> tuple[str, int | None]:
        """Return the first candidate URL that serves bytes."""
        last: SunoError | None = None
        for url in urls:
            if "/api/forbidden" in url:
                continue
            try:
                status, headers, _ = request(
                    url, method="HEAD", timeout=30, retries=1, allow=(403, 404, 410)
                )
                if status == 200:
                    ctype = headers.get("Content-Type") or headers.get("content-type") or ""
                    if any(bad in ctype.lower() for bad in ("text/", "application/xml")):
                        raise SunoError(
                            f"server returned {ctype or 'a non-media body'}, not audio",
                            "NOT_MEDIA",
                        )
                    length = headers.get("Content-Length") or headers.get("content-length")
                    return url, int(length) if length else None
                last = SunoError(f"HTTP {status}", "HTTP", status)
            except SunoError as e:
                last = e
                continue
        raise SunoError(
            f"no plain media is published for this song ({last})",
            "NO_MEDIA",
        )

    def _count(self, key: str) -> None:
        with self.lock:
            self.counts[key] += 1


# --------------------------------------------------------------------------
# Library listing
# --------------------------------------------------------------------------


def dedupe(clips: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        cid = str(clip.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(clip)
    return out


def identity_from_me(me: dict) -> tuple[str | None, str | None]:
    """Best-effort (user_id, handle) from an /api/user/me payload."""
    user = me.get("user")
    nested: dict = user if isinstance(user, dict) else {}
    uid = me.get("id") or me.get("user_id") or nested.get("id")
    handle = me.get("handle") or me.get("user_handle") or nested.get("handle")
    return (str(uid) if uid else None), (str(handle) if handle else None)


def account_identity(api: SunoAPI) -> tuple[str | None, str | None]:
    """Best-effort (user_id, handle) of the signed-in account."""
    try:
        me = api.me()
    except SunoError:
        return None, None
    return identity_from_me(me)


def only_mine(clips: list[dict], uid: str | None, handle: str | None) -> list[dict]:
    """Never download someone else's songs because a feed answered with a public page."""
    if not uid and not handle:
        return clips
    mine: list[dict] = []
    for clip in clips:
        owner_id = str(clip.get("user_id") or (clip.get("user") or {}).get("id") or "")
        owner_handle = str(clip.get("handle") or clip.get("user_handle") or "")
        if (uid and owner_id == uid) or (handle and owner_handle == handle):
            mine.append(clip)
        elif not owner_id and not owner_handle:
            mine.append(clip)  # no ownership fields at all: cannot judge, keep
    return mine


def list_library(api: SunoAPI, limit: int, verbose: bool = True) -> tuple[list[dict], str]:
    """Try the endpoints the suno.com client itself uses, best first."""
    attempts: list[tuple[str, Callable[[], list[dict]]]] = []

    def via_feed_v3() -> list[dict]:
        clips: list[dict] = []
        cursor: str | None = None
        for _ in range(200):
            data = api.feed(filters={}, cursor=cursor, limit=min(limit or 50, 100))
            batch = data.get("clips") or []
            clips.extend(batch)
            cursor = data.get("next_cursor")
            if not batch or not cursor or (limit and len(clips) >= limit):
                break
        return clips

    def via_feed_legacy() -> list[dict]:
        clips: list[dict] = []
        for page in range(200):
            batch = api.feed_legacy(page)
            if not batch:
                break
            clips.extend(batch)
            if limit and len(clips) >= limit:
                break
        return clips

    def via_profile() -> list[dict]:
        me = api.me()
        handle = me.get("handle") or (me.get("user") or {}).get("handle")
        if not handle:
            raise SunoError("account has no handle", "NO_HANDLE")
        clips: list[dict] = []
        page = 0
        while True:
            data = api.profile(handle, page=page, limit=20)
            batch = data.get("clips") or []
            clips.extend(batch)
            total = int(data.get("num_total_clips") or 0)
            page += 1
            if not batch or len(batch) < 20 or (total and len(clips) >= total) or page > 100:
                break
        return clips

    attempts.append(("library feed (POST /api/feed/v3)", via_feed_v3))
    attempts.append(("legacy feed (GET /api/feed/)", via_feed_legacy))
    attempts.append(("profile listings (GET /api/profiles/…)", via_profile))

    errors: list[str] = []
    for label, fn in attempts:
        try:
            clips = dedupe(fn())
        except SunoError as e:
            errors.append(f"{label}: {e}")
            continue
        if clips:
            uid, handle = account_identity(api)
            owned = only_mine(clips, uid, handle)
            if clips and not owned:
                errors.append(
                    f"{label}: returned {len(clips)} clip(s) that belong to other accounts"
                )
                continue
            if verbose:
                extra = (
                    ""
                    if len(owned) == len(clips)
                    else f" ({len(clips) - len(owned)} other people's skipped)"
                )
                log(f"library: {len(owned)} song(s) via {label}{extra}")
            return owned[:limit] if limit else owned, label
        errors.append(f"{label}: returned nothing")

    raise SunoError(
        "could not list your library. Every endpoint came back empty:\n  " + "\n  ".join(errors),
        "NO_LIBRARY",
    )


def list_playlist(api: SunoAPI, playlist_id: str) -> list[dict]:
    clips: list[dict] = []
    page = 0
    while True:
        data = api.playlist(playlist_id, page=page, limit=100)
        entries = data.get("playlist_clips") or []
        for entry in entries:
            clip = entry.get("clip") if isinstance(entry, dict) else None
            if isinstance(clip, dict):
                clips.append(clip)
        total = int(data.get("num_total_results") or 0)
        page += 1
        if not entries or len(entries) < 100 or (total and len(clips) >= total) or page > 100:
            break
    return dedupe(clips)


def list_profile(api: SunoAPI, handle: str, limit: int) -> list[dict]:
    clips: list[dict] = []
    page = 0
    while True:
        data = api.profile(handle, page=page, limit=20)
        batch = data.get("clips") or []
        clips.extend(batch)
        total = int(data.get("num_total_clips") or 0)
        page += 1
        if not batch or len(batch) < 20 or page > 200:
            break
        if total and len(clips) >= total:
            break
        if limit and len(clips) >= limit:
            break
    return dedupe(clips)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def default_out_dir() -> str:
    return str(Path.home() / "Music" / "Suno")


EPILOG = """\
examples:
  suno-dl --library                        everything in your library
  suno-dl https://suno.com/song/<uuid>     one song
  suno-dl https://suno.com/@yourhandle     a profile
  suno-dl --library -f mp3 -b 320 -j 4     convert while downloading

Get the cookie: DevTools -> Application -> Cookies -> suno.com (__session)
                 or auth.suno.com (__client)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="suno-dl",
        description="Download your own Suno songs at original quality (stdlib only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "targets",
        nargs="*",
        metavar="URL|ID",
        help="song / playlist / profile link, a song UUID, or @handle",
    )
    p.add_argument("--library", action="store_true", help="download everything in your own library")
    p.add_argument(
        "--cookie",
        default=os.environ.get("SUNO_COOKIE"),
        help="a __client cookie, a whole Cookie header, or __session=… (default: $SUNO_COOKIE)",
    )
    p.add_argument(
        "--session",
        default=os.environ.get("SUNO_SESSION"),
        help="a ready __session JWT from suno.com (works at once, expires in minutes)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="verify the session against the API, then exit",
    )
    p.add_argument("-o", "--out", default=default_out_dir(), help="output folder")
    p.add_argument(
        "-f",
        "--format",
        default="original",
        choices=sorted(FORMATS),
        help="output format (default: original = untouched MP4)",
    )
    p.add_argument(
        "-b",
        "--bitrate",
        type=int,
        default=None,
        help="audio bitrate in kbps for mp3/m4a/opus (default: 320/256/128)",
    )
    p.add_argument("-j", "--jobs", type=int, default=3, help="parallel downloads (default 3)")
    p.add_argument("-n", "--limit", type=int, default=0, help="stop after N songs")
    p.add_argument(
        "--since", default=None, help="only songs created on/after this date (YYYY-MM-DD)"
    )
    p.add_argument(
        "--cover",
        dest="cover",
        action="store_true",
        default=True,
        help="save cover art next to the audio (default)",
    )
    p.add_argument("--no-cover", dest="cover", action="store_false", help="do not save cover art")
    p.add_argument("--no-verify", action="store_true", help="skip the MP4 structure check")
    p.add_argument(
        "--capture",
        dest="capture",
        action="store_true",
        help="play the song in a headless browser and record it, even when a plain file exists",
    )
    p.add_argument(
        "--no-capture",
        dest="capture",
        action="store_false",
        help="never start a browser, skip songs that are only served to the player",
    )
    p.set_defaults(capture=None)
    p.add_argument("--force", action="store_true", help="re-download songs already in history")
    p.add_argument("--overwrite", action="store_true", help="overwrite same-named files")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    p.add_argument("--list", action="store_true", help="list the songs and exit")
    p.add_argument("--json", action="store_true", help="machine-readable listing output")
    p.add_argument("-q", "--quiet", action="store_true", help="less output")
    p.add_argument("-v", "--verbose", action="store_true", help="show auth/endpoint details")
    return p


def resolve_clips(api: SunoAPI, args) -> list[dict]:
    clips: list[dict] = []

    if args.library:
        library, _label = list_library(api, args.limit, verbose=not args.quiet)
        clips.extend(library)

    for raw in args.targets:
        kind, value = parse_target(raw)
        if kind == "short":
            kind, value = parse_target(resolve_short_link(value))
        if kind == "song":
            try:
                clips.append(api.clip(value))
            except HttpError as e:
                if e.status == 404:
                    raise SunoError(
                        f"song {value} was not found, or this account cannot see it",
                        "NOT_FOUND",
                    ) from None
                raise
        elif kind == "playlist":
            batch = list_playlist(api, value)
            if not batch:
                raise SunoError(f"playlist {value} is empty or not visible", "EMPTY")
            clips.extend(batch)
        elif kind == "profile":
            batch = list_profile(api, value, args.limit)
            if not batch:
                raise SunoError(f"profile @{value} has no visible songs", "EMPTY")
            clips.extend(batch)

    clips = dedupe(clips)

    if args.since:
        try:
            cutoff = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise SystemExit(f"--since expects YYYY-MM-DD, got {args.since!r}") from None
        kept = []
        for clip in clips:
            created = clip_created(clip)
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                kept.append(clip)
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                kept.append(clip)
        clips = kept

    clips = [c for c in clips if str(c.get("status") or "complete") == "complete"]

    if args.limit:
        clips = clips[: args.limit]
    return clips


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.format in ("mp3", "m4a", "aac", "flac", "wav", "opus"):
        if not ffmpeg_path():
            log("error: ffmpeg not found on PATH (brew install ffmpeg / apt install ffmpeg)")
            return 2
        if args.bitrate is None:
            args.bitrate = FORMATS[args.format][2]

    if not args.cookie and not args.session and (args.library or args.check):
        # Public songs, playlists and profiles work without a token; a library needs one.
        log(
            "error: this needs a session. Pass --session with your __session JWT, "
            "or --cookie with a __client cookie"
        )
        return 2

    auth: Auth | None = None
    if args.cookie or args.session:
        try:
            auth = Auth(args.cookie, args.session, verbose=args.verbose)
            auth.token()  # fail fast on bad credentials
        except SunoError as e:
            log(f"error: {e}")
            return 2

    api = SunoAPI(auth)

    if args.check:
        if auth is None:
            log("error: --check needs --session or --cookie")
            return 2
        try:
            me = api.me()
        except SunoError as e:
            log(f"error: {e}")
            return 2
        _uid, handle = identity_from_me(me)
        name = str(me.get("display_name") or "").strip()
        who = "@" + handle if handle else (_uid or "unknown")
        print(f"ok: signed in as {who}" + (f" ({name})" if name else ""))
        print(f"    {auth.source()} token, {auth.describe_expiry()}")
        if auth.source() == "__session":
            print("    tip: re-copy __session when it runs out, or use __client for long runs")
        return 0

    try:
        clips = resolve_clips(api, args)
    except SunoError as e:
        log(f"error: {e}")
        return 2

    if not clips:
        log("nothing to download")
        return 0

    if args.list:
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "id": c.get("id"),
                            "title": clip_title(c),
                            "artist": clip_artist(c),
                            "duration": clip_duration(c),
                            "created_at": clip_created(c),
                            "url": f"https://suno.com/song/{c.get('id')}",
                            "audio_url": next(iter(clip_audio_urls(c)), None),
                        }
                        for c in clips
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for c in clips:
                mins, secs = divmod(int(clip_duration(c) or 0), 60)
                print(f"{c.get('id')}  {mins}:{secs:02d}  {clip_artist(c)} - {clip_title(c)}")
        return 0

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    history = History()
    downloader = Downloader(api, args, history)

    if args.jobs > 1:
        args.quiet = True  # interleaved progress bars would be noise

    log(f"{len(clips)} song(s) -> {out_dir}  (format: {args.format}, jobs: {args.jobs})")

    failures = 0
    try:
        with futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            future_map = {pool.submit(downloader.process, clip): clip for clip in clips}
            for fut in futures.as_completed(future_map):
                clip = future_map[fut]
                try:
                    line = fut.result()
                except Exception as e:
                    line = f"FAILED: {clip_title(clip)}. {e}"
                if "FAILED" in line:
                    failures += 1
                print(line, flush=True)
    finally:
        if downloader.capture is not None:
            downloader.capture.stop()

    verb = "would download" if args.dry_run else "downloaded"
    log(
        f"done: {downloader.counts['ok']} {verb}, "
        f"{downloader.counts['skipped']} skipped, {downloader.counts['failed']} failed"
    )
    return 1 if failures or downloader.counts["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
