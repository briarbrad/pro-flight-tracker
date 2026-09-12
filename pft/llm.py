"""OpenRouter narrative/chat: breaker, cache, usage accounting."""

import hashlib
import json
import os
import threading
import time

import requests

from flask import jsonify

from pft.cache import _TTLCache
from pft.breakers import _breaker_open, _breaker_record
from pft.logging import log

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


NARRATIVE_MODEL = "openrouter/free"


# openrouter/free's random pool includes reasoning models (e.g. DeepSeek R1
# free) alongside plain instruct models, and OpenRouter's own docs give no
# guarantee that a reasoning model won't spend its entire max_tokens budget
# on internal "thinking" tokens before writing anything into `content` --
# in which case the visible answer comes back empty even though the call
# itself succeeded (billed $0, shows up in OpenRouter's Activity log). We
# mitigate two ways: (1) cap reasoning spend via `reasoning.max_tokens` on
# models that honor it, leaving headroom in the overall budget for the
# actual answer, and (2) if a call still comes back empty, retry once
# against a pinned, known non-reasoning free model rather than re-rolling
# the random router and risking the same outcome again.
NARRATIVE_MAX_TOKENS = 900


NARRATIVE_REASONING_MAX_TOKENS = 300


NARRATIVE_FALLBACK_MODEL = "meta-llama/llama-3.2-3b-instruct:free"


NARRATIVE_CACHE_TTL_SECONDS = max(
    0, int(os.environ.get("NARRATIVE_CACHE_TTL_SECONDS", "180") or 180))


NARRATIVE_CACHE_MAX_ENTRIES = 200


# /api/chat -- interactive follow-up Q&A about one flight, grounded in the
# same `facts` the narrative feature uses. Shares NARRATIVE_MODEL /
# NARRATIVE_FALLBACK_MODEL and the same empty-content retry mitigation.
# Identical resends are cached (same shared cache, "chat:" key namespace);
# a new question is a different key. Still bounded harder than narrative
# since a chat can rack up many more calls per flight than one narrative
# ever would against the shared 50-1000/day openrouter/free quota.
CHAT_MAX_TOKENS = 700


CHAT_REASONING_MAX_TOKENS = 250


CHAT_MAX_MESSAGES = 20  # ~10 back-and-forth turns kept; client trims further back


CHAT_MAX_MESSAGE_CHARS = 4000


# One shared bounded TTL cache for both /api/narrative and /api/chat —
# previously a second hand-rolled lock/dict/eviction beside the script
# cache. Keys are namespaced ("narrative:"/"chat:") so the two features
# can't collide.
_narrative_cache = _TTLCache(max_entries=NARRATIVE_CACHE_MAX_ENTRIES)


def _narrative_cache_key(system: str, user: str, facts) -> str:
    blob = json.dumps({"system": system, "user": user, "facts": facts},
                      sort_keys=True, default=str)
    return "narrative:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _chat_cache_key(flight: str, date: str, facts, messages) -> str:
    blob = json.dumps({"flight": flight, "date": date, "facts": facts,
                       "messages": messages},
                      sort_keys=True, default=str)
    return "chat:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Per-caller OpenRouter usage accounting. Keyed by _client_key() (token
# prefix or IP — the same bucketing the rate limiter uses). Bounded at
# 1000 callers, oldest evicted. /health reports aggregates only; the
# per-caller breakdown stays in-process for ops.
_LLM_USAGE_MAX_CALLERS = 1000


_llm_usage_lock = threading.Lock()


_llm_usage: dict[str, dict] = {}


def _llm_usage_record(kind: str) -> None:
    """Count one OpenRouter call (attempt, success or failure) per caller."""
    try:
        key = _client_key()
    except Exception:
        key = "unknown"
    now = time.monotonic()
    with _llm_usage_lock:
        entry = _llm_usage.get(key)
        if entry is None:
            entry = {"narrative": 0, "chat": 0, "last_seen": now}
            _llm_usage[key] = entry
        entry[kind] = entry.get(kind, 0) + 1
        entry["last_seen"] = now
        if len(_llm_usage) > _LLM_USAGE_MAX_CALLERS:
            oldest = min(_llm_usage,
                         key=lambda k: _llm_usage[k]["last_seen"])
            del _llm_usage[oldest]


