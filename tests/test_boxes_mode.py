"""organizer "boxes": Holden's grouping JSON → one plain open box per set, resting on the board."""
import copy
import importlib.util
import json

import numpy as np
import pytest

from conftest import HAS_CADQUERY, ROOT, needs_cad
from organizer import PlanError, plan_layout
from organizer.grouping import place_cells
from organizer.mesh import read_stl, single_open_cavity

spec = importlib.util.spec_from_file_location("convert_grouping_output", ROOT / "convert_grouping_output.py")
conv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conv)

BED = (256, 256, 256)  # illustrative printer volume, same as HOW_TO_TEST_HOLDEN_DATA.md
SETS = ["Red Player Kit", "Blue Player Kit", "Shared Supply: Dice", "Shared Supply: Battle Tokens"]


@pytest.fixture
def grouping():
    return json.loads((ROOT / "examples/holden_boxes_grouping.json").read_text(encoding="utf-8"))


@pytest.fixture
def boxes_plan(grouping):
    plan, _ = conv.convert(grouping, BED)
    return plan


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------
def test_converter_preserves_sets(grouping, boxes_plan):
    assert boxes_plan["organizer"] == "boxes"
    assert [c["name"] for c in boxes_plan["compartments"]] == SETS
    for comp, src in zip(boxes_plan["compartments"], grouping["sets"]):
        assert comp["type"] == "set" and comp["storage_style"] == src["storage_style"]
        assert "tray" not in comp and "pockets" not in comp and "stacks" not in comp
        for m, sm in zip(comp["members"], src["members"]):
            assert (m["name"], m["quantity"], m["shape"]) == (sm["name"], sm["quantity"], sm["shape"])
            assert m["dimensions_mm"] == sm["dimensions_mm"]
    assert boxes_plan["compartments"][3]["storage_style"] == "stack"


def test_board_is_bottom_layer_not_headroom(boxes_plan):
    assert boxes_plan["settings"]["headroom_mm"] == 0
    (board,) = boxes_plan["bottom_layer"]
    assert board["name"] == "Folded game board"
    assert board["dimensions_mm"] == {"length": 245.0, "width": 245.0, "height": 8.0}
    assert any("not verified" in n or "physical_fit_verified" in n for n in boxes_plan["conversion_notes"])


def test_converter_reports_every_problem(grouping):
    bad = copy.deepcopy(grouping)
    bad["sets"][0]["storage_style"] = "sleeved"
    bad["sets"][1]["members"][0]["dimensions_mm"].pop("height")
    bad["sets"][2]["members"][0]["quantity"] = 0
    with pytest.raises(conv.ConversionError) as e:
        conv.convert(bad, BED)
    text = " ".join(e.value.problems)
    assert "sleeved" in text and "height" in text and "quantity" in text
    bad["organizer"] = "drawers"
    with pytest.raises(conv.ConversionError, match="not supported"):
        conv.convert(bad, BED)


def test_ambiguous_excluded_item_is_reported(grouping):
    g = copy.deepcopy(grouping)
    g["excluded"][0]["reason"] = "Not stored in a compartment."
    plan, notes = conv.convert(g, BED)
    assert any(n.startswith("AMBIGUOUS") for n in notes)
    assert plan["bottom_layer"]  # boards default to the bottom, but it is flagged


@pytest.mark.skipif(not (ROOT / "exoutput.json").exists(), reason="earlier grouping output not present")
def test_divider_mode_still_converts():
    old = json.loads((ROOT / "exoutput.json").read_text(encoding="utf-8"))
    plan, _ = conv.convert(old, BED)
    assert plan["organizer"] == "trays"
    assert {c["tray"] for c in plan["compartments"]} == {s["name"] for s in old["sets"]}
    assert plan["bottom_layer"][0]["dimensions_mm"]["height"] == 8.0
    plan_layout(plan)  # still a valid plan for the tray engine


