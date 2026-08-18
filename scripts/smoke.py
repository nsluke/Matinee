"""End-to-end smoke test (no real device, no real video):

1. Spin up a fake Tronbyt server on 127.0.0.1 that records pushes.
2. Build a temp library: 1 show / 2 episodes / 3 chunks each.
3. Start the matinee daemon pointing at the fake server.
4. Drive the daemon via its HTTP API and assert the fake server saw the
   right sequence of pushes.
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import logging
import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


# ---- fake Tronbyt --------------------------------------------------------

PUSHES: list[dict] = []
DEVICE_PATCHES: list[dict] = []
INSTALL_PATCHES: list[dict] = []
EXPECTED_KEY = "test-key"


def make_fake_tronbyt() -> FastAPI:
    app = FastAPI()

    def _auth(authorization: str | None) -> None:
        if authorization != f"Bearer {EXPECTED_KEY}":
            raise HTTPException(401, "bad token")

    @app.post("/v0/devices/{device_id}/push")
    def push(device_id: str, body: dict, authorization: str = Header(None)):
        _auth(authorization)
        PUSHES.append({
            "device_id": device_id,
            "installationID": body.get("installationID"),
            "bytes": len(base64.b64decode(body["image"])),
            "background": body.get("background", False),
        })
        return "WebP received."

    @app.patch("/v0/devices/{device_id}")
    def patch_device(device_id: str, body: dict, authorization: str = Header(None)):
        _auth(authorization)
        DEVICE_PATCHES.append({"device_id": device_id, **body})
        return {"ok": True}

    @app.patch("/v0/devices/{device_id}/installations/{iname}")
    def patch_install(
        device_id: str, iname: str, body: dict, authorization: str = Header(None),
    ):
        _auth(authorization)
        INSTALL_PATCHES.append({"device_id": device_id, "iname": iname, **body})
        return {"ok": True}

    return app


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(app: FastAPI, port: int, ready: threading.Event) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def watch():
        while not server.started:
            time.sleep(0.02)
        ready.set()
    threading.Thread(target=watch, daemon=True).start()
    server.run()


# ---- helpers -------------------------------------------------------------

def build_fake_library(root: Path) -> None:
    """1 show, 2 episodes (sorted ep01 < ep02), 3 chunks each."""
    for ep in ("ep01", "ep02"):
        ep_dir = root / "TestShow" / ep
        ep_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for i in range(3):
            f = f"{i:04d}.webp"
            # fake "webp" payload — daemon doesn't care about the bytes
            (ep_dir / f).write_bytes(f"fake-webp-{ep}-{i}".encode())
            chunks.append({"index": i, "file": f, "start": i * 15.0, "duration": 15.0})
        (ep_dir / "manifest.json").write_text(json.dumps({
            "source": f"/fake/{ep}.mkv",
            "show": "TestShow", "episode": ep,
            "duration": 45.0, "chunk_seconds": 15, "fps": 10,
            "fit_mode": "crop", "quality": 75, "chunks": chunks,
        }))


def write_config(path: Path, tronbyt_port: int, daemon_port: int, root: Path) -> None:
    path.write_text(f"""
[tronbyt]
server_url = "http://127.0.0.1:{tronbyt_port}"
device_id = "dev-1"
api_key = "{EXPECTED_KEY}"
installation_id = "matinee"

[playback]
chunk_seconds = 1
fps = 10
fit_mode = "crop"
quality = 75
min_tail_seconds = 0

[library]
chunks_root = "{root}"
sources_root = "{root}/_sources"

