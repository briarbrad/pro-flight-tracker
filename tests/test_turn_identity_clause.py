"""Narrative guardrail against fabricated equipment/turn-time specifics.

Background: the analyst narrative for a real Branch-B (equipment) delay
cited "landed at 9:14 PM EDT", a scheduled departure of "10:05 PM EDT
(22:05 UTC)", and tail number "C-GGOF" -- none of which existed anywhere in
the deterministic facts payload sent to the LLM (facts only ever carried
relative minute deltas, e.g. "Turn time 40 min is below the 45 min
minimum"). That's a direct violation of the very first SYNTHESIS_RULES
entry: "Every number in your answer must come from the facts provided."

The fix has two parts, both covered here:
  1. An explicit SYNTHESIS_RULES entry telling the model it may only cite
     turn-time specifics that are actually present in the facts, and must
     describe the mechanism in relative terms when they are not.
  2. _turn_identity_clause() grounds real specifics (inbound flight ident,
     registration, and *_local clock times) into classify_branch's evidence
     and build_effects' cause text WHEN AVAILABLE, so the model has real
     data to cite instead of an empty vacuum it was filling by fabricating.

Run with: pytest tests/test_turn_identity_clause.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis  # noqa: E402


# ---------------------------------------------------------------------------
# SYNTHESIS_RULES guardrail text
# ---------------------------------------------------------------------------

def test_synthesis_rules_forbid_inventing_turn_time_specifics():
    rules_text = " ".join(analysis.SYNTHESIS_RULES).lower()
    assert "clock time" in rules_text
    assert "tail number" in rules_text
    assert "branch b" in rules_text


# ---------------------------------------------------------------------------
# _turn_identity_clause
# ---------------------------------------------------------------------------

def test_clause_empty_when_no_identifying_fields_present():
    """No ident and no local clock time means nothing grounded to cite --
    the clause must be empty, not fabricate a placeholder."""
    assert analysis._turn_identity_clause({}) == ""
    assert analysis._turn_identity_clause(
        {"turn_time_available_min": 40.0}) == ""
    assert analysis._turn_identity_clause(None) == ""


def test_clause_uses_only_fields_actually_present():
    """With ident + registration + both local times supplied, the clause
    must name exactly those values and nothing invented."""
    turn_analysis = {
        "turn_time_available_min": 40.0,
        "inbound_ident": "JZA8550",
        "inbound_registration": "C-GGOF",
        "inbound_eta_local": "9:14 PM EDT",
        "outbound_scheduled_departure_local": "10:05 PM EDT",
    }
    clause = analysis._turn_identity_clause(turn_analysis)
    assert "JZA8550" in clause
    assert "C-GGOF" in clause
    assert "9:14 PM EDT" in clause
    assert "10:05 PM EDT" in clause


def test_clause_degrades_gracefully_with_partial_fields():
    """An ident with no local time yet (cache written before enrichment, or
    a genuinely unresolved timezone) must still produce a valid clause using
    only what's there -- never insert a placeholder clock time."""
    clause = analysis._turn_identity_clause({"inbound_ident": "JZA8550"})
    assert "JZA8550" in clause
    assert "PM" not in clause and "AM" not in clause


def test_classify_branch_evidence_includes_grounded_clause_for_branch_b():
    turn_analysis = {
        "turn_time_available_min": 40.0,
        "turn_time_required_min_minimum": 45,
        "aircraft_category": "regional",
        "inbound_ident": "JZA8550",
        "inbound_eta_local": "9:14 PM EDT",
    }
    branch = analysis.classify_branch(
        {"hours_to_departure": 1.0}, [], turn_analysis, {}, [])
    evidence_text = " ".join(branch.get("evidence", []))
    assert "JZA8550" in evidence_text
    assert "9:14 PM EDT" in evidence_text


def test_build_effects_cause_includes_grounded_clause_for_deficit():
    turn_analysis = {
        "turn_time_available_min": 40.0,
        "turn_time_required_min_minimum": 45,
        "turn_time_required_min_standard": 60,
        "aircraft_category": "regional",
        "inbound_ident": "JZA8550",
        "inbound_registration": "C-GGOF",
    }
    effects = analysis.build_effects(
        {}, [], turn_analysis, {}, {"hours_to_departure": 1.0})
    equipment_effects = [e for e in effects if e.get("source") == "equipment_chain"]
    assert equipment_effects
    cause = equipment_effects[0]["cause"]
    assert "JZA8550" in cause
    assert "C-GGOF" in cause


def test_build_effects_no_clause_appended_when_equipment_not_constraint():
    """The identity clause is only appended to the ACTION (deficit) branch --
    an adequate turn should read as plain, uncluttered INFO text."""
    turn_analysis = {
        "turn_time_available_min": 90.0,
        "turn_time_required_min_minimum": 45,
        "turn_time_required_min_standard": 60,
        "aircraft_category": "regional",
        "inbound_ident": "JZA8550",
    }
    effects = analysis.build_effects(
        {}, [], turn_analysis, {}, {"hours_to_departure": 1.0})
    equipment_effects = [e for e in effects if e.get("source") == "equipment_chain"]
    assert equipment_effects
    assert equipment_effects[0]["severity"] == "INFO"
    assert "JZA8550" not in equipment_effects[0]["cause"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
