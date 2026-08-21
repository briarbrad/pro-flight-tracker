"""Regression tests for the server-side /api/narrative proxy.

Background: NarrativeService.swift used to call Rork's AI toolkit directly
from the device with a bearer secret baked into the compiled app bundle and
therefore recoverable by anyone who decompiles the IPA or inspects the
device's own outbound traffic, entirely independent of this server's own
auth/rate-limit gate. /api/narrative moves that call server-side: the secret
now lives only in Railway's environment (`OPENROUTER_API_KEY`) and the
client sends its already-computed llm_payload here instead. Upstream is
OpenRouter's Free Models Router (`openrouter/free`) via OpenRouter's
OpenAI-compatible chat/completions endpoint.

These tests stub out the outbound call to OpenRouter (requests.post) so
they run with no network access and no real credentials, and cover:
  - 501 when the server-side OPENROUTER_API_KEY isn't configured
  - 400 when the request body is missing required fields
  - a successful call extracts the chat completion text
  - a second identical call is served from cache without calling out again
  - a non-200 OpenRouter response passes its status code straight through

Run with: pytest tests/test_narrative_proxy.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _clear_narrative_cache():
    with app_module._narrative_cache_lock:
        app_module._narrative_cache.clear()


def test_returns_501_when_openrouter_key_not_configured():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
        resp = client.post("/api/narrative", json={"system": "s", "user": "u"})
    assert resp.status_code == 501
    assert "not configured" in resp.get_json()["error"]


def test_returns_400_when_system_or_user_missing():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/narrative", json={"system": "", "user": "hi"})
    assert resp.status_code == 400


def test_successful_call_extracts_narrative_text():
    _clear_narrative_cache()
    client = _client()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "  Flight is on time.  "}}]
    }
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp) as mock_post:
        resp = client.post("/api/narrative",
                           json={"system": "sys prompt", "user": "user prompt",
                                 "facts": {"phase": "AIRBORNE"}})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["narrative"] == "Flight is on time."
    assert body["cached"] is False
    # The secret must never appear anywhere except the outbound Authorization
    # header sent to OpenRouter -- never echoed back to the client.
    assert "secret" not in str(resp.data)
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == app_module.OPENROUTER_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["json"]["model"] == "openrouter/free"


def test_second_identical_call_is_served_from_cache():
    _clear_narrative_cache()
    client = _client()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "Cached narrative."}}]
    }
    payload = {"system": "sys", "user": "user", "facts": {"phase": "TAXI_OUT"}}
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp) as mock_post:
        first = client.post("/api/narrative", json=payload)
        second = client.post("/api/narrative", json=payload)
    assert first.get_json()["cached"] is False
    assert second.get_json()["cached"] is True
    assert second.get_json()["narrative"] == "Cached narrative."
    # Only one real call to OpenRouter for two identical requests.
    mock_post.assert_called_once()


def test_openrouter_error_status_passes_through():
    _clear_narrative_cache()
    client = _client()
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp):
        resp = client.post("/api/narrative",
                           json={"system": "s", "user": "u", "facts": {"a": 1}})
    assert resp.status_code == 429


def test_empty_content_from_free_router_retries_pinned_fallback_model():
    """openrouter/free can land on a reasoning model (e.g. DeepSeek R1 free)
    that spends its whole token budget thinking and writes nothing to
    `content`. The endpoint should retry once against
    NARRATIVE_FALLBACK_MODEL rather than surfacing the empty result."""
    _clear_narrative_cache()
    client = _client()
    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {
        "choices": [{"message": {"content": None,
                                  "reasoning": "...spent the whole budget thinking..."}}]
    }
    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.json.return_value = {
        "choices": [{"message": {"content": "Flight is on time."}}]
    }
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                     side_effect=[empty_resp, good_resp]) as mock_post:
        resp = client.post("/api/narrative",
                           json={"system": "s", "user": "u", "facts": {"a": 1}})
    assert resp.status_code == 200
    assert resp.get_json()["narrative"] == "Flight is on time."
    assert mock_post.call_count == 2
    first_call, second_call = mock_post.call_args_list
    assert first_call.kwargs["json"]["model"] == "openrouter/free"
    assert "reasoning" in first_call.kwargs["json"]
    assert second_call.kwargs["json"]["model"] == app_module.NARRATIVE_FALLBACK_MODEL
    assert "reasoning" not in second_call.kwargs["json"]


def test_empty_content_from_both_attempts_returns_502():
    _clear_narrative_cache()
    client = _client()
    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {
        "choices": [{"message": {"content": ""}}]
    }
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=empty_resp) as mock_post:
        resp = client.post("/api/narrative",
                           json={"system": "s", "user": "u", "facts": {"a": 1}})
    assert resp.status_code == 502
    assert "empty" in resp.get_json()["error"]
    assert mock_post.call_count == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
