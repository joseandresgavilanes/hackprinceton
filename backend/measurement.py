"""Check the CV measurement JSON before grouping: report what is missing, or ask ONE question.

Rules (a dimension is never guessed):
* A component without usable length, width and height (a cylinder's diameter counts as length and width)
  or without a quantity stops the job: the user has to retake photos. One width cannot fill those in, and
  the grouping step refuses an incomplete inventory anyway.
* If a photo that was used was NOT scaled from the ruler's tick marks (``scale_source`` "labels" or
  "assumed_length"), the whole reconstruction may be off by a few percent. The user is asked to measure the
  width of one reference component (a card if there is one, else the widest measured piece). The answer
  rescales every photo measurement by the same ratio.
* If the photos could not calculate the thickness of a card, board or rulebook (the fitted height's
  uncertainty is at least as large as the height itself), the CV script's own default thickness is used
  instead: 4 mm for a board or rulebook, 0.4 mm for a card (``assumed_dimensions`` in
  ``pipeline/boardgame_dimensions_ruler_v7.py``). This is listed in the job's warnings.
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
FLAT_KINDS = (
    ("rulebook", re.compile(r"\b(?:rule\s*books?|instruction(?:s| manuals?| booklets?)?|rules booklets?|manuals?)\b", re.I)),
    ("board", re.compile(r"\b(?:boards?|cardboard)\b", re.I)),
    ("card", re.compile(r"\bcards?\b", re.I)),
)


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
    method = str(c.get("measurement_method") or "photos").lower()
    return "photo" in method


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


def thickness_not_calculated(c: dict) -> bool:
    """The photos gave a height, but its uncertainty is at least as large as the height itself."""
    d, m = usable_dims(c), c.get("measurement") or {}
    err = (c.get("estimated_error_mm") or {}).get("height")
    if d is None or m.get("height_assumed") or m.get("height_method") == "upright_ruler":
        return False
    return isinstance(err, (int, float)) and not isinstance(err, bool) and err >= d["height"]


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
