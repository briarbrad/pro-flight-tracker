"""Regression tests for the server-side /api/chat proxy.

Background: this is the interactive counterpart to /api/narrative -- instead
of one self-contained synthesis, the traveller can ask free-form follow-up
questions about a flight ("why is there a weather delay when the skies are
clear", "what if the inbound flight is delayed further", "do you think the
delay increases") and get a conversational answer grounded in the same
`facts` object /api/brief already computes. Upstream is the same OpenRouter
Free Models Router (`openrouter/free`) as /api/narrative, sharing the same
reasoning-token-budget mitigation and empty-content fallback retry.

These tests stub out the outbound call to OpenRouter (requests.post) so they
run with no network access and no real credentials, and cover:
  - 501 when the server-side OPENROUTER_API_KEY isn't configured
  - 400 for each required-field validation case
  - a successful call extracts the reply text and forwards conversation history
  - the empty-content-from-openrouter/free retry-to-fallback-model behavior
    (same mitigation as /api/narrative, exercised here independently)
  - message-count and message-length bounds are enforced server-side

Run with: pytest tests/test_chat_proxy.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _base_body(**overrides):
    body = {
        "flight": "DL244",
        "date": "2026-08-21",
        "facts": {"branch_classification": {"branch": "A"}, "horizon": {"band": "MID"}},
        "messages": [{"role": "user", "content": "Why is there a weather delay when the skies are clear?"}],
    }
    body.update(overrides)
    return body


def _ok_resp(text: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def test_returns_501_when_openrouter_key_not_configured():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
        resp = client.post("/api/chat", json=_base_body())
    assert resp.status_code == 501
    assert "not configured" in resp.get_json()["error"]


def test_returns_400_when_flight_or_date_missing():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=_base_body(flight=""))
    assert resp.status_code == 400


def test_returns_400_when_facts_missing():
    client = _client()
    body = _base_body()
    del body["facts"]
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 400


def test_returns_400_when_messages_missing_or_empty():
    client = _client()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=_base_body(messages=[]))
    assert resp.status_code == 400


def test_returns_400_when_last_message_is_not_from_user():
    client = _client()
    body = _base_body(messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 400


def test_returns_400_when_a_message_has_bad_role():
    client = _client()
    body = _base_body(messages=[{"role": "system", "content": "hi"}])
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 400


def test_successful_call_forwards_conversation_and_extracts_reply():
    client = _client()
    body = _base_body(messages=[
        {"role": "user", "content": "Why is there a weather delay when the skies are clear?"},
        {"role": "assistant", "content": "It's usually a downstream program, not local weather."},
        {"role": "user", "content": "What if the inbound flight is delayed further?"},
    ])
    fake_resp = _ok_resp("If the inbound is delayed further, the turn time shrinks further.")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp) as mock_post:
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 200
    assert resp.get_json()["reply"] == "If the inbound is delayed further, the turn time shrinks further."
    sent_messages = mock_post.call_args.kwargs["json"]["messages"]
    # system prompt + all 3 conversation messages, in order, roles preserved
    assert sent_messages[0]["role"] == "system"
    assert "DL244" in sent_messages[0]["content"]
    assert [m["role"] for m in sent_messages[1:]] == ["user", "assistant", "user"]
    assert sent_messages[-1]["content"] == "What if the inbound flight is delayed further?"
    assert mock_post.call_args.kwargs["json"]["model"] == "openrouter/free"
    assert mock_post.call_args.kwargs["json"]["reasoning"] == {
        "max_tokens": app_module.CHAT_REASONING_MAX_TOKENS,
    }


def test_empty_content_retries_pinned_fallback_model():
    client = _client()
    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {
        "choices": [{"message": {"content": None, "reasoning": "thinking..."}}]
    }
    good_resp = _ok_resp("It should hold steady unless the inbound slips more.")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post",
                     side_effect=[empty_resp, good_resp]) as mock_post:
        resp = client.post("/api/chat", json=_base_body())
    assert resp.status_code == 200
    assert resp.get_json()["reply"] == "It should hold steady unless the inbound slips more."
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].kwargs["json"]["model"] == app_module.NARRATIVE_FALLBACK_MODEL


def test_openrouter_error_status_passes_through():
    client = _client()
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp):
        resp = client.post("/api/chat", json=_base_body())
    assert resp.status_code == 429


def test_message_history_is_truncated_to_max_messages():
    client = _client()
    # One more than the cap, alternating roles, ending on 'user'.
    messages = []
    for i in range(app_module.CHAT_MAX_MESSAGES + 1):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"turn {i}"})
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "final question"})
    body = _base_body(messages=messages)
    fake_resp = _ok_resp("ok")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
         patch.object(app_module.requests, "post", return_value=fake_resp) as mock_post:
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 200
    sent_messages = mock_post.call_args.kwargs["json"]["messages"]
    # system prompt + at most CHAT_MAX_MESSAGES conversation turns
    assert len(sent_messages) - 1 <= app_module.CHAT_MAX_MESSAGES
    assert sent_messages[-1]["content"] == messages[-1]["content"]


def test_oversized_facts_returns_400():
    client = _client()
    body = _base_body(facts={"padding": "x" * (app_module.CHAT_MAX_FACTS_CHARS + 100)})
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}):
        resp = client.post("/api/chat", json=body)
    assert resp.status_code == 400


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
