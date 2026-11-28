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
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Polygon

from .. import quality
from .. import report as run_report
from ..eval import congestion, legality
from ..eval.metrics import critical_loops_intent, crossings, regularity, rudy
from ..eval.score import DECAP_BAD_MM, DECAP_GOOD_MM, evaluate
from ..intent import model
from ..io.kicad import read_board, write_board
from ..render import Panel, gallery
from ..route.check import RouteCheckError, route_check
from ..toolchain import RUNTIME_DIR
from . import (
    analytical,
    blocks,
    decap,
    domains,
    escape,
    mechanical,
    problem,
    review,
    rigid,
)

SA_MOVES_PER_PART = 4000  # declared: annealing budget scales with movable parts ...
SA_MIN_MOVES = 200_000
SA_MAX_MOVES = 300_000  # ... within a fixed cap (deterministic, keeps S0-S5 under 5 minutes on video)
MODES = ("analytical", "sa_only")
W_CROSS = 2.0  # declared: cost of one pin-pair crossing in mm of wirelength
LEGALIZE_STEP_MM = 0.2
LEGALIZE_RETRIES = 2
LARGE_LEGALIZE_MOVABLE = 300
LARGE_LEGALIZE_RINGS = 80
MAX_CORRIDOR_TRIALS = 24
MAX_INFLATION_ROUNDS = 3
INFLATION_TIME_GUARD_S = 180.0  # reserve headroom for passive alignment within the 300s S0-S5 gate
NEAR_SATURATION_UTIL = 0.85  # trigger mild cell inflation on hotspots >= 85% utilization even when overflow == 0
PARALLEL_MIN_MOVABLE = 15  # skip Windows process-spawn overhead on tiny boards
PARALLEL_MAX_WORKERS = 5  # run up to 5 floorplan variants in 1 parallel wave on multi-core CPUs


