"""Airport ops: GAIRMET, TCF, lightning, RVR, ATFM."""

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
    _STRAY, _extract_flights, _iata_to_icao_airline, _is_conus,
    airport_list, clean_date, clean_duration, clean_ident, clean_int,
    clean_param, rekey_airports, to_faa, to_icao,
)
from pft.cache import (
    _annotated, _cache_get, _cache_key, _cache_put, _nocache_requested,
    _script_cache, _ttl_for,
)
from pft.breakers import _breaker_open, _breaker_record, _breaker_states
from pft.logging import log

bp = Blueprint("ops", __name__)


@bp.route("/api/ops/gairmet")
def ops_gairmet():
    """Get G-AIRMET turbulence forecasts along a route."""
    route = request.args.get("route", "")
    hazard = request.args.get("hazard", "")

    args = ["gairmet"]
    if route:
        airports, _ = airport_list(route)
        if airports:
            args += ["--route"] + airports
    if hazard:
        hazards = [s.strip() for s in hazard.split(",")]
        args += ["--hazard"] + hazards

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@bp.route("/api/ops/tcf")
def ops_tcf():
    """Get the TFM Convective Forecast — thunderstorm coverage/confidence
    driving FAA ground stops and reroutes, 2-6h out."""
    route = request.args.get("route", "")

    args = ["tcf"]
    if route:
        airports, _ = airport_list(route)
        if airports and len(airports) == 2:
            args += ["--route"] + airports

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@bp.route("/api/ops/lightning")
def ops_lightning():
    """Get real-time lightning strikes near an airport."""
    icao = to_icao(request.args.get("icao", ""))
    radius = clean_duration(request.args.get("radius", ""), 20, lo=1, hi=250)
    duration = clean_duration(request.args.get("duration", ""), 10)
    if not icao:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Airport code, e.g. icao=JFK or icao=KJFK.",
        }), 400

    args = ["lightning", "--icao", icao, "--radius", radius, "--duration", duration]
    data, status = run_script("airport_ops.py", args, timeout=int(duration) + 15)
    return jsonify(data), status


@bp.route("/api/ops/rvr")
def ops_rvr():
    """Get per-runway visual range from FAA sensors."""
    # RVR is the one feed that wants the 3-letter FAA code, not ICAO.
    airport = to_faa(request.args.get("airport", "") or request.args.get("icao", ""))
    if not airport:
        return jsonify({
            "error": "Missing or invalid 'airport' parameter",
            "hint": "Airport code, e.g. airport=JFK. 'KJFK' and 'icao=' also accepted.",
        }), 400

    args = ["rvr", "--airport", airport]
    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@bp.route("/api/ops/atfm")
