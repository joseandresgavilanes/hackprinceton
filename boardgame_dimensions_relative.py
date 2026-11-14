#!/usr/bin/env python3
"""
boardgame_dimensions_relative.py

Estimate the physical size of every component of a board game from online
photos, needing only ONE real-world measurement from you.

Idea
----
Photos that show several components together share a single (unknown) scale,
so the components' sizes relative to each other can be measured in pixels.
Each photo has its own zoom, so every photo gets its own unknown scale factor.
All photos are then solved together as one least-squares problem in log space:

        log(pixels of component c in photo p) = log(size of c) - log(scale of p)

Components that never share a photo are still linked through chains of photos.
One real measurement (the longest side of one component, chosen automatically
for being well-connected and easy to measure) pins down the absolute scale.

Pipeline
--------
1. LIST      Claude lists the game's components (names/quantities only).
2. SEARCH    Image search for photos showing many components together
             (or use your own photos with --image-dir).
3. DETECT    Claude (vision) finds individual component instances in each photo
             and gives rough boxes. Collages/renders/angled photos are rejected.
4. MEASURE   OpenCV (GrabCut) outlines each item and measures its pixel extents.
5. SOLVE     Robust least squares over all photos (outliers down-weighted and
             pruned). Connectivity to the measured component is checked.
6. ASK       You are asked for ONE measurement (e.g. the long side of a card).
             Extra measurements are requested only if photos split into
             disconnected groups (max --max-extra-anchors, default 1).

Setup
-----
    pip install langchain-anthropic langchain-core pydantic opencv-python-headless \
                numpy pillow requests
    export ANTHROPIC_API_KEY="..."
    export SERPAPI_API_KEY="..."      # or BRAVE_API_KEY with --search-provider brave

Usage
-----
    python boardgame_dimensions_relative.py "Carcassonne"
    python boardgame_dimensions_relative.py "Carcassonne" --anchor "Land tile=46mm"   # skip the prompt
    python boardgame_dimensions_relative.py "Carcassonne" --image-dir my_photos        # your own photos
"""

import argparse
import base64
import hashlib
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
import requests
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image
from pydantic import BaseModel, Field

MODEL = "claude-sonnet-5-5"
MAX_SIDE = 1568           # photos are downscaled to this (px) before analysis
MIN_SIDE = 300            # smaller photos are skipped
OUTLIER_LOG = 0.20        # observations >~22% off the consensus are pruned
HUBER_LOG = 0.08          # robust-loss knee (~8%)
TUKEY_LOG = 0.20          # observations >~22% off the consensus get ~zero weight
UA = {"User-Agent": "Mozilla/5.0 (boardgame-dimensions-relative)"}
EASY_TO_MEASURE = ("box", "card", "board", "rulebook", "rule book", "booklet", "manual", "die", "dice", "tile")


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
    quantity: Optional[int] = None
    search_terms: str = Field(description="Short image-search phrase, e.g. 'wooden settlement piece'")
    shape_hint: Shape = Field(description="Best basic-shape approximation (fallback only)")


class ComponentList(BaseModel):
    game: str
    edition_assumed: Optional[str] = None
    components: list[ComponentSpec]


class BBox(BaseModel):
    x_min: int
    y_min: int
    x_max: int
    y_max: int


class Detection(BaseModel):
    name: str = Field(description="Exactly one of the provided component names")
    bbox: BBox = Field(description="Tight pixel box around ONE single item")
    single_instance: bool = Field(description="True only if the box encloses one item, not a stack/pile/group")
    horizontal_dim: Literal["long", "short", "unknown"] = Field(
        "unknown", description="Side views only: is the item's horizontal extent its long or short dimension?")
    shape: Shape


class PhotoAnalysis(BaseModel):
    view: Literal["top_down", "side", "angled"]
    consistent_scale: bool
    reason: str
    detections: list[Detection] = Field(default_factory=list)


