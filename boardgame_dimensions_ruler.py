#!/usr/bin/env python3
"""
boardgame_dimensions_ruler.py  (v2: perspective-corrected)

Measure a board game's components from YOUR OWN photos, each containing a
standard ruler. No photos are downloaded from the internet. The web is used
only (optionally) to look up the game's list of components and quantities.

Angled photos are CORRECTED, not rejected
-----------------------------------------
A ruler is a rectangle, so its four corners in the photo determine the plane
it lies on:
  1. Claude marks the ruler's 4 corners; OpenCV refines them (GrabCut + polygon fit).
  2. The camera focal length comes from the photo's EXIF data (or, failing that,
     from the ruler's vanishing points, or a typical-phone default). With it the
     ruler's true 3-D shape is recovered, giving the plane's tilt and a
     homography that maps any pixel on that plane to real millimetres.
  3. SCALE (three tiers, best available is used):
       ticks   - tick marks measured on the RECTIFIED ruler (spacing is uniform
                 again after correction) -> precise
       labels  - two numbered ticks read by Claude
       assumed - the ruler body is about 12 in (304.8 mm); change with --ruler-length
     Tick/label scales are only used to refine the assumed length, so slightly
     distorted ticks no longer matter.
  4. Each piece is outlined (GrabCut), its outline is mapped through the
     homography into millimetres, and its size is measured there.
If the ruler's corners are not all visible, the program falls back to a plain
(uncorrected) scale from tick marks/labels and lowers the confidence.
Only extremely oblique photos (camera ~72 deg or more from straight-on) are rejected.

Photo guidelines
----------------
* The WHOLE ruler (all four corners) should be in frame, lying flat on the same
  surface as the pieces. Any camera angle is fine, but closer to straight-down is
  more accurate. Avoid ultra-wide lenses.
* Pieces flat and separated (not stacked or touching).
* Tall pieces photographed at a steep angle look smeared; shoot those from higher up.
* For HEIGHT/THICKNESS, add a photo with the ruler standing upright facing the
  camera and the pieces standing in front of it, in the same plane.
* Keep phone EXIF data (don't strip metadata): it contains the focal length.

Setup
-----
    pip install langchain-anthropic langchain-core pydantic opencv-python-headless \
                numpy pillow
    pip install pillow-heif          # optional: iPhone .heic photos
    export ANTHROPIC_API_KEY="..."

Usage
-----
    python boardgame_dimensions_ruler.py "Carcassonne" photos/
    python boardgame_dimensions_ruler.py "Carcassonne" a.jpg b.jpg -o out.json --debug-dir debug/
    python boardgame_dimensions_ruler.py "My Game" photos/ --components "Meeple, Land tile" --ruler-length 30cm
"""

import argparse
import base64
import io
import json
import math
import os
import re
import sys
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
MAX_SIDE = 1568                      # working size sent to Claude / used for outlines
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4}
METRIC_TICKS_MM = [1.0, 2.0, 5.0, 10.0]
IMPERIAL_TICKS_MM = [25.4 / 32, 25.4 / 16, 25.4 / 8, 25.4 / 4, 25.4 / 2, 25.4]
ASSUMED_RULER_LENGTH_MM = 304.8      # "about 12 inches"
MAX_TILT_DEG = 72.0                  # only photos more oblique than this are rejected
REFINE_AGREE = 0.15                  # tick scale must agree with the approximate scale within 15%
EDGE_ERODE_PX = 1                    # outline correction, in pixels of the frame used for outlining
SOURCE_PENALTY = {"ticks": 0, "labels": 1, "assumed_length": 2}


class Shape(str, Enum):
    RECTANGULAR_PRISM = "rectangular_prism"
    CUBE = "cube"
    CYLINDER = "cylinder"
    SPHERE = "sphere"
    CONE = "cone"
    HEXAGONAL_PRISM = "hexagonal_prism"
    TRIANGULAR_PRISM = "triangular_prism"


# --------------------------------------------------------------------------
# Schemas & prompts
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
    fully_visible: bool = Field(description="True only if all four corners of the ruler's body are visible in the photo")
    unit: Literal["mm", "cm", "in"] = Field(description="Unit of the numbered scale you read (prefer cm)")
    corner_start_a: Optional[Point] = Field(None, description="Corner at the LOW-number end, on the long edge that carries the tick marks")
    corner_end_a: Optional[Point] = Field(None, description="Corner at the HIGH-number end, same long edge as corner_start_a")
    corner_end_b: Optional[Point] = Field(None, description="Corner at the HIGH-number end, on the opposite long edge")
    corner_start_b: Optional[Point] = Field(None, description="Corner at the LOW-number end, on the opposite long edge")
    point_a: Optional[Point] = Field(None, description="Pixel position of a numbered tick mark on the measuring edge")
    value_a: Optional[float] = Field(None, description="Number printed at point_a, in `unit`")
    point_b: Optional[Point] = Field(None, description="Pixel position of a second numbered tick, far from point_a")
    value_b: Optional[float] = Field(None, description="Number printed at point_b, in `unit`")
    notes: str = ""


