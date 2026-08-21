# ✈️ Pro Flight Tracker API

A flight delay risk data engine that pulls from 17+ real-time aviation sources,
including direct FAA SWIM feeds — the same data pipeline airlines and air
traffic control run on.

Personal use. No accounts, no auth. A REST backend that aggregates flight,
weather, and FAA operational data and serves it as JSON.

> **Building a client?** Read **[RORK_BRIEF.md](RORK_BRIEF.md)** — it's the
> authoritative API contract: every endpoint, real response shapes, latency,
> cost limits, and known gaps.
>
> **Deploying or operating it?** Read **[SETUP.md](SETUP.md)**.

---

## Architecture

```
   Client (iOS app)
         │  HTTPS / JSON
         ▼
   Flask API  (app.py)          ← this repo, on Railway
         │  subprocess
         ▼
   ┌─────────────────────────────────────────────┐
   │ flight_data.py      AeroAPI, ADS-B, OpenSky │
   │ aviation_weather.py METAR/TAF/SIGMET/       │
   │                     ISIGMET/PIREP/FAA NAS   │
   │ airport_ops.py      G-AIRMET, TCF,          │
   │                     lightning, RVR, ATFM    │
   │ swim_consumer.py    8 FAA SWIM feeds        │
   │                     └→ Java JMS client      │
   └─────────────────────────────────────────────┘

   store.py  → Postgres (tracked flights, leader election)
```

Each script is a standalone CLI that prints JSON to stdout. Flask runs them as
subprocesses and returns their output over HTTP. The SWIM consumer is a Python
wrapper around the L3Harris JMS "jumpstart" client, which connects to FAA Solace
brokers over TLS, captures streaming XML for a few seconds, and parses it.

**Runtime requirements:** Python 3.12, **Java 25** (the jumpstart JAR is compiled
to class file 69 — Java 17 and 21 will not run it), and Postgres for persistent
flight tracking.

---

## Endpoints at a glance

Full reference with response shapes lives in [RORK_BRIEF.md](RORK_BRIEF.md).

| Group | Endpoints | Cost | Speed |
|---|---|---|---|
| Health | `/health` | free | instant |
| Flight | `/api/flight/status`, `/chain`, `/track` | **AeroAPI** | 1–5s |
| Weather | `/api/weather/metar`, `/taf`, `/sigmet`, `/isigmet`, `/pirep`, `/faa-status`, `/brief` | free | 1–3s |
| Airport ops | `/api/ops/gairmet`, `/tcf`, `/lightning`, `/rvr`, `/atfm` | free | 1–20s |
| FAA SWIM | `/api/swim/{tbfm,sfdps,itws,notams,stdds,tfms-flight,tfms-flow,tfdm}` | free | duration + ~4s |
| Aggregate | `/api/check` | **AeroAPI** | 30–60s |
| Analysis | `/api/brief` | **AeroAPI** (2–4) | 5–40s, scales with phase + horizon |
| Narrative | `/api/narrative` | OpenRouter Free Models Router (server-side) | 1–5s, cached briefly |
| Chat | `/api/chat` | OpenRouter Free Models Router (server-side) | 1–5s, never cached |
| Tracking | `/api/track` (POST/DELETE), `/api/tracked` | **AeroAPI** per interval, cadence adapts to phase | instant |

Only the AeroAPI-backed endpoints cost money. Weather, airport ops, and all
eight SWIM feeds are free to call as often as useful.

---

## Data sources

