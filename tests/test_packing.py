"""Packing, stacking and fill-box behaviour (pure Python)."""
import itertools
import random

import pytest

from conftest import minimal_plan
from organizer import PlanError, plan_layout
from organizer.grouping import place_cells
from organizer.packing import (block_layout, best_layout, equalize, expand, maxrects, scale_to_fill,
                               similar_names, valid_placement)
from organizer.pipeline import validate_layout


def _no_overlap(rects):
    for a, b in itertools.combinations(rects, 2):
        ax, ay, aw, al = a
        bx, by, bw, bl = b
        if min(ax + aw, bx + bw) - max(ax, bx) > 1e-6 and min(ay + al, by + bl) - max(ay, by) > 1e-6:
            return False
    return True


@pytest.mark.parametrize("heuristic", ["bssf", "blsf", "baf", "bl", "cp"])
def test_maxrects_random_cases_never_overlap(heuristic):
    rng = random.Random(1234)
    for _ in range(150):
        sizes = {i: (rng.uniform(5, 80), rng.uniform(5, 80)) for i in range(rng.randint(1, 14))}
        W, L = rng.uniform(100, 250), rng.uniform(100, 250)
        res = maxrects(sizes, W, L, heuristic)
        if res is None:
            continue
        assert valid_placement(res, W, L)
        for k, (x, y, w, l, rot) in res.items():
            assert sorted((w, l)) == pytest.approx(sorted(sizes[k]))


def test_expansion_keeps_rects_disjoint_and_in_bounds():
    rng = random.Random(7)
    for _ in range(100):
        sizes = {i: (rng.uniform(10, 60), rng.uniform(10, 60)) for i in range(rng.randint(2, 10))}
        found = best_layout(sizes, 200, 180)
        if not found:
            continue
        _, res, _ = found
        base = {k: list(v[:4]) for k, v in res.items()}
        grown = expand(scale_to_fill(base, 200, 180), 200, 180)
        rects = list(grown.values())
        assert _no_overlap(rects)
        for (x, y, w, l), (bx, by, bw, bl) in zip(rects, base.values()):
            assert x >= -1e-6 and y >= -1e-6 and x + w <= 200 + 1e-6 and y + l <= 180 + 1e-6
            assert w >= bw - 1e-9 and l >= bl - 1e-9


def test_equalize_gives_twins_one_size_even_when_rotated():
    """Twins 0, 1 (rotated 90°) and 2 start at different sizes; afterwards they match and nothing overlaps."""
    rng = random.Random(7)
    for _ in range(60):
        rects = {0: [0, 0, 40, 60], 1: [50, 0, 70, 30], 2: [130, 0, 45, 65], 3: [0, 80, 90, 50]}
        rects = {k: [v[0] + rng.uniform(0, 5), v[1] + rng.uniform(0, 5), v[2], v[3]] for k, v in rects.items()}
        rots = {0: 0, 1: 90, 2: 0, 3: 0}
        out = equalize(rects, rots, [[0, 1, 2]], 200, 200)
        own = [(out[k][2], out[k][3]) if rots[k] == 0 else (out[k][3], out[k][2]) for k in (0, 1, 2)]
        assert own[0] == own[1] == own[2]
        assert own[0][0] >= 40 - 1e-9 or own[0][1] >= 30 - 1e-9
        assert _no_overlap([tuple(v) for v in out.values()])
        assert all(-1e-6 <= x and -1e-6 <= y and x + w <= 200 + 1e-6 and y + l <= 200 + 1e-6
                   for x, y, w, l in out.values())


def test_identical_trays_keep_the_same_size_after_fill():
    piece = {"type": "bits", "quantity": 10, "piece_mm": {"width": 10, "length": 20, "height": 10}}
    plan = minimal_plan(*[{**piece, "name": f"Player {i}"} for i in range(1, 6)],
                        {"name": "Big", "type": "bits", "quantity": 4, "piece_mm": {"width": 30, "length": 40, "height": 20}},
                        box=(230, 180, 50))
    p, _, packing = plan_layout(plan)
    sizes = {(round(t.w, 6), round(t.l, 6), round(t.h, 6)) for t in packing.trays if t.design.name.startswith("Player")}
    assert len(sizes) == 1
    assert any("same size" in n for n in packing.notes)
    geoms = {t.design.id: place_cells(t.design, t.w, t.l, t.h, p.settings) for t in packing.trays}
    assert validate_layout(p, packing.trays, geoms)["ok"]


@pytest.mark.parametrize("a,b,expected", [
    ("Player 1 Bus Set (17 buses + 1 Scoring Marker)", "Player 4 Bus Set (17 buses + 1 Scoring Marker)", True),
    ("Transportation Card Deck", "Destination Ticket Card Deck", True),
    ("Shared Supply: Dice", "Shared Supply: Battle Tokens", True),
    ("Red Player Kit", "Blue Player Kit", True),
    ("Red Player Kit", "Shared Supply: Dice", False),
    ("Transportation Card Deck", "Player 1 Bus Set", False),
    ("Shared Dice and Battle Tokens", "Red Player Components", False),
    ("Dice", "Battle Tokens", False),
])
def test_similar_names(a, b, expected):
    assert similar_names(a, b) is expected


