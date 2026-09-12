# Pro Flight Tracker — Backend Brief for Rork

**Read this to understand how to get data out of the backend. Nothing more.**

This document deliberately contains **no UI, screen, layout, component, color, or
information-architecture guidance**. How the app looks and behaves is an open
design question the developer will work through with you directly. If you find
yourself inferring a screen structure from this document, stop — that decision
hasn't been made yet, and any earlier version of the docs that prescribed one
should be treated as void.

What this document *does* cover: the base URL, every endpoint, the exact shape
of what comes back, how long calls take, what they cost, and where the sharp
edges are.

---

## 1. The basics

| | |
|---|---|
| **Base URL** | `https://pro-flight-tracker-production.up.railway.app` |
| **Auth** | Dormant by default. Send `Authorization: Bearer <token>` matching `API_TOKEN` when set. Enforced only if `REQUIRE_AUTH=1` (otherwise unauthenticated requests are logged and still served). `/health` and CORS preflights are always exempt. A per-worker rate cap (`RATE_LIMIT_PER_MIN`, default 60) is always on and returns `429`. |
| **CORS** | Allowlist via `ALLOWED_ORIGINS` (comma-separated). Empty default = no browser origin. Native iOS is unaffected (no `Origin` header). |
| **Content type** | Everything returns JSON. |
| **Methods** | `GET` for reads; `POST`/`DELETE` only on `/api/track`; `/api/check` accepts both `GET` and `POST`. |

Health check, useful as a connectivity probe:

```
GET /health
→ {"service":"pro-flight-tracker","status":"ok","version":"1.13","timestamp":"...",
   "store":{...},"tracker_leader":true,"cache_entries":N,"breakers":{...},"swim_daemon":{...}}
```

`/health` **does** touch the database (it calls `store.health_check()` and
reports the result under `"store"`) but does **not** call any external
aviation upstream (AeroAPI, ADS-B, FAA feeds, etc.). A 200 here means the web
process is alive and the store connection works — it does not prove the
background tracker is making progress, that the SWIM daemon's JVM is
connected, or that any upstream source is reachable. (Corrected from a
previous version of this doc that said `/health` touches neither the
database nor upstreams — that was inaccurate.)

---

## 2. Response envelopes — read this before parsing anything

There is **no single global envelope**. The backend wraps four different
families of scripts, and each family returns its own shape. This is the most
common source of parsing bugs. Do not assume a uniform `{data: ...}` wrapper.

### Envelope A — weather endpoints (`/api/weather/*`)

```json
{
  "pull_time": "2026-08-16T18:28:11.128672+00:00",
  "command": "metar",
  "data": { ... },
  "errors": []
}
```

`data` is **keyed by ICAO code** for `metar`, `taf`, and `faa-status`. It is an
**array** for `sigmet`, `isigmet`, and `pirep` (those also add a top-level `count`).

### Envelope B — flight data endpoints (`/api/flight/*`)

```json
{
  "pull_time": "...",
  "source": "aeroapi",
  "command": "status",
  "flight": "DL244",
  "date": "2026-08-16",
  "data": { "flights": [ ... ], "route": { ... } },
  "errors": []
}
```

Note the double nesting: the flight array is at `data.flights`, **not** at the
top level.

### Envelope C — SWIM endpoints (`/api/swim/*`)

```json
{
  "feed": "tfms-flight",
  "query": { "airport": null, "flight": null, "duration_seconds": 12 },
  "timestamp": "2026-08-16T17:34:50.688949+00:00",
  "total_raw_messages": 37,
  "filtered_results": 34,
  "results": [ ... ]
}
```

`total_raw_messages` is how many messages came off the broker; `filtered_results`
is how many survived the airport/flight filter. **`filtered_results` defaults
to 50.** Pass `limit=` (integer, clamped 1–200) to raise or lower it. A value
equal to the limit you sent (or 50 if you sent none) means "at least that
many," not an exact count.

### Envelope D — ops endpoints (`/api/ops/*`)

Inconsistent, and mostly **flat with no envelope at all**. `/api/ops/lightning`
returns its fields at the top level:

```json
{
  "airport": "KJFK",
  "airport_coords": {"lat": 40.6413, "lon": -73.7781},
  "search_radius_nm": 20,
  "collection_duration_sec": 3,
  "total_strikes": 0,
  "strikes_within_5nm": 0,
  "strikes": [],
  "ramp_closure_risk": "NONE",
  "activity_level": "NONE",
  "risk_emoji": "🟢",
  "source": "Blitzortung",
  "note": "Lightning within 5 NM triggers ramp closure / ground stop...",
  "timestamp": "2026-08-16T18:29:21Z"
}
```

Treat each `/api/ops/*` endpoint's shape as its own thing and check it
empirically before relying on it.

---

## 3. Endpoint catalog

### Flight data — **these cost money**, see §5

| Endpoint | Query params | Notes |
|---|---|---|
| `GET /api/flight/status` | `flight` (req), `date` | 1 AeroAPI query (filed route is opt-in and this endpoint does not buy it) |
| `GET /api/flight/chain` | `flight` (req), `date` | 1–2 AeroAPI queries (inbound + optional position). Status is fetched here only when this endpoint is called standalone |
| `GET /api/flight/track` | `reg` or `flight` | Tries ADS-B/OpenSky first (free), falls back to AeroAPI |

### Weather — free, fast

| Endpoint | Query params | Notes |
|---|---|---|
| `GET /api/weather/metar` | `icao` (comma-separated) | Current observations, keyed by airport |
| `GET /api/weather/taf` | `icao` (comma-separated) | Terminal forecasts, keyed by airport |
| `GET /api/weather/sigmet` | `type` | Array + `count`. Severe weather areas with polygons — **CONUS only** |
| `GET /api/weather/isigmet` | `hazard` (`turb`\|`ice`) | Array + `count`. Same as `sigmet` but for everywhere `sigmet` doesn't cover: Alaska, Hawaii/Pacific, and every non-US FIR |
| `GET /api/weather/pirep` | `icao` (req), `distance` (default `200`, clamped 1–500) | Array + `count` |
| `GET /api/weather/faa-status` | `icao` (comma-separated) | GDPs / ground stops, keyed by airport |
| `GET /api/weather/brief` | `origin`, `dest` | Combined route briefing |
| `GET /api/weather/open-meteo` | `icao` (comma-separated), `hours` (1–48, default 12) | Open-Meteo **model guidance** (no API key). Precip probability, wind gusts, visibility-ish. **Not a TAF** — never invents VFR/IFR. See §4d |

**Airport codes: send either form.** 3-letter (`JFK`) and 4-letter ICAO
(`KJFK`) both work on every endpoint that takes an airport, including the
non-CONUS cases where they don't line up (`HNL`↔`PHNL`, `ANC`↔`PANC`,
`SJU`↔`TJSJ`). The backend converts to whatever each upstream feed needs.

**Response keys echo what you sent.** Ask for `icao=JFK` and the data comes
back under `data.JFK`; ask for `KJFK` and you get `data.KJFK`. When a
conversion happened, a top-level `resolved` map shows it
(`{"JFK": "KJFK"}`). You never have to guess which form the response used.

### Airport ops — free

| Endpoint | Query params | Notes |
|---|---|---|
| `GET /api/ops/gairmet` | `route` (comma-separated), `hazard` | Turbulence/icing forecast polygons |
| `GET /api/ops/tcf` | `route` (comma-separated) | TFM Convective Forecast — thunderstorm coverage/confidence 2-6h out, the product FAA traffic management actually uses to call ground stops/reroutes. Without `route`, dumps every active polygon nationwide |
| `GET /api/ops/lightning` | `icao` (req), `radius` (default `20`), `duration` (default `10`) | **Blocks for `duration` seconds** — it's a live WebSocket capture |
| `GET /api/ops/rvr` | `airport` (req; `icao` also accepted) | Either code form works |
| `GET /api/ops/atfm` | `flight` (req), `date` | Eurocontrol regulation inference (**costs AeroAPI** — prefer the heuristic already on `/api/brief` / `/api/flight/live`) |
| `GET /api/ops/flow-brief` | `flight`, `date`, `origin`, `dest` (flexible; at least one of flight/origin/dest), `duration` (4–12, default 8) | Interpreted ATC flow: TFMS-flow + TBFM + TFDM in parallel. See §4c |

