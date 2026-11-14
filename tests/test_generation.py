"""End-to-end CAD generation: real STL/GLB files are produced and validated."""
import itertools
import json
import subprocess
import sys

import numpy as np
import pytest

from conftest import ROOT, minimal_plan, needs_cad
from organizer.gltf import accessor_array, read_glb
from organizer.mesh import read_stl, validate_mesh

pytestmark = needs_cad
V1_KEYS = {"name", "stl", "position_mm", "rotation_degrees", "outer_mm", "footprint_in_box_mm", "cavity_mm"}


@pytest.mark.parametrize("key", ["legacy", "typed", "stacked"])
def test_every_stl_is_printable(generated, key):
    dest, layout = generated[key]
    assert layout["status"] == "ok"
    assert layout["validation"]["layout"]["ok"], layout["validation"]["layout"]["problems"]
    min_feature = layout["constraints"]["min_feature_mm"]
    for t in layout["trays"]:
        tris = read_stl(dest / t["stl"])
        rep = validate_mesh(tris, expected_size=[t["outer_mm"][k] for k in ("width", "length", "height")],
                            min_feature=min_feature, thickness=False)
        assert rep["ok"], (t["name"], rep["problems"])
        assert t["mesh_validation"]["ok"]
        assert t["mesh_validation"]["min_thickness"]["overall_min_mm"] >= min_feature - 0.05


@pytest.mark.parametrize("key", ["legacy", "typed", "stacked"])
def test_assembled_trays_do_not_collide_and_stay_in_box(generated, key):
    _, layout = generated[key]
    W, L = layout["box_inner_mm"]["width"], layout["box_inner_mm"]["length"]
    H = layout["usable_height_mm"]
    boxes = []
    for t in layout["trays"]:
        x, y, z = (t["position_mm"][k] for k in "xyz")
        fw, fl = t["footprint_in_box_mm"]["width"], t["footprint_in_box_mm"]["length"]
        h = t["outer_mm"]["height"]
        assert x >= 0 and y >= 0 and x + fw <= W + 0.01 and y + fl <= L + 0.01 and z + h <= H + 0.01
        boxes.append((x, y, z, x + fw, y + fl, z + h))
    for a, b in itertools.combinations(boxes, 2):
        overlap = [min(a[i + 3], b[i + 3]) - max(a[i], b[i]) for i in range(3)]
        assert min(overlap) <= 0.01


def test_layout_is_backward_compatible(generated):
    dest, layout = generated["legacy"]
    assert [t["stl"] for t in layout["trays"]] == [
        "01_playing_cards.stl", "02_square_tiles.stl", "03_red_meeples.stl", "04_blue_meeples.stl",
        "05_green_meeples.stl", "06_yellow_meeples.stl", "07_dice_and_tokens.stl"]
    for t in layout["trays"]:
        assert V1_KEYS <= set(t)
        assert t["rotation_degrees"] in (0, 90)
    assert {"game", "units", "method", "box_inner_mm", "printer_bed_mm", "constraints", "notes"} <= set(layout)


def test_transform_matches_position(generated):
    _, layout = generated["typed"]
    for t in layout["trays"]:
        m = np.array(t["transform_mm"])
        fw, fl = t["footprint_in_box_mm"]["width"], t["footprint_in_box_mm"]["length"]
        w, l = t["outer_mm"]["width"], t["outer_mm"]["length"]
        corners = np.array([[sx * w / 2, sy * l / 2, 0, 1] for sx in (-1, 1) for sy in (-1, 1)]) @ m.T
        assert corners[:, 0].min() == pytest.approx(t["position_mm"]["x"], abs=0.02)
        assert corners[:, 1].min() == pytest.approx(t["position_mm"]["y"], abs=0.02)
        assert corners[:, 0].max() - corners[:, 0].min() == pytest.approx(fw, abs=0.02)
        assert corners[:, 1].max() - corners[:, 1].min() == pytest.approx(fl, abs=0.02)


@pytest.mark.parametrize("key", ["typed", "stacked"])
def test_glb_uses_the_exact_stl_triangles(generated, key):
    dest, layout = generated[key]
    gltf, blob = read_glb(dest / "organizer.glb")
    nodes = {n["name"]: n for n in gltf["nodes"][1:]}
    for t in layout["trays"]:
        node = nodes[t["name"]]
        assert node["extras"]["stl"] == t["stl"]
        assert node["translation"] == pytest.approx(t["assembled_position_mm"], abs=0.01)
        assert node["extras"]["exploded_translation_mm"] == t["exploded_position_mm"]
        prim = gltf["meshes"][node["mesh"]]["primitives"][0]
        pos = accessor_array(gltf, blob, prim["attributes"]["POSITION"])
        stl = read_stl(dest / t["stl"]).reshape(-1, 3)
        assert np.allclose(pos, stl, atol=1e-4)
    assert any(n["extras"].get("printable") is False for n in nodes.values())


def test_typed_features_present(generated):
    _, layout = generated["typed"]
    kinds = {f["kind"] for t in layout["trays"] for f in t["features"]}
    assert {"finger_notch", "token_wells", "finger_slots", "cavity", "dividers"} <= kinds
    scoops = [f for t in layout["trays"] for f in t["features"] if f.get("scoop_radius_mm")]
    assert scoops


def test_cli_success_and_failure(tmp_path):
    out = tmp_path / "ok"
    r = subprocess.run([sys.executable, str(ROOT / "generate_organizer_stl.py"),
                        str(ROOT / "organizer_plan_example.json"), "-o", str(out), "--no-thickness-check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "Generated 7 printable STL files" in r.stdout
    assert (out / "organizer.glb").exists() and (out / "layout.json").exists()

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(minimal_plan({"name": "Deck", "type": "cards", "quantity": 10,
                                            "piece_mm": {"width": 63, "length": 88}})))
    out2 = tmp_path / "bad_out"
    r = subprocess.run([sys.executable, str(ROOT / "generate_organizer_stl.py"), str(bad), "-o", str(out2)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1
    rep = json.loads((out2 / "report.json").read_text())
    assert rep["missing_measurements"][0]["field"] == "piece_mm.thickness"
    assert not list(out2.glob("*.stl"))
