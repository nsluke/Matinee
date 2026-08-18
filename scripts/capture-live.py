"""End-to-end test against the REAL YouTube stream.

Spins up a fake Tronbyt server, runs the daemon, sends /live for the
provided URL, and captures the first N pushed WebPs to disk so we can
visually confirm what the device would see.

Run:  python scripts/capture-live.py <youtube_url> [N_chunks]
"""
from __future__ import annotations

import base64
import logging
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("capture")

OUT_DIR = Path("/tmp/matinee-capture")
EXPECTED_KEY = "test-key"


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_fake_tronbyt(out_dir: Path) -> FastAPI:
    app = FastAPI()
    chunk_n = {"i": 0}

    @app.post("/v0/devices/{device_id}/push")
    def push(device_id: str, body: dict, authorization: str = Header(None)):
        if authorization != f"Bearer {EXPECTED_KEY}":
            raise HTTPException(401, "bad token")
        raw = base64.b64decode(body["image"])
        i = chunk_n["i"]
        chunk_n["i"] += 1
        path = out_dir / f"chunk_{i:03d}.webp"
        path.write_bytes(raw)
        log.info("[fake-tronbyt] captured %s (%d bytes)", path.name, len(raw))
        return "WebP received."

    @app.patch("/v0/devices/{device_id}")
    def patch_device(device_id: str, body: dict, authorization: str = Header(None)):
        return {"ok": True}

    @app.patch("/v0/devices/{device_id}/installations/{iname}")
    def patch_install(device_id: str, iname: str, body: dict,
                       authorization: str = Header(None)):
        return {"ok": True}

    return app


def _run_server(app: FastAPI, port: int, ready: threading.Event) -> None:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)

    def watch():
        while not server.started:
            time.sleep(0.02)
        ready.set()
    threading.Thread(target=watch, daemon=True).start()
    server.run()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <youtube_url> [n_chunks]")
    url = sys.argv[1]
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in OUT_DIR.glob("*.webp"):
        f.unlink()

    tronbyt_port = _free_port()
    daemon_port = _free_port()

    cfg_path = OUT_DIR / "config.toml"
    cfg_path.write_text(f"""
[tronbyt]
server_url = "http://127.0.0.1:{tronbyt_port}"
device_id = "dev-1"
api_key = "{EXPECTED_KEY}"
installation_id = "matinee"

[playback]
chunk_seconds = 5
fps = 10
fit_mode = "crop"
quality = 75
min_tail_seconds = 0

[library]
chunks_root = "{OUT_DIR}/chunks"
sources_root = "{OUT_DIR}/sources"

[daemon]
host = "127.0.0.1"
port = {daemon_port}
state_db = "{OUT_DIR}/state.sqlite"
push_lead_seconds = 0
""")

    fake_ready = threading.Event()
    threading.Thread(
        target=_run_server,
        args=(make_fake_tronbyt(OUT_DIR), tronbyt_port, fake_ready),
        daemon=True,
    ).start()
    fake_ready.wait(5)

    from matinee.config import load
    from matinee.daemon import Player, build_app

    cfg = load(cfg_path)
    player = Player(cfg)
    api = build_app(player)
    daemon_ready = threading.Event()
    threading.Thread(
        target=_run_server, args=(api, daemon_port, daemon_ready), daemon=True,
    ).start()
    daemon_ready.wait(5)

    base = f"http://127.0.0.1:{daemon_port}"
    with httpx.Client(base_url=base, timeout=15.0) as c:
        log.info("triggering live mode for %s", url)
        r = c.post("/live", json={"url": url})
        r.raise_for_status()
        log.info("daemon: %s", r.json())

        # 5s chunk x 3 chunks + ffmpeg/yt-dlp startup + youtube latency. Budget 90s.
        deadline = time.time() + 90
        while time.time() < deadline:
            n = len(list(OUT_DIR.glob("*.webp")))
            if n >= target:
                break
            time.sleep(1)
            s = c.get("/status").json()
            if s.get("last_error"):
                log.warning("daemon error: %s", s["last_error"])

        chunks = sorted(OUT_DIR.glob("chunk_*.webp"))
        log.info("captured %d chunks", len(chunks))
        for chunk in chunks:
            log.info("  %s (%d bytes)", chunk.name, chunk.stat().st_size)

    player.stop()

    if not chunks:
        sys.exit("FAILED: no chunks captured (check yt-dlp/ffmpeg + URL)")

    print(f"\nCaptured chunks in: {OUT_DIR}")
    print("Open one in Preview:  open /tmp/matinee-capture/chunk_000.webp")


if __name__ == "__main__":
    main()
