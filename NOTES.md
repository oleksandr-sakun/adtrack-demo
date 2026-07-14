# Build notes

Why this system is shaped the way it is, what it refuses to do, and every trap
that cost time on the way. Written while building, not reconstructed after.

The README is the short version. This is the long one.

---

## The premise

Server-side conversion tracking looks like a solved problem. Send an HTTP POST
to Meta with an event, get a 200 back, done. Most implementations are exactly
that, and most of them are quietly wrong.

The failures are not crashes. They are silent:

- The collector returns 200 to the browser.
- The worker returns 200 from Meta.
- Nothing is logged as an error.
- The conversion never reaches the ad platform.

There is no place in Events Manager that says "you lost 3 purchases today."
Meta simply doesn't know about events it didn't receive. The discrepancy
surfaces weeks later as a gap between Stripe and Ads Manager — if anyone
happens to reconcile them.

Everything below is a response to some version of that failure mode.

---

## Architecture: accept, then deliver

`POST /collect` writes the event to SQLite and returns 200 immediately. It does
not call Meta. A background worker drains the queue.

The obvious alternative — accept, POST to Meta, then respond — couples the
user's page to Meta's availability:

- Meta is slow → the quiz freezes on the user's screen.
- Meta returns 5xx → the conversion is gone, and nobody finds out.

Accepting first makes the event durable at the moment it happens. Delivery
becomes a *state* that can be retried and audited, rather than an *event* that
either happened or didn't.

This is the decision the rest of the design hangs off. Once delivery is a state
in a table, "did Meta actually get this?" becomes a query instead of a hope.

**Trade-off accepted:** the browser gets a 200 before Meta has seen anything.
That is correct. The browser's job is to hand off the event, not to wait for a
third party. If delivery later fails permanently, that failure is visible in
`reconcile.py` — which is more than any synchronous implementation offers,
because a synchronous implementation that gets a 5xx has already returned to
the browser and forgotten.

---

## Deduplication: the same event_id in two places

`event_id` is generated **in the browser**, once, and sent to two destinations:

- the Pixel, via `fbq('track', name, data, { eventID: id })`
- our collector, in the POST body

Meta deduplicates on `(event_name, event_id)`. If the server minted its own id,
Meta would see two distinct conversions for one purchase, and the optimiser
would bid against a number that is double the truth.

This looks like a security hole — trusting a browser-supplied identifier. It is
deliberate and bounded: `event_id` influences nothing except which two events
get glued together. It grants no authority.

**Second line of defence, on our side:** `event_id` is the PRIMARY KEY of the
`events` table, and inserts use `INSERT OR IGNORE`. A double-click, a browser
retry, a back-button re-submit — all yield one event. Relying only on Meta's
dedup would be a mistake: Meta's dedup glues pixel↔CAPI, but it does not
protect you from sending the same server event twice yourself.

---

## Hashing: normalise first, or the hash matches nothing

Meta matches on the SHA-256 of a **normalised** value. Hash a raw string and
the hash is valid, well-formed, 64 hex characters — and matches nobody.

The failure is completely silent. The API returns 200. The event is accepted.
Event Match Quality quietly drops, the optimiser gets worse signal, and no
error appears anywhere.

Normalisation rules implemented in `app/hashing.py`:

| Field | Rule |
|---|---|
| email | lowercase, strip |
| phone | digits only, country code included, no `+` |
| name | lowercase, strip |
| zip | lowercase, strip, take part before space/dash |

`+380 (67) 123-45-67` and `380671234567` must produce the same hash. If they
don't, the same user shows up as two people.

### The counter-intuitive part: some fields must NOT be hashed

`fbc`, `fbp`, `client_ip_address`, `client_user_agent` go in **plaintext**.

This feels wrong — they are personal data too. But Meta expects them raw, and
hashing them does not protect the user: it destroys the match. This is the most
common error in hand-rolled CAPI integrations, and it costs match quality while
looking like extra diligence.

### Empty keys are omitted, not blanked

`{"em": ""}` tells Meta "an email identifier is present and it is garbage."
That drags match quality down. An absent key is simply absent, and neutral.

`build_user_data()` filters out falsy values before returning.

---

## Failure classification: 4xx and 5xx are not the same failure

A naive retry loop retries everything. That is wrong in both directions.

**4xx = our payload is broken.** Retrying it five times produces five identical
failures, burns the retry budget, and buries the real error under noise. Marked
`permanent`, logged at ERROR, never retried. A human has to fix it.

**5xx = Meta is down.** This is exactly what the queue exists for. Retried with
exponential backoff (2s, 4s, 8s, capped at 30s), logged at WARNING.

**Timeout = unknown.** The event may well have landed. Retried — and the retry
is safe precisely because `event_id` makes Meta dedup it. Without the shared
event_id, retrying a timeout would risk double-counting.

Collapsing these into one category means real bugs drown in outage noise.

---

## The silent drop: HTTP 200 with events_received = 0

Meta's response body contains `events_received`. A 200 with `events_received: 0`
means the request succeeded and the event did not.

Code that checks `resp.status_code == 200` reports this as success. The
conversion is gone, the log says OK, and the metric is wrong.

