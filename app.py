#!/usr/bin/env python3
"""
Pro Flight Tracker — API Server

Flask REST API that wraps the CLI scripts as HTTP endpoints.
Designed for deployment on Railway, called by the Rork iOS app.

Architecture:
  - Each script (flight_data.py, aviation_weather.py, airport_ops.py,
    swim_consumer.py) runs as a subprocess, outputs JSON to stdout
  - This server captures that JSON and returns it as HTTP responses
  - A background scheduler tracks flights and sends push notifications
    when risk levels change

All API keys are read from environment variables — never hardcoded.
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

import store

app = Flask(__name__)
CORS(app)  # Allow Rork app to call from any origin

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
DEFAULT_TIMEOUT = 45  # seconds per script call
SWIM_TIMEOUT = 45     # SWIM feeds need time for JMS connection + JVM startup
                      # (must exceed max --duration by ~15s: JVM start, TLS
                      #  handshake, and JMS teardown all happen outside it)
CHECK_TIMEOUT = 120   # full flight check runs many sources

# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------

def run_script(script: str, args: list, timeout: int = DEFAULT_TIMEOUT,
               env_extras: dict = None) -> tuple[dict, int]:
    """Run a Python script as subprocess, return (parsed_json, http_status)."""
    env = os.environ.copy()
    # Pass through all API keys from Railway env vars
    if env_extras:
        env.update(env_extras)

    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=str(SCRIPTS_DIR)
        )

        if result.returncode != 0:
            # Scripts report real failures as JSON on stdout and exit nonzero.
            # stderr is progress chatter ("Connecting to SWIM ..."), so prefer
            # stdout — otherwise the useful diagnostic gets thrown away.
            stdout = result.stdout.strip()
            if stdout:
                try:
                    payload = json.loads(stdout)
                    if isinstance(payload, dict):
                        payload.setdefault("returncode", result.returncode)
                        return payload, 500
                except json.JSONDecodeError:
                    pass
            return {
                "error": result.stderr.strip() or "Script failed",
                "stdout_tail": stdout[-1000:],
                "returncode": result.returncode
            }, 500

        # Try to parse JSON from stdout
        stdout = result.stdout.strip()
        if not stdout:
            return {"error": "Empty output", "stderr": result.stderr.strip()}, 500

        try:
            data = json.loads(stdout)
            return data, 200
        except json.JSONDecodeError:
            # Some scripts output multiple JSON objects (one per line)
            lines = stdout.split("\n")
            results = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if results:
                return {"results": results}, 200
            return {"raw_output": stdout[:2000], "stderr": result.stderr[:500]}, 200

    except subprocess.TimeoutExpired:
        return {"error": f"Script timed out after {timeout}s"}, 504
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)}"}, 500


# ---------------------------------------------------------------------------
# Query-parameter sanitizing
#
# Values from request.args go straight into a subprocess argv list. They are
# not shell-interpreted, but they ARE parsed by argparse in the child script,
# so junk in a URL (a stray quote, a value that starts with "-") crashes the
# script with an exit code 2 instead of returning a useful error. Clean them
# here so a malformed URL degrades gracefully instead of 500-ing.
# ---------------------------------------------------------------------------

# Quote characters that commonly ride along from a mis-quoted curl/shell call,
# including the smart quotes that appear when a command is pasted from chat.
_STRAY = '\'"`‘’“” \t\r\n'


def clean_param(value: str, maxlen: int = 32) -> str:
    """Strip stray quotes/whitespace. Returns '' for anything unusable."""
    if not value:
        return ""
    v = value.strip(_STRAY)[:maxlen]
    # A value starting with '-' would be read as a flag by argparse.
    return "" if v.startswith("-") else v


def clean_ident(value: str, maxlen: int = 16) -> str:
    """Clean an airport/flight/keyword identifier: alphanumerics only."""
    v = clean_param(value, maxlen).upper()
    return v if v.replace("-", "").replace("_", "").isalnum() else ""


def clean_duration(value: str, default: int, lo: int = 1, hi: int = 30) -> str:
    """Coerce a duration to a sane int, falling back to the endpoint default."""
    try:
        n = int(clean_param(value, 8))
    except (TypeError, ValueError):
        n = default
    return str(max(lo, min(hi, n)))


def run_scripts_parallel(tasks: list[dict], max_workers: int = 6) -> dict:
    """Run multiple script calls in parallel.

    tasks: [{"key": "metar", "script": "aviation_weather.py", "args": [...], "timeout": 15}, ...]
    Returns: {"key": {result_json}, ...}
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for task in tasks:
            fut = pool.submit(
                run_script,
                task["script"],
                task["args"],
                task.get("timeout", DEFAULT_TIMEOUT),
                task.get("env_extras")
            )
            futures[fut] = task["key"]

        for fut in as_completed(futures):
            key = futures[fut]
            try:
                data, status = fut.result()
                results[key] = {"data": data, "status": status}
            except Exception as e:
                results[key] = {"data": {"error": str(e)}, "status": 500}

    return results