### FAA SWIM — free (subscription-based), but slow

Every SWIM call opens a live TLS JMS connection to an FAA Solace broker, listens
for `duration` seconds, then disconnects. **Wall-clock time ≈ `duration` + ~3-5s**
of JVM startup and handshake.

| Endpoint | Query params | Default duration | Notes |
|---|---|---|---|
| `GET /api/swim/tbfm` | `airport`, `flight`, `duration` | 12 | Arrival metering / ATC sequencing |
| `GET /api/swim/sfdps` | `airport`, `flight`, `duration` | 10 | Flight positions (FIXM) |
| `GET /api/swim/itws` | `airport` (**req**), `duration` | 12 | Terminal weather: wind shear, gust fronts, microbursts |
| `GET /api/swim/notams` | `airport` (**req**), `duration` | 18 | NOTAMs (AIXM) |
| `GET /api/swim/stdds` | `airport` (**req**), `duration` | 10 | Surface / TRACON tracks |
| `GET /api/swim/tfms-flight` | `airport`, `flight`, `duration` | 14 | NAS-authoritative positions, ETAs, EDCTs |
| `GET /api/swim/tfms-flow` | `airport`, `keyword`, `duration` | 15 | GDP advisories, ground stops, flow restrictions |
| `GET /api/swim/tfdm` | `airport`, `flight`, `duration` | 14 | Surface management: pushback, taxi, queue wait |

Parameter handling notes that apply to all SWIM endpoints:

- `duration` is coerced to an integer and **clamped to 1–20**. Garbage falls
  back to the endpoint default rather than erroring. (The subprocess timeout
  is 45s; JVM startup + TLS + JMS teardown eat ~10–15s outside `--duration`,
  so a user-supplied `duration=30` used to 504.)
- `limit` is coerced to an integer and **clamped to 1–200** (default 50).
- `airport` / `flight` / `keyword` are uppercased and stripped of stray quotes.
  Non-alphanumeric values are discarded. A value starting with `-` is rejected.
- Endpoints marked **req** return `400` with a `hint` field if `airport` is
  missing or unusable.

Volume warning: `tfdm` and `stdds` are firehoses — TFDM alone returned **4,059
messages in 3 seconds** unfiltered. Always pass `airport` to those two. Note
that **JFK and LGA are not in TFDM yet**; `KEWR` is the live New York airport
for that feed.

### Tracking

| Endpoint | Method | Params |
|---|---|---|
| `/api/track` | `POST` | JSON body: `flight` (req), `push_token` (req), `date`, `interval_minutes` |
| `/api/track` | `DELETE` | Query: `flight`, `date` |
| `/api/tracked` | `GET` | — |

`interval_minutes` on `POST /api/track` only sets the *starting* cadence.
The background tracker re-derives it every check from the flight's current
phase and horizon (see §6) — it tightens automatically near departure/taxi
and loosens automatically while a flight is still hours out, so don't expect
the interval you requested to stay fixed for the life of the tracked flight.

See §6.

---

### Narrative

| Endpoint | Method | Params |
|---|---|---|
| `/api/narrative` | `POST` | JSON body: `system`, `user`, `facts` — pass `llm_payload` from `/api/brief` straight through |

See §4b “Using `llm_payload`” for the full contract. **Note:** this endpoint
is now live — the server holds a standalone `OPENROUTER_API_KEY` and calls
OpenRouter's Free Models Router (`openrouter/free`, $0/token) on the
client's behalf. `NarrativeService.swift` should call this endpoint instead
of any AI provider directly: send it the same `{system, user, facts}` shape
`/api/brief`'s `llm_payload` already gives you, unmodified. No provider
secret should ever ship inside the compiled app again.

### Chat

| Endpoint | Method | Params |
|---|---|---|
| `/api/chat` | `POST` | JSON body: `flight` (str), `date` (str), `facts` (the same `llm_payload.facts` object `/api/brief` already gives you), `messages` (array of `{role: "user"\|"assistant", content: str}`, ending on a `user` message) |

Interactive counterpart to Narrative — lets the traveller ask free-form
follow-up questions about one flight ("why is there a weather delay when
the skies are clear", "what if the inbound flight is delayed further",
"do you think the delay increases") instead of only reading one
self-contained synthesis. Same upstream (`openrouter/free`, server-side
`OPENROUTER_API_KEY`, same empty-content-from-a-reasoning-model retry
mitigation as Narrative), same "no provider secret in the app bundle" rule.

The endpoint is **stateless** — it holds no conversation memory between
requests. The client owns the conversation: keep the running `messages`
array in memory for that chat session (SwiftUI `@State`/`@Observable` is
enough, no persistence needed — the thread doesn't need to survive an app
restart) and resend the whole array, appending the new user question, on
every turn. `facts` should be `/api/brief`'s `llm_payload.facts` for that
flight, sent unmodified every time — the server rebuilds the system prompt
fresh each request so it's always current if the flight's facts changed
since the chat opened.

Response: `{"reply": str}`. Errors follow the same shape as Narrative: `501`
if `OPENROUTER_API_KEY` isn't set, `400` for a malformed body (missing
`flight`/`date`/`facts`, empty/missing `messages`, a message with a bad
`role` or empty `content`, or a `messages` array not ending on `user`), and
otherwise OpenRouter's own status code passed through (`429` = rate
limited, etc.) or `502` if the narrative still comes back empty after the
fallback-model retry.

Server-side bounds worth knowing (in `app.py`, constants prefixed `CHAT_`):
only the most recent `CHAT_MAX_MESSAGES` (20) messages are actually sent
upstream even if the client keeps a longer local history; each message is
truncated to `CHAT_MAX_MESSAGE_CHARS` (4000) chars server-side. `facts` is
NOT size-limited — it embeds the same raw source payloads (METAR, TAF, FAA
status, SWIM feeds) as `/api/narrative` already sends uncapped, and those
routinely exceed tens of KB for a real flight, so imposing a cap here would
just reject legitimate questions (this shipped once and broke chat entirely
until it was removed). The per-conversation message caps exist because chat
can rack up far more free-tier calls per flight than one narrative ever
would, against the same shared 50–1000/day `openrouter/free` quota — see §5.

---

## 4. `/api/check` — the aggregate endpoint

```
GET  /api/check?flight=DL244&date=2026-08-16
POST /api/check   body: {"flight":"DL244","date":"2026-08-16"}
```

This is the expensive, comprehensive one. It runs in two phases: it fetches
flight status first (to learn origin/destination), then fans out ~13-14 more
sources in parallel using those airports.

**Response shape:**

```json
{
  "flight": "DL244",
  "date": "2026-08-16",
  "timestamp": "...",
  "origin_icao": "KJFK",
  "destination_icao": "LICC",
  "data": {
    "flight_status":    { ...Envelope B... },
    "equipment_chain":  { ...Envelope B... },
    "metar":            { ...Envelope A... },
    "taf":              { ...Envelope A... },
    "faa_status":       { ...Envelope A... },
    "pirep_origin":     { ...Envelope A... },
    "sigmet":           { ...Envelope A... },
    "isigmet":          { ...Envelope A... },
    "gairmet":          { ...Envelope D... },
    "tcf":              { ...Envelope D... },
    "rvr_origin":       { ...Envelope D... },
    "lightning_origin": { ...Envelope D... },
    "tbfm":             { ...Envelope C... },
    "itws_origin":      { ...Envelope C... },
    "tfms_flight":      { ...Envelope C... },
    "tfms_flow_gdp":    { ...Envelope C... },
    "atfm":             { ...Envelope D... }
  }
}
```

**Critical:** each value under `data` retains **its own script's envelope**.
There is no normalization pass. `data.metar` is Envelope A, `data.tbfm` is
Envelope C, `data.lightning_origin` is Envelope D. Parse each accordingly.

