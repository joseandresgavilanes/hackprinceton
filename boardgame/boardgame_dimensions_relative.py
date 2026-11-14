#!/usr/bin/env python3
"""
boardgame_dimensions_ruler.py

Measure a board game's components from YOUR OWN photos, each containing a
standard ruler. No photos are downloaded from the internet. The web is used
only (optionally) to look up the game's list of components and quantities.

For every photo
---------------
1. RULER    Claude reads two numbered tick marks on the ruler (coarse scale).
            OpenCV then measures the spacing of the ruler's printed tick marks
            (autocorrelation along the ruler) for a precise scale, and checks
            that the spacing is uniform along the ruler (a tilted camera makes
            it uneven -> photo rejected).
2. DETECT   Claude finds individual component instances and gives rough boxes.
3. MEASURE  OpenCV (GrabCut) outlines each item; pixel extents -> millimetres.
Then all photos are combined (median with outlier rejection) into one JSON
index: component, quantity, basic shape, dimensions in mm, confidence.

Photo guidelines (they matter more than any code)
-------------------------------------------------
* Camera directly above (top-down), phone held parallel to the table. Use no
  wide-angle/ultra-wide lens, and don't zoom with digital crop.
* Ruler lying flat on the table right next to the pieces, fully in frame,
  with at least ~10 cm of its numbered scale visible and in focus.
* Lay pieces out flat, separated (not stacked or touching). Several pieces per
  photo is fine; photograph several of each type if you can.
* For HEIGHT/THICKNESS: add a side-on photo (camera level with the table) with
  the pieces standing/stacked-as-single-items in front of a ruler laid in the
  same plane, ruler facing the camera.
* Tall pieces photographed from above look slightly bigger than the ruler's
  plane; keep the camera at least ~50 cm away if you have tall pieces.

Setup
-----
    pip install langchain-anthropic langchain-core pydantic opencv-python-headless \
                numpy pillow
    pip install pillow-heif          # optional: iPhone .heic photos
    export ANTHROPIC_API_KEY="..."

Usage
-----
    python boardgame_dimensions_ruler.py "Carcassonne" photos/
    python boardgame_dimensions_ruler.py "Carcassonne" a.jpg b.jpg side.jpg -o out.json --debug-dir debug/
    python boardgame_dimensions_ruler.py "My Game" photos/ --components "Meeple, Land tile, Rulebook"
"""

import argparse
import base64
import io
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

import cv2
import numpy as np
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

try:  # optional: iPhone HEIC support
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

MODEL = "claude-sonnet-5-5"
MAX_SIDE = 1568                      # working size sent to Claude / used for object outlines
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4}
METRIC_TICKS_MM = [1.0, 2.0, 5.0, 10.0]
IMPERIAL_TICKS_MM = [25.4 / 32, 25.4 / 16, 25.4 / 8, 25.4 / 4, 25.4 / 2, 25.4]
TILT_LIMIT = 1.06                    # tick spacing may differ by <6% between the two halves of the ruler
REFINE_AGREE = 0.12                  # tick-refined scale must agree with label-based scale within 12%


class Shape(str, Enum):
    RECTANGULAR_PRISM = "rectangular_prism"
    CUBE = "cube"
    CYLINDER = "cylinder"
    SPHERE = "sphere"
    CONE = "cone"
    HEXAGONAL_PRISM = "hexagonal_prism"
    TRIANGULAR_PRISM = "triangular_prism"


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class ComponentSpec(BaseModel):
    name: str = Field(description="Component name, e.g. 'Settlement piece'")
    quantity: Optional[int] = Field(None, description="How many are in the base game")
    shape_hint: Shape = Field(description="Best basic-shape approximation (fallback only)")


class ComponentList(BaseModel):
    game: str
    edition_assumed: Optional[str] = None
    components: list[ComponentSpec]


class Point(BaseModel):
    x: float
    y: float


class BBox(BaseModel):
    x_min: int
    y_min: int
    x_max: int
    y_max: int


