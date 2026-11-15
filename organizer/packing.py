"""Spatial optimisation: where every tray goes inside the game box.

1. Try to fit every tray in ONE layer (several MaxRects heuristics × sort
   orders, plus rectpack's packers when installed; best candidate wins).
2. If that fails, stack trays into *columns*: two columns are merged when their
   heights fit the box and the merge saves the most floor area. Trays in a
   column share one footprint, so every upper tray is fully supported.
3. With ``fill_box`` the placed footprints grow into the free floor space (no
   rattling); trays that hold the same thing always keep the same size (they
   grow together); with ``fill_height`` the top tray of each column grows to the
   usable box height (flush surface under the lid/board).

Everything here is pure Python so it can be tested without CadQuery.
"""
from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from .grouping import TrayDesign, fits
from .schema import Plan, PlanError

EPS = 1e-6
HEURISTICS = ("bssf", "blsf", "baf", "bl", "cp")
ORDERS = ("area", "long", "perimeter", "short", "width", "length", "input")


# ---------------------------------------------------------------------------
# 2-D MaxRects (rotation allowed)
# ---------------------------------------------------------------------------
def _intersects(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax >= bx + bw - EPS or ax + aw <= bx + EPS or ay >= by + bh - EPS or ay + ah <= by + EPS)


