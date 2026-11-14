#!/usr/bin/env python3
"""GameFit AI: convert a reviewed compartment layout specification into printable STL trays.

    pip install cadquery rectpack  # rectpack is optional; built-in MaxRects fallback
    python generate_organizer_stl.py organizer_plan_example.json -o demo_stl

Coordinates in layout.json: x/y origin is the bottom-left of the *inside* of the
board-game box; the z=0 plane is the floor. All dimensions are millimetres.
The layout is one layer of independently printable, open-top trays.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path


def positive(x, label):
    try:
        x = float(x)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a positive number")
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"{label} must be a positive, finite number")
    return x


def xyz_dims(obj, label, keys):
    if not isinstance(obj, dict):
        raise ValueError(f"{label} should be an object with {', '.join(keys)}")
    return tuple(positive(obj.get(k), f"{label}.{k}") for k in keys)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "tray"


@dataclass(frozen=True)
class Item:
    name: str
    contents_w: float
    contents_l: float
    contents_h: float
    outer_w: float
    outer_l: float
    outer_h: float
    filename: str


def parse_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("compartments"), list):
        raise ValueError("Expected a JSON object with a 'compartments' list")
    box_w, box_l, box_h = xyz_dims(plan.get("box_inner_mm"), "box_inner_mm", ("width", "length", "height"))
    bed_w, bed_l = xyz_dims(plan.get("printer_bed_mm"), "printer_bed_mm", ("width", "length"))
    settings = plan.get("settings", {})
    wall = positive(settings.get("wall_mm", 2.4), "settings.wall_mm")
    floor = positive(settings.get("floor_mm", 2.0), "settings.floor_mm")
    clearance = positive(settings.get("clearance_mm", 1.0), "settings.clearance_mm")
    gap = positive(settings.get("tray_gap_mm", 1.0), "settings.tray_gap_mm")
    margin = positive(settings.get("box_margin_mm", 1.0), "settings.box_margin_mm")
    if wall < 1.2 or floor < 1.2:
        raise ValueError("Wall and floor must be at least 1.2 mm for this prototype")
    if not plan["compartments"]:
        raise ValueError("Provide at least one compartment")

    items = []
    used_names = set()
    for i, c in enumerate(plan["compartments"], 1):
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            raise ValueError(f"compartments[{i-1}].name is required")
        name = str(c["name"]).strip()
        if name in used_names:
            raise ValueError(f"Compartment name {name!r} is duplicated")
        used_names.add(name)
        w, l, h = xyz_dims(c.get("required_mm"), f"{name}.required_mm", ("width", "length", "height"))
        # required_mm is the bounding dimensions of the *entire stored group*
        # (stack/pile), not one individual piece. Padding is added on both sides.
        iw, il, ih = w + 2*clearance, l + 2*clearance, h + clearance
        ow, ol, oh = iw + 2*wall, il + 2*wall, ih + floor
        if oh > box_h + 1e-6:
            raise ValueError(f"{name}: tray height {oh:.1f} mm exceeds box height {box_h:.1f} mm")
        if min(ow, ol) > min(bed_w, bed_l) or max(ow, ol) > max(bed_w, bed_l):
            raise ValueError(f"{name}: tray {ow:.1f} × {ol:.1f} mm does not fit printer bed {bed_w} × {bed_l} mm")
        items.append(Item(name, iw, il, ih, ow, ol, oh, f"{i:02d}_{slug(name)}.stl"))

    if box_w <= 2*margin or box_l <= 2*margin:
        raise ValueError("The box is too small for the requested box_margin_mm")
    return (box_w, box_l, box_h), (bed_w, bed_l), (wall, floor, clearance, gap, margin), items


def _intersects(a, b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b
    return not (ax>=bx+bw-1e-8 or ax+aw<=bx+1e-8 or ay>=by+bh-1e-8 or ay+ah<=by+1e-8)


def _contained(a, b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b
    return ax>=bx-1e-8 and ay>=by-1e-8 and ax+aw<=bx+bw+1e-8 and ay+ah<=by+bh+1e-8


def pack_maxrects(items, binw, binl, gap):
    """Built-in 2D MaxRects short-side-fit heuristic (not guaranteed globally optimal)."""
    free = [(0., 0., binw, binl)]
    placements = {}
    for i in sorted(items, key=lambda i: i.outer_w*i.outer_l, reverse=True):
        best = None
        for fr in free:
            x,y,w,h=fr
            for rot,rw,rl in [(0,i.outer_w+gap,i.outer_l+gap), (90,i.outer_l+gap,i.outer_w+gap)]:
                if rw<=w+1e-8 and rl<=h+1e-8:
                    score=(min(w-rw,h-rl), max(w-rw,h-rl), y, x)
                    if best is None or score<best[0]:
                        best=(score, (x,y,rw,rl),rot)
        if best is None:
            raise ValueError(f"Could not place {i.name!r} in the box. Reduce compartments or use stacking/smaller trays.")
        _,placed,rot=best
        placements[i.name] = (placed[0],placed[1],rot)
        remaining=[]
        for (x,y,w,h) in free:
            if not _intersects((x,y,w,h), placed):
                remaining.append((x,y,w,h)); continue
            px,py,pw,ph=placed
            if px>x+1e-8: remaining.append((x,y,px-x,h))
            if px+pw<x+w-1e-8: remaining.append((px+pw,y,x+w-px-pw,h))
            if py>y+1e-8: remaining.append((x,y,w,py-y))
            if py+ph<y+h-1e-8: remaining.append((x,py+ph,w,y+h-py-ph))
        free=[r for j,r in enumerate(remaining) if r[2]>1e-6 and r[3]>1e-6 and
              not any(j!=k and _contained(r,b) for k,b in enumerate(remaining))]
    return placements, "maxrects_fallback"


def pack_rectpack(items, binw, binl, gap):
    """Use rectpack if installed; otherwise use the bundled deterministic fallback."""
    try:
        from rectpack import newPacker, MaxRectsBssf
    except ImportError:
        return pack_maxrects(items, binw, binl, gap)
    # rectpack documentation recommends integer coordinates (avoid floating rounding).
    scale=10  # 0.1 mm layout granularity; round rectangles up, bin down.
    p=newPacker(pack_algo=MaxRectsBssf, rotation=True)
    for i in items:
        p.add_rect(math.ceil((i.outer_w+gap)*scale), math.ceil((i.outer_l+gap)*scale), i.name)
    p.add_bin(math.floor(binw*scale),math.floor(binl*scale))
    p.pack()
    rs=p.rect_list()
    if len(rs)!=len(items):
        missing=set(i.name for i in items)-set(r[-1] for r in rs)
        raise ValueError(f"Could not fit all trays in one box layer: {', '.join(sorted(missing))}")
    orig={i.name:i for i in items}
    out={}
    for _,x,y,w,h,name in rs:
        item=orig[name]
        nw=math.ceil((item.outer_w+gap)*scale)
        nl=math.ceil((item.outer_l+gap)*scale)
        rot=0 if (w,h)==(nw,nl) else 90
        out[name]=(x/scale,y/scale,rot)
    return out, "rectpack_maxrects_bssf"


def generate_tray(item, wall, floor, path):
    """Build real hollow open-top CAD tray with one U-shaped finger access cutout."""
    import cadquery as cq
    shell=cq.Workplane("XY").box(item.outer_w,item.outer_l,item.outer_h, centered=(True,True,False))
    hole=(cq.Workplane("XY").workplane(offset=floor)
          .box(item.contents_w,item.contents_l,item.contents_h+0.5, centered=(True,True,False)))
    part=shell.cut(hole)
    # A horizontal cylinder centered at the top edge removes a semicircle in the front wall.
    if item.outer_w>=35 and item.outer_h>=14:
        radius=min(8.,(item.contents_w)/5., (item.outer_h-floor)*.45)
        notch=cq.Solid.makeCylinder(radius,wall+2.,
                 cq.Vector(0.,-item.outer_l/2.-1.,item.outer_h),cq.Vector(0.,1.,0.))
        part=part.cut(notch)
    cq.exporters.export(part,str(path), exportType="STL")
    return part


def generate(plan, dest):
    box, bed, settings, items = parse_plan(plan)
    bw,bl,bh=box; wall,floor,clearance,gap,margin=settings
    placements, method = pack_rectpack(items,bw-2*margin,bl-2*margin,gap)
    dest.mkdir(parents=True,exist_ok=True)
    records=[]
    for item in items:
        px,py,rot=placements[item.name]
        path=dest/item.filename
        generate_tray(item,wall,floor,path)
        # offset by half the tray gap within each packing rectangle
        x,y=margin+px+gap/2, margin+py+gap/2
        pw,pl=(item.outer_w,item.outer_l) if rot==0 else (item.outer_l,item.outer_w)
        records.append({
            "name":item.name,"stl":item.filename,
            "position_mm":{"x":round(x,2),"y":round(y,2),"z":0},
            "rotation_degrees":rot,
            "outer_mm":{"width":round(item.outer_w,2),"length":round(item.outer_l,2),"height":round(item.outer_h,2)},
            "footprint_in_box_mm":{"width":round(pw,2),"length":round(pl,2)},
            "cavity_mm":{"width":round(item.contents_w,2),"length":round(item.contents_l,2),"depth":round(item.contents_h,2)},
        })
    layout={"game":plan.get("game","Untitled"), "units":"mm", "method":method,
            "box_inner_mm":{"width":bw,"length":bl,"height":bh},
            "printer_bed_mm":{"width":bed[0],"length":bed[1]},
            "constraints":{"wall_mm":wall,"floor_mm":floor,"clearance_mm":clearance,"tray_gap_mm":gap,"box_margin_mm":margin},
            "notes":["Single flat layer of separate trays, not a stacked system.",
                     "Each required_mm specifies the total stored group size, not one piece.",
                     "Print each STL flat-side-down; verify slicer checks and a physical fit before production.",
                     "Packing heuristic is not guaranteed optimal."],
            "trays":records}
    (dest/"layout.json").write_text(json.dumps(layout,indent=2),encoding="utf-8")
    return layout


def main():
    p=argparse.ArgumentParser(description="Generate open-top 3D-printed board game organizer trays")
    p.add_argument("plan",help="reviewed JSON compartment plan")
    p.add_argument("-o","--out",default="generated_organizer")
    args=p.parse_args()
    try:
        plan=json.loads(Path(args.plan).read_text(encoding="utf-8"))
        result=generate(plan,Path(args.out))
    except (ValueError,OSError) as e:
        print(f"ERROR: {e}",file=sys.stderr)
        return 1
    print(f"Generated {len(result['trays'])} printable STL files + layout.json in {Path(args.out).resolve()}")
    print("Packing algorithm:",result["method"])
    return 0


if __name__=="__main__":
    raise SystemExit(main())
