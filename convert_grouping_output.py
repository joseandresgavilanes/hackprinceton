#!/usr/bin/env python3
"""Convert an Inside the Box grouping result (e.g. exoutput.json) to an STL engine plan.

Usage:
  python convert_grouping_output.py "exoutput (1).json" --bed 256 256 256 -o organizer_plan.json

--bed must be the ACTUAL printer build volume (width, length, height), in mm.
No missing physical dimensions are silently guessed; every problem is listed.

organizer "boxes"   → one plain open box per set (type "set"), preserving set names,
                      member quantities, dimensions and storage_style. No dividers.
organizer "divider" → the earlier behaviour: typed compartments grouped into trays.

Excluded flat items that lie at the bottom of the box (e.g. the folded board) become the
plan's ``bottom_layer``: the trays rest on them. They are never printed and never counted
as headroom under the lid.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SUPPORTED_ORGANIZERS = ("boxes", "divider", "trays")
BOX_STYLES = ("loose", "stack")
_BOTTOM_CUES = re.compile(r"\b(bottom|beneath|underneath|under|base|below|floor)\b", re.I)
_TOP_CUES = re.compile(r"\b(top|above|over|lid)\b", re.I)


class ConversionError(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(problems))


def _positive(obj, key, context, problems):
    v = (obj or {}).get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        problems.append(f"Missing or invalid {context}.{key}: {v!r}")
        return None
    return float(v)


def _quantity(v, context, problems):
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        problems.append(f"Missing or invalid {context}.quantity: {v!r}")
        return None
    return v


def compartment_from_member(member, tray):
    """'divider' mode (unchanged): one typed compartment per member, grouped by tray."""
    problems = []
    name = member.get("name") or member.get("component_id")
    dims = member.get("dimensions_mm") or {}
    n = _quantity(member.get("quantity"), name, problems)
    length = _positive(dims, "length", name, problems)
    width = _positive(dims, "width", name, problems)
    height = _positive(dims, "height", name, problems)
    if problems:
        raise ConversionError(problems)
    shape = member.get("shape", "")
    base = {"name": name, "tray": tray, "quantity": n}

    if shape == "cylinder" and height <= min(length, width) * 0.5:
        # Disc-shaped tokens, kept as a vertical stack.
        return {**base, "type": "round_tokens", "piece_mm": {"diameter": max(length, width), "thickness": height}}
    if shape in {"cube", "rectangular_prism"} and "dice" in (name or "").lower() and abs(length - width) < 1e-6 and abs(width - height) < 1e-6:
        return {**base, "type": "dice", "piece_mm": {"edge": length}, "pockets": True}
    if "card" in name.lower() or "tile" in name.lower():
        typ = "cards" if "card" in name.lower() else "tiles"
        return {**base, "type": typ, "piece_mm": {"width": width, "length": length, "thickness": height}}
    # Loose pawns/markers, including tall cylinders. The engine estimates a bulk
    # storage cavity, with its explicitly documented packing-density assumption.
    return {**base, "type": "bits", "piece_mm": {"width": width, "length": length, "height": height}}


def set_compartment(group, problems):
    """'boxes' mode: the whole set becomes ONE plain box, exactly as grouped upstream."""
    name = group.get("name")
    style = group.get("storage_style")
    if style not in BOX_STYLES:
        problems.append(f"Set {name!r}: storage_style {style!r} is not supported in boxes mode "
                        f"(use {' or '.join(BOX_STYLES)})")
    members = []
    for j, m in enumerate(group.get("members") or []):
        ctx = f"{name}.members[{j}] ({m.get('name') or m.get('component_id')})"
        dims = m.get("dimensions_mm") or {}
        q = _quantity(m.get("quantity"), ctx, problems)
        d = {k: _positive(dims, k, f"{ctx}.dimensions_mm", problems) for k in ("length", "width", "height")}
        if q is None or None in d.values():
            continue
        members.append({"component_id": m.get("component_id"), "name": m.get("name") or m.get("component_id"),
                        "quantity": q, "shape": m.get("shape"), "dimensions_mm": d})
    return {"name": name, "type": "set", "storage_style": style, "members": members}


def _excluded_position(part, notes):
    """Decide whether an excluded flat item lies under the trays ('bottom') or on top ('top')."""
    text = f"{part.get('reason', '')}"
    name = (part.get("name") or part.get("component_id") or "").lower()
    bottom, top = bool(_BOTTOM_CUES.search(text)), bool(_TOP_CUES.search(text))
    if bottom and not top:
        return "bottom"
    if top and not bottom:
        return "top"
    guess = "bottom" if "board" in name else "top"
    notes.append(f"AMBIGUOUS: the input does not say whether '{part.get('name')}' lies under or on top of the "
                 f"storage; treated as '{guess}'. Check this before printing.")
    return guess


def convert(src, bed):
    """Return (plan, notes). Raises ConversionError listing every problem found."""
    problems, notes = [], []
    organizer = str(src.get("organizer") or "divider").strip().lower()
    if organizer not in SUPPORTED_ORGANIZERS:
        problems.append(f"organizer {src.get('organizer')!r} is not supported (expected one of "
                        f"{', '.join(SUPPORTED_ORGANIZERS)})")
    box = (src.get("box") or {}).get("dimensions_mm") or {}
    w = _positive(box, "width", "box.dimensions_mm", problems)
    l = _positive(box, "length", "box.dimensions_mm", problems)
    h = _positive(box, "height", "box.dimensions_mm", problems)
    notes.append("box.dimensions_mm is used as the INSIDE size of the game box (the input does not label it "
                 "inside/outside; basis: %s)." % ((src.get("box") or {}).get("basis") or "unknown"))
    sets = src.get("sets") or []
    if not sets:
        problems.append("No sets in grouping output")
    names = [g.get("name") for g in sets]
    if any(not n for n in names):
        problems.append("Each set must have a name")
    dupes = sorted({n for n in names if n and names.count(n) > 1})
    if dupes:
        problems.append(f"Duplicate set names: {', '.join(dupes)}")
    if src.get("set_count") is not None and src.get("set_count") != len(sets):
        notes.append(f"set_count says {src.get('set_count')} but {len(sets)} sets are listed; using the listed sets.")

    compartments = []
    for g in sets:
        if not g.get("members"):
            problems.append(f"Set {g.get('name')!r} has no members")
            continue
        if organizer == "boxes":
            compartments.append(set_compartment(g, problems))
        else:
            for m in g["members"]:
                try:
                    compartments.append(compartment_from_member(m, g.get("name")))
                except ConversionError as e:
                    problems.extend(e.problems)
    if organizer == "divider" and src.get("divider_set_limit") and len(sets) > src["divider_set_limit"]:
        notes.append(f"{len(sets)} sets exceed divider_set_limit {src['divider_set_limit']}.")

    bottom_layer, headroom = [], 0.0
    for part in src.get("excluded", []):
        pname = part.get("name") or part.get("component_id")
        d = {k: _positive(part.get("dimensions_mm"), k, f"excluded {pname}.dimensions_mm", problems)
             for k in ("length", "width", "height")}
        q = part.get("quantity") or 1
        if None in d.values():
            continue
        if _excluded_position(part, notes) == "bottom":
            bottom_layer.append({"name": pname, "component_id": part.get("component_id"), "quantity": q,
                                 "dimensions_mm": d})
            notes.append(f"'{pname}' ({d['length']:g} × {d['width']:g} × {d['height']:g} mm) lies on the box floor; "
                         f"the storage sits on top of it (not printed, not lid headroom).")
        else:
            headroom += d["height"] * q
            notes.append(f"'{pname}' lies on top of the storage: {d['height'] * q:g} mm reserved as headroom.")
    if w and l and h:
        floor = sum(b["dimensions_mm"]["height"] * b["quantity"] for b in bottom_layer)
        if floor + headroom >= h:
            problems.append("Excluded flat components occupy the entire box height")
    if not src.get("physical_fit_verified", False):
        notes.append("Source says physical_fit_verified = false: this plan is for CAD, not a verified physical fit.")
    if problems:
        raise ConversionError(problems)

    plan = {
        "game": src.get("game") or "Unnamed game",
        "organizer": "boxes" if organizer == "boxes" else "trays",
        "box_inner_mm": {"width": w, "length": l, "height": h},
        "printer_bed_mm": {"width": float(bed[0]), "length": float(bed[1]), "height": float(bed[2])},
        "settings": {"headroom_mm": headroom},
        "bottom_layer": bottom_layer,
        "compartments": compartments,
        "source": {"organizer": src.get("organizer"), "set_count": src.get("set_count"),
                   "physical_fit_verified": src.get("physical_fit_verified"), "edition": src.get("edition")},
        "conversion_notes": notes,
    }
    return plan, notes


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, help="JSON from the game grouping AI")
    p.add_argument("--bed", nargs=3, type=float, metavar=("WIDTH", "LENGTH", "HEIGHT"), required=True)
    p.add_argument("-o", "--output", type=Path, default=Path("organizer_plan.json"))
    a = p.parse_args(argv)
    if any(x <= 0 for x in a.bed):
        p.error("Printer bed measurements must be positive mm")
    src = json.loads(a.input.read_text(encoding="utf-8"))
    try:
        plan, notes = convert(src, a.bed)
    except ConversionError as e:
        print(f"ERROR: cannot convert {a.input} ({len(e.problems)} problem(s)):", file=sys.stderr)
        for pr in e.problems:
            print(f"  - {pr}", file=sys.stderr)
        return 1
    a.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kind = "plain boxes" if plan["organizer"] == "boxes" else "compartments in trays"
    print(f"Created {a.output}: {len(plan['compartments'])} {kind} from {len(src['sets'])} sets "
          f"(organizer {src.get('organizer')!r})")
    for n in notes:
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
