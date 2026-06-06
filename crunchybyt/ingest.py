"""Pre-process video files into directories of 64x32 animated-WebP chunks.

One episode -> one directory containing 0000.webp, 0001.webp, ... and a
manifest.json that lists the chunks and their durations.

Usage:
    crunchybyt-ingest <input.mkv> [--show "Dragon Ball Z"] [--episode "S01E01"]
    crunchybyt-ingest scan     # walk sources_root, ingest anything new
    crunchybyt-ingest url <youtube_url> --show "DBZ" --episode "S01E01"
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config, PlaybackCfg, load

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ChunkInfo:
    index: int
    file: str
    start: float
    duration: float


@dataclass
class Manifest:
    source: str
    show: str
    episode: str
    duration: float
    chunk_seconds: int
    fps: int
    fit_mode: str
    quality: int
    chunks: list[ChunkInfo]


def slugify(name: str) -> str:
    return SLUG_RE.sub("-", name).strip("-")


def probe_duration(path: Path) -> float:
    """Return media duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return float(out.strip())


VALID_FIT_MODES = ("crop", "letterbox", "stretch")


def _vf_for(fit_mode: str, fps: int) -> str:
    if fit_mode == "crop":
        scale_crop = (
            "scale=64:32:force_original_aspect_ratio=increase,crop=64:32"
        )
    elif fit_mode == "letterbox":
        scale_crop = (
            "scale=64:32:force_original_aspect_ratio=decrease,"
            "pad=64:32:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif fit_mode == "stretch":
        scale_crop = "scale=64:32,setsar=1"
    else:
        raise ValueError(
            f"Unknown fit_mode: {fit_mode}. Expected one of {VALID_FIT_MODES}."
        )
    return f"fps={fps},{scale_crop}"


def encode_chunk(
    src: Path, dst: Path, start: float, duration: float, pb: PlaybackCfg,
) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(src),
        "-vf", _vf_for(pb.fit_mode, pb.fps),
        "-an", "-sn",
        "-c:v", "libwebp_anim",
        "-loop", "0",
        "-quality", str(pb.quality),
        "-compression_level", "6",
        "-preset", "picture",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def ingest_one(
    src: Path,
    cfg: Config,
    show: str,
    episode: str,
    force: bool = False,
) -> Path:
    show_slug = slugify(show)
    ep_slug = slugify(episode)
    out_dir = cfg.library.chunks_root / show_slug / ep_slug
    manifest_path = out_dir / "manifest.json"

    if manifest_path.is_file() and not force:
        print(f"[skip] {show_slug}/{ep_slug} already ingested", file=sys.stderr)
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(src)
    chunk_s = cfg.playback.chunk_seconds

    chunks: list[ChunkInfo] = []
    idx = 0
    start = 0.0
    while start < duration:
        remaining = duration - start
        this_dur = min(chunk_s, remaining)
        if this_dur < cfg.playback.min_tail_seconds and idx > 0:
            break
        fname = f"{idx:04d}.webp"
        dst = out_dir / fname
        print(f"[encode] {show_slug}/{ep_slug}/{fname}  {start:.1f}+{this_dur:.1f}s",
              file=sys.stderr)
        encode_chunk(src, dst, start, this_dur, cfg.playback)
        chunks.append(ChunkInfo(idx, fname, start, this_dur))
        idx += 1
        start += chunk_s

    manifest = Manifest(
        source=str(src),
        show=show_slug,
        episode=ep_slug,
        duration=duration,
        chunk_seconds=chunk_s,
        fps=cfg.playback.fps,
        fit_mode=cfg.playback.fit_mode,
        quality=cfg.playback.quality,
        chunks=chunks,
    )
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2))
    print(f"[done]  {show_slug}/{ep_slug}: {len(chunks)} chunks",
          file=sys.stderr)
    return out_dir


def derive_show_episode(src: Path, sources_root: Path) -> tuple[str, str]:
    """Best-effort: sources_root/<show>/[season/]<ep>.ext -> (show, ep).

    Episode keeps its stem (without extension). Falls back to ('unknown',
    stem) if the file isn't under sources_root.
    """
    try:
        rel = src.relative_to(sources_root)
    except ValueError:
        return "unknown", src.stem
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0], src.stem
    return "unknown", src.stem


def _yt_dlp_download(
    url: str, out_template: str, max_height: int | None,
) -> None:
    """Invoke yt-dlp. Raises CalledProcessError if it fails."""
    cmd = ["yt-dlp", "-o", out_template, "--no-playlist", "--no-warnings"]
    if max_height:
        # Prefer pre-merged formats at this height; fall back to bestvideo+audio
        # at the height; then any best.
        cmd += [
            "-f",
            f"best[height<={max_height}]/bestvideo[height<={max_height}]+bestaudio/best",
        ]
    cmd.append(url)
    subprocess.run(cmd, check=True)


