"""
SQLite-backed event queue and delivery audit log.

Design: accept-then-deliver. POST /collect writes the event and returns 200
immediately. A background worker drains the queue to Meta. This decouples the
user-facing latency from Meta's availability, and — more importantly — makes
event loss *visible* instead of silent.

Three tables:
  events     — the durable record of what we accepted. Source of truth.
  deliveries — one row per delivery ATTEMPT. Not a log; an audit trail.
               reconcile.py reads this to answer "what did we accept that Meta
               never confirmed?" That question is the whole product.
  spend      — daily ad spend, joined against revenue in the dashboard.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    event_name        TEXT NOT NULL,
    event_time        INTEGER NOT NULL,
    event_source_url  TEXT NOT NULL,
    user_data         TEXT NOT NULL,          -- JSON, already hashed
    custom_data       TEXT,                   -- JSON, nullable
    status            TEXT NOT NULL DEFAULT 'pending',
                                              -- pending | delivered | failed
    attempts          INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    delivered_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_status
    ON events(status, attempts);

CREATE TABLE IF NOT EXISTS deliveries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    attempted_at      INTEGER NOT NULL,
    http_status       INTEGER,
    fb_trace_id       TEXT,                   -- Meta's request id, for support
    events_received   INTEGER,                -- what Meta says it got
    error_message     TEXT,
    FOREIGN KEY (event_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_event
    ON deliveries(event_id);

CREATE TABLE IF NOT EXISTS spend (
    day               TEXT PRIMARY KEY,       -- YYYY-MM-DD
    amount            REAL NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD'
);
"""


@contextmanager
def get_conn():
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def insert_event(
    *,
    event_id: str,
    event_name: str,
    event_time: int,
    event_source_url: str,
    user_data: dict,
    custom_data: dict | None,
) -> bool:
    """
    Returns True if inserted, False if this event_id was already accepted.

    The PRIMARY KEY on event_id is the first line of dedup defence: if the
    browser retries the same event (double-click, network retry, back button),
    we accept it exactly once. Meta's own dedup is the second line. Relying on
    only one of them is how double-counted conversions happen.
    """
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
                (event_id, event_name, event_time, event_source_url,
                 user_data, custom_data, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_name,
                event_time,
                event_source_url,
                json.dumps(user_data, sort_keys=True),
                json.dumps(custom_data, sort_keys=True) if custom_data else None,
                now,
            ),
        )
        return cur.rowcount == 1


def claim_pending(limit: int = 20) -> list[sqlite3.Row]:
    """Events still owed to Meta, under the retry ceiling."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM events
             WHERE status = 'pending'
               AND attempts < ?
             ORDER BY created_at
             LIMIT ?
            """,
            (settings.max_delivery_attempts, limit),
        ).fetchall()


def record_delivery(
    *,
    event_id: str,
    http_status: int | None,
    fb_trace_id: str | None = None,
    events_received: int | None = None,
    error_message: str | None = None,
    succeeded: bool,
) -> None:
    """
    Write the attempt to the audit trail, then advance the event's state.

    Note that a failed attempt does NOT delete the event. It stays 'pending'
    with attempts+1 until it either succeeds or exhausts the ceiling, at which
    point it becomes 'failed' — and stays in the table forever, visible to
    reconcile.py. Nothing is ever quietly dropped.
    """
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO deliveries
                (event_id, attempted_at, http_status, fb_trace_id,
                 events_received, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, now, http_status, fb_trace_id, events_received, error_message),
        )

        if succeeded:
            conn.execute(
                """
                UPDATE events
                   SET status = 'delivered',
                       attempts = attempts + 1,
                       delivered_at = ?
                 WHERE event_id = ?
                """,
                (now, event_id),
            )
        else:
            conn.execute(
                """
                UPDATE events
                   SET attempts = attempts + 1,
                       status = CASE
                                  WHEN attempts + 1 >= ? THEN 'failed'
                                  ELSE 'pending'
                                END
                 WHERE event_id = ?
                """,
                (settings.max_delivery_attempts, event_id),
            )


def stats() -> dict:
    """Counts by status — the number reconcile.py and the dashboard both read."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}

        confirmed = conn.execute(
            """
            SELECT COUNT(DISTINCT event_id) AS n
              FROM deliveries
             WHERE events_received >= 1
            """
        ).fetchone()["n"]

    accepted = sum(by_status.values())
    return {
        "accepted": accepted,
        "pending": by_status.get("pending", 0),
        "delivered": by_status.get("delivered", 0),
        "failed": by_status.get("failed", 0),
        "confirmed_by_meta": confirmed,
        "unconfirmed": accepted - confirmed,
    }
