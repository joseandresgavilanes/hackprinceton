"""organizer "boxes": Holden's grouping JSON → one plain open box per set; excluded items ignored."""
import copy
import importlib.util
import itertools
import json
import math

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
# Holden's grouping outputs, converted with no manual edits: (file, number of sets)
GROUPINGS = [("exoutput.json", 3), ("examples/holden_boxes_grouping.json", 4),
             ("examples/ttr_london_grouping.json", 6)]


def load_grouping(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture
def grouping():
    return load_grouping("examples/holden_boxes_grouping.json")


@pytest.fixture
def ttr():
    return load_grouping("examples/ttr_london_grouping.json")


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0) > 1e-6


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


def test_excluded_components_are_ignored(grouping, boxes_plan):
    """No container, floor space or fit check for anything in ``excluded``; a board only costs height."""
    assert "bottom_layer" not in boxes_plan and boxes_plan["settings"]["headroom_mm"] == 8.0
    assert boxes_plan["source"]["on_top_of_boxes"] == [{"name": "Folded game board", "height_mm": 8.0}]
    assert [c["name"] for c in boxes_plan["compartments"]] == [g["name"] for g in grouping["sets"]]
    assert boxes_plan["source"]["excluded_ignored"] == ["Folded game board"]
    assert any(n.startswith("Ignored 1 excluded") for n in boxes_plan["conversion_notes"])
    assert any("not verified" in n or "physical_fit_verified" in n for n in boxes_plan["conversion_notes"])


def test_excluded_items_are_never_validated(grouping):
    g = copy.deepcopy(grouping)
    g["excluded"][0]["dimensions_mm"] = {"length": 999, "width": None}   # too big AND incomplete
    g["excluded"].append({"name": "Rulebook", "reason": "on top", "dimensions_mm": {}})
    plan, notes = conv.convert(g, BED)
    assert plan["source"]["excluded_ignored"] == ["Folded game board", "Rulebook"]
    # no thickness given → nothing reserved, but it is reported, never guessed
    assert plan["settings"]["headroom_mm"] == 0 and sum(n.startswith("MISSING") for n in notes) == 2
    plan_layout(plan)


def test_board_and_rulebook_thickness_shortens_every_box(ttr):
    """TTR: 4 mm board + 12.1 mm rulebook lie on the boxes, so 44 − 16.1 = 27.9 mm is left for them."""
    g = copy.deepcopy(ttr)
    g["excluded"].append({"name": "Spare plastic bags", "quantity": 1,
                          "dimensions_mm": {"length": 100, "width": 100, "height": 10}})  # not a board/rulebook
    plan, notes = conv.convert(g, BED)
    assert plan["settings"]["headroom_mm"] == pytest.approx(16.1)
    assert [i["name"] for i in plan["source"]["on_top_of_boxes"]] == [
        "Board map of the London transportation network", "Rule leaflet (rulebook)"]
    assert any("16.1 mm shorter" in n for n in notes)
    p, _, packing = plan_layout(plan)
    assert p.top_z == pytest.approx(27.9)
    assert all(t.z + t.h == pytest.approx(27.9) for t in packing.trays)


def test_too_thick_board_and_rulebook_is_an_error(ttr):
    g = copy.deepcopy(ttr)
    g["excluded"][1]["dimensions_mm"]["height"] = 40.0   # 4 + 40 ≥ 44 mm box
    with pytest.raises(conv.ConversionError, match="no height is left"):
        conv.convert(g, BED)


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


