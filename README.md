# Matinee

Play video on a 64×32 Tronbyt LED matrix display. Point it at video files you
already have and it turns them into a tiny, endlessly-looping picture show.

Two modes:

- **Library mode** (supported) — pre-process your own video files into
  animated-WebP chunks once, daemon pushes the next chunk on a timer.
  Resume-on-reboot, skip, episode rollover.
- **Live mode** (experimental) — point at a stream URL, daemon transcodes and
  pushes chunks as they arrive. See the caveats below; it is not currently
  reliable enough to build on.

You supply the video. Matinee is a player, not a source — it ships no content
and no default playlist.

## How it works

```
  LIBRARY MODE
  ingest (one-time)                         daemon push loop
  ────────────────                          ────────────────
  episode.mkv ──ffmpeg──► rgb24 ──Pillow──► 64x32   every chunk_seconds:
  one decode pass,        frames  group     animated read next chunk file,
  scale + crop + 10 fps           fps*N     WebPs    POST .../push to Tronbyt.

  LIVE MODE
  yt-dlp                   ffmpeg                   daemon
  ──────                   ──────                   ──────
  fetch in bounded    ──►  scale + crop + 10 fps  ──►  group fps*chunk_seconds
  chunks, pipe out         rgb24 frames to stdout      frames into one WebP via
                                                       Pillow, push, repeat.
```

- One WebP per chunk (15 s of content at 10 fps by default).
- Every push overwrites the same `installationID`, so the next `/next` poll on
  the device returns the new chunk.
- The daemon pins the installation and sets the device's `intervalSec` so the
  Tronbyt only ever shows our app.
- State lives in SQLite — show/episode/chunk in library mode, current URL in
  live mode — so the daemon resumes whatever you were doing on restart.

## Prerequisites

- Tronbyt server running and reachable from the Pi (you have this).
- Device API key (Tronbyt UI → device → "Show API key").
- `ffmpeg` + `ffprobe` on whatever machine runs the ingest (Mac, Pi, etc.).
  Any build will do — ffmpeg only has to *decode*. The WebPs are encoded
  in-process by Pillow, so a build without `libwebp_anim` (Homebrew's,
  for one) is fine.
- `yt-dlp` (installed automatically as a Python dep). For YouTube sources only.
- Python 3.10+.
- **A JavaScript runtime, for any YouTube source.** YouTube needs one to solve
  its signature challenge; without it every media URL comes back `403` and you
  get no video at all. Install one of `quickjs` (smallest — Debian and Pi OS
  ship it, binary is `qjs`), `deno` >= 2.3, `bun` >= 1.2.11, or `node` >= 22.
  Debian's `nodejs` is currently 20.x, which yt-dlp rejects as unsupported.

  yt-dlp only auto-detects `deno`, so anything else must be named explicitly.
  Simplest is a system-wide `/etc/yt-dlp.conf` containing:

  ```
  --js-runtimes quickjs
  ```

## Install (on the Pi)

```bash
git clone <this repo> ~/Matinee
cd ~/Matinee
sudo ./scripts/install-pi.sh
sudoedit /etc/matinee/config.toml      # set device_id and api_key
```

Put episode files under `/srv/matinee/sources/<Show Name>/...` then:

```bash
sudo -u pi /opt/matinee/venv/bin/matinee-ingest scan
sudo systemctl enable --now matinee
```

## Dev install (Mac)

```bash
python3 -m venv venv && . venv/bin/activate
pip install -e .
cp config.example.toml config.toml      # edit paths/keys
brew install ffmpeg                     # for ingest
```

## Ingest

```bash
# Single local file
matinee-ingest one /path/to/my_show_s01e01.mkv --show "My Show" --episode "S01E01"

# Walk sources_root (uses <root>/<show>/<file>.ext for naming)
matinee-ingest scan

# From a YouTube URL (yt-dlp downloads to sources_root, then ingests)
matinee-ingest url 'https://www.youtube.com/watch?v=...' \
    --show "My Show" --episode "S01E01"

# Re-encode (overwrite)
matinee-ingest scan --force
```

