"""SQLite-backed playback state. Single-row 'position' table.

The daemon is the only writer. CLI reads via the HTTP API.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .render import VALID_FIT_MODES  # noqa: F401  (re-exported for callers)


SCHEMA = """
CREATE TABLE IF NOT EXISTS position (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    show TEXT,
    episode TEXT,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'library',
    live_url TEXT,
    fit_mode TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show TEXT NOT NULL,
    episode TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    pushed_at REAL NOT NULL
);
"""

# ALTER statements applied on every open. Each one fails harmlessly if the
# column already exists (SQLite raises OperationalError).
MIGRATIONS = (
    "ALTER TABLE position ADD COLUMN mode TEXT NOT NULL DEFAULT 'library'",
    "ALTER TABLE position ADD COLUMN live_url TEXT",
    "ALTER TABLE position ADD COLUMN fit_mode TEXT",
)

LIBRARY_MODE = "library"
LIVE_MODE = "live"

# settings keys
PIN_INSTALLATION_ID = "pin_installation_id"


@dataclass
class Position:
    show: str | None
    episode: str | None
    chunk_index: int
    paused: bool
    mode: str
    live_url: str | None
    fit_mode: str | None
    updated_at: float


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False,
        )
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    self._db.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._db.execute(
                "INSERT OR IGNORE INTO position "
                "(id, chunk_index, paused, mode, updated_at) "
                "VALUES (1, 0, 0, 'library', ?)",
                (time.time(),),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self) -> Position:
        with self._lock:
            row = self._db.execute(
                "SELECT show, episode, chunk_index, paused, mode, live_url, "
                "fit_mode, updated_at FROM position WHERE id = 1"
            ).fetchone()
        show, episode, chunk_index, paused, mode, live_url, fit_mode, updated_at = row
        return Position(
            show=show, episode=episode, chunk_index=int(chunk_index),
            paused=bool(paused), mode=mode, live_url=live_url,
            fit_mode=fit_mode, updated_at=float(updated_at),
        )

    def set_position(self, show: str, episode: str, chunk_index: int = 0) -> Position:
        """Switch to library mode at the given show/episode/chunk."""
        with self._lock:
            self._db.execute(
                "UPDATE position SET show=?, episode=?, chunk_index=?, "
                "mode='library', updated_at=? WHERE id=1",
                (show, episode, chunk_index, time.time()),
            )
        return self.get()

    def set_live(self, live_url: str) -> Position:
        """Switch to live mode at the given URL."""
        with self._lock:
            self._db.execute(
                "UPDATE position SET mode='live', live_url=?, updated_at=? WHERE id=1",
                (live_url, time.time()),
            )
        return self.get()

    def advance(self, new_index: int) -> Position:
        with self._lock:
            self._db.execute(
                "UPDATE position SET chunk_index=?, updated_at=? WHERE id=1",
                (new_index, time.time()),
            )
        return self.get()

    def advance_from(self, expected_index: int, new_index: int) -> bool:
        """Compare-and-swap advance: only succeeds if chunk_index is still
        `expected_index`. Used by the push tick so it can't overwrite a
        concurrent /skip or /play.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE position SET chunk_index=?, updated_at=? "
                "WHERE id=1 AND chunk_index=?",
                (new_index, time.time(), expected_index),
            )
        return cur.rowcount > 0

    def set_paused(self, paused: bool) -> Position:
        with self._lock:
            self._db.execute(
                "UPDATE position SET paused=?, updated_at=? WHERE id=1",
                (1 if paused else 0, time.time()),
            )
        return self.get()

    def set_fit_mode(self, fit_mode: str | None) -> Position:
        """Override fit_mode for the live transcode. None clears the override
        and falls back to config.playback.fit_mode."""
        if fit_mode is not None and fit_mode not in VALID_FIT_MODES:
            raise ValueError(
                f"Unknown fit_mode: {fit_mode}. Expected one of {VALID_FIT_MODES}."
            )
        with self._lock:
            self._db.execute(
                "UPDATE position SET fit_mode=?, updated_at=? WHERE id=1",
                (fit_mode, time.time()),
            )
        return self.get()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,),
            ).fetchone()
        # No row_factory on this connection — rows are plain tuples.
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def log_push(self, show: str, episode: str, chunk_index: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO history (show, episode, chunk_index, pushed_at) "
                "VALUES (?, ?, ?, ?)",
                (show, episode, chunk_index, time.time()),
            )
