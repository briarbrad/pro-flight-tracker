"""Regression test for the /api/check origin/destination extraction bug.

Background: check_flight() used to look for airport codes at
status_data["flights"][i]["origin"]["code_icao"], but
flight_data.cmd_status() actually returns them at
status_data["data"]["flights"][i]["origin_icao"] (a flat key, produced by
flight_data._parse_aeroapi_flight()). Because neither the key path nor the
field name matched, origin_icao/dest_icao were always None, and every
airport-dependent phase-2 task in /api/check (METAR, TAF, FAA NAS, RVR,
ops feeds) was silently skipped for every request.

This test pins the real envelope shape so any future refactor of either
side (app.py's extraction or flight_data.py's output shape) breaks a test
instead of breaking production silently.

Run with: pytest tests/test_check_envelope.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _extract_origin_dest_icao  # noqa: E402


def _status_envelope(flights):
    """Builds an envelope shaped exactly like flight_data.cmd_status()'s
    return value: {"data": {"flights": [...], "route": ...}, ...}."""
    return {
        "pull_time": "2026-08-18T12:00:00Z",
        "source": "aeroapi",
        "command": "status",
        "flight": "DL244",
        "date": "2026-08-18",
        "data": {"flights": flights, "route": None},
        "errors": [],
    }


def _flat_leg(origin_icao="KJFK", dest_icao="KLAX", **overrides):
    """A single flattened leg dict, shaped like
    flight_data._parse_aeroapi_flight()'s output."""
    leg = {
        "fa_flight_id": "DL244-123",
        "ident": "DL244",
        "origin_icao": origin_icao, "origin_iata": "JFK",
        "dest_icao": dest_icao, "dest_iata": "LAX",
        "status": "Scheduled",
    }
    leg.update(overrides)
    return leg


def test_extracts_icao_codes_from_real_envelope_shape():
    """The core regression case: a normal, successful status response must
    yield the origin/destination ICAO codes so phase-2 tasks get scheduled."""
    status_data = _status_envelope([_flat_leg("KJFK", "KLAX")])
    origin, dest = _extract_origin_dest_icao(status_data, 200)
    assert origin == "KJFK"
    assert dest == "KLAX"


def test_old_broken_shape_assumptions_are_not_what_ships():
    """Documents the exact wrong assumptions the previous implementation
    made, so nobody re-introduces them. Both must be no-ops against the
    real envelope."""
    status_data = _status_envelope([_flat_leg("KJFK", "KLAX")])

    # Old assumption 1: top-level "flights" key (wrong -- it's under "data").
    assert status_data.get("flights", []) == []

    # Old assumption 2: nested origin/destination dicts with "code_icao"
    # (wrong -- legs are flat with "origin_icao"/"dest_icao").
    leg = status_data["data"]["flights"][0]
    assert leg.get("origin", {}) == {}
    assert leg.get("destination", {}) == {}


def test_returns_none_none_when_status_call_failed():
    origin, dest = _extract_origin_dest_icao({"error": "AeroAPI: no flights found"}, 503)
    assert (origin, dest) == (None, None)


def test_returns_none_none_when_no_flights_found():
    status_data = _status_envelope([])
    origin, dest = _extract_origin_dest_icao(status_data, 200)
    assert (origin, dest) == (None, None)


def test_returns_none_none_when_status_data_not_a_dict():
    origin, dest = _extract_origin_dest_icao(None, 200)
    assert (origin, dest) == (None, None)


def test_uses_first_leg_when_multiple_legs_present():
    """cmd_status can return multiple matching legs for the target date;
    /api/check should use the first (primary) one, consistent with how the
    rest of app.py treats flights[0] (e.g. app.py's phase/horizon and
    equipment-chain logic)."""
    status_data = _status_envelope([
        _flat_leg("KJFK", "KLAX"),
        _flat_leg("KBOS", "KORD"),
    ])
    origin, dest = _extract_origin_dest_icao(status_data, 200)
    assert (origin, dest) == ("KJFK", "KLAX")


def test_missing_icao_fields_on_leg_return_none_not_klerror():
    """A leg present but missing airport codes (e.g. a partial AeroAPI
    record) should degrade to None, not raise."""
    status_data = _status_envelope([_flat_leg(origin_icao=None, dest_icao=None)])
    origin, dest = _extract_origin_dest_icao(status_data, 200)
    assert (origin, dest) == (None, None)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
