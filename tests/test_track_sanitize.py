"""Regression tests for POST/DELETE /api/track input sanitizing.

The safety-batch pass cleaned flight/date on /api/check, /api/flight/*,
and /api/brief, but /api/track wrote the raw JSON body straight into the
store. Two concrete failures followed:

  1. Case-folded idents (`dl244` vs `DL244`) created a different track_id
     than every other endpoint, so DELETE couldn't find what POST wrote
     and the tracker polled a differently-cased ident than /api/brief.
  2. `date=""` / `date=null` overrode the UTC-today default (dict.get only
     substitutes when the key is missing) and stored track_id "DL244_" or
     "DL244_None", which the tracker then billed AeroAPI for forever.

Run with: pytest tests/test_track_sanitize.py -v
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import store  # noqa: E402


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _reset_store():
    with store._mem_lock:
        store._mem.clear()


def test_post_uppercases_flight_and_builds_canonical_track_id():
    _reset_store()
    client = _client()
    resp = client.post("/api/track", json={
        "flight": "dl244",
        "date": "2026-09-12",
        "push_token": "ExponentPushToken[test]",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["track_id"] == "DL244_2026-09-12"
    assert body["status"] == "tracking"
    stored = store.list_all()
    assert len(stored) == 1
    assert stored[0]["flight"] == "DL244"
    assert stored[0]["date"] == "2026-09-12"


def test_post_empty_date_defaults_to_utc_today_not_blank():
    _reset_store()
    client = _client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = client.post("/api/track", json={
        "flight": "UA100",
        "date": "",
        "push_token": "ExponentPushToken[test]",
    })
    assert resp.status_code == 200
    assert resp.get_json()["track_id"] == f"UA100_{today}"


def test_post_null_date_defaults_to_utc_today():
    _reset_store()
    client = _client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = client.post("/api/track", json={
        "flight": "AA200",
        "date": None,
        "push_token": "ExponentPushToken[test]",
    })
    assert resp.status_code == 200
    assert resp.get_json()["track_id"] == f"AA200_{today}"


def test_post_invalid_date_defaults_to_utc_today():
    _reset_store()
    client = _client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = client.post("/api/track", json={
        "flight": "B6300",
        "date": "not-a-date",
        "push_token": "ExponentPushToken[test]",
    })
    assert resp.status_code == 200
    assert resp.get_json()["track_id"] == f"B6300_{today}"


def test_post_invalid_flight_is_400():
    _reset_store()
    client = _client()
    resp = client.post("/api/track", json={
        "flight": "!!!",
        "date": "2026-09-12",
        "push_token": "ExponentPushToken[test]",
    })
    assert resp.status_code == 400


def test_delete_uses_same_canonical_track_id():
    _reset_store()
    client = _client()
    client.post("/api/track", json={
        "flight": "dl244",
        "date": "2026-09-12",
        "push_token": "ExponentPushToken[test]",
    })
    resp = client.delete("/api/track?flight=dl244&date=2026-09-12")
    assert resp.status_code == 200
    assert resp.get_json()["track_id"] == "DL244_2026-09-12"
    assert store.list_all() == []


def test_delete_missing_flight_is_400():
    client = _client()
    resp = client.delete("/api/track")
    assert resp.status_code == 400


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
