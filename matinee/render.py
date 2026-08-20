"""Shared rendering primitives: source video -> 64x32 animated WebP.

Both paths — ingest (files on disk) and live (a stream) — scale frames the
same way and assemble them into WebP the same way, so the geometry, the
ffmpeg video filter and the Pillow save call live here rather than being
duplicated in each caller.

Pillow does the WebP encoding rather than ffmpeg's libwebp_anim encoder,
which many ffmpeg builds omit (Homebrew's, for one).
"""
from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image

WIDTH, HEIGHT = 64, 32
BYTES_PER_FRAME = WIDTH * HEIGHT * 3  # rgb24

VALID_FIT_MODES = ("crop", "letterbox", "stretch")


def video_filter(fit_mode: str, fps: int) -> str:
    """ffmpeg -vf expression that lands any source on the 64x32 display."""
    if fit_mode == "crop":
        scale = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT}"
        )
    elif fit_mode == "letterbox":
        scale = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif fit_mode == "stretch":
        scale = f"scale={WIDTH}:{HEIGHT},setsar=1"
    else:
        raise ValueError(
            f"Unknown fit_mode: {fit_mode}. Expected one of {VALID_FIT_MODES}."
        )
    return f"fps={fps},{scale}"


def raw_video_cmd(input_arg: str, fit_mode: str, fps: int) -> list[str]:
    """ffmpeg argv that decodes `input_arg` to raw rgb24 frames on stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", input_arg,
        "-vf", video_filter(fit_mode, fps),
        "-pix_fmt", "rgb24",
        "-f", "rawvideo",
        "-an", "-sn",
        "-",
    ]


def frame_from_bytes(raw: bytes) -> Image.Image:
    return Image.frombytes("RGB", (WIDTH, HEIGHT), raw)


def encode_animation(
    frames: Sequence[Image.Image], fps: int, quality: int,
) -> bytes:
    """Assemble frames into one looping animated WebP."""
    out = io.BytesIO()
    duration_ms = max(1, int(1000 / fps))
    frames[0].save(
        out, format="WEBP",
        save_all=True, append_images=list(frames[1:]),
        duration=duration_ms, loop=0,
        quality=quality, method=4,
    )
    return out.getvalue()
