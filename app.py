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

import copy
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, has_request_context
from flask_cors import CORS

import store
import analysis

# The data scripts are importable modules — run them in-process instead of
# paying a ~100-300ms interpreter spawn per call. swim_consumer.py is NOT
# imported: it wraps a JVM and stays a subprocess.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import flight_data as _mod_flight_data          # noqa: E402
import aviation_weather as _mod_aviation_weather  # noqa: E402
import airport_ops as _mod_airport_ops          # noqa: E402

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
# Auth + rate limiting
#
# Every endpoint spends real money (AeroAPI) or real time (SWIM JVM spawns),
# and the Railway URL is guessable. Two layers, both enforced in a single
# before_request hook:
#
#   1. Bearer-token auth, DORMANT BY DEFAULT. Rollout is three steps so the
#      shipped app never breaks:
#        a. Set API_TOKEN on Railway            -> unauthenticated requests
#           are logged but still served (visibility, zero risk)
#        b. Ship the client sending the header  -> logs go quiet
#        c. Set REQUIRE_AUTH=1                  -> missing/wrong token = 401
#      Unsetting REQUIRE_AUTH is the instant rollback at any point.
#
#   2. A per-minute request cap, ALWAYS ON. Fixed 60-second window, counted
#      per bearer token when one is presented, else per client IP (first hop
#      of X-Forwarded-For — Railway terminates TLS in front of us). In-memory
#      by design: with --workers 2 the real cluster-wide cap is up to
#      2x RATE_LIMIT_PER_MIN, which is fine — this is a bulkhead against
#      abuse and runaway client loops, not a billing meter.
#
# /health stays exempt from both so Railway's healthcheck (and a locked-out
# operator) can always see the service. CORS preflights (OPTIONS) are exempt
# from auth because browsers never attach Authorization headers to them.
# ---------------------------------------------------------------------------

RATE_LIMIT_PER_MIN = max(1, int(os.environ.get("RATE_LIMIT_PER_MIN", "60") or 60))

_rate_lock = threading.Lock()
_rate_buckets: dict[str, tuple[float, int]] = {}  # key -> (window_start, count)


def _client_key() -> str:
    """Rate-limit key: the bearer token if presented, else the client IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 7:
        return "tok:" + auth[7:15]  # prefix is plenty for bucketing
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return "ip:" + fwd.split(",")[0].strip()
    return "ip:" + (request.remote_addr or "unknown")


def _rate_limited(key: str) -> int:
    """Count a request against key's window. Returns seconds to wait, 0 = ok."""
    now = time.monotonic()
    with _rate_lock:
        start, count = _rate_buckets.get(key, (now, 0))
        if now - start >= 60.0:
            start, count = now, 0
        count += 1
        _rate_buckets[key] = (start, count)
        # Opportunistic prune so the dict can't grow unbounded under an
        # address-rotating scanner.
        if len(_rate_buckets) > 1000:
            cutoff = now - 120.0
            for k in [k for k, (s, _) in _rate_buckets.items() if s < cutoff]:
                del _rate_buckets[k]
    if count > RATE_LIMIT_PER_MIN:
        return max(1, int(60.0 - (now - start)) + 1)
    return 0


def _token_ok(supplied: str) -> bool:
    expected = os.environ.get("API_TOKEN", "").strip()
    if not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


@app.before_request
def _gate():
    # Always let the healthcheck and CORS preflights through.
    if request.path == "/health" or request.method == "OPTIONS":
        return None

    # --- Rate cap (always on) ---
    retry_in = _rate_limited(_client_key())
    if retry_in:
        resp = jsonify({
            "error": "Rate limit exceeded",
            "limit_per_minute": RATE_LIMIT_PER_MIN,
            "retry_after_seconds": retry_in,
        })
        resp.headers["Retry-After"] = str(retry_in)
        return resp, 429

    # --- Bearer auth ---
    api_token = os.environ.get("API_TOKEN", "").strip()
    require = os.environ.get("REQUIRE_AUTH", "").lower() in ("1", "true", "yes")

    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
    authed = bool(supplied) and _token_ok(supplied)

    if not require:
        # Dormant mode: serve everything, but make unauthenticated traffic
        # visible in the deploy logs so we know when the client has caught up
        # (and whether anyone else is hitting the URL).
        if api_token and not authed:
            print(f"[AUTH] Unauthenticated request (not yet enforced): "
                  f"{request.method} {request.path} from {_client_key()}",
                  file=sys.stderr)
        return None

    if not api_token:
        # REQUIRE_AUTH=1 with no token configured is a misconfiguration.
        # Fail closed — this flag exists to protect the AeroAPI account —
        # with an error that says exactly what to fix.
        return jsonify({
            "error": "Server misconfigured: REQUIRE_AUTH is set but API_TOKEN is not",
            "hint": "Set API_TOKEN on Railway, or unset REQUIRE_AUTH",
        }), 503

    if not authed:
        return jsonify({
            "error": "Unauthorized",
            "hint": "Send 'Authorization: Bearer <token>' matching the server's API_TOKEN",
        }), 401

    return None