**Keys are not guaranteed present.** If phase 1 can't determine origin and
destination — bad flight number, AeroAPI failure, a flight not in the system —
the entire phase-2 airport-dependent block is skipped, and `metar`, `taf`,
`faa_status`, `tbfm`, `itws_origin`, `rvr_origin`, `lightning_origin`, `gairmet`,
and `tcf` simply won't exist in the response. Always check for presence.

**`isigmet` is conditional even when phase 2 runs.** It only appears when
either airport is outside the contiguous US — `sigmet` already covers CONUS,
so `isigmet` is skipped as redundant on an all-CONUS route (e.g. JFK–LAX).
It shows up on anything touching Alaska, Hawaii, or an international airport
(e.g. JFK–FCO).

**Per-source failures are inlined, not fatal.** If one source fails, its key
holds `{"error": ...}` while everything else succeeds. The HTTP status is still
`200`. Check for an `error` key inside each section.

**Latency: 30–60 seconds.** This is not a request you can hang a synchronous
spinner on without thought. It serially waits for AeroAPI, then runs a parallel
fan-out whose slowest members are the SWIM feeds. Budget accordingly; consider
whether you want the aggregate at all versus calling the individual endpoints
you actually need.

---

## 4b. `/api/brief` — horizon-gated analysis + LLM prompt payload

```
GET  /api/brief?flight=DL244&date=2026-08-16
POST /api/brief   body: {"flight":"DL244","date":"2026-08-16"}
```

Use this instead of `/api/check` when you want a *judgement* rather than a
data dump. It resolves how far away the departure is, consults only the
sources that still carry signal at that horizon, runs the analysis
deterministically, and hands back a ready-to-send LLM prompt.

**Why it matters:** an FAA ground delay program happening right now tells you
essentially nothing about a flight leaving in 15 hours — those programs are
same-day and tied to a specific window. Feeding live conditions to a model for
a distant departure produces confident, wrong answers. This endpoint excludes
them explicitly and says so.

### Flight phase — read this before `horizon`

`phase` is where the aircraft physically is right now, derived from which
`actual_*` milestones have been filed. It is the primary state for the UI:
everything else, including the horizon band, is computed relative to it.

| `phase` | Meaning | `next_event` |
|---|---|---|
| `PRE_GATE` | Still at the gate | `gate_departure` |
| `TAXI_OUT` | Left the gate, has not taken off | `takeoff` |
| `AIRBORNE` | In the air | `landing` |
| `TAXI_IN` | Landed, not yet at a gate | `gate_arrival` |
| `ARRIVED` | At the destination gate | — |
| `CANCELLED` | Cancelled | — |

```json
"phase": {
  "phase": "TAXI_OUT",
  "phase_label": "Taxiing out",
  "phase_detail": "Left the gate, has not taken off",
  "since": "2026-08-16T23:20:00+00:00",
  "elapsed_in_phase_min": 100,
  "is_terminal": false,
  "diverted": false,
  "next_event": "takeoff",
  "next_event_label": "Takeoff",
  "next_event_time": "2026-08-17T02:28:00+00:00",
  "next_event_local_display": "10:28 PM EDT",
  "next_event_basis": "airline/FAA estimate",
  "next_event_status": "ESTIMATED",
  "next_event_overdue": false,
  "minutes_to_next_event": 88
}
```

`next_event` names the key in `predicted_times` that describes what happens
next, so `predicted_times[brief.phase.next_event]` is always the time to lead
with. `next_event_*` mirrors that entry — including `basis` and `status`, so
`CONTROLLED` (an FAA-assigned time) still reads differently from `ESTIMATED`.

`next_event_overdue: true` means the predicted time has passed and the
milestone still hasn't happened — a wheels-up estimate that came and went
while the aircraft is still on the taxiway. That is a live, worsening state,
not a completed one.

### Horizon bands

The band is now driven by **time to `next_event`**, not time to gate
departure. A flight 40 minutes from wheels-up is `IMMINENT` whether it's
sitting at the gate or has been on a taxiway for an hour.

| Band | Hours to next event | What carries signal |
|---|---|---|
| `IMMINENT` | 0–2 (and anything overdue) | Everything live: surface, metering, RVR, lightning |
| `NEAR` | 2–6 | Active delay programs, equipment chain |
| `SAME_DAY` | 6–12 | Equipment chain, terminal forecast |
| `NEXT_DAY` | 12–24 | Forecast only. Programs will have expired |
| `DISTANT` | 24+ | Schedule and base rates |
| `ARRIVED` | — | Flight complete |
| `CANCELLED` | — | Nothing to assess |

`horizon` carries both clocks. `hours_to_departure` keeps its original
meaning — hours to *gate* departure, and it goes **negative** once the
aircraft pushes back. `hours_to_next_event` is the one that drives gating.

> **Breaking-ish change:** the band `DEPARTED` is no longer emitted. It used
> to appear the moment `actual_out` was set, which meant a flight holding on
> a taxiway was reported as finished and every live source was switched off.
> That state is now `phase: "TAXI_OUT"` with a normal live band. If you have
> a `case "DEPARTED"` branch, it should become a `phase` check.

### Response

```json
{
  "flight": "DL244",
  "phase": { "...": "see above" },
  "taxi": { "...": "see below" },
  "position": { "...": "see below" },
  "refresh_after_seconds": 300,
  "horizon": {
    "hours_to_departure": 15.0,
    "hours_to_next_event": 15.0,
    "gating_basis": "Pushback (before pushback)",
    "phase": "PRE_GATE",
    "band": "NEXT_DAY",
    "band_guidance": "Forecast-only regime...",
    "reference_basis": "scheduled"
  },
  "verdict": {
    "departure_risk": "LOW",
    "confidence": "LOW",
    "confidence_basis": "Horizon band NEXT_DAY — ...",
    "drivers": ["..."]
  },
  "simple_summary": {
    "headline": "Too early for a firm call — nothing worrying yet",
    "what_i_think": "We're about 15 hours from departure. ...",
    "confidence": "LOW",
    "risk": "LOW",
    "next_event_label": "Pushback",
    "next_event_local_display": "7:41 PM EDT",
    "basis_bullets": ["Too early to judge", "Nothing worrying yet"]
  },
  "status": {
    "code": "UNKNOWN",
    "label": "Too early to call",
    "phase": "PRE_GATE"
  },
  "impactMinutes": null,
  "causes": [],
  "outlook": {
    "applicable": true,
    "riskLevel": "LOW",
    "confidence": "LOW",
    "headline": "Low delay risk on the forecast",
    "causes": [
      {"label": "Nothing worrying on the forecast",
       "why": "No thunderstorms, low visibility, or winter weather...",
       "severity": "INFO", "source": "taf"}
    ]
  },
  "branch_classification": {
    "branch": "A" | "B" | "NOT_APPLICABLE" | "UNDETERMINED",
    "branch_label": "Transient — weather-driven, expected to clear",
    "evidence": ["..."],
    "active_program_count": 0
  },
  "sources_consulted": ["flight_status", "taf"],
  "sources_excluded": {
    "faa_status": "Departure is 15.0h out; this source stops carrying signal beyond 6.0h"
  },
  "sources": { "...": "full payload per source" },
  "llm_payload": { "system": "...", "user": "...", "facts": {...}, "guardrails": [...] },
  "aeroapi_queries_used": 2
}
```


### `effects` and `predicted_times` (added)

The brief response now carries two more deterministic blocks, both also
included in `llm_payload.facts`:

**`effects[]`** — every finding rephrased as its effect on THIS flight:

```json
{
  "cause": "Ground delay program at KJFK, avg delay 2h07m",
  "effect": "A GDP meters flights ARRIVING INTO the origin — it does not assign delays to this departure...",
  "severity": "INFO",          // INFO | WATCH | ACTION
  "source": "faa_status"
}
```

Severity semantics: `ACTION` will move the flight or needs the user's
attention (EDCT assigned, turn below minimum, ground stop at destination);
`WATCH` could move it; `INFO` is context. The origin-vs-destination logic is
encoded here — a GDP at the departure airport is INFO for a departure, ACTION
territory only for flights arriving there.

