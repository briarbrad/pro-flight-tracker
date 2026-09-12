"""FAA SWIM feeds."""

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

bp = Blueprint("swim", __name__)


def _swim_call(feed: str, default_duration: int, airport_required: bool = False,
               allow_flight: bool = False, allow_keyword: bool = False):
    """Shared handler for the SWIM endpoints. Sanitizes every query param."""
    airport = clean_ident(request.args.get("airport", ""))
    flight = clean_ident(request.args.get("flight", ""))
    keyword = clean_ident(request.args.get("keyword", ""))
    # Cap at 20s: SWIM_TIMEOUT is 45s and JVM startup + TLS handshake + JMS
    # teardown eat ~10-15s outside --duration. A user-supplied duration=30
    # used to reliably outrun the subprocess timeout and 504.
    duration = clean_duration(request.args.get("duration", ""), default_duration,
                              hi=20)
    # --limit used to be hardcoded at 50 in swim_consumer.py with no query
    # parameter to raise it, so filtered_results==50 meant "at least 50".
    # Clamp 1–200: high enough for a busy airport window, low enough that
    # a tfdm/stdds firehose can't blow up a response.
    limit = clean_int(request.args.get("limit", ""), 50, lo=1, hi=200)

    if airport_required and not airport:
        return jsonify({
            "error": "Missing or invalid 'airport' parameter",
            "hint": "Expected an ICAO code, e.g. airport=KJFK",
        }), 400

    args = [feed]
    if airport:
        args += ["--airport", airport]
    if allow_flight and flight:
        args += ["--flight", flight]
    if allow_keyword and keyword:
        args += ["--keyword", keyword]
    args += ["--duration", duration, "--limit", limit]

    data, status = run_script("swim_consumer.py", args, timeout=SWIM_TIMEOUT)
    return jsonify(data), status


@bp.route("/api/swim/tbfm")
def swim_tbfm():
    """Get TBFM arrival metering data."""
    return _swim_call("tbfm", 12, allow_flight=True)


@bp.route("/api/swim/sfdps")
def swim_sfdps():
    """Get SFDPS flight positions (FIXM)."""
    return _swim_call("sfdps", 10, allow_flight=True)


@bp.route("/api/swim/itws")
def swim_itws():
    """Get ITWS terminal weather alerts."""
    return _swim_call("itws", 12, airport_required=True)


@bp.route("/api/swim/notams")
def swim_notams():
    """Get NOTAMs from SWIM FNS feed."""
    return _swim_call("notams", 18, airport_required=True)


@bp.route("/api/swim/stdds")
def swim_stdds():
    """Get STDDS surface/TRACON tracks."""
    return _swim_call("stdds", 10, airport_required=True)


@bp.route("/api/swim/tfms-flight")
def swim_tfms_flight():
    """Get TFMS flight positions (NAS-authoritative)."""
    return _swim_call("tfms-flight", 14, allow_flight=True)


@bp.route("/api/swim/tfms-flow")
def swim_tfms_flow():
    """Get TFMS flow info (GDP advisories, TMI assignments, restrictions)."""
    return _swim_call("tfms-flow", 15, allow_keyword=True)


@bp.route("/api/swim/tfdm")
def swim_tfdm():
    """Get TFDM surface management data."""
    return _swim_call("tfdm", 14, allow_flight=True)
