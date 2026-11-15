"""CadQuery geometry: turn placed cells into one watertight printable tray solid.

Tray frame: x/y centred on the footprint, z = 0 on the build plate, open top.
Every cavity is cut down from the rim (raised floors), so all tools extend
1 mm above the rim to avoid coplanar faces.
"""
from __future__ import annotations

from .grouping import CellGeom
from .schema import Settings


class CadError(RuntimeError):
    pass


def _cq():
    try:
        import cadquery as cq
    except ImportError as e:  # pragma: no cover - environment dependent
        raise CadError("CadQuery is required for STL generation: pip install cadquery") from e
    return cq


def _box(cq, x0, y0, z0, x1, y1, z1):
    return cq.Solid.makeBox(x1 - x0, y1 - y0, z1 - z0, cq.Vector(x0, y0, z0))


def _rect_cavity(cq, g: CellGeom, H: float, top: float):
    z0 = H - g.depth
    wp = cq.Workplane("XY").box(g.w, g.l, top - z0, centered=(True, True, False))
    r = min(g.scoop_r, 0.45 * min(g.w, g.l), 0.9 * g.depth)
    if r >= 1.0:
        wp = wp.edges("<Z").fillet(r)
    return wp.val().translate(cq.Vector(g.cx, g.cy, z0)), (r if r >= 1.0 else 0.0)


def _u_notch(cq, side: str, g: CellGeom, W: float, L: float, top: float, width: float, z_bottom: float):
    """U-shaped cut through everything between the cavity edge and the tray's outer face."""
    r = width / 2
    zc = z_bottom + r
    sign = 1 if side[0] == "+" else -1
    if side[1] == "x":
        edge = g.cx + sign * g.w / 2
        a0, a1 = sorted((edge - sign * 0.5, sign * (W / 2 + 1.0)))
        cyl = cq.Solid.makeCylinder(r, a1 - a0, cq.Vector(a0, g.cy, zc), cq.Vector(1, 0, 0))
        box = _box(cq, a0, g.cy - r, zc, a1, g.cy + r, top) if top > zc + 1e-3 else None
    else:
        edge = g.cy + sign * g.l / 2
        a0, a1 = sorted((edge - sign * 0.5, sign * (L / 2 + 1.0)))
        cyl = cq.Solid.makeCylinder(r, a1 - a0, cq.Vector(g.cx, a0, zc), cq.Vector(0, 1, 0))
        box = _box(cq, g.cx - r, a0, zc, g.cx + r, a1, top) if top > zc + 1e-3 else None
    return cyl if box is None else cyl.fuse(box)


def build_tray(geoms: list[CellGeom], W: float, L: float, H: float, s: Settings):
    """Return (cadquery Solid, features list) for one tray."""
    cq = _cq()
    top = H + 1.0
    body = cq.Workplane("XY").box(W, L, H, centered=(True, True, False))
    rc = min(s.corner_radius_mm, 0.45 * s.wall_mm)
    if rc >= 0.1:
        body = body.edges("|Z").fillet(rc)
    tools, features = [], []
    for g in geoms:
        z0 = H - g.depth
        if g.kind == "wells":
            for (x, y), d in zip(g.wells, g.slot_dirs):
                tools.append(cq.Solid.makeCylinder(g.well_r, top - z0, cq.Vector(x, y, z0)))
                if d is None:
                    continue
                zs = max(z0 + 1.0, H - g.slot_depth)
                hw = g.slot_w / 2
                if d[0]:
                    x0, x1 = sorted((x, d[0] * (W / 2 + 1.0)))
                    tools.append(_box(cq, x0, y - hw, zs, x1, y + hw, top))
                else:
                    y0, y1 = sorted((y, d[1] * (L / 2 + 1.0)))
                    tools.append(_box(cq, x - hw, y0, zs, x + hw, y1, top))
            features.append({"kind": "token_wells", "cell": g.name, "count": len(g.wells),
                             "diameter_mm": round(2 * g.well_r, 2), "depth_mm": round(g.depth, 2)})
            n_slots = sum(1 for d in g.slot_dirs if d)
            if n_slots:
                features.append({"kind": "finger_slots", "cell": g.name, "count": n_slots,
                                 "width_mm": round(g.slot_w, 2), "depth_mm": round(H - max(z0 + 1.0, H - g.slot_depth), 2)})
        else:
            tool, r = _rect_cavity(cq, g, H, top)
            tools.append(tool)
            features.append({"kind": "cavity", "cell": g.name,
                             "size_mm": [round(g.w, 2), round(g.l, 2), round(g.depth, 2)],
                             **({"scoop_radius_mm": round(r, 2)} if r else {})})
        for side in g.notch_sides:
            span = g.l if side[1] == "x" else g.w
            if g.notch == "deep":
                width = min(s.finger_notch_width_mm, 0.6 * span)
                zb = max(z0 + 1.0, H - 0.6 * g.depth)
            else:  # shallow semicircle at the rim (original prototype behaviour)
                if span < 30 or g.depth < 10:
                    continue
                rr = min(8.0, span / 5, g.depth * 0.45)
                width, zb = 2 * rr, H - rr
            tools.append(_u_notch(cq, side, g, W, L, top, width, zb))
            features.append({"kind": "finger_notch", "cell": g.name, "side": side,
                             "width_mm": round(width, 2), "depth_mm": round(H - zb, 2)})
        if g.notch == "deep" and not g.notch_sides:
            features.append({"kind": "warning", "cell": g.name,
                             "message": "no outer wall available for a finger notch"})

    solid = body.val()
    try:
        result = solid.cut(*tools).clean() if tools else solid
    except Exception:  # pragma: no cover - fall back to sequential booleans
        result = solid
        for t in tools:
            result = result.cut(t)
        result = result.clean()
    solids = result.Solids()
    if len(solids) != 1:
        raise CadError(f"boolean result has {len(solids)} solids (a cut split the tray)")
    shape = solids[0]
    if not shape.isValid():
        raise CadError("OpenCascade reports the tray solid as invalid")
    return shape, features


def export_stl(shape, path, s: Settings) -> None:
    ok = shape.exportStl(str(path), tolerance=s.stl_tolerance_mm, angularTolerance=s.stl_angular_tolerance_rad,
                         ascii=False, relative=False)
    if ok is False:
        raise CadError(f"STL export failed for {path}")


def box_shell_triangles(W: float, L: float, H: float, t: float = 2.0):
    """Thin open-top box around the interior (preview only, never printed)."""
    import numpy as np
    cq = _cq()
    outer = cq.Solid.makeBox(W + 2 * t, L + 2 * t, H + t, cq.Vector(-t, -t, -t))
    shell = outer.cut(cq.Solid.makeBox(W, L, H + 1, cq.Vector(0, 0, 0)))
    verts, tris = shell.tessellate(0.1, 0.3)
    V = np.array([(v.x, v.y, v.z) for v in verts], dtype=np.float64)
    return V[np.array(tris, dtype=np.int64)]
