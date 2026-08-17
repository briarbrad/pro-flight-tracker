# Message for Rork — local times + forecast in the brief

Copy everything below this line.

---

Two backend changes to `/api/brief`.

## 1. Times now come with airport-local versions

Every entry in `predicted_times` has four new fields:

```json
"gate_departure": {
  "time": "2026-08-17T14:02:00+00:00",     // UTC, unchanged
  "time_local": "2026-08-17T10:02:00-04:00",
  "local_display": "10:02 AM EDT",
  "utc_display": "14:02Z",
  "timezone": "America/New_York",
  "status": "SCHEDULED",
  "basis": "...",
  "delay_vs_schedule_min": 0
}
```

**Render `local_display`.** Gate and takeoff are local to the *origin*;
arrival is local to the *destination* — the backend already applies the right
zone to each, so don't convert anything yourself. A JFK→Rome arrival shows in
Rome time, which is what a traveller wants.

`timezone: ""` means the zone couldn't be resolved. In that case
`local_display` falls back to Zulu (e.g. `"14:02Z"`) — show it as-is and don't
guess an offset. A top-level `timezones: {origin, destination}` is also
returned.

The analyst narrative is now instructed to write in airport-local time too, so
the prose and the chips agree. No more Zulu in the narrative.

## 2. The terminal forecast now drives the verdict

New top-level `taf_windows` with a `departure` and `arrival` assessment, each
covering ±60 minutes around the actual predicted time:

```json
"taf_windows": {
  "departure": {
    "airport": "KLGA",
    "prevailing_category": "IFR",            // VFR | MVFR | IFR | LIFR | UNKNOWN
    "worst_conditional_category": "LIFR",    // from TEMPO/PROB groups
    "significant_weather": ["thunderstorms", "mist"],
    "max_gust_kts": 34,
    "wind_shear": true,
    "available": true,
    "note": "",
    "periods": [ { "from_local": "6:00 AM EDT", "category": "IFR",
                   "conditional": false, "change_indicator": "FM", ... } ]
  },
  "arrival": { ... }
}
```

Forecast findings also appear in `effects[]` with `"source": "taf"`, so if
you're already rendering the effects list you get this for free.

**The prevailing / conditional distinction matters and is worth showing.**
`prevailing_category` is what's actually forecast (FM groups).
`worst_conditional_category` comes from TEMPO/PROB groups — a temporary
deterioration that may not happen. A TEMPO IFR group is a "keep an eye on it",
not a "replan". They surface as WATCH; only prevailing conditions escalate the
verdict.

**What escalates risk:** prevailing IFR/LIFR, thunderstorms, or freezing
precipitation → `ACTION` → MODERATE. Everything else — MVFR, gusts, wind
shear, TEMPO groups — surfaces as `WATCH`/`INFO` and does **not** move the
headline. That's deliberate: marginal ceilings are routine, and escalating
them would train the user to ignore the risk color.

## Why this matters for long-horizon flights

Previously a flight 12+ hours out always read "No delay mechanism identified"
even when the forecast was ugly — the TAF was fetched but nothing read it. Now
a flight tomorrow morning with forecast IFR and thunderstorms correctly returns
MODERATE with a stated reason, while a clear-forecast flight stays LOW.

Confidence is still LOW at that horizon, and that combination is meaningful:
"we can see a real weather constraint, but a 14-hour-out forecast can move."
Keep showing risk and confidence together.

## Unchanged

Everything else — the effects list, EDCT banner, severity ordering, horizon
gating, no-polling — works exactly as before. `effects[]` now arrives
pre-sorted ACTION → WATCH → INFO, so render top-down without re-sorting.
