"""Long-running daemon: push loop + local HTTP control API.

The push loop is the only thing that talks to the Tronbyt server. The HTTP
API only mutates state in SQLite; the loop reacts on its next tick.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import library, live
from .config import Config, load
from .state import LIBRARY_MODE, LIVE_MODE, VALID_FIT_MODES, Position, Store
from .tronbyt import TronbytClient

log = logging.getLogger("matinee.daemon")


def _resolve_next_chunk(
    cfg: Config, pos: Position,
) -> tuple[library.Episode, int] | None:
    """Map current state -> (episode, chunk_index_to_push), advancing past
    episode boundaries. Returns None if the library is empty."""
    root = cfg.library.chunks_root

    if pos.show and pos.episode:
        ep = library.load_episode(root, pos.show, pos.episode)
        if ep is None:
            log.warning("Episode missing: %s/%s. Resetting.", pos.show, pos.episode)
            ep = None
    else:
        ep = None

    if ep is None:
        shows = library.list_shows(root)
        for show in shows:
            eps = library.list_episodes(root, show)
            if eps:
                return eps[0], 0
        return None

    idx = pos.chunk_index
    if idx >= ep.chunk_count:
        nxt = library.next_episode(root, ep.show, ep.episode)
        if nxt is None:
            return ep, ep.chunk_count - 1
        return nxt, 0
    return ep, idx


class Player:
    """Owns the push loop and the shared state. Dispatches between library
    mode (read pre-ingested chunks from disk) and live mode (transcode a
    URL via ffmpeg). Mode is persisted in the position row, so daemon
    restarts pick up where the user left off."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.daemon.state_db)
        self.client = TronbytClient(cfg.tronbyt)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._live_stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_push_at: float | None = None

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="matinee-push", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        ls = self._live_stop
        if ls is not None:
            ls.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.store.close()
        self.client.close()

    def kick(self) -> None:
        """Wake the loop now. Aborts a live session so the dispatcher can
        re-read state (mode may have changed)."""
        self._wake.set()
        ls = self._live_stop
        if ls is not None:
            ls.set()

    def effective_fit_mode(self) -> str:
        """Override (if set) wins over the config default."""
        return self.store.get().fit_mode or self.cfg.playback.fit_mode

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=seconds)
        self._wake.clear()

    # ---- run loop -----------------------------------------------------------

    def _run(self) -> None:
        self._configure_device_once()
        while not self._stop.is_set():
            try:
                pos = self.store.get()
                if pos.mode == LIVE_MODE:
                    self._live_loop(pos.live_url)
                else:
                    self._library_loop()
            except Exception as exc:  # noqa: BLE001
                self.last_error = repr(exc)
                log.exception("dispatch loop crashed; restarting after 2s")
                if self._stop.wait(timeout=2):
                    return

    def _configure_device_once(self) -> None:
        try:
            self.client.set_default_interval(self.cfg.playback.chunk_seconds)
        except Exception as exc:  # noqa: BLE001
            log.warning("set_default_interval failed (continuing): %s", exc)
        try:
            self.client.pin_installation()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                log.warning("pin_installation failed (continuing): %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("pin_installation failed (continuing): %s", exc)

    # ---- library mode -------------------------------------------------------

    def _library_loop(self) -> None:
        interval = max(
            1, self.cfg.playback.chunk_seconds - self.cfg.daemon.push_lead_seconds,
        )
        while not self._stop.is_set():
            pos = self.store.get()
            if pos.mode != LIBRARY_MODE:
                return  # mode changed; bounce back to dispatcher
            if not pos.paused:
                try:
                    self._library_tick(pos)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = repr(exc)
                    log.exception("library tick failed")
            self._wait(interval)

    def _library_tick(self, pos: Position) -> None:
        resolved = _resolve_next_chunk(self.cfg, pos)
        if resolved is None:
            log.info("library empty; nothing to push")
            return
        ep, idx = resolved

        if pos.show != ep.show or pos.episode != ep.episode or pos.chunk_index != idx:
            self.store.set_position(ep.show, ep.episode, idx)

        chunk = ep.chunks[idx]
        data = library.chunk_path(ep, idx).read_bytes()
        self.client.push_webp(data)
        self.last_push_at = time.time()
        self.last_error = None
        self.store.log_push(ep.show, ep.episode, idx)
        # CAS so we don't clobber a concurrent /skip or /play that came in
        # while we were doing the push.
        if not self.store.advance_from(idx, idx + 1):
            log.info("tick raced with control action; deferring to new state")
        log.info(
            "pushed %s/%s chunk %d (%.1fs, %d bytes)",
            ep.show, ep.episode, idx, chunk.duration, len(data),
        )

    # ---- live mode ----------------------------------------------------------

    def _live_loop(self, url: str | None) -> None:
        if not url:
            log.warning("live mode but no URL set; idling")
            self._wait(5)
            return
        params = live.LiveParams(
            fps=self.cfg.playback.fps,
            chunk_seconds=self.cfg.playback.chunk_seconds,
            quality=self.cfg.playback.quality,
            fit_mode=self.effective_fit_mode(),
        )
        self._live_stop = threading.Event()
        try:
            live.run_live_session(
                url, params, self._live_stop,
                on_chunk=self._push_live_chunk,
                on_error=self._set_live_error,
            )
        finally:
            self._live_stop = None
            self._wake.clear()

    def _push_live_chunk(self, blob: bytes) -> None:
        try:
            self.client.push_webp(blob)
        except Exception as exc:  # noqa: BLE001
            self.last_error = repr(exc)
            log.exception("push failed for live chunk")
            return
        self.last_push_at = time.time()
        self.last_error = None
        log.info("pushed live chunk (%d bytes)", len(blob))

    def _set_live_error(self, msg: str) -> None:
        self.last_error = msg


# ---- HTTP API ---------------------------------------------------------------


class StatusOut(BaseModel):
    mode: str
    show: str | None
    episode: str | None
    chunk_index: int
    live_url: str | None
    paused: bool
    fit_mode: str
    chunk_fit_mode: str | None
    updated_at: float
    last_push_at: float | None
    last_error: str | None


class PlayIn(BaseModel):
    show: str
    episode: str | None = None
    chunk_index: int = Field(default=0, ge=0)


class LiveIn(BaseModel):
    url: str = Field(min_length=1)


class SkipIn(BaseModel):
    n: int = 1


class FitIn(BaseModel):
    fit_mode: str = Field(min_length=1)


class EpisodeOut(BaseModel):
    episode: str
    duration: float
    chunks: int


class ShowOut(BaseModel):
    show: str
    episodes: list[EpisodeOut]


def build_app(player: Player) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        player.start()
        try:
            yield
        finally:
            player.stop()

    app = FastAPI(title="matinee", version="0.1.0", lifespan=lifespan)

    def _status_payload() -> StatusOut:
        p = player.store.get()
        chunk_fit_mode: str | None = None
        if p.mode == LIBRARY_MODE and p.show and p.episode:
            ep = library.load_episode(player.cfg.library.chunks_root, p.show, p.episode)
            if ep is not None:
                chunk_fit_mode = ep.fit_mode
        return StatusOut(
            mode=p.mode,
            show=p.show, episode=p.episode, chunk_index=p.chunk_index,
            live_url=p.live_url, paused=p.paused,
            fit_mode=player.effective_fit_mode(),
            chunk_fit_mode=chunk_fit_mode,
            updated_at=p.updated_at,
            last_push_at=player.last_push_at, last_error=player.last_error,
        )

    @app.get("/status", response_model=StatusOut)
    def status() -> StatusOut:
        return _status_payload()

    @app.get("/library", response_model=list[ShowOut])
    def get_library() -> list[ShowOut]:
        root = player.cfg.library.chunks_root
        out: list[ShowOut] = []
        for show in library.list_shows(root):
            eps = library.list_episodes(root, show)
            out.append(ShowOut(
                show=show,
                episodes=[
                    EpisodeOut(episode=e.episode, duration=e.duration, chunks=e.chunk_count)
                    for e in eps
                ],
            ))
        return out

    @app.post("/play", response_model=StatusOut)
    def play(body: PlayIn) -> StatusOut:
        root = player.cfg.library.chunks_root
        eps = library.list_episodes(root, body.show)
        if not eps:
            raise HTTPException(404, f"show not found or empty: {body.show}")
        ep_name = body.episode or eps[0].episode
        ep = library.load_episode(root, body.show, ep_name)
        if ep is None:
            raise HTTPException(404, f"episode not found: {body.show}/{ep_name}")
        if body.chunk_index >= ep.chunk_count:
            raise HTTPException(400, f"chunk_index {body.chunk_index} >= {ep.chunk_count}")
        player.store.set_position(body.show, ep_name, body.chunk_index)
        player.store.set_paused(False)
        player.kick()
        return _status_payload()

    @app.post("/live", response_model=StatusOut)
    def go_live(body: LiveIn) -> StatusOut:
        player.store.set_live(body.url)
        player.store.set_paused(False)
        player.kick()
        return _status_payload()

    @app.post("/pause", response_model=StatusOut)
    def pause() -> StatusOut:
        if player.store.get().mode == LIVE_MODE:
            raise HTTPException(400, "pause is only valid in library mode; switch with /play")
        player.store.set_paused(True)
        return _status_payload()

    @app.post("/resume", response_model=StatusOut)
    def resume() -> StatusOut:
        player.store.set_paused(False)
        player.kick()
        return _status_payload()

    @app.post("/skip", response_model=StatusOut)
    def skip(body: SkipIn) -> StatusOut:
        p = player.store.get()
        if p.mode != LIBRARY_MODE:
            raise HTTPException(400, "skip is only valid in library mode")
        if not (p.show and p.episode):
            raise HTTPException(400, "no current episode")
        ep = library.load_episode(player.cfg.library.chunks_root, p.show, p.episode)
        if ep is None:
            raise HTTPException(404, "current episode missing on disk")
        new_idx = max(0, p.chunk_index + body.n)
        if new_idx >= ep.chunk_count:
            new_idx = ep.chunk_count
        player.store.advance(new_idx)
        player.kick()
        return _status_payload()

    @app.post("/fit", response_model=StatusOut)
    def set_fit(body: FitIn) -> StatusOut:
        mode = body.fit_mode.lower()
        if mode == "default":
            player.store.set_fit_mode(None)
        elif mode in VALID_FIT_MODES:
            player.store.set_fit_mode(mode)
        else:
            raise HTTPException(
                400,
                f"unknown fit_mode {body.fit_mode!r}; "
                f"expected one of {VALID_FIT_MODES} or 'default'",
            )
        player.kick()
        return _status_payload()

    @app.post("/next_episode", response_model=StatusOut)
    def next_episode() -> StatusOut:
        p = player.store.get()
        if p.mode != LIBRARY_MODE:
            raise HTTPException(400, "next_episode is only valid in library mode")
        if not (p.show and p.episode):
            raise HTTPException(400, "no current episode")
        nxt = library.next_episode(player.cfg.library.chunks_root, p.show, p.episode)
        if nxt is None:
            raise HTTPException(404, "no next episode")
        player.store.set_position(nxt.show, nxt.episode, 0)
        player.kick()
        return _status_payload()

    return app


# ---- Entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load(args.config)
    cfg.library.chunks_root.mkdir(parents=True, exist_ok=True)
    cfg.daemon.state_db.parent.mkdir(parents=True, exist_ok=True)

    player = Player(cfg)
    app = build_app(player)
    uvicorn.run(
        app, host=cfg.daemon.host, port=cfg.daemon.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
