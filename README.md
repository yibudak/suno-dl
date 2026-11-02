<div align="center">

# suno-dl

**Download the songs you made on Suno, at the quality Suno serves them.**

The whole tool is one Python file that uses only the standard library.

![python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-black?style=flat-square)

</div>

`suno-dl` signs in with your Suno session, lists the songs your account owns, and
saves the original MP4. It reads the file back before reporting success, so a
dropped connection fails the song instead of leaving a half file. Ask for mp3,
flac, wav, opus or m4a and it runs the conversion through ffmpeg.

```console
$ suno-dl --library
3 song(s) -> /Users/you/Music/Suno  (format: original, jobs: 3)
ok: Afterglow.mp4 (8.3 MB)
ok: Slow Motion.mp4 (8.8 MB)
ok: Paper Planes.mp4 (8.2 MB)
done: 3 downloaded, 0 skipped, 0 failed
```

## What you get

- **The file Suno serves.** The default format copies the MP4 without re-encoding.
- **Resume and verification.** A dropped download continues from the last byte,
  and the tool inspects the MP4 boxes and duration before it marks the song done.
- **A history file.** `~/.suno-dl/history.json` records what you downloaded, so
  the next run skips those songs. Cover art lands beside the audio.
- **Flags for scripts.** `--jobs` runs several downloads at once, `--since`
  limits a run by date, and `--list --json` prints the queue without downloading.

## Install

```bash
# run the file directly
python3 suno_dl.py --library

# or install the command from a clone
pipx install .
suno-dl --library
```

Python 3.10 or newer. The single file needs no virtualenv, no build step and no
dependencies.

## Sign in

Suno has no API keys, so `suno-dl` uses the session your browser already holds.
Copy one cookie out of DevTools. Your account may show `__session`, `__client`,
or both.

| cookie | lives on | lifetime | what the tool does with it |
|---|---|---|---|
| `__session` | suno.com | minutes | sends it to the API as the bearer token |
| `__client` | auth.suno.com | weeks | exchanges it for a fresh bearer token on demand |

### If you only have `__session`

1. Open DevTools, then **Application**, then **Cookies**, then `https://suno.com`,
   and copy `__session`.
2. Put the JWT in an environment variable:

```bash
export SUNO_SESSION='eyJhbGciOi…'
suno-dl --check
```

`--check` prints the account and the time left on the token.

```console
$ suno-dl --check
ok: signed in as @yourhandle (Your Name)
    __session token, expires in 47s
    tip: re-copy __session when it runs out, or use __client for long runs
```

The token lives for minutes. The tool needs it to list your songs; the CDN
serves the audio itself without authentication. A fresh token therefore covers a
large library. If the listing outlives the token, the tool stops and tells you to
copy a new one.

### Use `__client` when you have it

`__client` lives for weeks, and the tool mints fresh tokens from it on its own.

```bash
export SUNO_COOKIE='__client=eyJhbGciOi…'
suno-dl --check
suno-dl --library
```

You can also paste a whole `Cookie:` header into `--cookie`, and the tool picks
`__session` and `__client` out of it.

Treat either value like a password: it opens your library. Keep it in a secret
manager or an environment variable.

## Use it

```bash
suno-dl --library                        # everything you own
suno-dl https://suno.com/song/<uuid>     # one song
suno-dl https://suno.com/s/<code>        # a share link
suno-dl https://suno.com/playlist/<id>   # a playlist
suno-dl https://suno.com/@yourhandle     # a profile
suno-dl --library --list                 # look first, download later
suno-dl --library --dry-run              # show the plan, touch nothing
```

| flag | what it does |
|---|---|
| `-f, --format` | `original` (default), `mp3`, `m4a`, `flac`, `wav`, `opus` |
| `-b, --bitrate` | kbps for mp3, m4a and opus (default 320) |
| `-j, --jobs` | parallel downloads (default 3) |
| `-n, --limit` | stop after N songs |
| `--since YYYY-MM-DD` | only songs created from that date |
| `-o, --out` | output folder (default `~/Music/Suno`) |
| `--force` | download again even when history says you have it |
| `--no-cover` | skip the artwork file |

Public songs, playlists and profiles work without a cookie. Private and unlisted
songs need a session.

### What it can and cannot fetch

Suno publishes a plain MP4 for public clips. When a clip is private or unlisted,
`video_url` comes back empty and the only entry in `media_urls` is encrypted for
Suno's player, which unlocks it with a license key. The tool reports what it found
instead of saving ciphertext.

```console
FAILED (PRIVATE): <title>. Suno publishes a plain MP4 only for public clips, and
this one is private. Its only stream is encrypted for Suno's player, so there is no
file to fetch.
```

Suno bills downloads separately from generation credits (`download_credit_packs`
in `/api/billing/info/`), so its download button spends a limited resource.
`suno-dl` does not press that button. It fetches the plain MP4 that Suno publishes
for public clips.

## Under the hood

For each song the tool:

1. asks `/api/clip/{id}` for the title, artist, duration, artwork and media URL;
2. picks the first candidate that returns real media, preferring the CDN MP4;
3. downloads it with `Range`, resuming a `.part` file after a dropped connection;
4. inspects the container: `ftyp` first, one `moov`, no truncated box, and a
   duration that matches the API;
5. writes the cover beside the audio and records the song in
   `~/.suno-dl/history.json`.

Filenames follow `Artist - Title.ext`, sanitised for Windows, macOS and Linux,
with ` (1)`, ` (2)` added on collisions.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pre-commit install --hook-type pre-commit --hook-type pre-push
pytest                      # 96 tests, no network
ruff check . && ruff format --check .
mypy suno_dl.py
```

CI runs the linters and the test suite on Python 3.10 through 3.14 across Linux,
macOS and Windows. It also builds a wheel, installs it, and runs `suno-dl
--version`. Pushing a `v*.*.*` tag builds the release, attaches it to a GitHub
Release, and publishes to PyPI through trusted publishing.

## Fair use

The tool authenticates as **you** and downloads what your account can already
play. Download your own work, or work you have the right to keep. It does not
bypass any protection and does not touch Suno's encrypted stream. Respect the
platform's terms and the artists.

## License

MIT. See [LICENSE](LICENSE).