def ops_atfm():
    """Infer Eurocontrol ATFM regulation from delay patterns."""
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter"}), 400

    args = ["atfm-infer", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@bp.route("/api/ops/flow-brief")
def ops_flow_brief():
    """Interpreted ATC flow brief from TFMS-flow + TBFM + TFDM.

    Pulls the three SWIM feeds in parallel with a hard wall-clock cap.
    Quiet / undeployed / non-US feeds return empty structures (HTTP 200),
    never a 500. The client can render `advisories`, `metering`, `surface`,
    and `effects` without re-deriving meaning.

    Query params (all optional except at least one of flight / origin / dest):
      flight   e.g. DL244 — used for TBFM/TFDM callsign match and, if
               origin/dest are omitted, one AeroAPI status lookup
      date     YYYY-MM-DD (only needed when resolving airports from flight)
      origin / dest / departure / destination / arrival
      duration SWIM listen seconds, clamped 4–12 (default 8)
    """
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    origin = to_icao(request.args.get("origin", "")
                     or request.args.get("departure", ""))
    dest = to_icao(request.args.get("dest", "")
                   or request.args.get("destination", "")
                   or request.args.get("arrival", ""))
    duration = int(clean_duration(request.args.get("duration", ""), 8,
                                  lo=4, hi=12))

    if not flight and not origin and not dest:
        return jsonify({
            "error": "Need at least one of flight, origin, or dest",
            "hint": "e.g. flight=DL244&origin=KJFK&dest=EGLL  "
                    "(LHR and KLHR both resolve to EGLL)",
        }), 400

    aeroapi_queries = 0
    if (not origin or not dest) and flight:
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        status_data, status_code = run_script(
            "flight_data.py", ["status", "--flight", flight, "--date", date],
            timeout=20)
        aeroapi_queries = 1
        resolved_o, resolved_d = _extract_origin_dest_icao(status_data, status_code)
        origin = origin or resolved_o
        dest = dest or resolved_d

    if not origin and not dest:
        return jsonify({
            "error": "Could not resolve origin or destination",
            "hint": "Pass origin= and dest= (ICAO or IATA) or a flight that "
                    "AeroAPI can resolve.",
            "aeroapi_queries_used": aeroapi_queries,
        }), 400

    swim_callsign = ""
    if flight:
        swim_callsign = _iata_to_icao_airline(flight[:2]) + flight[2:]

    # Per-script timeout must exceed --duration by JVM startup (~10-15s).
    # The request-level deadline is what keeps the endpoint from hanging.
    per_timeout = duration + 14
    deadline = time.monotonic() + max(18, duration + 16)

    tasks = []
    sources_tried = []
    sources_skipped = []

    if origin and flow_brief.is_us_nas(origin):
        tasks.append({
            "key": "tfms_flow_origin",
            "script": "swim_consumer.py",
            "args": ["tfms-flow", "--airport", origin,
                     "--duration", str(duration), "--limit", "80"],
            "timeout": per_timeout,
        })
        sources_tried.append("tfms-flow:origin")
    elif origin:
        sources_skipped.append("tfms-flow:origin")

    if dest and flow_brief.is_us_nas(dest) and dest != origin:
        tasks.append({
            "key": "tfms_flow_dest",
            "script": "swim_consumer.py",
            "args": ["tfms-flow", "--airport", dest,
                     "--duration", str(duration), "--limit", "80"],
            "timeout": per_timeout,
        })
        sources_tried.append("tfms-flow:dest")
    elif dest and dest != origin:
        sources_skipped.append("tfms-flow:dest")

    if dest and flow_brief.is_us_nas(dest):
        tbfm_args = ["tbfm", "--airport", dest,
                     "--duration", str(duration), "--limit", "50"]
        if swim_callsign:
            tbfm_args += ["--flight", swim_callsign]
        tasks.append({
            "key": "tbfm",
            "script": "swim_consumer.py",
            "args": tbfm_args,
            "timeout": per_timeout,
        })
        sources_tried.append("tbfm")
    else:
        sources_skipped.append("tbfm")

    if origin and flow_brief.is_tfdm_airport(origin):
        tfdm_args = ["tfdm", "--airport", origin,
                     "--duration", str(duration), "--limit", "80"]
        if swim_callsign:
            tfdm_args += ["--flight", swim_callsign]
        tasks.append({
            "key": "tfdm_origin",
            "script": "swim_consumer.py",
            "args": tfdm_args,
            "timeout": per_timeout,
        })
        sources_tried.append("tfdm:origin")
    else:
        sources_skipped.append("tfdm:origin")

    if dest and dest != origin and flow_brief.is_tfdm_airport(dest):
        tfdm_d_args = ["tfdm", "--airport", dest,
                       "--duration", str(duration), "--limit", "80"]
        if swim_callsign:
            tfdm_d_args += ["--flight", swim_callsign]
        tasks.append({
            "key": "tfdm_dest",
            "script": "swim_consumer.py",
            "args": tfdm_d_args,
            "timeout": per_timeout,
        })
        sources_tried.append("tfdm:dest")
    elif dest and dest != origin:
        sources_skipped.append("tfdm:dest")

    t0 = time.monotonic()
    raw = run_scripts_parallel(tasks, max_workers=6, deadline=deadline) if tasks else {}
    timings = {"total": round(time.monotonic() - t0, 2)}

    swim = {}
    sources_quiet = list(sources_skipped)
    for key, result in raw.items():
        # run_scripts_parallel doesn't return per-task elapsed; approximate
        # from the batch. Individual keys still get an entry.
        timings[key] = timings.get("total")
        payload = result.get("data") if isinstance(result, dict) else None
        swim[key] = payload if isinstance(payload, dict) else {"results": []}
        results = (payload or {}).get("results") if isinstance(payload, dict) else None
        errored = (isinstance(payload, dict) and payload.get("error")
                   and not results)
        empty = not results
        if result.get("status") != 200 or errored or empty:
            if key not in sources_quiet:
                sources_quiet.append(key)

    # Alias keys the assembler looks for.
    if "tfdm_origin" in swim and "tfdm" not in swim:
        swim["tfdm"] = swim["tfdm_origin"]

    generated = datetime.now(timezone.utc).isoformat()
    brief = flow_brief.assemble_flow_brief(
        flight=flight or None,
        date=date or None,
        origin=origin or None,
        dest=dest or None,
        swim=swim,
        timings=timings,
        sources_tried=sources_tried,
        sources_quiet=sources_quiet,
        aeroapi_queries_used=aeroapi_queries,
    )
    brief["generated_at"] = generated
    brief["duration_seconds"] = duration
    brief["sources_skipped"] = sources_skipped
    return jsonify(brief), 200
