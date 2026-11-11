# SPDX-License-Identifier: MIT
"""Short power-pad connections to pours and local decoupling pads."""

from __future__ import annotations

import math
from collections.abc import Iterator

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from ..eval.legality import board_polygon
from ..ir import Board, Footprint, Pad
from .copper import (
    copper_separated,
    existing_holes,
    holes_separated,
    outward,
    pad_layers,
    pad_polygon,
)
from .pairs import PairRule, Window, obstacles, trace_path


def _pad_clearances(board: Board, layers: list[str], net: str, clearance: float):
    for footprint in board.footprints:
        for pad in footprint.pads:
            if pad.net == net and pad.kind != "np_thru_hole":
                continue
            rules = board.netclass_of(pad.net) if pad.net else None
            gap = max(clearance, rules.clearance) if rules else clearance
            yield pad_layers(footprint, pad, layers), pad_polygon(footprint, pad, gap)


def plan_power_link(board: Board, source: tuple[Footprint, Pad], destination: tuple[Footprint, Pad], *,
                    fixed: list[dict], max_length_mm: float = 5.0) -> list[dict] | None:
    """Connect two specified same-net pads on their common side, without adding vias."""
    src_fp, src_pad = source
    dst_fp, dst_pad = destination
    net = src_pad.net
    rules = board.netclass_of(net) if net and dst_pad.net == net else None
    if rules is None:
        return None
    layers = [layer.canonical for layer in board.copper_layers]
    src_layers, dst_layers = pad_layers(src_fp, src_pad, layers), pad_layers(dst_fp, dst_pad, layers)
    if len(src_layers) != 1 or src_layers != dst_layers:
        return None
    layer = src_layers[0]
    start, end = src_fp.pad_world(src_pad), dst_fp.pad_world(dst_pad)
    outline = board_polygon(board)
    if (not math.isfinite(max_length_mm) or math.dist(start, end) > max_length_mm
            or outline is None or not outline.covers(Point(start)) or not outline.covers(Point(end))):
        return None
    if start == end:
        return []

    import placer_core

    width = max(rules.track_width, board.rules.get("min_track_width", 0))
    clearance = max(rules.clearance, board.rules.get("min_clearance", 0))
    rule = PairRule(width, clearance, clearance, rules.via_diameter, rules.via_drill)
    x0, x1 = sorted((start[0], end[0]))
    y0, y1 = sorted((start[1], end[1]))
    win = Window(board, (x0 - max_length_mm, y0 - max_length_mm, x1 + max_length_mm, y1 + max_length_mm), [layer])
    raw = obstacles(board, win, rule, fixed, skip_pad_nets={net}, skip_copper_nets={net})
    keepouts = [
        Polygon(area["polygons"][0], area["polygons"][1:])
        for area in board.rule_areas if layer in area["layers"] and area["keepout"]["tracks"] and area["polygons"]
    ]
    for polygon in keepouts:
        raw[layer] |= win.raster(polygon)
    forbidden = keepouts + [g for sides, g in _pad_clearances(board, layers, net, clearance) if layer in sides]
    forbidden.extend(
        Polygon(ring) for item in board.obstacles if item["copper"] == layer
        and not item["overlapping_pads"] and not item.get("net")
        for ring in item["outlines"] if len(ring) >= 3
    )
    edge = board.rules.get("copper_edge_clearance", 0)
    other_copper = []
    for item in fixed:
        if item["net"] != net:
            other = board.netclass_of(item["net"])
            other_copper.append((item, max(clearance, other.clearance) if other else clearance))

    # Grid tie-breaking and the exact pad leads can make the two directions differ.
    for a, b in ((start, end), (end, start)):
        result = trace_path(win, raw, [layer], [], (a, layer), (b, layer), net, rule, placer_core, False)
        if result is None:
            continue
        tracks, _ = result
        lines = [LineString(track["points"]) for track in tracks]
        if sum(line.length for line in lines) > max_length_mm + 1e-6:
            continue
        copper = unary_union([line.buffer(width / 2) for line in lines])
        if not outline.covers(copper.buffer(edge)) or any(copper.intersects(g) for g in forbidden):
            continue
        if all(copper_separated(tracks, [], [item], gap) for item, gap in other_copper):
            if a == end:
                return [{**track, "points": track["points"][::-1]} for track in reversed(tracks)]
            return tracks
    return None


