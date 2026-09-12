"""Regression tests for the SWIM --limit query parameter.

--limit was hardcoded at 50 in swim_consumer.py with no HTTP query
parameter to raise it, so filtered_results==50 meant "at least 50, we
threw the rest away". _swim_call now forwards a clamped limit, and the
daemon read path already honored --limit when present.

Run with: pytest tests/test_swim_limit.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _swim_argv_to_query, clean_int  # noqa: E402


def test_clean_int_clamps_to_bounds():
    assert clean_int("0", 50, 1, 200) == "1"
    assert clean_int("999", 50, 1, 200) == "200"
    assert clean_int("75", 50, 1, 200) == "75"
    assert clean_int("nope", 50, 1, 200) == "50"
    assert clean_int("", 50, 1, 200) == "50"


def test_swim_argv_parses_limit():
    q = _swim_argv_to_query(
        ["tfms-flight", "--flight", "DAL244", "--duration", "8", "--limit", "120"])
    assert q["feed"] == "tfms-flight"
    assert q["flight"] == "DAL244"
    assert q["limit"] == 120


def test_swim_argv_defaults_limit_to_50():
    q = _swim_argv_to_query(["tfms-flow", "--keyword", "GDP", "--duration", "12"])
    assert q["limit"] == 50


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
