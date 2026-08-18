"""Tronbyt server HTTP client. Only the bits we need.

Reference: https://github.com/tronbyt/server/blob/main/API.md
"""
from __future__ import annotations

import base64
from typing import Any

import httpx

from .config import TronbytCfg


class TronbytClient:
    def __init__(self, cfg: TronbytCfg, timeout: float = 10.0) -> None:
        self._cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TronbytClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def push_webp(self, image_bytes: bytes, background: bool = False) -> None:
        """Push a pre-rendered WebP. Overwrites any previous push at the same
        installation_id. The device will pick it up on its next /next poll."""
        payload = {
            "installationID": self._cfg.installation_id,
            "image": base64.b64encode(image_bytes).decode("ascii"),
            "background": background,
        }
        r = self._client.post(
            f"/v0/devices/{self._cfg.device_id}/push", json=payload,
        )
        r.raise_for_status()

    def set_default_interval(self, seconds: int) -> None:
        """Set the device-level intervalSec, which becomes Tronbyt-Dwell-Secs
        for installations whose own display_time is 0 (pushed installs).
        """
        r = self._client.patch(
            f"/v0/devices/{self._cfg.device_id}",
            json={"intervalSec": seconds},
        )
        r.raise_for_status()

    def pin_installation(self) -> None:
        """Pin our installation so the device only ever shows our content."""
        r = self._client.patch(
            f"/v0/devices/{self._cfg.device_id}/installations/{self._cfg.installation_id}",
            json={"pinned": True},
        )
        r.raise_for_status()