def _llm_usage_summary() -> dict:
    with _llm_usage_lock:
        return {
            "narrative_calls": sum(e["narrative"] for e in _llm_usage.values()),
            "chat_calls": sum(e["chat"] for e in _llm_usage.values()),
            "distinct_callers": len(_llm_usage),
        }


def _openrouter_call(openrouter_key: str, messages: list, model: str,
                     cap_reasoning: bool, max_tokens: int,
                     reasoning_max_tokens: int = NARRATIVE_REASONING_MAX_TOKENS):
    """POST one chat-completion request to OpenRouter.

    Shared by /api/narrative and /api/chat so both get the same headers,
    timeout, and reasoning-token-budget mitigation (see NARRATIVE_MODEL
    comment above for why that mitigation exists).
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if cap_reasoning:
        payload["reasoning"] = {"max_tokens": reasoning_max_tokens}
    return requests.post(
        OPENROUTER_API_URL,
        json=payload,
        headers={"Authorization": f"Bearer {openrouter_key}",
                 "Content-Type": "application/json",
                 # Optional per OpenRouter's docs, but they use this to
                 # attribute traffic on their public rankings page.
                 "HTTP-Referer": "https://pro-flight-tracker-production.up.railway.app",
                 "X-Title": "Pro Flight Tracker"},
        timeout=45,
    )


def _openrouter_extract_text(resp):
    """Returns (text, error_response_or_None) from a successful (200) resp."""
    try:
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError):
        return None, (jsonify({"error": "Narrative service returned an unexpected shape"}), 502)
    return text, None


def _openrouter_call_with_fallback(openrouter_key: str, messages: list,
                                   max_tokens: int,
                                   reasoning_max_tokens: int = NARRATIVE_REASONING_MAX_TOKENS,
                                   kind: str = "narrative"):
    """Calls openrouter/free (reasoning-capped), and if `content` still comes
    back empty -- e.g. the router landed on a reasoning model that spent its
    whole budget thinking -- retries once against a pinned non-reasoning
    free model. Returns (text_or_None, error_response_or_None) where the
    error is already a (jsonify(...), status) tuple ready to `return`.

    Circuit-breaks on the shared "openrouter" breaker: after 3 consecutive
    failures, calls fail fast with 503 for the cooldown instead of burning
    the 45s timeout each time. Every attempt (breaker-passed) is counted
    per caller in the usage ledger.
    """
    retry_in = _breaker_open("openrouter")
    if retry_in:
        return None, (jsonify({
            "error": "Narrative service temporarily unavailable "
                     "(circuit open after repeated failures)",
            "retry_after_seconds": retry_in,
        }), 503)

    _llm_usage_record(kind)

    try:
        resp = _openrouter_call(openrouter_key, messages, NARRATIVE_MODEL,
                                cap_reasoning=True, max_tokens=max_tokens,
                                reasoning_max_tokens=reasoning_max_tokens)
    except requests.RequestException as exc:
        _breaker_record("openrouter", False)
        return None, (jsonify({
            "error": f"Narrative service unreachable: {type(exc).__name__}",
        }), 502)

    if resp.status_code != 200:
        _breaker_record("openrouter", False)
        # Pass OpenRouter's own status through — the client's error handling
        # already has copy for 401/402/429 and a generic fallback for others.
        return None, (jsonify({
            "error": f"Narrative service error ({resp.status_code})",
        }), resp.status_code)

    text, err = _openrouter_extract_text(resp)
    if err is not None:
        _breaker_record("openrouter", False)
        return None, err

    if not text:
        _llm_usage_record(kind)  # the fallback-model retry is a real request too
        try:
            retry_resp = _openrouter_call(openrouter_key, messages,
                                          NARRATIVE_FALLBACK_MODEL,
                                          cap_reasoning=False,
                                          max_tokens=max_tokens)
        except requests.RequestException as exc:
            _breaker_record("openrouter", False)
            return None, (jsonify({
                "error": f"Narrative service unreachable: {type(exc).__name__}",
            }), 502)
        if retry_resp.status_code != 200:
            _breaker_record("openrouter", False)
            return None, (jsonify({
                "error": f"Narrative service error ({retry_resp.status_code})",
            }), retry_resp.status_code)
        text, err = _openrouter_extract_text(retry_resp)
        if err is not None:
            _breaker_record("openrouter", False)
            return None, err

    if not text:
        _breaker_record("openrouter", False)
        return None, (jsonify({"error": "The narrative came back empty"}), 502)

    _breaker_record("openrouter", True)
    return text, None
