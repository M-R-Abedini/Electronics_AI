# SPDX-License-Identifier: MIT
"""Local placement refinement for the longest resolved supply-pad links."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable

from ..eval import legality, metrics
from ..geometry import transform
from ..intent import model
from ..ir import Board


def _tail(distances: dict[str, float]) -> float:
    if not distances:
        return 0.0
    count = math.ceil(len(distances) / 10)
    return math.fsum(sorted(distances.values(), reverse=True)[:count])


def _candidate_distances(board: Board, intent: dict, resolved: dict[str, float]) -> dict[str, tuple[float, str, list[str]]]:
    by_ref = {fp.ref: fp for fp in board.footprints}
    out: dict[str, tuple[float, str, list[str]]] = {}
    for ref, part in sorted((intent or {}).get("parts", {}).items()):
        if ref in resolved or ref not in by_ref:
            continue
        role, claim = part.get("role"), part.get("target")
        if model.is_uncertain(role) or role.get("value") != "decap" or not claim:
            continue
        target = claim.get("value") or {}
        net, target_ref = target.get("net"), target.get("ref")
        candidates = [c for c in (target.get("candidates") or ()) if c != ref and c in by_ref]
        if target_ref or not net or not candidates or float(claim.get("confidence", 0.0)) <= 0:
            continue
        sp = [pt for p, pt in by_ref[ref].pads_world() if p.net == net]
        tp = [pt for c in candidates for p, pt in by_ref[c].pads_world() if p.net == net]
        if sp and tp:
            out[ref] = (min(math.dist(a, b) for a in sp for b in tp), net, candidates)
    return out


def _measure(board: Board, intent: dict) -> tuple[dict[str, float], dict[str, tuple[float, str, list[str]]], dict, dict]:
    distances: dict[str, float] = {}
    cl = metrics.critical_loops_intent(board, intent, resolved_by_ref=distances)
    cands = _candidate_distances(board, intent, distances)
    return distances, cands, cl["resolved_decap_pin_distance"], cl["decap_pin_distance"]


def _poses_for_targets(result: Board, fp, target_refs: list[str], req_net: str | None, numbers: set[str],
                       clearance: float) -> list[tuple[float, float, int]]:
    per_ic: list[list[tuple[float, tuple[float, float, int]]]] = []
    for ic_ref in target_refs:
        ic = result.fp(ic_ref)
        shape = legality.courtyard_polygon(ic)
        if shape is None:
            continue
        eligible = [(p, point) for p, point in ic.pads_world() if p.net and (not req_net or p.net == req_net)
                    and (not numbers or p.number in numbers)]
        if not eligible:
            continue
        left, top, right, bottom = shape.bounds
        ic_cands: dict[tuple[float, float, int], float] = {}
        for pad in fp.pads:
            if not pad.net:
                continue
            for rotation in (0, 90, 180, 270):
                lx, ly, rx, ry = fp.box(0.0, 0.0, rotation)
                px, py = transform(pad.local, (0, 0), rotation)
                for pin, (tx, ty) in eligible:
                    if pad.net != pin.net:
                        continue
                    for x, y in ((left - rx - clearance, ty - py), (right - lx + clearance, ty - py),
                                 (tx - px, top - ry - clearance), (tx - px, bottom - ly + clearance)):
                        pose = round(x, 4), round(y, 4), rotation
                        distance = math.dist(transform(pad.local, pose[:2], rotation), (tx, ty))
                        ic_cands[pose] = min(distance, ic_cands.get(pose, math.inf))
        if ic_cands:
            limit = 8 if len(target_refs) == 1 else 4
            per_ic.append(sorted(((d, pose) for pose, d in ic_cands.items()), key=lambda t: (t[0], t[1]))[:limit])
    # Interleave across candidate ICs so one crowded IC cannot starve alternatives.
    poses: list[tuple[float, float, int]] = []
    seen: set[tuple[float, float, int]] = set()
    for slot in range(8):
        for ranked in per_ic:
            if slot < len(ranked):
                pose = ranked[slot][1]
                if pose not in seen:
                    seen.add(pose)
                    poses.append(pose)
    return poses[:12] if len(target_refs) > 1 else poses[:8]


def refine(board: Board, intent: dict, fixed: set[str], *, max_trials: int = 32,
           preserve_escapes: Callable[[Board], bool] | None = None) -> tuple[Board, dict]:
    """Try relocating tail capacitors (resolved and candidate-target) near supply pins cleanly."""
    distances, cand_dists, before, before_all = _measure(board, intent)
    report = {"before": before, "after": before, "before_all": before_all, "after_all": before_all,
              "trials": 0, "moved": []}
    if not distances and not cand_dists:
        return board, report
    result = board.copy()
    cross = metrics.crossings(board)["count"]
    trials, moved = 0, []
    clearance = max(board.rules.get("courtyard_clearance", 0), 0.05)
    all_dists = {**distances, **{r: info[0] for r, info in cand_dists.items()}}
    count_res = math.ceil(len(distances) / 10) if distances else 0
    worst: list[tuple[str, bool]] = [
        (ref, False) for ref in sorted(distances, key=lambda r: (-distances[r], r))[:count_res] if ref not in fixed
    ]
    count_cand = max(math.ceil(len(cand_dists) / 10), 4) if cand_dists else 0
    for ref in sorted(cand_dists, key=lambda r: (-cand_dists[r][0], r)):
        if ref not in fixed and cand_dists[ref][0] > 1.5 and len(worst) - count_res < count_cand:
            worst.append((ref, True))
    for ref, is_cand in worst:
        fp = result.fp(ref)
        if legality.courtyard_polygon(fp) is None:
            continue
        if is_cand:
            _, req_net, target_refs = cand_dists[ref]
            numbers: set[str] = set()
        else:
            target = intent["parts"][ref]["target"]["value"]
            target_refs = [target["ref"]]
            req_net = target.get("net")
            numbers = set(target.get("pins", []))
        original = fp.x, fp.y, fp.rotation
        for pose in _poses_for_targets(result, fp, target_refs, req_net, numbers, clearance):
            if trials >= max_trials:
                break
            trials += 1
            fp.x, fp.y, fp.rotation = pose
            trial, trial_cand, stats, stats_all = _measure(result, intent)
            trial_all = {**trial, **{r: info[0] for r, info in trial_cand.items()}}
            old_stats, old_all = report["after"], report["after_all"]
            if is_cand:
                improved = (ref in trial_cand and trial_cand[ref][0] < cand_dists[ref][0]
                            and (not distances or (trial.keys() == distances.keys()
                                                   and all(stats[k] <= old_stats[k] for k in ("median_mm", "p90_mm", "max_mm"))
                                                   and _tail(trial) <= _tail(distances)))
                            and all(stats_all[k] <= old_all[k] for k in ("median_mm", "p90_mm", "max_mm"))
                            and (_tail(trial_all) < _tail(all_dists)
                                 or math.fsum(trial_all.values()) < math.fsum(all_dists.values()) - 1e-6))
            else:
                improved = (trial.keys() == distances.keys() and trial[ref] < distances[ref]
                            and all(stats[k] <= old_stats[k] for k in ("median_mm", "p90_mm", "max_mm"))
                            and _tail(trial) < _tail(distances))
            if improved and not any(not f.fixed for f in legality.check(result, fixed=fixed)):
                next_cross = metrics.crossings(result)["count"]
                if next_cross <= cross and (preserve_escapes is None or preserve_escapes(result)):
                    distances, cand_dists, all_dists, cross = trial, trial_cand, trial_all, next_cross
                    report["after"], report["after_all"] = stats, stats_all
                    moved.append(ref)
                    break
            fp.x, fp.y, fp.rotation = original
        if trials >= max_trials:
            break
    report.update(trials=trials, moved=moved)
    return (result if moved else board), report


MAX_ALIGN_SHIFT_MM = 1.5


def _passive_prefix(ref: str) -> str:
    chars: list[str] = []
    for ch in ref:
        if ch.isalpha():
            chars.append(ch.upper())
        else:
            break
    return "".join(chars)


def _decap_ok(board: Board, intent: dict | None, has_res: bool, has_all: bool,
              res_ref: dict, all_ref: dict) -> tuple[bool, dict, dict]:
    if not has_res and not has_all:
        return True, res_ref, all_ref
    t_res, t_cand, rstats, astats = _measure(board, intent or {})
    if has_res and any(rstats.get(k, 0.0) > res_ref[k] + 1e-9 for k in ("median_mm", "p90_mm", "max_mm")):
        return False, res_ref, all_ref
    if has_all and any(astats.get(k, 0.0) > all_ref[k] + 1e-9 for k in ("median_mm", "p90_mm", "max_mm")):
        return False, res_ref, all_ref
    return True, (rstats if t_res else res_ref), (astats if (t_res or t_cand) else all_ref)


def align_passives(board: Board, intent: dict | None, fixed: set[str], *,
                   preserve_escapes: Callable[[Board], bool] | None = None,
                   max_trials: int = 48) -> tuple[Board, dict]:
    """Snap unaligned same-footprint, same-side passives within 5 mm onto a peer's X or Y line."""
    before = metrics.regularity(board)
    report = {"before": before, "after": before, "trials": 0, "moved": []}
    if before["n"] < 2 or before["aligned_fraction"] >= 1.0:
        return board, report

    result = board.copy()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for fp in result.footprints:
        if _passive_prefix(fp.ref) in metrics.PASSIVE_PREFIXES:
            groups[(fp.side, fp.lib_id)].append(fp)

    aligned_init: set[str] = set()
    raw_cands: list[tuple[int, float, str, str, str]] = []
    for grp in groups.values():
        if len(grp) < 2:
            continue
        for a in grp:
            for b in grp:
                if a is b:
                    continue
                dx, dy = abs(a.x - b.x), abs(a.y - b.y)
                if math.hypot(dx, dy) <= metrics.ALIGN_RADIUS_MM and (
                        dx <= metrics.ALIGN_TOL_MM or dy <= metrics.ALIGN_TOL_MM):
                    aligned_init.add(a.ref)
                    break
        for a in grp:
            if a.ref in fixed or a.locked:
                continue
            for b in grp:
                if a is b:
                    continue
                dx, dy = abs(a.x - b.x), abs(a.y - b.y)
                if math.hypot(dx, dy) > metrics.ALIGN_RADIUS_MM:
                    continue
                if dx <= metrics.ALIGN_TOL_MM or dy <= metrics.ALIGN_TOL_MM:
                    continue
                if dx <= MAX_ALIGN_SHIFT_MM:
                    raw_cands.append((0 if a.ref not in aligned_init else 1, round(dx, 6), a.ref, b.ref, "x"))
                if dy <= MAX_ALIGN_SHIFT_MM:
                    raw_cands.append((0 if a.ref not in aligned_init else 1, round(dy, 6), a.ref, b.ref, "y"))

    if not raw_cands:
        return board, report

    raw_cands.sort()
    res_d, cand_d, before_res, before_all = _measure(result, intent or {})
    has_res, has_all = bool(res_d), bool(res_d or cand_d)
    t0 = time.monotonic()
    cross = metrics.crossings(result)["count"]
    slow_cross = (time.monotonic() - t0) >= 0.1

    best_reg, cur_res, cur_all = before, before_res, before_all
    trials = 0
    moved: list[str] = []
    moved_set: set[str] = set()
    failed_per_ref: dict[str, int] = defaultdict(int)
    wave_items: list[tuple[str, float, float, str, str]] = []

    def _try_legal_and_decap(ref: str, axis: str, other_ref: str) -> tuple[bool, bool, float, float, dict, dict, dict]:
        nonlocal trials
        fp, other = result.fp(ref), result.fp(other_ref)
        ox, oy = fp.x, fp.y
        nx, ny = (round(other.x, 4), oy) if axis == "x" else (ox, round(other.y, 4))
        dx_now, dy_now = abs(ox - other.x), abs(oy - other.y)
        if dx_now <= metrics.ALIGN_TOL_MM or dy_now <= metrics.ALIGN_TOL_MM:
            return False, False, ox, oy, best_reg, cur_res, cur_all
        if (abs(nx - ox) + abs(ny - oy) > MAX_ALIGN_SHIFT_MM
                or math.hypot(nx - other.x, ny - other.y) > metrics.ALIGN_RADIUS_MM):
            return False, False, ox, oy, best_reg, cur_res, cur_all
        fp.x, fp.y = nx, ny
        t_reg = metrics.regularity(result)
        if (t_reg["aligned_fraction"] <= best_reg["aligned_fraction"] + 1e-9
                or t_reg["rotation_on_90_grid"] < best_reg["rotation_on_90_grid"] - 1e-9):
            fp.x, fp.y = ox, oy
            return False, False, ox, oy, best_reg, cur_res, cur_all
        ok_decap, n_res, n_all = _decap_ok(result, intent, has_res, has_all, cur_res, cur_all)
        if not ok_decap:
            fp.x, fp.y = ox, oy
            return False, True, ox, oy, best_reg, cur_res, cur_all
        if trials >= max_trials:
            fp.x, fp.y = ox, oy
            return False, False, ox, oy, best_reg, cur_res, cur_all
        trials += 1
        if any(not f.fixed for f in legality.check(result, fixed=fixed)) or (
                preserve_escapes is not None and not preserve_escapes(result)):
            fp.x, fp.y = ox, oy
            return False, True, ox, oy, best_reg, cur_res, cur_all
        return True, True, ox, oy, t_reg, n_res, n_all

    def _flush_batch() -> None:
        nonlocal best_reg, cur_res, cur_all, cross, wave_items
        if not wave_items:
            return
        next_cross = metrics.crossings(result)["count"]
        if next_cross <= cross:
            cross = next_cross
            for r, *_ in wave_items:
                moved.append(r)
            wave_items = []
            return
        snap = list(wave_items)
        for r, ox, oy, _, _ in reversed(snap):
            fp = result.fp(r)
            fp.x, fp.y = ox, oy
            moved_set.discard(r)
        best_reg = metrics.regularity(result)
        if has_res or has_all:
            _, _, cur_res, cur_all = _measure(result, intent or {})
        wave_items = []
        for r, _, _, axis, oref in snap:
            ok, _, ox, oy, t_reg, n_res, n_all = _try_legal_and_decap(r, axis, oref)
            if not ok:
                continue
            c = metrics.crossings(result)["count"]
            if c <= cross:
                cross, best_reg, cur_res, cur_all = c, t_reg, n_res, n_all
                moved.append(r)
                moved_set.add(r)
            else:
                result.fp(r).x, result.fp(r).y = ox, oy

    for _, _, ref, other_ref, axis in raw_cands:
        if trials >= max_trials:
            break
        if ref in moved_set or failed_per_ref[ref] >= 2:
            continue
        ok, tried, ox, oy, t_reg, n_res, n_all = _try_legal_and_decap(ref, axis, other_ref)
        if not ok:
            if tried:
                failed_per_ref[ref] += 1
            continue
        if slow_cross:
            best_reg, cur_res, cur_all = t_reg, n_res, n_all
            moved_set.add(ref)
            wave_items.append((ref, ox, oy, axis, other_ref))
            if len(wave_items) >= 6:
                _flush_batch()
        else:
            next_cross = metrics.crossings(result)["count"]
            if next_cross <= cross:
                cross, best_reg, cur_res, cur_all = next_cross, t_reg, n_res, n_all
                moved.append(ref)
                moved_set.add(ref)
            else:
                result.fp(ref).x, result.fp(ref).y = ox, oy
                failed_per_ref[ref] += 1

    if slow_cross:
        _flush_batch()

    report.update(after=metrics.regularity(result), trials=trials, moved=moved)
    return (result if moved else board), report