def ingest_url(
    url: str,
    cfg: Config,
    show: str,
    episode: str,
    force: bool = False,
    max_height: int | None = 480,
    cleanup: bool = False,
    downloader=_yt_dlp_download,
) -> Path:
    """Download a video URL via yt-dlp and ingest the result.

    The download lands in `sources_root/<show_slug>/<episode_slug>.<ext>`,
    so a subsequent `crunchybyt-ingest scan` would also see it. If the
    source already exists (any video extension) we skip the download unless
    `force` is set. With `cleanup=True` the downloaded source file is
    removed after a successful ingest.

    `downloader(url, out_template, max_height)` is injectable for tests.
    """
    if shutil.which("yt-dlp") is None and downloader is _yt_dlp_download:
        sys.exit("yt-dlp not on PATH. pip install yt-dlp (or apt install yt-dlp).")

    show_slug = slugify(show)
    ep_slug = slugify(episode)
    dl_dir = cfg.library.sources_root / show_slug
    dl_dir.mkdir(parents=True, exist_ok=True)

    existing = [
        p for p in dl_dir.glob(f"{ep_slug}.*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    if existing and not force:
        src = existing[0]
        print(f"[skip-dl] {src.name} already present", file=sys.stderr)
    else:
        # Wipe any old downloads first so we don't end up with two source files.
        for old in existing:
            old.unlink()
        template = str(dl_dir / f"{ep_slug}.%(ext)s")
        print(f"[dl]    {url}\n        -> {template}", file=sys.stderr)
        try:
            downloader(url, template, max_height)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"yt-dlp failed (exit {exc.returncode})")
        candidates = [
            p for p in dl_dir.glob(f"{ep_slug}.*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        ]
        if not candidates:
            sys.exit(f"yt-dlp finished but no video file at {template}")
        src = candidates[0]

    out_dir = ingest_one(src, cfg, show, episode, force=force)
    if cleanup:
        try:
            src.unlink()
            print(f"[cleanup] removed source {src.name}", file=sys.stderr)
        except OSError as exc:
            print(f"[warn] couldn't remove source: {exc}", file=sys.stderr)
    return out_dir


def scan(cfg: Config, force: bool = False) -> None:
    root = cfg.library.sources_root
    if not root.is_dir():
        sys.exit(f"sources_root not found: {root}")
    found = 0
    for src in sorted(root.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in VIDEO_EXTS:
            continue
        show, episode = derive_show_episode(src, root)
        ingest_one(src, cfg, show, episode, force=force)
        found += 1
    if found == 0:
        print("[scan] no video files found", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="Path to config.toml")
    p.add_argument("--force", action="store_true", help="Re-encode even if manifest exists")
    sub = p.add_subparsers(dest="cmd")

    one = sub.add_parser("one", help="Ingest a single file (default if a path is passed)")
    one.add_argument("input", type=Path)
    one.add_argument("--show", required=True)
    one.add_argument("--episode", required=True)

    sub.add_parser("scan", help="Walk sources_root and ingest everything new")

    url_p = sub.add_parser(
        "url", help="Download a video URL via yt-dlp and ingest it",
    )
    url_p.add_argument("url")
    url_p.add_argument("--show", required=True)
    url_p.add_argument("--episode", required=True)
    url_p.add_argument(
        "--max-height", type=int, default=480, dest="max_height",
        help="Cap source resolution to speed up the download (default 480p).",
    )
    url_p.add_argument(
        "--no-cap", action="store_true",
        help="Don't cap resolution (download whatever yt-dlp picks).",
    )
    url_p.add_argument(
        "--cleanup", action="store_true",
        help="Delete the downloaded source file after a successful ingest.",
    )

    # Allow `crunchybyt-ingest path.mkv --show X --episode Y` without the
    # `one` subcommand keyword.
    args, rest = p.parse_known_args(argv)
    if args.cmd is None and rest:
        args = p.parse_args(["one", *rest, *(["--config", str(args.config)] if args.config else []), *(["--force"] if args.force else [])])

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe not on PATH. Install ffmpeg first.")

    cfg = load(args.config)
    cfg.library.chunks_root.mkdir(parents=True, exist_ok=True)
    cfg.library.sources_root.mkdir(parents=True, exist_ok=True)

    if args.cmd == "scan":
        scan(cfg, force=args.force)
    elif args.cmd == "one":
        ingest_one(args.input, cfg, args.show, args.episode, force=args.force)
    elif args.cmd == "url":
        ingest_url(
            args.url, cfg, args.show, args.episode,
            force=args.force,
            max_height=None if args.no_cap else args.max_height,
            cleanup=args.cleanup,
        )
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
