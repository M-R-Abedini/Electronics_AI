# SPDX-License-Identifier: MIT
"""Deterministic quadrant dogbone escape for BGA packages on multi-layer boards (S6a).

Detects ball-grid arrays purely from SMD pad geometry (no hardcoded part or library
names), computes symmetric 4-quadrant diagonal dogbone via sites respecting board
minimum via/drill/track/clearance rules, and validates every segment and via against
all pads, drills, keepouts, obstacles, and previously locked S6a copper using a
spatial STRtree index.
"""

from __future__ import annotations

import math
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

from ..eval.legality import board_polygon
from ..geometry import transform
from ..ir import Board, Footprint, Pad, plane_zone_layers
from .copper import (
    copper_separated,
    existing_holes,
    holes_separated,
    pad_layers,
    pad_polygon,
    via_layer_span,
)
from .fanout import _obstacle_hits_nets

BGA_MIN_PADS = 16
BGA_MAX_PADS = 400
BGA_MIN_GRID = 4
BGA_MIN_PITCH_MM = 0.35
BGA_MAX_PITCH_MM = 1.50
CLUSTER_TOL_MM = 0.08
MICROVIA_AUTO_PITCH_MM = 0.65
MICROVIA_MODES = ("auto", "on", "off")


def _is_real_net(net: str | None) -> bool:
    return bool(net) and not net.startswith("unconnected-(")


