"""The organizer pipeline for one job: measure → group → convert → generate.

fixtures mode: the measurement and grouping stages read committed JSON (no paid API calls).
live mode: they run the CV and grouping scripts as subprocesses with the server's ANTHROPIC_API_KEY.
The converter and the CAD engine always run for real, in the job's own folder.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from convert_grouping_output import ConversionError, convert
from organizer import GenerationError, PlanError, generate, write_failure_report

from . import measurement
from .jobs import Job, JobStore, StageError


def normalize(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(name).lower().replace("&", " and "))
    return " ".join(s.split())


def find_fixture(config, game_name: str) -> dict | None:
    key = normalize(game_name)
    for k, fx in config.fixtures.items():
        if key in (normalize(k), normalize(fx.get("title", k))):
            return fx
    return None


def fixture_titles(config) -> list[str]:
    return [fx.get("title", k) for k, fx in config.fixtures.items()]


def _load_json(path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StageError(f"The {what} step wrote no output ({path.name}).", code="missing_output")
    except json.JSONDecodeError as e:
        raise StageError(f"The {what} output is not valid JSON: {e}", code="invalid_output")
    if not isinstance(data, dict):
        raise StageError(f"The {what} output must be a JSON object.", code="invalid_output")
    return data


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _scrub(text: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    return text.replace(key, "[redacted]") if key else text


def _run_script(cmd: list, job: Job, config, step: str) -> None:
    """Run a live-mode stage script; its output is logged to <job>/<step>.log (secrets removed)."""
    try:
        proc = subprocess.run(cmd, cwd=job.dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=config.stage_timeout_s, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        raise StageError(f"The {step} step took longer than {config.stage_timeout_s / 60:.0f} min and was stopped.",
                         code=f"{step}_timeout", http_status=504)
    except OSError as e:
        raise StageError(f"Could not start the {step} step: {e}", code=f"{step}_unavailable")
    out = _scrub((proc.stdout or "") + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    (job.dir / f"{step}.log").write_text(out, encoding="utf-8")
    if proc.returncode != 0:
        lines = [ln for ln in _scrub(proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
        last = lines[-1] if lines else f"exit code {proc.returncode}"
        raise StageError(f"The {step} step failed: {last}", code=f"{step}_failed", http_status=502,
                         details=lines[-15:])


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def measure(job: Job, store: JobStore, config) -> None:
    """CV measurement JSON → checked (and, after the user's answer, corrected) inventory on ``job.cv``."""
    if job.cv_done:
        return
    store.update(job, stage="measuring")
    out = job.dir / "dimensions.json"
    if config.mode == "fixtures":
        fx = find_fixture(config, job.game_name)
        if fx is None:
            raise StageError(f"No fixture for '{job.game_name}'.", code="unknown_game", http_status=422)
        if not fx.get("dimensions"):
            job.notes.append("Fixtures mode: no CV measurement is stored for this game, so the measurements in "
                             "its grouping JSON are used. The photos were checked and saved but not measured.")
            store.update(job, cv=None, cv_done=True)
            return
        cv = _load_json(config.fixture_path(fx["dimensions"]), "measurement fixture")
        _write_json(out, cv)
        job.notes.append(f"Fixtures mode: measurements from {fx['dimensions']} (no CV or AI call).")
    else:
        _run_script(config.cv_command + [job.game_name, *map(str, job.photos), "-o", str(out), "--no-prompt"],
                    job, config, "measurement")
        cv = _load_json(out, "measurement")

    missing = measurement.missing_components(cv)
    if missing:
        raise StageError("The photos did not give complete measurements for: " + "; ".join(missing[:5])
                         + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
                         + ". Retake the photos with each piece flat and fully visible next to the whole ruler, "
                           "plus at least one side view for heights.",
                         code="missing_measurements", http_status=422, details=missing)
    cv, thickness, thickness_warnings = measurement.default_thickness(cv)
    job.warnings.extend(thickness_warnings)
    job.warnings.extend(measurement.low_confidence(cv))
    question = measurement.scale_question(cv)
    if question:
        store.update(job, cv=cv, thickness=thickness, status="needs_measurement", stage="waiting_for_measurement",
                     question=question)
        return
    store.update(job, cv=cv, thickness=thickness, cv_done=True)


def group(job: Job, store: JobStore, config) -> dict:
    store.update(job, stage="grouping")
    if config.mode == "fixtures":
        fx = find_fixture(config, job.game_name)
        grouping = _load_json(config.fixture_path(fx["grouping"]), "grouping fixture")
        if job.thickness:
            grouping = measurement.set_heights(grouping, job.thickness)
        if job.scale:
            grouping = measurement.rescale_grouping(grouping, job.scale["ratio"], job.scale["ids"])
        job.notes.append(f"Fixtures mode: grouping from {fx['grouping']} (no AI call).")
    else:
        inp, out = job.dir / "dimensions_checked.json", job.dir / "grouping.json"
        _write_json(inp, job.cv)
        _run_script(config.grouping_command + [str(inp), str(out)], job, config, "grouping")
        grouping = _load_json(out, "grouping")
    _write_json(job.dir / "grouping.json", grouping)
    job.warnings.extend(f"Grouping: {w}" for w in grouping.get("warnings") or [] if isinstance(w, str))
    return grouping


def to_plan(job: Job, store: JobStore, config, grouping: dict) -> dict:
    store.update(job, stage="converting")
    try:
        plan, notes = convert(grouping, list(config.bed), compact=config.compact)
    except ConversionError as e:
        raise StageError("The grouping cannot be turned into storage boxes: " + "; ".join(e.problems[:3])
                         + (f" (+{len(e.problems) - 3} more)" if len(e.problems) > 3 else ""),
                         code="invalid_grouping", http_status=422, details=e.problems)
    _write_json(job.output_dir / "plan.json", plan)
    job.notes.extend(notes)
    return plan


def build(job: Job, store: JobStore, config, plan: dict) -> dict:
    store.update(job, stage="generating")
    try:
        return generate(plan, job.output_dir)
    except PlanError as e:
        write_failure_report(job.output_dir, e)
        errors = e.issues.errors
        raise StageError(str(e), code=errors[0].code if errors else "invalid_plan", http_status=422,
                         details=[i.message for i in errors + e.issues.missing_measurements])
    except GenerationError as e:
        lv = e.layout.get("validation", {}).get("layout", {})
        raise StageError("The generated organizer failed its geometry checks, so no files are offered. "
                         + str(e), code="invalid_geometry", details=lv.get("problems", []))


def run(job: Job, store: JobStore, config) -> None:
    measure(job, store, config)
    if job.status == "needs_measurement":
        return
    grouping = group(job, store, config)
    plan = to_plan(job, store, config, grouping)
    layout = build(job, store, config, plan)
    job.warnings.extend(w["message"] for w in layout.get("warnings", []) if isinstance(w, dict))
    store.update(job, layout=layout, files=[{"name": t["name"], "stl": t["stl"]} for t in layout["trays"]],
                 status="complete", stage="done")