# ---------------------------------------------------------------------------
# Engine (no CAD)
# ---------------------------------------------------------------------------
def test_four_plain_boxes_on_the_board(boxes_plan):
    plan, designs, packing = plan_layout(boxes_plan)
    assert plan.mode == "boxes" and plan.floor_z == 8 and plan.usable_height == 57
    assert [d.name for d in designs] == SETS and len(packing.trays) == 4
    x0, y0, x1, y1 = plan.support_rect
    for p in packing.trays:
        geoms = place_cells(p.design, p.w, p.l, p.h, plan.settings)
        assert len(geoms) == 1
        g = geoms[0]
        assert g.kind == "rect" and g.notch == "none" and g.scoop_r == 0 and not g.wells
        # compact: no growing to fill the box, no raised floor
        assert (p.w, p.l, p.h) == pytest.approx((p.design.min_w, p.design.min_l, p.design.min_h))
        assert p.h - g.depth == pytest.approx(plan.settings.floor_mm)
        assert p.z == pytest.approx(8.0) and p.z + p.h <= 65 + 1e-9
        fw, fl = p.footprint
        assert x0 - 1e-6 <= p.x and p.x + fw <= x1 + 1e-6 and y0 - 1e-6 <= p.y and p.y + fl <= y1 + 1e-6
        assert max(p.w, p.l) <= 256 and p.h <= 256


def test_box_sizes_follow_contents(boxes_plan):
    plan, _, _ = plan_layout(boxes_plan)
    c = plan.settings.clearance_mm
    cells = {comp.name: comp.cells for comp in plan.compartments}
    assert all(len(v) == 1 for v in cells.values())
    tokens = cells["Shared Supply: Battle Tokens"][0]
    assert (tokens.w, tokens.l, tokens.depth) == pytest.approx((20 + 2 * c, 20 + 2 * c, 20 * 2 + c))
    dice = cells["Shared Supply: Dice"][0]
    assert dice.w >= 2 * 16 + 2 * c and dice.l >= 2 * 16 + 2 * c and dice.depth == pytest.approx(16 + c)
    for kit in ("Red Player Kit", "Blue Player Kit"):
        cell = cells[kit][0]
        arr = cell.contents["arrangement_mm"]
        assert sorted(a["member"] for a in arr).count(arr[0]["member"]) in (3, 6)
        assert len(arr) == 9  # every pawn and marker has a place
        for a in arr:  # inside the cavity (clearance on each side)
            assert a["x"] >= -1e-6 and a["y"] >= -1e-6
            assert a["x"] + a["w"] <= cell.w - 2 * c + 1e-6 and a["y"] + a["l"] <= cell.l - 2 * c + 1e-6
        for i, a in enumerate(arr):  # no overlap between pieces
            for b in arr[i + 1:]:
                assert (min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]) <= 1e-6 or
                        min(a["y"] + a["l"], b["y"] + b["l"]) - max(a["y"], b["y"]) <= 1e-6)
        assert cell.depth == pytest.approx(25 + c)  # pawns stand upright


def test_too_tall_stack_is_reported_not_split(boxes_plan):
    boxes_plan["compartments"][3]["members"][0]["quantity"] = 40  # 80 mm stack, 57 mm available
    with pytest.raises(PlanError) as e:
        plan_layout(boxes_plan)
    assert any(i.code == "too_tall" and "never split" in i.message for i in e.value.issues.errors)


@pytest.mark.parametrize("key,value", [("tray", "All"), ("stacks", 2), ("pockets", True)])
def test_boxes_mode_rejects_subdivisions(boxes_plan, key, value):
    boxes_plan["compartments"][2][key] = value
    with pytest.raises(PlanError) as e:
        plan_layout(boxes_plan)
    assert e.value.issues.errors[0].code == "unsupported_in_boxes_mode"


def test_boxes_mode_rejects_shaped_types(boxes_plan):
    boxes_plan["compartments"].append({"name": "Coins", "type": "round_tokens", "quantity": 5,
                                       "piece_mm": {"diameter": 20, "thickness": 2}})
    with pytest.raises(PlanError, match="not available in organizer 'boxes' mode"):
        plan_layout(boxes_plan)


