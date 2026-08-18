"""Delay-trend tracking: snapshots across scheduled checks -> visible trend.

Background: tracked_flights only ever persisted the LAST risk tier between
polls, so whether a delay was growing, shrinking, or holding steady across
the day was silently discarded. store.record_snapshot() now writes one row
per scheduled check (on the tracker's existing cadence -- zero extra AeroAPI
queries) and analysis.classify_delay_trend() names the direction that the
series already implies.

Covers the two cases the feature spec calls out explicitly:
  - a growing delay across three consecutive checks must read "widening"
  - a single check so far must yield NO trend (None), never a false
    "holding steady"

Runs against the in-memory backend (no DATABASE_URL in this environment);
the Postgres branch is a straight parallel INSERT/SELECT with the same
oldest-first ordering and per-track pruning.

Run with: pytest tests/test_delay_trend.py -v
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis  # noqa: E402
import store  # noqa: E402


def _fresh_track_id(prefix: str) -> str:
    return f"{prefix}-{id(object())}"


def _write_series(track_id: str, deltas: list, start: datetime = None,
                  step_minutes: int = 15) -> None:
    start = start or (datetime.now(timezone.utc)
                      - timedelta(minutes=step_minutes * len(deltas)))
    for i, delta in enumerate(deltas):
        store.record_snapshot(
            track_id, start + timedelta(minutes=step_minutes * i),
            predicted_out="2026-08-18T22:00:00+00:00",
            predicted_in="2026-08-19T01:00:00+00:00",
            delta_minutes=delta, risk="LOW")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_growing_delay_across_three_checks_reads_widening():
    """The core fixture: +2 -> +8 -> +15 across three consecutive checks."""
    snapshots = [{"delta_minutes": d} for d in (2.0, 8.0, 15.0)]
    assert analysis.classify_delay_trend(snapshots) == "widening"


def test_single_check_yields_no_trend_not_false_steady():
    """One data point is not a trend -- None, never 'steady'."""
    assert analysis.classify_delay_trend([{"delta_minutes": 4.0}]) is None
    assert analysis.classify_delay_trend([]) is None
    assert analysis.classify_delay_trend(None) is None


def test_small_wobble_reads_holding_steady():
    """+2 -> +4 -> +4: inside the noise band, so 'steady', not 'widening'."""
    snapshots = [{"delta_minutes": d} for d in (2.0, 4.0, 4.0)]
    assert analysis.classify_delay_trend(snapshots) == "steady"


def test_shrinking_delay_reads_narrowing():
    snapshots = [{"delta_minutes": d} for d in (12.0, 6.0, 1.0)]
    assert analysis.classify_delay_trend(snapshots) == "narrowing"


def test_null_deltas_are_ignored_not_counted_as_points():
    """Rows where the delta was unknowable must not fabricate a trend."""
    snapshots = [{"delta_minutes": None}, {"delta_minutes": 5.0},
                 {"delta_minutes": True}]  # bool guards against True==1
    assert analysis.classify_delay_trend(snapshots) is None


# ---------------------------------------------------------------------------
# Store roundtrip
# ---------------------------------------------------------------------------

def test_snapshots_roundtrip_oldest_first():
    track_id = _fresh_track_id("trend-rt")
    try:
        _write_series(track_id, [2.0, 8.0, 15.0])
        snaps = store.recent_snapshots(track_id)
        assert [s["delta_minutes"] for s in snaps] == [2.0, 8.0, 15.0]
        assert analysis.classify_delay_trend(snaps) == "widening"
        # Every row keeps its provenance fields.
        assert all(s["risk"] == "LOW" for s in snaps)
        assert all(s["predicted_out"] for s in snaps)
    finally:
        store._snap_mem.pop(track_id, None)


def test_recent_snapshots_respects_limit_keeping_newest():
    track_id = _fresh_track_id("trend-limit")
    try:
        _write_series(track_id, [float(d) for d in range(1, 21)])
        snaps = store.recent_snapshots(track_id, limit=5)
        assert [s["delta_minutes"] for s in snaps] == [16.0, 17.0, 18.0, 19.0, 20.0]
    finally:
        store._snap_mem.pop(track_id, None)


def test_snapshots_prune_past_the_tracking_window():
    """Rows older than the flight's own tracking window are dropped on write."""
    track_id = _fresh_track_id("trend-prune")
    now = datetime.now(timezone.utc)
    ancient = now - timedelta(hours=store.SNAPSHOT_RETENTION_HOURS + 2)
    try:
        store.record_snapshot(track_id, ancient, delta_minutes=99.0, risk="LOW")
        store.record_snapshot(track_id, now, delta_minutes=3.0, risk="LOW")
        snaps = store.recent_snapshots(track_id)
        assert [s["delta_minutes"] for s in snaps] == [3.0]
    finally:
        store._snap_mem.pop(track_id, None)


def test_purge_expired_also_prunes_stale_snapshots():
    track_id = _fresh_track_id("trend-purge")
    ancient = (datetime.now(timezone.utc)
               - timedelta(hours=store.SNAPSHOT_RETENTION_HOURS + 2))
    try:
        # Write directly into memory to bypass record_snapshot's own pruning.
        with store._mem_lock:
            store._snap_mem[track_id] = [{
                "track_id": track_id, "checked_at": store._iso(ancient),
                "predicted_out": None, "predicted_in": None,
                "delta_minutes": 7.0, "risk": "LOW"}]
        store.purge_expired()
        assert store.recent_snapshots(track_id) == []
    finally:
        store._snap_mem.pop(track_id, None)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
