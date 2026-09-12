"""Aviation weather endpoints."""

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

bp = Blueprint("weather", __name__)


def _airport_weather(command: str):
    """Shared handler for the ICAO-keyed weather endpoints."""
    codes, mapping = airport_list(request.args.get("icao", ""))
    if not codes:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Airport code(s), comma-separated. Both 'JFK' and 'KJFK' work.",
        }), 400

    data, status = run_script("aviation_weather.py", [command, "--icao"] + codes)
    return jsonify(rekey_airports(data, mapping)), status


@bp.route("/api/weather/metar")
def weather_metar():
    """Get METAR observations."""
    return _airport_weather("metar")


@bp.route("/api/weather/taf")
def weather_taf():
    """Get TAF terminal forecasts."""
    return _airport_weather("taf")


@bp.route("/api/weather/sigmet")
def weather_sigmet():
    """Get SIGMETs and Convective SIGMETs."""
    sig_type = request.args.get("type", "")
    args = ["sigmet"]
    if sig_type:
        args += ["--type", sig_type]

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@bp.route("/api/weather/isigmet")
def weather_isigmet():
    """Get international SIGMETs — Alaska, Hawaii/Pacific, and non-US FIRs,
    which the domestic /api/weather/sigmet endpoint doesn't cover."""
    hazard = request.args.get("hazard", "")
    args = ["isigmet"]
    if hazard:
        args += ["--hazard", hazard]

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@bp.route("/api/weather/pirep")
def weather_pirep():
    """Get PIREPs near an airport."""
    codes, mapping = airport_list(request.args.get("icao", ""))
    distance = clean_duration(request.args.get("distance", ""), 200, lo=1, hi=500)
    if not codes:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Airport code, e.g. icao=JFK or icao=KJFK.",
        }), 400

    args = ["pirep", "--icao"] + codes + ["--distance", distance]
    data, status = run_script("aviation_weather.py", args)
    return jsonify(rekey_airports(data, mapping)), status


@bp.route("/api/weather/faa-status")
def weather_faa_status():
    """Get FAA delay programs (GDP, ground stops, etc.)."""
    codes, mapping = airport_list(request.args.get("icao", ""))
    if not codes:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Airport code(s), comma-separated. Both 'JFK' and 'KJFK' work.",
        }), 400

    data, status = run_script("aviation_weather.py", ["faa-status", "--icao"] + codes)
    return jsonify(rekey_airports(data, mapping)), status


@bp.route("/api/weather/brief")
def weather_brief():
    """Get a full weather briefing for a route."""
    origin = to_icao(request.args.get("origin", ""))
    dest = to_icao(request.args.get("dest", ""))
    if not origin or not dest:
        return jsonify({
            "error": "Missing or invalid 'origin' or 'dest' parameter",
            "hint": "Airport codes, e.g. origin=JFK&dest=LAX. Both 'JFK' and 'KJFK' work.",
        }), 400

    args = ["brief", "--origin", origin, "--dest", dest]
    data, status = run_script("aviation_weather.py", args, timeout=30)
    return jsonify(data), status


@bp.route("/api/weather/open-meteo")
def weather_open_meteo():
    """Open-Meteo model guidance for one or more airports. No API key.

    Labelled `model_guidance` — not a TAF. Does not invent VFR/IFR
    categories. Useful when the official TAF is thin or the horizon is
    past TAF coverage.
    """
    codes, mapping = airport_list(request.args.get("icao", ""))
    hours = int(clean_duration(request.args.get("hours", ""), 12, lo=1, hi=48))
    if not codes:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Airport code(s), comma-separated. Both 'JFK' and 'KJFK' "
                    "work; 'LHR' resolves to EGLL.",
        }), 400

    data, status = run_script(
        "aviation_weather.py",
        ["open-meteo", "--icao"] + codes + ["--hours", str(hours)],
        timeout=15)
    return jsonify(rekey_airports(data, mapping)), status
