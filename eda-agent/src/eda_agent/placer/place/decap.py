# SPDX-License-Identifier: MIT
"""Local placement refinement for the longest resolved supply-pad links."""

from __future__ import annotations

import math
from collections.abc import Callable

from ..eval import legality, metrics
from ..geometry import transform
from ..ir import Board


def _tail(distances: dict[str, float]) -> float:
    count = math.ceil(len(distances) / 10)
    return math.fsum(sorted(distances.values(), reverse=True)[:count])


def _measure(board: Board, intent: dict) -> tuple[dict[str, float], dict]:
    distances: dict[str, float] = {}
    stats = metrics.critical_loops_intent(board, intent, resolved_by_ref=distances)["resolved_decap_pin_distance"]
    return distances, stats


def refine(board: Board, intent: dict, fixed: set[str], *, max_trials: int = 32,
           preserve_escapes: Callable[[Board], bool] | None = None) -> tuple[Board, dict]:
    """Try relocating tail capacitors near their declared supply pin, preserving legality and crossings."""
    distances, before = _measure(board, intent)
    report = {"before": before, "after": before, "trials": 0, "moved": []}
    if not distances:
        return board, report
    result = board.copy()
    cross = metrics.crossings(board)["count"]
    trials, moved = 0, []
    count = math.ceil(len(distances) / 10)
    worst = [ref for ref in sorted(distances, key=lambda ref: (-distances[ref], ref))[:count] if ref not in fixed]
    for ref in worst:
        fp = result.fp(ref)
        target = intent["parts"][ref]["target"]["value"]
        ic = result.fp(target["ref"])
        numbers = set(target.get("pins", []))
        eligible = [(p, point) for p, point in ic.pads_world() if p.net and (not target.get("net") or p.net == target["net"])
                    and (not numbers or p.number in numbers)]
        shape = legality.courtyard_polygon(ic)
        if shape is None or legality.courtyard_polygon(fp) is None:
            continue
        original = fp.x, fp.y, fp.rotation
        candidates: dict[tuple[float, float, int], float] = {}
        clearance = max(board.rules.get("courtyard_clearance", 0), 0.05)
        left, top, right, bottom = shape.bounds
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
                        candidates[pose] = min(distance, candidates.get(pose, math.inf))
        # Limit effort per part so one crowded target cannot consume the whole budget.
        for pose in sorted(candidates, key=lambda pose: (candidates[pose], pose))[:8]:
            if trials >= max_trials:
                break
            trials += 1
            fp.x, fp.y, fp.rotation = pose
            trial, stats = _measure(result, intent)
            old_stats = report["after"]
            if (trial.keys() == distances.keys() and trial[ref] < distances[ref]
                    and all(stats[k] <= old_stats[k] for k in ("median_mm", "p90_mm", "max_mm"))
                    and _tail(trial) < _tail(distances)
                    and not any(not f.fixed for f in legality.check(result, fixed=fixed))):
                next_cross = metrics.crossings(result)["count"]
                if next_cross <= cross and (preserve_escapes is None or preserve_escapes(result)):
                    distances, cross = trial, next_cross
                    report["after"] = stats
                    moved.append(ref)
                    break
            fp.x, fp.y, fp.rotation = original
        if trials >= max_trials:
            break
    report.update(trials=trials, moved=moved)
    return (result if moved else board), report
