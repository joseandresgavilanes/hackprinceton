"""Mesh validator and GLB writer on synthetic geometry (no CAD needed)."""
import numpy as np
import pytest

from organizer.gltf import accessor_array, read_glb, write_glb
from organizer.mesh import min_thickness, read_stl, validate_mesh, write_stl


def box_tris(x0, y0, z0, x1, y1, z1, inward=False):
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    t = v[np.array(f)]
    return t[:, ::-1] if inward else t


def test_closed_box_passes():
    rep = validate_mesh(box_tris(0, 0, 0, 10, 20, 5), expected_size=(10, 20, 5), expected_volume=1000)
    assert rep["ok"], rep["problems"]
    assert rep["volume_mm3"] == pytest.approx(1000)
    assert rep["euler_characteristic"] == 2 and rep["shells"] == 1


def test_open_mesh_is_not_watertight():
    rep = validate_mesh(box_tris(0, 0, 0, 1, 1, 1)[:-1])
    assert not rep["watertight"] and not rep["ok"]


def test_flipped_face_is_detected():
    t = box_tris(0, 0, 0, 1, 1, 1)
    t[0] = t[0][::-1]
    rep = validate_mesh(t)
    assert not rep["consistent_winding"] and not rep["ok"]


def test_inside_out_and_two_shells_are_detected():
    assert not validate_mesh(box_tris(0, 0, 0, 1, 1, 1, inward=True))["outward_normals"]
    two = np.concatenate([box_tris(0, 0, 0, 1, 1, 1), box_tris(5, 5, 5, 6, 6, 6)])
    assert validate_mesh(two)["shells"] == 2


def test_ray_thickness_of_hollow_box():
    # 2 mm walls, 3 mm floor: outer shell + inward-facing inner surface (open top avoided by closing it)
    outer = box_tris(0, 0, 0, 40, 30, 20)
    inner = box_tris(2, 2, 3, 38, 28, 18, inward=True)  # sealed void → walls 2 mm, floor 3 mm, top 2 mm
    th = min_thickness(np.concatenate([outer, inner]), spacing=1.0)
    assert th["x"]["min_mm"] == pytest.approx(2.0, abs=1e-6)
    assert th["y"]["min_mm"] == pytest.approx(2.0, abs=1e-6)
    assert th["z"]["min_mm"] == pytest.approx(2.0, abs=1e-6)
    rep = validate_mesh(np.concatenate([outer, inner]), min_feature=2.5)
    assert any("minimum feature" in p for p in rep["problems"])


def test_stl_roundtrip(tmp_path):
    t = box_tris(0, 0, 0, 3, 4, 5)
    write_stl(tmp_path / "b.stl", t)
    assert np.allclose(read_stl(tmp_path / "b.stl"), t)


def test_glb_roundtrip_preserves_triangles(tmp_path):
    a, b = box_tris(-5, -5, 0, 5, 5, 8), box_tris(-3, -4, 0, 3, 4, 2)
    parts = [{"name": "A", "triangles": a, "translation": (10, 20, 0), "rotation_deg": 90,
              "extras": {"assembled_translation_mm": [10, 20, 0], "exploded_translation_mm": [10, 20, 50]}},
             {"name": "B", "triangles": b, "color": (1, 0, 0, 0.2), "transparent": True}]
    write_glb(tmp_path / "o.glb", parts, {"units": "mm"})
    gltf, blob = read_glb(tmp_path / "o.glb")
    assert gltf["asset"]["version"] == "2.0"
    root = gltf["nodes"][0]
    assert root["scale"] == [0.001] * 3 and len(root["children"]) == 2
    node_a = gltf["nodes"][1]
    assert node_a["translation"] == [10, 20, 0]
    assert node_a["rotation"] == pytest.approx([0, 0, 2 ** -0.5, 2 ** -0.5])
    assert node_a["extras"]["exploded_translation_mm"] == [10, 20, 50]
    pos = accessor_array(gltf, blob, gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"])
    assert np.allclose(pos, a.reshape(-1, 3))
    assert gltf["materials"][1]["alphaMode"] == "BLEND"
    assert len(blob) % 4 == 0