def test_block_layout_tiles_the_floor_and_keeps_groups_equal():
    rng = random.Random(11)
    done = 0
    for _ in range(80):
        groups, sizes, k = [], {}, 0
        for _ in range(rng.randint(1, 5)):                     # up to 5 kinds of set
            n = rng.choice([1, 1, 2, 3, 4])
            w, l = rng.uniform(20, 70), rng.uniform(20, 90)
            keys = list(range(k, k + n))
            for key in keys:
                sizes[key] = (w + 1, l + 1)
            k += n
            if n > 1:
                groups.append(keys)
        res = block_layout(sizes, groups, 240, 230, 1.0)
        if res is None:
            continue
        done += 1
        _, placements, rects, _ = res
        assert set(rects) == set(sizes)
        assert _no_overlap([tuple(v) for v in rects.values()])
        assert sum(v[2] * v[3] for v in rects.values()) == pytest.approx(240 * 230)
        for key, (x, y, w, l, rot) in placements.items():
            ow, ol = (w, l) if rot == 0 else (l, w)
            assert ow >= sizes[key][0] - 1e-6 and ol >= sizes[key][1] - 1e-6   # still holds its contents
        for g in groups:
            own = {(round(w if r == 0 else l, 6), round(l if r == 0 else w, 6))
                   for x, y, w, l, r in (placements[key] for key in g)}
            assert len(own) == 1
    assert done > 40


def test_block_layout_skips_many_kinds():
    sizes = {i: (20 + i, 30 + i) for i in range(6)}
    assert block_layout(sizes, [], 240, 240, 1.0) is None


def test_legacy_single_layer_layout_is_valid(legacy_plan):
    plan, designs, packing = plan_layout(legacy_plan)
    assert packing.layers == 1 and len(packing.trays) == 7
    geoms = {p.design.id: place_cells(p.design, p.w, p.l, p.h, plan.settings) for p in packing.trays}
    v = validate_layout(plan, packing.trays, geoms)
    assert v["ok"], v["problems"]
    for p in packing.trays:  # fill_height: flush with the usable height
        assert p.h == pytest.approx(plan.usable_height)


def test_fill_options_off_reproduce_minimal_trays(legacy_plan):
    legacy_plan["settings"].update(fill_box=False, fill_height=False)
    plan, designs, packing = plan_layout(legacy_plan)
    for p in packing.trays:
        assert (p.w, p.l, p.h) == pytest.approx((p.design.min_w, p.design.min_l, p.design.min_h))
    cards = next(p for p in packing.trays if p.design.name == "Playing cards")
    assert (cards.w, cards.l, cards.h) == pytest.approx((72.8, 98.8, 29.0))  # identical to the v1 engine


def test_stacking_when_one_layer_is_not_enough(stacked_plan):
    plan, designs, packing = plan_layout(stacked_plan)
    assert packing.layers >= 2
    geoms = {p.design.id: place_cells(p.design, p.w, p.l, p.h, plan.settings) for p in packing.trays}
    v = validate_layout(plan, packing.trays, geoms)
    assert v["ok"], v["problems"]
    assert v["unsupported_trays"] == 0 and v["collisions"] == 0
    upper = [p for p in packing.trays if p.layer > 0]
    for p in upper:
        below = [q for q in packing.trays if q.column == p.column and q.layer == p.layer - 1][0]
        assert below.footprint == pytest.approx(p.footprint)
        assert below.z + below.h == pytest.approx(p.z)


def test_single_layer_fails_gracefully_when_stacking_disabled(stacked_plan):
    stacked_plan["settings"]["max_layers"] = 1
    with pytest.raises(PlanError) as e:
        plan_layout(stacked_plan)
    err = e.value.issues.errors[0]
    assert err.code == "does_not_fit" and "cm²" in err.message


def test_tray_larger_than_printer_bed_is_rejected():
    plan = minimal_plan({"name": "Board-sized", "required_mm": {"width": 150, "length": 150, "height": 10}},
                        box=(300, 300, 50), bed=(120, 120))
    with pytest.raises(PlanError) as e:
        plan_layout(plan)
    assert e.value.issues.errors[0].code == "too_large"


def test_trays_never_exceed_printer_bed_after_fill(legacy_plan):
    legacy_plan["printer_bed_mm"] = {"width": 120, "length": 120, "height": 200}
    plan, _, packing = plan_layout(legacy_plan)
    for p in packing.trays:
        assert max(p.w, p.l) <= 120 + 1e-6
        assert p.h <= 200


def test_grouped_tray_has_one_cell_per_member(typed_plan):
    plan, designs, packing = plan_layout(typed_plan)
    pieces = next(d for d in designs if d.name == "Player pieces")
    assert pieces.compartments == ["Red workers", "Blue workers", "Green workers", "Yellow workers"]
    p = next(t for t in packing.trays if t.design is pieces)
    geoms = place_cells(pieces, p.w, p.l, p.h, plan.settings)
    assert len(geoms) == 4


@pytest.mark.parametrize("fixture", ["legacy_plan", "typed_plan", "stacked_plan"])
def test_fill_box_never_adds_cells_or_dividers(fixture, request):
    plan, designs, packing = plan_layout(request.getfixturevalue(fixture))
    requested = {c.name: len(c.cells) for c in plan.compartments}
    for p in packing.trays:
        geoms = place_cells(p.design, p.w, p.l, p.h, plan.settings)
        # exactly the cells the plan asked for: one per compartment unless stacks/cells/pockets/tray say otherwise
        assert len(geoms) == sum(requested[n] for n in p.design.compartments)
        for g in geoms:
            if not g.growable:  # fixed cavities keep their exact size when the tray grows
                cell = next(c for c in p.design.cells if c.name == g.name)
                assert sorted((g.w, g.l)) == pytest.approx(sorted((cell.w, cell.l)))
    legacy = [d for d in designs if len(d.compartments) == 1 and len(d.cells) == 1]
    assert legacy  # single-compartment trays exist and stay single-cavity


def test_headroom_reduces_usable_height(legacy_plan):
    legacy_plan["settings"]["headroom_mm"] = 10
    plan, _, packing = plan_layout(legacy_plan)
    assert all(p.z + p.h <= 40 + 1e-6 for p in packing.trays)
