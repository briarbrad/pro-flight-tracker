"""Expanded /health: upstream config + breaker state, no paid probes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def test_health_reports_upstream_config_without_secrets(monkeypatch):
    monkeypatch.setenv("AEROAPI_KEY", "sekret-aero")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sekret-or")
    monkeypatch.setenv("SWIM_USERNAME", "swimuser")
    monkeypatch.setenv("SWIM_PASSWORD", "sekret-swim")
    client = app_module.app.test_client()
    body = client.get("/health").get_json()

    up = body["upstreams"]
    assert up["aeroapi"]["configured"] is True
    assert up["openrouter"]["configured"] is True
    assert up["openrouter"]["model"] == app_module.NARRATIVE_MODEL
    assert up["swim"]["username_configured"] is True
    assert up["swim"]["password_configured"] is True
    # No secret values leak into the payload.
    raw = str(body)
    for secret in ("sekret-aero", "sekret-or", "sekret-swim"):
        assert secret not in raw

    assert body["rate_limit"]["per_minute"] == app_module.RATE_LIMIT_PER_MIN
    assert body["rate_limit"]["scope"] in ("shared", "per-worker")
    assert isinstance(body["lightning_inflight"], int)
    assert "breakers" in body


def test_health_reports_unconfigured_upstreams(monkeypatch):
    for var in ("AEROAPI_KEY", "OPENROUTER_API_KEY", "SWIM_USERNAME",
                "SWIM_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    client = app_module.app.test_client()
    up = client.get("/health").get_json()["upstreams"]
    assert up["aeroapi"]["configured"] is False
    assert up["openrouter"]["configured"] is False
    assert up["swim"]["password_configured"] is False
    # Keyless sources are always available.
    assert up["blitzortung"]["configured"] is True
