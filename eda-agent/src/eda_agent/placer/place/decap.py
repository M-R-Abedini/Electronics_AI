# SPDX-License-Identifier: MIT
"""Local placement refinement for decoupling capacitors and passive regularity."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace

from shapely import STRtree
from shapely.geometry import LineString

from ..eval import legality, metrics
from ..geometry import transform
from ..intent import model
from ..ir import Board, Box, Footprint


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


class CrossingTracker:
    """Incremental signal-net MST crossing oracle matching ``metrics.crossings`` exactly."""

    def __init__(self, board: Board) -> None:
        self.board = board
        self.planes = metrics.zone_nets(board)
        self.net_fps: dict[str, list[tuple]] = {}
        for fp in board.footprints:
            for p in fp.pads:
                if p.net and p.net not in self.planes:
                    self.net_fps.setdefault(p.net, []).append((fp, p))
        self.edges_by_net = {net: self._compute_net_edges(net) for net in self.net_fps}
        self._index = None
        self.total = metrics.crossings(board)["count"]

    def _compute_net_edges(self, net: str) -> list[LineString]:
        pts = [fp.pad_world(p) for fp, p in self.net_fps.get(net, ())]
        return [LineString([a, b]) for a, b in metrics.mst_edges(pts) if a != b]

    def _count_nets_vs_all(self, edges_map: dict[str, list[LineString]]) -> int:
        lines, owners = [], []
        for net, edges in edges_map.items():
            lines.extend(edges)
            owners.extend([net] * len(edges))
        if not lines:
            return 0
        if self._index is None:
            all_lines, all_owners = [], []
            for net, edges in self.edges_by_net.items():
                all_lines.extend(edges)
                all_owners.extend([net] * len(edges))
            self._index = STRtree(all_lines), all_owners
        tree, all_owners = self._index
        external = sum(1 for j in tree.query(lines, predicate="crosses")[1] if all_owners[j] not in edges_map)
        internal = 0
        if len(edges_map) > 1:
            internal = sum(1 for i, j in zip(*STRtree(lines).query(lines, predicate="crosses"))
                           if i < j and owners[i] != owners[j])
        return external + internal

    def prepare(self, moved_fps: tuple) -> tuple[Callable[[], tuple[int, dict]], Callable[[int, dict], None]]:
        """Prepare query/commit closures for the nets touched by ``moved_fps``."""
        touched = {p.net for fp in moved_fps for p in fp.pads
                   if p.net and p.net not in self.planes and p.net in self.edges_by_net}
        base_other = None

        def query() -> tuple[int, dict]:
            nonlocal base_other
            if not touched:
                return self.total, {}
            if base_other is None:
                base_other = self.total - self._count_nets_vs_all({n: self.edges_by_net[n] for n in touched})
            new_map = {n: self._compute_net_edges(n) for n in touched}
            return base_other + self._count_nets_vs_all(new_map), new_map

        def commit(new_count: int, new_map: dict) -> None:
            self.total = new_count
            if any(new_map.values()) or any(self.edges_by_net[n] for n in new_map):
                self._index = None
            self.edges_by_net.update(new_map)

        return query, commit


def _find_blockers(board: Board, fp: Footprint, outline_bounds: Box | None,
                   box: Callable[[Footprint], Box] = Footprint.box) -> list | None:
    """Return same-side AABB blockers for ``fp``, or None if outside ``outline_bounds``."""
    ax0, ay0, ax1, ay1 = box(fp)
    if outline_bounds is not None:
        bx0, by0, bx1, by1 = outline_bounds
        if ax0 < bx0 or ay0 < by0 or ax1 > bx1 or ay1 > by1:
            return None
    out = []
    for other in board.footprints:
        if other is fp or other.side != fp.side:
            continue
        ox0, oy0, ox1, oy1 = box(other)
        if max(ax0, ox0) < min(ax1, ox1) - 0.01 and max(ay0, oy0) < min(ay1, oy1) - 0.01:
            out.append(other)
    return out


def _local_legal(board: Board, moved_fps: tuple, fixed: set[str],
                 outline_bounds: tuple[float, float, float, float] | None) -> bool:
    """Fast local courtyard/TH/edge check within 3 mm of ``moved_fps`` before full legality."""
    for fp in moved_fps:
        if _find_blockers(board, fp, outline_bounds) != []:
            return False
    moved_refs = {f.ref for f in moved_fps}
    boxes = [f.box() for f in moved_fps]
    x0 = min(bx[0] for bx in boxes) - 3.0
    y0 = min(bx[1] for bx in boxes) - 3.0
    x1 = max(bx[2] for bx in boxes) + 3.0
    y1 = max(bx[3] for bx in boxes) + 3.0
    sub_fps = [f for f in board.footprints if f.ref in moved_refs or not (
        f.box()[2] < x0 or f.box()[0] > x1 or f.box()[3] < y0 or f.box()[1] > y1)]
    sub = replace(board, footprints=sub_fps)
    return not any(not f.fixed for f in legality.check(sub, fixed=fixed))


def _body_center_world(fp) -> tuple[float, float]:
    lx, ly, rx, ry = fp.box(0.0, 0.0, fp.rotation)
    return fp.x + 0.5 * (lx + rx), fp.y + 0.5 * (ly + ry)


def _set_body_center_pose(fp, cx: float, cy: float, rot: float) -> None:
    lx, ly, rx, ry = fp.box(0.0, 0.0, rot)
    fp.x = round(cx - 0.5 * (lx + rx), 4)
    fp.y = round(cy - 0.5 * (ly + ry), 4)
    fp.rotation = rot % 360


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
                    for gap in (clearance, clearance + 0.4, clearance + 0.8):
                        for shift in (0.0, -0.8, 0.8, -1.6, 1.6, -2.4, 2.4):
                            for x, y in ((left - rx - gap, ty - py + shift),
                                         (right - lx + gap, ty - py + shift),
                                         (tx - px + shift, top - ry - gap),
                                         (tx - px + shift, bottom - ly + gap)):
                                pose = round(x, 4), round(y, 4), rotation
                                distance = math.dist(transform(pad.local, pose[:2], rotation), (tx, ty))
                                ic_cands[pose] = min(distance, ic_cands.get(pose, math.inf))
                    px0, py0 = fp.pad_world(pad)
                    vx, vy = tx - px0, ty - py0
                    if math.hypot(vx, vy) > 1.2:
                        for frac in (0.35, 0.7):
                            pose = round(tx - vx * (1.0 - frac) - px, 4), round(ty - vy * (1.0 - frac) - py, 4), rotation
                            distance = math.dist(transform(pad.local, pose[:2], rotation), (tx, ty))
                            ic_cands[pose] = min(distance, ic_cands.get(pose, math.inf))
        if ic_cands:
            limit = 16 if len(target_refs) == 1 else 8
            per_ic.append(sorted(((d, pose) for pose, d in ic_cands.items()), key=lambda t: (t[0], t[1]))[:limit])
    # Interleave across candidate ICs so one crowded IC cannot starve alternatives.
    poses: list[tuple[float, float, int]] = []
    seen: set[tuple[float, float, int]] = set()
    for slot in range(16):
        for ranked in per_ic:
            if slot < len(ranked):
                pose = ranked[slot][1]
                if pose not in seen:
                    seen.add(pose)
                    poses.append(pose)
    return poses[:24] if len(target_refs) > 1 else poses[:20]


def _touches_same_side_neighbor(board: Board, fp) -> bool:
    shape = legality.courtyard_polygon(fp)
    if shape is None:
        return False
    mx0, my0, mx1, my1 = shape.bounds
    for other in board.footprints:
        if other is fp or other.side != fp.side:
            continue
        ox0, oy0, ox1, oy1 = other.box()
        if mx0 - 0.02 > ox1 or mx1 + 0.02 < ox0 or my0 - 0.02 > oy1 or my1 + 0.02 < oy0:
            continue
        other_shape = legality.courtyard_polygon(other)
        if other_shape is not None and shape.distance(other_shape) < 0.01 - 1e-9:
            return True
    return False


def refine(board: Board, intent: dict, fixed: set[str], *, max_trials: int = 240,
           preserve_escapes: Callable[[Board], bool] | None = None) -> tuple[Board, dict]:
    """Try relocating tail capacitors (resolved and candidate-target) near supply pins cleanly."""
    distances, cand_dists, before, before_all = _measure(board, intent)
    report = {"before": before, "after": before, "before_all": before_all, "after_all": before_all,
              "trials": 0, "moved": []}
    if not distances and not cand_dists:
        return board, report
    result = board.copy()
    tracker = CrossingTracker(result)
    outline = legality.board_polygon(result)
    outline_bounds = outline.bounds if outline is not None else None
    trials, moved = 0, []
    clearance = max(board.rules.get("courtyard_clearance", 0), 0.05)
    all_dists = {**distances, **{r: info[0] for r, info in cand_dists.items()}}
    worst: list[tuple[str, bool]] = [
        (ref, False) for ref in sorted(distances, key=lambda r: (-distances[r], r))
        if ref not in fixed and distances[ref] > 1.2
    ]
    for ref in sorted(cand_dists, key=lambda r: (-cand_dists[r][0], r)):
        if ref not in fixed and cand_dists[ref][0] > 1.2:
            worst.append((ref, True))

    def _stats_improved(trial: dict, trial_cand: dict, stats: dict, stats_all: dict,
                        ref: str, is_cand: bool, old_stats: dict, old_all: dict) -> bool:
        trial_all = {**trial, **{r: info[0] for r, info in trial_cand.items()}}
        if is_cand:
            return (ref in trial_cand and trial_cand[ref][0] < cand_dists[ref][0]
                    and (not distances or (trial.keys() == distances.keys()
                                           and all(stats[k] <= old_stats[k] for k in ("median_mm", "p90_mm", "max_mm"))
                                           and _tail(trial) <= _tail(distances)))
                    and all(stats_all[k] <= old_all[k] for k in ("median_mm", "p90_mm", "max_mm"))
                    and (_tail(trial_all) < _tail(all_dists)
                         or math.fsum(trial_all.values()) < math.fsum(all_dists.values()) - 1e-6))
        return (
            trial.keys() == distances.keys()
            and trial[ref] < distances[ref]
            and all(stats[k] <= old_stats[k] for k in ("median_mm", "p90_mm", "max_mm"))
            and all(stats_all[k] <= old_all[k] for k in ("median_mm", "p90_mm", "max_mm"))
            and (_tail(trial) < _tail(distances)
                 or (_tail(trial) <= _tail(distances)
                     and math.fsum(trial.values()) < math.fsum(distances.values()) - 1e-6))
        )

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
        ocx, ocy = _body_center_world(fp)
        query_one, commit_one = tracker.prepare((fp,))
        accepted = False
        for pose in _poses_for_targets(result, fp, target_refs, req_net, numbers, clearance):
            if trials >= max_trials:
                break
            trials += 1
            fp.x, fp.y, fp.rotation = pose
            trial, trial_cand, stats, stats_all = _measure(result, intent)
            old_stats, old_all = report["after"], report["after_all"]
            if not _stats_improved(trial, trial_cand, stats, stats_all, ref, is_cand, old_stats, old_all):
                continue
            blockers = _find_blockers(result, fp, outline_bounds)
            if blockers is None:
                continue
            if len(blockers) == 0:
                next_cross, nmap = query_one()
                if next_cross > tracker.total:
                    continue
                if (_touches_same_side_neighbor(result, fp)
                        or not _local_legal(result, (fp,), fixed, outline_bounds)
                        or (preserve_escapes is not None and not preserve_escapes(result))
                        or any(not f.fixed for f in legality.check(result, fixed=fixed))):
                    continue
                distances, cand_dists = trial, trial_cand
                all_dists = {**trial, **{r: info[0] for r, info in trial_cand.items()}}
                commit_one(next_cross, nmap)
                report["after"], report["after_all"] = stats, stats_all
                moved.append(ref)
                accepted = True
                break
            elif len(blockers) == 1 and blockers[0].ref not in fixed and not blockers[0].locked:
                other = blockers[0]
                other_orig = other.x, other.y, other.rotation
                query_two, commit_two = tracker.prepare((fp, other))
                for rot_o in (other_orig[2], (other_orig[2] + 90) % 360,
                              (other_orig[2] + 180) % 360, (other_orig[2] + 270) % 360):
                    _set_body_center_pose(other, ocx, ocy, rot_o)
                    if _find_blockers(result, fp, outline_bounds) != [] or _find_blockers(result, other, outline_bounds) != []:
                        continue
                    t2, tc2, s2, sa2 = _measure(result, intent)
                    if not _stats_improved(t2, tc2, s2, sa2, ref, is_cand, old_stats, old_all):
                        continue
                    nc2, nmap2 = query_two()
                    if nc2 > tracker.total:
                        continue
                    if (_touches_same_side_neighbor(result, fp) or _touches_same_side_neighbor(result, other)
                            or not _local_legal(result, (fp, other), fixed, outline_bounds)
                            or (preserve_escapes is not None and not preserve_escapes(result))
                            or any(not f.fixed for f in legality.check(result, fixed=fixed))):
                        continue
                    distances, cand_dists = t2, tc2
                    all_dists = {**t2, **{r: info[0] for r, info in tc2.items()}}
                    commit_two(nc2, nmap2)
                    report["after"], report["after_all"] = s2, sa2
                    moved.extend([ref, other.ref] if other.ref not in moved else [ref])
                    accepted = True
                    break
                if accepted:
                    break
                other.x, other.y, other.rotation = other_orig
        if not accepted:
            fp.x, fp.y, fp.rotation = original
        if trials >= max_trials:
            break
    if moved and any(not f.fixed for f in legality.check(result, fixed=fixed)):
        return board, {**report, "trials": trials, "moved": []}
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
                   max_trials: int = 48, extend: bool = False) -> tuple[Board, dict]:
    """Align passives locally (``extend=False``) or run full rotation/swap/stacking refinement."""
    before = metrics.regularity(board)
    report = {"before": before, "after": before, "trials": 0, "moved": []}
    if before["n"] < 2 or (not extend and before["aligned_fraction"] >= 1.0):
        return board, report

    result = board.copy()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for fp in result.footprints:
        if _passive_prefix(fp.ref) in metrics.PASSIVE_PREFIXES:
            groups[(fp.side, fp.lib_id)].append(fp)

    res_d, cand_d, before_res, before_all = _measure(result, intent or {})
    has_res, has_all = bool(res_d), bool(res_d or cand_d)
    best_reg, cur_res, cur_all = before, before_res, before_all
    trials = 0
    moved: list[str] = []
    moved_set: set[str] = set()

    if extend:
        tracker = CrossingTracker(result)
        outline = legality.board_polygon(result)
        outline_bounds = outline.bounds if outline is not None else None
        boxes = {}

        def box(fp):
            pose = fp.x, fp.y, fp.rotation
            cached = boxes.get(fp.ref)
            if cached is None or cached[0] != pose:
                cached = boxes[fp.ref] = (pose, fp.box())
            return cached[1]

        def blockers(fp):
            return _find_blockers(result, fp, outline_bounds, box)

        clearance = max(board.rules.get("courtyard_clearance", 0), 0.06)
        base_rudy = metrics.rudy(result).get("over_capacity_fraction", 0.0)
        movable_fps = [fp for fp in sorted(result.footprints, key=lambda f: f.ref)
                       if fp.ref not in fixed and not fp.locked]

        def _mark_moved(ref: str) -> None:
            if ref not in moved_set:
                moved.append(ref)
                moved_set.add(ref)

        def _full_accept_ok(moved_fps: tuple) -> tuple[bool, dict, dict]:
            nonlocal trials
            ok_d, n_res, n_all = _decap_ok(result, intent, has_res, has_all, before_res, before_all)
            if not ok_d:
                return False, cur_res, cur_all
            if any(_touches_same_side_neighbor(result, f) for f in moved_fps):
                return False, cur_res, cur_all
            if not _local_legal(result, moved_fps, fixed, outline_bounds):
                return False, cur_res, cur_all
            if metrics.rudy(result).get("over_capacity_fraction", 0.0) > base_rudy + 1e-6:
                return False, cur_res, cur_all
            if trials >= max_trials:
                return False, cur_res, cur_all
            trials += 1
            if (preserve_escapes is not None and not preserve_escapes(result)) or any(
                    not f.fixed for f in legality.check(result, fixed=fixed)):
                return False, cur_res, cur_all
            return True, n_res, n_all

        # Multi-round refinement: rotation/swap (Pass A) + analytic stacking/co-alignment (Pass B).
        for _round in range(4):
            if trials >= max_trials:
                break
            round_changed = False

            # Pass A.1: Center-preserving rotation to reduce crossings or improve axis uniformity.
            for fp in movable_fps:
                if trials >= max_trials:
                    break
                ox, oy, orot = fp.x, fp.y, fp.rotation
                cx, cy = _body_center_world(fp)
                best_key = (-tracker.total, best_reg["aligned_fraction"], best_reg["dominant_axis_fraction"])
                best_state = (ox, oy, orot)
                best_meta: tuple[int, dict, dict, dict, dict] | None = None
                query, commit = tracker.prepare((fp,))
                for r in (0, 90, 180, 270):
                    if r == orot:
                        continue
                    _set_body_center_pose(fp, cx, cy, r)
                    if blockers(fp) != []:
                        continue
                    nr = metrics.regularity(result)
                    if nr["aligned_fraction"] < best_reg["aligned_fraction"] - 1e-9:
                        continue
                    nc, nmap = query()
                    if nc > tracker.total:
                        continue
                    key = (-nc, nr["aligned_fraction"], nr["dominant_axis_fraction"])
                    if key <= best_key:
                        continue
                    ok, n_res, n_all = _full_accept_ok((fp,))
                    if not ok:
                        continue
                    best_key = key
                    best_state = (fp.x, fp.y, fp.rotation)
                    best_meta = (nc, nmap, nr, n_res, n_all)
                fp.x, fp.y, fp.rotation = best_state
                if best_meta is not None:
                    commit(best_meta[0], best_meta[1])
                    best_reg, cur_res, cur_all = best_meta[2], best_meta[3], best_meta[4]
                    _mark_moved(fp.ref)
                    round_changed = True

            # Pass A.2: 2-part center swap + optional 180-deg flip within same-group passives.
            for _, grp in sorted(groups.items()):
                if len(grp) < 2 or trials >= max_trials:
                    continue
                ordered = sorted(grp, key=lambda f: f.ref)
                for idx, a in enumerate(ordered):
                    if a.ref in fixed or a.locked:
                        continue
                    for target in ordered[idx + 1:]:
                        if target.ref in fixed or target.locked or trials >= max_trials:
                            continue
                        if math.hypot(a.x - target.x, a.y - target.y) > 20.0:
                            continue
                        s0 = (a.x, a.y, a.rotation, target.x, target.y, target.rotation)
                        best_key = (-tracker.total, best_reg["aligned_fraction"], best_reg["dominant_axis_fraction"])
                        best_s = s0
                        best_meta2: tuple[int, dict, dict, dict, dict] | None = None
                        acx, acy = _body_center_world(a)
                        tcx, tcy = _body_center_world(target)
                        query, commit = tracker.prepare((a, target))
                        for ra in (s0[2], (s0[2] + 180) % 360):
                            for rt in (s0[5], (s0[5] + 180) % 360):
                                _set_body_center_pose(a, tcx, tcy, ra)
                                _set_body_center_pose(target, acx, acy, rt)
                                if blockers(a) != [] or blockers(target) != []:
                                    continue
                                nc, nmap = query()
                                if nc > tracker.total:
                                    continue
                                nr = metrics.regularity(result)
                                key = (-nc, nr["aligned_fraction"], nr["dominant_axis_fraction"])
                                if key <= best_key:
                                    continue
                                ok, n_res, n_all = _full_accept_ok((a, target))
                                if not ok:
                                    continue
                                best_key = key
                                best_s = (a.x, a.y, a.rotation, target.x, target.y, target.rotation)
                                best_meta2 = (nc, nmap, nr, n_res, n_all)
                        a.x, a.y, a.rotation, target.x, target.y, target.rotation = best_s
                        if best_meta2 is not None:
                            commit(best_meta2[0], best_meta2[1])
                            best_reg, cur_res, cur_all = best_meta2[2], best_meta2[3], best_meta2[4]
                            _mark_moved(a.ref)
                            _mark_moved(target.ref)
                            round_changed = True

            # Pass B: 1D coordinate snaps + analytic neighbor stacking + pair co-alignment.
            for _, grp in sorted(groups.items()):
                if len(grp) < 2 or trials >= max_trials:
                    continue
                ordered = sorted(grp, key=lambda f: f.ref)
                for a in ordered:
                    if a.ref in fixed or a.locked or trials >= max_trials:
                        continue
                    ox, oy, orot = a.x, a.y, a.rotation
                    best_state = (ox, oy, orot)
                    best_key = (best_reg["aligned_fraction"], best_reg["dominant_axis_fraction"], -tracker.total)
                    best_meta3: tuple[int, dict, dict, dict, dict] | None = None
                    cands: list[tuple[float, float, float]] = []
                    for target in ordered:
                        if target is a:
                            continue
                        d_ab = math.hypot(a.x - target.x, a.y - target.y)
                        if d_ab <= 12.0:
                            for rot in (orot, (orot + 180.0) % 360.0, target.rotation, (target.rotation + 180.0) % 360.0):
                                if abs(target.y - oy) <= metrics.ALIGN_RADIUS_MM:
                                    cands.append((round(target.x, 4), oy, rot))
                                if abs(target.x - ox) <= metrics.ALIGN_RADIUS_MM:
                                    cands.append((ox, round(target.y, 4), rot))
                        if d_ab <= 30.0:
                            lx_t, ly_t, rx_t, ry_t = target.box(0.0, 0.0, target.rotation)
                            for rot in (target.rotation, (target.rotation + 180.0) % 360.0, orot,
                                        (orot + 180.0) % 360.0, 0.0, 90.0, 180.0, 270.0):
                                lx_a, ly_a, rx_a, ry_a = a.box(0.0, 0.0, rot)
                                for gap in (clearance, clearance + 0.3, clearance + 0.6,
                                            clearance + 1.0, clearance + 1.4, clearance + 1.8,
                                            clearance + 2.2, clearance + 2.6):
                                    for sy in ((ly_t - ry_a) - gap, (ry_t - ly_a) + gap):
                                        if abs(sy) <= metrics.ALIGN_RADIUS_MM:
                                            cands.append((round(target.x, 4), round(target.y + sy, 4), rot))
                                    for sx in ((lx_t - rx_a) - gap, (rx_t - lx_a) + gap):
                                        if abs(sx) <= metrics.ALIGN_RADIUS_MM:
                                            cands.append((round(target.x + sx, 4), round(target.y, 4), rot))
                    query, commit = tracker.prepare((a,))
                    seen_p: set[tuple[float, float, float]] = set()
                    for tx, ty, trot in cands:
                        if (tx, ty, trot) in seen_p or trials >= max_trials:
                            continue
                        seen_p.add((tx, ty, trot))
                        if math.hypot(tx - ox, ty - oy) > 22.0:
                            continue
                        a.x, a.y, a.rotation = tx, ty, trot
                        if blockers(a) != []:
                            continue
                        nr = metrics.regularity(result)
                        if (nr["aligned_fraction"], nr["dominant_axis_fraction"]) <= best_key[:2]:
                            continue
                        nc, nmap = query()
                        if nc > tracker.total:
                            continue
                        key = (nr["aligned_fraction"], nr["dominant_axis_fraction"], -nc)
                        if key <= best_key:
                            continue
                        ok, n_res, n_all = _full_accept_ok((a,))
                        if not ok:
                            continue
                        best_key = key
                        best_state = (tx, ty, trot)
                        best_meta3 = (nc, nmap, nr, n_res, n_all)
                    a.x, a.y, a.rotation = best_state
                    if best_meta3 is not None:
                        commit(best_meta3[0], best_meta3[1])
                        best_reg, cur_res, cur_all = best_meta3[2], best_meta3[3], best_meta3[4]
                        _mark_moved(a.ref)
                        round_changed = True

                # Co-alignment for unaligned peer pairs within 25 mm.
                for idx, a in enumerate(ordered):
                    if a.ref in fixed or a.locked or trials >= max_trials:
                        continue
                    for target in ordered[idx + 1:]:
                        if target.ref in fixed or target.locked or trials >= max_trials:
                            continue
                        if math.hypot(a.x - target.x, a.y - target.y) > 25.0:
                            continue
                        s0 = (a.x, a.y, a.rotation, target.x, target.y, target.rotation)
                        best_key = (best_reg["aligned_fraction"], best_reg["dominant_axis_fraction"], -tracker.total)
                        best_s2 = s0
                        best_meta4: tuple[int, dict, dict, dict, dict] | None = None
                        query, commit = tracker.prepare((a, target))
                        for dtx, dty in ((0, 0), (-2, 0), (2, 0), (0, -2), (0, 2), (-4, 0), (4, 0), (0, -4), (0, 4)):
                            ntx, nty = round(s0[3] + dtx, 4), round(s0[4] + dty, 4)
                            for trot in (s0[5], (s0[5] + 180) % 360, (s0[5] + 90) % 360, (s0[5] + 270) % 360):
                                target.x, target.y, target.rotation = ntx, nty, trot
                                if blockers(target) != []:
                                    continue
                                lx_t, ly_t, rx_t, ry_t = target.box(0.0, 0.0, trot)
                                for arot in (trot, (trot + 180) % 360):
                                    lx_a, ly_a, rx_a, ry_a = a.box(0.0, 0.0, arot)
                                    for gap in (clearance, clearance + 0.4, clearance + 0.9,
                                                clearance + 1.4, clearance + 2.0):
                                        stack_pts = [
                                            (ntx, round(nty + ((ly_t - ry_a) - gap), 4)),
                                            (ntx, round(nty + ((ry_t - ly_a) + gap), 4)),
                                            (round(ntx + ((lx_t - rx_a) - gap), 4), nty),
                                            (round(ntx + ((rx_t - lx_a) + gap), 4), nty),
                                        ]
                                        for ax, ay in stack_pts:
                                            if trials >= max_trials:
                                                break
                                            if math.hypot(ax - ntx, ay - nty) > metrics.ALIGN_RADIUS_MM:
                                                continue
                                            a.x, a.y, a.rotation = ax, ay, arot
                                            if blockers(a) != []:
                                                continue
                                            nr = metrics.regularity(result)
                                            if (nr["aligned_fraction"], nr["dominant_axis_fraction"]) <= best_key[:2]:
                                                continue
                                            nc, nmap = query()
                                            if nc > tracker.total:
                                                continue
                                            key = (nr["aligned_fraction"], nr["dominant_axis_fraction"], -nc)
                                            if key <= best_key:
                                                continue
                                            ok, n_res, n_all = _full_accept_ok((a, target))
                                            if not ok:
                                                continue
                                            best_key = key
                                            best_s2 = (ax, ay, arot, ntx, nty, trot)
                                            best_meta4 = (nc, nmap, nr, n_res, n_all)
                        a.x, a.y, a.rotation, target.x, target.y, target.rotation = best_s2
                        if best_meta4 is not None:
                            commit(best_meta4[0], best_meta4[1])
                            best_reg, cur_res, cur_all = best_meta4[2], best_meta4[3], best_meta4[4]
                            _mark_moved(a.ref)
                            _mark_moved(target.ref)
                            round_changed = True
            if not round_changed:
                break

        if moved and any(not f.fixed for f in legality.check(result, fixed=fixed)):
            return board, {**report, "trials": trials, "moved": []}
        report.update(after=metrics.regularity(result), trials=trials, moved=moved)
        return (result if moved else board), report

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
    cross = metrics.crossings(result)["count"]
    failed_per_ref: dict[str, int] = defaultdict(int)

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
        if (_touches_same_side_neighbor(result, fp)
                or any(not f.fixed for f in legality.check(result, fixed=fixed))
                or (preserve_escapes is not None and not preserve_escapes(result))):
            fp.x, fp.y = ox, oy
            return False, True, ox, oy, best_reg, cur_res, cur_all
        return True, True, ox, oy, t_reg, n_res, n_all

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
        next_cross = metrics.crossings(result)["count"]
        if next_cross <= cross:
            cross, best_reg, cur_res, cur_all = next_cross, t_reg, n_res, n_all
            moved.append(ref)
            moved_set.add(ref)
        else:
            result.fp(ref).x, result.fp(ref).y = ox, oy
            failed_per_ref[ref] += 1

    report.update(after=metrics.regularity(result), trials=trials, moved=moved)
    return (result if moved else board), report
