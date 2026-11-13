# SPDX-License-Identifier: MIT
"""Short power-pad connections to pours and local decoupling pads."""

from __future__ import annotations

import itertools
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


class _Site:
    """What a short power track or via of one net must clear on a board."""

    def __init__(self, board: Board, net: str, fixed: list[dict]):
        rules = board.netclass_of(net)
        self.board, self.net = board, net
        self.layers = [layer.canonical for layer in board.copper_layers]
        self.width = max(rules.track_width, board.rules.get("min_track_width", 0))
        self.diameter = max(rules.via_diameter, board.rules.get("min_via_diameter", 0))
        self.drill = max(rules.via_drill, board.rules.get("min_through_drill", 0))
        self.clearance = clearance = max(rules.clearance, board.rules.get("min_clearance", 0))
        self.edge = board.rules.get("copper_edge_clearance", 0)
        self.outline = board_polygon(board)
        self.holes = existing_holes(board, fixed)
        self.other = []
        for item in fixed:
            if item["net"] != net:
                other_rules = board.netclass_of(item["net"])
                self.other.append((item, max(clearance, other_rules.clearance) if other_rules else clearance))
        self.same_vias = [v for v in fixed if v["net"] == net and "points" not in v]
        pad_obstacles = list(_pad_clearances(board, self.layers, net, clearance))
        # A via keeps the clearance to SMD pads of its own net as well: the router counts a via
        # closer than that to a same-net SMD pad as a violation (no via on or against a pad).
        solder_pads = [pad_polygon(fp, pad, clearance) for fp in board.footprints for pad in fp.pads if pad.kind == "smd"]
        keepouts = [
            (area["layers"], area["keepout"], Polygon(area["polygons"][0], area["polygons"][1:]))
            for area in board.rule_areas if area["polygons"]
        ]
        graphics = [
            (item["copper"], Polygon(ring).buffer(clearance))  # keepouts for the router, with its clearance
            for item in board.obstacles if not item["overlapping_pads"] and not item.get("net")
            for ring in item["outlines"] if len(ring) >= 3
        ]
        self._track = {
            layer: prep(unary_union(
                [g for lays, g in pad_obstacles if layer in lays]
                + [g for lays, flags, g in keepouts if layer in lays and flags["tracks"]]
                + [g for copper, g in graphics if copper == layer]))
            for layer in ("F.Cu", "B.Cu") if layer in self.layers
        }
        self._via = prep(unary_union(
            solder_pads + [g for _, g in pad_obstacles]
            + [g for _, flags, g in keepouts if flags["vias"]]
            + [g for _, g in graphics]
        ))

    def track(self, layer: str, points: list[tuple[float, float]]) -> dict | None:
        """The track, when it clears pads, keep-outs, the board edge and other nets' new copper."""
        shape = LineString(points).buffer(self.width / 2)
        if self._track[layer].intersects(shape) or not self.outline.covers(shape.buffer(self.edge)):
            return None
        track = {"net": self.net, "layer": layer, "width": self.width, "points": points}
        return track if all(copper_separated([track], [], [item], gap) for item, gap in self.other) else None

    def via(self, x: float, y: float, inside=None) -> dict | None:
        """A through via at the point, when it is legal there (and inside ``inside`` if given)."""
        shape = Point(x, y).buffer(self.diameter / 2)
        if (inside is not None and not inside.covers(shape)) or self._via.intersects(shape):
            return None
        if not self.outline.covers(shape.buffer(self.edge)):
            return None
        # two vias of one net closer than the clearance are one via too many: share instead
        if any(math.dist((x, y), (v["x"], v["y"])) < (v["diameter"] + self.diameter) / 2 + self.clearance - 1e-9
               for v in self.same_vias):
            return None
        via = {"net": self.net, "x": x, "y": y, "diameter": self.diameter, "drill": self.drill,
               "layers": ["F.Cu", "B.Cu"]}
        if not holes_separated([via], self.holes, self.board.rules.get("hole_to_hole", 0)):
            return None
        # The writer needs the two end layers, but a through via crosses every
        # copper layer when checking its clearance to existing routing.
        span = {**via, "layers": self.layers}
        return via if all(copper_separated([], [span], [item], gap) for item, gap in self.other) else None


def _outer_layer(board: Board, footprint: Footprint, pad: Pad) -> str | None:
    layers = [layer.canonical for layer in board.copper_layers]
    source = pad_layers(footprint, pad, layers)
    if not {"F.Cu", "B.Cu"} <= set(layers) or pad.kind != "smd" or len(source) != 1 or source[0] not in ("F.Cu", "B.Cu"):
        return None
    return source[0]