Output goes under `chunks_root/<show>/<episode>/`:
```
0000.webp  0001.webp  …  NNNN.webp  manifest.json
```

### Ingesting on a faster machine

Ingest and playback are decoupled — the daemon only ever reads
`chunks_root`, so nothing requires the two to happen on the same box. On a
Raspberry Pi 3 a 1080p 10-bit HEVC source decodes far slower than realtime,
and the source files dwarf the Pi's free space, so the practical route is to
ingest on a desktop and copy the (tiny) chunks over:

```bash
# On the desktop, with chunks_root pointing somewhere local:
matinee-ingest one "/Volumes/SD/My Show/ep01.mkv" --show "My Show" --episode "E01"

# Then ship just the chunks:
rsync -a ~/Matinee/chunks/ pi@tronbyt-host.local:/srv/matinee/chunks/
```

A 24-minute episode encodes in about 30 s on an Apple-silicon laptop and
lands at roughly 5 MB of WebP, so a 25-episode season is ~120 MB on the Pi.
Name episodes so they sort in order (`E01` … `E25`); the library plays them
in sorted order and rolls over to the next one.

### `ingest url` notes

- Defaults to capping the download at 480p (`--max-height 480`). We're squashing
  to 64×32, so anything more is wasted bandwidth. Use `--no-cap` if you really
  want yt-dlp to pick the highest.
- The downloaded file lands at `sources_root/<show-slug>/<episode-slug>.<ext>`.
  Running `url` a second time skips the download (a future `scan` would see it
  too); add `--force` to redo both download and encode.
- Pass `--cleanup` to delete the downloaded source after a successful ingest if
  you only want the WebP chunks on disk.
- Use the actual video URL, not a playlist URL (we pass `--no-playlist`).

## Control (the CLI talks to the daemon over localhost)

```bash
matinee status
matinee library
matinee play "My Show"            # first episode, chunk 0
matinee play "My Show" S01E03     # specific episode
matinee skip 5                          # jump 5 chunks forward (~75 s)
matinee skip -3                         # jump back
matinee next                            # next episode
matinee pause                           # library mode only
matinee resume
matinee live https://youtube.com/...    # switch to live mode (any yt-dlp URL)
matinee fit stretch                     # stretch image to fill the display
matinee fit crop                        # zoom + crop (default for 4:3 sources)
matinee fit letterbox                   # whole frame, black bars on the sides
matinee fit default                     # clear the override, use config.toml
```

## Fit mode

The 64×32 display is 2:1 but most classic TV content is 4:3. `fit_mode` picks how to bridge
the gap:

- `crop` — scale up and center-crop. Fills the display, clips top/bottom edges.
- `letterbox` — scale down and pad with black. Whole frame visible, image only
  fills the middle ~43 pixels.
- `stretch` — scale to exactly 64×32, ignoring the source aspect ratio. Fills
  the display but squashes the picture vertically.

`matinee fit <mode>` sets a runtime override stored in the daemon's state
DB. In **live mode** the next reconnect picks up the new mode (~one chunk of
latency). In **library mode** the chunks are pre-encoded, so you also need to
re-ingest: `matinee-ingest scan --force`. `matinee status` flags this for
you — it shows both the desired fit and the fit baked into the chunk on disk.

## Live mode

> **Experimental, and currently degraded.** As of August 2026 YouTube's CDN
> serves only the *first* bounded byte range on a freshly resolved URL and
> returns `403` for subsequent ones, so a session yields roughly 45 seconds of
> video and then ends and advances. The practical result is short clips with
> gaps, not continuous playback. Sustaining a stream would require re-resolving
> (several seconds of signature-challenge work) per megabyte, which isn't
> worth it. **Library mode is the supported path** — ingest once, play from
> disk, no dependency on YouTube at playback time.