# ---------------------------------------------------------------------------
# Script runner
#
# run_script() keeps its original signature — every endpoint, the brief, and
# the tracker call it unchanged — but the inside is now a pipeline:
#
#   run_script -> TTL cache -> circuit breaker -> in-process call (python
#                                                 modules) or subprocess (SWIM)
#
# In-process: flight_data / aviation_weather / airport_ops are imported and
# their dispatch() runs on a worker thread with a hard result timeout —
# same timeout semantics as the old subprocess kill, minus the ~100-300ms
# interpreter spawn per call, plus shared state (cache + breaker) that a
# process-per-request design made impossible. swim_consumer.py still runs
# as a subprocess because it wraps a JVM.
# ---------------------------------------------------------------------------

def _run_subprocess(script: str, args: list, timeout: int = DEFAULT_TIMEOUT,
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
            if isinstance(data, dict):
                # Uniform freshness: every envelope carries a top-level
                # fetched_at = when the SOURCE was pulled (the scripts'
                # pull_time), not when this HTTP response was assembled.
                # The client's "Updated Xs ago" should render this.
                data.setdefault("fetched_at",
                                data.get("pull_time")
                                or datetime.now(timezone.utc).isoformat())
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
                return {"results": results,
                        "fetched_at": datetime.now(timezone.utc).isoformat()}, 200
            return {"raw_output": stdout[:2000], "stderr": result.stderr[:500]}, 200

    except subprocess.TimeoutExpired:
        return {"error": f"Script timed out after {timeout}s"}, 504
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)}"}, 500


# --- In-process execution ---------------------------------------------------

_INPROC_MODULES = {
    "flight_data.py": _mod_flight_data,
    "aviation_weather.py": _mod_aviation_weather,
    "airport_ops.py": _mod_airport_ops,
}

# Generously sized: these are IO-bound HTTP fetches, and run_scripts_parallel
# workers block on this pool — it must always be deeper than the outer
# fan-out (max 10) or a burst could deadlock waiting on itself.
_INPROC_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="inproc")


def _run_inprocess(module, script: str, args: list, timeout: int,
                   env_extras: dict = None) -> tuple[dict, int]:
    """Execute a script's dispatch() on a worker thread with a hard timeout.

    A timeout abandons the worker (threads can't be killed) — the underlying
    HTTP calls carry their own 10-12s socket timeouts, so abandoned work
    self-terminates shortly after. Matches subprocess-timeout semantics from
    the caller's point of view.
    """
    try:
        ns = module.build_parser().parse_args([str(a) for a in args])
    except SystemExit:
        # argparse rejected the args — same class of failure as the old
        # "script exited 2" path.
        return {"error": f"Invalid arguments for {script}: {args}"}, 500

    # The subprocess path passed prefetched status via env var; env vars are
    # process-global and therefore a race between request threads, so the
    # in-process path rides it on the namespace instead.
    if env_extras and env_extras.get("PFT_PREFETCHED_STATUS"):
        ns.prefetched_status = env_extras["PFT_PREFETCHED_STATUS"]

    fut = _INPROC_POOL.submit(module.dispatch, ns)
    try:
        data = fut.result(timeout=timeout)
    except TimeoutError:
        fut.cancel()
        return {"error": f"Script timed out after {timeout}s"}, 504
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)}"}, 500

    if not isinstance(data, dict):
        return {"error": f"{script} returned {type(data).__name__}, expected dict"}, 500
    data.setdefault("fetched_at",
                    data.get("pull_time")
                    or datetime.now(timezone.utc).isoformat())
    return data, 200


