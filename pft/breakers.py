"""Per-upstream circuit breakers."""

import threading
import time

# Content keys that mean "this call returned something usable," scanned
# across flight_data.py / aviation_weather.py / airport_ops.py response
# shapes. A dispatch() call can embed an "error" key on a normal HTTP 200
# (see e.g. scripts/airport_ops.py's lightning capture, which reports a
# WebSocket error but still returns any strikes/ramp_alerts collected before
# it dropped) — that is a degraded-but-useful response, not an outage, and
# must not trip the breaker. Only a response that is error-and-nothing-else
# should count as an upstream failure.
_RESULT_CONTENT_KEYS = (
    "data", "flights", "results", "route", "strikes", "ramp_alerts",
    "conditions", "forecast", "advisories", "records", "position",
    "current", "hourly", "near_term", "extended_weather", "metering",
)


def _is_upstream_failure(data) -> bool:
    """True when a 200 response is an error report with no usable payload."""
    if not isinstance(data, dict) or "error" not in data:
        return False
    return not any(data.get(k) for k in _RESULT_CONTENT_KEYS)


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
        if sub == "faa-status":
            return "faa_nas"
        if sub == "open-meteo":
            return "open_meteo"
        return "awc"
    if script == "airport_ops.py":
        # atfm-infer gets its own breaker: it makes its own AeroAPI calls,
        # and lumping it under "aeroapi" would misattribute its failures to
        # (and trip) the shared AeroAPI breaker, or vice versa.
        return {"lightning": "blitzortung", "rvr": "faa_rvr",
                "atfm-infer": "atfm_infer"}.get(sub, "awc")
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
