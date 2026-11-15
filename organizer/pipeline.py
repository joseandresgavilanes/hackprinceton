"""End-to-end pipeline: plan → cells → trays → packing → CAD → STL + GLB + layout.json."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .grouping import CellGeom, build_tray_designs, fits, place_cells
from .mesh import box_triangles, read_stl, single_open_cavity, validate_mesh
from .packing import SUPPORT_COVER, PackResult, PlacedTray, pack_organizer
from .schema import Plan, PlanError, parse_plan

ENGINE_VERSION = "2.0.0"
SCHEMA_VERSION = 2

TYPE_COLORS = {
    "cards": (0.30, 0.47, 0.66, 1.0), "tiles": (0.96, 0.52, 0.09, 1.0), "round_tokens": (0.89, 0.34, 0.35, 1.0),
    "dice": (0.45, 0.72, 0.70, 1.0), "bits": (0.33, 0.64, 0.29, 1.0), "generic": (0.70, 0.47, 0.64, 1.0),
    "mixed": (0.62, 0.46, 0.38, 1.0),
}
SET_PALETTE = [(0.84, 0.30, 0.28, 1.0), (0.27, 0.47, 0.80, 1.0), (0.93, 0.72, 0.25, 1.0),
               (0.55, 0.42, 0.75, 1.0), (0.33, 0.64, 0.45, 1.0), (0.90, 0.52, 0.22, 1.0)]

NOTES = [
    "Trays are separate open-top parts. Trays with layer > 0 rest directly on the trays below: on one tray with "
    "the same footprint, or on several trays of one common height whose rims together carry their whole floor.",
    "Print each STL as exported: flat side down, z = 0 on the build plate. Units are millimetres.",
    "Raised floors and thickened walls are solid in CAD; the slicer fills them with infill (10–15% is plenty).",
    "required_mm (when used) is the bounding box of the whole stored group, not one piece.",
    "The packing heuristic is not guaranteed optimal. Verify slicer checks and a physical test fit before a "
    "full print.",
]


class GenerationError(RuntimeError):
    def __init__(self, message: str, layout: dict):
        super().__init__(message)
        self.layout = layout


def plan_layout(plan_dict: dict, allow_partial: bool = False) -> tuple[Plan, list, PackResult]:
    """Validate, group and pack without running CAD (fast; used by --check and the tests)."""
    plan = parse_plan(plan_dict, allow_partial=allow_partial)
    designs = build_tray_designs(plan)
    packing = pack_organizer(designs, plan)
    return plan, designs, packing


# ---------------------------------------------------------------------------
# Layout-level validation (independent of the packer's own bookkeeping)
# ---------------------------------------------------------------------------
def _overlap(a0, a1, b0, b1, eps=1e-3):
    return min(a1, b1) - max(a0, b0) > eps


def _cell_rect(g: CellGeom):
    return (g.cx - g.w / 2, g.cy - g.l / 2, g.cx + g.w / 2, g.cy + g.l / 2)


SUPPORT_CORE = 0.5  # the rims an upper tray rests on must surround the middle half of its base (each axis)


def _open_rects(q: PlacedTray, geoms: list) -> list:
    """Where q's top face is open (cavities; token wells as their bounding squares), in box coordinates."""
    cx, cy = q.center
    out = []
    for g in geoms:
        local = ([(x - g.well_r, y - g.well_r, x + g.well_r, y + g.well_r) for x, y in g.wells]
                 if g.kind == "wells" else [_cell_rect(g)])
        for u0, v0, u1, v1 in local:   # tray frame → box frame (rot is a quarter turn counter-clockwise)
            out.append((cx + u0, cy + v0, cx + u1, cy + v1) if q.rot == 0 else (cx - v1, cy + u0, cx - v0, cy + u1))
    return out


