# SPDX-License-Identifier: MIT
"""``place_board``: S0 contract -> S1 fixed -> S2b sides -> S3 global -> S4 legalize -> S5 anneal -> write.

S6a and S2 macros arrive in F3; this pipeline routes everything with S6c
(Freerouting) through the strict route check, then evaluates (S7).  The input
file is only read; the result is a new board plus ``run_report.json``.

``mode`` selects the global stage (DG-F1): ``analytical`` (S3) or ``sa_only``
(a deterministic greedy start, then the same S4 and S5).
"""

from __future__ import annotations

import copy
import math
import time
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Polygon

from .. import report as run_report
from ..eval import legality
from ..eval.metrics import critical_loops_intent, crossings
from ..eval.score import evaluate
from ..intent import model
from ..io.kicad import read_board, write_board
from ..render import Panel, gallery
from ..route.check import route_check
from ..toolchain import RUNTIME_DIR
from . import analytical, blocks, decap, escape, mechanical, problem, rigid

SA_MOVES_PER_PART = 4000  # declared: annealing budget scales with movable parts ...
SA_MIN_MOVES = 200_000
SA_MAX_MOVES = 300_000  # ... within a fixed cap (deterministic, keeps S0-S5 under 5 minutes on video)
MODES = ("analytical", "sa_only")
W_CROSS = 2.0  # declared: cost of one pin-pair crossing in mm of wirelength
LEGALIZE_STEP_MM = 0.2
LEGALIZE_RETRIES = 2
MAX_CORRIDOR_TRIALS = 24


