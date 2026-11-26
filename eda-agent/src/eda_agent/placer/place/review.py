# SPDX-License-Identifier: MIT
"""S8 review and repair of saved placement candidates."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from shapely.affinity import translate
from shapely.geometry import box

from .. import quality
from ..eval import legality, lint
from ..intent import model
from ..io.kicad import KiCadAdapterError, read_board, refill_copy
from ..render import add_native_review_views, save_review_views
from ..route import drc
from . import mechanical

_PHYSICAL_DRC = {
    "shorting_items", "clearance", "solder_mask_bridge", "copper_edge_clearance", "items_not_allowed",
    "hole_clearance", "hole_to_hole", "starved_thermal", "track_crossing", "silk_over_copper", "silk_overlap",
}


def _drc_findings(candidate, reference):
    from ..diagnose import _points

    attribution = drc.attribute(candidate, reference)
    findings = []
    for row in attribution.router + attribution.new_other:
        if row["type"] not in _PHYSICAL_DRC:
            continue
        points = _points(row.get("items", []))
        refs = sorted({p["ref"] or match[1] for p in points
                       if (match := re.search(r"\bof ([^\s(]+)", p["description"])) or p["ref"]})
        # Trackless placement previews do not establish the final thermal connection.
        # The same KiCad finding remains an error in the routed artifact's DRC.
        placement_severity = "warning" if row["type"] == "starved_thermal" else "error"
        findings.append({**quality.finding(row["type"], refs, detail=row["description"], severity=placement_severity),
                         "points": points})
    return findings


def draft_evidence(path, board, reviewer, folder, *, reference_drc=None, native_views=True):
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    findings = reviewer.compare(board)
    preview, preview_board, drc_path = path, board, None
    refill_s = 0.0
    actual_views = None
    try:
        if any(row["kind"] == "stale_zone_fill" for row in findings):
            stale_board = read_board(path, include_fill=True)
            actual_views = save_review_views(path, stale_board, stale_board, reviewer.interfaces, findings,
                                             folder / "saved-stale", frame=reviewer.basis.outline_box)
        if board.zones:
            refill_started = time.monotonic()
            preview = refill_copy(path, folder / "copper-preview.kicad_pcb")
            preview_board = read_board(preview, include_fill=True)
            refill_s = time.monotonic() - refill_started
            if quality.fill_state(preview_board)["unfilled"]:
                findings.append(quality.finding("preview_zones_unfilled", severity="unknown"))
            if changes := quality.zone_changes(board, preview_board):
                findings.append(quality.finding("preview_zone_definition_changed", zones=changes))
        if reference_drc is not None:
            drc_path = folder / "placement.drc.json"
            result = drc.run(preview, drc_path, refill=False)
            findings.extend(_drc_findings(result, reference_drc))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        findings.append(quality.finding("copper_review_unavailable", severity="unknown", detail=str(exc)))
        preview, preview_board = path, board
    views = save_review_views(preview, board, preview_board, reviewer.interfaces, findings, folder,
                              frame=reviewer.basis.outline_box, native=native_views)
    copper = quality.fill_state(board)
    status = "failed" if quality.rank(findings)[0] else "needs_evidence" if quality.rank(findings)[1] else "approved"
    return {"status": status, "artifact": quality.identity(path),
            "zones_require_refill": bool(board.zones), "copper": copper,
            "preview": quality.identity(preview) if preview != path else None,
            "saved_stale_views": actual_views,
            "preview_copper": quality.fill_state(preview_board), "findings": findings,
            "drc_report": str(drc_path) if drc_path and drc_path.is_file() else None,
            "findings_path": str(quality.write_findings(folder / "findings.json", findings)),
            "views": views, "seconds_refill": round(refill_s, 3), "seconds_review": round(time.monotonic() - t0, 3)}


def _candidate(board, reviewer, action, findings):
    if action == "restore_reference":
        refs = {fp.ref for fp in board.footprints}
    elif action == "restore_protected":
        refs = {ref for row in findings if row["kind"] == "protected_pose_changed" for ref in row["refs"]}
    elif action == "restore_blockers":
        refs = ({ref for row in findings if row["severity"] == "error" for ref in row["refs"]}
                - reviewer.expected.keys()) & reviewer.source.keys()
    else:
        refs = set()
    if not refs and action != "refresh_draft":
        return None
    trial = board.copy()
    artwork_refs = {ref for row in findings if row["kind"] == "artwork_occluded" for ref in row["refs"]}
    for ref in sorted(refs):
        current = trial.fp(ref)
        if (action == "restore_blockers" and ref in artwork_refs
                and _shift_clear_of_artwork(trial, current, reviewer.basis.artwork)):
            continue
        target = reviewer.expected.get(ref) or reviewer.source.get(ref)
        if target is not None:
            current.x, current.y, current.rotation, current.side = (target.x, target.y, target.rotation, target.side)
    if action != "refresh_draft" and all(mechanical.same_pose(fp, board.fp(fp.ref)) for fp in trial.footprints):
        return None
    return trial


def _shift_clear_of_artwork(board, fp, authored):
    shape = legality.courtyard_polygon(fp)
    outline = legality.board_polygon(board)
    if shape is None or outline is None:
        return False
    body = box(*fp.body_bounds_mm) if fp.body_bounds_mm else shape
    artwork = [box(*row["bounds_mm"]) for row in authored if row["side"] == fp.side and row["owner"] != fp.ref]
    neighbors = [legality.courtyard_polygon(other) for other in board.footprints
                 if other.ref != fp.ref and other.side == fp.side]
    clearance = max(board.rules.get("courtyard_clearance", 0.0), 0.02)
    for step in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        for dx, dy in ((-step, 0), (step, 0), (0, -step), (0, step),
                       (-step, -step), (-step, step), (step, -step), (step, step)):
            moved = translate(shape, xoff=dx, yoff=dy)
            ink_bounds = translate(body, xoff=dx, yoff=dy)
            if (outline.covers(moved) and not any(ink_bounds.intersects(ink) for ink in artwork)
                    and not any(neighbor is not None and moved.distance(neighbor) < clearance for neighbor in neighbors)):
                fp.x += dx
                fp.y += dy
                fp.body_bounds_mm = ink_bounds.bounds
                return True
    return False


def _repair_actions(findings):
    hard = {row["kind"] for row in findings if row["severity"] == "error"}
    if hard == {"stale_zone_fill"}:
        return ("refresh_draft",)
    # Every pose correction rewrites the draft with invalidated copper. Use
    # the remaining rounds to resolve geometry and retain a known-good fallback.
    return ("restore_protected", "restore_blockers", "restore_reference")


def run(path: Path, reviewer: quality.PlacementReview, folder: Path, write_trial, *, allow_reference=True):
    """Keep the strongest checked state and retain the evidence for every tried correction."""
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    reference_error = None
    try:
        reference_drc = drc.run_cached(Path(reviewer.basis.source["path"]), folder / "reference.drc.json")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        reference_drc, reference_error = None, str(exc)
    board = read_board(path)
    selected = draft_evidence(path, board, reviewer, folder / "round-0", reference_drc=reference_drc, native_views=False)
    rounds = [{"action": "review_saved_placement", "accepted": True, **selected}]
    stop = "placement_approved" if selected["status"] == "approved" else "no_applicable_repair"
    for action in _repair_actions(selected["findings"]):
        findings = selected["findings"]
        if selected["status"] == "approved" or not quality.rank(findings)[0] or len(rounds) > quality.MAX_REPAIRS:
            break
        if (action == "restore_reference" and not allow_reference) or (action == "restore_blockers" and reviewer.cold):
            continue
        trial = _candidate(board, reviewer, action, findings)
        if trial is None:
            continue
        round_dir = folder / f"round-{len(rounds)}"
        try:
            trial_path, stats = write_trial(trial, round_dir / path.name)
            native_trial = read_board(trial_path)
            result = draft_evidence(trial_path, native_trial, reviewer, round_dir,
                                    reference_drc=reference_drc, native_views=False)
        except (OSError, KiCadAdapterError, subprocess.SubprocessError) as exc:
            rounds.append({"action": action, "accepted": False, "error": str(exc)})
            continue
        accepted = quality.improves(findings, result["findings"])
        rounds.append({"action": action, "accepted": accepted, **result})
        if accepted:
            path, board, selected = trial_path, native_trial, {**result, "write_stats": stats}
            if selected["status"] == "approved":
                stop = "placement_approved"
    if selected["status"] != "approved" and len(rounds) > quality.MAX_REPAIRS:
        stop = "repair_budget_exhausted"
    if reference_error:
        selected["status"] = "needs_evidence" if selected["status"] == "approved" else selected["status"]
        selected["reference_drc_error"] = reference_error
        stop = "reference_drc_unavailable"
    native_started = time.monotonic()
    native_src = Path((selected["preview"] or selected["artifact"])["path"])
    add_native_review_views(native_src, Path(selected["findings_path"]).parent, selected["views"])
    selected["seconds_review"] = round(selected["seconds_review"] + time.monotonic() - native_started, 3)
    return path, board, {**selected, "rounds": rounds, "stop": stop}


def routed_evidence(path, board, reviewer, report, placement, folder, *, taps=None):
    checked = report["quality"]["routing"]
    findings = reviewer.compare(board, draft=False, allowed_new_rule_areas=checked.get("generated_rule_areas", ()))
    if taps and not taps["passed"]:
        findings.append(quality.finding("s6a_tap_lost_after_routing"))
    if checked["artifact"] != quality.identity(path):
        findings.append(quality.finding("route_changed_after_drc"))
    if placement["status"] != "approved":
        findings.append(quality.finding("placement_not_approved"))

    def critical_key(row):
        return row["rule"], row.get("net"), tuple(row.get("refs") or ())

    reference = read_board(reviewer.basis.source["path"], include_fill=True)
    old = {critical_key(row) for row in lint.audit(reference, reviewer.intent).get("findings", [])
           if row["severity"] == "critical"}
    current = lint.audit(board, reviewer.intent)
    for row in current.get("findings", []):
        if row["severity"] != "critical" or critical_key(row) in old:
            continue
        net = row.get("net")
        claim = reviewer.intent.get("nets", {}).get(net, {}).get("class")
        hard = model.is_hard(claim) and claim["source"]["kind"] in ("board", "user", "datasheet")
        findings.append(quality.finding(row["rule"], row.get("refs") or (), detail=row["detail"],
                                        location=row.get("location_mm"), net=net, severity="error" if hard else "warning"))
    views = save_review_views(path, board, board, reviewer.interfaces, findings, folder,
                              frame=reviewer.basis.outline_box)
    return {**checked, **quality.routing_status(
                report["metrics"], report["gates"], checked["copper"], reviewed=quality.rank(findings) == (0, 0)),
            "findings": findings, "views": views, "si_pi": current,
            "findings_path": str(quality.write_findings(folder / "findings.json", findings))}
