"""?nocache=1 fails fast on an open breaker instead of serving stale data."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _trip_breaker(upstream):
    for _ in range(app_module._BREAKER_THRESHOLD):
        app_module._breaker_record(upstream, ok=False)
    assert app_module._breaker_open(upstream) > 0


def _seed_cache(script, args, data, age_seconds=10, ttl=1800):
    key = app_module._cache_key(script, args)
    app_module._cache_put(key, data, 200, ttl)
    # Backdate the entry to control freshness.
    with app_module._script_cache._lock:
        app_module._script_cache._data[key][1] -= age_seconds
    return key


def test_nocache_fails_fast_on_open_breaker():
    upstream = "awc"
    _trip_breaker(upstream)
    key = _seed_cache("aviation_weather.py", ["metar", "--icao", "KJFK"],
                      {"conditions": "cached"})
    try:
        with app_module.app.test_request_context("/?nocache=1"):
            data, status = app_module.run_script(
                "aviation_weather.py", ["metar", "--icao", "KJFK"], timeout=5)
        assert status == 503
        assert "temporarily unavailable" in data["error"]
        assert "retry_after_seconds" in data
    finally:
        app_module._breaker_record(upstream, ok=True)
        app_module._script_cache.delete(key)


def test_stale_still_served_without_nocache():
    upstream = "awc"
    _trip_breaker(upstream)
    # Entry older than the taf TTL (1800s) but within the stale window,
    # so the breaker-open path serves it as stale rather than a fresh hit.
    key = _seed_cache("aviation_weather.py", ["taf", "--icao", "KJFK"],
                      {"forecast": "cached"}, age_seconds=1900)
    try:
        with app_module.app.test_request_context("/"):
            data, status = app_module.run_script(
                "aviation_weather.py", ["taf", "--icao", "KJFK"], timeout=5)
        assert status == 200
        assert data["forecast"] == "cached"
        assert data["cache"]["stale"] is True
    finally:
        app_module._breaker_record(upstream, ok=True)
        app_module._script_cache.delete(key)


def test_valueless_nocache_also_bypasses_cache():
    """?nocache (no =1) is the same flag — presence is what counts."""
    key = _seed_cache("aviation_weather.py", ["metar", "--icao", "KJFK"],
                      {"conditions": "cached"})
    try:
        with app_module.app.test_request_context("/?nocache"):
            assert app_module._nocache_requested() is True
        with app_module.app.test_request_context("/?nocache=1"):
            assert app_module._nocache_requested() is True
        with app_module.app.test_request_context("/"):
            assert app_module._nocache_requested() is False
    finally:
        app_module._script_cache.delete(key)
