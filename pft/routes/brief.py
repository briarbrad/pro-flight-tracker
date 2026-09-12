"""Flight check and pre-departure brief."""

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
from pft.llm import (
    CHAT_MAX_MESSAGE_CHARS, CHAT_MAX_MESSAGES, CHAT_MAX_TOKENS,
    CHAT_REASONING_MAX_TOKENS, NARRATIVE_CACHE_TTL_SECONDS,
    NARRATIVE_FALLBACK_MODEL, NARRATIVE_MAX_TOKENS, NARRATIVE_MODEL,
    NARRATIVE_REASONING_MAX_TOKENS, OPENROUTER_API_URL,
    _chat_cache_key, _llm_usage_record, _narrative_cache,
    _narrative_cache_key, _openrouter_call_with_fallback,
    _openrouter_extract_text,
)
from pft.logging import log

bp = Blueprint("brief", __name__)


def _extract_origin_dest_icao(status_data, status_code):
    """Pull the origin/destination ICAO codes for the primary leg out of a
    flight_data.cmd_status() envelope.

    That envelope shape is {"data": {"flights": [...], "route": ...}, ...},
    and each entry in data.flights is the FLATTENED dict produced by
    flight_data._parse_aeroapi_flight(), which exposes flat "origin_icao"/
    "dest_icao" keys (not nested origin/destination objects, and not a
    "code_icao" field — see scripts/flight_data.py:_parse_aeroapi_flight).

    Pulled out of check_flight() as its own pure function so it can be unit
    tested directly (see tests/test_check_envelope.py) instead of only being
    exercisable through a full, network-dependent /api/check request.
    Returns (origin_icao, dest_icao), either of which may be None.
    """
    if status_code != 200 or not isinstance(status_data, dict):
        return None, None
    flights_list = (status_data.get("data") or {}).get("flights") or []
    if not flights_list:
        return None, None
    first_leg = flights_list[0] or {}
    return first_leg.get("origin_icao"), first_leg.get("dest_icao")


