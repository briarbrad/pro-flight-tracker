"""Regression test for the circuit breaker only checking HTTP status.

Background: run_script() used to record breaker success/failure purely from
the subprocess/dispatch HTTP status code (`status == 200`), even when the
response body itself was `{"error": "..."}` with no usable payload. Every
provider script in scripts/ can return exactly that shape on a 200 (e.g. a
timeout inside a try/except that still returns 200 with an error key) --
which meant a real, sustained upstream outage never tripped the breaker, so
callers kept paying the full per-call timeout on every single request
instead of failing fast after a few consecutive failures.

Separately, a 200-with-embedded-error response used to get cached as if it
were good data, so a transient bad response could get served back as
"fresh" to every caller for the rest of its TTL.

This test pins _is_upstream_failure()'s contract: a bare {"error": ...} with
no other content counts as a failure, but a script's own documented partial
degradation shape -- {"errors": [...]} alongside real content keys (data,
flights, results, etc.) -- must NOT trip the breaker, matching the
error/errors convention used consistently across flight_data.py,
aviation_weather.py, airport_ops.py, and swim_consumer.py.

Run with: pytest tests/test_breaker_body_check.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _is_upstream_failure  # noqa: E402


def test_bare_error_with_no_content_is_a_failure():
    """The core regression case: an all-error, no-content 200 body must be
    detected as an upstream failure so the breaker actually trips."""
    assert _is_upstream_failure({"error": "AeroAPI timeout after 10s"}) is True


def test_error_alongside_real_content_is_not_a_failure():
    """airport_ops.py's lightning capture reports a WebSocket error but
    still returns any strikes/ramp_alerts collected before it dropped --
    that is a degraded-but-useful response, not an outage."""
    assert _is_upstream_failure(
        {"error": "WebSocket closed early", "strikes": [{"lat": 1, "lon": 2}]}
    ) is False


def test_partial_multi_source_errors_list_is_not_a_failure():
    """The {"errors": [...]} convention (partial/graceful multi-source
    degradation) is a completely different signal from a top-level
    "error" key and must never trip the breaker on its own."""
    assert _is_upstream_failure(
        {"data": {"flights": [{"ident": "DL244"}]}, "errors": ["METAR unavailable"]}
    ) is False


def test_successful_response_with_no_error_key_is_not_a_failure():
    assert _is_upstream_failure({"data": {"flights": []}}) is False


def test_non_dict_body_is_not_a_failure():
    """A None/list/str body can't be introspected for an error key -- treat
    as not-a-failure here; the HTTP status code alone still governs those
    cases in run_script()."""
    assert _is_upstream_failure(None) is False
    assert _is_upstream_failure([1, 2, 3]) is False
    assert _is_upstream_failure("boom") is False


def test_error_with_falsy_content_values_is_still_a_failure():
    """Empty containers under the recognized content keys mean nothing was
    actually recovered -- still a failure, not a partial success."""
    assert _is_upstream_failure(
        {"error": "no data", "flights": [], "results": None}
    ) is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
