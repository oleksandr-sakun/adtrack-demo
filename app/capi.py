"""
Meta Conversions API client.

Two things this does that a naive implementation does not:

1. It distinguishes 4xx from 5xx. A 4xx is a broken payload — retrying it five
   times changes nothing, burns the retry budget, and buries the real error
   under a pile of identical failures. It is marked permanent immediately.
   A 5xx is Meta being unavailable — that is exactly what the queue exists for,
   and it retries with backoff.

2. It reads Meta's response instead of just checking the status code. Meta
   returns `events_received` and `fb_trace_id` in the body. A 200 with
   events_received=0 is a silent drop — the request succeeded, the event did
   not. Code that only checks `resp.status_code == 200` reports success there.
"""

import asyncio
import json
from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass
class DeliveryResult:
    succeeded: bool
    http_status: int | None
    events_received: int | None = None
    fb_trace_id: str | None = None
    error_message: str | None = None
    permanent: bool = False  # True => do not retry, the payload is the problem


def _build_body(events: list[dict]) -> dict:
    """
    The access token goes in the BODY, not the query string.

    Meta accepts both. But a token in the query string ends up in every access
    log, every proxy log, and — because httpx logs the full request URL at INFO
    — in the application log too. A credential that lands in a log file is a
    credential you have to rotate. Putting it in the body costs nothing and
    removes an entire class of leak.
    """
    body: dict = {
        "data": events,
        "access_token": settings.meta_access_token,
    }
    if settings.meta_test_event_code:
        body["test_event_code"] = settings.meta_test_event_code
    return body


async def send_events(events: list[dict]) -> DeliveryResult:
    """POST a batch of events to the Conversions API."""
    if not settings.live_mode:
        return DeliveryResult(
            succeeded=False,
            http_status=None,
            error_message="no credentials configured",
            permanent=True,
        )

    body = _build_body(events)

    try:
        async with httpx.AsyncClient(timeout=settings.capi_timeout_sec) as client:
            resp = await client.post(settings.capi_url, json=body)
    except httpx.TimeoutException:
        # Meta recommends a ~1.5s timeout. A timeout is NOT a failure to
        # deliver — the event may well have landed. It is retried, and the
        # event_id makes the retry safe: Meta dedups it.
        return DeliveryResult(
            succeeded=False,
            http_status=None,
            error_message="timeout",
            permanent=False,
        )
    except httpx.HTTPError as exc:
        return DeliveryResult(
            succeeded=False,
            http_status=None,
            error_message=f"transport: {exc}",
            permanent=False,
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        payload = {}

    trace_id = payload.get("fbtrace_id")

    if resp.status_code >= 500:
        return DeliveryResult(
            succeeded=False,
            http_status=resp.status_code,
            fb_trace_id=trace_id,
            error_message=resp.text[:500],
            permanent=False,  # Meta's problem. Retry.
        )

    if resp.status_code >= 400:
        err = payload.get("error", {})
        msg = err.get("message", resp.text[:500])
        return DeliveryResult(
            succeeded=False,
            http_status=resp.status_code,
            fb_trace_id=trace_id,
            error_message=msg,
            permanent=True,  # Our problem. Retrying will not fix it.
        )

    received = payload.get("events_received")

    # A 200 with events_received == 0 is the failure mode nobody catches.
    if not received:
        return DeliveryResult(
            succeeded=False,
            http_status=resp.status_code,
            events_received=received,
            fb_trace_id=trace_id,
            error_message="HTTP 200 but events_received=0",
            permanent=True,
        )

    return DeliveryResult(
        succeeded=True,
        http_status=resp.status_code,
        events_received=received,
        fb_trace_id=trace_id,
    )


async def send_with_backoff(events: list[dict], attempt: int) -> DeliveryResult:
    """
    Exponential backoff, applied BEFORE the call, scaled by prior attempts.
    attempt=0 -> no wait, 1 -> 2s, 2 -> 4s, 3 -> 8s (capped at 30s).
    """
    if attempt > 0:
        await asyncio.sleep(min(2 ** attempt, 30))
    return await send_events(events)