@pytest.mark.parametrize("name,n_sets", GROUPINGS)
def test_one_plain_box_per_set_without_edits(name, n_sets):
    """Each grouping JSON converts as-is (any organizer) into exactly one plain box per set."""
    src = load_grouping(name)
    plan, _ = conv.convert(src, BED)
    assert plan["organizer"] == "boxes" and len(plan["compartments"]) == n_sets == len(src["sets"])
    for comp, g in zip(plan["compartments"], src["sets"]):
        assert comp["name"] == g["name"] and comp["type"] == "set" and comp["storage_style"] == g["storage_style"]
        assert [(m["name"], m["quantity"], m["dimensions_mm"]) for m in comp["members"]] == \
               [(m["name"], m["quantity"], m["dimensions_mm"]) for m in g["members"]]
    box = src["box"]["dimensions_mm"]
    p, designs, packing = plan_layout(plan)
    on_top = sum(i["height_mm"] for i in plan["source"]["on_top_of_boxes"])  # boards/rulebooks only
    assert p.floor_z == 0 and p.usable_height == pytest.approx(box["height"] - on_top)
    assert all(t.z + t.h == pytest.approx(box["height"] - on_top) for t in packing.trays)
    assert len(designs) == len(packing.trays) == n_sets
    for d in designs:
        assert len(d.cells) == 1                                          # one cavity, no dividers
    trays = packing.trays
    # sets that hold the same thing (same members, quantities and sizes) get identical boxes
    by_content = {}
    for t, g in zip(sorted(trays, key=lambda t: t.design.id), src["sets"]):
        key = (g["storage_style"], tuple((m["quantity"], m.get("shape"), tuple(sorted(m["dimensions_mm"].items())))
                                         for m in g["members"]))
        by_content.setdefault(key, set()).add((round(t.w, 6), round(t.l, 6), round(t.h, 6)))
    assert all(len(sizes) == 1 for sizes in by_content.values()), by_content
    # everything fits the box and together the boxes cover the whole floor (only the tray gaps stay free)
    bw, bl = p.bin_size
    gap = p.settings.tray_gap_mm
    assert sum((t.footprint[0] + gap) * (t.footprint[1] + gap) for t in trays) == pytest.approx(bw * bl, rel=1e-6)
    # different kinds of set get clearly different boxes (≥ 25 % apart in width or length)
    shapes = sorted({(round(min(t.w, t.l), 6), round(max(t.w, t.l), 6)) for t in trays})
    for a, b in itertools.combinations(shapes, 2):
        assert max(abs(math.log(a[0] / b[0])), abs(math.log(a[1] / b[1]))) >= math.log(1.25) - 1e-9, (a, b)
    for i, a in enumerate(trays):
        aw, al = a.footprint
        assert 0 <= a.x and a.x + aw <= box["width"] + 1e-6 and 0 <= a.y and a.y + al <= box["length"] + 1e-6
        assert a.z >= 0 and a.z + a.h <= box["height"] + 1e-6
        assert max(a.w, a.l) <= BED[0] and a.h <= BED[2]
        for b in trays[i + 1:]:
            bw, bl = b.footprint
            assert not (_overlap(a.x, a.x + aw, b.x, b.x + bw) and _overlap(a.y, a.y + al, b.y, b.y + bl)
                        and _overlap(a.z, a.z + a.h, b.z, b.z + b.h)), (a.design.name, b.design.name)


@pytest.mark.skipif(not (ROOT / "exoutput.json").exists(), reason="earlier grouping output not present")
def test_typed_divider_conversion_still_available():
    old = load_grouping("exoutput.json")
    plan, _ = conv.convert(old, BED, typed=True)
    assert plan["organizer"] == "trays" and "bottom_layer" not in plan
    assert {c["tray"] for c in plan["compartments"]} == {s["name"] for s in old["sets"]}
    plan_layout(plan)  # still a valid plan for the tray engine


def test_ttr_london_converts_despite_oversized_board(ttr):
    """The excluded 236.6 mm board is larger than the estimated 226 mm box: it must not matter."""
    plan, notes = conv.convert(ttr, BED)
    names = [c["name"] for c in plan["compartments"]]
    assert sum(n.startswith("Player ") and "Bus Set" in n for n in names) == 4
    assert {"Transportation Card Deck", "Destination Ticket Card Deck"} <= set(names)
    styles = {c["name"]: c["storage_style"] for c in plan["compartments"]}
    assert styles["Transportation Card Deck"] == styles["Destination Ticket Card Deck"] == "card_stack"
    assert len(plan["source"]["excluded_ignored"]) == 2
    assert any("ESTIMATE" in n for n in notes)


def test_compact_flag(grouping):
    plan, _ = conv.convert(grouping, BED, compact=True)
    assert plan["settings"]["fill_box"] is False and plan["settings"]["fill_height"] is False