def _solve_one(board, *, side_policy: str, mode: str, seed: int, intent: dict | None = None,
               floorplan: bool = False, reserve: bool = False, rigid_blocks: bool = False,
               budget_divisor: int = 1) -> tuple:
    """S1 to S5 on the IR with one global stage; returns (placed board, stage report)."""
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    pb = problem.build(board, side_policy=side_policy, intent=intent, floorplan=floorplan, reserve=reserve)
    timings["s0_s2b"] = round(time.monotonic() - t0, 2)
    movable = sum(1 for p in pb.parts if not p[5])
    t = time.monotonic()
    solver_pb, retained, macros = pb, list(range(len(pb.parts))), {}
    if rigid_blocks and intent:
        groups = {}
        seeded_poses, _, _ = blocks.seed(pb, intent, _greedy_start(pb), groups=groups)
        macros = rigid.compile_macros(pb, seeded_poses, groups)
        if macros:
            solver_pb, retained = rigid.collapse(pb, macros)
    if mode == "analytical":
        s3 = analytical.global_place(solver_pb, max_iters=analytical.MAX_ITERS // budget_divisor)
        start = s3.poses
        s3_info = {"iterations": s3.iterations, "overflow": s3.overflow, "wa_wirelength": s3.wirelength}
    elif mode == "sa_only":
        start = _greedy_start(solver_pb)
        s3_info = {"start": "greedy spiral at the board centre"}
    else:
        raise ValueError(f"unknown mode {mode!r}")
    priority, seeded = [], 0
    if floorplan and intent and not macros:
        start, priority, seeded = blocks.seed(pb, intent, start)
        s3_info["block_seeded_parts"] = seeded
    timings["s3"] = round(time.monotonic() - t, 2)
    core = problem.rust_problem(solver_pb, w_cross=W_CROSS)
    core.set_positions([pose if not part[5] else (part[7], part[8], part[9]) for pose, part in zip(start, solver_pb.parts,
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
    iters = min(SA_MAX_MOVES, max(SA_MIN_MOVES, SA_MOVES_PER_PART * movable)) // budget_divisor
    sa = core.anneal(iters, seed, max(diag * 0.15, 1.0), 0.05) if not failed else {"skipped": "illegal start"}
    timings["s5"] = round(time.monotonic() - t, 2)
    illegal = core.illegal_parts()
    placed = problem.apply(pb, rigid.expand(pb, core.positions(), retained, macros) if macros else core.positions())
    findings = legality.check(placed, fixed=set(pb.fixed))
    th_findings = [f for f in findings if not f.fixed and f.detail.startswith("thru_hole_pads:")]
    if th_findings and not failed and not illegal and "F" in pb.sides.values() and "B" in pb.sides.values():
        placed, core, findings = _resolve_movable_th_collisions(
            placed, pb, solver_pb, core, retained, macros, diag, th_findings, findings
        )
    if not failed and not illegal:
        placed, core, findings = _resolve_edge_copper_clearances(
            placed, pb, solver_pb, core, retained, macros, diag, findings
        )
    violations = sum(not finding.fixed for finding in findings)
    info = {
        "mode": mode, "side_policy": side_policy, "seed": seed, "movable": movable,
        "fixed": pb.fixed, "decap_affinities": len(pb.affinities) - pb.local_block_links,
        "local_block_links": pb.local_block_links, "s3": s3_info,
        "s4_failed": [solver_pb.refs[i] for i in failed], "s4_retries": retries,
        "cost_after_s4": after_legalize, "s5": sa, "cost_final": core.cost(),
        "illegal_after_s5": [solver_pb.refs[i] for i in illegal], "strict_violations": violations, "timings": timings,
        "seconds_s0_s5": round(time.monotonic() - t0, 2),
    }
    if macros:
        corridor_info = rigid.corridors(placed, macros)
        if not (failed or illegal or violations):
            placed, corridor_info = rigid.clear_blockers(placed, macros, set(pb.fixed), intent, corridor_info,
                                                          max_trials=MAX_CORRIDOR_TRIALS)
            if corridor_info["moved"]:
                poses = core.positions()
                moved = set(corridor_info["moved"])
                core.set_positions([(*placed.fp(ref).position, pose[2]) if ref in moved else pose
                                    for ref, pose in zip(solver_pb.refs, poses, strict=True)])
                info["cost_final"] = core.cost()
        info["rigid_macros"] = {"members": [[p.ref for p in m.parts] for m in macros.values()],
                                "corridors": corridor_info}
        info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    info["iteration_budget"] = {"analytical": analytical.MAX_ITERS // budget_divisor, "annealing": iters}
    return placed, info


def _resolve_movable_th_collisions(placed, pb, solver_pb, core, retained, macros, diag, th_findings, findings):
    idx_by_ref = {ref: idx for idx, ref in enumerate(solver_pb.refs)}
    member_to_anchor = {p.ref: anchor for anchor, m in (macros or {}).items() for p in m.parts}
    max_clear = max([0.15, float(pb.board.rules.get("min_clearance") or 0.0),
                     *(float(nc.clearance or 0.0) for nc in pb.board.netclasses)])

    def _relocate_side(cur_placed, cur_core, cur_th_findings, side_to_move: str, side_holes: list):
        targets = sorted({
            member_to_anchor.get(r, r)
            for f in cur_th_findings if not f.fixed and f.detail.startswith("thru_hole_pads:")
            for r in f.refs if r not in pb.fixed and pb.sides.get(r) == side_to_move
        })
        if not targets or not all(r in idx_by_ref for r in targets):
            return cur_placed, cur_core, False
        poses_now = cur_core.positions()
        target_idxs = [idx_by_ref[r] for r in targets]
        move_set = set(target_idxs)
        new_parts = [
            (*part[:5], idx not in move_set, part[6] if idx in move_set else False, *poses_now[idx])
            for idx, part in enumerate(solver_pb.parts)
        ]
        fix_pb = copy.copy(solver_pb)
        fix_pb.parts = new_parts
        fix_pb.holes = list(solver_pb.holes) + side_holes
        fix_core = problem.rust_problem(fix_pb, w_cross=W_CROSS)
        if fix_core.legalize(LEGALIZE_STEP_MM / 2, diag, target_idxs):
            return cur_placed, cur_core, False
        fixed_poses = fix_core.positions()
        cur_core.set_positions(fixed_poses)
        next_placed = problem.apply(pb, rigid.expand(pb, fixed_poses, retained, macros) if macros else fixed_poses)
        return next_placed, cur_core, True

    def _side_holes(cur_placed, cur_th_findings, side_to_move: str, pass_idx: int) -> list:
        opp = "F" if side_to_move == "B" else "B"
        lmap = legality.copper_layer_map(cur_placed)
        base_margin = max(0.25, max_clear + 0.15) + pass_idx * 0.10
        holes = [
            ring for fp in cur_placed.footprints
            if fp.side == opp
            for ring in legality.thru_hole_hole_rings(fp, margin_mm=base_margin, layer_map=lmap)
        ]
        overhangs = [
            legality.thru_hole_overhang_mm(cur_placed.fp(owner))
            for f in cur_th_findings
            if not f.fixed and f.detail.startswith("thru_hole_pads:")
            and (owner := f.detail.split(":", 1)[1]) not in pb.fixed
            and pb.sides.get(owner) == side_to_move
        ]
        if overhangs:
            margin = max(base_margin, 0.20 + max(overhangs))
            for fp in cur_placed.footprints:
                if fp.side == opp:
                    holes.extend(
                        legality.thru_hole_hole_rings(fp, margin_mm=margin, thru_hole_only=False, layer_map=lmap)
                    )
        return holes

    for pass_idx in range(2):
        if not th_findings:
            break
        moved_any = False
        for side_to_move in ("B", "F"):
            if not th_findings:
                break
            side_holes = _side_holes(placed, th_findings, side_to_move, pass_idx)
            if not side_holes:
                continue
            placed, core, moved_side = _relocate_side(placed, core, th_findings, side_to_move, side_holes)
            if moved_side:
                moved_any = True
                findings = legality.check(placed, fixed=set(pb.fixed))
                th_findings = [f for f in findings if not f.fixed and f.detail.startswith("thru_hole_pads:")]
        if not moved_any:
            break
    return placed, core, findings


def _resolve_edge_copper_clearances(placed, pb, solver_pb, core, retained, macros, diag, findings):
    edge_findings = [
        f for f in findings
        if not f.fixed and f.kind == "outside_outline" and f.detail.startswith("copper_edge_clearance:")
    ]
    if not edge_findings:
        return placed, core, findings
    edge_clear = float(pb.board.rules.get("copper_edge_clearance") or 0.0)
    if edge_clear <= 0:
        return placed, core, findings
    idx_by_ref = {ref: idx for idx, ref in enumerate(solver_pb.refs)}
    member_to_anchor = {p.ref: anchor for anchor, m in (macros or {}).items() for p in m.parts}
    inset_outers: list[list[tuple[float, float]]] = []
    for ring in solver_pb.outers:
        shrunk = Polygon(ring).buffer(-(edge_clear + 0.05))
        if shrunk.is_empty:
            continue
        geoms = [shrunk] if shrunk.geom_type == "Polygon" else list(getattr(shrunk, "geoms", ()))
        for poly in geoms:
            if not poly.is_empty and poly.area > 0:
                inset_outers.append([(round(x, 6), round(y, 6)) for x, y in list(poly.exterior.coords)[:-1]])
    if not inset_outers:
        return placed, core, findings
    targets = sorted({member_to_anchor.get(r, r) for f in edge_findings for r in f.refs if r not in pb.fixed})
    if not targets or not all(r in idx_by_ref for r in targets):
        return placed, core, findings
    poses_now = core.positions()
    target_idxs = [idx_by_ref[r] for r in targets]
    move_set = set(target_idxs)
    new_parts = [
        (*part[:5], idx not in move_set, part[6] if idx in move_set else False, *poses_now[idx])
        for idx, part in enumerate(solver_pb.parts)
    ]
    fix_pb = copy.copy(solver_pb)
    fix_pb.parts = new_parts
    fix_pb.outers = inset_outers
    fix_core = problem.rust_problem(fix_pb, w_cross=W_CROSS)
    if fix_core.legalize(LEGALIZE_STEP_MM / 2, diag, target_idxs):
        return placed, core, findings
    fixed_poses = fix_core.positions()
    core.set_positions(fixed_poses)
    next_placed = problem.apply(pb, rigid.expand(pb, fixed_poses, retained, macros) if macros else fixed_poses)
    return next_placed, core, legality.check(next_placed, fixed=set(pb.fixed))


def _solve(board, *, side_policy: str, mode: str, seed: int, intent: dict | None, floorplan: bool,
           reserve: bool = False, rigid_blocks: bool = False, budget_divisor: int = 1) -> tuple:
    """S1 to S5.  ``best`` (default) runs both global stages and keeps the legal result of lower cost.

    DG-F1 evidence: the analytical start won on ecc83 and pic_programmer, the
    greedy start on royalblue and StickHub (where S3's clusters left no room for
    the last parts), so neither is dropped; each run reports both.
    """
    if mode != "best":
        return _solve_one(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent,
                          floorplan=floorplan, reserve=reserve, rigid_blocks=rigid_blocks, budget_divisor=budget_divisor)
    t0 = time.monotonic()
    results = [(m, *_solve_one(board, side_policy=side_policy, mode=m, seed=seed, intent=intent,
                               floorplan=floorplan, reserve=reserve, rigid_blocks=rigid_blocks,
                               budget_divisor=budget_divisor))
               for m in MODES]

    def rank(item):
        _m, _placed, info = item
        return (*_legality_key(info), bool(rigid_blocks and not info.get("rigid_macros")), info["cost_final"]["total"])

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
    boxes = {fp.ref: problem.local_box(fp) for fp in board.footprints}
    exits = escape.envelopes(board, intent or {}, boxes)

    def measure(candidate):
        flipped = {fp.ref for fp in candidate.footprints if fp.side != board.fp(fp.ref).side}
        return escape.audit(candidate, exits, flipped=flipped)

    selected = escape.select_reservations(exits) if use_floorplan else {}
    modes = []
    if use_floorplan:
        modes.append(("blocks", True, False))
        if intent.get("blocks"):
            modes.append(("rigid", True, False))
        if selected:
            modes.extend([("reserve", False, True), ("combined", True, True)])
    divisor = len(modes) + 1
    budget = {"budget_divisor": divisor} if divisor > 1 else {}
    def blocked_exits(escape_audit):
        return {(r["ref"], r["pin"], r["net"]) for r in escape_audit.get("blocked", [])}

    def refine_decap(cand, cand_info, cand_audit):
        if not use_floorplan:
            return cand, cand_audit, None
        rm = cand_info.get("rigid_macros") or {}
        if rm.get("corridors", {}).get("total") or any(_legality_key(cand_info)):
            return cand, cand_audit, None
        rigid_members = {r for group in rm.get("members", []) for r in group}
        refine_fixed = set(cand_info["fixed"]) | rigid_members
        blocked = blocked_exits(cand_audit)

        def preserve(board_state):
            return blocked_exits(measure(board_state)) <= blocked

        cand, ref_report = decap.refine(cand, intent, refine_fixed, preserve_escapes=preserve)
        if ref_report["moved"]:
            cand_audit = measure(cand)
            cand_info["cost_final_stage"] = "before_decap_refinement"
            ref_report["output_metrics"] = _floorplan_metrics(cand, intent)
        return cand, cand_audit, ref_report

    placed, info = _solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent,
                         floorplan=False, **budget)
    variant = "baseline"
    audit = measure(placed)
    comparisons = {}
    tail_refinement = None
    if use_floorplan:
        placed, audit, tail_refinement = refine_decap(placed, info, audit)
        baseline, baseline_info = placed, info
        base_metrics = _floorplan_metrics(baseline, intent)
        rank = _floorplan_rank(baseline_info, base_metrics, audit)
        for name, use_blocks, reserve in modes:
            other, other_info = _solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent,
                                       floorplan=use_blocks, reserve=reserve, rigid_blocks=name == "rigid", **budget)
            other_audit = measure(other)
            other_refinement = None
            if not (name == "rigid" and not other_info.get("rigid_macros")):
                other, other_audit, other_refinement = refine_decap(other, other_info, other_audit)
            guard = _compare_floorplans(other, other_info, baseline, baseline_info, intent, base_metrics=base_metrics)
            other_rank = _floorplan_rank(other_info, guard["candidate"], other_audit)
            comparisons[name] = {**guard, "clear_exits": other_audit["clear"],
                                 "strict_violations": other_info["strict_violations"],
                                 "seconds_s0_s5": other_info["seconds_s0_s5"],
                                 "decap_refinement": other_refinement}
            if name == "rigid":
                comparisons[name]["rigid_macros"] = other_info.get("rigid_macros")
                if not other_info.get("rigid_macros"):
                    continue
            if not guard["use_baseline"] and other_rank < rank:
                variant, placed, info, audit, rank, tail_refinement = (
                    name, other, other_info, other_audit, other_rank, other_refinement
                )
    reservations_used = variant in ("reserve", "combined")
    passive_alignment = None
    rigid_macros = info.get("rigid_macros") or {}
    if use_floorplan and not rigid_macros.get("corridors", {}).get("total") and not any(_legality_key(info)):
        rigid_members = {r for group in rigid_macros.get("members", []) for r in group}
        refine_fixed = set(info["fixed"]) | rigid_members
        blocked = blocked_exits(audit)

        def preserve_align(board_state):
            return blocked_exits(measure(board_state)) <= blocked

        placed, passive_alignment = decap.align_passives(
            placed, intent, refine_fixed, preserve_escapes=preserve_align
        )
        if passive_alignment["moved"]:
            audit = measure(placed)
            info.setdefault("cost_final_stage", "before_passive_alignment")
            passive_alignment["output_metrics"] = _floorplan_metrics(placed, intent)
    info["passive_alignment"] = passive_alignment
    info["floorplan"] = {"requested": use_floorplan, "reservations_used": reservations_used,
                         "reserved_pad_count": sum(map(len, selected.values())) if reservations_used else 0,
                         "selected_variant": variant, "quality_guard": comparisons.get(variant),
                         "comparisons": comparisons, "escape": audit, "iteration_budget_divisor": divisor,
                         "decap_refinement": tail_refinement, "passive_alignment": passive_alignment}
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
                floorplan: bool = False, schematic_path: str | Path | None = None,
                cluster_refs: list[str] | None = None) -> dict:
    """Inputs that decide the output (P7): the board, the intent (file or netlist draft), the seed, the config."""
    from ..intent.resolve import resolve
    from ..intent.resolve import side_policy as policy_of

    src = Path(board_path).resolve()
    reference = read_board(src)
    board, cold = reference, None
    if cluster_refs is not None and (schematic_path is not None or route):
        raise ValueError("cluster placement requires the existing layout and route=False")
    if schematic_path is not None:
        from ..io.schematic_input import cold_input

        board, cold = cold_input(reference, Path(schematic_path).resolve(strict=True))
    intent, intent_info = resolve(board, intent_path)
    side_policy = policy_of(intent, side_policy)
    if cluster_refs is not None:
        if any(ref not in cluster_refs and claim["value"]["mode"] == "region"
               for ref, claim in intent.get("mechanical", {}).items()):
            raise ValueError("cluster intent cannot relocate an outside footprint")
        board, cluster_meta = _cluster_input(reference, cluster_refs)
        # Cluster placement is a local ECO; all components stay on their current side.
        side_policy = "expert"
    else:
        cluster_meta = None
    outside = set(board.placement_anchors) if cluster_refs is not None else set()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "place" / f"{board.name}-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    placed, info = solve(board, side_policy=side_policy, mode=mode, seed=seed, intent=intent, floorplan=floorplan)
    if cluster_refs is not None:
        relocates = any(row["mode"] == "region" for row in info["mechanical_anchors"])
        if info["movable"] == 0 and not relocates:
            raise ValueError("cluster has no movable footprint")
        original_violations = sum(not finding.fixed for finding in legality.check(board, fixed=outside))
        before = {"strict_violations": original_violations, "illegal_after_s5": [], "s4_failed": []}
        guard = _compare_floorplans(placed, info, board, before, intent)
        # Explicit relocation constraints must still be honored even if the original scores better.
        kept_original = guard["use_baseline"] and not relocates
        if kept_original:
            placed = board
            info.update(before, cost_final_stage="rejected_cluster_candidate")
            info["floorplan"].update(selected_variant="original", quality_guard=None, reservations_used=False,
                                     reserved_pad_count=0, escape=escape.audit(
                                         board, escape.envelopes(board, intent,
                                                                  {fp.ref: problem.local_box(fp) for fp in board.footprints})))
            info.pop("rigid_macros", None)
        for fp in placed.footprints:
            fp.locked = reference.fp(fp.ref).locked
        for ref in outside:
            info["fixed"].setdefault(ref, board.placement_anchors[ref])
        if any(
            (fp.position, fp.rotation, fp.side) != (reference.fp(fp.ref).position, reference.fp(fp.ref).rotation,
                                                    reference.fp(fp.ref).side)
            for fp in placed.footprints if fp.ref in outside
        ):
            raise RuntimeError("cluster placement moved an anchored footprint")
        info["cluster_guard"] = {**guard, "kept_original": kept_original}
        info["cluster"] = {**cluster_meta, **guard, "kept_original": kept_original}
        info["zones_require_refill"] = cluster_meta["zones_require_refill"]
    if cold is not None:
        for ref, reason in cold["anchors"].items():
            placed.fp(ref).locked = reference.fp(ref).locked
            info["fixed"][ref] = reason
        cold["assembly_sides"] = side_policy
        if info["movable"] == 0 and cold["reset_poses"]:
            raise RuntimeError("cold placement did not expose any reset footprints to the solver")
        info["cold_input"] = cold
    out_board = write_board(src, placed, run_dir / f"{board.name}.placed.kicad_pcb", strip_routing=True,
                            strip_zones=cold is not None, unfill_zones=cluster_refs is not None)
    routed = (route_check(out_board, out_dir=run_dir / "route", reference_board=src, router_timeout_s=router_timeout_s)
              if route else None)
    fixed = set(info["fixed"])
    evaluation = evaluate(placed, routed["metrics"] if routed else None, fixed, intent)
    reference_for_score = reference
    if cold is not None or cluster_refs is not None:
        reference_for_score = board.copy()
        for fp in reference_for_score.footprints:
            source = reference.fp(fp.ref)
            fp.x, fp.y, fp.rotation, fp.side, fp.locked = (
                source.x, source.y, source.rotation, source.side, source.locked
            )
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
        kind="place_cluster" if cluster_refs is not None else "place_board",
        inputs={"board": board.source, "intent": intent_info},
        config={"side_policy": side_policy, "mode": mode, "seed": seed, "w_cross": problem.WEIGHT_SIGNAL * W_CROSS,
                "sa_moves_per_part": SA_MOVES_PER_PART, "floorplan": floorplan, "cluster_refs": cluster_refs},
        stages={"place": info, "route_check": {"run_report": str(run_dir / "route" / "run_report.json")} if routed else None},
        metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"], "fanout_escape": info["floorplan"]["escape"],
                 "route": routed["metrics"] if routed else None},
        gates=gates,
        failures=(routed["failures"] if routed else []) + [
            {"stage": "legality", "cause": f["kind"], "refs": f["refs"]}
            for f in evaluation["legality"]["findings"] if not f["fixed"]],
        artifacts={"run_dir": str(run_dir), "placed_board": str(out_board), "gallery": str(page)},
        reference_evaluation=reference_evaluation if cold is not None or cluster_refs is not None else None,
    )
    run_report.write(rep, run_dir / "run_report.json")
    return rep


