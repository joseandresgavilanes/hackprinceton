"""HTTP API for the frontend (see frontend/BACKEND_CONTRACT.md) plus the frontend itself.

POST /api/organizer-jobs                      multipart: game_name + 4 × reference_photos[] → 202 processing
GET  /api/organizer-jobs/{id}                 202 processing | 200 needs_measurement | 200 complete | 4xx/5xx error
POST /api/organizer-jobs/{id}/measurements    JSON {object, width_mm} → 202 processing
GET  /files/{id}/{name}                       the job's STL files, organizer.glb, layout.json
GET  /api/health                              mode and available fixtures (never any secret)
GET  /frontend/…                              the web UI; /frontend/config.js points it at this API
"""
from __future__ import annotations

import json
import math
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ROOT, measurement
from .config import Config
from .jobs import STAGE_LABELS, STAGES, JobStore
from .pipeline import find_fixture, fixture_titles, normalize

JOB_ID = re.compile(r"^[0-9a-f]{16}$")
MEDIA_TYPES = {".stl": "model/stl", ".glb": "model/gltf-binary", ".json": "application/json"}
MAX_GAME_NAME = 120
MAX_WIDTH_MM = 1000.0


def image_kind(head: bytes) -> str | None:
    """File type from the first bytes (the browser's Content-Type is not trusted)."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "heic"
    return None


def error(http_status: int, message: str, code: str, details=None, job_id=None) -> JSONResponse:
    body = {"status": "error", "code": code, "message": message}
    if job_id:
        body["job_id"] = job_id
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=http_status)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(config)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.shutdown()

    app = FastAPI(title="Inside the Box backend", lifespan=lifespan)
    app.state.config, app.state.store = config, store
    app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])

    def file_url(request: Request, job_id: str, name: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/files/{job_id}/{quote(name)}"

    def job_response(request: Request, job) -> JSONResponse:
        with store.lock:
            base = {"job_id": job.id, "game_name": job.game_name, "mode": job.mode}
            if job.status == "error":
                e = job.error
                return error(e["http_status"], e["message"], e["code"], e.get("details"), job.id)
            if job.status == "needs_measurement":
                q = {k: job.question[k] for k in ("object", "dimension", "suggested_value", "reason")}
                return JSONResponse({"status": "needs_measurement", **base, "question": q})
            if job.status == "complete":
                files = [{"name": f["name"], "stl_url": file_url(request, job.id, f["stl"]),
                          "size_bytes": (job.output_dir / f["stl"]).stat().st_size} for f in job.files]
                return JSONResponse({"status": "complete", **base, "layout": job.layout, "files": files,
                                     "glb_url": file_url(request, job.id, "organizer.glb"),
                                     "layout_url": file_url(request, job.id, "layout.json"),
                                     "warnings": job.warnings, "notes": job.notes})
            stages = [{"id": s, "label": STAGE_LABELS[s],
                       "state": "done" if s in job.stages_done else "active" if s == job.stage else "pending"}
                      for s in STAGES]
            return JSONResponse({"status": "processing", **base, "stage": job.stage,
                                 "stage_label": STAGE_LABELS.get(job.stage, job.stage), "stages": stages},
                                status_code=202)

    @app.get("/api/health")
    def health():
        body = {"status": "ok", "mode": config.mode, "jobs_dir": str(config.jobs_dir)}
        if config.mode == "fixtures":
            body["fixtures"] = fixture_titles(config)
        else:
            body["live_ready"] = bool(Config.api_key())
        return body

    @app.post("/api/organizer-jobs")
    async def create_job(request: Request):
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            return error(400, f"Send multipart/form-data with game_name and {config.photo_count} reference_photos[] "
                              "image files.", "bad_request")
        try:
            form = await request.form(max_files=config.photo_count + 4, max_fields=20)
        except Exception as e:
            return error(400, f"Could not read the upload: {e}", "bad_request")
        game = form.get("game_name")
        if not isinstance(game, str) or not game.strip():
            return error(400, "Enter the name of the board game.", "missing_game_name")
        game = " ".join(game.split())
        if len(game) > MAX_GAME_NAME:
            return error(400, f"The game name is longer than {MAX_GAME_NAME} characters.", "invalid_game_name")
        uploads = form.getlist("reference_photos[]") + form.getlist("reference_photos")
        if any(not hasattr(u, "read") for u in uploads):
            return error(400, "reference_photos[] must be image files.", "invalid_photo")
        if len(uploads) != config.photo_count:
            return error(400, f"Upload exactly {config.photo_count} photos of the components next to a ruler "
                              f"(got {len(uploads)}).", "wrong_photo_count")
        photos = []
        for k, up in enumerate(uploads, 1):
            label = f"Photo {k} ({up.filename or 'unnamed'})"
            data = await up.read(config.max_photo_bytes + 1)
            if not data:
                return error(400, f"{label} is empty.", "invalid_photo")
            if len(data) > config.max_photo_bytes:
                return error(413, f"{label} is larger than {config.max_photo_bytes // (1024 * 1024)} MB.",
                             "photo_too_large")
            kind = image_kind(data[:16])
            if kind is None:
                return error(400, f"{label} is not a JPEG, PNG, HEIC or WebP image.", "invalid_photo")
            photos.append((data, kind))
        if config.mode == "fixtures" and find_fixture(config, game) is None:
            return error(422, f"The local backend runs without paid API calls (fixtures mode) and only knows: "
                              f"{', '.join(fixture_titles(config))}. To organize '{game}', start it in live mode "
                              f"(ITB_PIPELINE_MODE=live with ANTHROPIC_API_KEY in the server's environment).",
                         "unknown_game")
        if config.mode == "live" and not Config.api_key():
            return error(503, "The backend is in live mode but ANTHROPIC_API_KEY is not set on the server.",
                         "not_configured")
        job = store.create(game, photos)
        store.submit(job)
        return job_response(request, job)

    @app.get("/api/organizer-jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = store.get(job_id)
        if job is None:
            return error(404, f"No organizer job '{job_id}'.", "not_found")
        return job_response(request, job)

    @app.post("/api/organizer-jobs/{job_id}/measurements")
    async def answer(job_id: str, request: Request):
        job = store.get(job_id)
        if job is None:
            return error(404, f"No organizer job '{job_id}'.", "not_found")
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return error(400, 'Send JSON like {"object": "...", "width_mm": 63}.', "bad_request", job_id=job.id)
        obj, width = body.get("object"), body.get("width_mm")
        if not isinstance(obj, str) or not obj.strip():
            return error(400, "Name the object you measured.", "missing_object", job_id=job.id)
        if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or width <= 0:
            return error(400, "width_mm must be a positive number of millimetres.", "invalid_width", job_id=job.id)
        if width > MAX_WIDTH_MM:
            return error(400, f"{width:g} mm is not a plausible component width.", "invalid_width", job_id=job.id)
        with store.lock:
            if job.status != "needs_measurement":
                return error(409, f"This job is not waiting for a measurement (status: {job.status}).",
                             "not_waiting", job_id=job.id)
            q = job.question
            if normalize(obj) != normalize(q["object"]):
                return error(400, f"Please measure '{q['object']}', the piece the scan uses as its reference "
                                  f"(you entered '{obj.strip()}').", "wrong_object", job_id=job.id)
            try:
                ratio = measurement.answer_ratio(q, float(width))
            except ValueError as e:
                return error(400, str(e), "implausible_measurement", job_id=job.id)
            cv, ids = measurement.rescale_cv(job.cv, ratio, q, float(width))
            (job.dir / "dimensions_corrected.json").write_text(json.dumps(cv, indent=2, ensure_ascii=False),
                                                               encoding="utf-8")
            job.warnings.append(f"Every photo measurement was scaled by {ratio:.4f} from your measurement of "
                                f"'{q['object']}' ({width:g} mm; the photos gave {q['measured_mm']:.1f} mm).")
            store.update(job, answer={"object": q["object"], "width_mm": float(width), "ratio": ratio}, cv=cv,
                         cv_done=True, scale={"ratio": ratio, "ids": ids}, status="processing", stage="queued")
        store.submit(job)
        return job_response(request, job)

    def finished_files(job_id: str) -> set:
        """Files a finished job may serve: from memory, or from its job.json after a server restart."""
        job = store.get(job_id)
        if job is not None:
            with store.lock:
                return {f["stl"] for f in job.files} | {"organizer.glb", "layout.json"} \
                    if job.status == "complete" else set()
        try:
            record = json.loads((config.jobs_dir / job_id / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if record.get("status") != "complete":
            return set()
        return {f["stl"] for f in record.get("files", [])} | {"organizer.glb", "layout.json"}

    @app.get("/files/{job_id}/{name}")
    def job_file(job_id: str, name: str):
        allowed = finished_files(job_id) if JOB_ID.match(job_id) else set()
        path = config.jobs_dir / job_id / "output" / name if name in allowed else None
        if path is None or not path.is_file():
            return error(404, "No such file for this job.", "not_found")
        return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
                            filename=name)

    @app.get("/frontend/config.js")
    def frontend_config():
        js = "window.INSIDE_THE_BOX_CONFIG = " + json.dumps({"apiUrl": "/api/organizer-jobs", "mode": config.mode}) + ";\n"
        return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/")
    def index():
        return RedirectResponse("/frontend/")

    app.mount("/frontend", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app