class RulerReading(BaseModel):
    ruler_visible: bool
    unit: Literal["mm", "cm", "in"] = Field(description="Unit of the numbered scale you read (prefer cm)")
    point_a: Optional[Point] = Field(None, description="Pixel position of a numbered tick mark, ON the ruler's measuring edge")
    value_a: Optional[float] = Field(None, description="Number printed at point_a, in `unit`")
    point_b: Optional[Point] = Field(None, description="Pixel position of a second numbered tick, far from point_a")
    value_b: Optional[float] = Field(None, description="Number printed at point_b, in `unit`")
    notes: str = ""


class Detection(BaseModel):
    name: str = Field(description="Exactly one of the provided component names")
    bbox: BBox = Field(description="Tight pixel box around ONE single item")
    single_instance: bool = Field(description="True only if the box encloses one item, not a stack/pile/group")
    horizontal_dim: Literal["long", "short", "unknown"] = Field(
        "unknown", description="Side views only: is the item's horizontal extent its long or short dimension?")
    shape: Shape


class PhotoDetections(BaseModel):
    view: Literal["top_down", "side", "angled"]
    reason: str
    detections: list[Detection] = Field(default_factory=list)


RULER_SYSTEM = """You read rulers in photos. Find the ruler and report TWO numbered major tick marks so the photo's scale can be computed.

- If the ruler has several scales, choose ONE (prefer centimetres, otherwise inches) and set `unit` accordingly:
  'cm' if the numbers count centimetres (1, 2, 3 ...), 'mm' if they count millimetres (10, 20, 30 ...), 'in' for inches.
- point_a / point_b: pixel coordinates (origin top-left, x right, y down, in the image shown) of the exact
  position of two NUMBERED tick marks on the ruler's measuring edge, as FAR APART as possible along the ruler.
  value_a / value_b are the numbers printed at those ticks, in the chosen unit.
- The ruler may be at any angle. If no ruler with readable numbers is visible, set ruler_visible=false."""

DETECT_SYSTEM = """You are a computer-vision assistant. You get one photo (containing a ruler) and a list of a board game's components.
Find individual, fully visible instances of the listed components so they can be measured. Ignore the ruler itself.

For each listed component you can clearly see, return one or two detections (separate single items):
 - name: exactly as written in the list.
 - bbox: tight pixel box around that single item (origin top-left, x right, y down, in the image shown).
 - single_instance: true only if the box encloses ONE item (not a stack, pile, bundle, or group; not overlapped).
 - horizontal_dim: only for side views: whether the item's horizontal extent is its 'long' or 'short' dimension; else 'unknown'.
 - shape: the closest basic shape (rectangular_prism, cube, cylinder, sphere, cone, hexagonal_prism, triangular_prism).
   Irregular items (meeples, miniatures, ...) must be approximated by the closest of these.
Photo-level: view = 'top_down' (camera roughly above objects lying flat; mild tilt is fine), 'side' (camera roughly
level with the objects), or 'angled' (clearly oblique / strong perspective). reason = one short sentence.
Do not guess at components you cannot see."""

