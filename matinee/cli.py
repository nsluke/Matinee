"""matinee CLI: thin wrapper over the daemon's HTTP API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import load


def _api(cfg_path: Path | None) -> httpx.Client:
    cfg = load(cfg_path)
    base = f"http://{cfg.daemon.host}:{cfg.daemon.port}"
    return httpx.Client(base_url=base, timeout=5.0)


def _fmt_status(s: dict[str, Any]) -> str:
    mode = s.get("mode", "library")
    if mode == "live":
        where = f"LIVE: {s.get('live_url')}"
    elif s.get("show"):
        where = f"{s['show']} / {s['episode']} @ chunk {s['chunk_index']}"
    else:
        where = "—"
    fit = s.get("fit_mode")
    chunk_fit = s.get("chunk_fit_mode")
    if fit and mode == "library" and chunk_fit and chunk_fit != fit:
        fit_line = (
            f"fit:    {fit}  (chunks on disk: {chunk_fit} — "
            f"run `matinee-ingest scan --force` to re-render)"
        )
    elif fit:
        fit_line = f"fit:    {fit}"
    else:
        fit_line = None
    bits = [
        f"mode:   {mode}",
        f"now:    {where}",
        f"state:  {'paused' if s.get('paused') else 'playing'}",
    ]
    if fit_line:
        bits.append(fit_line)
    if s.get("last_push_at"):
        bits.append(f"last push: {s['last_push_at']:.0f} (unix)")
    if s.get("last_error"):
        bits.append(f"error:  {s['last_error']}")
    return "\n".join(bits)


def cmd_status(api: httpx.Client, _args: argparse.Namespace) -> None:
    r = api.get("/status")
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_play(api: httpx.Client, args: argparse.Namespace) -> None:
    body = {"show": args.show}
    if args.episode:
        body["episode"] = args.episode
    if args.chunk_index is not None:
        body["chunk_index"] = args.chunk_index
    r = api.post("/play", json=body)
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_live(api: httpx.Client, args: argparse.Namespace) -> None:
    r = api.post("/live", json={"url": args.url})
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_pause(api: httpx.Client, _args: argparse.Namespace) -> None:
    r = api.post("/pause")
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_resume(api: httpx.Client, _args: argparse.Namespace) -> None:
    r = api.post("/resume")
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_skip(api: httpx.Client, args: argparse.Namespace) -> None:
    r = api.post("/skip", json={"n": args.n})
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_next(api: httpx.Client, _args: argparse.Namespace) -> None:
    r = api.post("/next_episode")
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_fit(api: httpx.Client, args: argparse.Namespace) -> None:
    r = api.post("/fit", json={"fit_mode": args.mode})
    r.raise_for_status()
    print(_fmt_status(r.json()))


def cmd_library(api: httpx.Client, args: argparse.Namespace) -> None:
    r = api.get("/library")
    r.raise_for_status()
    shows = r.json()
    if args.json:
        print(json.dumps(shows, indent=2))
        return
    if not shows:
        print("(library empty — run matinee-ingest first)")
        return
    for show in shows:
        print(f"{show['show']}  ({len(show['episodes'])} episodes)")
        for ep in show["episodes"]:
            mins = ep["duration"] / 60
            print(f"  - {ep['episode']:<20} {mins:5.1f} min  ({ep['chunks']} chunks)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="matinee")
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show current playback state")

    play = sub.add_parser("play", help="Switch to a show/episode and start playing")
    play.add_argument("show")
    play.add_argument("episode", nargs="?", default=None)
    play.add_argument("--chunk-index", type=int, dest="chunk_index", default=None)

    live = sub.add_parser("live", help="Switch to live mode with a YouTube URL")
    live.add_argument("url", help="YouTube watch/live URL (e.g. https://youtube.com/watch?v=...)")

    sub.add_parser("pause")
    sub.add_parser("resume")

    skip = sub.add_parser("skip", help="Jump N chunks forward (or back if negative)")
    skip.add_argument("n", type=int, nargs="?", default=1)

    sub.add_parser("next", help="Jump to the next episode")

    fit = sub.add_parser(
        "fit",
        help="Set the fit mode used to map source video onto the 64x32 display",
    )
    fit.add_argument(
        "mode",
        choices=("crop", "letterbox", "stretch", "default"),
        help="crop = zoom to fill (clips edges), letterbox = whole frame with "
             "black bars, stretch = fill display ignoring aspect ratio, "
             "default = clear override and use config",
    )

    lib = sub.add_parser("library", help="List ingested shows and episodes")
    lib.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    handlers = {
        "status": cmd_status, "play": cmd_play, "live": cmd_live,
        "pause": cmd_pause, "resume": cmd_resume, "skip": cmd_skip,
        "next": cmd_next, "library": cmd_library, "fit": cmd_fit,
    }

    try:
        with _api(args.config) as api:
            handlers[args.cmd](api, args)
    except httpx.ConnectError:
        sys.exit("Could not reach the matinee daemon. Is it running?")
    except httpx.HTTPStatusError as exc:
        sys.exit(f"daemon error: {exc.response.status_code} {exc.response.text}")


if __name__ == "__main__":
    main()