def _solve_one(board, *, side_policy: str, mode: str, seed: int, intent: dict | None = None,
               floorplan: bool = False, reserve: bool = False, rigid_blocks: bool = False,
               budget_divisor: int = 1, _allow_domain_split: bool = True) -> tuple:
    """S1 to S5 on the IR with one global stage; returns (placed board, stage report)."""
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    pb = problem.build(board, side_policy=side_policy, intent=intent, floorplan=floorplan, reserve=reserve)
    if _allow_domain_split:
        doms = domains.decompose_isolated_domains(board, set(pb.fixed))
        if doms:
            merged = board.copy()
            dom_reports = []
            sum_total = 0.0
            sum_hpwl = 0.0
            sum_cross = 0.0
            for dom_idx, (reg_poly, dom_movable) in enumerate(doms):
                sub_board, sub_intent = domains.make_domain_subboard(
                    board, reg_poly, set(pb.fixed), dom_movable, intent=intent,
                )
                sub_placed, sub_info = _solve_one(
                    sub_board, side_policy=side_policy, mode=mode, seed=seed + dom_idx,
                    intent=sub_intent, floorplan=floorplan, reserve=reserve,
                    rigid_blocks=rigid_blocks, budget_divisor=max(1, budget_divisor),
                    _allow_domain_split=False,
                )
                for ref in dom_movable:
                    sp = sub_placed.fp(ref)
                    mp = merged.fp(ref)
                    mp.x, mp.y, mp.rotation, mp.side = sp.x, sp.y, sp.rotation, sp.side
                cf = sub_info.get("cost_final") or {}
                sum_total += float(cf.get("total", 0.0))
                sum_hpwl += float(cf.get("hpwl", 0.0))
                sum_cross += float(cf.get("crossings", 0.0))
                dom_reports.append({
                    "domain": dom_idx,
                    "area_mm2": round(float(reg_poly.area), 1),
                    "movable": len(dom_movable),
                    "s4_failed": sub_info.get("s4_failed", []),
                    "illegal_after_s5": sub_info.get("illegal_after_s5", []),
                    "strict_violations": sub_info.get("strict_violations", 0),
                    "cost_total": round(float(cf.get("total", 0.0)), 2),
                })
            findings = legality.check(merged, fixed=set(pb.fixed))
            violations = sum(not f.fixed for f in findings)
            cost_summary = {"total": round(sum_total, 4), "hpwl": round(sum_hpwl, 4), "crossings": round(sum_cross, 4)}
            info = {
                "mode": mode, "side_policy": side_policy, "seed": seed,
                "movable": sum(1 for p in pb.parts if not p[5]),
                "fixed": pb.fixed,
                "isolated_domains": dom_reports,
                "s4_failed": [r for dr in dom_reports for r in dr["s4_failed"]],
                "illegal_after_s5": [r for dr in dom_reports for r in dr["illegal_after_s5"]],
                "strict_violations": violations,
                "cost_after_s4": cost_summary,
                "cost_final": cost_summary,
                "timings": {"total_domains": round(time.monotonic() - t0, 2)},
                "seconds_s0_s5": round(time.monotonic() - t0, 2),
            }
            return merged, info
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
    diag = problem.board_diag(pb)
    w_cross = W_CROSS * max(1.0, min(2.5, diag / 90.0))
    core = problem.rust_problem(solver_pb, w_cross=w_cross)
    core.set_positions([pose if not part[5] else (part[7], part[8], part[9]) for pose, part in zip(start, solver_pb.parts,
                                                                                                strict=True)])
    t = time.monotonic()
    start_poses = core.positions()
    large = movable >= LARGE_LEGALIZE_MOVABLE
    legalize_step = max(LEGALIZE_STEP_MM, diag / LARGE_LEGALIZE_RINGS) if large else LEGALIZE_STEP_MM
    retry_step = max(LEGALIZE_STEP_MM, legalize_step * 0.75) if large else LEGALIZE_STEP_MM / 2
    failed = core.legalize(legalize_step, diag, priority)
    retries = 0
    while failed and retries < LEGALIZE_RETRIES:  # deterministic retry: the parts left over go first
        retries += 1
        if not large:
            core.set_positions(start_poses)
            step = retry_step
        else:
            # On large boards, keep successful coarse poses and use a finer step when only a few parts remain.
            step = max(LEGALIZE_STEP_MM, 0.4) if len(failed) <= 8 else retry_step
        failed = core.legalize(step, diag, sorted(set(failed)))
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
        placed, core, findings = _resolve_mask_aperture_collisions(
            placed, pb, solver_pb, core, retained, macros, diag, findings
        )
        placed, core, findings = _resolve_hv_clearances(
            placed, pb, solver_pb, core, retained, macros, findings
        )
        placed, core, findings = _resolve_silk_graphic_overlaps(
            placed, pb, solver_pb, core, retained, macros, findings
        )
    violations = sum(not finding.fixed for finding in findings)
    info = {
        "mode": mode, "side_policy": side_policy, "seed": seed, "movable": movable,
        "fixed": pb.fixed, "decap_affinities": len(pb.affinities) - pb.local_block_links,
        "local_block_links": pb.local_block_links, "s3": s3_info,
        "s4_failed": [solver_pb.refs[i] for i in failed], "s4_retries": retries,
        "s4_step_mm": legalize_step, "s4_retry_step_mm": retry_step,
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
    member_to_anchor = {p.ref: pb.refs[anchor] for anchor, m in (macros or {}).items() for p in m.parts}
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
        # Also block the full courtyard of any opposite-side part directly hit by a movable TH part's pads.
        hit_opp = {
            r for f in cur_th_findings
            if not f.fixed and f.detail.startswith("thru_hole_pads:")
            for r in f.refs if pb.sides.get(r) == opp
        }
        for r_opp in sorted(hit_opp):
            poly = legality.courtyard_polygon(cur_placed.fp(r_opp))
            if poly is not None and not poly.is_empty:
                grown = poly.buffer(base_margin)
                geoms = [grown] if grown.geom_type == "Polygon" else list(getattr(grown, "geoms", ()))
                for g in geoms:
                    if not g.is_empty:
                        holes.append([(round(x, 6), round(y, 6)) for x, y in list(g.exterior.coords)[:-1]])
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
    member_to_anchor = {p.ref: pb.refs[anchor] for anchor, m in (macros or {}).items() for p in m.parts}
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
    targets = sorted({member_to_anchor.get(r, r) for f in edge_findings for r in f.refs if r not in pb.fixed}, key=str)
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


def _resolve_mask_aperture_collisions(placed, pb, solver_pb, core, retained, macros, diag, findings):
    """Relocate movable footprints whose pads land on free-standing same-side solder-mask apertures."""
    mask_findings = [
        f for f in findings
        if not f.fixed and f.kind == "keepout" and f.detail.startswith("mask_aperture:")
    ]
    if not mask_findings:
        return placed, core, findings
    mask_obs = legality.board_mask_obstacles(placed, set(pb.fixed))
    if not mask_obs:
        return placed, core, findings
    idx_by_ref = {ref: idx for idx, ref in enumerate(solver_pb.refs)}
    member_to_anchor = {p.ref: pb.refs[anchor] for anchor, m in (macros or {}).items() for p in m.parts}
    for side in ("F", "B"):
        side_findings = [f for f in mask_findings if f.side == side]
        if not side_findings:
            continue
        targets = sorted({
            member_to_anchor.get(r, r) for f in side_findings for r in f.refs
            if r not in pb.fixed and pb.sides.get(r) == side
        })
        if not targets or not all(r in idx_by_ref for r in targets):
            continue
        side_holes = []
        for _uid, ms, poly in mask_obs:
            if ms != side:
                continue
            x0, y0, x1, y1 = poly.bounds
            m = 0.02
            side_holes.append([
                (round(x0 - m, 6), round(y0 - m, 6)),
                (round(x1 + m, 6), round(y0 - m, 6)),
                (round(x1 + m, 6), round(y1 + m, 6)),
                (round(x0 - m, 6), round(y1 + m, 6)),
            ])
        if not side_holes:
            continue
        poses_now = core.positions()
        target_idxs = [idx_by_ref[r] for r in targets]
        # Pass 1: relocate only offending parts; Pass 2 fallback: let same-side movable neighbors yield.
        resolved_poses = None
        for allow_neighbors in (False, True):
            if allow_neighbors:
                move_set = {
                    idx for idx, part in enumerate(solver_pb.parts)
                    if not part[5] and pb.sides.get(solver_pb.refs[idx]) == side
                }
            else:
                move_set = set(target_idxs)
            new_parts = [
                (*part[:5], idx not in move_set, part[6] if idx in move_set else False, *poses_now[idx])
                for idx, part in enumerate(solver_pb.parts)
            ]
            fix_pb = copy.copy(solver_pb)
            fix_pb.parts = new_parts
            fix_pb.holes = list(solver_pb.holes) + side_holes
            fix_core = problem.rust_problem(fix_pb, w_cross=W_CROSS)
            step = LEGALIZE_STEP_MM if allow_neighbors else LEGALIZE_STEP_MM / 2
            failed_m = fix_core.legalize(step, diag, target_idxs)
            if failed_m and allow_neighbors:
                fix_core.set_positions(poses_now)
                failed_m = fix_core.legalize(LEGALIZE_STEP_MM / 2, diag, sorted(set(target_idxs) | set(failed_m)))
            if not failed_m:
                if allow_neighbors:
                    fix_core.anneal(min(25_000, max(8_000, 400 * len(move_set))), 7, max(diag * 0.05, 0.5), 0.05)
                if not fix_core.illegal_parts():
                    resolved_poses = fix_core.positions()
                    break
        if resolved_poses is None:
            continue
        core.set_positions(resolved_poses)
        placed = problem.apply(pb, rigid.expand(pb, resolved_poses, retained, macros) if macros else resolved_poses)
        findings = legality.check(placed, fixed=set(pb.fixed))
        mask_findings = [
            f for f in findings
            if not f.fixed and f.kind == "keepout" and f.detail.startswith("mask_aperture:")
        ]
    return placed, core, findings


def _resolve_hv_clearances(placed, pb, solver_pb, core, retained, macros, findings):
    """Nudge any movable part that violates DRU/netclass HV pad-to-pad clearance against fixed or movable neighbors."""
    clr_guard = domains.make_clearance_guard(placed)
    if clr_guard is None or macros:
        return placed, core, findings
    idx_by_ref = {ref: idx for idx, ref in enumerate(solver_pb.refs)}
    dom_regions = domains.ref_regions(placed, set(pb.fixed))
    offenders = [
        fp for fp in placed.footprints
        if fp.ref not in pb.fixed and not fp.locked and fp.ref in idx_by_ref and not clr_guard(placed, (fp,))
    ]
    if not offenders:
        return placed, core, findings
    poses_now = list(core.positions())
    moved_any = False
    for fp in offenders:
        ox, oy, orot = fp.x, fp.y, fp.rotation
        idx = idx_by_ref[fp.ref]
        found = False
        for radius in (0.6, 1.1, 1.6, 2.2, 3.0, 4.0):
            for k in range(16):
                ang = (2.0 * math.pi * k) / 16.0
                nx = round(ox + radius * math.cos(ang), 4)
                ny = round(oy + radius * math.sin(ang), 4)
                fp.x, fp.y = nx, ny
                if (
                    domains.footprints_in_domains((fp,), dom_regions)
                    and clr_guard(placed, (fp,))
                    and not any(not f.fixed for f in legality.check(placed, fixed=set(pb.fixed)))
                ):
                    poses_now[idx] = (nx, ny, poses_now[idx][2])
                    found = True
                    moved_any = True
                    break
            if found:
                break
        if not found:
            fp.x, fp.y, fp.rotation = ox, oy, orot
    if moved_any:
        core.set_positions(poses_now)
        findings = legality.check(placed, fixed=set(pb.fixed))
    return placed, core, findings


def _resolve_silk_graphic_overlaps(placed, pb, solver_pb, core, retained, macros, findings):
    """Nudge any movable part whose own-side F.SilkS/B.SilkS graphics violate board silk_clearance."""
    silk_clr = float(placed.rules.get("silk_clearance") or 0.0)
    if silk_clr <= 0.0 or macros:
        return placed, core, findings
    idx_by_ref = {ref: idx for idx, ref in enumerate(solver_pb.refs)}
    dom_regions = domains.ref_regions(placed, set(pb.fixed))
    clr_guard = domains.make_clearance_guard(placed)
    offenders = [
        fp for fp in placed.footprints
        if fp.ref not in pb.fixed and not fp.locked and fp.ref in idx_by_ref
        and decap.silk_graphic_overlap(placed, fp, silk_clr)
    ]
    if not offenders:
        return placed, core, findings
    poses_now = list(core.positions())
    moved_any = False
    for fp in offenders:
        if not decap.silk_graphic_overlap(placed, fp, silk_clr):
            continue
        ox, oy, orot = fp.x, fp.y, fp.rotation
        idx = idx_by_ref[fp.ref]
        found = False
        for radius in (0.25, 0.45, 0.70, 1.0, 1.4, 2.0):
            for k in range(16):
                ang = (2.0 * math.pi * k) / 16.0
                nx = round(ox + radius * math.cos(ang), 4)
                ny = round(oy + radius * math.sin(ang), 4)
                fp.x, fp.y = nx, ny
                if (
                    not decap.silk_graphic_overlap(placed, fp, silk_clr)
                    and domains.footprints_in_domains((fp,), dom_regions)
                    and (clr_guard is None or clr_guard(placed, (fp,)))
                    and not any(not f.fixed for f in legality.check(placed, fixed=set(pb.fixed)))
                ):
                    poses_now[idx] = (nx, ny, poses_now[idx][2])
                    found = True
                    moved_any = True
                    break
            if found:
                break
        if not found:
            fp.x, fp.y, fp.rotation = ox, oy, orot
    if moved_any:
        core.set_positions(poses_now)
        findings = legality.check(placed, fixed=set(pb.fixed))
    return placed, core, findings


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
        _m, cand_board, info = item
        if intent:
            fm = _floorplan_metrics(cand_board, intent)
            return (*_legality_key(info), bool(rigid_blocks and not info.get("rigid_macros")),
                    -round(fm["placement_pro_score"], 2), info["cost_final"]["total"])
        return (*_legality_key(info), bool(rigid_blocks and not info.get("rigid_macros")), info["cost_final"]["total"])

    mode, placed, info = min(results, key=rank)
    info = dict(info)
    info["mode"] = f"best:{mode}"
    info["candidates"] = {m: {"cost": i["cost_final"], "illegal": i["illegal_after_s5"], "s4_failed": i["s4_failed"],
                              "seconds": i["seconds_s0_s5"]} for m, _p, i in results}
    info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    return placed, info


def _run_floorplan_candidate(args: tuple) -> tuple[str, object, dict]:
    """Worker for parallel P7-deterministic floorplan candidate evaluation (GIL released in Rust)."""
    name, board, side_policy, mode, seed, intent, use_blocks, reserve, rigid_blocks, budget_divisor = args
    placed, info = _solve(
        board.copy(),
        side_policy=side_policy,
        mode=mode,
        seed=seed,
        intent=intent,
        floorplan=use_blocks,
        reserve=reserve,
        rigid_blocks=rigid_blocks,
        budget_divisor=budget_divisor,
    )
    return name, placed, info


_ORIGINAL_SOLVE = _solve


def solve(board, *, side_policy: str = "expert", mode: str = "best", seed: int = 0,
          intent: dict | None = None, floorplan: bool = False, parallel: bool = True,
          restarts: int | None = None) -> tuple:
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
    fixed_guess = problem.fixed_parts(board, intent)
    movable_est = sum(1 for fp in board.footprints if fp.ref not in fixed_guess and not fp.locked)
    # Full 100% S3/S5 iteration budget for normal/medium boards (< 175 movable parts).
    # For large (>= 175 movable) boards, drop redundant variants (keep baseline + blocks + combined/rigid)
    # and run at divisor=2 (2x deeper SA than legacy divisor=4) while staying within the 300s S0-S5 budget.
    if len(modes) >= 3 and movable_est >= 175:
        keep_names = {"blocks", "combined"} if selected else {"blocks", "rigid"}
        modes = [m for m in modes if m[0] in keep_names]
        divisor = 4 if movable_est >= 300 else 2
    else:
        divisor = 1
    budget = {"budget_divisor": divisor}

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

        cand, ref_report = decap.refine(cand, intent, refine_fixed, max_trials=480, preserve_escapes=preserve)
        if ref_report["moved"]:
            cand_audit = measure(cand)
            cand_info["cost_final_stage"] = "before_decap_refinement"
            ref_report["output_metrics"] = _floorplan_metrics(cand, intent)
        return cand, cand_audit, ref_report

    eff_restarts = (
        restarts
        if restarts is not None
        else (2 if (use_floorplan and movable_est < 175 and _solve is _ORIGINAL_SOLVE) else 1)
    )
    specs = [("baseline", False, False, False, seed)] + [
        (name, use_blocks, reserve, name == "rigid", seed) for name, use_blocks, reserve in modes
    ]
    extra_mode_names: list[tuple[str, bool, bool]] = []
    if use_floorplan and eff_restarts >= 2:
        seed2 = seed + 17
        specs.append(("baseline_s2", False, False, False, seed2))
        extra_mode_names.append(("baseline_s2", False, False))
        specs.append(("blocks_s2", True, False, False, seed2))
        extra_mode_names.append(("blocks_s2", True, False))
        if selected:
            specs.append(("combined_s2", True, True, False, seed2))
            extra_mode_names.append(("combined_s2", True, True))
    workers = 1
    use_pool = (
        parallel
        and len(specs) > 1
        and movable_est >= PARALLEL_MIN_MOVABLE
        and _solve is _ORIGINAL_SOLVE
        and (os.cpu_count() or 1) > 1
    )
    solved_by_name: dict[str, tuple] = {}
    if use_pool:
        workers = min(len(specs), os.cpu_count() or 1, max(PARALLEL_MAX_WORKERS, min(len(specs), 7)))
        payloads = [
            (name, board, side_policy, mode, s_seed, intent, use_blocks, reserve, is_rigid, divisor)
            for name, use_blocks, reserve, is_rigid, s_seed in specs
        ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, cand_b, cand_i in pool.map(_run_floorplan_candidate, payloads):
                solved_by_name[name] = (cand_b, cand_i)
    if not solved_by_name:
        for name, use_blocks, reserve, is_rigid, s_seed in specs:
            if name.startswith("baseline"):
                solved_by_name[name] = _solve(
                    board, side_policy=side_policy, mode=mode, seed=s_seed, intent=intent,
                    floorplan=False, **budget,
                )
            else:
                solved_by_name[name] = _solve(
                    board, side_policy=side_policy, mode=mode, seed=s_seed, intent=intent,
                    floorplan=use_blocks, reserve=reserve, rigid_blocks=is_rigid, **budget,
                )

    placed, info = solved_by_name["baseline"]
    variant = "baseline"
    audit = measure(placed)
    comparisons = {}
    tail_refinement = None
    if use_floorplan:
        placed, audit, tail_refinement = refine_decap(placed, info, audit)
        baseline, baseline_info = placed, info
        base_metrics = _floorplan_metrics(baseline, intent)
        rank = _floorplan_rank(baseline_info, base_metrics, audit)
        for name, _use_blocks, _reserve in list(modes) + extra_mode_names:
            other, other_info = solved_by_name[name]
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
    reservations_used = variant in ("reserve", "combined", "combined_s2")
    congestion_refinement = None
    passive_alignment = None
    rigid_macros = info.get("rigid_macros") or {}
    if use_floorplan and not rigid_macros.get("corridors", {}).get("total") and not any(_legality_key(info)):
        placed, audit, congestion_refinement = _refine_congestion(
            placed, info, audit, intent=intent, variant=variant,
            reservations_used=reservations_used, seed=seed, measure=measure, t0=t0,
        )
        if congestion_refinement and congestion_refinement["accepted_rounds"] > 0:
            info.setdefault("cost_final_stage", "before_congestion_refinement")
        rigid_members = {r for group in rigid_macros.get("members", []) for r in group}
        refine_fixed = set(info["fixed"]) | rigid_members
        blocked = blocked_exits(audit)
        # Precompute world-space exit corridor bounding boxes so align_passives only runs escape.audit
        # when a moved passive's old or new box actually enters an IC's escape zone.
        from ..geometry import transform as _xf

        flipped_ic = {fp.ref for fp in placed.footprints if fp.side != board.fp(fp.ref).side}
        exit_boxes = []
        for ic_ref, row in exits.items():
            ic_fp = placed.fp(ic_ref)
            if not row:
                continue
            mirror = -1.0 if ic_ref in flipped_ic else 1.0
            pts = [_xf((pt[0], pt[1] * mirror), ic_fp.position, ic_fp.rotation) for e in row for pt in (e.start, e.end)]
            pad_m = max(max(e.width, e.via_space) for e in row) + 0.5
            exit_boxes.append((
                min(p[0] for p in pts) - pad_m,
                min(p[1] for p in pts) - pad_m,
                max(p[0] for p in pts) + pad_m,
                max(p[1] for p in pts) + pad_m,
                ic_fp.side,
            ))

        def preserve_align(board_state, moved_boxes=None):
            if moved_boxes is not None and exit_boxes:
                touches_exit = False
                for mx0, my0, mx1, my1, mside in moved_boxes:
                    for ex0, ey0, ex1, ey1, eside in exit_boxes:
                        if mside == eside and mx0 <= ex1 and mx1 >= ex0 and my0 <= ey1 and my1 >= ey0:
                            touches_exit = True
                            break
                    if touches_exit:
                        break
                if not touches_exit:
                    return True
            return blocked_exits(measure(board_state)) <= blocked

        placed, passive_alignment = decap.align_passives(
            placed, intent, refine_fixed, preserve_escapes=preserve_align,
            max_trials=max(400, 3 * movable_est), extend=True,
        )
        if passive_alignment["moved"]:
            audit = measure(placed)
            blocked = blocked_exits(audit)
            pol_cand, pol_decap = decap.refine(placed, intent, refine_fixed, preserve_escapes=preserve_align)
            if pol_decap["moved"]:
                pol_cand, pol_align = decap.align_passives(
                    pol_cand, intent, refine_fixed, preserve_escapes=preserve_align,
                    max_trials=200, extend=True, seed_moved=set(pol_decap["moved"]),
                )
                pol_metrics = _floorplan_metrics(pol_cand, intent)
                cur_metrics = _floorplan_metrics(placed, intent)
                if (pol_metrics["crossings"] <= cur_metrics["crossings"]
                        and (pol_metrics["decap_median_mm"] or 0.0) <= (cur_metrics["decap_median_mm"] or math.inf)
                        and (pol_metrics["all_decap_p90_mm"] or 0.0) <= (cur_metrics["all_decap_p90_mm"] or math.inf)):
                    placed = pol_cand
                    audit = measure(placed)
                    passive_alignment["post_decap_polish"] = {
                        "decap_moved": pol_decap["moved"],
                        "align_moved": pol_align["moved"],
                    }
            info.setdefault("cost_final_stage", "before_passive_alignment")
            passive_alignment["output_metrics"] = _floorplan_metrics(placed, intent)
    info["pathfinder"] = congestion.summary(placed)
    info["congestion_refinement"] = congestion_refinement
    info["passive_alignment"] = passive_alignment
    info["floorplan"] = {"requested": use_floorplan, "reservations_used": reservations_used,
                         "reserved_pad_count": sum(map(len, selected.values())) if reservations_used else 0,
                         "selected_variant": variant, "quality_guard": comparisons.get(variant),
                         "comparisons": comparisons, "escape": audit, "iteration_budget_divisor": divisor,
                         "parallel_workers": workers,
                         "decap_refinement": tail_refinement, "congestion_refinement": congestion_refinement,
                         "passive_alignment": passive_alignment}
    for row in anchors:
        ref = row["ref"]
        placed.fp(ref).locked = original.fp(ref).locked
        info["fixed"][ref] = f"mechanical constraint ({row['mode']})"
    info["mechanical_anchors"] = anchors
    info["seconds_s0_s5"] = round(time.monotonic() - t0, 2)
    return placed, info


def _legality_key(info: dict) -> tuple:
    return info["strict_violations"], len(info["illegal_after_s5"]), len(info["s4_failed"])


def _refine_congestion(placed, info: dict, audit: dict, *, intent: dict | None, variant: str,
                       reservations_used: bool, seed: int, measure, t0: float) -> tuple:
    """F5 guarded Cell Inflation loop (up to 3 rounds) driven by Rust PathFinder overflow or near-saturation."""
    cur_pf = congestion.estimate(placed)
    report: dict = {
        "available": bool(cur_pf.get("available")),
        "initial": congestion.public_summary(cur_pf),
        "rounds": [],
        "accepted_rounds": 0,
    }
    has_overflow = cur_pf.get("total_overflow", 0.0) > 1e-6
    near_sat = cur_pf.get("peak_utilization", 0.0) >= NEAR_SATURATION_UTIL
    if (not cur_pf.get("available") or (not has_overflow and not near_sat)
            or time.monotonic() - t0 >= INFLATION_TIME_GUARD_S):
        report["final"] = report["initial"]
        return placed, audit, report

    rigid_members = {r for group in (info.get("rigid_macros") or {}).get("members", []) for r in group}
    fixed_map = {**info["fixed"], **{r: "rigid macro member" for r in rigid_members}}
    fixed_set = set(fixed_map)
    dom_regions = domains.ref_regions(placed, set(info["fixed"]))
    cur_placed, cur_audit = placed, audit
    cur_metrics = _floorplan_metrics(cur_placed, intent) if intent else {"crossings": crossings(cur_placed)["count"]}

    for round_idx in range(MAX_INFLATION_ROUNDS):
        cur_overflow = cur_pf.get("total_overflow", 0.0) > 1e-6
        cur_near_sat = cur_pf.get("peak_utilization", 0.0) >= NEAR_SATURATION_UTIL
        if (not cur_overflow and not cur_near_sat) or time.monotonic() - t0 >= INFLATION_TIME_GUARD_S:
            break
        if cur_overflow:
            infl = congestion.part_inflations(cur_placed, cur_pf, fixed_set, scale_mm=0.2 * (round_idx + 1))
            mode_tag = "overflow"
        else:
            probe_pf = congestion.estimate(cur_placed, capacity_scale=NEAR_SATURATION_UTIL)
            infl = congestion.part_inflations(
                cur_placed, probe_pf, fixed_set, max_inflate_mm=0.25, scale_mm=0.12 * (round_idx + 1)
            )
            mode_tag = "near_saturation"
        if not infl:
            break
        inf_pb = problem.build(
            cur_placed, side_policy="expert", intent=intent,
            floorplan=(variant in ("blocks", "combined")), reserve=reservations_used,
            inflations=infl, fixed_override=fixed_map,
        )
        inf_core = problem.rust_problem(inf_pb, w_cross=W_CROSS)
        start_poses = inf_pb.poses()
        inf_core.set_positions(start_poses)
        diag = problem.board_diag(inf_pb)
        prio = sorted((i for i, ref in enumerate(inf_pb.refs) if ref in infl),
                      key=lambda i: (-infl[inf_pb.refs[i]], i))
        failed = inf_core.legalize(LEGALIZE_STEP_MM, diag, prio)
        if failed:
            inf_core.set_positions(start_poses)
            failed = inf_core.legalize(LEGALIZE_STEP_MM / 2, diag, sorted(set(failed)))
        if failed:
            report["rounds"].append({"round": round_idx + 1, "inflated_parts": len(infl),
                                     "accepted": False, "reason": "legalize_failed"})
            break
        iters = min(60_000, max(20_000, 1000 * len(infl)))
        inf_core.anneal(iters, seed + 101 * (round_idx + 1), max(diag * 0.08, 0.8), 0.05)
        if inf_core.illegal_parts():
            report["rounds"].append({"round": round_idx + 1, "inflated_parts": len(infl),
                                     "accepted": False, "reason": "illegal_after_anneal"})
            continue
        cand = problem.apply(inf_pb, inf_core.positions())
        findings = legality.check(cand, fixed=set(info["fixed"]))
        th_findings = [f for f in findings if not f.fixed and f.detail.startswith("thru_hole_pads:")]
        if th_findings and "F" in inf_pb.sides.values() and "B" in inf_pb.sides.values():
            cand, inf_core, findings = _resolve_movable_th_collisions(
                cand, inf_pb, inf_pb, inf_core, list(range(len(inf_pb.parts))), {}, diag, th_findings, findings
            )
        cand, inf_core, findings = _resolve_edge_copper_clearances(
            cand, inf_pb, inf_pb, inf_core, list(range(len(inf_pb.parts))), {}, diag, findings
        )
        cand, inf_core, findings = _resolve_mask_aperture_collisions(
            cand, inf_pb, inf_pb, inf_core, list(range(len(inf_pb.parts))), {}, diag, findings
        )
        if any(not f.fixed for f in findings) or not domains.footprints_in_domains(cand.footprints, dom_regions):
            report["rounds"].append({"round": round_idx + 1, "inflated_parts": len(infl),
                                     "accepted": False, "reason": "legality"})
            continue
        cand_audit = measure(cand)
        if cand_audit["clear"] < cur_audit["clear"]:
            report["rounds"].append({"round": round_idx + 1, "inflated_parts": len(infl),
                                     "accepted": False, "reason": "blocked_escape"})
            continue
        if intent:
            cand_metrics = _floorplan_metrics(cand, intent)
            if ((cand_metrics["decap_median_mm"] or 0.0) > (cur_metrics["decap_median_mm"] or math.inf) + 1e-3
                    or (cand_metrics["all_decap_p90_mm"] or 0.0) > (cur_metrics["all_decap_p90_mm"] or math.inf) + 1e-3):
                min_clear = cur_audit["clear"]

                def _pres(bs, _mc=min_clear):
                    return measure(bs)["clear"] >= _mc

                cand, _ = decap.refine(cand, intent, fixed_set, preserve_escapes=_pres)
                cand_audit = measure(cand)
                if any(not f.fixed for f in legality.check(cand, fixed=set(info["fixed"]))) or (
                    cand_audit["clear"] < cur_audit["clear"]
                ):
                    report["rounds"].append({"round": round_idx + 1, "inflated_parts": len(infl),
                                             "accepted": False, "reason": "decap_legality"})
                    continue
        cand_pf = congestion.estimate(cand)
        cand_metrics = _floorplan_metrics(cand, intent) if intent else {"crossings": crossings(cand)["count"]}
        if cur_overflow:
            improved = (
                cand_pf["total_overflow"] < cur_pf["total_overflow"] - 1e-3
                or (cand_pf["total_overflow"] <= cur_pf["total_overflow"] + 1e-6
                    and cand_pf["routability_cost"] < cur_pf["routability_cost"] * 0.99)
            )
        else:
            improved = cand_pf["total_overflow"] <= 1e-6 and (
                cand_pf["peak_utilization"] < cur_pf["peak_utilization"] - 0.02
                or cand_pf["routability_cost"] < cur_pf["routability_cost"] * 0.99
            )
        big_relief = cur_pf["total_overflow"] > 1e-6 and cand_pf["total_overflow"] <= cur_pf["total_overflow"] * 0.75
        xing_tol = max(4, int(0.04 * cur_metrics["crossings"])) if big_relief else 0
        no_regress = cand_metrics["crossings"] <= cur_metrics["crossings"] + xing_tol
        if intent and no_regress:
            no_regress = (
                (cand_metrics["decap_median_mm"] or 0.0) <= (cur_metrics["decap_median_mm"] or math.inf) + 1e-3
                and (cand_metrics["all_decap_p90_mm"] or 0.0) <= (cur_metrics["all_decap_p90_mm"] or math.inf) + 1e-3
                and cand_metrics["resolved_decaps"] >= cur_metrics["resolved_decaps"]
            )
        entry = {
            "round": round_idx + 1,
            "mode": mode_tag,
            "inflated_parts": len(infl),
            "overflow_before": cur_pf["total_overflow"],
            "overflow_after": cand_pf["total_overflow"],
            "peak_util_before": cur_pf["peak_utilization"],
            "peak_util_after": cand_pf["peak_utilization"],
            "rcost_before": cur_pf["routability_cost"],
            "rcost_after": cand_pf["routability_cost"],
            "accepted": bool(improved and no_regress),
        }
        if improved and no_regress:
            cur_placed, cur_pf, cur_audit, cur_metrics = (
                cand, cand_pf, cand_audit, cand_metrics
            )
            report["accepted_rounds"] += 1
        else:
            entry["reason"] = "no_improvement" if not improved else "metric_regression"
        report["rounds"].append(entry)

    report["final"] = congestion.public_summary(cur_pf)
    return cur_placed, cur_audit, report


def _esd_max_connector_mm(board, intent: dict | None) -> float | None:
    parts = (intent or {}).get("parts") or {}
    by_ref = {fp.ref: fp for fp in board.footprints}
    worst: float | None = None
    for ref, meta in parts.items():
        rc = meta.get("role")
        tc = meta.get("target")
        if not rc or model.is_uncertain(rc) or rc.get("value") != "esd" or not tc or model.is_uncertain(tc):
            continue
        conn_ref = (tc.get("value") or {}).get("ref")
        if ref not in by_ref or not conn_ref or conn_ref not in by_ref:
            continue
        esd_fp, conn_fp = by_ref[ref], by_ref[conn_ref]
        shared = (esd_fp.nets & conn_fp.nets)
        for net in shared:
            sp = [pt for p, pt in esd_fp.pads_world() if p.net == net]
            cp = [pt for p, pt in conn_fp.pads_world() if p.net == net]
            if sp and cp:
                d = min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in sp for b in cp)
                worst = d if worst is None else max(worst, d)
    return round(worst, 3) if worst is not None else None


def _floorplan_metrics(board, intent: dict) -> dict:
    loops = critical_loops_intent(board, intent)
    distance = loops["resolved_decap_pin_distance"]
    xings = crossings(board)
    reg = regularity(board)
    rudy_m = rudy(board)
    reg_score = (
        100.0 * (0.5 * reg["aligned_fraction"] + 0.3 * reg["rotation_on_90_grid"] + 0.2 * reg["dominant_axis_fraction"])
        if reg.get("n") else None
    )
    ratsnest_score = 100.0 / (1.0 + 4.0 * xings["per_edge"])
    d_all = loops["decap_pin_distance"]
    loop_score = (
        max(0.0, min(100.0, 100.0 * (DECAP_BAD_MM - d_all["median_mm"]) / (DECAP_BAD_MM - DECAP_GOOD_MM)))
        if d_all.get("n") else None
    )
    rout_score = (100.0 * (1.0 - rudy_m["over_capacity_fraction"])) if rudy_m.get("available") else None
    weighted_terms = [
        (rout_score, 35.0),
        (loop_score, 25.0),
        (ratsnest_score, 15.0),
        (reg_score, 15.0),
    ]
    w_sum = sum(w for v, w in weighted_terms if v is not None)
    pro_est = round(sum(v * w for v, w in weighted_terms if v is not None) / w_sum, 2) if w_sum > 0 else 0.0
    return {
        "crossings": xings["count"],
        "decap_p90_mm": distance.get("p90_mm"),
        "resolved_decaps": distance["n"],
        "all_decap_p90_mm": d_all.get("p90_mm"),
        "decap_median_mm": d_all.get("median_mm"),
        "regularity_score": round(reg_score, 2) if reg_score is not None else None,
        "placement_pro_score": pro_est,
        "esd_max_mm": _esd_max_connector_mm(board, intent),
    }


def _floorplan_rank(info: dict, metrics: dict, audit: dict) -> tuple:
    tail = metrics["decap_p90_mm"]
    med = metrics.get("decap_median_mm")
    decap_bucket = round(max(0.0, (med or 0.0) - 3.5), 1)
    esd_pen = 1 if (metrics.get("esd_max_mm") or 0.0) > 10.0 else 0
    pro = float(metrics.get("placement_pro_score") or 0.0)
    # Primary ranker: legality -> ESD safety -> decap median gate -> unified Pro Score v1 -> escape clarity -> crossings.
    return (
        *_legality_key(info),
        esd_pen,
        decap_bucket,
        -round(pro, 2),
        -audit["clear"],
        metrics["crossings"],
        tail if tail is not None else math.inf,
    )


def _compare_floorplans(candidate, candidate_info, baseline, baseline_info, intent: dict, *, base_metrics=None) -> dict:
    """Prefer the baseline when an equally legal trial worsens measured objectives without a Pro Score gain."""
    trial = _floorplan_metrics(candidate, intent)
    base = base_metrics if base_metrics is not None else _floorplan_metrics(baseline, intent)
    big_xing_win = (
        base.get("crossings") is not None
        and trial.get("crossings") is not None
        and base["crossings"] > 0
        and trial["crossings"] <= int(base["crossings"] * 0.95)
    )
    big_decap_win = (
        base.get("decap_median_mm") is not None
        and trial.get("decap_median_mm") is not None
        and base["decap_median_mm"] - trial["decap_median_mm"] >= 1.5
        and (trial.get("all_decap_p90_mm") or 0.0) <= (base.get("all_decap_p90_mm") or math.inf) - 1.5
    )
    pro_win = (
        float(trial.get("placement_pro_score") or 0.0) >= float(base.get("placement_pro_score") or 0.0) + 1.0
        and (trial.get("decap_median_mm") or 0.0) <= max((base.get("decap_median_mm") or 0.0) + 0.25, 2.2)
        and (trial.get("all_decap_p90_mm") or 0.0) <= max((base.get("all_decap_p90_mm") or 0.0) + 1.2, 3.6)
    )
    p90_tol = 1.2 if pro_win else (0.15 if big_xing_win else 0.0)
    xing_tol = max(3, int(base["crossings"] * 0.25)) if big_decap_win and base.get("crossings") else (2 if pro_win else 0)
    regressions = []
    for key in ("crossings", "decap_p90_mm", "all_decap_p90_mm"):
        bval = base.get(key)
        tval = trial.get(key)
        if bval is None:
            continue
        tol = xing_tol if key == "crossings" else p90_tol
        if tval is None or tval > bval + tol:
            regressions.append(key)
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
                cluster_refs: list[str] | None = None, s6a: bool = True,
                preserve_routing: str = "none", silkscreen_policy: str = "auto",
                fab_profile: str = "ipc_standard", keep_placement: bool = False,
                restarts: int | None = None, monotonic: bool = False) -> dict:
    """Inputs that decide the output (P7): the board, the intent (file or netlist draft), the seed, the config."""
    from .. import silkscreen as silk_mod
    from ..intent.resolve import resolve
    from ..intent.resolve import side_policy as policy_of

    if preserve_routing not in ("none", "outside_cluster"):
        raise ValueError(f"unsupported preserve_routing mode: {preserve_routing!r}")
    if preserve_routing == "outside_cluster" and cluster_refs is None:
        raise ValueError("preserve_routing='outside_cluster' is only valid with cluster_refs")
    if keep_placement and (cluster_refs is not None or schematic_path is not None):
        raise ValueError("keep_placement=True cannot be combined with cluster_refs or schematic_path")
    if silkscreen_policy not in silk_mod.SILKSCREEN_POLICIES:
        raise ValueError(f"unsupported silkscreen_policy: {silkscreen_policy!r}")
    if fab_profile not in silk_mod.FAB_PROFILES:
        raise ValueError(f"unsupported fab_profile: {fab_profile!r}")
    src = Path(board_path).resolve()
    reference = read_board(src)
    source_identity = quality.identity(src) if reference.source.get("eda") == "kicad" else None
    board, cold = reference, None
    if cluster_refs is not None and schematic_path is not None:
        raise ValueError("cluster placement requires the existing layout (no schematic_path)")
    if cluster_refs is not None and route and preserve_routing != "outside_cluster":
        raise ValueError("cluster placement requires route=False unless preserve_routing='outside_cluster'")
    if schematic_path is not None:
        from ..io.schematic_input import cold_input

        declared = resolve(reference, intent_path)[0] if intent_path else None
        board, cold = cold_input(reference, Path(schematic_path).resolve(strict=True), intent=declared)
    intent, intent_info = resolve(board, intent_path)
    side_policy = policy_of(intent, side_policy)
    if cluster_refs is not None:
        if any(ref not in cluster_refs and claim["value"]["mode"] == "region"
               for ref, claim in intent.get("mechanical", {}).items()):
            raise ValueError("cluster intent cannot relocate an outside footprint")
        board, cluster_meta = _cluster_input(reference, cluster_refs, intent)
        # Cluster placement is a local ECO; all components stay on their current side.
        side_policy = "expert"
    else:
        cluster_meta = None
    outside = set(board.placement_anchors) if cluster_refs is not None else set()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "place" / f"{board.name}-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if keep_placement:
        placed = board.copy()
        fixed_map = problem.fixed_parts(placed, intent)
        boxes = {fp.ref: problem.local_box(fp) for fp in placed.footprints}
        info = {
            "mode": "keep_placement",
            "fixed": fixed_map,
            "movable": 0,
            "seconds_s0_s5": 0.0,
            "strict_violations": sum(not f.fixed for f in legality.check(placed, fixed=set(fixed_map))),
            "illegal_after_s5": [],
            "s4_failed": [],
            "mechanical_anchors": [],
            "floorplan": {
                "selected_variant": "keep_placement",
                "quality_guard": None,
                "reservations_used": False,
                "reserved_pad_count": 0,
                "escape": escape.audit(placed, escape.envelopes(placed, intent, boxes)),
            },
        }
    else:
        solve_kwargs: dict = {
            "side_policy": side_policy, "mode": mode, "seed": seed, "intent": intent,
            "floorplan": floorplan,
        }
        if restarts is not None:
            solve_kwargs["restarts"] = restarts
        placed, info = solve(board, **solve_kwargs)
        if monotonic and cold is None and cluster_refs is None and intent:
            ref_fixed = set(info["fixed"])
            ref_viols = sum(not f.fixed for f in legality.check(board, fixed=ref_fixed))
            if ref_viols == 0 and info["strict_violations"] == 0:
                ref_m = _floorplan_metrics(board, intent)
                cand_m = _floorplan_metrics(placed, intent)
                if float(ref_m["placement_pro_score"]) > float(cand_m["placement_pro_score"]) + 0.5:
                    placed = board.copy()
                    info["floorplan"]["selected_variant"] = "monotonic_reference"
                    info["monotonic_guard"] = {
                        "kept_reference": True,
                        "reference_pro_score": ref_m["placement_pro_score"],
                        "candidate_pro_score": cand_m["placement_pro_score"],
                    }
                else:
                    info["monotonic_guard"] = {
                        "kept_reference": False,
                        "reference_pro_score": ref_m["placement_pro_score"],
                        "candidate_pro_score": cand_m["placement_pro_score"],
                    }
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
    write_stats: dict = {}
    eco_preserve = cluster_refs is not None and preserve_routing == "outside_cluster"
    from ..route import dru as dru_mod

    dru_rules = dru_mod.extract_effective_rules(src.with_suffix(".kicad_dru"))
    silk_cfg = silk_mod.build_config(
        placed, intent, policy=silkscreen_policy, fab_profile=fab_profile, dru_rules=dru_rules,
    )
    def write_trial(candidate, target):
        stats: dict = {}
        cfg = silk_mod.build_config(candidate, intent, policy=silkscreen_policy,
                                    fab_profile=fab_profile, dru_rules=dru_rules)
        path = write_board(src, candidate, target, strip_routing=not eco_preserve, strip_zones=cold is not None,
                           unfill_zones=not eco_preserve, preserve_outside_routing=eco_preserve,
                           clean_silkscreen=silkscreen_policy != "off", silkscreen_config=cfg, result_out=stats)
        return path, stats

    out_board, write_stats = write_trial(placed, run_dir / f"{board.name}.placed.kicad_pcb")
    reviewer = placement_review = None
    time_gate_s = max(300, int(1.5 * int(info.get("movable") or 0)))
    if reference.source.get("eda") == "kicad":
        reviewer = quality.PlacementReview(reference, intent, info["fixed"], info["mechanical_anchors"],
                                           cold=cold is not None, fab_profile=fab_profile)
        out_board, placed, placement_review = review.run(
            out_board, reviewer, run_dir / "review", write_trial, allow_reference=False,
        )
        write_stats = placement_review.pop("write_stats", write_stats)
        if len(placement_review["rounds"]) > 1:
            silk_cfg = silk_mod.build_config(placed, intent, policy=silkscreen_policy,
                                              fab_profile=fab_profile, dru_rules=dru_rules)
            boxes = {fp.ref: problem.local_box(fp) for fp in placed.footprints}
            info["floorplan"]["escape"] = escape.audit(placed, escape.envelopes(placed, intent, boxes))
            repairs = [row["action"] for row in placement_review["rounds"][1:] if row["accepted"]]
            if repairs:
                info["cost_final_stage"] = repairs[-1]
        info["zones_require_refill"] = placement_review["zones_require_refill"]
        if info["seconds_s0_s5"] > time_gate_s and placement_review["status"] == "approved":
            placement_review["status"] = "failed"
            placement_review["reasons"] = ["placement_time_budget"]
    silk_raw = write_stats.get("silkscreen")
    if isinstance(silk_raw, dict):
        post_texts = silk_raw.pop("post_texts", [])
        silk_audit = silk_mod.audit_texts(
            post_texts, fab_profile=fab_profile, rules=placed.rules, board=placed, dru_rules=dru_rules,
        )
        info["silkscreen"] = {
            **silk_raw,
            "density": silk_cfg["density"],
            "audit": silk_audit,
        }
    else:
        info["silkscreen"] = {"policy": silkscreen_policy, "skipped": True}
    if eco_preserve:
        info["preserve_routing"] = {
            "mode": preserve_routing,
            "preserved_items": write_stats.get("preserved_routing"),
            "removed_items": write_stats.get("removed_routing"),
            "moved_footprints": write_stats.get("moved_footprints"),
            "unfilled_zones": write_stats.get("unfilled_zones"),
        }
    def run_route_trial(artifact, layout, cfg, root):
        import json

        from ..io.kicad import KiCadAdapterError, lock_routing_copy, run_helper
        from ..route import s6a as s6a_mod

        trial_silk = {}
        stage = None
        route_input = artifact

        def finish_legend(routed_pcb):
            if not routed_pcb.is_file():
                return
            with routed_pcb.open("rb") as artifact_file:
                if not artifact_file.read(32).lstrip().startswith(b"(kicad_pcb"):
                    return
            pr_cfg = root / "post_route_silk_cfg.json"
            pr_out = root / "post_route_silk_out.json"
            pr_cfg.write_text(json.dumps(cfg), encoding="utf-8")
            run_helper("clean_silkscreen", routed_pcb, pr_cfg, routed_pcb, pr_out)
            result = json.loads(pr_out.read_text(encoding="utf-8"))
            if isinstance(result, dict) and not result.get("skipped"):
                texts = result.pop("post_texts", [])
                trial_silk.update(result, post_route_via_fixup=True, density=cfg["density"],
                                  audit=silk_mod.audit_texts(texts, fab_profile=fab_profile, rules=layout.rules,
                                                             board=layout, dru_rules=dru_rules))

        root.mkdir(parents=True, exist_ok=True)
        try:
            if eco_preserve:
                route_input = lock_routing_copy(artifact, root / f"{board.name}.eco_locked.kicad_pcb")
            elif s6a:
                stage = s6a_mod.apply_to_board(artifact, intent, root / "s6a")
                if stage["applied"] and stage["s6a_board"]:
                    route_input = Path(stage["s6a_board"])
            result = route_check(route_input, out_dir=root / "route", reference_board=src,
                                 router_timeout_s=router_timeout_s, intent=intent,
                                 **({"postprocess": finish_legend} if reviewer and silkscreen_policy != "off" else {}))
            routed_path = Path(result["artifacts"]["routed_board"])
            if reviewer is None and silkscreen_policy != "off":
                finish_legend(routed_path)
            saved = read_board(routed_path, **({"include_fill": True} if reviewer else {}))
            summary = None
            if stage is not None:
                summary = {k: v for k, v in stage.items() if k not in ("live_taps", "report")}
                if stage["applied"] and stage["live_taps"]:
                    summary["tap_fill_after_ses"] = s6a_mod.verify_taps_after_route(routed_path, stage["live_taps"])
            return result, saved, summary, trial_silk, None
        except (RouteCheckError, KiCadAdapterError) as exc:
            return None, None, None, {}, str(exc)

    routed = routed_board = s6a_summary = route_problem = None
    route_root = run_dir
    placement_blocks_routing = route and placement_review is not None and placement_review["status"] != "approved"
    if route and not placement_blocks_routing:
        routed, routed_board, s6a_summary, routed_silk, route_problem = run_route_trial(out_board, placed, silk_cfg, route_root)
        if routed_silk:
            info["silkscreen"] = routed_silk
    fixed = set(info["fixed"])
    evaluation = evaluate(placed, routed["metrics"] if routed else None, fixed, intent, copper_board=routed_board)
    reference_for_score = reference
    if cold is not None or cluster_refs is not None:
        reference_for_score = board.copy()
        for fp in reference_for_score.footprints:
            source = reference.fp(fp.ref)
            fp.x, fp.y, fp.rotation, fp.side, fp.locked = (
                source.x, source.y, source.rotation, source.side, source.locked
            )
    reference_evaluation = evaluate(reference_for_score, fixed=fixed, intent=intent)
    if placement_blocks_routing:
        routing_review = {"status": "blocked", "reasons": ["placement_not_approved"]}
    else:
        routing_review = {"status": "failed" if route_problem else "not_requested",
                          "reasons": [route_problem] if route_problem else []}
    if routed and reviewer and routed_board is not None:
        path = Path(routed["artifacts"]["routed_board"])
        routing_review = review.routed_evidence(path, routed_board, reviewer, routed, placement_review,
                                                run_dir / "review" / "routed",
                                                taps=(s6a_summary or {}).get("tap_fill_after_ses"))
    page = gallery([Panel(f"{board.name} (reference)", reference_for_score, reference_evaluation),
                    Panel(f"{board.name} (placer v2, {mode})",
                          routed_board if routed_board is not None else placed, evaluation,
                          routed["metrics"] if routed else None)],
                   run_dir / "gallery.html", title=f"{board.name}: placer v2 vs expert")
    gates = [run_report.gate("legality_violations", evaluation["legality"]["strict_violations"], 0, "=="),
             run_report.gate("s0_s5_seconds", info["seconds_s0_s5"], time_gate_s, "<=")]
    if routed:
        gates += [g for g in routed["gates"] if g["name"] != "time_budget_s"]
    if placement_review is not None:
        gates[0] = run_report.gate("legality_violations",
                                   sum(f["kind"] == "new_placement_violation" for f in placement_review["findings"]), 0)
        gates.append(run_report.gate("placement_review_approved", placement_review["status"], "approved"))
        if route:
            gates.append(run_report.gate("routing_review_approved", routing_review["status"], "approved"))
    artifacts = {"run_dir": str(run_dir), "placed_board": str(out_board), "gallery": str(page)}
    if s6a_summary and s6a_summary.get("s6a_board"):
        artifacts["s6a_board"] = s6a_summary["s6a_board"]
    if routed and reviewer:
        artifacts["routed_board"] = routed["artifacts"]["routed_board"]
    rep = run_report.build(
        kind="place_cluster" if cluster_refs is not None else "place_board",
        inputs={"board": board.source, "intent": intent_info, **({"board_artifact": source_identity} if source_identity else {})},
        config={"side_policy": side_policy, "mode": info["mode"], "seed": seed, "w_cross": problem.WEIGHT_SIGNAL * W_CROSS,
                "sa_moves_per_part": SA_MOVES_PER_PART, "floorplan": floorplan, "cluster_refs": cluster_refs,
                "s6a": s6a, "preserve_routing": preserve_routing, "keep_placement": keep_placement,
                "router_timeout_s": router_timeout_s,
                "silkscreen_policy": silkscreen_policy, "fab_profile": fab_profile},
        stages={"place": info, "s6a": s6a_summary,
                "route_check": {"run_report": str(route_root / "route" / "run_report.json")} if routed else None},
        metrics={**evaluation["metrics"], "pro_score": evaluation["pro_score"], "fanout_escape": info["floorplan"]["escape"],
                 "route": routed["metrics"] if routed else None},
        gates=gates,
        failures=(routed["failures"] if routed else []) + [
            {"stage": "legality", "cause": f["kind"], "refs": f["refs"]}
            for f in evaluation["legality"]["findings"] if not f["fixed"]],
        artifacts=artifacts,
        **({"quality": {"placement": placement_review, "routing": routing_review}} if reviewer else {}),
        reference_evaluation=reference_evaluation if cold is not None or cluster_refs is not None else None,
    )
    if source_identity is not None and source_identity != quality.identity(src):
        raise RuntimeError("input board or project rules changed during placement review")
    run_report.write(rep, run_dir / "run_report.json")
    return rep


def _cluster_input(reference, refs: list[str], intent: dict | None = None) -> tuple:
    """Limit movable parts to the requested cluster while keeping zone definitions and layer roles intact."""
    requested = set(refs)
    if not requested or requested - {fp.ref for fp in reference.footprints}:
        raise ValueError("cluster must contain existing footprint references")
    anchors = problem.fixed_parts(reference, intent)
    regional = {ref for ref, row in (intent or {}).get("mechanical", {}).items()
                if row["value"]["mode"] == "region" and ref in requested and not reference.fp(ref).locked}
    if not (requested - set(anchors) or regional):
        raise ValueError("cluster has no movable footprint")
    board = reference.copy()
    board.placement_anchors = {fp.ref: anchors.get(fp.ref, "outside requested cluster") for fp in reference.footprints
                               if fp.ref not in requested or (fp.ref in anchors and fp.ref not in regional)}
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


