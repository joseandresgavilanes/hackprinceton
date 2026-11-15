"""Organizer jobs: state, on-disk folder, and the background worker that runs the pipeline."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("inside_the_box.jobs")

STAGES = ("measuring", "grouping", "converting", "generating")
STAGE_LABELS = {
    "queued": "Waiting for the organizer worker…",
    "measuring": "Measuring components from your photos…",
    "waiting_for_measurement": "Waiting for one measurement from you…",
    "grouping": "Grouping components into storage sets…",
    "converting": "Planning one storage box per set…",
    "generating": "Building and checking the printable STL files…",
    "done": "Your organizer is ready!",
}


class StageError(Exception):
    """A pipeline failure with a message meant for the user."""

    def __init__(self, message: str, *, code: str = "pipeline_error", http_status: int = 500,
                 details: list | None = None):
        super().__init__(message)
        self.message, self.code, self.http_status, self.details = message, code, http_status, details or []

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details, "http_status": self.http_status}


@dataclass
class Job:
    id: str
    game_name: str
    mode: str
    dir: Path
    photos: list
    status: str = "processing"          # processing | needs_measurement | complete | error
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    question: dict | None = None
    answer: dict | None = None
    error: dict | None = None
    layout: dict | None = None
    files: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    stages_done: list = field(default_factory=list)
    cv: dict | None = None               # CV measurement after checks (corrected once the user answered)
    cv_done: bool = False
    scale: dict | None = None            # {"ratio", "ids"} once a scale correction was applied

    @property
    def output_dir(self) -> Path:
        return self.dir / "output"

    def record(self) -> dict:
        return {"id": self.id, "game_name": self.game_name, "mode": self.mode, "status": self.status,
                "stage": self.stage, "stages_done": self.stages_done, "created_at": self.created_at,
                "updated_at": self.updated_at, "photos": [p.name for p in self.photos], "question": self.question,
                "answer": self.answer, "error": self.error, "files": self.files, "warnings": self.warnings,
                "notes": self.notes}

    def save(self) -> None:
        (self.dir / "job.json").write_text(json.dumps(self.record(), indent=2, ensure_ascii=False), encoding="utf-8")


class JobStore:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._pool = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="itb-job")

    def create(self, game_name: str, photos: list[tuple[bytes, str]]) -> Job:
        job_id = secrets.token_hex(8)
        folder = self.config.jobs_dir / job_id
        (folder / "photos").mkdir(parents=True)
        paths = []
        for k, (data, ext) in enumerate(photos, 1):   # never reuse the client's file name
            path = folder / "photos" / f"photo_{k}.{ext}"
            path.write_bytes(data)
            paths.append(path)
        job = Job(id=job_id, game_name=game_name, mode=self.config.mode, dir=folder, photos=paths)
        with self.lock:
            self._jobs[job_id] = job
            job.save()
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self._jobs.get(job_id)

    def update(self, job: Job, **changes) -> None:
        with self.lock:
            if "stage" in changes and job.stage in STAGES and changes["stage"] != job.stage \
                    and job.stage not in job.stages_done and changes.get("status") != "error":
                job.stages_done.append(job.stage)
            for k, v in changes.items():
                setattr(job, k, v)
            job.updated_at = time.time()
            job.save()

    def submit(self, job: Job) -> None:
        self._pool.submit(self._run, job)

    def _run(self, job: Job) -> None:
        from .pipeline import run
        try:
            run(job, self, self.config)
        except StageError as e:
            self.update(job, status="error", error=e.to_dict())
        except Exception as e:  # last resort: never leave a job "processing" forever
            log.exception("job %s failed", job.id)
            self.update(job, status="error", error=StageError(
                f"Unexpected error while {STAGE_LABELS.get(job.stage, job.stage).rstrip('…').lower()}: "
                f"{type(e).__name__}: {e}", code="internal_error").to_dict())

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
