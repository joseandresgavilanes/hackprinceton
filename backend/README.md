# Inside the Box backend

One local server for the whole user journey: the web UI uploads a game name and 4 ruler photos, the backend
runs **CV measurement → grouping → converter → CAD engine** as a background job, and the UI shows the 3D
preview and the printable STL downloads. It implements `frontend/BACKEND_CONTRACT.md`.

## Run

```bash
pip install -r requirements-engine.txt -r requirements-backend.txt
python -m backend            # http://127.0.0.1:8000/  (use --port to pick another)
```

Open the printed URL: it serves the web UI (`/frontend/`) already connected to the API. In the Claude desktop
app, the `inside-the-box` entry in `.claude/launch.json` starts it on port 8790.

## Modes

| mode | measurement + grouping | cost | games |
|---|---|---|---|
| `fixtures` (default) | committed JSON: `kotoutput.json`, `ttrloutput.json` + `boardgame/ticket_to_ride_london_dimensions.json`, `examples/holden_boxes_grouping.json` | free | King of Tokyo, Ticket to Ride: London, Hypothetical Battle Game (+ any from `--fixtures FILE`) |
| `live` (`--mode live` or `ITB_PIPELINE_MODE=live`) | `Final stuff/boardgame_dimensions_ruler_v7.py`, then `Final stuff/organize_game_boxes.py` | Anthropic API calls (+ web search) | any |

In both modes the converter (`convert_grouping_output.py`) and the CAD engine (`organizer/`) run for real in
the job's own folder: every job's STL, GLB and layout are generated for that job, never taken from a demo.
In fixtures mode the photos are validated and stored but not measured.

Live mode needs `ANTHROPIC_API_KEY` in the server's environment (or in `.env` at the repository root, which
is git-ignored) and `pip install -r requirements-live.txt`. The key is passed to the stage scripts only; it is
never sent to the browser, and it is removed from stage logs and error messages.

## API

| request | response |
|---|---|
| `POST /api/organizer-jobs` (multipart: `game_name`, 4 × `reference_photos[]`) | `202 {"status": "processing", "job_id", "stage", "stage_label", "stages": [...]}` |
| `GET /api/organizer-jobs/{id}` | `202` processing · `200 needs_measurement` with `question {object, dimension, suggested_value, reason}` · `200 complete` with `layout`, `files [{name, stl_url, size_bytes}]` (same order as `layout.trays`), `glb_url`, `layout_url`, `warnings`, `notes` · `4xx/5xx {"status": "error", "code", "message", "details"}` |
| `POST /api/organizer-jobs/{id}/measurements` (JSON `{"object", "width_mm"}`) | `202` processing, or `400` (wrong object, not a positive number, more than 2× the photo measurement) / `409` (not waiting) |
| `GET /files/{id}/{name}` | that job's `NN_<name>.stl`, `organizer.glb` or `layout.json` (only once complete) |
| `GET /api/health` | mode, fixture games or whether live mode has its key (never the key) |

Uploads are checked before a job starts: a game name (≤ 120 characters), exactly 4 files, each a real
JPEG/PNG/HEIC/WebP (by its bytes, not its name), ≤ 25 MB. Photos are saved as `photo_1…4.<ext>`.

### When the user is asked for a measurement

After the measurement stage (`backend/measurement.py`):

- A component without a usable length × width × height or quantity stops the job (`422 missing_measurements`)
  with a list of what is missing: retake the photos. Nothing is guessed.
- If a photo's scale was not read from the ruler's tick marks (`scale_source` `labels` or `assumed_length`),
  the job asks for the width of one reference piece (a card if there is one). The answer rescales every
  photo measurement by `answer / measured` (fixtures mode applies the same correction to the grouping JSON).
- Low-confidence measurements are listed in `warnings`; they don't stop the job.

### Errors the user can see

| code | HTTP | when |
|---|---|---|
| `missing_game_name`, `wrong_photo_count`, `invalid_photo`, `photo_too_large`, `bad_request` | 400 / 413 | bad upload |
| `unknown_game` | 422 | fixtures mode has no data for that game |
| `not_configured` | 503 | live mode without `ANTHROPIC_API_KEY` |
| `missing_measurements` | 422 | CV could not measure some components |
| `invalid_grouping` | 422 | the grouping JSON cannot be converted (converter problems in `details`) |
| `does_not_fit`, `too_tall`, `too_large`, … | 422 | the engine's own plan errors (`report.json` is kept) |
| `invalid_geometry` | 500 | generated meshes failed validation; nothing is offered for download |
| `measurement_failed` / `grouping_failed` / `…_timeout` | 502 / 504 | a live-mode stage script failed or timed out (last error line in the message, full log in the job folder) |

## Jobs on disk

`organizer_jobs/<job_id>/` (git-ignored, `ITB_JOBS_DIR` to move it): `job.json` (status, stage, question,
answer, error), `photos/`, `dimensions.json` (+ `dimensions_corrected.json`), `grouping.json`,
`measurement.log` / `grouping.log` (live mode), and `output/` with `plan.json`, the STL files,
`organizer.glb` and `layout.json` (or `report.json` on failure). Jobs run one at a time; their status is kept
in memory, so restarting the server forgets them (the folders stay).

## Configuration

Environment variables (see `backend/config.py`): `ITB_PIPELINE_MODE`, `ANTHROPIC_API_KEY`, `ITB_JOBS_DIR`,
`ITB_PRINTER_BED` (default `256,256,256`, illustrative), `ITB_COMPACT`, `ITB_FIXTURES` (extra fixture games:
`{"<name>": {"title", "grouping", "dimensions"?}}`, paths relative to that file), `ITB_CV_SCRIPT`,
`ITB_GROUPING_SCRIPT`, `ITB_STAGE_TIMEOUT_S`, `ITB_CORS_ORIGINS` (for a UI served from another origin).

## Tests

`python -m pytest tests/test_backend.py -q` drives the API in-process: upload validation, the King of Tokyo
journey down to byte-identical STL downloads, the measurement question and answer, `does_not_fit` and
missing-measurement errors, live mode with stand-in stage scripts (argument passing, key kept secret,
failures reported). No paid API calls are made.
