# ✈️ Pro Flight Tracker API

A comprehensive flight delay risk assessment engine that pulls from **15+ real-time aviation data sources** including direct FAA SWIM feeds — the same data pipeline used by airlines and air traffic control.

**Personal use. No accounts. Type a flight number, get a risk assessment.**

---

## Architecture

```
┌─────────────────────┐
│   Rork iOS App      │  ← React Native / Expo
│                     │
│  • Flight input     │
│  • Risk display     │
│  • Push alerts      │
└────────┬────────────┘
         │ HTTPS / JSON
         ▼
┌─────────────────────┐
│  Flask API Server   │  ← This repo, deployed on Railway
│  (app.py)           │
│                     │
│  Wraps 4 Python     │
│  scripts as REST    │
│  endpoints          │
└────────┬────────────┘
         │ subprocess calls
         ▼
┌─────────────────────────────────────────────┐
│  Data Scripts                                │
│                                              │
│  flight_data.py      → AeroAPI, ADS-B,      │
│                        OpenSky               │
│  aviation_weather.py → METAR, TAF, SIGMET,   │
│                        PIREPs, FAA status     │
│  airport_ops.py      → G-AIRMET, Lightning,  │
│                        RVR, ATFM inference    │
│  swim_consumer.py    → TBFM, SFDPS, ITWS,   │
│                        NOTAMs, STDDS, TFMS,   │
│                        TFDM (via Java JMS)    │
└─────────────────────────────────────────────┘
```

---

## API Reference

**Base URL:** `https://YOUR-APP.up.railway.app`

All endpoints return JSON. No authentication required (personal use).

### Core Endpoint — Full Flight Check

This is the main endpoint the app should call. It runs all data sources in parallel and returns everything in one response.

```
GET /api/check?flight=DL244&date=2026-08-16
POST /api/check  (body: {"flight": "DL244", "date": "2026-08-16"})
```

**Response structure:**
```json
{
  "flight": "DL244",
  "date": "2026-08-16",
  "timestamp": "2026-08-16T15:30:00Z",
  "origin_icao": "KJFK",
  "destination_icao": "LICC",
  "data": {
    "flight_status": {
      "flights": [{
        "ident": "DL244",
        "status": "Scheduled",
        "aircraft_type": "B763",
        "registration": "N1604R",
        "origin": {"code_icao": "KJFK", "name": "John F Kennedy Intl"},
        "destination": {"code_icao": "LICC", "name": "Catania-Fontanarossa"},
        "scheduled_out": "2026-08-16T20:45:00Z",
        "estimated_out": "2026-08-16T20:45:00Z",
        "gate_origin": "B44"
      }]
    },
    "equipment_chain": {
      "outbound": { ... },
      "inbound": {
        "ident": "DL245",
        "registration": "N1604R",
        "estimated_in": "2026-08-16T18:09:00Z"
      },
      "turn_time_minutes": 156,
      "turn_assessment": "comfortable"
    },
    "metar": {
      "results": [{
        "icaoId": "KJFK",
        "rawOb": "KJFK 161556Z 19008KT 10SM SCT070 30/21 A3002",
        "temp": 30,
        "dewp": 21,
        "wdir": 190,
        "wspd": 8,
        "visib": 10,
        "fltCat": "VFR"
      }]
    },
    "taf": { ... },
    "sigmet": { ... },
    "pirep_origin": { ... },
    "faa_status": { ... },
    "gairmet": { ... },
    "rvr_origin": { ... },
    "lightning_origin": { ... },
    "tbfm": { ... },
    "itws_origin": { ... },
    "tfms_flight": { ... },
    "tfms_flow_gdp": { ... },
    "atfm": { ... }
  }
}
```

**Timing:** ~15-30 seconds for a full check (SWIM feeds need ~10s each for JMS connection).

---

### Flight Data Endpoints