def _joins(origin: tuple[float, float], angle: float, end: tuple[float, float]) -> Iterator[list[tuple[float, float]]]:
    """Straight to ``end``, then with one knee on the pad's outward axis."""
    yield [origin, end]
    for steps in range(8, 31, 2):
        knee = (origin[0] + steps / 20 * math.cos(angle), origin[1] + steps / 20 * math.sin(angle))
        if math.dist(knee, end) > 1e-6:
            yield [origin, knee, end]


def _length(points: list[tuple[float, float]]) -> float:
    return sum(math.dist(p, q) for p, q in itertools.pairwise(points))


def plan_power_tap(board: Board, footprint: Footprint, pad: Pad, target_layer: str, *,
                   fixed: list[dict], max_length_mm: float = 2.0,
                   share: list[dict] | None = None, share_max_mm: float = 1.5) -> tuple[dict, dict] | None:
    """Plan a track polyline and through via. Emit one track per consecutive pair of points.

    A via in ``share`` (same net, already tapping the pour) within ``share_max_mm`` of track is
    used instead of drilling another one; the returned via is then that very dict.

    The via is planned inside the pour's outline. Whether it lands on filled copper is KiCad's call after
    the refill: ``s6a.plan`` drops a tap whose via the fill does not reach (``filled_hits``)."""
    layers = [layer.canonical for layer in board.copper_layers]
    layer = _outer_layer(board, footprint, pad)
    # the pad is on an outer layer; the pour may be on any other layer (a through via reaches inner planes too)
    if layer is None or target_layer == layer or target_layer not in layers:
        return None
    net = pad.net
    if not net or board.netclass_of(net) is None:
        return None
    # a zone lists its layers by the names the EDA shows ("Mid-Layer 1"), the target is canonical ("In1.Cu")
    canonical = {cl.name: cl.canonical for cl in board.copper_layers}
    target = unary_union([
        Polygon(zone["outline"][0], zone["outline"][1:])
        for zone in board.zones
        if zone["net"] == net and zone["outline"] and target_layer in {canonical.get(n, n) for n in zone["layers"]}
    ])
    if target.is_empty or board_polygon(board) is None:
        return None
    site = _Site(board, net, fixed)
    origin = footprint.pad_world(pad)
    dx, dy = outward(footprint, pad)
    angle = math.atan2(dy, dx)
    for via in sorted(share or [], key=lambda v: math.dist(origin, (v["x"], v["y"]))):
        end = (via["x"], via["y"])
        if via["net"] != net or math.dist(origin, end) > share_max_mm or not target.covers(Point(end)):
            continue
        for points in _joins(origin, angle, end):
            if _length(points) <= share_max_mm + 1e-9 and (track := site.track(layer, points)):
                return track, via
    for points in _candidate_paths(origin, angle, max_length_mm):
        via = site.via(*points[-1], inside=target)
        if via and (track := site.track(layer, points)):
            return track, via
    return None


def plan_cross_link(board: Board, source: tuple[Footprint, Pad], destination: tuple[Footprint, Pad], *,
                    fixed: list[dict], max_length_mm: float = 5.0, tap_max_mm: float = 2.0) -> list[dict] | None:
    """Join two same-net SMD pads on opposite outer layers through one via.

    The via is a dogbone off one pad; the other pad reaches it with a straight or one-knee track.
    Returns [track, via, track], or None."""
    net = source[1].net
    if not net or destination[1].net != net or board.netclass_of(net) is None:
        return None
    sides = [(fp, pad, _outer_layer(board, fp, pad)) for fp, pad in (source, destination)]
    if None in (sides[0][2], sides[1][2]) or sides[0][2] == sides[1][2] or board_polygon(board) is None:
        return None
    ends = [fp.pad_world(pad) for fp, pad, _ in sides]
    if math.dist(*ends) > max_length_mm:
        return None
    site = _Site(board, net, fixed)
    for near, far in ((0, 1), (1, 0)):
        fp, pad, layer = sides[near]
        far_fp, far_pad, far_layer = sides[far]
        dx, dy = outward(fp, pad)
        fx, fy = outward(far_fp, far_pad)
        # dogbones that end nearer the other pad first
        dogbones = sorted(_candidate_paths(ends[near], math.atan2(dy, dx), tap_max_mm),
                          key=lambda pts: math.dist(pts[-1], ends[far]))
        for points in dogbones:
            budget = max_length_mm - _length(points)
            if math.dist(points[-1], ends[far]) > budget:
                continue
            via = site.via(*points[-1])
            if via is None or (first := site.track(layer, points)) is None:
                continue
            for back in _joins(ends[far], math.atan2(fy, fx), points[-1]):
                if _length(back) <= budget + 1e-9 and (second := site.track(far_layer, back)):
                    return [first, via, second]
    return None