def _candidate_paths(origin: tuple[float, float], angle: float,
                     max_length_mm: float) -> Iterator[list[tuple[float, float]]]:
    def advance(point: tuple[float, float], direction: float, steps: int) -> tuple[float, float]:
        return point[0] + steps / 20 * math.cos(direction), point[1] + steps / 20 * math.sin(direction)

    max_steps = int(max_length_mm * 20 + 1e-9)
    for steps in range(8, max_steps + 1):
        for turn in (0, 15, -15, 30, -30, 45, -45, 60, -60):
            yield [origin, advance(origin, angle + math.radians(turn), steps)]
    for steps in range(14, max_steps + 1):
        for first_steps in range(8, min(25, steps - 5), 2):
            for departure in (0, 15, -15, 30, -30):
                direction = angle + math.radians(departure)
                knee = advance(origin, direction, first_steps)
                for bend in (45, -45, 90, -90):
                    yield [origin, knee, advance(knee, direction + math.radians(bend), steps - first_steps)]


def plan_power_tap(board: Board, footprint: Footprint, pad: Pad, target_layer: str, *,
                   fixed: list[dict], max_length_mm: float = 2.0) -> tuple[dict, dict] | None:
    """Plan a track polyline and through via. Emit one track per consecutive pair of points.

    The via is planned inside the pour's outline. Whether it lands on filled copper is KiCad's call after
    the refill: ``s6a.plan`` drops a tap whose via the fill does not reach (``filled_hits``)."""
    layers = [layer.canonical for layer in board.copper_layers]
    outer = ("F.Cu", "B.Cu")
    source = pad_layers(footprint, pad, layers)
    # the pad is on an outer layer; the pour may be on any other layer (a through via reaches inner planes too)
    if (not set(outer) <= set(layers) or pad.kind != "smd" or len(source) != 1 or source[0] not in outer
            or target_layer == source[0] or target_layer not in layers):
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
    holes = existing_holes(board, fixed)
    other = []
    for item in fixed:
        if item["net"] != net:
            other_rules = board.netclass_of(item["net"])
            other.append((item, max(clearance, other_rules.clearance) if other_rules else clearance))
    pad_obstacles = list(_pad_clearances(board, layers, net, clearance))
    solder_pads = [pad_polygon(fp, pad) for fp in board.footprints for pad in fp.pads if pad.kind == "smd"]
    keepouts = [
        (area["layers"], area["keepout"], Polygon(area["polygons"][0], area["polygons"][1:]))
        for area in board.rule_areas if area["polygons"]
    ]
    graphics = [
        (item["copper"], Polygon(ring))
        for item in board.obstacles if not item["overlapping_pads"] and not item.get("net")
        for ring in item["outlines"] if len(ring) >= 3
    ]

    layer = source[0]
    track_obstacles = prep(unary_union(
        [g for lays, g in pad_obstacles if layer in lays]
        + [g for lays, flags, g in keepouts if layer in lays and flags["tracks"]]
        + [g for copper, g in graphics if copper == layer]
    ))
    via_obstacles = prep(unary_union(
        solder_pads + [g for _, g in pad_obstacles]
        + [g for _, flags, g in keepouts if flags["vias"]]
        + [g for _, g in graphics]
    ))
    origin = footprint.pad_world(pad)
    dx, dy = outward(footprint, pad)
    angle = math.atan2(dy, dx)
    for points in _candidate_paths(origin, angle, max_length_mm):
        vx, vy = points[-1]
        via_shape = Point(vx, vy).buffer(diameter / 2)
        if not target.covers(via_shape) or via_obstacles.intersects(via_shape):
            continue
        track_shape = LineString(points).buffer(width / 2)
        if track_obstacles.intersects(track_shape) or not outline.covers(via_shape.buffer(edge) | track_shape.buffer(edge)):
            continue
        track = {"net": net, "layer": layer, "width": width, "points": points}
        via = {"net": net, "x": vx, "y": vy, "diameter": diameter, "drill": drill, "layers": list(outer)}
        if not holes_separated([via], holes, board.rules.get("hole_to_hole", 0)):
            continue
        if all(copper_separated([track], [via], [item], gap) for item, gap in other):
            return track, via
    return None