| Source | Auth | Script | Provides |
|---|---|---|---|
| AeroAPI (FlightAware) | API key | flight_data.py | Flight status, equipment chain, tail tracking |
| ADS-B Exchange | API key | flight_data.py | Real-time aircraft position |
| OpenSky Network | optional | flight_data.py | Fallback position |
| METAR / TAF | none | aviation_weather.py | Observations, terminal forecasts |
| SIGMET / PIREP | none | aviation_weather.py | Severe weather (CONUS), pilot reports |
| ISIGMET | none | aviation_weather.py | Severe weather outside CONUS (AK/HI/Pacific + international FIRs) |
| FAA NAS Status | none | aviation_weather.py | GDPs, ground stops, delay programs |
| G-AIRMET | none | airport_ops.py | Turbulence/icing forecast polygons |
| TCF | none | airport_ops.py | TFM Convective Forecast — thunderstorm coverage driving FAA ground stops/reroutes |
| Blitzortung | none | airport_ops.py | Live lightning (ramp closure risk) |
| FAA RVR | none | airport_ops.py | Per-runway visual range |
| SWIM TBFM | SWIM password | swim_consumer.py | ATC arrival metering |
| SWIM SFDPS | SWIM password | swim_consumer.py | NAS flight positions (FIXM) |
| SWIM ITWS | SWIM password | swim_consumer.py | Wind shear, gust fronts, microbursts |
| SWIM NOTAMs | SWIM password | swim_consumer.py | Runway closures, restrictions (AIXM) |
| SWIM STDDS | SWIM password | swim_consumer.py | Surface / TRACON tracks |
| SWIM TFMS | SWIM password | swim_consumer.py | NAS positions, EDCTs, GDP advisories |
| SWIM TFDM | SWIM password | swim_consumer.py | Tower surface management |

---

## File structure

