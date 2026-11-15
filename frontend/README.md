# GameFit frontend demo

This is a static frontend for the organizer flow: a board-game name plus ruler photos, rulebook-assisted component classification, a measurement check when image scale is uncertain, and generated STL downloads with an interactive Three.js preview.

## Run it

From the repository root, serve the project (opening `index.html` directly prevents the STL and layout JSON from loading in most browsers):

```powershell
python -m http.server 8000
```

Then open [http://localhost:8000/frontend/](http://localhost:8000/frontend/).

The demo loads the existing files from `generated_organizer/`. Submitting four photos opens the illustrated loading scene for about four seconds, then sends the orbiting photo cards off the plate one at a time and opens the generated example layout.

## Backend connection

With `ORGANIZER_API_URL` empty, the page stays in demo mode. Set it to your backend's `POST /api/organizer-jobs` URL to submit the game name and every selected image as multipart form data. A backend may return `202` with `{ "status": "processing", "job_id": "..." }`; the page then keeps the loading scene visible and polls `GET {ORGANIZER_API_URL}/{job_id}` every 1.5 seconds until it receives `complete` or `needs_measurement`. Keep the Claude key in the backend's environment, never in the browser.
