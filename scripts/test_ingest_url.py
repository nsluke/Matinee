"""Unit-ish test for the `crunchybyt-ingest url` wiring.

Verifies that ingest_url:
  - calls the downloader with the right args (URL, output template, max_height),
  - discovers the file the downloader wrote,
  - skips the download on a second invocation when the source already exists,
  - hands the discovered path to ingest_one (which we stub, since ffmpeg may
    not be available on dev machines).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from crunchybyt import ingest as ingest_mod
from crunchybyt.config import load as load_cfg

CONFIG_TEMPLATE = """
[tronbyt]
server_url = "http://x"
device_id = "x"
api_key = "x"
installation_id = "x"

[playback]
chunk_seconds = 15
fps = 10
fit_mode = "crop"
quality = 75
min_tail_seconds = 3

[library]
chunks_root = "{root}/chunks"
sources_root = "{root}/sources"

[daemon]
host = "127.0.0.1"
port = 9999
state_db = "{root}/state.sqlite"
push_lead_seconds = 0
"""


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg_path = root / "config.toml"
        cfg_path.write_text(CONFIG_TEMPLATE.format(root=root))
        cfg = load_cfg(cfg_path)

        download_calls: list[tuple[str, str, int | None]] = []

        def fake_downloader(url: str, template: str, max_height: int | None) -> None:
            download_calls.append((url, template, max_height))
            out = Path(template.replace("%(ext)s", "mp4"))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake video bytes")

        ingest_one_calls: list[tuple[Path, str, str]] = []

        def fake_ingest_one(src, cfg_, show, episode, force=False):
            ingest_one_calls.append((src, show, episode))
            return root / "chunks" / "stub"

        original_ingest_one = ingest_mod.ingest_one
        ingest_mod.ingest_one = fake_ingest_one
        try:
            # First call: should download.
            ingest_mod.ingest_url(
                "https://youtube.com/watch?v=ABC",
                cfg, "Dragon Ball Z", "S01E01",
                downloader=fake_downloader,
            )

            assert len(download_calls) == 1, download_calls
            url, template, mh = download_calls[0]
            assert url == "https://youtube.com/watch?v=ABC"
            assert "Dragon-Ball-Z" in template
            assert "S01E01" in template
            assert template.endswith("%(ext)s")
            assert mh == 480, f"default cap should be 480p, got {mh}"
            print(f"  download call: ...{template[-40:]}, cap={mh}")

            assert len(ingest_one_calls) == 1
            src, show, ep = ingest_one_calls[0]
            assert src.name == "S01E01.mp4", src
            assert show == "Dragon Ball Z"
            assert ep == "S01E01"
            print(f"  ingest_one received: {src.name}")

            # Second call: source already on disk, should skip download.
            download_calls.clear()
            ingest_one_calls.clear()
            ingest_mod.ingest_url(
                "https://youtube.com/watch?v=ABC",
                cfg, "Dragon Ball Z", "S01E01",
                downloader=fake_downloader,
            )
            assert len(download_calls) == 0, "should have skipped re-download"
            assert len(ingest_one_calls) == 1, "should still re-ingest"
            print("  second call: skipped download, re-ingested OK")

            # --no-cap: max_height=None propagates.
            download_calls.clear()
            ingest_mod.ingest_url(
                "https://youtube.com/watch?v=XYZ",
                cfg, "Mobile Suit Gundam", "S01E01",
                downloader=fake_downloader,
                max_height=None,
            )
            assert download_calls[0][2] is None, download_calls
            print("  --no-cap: max_height=None propagates")

            # --cleanup removes the source after ingest.
            ingest_mod.ingest_url(
                "https://youtube.com/watch?v=DEF",
                cfg, "Dragon Ball Z", "S01E02",
                downloader=fake_downloader,
                cleanup=True,
            )
            leftover = list((root / "sources" / "Dragon-Ball-Z").glob("S01E02.*"))
            assert not leftover, f"cleanup should have removed source, found: {leftover}"
            print("  --cleanup: source removed after ingest")

        finally:
            ingest_mod.ingest_one = original_ingest_one

    print("\nINGEST URL TEST PASSED")


if __name__ == "__main__":
    main()
