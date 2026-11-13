# SPDX-License-Identifier: MIT
"""Compile placement-only macros into compound solver parts."""

from __future__ import annotations

import math
from dataclasses import replace

from shapely import STRtree
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from ..eval.legality import board_polygon, check, courtyard_polygon
from ..eval.metrics import critical_loops_intent, crossings
from ..geometry import box_overlap_area, rotated_box, transform
from ..ir import Board, is_ground_net
from ..macro import Macro, MacroPart, Port, Transform, instantiate, to_local
from .problem import PlaceProblem


def compile_macros(pb: PlaceProblem, poses: list, groups: dict[int, list[int]]) -> dict[int, Macro]:
    """Keep disjoint same-side groups whose seed courtyards already fit together."""
    result = {}
    pads_by_net = pb.board.net_pads()
    classes = {c.name: c for c in pb.board.netclasses}
    rules = {net.name: classes.get(net.netclass) for net in pb.board.nets}
    clearance = max(0.0, pb.board.rules.get("courtyard_clearance", 0.0)) / 2
    used: set[int] = set()
    roots = set(groups)

    def bounds(i):
        px, py, pr = poses[i]
        a, b, c, d = pb.parts[i][:4]
        return rotated_box((a - clearance, b - clearance, c + clearance, d + clearance), (px, py), pr * 90)

    for root, members in sorted(groups.items()):
        if pb.parts[root][5] or pb.board.fp(pb.refs[root]).side != pb.sides[pb.refs[root]]:
            continue
        x, y, r = poses[root]
        side = pb.sides[pb.refs[root]]
        frame = Transform(x, y, r * 90, side)
        accepted = [root]
        boxes = [bounds(root)]
        for i in members:
            ref = pb.refs[i]
            fp = pb.board.fp(ref)
            if (i in roots or i in used or pb.parts[i][5] or pb.sides[ref] != side or fp.side != side or not fp.uuid):
                continue
            candidate_box = bounds(i)
            if any(box_overlap_area(candidate_box, other) > 1e-8 for other in boxes):
                continue
            boxes.append(candidate_box)
            accepted.append(i)
        if len(accepted) < 2 or not pb.board.fp(pb.refs[root]).uuid:
            continue
        parts = []
        for i in accepted:
            ref = pb.refs[i]
            px, py, pr = poses[i]
            lx, ly = to_local((px, py), frame, False)
            parts.append(MacroPart(ref, pb.board.fp(ref).uuid, lx, ly, (pr - r) * 90, "same"))
        macro = Macro(f"placement:{pb.refs[root]}", side, pb.board.layer_count, parts)
        by_ref = {p.ref: p for p in parts}
        nets = {n for ref in by_ref for n in pb.board.fp(ref).nets}
        for net in sorted(nets):
            pads = pads_by_net[net]
            if is_ground_net(net) or not any(fp.ref not in by_ref for fp, _ in pads):
                continue
            rule = rules.get(net) or classes.get("Default")
            if not rule or rule.track_width <= 0:
                continue
            candidates = [transform(pad.local, (part.x, part.y), part.rotation)
                          for fp, pad in pads if (part := by_ref.get(fp.ref))]
            if candidates:
                px, py = max(candidates, key=lambda p: (math.hypot(*p), p))
                width = rule.track_width + 2 * max(0.0, rule.clearance)
                macro.ports.append(Port(net, "pad", f"{side}.Cu", px, py,
                                        math.degrees(math.atan2(-py, px)) % 360, width))
        result[root] = macro
        used.update(accepted)
    return result


def collapse(pb: PlaceProblem, macros: dict[int, Macro]) -> tuple[PlaceProblem, list[int]]:
    by_ref = {ref: i for i, ref in enumerate(pb.refs)}
    owner = {by_ref[part.ref]: (root, part) for root, macro in macros.items() for part in macro.parts}
    retained = [i for i in range(len(pb.parts)) if i not in owner or i in macros]
    indexes = {i: j for j, i in enumerate(retained)}
    parts = [pb.parts[i] for i in retained]
    for root, macro in macros.items():
        boxes = [rotated_box(pb.parts[by_ref[p.ref]][:4], (p.x, p.y), p.rotation) for p in macro.parts]
        bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        parts[indexes[root]] = (*bounds, *pb.parts[root][4:])
    pins = []
    for i, x, y in pb.pins:
        if i in owner:
            root, part = owner[i]
            x, y = transform((x, y), (part.x, part.y), part.rotation)
            i = root
        pins.append((indexes[i], x, y))
    return replace(pb, parts=parts, pins=pins, refs=[pb.refs[i] for i in retained]), retained


def expand(pb: PlaceProblem, poses: list, retained: list[int], macros: dict[int, Macro]) -> list:
    out = pb.poses()
    indexes = {ref: i for i, ref in enumerate(pb.refs)}
    for i, pose in zip(retained, poses, strict=True):
        out[i] = pose
        if i in macros:
            x, y, r = pose
            for part in instantiate(macros[i], Transform(x, y, r * 90, macros[i].home_side)).poses.values():
                out[indexes[part["ref"]]] = (part["x"], part["y"], round(part["rotation"] / 90) % 4)
    return out


