# Inside the Box — Parametric Organizer Engine

Turns measured board-game components into printable organizer trays:

```
component measurements → grouping → spatial optimisation → parametric CAD → STL + GLB → 3-D preview
   (CV scripts / plan)    (trays)     (layers, packing)      (CadQuery)    (validated)  (preview/)
```

Everything is in **millimetres**. No LLM or paid API is called here. The computer-vision scripts
(`boardgame_dimensions_relative.py`, `boardgame_dimensions_ruler.py`) are unchanged.

## Install

```bash
pip install -r requirements-engine.txt   # cadquery, numpy (rectpack optional), pytest
```

Tested with CadQuery 2.8.0 on Python 3.12. `rectpack` is optional: the engine always runs its own
MaxRects packer and adds rectpack's packers as extra candidates when installed.

## Quick start

```bash
# Original example (the v1 JSON format still works unchanged)
python generate_organizer_stl.py organizer_plan_example.json -o generated_organizer

# Typed components: cards with sleeves, coins, dice pockets, grouped player pieces…
python generate_organizer_stl.py examples/typed_demo_plan.json -o examples/output/typed_demo

# Same components in a smaller box → trays are stacked in 2 layers
python generate_organizer_stl.py examples/stacked_demo_plan.json -o examples/output/stacked_demo

# Validate + pack only (no CAD, instant)
python generate_organizer_stl.py examples/typed_demo_plan.json --check

# Start from the CV output instead of a hand-written plan
python generate_organizer_stl.py --from-cv carcassonne_dimensions.json --box 290 290 70 --bed 256 256 256 -o out

# Preview (any static server works; the GLB can't load from file://)
python -m http.server 8000
#   open http://localhost:8000/preview/                       (generated_organizer/organizer.glb)
#   open http://localhost:8000/preview/?glb=../examples/output/stacked_demo/organizer.glb
```

All example dimensions are **made up for testing** and are not from a real game edition.

Exit codes: `0` success · `1` invalid plan / missing measurements / does not fit (a `report.json`
with every problem is written to the output folder) · `2` geometry was generated but failed validation
(files kept for inspection, `layout.json` has `"status": "invalid"`).

Flags: `--check`, `--allow-partial` (skip compartments that have errors, generate the rest),
`--no-glb`, `--no-thickness-check`, `--from-cv FILE --box W L H --bed W L [H]`.

## Input plan

```json
{
  "game": "My game",
  "box_inner_mm":   {"width": 296, "length": 296, "height": 72},
  "printer_bed_mm": {"width": 256, "length": 256, "height": 256},
  "settings": {"headroom_mm": 8},
  "compartments": [ ... ]
}
```

`box_inner_mm` is the **inside** of the box (floor to lid underside). `printer_bed_mm.height` is optional
(a warning says Z is not checked). Missing measurements are never guessed: they are reported as
`missing_measurement` errors naming the exact field.

### Compartment types

| `type` | Required fields | Geometry |
|---|---|---|
| `cards` | `piece_mm {width, length, thickness}`, `quantity` (or `stack_height_mm`). Sleeved: `sleeve_mm {width, length, thickness}` (sleeved card) | Flat well, U-shaped finger notches in both long walls. A stack taller than the box is an error that names the `stacks` value needed; it is never split automatically. |
| `tiles` | same as cards | same as cards |
| `round_tokens` | `piece_mm {diameter, thickness}`, `quantity`, optional `stacks` | One cylindrical well per stack, finger slot from each well out through the outer wall. |
| `dice` | `piece_mm {edge}` (or width/length/height), `quantity`, optional `pockets: true` | Scooped bin (dice in a grid, up to 2 deep) or one pocket per die. |
| `bits` | `piece_mm {width, length, height}` + `quantity`, **or** `required_mm`; optional `cells: n` | Scooped bin (rounded bottom edges). Size from piece boxes uses `bulk_packing_efficiency` and is reported as an assumption. |
| `generic` | `required_mm {width, length, height}` | Rectangular bin with a shallow rim notch (the v1 behaviour). |
| `set` | `storage_style` (`loose`/`stack`/`card_stack`/`tight_pack`) + `members[] {name, quantity, shape, dimensions_mm {length, width, height}}`, or `required_mm` | One plain open rectangular cavity for the whole set (see organizer `boxes`). |

Common optional fields: `"tray": "Player pieces"` puts several compartments in one tray, separated by
dividers (e.g. one cell per player colour); `"layer": "top" | "bottom"` hints the stacking order.

