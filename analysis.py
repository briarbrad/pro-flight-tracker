#!/usr/bin/env python3
"""
Analysis layer for Pro Flight Tracker.

Everything here is deterministic. It decides which data sources are worth
consulting for a given time-to-departure, classifies the delay mechanism using
the Branch A / Branch B methodology from references/analytical-framework.md,
and assembles a prompt payload for a downstream LLM.

The division of labour is deliberate:

  - Python does arithmetic, time comparisons, thresholds, and source gating.
  - The LLM does narrative synthesis, and only ever sees numbers that were
    already computed correctly here.

The motivating failure is Example 1 in analytical-framework.md: an active JFK
GDP produced a predicted 30-90 minute hold when the real outcome was a
15-minute push. The GDP was real, it just wasn't relevant to that flight at
that horizon. Handing a model raw live conditions for a distant departure
reproduces that mistake, so horizon gating happens before the model is asked
anything.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
FRAMEWORK_PATH = REPO_ROOT / "references" / "analytical-framework.md"

# ---------------------------------------------------------------------------
# Horizon bands
# ---------------------------------------------------------------------------

# (lower_bound_hours, upper_bound_hours_exclusive, label)
HORIZON_BANDS = [
    (0.0, 2.0, "IMMINENT"),
    (2.0, 6.0, "NEAR"),
    (6.0, 12.0, "SAME_DAY"),
    (12.0, 24.0, "NEXT_DAY"),
    (24.0, None, "DISTANT"),
]

BAND_GUIDANCE = {
    "IMMINENT": "Live operational data is authoritative. Surface, metering, "
                "RVR and lightning all carry real signal.",
    "NEAR": "Active traffic management programs are the dominant signal. "
            "Equipment chain is knowable and matters.",
    "SAME_DAY": "Equipment chain and terminal forecast dominate. Current "
                "programs are weakening evidence.",
    "NEXT_DAY": "Forecast-only regime. Current traffic management programs "
                "will almost certainly have expired before departure. The "
                "terminal forecast covering the departure window is the "
                "highest-value source. Overnight risk transfers through "
                "aircraft positioning, not through today's programs.",
    "DISTANT": "Schedule and base rates only. Almost nothing observable today "
               "constrains this departure.",
}

# Maximum hours-to-departure at which each source still carries signal.
# None means always relevant. These thresholds encode the horizon table:
# a ground stop happening now tells you nothing about a departure tomorrow.
SOURCE_HORIZON = {
    "flight_status":   (None, "Schedule, status and equipment identity"),
    "taf":             (30.0, "Terminal forecast covering the departure window"),
    "equipment_chain": (12.0, "Inbound aircraft and turn time"),
    "faa_status":      (6.0,  "Active FAA delay programs"),
    "tfms_flow":       (6.0,  "Traffic management advisories with validity windows"),
    "sigmet":          (6.0,  "Active severe weather areas"),
    "gairmet":         (12.0, "Forecast turbulence and icing"),
    "metar":           (3.0,  "Current surface observation"),
    "lightning":       (2.0,  "Live strike activity driving ramp closures"),
    "rvr":             (2.0,  "Live runway visual range"),
    "tbfm":            (2.0,  "Live arrival metering"),
    "itws":            (2.0,  "Live terminal weather alerts"),
}

# Reasons a program might cascade past its own expiry, per Branch B.
_WEATHER_REASONS = ("weather", "thunderstorm", "wind", "snow", "ice", "fog",
                    "rain", "low ceiling", "visibility", "swap", "wx")
_STRUCTURAL_REASONS = ("staffing", "equipment", "volume", "runway",
                       "construction", "outage")


# ---------------------------------------------------------------------------
# Horizon
# ---------------------------------------------------------------------------

def parse_iso(value):
    """Parse an ISO 8601 timestamp, tolerating a trailing Z. None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def band_for(hours):
    if hours is None:
        return "UNKNOWN"
    if hours < 0:
        return "DEPARTED"
    for low, high, label in HORIZON_BANDS:
        if hours >= low and (high is None or hours < high):
            return label
    return "DISTANT"