```bash
matinee live https://www.youtube.com/watch?v=<some-24/7-channel>
matinee live 'https://www.youtube.com/playlist?list=<playlist-id>'
matinee live 'https://www.youtube.com/watch?v=<vid>&list=<playlist-id>'
```

- `yt-dlp` fetches the stream in bounded chunks and pipes it to `ffmpeg`.
  (ffmpeg can't open a `googlevideo` URL directly — it has no way to issue the
  bounded range requests the CDN now insists on.)
- `yt-dlp` resolves the URL to a direct stream (handles HLS, DASH, etc.).
- `ffmpeg` pulls the stream, scales to 64×32, emits raw RGB frames.
- Daemon groups every `fps * chunk_seconds` frames into one animated WebP via
  Pillow and pushes it. So at the defaults (10 fps, 15 s) you get a fresh
  15-second loop on the device every 15 seconds.
- If `ffmpeg` or the stream dies, daemon backs off and reconnects.
- **Stall watchdog:** if ffmpeg stops emitting frames for `stall_timeout`
  seconds (default 20) — e.g. a YouTube `googlevideo` URL expired after ~6 h,
  or the stream hung — the daemon kills that ffmpeg and recovers (reconnects,
  or advances to the next playlist entry). Without this, a blocking frame read
  could wedge the daemon indefinitely and the device would loop the last chunk
  forever.
- Switch back any time with `matinee play <show>`. Live URL is remembered, so
  `matinee status` shows it even when you're in library mode.

### Playlist URLs

Point at any YouTube URL with a `list=` parameter (a real playlist URL, or a
watch URL with `&list=...` tacked on) and the daemon expands it via
`yt-dlp --flat-playlist`, then plays the entries in order. When ffmpeg exits
on an entry (video ended, stream cut off, errored — doesn't matter) we advance
to the next one. The playlist wraps when it hits the end, so it plays
continuously.

Progress doesn't persist across daemon restarts — every `matinee live <URL>`
or restart starts from entry 0. (That's a deliberate trade-off; ask if you
want it persisted in the state DB.)

Caveats:
- `pause`/`resume`/`skip`/`next` are library-only. To "stop" live, switch to
  library mode (or to a different live URL).
- Live latency = YouTube live edge (~5–30 s) + one `chunk_seconds` of buffering.
- DRM-locked sources (Crunchyroll, Netflix) won't work — `yt-dlp` can't fetch
  them.

## Tuning

In `config.toml`:

- `playback.chunk_seconds` — how long each WebP plays. Smaller = quicker
  reaction to skip/play commands but more pushes per minute. 15 is the
  Tronbyt-default device interval.
- `playback.fps` — frames inside each WebP. 10 is a good default for animation at
  64×32; raise for smoother motion (bigger files), lower if pushes time out.
- `playback.fit_mode` — default fit when no runtime override is set. One of
  `"crop"`, `"letterbox"`, `"stretch"` (see [Fit mode](#fit-mode)).
- `playback.quality` — WebP quality 0–100. 75 is a good balance.
- `daemon.push_lead_seconds` — push next chunk this many seconds before the
  device polls. Keep ≥ 1 so the new chunk is ready in time.

## File layout

```
matinee/
  config.py     load config.toml
  render.py     shared 64x32 geometry, ffmpeg filter, Pillow WebP encode
  ingest.py     one ffmpeg decode pass → WebP chunks + manifest.json
  library.py    read manifests, list shows, find next episode
  state.py      SQLite store: current position + push history + mode
  tronbyt.py    Tronbyt server HTTP client (push, pin, set interval)
  live.py       yt-dlp + ffmpeg + Pillow → live WebP chunks
  daemon.py     mode dispatcher (library/live) + FastAPI control surface
  cli.py        talks to the daemon
scripts/
  install-pi.sh, matinee.service, smoke.py
config.example.toml
pyproject.toml
```

## Notes

- The daemon assumes one device per install. For multiple devices, run multiple
  daemons with different config files.
- This is for content you own or otherwise have the right to display.
