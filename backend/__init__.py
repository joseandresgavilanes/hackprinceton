"""Inside the Box backend: the job API the frontend calls.

game name + 4 ruler photos → CV measurement → grouping → converter → CAD engine → STL/GLB downloads.
Start it with ``python -m backend`` (see backend/README.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # convert_grouping_output.py and organizer/ live at the repository root
    sys.path.insert(0, str(ROOT))

from .app import create_app  # noqa: E402
from .config import Config  # noqa: E402

__all__ = ["create_app", "Config", "ROOT"]