LIST_SYSTEM = (
    "You are a meticulous board game archivist. List every distinct physical component of the requested board "
    "game (base game, latest standard edition): boards, card types, tiles, tokens, dice, player pieces, rulebook, "
    "and the box. Group identical items (e.g. 'Settlement piece', quantity 20). DO NOT give any dimensions."
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def slugify(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_") or "x"


def writable_dir(preferred: Path, *fallbacks: Path) -> Path:
    for d in (preferred, *fallbacks):
        try:
            d.mkdir(parents=True, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            continue
    raise SystemExit("Could not find a writable directory. Use -o with a writable path.")


def _text_from(message) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def list_components(game: str, use_web: bool) -> ComponentList:
    llm = ChatAnthropic(model=MODEL, max_tokens=4000)
    research = llm
    if use_web:
        research = llm.bind_tools([{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])
    notes = _text_from(research.invoke([SystemMessage(content=LIST_SYSTEM), HumanMessage(content=f"Board game: {game}")]))
    return llm.with_structured_output(ComponentList).invoke([
        SystemMessage(content="Convert these notes into the schema. Do not invent components."),
        HumanMessage(content=f"Game: {game}\n\nNotes:\n{notes}"),
    ])


def collect_photos(paths: list[str]) -> list[Path]:
    out = []
    for p in map(lambda s: Path(s).expanduser(), paths):
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in PHOTO_EXTS)
        elif p.is_file():
            out.append(p)
        else:
            print(f"  (not found: {p})", file=sys.stderr)
    return out


def load_photo(path: Path):
    """Returns (full-res RGB PIL image, working-size RGB PIL image, f = full/working pixel ratio)."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")   # honour phone rotation metadata
    scale = MAX_SIDE / max(img.size)
    if scale >= 1:
        return img, img, 1.0
    work = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img, work, img.width / work.width


def _vision(llm, system: str, work: Image.Image, text: str):
    buf = io.BytesIO()
    work.save(buf, "JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": f"{text}\nImage size: {work.width}x{work.height} pixels."},
    ])
    try:
        return llm.invoke([SystemMessage(content=system), msg])
    except Exception as e:
        print(f"    vision call failed ({e})", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Ruler scale: coarse (labels) + precise (tick spacing)
# --------------------------------------------------------------------------
def _autocorr(p: np.ndarray) -> np.ndarray:
    n = len(p)
    p = p - p.mean()
    f = np.fft.rfft(p, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n] / (n - np.arange(n))     # unbiased
    return ac / ac[0] if ac[0] > 0 else ac


def estimate_scale(profile: np.ndarray, s0: float, cands_mm: list[float]):
    """Find the tick period in a 1-D profile along the ruler. s0 = coarse mm/px.
    Returns (mm_per_px, strength) or None."""
    n = len(profile)
    if n < 80:
        return None
    w = max(15, int(0.15 * n)) | 1
    prof = profile.astype(np.float32)
    prof = (prof - cv2.blur(prof.reshape(1, -1), (w, 1)).ravel()).astype(np.float64)   # high-pass
    ac = _autocorr(prof)
    found = []
    for c in cands_mm:
        e = c / s0
        lo, hi = int(0.85 * e), min(int(1.15 * e) + 1, n // 3)
        if e < 4 or hi - lo < 3 or lo < 2:
            continue
        i = int(np.argmax(ac[lo:hi])) + lo
        if i <= 1 or i >= n - 1 or not (ac[i - 1] <= ac[i] >= ac[i + 1]):
            continue
        den = ac[i - 1] - 2 * ac[i] + ac[i + 1]
        lag = i + (0.5 * (ac[i - 1] - ac[i + 1]) / den if den != 0 else 0.0)
        found.append((float(ac[i]), lag, c))
    if not found:
        return None
    best = max(f[0] for f in found)
    good = [f for f in found if f[0] >= max(0.2, 0.7 * best)]
    if not good:
        return None
    strength, lag, c = max(good, key=lambda f: f[1])      # longest lag = most periods averaged = most precise
    return c / lag, strength


def refine_scale(gray: np.ndarray, pa: np.ndarray, pb: np.ndarray, s0: float, unit: str):
    """Measure tick spacing along the ruler in the FULL-resolution grayscale image.
    pa, pb in that image's pixels; s0 = coarse mm/px there.
    Returns dict(scale, strength, tilt) or None."""
    cands = IMPERIAL_TICKS_MM if unit == "in" else METRIC_TICKS_MM
    d = pb - pa
    L = float(np.hypot(*d))
    if L < 100:
        return None
    H, W = gray.shape
    m = 0.35 * L
    x0, y0 = int(max(0, min(pa[0], pb[0]) - m)), int(max(0, min(pa[1], pb[1]) - m))
    x1, y1 = int(min(W, max(pa[0], pb[0]) + m)), int(min(H, max(pa[1], pb[1]) + m))
    crop = gray[y0:y1, x0:x1]
    a, b = pa - [x0, y0], pb - [x0, y0]
    ctr = (a + b) / 2
    ang = math.degrees(math.atan2(d[1], d[0]))
    for sign in (1, -1):                                   # pick the rotation that makes the ruler horizontal
        M = cv2.getRotationMatrix2D((float(ctr[0]), float(ctr[1])), sign * ang, 1.0)
        a2, b2 = M @ [a[0], a[1], 1.0], M @ [b[0], b[1], 1.0]
        if abs(b2[1] - a2[1]) < 0.02 * L:
            break
    else:
        return None
    if a2[0] > b2[0]:
        a2, b2 = b2, a2
    rot = cv2.warpAffine(crop, M, (crop.shape[1], crop.shape[0]), flags=cv2.INTER_LINEAR)
    gx = np.abs(cv2.Sobel(rot.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3))
    xs, xe = int(a2[0]), int(b2[0])
    yc = (a2[1] + b2[1]) / 2
    hb = max(8, int(0.03 * L))
    best = None
    for yb in np.arange(yc - 0.20 * L, yc + 0.20 * L, hb / 2):         # scan bands; keep the most periodic one
        yb = int(yb)
        if yb < 0 or yb + hb > gx.shape[0]:
            continue
        prof = gx[yb:yb + hb, xs:xe].mean(axis=0)
        est = estimate_scale(prof, s0, cands)
        if est and (best is None or est[1] > best[0][1]):
            best = (est, prof)
    if best is None:
        return None
    (scale, strength), prof = best
    half = len(prof) // 2
    left, right = estimate_scale(prof[:half], s0, cands), estimate_scale(prof[half:], s0, cands)
    tilt = max(left[0], right[0]) / min(left[0], right[0]) if left and right else None
    return {"scale": scale, "strength": strength, "tilt": tilt}


def ruler_to_scale(rr: RulerReading, work: Image.Image, f: float, full_gray: np.ndarray):
    """Returns (mm_per_working_px, info_dict) or (None, reason)."""
    if not rr.ruler_visible or not (rr.point_a and rr.point_b) or rr.value_a is None or rr.value_b is None:
        return None, f"no_ruler: {rr.notes or 'no readable ruler found'}"
    pa, pb = np.array([rr.point_a.x, rr.point_a.y]), np.array([rr.point_b.x, rr.point_b.y])
    dist = float(np.hypot(*(pb - pa)))
    span_mm = abs(rr.value_b - rr.value_a) * UNIT_MM[rr.unit]
    if dist < 0.15 * max(work.size) or span_mm <= 0:
        return None, "bad_ruler_reading: ruler ticks too close together to calibrate"
    s0 = span_mm / dist                                    # coarse mm per working pixel
    if not 0.02 <= s0 <= 5.0 * 1568 / max(work.size) * 3:  # sanity: image spans roughly 3 cm .. a few metres
        return None, f"bad_ruler_reading: implausible scale ({s0:.3f} mm/px)"
    info = {"unit": rr.unit, "scale_source": "labels_only", "tick_strength": None, "tilt_ratio": None}
    ref = refine_scale(full_gray, pa * f, pb * f, s0 / f, rr.unit)
    s = s0
    if ref:
        s_ref = ref["scale"] * f                           # mm per working px
        if abs(s_ref / s0 - 1) <= REFINE_AGREE:
            s = s_ref
            info.update(scale_source="tick_refined", tick_strength=round(ref["strength"], 2))
        if ref["tilt"]:
            info["tilt_ratio"] = round(ref["tilt"], 3)
            if ref["tilt"] > TILT_LIMIT:
                return None, f"tilted_camera: tick spacing varies {100 * (ref['tilt'] - 1):.0f}% along the ruler"
    info["mm_per_px"] = s
    info["coarse_mm_per_px"] = s0
    return s, info


# --------------------------------------------------------------------------
# Object outlines & measurement
# --------------------------------------------------------------------------
def _clip_bbox(b: BBox, W: int, H: int) -> Optional[BBox]:
    x0, y0 = max(0, min(b.x_min, b.x_max)), max(0, min(b.y_min, b.y_max))
    x1, y1 = min(W, max(b.x_min, b.x_max)), min(H, max(b.y_min, b.y_max))
    return BBox(x_min=x0, y_min=y0, x_max=x1, y_max=y1) if x1 - x0 >= 12 and y1 - y0 >= 12 else None


def _iou(a: BBox, b: BBox) -> float:
    ix = max(0, min(a.x_max, b.x_max) - max(a.x_min, b.x_min))
    iy = max(0, min(a.y_max, b.y_max) - max(a.y_min, b.y_min))
    inter = ix * iy
    union = (a.x_max - a.x_min) * (a.y_max - a.y_min) + (b.x_max - b.x_min) * (b.y_max - b.y_min) - inter
    return inter / union if union else 0.0


def segment(img_bgr: np.ndarray, bbox: BBox, pad: float = 0.15, border: int = 10):
    """Refine a rough box into a precise outline with GrabCut. Returns a contour or None."""
    H, W = img_bgr.shape[:2]
    bw, bh = bbox.x_max - bbox.x_min, bbox.y_max - bbox.y_min
    x0, y0 = max(0, int(bbox.x_min - pad * bw)), max(0, int(bbox.y_min - pad * bh))
    x1, y1 = min(W, int(bbox.x_max + pad * bw)), min(H, int(bbox.y_max + pad * bh))
    crop = cv2.copyMakeBorder(img_bgr[y0:y1, x0:x1], border, border, border, border, cv2.BORDER_REPLICATE)
    rect = (bbox.x_min - x0 + border, bbox.y_min - y0 + border, bw, bh)
    mask = np.zeros(crop.shape[:2], np.uint8)
    try:
        cv2.grabCut(crop, mask, rect, np.zeros((1, 65)), np.zeros((1, 65)), 5, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    fg = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fg = cv2.morphologyEx(cv2.morphologyEx(fg, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if not 0.25 <= cv2.contourArea(c) / (bw * bh) <= 1.15:
        return None
    return c + np.array([[[x0 - border, y0 - border]]], dtype=c.dtype)


def measure_photo(work: Image.Image, pd: PhotoDetections, name_map: dict, s: float, photo: str,
                  scale_source: str, debug_path: Optional[Path] = None, ruler_pts=None):
    """Returns (measurements, 'code: detail'). Each measurement is one dimension of one item, in mm."""
    if pd.view == "angled":
        return [], f"angled_view: {pd.reason}"
    bgr = cv2.cvtColor(np.array(work), cv2.COLOR_RGB2BGR)
    H, W = bgr.shape[:2]
    dbg = bgr.copy() if debug_path else None
    boxes, out = [], []

    def add(comp, kind, px, shape):
        mm = px * s
        if 1.0 <= mm <= 1000.0:
            out.append({"photo": photo, "comp": comp, "kind": kind, "mm": mm, "shape": shape, "src": scale_source})

    for d in pd.detections:
        comp = name_map.get(d.name.strip().lower())
        if not comp or not d.single_instance:
            continue
        box = _clip_bbox(d.bbox, W, H)
        if not box or any(_iou(box, b) > 0.3 for b in boxes):
            continue
        c = segment(bgr, box)
        if c is None:
            continue
        boxes.append(box)
        (_, _), (rw, rh), _ = cv2.minAreaRect(c)
        _, _, bw, bh = cv2.boundingRect(c)
        if pd.view == "top_down":
            add(comp, "long", max(rw, rh), d.shape.value)
            add(comp, "short", min(rw, rh), d.shape.value)
        else:
            add(comp, "height", bh, d.shape.value)
            if d.horizontal_dim in ("long", "short"):
                add(comp, d.horizontal_dim, bw, d.shape.value)
        if dbg is not None:
            cv2.drawContours(dbg, [c], -1, (0, 0, 255), 2)
            cv2.putText(dbg, comp[:24], (box.x_min, max(15, box.y_min - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    if dbg is not None:
        if ruler_pts:
            for p in ruler_pts:
                cv2.circle(dbg, (int(p[0]), int(p[1])), 8, (255, 0, 0), 3)
        cv2.putText(dbg, f"{s:.4f} mm/px ({scale_source})", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 3)
        cv2.imwrite(str(debug_path), dbg)
    n = len({o["comp"] for o in out})
    if n == 0:
        return [], f"nothing_measured: no component could be outlined; {pd.reason}"
    return out, f"ok: {n} component type(s) measured ({pd.view})"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def robust(vals: list[float]):
    """Median after rejecting >25% outliers. Returns (value, n_kept, relative_spread)."""
    if not vals:
        return None, 0, None
    v = np.array(vals, dtype=float)
    if len(v) >= 4:
        med = np.median(v)
        keep = np.abs(v - med) / med <= 0.25
        if keep.sum() >= 2:
            v = v[keep]
    spread = float(np.std(v) / np.mean(v)) if len(v) > 1 else None
    return float(np.median(v)), len(v), spread


def build_dimensions(shape: Shape, long, short, height) -> dict:
    vals = [x for x in (long, short, height) if x is not None]
    avg = lambda *xs: float(np.mean([x for x in xs if x is not None]))
    if shape in (Shape.CYLINDER, Shape.CONE, Shape.HEXAGONAL_PRISM):
        d = {"diameter": avg(long, short) if (long or short) else None, "height": height}
    elif shape == Shape.SPHERE:
        d = {"diameter": avg(*vals) if vals else None}
    elif shape == Shape.CUBE:
        e = avg(*vals) if vals else None
        d = {"length": e, "width": e, "height": e}
    else:
        d = {"length": long, "width": short, "height": height}
    return {k: round(v, 1) for k, v in d.items() if v is not None}


def aggregate(spec: ComponentSpec, ms: list[dict]) -> dict:
    by = defaultdict(list)
    for m in ms:
        by[m["kind"]].append(m["mm"])
    long_v, n1, s1 = robust(by["long"])
    short_v, n2, s2 = robust(by["short"])
    height_v, n3, s3 = robust(by["height"])
    shape = Shape(Counter(m["shape"] for m in ms).most_common(1)[0][0])
    n = max(n1, n2, n3)
    spreads = [s for s in (s1, s2, s3) if s is not None]
    spread = max(spreads) if spreads else None
    if n >= 4 and spread is not None and spread < 0.05:
        conf = 2
    elif n >= 2 and spread is not None and spread < 0.10:
        conf = 1
    else:
        conf = 0
    labels_only = sum(m["src"] == "labels_only" for m in ms) / len(ms)
    note = []
    if labels_only > 0.5:
        conf = max(0, conf - 1)
        note.append("Scale came mostly from ruler labels only (tick marks not resolved); less precise.")
    if height_v is None and shape != Shape.SPHERE:
        note.append("Height/thickness not measured (needs a side-on photo with a ruler).")
    return {"shape": shape.value, "dimensions_mm": build_dimensions(shape, long_v, short_v, height_v),
            "confidence": ["low", "medium", "high"][conf], "notes": " ".join(note) or None,
            "measurement": {"n_measurements": n, "relative_spread": None if spread is None else round(spread, 3)}}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Measure board game components from your own photos with a ruler.")
    p.add_argument("game", help="board game name (used to look up its component list)")
    p.add_argument("photos", nargs="+", help="photo files and/or folders (no internet photos are used)")
    p.add_argument("-o", "--output")
    p.add_argument("--components", help="comma-separated component names; skips the automatic component lookup")
    p.add_argument("--no-web", action="store_true", help="don't use web search for the component list")
    p.add_argument("--debug-dir", help="save annotated copies of each photo (ruler points, outlines, scale)")
    args = p.parse_args()

    photos = collect_photos(args.photos)
    if not photos:
        raise SystemExit("No photos found. Pass image files or a folder of images.")
    debug_dir = writable_dir(Path(args.debug_dir), Path.home() / "boardgame_debug") if args.debug_dir else None

    if args.components:
        specs = [ComponentSpec(name=n.strip(), shape_hint=Shape.RECTANGULAR_PRISM)
                 for n in args.components.split(",") if n.strip()]
        comp_list = ComponentList(game=args.game, components=specs)
    else:
        print(f"Looking up components of {args.game}...", file=sys.stderr)
        comp_list = list_components(args.game, not args.no_web)
    specs = comp_list.components
    names = [s.name for s in specs]
    name_map = {n.lower(): n for n in names}
    print(f"  {len(specs)} components: {', '.join(names)}", file=sys.stderr)

    llm = ChatAnthropic(model=MODEL, max_tokens=3000)
    ruler_llm, detect_llm = llm.with_structured_output(RulerReading), llm.with_structured_output(PhotoDetections)

    all_ms, photo_log = [], []
    for i, path in enumerate(photos, 1):
        print(f"[{i}/{len(photos)}] {path.name}", file=sys.stderr)
        entry = {"file": path.name}
        try:
            full, work, f = load_photo(path)
        except Exception as e:
            entry.update(status="rejected", reason=f"unreadable_image: {e}")
            photo_log.append(entry)
            print(f"    skip unreadable_image: {e}", file=sys.stderr)
            continue
        rr = _vision(ruler_llm, RULER_SYSTEM, work, "Find the ruler and read two numbered ticks.")
        if rr is None:
            entry.update(status="rejected", reason="vision_call_failed")
            photo_log.append(entry)
            continue
        gray = cv2.cvtColor(np.array(full), cv2.COLOR_RGB2GRAY)
        s, info = ruler_to_scale(rr, work, f, gray)
        if s is None:
            entry.update(status="rejected", reason=info)
            photo_log.append(entry)
            print(f"    skip {info}", file=sys.stderr)
            continue
        listing = "\n".join(f"- {n}" for n in names)
        pd = _vision(detect_llm, DETECT_SYSTEM, work, f"Game: {args.game}\nComponents:\n{listing}")
        if pd is None:
            entry.update(status="rejected", reason="vision_call_failed")
            photo_log.append(entry)
            continue
        dbg = debug_dir / f"{slugify(path.stem)}_debug.jpg" if debug_dir else None
        pts = [(rr.point_a.x, rr.point_a.y), (rr.point_b.x, rr.point_b.y)]
        ms, why = measure_photo(work, pd, name_map, s, path.name, info["scale_source"], dbg, pts)
        print(f"    {'OK  ' if ms else 'skip'} {why}  [scale {s:.4f} mm/px, {info['scale_source']}, tilt={info['tilt_ratio']}]",
              file=sys.stderr)
        entry.update(status="used" if ms else "rejected", reason=why, mm_per_pixel=round(s, 5),
                     scale_source=info["scale_source"], tick_strength=info["tick_strength"],
                     tilt_ratio=info["tilt_ratio"], items_measured=len(ms))
        photo_log.append(entry)
        all_ms += ms

    by_comp = defaultdict(list)
    for m in all_ms:
        by_comp[m["comp"]].append(m)
    out = []
    for i, spec in enumerate(specs, 1):
        e = {"index": i, "name": spec.name, "quantity": spec.quantity}
        if by_comp[spec.name]:
            e.update(aggregate(spec, by_comp[spec.name]))
        else:
            e.update({"shape": spec.shape_hint.value, "dimensions_mm": None, "confidence": "none",
                      "notes": "Not found/measurable in the supplied photos."})
        out.append(e)

    result = {
        "game": comp_list.game,
        "edition_assumed": comp_list.edition_assumed,
        "units": "mm",
        "method": "User-supplied photos with a ruler; scale from ruler tick spacing (OpenCV); outlines via GrabCut.",
        "photos": photo_log,
        "components": out,
    }
    path = Path(args.output).expanduser() if args.output else \
        writable_dir(Path.cwd(), Path.home()) / f"{slugify(args.game)}_dimensions.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    used = sum(1 for e in photo_log if e["status"] == "used")
    print(f"\nWrote {path.resolve()}: {sum(1 for c in out if c['dimensions_mm'])}/{len(out)} components measured "
          f"from {used}/{len(photos)} photos.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())