"""Shared fixtures: an offline HTTP server that behaves like a media CDN."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest


class _Handler(BaseHTTPRequestHandler):
    files: ClassVar[dict[str, bytes]] = {}
    seen_range: ClassVar[list[str]] = []
    ignore_range: ClassVar[bool] = False

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        name = self.path.split("?")[0].lstrip("/")
        data = self.files.get(name)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        range_header = self.headers.get("Range")
        if range_header and not self.ignore_range and range_header.startswith("bytes="):
            self.seen_range.append(range_header)
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)

        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture
def http_server():
    """Yield (base_url, handler_class) backed by a throwaway local server."""
    handler = type(
        "IsolatedHandler",
        (_Handler,),
        {"files": {}, "seen_range": [], "ignore_range": False},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the history file somewhere disposable."""
    import suno_dl

    path = tmp_path / "state" / "history.json"
    monkeypatch.setattr(suno_dl, "HISTORY_FILE", path)
    return path