def _contact_points(p: PlacedTray, q: PlacedTray, holes: list) -> list:
    """Corners of the solid parts of q's top face (rims, walls) under p's footprint."""
    (pw, pl), (qw, ql) = p.footprint, q.footprint
    x0, y0, x1, y1 = max(p.x, q.x), max(p.y, q.y), min(p.x + pw, q.x + qw), min(p.y + pl, q.y + ql)
    if x1 - x0 <= 1e-6 or y1 - y0 <= 1e-6:
        return []
    xs = sorted({x0, x1} | {v for h in holes for v in (h[0], h[2]) if x0 < v < x1})
    ys = sorted({y0, y1} | {v for h in holes for v in (h[1], h[3]) if y0 < v < y1})
    pts = []
    for xa, xb in zip(xs, xs[1:]):
        for ya, yb in zip(ys, ys[1:]):
            mx, my = (xa + xb) / 2, (ya + yb) / 2
            if not any(h[0] < mx < h[2] and h[1] < my < h[3] for h in holes):
                pts += [(xa, ya), (xb, ya), (xa, yb), (xb, yb)]
    return pts


def _hull(points: list) -> list:
    """Convex hull, counter-clockwise (Andrew's monotone chain)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _in_hull(hull: list, pt, eps=1e-6) -> bool:
    if len(hull) < 3:
        return False
    return all((b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0]) >= -eps
               for a, b in zip(hull, hull[1:] + hull[:1]))


def rim_supported(p: PlacedTray, below: list, geoms: dict) -> bool:
    """True if the solid rims/walls p rests on surround the middle of its base, so it cannot tip.

    Trays are open boxes: only their solid top face carries anything. A tray on one same-size
    tray rests on its whole rim; a tray placed inside the footprint of one larger open tray only
    touches part of the rim and sits over the cavity.
    """
    pts = [pt for q in below for pt in _contact_points(p, q, _open_rects(q, geoms[q.design.id]))]
    hull = _hull(pts)
    (fw, fl), (cx, cy) = p.footprint, p.center
    dx, dy = fw * SUPPORT_CORE / 2, fl * SUPPORT_CORE / 2
    return all(_in_hull(hull, (cx + sx * dx, cy + sy * dy)) for sx in (-1, 1) for sy in (-1, 1))


def validate_layout(plan: Plan, trays: list[PlacedTray], geoms: dict) -> dict:
    s = plan.settings
    W, L, _ = plan.box
    H = plan.top_z
    m = s.box_margin_mm
    eps = 1e-3
    problems = []
    for p in trays:
        fw, fl = p.footprint
        if p.x < m - eps or p.y < m - eps or p.x + fw > W - m + eps or p.y + fl > L - m + eps:
            problems.append(f"{p.design.name}: outside the box floor")
        if p.z < plan.floor_z - eps:
            problems.append(f"{p.design.name}: intersects the bottom layer (z {p.z:.2f} < {plan.floor_z:.2f} mm)")
        if p.z + p.h > H + eps:
            problems.append(f"{p.design.name}: top at {p.z + p.h:.2f} mm exceeds the usable top {H:.2f} mm")
        if not fits(p.w, p.l, plan.bed[0], plan.bed[1]):
            problems.append(f"{p.design.name}: {p.w:.1f} × {p.l:.1f} mm does not fit the printer bed")
        if plan.bed[2] and p.h > plan.bed[2] + eps:
            problems.append(f"{p.design.name}: {p.h:.1f} mm is taller than the printer's Z limit")
        if p.w + eps < p.design.min_w or p.l + eps < p.design.min_l or p.h + eps < p.design.min_h:
            problems.append(f"{p.design.name}: tray smaller than its contents require")
    collisions = 0
    for i, a in enumerate(trays):
        ax, ay = a.footprint
        for b in trays[i + 1:]:
            bx, by = b.footprint
            if (_overlap(a.x, a.x + ax, b.x, b.x + bx) and _overlap(a.y, a.y + ay, b.y, b.y + by)
                    and _overlap(a.z, a.z + a.h, b.z, b.z + b.h)):
                collisions += 1
                problems.append(f"{a.design.name} collides with {b.design.name}")
    unsupported = 0
    support = plan.support_rect
    board_cover = None
    for p in trays:
        fw, fl = p.footprint
        if p.z <= plan.floor_z + eps:
            if support:
                # The board may slide within its play in the box; support_rect is covered in every position.
                x0, y0, x1, y1 = support
                cx, cy = p.center
                cov = (max(0, min(p.x + fw, x1) - max(p.x, x0)) * max(0, min(p.y + fl, y1) - max(p.y, y0))) / (fw * fl)
                board_cover = cov if board_cover is None else min(board_cover, cov)
                if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                    unsupported += 1
                    problems.append(f"{p.design.name}: its centre is not over the {plan.bottom_layer[-1]['name']} "
                                    f"in every position the board can slide to")
            continue
        cover = 0.0
        below = [q for q in trays if q is not p and abs(q.z + q.h - p.z) <= eps]
        for q in below:
            qw, ql = q.footprint
            cover += max(0, min(p.x + fw, q.x + qw) - max(p.x, q.x)) * max(0, min(p.y + fl, q.y + ql) - max(p.y, q.y))
        if cover < SUPPORT_COVER * fw * fl:
            unsupported += 1
            problems.append(f"{p.design.name}: only {cover / (fw * fl):.0%} of its base is supported")
        elif not rim_supported(p, below, geoms):
            unsupported += 1
            problems.append(f"{p.design.name}: rests over the open cavity of the box below; the rims it touches "
                            f"do not surround the middle of its base")
    min_wall = min_div = min_floor = math.inf
    for p in trays:
        gs = geoms[p.design.id]
        for g in gs:
            min_floor = min(min_floor, p.h - g.depth)
            if g.kind == "wells":
                for x, y in g.wells:
                    min_wall = min(min_wall, p.w / 2 - abs(x) - g.well_r, p.l / 2 - abs(y) - g.well_r)
                for i, (x1, y1) in enumerate(g.wells):
                    for x2, y2 in g.wells[i + 1:]:
                        min_div = min(min_div, math.hypot(x1 - x2, y1 - y2) - 2 * g.well_r)
            else:
                x0, y0, x1, y1 = _cell_rect(g)
                min_wall = min(min_wall, p.w / 2 - max(-x0, x1), p.l / 2 - max(-y0, y1))
        for i, a in enumerate(gs):
            for b in gs[i + 1:]:
                ra, rb = _cell_rect(a), _cell_rect(b)
                sep = max(rb[0] - ra[2], ra[0] - rb[2], rb[1] - ra[3], ra[1] - rb[3])
                min_div = min(min_div, sep)
    if min_wall < s.wall_mm - eps:
        problems.append(f"an outer wall is {min_wall:.2f} mm (< wall_mm {s.wall_mm})")
    if min_div < s.divider_mm - eps:
        problems.append(f"a divider is {min_div:.2f} mm (< divider_mm {s.divider_mm})")
    if min_floor < s.floor_mm - eps:
        problems.append(f"a floor is {min_floor:.2f} mm (< floor_mm {s.floor_mm})")
    if plan.mode == "boxes":
        for p in trays:
            gs = geoms[p.design.id]
            if len(gs) != 1 or gs[0].kind != "rect" or gs[0].notch != "none" or gs[0].scoop_r:
                problems.append(f"{p.design.name}: boxes mode requires exactly one plain rectangular cavity")
    r = lambda v: None if v == math.inf else round(v, 3)
    return {"ok": not problems, "problems": problems, "collisions": collisions, "unsupported_trays": unsupported,
            "min_wall_mm": r(min_wall), "min_divider_mm": r(min_div), "min_floor_mm": r(min_floor),
            "min_board_support_fraction": None if board_cover is None else round(board_cover, 3)}


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------
def _transform(p: PlacedTray) -> list[list[float]]:
    cx, cy = p.center
    c, s = (1.0, 0.0) if p.rot == 0 else (0.0, 1.0)
    return [[c, -s, 0.0, round(cx, 3)], [s, c, 0.0, round(cy, 3)], [0.0, 0.0, 1.0, round(p.z, 3)],
            [0.0, 0.0, 0.0, 1.0]]


def _r(v, n=2):
    return round(float(v), n)


def _cell_record(g: CellGeom, p: PlacedTray) -> dict:
    rec = {"name": g.name, "component": g.component, "type": g.type, "shape": g.kind,
           "cavity_center_mm": [_r(g.cx), _r(g.cy)],
           "cavity_mm": {"width": _r(g.w), "length": _r(g.l), "depth": _r(g.depth)},
           "floor_thickness_mm": _r(p.h - g.depth), "grows_to_fill": g.growable}
    if g.kind == "wells":
        rec["wells_mm"] = [[_r(x), _r(y)] for x, y in g.wells]
        rec["well_diameter_mm"] = _r(2 * g.well_r)
    if g.contents:
        rec["contents"] = g.contents
    return rec


def _clean_stale(dest: Path, keep: set[str]) -> None:
    for f in dest.glob("[0-9][0-9]_*.stl"):
        if f.name not in keep:
            f.unlink()
    rep = dest / "report.json"
    if rep.exists():
        rep.unlink()


def generate(plan_dict: dict, dest, *, glb: bool = True, allow_partial: bool = False,
             thickness_check: bool = True) -> dict:
    """Generate STL files, organizer.glb and layout.json in ``dest``. Returns the layout dict.

    Raises PlanError (ValueError) for unusable input and GenerationError if the
    produced geometry fails validation (files are still written for inspection).
    """
    from .cad import box_shell_triangles, build_tray, export_stl

    dest = Path(dest)
    plan, designs, packing = plan_layout(plan_dict, allow_partial)
    s = plan.settings
    W, L, BH = plan.box
    dest.mkdir(parents=True, exist_ok=True)

    order = sorted(packing.trays, key=lambda p: (p.layer, p.z, p.design.id))
    assembly_order = {p.design.id: k for k, p in enumerate(order)}
    geoms_by, records, parts = {}, [], []
    meshes_ok = True
    for p in packing.trays:
        d = p.design
        geoms = place_cells(d, p.w, p.l, p.h, s)
        geoms_by[d.id] = geoms
        shape, features = build_tray(geoms, p.w, p.l, p.h, s)
        path = dest / d.filename
        export_stl(shape, path, s)
        tris = read_stl(path)
        cad_volume = shape.Volume()
        mv = validate_mesh(tris, expected_size=(p.w, p.l, p.h), expected_volume=cad_volume,
                           min_feature=s.min_feature_mm, thickness=thickness_check)
        if plan.mode == "boxes":  # prove on the printed triangles: one open rectangular cavity, nothing inside
            g0 = geoms[0]
            cav = single_open_cavity(tris, (p.w, p.l, p.h), (g0.cx, g0.cy, g0.w, g0.l, g0.depth))
            mv["single_open_cavity"] = cav
            if len(geoms) != 1 or not cav["ok"]:
                mv["problems"].append("not exactly one open rectangular cavity (divider, pocket or cutout found)")
                mv["ok"] = False
        meshes_ok &= mv["ok"]
        summary = ", ".join(f"{m['quantity']}× {m['name']}" for g in geoms for m in g.contents.get("members", []))
        cx, cy = p.center
        fw, fl = p.footprint
        lift = BH + s.explode_gap_mm * (p.layer + 1)
        exploded = [cx + (cx - W / 2) * 0.35, cy + (cy - L / 2) * 0.35, p.z + lift]
        main = max(geoms, key=lambda g: g.w * g.l)
        n_div = len(d.cells) - 1  # only between cells the plan asked for
        if n_div:
            features.append({"kind": "dividers", "count": n_div, "thickness_mm": s.divider_mm})
        rec = {
            # --- keys from schema v1 (unchanged meaning) ---
            "name": d.name, "stl": d.filename,
            "position_mm": {"x": _r(p.x), "y": _r(p.y), "z": _r(p.z)},
            "rotation_degrees": p.rot,
            "outer_mm": {"width": _r(p.w), "length": _r(p.l), "height": _r(p.h)},
            "footprint_in_box_mm": {"width": _r(fw), "length": _r(fl)},
            "cavity_mm": {"width": _r(main.w), "length": _r(main.l), "depth": _r(main.depth)},
            # --- new in schema v2 ---
            "id": d.id, "type": d.type, "compartments": d.compartments,
            **({"storage_style": geoms[0].contents.get("storage_style"), "contents": summary}
               if d.type == "set" else {}),
            "layer": p.layer, "column": p.column,
            "center_mm": {"x": _r(cx), "y": _r(cy), "z": _r(p.z)},
            "transform_mm": _transform(p),
            "assembled_position_mm": [_r(cx), _r(cy), _r(p.z)],
            "exploded_position_mm": [_r(v) for v in exploded],
            "assembly_order": assembly_order[d.id],
            "min_outer_mm": {"width": _r(d.content_min_w), "length": _r(d.content_min_l),
                             "height": _r(d.content_min_h)},
            **({"size_group": d.size_group} if d.size_group else {}),
            "cells": [_cell_record(g, p) for g in geoms],
            "features": features,
            "solid_volume_cm3": _r(cad_volume / 1000, 3),
            "mesh_validation": mv,
        }
        records.append(rec)
        color = SET_PALETTE[(d.id - 1) % len(SET_PALETTE)] if d.type == "set" else \
            TYPE_COLORS.get(d.type, TYPE_COLORS["mixed"])
        parts.append({"name": d.name, "triangles": tris, "color": color,
                      "translation": (cx, cy, p.z), "rotation_deg": p.rot,
                      "extras": {"tray_id": d.id, "name": d.name, "stl": d.filename, "type": d.type, "layer": p.layer,
                                 "column": p.column, "printable": True, "rotation_degrees": p.rot,
                                 "assembled_translation_mm": rec["assembled_position_mm"],
                                 "exploded_translation_mm": rec["exploded_position_mm"],
                                 "assembly_order": rec["assembly_order"],
                                 **({"contents": summary, "storage_style": rec["storage_style"]}
                                    if d.type == "set" else {}),
                                 "outer_mm": [rec["outer_mm"][k] for k in ("width", "length", "height")]}})

    lv = validate_layout(plan, packing.trays, geoms_by)
    bottom_records, z0 = [], 0.0
    for b in plan.bottom_layer:
        bd = b["dimensions_mm"]
        fits_as_is = bd["width"] <= W + 1e-6 and bd["length"] <= L + 1e-6
        bw, bl = (bd["width"], bd["length"]) if fits_as_is else (bd["length"], bd["width"])
        h = bd["height"] * b["quantity"]
        bottom_records.append({"name": b["name"], "component_id": b.get("component_id"), "printable": False,
                               "dimensions_mm": bd, "quantity": b["quantity"],
                               "z_range_mm": [_r(z0), _r(z0 + h)],
                               "assumed_position_mm": {"x": _r((W - bw) / 2), "y": _r((L - bl) / 2)},
                               "position_note": f"centred for the preview; it can slide by up to {W - bw:g} × "
                                                f"{L - bl:g} mm, which the support check accounts for"})
        z0 += h
    floor_used = sum(p.footprint[0] * p.footprint[1] for p in packing.trays if p.layer == 0)
    tray_vol = sum(p.w * p.l * p.h for p in packing.trays)
    files = {"stl": [r["stl"] for r in records], "glb": "organizer.glb" if glb else None,
             "layout": "layout.json"}
    layout = {
        "schema_version": SCHEMA_VERSION,
        "engine": f"inside-the-box organizer {ENGINE_VERSION}",
        "status": "ok" if (meshes_ok and lv["ok"]) else "invalid",
        "game": plan.game, "units": "mm", "method": packing.method,
        "box_inner_mm": {"width": W, "length": L, "height": BH},
        "usable_height_mm": _r(plan.usable_height),
        "organizer_mode": plan.mode,
        "floor_offset_mm": _r(plan.floor_z),
        "bottom_layer": bottom_records,
        "printer_bed_mm": {"width": plan.bed[0], "length": plan.bed[1],
                           **({"height": plan.bed[2]} if plan.bed[2] else {})},
        "constraints": {"wall_mm": s.wall_mm, "floor_mm": s.floor_mm, "clearance_mm": s.clearance_mm,
                        "tray_gap_mm": s.tray_gap_mm, "box_margin_mm": s.box_margin_mm,
                        "divider_mm": s.divider_mm, "min_feature_mm": s.min_feature_mm,
                        "headroom_mm": s.headroom_mm, "fill_height": s.fill_height, "fill_box": s.fill_box,
                        "max_layers": s.max_layers},
        "coordinate_system": {
            "origin": "lower-left corner of the box interior floor",
            "axes": "x = box width, y = box length, z = up (millimetres)",
            "stl_frame": "each STL is centred on x/y = 0 with its base at z = 0",
            "placement": "rotate the STL by rotation_degrees counter-clockwise about +z, then translate it "
                         "to center_mm (equivalently apply transform_mm, a row-major 4×4 matrix)",
            "position_mm": "lower-left corner of the rotated footprint (schema v1 meaning); z includes the "
                           "bottom layer (floor_offset_mm) the trays rest on",
            "glb": "organizer.glb root node converts this Z-up mm frame to glTF Y-up metres; tray nodes "
                   "use the same mm values as this file",
        },
        "files": files,
        "stats": {"trays": len(records), "layers": packing.layers,
                  "floor_coverage": round(floor_used / (W * L), 3),
                  "tray_volume_fraction": round(tray_vol / (W * L * plan.usable_height), 3),
                  "min_floor_area_needed_mm2": round(packing.min_area_mm2)},
        "notes": NOTES + packing.notes,
        "assumptions": plan.assumptions,
        "warnings": [i.to_dict() for i in plan.issues.warnings],
        "skipped_compartments": plan.skipped,
        "validation": {"layout": lv, "meshes_ok": meshes_ok},
        "trays": records,
    }
    if glb:
        from .gltf import write_glb
        box_part = {"name": "Game box (preview only)", "triangles": box_shell_triangles(W, L, BH),
                    "color": (0.55, 0.50, 0.45, 0.15), "transparent": True,
                    "extras": {"printable": False, "box_inner_mm": [W, L, BH]}}
        board_parts = []
        for b in bottom_records:
            x0, y0 = b["assumed_position_mm"]["x"], b["assumed_position_mm"]["y"]
            bw, bl = W - 2 * x0, L - 2 * y0
            za, zb = b["z_range_mm"]
            board_parts.append({"name": f"{b['name']} (not printed)", "triangles": box_triangles(0, 0, 0, bw, bl, zb - za),
                                "color": (0.30, 0.47, 0.38, 1.0), "translation": (x0, y0, za),
                                "extras": {"printable": False, "kind": "bottom_layer", "name": b["name"],
                                           "z_range_mm": b["z_range_mm"]}})
        write_glb(dest / "organizer.glb", [box_part] + board_parts + parts, scene_extras={
            "units": "mm (root node scales to metres)", "up_axis": "Z in node space; root rotates to +Y up",
            "box_inner_mm": [W, L, BH], "usable_height_mm": _r(plan.usable_height), "layout": "layout.json",
            "floor_offset_mm": _r(plan.floor_z), "organizer_mode": plan.mode,
            "explode": "lerp each tray node's translation from extras.assembled_translation_mm to "
                       "extras.exploded_translation_mm (parent frame, mm); animate in extras.assembly_order"})
    _clean_stale(dest, set(files["stl"]))
    (dest / "layout.json").write_text(json.dumps(layout, indent=2, ensure_ascii=False), encoding="utf-8")
    if layout["status"] != "ok":
        bad = lv["problems"] + [f"{r['name']}: {pr}" for r in records for pr in r["mesh_validation"]["problems"]]
        raise GenerationError("Generated geometry failed validation: " + "; ".join(bad), layout)
    return layout


def write_failure_report(dest, error: PlanError) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "report.json"
    path.write_text(json.dumps(error.report(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


__all__ = ["generate", "plan_layout", "validate_layout", "GenerationError", "write_failure_report",
           "ENGINE_VERSION"]
