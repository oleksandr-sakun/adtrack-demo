#!/usr/bin/env python3
"""
Reconcile what we accepted against what Meta confirmed.

Every tracking system reports success. The question this tool answers is a
different one: is the success real?

An event can be lost in ways that leave no error anywhere. The collector
accepted it (200 to the browser), the worker delivered it (200 from Meta), and
yet Meta's `events_received` said 0 — or the event exhausted its retries during
an outage and nobody was watching the log at 3am. Both cases look like a
healthy system from the outside. Neither shows up in ad spend until the
optimisation has already been running on a number that is wrong.

The invariant this asserts:

    accepted == confirmed_by_meta

Any gap is a real, countable conversion that the ad platform does not know
about. This prints the gap, and then prints exactly which events it consists
of, because "you lost 3 events" is a metric and "you lost THESE 3 events" is
something you can act on.

  python tools/reconcile.py
  python tools/reconcile.py --verbose      # list every unconfirmed event
  python tools/reconcile.py --exit-code    # non-zero if a gap exists (for cron)
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import get_conn  # noqa: E402


def ts(epoch: int | None) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch_summary(conn: sqlite3.Connection) -> dict:
    accepted = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]

    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM events GROUP BY status"
        )
    }

    # Confirmed means Meta explicitly told us it received the event.
    # NOT "we got a 200" — a 200 with events_received=0 is a silent drop, and
    # trusting the status code alone is precisely how that drop stays silent.
    confirmed = conn.execute(
        """
        SELECT COUNT(DISTINCT event_id) n
          FROM deliveries
         WHERE events_received >= 1
        """
    ).fetchone()["n"]

    return {
        "accepted": accepted,
        "pending": by_status.get("pending", 0),
        "delivered": by_status.get("delivered", 0),
        "failed": by_status.get("failed", 0),
        "confirmed": confirmed,
        "gap": accepted - confirmed,
    }


def fetch_unconfirmed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Events we accepted that Meta never confirmed receiving.

    LEFT JOIN rather than NOT IN: an event with zero delivery attempts (the
    worker never got to it) and an event with five failed attempts are both
    unconfirmed, and both belong in this list. A NOT IN over the deliveries
    table would quietly miss the first kind.
    """
    return conn.execute(
        """
        SELECT e.event_id,
               e.event_name,
               e.status,
               e.attempts,
               e.created_at,
               COUNT(d.id)                AS delivery_attempts,
               MAX(d.http_status)         AS last_http,
               MAX(d.attempted_at)        AS last_attempt,
               (SELECT error_message
                  FROM deliveries
                 WHERE event_id = e.event_id
                 ORDER BY attempted_at DESC
                 LIMIT 1)                 AS last_error
          FROM events e
          LEFT JOIN deliveries d
                 ON d.event_id = e.event_id
                AND d.events_received >= 1
         GROUP BY e.event_id
        HAVING COUNT(d.id) = 0
         ORDER BY e.created_at
        """
    ).fetchall()


def fetch_revenue_at_risk(conn: sqlite3.Connection) -> float:
    """
    The gap in dollars, not events.

    A lost ViewContent costs nothing. A lost Purchase costs the optimiser its
    signal on real revenue. Counting events treats them the same; this does not.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(json_extract(e.custom_data, '$.value')), 0) v
          FROM events e
          LEFT JOIN deliveries d
                 ON d.event_id = e.event_id
                AND d.events_received >= 1
         WHERE e.event_name = 'Purchase'
         GROUP BY e.event_id
        HAVING COUNT(d.id) = 0
        """
    ).fetchall()
    return sum(r["v"] for r in row)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--verbose", action="store_true",
                   help="list every unconfirmed event")
    p.add_argument("--exit-code", action="store_true",
                   help="exit non-zero when a gap exists")
    args = p.parse_args()

    with get_conn() as conn:
        s = fetch_summary(conn)
        unconfirmed = fetch_unconfirmed(conn)
        at_risk = fetch_revenue_at_risk(conn)

    print("  accepted by collector   ", s["accepted"])
    print("  confirmed by Meta       ", s["confirmed"])
    print("  ─────────────────────────")
    print("  gap                     ", s["gap"])
    print()
    print(f"  pending {s['pending']}   delivered {s['delivered']}   failed {s['failed']}")

    if s["gap"] == 0:
        print()
        print("  ✓ every accepted event is confirmed received by Meta")
        return 0

    print()
    if at_risk:
        print(f"  ⚠ {s['gap']} event(s) unconfirmed — ${at_risk:,.2f} of "
              f"Purchase value Meta never saw")
    else:
        print(f"  ⚠ {s['gap']} event(s) unconfirmed")

    if args.verbose:
        print()
        print(f"  {'event_id':<34} {'event':<22} {'status':<10} {'tries':<6} last error")
        print("  " + "─" * 100)
        for r in unconfirmed:
            err = (r["last_error"] or "never attempted")[:40]
            print(f"  {r['event_id']:<34} {r['event_name']:<22} "
                  f"{r['status']:<10} {r['attempts']:<6} {err}")
    else:
        print("  run with --verbose to see which ones")

    return 1 if args.exit_code else 0


if __name__ == "__main__":
    raise SystemExit(main())
