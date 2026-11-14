"""Plan parsing, validation and per-type cavity sizing (no CAD needed)."""
import pytest

from conftest import minimal_plan
from organizer import PlanError, parse_plan


def test_legacy_plan_still_parses(legacy_plan):
    plan = parse_plan(legacy_plan)
    assert [c.name for c in plan.compartments][:2] == ["Playing cards", "Square tiles"]
    # legacy cavity math is unchanged: required + 2·clearance, depth = height + clearance
    cards = plan.compartments[0].cells[0]
    assert (cards.w, cards.l, cards.depth) == pytest.approx((68.0, 94.0, 27.0))
    # name-based type inference only changes features
    assert plan.compartments[0].type == "cards" and cards.notch == "deep"
    assert plan.compartments[2].type == "bits"


def test_all_errors_are_reported_at_once():
    plan = minimal_plan({"name": "A", "required_mm": {"width": -1, "length": 10, "height": 5}},
                        {"name": "A", "required_mm": {"width": 1, "length": 10, "height": 5}},
                        {"name": "B", "type": "spaceship", "required_mm": {"width": 1, "length": 1, "height": 1}})
    with pytest.raises(PlanError) as e:
        parse_plan(plan)
    codes = {i.code for i in e.value.issues.errors}
    assert {"invalid_value", "duplicate_name"} <= codes
    assert len(e.value.issues.errors) >= 3


def test_missing_measurements_are_flagged_not_invented():
    plan = minimal_plan({"name": "Deck", "type": "cards", "quantity": 50, "piece_mm": {"width": 63, "length": 88}},
                        {"name": "Coins", "type": "round_tokens", "piece_mm": {"diameter": 20}})
    with pytest.raises(PlanError) as e:
        parse_plan(plan)
    missing = {(i.component, i.field) for i in e.value.issues.missing_measurements}
    assert ("Deck", "piece_mm.thickness") in missing
    assert ("Coins", "piece_mm.thickness") in missing
    assert ("Coins", "quantity") in missing
    assert e.value.report()["status"] == "failed"


def test_missing_box_and_bed_are_flagged():
    with pytest.raises(PlanError) as e:
        parse_plan({"compartments": [{"name": "x", "required_mm": {"width": 1, "length": 1, "height": 1}}]})
    fields = {i.field for i in e.value.issues.missing_measurements}
    assert {"box_inner_mm.width", "box_inner_mm.height", "printer_bed_mm.width"} <= fields


def test_sleeved_cards_need_sleeve_measurements():
    plan = minimal_plan({"name": "Deck", "type": "cards", "quantity": 10, "sleeved": True,
                         "piece_mm": {"width": 63, "length": 88, "thickness": 0.3}})
    with pytest.raises(PlanError) as e:
        parse_plan(plan)
    assert any(i.field == "sleeve_mm.thickness" for i in e.value.issues.missing_measurements)


def test_card_stack_math_and_sleeves():
    plan = parse_plan(minimal_plan(
        {"name": "Bare", "type": "cards", "quantity": 60, "piece_mm": {"width": 63, "length": 88, "thickness": 0.3}},
        {"name": "Sleeved", "type": "cards", "quantity": 60, "piece_mm": {"width": 63, "length": 88, "thickness": 0.3},
         "sleeve_mm": {"width": 66, "length": 91, "thickness": 0.6}}))
    bare, sleeved = plan.compartments[0].cells[0], plan.compartments[1].cells[0]
    assert (bare.w, bare.l, bare.depth) == pytest.approx((65, 90, 18 + 1))
    assert (sleeved.w, sleeved.l, sleeved.depth) == pytest.approx((68, 93, 36 + 1))


def test_tall_stack_is_reported_not_split():
    tiles = {"name": "Tiles", "type": "tiles", "quantity": 60, "piece_mm": {"width": 40, "length": 40, "thickness": 2}}
    with pytest.raises(PlanError) as e:  # 120 mm of tiles, 47 mm of room: no automatic split/dividers
        parse_plan(minimal_plan(tiles, box=(200, 200, 50)))
    err = e.value.issues.errors[0]
    assert err.code == "too_tall" and '"stacks": 3' in err.message
    plan = parse_plan(minimal_plan({**tiles, "stacks": 3}, box=(200, 200, 50)))  # explicit request
    cells = plan.compartments[0].cells
    assert len(cells) == 3 and all(c.depth <= 50 - 2 for c in cells)


def test_round_token_wells():
    plan = parse_plan(minimal_plan({"name": "Coins", "type": "round_tokens", "quantity": 40,
                                    "piece_mm": {"diameter": 25, "thickness": 2.5}}, box=(200, 200, 40)))
    cell = plan.compartments[0].cells[0]
    assert cell.kind == "wells"
    per_stack = cell.contents["per_stack"]
    assert per_stack * 2.5 + 1 <= 40 - 2
    assert len(cell.wells) == cell.contents["stacks"]
    assert cell.well_r == pytest.approx(12.5 + 1)


def test_dice_pockets_and_bits_assumption():
    plan = parse_plan(minimal_plan(
        {"name": "Dice", "type": "dice", "quantity": 4, "pockets": True, "piece_mm": {"edge": 16}},
        {"name": "Cubes", "type": "bits", "quantity": 30, "piece_mm": {"width": 8, "length": 8, "height": 8}}))
    assert len(plan.compartments[0].cells) == 4
    assert any("packing efficiency" in a for a in plan.assumptions)


def test_too_tall_component_fails():
    with pytest.raises(PlanError) as e:
        parse_plan(minimal_plan({"name": "Tower", "required_mm": {"width": 30, "length": 30, "height": 80}}))
    assert any(i.code == "too_tall" for i in e.value.issues.errors)


def test_allow_partial_skips_bad_compartments():
    plan = minimal_plan({"name": "Good", "required_mm": {"width": 30, "length": 30, "height": 10}},
                        {"name": "Bad", "type": "cards", "piece_mm": {"width": 63, "length": 88}})
    with pytest.raises(PlanError):
        parse_plan(plan)
    p = parse_plan(plan, allow_partial=True)
    assert [c.name for c in p.compartments] == ["Good"] and p.skipped == ["Bad"]


def test_settings_validation():
    with pytest.raises(PlanError, match="at least 1.2"):
        parse_plan(minimal_plan({"name": "x", "required_mm": {"width": 1, "length": 1, "height": 1}}, wall_mm=0.8))
    with pytest.raises(PlanError, match="divider_mm"):
        parse_plan(minimal_plan({"name": "x", "required_mm": {"width": 1, "length": 1, "height": 1}},
                                divider_mm=0.8))