def _cluster_coords(values: list[float], tol: float = CLUSTER_TOL_MM) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def detect_bga(fp: Footprint) -> dict[str, Any] | None:
    """Detect a regular 2D SMD ball grid array from pad coordinates alone."""
    smd = [p for p in fp.pads if p.kind == "smd"]
    if len(smd) < BGA_MIN_PADS or len(smd) > BGA_MAX_PADS:
        return None
    areas = sorted(p.size[0] * p.size[1] for p in smd)
    med_area = areas[len(areas) // 2]
    # Exclude large exposed thermal pads so only ball pads define the array grid.
    balls = [p for p in smd if p.size[0] * p.size[1] <= max(med_area * 2.5, 0.01)]
    if len(balls) < BGA_MIN_PADS:
        return None
    # True BGA balls are symmetric (square/circular); reject elongated LGA/QFN perimeter modules.
    symmetric = sum(
        1 for p in balls
        if max(p.size[0], p.size[1]) > 0
        and abs(p.size[0] - p.size[1]) <= 0.12 * max(p.size[0], p.size[1])
    )
    if symmetric / len(balls) < 0.85:
        return None

    cols = _cluster_coords([p.local[0] for p in balls])
    rows = _cluster_coords([p.local[1] for p in balls])
    if len(cols) < BGA_MIN_GRID or len(rows) < BGA_MIN_GRID:
        return None

    dx_list = [cols[i + 1] - cols[i] for i in range(len(cols) - 1)]
    dy_list = [rows[i + 1] - rows[i] for i in range(len(rows) - 1)]
    px = _median(dx_list)
    py = _median(dy_list)
    if not (BGA_MIN_PITCH_MM <= px <= BGA_MAX_PITCH_MM and BGA_MIN_PITCH_MM <= py <= BGA_MAX_PITCH_MM):
        return None
    if abs(px - py) > 0.15 * max(px, py):
        return None

    # Regularity check: at least 75% of adjacent column/row steps match median pitch within 12%.
    good_x = sum(1 for d in dx_list if abs(d - px) <= 0.12 * px)
    good_y = sum(1 for d in dy_list if abs(d - py) <= 0.12 * py)
    if good_x / len(dx_list) < 0.75 or good_y / len(dy_list) < 0.75:
        return None

    # Confirm interior pads exist (distinguishes true 2D BGA from perimeter-only QFN/QFP).
    grid: dict[tuple[int, int], Pad] = {}
    for p in balls:
        ci = min(range(len(cols)), key=lambda i: abs(cols[i] - p.local[0]))
        ri = min(range(len(rows)), key=lambda i: abs(rows[i] - p.local[1]))
        if abs(cols[ci] - p.local[0]) <= CLUSTER_TOL_MM and abs(rows[ri] - p.local[1]) <= CLUSTER_TOL_MM:
            grid[(ci, ri)] = p
    ncols, nrows = len(cols), len(rows)
    interior = [k for k in grid if 0 < k[0] < ncols - 1 and 0 < k[1] < nrows - 1]
    if len(interior) < 4:
        return None

    pad_diam = _median([max(p.size[0], p.size[1]) for p in balls])
    return {
        "pitch_x": round(px, 6),
        "pitch_y": round(py, 6),
        "pad_diameter": round(pad_diam, 6),
        "cols": cols,
        "rows": rows,
        "ncols": ncols,
        "nrows": nrows,
        "grid": grid,
        "cx": 0.5 * (cols[0] + cols[-1]),
        "cy": 0.5 * (rows[0] + rows[-1]),
    }


def _quadrant_offsets(
    lx: float, ly: float, cx: float, cy: float, px: float, py: float
) -> list[tuple[float, float]]:
    """Return 4 diagonal dogbone midpoint offsets, primary outward quadrant first."""
    sx = -1.0 if lx < cx else 1.0
    sy = -1.0 if ly < cy else 1.0
    hx, hy = 0.5 * px, 0.5 * py
    return [
        (sx * hx, sy * hy),
        (sx * hx, -sy * hy),
        (-sx * hx, sy * hy),
        (-sx * hx, -sy * hy),
    ]


def _via_options(board: Board, net: str, pitch: float, pad_diam: float) -> list[tuple[float, float, float, float]]:
    """Return ordered legal (track_width, clearance, via_diameter, via_drill) tuples.

    Tries the netclass via first; if that via cannot clear neighboring balls at the
    dogbone midpoint, falls back to the board's minimum legal via diameter/drill
    (never below fab minimums).
    """
    rules = board.netclass_of(net)
    min_w = float(board.rules.get("min_track_width") or 0.0)
    min_clr = float(board.rules.get("min_clearance") or 0.0)
    min_vd = float(board.rules.get("min_via_diameter") or 0.0)
    min_dr = float(board.rules.get("min_through_drill") or 0.0)

    nc_w = max(rules.track_width, min_w)
    nc_clr = max(rules.clearance, min_clr)
    nc_vd = max(rules.via_diameter, min_vd)
    nc_dr = max(rules.via_drill, min_dr)

    diag_center_dist = 0.5 * math.hypot(pitch, pitch)
    # Between-via routing channel requirement: two adjacent dogbone vias spaced by `pitch`
    # must leave room for at least one signal track + 2*clearance to escape on inner layers.
    min_channel_nc = nc_w + 2.0 * nc_clr
    min_channel_fab = (min_w if min_w > 0 else nc_w) + 2.0 * nc_clr

    nc_fits_diag = diag_center_dist - 0.5 * pad_diam - 0.5 * nc_vd >= nc_clr + 0.005
    nc_leaves_channel = (pitch - nc_vd) >= min_channel_nc - 1e-4

    has_min_via = min_vd > 0 and min_dr > 0 and (abs(min_vd - nc_vd) > 1e-4 or abs(min_dr - nc_dr) > 1e-4)
    w_min = max(min_w, min(nc_w, max(min_w, 0.10))) if min_w > 0 else nc_w
    min_fits_diag = has_min_via and (diag_center_dist - 0.5 * pad_diam - 0.5 * min_vd >= nc_clr + 0.005)
    min_leaves_channel = has_min_via and ((pitch - min_vd) >= min_channel_fab - 1e-4)

    opts: list[tuple[float, float, float, float]] = []
    # Prefer the netclass via ONLY when it also leaves an open between-via escape channel;
    # otherwise prefer the legal board-minimum via first so inner routing channels stay open!
    if nc_fits_diag and nc_leaves_channel:
        opts.append((nc_w, nc_clr, nc_vd, nc_dr))
        if min_fits_diag:
            opts.append((w_min, nc_clr, min_vd, min_dr))
    elif min_fits_diag and min_leaves_channel:
        opts.append((w_min, nc_clr, min_vd, min_dr))
        if nc_fits_diag:
            opts.append((nc_w, nc_clr, nc_vd, nc_dr))
    else:
        if min_fits_diag:
            opts.append((w_min, nc_clr, min_vd, min_dr))
        if nc_fits_diag:
            opts.append((nc_w, nc_clr, nc_vd, nc_dr))
    return opts


def _microvia_spec(board: Board, net: str, pitch: float, pad_diam: float) -> tuple[float, float, float, float] | None:
    """Return legal (track_width, clearance, uvia_diameter, uvia_drill) for KiCad 10 microvia-in-pad.

    Respects board-wide microvia minimums, netclass microvia overrides, and board annular ring
    so generated L1-L2 / L2-L3 microvias pass KiCad 10 DRC with zero errors.
    """
    rules = board.netclass_of(net)
    min_w = float(board.rules.get("min_track_width") or 0.0)
    min_clr = float(board.rules.get("min_clearance") or 0.0)
    min_ud = float(board.rules.get("min_microvia_diameter") or 0.0)
    min_udr = float(board.rules.get("min_microvia_drill") or 0.0)
    min_ann = float(board.rules.get("min_via_annular_width") or 0.0)

    nc_w = max(rules.track_width, min_w) if min_w > 0 else rules.track_width
    w = max(min_w, min(nc_w, max(min_w, 0.12))) if min_w > 0 else nc_w
    clr = max(rules.clearance, min_clr)
    nc_ud = float(rules.microvia_diameter or 0.0)
    nc_udr = float(rules.microvia_drill or 0.0)

    drill = max(nc_udr, min_udr, 0.10)
    diam = max(nc_ud, min_ud, drill + 2.0 * min_ann, drill + 0.10)
    # Microvia-in-pad must clear neighboring balls on the pad layer (with small rounding margin).
    if pitch - 0.5 * (max(pad_diam, diam) + pad_diam) < clr - 0.005:
        return None
    return (round(w, 4), round(clr, 4), round(diam, 4), round(drill, 4))


def _adjacent_escape_layers(pad_layer: str, ordered_layers: list[str]) -> tuple[str | None, str | None]:
    """Return (adj1, adj2) inner escape layers stepping inward from ``pad_layer`` (F.Cu or B.Cu)."""
    if len(ordered_layers) < 4 or pad_layer not in (ordered_layers[0], ordered_layers[-1]):
        return (None, None)
    if pad_layer == ordered_layers[0]:
        adj1 = ordered_layers[1]
        adj2 = ordered_layers[2] if len(ordered_layers) >= 6 else None
        return (adj1, adj2)
    adj1 = ordered_layers[-2]
    adj2 = ordered_layers[-3] if len(ordered_layers) >= 6 else None
    return (adj1, adj2)


def _item_bbox(item: dict) -> tuple[float, float, float, float]:
    if "points" in item:
        pts = item["points"]
        hw = float(item.get("width") or 0.0) * 0.5
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)
    r = float(item.get("diameter") or 0.0) * 0.5
    x, y = float(item["x"]), float(item["y"])
    return (x - r, y - r, x + r, y + r)


