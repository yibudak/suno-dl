<div align="center">

# suno-dl

**Your Suno library, on your disk, at original quality.**

One Python file. No browser, no dependencies, no ffmpeg unless you ask for it.

![python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-black?style=flat-square)

</div>

---

`suno-dl` signs in with your own Suno session, lists the songs your account owns,
and downloads the original MP4 — the same asset Suno's API hands out — then checks
the file is intact before calling it done. Audio-only formats are one flag away.

```console
$ suno-dl --library
3 song(s) -> /Users/you/Music/Suno  (format: original, jobs: 3)
ok: Afterglow.mp4 (8.3 MB)
ok: Slow Motion.mp4 (8.8 MB)
ok: Paper Planes.mp4 (8.2 MB)
done: 3 downloaded, 0 skipped, 0 failed
```

## Why this one

- **One file, zero dependencies.** Standard library only. Copy `suno_dl.py`
  anywhere and run it; nothing to install, nothing to break.
- **Original quality.** No re-encoding by default — the MP4 exactly as served.
- **It finishes what it starts.** HTTP Range resume, retries, and a structural
  check of every file (box layout + duration) so a dropped connection never
  leaves you with a half-song.
- **It remembers.** A history file skips what you already have, and cover art
  lands next to the audio.
- **Parallel, scriptable.** `--jobs`, `--since`, `--list --json` for pipelines.

## Install

```bash
# 1. zero install: suno_dl.py is the whole tool — copy it anywhere and run it
python3 suno_dl.py --library

# 2. or install the command from a clone
pipx install .
suno-dl --library
```

Python 3.10+. No virtualenv, no build step, no dependencies.

## Sign in

Suno has no API keys, so `suno-dl` borrows the session your browser already
holds. You copy one cookie out of DevTools — depending on your account you will
see `__session`, `__client`, or both:

| cookie | lives on | lifetime | what the tool does with it |
|---|---|---|---|
| `__session` | suno.com | minutes | sends it straight to the API as the bearer token |
| `__client` | auth.suno.com | weeks | exchanges it for a fresh bearer token, again and again |

### Fast path — only `__session`? That works

1. DevTools → **Application** → **Cookies** → `https://suno.com` → `__session`.
2. Copy the value (a long `eyJ…` JWT) and pass it in:

```bash
export SUNO_SESSION='eyJhbGciOi…'
suno-dl --check
```

`--check` tells you who you are and how long the token has left:

```console
$ suno-dl --check
ok: signed in as @yourhandle (Your Name)
    __session token, expires in 47s
    tip: re-copy __session when it runs out, or use __client for long runs
```

The JWT lives for minutes, but it is only needed to **list** your songs — the
files themselves come from the CDN, unauthenticated. A fresh token is therefore
enough to start even a large library; if the listing itself outlives it, the tool
says so instead of failing silently.

### Long runs — `__client`

If `https://auth.suno.com` has a `__client` cookie as well, use that one
instead: it is long-lived and the tool mints fresh tokens from it on its own.

```bash
export SUNO_COOKIE='__client=eyJhbGciOi…'
suno-dl --check
suno-dl --library
```

A whole `Cookie:` header pasted into `--cookie` works too — `__session` and
`__client` are picked out of it automatically.

Either way, treat the value like a password: it opens your whole library. Keep
it in a secret manager or an env var, not in shell history.

## Use it

```bash
suno-dl --library                        # everything you own
suno-dl https://suno.com/song/<uuid>     # one song
suno-dl https://suno.com/playlist/<id>   # a playlist
suno-dl https://suno.com/@yourhandle     # a profile
suno-dl --library --list                 # look first, download later
suno-dl --library --dry-run              # show the plan, touch nothing
```

| flag | what it does |
|---|---|
| `-f, --format` | `original` (default) · `mp3` · `m4a` · `flac` · `wav` · `opus` |
| `-b, --bitrate` | kbps for mp3/m4a/opus — 320 is the default |
| `-j, --jobs` | parallel downloads (default 3) |
| `-n, --limit` | stop after N songs |
| `--since YYYY-MM-DD` | only songs created from that date |
| `-o, --out` | output folder (default `~/Music/Suno`) |
| `--force` | re-download even if history says you have it |
| `--no-cover` | skip the sidecar artwork |

Public songs, playlists and profiles work without a cookie. Private and unlisted
songs need `--library` with your session.

### What it can — and cannot — fetch

Suno publishes a plain MP4 only for songs that are **public** and downloadable on
your plan. A private or unlisted song has no such file: its only stream is the
encrypted one Suno's player unlocks with a license key. The tool says so instead
of saving ciphertext:

```console
FAILED (PRIVATE): <title> — Suno only serves private/unlisted songs through its
encrypted player, so there is no plain file to download.
```

Want a copy of your own private song? Publish it in Suno, run `suno-dl`, then
flip it back to private. If `/api/clip/{id}` reports `"is_download_unlocked":
false`, downloads are not enabled for that account/plan at all — that is a Suno
setting, not something this tool can change.

## Under the hood

For each song the tool:

1. asks `/api/clip/{id}` for title, artist, duration, artwork and media URL;
2. picks the first candidate that answers with real media, preferring the CDN MP4;
3. downloads it with `Range`, resuming a `.part` file after a dropped connection;
4. verifies the container — `ftyp` first, exactly one `moov`, no truncated box,
   payload size and duration consistent with the API;
5. writes the cover beside the audio and records the song in
   `~/.suno-dl/history.json`.

Names follow `Artist - Title.ext`, sanitised for Windows, macOS and Linux, with
` (1)`, ` (2)` on collisions.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pre-commit install --hook-type pre-commit --hook-type pre-push
pytest                      # 90 tests, no network
ruff check . && ruff format --check .
mypy suno_dl.py
```

CI runs lint, types, the test suite on Python 3.10–3.14 across Linux, macOS and
Windows, plus a wheel build that installs and runs `suno-dl --version`.
Tagging `v*.*.*` builds the release, attaches it to a GitHub Release and
publishes to PyPI through trusted publishing.

## Fair use

This tool authenticates as **you** and downloads what your account can already
play. Download your own work, or work you have the right to keep. It does not
bypass any protection and does not touch Suno's encrypted stream. Respect the
platform's terms and the artists.

## License

MIT — see [LICENSE](LICENSE).