# ============================================================================
# HEALTH
# ============================================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "pro-flight-tracker",
"version": "1.3",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


# ============================================================================
# FLIGHT DATA ENDPOINTS
# ============================================================================

@app.route("/api/flight/status")
def flight_status():
    """Get flight status from AeroAPI."""
    flight = request.args.get("flight")
    date = request.args.get("date")
    if not flight:
        return jsonify({"error": "Missing 'flight' parameter"}), 400

    args = ["status", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("flight_data.py", args)
    return jsonify(data), status


@app.route("/api/flight/chain")
def flight_chain():
    """Get equipment chain (inbound flight, tail, turn time)."""
    flight = request.args.get("flight")
    date = request.args.get("date")
    if not flight:
        return jsonify({"error": "Missing 'flight' parameter"}), 400

    args = ["chain", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("flight_data.py", args, timeout=30)
    return jsonify(data), status


@app.route("/api/flight/track")
def flight_track():
    """Get real-time aircraft position."""
    reg = request.args.get("reg")
    flight = request.args.get("flight")
    if not reg and not flight:
        return jsonify({"error": "Missing 'reg' or 'flight' parameter"}), 400

    args = ["track"]
    if reg:
        args += ["--reg", reg]
    elif flight:
        args += ["--flight", flight]

    data, status = run_script("flight_data.py", args)
    return jsonify(data), status


# ============================================================================
# WEATHER ENDPOINTS
# ============================================================================

@app.route("/api/weather/metar")
def weather_metar():
    """Get METAR observations."""
    icao = request.args.get("icao", "")
    if not icao:
        return jsonify({"error": "Missing 'icao' parameter"}), 400

    # Support comma-separated list: KJFK,KLGA
    icao_list = [s.strip() for s in icao.split(",")]
    args = ["metar", "--icao"] + icao_list

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/taf")
def weather_taf():
    """Get TAF terminal forecasts."""
    icao = request.args.get("icao", "")
    if not icao:
        return jsonify({"error": "Missing 'icao' parameter"}), 400

    icao_list = [s.strip() for s in icao.split(",")]
    args = ["taf", "--icao"] + icao_list

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/sigmet")
def weather_sigmet():
    """Get SIGMETs and Convective SIGMETs."""
    sig_type = request.args.get("type", "")
    args = ["sigmet"]
    if sig_type:
        args += ["--type", sig_type]

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/pirep")
def weather_pirep():
    """Get PIREPs near an airport."""
    icao = request.args.get("icao")
    distance = request.args.get("distance", "200")
    if not icao:
        return jsonify({"error": "Missing 'icao' parameter"}), 400

    args = ["pirep", "--icao", icao, "--distance", distance]
    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/faa-status")
def weather_faa_status():
    """Get FAA delay programs (GDP, ground stops, etc.)."""
    icao = request.args.get("icao", "")
    if not icao:
        return jsonify({"error": "Missing 'icao' parameter"}), 400

    icao_list = [s.strip() for s in icao.split(",")]
    args = ["faa-status", "--icao"] + icao_list

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/brief")
def weather_brief():
    """Get a full weather briefing for a route."""
    origin = request.args.get("origin")
    dest = request.args.get("dest")
    if not origin or not dest:
        return jsonify({"error": "Missing 'origin' or 'dest' parameter"}), 400

    args = ["brief", "--origin", origin, "--dest", dest]
    data, status = run_script("aviation_weather.py", args, timeout=30)
    return jsonify(data), status


# ============================================================================
# AIRPORT OPERATIONS ENDPOINTS
# ============================================================================

@app.route("/api/ops/gairmet")
def ops_gairmet():
    """Get G-AIRMET turbulence forecasts along a route."""
    route = request.args.get("route", "")
    hazard = request.args.get("hazard", "")

    args = ["gairmet"]
    if route:
        airports = [s.strip() for s in route.split(",")]
        args += ["--route"] + airports
    if hazard:
        hazards = [s.strip() for s in hazard.split(",")]
        args += ["--hazard"] + hazards

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@app.route("/api/ops/lightning")
def ops_lightning():
    """Get real-time lightning strikes near an airport."""
    icao = clean_ident(request.args.get("icao", ""))
    radius = clean_duration(request.args.get("radius", ""), 20, lo=1, hi=250)
    duration = clean_duration(request.args.get("duration", ""), 10)
    if not icao:
        return jsonify({
            "error": "Missing or invalid 'icao' parameter",
            "hint": "Expected an ICAO code, e.g. icao=KJFK",
        }), 400

    args = ["lightning", "--icao", icao, "--radius", radius, "--duration", duration]
    data, status = run_script("airport_ops.py", args, timeout=int(duration) + 15)
    return jsonify(data), status


@app.route("/api/ops/rvr")
def ops_rvr():
    """Get per-runway visual range from FAA sensors."""
    airport = request.args.get("airport")
    if not airport:
        return jsonify({"error": "Missing 'airport' parameter"}), 400

    args = ["rvr", "--airport", airport]
    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@app.route("/api/ops/atfm")
def ops_atfm():
    """Infer Eurocontrol ATFM regulation from delay patterns."""
    flight = request.args.get("flight")
    date = request.args.get("date")
    if not flight:
        return jsonify({"error": "Missing 'flight' parameter"}), 400

    args = ["atfm-infer", "--flight", flight]
    if date:
        args += ["--date", date]

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


# ============================================================================
# FAA SWIM ENDPOINTS
# ============================================================================

def _swim_call(feed: str, default_duration: int, airport_required: bool = False,
               allow_flight: bool = False, allow_keyword: bool = False):
    """Shared handler for the SWIM endpoints. Sanitizes every query param."""
    airport = clean_ident(request.args.get("airport", ""))
    flight = clean_ident(request.args.get("flight", ""))
    keyword = clean_ident(request.args.get("keyword", ""))
    duration = clean_duration(request.args.get("duration", ""), default_duration)

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
    args += ["--duration", duration]

    data, status = run_script("swim_consumer.py", args, timeout=SWIM_TIMEOUT)
    return jsonify(data), status


@app.route("/api/swim/tbfm")
def swim_tbfm():
    """Get TBFM arrival metering data."""
    return _swim_call("tbfm", 12, allow_flight=True)


@app.route("/api/swim/sfdps")
def swim_sfdps():
    """Get SFDPS flight positions (FIXM)."""
    return _swim_call("sfdps", 10, allow_flight=True)


@app.route("/api/swim/itws")
def swim_itws():
    """Get ITWS terminal weather alerts."""
    return _swim_call("itws", 12, airport_required=True)


@app.route("/api/swim/notams")
def swim_notams():
    """Get NOTAMs from SWIM FNS feed."""
    return _swim_call("notams", 18, airport_required=True)


@app.route("/api/swim/stdds")
def swim_stdds():
    """Get STDDS surface/TRACON tracks."""
    return _swim_call("stdds", 10, airport_required=True)


@app.route("/api/swim/tfms-flight")
def swim_tfms_flight():
    """Get TFMS flight positions (NAS-authoritative)."""
    return _swim_call("tfms-flight", 14, allow_flight=True)


@app.route("/api/swim/tfms-flow")
def swim_tfms_flow():
    """Get TFMS flow info (GDP advisories, TMI assignments, restrictions)."""
    return _swim_call("tfms-flow", 15, allow_keyword=True)


@app.route("/api/swim/tfdm")
def swim_tfdm():
    """Get TFDM surface management data."""
    return _swim_call("tfdm", 14, allow_flight=True)


# ============================================================================
# UNIFIED FLIGHT CHECK — The main endpoint for the Rork app
# ============================================================================

@app.route("/api/check", methods=["GET", "POST"])
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

    if not flight:
        return jsonify({"error": "Missing 'flight' parameter"}), 400

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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

    # Extract origin/dest airports from flight status
    origin_icao = None
    dest_icao = None
    if status_code == 200 and isinstance(status_data, dict):
        origin_icao = status_data.get("origin", {}).get("code_icao") or \
                       status_data.get("origin_icao")
        dest_icao = status_data.get("destination", {}).get("code_icao") or \
                     status_data.get("destination_icao")

        # Try alternate field names
        if not origin_icao:
            for f in status_data.get("flights", []):
                origin_icao = origin_icao or f.get("origin", {}).get("code_icao")
                dest_icao = dest_icao or f.get("destination", {}).get("code_icao")

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
    chain_env = None
    if status_code == 200 and isinstance(status_data, dict):
        if (status_data.get("data") or {}).get("flights"):
            try:
                chain_env = {"PFT_PREFETCHED_STATUS": json.dumps(status_data)}
            except (TypeError, ValueError):
                chain_env = None

    phase2_tasks.append({
        "key": "equipment_chain",
        "script": "flight_data.py",
        "args": ["chain", "--flight", flight, "--date", date],
        "timeout": 30,
        "env_extras": chain_env,
    })

    # Run phase 2 in parallel
    phase2_results = run_scripts_parallel(phase2_tasks, max_workers=8)

    # Also run the airport-independent tasks in parallel
    swim_results = run_scripts_parallel(tasks, max_workers=4)

    # Merge everything into a unified response
    response = {
        "flight": flight,
        "date": date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "origin_icao": origin_icao,
        "destination_icao": dest_icao,
        "data": {
            "flight_status": status_data if status_code == 200 else {"error": status_data},
        }
    }

    # Merge phase 2 results
    for key, result in phase2_results.items():
        response["data"][key] = result["data"] if result["status"] == 200 else {"error": result["data"]}

    # Merge SWIM results
    for key, result in swim_results.items():
        if key not in response["data"]:
            response["data"][key] = result["data"] if result["status"] == 200 else {"error": result["data"]}

    return jsonify(response), 200


# ============================================================================
# FLIGHT TRACKING — Background monitoring with push notifications
# ============================================================================

# Tracked flights live in `store` — Postgres when DATABASE_URL is set,
# in-memory otherwise. See store.py. The old module-level dict was per-worker,
# so with gunicorn --workers 2 a POST landed in one worker and the other never
# saw it.


@app.route("/api/track", methods=["POST"])
def start_tracking():
    """Start tracking a flight for push notifications.

    JSON body:
      flight (required): e.g. "DL244"
      date (optional): defaults to today
      push_token (required): Expo push token from the app
      interval_minutes (optional): check interval, default 15
    """
    body = request.get_json(silent=True) or {}
    flight = body.get("flight")
    push_token = body.get("push_token")

    if not flight or not push_token:
        return jsonify({"error": "Missing 'flight' and/or 'push_token'"}), 400

    date = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

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


@app.route("/api/track", methods=["DELETE"])
def stop_tracking():
    """Stop tracking a flight."""
    flight = request.args.get("flight")
    date = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    track_id = f"{flight}_{date}"

    if store.remove(track_id):
        return jsonify({"status": "stopped", "track_id": track_id})
    return jsonify({"error": "Not tracking this flight"}), 404


@app.route("/api/tracked")
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
        "tracker_on_this_worker": TRACKER_IS_LEADER,
    })