[daemon]
host = "127.0.0.1"
port = {daemon_port}
state_db = "{root}/state.sqlite"
push_lead_seconds = 0
""")


# ---- main ----------------------------------------------------------------

class StubFrameSource:
    """Yields synthetic colored frames, then signals end-of-stream."""

    def __init__(self, total_frames: int) -> None:
        self._remaining = total_frames

    def read_frame(self) -> bytes | None:
        if self._remaining <= 0:
            return None
        self._remaining -= 1
        # Alternating red/blue every frame so it'd be obvious on a device.
        color = b"\xff\x00\x00" if self._remaining % 2 else b"\x00\x00\xff"
        return color * (64 * 32)

    def close(self) -> None:
        pass


def install_live_stubs(frames_per_session: int) -> None:
    """Monkeypatch live.py to avoid yt-dlp and ffmpeg."""
    from matinee import live as live_mod

    live_mod.resolve_stream_url = lambda url, timeout=30.0: f"stream:{url}"

    def factory(stream_url, params):
        return StubFrameSource(frames_per_session)

    # Patch the default factory used inside run_live_session.
    original = live_mod.run_live_session

    def patched(yt_url, params, stop, on_chunk, on_error=None, source_factory=None):
        return original(
            yt_url, params, stop, on_chunk, on_error=on_error,
            source_factory=source_factory or factory,
        )
    live_mod.run_live_session = patched


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build_fake_library(root)

        tronbyt_port = _free_port()
        daemon_port = _free_port()
        cfg_path = root / "config.toml"
        write_config(cfg_path, tronbyt_port, daemon_port, root)

        # start fake Tronbyt
        fake_ready = threading.Event()
        threading.Thread(
            target=_run_server,
            args=(make_fake_tronbyt(), tronbyt_port, fake_ready),
            daemon=True,
        ).start()
        fake_ready.wait(5)

        # start the daemon in this process (background thread)
        from matinee.daemon import Player, build_app
        from matinee.config import load

        cfg = load(cfg_path)
        # Install live-mode stubs before constructing the Player so the
        # daemon thread sees the patched module-level functions.
        install_live_stubs(frames_per_session=cfg.playback.fps * cfg.playback.chunk_seconds * 3)

        player = Player(cfg)
        api = build_app(player)

        daemon_ready = threading.Event()
        threading.Thread(
            target=_run_server, args=(api, daemon_port, daemon_ready), daemon=True,
        ).start()
        daemon_ready.wait(5)

        base = f"http://127.0.0.1:{daemon_port}"
        with httpx.Client(base_url=base, timeout=5.0) as c:
            # Wait for the first auto-pushed chunk (library has only one show,
            # the daemon picks it up immediately).
            deadline = time.time() + 5
            while not PUSHES and time.time() < deadline:
                time.sleep(0.1)
            assert PUSHES, "daemon never pushed"
            print(f"first auto-push:    {PUSHES[0]}")

            # Status reflects current chunk
            s = c.get("/status").json()
            print(f"status after auto:  {s}")
            assert s["show"] == "TestShow"
            assert s["paused"] is False

            # Library lists what we built
            lib = c.get("/library").json()
            print(f"library:            {lib}")
            assert len(lib) == 1 and len(lib[0]["episodes"]) == 2

            # Switch episode explicitly to ep02
            r = c.post("/play", json={"show": "TestShow", "episode": "ep02"}).json()
            print(f"play ep02:          {r}")
            assert r["episode"] == "ep02" and r["chunk_index"] == 0

            # Wait for two more pushes (ep02 chunks 0 and 1)
            before = len(PUSHES)
            deadline = time.time() + 5
            while len(PUSHES) < before + 2 and time.time() < deadline:
                time.sleep(0.1)
            assert len(PUSHES) >= before + 2, f"expected >= {before + 2}, got {len(PUSHES)}"
            print(f"pushes after play:  {len(PUSHES)}")

            # Pause and confirm no more pushes
            c.post("/pause")
            paused_at = len(PUSHES)
            time.sleep(2)
            assert len(PUSHES) == paused_at, "pushed while paused"
            print(f"pushes after pause: {len(PUSHES)} (unchanged)")

            # Resume
            c.post("/resume")
            time.sleep(1.5)
            assert len(PUSHES) > paused_at, "no push after resume"
            print(f"pushes after resume: {len(PUSHES)}")

            # Skip past end of ep02 -> should roll into ep01 (wraps in sort order)
            c.post("/play", json={"show": "TestShow", "episode": "ep02"})
            c.post("/skip", json={"n": 5})  # ep02 has 3 chunks; +5 forces rollover
            time.sleep(1.5)
            s = c.get("/status").json()
            print(f"after rollover:     {s}")
            assert s["episode"] == "ep01", f"expected ep01 after wrap, got {s['episode']}"

            # Confirm device patches
            print(f"device patches:     {DEVICE_PATCHES}")
            print(f"install patches:    {INSTALL_PATCHES}")
            assert any(p.get("intervalSec") == 1 for p in DEVICE_PATCHES)

            # ---- live mode -------------------------------------------------
            print("\n--- live mode ---")
            lib_pushes = len(PUSHES)
            r = c.post("/live", json={"url": "https://youtube.com/watch?v=FAKE"}).json()
            print(f"go live:            {r}")
            assert r["mode"] == "live"
            assert r["live_url"] == "https://youtube.com/watch?v=FAKE"

            # Wait for live chunks to land. Our stub emits 3 chunks worth of
            # frames per session (fps*chunk_seconds*3 = 30 frames at fps=10,
            # chunk_seconds=1), so we should see >= 3 pushes from one session,
            # plus the session will restart and produce more.
            deadline = time.time() + 8
            while len(PUSHES) < lib_pushes + 3 and time.time() < deadline:
                time.sleep(0.1)
            live_pushes = len(PUSHES) - lib_pushes
            print(f"live pushes:        {live_pushes}")
            assert live_pushes >= 3, f"expected >= 3 live pushes, got {live_pushes}"

            # The pushed bytes should be real animated-WebP, much larger than
            # the 16-byte library stub bytes.
            live_sizes = [p["bytes"] for p in PUSHES[lib_pushes:]]
            print(f"live chunk sizes:   {live_sizes[:5]}...")
            assert all(s > 100 for s in live_sizes), "live chunks suspiciously small"

            # Switch back to library and confirm the live loop exits.
            before_switch = len(PUSHES)
            c.post("/play", json={"show": "TestShow", "episode": "ep01"})
            time.sleep(1.5)
            s = c.get("/status").json()
            print(f"back to library:    {s}")
            assert s["mode"] == "library"
            assert s["show"] == "TestShow"
            assert len(PUSHES) > before_switch, "no library push after mode switch"

            # ---- fit mode -------------------------------------------------
            print("\n--- fit mode ---")
            assert s["fit_mode"] == "crop", f"expected crop default, got {s['fit_mode']}"
            assert s["chunk_fit_mode"] == "crop"

            r = c.post("/fit", json={"fit_mode": "stretch"}).json()
            print(f"set stretch:        fit={r['fit_mode']} chunk={r['chunk_fit_mode']}")
            assert r["fit_mode"] == "stretch"
            assert r["chunk_fit_mode"] == "crop", \
                "library chunks should still report their baked-in mode"

            # Unknown mode rejected
            bad = c.post("/fit", json={"fit_mode": "warp"})
            assert bad.status_code == 400, f"expected 400, got {bad.status_code}"

            # Clear the override
            r = c.post("/fit", json={"fit_mode": "default"}).json()
            print(f"clear override:     fit={r['fit_mode']}")
            assert r["fit_mode"] == "crop", "should fall back to config default"

        player.stop()
        print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
