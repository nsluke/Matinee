"""Read pre-ingested episodes from chunks_root.

Layout: <chunks_root>/<show>/<episode>/manifest.json + NNNN.webp
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_DIGITS = re.compile(r"(\d+)")


def _natural_key(name: str) -> tuple:
    """Sort key that orders embedded numbers numerically.

    Plain string sort puts "Episode 10" before "Episode 2", which silently
    plays a show out of order — `scan` names episodes after their source
    filename, so unpadded numbering is the common case rather than an edge
    one. Zero-padded names are unaffected.
    """
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _DIGITS.split(name)
    )


@dataclass(frozen=True)
class Chunk:
    index: int
    file: str
    start: float
    duration: float


@dataclass(frozen=True)
class Episode:
    show: str
    episode: str
    dir: Path
    duration: float
    fit_mode: str | None
    chunks: tuple[Chunk, ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def _read_manifest(manifest_path: Path) -> Episode | None:
    try:
        raw = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    chunks = tuple(
        Chunk(c["index"], c["file"], c["start"], c["duration"])
        for c in raw.get("chunks", [])
    )
    if not chunks:
        return None
    return Episode(
        show=raw["show"],
        episode=raw["episode"],
        dir=manifest_path.parent,
        duration=raw["duration"],
        fit_mode=raw.get("fit_mode"),
        chunks=chunks,
    )


def load_episode(chunks_root: Path, show: str, episode: str) -> Episode | None:
    return _read_manifest(chunks_root / show / episode / "manifest.json")


def list_shows(chunks_root: Path) -> list[str]:
    if not chunks_root.is_dir():
        return []
    return sorted(
        (p.name for p in chunks_root.iterdir() if p.is_dir()),
        key=_natural_key,
    )


def list_episodes(chunks_root: Path, show: str) -> list[Episode]:
    show_dir = chunks_root / show
    if not show_dir.is_dir():
        return []
    episodes = []
    for ep_dir in sorted(show_dir.iterdir(), key=lambda p: _natural_key(p.name)):
        if not ep_dir.is_dir():
            continue
        ep = _read_manifest(ep_dir / "manifest.json")
        if ep is not None:
            episodes.append(ep)
    return episodes


def next_episode(chunks_root: Path, show: str, episode: str) -> Episode | None:
    """Return the episode that sorts immediately after `episode` in `show`.

    Wraps to the first episode if at the end. Returns None if the show
    has no episodes.
    """
    eps = list_episodes(chunks_root, show)
    if not eps:
        return None
    for i, ep in enumerate(eps):
        if ep.episode == episode:
            return eps[(i + 1) % len(eps)]
    return eps[0]


def chunk_path(ep: Episode, index: int) -> Path:
    return ep.dir / ep.chunks[index].file
