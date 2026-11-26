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


def _drc_findings(candidate, reference, *, exclusions=None):
    from ..diagnose import _points

    attribution = drc.attribute(candidate, reference, exclusions=exclusions)
    findings = []
    for row in attribution.router + attribution.new_other:
        if row["type"] not in _PHYSICAL_DRC:
            continue
        points = _points(row.get("items", []))
        refs = sorted({p["ref"] or match[1] for p in points
                       if (match := re.search(r"\bof ([^\s(]+)", p["description"])) or p["ref"]})
        # Trackless placement previews do not establish the final thermal connection or run
        # post-route mask-guard healing on board-level mask graphics vs zones (no footprint refs).
        # The same KiCad finding remains an error in the routed artifact's DRC.
        is_zone_mask_preview = row["type"] == "solder_mask_bridge" and not refs
        placement_severity = "warning" if (row["type"] == "starved_thermal" or is_zone_mask_preview) else "error"
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
            exclusions = drc.load_exclusions(Path(reviewer.basis.source["path"]))
            findings.extend(_drc_findings(result, reference_drc, exclusions=exclusions))
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
    if action == "restore_protected":
        refs = {ref for row in findings if row["kind"] == "protected_pose_changed" for ref in row["refs"]}
    elif action == "repair_local":
        refs = set()
        for row in findings:
            if row["severity"] != "error":
                continue
            # For blocked critical pad exits [ic, *blockers], move the blocker(s) rather than the IC.
            if row["kind"] == "critical_pad_exit_blocked" and len(row["refs"]) >= 2:
                refs.update(row["refs"][1:])
            else:
                refs.update(row["refs"])
        refs -= reviewer.expected.keys()
    else:
        refs = set()
    if not refs and action != "refresh_draft":
        return None
    trial = board.copy()
    if action == "restore_protected":
        for ref in sorted(refs):
            current = trial.fp(ref)
            target = reviewer.expected.get(ref)
            if target is not None:
                current.x, current.y, current.rotation, current.side = (target.x, target.y, target.rotation, target.side)
    elif action == "repair_local":
        fixed_set = set(reviewer.expected) | set(reviewer.fixed)
        moved_any = False
        for ref in sorted(refs):
            if ref in fixed_set:
                continue
            current = trial.fp(ref)
            if _shift_clear_of_obstacles(trial, current, reviewer, fixed_set):
                moved_any = True
        if not moved_any:
            return None
    if action != "refresh_draft" and all(mechanical.same_pose(fp, board.fp(fp.ref)) for fp in trial.footprints):
        return None
    return trial


def _artwork_boxes_for_side(authored, side: str, owner: str) -> list:
    out = []
    for row in authored:
        if row["side"] != side or row["owner"] == owner:
            continue
        x0, y0, x1, y1 = row["bounds_mm"]
        label = (row.get("label") or "").lower()
        kind = row.get("kind") or ("barcode" if ("qr" in label or "barcode" in label) else "graphic")
        m = 1.05 if kind == "barcode" else 0.05
        out.append(box(x0 - m, y0 - m, x1 + m, y1 + m))
    return out


def _critical_blocked_keys(reviewer, board) -> set:
    out = set()
    for key in reviewer._blocked(board):
        if key in reviewer.base_exits:
            continue
        _ref, _pin, net = key
        claim = reviewer.intent.get("nets", {}).get(net, {}).get("class")
        if claim and model.is_hard(claim) and claim["value"] != "GENERAL":
            out.add(key)
    return out


def _shift_clear_of_obstacles(board, fp, reviewer, fixed_set):
    shape = legality.courtyard_polygon(fp)
    outline = legality.board_polygon(board)
    if shape is None or outline is None:
        return False
    body = box(*fp.body_bounds_mm) if fp.body_bounds_mm else shape
    artwork = _artwork_boxes_for_side(reviewer.basis.artwork, fp.side, fp.ref)
    neighbors = [legality.courtyard_polygon(other) for other in board.footprints
                 if other.ref != fp.ref and other.side == fp.side]
    clearance = max(board.rules.get("courtyard_clearance", 0.0), 0.05)
    before_blocked = _critical_blocked_keys(reviewer, board)
    orig_x, orig_y, orig_rot, orig_body = fp.x, fp.y, fp.rotation, fp.body_bounds_mm
    for step in (0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.5, 7.0, 9.0, 12.0):
        for dx, dy in ((-step, 0), (step, 0), (0, -step), (0, step),
                       (-step, -step), (-step, step), (step, -step), (step, step)):
            moved = translate(shape, xoff=dx, yoff=dy)
            ink_bounds = translate(body, xoff=dx, yoff=dy)
            if not outline.covers(moved):
                continue
            if any(ink_bounds.intersects(ink) or moved.intersects(ink) for ink in artwork):
                continue
            if any(neighbor is not None and moved.distance(neighbor) < clearance for neighbor in neighbors):
                continue
            fp.x, fp.y = round(orig_x + dx, 4), round(orig_y + dy, 4)
            fp.body_bounds_mm = ink_bounds.bounds
            if any(not f.fixed and fp.ref in f.refs for f in legality.check(board, fixed=fixed_set)):
                fp.x, fp.y, fp.rotation, fp.body_bounds_mm = orig_x, orig_y, orig_rot, orig_body
                continue
            after_blocked = _critical_blocked_keys(reviewer, board)
            if after_blocked - before_blocked:
                fp.x, fp.y, fp.rotation, fp.body_bounds_mm = orig_x, orig_y, orig_rot, orig_body
                continue
            if before_blocked and len(after_blocked) >= len(before_blocked) and not any(
                ink_bounds.intersects(ink) for ink in artwork
            ):
                # Keep searching if this part was moved to clear a critical pad exit and still blocks it.
                still_blocks = any(fp.ref in row["blocked_by"] for k, row in reviewer._blocked(board).items() if k in after_blocked)
                if still_blocks:
                    fp.x, fp.y, fp.rotation, fp.body_bounds_mm = orig_x, orig_y, orig_rot, orig_body
                    continue
            return True
    return False


def _repair_actions(findings):
    hard = {row["kind"] for row in findings if row["severity"] == "error"}
    if hard == {"stale_zone_fill"}:
        return ("refresh_draft",)
    # Every pose correction rewrites the draft with invalidated copper and performs genuine local
    # legal repairs without falling back to the untouched authored reference layout.
    return ("restore_protected", "repair_local")


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
