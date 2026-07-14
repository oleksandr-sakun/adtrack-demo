"""
The invariants this suite defends.

Each test below corresponds to a way this pipeline can lie about itself. They
are not here to prove the happy path works — the happy path is easy and it is
visible. They are here because every one of these failures is SILENT: the
system reports success while the data is wrong.
"""

import asyncio
import json

import httpx
import pytest

from app import capi
from app.hashing import build_user_data, hash_email, hash_phone, norm_phone
from app.store import get_conn, insert_event, stats


# ---------------------------------------------------------------------------
# Transport mock
# ---------------------------------------------------------------------------

def mock_transport(monkeypatch, *, status=200, body=None, raises=None):
    """Replace httpx's network layer with a canned response."""
    payload = body if body is not None else {
        "events_received": 1,
        "fbtrace_id": "TRACE123",
    }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            if raises:
                raise raises
            return httpx.Response(
                status_code=status,
                json=payload,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(capi.httpx, "AsyncClient", FakeClient)


# ---------------------------------------------------------------------------
# Hashing — a wrong hash matches nothing, and Meta never says so
# ---------------------------------------------------------------------------

def test_email_normalised_before_hashing():
    """Case and whitespace must not produce different hashes."""
    assert hash_email("  John@Example.COM ") == hash_email("john@example.com")


def test_phone_normalised_to_digits():
    """Formatting must not produce different hashes."""
    assert norm_phone("+380 (67) 123-45-67") == "380671234567"
    assert hash_phone("+380 67 123 45 67") == hash_phone("380671234567")


def test_cookies_and_ip_are_not_hashed():
    """
    Meta requires fbc/fbp/ip/user_agent in PLAINTEXT. Hashing them does not
    protect the user — it destroys the match. This is the single most common
    error in hand-rolled CAPI integrations.
    """
    ud = build_user_data(
        email="a@b.com",
        fbp="fb.1.123.456",
        fbc="fb.1.123.abc",
        client_ip="203.0.113.10",
        client_user_agent="Mozilla/5.0",
    )
    assert ud["fbp"] == "fb.1.123.456"
    assert ud["fbc"] == "fb.1.123.abc"
    assert ud["client_ip_address"] == "203.0.113.10"
    assert ud["client_user_agent"] == "Mozilla/5.0"
    assert len(ud["em"]) == 64          # hashed


def test_empty_identifiers_are_omitted_not_blanked():
    """
    An empty string is an identifier that is present and worthless — it drags
    match quality down. An absent key is simply absent.
    """
    ud = build_user_data(email="", phone=None, client_ip="1.2.3.4")
    assert "em" not in ud
    assert "ph" not in ud
    assert ud["client_ip_address"] == "1.2.3.4"


# ---------------------------------------------------------------------------
# Deduplication — the difference between one conversion and two
# ---------------------------------------------------------------------------

def test_same_event_id_accepted_once():
    """Double-click, browser retry, back button — all must yield one event."""
    args = dict(
        event_name="Purchase",
        event_time=1700000000,
        event_source_url="https://x.io/q",
        user_data={"em": "a" * 64},
        custom_data={"value": 10.0, "currency": "USD"},
    )
    assert insert_event(event_id="dup-1", **args) is True
    assert insert_event(event_id="dup-1", **args) is False
    assert stats()["accepted"] == 1


# ---------------------------------------------------------------------------
# Failure classification — retrying a broken payload fixes nothing
# ---------------------------------------------------------------------------

def test_4xx_is_permanent(monkeypatch):
    """A malformed payload will fail identically five times. Do not retry it."""
    mock_transport(monkeypatch, status=400,
                   body={"error": {"message": "Invalid parameter"}})
    r = asyncio.run(capi.send_events([{"event_name": "Purchase"}]))
    assert r.succeeded is False
    assert r.permanent is True


def test_5xx_is_transient(monkeypatch):
    """Meta being down is exactly what the queue exists for. Retry."""
    mock_transport(monkeypatch, status=503, body={})
    r = asyncio.run(capi.send_events([{"event_name": "Purchase"}]))
    assert r.succeeded is False
    assert r.permanent is False


def test_timeout_is_transient(monkeypatch):
    """
    A timeout is not a failure to deliver — the event may well have landed.
    Retrying is safe because event_id makes Meta dedup our retry.
    """
    mock_transport(monkeypatch,
                   raises=httpx.TimeoutException("timed out"))
    r = asyncio.run(capi.send_events([{"event_name": "Purchase"}]))
    assert r.succeeded is False
    assert r.permanent is False
    assert r.error_message == "timeout"


def test_200_with_zero_received_is_a_failure(monkeypatch):
    """
    THE silent drop. HTTP 200, no error, events_received=0 — the request
    succeeded and the event did not. Code that checks only the status code
    reports this as success and loses the conversion without a trace.
    """
    mock_transport(monkeypatch, status=200,
                   body={"events_received": 0, "fbtrace_id": "T1"})
    r = asyncio.run(capi.send_events([{"event_name": "Purchase"}]))
    assert r.succeeded is False
    assert r.permanent is True
    assert "events_received=0" in r.error_message


# ---------------------------------------------------------------------------
# The queue — nothing is ever quietly dropped
# ---------------------------------------------------------------------------

def _queue_one(event_id="q-1", value=25.0):
    insert_event(
        event_id=event_id,
        event_name="Purchase",
        event_time=1700000000,
        event_source_url="https://x.io/q",
        user_data={"em": "a" * 64},
        custom_data={"value": value, "currency": "USD"},
    )


def test_successful_delivery_marks_confirmed(monkeypatch):
    from app.worker import drain_once

    mock_transport(monkeypatch)
    _queue_one()
    asyncio.run(drain_once())

    s = stats()
    assert s["delivered"] == 1
    assert s["confirmed_by_meta"] == 1
    assert s["unconfirmed"] == 0


def test_failed_event_stays_visible_forever(monkeypatch, no_sleep):
    """
    After exhausting retries the event becomes 'failed' and STAYS in the table.
    It is never deleted. reconcile.py can still find it, name it, and price it.
    A system that drops what it cannot deliver cannot tell you what it lost.
    """
    from app.worker import drain_once

    mock_transport(monkeypatch, status=400,
                   body={"error": {"message": "Invalid parameter"}})
    _queue_one(value=199.0)

    for _ in range(5):
        asyncio.run(drain_once())

    s = stats()
    assert s["failed"] == 1
    assert s["confirmed_by_meta"] == 0
    assert s["unconfirmed"] == 1        # the gap reconcile.py reports

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = 'q-1'"
        ).fetchone()
        assert row is not None                      # not deleted
        assert row["attempts"] == 3                 # ceiling, from conftest

        attempts = conn.execute(
            "SELECT COUNT(*) n FROM deliveries WHERE event_id = 'q-1'"
        ).fetchone()["n"]
        assert attempts == 3                        # every attempt audited


