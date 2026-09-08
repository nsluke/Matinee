"""End-to-end smoke test (no real device, no real video):

1. Spin up a fake Tronbyt server on 127.0.0.1 that records pushes.
2. Build a temp library: 1 show / 2 episodes / 3 chunks each.
3. Start the matinee daemon pointing at the fake server.
4. Drive the daemon via its HTTP API and assert the fake server saw the
   right sequence of pushes.
"""
from __future__ import annotations

import base64
import io
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

# Installations the device already has before we ever push, mimicking a real
# Tronbyt with a few apps on it.
PRESET_INSTALLS = {"1": "clock", "2": "weather"}
# Installations the server has created for pushes, keyed by the push label.
# The real server assigns its OWN id here and never exposes the label again,
# which is why the daemon has to discover the id rather than assume it.
PUSH_INSTALLS: dict[str, str] = {}
INSTALL_PINNED: set[str] = set()


def _all_install_ids() -> dict[str, str]:
    return {**PRESET_INSTALLS, **{v: "pushed" for v in PUSH_INSTALLS.values()}}


def make_fake_tronbyt() -> FastAPI:
    app = FastAPI()

    def _auth(authorization: str | None) -> None:
        if authorization != f"Bearer {EXPECTED_KEY}":
            raise HTTPException(401, "bad token")

    @app.post("/v0/devices/{device_id}/push")
    def push(device_id: str, body: dict, authorization: str = Header(None)):
        _auth(authorization)
        label = body.get("installationID")
        if label not in PUSH_INSTALLS:
            # A push under a new label creates a new installation with a
            # server-assigned id, exactly as the real server does.
            PUSH_INSTALLS[label] = str(len(_all_install_ids()) + 1)
        PUSHES.append({
            "device_id": device_id,
            "installationID": label,
            "bytes": len(base64.b64decode(body["image"])),
            "background": body.get("background", False),
        })
        return "WebP received."

    @app.get("/v0/devices/{device_id}/installations")
    def list_installs(device_id: str, authorization: str = Header(None)):
        _auth(authorization)
        return {"installations": [
            {"id": iid, "appID": app_id, "pinned": iid in INSTALL_PINNED}
            for iid, app_id in _all_install_ids().items()
        ]}

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
        if iname not in _all_install_ids():
            raise HTTPException(404, "App not found")
        INSTALL_PATCHES.append({"device_id": device_id, "iname": iname, **body})
        if body.get("pinned"):
            INSTALL_PINNED.add(iname)
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


def check_settings_roundtrip() -> None:
    """The settings table backs the persisted pin id, which is only read back
    on a *later* daemon start — so nothing else in this test would notice it
    being unreadable."""
    import tempfile

    from matinee.state import PIN_INSTALLATION_ID, Store

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "settings.sqlite")
        try:
            assert st.get_setting(PIN_INSTALLATION_ID) is None, "unset should be None"
            st.set_setting(PIN_INSTALLATION_ID, "9")
            assert st.get_setting(PIN_INSTALLATION_ID) == "9", "read-back failed"
            st.set_setting(PIN_INSTALLATION_ID, "11")
            assert st.get_setting(PIN_INSTALLATION_ID) == "11", "upsert failed"
        finally:
            st.close()
    print("settings round-trip: ok")


def check_uniform_chunk_encoding() -> None:
    """Chunks served to Pixlet must have one delay for every frame.

    Pixlet re-encodes an animation with a single delay rather than honouring
    per-frame timing, so a chunk whose identical frames were merged (which is
    what libwebp does by default) plays short by exactly the time folded into
    those merged frames. Asserting on the encoded bytes is the only way to
    catch a regression here — the animation still looks valid either way.
    """
    from PIL import Image

    from matinee.render import (
        anmf_durations,
        encode_animation,
        encode_uniform_animation,
        expand_to_uniform,
    )

    fps, quality = 10, 75
    # A long static run followed by motion — the shape that triggers merging.
    frames = []
    for i in range(150):
        im = Image.new("RGB", (64, 32), (10, 10, 10))
        if i > 40:
            px = im.load()
            for x in range(max(0, i % 64 - 3), min(64, i % 64 + 3)):
                for y in range(10, 22):
                    px[x, y] = (200, 80, 40)
        frames.append(im)

    merged = encode_animation(frames, fps, quality)
    merged_durs = anmf_durations(merged)
    assert len(set(merged_durs)) > 1, (
        "test is not exercising anything: these frames were expected to merge"
    )

    uniform = encode_uniform_animation(frames, fps, quality)
    durs = anmf_durations(uniform)
    assert len(durs) == 150, f"lost frames: {len(durs)} != 150"
    assert set(durs) == {100}, f"delays not uniform: {sorted(set(durs))[:5]}"
    assert sum(durs) == 15000, f"wrong total duration: {sum(durs)}"
    assert Image.open(io.BytesIO(uniform)).n_frames == 150, "not decodable"

    # And a merged chunk already on disk can be re-timed without re-ingesting.
    restored = anmf_durations(expand_to_uniform(merged, fps, quality))
    assert set(restored) == {100}, f"expand left mixed delays: {set(restored)}"
    assert sum(restored) == sum(merged_durs), (
        f"expand changed duration: {sum(restored)} != {sum(merged_durs)}"
    )
    print(
        f"uniform chunk encoding: ok "
        f"(merged {len(merged_durs)} frames -> uniform {len(durs)}, "
        f"{sum(durs)}ms preserved)"
    )


def main() -> None:
    import tempfile

    check_settings_roundtrip()
    check_uniform_chunk_encoding()

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

            # Pinning must target the id the server assigned to our pushed
            # installation, not the label we push under. Addressing it by the
            # label 404s on a real server, which used to be swallowed and left
            # the device rotating its other apps.
            our_id = PUSH_INSTALLS["matinee"]
            assert our_id not in PRESET_INSTALLS, "test setup: id collision"
            pins = [p for p in INSTALL_PATCHES if p.get("pinned")]
            assert pins, "daemon never pinned an installation"
            assert all(p["iname"] == our_id for p in pins), (
                f"pinned the wrong installation: {pins} (expected id {our_id})"
            )
            assert len(pins) == 1, f"pinned repeatedly: {pins}"
            print(f"pinned installation: {our_id} (push label was 'matinee')")

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
