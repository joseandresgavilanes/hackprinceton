# GameFit frontend ↔ backend integration

This document defines the data boundary between the GameFit frontend and the organizer backend. The frontend does not call Claude, rulebook services, CV, packing, or STL generation directly. The backend owns those steps and returns only the organizer job result.

## 1. Frontend configuration

In [`app.js`](./app.js), set `ORGANIZER_API_URL` to the create-job endpoint. Do **not** include a trailing slash.

```js
const ORGANIZER_API_URL = 'http://localhost:3000/api/organizer-jobs';
```

For a separate frontend/backend origin, enable CORS for the frontend origin (for example, `http://localhost:8000`). The STL URLs returned by the backend must also be readable by the browser with CORS enabled.

Keep the Claude key on the backend in `.env`:

```env
ANTHROPIC_API_KEY=your_key_here
```

## 2. Create an organizer job

### Request sent by the frontend

`POST {ORGANIZER_API_URL}`

Content type: `multipart/form-data` (the browser sets its boundary; do not manually set the request `Content-Type` on the frontend).

| Field | Type | Required | Rule |
| --- | --- | --- | --- |
| `game_name` | string | Yes | Non-empty board-game name. `Bypass` is a local demo-only special case and is never sent. |
| `reference_photos[]` | file[] | Yes | Exactly 4 image files. Each should show components and a ruler. |

The backend flow is expected to use `game_name` to find rulebook/component information and use that information to classify the submitted images. It can ask for one verified measurement when image scale or classification is uncertain.

### Success response: organizer is complete

Return HTTP `200` with this shape when STL files are ready. This is the direct normal path to the downloads screen.

```json
{
  "status": "complete",
  "job_id": "job_123",
  "layout": {
    "units": "mm",
    "box_inner_mm": { "width": 210, "length": 210, "height": 50 },
    "trays": [
      {
        "name": "Playing cards",
        "stl": "01_playing_cards.stl",
        "position_mm": { "x": 2.25, "y": 2.25, "z": 0 },
        "rotation_degrees": 0,
        "outer_mm": { "width": 72.8, "length": 98.8, "height": 29 },
        "footprint_in_box_mm": { "width": 72.8, "length": 98.8 }
      }
    ]
  },
  "files": [
    {
      "name": "Playing cards",
      "stl_url": "http://localhost:3000/files/job_123/01_playing_cards.stl"
    }
  ]
}
```

`files` must be in the same order as `layout.trays`. The frontend pairs the first file with the first tray, and so on. `name` should match the associated `layout.trays[].name` for human-readable download labels.

### Success response: one measurement is needed

Return HTTP `200` with this shape when the backend needs a verified dimension before it can create the STL files:

```json
{
  "status": "needs_measurement",
  "job_id": "job_123",
  "question": {
    "object": "Standard playing card",
    "dimension": "width_mm",
    "suggested_value": 63
  }
}
```

| Value | Type | Meaning |
| --- | --- | --- |
| `status` | literal string | Must be `needs_measurement`. |
| `job_id` | string | Stable identifier used in the next request. |
| `question.object` | string | Name of the item the user should measure. |
| `question.dimension` | string | The requested dimension; use `width_mm` for the current UI. |
| `question.suggested_value` | number | Optional proposed value in millimetres. |

The current frontend shows its generic measurement card after receiving this status. It sends the user response to the endpoint below.

### Still processing or an error

| Backend situation | HTTP/status body | Current frontend behavior |
| --- | --- | --- |
| Work not ready yet | `202` + `{ "status": "processing", "job_id": "job_123" }` | Keeps the illustrated loading screen open and polls the job endpoint every 1.5 seconds. |
| Invalid game name/photos | `400` + `{ "status": "error", "message": "..." }` | Shows the generic failure alert. |
| Rulebook/CV/generation failure | `422` or `500` + `{ "status": "error", "message": "..." }` | Shows the generic failure alert. |

## 2a. Long-running jobs and the illustrated loading screen

The loading screen is now a first-class job state. As soon as the user submits four photos, the frontend opens the illustrated plate scene and makes the submitted photos orbit on the plate. Do not return `needs_measurement` until the backend has actually completed its initial analysis and determined that a measurement is necessary.

For a job that takes longer than the original upload request, return immediately with HTTP `202`:

```json
{ "status": "processing", "job_id": "job_123" }
```

Then implement this endpoint:

```text
GET /api/organizer-jobs/job_123
```

While the job is in progress, return `202` and the same `processing` response. The frontend polls this endpoint every 1.5 seconds while leaving the loading scene visible. On the first non-processing response, return exactly one of:

- `200` + `status: "complete"`, with `files` and `layout`: the photos finish their current orbit, leave the plate one at a time, and the frontend opens the results screen.
- `200` + `status: "needs_measurement"`: the loading scene closes and the frontend opens the reference-measurement screen.
- `4xx`/`5xx` + `status: "error"`: the loading scene closes and the frontend displays its failure message.

This keeps the browser in the loading state while backend work is actually occurring; it must not navigate to the measurement screen simply because a job was accepted.

## 3. Submit the confirmed measurement

The frontend calls:

`POST {ORGANIZER_API_URL}/{job_id}/measurements`

Content type: `application/json`.

```json
{
  "object": "Standard playing card",
  "width_mm": 63
}
```

Normal validation in the frontend requires a non-empty `object` and a positive numeric `width_mm`. The backend must validate these again and must not treat the browser validation as authoritative.

