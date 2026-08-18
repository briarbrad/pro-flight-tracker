"""Regression test for the background tracker's polling cadence.

Background: the tracker loop already computed `tracked_phase` and `horizon`
on every pass (needed for the risk assessment itself), but store.py's
due_for_check() used a single, fixed `interval_minutes` column that was only
ever set once, at track-creation time, from whatever the client happened to
request. A flight tracked a day out at a 15-minute interval stayed on that
15-minute cadence for its entire lifetime -- burning AeroAPI queries on a
flight that couldn't possibly have new information yet -- while a flight
that had progressed to TAXI_OUT (where minute-to-minute EDCT/queue changes
actually matter) got no tighter than whatever interval was requested hours
earlier.

analysis.tracking_interval_minutes() reuses the existing refresh_interval()
phase/horizon bands (already used for the client-facing
`refresh_after_seconds` hint) so the tracker's own polling cadence and the
client's staleness hint always agree about how urgent a given flight is.

Run with: pytest tests/test_tracking_interval.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis  # noqa: E402


def test_taxi_out_gets_tight_interval():
    """TAXI_OUT refreshes every 300s (5 min) per _REFRESH_BY_PHASE -- the
    tracker should check exactly that often, its own floor."""
    minutes = analysis.tracking_interval_minutes(
        horizon={"band": "SAME_DAY"}, phase={"phase": "TAXI_OUT"})
    assert minutes == 5


def test_airborne_gets_fifteen_minutes():
    minutes = analysis.tracking_interval_minutes(
        horizon={"band": "SAME_DAY"}, phase={"phase": "AIRBORNE"})
    assert minutes == 15


def test_distant_horizon_gets_slow_cadence_not_default_fifteen():
    """The core regression case: a flight still a day+ out must NOT be
    checked on the same tight cadence as one that's about to depart. DISTANT
    maps to 21600s = 360 min, right at the tracker's own slow-end clamp."""
    minutes = analysis.tracking_interval_minutes(
        horizon={"band": "DISTANT"}, phase={"phase": "SCHEDULED"})
    assert minutes == 360


def test_imminent_horizon_gets_five_minutes():
    minutes = analysis.tracking_interval_minutes(
        horizon={"band": "IMMINENT"}, phase={"phase": "SCHEDULED"})
    assert minutes == 5


def test_unknown_band_falls_back_to_default_without_erroring():
    minutes = analysis.tracking_interval_minutes(horizon={}, phase={})
    assert minutes == 30  # 1800s default from _REFRESH_BY_BAND["UNKNOWN"]


def test_finished_flight_falls_back_to_slow_clamp_not_none():
    """refresh_interval() returns None for ARRIVED/CANCELLED (nothing more
    will change). The tracker removes finished flights right after this is
    computed, but the function itself must never propagate None into a
    minutes value the caller could pass straight to a sleep/interval column."""
    minutes = analysis.tracking_interval_minutes(
        horizon={"band": "SAME_DAY"}, phase={"phase": "ARRIVED"})
    assert minutes == 360


def test_result_is_always_within_tracker_clamp_bounds():
    """Every phase/band combination must land inside the tracker's own
    [5, 360] minute clamp -- distinct from the narrower client-supplied
    clamp used only at track-creation time."""
    phases = [None, "TAXI_OUT", "TAXI_IN", "AIRBORNE", "ARRIVED",
              "CANCELLED", "SCHEDULED"]
    bands = [None, "IMMINENT", "NEAR", "SAME_DAY", "NEXT_DAY", "DISTANT",
             "UNKNOWN"]
    for phase_name in phases:
        for band in bands:
            minutes = analysis.tracking_interval_minutes(
                horizon={"band": band} if band else {},
                phase={"phase": phase_name} if phase_name else {})
            assert 5 <= minutes <= 360, (phase_name, band, minutes)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