def _execute(script: str, args: list, timeout: int,
             env_extras: dict = None) -> tuple[dict, int]:
    module = _INPROC_MODULES.get(script)
    if module is not None:
        return _run_inprocess(module, script, args, timeout, env_extras)
    if script == "swim_consumer.py":
        served = _swim_daemon_serve(args)
        if served is not None:
            return served, 200
    return _run_subprocess(script, args, timeout, env_extras)


# --- SWIM daemon read path --------------------------------------------------
#
# When the long-running consumer (swim_daemon.py, leader process) has a
# recent heartbeat for a queue, SWIM requests are answered from its stored
# events instead of spawning a JVM: same "results" shape the subprocess
# prints, ~0ms instead of 10-45s — and no queue competition, which is what
# used to let two overlapping requests each see half the message stream.
# Falls back to the subprocess automatically whenever the daemon is down
# (local dev, JVM crash-looping, non-daemon queues like tbfm/notams).

# Feeds -> config queue names, mirrored from swim_consumer.FEED_QUEUE_MAP
# (import kept lazy: swim_consumer pulls in nothing heavy, but the daemon
# module owns the parser bindings).
_SWIM_FEED_TO_QUEUE = {
    "tbfm": "tbfm", "itws": "itws", "notams": "notams", "sfdps": "sfdps",
    "stdds": "stdds", "tfms-flight": "tfms", "tfms-flow": "tfms",
    "tfdm": "tfdm",
}

# How far back a daemon-served window reaches. Generous compared with the
# 8-18s subprocess capture windows — the daemon has been listening the
# whole time, which is the point.
_SWIM_SERVE_WINDOW_SECONDS = 900


def _swim_argv_to_query(args: list) -> dict:
    """["tfms-flight","--flight","DAL5187","--duration","8"] -> fields."""
    q = {"feed": str(args[0]) if args else "", "airport": None,
         "flight": None, "keyword": None, "limit": 50}
    i = 1
    while i < len(args):
        a, val = str(args[i]), (str(args[i + 1]) if i + 1 < len(args) else None)
        if a in ("--airport", "-a"):
            q["airport"] = val; i += 2
        elif a in ("--flight", "-f"):
            q["flight"] = val; i += 2
        elif a in ("--keyword", "-k"):
            q["keyword"] = val; i += 2
        elif a in ("--limit", "-n") and val and val.isdigit():
            q["limit"] = int(val); i += 2
        else:
            i += 2 if val and not val.startswith("-") else 1
    return q


def _swim_daemon_serve(args: list):
    """Answer a SWIM request from daemon-collected events, or None."""
    q = _swim_argv_to_query(args)
    queue_name = _SWIM_FEED_TO_QUEUE.get(q["feed"])
    if not queue_name:
        return None
    try:
        health = store.swim_daemon_health(queue_name)
    except Exception:
        return None
    if not health.get("alive"):
        return None

    results = store.swim_recent_events(
        q["feed"], window_seconds=_SWIM_SERVE_WINDOW_SECONDS,
        airport=q["airport"], flight=q["flight"], keyword=q["keyword"],
        limit=q["limit"])
    return {
        "feed": q["feed"],
        "query": {"airport": q["airport"], "flight": q["flight"],
                  "duration_seconds": None},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "served_from": "daemon",
        "window_seconds": _SWIM_SERVE_WINDOW_SECONDS,
        "daemon_last_message_at": health.get("last_message_at"),
        "total_raw_messages": health.get("messages_total"),
        "filtered_results": len(results),
        "results": results,
    }


# --- TTL cache --------------------------------------------------------------
#
# Keyed by (script, args). TTLs follow the codebase's own staleness model
# (SOURCE_HORIZON / refresh_after_seconds): forecasts hold for 30-60 min,
# live-ish feeds for minutes, and AeroAPI status for its phase-derived
# refresh interval — so the server never re-buys data it itself would tell
# the client hasn't changed yet. Cached responses keep their ORIGINAL
# fetched_at (the client's freshness caption must reflect source time) and
# carry a "cache" annotation. Bypass for debugging with ?nocache=1 on any
# endpoint. Per-worker (gunicorn --workers 2 = two caches), which only
# means a worst case of one extra fetch per source.

