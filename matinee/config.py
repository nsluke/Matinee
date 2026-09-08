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


# How chunks reach the device.
#   "push" — we render and POST each chunk to the Tronbyt server (needs
#            [tronbyt] credentials, and pins an installation).
#   "pull" — we serve chunks over HTTP and a Pixlet app on the Tronbyt server
#            fetches them. No credentials, no device id, no pinning.
PUSH_TRANSPORT = "push"
PULL_TRANSPORT = "pull"
VALID_TRANSPORTS = (PUSH_TRANSPORT, PULL_TRANSPORT)


@dataclass(frozen=True)
class DaemonCfg:
    host: str
    port: int
    state_db: Path
    push_lead_seconds: int
    transport: str = PUSH_TRANSPORT


@dataclass(frozen=True)
class Config:
    # None in pull mode: nothing talks to the Tronbyt server, so the whole
    # [tronbyt] section (device id, API key) is unnecessary.
    tronbyt: TronbytCfg | None
    playback: PlaybackCfg
    library: LibraryCfg
    daemon: DaemonCfg
    source_path: Path


def _candidate_paths() -> list[Path]:
    env = os.environ.get("MATINEE_CONFIG")
    paths: list[Path] = []
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "config.toml")
    paths.append(Path.home() / ".config" / "matinee" / "config.toml")
    paths.append(Path("/etc/matinee/config.toml"))
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
                f"No matinee config found. Set MATINEE_CONFIG or place "
                f"config.toml at one of: {tried}"
            )

    with path.open("rb") as f:
        raw = tomllib.load(f)

    daemon_raw = raw.get("daemon", {})
    transport = str(daemon_raw.get("transport", PUSH_TRANSPORT)).lower()
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"{path}: [daemon] transport = {transport!r} is not one of "
            f"{VALID_TRANSPORTS}"
        )

    tronbyt_raw = raw.get("tronbyt")
    if transport == PUSH_TRANSPORT and tronbyt_raw is None:
        raise ValueError(
            f"{path}: [tronbyt] is required when [daemon] transport = \"push\" "
            f"(it is what we push to). Set transport = \"pull\" to serve chunks "
            f"over HTTP instead and drop the section."
        )

    return Config(
        tronbyt=TronbytCfg(**tronbyt_raw) if tronbyt_raw is not None else None,
        playback=PlaybackCfg(**raw["playback"]),
        library=LibraryCfg(
            chunks_root=Path(raw["library"]["chunks_root"]).expanduser(),
            sources_root=Path(raw["library"]["sources_root"]).expanduser(),
        ),
        daemon=DaemonCfg(
            host=daemon_raw["host"],
            port=int(daemon_raw["port"]),
            state_db=Path(daemon_raw["state_db"]).expanduser(),
            push_lead_seconds=int(daemon_raw["push_lead_seconds"]),
            transport=transport,
        ),
        source_path=path,
    )
