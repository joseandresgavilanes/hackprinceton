"""Backend integration tests: the frontend's job API, end to end, without paid API calls.

Fixtures mode uses committed JSON for the CV and grouping stages; live mode is exercised with stand-in
stage scripts. The converter and the CAD engine always run for real.
"""
import json
import struct
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, needs_cad
from backend import Config, create_app

KEY = "fake-test-key-DO-NOT-LEAK-0123456789"   # a dummy value; must never appear in responses or logs
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64          # the backend sniffs the type; fixtures mode never decodes it
REAL_PHOTOS = sorted((ROOT / "examples" / "photos").glob("*.jpg"))[:4]
KOT_SETS = ["Power Card Deck", "Dice Pool", "Energy Cubes", "Status & Special Tokens", "Monster Kits (All Players)"]


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**kw):
        client = TestClient(create_app(Config(jobs_dir=tmp_path / "jobs", **kw)))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def photos(n=4, data=PNG):
    return [("reference_photos[]", (f"p{k}.png", data, "image/png")) for k in range(n)]


def start(client, game="King of Tokyo", files=None):
    return client.post("/api/organizer-jobs", data={"game_name": game}, files=photos() if files is None else files)


def wait(client, job_id, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/organizer-jobs/{job_id}")
        if r.json().get("status") != "processing":
            return r
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout} s")


def path_of(url):
    return url.replace("http://testserver", "")


# ---------------------------------------------------------------------------
# A small test game: a card deck and some cubes, with matching CV and grouping JSON
# ---------------------------------------------------------------------------
def cv_json(scale_source="ticks", card_dims=None):
    return {"game": "Test Game", "units": "mm",
            "photos": [{"file": f"IMG_{k}.jpg", "status": "used", "scale_source": scale_source} for k in range(4)],
            "components": [
                {"index": 1, "name": "Test card", "quantity": 30, "shape": "rectangular_prism",
                 "dimensions_mm": card_dims if card_dims is not None else {"length": 88.0, "width": 63.0, "height": 0.3},
                 "confidence": "high", "measurement_method": "photos"},
                {"index": 2, "name": "Wooden cube", "quantity": 20, "shape": "cube",
                 "dimensions_mm": {"length": 10.0, "width": 10.0, "height": 10.0}, "confidence": "low",
                 "estimated_error_pct": 12.0, "measurement_method": "photos"}]}


def grouping_json(box=(150.0, 150.0, 40.0), card=(88.0, 63.0, 0.3), cards=30, second_deck=False):
    deck = lambda name, cid: {"name": name, "reason": "test", "storage_style": "card_stack", "members": [
        {"component_id": cid, "name": "Test card", "quantity": cards, "shape": "rectangular_prism",
         "dimensions_mm": dict(zip(("length", "width", "height"), card))}]}
    sets = [deck("Cards", "component_1"),
            deck("More cards", "component_1") if second_deck else
            {"name": "Cubes", "reason": "test", "storage_style": "loose", "members": [
                {"component_id": "component_2", "name": "Wooden cube", "quantity": 20, "shape": "cube",
                 "dimensions_mm": {"length": 10.0, "width": 10.0, "height": 10.0}}]}]
    return {"game": "Test Game", "organizer": "boxes", "set_count": 2, "physical_fit_verified": False,
            "box": {"dimensions_mm": dict(zip(("length", "width", "height"), box)), "basis": "user_supplied",
                    "explanation": "test"},
            "sets": sets, "excluded": [], "warnings": ["Test warning from the grouping step."]}


def test_fixtures(tmp_path, cv=None, grouping=None):
    root = tmp_path / "fixtures"
    root.mkdir(exist_ok=True)
    fx = {"title": "Test Game", "grouping": "grouping.json"}
    (root / "grouping.json").write_text(json.dumps(grouping or grouping_json()))
    if cv is not None:
        (root / "cv.json").write_text(json.dumps(cv))
        fx["dimensions"] = "cv.json"
    return {"fixtures": {"test game": fx}, "fixtures_root": root}


test_fixtures.__test__ = False   # a helper, not a test


