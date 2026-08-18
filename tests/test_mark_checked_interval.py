"""Regression test for mark_checked() persisting a self-computed interval.

Background: mark_checked() used to only ever update last_check/last_risk.
The tracker's due_for_check() cadence was therefore permanently pinned to
whatever interval_minutes was set once, at add()-time -- there was no way
for a later check to tighten or loosen its own next cadence based on the
flight's current phase/horizon. mark_checked() now accepts an optional
interval_minutes and, when given, persists it so due_for_check() picks up
the new cadence on its very next read.

This test runs against the in-memory backend (no DATABASE_URL set in this
environment), which is the code path shared by local dev and by any request
that reaches store.py before Postgres is configured -- the Postgres branch
is a straight parallel SQL UPDATE with the same optional-column behavior.

Run with: pytest tests/test_mark_checked_interval.py -v
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store  # noqa: E402


def _fresh_track_id(prefix: str) -> str:
    return f"{prefix}-{id(object())}"


def test_mark_checked_without_interval_leaves_interval_unchanged():
    track_id = _fresh_track_id("t1")
    now = datetime.now(timezone.utc)
    store.add(track_id, "DL244", "2026-08-18", None, interval_minutes=15)
    try:
        store.mark_checked(track_id, now, "LOW")
        record = next(r for r in store.list_all() if r["track_id"] == track_id)
        assert record["interval_minutes"] == 15
        assert record["last_risk"] == "LOW"
    finally:
        store.remove(track_id)


def test_mark_checked_with_interval_updates_the_stored_cadence():
    """The core regression case: passing interval_minutes must actually
    change what due_for_check() uses next time around."""
    track_id = _fresh_track_id("t2")
    now = datetime.now(timezone.utc)
    store.add(track_id, "DL244", "2026-08-18", None, interval_minutes=15)
    try:
        store.mark_checked(track_id, now, "WATCH", interval_minutes=5)
        record = next(r for r in store.list_all() if r["track_id"] == track_id)
        assert record["interval_minutes"] == 5
    finally:
        store.remove(track_id)


def test_tightened_interval_makes_flight_due_sooner():
    """Concrete behavioral proof: after tightening to 5 minutes, a flight
    checked 6 minutes ago is due; at the original 15-minute interval it
    would not have been."""
    track_id = _fresh_track_id("t3")
    checked_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    now = datetime.now(timezone.utc)
    store.add(track_id, "DL244", "2026-08-18", None, interval_minutes=15)
    try:
        store.mark_checked(track_id, checked_at, "LOW", interval_minutes=5)
        due_ids = {r["track_id"] for r in store.due_for_check(now)}
        assert track_id in due_ids
    finally:
        store.remove(track_id)


def test_loosened_interval_makes_flight_not_due_yet():
    track_id = _fresh_track_id("t4")
    checked_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    now = datetime.now(timezone.utc)
    store.add(track_id, "DL244", "2026-08-18", None, interval_minutes=15)
    try:
        # 20 min elapsed, but a DISTANT-horizon flight now polls at 360 min.
        store.mark_checked(track_id, checked_at, "LOW", interval_minutes=360)
        due_ids = {r["track_id"] for r in store.due_for_check(now)}
        assert track_id not in due_ids
    finally:
        store.remove(track_id)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
