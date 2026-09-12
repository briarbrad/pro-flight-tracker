"""Deterministic Simple-mode summary — no AeroAPI, no LLM.

Run with: pytest tests/test_simple_summary.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis  # noqa: E402

_JARGON = (
    "EDCT", "GDP", "CTOT", "TAF", "ATFM", "Branch A", "Branch B",
    "NOT_APPLICABLE", "wheels-up", "TFMS", "TBFM",
)


def _assert_no_jargon(summary: dict):
    blob = " ".join([
        summary.get("headline") or "",
        summary.get("what_i_think") or "",
        " ".join(summary.get("basis_bullets") or []),
    ])
    for word in _JARGON:
        assert word not in blob, f"jargon leaked: {word!r} in {blob!r}"


def _keys(summary: dict):
    assert set(summary) == {
        "headline", "what_i_think", "confidence", "risk",
        "next_event_label", "next_event_local_display", "basis_bullets",
    }
    assert isinstance(summary["basis_bullets"], list)
    assert summary["basis_bullets"]
    _assert_no_jargon(summary)


def test_edct_delay_predicts_late_leave_on_time_ish_arrival():
    summary = analysis.build_simple_summary(
        phase={
            "phase": "PRE_GATE",
            "next_event": "gate_departure",
            "next_event_label": "Pushback",
            "next_event_local_display": "7:21 PM EDT",
        },
        horizon={"band": "IMMINENT", "hours_to_departure": 1.1,
                 "hours_to_next_event": 1.1, "phase": "PRE_GATE"},
        verdict={"departure_risk": "LOW", "confidence": "HIGH",
                 "drivers": ["No delay mechanism identified"]},
        effects=[{
            "cause": "FAA traffic management has assigned this flight an "
                     "EDCT of 2026-08-16T23:41:00Z",
            "effect": "That is the controlled wheels-up time.",
            "severity": "ACTION",
            "source": "swim_tfms",
        }, {
            "cause": "Inbound aircraft leaves only 28 min of turn time "
                     "(narrow-body minimum: 35 min).",
            "effect": "Departure is effectively guaranteed to slip.",
            "severity": "ACTION",
            "source": "equipment_chain",
        }],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 16,
                               "status": "DERIVED",
                               "local_display": "7:21 PM EDT"},
            "takeoff": {"delay_vs_schedule_min": 16, "status": "CONTROLLED",
                        "local_display": "7:41 PM EDT"},
            "gate_arrival": {"delay_vs_schedule_min": -8,
                             "status": "ESTIMATED"},
            "uncertainty_minutes": 10,
            "edct": {"edct": "2026-08-16T23:41:00Z"},
        },
        taxi={"applicable": False},
        branch={"branch": "B", "branch_label": "Structural — cascades forward"},
        origin="KJFK",
        dest="KLAX",
    )
    _keys(summary)
    assert "15–25 min late leaving JFK" in summary["headline"]
    assert "on-time-ish arrival" in summary["headline"]
    assert "FAA takeoff slot" in summary["what_i_think"]
    assert "inbound" in summary["what_i_think"].lower()
    assert summary["confidence"] == "HIGH"
    assert summary["risk"] == "MODERATE"  # lifted from LOW by ACTION effects
    assert summary["next_event_label"] == "Pushback"
    assert summary["next_event_local_display"] == "7:21 PM EDT"
    assert "FAA takeoff slot assigned" in summary["basis_bullets"]
    assert "Inbound plane running tight" in summary["basis_bullets"]


def test_too_early_refuses_fake_green_certainty():
    summary = analysis.build_simple_summary(
        phase={
            "phase": "PRE_GATE",
            "next_event": "gate_departure",
            "next_event_label": "Pushback",
            "next_event_local_display": "8:10 AM EDT",
        },
        horizon={"band": "DISTANT", "hours_to_departure": 36.0,
                 "hours_to_next_event": 36.0, "phase": "PRE_GATE"},
        verdict={"departure_risk": "LOW", "confidence": "LOW",
                 "drivers": ["No delay mechanism identified"]},
        effects=[],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 0,
                               "status": "SCHEDULED"},
            "takeoff": {"delay_vs_schedule_min": 0, "status": "DERIVED"},
            "gate_arrival": {"delay_vs_schedule_min": 0,
                             "status": "SCHEDULED"},
            "edct": None,
        },
        taxi={"applicable": False},
        branch={"branch": "NOT_APPLICABLE",
                "branch_label": "Too far out to classify"},
        origin="KJFK",
        dest="EGLL",
    )
    _keys(summary)
    assert summary["headline"].startswith("Too early for a firm call")
    assert "nothing worrying yet" in summary["headline"]
    assert "Looking on time" not in summary["headline"]
    assert "on schedule" not in summary["headline"].lower()
    assert summary["confidence"] == "LOW"
    assert summary["risk"] == "LOW"
    assert "Too early to judge" in summary["basis_bullets"]
    assert "Nothing worrying yet" in summary["basis_bullets"]


def test_next_day_not_applicable_is_also_too_early():
    summary = analysis.build_simple_summary(
        phase={"phase": "PRE_GATE", "next_event_label": "Pushback",
               "next_event_local_display": "6:00 AM EDT"},
        horizon={"band": "NEXT_DAY", "hours_to_departure": 18.0},
        verdict={"departure_risk": "LOW", "confidence": "LOW"},
        effects=[],
        predicted_times={"takeoff": {"delay_vs_schedule_min": 0}},
        branch={"branch": "NOT_APPLICABLE"},
        origin="KATL",
    )
    assert "Too early for a firm call" in summary["headline"]
    assert summary["confidence"] == "LOW"


def test_cancelled_is_past_tense_closure():
    summary = analysis.build_simple_summary(
        phase={"phase": "CANCELLED", "is_terminal": True,
               "next_event": None, "next_event_label": None,
               "next_event_local_display": None},
        horizon={"band": "CANCELLED", "phase": "CANCELLED"},
        verdict={"departure_risk": "HIGH", "confidence": "HIGH",
                 "drivers": ["Flight is cancelled."]},
        effects=[],
        predicted_times={"edct": None},
        taxi={"applicable": False},
        branch={"branch": "UNDETERMINED"},
        origin="KJFK",
        dest="KLAX",
    )
    _keys(summary)
    assert "cancelled" in summary["headline"].lower()
    assert summary["headline"].startswith("This flight has been cancelled")
    assert "rebooking" in summary["what_i_think"].lower()
    assert summary["risk"] == "HIGH"
    assert summary["confidence"] == "HIGH"
    assert summary["next_event_label"] is None
    assert "Flight cancelled" in summary["basis_bullets"]


def test_arrived_on_time_is_past_tense_closure():
    summary = analysis.build_simple_summary(
        phase={"phase": "ARRIVED", "is_terminal": True,
               "next_event": None, "next_event_label": None},
        horizon={"band": "ARRIVED", "phase": "ARRIVED"},
        verdict={"departure_risk": "LOW", "confidence": "HIGH"},
        effects=[],
        predicted_times={
            "gate_arrival": {"delay_vs_schedule_min": 3, "status": "ACTUAL"},
        },
        branch={"branch": "UNDETERMINED"},
        origin="KJFK",
        dest="KLAX",
    )
    _keys(summary)
    assert "arrived" in summary["headline"].lower()
    assert "on time" in summary["headline"].lower()
    assert "has arrived" in summary["headline"]
    assert "gate" in summary["what_i_think"].lower()
    assert "Flight arrived" in summary["basis_bullets"] or \
        "Arrived on time" in summary["basis_bullets"]


def test_clear_low_risk_imminent_is_on_time():
    summary = analysis.build_simple_summary(
        phase={
            "phase": "PRE_GATE",
            "next_event": "gate_departure",
            "next_event_label": "Pushback",
            "next_event_local_display": "7:41 PM EDT",
        },
        horizon={"band": "IMMINENT", "hours_to_departure": 0.8,
                 "hours_to_next_event": 0.8},
        verdict={"departure_risk": "LOW", "confidence": "HIGH",
                 "drivers": ["No delay mechanism identified"]},
        effects=[{
            "cause": "Turn time 55 min vs 40 min standard for a narrow-body",
            "effect": "Equipment is not a constraint.",
            "severity": "INFO",
            "source": "equipment_chain",
        }],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 2,
                               "status": "ESTIMATED"},
            "takeoff": {"delay_vs_schedule_min": 2, "status": "ESTIMATED",
                        "local_display": "8:01 PM EDT"},
            "gate_arrival": {"delay_vs_schedule_min": 0,
                             "status": "ESTIMATED"},
            "edct": None,
        },
        taxi={"applicable": False},
        branch={"branch": "UNDETERMINED"},
        origin="KJFK",
        dest="KLAX",
    )
    _keys(summary)
    assert "Looking on time" in summary["headline"]
    assert "7:41 PM EDT" in summary["headline"]
    assert "Too early" not in summary["headline"]
    assert summary["risk"] == "LOW"
    assert summary["confidence"] == "HIGH"
    assert "Nothing worrying yet" in summary["basis_bullets"] or \
        "On schedule so far" in summary["basis_bullets"]


def test_traveler_airport_drops_k_and_maps_lhr():
    assert analysis.traveler_airport("KJFK") == "JFK"
    assert analysis.traveler_airport("EGLL") == "LHR"
    assert analysis.traveler_airport("JFK") == "JFK"
    assert analysis.traveler_airport("") == ""


def test_lateness_span_rounds_to_traveler_window():
    assert analysis._lateness_span(16) == "15–25 min"
    assert analysis._lateness_span(20) == "20–30 min"
    assert analysis._lateness_span(3) is None
    assert analysis._lateness_span(9) == "a few minutes"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
