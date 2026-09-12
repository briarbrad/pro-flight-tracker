"""Regression tests for the bearer-auth / rate-limit gate.

hmac.compare_digest raises ValueError when its two inputs differ in
length. The documented rollout is (1) set API_TOKEN on Railway while
auth is still dormant, (2) ship the client sending a bearer token, (3)
flip REQUIRE_AUTH=1. If the client is already sending a differently-sized
token (placeholder, stale build, empty-but-header-present), step (1)
used to 500 every request — including in log-only mode — instead of
treating it as "not authed".

These tests pin that mismatch is False, not an exception, and that
REQUIRE_AUTH=1 returns 401 rather than 500 for a wrong-length token.
/health stays reachable either way.

Run with: pytest tests/test_auth_gate.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _reset_rate_buckets():
    with app_module._rate_lock:
        app_module._rate_buckets.clear()


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_token_ok_same_value_is_true():
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "correct-token-value"}):
        assert app_module._token_ok("correct-token-value") is True


def test_token_ok_wrong_length_is_false_not_valueerror():
    """The core regression: a shorter/longer supplied token must not raise."""
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "sixteen-char-tok"}):
        assert app_module._token_ok("short") is False
        assert app_module._token_ok("this-token-is-much-longer-than-expected") is False
        assert app_module._token_ok("") is False


def test_token_ok_same_length_wrong_value_is_false():
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "abcdefgh"}):
        assert app_module._token_ok("hgfedcba") is False


def test_dormant_auth_serves_wrong_length_bearer_instead_of_500():
    """REQUIRE_AUTH unset: a mismatched bearer used to 500 the request."""
    _reset_rate_buckets()
    client = _client()
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "server-token-16", "REQUIRE_AUTH": ""}):
        resp = client.get("/api/tracked",
                          headers={"Authorization": "Bearer short"})
    assert resp.status_code == 200
    assert "error" not in (resp.get_json() or {}) or resp.get_json().get("tracked") is not None


def test_require_auth_wrong_length_token_is_401_not_500():
    _reset_rate_buckets()
    client = _client()
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "server-token-16", "REQUIRE_AUTH": "1"}):
        resp = client.get("/api/tracked",
                          headers={"Authorization": "Bearer short"})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["error"] == "Unauthorized"


def test_require_auth_matching_token_is_ok():
    _reset_rate_buckets()
    client = _client()
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "server-token-16", "REQUIRE_AUTH": "1"}):
        resp = client.get("/api/tracked",
                          headers={"Authorization": "Bearer server-token-16"})
    assert resp.status_code == 200


def test_health_is_exempt_from_auth():
    client = _client()
    with __import__("unittest.mock").mock.patch.dict(
            os.environ, {"API_TOKEN": "server-token-16", "REQUIRE_AUTH": "1"}):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["version"] == "1.12"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
