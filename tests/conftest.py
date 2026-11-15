import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAS_CADQUERY = importlib.util.find_spec("cadquery") is not None
needs_cad = pytest.mark.skipif(not HAS_CADQUERY, reason="CadQuery not installed")


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture
def legacy_plan():
    return copy.deepcopy(load("organizer_plan_example.json"))


@pytest.fixture
def typed_plan():
    return copy.deepcopy(load("examples/typed_demo_plan.json"))


@pytest.fixture
def stacked_plan():
    return copy.deepcopy(load("examples/stacked_demo_plan.json"))


def minimal_plan(*compartments, box=(200, 200, 60), bed=(220, 220, 250), **settings):
    return {"game": "test", "box_inner_mm": dict(zip(("width", "length", "height"), box)),
            "printer_bed_mm": dict(zip(("width", "length", "height"), bed)),
            "settings": {"headroom_mm": 0, **settings}, "compartments": list(compartments)}


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    """Run the full CAD pipeline once per example plan (shared by the geometry tests)."""
    if not HAS_CADQUERY:
        pytest.skip("CadQuery not installed")
    from organizer import generate
    out = {}
    for key, name in (("legacy", "organizer_plan_example.json"), ("typed", "examples/typed_demo_plan.json"),
                      ("stacked", "examples/stacked_demo_plan.json")):
        dest = tmp_path_factory.mktemp(key)
        out[key] = (dest, generate(load(name), dest))
    return out
