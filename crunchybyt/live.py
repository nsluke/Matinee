"""Live transcode: YouTube URL -> ffmpeg -> animated WebP chunks.

ffmpeg emits raw 64x32 RGB frames to stdout. We read them in groups of
fps * chunk_seconds and assemble each group into one animated WebP via
Pillow, then yield the bytes for the daemon to push.

A FrameSource abstraction lets the smoke test substitute synthetic frames
for a real ffmpeg subprocess.
"""
from __future__ import annotations

import io
import logging
import os
import select
import shutil
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger("crunchybyt.live")

WIDTH, HEIGHT = 64, 32
BYTES_PER_FRAME = WIDTH * HEIGHT * 3  # rgb24


@dataclass(frozen=True)
class LiveParams:
    fps: int
    chunk_seconds: int
    quality: int
    fit_mode: str  # "crop", "letterbox", or "stretch"
    # Kill + recover the ffmpeg session if it emits no frame data for this
    # long. ffmpeg can hang indefinitely when a YouTube googlevideo URL
    # expires (~6h) or the stream stalls and its -reconnect logic spins
    # without producing bytes. Without this, the daemon's blocking frame
    # read wedges forever. Generous enough not to trip on normal playback
    # (frames arrive ~every 1/fps s) or a brief reconnect.
    stall_timeout: float = 20.0

    @property
    def frames_per_chunk(self) -> int:
        return self.fps * self.chunk_seconds


class FrameSource(ABC):
    @abstractmethod
    def read_frame(self) -> bytes | None:
        """Return BYTES_PER_FRAME bytes, or None on end-of-stream."""

    @abstractmethod
    def close(self) -> None: ...


