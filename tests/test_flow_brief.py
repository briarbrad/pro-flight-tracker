"""Adapters for /api/ops/flow-brief — empty SWIM is success, not a 500.

Run with: pytest tests/test_flow_brief.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import flow_brief  # noqa: E402


EMPTY_SWIM = {
    "results": [],
    "filtered_results": 0,
    "total_raw_messages": 0,
    "feed": "tfms-flow",
}


def test_empty_tfms_flow_is_success_not_error():
    out = flow_brief.interpret_tfms_flow(EMPTY_SWIM, origin="KJFK", dest="KEWR")
    assert out["advisories"] == []
    assert out["effects"] == []
    assert out["count"] == 0


def test_empty_tbfm_is_applicable_at_us_dest_with_empty_items():
    out = flow_brief.interpret_tbfm(EMPTY_SWIM, flight="DAL244", dest="KJFK")
    assert out["applicable"] is True
    assert out["items"] == []
    assert out["note"]


def test_tbfm_non_us_dest_is_not_applicable():
    out = flow_brief.interpret_tbfm(EMPTY_SWIM, flight="DAL244", dest="EGLL")
    assert out["applicable"] is False
    assert "outside the US NAS" in (out.get("note") or "")


def test_tfdm_undeployed_jfk_is_empty_success():
    out = flow_brief.interpret_tfdm(EMPTY_SWIM, airport="KJFK", flight="DAL244")
    assert out["applicable"] is False
    assert out["queue_wait_min"] is None
    assert "not in the TFDM" in (out.get("note") or "")


def test_tfdm_deployed_but_quiet_is_applicable_empty():
    out = flow_brief.interpret_tfdm(EMPTY_SWIM, airport="KEWR", flight="UAL100")
    assert out["applicable"] is True
    assert out["queue_wait_min"] is None
    assert out["earliest_wheels_up"] is None


def test_assemble_empty_swim_never_500s():
    brief = flow_brief.assemble_flow_brief(
        flight="DL244", date="2026-09-12",
        origin="KJFK", dest="EGLL",
        swim={"tfms_flow": EMPTY_SWIM, "tbfm": EMPTY_SWIM, "tfdm": EMPTY_SWIM},
        timings={"total": 0.01},
        sources_tried=["tfms-flow:origin"],
        sources_quiet=["tfms-flow:origin", "tbfm", "tfdm:origin"],
        aeroapi_queries_used=0,
    )
    assert brief["advisories"] == []
    assert brief["effects"] == []
    assert brief["metering"]["applicable"] is False  # EGLL
    assert brief["surface"]["applicable"] is False  # KJFK not TFDM
    assert brief["sources_tried"]
    assert brief["sources_quiet"]


def test_gdp_advisory_becomes_watch_effect():
    payload = {
        "results": [{
            "type": "tfms_advisory",
            "msg_type": "GADV",
            "advisory_number": "042",
            "title": "GDP FOR EWR",
            "text": "GROUND DELAY PROGRAM AT EWR DUE TO WEATHER",
            "effective_start": "2026-09-12T12:00:00Z",
            "effective_end": "2026-09-12T20:00:00Z",
        }]
    }
    out = flow_brief.interpret_tfms_flow(payload, origin="KJFK", dest="KEWR")
    assert len(out["advisories"]) == 1
    adv = out["advisories"][0]
    assert adv["severity"] == "WATCH"
    assert adv["source"] == "tfms-flow"
    assert adv["effective_start"] == "2026-09-12T12:00:00Z"
    assert adv["airport"] == "KEWR"
    assert out["effects"]
    assert out["effects"][0]["source"] == "tfms-flow"


def test_ground_stop_is_action():
    payload = {
        "results": [{
            "type": "tfms_advisory",
            "msg_type": "GADV",
            "title": "GROUND STOP FOR BOS",
            "text": "GROUND STOP AT BOS IN EFFECT",
        }]
    }
    out = flow_brief.interpret_tfms_flow(payload, origin="KBOS", dest="KATL")
    assert out["advisories"][0]["severity"] == "ACTION"


def test_mit_restriction_is_watch():
    payload = {
        "results": [{
            "type": "tfms_restriction",
            "msg_type": "RSTR",
            "element": "JFK",
            "element_type": "AIRPORT",
            "mit_value": "20",
            "avg_delay_minutes": "12",
            "start_time": "2026-09-12T14:00:00Z",
        }]
    }
    out = flow_brief.interpret_tfms_flow(payload, origin="KJFK", dest="KLAX")
    assert out["advisories"][0]["severity"] == "WATCH"
    assert "MIT" in out["advisories"][0]["text"]


def test_tmi_other_flight_is_dropped_when_callsign_given():
    payload = {
        "results": [{
            "type": "tfms_tmi_flight",
            "msg_type": "TMI_FLIGHT_LIST",
            "flight_id": "AAL100",
            "fca_ids": ["FCA001"],
            "arr_airport": "KJFK",
        }]
    }
    out = flow_brief.interpret_tfms_flow(payload, dest="KJFK", flight="DAL244")
    assert out["advisories"] == []


def test_tbfm_item_maps_fix_eta_status_and_this_flight():
    payload = {
        "results": [{
            "type": "tbfm_metering",
            "flight_id": "DAL244",
            "dest_airport": "KJFK",
            "dep_airport": "KATL",
            "msg_type": "UPDATE",
            "eta": {"sta": "2026-09-12T18:22:00Z", "cta": "2026-09-12T18:25:00Z"},
            "flight_info": {"mfx": "ROBER"},
        }]
    }
    out = flow_brief.interpret_tbfm(payload, flight="DAL244", dest="KJFK")
    assert out["applicable"] is True
    assert out["items"][0]["fix"] == "ROBER"
    assert out["items"][0]["eta"] == "2026-09-12T18:25:00Z"
    assert out["items"][0]["this_flight"] is True
    fx = flow_brief.metering_effects(out, "DAL244")
    assert fx and fx[0]["source"] == "tbfm"


def test_tfdm_record_picks_this_flight_and_queue():
    payload = {
        "results": [{
            "type": "tfdm_flight",
            "flight_id": "UAL100",
            "aerodrome": "KEWR",
            "queue_wait_minutes": 22,
            "taxi_out_minutes": 45,
            "runway_departure_earliest": "2026-09-12T15:40:00Z",
            "flight_state": "PUSHBACK",
        }]
    }
    out = flow_brief.interpret_tfdm(payload, airport="KEWR", flight="UAL100")
    assert out["applicable"] is True
    assert out["queue_wait_min"] == 22
    assert out["estimated_taxi_out_min"] == 45
    assert out["state"] == "PUSHBACK"
    fx = flow_brief.tfdm_effects(out)
    assert any(e["source"] == "tfdm" for e in fx)
    assert any(e["severity"] == "WATCH" for e in fx)


def test_error_payload_without_results_is_quiet():
    out = flow_brief.interpret_tfms_flow(
        {"error": "SWIM_PASSWORD not set"}, origin="KJFK")
    assert out["advisories"] == []


def test_merge_tfms_dedups():
    a = {"results": [{"type": "tfms_advisory", "msg_type": "GADV",
                      "advisory_number": "1", "title": "X",
                      "effective_start": "t"}]}
    b = {"results": [{"type": "tfms_advisory", "msg_type": "GADV",
                      "advisory_number": "1", "title": "X",
                      "effective_start": "t"}]}
    merged = flow_brief.merge_tfms_payloads(a, b)
    assert len(merged["results"]) == 1


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_flow_brief_endpoint_empty_swim_is_200():
    """Happy-path empty: mocked SWIM returns nothing, endpoint still 200."""
    empty = {"data": EMPTY_SWIM, "status": 200}

    def fake_parallel(tasks, max_workers=6, deadline=None):
        return {t["key"]: empty for t in tasks}

    with patch.object(app_module, "run_scripts_parallel", side_effect=fake_parallel):
        resp = _client().get(
            "/api/ops/flow-brief?flight=DL244&origin=KJFK&dest=EGLL")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["origin"] == "KJFK"
    assert body["dest"] == "EGLL"
    assert body["advisories"] == []
    assert body["effects"] == []
    assert isinstance(body["metering"], dict)
    assert isinstance(body["surface"], dict)
    assert "sources_tried" in body
    assert "sources_quiet" in body
    assert "timings" in body
    assert body["aeroapi_queries_used"] == 0


def test_flow_brief_requires_a_handle():
    resp = _client().get("/api/ops/flow-brief")
    assert resp.status_code == 400


def test_flow_brief_klhr_resolves_to_egll():
    empty = {"data": EMPTY_SWIM, "status": 200}

    def fake_parallel(tasks, max_workers=6, deadline=None):
        return {t["key"]: empty for t in tasks}

    with patch.object(app_module, "run_scripts_parallel", side_effect=fake_parallel):
        resp = _client().get("/api/ops/flow-brief?origin=JFK&dest=KLHR")
    assert resp.status_code == 200
    assert resp.get_json()["dest"] == "EGLL"
    assert resp.get_json()["origin"] == "KJFK"