| Endpoint | Params | Description |
|---|---|---|
| `GET /api/flight/status` | `flight`, `date` | Flight status, times, gates, aircraft type |
| `GET /api/flight/chain` | `flight`, `date` | Equipment chain — inbound flight, tail, turn time |
| `GET /api/flight/track` | `reg` or `flight` | Real-time aircraft position (ADS-B/OpenSky) |

### Weather Endpoints

| Endpoint | Params | Description |
|---|---|---|
| `GET /api/weather/metar` | `icao` (comma-sep) | Current airport observations |
| `GET /api/weather/taf` | `icao` (comma-sep) | Terminal area forecasts (24-30h) |
| `GET /api/weather/sigmet` | `type` (optional) | Active SIGMETs / Convective SIGMETs |
| `GET /api/weather/pirep` | `icao`, `distance` | Pilot reports of turbulence/icing |
| `GET /api/weather/faa-status` | `icao` (comma-sep) | FAA delay programs (GDPs, ground stops) |
| `GET /api/weather/brief` | `origin`, `dest` | Full weather briefing for a route |

### Airport Operations Endpoints

| Endpoint | Params | Description |
|---|---|---|
| `GET /api/ops/gairmet` | `route` (comma-sep) | Turbulence forecasts along route |
| `GET /api/ops/lightning` | `icao`, `radius`, `duration` | Real-time lightning strikes near airport |
| `GET /api/ops/rvr` | `airport` | Per-runway visual range (low-vis ops) |
| `GET /api/ops/atfm` | `flight`, `date` | Eurocontrol ATFM regulation inference |

### FAA SWIM Endpoints

These connect to live FAA Solace message brokers via a Java JMS client. Each call takes ~10-15 seconds.

| Endpoint | Params | Description |
|---|---|---|
| `GET /api/swim/tbfm` | `airport`, `flight`, `duration` | Arrival metering — ATC sequencing times |
| `GET /api/swim/sfdps` | `airport`, `flight`, `duration` | Flight positions (FIXM — NAS surveillance) |
| `GET /api/swim/itws` | `airport`, `duration` | Terminal weather — gust fronts, wind shear |
| `GET /api/swim/notams` | `airport`, `duration` | NOTAMs — runway closures, restrictions |
| `GET /api/swim/stdds` | `airport`, `duration` | Surface/TRACON tracks |
| `GET /api/swim/tfms-flight` | `airport`, `flight`, `duration` | NAS-authoritative flight positions |
| `GET /api/swim/tfms-flow` | `airport`, `keyword`, `duration` | GDP advisories, flow restrictions |
| `GET /api/swim/tfdm` | `airport`, `flight`, `duration` | Surface management (pushback, taxi, queue) |

### Flight Tracking (Push Notifications)

| Endpoint | Method | Params | Description |
|---|---|---|---|
| `/api/track` | POST | `flight`, `date`, `push_token`, `interval_minutes` | Start tracking — sends push when risk changes |
| `/api/track` | DELETE | `flight`, `date` | Stop tracking |
| `/api/tracked` | GET | — | List currently tracked flights |

**Push notification flow:**
1. App sends Expo push token + flight to `/api/track`
2. Server checks flight every N minutes (default 15)
3. When risk level changes (LOW → MODERATE, etc.), server sends push via Expo Push API
4. App receives notification with new risk level

---

## Rork App Integration Guide

### What Rork Needs to Know

This is a **REST API backend** for a flight trackingiOS app. The app has no user accounts — it's personal use, "type a flight and track it."

### Recommended App Screens

**1. Home Screen**
- Text input: "Enter flight number (e.g. DL244)"
- Date picker (defaults to today)
- Big "Check Flight" button
- Below: list of currently tracked flights (from `/api/tracked`)

