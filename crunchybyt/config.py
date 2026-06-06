from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass(frozen=True)
class TronbytCfg:
    server_url: str
    device_id: str
    api_key: str
    installation_id: str


@dataclass(frozen=True)
class PlaybackCfg:
    chunk_seconds: int
    fps: int
    fit_mode: str
    quality: int
    min_tail_seconds: int


@dataclass(frozen=True)
class LibraryCfg:
    chunks_root: Path
    sources_root: Path


@dataclass(frozen=True)
class DaemonCfg:
    host: str
    port: int
    state_db: Path
    push_lead_seconds: int


@dataclass(frozen=True)
class Config:
    tronbyt: TronbytCfg
    playback: PlaybackCfg
    library: LibraryCfg
    daemon: DaemonCfg
    source_path: Path


def _candidate_paths() -> list[Path]:
    env = os.environ.get("CRUNCHYBYT_CONFIG")
    paths: list[Path] = []
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "config.toml")
    paths.append(Path.home() / ".config" / "crunchybyt" / "config.toml")
    paths.append(Path("/etc/crunchybyt/config.toml"))
    return paths


def load(path: Path | None = None) -> Config:
    if path is None:
        for candidate in _candidate_paths():
            if candidate.is_file():
                path = candidate
                break
        else:
            tried = ", ".join(str(p) for p in _candidate_paths())
            raise FileNotFoundError(
                f"No crunchybyt config found. Set CRUNCHYBYT_CONFIG or place "
                f"config.toml at one of: {tried}"
            )

    with path.open("rb") as f:
        raw = tomllib.load(f)

    return Config(
        tronbyt=TronbytCfg(**raw["tronbyt"]),
        playback=PlaybackCfg(**raw["playback"]),
        library=LibraryCfg(
            chunks_root=Path(raw["library"]["chunks_root"]).expanduser(),
            sources_root=Path(raw["library"]["sources_root"]).expanduser(),
        ),
        daemon=DaemonCfg(
            host=raw["daemon"]["host"],
            port=int(raw["daemon"]["port"]),
            state_db=Path(raw["daemon"]["state_db"]).expanduser(),
            push_lead_seconds=int(raw["daemon"]["push_lead_seconds"]),
        ),
        source_path=path,
    )