# ---------------------------------------------------------------------------
# Engine (no CAD)
# ---------------------------------------------------------------------------
def test_four_plain_boxes_fit_the_game_box(boxes_plan):
    """Default: all boxes the same height (flush with the lid) and together covering the board."""
    plan, designs, packing = plan_layout(boxes_plan)
    assert plan.mode == "boxes" and plan.floor_z == 0 and plan.usable_height == 57   # 65 − 8 mm board on top
    assert [d.name for d in designs] == SETS and len(packing.trays) == 4
    x0, y0, x1, y1 = plan.bin_rect
    covered = 0.0
    for p in packing.trays:
        geoms = place_cells(p.design, p.w, p.l, p.h, plan.settings)
        assert len(geoms) == 1
        g = geoms[0]
        assert g.kind == "rect" and g.notch == "none" and g.scoop_r == 0 and not g.wells
        assert p.z == pytest.approx(0.0) and p.z + p.h == pytest.approx(57.0)   # same height, flush
        assert p.h - g.depth == pytest.approx(plan.settings.floor_mm)            # deeper cavity, no solid fill
        assert p.w >= p.design.min_w - 1e-6 and p.l >= p.design.min_l - 1e-6     # still holds its contents
        fw, fl = p.footprint
        assert x0 - 1e-6 <= p.x and p.x + fw <= x1 + 1e-6 and y0 - 1e-6 <= p.y and p.y + fl <= y1 + 1e-6
        assert max(p.w, p.l) <= 256 and p.h <= 256
        covered += fw * fl
    # every gap between boxes is just the tray gap: the boxes fill the box floor
    gap = plan.settings.tray_gap_mm
    assert covered >= (x1 - x0 - 2 * gap) * (y1 - y0 - 2 * gap) * 0.95


def test_compact_boxes_on_request(boxes_plan):
    boxes_plan["settings"].update(fill_height=False, fill_box=False)
    plan, designs, packing = plan_layout(boxes_plan)
    for p in packing.trays:
        g = place_cells(p.design, p.w, p.l, p.h, plan.settings)[0]
        assert (p.w, p.l, p.h) == pytest.approx((p.design.min_w, p.design.min_l, p.design.min_h))
        assert p.h - g.depth == pytest.approx(plan.settings.floor_mm)
        assert p.z + p.h <= 57 + 1e-9


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


def test_card_stack_box_is_one_deck(ttr):
    plan, _ = conv.convert(ttr, BED)
    p, _, _ = plan_layout(plan)
    c = p.settings.clearance_mm
    cells = {comp.name: comp.cells for comp in p.compartments}
    for name, (n, w, l) in {"Transportation Card Deck": (44, 44.1, 67.7),
                            "Destination Ticket Card Deck": (20, 43.5, 69.0)}.items():
        (cell,) = cells[name]
        assert (cell.w, cell.l, cell.depth) == pytest.approx((w + 2 * c, l + 2 * c, n * 0.4 + c))
        assert cell.contents["storage_style"] == "card_stack"
        assert cell.contents["stack_height_mm"] == pytest.approx(n * 0.4)


def _sizes(packing, *names):
    return [(round(t.w, 6), round(t.l, 6), round(t.h, 6)) for n in names for t in packing.trays if t.design.name == n]


@pytest.mark.parametrize("compact", [False, True])
def test_similarly_named_sets_share_one_box_shape(ttr, grouping, compact):
    """'Transportation Card Deck' ~ 'Destination Ticket Card Deck', 'Shared Supply: Dice' ~ '…: Battle Tokens'."""
    plan, _ = conv.convert(ttr, BED, compact=compact)
    _, _, packing = plan_layout(plan)
    decks = _sizes(packing, "Transportation Card Deck", "Destination Ticket Card Deck")
    assert len(decks) == 2 and decks[0] == decks[1]                       # same size AND same orientation
    c = 1.0  # default clearance: the shared box still holds the largest deck and the tallest stack
    w, l, h = decks[0]
    assert min(w, l) >= 44.1 + 2 * c + 2 * 2.4 - 1e-6 and max(w, l) >= 69.0 + 2 * c + 2 * 2.4 - 1e-6
    assert h >= 44 * 0.4 + c + 2.0 - 1e-6
    buses = {s for s in _sizes(packing, *[t.design.name for t in packing.trays if "Bus Set" in t.design.name])}
    assert len(buses) == 1
    plan, _ = conv.convert(grouping, BED, compact=compact)
    _, _, packing = plan_layout(plan)
    supply = _sizes(packing, "Shared Supply: Dice", "Shared Supply: Battle Tokens")
    assert supply[0] == supply[1]
    assert any("Similar names" in n for n in packing.notes)


def test_similar_name_sizing_can_be_switched_off(ttr):
    plan, _ = conv.convert(ttr, BED, compact=True)
    plan["settings"]["similar_names_same_size"] = False
    _, _, packing = plan_layout(plan)
    a, b = _sizes(packing, "Transportation Card Deck", "Destination Ticket Card Deck")
    assert a != b and not any(t.design.size_group for t in packing.trays)


def test_card_stack_is_never_split(ttr):
    plan, _ = conv.convert(ttr, BED)
    plan["compartments"][4]["members"][0]["quantity"] = 200  # 80 mm deck, 44 mm box
    with pytest.raises(PlanError) as e:
        plan_layout(plan)
    assert any(i.code == "too_tall" and "never split" in i.message and "deck" in i.message
               for i in e.value.issues.errors)


