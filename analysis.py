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

from datetime import datetime, timezone
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
        weather_driven = [p for p in programs if _reason_is_weather(p.get("reason"))]
        structural = [p for p in programs if not _reason_is_weather(p.get("reason"))]
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
