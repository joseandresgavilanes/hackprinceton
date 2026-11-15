# GameFit frontend demo

This is a static frontend for the organizer flow: a board-game name plus ruler photos, rulebook-assisted component classification, a measurement check when image scale is uncertain, and generated STL downloads with an interactive Three.js preview. The preview is the shared viewer in `viewer/` (`organizer-viewer.js`, `contents.js`): a live compact view in the results card, and **Open studio** for a full-screen configurator (box details and downloads, isolate/hide, dimension lines, camera presets, explode and assembly, approximate contents, filament colours). Selecting a box and its file row stay in sync.

## Run it

From the repository root, serve the project (opening `index.html` directly prevents the STL and layout JSON from loading in most browsers):

```powershell
python -m http.server 8000
```

Then open [http://localhost:8000/frontend/](http://localhost:8000/frontend/).

The demo loads the existing files from `generated_organizer/`. Photos the browser cannot preview (for example iPhone HEIC files in Chrome) show as a labelled card instead of a broken image; they are still uploaded unchanged. Submitting 2–6 photos opens the illustrated loading scene for about four seconds, then sends the orbiting photo cards off the plate one at a time and opens the generated example layout.

## Backend connection

Run the backend from the repository root instead of a static server:

```bash
python -m backend
```

Then open [http://127.0.0.1:8000/](http://127.0.0.1:8000/). The backend serves this page with a `config.js` that points `ORGANIZER_API_URL` at its own `POST /api/organizer-jobs`; every submission is then a real job (the `Bypass` and `loading` demo names only work without a backend). The page polls `GET /api/organizer-jobs/{job_id}` every 1.5 seconds, shows the backend's current stage on the loading screen, fills in the piece to measure when it receives `needs_measurement`, and shows the backend's error message when a job fails. Keep the Claude key in the backend's environment, never in the browser. See `backend/README.md`.

Served by `python -m http.server` (no backend), `config.js` leaves the API URL empty and the page stays in demo mode, loading the files from `generated_organizer/`.
