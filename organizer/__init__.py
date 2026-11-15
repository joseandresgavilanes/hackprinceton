"""Inside the Box — parametric board-game organizer engine.

Pipeline: component measurements → grouping → spatial optimisation →
parametric CAD (CadQuery) → validated STL + GLB → interactive preview.
All lengths are millimetres. CadQuery is imported lazily, so validation,
grouping and packing work without it.
"""
from .pipeline import ENGINE_VERSION as __version__
from .pipeline import GenerationError, generate, plan_layout, write_failure_report
from .schema import PlanError, Settings, parse_plan

__all__ = ["generate", "plan_layout", "parse_plan", "PlanError", "GenerationError",
           "Settings", "write_failure_report", "__version__"]
