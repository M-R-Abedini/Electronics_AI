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
from ..eval import legality
from ..eval.metrics import critical_loops_intent, crossings
from ..eval.score import evaluate
from ..io.kicad import read_board, write_board
from ..render import Panel, gallery
from ..route.check import route_check
from ..toolchain import RUNTIME_DIR
from . import analytical, blocks, escape, mechanical, problem

SA_MOVES_PER_PART = 4000  # declared: annealing budget scales with movable parts ...
SA_MIN_MOVES = 200_000
SA_MAX_MOVES = 300_000  # ... within a fixed cap (deterministic, keeps S0-S5 under 5 minutes on video)
MODES = ("analytical", "sa_only")
W_CROSS = 2.0  # declared: cost of one pin-pair crossing in mm of wirelength
LEGALIZE_STEP_MM = 0.2
LEGALIZE_RETRIES = 2


def _solve_one(board, *, side_policy: str, mode: str, seed: int, intent: dict | None = None,
               floorplan: bool = False, reserve: bool = False) -> tuple:
    """S1 to S5 on the IR with one global stage; returns (placed board, stage report)."""
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    pb = problem.build(board, side_policy=side_policy, intent=intent, floorplan=floorplan, reserve=reserve)
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
    priority, seeded = [], 0
    if floorplan and intent:
        start, priority, seeded = blocks.seed(pb, intent, start)
        s3_info["block_seeded_parts"] = seeded
    timings["s3"] = round(time.monotonic() - t, 2)
    core = problem.rust_problem(pb, w_cross=W_CROSS)
    core.set_positions([pose if not part[5] else (part[7], part[8], part[9]) for pose, part in zip(start, pb.parts,
                                                                                                strict=True)])
    t = time.monotonic()
    diag = problem.board_diag(pb)
    start_poses = core.positions()
    failed = core.legalize(LEGALIZE_STEP_MM, diag, priority)
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
    violations = sum(not finding.fixed for finding in legality.check(placed, fixed=set(pb.fixed)))
    info = {
        "mode": mode, "side_policy": side_policy, "seed": seed, "movable": movable,
        "fixed": pb.fixed, "decap_affinities": len(pb.affinities) - pb.local_block_links,
        "local_block_links": pb.local_block_links, "s3": s3_info,
        "s4_failed": [pb.refs[i] for i in failed], "s4_retries": retries,
        "cost_after_s4": after_legalize, "s5": sa, "cost_final": core.cost(),
        "illegal_after_s5": [pb.refs[i] for i in illegal], "strict_violations": violations, "timings": timings,
        "seconds_s0_s5": round(time.monotonic() - t0, 2),
    }
    return placed, info


def _solve(board, *, side_policy: str, mode: str, seed: int, intent: dict | None, floorplan: bool,
           reserve: bool = False) -> tuple:
    """S1 to S5.  ``best`` (default) runs both global stages and keeps the legal result of lower cost.

    DG-F1 evidence: the analytical start won on ecc83 and pic_programmer, the
    greedy start on royalblue and StickHub (where S3's clusters left no room for
    the last parts), so neither is dropped; each run reports both.
    """
    if mode != "best":
        return _solve_one(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent,
                          floorplan=floorplan, reserve=reserve)
    t0 = time.monotonic()
    results = [(m, *_solve_one(board, side_policy=side_policy, mode=m, seed=seed, intent=intent,
                               floorplan=floorplan, reserve=reserve))
               for m in MODES]

    def rank(item):
        _m, _placed, info = item
        return (*_legality_key(info), info["cost_final"]["total"])

    mode, placed, info = min(results, key=rank)
    info = dict(info)
    info["mode"] = f"best:{mode}"
    info["candidates"] = {m: {"cost": i["cost_final"], "illegal": i["illegal_after_s5"], "s4_failed": i["s4_failed"],
                              "seconds": i["seconds_s0_s5"]} for m, _p, i in results}
    info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    return placed, info


def solve(board, *, side_policy: str = "expert", mode: str = "best", seed: int = 0,
          intent: dict | None = None, floorplan: bool = False) -> tuple:
    """Place the board, comparing independent floorplan candidates when requested."""
    t0 = time.monotonic()
    original = board
    board, anchors = mechanical.anchor(board, intent or {})
    use_floorplan = bool(floorplan and intent)
    placed, info = _solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent, floorplan=False)
    boxes = {fp.ref: problem.local_box(fp) for fp in board.footprints}
    exits = escape.envelopes(board, intent or {}, boxes)

    def measure(candidate):
        flipped = {fp.ref for fp in candidate.footprints if fp.side != board.fp(fp.ref).side}
        return escape.audit(candidate, exits, flipped=flipped)

    selected = escape.select_reservations(exits) if use_floorplan else {}
    variant = "baseline"
    audit = measure(placed)
    comparisons = {}
    if use_floorplan:
        baseline, baseline_info = placed, info
        base_metrics = _floorplan_metrics(baseline, intent)
        rank = _floorplan_rank(info, base_metrics, audit)
        modes = [("blocks", True, False)]
        if selected:
            modes.extend([("reserve", False, True), ("combined", True, True)])
        for name, use_blocks, reserve in modes:
            other, other_info = _solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent,
                                       floorplan=use_blocks, reserve=reserve)
            guard = _compare_floorplans(other, other_info, baseline, baseline_info, intent, base_metrics=base_metrics)
            other_audit = measure(other)
            other_rank = _floorplan_rank(other_info, guard["candidate"], other_audit)
            comparisons[name] = {**guard, "clear_exits": other_audit["clear"],
                                 "strict_violations": other_info["strict_violations"],
                                 "seconds_s0_s5": other_info["seconds_s0_s5"]}
            if not guard["use_baseline"] and other_rank < rank:
                variant, placed, info, audit, rank = name, other, other_info, other_audit, other_rank
    reservations_used = variant in ("reserve", "combined")
    info["floorplan"] = {"requested": use_floorplan, "reservations_used": reservations_used,
                         "reserved_pad_count": sum(map(len, selected.values())) if reservations_used else 0,
                         "selected_variant": variant, "quality_guard": comparisons.get(variant),
                         "comparisons": comparisons, "escape": audit}
    for row in anchors:
        ref = row["ref"]
        placed.fp(ref).locked = original.fp(ref).locked
        info["fixed"][ref] = f"mechanical constraint ({row['mode']})"
    info["mechanical_anchors"] = anchors
    info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    return placed, info