def compute_horizon(flight: dict, now: datetime = None) -> dict:
    """Hours until this flight departs, and which band that puts it in.

    Prefers the most committed time available: actual > estimated > scheduled.
    """
    now = now or datetime.now(timezone.utc)
    scheduled = parse_iso(flight.get("scheduled_out")) if flight else None
    estimated = parse_iso(flight.get("estimated_out")) if flight else None
    actual = parse_iso(flight.get("actual_out")) if flight else None

    reference = actual or estimated or scheduled
    hours = None
    if reference:
        hours = (reference - now).total_seconds() / 3600.0

    return {
        "hours_to_departure": round(hours, 2) if hours is not None else None,
        "band": band_for(hours),
        "band_guidance": BAND_GUIDANCE.get(band_for(hours), ""),
        "reference_time": reference.isoformat() if reference else None,
        "reference_basis": ("actual" if actual else
                            "estimated" if estimated else
                            "scheduled" if scheduled else None),
        "scheduled_out": flight.get("scheduled_out") if flight else None,
        "evaluated_at": now.isoformat(),
    }


def source_plan(hours) -> dict:
    """Decide which sources to consult for this horizon.

    Returns {source: {"relevant": bool, "reason": str, "provides": str}}.
    Gating here is what keeps a 15-hour-out flight from being judged on
    conditions that will have cleared — and it also avoids paying AeroAPI for
    an equipment chain that isn't knowable yet.
    """
    plan = {}
    for source, (max_hours, provides) in SOURCE_HORIZON.items():
        if hours is None:
            relevant = source in ("flight_status", "taf")
            reason = ("Departure time unknown — limited to schedule-independent "
                      "sources") if not relevant else "Safe to consult without a horizon"
        elif hours < 0:
            relevant = source == "flight_status"
            reason = "Flight has already departed"
        elif max_hours is None or hours <= max_hours:
            relevant = True
            reason = f"Within the {max_hours}h useful window" if max_hours else "Always relevant"
        else:
            relevant = False
            reason = (f"Departure is {hours:.1f}h out; this source stops "
                      f"carrying signal beyond {max_hours}h")
        plan[source] = {"relevant": relevant, "reason": reason,
                        "provides": provides}
    return plan


# ---------------------------------------------------------------------------
# Branch A / Branch B classification
# ---------------------------------------------------------------------------

def _programs_from_faa(faa_status) -> list:
    """Flatten an faa-status payload into a list of active program dicts."""
    programs = []
    if not isinstance(faa_status, dict):
        return programs
    data = faa_status.get("data")
    if not isinstance(data, dict):
        return programs
    for airport, record in data.items():
        if not isinstance(record, dict):
            continue
        for kind, key in (("GROUND_STOP", "ground_stops"),
                          ("GDP", "ground_delay_programs"),
                          ("ARR_DEP_DELAY", "arrival_departure_delays"),
                          ("CLOSURE", "closures")):
            for entry in record.get(key) or []:
                if isinstance(entry, dict):
                    programs.append({"airport_key": airport, "type": kind, **entry})
    return programs


def _reason_is_weather(reason: str) -> bool:
    r = (reason or "").lower()
    if any(term in r for term in _STRUCTURAL_REASONS):
        return False
    return any(term in r for term in _WEATHER_REASONS)