_TTL_BY_CALL = {
    ("aviation_weather.py", "metar"): 300,
    ("aviation_weather.py", "taf"): 1800,
    ("aviation_weather.py", "sigmet"): 1800,
    ("aviation_weather.py", "isigmet"): 1800,
    ("aviation_weather.py", "pirep"): 600,
    ("aviation_weather.py", "faa-status"): 300,
    ("aviation_weather.py", "brief"): 300,
    ("airport_ops.py", "gairmet"): 1800,
    ("airport_ops.py", "tcf"): 1800,
    ("airport_ops.py", "lightning"): 60,
    ("airport_ops.py", "rvr"): 120,
    ("airport_ops.py", "atfm-infer"): 600,
    ("flight_data.py", "chain"): 600,
    ("flight_data.py", "track"): 30,
    ("swim_consumer.py", "tfms-flow"): 300,
    ("swim_consumer.py", "tfms-flight"): 240,
    ("swim_consumer.py", "tbfm"): 300,
    ("swim_consumer.py", "itws"): 300,
    ("swim_consumer.py", "notams"): 900,
    ("swim_consumer.py", "stdds"): 180,
    ("swim_consumer.py", "sfdps"): 300,
    ("swim_consumer.py", "tfdm"): 240,
}

_CACHE_MAX_ENTRIES = 500
_STALE_MAX_SECONDS = 6 * 3600   # never serve anything older than this

_cache_lock = threading.Lock()
_cache: dict[tuple, dict] = {}  # key -> {data, status, stored_at, ttl}


def _cache_key(script: str, args: list) -> tuple:
    return (script, tuple(str(a) for a in args))


def _ttl_for(script: str, args: list, data: dict) -> int:
    sub = str(args[0]) if args else ""
    if script == "flight_data.py" and sub == "status":
        # Phase-derived: an airborne flight's status holds for its refresh
        # interval; a finished flight never changes again.
        try:
            flights = _extract_flights(data)
            if flights:
                now = datetime.now(timezone.utc)
                phase = analysis.compute_phase(flights[0], now)
                horizon = analysis.compute_horizon(flights[0], now, phase)
                interval = analysis.refresh_interval(phase, horizon)
                if interval is None:
                    return 3600  # terminal — nothing further will change
                return max(60, min(int(interval), 900))
        except Exception:
            pass
        return 120
    return _TTL_BY_CALL.get((script, sub), 0)


def _cache_get(key: tuple):
    with _cache_lock:
        entry = _cache.get(key)
        return copy.deepcopy(entry) if entry else None


def _cache_put(key: tuple, data: dict, status: int, ttl: int) -> None:
    if ttl <= 0 or status != 200 or not isinstance(data, dict):
        return
    with _cache_lock:
        _cache[key] = {"data": copy.deepcopy(data), "status": status,
                       "stored_at": time.monotonic(), "ttl": ttl}
        if len(_cache) > _CACHE_MAX_ENTRIES:
            now = time.monotonic()
            expired = [k for k, e in _cache.items()
                       if now - e["stored_at"] > e["ttl"]]
            for k in expired:
                del _cache[k]
            while len(_cache) > _CACHE_MAX_ENTRIES:
                oldest = min(_cache, key=lambda k: _cache[k]["stored_at"])
                del _cache[oldest]


def _annotated(entry: dict, stale: bool = False, reason: str = None) -> tuple[dict, int]:
    data = entry["data"]
    note = {"hit": True, "age_seconds": int(time.monotonic() - entry["stored_at"])}
    if stale:
        note["stale"] = True
        note["reason"] = reason or "served stale"
    data["cache"] = note
    return data, entry["status"]


def _nocache_requested() -> bool:
    return has_request_context() and request.args.get("nocache") == "1"


# --- Circuit breaker --------------------------------------------------------
#
# Per upstream, not per endpoint: when AeroAPI is down, every flight_data
# call should stop paying the 10s x 2-attempt timeout tax immediately. Trips
# after N consecutive failures, then serves stale cache (flagged) or fails
# fast for the cooldown window; the first call after cooldown is the trial
# that closes it again. Shared across requests — the whole point of moving
# in-process.

_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 120  # seconds

_breaker_lock = threading.Lock()
_breakers: dict[str, dict] = {}  # upstream -> {failures, opened_until}


def _upstream_for(script: str, args: list) -> str:
    sub = str(args[0]) if args else ""
    if script == "flight_data.py":
        return "aeroapi"
    if script == "swim_consumer.py":
        return "swim"
    if script == "aviation_weather.py":
        return "faa_nas" if sub == "faa-status" else "awc"
    if script == "airport_ops.py":
        return {"lightning": "blitzortung", "rvr": "faa_rvr",
                "atfm-infer": "aeroapi"}.get(sub, "awc")
    return script


