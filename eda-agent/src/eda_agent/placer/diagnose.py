# SPDX-License-Identifier: MIT
"""Evidence from a placement or route-check run, in board millimetres."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import silkscreen as silk_mod
from .eval import legality, metrics
from .eval.score import DECAP_BAD_MM
from .intent.resolve import IntentError, resolve
from .io.kicad import read_board
from .place import escape, problem

_LAYER = re.compile(r"\bon ([\w.]+\.(?:Cu|Mask|SilkS))\b")
_PIN = re.compile(r"\b[Pp]ad (\S+) \[([^\]]*)\] of (\S+)")
_DISTANCE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*mm")
# KiCad 10 applies its extended per-check limit to unconnected items.
_KICAD_OPEN_LINK_LIMIT = 499


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _points(items: list[dict]) -> list[dict]:
    points = []
    for item in items:
        data = item.get("pos") or {}
        if "x" not in data or "y" not in data:
            continue
        description = item.get("description", "")
        layer = _LAYER.search(description)
        pin = _PIN.search(description)
        points.append({"x_mm": data["x"], "y_mm": data["y"], "layer": layer[1] if layer else None,
                       "uuid": item.get("uuid"),
                       "ref": pin[3] if pin else None, "pad": pin[1] if pin else None,
                       "net": pin[2] if pin else None, "description": description})
    return points


def _key(row: dict) -> tuple:
    return row["type"], tuple(sorted(item.get("description", "") for item in row.get("items", [])))


def _drc_rows(report: dict, board_path: Path, report_path: Path) -> list[dict]:
    run = report_path.parent
    native = board_path.with_suffix(".drc.json")
    if not native.is_file():
        # Fall back to route_check's final KiCad DRC JSON (sanitized or routed) so dangling warnings surface.
        rc_stage = (report.get("stages") or {}).get("route_check") or {}
        rc_rep_path = Path(rc_stage["run_report"]) if rc_stage.get("run_report") else report_path
        work_dir = rc_rep_path.parent / "work"
        if work_dir.is_dir():
            sanitized = sorted(work_dir.glob("drc_sanitized_*.json"))
            candidate = sanitized[-1] if sanitized else (work_dir / "drc_routed.json")
            if candidate.is_file():
                native = candidate
    comparison = run / f"{board_path.stem.removesuffix('.placed')}.ref_unrouted.drc.json"
    after = _load(native) if native.is_file() else None
    before = _load(comparison) if comparison.is_file() else None
    if any(data is not None and data.get("coordinate_units") != "mm" for data in (before, after)):
        raise ValueError("Native DRC reports must use millimetres")
    if after is not None and before is not None:
        baseline = Counter(_key(v) for v in before.get("violations", []))
        rows = []
        for row in after.get("violations", []):
            key = _key(row)
            if baseline[key]:
                baseline[key] -= 1
            elif row["severity"] == "error" or row["type"] in ("isolated_copper", "track_dangling", "via_dangling"):
                rows.append({**row, "source": str(native), "baseline_drc": str(comparison)})
        # KiCad can select different endpoints for the same disconnected islands.
        # Compare connection counts per net and show a representative current pair.
        def by_net(data):
            result = {}
            for row in data.get("unconnected_items", []):
                nets = {point["net"] for point in _points(row.get("items", [])) if point["net"]}
                if len(nets) == 1:
                    result.setdefault(nets.pop(), []).append(row)
            return result

        previous = by_net(before)
        positive = []
        for net, current in sorted(by_net(after).items()):
            old_count = len(previous.get(net, []))
            if len(current) > old_count:
                positive.append({**current[0], "net": net, "measured_count": len(current), "baseline_count": old_count,
                                 "description": "Open links increased after placement and zone refill; endpoints are illustrative",
                                 "source": str(native), "baseline_drc": str(comparison)})
        return rows + sorted(positive, key=lambda row: row["baseline_count"] - row["measured_count"])
    if after is not None:
        dangling_rows = [
            {**row, "source": str(native)} for row in after.get("violations", [])
            if row["type"] in ("track_dangling", "via_dangling")
        ]
        # Prefer attributed router/new errors when route_check report already separated baseline errors.
        if report.get("drc"):
            err_rows = [{**row, "source": str(report_path)} for key in ("router_caused", "new_non_routing")
                        for row in report.get("drc", {}).get(key, [])]
            return err_rows + dangling_rows
        err_rows = [{**row, "source": str(native)} for row in after.get("violations", [])
                    if row["severity"] == "error"]
        return err_rows + dangling_rows
    return [{**row, "source": str(report_path)} for key in ("router_caused", "new_non_routing")
            for row in report.get("drc", {}).get(key, [])]


def _suggest_action(finding: dict) -> dict | None:
    """Emit a Law-P1-safe symbolic suggested action (zero raw coordinates) for an actionable finding."""
    kind = finding.get("kind") or ""
    pts = finding.get("points") or []
    refs = list(finding.get("refs") or [])
    if not refs:
        refs = [p["ref"] for p in pts if p.get("ref")]
    if kind in ("decap_distance", "lint_decap_loop") and len(refs) >= 2:
        return {
            "tool": "placer__refine",
            "change": {"ref": refs[0], "toward_ref": refs[1], "step_mm": 1.0, "confidence": 0.85},
        }
    if kind == "pad_exit_blocked":
        blocked_by = finding.get("blocked_by") or []
        fp_ref = refs[0] if refs else None
        blocker = blocked_by[0] if isinstance(blocked_by, list) and blocked_by else None
        if fp_ref and isinstance(blocker, str) and blocker != fp_ref:
            return {
                "tool": "placer__refine",
                "change": {"ref": blocker, "away_from_ref": fp_ref, "step_mm": 0.8, "confidence": 0.80},
            }
    if kind in ("lint_fb_near_sw", "lint_fb_under_inductor") and len(refs) >= 2:
        return {
            "tool": "placer__refine",
            "change": {"ref": refs[0], "away_from_ref": refs[1], "step_mm": 1.5, "confidence": 0.85},
        }
    if kind == "unrouted":
        uniq = sorted({p["ref"] for p in pts if p.get("ref")})
        if uniq:
            return {"tool": "placer__place_cluster", "refs": uniq, "preserve_routing": "outside_cluster"}
    return None


def diagnose(
    run: str,
    limit: int = 8,
    *,
    kinds: list[str] | None = None,
    offset: int = 0,
) -> tuple[dict, dict]:
    report_path = Path(run).resolve()
    if report_path.is_dir():
        report_path /= "run_report.json"
    report = _load(report_path)
    artifacts = report["artifacts"]
    board_path = Path(artifacts.get("routed_board") or artifacts.get("placed_board") or report["inputs"]["board"]["path"])
    source = str(report_path)
    board = read_board(board_path)
    footprints = {fp.ref: fp for fp in board.footprints}
    findings = []
    for row in _drc_rows(report, board_path, report_path):
        desc = row["description"]
        distances = [float(d) for d in _DISTANCE.findall(desc)]
        finding = {"kind": row["type"], "description": desc, "points": _points(row.get("items", [])),
                   "source": row["source"], "severity": row["severity"]}
        if "baseline_drc" in row:
            finding["baseline_drc"] = row["baseline_drc"]
        if "measured_count" in row:
            finding.update(net=row["net"], measured_count=row["measured_count"], baseline_count=row["baseline_count"])
        if len(distances) == 2 and "actual" in desc.lower():
            finding.update(limit_mm=distances[0], measured_mm=distances[1])
        findings.append(finding)

    blocked = report.get("metrics", {}).get("fanout_escape", {}).get("blocked", [])
    if report.get("kind") in {"place_board", "place_cluster", "place_refinement"}:
        saved = report.get("inputs", {}).get("intent") or {}
        try:
            origin = report.get("inputs", {}).get("board", {}).get("path")
            basis = read_board(origin) if origin and Path(origin).is_file() else board
            intent, info = resolve(basis, saved.get("path"))
        except (OSError, IntentError):
            intent, info = {}, {}
        if info and (not saved.get("sha256") or info["sha256"] == saved["sha256"]):
            if any("start_mm" not in row for row in blocked):
                original_sides = {fp.ref: fp.side for fp in basis.footprints}
                flipped = {fp.ref for fp in board.footprints if original_sides.get(fp.ref, fp.side) != fp.side}
                envelopes = escape.envelopes(basis, intent, {fp.ref: problem.local_box(fp) for fp in basis.footprints})
                audit = escape.audit(board, {ref: rows for ref, rows in envelopes.items() if ref in footprints}, flipped=flipped)
                current = {(row["ref"], row["pin"], row["net"]): row for row in audit["blocked"]}
                blocked = [{**current.get((row["ref"], row["pin"], row["net"]), {}), **row} for row in blocked]
            witnesses = {}
            metrics.critical_loops_intent(board, intent, witnesses=witnesses)
            for witness in sorted(witnesses.values(), key=lambda row: -row["distance_mm"]):
                if witness["distance_mm"] > DECAP_BAD_MM:
                    findings.append({"kind": "decap_distance", "source": source, "severity": "warning",
                                     "description": "Resolved capacitor exceeds the placement scoring reference distance",
                                     "points": witness["points"], "measured_mm": witness["distance_mm"],
                                     "score_reference_mm": DECAP_BAD_MM})
    for row in blocked:
        fp = footprints.get(row["ref"])
        if fp is None:
            continue
        pad = next((pad for pad in fp.pads if pad.number == row["pin"] and pad.net == row["net"]), None)
        if pad is None:
            continue
        x, y = fp.pad_world(pad)
        evidence = {key: row[key] for key in ("start_mm", "end_mm", "track_envelope_mm", "via_envelope_mm") if key in row}
        findings.append({"kind": "pad_exit_blocked", "description": "Outward escape envelope intersects an obstacle",
                         "points": [{"x_mm": x, "y_mm": y, "layer": row.get("layer", f"{fp.side}.Cu"),
                                     "ref": fp.ref, "pad": pad.number, "net": pad.net}],
                         "blocked_by": row["blocked_by"], "outside_outline": row["outside_outline"],
                         "keepout": row["keepout"], "source": source, **evidence})

    for row in report.get("failures", []):
        if row.get("stage") != "legality":
            continue
        points = [{"x_mm": fp.x, "y_mm": fp.y, "layer": f"{fp.side}.Cu", "ref": ref, "pad": None}
                  for ref in row["refs"] if (fp := footprints.get(ref)) is not None]
        findings.append({"kind": row["cause"], "description": "Placement legality finding", "points": points,
                         "source": source, "severity": "error"})

    route_path = (report.get("stages", {}).get("route_check") or {}).get("run_report")
    route_report = _load(Path(route_path)) if route_path else report
    if route_path:
        routed_path = Path(route_report["artifacts"]["routed_board"])
        for row in _drc_rows(route_report, routed_path, Path(route_path)):
            findings.append({"kind": row["type"], "description": row["description"], "severity": row["severity"],
                             "points": _points(row.get("items", [])), "source": route_path})
    route_metrics = route_report.get("metrics", {})
    stop = route_metrics.get("router_stop")
    settled = bool(route_metrics.get("settled", stop in {"completed", "converged"}))
    for row in route_report.get("failures", []):
        if row.get("stage") != "S6c":
            continue
        findings.append({"kind": "unrouted", "net": row["net"], "cause_hint": row["cause"],
                         "description": "Connection still open in this route output", "points": _points(row["evidence"]),
                         "source": route_path or source, "run_stop": stop, "cause_confirmed": False})

    from .route.bga_escape import check_hdi_rule_conflicts

    hdi_conflicts = check_hdi_rule_conflicts(board)
    for hc in hdi_conflicts:
        fp = footprints.get(hc["ref"])
        pts = [{"x_mm": fp.x, "y_mm": fp.y, "layer": f"{fp.side}.Cu", "ref": fp.ref, "pad": None}] if fp else []
        findings.append({
            "kind": "hdi_rule_conflict",
            "severity": "error",
            "description": hc["message"],
            "points": pts,
            "source": source,
            "board_hole_clearance_mm": hc["board_hole_clearance_mm"],
            "max_possible_hole_clearance_mm": hc["max_possible_hole_clearance_mm"],
            "recommended_hole_clearance_mm": hc["recommended_hole_clearance_mm"],
        })

    silk_audit = silk_mod.audit_board(board)
    if board.silkscreen_texts:
        silk_descriptions = {
            "fab_text_outside_board": "Assembly text on F.Fab/B.Fab extends outside the board outline; normalize or resize Fab text",
            "fab_text_overlap": "Assembly texts on F.Fab/B.Fab overlap each other; resize Refs inside component bodies and hide cluttering Fab Value strings",
            "fab_text_oversized_for_body": "Assembly text on F.Fab/B.Fab exceeds its component body; size strictly within component body per IPC-A-610",
            "text_mirror_mismatch": "Visible text has wrong mirror state for its side (mirrored on Front or non-mirrored on Back)",
            "silk_text_outside_board": "Overlay text extends outside the board outline; resize to board proportions or transfer to F.Fab/B.Fab",
            "silk_oversized_for_miniature_board": "Overlay reference is oversized for a miniature board; use dense 0.80 mm height or transfer to F.Fab/B.Fab",
            "silk_under_component_body": "Overlay reference sits under a component body; relocate outside body or transfer to F.Fab/B.Fab",
            "silk_pad_clearance": "Overlay text violates minimum pad/mask clearance; relocate or transfer to F.Fab/B.Fab",
            "silk_text_overlap": "Overlay text overlaps another silkscreen item; relocate or transfer to F.Fab/B.Fab",
            "missing_component_legend": "Component has no visible reference on Overlay (Silkscreen) or Assembly (F.Fab/B.Fab)",
        }
        for sf in silk_audit.get("findings", []):
            ref = sf.get("ref")
            fp = footprints.get(ref) if ref else None
            pos = sf.get("pos_mm")
            pts = []
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                pts.append({"x_mm": float(pos[0]), "y_mm": float(pos[1]), "layer": sf.get("layer"),
                            "ref": ref if ref != "board" else None, "pad": None, "text": sf.get("text")})
            elif fp is not None:
                pts.append({"x_mm": fp.x, "y_mm": fp.y, "layer": sf.get("layer") or f"{fp.side}.Silkscreen",
                            "ref": fp.ref, "pad": None, "text": sf.get("text")})
            rule = sf["rule"]
            entry = {
                "kind": rule,
                "severity": sf["severity"],
                "description": silk_descriptions.get(rule, f"Silkscreen/assembly DFM finding ({rule})"),
                "points": pts,
                "source": source,
            }
            for k in ("measured_mm", "limit_mm", "recommended_mm", "bbox_mm", "ratio"):
                if k in sf:
                    entry[k] = sf[k]
            findings.append(entry)

    # Surface both critical and warning F6 electrical lint findings, DRU untranslated rules,
    # and top PathFinder congestion hotspots (below DRC/unrouted priority).
    lint_block = (report.get("metrics") or {}).get("lint") or {}
    for lf in lint_block.get("findings") or []:
        sev = lf.get("severity")
        if sev not in ("critical", "warning"):
            continue
        loc = lf.get("location_mm")
        pts = ([{"x_mm": float(loc[0]), "y_mm": float(loc[1]), "layer": None,
                 "ref": (lf.get("refs") or [None])[0], "pad": None, "net": lf.get("net")}]
               if isinstance(loc, (list, tuple)) and len(loc) == 2 else [])
        findings.append({
            "kind": f"lint_{lf['rule']}",
            "severity": "error" if sev == "critical" else "warning",
            "description": lf.get("detail") or lf.get("message") or f"Electrical lint finding ({lf['rule']})",
            "points": pts,
            "net": lf.get("net"),
            "refs": lf.get("refs") or [],
            "source": source,
        })
    dru_stage = (route_report.get("stages") or {}).get("dsn_dru_compiled") or {}
    for rule_name in (dru_stage.get("untranslated") or [])[:6]:
        findings.append({
            "kind": "dru_untranslated",
            "severity": "warning",
            "description": f"Custom .kicad_dru rule '{rule_name}' is not natively expressible in Specctra DSN; verify against KiCad DRC",
            "points": [],
            "rule_name": rule_name,
            "source": route_path or source,
        })
    pf_block = (report.get("metrics") or {}).get("pathfinder") or {}
    for hs in (pf_block.get("hotspots") or [])[:3]:
        if float(hs.get("overflow") or 0.0) <= 0.0:
            continue
        findings.append({
            "kind": "pathfinder_hotspot",
            "severity": "warning",
            "description": f"PathFinder routing congestion hotspot (overflow={hs['overflow']:.2f})",
            "points": [{"x_mm": float(hs["x_mm"]), "y_mm": float(hs["y_mm"]), "layer": None, "ref": None, "pad": None}],
            "overflow": hs["overflow"],
            "source": source,
        })

    counts = Counter(row["kind"] for row in findings)
    open_link_deltas = [{"net": row["net"], "before": row["baseline_count"], "after": row["measured_count"]}
                        for row in findings if row["kind"] == "unconnected_items" and "baseline_count" in row]
    # Show each observed type before using the remaining slots on repeated findings.
    priority = {"shorting_items": 0, "clearance": 1, "copper_edge_clearance": 1, "solder_mask_bridge": 2,
                "pad_exit_blocked": 2, "unconnected_items": 3, "unrouted": 3, "isolated_copper": 4}
    silk_kinds = {
        "silk_text_height", "silk_stroke_width", "silk_stroke_ratio_too_thin", "silk_stroke_ratio_too_thick",
        "silk_pad_clearance", "silk_under_component_body", "silk_text_overlap", "silk_text_outside_board",
        "silk_oversized_for_miniature_board", "fab_text_outside_board", "fab_text_overlap",
        "fab_text_oversized_for_body", "text_mirror_mismatch", "missing_component_legend",
    }

    def _priority(row: dict) -> int:
        kind = row["kind"]
        if kind in priority:
            return priority[kind]
        if kind in silk_kinds:
            return 7 if row.get("severity") == "error" else 8
        if kind.startswith("lint_"):
            return 6 if row.get("severity") == "error" else 7
        if kind == "dru_untranslated":
            return 8
        if kind == "pathfinder_hotspot":
            return 9
        return 5 if row.get("severity") == "error" else 6

    findings.sort(key=_priority)
    if kinds:
        allowed = set(kinds)
        findings = [row for row in findings if row["kind"] in allowed]
    unique_types = set()
    first, rest = [], []
    for row in findings:
        (rest if row["kind"] in unique_types else first).append(row)
        unique_types.add(row["kind"])
    ordered_findings = first + rest
    page_offset = max(0, int(offset))
    page = ordered_findings[page_offset : page_offset + limit]
    suggested_actions: list[dict] = []
    for row in page:
        act = _suggest_action(row)
        if act is not None:
            row["suggested_action"] = act
            suggested_actions.append({"kind": row["kind"], **act})
    unrouted = route_metrics.get("unrouted")
    localized = counts.get("unrouted", 0)
    unlocalized = max(0, unrouted - localized) if unrouted is not None else None
    locations_status = None
    if unrouted is not None:
        locations_status = "complete" if localized == unrouted else "count_mismatch"
        kicad_version = route_report.get("tools", {}).get("kicad", {}).get("version", "")
        if unlocalized and localized == _KICAD_OPEN_LINK_LIMIT and kicad_version.startswith("10."):
            locations_status = "kicad_drc_limit"
    value = {"run": source, "board": str(board_path), "finding_counts": dict(counts),
             "image_scope": "Placement IR with board outline, pads, tracks, mask artwork and zone boundaries; zone fill is omitted",
             "open_link_deltas": open_link_deltas,
             "total_findings": len(ordered_findings),
             "offset": page_offset,
             "reported_findings": len(page),
             "suggested_actions": suggested_actions,
             "silkscreen": silk_audit,
             "route": {"status": ("settled" if settled else "inconclusive") if stop else "not_run",
                       "stop": stop, "unrouted": unrouted, "localized_open_links": localized,
                       "unlocalized_open_links": unlocalized, "locations_status": locations_status},
             "findings": page}
    boards = {source: board}
    if route_path and any(finding["source"] == route_path for finding in value["findings"]):
        boards[route_path] = read_board(routed_path)
    for finding in value["findings"]:
        actual_board = boards.get(finding["source"], board)
        for point in finding["points"]:
            if point.get("layer") or not point.get("pad"):
                continue
            matches = [fp for fp in actual_board.footprints if fp.ref == point["ref"]]
            if len(matches) == 1:
                pad = next((pad for pad in matches[0].pads if pad.number == point["pad"]), None)
                if pad is not None:
                    point["layers"] = sorted(f"{side}.Cu" for side in legality.pad_copper_sides(matches[0], pad))
    return value, boards
