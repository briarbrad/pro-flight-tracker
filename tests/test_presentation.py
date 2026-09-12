"""Presentation layer — status / impactMinutes / causes / outlook.

No AeroAPI, no LLM. Run with: pytest tests/test_presentation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis  # noqa: E402

_SEV = {"ACTION": 0, "WATCH": 1, "INFO": 2}


def _assert_sev_order(items: list):
    ranks = [_SEV.get(c.get("severity"), 3) for c in items]
    assert ranks == sorted(ranks), items


def _edct_kwargs():
    return dict(
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
        }, {
            "cause": "Ground delay program at KJFK, avg delay 2h07m",
            "effect": "A GDP meters flights ARRIVING INTO the origin — "
                      "it does not assign delays to this departure.",
            "severity": "INFO",
            "source": "faa_status",
        }],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 16,
                               "status": "DERIVED"},
            "takeoff": {"delay_vs_schedule_min": 16, "status": "CONTROLLED"},
            "gate_arrival": {"delay_vs_schedule_min": -8,
                             "status": "ESTIMATED"},
            "edct": {"edct": "2026-08-16T23:41:00Z"},
        },
        taxi={"applicable": False},
        branch={"branch": "B", "branch_label": "Structural — cascades forward"},
        origin="KJFK",
        dest="KLAX",
    )


def test_delayed_with_edct():
    preso = analysis.build_presentation(
        **_edct_kwargs(), forecast_consulted=True)

    assert preso["status"]["code"] == "DELAYED"
    assert preso["status"]["label"] == "Delayed 16m"
    assert preso["status"]["phase"] == "PRE_GATE"
    assert preso["impactMinutes"] == 16
    assert preso["outlook"] == {"applicable": False}

    labels = [c["label"] for c in preso["causes"]]
    assert "FAA takeoff slot" in labels
    assert "Inbound plane tight" in labels
    assert any(c["label"] == "GDP at JFK" for c in preso["causes"])
    gdp = next(c for c in preso["causes"] if c["label"] == "GDP at JFK")
    assert gdp["severity"] == "INFO"
    assert gdp["source"] == "faa_status"
    assert "wheels-up" in gdp["why"]
    _assert_sev_order(preso["causes"])
    assert _SEV[preso["causes"][0]["severity"]] == 0  # ACTION first


def test_too_early_outlook_with_taf_thunderstorms():
    preso = analysis.build_presentation(
        phase={
            "phase": "PRE_GATE",
            "next_event": "gate_departure",
            "next_event_label": "Pushback",
            "next_event_local_display": "8:10 AM EDT",
        },
        horizon={"band": "DISTANT", "hours_to_departure": 36.0,
                 "hours_to_next_event": 36.0, "phase": "PRE_GATE"},
        verdict={"departure_risk": "MODERATE", "confidence": "LOW",
                 "drivers": ["Thunderstorms forecast at the departure airport"]},
        effects=[{
            "cause": "Thunderstorms forecast at the departure airport during "
                     "the departure window",
            "effect": "Convection is the most common trigger for ground "
                      "stops and ramp closures.",
            "severity": "ACTION",
            "source": "taf",
        }],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 0,
                               "status": "SCHEDULED"},
            "takeoff": {"delay_vs_schedule_min": 0, "status": "DERIVED"},
            "edct": None,
        },
        taxi={"applicable": False},
        branch={"branch": "NOT_APPLICABLE",
                "branch_label": "Too far out to classify"},
        origin="KJFK",
        dest="KLAX",
        taf_windows={
            "departure": {
                "available": True,
                "airport": "KJFK",
                "prevailing_category": "VFR",
                "significant_weather": ["thunderstorms"],
            }
        },
        forecast_consulted=True,
    )

    assert preso["status"]["code"] == "UNKNOWN"
    assert preso["status"]["label"] == "Too early to call"
    assert preso["impactMinutes"] is None
    # Forecast rows are not dumped as a live delay.
    assert all(c["source"] != "taf" for c in preso["causes"])

    out = preso["outlook"]
    assert out["applicable"] is True
    assert out["riskLevel"] == "MODERATE"
    assert out["confidence"] == "LOW"  # DISTANT is always LOW
    assert "thunderstorm" in out["headline"].lower()
    assert "Elevated delay risk" in out["headline"]
    assert any("thunderstorm" in c["label"].lower() for c in out["causes"])
    assert all(c["source"] == "taf" for c in out["causes"]
               if "thunderstorm" in c["label"].lower())
    _assert_sev_order(out["causes"])


def test_cancelled():
    preso = analysis.build_presentation(
        phase={"phase": "CANCELLED", "is_terminal": True,
               "next_event": None, "next_event_label": None},
        horizon={"band": "CANCELLED", "phase": "CANCELLED"},
        verdict={"departure_risk": "HIGH", "confidence": "HIGH",
                 "drivers": ["Flight is cancelled."]},
        effects=[],
        predicted_times={"edct": None},
        taxi={"applicable": False},
        branch={"branch": "UNDETERMINED"},
        origin="KJFK",
        dest="KLAX",
        forecast_consulted=True,
    )
    assert preso["status"]["code"] == "CANCELLED"
    assert preso["status"]["label"] == "Cancelled"
    assert preso["status"]["phase"] == "CANCELLED"
    assert preso["impactMinutes"] is None
    assert preso["outlook"] == {"applicable": False}


def test_clear_long_horizon_low_risk():
    preso = analysis.build_presentation(
        phase={
            "phase": "PRE_GATE",
            "next_event": "gate_departure",
            "next_event_label": "Pushback",
        },
        horizon={"band": "DISTANT", "hours_to_departure": 36.0,
                 "hours_to_next_event": 36.0, "phase": "PRE_GATE"},
        verdict={"departure_risk": "LOW", "confidence": "LOW",
                 "drivers": ["No delay mechanism identified"]},
        effects=[{
            "cause": "Forecast at the departure airport is VFR through the "
                     "departure window",
            "effect": "Ceiling and visibility are not expected to constrain "
                      "operations.",
            "severity": "INFO",
            "source": "taf",
        }],
        predicted_times={
            "gate_departure": {"delay_vs_schedule_min": 0,
                               "status": "SCHEDULED"},
            "takeoff": {"delay_vs_schedule_min": 0, "status": "SCHEDULED"},
            "edct": None,
        },
        taxi={"applicable": False},
        branch={"branch": "NOT_APPLICABLE"},
        origin="KJFK",
        dest="EGLL",
        taf_windows={
            "departure": {
                "available": True,
                "airport": "KJFK",
                "prevailing_category": "VFR",
                "significant_weather": [],
            }
        },
        forecast_consulted=True,
    )
    assert preso["status"]["code"] == "UNKNOWN"
    assert "on time" not in preso["status"]["label"].lower()
    assert preso["impactMinutes"] is None
    out = preso["outlook"]
    assert out["applicable"] is True
    assert out["riskLevel"] == "LOW"
    assert out["confidence"] == "LOW"
    assert "Low delay risk" in out["headline"]
    assert out["causes"]
    assert all(c["severity"] != "ACTION" for c in out["causes"])


def test_live_tile_never_emits_applicable_outlook():
    """Same far-out inputs, but live did not consult forecast sources."""
    preso = analysis.build_presentation(
        phase={"phase": "PRE_GATE"},
        horizon={"band": "DISTANT", "hours_to_departure": 36.0,
                 "phase": "PRE_GATE"},
        verdict={"departure_risk": "LOW", "confidence": "LOW"},
        effects=[],
        predicted_times={"takeoff": {"delay_vs_schedule_min": 0}},
        branch={"branch": "NOT_APPLICABLE"},
        origin="KJFK",
        forecast_consulted=False,
    )
    assert preso["outlook"] == {"applicable": False}
    assert preso["status"]["code"] == "UNKNOWN"


def test_taxiing_outlook_not_applicable():
    preso = analysis.build_presentation(
        phase={"phase": "TAXI_OUT"},
        horizon={"band": "IMMINENT", "hours_to_next_event": 0.4,
                 "phase": "TAXI_OUT"},
        verdict={"departure_risk": "MODERATE", "confidence": "HIGH"},
        effects=[{
            "cause": "100 min into taxi-out at KJFK against a typical 30 min",
            "effect": "The aircraft is out of the gate and in the departure queue.",
            "severity": "ACTION",
            "source": "taxi",
        }],
        predicted_times={"takeoff": {"delay_vs_schedule_min": 40,
                                     "status": "ESTIMATED"}},
        taxi={"applicable": True, "assessment": "EXTENDED",
              "phase": "TAXI_OUT", "excess_vs_typical_min": 70},
        branch={"branch": "UNDETERMINED"},
        origin="KJFK",
        forecast_consulted=True,
    )
    assert preso["outlook"] == {"applicable": False}
    assert preso["status"]["code"] == "DELAYED"
    assert preso["impactMinutes"] in (40, 70)
    assert any(c["source"] == "taxi" for c in preso["causes"])


def test_outlook_from_extended_weather_precip():
    preso = analysis.build_presentation(
        phase={"phase": "PRE_GATE"},
        horizon={"band": "NEXT_DAY", "hours_to_departure": 18.0,
                 "phase": "PRE_GATE"},
        verdict={"departure_risk": "LOW", "confidence": "LOW"},
        effects=[],
        predicted_times={"takeoff": {"delay_vs_schedule_min": 0}},
        branch={"branch": "NOT_APPLICABLE"},
        origin="KJFK",
        dest="KLAX",
        taf_windows={"departure": {"available": True,
                                   "prevailing_category": "VFR"}},
        extended_weather={
            "label": "model_guidance",
            "source": "open-meteo",
            "airports": {
                "KJFK": {
                    "icao": "KJFK",
                    "next_6h": {
                        "max_precip_probability_pct": 80,
                        "max_wind_gust_kts": 18.0,
                        "min_visibility_m": 16000,
                    },
                }
            },
        },
        forecast_consulted=True,
    )
    out = preso["outlook"]
    assert out["applicable"] is True
    assert out["riskLevel"] == "MODERATE"
    assert out["confidence"] == "MEDIUM"  # NEXT_DAY + covering TAF
    assert any(c["source"] == "extended_weather" for c in out["causes"])
    assert any("rain" in c["label"].lower() for c in out["causes"])


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
