"""CV measurement JSON → draft plan."""
import pytest

from organizer import PlanError, parse_plan, plan_from_cv

CV = {
    "game": "Example", "units": "mm",
    "components": [
        {"index": 1, "name": "Game box", "quantity": 1, "shape": "rectangular_prism",
         "dimensions_mm": {"length": 300, "width": 300, "height": 75}, "confidence": "high"},
        {"index": 2, "name": "Rulebook", "quantity": 1, "shape": "rectangular_prism",
         "dimensions_mm": {"length": 280, "width": 280, "height": 3}, "confidence": "medium"},
        {"index": 3, "name": "Resource card", "quantity": 95, "shape": "rectangular_prism",
         "dimensions_mm": {"length": 88, "width": 63}, "confidence": "high",
         "notes": "Height/thickness not measured"},
        {"index": 4, "name": "Gold coin", "quantity": 30, "shape": "cylinder",
         "dimensions_mm": {"diameter": 22, "height": 2}, "confidence": "high"},
        {"index": 5, "name": "Settlement", "quantity": 20, "shape": "rectangular_prism",
         "dimensions_mm": {"length": 16, "width": 12, "height": 14}, "confidence": "low"},
        {"index": 6, "name": "Six-sided die", "quantity": 2, "shape": "cube",
         "dimensions_mm": {"length": 16, "width": 16, "height": 16}, "confidence": "high"},
        {"index": 7, "name": "Robber", "quantity": None, "shape": "cylinder", "dimensions_mm": None,
         "confidence": "none"},
    ],
}


def test_types_and_fields_are_mapped():
    plan = plan_from_cv(CV)
    comps = {c["name"]: c for c in plan["compartments"]}
    assert "Game box" not in comps and "Rulebook" not in comps
    assert comps["Resource card"]["type"] == "cards"
    assert "thickness" not in comps["Resource card"]["piece_mm"]       # not invented
    assert comps["Gold coin"]["type"] == "round_tokens"
    assert comps["Gold coin"]["piece_mm"] == {"diameter": 22, "thickness": 2}
    assert comps["Settlement"]["type"] == "bits"
    assert comps["Six-sided die"]["type"] == "dice" and comps["Six-sided die"]["piece_mm"] == {"edge": 16}
    assert any("INSIDE" in n for n in plan["cv_notes"])
    assert any("headroom" in n for n in plan["cv_notes"])


def test_draft_reports_every_gap():
    plan = plan_from_cv(CV)
    with pytest.raises(PlanError) as e:
        parse_plan(plan)
    missing = {(i.component, i.field) for i in e.value.issues.missing_measurements}
    assert (None, "box_inner_mm.width") in missing
    assert ("Resource card", "piece_mm.thickness") in missing
    assert ("Robber", "quantity") in missing


def test_complete_draft_generates_plan():
    cv = {"units": "mm", "components": [c for c in CV["components"] if c["index"] in (4, 5, 6)]}
    plan = plan_from_cv(cv, box={"width": 290, "length": 290, "height": 70},
                        bed={"width": 220, "length": 220}, settings={"headroom_mm": 4})
    parsed = parse_plan(plan)
    assert len(parsed.compartments) == 3


def test_relative_units_are_rejected():
    plan = plan_from_cv({"units": "relative (reference dimension = 1.0)", "components": CV["components"][3:4]})
    assert "piece_mm" not in plan["compartments"][0]
    assert any("relative units" in n for n in plan["cv_notes"])
