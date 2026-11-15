"""Check and complete the CV measurement JSON before grouping.

Rules (in order of evidence):
* Measurements come from the supplied ruler photos.  A component with an unresolved height but a measured
  footprint uses the CV script's established cylinder approximation (or the documented thin flat-piece
  thickness for cards, boards and rulebooks).
* Only after those image-derived paths are exhausted, an unresolved component is inferred from a measured
  component of the same type/shape.  Its method and a conspicuous warning make that last-resort decision
  auditable.  The sole final fallback is a fixed 20 mm diameter × 30 mm height cylinder.
* The job never reaches grouping with an incomplete length × width × height inventory.  If a count is absent
  after image/inventory processing, it is conservatively recorded as one rather than surfacing a blocking error.
* If a photo that was used was NOT scaled from the ruler's tick marks (``scale_source`` "labels" or
  "assumed_length"), the whole reconstruction may be off by a few percent. The user is asked to measure the
  width of one reference component (a card if there is one, else the widest measured piece). The answer
  rescales every photo measurement by the same ratio.
* If the photos could not calculate the thickness of a card, board or rulebook (the CV script's own error
  model rates that height as low confidence: its error is above 8 % AND above 1.6 mm), the CV script's own
  default thickness is used instead: 4 mm for a board or rulebook, 0.4 mm for a card (``assumed_dimensions``
  and ``CONF_MEDIUM``/``CONF_MEDIUM_MM`` in ``pipeline/boardgame_dimensions_ruler_v7.py``). This is listed in
  the job's warnings.
* Low-confidence components are reported as warnings; they do not stop the job.
"""
from __future__ import annotations

import copy
import math
import re

TRUSTED_SCALE = {"ticks"}
MAX_RATIO = 2.0           # an answer more than 2× off the measured width is almost certainly a units mistake
# The CV script's fallback thicknesses, and how it recognises those pieces by name.
DEFAULT_THICKNESS_MM = {"rulebook": 4.0, "board": 4.0, "card": 0.4}
# The CV script's limits for a measurement better than "low" confidence: relative error ≤ 8 % or error ≤ 1.6 mm.
CONF_MEDIUM, CONF_MEDIUM_MM = 0.08, 1.6
FLAT_KINDS = (
    ("rulebook", re.compile(r"\b(?:rule\s*books?|instruction(?:s| manuals?| booklets?)?|rules booklets?|manuals?)\b", re.I)),
    ("board", re.compile(r"\b(?:boards?|cardboard)\b", re.I)),
    ("card", re.compile(r"\bcards?\b", re.I)),
)
SIZE_MODIFIERS = ((re.compile(r"\b(?:tiny|mini(?:ature)?)\b", re.I), 0.65),
                  (re.compile(r"\bsmall\b", re.I), 0.80),
                  (re.compile(r"\b(?:large|big)\b", re.I), 1.25),
                  (re.compile(r"\b(?:giant|jumbo|huge)\b", re.I), 1.50))
LAST_RESORT_CYLINDER_DIAMETER_MM = 20.0
LAST_RESORT_CYLINDER_HEIGHT_MM = 30.0


def component_id(c: dict, i: int) -> str:
    """The id the grouping step gives a CV component (``organize_game_boxes.load_game``)."""
    return c.get("id") or f"component_{c.get('index', i)}"


def usable_dims(c: dict) -> dict | None:
    d = c.get("dimensions_mm") or {}
    if not isinstance(d, dict):
        return None
    if d.get("diameter") is not None:
        d = {"length": d["diameter"], "width": d["diameter"], **{k: v for k, v in d.items() if k != "diameter"}}
    out = {}
    for k in ("length", "width", "height"):
        v = d.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            return None
        out[k] = float(v)
    return out


def _photo_measured(c: dict) -> bool:
    method = str(c.get("measurement_method") or "").lower()
    # The CV script's labelled cylinder/card/board fallbacks still begin with a measured image footprint.
    return "photo" in method or method.startswith("assumed_")


