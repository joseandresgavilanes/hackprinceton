"""Backend settings, read from the environment (and an optional ``.env`` file at the repository root).

| variable | default | meaning |
|---|---|---|
| ``ITB_PIPELINE_MODE`` | ``fixtures`` | ``fixtures``: CV and grouping come from committed JSON files (no paid API calls); the converter and CAD engine always run for real. ``live``: run the CV and grouping scripts, which call Anthropic. |
| ``ANTHROPIC_API_KEY`` | — | live mode only. Stays on the server: passed to the stage scripts, never returned to the browser. |
| ``ITB_JOBS_DIR`` | ``organizer_jobs/`` | one folder per job: photos, intermediate JSON, STL/GLB/layout. |
| ``ITB_PRINTER_BED`` | ``256,256,256`` | printer build volume in mm (illustrative default). |
| ``ITB_COMPACT`` | ``0`` | ``1``: boxes sized to their contents instead of filling the game box. |
| ``ITB_CV_SCRIPT`` / ``ITB_GROUPING_SCRIPT`` | ``pipeline/…`` | live-mode stage scripts. |
| ``ITB_STAGE_TIMEOUT_S`` | ``1800`` | live mode: give up on a stage script after this many seconds. |
| ``ITB_FIT_RETRIES`` | ``2`` | live mode: regroup after a ``does_not_fit`` result, at most this many times. |
| ``ITB_CORS_ORIGINS`` | ``http://localhost:8000,http://127.0.0.1:8000`` | extra origins allowed to call the API (the backend also serves the frontend itself). |
| ``ITB_FIXTURES`` | — | JSON file with extra fixture games: ``{"<name>": {"title", "grouping", "dimensions"?}}``, paths relative to it. |
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import ROOT

MODES = ("fixtures", "live")

# Committed pipeline outputs, keyed by normalised game name. "dimensions" is a CV measurement JSON
# (optional), "grouping" is Holden's grouping JSON. Paths are relative to the repository root.
DEFAULT_FIXTURES = {
    "king of tokyo": {"title": "King of Tokyo", "grouping": "examples/king_of_tokyo_grouping.json"},
    "ticket to ride london": {"title": "Ticket to Ride: London",
                              "dimensions": "examples/ttr_london_dimensions.json",
                              "grouping": "examples/ttr_london_grouping.json"},
    "hypothetical battle game": {"title": "Hypothetical Battle Game",
                                 "grouping": "examples/holden_boxes_grouping.json"},
}


def load_dotenv(path: Path) -> None:
    """Minimal ``KEY=VALUE`` reader. Never overrides variables already set in the environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_fixture_file(path) -> dict:
    """Extra fixture games: {"<game name>": {"title"?, "grouping": file, "dimensions"?: file}}, paths relative
    to the JSON file."""
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, fx in data.items():
        if not isinstance(fx, dict) or not fx.get("grouping"):
            raise ValueError(f"{path}: fixture '{name}' needs a 'grouping' file")
        fx = {"title": fx.get("title", name), **{k: str(path.parent / fx[k]) for k in ("grouping", "dimensions")
                                                 if fx.get(k)}}
        for k in ("grouping", "dimensions"):
            if k in fx and not Path(fx[k]).is_file():
                raise ValueError(f"{path}: fixture '{name}': {k} file {fx[k]} not found")
        out[name] = fx
    return out


def _script(env: str, default: Path) -> list[str]:
    return [sys.executable, os.environ.get(env) or str(default)]


@dataclass
class Config:
    mode: str = "fixtures"
    jobs_dir: Path = ROOT / "organizer_jobs"
    bed: tuple = (256.0, 256.0, 256.0)
    compact: bool = False
    fixtures: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_FIXTURES.items()})
    fixtures_root: Path = ROOT
    cv_command: list = field(default_factory=lambda: _script(
        "ITB_CV_SCRIPT", ROOT / "pipeline" / "boardgame_dimensions_ruler_v7.py"))
    grouping_command: list = field(default_factory=lambda: _script(
        "ITB_GROUPING_SCRIPT", ROOT / "pipeline" / "organize_game_boxes.py"))
    stage_timeout_s: float = 1800.0
    fit_retries: int = 2
    min_photos: int = 2                  # reference photos per job (the web UI reads these limits)
    max_photos: int = 6
    max_photo_bytes: int = 25 * 1024 * 1024
    cors_origins: list = field(default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"])
    workers: int = 1

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"pipeline mode must be one of {', '.join(MODES)}, got {self.mode!r}")
        if not 0 <= self.fit_retries <= 3:
            raise ValueError("fit_retries must be between 0 and 3")
        self.jobs_dir = Path(self.jobs_dir)

    @staticmethod
    def api_key() -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    def fixture_path(self, rel: str) -> Path:
        return self.fixtures_root / rel

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_dotenv(ROOT / ".env")
        kw = {"mode": os.environ.get("ITB_PIPELINE_MODE", "fixtures").strip().lower()}
        if os.environ.get("ITB_JOBS_DIR"):
            kw["jobs_dir"] = Path(os.environ["ITB_JOBS_DIR"])
        if os.environ.get("ITB_PRINTER_BED"):
            kw["bed"] = tuple(float(v) for v in os.environ["ITB_PRINTER_BED"].replace("x", ",").split(","))
        if os.environ.get("ITB_COMPACT"):
            kw["compact"] = os.environ["ITB_COMPACT"].strip().lower() in ("1", "true", "yes")
        if os.environ.get("ITB_STAGE_TIMEOUT_S"):
            kw["stage_timeout_s"] = float(os.environ["ITB_STAGE_TIMEOUT_S"])
        if os.environ.get("ITB_FIT_RETRIES"):
            kw["fit_retries"] = int(os.environ["ITB_FIT_RETRIES"])
        if os.environ.get("ITB_CORS_ORIGINS"):
            kw["cors_origins"] = [o.strip() for o in os.environ["ITB_CORS_ORIGINS"].split(",") if o.strip()]
        extra = overrides.pop("fixtures_file", None) or os.environ.get("ITB_FIXTURES")
        if extra:
            kw["fixtures"] = {**{k: dict(v) for k, v in DEFAULT_FIXTURES.items()}, **load_fixture_file(extra)}
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)