def _breaker_open(upstream: str) -> int:
    """Seconds until the breaker half-opens, 0 if closed/half-open."""
    with _breaker_lock:
        b = _breakers.get(upstream)
        if not b:
            return 0
        remaining = b.get("opened_until", 0) - time.monotonic()
        return max(0, int(remaining))


def _breaker_record(upstream: str, ok: bool) -> None:
    with _breaker_lock:
        b = _breakers.setdefault(upstream, {"failures": 0, "opened_until": 0})
        if ok:
            b["failures"] = 0
            b["opened_until"] = 0
        else:
            b["failures"] += 1
            if b["failures"] >= _BREAKER_THRESHOLD:
                b["opened_until"] = time.monotonic() + _BREAKER_COOLDOWN


def _breaker_states() -> dict:
    with _breaker_lock:
        now = time.monotonic()
        return {u: {"failures": b["failures"],
                    "open_for_seconds": max(0, int(b["opened_until"] - now))}
                for u, b in _breakers.items() if b["failures"] > 0}


# --- The public runner ------------------------------------------------------

def run_script(script: str, args: list, timeout: int = DEFAULT_TIMEOUT,
               env_extras: dict = None) -> tuple[dict, int]:
    """Fetch through cache and breaker. Signature unchanged from the
    subprocess era — callers don't know any of this exists."""
    key = _cache_key(script, args)
    upstream = _upstream_for(script, args)
    cached = _cache_get(key)
    now = time.monotonic()

    # 1) Fresh cache hit — no upstream contact at all.
    if cached and not _nocache_requested():
        age = now - cached["stored_at"]
        if age <= cached["ttl"]:
            return _annotated(cached)

    # 2) Breaker open — serve stale if we can, fail fast if we can't.
    retry_in = _breaker_open(upstream)
    if retry_in:
        if cached and (now - cached["stored_at"]) <= _STALE_MAX_SECONDS:
            return _annotated(cached, stale=True,
                              reason=f"{upstream} unavailable (circuit open, "
                                     f"retry in {retry_in}s)")
        return {"error": f"Upstream '{upstream}' temporarily unavailable "
                         f"(circuit open after repeated failures)",
                "retry_after_seconds": retry_in}, 503

    # 3) Live fetch.
    data, status = _execute(script, args, timeout, env_extras)
    _breaker_record(upstream, status == 200)

    if status == 200:
        _cache_put(key, data, status, _ttl_for(script, args, data))
        return data, status

    # 4) Fetch failed — a flagged stale answer beats an error for every
    # consumer here (the brief's partial-result envelope, the tracker, the
    # client's cards). The flag keeps it honest.
    if cached and (now - cached["stored_at"]) <= _STALE_MAX_SECONDS:
        return _annotated(cached, stale=True,
                          reason=f"live fetch failed "
                                 f"({(data or {}).get('error', 'unknown')})")
    return data, status


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


def clean_date(value: str) -> str:
    """Validate a YYYY-MM-DD date string. Returns '' for anything else.

    Dates ride into subprocess argv and into AeroAPI result filtering, so
    anything that isn't a real calendar date is dropped rather than passed
    through.
    """
    v = clean_param(value or "", 10)
    if not v:
        return ""
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Airport code normalization
#
# The upstream sources disagree about which code they want:
#   - aviationweather.gov (METAR/TAF/PIREP) needs 4-letter ICAO ("KJFK").
#     Passing "JFK" returns a non-JSON body and the parse fails, surfacing as
#     an empty result rather than a clear error.
#   - FAA NAS status accepts either — it matches on both forms internally.
#   - FAA RVR wants the 3-letter FAA code ("JFK").
#
# Clients shouldn't have to know that. Normalize on the way in, then re-key
# the response back to whatever the caller actually asked for, so a client
# that sends "JFK" finds its data under "JFK".
# ---------------------------------------------------------------------------