def ensure_expected_components(cv: dict) -> tuple[dict, list[str]]:
    """Make the CV inventory total before dimensions are completed.

    Live CV publishes ``expected_components`` from its component-list stage.  If an expected name was not
    emitted by vision, retain it as an explicit unresolved component instead of silently letting it disappear
    before grouping.  Fixtures predating this field remain supported.
    """
    cv = copy.deepcopy(cv)
    expected = cv.get("expected_components")
    warnings = []
    if expected is None:
        expected = []
        warnings.append("The CV stage omitted its expected inventory; preserved all detected components and applied the emergency fallback if needed.")
    elif not isinstance(expected, list):
        expected = []
        warnings.append("The CV expected inventory was invalid; preserved all detected components and applied the emergency fallback if needed.")
    components = cv.get("components")
    if not isinstance(components, list):
        components = []
        cv["components"] = components
        warnings.append("The CV component list was invalid; added one emergency placeholder component.")
    else:
        invalid_entries = len([c for c in components if not isinstance(c, dict)])
        if invalid_entries:
            components[:] = [c for c in components if isinstance(c, dict)]
            warnings.append(f"Discarded {invalid_entries} malformed CV component entr{'y' if invalid_entries == 1 else 'ies'}.")
    seen = {str(c.get("name")) for c in components if isinstance(c, dict) and c.get("name")}
    for ordinal, item in enumerate(expected, 1):
        if not isinstance(item, dict) or not item.get("name"):
            warnings.append(f"Expected inventory entry {ordinal} was unnamed and was skipped.")
            continue
        name = str(item["name"])
        if name in seen:
            continue
        components.append({"index": item.get("index", len(components) + 1), "name": name,
                           "quantity": item.get("quantity"), "shape": item.get("shape", "cylinder"),
                           "dimensions_mm": None, "confidence": "none", "estimated_error_pct": None,
                           "measurement_method": None,
                           "notes": "Not emitted by vision; retained from the required measurement inventory."})
        seen.add(name)
        warnings.append(f"{name}: absent from the vision detections; retained for mandatory completion.")
    if not components:
        components.append({"index": 1, "name": "Unidentified component", "quantity": 1, "shape": "cylinder",
                           "dimensions_mm": None, "confidence": "none", "estimated_error_pct": None,
                           "measurement_method": None,
                           "notes": "No component inventory survived measurement; emergency placeholder retained."})
        warnings.append("No components were emitted by CV; added one emergency placeholder component.")
    return cv, warnings


def missing_components(cv: dict) -> list[str]:
    out = []
    for i, c in enumerate(cv.get("components") or [], 1):
        name = c.get("name") or f"component {i}"
        q = c.get("quantity")
        if usable_dims(c) is None:
            out.append(f"{name}: no complete length × width × height")
        elif not isinstance(q, int) or isinstance(q, bool) or q <= 0:
            out.append(f"{name}: no quantity")
    return out


def complete_quantities(cv: dict) -> tuple[dict, list[str]]:
    """Never fail the job solely because vision/the inventory lookup did not provide a count."""
    cv = copy.deepcopy(cv)
    warnings = []
    for i, c in enumerate(cv.get("components") or [], 1):
        q = c.get("quantity")
        if isinstance(q, int) and not isinstance(q, bool) and q > 0:
            continue
        c["quantity"] = 1
        c.setdefault("measurement", {}).update({"quantity_inferred": True, "quantity_inference_method": "emergency_default_one"})
        warnings.append(f"{c.get('name') or f'component {i}'}: no reliable quantity was available; assumed one piece.")
    return cv, warnings


def thickness_not_calculated(c: dict) -> bool:
    """The photos gave a height, but the CV script's error model rates it as low confidence."""
    d, m = usable_dims(c), c.get("measurement") or {}
    err = (c.get("estimated_error_mm") or {}).get("height")
    if d is None or m.get("height_assumed") or m.get("height_method") == "upright_ruler":
        return False
    if not isinstance(err, (int, float)) or isinstance(err, bool):
        return False
    return err / d["height"] > CONF_MEDIUM and err > CONF_MEDIUM_MM


def default_thickness(cv: dict) -> tuple[dict, dict, list[str]]:
    """Cards, boards and rulebooks whose thickness could not be calculated get the CV script's default.

    Returns (new cv, {component id: height used}, warnings). Every other measurement is left as it is.
    """
    cv = copy.deepcopy(cv)
    heights, warnings = {}, []
    for i, c in enumerate(cv.get("components") or [], 1):
        kind = next((k for k, rx in FLAT_KINDS if rx.search(str(c.get("name") or ""))), None)
        if kind is None or not thickness_not_calculated(c):
            continue
        old, err, new = c["dimensions_mm"]["height"], c["estimated_error_mm"]["height"], DEFAULT_THICKNESS_MM[kind]
        c["dimensions_mm"]["height"] = new
        c.setdefault("measurement", {}).update({
            "height_method": "assumed_card_thickness" if kind == "card" else "assumed_4mm_thickness",
            "height_assumed": True, "uncalculated_height_mm": old, "uncalculated_height_error_mm": err})
        heights[component_id(c, i)] = new
        warnings.append(f"{c.get('name')}: the photos could not calculate its thickness ({old:g} ± {err:g} mm), "
                        f"so the default {new:g} mm for a {kind} was used.")
    return cv, heights, warnings


def _positive(value) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0 else None)


def _flat_kind(c: dict) -> str | None:
    return next((kind for kind, rx in FLAT_KINDS if rx.search(str(c.get("name") or ""))), None)


