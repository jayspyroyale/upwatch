"""SQLite storage for monitors and their check results.

Every public method opens its own short-lived connection, so a single Store can
be shared safely between the web server threads and the scheduler.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

HISTORY_LIMIT = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    paused      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id   INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    checked_at   REAL    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('up', 'down')),
    status_code  INTEGER,
    response_ms  INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_monitor ON checks (monitor_id, id DESC);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def default_db_path() -> Path:
    """Database location: $UPWATCH_DB, else ~/.upwatch/upwatch.db."""
    env = os.environ.get("UPWATCH_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".upwatch" / "upwatch.db"


class ValidationError(ValueError):
    """Raised for bad user input (invalid URL, empty name, ...)."""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValidationError("URL is required.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValidationError(f"'{url}' is not a valid URL.")
    return url


@dataclass
class Check:
    id: int
    monitor_id: int
    checked_at: float
    status: str
    status_code: int | None
    response_ms: int | None
    error: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Monitor:
    id: int
    name: str
    url: str
    paused: bool
    created_at: float
    last_check: Check | None = None

    @property
    def status(self) -> str:
        """'up', 'down' or 'unknown' (never checked yet)."""
        return self.last_check.status if self.last_check else "unknown"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status
        return data


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:  # commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    # -- monitors ---------------------------------------------------------

    def add_monitor(self, url: str, name: str | None = None) -> Monitor:
        url = normalize_url(url)
        name = (name or "").strip() or urlparse(url).hostname or url
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO monitors (name, url, created_at) VALUES (?, ?, ?)",
                (name, url, time.time()),
            )
            monitor_id = cur.lastrowid
        return self.get_monitor(monitor_id)

    def get_monitor(self, monitor_id: int) -> Monitor | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
            if row is None:
                return None
            return self._monitor_from_row(conn, row)

    def find_monitor(self, ref: str) -> Monitor | None:
        """Look a monitor up by numeric id or (case-insensitive) name."""
        ref = str(ref).strip()
        if ref.isdigit():
            found = self.get_monitor(int(ref))
            if found:
                return found
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM monitors WHERE lower(name) = lower(?) ORDER BY id LIMIT 1", (ref,)
            ).fetchone()
            return self._monitor_from_row(conn, row) if row else None

    def list_monitors(self, active_only: bool = False) -> list[Monitor]:
        sql = "SELECT * FROM monitors"
        if active_only:
            sql += " WHERE paused = 0"
        sql += " ORDER BY id"
        with self._connect() as conn:
            return [self._monitor_from_row(conn, row) for row in conn.execute(sql).fetchall()]

    def set_paused(self, monitor_id: int, paused: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE monitors SET paused = ? WHERE id = ?", (int(paused), monitor_id)
            )
            return cur.rowcount > 0

    def remove_monitor(self, monitor_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM checks WHERE monitor_id = ?", (monitor_id,))
            cur = conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            return cur.rowcount > 0

    # -- checks -----------------------------------------------------------

    def record_check(
        self,
        monitor_id: int,
        status: str,
        status_code: int | None = None,
        response_ms: int | None = None,
        error: str | None = None,
        checked_at: float | None = None,
        keep: int = HISTORY_LIMIT,
    ) -> Check | None:
        """Save a check result and trim history to the newest `keep` rows.

        Returns None if the monitor was removed while the check was running.
        """
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM monitors WHERE id = ?", (monitor_id,)).fetchone() is None:
                return None
            cur = conn.execute(
                "INSERT INTO checks (monitor_id, checked_at, status, status_code, response_ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (monitor_id, checked_at or time.time(), status, status_code, response_ms, error),
            )
            conn.execute(
                "DELETE FROM checks WHERE monitor_id = ? AND id NOT IN ("
                " SELECT id FROM checks WHERE monitor_id = ? ORDER BY id DESC LIMIT ?)",
                (monitor_id, monitor_id, keep),
            )
            row = conn.execute("SELECT * FROM checks WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._check_from_row(row)

    def history(self, monitor_id: int, limit: int = HISTORY_LIMIT) -> list[Check]:
        """Most recent checks first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checks WHERE monitor_id = ? ORDER BY id DESC LIMIT ?",
                (monitor_id, limit),
            ).fetchall()
            return [self._check_from_row(r) for r in rows]

    # -- settings ---------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _check_from_row(row: sqlite3.Row) -> Check:
        return Check(
            id=row["id"],
            monitor_id=row["monitor_id"],
            checked_at=row["checked_at"],
            status=row["status"],
            status_code=row["status_code"],
            response_ms=row["response_ms"],
            error=row["error"],
        )

    def _monitor_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Monitor:
        last = conn.execute(
            "SELECT * FROM checks WHERE monitor_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        return Monitor(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            paused=bool(row["paused"]),
            created_at=row["created_at"],
            last_check=self._check_from_row(last) if last else None,
        )
