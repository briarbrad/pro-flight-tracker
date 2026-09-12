"""Flight status / chain / track / live."""

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

import store
import analysis
import flow_brief

from pft.runner import (
    CHECK_TIMEOUT, DEFAULT_TIMEOUT, SWIM_TIMEOUT,
    run_script, run_scripts_parallel, _status_prefetch_env,
)
from pft.validation import (
    _STRAY, _extract_flights, _is_conus,
    airport_list, clean_date, clean_duration, clean_ident, clean_int,
    clean_param, rekey_airports, to_faa, to_icao,
)
from pft.cache import (
    _annotated, _cache_get, _cache_key, _cache_put, _nocache_requested,
    _script_cache, _ttl_for,
)
from pft.breakers import _breaker_open, _breaker_record, _breaker_states
from pft.logging import log

bp = Blueprint("flight", __name__)


@bp.route("/api/flight/status")
def flight_status():
    """Get flight status from AeroAPI."""
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter"}), 400

    args = ["status", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("flight_data.py", args)
    return jsonify(data), status


@bp.route("/api/flight/chain")
def flight_chain():
    """Get equipment chain (inbound flight, tail, turn time)."""
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter"}), 400

    args = ["chain", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("flight_data.py", args, timeout=30)
    return jsonify(data), status


@bp.route("/api/flight/track")
def flight_track():
    """Get real-time aircraft position."""
    reg = clean_ident(request.args.get("reg", ""))
    flight = clean_ident(request.args.get("flight", ""))
    if not reg and not flight:
        return jsonify({"error": "Missing or invalid 'reg' or 'flight' parameter"}), 400

    args = ["track"]
    if reg:
        args += ["--reg", reg]
    elif flight:
        args += ["--flight", flight]

    data, status = run_script("flight_data.py", args)
    return jsonify(data), status


@bp.route("/api/flight/live")
def flight_live():
    """Phase, predicted times, taxi analysis, and a verdict from ONE status
    fetch — the endpoint the client's main refresh should hit.

    This exists because phase and predicted_times used to be computed only
    inside /api/brief (2-6 AeroAPI queries, SWIM JVM spawn, 10-60s), so the
    app literally could not update its phase/predicted-times cards without
    paying for a full brief — the root cause of the stale "In the air after
    Arrived" bug. Everything here is derived in-process from the single
    AeroAPI status payload: 1 paid query, ~1-3s.

    Same field shapes as the brief envelope (phase, predicted_times, taxi,
    verdict, simple_summary, status, impactMinutes, causes, outlook,
    refresh_after_seconds), so the client's existing decoders work unchanged.
    `outlook.applicable` is always false here — this tile has no forecast
    sources. The verdict is marked scope="status_only": no weather, FAA
    program, or equipment-chain sources are consulted at this price point —
    it can flag cancellations, diversions, slips, EDCTs, and taxi anomalies,
    but a LOW here is "nothing visible in status data," not "all clear".

    Query params:
      flight (required)  e.g. DL5187
      date   (optional)  YYYY-MM-DD, defaults to UTC today
      edct=cached        re-attach the last EDCT found by a brief (within
                         store.EDCT_TTL_MINUTES), so FAA-controlled times
                         survive cheap refreshes between brief runs
    """
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter",
                        "hint": "e.g. flight=DL244"}), 400
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    status_data, status_code = run_script(
        "flight_data.py", ["status", "--flight", flight, "--date", date],
        timeout=20)

    flights = _extract_flights(status_data) if status_code == 200 else []
    if not flights:
        return jsonify({
            "flight": flight, "date": date,
            "error": "No flight data available",
            "detail": (status_data.get("errors") or status_data.get("error")
                       if isinstance(status_data, dict) else None),
            "hint": "Check the flight number and date. AeroAPI only carries "
                    "roughly a 10-day forward window.",
            "aeroapi_queries_used": 1,
        }), 404

    primary = flights[0]

    # One timestamp for the whole response — same discipline as the brief.
    now = datetime.now(timezone.utc)
    phase = analysis.compute_phase(primary, now)
    horizon = analysis.compute_horizon(primary, now, phase)

    origin = primary.get("origin_icao")
    dest = primary.get("dest_icao")
    origin_tz = analysis.resolve_timezone(origin, primary.get("origin_timezone"))
    dest_tz = analysis.resolve_timezone(dest, primary.get("dest_timezone"))

    # EDCT: never looked up here (that's the brief's SWIM job), but a slot
    # the last brief found is still the controlling fact — re-attach it on
    # request. The cache TTLs out after EDCT_TTL_MINUTES because a revised
    # slot presented as authoritative is worse than no slot.
    edct = {}
    edct_cache_info = None
    if request.args.get("edct", "").lower() == "cached":
        cached = store.get_cached_edct(flight, date)
        if cached:
            edct = cached["payload"]
            edct_cache_info = {"attached": True, "cached_at": cached["cached_at"],
                               "ttl_minutes": store.EDCT_TTL_MINUTES}
        else:
            edct_cache_info = {"attached": False,
                               "note": "No fresh cached EDCT — run a brief to "
                                       "look one up via SWIM"}

    predictions = analysis.predict_times(primary, edct, horizon,
                                         origin_tz=origin_tz, dest_tz=dest_tz)
    phase = analysis.attach_next_event(phase, predictions, origin_tz, dest_tz,
                                       now)
    taxi = analysis.analyze_taxi(primary, phase, predictions, now)

    # Turn-time/equipment: never looked up here either (2 extra AeroAPI
    # queries), but re-attach a fresh finding from the last brief so this
    # tile can't contradict itself — previously this was always {}, so the
    # "no single cause was identified" note was true only because /live
    # had never checked, even when a brief had already found one minutes
    # earlier. Unlike EDCT this doesn't need an opt-in query param: it's
    # a cache read, not extra cost, so there's no reason to gate it.
    cached_turn = store.get_cached_turn_analysis(flight, date)
    turn_analysis = cached_turn["payload"] if cached_turn else {}

    # Verdict-lite: same assess() as the brief, fed only status-derived
    # inputs — no FAA programs, no weather effects, and turn analysis only
    # when a recent brief already found one (see cached_turn above).
    # ATFM is the one extra that's free here: it scores this same status
    # payload when the destination is European. No new AeroAPI query,
    # no SWIM, no G-AIRMET HTTP — those stay on /api/brief.
    plan = analysis.source_plan(horizon["hours_to_next_event"], phase)
    atfm = {}
    atfm_fx = []
    if dest and _mod_airport_ops._is_eurocontrol(dest):
        hours = horizon.get("hours_to_next_event")
        if hours is None or hours <= 12:
            atfm = _mod_airport_ops.infer_atfm_from_status(primary)
            atfm_fx = analysis.atfm_effects(atfm)
    else:
        plan["atfm"] = {
            "relevant": False,
            "reason": (f"Destination {dest} is not in Eurocontrol airspace"
                       if dest else "No destination"),
            "provides": "Eurocontrol CTOT heuristic",
        }
    branch = analysis.classify_branch(horizon, [], turn_analysis, plan, [])
    effects = (analysis.build_effects(primary, [], turn_analysis, edct, horizon)
               + analysis.taxi_effects(taxi, edct)
               + atfm_fx)
    verdict = analysis.assess(horizon, branch, turn_analysis, primary, effects,
                              taxi, phase)
    verdict["scope"] = "status_only"

    _sev = {"ACTION": 0, "WATCH": 1, "INFO": 2}
    effects.sort(key=lambda e: _sev.get(e.get("severity"), 3))

    simple_summary = analysis.build_simple_summary(
        phase=phase, horizon=horizon, verdict=verdict, effects=effects,
        predicted_times=predictions, taxi=taxi, branch=branch,
        origin=origin, dest=dest)
    # Status-only tile: no TAF / Open-Meteo / TCF, so outlook is never a
    # forecast. Client still receives the key with applicable: false.
    presentation = analysis.build_presentation(
        phase=phase, horizon=horizon, verdict=verdict, effects=effects,
        predicted_times=predictions, taxi=taxi, branch=branch,
        origin=origin, dest=dest, forecast_consulted=False)

    return jsonify({
        "flight": flight,
        "date": date,
        "generated_at": now.isoformat(),
        "fetched_at": (status_data.get("pull_time")
                       if isinstance(status_data, dict) else None)
                      or now.isoformat(),
        "phase": phase,
        "taxi": taxi,
        "horizon": horizon,
        "verdict": verdict,
        "simple_summary": simple_summary,
        "status": presentation["status"],
        "impactMinutes": presentation["impactMinutes"],
        "causes": presentation["causes"],
        "outlook": presentation["outlook"],
        "effects": effects,
        "predicted_times": predictions,
        "timezones": {"origin": origin_tz, "destination": dest_tz},
        "edct_cache": edct_cache_info,
        "turn_analysis_cache": ({"attached": True,
                                "cached_at": cached_turn["cached_at"],
                                "ttl_minutes": store.TURN_ANALYSIS_TTL_MINUTES}
                               if cached_turn else
                               {"attached": False,
                                "note": "No fresh cached turn-time finding — "
                                        "run a brief to check inbound "
                                        "equipment"}),
        "atfm": atfm or {"applicable": False},
        "delay_trend": _record_and_get_delay_trend(flight, date, primary,
                                                    verdict.get("departure_risk")),
        "refresh_after_seconds": analysis.refresh_interval(phase, horizon),
        "aeroapi_queries_used": 1,
    }), 200
