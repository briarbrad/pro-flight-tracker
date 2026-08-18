"""_record_and_get_delay_trend: the live/brief response's own delay_trend
must reflect the data THIS request just fetched, not just the background
tracker's separate polling cadence.

Background: delay_trend was read exclusively from a flight_snapshots table
written only by the background tracker loop -- completely decoupled from
whatever live status a given /api/flight/live or /api/brief call had just
fetched for its own hero tile. That let the trend box read "0 -> 0 -> 0 -> 0,
holding steady" on the exact same screen as a tile already showing a live
+30 min delay, whenever the tracker's own next scheduled check hadn't landed
yet. _record_and_get_delay_trend writes a snapshot from data already in hand
(no extra AeroAPI cost) before reading the trend back, so the two can never
disagree.

Run with: pytest tests/test_record_and_get_delay_trend.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store  # noqa: E402
import app as app_module  # noqa: E402

_record_and_get_delay_trend = app_module._record_and_get_delay_trend


def _fresh_ident(prefix: str) -> str:
    return f"{prefix}{id(object()) % 100000}"


def test_writes_a_snapshot_from_the_supplied_primary_and_reads_it_back():
    flight = _fresh_ident("AC")
    date = "2026-08-18"
    track_id = f"{flight}_{date}"
    primary = {
        "estimated_out": "2026-08-18T22:35:00Z",
        "scheduled_out": "2026-08-18T22:05:00Z",
        "estimated_in": "2026-08-19T01:00:00Z",
        "scheduled_in": "2026-08-19T00:38:00Z",
    }
    try:
        trend = _record_and_get_delay_trend(flight, date, primary, "HIGH")
        assert trend is not None
        snaps = store.recent_snapshots(track_id)
        assert len(snaps) == 1
        assert snaps[0]["risk"] == "HIGH"
        # A live +30 min tile must not roundtrip as "holding steady at 0".
        assert snaps[0]["delta_minutes"] and snaps[0]["delta_minutes"] > 0
    finally:
        store._snap_mem.pop(track_id, None)


def test_second_call_appends_rather_than_overwrites():
    """Two calls (e.g. a live fetch followed by a brief a minute later) must
    both land in the series, not silently clobber each other."""
    flight = _fresh_ident("UA")
    date = "2026-08-18"
    track_id = f"{flight}_{date}"
    primary_1 = {"estimated_out": "2026-08-18T22:10:00Z",
                "scheduled_out": "2026-08-18T22:05:00Z"}
    primary_2 = {"estimated_out": "2026-08-18T22:35:00Z",
                "scheduled_out": "2026-08-18T22:05:00Z"}
    try:
        _record_and_get_delay_trend(flight, date, primary_1, "LOW")
        trend = _record_and_get_delay_trend(flight, date, primary_2, "MODERATE")
        snaps = store.recent_snapshots(track_id)
        assert len(snaps) == 2
        assert trend is not None
    finally:
        store._snap_mem.pop(track_id, None)


def test_never_raises_even_if_primary_is_malformed():
    """delay_trend is an enhancement -- a bad primary dict must degrade to
    "no new snapshot", never take the whole live/brief response down with
    it."""
    flight = _fresh_ident("DL")
    date = "2026-08-18"
    track_id = f"{flight}_{date}"
    try:
        # primary=None is malformed input; the helper must swallow it.
        result = _record_and_get_delay_trend(flight, date, None, "LOW")
        assert result is None or isinstance(result, dict)
    finally:
        store._snap_mem.pop(track_id, None)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