# Non-CONUS US airports where ICAO isn't simply "K" + FAA code.
_FAA_TO_ICAO = {
    # Alaska
    "ANC": "PANC", "FAI": "PAFA", "JNU": "PAJN", "KTN": "PAKT", "BET": "PABE",
    "OTZ": "PAOT", "OME": "PAOM", "SIT": "PASI", "ADQ": "PADQ", "BRW": "PABR",
    # Hawaii
    "HNL": "PHNL", "OGG": "PHOG", "KOA": "PHKO", "LIH": "PHLI", "ITO": "PHTO",
    # Territories
    "GUM": "PGUM", "SPN": "PGSN", "SJU": "TJSJ", "STT": "TIST", "STX": "TISX",
    "PPG": "NSTU",
}
_ICAO_TO_FAA = {v: k for k, v in _FAA_TO_ICAO.items()}


def _is_conus(icao: str) -> bool:
    """True for contiguous-US ICAO codes (the domestic /airsigmet coverage
    area). Alaska/Hawaii/territories are 'P'/'T'/'N'-prefixed, same as every
    non-US airport — all of them are blind spots for the domestic SIGMET feed
    and need the international one instead."""
    return bool(icao) and icao.startswith("K")


def to_icao(code: str) -> str:
    """Best-effort 4-letter ICAO. Passes through anything already 4 chars."""
    c = clean_ident(code)
    if not c:
        return ""
    if len(c) == 4:
        return c
    if len(c) == 3:
        return _FAA_TO_ICAO.get(c, "K" + c)
    return c


def to_faa(code: str) -> str:
    """Best-effort 3-letter FAA code (what the RVR feed expects)."""
    c = clean_ident(code)
    if not c:
        return ""
    if len(c) == 3:
        return c
    if c in _ICAO_TO_FAA:
        return _ICAO_TO_FAA[c]
    if len(c) == 4 and c.startswith("K"):
        return c[1:]
    return c


def airport_list(raw: str):
    """Split a comma/space separated airport list into ICAO codes.

    Returns (icao_codes, {icao: as_the_caller_wrote_it}).
    """
    mapping = {}
    codes = []
    for token in (raw or "").replace(",", " ").split():
        original = clean_ident(token)
        if not original:
            continue
        icao = to_icao(original)
        if icao and icao not in mapping:
            mapping[icao] = original
            codes.append(icao)
    return codes, mapping


def rekey_airports(payload, mapping: dict):
    """Rename `data` keys from ICAO back to what the caller requested."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return payload
    renamed = {}
    for key, value in payload["data"].items():
        renamed[mapping.get(key, key)] = value
    payload["data"] = renamed
    # Keep the resolution visible so a client can tell what was actually queried.
    payload["resolved"] = {orig: icao for icao, orig in mapping.items()
                           if orig != icao}
    return payload


def clean_duration(value: str, default: int, lo: int = 1, hi: int = 30) -> str:
    """Coerce a duration to a sane int, falling back to the endpoint default."""
    try:
        n = int(clean_param(value, 8))
    except (TypeError, ValueError):
        n = default
    return str(max(lo, min(hi, n)))


def run_scripts_parallel(tasks: list[dict], max_workers: int = 6,
                         deadline: float = None) -> dict:
    """Run multiple script calls in parallel.

    tasks: [{"key": "metar", "script": "aviation_weather.py", "args": [...], "timeout": 15}, ...]
    deadline: optional time.monotonic() value; results not in by then are
        returned as 504 "budget exceeded" entries instead of blocking the
        request. (The underlying subprocesses still die on their own
        per-script timeouts; we just stop waiting for them.)
    Returns: {"key": {result_json}, ...}
    """
    results = {}
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
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

        remaining = None
        if deadline is not None:
            remaining = max(0.5, deadline - time.monotonic())
        try:
            for fut in as_completed(futures, timeout=remaining):
                key = futures[fut]
                try:
                    data, status = fut.result()
                    results[key] = {"data": data, "status": status}
                except Exception as e:
                    results[key] = {"data": {"error": str(e)}, "status": 500}
        except TimeoutError:
            for fut, key in futures.items():
                if key not in results:
                    fut.cancel()
                    results[key] = {
                        "data": {"error": "Skipped — request time budget exceeded"},
                        "status": 504,
                    }
    finally:
        # Don't block the response on threads still babysitting slow
        # subprocesses; they exit when their own timeouts fire.
        pool.shutdown(wait=deadline is None, cancel_futures=True)

    return results


# ============================================================================
# HEALTH
# ============================================================================

@app.route("/health")
def health():
    store_info = store.health_check()
    with _cache_lock:
        cache_entries = len(_cache)
    return jsonify({
        "status": "ok" if store_info.get("ok") else "degraded",
        "service": "pro-flight-tracker",
        "version": "1.8",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": store_info,
        "tracker_leader": TRACKER_IS_LEADER,
        "cache_entries": cache_entries,
        "breakers": _breaker_states(),
        "swim_daemon": {q: store.swim_daemon_health(q)
                        for q in ("tfms", "itws")},
    })


# ============================================================================
# FLIGHT DATA ENDPOINTS
# ============================================================================

@app.route("/api/flight/status")
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


@app.route("/api/flight/chain")
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


@app.route("/api/flight/track")
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


# ============================================================================
# WEATHER ENDPOINTS
# ============================================================================

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


@app.route("/api/weather/metar")
def weather_metar():
    """Get METAR observations."""
    return _airport_weather("metar")


@app.route("/api/weather/taf")
def weather_taf():
    """Get TAF terminal forecasts."""
    return _airport_weather("taf")


@app.route("/api/weather/sigmet")
def weather_sigmet():
    """Get SIGMETs and Convective SIGMETs."""
    sig_type = request.args.get("type", "")
    args = ["sigmet"]
    if sig_type:
        args += ["--type", sig_type]

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/isigmet")
def weather_isigmet():
    """Get international SIGMETs — Alaska, Hawaii/Pacific, and non-US FIRs,
    which the domestic /api/weather/sigmet endpoint doesn't cover."""
    hazard = request.args.get("hazard", "")
    args = ["isigmet"]
    if hazard:
        args += ["--hazard", hazard]

    data, status = run_script("aviation_weather.py", args)
    return jsonify(data), status


