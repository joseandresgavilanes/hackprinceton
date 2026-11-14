"""Mesh I/O and printability checks using numpy only.

The checks run on the triangles actually written to the STL file, not on the
CAD model, so they catch tessellation/export problems too:

* watertight & 2-manifold: every edge is shared by exactly two triangles
* consistent winding: each shared edge is traversed once in each direction
* outward orientation: signed volume > 0
* one shell (no loose fragments), no degenerate faces
* bounding box and volume agree with the CAD model
* minimum wall/floor thickness, measured by casting axis-aligned rays
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

STL_DTYPE = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def read_stl(path) -> np.ndarray:
    """Return triangles as float64 array (n, 3, 3). Supports binary and ASCII STL."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data):
            return np.frombuffer(data, dtype=STL_DTYPE, count=n, offset=84)["v"].astype(np.float64)
    text = data.decode("ascii", errors="replace")
    vals = [list(map(float, ln.split()[1:4])) for ln in text.splitlines() if ln.strip().startswith("vertex")]
    if not vals or len(vals) % 3:
        raise ValueError(f"{path}: not a valid STL file")
    return np.array(vals, dtype=np.float64).reshape(-1, 3, 3)


def write_stl(path, tris: np.ndarray) -> None:
    tris = np.asarray(tris, dtype=np.float32)
    rec = np.zeros(len(tris), dtype=STL_DTYPE)
    rec["v"] = tris
    rec["n"] = face_normals(tris)
    with open(path, "wb") as f:
        f.write(b"Inside the Box organizer".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        f.write(rec.tobytes())


def box_triangles(x0, y0, z0, x1, y1, z1) -> np.ndarray:
    """Closed, outward-facing axis-aligned box as (12, 3, 3) triangles."""
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=np.float64)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def face_normals(tris: np.ndarray) -> np.ndarray:
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    out = np.where(ln > 1e-12, n / np.where(ln > 1e-12, ln, 1), np.array([0.0, 0.0, 1.0]))
    return out.astype(tris.dtype)


def _weld(tris: np.ndarray, q: float = 1e-4):
    keys = np.round(tris.reshape(-1, 3) / q).astype(np.int64)
    _, inv = np.unique(keys, axis=0, return_inverse=True)
    return inv.reshape(-1, 3), int(inv.max()) + 1


def _components(n_vertices: int, faces: np.ndarray) -> int:
    parent = np.arange(n_vertices)

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    for a, b, c in faces:
        ra, rb, rc = find(a), find(b), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    used = np.unique(faces)
    return len({find(v) for v in used})


