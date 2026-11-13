# SPDX-License-Identifier: MIT
"""``pixi run place <board.kicad_pcb>``: place a board from scratch, route-check and evaluate it."""

from __future__ import annotations

import argparse
import json
import sys

from .pipeline import place_board


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="place", description=__doc__)
    ap.add_argument("board")
    ap.add_argument("--out")
    ap.add_argument("--side-policy", choices=["expert", "top_only"], help="default: the intent's, else expert")
    ap.add_argument("--intent", help="design_intent.json (default: drafted from the netlist)")
    ap.add_argument("--schematic", help="KiCad schematic for cold placement with this PCB as a geometry template")
    ap.add_argument("--mode", default="best", choices=["best", "analytical", "sa_only"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-route", action="store_true")
    ap.add_argument("--floorplan", action="store_true", help="try placement with local blocks and fanout reservations")
    ap.add_argument("--route-timeout", type=int)
    args = ap.parse_args(argv)
    rep = place_board(args.board, out_dir=args.out, side_policy=args.side_policy, mode=args.mode, seed=args.seed,
                      route=not args.no_route, router_timeout_s=args.route_timeout, intent_path=args.intent,
                      floorplan=args.floorplan, schematic_path=args.schematic)
    info = rep["stages"]["place"]
    route = rep["metrics"]["route"]
    print(json.dumps({"passed": rep["passed"], "gates": {g["name"]: [g["value"], g["passed"]] for g in rep["gates"]},
                      "mode": info["mode"], "seconds_s0_s5": info["seconds_s0_s5"],
                      "hpwl_signal_mm": rep["metrics"]["hpwl"]["signal_mm"],
                      "coverage": route["coverage"] if route else None,
                      "placed_board": rep["artifacts"]["placed_board"], "gallery": rep["artifacts"]["gallery"]},
                     indent=1))
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
