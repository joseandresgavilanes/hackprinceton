# Inside the Box: test grouping JSON with the 3D engine

This test connects `exoutput.json` (AI grouping output) to the new organizer v2 engine.
The game is hypothetical and its physical fit is NOT verified.

## What is in this package
- `exoutput.json`: the original grouping output (`organizer: "divider"`).
- `examples/holden_boxes_grouping.json`: the latest grouping output (`organizer: "boxes"`, 4 sets),
  copied unchanged from `exoutput (1).json`.
- `convert_grouping_output.py`: converts `sets[].members[]` into the engine's typed `compartments[]`.
- `inside_the_box_holden_plan.json`: plan converted using a **sample 256 x 256 x 256 mm** printer bed.

## Run from inside your repository

Make sure Claude's updated engine is actually present (`organizer/`, `preview/`, and
`generate_organizer_stl.py`). Replace sample printer-bed measurements with those
of the physical printer you intend to use.

```bash
# latest grouping output (organizer "boxes"): one plain open box per set
python convert_grouping_output.py examples/holden_boxes_grouping.json --bed 256 256 256 -o examples/holden_boxes_plan.json
python generate_organizer_stl.py examples/holden_boxes_plan.json --check
python generate_organizer_stl.py examples/holden_boxes_plan.json -o holden_boxes_demo
python -m http.server 8000
```

(`exoutput.json`, the earlier `organizer: "divider"` output, still converts the old way.)

In a browser, open:

http://localhost:8000/preview/?glb=../holden_boxes_demo/organizer.glb

The STL files will be in `holden_boxes_demo/`, along with `organizer.glb` and `layout.json`.

## Read before printing
- The 250 x 250 x 65 mm **inside** box size comes from the supplied JSON.
- The folded board is 245 x 245 x 8 mm, excluded from tray cavities.
- The board becomes the plan's `bottom_layer`: the boxes rest on top of it (z = 8 mm),
  57 mm remain for them, and the GLB preview draws the board as a non-printed slab.
  It is not treated as headroom under the lid.
- The printer bed set to 256 x 256 x 256 mm is an illustrative setting, NOT
  a verified printer specification.
- Loose boxes are sized so every piece's bounding box fits in one arranged layer; pieces
  poured in at random may need settling.
- The upstream input explicitly says `physical_fit_verified = false`.
- Do not run `--from-cv` on `exoutput.json`: this is grouping output, not CV output.
