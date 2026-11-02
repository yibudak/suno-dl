"""The browser capture path: WebSocket framing, the page hook, and the flow."""

from __future__ import annotations

import base64
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

import suno_dl
from suno_dl import CAPTURE_JS, Capture, DevTools, SunoError, _Frame, find_browser

CLIP = {"id": "11111111-2222-4333-8444-555555555555", "is_public": False}


# --------------------------------------------------------------------------
# WebSocket framing
# --------------------------------------------------------------------------


class _Server(threading.Thread):
    """Accept one client and answer like a DevTools socket."""

    def __init__(self, reply: str) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        self.received: list[dict] = []

    def run(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            data = conn.recv(65536)
            length = data[1] & 0x7F
            start = 2
            if length == 126:
                length = struct.unpack(">H", data[2:4])[0]
                start = 4
            mask = data[start : start + 4]
            body = bytes(
                b ^ mask[i % 4] for i, b in enumerate(data[start + 4 : start + 4 + length])
            )
            self.received.append(json_blob(body))
            payload = self.reply.encode()
            conn.sendall(bytes([0x81, len(payload)]) + payload)


def json_blob(raw: bytes) -> dict:
    import json

    return json.loads(raw)


def test_frame_rejects_a_plain_socket():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def answer() -> None:
            conn, _ = sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        with pytest.raises(SunoError, match="refused"):
            _Frame(f"ws://127.0.0.1:{port}/x")


def test_devtools_call_skips_events_and_masks_frames():
    reply = '{"id":1,"result":{"ok":true}}'
    server = _Server(reply)
    server.start()
    frame = _Frame(f"ws://127.0.0.1:{server.port}/devtools/browser/x")
    devtools = DevTools.__new__(DevTools)
    devtools.frame = frame
    devtools.counter = 0
    devtools.session = None
    assert devtools.call("Target.getTargets") == {"ok": True}
    sent = server.received[0]
    assert sent["method"] == "Target.getTargets"
    frame.close()


def test_devtools_raises_on_error_payload():
    server = _Server('{"id":1,"error":{"message":"no such target"}}')
    server.start()
    frame = _Frame(f"ws://127.0.0.1:{server.port}/x")
    devtools = DevTools.__new__(DevTools)
    devtools.frame, devtools.counter, devtools.session = frame, 0, None
    with pytest.raises(SunoError, match="no such target"):
        devtools.call("Nope")
    frame.close()


# --------------------------------------------------------------------------
# The hook and browser lookup
# --------------------------------------------------------------------------


def test_hook_patches_the_pipeline_and_drains():
    assert "appendBuffer" in CAPTURE_JS
    assert "addSourceBuffer" in CAPTURE_JS
    assert "__capDrain" in CAPTURE_JS
    assert "btoa" in CAPTURE_JS


def test_browser_override_wins(monkeypatch):
    monkeypatch.setenv("SUNO_DL_BROWSER", "/nowhere/chrome")
    assert find_browser() == "/nowhere/chrome"


def test_browser_candidates_are_absolute():
    for path in suno_dl.BROWSER_CANDIDATES:
        assert path.startswith("/") or path[1:3] == ":\\"


# --------------------------------------------------------------------------
# The capture flow itself
# --------------------------------------------------------------------------


class FakeDevTools:
    """Answer the JavaScript the capture asks for, without a browser."""

    def __init__(self, total: float, chunk: bytes, limit: int = 4) -> None:
        self.total = total
        self.chunk = chunk
        self.limit = limit
        self.polls = 0
        self.clock = 0.0
        self.navigated: list[str] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        if method == "Page.navigate":
            self.navigated.append((params or {})["url"])
        return {}

    def ev(self, expression: str, timeout: float = 60.0):
        if "__capPlay" in expression:
            return "button"
        if "querySelector" in expression:
            return True
        if "__capRate" in expression:
            return 1
        if "__capDrain" in expression:
            if self.polls >= self.limit:
                return {"b64": [], "bytes": 0}
            self.polls += 1
            self.clock = self.total * self.polls / self.limit
            return {"b64": [base64.b64encode(self.chunk).decode()], "bytes": len(self.chunk)}
        if "__capMedia" in expression:
            return {"t": self.clock, "d": self.total, "ended": False}
        if "__capStatus" in expression:
            return {"bytes": 1 + self.polls, "dropped": 0, "errors": []}
        if "location.pathname" in expression:
            return "/song/x"
        return None

    def close(self) -> None:
        pass


def _capture() -> Capture:
    return Capture("token", quiet=True)


def test_fetch_writes_the_stream(tmp_path: Path):
    capture = _capture()
    fake = FakeDevTools(total=20.0, chunk=b"\x00\x00\x00\x1cftyp" + b"x" * 60)
    dest = tmp_path / "out.mp4"
    stats = capture._fetch(fake, CLIP["id"], dest, 20.0, quiet=True)
    assert stats["reason"] == "duration"
    assert stats["duration"] == pytest.approx(20.0)
    assert dest.read_bytes().startswith(b"\x00\x00\x00\x1cftyp")
    assert fake.navigated == [f"https://suno.com/song/{CLIP['id']}"]


def test_fetch_stops_on_a_full_buffer_not_the_playhead(tmp_path: Path):
    """A buffered song must not be waited out at 1x."""
    capture = _capture()
    fake = FakeDevTools(total=200.0, chunk=b"q", limit=2)
    fake.ev = lambda expression, timeout=60.0: (
        {"b64": [base64.b64encode(b"q" * 4).decode()], "bytes": 4}
        if "__capDrain" in expression
        else {"t": 1.5, "d": 200.0, "ended": False, "buffered": 200.0, "rate": 16.0}
        if "__capMedia" in expression
        else {"bytes": 4}
        if "__capStatus" in expression
        else "button"
    )
    dest = tmp_path / "buffered.mp4"
    stats = capture._fetch(fake, CLIP["id"], dest, 200.0, quiet=True)
    assert stats["reason"] == "buffered"
    assert stats["duration"] == pytest.approx(200.0)


def test_default_rate_is_capped():
    assert min(20.0, float(suno_dl.os.environ.get("SUNO_DL_SPEED", "16"))) == suno_dl.PLAY_RATE


def test_fetch_stops_when_the_media_ends(tmp_path: Path):
    capture = _capture()
    fake = FakeDevTools(total=0.0, chunk=b"y" * 10)
    fake.ev = lambda expression, timeout=60.0: (
        {"b64": [base64.b64encode(b"z" * 8).decode()], "bytes": 8}
        if "__capDrain" in expression
        else {"t": 5.0, "d": 5.0, "ended": True}
        if "__capMedia" in expression
        else {"bytes": 8}
        if "__capStatus" in expression
        else "button"
    )
    dest = tmp_path / "ended.mp4"
    stats = capture._fetch(fake, CLIP["id"], dest, 0.0, quiet=True)
    assert stats["reason"] == "ended"
    assert dest.stat().st_size == 8


def test_fetch_splices_chunks_in_order(tmp_path: Path):
    capture = _capture()
    fake = FakeDevTools(total=4.0, chunk=b"ab", limit=3)
    dest = tmp_path / "ordered.mp4"
    capture._fetch(fake, CLIP["id"], dest, 4.0, quiet=True)
    assert dest.read_bytes() == b"ababab"


def test_capture_is_used_when_a_clip_has_no_plain_file(tmp_path: Path):
    down = _downloader(tmp_path)
    seen: list[dict] = []

    class FakeCapture:
        def fetch(self, clip, dest, duration, quiet=False):
            seen.append(clip)
            dest.write_bytes(b"\x00\x00\x00\x1cftyp" + b"m" * 4096)
            return {
                "reason": "duration",
                "bytes": 4100,
                "duration": 12.0,
                "dropped": 0,
                "errors": [],
            }

    down.capture = FakeCapture()
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert line.startswith("ok:"), line
    assert seen
    assert seen[0]["id"] == CLIP["id"]


def test_public_clip_skips_the_browser(tmp_path: Path):
    down = _downloader(tmp_path)

    class Boom:
        def fetch(self, *a, **kw):
            raise AssertionError("browser should not start")

    down.capture = Boom()
    down._pick_source = lambda urls, clip: (urls[0], 4)
    suno_dl.download_file = lambda url, dest, expected=None, quiet=False: dest.write_bytes(
        b"\x00" * 4
    )
    line = down.process(
        {
            "id": CLIP["id"],
            "title": "Public One",
            "status": "complete",
            "is_public": True,
            "video_url": "https://cdn1.suno.ai/x.mp4",
        }
    )
    assert line.startswith("ok:"), line


def test_private_capture_without_a_session_says_so(tmp_path: Path):
    down = _downloader(tmp_path, auth=None)
    down.api.auth = None
    down.has_auth = False
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert "NO_SESSION" in line


def test_public_capture_runs_without_a_session(tmp_path: Path):
    down = _downloader(tmp_path, auth=None)
    down.api.auth = None
    down.has_auth = False
    down.args.capture = True
    seen: list[str] = []

    class Stub:
        def fetch(self, clip, dest, duration, quiet=False):
            seen.append("captured")
            dest.write_bytes(b"\x00\x00\x00\x1cftyp" + b"m" * 5000)
            return {
                "reason": "duration",
                "bytes": 5004,
                "duration": 9.0,
                "dropped": 0,
                "errors": [],
            }

    down.capture = Stub()
    line = down.process(
        {
            "id": CLIP["id"],
            "title": "Public One",
            "status": "complete",
            "is_public": True,
            "video_url": "https://cdn1.suno.ai/x.mp4",
        }
    )
    assert seen == ["captured"]
    assert line.startswith("ok:"), line


def test_no_capture_keeps_the_browser_off(tmp_path: Path):
    down = _downloader(tmp_path)
    down.args.capture = False
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert "PRIVATE" in line


def test_signed_out_page_is_reported(tmp_path: Path):
    down = _downloader(tmp_path)

    class Stub:
        def fetch(self, clip, dest, duration, quiet=False):
            dest.write_bytes(b"")
            return {
                "reason": "timeout",
                "bytes": 0,
                "duration": 0.0,
                "dropped": 0,
                "errors": [],
                "page": "/discover Home Explore Log in",
            }

    down.capture = Stub()
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert "AUTH" in line
    assert "signed out" in line


def test_empty_capture_is_an_error(tmp_path: Path):
    down = _downloader(tmp_path)

    class Stub:
        def fetch(self, clip, dest, duration, quiet=False):
            dest.write_bytes(b"")
            return {"reason": "timeout", "bytes": 0, "duration": 0.0, "dropped": 0, "errors": []}

    down.capture = Stub()
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert "CAPTURE" in line


def test_dry_run_names_the_capture(tmp_path: Path):
    down = _downloader(tmp_path)
    down.args.dry_run = True
    line = down.process(dict(CLIP, title="Private One", status="complete"))
    assert "would capture" in line


class _Args:
    def __init__(self, out: Path) -> None:
        self.out = str(out)
        self.format = "original"
        self.jobs = 1
        self.bitrate = None
        self.quiet = True
        self.cover = False
        self.force = False
        self.overwrite = False
        self.dry_run = False
        self.no_verify = True
        self.capture = None


class _Auth:
    def token(self) -> str:
        return "jwt"


def _downloader(tmp_path: Path, auth: object | None = None):
    from suno_dl import Downloader, History, SunoAPI

    return Downloader(SunoAPI(auth or _Auth()), _Args(tmp_path), History(tmp_path / "history.json"))


def test_capture_stops_the_browser(monkeypatch):
    capture = _capture()
    killed: list[bool] = []

    class Proc:
        def terminate(self):
            killed.append(True)

        def wait(self, timeout=None):
            return 0

    capture.proc = Proc()
    capture.devtools = FakeDevTools(1, b"x")
    capture.stop()
    assert killed == [True]
    assert capture.devtools is None
    assert capture.proc is None


def test_stop_is_safe_before_start():
    _capture().stop()
    time.sleep(0)
