# Analytical Framework — Deep Reference

This document contains the full GDP cascade analysis methodology, calibration
rules with worked examples, and the equipment chain analysis procedure.
The SKILL.md summarizes these; this file is the authoritative deep reference.

## GDP Cascade Analysis — Full Methodology

### Step 1: Identify Active Programs

Pull FAA status via `aviation_weather.py faa-status`. For each active program:
- Note the airport, reason, average/max delay, trend
- Note the program type: GDP, Ground Stop, Arr/Dep Delay, Closure

### Step 2: Classify Into Branch A or B

**Branch A — Transient (No Cascade)**

Conditions (ALL must be true):
- The delay program is weather-driven (not equipment/staffing)
- The weather causing it is forecast to clear before the flight arrives
- Check TAF at the affected airport for the arrival window
- Check NWS/Beacon for confirmation
- The flight has sufficient block time (4h+) to absorb en-route delays

If Branch A:
- GDP delays will be absorbed airborne via miles-in-trail spacing
- Departure hold estimate: 0–20 minutes for long-haul; 15–45 for medium
- GDP trend "decreasing" further reduces the estimate

**Branch B — Structural (Cascades Forward)**

Conditions (ANY is sufficient):
- Equipment out of position: the inbound aircraft is delayed >60 min
  AND its delay pushes the turn time below the minimum benchmark
- Crew timing: if inbound crew has been delayed such that rest-hour
  rules may force a crew swap (14-hour duty day limit for domestic)
- Queue drain: even after weather clears, the accumulated stack of
  GDP-held aircraft must drain. This takes 1–3 hours depending on
  airport capacity. JFK can process ~40 arrivals/hour; a 200-plane
  queue takes ~5 hours to drain to normal.
- Cancellations: if 10%+ of inbound flights to an airport are cancelled,
  aircraft utilization cascades. Check AeroAPI for cancellation rate.

If Branch B:
- Delays will cascade forward regardless of weather improvement
- Estimate the cascade duration from the drain time calculation
- Equipment-specific: trace the exact inbound, don't use airport averages

### Step 3: Calculate Impact on This Specific Flight

Never apply airport-level average delays to a specific flight without
checking its equipment chain. A flight whose inbound is on time and coming
from a non-affected airport may depart on time even during a GDP.

## Equipment Chain Analysis — Procedure

### Tracing the Chain

1. `flight_data.py chain --flight DL960 --date 2026-08-13`
2. This returns:
   - Outbound flight details (times, gates, aircraft type)
   - Inbound flight details (where is the plane coming from?)
   - Current aircraft position (real-time via ADS-B/OpenSky)
   - Turn time analysis (available vs. required)

### Turn Time Assessment

| Available Turn Time | Category | Assessment |
|---|---|---|
| < minimum benchmark | 🔴 RED | Delay virtually certain |
| minimum to standard | 🟡 YELLOW | Tight — watch closely |
| standard to 2× standard | 🟢 GREEN | Adequate |
| > 2× standard | 🟢 GREEN | Comfortable buffer |

Minimums: Regional 45 min, Narrowbody 60 min, Widebody 90 min
Standard: Regional 60 min, Narrowbody 90 min, Widebody 150 min

### Position Tracking

When the inbound is airborne:
- Calculate remaining distance and ETA from current position + groundspeed
- Compare to the scheduled arrival time
- If ETA is later than scheduled, recalculate available turn time
- If turn time drops below minimum, escalate to 🔴

When the inbound is on the ground:
- Is it at the gate? (on_ground = true at origin airport)
- Has it pushed? (check actual_out vs scheduled_out)
- How long until it arrives at the origin of our flight?

## Calibration Rules — Worked Examples

### Example 1: DL960 LAX→JFK, Aug 6–7, 2026

**Scenario:** Active JFK GDP due to evening thunderstorms. DL960 is a 5h+
transcon departing LAX at ~11 PM, arriving JFK ~6:30 AM.

**Initial analysis (wrong):** Predicted 30–90 min departure hold due to GDP.
Rating: 🟡 MODERATE departure delay.

**What actually happened:** 15-minute push delay, arrived 6 min late.
The GDP was absorbed entirely en route via miles-in-trail spacing.

**Lesson → Rule 1:**
- This was textbook Branch A: weather-driven GDP, transient storms
  clearing overnight, 5h+ flight with massive en-route absorption capacity
- Long-haul flights under Branch A transient GDP → LOW departure risk
- FAA uses miles-in-trail (MIT) spacing to absorb GDP time airborne
- MIT adds ~10–15 min to a 5h flight, not 30–90 min of gate hold

### Example 2: DL5742 LGA↔DCA Shuttle, Aug 10, 2026

**Scenario:** E175 regional shuttle, ~1h block time, tight turn schedule.

**Key insight:** Regional shuttle with 45–60 min turns has very little
buffer. Any inbound delay of >15 min cascades directly. Equipment chain
analysis was more important than weather for this flight.

**Lesson → Rule 6:** Equipment chain is as important as weather.
Always trace the inbound, especially for regional/shuttle operations.

### Example 3: Tuesday Return Flight, Aug 11, 2026

**Scenario:** User asked to check a Tuesday flight, but it was still
Sunday. No TAF existed yet for Tuesday.

