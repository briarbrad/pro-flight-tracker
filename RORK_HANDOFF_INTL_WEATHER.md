# Message for Rork — international SIGMET + convective forecast

Copy everything below this line.

---

Two new data sources, both free, both already wired into `/api/check` and
`/api/brief`. Ships in the same deploy as the local-time + TAF-verdict
changes — if you haven't picked those up yet, that handoff still applies
too.

## 1. International SIGMET fills the CONUS gap

Domestic `/api/weather/sigmet` only ever covered the contiguous US. New
`/api/weather/isigmet` covers everywhere `sigmet` doesn't: Alaska,
Hawaii/Pacific, and every non-US FIR.

New key in `/api/check`'s `data` and `/api/brief`'s `sources`: `isigmet`.
Same envelope as `sigmet` — a bare array plus a top-level `count`, no
wrapper:

```json
{
  "id": "LIMA 2",
  "issuing_office": "PAWU",
  "fir_id": "PAZA",
  "fir_name": "ANCHORAGE",
  "hazard": "TURB",
  "qualifier": "OCNL",
  "base_ft": 37000,
  "top_ft": 42000,
  "geometry_type": "AREA",
  "area_coords": [{"lat": 61.2, "lon": -149.9}, "..."],
  "valid_from": "2026-08-17T02:00:00+00:00",
  "valid_to": "2026-08-17T06:00:00+00:00",
  "movement": {"direction": "270", "speed_kts": 15},
  "raw": null
}
```

**It's conditional, not always present.** It's only fetched when the route
leaves CONUS — either airport's ICAO doesn't start with `K`. A JFK–LAX check
will never have an `isigmet` key, and that's correct, not a bug: `sigmet`
already covers that airspace, so fetching both would just be a duplicate
call. A JFK–FCO or ANC–SEA check will have it.

## 2. TCF — the actual product FAA uses to call ground stops for thunderstorms

New `/api/ops/tcf?route=ORIGIN,DEST` (and a `tcf` key in `/api/check` /
`/api/brief`). This is the FAA's TFM Convective Forecast — the same polygon
product traffic management uses to decide ground stops and reroutes 2-6h out
for thunderstorms. It's a sharper signal than reading "TS" out of a TAF,
because it's literally the input to the FAA response you're trying to
anticipate.

Same shape as G-AIRMET's route-filtered response — `relevant[]`,
`risk_level`, `risk_emoji`:

```json
{
  "route": "KJFK-KATL",
  "issue_time": "20260817_0300",
  "relevant_count": 1,
  "relevant": [{
    "valid_time": "20260817_1100",
    "issue_time": "20260817_0300",
    "coverage": "medium",
    "confidence": "high",
    "tops_hundreds_ft": 390,
    "near_origin": false,
    "near_dest": true,
    "along_route": true
  }],
  "risk_level": "MODERATE",
  "risk_emoji": "🟡"
}
```

`risk_level` is `MODERATE` if any relevant polygon has `coverage: "medium"`,
`LOW` if only `"sparse"`, `NONE` if nothing intersects the route. **An empty
`relevant[]` is the common case, not a failure** — most routes on most days
have no convective forecast area anywhere near them.

Call it without `route` and it dumps every active polygon nationwide instead
(`items[]`, `total_active`) — mainly useful for debugging, not for a single
flight's risk picture.

## Where these fit in the risk model

Neither one moves `/api/brief`'s deterministic `verdict` — that's
deliberate, matching how `sigmet` and `gairmet` already work today. Only
`taf_windows` escalates `departure_risk` directly. `isigmet` and `tcf` are
additional facts handed to the LLM narrative (`sources` / `llm_payload.facts`),
gated the same way as everything else: within a 6h horizon of departure.

## Unchanged

Everything from the local-time + TAF handoff — `local_display`,
`taf_windows`, the escalation rules — is unaffected and ships in the same
deploy. `/health` now reports `"version":"1.4"`.