def classify_branch(horizon: dict, programs: list, turn_analysis: dict,
                    plan: dict) -> dict:
    """Apply the Branch A / Branch B methodology.

    Branch A — transient. Weather-driven, expected to clear, absorbed en route.
    Branch B — structural. Cascades forward regardless of weather improvement.

    Returns a branch plus the evidence, rather than a single opaque label —
    the LLM needs the reasoning, not the verdict alone.
    """
    hours = horizon.get("hours_to_departure")
    evidence = []
    branch = "UNDETERMINED"

    # --- Branch B: equipment out of position -----------------------------
    turn_says_b = False
    if isinstance(turn_analysis, dict):
        available = turn_analysis.get("turn_time_available_min")
        minimum = turn_analysis.get("turn_time_required_min_minimum")
        if available is not None and minimum is not None:
            if available < minimum:
                turn_says_b = True
                evidence.append(
                    f"Turn time {available:.0f} min is below the "
                    f"{minimum} min minimum for a "
                    f"{turn_analysis.get('aircraft_category', 'unknown')} — "
                    "equipment is the binding constraint (Branch B)."
                )
            else:
                evidence.append(
                    f"Turn time {available:.0f} min clears the {minimum} min "
                    "minimum — equipment is not currently the constraint."
                )

    # --- Program-driven classification -----------------------------------
    if not plan.get("faa_status", {}).get("relevant", False):
        evidence.append(
            "Active delay programs were not consulted: at "
            f"{hours:.1f}h out they would have expired before departure."
            if hours is not None else
            "Active delay programs were not consulted for this horizon."
        )
    elif not programs:
        evidence.append("No active FAA delay programs at the relevant airports.")
    else:
        # Three-way split: explicitly structural causes make Branch B;
        # weather causes make Branch A; an empty or unrecognized reason is
        # treated as A-with-unknown-cause rather than silently escalating to
        # B (and therefore HIGH) on missing metadata.
        def _cause(p):
            r = (p.get("reason") or "").lower()
            if any(term in r for term in _STRUCTURAL_REASONS):
                return "structural"
            if any(term in r for term in _WEATHER_REASONS):
                return "weather"
            return "unknown"

        weather_driven = [p for p in programs if _cause(p) == "weather"]
        structural = [p for p in programs if _cause(p) == "structural"]
        unknown_cause = [p for p in programs if _cause(p) == "unknown"]
        if unknown_cause and not structural:
            evidence.append(
                f"{len(unknown_cause)} program(s) have no stated cause — "
                "treated as transient rather than structural, but worth "
                "watching.")
            weather_driven = weather_driven + unknown_cause
        for p in programs:
            evidence.append(
                f"{p['type']} at {p.get('airport', p.get('airport_key'))}"
                + (f" — {p['reason']}" if p.get("reason") else "")
                + (f", avg {p['average_delay']}" if p.get("average_delay") else "")
            )
        if structural:
            branch = "B"
            evidence.append(
                "At least one program is non-weather (staffing, volume, "
                "equipment or runway) — those do not clear on a forecast "
                "and cascade forward (Branch B)."
            )
        elif weather_driven:
            branch = "A"
            evidence.append(
                "All active programs are weather-driven. Branch A applies if "
                "the forecast clears the departure window; delay is typically "
                "absorbed en route via miles-in-trail rather than gate hold."
            )

    if turn_says_b:
        branch = "B"

    if branch == "UNDETERMINED" and hours is not None and hours > 12:
        branch = "NOT_APPLICABLE"
        evidence.append(
            "No delay mechanism is assessable this far out. Overnight risk "
            "transfers through aircraft positioning, which is not yet knowable."
        )

    return {
        "branch": branch,
        "branch_label": {
            "A": "Transient — weather-driven, expected to clear",
            "B": "Structural — cascades forward",
            "NOT_APPLICABLE": "Too far out to classify",
            "UNDETERMINED": "Insufficient evidence",
        }.get(branch, branch),
        "evidence": evidence,
        "active_program_count": len(programs),
    }


def assess(horizon: dict, branch: dict, turn_analysis: dict,
           flight: dict) -> dict:
    """Coarse departure-risk verdict plus an honest confidence level.

    Confidence is as important as the verdict: at long horizons the correct
    output is 'we can't tell yet', not a falsely precise number.
    """
    hours = horizon.get("hours_to_departure")
    risk = "LOW"
    drivers = []

    if flight.get("cancelled"):
        risk, drivers = "HIGH", ["Flight is cancelled."]
    elif flight.get("diverted"):
        risk, drivers = "HIGH", ["Flight is diverted."]
    else:
        if branch.get("branch") == "B":
            risk = "HIGH"
            drivers.append("Structural (Branch B) mechanism in play — "
                           "does not resolve with improving weather.")
        elif branch.get("branch") == "A":
            risk = "MODERATE"
            drivers.append("Active weather-driven program (Branch A) — "
                           "typically absorbed en route, not held at the gate.")

        if isinstance(turn_analysis, dict):
            available = turn_analysis.get("turn_time_available_min")
            standard = turn_analysis.get("turn_time_required_min_standard")
            minimum = turn_analysis.get("turn_time_required_min_minimum")
            if available is not None and minimum is not None:
                if available < minimum and risk != "HIGH":
                    risk = "HIGH"
                    drivers.append(turn_analysis.get("note", "Turn time below minimum."))
                elif standard and available < standard and risk == "LOW":
                    risk = "MODERATE"
                    drivers.append(turn_analysis.get("note", "Turn time below standard."))

    # Confidence is driven by horizon, not by how much data we happened to get.
    band = horizon.get("band")
    confidence = {
        "IMMINENT": "HIGH",
        "NEAR": "HIGH",
        "SAME_DAY": "MEDIUM",
        "NEXT_DAY": "LOW",
        "DISTANT": "LOW",
        "DEPARTED": "HIGH",
        "UNKNOWN": "LOW",
    }.get(band, "LOW")

    if not drivers:
        drivers.append("No delay mechanism identified from the sources "
                       "relevant at this horizon.")

    return {
        "departure_risk": risk,
        "confidence": confidence,
        "confidence_basis": f"Horizon band {band} — "
                            + BAND_GUIDANCE.get(band, ""),
        "drivers": drivers,
    }


