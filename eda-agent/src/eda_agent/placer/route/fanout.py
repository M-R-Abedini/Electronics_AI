# SPDX-License-Identifier: MIT
"""Short power-pad connections to pours and local decoupling pads."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator

import numpy as np
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
    via_layer_span,
)
from .pairs import (
    Exits,
    PairRule,
    Window,
    _block_drills,
    obstacles,
    rule_for,
    trace_path,
)


def _obstacle_hits_nets(board: Board, item: dict, skip_nets: set[str]) -> bool:
    if not item.get("overlapping_pads"):
        return False
    if item.get("kind") != "copper_graphic":
        return True
    pad_nets = {f"{fp.ref}-{p.number}": p.net for fp in board.footprints for p in fp.pads}
    return any(pad_nets.get(name) in skip_nets for name in item["overlapping_pads"])


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
    link_res = min(0.05, max(0.025, width / 3.0)) if width <= 0.12 else 0.05
    win = Window(
        board, (x0 - max_length_mm, y0 - max_length_mm, x1 + max_length_mm, y1 + max_length_mm),
        [layer], res=link_res,
    )
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
        and not _obstacle_hits_nets(board, item, {net}) and not item.get("net")
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


_OCT_STEPS: dict[int, tuple[int, int]] = {
    0: (1, 0),
    45: (1, 1),
    90: (0, 1),
    135: (-1, 1),
    180: (-1, 0),
    225: (-1, -1),
    270: (0, -1),
    315: (1, -1),
}


def _advance_oct(point: tuple[float, float], deg: int, length_mm: float) -> tuple[float, float]:
    """Advance strictly along a 0/45/90-degree octilinear ray with exact |dx| == |dy| on diagonals."""
    sx, sy = _OCT_STEPS[deg % 360]
    if sx != 0 and sy != 0:
        d = round(length_mm / math.sqrt(2.0), 4)
        return (round(point[0] + sx * d, 4), round(point[1] + sy * d, 4))
    d = round(length_mm, 4)
    return (round(point[0] + sx * d, 4), round(point[1] + sy * d, 4))


def _octilinear_joins(origin: tuple[float, float], end: tuple[float, float]) -> Iterator[list[tuple[float, float]]]:
    """Yield 1- or 2-segment strict 0/45/90-degree octilinear polylines connecting ``origin`` to ``end``."""
    dx = end[0] - origin[0]
    dy = end[1] - origin[1]
    adx, ady = abs(dx), abs(dy)
    if adx < 1e-4 or ady < 1e-4 or abs(adx - ady) < 1e-4:
        yield [origin, end]
        return
    diag = min(adx, ady)
    sx = 1.0 if dx > 0 else -1.0
    sy = 1.0 if dy > 0 else -1.0
    # 1. Diagonal first from origin, then cardinal into end.
    k1 = (round(origin[0] + sx * diag, 4), round(origin[1] + sy * diag, 4))
    yield [origin, k1, end]
    # 2. Cardinal first from origin, then diagonal into end.
    if adx > ady:
        k2 = (round(origin[0] + sx * (adx - diag), 4), origin[1])
    else:
        k2 = (origin[0], round(origin[1] + sy * (ady - diag), 4))
    if k2 != k1:
        yield [origin, k2, end]


def _candidate_paths(origin: tuple[float, float], angle: float,
                     max_length_mm: float, *, full_sweep: bool = False) -> Iterator[list[tuple[float, float]]]:
    """Yield strict 0/45/90-degree octilinear tap paths (1- or 2-segment) up to ``max_length_mm``.

    Strict octilinear geometry prevents Freerouting's PolylineTrace.normalize from oscillating on
    locked non-45-degree S6a power stubs while reaching every quadrant via 45-degree mitred doglegs.
    """
    base_deg = (round(math.degrees(angle) / 45.0) * 45) % 360
    max_steps = int(max_length_mm * 20 + 1e-9)
    if full_sweep:
        # Fallback when primary +/-45 deg outward sweep is blocked (e.g. pad facing board edge):
        # try shorter fine-pitch steps (0.25..0.35 mm) and wide/side/back octilinear rays + doglegs.
        for steps in range(5, min(8, max_steps + 1)):
            for turn in (0, 45, -45):
                yield [origin, _advance_oct(origin, base_deg + turn, steps / 20.0)]
        for steps in range(5, max_steps + 1):
            for turn in (90, -90, 135, -135, 180):
                yield [origin, _advance_oct(origin, base_deg + turn, steps / 20.0)]
        for steps in range(10, max_steps + 1):
            for first_steps in range(4, min(20, steps - 3), 2):
                for departure in (90, -90, 135, -135, 180):
                    d0 = (base_deg + departure) % 360
                    knee = _advance_oct(origin, d0, first_steps / 20.0)
                    for bend in (45, -45):
                        yield [origin, knee, _advance_oct(knee, d0 + bend, (steps - first_steps) / 20.0)]
        return
    for steps in range(8, max_steps + 1):
        for turn in (0, 45, -45):
            yield [origin, _advance_oct(origin, base_deg + turn, steps / 20.0)]
    for steps in range(10, max_steps + 1):
        for first_steps in range(4, min(24, steps - 3), 2):
            for departure in (0, 45, -45):
                d0 = (base_deg + departure) % 360
                knee = _advance_oct(origin, d0, first_steps / 20.0)
                for bend in (45, -45, 90, -90):
                    yield [origin, knee, _advance_oct(knee, d0 + bend, (steps - first_steps) / 20.0)]


class _Site:
    """What a short power track or via of one net must clear on a board."""

    def __init__(self, board: Board, net: str, fixed: list[dict], *, static_cache: dict | None = None):
        rules = board.netclass_of(net)
        self.board, self.net = board, net
        self.layers = [layer.canonical for layer in board.copper_layers]
        self.width = max(rules.track_width, board.rules.get("min_track_width", 0))
        self.diameter = max(rules.via_diameter, board.rules.get("min_via_diameter", 0))
        self.drill = max(rules.via_drill, board.rules.get("min_through_drill", 0))
        self.clearance = clearance = max(rules.clearance, board.rules.get("min_clearance", 0))
        self.edge = board.rules.get("copper_edge_clearance", 0)
        self.holes = existing_holes(board, fixed)
        self.other = []
        self._other_shapes = []
        for item in fixed:
            if item["net"] != net:
                other_rules = board.netclass_of(item["net"])
                gap = max(clearance, other_rules.clearance) if other_rules else clearance
                self.other.append((item, gap))
                if "points" in item:
                    g = LineString(item["points"])
                    r = item["width"] / 2.0
                    lyrs = {item["layer"]}
                else:
                    g = Point(item["x"], item["y"])
                    r = item["diameter"] / 2.0
                    lyrs = set(item["layers"])
                x0, y0, x1, y1 = g.bounds
                pad_b = r + gap + 1e-4
                self._other_shapes.append((g, r, lyrs, (x0 - pad_b, y0 - pad_b, x1 + pad_b, y1 + pad_b), gap))
        self.same_vias = [v for v in fixed if v["net"] == net and "points" not in v]

        hole_clr = float(board.rules.get("hole_clearance") or 0.0)
        via_annular = max(0.0, 0.5 * (self.diameter - self.drill))
        via_clr = max(clearance, (hole_clr - via_annular + 0.005) if hole_clr > 0.0 else 0.0)

        cache_key = (id(board), net, clearance, round(via_clr, 5))
        cached = static_cache.get(cache_key) if static_cache is not None else None
        if cached is not None:
            self.outline, self._track, self._via, self._via_layer_obs, self._via_span_cache = cached
            return

        self.outline = board_polygon(board)
        pad_obstacles = list(_pad_clearances(board, self.layers, net, clearance))
        via_pad_obstacles = (
            pad_obstacles if abs(via_clr - clearance) <= 1e-6
            else list(_pad_clearances(board, self.layers, net, via_clr))
        )
        # A via keeps the clearance to SMD pads of its own net as well: the router counts a via
        # closer than that to a same-net SMD pad as a violation (no via on or against a pad).
        solder_pads_by_layer: list[tuple[frozenset[str], Polygon, tuple[float, float], str | None]] = [
            (
                frozenset(pad_layers(fp, pad, self.layers)),
                pad_polygon(fp, pad, clearance if pad.net == net else via_clr),
                fp.pad_world(pad),
                pad.net,
            )
            for fp in board.footprints for pad in fp.pads if pad.kind == "smd"
        ]
        solder_pads = [g for _, g, _, _ in solder_pads_by_layer]
        keepouts = [
            (area["layers"], area["keepout"], Polygon(area["polygons"][0], area["polygons"][1:]))
            for area in board.rule_areas if area["polygons"]
        ]
        graphics = [
            (
                item["copper"],
                Polygon(ring).buffer(
                    0.0 if item.get("kind") == "mask_aperture"
                    else max(0.0, clearance - float(item.get("grown_mm") or 0.0))
                ),
            )
            for item in board.obstacles if not _obstacle_hits_nets(board, item, {net}) and not item.get("net")
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
            solder_pads + [g for _, g in via_pad_obstacles]
            + [g for _, flags, g in keepouts if flags["vias"]]
            + [g for _, g in graphics]
        ))
        self._via_layer_obs = (solder_pads_by_layer, via_pad_obstacles, keepouts, graphics)
        self._via_span_cache: dict[frozenset[str], object] = {}
        if static_cache is not None:
            static_cache[cache_key] = (
                self.outline, self._track, self._via, self._via_layer_obs, self._via_span_cache,
            )

    def _via_for_span(self, span: frozenset[str], *, skip_own_pad_at: tuple[float, float] | None = None):
        if skip_own_pad_at is None:
            cached = self._via_span_cache.get(span)
            if cached is not None:
                return cached
        solder_pads_by_layer, via_pad_obstacles, keepouts, graphics = self._via_layer_obs
        geoms = (
            [
                g for lays, g, pxy, pnet in solder_pads_by_layer
                if (lays & span) and not (
                    skip_own_pad_at is not None and pnet == self.net
                    and math.hypot(pxy[0] - skip_own_pad_at[0], pxy[1] - skip_own_pad_at[1]) <= 0.05
                )
            ]
            + [g for lays, g in via_pad_obstacles if set(lays) & span]
            + [g for lays, flags, g in keepouts if flags["vias"] and set(lays) & span]
            + [g for copper, g in graphics if copper in span]
        )
        prepared = prep(unary_union(geoms)) if geoms else prep(Polygon())
        if skip_own_pad_at is None:
            self._via_span_cache[span] = prepared
        return prepared

    def microvia_tap(
        self, x: float, y: float, source_layer: str, target_layer: str, inside=None, *,
        allow_own_pad_at: tuple[float, float] | None = None,
    ) -> list[dict] | None:
        """Layer-scoped blind/microvia stack from *source_layer* to inner plane *target_layer*.

        Only active when the board allows microvias (HDI). Ignores opposite-side SMD pads
        outside the microvia's layer span (e.g. backside shields/ICs blocking through-vias).
        """
        if not self.board.rules.get("allow_microvias", False) or len(self.layers) < 4:
            return None
        ordered = [cl.canonical for cl in sorted(self.board.copper_layers, key=lambda c: c.index)]
        if source_layer == ordered[0]:
            adj1, adj2 = ordered[1], ordered[2]
        elif source_layer == ordered[-1]:
            adj1, adj2 = ordered[-2], ordered[-3]
        else:
            return None
        if target_layer == adj1:
            spans = [(source_layer, adj1)]
            span_set = frozenset({source_layer, adj1})
        elif target_layer == adj2:
            spans = [(source_layer, adj1), (adj1, adj2)]
            span_set = frozenset({source_layer, adj1, adj2})
        else:
            return None

        nc = self.board.netclass_of(self.net)
        ud = max(
            float(getattr(nc, "microvia_diameter", 0.0) or 0.0),
            float(self.board.rules.get("min_microvia_diameter") or 0.0),
            0.20,
        )
        udr = max(
            float(getattr(nc, "microvia_drill", 0.0) or 0.0),
            float(self.board.rules.get("min_microvia_drill") or 0.0),
            0.10,
        )
        min_ann = float(self.board.rules.get("min_via_annular_width") or 0.0)
        if 0.5 * (ud - udr) + 1e-6 < min_ann:
            ud = round(udr + 2.0 * min_ann, 4)

        pt = Point(x, y)
        vr = ud / 2.0
        shape = pt.buffer(vr)
        if (inside is not None and not inside.covers(shape)) or self._via_for_span(span_set, skip_own_pad_at=allow_own_pad_at).intersects(shape):
            return None
        if not self.outline.covers(shape.buffer(self.edge)):
            return None
        for v in self.same_vias:
            if not (via_layer_span(v, ordered) & span_set):
                continue
            if math.dist((x, y), (v["x"], v["y"])) < (v["diameter"] + ud) / 2.0 + self.clearance - 1e-9:
                return None
        vias = [
            {
                "net": self.net,
                "x": x,
                "y": y,
                "diameter": ud,
                "drill": udr,
                "layers": [la, lb],
                "via_type": "micro",
                "filled": True,
                "capped": la in ("F.Cu", "B.Cu"),
            }
            for la, lb in spans
        ]
        # Check hole separation against existing holes (skip self-stack pair check by testing each individually).
        hth = float(self.board.rules.get("hole_to_hole") or 0.0)
        for v in vias:
            if not holes_separated([v], self.holes, hth, ordered_layers=ordered):
                return None
        for g, r, lyrs, (ox0, oy0, ox1, oy1), gap in self._other_shapes:
            if not (span_set & lyrs):
                continue
            if x + vr < ox0 or ox1 < x - vr or y + vr < oy0 or oy1 < y - vr:
                continue
            if pt.distance(g) < vr + r + gap - 1e-6:
                return None
        return vias

    def track(self, layer: str, points: list[tuple[float, float]]) -> dict | None:
        """The track, when it clears pads, keep-outs, the board edge and other nets' new copper."""
        line = LineString(points)
        hr = self.width / 2.0
        shape = line.buffer(hr)
        if self._track[layer].intersects(shape) or not self.outline.covers(shape.buffer(self.edge)):
            return None
        bx0, by0, bx1, by1 = line.bounds
        for g, r, lyrs, (ox0, oy0, ox1, oy1), gap in self._other_shapes:
            if layer not in lyrs:
                continue
            if bx1 + hr < ox0 or ox1 < bx0 - hr or by1 + hr < oy0 or oy1 < by0 - hr:
                continue
            if line.distance(g) < hr + r + gap - 1e-6:
                return None
        return {"net": self.net, "layer": layer, "width": self.width, "points": points}

    def via(self, x: float, y: float, inside=None) -> dict | None:
        """A through via at the point, when it is legal there (and inside ``inside`` if given)."""
        pt = Point(x, y)
        vr = self.diameter / 2.0
        shape = pt.buffer(vr)
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
        all_layers = set(self.layers)
        for g, r, lyrs, (ox0, oy0, ox1, oy1), gap in self._other_shapes:
            if not (all_layers & lyrs):
                continue
            if x + vr < ox0 or ox1 < x - vr or y + vr < oy0 or oy1 < y - vr:
                continue
            if pt.distance(g) < vr + r + gap - 1e-6:
                return None
        return via


