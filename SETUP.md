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
  "version": "1.3",
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

Railway Hobby plan gives 512 MB by default. For SWIM-heavy usage, you may need to increase to 1 GB in **Settings** → **Resource Limits**. If you hit memory errors, reduce gunicorn workers to 1:

```
web: gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
```

---

## Troubleshooting

### SWIM feeds return empty results
- Check that `SWIM_PASSWORD` env var is set correctly in Railway
- SWIM brokers require IPv4: the scripts use `-Djava.net.preferIPv4Stack=true`
- NOTAMs are event-driven — a short duration may return 0 results (not an error)

### Java not found
- The Dockerfile installs OpenJDK. If the build fails, check Docker build logs
- The jumpstart JAR requires Java 21+. Verify with `java -version` in Railway shell

### Memory errors (OOMKilled)
- Increase Railway memory limit to 1 GB
- Reduce gunicorn workers: `--workers 1`
- Don't run multiple SWIM feeds simultaneously in the `/api/check` endpoint (they're serialized by default)

### AeroAPI rate limits
- Personal tier has query-based pricing (~$0.01/query)
- The `/api/check` endpoint makes 2-3 AeroAPI calls per check
- For background tracking, the interval defaults to 15 minutes — that's ~96 checks/day

---

## Updating

To update the code after making changes:

```bash
git add .
git commit -m "Update description"
git push origin main
```

Railway auto-deploys from the main branch. The new version will be live in ~2 minutes.