# ---------------------------------------------------------------------------
# LLM prompt payload
# ---------------------------------------------------------------------------

def _framework_text(max_chars: int = 12000) -> str:
    try:
        text = FRAMEWORK_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:max_chars]


SYNTHESIS_RULES = [
    "Every number in your answer must come from the facts provided. Do not "
    "compute, estimate, or infer new figures.",
    "Sources marked not_consulted were deliberately excluded because they do "
    "not carry signal at this time-to-departure. Do not speculate about them "
    "and do not treat their absence as reassuring or alarming.",
    "Respect the stated confidence. At LOW confidence, say plainly that it is "
    "too early to judge rather than producing a precise-sounding estimate.",
    "A weather-driven program (Branch A) is normally absorbed en route via "
    "miles-in-trail spacing, not as a gate hold. Do not convert an airport's "
    "average delay into this flight's expected delay.",
    "If the branch is NOT_APPLICABLE, explain which signals will become "
    "meaningful and roughly when, instead of assessing risk now.",
]


def build_llm_payload(flight_ident: str, date: str, horizon: dict,
                      plan: dict, branch: dict, verdict: dict,
                      sources: dict) -> dict:
    """Assemble a ready-to-send prompt for a downstream LLM.

    Returned as separate `system` / `user` strings plus the structured `facts`,
    so the caller can send it to whatever model it likes without needing to
    know the methodology.
    """
    consulted = {k: v for k, v in sources.items() if v.get("status") == "ok"}
    excluded = {k: plan[k]["reason"] for k in plan if not plan[k]["relevant"]}

    facts = {
        "flight": flight_ident,
        "date": date,
        "horizon": horizon,
        "branch_classification": branch,
        "deterministic_verdict": verdict,
        "sources_consulted": consulted,
        "sources_not_consulted": excluded,
    }

    system = (
        "You are a flight delay risk analyst. You are given facts that have "
        "already been gathered, filtered by relevance, and analysed "
        "deterministically. Your job is synthesis and explanation only.\n\n"
        "Rules:\n"
        + "\n".join(f"- {r}" for r in SYNTHESIS_RULES)
        + "\n\nMethodology reference:\n\n"
        + _framework_text()
    )

    user = (
        f"Flight {flight_ident} on {date}.\n"
        f"Departure is {horizon.get('hours_to_departure')} hours away "
        f"(band: {horizon.get('band')}).\n\n"
        f"{horizon.get('band_guidance', '')}\n\n"
        "Using only the facts below, explain the delay risk in plain language: "
        "what the actual mechanism is (or why none is assessable yet), what "
        "would change the picture, and what is worth watching next.\n\n"
        "FACTS:\n"
    )

    return {
        "system": system,
        "user": user,
        "facts": facts,
        "guardrails": SYNTHESIS_RULES,
        "note": "Send `system` as the system prompt and `user` + "
                "JSON.stringify(facts) as the user message.",
    }


# ---------------------------------------------------------------------------
# EDCT extraction
# ---------------------------------------------------------------------------

def extract_edct(tfms_results: list, callsign: str) -> dict:
    """Find the FAA-assigned wheels-up time (EDCT) for one flight.

    Scans SWIM tfms-flight results for the callsign and returns the most
    recent controlled-time assignment. Sources, in order of authority:
    an explicit `edct`/`ctd` element, or an etd whose type marks it
    CONTROLLED. Returns {} if the flight has no EDCT — which is the normal
    case; only flights captured by a traffic management program get one.
    """
    if not tfms_results or not callsign:
        return {}
    cs = callsign.upper().replace(" ", "")
    best = {}
    best_ts = ""
    for r in tfms_results:
        if not isinstance(r, dict):
            continue
        rid = (r.get("flight_id") or "").upper().replace(" ", "")
        if rid != cs:
            continue
        ts = r.get("source_timestamp") or ""

        edct_time = r.get("edct")
        etd = r.get("etd") or {}
        if not edct_time and isinstance(etd, dict):
            if "CONTROL" in (etd.get("type") or "").upper():
                edct_time = etd.get("time")
        if not edct_time:
            continue
        if ts >= best_ts:
            best_ts = ts
            best = {
                "edct": edct_time,
                "cta": r.get("cta"),
                "assigned_via": r.get("msg_type", ""),
                "as_of": ts,
            }
    return best