def _contained(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax >= bx - EPS and ay >= by - EPS and ax + aw <= bx + bw + EPS and ay + ah <= by + bh + EPS


def _split_free(free, placed):
    out = []
    px, py, pw, ph = placed
    for fr in free:
        if not _intersects(fr, placed):
            out.append(fr)
            continue
        x, y, w, h = fr
        if px > x + EPS:
            out.append((x, y, px - x, h))
        if px + pw < x + w - EPS:
            out.append((px + pw, y, x + w - px - pw, h))
        if py > y + EPS:
            out.append((x, y, w, py - y))
        if py + ph < y + h - EPS:
            out.append((x, py + ph, w, y + h - py - ph))
    uniq = []
    for r in out:
        if r[2] > EPS and r[3] > EPS and not any(all(abs(a - b) < EPS for a, b in zip(r, u)) for u in uniq):
            uniq.append(r)
    return [r for j, r in enumerate(uniq) if not any(j != k and _contained(r, b) for k, b in enumerate(uniq))]


def _contact(x, y, w, h, used, bin_w, bin_h):
    score = 0.0
    if x <= EPS or x + w >= bin_w - EPS:
        score += h
    if y <= EPS or y + h >= bin_h - EPS:
        score += w
    for ux, uy, uw, uh in used:
        if abs(ux - (x + w)) < EPS or abs(ux + uw - x) < EPS:
            score += max(0.0, min(y + h, uy + uh) - max(y, uy))
        if abs(uy - (y + h)) < EPS or abs(uy + uh - y) < EPS:
            score += max(0.0, min(x + w, ux + uw) - max(x, ux))
    return score


def maxrects(sizes: dict, bin_w: float, bin_l: float, heuristic: str = "bssf",
             order: Optional[list] = None) -> Optional[dict]:
    """Place rectangles {key: (w, l)}; returns {key: (x, y, w, l, rot)} or None if one does not fit."""
    free = [(0.0, 0.0, bin_w, bin_l)]
    used = []
    placed = {}
    for key in (order or list(sizes)):
        w, l = sizes[key]
        best = None
        for fx, fy, fw, fh in free:
            for rot, (rw, rh) in ((0, (w, l)), (90, (l, w))):
                if rot == 90 and abs(w - l) < EPS:
                    continue
                if rw > fw + EPS or rh > fh + EPS:
                    continue
                lw, lh = fw - rw, fh - rh
                if heuristic == "bssf":
                    score = (min(lw, lh), max(lw, lh), fy, fx)
                elif heuristic == "blsf":
                    score = (max(lw, lh), min(lw, lh), fy, fx)
                elif heuristic == "baf":
                    score = (fw * fh - rw * rh, min(lw, lh), fy, fx)
                elif heuristic == "bl":
                    score = (fy + rh, fx, 0, 0)
                else:  # contact point
                    score = (-_contact(fx, fy, rw, rh, used, bin_w, bin_l), fy, fx, 0)
                if best is None or score < best[0]:
                    best = (score, (fx, fy, rw, rh), rot)
        if best is None:
            return None
        rect, rot = best[1], best[2]
        placed[key] = (*rect, rot)
        used.append(rect)
        free = _split_free(free, rect)
    return placed


def _orders(sizes: dict) -> dict:
    keys = list(sizes)
    w = {k: sizes[k][0] for k in keys}
    l = {k: sizes[k][1] for k in keys}
    return {
        "area": sorted(keys, key=lambda k: -w[k] * l[k]),
        "long": sorted(keys, key=lambda k: (-max(w[k], l[k]), -min(w[k], l[k]))),
        "perimeter": sorted(keys, key=lambda k: -(w[k] + l[k])),
        "short": sorted(keys, key=lambda k: (-min(w[k], l[k]), -max(w[k], l[k]))),
        "width": sorted(keys, key=lambda k: -w[k]),
        "length": sorted(keys, key=lambda k: -l[k]),
        "input": keys,
    }


def _rectpack_candidates(sizes: dict, bin_w: float, bin_l: float) -> list:
    try:
        import rectpack
    except ImportError:
        return []
    scale = 10  # 0.1 mm grid; rectangles rounded up, bin rounded down → never overlap in real units
    algos = [("MaxRectsBssf", rectpack.MaxRectsBssf), ("MaxRectsBaf", rectpack.MaxRectsBaf),
             ("MaxRectsBlsf", rectpack.MaxRectsBlsf), ("MaxRectsBl", rectpack.MaxRectsBl),
             ("SkylineBl", rectpack.SkylineBl), ("GuillotineBssfSas", rectpack.GuillotineBssfSas)]
    out = []
    keys = list(sizes)
    for name, algo in algos:
        p = rectpack.newPacker(pack_algo=algo, rotation=True, sort_algo=rectpack.SORT_AREA)
        ints = {}
        for k in keys:
            iw, il = math.ceil(sizes[k][0] * scale - 1e-6), math.ceil(sizes[k][1] * scale - 1e-6)
            ints[k] = (iw, il)
            p.add_rect(iw, il, rid=keys.index(k))
        p.add_bin(math.floor(bin_w * scale + 1e-6), math.floor(bin_l * scale + 1e-6))
        p.pack()
        rects = p.rect_list()
        if len(rects) != len(keys):
            continue
        res = {}
        for _, x, y, w, h, rid in rects:
            k = keys[rid]
            rot = 0 if (w, h) == ints[k] else 90
            rw, rl = sizes[k] if rot == 0 else (sizes[k][1], sizes[k][0])
            res[k] = (x / scale, y / scale, rw, rl, rot)
        out.append((f"rectpack_{name}", res))
    return out


def valid_placement(res: dict, bin_w: float, bin_l: float) -> bool:
    rects = [(x, y, w, l) for x, y, w, l, _ in res.values()]
    for x, y, w, l in rects:
        if x < -EPS or y < -EPS or x + w > bin_w + 1e-4 or y + l > bin_l + 1e-4:
            return False
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _intersects(rects[i], rects[j]):
                return False
    return True


# ---------------------------------------------------------------------------
# Fill-box expansion
# ---------------------------------------------------------------------------
def expand(rects: dict, bin_w: float, bin_l: float, cap=None, rounds: int = 2) -> dict:
    """Grow rectangles {k: [x, y, w, l]} into free space without overlaps.

    ``cap(k, w, l, axis)`` returns the largest allowed size along ``axis``.
    """
    r = {k: list(v) for k, v in rects.items()}
    bins = (bin_w, bin_l)
    for _ in range(rounds):
        for axis in (0, 1):
            other = 1 - axis
            for sign in (1, -1):
                keys = sorted(r, key=(lambda k: -(r[k][axis] + r[k][axis + 2])) if sign > 0
                              else (lambda k: r[k][axis]))
                for k in keys:
                    pos, size = r[k][axis], r[k][axis + 2]
                    lo, hi = r[k][other], r[k][other] + r[k][other + 2]
                    limit = (bins[axis] - (pos + size)) if sign > 0 else pos
                    for j, o in r.items():
                        if j == k or o[other] >= hi - EPS or o[other] + o[other + 2] <= lo + EPS:
                            continue
                        if sign > 0 and o[axis] >= pos + size - EPS:
                            limit = min(limit, o[axis] - (pos + size))
                        elif sign < 0 and o[axis] + o[axis + 2] <= pos + EPS:
                            limit = min(limit, pos - (o[axis] + o[axis + 2]))
                    if cap is not None:
                        limit = min(limit, cap(k, r[k][2], r[k][3], axis) - size)
                    if limit > 1e-3:
                        r[k][axis + 2] += limit
                        if sign < 0:
                            r[k][axis] -= limit
    return r


# ---------------------------------------------------------------------------
# Columns (stacked trays sharing one footprint)
# ---------------------------------------------------------------------------
@dataclass
class Column:
    trays: list                 # TrayDesign, bottom → top (ordered later)
    rel_rot: list               # tray turned 90° relative to the column frame
    w: float
    l: float

    @property
    def h(self) -> float:
        return sum(t.min_h for t in self.trays)

    @property
    def name(self) -> str:
        return " + ".join(t.name for t in self.trays)


def _merge(a: Column, b: Column, flip: bool) -> Column:
    bw, bl = (b.l, b.w) if flip else (b.w, b.l)
    return Column(a.trays + b.trays, a.rel_rot + [r != flip for r in b.rel_rot], max(a.w, bw), max(a.l, bl))


def _best_merge(cols: list[Column], max_h: float, max_layers: int, gap: float, limits) -> Optional[tuple]:
    best = None
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            if len(a.trays) + len(b.trays) > max_layers or a.h + b.h > max_h + EPS:
                continue
            for flip in (False, True):
                m = _merge(a, b, flip)
                if not all(fits(m.w, m.l, W, L) for W, L in limits):
                    continue
                saving = (a.w + gap) * (a.l + gap) + (b.w + gap) * (b.l + gap) - (m.w + gap) * (m.l + gap)
                waste = 2 * m.w * m.l - a.w * a.l - b.w * b.l
                key = (round(saving, 6), -waste)
                if saving > EPS and (best is None or key > best[0]):
                    best = (key, i, j, m)
    return best


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class PlacedTray:
    design: TrayDesign
    x: float                    # footprint lower-left in box coordinates
    y: float
    z: float
    rot: int                    # 0 or 90 (counter-clockwise about +z)
    w: float                    # outer size in the tray's own (STL) frame
    l: float
    h: float
    layer: int
    column: int

    @property
    def footprint(self) -> tuple[float, float]:
        return (self.w, self.l) if self.rot == 0 else (self.l, self.w)

    @property
    def center(self) -> tuple[float, float]:
        fw, fl = self.footprint
        return self.x + fw / 2, self.y + fl / 2


@dataclass
class PackResult:
    trays: list
    method: str
    layers: int
    notes: list = field(default_factory=list)
    min_area_mm2: float = 0.0


def scale_to_fill(rects: dict, bin_w: float, bin_l: float, cap=None) -> dict:
    """Stretch a compact layout to the bin (affine scaling keeps it overlap-free).

    Every tray grows in proportion, instead of the trays on the edge of the
    layout absorbing all the free space.
    """
    ext_w = max(v[0] + v[2] for v in rects.values())
    ext_l = max(v[1] + v[3] for v in rects.values())
    fx, fy = bin_w / ext_w, bin_l / ext_l
    if cap is not None:
        for k, (_, _, w, l) in rects.items():
            fx = min(fx, cap(k, w, l, 0) / w)
        for k, (_, _, w, l) in rects.items():
            fy = min(fy, cap(k, w * fx, l, 1) / l)
    fx, fy = max(1.0, fx), max(1.0, fy)
    return {k: [x * fx, y * fy, w * fx, l * fy] for k, (x, y, w, l) in rects.items()}


# ---------------------------------------------------------------------------
# Identical trays stay identical
# ---------------------------------------------------------------------------
def _design_sig(d: TrayDesign) -> tuple:
    """What a tray physically has to hold: two trays with the same signature get the same size."""
    cells = tuple((c.type, c.kind, round(c.w, 3), round(c.l, 3), round(c.depth, 3), c.growable, c.deepens,
                   c.notch, round(c.scoop_r, 3), len(c.wells)) for c in d.cells)
    return round(d.min_w, 3), round(d.min_l, 3), round(d.min_h, 3), cells


def twin_groups(cols: list) -> list[list[int]]:
    """Indices of columns that must be the same size: identical contents (e.g. one bus set per player)
    or one similar-name group (``TrayDesign.size_group``)."""
    sig: dict = {}
    for i, c in enumerate(cols):
        key = (round(c.w, 3), round(c.l, 3),
               tuple(("group", t.size_group) if t.size_group else (_design_sig(t), r)
                     for t, r in zip(c.trays, c.rel_rot)))
        sig.setdefault(key, []).append(i)
    return [g for g in sig.values() if len(g) > 1]


# Words that do not say what a set is: colours, filler words (numbers and brackets are dropped too)
_NAME_NOISE = {"red", "blue", "green", "yellow", "black", "white", "orange", "purple", "pink", "grey", "gray",
               "brown", "and", "the", "of", "for", "a", "an", "with"}


def name_key(name: str) -> tuple:
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name.lower())
    return tuple(w for w in re.findall(r"[a-z]+", name) if w not in _NAME_NOISE)


