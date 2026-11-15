#!/usr/bin/env python3
"""Inside the Box: turn a board-game compartment plan into printable organizer trays.

    pip install cadquery rectpack  # rectpack is optional; built-in MaxRects is always used too
    python generate_organizer_stl.py examples/organizer_plan_example.json -o generated_organizer

Outputs (all millimetres): one STL per tray, organizer.glb (whole organizer for
the 3-D preview, built from the same triangles) and layout.json (positions,
rotations, layers, exploded-view offsets, validation results).

Coordinates in layout.json: x/y origin is the lower-left of the *inside* of the
board-game box; z = 0 is the box floor. See README_STL_ENGINE.md for the
input format (the original `required_mm` plans still work unchanged).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from organizer import GenerationError, PlanError, generate, plan_layout, write_failure_report


def _print_issues(err: PlanError) -> None:
    for it in err.issues.errors:
        print(f"  - {it.message}", file=sys.stderr)
    miss = err.issues.missing_measurements
    if miss:
        print(f"Missing measurements ({len(miss)}): measure these rather than guessing.", file=sys.stderr)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate 3D-printable board game organizer trays (STL + GLB)")
    p.add_argument("plan", help="plan JSON (from convert_grouping_output.py, or hand-written)")
    p.add_argument("-o", "--out", default="generated_organizer", help="output directory")
    p.add_argument("--check", action="store_true", help="validate, group and pack only (no CAD, no files)")
    p.add_argument("--allow-partial", action="store_true",
                   help="skip compartments with errors/missing measurements instead of failing")
    p.add_argument("--no-glb", action="store_true", help="do not write organizer.glb")
    p.add_argument("--no-thickness-check", action="store_true", help="skip the ray-cast wall thickness check")
    args = p.parse_args(argv)
    out = Path(args.out)

    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

        if args.check:
            pl, designs, packing = plan_layout(plan, allow_partial=args.allow_partial)
            print(f"Plan OK: {len(designs)} trays, {packing.layers} layer(s), packing {packing.method}")
            for t in packing.trays:
                print(f"  {t.design.name}: {t.w:.1f} × {t.l:.1f} × {t.h:.1f} mm at "
                      f"({t.x:.1f}, {t.y:.1f}, {t.z:.1f}) rot {t.rot} layer {t.layer}")
            for w in pl.issues.warnings:
                print(f"  warning: {w.message}")
            return 0

        result = generate(plan, out, glb=not args.no_glb, allow_partial=args.allow_partial,
                          thickness_check=not args.no_thickness_check)
    except PlanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _print_issues(e)
        rep = write_failure_report(out, e)
        print(f"Full report: {rep.resolve()}", file=sys.stderr)
        return 1
    except GenerationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(f"Files were written to {out.resolve()} for inspection (layout.json status = invalid).",
              file=sys.stderr)
        return 2
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Generated {len(result['trays'])} printable STL files + layout.json"
          f"{' + organizer.glb' if result['files']['glb'] else ''} in {out.resolve()}")
    print("Packing algorithm:", result["method"])
    st = result["stats"]
    print(f"Layers: {st['layers']}  floor coverage: {st['floor_coverage']:.0%}  "
          f"all meshes watertight/manifold: {result['validation']['meshes_ok']}")
    for w in result["warnings"]:
        print(f"  warning: {w['message']}")
    for a in result["assumptions"]:
        print(f"  assumption: {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
