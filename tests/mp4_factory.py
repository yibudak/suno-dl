"""Build tiny but structurally valid MP4 files for the verifier tests."""

from __future__ import annotations

import struct


def box(kind: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind.encode("latin1") + payload


def mvhd(timescale: int, duration: int) -> bytes:
    payload = bytes([0, 0, 0, 0])  # version 0 + flags
    payload += struct.pack(">II", 0, 0)  # creation, modification
    payload += struct.pack(">II", timescale, duration)
    payload += struct.pack(">I", 0x00010000)  # rate
    payload += struct.pack(">H", 0x0100)  # volume
    payload += b"\x00" * 10
    payload += b"\x00" * 36  # matrix
    payload += b"\x00" * 24  # predefined
    payload += struct.pack(">I", 2)  # next track id
    return box("mvhd", payload)


def progressive(duration_ms: int = 12345, media: int = 4096) -> bytes:
    """ftyp + mdat + moov (moov last, like a file written in one pass)."""
    return (
        box("ftyp", b"isomisomavc1")
        + box("mdat", b"\x00" * media)
        + box("moov", mvhd(1000, duration_ms))
    )


def fragmented(duration_ms: int = 60000, fragments: int = 3) -> bytes:
    """ftyp + moov + N x (moof + mdat), the shape a media stream has."""
    out = box("ftyp", b"isomiso6") + box("moov", mvhd(1000, duration_ms))
    for _ in range(fragments):
        out += box("moof", b"\x00" * 16) + box("mdat", b"\x00" * 4096)
    return out
