#!/usr/bin/env python3
"""
Pro Flight Tracker — API Server (thin Flask shell).

All domain logic lives in the pft package:
  pft/runner.py      script execution pipeline (cache -> breaker -> run)
  pft/cache.py       bounded TTL cache
  pft/breakers.py    per-upstream circuit breakers
  pft/ratelimit.py   per-minute rate cap (shared Postgres counter)
  pft/swim_serve.py  SWIM daemon read path
  pft/validation.py  query sanitizing + airport-code helpers
  pft/llm.py         OpenRouter narrative/chat
  pft/tracker.py     background flight tracker
  pft/routes/        one blueprint per route domain

This module keeps: app setup, auth gate, request IDs, /health, blueprint
registration, and the tracker kickoff. Names imported below from pft.*
are re-exported for backwards compatibility (tests and external tooling).
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests  # noqa: F401  (re-exported; tests patch app.requests)
from flask import Flask, g, has_request_context, jsonify, request
from flask_cors import CORS

import store
import analysis  # noqa: F401
import flow_brief  # noqa: F401

from pft.script_modules import (
    flight_data as _mod_flight_data,
    aviation_weather as _mod_aviation_weather,
    airport_ops as _mod_airport_ops,
    swim_consumer as _mod_swim_consumer,
)
from pft.logging import _req_id, log
from pft.ratelimit import (
    RATE_LIMIT_PER_MIN, _client_key, _rate_limit_scope, _rate_limited,
    _rate_limited_mem, _rate_buckets, _rate_lock,
)
from pft.cache import (
    _TTLCache, _annotated, _cache_get, _cache_key, _cache_put,
    _nocache_requested, _script_cache,
)
from pft.breakers import (
    _breaker_lock, _breaker_open, _breaker_record, _breaker_states,
    _breakers, _BREAKER_THRESHOLD, _is_upstream_failure,
)
from pft.runner import (
    _is_lightning_call, _lightning_inflight, _lightning_singleflight,
    _prefetch_env_for_subprocess, _status_prefetch_env,
    _PREFETCH_ENV_MAX_BYTES, run_script, run_scripts_parallel,
)
from pft.validation import (
    _extract_flights, clean_ident, clean_int, to_icao,
)
from pft.llm import (
    CHAT_MAX_MESSAGES, CHAT_REASONING_MAX_TOKENS, NARRATIVE_FALLBACK_MODEL,
    NARRATIVE_MODEL, OPENROUTER_API_URL, _llm_usage, _llm_usage_lock,
    _llm_usage_summary, _narrative_cache,
)
from pft.swim_serve import _SWIM_FEED_TO_QUEUE, _swim_argv_to_query
from pft.routes import brief as _r_brief, flight as _r_flight
from pft.routes import narrative as _r_narrative, ops as _r_ops
from pft.routes import swim as _r_swim, track as _r_track, weather as _r_weather
from pft.routes.brief import _extract_origin_dest_icao  # noqa: F401 (re-export)
import pft.tracker as _tracker
from pft.tracker import (
    _parse_iso, _record_and_get_delay_trend, extract_risk_level,
    start_background_tracker,
)

app = Flask(__name__)

# CORS is a browser-only mechanism — it has no effect on the native iOS
# client (URLSession doesn't send an Origin header, so nothing here can ever
# block or affect the app's own requests). It only matters for whether an
# arbitrary website's JavaScript can call this API from a user's browser.
# Default to allowing none; set ALLOWED_ORIGINS to a comma-separated list
# (e.g. "https://example.com,https://admin.example.com") to permit specific
# origins for a future web dashboard or admin tool.
_allowed_origins = [o.strip() for o in
                    os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    CORS(app, origins=_allowed_origins)
else:
    CORS(app, origins=[])  # no browser origin permitted by default

for _bp in (_r_flight.bp, _r_weather.bp, _r_ops.bp, _r_swim.bp,
            _r_brief.bp, _r_track.bp, _r_narrative.bp):
    app.register_blueprint(_bp)

def _token_ok(supplied: str) -> bool:
    expected = os.environ.get("API_TOKEN", "").strip()
    if not expected:
        return False
    # hmac.compare_digest raises ValueError when the inputs differ in
    # length. During the documented dormant-auth rollout the iOS app may
    # already be sending a bearer token of a different size than Railway's
    # API_TOKEN — that used to 500 every request (including in log-only
    # mode) instead of treating it as "not authed". Hash both sides to a
    # fixed length first so a mismatch is just False.
    supplied_digest = hashlib.sha256((supplied or "").encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(supplied_digest, expected_digest)

@app.before_request
def _gate():
    g.request_id = uuid.uuid4().hex[:8]
    # Always let the healthcheck and CORS preflights through.
    if request.path == "/health" or request.method == "OPTIONS":
        return None
    # --- Rate cap (always on) ---
    retry_in = _rate_limited(_client_key())
    if retry_in:
        resp = jsonify({
            "error": "Rate limit exceeded",
            "limit_per_minute": RATE_LIMIT_PER_MIN,
            "scope": _rate_limit_scope(),
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

@app.after_request
def _request_id_header(resp):
    # Echo the request ID so a client can quote it when reporting an error;
    # it matches the [req:...] prefix on the server's stderr lines.
    try:
        rid = g.get("request_id")
    except Exception:
        rid = None
    if rid:
        resp.headers["X-Request-ID"] = rid
    return resp

@app.route("/health")
def health():
    store_info = store.health_check()
    # Upstream *configuration* only — booleans and model names, never
    # secrets, and no live probes (probing AeroAPI/SWIM here would burn
    # paid quota on every healthcheck).
    upstreams = {
        "aeroapi": {
            "configured": bool(os.environ.get("AEROAPI_KEY", "").strip()),
            "keyless": False,
        },
        "openrouter": {
            "configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
            "keyless": False,
            "model": NARRATIVE_MODEL,
            "fallback_model": NARRATIVE_FALLBACK_MODEL,
        },
        "swim": {
            "username_configured": bool(os.environ.get("SWIM_USERNAME", "").strip()),
            "password_configured": bool(os.environ.get("SWIM_PASSWORD", "").strip()),
            "keyless": False,
        },
        "aviationweather_gov": {"configured": True, "keyless": True},
        "blitzortung": {"configured": True, "keyless": True},
        "open_meteo": {"configured": True, "keyless": True},
        "faa": {"configured": True, "keyless": True},
    }
    return jsonify({
        "status": "ok" if store_info.get("ok") else "degraded",
        "service": "pro-flight-tracker",
        "version": "1.13",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": store_info,
        "tracker_leader": _tracker.TRACKER_IS_LEADER,
        "cache_entries": len(_script_cache),
        "breakers": _breaker_states(),
        "upstreams": upstreams,
        "rate_limit": {
            "per_minute": RATE_LIMIT_PER_MIN,
            "scope": _rate_limit_scope(),
        },
        "lightning_inflight": len(_lightning_inflight),
        "llm_usage": _llm_usage_summary(),
        "swim_daemon": {q: store.swim_daemon_health(q)
                        for q in ("tfms", "itws")},
    })

TRACKER_IS_LEADER = start_background_tracker()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
