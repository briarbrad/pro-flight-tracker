"""Cache key normalization and the no-deepcopy-on-get contract."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _clear():
    app_module._script_cache.clear()


def test_reordered_flags_share_one_key():
    _clear()
    k1 = app_module._cache_key("flight_data.py",
                               ["status", "--flight", "DL1",
                                "--date", "2026-09-12"])
    k2 = app_module._cache_key("flight_data.py",
                               ["status", "--date", "2026-09-12",
                                "--flight", "DL1"])
    assert k1 == k2


def test_different_flag_values_are_different_keys():
    _clear()
    k1 = app_module._cache_key("flight_data.py",
                               ["status", "--flight", "DL1"])
    k2 = app_module._cache_key("flight_data.py",
                               ["status", "--flight", "DL2"])
    assert k1 != k2


def test_positional_order_still_matters():
    _clear()
    k1 = app_module._cache_key("aviation_weather.py", ["metar", "KJFK", "KLGA"])
    k2 = app_module._cache_key("aviation_weather.py", ["metar", "KLGA", "KJFK"])
    assert k1 != k2


def test_annotated_stamp_does_not_leak_into_cached_object():
    """_annotated copies before stamping, so the cache's own object is never
    mutated — this is what makes deepcopy-on-get unnecessary."""
    _clear()
    key = app_module._cache_key("flight_data.py", ["status", "--flight", "DL1"])
    app_module._cache_put(key, {"flights": [{"id": "DL1"}]}, 200, ttl=300)

    first = app_module._cache_get(key)
    data1, _ = app_module._annotated(first)
    assert data1["cache"]["hit"] is True

    second = app_module._cache_get(key)
    assert "cache" not in second["data"]


def test_cache_hit_returns_equal_data_without_copy_overhead():
    _clear()
    key = app_module._cache_key("flight_data.py", ["status", "--flight", "DL1"])
    payload = {"flights": [{"id": "DL1"}]}
    app_module._cache_put(key, payload, 200, ttl=300)
    # Mutating the caller's original after put must not affect the cache.
    payload["flights"].append({"id": "MUTATED"})
    got = app_module._cache_get(key)
    assert got["data"] == {"flights": [{"id": "DL1"}]}