@bp.route("/api/check", methods=["GET", "POST"])
def check_flight():
    """Run a comprehensive flight check — pulls ALL data sources in parallel.

    Query params or JSON body:
      flight (required): e.g. "DL244"
      date (optional): e.g. "2026-08-16", defaults to today

    Returns a unified JSON object with all data source results, organized
    for the Rork app to render into the risk assessment UI.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        flight = body.get("flight") or request.args.get("flight")
        date = body.get("date") or request.args.get("date")
    else:
        flight = request.args.get("flight")
        date = request.args.get("date")

    flight = clean_ident(flight or "")
    date = clean_date(date or "")

    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter"}), 400

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Whole-request budget. Everything below — the phase-1 status fetch and
    # the merged parallel batch — must land inside this, or the stragglers
    # come back as per-source 504 entries instead of the request itself
    # brushing gunicorn's --timeout.
    deadline = time.monotonic() + CHECK_TIMEOUT

    # Normalize flight format: "DL244" → "DL244", but we need airline code
    # for SWIM (DAL244)
    airline_prefix = flight[:2].upper()
    flight_num = flight[2:]
    swim_callsign = _iata_to_icao_airline(airline_prefix) + flight_num

    # Tasks that don't depend on knowing origin/dest yet.
    # NOTE: flight_status and equipment_chain used to be listed here too, but
    # were sliced off by `tasks[2:]` below and run elsewhere — dead entries
    # that would have silently come back to life on any reorder.
    tasks = [
        # --- Weather (origin + dest will be filled from flight_status,
        #     but we can do a broad pull) ---
        {
            "key": "sigmet",
            "script": "aviation_weather.py",
            "args": ["sigmet"],
            "timeout": 15
        },
        # --- SWIM feeds ---
        {
            "key": "tfms_flow_gdp",
            "script": "swim_consumer.py",
            "args": ["tfms-flow", "--keyword", "GDP", "--duration", "12"],
            "timeout": SWIM_TIMEOUT
        },
        {
            "key": "tfms_flight",
            "script": "swim_consumer.py",
            "args": ["tfms-flight", "--flight", swim_callsign, "--duration", "12"],
            "timeout": SWIM_TIMEOUT
        },
    ]

    # Phase 1: Get flight status first (we need origin/dest for weather)
    status_data, status_code = run_script(
        "flight_data.py",
        ["status", "--flight", flight, "--date", date],
        timeout=20
    )

    # Extract origin/dest airports from flight status. See
    # _extract_origin_dest_icao() for why this must read data.flights[0]
    # with flat origin_icao/dest_icao keys rather than a top-level "flights"
    # key with nested "code_icao" fields — the old version of this block used
    # the latter, which don't exist anywhere in this envelope, so
    # origin_icao/dest_icao were always None and every airport-dependent
    # phase-2 task below was silently skipped.
    origin_icao, dest_icao = _extract_origin_dest_icao(status_data, status_code)

    # Phase 2: Now build weather + ops tasks with known airports
    phase2_tasks = []

    if origin_icao and dest_icao:
        # Weather for both airports
        phase2_tasks.extend([
            {
                "key": "metar",
                "script": "aviation_weather.py",
                "args": ["metar", "--icao", origin_icao, dest_icao],
                "timeout": 15
            },
            {
                "key": "taf",
                "script": "aviation_weather.py",
                "args": ["taf", "--icao", origin_icao, dest_icao],
                "timeout": 15
            },
            {
                "key": "pirep_origin",
                "script": "aviation_weather.py",
                "args": ["pirep", "--icao", origin_icao, "--distance", "200"],
                "timeout": 15
            },
            {
                "key": "faa_status",
                "script": "aviation_weather.py",
                "args": ["faa-status", "--icao", origin_icao, dest_icao],
                "timeout": 15
            },
            {
                "key": "gairmet",
                "script":"airport_ops.py",
                "args": ["gairmet", "--route", origin_icao, dest_icao],
                "timeout": 20
            },
            {
                "key": "tcf",
                "script": "airport_ops.py",
                "args": ["tcf", "--route", origin_icao, dest_icao],
                "timeout": 20
            },
            {
                "key": "rvr_origin",
                "script": "airport_ops.py",
                "args": ["rvr", "--airport", _icao_to_faa(origin_icao)],
                "timeout": 15
            },
            {
                "key": "lightning_origin",
                "script": "airport_ops.py",
                "args": ["lightning", "--icao", origin_icao, "--duration", "5"],
                "timeout": 20
            },
        ])

        # Domestic /airsigmet (already in the phase-1 "sigmet" task) doesn't
        # cover Alaska, Hawaii, or anywhere outside the contiguous US — only
        # pull the international feed when the route actually leaves it.
        if not _is_conus(origin_icao) or not _is_conus(dest_icao):
            phase2_tasks.append({
                "key": "isigmet",
                "script": "aviation_weather.py",
                "args": ["isigmet"],
                "timeout": 15,
            })

        # SWIM feeds for origin airport
        phase2_tasks.extend([
            {
                "key": "tbfm",
                "script": "swim_consumer.py",
                "args": ["tbfm", "--airport", dest_icao, "--duration", "10"],
                "timeout": SWIM_TIMEOUT
            },
            {
                "key": "itws_origin",
                "script": "swim_consumer.py",
                "args": ["itws", "--airport", origin_icao, "--duration", "10"],
                "timeout": SWIM_TIMEOUT
            },
        ])

        # ATFM for European destinations
        if dest_icao and len(dest_icao) == 4:
            prefix = dest_icao[0:2]
            if prefix in ("EG", "EI", "EH", "EB", "ED", "EK", "EE", "EF",
                          "EN", "EP", "ES", "ET", "EV", "EY",
                          "LF", "LI", "LE", "LP", "LG", "LH", "LJ",
                          "LK", "LO", "LR", "LT", "LZ", "LB", "LW",
                          "LC", "LD", "LM", "LN", "LS", "LU",
                          "BI", "GC", "GE", "UD", "UG", "UK"):
                phase2_tasks.append({
                    "key": "atfm",
                    "script": "airport_ops.py",
                    "args": ["atfm-infer", "--flight", flight, "--date", date],
                    "timeout": 20
                })

    # Also run equipment chain in parallel with phase 2.
    # Hand it the Phase 1 flight status so it doesn't re-buy /flights/{ident}
    # from AeroAPI — that saves one query on every single check.
    chain_env = _status_prefetch_env(status_data) if status_code == 200 else None

    phase2_tasks.append({
        "key": "equipment_chain",
        "script": "flight_data.py",
        "args": ["chain", "--flight", flight, "--date", date],
        "timeout": 30,
        "env_extras": chain_env,
    })

    # Run everything in ONE parallel batch under the whole-request budget.
    # (These used to be two serialized run_scripts_parallel calls — the
    # airport-independent SWIM/sigmet tasks waited for all of phase 2 to
    # finish before even starting, so worst case was 20s + 45s + 45s against
    # gunicorn's 120s. Keys don't collide across the two lists.)
    all_results = run_scripts_parallel(phase2_tasks + tasks, max_workers=10,
                                       deadline=deadline)

    # Merge everything into a unified response
    response = {
        "flight": flight,
        "date": date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fetched_at": (status_data.get("pull_time")
                       if isinstance(status_data, dict) else None)
                      or datetime.now(timezone.utc).isoformat(),
        "origin_icao": origin_icao,
        "destination_icao": dest_icao,
        "data": {
            "flight_status": status_data if status_code == 200 else {"error": status_data},
        }
    }

    for key, result in all_results.items():
        response["data"][key] = result["data"] if result["status"] == 200 else {"error": result["data"]}

    return jsonify(response), 200


@bp.route("/api/brief", methods=["GET", "POST"])
def flight_brief():
    """Horizon-aware analysis for one flight, plus a ready-to-send LLM prompt.

    Unlike /api/check, this does NOT fan out to everything. It resolves how far
    away the departure is, then consults only the sources that still carry
    signal at that horizon. A flight 15 hours out doesn't pay for live surface
    feeds or an equipment chain that isn't assigned yet — which makes this both
    cheaper and more accurate than the aggregate endpoint.

    Returns a deterministic verdict AND `llm_payload`, so the client can send
    the synthesis step to whatever model it wants. All arithmetic is done here;
    the model never computes anything.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        flight = clean_ident(body.get("flight") or request.args.get("flight", ""))
        date = body.get("date") or request.args.get("date")
    else:
        flight = clean_ident(request.args.get("flight", ""))
        date = request.args.get("date")

    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter",
                        "hint": "e.g. flight=DL244"}), 400
    date = clean_date(date or "")
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    aeroapi_queries = 0
    sources = {}

    # --- Step 1: flight status. Always needed; it's what dates the horizon.
    status_data, status_code = run_script(
        "flight_data.py", ["status", "--flight", flight, "--date", date],
        timeout=20)
    aeroapi_queries += 1  # route is opt-in now and the brief doesn't buy it

    flights = _extract_flights(status_data) if status_code == 200 else []
    if not flights:
        return jsonify({
            "flight": flight, "date": date,
            "error": "No flight data available",
            "detail": (status_data.get("errors") or status_data.get("error")
                       if isinstance(status_data, dict) else None),
            "hint": "Check the flight number and date. AeroAPI only carries "
                    "roughly a 10-day forward window.",
            "aeroapi_queries_used": aeroapi_queries,
        }), 404

    primary = flights[0]
    sources["flight_status"] = {"status": "ok", "relevance": "PRIMARY",
                                "data": primary}

    # --- Step 2: phase, then horizon, decide everything downstream.
    # Phase first: gating on time-to-gate-departure alone meant a flight that
    # had pushed back but not taken off had every live source switched off,
    # which is exactly backwards — a 3-hour taxi queue is when EDCT, ground
    # stops and surface congestion matter most.
    # One timestamp for the whole brief. Elapsed-taxi, time-to-next-event and
    # the horizon band all have to agree with each other; recomputing "now"
    # per call would let them drift apart within a single response.
    now = datetime.now(timezone.utc)
    phase = analysis.compute_phase(primary, now)
    horizon = analysis.compute_horizon(primary, now, phase)
    plan = analysis.source_plan(horizon["hours_to_next_event"], phase)

    origin = primary.get("origin_icao")
    dest = primary.get("dest_icao")
    airports = [a for a in (origin, dest) if a]

    # ATFM is in SOURCE_HORIZON (12h) but only means something for a
    # European arrival. Override the plan so sources_excluded says so
    # instead of pretending we consulted Eurocontrol for KJFK–KLAX.
    if dest and not _mod_airport_ops._is_eurocontrol(dest):
        plan["atfm"] = {
            "relevant": False,
            "reason": f"Destination {dest} is not in Eurocontrol airspace",
            "provides": plan.get("atfm", {}).get("provides",
                "Eurocontrol CTOT heuristic"),
        }

    # --- Step 3: fetch only what the horizon justifies.
    tasks = []
    if plan["taf"]["relevant"] and airports:
        tasks.append({"key": "taf", "script": "aviation_weather.py",
                      "args": ["taf", "--icao"] + airports, "timeout": 15})
    if plan["faa_status"]["relevant"] and airports:
        tasks.append({"key": "faa_status", "script": "aviation_weather.py",
                      "args": ["faa-status", "--icao"] + airports, "timeout": 15})
    if plan["metar"]["relevant"] and airports:
        tasks.append({"key": "metar", "script": "aviation_weather.py",
                      "args": ["metar", "--icao"] + airports, "timeout": 15})
    if plan["sigmet"]["relevant"]:
        tasks.append({"key": "sigmet", "script": "aviation_weather.py",
                      "args": ["sigmet"], "timeout": 15})
    if plan["isigmet"]["relevant"] and airports and (not _is_conus(origin) or not _is_conus(dest)):
        # Domestic /airsigmet doesn't cover Alaska, Hawaii, or anywhere
        # outside the contiguous US — only fetch this extra call when the
        # route actually leaves that coverage area.
        tasks.append({"key": "isigmet", "script": "aviation_weather.py",
                      "args": ["isigmet"], "timeout": 15})
    if plan["gairmet"]["relevant"] and origin and dest:
        tasks.append({"key": "gairmet", "script": "airport_ops.py",
                      "args": ["gairmet", "--route", origin, dest], "timeout": 20})
    if plan["tcf"]["relevant"] and origin and dest:
        tasks.append({"key": "tcf", "script": "airport_ops.py",
                      "args": ["tcf", "--route", origin, dest], "timeout": 20})
    if plan["tfms_flow"]["relevant"]:
        tasks.append({"key": "tfms_flow", "script": "swim_consumer.py",
                      "args": ["tfms-flow", "--keyword", "GDP", "--duration", "10"],
                      "timeout": SWIM_TIMEOUT})
    if plan["lightning"]["relevant"] and origin:
        tasks.append({"key": "lightning", "script": "airport_ops.py",
                      "args": ["lightning", "--icao", origin, "--duration", "5"],
                      "timeout": 20})
    if plan["rvr"]["relevant"] and origin:
        tasks.append({"key": "rvr", "script": "airport_ops.py",
                      "args": ["rvr", "--airport", to_faa(origin)], "timeout": 15})
    if horizon.get("band") in ("SAME_DAY", "NEXT_DAY", "DISTANT") and airports:
        # Free, fast, no key. TAF is the official product when it covers
        # the window; this is labelled model_guidance for the gap beyond.
        tasks.append({"key": "open_meteo", "script": "aviation_weather.py",
                      "args": ["open-meteo", "--icao"] + airports,
                      "timeout": 12})
    if plan["position"]["relevant"]:
        # Answers "where is it and is it actually moving" once the aircraft
        # is out of the gate. ADS-B and OpenSky are free and tried first;
        # the AeroAPI fallback only fires when both miss, and gets the
        # prefetched status so it costs one query rather than two.
        pos_args = ["track", "--flight", flight]
        if primary.get("registration"):
            pos_args += ["--reg", primary["registration"]]
        pos_env = _status_prefetch_env(status_data)
        tasks.append({"key": "position", "script": "flight_data.py",
                      "args": pos_args, "timeout": 20,
                      "env_extras": pos_env})

    if tasks:
        for key, result in run_scripts_parallel(tasks, max_workers=6).items():
            ok = result["status"] == 200
            sources[key] = {
                "status": "ok" if ok else "error",
                "relevance": "RELEVANT",
                "provides": plan.get(key, {}).get("provides"),
                "data": result["data"] if ok else None,
                "error": None if ok else result["data"],
            }

    # --- Step 4: equipment chain, only when it's actually knowable.
    turn_analysis = {}
    if plan["equipment_chain"]["relevant"]:
        chain_env = _status_prefetch_env(status_data)
        chain_data, chain_code = run_script(
            "flight_data.py", ["chain", "--flight", flight, "--date", date],
            timeout=30, env_extras=chain_env)
        aeroapi_queries += 2  # status is prefetched; inbound + position remain
        if chain_code == 200 and isinstance(chain_data, dict):
            inner = chain_data.get("data") or {}
            turn_analysis = inner.get("turn_analysis") or {}
            sources["equipment_chain"] = {"status": "ok", "relevance": "PRIMARY",
                                          "provides": plan["equipment_chain"]["provides"],
                                          "data": inner}
            # Write-through so /api/flight/live (which never affords this
            # lookup itself) and the tracker's background poll can re-attach
            # this finding instead of staying blind to it — same pattern as
            # the EDCT write-through a few lines below.
            store.cache_turn_analysis(flight, date, turn_analysis)
        else:
            sources["equipment_chain"] = {"status": "error", "relevance": "PRIMARY",
                                          "error": chain_data}

    # --- Step 4b: EDCT lookup via SWIM, when close enough to matter.
    # EDCTs are assigned same-day by traffic management; past ~6h out there
    # is nothing to find. Free call (SWIM is subscription, not per-query).
    edct = {}
    if plan["tfms_flow"]["relevant"]:
        swim_callsign = _iata_to_icao_airline(flight[:2]) + flight[2:]
        tfms_data, tfms_code = run_script(
            "swim_consumer.py",
            ["tfms-flight", "--flight", swim_callsign, "--duration", "8"],
            timeout=SWIM_TIMEOUT)
        if tfms_code == 200 and isinstance(tfms_data, dict):
            edct = analysis.extract_edct(tfms_data.get("results", []),
                                         swim_callsign)
            # Write-through so /api/flight/live?edct=cached can re-attach
            # this slot on cheap refreshes without another SWIM lookup.
            store.cache_edct(flight, date, edct)
            sources["tfms_edct"] = {
                "status": "ok", "relevance": "PRIMARY",
                "provides": "FAA-assigned EDCT / controlled times",
                "data": edct or {"edct": None,
                                 "note": "No EDCT assigned to this flight "
                                         "(normal unless captured by a "
                                         "traffic management program)"},
            }

    # --- Step 5: deterministic analysis.
    origin_tz = analysis.resolve_timezone(origin, primary.get("origin_timezone"))
    dest_tz = analysis.resolve_timezone(dest, primary.get("dest_timezone"))

    # Ground the equipment/turn-time facts in real clock times now that the
    # origin timezone is known. The inbound flight lands at THIS flight's
    # origin airport, so origin_tz is correct for both timestamps. Without
    # this, the facts only ever carried relative minute deltas — leaving the
    # LLM nothing to cite but a blank, which it was filling in with
    # invented clock times (see the new SYNTHESIS_RULES entry).
    if turn_analysis.get("inbound_eta"):
        turn_analysis["inbound_eta_local"] = analysis.to_local(
            turn_analysis["inbound_eta"], origin_tz)["display"]
    if turn_analysis.get("outbound_scheduled_departure"):
        turn_analysis["outbound_scheduled_departure_local"] = analysis.to_local(
            turn_analysis["outbound_scheduled_departure"], origin_tz)["display"]

    programs = analysis._programs_from_faa(
        (sources.get("faa_status") or {}).get("data"))

    # Predictions first: the phase's next_event and the TAF windows both key
    # off them, and they're what carries the EDCT into everything downstream.
    predictions = analysis.predict_times(primary, edct, horizon,
                                         origin_tz=origin_tz, dest_tz=dest_tz)
    phase = analysis.attach_next_event(phase, predictions, origin_tz, dest_tz,
                                       now)

    # What the aircraft is physically doing, and whether that's abnormal.
    taxi = analysis.analyze_taxi(primary, phase, predictions, now)
    position = analysis.describe_position(
        (sources.get("position") or {}).get("data"), phase)

    # Terminal forecast across the actual departure and arrival windows.
    # Beyond ~6h out this is the only source still carrying signal, so it has
    # to reach the verdict — not just the narrative.
    #
    # The departure window centres on predicted TAKEOFF, not gate departure:
    # a flight that pushed back at 23:20 and takes off at 02:28 meets an
    # entirely different TAF period than the one covering its pushback.
    taf_payload = (sources.get("taf") or {}).get("data")
    dep_ref = (analysis.parse_iso((predictions.get("takeoff") or {}).get("time"))
               or analysis.parse_iso(primary.get("estimated_out"))
               or analysis.parse_iso(primary.get("scheduled_out")))
    arr_ref = (analysis.parse_iso((predictions.get("gate_arrival") or {}).get("time"))
               or analysis.parse_iso(primary.get("estimated_in"))
               or analysis.parse_iso(primary.get("scheduled_in")))
    taf_windows = {}
    weather_effects = []
    if taf_payload and dep_ref:
        dep_taf = analysis.analyze_taf(
            taf_payload, origin,
            dep_ref - timedelta(minutes=60), dep_ref + timedelta(minutes=60),
            origin_tz)
        taf_windows["departure"] = dep_taf
        weather_effects += analysis.taf_effects(dep_taf, "departure")
    if taf_payload and arr_ref:
        arr_taf = analysis.analyze_taf(
            taf_payload, dest,
            arr_ref - timedelta(minutes=60), arr_ref + timedelta(minutes=60),
            dest_tz)
        taf_windows["arrival"] = arr_taf
        weather_effects += analysis.taf_effects(arr_taf, "arrival")

    # ATFM heuristic is free: it scores the status payload we already have.
    # Never call atfm-infer here — that re-buys AeroAPI.
    atfm = {}
    if plan.get("atfm", {}).get("relevant"):
        atfm = _mod_airport_ops.infer_atfm_from_status(primary)
        sources["atfm"] = {
            "status": "ok",
            "relevance": "RELEVANT",
            "provides": plan["atfm"].get("provides"),
            "data": atfm,
        }

    gairmet_fx = analysis.gairmet_effects(
        (sources.get("gairmet") or {}).get("data"))
    atfm_fx = analysis.atfm_effects(atfm)

    om_payload = (sources.get("open_meteo") or {}).get("data")
    extended_weather = None
    if isinstance(om_payload, dict) and om_payload.get("data"):
        extended_weather = {
            "label": "model_guidance",
            "source": "open-meteo",
            "note": om_payload.get("note") or getattr(
                _mod_aviation_weather, "OPEN_METEO_NOTE", ""),
            "airports": om_payload.get("data"),
        }

    branch = analysis.classify_branch(horizon, programs, turn_analysis, plan,
                                      weather_effects)
    effects = (analysis.build_effects(primary, programs, turn_analysis,
                                      edct, horizon)
               + weather_effects
               + gairmet_fx
               + atfm_fx
               + analysis.taxi_effects(taxi, edct)
               + analysis.position_effects(position, phase))
    verdict = analysis.assess(horizon, branch, turn_analysis, primary, effects,
                              taxi, phase)

    # Severity order so the client can render top-down without re-sorting.
    _sev = {"ACTION": 0, "WATCH": 1, "INFO": 2}
    effects.sort(key=lambda e: _sev.get(e.get("severity"), 3))

    simple_summary = analysis.build_simple_summary(
        phase=phase, horizon=horizon, verdict=verdict, effects=effects,
        predicted_times=predictions, taxi=taxi, branch=branch,
        origin=origin, dest=dest)
    presentation = analysis.build_presentation(
        phase=phase, horizon=horizon, verdict=verdict, effects=effects,
        predicted_times=predictions, taxi=taxi, branch=branch,
        origin=origin, dest=dest,
        taf_windows=taf_windows,
        extended_weather=extended_weather,
        tcf=(sources.get("tcf") or {}).get("data"),
        gairmet=(sources.get("gairmet") or {}).get("data"),
        sigmet=(sources.get("sigmet") or {}).get("data"),
        isigmet=(sources.get("isigmet") or {}).get("data"),
        forecast_consulted=True)

    excluded = {k: v["reason"] for k, v in plan.items() if not v["relevant"]}
    if extended_weather is None and horizon.get("band") not in (
            "SAME_DAY", "NEXT_DAY", "DISTANT"):
        excluded.setdefault(
            "open_meteo",
            "Near-term TAF/METAR still cover this horizon — Open-Meteo "
            "model guidance is folded in at SAME_DAY and longer.")

    payload = analysis.build_llm_payload(flight, date, horizon, plan, branch,
                                         verdict, sources)
    # The model gets the computed effects and predictions as facts, plus a
    # rule to report rather than re-derive them.
    payload["facts"]["effects"] = effects
    payload["facts"]["predicted_times"] = predictions
    payload["facts"]["simple_summary"] = simple_summary
    payload["facts"]["status"] = presentation["status"]
    payload["facts"]["impactMinutes"] = presentation["impactMinutes"]
    payload["facts"]["causes"] = presentation["causes"]
    payload["facts"]["outlook"] = presentation["outlook"]
    payload["facts"]["taf_windows"] = taf_windows
    payload["facts"]["phase"] = phase
    payload["facts"]["taxi"] = taxi
    payload["facts"]["position"] = position
    if atfm:
        payload["facts"]["atfm"] = atfm
    if extended_weather:
        payload["facts"]["extended_weather"] = extended_weather
        payload["guardrails"].append(
            "extended_weather is Open-Meteo model guidance, not a TAF. "
            "Never invent VFR/IFR categories from it, and never let it "
            "override a TAF that covers the same window.")
    payload["guardrails"].append(
        "Predicted gate/takeoff/arrival times and any EDCT are already "
        "computed and included in the facts. Report them with their stated "
        "basis and uncertainty; never derive alternative times.")
    payload["guardrails"].append(
        "`phase` is where the aircraft physically is right now. Write about "
        "what happens NEXT from that phase — for a taxiing flight the "
        "question is wheels-up, not pushback. Never describe a flight that "
        "has left the gate as still waiting to depart, and never call a "
        "flight 'departed' when it is holding on the ground.")
    payload["system"] += (
        "\n- Predicted gate/takeoff/arrival times and any EDCT are already "
        "computed and included in the facts. Report them with their stated "
        "basis and uncertainty; never derive alternative times."
        "\n- `phase` is where the aircraft physically is right now. Write "
        "about what happens NEXT from that phase — for a taxiing flight the "
        "question is wheels-up, not pushback. Never describe a flight that "
        "has left the gate as still waiting to depart, and never call a "
        "flight 'departed' when it is holding on the ground.")

    return jsonify({
        "flight": flight,
        "date": date,
        "generated_at": now.isoformat(),
        "fetched_at": (status_data.get("pull_time")
                       if isinstance(status_data, dict) else None)
                      or now.isoformat(),
        "phase": phase,
        "taxi": taxi,
        "position": position,
        "horizon": horizon,
        "verdict": verdict,
        "simple_summary": simple_summary,
        "status": presentation["status"],
        "impactMinutes": presentation["impactMinutes"],
        "causes": presentation["causes"],
        "outlook": presentation["outlook"],
        "effects": effects,
        "predicted_times": predictions,
        "taf_windows": taf_windows,
        "atfm": atfm or {"applicable": False},
        "extended_weather": extended_weather,
        "timezones": {"origin": origin_tz, "destination": dest_tz},
        "branch_classification": branch,
        "sources_consulted": sorted(k for k, v in sources.items()
                                    if v.get("status") == "ok"),
        "sources_excluded": excluded,
        "sources": sources,
        "llm_payload": payload,
        "delay_trend": _record_and_get_delay_trend(flight, date, primary,
                                                    verdict.get("departure_risk")),
        "refresh_after_seconds": analysis.refresh_interval(phase, horizon),
        "aeroapi_queries_used": aeroapi_queries,
    }), 200