@app.route("/api/weather/pirep")
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


@app.route("/api/weather/faa-status")
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


@app.route("/api/weather/brief")
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
        airports, _ = airport_list(route)
        if airports:
            args += ["--route"] + airports
    if hazard:
        hazards = [s.strip() for s in hazard.split(",")]
        args += ["--hazard"] + hazards

    data, status = run_script("airport_ops.py", args)
    return jsonify(data), status


@app.route("/api/ops/tcf")
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


@app.route("/api/ops/lightning")
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


@app.route("/api/ops/rvr")
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


@app.route("/api/ops/atfm")
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


# ============================================================================
# FAA SWIM ENDPOINTS
# ============================================================================

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
# LIVE — cheap phase + predictions for the client's main refresh
# ============================================================================

@app.route("/api/flight/live")
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
    verdict, refresh_after_seconds), so the client's existing decoders work
    unchanged. The verdict is marked scope="status_only": no weather, FAA
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

    # Verdict-lite: same assess() as the brief, fed only status-derived
    # inputs — no FAA programs, no weather effects, no turn analysis.
    plan = analysis.source_plan(horizon["hours_to_next_event"], phase)
    branch = analysis.classify_branch(horizon, [], {}, plan, [])
    effects = (analysis.build_effects(primary, [], {}, edct, horizon)
               + analysis.taxi_effects(taxi, edct))
    verdict = analysis.assess(horizon, branch, {}, primary, effects, taxi,
                              phase)
    verdict["scope"] = "status_only"

    _sev = {"ACTION": 0, "WATCH": 1, "INFO": 2}
    effects.sort(key=lambda e: _sev.get(e.get("severity"), 3))

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
        "effects": effects,
        "predicted_times": predictions,
        "timezones": {"origin": origin_tz, "destination": dest_tz},
        "edct_cache": edct_cache_info,
        "refresh_after_seconds": analysis.refresh_interval(phase, horizon),
        "aeroapi_queries_used": 1,
    }), 200


# ============================================================================
# ANALYSIS BRIEF — horizon-gated, deterministic, LLM-ready
# ============================================================================