Return HTTP `200` only when the files are ready, using the same `status: "complete"`, `layout`, and `files` response shape from section 2. A measurement submission may also return `202` + `status: "processing"`; in that case the frontend keeps the same loading scene active and polls `GET /api/organizer-jobs/:job_id` until completion.

## 4. STL and layout requirements

The 3D viewer needs both the STL file URL and the associated layout tray. All dimensions are millimetres.

| Layout field | Required | Viewer use |
| --- | --- | --- |
| `box_inner_mm.width`, `box_inner_mm.length` | Yes | Centers the packed organizer in the 3D view. |
| `trays[].name` | Yes | Download label and file pairing. |
| `trays[].position_mm.x`, `trays[].position_mm.y` | Yes | Bottom-left position of the tray footprint in the box. |
| `trays[].rotation_degrees` | Yes | `0` or `90`; rotates the tray around its vertical axis. |
| `trays[].outer_mm.width`, `length`, `height` | Yes | Places the STL on the floor rather than through it. |
| `trays[].footprint_in_box_mm.width`, `length` | Yes | Finds the footprint center after the possible rotation. |
| `files[].stl_url` | Yes | Browser-accessible URL for the corresponding STL mesh and download link. |

STL geometry must use z-up coordinates, with its base at `z = 0`. The frontend converts it to Three.js y-up coordinates, raises it by half of `outer_mm.height`, applies `rotation_degrees`, and places it from the footprint center. Do not pre-translate the mesh to the box location inside the STL itself.

## 5. Intermediate AI/CV planner result

The supplied `exoutput.json` is a valid **intermediate backend result**. It is not yet a response the frontend can render because it describes components and grouping decisions, not generated STL files or tray positions.

### Producer: research/CV + organizer-selection stage

This stage receives the game name, four photos, and any confirmed fallback measurement. It returns the following values to the **packing/STL stage**:

| JSON value | Required | Consumer use |
| --- | --- | --- |
| `game`, `edition` | Yes / optional | Traceability and job display. |
| `organizer` | Yes | Requested organizer approach, for example `boxes`. |
| `box.dimensions_mm.length`, `width`, `height` | Yes | Interior box dimensions in millimetres. |
| `sets[]` | Yes | One proposed storage group per future tray/compartment. |
| `sets[].name` | Yes | Future tray name. |
| `sets[].storage_style` | Yes | How the packer should calculate the stored envelope (`loose`, `stack`, etc.). |
| `sets[].members[]` | Yes | Components grouped into the tray. |
| `members[].quantity`, `shape`, `dimensions_mm` | Yes | Inputs for computing the physical stored envelope. |
| `excluded[]` | Yes when applicable | Components intentionally omitted from trays, such as a board stored flat. |
| `physical_fit_verified` | Yes | `false` means do not claim a verified physical fit. Preserve a warning or request confirmation. |
| `warnings[]` | Recommended | Surface in job logs or final UI; never silently discard fit/research uncertainty. |

`dimensions_mm` uses `{ "length", "width", "height" }`, all in millimetres. For cylinders, the producer defines `length == width == diameter`, as the example notes.

### Consumer: packing/STL stage

The current CAD generator does **not** accept `sets[].members` directly. It accepts a reviewed organizer plan with `box_inner_mm` and `compartments[].required_mm`. The packing stage must make this conversion:

```text
exoutput.json
  box.dimensions_mm
  sets[].members + storage_style
       ↓ calculate a safe group envelope per set
organizer_plan.json
  box_inner_mm
  compartments[].required_mm
       ↓ generate_organizer_stl.py
layout.json + individual STL files
       ↓
frontend status: "complete" response
```

Required mapping:

```json
{
  "game": "<exoutput.game>",
  "box_inner_mm": {
    "width": "<exoutput.box.dimensions_mm.width>",
    "length": "<exoutput.box.dimensions_mm.length>",
    "height": "<exoutput.box.dimensions_mm.height>"
  },
  "compartments": [
    {
      "name": "<sets[i].name>",
      "required_mm": {
        "width": "<derived group width>",
        "length": "<derived group length>",
        "height": "<derived group height>"
      }
    }
  ]
}
```

Envelope rules are owned by the packing stage, not the frontend:

- `loose`: derive a conservative rectangular envelope for all items in the group; do not sum incompatible dimensions blindly.
- `stack`: use the component footprint plus the total stacked height (`quantity × item height`) when that stack is physically valid.
- `excluded`: do not emit a tray. Account for its consumed box layer/height before claiming remaining space.
- Add the generator's wall, floor, clearance, tray-gap, and box-margin settings only once—inside the generator/planner contract.

The packer must reject or return a clear failure when the derived trays cannot fit. It must not scale component dimensions down to force a fit.

### Final boundary back to the frontend

After STL generation, the backend converts the generated `layout.json` and STL paths into the `status: "complete"` response from section 2. The frontend should never receive raw Claude reasoning, long `research_notes`, or source URLs as part of the required rendering contract; store those server-side or expose them separately as optional job metadata.

## 6. Frontend-only demo path

`Bypass` is intentionally only for local visual testing:

1. Game name: `Bypass`
2. Object: `Bypass`
3. Width: `Bypass`

This never calls the backend and displays only the repository's sample STLs. Normal submissions require four photos and show STL files only from a backend `complete` response.

For loading-screen-only visual testing, enter `Loading` as the game name. This is another local-only test mode: it opens the plate animation immediately, uses any photos currently selected as the orbiting cards, waits for the demo duration, then opens the sample results screen. It never calls the backend.