```
pro-flight-tracker/
├── app.py                  Flask API — all endpoints, background tracker
├── store.py                Tracking store (Postgres / in-memory) + leader election
├── analysis.py             Flight phase, horizon gating, Branch A/B,
│                           taxi/position analysis, LLM prompt payload
├── Dockerfile              Python 3.12 + Java 25. Copies files BY NAME —
│                             a new top-level module must be added here
├── Procfile                gunicorn process definition
├── railway.json            Railway build/deploy config
├── requirements.txt        Python dependencies
├── README.md               This file
├── RORK_BRIEF.md           API contract for client developers
├── SETUP.md                Deployment, env vars, troubleshooting
├── scripts/
│   ├── flight_data.py      AeroAPI + ADS-B + OpenSky
│   ├── aviation_weather.py METAR/TAF/SIGMET/PIREP/FAA status
│   ├── airport_ops.py      G-AIRMET/lightning/RVR/ATFM
│   └── swim_consumer.py    All 8 SWIM feeds via JMS
├── swim/
│   ├── config.json         SWIM queue/broker configuration
│   ├── bin/run             Launches the Java JAR
│   ├── lib/                jumpstart-jar-with-dependencies.jar (~10 MB)
│   └── src/                Java source, for reference
└── references/             Analytical framework, report template, source docs
```

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AEROAPI_KEY` | yes | FlightAware AeroAPI v4 |
| `SWIM_PASSWORD` | yes | FAA SWIM broker password (all feeds share one) |
| `DATABASE_URL` | recommended | Postgres. Without it, tracking falls back to in-memory and dies on redeploy |
| `ADSB_EXCHANGE_KEY` | optional | Real-time position |
| `OPENSKY_API_KEY` | optional | Fallback position, `username:password` |
| `WEATHER_USER_AGENT` | optional | UA string for weather APIs |
| `DISABLE_TRACKER` | optional | Set `1` to stop background polling without a code deploy |
| `DB_POOL_MAX_SIZE` | optional | Max pooled Postgres connections (default `10`) |
| `ALLOWED_ORIGINS` | optional | Comma-separated browser origins allowed to call the API cross-origin. Empty (default) allows none. Purely a browser-side CORS control — it has no effect on the native iOS client, which never sends an `Origin` header |
| `OPENROUTER_API_KEY` | required for `/api/narrative` and `/api/chat` | Server-side secret shared by both AI features. Create a key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) and set it here — never in the iOS app bundle. Without it, both endpoints return `501`; `/api/narrative`'s client falls back to the deterministic verdict with no prose narrative, and the chat button should hide/disable itself client-side (see `RORK_BRIEF.md`) |
| `NARRATIVE_CACHE_TTL_SECONDS` | optional | How long `/api/narrative` caches an identical (system, user, facts) response before calling OpenRouter again (default `180`, `0` disables caching). `/api/chat` is never cached — every question is different |
| `API_TOKEN` / `REQUIRE_AUTH` | optional | Bearer-token auth, dormant until `REQUIRE_AUTH=1` is set (see `app.py`'s auth section for the rollout sequence). `API.swift` already sends `Config.EXPO_PUBLIC_BACKEND_API_TOKEN` as a bearer token when configured, so enabling this needs no new iOS code — only matching values on both sides |

SWIM usernames, queue names, and broker assignments live in `swim/config.json`,
not in env vars.

---

## Known issues

**SWIM results cap at 50** with no query parameter to raise it — `--limit` is
hardcoded in `swim_consumer.py`.

**Response envelopes are inconsistent** across endpoint families. See
[RORK_BRIEF.md §2](RORK_BRIEF.md).

---

## Version history

> **Note:** `/health` currently reports `"version": "1.8"` in some older deploys, but this list and
> RORK_BRIEF.md had drifted to v1.3/v1.5 respectively, and v1.4–v1.7 were
> never logged here. Rather than guess at unlogged history, this entry only
> documents the fix made in this pass — please backfill v1.4–v1.7 from
> memory/commit history if it matters, and bump `app.py`'s health-check
> version string on every future change so this list and `/health` can't
> drift again.

- **v1.9** (current) — Backend hardening pass:
  - Circuit breaker now inspects the response body, not just HTTP status —
    a `200` that's actually a bare `{"error": ...}` with no usable content
    (a real upstream failure some provider scripts can return) used to read
    as success and even get cached as good data. Fixed both.
  - Background tracker's polling cadence is now phase/horizon-aware instead
    of fixed at track-creation time — a flight a day out polls roughly
    hourly-to-6-hourly instead of every 15 minutes; one taxiing out tightens
    to every 5.
  - Added Postgres connection pooling (`psycopg_pool`) — every store call
    used to open and close a brand new connection, including from the
    tracker's own per-flight loop.
  - `CORS(app)` (wide open) replaced with an explicit `ALLOWED_ORIGINS`
    allowlist, default-deny. This is a browser-only control with zero effect
    on the native iOS client.
  - Added `/api/narrative`, an optional server-side proxy for the AI
    narrative call, meant to move the toolkit secret out of the app bundle.
    **Not wired up**: Rork's toolkit credential is platform-managed (not
    exposed for copying in Rork's Secrets UI on our plan), so the shipped
    client keeps calling Rork's hosted AI toolkit directly, as originally
    built. The endpoint is dormant (`501` until configured) and harmless to
    leave in place if we revisit this later with a standalone provider key.
- **v1.8** — Fixed `/api/check` reading origin/destination from
  the wrong envelope shape (it looked for a top-level `flights` key with
  nested `code_icao` fields; the real shape is `data.flights[i].origin_icao`
  / `dest_icao`), which had been silently skipping every airport-dependent
  phase-2 task (METAR, TAF, FAA NAS, RVR, ops feeds) for every check. Also
  renamed `swim-daemon.py` → `swim_daemon.py` (the code does `import
  swim_daemon`, which cannot resolve a hyphenated filename) and added it to
  the Dockerfile's `COPY` list, so the persistent SWIM daemon can actually
  start in production instead of silently failing every boot.
- **v1.3** — TFMS + TFDM SWIM feeds (GDP advisories, NAS positions, surface management)
- **v1.2** — FAA SWIM integration (TBFM, SFDPS, ITWS, NOTAMs, STDDS)
- **v1.1** — G-AIRMET, lightning, RVR, ATFM inference
- **v1.0** — Core flight data, weather, delay analysis