@app.route("/api/brief", methods=["GET", "POST"])
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
    date = clean_param(date or datetime.now(timezone.utc).strftime("%Y-%m-%d"), 12)

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
    if plan["position"]["relevant"]:
        # Answers "where is it and is it actually moving" once the aircraft
        # is out of the gate. ADS-B and OpenSky are free and tried first;
        # the AeroAPI fallback only fires when both miss, and gets the
        # prefetched status so it costs one query rather than two.
        pos_args = ["track", "--flight", flight]
        if primary.get("registration"):
            pos_args += ["--reg", primary["registration"]]
        pos_env = None
        if isinstance(status_data, dict):
            try:
                pos_env = {"PFT_PREFETCHED_STATUS": json.dumps(status_data)}
            except (TypeError, ValueError):
                pos_env = None
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
        chain_env = None
        if isinstance(status_data, dict):
            try:
                chain_env = {"PFT_PREFETCHED_STATUS": json.dumps(status_data)}
            except (TypeError, ValueError):
                chain_env = None
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

    branch = analysis.classify_branch(horizon, programs, turn_analysis, plan,
                                      weather_effects)
    effects = (analysis.build_effects(primary, programs, turn_analysis,
                                      edct, horizon)
               + weather_effects
               + analysis.taxi_effects(taxi, edct)
               + analysis.position_effects(position, phase))
    verdict = analysis.assess(horizon, branch, turn_analysis, primary, effects,
                              taxi, phase)

    # Severity order so the client can render top-down without re-sorting.
    _sev = {"ACTION": 0, "WATCH": 1, "INFO": 2}
    effects.sort(key=lambda e: _sev.get(e.get("severity"), 3))

    excluded = {k: v["reason"] for k, v in plan.items() if not v["relevant"]}

    payload = analysis.build_llm_payload(flight, date, horizon, plan, branch,
                                         verdict, sources)
    # The model gets the computed effects and predictions as facts, plus a
    # rule to report rather than re-derive them.
    payload["facts"]["effects"] = effects
    payload["facts"]["predicted_times"] = predictions
    payload["facts"]["taf_windows"] = taf_windows
    payload["facts"]["phase"] = phase
    payload["facts"]["taxi"] = taxi
    payload["facts"]["position"] = position
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
        "effects": effects,
        "predicted_times": predictions,
        "taf_windows": taf_windows,
        "timezones": {"origin": origin_tz, "destination": dest_tz},
        "branch_classification": branch,
        "sources_consulted": sorted(k for k, v in sources.items()
                                    if v.get("status") == "ok"),
        "sources_excluded": excluded,
        "sources": sources,
        "llm_payload": payload,
        "refresh_after_seconds": analysis.refresh_interval(phase, horizon),
        "aeroapi_queries_used": aeroapi_queries,
    }), 200


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

                # Horizon gate. Without this the tracker escalates on a delay
                # program that is active RIGHT NOW for a flight leaving
                # tomorrow — the program will have expired long before
                # departure, so the alert is a false alarm. Same reasoning as
                # /api/brief; see Example 1 in analytical-framework.md.
                tracked_flights_found = _extract_flights(
                    results.get("flight_status", {}).get("data"))
                primary = tracked_flights_found[0] if tracked_flights_found else {}
                # Phase-aware, same as /api/brief: a flight holding on a
                # taxiway used to fall into the hours<0 branch and have
                # faa_status gated off, so an active ground stop that was
                # genuinely holding it went unalerted.
                tracked_phase = analysis.compute_phase(primary, now)
                horizon = analysis.compute_horizon(primary, now, tracked_phase)
                plan = analysis.source_plan(horizon["hours_to_next_event"],
                                            tracked_phase)

                # Phase 2 — FAA delay programs, but only when they can still
                # matter at this horizon. Free call, but a misleading one
                # when the flight is a day out.
                airports = []
                for f in tracked_flights_found:
                    for key in ("origin_icao", "dest_icao"):
                        code = f.get(key)
                        if code and code not in airports:
                            airports.append(code)

                if airports and plan["faa_status"]["relevant"]:
                    faa = run_scripts_parallel([{
                        "key": "faa_status",
                        "script": "aviation_weather.py",
                        "args": ["faa-status", "--icao"] + airports,
                        "timeout": 15,
                    }], max_workers=1)
                    results.update(faa)

                # Drop anything the horizon says is not decision-relevant so
                # it cannot drive an alert.
                relevance = {"flight_status": True,
                             "faa_status": plan["faa_status"]["relevant"],
                             "tfms_flow_gdp": plan["tfms_flow"]["relevant"]}
                check_data = {"data": {
                    k: v["data"] for k, v in results.items()
                    if relevance.get(k, True)
                }}

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

    # The persistent SWIM consumer rides the same leadership: exactly one
    # process cluster-wide holds the JMS connections, so request-path
    # fallbacks never compete with it for queue messages.
    try:
        import swim_daemon
        swim_daemon.start(airline_map=_AIRLINE_MAP)
    except Exception as exc:
        print(f"[TRACKER] SWIM daemon failed to start: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


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