**Dividers are never assumed.** Internal walls only appear where the plan asks for separate cells:
several compartments sharing a `tray`, `stacks` (cards, tiles, tokens), `cells` (bins) or dice
`pockets: true`. A compartment without these is one cavity, however much the tray grows.
(The material between round-token wells is part of the well shape, not a divider.)
Any type also accepts `required_mm` (the bounding box of the whole stored group, as in v1).

**v1 compatibility:** entries with only `name` + `required_mm` still work with the same cavity maths
(`required + 2·clearance`, depth `height + clearance`). Their type is inferred from the name
("cards", "tiles", "meeples", "dice"…) only to pick finger-access features. Set
`"fill_box": false, "fill_height": false` to get exactly the v1 tray sizes.

### Organizer `"boxes"` (simple removable boxes)

Set `"organizer": "boxes"` at the top of the plan to get **one plain open-top box per set** — the
grouping decided upstream is used as-is:

- every compartment is a `set`; exactly one rectangular cavity, no dividers, pockets, wells,
  notches, scoops or automatic subdivisions (`tray`, `stacks`, `cells`, `pockets` and shaped types
  are rejected with an error);
- `stack`: cavity = largest piece footprint + clearance, depth = whole stack + clearance. A stack that
  is too tall is an error — it is never split;
- `card_stack`: the same for one card deck lying flat — cavity = card length × width + clearance, depth =
  quantity × card thickness (`dimensions_mm.height`) + clearance. No card slots; the deck is never split;
