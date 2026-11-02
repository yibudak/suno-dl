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

Suno has no API keys, so `suno-dl` uses the same short-lived token flow as the
web app. Grab your cookie once:

1. Open <https://suno.com> and sign in.
2. DevTools → **Application** → **Cookies** → `https://auth.suno.com`.
3. Copy the value of **`__client`** (a long `eyJ…` string) — it is `HttpOnly`,
   which is exactly why DevTools is the only way to see it.

```bash
export SUNO_COOKIE='__client=eyJhbGciOi…'
```

The `__client` cookie is exchanged for a JWT that expires in minutes; the script
re-mints it automatically. Treat the cookie like a password — it opens your
whole library. Prefer a secret manager or an env var over shell history.

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
pytest                      # 80 tests, no network
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