**Lesson → Rule 4:** TAFs only cover 24–30 hours. For flights beyond
that horizon, acknowledge the gap explicitly, use NWS extended and
Beacon as interim indicators, and recommend recheck timing.

## Enhanced Operations Sources (v1.1)

### G-AIRMET Turbulence — Integration with En Route Assessment

G-AIRMETs are the NWS-processed derivative of the Graphical Turbulence
Guidance (GTG) model. They provide systematic polygon-based turbulence
forecasts, upgrading the en route assessment from PIREP-based (anecdotal)
to model-based (comprehensive).

**Usage:**
```
python3 airport_ops.py gairmet --route KJFK LIRF
```

**Integration logic:**
1. Pull G-AIRMETs for `turb-hi`, `turb-lo`, `llws` hazards
2. Filter polygons relevant to the flight route (within 75 NM of
   great-circle path, or within 100 NM of origin/destination)
3. Check severity (MOD/SEV/EXTM) and altitude bands vs. cruise altitude
4. G-AIRMET MOD along route → 🟡 en route minimum, even if no PIREPs
   (pilots may be deviating around the area — absence of PIREPs ≠ smooth)
5. G-AIRMET SEV → 🔴 en route, expect deviations and turbulence

### Lightning — Ramp Closure Risk

Real-time lightning from Blitzortung crowdsourced network (~global
coverage, seconds latency). Directly addresses the trigger that starts
many delay cascades — it's not the rain that closes the ramp, it's the
lightning.

**Usage:**
```
python3 airport_ops.py lightning --icao KJFK --radius 20 --duration 30
```

**Integration logic:**
1. Collect strikes for 30 seconds (configurable) within radius of airport
2. Any strike within 5 NM → ramp closure risk HIGH (🔴)
   - Ramp closure = no pushbacks, no gate arrivals, baggage loading stops
   - All-clear requires 15+ consecutive minutes with zero strikes <5 NM
3. Strikes 5-20 NM → MODERATE activity (🟡) — ramp closure possible if
   storms are tracking toward airport
4. Use with TAF/SIGMETs: lightning confirms convective activity is
   occurring NOW, not just forecast

### RVR — Low Visibility Operations

Runway Visual Range from FAA airport sensors. Reports per-runway
visibility at touchdown zone, midpoint, and rollout positions. Critical
during fog, heavy rain, and snow events.

**Usage:**
```
python3 airport_ops.py rvr --airport JFK
```

**Integration logic:**
1. RVR > 6000 ft → not a factor (normal VFR)
2. RVR 1800-4000 ft → IFR, above CAT I → 🟡 if at destination
3. RVR 600-1800 ft → below CAT I → 🔴, only CAT II aircraft can land
4. RVR < 600 ft → below CAT II → 🔴 CRITICAL, only CAT III with autoland
5. Also parse METAR RVR groups (R04L/2000V4000FT) during low-vis events
6. RVR is reported at ~120 US airports, updates every 60 seconds
7. Key fog airports: SFO, LGA, EWR — check RVR early morning

**Not all runways have all sensors.** "FFF" = sensor fault, blank = no
sensor at that position. Base arrival risk on worst active runway with
working sensors.

### ATFM Inference — Eurocontrol Flow Management Heuristic

Direct Eurocontrol ATFM data requires institutional B2B PKI credentials
(airline/ANSP only). This heuristic infers probable CTOT (Calculated
Take-Off Time) regulation from observable delay patterns.

**Usage:**
```
python3 airport_ops.py atfm-infer --flight DL182 --date 2026-08-15
```

**Heuristic scoring:**
- Departure delay 15-120 min (CTOT range): +30 confidence
- Delay aligns to 5-min slot increment: +15
- Origin weather is VFR (delay not local): +20
- Arrival in European peak traffic window (05-10Z or 14-20Z): +10
- AeroAPI status contains delay flag: +10

**Verdicts:**
- ≥50% confidence → PROBABLE (🟡)
- 25-49% → POSSIBLE (🟡)
- <25% with indicators → UNLIKELY (🟢)
- No indicators → NO_INDICATION (🟢)

**How to use in flight check:**
- Only relevant for European destinations (ICAO prefix E*, L*, BI, GC, etc.)
- If PROBABLE → add inferred delay to departure estimate, flag as
  "Eurocontrol ATFM regulation likely — CTOT slot assigned"
- Equipment chain assessment: CTOT delays the pushback, not the aircraft
  positioning. The plane is at the gate; it's just waiting for its slot.

## Risk Rating Decision Tree

```
For each category (Departure, En Route, Arrival, Equipment):

IF any CRITICAL factor exists → 🔴 RED
   (Ground Stop, Closure, inbound cancelled, IFR below minimums,
    severe convection over route, turn time below minimum,
    RVR below CAT I at destination, active ramp closure from lightning)

ELSE IF any WARNING factor exists → 🟡 YELLOW
   (GDP active, tight turn time, MVFR conditions, moderate turbulence,
    SIGMET overlapping arrival window, Branch B cascade possible,
    G-AIRMET MOD along route, lightning activity 5-20 NM,
    ATFM/CTOT probable for European destination)

ELSE → 🟢 GREEN
   (No delays, VFR conditions, adequate turn time, smooth ride,
    no lightning, RVR >6000 or not reporting, no ATFM indication)

Overall = worst individual category, with narrative context.
```