**`source` values you will see** (v1.13): `faa_status`, `swim_tfms`,
`equipment_chain`, `taf`, `taxi`, `position`, **`gairmet`**, **`atfm`**.
`gairmet` is emitted only when `/api/brief` already consulted the G-AIRMET
script (horizon ≤12h, not taxi-in) and `relevant[]` is non-empty. `atfm`
is the Eurocontrol CTOT heuristic run **in-process on the status payload
already paid for** — no extra AeroAPI query. It appears on `/api/brief`
and `/api/flight/live` when the destination is in Eurocontrol airspace
and the horizon is ≤12h. `NO_INDICATION` / non-European dest emit nothing.

A matching top-level `atfm` object is included on brief and live:

```json
"atfm": {
  "applicable": true,
  "destination": "EGLL",
  "in_eurocontrol": true,
  "verdict": "PROBABLE",
  "confidence_pct": 70,
  "delay_min": 30,
  "indicators": [ { "type": "...", "detail": "...", "weight": 30 } ],
  "note": "..."
}
```

`applicable: false` (US-domestic dest) is the common case — the key is
always present so you can check the flag, not key presence.

**`predicted_times`** — gate departure, takeoff, gate arrival, each with:

```json
{
  "time": "2026-08-16T23:41:00Z",
  "status": "CONTROLLED",       // ACTUAL | CONTROLLED | ESTIMATED | DERIVED | SCHEDULED | UNKNOWN
  "basis": "FAA-assigned EDCT (controlled wheels-up, -5/+5 min window)",
  "delay_vs_schedule_min": 16
}
```

plus `uncertainty_minutes` (widens with horizon: ±10 IMMINENT, ±20 NEAR,
±45 SAME_DAY, ±90 NEXT_DAY, null DISTANT) and `edct` (the raw FAA
assignment with `as_of` and `assigned_via`, or null — null is the normal
case; only flights captured by a traffic management program get one).

`CONTROLLED` means an FAA-assigned time — treat it as authoritative over any
airline estimate. EDCTs are fetched via SWIM only within ~6h of departure.


**Local times.** Each prediction also carries `local_display` ("10:02 AM EDT"),
`time_local`, `utc_display`, and `timezone`. Gate and takeoff use the origin's
zone; arrival uses the destination's. `timezone: ""` means unresolved — the
display falls back to Zulu rather than guessing an offset. A top-level
`timezones: {origin, destination}` is included too.

**`taf_windows`** assesses the terminal forecast across the ±60 min around the
predicted departure and arrival, with `prevailing_category` (VFR/MVFR/IFR/LIFR
from FM groups) separated from `worst_conditional_category` (TEMPO/PROB). Only
prevailing IFR/LIFR, thunderstorms, or freezing precipitation escalate the
verdict; MVFR, gusts, shear and TEMPO groups surface as WATCH/INFO without
moving it. This is what makes a 12h+ flight assessable at all — beyond ~6h the
TAF is the only source still in play.

**`isigmet` and `tcf` (added)** — both consulted within a 6h horizon and
included in `sources` / `llm_payload.facts`. `gairmet` now *also* contributes
deterministic `effects[]` (`source: "gairmet"`) when `relevant[]` is
non-empty — SEV/EXTM turbulence is WATCH, not ACTION, because a G-AIRMET
is not a ground-delay mechanism. Only `taf_windows` escalates the verdict
itself. `isigmet` is gated the same way as in `/api/check` — only fetched
when the route leaves CONUS. `tcf` is always fetched once origin/dest are
known and within horizon; its `relevant[]` array is empty (not absent) when no
convective forecast area intersects the route.

**`extended_weather` (v1.11)** — present on `/api/brief` when the horizon
band is `SAME_DAY`, `NEXT_DAY`, or `DISTANT`. Compact Open-Meteo model
guidance for origin/dest (precip probability, gusts, visibility in metres).
`null` on nearer bands where TAF/METAR still cover the window. **Never
an aviation flight category.** Same object lives in `llm_payload.facts`
with a guardrail telling the model not to override a TAF.

```json
"extended_weather": {
  "label": "model_guidance",
  "source": "open-meteo",
  "note": "Numerical weather model guidance via Open-Meteo...",
  "airports": {
    "KJFK": {
      "icao": "KJFK",
      "label": "model_guidance",
      "current": { "temp_c": 18.2, "wind_gust_kts": 22.0, "visibility_m": 16000 },
      "next_6h": {
        "max_precip_probability_pct": 80,
        "max_wind_gust_kts": 28.0,
        "min_visibility_m": 4000
      },
      "hourly": [ { "time": "2026-09-12T15:00", "precip_probability_pct": 40 } ]
    }
  }
}
```

### `taxi` — is this wait abnormal?

Present on every response; `applicable: false` unless the phase is
`TAXI_OUT` or `TAXI_IN`.

```json
"taxi": {
  "applicable": true,
  "phase": "TAXI_OUT",
  "airport": "KJFK",
  "elapsed_min": 100,
  "typical_min": 30,
  "predicted_total_min": 188,
  "excess_vs_typical_min": 158,
  "assessment": "EXTENDED",
  "summary": "100 min into taxi-out at KJFK against a typical 30 min, and the predicted total is 188 min — roughly 158 min beyond normal."
}
```

`assessment` is `NORMAL` / `ELEVATED` / `EXTENDED` / `UNKNOWN`, judged against
a **per-airport** baseline — 30 minutes is a routine JFK taxi and would be
alarming at DCA. `predicted_total_min` is measured to predicted wheels-up, so
it keeps growing as the estimate slips; `elapsed_min` alone understates a
hold that isn't over yet.

`summary` is written to be rendered verbatim. An `EXTENDED` taxi-out
escalates `verdict.departure_risk` to at least `MODERATE` and appears in
`drivers`. Taxi-*in* deliberately does not escalate — the flight has landed,
so departure risk no longer describes anything — but it still appears in
`effects[]` because it affects connection timing.

### `position` — where it is and whether it's moving