def _bboxes_close(a: tuple[float, float, float, float], b: tuple[float, float, float, float], gap: float) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


class _BgaContext:
    """Board-wide spatial index built once for fast, DRC-exact BGA escape validation."""

    def __init__(self, board: Board, fixed: list[dict]) -> None:
        self.board = board
        self.ordered_layers = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
        self.top_layer = self.ordered_layers[0]
        self.bottom_layer = self.ordered_layers[-1]
        self.outline = board_polygon(board)
        self.edge = float(board.rules.get("copper_edge_clearance") or 0.0)
        self.hole_to_hole = float(board.rules.get("hole_to_hole") or 0.0)
        self.min_clr = float(board.rules.get("min_clearance") or 0.0)

        self.pad_geoms: list[Polygon] = []
        self.pad_meta: list[tuple[frozenset[str], str | None, str, float]] = []
        max_pad_clr = self.min_clr
        for fp in board.footprints:
            for pad in fp.pads:
                geom = pad_polygon(fp, pad, 0.0)
                lyrs = frozenset(pad_layers(fp, pad, self.ordered_layers))
                p_rules = board.netclass_of(pad.net) if pad.net else None
                p_clr = max(self.min_clr, p_rules.clearance) if p_rules else self.min_clr
                max_pad_clr = max(max_pad_clr, p_clr)
                self.pad_geoms.append(geom)
                self.pad_meta.append((lyrs, pad.net, pad.kind, p_clr))
        self.max_pad_clr = max_pad_clr
        self.pad_tree = STRtree(self.pad_geoms) if self.pad_geoms else None

        keepouts = [
            (area["layers"], area["keepout"], Polygon(area["polygons"][0], area["polygons"][1:]))
            for area in board.rule_areas
            if area.get("polygons")
        ]
        via_ko_polys = [g for _, flags, g in keepouts if flags.get("vias")]
        self.via_keepout = prep(unary_union(via_ko_polys)) if via_ko_polys else None
        self.track_keepout = {}
        for lyr in self.ordered_layers:
            polys = [g for lays, flags, g in keepouts if lyr in lays and flags.get("tracks")]
            self.track_keepout[lyr] = prep(unary_union(polys)) if polys else None

        # Precompute static un-netted board obstacles (mask_aperture, pad_keepout, copper_graphic).
        self.obstacles_raw: list[tuple[dict, str, Polygon, bool, float]] = []
        for item in board.obstacles:
            if item.get("net"):
                continue
            copper_lyr = item.get("copper", "")
            is_mask = item.get("kind") == "mask_aperture"
            grown = float(item.get("grown_mm") or 0.0)
            for ring in item.get("outlines", ()):
                if len(ring) >= 3:
                    self.obstacles_raw.append((item, copper_lyr, Polygon(ring), is_mask, grown))
        self.obs_tree = STRtree([o[2] for o in self.obstacles_raw]) if self.obstacles_raw else None

        self.holes: list[tuple[float, float, float, frozenset[str] | None]] = list(existing_holes(board, fixed))
        self.copper: list[tuple[dict, tuple[float, float, float, float], str, float]] = []
        self.vias_by_net: dict[str, list[dict]] = {}
        for item in fixed:
            self.register_copper(item, add_hole=False)

    def register_copper(self, item: dict, *, add_hole: bool = True) -> None:
        net = item.get("net") or ""
        rules = self.board.netclass_of(net) if net else None
        clr = max(self.min_clr, rules.clearance) if rules else self.min_clr
        if "points" not in item:
            span = sorted(via_layer_span(item, self.ordered_layers))
            stored = {**item, "layers": span}
            self.copper.append((stored, _item_bbox(item), net, clr))
            self.vias_by_net.setdefault(net, []).append(stored)
            if add_hole:
                self.holes.append((float(item["x"]), float(item["y"]), float(item["drill"]), frozenset(span)))
        else:
            self.copper.append((item, _item_bbox(item), net, clr))

    def _clears_obstacles(
        self,
        geom: Any,
        layers: str | set[str] | frozenset[str] | None,
        net: str,
        clr: float,
    ) -> bool:
        if self.obs_tree is None:
            return True
        layer_set = {layers} if isinstance(layers, str) else layers
        query_box = geom.buffer(clr + 0.05)
        for idx in self.obs_tree.query(query_box):
            item, obs_lyr, poly, is_mask, grown = self.obstacles_raw[int(idx)]
            if layer_set is not None and obs_lyr not in layer_set:
                continue
            if _obstacle_hits_nets(self.board, item, {net}):
                continue
            req = 0.0 if is_mask else max(0.0, clr - grown)
            if req <= 0.0:
                if geom.intersects(poly):
                    return False
            elif geom.distance(poly) < req - 1e-6:
                return False
        return True

    def _validate_microvia(
        self,
        net: str,
        via_xy: tuple[float, float],
        span_layers: tuple[str, str],
        clr: float,
        diameter: float,
        drill: float,
        *,
        allow_own_pad_at: tuple[float, float] | None = None,
        extra_copper: list[dict] | None = None,
        extra_holes: list[tuple[float, float, float, frozenset[str] | None]] | None = None,
    ) -> dict | None:
        if self.outline is None:
            return None
        vx, vy = round(via_xy[0], 4), round(via_xy[1], 4)
        via_pt = Point(vx, vy)
        via_disc = via_pt.buffer(diameter * 0.5)
        if not self.outline.covers(via_disc.buffer(self.edge)):
            return None
        if self.via_keepout is not None and self.via_keepout.intersects(via_disc):
            return None
        span_set = frozenset(span_layers)
        if self.pad_tree is not None:
            pad_margin = 0.005
            search_pad_clr = max(clr, self.max_pad_clr) + 0.02
            for idx in self.pad_tree.query(via_disc.buffer(search_pad_clr)):
                p_geom = self.pad_geoms[int(idx)]
                p_lyrs, p_net, p_kind, p_clr = self.pad_meta[int(idx)]
                if not (p_lyrs & span_set):
                    continue
                if allow_own_pad_at is not None and p_net == net and p_kind == "smd":
                    c = p_geom.centroid
                    if math.hypot(float(c.x) - allow_own_pad_at[0], float(c.y) - allow_own_pad_at[1]) <= 0.05:
                        continue
                if p_kind != "smd" and p_kind != "np_thru_hole" and p_net == net:
                    continue
                req = max(clr, p_clr) + pad_margin
                if via_disc.distance(p_geom) < req:
                    return None
        if not self._clears_obstacles(via_disc, span_set, net, clr):
            return None
        for other_via in self.vias_by_net.get(net, ()):
            if not (set(other_via.get("layers") or ()) & span_set):
                continue
            min_gap = (diameter + float(other_via["diameter"])) * 0.5 + clr
            if math.dist((vx, vy), (float(other_via["x"]), float(other_via["y"]))) < min_gap - 1e-6:
                return None
        via = {
            "x": vx,
            "y": vy,
            "diameter": diameter,
            "drill": drill,
            "net": net,
            "via_type": "micro",
            "layers": [span_layers[0], span_layers[1]],
            "filled": True,
            "capped": allow_own_pad_at is not None,
        }
        all_holes = self.holes + (extra_holes or [])
        if not holes_separated([via], all_holes, self.hole_to_hole, ordered_layers=self.ordered_layers):
            return None
        span_via = {**via, "layers": list(span_set)}
        v_box = _item_bbox(via)
        for item, i_box, i_net, i_clr in self.copper:
            if i_net == net:
                continue
            gap = max(clr, i_clr)
            if not _bboxes_close(v_box, i_box, gap):
                continue
            if not copper_separated([], [span_via], [item], gap):
                return None
        if extra_copper:
            for item in extra_copper:
                if item.get("net") == net:
                    continue
                if not copper_separated([], [span_via], [item], clr):
                    return None
        return via

    def validate_staggered_microvia(
        self,
        net: str,
        pad_layer: str,
        adj1_layer: str,
        adj2_layer: str,
        pad_xy: tuple[float, float],
        stagger_xy: tuple[float, float],
        width: float,
        clr: float,
        diameter: float,
        drill: float,
    ) -> list[dict] | None:
        """Validate VIPPO L1-L2 at pad center + short adj1 track + staggered L2-L3 microvia."""
        v1 = self._validate_microvia(
            net, pad_xy, (pad_layer, adj1_layer), clr, diameter, drill,
            allow_own_pad_at=pad_xy,
        )
        if v1 is None:
            return None
        vx1, vy1 = v1["x"], v1["y"]
        vx2, vy2 = round(stagger_xy[0], 4), round(stagger_xy[1], 4)
        # Strictly staggered (non-stacked): ensure laser drills on L1-L2 and L2-L3 stay cleanly separated.
        min_stagger = max(diameter + clr, drill + self.hole_to_hole)
        if math.hypot(vx2 - vx1, vy2 - vy1) < min_stagger - 1e-4:
            return None
        seg_pts = [(vx1, vy1), (vx2, vy2)]
        line = LineString(seg_pts)
        track_copper = line.buffer(width * 0.5)
        if self.outline is None or not self.outline.covers(track_copper.buffer(self.edge)):
            return None
        tko = self.track_keepout.get(adj1_layer)
        if tko is not None and tko.intersects(track_copper):
            return None
        if self.pad_tree is not None:
            search_pad_clr = max(clr, self.max_pad_clr) + 0.02
            for idx in self.pad_tree.query(track_copper.buffer(search_pad_clr)):
                p_geom = self.pad_geoms[int(idx)]
                p_lyrs, p_net, p_kind, p_clr = self.pad_meta[int(idx)]
                if adj1_layer not in p_lyrs:
                    continue
                if p_net == net and p_kind != "np_thru_hole":
                    continue
                if track_copper.distance(p_geom) < max(clr, p_clr) + 0.005:
                    return None
        if not self._clears_obstacles(track_copper, adj1_layer, net, clr):
            return None
        track = {"layer": adj1_layer, "width": width, "net": net, "points": seg_pts}
        t_box = _item_bbox(track)
        for item, i_box, i_net, i_clr in self.copper:
            if i_net == net:
                continue
            gap = max(clr, i_clr)
            if not _bboxes_close(t_box, i_box, gap):
                continue
            if not copper_separated([track], [], [item], gap):
                return None
        v1_hole = (vx1, vy1, drill, frozenset((pad_layer, adj1_layer)))
        v2 = self._validate_microvia(
            net, (vx2, vy2), (adj1_layer, adj2_layer), clr, diameter, drill,
            extra_holes=[v1_hole],
        )
        if v2 is None:
            return None
        return [v1, track, v2]

    def validate_dogbone(
        self,
        net: str,
        pad_layer: str,
        pad_xy: tuple[float, float],
        via_xy: tuple[float, float],
        width: float,
        clr: float,
        diameter: float,
        drill: float,
    ) -> tuple[dict, dict] | None:
        if self.outline is None:
            return None
        vx, vy = round(via_xy[0], 4), round(via_xy[1], 4)
        via_pt = Point(vx, vy)
        via_disc = via_pt.buffer(diameter * 0.5)
        if not self.outline.covers(via_disc.buffer(self.edge)):
            return None
        if self.via_keepout is not None and self.via_keepout.intersects(via_disc):
            return None

        seg_pts = [(pad_xy[0], pad_xy[1]), (vx, vy)]
        line = LineString(seg_pts)
        if line.length < 1e-4:
            return None
        track_copper = line.buffer(width * 0.5)
        if not self.outline.covers(track_copper.buffer(self.edge)):
            return None
        tko = self.track_keepout.get(pad_layer)
        if tko is not None and tko.intersects(track_copper):
            return None

        # Check pads via STRtree (only neighboring balls are inspected).
        if self.pad_tree is not None:
            pad_margin = 0.005  # small safety margin for Specctra DSN integer-grid rounding
            search_pad_clr = max(clr, self.max_pad_clr) + 0.02
            for idx in self.pad_tree.query(via_disc.buffer(search_pad_clr)):
                p_geom = self.pad_geoms[int(idx)]
                _lyrs, p_net, p_kind, p_clr = self.pad_meta[int(idx)]
                # Via must clear ALL SMD pads (including same-net SMD pads) and all non-same-net/NPTH pads.
                if p_kind != "smd" and p_kind != "np_thru_hole" and p_net == net:
                    continue
                req = max(clr, p_clr) + pad_margin
                if via_disc.distance(p_geom) < req:
                    return None
            for idx in self.pad_tree.query(track_copper.buffer(search_pad_clr)):
                p_geom = self.pad_geoms[int(idx)]
                p_lyrs, p_net, p_kind, p_clr = self.pad_meta[int(idx)]
                if pad_layer not in p_lyrs:
                    continue
                if p_net == net and p_kind != "np_thru_hole":
                    continue
                req = max(clr, p_clr) + pad_margin
                if track_copper.distance(p_geom) < req:
                    return None

        if not self._clears_obstacles(via_disc, None, net, clr):
            return None
        if not self._clears_obstacles(track_copper, pad_layer, net, clr):
            return None

        # Same-net via separation (avoid overlapping or crowding another escape via on the same net).
        for other_via in self.vias_by_net.get(net, ()):
            min_gap = (diameter + float(other_via["diameter"])) * 0.5 + clr
            if math.dist((vx, vy), (float(other_via["x"]), float(other_via["y"]))) < min_gap - 1e-6:
                return None

        via = {
            "x": vx,
            "y": vy,
            "diameter": diameter,
            "drill": drill,
            "net": net,
            "layers": [self.top_layer, self.bottom_layer],
        }
        if not holes_separated([via], self.holes, self.hole_to_hole, ordered_layers=self.ordered_layers):
            return None

        track = {"layer": pad_layer, "width": width, "net": net, "points": seg_pts}
        span_via = {**via, "layers": self.ordered_layers}
        v_box = _item_bbox(via)
        t_box = _item_bbox(track)
        for item, i_box, i_net, i_clr in self.copper:
            if i_net == net:
                continue
            gap = max(clr, i_clr)
            if not (_bboxes_close(v_box, i_box, gap) or _bboxes_close(t_box, i_box, gap)):
                continue
            if not copper_separated([track], [span_via], [item], gap):
                return None
        return track, via