`capi.py` treats this as a permanent failure. It is the single most important
line in the client, and it is three lines long.

---

## The delivery table is an audit trail, not a log

`deliveries` holds **one row per attempt**, not one row per event.

That means the system can answer "what happened to THIS conversion?" — how many
times we tried, what Meta returned each time, and Meta's `fbtrace_id` for each
attempt. Without `fbtrace_id`, a support conversation with Meta about a missing
event is not possible.

**Failed events are never deleted.** After exhausting retries an event becomes
`failed` and stays in the table forever, visible to `reconcile.py`. A system
that drops what it cannot deliver cannot tell you what it lost.

---

## Events are sent one at a time, not batched

Meta accepts up to 1000 events per request, and batching is faster.

But a batch returns one status code for many events. A partial failure inside a
batch is invisible at the row level, and `deliveries` can no longer map cleanly
to individual conversions.

Batching is the optimisation you add **after** the audit trail works, not
instead of it. At demo scale it buys nothing but lost visibility.

---

## reconcile.py: the invariant

    accepted == confirmed_by_meta

Any gap is a real, countable conversion the ad platform does not know about.

Two implementation details that matter:

**`LEFT JOIN`, not `NOT IN`.** An event with zero delivery attempts (the worker
never reached it) and an event with five failed attempts are both unconfirmed,
and both belong in the report. A `NOT IN` over `deliveries` would silently miss
the first kind — the exact class of event most likely to be lost.

**"Confirmed" means Meta said `events_received >= 1`.** Not "we got a 200."
Trusting the status code alone is how the silent drop stays silent.

**Revenue at risk, not just event count.** A lost `ViewContent` costs nothing.
A lost `Purchase` costs the optimiser its signal on real money. The tool reports
dollars, because "you lost 3 events" is a metric and "you lost $199 of purchase
value Meta never saw" is a decision.

`--exit-code` exists so this can live in cron and fail loudly when the invariant
breaks.

---

## Tests: no network, no credentials

`evals/` mocks the HTTP transport entirely. `git clone && pytest` is green on a
machine that has never seen a Meta token.

A test suite that requires live API access is a test suite that gets skipped,
and a skipped test protects nothing.

The suite does not test that the happy path works — the happy path is easy and
visible. Each test corresponds to **one way the system can lie about itself**:

- a hash built from an unnormalised value
- a hashed `fbp` that will never match
- an empty-string identifier that looks present
- the same event accepted twice
- a 4xx retried until the real cause is buried
- a 5xx that should have been retried and wasn't
- a 200 with `events_received: 0`
- a failed event deleted instead of kept
- a token in the query string
- `test_event_code` left on in production

15 tests, 1.2 seconds. That speed is the point: it is what makes it safe to let
AI tooling take large swings at the implementation, because a bad swing surfaces
in seconds instead of in production.

---

## Security: the token leaked, twice

**In logs.** The token was originally passed as a query-string parameter.
`httpx` logs the full request URL at INFO level — so the token appeared in
stdout, in the systemd journal, and anywhere those were copied.

Fix, two parts:
1. The token moved into the request **body**. Meta accepts both. A credential in
   a query string ends up in every access log, proxy log, and application log
   downstream of you.
2. The `httpx` logger is muted at WARNING. Not because the body-move isn't
   enough, but because one careless future change putting the token back in the
   query string would start the leak again silently. Don't trust that nobody
   makes that change.

`test_token_is_in_body_not_query` asserts this permanently.

**In screenshots.** Twice, while working through the Meta UI. Rotated both
times. Worth stating plainly: this is not a code problem, it is a habit problem.
`grep -o '^[A-Z_]*=' .env` shows the keys without the values, and that is the
only way `.env` should ever be inspected.

---

## Traps in the Meta UI (which cost more time than the code)

**Dataset creation fails silently without a Business Portfolio.** The
"Connect data source" modal simply vanishes on Next — no error, no message. The
cause: the dataset needs an owner, and an ad account outside any portfolio has
none. Creating a portfolio (`OSakun Dev`) fixed it instantly. Nothing in the UI
suggests this.

**The Test Events tab does not exist until the first event arrives.** Which
makes the bootstrap circular: you need `test_event_code` to test, and the tab
that gives you the code only appears after you have already sent something. The
resolution is to send the first event *without* the code, live, then collect the
code and switch. The client handles this: `test_event_code` is added to the
payload only if the setting is non-empty.

**Meta pushes third-party gateways hard.** "Birch — Recommended", "Set up with
Meta", "partner integration" are all offered before "Set up manually". Manual
direct integration is the last option in the list and the only one that hands
you a token.

**`SubscribedButtonClick` events appear that you never wrote.** These are
Automatic Advanced Matching — the Pixel attaches listeners to buttons and fires
events on its own. Harmless, but it is noise in the dataset, and it explains
events in Events Manager that have no counterpart in your code.

---

## The pydantic-settings trap

`Settings` read `.env` and returned an empty `test_event_code` while the file
plainly contained `META_TEST_EVENT_CODE=TEST23659`.

