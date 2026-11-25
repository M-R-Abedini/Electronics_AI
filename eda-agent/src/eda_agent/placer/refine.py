# SPDX-License-Identifier: MIT
"""Evaluate one local placement hypothesis on independent KiCad copies."""

from __future__ import annotations

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import report as run_report
from .eval import legality, metrics, score
from .intent.resolve import resolve
from .io.kicad import read_board, write_board
from .place import decap, escape, problem
from .route import drc
from .route.check import PROJECT_SIDECARS, RouteCheckError, route_check
from .toolchain import ToolchainError, sha256_file


def _baseline_cache_key(
    board_path: Path,
    reference: Path,
    route_timeout_s: int,
    *,
    s6a_enabled: bool = False,
    intent_sha256: str | None = None,
) -> str:
    payload: dict = {
        "board_sha256": sha256_file(board_path),
        "reference_sha256": sha256_file(reference),
        "route_timeout_s": int(route_timeout_s),
        "s6a": bool(s6a_enabled),
        "intent_sha256": intent_sha256,
        "sidecars": {},
        "tools": {},
    }
    for suffix in PROJECT_SIDECARS:
        side = board_path.with_suffix(suffix)
        if side.is_file():
            payload["sidecars"][suffix] = sha256_file(side)
    try:
        tools = run_report.tools()
        payload["tools"] = {
            "kicad": (tools.get("kicad") or {}).get("version"),
            "java": (tools.get("java") or {}).get("version"),
            "freerouting": (tools.get("freerouting") or {}).get("sha256"),
        }
    except (ToolchainError, OSError):
        pass
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cached_baseline(cache_file: Path) -> dict | None:
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and all(k in data for k in ("metrics", "drc", "gates")):
        return data
    return None


