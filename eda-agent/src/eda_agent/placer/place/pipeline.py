# SPDX-License-Identifier: MIT
"""``place_board``: S0 contract -> S1 fixed -> S2b sides -> S3 global -> S4 legalize -> S5 anneal -> write.

S6a and S2 macros arrive in F3; this pipeline routes everything with S6c
(Freerouting) through the strict route check, then evaluates (S7).  The input
file is only read; the result is a new board plus ``run_report.json``.

``mode`` selects the global stage (DG-F1): ``analytical`` (S3) or ``sa_only``
(a deterministic greedy start, then the same S4 and S5).
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import report as run_report
from ..eval.score import evaluate
from ..io.kicad import read_board, write_board
from ..render import Panel, gallery
from ..route.check import route_check
from ..toolchain import RUNTIME_DIR
from . import analytical, problem

SA_MOVES_PER_PART = 4000  # declared: annealing budget scales with movable parts ...
SA_MIN_MOVES = 200_000
SA_MAX_MOVES = 300_000  # ... within a fixed cap (deterministic, keeps S0-S5 under 5 minutes on video)
MODES = ("analytical", "sa_only")
W_CROSS = 2.0  # declared: cost of one pin-pair crossing in mm of wirelength
LEGALIZE_STEP_MM = 0.2
LEGALIZE_RETRIES = 2


def _solve_one(board, *, side_policy: str, mode: str, seed: int) -> tuple:
    """S1 to S5 on the IR with one global stage; returns (placed board, stage report)."""
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    pb = problem.build(board, side_policy=side_policy)
    timings["s0_s2b"] = round(time.monotonic() - t0, 2)
    movable = sum(1 for p in pb.parts if not p[5])
    t = time.monotonic()
    if mode == "analytical":
        s3 = analytical.global_place(pb)
        start = s3.poses
        s3_info = {"iterations": s3.iterations, "overflow": s3.overflow, "wa_wirelength": s3.wirelength}
    elif mode == "sa_only":
        start = _greedy_start(pb)
        s3_info = {"start": "greedy spiral at the board centre"}
    else:
        raise ValueError(f"unknown mode {mode!r}")
    timings["s3"] = round(time.monotonic() - t, 2)
    core = problem.rust_problem(pb, w_cross=W_CROSS)
    core.set_positions([pose if not part[5] else (part[7], part[8], part[9]) for pose, part in zip(start, pb.parts,
                                                                                                strict=True)])
    t = time.monotonic()
    diag = problem.board_diag(pb)
    start_poses = core.positions()
    failed = core.legalize(LEGALIZE_STEP_MM, diag)
    retries = 0
    while failed and retries < LEGALIZE_RETRIES:  # deterministic retry: the parts left over go first
        retries += 1
        core.set_positions(start_poses)
        failed = core.legalize(LEGALIZE_STEP_MM / 2, diag, sorted(set(failed)))
    after_legalize = core.cost()
    timings["s4"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    iters = min(SA_MAX_MOVES, max(SA_MIN_MOVES, SA_MOVES_PER_PART * movable))
    sa = core.anneal(iters, seed, max(diag * 0.15, 1.0), 0.05) if not failed else {"skipped": "illegal start"}
    timings["s5"] = round(time.monotonic() - t, 2)
    illegal = core.illegal_parts()
    placed = problem.apply(pb, core.positions())
    info = {
        "mode": mode, "side_policy": side_policy, "seed": seed, "movable": movable,
        "fixed": pb.fixed, "s3": s3_info, "s4_failed": [pb.refs[i] for i in failed], "s4_retries": retries,
        "cost_after_s4": after_legalize, "s5": sa, "cost_final": core.cost(),
        "illegal_after_s5": [pb.refs[i] for i in illegal], "timings": timings,
        "seconds_s0_s5": round(time.monotonic() - t0, 2),
    }
    return placed, info


def solve(board, *, side_policy: str = "expert", mode: str = "best", seed: int = 0) -> tuple:
    """S1 to S5.  ``best`` (default) runs both global stages and keeps the legal result of lower cost.

    DG-F1 evidence: the analytical start won on ecc83 and pic_programmer, the
    greedy start on royalblue and StickHub (where S3's clusters left no room for
    the last parts), so neither is dropped; each run reports both.
    """
    if mode != "best":
        return _solve_one(board, side_policy=side_policy, mode=mode, seed=seed)
    t0 = time.monotonic()
    results = [(m, *_solve_one(board, side_policy=side_policy, mode=m, seed=seed)) for m in MODES]

    def rank(item):
        _m, _placed, info = item
        return (len(info["illegal_after_s5"]) > 0 or len(info["s4_failed"]) > 0, info["cost_final"]["total"])

    mode, placed, info = min(results, key=rank)
    info = dict(info)
    info["mode"] = f"best:{mode}"
    info["candidates"] = {m: {"cost": i["cost_final"], "illegal": i["illegal_after_s5"], "s4_failed": i["s4_failed"],
                              "seconds": i["seconds_s0_s5"]} for m, _p, i in results}
    info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    return placed, info


def _greedy_start(pb: problem.PlaceProblem) -> list[tuple[float, float, int]]:
    xs = [p[0] for ring in pb.outers for p in ring]
    ys = [p[1] for ring in pb.outers for p in ring]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    golden = math.pi * (3 - math.sqrt(5))
    out = []
    for n, part in enumerate(pb.parts):
        if part[5]:
            out.append((part[7], part[8], part[9]))
        else:
            rad = 0.4 * math.sqrt(n + 1)
            out.append((cx + rad * math.cos(n * golden), cy + rad * math.sin(n * golden), 0))
    return out


def place_board(board_path: str | Path, *, out_dir: str | Path | None = None, side_policy: str | None = None,
                mode: str = "best", seed: int = 0, route: bool = True,
                router_timeout_s: int | None = None, intent_path: str | Path | None = None) -> dict:
    """Inputs that decide the output (P7): the board, the intent (file or netlist draft), the seed, the config."""
    from ..intent.resolve import resolve
    from ..intent.resolve import side_policy as policy_of

    src = Path(board_path).resolve()
    board = read_board(src)
    intent, intent_info = resolve(board, intent_path)
    side_policy = policy_of(intent, side_policy)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "place" / f"{board.name}-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    placed, info = solve(board, side_policy=side_policy, mode=mode, seed=seed)
    out_board = write_board(src, placed, run_dir / f"{board.name}.placed.kicad_pcb", strip_routing=True)
    routed = (route_check(out_board, out_dir=run_dir / "route", reference_board=src, router_timeout_s=router_timeout_s)
              if route else None)
    fixed = set(info["fixed"])
    evaluation = evaluate(placed, routed["metrics"] if routed else None, fixed, intent)
    page = gallery([Panel(f"{board.name} (expert)", board, evaluate(board, fixed=fixed, intent=intent)),
                    Panel(f"{board.name} (placer v2, {mode})",
                          read_board(routed["artifacts"]["routed_board"]) if routed else placed, evaluation,
                          routed["metrics"] if routed else None)],
                   run_dir / "gallery.html", title=f"{board.name}: placer v2 vs expert")
    gates = [run_report.gate("legality_violations", evaluation["legality"]["strict_violations"], 0, "=="),
             run_report.gate("s0_s5_seconds", info["seconds_s0_s5"], 300, "<=")]
    if routed:
        gates += [g for g in routed["gates"] if g["name"] != "time_budget_s"]
    rep = run_report.build(
        kind="place_board", inputs={"board": board.source, "intent": intent_info},
        config={"side_policy": side_policy, "mode": mode, "seed": seed, "w_cross": problem.WEIGHT_SIGNAL * W_CROSS,
                "sa_moves_per_part": SA_MOVES_PER_PART},
        stages={"place": info, "route_check": {"run_report": str(run_dir / "route" / "run_report.json")} if routed else None},
        metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"],
                 "route": routed["metrics"] if routed else None},
        gates=gates,
        failures=(routed["failures"] if routed else []) + [
            {"stage": "legality", "cause": f["kind"], "refs": f["refs"]}
            for f in evaluation["legality"]["findings"] if not f["fixed"]],
        artifacts={"run_dir": str(run_dir), "placed_board": str(out_board), "gallery": str(page)},
    )
    run_report.write(rep, run_dir / "run_report.json")
    return rep


