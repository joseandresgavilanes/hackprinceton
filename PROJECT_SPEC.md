# Inside the Box — Project Specification

> Written for an LLM that needs to understand or work on this repository. All sizes are in
> **millimetres**. "Box" means the board game's cardboard box; "storage box" / "tray" means a
> 3D-printed part that goes inside it.
>
> **Status:** 2026-11-14, after PR #1 (organizer engine v2, including the King of Tokyo fix), PR #2 (web UI)
> and PR #3 (backend, viewer stacking fix, `tight_pack`, first live run). §12 says what works today; §13 lists what is still missing.

## 1. What the project does

Inside the Box (a HackPrinceton project) designs **3D-printable organizers for board games**:

```
game name + 2–6 ruler photos ─► CV measurement ──► grouping into sets ──► converter ──► CAD engine ──► STL + GLB + layout.json ──► web UI / 3D preview
 (frontend/)                    (…ruler_v7.py,      (organize_game_       (convert_      (organizer/)                               (frontend/, preview/)
                                 Claude + OpenCV)    boxes.py, Claude)     grouping_
                                                                           output.py)
```

1. **Measure** each kind of game component from the user's photos with a ruler in frame (CV + Claude
   vision), and write a *dimensions JSON*.
2. **Group** components into storage sets. An LLM step ("Holden's grouping") researches the rules and
   box size on the web and writes a *grouping JSON* (§3).
3. **Convert** the grouping JSON into an engine *plan* (§4).
4. **Generate** one simple open-top storage box per set, packed to fill the game box. The parametric
   CadQuery engine writes one STL per box, an assembled `organizer.glb` and `layout.json`.
5. **Show** the result: the web UI (`frontend/`) for the user flow and downloads, with a full-screen 3D
   studio, and the same viewer as a standalone page (`preview/`).

The backend (`backend/`, `python -m backend`) runs the five stages as one job behind the web UI's API; each
stage also still works as its own command-line script (§10).

## 2. Repository map

