# SPDX-License-Identifier: MIT
"""Short power-pad dogbones to an opposite-side pour on a two-layer board."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..eval.legality import board_polygon
from ..ir import Board, Footprint, Pad
from .pairs import _copper_separated, _existing_holes, _holes_separated, _outward, pad_layers, pad_polygon


def plan_power_tap(board: Board, footprint: Footprint, pad: Pad, target_layer: str, *,
                   fixed: list[dict], max_length_mm: float = 2.0) -> tuple[dict, dict] | None:
    """Plan a trace and through via for an SMD pad. KiCad refill checks pour connectivity."""
    layers = [layer.canonical for layer in board.copper_layers]
    outer = ("F.Cu", "B.Cu")
    source = pad_layers(footprint, pad, layers)
    if set(layers) != set(outer) or pad.kind != "smd" or len(source) != 1 or {source[0], target_layer} != set(outer):
        return None
    net = pad.net
    rules = board.netclass_of(net) if net else None
    if rules is None:
        return None
    target = unary_union([
        Polygon(zone["outline"][0], zone["outline"][1:])
        for zone in board.zones if zone["net"] == net and target_layer in zone["layers"] and zone["outline"]
    ])
    outline = board_polygon(board)
    if target.is_empty or outline is None:
        return None

    width = max(rules.track_width, board.rules.get("min_track_width", 0))
    diameter = max(rules.via_diameter, board.rules.get("min_via_diameter", 0))
    drill = max(rules.via_drill, board.rules.get("min_through_drill", 0))
    clearance = max(rules.clearance, board.rules.get("min_clearance", 0))
    edge = board.rules.get("copper_edge_clearance", 0)
    holes = _existing_holes(board, fixed)
    other = []
    for item in fixed:
        if item["net"] != net:
            other_rules = board.netclass_of(item["net"])
            other.append((item, max(clearance, other_rules.clearance) if other_rules else clearance))
    pad_obstacles = []
    solder_pads = []
    for fp in board.footprints:
        for candidate in fp.pads:
            if candidate.kind == "smd":
                solder_pads.append(pad_polygon(fp, candidate))
            if candidate.net == net and candidate.kind != "np_thru_hole":
                continue
            other_rules = board.netclass_of(candidate.net) if candidate.net else None
            gap = max(clearance, other_rules.clearance) if other_rules else clearance
            pad_obstacles.append((pad_layers(fp, candidate, layers), pad_polygon(fp, candidate, gap)))
    keepouts = [
        (area["layers"], area["keepout"], Polygon(area["polygons"][0], area["polygons"][1:]))
        for area in board.rule_areas if area["polygons"]
    ]
    graphics = [
        (item["copper"], Polygon(ring))
        for item in board.obstacles if not item["overlapping_pads"] and not item.get("net")
        for ring in item["outlines"] if len(ring) >= 3
    ]

    x, y = footprint.pad_world(pad)
    dx, dy = _outward(footprint, pad)
    angle = math.atan2(dy, dx)
    for step in range(8, int(max_length_mm * 20 + 1e-9) + 1):
        for turn in (0, 15, -15, 30, -30, 45, -45, 60, -60):
            direction = angle + math.radians(turn)
            vx, vy = x + step / 20 * math.cos(direction), y + step / 20 * math.sin(direction)
            via_shape = Point(vx, vy).buffer(diameter / 2)
            line = LineString([(x, y), (vx, vy)])
            track_shape = line.buffer(width / 2)
            if not target.covers(via_shape) or not outline.covers(via_shape.buffer(edge) | track_shape.buffer(edge)):
                continue
            if any(via_shape.intersects(g) for g in solder_pads):
                continue
            if any((source[0] in lays and track_shape.intersects(g)) or via_shape.intersects(g)
                   for lays, g in pad_obstacles):
                continue
            if any((source[0] in lays and flags["tracks"] and track_shape.intersects(g))
                   or (flags["vias"] and via_shape.intersects(g)) for lays, flags, g in keepouts):
                continue
            if any((layer == source[0] and track_shape.intersects(g)) or via_shape.intersects(g)
                   for layer, g in graphics):
                continue
            track = {"net": net, "layer": source[0], "width": width, "points": [(x, y), (vx, vy)]}
            via = {"net": net, "x": vx, "y": vy, "diameter": diameter, "drill": drill, "layers": list(outer)}
            if not _holes_separated([via], holes, board.rules.get("hole_to_hole", 0)):
                continue
            if all(_copper_separated([track], [via], [item], gap) for item, gap in other):
                return track, via
    return None