class Detection(BaseModel):
    name: str = Field(description="Exactly one of the provided component names")
    bbox: BBox = Field(description="Tight pixel box around ONE single item")
    single_instance: bool = Field(description="True only if the box encloses one item, not a stack/pile/group")
    horizontal_dim: Literal["long", "short", "unknown"] = Field(
        "unknown", description="Upright photos only: is the item's horizontal extent its long or short dimension?")
    shape: Shape


class PhotoDetections(BaseModel):
    view: Literal["flat", "upright"]
    reason: str
    detections: list[Detection] = Field(default_factory=list)


RULER_SYSTEM = """You read rulers in photos. The photo may be taken from any angle, so the ruler can look skewed (a trapezoid).

Report:
1. unit: the numbered scale you will read: 'cm' if numbers count centimetres (1, 2, 3 ...), 'mm' if they count
   millimetres (10, 20 ...), 'in' for inches. Prefer centimetres if the ruler has both.
2. The FOUR CORNERS of the ruler's physical body (its outer outline), as pixel coordinates in the image shown
   (origin top-left, x right, y down), as precisely as you can:
     corner_start_a: the end with the LOWEST numbers (the 0 end), on the long edge that carries the tick marks
     corner_end_a:   the HIGH-number end, same long edge as corner_start_a
     corner_end_b:   the HIGH-number end, on the other long edge
     corner_start_b: the LOW-number end, on the other long edge
   Set fully_visible=false (and you may omit corners) unless all four corners are visible and unobstructed.
3. point_a / point_b: pixel positions of two NUMBERED tick marks on the measuring edge, as FAR APART as possible,
   with value_a / value_b the numbers printed there in the chosen unit.
If no ruler with readable numbers is visible, set ruler_visible=false."""

DETECT_SYSTEM = """You are a computer-vision assistant. You get one photo (containing a ruler) and a list of a board game's components.
Find individual, fully visible instances of the listed components so they can be measured. Ignore the ruler itself.

For each listed component you can clearly see, return one or two detections (separate single items):
 - name: exactly as written in the list.
 - bbox: tight pixel box around that single item (origin top-left, x right, y down, in the image shown).
 - single_instance: true only if the box encloses ONE item (not a stack, pile, bundle, or group; not overlapped).
 - horizontal_dim: only for 'upright' photos: whether the item's horizontal extent is its 'long' or 'short' dimension; else 'unknown'.
 - shape: the closest basic shape (rectangular_prism, cube, cylinder, sphere, cone, hexagonal_prism, triangular_prism).
   Irregular items (meeples, miniatures, ...) must be approximated by the closest of these.
Photo-level: view = 'flat' if the pieces and the ruler lie flat on the same surface (ANY camera angle, even oblique),
or 'upright' if the ruler stands upright facing the camera with pieces standing in front of it in the same plane
(a photo for measuring heights). reason = one short sentence.
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


def parse_length(text: str) -> Optional[float]:
    """'304.8', '12in', '30 cm' -> millimetres."""
    m = re.fullmatch(r'\s*([0-9]*\.?[0-9]+)\s*(mm|cm|m|in|inch|inches|")?\s*', text.lower())
    if not m:
        return None
    unit = {"mm": 1, "cm": 10, "m": 1000, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4}[m.group(2) or "mm"]
    v = float(m.group(1)) * unit
    return v if v > 0 else None


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
    """Returns (full-res RGB image, working-size RGB image, f = full/working pixel ratio, 35mm-equiv focal or None)."""
    raw = Image.open(path)
    f35 = None
    try:
        v = raw.getexif().get_ifd(0x8769).get(41989)          # FocalLengthIn35mmFilm
        f35 = float(v) if v else None
    except Exception:
        pass
    img = ImageOps.exif_transpose(raw).convert("RGB")         # honour phone rotation metadata
    scale = MAX_SIDE / max(img.size)
    if scale >= 1:
        return img, img, 1.0, f35
    work = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img, work, img.width / work.width, f35


def focal_px_from_35mm(f35: Optional[float], size) -> Optional[float]:
    """35mm-equivalent focal length -> focal length in pixels of an image of `size` (uncropped)."""
    return f35 / 43.27 * math.hypot(*size) if f35 else None


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


def persp(H: np.ndarray, pts) -> np.ndarray:
    pts = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


# --------------------------------------------------------------------------
# Tick-mark period estimation
# --------------------------------------------------------------------------
def _autocorr(p: np.ndarray) -> np.ndarray:
    n = len(p)
    p = p - p.mean()
    f = np.fft.rfft(p, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n] / (n - np.arange(n))     # unbiased
    return ac / ac[0] if ac[0] > 0 else ac


def estimate_scale(profile: np.ndarray, s0: float, cands_mm: list[float]):
    """Find the tick period in a 1-D profile along the ruler. s0 = approximate mm/px.
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


