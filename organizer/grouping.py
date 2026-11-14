"""Grouping compartments into trays and laying cells out inside a tray.

Compartments that share a ``"tray"`` name become one tray whose cells are
separated by divider walls. Cells are arranged in shelf rows; the arrangement
with the smallest footprint that fits both the printer bed and the box wins.

``place_cells`` maps the arrangement onto the *final* tray size (which may be
larger than the minimum after fill-box expansion): bins grow, fixed cavities
(cards, tiles, token wells, die pockets) keep their exact size and either stay
centred, with solid wall around them. Dividers only ever separate cells the plan
asked for (several compartments in one ``tray``, ``stacks``, ``cells``, dice ``pockets``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .components import Cell
from .schema import Issues, Plan, PlanError, Settings, slug

EPS = 1e-6


def fits(w: float, l: float, W: float, L: float) -> bool:
    """True if a w×l rectangle fits a W×L area, allowing a 90° turn."""
    return (w <= W + EPS and l <= L + EPS) or (l <= W + EPS and w <= L + EPS)


@dataclass
class Arrangement:
    rows: list                 # [[(cell_index, rotated), ...], ...] from -y to +y
    inner_w: float
    inner_l: float


@dataclass
class TrayDesign:
    id: int
    name: str
    filename: str
    compartments: list
    cells: list
    arrangement: Arrangement
    wall: float
    floor: float
    layer_pref: str = "any"

    @property
    def min_w(self) -> float:
        return self.arrangement.inner_w + 2 * self.wall

    @property
    def min_l(self) -> float:
        return self.arrangement.inner_l + 2 * self.wall

    @property
    def min_h(self) -> float:
        return max(c.depth for c in self.cells) + self.floor

    @property
    def type(self) -> str:
        types = {c.type for c in self.cells}
        return types.pop() if len(types) == 1 else "mixed"


def _cell_dims(cell: Cell, rot: bool) -> tuple[float, float]:
    return (cell.l, cell.w) if rot else (cell.w, cell.l)


def _arrangement(cells: list[Cell], rows_idx: list[list[int]], rot: bool, div: float) -> Arrangement:
    rows = [[(i, rot) for i in r] for r in rows_idx]
    widths = [sum(_cell_dims(cells[i], rot)[0] for i in r) + div * (len(r) - 1) for r in rows_idx]
    heights = [max(_cell_dims(cells[i], rot)[1] for i in r) for r in rows_idx]
    return Arrangement(rows, max(widths), sum(heights) + div * (len(rows_idx) - 1))


def _balanced_rows(n: int, k: int) -> list[list[int]]:
    base, extra = divmod(n, k)
    out, i = [], 0
    for r in range(k):
        size = base + (1 if r < extra else 0)
        out.append(list(range(i, i + size)))
        i += size
    return out


def best_arrangement(cells: list[Cell], s: Settings, limits: list[tuple[float, float]]) -> Optional[Arrangement]:
    """Smallest shelf arrangement whose tray (inner + walls) fits every (W, L) limit."""
    div = s.divider_mm
    cands = []
    if len(cells) == 1:
        cands.append(_arrangement(cells, [[0]], False, div))
    else:
        for rot in (False, True):
            for k in range(1, len(cells) + 1):
                cands.append(_arrangement(cells, _balanced_rows(len(cells), k), rot, div))
    ok = [a for a in cands
          if all(fits(a.inner_w + 2 * s.wall_mm, a.inner_l + 2 * s.wall_mm, W, L) for W, L in limits)]
    if not ok:
        return None
    return min(ok, key=lambda a: (round(a.inner_w * a.inner_l, 3), max(a.inner_w, a.inner_l)))


def build_tray_designs(plan: Plan) -> list[TrayDesign]:
    s = plan.settings
    groups: dict[str, list] = {}
    order: list[str] = []
    for comp in plan.compartments:
        key = comp.tray or f"\0{comp.name}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(comp)

    issues = plan.issues
    bin_w, bin_l = plan.bin_size
    limits = [(plan.bed[0], plan.bed[1]), (bin_w, bin_l)]
    designs = []
    failed = False
    for idx, key in enumerate(order, 1):
        comps = groups[key]
        name = comps[0].name if key.startswith("\0") else key
        cells = [cell for comp in comps for cell in comp.cells]
        prefs = {c.layer_pref for c in comps} - {"any"}
        if len(prefs) > 1:
            issues.warn("layer_conflict", f"{name}: compartments ask for different layers; using 'any'", name)
        pref = prefs.pop() if len(prefs) == 1 else "any"
        arr = best_arrangement(cells, s, limits)
        if arr is None:
            single = _arrangement(cells, [[i] for i in range(len(cells))], False, s.divider_mm)
            w = min((_arrangement(cells, [list(range(len(cells)))], False, s.divider_mm), single),
                    key=lambda a: a.inner_w * a.inner_l)
            issues.error("too_large", f"{name}: the smallest tray ({w.inner_w + 2 * s.wall_mm:.1f} × "
                                      f"{w.inner_l + 2 * s.wall_mm:.1f} mm) does not fit the printer bed "
                                      f"{plan.bed[0]:g} × {plan.bed[1]:g} mm and/or the box floor "
                                      f"{bin_w:.1f} × {bin_l:.1f} mm. Split it into smaller compartments.",
                         name)
            failed = True
            continue
        designs.append(TrayDesign(id=idx, name=name, filename=f"{idx:02d}_{slug(name)}.stl",
                                  compartments=[c.name for c in comps], cells=cells, arrangement=arr,
                                  wall=s.wall_mm, floor=s.floor_mm, layer_pref=pref))
    if failed:
        raise PlanError(issues)
    return designs


# ---------------------------------------------------------------------------
# Cell placement inside a tray of final size
# ---------------------------------------------------------------------------
_DIR_SIDE = {(1, 0): "+x", (-1, 0): "-x", (0, 1): "+y", (0, -1): "-y"}


@dataclass
class CellGeom:
    name: str
    component: str
    type: str
    kind: str
    slot: tuple                 # (x0, y0, x1, y1) region reserved for this cell
    cx: float                   # cavity centre (tray frame, origin = tray centre)
    cy: float
    w: float                    # cavity size along tray x / y
    l: float
    depth: float
    rot: bool
    sides: set                  # tray sides reachable without crossing another cell
    scoop_r: float = 0.0
    notch: str = "none"
    notch_sides: list = field(default_factory=list)
    wells: list = field(default_factory=list)       # [(x, y)] tray frame
    well_r: float = 0.0
    slot_dirs: list = field(default_factory=list)   # [(dx, dy) or None] per well
    slot_w: float = 0.0
    slot_depth: float = 0.0
    growable: bool = False
    contents: dict = field(default_factory=dict)


def _distribute(sizes: list[float], extra: float, targets: list[int]) -> list[float]:
    if extra < -1e-4:
        raise ValueError(f"internal error: cells overflow the tray by {-extra:.3f} mm")
    extra = max(extra, 0.0)
    out = list(sizes)
    for j in targets:
        out[j] += extra / len(targets)
    return out


def _local_to_tray(u, v, rot):
    return (-v, u) if rot else (u, v)


def place_cells(design: TrayDesign, W: float, L: float, H: float, s: Settings) -> list[CellGeom]:
    """Lay the design's cells out inside a tray of outer size W × L × H (tray-local frame)."""
    div = s.divider_mm
    iw, il = W - 2 * design.wall, L - 2 * design.wall
    rows = design.arrangement.rows
    cells = design.cells
    row_h = [max(_cell_dims(cells[i], r)[1] for i, r in row) for row in rows]
    grow_rows = [k for k, row in enumerate(rows) if any(cells[i].growable for i, _ in row)]
    row_h = _distribute(row_h, il - sum(row_h) - div * (len(rows) - 1), grow_rows or list(range(len(rows))))
    out: list[CellGeom] = []
    y = -il / 2
    for k, row in enumerate(rows):
        widths = [_cell_dims(cells[i], r)[0] for i, r in row]
        grow = [j for j, (i, _) in enumerate(row) if cells[i].growable]
        widths = _distribute(widths, iw - sum(widths) - div * (len(row) - 1), grow or list(range(len(row))))
        x = -iw / 2
        for j, (i, rot) in enumerate(row):
            sides = set()
            if k == 0:
                sides.add("-y")
            if k == len(rows) - 1:
                sides.add("+y")
            if j == 0:
                sides.add("-x")
            if j == len(row) - 1:
                sides.add("+x")
            out.append(_cell_geom(cells[i], rot, (x, y, x + widths[j], y + row_h[k]), sides))
            x += widths[j] + div
        y += row_h[k] + div
    return out