def test_hand_written_bottom_layer_is_still_supported(boxes_plan):
    """The converter ignores excluded items, but a plan may still declare a bottom layer explicitly."""
    board = {"name": "Folded game board", "quantity": 1,
             "dimensions_mm": {"length": 245.0, "width": 245.0, "height": 8.0}}
    p, _, packing = plan_layout({**boxes_plan, "bottom_layer": [board]})
    assert p.floor_z == 8 and all(t.z == pytest.approx(8.0) and t.z + t.h == pytest.approx(65 - 8) for t in packing.trays)
    board["dimensions_mm"]["length"] = 300
    with pytest.raises(PlanError, match="does not fit the box interior"):
        plan_layout({**boxes_plan, "bottom_layer": [board]})


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
    assert layout["bottom_layer"] == [] and layout["validation"]["layout"]["min_board_support_fraction"] is None


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


def _check_open_boxes(dest, layout, n):
    assert layout["status"] == "ok" and layout["organizer_mode"] == "boxes" and len(layout["trays"]) == n
    assert len(list(dest.glob("*.stl"))) == n
    for t in layout["trays"]:
        mv = t["mesh_validation"]
        assert mv["watertight"] and mv["manifold"] and mv["shells"] == 1 and mv["ok"]
        assert mv["single_open_cavity"]["ok"]
        assert [f["kind"] for f in t["features"]] == ["cavity"] and len(t["cells"]) == 1
        tris = read_stl(dest / t["stl"])
        o, cav, cell = t["outer_mm"], t["cavity_mm"], t["cells"][0]
        rep = single_open_cavity(tris, (o["width"], o["length"], o["height"]),
                                 (*cell["cavity_center_mm"], cav["width"], cav["length"], cav["depth"]))
        assert rep["ok"], (t["name"], rep)


def _check_glb(dest, layout, names):
    """Only the printed boxes (plus the non-printed ghost box); no excluded items; mesh = STL."""
    from organizer.gltf import accessor_array, read_glb
    gltf, blob = read_glb(dest / "organizer.glb")
    nodes = gltf["nodes"][1:]
    printable = [n for n in nodes if n["extras"].get("printable")]
    assert sorted(n["extras"]["name"] for n in printable) == sorted(names)
    assert not [n for n in nodes if n["extras"].get("kind") == "bottom_layer"]
    assert len(nodes) == len(names) + 1
    for n in printable:
        t = next(t for t in layout["trays"] if t["name"] == n["extras"]["name"])
        assert n["translation"][2] == pytest.approx(t["position_mm"]["z"])
        assert n["extras"]["contents"] == t["contents"]
        pos = accessor_array(gltf, blob, gltf["meshes"][n["mesh"]]["primitives"][0]["attributes"]["POSITION"])
        assert np.allclose(pos, read_stl(dest / t["stl"]).reshape(-1, 3), atol=1e-4)


@needs_cad
def test_glb_has_only_the_four_boxes(boxes_output):
    dest, layout = boxes_output
    _check_glb(dest, layout, SETS)
    assert all(t["position_mm"]["z"] == 0 for t in layout["trays"])


@pytest.fixture(scope="module")
def ttr_output(tmp_path_factory):
    if not HAS_CADQUERY:
        pytest.skip("CadQuery not installed")
    from organizer import generate
    plan, _ = conv.convert(load_grouping("examples/ttr_london_grouping.json"), BED)
    dest = tmp_path_factory.mktemp("ttr_london")
    return dest, generate(plan, dest), plan


@needs_cad
def test_ttr_london_six_printable_open_boxes(ttr_output):
    dest, layout, plan = ttr_output
    _check_open_boxes(dest, layout, 6)
    _check_glb(dest, layout, [c["name"] for c in plan["compartments"]])
    for t in layout["trays"]:
        assert t["position_mm"]["z"] + t["outer_mm"]["height"] == pytest.approx(27.9)   # 44 − board − rulebook
    buses = {tuple(t["outer_mm"].values()) for t in layout["trays"] if "Bus Set" in t["name"]}
    assert len(buses) == 1  # the four player bus sets are identical boxes
    decks = {t["name"]: t for t in layout["trays"] if "Card Deck" in t["name"]}
    assert {t["storage_style"] for t in decks.values()} == {"card_stack"}
    assert len({tuple(t["outer_mm"].values()) for t in decks.values()}) == 1  # similarly named decks too
    assert {t["size_group"] for t in decks.values()} == {"card deck"}