def _legality_key(info: dict) -> tuple:
    return info["strict_violations"], len(info["illegal_after_s5"]), len(info["s4_failed"])


def _floorplan_metrics(board, intent: dict) -> dict:
    loops = critical_loops_intent(board, intent)
    distance = loops["resolved_decap_pin_distance"]
    return {"crossings": crossings(board)["count"], "decap_p90_mm": distance.get("p90_mm"),
            "resolved_decaps": distance["n"], "all_decap_p90_mm": loops["decap_pin_distance"].get("p90_mm")}


def _floorplan_rank(info: dict, metrics: dict, audit: dict) -> tuple:
    tail = metrics["decap_p90_mm"]
    return (*_legality_key(info), -audit["clear"], metrics["crossings"], tail if tail is not None else math.inf)


def _compare_floorplans(candidate, candidate_info, baseline, baseline_info, intent: dict, *, base_metrics=None) -> dict:
    """Prefer the baseline when an equally legal trial worsens a measured objective."""
    trial = _floorplan_metrics(candidate, intent)
    base = base_metrics if base_metrics is not None else _floorplan_metrics(baseline, intent)
    regressions = [key for key in ("crossings", "decap_p90_mm")
                   if base[key] is not None and (trial[key] is None or trial[key] > base[key])]
    if trial["resolved_decaps"] < base["resolved_decaps"]:
        regressions.append("resolved_decap_coverage")
    trial_key, base_key = _legality_key(candidate_info), _legality_key(baseline_info)
    return {"candidate": trial, "baseline": base, "regressions": regressions,
            "use_baseline": base_key < trial_key or (base_key == trial_key and bool(regressions))}


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
                router_timeout_s: int | None = None, intent_path: str | Path | None = None,
                floorplan: bool = False, schematic_path: str | Path | None = None) -> dict:
    """Inputs that decide the output (P7): the board, the intent (file or netlist draft), the seed, the config."""
    from ..intent.resolve import resolve
    from ..intent.resolve import side_policy as policy_of

    src = Path(board_path).resolve()
    reference = read_board(src)
    board, cold = reference, None
    if schematic_path is not None:
        from ..io.schematic_input import cold_input

        board, cold = cold_input(reference, Path(schematic_path).resolve(strict=True))
    intent, intent_info = resolve(board, intent_path)
    side_policy = policy_of(intent, side_policy)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "place" / f"{board.name}-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    placed, info = solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent, floorplan=floorplan)
    if cold is not None:
        for ref, reason in cold["anchors"].items():
            placed.fp(ref).locked = reference.fp(ref).locked
            info["fixed"][ref] = reason
        cold["assembly_sides"] = side_policy
        if info["movable"] == 0 and cold["reset_poses"]:
            raise RuntimeError("cold placement did not expose any reset footprints to the solver")
        info["cold_input"] = cold
    out_board = write_board(src, placed, run_dir / f"{board.name}.placed.kicad_pcb", strip_routing=True,
                            strip_zones=cold is not None)
    routed = (route_check(out_board, out_dir=run_dir / "route", reference_board=src, router_timeout_s=router_timeout_s)
              if route else None)
    fixed = set(info["fixed"])
    evaluation = evaluate(placed, routed["metrics"] if routed else None, fixed, intent)
    reference_for_score = reference
    if cold is not None:
        reference_for_score = board.copy()
        for fp in reference_for_score.footprints:
            source = reference.fp(fp.ref)
            fp.x, fp.y, fp.rotation, fp.locked = source.x, source.y, source.rotation, source.locked
    reference_evaluation = evaluate(reference_for_score, fixed=fixed, intent=intent)
    page = gallery([Panel(f"{board.name} (reference)", reference_for_score, reference_evaluation),
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
                "sa_moves_per_part": SA_MOVES_PER_PART, "floorplan": floorplan},
        stages={"place": info, "route_check": {"run_report": str(run_dir / "route" / "run_report.json")} if routed else None},
        metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"], "fanout_escape": info["floorplan"]["escape"],
                 "route": routed["metrics"] if routed else None},
        gates=gates,
        failures=(routed["failures"] if routed else []) + [
            {"stage": "legality", "cause": f["kind"], "refs": f["refs"]}
            for f in evaluation["legality"]["findings"] if not f["fixed"]],
        artifacts={"run_dir": str(run_dir), "placed_board": str(out_board), "gallery": str(page)},
        reference_evaluation=reference_evaluation if cold is not None else None,
    )
    run_report.write(rep, run_dir / "run_report.json")
    return rep


