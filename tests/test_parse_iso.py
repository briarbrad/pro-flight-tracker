"""_parse_iso must always return timezone-aware datetimes.

A naive ISO string (no offset/Z) previously parsed to a naive datetime,
which 500s when subtracted from an aware datetime downstream. The
canonical parser normalizes naive input to UTC.
"""
from datetime import datetime, timezone

import app


def test_naive_string_normalized_to_utc():
    dt = app._parse_iso("2026-09-12T10:00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)


def test_z_string_stays_utc():
    dt = app._parse_iso("2026-09-12T10:00:00Z")
    assert dt == datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)


def test_offset_preserved():
    dt = app._parse_iso("2026-09-12T10:00:00-04:00")
    assert dt.utcoffset().total_seconds() == -4 * 3600


def test_mixed_naive_aware_subtraction_does_not_raise():
    # This is the exact 500: naive API timestamp minus aware now.
    naive = app._parse_iso("2026-09-12T10:00:00")
    aware = app._parse_iso("2026-09-12T11:00:00Z")
    assert (aware - naive).total_seconds() == 3600


def test_garbage_returns_none():
    assert app._parse_iso(None) is None
    assert app._parse_iso("") is None
    assert app._parse_iso("not-a-time") is None
    assert app._parse_iso(123) is None