def _outer_layer(board: Board, footprint: Footprint, pad: Pad) -> str | None:
    layers = [layer.canonical for layer in board.copper_layers]
    source = pad_layers(footprint, pad, layers)
    if not {"F.Cu", "B.Cu"} <= set(layers) or pad.kind != "smd" or len(source) != 1 or source[0] not in ("F.Cu", "B.Cu"):
        return None
    return source[0]


def _joins(origin: tuple[float, float], angle: float, end: tuple[float, float]) -> Iterator[list[tuple[float, float]]]:
    """Strict 0/45/90-degree octilinear joins to ``end``, plus one-knee outward octilinear exits."""
    yield from _octilinear_joins(origin, end)
    base_deg = (round(math.degrees(angle) / 45.0) * 45) % 360
    for steps in range(6, 29, 4):
        knee = _advance_oct(origin, base_deg, steps / 20.0)
        if math.dist(knee, end) > 1e-4:
            for tail in _octilinear_joins(knee, end):
                yield [origin, *tail]


def _length(points: list[tuple[float, float]]) -> float:
    return sum(math.dist(p, q) for p, q in itertools.pairwise(points))


def plan_power_tap(board: Board, footprint: Footprint, pad: Pad, target_layer: str, *,
                   fixed: list[dict], max_length_mm: float = 2.0,
                   share: list[dict] | None = None, share_max_mm: float = 1.5,
                   site_cache: dict | None = None,
                   allow_microvia: bool = False) -> tuple[dict, dict | list[dict]] | None:
    """Plan a track polyline and via (or HDI microvia stack). Emit one track per consecutive pair of points.

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
    site = _Site(board, net, fixed, static_cache=site_cache)
    origin = footprint.pad_world(pad)
    dx, dy = outward(footprint, pad)
    angle = math.atan2(dy, dx)
    for via in sorted(share or [], key=lambda v: math.dist(origin, (v["x"], v["y"]))):
        end = (via["x"], via["y"])
        if via["net"] != net or math.dist(origin, end) > share_max_mm or not target.covers(Point(end)):
            continue
        if via.get("via_type") == "micro" and layer not in set(via.get("layers", ())):
            continue
        for points in _joins(origin, angle, end):
            if _length(points) <= share_max_mm + 1e-9 and (track := site.track(layer, points)):
                return track, via
    for full_sweep in (False, True):
        for points in _candidate_paths(origin, angle, max_length_mm, full_sweep=full_sweep):
            via = site.via(*points[-1], inside=target)
            if via and (track := site.track(layer, points)):
                return track, via
    if allow_microvia and board.rules.get("allow_microvias", False):
        for full_sweep in (False, True):
            for points in _candidate_paths(origin, angle, max_length_mm, full_sweep=full_sweep):
                uvias = site.microvia_tap(*points[-1], layer, target_layer, inside=target)
                if uvias and (track := site.track(layer, points)):
                    return track, (uvias[0] if len(uvias) == 1 else uvias)
        # HDI VIPPO fallback: when off-pad dogleg sites are blocked on a dense microvia-enabled board
        # and the microvia fits inside the SMD pad itself, drop directly in-pad (IPC-4761 Type VII).
        uvias_inpad = site.microvia_tap(*origin, layer, target_layer, inside=target, allow_own_pad_at=origin)
        if uvias_inpad and min(pad.size) + 1e-6 >= uvias_inpad[0]["diameter"]:
            return None, (uvias_inpad[0] if len(uvias_inpad) == 1 else uvias_inpad)
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


def traps_pads(board: Board, fixed: list[dict], tracks: list[dict], vias: list[dict],
               own_nets: set[str], *, margin_mm: float = 4.5, res: float | None = None) -> list[str]:
    """Return pad names cut off from the largest free routing region by ``tracks`` + ``vias`` (arch 14 row 2a)."""
    if not tracks and not vias:
        return []
    outer = [cl.canonical for cl in board.copper_layers if cl.canonical in ("F.Cu", "B.Cu")]
    if not outer:
        return []
    ref_net = next(iter(own_nets), "")
    rule = rule_for(board, ref_net)
    # Use standard trace width (not wide diff-pair width) when checking general pad escape clearance.
    nc = board.netclass_of(ref_net) or next(c for c in board.netclasses if c.name == "Default")
    gen_width = max(board.rules.get("min_track_width", nc.track_width), 1e-3)
    gen_clear = max(board.rules.get("min_clearance", nc.clearance), 1e-3)
    gen_via_d = max(board.rules.get("min_via_diameter", nc.via_diameter), 1e-3)
    gen_drill = max(board.rules.get("min_through_drill", nc.via_drill), 1e-3)
    eff_res = float(res) if res is not None else min(0.075, max(0.03, gen_width / 3.0))
    pts = [p for t in tracks for p in t["points"]] + [(v["x"], v["y"]) for v in vias]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    win = Window(board, (min(xs) - margin_mm, min(ys) - margin_mm, max(xs) + margin_mm, max(ys) + margin_mm),
                 outer, res=eff_res)
    escape_rule = PairRule(width=gen_width, gap=gen_clear, clearance=gen_clear,
                           via_d=min(rule.via_d, gen_via_d), via_drill=min(rule.via_drill, gen_drill))
    blocked_drills = np.zeros((win.ny, win.nx), bool)
    _block_drills(win, blocked_drills, existing_holes(board, fixed), escape_rule.via_drill,
                  board.rules.get("hole_to_hole", 0.0))
    exits = Exits(board, win, escape_rule, fixed, own_nets, blocked_drills)
    return exits.closed_in(tracks, vias)


def reserve_power_escapes(board: Board, intent: dict, *, active_roles: tuple[str, ...],
                          pours: dict[str, set[str]], tap_max_mm: float = 2.0) -> list[dict]:
    """Reserve escape space for active-IC power/ground pads without locking copper (arch 14 row 2b).

    Returned dicts are tagged ``kind='reserve'`` and passed only as obstacles during pair routing;
    they are never written to the PCB as copper.
    """
    from ..intent import model

    by_canon = {cl.name: cl.canonical for cl in board.copper_layers}
    power_nets = {
        net for net, info in (intent.get("nets") or {}).items()
        if ((info.get("rail") or {}).get("value")) in ("power", "ground")
    }
    if not power_nets:
        return []
    reserved: list[dict] = []
    sites: dict[str, _Site] = {}
    for ref in sorted(intent.get("parts", {}), key=model.natural_key):
        if intent["parts"][ref]["role"]["value"] not in active_roles:
            continue
        fp = board.fp(ref)
        for pad in fp.pads:
            if pad.kind != "smd" or pad.net not in power_nets:
                continue
            layer = _outer_layer(board, fp, pad)
            if layer is None or board.netclass_of(pad.net) is None:
                continue
            origin = fp.pad_world(pad)
            owner = f"{ref}-{pad.number}"
            if pad.net in pours:
                targets = sorted({by_canon.get(n, n) for n in pours[pad.net]} - {layer})
                got = None
                for target_layer in targets:
                    got = plan_power_tap(board, fp, pad, target_layer, fixed=reserved, max_length_mm=tap_max_mm)
                    if got is not None:
                        break
                if got is not None:
                    trk, via = got
                    reserved.append({**trk, "kind": "reserve", "owner": owner, "origin": origin})
                    reserved.append({**via, "kind": "reserve", "owner": owner, "origin": origin})
                    continue
            site = sites.get(pad.net)
            if site is None:
                site = sites[pad.net] = _Site(board, pad.net, [])
            dx, dy = outward(fp, pad)
            stub_len = max(pad.size) / 2 + site.clearance + site.width / 2 + 0.35
            end = (origin[0] + dx * stub_len, origin[1] + dy * stub_len)
            trk = site.track(layer, [origin, end])
            if trk is not None:
                reserved.append({**trk, "kind": "reserve", "owner": owner, "origin": origin})
    return reserved


def plan_plane_stitching(
    board: Board,
    pours: dict[str, set[str]],
    *,
    fixed: list[dict],
    min_stitch_vias: int = 8,
    min_spacing_mm: float = 3.0,
) -> list[dict]:
    """Place DRC-checked through-vias stitching same-net inner planes together (e.g. dual GND planes).

    On 6L/8L+ HDI boards with multiple inner planes on the same net (such as In1.Cu and In6.Cu both on GND),
    blind surface microvias (F.Cu->In1.Cu and B.Cu->In6.Cu) leave those two planes electrically disconnected
    unless stitched with through-vias. Adding well-distributed through-vias guarantees a unified low-inductance
    return path and prevents the autorouter from oscillating while trying to bridge split planes.
    """
    ordered = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    if len(ordered) < 4:
        return []
    by_canon = {cl.name: cl.canonical for cl in board.copper_layers}
    outer = {ordered[0], ordered[-1]}
    stitched: list[dict] = []
    cur_fixed = list(fixed)

    for net in sorted(pours):
        plane_lyrs = sorted({by_canon.get(n, n) for n in pours[net]} - outer)
        if len(plane_lyrs) < 2 or board.netclass_of(net) is None:
            continue
        plane_set = set(plane_lyrs)
        existing_stitch = [
            v for v in cur_fixed
            if v.get("net") == net and "points" not in v and plane_set <= via_layer_span(v, ordered)
        ]
        if len(existing_stitch) >= min_stitch_vias:
            continue
        # Intersect the plane outlines across all plane layers of this net so every stitch hits all planes.
        common = None
        for lyr in plane_lyrs:
            polys = [
                Polygon(z["outline"][0], z["outline"][1:])
                for z in board.zones
                if z.get("net") == net and z.get("outline")
                and lyr in {by_canon.get(n, n) for n in z.get("layers", ())}
            ]
            if not polys:
                common = None
                break
            u = unary_union(polys)
            common = u if common is None else common.intersection(u)
        if common is None or common.is_empty:
            continue

        x0, y0, x1, y1 = common.bounds
        step = max(1.0, min(2.5, min(x1 - x0, y1 - y0) / 10.0))
        candidates: list[tuple[float, float]] = []
        y = y0 + step * 0.5
        while y < y1:
            x = x0 + step * 0.5
            while x < x1:
                if common.contains(Point(x, y)):
                    candidates.append((round(x, 3), round(y, 3)))
                x += step
            y += step
        if not candidates:
            continue

        # Prefer spread-out candidates using farthest-point greedy ordering for uniform return-path coverage.
        placed_xy = [(float(v["x"]), float(v["y"])) for v in existing_stitch]
        site = _Site(board, net, cur_fixed)
        needed = min_stitch_vias - len(existing_stitch)
        # Seed ordering from board center outward, then farthest from already-placed stitch vias.
        cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        remaining = list(candidates)
        while needed > 0 and remaining:
            if placed_xy:
                remaining.sort(
                    key=lambda pt: min(math.dist(pt, q) for q in placed_xy),
                    reverse=True,
                )
            else:
                remaining.sort(key=lambda pt: math.dist(pt, (cx, cy)))
            found = False
            for idx, (px, py) in enumerate(remaining):
                if placed_xy and min(math.dist((px, py), q) for q in placed_xy) < min_spacing_mm:
                    remaining = []
                    break
                via = site.via(px, py, inside=common)
                if via is None:
                    continue
                if traps_pads(board, cur_fixed, [], [via], {net}):
                    continue
                stitched.append(via)
                cur_fixed.append(via)
                placed_xy.append((px, py))
                site = _Site(board, net, cur_fixed)
                remaining.pop(idx)
                needed -= 1
                found = True
                break
            if not found:
                break
    return stitched