def plan_bga_escapes(
    board: Board,
    intent: dict,
    *,
    fixed: list[dict],
    already_handled: set[tuple[str, str]] | None = None,
    include_signals: bool = True,
    microvia: str = "auto",
) -> dict[str, Any]:
    """Generate DRC-safe BGA escapes (quadrant dogbone + HDI microvia-in-pad) on >= 4-layer boards.

    ``microvia`` policy:
      - ``"off"``: classic quadrant dogbone through-vias only.
      - ``"auto"`` (default): activates IPC-4761 Type VII Microvia-in-Pad (L1-L2 VIPPO and
        staggered L2-L3 on >= 6-layer boards) ONLY when ``board.rules['allow_microvias']`` is
        True and either pitch <= 0.65 mm or dogbone cannot fit a legal channel-preserving via.
      - ``"on"``: prefers Microvia-in-Pad whenever ``board.rules['allow_microvias']`` is True
        on >= 4-layer boards, falling back to dogbone if needed.
    """
    if microvia not in MICROVIA_MODES:
        raise ValueError(f"unsupported microvia mode {microvia!r}; expected one of {MICROVIA_MODES}")
    handled = set(already_handled or ())
    if len(board.copper_layers) < 4:
        return {"copper": [], "rows": [], "packages": []}

    by_canon = {cl.name: cl.canonical for cl in board.copper_layers}
    planes = plane_zone_layers(board)
    plane_polys: dict[str, Any] = {}
    plane_layers_by_net: dict[str, set[str]] = {}
    for z in board.zones:
        if z.get("net") and z.get("outline") and z.get("uuid") in planes:
            poly = Polygon(z["outline"][0], z["outline"][1:])
            if not poly.is_empty:
                prev = plane_polys.get(z["net"])
                plane_polys[z["net"]] = poly if prev is None else unary_union([prev, poly])
            canon_lyrs = {by_canon.get(n, n) for n in planes[z["uuid"]]}
            plane_layers_by_net.setdefault(z["net"], set()).update(canon_lyrs)
    plane_nets = set(plane_polys)
    board_allows_uvia = bool(board.rules.get("allow_microvias", False))

    bgas: list[tuple[Footprint, dict[str, Any]]] = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        bga = detect_bga(fp)
        if bga is not None:
            bgas.append((fp, bga))
    if not bgas:
        return {"copper": [], "rows": [], "packages": []}

    ctx = _BgaContext(board, fixed)
    new_copper: list[dict] = []
    rows: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []

    for fp, bga in bgas:
        px, py = bga["pitch_x"], bga["pitch_y"]
        pitch = min(px, py)
        pad_diam = bga["pad_diameter"]
        ncols, nrows = bga["ncols"], bga["nrows"]
        cx, cy = bga["cx"], bga["cy"]

        # On fine-pitch HDI BGAs (pitch <= 0.65 mm with allow_microvias=True), inner-ring
        # (ring >= 1) signal balls physically cannot escape on the surface layer and require
        # microvia-in-pad escape automatically.
        auto_hdi_signals = (
            microvia != "off"
            and board_allows_uvia
            and (microvia == "on" or pitch <= MICROVIA_AUTO_PITCH_MM)
        )
        escape_sig_here = bool(include_signals or auto_hdi_signals)

        # Sort balls from outermost inner ring inward for deterministic, symmetric fanout.
        candidates: list[tuple[int, int, int, Pad]] = []
        for (ci, ri), pad in bga["grid"].items():
            if not _is_real_net(pad.net):
                continue
            if (fp.ref, str(pad.number)) in handled:
                continue
            is_plane = pad.net in plane_nets
            if not escape_sig_here and not is_plane:
                continue
            ring = min(ci, ncols - 1 - ci, ri, nrows - 1 - ri)
            is_outer = ring == 0
            if is_outer and not is_plane:
                # Outer-perimeter signal balls escape on the surface layer without needing a via.
                continue
            candidates.append((ring, ri, ci, pad))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))

        pkg_planned = len(candidates)
        pkg_routed = 0
        pkg_microvia = 0
        for ring, _ri, _ci, pad in candidates:
            net = pad.net
            pad_lyrs = pad_layers(fp, pad, ctx.ordered_layers)
            if len(pad_lyrs) != 1:
                continue
            pad_layer = pad_lyrs[0]
            pad_xy = fp.pad_world(pad)
            offsets = _quadrant_offsets(pad.local[0], pad.local[1], cx, cy, px, py)
            opts = _via_options(board, net, pitch, pad_diam)
            row: dict[str, Any] = {"ref": fp.ref, "pad": str(pad.number), "net": net, "routed": False}

            adj1, adj2 = _adjacent_escape_layers(pad_layer, ctx.ordered_layers)
            want_uvia = (
                microvia != "off"
                and board_allows_uvia
                and adj1 is not None
                and (microvia == "on" or pitch <= MICROVIA_AUTO_PITCH_MM or not opts)
            )
            target_plane = plane_polys.get(net) if (not escape_sig_here) else None
            net_plane_lyrs = plane_layers_by_net.get(net, set())

            placed_uvia: list[dict] | None = None
            uvia_kind: str | None = None
            if want_uvia:
                uspec = _microvia_spec(board, net, pitch, pad_diam)
                if uspec is not None:
                    uw, uclr, ud, udr = uspec
                    need_l2_l3 = (
                        adj2 is not None
                        and (
                            (net in plane_nets and adj1 not in net_plane_lyrs and adj2 in net_plane_lyrs)
                            or (escape_sig_here and net not in plane_nets and ring >= 2)
                        )
                    )
                    can_l1_l2 = (
                        (net not in plane_nets)
                        or (adj1 in net_plane_lyrs)
                    )
                    if need_l2_l3 and adj2 is not None:
                        for dx, dy in offsets:
                            stag_xy = transform((pad.local[0] + dx, pad.local[1] + dy), (fp.x, fp.y), fp.rotation)
                            if target_plane is not None and not target_plane.covers(Point(round(stag_xy[0], 4), round(stag_xy[1], 4))):
                                continue
                            got_u = ctx.validate_staggered_microvia(
                                net, pad_layer, adj1, adj2, pad_xy, stag_xy, uw, uclr, ud, udr,
                            )
                            if got_u is not None:
                                placed_uvia = got_u
                                uvia_kind = "microvia_l1_l2_l3_staggered"
                                break
                    elif can_l1_l2 and (target_plane is None or target_plane.covers(Point(round(pad_xy[0], 4), round(pad_xy[1], 4)))):
                        v1 = ctx._validate_microvia(
                            net, pad_xy, (pad_layer, adj1), uclr, ud, udr,
                            allow_own_pad_at=pad_xy,
                        )
                        if v1 is not None:
                            placed_uvia = [v1]
                            uvia_kind = "microvia_in_pad_l1_l2"

            if placed_uvia is not None:
                for item in placed_uvia:
                    ctx.register_copper(item, add_hole=("points" not in item))
                new_copper.extend(placed_uvia)
                handled.add((fp.ref, str(pad.number)))
                pkg_routed += 1
                pkg_microvia += 1
                vias_u = [c for c in placed_uvia if "points" not in c]
                tracks_u = [c for c in placed_uvia if "points" in c]
                row.update(
                    routed=True,
                    kind=uvia_kind,
                    via_type="micro",
                    via_count=len(vias_u),
                    via_diameter_mm=vias_u[0]["diameter"],
                    via_drill_mm=vias_u[0]["drill"],
                    layers=[v["layers"] for v in vias_u],
                    length_mm=round(sum(math.dist(t["points"][0], t["points"][1]) for t in tracks_u), 3),
                )
                rows.append(row)
                continue

            if not opts:
                row["failed"] = "pitch_requires_microvia_or_vip"
                rows.append(row)
                continue

            placed_pair: tuple[dict, dict] | None = None
            for width, clr, vd, vdr in opts:
                for dx, dy in offsets:
                    via_xy = transform((pad.local[0] + dx, pad.local[1] + dy), (fp.x, fp.y), fp.rotation)
                    if target_plane is not None and not target_plane.covers(Point(round(via_xy[0], 4), round(via_xy[1], 4))):
                        continue
                    got = ctx.validate_dogbone(net, pad_layer, pad_xy, via_xy, width, clr, vd, vdr)
                    if got is not None:
                        placed_pair = got
                        break
                if placed_pair is not None:
                    break

            if placed_pair is not None:
                track, via = placed_pair
                ctx.register_copper(track, add_hole=False)
                ctx.register_copper(via, add_hole=True)
                new_copper.extend([track, via])
                handled.add((fp.ref, str(pad.number)))
                pkg_routed += 1
                row.update(
                    routed=True,
                    kind="dogbone_through",
                    via_type="through",
                    via_diameter_mm=via["diameter"],
                    via_drill_mm=via["drill"],
                    length_mm=round(math.dist(track["points"][0], track["points"][1]), 3),
                )
            else:
                row["failed"] = "via_site_blocked"
            rows.append(row)

        packages.append({
            "ref": fp.ref,
            "balls": len(bga["grid"]),
            "pitch_mm": round(pitch, 3),
            "planned": pkg_planned,
            "routed": pkg_routed,
            "microvia_routed": pkg_microvia,
        })

    return {"copper": new_copper, "rows": rows, "packages": packages}
