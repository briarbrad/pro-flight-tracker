# Message for Rork — flight phase, taxi holds, and live position

Copy everything below this line.

---

This fixes a real hole. A flight that has pushed back but hasn't taken off —
sitting in a queue, engines running, doors closed — was being reported as
finished. The brief said "Departs — departed", the risk stayed wherever it
was before pushback, and every live data source was switched off at exactly
the moment they mattered most.

Concretely: DL230 JFK→FCO pushed back at 7:20 PM and did not take off until
10:28 PM. For those three hours the app had nothing useful to say.

## 1. `phase` is the new primary state

Every `/api/brief` response now has a top-level `phase`. Read this **before**
`horizon` — it's what the screen should be organised around.

```json
"phase": {
  "phase": "TAXI_OUT",
  "phase_label": "Taxiing out",
  "phase_detail": "Left the gate, has not taken off",
  "elapsed_in_phase_min": 100,
  "next_event": "takeoff",
  "next_event_label": "Takeoff",
  "next_event_local_display": "10:28 PM EDT",
  "next_event_basis": "airline/FAA estimate",
  "next_event_status": "ESTIMATED",
  "next_event_overdue": false,
  "minutes_to_next_event": 88
}
```

Six values: `PRE_GATE`, `TAXI_OUT`, `AIRBORNE`, `TAXI_IN`, `ARRIVED`,
`CANCELLED`.

**The headline is always the next event, never the schedule.** Whatever the
phase, `phase.next_event_label` + `next_event_local_display` +
`minutes_to_next_event` is the sentence that matters — "Takeoff 10:28 PM EDT,
88 min". For a taxiing flight, pushback is history; nobody cares that it left
the gate 5 minutes early when it's been sitting on a taxiway for an hour and
forty.

`next_event` also names the key in `predicted_times`, so
`predicted_times[phase.next_event]` gets you the full entry with its basis and
`delay_vs_schedule_min` without a switch statement.

Two flags worth honouring:

- `next_event_status: "CONTROLLED"` means an FAA-assigned time. It is
  materially harder than `ESTIMATED` and worth showing differently.
- `next_event_overdue: true` means the predicted time has passed and it still
  hasn't happened. This is getting worse, not resolving.

## 2. `taxi` says whether the wait is abnormal

```json
"taxi": {
  "applicable": true,
  "assessment": "EXTENDED",
  "elapsed_min": 100,
  "typical_min": 30,
  "predicted_total_min": 188,
  "summary": "100 min into taxi-out at KJFK against a typical 30 min, and the predicted total is 188 min — roughly 158 min beyond normal."
}
```

`assessment` is `NORMAL` / `ELEVATED` / `EXTENDED`. It's judged per-airport —
30 minutes is a routine JFK taxi and would be alarming at DCA — so a `NORMAL`
here genuinely means "this is what this airport does," which is a useful thing
to be able to tell someone who's watching the clock.

`summary` is written to be rendered verbatim.

An `EXTENDED` taxi-out now escalates `verdict.departure_risk` to at least
`MODERATE` and appears in `verdict.drivers`. That's the fix for the risk chip
sitting on a stale pre-pushback value while the delay is actively happening.

`applicable: false` whenever the flight isn't taxiing — the key is always
present, so check the flag.

## 3. `position` says where it is and whether it's moving

```json
"position": {
  "available": true,
  "movement": "STOPPED",
  "movement_label": "Stopped on the ground",
  "latitude": 40.6398, "longitude": -73.7789,
  "groundspeed_kts": 0,
  "note": "Holding — the aircraft is stationary on the airport surface, typically in a departure queue or a penalty box waiting on a release."
}
```

`movement`: `STOPPED` / `TAXIING` / `TAKEOFF_ROLL` / `AIRBORNE` /
`ON_GROUND` / `UNKNOWN`.

This is the part a map can't tell you on its own. Parked in a queue and
rolling toward the runway are the same dot in the same place and completely
different experiences. `STOPPED` → still waiting. `TAXIING` → actually moving.
`TAKEOFF_ROLL` → wheels-up in seconds.

Only fetched once the aircraft is out of the gate — before pushback the
airframe is still flying someone else's route, so its position is misleading.
ADS-B and OpenSky are free and tried first; AeroAPI is a fallback that costs
one query and only fires when both miss.

`available: false` means no position is being reported. Surface ADS-B is
patchy at some airports — that's missing data, not a problem with the flight.
Say nothing rather than saying "unknown".

## 4. `refresh_after_seconds`

`300` while taxiing, `900` airborne, up to `21600` for a distant departure,
`null` once the flight is over.

The screenshot that prompted this said "Brief run 2h ago" next to a live taxi
hold. Use this to know when to grey the brief out or offer a re-run.

**It is a staleness threshold, not a polling interval.** `/api/brief` costs
AeroAPI queries and the monthly credit is small — a 5-minute auto-refresh
would burn through it. Keep refresh user-initiated; this just tells you when
to suggest it.

## 5. What changed that could break you

- **`horizon.band` no longer returns `DEPARTED`.** It used to appear the
  instant `actual_out` was set. Anything branching on it will silently stop
  matching — use `phase` instead.
- **`horizon.hours_to_departure` goes negative after pushback** and stays
  negative. That's correct, not an error. `horizon.hours_to_next_event` is
  the number that drives everything now.
- **`equipment_chain` disappears from `TAXI_OUT` onward.** It shows up in
  `sources_excluded` with a reason. The turn it described already happened.

Nothing else moved. `effects[]`, `predicted_times`, `taf_windows`,
`local_display`, severity ordering and the EDCT banner all behave exactly as
before — `effects[]` just gained two new `source` values, `taxi` and
`position`.

## 6. Bonus: it got cheaper

A taxiing flight now consults *more* live sources than a pre-departure one
while costing **2 AeroAPI queries instead of 4**, because the equipment chain
is skipped. The old behaviour paid for the chain and then threw away every
weather and traffic-management source. That was backwards in both directions.