| Path | Role |
|---|---|
| `backend/` | FastAPI server for the whole journey (`python -m backend`, see `backend/README.md`): serves the web UI and the job API of `frontend/BACKEND_CONTRACT.md`, and runs measure → group → convert → generate as a background job per upload, in `organizer_jobs/<id>/`. **fixtures** mode (default) reads committed CV/grouping JSON (no paid calls); **live** mode runs the `Final stuff/` scripts with the server's `ANTHROPIC_API_KEY`. The converter and CAD engine always run for real. |
| `frontend/` | "GameFit" web UI (PR #2): game name + 2–6 ruler photos → loading scene with the job's stage → optional measurement question → STL downloads + three.js viewer. `config.js` points it at the backend's API when the backend serves it; served statically, it stays in demo mode and shows `generated_organizer/`. |
| `Final stuff/` | The folder the team started for the final pipeline: `boardgame_dimensions_ruler_v7.py` (CV, identical to `boardgame/`'s copy) and `organize_game_boxes.py` (newest grouping script; always writes `organizer: "boxes"`). |
| `boardgame/` | Pipeline workspace. CV scripts (`boardgame_dimensions_ruler.py`, `_relative.py`, `_cv.py`, `_ruler_v7.py`), grouping scripts (`organize_game.py`, `_compatible.py`, `_shapes.py`), sample dimensions JSONs (`ticket_to_ride_london_dimensions.json`, `root_dimensions.json`, `excape_dimensions.json`, `example_game.json`), grouping outputs, and test photos (`photos/`). |
| `boardgame_dimensions_ruler.py`, `boardgame_dimensions_relative.py` (root) | Earlier CV versions; they differ from the `boardgame/` copies. Ruler: perspective from the ruler's 4 corners (homography), GrabCut outlines, Claude marks ruler and pieces. Relative: one least-squares log-scale solve across photos plus one real measurement as anchor. **Do not modify unless absolutely necessary.** |
| `convert_grouping_output.py` | Grouping JSON → engine *plan* JSON (§4). |
| `generate_organizer_stl.py` | CLI for the engine: plan → STL/GLB/layout. |
| `organizer/schema.py` | Plan parsing, `Settings`, `Plan`, issue collection (errors, warnings, missing measurements). |
| `organizer/components.py` | Per-type cavity designers → `Cell`s. `_set_box` handles organizer `boxes`. |
| `organizer/grouping.py` | `TrayDesign`: cells arranged inside one tray; `place_cells`. |
| `organizer/packing.py` | Spatial optimisation: block layout, MaxRects/rectpack, fill-box growth, column stacking, bridges, size groups. |
| `organizer/cad.py` | CadQuery solid: rounded outer box minus cavities (plus notches, wells and scoops in tray mode). |
| `organizer/mesh.py` | numpy-only STL IO and validation (watertight, manifold, thickness ray-casts, single-cavity check). |
| `organizer/gltf.py` | Writes the GLB from the exact STL triangles. |
| `organizer/pipeline.py` | Orchestration (`plan_layout`, `generate`), layout validation (incl. rim support), `layout.json`, failure `report.json`. |
| `organizer/cv_adapter.py` | CV dimensions JSON → draft *tray-mode* plan (bypasses grouping). Never fills gaps. |
| `frontend/viewer/` | Shared three.js 0.160 viewer: `organizer-viewer.js` draws each STL placed by `layout.json` (stacked boxes keep their height) with selection, details panel + STL download, isolate/hide, dimension lines, Iso/Top/Front, fit/reset, explode + assembly, game box toggle, filament colours; `contents.js` computes approximate non-printed stand-ins for the stored pieces (tested in `tests/js/`). Visual only. |
| `preview/index.html` | Standalone page around that viewer: `?layout=` or `?glb=` (reads the folder's `layout.json` + STLs), plus deep links (`view`, `explode`, `contents`, `select`, `filament`, `box`). |
| `tests/` | pytest suite (121 tests, one of them runs the viewer's JS tests with Node): engine, converter, backend API and the viewer's contents module. Nothing tests the CV or grouping scripts or the web UI's interaction code (checked by hand in a browser). |
| `examples/` | Sample plans, Holden's grouping JSONs (`holden_boxes_grouping.json` = 4 sets, `ttr_london_grouping.json` = Ticket to Ride: London). |
| `exoutput.json`, `ttrloutput.json`, `kotoutput.json` | Holden's grouping outputs: 3 sets ("divider"), TTR London with 6 sets, King of Tokyo with 5 sets. |
| `holden_final/` | **Current** generated results for every grouping JSON (§7), plus a compact TTR variant. |
| `holden_boxes_demo/`, `holden_demo/`, `generated_organizer/`, `examples/output/` | Older demo outputs. Keep them; don't overwrite them. |
| `README_STL_ENGINE.md`, `HOW_TO_TEST_HOLDEN_DATA.md` | User docs for the engine and for running Holden's data. |

Dependencies:
- **Engine:** `requirements-engine.txt`: `cadquery>=2.4` (tested 2.8.0, Python 3.12), `numpy`, optional
  `rectpack`, and `pytest`. The engine calls no LLM or paid API.
- **CV** (`…ruler_v7.py`): `opencv`, `numpy`, `scipy`, `Pillow` (+ optional `pillow_heif`),
  `pydantic`, `langchain-anthropic`; model `claude-sonnet-5-5`.
- **Grouping** (`organize_game_boxes.py`): `anthropic`, `pydantic`, the web-search tool; default model
  `claude-sonnet-4-6` (overridable with `ANTHROPIC_MODEL`).
- **Backend:** `requirements-backend.txt` (`fastapi`, `uvicorn`, `python-multipart`, `httpx` for tests).
- The CV and grouping dependencies are in `requirements-live.txt` (not yet tested from a clean install).

## 3. Input: Holden's grouping JSON

Produced by the grouping step (newest: `Final stuff/organize_game_boxes.py <dimensions.json> <out.json>`,
which accepts the CV ruler JSON or the `example_game.json` schema). The converter must accept it
**unedited**.

```jsonc
{
  "game": "Ticket to Ride: London",
  "edition": "Days of Wonder, 2019",
  "organizer": "boxes",            // "boxes" (> divider_set_limit sets) or "divider" (≤ limit); the newest script always writes "boxes"
  "divider_set_limit": 5,
  "set_count": 6,
  "physical_fit_verified": false,
  "box": {
    "dimensions_mm": {"length": 226.0, "width": 226.0, "height": 44.0},
    "basis": "estimated_from_external",   // "user_supplied" | "reported_internal" | "estimated_from_external" | "unknown"
    "explanation": "..."
  },
  "sets": [
    {
      "name": "Transportation Card Deck",
      "reason": "...",
      "storage_style": "card_stack",       // "loose" | "stack" | "card_stack" | "tight_pack"
      "members": [
        {"component_id": "component_3", "name": "Transportation card", "quantity": 44,
         "shape": "rectangular_prism",      // also cube, square_prism, cylinder, sphere, cone, hexagonal_prism, triangular_prism
         "dimensions_mm": {"length": 67.7, "width": 44.1, "height": 0.4}}
      ]
    }
  ],
  "excluded": [
    {"component_id": "component_1", "name": "Board map of the London transportation network",
     "quantity": 1, "reason": "...", "shape": "rectangular_prism",
     "dimensions_mm": {"length": 236.6, "width": 168.9, "height": 4.0}}
  ],
  "warnings": [], "research_notes": "...", "sources": {}
}
```

The engine treats every shape as its bounding box (`length × width × height`; cylinders use
`length == width == diameter`).

## 4. Conversion rules (`convert_grouping_output.py`)

The JSON is the **single source of truth**.

- `box.dimensions_mm` is used as the box **inside** size. If `basis` contains "estimate", a note says to
  measure the real box.
- **Every entry of `sets` becomes exactly ONE storage box** (`type: "set"`), for both `"boxes"` and
  `"divider"`. Name, members, quantities, shapes, dimensions and `storage_style` are copied verbatim.
  Sets are never added, merged or split.
- **`excluded` items are ignored**: no container, no floor space, no fit check. An excluded board may be
  larger than the box and generation must still succeed.
  - **Exception (height only):** an excluded **board or rulebook** lies *on top* of the storage boxes.
    These are found by name, matching `board|map|rules|rulebook|leaflet|booklet|manual|instructions`.
  - Their `height × quantity` is summed into `settings.headroom_mm`, so every box is that much shorter.
  - Examples: TTR has a 4 mm board + a 12.1 mm rulebook, so the boxes are 44 − 16.1 = 27.9 mm tall;
    King of Tokyo has a 4 mm board + a 4 mm rulebook, so 67 − 8 = 59 mm.
  - A board/rulebook with no valid height gets a `MISSING` note and nothing is reserved; the value is
    never guessed.
  - If board + rulebook ≥ box height, the conversion fails.
- Every problem is collected and reported together (`ConversionError(problems)`), e.g. missing
  dimensions, a bad quantity, an unknown `storage_style`, duplicate set names.
- Flags:
  - `--bed W L H`: required printer build volume. 256³ is used only as an illustration.
  - `--compact`: boxes sized to their contents.
  - `--typed`: the old divider conversion, i.e. typed per-member compartments in one tray per set.
- The output folder must already exist (the script does not create it).
- Output plan:

```json
{
  "game": "...", "organizer": "boxes",
  "box_inner_mm": {"width": 226, "length": 226, "height": 44},
  "printer_bed_mm": {"width": 256, "length": 256, "height": 256},
  "settings": {"headroom_mm": 16.1},
  "compartments": [{"name": "...", "type": "set", "storage_style": "card_stack", "members": [...]}],
  "source": {"organizer": "boxes", "set_count": 6, "physical_fit_verified": false, "edition": "...",
             "box_basis": "...", "excluded_ignored": ["..."],
             "on_top_of_boxes": [{"name": "...", "height_mm": 4.0}]},
  "conversion_notes": ["..."]
}
```

## 5. Engine plan format (general)

`box_inner_mm`, `printer_bed_mm` (height optional), `settings`, `compartments[]`, and optionally
`organizer` (`"trays"` default, or `"boxes"`) and `bottom_layer[]`.

`bottom_layer[]` is only for hand-written plans: non-printed flat items *under* the trays, which raise
every tray. The converter never writes it.

Compartment types (tray mode):

| type | fields |
|---|---|
| `cards`, `tiles` | `piece_mm {width, length, thickness}`, `quantity` |
| `round_tokens` | `piece_mm {diameter, thickness}`, `quantity` |
| `dice` | `piece_mm {edge}`, `quantity` |
| `bits` | `piece_mm {width, length, height}` + `quantity`, or `required_mm` |
| `generic` | `required_mm` (legacy v1 format, still supported) |
| `set` | used by organizer `boxes` |

- Cards/tiles take optional `sleeve_mm` and `stacks`; `round_tokens` takes `stacks`; dice take
  `pockets`; bits take `cells`.
- Shared optional keys: `tray` (several compartments in one tray, separated by dividers) and
  `layer: top|bottom`.

**Organizer `boxes` accepts only `type: "set"`.** It rejects `tray`, `stacks`, `cells`, `pockets` and shaped
types with the error `unsupported_in_boxes_mode`.

## 6. Box design rules (organizer `boxes`)

Each box is an **open-top rectangular box with exactly one continuous rectangular cavity**. It has no
dividers, spare pockets, subdivisions, cylindrical wells, card slots, finger notches or scoops.

Let `c` = `clearance_mm` (1.0), `wall_mm` (2.4) and `floor_mm` (2.0).

| storage_style | cavity |
|---|---|
| `stack` | footprint = largest member footprint + 2c; depth = Σ(quantity × height) + c. Never split; too tall → error. |
| `card_stack` | One deck lying flat: footprint = card length × width + 2c; depth = quantity × card thickness (`height`) + c. Never split. Warns if `height` is not the smallest dimension. |
| `tight_pack` | Only `rectangular_prism`, `square_prism` or `cube` members (others → `invalid_value` error, the grouping step's own rule). Same arrangement as `loose`, in identical layers placed by hand, choosing the layer count with the **smallest footprint** (a compact block). Reported as an assumption: the pieces must be placed, not poured. |
| `loose` | Every piece's bounding box is laid out without overlap (MaxRects strip search) in the fewest identical layers that fit. Pieces too tall to stand are laid on their side. Cavity = arrangement + 2c; depth = layers × tallest piece + c. Reported as an assumption: "pieces poured in at random may need settling". |

- Minimum outer size = cavity + 2·wall; height = depth + floor.
- The single cavity **grows and deepens with the box**, so walls stay `wall_mm` and the floor stays
  `floor_mm`. No solid plastic is ever added (also true for boxes raised under a bridge, §7).

## 7. Layout / packing rules (organizer `boxes`, default `fill_height` + `fill_box`)

Implemented in `organizer/packing.py::pack_organizer`.

1. **Same height:** every box reaches `box height − headroom_mm`, flush under the lid (and under any
   board/rulebook lying on top). With stacking, the top box of each stack does.
2. **Same contents → identical boxes.** Twin detection compares the design's cell geometry.
3. **Similar names → one shared box shape and size** (`share_similar_sizes`, setting
   `similar_names_same_size`, default true).
   - Two names are similar if they're equal after dropping numbers, colours, filler words and bracketed
     text, or if they share their first or last two words.
   - Examples: "Player 1 Bus Set" ~ "Player 4 Bus Set"; "Transportation Card Deck" ~ "Destination
     Ticket Card Deck"; "Shared Supply: Dice" ~ "Shared Supply: Battle Tokens".
   - The shared size is the maximum each member needs (short side, long side, height), with all members
     oriented the same way. `layout.json` names the group in `size_group`.
4. **Fill the whole floor with one block per kind of set** (`block_layout`, used when there are ≤ 5
   kinds):
   - Each kind (a group of equal boxes, or a single box) becomes one rectangular block laid out as a
     full grid.
   - Blocks tile the box floor by straight guillotine cuts; the search is exhaustive over cut trees,
     grid shapes and rotations.
   - Slack is shared in proportion to each side's minimum, so the floor is fully covered except the
     1 mm gaps.
   - Choice order:
     1. different kinds get **clearly different shapes** (≥ 25 % apart in width or length);
     2. avoid boxes longer than 2.5× their width;
     3. most even growth.
5. **Fallback** (more kinds, or tray mode):
   - MaxRects with 5 heuristics × 7 sort orders, plus rectpack candidates.
   - With `fill_box`: proportional `scale_to_fill`, then greedy `expand`, then `equalize`, so twins grow
     together.
   - Candidates are ranked by floor coverage, then evenness.
6. **Column stacking:** only if nothing fits in one layer (boxes mode allows `max_layers: 2`). Two
   columns are merged when it saves the most floor area. Boxes in a column share a footprint, so the
   upper box rests on the lower box's whole rim.
7. **Bridges:** only if columns are not enough either (`_bridges`, `_make_bridge`, `_place_bridge`).
   - One box (the *top*) rests on **two or more** boxes that stand at one common height (the tallest of
     them) and are stretched to tile ≥ 95 % of its footprint. Its solid floor is carried by their rims.
   - The search tries every top × every set of boxes under it (fewest under first, then largest top) for
     up to 8 boxes. The rest are packed as usual (including column merges); the top may grow if the
     boxes under it need more room. Twin groups are never split, and a twin is never the top.
   - **Several small boxes on top of one large open box are never used:** its top is only a rim around
     an open cavity, so they would sit over the hole.
   - Example: King of Tokyo. Monster Kits (200.5 × 197 mm min) leaves only 46.5 / 50 mm strips on the
     248 mm floor, narrower than any other box, so it rests at 38 mm on the other four boxes.
8. **Never:** overlap; exceed the box or printer bed; change measured dimensions to force a fit. If it
   can't fit, `does_not_fit` lists the largest boxes and the options, and says whether the failure is
   **proven** (total minimum area > `max_layers` × floor) or only that **the heuristic packer found no
   arrangement** (not a proof).
9. **`--compact`** (`fill_box`/`fill_height` false): boxes are sized only to their contents. Size groups
   still apply. Boxes under a bridge are still raised and stretched (support requires it).

Current results (`holden_final/`, bed 256³):

| Input | Boxes | Size (mm) |
|---|---|---|
| `exoutput.json` | 3 | 2 × 73.6×123, 1 × 172.4×247; all 57 tall (8 mm board on top) |
| `exoutput (1).json` / `examples/holden_boxes_grouping.json` | 4 | kits 2 × 123×150.8, supplies 2 × 95.2×123; 57 tall |
| `ttrloutput.json` | 6 | buses 4 × 111×81.5, card decks 2 × 57.9×111; 27.9 tall |
| `kotoutput.json` (`king_of_tokyo_5_boxes`) | 5 | Power Card Deck 100.6×145.4, Dice Pool 100.6×100.6, Energy Cubes 100.5×145.4, Status & Special Tokens 145.4×145.5, all 38 tall on the floor; Monster Kits 247×247×21 on top (z 38 → 59) |

## 8. CAD, validation and outputs

- **CAD:** CadQuery rounded box (`corner_radius_mm` 1.0) minus the cavity, cut from the rim down. It
  must be one valid solid. STL is binary (`stl_tolerance_mm` 0.05, angular 0.2 rad).
- **Mesh validation** runs on the triangles written to disk:
  - watertight, 2-manifold, consistent winding, outward normals, one shell, no degenerate faces;
  - bounding box and volume match the CAD;
  - ray-cast minimum wall/floor thickness ≥ `min_feature_mm` (1.2);
  - boxes mode only: `single_open_cavity`, where vertical rays prove one open rectangular cavity with
    nothing inside it.
- **Layout validation** (`validate_layout`):
  - every box is inside the box floor and under the usable height;
  - no 3D collisions;
  - every upper box has ≥ 95 % of its base over boxes whose top is at its height, **and** the solid
    rims/walls it touches surround the middle 50 % × 50 % of its base (`rim_supported`: convex hull of
    the contact points; cavities and token wells count as holes). This rejects a box set over an open
    cavity;
  - everything fits the printer;
  - walls and floors are ≥ settings.
- **Outputs** (one folder per run):
  - `NN_<name>.stl` per box. Each STL is centred at x/y = 0 with its base at z = 0.
  - `layout.json` (schema_version 2):
    - top level: `status`, `method` (e.g. `blocks + bridge + fill_box`), `organizer_mode`,
      `box_inner_mm`, `usable_height_mm`, `constraints` (settings), `stats` (floor_coverage, layers …),
      `notes`, `assumptions`, `warnings`, `validation`, `trays[]`;
    - per tray: `name`, `stl`, `position_mm` (x, y, **z**), `rotation_degrees`, `outer_mm`,
      `footprint_in_box_mm`, `cavity_mm`, `layer`, `column`, `center_mm`, `transform_mm` (4×4),
      `storage_style`, `contents`, `size_group`, `min_outer_mm` (what the contents need),
      `assembled_position_mm`, `exploded_position_mm`, `assembly_order`, `cells[]`, `features[]`,
      `mesh_validation`.
  - **Coordinates:** origin = lower-left corner of the box interior floor, z up. Place a box by rotating
    it `rotation_degrees` counter-clockwise about +z, then translating it to `center_mm`. Stacked boxes
    have `z > 0`; every viewer must use it.
  - `organizer.glb`:
    - built from the same STL triangles;
    - root node `InsideTheBox` (Z-up → Y-up, mm → m);
    - a translucent non-printable `Game box (preview only)` node;
    - one node per box, with `extras` {name, stl, contents, storage_style, layer, assembled/exploded
      translations, assembly_order}.
  - On failure: `report.json` with every error and missing measurement (removed on the next success).
- **Exit codes:** 0 ok · 1 invalid plan / missing measurement / doesn't fit · 2 geometry failed
  validation (files kept, `status: "invalid"`).

## 9. Settings (defaults)

| setting | default |
|---|---|
| `wall_mm` | 2.4 |
| `floor_mm` | 2.0 |
| `clearance_mm` | 1.0 per side (+ once on top) |
| `tray_gap_mm` | 1.0 |
| `box_margin_mm` | 1.0 |
| `divider_mm` (tray mode) | 1.6 |
| `min_feature_mm` | 1.2 |
| `headroom_mm` | 0 (the converter sets it from boards/rulebooks) |
| `fill_height` | true |
| `fill_box` | true |
| `max_layers` | 3 (boxes: 2) |
| `similar_names_same_size` | true |
| `corner_radius_mm` | 1.0 |
| `bulk_packing_efficiency` (tray-mode bits) | 0.6 |
| `explode_gap_mm` | 30 |

These are printer/process defaults, not game measurements.

## 10. How to run

Full chain, one stage at a time (`ANTHROPIC_API_KEY` must be set for the first two):

```bash
python "Final stuff/boardgame_dimensions_ruler_v7.py" "King of Tokyo" photos/ -o dims.json   # CV
python "Final stuff/organize_game_boxes.py" dims.json grouping.json                          # grouping (web search)
mkdir -p out
python convert_grouping_output.py grouping.json --bed 256 256 256 -o out/plan.json
python generate_organizer_stl.py out/plan.json -o out
```

Engine only, on the committed grouping JSONs:

```bash
pip install -r requirements-engine.txt
python convert_grouping_output.py kotoutput.json --bed 256 256 256 -o holden_final/king_of_tokyo_5_boxes/plan.json
python generate_organizer_stl.py holden_final/king_of_tokyo_5_boxes/plan.json -o holden_final/king_of_tokyo_5_boxes
python generate_organizer_stl.py <plan.json> --check          # validate + pack only, no CAD
python generate_organizer_stl.py --from-cv dims.json --box W L H --bed W L H -o out   # tray mode straight from CV output
python -m pytest tests -q    # 121 tests, ~38 s with CadQuery (Node runs tests/js)
```

The whole journey in the browser (no terminal steps after this one):

```bash
pip install -r requirements-engine.txt -r requirements-backend.txt
python -m backend                  # fixtures mode, http://127.0.0.1:8000/  (--port, --mode live, --fixtures FILE)
```

Viewers without the backend (serve the repo root; any free port works):

```bash
python -m http.server 8000
# 3D preview:  http://localhost:8000/preview/?glb=../holden_final/king_of_tokyo_5_boxes/organizer.glb
# Web UI demo: http://localhost:8000/frontend/
```

Other CLI flags: `--allow-partial` (skip broken compartments), `--no-glb`, `--no-thickness-check`.

## 11. Hard rules for anyone changing this project

1. **Millimetres everywhere.**
2. **Never invent physical dimensions.** Missing or incomplete measurements are reported
   (`missing_measurement`, `MISSING` notes), not filled in.
3. **Never add dividers, pockets or subdivisions automatically.** In boxes mode: exactly one
   rectangular cavity per set.
4. **The grouping JSON decides what is stored.** One box per set; no invented, merged or split sets;
   excluded items get no container. Only an excluded board/rulebook's thickness is reserved, as height.
5. **Validate meshes and layouts programmatically.** Don't assume a CAD export is printable. Don't
   weaken validation to make something pass. A box may only rest on solid rims, never over an open
   cavity.
6. **Don't change measured dimensions to force a fit.** Report `does_not_fit` with options instead, and
   say whether infeasibility is proven or only "not found".
7. Don't modify the CV scripts unless absolutely necessary. Avoid new heavy dependencies or paid APIs in
   the engine. Keep provider keys on the backend, never in the browser.
8. Keep backward compatibility: v1 `required_mm` plans, tray mode, the CLI flags, and the `layout.json`
   v1 keys.
9. Write new results to a **new output folder**; don't overwrite existing demo outputs.
10. Don't commit or push unless asked.
11. Report physical fit as **unverified** (upstream `physical_fit_verified = false`).

## 12. Current state

**Works and is tested (121 tests):**
- Holden's grouping JSON → converter → one plain open box per set → packed, validated STL + GLB +
  `layout.json`, for all four committed grouping outputs (3, 4, 6 and 5 sets), default and `--compact`.
- One-layer, column and bridge layouts; same-size twins and similar-name groups; full-floor filling;
  mesh and layout validation, including rim support; clear `does_not_fit` reports.
- All four storage styles the grouping script writes (`loose`, `stack`, `card_stack`, `tight_pack`) convert
  and generate; a test fails if the grouping script ever adds a style the converter doesn't know.
- **The user journey, in fixtures mode** (`backend/`, 18 integration tests, also run in a browser): upload a
  game name and 2–6 photos → stage-by-stage loading screen → (if a photo's scale was estimated) one width
  measurement that rescales every photo measurement → 3D preview and STL downloads of files generated for
  that job. Upload checks, missing-measurement, conversion, `does_not_fit` and stage-failure errors reach the
  user with the backend's message. Fixture games: King of Tokyo, Ticket to Ride: London, Hypothetical Battle
  Game (more with `--fixtures FILE`).
- The shared 3D viewer (web UI studio and `preview/`) renders every output, including stacked layers;
  checked in a browser on Ticket to Ride: London and King of Tokyo, desktop and phone width.

**Live mode, run once against the real API (2026-11-14):**
- Ticket to Ride: London, 4 real ruler photos, through the web UI: 6 sets → 6 validated boxes in one layer,
  3D preview and STL downloads in **156 s** (CV measurement 95 s, grouping + web research 55 s, conversion
  < 1 s, CAD 5 s). 12 API calls on `claude-sonnet-5-5`, 130 k input / 15 k output tokens (≈ $0.41) + 6 web
  searches, no retries. Every photo was scaled from the ruler's ticks, so no measurement was asked.
- It needs `brotli>=1.2.0` (an old Brotli makes every API call fail as a "Connection error") and a `scipy`
  built for the installed NumPy; see `backend/README.md` → Live-mode environment problems.
- The CV and grouping scripts are not tested in the repo; live mode's wiring is (stand-in scripts). The
  grouping script reads the CV JSON directly (`load_game` maps `index` → `id`, `edition_assumed`).

## 13. What is missing to make it complete

*Done in PR #3:* the backend job server and its tests; the scale-measurement question; the web UI viewer's
`position_mm.z` and its refresh between jobs; the backend's stage text, error messages and measurement prompt
in the UI; `tight_pack`; `BACKEND_INTEGRATION.md` §5; `requirements-backend.txt` / `requirements-live.txt`;
finished jobs' files downloadable after a restart; one real live run (§12).

**P0: before printing anything from live mode**
1. **Measurement accuracy.** All 6 components of the live run were low confidence, and two CV runs on the same
   photos disagree by up to 12 % (Destination Ticket card 69.0 vs 77.0 mm long, scoring marker 14.7 vs
   17.9 mm). The backend only asks for a measurement when a photo's scale was estimated, so nothing was asked.
   Ask for one reference measurement also when the pieces that size the boxes (cards, tokens) are low
   confidence, and measure a card by hand before printing.
2. **Assumed thicknesses decide the box height without saying so.** CV fell back to 0.4 mm per card and
   4 mm for the board and the rulebook (`measurement.height_assumed: true`); the board + rulebook thickness
   becomes the headroom, so it sets every box's height (an earlier run measured the rulebook at 12.1 mm).
   Name assumed heights in the job warnings, ask for the rulebook/board thickness, and show the job's
   `warnings` in the web UI (it shows none today).

**P1: correctness and consistency**
2. **Pick one canonical version of each script** and archive the rest: CV exists as root
   `…ruler.py`/`…relative.py`, `boardgame/…ruler.py`/`…relative.py`/`…cv.py`/`…ruler_v7.py` and
   `Final stuff/…ruler_v7.py`; grouping as `organize_game.py`, `_compatible.py`, `_shapes.py` and
   `organize_game_boxes.py`. Rename `Final stuff/` to a path without a space (e.g. `pipeline/`) and update
   `ITB_CV_SCRIPT` / `ITB_GROUPING_SCRIPT` defaults in `backend/config.py`.
3. **The measurement question only covers the image scale.** Missing dimensions stop the job (the UI can
   ask for one width only), and low-confidence pieces are only listed as warnings. A UI for a few answers
   (e.g. a missing height) would let those jobs continue instead of asking for new photos.
4. **The backend is a local single-user server:** job status lives in memory (a restart forgets it; finished
   jobs' files stay downloadable), jobs run one at a time, old job folders are never deleted, and there is no
   authentication or rate limiting. Don't expose it to the internet as is.
5. **Web UI demo data:** point the static demo at a boxes-mode result (e.g. `holden_final/…`), not the
   legacy tray demo, and stop hard-coding "SUMMIT SUPPLIES" (the tray count now comes from the job).
6. **Model IDs:** CV uses `claude-sonnet-5-5` (hard-coded), grouping defaults to `claude-sonnet-4-6` unless
   `ANTHROPIC_MODEL` is set (the live run set it to `claude-sonnet-5-5`). On Sonnet 5.5, LangChain cannot force
   the structured-output tool call; it worked on every call, but a reply without it fails the CV stage.
7. `convert_grouping_output.py -o dir/plan.json` should create `dir/`.

**P2: quality and confidence**
8. **Contract tests across stages:** the four committed grouping outputs are tested, and a test checks
   that the converter knows every storage style the grouping script declares. Still missing: real
   `organize_game_boxes.py` outputs covering every shape, and schema tests for CV JSON → grouping input.
9. **Physical verification:** print one organizer and test-fit it. King of Tokyo's 250 × 250 × 67 and
   TTR's 226 × 226 × 44 box sizes are estimated from the outside; TTR's 12.1 mm rule leaflet looks too
   thick. Use the real printer's bed (`ITB_PRINTER_BED`) instead of the illustrative 256³.
10. **Repo hygiene:** ~18 MB of test photos (`boardgame/photos/`) and two unrelated stock images
    (`boardgame/a-beautiful-sunset-…jpg`, `boardgame/nuclear-test-explosion-…jpg`) are committed;
    local scratch folders (`king_of_tokyo_demo/`, `ticket_to_ride_*`, `ttrloutput_test.json`) are
    untracked clutter. Consider Git LFS or removing them, and add scratch patterns to `.gitignore`.
11. **Packing limits:** the block layout covers ≤ 5 kinds of set (more fall back to MaxRects, which can
    leave gaps); bridges are searched for ≤ 8 boxes and at most one bridge per layout; `--compact` doesn't
    force different kinds to look different.
12. **Usability trade-offs to surface in the UI:** under a bridge, the lower boxes are reached by lifting
    the top box off, and short contents (King of Tokyo's Energy Cubes need 17 mm) sit in a 38 mm box;
    `loose` sizing assumes bounding boxes side by side (real pouring may need more room); there is no
    finger access in boxes mode (small decks sit at the bottom of a full-height box); name similarity is
    heuristic and can over-group generic names (`similar_names_same_size: false` turns it off). The web UI
    shows none of the job's `warnings` yet.