VISION_SYSTEM = """You are a computer-vision assistant. You get one photo and a list of a board game's components.
Goal: find individual, fully visible instances of the listed components so their RELATIVE sizes can be measured.

For each listed component you can clearly see, return one or two detections (separate single items):
 - name: exactly as written in the list.
 - bbox: tight pixel box around that single item (origin top-left, x right, y down, in the image shown).
 - single_instance: true only if the box encloses ONE item (not a stack, pile, bundle, or group).
 - horizontal_dim: only for side views: whether the item's horizontal extent is its 'long' or 'short' dimension; else 'unknown'.
 - shape: closest basic shape.
Photo-level fields:
 - view: 'top_down' (camera roughly above objects lying on a flat surface; mild tilt is fine),
         'side' (camera roughly level with the objects), 'angled' (clearly oblique / strong perspective).
 - consistent_scale: true if the objects are real things photographed together on roughly the same surface at a
   similar distance from the camera. FALSE for renders, collages/montages of separately photographed items,
   catalogue-style layouts, or photos where objects are at very different distances from the lens.
 - reason: one short sentence on what you see.
Do not guess at components you cannot see. Prefer a few clean detections over many doubtful ones."""

LIST_SYSTEM = (
    "You are a meticulous board game archivist. List every distinct physical component of the requested board "
    "game (base game, latest standard edition): boards, card types, tiles, tokens, dice, player pieces, rulebook, "
    "and the box. Group identical items (e.g. 'Settlement piece', quantity 20). DO NOT give any dimensions."
)


# --------------------------------------------------------------------------
# Small helpers
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
    raise SystemExit("Could not find a writable directory. Use --cache-dir and -o with a writable path.")


def parse_length(text: str) -> Optional[float]:
    """'88.9', '88.9mm', '3.5in', '6 cm' -> millimetres."""
    m = re.fullmatch(r'\s*([0-9]*\.?[0-9]+)\s*(mm|cm|m|in|inch|inches|")?\s*', text.lower())
    if not m:
        return None
    unit = {"mm": 1, "cm": 10, "m": 1000, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4}[m.group(2) or "mm"]
    v = float(m.group(1)) * unit
    return v if v > 0 else None


def ask_length(prompt: str) -> Optional[float]:
    if not sys.stdin.isatty():
        return None
    while True:
        s = input(prompt).strip()
        if not s:
            return None
        v = parse_length(s)
        if v:
            return v
        print("  Couldn't read that. Try e.g. 88.9, 3.5in or 6cm (or press Enter to skip).")


def _text_from(message) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