def similar_names(a: str, b: str) -> bool:
    """Same name apart from numbers/colours/brackets, or the same first or last two words.

    'Player 1 Bus Set' ~ 'Player 2 Bus Set', 'Transportation Card Deck' ~ 'Destination Ticket Card Deck',
    'Shared Supply: Dice' ~ 'Shared Supply: Battle Tokens'.
    """
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    return ka == kb or (min(len(ka), len(kb)) >= 2 and (ka[:2] == kb[:2] or ka[-2:] == kb[-2:]))


def similar_name_groups(designs: list) -> list[list]:
    parent = list(range(len(designs)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(designs)):
        for j in range(i + 1, len(designs)):
            if similar_names(designs[i].name, designs[j].name):
                parent[root(j)] = root(i)
    groups: dict = {}
    for i, d in enumerate(designs):
        groups.setdefault(root(i), []).append(d)
    return [g for g in groups.values() if len(g) > 1]


def _group_label(group: list) -> str:
    """The words the names share, e.g. 'card deck' for 'Transportation/Destination Ticket Card Deck'."""
    keys = [name_key(d.name) for d in group]
    n = min(len(k) for k in keys)
    pre = next((i for i in range(n) if len({k[i] for k in keys}) > 1), n)
    suf = next((i for i in range(n) if len({k[-1 - i] for k in keys}) > 1), n)
    words = keys[0][:pre] if pre >= suf else keys[0][len(keys[0]) - suf:]
    return " ".join(words) or group[0].name


def share_similar_sizes(designs: list, plan: Plan) -> list[str]:
    """organizer "boxes": similarly named sets get one box size and orientation (the largest each needs)."""
    s = plan.settings
    if plan.mode != "boxes" or not s.similar_names_same_size:
        return []
    notes = []
    limits = [(plan.bed[0], plan.bed[1]), plan.bin_size]
    for group in similar_name_groups(designs):
        short = max(min(d.content_min_w, d.content_min_l) for d in group)
        long = max(max(d.content_min_w, d.content_min_l) for d in group)
        h = max(d.content_min_h for d in group)
        names = ", ".join(f"'{d.name}'" for d in group)
        if not all(fits(short, long, W, L) for W, L in limits) or h > plan.max_tray_height + EPS:
            plan.issues.warn("similar_size_skipped", f"Similarly named sets {names} cannot share one box size "
                                                     f"({short:.1f} × {long:.1f} × {h:.1f} mm does not fit); "
                                                     f"each keeps its own size")
            continue
        label = _group_label(group)
        for d in group:
            d.size_floor = (short, long, h) if d.content_min_w <= d.content_min_l else (long, short, h)
            d.size_group = label
        notes.append(f"Similar names, same box shape and size (at least {short:.1f} × {long:.1f} × {h:.1f} mm, "
                     f"what the largest needs): {names}")
    return notes


def _own(r, rot) -> tuple:
    return (r[2], r[3]) if rot == 0 else (r[3], r[2])


def _try_grow(rect, axis, step, others, bins, cap_size) -> bool:
    """Grow ``rect`` by ``step`` along ``axis`` on the + side, else the − side, if nothing is in the way."""
    if rect[axis + 2] + step > cap_size + EPS:
        return False
    for sign in (1, -1):
        cand = list(rect)
        cand[axis + 2] += step
        if sign < 0:
            cand[axis] -= step
        if cand[axis] < -EPS or cand[axis] + cand[axis + 2] > bins[axis] + EPS:
            continue
        if any(_intersects(tuple(cand), tuple(o)) for o in others):
            continue
        rect[:] = cand
        return True
    return False


def equalize(rects: dict, rots: dict, groups: list, bin_w: float, bin_l: float, cap=None) -> dict:
    """Make every tray of a twin group the same size, then let groups grow together.

    Each group is first shrunk to its smallest member (in the trays' own frame, so a rotated
    twin counts too); then, in decreasing steps, a group grows only if ALL its members can grow
    by the same amount, and single trays fill whatever is left. Shrinking and growing into free
    space never creates an overlap.
    """
    r = {k: list(v) for k, v in rects.items()}
    for g in groups:
        ow = min(_own(r[k], rots[k])[0] for k in g)
        ol = min(_own(r[k], rots[k])[1] for k in g)
        for k in g:
            r[k][2], r[k][3] = (ow, ol) if rots[k] == 0 else (ol, ow)
    in_group = {k for g in groups for k in g}
    units = [list(g) for g in groups] + [[k] for k in r if k not in in_group]
    bins = (bin_w, bin_l)
    for step in (16.0, 8.0, 4.0, 2.0, 1.0, 0.5, 0.25, 0.1):
        changed = True
        while changed:
            changed = False
            for unit in units:
                for own_axis in (0, 1):
                    trial = {k: list(r[k]) for k in unit}
                    ok = True
                    for k in unit:
                        axis = own_axis if rots[k] == 0 else 1 - own_axis
                        others = [trial[j] if j in trial else r[j] for j in r if j != k]
                        limit = cap(k, r[k][2], r[k][3], axis) if cap is not None else math.inf
                        if not _try_grow(trial[k], axis, step, others, bins, limit):
                            ok = False
                            break
                    if ok:
                        r.update(trial)
                        changed = True
    return r


# ---------------------------------------------------------------------------
# Boxes mode: one block per set type, the blocks tile the whole floor
# ---------------------------------------------------------------------------
DISTINCT_RATIO = 1.25   # different set types: ≥ 25 % apart in width or length
MAX_BLOCKS = 5          # exhaustive guillotine search up to this many set types
MAX_ASPECT = 2.5        # preferred limit on box length / width


def _subset_trees(blocks: tuple, leaf_opts: dict, memo: dict) -> list:
    """Every guillotine arrangement of ``blocks`` as [(min_w, min_l, tree)]."""
    if blocks in memo:
        return memo[blocks]
    if len(blocks) == 1:
        out = list(leaf_opts[blocks[0]])
    else:
        out = []
        first, rest = blocks[0], blocks[1:]
        for mask in range(2 ** len(rest) - 1):          # side A holds `first`; side B is never empty
            a = (first,) + tuple(b for i, b in enumerate(rest) if mask >> i & 1)
            b = tuple(b for i, b in enumerate(rest) if not mask >> i & 1)
            for aw, al, ta in _subset_trees(a, leaf_opts, memo):
                for bw, bl, tb in _subset_trees(b, leaf_opts, memo):
                    out.append((aw + bw, max(al, bl), ("v", ta, tb, aw, bw)))
                    out.append((max(aw, bw), al + bl, ("h", ta, tb, al, bl)))
        if len(out) > 400:  # keep the arrangements that are smallest in some direction (Pareto front)
            out.sort(key=lambda t: (t[0], t[1]))
            front, best_l = [], math.inf
            for t in out:
                if t[1] < best_l - EPS:
                    front.append(t)
                    best_l = t[1]
            out = front + [t for t in out if t not in front][: 400 - len(front)]
    memo[blocks] = out
    return out


def _place_tree(tree, x, y, w, l, blocks, out):
    """Slack is shared in proportion to each side's minimum, so every box keeps its proportions."""
    if tree[0] == "leaf":
        _, b, cols, rows, rot = tree
        cw, cl = w / cols, l / rows
        for i, k in enumerate(blocks[b]):
            out[k] = (x + (i % cols) * cw, y + (i // cols) * cl, cw, cl, rot)
    elif tree[0] == "v":
        _, ta, tb, aw, bw = tree
        wa = w * aw / (aw + bw)
        _place_tree(ta, x, y, wa, l, blocks, out)
        _place_tree(tb, x + wa, y, w - wa, l, blocks, out)
    else:
        _, ta, tb, al, bl = tree
        la = l * al / (al + bl)
        _place_tree(ta, x, y, w, la, blocks, out)
        _place_tree(tb, x, y + la, w, l - la, blocks, out)


def _shape_distance(a, b) -> float:
    (a1, a2), (b1, b2) = sorted(a), sorted(b)
    return max(abs(math.log(a1 / b1)), abs(math.log(a2 / b2)))


def block_layout(sizes: dict, groups: list, bin_w: float, bin_l: float, gap: float, cap=None):
    """Fill the whole floor: every set type (a group of same-size boxes, or one box) is one rectangular
    block laid out as a full grid, and the blocks tile the box by straight (guillotine) cuts.

    Of the arrangements that fit, the best one gives different set types clearly different boxes
    (≥ DISTINCT_RATIO apart in width or length), then avoids long narrow boxes (> MAX_ASPECT), then
    grows every box most evenly. Returns (method, placements, rects, distinct), or None.
    """
    in_group = {k for g in groups for k in g}
    blocks = [list(g) for g in groups] + [[k] for k in sizes if k not in in_group]
    if len(blocks) > MAX_BLOCKS:
        return None
    leaf_opts = {}
    for b, keys in enumerate(blocks):
        w, l = sizes[keys[0]]
        n = len(keys)
        opts = []
        for cols in (c for c in range(1, n + 1) if n % c == 0):
            for rot in ((0,) if abs(w - l) < EPS else (0, 90)):
                bw, bl = (w, l) if rot == 0 else (l, w)
                opts.append((cols * bw, (n // cols) * bl, ("leaf", b, cols, n // cols, rot)))
        leaf_opts[b] = opts
    best = None
    for mw, ml, tree in _subset_trees(tuple(range(len(blocks))), leaf_opts, {}):
        if mw > bin_w + EPS or ml > bin_l + EPS:
            continue
        out = {}
        _place_tree(tree, 0.0, 0.0, bin_w, bin_l, blocks, out)
        own, ok = {}, True
        for k, (x, y, w, l, rot) in out.items():
            if cap is not None and (w > cap(k, w, l, 0) + EPS or l > cap(k, w, l, 1) + EPS):
                ok = False
                break
            own[k] = (w - gap, l - gap) if rot == 0 else (l - gap, w - gap)
        if not ok:
            continue
        growth = max(own[k][0] * own[k][1] / ((sizes[k][0] - gap) * (sizes[k][1] - gap)) for k in own)
        aspect = max(max(v) / min(v) for v in own.values())
        dist = min((_shape_distance(own[a[0]], own[b[0]]) for a, b in itertools.combinations(blocks, 2)),
                   default=math.inf)
        # long narrow troughs (> MAX_ASPECT) only when nothing squarer fits
        key = (dist < math.log(DISTINCT_RATIO) - 1e-9, round(max(aspect, MAX_ASPECT), 1), round(growth, 2),
               round(aspect, 2))
        if best is None or key < best[0]:
            best = (key, out)
    if best is None:
        return None
    out = best[1]
    return ("blocks", {k: v for k, v in out.items()}, {k: list(v[:4]) for k, v in out.items()},
            not best[0][0])


def _score(res: dict, bin_w: float, bin_l: float, fill: bool, cap, groups=()) -> tuple:
    if fill:
        base = {k: list(v[:4]) for k, v in res.items()}
        grown = expand(scale_to_fill(base, bin_w, bin_l, cap), bin_w, bin_l, cap)
        if groups:
            grown = equalize(grown, {k: v[4] for k, v in res.items()}, groups, bin_w, bin_l, cap)
        covered = sum(v[2] * v[3] for v in grown.values())
        # most floor covered first (1 cm² buckets), then the most even growth between trays
        worst = max((g[2] * g[3]) / (b[2] * b[3]) for g, b in zip(grown.values(), base.values()))
        return (-round(covered / 100), round(worst, 2)), grown
    ext_w = max(v[0] + v[2] for v in res.values())
    ext_l = max(v[1] + v[3] for v in res.values())
    return (round(ext_w * ext_l, 3), round(ext_w + ext_l, 3)), {k: list(v[:4]) for k, v in res.items()}


def best_layout(sizes: dict, bin_w: float, bin_l: float, fill: bool = False, cap=None, groups=()):
    """Try every heuristic/order (and rectpack); return (method, placements, rects) or None."""
    cands = []
    orders = _orders(sizes)
    for h in HEURISTICS:
        for oname, order in orders.items():
            res = maxrects(sizes, bin_w, bin_l, h, order)
            if res is not None:
                cands.append((f"maxrects_{h}/{oname}", res))
    cands.extend(_rectpack_candidates(sizes, bin_w, bin_l))
    best = None
    for method, res in cands:
        if not valid_placement(res, bin_w, bin_l):
            continue
        score, rects = _score(res, bin_w, bin_l, fill, cap, groups)
        if best is None or score < best[0]:
            best = (score, method, res, rects)
    return None if best is None else best[1:]


def pack_organizer(designs: list[TrayDesign], plan: Plan) -> PackResult:
    s = plan.settings
    gap = s.tray_gap_mm
    bin_w, bin_l = plan.bin_size
    max_h = plan.max_tray_height        # one tray (box space and printer Z)
    stack_h = plan.usable_height        # a whole column
    bed_w, bed_l = plan.bed[0], plan.bed[1]
    limits = [(bed_w, bed_l), (bin_w, bin_l)]
    for d in designs:
        if d.min_h > max_h + EPS:  # designers already guard this; keep as a safety net
            plan.issues.error("too_tall", f"{d.name}: tray height {d.min_h:.1f} mm exceeds the available "
                                          f"{max_h:.1f} mm", d.name)
    if plan.issues.errors:
        raise PlanError(plan.issues)

    notes = share_similar_sizes(designs, plan)
    # similar-name groups: every box in the column frame (short side, long side) so they line up
    cols = [Column([d], [d.min_w > d.min_l], min(d.min_w, d.min_l), max(d.min_w, d.min_l)) if d.size_group
            else Column([d], [False], d.min_w, d.min_l) for d in designs]
    min_area = sum((d.min_w + gap) * (d.min_l + gap) for d in designs)

    def cap_for(cmap):
        def cap(k, w, l, axis):
            # keep the (gap-inflated) column footprint printable and inside the box
            cw, cl = w - gap, l - gap
            other = cl if axis == 0 else cw
            best = 0.0
            for W, L in ((bed_w, bed_l), (bed_l, bed_w)):
                if other <= (L if axis == 0 else W) + EPS:
                    best = max(best, W if axis == 0 else L)
            return best + gap
        return cap

    while True:
        sizes = {i: (c.w + gap, c.l + gap) for i, c in enumerate(cols)}
        twins = twin_groups(cols)
        if plan.mode == "boxes" and s.fill_box and all(len(c.trays) == 1 for c in cols):
            blocks = block_layout(sizes, twins, bin_w, bin_l, gap, cap_for(cols))
            if blocks:
                *found, distinct = blocks
                notes.append("Each kind of set is one block of equal boxes; the blocks fill the whole box floor"
                             + ("" if distinct else ". Some different kinds could not be given clearly "
                                                    "different box shapes"))
                break
        found = best_layout(sizes, bin_w, bin_l, s.fill_box, cap_for(cols), twins)
        if found:
            break
        merge = _best_merge(cols, stack_h, s.max_layers, gap, limits)
        if merge is None:
            need = sum((c.w + gap) * (c.l + gap) for c in cols)
            biggest = sorted(designs, key=lambda d: -d.min_w * d.min_l)[:3]
            plan.issues.error(
                "does_not_fit",
                f"The trays cannot fit in the {plan.box[0]:g} × {plan.box[1]:g} × {plan.box[2]:g} mm box: they "
                f"need at least {need / 100:.0f} cm² of floor (after stacking up to {s.max_layers} high within "
                f"{stack_h:.1f} mm) but the box floor offers {bin_w * bin_l / 100:.0f} cm². Largest trays: "
                + ", ".join(f"{d.name} {d.min_w:.0f}×{d.min_l:.0f}×{d.min_h:.0f}" for d in biggest)
                + ". Options: a larger box, smaller clearance/walls, fewer components per box, "
                  "more settings.max_layers, or less headroom_mm.")
            raise PlanError(plan.issues)
        _, i, j, m = merge
        notes.append(f"Stacked '{cols[j].name}' on '{cols[i].name}' to save floor space")
        cols = [c for k, c in enumerate(cols) if k not in (i, j)] + [m]

    method, placements, rects = found
    if s.fill_box:
        method += " + fill_box"
    for g in twins:
        if not any(t.size_group for i in g for t in cols[i].trays):
            notes.append("Same contents, same size: " + ", ".join(f"'{cols[i].name}'" for i in g))
    placed: list[PlacedTray] = []
    layers = 1
    for ci, col in enumerate(cols):
        x, y, rw, rl = rects[ci]
        crot = placements[ci][4]
        fw, fl = rw - gap, rl - gap                     # column footprint in box frame
        cw, cl = (fw, fl) if crot == 0 else (fl, fw)    # in column frame
        # bottom → top: explicit preferences first, then taller trays lower down
        order = sorted(range(len(col.trays)), key=lambda t: (
            {"bottom": 0, "any": 1, "top": 2}[col.trays[t].layer_pref], -col.trays[t].min_h))
        z = 0.0
        layers = max(layers, len(order))
        for li, t in enumerate(order):
            d, rel = col.trays[t], col.rel_rot[t]
            h = d.min_h
            if s.fill_height and li == len(order) - 1:
                h = max(h, min(max_h, stack_h - z))
            tw, tl = (cl, cw) if rel else (cw, cl)
            rot = ((90 if rel else 0) + crot) % 180
            placed.append(PlacedTray(design=d, x=plan.bin_rect[0] + x + gap / 2, y=plan.bin_rect[1] + y + gap / 2,
                                     z=plan.floor_z + z, rot=rot, w=tw, l=tl, h=h, layer=li, column=ci))
            z += h
    placed.sort(key=lambda p: p.design.id)
    return PackResult(trays=placed, method=method, layers=layers, notes=notes, min_area_mm2=min_area)