# ---------------------------------------------------------------------------
# Cause -> effect on THIS flight
# ---------------------------------------------------------------------------

def _block_hours(flight: dict):
    """Filed block time in hours, or None."""
    ete = flight.get("filed_ete")
    try:
        seconds = float(ete)
        if seconds > 0:
            return seconds / 3600.0
    except (TypeError, ValueError):
        pass
    off = parse_iso(flight.get("scheduled_out"))
    on = parse_iso(flight.get("scheduled_in"))
    if off and on and on > off:
        return (on - off).total_seconds() / 3600.0
    return None


def build_effects(flight: dict, programs: list, turn_analysis: dict,
                  edct: dict, horizon: dict) -> list:
    """Translate each finding into its effect on THIS flight.

    Every entry is {cause, effect, severity, source}. severity is one of
    INFO (context, no action), WATCH (could move the flight), ACTION
    (will move the flight / user should act on it).

    The aviation nuance that matters here: a GDP/ground stop constrains
    ARRIVALS INTO its airport. For a flight DEPARTING that airport the
    effect is indirect (congestion, late inbound equipment); for a flight
    ARRIVING there it is direct (EDCT assignment, airborne holding). The
    generic "GDP at your airport" framing conflates the two.
    """
    effects = []
    origin = (flight.get("origin_icao") or "").upper()
    dest = (flight.get("dest_icao") or "").upper()
    block = _block_hours(flight)
    long_haul = block is not None and block >= 4.0

    def norm(code):
        c = (code or "").upper()
        return c if len(c) == 4 else ("K" + c if len(c) == 3 else c)

    # --- EDCT first: it's the single most actionable item ----------------
    if edct.get("edct"):
        effects.append({
            "cause": f"FAA traffic management has assigned this flight an "
                     f"EDCT of {edct['edct']}",
            "effect": "That is the controlled wheels-up time (window is "
                      "-5/+5 minutes). Expect pushback to be timed so the "
                      "aircraft reaches the runway for that slot; the "
                      "schedule times no longer govern.",
            "severity": "ACTION",
            "source": "swim_tfms",
        })

    for p in programs:
        p_airport = norm(p.get("airport") or p.get("airport_key"))
        at_origin = p_airport and p_airport == norm(origin)
        at_dest = p_airport and p_airport == norm(dest)
        where = "origin" if at_origin else "destination" if at_dest else "other"
        ptype = p.get("type")
        reason = p.get("reason") or ""
        avg = p.get("average_delay") or ""

        if ptype == "GROUND_STOP":
            if at_dest:
                effects.append({
                    "cause": f"Ground stop at {p_airport}"
                             + (f" ({reason})" if reason else ""),
                    "effect": "Departures bound for the destination are held "
                              "on the ground until the stop lifts"
                              + (f" (expected end {p['expected_end']})"
                                 if p.get("expected_end") else "")
                              + ". Expect the departure to hold.",
                    "severity": "ACTION", "source": "faa_status",
                })
            elif at_origin:
                effects.append({
                    "cause": f"Ground stop at {p_airport}"
                             + (f" ({reason})" if reason else ""),
                    "effect": "This halts flights INBOUND to the origin, not "
                              "this departure. Direct effect is limited, but "
                              "inbound equipment for later flights will "
                              "arrive late and surface congestion builds.",
                    "severity": "WATCH", "source": "faa_status",
                })
        elif ptype == "GDP":
            if at_dest:
                effects.append({
                    "cause": f"Ground delay program at {p_airport}"
                             + (f", avg delay {avg}" if avg else ""),
                    "effect": ("This flight is subject to the program and may "
                               "receive an EDCT. "
                               + ("None is assigned yet — schedule times "
                                  "still govern." if not edct.get("edct")
                                  else "Its EDCT is shown above."))
                              + (" Long block time gives en-route absorption "
                                 "capacity, so arrival impact is usually "
                                 "smaller than the program average."
                                 if long_haul else ""),
                    "severity": "WATCH" if not edct.get("edct") else "INFO",
                    "source": "faa_status",
                })
            elif at_origin:
                effects.append({
                    "cause": f"Ground delay program at {p_airport}"
                             + (f", avg delay {avg}" if avg else ""),
                    "effect": "A GDP meters flights ARRIVING INTO the origin "
                              "— it does not assign delays to this departure. "
                              "Expect indirect effects only: gate/taxi "
                              "congestion and late-arriving aircraft. The "
                              f"program average ({avg or 'n/a'}) is NOT this "
                              "flight's expected delay.",
                    "severity": "INFO", "source": "faa_status",
                })
        elif ptype == "ARR_DEP_DELAY":
            trend = p.get("trend") or ""
            effects.append({
                "cause": f"General delays at {p_airport}: "
                         f"{p.get('min_delay','')}-{p.get('max_delay','')}"
                         + (f", trend {trend}" if trend else ""),
                "effect": ("Departure-side delays at the origin apply to this "
                           "flight's taxi-out and release."
                           if at_origin else
                           "Arrival-side delays at the destination may add "
                           "airborne metering on arrival."),
                "severity": "WATCH" if "ncreas" in trend else "INFO",
                "source": "faa_status",
            })
        elif ptype == "CLOSURE":
            effects.append({
                "cause": f"Closure at {p_airport}",
                "effect": "Reduced capacity at the "
                          f"{where} airport — expect knock-on delays.",
                "severity": "WATCH", "source": "faa_status",
            })

    # --- Equipment ---------------------------------------------------------
    if isinstance(turn_analysis, dict):
        avail = turn_analysis.get("turn_time_available_min")
        minimum = turn_analysis.get("turn_time_required_min_minimum")
        standard = turn_analysis.get("turn_time_required_min_standard")
        cat = turn_analysis.get("aircraft_category", "aircraft")
        if avail is not None and minimum is not None:
            if avail < minimum:
                shortfall = int(minimum - avail)
                effects.append({
                    "cause": f"Inbound aircraft leaves only {avail:.0f} min "
                             f"of turn time ({cat} minimum: {minimum} min)",
                    "effect": f"Departure is effectively guaranteed to slip "
                              f"by at least ~{shortfall} min — the aircraft "
                              "physically cannot turn faster than the minimum.",
                    "severity": "ACTION", "source": "equipment_chain",
                })
            elif standard and avail < standard:
                effects.append({
                    "cause": f"Turn time {avail:.0f} min is below the "
                             f"{standard} min standard for a {cat}",
                    "effect": "Workable but with no buffer — any inbound slip "
                              "transfers directly to this departure.",
                    "severity": "WATCH", "source": "equipment_chain",
                })
            else:
                effects.append({
                    "cause": f"Turn time {avail:.0f} min vs {standard} min "
                             f"standard for a {cat}",
                    "effect": "Equipment is not a constraint. Moderate inbound "
                              "delays would be absorbed by the buffer.",
                    "severity": "INFO", "source": "equipment_chain",
                })

    return effects