Cause: pydantic-settings gives **environment variables priority over the .env
file**. An earlier `set -a; source .env; set +a` (used once, to curl-check the
Graph API version) had exported the variable while it was still empty. The empty
export silently shadowed the file.

Twenty minutes lost. The symptom — events sent as LIVE instead of TEST, not
appearing in Test Events — pointed at Meta, not at the shell.

`unset` fixed it. The lesson is not "don't source .env", it is that a config
system with a precedence order will eventually surprise you, and the first thing
to check when config looks wrong is *what actually reached the process*, not
what is in the file.

---

## Other deliberate choices

**`event_time` is set by the server, never the client.** Meta rejects events
older than 7 days and events from the future. A wrong clock on a user's device
would silently kill their conversion.

**Client IP comes from `X-Forwarded-For`.** Behind a reverse proxy,
`request.client.host` is the proxy. A wrong IP is worse than no IP: it is a
confident identifier pointing at the wrong person, which drags match quality
down rather than leaving it neutral.

**A `Purchase` without `value`/`currency` is rejected with 422.** Meta would
accept it and optimise blindly. A purchase of unknown worth is a bug upstream,
and swallowing it here would hide that bug inside the ad spend.

**The worker never dies.** `run_forever` catches everything and continues. The
worst failure mode is a dead worker with a live collector: the queue grows,
events stop being delivered, nothing errors, and the data is wrong for days.

**Raw PII and hashed PII live in separate models.** `IncomingEvent` has no
`user_data` field — the browser cannot supply one. The server always rebuilds
`user_data` itself via `build_user_data()`. A client cannot smuggle in a
pre-computed hash or bypass normalisation.

---

## The accident that proved the point

On the very first run of the landing page, `ViewContent` arrived at Meta from
the **server only**. No browser copy.

Cause: `ViewContent` fires 400ms after load, and the Pixel had not finished
initialising. The `fbq` call was lost. The `fetch` to our collector was not,
because it does not depend on the Pixel.

This was not staged. It is the entire argument for server-side tracking,
demonstrated by accident on the first attempt: the browser event vanished — ad
blocker, slow pixel, ITP, cookie policy, the cause doesn't matter — and the
conversion survived anyway.

It was left in rather than fixed by raising the timeout, because it is more
honest and more instructive than a clean run would have been.

---

## Late additions

Four things landed after the bulk of this file was written.

### The Pixel ID was hard-coded in the HTML

`fbq('init', '2277246586437973')` sat in `static/index.html`.

A Pixel ID is not a secret — it is visible in the page source of every site that
runs one. But hard-coding it means anyone cloning the repo silently sends their
test events into *my* dataset: useless to them, noise for me. It is now injected
at serve time from `.env`, and the HTML carries `__META_PIXEL_ID__`.

The difference is between a demo you can look at and a demo you can run.

### .env.example must stay empty

Tempting to fill `.env.example` with working values so it "just works" on clone.
That reintroduces the same bug from the other direction — the template is what
someone copies into their own `.env`, so real values there send their traffic
into my dataset.

`.env` (real, gitignored) and `.env.example` (empty template, tracked) are
deliberately different files. The example carries only safe defaults: API
version, intervals, retry ceiling.

### .gitignore matched *.db but not .db.keep

A DB snapshot named `adtrack.db.keep` got committed. `*.db` does not match a
different suffix.

Nothing sensitive was in it — hashed test identifiers, no client data. But it is
a reminder that a `.gitignore` pattern protects the filenames you thought of, not
the ones you didn't. The check that actually works is reading `git status --short`
before every commit, not trusting the ignore file.

### Two workers drained the same queue, and nothing broke

During testing, a backgrounded uvicorn and a manually-run `drain_once()` were
both pulling from the queue at once. The retry counts came out lower than
expected, which looked like a bug.

It wasn't. Both processes were claiming from the same table, the attempt counter
is shared, and the ceiling still held. The event was delivered once. This was
never designed for — it was an accident — but it does demonstrate that the queue
tolerates more than one consumer, which is the property that makes the eventual
migration to a standalone worker process cheap.

### Meta's own Deduplicated label

The first dedup screenshot showed two events sharing an `event_id` and required
a caption explaining that Meta would merge them.

A later run produced something better: Meta groups the pair in the UI and tags
the server copy **`Deduplicated`** itself. The evidence stopped needing a
caption. Worth knowing that the label exists — it is the fastest way to confirm
dedup is actually working rather than assuming it from matching ids.

---

## What this is not

- Not batched. Deliberately (see above).
- Not multi-platform. TikTok and Google are the same shape — a client per
  destination behind the same queue — but adding them proves nothing new.
- Not a GTM replacement. There is no container, no tag manager, no client-side
  tag orchestration. This is the server-side half.
- Not production-hardened. No auth on `/collect`, no rate limiting, no
  horizontal scale. SQLite is the queue; at real volume that becomes Postgres
  or Redis, and the worker becomes its own process. The queue is the interface,
  so that migration touches nothing else.

The point was never to ship a tracking platform. It was to demonstrate that the
hard part of tracking is not sending the event — it is knowing, provably,
whether the event arrived.