# --------------------------------------------------------------------------
# Stage 1: components
# --------------------------------------------------------------------------
def list_components(game: str) -> ComponentList:
    llm = ChatAnthropic(model=MODEL, max_tokens=4000)
    research = llm.bind_tools([{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])
    notes = _text_from(research.invoke([SystemMessage(content=LIST_SYSTEM), HumanMessage(content=f"Board game: {game}")]))
    return llm.with_structured_output(ComponentList).invoke([
        SystemMessage(content="Convert these notes into the schema. Do not invent components."),
        HumanMessage(content=f"Game: {game}\n\nNotes:\n{notes}"),
    ])


# --------------------------------------------------------------------------
# Stage 2: photos
# --------------------------------------------------------------------------
def search_images(query: str, provider: str, n: int = 10) -> list[str]:
    try:
        if provider == "serpapi":
            r = requests.get("https://serpapi.com/search.json", timeout=30,
                             params={"engine": "google_images", "q": query, "api_key": os.environ["SERPAPI_API_KEY"]})
            r.raise_for_status()
            return [x["original"] for x in r.json().get("images_results", []) if x.get("original")][:n]
        r = requests.get("https://api.search.brave.com/res/v1/images/search", timeout=30,
                         params={"q": query, "count": n},
                         headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"})
        r.raise_for_status()
        return [x["properties"]["url"] for x in r.json().get("results", []) if x.get("properties", {}).get("url")][:n]
    except KeyError as e:
        raise SystemExit(f"Missing environment variable for image search: {e}")
    except Exception as e:
        print(f"    search failed ({e})", file=sys.stderr)
        return []


def prepare_image(img: Image.Image) -> Optional[Image.Image]:
    if min(img.size) < MIN_SIDE:
        return None
    scale = MAX_SIDE / max(img.size)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def fetch_image(url: str, cache_dir: Path) -> Optional[Image.Image]:
    path = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".img")
    try:
        if path.exists():
            data = path.read_bytes()
        else:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            if "image" not in r.headers.get("content-type", "image"):
                return None
            data = r.content
            path.write_bytes(data)
        return prepare_image(Image.open(io.BytesIO(data)).convert("RGB"))
    except Exception:
        return None


def iter_local_images(folder: Path):
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            try:
                img = prepare_image(Image.open(p).convert("RGB"))
            except Exception:
                continue
            if img is not None:
                yield str(p), img


# --------------------------------------------------------------------------
# Stage 3: vision detection
# --------------------------------------------------------------------------
def analyze_photo(vision_llm, img: Image.Image, game: str, names: list[str]) -> Optional[PhotoAnalysis]:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()
    listing = "\n".join(f"- {n}" for n in names)
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": f"Game: {game}\nComponents:\n{listing}\nImage size: {img.width}x{img.height} pixels."},
    ])
    try:
        return vision_llm.invoke([SystemMessage(content=VISION_SYSTEM), msg])
    except Exception as e:
        print(f"    vision call failed ({e})", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Stage 4: pixel measurement (OpenCV)
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
    if not 0.25 <= cv2.contourArea(c) / (bw * bh) <= 1.15:      # segmentation clearly failed
        return None
    return c + np.array([[[x0 - border, y0 - border]]], dtype=c.dtype)


def metrics(c: np.ndarray) -> dict:
    (_, _), (w, h), _ = cv2.minAreaRect(c)
    _, _, bw, bh = cv2.boundingRect(c)
    return {"rect_long": max(w, h), "rect_short": min(w, h), "aabb_w": bw, "aabb_h": bh}


def extract_observations(img: Image.Image, pa: PhotoAnalysis, name_map: dict, photo_id: str,
                         debug_path: Optional[Path] = None):
    """Turn one analysed photo into pixel observations. Returns (observations, 'code: detail')."""
    if pa.view == "angled":
        return [], f"angled_view: {pa.reason}"
    if not pa.consistent_scale:
        return [], f"inconsistent_scale: {pa.reason}"
    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    H, W = bgr.shape[:2]
    dbg = bgr.copy() if debug_path else None
    boxes, obs = [], []

    def add(comp, kind, px, shape):
        if px >= 6:
            obs.append({"photo": photo_id, "comp": comp, "kind": kind, "logpx": math.log(px),
                        "w": float(np.clip(px / 60.0, 0.1, 1.0)), "shape": shape})

    for d in pa.detections:
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
        m = metrics(c)
        if pa.view == "top_down":
            add(comp, "long", m["rect_long"], d.shape.value)
            add(comp, "short", m["rect_short"], d.shape.value)
        else:  # side view: vertical extent is the height
            add(comp, "height", m["aabb_h"], d.shape.value)
            if d.horizontal_dim in ("long", "short"):
                add(comp, d.horizontal_dim, m["aabb_w"], d.shape.value)
        if dbg is not None:
            cv2.drawContours(dbg, [c], -1, (0, 0, 255), 2)
            cv2.putText(dbg, comp[:24], (box.x_min, max(15, box.y_min - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    n_comp = len({o["comp"] for o in obs})
    if n_comp < 2:
        return [], f"too_few_components: only {n_comp} measurable component(s); {pa.reason}"
    if dbg is not None:
        cv2.imwrite(str(debug_path), dbg)
    return obs, f"ok: {n_comp} components measured ({pa.view})"


# --------------------------------------------------------------------------
# Stage 5: robust joint solve
# --------------------------------------------------------------------------
class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def dvar(o):
    return ("D", o["comp"], o["kind"])


def kvar(o):
    return ("k", o["photo"])


def valid_obs(obs: list[dict]) -> list[dict]:
    """Keep only photos that show at least two distinct components (otherwise they carry no ratio information)."""
    comps = defaultdict(set)
    for o in obs:
        comps[o["photo"]].add(o["comp"])
    return [o for o in obs if len(comps[o["photo"]]) >= 2]


def groups_of(obs: list[dict]) -> list[dict]:
    """Connected groups of unknowns (component sizes linked through shared photos)."""
    dsu = DSU()
    for o in obs:
        dsu.union(dvar(o), kvar(o))
    groups = defaultdict(lambda: {"vars": set(), "comps": set(), "photos": defaultdict(set)})
    for o in obs:
        g = groups[dsu.find(dvar(o))]
        g["vars"].add(dvar(o))
        g["comps"].add(o["comp"])
        g["photos"][o["comp"]].add(o["photo"])
    return sorted(groups.values(), key=lambda g: -len(g["comps"]))


def pick_anchor_var(group: dict):
    """Choose the unknown that is best to ask the user to measure: well-connected, easy to measure, long side."""
    kind_pref = {"long": 2, "short": 1, "height": 0}

    def score(v):
        _, comp, kind = v
        easy = 3 if any(k in comp.lower() for k in EASY_TO_MEASURE) else 0
        return len(group["photos"][comp]) + easy + kind_pref[kind]
    return max(group["vars"], key=score)


KIND_TEXT = {"long": "longest side (or diameter)", "short": "shortest side", "height": "height / thickness"}


def _irls(rows: list[dict], anchors: dict):
    vars_ = sorted({dvar(o) for o in rows} | {kvar(o) for o in rows}, key=str)
    idx = {v: i for i, v in enumerate(vars_)}
    m, n = len(rows), len(vars_)
    A = np.zeros((m + len(anchors), n))
    y = np.zeros(m + len(anchors))
    base_w = np.ones(m + len(anchors))
    for r, o in enumerate(rows):
        A[r, idx[dvar(o)]] = 1.0
        A[r, idx[kvar(o)]] = -1.0
        y[r] = o["logpx"]
        base_w[r] = o["w"]
    for j, (v, mm) in enumerate(anchors.items()):
        A[m + j, idx[v]] = 1.0
        y[m + j] = math.log(mm)
        base_w[m + j] = 100.0
    rw = np.ones_like(base_w)
    for it in range(30):
        sw = np.sqrt(base_w * rw)[:, None]
        x = np.linalg.lstsq(A * sw, y * sw[:, 0], rcond=None)[0]
        res = A @ x - y
        r = np.abs(res[:m])
        if it < 10:   # Huber first: stable start
            rw[:m] = np.where(r <= HUBER_LOG, 1.0, HUBER_LOG / np.maximum(r, 1e-9))
        else:         # then Tukey biweight: clearly wrong observations get ~zero weight
            rw[:m] = np.maximum((1 - np.minimum(r / TUKEY_LOG, 1.0) ** 2) ** 2, 1e-4)
    values = {v: math.exp(x[idx[v]]) for v in vars_ if v[0] == "D"}
    return values, res[:m]


def solve_once(obs: list[dict], anchors: dict):
    dsu = DSU()
    for o in obs:
        dsu.union(dvar(o), kvar(o))
    anchored_roots = {dsu.find(v) for v in anchors if v in dsu.p}
    by_root = defaultdict(list)
    for i, o in enumerate(obs):
        by_root[dsu.find(dvar(o))].append(i)
    values, resid = {}, {}
    for root, idxs in by_root.items():
        if root not in anchored_roots:
            continue
        rows = [obs[i] for i in idxs]
        local = {v: mm for v, mm in anchors.items() if v in dsu.p and dsu.find(v) == root}
        vals, res = _irls(rows, local)
        values.update(vals)
        resid.update({i: float(r) for i, r in zip(idxs, res)})
    return values, resid


def solve(obs: list[dict], anchors: dict):
    values, resid = solve_once(obs, anchors)
    bad = {i for i, r in resid.items() if abs(r) > OUTLIER_LOG}
    if bad:
        obs = [o for i, o in enumerate(obs) if i not in bad]
        values, resid = solve_once(obs, anchors)
    return values, resid, obs


def build_dimensions(shape: Shape, long, short, height, nd=1) -> dict:
    avg = lambda *xs: float(np.mean([x for x in xs if x is not None]))
    if shape in (Shape.CYLINDER, Shape.CONE, Shape.HEXAGONAL_PRISM):
        d = {"diameter": avg(long, short) if (long or short) else None, "height": height}
    elif shape == Shape.SPHERE:
        d = {"diameter": avg(long, short, height) if any(v is not None for v in (long, short, height)) else None}
    elif shape == Shape.CUBE:
        e = avg(long, short, height) if any(v is not None for v in (long, short, height)) else None
        d = {"length": e, "width": e, "height": e}
    else:
        d = {"length": long, "width": short, "height": height}
    return {k: round(v, nd) for k, v in d.items() if v is not None}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def gather(args, game, specs, vision_llm, cache_dir, debug_dir):
    names = [s.name for s in specs]
    name_map = {n.lower(): n for n in names}
    names_key = hashlib.sha1("|".join(sorted(names)).encode()).hexdigest()[:8]
    all_obs, tally, seen, analyzed = [], Counter(), set(), [0]

    def covered() -> int:
        ph = defaultdict(set)
        for o in valid_obs(all_obs):
            ph[o["comp"]].add(o["photo"])
        return sum(1 for n in names if len(ph[n]) >= 2)

    def process(pid: str, img: Image.Image):
        analyzed[0] += 1
        ck = cache_dir / ("analysis_" + hashlib.sha1(f"{pid}|{game}|{names_key}".encode()).hexdigest() + ".json")
        pa = None
        if ck.exists():
            try:
                pa = PhotoAnalysis.model_validate_json(ck.read_text())
            except Exception:
                pa = None
        if pa is None:
            pa = analyze_photo(vision_llm, img, game, names)
            if pa is None:
                tally["vision_call_failed"] += 1
                return
            ck.write_text(pa.model_dump_json())
        dbg = debug_dir / f"photo_{analyzed[0]}.jpg" if debug_dir else None
        o, why = extract_observations(img, pa, name_map, pid, dbg)
        tally[why.split(":")[0]] += 1
        print(f"  [{analyzed[0]}] {'OK  ' if o else 'skip'} {why}", file=sys.stderr)
        all_obs.extend(o)

    def done() -> bool:
        return analyzed[0] >= args.max_photos or covered() == len(names)

    if args.image_dir:
        for pid, img in iter_local_images(Path(args.image_dir).expanduser()):
            if done():
                break
            process(pid, img)
    else:
        general = [f"{game} board game components", f"{game} what's in the box components",
                   f"{game} components laid out top view", f"{game} unboxing all pieces",
                   f"{game} pieces size comparison"]

        def run(queries):
            for q in queries:
                for url in search_images(q, args.search_provider):
                    if done():
                        return
                    if url in seen:
                        continue
                    seen.add(url)
                    img = fetch_image(url, cache_dir)
                    if img is not None:
                        process(url, img)

        print("Searching for photos showing many components together...", file=sys.stderr)
        run(general)
        if not done():  # second pass: target components that still lack coverage
            ph = defaultdict(set)
            for o in valid_obs(all_obs):
                ph[o["comp"]].add(o["photo"])
            lacking = [s for s in specs if len(ph[s.name]) < 2]
            print(f"Targeted search for {len(lacking)} under-covered component(s)...", file=sys.stderr)
            for s in lacking:
                run([f"{game} {s.search_terms} with other components"])
                if done():
                    break
    if analyzed[0] == 0:
        print("  No photos could be downloaded/found (check your search API key or --image-dir).", file=sys.stderr)
    elif not all_obs:
        print(f"  All {analyzed[0]} photos rejected: {dict(tally)}", file=sys.stderr)
    return valid_obs(all_obs), analyzed[0]


def match_component(text: str, names: list[str]) -> Optional[str]:
    t = text.strip().lower()
    for n in names:
        if n.lower() == t:
            return n
    hits = [n for n in names if t in n.lower() or n.lower() in t]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    p = argparse.ArgumentParser(description="Estimate component sizes from relative measurements + one real measurement.")
    p.add_argument("game")
    p.add_argument("-o", "--output")
    p.add_argument("--anchor", action="append", default=[], metavar='"Component=63.5mm"',
                   help="real measurement of a component's LONGEST side/diameter (units: mm, cm, in). Skips the prompt.")
    p.add_argument("--components", help="comma-separated component names (skips the automatic component listing)")
    p.add_argument("--image-dir", help="folder of YOUR photos showing several components together")
    p.add_argument("--search-provider", choices=["serpapi", "brave"], default="serpapi")
    p.add_argument("--max-photos", type=int, default=25, help="max photos analysed (each is one vision call)")
    p.add_argument("--max-extra-anchors", type=int, default=1,
                   help="extra measurements to request ONLY if photos split into disconnected groups")
    p.add_argument("--debug-dir", help="save annotated photos showing the detected outlines")
    p.add_argument("--cache-dir", help="default: ~/.cache/boardgame_cv")
    args = p.parse_args()

    cache_dir = writable_dir(Path(args.cache_dir) if args.cache_dir else Path.home() / ".cache" / "boardgame_cv",
                             Path(tempfile.gettempdir()) / "boardgame_cv")
    debug_dir = writable_dir(Path(args.debug_dir), Path.home() / "boardgame_cv_debug") if args.debug_dir else None

    if args.components:
        names = [c.strip() for c in args.components.split(",") if c.strip()]
        comp_list = ComponentList(game=args.game, components=[
            ComponentSpec(name=n, search_terms=n, shape_hint=Shape.RECTANGULAR_PRISM) for n in names])
    else:
        print(f"Listing components of {args.game}...", file=sys.stderr)
        comp_list = list_components(args.game)
    specs = comp_list.components
    names = [s.name for s in specs]
    print(f"  {len(specs)} components: {', '.join(names)}", file=sys.stderr)

    vision_llm = ChatAnthropic(model=MODEL, max_tokens=3000).with_structured_output(PhotoAnalysis)
    obs, n_photos = gather(args, args.game, specs, vision_llm, cache_dir, debug_dir)

    # ---- anchors -------------------------------------------------------------
    anchors, units = {}, "mm"
    for a in args.anchor:
        name_txt, _, val = a.rpartition("=")
        comp, mm = match_component(name_txt, names), parse_length(val)
        if not comp or not mm:
            raise SystemExit(f'Could not parse --anchor "{a}". Use e.g. --anchor "Land tile=46mm" (component names: {names})')
        anchors[("D", comp, "long")] = mm

    groups = groups_of(obs)
    asked_extra = 0
    for gi, g in enumerate(groups):
        if any(v in g["vars"] for v in anchors):
            continue
        if gi > 0 and (units == "relative" or len(g["comps"]) < 2 or asked_extra >= args.max_extra_anchors):
            continue
        var = pick_anchor_var(g)
        label = f"the {KIND_TEXT[var[2]]} of '{var[1]}'"
        if gi == 0:
            print(f"\nTo convert relative sizes to real units I need ONE measurement: {label}.", file=sys.stderr)
        else:
            print(f"\nSome components ({', '.join(sorted(g['comps']))}) never appeared in photos linked to the rest. "
                  f"One more measurement would resolve them: {label}.", file=sys.stderr)
        if not sys.stdin.isatty():
            print(f'  (Not an interactive terminal: pass it with --anchor "{var[1]}=<length>")', file=sys.stderr)
        mm = ask_length(f"  Length of {label} (e.g. 88.9, 3.5in, 6cm; Enter to skip): ")
        if gi > 0:
            asked_extra += 1
        if mm:
            anchors[var] = mm
        elif gi == 0:
            anchors[var] = 1.0
            units = "relative"
            print(f'  No measurement given -> output is RELATIVE (1.0 = {label}). Re-run with '
                  f'--anchor "{var[1]}=<length>" to get millimetres (photo analyses are cached, so this is cheap).',
                  file=sys.stderr)

    # ---- solve -----------------------------------------------------------------
    values, resid, used = solve(obs, anchors) if obs and anchors else ({}, {}, obs)
    stats_photos, stats_res = defaultdict(set), defaultdict(list)
    for i, o in enumerate(used):
        if i in resid:
            stats_photos[o["comp"]].add(o["photo"])
            stats_res[o["comp"]].append(resid[i])
    shapes = defaultdict(list)
    for o in obs:
        shapes[o["comp"]].append(o["shape"])
    seen_comps = {o["comp"] for o in obs}

    nd = 1 if units == "mm" else 3
    dim_key = "dimensions_mm" if units == "mm" else "dimensions_relative"
    out = []
    for i, s in enumerate(specs, 1):
        long_v, short_v, height_v = (values.get(("D", s.name, k)) for k in ("long", "short", "height"))
        entry = {"index": i, "name": s.name, "quantity": s.quantity}
        if all(v is None for v in (long_v, short_v, height_v)):
            entry.update({"shape": s.shape_hint.value, dim_key: None, "confidence": "none",
                          "notes": ("Seen in photos, but not linked to the measured component; provide another --anchor."
                                    if s.name in seen_comps else "Never clearly visible in the photos analysed.")})
        else:
            shape = Shape(Counter(shapes[s.name]).most_common(1)[0][0]) if shapes[s.name] else s.shape_hint
            n_ph = len(stats_photos[s.name])
            rms = float(np.sqrt(np.mean(np.square(stats_res[s.name])))) if stats_res[s.name] else None
            conf = ("high" if n_ph >= 3 and rms is not None and rms < 0.04 else
                    "medium" if n_ph >= 2 and rms is not None and rms < 0.08 else "low")
            note = None
            if height_v is None and shape != Shape.SPHERE:
                note = "Height/thickness not measurable from the photos (needs a side-on photo)."
            entry.update({"shape": shape.value, dim_key: build_dimensions(shape, long_v, short_v, height_v, nd),
                          "confidence": conf, "notes": note,
                          "measurement": {"photos_used": n_ph, "relative_spread": None if rms is None else round(rms, 3)}})
        out.append(entry)

    result = {
        "game": comp_list.game,
        "edition_assumed": comp_list.edition_assumed,
        "units": units if units == "mm" else "relative (reference dimension = 1.0)",
        "method": "Relative sizes from photos showing several components together (joint robust least-squares), "
                  "scaled by the user's real measurement(s).",
        "user_measurements": [{"component": v[1], "dimension": KIND_TEXT[v[2]],
                               "value_mm" if units == "mm" else "value": round(mm, 2)} for v, mm in anchors.items()],
        "photos_analysed": n_photos,
        "components": out,
    }
    path = Path(args.output).expanduser() if args.output else \
        writable_dir(Path.cwd(), Path.home()) / f"{slugify(args.game)}_dimensions.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    measured = sum(1 for c in out if c.get(dim_key))
    print(f"\nWrote {path.resolve()}: {measured}/{len(out)} components sized.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())