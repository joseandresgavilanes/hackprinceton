"""Runs the viewer's JavaScript tests (tests/js) with Node, so `pytest tests` covers them too."""
import shutil
import subprocess

import pytest

from conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_viewer_contents_stand_ins():
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    proc = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