def _relative_factor(name: str) -> float:
    return next((factor for rx, factor in SIZE_MODIFIERS if rx.search(name)), 1.0)


def _copy_scaled_dimensions(reference: dict, target: dict) -> dict:
    """Copy a measured reference, preserving/scaling to any partial image measurement on target."""
    ref = usable_dims(reference)
    if ref is None:
        raise ValueError("reference needs usable dimensions")
    raw = target.get("dimensions_mm") if isinstance(target.get("dimensions_mm"), dict) else {}
    partial = {k: _positive(raw.get(k)) for k in ("length", "width", "height", "diameter")}
    partial = {k: v for k, v in partial.items() if v is not None}
    ref_raw = reference.get("dimensions_mm") or {}
    comparable = {"length": ref["length"], "width": ref["width"], "height": ref["height"],
                  "diameter": _positive(ref_raw.get("diameter"))}
    ratios = [value / comparable[key] for key, value in partial.items() if comparable.get(key)]
    factor = sum(ratios) / len(ratios) if ratios else _relative_factor(str(target.get("name") or ""))
    copied = {key: round(value * factor, 2) for key, value in ref.items()}
    copied.update({key: round(value, 2) for key, value in partial.items() if key in copied})
    return copied


def _reference_candidates(cv: dict, target: dict) -> list[dict]:
    """Prefer semantic same-type suggestions, then the closest measured shape in the supplied photos."""
    components = list(cv.get("components") or [])
    name = target.get("name")
    names = set()
    for group in cv.get("type_groups") or []:
        members = group.get("members") if isinstance(group, dict) else None
        if isinstance(members, list) and name in members:
            names.update(str(member) for member in members if member != name)
    for pending in cv.get("unresolved_same_type_candidates") or []:
        if isinstance(pending, dict) and name in (pending.get("components") or []):
            names.update(str(member) for member in pending.get("possible_same_as") or [])
    measured = [c for c in components if _photo_measured(c) and usable_dims(c)]
    preferred = [c for c in measured if c.get("name") in names]
    if preferred:
        return preferred
    shape = str(target.get("shape") or "")
    same_shape = [c for c in measured if c is not target and str(c.get("shape") or "") == shape]
    return same_shape or [c for c in measured if c is not target]


def _last_resort_dimensions(c: dict) -> tuple[dict, str]:
    """The deliberately fixed emergency fallback; use only after every image-based path fails."""
    return ({"diameter": LAST_RESORT_CYLINDER_DIAMETER_MM, "height": LAST_RESORT_CYLINDER_HEIGHT_MM},
            "the emergency 20 mm diameter × 30 mm height cylinder")


def complete_dimensions(cv: dict) -> tuple[dict, list[str]]:
    """Make every component's dimensions explicit before grouping, retaining an audit trail.

    This deliberately mirrors the CV script's order: measured footprint → cylinder/flat approximation →
    same-type relative size → a named, conservative final approximation.  It never hides an inference as a
    photo measurement.
    """
    cv = copy.deepcopy(cv)
    warnings = []
    for i, c in enumerate(cv.get("components") or [], 1):
        if usable_dims(c):
            continue
        raw = c.get("dimensions_mm") if isinstance(c.get("dimensions_mm"), dict) else {}
        length, width, height, diameter = (_positive(raw.get(key)) for key in ("length", "width", "height", "diameter"))
        kind = _flat_kind(c)
        method = note = None
        # Directly measurable footprint: use the same explicit fallbacks as the CV script.
        if length is not None and width is not None and height is None:
            if kind:
                dimensions = {"length": length, "width": width, "height": DEFAULT_THICKNESS_MM[kind]}
                method, note = ("assumed_card_thickness" if kind == "card" else "assumed_4mm_thickness",
                                f"Measured footprint; assumed documented {DEFAULT_THICKNESS_MM[kind]:g} mm {kind} thickness.")
            else:
                dimensions = {"diameter": min(length, width), "height": max(length, width)}
                method, note = ("assumed_cylinder_from_footprint",
                                "Measured footprint; used cylinder approximation (short side = diameter, long side = height).")
        elif diameter is not None and height is None:
            dimensions = {"length": diameter, "width": diameter, "height": diameter}
            method, note = "assumed_cylinder_from_diameter", "Measured diameter; used a cylinder with equal axial height."
        else:
            references = _reference_candidates(cv, c)
            if references:
                ref = references[0]
                dimensions = _copy_scaled_dimensions(ref, c)
                method, note = ("relative_token_size_inference",
                                f"No complete direct measurement; inferred relative size from '{ref.get('name')}'.")
            else:
                dimensions, basis = _last_resort_dimensions(c)
                c["shape"] = "cylinder"
                method, note = "last_resort_20x30mm_cylinder", f"Every image-based option was exhausted; used {basis}."
        c["dimensions_mm"] = dimensions
        c["confidence"] = "inferred"
        c["estimated_error_pct"] = None
        c["measurement_method"] = method
        c.setdefault("measurement", {}).update({"dimensions_inferred": True, "inference_method": method})
        c["notes"] = note
        warnings.append(f"{c.get('name') or f'component {i}'}: {note}")
    return cv, warnings


