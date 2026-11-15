"""Shape-specific compartment designers.

Each designer turns one compartment of the plan into one or more *cells*: the
empty volume (cavity) that must be cut into a tray, plus its ergonomic
features. Sizes come only from the plan's measurements; when a measurement is
missing the designer reports it and returns no cells.

Cell coordinates are local: ``w`` along x, ``l`` along y, ``depth`` measured
down from the tray rim (trays use raised floors so contents sit near the top).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .schema import Compartment, Issues, Settings, dims, num, quantity


@dataclass
class Cell:
    name: str
    component: str
    type: str
    kind: str = "rect"                 # "rect" | "wells"
    w: float = 0.0                     # cavity size along local x
    l: float = 0.0                     # cavity size along local y
    depth: float = 0.0                 # cavity depth below the rim
    growable: bool = False             # bins: cavity may grow with the tray (fill_box)
    deepens: bool = False              # cavity deepens with a taller tray (floor stays floor_mm)
    scoop_r: float = 0.0               # radius of the rounded bottom edges
    notch: str = "none"                # "deep" (both long walls) | "shallow" (one wall) | "none"
    wells: list = field(default_factory=list)      # [(u, v)] well centres relative to the cavity centre
    well_r: float = 0.0
    well_dirs: list = field(default_factory=list)  # finger-slot direction per well: -1/+1 along local y
    slot_w: float = 0.0
    slot_depth: float = 0.0
    contents: dict = field(default_factory=dict)


def _scoop_radius(s: Settings, w: float, l: float, depth: float) -> float:
    return max(0.0, min(s.scoop_radius_mm, 0.35 * depth, 0.25 * min(w, l)))


def _scooped(cell: Cell, s: Settings, max_depth: float) -> Cell:
    """Add a rounded bottom and enlarge the cavity so the curve doesn't steal content space.

    A piece resting against a fillet of radius r is lifted/pushed by at most
    r·(1 - 1/√2) ≈ 0.29 r, so each side grows by 0.3 r and the depth by 0.3 r.
    """
    r = min(_scoop_radius(s, cell.w, cell.l, cell.depth), (max_depth - cell.depth) / 0.3)
    if r < 1.0:
        return cell
    cell.scoop_r = round(r, 2)
    cell.w += 0.6 * r
    cell.l += 0.6 * r
    cell.depth += 0.3 * r
    return cell


def _required_cell(comp: Compartment, s: Settings, issues: Issues, max_depth: float, **features) -> list[Cell]:
    """Legacy / pre-grouped input: `required_mm` is the bounding box of the whole stored group."""
    req = dims(comp.data.get("required_mm"), "required_mm", ("width", "length", "height"), issues, comp.name,
               why="give the bounding box of the whole stored group (stack/pile), not one piece")
    if req is None:
        return []
    c = s.clearance_mm
    n = comp.data.get("cells", 1)
    k = quantity(n, "cells", issues, comp.name) or 1
    scoop = features.pop("scoop", False)
    w, l, h = req["width"], req["length"], req["height"]
    if k > 1:  # split the group across k equal cells along its longer side
        if w >= l:
            w = w / k
        else:
            l = l / k
    out = []
    for i in range(k):
        cell = Cell(name=comp.name if k == 1 else f"{comp.name} #{i + 1}", component=comp.name, type=comp.type,
                    w=w + 2 * c, l=l + 2 * c, depth=h + c,
                    contents={"source": "required_mm", "group_bbox_mm": req}, **features)
        out.append(_scooped(cell, s, max_depth) if scoop else cell)
    return out


def _check_range(value, lo, hi, label, comp, issues):
    if value is not None and not lo <= value <= hi:
        issues.warn("implausible_measurement", f"{comp.name}: {label} = {value:g} mm is outside the usual "
                                               f"{lo:g}–{hi:g} mm range; please re-check the measurement",
                    comp.name, label)


# ---------------------------------------------------------------------------
# Cards and tiles: flat stacks with finger notches on both long walls
# ---------------------------------------------------------------------------
def _stack_cells(comp: Compartment, s: Settings, max_depth: float, issues: Issues) -> list[Cell]:
    d = comp.data
    if "required_mm" in d:
        return _required_cell(comp, s, issues, max_depth, notch="deep")
    kind = "card" if comp.type == "cards" else "tile"
    piece = d.get("piece_mm")
    if piece is None:
        issues.missing(comp.name, "piece_mm", f"give one {kind}'s width, length and thickness in mm "
                                              f"(or a required_mm bounding box for the whole stack)")
        return []
    if not isinstance(piece, dict):
        issues.error("invalid_value", f"{comp.name}: piece_mm must be an object", comp.name, "piece_mm")
        return []
    W = num(piece.get("width"), "piece_mm.width", issues, comp.name)
    L = num(piece.get("length"), "piece_mm.length", issues, comp.name)
    T = None
    sleeve = d.get("sleeve_mm")
    sleeved = bool(d.get("sleeved")) or sleeve is not None
    if sleeved and comp.type == "cards":
        sv = dims(sleeve, "sleeve_mm", ("width", "length", "thickness"), issues, comp.name,
                  why="sleeved cards need the sleeve's outer width/length and one sleeved card's thickness")
        if sv:
            if W and (sv["width"] < W or sv["length"] < (L or 0)):
                issues.error("invalid_value", f"{comp.name}: sleeve_mm is smaller than the bare card", comp.name)
            W, L, T = sv["width"], sv["length"], sv["thickness"]
            _check_range(T, 0.3, 1.2, "sleeve_mm.thickness", comp, issues)
        else:
            W = L = None
    stack_h = num(d.get("stack_height_mm"), "stack_height_mm", issues, comp.name, required=False)
    q = quantity(d.get("quantity"), "quantity", issues, comp.name, required=stack_h is None)
    if T is None and not (sleeved and comp.type == "cards"):
        T = num(piece.get("thickness"), "piece_mm.thickness", issues, comp.name, required=stack_h is None,
                why=f"measure a stack of {kind}s and divide by the count, or give stack_height_mm")
        if comp.type == "cards":
            _check_range(T, 0.15, 1.0, "piece_mm.thickness", comp, issues)
        else:
            _check_range(T, 0.5, 8.0, "piece_mm.thickness", comp, issues)
    if W is None or L is None or (stack_h is None and (T is None or q is None)):
        return []

    c = s.clearance_mm
    total = stack_h if stack_h is not None else q * T
    room = max_depth - c
    if room <= 0:
        issues.error("too_tall", f"{comp.name}: no vertical room left for contents", comp.name)
        return []
    # One stack unless the plan says otherwise: splitting a deck adds dividers, which is the
    # user's decision, so a stack that is too tall is reported instead of split automatically.
    k = quantity(d.get("stacks"), "stacks", issues, comp.name, required=False) or 1
    per_q = math.ceil(q / k) if q else None
    per_h = per_q * T if (per_q and T and stack_h is None) else total / k
    if per_h > room + 1e-9:
        need = math.ceil(total / room - 1e-9)
        issues.error("too_tall", f"{comp.name}: a {per_h:.1f} mm stack is taller than the {room:.1f} mm available. "
                                 f"Set \"stacks\": {need} (or more) to store it as side-by-side stacks, or reduce "
                                 f"headroom_mm / the quantity.", comp.name, "stacks")
        return []
    out = []
    for i in range(k):
        out.append(Cell(name=comp.name if k == 1 else f"{comp.name} #{i + 1}", component=comp.name,
                        type=comp.type, w=W + 2 * c, l=L + 2 * c, depth=per_h + c, notch="deep",
                        contents={"piece_mm": {"width": W, "length": L, "thickness": T},
                                  "quantity": per_q, "stack_height_mm": round(per_h, 2), "sleeved": sleeved}))
    return out


# ---------------------------------------------------------------------------
# Round tokens / coins: cylindrical wells with finger slots
# ---------------------------------------------------------------------------
def _round_tokens(comp: Compartment, s: Settings, max_depth: float, issues: Issues) -> list[Cell]:
    d = comp.data
    if "required_mm" in d:
        return _required_cell(comp, s, issues, max_depth, scoop=True, growable=True)
    p = dims(d.get("piece_mm"), "piece_mm", ("diameter", "thickness"), issues, comp.name,
             why="measure one token's diameter and thickness")
    q = quantity(d.get("quantity"), "quantity", issues, comp.name)
    if p is None or q is None:
        return []
    dia, t = p["diameter"], p["thickness"]
    c = s.clearance_mm
    per_max = math.floor((max_depth - c) / t + 1e-9)
    if per_max < 1:
        issues.error("too_tall", f"{comp.name}: a {t} mm token does not fit the available height", comp.name)
        return []
    stacks = quantity(d.get("stacks"), "stacks", issues, comp.name, required=False) or math.ceil(q / per_max)
    per = math.ceil(q / stacks)
    if per > per_max:
        issues.error("too_tall", f"{comp.name}: {per} tokens per stack exceed the available height; "
                                 f"increase 'stacks'", comp.name, "stacks")
        return []
    R = dia / 2 + c
    div = s.divider_mm
    rows = 1 if stacks <= 4 else 2
    cols = math.ceil(stacks / rows)
    W = cols * 2 * R + (cols - 1) * div
    L = rows * 2 * R + (rows - 1) * div
    wells, dirs = [], []
    for i in range(stacks):
        r, col = divmod(i, cols)
        wells.append((-W / 2 + R + col * (2 * R + div), -L / 2 + R + r * (2 * R + div)))
        dirs.append(-1 if r == 0 else 1)
    depth = per * t + c
    return [Cell(name=comp.name, component=comp.name, type=comp.type, kind="wells", w=W, l=L, depth=depth,
                 wells=wells, well_r=R, well_dirs=dirs,
                 slot_w=min(max(0.45 * dia, 8.0), 1.4 * R), slot_depth=0.6 * depth,
                 contents={"piece_mm": p, "quantity": q, "stacks": stacks, "per_stack": per})]


# ---------------------------------------------------------------------------
# Dice: scooped bin (default) or one pocket per die
# ---------------------------------------------------------------------------
def _dice(comp: Compartment, s: Settings, max_depth: float, issues: Issues) -> list[Cell]:
    d = comp.data
    if "required_mm" in d:
        return _required_cell(comp, s, issues, max_depth, scoop=True, growable=True)
    piece = d.get("piece_mm")
    e = None
    if isinstance(piece, dict) and piece.get("edge") is not None:
        e = num(piece.get("edge"), "piece_mm.edge", issues, comp.name)
    elif isinstance(piece, dict) and all(piece.get(k) is not None for k in ("width", "length", "height")):
        p = dims(piece, "piece_mm", ("width", "length", "height"), issues, comp.name)
        e = max(p.values()) if p else None
    else:
        issues.missing(comp.name, "piece_mm.edge", "measure one die's edge length (or width/length/height)")
    q = quantity(d.get("quantity"), "quantity", issues, comp.name)
    if e is None or q is None:
        return []
    c = s.clearance_mm
    if e + c > max_depth:
        issues.error("too_tall", f"{comp.name}: a {e} mm die does not fit the available height", comp.name)
        return []
    if d.get("pockets"):
        return [Cell(name=f"{comp.name} #{i + 1}", component=comp.name, type=comp.type,
                     w=e + 2 * c, l=e + 2 * c, depth=e + c, contents={"edge_mm": e, "quantity": 1})
                for i in range(q)]
    levels = max(1, min(2, math.floor((max_depth - c) / e)))
    per_level = math.ceil(q / levels)
    cols = math.ceil(math.sqrt(per_level))
    rows = math.ceil(per_level / cols)
    gap = 0.5
    cell = Cell(name=comp.name, component=comp.name, type=comp.type, growable=True,
                w=cols * e + (cols - 1) * gap + 2 * c, l=rows * e + (rows - 1) * gap + 2 * c,
                depth=levels * e + c, contents={"edge_mm": e, "quantity": q, "levels": levels})
    return [_scooped(cell, s, max_depth)]


# ---------------------------------------------------------------------------
# Bits / irregular pieces (meeples, cubes, minis): scooped bins
# ---------------------------------------------------------------------------
def _bits(comp: Compartment, s: Settings, max_depth: float, issues: Issues) -> list[Cell]:
    d = comp.data
    if "required_mm" in d:
        return _required_cell(comp, s, issues, max_depth, scoop=True, growable=True)
    p = dims(d.get("piece_mm"), "piece_mm", ("width", "length", "height"), issues, comp.name,
             why="measure one piece's bounding box, or give required_mm for the whole pile")
    q = quantity(d.get("quantity"), "quantity", issues, comp.name)
    k = quantity(d.get("cells", 1), "cells", issues, comp.name) or 1
    if p is None or q is None:
        return []
    c = s.clearance_mm
    eff = s.bulk_packing_efficiency
    big = max(p.values())
    small = min(p.values())
    if small + c > max_depth:
        issues.error("too_tall", f"{comp.name}: pieces do not fit the available height in any orientation",
                     comp.name)
        return []
    comp.assumptions.append(
        f"{comp.name}: bin sized from {q} × {p['width']:g}×{p['length']:g}×{p['height']:g} mm bounding boxes "
        f"at {eff:.0%} loose-pile packing efficiency (settings.bulk_packing_efficiency); verify with a test "
        f"pile or give required_mm")
    out = []
    for i in range(k):
        n = math.ceil(q / k)
        vol = n * p["width"] * p["length"] * p["height"] / eff
        content_h = min(max_depth - c, max(min(big, max_depth - c), min(40.0, vol ** (1 / 3))))
        area = vol / content_h
        w = max(math.sqrt(area / 1.3), big)
        l = max(area / w, big)
        cell = Cell(name=comp.name if k == 1 else f"{comp.name} #{i + 1}", component=comp.name, type=comp.type,
                    growable=True, w=w + 2 * c, l=l + 2 * c, depth=content_h + c,
                    contents={"piece_mm": p, "quantity": n, "estimated_volume_mm3": round(vol)})
        out.append(_scooped(cell, s, max_depth))
    return out


def _generic(comp: Compartment, s: Settings, max_depth: float, issues: Issues) -> list[Cell]:
    return _required_cell(comp, s, issues, max_depth, notch="shallow", growable=True)


# ---------------------------------------------------------------------------
# Sets (organizer "boxes"): one plain open box per upstream set
# ---------------------------------------------------------------------------
def _min_enclosing(rects: list, limits: list) -> tuple | None:
    """Smallest W × L rectangle into which all (w, l) rectangles can be laid side by side.

    Uses the MaxRects packer on strips of candidate widths. Returns (W, L, placements)
    with placements [(index, x, y, w, l)], or None if nothing fits within ``limits``.
    """
    from .grouping import fits
    from .packing import maxrects

    lo = max(min(w, l) for w, l in rects)
    hi = max(lo, sum(max(w, l) for w, l in rects))
    n = 80
    widths = sorted({round(lo + (hi - lo) * k / (n - 1), 3) for k in range(n)} |
                    {round(max(w, l), 3) for w, l in rects} | {round(lo, 3)})
    sizes = {i: r for i, r in enumerate(rects)}
    order = sorted(sizes, key=lambda i: -sizes[i][0] * sizes[i][1])
    best = None
    for W in widths:
        for h in ("bl", "bssf"):
            res = maxrects(sizes, W, 1e6, h, order)
            if res is None:
                continue
            used_w = max(x + w for x, y, w, l, _ in res.values())
            L = max(y + l for x, y, w, l, _ in res.values())
            if limits and not all(fits(used_w, L, a, b) for a, b in limits):
                continue
            aspect = max(used_w, L) / min(used_w, L)
            key = (aspect > 2.0, round(used_w * L, 3), aspect)
            if best is None or key < best[0]:
                best = (key, used_w, L, [(i, x, y, w, l) for i, (x, y, w, l, _) in res.items()])
    return None if best is None else best[1:]


SET_STYLES = ("loose", "stack", "card_stack", "tight_pack")
TIGHT_PACK_SHAPES = ("rectangular_prism", "square_prism", "cube")   # same rule as the grouping step


def _set_box(comp: Compartment, s: Settings, max_depth: float, issues: Issues, limits=None) -> list[Cell]:
    """Exactly one rectangular open cavity for all members of a set (no dividers or shaped cutouts)."""
    d = comp.data
    c = s.clearance_mm
    plain = dict(name=comp.name, component=comp.name, type="set", growable=True, deepens=True)
    if d.get("members") is None and "required_mm" in d:
        req = dims(d.get("required_mm"), "required_mm", ("width", "length", "height"), issues, comp.name)
        if req is None:
            return []
        return [Cell(w=req["width"] + 2 * c, l=req["length"] + 2 * c, depth=req["height"] + c,
                     contents={"source": "required_mm", "group_bbox_mm": req}, **plain)]
    style = str(d.get("storage_style", "")).strip().lower()
    if style not in SET_STYLES:
        issues.error("invalid_value", f"{comp.name}: storage_style must be one of {', '.join(SET_STYLES)}, got "
                                      f"{d.get('storage_style')!r}", comp.name, "storage_style")
    members = d.get("members")
    if not isinstance(members, list) or not members:
        issues.missing(comp.name, "members", "list the components in this set with quantity and dimensions_mm")
        return []
    parsed = []
    for j, m in enumerate(members):
        if not isinstance(m, dict):
            issues.error("invalid_value", f"{comp.name}: members[{j}] must be an object", comp.name)
            continue
        mname = str(m.get("name") or m.get("component_id") or f"members[{j}]")
        md = dims(m.get("dimensions_mm"), f"members[{j}].dimensions_mm", ("length", "width", "height"), issues,
                  comp.name, why=f"measure one '{mname}'")
        q = quantity(m.get("quantity"), f"members[{j}].quantity", issues, comp.name)
        if md and q:
            parsed.append({"name": mname, "component_id": m.get("component_id"), "quantity": q,
                           "shape": m.get("shape"), "dimensions_mm": md})
    if len(parsed) != len(members) or style not in SET_STYLES:
        return []
    if style == "tight_pack":
        odd = [p for p in parsed if p["shape"] not in TIGHT_PACK_SHAPES]
        if odd:
            issues.error("invalid_value", f"{comp.name}: storage_style 'tight_pack' needs rectangular prisms, square "
                                          f"prisms or cubes, but " + ", ".join(f"'{p['name']}' is a {p['shape']}"
                                                                               for p in odd)
                                          + ". Use 'loose' for these pieces.", comp.name, "storage_style")
            return []
    room = max_depth - c

    if style in ("stack", "card_stack"):
        # One vertical stack (card_stack: one deck lying flat); height is the piece/card thickness.
        what = "deck" if style == "card_stack" else "stack"
        total = sum(p["quantity"] * p["dimensions_mm"]["height"] for p in parsed)
        W = max(p["dimensions_mm"]["width"] for p in parsed)
        L = max(p["dimensions_mm"]["length"] for p in parsed)
        if len({(p["dimensions_mm"]["width"], p["dimensions_mm"]["length"]) for p in parsed}) > 1:
            issues.warn("mixed_stack", f"{comp.name}: stacked members have different footprints; the box is sized "
                                       f"to the largest ({W:g} × {L:g} mm)", comp.name)
        if style == "card_stack":
            for p in parsed:
                md = p["dimensions_mm"]
                if md["height"] > min(md["length"], md["width"]):
                    issues.warn("card_thickness", f"{comp.name}: '{p['name']}' height {md['height']:g} mm is used "
                                                  f"as the card thickness but is not its smallest dimension",
                                comp.name)
        if total > room + 1e-9:
            issues.error("too_tall", f"{comp.name}: the {total:g} mm {what} is taller than the {room:.1f} mm "
                                     f"available. storage_style '{style}' is never split automatically; change the "
                                     f"grouping (e.g. two sets) or the box/headroom measurements.",
                         comp.name, "storage_style")
            return []
        basis = ("one deck lying flat: " + " + ".join(f"{p['quantity']} × {p['dimensions_mm']['height']:g} mm"
                                                      for p in parsed)
                 if style == "card_stack" else "one vertical stack of all listed pieces")
        return [Cell(w=W + 2 * c, l=L + 2 * c, depth=total + c,
                     contents={"storage_style": style, "members": parsed, "stack_height_mm": round(total, 3),
                               "fit_basis": basis}, **plain)]

    # loose: every piece's bounding box laid out in one arranged layer (or the fewest identical layers).
    # tight_pack: block-like pieces placed by hand in identical grid layers; as many layers as give the
    # smallest footprint (a compact block), since they are stacked on purpose rather than poured.
    pieces, notes = [], []
    for p in parsed:
        dl, dw, dh = p["dimensions_mm"]["length"], p["dimensions_mm"]["width"], p["dimensions_mm"]["height"]
        if dh > room + 1e-9:  # too tall standing up: lay it on its side
            dl, dw, dh = sorted((dl, dw, dh), reverse=True)
            notes.append(f"{p['name']} laid on its side ({dh:g} mm high)")
            if dh > room + 1e-9:
                issues.error("too_tall", f"{comp.name}: '{p['name']}' does not fit the {room:.1f} mm available "
                                         f"in any orientation", comp.name)
                return []
        pieces.append((p, dl, dw, dh))
    h_layer = max(dh for _, _, _, dh in pieces)
    inner_limits = [(a - 2 * (c + s.wall_mm), b - 2 * (c + s.wall_mm)) for a, b in (limits or [])]
    if style == "loose" and s.loose_piles and len(parsed) > 1:   # mixed sets only (e.g. a board and its figure)
        piled = _piled_layers(pieces, room, inner_limits)
        if piled:
            k, W, L, height = piled
            comp.assumptions.append(
                f"{comp.name}: loose box sized for its pieces piled in {k} layer(s) of at most {W:.1f} × {L:.1f} mm, "
                f"largest pieces at the bottom ({height:.1f} mm in all); poured in at random they may need "
                f"settling. Physical fit is not verified.")
            return [Cell(w=W + 2 * c, l=L + 2 * c, depth=height + c,
                         contents={"storage_style": style, "members": parsed, "layers": k,
                                   "fit_basis": f"pieces piled in {k} layer(s), largest at the bottom"
                                                + (f"; {'; '.join(notes)}" if notes else "")}, **plain)]
    best = None
    for k in range(1, max(1, math.floor(room / h_layer + 1e-9)) + 1):
        rects, owners = [], []
        for p, dl, dw, _ in pieces:
            n = math.ceil(p["quantity"] / k)
            rects += [(dw, dl)] * n
            owners += [p["name"]] * n
        found = _min_enclosing(rects, inner_limits)
        if not found:
            continue
        if best is None or found[0] * found[1] < best[1][0] * best[1][1] - 1e-6:
            best = (k, found, owners)
        if style == "loose":
            break
    if not best:
        issues.error("too_large", f"{comp.name}: the pieces cannot be laid out in a box that fits the game box "
                                  f"and printer bed", comp.name)
        return []
    k, (W, L, placed), owners = best
    arrangement = [{"member": owners[i], "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "l": round(l, 2)}
                   for i, x, y, w, l in placed]
    if style == "tight_pack":
        basis = f"bounding boxes arranged without overlap, {k} identical layer(s) placed by hand"
        comp.assumptions.append(
            f"{comp.name}: tight_pack box sized for the pieces placed by hand in {k} layer(s) of the arranged grid "
            f"({W:.1f} × {L:.1f} mm per layer); poured in at random they may not fit. Physical fit is not verified.")
    else:
        basis = "bounding boxes arranged without overlap"
        comp.assumptions.append(
            f"{comp.name}: loose box sized so every piece's bounding box fits in {k} arranged layer(s) of "
            f"{W:.1f} × {L:.1f} mm; pieces poured in at random may need settling. Physical fit is not verified.")
    return [Cell(w=W + 2 * c, l=L + 2 * c, depth=k * h_layer + c,
                 contents={"storage_style": style, "members": parsed, "layers": k,
                           "fit_basis": basis + (f"; {'; '.join(notes)}" if notes else ""),
                           "arrangement_mm": arrangement}, **plain)]


def _piled_layers(pieces: list, room: float, limits: list) -> tuple | None:
    """Smallest footprint for a loose set whose pieces may be piled in layers that differ.

    Pieces go into k layers, largest first, each into the layer holding the least area so far; a layer is
    as tall as its tallest piece and all layers together must fit in ``room``. Returns (k, W, L, height)
    for the k with the smallest footprint (fewest layers on a tie), or None.
    """
    units = sorted(((dl, dw, dh) for p, dl, dw, dh in pieces for _ in range(p["quantity"])),
                   key=lambda u: -u[0] * u[1])
    best = None
    for k in range(1, len(units) + 1):
        layers, area = [[] for _ in range(k)], [0.0] * k
        for u in units:
            j = min(range(k), key=lambda i: area[i])
            layers[j].append(u)
            area[j] += u[0] * u[1]
        height = sum(max(u[2] for u in layer) for layer in layers)
        if height > room + 1e-9:
            break
        sizes = []
        for layer in layers:
            found = _min_enclosing([(dw, dl) for dl, dw, _ in layer], limits)
            if not found:
                break
            sizes.append(sorted(found[:2]))
        else:
            W, L = max(sz[0] for sz in sizes), max(sz[1] for sz in sizes)
            if best is None or W * L < best[1] * best[2] - 1e-6:
                best = (k, W, L, height)
    return best


DESIGNERS = {
    "cards": _stack_cells, "tiles": _stack_cells, "round_tokens": _round_tokens,
    "dice": _dice, "bits": _bits, "generic": _generic,
}


def design_compartment(comp: Compartment, s: Settings, max_depth: float, issues: Issues,
                       limits=None) -> list[Cell]:
    """Return the cells for a compartment (empty list if it has errors).

    ``limits``: [(W, L), ...] outer footprints a tray must fit (box floor, printer bed).
    """
    before = len(issues.errors)
    if comp.type == "set":
        cells = _set_box(comp, s, max_depth, issues, limits)
        return cells if len(issues.errors) == before else []
    if comp.type in ("cards", "tiles") and comp.data.get("required_mm") is None and comp.data.get("cells"):
        issues.warn("ignored", f"{comp.name}: 'cells' applies to bins; use 'stacks' for cards/tiles", comp.name)
    cells = DESIGNERS[comp.type](comp, s, max_depth, issues)
    for cell in cells:
        if cell.depth > max_depth + 1e-6:
            issues.error("too_tall", f"{comp.name}: needs {cell.depth + s.floor_mm:.1f} mm of tray height but "
                                     f"only {max_depth + s.floor_mm:.1f} mm is available (box height minus "
                                     f"headroom, or printer Z)", comp.name)
            break
    return cells if len(issues.errors) == before else []
