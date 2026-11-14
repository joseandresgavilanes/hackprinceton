"""Plan parsing and validation for the organizer engine.

All lengths are millimetres. Problems are *collected* rather than raised one at
a time, so a caller always gets the complete list of what is wrong or missing.
Physical measurements are never invented: when one is absent it is reported as
a ``missing_measurement`` error. Process settings (wall thickness, clearance...)
have documented defaults because they describe the printer, not the game.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

COMPONENT_TYPES = ("cards", "tiles", "round_tokens", "dice", "bits", "generic", "set")
ORGANIZER_MODES = ("trays", "boxes")   # anything else (e.g. "divider") behaves like "trays"
BOXES_FORBIDDEN_KEYS = ("tray", "group", "stacks", "cells", "pockets")

TYPE_ALIASES = {
    "card": "cards", "cards": "cards", "deck": "cards",
    "tile": "tiles", "tiles": "tiles",
    "round_token": "round_tokens", "round_tokens": "round_tokens", "coin": "round_tokens",
    "coins": "round_tokens", "disc": "round_tokens", "discs": "round_tokens", "chips": "round_tokens",
    "die": "dice", "dice": "dice",
    "bit": "bits", "bits": "bits", "meeple": "bits", "meeples": "bits", "irregular": "bits",
    "pieces": "bits", "miniatures": "bits", "tokens": "bits", "cubes": "bits",
    "generic": "generic", "bin": "generic",
    "set": "set", "box": "set",
}

# Used only for legacy entries that give `required_mm` but no `type`. The
# inferred type changes ergonomic features (notches/scoops), never dimensions.
NAME_HINTS = (
    ("cards", re.compile(r"\bcards?\b|\bdecks?\b", re.I)),
    ("tiles", re.compile(r"\btiles?\b", re.I)),
    ("dice", re.compile(r"\bdice\b|\bdie\b", re.I)),
    ("bits", re.compile(r"meeple|\bpieces?\b|\bcubes?\b|\btokens?\b|\bbits?\b|\bminis?\b|miniature|"
                        r"\bmarkers?\b|\bworkers?\b|\bpawns?\b|\bcoins?\b", re.I)),
)

LAYER_PREFS = ("any", "top", "bottom")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "tray"


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
@dataclass
class Issue:
    severity: str            # "error" | "warning"
    code: str
    message: str
    component: Optional[str] = None
    field: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


class Issues:
    def __init__(self) -> None:
        self.items: list[Issue] = []

    def error(self, code, message, component=None, field=None):
        self.items.append(Issue("error", code, message, component, field))

    def warn(self, code, message, component=None, field=None):
        self.items.append(Issue("warning", code, message, component, field))

    def missing(self, component, field_name, why):
        where = f"{component}: " if component else ""
        self.items.append(Issue("error", "missing_measurement", f"{where}{field_name} is required — {why}",
                                component, field_name))

    @property
    def errors(self):
        return [i for i in self.items if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.items if i.severity == "warning"]

    @property
    def missing_measurements(self):
        return [i for i in self.items if i.code == "missing_measurement"]

    def for_component(self, name):
        return [i for i in self.errors if i.component == name]


class PlanError(ValueError):
    """Raised when a plan cannot be turned into an organizer. Carries every issue found."""

    def __init__(self, issues: Issues, headline: str | None = None):
        self.issues = issues
        errs = issues.errors
        msg = headline or (errs[0].message if errs else "invalid plan")
        if len(errs) > 1 and not headline:
            msg += f" (+{len(errs) - 1} more problem(s))"
        super().__init__(msg)

    def report(self) -> dict:
        return {
            "status": "failed",
            "errors": [i.to_dict() for i in self.issues.errors],
            "missing_measurements": [i.to_dict() for i in self.issues.missing_measurements],
            "warnings": [i.to_dict() for i in self.issues.warnings],
        }


# ---------------------------------------------------------------------------
# Number helpers
# ---------------------------------------------------------------------------
def num(value: Any, label: str, issues: Issues, component: str | None = None, *, required=True,
        minimum: float = 0.0, allow_equal=False, why="measure it in millimetres") -> Optional[float]:
    """Parse a finite number > minimum (or >= when allow_equal). Missing → missing_measurement."""
    if value is None:
        if required:
            issues.missing(component, label, why)
        return None
    if isinstance(value, bool):
        issues.error("invalid_value", f"{label} must be a number, got {value!r}", component, label)
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        issues.error("invalid_value", f"{label} must be a number, got {value!r}", component, label)
        return None
    if not math.isfinite(x) or x < minimum or (x == minimum and not allow_equal):
        op = ">=" if allow_equal else ">"
        issues.error("invalid_value", f"{label} must be a finite number {op} {minimum:g}, got {value!r}",
                     component, label)
        return None
    return x


def dims(obj: Any, label: str, keys: tuple[str, ...], issues: Issues, component=None, *, required=True,
         why="measure it in millimetres") -> Optional[dict]:
    """Parse an object of positive numbers. Returns None if anything is missing/invalid."""
    if obj is None:
        if required:
            for k in keys:
                issues.missing(component, f"{label}.{k}", why)
        return None
    if not isinstance(obj, dict):
        issues.error("invalid_value", f"{label} should be an object with {', '.join(keys)}", component, label)
        return None
    out = {}
    for k in keys:
        out[k] = num(obj.get(k), f"{label}.{k}", issues, component, why=why)
    return None if any(v is None for v in out.values()) else out


def quantity(value: Any, label: str, issues: Issues, component=None, *, required=True) -> Optional[int]:
    if value is None:
        if required:
            issues.missing(component, label, "count how many pieces go in this compartment")
        return None
    try:
        q = float(value)
    except (TypeError, ValueError):
        q = float("nan")
    if isinstance(value, bool) or not math.isfinite(q) or q < 1 or abs(q - round(q)) > 1e-9:
        issues.error("invalid_value", f"{label} must be a whole number >= 1, got {value!r}", component, label)
        return None
    return int(round(q))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    wall_mm: float = 2.4                 # outer tray wall
    floor_mm: float = 2.0                # minimum floor under the deepest cavity
    clearance_mm: float = 1.0            # per-side play around contents (+ once on top)
    tray_gap_mm: float = 1.0             # air gap between neighbouring trays
    box_margin_mm: float = 1.0           # gap between trays and the box walls
    divider_mm: float = 1.6              # internal walls between cells
    min_feature_mm: float = 1.2          # thinnest wall the printer can make reliably
    headroom_mm: float = 0.0             # height reserved under the lid (board, rulebook)
    fill_height: bool = True             # raise trays to a flush top (raised floors)
    fill_box: bool = True                # grow trays sideways into empty floor space
    max_layers: int = 3                  # max trays stacked in one column
    corner_radius_mm: float = 1.0        # outer vertical edge rounding
    finger_notch_width_mm: float = 20.0  # U-notch width for cards/tiles
    scoop_radius_mm: float = 12.0        # max bottom scoop radius for bins
    bulk_packing_efficiency: float = 0.6 # loose-pile density for bits sized from piece bboxes
    explode_gap_mm: float = 30.0         # vertical spacing in the exploded view
    stl_tolerance_mm: float = 0.05       # absolute chordal deviation of the STL tessellation
    stl_angular_tolerance_rad: float = 0.2

    given: tuple = field(default=(), compare=False, repr=False)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "given"}


_SETTING_RULES = {
    # name: (minimum, allow_equal)
    "wall_mm": (0.0, False), "floor_mm": (0.0, False), "clearance_mm": (0.0, True),
    "tray_gap_mm": (0.0, True), "box_margin_mm": (0.0, True), "divider_mm": (0.0, False),
    "min_feature_mm": (0.0, False), "headroom_mm": (0.0, True), "corner_radius_mm": (0.0, True),
    "finger_notch_width_mm": (0.0, False), "scoop_radius_mm": (0.0, True),
    "bulk_packing_efficiency": (0.0, False), "explode_gap_mm": (0.0, True),
    "stl_tolerance_mm": (0.0, False), "stl_angular_tolerance_rad": (0.0, False),
}


def parse_settings(raw: Any, issues: Issues, mode: str = "trays") -> Settings:
    s = Settings()
    if mode == "boxes":
        # Simple removable boxes: sized to their contents, no raised floors, no growing into
        # free floor space, no stacking unless the plan asks for it.
        s.fill_height, s.fill_box, s.max_layers = False, False, 1
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        issues.error("invalid_value", "settings must be an object")
        return s
    known = {f.name for f in fields(Settings)} - {"given"}
    for key in raw:
        if key not in known:
            issues.warn("unknown_setting", f"settings.{key} is not a recognised setting and was ignored")
    given = []
    for name, (minimum, eq) in _SETTING_RULES.items():
        if name in raw:
            v = num(raw[name], f"settings.{name}", issues, minimum=minimum, allow_equal=eq)
            if v is not None:
                setattr(s, name, v)
                given.append(name)
    for name in ("fill_height", "fill_box"):
        if name in raw:
            if isinstance(raw[name], bool):
                setattr(s, name, raw[name])
                given.append(name)
            else:
                issues.error("invalid_value", f"settings.{name} must be true or false")
    if "max_layers" in raw:
        q = quantity(raw["max_layers"], "settings.max_layers", issues)
        if q is not None:
            if q > 6:
                issues.error("invalid_value", "settings.max_layers must be between 1 and 6")
            else:
                s.max_layers = q
                given.append("max_layers")
    s.given = tuple(given)

    # Manufacturability floor (kept from the original prototype).
    if s.wall_mm < 1.2 or s.floor_mm < 1.2:
        issues.error("unprintable", "Wall and floor must be at least 1.2 mm for this prototype")
    if s.divider_mm < s.min_feature_mm:
        issues.error("unprintable", f"settings.divider_mm ({s.divider_mm}) is thinner than "
                                    f"settings.min_feature_mm ({s.min_feature_mm})")
    if s.wall_mm < s.min_feature_mm:
        issues.error("unprintable", f"settings.wall_mm ({s.wall_mm}) is thinner than "
                                    f"settings.min_feature_mm ({s.min_feature_mm})")
    if s.corner_radius_mm >= s.wall_mm:
        issues.error("invalid_value", "settings.corner_radius_mm must be smaller than settings.wall_mm")
    if not 0.2 <= s.bulk_packing_efficiency <= 0.9:
        issues.error("invalid_value", "settings.bulk_packing_efficiency must be between 0.2 and 0.9")
    if "headroom_mm" not in given:
        issues.warn("no_headroom", "settings.headroom_mm not given: no space is reserved under the lid. "
                                   "If a board or rulebook sits on top of the trays, measure that stack and "
                                   "set settings.headroom_mm, otherwise the lid may not close.")
    return s


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@dataclass
class Compartment:
    index: int
    name: str
    type: str
    data: dict
    tray: Optional[str] = None
    layer_pref: str = "any"
    type_inferred: bool = False
    cells: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)


@dataclass
class Plan:
    game: str
    box: tuple[float, float, float]
    bed: tuple[float, float, Optional[float]]
    settings: Settings
    compartments: list[Compartment]
    skipped: list[str]
    issues: Issues
    raw: dict
    mode: str = "trays"
    bottom_layer: list = field(default_factory=list)   # non-printed flat items under the trays

    @property
    def floor_z(self) -> float:
        """Height of the non-printed items lying on the box floor (e.g. a folded board)."""
        return sum(b["dimensions_mm"]["height"] * b["quantity"] for b in self.bottom_layer)

    @property
    def top_z(self) -> float:
        """Highest point a tray may reach (box height minus headroom under the lid)."""
        return self.box[2] - self.settings.headroom_mm

    @property
    def usable_height(self) -> float:
        """Height available for trays: between the bottom layer and the headroom."""
        return self.top_z - self.floor_z

    @property
    def max_tray_height(self) -> float:
        h = self.usable_height
        return min(h, self.bed[2]) if self.bed[2] else h

    @property
    def bottom_footprint(self) -> Optional[tuple[float, float]]:
        """(x, y) size of the top bottom-layer item, oriented to fit the box; None without one."""
        if not self.bottom_layer:
            return None
        d = self.bottom_layer[-1]["dimensions_mm"]
        W, L = self.box[0], self.box[1]
        return (d["width"], d["length"]) if d["width"] <= W + 1e-6 and d["length"] <= L + 1e-6 \
            else (d["length"], d["width"])

    @property
    def support_rect(self) -> Optional[tuple[float, float, float, float]]:
        """Floor area the bottom layer covers wherever it slides inside the box (x0, y0, x1, y1).

        A board of size b in a box of size B can sit anywhere in [0, B - b], so [B - b, b] is
        always covered. If that strip is narrower than 10 mm the board's position matters too
        much and it is assumed centred (reported as a warning by the pipeline).
        """
        fp = self.bottom_footprint
        if fp is None:
            return None
        out = []
        for size, b in ((self.box[0], fp[0]), (self.box[1], fp[1])):
            lo, hi = size - b, b
            if hi - lo < 10:
                lo, hi = (size - b) / 2, (size + b) / 2
            out.append((lo, hi))
        return (out[0][0], out[1][0], out[0][1], out[1][1])

    @property
    def bin_rect(self) -> tuple[float, float, float, float]:
        """Where trays may be placed (x0, y0, x1, y1): inside the box margin and, if the trays rest on
        a bottom layer, fully on top of it in every position it can slide to."""
        m = self.settings.box_margin_mm
        x0, y0, x1, y1 = m, m, self.box[0] - m, self.box[1] - m
        sr = self.support_rect
        if sr:
            x0, y0, x1, y1 = max(x0, sr[0]), max(y0, sr[1]), min(x1, sr[2]), min(y1, sr[3])
        return x0, y0, x1, y1

    @property
    def bin_size(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bin_rect
        return x1 - x0, y1 - y0

    @property
    def assumptions(self) -> list[str]:
        out = []
        for c in self.compartments:
            out.extend(c.assumptions)
        return out


def _resolve_type(c: dict, name: str, issues: Issues, mode: str = "trays") -> tuple[Optional[str], bool]:
    t = c.get("type")
    if mode == "boxes":
        # boxes mode: every compartment is one plain box (a set), never a shaped/subdivided tray
        if t is not None and TYPE_ALIASES.get(str(t).strip().lower()) != "set":
            issues.error("unsupported_in_boxes_mode",
                         f"{name}: type {t!r} is not available in organizer 'boxes' mode, which builds one plain "
                         f"open box per set. Give it as type 'set' with members (or required_mm).", name, "type")
            return None, False
        return "set", False
    if t is None:
        if "required_mm" not in c:
            issues.error("missing_type", f"{name}: give a 'type' ({', '.join(COMPONENT_TYPES)}) or a "
                                         f"'required_mm' bounding box", name, "type")
            return None, False
        for typ, rx in NAME_HINTS:
            if rx.search(name):
                return typ, True
        return "generic", False
    key = str(t).strip().lower().replace(" ", "_").replace("-", "_")
    if key not in TYPE_ALIASES:
        issues.error("invalid_value", f"{name}: unknown type {t!r}; use one of {', '.join(COMPONENT_TYPES)}",
                     name, "type")
        return None, False
    return TYPE_ALIASES[key], False


def parse_plan(plan: Any, *, allow_partial: bool = False) -> Plan:
    """Validate a plan dict and design every compartment's cells.

    Raises PlanError (a ValueError) listing *all* problems. With allow_partial,
    compartments that have their own errors are skipped (reported as warnings)
    and the rest continue; plan-wide errors are always fatal.
    """
    from .components import design_compartment  # local import: components imports this module

    issues = Issues()
    if not isinstance(plan, dict) or not isinstance(plan.get("compartments"), list):
        issues.error("invalid_plan", "Expected a JSON object with a 'compartments' list")
        raise PlanError(issues)

    box = dims(plan.get("box_inner_mm"), "box_inner_mm", ("width", "length", "height"), issues,
               why="measure the INSIDE of the game box (floor to lid underside)")
    bed_raw = plan.get("printer_bed_mm")
    bed = dims(bed_raw, "printer_bed_mm", ("width", "length"), issues,
               why="use your printer's build plate size")
    bed_h = None
    if isinstance(bed_raw, dict) and bed_raw.get("height") is not None:
        bed_h = num(bed_raw.get("height"), "printer_bed_mm.height", issues)
    elif bed is not None:
        issues.warn("no_printer_height", "printer_bed_mm.height not given: tray heights are not checked "
                                         "against the printer's Z limit")
    organizer = plan.get("organizer", "trays")
    if not isinstance(organizer, str) or not organizer.strip():
        issues.error("invalid_value", f"organizer must be a string such as 'boxes', got {organizer!r}")
        organizer = "trays"
    mode = "boxes" if organizer.strip().lower() == "boxes" else "trays"
    settings = parse_settings(plan.get("settings"), issues, mode)
    bottom = _parse_bottom_layer(plan.get("bottom_layer"), box, issues)
    floor_z = sum(b["dimensions_mm"]["height"] * b["quantity"] for b in bottom)

    if box is not None:
        m = settings.box_margin_mm
        if box["width"] <= 2 * m or box["length"] <= 2 * m:
            issues.error("box_too_small", "The box is too small for the requested box_margin_mm")
        if box["height"] - settings.headroom_mm - floor_z <= settings.floor_mm + 1:
            issues.error("box_too_small", f"box height {box['height']} mm minus headroom {settings.headroom_mm} mm "
                                          f"and bottom layer {floor_z:g} mm leaves no room for trays")

    if not plan["compartments"]:
        issues.error("invalid_plan", "Provide at least one compartment")

    compartments: list[Compartment] = []
    used = set()
    for i, c in enumerate(plan["compartments"]):
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            issues.error("invalid_value", f"compartments[{i}].name is required", None, f"compartments[{i}].name")
            continue
        name = str(c["name"]).strip()
        if name in used:
            issues.error("duplicate_name", f"Compartment name {name!r} is duplicated", name, "name")
            continue
        used.add(name)
        typ, inferred = _resolve_type(c, name, issues, mode)
        if typ is None:
            continue
        if mode == "boxes":
            bad_keys = [k for k in BOXES_FORBIDDEN_KEYS if c.get(k) not in (None, False)]
            if bad_keys:
                issues.error("unsupported_in_boxes_mode",
                             f"{name}: {', '.join(bad_keys)} would group or subdivide boxes; organizer 'boxes' "
                             f"builds exactly one open cavity per set", name, bad_keys[0])
                continue
        tray = c.get("tray", c.get("group"))
        tray = str(tray).strip() if tray not in (None, "") else None
        pref = str(c.get("layer", "any")).lower()
        if pref not in LAYER_PREFS:
            issues.error("invalid_value", f"{name}: layer must be one of {', '.join(LAYER_PREFS)}", name, "layer")
            pref = "any"
        comp = Compartment(index=i, name=name, type=typ, data=c, tray=tray, layer_pref=pref, type_inferred=inferred)
        if inferred:
            comp.assumptions.append(f"{name}: no type given; treated as '{typ}' from its name "
                                    f"(affects finger access features only, not dimensions)")
        compartments.append(comp)

    # Cell design needs the usable height. Without a sane box it still runs (with no height
    # limit) so every missing piece measurement is reported in the same pass.
    if box is not None and not any(i.code == "box_too_small" for i in issues.errors):
        usable = box["height"] - settings.headroom_mm - floor_z
        max_h = min(usable, bed_h) if bed_h else usable
        max_depth = max_h - settings.floor_mm
    else:
        max_depth = 1e6  # effectively unlimited; the cells are discarded because the plan fails
    limits = []
    if box is not None:
        limits.append((box["width"] - 2 * settings.box_margin_mm, box["length"] - 2 * settings.box_margin_mm))
    if bed is not None:
        limits.append((bed["width"], bed["length"]))
    for comp in compartments:
        comp.cells = design_compartment(comp, settings, max_depth, issues, limits)

    global_errors = [e for e in issues.errors if e.component is None]
    skipped: list[str] = []
    if allow_partial and not global_errors:
        bad = {e.component for e in issues.errors}
        if bad:
            skipped = [c.name for c in compartments if c.name in bad]
            for name in sorted(bad):
                issues.warn("skipped", f"{name}: skipped because of the errors above (--allow-partial)", name)
            # downgrade the per-component errors so they don't abort generation
            for it in issues.items:
                if it.severity == "error" and it.component in bad:
                    it.severity = "warning"
            compartments = [c for c in compartments if c.name not in bad]
            if not compartments:
                issues.error("invalid_plan", "No compartment could be generated")
    if box is not None and bottom:
        d = bottom[-1]["dimensions_mm"]
        fp = sorted((d["width"], d["length"]))
        if any(2 * b - size < 10 for b, size in zip(fp, sorted((box["width"], box["length"])))):
            issues.warn("bottom_layer_position", f"'{bottom[-1]['name']}' is much smaller than the box, so where it "
                                                 f"lies is unknown; it is assumed centred and the trays are placed "
                                                 f"on that centred area. Confirm its real position.")
    if issues.errors:
        raise PlanError(issues)
    return Plan(
        game=str(plan.get("game", "Untitled")),
        box=(box["width"], box["length"], box["height"]),
        bed=(bed["width"], bed["length"], bed_h),
        settings=settings,
        compartments=compartments,
        skipped=skipped,
        issues=issues,
        raw=plan,
        mode=mode,
        bottom_layer=bottom,
    )


def _parse_bottom_layer(raw: Any, box: Optional[dict], issues: Issues) -> list[dict]:
    """Non-printed flat items lying on the box floor UNDER the trays (e.g. a folded board).

    Their height raises every tray; it is not lid headroom. Each item needs measured
    length/width/height and must fit the box interior.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.error("invalid_value", "bottom_layer must be a list of items")
        return []
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            issues.error("invalid_value", f"bottom_layer[{i}] must be an object")
            continue
        name = str(item.get("name") or item.get("component_id") or f"bottom_layer[{i}]")
        d = dims(item.get("dimensions_mm"), f"bottom_layer[{i}].dimensions_mm", ("length", "width", "height"),
                 issues, why=f"measure '{name}' (it lies under the trays)")
        q = quantity(item.get("quantity", 1), f"bottom_layer[{i}].quantity", issues)
        if d is None or q is None:
            continue
        if box is not None and not (
                (d["width"] <= box["width"] + 1e-6 and d["length"] <= box["length"] + 1e-6) or
                (d["length"] <= box["width"] + 1e-6 and d["width"] <= box["length"] + 1e-6)):
            issues.error("too_large", f"bottom layer '{name}' ({d['length']:g} × {d['width']:g} mm) does not fit "
                                      f"the box interior")
            continue
        out.append({"name": name, "component_id": item.get("component_id"), "quantity": q, "dimensions_mm": d})
    return out