def set_heights(grouping: dict, heights: dict) -> dict:
    """Apply ``default_thickness`` results to a grouping JSON whose members carry the CV dimensions."""
    grouping = copy.deepcopy(grouping)
    for m in [m for s in grouping.get("sets") or [] for m in s.get("members") or []] + list(grouping.get("excluded") or []):
        if m.get("component_id") in heights and isinstance(m.get("dimensions_mm"), dict):
            m["dimensions_mm"] = {**m["dimensions_mm"], "height": heights[m["component_id"]]}
    return grouping


def low_confidence(cv: dict) -> list[str]:
    out = []
    for c in cv.get("components") or []:
        if str(c.get("confidence")).lower() == "low" and usable_dims(c):
            err = c.get("estimated_error_pct")
            out.append(f"Low-confidence measurement: {c.get('name')}" + (f" (±{err:g}%)" if err else ""))
    return out


def scale_question(cv: dict) -> dict | None:
    photos = [p for p in cv.get("photos") or [] if p.get("status", "used") == "used"]
    unsure = [p for p in photos if p.get("scale_source") and p["scale_source"] not in TRUSTED_SCALE]
    if not unsure:
        return None
    cands = [(i, c, usable_dims(c)) for i, c in enumerate(cv.get("components") or [], 1)]
    cands = [(i, c, d) for i, c, d in cands if d and _photo_measured(c)]
    if not cands:
        return None
    # prefer a card (flat, easy to measure with a ruler), then the widest piece (smallest relative error)
    i, ref, d = min(cands, key=lambda t: ("card" not in str(t[1].get("name", "")).lower(), -t[2]["width"]))
    sources = sorted({p["scale_source"] for p in unsure})
    return {"object": ref.get("name") or f"component {i}", "dimension": "width_mm",
            "suggested_value": round(d["width"], 1), "measured_mm": d["width"],
            "component_id": ref.get("id") or f"component_{ref.get('index', i)}",
            "reason": f"The scale of {len(unsure)} of {len(photos)} photo(s) was estimated "
                      f"({', '.join(sources)}), not read from the ruler's tick marks. Measure the width of one "
                      f"'{ref.get('name')}' with a ruler so every size can be corrected."}


def answer_ratio(question: dict, width_mm: float) -> float:
    ratio = width_mm / question["measured_mm"]
    if not 1 / MAX_RATIO <= ratio <= MAX_RATIO:
        raise ValueError(f"{width_mm:g} mm is more than {MAX_RATIO:g}× the {question['measured_mm']:.1f} mm measured "
                         f"from the photos for '{question['object']}'. Check the units (millimetres) and measure "
                         f"its width (the shorter side) again.")
    return ratio


def _scale_dims(d: dict, ratio: float) -> dict:
    return {k: (round(v * ratio, 2) if k in ("length", "width", "height", "diameter")
                and isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in d.items()}


def rescale_cv(cv: dict, ratio: float, question: dict, width_mm: float) -> tuple[dict, list[str]]:
    """Every photo measurement scaled by ``ratio``; returns (new cv, ids of the scaled components)."""
    cv = copy.deepcopy(cv)
    ids = []
    for i, c in enumerate(cv.get("components") or [], 1):
        if _photo_measured(c) and isinstance(c.get("dimensions_mm"), dict):
            c["dimensions_mm"] = _scale_dims(c["dimensions_mm"], ratio)
            ids.append(c.get("id") or f"component_{c.get('index', i)}")
    cv["scale_correction"] = {"reference": question["object"], "measured_from_photos_mm": question["measured_mm"],
                              "measured_by_user_mm": width_mm, "ratio": round(ratio, 5), "components": ids}
    return cv, ids


def rescale_grouping(grouping: dict, ratio: float, ids: list[str]) -> dict:
    """Apply the same correction to a grouping JSON whose members carry the CV dimensions (fixtures mode)."""
    grouping = copy.deepcopy(grouping)
    ids = set(ids)
    for m in [m for s in grouping.get("sets") or [] for m in s.get("members") or []] + list(grouping.get("excluded") or []):
        if m.get("component_id") in ids and isinstance(m.get("dimensions_mm"), dict):
            m["dimensions_mm"] = _scale_dims(m["dimensions_mm"], ratio)
    return grouping
