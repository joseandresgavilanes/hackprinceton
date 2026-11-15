# Inside the Box: test grouping JSON with the 3D engine

Holden's grouping JSON → one simple rectangular storage box per set → packing inside
`box.dimensions_mm` → validated STL + GLB → interactive 3D preview.
None of the physical fits below are verified (`physical_fit_verified = false` upstream).

## Inputs
- `exoutput.json`: first grouping output (`organizer: "divider"`, 3 sets).
- `examples/holden_boxes_grouping.json`: copy of `exoutput (1).json` (`organizer: "boxes"`, 4 sets).
- `ttrloutput.json` / `examples/ttr_london_grouping.json`: Ticket to Ride: London (`organizer: "boxes"`,
  6 sets, two of them `card_stack`).

No manual edits to these files are needed.

## Rules the converter applies
- `box.dimensions_mm` is used as the inside size of the game box.
- Each entry of `sets` becomes exactly one plain open-top box with one rectangular cavity, whatever
  the `organizer` value. There are no dividers, pockets, wells or slots, and no sets are added.
- `storage_style`:
  - `loose`: the cavity fits every piece's bounding box laid out side by side (assumption reported).
  - `stack`: one vertical stack of the pieces.
  - `card_stack`: one deck lying flat; depth = quantity × card thickness.
  - `tight_pack`: block-like pieces (rectangular prisms, square prisms, cubes) placed by hand in
    identical grid layers, as compact as the height allows.
- Sets that hold the same thing (e.g. the four player bus sets) always get boxes of exactly the same size.
- Similarly named sets ("Transportation Card Deck" / "Destination Ticket Card Deck", "Shared Supply: …")
  share one box shape and size, big enough for each of them (`size_group` in `layout.json`).
- All boxes together fill the whole box floor; different kinds of set get clearly different box shapes.
- Everything in `excluded` is ignored: no container, no floor space, no fit check. TTR's board
  (236.6 mm) is larger than its estimated 226 mm box and generation still succeeds.
- Except height: an excluded board or rulebook lies on top of the boxes, so every box is shorter by its
  listed thickness (TTR: 4 mm board + 12.1 mm rulebook → 44 − 16.1 = 27.9 mm boxes; Holden: 8 mm board →
  57 mm boxes).

## Run

```bash
python convert_grouping_output.py ttrloutput.json --bed 256 256 256 -o holden_final/ttr_london_6_boxes/plan.json
python generate_organizer_stl.py holden_final/ttr_london_6_boxes/plan.json -o holden_final/ttr_london_6_boxes
python -m http.server 8000
```

Then open http://localhost:8000/preview/?glb=../holden_final/ttr_london_6_boxes/organizer.glb

Swap the input and folder for `exoutput.json` → `holden_final/exoutput_3_boxes` and
`"exoutput (1).json"` → `holden_final/exoutput_1_4_boxes`.

Options:
- `--compact`: each box is sized only to its contents. Without it, all boxes are the full box height
  and grow to cover the floor; each cavity grows with its box, so no solid plastic is added.
  Example output: `holden_final/ttr_london_6_boxes_compact`.
- `--typed`: the earlier divider conversion (typed compartments per member).

## Read before printing
- TTR's 226 × 226 × 44 mm box is **estimated** from the outside size; measure the real interior.
- The board/rulebook thickness comes from the JSON (TTR lists a 12.1 mm rule leaflet, which looks
  thick for a leaflet); measure them, since the box height depends on it.
- The 256 × 256 × 256 mm printer bed is illustrative, NOT a verified printer specification.
- Loose boxes are sized so every piece's bounding box fits in one arranged layer; pieces poured in at
  random may need settling.
- Do not run `--from-cv` on these files: they are grouping output, not CV output.