def _cell_geom(cell: Cell, rot: bool, slot: tuple, sides: set) -> CellGeom:
    x0, y0, x1, y1 = slot
    cw, cl = _cell_dims(cell, rot)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # Extra room in the slot (fill_box): a bin's cavity takes the whole slot; a fixed cavity
    # (cards, tiles, token wells, die pockets) keeps its size and is centred, leaving solid
    # wall around it. No extra pockets or dividers are ever added here.
    w, l = (x1 - x0, y1 - y0) if cell.growable else (cw, cl)
    g = CellGeom(name=cell.name, component=cell.component, type=cell.type, kind=cell.kind, slot=slot,
                 cx=cx, cy=cy, w=w, l=l, depth=cell.depth, rot=rot, sides=set(sides), scoop_r=cell.scoop_r,
                 notch=cell.notch, growable=cell.growable, contents=cell.contents)

    # finger access decisions (geometry is cut in cad.py)
    if g.kind == "wells":
        g.well_r = cell.well_r
        g.slot_w = cell.slot_w
        g.slot_depth = cell.slot_depth
        single_row = len({round(v, 6) for _, v in cell.wells}) == 1
        for (u, v), dv in zip(cell.wells, cell.well_dirs):
            du, dvv = _local_to_tray(u, v, rot)
            g.wells.append((g.cx + du, g.cy + dvv))
            d = _local_to_tray(0, dv, rot)
            d = (int(round(d[0])), int(round(d[1])))
            if _DIR_SIDE[d] not in g.sides:
                opp = (-d[0], -d[1])
                d = opp if (single_row and _DIR_SIDE[opp] in g.sides) else None
            g.slot_dirs.append(d)
    if g.notch == "deep":
        pair, other = (["-x", "+x"], ["-y", "+y"]) if g.w <= g.l else (["-y", "+y"], ["-x", "+x"])
        g.notch_sides = [p for p in pair if p in g.sides] or [p for p in other if p in g.sides]
    elif g.notch == "shallow":
        g.notch_sides = [p for p in ("-y", "+y", "-x", "+x") if p in g.sides][:1]
    return g
