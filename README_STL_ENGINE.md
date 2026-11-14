# GameFit AI — Printable 3D Organizer Engine (hackathon MVP)

This folder originally contained two board-game component measurement pipelines:
- `boardgame_dimensions_relative.py` — relative measurements from images plus scale anchor
- `boardgame_dimensions_ruler.py` — measurements with an image ruler

**Added:** `generate_organizer_stl.py`, a deterministic CAD engine which uses a reviewed placement plan to generate individually printable open-top trays. This does not call an LLM or require Higgsfield.

## Installation

```bash
pip install cadquery rectpack
```

`rectpack` is optional: if missing, the generator uses its included MaxRects heuristic. The generator was run and verified with CadQuery 2.8.0 (other versions may work).

## Generate the example

```bash
python generate_organizer_stl.py organizer_plan_example.json -o generated_organizer
```

The output directory contains:
- One binary **STL per compartment** (printable 3-D geometry)
- `layout.json` with placement coordinates, rotation, cavity and outer dimensions for the web frontend

The example measurements are fictional and for geometry testing only; they do **not** represent a verified game edition.

## Contract for your teammates

The CV service returns one inventory entry per *type* of component, e.g. cards `width=63`, `length=88`, `height=0.3`, `quantity=60` (all mm). Before feeding this into the CAD engine, the grouping/compartment planner must:

1. Decide which physical pieces belong together (e.g. cards in one stack, meeples grouped by player color).
2. Compute the bounding dimensions of the **stored group** (`required_mm`), **not** one individual piece. E.g. a stack of 60 cards each 0.3 mm thick is roughly `63 x 88 x 18 mm` before tolerance.
3. Supply the actual **inside** size of the board-game box and the actual printer bed dimensions.
4. Confirm any unknown measurement rather than inventing a physically precise value.
5. Send the finished JSON plan to this engine.

Example input (see full example file):

```json
{
  "game": "Example game",
  "box_inner_mm": {"width": 210, "length": 210, "height": 50},
  "printer_bed_mm": {"width": 220, "length": 220},
  "settings": {
    "wall_mm": 2.4,
    "floor_mm": 2,
    "clearance_mm": 1,
    "tray_gap_mm": 1.5,
    "box_margin_mm": 1.5
  },
  "compartments": [
    {"name": "Playing cards", "required_mm": {"width": 66, "length": 92, "height": 26}}
  ]
}
```

## What the algorithm actually does

1. Validate mm values, unique names, nonzero dimensions, and maximum box/printer heights.
2. Add print clearance around components, walls, and a solid floor.
3. Run a 2D MaxRects packing heuristic (uses `rectpack` if installed, otherwise bundled fallback).
4. Reject layouts that cannot fit **without collisions** into a *single layer* of the existing box.
5. Create each tray with CadQuery solid-subtraction (outer prism minus cavity) and a front-side U-shaped finger-access notch.
6. Export each part to `.stl` and return its box coordinates in `layout.json`.

## Frontend integration

The frontend can import each STL mesh with `STLLoader` from three.js and position each tray from `layout.json`:

- STL geometry is centered on (0,0) in x/y with base at z=0.
- `position_mm` is the bottom-left of the rotated tray's bounding footprint, measured from the lower-left of the box interior.
- `rotation_degrees`: 0 or 90. Rotate the STL about its z-axis, then translate it to `(x + footprint_width/2, y + footprint_length/2, 0)`.
- **three.js is normally y-up**; convert the backend's z-up coordinates for rendering.
- For a brief animation, first render each tray elevated above the box and interpolate its z-position down to 0 in the correct order.

## Limitations / practical checks

- Current prototype: independent trays, **one flat layer only**, rectangular compartments. Board/lid and stacked tray layers are not handled yet.
- A solver may return `layout cannot fit`. That is useful: don't silently scale components smaller. Add multi-layer packing or select a larger box.
- `required_mm` must be the complete grouped content dimensions; no automatic stacking logic exists here.
- Default floor 2 mm, wall 2.4 mm, per-side clearance 1 mm are **starting defaults**, not universal printer-specific guarantees.
- STL files have no authoritative unit metadata. Treat every coordinate as millimetres when slicing.
- Test with Cura/PrusaSlicer/Bambu Studio, check mesh repair, overhangs, tolerances, wall thickness, print-bed boundaries, and physical box fit before claiming the result prints correctly.
- This program was validated on example inputs and STL watertightness; it has not been tested with physical prints.

## Suggested next hackathon milestones

1. Wire CV JSON into a reviewed compartment planner.
2. Allow user edits: sleeve allowance, extra clearance, divider count, grouped-by-player, etc.
3. Add stacking/multi-layer trays and split a tray when it exceeds the printer bed.
4. Add interactive STL/GLB viewer and animation in the existing frontend.
