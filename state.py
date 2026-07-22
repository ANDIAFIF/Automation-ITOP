"""
State layer sync agent (SQLite).

Tabel:
  - sync_state     : high-water mark `last_update` per class iTop
  - synced_tickets : hash payload terakhir yang berhasil di-push (dedup
                     saat re-fetch inklusif >= HWM)

Key design:
  - HWM disimpan verbatim (string datetime iTop 'YYYY-MM-DD HH:MM:SS') —
    dibandingkan server-side oleh iTop, tanpa math timezone di sini.
  - HWM hanya di-advance SETELAH batch di-ack dashboard tanpa error.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import BASE_DIR, settings


def _resolve_db_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


DB_PATH = _resolve_db_path(settings.database_path)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    itop_class          TEXT PRIMARY KEY,
    last_update_hwm     TEXT,
    last_poll_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS synced_tickets (
    itop_class          TEXT NOT NULL,
    itop_id             INTEGER NOT NULL,
    ref                 TEXT,
    last_update         TEXT,
    payload_hash        TEXT NOT NULL,
    synced_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (itop_class, itop_id)
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# -----------------------------------------------------------------------------
# HWM per class
# -----------------------------------------------------------------------------
def get_hwm(itop_class: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_update_hwm FROM sync_state WHERE itop_class = ?", (itop_class,)
        ).fetchone()
        return row["last_update_hwm"] if row else None


def set_hwm(itop_class: str, hwm: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (itop_class, last_update_hwm, last_poll_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(itop_class) DO UPDATE SET
                last_update_hwm = excluded.last_update_hwm,
                last_poll_at = excluded.last_poll_at
            """,
            (itop_class, hwm),
        )


def touch_poll(itop_class: str) -> None:
    """Update last_poll_at tanpa mengubah HWM (cycle tanpa data baru)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (itop_class, last_update_hwm, last_poll_at)
            VALUES (?, NULL, datetime('now'))
            ON CONFLICT(itop_class) DO UPDATE SET last_poll_at = datetime('now')
            """,
            (itop_class,),
        )


# -----------------------------------------------------------------------------
# Dedup payload hash
# -----------------------------------------------------------------------------
def get_payload_hash(itop_class: str, itop_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_hash FROM synced_tickets WHERE itop_class = ? AND itop_id = ?",
            (itop_class, itop_id),
        ).fetchone()
        return row["payload_hash"] if row else None


def record_synced(
    itop_class: str, itop_id: int, ref: str | None, last_update: str | None, phash: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO synced_tickets (itop_class, itop_id, ref, last_update, payload_hash, synced_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(itop_class, itop_id) DO UPDATE SET
                ref = excluded.ref,
                last_update = excluded.last_update,
                payload_hash = excluded.payload_hash,
                synced_at = excluded.synced_at
            """,
            (itop_class, itop_id, ref, last_update, phash),
        )


if __name__ == "__main__":
    print(f"Init database di: {DB_PATH}")
    init_db()
    with get_conn() as conn:
        for row in conn.execute("SELECT * FROM sync_state").fetchall():
            print(dict(row))