def min_thickness(tris: np.ndarray, spacing: float = 1.0, max_rays: int = 2500, chunk: int = 128,
                  min_cos: float = 0.35) -> dict:
    """Smallest solid span along x, y and z, found by casting a grid of rays through the mesh.

    Uses the outward normals: a hit on a face whose normal opposes the ray enters the solid,
    one that agrees exits it, and a span runs from an entry to the next exit. Faces nearly
    parallel to the ray (|cos| < ``min_cos``) are ignored: tessellated curved walls are
    slightly slanted and grazing them would report fake slivers. Ignoring a crossing can
    only merge spans (over-estimate), never report a wall thinner than it is; a genuinely
    thin wall is perpendicular to some axis and is measured exactly along it.
    """
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(0), pts.max(0)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    nlen = np.linalg.norm(normals, axis=1)
    result = {}
    offsets = (0.01731, 0.02377)  # break symmetry so rays avoid edges and vertices
    for axis, name in enumerate("xyz"):
        b, c = [i for i in range(3) if i != axis]
        nb = max(2, int((hi[b] - lo[b]) / spacing))
        nc = max(2, int((hi[c] - lo[c]) / spacing))
        if nb * nc > max_rays:
            f = (nb * nc / max_rays) ** 0.5
            nb, nc = max(2, int(nb / f)), max(2, int(nc / f))
        gb = lo[b] + (np.arange(nb) + 0.5) * (hi[b] - lo[b]) / nb + offsets[0]
        gc = lo[c] + (np.arange(nc) + 0.5) * (hi[c] - lo[c]) / nc + offsets[1]
        P = np.stack(np.meshgrid(gb, gc, indexing="ij"), -1).reshape(-1, 2)
        cosv = normals[:, axis] / np.where(nlen > 0, nlen, 1)
        keep = (nlen > 1e-12) & (np.abs(cosv) >= min_cos)
        T = tris[keep]
        entering = cosv[keep] < 0
        A, B, C = T[:, 0], T[:, 1], T[:, 2]
        A2, e1, e2 = A[:, [b, c]], (B - A)[:, [b, c]], (C - A)[:, [b, c]]
        det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        Aa, d1, d2 = A[:, axis], (B - A)[:, axis], (C - A)[:, axis]
        tmin = np.minimum(np.minimum(A2, A2 + e1), A2 + e2)
        tmax = np.maximum(np.maximum(A2, A2 + e1), A2 + e2)
        best, where = np.inf, None
        for s in range(0, len(P), chunk):
            Pk = P[s:s + chunk]
            pmin, pmax = Pk.min(0), Pk.max(0)
            sel = np.nonzero((tmax[:, 0] >= pmin[0]) & (tmin[:, 0] <= pmax[0]) &
                             (tmax[:, 1] >= pmin[1]) & (tmin[:, 1] <= pmax[1]))[0]
            if not len(sel):
                continue
            d = Pk[:, None, :] - A2[sel][None]
            u = (d[..., 0] * e2[sel, 1] - d[..., 1] * e2[sel, 0]) / det[sel]
            v = (e1[sel, 0] * d[..., 1] - e1[sel, 1] * d[..., 0]) / det[sel]
            hit = (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
            ri, ti = np.nonzero(hit)
            if not len(ri):
                continue
            t = Aa[sel][ti] + u[ri, ti] * d1[sel][ti] + v[ri, ti] * d2[sel][ti]
            ent = entering[sel][ti]
            order = np.lexsort((t, ri))
            ri, t, ent = ri[order], t[order], ent[order]
            bounds = np.flatnonzero(np.diff(ri)) + 1
            for rr, tt, ee in zip(np.split(ri, bounds), np.split(t, bounds), np.split(ent, bounds)):
                start = None
                for tv, is_in in zip(tt, ee):
                    if is_in:
                        start = tv
                    elif start is not None:
                        span = tv - start
                        start = None
                        if 1e-6 < span < best:
                            p = Pk[rr[0]]
                            loc = [0.0, 0.0, 0.0]
                            loc[b], loc[c], loc[axis] = float(p[0]), float(p[1]), float(tv - span)
                            best, where = float(span), [round(x, 2) for x in loc]
        result[name] = {"min_mm": None if best == np.inf else round(best, 3), "at_mm": where}
    vals = [r["min_mm"] for r in result.values() if r["min_mm"] is not None]
    result["overall_min_mm"] = min(vals) if vals else None
    return result


def top_surface(tris: np.ndarray, pts: np.ndarray, chunk: int = 256) -> np.ndarray:
    """Highest z of the mesh above each (x, y) point (vertical rays); NaN where nothing is hit."""
    A, B, C = tris[:, 0], tris[:, 1], tris[:, 2]
    e1, e2 = (B - A)[:, :2], (C - A)[:, :2]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    keep = np.abs(det) > 1e-12
    A, e1, e2, det = A[keep], e1[keep], e2[keep], det[keep]
    dz1, dz2 = (B - tris[:, 0])[keep, 2], (C - tris[:, 0])[keep, 2]
    lo = np.minimum(np.minimum(A[:, :2], A[:, :2] + e1), A[:, :2] + e2)
    hi = np.maximum(np.maximum(A[:, :2], A[:, :2] + e1), A[:, :2] + e2)
    out = np.full(len(pts), np.nan)
    for s in range(0, len(pts), chunk):
        P = pts[s:s + chunk]
        sel = np.nonzero((hi[:, 0] >= P[:, 0].min()) & (lo[:, 0] <= P[:, 0].max()) &
                         (hi[:, 1] >= P[:, 1].min()) & (lo[:, 1] <= P[:, 1].max()))[0]
        if not len(sel):
            continue
        d = P[:, None, :] - A[sel, :2][None]
        u = (d[..., 0] * e2[sel, 1] - d[..., 1] * e2[sel, 0]) / det[sel]
        v = (e1[sel, 0] * d[..., 1] - e1[sel, 1] * d[..., 0]) / det[sel]
        z = A[sel, 2][None] + u * dz1[sel][None] + v * dz2[sel][None]
        z = np.where((u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9), z, -np.inf)
        m = z.max(axis=1)
        out[s:s + chunk] = np.where(np.isfinite(m), m, np.nan)
    return out


def single_open_cavity(tris: np.ndarray, size, cavity, step: float = 1.0, inset: float = 0.6) -> dict:
    """Check, on the mesh, that a box has exactly one open rectangular cavity.

    ``size`` = (W, L, H) outer; ``cavity`` = (cx, cy, w, l, depth) in the STL frame. Vertical rays
    must see the cavity floor everywhere inside the cavity rectangle (so no divider, pocket or
    cutout rises from it) and the full-height rim everywhere in the wall band around it.
    """
    W, L, H = size
    cx, cy, w, l, depth = cavity
    xs = np.arange(-W / 2 + inset, W / 2 - inset + 1e-9, step) + 0.0137
    ys = np.arange(-L / 2 + inset, L / 2 - inset + 1e-9, step) + 0.0119
    P = np.stack(np.meshgrid(xs, ys, indexing="ij"), -1).reshape(-1, 2)
    inside = (np.abs(P[:, 0] - cx) < w / 2 - inset) & (np.abs(P[:, 1] - cy) < l / 2 - inset)
    wall = (np.abs(P[:, 0] - cx) > w / 2 + inset) | (np.abs(P[:, 1] - cy) > l / 2 + inset)
    corner = 1.5  # skip the rounded outer corners
    wall &= ~((np.abs(P[:, 0]) > W / 2 - corner) & (np.abs(P[:, 1]) > L / 2 - corner))
    z = top_surface(tris, P)
    floor_z = H - depth
    bad_floor = int(np.sum(~(np.abs(z[inside] - floor_z) < 0.05)))
    bad_rim = int(np.sum(~(np.abs(z[wall] - H) < 0.05)))
    return {"ok": bool(bad_floor == 0 and bad_rim == 0 and inside.any() and wall.any()),
            "cavity_points": int(inside.sum()), "rim_points": int(wall.sum()),
            "cavity_points_not_on_floor": bad_floor, "rim_points_below_top": bad_rim,
            "cavity_floor_z_mm": round(floor_z, 3)}


def validate_mesh(tris: np.ndarray, *, expected_size=None, expected_volume=None, min_feature=None,
                  thickness=True, size_tol: float = 0.1, volume_rtol: float = 0.02) -> dict:
    """Run the printability checks; returns a JSON-serialisable report with an ``ok`` flag."""
    faces, nv = _weld(tris)
    degenerate = (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2])
    good = faces[~degenerate]
    directed = np.concatenate([good[:, [0, 1]], good[:, [1, 2]], good[:, [2, 0]]])
    und = np.sort(directed, axis=1)
    _, ucount = np.unique(und[:, 0] * nv + und[:, 1], return_counts=True)
    _, dcount = np.unique(directed[:, 0] * nv + directed[:, 1], return_counts=True)
    boundary = int(np.sum(ucount == 1))
    nonmanifold = int(np.sum(ucount > 2))
    flipped = int(np.sum(dcount > 1))
    volume = float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)
    area = 0.5 * np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
    shells = _components(nv, good) if len(good) else 0
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    E, F = len(ucount), len(good)
    chi = len(np.unique(good)) - E + F
    rep = {
        "triangles": int(len(tris)),
        "watertight": boundary == 0,
        "manifold": nonmanifold == 0,
        "consistent_winding": flipped == 0,
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "inconsistent_edges": flipped,
        "degenerate_triangles": int(degenerate.sum() + np.sum(area[~degenerate] < 1e-10)),
        "shells": shells,
        "euler_characteristic": int(chi),
        "volume_mm3": round(volume, 2),
        "outward_normals": volume > 0,
        "bbox_mm": [round(float(x), 3) for x in hi - lo],
    }
    problems = []
    if boundary:
        problems.append(f"{boundary} open (boundary) edges — mesh is not watertight")
    if nonmanifold:
        problems.append(f"{nonmanifold} non-manifold edges")
    if flipped:
        problems.append(f"{flipped} edges with inconsistent winding")
    if volume <= 0:
        problems.append("normals point inwards or volume is zero")
    if shells != 1:
        problems.append(f"mesh has {shells} separate shells (expected 1)")
    if expected_size is not None:
        dev = float(np.max(np.abs((hi - lo) - np.asarray(expected_size))))
        rep["bbox_deviation_mm"] = round(dev, 4)
        if dev > size_tol:
            problems.append(f"bounding box differs from the CAD size by {dev:.3f} mm")
    if expected_volume:
        rel = abs(volume - expected_volume) / expected_volume
        rep["volume_vs_cad"] = round(rel, 5)
        if rel > volume_rtol:
            problems.append(f"mesh volume differs from the CAD volume by {rel:.1%}")
    if thickness:
        th = min_thickness(tris)
        rep["min_thickness"] = th
        if min_feature is not None and th["overall_min_mm"] is not None and th["overall_min_mm"] < min_feature - 0.05:
            problems.append(f"thinnest section {th['overall_min_mm']:.2f} mm is below the "
                            f"{min_feature:.2f} mm minimum feature size")
    rep["problems"] = problems
    rep["ok"] = not problems
    return rep