**2. Flight Check Results Screen**
- **Risk Verdict Card** at top — color-coded:
  - 🟢 GREEN = LOW risk (background: #e8f5e9, border: #4caf50)
  - 🟡 YELLOW = MODERATE risk (background: #fff8e1, border: #ff9800)
  - 🔴 RED = HIGH risk (background: #ffebee, border: #f44336)
- **Risk Table** — 4 rows:
  - 🛫 Departure Delay
  - ✈️ En Route
  - 🛬 Arrival Conditions
  - 🔧 Equipment Chain
- **Collapsible sections** for each data source:
  - Equipment Chain (inbound flight, tail number, turn time)
  - Weather (METAR decoded, TAF summary, Beacon precip)
  - FAA Programs (any active GDPs or ground stops)
  - En Route (PIREPs, G-AIRMET, SIGMETs)
  - Airport Ops (RVR, lightning, ATFM)
  - SWIM Data (TBFM, TFMS flow, TFDM)
- **"Track This Flight"** button → calls POST `/api/track`
- **"Recheck"** button → calls GET `/api/check` again

**3. Bottom Line Summary**
- One-paragraph plain-English summary of the risk assessment
- The app should generate this from the data (or display raw summary if included)

### Risk Assessment Logic (for the App)

The API returns raw data — the app computes risk levels:

```javascript
function assessRisk(data) {
  let risks = {
    departure: 'LOW',
    enRoute: 'LOW',
    arrival: 'LOW',
    equipment: 'LOW'
  };

  // Departure: check FAA status + weather
  const faa = data.faa_status;
  if (faa?.programs?.some(p => p.type === 'Ground Stop')) {
    risks.departure = 'HIGH';
  } else if (faa?.programs?.some(p => p.type === 'GDP')) {
    risks.departure = 'MODERATE';
  }

  // Check lightning near origin
  const lightning = data.lightning_origin;
  if (lightning?.strikes_within_5nm > 0) {
    risks.departure = 'HIGH';  // Ramp closure
  } else if (lightning?.total_strikes > 0) {
    risks.departure = Math.max(risks.departure, 'MODERATE');
  }

  // En Route: check G-AIRMET + PIREPs + SIGMETs
  const gairmet = data.gairmet;
  if (gairmet?.hazards?.some(h => h.severity === 'SEV')) {
    risks.enRoute = 'HIGH';
  } else if (gairmet?.hazards?.some(h => h.severity === 'MOD')) {
    risks.enRoute = 'MODERATE';
  }

  // Arrival: check destination weather
  // (parse TAF for arrival window conditions)

  // Equipment: check turn time
  const chain = data.equipment_chain;
  if (chain?.turn_time_minutes < chain?.minimum_turn) {
    risks.equipment = 'HIGH';
  } else if (chain?.turn_time_minutes < chain?.standard_turn) {
    risks.equipment = 'MODERATE';
  }

  // Overall = worst individual category
  const overall = ['departure', 'enRoute', 'arrival', 'equipment']
    .map(k => risks[k])
    .reduce((worst, r) => RANK[r] > RANK[worst] ? r : worst, 'LOW');

  return { risks, overall };
}

const RANK = { LOW: 0, MODERATE: 1, HIGH: 2 };
```

### Expo Push Token Setup

In the Rork app, register for push notifications on app launch:

```javascript
import * as Notifications from 'expo-notifications';

async function registerForPush() {
  const { status } = await Notifications.requestPermissionsAsync();
  if (status !== 'granted') return null;
  const token = (await Notifications.getExpoPushTokenAsync()).data;
  return token;  // e.g. "ExponentPushToken[xxxxxxxxxxxxxx]"
}
```

Pass this token when tracking a flight:
```javascript
await fetch('https://YOUR-APP.up.railway.app/api/track', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    flight: 'DL244',
    date: '2026-08-16',
    push_token: pushToken,
    interval_minutes: 15
  })
});
```

---

## Data Sources

| Source | Auth | Script | What It Provides |
|---|---|---|---|
| AeroAPI (FlightAware) | API key | flight_data.py | Flight status, equipment chain, tail tracking |
| ADS-B Exchange | API key | flight_data.py | Real-time aircraft position |
| OpenSky Network | Optional | flight_data.py | Fallback position tracking |
| METAR | None | aviation_weather.py | Current airport observations |
| TAF | None | aviation_weather.py | Terminal forecasts (24-30h) |
| SIGMET | None | aviation_weather.py | Severe weather areas |
| PIREPs | None | aviation_weather.py | Pilot turbulence/icing reports |
| FAA NASSTATUS | None | aviation_weather.py | GDPs, ground stops, delay programs |
| G-AIRMET | None | airport_ops.py | Model-based turbulence forecasts |
| Blitzortung | None | airport_ops.py | Real-time lightning (ramp closure risk) |
| FAA RVR | None | airport_ops.py | Per-runway visibility |
| SWIM TBFM | SWIM password | swim_consumer.py | ATC arrival metering/sequencing |
| SWIM SFDPS | SWIM password | swim_consumer.py | NAS flight positions (FIXM) |
| SWIM ITWS | SWIM password | swim_consumer.py | Terminal weather alerts |
| SWIM NOTAMs | SWIM password | swim_consumer.py | Runway closures, restrictions |
| SWIM STDDS | SWIM password | swim_consumer.py | Surface/TRACON tracks |
| SWIM TFMS | SWIM password | swim_consumer.py | NAS-authoritative positions + GDP advisories |
| SWIM TFDM | SWIM password | swim_consumer.py | Tower surface management |

---

## File Structure

```
pro-flight-tracker/
├── app.py                     # Flask API server (all endpoints)
├── Dockerfile                 # Railway deployment container
├── Procfile                   # Railway process definition
├── railway.json               # Railway build/deploy config
├── requirements.txt           # Python dependencies
├── SETUP.md                   # Railway deployment guide + env vars
├── README.md                  # This file
├── .gitignore
├── scripts/
│   ├── flight_data.py         # AeroAPI + ADS-B + OpenSky (470 lines)
│   ├── aviation_weather.py    # METAR/TAF/SIGMET/PIREP/FAA (854 lines)
│   ├── airport_ops.py         # G-AIRMET/Lightning/RVR/ATFM (892 lines)
│   └── swim_consumer.py       # All 8 SWIM feeds via JMS (1,355 lines)
├── swim/
│   ├── config.json            # SWIM queue/broker configuration
│   ├── bin/run                # Shell script to launch Java JAR
│   ├── lib/
│   │   └── jumpstart-jar-with-dependencies.jar  # L3Harris JMS client (~10 MB)
│   └── src/                   # Java source (for reference)
└── references/
    ├── analytical-framework.md # Risk assessment methodology
    ├── report-template.md      # HTML email template
    └── sources.md              # Complete API documentation
```

---

## How the Scripts Work

Each script is a standalone CLI tool that outputs JSON to stdout. The Flask server calls them via subprocess and returns the JSON as HTTP responses.

**flight_data.py** — Queries FlightAware AeroAPI for flight status, then traces the equipment chain (what plane, where is it coming from, how much turn time). Also queries ADS-B Exchange and OpenSky for real-time aircraft position.

**aviation_weather.py** — Pulls from free government aviation weather APIs (aviationweather.gov, nasstatus.faa.gov). Returns structured METAR/TAF/SIGMET/PIREP data. The FAA status endpoint checks for active Ground Delay Programs and ground stops.

**airport_ops.py** — Four specialty data sources: G-AIRMET turbulence polygons from NWS, real-time lightning from Blitzortung (WebSocket), per-runway visibility from FAA RVR sensors, and Eurocontrol ATFM regulation inference from delay patterns.

**swim_consumer.py** — Python wrapper that launches the L3Harris Java JMS client as a subprocess, connects to FAA Solace message brokers, captures streaming XML messages for a time window (10-15 seconds), parses the XML (FIXM, AIXM, TBFM, ITWS schemas), filters by airport or flight, and returns structured JSON. Requires Java 21+ and the SWIM password.

---

##Version History

- **v1.3** — TFMS + TFDM SWIM feeds (GDP advisories, NAS-authoritative positions, surface management)
- **v1.2** — FAA SWIM integration (TBFM, SFDPS, ITWS, NOTAMs, STDDS)
- **v1.1** — G-AIRMET, Lightning, RVR, ATFM inference
- **v1.0** — Core flight data, weather, and delay analysis
