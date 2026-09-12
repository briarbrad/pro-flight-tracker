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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_llm_state():
    """Breaker failures, usage counters, and the LLM cache are module-global;
    reset them so tests can't trip each other's circuit breaker."""
    app_module._narrative_cache.clear()
    with app_module._breaker_lock:
        app_module._breakers.pop("openrouter", None)
    with app_module._llm_usage_lock:
        app_module._llm_usage.clear()
    yield
    app_module._narrative_cache.clear()
    with app_module._breaker_lock:
        app_module._breakers.pop("openrouter", None)
    with app_module._llm_usage_lock:
        app_module._llm_usage.clear()


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _clear_narrative_cache():
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


def _ok_resp(text="Reply text."):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _err_resp(status=429):
    resp = MagicMock()
    resp.status_code = status
    return resp


def _narrative_payload(**kw):
    payload = {"system": "s", "user": "u", "facts": {"a": 1}}
    payload.update(kw)
    return payload


def test_breaker_trips_after_three_consecutive_failures():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                      return_value=_err_resp(429)) as mock_post:
        r1 = client.post("/api/narrative", json=_narrative_payload(facts={"n": 1}))
        r2 = client.post("/api/narrative", json=_narrative_payload(facts={"n": 2}))
        r3 = client.post("/api/narrative", json=_narrative_payload(facts={"n": 3}))
        assert (r1.status_code, r2.status_code, r3.status_code) == (429, 429, 429)
        # Fourth call: breaker open, fails fast without touching the network.
        r4 = client.post("/api/narrative", json=_narrative_payload(facts={"n": 4}))
    assert r4.status_code == 503
    assert "retry_after_seconds" in r4.get_json()
    assert mock_post.call_count == 3


def test_breaker_resets_on_success():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                      side_effect=[_err_resp(500), _err_resp(500), _ok_resp()]):
        assert client.post("/api/narrative",
                           json=_narrative_payload(facts={"n": 1})).status_code == 500
        assert client.post("/api/narrative",
                           json=_narrative_payload(facts={"n": 2})).status_code == 500
        # Success clears the failure count: no 503 on the next call.
        ok = client.post("/api/narrative",
                         json=_narrative_payload(facts={"n": 3}))
        assert ok.status_code == 200


def _chat_payload(**kw):
    payload = {"flight": "DL5187", "date": "2026-09-12",
               "facts": {"phase": "AIRBORNE"},
               "messages": [{"role": "user", "content": "Will it be on time?"}]}
    payload.update(kw)
    return payload


def test_chat_identical_resend_is_cached():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                      return_value=_ok_resp("On time.")) as mock_post:
        first = client.post("/api/chat", json=_chat_payload())
        second = client.post("/api/chat", json=_chat_payload())
    assert first.get_json() == {"reply": "On time.", "cached": False}
    assert second.get_json() == {"reply": "On time.", "cached": True}
    mock_post.assert_called_once()


def test_chat_new_question_is_not_cached():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                      return_value=_ok_resp("Reply.")) as mock_post:
        client.post("/api/chat", json=_chat_payload())
        other = _chat_payload(
            messages=[{"role": "user", "content": "What about the return leg?"}])
        resp = client.post("/api/chat", json=other)
    assert resp.get_json()["cached"] is False
    assert mock_post.call_count == 2


def test_llm_usage_accounted_per_caller():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=_ok_resp()):
        client.post("/api/narrative", json=_narrative_payload(facts={"n": 1}))
        client.post("/api/narrative", json=_narrative_payload(facts={"n": 2}))
        client.post("/api/chat", json=_chat_payload())
        # Cache hit: not an upstream call, not counted.
        client.post("/api/chat", json=_chat_payload())
    summary = app_module._llm_usage_summary()
    assert summary["narrative_calls"] == 2
    assert summary["chat_calls"] == 1
    assert summary["distinct_callers"] == 1
    # /health surfaces the aggregates.
    health = client.get("/health")
    assert health.get_json()["llm_usage"] == summary


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
