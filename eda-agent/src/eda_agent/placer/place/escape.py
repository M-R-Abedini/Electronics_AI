# SPDX-License-Identifier: MIT
"""Placement space for outward pad escapes, using the board's routing rules."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from shapely import STRtree
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

from ..eval.legality import board_polygon, courtyard_polygon
from ..geometry import Box, Point, rotated_box, transform
from ..intent import model
from ..ir import Board, is_ground_net

MIN_CONNECTED_PADS = 8
_DIRECTIONS = ((-1, 0), (0, -1), (1, 0), (0, 1))


@dataclass(frozen=True)
class Escape:
    pin: str
    net: str
    start: Point
    end: Point
    width: float
    via_space: float


def envelopes(board: Board, intent: dict, boxes: dict[str, Box]) -> dict[str, list[Escape]]:
    """Propose short exits from peripheral SMD pads of active ICs."""
    classes = {row.name: row for row in board.netclasses}
    rules = {net.name: classes.get(net.netclass) for net in board.nets}
    connected = {net for net, pads in board.net_pads().items() if len({fp.ref for fp, _ in pads}) >= 2}
    planes = {net for layer in board.stackup.roles for net in layer.nets}
    out: dict[str, list[Escape]] = {}
    for fp in board.footprints:
        role = intent.get("parts", {}).get(fp.ref, {}).get("role")
        if model.is_uncertain(role) or role["value"] not in ("ic", "converter_ic", "linear_regulator", "power_switch", "connector"):
            continue
        pads = [p for p in fp.pads if p.kind == "smd" and p.net in connected and not is_ground_net(p.net)]
        min_pads = 12 if role["value"] == "connector" else MIN_CONNECTED_PADS
        if len(pads) < min_pads:
            continue
        x0, y0, x1, y1 = boxes[fp.ref]
        for pad in pads:
            rule = rules.get(pad.net) or classes.get("Default")
            if rule is None or rule.track_width <= 0 or rule.via_diameter <= 0:
                continue
            x, y = pad.local
            w, h = pad.size
            px0, py0, px1, py1 = rotated_box((-w / 2, -h / 2, w / 2, h / 2), (x, y), pad.rotation)
            gaps = (px0 - x0, py0 - y0, x1 - px1, y1 - py1)
            side = min(range(4), key=lambda i: gaps[i])
            ux, uy = _DIRECTIONS[side]
            to_edge = (x - x0, y - y0, x1 - x, y1 - y)[side]
            # Pads well inside a package need a BGA escape strategy, not an exterior reservation;
            # only relax for tiny fine-pitch BGA outer balls (<= 0.30 mm) where courtyard margin exceeds ball diameter.
            gap_lim = max(pad.size) * 2.0 if max(pad.size) <= 0.30 else max(pad.size)
            if gaps[side] > gap_lim:
                continue
            clearance = max(rule.clearance, 0.0)
            width = rule.track_width + 2 * clearance
            via_space = rule.via_diameter + 2 * clearance
            depth = via_space if pad.net in planes else max(via_space, 2 * width)
            start = (x + ux * to_edge, y + uy * to_edge)
            end = (start[0] + ux * depth, start[1] + uy * depth)
            out.setdefault(fp.ref, []).append(Escape(str(pad.number), pad.net, start, end, width, via_space))
    return out


def reserved_box(bounds: Box, exits: list[Escape]) -> Box:
    x0, y0, x1, y1 = bounds
    for exit in exits:
        x, y = exit.end
        radius = exit.via_space / 2
        x0, y0, x1, y1 = min(x0, x - radius), min(y0, y - radius), max(x1, x + radius), max(y1, y + radius)
    return x0, y0, x1, y1


def select_reservations(exits: dict[str, list[Escape]]) -> dict[str, list[Escape]]:
    """Reserve the most crowded bank of each footprint, where adjacent exits compete for space."""
    selected = {}
    for ref, rows in exits.items():
        banks: dict[Point, list[Escape]] = {}
        for row in rows:
            dx, dy = row.end[0] - row.start[0], row.end[1] - row.start[1]
            direction = (float(dx > 0) - float(dx < 0), float(dy > 0) - float(dy < 0))
            banks.setdefault(direction, []).append(row)
        crowded = []
        for (dx, _), bank in banks.items():
            axis = 1 if dx else 0
            ordered = sorted(bank, key=lambda row: row.start[axis])
            for a, b in pairwise(ordered):
                pitch = b.start[axis] - a.start[axis]
                if pitch < (a.via_space + b.via_space) / 2:
                    crowded.append((pitch / ((a.via_space + b.via_space) / 2), a, b, bank))
        if crowded:
            _, _, _, bank = min(crowded, key=lambda row: (row[0], -len(row[3]), row[1].pin, row[2].pin))
            selected[ref] = bank
    return selected


_KEEPOUTS_CACHE: dict[tuple, dict] = {}
_ESCAPE_GEOMS_CACHE: dict[tuple, list] = {}


def audit(board: Board, exits: dict[str, list[Escape]], *, flipped: set[str] | None = None) -> dict:
    """Count clear exterior exits against the actual outline and nearby courtyards."""
    if not exits:
        return {"total": 0, "clear": 0, "blocked": []}
    outline = board_polygon(board)
    outline_prep = prep(outline) if outline is not None else None
    shapes = {side: [(fp.ref, geom) for fp in board.footprints if fp.side == side
                     and (geom := courtyard_polygon(fp)) is not None] for side in ("F", "B")}
    trees = {side: STRtree([geom for _, geom in rows]) for side, rows in shapes.items()}
    ko_key = (board.name, len(board.rule_areas), tuple(r.get("uuid") for r in board.rule_areas))
    if ko_key in _KEEPOUTS_CACHE:
        keepouts = _KEEPOUTS_CACHE[ko_key]
    else:
        names = {layer.name: layer.canonical for layer in board.copper_layers}
        keepouts = {(side, kind): unary_union([Polygon(ring).buffer(0) for row in board.rule_areas
                                     if row["keepout"].get(kind)
                                     and any(names.get(layer, layer) == f"{side}.Cu" for layer in row["layers"])
                                     for ring in row["polygons"] if len(ring) >= 3])
                    for side in ("F", "B") for kind in ("tracks", "vias")}
        if len(_KEEPOUTS_CACHE) > 256:
            _KEEPOUTS_CACHE.clear()
        _KEEPOUTS_CACHE[ko_key] = keepouts
    blocked = []
    flipped = flipped or set()
    total = sum(map(len, exits.values()))
    for ref, rows in exits.items():
        fp = board.fp(ref)

        mirror = -1 if ref in flipped else 1
        g_key = (board.name, ref, fp.x, fp.y, fp.rotation, fp.side, mirror, len(rows))
        if g_key in _ESCAPE_GEOMS_CACHE:
            exit_geoms = _ESCAPE_GEOMS_CACHE[g_key]
        else:
            exit_geoms = []
            for exit in rows:
                start, end = (transform((x, y * mirror), fp.position, fp.rotation) for x, y in (exit.start, exit.end))
                x, y = end
                radius = exit.via_space / 2
                strip = LineString([start, end]).buffer(exit.width / 2, cap_style="flat")
                via = box(x - radius, y - radius, x + radius, y + radius)
                region = strip.union(via)
                outside = outline_prep is None or not outline_prep.covers(region)
                keepout = any(not keepouts[fp.side, kind].is_empty and geom.intersection(keepouts[fp.side, kind]).area > 1e-6
                              for geom, kind in ((strip, "tracks"), (via, "vias")))
                exit_geoms.append((exit, start, end, region, outside, keepout))
            if len(_ESCAPE_GEOMS_CACHE) > 4096:
                _ESCAPE_GEOMS_CACHE.clear()
            _ESCAPE_GEOMS_CACHE[g_key] = exit_geoms
        for exit, start, end, region, outside, keepout in exit_geoms:
            blockers = sorted(shapes[fp.side][i][0] for i in trees[fp.side].query(region, predicate="intersects")
                              if shapes[fp.side][i][0] != ref and region.intersection(shapes[fp.side][i][1]).area > 1e-6)
            if blockers or outside or keepout:
                blocked.append({"ref": ref, "pin": exit.pin, "net": exit.net, "blocked_by": blockers,
                                "outside_outline": outside, "keepout": keepout, "layer": f"{fp.side}.Cu",
                                "start_mm": start, "end_mm": end, "track_envelope_mm": exit.width,
                                "via_envelope_mm": exit.via_space})
    return {"total": total, "clear": total - len(blocked), "blocked": blocked}
