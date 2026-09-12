"""G-AIRMET and ATFM now contribute effects[] when consulted.

Run with: pytest tests/test_gairmet_atfm_effects.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import analysis  # noqa: E402
import airport_ops  # noqa: E402


def test_gairmet_empty_relevant_emits_nothing():
    assert analysis.gairmet_effects({"relevant": [], "risk_level": "NONE"}) == []
    assert analysis.gairmet_effects(None) == []
    assert analysis.gairmet_effects({"error": "timeout"}) == []


def test_gairmet_mod_turb_is_info():
    fx = analysis.gairmet_effects({
        "relevant": [{
            "hazard": "TURB-HI",
            "severity": "MOD",
            "near_origin": True,
            "near_dest": False,
            "along_route": False,
            "base": "180",
            "top": "380",
            "base_ft": 18000,
            "top_ft": 38000,
        }]
    })
    assert len(fx) == 1
    assert fx[0]["source"] == "gairmet"
    assert fx[0]["severity"] == "INFO"
    assert "TURB-HI" in fx[0]["cause"]
    assert "near origin" in fx[0]["cause"]


def test_gairmet_sev_turb_is_watch_not_action():
    fx = analysis.gairmet_effects({
        "relevant": [{
            "hazard": "TURB-LO",
            "severity": "SEV",
            "along_route": True,
        }]
    })
    assert fx[0]["severity"] == "WATCH"
    assert fx[0]["source"] == "gairmet"
    assert "reroute" in fx[0]["effect"].lower()


def test_atfm_not_applicable_emits_nothing():
    assert analysis.atfm_effects({"applicable": False}) == []
    assert analysis.atfm_effects({
        "applicable": True, "verdict": "NO_INDICATION",
    }) == []


def test_atfm_probable_is_watch():
    fx = analysis.atfm_effects({
        "applicable": True,
        "destination": "EGLL",
        "verdict": "PROBABLE",
        "confidence_pct": 70,
        "delay_min": 30,
    })
    assert fx[0]["source"] == "atfm"
    assert fx[0]["severity"] == "WATCH"
    assert "EGLL" in fx[0]["cause"]
    assert "CTOT" in fx[0]["effect"]


def test_infer_atfm_from_flat_status_no_aeroapi():
    """Brief/live shape: dest_icao at the top level, not destination.code_icao."""
    flight = {
        "dest_icao": "EGLL",
        "origin_icao": "KJFK",
        "scheduled_out": "2026-09-12T10:00:00Z",
        "estimated_out": "2026-09-12T10:30:00Z",
        "scheduled_in": "2026-09-12T17:05:00Z",
        "status": "Delayed",
    }
    atfm = airport_ops.infer_atfm_from_status(flight)
    assert atfm["applicable"] is True
    assert atfm["destination"] == "EGLL"
    assert atfm["verdict"] in ("POSSIBLE", "PROBABLE")
    fx = analysis.atfm_effects(atfm)
    assert fx and fx[0]["source"] == "atfm"


def test_infer_atfm_us_dest_not_applicable():
    atfm = airport_ops.infer_atfm_from_status({
        "dest_icao": "KLAX",
        "scheduled_out": "2026-09-12T10:00:00Z",
        "estimated_out": "2026-09-12T10:30:00Z",
    })
    assert atfm["applicable"] is False
    assert analysis.atfm_effects(atfm) == []


def test_source_plan_includes_atfm_inside_12h():
    plan = analysis.source_plan(4.0, {"phase": "PRE_GATE", "is_terminal": False})
    assert "atfm" in plan
    assert plan["atfm"]["relevant"] is True


def test_source_plan_excludes_atfm_when_distant():
    plan = analysis.source_plan(20.0, {"phase": "PRE_GATE", "is_terminal": False})
    assert plan["atfm"]["relevant"] is False