class FFmpegFrameSource(FrameSource):
    """Spawns ffmpeg piping raw rgb24 frames from a stream URL."""

    def __init__(self, stream_url: str, params: LiveParams) -> None:
        self._stall_timeout = params.stall_timeout
        self._proc = subprocess.Popen(
            self._cmd(stream_url, params),
            stdout=subprocess.PIPE,
            # Discard stderr instead of PIPE: we never drain it, and during a
            # reconnect-error storm an unread 64K stderr pipe fills and blocks
            # ffmpeg's writes — which silently stalls stdout too (classic
            # subprocess deadlock). DEVNULL removes that failure path entirely.
            stderr=subprocess.DEVNULL,
            bufsize=0,  # unbuffered: select() must see the real pipe state
        )

    @staticmethod
    def _vf(fit_mode: str, fps: int) -> str:
        if fit_mode == "crop":
            scale = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT}"
            )
        elif fit_mode == "letterbox":
            scale = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        elif fit_mode == "stretch":
            scale = f"scale={WIDTH}:{HEIGHT},setsar=1"
        else:
            raise ValueError(
                f"Unknown fit_mode: {fit_mode}. "
                f"Expected one of ('crop', 'letterbox', 'stretch')."
            )
        return f"fps={fps},{scale}"

    @classmethod
    def _cmd(cls, stream_url: str, p: LiveParams) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "30",
            "-i", stream_url,
            "-vf", cls._vf(p.fit_mode, p.fps),
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "-an", "-sn",
            "-",
        ]

    def read_frame(self) -> bytes | None:
        assert self._proc.stdout is not None
        return _read_exact(
            self._proc.stdout.fileno(), BYTES_PER_FRAME, self._stall_timeout,
        )

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _read_exact(fd: int, n: int, idle_timeout: float | None = None) -> bytes | None:
    """Read exactly n bytes from raw file descriptor fd.

    Returns None on EOF, or — when idle_timeout is set — if no bytes arrive
    for idle_timeout seconds (the ffmpeg stall watchdog). We read the raw fd
    via os.read (not a BufferedReader) so select() reflects the true pipe
    state rather than bytes hidden in a Python-side buffer.
    """
    buf = bytearray()
    while len(buf) < n:
        if idle_timeout is not None:
            ready, _, _ = select.select([fd], [], [], idle_timeout)
            if not ready:
                log.warning(
                    "ffmpeg emitted no frame data for %.0fs; treating as "
                    "stalled and recovering", idle_timeout,
                )
                return None
        try:
            chunk = os.read(fd, n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---- URL resolution ---------------------------------------------------------


class LiveResolveError(RuntimeError):
    pass


def _find_executable(name: str) -> str | None:
    """Locate an executable, preferring the same bin dir as our Python
    interpreter (so pip-installed entry points work under systemd's
    stripped PATH), then falling back to PATH."""
    sibling = Path(sys.executable).parent / name
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which(name)


def resolve_stream_url(yt_url: str, timeout: float = 30.0) -> str:
    """Resolve a YouTube watch/live URL to a direct stream URL via yt-dlp."""
    yt_dlp = _find_executable("yt-dlp")
    if yt_dlp is None:
        raise LiveResolveError(
            "yt-dlp not found. pip install yt-dlp (or apt install yt-dlp)."
        )
    try:
        out = subprocess.run(
            [yt_dlp, "-g", "--no-warnings", "--no-playlist", yt_url],
            check=True, capture_output=True, text=True, timeout=timeout,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise LiveResolveError(f"yt-dlp failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LiveResolveError(f"yt-dlp timed out after {timeout}s") from exc
    lines = [ln for ln in out.strip().splitlines() if ln]
    if not lines:
        raise LiveResolveError(f"yt-dlp returned no stream URL for {yt_url}")
    # yt-dlp prints video URL first, then audio. We want the first (video).
    return lines[0]


def _looks_like_playlist(url: str) -> bool:
    """Heuristic: any YouTube URL carrying a ?list= or &list= param is a
    playlist (watch?v=X&list=Y, playlist?list=Y, youtu.be/X?list=Y)."""
    return "list=" in url


def resolve_playlist(url: str, timeout: float = 60.0) -> list[str]:
    """Expand a YouTube playlist URL into a list of individual video URLs.

    Non-playlist URLs are returned as a single-element list. On any failure
    (yt-dlp missing, bad URL, timeout) we fall back to [url] so the caller
    still has something to try — a busted enumeration shouldn't take the
    whole session down.
    """
    if not _looks_like_playlist(url):
        return [url]
    yt_dlp = _find_executable("yt-dlp")
    if yt_dlp is None:
        log.warning("yt-dlp not found; treating playlist URL as a single video")
        return [url]
    try:
        out = subprocess.run(
            [yt_dlp, "--flat-playlist", "--print", "url",
             "--no-warnings", url],
            check=True, capture_output=True, text=True, timeout=timeout,
        ).stdout
    except subprocess.CalledProcessError as exc:
        log.warning(
            "playlist enumeration failed (%s); treating as single video",
            exc.stderr.strip() or exc,
        )
        return [url]
    except subprocess.TimeoutExpired:
        log.warning("playlist enumeration timed out; treating as single video")
        return [url]
    entries = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return entries or [url]


# ---- chunk assembly ---------------------------------------------------------


def _encode_chunk(frames: list[Image.Image], params: LiveParams) -> bytes:
    out = io.BytesIO()
    duration_ms = max(1, int(1000 / params.fps))
    frames[0].save(
        out, format="WEBP",
        save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0,
        quality=params.quality, method=4,
    )
    return out.getvalue()


def chunk_stream(
    source: FrameSource, params: LiveParams, stop: threading.Event,
) -> Iterator[bytes]:
    """Read frames; yield one animated-WebP blob every frames_per_chunk frames.

    Returns cleanly when the source ends or stop is set. Caller owns source.
    """
    frames: list[Image.Image] = []
    while not stop.is_set():
        raw = source.read_frame()
        if raw is None:
            break
        frames.append(Image.frombytes("RGB", (WIDTH, HEIGHT), raw))
        if len(frames) >= params.frames_per_chunk:
            yield _encode_chunk(frames, params)
            frames = []
    # Don't emit a trailing partial chunk — the next stream restart will
    # produce a full one, and partial chunks make the device flicker.


# ---- top-level orchestration -----------------------------------------------


def run_live_session(
    yt_url: str,
    params: LiveParams,
    stop: threading.Event,
    on_chunk: "callable",
    on_error: "callable | None" = None,
    source_factory: "callable | None" = None,
) -> None:
    """Run a live transcode session until `stop` is set.

    If `yt_url` is a playlist (contains `list=`), we expand it and iterate
    through entries; otherwise it's a one-entry playlist that loops on
    itself (matches the old single-URL behavior).

    Calls `on_chunk(bytes)` for every encoded chunk. If ffmpeg or yt-dlp dies,
    backs off and retries. `source_factory(stream_url, params)` is injectable
    for tests; defaults to FFmpegFrameSource.
    """
    factory = source_factory or (lambda u, p: FFmpegFrameSource(u, p))

    playlist = resolve_playlist(yt_url)
    total = len(playlist)
    if total > 1:
        log.info("playlist mode: %d entries (no progress persisted across restarts)", total)

    idx = 0
    backoff = 1.0
    while not stop.is_set():
        current_url = playlist[idx]
        try:
            stream_url = resolve_stream_url(current_url)
        except LiveResolveError as exc:
            log.warning(
                "[%d/%d] resolve failed: %s", idx + 1, total, exc,
            )
            if on_error:
                on_error(str(exc))
            if stop.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 60)
            # Don't get stuck on a bad URL in a multi-entry playlist —
            # advance past it once we've waited a chunk-sized backoff.
            if total > 1 and backoff >= params.chunk_seconds:
                log.warning("[%d/%d] giving up on this entry; advancing", idx + 1, total)
                idx = (idx + 1) % total
                backoff = 1.0
            continue

        log.info(
            "live session [%d/%d]: %s -> %s...",
            idx + 1, total, current_url, stream_url[:80],
        )
        backoff = 1.0
        source = factory(stream_url, params)
        emitted = 0
        try:
            for blob in chunk_stream(source, params, stop):
                on_chunk(blob)
                emitted += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("live chunk loop failed")
            if on_error:
                on_error(repr(exc))
        finally:
            source.close()

        if stop.is_set():
            return

        # ffmpeg exited. Always advance — whether it played through (normal
        # end of a recorded video), got cut off (HLS edge / network hiccup),
        # or produced nothing at all. With a single-entry playlist this is
        # a no-op modulo wrap (idx stays at 0); with a real playlist it
        # moves us forward.
        if emitted == 0:
            log.warning(
                "[%d/%d] produced 0 chunks; backing off %.1fs before advancing",
                idx + 1, total, backoff,
            )
            if stop.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 60)
        else:
            log.info(
                "[%d/%d] ended after %d chunks; advancing",
                idx + 1, total, emitted,
            )
        idx = (idx + 1) % total
