# SPDX-License-Identifier: MIT
"""Placement space for outward pad escapes, using the board's routing rules."""

from __future__ import annotations

from dataclasses import dataclass

from shapely import STRtree
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

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
        if model.is_uncertain(role) or role["value"] not in ("ic", "converter_ic", "linear_regulator", "power_switch"):
            continue
        pads = [p for p in fp.pads if p.kind == "smd" and p.net in connected and not is_ground_net(p.net)]
        if len(pads) < MIN_CONNECTED_PADS:
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
            # Pads well inside a package need a BGA escape strategy, not an exterior reservation.
            if gaps[side] > max(pad.size):
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


def audit(board: Board, exits: dict[str, list[Escape]], *, flipped: set[str] | None = None) -> dict:
    """Count clear exterior exits against the actual outline and nearby courtyards."""
    if not exits:
        return {"total": 0, "clear": 0, "blocked": []}
    outline = board_polygon(board)
    shapes = {side: [(fp.ref, geom) for fp in board.footprints if fp.side == side
                     and (geom := courtyard_polygon(fp)) is not None] for side in ("F", "B")}
    trees = {side: STRtree([geom for _, geom in rows]) for side, rows in shapes.items()}
    names = {layer.name: layer.canonical for layer in board.copper_layers}
    keepouts = {(side, kind): unary_union([Polygon(ring).buffer(0) for row in board.rule_areas
                                 if row["keepout"].get(kind)
                                 and any(names.get(layer, layer) == f"{side}.Cu" for layer in row["layers"])
                                 for ring in row["polygons"] if len(ring) >= 3])
                for side in ("F", "B") for kind in ("tracks", "vias")}
    blocked = []
    flipped = flipped or set()
    total = sum(map(len, exits.values()))
    for ref, rows in exits.items():
        fp = board.fp(ref)

        mirror = -1 if ref in flipped else 1
        for exit in rows:
            start, end = (transform((x, y * mirror), fp.position, fp.rotation) for x, y in (exit.start, exit.end))
            x, y = end
            radius = exit.via_space / 2
            strip = LineString([start, end]).buffer(exit.width / 2, cap_style="flat")
            via = box(x - radius, y - radius, x + radius, y + radius)
            region = strip.union(via)
            blockers = sorted(shapes[fp.side][i][0] for i in trees[fp.side].query(region, predicate="intersects")
                              if shapes[fp.side][i][0] != ref and region.intersection(shapes[fp.side][i][1]).area > 1e-6)
            outside = outline is None or not outline.covers(region)
            keepout = any(geom.intersection(keepouts[fp.side, kind]).area > 1e-6
                          for geom, kind in ((strip, "tracks"), (via, "vias")))
            if blockers or outside or keepout:
                blocked.append({"ref": ref, "pin": exit.pin, "net": exit.net, "blocked_by": blockers,
                                "outside_outline": outside, "keepout": keepout})
    return {"total": total, "clear": total - len(blocked), "blocked": blocked}