# ---------------------------------------------------------------------------
# Service basics and input validation (no CAD needed)
# ---------------------------------------------------------------------------
def test_frontend_is_served_and_pointed_at_the_api(make_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    client = make_client()
    assert client.get("/", follow_redirects=False).headers["location"] == "/frontend/"
    page = client.get("/frontend/")
    assert page.status_code == 200 and 'src="./config.js"' in page.text
    cfg = client.get("/frontend/config.js")
    assert '"apiUrl": "/api/organizer-jobs"' in cfg.text and cfg.headers["cache-control"] == "no-store"
    assert '"photos": {"min": 2, "max": 6}' in cfg.text
    health = client.get("/api/health").json()
    assert health["mode"] == "fixtures" and "King of Tokyo" in health["fixtures"]
    for r in (page, cfg, client.get("/api/health"), client.get("/frontend/app.js")):
        assert KEY not in r.text


@pytest.mark.parametrize("case, kwargs, status, code", [
    ("no game name", {"data": {"game_name": "  "}, "files": photos()}, 400, "missing_game_name"),
    ("one photo", {"data": {"game_name": "King of Tokyo"}, "files": photos(1)}, 400, "wrong_photo_count"),
    ("seven photos", {"data": {"game_name": "King of Tokyo"}, "files": photos(7)}, 400, "wrong_photo_count"),
    ("not an image", {"data": {"game_name": "King of Tokyo"}, "files": photos(data=b"%PDF-1.7 not a photo")},
     400, "invalid_photo"),
    ("empty file", {"data": {"game_name": "King of Tokyo"}, "files": photos(data=b"")}, 400, "invalid_photo"),
    ("too large", {"data": {"game_name": "King of Tokyo"}, "files": photos(data=PNG + b"\0" * 4096)},
     413, "photo_too_large"),
    ("no fixture", {"data": {"game_name": "Catan"}, "files": photos()}, 422, "unknown_game"),
    ("json body", {"json": {"game_name": "King of Tokyo"}}, 400, "bad_request"),
])
def test_uploads_are_validated(make_client, tmp_path, case, kwargs, status, code):
    client = make_client(max_photo_bytes=2048)
    r = client.post("/api/organizer-jobs", **kwargs)
    assert r.status_code == status, (case, r.json())
    body = r.json()
    assert body["status"] == "error" and body["code"] == code and body["message"]
    assert not any((tmp_path / "jobs").iterdir()), "a rejected upload must not create a job"


def test_live_mode_without_a_key_refuses_jobs(make_client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = make_client(mode="live")
    assert client.get("/api/health").json() == {"status": "ok", "mode": "live", "live_ready": False,
                                                "jobs_dir": client.app.state.config.jobs_dir.as_posix()}
    r = start(client, "Any Game")
    assert r.status_code == 503 and r.json()["code"] == "not_configured"


def test_unknown_jobs_and_files_are_404(make_client):
    client = make_client()
    assert client.get("/api/organizer-jobs/0123456789abcdef").status_code == 404
    r = client.post("/api/organizer-jobs/0123456789abcdef/measurements", json={"object": "x", "width_mm": 1})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    for url in ("/files/0123456789abcdef/01_box.stl", "/files/..%2F..%2Fetc/passwd", "/files/zz/organizer.glb"):
        assert client.get(url).status_code == 404


def test_missing_measurements_stop_the_job(make_client, tmp_path):
    cv = cv_json(card_dims={"length": 88.0, "width": None})
    client = make_client(**test_fixtures(tmp_path, cv=cv))
    r = wait(client, start(client, "Test Game").json()["job_id"])
    body = r.json()
    assert r.status_code == 422 and body["code"] == "missing_measurements"
    assert "Test card" in body["message"] and "Retake the photos" in body["message"]
    assert body["details"] == ["Test card: no complete length × width × height"]


# ---------------------------------------------------------------------------
# Full jobs (CadQuery)
# ---------------------------------------------------------------------------
@needs_cad
def test_king_of_tokyo_upload_to_printable_files(make_client, tmp_path):
    client = make_client()
    files = [("reference_photos[]", (p.name, p.read_bytes(), "image/jpeg")) for p in REAL_PHOTOS]
    r = start(client, "  king of TOKYO ", files)
    assert r.status_code == 202
    first = r.json()
    assert first["status"] == "processing" and first["stage_label"] and len(first["stages"]) == 4
    job_id = first["job_id"]
    job_dir = tmp_path / "jobs" / job_id
    for k, p in enumerate(REAL_PHOTOS, 1):   # stored under server-chosen names, byte for byte
        assert (job_dir / "photos" / f"photo_{k}.jpg").read_bytes() == p.read_bytes()

    r = wait(client, job_id)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "complete" and body["mode"] == "fixtures"
    layout = body["layout"]
    assert layout["status"] == "ok" and [t["name"] for t in layout["trays"]] == KOT_SETS
    assert [f["name"] for f in body["files"]] == KOT_SETS               # files[i] belongs to layout.trays[i]
    top = layout["trays"][-1]
    assert top["layer"] == 1 and top["position_mm"]["z"] == pytest.approx(38.0)
    for f, t in zip(body["files"], layout["trays"]):
        assert f["stl_url"] == f"http://testserver/files/{job_id}/{t['stl']}"
        stl = client.get(path_of(f["stl_url"]))
        assert stl.status_code == 200 and stl.headers["content-type"] == "model/stl"
        n = struct.unpack("<I", stl.content[80:84])[0]                 # binary STL: header, count, 50 B/facet
        assert len(stl.content) == 84 + 50 * n == f["size_bytes"] and n > 12
        assert stl.content == (job_dir / "output" / t["stl"]).read_bytes()   # this job's files, not a demo
    glb = client.get(path_of(body["glb_url"]))
    assert glb.status_code == 200 and glb.content[:4] == b"glTF"
    assert client.get(path_of(body["layout_url"])).json() == layout
    assert any("Fixtures mode" in n for n in body["notes"])
    assert json.loads((job_dir / "job.json").read_text())["status"] == "complete"


@needs_cad
def test_scale_question_and_answer(make_client, tmp_path):
    client = make_client(**test_fixtures(tmp_path, cv=cv_json(scale_source="assumed_length")))
    job_id = start(client, "Test Game").json()["job_id"]
    r = wait(client, job_id)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "needs_measurement"
    q = body["question"]
    assert (q["object"], q["dimension"], q["suggested_value"]) == ("Test card", "width_mm", 63.0)
    assert "assumed_length" in q["reason"]

    url = f"/api/organizer-jobs/{job_id}/measurements"
    for payload, status, code in [({"object": "Wooden cube", "width_mm": 10}, 400, "wrong_object"),
                                  ({"object": "Test card", "width_mm": 300}, 400, "implausible_measurement"),
                                  ({"object": "Test card", "width_mm": -2}, 400, "invalid_width"),
                                  ({"object": "Test card", "width_mm": True}, 400, "invalid_width"),
                                  ({"object": "", "width_mm": 63}, 400, "missing_object")]:
        bad = client.post(url, json=payload)
        assert (bad.status_code, bad.json()["code"]) == (status, code), payload
    assert client.post(url, content=b"not json", headers={"content-type": "application/json"}).status_code == 400

    ok = client.post(url, json={"object": " test CARD ", "width_mm": 66.15})   # 5 % larger than the photos said
    assert ok.status_code == 202 and ok.json()["status"] == "processing"
    assert client.post(url, json={"object": "Test card", "width_mm": 66.15}).status_code == 409

    r = wait(client, job_id)
    body = r.json()
    assert body["status"] == "complete", body
    job_dir = tmp_path / "jobs" / job_id
    corrected = json.loads((job_dir / "dimensions_corrected.json").read_text())
    assert corrected["scale_correction"]["ratio"] == pytest.approx(1.05)
    grouping = json.loads((job_dir / "grouping.json").read_text())
    card = grouping["sets"][0]["members"][0]["dimensions_mm"]
    assert (card["length"], card["width"]) == pytest.approx((92.4, 66.15))   # same correction downstream
    cards = next(t for t in body["layout"]["trays"] if t["name"] == "Cards")
    assert cards["cells"][0]["contents"]["members"][0]["dimensions_mm"]["width"] == pytest.approx(66.15)
    assert any("scaled by 1.0500" in w for w in body["warnings"])
    assert "Low-confidence measurement: Wooden cube (±12%)" in body["warnings"]
    assert "Grouping: Test warning from the grouping step." in body["warnings"]


@needs_cad
@pytest.mark.parametrize("n", [2, 6])
def test_two_to_six_photos_make_a_job(make_client, tmp_path, n):
    client = make_client(**test_fixtures(tmp_path))
    r = start(client, "Test Game", files=photos(n))
    assert r.status_code == 202, r.json()
    job_id = r.json()["job_id"]
    assert len(list((tmp_path / "jobs" / job_id / "photos").iterdir())) == n
    assert wait(client, job_id).json()["status"] == "complete"


@needs_cad
def test_finished_files_stay_downloadable_after_a_restart(make_client, tmp_path):
    client = make_client(**test_fixtures(tmp_path))
    job_id = start(client, "Test Game").json()["job_id"]
    body = wait(client, job_id).json()
    assert body["status"] == "complete"
    urls = [path_of(f["stl_url"]) for f in body["files"]] + [path_of(body["glb_url"]), path_of(body["layout_url"])]
    restarted = make_client(**test_fixtures(tmp_path))          # new process state, same jobs folder
    assert restarted.get(f"/api/organizer-jobs/{job_id}").status_code == 404   # status is in memory only
    for url in urls:
        r = restarted.get(url)
        assert r.status_code == 200 and r.content == client.get(url).content, url
    assert restarted.get(f"/files/{job_id}/plan.json").status_code == 404     # still only the listed files
    assert restarted.get(f"/files/{job_id}/..%2Fjob.json").status_code == 404


@needs_cad
def test_an_organizer_that_cannot_fit_is_reported(make_client, tmp_path):
    grouping = grouping_json(box=(100.0, 100.0, 20.0), second_deck=True)   # two 12 mm decks: no floor, no stack
    client = make_client(**test_fixtures(tmp_path, grouping=grouping))
    r = wait(client, start(client, "Test Game").json()["job_id"])
    body = r.json()
    assert r.status_code == 422 and body["code"] == "does_not_fit"
    assert "cannot fit in the 100 × 100 × 20 mm box" in body["message"]
    assert (tmp_path / "jobs" / body["job_id"] / "output" / "report.json").exists()
    assert client.get(f"/files/{body['job_id']}/layout.json").status_code == 404


# ---------------------------------------------------------------------------
# Live mode, with stand-in stage scripts (no paid calls)
# ---------------------------------------------------------------------------
def write_script(path, body):
    path.write_text("import json, os, sys\n" + body)
    return [sys.executable, str(path)]


@needs_cad
def test_live_mode_runs_the_stage_scripts_and_keeps_the_key_secret(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    seen, cv_src, grouping_src = tmp_path / "seen.json", tmp_path / "cv_src.json", tmp_path / "grouping_src.json"
    cv_src.write_text(json.dumps(cv_json()))
    grouping_src.write_text(json.dumps(grouping_json()))
    cv_cmd = write_script(tmp_path / "fake_cv.py", f"""
out = sys.argv[sys.argv.index('-o') + 1]
json.dump({{'argv': sys.argv[1:], 'has_key': os.environ.get('ANTHROPIC_API_KEY') == {KEY!r}}}, open({str(seen)!r}, 'w'))
print('calling the model with key ' + os.environ['ANTHROPIC_API_KEY'])
open(out, 'w').write(open({str(cv_src)!r}).read())
""")
    grouping_cmd = write_script(tmp_path / "fake_grouping.py", f"""
inp, out = sys.argv[1], sys.argv[2]
assert json.load(open(inp))['components'][0]['name'] == 'Test card'
open(out, 'w').write(open({str(grouping_src)!r}).read())
""")
    client = make_client(mode="live", cv_command=cv_cmd, grouping_command=grouping_cmd)
    job_id = start(client, "Any Game At All").json()["job_id"]
    r = wait(client, job_id)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "complete", body
    assert [t["name"] for t in body["layout"]["trays"]] == ["Cards", "Cubes"]
    call = json.loads(seen.read_text())
    job_dir = tmp_path / "jobs" / job_id
    assert call["has_key"] and call["argv"][0] == "Any Game At All" and call["argv"][-1] == "--no-prompt"
    assert call["argv"][1:5] == [str(job_dir / "photos" / f"photo_{k}.png") for k in range(1, 5)]
    log = (job_dir / "measurement.log").read_text()
    assert "[redacted]" in log and KEY not in log
    assert KEY not in r.text and KEY not in (job_dir / "job.json").read_text()


def test_live_stage_failure_is_reported(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    cv_cmd = write_script(tmp_path / "broken_cv.py",
                          f"print('starting', file=sys.stderr)\nprint('RateLimitError: slow down ({KEY})', file=sys.stderr)\n"
                          "sys.exit(3)\n")
    client = make_client(mode="live", cv_command=cv_cmd)
    r = wait(client, start(client, "Any Game").json()["job_id"])
    body = r.json()
    assert r.status_code == 502 and body["code"] == "measurement_failed"
    assert body["message"] == "The measurement step failed: RateLimitError: slow down ([redacted])"
    assert KEY not in r.text


def test_a_fixture_file_adds_games(make_client, tmp_path, monkeypatch):
    (tmp_path / "g.json").write_text(json.dumps(grouping_json()))
    (tmp_path / "extra.json").write_text(json.dumps({"test game": {"title": "Test Game", "grouping": "g.json"}}))
    monkeypatch.setenv("ITB_FIXTURES", str(tmp_path / "extra.json"))
    cfg = Config.from_env(jobs_dir=tmp_path / "jobs")
    assert cfg.fixtures["test game"]["grouping"] == str(tmp_path / "g.json") and "king of tokyo" in cfg.fixtures
    (tmp_path / "bad.json").write_text(json.dumps({"x": {"grouping": "missing.json"}}))
    with pytest.raises(ValueError, match="not found"):
        Config.from_env(fixtures_file=str(tmp_path / "bad.json"))