# ---------------------------------------------------------------------------
# Predicted times
# ---------------------------------------------------------------------------

TAXI_OUT_DEFAULT_MIN = 20  # planning figure when no TFDM taxi estimate exists

UNCERTAINTY_BY_BAND = {
    "IMMINENT": 10, "NEAR": 20, "SAME_DAY": 45,
    "NEXT_DAY": 90, "DISTANT": None, "DEPARTED": 5, "UNKNOWN": None,
}


def _fmt(dt):
    return dt.isoformat() if dt else None


def _delta_min(a, b):
    if a and b:
        return round((a - b).total_seconds() / 60.0)
    return None


def predict_times(flight: dict, edct: dict, horizon: dict,
                  taxi_out_min: int = None) -> dict:
    """Deterministic gate/wheels-up/arrival estimates with stated basis.

    Precedence per event: actual > EDCT-derived (for departure legs) >
    airline estimate > schedule. Every figure carries `basis` so the UI and
    the LLM can say WHY, and `delay_vs_schedule_min` so 'leaves at X' reads
    as '+N vs schedule' too. Uncertainty widens with horizon; at DISTANT
    the honest answer is the schedule itself, flagged as such.
    """
    band = horizon.get("band", "UNKNOWN")
    unc = UNCERTAINTY_BY_BAND.get(band)
    taxi = taxi_out_min or TAXI_OUT_DEFAULT_MIN

    sched_out = parse_iso(flight.get("scheduled_out"))
    est_out = parse_iso(flight.get("estimated_out"))
    act_out = parse_iso(flight.get("actual_out"))
    sched_off = parse_iso(flight.get("scheduled_off"))
    est_off = parse_iso(flight.get("estimated_off"))
    act_off = parse_iso(flight.get("actual_off"))
    sched_on = parse_iso(flight.get("scheduled_on"))
    est_on = parse_iso(flight.get("estimated_on"))
    act_on = parse_iso(flight.get("actual_on"))
    sched_in = parse_iso(flight.get("scheduled_in"))
    est_in = parse_iso(flight.get("estimated_in"))
    act_in = parse_iso(flight.get("actual_in"))
    edct_dt = parse_iso(edct.get("edct")) if edct else None
    cta_dt = parse_iso(edct.get("cta")) if edct else None

    # --- Wheels-up (takeoff) ---------------------------------------------
    if act_off:
        off = {"time": _fmt(act_off), "basis": "actual takeoff", "status": "ACTUAL"}
    elif edct_dt:
        off = {"time": _fmt(edct_dt),
               "basis": "FAA-assigned EDCT (controlled wheels-up, -5/+5 min window)",
               "status": "CONTROLLED"}
    elif est_off:
        off = {"time": _fmt(est_off), "basis": "airline/FAA estimate",
               "status": "ESTIMATED"}
    elif est_out or sched_out:
        base = est_out or sched_out
        off = {"time": _fmt(base + timedelta(minutes=taxi)),
               "basis": f"gate estimate + {taxi} min taxi-out (planning figure)",
               "status": "DERIVED"}
    else:
        off = {"time": None, "basis": "no departure times available",
               "status": "UNKNOWN"}

    # --- Gate departure (off-block) --------------------------------------
    if act_out:
        out = {"time": _fmt(act_out), "basis": "actual gate departure",
               "status": "ACTUAL"}
    elif edct_dt:
        push = edct_dt - timedelta(minutes=taxi)
        if est_out and est_out > push:
            push = est_out
        out = {"time": _fmt(push),
               "basis": f"EDCT minus ~{taxi} min taxi (crews push to make "
                        "the wheels-up slot)",
               "status": "DERIVED"}
    elif est_out:
        out = {"time": _fmt(est_out), "basis": "airline estimate",
               "status": "ESTIMATED"}
    elif sched_out:
        out = {"time": _fmt(sched_out), "basis": "schedule (no live estimate)",
               "status": "SCHEDULED"}
    else:
        out = {"time": None, "basis": "no gate times available",
               "status": "UNKNOWN"}

    # --- Arrival ----------------------------------------------------------
    if act_in:
        arr = {"time": _fmt(act_in), "basis": "actual gate arrival",
               "status": "ACTUAL"}
    elif est_in:
        arr = {"time": _fmt(est_in), "basis": "airline/FAA estimate",
               "status": "ESTIMATED"}
    elif cta_dt:
        arr = {"time": _fmt(cta_dt), "basis": "FAA controlled arrival time",
               "status": "CONTROLLED"}
    elif est_on:
        arr = {"time": _fmt(est_on), "basis": "estimated touchdown",
               "status": "ESTIMATED"}
    elif sched_in:
        arr = {"time": _fmt(sched_in), "basis": "schedule (no live estimate)",
               "status": "SCHEDULED"}
    else:
        arr = {"time": None, "basis": "no arrival times available",
               "status": "UNKNOWN"}

    out["delay_vs_schedule_min"] = _delta_min(parse_iso(out["time"]), sched_out)
    off["delay_vs_schedule_min"] = _delta_min(parse_iso(off["time"]), sched_off)
    arr["delay_vs_schedule_min"] = _delta_min(parse_iso(arr["time"]), sched_in)

    return {
        "gate_departure": out,
        "takeoff": off,
        "gate_arrival": arr,
        "uncertainty_minutes": unc,
        "uncertainty_note": (f"±{unc} min at this horizon" if unc is not None
                             else "Too far out for meaningful precision — "
                                  "times shown are the schedule"),
        "edct": edct or None,
        "schedule_reference": {
            "scheduled_out": _fmt(sched_out), "scheduled_off": _fmt(sched_off),
            "scheduled_on": _fmt(sched_on), "scheduled_in": _fmt(sched_in),
        },
    }