def _save_cached_baseline(cache_file: Path, report_data: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(report_data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_file)
    except OSError:
        pass


def refine(run: str, change: dict, out_root: Path, *, route_timeout_s: int = 180,
           preview: bool = False, cache_baseline: bool = True, min_improvement: int = 1) -> dict:
    report_path = Path(run).resolve()
    if report_path.is_dir():
        report_path /= "run_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("config", {}).get("cold_input") or report.get("stages", {}).get("place", {}).get("cold_input"):
        raise ValueError("Refinement requires a placement with a reproducible PCB intent input")
    depth = int(report.get("refinement_round", 0))
    if depth >= 3:
        raise ValueError("Three refinement rounds have already been accepted for this run")
    src = Path(report["artifacts"].get("placed_board") or report["inputs"]["board"]["path"]).resolve()
    before = read_board(src)
    original_hash = sha256_file(src)
    place_stage = report.get("stages", {}).get("place") or {}
    why_fixed = problem.fixed_parts(before) | (place_stage.get("fixed") or {})
    ref = change["ref"]
    if sum(fp.ref == ref for fp in before.footprints) != 1:
        raise ValueError("Refinement requires a unique existing footprint reference")
    reference = Path(report["inputs"]["board"]["path"]).resolve()
    reference_hash = report["inputs"]["board"].get("sha256")
    if reference_hash is not None and sha256_file(reference) != reference_hash:
        raise ValueError("The placement input changed; generate a fresh placement run")
    saved = report["inputs"]["intent"]
    design = read_board(reference) if reference != src else before
    intent, info = resolve(design, saved.get("path"))
    if info["sha256"] != saved["sha256"]:
        raise ValueError("Recorded intent hash differs; rerun placement with the current intent")
    protected = set(why_fixed) | set(intent.get("mechanical", {}))
    macros = place_stage.get("rigid_macros") or {}
    protected.update(member for group in macros.get("members", []) for member in group)
    delta = {"run": str(report_path), "retained_board": str(src), "source_sha256": original_hash, "change": change}
    if ref in protected:
        return {**delta, "status": "rejected", "reason": "Footprint has a fixed or mechanical placement"}
    after = before.copy()
    fp = after.fp(ref)
    angle = change.get("rotation_deg")
    if angle is not None:
        if angle not in (0, 90, 180, 270):
            raise ValueError("Refinement rotation must be orthogonal")
        fp.rotation = angle
    swap_ref = change.get("swap_with_ref")
    side_flip = bool(change.get("side_flip", False))
    passive_align = bool(change.get("passive_align", False))
    block_refs_raw = change.get("block_refs")
    if swap_ref and (change.get("toward_ref") or change.get("away_from_ref")):
        raise ValueError("swap_with_ref cannot be combined with toward_ref or away_from_ref")
    if swap_ref == ref:
        raise ValueError("swap_with_ref must name a different footprint")
    if change.get("toward_ref") and change.get("away_from_ref"):
        raise ValueError("Choose one movement direction")
    if change.get("away_from_ref") == ref or change.get("toward_ref") == ref:
        raise ValueError("Movement target must be on a different footprint")
    if ("target_pad" in change or "step_mm" in change) and not (change.get("toward_ref") or change.get("away_from_ref")):
        raise ValueError("A target pad or step requires a movement direction")
    if side_flip:
        prior_side_policy = str((report.get("config") or {}).get("side_policy") or "expert")
        if prior_side_policy not in ("level2_passives_back", "level3_two_sided") and not change.get("allow_side_flip"):
            return {**delta, "status": "rejected",
                    "reason": f"side_flip requires side_policy in ('level2_passives_back', 'level3_two_sided') or allow_side_flip=True (current: {prior_side_policy})"}
        bc = decap._body_center_world(fp)
        fp.side = "B" if fp.side == "F" else "F"
        decap._set_body_center_pose(fp, bc[0], bc[1], fp.rotation)
    if passive_align:
        # Law-P1-safe orthogonal orientation alignment minimizing pad-to-pad distance to connected neighbor pins.
        target_hint = change.get("toward_ref")
        best_rot = fp.rotation
        best_cost = math.inf
        ref_pads_by_net: dict[str, list[tuple[float, float]]] = {}
        for other in after.footprints:
            if other.ref == ref:
                continue
            if target_hint and other.ref != target_hint:
                continue
            for p, xy in other.pads_world():
                if p.net:
                    ref_pads_by_net.setdefault(p.net, []).append(xy)
        bc = decap._body_center_world(fp)
        for cand_rot in (0, 90, 180, 270):
            decap._set_body_center_pose(fp, bc[0], bc[1], cand_rot)
            cost = 0.0
            for p, xy in fp.pads_world():
                peers = ref_pads_by_net.get(p.net or "")
                if peers:
                    cost += min(math.dist(xy, q) for q in peers)
            if cost < best_cost - 1e-6:
                best_cost = cost
                best_rot = cand_rot
        decap._set_body_center_pose(fp, bc[0], bc[1], best_rot)
    moved_block_refs: list[str] = []
    if swap_ref:
        if sum(other.ref == swap_ref for other in after.footprints) != 1:
            raise ValueError("swap_with_ref must identify a unique existing footprint")
        if swap_ref in protected:
            return {**delta, "status": "rejected", "reason": "Swap peer has a fixed or mechanical placement"}
        peer = after.fp(swap_ref)
        if peer.side != fp.side:
            return {**delta, "status": "rejected", "reason": "swap_with_ref requires both footprints on the same side"}
        c1 = decap._body_center_world(fp)
        c2 = decap._body_center_world(peer)
        decap._set_body_center_pose(fp, c2[0], c2[1], fp.rotation)
        decap._set_body_center_pose(peer, c1[0], c1[1], peer.rotation)
    elif target_ref := change.get("toward_ref") or change.get("away_from_ref"):
        if sum(other.ref == target_ref for other in after.footprints) != 1:
            raise ValueError("Refinement target must be a unique existing footprint")
        target = after.fp(target_ref)
        target_xy = target.position
        if number := change.get("target_pad"):
            pads = [pad for pad in target.pads if pad.number == number]
            if len(pads) != 1:
                raise ValueError("Target pad must identify one pad on the target footprint")
            target_xy = target.pad_world(pads[0])
        distance = math.dist(fp.position, target_xy)
        step = change.get("step_mm", 1.0)
        if not 0.05 <= step <= 3.0:
            raise ValueError("Refinement step must be between 0.05 and 3 mm")
        if distance:
            fraction = -step / distance if change.get("away_from_ref") else min(step, distance) / distance
            dx_move = (target_xy[0] - fp.x) * fraction
            dy_move = (target_xy[1] - fp.y) * fraction
            fp.x += dx_move
            fp.y += dy_move
            if block_refs_raw:
                for bref in block_refs_raw:
                    if bref == ref or bref in protected or sum(o.ref == bref for o in after.footprints) != 1:
                        continue
                    bfp = after.fp(bref)
                    if bfp.side == fp.side:
                        bfp.x += dx_move
                        bfp.y += dy_move
                        moved_block_refs.append(bref)
    changed_ref = (
        before.fp(ref).position != fp.position
        or before.fp(ref).rotation != fp.rotation
        or before.fp(ref).side != fp.side
    )
    changed_swap = bool(swap_ref) and (
        before.fp(swap_ref).position != after.fp(swap_ref).position
        or before.fp(swap_ref).rotation != after.fp(swap_ref).rotation
    )
    if not changed_ref and not changed_swap:
        return {**delta, "status": "rejected", "reason": "Proposal produced no placement change"}

    findings = [f for f in legality.check(after, fixed=protected) if not f.fixed]
    if findings:
        return {**delta, "status": "rejected", "reason": "Placement legality failed",
                "violations": [{"kind": f.kind, "refs": f.refs, "detail": f.detail} for f in findings[:12]]}
    old_witnesses, new_witnesses = {}, {}
    old_loops = metrics.critical_loops_intent(before, intent, witnesses=old_witnesses)
    new_loops = metrics.critical_loops_intent(after, intent, witnesses=new_witnesses)
    for capacitor, old in old_witnesses.items():
        if new_witnesses.get(capacitor, {}).get("distance_mm", math.inf) > old["distance_mm"] + 1e-6:
            return {**delta, "status": "rejected", "reason": f"Resolved decoupling connection regressed: {capacitor}"}
    for key in ("decap_pin_distance", "resolved_decap_pin_distance", "crystal_ic_distance", "crystal_load_distance"):
        for stat in ("p90_mm", "max_mm"):
            old = old_loops[key].get(stat)
            if old is not None and new_loops[key].get(stat, math.inf) > old:
                return {**delta, "status": "rejected", "reason": f"Critical loop regressed: {key}.{stat}"}
    flips = {fp.ref for fp in before.footprints if fp.side != design.fp(fp.ref).side}
    exits = escape.envelopes(design, intent, {fp.ref: problem.local_box(fp) for fp in design.footprints})
    blocked = [{(row["ref"], row["pin"], row["net"]) for row in escape.audit(board, exits, flipped=flips)["blocked"]}
               for board in (before, after)]
    if blocked[1] - blocked[0]:
        return {**delta, "status": "rejected", "reason": "Proposal blocked an additional pad exit"}
    comparison = {"baseline_blocked_exits": len(blocked[0]), "candidate_blocked_exits": len(blocked[1]),
                  "freed_exits": [dict(zip(("ref", "pad", "net"), row, strict=True))
                                  for row in sorted(blocked[0] - blocked[1])],
                  "baseline_crossings": metrics.crossings(before)["count"],
                  "candidate_crossings": metrics.crossings(after)["count"]}
    if preview:
        out = {**delta, "status": "preview", **comparison,
               "pose_before": {"x_mm": before.fp(ref).x, "y_mm": before.fp(ref).y,
                               "rotation_deg": before.fp(ref).rotation, "side": before.fp(ref).side},
               "pose_after": {"x_mm": fp.x, "y_mm": fp.y, "rotation_deg": fp.rotation, "side": fp.side}}
        if swap_ref:
            out["swap_pose_before"] = {
                "ref": swap_ref, "x_mm": before.fp(swap_ref).x, "y_mm": before.fp(swap_ref).y,
                "rotation_deg": before.fp(swap_ref).rotation, "side": before.fp(swap_ref).side,
            }
            out["swap_pose_after"] = {
                "ref": swap_ref, "x_mm": after.fp(swap_ref).x, "y_mm": after.fp(swap_ref).y,
                "rotation_deg": after.fp(swap_ref).rotation, "side": after.fp(swap_ref).side,
            }
        return out

    work = out_root.resolve() / f"{src.stem}-refine-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    candidate = write_board(src, after, work / src.name, strip_routing=True, unfill_zones=True)
    original = write_board(src, before, work / "baseline" / src.name, strip_routing=True, unfill_zones=True)
    if any(item["kind"] == "mask_aperture" for item in before.obstacles):
        mask_before = work / "mask-before.drc.json"
        # The candidate report also follows the placement report's sidecar naming convention.
        mask_after = candidate.with_suffix(".drc.json")
        old_mask = drc.run(original, mask_before)
        new_mask = drc.run(candidate, mask_after)
        added = drc.new_mask_bridges(old_mask, new_mask, {ref, swap_ref} if swap_ref else {ref})
        if added:
            return {**delta, "status": "rejected", "reason": "New solder mask bridges before routing",
                    "new_mask_bridge_count": len(added), "new_mask_bridges": added[:12],
                    "drc_reports": [str(mask_before), str(mask_after)], **comparison}
    use_s6a = bool(report.get("config", {}).get("s6a", False))

    def _route_trial(board_file: Path, route_dir: Path) -> dict:
        route_in = board_file
        if use_s6a:
            from .route import s6a as s6a_mod

            s6a_res = s6a_mod.apply_to_board(board_file, intent, route_dir / "s6a")
            if s6a_res["applied"] and s6a_res["s6a_board"]:
                route_in = Path(s6a_res["s6a_board"])
        return route_check(
            route_in, out_dir=route_dir, reference_board=reference,
            router_timeout_s=route_timeout_s, intent=intent,
        )

    cache_key = _baseline_cache_key(
        original, reference, route_timeout_s,
        s6a_enabled=use_s6a, intent_sha256=info["sha256"],
    )
    cache_file = out_root.resolve() / ".refine_baseline_cache" / f"{cache_key}.json"
    cached_a = _load_cached_baseline(cache_file) if cache_baseline else None
    baseline_cached = cached_a is not None
    try:
        if cached_a is not None:
            a = cached_a
            b = _route_trial(candidate, work / "route_after")
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(_route_trial, original, work / "route_before")
                fut_b = pool.submit(_route_trial, candidate, work / "route_after")
                a, b = fut_a.result(), fut_b.result()
            if cache_baseline:
                _save_cached_baseline(cache_file, a)
    except RouteCheckError as exc:
        return {**delta, "status": "inconclusive", "reason": str(exc), "trial_dir": str(work)}
    before_route, after_route = a["metrics"], b["metrics"]
    if sha256_file(src) != original_hash:
        raise RuntimeError("Baseline board changed while refinement was running")
    before_report_ref = str(cache_file) if baseline_cached else str(work / "route_before" / "run_report.json")
    comparison.update(baseline=before_route["unrouted"], candidate=after_route["unrouted"],
                      baseline_cached=baseline_cached,
                      baseline_coverage=before_route["coverage"], candidate_coverage=after_route["coverage"],
                      baseline_settled=before_route["settled"], candidate_settled=after_route["settled"],
                      baseline_drc={key: len(a["drc"][key]) for key in ("router_caused", "new_non_routing")},
                      candidate_drc={key: len(b["drc"][key]) for key in ("router_caused", "new_non_routing")},
                      router_timeout_s=route_timeout_s,
                      reports=[before_report_ref, str(work / "route_after" / "run_report.json")])
    if comparison["candidate_drc"]["router_caused"] or comparison["candidate_drc"]["new_non_routing"]:
        return {**delta, "status": "rejected", "reason": "Candidate failed native DRC", **comparison}
    if not before_route["settled"] or not after_route["settled"]:
        return {**delta, "status": "inconclusive", "reason": "Both route runs must settle", **comparison}
    if before_route["denominator"] != after_route["denominator"]:
        return {**delta, "status": "inconclusive", "reason": "Route denominators differ", **comparison}
    if any(not g["passed"] for g in a.get("gates", []) if not g["name"].startswith("drc_")):
        return {**delta, "status": "inconclusive", "reason": "Baseline route accounting or import failed", **comparison}
    if (after_route["unrouted_signal"] > before_route["unrouted_signal"]
            or after_route["unrouted_plane"] > before_route["unrouted_plane"] or not b["passed"]):
        return {**delta, "status": "rejected", "reason": "Route constraints or DRC failed", **comparison}
    req_gain = 0 if before_route["unrouted"] == 0 else max(1, int(min_improvement))
    if before_route["unrouted"] - after_route["unrouted"] < req_gain:
        return {**delta, "status": "rejected", "reason": "No reduction in open connections", **comparison}
    before_copper_path = (a.get("artifacts") or {}).get("routed_board")
    after_copper_path = (b.get("artifacts") or {}).get("routed_board")
    before_copper = read_board(before_copper_path) if before_copper_path and Path(before_copper_path).is_file() else None
    after_copper = read_board(after_copper_path) if after_copper_path and Path(after_copper_path).is_file() else None
    old_eval = score.evaluate(before, before_route, protected, intent, copper_board=before_copper)
    old_score = old_eval["pro_score"]["score"]
    new_evaluation = score.evaluate(after, after_route, protected, intent, copper_board=after_copper)
    new_score = new_evaluation["pro_score"]["score"]
    old_lint = ((old_eval.get("metrics") or {}).get("lint") or {})
    new_lint = ((new_evaluation.get("metrics") or {}).get("lint") or {})
    old_lint_s = old_lint.get("score")
    new_lint_s = new_lint.get("score")
    pareto_lint_win = (
        before_route["unrouted"] == 0
        and after_route["unrouted"] == 0
        and old_lint_s is not None
        and new_lint_s is not None
        and (new_lint_s - old_lint_s) >= 2.0
        and int(new_lint.get("critical_errors") or 0) <= int(old_lint.get("critical_errors") or 0)
        and new_score >= old_score - 0.15
    )
    comparison.update(
        baseline_pro_score=old_score,
        candidate_pro_score=new_score,
        baseline_lint_score=old_lint_s,
        candidate_lint_score=new_lint_s,
        pareto_lint_win=pareto_lint_win,
    )
    if not pareto_lint_win and ((before_route["unrouted"] == 0 and new_score <= old_score + 1e-6) or new_score < old_score):
        return {**delta, "status": "rejected", "reason": "Pro Score did not improve" if before_route["unrouted"] == 0 else "Pro Score regressed", **comparison}
    new_report = work / "run_report.json"
    prior_cfg = dict(report.get("config") or {})
    preserved_cfg = {
        k: prior_cfg[k]
        for k in ("s6a", "side_policy", "floorplan", "silkscreen_policy", "fab_profile")
        if k in prior_cfg
    }
    preserved_cfg.update({"s6a": use_s6a, "change": change, "router_timeout_s": route_timeout_s})
    updated = run_report.build(
        kind="place_refinement", inputs=report["inputs"], config=preserved_cfg,
        stages={"place": {"fixed": why_fixed, "rigid_macros": macros},
                "route_check": {"run_report": comparison["reports"][1]}},
        metrics={**new_evaluation["metrics"], "pro_score": new_evaluation["pro_score"],
                 "fanout_escape": escape.audit(after, exits, flipped=flips), "route": after_route},
        gates=b.get("gates", []), failures=[], refinement_round=depth + 1, parent_run=str(report_path),
        artifacts={"placed_board": str(candidate), "run_dir": str(work)},
    )
    run_report.write(updated, new_report)
    return {**delta, "status": "accepted", "retained_board": str(candidate), "run_report": str(new_report),
            "routed_board": b["artifacts"]["routed_board"], "moved_block_refs": moved_block_refs, **comparison}
