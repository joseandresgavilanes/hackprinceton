#!/usr/bin/env python3
"""Convert an Inside the Box grouping result (e.g. exoutput.json) to an STL engine plan.

Usage:
  python convert_grouping_output.py ttrloutput.json --bed 256 256 256 -o organizer_plan.json

--bed must be the ACTUAL printer build volume (width, length, height), in mm.
No missing physical dimensions are silently guessed; every problem is listed.

The JSON is the single source of truth:
  * box.dimensions_mm is the space the boxes must fit in.
  * Every entry of ``sets`` becomes exactly ONE plain open box (type "set") with one
    rectangular cavity, preserving the set's name, members, quantities, dimensions and
    storage_style (loose, stack or card_stack). No dividers, wells or slots; no extra sets.
  * Everything in ``excluded`` is ignored: no container, no floor space and no fit check.
    The one exception: an excluded BOARD or RULEBOOK lies on top of the boxes, so every box
    is made shorter by its listed thickness (dimensions_mm.height × quantity) — that height
    is kept free under the lid (settings.headroom_mm).

This applies to organizer "boxes" and "divider" alike. ``--typed`` restores the earlier
divider conversion (one typed compartment per member, grouped into one tray per set).
``--compact`` sizes each box to its contents instead of filling the game box.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SUPPORTED_ORGANIZERS = ("boxes", "divider", "trays")
BOX_STYLES = ("loose", "stack", "card_stack")
# Excluded items that lie flat on top of the boxes (identified by their name)
_LID_ITEMS = re.compile(r"\b(board|boards|gameboard|map|rules?|rulebooks?|leaflet|booklet|manual|instructions?)\b",
                        re.I)


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


def convert(src, bed, typed=False, compact=False):
    """Return (plan, notes). Raises ConversionError listing every problem found.

    ``typed``: per-member typed compartments in one tray per set (the earlier divider
    conversion) instead of one plain box per set. ``compact``: boxes sized to their contents
    instead of the default flush-height boxes that fill the game box floor.
    """
    problems, notes = [], []
    organizer = str(src.get("organizer") or "divider").strip().lower()
    if organizer not in SUPPORTED_ORGANIZERS:
        problems.append(f"organizer {src.get('organizer')!r} is not supported (expected one of "
                        f"{', '.join(SUPPORTED_ORGANIZERS)})")
    box = (src.get("box") or {}).get("dimensions_mm") or {}
    w = _positive(box, "width", "box.dimensions_mm", problems)
    l = _positive(box, "length", "box.dimensions_mm", problems)
    h = _positive(box, "height", "box.dimensions_mm", problems)
    basis = (src.get("box") or {}).get("basis") or "unknown"
    notes.append(f"box.dimensions_mm is used as the INSIDE size of the game box (basis: {basis}).")
    if "estimate" in str(basis).lower():
        notes.append("The box size is an ESTIMATE in the source; measure the real box interior before printing.")
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
        if not typed:
            compartments.append(set_compartment(g, problems))
        else:
            for m in g["members"]:
                try:
                    compartments.append(compartment_from_member(m, g.get("name")))
                except ConversionError as e:
                    problems.extend(e.problems)
    if typed and src.get("divider_set_limit") and len(sets) > src["divider_set_limit"]:
        notes.append(f"{len(sets)} sets exceed divider_set_limit {src['divider_set_limit']}.")
    if not typed and organizer != "boxes":
        notes.append(f"organizer {src.get('organizer')!r}: one plain box per set (use --typed for the earlier "
                     f"divided trays).")

    # Excluded components are not part of the organizer: no container, floor space or fit check.
    # Boards and rulebooks lie on top of the boxes, so their listed thickness is kept free under the lid.
    excluded, on_top = [], []
    for part in src.get("excluded") or []:
        if not isinstance(part, dict):
            continue
        pname = part.get("name") or part.get("component_id") or "unnamed"
        excluded.append(pname)
        if not _LID_ITEMS.search(pname):
            continue
        t = (part.get("dimensions_mm") or {}).get("height")
        q = part.get("quantity") or 1
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0 or not isinstance(q, int) or q < 1:
            notes.append(f"MISSING: '{pname}' has no valid dimensions_mm.height/quantity, so no height is kept "
                         f"for it on top of the boxes. Measure it before printing.")
            continue
        on_top.append({"name": pname, "height_mm": round(float(t) * q, 3)})
    headroom = round(sum(i["height_mm"] for i in on_top), 3)
    if excluded:
        notes.append(f"Ignored {len(excluded)} excluded component(s) (no container, floor space or fit check): "
                     f"{'; '.join(excluded)}.")
    if on_top:
        notes.append("Boxes made " + f"{headroom:g} mm shorter so these lie on top of them under the lid: "
                     + "; ".join(f"'{i['name']}' {i['height_mm']:g} mm" for i in on_top) + ".")
    if h and headroom >= h:
        problems.append(f"The excluded board/rulebook stack ({headroom:g} mm) is as tall as the {h:g} mm box: "
                        f"no height is left for the storage boxes")
    if not src.get("physical_fit_verified", False):
        notes.append("Source says physical_fit_verified = false: this plan is for CAD, not a verified physical fit.")
    if problems:
        raise ConversionError(problems)

    settings = {"headroom_mm": headroom}
    if compact:
        settings.update(fill_height=False, fill_box=False)
    plan = {
        "game": src.get("game") or "Unnamed game",
        "organizer": "trays" if typed else "boxes",
        "box_inner_mm": {"width": w, "length": l, "height": h},
        "printer_bed_mm": {"width": float(bed[0]), "length": float(bed[1]), "height": float(bed[2])},
        "settings": settings,
        "compartments": compartments,
        "source": {"organizer": src.get("organizer"), "set_count": src.get("set_count"),
                   "physical_fit_verified": src.get("physical_fit_verified"), "edition": src.get("edition"),
                   "box_basis": basis, "excluded_ignored": excluded, "on_top_of_boxes": on_top},
        "conversion_notes": notes,
    }
    return plan, notes


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, help="JSON from the game grouping AI")
    p.add_argument("--bed", nargs=3, type=float, metavar=("WIDTH", "LENGTH", "HEIGHT"), required=True)
    p.add_argument("-o", "--output", type=Path, default=Path("organizer_plan.json"))
    p.add_argument("--typed", action="store_true",
                   help="earlier divider conversion: typed compartments per member, one tray per set")
    p.add_argument("--compact", action="store_true",
                   help="boxes sized to their contents instead of flush boxes filling the game box")
    a = p.parse_args(argv)
    if any(x <= 0 for x in a.bed):
        p.error("Printer bed measurements must be positive mm")
    src = json.loads(a.input.read_text(encoding="utf-8"))
    try:
        plan, notes = convert(src, a.bed, typed=a.typed, compact=a.compact)
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