def tick_scale_rectified(gray_full, H_full_mm, L_mm, W_mm, kappa_guess, unit):
    """Measure tick spacing on the RECTIFIED ruler.
    H_full_mm maps full-res photo pixels -> nominal millimetres (origin at the ruler's start corner).
    Returns dict(kappa, strength, uniformity) or None. kappa = true mm / nominal mm."""
    cands = IMPERIAL_TICKS_MM if unit == "in" else METRIC_TICKS_MM
    ppm = min(10.0, 3600.0 / L_mm)
    size = (int(L_mm * ppm), int(W_mm * ppm))
    if size[0] < 200 or size[1] < 20:
        return None
    rect = cv2.warpPerspective(gray_full, np.diag([ppm, ppm, 1.0]) @ H_full_mm, size, flags=cv2.INTER_LINEAR)
    gx = np.abs(cv2.Sobel(rect.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3))
    xs, xe = int(0.02 * size[0]), int(0.98 * size[0])
    hb = max(8, int(2.0 * ppm))
    s0 = kappa_guess / ppm                                  # approx real mm per rectified pixel
    best = None
    for yb in range(0, size[1] - hb, max(1, hb // 2)):
        prof = gx[yb:yb + hb, xs:xe].mean(axis=0)
        est = estimate_scale(prof, s0, cands)
        if est and (best is None or est[1] > best[0][1]):
            best = (est, prof)
    if best is None:
        return None
    (s_rect, strength), prof = best
    half = len(prof) // 2
    left, right = estimate_scale(prof[:half], s0, cands), estimate_scale(prof[half:], s0, cands)
    uni = max(left[0], right[0]) / min(left[0], right[0]) if left and right else None
    return {"kappa": s_rect * ppm, "strength": strength, "uniformity": uni}


def refine_scale_scalar(gray, pa, pb, s0, unit):
    """Fallback (no rectification): tick spacing along the line pa->pb in the full-res image.
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
    for sign in (1, -1):
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
    for yb in np.arange(yc - 0.20 * L, yc + 0.20 * L, hb / 2):
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


# --------------------------------------------------------------------------
# Perspective correction from the ruler rectangle
# --------------------------------------------------------------------------
def _intersect(l1, l2):
    """Intersection of two lines given as (vx, vy, x0, y0)."""
    A = np.array([[l1[0], -l2[0]], [l1[1], -l2[1]]], np.float64)
    if abs(np.linalg.det(A)) < 1e-9:
        return None
    t = np.linalg.solve(A, np.array([l2[2] - l1[2], l2[3] - l1[3]], np.float64))
    return np.array([l1[2] + t[0] * l1[0], l1[3] + t[0] * l1[1]])


def _ransac_line(pts: np.ndarray, tol: float = 1.5, iters: int = 300, seed: int = 0):
    """Robust straight line through pts (N,2). Returns ((vx,vy,x0,y0), inlier_fraction) or (None, 0)."""
    n = len(pts)
    if n < 8:
        return None, 0.0
    rng = np.random.default_rng(seed)
    best_mask, best_cnt = None, 0
    for _ in range(iters):
        i, j = rng.choice(n, 2, replace=False)
        d = pts[j] - pts[i]
        L = np.linalg.norm(d)
        if L < 1e-6:
            continue
        dist = np.abs(d[0] * (pts[:, 1] - pts[i][1]) - d[1] * (pts[:, 0] - pts[i][0])) / L
        mask = dist < tol
        if mask.sum() > best_cnt:
            best_mask, best_cnt = mask, int(mask.sum())
    if best_mask is None:
        return None, 0.0
    vx, vy, x0, y0 = cv2.fitLine(pts[best_mask].astype(np.float32), cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    return (float(vx), float(vy), float(x0), float(y0)), best_cnt / n


def refine_quad_edges(gray: np.ndarray, quad: np.ndarray):
    """Sub-pixel refinement of a quadrilateral using brightness steps along each side.
    For each side, sample many positions; along the outward normal pick the OUTERMOST strong step
    (so tick marks that start at the ruler's edge cannot pull the edge inwards); fit a line robustly (RANSAC).
    Returns the 4 refined corners, or None."""
    g = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0)
    q = np.asarray(quad, np.float64)
    cen = q.mean(0)
    min_edge = min(np.linalg.norm(q[(i + 1) % 4] - q[i]) for i in range(4))
    delta = max(8.0, 0.22 * min_edge)
    offs = np.arange(-delta, delta + 0.01, 0.5)
    lines = []
    for i in range(4):
        A, B = q[i], q[(i + 1) % 4]
        d = B - A
        L = np.linalg.norm(d)
        u = d / L
        nrm = np.array([-u[1], u[0]])
        if nrm @ ((A + B) / 2 - cen) < 0:
            nrm = -nrm                                     # outward-pointing normal
        ts = np.linspace(0.10, 0.90, 90)
        base = A[None, :] + ts[:, None] * d[None, :]
        X = (base[:, None, 0] + offs[None, :] * nrm[0]).astype(np.float32)
        Y = (base[:, None, 1] + offs[None, :] * nrm[1]).astype(np.float32)
        prof = cv2.remap(g, X, Y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        grad = np.abs(np.gradient(prof, axis=1))
        pts = []
        for k in range(len(ts)):
            a = grad[k]
            thr = max(8.0, 0.5 * a.max())
            peaks = [j for j in range(1, len(a) - 1) if a[j] >= thr and a[j] >= a[j - 1] and a[j] >= a[j + 1]]
            if not peaks:
                continue
            j = max(peaks, key=lambda jj: offs[jj])        # outermost strong step
            pts.append(base[k] + offs[j] * nrm)
        line, frac = _ransac_line(np.array(pts)) if len(pts) >= 12 else (None, 0.0)
        if line is None or frac < 0.35:
            return None
        lines.append(line)
    out = []
    for i in range(4):
        c = _intersect(lines[(i - 1) % 4], lines[i])
        if c is None or np.linalg.norm(c - q[i]) > 2.0 * delta:
            return None
        out.append(c)
    return np.array(out)


def refine_quad(bgr: np.ndarray, quad: np.ndarray, gray_full: Optional[np.ndarray] = None,
                f_ratio: float = 1.0) -> Optional[np.ndarray]:
    """Snap Claude's rough ruler corners to the ruler's actual outline:
    GrabCut -> rough 4-vertex polygon -> robust straight-line fit along each of the 4 sides -> intersect."""
    H, W = bgr.shape[:2]
    q = np.asarray(quad, np.float32)
    cen = q.mean(0)
    ext = np.abs(q - cen).max(0) * 1.5 + 10
    x0, y0 = np.maximum(0, (cen - ext)).astype(int)
    x1, y1 = int(min(W, cen[0] + ext[0])), int(min(H, cen[1] + ext[1]))
    crop = bgr[y0:y1, x0:x1]
    if crop.shape[0] < 40 or crop.shape[1] < 40:
        return None
    off = np.array([x0, y0], np.float32)

    def poly(k):
        return np.round(cen + (q - cen) * k - off).astype(np.int32)
    mask = np.full(crop.shape[:2], cv2.GC_BGD, np.uint8)
    cv2.fillPoly(mask, [poly(1.25)], int(cv2.GC_PR_BGD))
    cv2.fillPoly(mask, [poly(1.0)], int(cv2.GC_PR_FGD))
    cv2.fillPoly(mask, [poly(0.5)], int(cv2.GC_FGD))
    try:
        cv2.grabCut(crop, mask, None, np.zeros((1, 65)), np.zeros((1, 65)), 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    fg = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fg = cv2.morphologyEx(cv2.morphologyEx(fg, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(cnt)
    peri = cv2.arcLength(hull, True)
    poly4 = None
    for eps in np.linspace(0.01, 0.08, 15):
        ap = cv2.approxPolyDP(hull, eps * peri, True)
        if len(ap) == 4:
            poly4 = ap.reshape(4, 2).astype(np.float64)
            break
    if poly4 is None:
        return None
    area_new, area_old = cv2.contourArea(poly4.astype(np.float32) + off), cv2.contourArea(q)
    if not 0.7 <= area_new / max(area_old, 1) <= 1.35:
        return None
    Lq = float(np.linalg.norm(q[1] - q[0]))
    order = []
    for p in q:                                    # match each of Claude's corners to the nearest fitted vertex
        d = np.linalg.norm(poly4 + off - p, axis=1)
        order.append(int(np.argmin(d)))
        if d.min() > 0.10 * Lq:
            return None
    if len(set(order)) != 4:
        return None
    V = poly4[order]                               # rough vertices in Claude's order (crop coordinates)

    # ---- straight-edge refinement: robust line fit through the outline points along each side ----
    pts = cnt.reshape(-1, 2).astype(np.float64)
    min_edge = min(np.linalg.norm(V[(i + 1) % 4] - V[i]) for i in range(4))
    thr = max(6.0, 0.12 * min_edge)
    lines = []
    for i in range(4):
        A, B = V[i], V[(i + 1) % 4]
        d = B - A
        Ls = float(np.linalg.norm(d))
        t = ((pts - A) @ d) / (Ls * Ls)
        dist = np.abs(d[0] * (pts[:, 1] - A[1]) - d[1] * (pts[:, 0] - A[0])) / Ls
        sel = pts[(t > 0.08) & (t < 0.92) & (dist < thr)]
        if len(sel) < 15:
            return V + off
        vx, vy, px, py = cv2.fitLine(sel.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((float(vx), float(vy), float(px), float(py)))
    out = []
    for i in range(4):                             # corner i = intersection of side i-1 and side i
        c = _intersect(lines[(i - 1) % 4], lines[i])
        if c is None or np.linalg.norm(c - V[i]) > max(10.0, 0.25 * min_edge):
            out = None
            break
        out.append(c)
    start = (np.array(out) if out is not None else V) + off
    # final sub-pixel snap, on the FULL-resolution photo when available (tick marks are resolved there,
    # so the true body edge is visible between them)
    if gray_full is not None and f_ratio > 1.0:
        gq = refine_quad_edges(gray_full, start * f_ratio)
        if gq is not None:
            return gq / f_ratio
    grad_quad = refine_quad_edges(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), start)
    return grad_quad if grad_quad is not None else start


def rectify_from_quad(quad: np.ndarray, size, f_exif_px: Optional[float], L_mm: float):
    """Recover the plane of a rectangle from its 4 image corners and build a pixel -> mm homography.
    quad order: start_a, end_a, end_b, start_b (long side = start_a -> end_a).
    Returns dict(H, f, f_source, tilt_deg, width_mm) or None."""
    W, H = size
    c = np.array([W / 2.0, H / 2.0])
    P = np.asarray(quad, np.float64)
    if not cv2.isContourConvex(P.astype(np.float32).reshape(-1, 1, 2)):
        return None
    hom = lambda p: np.array([p[0], p[1], 1.0])
    l01, l32 = np.cross(hom(P[0]), hom(P[1])), np.cross(hom(P[3]), hom(P[2]))
    l03, l12 = np.cross(hom(P[0]), hom(P[3])), np.cross(hom(P[1]), hom(P[2]))
    v1, v2 = np.cross(l01, l32), np.cross(l03, l12)
    maxdim = max(W, H)
    f_vp = None
    if abs(v1[2]) > 1e-9 * np.linalg.norm(v1[:2]) and abs(v2[2]) > 1e-9 * np.linalg.norm(v2[:2]):
        a, b = v1[:2] / v1[2] - c, v2[:2] / v2[2] - c
        f2 = -float(a @ b)
        if f2 > 0:
            f_vp = math.sqrt(f2)
    if f_exif_px and 0.3 * maxdim <= f_exif_px <= 3.0 * maxdim:
        f, fsrc = f_exif_px, "exif"
    elif f_vp and 0.4 * maxdim <= f_vp <= 2.5 * maxdim:
        f, fsrc = f_vp, "vanishing_points"
    else:
        f, fsrc = 0.8 * maxdim, "default_guess"

    Ainv = np.array([[1 / f, 0, -c[0] / f], [0, 1 / f, -c[1] / f], [0, 0, 1.0]])
    r = [Ainv @ hom(p) for p in P]
    M = np.stack([r[0], -r[1], r[2], -r[3]], axis=1)          # parallelogram: X0 + X2 = X1 + X3
    lam = np.linalg.svd(M)[2][-1]
    if np.all(lam < 0):
        lam = -lam
    if not np.all(lam > 0):
        return None
    X = [lam[i] * r[i] for i in range(4)]
    e1, e2 = X[1] - X[0], X[3] - X[0]
    if np.linalg.norm(e1) < 1e-9 or np.linalg.norm(e2) < 1e-9:
        return None
    n = np.cross(e1, e2)
    n /= np.linalg.norm(n)
    u1 = e1 / np.linalg.norm(e1)
    u2 = np.cross(n, u1)
    if u2 @ e2 < 0:
        u2 = -u2
    s = L_mm / np.linalg.norm(e1)                              # mm per camera-space unit
    X0, nX0 = X[0], float(n @ X[0])
    rows = np.stack([s * (nX0 * u1 - (u1 @ X0) * n), s * (nX0 * u2 - (u2 @ X0) * n), n])
    Hm = rows @ Ainv
    return {"H": Hm, "f": f, "f_source": fsrc, "tilt_deg": math.degrees(math.acos(min(1.0, abs(float(n[2]))))),
            "width_mm": float(L_mm * np.linalg.norm(e2) / np.linalg.norm(e1))}


def calibrate(rr: RulerReading, work: Image.Image, full: Image.Image, f_ratio: float,
              f_exif_px: Optional[float], L_mm: float):
    """Build the pixel->mm mapping for one photo. Returns (scale_dict, None) or (None, 'code: reason')."""
    if not rr.ruler_visible:
        return None, f"no_ruler: {rr.notes or 'no readable ruler found'}"
    gray = cv2.cvtColor(np.array(full), cv2.COLOR_RGB2GRAY)
    size = work.size
    has_labels = all(v is not None for v in (rr.point_a, rr.point_b, rr.value_a, rr.value_b))
    span_mm = abs(rr.value_b - rr.value_a) * UNIT_MM[rr.unit] if has_labels else 0.0
    corners = [rr.corner_start_a, rr.corner_end_a, rr.corner_end_b, rr.corner_start_b]
    quad0 = np.array([[p.x, p.y] for p in corners], np.float64) if rr.fully_visible and all(corners) else None

    # ---------------- preferred: perspective-corrected -----------------------
    if quad0 is not None:
        bgr = cv2.cvtColor(np.array(work), cv2.COLOR_RGB2BGR)
        refined = refine_quad(bgr, quad0, gray, f_ratio)
        cands = ([("refined", refined)] if refined is not None else []) + [("claude", quad0)]
        results = []
        too_oblique = None
        for qname, q in cands:
            rect = rectify_from_quad(q, size, f_exif_px, L_mm)
            if rect is None or not 15.0 <= rect["width_mm"] <= 60.0:
                continue
            if rect["tilt_deg"] > MAX_TILT_DEG:
                too_oblique = rect["tilt_deg"]
                continue
            H = rect["H"]
            kappa, source = 1.0, "assumed_length"
            if has_labels:
                pm = persp(H, [[rr.point_a.x, rr.point_a.y], [rr.point_b.x, rr.point_b.y]])
                dist = float(np.linalg.norm(pm[1] - pm[0]))
                if dist > 0 and span_mm > 0 and 0.5 <= span_mm / dist <= 2.0:
                    kappa, source = span_mm / dist, "labels"
            H_full = H @ np.diag([1 / f_ratio, 1 / f_ratio, 1.0])
            tk = tick_scale_rectified(gray, H_full, L_mm, rect["width_mm"], kappa, rr.unit)
            strength, uni = None, None
            if tk:
                kappa, source, strength, uni = tk["kappa"], "ticks", round(tk["strength"], 2), tk["uniformity"]
            score = uni if uni is not None else 1.08
            results.append({"score": score, "res": {
                "kind": "rectified", "H": np.diag([kappa, kappa, 1.0]) @ H, "source": source, "kappa": kappa,
                "tilt_deg": rect["tilt_deg"], "focal_source": rect["f_source"], "ruler_width_mm": rect["width_mm"],
                "tick_strength": strength, "uniformity": None if uni is None else round(uni, 3), "quad": q,
                "quad_source": qname}})
            if score <= 1.12:
                break          # refined outline is tried first; take it if it is consistent
        if results:
            best = min(results, key=lambda r: r["score"])
            res = best["res"]
            if best["score"] <= 1.12:
                return res, None
            res["source_note"] = "rectification uncertain"
            if too_oblique is None:
                return res, None
        if too_oblique is not None and not results:
            return None, f"too_oblique: camera is ~{too_oblique:.0f} degrees off straight-on; retake from higher up"

    # ---------------- fallback: scalar scale, no perspective correction -------
    s0, source = None, None
    if has_labels:
        dist = float(np.hypot(rr.point_b.x - rr.point_a.x, rr.point_b.y - rr.point_a.y))
        if dist > 0.15 * max(size) and span_mm > 0:
            s0, source = span_mm / dist, "labels"
    if s0 is None and quad0 is not None:
        s0, source = L_mm / float(np.linalg.norm(quad0[1] - quad0[0])), "assumed_length"
    if s0 is None:
        return None, "no_scale: need either two readable numbered ticks or all four ruler corners"
    pa = np.array([rr.point_a.x, rr.point_a.y]) if has_labels else quad0[0]
    pb = np.array([rr.point_b.x, rr.point_b.y]) if has_labels else quad0[1]
    ref = refine_scale_scalar(gray, pa * f_ratio, pb * f_ratio, s0 / f_ratio, rr.unit)
    s, strength, tilt = s0, None, None
    if ref:
        tilt = None if ref["tilt"] is None else round(ref["tilt"], 3)
        if abs(ref["scale"] * f_ratio / s0 - 1) <= REFINE_AGREE:
            s, source, strength = ref["scale"] * f_ratio, "ticks", round(ref["strength"], 2)
    return {"kind": "scalar", "s": s, "source": source, "tilt_deg": None, "tick_strength": strength,
            "uniformity": tilt, "focal_source": None, "ruler_width_mm": None, "quad": quad0,
            "quad_source": None, "source_note": "angle NOT corrected (ruler corners not fully visible)"}, None


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
    if EDGE_ERODE_PX:   # GrabCut includes soft anti-aliased edge pixels (and shadows): pull the outline in slightly
        fg = cv2.erode(fg, np.ones((3, 3), np.uint8), iterations=EDGE_ERODE_PX)
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if not 0.10 <= cv2.contourArea(c) / (bw * bh) <= 1.15:      # outside this range the outline clearly failed
        return None
    return c + np.array([[[x0 - border, y0 - border]]], dtype=c.dtype)


def to_mm(scale: dict, pts: np.ndarray, k: float = 1.0) -> np.ndarray:
    """Map pixel points to millimetres. k = scale of the points' frame relative to the working image (1 = working, f = full-res)."""
    if scale["kind"] == "rectified":
        return persp(scale["H"] @ np.diag([1.0 / k, 1.0 / k, 1.0]), pts)
    return np.asarray(pts, np.float64) * (scale["s"] / k)


def measure_photo(work: Image.Image, pd: PhotoDetections, name_map: dict, scale: dict, photo: str,
                  debug_path: Optional[Path] = None, full: Optional[Image.Image] = None, f_ratio: float = 1.0):
    """Returns (measurements, 'code: detail'). Each measurement is one dimension of one item, in mm.
    Pieces of moderate size are outlined on the full-resolution photo for better precision."""
    bgr = cv2.cvtColor(np.array(work), cv2.COLOR_RGB2BGR)
    H, W = bgr.shape[:2]
    bgr_full = [None]
    dbg = bgr.copy() if debug_path else None
    boxes, out = [], []
    uncorrected = scale["kind"] != "rectified"

    def outline(box):
        """Returns (points, k): outline points and the frame scale they are expressed in."""
        if full is not None and f_ratio > 1.0 and max(box.x_max - box.x_min, box.y_max - box.y_min) * f_ratio <= 900:
            if bgr_full[0] is None:
                bgr_full[0] = cv2.cvtColor(np.array(full), cv2.COLOR_RGB2BGR)
            Hf, Wf = bgr_full[0].shape[:2]
            fb = _clip_bbox(BBox(x_min=int(box.x_min * f_ratio), y_min=int(box.y_min * f_ratio),
                                 x_max=int(box.x_max * f_ratio), y_max=int(box.y_max * f_ratio)), Wf, Hf)
            if fb:
                cf = segment(bgr_full[0], fb)
                if cf is not None:
                    return cf.reshape(-1, 2).astype(np.float64), f_ratio
        c = segment(bgr, box)
        return (None, 1.0) if c is None else (c.reshape(-1, 2).astype(np.float64), 1.0)

    def add(comp, kind, mm, shape):
        if 1.0 <= mm <= 1000.0:
            out.append({"photo": photo, "comp": comp, "kind": kind, "mm": float(mm), "shape": shape,
                        "src": scale["source"], "tilt": scale.get("tilt_deg") or 0.0, "uncorrected": uncorrected})

    for d in pd.detections:
        comp = name_map.get(d.name.strip().lower())
        if not comp or not d.single_instance:
            continue
        box = _clip_bbox(d.bbox, W, H)
        if not box or any(_iou(box, b) > 0.3 for b in boxes):
            continue
        pts, k = outline(box)
        if pts is None:
            continue
        boxes.append(box)
        mm = to_mm(scale, pts, k)
        if pd.view == "flat":
            (_, _), (rw, rh), _ = cv2.minAreaRect(mm.astype(np.float32))
            add(comp, "long", max(rw, rh), d.shape.value)
            add(comp, "short", min(rw, rh), d.shape.value)
        else:                                        # upright: heights along the image's vertical direction
            p0 = pts.mean(axis=0)
            m0, m1 = to_mm(scale, np.array([p0, p0 + [0.0, -40.0 * k]]), k)
            up = m1 - m0
            up /= max(np.linalg.norm(up), 1e-12)
            perp = np.array([-up[1], up[0]])
            add(comp, "height", np.ptp(mm @ up), d.shape.value)
            if d.horizontal_dim in ("long", "short"):
                add(comp, d.horizontal_dim, np.ptp(mm @ perp), d.shape.value)
        if dbg is not None:
            cv2.polylines(dbg, [np.round(pts / k).astype(np.int32).reshape(-1, 1, 2)], True, (0, 0, 255), 2)
            cv2.putText(dbg, comp[:24], (box.x_min, max(15, box.y_min - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    if dbg is not None:
        if scale.get("quad") is not None:
            cv2.polylines(dbg, [np.round(np.asarray(scale["quad"])).astype(np.int32)], True, (255, 0, 0), 3)
        tilt = scale.get("tilt_deg")
        label = f"{scale['kind']} | scale: {scale['source']}" + (f" | tilt {tilt:.0f} deg" if tilt is not None else "")
        cv2.putText(dbg, label, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 3)
        cv2.putText(dbg, label, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 1)
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
    notes = []
    pen = int(round(float(np.mean([SOURCE_PENALTY[m["src"]] for m in ms]))))
    if pen:
        conf -= pen
        notes.append("Scale came mostly from ruler labels or the assumed ruler length rather than tick marks; less precise.")
    if float(np.median([m["tilt"] for m in ms])) > 45:
        conf -= 1
        notes.append("Photos were taken at a steep angle (perspective-corrected, but less accurate).")
    if sum(m["uncorrected"] for m in ms) / len(ms) > 0.5:
        conf -= 1
        notes.append("Angle could not be corrected (whole ruler not visible in those photos).")
    if height_v is None and shape != Shape.SPHERE:
        notes.append("Height/thickness not measured (needs an 'upright' photo with a ruler).")
    return {"shape": shape.value, "dimensions_mm": build_dimensions(shape, long_v, short_v, height_v),
            "confidence": ["low", "medium", "high"][max(0, min(2, conf))], "notes": " ".join(notes) or None,
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
    p.add_argument("--ruler-length", default="12in", help="approximate physical length of your ruler's body (default 12in)")
    p.add_argument("--focal-35mm", type=float, help="35mm-equivalent focal length, if your photos lack EXIF data")
    p.add_argument("--debug-dir", help="save annotated copies of each photo (ruler outline, piece outlines, scale)")
    args = p.parse_args()

    L_mm = parse_length(args.ruler_length)
    if not L_mm:
        raise SystemExit(f"Could not read --ruler-length '{args.ruler_length}' (try 12in, 30cm, 305mm).")
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

        def reject(reason):
            entry.update(status="rejected", reason=reason)
            photo_log.append(entry)
            print(f"    skip {reason}", file=sys.stderr)

        try:
            full, work, f_ratio, f35 = load_photo(path)
        except Exception as e:
            reject(f"unreadable_image: {e}")
            continue
        f_exif = focal_px_from_35mm(args.focal_35mm or f35, work.size)
        rr = _vision(ruler_llm, RULER_SYSTEM, work, "Find the ruler: its four corners and two numbered ticks.")
        if rr is None:
            reject("vision_call_failed")
            continue
        try:
            scale, why = calibrate(rr, work, full, f_ratio, f_exif, L_mm)
        except Exception as e:
            reject(f"calibration_error: {e}")
            continue
        if scale is None:
            reject(why)
            continue
        listing = "\n".join(f"- {n}" for n in names)
        pd = _vision(detect_llm, DETECT_SYSTEM, work, f"Game: {args.game}\nComponents:\n{listing}")
        if pd is None:
            reject("vision_call_failed")
            continue
        dbg = debug_dir / f"{slugify(path.stem)}_debug.jpg" if debug_dir else None
        ms, why = measure_photo(work, pd, name_map, scale, path.name, dbg, full, f_ratio)
        tilt = scale.get("tilt_deg")
        print(f"    {'OK  ' if ms else 'skip'} {why}  [{scale['kind']}, scale from {scale['source']}"
              + (f", tilt {tilt:.0f} deg" if tilt is not None else "") + "]", file=sys.stderr)
        entry.update(status="used" if ms else "rejected", reason=why, mode=scale["kind"], scale_source=scale["source"],
                     tilt_deg=None if tilt is None else round(tilt, 1), focal_source=scale.get("focal_source"),
                     ruler_width_mm_estimate=None if scale.get("ruler_width_mm") is None else round(scale["ruler_width_mm"], 1),
                     tick_strength=scale.get("tick_strength"), tick_uniformity=scale.get("uniformity"),
                     note=scale.get("source_note"), items_measured=len(ms))
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
        "method": "User-supplied photos with a ruler; perspective corrected using the ruler rectangle; "
                  "scale from ruler tick marks / labels / assumed ruler length; outlines via GrabCut.",
        "assumed_ruler_length_mm": round(L_mm, 1),
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
