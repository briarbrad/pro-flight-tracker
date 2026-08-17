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
| **Auth** | None. No API key, no accounts, no session. Single-user personal app. |
| **CORS** | Wide open (`flask-cors` with default config) — any origin may call it. |
| **Content type** | Everything returns JSON. |
| **Methods** | `GET` for reads; `POST`/`DELETE` only on `/api/track`; `/api/check` accepts both `GET` and `POST`. |

Health check, useful as a connectivity probe:

```
GET /health
→ {"service":"pro-flight-tracker","status":"ok","version":"1.4","timestamp":"..."}
```

`/health` does **not** touch the database or any upstream API, so a 200 here
only means the web process is alive.

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
is how many survived the airport/flight filter. **`filtered_results` caps at 50**
— that's a hardcoded `--limit` default in the script with no query parameter
wired up to override it, so a value of exactly 50 means "at least 50," not 50.

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
| `GET /api/flight/status` | `flight` (req), `date` | 2 AeroAPI queries (status + route) |
| `GET /api/flight/chain` | `flight` (req), `date` | 2–3 AeroAPI queries. Inbound aircraft + turn time |
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
| `GET /api/ops/atfm` | `flight` (req), `date` | Eurocontrol regulation inference |

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

- `duration` is coerced to an integer and **clamped to 1–30**. Garbage falls
  back to the endpoint default rather than erroring.
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

See §6.

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

### Horizon bands

| Band | Hours out | What carries signal |
|---|---|---|
| `IMMINENT` | 0–2 | Everything live: surface, metering, RVR, lightning |
| `NEAR` | 2–6 | Active delay programs, equipment chain |
| `SAME_DAY` | 6–12 | Equipment chain, terminal forecast |
| `NEXT_DAY` | 12–24 | Forecast only. Programs will have expired |
| `DISTANT` | 24+ | Schedule and base rates |

### Response

```json
{
  "flight": "DL244",
  "horizon": {
    "hours_to_departure": 15.0,
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
included in `sources` / `llm_payload.facts`, same treatment as `sigmet` and
`gairmet` already got: raw findings for the model to reason about, not
deterministic verdict inputs. Only `taf_windows` escalates the verdict itself;
these two widen situational awareness without another escalation path to keep
calibrated. `isigmet` is gated the same way as in `/api/check` — only fetched
when the route leaves CONUS. `tcf` is always fetched once origin/dest are
known and within horizon; its `relevant[]` array is empty (not absent) when no
convective forecast area intersects the route.

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

### Cost

Cheaper than `/api/check`, and it scales down with distance:

| Horizon | AeroAPI queries | Sources consulted |
|---|---|---|
| 0–6h | 4 | 8–12 (adds `isigmet` on non-CONUS routes, `tcf` always) |
| 6–12h | 4 | 7–10 |
| 12h+ | **2** | 2 |

The equipment chain is skipped past 12h because the inbound aircraft isn't
reliably assigned yet — so you don't pay for it.

### Using `llm_payload`

Send it to whatever model you like. All arithmetic is already done; the model
only writes prose about numbers computed here.

```js
const brief = await fetch(`${BASE}/api/brief?flight=${flight}`).then(r => r.json());
const { system, user, facts } = brief.llm_payload;

const narrative = await callYourModel({
  system,
  user: user + JSON.stringify(facts, null, 2),
});
```

`system` embeds the full analytical framework plus five guardrails — chiefly
"every number must come from the facts provided" and "sources marked
not_consulted were deliberately excluded; do not speculate about them."

Render `verdict` and `branch_classification` directly from the JSON. Use the
model only for the narrative — that way the numbers on screen are always the
deterministic ones, even if the model output is slow or fails.

---

## 5. Cost and rate limits — these are real design constraints

**AeroAPI (FlightAware), Personal tier:**

- **$5 of free usage credit per month.** Credits do not roll over.
- **10 result sets per minute** rate limit.
- Only `/api/flight/*` and `/api/check` touch AeroAPI. Everything else —
  weather, ops, all SWIM feeds — is free.

Query cost per call:

| Call | AeroAPI queries |
|---|---|
| `/api/flight/status` | 2 |
| `/api/flight/chain` | 2–3 |
| `/api/flight/track` | 0–1 (only if ADS-B and OpenSky both miss) |
| `/api/check` | 3–4 total |
| Background tracker, per interval per flight | 2 |

Practical implications for anything you build:

- **Do not poll `/api/check` on a timer.** At 3–4 queries a call, a 30-second
  refresh loop would exhaust a month of credit in well under an hour.
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
- `interval_minutes` is **clamped to 5–240**. Values outside that range are
  silently adjusted, so read back what the response reports.
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
| `404` | `DELETE /api/track` on an untracked flight | `{"error": "Not tracking this flight"}` |
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