def test_retry_ceiling_stops_the_worker_hammering(monkeypatch, no_sleep):
    """Past the ceiling the event is no longer claimed — no infinite retry."""
    from app.store import claim_pending
    from app.worker import drain_once

    mock_transport(monkeypatch, status=503, body={})
    _queue_one()

    for _ in range(10):
        asyncio.run(drain_once())

    assert claim_pending() == []
    assert stats()["failed"] == 1


def test_transient_failure_then_recovery(monkeypatch, no_sleep):
    """
    Meta returns 503, then recovers. The event must survive the outage and be
    delivered — that is the entire justification for the queue.
    """
    from app.worker import drain_once

    mock_transport(monkeypatch, status=503, body={})
    _queue_one()
    asyncio.run(drain_once())
    assert stats()["pending"] == 1          # still owed, not lost

    mock_transport(monkeypatch, status=200,
                   body={"events_received": 1, "fbtrace_id": "T2"})
    asyncio.run(drain_once())

    s = stats()
    assert s["delivered"] == 1
    assert s["unconfirmed"] == 0


# ---------------------------------------------------------------------------
# Payload shape — what actually goes on the wire
# ---------------------------------------------------------------------------

def test_token_is_in_body_not_query(monkeypatch):
    """
    A token in the query string lands in access logs, proxy logs, and (because
    httpx logs full URLs at INFO) the application log. This asserts it does not.
    """
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **kw):
            captured["url"] = url
            captured["body"] = json
            captured["params"] = kw.get("params")
            return httpx.Response(200, json={"events_received": 1},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(capi.httpx, "AsyncClient", FakeClient)
    asyncio.run(capi.send_events([{"event_name": "ViewContent"}]))

    assert "access_token" not in captured["url"]
    assert captured["params"] is None
    assert captured["body"]["access_token"] == "test-token"


def test_test_event_code_only_when_set(monkeypatch):
    """
    Forgetting to remove test_event_code in production is the classic CAPI
    footgun: events look fine and optimise nothing. It must be present only
    when explicitly configured.
    """
    from app.config import settings

    assert "test_event_code" not in capi._build_body([{}])

    monkeypatch.setattr(settings, "meta_test_event_code", "TEST99999")
    assert capi._build_body([{}])["test_event_code"] == "TEST99999"
