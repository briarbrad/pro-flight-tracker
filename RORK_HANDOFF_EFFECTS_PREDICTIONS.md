# Message for Rork — simplify the flight screen around effects + predictions

Copy everything below this line.

---

The backend's `/api/brief` response has two new blocks. Restructure the flight
screen around them — they replace most of what's currently rendered, and they
fix a contradiction the current screen has.

## New data

**`predicted_times`** — the answer to "when does this flight actually go":

```json
"predicted_times": {
  "gate_departure": {"time": "...", "status": "DERIVED",
                     "basis": "EDCT minus ~20 min taxi (crews push to make the wheels-up slot)",
                     "delay_vs_schedule_min": 16},
  "takeoff":        {"time": "...", "status": "CONTROLLED",
                     "basis": "FAA-assigned EDCT (controlled wheels-up, -5/+5 min window)",
                     "delay_vs_schedule_min": 16},
  "gate_arrival":   {"time": "...", "status": "ESTIMATED",
                     "basis": "airline/FAA estimate", "delay_vs_schedule_min": -18},
  "uncertainty_minutes": 10,
  "uncertainty_note": "±10 min at this horizon",
  "edct": {"edct": "...", "as_of": "...", "assigned_via": "..."}  // or null
}
```

**`effects[]`** — every finding as cause → effect on THIS flight:

```json
"effects": [
  {"cause": "FAA traffic management has assigned this flight an EDCT of ...",
   "effect": "That is the controlled wheels-up time (window is -5/+5 minutes)...",
   "severity": "ACTION", "source": "swim_tfms"},
  {"cause": "Ground delay program at KJFK, avg delay 2h07m",
   "effect": "A GDP meters flights ARRIVING INTO the origin — it does not assign delays to this departure...",
   "severity": "INFO", "source": "faa_status"}
]
```

## Screen restructure

Top of the flight screen, in this order:

1. **Predicted times row** — the headline. Three entries: Gate / Takeoff /
   Arrival. Each shows local time plus the delta chip
   (`delay_vs_schedule_min`: "+16 min" amber if positive, "on time" if 0/-).
   Show `uncertainty_note` in small text under the row. Tapping an entry
   reveals its `basis` string.

2. **EDCT banner** — only when `predicted_times.edct` is non-null. This is an
   FAA-assigned wheels-up time and the single most important fact on the
   screen when present: "FAA assigned takeoff slot: 7:41 PM (±5 min)". Style
   it distinctly from estimates — it is not a guess. `edct` null is the
   normal case; render nothing.

3. **Effects list** — replaces the current "Risk breakdown" AND the
   "Conditions at X right now" chips as the primary explanation. Sort
   ACTION → WATCH → INFO. Render `cause` as the bold line, `effect` as the
   body. Collapse INFO items behind "More context (N)" by default.

4. **Analyst narrative** — demote to a collapsed section at the bottom.
   With effects and predictions rendered natively, the narrative is color
   commentary, not the main event.

## Status vocabulary for time chips

| status | meaning | treatment |
|---|---|---|
| `ACTUAL` | it happened | plain, past tense |
| `CONTROLLED` | FAA-assigned (EDCT/CTA) | strongest highlight — authoritative over airline estimates |
| `ESTIMATED` | airline/FAA live estimate | normal |
| `DERIVED` | computed (e.g. EDCT − taxi) | normal, show basis on tap |
| `SCHEDULED` | schedule only, no live data | muted |
| `UNKNOWN` | nothing available | show "—" |

## Why this fixes the contradiction

The current screen shows "GDP at KJFK, expect EDCT holds" as a risk on a
flight DEPARTING JFK. A GDP constrains flights ARRIVING INTO its airport —
for a departure it's indirect context, not a delay assignment. The backend
now encodes that distinction in `effects[].severity`: that GDP arrives as
INFO for a JFK departure and would be WATCH/ACTION only for a flight bound
for JFK. Trust the severity — don't re-derive urgency from the raw
conditions data, and don't render raw `faa_status` chips as flight risks
anymore.

## Rules

- All times/severities are computed server-side. Render them; never
  recompute or re-rank client-side.
- Do not build predictions from raw sources yourself — `predicted_times`
  already encodes precedence (actual > EDCT > estimate > schedule).
- The LLM narrative already receives effects and predictions as facts with a
  guardrail against re-deriving times. If you're re-prompting the model
  yourself anywhere, stop — use `llm_payload` as-is.
- No polling. Everything above refreshes only on user action, same as before.

One backend fix that will change what you see: aircraft category was
misclassifying every widebody subtype (A333, B763, B77W → "narrowbody").
Fixed server-side — chips and turn-time standards will now read correctly.
No client change needed.
