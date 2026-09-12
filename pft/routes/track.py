"""Background flight tracking."""

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

import store
import analysis
import flow_brief
import pft.tracker

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

bp = Blueprint("track", __name__)


@bp.route("/api/track", methods=["POST"])
def start_tracking():
    """Start tracking a flight for push notifications.

    JSON body:
      flight (required): e.g. "DL244"
      date (optional): defaults to today
      push_token (required): Expo push token from the app
      interval_minutes (optional): check interval, default 15
    """
    body = request.get_json(silent=True) or {}
    flight = clean_ident(body.get("flight") or "")
    push_token = (body.get("push_token") or "").strip()

    if not flight or not push_token:
        return jsonify({"error": "Missing or invalid 'flight' and/or 'push_token'"}), 400

    # body.get("date", today) treated date="" / date=null as a real value and
    # stored track_id "DL244_" / "DL244_None", then the tracker spent AeroAPI
    # credit polling a date that can never match. Invalid dates fall back to
    # UTC today, same as /api/check and /api/flight/live.
    date = clean_date(body.get("date") or "")
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Guard the interval: below ~5 minutes the AeroAPI spend climbs fast and
    # you risk the Personal tier's 10 result-sets/minute limit.
    try:
        interval = int(body.get("interval_minutes", 15))
    except (TypeError, ValueError):
        interval = 15
    interval = max(5, min(240, interval))

    track_id = f"{flight}_{date}"
    record = store.add(track_id, flight, date, push_token, interval)

    return jsonify({
        "status": "tracking",
        "track_id": track_id,
        "interval_minutes": interval,
        "expires_at": record.get("expires_at"),
        "message": f"Now tracking {flight} on {date}. "
                   f"You'll get a push notification if the risk level changes."
    })


@bp.route("/api/track", methods=["DELETE"])
def stop_tracking():
    """Stop tracking a flight."""
    flight = clean_ident(request.args.get("flight", ""))
    date = clean_date(request.args.get("date", ""))
    if not flight:
        return jsonify({"error": "Missing or invalid 'flight' parameter"}), 400
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    track_id = f"{flight}_{date}"

    if store.remove(track_id):
        return jsonify({"status": "stopped", "track_id": track_id})
    return jsonify({"error": "Not tracking this flight"}), 404


@bp.route("/api/tracked")
def list_tracked():
    """List all currently tracked flights."""
    tracked = store.list_all()
    return jsonify({
        "tracked": tracked,
        "count": len(tracked),
        "store_backend": store.backend_name(),
        # True only if THIS worker holds the tracker lease. With >1 worker,
        # most requests land on a standby and will report False even though
        # the tracker is running fine elsewhere. Check the logs for
        # "[TRACKER] Background flight tracker started" to confirm.
        "tracker_on_this_worker": pft.tracker.TRACKER_IS_LEADER,
    })
