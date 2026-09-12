"""Regression tests for PFT_PREFETCHED_STATUS reuse.

cmd_chain used to re-fetch /flights/{ident} after /api/check (and the
background tracker) had already paid for the same call. _prefetched_flights
skips that query when the payload describes the same flight — and must NOT
reuse a payload for a different ident or a different date.

Run with: pytest tests/test_prefetch_status.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _mod_flight_data as flight_data, _status_prefetch_env  # noqa: E402


def _status_payload(flight="DL244", date="2026-09-12", flights=None):
    if flights is None:
        flights = [{"ident": flight, "origin_icao": "KJFK", "dest_icao": "KLAX"}]
    return {
        "pull_time": "2026-09-12T12:00:00Z",
        "source": "aeroapi",
        "command": "status",
        "flight": flight,
        "date": date,
        "data": {"flights": flights, "route": None},
        "errors": [],
    }


def test_prefetched_flights_reuses_matching_ident_and_date():
    raw = json.dumps(_status_payload())
    flights = flight_data._prefetched_flights("DL244", raw=raw, date="2026-09-12")
    assert flights is not None
    assert flights[0]["ident"] == "DL244"


def test_prefetched_flights_rejects_different_ident():
    raw = json.dumps(_status_payload(flight="DL244"))
    assert flight_data._prefetched_flights("UA100", raw=raw, date="2026-09-12") is None


def test_prefetched_flights_rejects_different_date():
    """Same ident, different date must fall through to a live AeroAPI call."""
    raw = json.dumps(_status_payload(flight="DL244", date="2026-09-12"))
    assert flight_data._prefetched_flights("DL244", raw=raw, date="2026-09-13") is None


def test_prefetched_flights_allows_missing_date_on_either_side():
    """Don't refuse a usable payload just because one side omitted a date."""
    payload = _status_payload()
    payload.pop("date")
    raw = json.dumps(payload)
    assert flight_data._prefetched_flights("DL244", raw=raw, date="2026-09-12") is not None
    raw = json.dumps(_status_payload())
    assert flight_data._prefetched_flights("DL244", raw=raw, date=None) is not None


def test_prefetched_flights_junk_raw_returns_none():
    assert flight_data._prefetched_flights("DL244", raw="not-json") is None
    assert flight_data._prefetched_flights("DL244", raw="") is None
    assert flight_data._prefetched_flights("DL244", raw="[]") is None


def test_status_prefetch_env_builds_json_for_usable_envelope():
    env = _status_prefetch_env(_status_payload())
    assert env is not None
    parsed = json.loads(env["PFT_PREFETCHED_STATUS"])
    assert parsed["flight"] == "DL244"
    assert parsed["data"]["flights"]


def test_status_prefetch_env_rejects_errors_and_empty():
    assert _status_prefetch_env(None) is None
    assert _status_prefetch_env({"error": "nope"}) is None
    assert _status_prefetch_env({"data": {"flights": []}}) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
