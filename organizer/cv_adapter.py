"""Convert the computer-vision measurement output into a draft organizer plan.

Input: the JSON written by ``boardgame_dimensions_ruler.py`` or
``boardgame_dimensions_relative.py`` (one entry per *kind* of component with
``shape``, ``quantity`` and per-piece ``dimensions_mm``).

The adapter only maps fields; it never fills gaps. Anything the CV step did not
measure (thickness, quantity, the box INSIDE size, the printer bed) is left out
so that plan validation reports it as a missing measurement.
"""
from __future__ import annotations

import re

_NOT_STORED = re.compile(r"\bbox(es)?\b|\bboards?\b|rule ?books?|\brules\b|manual|booklet|\binserts?\b|\blids?\b",
                         re.I)


def infer_type(name: str, shape: str | None, dm: dict | None) -> str:
    n = name.lower()
    if re.search(r"\bcards?\b", n):
        return "cards"
    if re.search(r"\btiles?\b", n):
        return "tiles"
    if re.search(r"\bdice\b|\bdie\b", n):
        return "dice"
    if shape in ("cylinder", "hexagonal_prism"):
        if re.search(r"\bcoins?\b|\bchips?\b|\bdiscs?\b|\bdisks?\b|\btokens?\b", n):
            return "round_tokens"
        if dm and dm.get("diameter") and dm.get("height") and dm["height"] <= 0.35 * dm["diameter"]:
            return "round_tokens"
    return "bits"


def _piece(typ: str, shape: str | None, dm: dict | None) -> dict | None:
    if not dm:
        return None
    g = dm.get
    if typ in ("cards", "tiles"):
        p = {"width": g("width"), "length": g("length"), "thickness": g("height")}
    elif typ == "round_tokens":
        p = {"diameter": g("diameter"), "thickness": g("height")}
    elif typ == "dice":
        vals = [v for v in (g("length"), g("width"), g("height"), g("diameter")) if v]
        p = {"edge": max(vals)} if vals else {}
    elif shape in ("cylinder", "cone", "hexagonal_prism"):
        p = {"width": g("diameter"), "length": g("diameter"), "height": g("height")}
    elif shape == "sphere":
        p = {"width": g("diameter"), "length": g("diameter"), "height": g("diameter")}
    else:
        p = {"width": g("width"), "length": g("length"), "height": g("height")}
    p = {k: v for k, v in p.items() if v is not None}
    return p or None


def plan_from_cv(cv: dict, *, box: dict | None = None, bed: dict | None = None,
                 settings: dict | None = None) -> dict:
    """Return a draft plan dict. ``cv_notes`` lists what the user still has to check."""
    notes: list[str] = []
    units = str(cv.get("units", "mm"))
    relative = units != "mm"
    if relative:
        notes.append("CV output is in relative units (no real-world anchor), so no piece sizes were copied. "
                     "Re-run the CV step with an anchor measurement to get millimetres.")
    comps = []
    for e in cv.get("components", []):
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        shape = e.get("shape")
        dm = None if relative else e.get("dimensions_mm")
        if _NOT_STORED.search(name):
            if re.search(r"\bbox(es)?\b", name, re.I):
                size = (f" (measured outside ≈ {dm.get('length')} × {dm.get('width')} × {dm.get('height')} mm)"
                        if dm else "")
                notes.append(f"'{name}'{size}: box_inner_mm must be the INSIDE of the box — measure it directly.")
            else:
                thick = f" It measured {dm['height']} mm thick." if dm and dm.get("height") else ""
                notes.append(f"'{name}' is not stored in a tray. If it lies on top of the trays, add its "
                             f"thickness to settings.headroom_mm.{thick}")
            continue
        typ = infer_type(name, shape, dm)
        comp: dict = {"name": name, "type": typ}
        if e.get("quantity") is not None:
            comp["quantity"] = e["quantity"]
        piece = _piece(typ, shape, dm)
        if piece:
            comp["piece_mm"] = piece
        comp["source"] = {k: e.get(k) for k in ("index", "shape", "confidence", "notes") if e.get(k) is not None}
        if e.get("confidence") in ("low", "none"):
            notes.append(f"'{name}': CV confidence is {e.get('confidence')} — re-measure before printing.")
        comps.append(comp)
    plan: dict = {"game": cv.get("game") or "Untitled"}
    if box:
        plan["box_inner_mm"] = box
    if bed:
        plan["printer_bed_mm"] = bed
    plan["settings"] = dict(settings or {})
    plan["compartments"] = comps
    plan["cv_notes"] = notes
    return plan