def test_board_too_large_is_rejected(boxes_plan):
    boxes_plan["bottom_layer"][0]["dimensions_mm"]["length"] = 300
    with pytest.raises(PlanError, match="does not fit the box interior"):
        plan_layout(boxes_plan)


# ---------------------------------------------------------------------------
# Full CAD run
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def boxes_output(tmp_path_factory):
    if not HAS_CADQUERY:
        pytest.skip("CadQuery not installed")
    from organizer import generate
    plan, _ = conv.convert(json.loads((ROOT / "examples/holden_boxes_grouping.json").read_text()), BED)
    dest = tmp_path_factory.mktemp("holden_boxes")
    return dest, generate(plan, dest)


@needs_cad
def test_exactly_four_valid_open_boxes(boxes_output):
    dest, layout = boxes_output
    assert layout["status"] == "ok" and layout["organizer_mode"] == "boxes"
    assert sorted(p.name for p in dest.glob("*.stl")) == [
        "01_red_player_kit.stl", "02_blue_player_kit.stl", "03_shared_supply_dice.stl",
        "04_shared_supply_battle_tokens.stl"]
    for t in layout["trays"]:
        mv = t["mesh_validation"]
        assert mv["watertight"] and mv["manifold"] and mv["shells"] == 1 and mv["ok"]
        assert [f["kind"] for f in t["features"]] == ["cavity"]  # no notch, slot, divider or scoop
        assert len(t["cells"]) == 1
        # independent re-check on the STL file: one open rectangular cavity, nothing inside it
        tris = read_stl(dest / t["stl"])
        o, cav, cell = t["outer_mm"], t["cavity_mm"], t["cells"][0]
        rep = single_open_cavity(tris, (o["width"], o["length"], o["height"]),
                                 (*cell["cavity_center_mm"], cav["width"], cav["length"], cav["depth"]))
        assert rep["ok"], (t["name"], rep)
    assert layout["validation"]["layout"]["min_board_support_fraction"] == 1.0
    assert layout["bottom_layer"][0]["z_range_mm"] == [0.0, 8.0]


@needs_cad
def test_single_cavity_check_detects_dividers(generated):
    """The same check must fail on a tray that really has internal walls."""
    dest, layout = generated["typed"]
    t = next(t for t in layout["trays"] if t["name"] == "Player pieces")
    o, cell = t["outer_mm"], t["cells"][0]
    whole = (0.0, 0.0, o["width"] - 2 * layout["constraints"]["wall_mm"],
             o["length"] - 2 * layout["constraints"]["wall_mm"], cell["cavity_mm"]["depth"])
    rep = single_open_cavity(read_stl(dest / t["stl"]), (o["width"], o["length"], o["height"]), whole)
    assert not rep["ok"] and rep["cavity_points_not_on_floor"] > 0


@needs_cad
def test_glb_has_four_boxes_and_the_board(boxes_output):
    from organizer.gltf import accessor_array, read_glb
    dest, layout = boxes_output
    gltf, blob = read_glb(dest / "organizer.glb")
    nodes = gltf["nodes"][1:]
    printable = [n for n in nodes if n["extras"].get("printable")]
    assert sorted(n["extras"]["name"] for n in printable) == sorted(SETS)
    board = [n for n in nodes if n["extras"].get("kind") == "bottom_layer"]
    assert len(board) == 1 and board[0]["translation"][2] == 0 and board[0]["extras"]["z_range_mm"] == [0.0, 8.0]
    for n in printable:
        t = next(t for t in layout["trays"] if t["name"] == n["extras"]["name"])
        assert n["translation"][2] == pytest.approx(8.0)
        assert n["extras"]["contents"] == t["contents"]
        pos = accessor_array(gltf, blob, gltf["meshes"][n["mesh"]]["primitives"][0]["attributes"]["POSITION"])
        assert np.allclose(pos, read_stl(dest / t["stl"]).reshape(-1, 3), atol=1e-4)
