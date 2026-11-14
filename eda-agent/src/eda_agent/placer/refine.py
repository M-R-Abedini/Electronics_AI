# SPDX-License-Identifier: MIT
"""Evaluate one local placement hypothesis on independent KiCad copies."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from . import report as run_report
from .eval import legality, metrics, score
from .intent.resolve import resolve
from .io.kicad import read_board, write_board
from .place import escape, problem
from .route.check import RouteCheckError, route_check
from .toolchain import sha256_file


def refine(run: str, change: dict, out_root: Path, *, route_timeout_s: int = 180, preview: bool = False) -> dict:
    report_path = Path(run).resolve()
    if report_path.is_dir():
        report_path /= "run_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("config", {}).get("cold_input") or report.get("stages", {}).get("place", {}).get("cold_input"):
        raise ValueError("Refinement requires a placement with a reproducible PCB intent input")
    depth = int(report.get("refinement_round", 0))
    if depth >= 3:
        raise ValueError("Three refinement rounds have already been accepted for this run")
    src = Path(report["artifacts"]["placed_board"]).resolve()
    before = read_board(src)
    original_hash = sha256_file(src)
    why_fixed = problem.fixed_parts(before) | (report.get("stages", {}).get("place", {}).get("fixed") or {})
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
    macros = report["stages"]["place"].get("rigid_macros") or {}
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
    if change.get("toward_ref") and change.get("away_from_ref"):
        raise ValueError("Choose one movement direction")
    if ("target_pad" in change or "step_mm" in change) and not (change.get("toward_ref") or change.get("away_from_ref")):
        raise ValueError("A target pad or step requires a movement direction")
    if target_ref := change.get("toward_ref") or change.get("away_from_ref"):
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
            fp.x += (target_xy[0] - fp.x) * fraction
            fp.y += (target_xy[1] - fp.y) * fraction
    if before.fp(ref).position == fp.position and before.fp(ref).rotation == fp.rotation:
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
        return {**delta, "status": "preview", **comparison,
                "pose_before": {"x_mm": before.fp(ref).x, "y_mm": before.fp(ref).y,
                                "rotation_deg": before.fp(ref).rotation, "side": before.fp(ref).side},
                "pose_after": {"x_mm": fp.x, "y_mm": fp.y, "rotation_deg": fp.rotation, "side": fp.side}}

    work = out_root.resolve() / f"{src.stem}-refine-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    candidate = write_board(src, after, work / src.name, strip_routing=True, unfill_zones=True)
    original = write_board(src, before, work / "baseline" / src.name, strip_routing=True, unfill_zones=True)
    try:
        a, b = (route_check(path, out_dir=work / name, reference_board=reference, router_timeout_s=route_timeout_s)
                for path, name in ((original, "route_before"), (candidate, "route_after")))
    except RouteCheckError as exc:
        return {**delta, "status": "inconclusive", "reason": str(exc), "trial_dir": str(work)}
    before_route, after_route = a["metrics"], b["metrics"]
    if sha256_file(src) != original_hash:
        raise RuntimeError("Baseline board changed while refinement was running")
    comparison.update(baseline=before_route["unrouted"], candidate=after_route["unrouted"],
                      baseline_coverage=before_route["coverage"], candidate_coverage=after_route["coverage"],
                      baseline_settled=before_route["settled"], candidate_settled=after_route["settled"],
                      baseline_drc={key: len(a["drc"][key]) for key in ("router_caused", "new_non_routing")},
                      candidate_drc={key: len(b["drc"][key]) for key in ("router_caused", "new_non_routing")},
                      router_timeout_s=route_timeout_s,
                      reports=[str(work / name / "run_report.json") for name in ("route_before", "route_after")])
    if not before_route["settled"] or not after_route["settled"]:
        return {**delta, "status": "inconclusive", "reason": "Both route runs must settle", **comparison}
    if before_route["denominator"] != after_route["denominator"]:
        return {**delta, "status": "inconclusive", "reason": "Route denominators differ", **comparison}
    if any(not g["passed"] for g in a.get("gates", []) if not g["name"].startswith("drc_")):
        return {**delta, "status": "inconclusive", "reason": "Baseline route accounting or import failed", **comparison}
    if (b["drc"]["new_non_routing"] or b["drc"]["router_caused"]
            or after_route["unrouted_signal"] > before_route["unrouted_signal"]
            or after_route["unrouted_plane"] > before_route["unrouted_plane"] or not b["passed"]):
        return {**delta, "status": "rejected", "reason": "Route constraints or DRC failed", **comparison}
    if after_route["unrouted"] >= before_route["unrouted"]:
        return {**delta, "status": "rejected", "reason": "No reduction in open connections", **comparison}
    old_score = score.evaluate(before, before_route, protected, intent)["pro_score"]["score"]
    new_evaluation = score.evaluate(after, after_route, protected, intent)
    new_score = new_evaluation["pro_score"]["score"]
    comparison.update(baseline_pro_score=old_score, candidate_pro_score=new_score)
    if new_score < old_score:
        return {**delta, "status": "rejected", "reason": "Pro Score regressed", **comparison}
    new_report = work / "run_report.json"
    updated = run_report.build(
        kind="place_refinement", inputs=report["inputs"], config={"change": change, "router_timeout_s": route_timeout_s},
        stages={"place": {"fixed": why_fixed, "rigid_macros": macros},
                "route_check": {"run_report": comparison["reports"][1]}},
        metrics={**new_evaluation["metrics"], "pro_score": new_evaluation["pro_score"],
                 "fanout_escape": escape.audit(after, exits, flipped=flips), "route": after_route},
        gates=b.get("gates", []), failures=[], refinement_round=depth + 1, parent_run=str(report_path),
        artifacts={"placed_board": str(candidate), "run_dir": str(work)},
    )
    run_report.write(updated, new_report)
    return {**delta, "status": "accepted", "retained_board": str(candidate), "run_report": str(new_report),
            "routed_board": b["artifacts"]["routed_board"], **comparison}