# ============================================================================
# BACKGROUND TRACKER THREAD
# ============================================================================

def send_push_notification(push_token: str, title: str, body: str, data: dict = None):
    """Send a push notification via Expo Push API."""
    import urllib.request
    message = {
        "to": push_token,
        "sound": "default",
        "title": title,
        "body": body,
    }
    if data:
        message["data"] = data

    req_data = json.dumps([message]).encode("utf-8")
    req = urllib.request.Request(
        "https://exp.host/--/api/v2/push/send",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[PUSH ERROR] {e}", file=sys.stderr)
        return None


def _escalate(current: str, candidate: str) -> str:
    """Return whichever risk level is worse."""
    return candidate if _risk_rank(candidate) > _risk_rank(current) else current


def _extract_flights(flight_status: dict) -> list:
    """Pull the flight list out of a flight_data.py `status` payload.

    The real shape nests it two deep: {"command":"status","data":{"flights":[...]}}.
    The bare "flights" fallback is for a payload that's already been unwrapped.
    """
    if not isinstance(flight_status, dict):
        return []
    inner = flight_status.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("flights"), list):
        return inner["flights"]
    if isinstance(flight_status.get("flights"), list):
        return flight_status["flights"]
    return []


def _parse_iso(value: str):
    """Parse an ISO 8601 timestamp, tolerating a trailing Z. None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _departure_delay_minutes(flight: dict):
    """Minutes the departure has slipped vs schedule, or None if unknowable.

    AeroAPI status strings are things like "Scheduled" / "En Route" / "Arrived"
    and often never literally say "Delayed", so comparing the estimated and
    scheduled times is the reliable signal.
    """
    scheduled = _parse_iso(flight.get("scheduled_out"))
    if not scheduled:
        return None
    actual = _parse_iso(flight.get("actual_out"))
    estimated = _parse_iso(flight.get("estimated_out"))
    effective = actual or estimated
    if not effective:
        return None
    return (effective - scheduled).total_seconds() / 60.0


def _faa_airport_records(faa_status: dict) -> list:
    """Yield the per-airport records from an aviation_weather.py faa-status payload.

    Real shape: {"command":"faa-status","data":{"KJFK":{...},"KEWR":{...}}}
    — keyed by ICAO. The old code looked for a top-level "programs" list, which
    has never existed, so ground stops and GDPs were silently never detected.
    """
    if not isinstance(faa_status, dict):
        return []
    data = faa_status.get("data")
    if not isinstance(data, dict):
        return []
    return [rec for rec in data.values() if isinstance(rec, dict)]


def extract_risk_level(check_data: dict) -> str:
    """Overall risk level for push notifications: 'LOW', 'MODERATE', or 'HIGH'.

    Deliberately conservative and self-contained — this drives whether the user
    gets woken up, not what the client displays. /api/check still returns raw
    data and does no interpretation.
    """
    data = check_data.get("data", {})
    risk = "LOW"

    # ---- FAA delay programs at the airports involved -------------------
    for record in _faa_airport_records(data.get("faa_status")):
        if record.get("ground_stops"):
            risk = _escalate(risk, "HIGH")
        if record.get("ground_delay_programs"):
            risk = _escalate(risk, "MODERATE")
        if record.get("arrival_departure_delays"):
            risk = _escalate(risk, "MODERATE")
        if record.get("closures"):
            risk = _escalate(risk, "MODERATE")

    # ---- The flight itself ---------------------------------------------
    for flight in _extract_flights(data.get("flight_status")):
        if not isinstance(flight, dict):
            continue

        if flight.get("cancelled") or flight.get("diverted"):
            risk = _escalate(risk, "HIGH")
            continue

        status = (flight.get("status") or "").lower()
        if "cancel" in status or "divert" in status:
            risk = _escalate(risk, "HIGH")
            continue
        if "delay" in status:
            risk = _escalate(risk, "MODERATE")

        delay = _departure_delay_minutes(flight)
        if delay is not None:
            if delay >= 45:
                risk = _escalate(risk, "HIGH")
            elif delay >= 15:
                risk = _escalate(risk, "MODERATE")

    # ---- TFMS flow advisories (ground stops / GDP issuances) -----------
    flow = data.get("tfms_flow_gdp")
    if isinstance(flow, dict):
        for result in flow.get("results", []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('title', '')} {result.get('text', '')}".upper()
            if not text.strip():
                text = str(result).upper()
            # A cancellation advisory is the program ENDING — not a new risk.
            if "CANCEL" in text or "PURGE" in text:
                continue
            if "GROUND STOP" in text or result.get("msg_type") == "RSTR":
                risk = _escalate(risk, "HIGH")
            elif "GDP" in text or "GROUND DELAY" in text:
                risk = _escalate(risk, "MODERATE")

    return risk


def _flight_is_finished(status_data: dict) -> str | None:
    """Return a reason string if the flight is over, else None.

    Once a flight has arrived, been cancelled, or diverted, there is nothing
    left to warn about — continuing to poll just spends AeroAPI credit.
    """
    if not isinstance(status_data, dict):
        return None

    flights = status_data.get("data", {}).get("flights") or status_data.get("flights")
    if not flights:
        return None

    f = flights[0]
    if not isinstance(f, dict):
        return None

    if f.get("cancelled"):
        return "cancelled"
    if f.get("diverted"):
        return "diverted"
    if f.get("actual_in"):
        return "arrived at gate"
    status = (f.get("status") or "").lower()
    if "arrived" in status or "landed" in status:
        return "arrived"
    return None


def background_tracker():
    """Background thread that periodically checks tracked flights.

    Only ever runs in the single process that won leader election — see
    store.acquire_leadership(). Two copies of this loop would double AeroAPI
    spend for identical data.

    Runs a check immediately on entry, then every 60s after. Running
    immediately matters most for the retry path in _watch_for_leadership():
    when a worker finally wins the lock minutes after boot, anything that
    was already due (e.g. last_check was never set) shouldn't have to wait a
    further 60s on top of however long the retry took.
    """
    first_pass = True
    while True:
        if not first_pass:
            time.sleep(60)  # Check every minute if any flights are due
        first_pass = False

        now = datetime.now(timezone.utc)

        try:
            dropped = store.purge_expired(now)
            if dropped:
                print(f"[TRACKER] Purged {dropped} expired flight(s)", file=sys.stderr)
            flights_to_check = store.due_for_check(now)
        except Exception as exc:
            print(f"[TRACKER] Store error: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue  # next iteration still sleeps first — no tight-looping

        for info in flights_to_check:
            track_id = info.get("track_id", "?")
            try:
                flight = info["flight"]
                date = info["date"]

                # Phase 1 — flight status + flow advisories.
                # flight_data.py status = 2 AeroAPI queries; SWIM is free.
                quick_tasks = [
                    {
                        "key": "flight_status",
                        "script": "flight_data.py",
                        "args": ["status", "--flight", flight, "--date", date],
                        "timeout": 20
                    },
                    {
                        "key": "tfms_flow_gdp",
                        "script": "swim_consumer.py",
                        "args": ["tfms-flow", "--keyword", "GDP", "--duration", "8"],
                        "timeout": 25
                    },
                ]

                results = run_scripts_parallel(quick_tasks, max_workers=2)

                # Phase 2 — FAA delay programs for this flight's airports.
                # The tracker never used to fetch this at all, so ground stops
                # and GDPs could not possibly be detected no matter how the
                # risk logic read them. It's a free (non-AeroAPI) call, but it
                # needs the airports, which only phase 1 can tell us.
                airports = []
                for f in _extract_flights(results.get("flight_status", {}).get("data")):
                    for key in ("origin_icao", "dest_icao"):
                        code = f.get(key)
                        if code and code not in airports:
                            airports.append(code)

                if airports:
                    faa = run_scripts_parallel([{
                        "key": "faa_status",
                        "script": "aviation_weather.py",
                        "args": ["faa-status", "--icao"] + airports,
                        "timeout": 15,
                    }], max_workers=1)
                    results.update(faa)

                check_data = {"data": {k: v["data"] for k, v in results.items()}}

                new_risk = extract_risk_level(check_data)
                old_risk = info.get("last_risk")

                # Update tracking state
                store.mark_checked(track_id, now, new_risk)

                # Stop tracking once the flight is over.
                finished = _flight_is_finished(
                    results.get("flight_status", {}).get("data")
                )
                if finished:
                    store.remove(track_id)
                    print(f"[TRACKER] {flight} {finished} — untracked",
                          file=sys.stderr)

                # Notify on a risk transition — and also on the very first
                # check if the flight is already at risk. Previously the
                # `old_risk and ...` guard meant a flight that was ALREADY
                # HIGH when you started tracking it never notified at all,
                # because there was no prior value to differ from.
                emoji = {"LOW": "🟢", "MODERATE": "🟡", "HIGH": "🔴"}.get(new_risk, "⚪")
                title = body_text = None

                if old_risk is None:
                    if new_risk != "LOW":
                        title = f"{emoji} {flight} Risk: {new_risk}"
                        body_text = "Already elevated when tracking started. Tap to see details."
                elif new_risk != old_risk:
                    direction = ("↑ Elevated" if _risk_rank(new_risk) > _risk_rank(old_risk)
                                 else "↓ Improved")
                    title = f"{emoji} {flight} Risk: {new_risk}"
                    body_text = f"{direction} from {old_risk}. Tap to see details."

                if title:
                    send_push_notification(
                        info["push_token"],
                        title,
                        body_text,
                        {"flight": flight, "date": date, "risk": new_risk}
                    )

            except Exception as e:
                print(f"[TRACKER ERROR] {track_id}: {e}", file=sys.stderr)


def _risk_rank(level: str) -> int:
    return {"LOW": 0, "MODERATE": 1, "HIGH": 2}.get(level, -1)


# ============================================================================
# HELPERS
# ============================================================================

# IATA → ICAO airline code mapping (for SWIM callsigns)
_AIRLINE_MAP = {
    "DL": "DAL", "AA": "AAL", "UA": "UAL", "WN": "SWA", "B6": "JBU",
    "AS": "ASA", "NK": "NKS", "F9": "FFT", "HA": "HAL", "SY": "SCX",
    "G4": "AAY", "BA": "BAW", "AF": "AFR", "LH": "DLH", "KL": "KLM",
    "AZ": "ITY", "IB": "IBE", "EI": "EIN", "AY": "FIN", "SK": "SAS",
    "TP": "TAP", "TK": "THY", "EK": "UAE", "QR": "QTR", "CX": "CPA",
    "SQ": "SIA", "NH": "ANA", "JL": "JAL", "QF": "QFA", "AC": "ACA",
    "AM": "AMX", "VS": "VIR", "LX": "SWR", "OS": "AUA", "SN": "BEL",
    "AT": "RAM",
}


def _iata_to_icao_airline(iata: str) -> str:
    return _AIRLINE_MAP.get(iata.upper(), iata.upper())


def _icao_to_faa(icao: str) -> str:
    """Convert ICAO code to FAA/IATA (strip K prefix for US airports)."""
    if icao and len(icao) == 4 and icao.startswith("K"):
        return icao[1:]
    return icao


# ============================================================================
# STARTUP
# ============================================================================

TRACKER_IS_LEADER = False  # flipped True by _become_leader(), possibly long
                           # after import if leadership is won on a retry


def _become_leader() -> None:
    """Take over as tracker leader: init schema, start the polling thread."""
    global TRACKER_IS_LEADER

    # Leadership FIRST, schema init SECOND. store.init() runs
    # CREATE TABLE IF NOT EXISTS, which is not actually safe against two
    # sessions doing it at once on a table that's never existed — both see
    # "doesn't exist" and race, and the loser can hit a UniqueViolation on
    # Postgres's internal pg_type bookkeeping. Only ever calling this from
    # the confirmed leader means exactly one process, cluster-wide, ever
    # runs it — the race can't happen at all.
    try:
        store.init()
    except Exception as exc:
        print(f"[TRACKER] Store init failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)

    thread = threading.Thread(target=background_tracker, daemon=True)
    thread.start()
    TRACKER_IS_LEADER = True
    print(f"[TRACKER] Background flight tracker started "
          f"(pid={os.getpid()}, store={store.backend_name()})", file=sys.stderr)


def _watch_for_leadership(poll_seconds: int = 15) -> None:
    """Keep retrying acquire_leadership() until it succeeds.

    Rolling deploys run the old and new deployment side by side for a short
    overlap so traffic never drops. Every worker in the new deployment can
    lose its very first leadership attempt simply because the OLD
    deployment's leader hasn't been killed yet — that's the overlap working
    as intended, not a failure. But a one-shot "try once at boot, give up
    forever" check has no way to notice once the old leader's container is
    stopped and the lock frees up moments later. This loop is what actually
    claims it once that happens.
    """
    while True:
        time.sleep(poll_seconds)
        if store.acquire_leadership():
            print(f"[TRACKER] Acquired leadership on retry, pid={os.getpid()}",
                  file=sys.stderr)
            _become_leader()
            return


def start_background_tracker() -> bool:
    """Try to become tracker leader now; if that fails, keep retrying.

    Runs at IMPORT time, not under `if __name__ == "__main__"`. Gunicorn
    imports this module as `app`, so anything gated on __main__ never executes
    in production — which is why the tracker used to silently never run.

    Leader election keeps that fix from creating a worse problem: with
    --workers 2, every worker would otherwise start its own tracker and bill
    AeroAPI twice for identical data.

    Returns whether THIS call won leadership immediately. A False return does
    not mean this process gave up — see _watch_for_leadership.
    """
    if os.environ.get("DISABLE_TRACKER", "").lower() in ("1", "true", "yes"):
        print("[TRACKER] Disabled via DISABLE_TRACKER", file=sys.stderr)
        return False

    if store.acquire_leadership():
        _become_leader()
        return True

    print(f"[TRACKER] Standby (another worker holds the lease), "
          f"pid={os.getpid()}", file=sys.stderr)
    watcher = threading.Thread(target=_watch_for_leadership, daemon=True)
    watcher.start()
    return False


TRACKER_IS_LEADER = start_background_tracker()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
