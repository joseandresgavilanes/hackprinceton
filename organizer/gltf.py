"""Minimal binary glTF 2.0 (GLB) writer for the organizer preview.

The meshes are the *same triangles* that were written to the STL files (read
back from disk), so the preview can never drift from what gets printed.

Node layout::

    InsideTheBox (root)   rotation: Z-up → glTF Y-up, scale: mm → m
      ├─ Game box          non-printable ghost of the box interior
      ├─ <tray 1>          translation/rotation in box millimetres (Z-up)
      └─ ...

Each tray node's ``extras`` carries ``assembled_translation_mm`` and
``exploded_translation_mm`` (both in the root's mm frame) plus an
``assembly_order`` so a frontend can animate an exploded view by lerping the
node's position.
"""
from __future__ import annotations

import json
import math
import struct

import numpy as np

from .mesh import face_normals

_ARRAY_BUFFER = 34962
_FLOAT = 5126


def _pad(b: bytes, fill: bytes) -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def quat_z(deg: float) -> list[float]:
    h = math.radians(deg) / 2
    return [0.0, 0.0, round(math.sin(h), 9), round(math.cos(h), 9)]


def build_gltf(parts: list[dict], scene_extras: dict | None = None) -> tuple[dict, bytes]:
    """parts: dicts with name, triangles (n,3,3 mm, part-local), color (rgba), translation (mm),
    rotation_deg (about +z), extras, optional transparent flag. Returns (gltf json, binary buffer)."""
    s2 = math.sqrt(0.5)
    gltf = {
        "asset": {"version": "2.0", "generator": "Inside the Box organizer engine"},
        "scene": 0,
        "scenes": [{"name": "organizer", "nodes": [0], "extras": scene_extras or {}}],
        "nodes": [{"name": "InsideTheBox", "rotation": [-s2, 0.0, 0.0, s2], "scale": [0.001, 0.001, 0.001],
                   "children": [], "extras": {"note": "children are in millimetres, Z-up (box frame)"}}],
        "meshes": [], "materials": [], "accessors": [], "bufferViews": [], "buffers": [],
    }
    blob = bytearray()

    def add_view(arr: np.ndarray) -> int:
        nonlocal blob
        blob = bytearray(_pad(bytes(blob), b"\x00"))
        off = len(blob)
        data = arr.astype("<f4").tobytes()
        blob += data
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": off, "byteLength": len(data),
                                    "target": _ARRAY_BUFFER})
        return len(gltf["bufferViews"]) - 1

    for part in parts:
        tris = np.asarray(part["triangles"], dtype=np.float32)
        pos = tris.reshape(-1, 3)
        nrm = np.repeat(face_normals(tris), 3, axis=0)
        pv, nv = add_view(pos), add_view(nrm)
        gltf["accessors"].append({"bufferView": pv, "componentType": _FLOAT, "count": len(pos), "type": "VEC3",
                                  "min": pos.min(0).astype(float).tolist(), "max": pos.max(0).astype(float).tolist()})
        gltf["accessors"].append({"bufferView": nv, "componentType": _FLOAT, "count": len(nrm), "type": "VEC3"})
        r, g, b, a = part.get("color", (0.8, 0.8, 0.8, 1.0))
        mat = {"name": f"{part['name']} material",
               "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, a], "metallicFactor": 0.0,
                                        "roughnessFactor": 0.75}}
        if part.get("transparent"):
            mat.update({"alphaMode": "BLEND", "doubleSided": True})
        gltf["materials"].append(mat)
        acc = len(gltf["accessors"])
        gltf["meshes"].append({"name": part["name"], "primitives": [
            {"attributes": {"POSITION": acc - 2, "NORMAL": acc - 1}, "material": len(gltf["materials"]) - 1,
             "mode": 4}]})
        node = {"name": part["name"], "mesh": len(gltf["meshes"]) - 1,
                "translation": [float(x) for x in part.get("translation", (0, 0, 0))],
                "extras": part.get("extras", {})}
        if part.get("rotation_deg"):
            node["rotation"] = quat_z(part["rotation_deg"])
        gltf["nodes"].append(node)
        gltf["nodes"][0]["children"].append(len(gltf["nodes"]) - 1)
    blob = bytearray(_pad(bytes(blob), b"\x00"))
    gltf["buffers"].append({"byteLength": len(blob)})
    return gltf, bytes(blob)


def write_glb(path, parts: list[dict], scene_extras: dict | None = None) -> dict:
    gltf, blob = build_gltf(parts, scene_extras)
    js = _pad(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<I4s", len(js), b"JSON"))
        f.write(js)
        f.write(struct.pack("<I4s", len(blob), b"BIN\x00"))
        f.write(blob)
    return gltf


def read_glb(path) -> tuple[dict, bytes]:
    """Parse a GLB file (used by the tests and for sanity checks)."""
    data = open(path, "rb").read()
    magic, version, total = struct.unpack("<4sII", data[:12])
    if magic != b"glTF" or version != 2 or total != len(data):
        raise ValueError("not a glTF 2.0 binary file")
    jlen, jtype = struct.unpack("<I4s", data[12:20])
    if jtype != b"JSON":
        raise ValueError("first GLB chunk must be JSON")
    gltf = json.loads(data[20:20 + jlen])
    off = 20 + jlen
    blen, btype = struct.unpack("<I4s", data[off:off + 8])
    if btype != b"BIN\x00":
        raise ValueError("second GLB chunk must be BIN")
    return gltf, data[off + 8:off + 8 + blen]


def accessor_array(gltf: dict, blob: bytes, index: int) -> np.ndarray:
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    return np.frombuffer(blob, dtype="<f4", count=acc["count"] * 3, offset=start).reshape(-1, 3)
