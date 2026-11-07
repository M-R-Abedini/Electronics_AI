# SPDX-License-Identifier: MIT
"""``pixi run route-check <board.kicad_pcb>``: strict route check of one board."""

from __future__ import annotations

import argparse
import json
import sys

from .check import route_check


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="route-check", description=__doc__)
    ap.add_argument("board")
    ap.add_argument("--out", help="run directory (default .runtime/route_check/<stem>/<utc>)")
    ap.add_argument("--neck-width-um", type=float, help="override; default = board minimum track width")
    ap.add_argument("--no-obstacle-keepouts", action="store_true",
                    help="experiment only: do not export mask apertures and copper graphics as keepouts")
    args = ap.parse_args(argv)
    report = route_check(args.board, out_dir=args.out, neck_width_um=args.neck_width_um,
                         obstacle_keepouts=not args.no_obstacle_keepouts)
    m = report["metrics"]
    print(json.dumps({"passed": report["passed"],
                      "gates": {g["name"]: [g["value"], g["op"], g["threshold"], g["passed"]] for g in report["gates"]},
                      "coverage": m["coverage"], "unrouted": m["unrouted"], "vias": m["vias"],
                      "length_mm": m["track_length_mm"], "session_sha256": m["session_sha256"],
                      "causes": sorted({f["cause"] for f in report["failures"]}),
                      "report": report["artifacts"]["run_dir"] + "/run_report.json"}, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