def _cluster_input(reference, refs: list[str]) -> tuple:
    """Limit movable parts to the requested cluster while keeping zone definitions and layer roles intact."""
    requested = set(refs)
    if not requested or requested - {fp.ref for fp in reference.footprints}:
        raise ValueError("cluster must contain existing footprint references")
    anchors = problem.fixed_parts(reference)
    if not requested - set(anchors):
        raise ValueError("cluster has no movable footprint")
    board = reference.copy()
    board.placement_anchors = {fp.ref: anchors.get(fp.ref, "outside requested cluster") for fp in reference.footprints
                               if fp.ref not in requested or fp.ref in anchors}
    input_fill = copy.deepcopy(reference.zone_fill_by_layer_mm2)
    board.routing, board.routing_items, board.teardrops = {}, 0, 0
    board.zone_fill_by_layer_mm2 = {}
    for zone in board.zones:
        zone["filled"] = False
        zone["filled_area_mm2"] = {layer: 0.0 for layer in zone.get("layers", [])}
    return board, {
        "requested_refs": sorted(requested, key=model.natural_key),
        "outside_anchors": len(board.placement_anchors),
        "zones_preserved": len(board.zones),
        "zones_require_refill": bool(board.zones),
        "input_zone_fill_by_layer_mm2": input_fill,
    }