def corridors(board: Board, macros: dict[int, Macro]) -> dict:
    """Audit straight projected corridors between same-side macro ports."""
    groups = {p.ref: m.template for m in macros.values() for p in m.parts}
    shapes = [(fp, shape) for fp in board.footprints if (shape := courtyard_polygon(fp)) is not None]
    tree = STRtree([shape for _, shape in shapes])
    outline = board_polygon(board)
    layer_names = {layer.name: layer.canonical for layer in board.copper_layers}
    keepouts = {side: unary_union([Polygon(ring).buffer(0) for area in board.rule_areas
                                  if area["keepout"].get("tracks") and any(
                                      layer_names.get(layer, layer) == f"{side}.Cu" for layer in area["layers"])
                                  for ring in area["polygons"] if len(ring) >= 3]) for side in ("F", "B")}
    by_net = {}
    for macro in macros.values():
        anchor = board.fp(macro.parts[0].ref)
        instance = instantiate(macro, Transform(anchor.x, anchor.y, anchor.rotation, anchor.side))
        for port in instance.ports:
            by_net.setdefault((port.net, anchor.side), []).append((macro.template, port))
    rows = []
    for (net, side), local in sorted(by_net.items()):
        if len(local) != 2:
            continue
        (a, p1), (b, p2) = local
        width = max(p1.min_channel_mm, p2.min_channel_mm)
        strip = LineString(((p1.x, p1.y), (p2.x, p2.y))).buffer(width / 2)
        blocked = sorted({shapes[i][0].ref for i in tree.query(strip, predicate="intersects")
                          if shapes[i][0].side == side and groups.get(shapes[i][0].ref) not in (a, b)})
        rows.append({"net": net, "side": side, "blocks": [a, b], "width_mm": width,
                     "endpoints_mm": [[p1.x, p1.y], [p2.x, p2.y]],
                     "blocked_by": blocked, "track_keepout": strip.intersects(keepouts[side]),
                     "outside_outline": outline is None or not outline.covers(strip)})
    return {"total": len(rows), "clear": sum(not (r["blocked_by"] or r["outside_outline"] or r["track_keepout"])
                                             for r in rows), "rows": rows}


def _corridor_objective(audit: dict) -> tuple[set[tuple], int]:
    clear = set()
    blockers = 0
    for row in audit["rows"]:
        if row["track_keepout"] or row["outside_outline"]:
            continue
        if row["blocked_by"]:
            blockers += len(row["blocked_by"])
        else:
            clear.add((row["net"], row["side"], *row["blocks"]))
    return clear, blockers


def clear_blockers(board: Board, macros: dict[int, Macro], fixed: set[str], intent: dict, audit: dict,
                   *, max_trials: int) -> tuple[Board, dict]:
    """Improve the blocked-corridor objective with bounded moves of independent footprints."""
    original_clear = audit["clear"]
    protected = fixed | {p.ref for macro in macros.values() for p in macro.parts}
    refs = sorted({ref for row in audit["rows"] if not (row["track_keepout"] or row["outside_outline"])
                   for ref in row["blocked_by"]} - protected)
    if not refs:
        return board, {**audit, "before_clear": original_clear, "trials": 0, "moved": []}
    board = board.copy()
    tail = critical_loops_intent(board, intent)["resolved_decap_pin_distance"]
    cross = crossings(board)["count"]
    clear, blockers = _corridor_objective(audit)
    before_blockers = blockers
    trials, moved = 0, []
    for ref in refs:
        fp = board.fp(ref)
        x, y = fp.position
        width = max((row["width_mm"] for row in audit["rows"] if ref in row["blocked_by"]), default=0)
        if not width:
            continue
        x0, y0, x1, y1 = fp.box()
        step = max(width, min(x1 - x0, y1 - y0) / 2)
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step),
                       (2 * step, 0), (-2 * step, 0), (0, 2 * step), (0, -2 * step)):
            if trials >= max_trials:
                break
            trials += 1
            fp.x, fp.y = round(x + dx, 4), round(y + dy, 4)
            proposed = corridors(board, macros)
            next_clear, next_blockers = _corridor_objective(proposed)
            improves = clear <= next_clear and (clear != next_clear or next_blockers < blockers)
            if improves and not any(not f.fixed for f in check(board, fixed=fixed)):
                distance = critical_loops_intent(board, intent)["resolved_decap_pin_distance"]
                next_cross = crossings(board)["count"]
                if (distance["n"] >= tail["n"] and distance.get("p90_mm", math.inf) <= tail.get("p90_mm", math.inf)
                        and next_cross <= cross):
                    audit, tail, cross = proposed, distance, next_cross
                    clear, blockers = next_clear, next_blockers
                    moved.append(ref)
                    break
            fp.x, fp.y = x, y
        if trials >= max_trials:
            break
    return board, {**audit, "before_clear": original_clear, "before_blockers": before_blockers,
                   "after_blockers": blockers, "trials": trials, "moved": moved}
