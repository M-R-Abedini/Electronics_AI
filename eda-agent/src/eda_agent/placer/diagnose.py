# SPDX-License-Identifier: MIT
"""Evidence from a placement or route-check run, in board millimetres."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

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
                       "ref": pin[3] if pin else None, "pad": pin[1] if pin else None,
                       "net": pin[2] if pin else None, "description": description})
    return points


def _key(row: dict) -> tuple:
    return row["type"], tuple(sorted(item.get("description", "") for item in row.get("items", [])))


def _drc_rows(report: dict, board_path: Path, report_path: Path) -> list[dict]:
    run = report_path.parent
    native = board_path.with_suffix(".drc.json")
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
            elif row["severity"] == "error" or row["type"] == "isolated_copper":
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
        return [{**row, "source": str(native)} for row in after.get("violations", [])
                if row["severity"] == "error"]
    return [{**row, "source": str(report_path)} for key in ("router_caused", "new_non_routing")
            for row in report.get("drc", {}).get(key, [])]


def diagnose(run: str, limit: int = 8) -> tuple[dict, dict]:
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

    counts = Counter(row["kind"] for row in findings)
    open_link_deltas = [{"net": row["net"], "before": row["baseline_count"], "after": row["measured_count"]}
                        for row in findings if row["kind"] == "unconnected_items" and "baseline_count" in row]
    # Show each observed type before using the remaining slots on repeated findings.
    priority = {"shorting_items": 0, "clearance": 1, "copper_edge_clearance": 1, "solder_mask_bridge": 2,
                "pad_exit_blocked": 2, "unconnected_items": 3, "unrouted": 3, "isolated_copper": 4}
    findings.sort(key=lambda row: priority.get(row["kind"], 5 if row.get("severity") == "error" else 6))
    unique_types = set()
    first, rest = [], []
    for row in findings:
        (rest if row["kind"] in unique_types else first).append(row)
        unique_types.add(row["kind"])
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
             "image_scope": "Placement IR with board outline, pads, tracks and zone boundaries; zone fill is omitted",
             "open_link_deltas": open_link_deltas,
             "reported_findings": min(len(findings), limit),
             "route": {"status": ("settled" if settled else "inconclusive") if stop else "not_run",
                       "stop": stop, "unrouted": unrouted, "localized_open_links": localized,
                       "unlocalized_open_links": unlocalized, "locations_status": locations_status},
             "findings": (first + rest)[:limit]}
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
