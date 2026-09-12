"""Narrative + chat (OpenRouter)."""

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

import store
import analysis
import flow_brief

from pft.runner import (
    CHECK_TIMEOUT, DEFAULT_TIMEOUT, SWIM_TIMEOUT,
    run_script, run_scripts_parallel, _status_prefetch_env,
)
from pft.validation import (
    _STRAY, _extract_flights, _is_conus,
    airport_list, clean_date, clean_duration, clean_ident, clean_int,
    clean_param, rekey_airports, to_faa, to_icao,
)
from pft.cache import (
    _annotated, _cache_get, _cache_key, _cache_put, _nocache_requested,
    _script_cache, _ttl_for,
)
from pft.breakers import _breaker_open, _breaker_record, _breaker_states
from pft.llm import (
    CHAT_MAX_MESSAGE_CHARS, CHAT_MAX_MESSAGES, CHAT_MAX_TOKENS,
    CHAT_REASONING_MAX_TOKENS, NARRATIVE_CACHE_TTL_SECONDS,
    NARRATIVE_FALLBACK_MODEL, NARRATIVE_MAX_TOKENS, NARRATIVE_MODEL,
    NARRATIVE_REASONING_MAX_TOKENS, OPENROUTER_API_URL,
    _chat_cache_key, _llm_usage_record, _narrative_cache,
    _narrative_cache_key, _openrouter_call_with_fallback,
    _openrouter_extract_text,
)
from pft.logging import log

bp = Blueprint("narrative", __name__)


@bp.route("/api/narrative", methods=["POST"])
def api_narrative():
    """Turn a brief's llm_payload into a short plain-English narrative.

    Body: {"system": str, "user": str, "facts": <json>} — exactly the shape
    of the `llm_payload` object /api/brief already returns, sent back
    unmodified. Response: {"narrative": str, "cached": bool}.
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not openrouter_key:
        return jsonify({
            "error": "AI narrative is not configured on the server",
            "hint": "Set OPENROUTER_API_KEY on Railway (create a key at "
                    "https://openrouter.ai/settings/keys)",
        }), 501

    body = request.get_json(silent=True) or {}
    system = (body.get("system") or "").strip()
    user = (body.get("user") or "").strip()
    facts = body.get("facts")
    if not system or not user:
        return jsonify({"error": "Both 'system' and 'user' are required"}), 400

    cache_key = _narrative_cache_key(system, user, facts)
    cached = _narrative_cache.get_fresh(cache_key)
    if cached is not None:
        return jsonify({"narrative": cached, "cached": True}), 200

    user_content = user
    if facts is not None:
        user_content += "\n" + json.dumps(facts, sort_keys=True, default=str)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    text, err = _openrouter_call_with_fallback(
        openrouter_key, messages, max_tokens=NARRATIVE_MAX_TOKENS,
        kind="narrative")
    if err is not None:
        return err

    _narrative_cache.put(cache_key, text, NARRATIVE_CACHE_TTL_SECONDS)
    return jsonify({"narrative": text, "cached": False}), 200


@bp.route("/api/chat", methods=["POST"])
def api_chat():
    """Interactive follow-up chat about one flight, grounded in the same
    `facts` the narrative feature uses (from /api/brief's `llm_payload.
    facts`) plus the running conversation so far.

    Body: {
      "flight": str, "date": str,       # for the system prompt's framing
      "facts": <json>,                   # the same facts object as /api/narrative
      "messages": [{"role": "user"|"assistant", "content": str}, ...]
    }
    The client owns conversation history and resends it each turn --
    the server is stateless and rebuilds the system prompt fresh every
    request, so there's nothing to expire or clean up between calls.
    Identical resends (client retry, double-tap) are served from the
    shared LLM cache; a new question is a different cache key.
    Response: {"reply": str, "cached": bool}.
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not openrouter_key:
        return jsonify({
            "error": "AI chat is not configured on the server",
            "hint": "Set OPENROUTER_API_KEY on Railway (create a key at "
                    "https://openrouter.ai/settings/keys)",
        }), 501

    body = request.get_json(silent=True) or {}
    flight = (body.get("flight") or "").strip()
    date = (body.get("date") or "").strip()
    facts = body.get("facts")
    raw_messages = body.get("messages")

    if not flight or not date:
        return jsonify({"error": "Both 'flight' and 'date' are required"}), 400
    if facts is None:
        return jsonify({"error": "'facts' is required"}), 400
    if not isinstance(raw_messages, list) or not raw_messages:
        return jsonify({"error": "'messages' must be a non-empty list"}), 400

    cleaned = []
    for m in raw_messages:
        if not isinstance(m, dict):
            return jsonify({"error": "Each message must be an object"}), 400
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            return jsonify({
                "error": "Each message needs role 'user'/'assistant' and non-empty content",
            }), 400
        if len(content) > CHAT_MAX_MESSAGE_CHARS:
            content = content[:CHAT_MAX_MESSAGE_CHARS]
        cleaned.append({"role": role, "content": content})

    if cleaned[-1]["role"] != "user":
        return jsonify({"error": "The last message must be from 'user'"}), 400

    # Keep only the most recent turns -- bounds both the request size sent
    # to OpenRouter and how many free-tier calls one runaway conversation
    # can rack up against the shared daily quota.
    if len(cleaned) > CHAT_MAX_MESSAGES:
        cleaned = cleaned[-CHAT_MAX_MESSAGES:]
        if cleaned[0]["role"] != "user":
            cleaned = cleaned[1:]

    system = analysis.build_chat_system_prompt(flight, date, facts)
    messages = [{"role": "system", "content": system}] + cleaned

    # Cache on the exact (flight, date, facts, messages) tuple: a resend of
    # the same turn is byte-identical and shouldn't re-burn free-tier quota.
    chat_key = _chat_cache_key(flight, date, facts, cleaned)
    cached = _narrative_cache.get_fresh(chat_key)
    if cached is not None:
        return jsonify({"reply": cached, "cached": True}), 200

    text, err = _openrouter_call_with_fallback(
        openrouter_key, messages, max_tokens=CHAT_MAX_TOKENS,
        reasoning_max_tokens=CHAT_REASONING_MAX_TOKENS, kind="chat")
    if err is not None:
        return err

    _narrative_cache.put(chat_key, text, NARRATIVE_CACHE_TTL_SECONDS)
    return jsonify({"reply": text, "cached": False}), 200
