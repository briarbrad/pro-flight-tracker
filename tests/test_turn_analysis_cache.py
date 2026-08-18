"""Turn-analysis caching: a manual brief's equipment finding must be reusable
elsewhere for free, and must be able to drive risk on its own.

Background: /api/flight/live used to always pass an empty turn_analysis to
classify_branch/build_effects/assess "to save AeroAPI cost" -- so the live
tile's cause line could contradict a HIGH-risk equipment finding the user had
just seen on the brief screen seconds earlier. Separately, extract_risk_level
(the only function driving tracker alerts) never looked at turn_analysis at
all, so a binding equipment constraint could sit there for hours without
ever raising risk or firing an alert -- only the airline's own estimate,
once it finally moved, could trigger anything.

This covers:
  - store.cache_turn_analysis / get_cached_turn_analysis round-trip and TTL
  - a fresh cache entry is exactly what a HIGH-risk /api/flight/live call
    would need to reproduce the same verdict a prior /api/brief produced
  - extract_risk_level escalates HIGH on a turn-time deficit, MODERATE on a
    below-standard-but-workable turn, and leaves risk alone when adequate
  - a turn_analysis with missing fields (no minimum, or no data at all)
    never raises risk -- absence must never look like danger

Run with: pytest tests/test_turn_analysis_cache.py -v
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store  # noqa: E402
import app as flask_app  # noqa: E402

extract_risk_level = flask_app.extract_risk_level


def _fresh_ident(prefix: str) -> str:
    return f"{prefix}{id(object()) % 100000}"


# ---------------------------------------------------------------------------
# store.cache_turn_analysis / get_cached_turn_analysis
# ---------------------------------------------------------------------------

def test_cache_roundtrip_returns_fresh_payload():
    flight = _fresh_ident("AC")
    date = "2026-08-18"
    payload = {"turn_time_available_min": 40.0,
              "turn_time_required_min_minimum": 45,
              "turn_time_required_min_standard": 60,
              "aircraft_category": "regional", "sufficient": False,
              "inbound_ident": "JZA8550", "inbound_registration": "C-GGOF"}
    try:
        store.cache_turn_analysis(flight, date, payload)
        cached = store.get_cached_turn_analysis(flight, date)
        assert cached is not None
        assert cached["payload"]["turn_time_available_min"] == 40.0
        assert cached["payload"]["inbound_ident"] == "JZA8550"
        assert cached["cached_at"]
    finally:
        store._turn_mem.pop(store._turn_key(flight, date), None)


def test_cache_ignores_empty_or_dataless_payload():
    """An empty or "no data" finding must not overwrite/create a cache
    entry -- caching a null result would make get_cached_turn_analysis
    return a false "nothing to worry about" for the whole TTL window."""
    flight = _fresh_ident("UA")
    date = "2026-08-18"
    try:
        store.cache_turn_analysis(flight, date, {})
        assert store.get_cached_turn_analysis(flight, date) is None
        store.cache_turn_analysis(flight, date,
                                  {"turn_time_available_min": None,
                                   "note": "No inbound flight data"})
        assert store.get_cached_turn_analysis(flight, date) is None
    finally:
        store._turn_mem.pop(store._turn_key(flight, date), None)


def test_stale_cache_entry_is_treated_as_absent():
    """Past TURN_ANALYSIS_TTL_MINUTES the entry must read back as None --
    the inbound aircraft's ETA moves continuously, so a stale verdict is
    worse than none at all."""
    flight = _fresh_ident("DL")
    date = "2026-08-18"
    stale_at = datetime.now(timezone.utc) - timedelta(
        minutes=store.TURN_ANALYSIS_TTL_MINUTES + 5)
    try:
        with store._mem_lock:
            store._turn_mem[store._turn_key(flight, date)] = {
                "payload": {"turn_time_available_min": 10.0,
                           "turn_time_required_min_minimum": 45},
                "updated_at": stale_at}
        assert store.get_cached_turn_analysis(flight, date) is None
    finally:
        store._turn_mem.pop(store._turn_key(flight, date), None)


# ---------------------------------------------------------------------------
# extract_risk_level now checking turn_analysis
# ---------------------------------------------------------------------------

def test_extract_risk_level_escalates_high_on_turn_time_deficit():
    """A binding equipment constraint must reach HIGH on its own, with no
    flight-status delay text and no FAA program present -- this is exactly
    the "airline hasn't updated their estimate yet" case."""
    data = {"data": {
        "turn_analysis": {"turn_time_available_min": 40.0,
                          "turn_time_required_min_minimum": 45,
                          "turn_time_required_min_standard": 60},
    }}
    assert extract_risk_level(data) == "HIGH"


def test_extract_risk_level_escalates_moderate_when_below_standard_only():
    data = {"data": {
        "turn_analysis": {"turn_time_available_min": 50.0,
                          "turn_time_required_min_minimum": 45,
                          "turn_time_required_min_standard": 60},
    }}
    assert extract_risk_level(data) == "MODERATE"


def test_extract_risk_level_leaves_low_when_turn_time_adequate():
    data = {"data": {
        "turn_analysis": {"turn_time_available_min": 90.0,
                          "turn_time_required_min_minimum": 45,
                          "turn_time_required_min_standard": 60},
    }}
    assert extract_risk_level(data) == "LOW"


def test_extract_risk_level_ignores_incomplete_turn_analysis():
    """Missing minimum/available must never be treated as a deficit --
    absence of a finding must never read as danger."""
    assert extract_risk_level({"data": {"turn_analysis": {}}}) == "LOW"
    assert extract_risk_level(
        {"data": {"turn_analysis": {"note": "No inbound flight data"}}}
    ) == "LOW"
    assert extract_risk_level({"data": {}}) == "LOW"


def test_extract_risk_level_turn_time_combines_with_other_signals():
    """A turn-time HIGH must not be downgraded by an unrelated LOW
    signal evaluated elsewhere -- escalation is monotonic."""
    data = {"data": {
        "flight_status": {"flights": [{"status": "On Time"}]},
        "turn_analysis": {"turn_time_available_min": 20.0,
                          "turn_time_required_min_minimum": 45},
    }}
    assert extract_risk_level(data) == "HIGH"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