- `loose`: cavity = smallest rectangle in which every piece's bounding box can be laid out without
  overlap (one layer, more only if one layer can't fit), + clearance. The arrangement is written to
  `layout.json` (`cells[].contents.arrangement_mm`). Pieces poured in at random may need settling, so
  physical fit is reported as not verified;
- `tight_pack`: block-like pieces (`rectangular_prism`, `square_prism` or `cube` only; other shapes are an
  error) placed by hand in identical grid layers. Same arrangement as `loose`, but it uses as many layers
  as give the smallest footprint (a compact block), and reports that the pieces must be placed, not poured;
- the boxes together fit the game box: by default (`fill_height`/`fill_box` true) every box is the full
  usable height (flush with the lid) and the boxes grow to cover the floor area. Each box's single cavity
  grows and deepens with it, so walls stay `wall_mm` and the floor stays `floor_mm` thick (no solid fill). Set
  `"fill_height": false, "fill_box": false` for compact boxes sized only to their contents. Boxes are put in
  one layer; two are stacked (same footprint, fully supported) only if one layer cannot fit (`max_layers: 2`).
  If that is not enough either, one box may rest on several boxes that stand at one common height and
  together cover its footprint (a *bridge*, e.g. King of Tokyo's Monster Kits on the other four sets).
  Several small boxes are never put on top of one larger box: it is open, so they would sit over its cavity;
- boxes that hold the same thing (e.g. one bus set per player) always come out the **same size**: they grow
  together, never one more than another (listed as "Same contents, same size" in `layout.json` notes);
- **similarly named** sets share one box shape and size too: names that match once numbers, colours and
  bracketed text are ignored, or that share their first or last two words ("Transportation Card Deck" ~
  "Destination Ticket Card Deck", "Shared Supply: Dice" ~ "Shared Supply: Battle Tokens"). The shared box is
  big enough for each member (largest short side, long side and height), all lie the same way, and they grow
  together. Each tray's `size_group` in `layout.json` names the group. Turn off with
  `"similar_names_same_size": false`;
- with `fill_box`, each kind of set (a group of equal boxes, or a single box) becomes one rectangular block
  laid out as a full grid, and the blocks tile the **whole** box floor with straight cuts (up to 5 kinds;
  more fall back to the general packer). The chosen layout gives different kinds clearly different boxes
  (≥ 25 % apart in width or length), avoids boxes longer than 2.5 × their width when possible, then grows
  every box most evenly;
- after export, vertical rays over each STL prove there is exactly one open rectangular cavity
  (`mesh_validation.single_open_cavity`).

### Bottom layer (board under the trays, hand-written plans only)

```json
"bottom_layer": [{"name": "Folded game board", "quantity": 1,
                  "dimensions_mm": {"length": 245, "width": 245, "height": 8}}]
```

Flat items listed here lie on the box floor **under** the storage. They are never printed and are not
lid headroom: every tray starts at their height (`floor_offset_mm`), the usable height is
`box height − bottom layer − headroom_mm`, and trays are placed only where the board covers the floor in
every position it can slide to (a 245 mm board in a 250 mm box → x, y ∈ [5, 245]). The preview draws the
board as a non-printed slab. `convert_grouping_output.py` never writes a bottom layer: an excluded board
or rulebook only shortens the boxes (headroom), see below.

### Settings (defaults)

| setting | default | meaning |
|---|---|---|
| `wall_mm` / `floor_mm` | 2.4 / 2.0 | outer wall; minimum floor under the deepest cavity (both ≥ 1.2) |
| `clearance_mm` | 1.0 | play per side around contents (+ once above) |
| `divider_mm` | 1.6 | internal walls between cells |
| `min_feature_mm` | 1.2 | thinnest section the mesh check accepts |
| `tray_gap_mm` / `box_margin_mm` | 1.0 / 1.0 | air between trays / between trays and the box walls |
| `headroom_mm` | 0 (warns) | height kept free under the lid for the board and rulebook — **measure it** |
| `fill_height` | true | raise each tray to the usable height (flush top, contents sit near the rim on a raised floor) |
| `fill_box` | true | grow trays into empty floor space so nothing rattles; bins grow with the tray, fixed cavities keep their size (solid wall around them) — no extra pockets or dividers; identical trays stay identical |
| `max_layers` | 3 (boxes: 2) | max trays stacked in one column |
| `similar_names_same_size` | true | organizer `boxes`: similarly named sets share one box shape and size |
| `finger_notch_width_mm`, `scoop_radius_mm` | 20, 12 | ergonomics |
| `bulk_packing_efficiency` | 0.6 | loose-pile density used only for `bits` sized from piece boxes |
| `stl_tolerance_mm`, `stl_angular_tolerance_rad` | 0.05, 0.2 | STL tessellation accuracy |

These are printer/process defaults, not game measurements.

## How it works

1. **Validate** (`organizer/schema.py`) — collects every error, warning and missing measurement in one pass.
2. **Design cells** (`organizer/components.py`) — per-type cavity sizes from piece size × quantity. Scooped
   cavities are enlarged by 0.3 × scoop radius so the curve never pushes contents above the rim.
3. **Group** (`organizer/grouping.py`) — cells of one tray are laid out in shelf rows; the smallest
   arrangement that fits both the printer bed and the box wins.
4. **Pack** (`organizer/packing.py`) — tries 5 MaxRects heuristics × 7 sort orders (+ rectpack) for a
   single layer. If nothing fits, it stacks trays into columns, merging the pair that saves the most floor
   area while the column height fits the box. Trays in a column share one footprint, so every upper tray
   is fully supported. If columns are not enough, it searches *bridges* (every top tray × every set of 2+
   trays under it, up to 8 trays): the trays under it are raised to the tallest one's height and tiled
   across the top tray's footprint (it grows if they need more room), and the rest are packed as before.
   Layout validation checks that each upper tray rests on solid rims and walls surrounding the middle half
   of its base, not only that its footprint overlaps the trays below. With `fill_box`, the best layout is
   scaled proportionally to the box and remaining
   gaps are filled; trays with identical contents are kept the same size (each group grows only by amounts
   all its members can take); candidates are ranked by covered floor area, then by how evenly trays grew.
5. **CAD** (`organizer/cad.py`) — CadQuery: rounded outer box minus cavities (cut from the rim down),
   scoop fillets, U-notches, coin wells + slots. Must produce exactly one valid solid.
6. **Validate output** (`organizer/mesh.py`, `pipeline.validate_layout`) — on the triangles actually written to
   each STL: watertight, 2-manifold, consistent winding, outward normals, one shell, bbox and volume match the
   CAD model, and ray-cast minimum wall/floor thickness ≥ `min_feature_mm`. Layout level: inside the box and
   usable height, no 3-D collisions, upper trays ≥ 95 % supported, fits the printer, walls/dividers/floors ≥ settings.
7. **Export** — one STL per tray, `organizer.glb`, `layout.json`.

## Outputs

### `layout.json` (schema_version 2)

All v1 keys are kept with the same meaning (`name`, `stl`, `position_mm`, `rotation_degrees`, `outer_mm`,
`footprint_in_box_mm`, `cavity_mm`, `box_inner_mm`, `constraints`, `notes`…). New keys per tray include:

- `layer`, `column`, `center_mm`, `transform_mm` (row-major 4×4: STL frame → box frame)
- `assembled_position_mm`, `exploded_position_mm`, `assembly_order` — for animations
- `cells[]` (cavity centre/size, floor thickness, contents), `features[]` (notches, slots, scoops, dividers)
- `mesh_validation` (all checks above, with numbers)

Top level: `status`, `usable_height_mm`, `stats` (layers, floor coverage), `warnings`, `assumptions`,
`skipped_compartments`, `validation`, `coordinate_system`.

**Coordinates:** origin = lower-left corner of the box interior floor, z up, mm. Each STL is centred on
x/y = 0 with its base at z = 0. To place it: rotate by `rotation_degrees` counter-clockwise about +z, then
translate to `center_mm` (or apply `transform_mm`).

### `organizer.glb`

Built from the **same triangles** as the STL files (read back from disk), so the preview always matches the
print. Root node `InsideTheBox` rotates Z-up → glTF Y-up and scales mm → m. Children: a translucent
`Game box (preview only)` (`extras.printable = false`) and one node per tray, positioned in box millimetres.
Tray `extras` carry `name`, `stl`, `layer`, `assembled_translation_mm`, `exploded_translation_mm` and
`assembly_order`. For an exploded view, lerp each tray node's `position` between those two vectors.
three.js `GLTFLoader` exposes `extras` as `object.userData`.

### `preview/index.html`

A single-file three.js viewer (CDN) with an explode slider, an animated assembly, box toggle and
click-to-inspect. It reads `?glb=` (default `../generated_organizer/organizer.glb`).

## From Holden's grouping output

```bash
python convert_grouping_output.py ttrloutput.json --bed 256 256 256 -o holden_final/ttr_london_6_boxes/plan.json
python generate_organizer_stl.py holden_final/ttr_london_6_boxes/plan.json -o holden_final/ttr_london_6_boxes
python -m http.server 8000   # → http://localhost:8000/preview/?glb=../holden_final/ttr_london_6_boxes/organizer.glb
```

The grouping JSON is the single source of truth:

- `box.dimensions_mm` is the space the boxes must fit in (used as the inside size; an estimated size is
  flagged);
- every entry of `sets` becomes exactly one `set` box (names, quantities, dimensions, shapes and
  `storage_style` — `loose`, `stack`, `card_stack` or `tight_pack` — copied verbatim), for `organizer: "boxes"` and
  `"divider"` alike. No sets are added, merged or split;
- everything in `excluded` is **ignored**: no container, no floor space and no fit check. It is listed in
  `conversion_notes` and `source.excluded_ignored`. The one exception is height: an excluded **board or
  rulebook** (found by name: board, map, rules, rulebook, leaflet, booklet, manual, instructions) lies on
  top of the boxes, so its listed `dimensions_mm.height × quantity` becomes `settings.headroom_mm` and every
  box is that much shorter (`source.on_top_of_boxes`). A board/rulebook without a height is reported as
  `MISSING`, never guessed.

`--compact` sizes boxes to their contents instead of filling the game box; `--typed` restores the earlier
divider conversion (typed compartments per member, one tray per set). All conversion problems are listed
together and nothing is guessed. The 256 mm bed is an illustrative value — use your printer's real build
volume.

## From the CV output

`--from-cv` (or `organizer.plan_from_cv`) maps the CV JSON to a draft plan (`plan_draft.json`): cards and tiles
become stacks (CV `height` = thickness), flat cylinders become `round_tokens`, dice become `dice`, everything
else `bits`. The box, boards and rulebooks are not stored in trays; their measurements are turned into notes
(the box size from photos is the *outside*, and the board/rulebook stack belongs in `headroom_mm`). Anything
the CV step didn't measure — card thickness, quantities, the box interior, the printer bed — stays missing and
is reported, so edit the draft and re-run with the draft as the plan.

## Tests

```bash
python -m pytest tests -q        # ~15 s; geometry tests are skipped if CadQuery is missing
```

Covers parsing and every error path, cavity maths, missing-measurement reporting, randomised packing
(no overlaps, in bounds), fill-box expansion, stacking support, printer-bed limits, mesh checks on synthetic
meshes, GLB round-trip, full CAD runs of all three example plans (every STL validated, no 3-D collisions,
GLB triangles == STL triangles), the CLI and the CV adapter.

## Limitations (be honest in the demo)

- Not physically print-tested yet. Default tolerances (clearance 1 mm, walls 2.4 mm) are starting points;
  print a test tray before a full set.
- Trays are open-top rectangles; no lids, snap fits, stacking lips, card-ramp floors or hex/irregular-outline
  cavities. Irregular pieces use bounding boxes.
- Packing is heuristic (single-layer MaxRects variants + greedy stacking), not guaranteed optimal. Stacked
  trays share a footprint (simple and always supported, but sometimes less compact than free 3-D packing).
- Trays larger than the printer bed are rejected rather than split automatically.
- `bits` sized from piece boxes depend on the assumed packing efficiency — prefer measuring a real pile
  (`required_mm`).
- Raised floors are solid CAD; they print fast with low infill but still add filament.
- The output folder is treated as generated: stale `NN_*.stl` files from a previous run are removed.
