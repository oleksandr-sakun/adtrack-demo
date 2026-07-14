"""
Collector API.

The one design decision that matters here: POST /collect does NOT call Meta.
It writes the event to SQLite and returns 200 immediately. The worker delivers
it out of band.

The naive alternative — accept, POST to Meta, then respond — couples the user's
page to Meta's availability. When Meta is slow, the quiz freezes. When Meta
returns 5xx, the conversion is gone and nobody finds out. Accepting first makes
the event durable at the moment it happens, and turns delivery into a state
that can be retried and audited rather than an event that either happened or
didn't.
"""

import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.hashing import build_user_data
from app.logging_setup import configure
from app.models import IncomingEvent
from app.store import init_db, insert_event, stats
from app.worker import run_forever

configure()
log = logging.getLogger("adtrack.api")

import asyncio  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(run_forever())
    log.info("collector ready (mode=%s)",
             "TEST" if settings.meta_test_event_code else "LIVE")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="adtrack-demo", lifespan=lifespan)


def client_ip(request: Request) -> str | None:
    """
    Behind a reverse proxy, request.client.host is the proxy. Meta wants the
    real visitor IP — a wrong IP is worse than no IP, because it is a confident
    identifier pointing at the wrong person, which drags match quality down
    instead of leaving it neutral.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@app.post("/collect")
async def collect(event: IncomingEvent, request: Request):
    if event.requires_value() and (event.value is None or not event.currency):
        # Meta accepts a Purchase without value/currency and optimises blindly.
        # We reject it, because a Purchase whose worth is unknown is a bug
        # upstream, and swallowing it here would hide that bug in the ad spend.
        raise HTTPException(
            status_code=422,
            detail="Purchase requires both value and currency",
        )

    user_data = build_user_data(
        email=event.email,
        phone=event.phone,
        client_ip=client_ip(request),
        client_user_agent=request.headers.get("user-agent"),
        fbc=event.fbc,
        fbp=event.fbp,
    )

    custom_data = None
    if event.value is not None and event.currency:
        custom_data = {"value": event.value, "currency": event.currency}

    accepted = insert_event(
        event_id=event.event_id,
        event_name=event.event_name,
        event_time=int(time.time()),   # server clock, never the client's
        event_source_url=event.event_source_url,
        user_data=user_data,
        custom_data=custom_data,
    )

    if not accepted:
        # Not an error. The browser retried, or the user double-clicked.
        # Returning 200 keeps the client simple; `duplicate` tells the truth.
        log.info("duplicate %s %s", event.event_name, event.event_id)
        return JSONResponse({"status": "duplicate", "event_id": event.event_id})

    log.info("accepted %s %s (%s identifiers)",
             event.event_name, event.event_id, len(user_data))
    return JSONResponse({"status": "accepted", "event_id": event.event_id})


@app.get("/health")
async def health():
    return {
        "ok": True,
        "mode": "TEST" if settings.meta_test_event_code else "LIVE",
        "dataset": settings.meta_pixel_id,
        **stats(),
    }


@app.get("/")
async def landing() -> HTMLResponse:
    """
    The Pixel ID is injected at serve time rather than hard-coded in the HTML.

    It is not a secret — a Pixel ID is visible in the page source of every site
    that runs one. But hard-coding it means anyone cloning this repo silently
    sends their test events into MY dataset, which is both useless to them and
    noise for me. Templating it keeps the repo runnable by anyone with their own
    .env, which is the difference between a demo you can look at and a demo you
    can run.
    """
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    return HTMLResponse(html.replace("__META_PIXEL_ID__", settings.meta_pixel_id))


app.mount("/static", StaticFiles(directory="static"), name="static")
