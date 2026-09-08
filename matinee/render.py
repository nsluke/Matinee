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


# ---- uniform-delay animation ------------------------------------------------
#
# Pillow/libwebp collapse consecutive frames that are identical after lossy
# encoding, extending the previous frame's duration instead of emitting a new
# one. Total playing time is preserved, so this is invisible to a player that
# honours per-frame durations.
#
# Pixlet is not such a player. It re-encodes with ONE delay for the whole
# animation (encode/webp.go: `frameDuration := s.delay`), so a merged chunk
# loses exactly the time that was folded into the merged frames — measured on
# real episodes, a median chunk plays in 77% of its true duration and the worst
# in a third of it. For anything Pixlet renders we therefore need every frame
# present, at one fixed duration.
#
# Perturbing a pixel to defeat the merge works only if the change survives
# quantization, which means a visible flicker. Assembling the RIFF container
# ourselves is exact and costs nothing visually.

_VP8X_ANIMATION = 0x02


def _riff_chunk(fourcc: bytes, payload: bytes) -> bytes:
    """One RIFF chunk: FourCC, LE size, payload, pad to even length."""
    out = fourcc + len(payload).to_bytes(4, "little") + payload
    return out + b"\x00" if len(payload) % 2 else out


def _still_bitstream(frame: Image.Image, quality: int) -> bytes:
    """Encode one frame and return its raw VP8/VP8L chunk, header included."""
    buf = io.BytesIO()
    frame.save(buf, format="WEBP", quality=quality, method=4)
    data = buf.getvalue()
    # RIFF....WEBP then a sequence of chunks; we want the image bitstream one.
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        end = pos + 8 + size + (size % 2)
        if fourcc in (b"VP8 ", b"VP8L"):
            return data[pos:pos + 8 + size + (size % 2)]
        pos = end
    raise ValueError("no VP8/VP8L bitstream in encoded frame")


def encode_uniform_animation(
    frames: Sequence[Image.Image], fps: int, quality: int,
) -> bytes:
    """Animated WebP with every frame kept, each shown for exactly 1000/fps ms.

    Same output as encode_animation for a player that honours per-frame
    timing; the difference is that no frame is merged away, so a player that
    applies a single delay to the whole animation still gets the right
    duration and pacing.
    """
    if not frames:
        raise ValueError("no frames to encode")
    duration_ms = max(1, int(round(1000 / fps)))

    vp8x = bytes([_VP8X_ANIMATION, 0, 0, 0])
    vp8x += (WIDTH - 1).to_bytes(3, "little") + (HEIGHT - 1).to_bytes(3, "little")
    anim = (0).to_bytes(4, "little") + (0).to_bytes(2, "little")  # bg, loop forever

    body = _riff_chunk(b"VP8X", vp8x) + _riff_chunk(b"ANIM", anim)
    for frame in frames:
        payload = (
            (0).to_bytes(3, "little")            # x offset / 2
            + (0).to_bytes(3, "little")          # y offset / 2
            + (WIDTH - 1).to_bytes(3, "little")
            + (HEIGHT - 1).to_bytes(3, "little")
            + duration_ms.to_bytes(3, "little")
            + bytes([0x02])                      # no blend, no dispose
            + _still_bitstream(frame, quality)
        )
        body += _riff_chunk(b"ANMF", payload)

    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def anmf_durations(data: bytes) -> list[int]:
    """Per-frame durations (ms) read straight from the WebP container.

    Pillow's WebP reader reports None for these, so read the ANMF chunks.
    """
    out: list[int] = []
    pos = 12  # past "RIFF<size>WEBP"
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if fourcc == b"ANMF":
            # x,y,w,h are 3 bytes each, then a 3-byte duration.
            out.append(int.from_bytes(data[pos + 20:pos + 23], "little"))
        pos += 8 + size + (size % 2)
    return out


def expand_to_uniform(data: bytes, fps: int, quality: int) -> bytes:
    """Re-time an animated WebP so every frame lasts exactly 1000/fps ms.

    Chunks on disk have merged frames (see encode_uniform_animation). Rather
    than re-ingest a whole library to serve Pixlet, restore the frames the
    merge folded away by repeating each one for as long as it was held.
    """
    step = 1000 / fps
    durations = anmf_durations(data)
    src = Image.open(io.BytesIO(data))

    frames: list[Image.Image] = []
    for i in range(src.n_frames):
        src.seek(i)
        frame = src.convert("RGB")
        held = durations[i] if i < len(durations) else step
        for _ in range(max(1, round(held / step))):
            frames.append(frame)
    return encode_uniform_animation(frames, fps, quality)
