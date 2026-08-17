# Pro Flight Tracker — Railway Setup Guide

## Prerequisites

1. A [Railway](https://railway.app) account (Hobby plan, $5/month — includes $5 usage credits)
2. A [GitHub](https://github.com) account
3. API keys (see below)

---

## Step 1: Create GitHub Repository

```bash
# Clone or download this directory, then:
cd pro-flight-tracker
git init
git add .
git commit -m "Initial commit — Pro Flight Tracker v1.3"
git remote add origin https://github.com/YOUR_USERNAME/pro-flight-tracker.git
git push -u origin main
```

**Important:** The `swim/lib/jumpstart-jar-with-dependencies.jar` file (~10 MB) must be included. Consider using [Git LFS](https://git-lfs.github.com/) if your repo gets large:
```bash
git lfs install
git lfs track "*.jar"
git add .gitattributes
```

---

## Step 2: Deploy to Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub Repo**
2. Select your `pro-flight-tracker` repository
3. Railway will auto-detect the Dockerfile and start building
4. Once deployed, go to **Settings** → **Networking** → **Generate Domain** to get your public URL

---

## Step 3: Set Environment Variables

In Railway dashboard → your service → **Variables** tab, add these:

### Required Variables

| Variable | Description | How to Get |
|---|---|---|
| `AEROAPI_KEY` | FlightAware AeroAPI v4 key | [flightaware.com/aeroapi](https://www.flightaware.com/aeroapi/) — Personal tier is ~$1/query |
| `SWIM_PASSWORD` | FAA SWIM broker password | From your SWIFT Portal subscription (same password for all feeds) |

### Optional Variables (Enhanced Features)

| Variable | Description | How to Get |
|---|---|---|
| `ADSB_EXCHANGE_KEY` | ADS-B Exchange RapidAPI key | [rapidapi.com/adsbexchange](https://rapidapi.com/adsbexchange/api/adsbexchange-com1) — Free tier available |
| `OPENSKY_API_KEY` | OpenSky Network credentials | [opensky-network.org](https://opensky-network.org/) — Free account, format: `username:password` |
| `WEATHER_USER_AGENT` | User-Agent for weather APIs | Any string, e.g. `ProFlightTracker/1.3 (your@email.com)` |

### Pre-Set (Already in Config)

These are already configured in `swim/config.json` and don't need env vars:

| Setting | Value | Notes |
|---|---|---|
| SWIM Username | `bradleysinger.gmail.com` | Hardcoded in config.json |
| SWIM Queues | All 7 feed queues | STDDS, TBFM, SFDPS, ITWS, NOTAMs, TFMS, TFDM |
| SWIM Brokers | ems1, ems2, ems3 | Different feeds use different brokers |

> **Note:** If you change your SWIM username or resubscribe to feeds, update `swim/config.json` with the new queue names.

---

## Step 4: Verify Deployment

Once deployed, test the health endpoint:

```bash
curl https://YOUR-APP.up.railway.app/health
```

Expected response:
```json
{
  "status": "ok",
  "service": "pro-flight-tracker",
  "version": "1.4",
  "timestamp": "2026-08-16T15:30:00Z"
}
```

Test a flight check:
```bash
curl "https://YOUR-APP.up.railway.app/api/flight/status?flight=DL244&date=2026-08-16"
```

Test SWIM connectivity:
```bash
curl "https://YOUR-APP.up.railway.app/api/swim/tbfm?airport=KJFK&duration=8"
```

---

## Resource Usage

| Component | Memory | Notes |
|---|---|---|
| Flask/Gunicorn | ~80 MB | 2 workers, 4 threads each |
| Python scripts | ~30 MB each | Run-and-exit, not persistent |
| Java JVM (SWIM) | ~200-400 MB | Per SWIM query, short-lived |
| **Peak (during SWIM query)** | **~500 MB** | Only during active checks |

Railway's Hobby plan allows up to 48 GB RAM / 48 vCPU per service (up to
6 replicas), so memory headroom is not the binding constraint — **cost is**.

Hobby is $5/month and includes $5 of usage credit. Credits do **not** roll
over; they reset each billing cycle. Past that you pay the delta at
**$10 / GB / month** of RAM and **$20 / vCPU / month**, billed on actual
consumption rather than the ceiling.

Rough arithmetic: a mostly-idle Flask app at ~80 MB running 24/7 is about
$0.80/month of RAM. A 400 MB JVM alive for 20 seconds costs a fraction of a
cent, so even thousands of SWIM queries a month stay inside the credit.

**The real cost risk is FlightAware AeroAPI, not Railway.** See below.

---

## Troubleshooting

### Background tracker (fixed — requires Postgres setup)

**Historical bug:** `tracker_thread.start()` used to live inside
`if __name__ == "__main__":`. Gunicorn imports the module as `app`, not
`__main__`, so that block never ran in production — `POST /api/track` returned
200, `/api/tracked` listed the flight, and no check ever ran. It worked
locally via `python app.py`, which is what made it easy to miss.

It now starts at import time, with two safeguards:

- **Leader election** (`store.acquire_leadership()`) — exactly one process runs
  the tracker. A Postgres advisory lock when `DATABASE_URL` is set (works
  across replicas), an `flock` in `/tmp` otherwise (works across workers in one
  container). Without this, `--workers 2` would poll and bill AeroAPI twice.
- **Shared state** (`store.py`) — tracked flights live in Postgres, so a
  `POST /api/track` on one worker is visible to all of them and survives
  redeploys.

**To enable persistence:** in the Railway project, **New** → **Database** →
**Add PostgreSQL**. Railway injects `DATABASE_URL` into the service
automatically; the table is created on first boot. Without `DATABASE_URL` the
app falls back to in-memory tracking and still works, but state dies on every
redeploy.

Verify it started — look for this in the deploy logs:

```
[TRACKER] Background flight tracker started (pid=..., store=postgres)
[TRACKER] Standby (another worker holds the lease), pid=...
```

Exactly one "started" line and one "Standby" line per pair of workers is
correct. `store=memory` means `DATABASE_URL` isn't reaching the service.

`GET /api/tracked` reports `store_backend` and `tracker_on_this_worker`. That
last field is only true on the leader, so with 2 workers most requests report
`false` even when the tracker is healthy — trust the logs, not that field.

Set `DISABLE_TRACKER=1` to turn the tracker off entirely without a redeploy of
code (useful if you want to stop AeroAPI spend immediately).

**Automatic untracking.** A flight is dropped once AeroAPI reports it
cancelled, diverted, arrived, or with an `actual_in` time — and unconditionally
after 36 hours (`DEFAULT_TTL_HOURS` in `store.py`). `interval_minutes` is
clamped to 5-240 to keep a typo from draining the AeroAPI credit.

### SWIM connection problems
- Check that `SWIM_PASSWORD` env var is set correctly in Railway
- SWIM brokers require IPv4: the scripts use `-Djava.net.preferIPv4Stack=true`

### Java version errors
- **The jumpstart JAR requires Java 25.** It is compiled to class file
  version 69; Java 17 and Java 21 both fail immediately with
  `UnsupportedClassVersionError` before any network call happens.
- Do NOT use `apt-get install default-jdk` — no Debian release ships Java 25.
  The Dockerfile copies the JRE from `eclipse-temurin:25-jre` instead.
- The build has a hard version gate. Look for
  `Detected Java specification version: 25` in the Railway build log. If that
  line is missing, the new Dockerfile did not deploy.

### Memory errors (OOMKilled)
- Check **Settings** → **Resource Limits** on the service; the plan ceiling is
  high but the per-service limit may be set lower
- Reduce gunicorn workers: `--workers 1`
- Don't run multiple SWIM feeds simultaneously in the `/api/check` endpoint (they're serialized by default)

### AeroAPI cost — the main budget risk

Account: **AeroAPI Personal** — $5 free usage credit/month, **10 result sets
per minute** rate limit. FlightAware does not publish per-query rates on a
static page; check the usage page in your AeroAPI console for actual spend.

**Actual query count per `/api/check` — 4 to 5, not 2-3:**

| Phase | Script | AeroAPI endpoints hit | Calls |
|---|---|---|---|
| 1 | `flight_data.py status` | `/flights/{ident}`, `/flights/{id}/route` | 2 |
| 2 | `flight_data.py chain` | `/flights/{ident}`, `/flights/{inbound_id}` | 2 |
| 2 | `flight_data.py chain` | `/flights/{id}/position` (only if ADS-B failed) | 0-1 |

One of those is wasted: `cmd_chain` re-fetches the same `/flights/{ident}`
that Phase 1 already retrieved. Passing the Phase 1 result through would cut
roughly 20% off the cost of every check.

**Rate limit headroom:** a single check at 4-5 calls is comfortably under
10/minute. Two concurrent checks are not — expect 429s if the app fans out.

**Background tracker budget:** at the 15-minute default, a quick check costs
2 AeroAPI calls, so 4 checks/hour = 192 calls/day *per tracked flight*.
Against a $5 monthly credit, one continuously tracked flight exhausts the
credit in days, not weeks. Raise `interval_minutes`, and note there is
currently **no expiry** — a tracked flight keeps polling after it lands.

> **The background tracker does not currently run in production.** See below.

### SWIM feeds return empty results
- `total_raw_messages: 0` with no `error` field means the connection worked
  and the feed was simply quiet — NOTAMs and `tfms-flow` are event-driven
- A genuine failure now returns an `error` plus a `detail` field
- Always pass `airport=` to the high-volume feeds (`tfdm`, `stdds`, `sfdps`);
  TFDM alone can emit 4,000+ messages in 3 seconds unfiltered

---

## Updating

To update the code after making changes:

```bash
git add .
git commit -m "Update description"
git push origin main
```

Railway auto-deploys from the main branch. The new version will be live in ~2 minutes.
