#!/usr/bin/env python3
"""
Send a single event to the Conversions API from the command line.

Useful for: validating credentials, demonstrating deduplication (send the same
--event-id twice), and showing how Event Match Quality responds to the presence
or absence of hashed identifiers (compare --no-pii against the default).

  python tools/send_test_event.py --event Purchase
  python tools/send_test_event.py --event Purchase --no-pii
  python tools/send_test_event.py --event ViewContent --event-id fixed123 # twice
"""

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.capi import send_events          # noqa: E402
from app.config import settings           # noqa: E402
from app.hashing import build_user_data   # noqa: E402

EVENTS = ("ViewContent", "CompleteRegistration", "Purchase")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--event", choices=EVENTS, default="Purchase")
    p.add_argument("--event-id", default=None,
                   help="reuse the same id to test dedup; default is random")
    p.add_argument("--no-pii", action="store_true",
                   help="omit email/phone to show the effect on match quality")
    p.add_argument("--value", type=float, default=49.90)
    p.add_argument("--currency", default="USD")
    return p.parse_args()


def build_event(args: argparse.Namespace) -> dict:
    if args.no_pii:
        user_data = build_user_data(
            client_ip="203.0.113.10",
            client_user_agent="Mozilla/5.0 (adtrack-demo CLI)",
        )
    else:
        user_data = build_user_data(
            email="test@example.com",
            phone="+380671234567",
            client_ip="203.0.113.10",
            client_user_agent="Mozilla/5.0 (adtrack-demo CLI)",
        )

    event = {
        "event_name": args.event,
        "event_time": int(time.time()),
        "event_id": args.event_id or uuid.uuid4().hex,
        "event_source_url": "https://adtrack-demo.local/quiz",
        "action_source": "website",
        "user_data": user_data,
    }

    if args.event == "Purchase":
        event["custom_data"] = {"value": args.value, "currency": args.currency}

    return event


def main() -> int:
    args = parse_args()

    if not settings.live_mode:
        print("No credentials in .env — nothing to send.", file=sys.stderr)
        return 1

    event = build_event(args)

    mode = "TEST" if settings.meta_test_event_code else "LIVE"
    print(f"mode:       {mode}"
          f"{' (' + settings.meta_test_event_code + ')' if mode == 'TEST' else ''}")
    print(f"dataset:    {settings.meta_pixel_id}")
    print(f"event:      {event['event_name']}")
    print(f"event_id:   {event['event_id']}")
    print(f"identifiers: {', '.join(event['user_data'].keys())}")
    print()

    result = asyncio.run(send_events([event]))

    if result.succeeded:
        print(f"OK   events_received={result.events_received}  "
              f"trace={result.fb_trace_id}")
        return 0

    print(f"FAIL http={result.http_status}  permanent={result.permanent}")
    print(f"     {result.error_message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