Fetched once the aircraft is out of the gate (never `PRE_GATE`, where the
airframe is still operating someone else's flight). ADS-B and OpenSky are
tried first and are free; AeroAPI is the fallback.

```json
"position": {
  "available": true,
  "movement": "STOPPED",
  "movement_label": "Stopped on the ground",
  "latitude": 40.6398, "longitude": -73.7789,
  "groundspeed_kts": 0, "altitude_ft": 0, "heading": 132,
  "on_ground": true,
  "source": "adsb_exchange",
  "observed_at": "2026-08-17T01:00:00Z",
  "note": "Holding — the aircraft is stationary on the airport surface, typically in a departure queue or a penalty box waiting on a release."
}
```

`movement` is `STOPPED` / `TAXIING` / `TAKEOFF_ROLL` / `AIRBORNE` /
`ON_GROUND` / `UNKNOWN`. This is the distinction a lat/lon pair can't make on
its own: parked in a queue and rolling toward the runway look identical on a
map and feel completely different to a passenger. `TAKEOFF_ROLL` means
wheels-up is seconds away.

`available: false` with a `note` means the aircraft isn't reporting a
position right now. Surface ADS-B coverage is patchy at some airports — treat
it as missing data, not as a problem with the flight.

### `refresh_after_seconds`

Seconds after which this brief should be considered stale — 300 during a
taxi, 900 airborne, up to 21600 for a distant departure, and `null` once the
flight is finished.

**This is a staleness threshold, not a polling interval.** `/api/brief` costs
AeroAPI queries (§5); use it to decide when to show a refresh affordance or
mark the brief as aged, not to re-run on a timer. A brief run before pushback
is worthless 20 minutes into a taxi hold, which is what this exists to catch.

### Reading the verdict

**`confidence` matters as much as `departure_risk`.** At `NEXT_DAY` or
`DISTANT`, confidence is `LOW` by construction — that's the honest answer, not
a data failure. `LOW` risk at `LOW` confidence means "nothing is visibly wrong
yet," not "this flight is fine."

**`branch`** is the delay mechanism, from `references/analytical-framework.md`:

- `A` — transient, weather-driven, expected to clear. Normally absorbed en
  route via miles-in-trail spacing rather than held at the gate. An airport's
  average delay is *not* this flight's expected delay.
- `B` — structural. Equipment out of position, non-weather cause (staffing,
  volume, runway). Cascades forward regardless of weather improvement.
- `NOT_APPLICABLE` — too far out for any mechanism to be assessable.

### `simple_summary` (v1.12, unchanged in v1.13) — Simple mode, same product

A deterministic, traveler-facing prediction block on **`/api/brief` and
`/api/flight/live`**. Built in Python from fields already on the response
(`verdict`, `effects`, `predicted_times`, `phase`, `taxi`,
`branch_classification`, `horizon`). **No extra AeroAPI query, no LLM.**
Pro fields are unchanged — Simple mode reads this object; Pro mode can
ignore it.

```json
"simple_summary": {
  "headline": "Likely 15–25 min late leaving JFK; still expect an on-time-ish arrival.",
  "what_i_think": "Based on the FAA takeoff slot and the inbound aircraft, the departure will wait for a specific takeoff time rather than the published schedule. There's usually time to make up some of that in the air.",
  "confidence": "MEDIUM",
  "risk": "MODERATE",
  "next_event_label": "Takeoff",
  "next_event_local_display": "7:41 PM EDT",
  "basis_bullets": ["FAA takeoff slot assigned", "Inbound plane running tight"]
}
```

| Field | What it is |
|---|---|
| `headline` | One clear prediction. Render this first. |
| `what_i_think` | One or two calm sentences of why. Jargon is expanded (`EDCT` → "FAA takeoff slot"). |
| `confidence` | Same vocabulary as `verdict.confidence`: `LOW` / `MEDIUM` / `HIGH`. |
| `risk` | Same vocabulary as `verdict.departure_risk`: `LOW` / `MODERATE` / `HIGH`. An assigned takeoff slot or a turn below minimum can lift this to `MODERATE` even if the coarse verdict stayed `LOW`. |
| `next_event_label` / `next_event_local_display` | Copied from `phase` (`null` once the flight is cancelled or has no next event). |
| `basis_bullets` | 1–4 short reasons, already traveler-safe. |

**Horizon honesty.** When `horizon.band` is `NEXT_DAY` or `DISTANT`, or
`branch` is `NOT_APPLICABLE`, and nothing ACTION-level is in play, the
headline is *"Too early for a firm call — nothing worrying yet"* — not a
fake-green "looking on time." A far-out TAF ACTION still says it's too
early for a clock time, but names the weather as something to watch.

**Closure.** Cancelled and arrived flights get a past-tense summary
("This flight has been cancelled." / "This flight has arrived — on time.").

The same object is copied into `llm_payload.facts.simple_summary` on
`/api/brief` so a narrative can quote it. Simple mode does not need that
call — render this JSON as-is.

`/api/flight/live` includes the same shape. Its verdict is still
`scope: "status_only"`, so the summary can only speak to what status (plus
any cached EDCT / turn) already knows. Use `/api/brief` when Simple mode
wants the full prediction.

### `status`, `impactMinutes`, `causes[]`, `outlook` (v1.13)

A thin presentation layer on **`/api/brief` and `/api/flight/live`**. Built
in Python from fields already on the response — `effects[]`, `verdict`,
`predicted_times`, `phase`, `taxi`, EDCT, plus (brief only) `taf_windows`,
`extended_weather`, G-AIRMET, TCF, SIGMET/ISIGMET. **No extra AeroAPI
query, no LLM.** `simple_summary` stays the hero line; these blocks are
the story the flight screen should tell instead of dumping feeds.

**Client rule (one rule, stick to it):**

| Show | When |
|---|---|
| `outlook` | `outlook.applicable === true` (far-out forecast) |
| `status` + `impactMinutes` + `causes[]` | live / near — and always render `status` when a brief exists |
| Both in the JSON | always, when a brief/live payload exists. They coexist. |

`outlook` is **always present**. `applicable` is `true` only when **all** of:

1. This is `/api/brief` (forecast sources were consulted). `/api/flight/live` is status-only and **always** returns `"outlook": {"applicable": false}` — do not treat a far-out live tile as a forecast.
2. `phase.phase` is `PRE_GATE` (not taxiing, airborne, arrived, or cancelled).
3. `horizon.band` is `NEXT_DAY` or `DISTANT`, **or** `branch_classification.branch` is `NOT_APPLICABLE`.

Same-day / near / imminent / taxi / airborne / arrived keep live `status`
in front. When `applicable` is false the object is just
`{"applicable": false}` — no headline, no fake risk.

```json
"status": {
  "code": "DELAYED",
  "label": "Delayed 42m",
  "phase": "PRE_GATE"
},
"impactMinutes": 42,
"causes": [
  {
    "label": "GDP at JFK",
    "why": "Arrival metering into the airport — may push your wheels-up",
    "severity": "WATCH",
    "source": "faa_status"
  }
]
```

| Field | What it is |
|---|---|
| `status.code` | `DELAYED` / `ON_TIME` / `EARLY` / `CANCELLED` / `ARRIVED` / `DIVERTED` / `UNKNOWN`. Far-out horizons are `UNKNOWN` ("Too early to call") — never a fake `ON_TIME`. |
| `status.label` | Chip text: `"Delayed 42m"`, `"On time"`, `"Cancelled"`, `"Arrived on time"`, `"Too early to call"`. |
| `status.phase` | Mirror of `phase.phase`. |
| `impactMinutes` | Signed minutes vs schedule (takeoff/gate-out, or arrival once airborne). `null` when unknown, cancelled, or too early for a real reading. Positive = late. |
| `causes[]` | Operational story, ordered ACTION → WATCH → INFO (same spirit as `effects[]`). `{label, why, severity, source}`. Reassuring INFO (VFR, "equipment is not a constraint") is omitted. When outlook is applicable, forecast-source rows (`taf`, `gairmet`, `tcf`, `sigmet`, `isigmet`, `extended_weather`) are **not** repeated here — they live on `outlook.causes` so they are not presented as a live delay. |

```json
"outlook": {
  "applicable": true,
  "riskLevel": "LOW" | "MODERATE" | "HIGH",
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "headline": "Elevated delay risk · weather",
  "causes": [
    {"label": "Thunderstorms around departure",
     "why": "Thunderstorms in this window are the usual trigger for ground stops...",
     "severity": "ACTION",
     "source": "taf"}
  ]
}
```

Outlook **must feel like a forecast**, not a fake live delay. It extrapolates
tomorrow from TAF windows, TCF, G-AIRMET, Open-Meteo `extended_weather`,
and SIGMET/ISIGMET when those were already on the brief. Confidence is
`LOW` at `DISTANT` and when the TAF does not cover the window; `MEDIUM`
is the ceiling at `NEXT_DAY` with a covering TAF — outlook is never a
high-confidence clock time.

`source` values on `causes` / `outlook.causes` (v1.13): the `effects[]`
set (`faa_status`, `swim_tfms`, `equipment_chain`, `taf`, `taxi`,
`position`, `gairmet`, `atfm`) plus `tcf`, `sigmet`, `isigmet`,
`extended_weather`.

The same four keys are copied into `llm_payload.facts` on `/api/brief`.

### Cost

Cheaper than `/api/check`, and it scales down with distance:

| Phase / horizon | AeroAPI queries | Sources consulted |
|---|---|---|
| `PRE_GATE`, 0–6h | 4 | 8–12 (adds `isigmet` on non-CONUS routes, `tcf` always; `atfm` in-process if dest is European) |
| `PRE_GATE`, 6–12h | 4 | 7–11 (`open_meteo` + `atfm` heuristic; still 4 AeroAPI) |
| `PRE_GATE`, 12h+ | **2** | 3–4 (`taf` + `open_meteo` model guidance) |
| `TAXI_OUT` | **2** (3 if ADS-B misses) | 10–13 |
| `AIRBORNE` | **2** (3 if ADS-B misses) | 3–5 |
| `TAXI_IN` | **2** | 2 |
| `ARRIVED` / `CANCELLED` | **2** | 1 |

The equipment chain is skipped past 12h because the inbound aircraft isn't
reliably assigned yet — so you don't pay for it. It's also skipped in every
phase from `TAXI_OUT` onward: the aircraft is already out, so the turn it
describes is finished and buying its history is pure waste. That's why a
taxiing flight costs 2 queries while consulting *more* live sources than a
pre-departure one.

The `position` lookup adds one query only when ADS-B **and** OpenSky both
miss; it reuses the already-paid flight status, so the fallback costs one
query rather than two.

### Using `llm_payload` — currently sent straight to Rork's AI toolkit

All arithmetic is already done in `llm_payload`; a model only writes prose
about numbers computed here. The shipped client sends it directly to Rork's
hosted AI toolkit (as originally built) — **do not** switch this to the
backend's `/api/narrative` endpoint described below; that endpoint exists
but is not configured server-side (see note above) and would just return
`501` for every call.

**Not used today, kept for reference if this is revisited later:** if
`/api/narrative` is ever wired up server-side, the call would look like
this —

```js
const brief = await fetch(`${BASE}/api/brief?flight=${flight}`).then(r => r.json());
const { system, user, facts } = brief.llm_payload;

const { narrative } = await fetch(`${BASE}/api/narrative`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ system, user, facts }),
}).then(r => r.json());
```

`POST /api/narrative` — body `{system, user, facts}` (exactly the shape of
`llm_payload`, sent back unmodified) — returns `{"narrative": string,
"cached": bool}` on success. `cached: true` means an identical
(system, user, facts) tuple was answered recently and no new model call was
made — expect this often when several clients poll the same tracked flight
within a few minutes of each other.

**Background:** `/api/narrative` was built to close a credential-leak
concern — the app's original approach embedded Rork's toolkit secret key in
the compiled bundle, recoverable by decompiling the IPA or watching the
device's own traffic. That's now fixed: the server holds a standalone
OpenRouter key (`OPENROUTER_API_KEY`, set on Railway, never in the app),
calls OpenRouter's Free Models Router (`openrouter/free`) on the client's
behalf, and returns just the finished narrative text. `NarrativeService.
swift` should call this endpoint exclusively — no provider secret of any
kind belongs in the iOS bundle going forward. `openrouter/free` was chosen
over OpenRouter's task-aware `auto`/`auto-beta` routers specifically
because it never bills per token (auto/auto-beta pass through the routed
model's standard rate instead); the tradeoff is a lower quality ceiling and
a shared daily rate limit (50 req/day, 1000/day once the account has $10+
in purchased credits) across every caller of this one server-side key.

**Known failure mode:** `openrouter/free`'s random pool includes reasoning
models (e.g. DeepSeek R1 free). A reasoning model can spend its entire
`max_tokens` budget on internal "thinking" tokens and never write anything
to the visible `content` field — the call still succeeds and shows up in
OpenRouter's Activity log, but the endpoint has nothing to return. We cap
reasoning spend via `reasoning.max_tokens` on the first attempt, and if
`content` still comes back empty, retry once against a pinned non-reasoning
free model (`NARRATIVE_FALLBACK_MODEL` in `app.py`) instead of re-rolling
the same random risk. Worst case this costs 2 free-tier requests per
narrative instead of 1, so budget for that against the 50-1000/day shared
quota above.

`system` embeds the full analytical framework plus five guardrails — chiefly
"every number must come from the facts provided" and "sources marked
not_consulted were deliberately excluded; do not speculate about them."

Render `verdict` and `branch_classification` directly from the JSON. Use the
narrative only for prose — that way the numbers on screen are always the
deterministic ones, even if the narrative call is slow or fails.

---

## 4c. `/api/ops/flow-brief` — interpreted ATC flow (v1.11)

```
GET /api/ops/flow-brief?flight=DL244&date=2026-09-12&origin=KJFK&dest=EGLL
GET /api/ops/flow-brief?origin=JFK&dest=LHR
GET /api/ops/flow-brief?flight=DL244          # resolves airports via 1 AeroAPI status
```

Highest-priority nerd endpoint. Fans out **in parallel** to the existing
SWIM scripts with a capped listen (`duration` 4–12s, default 8) and a
hard request deadline (~duration + 16s). The iOS client should render
this JSON as-is — do not re-derive meaning from raw Envelope C.

| Feed | What we ask | When skipped (still 200) |
|---|---|---|
| `tfms-flow` | origin and dest keywords / GDP / MIT / TMI | Airport is not US NAS (`K*`) |
| `tbfm` | dest arrival metering, filtered by callsign when `flight` is given | Dest is not US NAS |
| `tfdm` | origin (and dest if deployed) taxi queue / earliest wheels-up | Airport is not in the TFDM set — **JFK and LGA are not**. Empty is success |

Quiet feeds = empty arrays / null fields, **never HTTP 500**. A missing
`SWIM_PASSWORD`, a timeout, or an undeployed airport all look like
`sources_quiet`.

**Params** (all optional except at least one of `flight` / `origin` / `dest`):

| Param | Notes |
|---|---|
| `flight` | Used for TBFM/TFDM callsign match. If origin/dest omitted, costs **1 AeroAPI** status lookup |
| `date` | YYYY-MM-DD; only needed when resolving airports from `flight` |
| `origin` / `dest` | ICAO or IATA. `LHR` and `KLHR` both become `EGLL` |
| `departure` / `destination` / `arrival` | Aliases for origin/dest |
| `duration` | SWIM listen seconds, clamped 4–12, default 8 |

**Response shape:**

```json
{
  "flight": "DL244",
  "date": "2026-09-12",
  "origin": "KJFK",
  "dest": "EGLL",
  "generated_at": "2026-09-12T14:02:01+00:00",
  "duration_seconds": 8,
  "advisories": [
    {
      "title": "GDP FOR EWR",
      "text": "GROUND DELAY PROGRAM AT EWR DUE TO WEATHER",
      "severity": "WATCH",
      "airport": "KEWR",
      "source": "tfms-flow",
      "effective_start": "2026-09-12T12:00:00Z",
      "effective_end": "2026-09-12T20:00:00Z",
      "kind": "advisory"
    }
  ],
  "metering": {
    "applicable": false,
    "airport": "EGLL",
    "items": [],
    "count": 0,
    "note": "EGLL is outside the US NAS — TBFM arrival metering is not published for this destination."
  },
  "surface": {
    "applicable": false,
    "airport": "KJFK",
    "queue_wait_min": null,
    "estimated_taxi_out_min": null,
    "earliest_wheels_up": null,
    "state": null,
    "note": "KJFK is not in the TFDM deployment set (JFK/LGA are not live; KEWR is the NY-area airport). Empty is success."
  },
  "surface_dest": null,
  "effects": [
    {
      "cause": "GDP FOR EWR",
      "effect": "A ground delay program meters arrivals into the named airport...",
      "severity": "WATCH",
      "source": "tfms-flow"
    }
  ],
  "sources_tried": ["tfms-flow:origin"],
  "sources_quiet": ["tfms-flow:origin", "tbfm", "tfdm:origin"],
  "sources_skipped": ["tfms-flow:dest", "tbfm", "tfdm:origin", "tfdm:dest"],
  "timings": { "total": 9.4, "tfms_flow_origin": 9.4 },
  "aeroapi_queries_used": 0,
  "note": "SWIM captures are short live listens. Empty arrays mean the feed was quiet or not deployed — not a server error."
}
```

`effects[]` uses the same `{cause, effect, severity, source}` vocabulary
as `/api/brief`. `metering.items[]` is `{flight_id, fix, eta, status,
dest_airport, dep_airport, this_flight}`.

**Cost:** $0 SWIM when origin/dest are passed. 1 AeroAPI query only if
you omit airports and pass `flight`. Do **not** poll this on the live
tile refresh — it is a same-day deep dive, not a 30-second loop.

`/api/brief` does **not** add extra TBFM/TFDM SWIM calls (those are
already slow and were left on this dedicated endpoint). The brief still
runs `tfms-flow` / `tfms-flight` when the horizon plan already includes
them.

---

## 4d. `/api/weather/open-meteo` — model guidance (v1.11)

```
GET /api/weather/open-meteo?icao=KJFK
GET /api/weather/open-meteo?icao=JFK,LHR&hours=18
```

No API key. Envelope A (`pull_time`, `command`, `data` keyed by the
codes you sent, plus `label` / `note`). Each station block:

```json
{
  "icao": "KJFK",
  "label": "model_guidance",
  "source": "open-meteo",
  "coord_source": "airport_table",
  "coords": { "lat": 40.6413, "lon": -73.7781 },
  "current": {
    "time": "2026-09-12T14:00",
    "temp_c": 18.2,
    "precip_mm": 0.0,
    "weather_code": 3,
    "wind_kts": 12.0,
    "wind_gust_kts": 22.0,
    "visibility_m": 16000
  },
  "hourly": [ { "time": "...", "precip_probability_pct": 40,
                "wind_gust_kts": 24.0, "visibility_m": 8000 } ],
  "next_6h": {
    "max_precip_probability_pct": 80,
    "max_wind_gust_kts": 28.0,
    "min_visibility_m": 4000
  },
  "note": "Numerical weather model guidance via Open-Meteo..."
}
```

There is **no** `flight_category` / VFR / IFR field and there never will
be. When a TAF covers the same window, the TAF wins. Folded into
`/api/brief` as `extended_weather` at `SAME_DAY`+ horizons only — not
on `/api/flight/live`.

---

## 5. Cost and rate limits — these are real design constraints

**AeroAPI (FlightAware), Personal tier:**

- **$5 of free usage credit per month.** Credits do not roll over.
- **10 result sets per minute** rate limit.
- Only `/api/flight/*`, `/api/brief`, `/api/check`, and the background
  tracker touch AeroAPI. Everything else — weather, ops, all SWIM feeds —
  is free.

Query cost per call:

| Call | AeroAPI queries |
|---|---|
| `/api/flight/status` | 1 |
| `/api/flight/chain` | 1–2 (inbound + optional position fallback) |
| `/api/flight/track` | 0–1 (only if ADS-B and OpenSky both miss) |
| `/api/flight/live` | 1 |
| `/api/brief` | 1–3 (status + inbound if equipment-chain is relevant + optional position) |
| `/api/check` | 2–3 total (status is reused by chain) |
| Background tracker, per interval per flight | 1 typical; +1 inbound when a cold equipment-chain lookup runs |

Practical implications for anything you build:

- **Do not poll `/api/check` on a timer.** At 2–3 queries a call, a 30-second
  refresh loop would exhaust a month of credit in well under an hour.
  Use `/api/flight/live` (1 query) for the main refresh.
- **Do not fire concurrent flight lookups.** Two simultaneous `/api/check` calls
  can breach 10 queries/minute and start returning 429s.
- Weather and SWIM are free — refresh those as often as is useful without
  worrying about cost. Only the AeroAPI-backed calls need rationing.
- Any "refresh" affordance should be user-initiated rather than automatic.

---

## 6. Flight tracking lifecycle

Tracking is server-side. A background worker polls tracked flights and pushes a
notification via the Expo Push API when the computed risk level *changes*.

**Start tracking:**

```
POST /api/track
Content-Type: application/json

{
  "flight": "DL244",
  "date": "2026-08-16",
  "push_token": "ExponentPushToken[...]",
  "interval_minutes": 15
}
```

```json
{
  "status": "tracking",
  "track_id": "DL244_2026-08-16",
  "interval_minutes": 15,
  "expires_at": "2026-08-18T06:12:49.483224+00:00",
  "message": "..."
}
```

- `track_id` is always `"{flight}_{date}"`. Re-POSTing the same pair **updates**
  the existing record rather than creating a duplicate.
- `interval_minutes` is **clamped to 5–240** at creation time. Values outside
  that range are silently adjusted, so read back what the response reports.
- **This is only the starting cadence.** After the first check, the tracker
  recomputes the interval itself on every pass from the flight's current
  phase/horizon (the same bands `refresh_after_seconds` in `/api/brief` uses)
  and can widen it up to 360 minutes for a flight still far out, or tighten
  it down to 5 minutes once it's taxiing — independent of what was requested
  at creation. `GET /api/tracked` always reflects the current, possibly
  auto-adjusted value.
- `push_token` is required by the endpoint but never validated. A malformed
  token fails silently at notification time.

**List tracked:**

```
GET /api/tracked
```

```json
{
  "count": 1,
  "store_backend": "postgres",
  "tracker_on_this_worker": false,
  "tracked": [{
    "track_id": "DL244_2026-08-16",
    "flight": "DL244",
    "date": "2026-08-16",
    "push_token": "ExponentPushToken[...]",
    "interval_minutes": 5,
    "last_check": "2026-08-16T18:23:28.347417+00:00",
    "last_risk": "LOW",
    "created_at": "2026-08-16T18:12:49.483224+00:00",
    "expires_at": "2026-08-18T06:12:49.483224+00:00"
  }]
}
```

`last_check` is `null` until the worker's first pass (up to ~60s after the POST).
`store_backend` and `tracker_on_this_worker` are operational diagnostics, not
app data — `tracker_on_this_worker` is `false` on any request that lands on a
non-leader worker even when the tracker is perfectly healthy, so don't surface
it as a status signal.

**Stop tracking:**

```
DELETE /api/track?flight=DL244&date=2026-08-16
→ 200 {"status":"stopped","track_id":"..."}
→ 404 {"error":"Not tracking this flight"}
```

**Automatic removal.** A flight is untracked on its own once AeroAPI reports it
cancelled, diverted, arrived, or carrying an `actual_in` time — and
unconditionally at `expires_at` (36 hours after creation). The client does not
need to clean up after landing, and should not assume a track it created will
still exist later.

**Notification semantics.** A push fires on any *transition* between risk
levels, and also on the very first check if the flight is already at
`MODERATE` or `HIGH` when tracking begins. A first check that comes back `LOW`
stays silent. Payload:

```json
{
  "title": "🟡 DL244 Risk: MODERATE",
  "body": "↑ Elevated from LOW. Tap to see details.",
  "data": {"flight":"DL244","date":"2026-08-16","risk":"MODERATE"}
}
```

---

## 7. Errors

| Status | Meaning | Body |
|---|---|---|
| `200` | Success — **may still contain per-source `error` keys** | varies |
| `400` | Missing/invalid required parameter | `{"error": "...", "hint": "..."}` |
| `401` | `REQUIRE_AUTH=1` and bearer token missing/wrong | `{"error": "Unauthorized", "hint": "..."}` |
| `404` | `DELETE /api/track` on an untracked flight | `{"error": "Not tracking this flight"}` |
| `429` | Per-worker rate cap exceeded | `{"error": "Rate limit exceeded", "limit_per_minute": N, "retry_after_seconds": N}` |
| `500` | Script failed | `{"error": "...", "detail": "...", "returncode": N}` |
| `504` | Script exceeded its timeout | `{"error": "Script timed out after Ns"}` |

A `500` from a SWIM endpoint carries a `detail` field with the underlying JVM or
broker error, and sometimes a `hint`. Surface `detail` when debugging.

**An empty SWIM result is not an error.** `total_raw_messages: 0` with no
`error` key means the connection succeeded and the feed was simply quiet —
normal for NOTAMs and `tfms-flow`, which are event-driven and can be silent for
long stretches. Only treat a response as failed if it actually carries `error`.

---

## 8. What the backend does *not* do

Be aware of these before deciding what the client is responsible for.

**`/api/check` returns no interpretation.** It hands back raw data from ~17
sources — no score, no verdict, no summary. If you want a judgement, use
`/api/brief` (§4b), which does the analysis deterministically and returns a
verdict, a delay-mechanism classification, and an LLM prompt payload. There is a `risk_emoji` and
`ramp_closure_risk` field inside the lightning payload specifically, but that's
a single source's own assessment, not a flight-level one.

**`last_risk` on a tracked flight is the one exception**, and it applies only
to tracking — not to `/api/check`. It is a deliberately coarse `LOW` /
`MODERATE` / `HIGH` used solely to decide whether to send a push. It escalates
on:

| Signal | Level |
|---|---|
| Flight cancelled or diverted | `HIGH` |
| Active FAA ground stop at origin or destination | `HIGH` |
| Departure slipped ≥ 45 min vs schedule | `HIGH` |
| TFMS ground stop advisory | `HIGH` |
| Active Ground Delay Program | `MODERATE` |
| Arrival/departure delays or runway closures | `MODERATE` |
| Departure slipped 15–44 min | `MODERATE` |
| TFMS GDP issuance | `MODERATE` |

Program *cancellation* advisories are correctly ignored (a GDP ending is not a
new risk). Don't mistake this for a flight-level assessment suitable for
display — it reads only three sources and is tuned to avoid false alarms, not
to be comprehensive.

**No caching.** Every call hits upstream live. Two identical `/api/check` calls
a second apart cost twice.

**No pagination anywhere.** SWIM results silently cap at 50.

**No websockets or streaming.** Everything is request/response. Live-feeling
updates require polling, subject to the cost constraints in §5.

**No authentication.** The URL is the only secret. Don't build anything that
assumes per-user separation.

---

## 9. Quick reference — useful field paths

Once you know the envelope, these are the paths worth knowing:

```
# Flight status (Envelope B)
data.flights[0].ident                  "DL244"
data.flights[0].status                 "En Route" / "Scheduled" / "Cancelled"
data.flights[0].registration           "N1604R"
data.flights[0].aircraft_type          "B763"
data.flights[0].origin_icao            "KJFK"
data.flights[0].dest_icao              "LICC"
data.flights[0].scheduled_out          ISO 8601
data.flights[0].estimated_out          ISO 8601
data.flights[0].actual_out / _off / _on / _in
data.flights[0].gate_origin            "B44"
data.flights[0].progress_percent       0-100
data.flights[0].cancelled              bool
data.flights[0].diverted               bool
data.flights[0].inbound_fa_flight_id   feeds the equipment chain

# METAR (Envelope A, keyed by ICAO)
data.KJFK.raw                          full METAR string
data.KJFK.flight_category              "VFR" / "MVFR" / "IFR" / "LIFR"
data.KJFK.temperature_c
data.KJFK.dewpoint_c
data.KJFK.visibility_sm                "10+" (string, may not be numeric)
data.KJFK.ceiling_ft
data.KJFK.wind.speed_kts / .gust_kts / .direction_deg
data.KJFK.clouds[].coverage / .base_agl_ft

# FAA status (Envelope A, keyed by ICAO)
data.KJFK.status                       "NO_ACTIVE_DELAYS" or active
data.KJFK.ground_stops[]
data.KJFK.ground_delay_programs[]
data.KJFK.arrival_departure_delays[]
data.KJFK.closures[]

# SIGMET (Envelope A, array + count) — CONUS only
data[].hazard                          "CONVECTIVE" / "TURB" / "ICE"
data[].severity                        numeric
data[].area_coords[]                   {lat, lon} polygon
data[].valid_from / .valid_to
data[].altitude_low_ft / .altitude_hi_ft
data[].movement.direction_deg / .speed_kts

# ISIGMET (Envelope A, array + count) — Alaska/Hawaii/Pacific + non-US FIRs
data[].hazard                          "TURB" / "ICE"
data[].fir_id / .fir_name              e.g. "PAZA" / "ANCHORAGE"
data[].base_ft / .top_ft
data[].area_coords[]                   {lat, lon} polygon
data[].valid_from / .valid_to
data[].movement.direction / .speed_kts

# G-AIRMET (Envelope D, only when queried with ?route=)
relevant[].hazard / .severity          "TURB-HI" etc / "MOD" | "SEV" | "EXTM"
relevant[].base_ft / .top_ft
relevant[].near_origin / .near_dest / .along_route   bool
risk_level                             "NONE" | "LOW" | "MODERATE" | "HIGH"

# TCF (Envelope D, only when queried with ?route=)
relevant[].coverage                    "sparse" | "medium"
relevant[].confidence                  forecaster confidence in the coverage call
relevant[].tops_hundreds_ft            echo top category
relevant[].valid_time / .issue_time
relevant[].near_origin / .near_dest / .along_route   bool
risk_level                             "NONE" | "LOW" | "MODERATE"

# TFMS flight (Envelope C)
results[].flight_id                    "AAL1724"
results[].msg_type                     "trackInformation" / "departureInformation" /
                                       "flightPlanAmendmentInformation"
results[].latitude / .longitude
results[].altitude_ft
results[].speed_kts
results[].eta.time / .type             "ESTIMATED" / "ACTUAL"
results[].etd.time / .type
results[].arr_airport / .dep_airport
results[].route

# TFDM surface (Envelope C)
results[].flight_state                 "SCHEDULED" / "PUSHBACK" / "AIRBORNE"
results[].taxi_out_minutes
results[].queue_wait_minutes           surface congestion signal
results[].runway_departure_estimated
results[].runway_assigned

# TFMS flow advisories (Envelope C)
results[].type                         "tfms_advisory" / "tfms_restriction" /
                                       "tfms_tmi_flight"
results[].title / .text                advisory prose (GDP/GS issuances)
results[].effective_start / .effective_end
```

---

## 10. Things that will bite you

1. **Envelope inconsistency** (§2) — the single biggest source of parsing bugs.
2. **`/api/check` keys can be absent**, not just empty, when origin/dest lookup
   fails.
3. **`/api/ops/rvr`'s primary param is `airport`**, though `icao` is accepted
   as an alias.
4. **SWIM calls block for `duration` seconds.** They are not fast reads.
5. **`filtered_results: 50` means "capped,"** not "exactly 50."
6. **`last_risk` is tracking-only** — it never appears in `/api/check`, and
   it's a coarse push-notification trigger, not a display-ready verdict (§8).
7. **`visibility_sm` can be a string** like `"10+"`, not a number.
8. **Empty SWIM results are normal**, not failures.
9. **AeroAPI credit is small and non-rolling** — every design decision that
   touches `/api/flight/*` or `/api/check` should account for it.
10. **`isigmet` only appears on routes leaving CONUS.** Don't treat its
    absence as an error — on a JFK–LAX check it's correctly never fetched
    because `sigmet` already has that airspace covered.
11. **`tcf`/`gairmet`'s `relevant[]` can be legitimately empty.** No
    convective/turbulence polygons intersecting the route is the common case,
    not a fetch failure — check `risk_level`/`error`, not just array length.
12. **`horizon.hours_to_departure` goes negative after pushback** and stays
    negative for the rest of the flight. Gate on `phase` or
    `hours_to_next_event` instead; a negative number here is not an error.
13. **The band `DEPARTED` no longer exists.** A pushed-back flight is
    `phase: "TAXI_OUT"` with a live band. Any branch keyed on `DEPARTED`
    will silently stop matching.
14. **`equipment_chain` is absent from `TAXI_OUT` onward** — it appears in
    `sources_excluded`, not as an error. The turn it describes already
    happened.
15. **`taxi.applicable` and `position.available` are both false much of the
    time** and the keys are always present. Check the flag, not the key.
16. **`/api/ops/flow-brief` empty arrays are success.** TFDM is not at
    JFK/LGA; TBFM/TFMS are US NAS only. A London arrival will have
    `metering.applicable: false` and that is correct.
17. **`extended_weather` / `/api/weather/open-meteo` is not a TAF.** No
    VFR/IFR field is emitted. Do not invent one client-side from visibility
    metres or weather codes — that would contradict an official TAF.
18. **`GET /api/ops/atfm` still costs AeroAPI.** The same heuristic is
    already on `/api/brief` and `/api/flight/live` for free. Prefer those.
19. **`outlook.applicable` is the only switch.** Do not infer a forecast
    from a far-out `/api/flight/live` tile — that endpoint never consults
    TAF/Open-Meteo and always returns `applicable: false`. Far-out
    `status.code` is `UNKNOWN`, not `ON_TIME`.
