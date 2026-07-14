"""
Background delivery worker.

Drains the pending queue to Meta. Runs inside the FastAPI process via lifespan,
because at demo scale a separate process buys nothing but deployment ceremony.
At real volume this becomes its own systemd unit reading the same table — the
queue is the interface, so that migration touches no other module.

The worker sends events ONE AT A TIME rather than batching them. Meta accepts
up to 1000 per request, and batching is faster — but a batch gives you one
status code for many events, so a partial failure inside the batch is invisible
at the row level. Per-event delivery means every row in `deliveries` maps to
exactly one event, and reconcile.py can answer "what happened to THIS
conversion?" with a straight answer. Batching is the optimisation you add after
the audit trail works, not before.
"""

import asyncio
import json
import logging

from app.capi import send_with_backoff
from app.config import settings
from app.store import claim_pending, record_delivery

log = logging.getLogger("adtrack.worker")


async def deliver_one(row) -> None:
    event = {
        "event_name": row["event_name"],
        "event_time": row["event_time"],
        "event_id": row["event_id"],
        "event_source_url": row["event_source_url"],
        "action_source": "website",
        "user_data": json.loads(row["user_data"]),
    }
    if row["custom_data"]:
        event["custom_data"] = json.loads(row["custom_data"])

    result = await send_with_backoff([event], attempt=row["attempts"])

    record_delivery(
        event_id=row["event_id"],
        http_status=result.http_status,
        fb_trace_id=result.fb_trace_id,
        events_received=result.events_received,
        error_message=result.error_message,
        succeeded=result.succeeded,
    )

    if result.succeeded:
        log.info(
            "delivered %s %s (trace=%s)",
            row["event_name"], row["event_id"], result.fb_trace_id,
        )
    elif result.permanent:
        # A permanent failure is a bug in OUR payload, not a Meta outage.
        # It is logged loudly because retrying will not fix it — a human must.
        log.error(
            "permanent failure %s: http=%s %s",
            row["event_id"], result.http_status, result.error_message,
        )
    else:
        log.warning(
            "transient failure %s (attempt %s/%s): %s",
            row["event_id"], row["attempts"] + 1,
            settings.max_delivery_attempts, result.error_message,
        )


async def drain_once() -> int:
    """One pass over the queue. Returns how many events were attempted."""
    rows = claim_pending()
    for row in rows:
        await deliver_one(row)
    return len(rows)


async def run_forever() -> None:
    log.info(
        "worker started (interval=%ss, max_attempts=%s, mode=%s)",
        settings.worker_interval_sec,
        settings.max_delivery_attempts,
        "TEST" if settings.meta_test_event_code else "LIVE",
    )
    while True:
        try:
            await drain_once()
        except asyncio.CancelledError:
            log.info("worker stopping")
            raise
        except Exception:
            # The worker must not die. A crash here means events silently stop
            # being delivered while the collector keeps happily accepting them —
            # the queue grows, nobody notices, and the data is wrong for days.
            log.exception("worker pass failed; continuing")
        await asyncio.sleep(settings.worker_interval_sec)
