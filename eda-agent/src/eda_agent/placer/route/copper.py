# SPDX-License-Identifier: MIT
"""Copper geometry shared by the S6a planners: pad shapes, escape direction, clearance and drill checks."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon

from ..geometry import transform
from ..ir import Board, Footprint, Pad


def pad_polygon(fp: Footprint, pad: Pad, grow: float = 0.0) -> Polygon:
    cx, cy = fp.pad_world(pad)
    w, h = pad.size
    ang = fp.rotation + pad.rotation
    if pad.shape in ("circle", "oval") and abs(w - h) < 1e-6:
        return Point(cx, cy).buffer(w / 2 + grow, quad_segs=8)
    corners = [transform(c, (cx, cy), ang) for c in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
    return Polygon(corners).buffer(grow, join_style="mitre") if grow else Polygon(corners)


def pad_layers(fp: Footprint, pad: Pad, layers: list[str]) -> list[str]:
    if pad.kind in ("thru_hole", "np_thru_hole") or "*.Cu" in pad.layers:
        return list(layers)
    named = [lay for lay in pad.layers if lay.endswith(".Cu")]
    if named:
        return [lay for lay in named if lay in layers]
    return [("F.Cu" if fp.side == "F" else "B.Cu")]


def outward(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Unit vector along the pad's long axis, pointing away from the part's centre (the way a pin escapes).

    The axis goes through the same ``transform`` as the pad itself, so it follows any rotation and side.
    """
    px, py = fp.pad_world(pad)
    dx, dy = px - fp.x, py - fp.y
    w, h = pad.size
    if abs(w - h) < 1e-6:
        ax, ay = dx, dy
    else:
        ox, oy = transform((0.0, 0.0), (0.0, 0.0), fp.rotation + pad.rotation)
        ux, uy = transform((1.0, 0.0) if w > h else (0.0, 1.0), (0.0, 0.0), fp.rotation + pad.rotation)
        ax, ay = ux - ox, uy - oy
        if ax * dx + ay * dy < 0:
            ax, ay = -ax, -ay
    n = math.hypot(ax, ay) or 1.0
    return ax / n, ay / n


def copper_separated(tracks: list[dict], vias: list[dict], other: list[dict], clearance: float) -> bool:
    def shape(item):
        if "points" in item:
            return LineString(item["points"]), item["width"] / 2, {item["layer"]}
        return Point(item["x"], item["y"]), item["diameter"] / 2, set(item["layers"])

    obstacles = [shape(item) for item in other]
    for item in tracks + vias:
        geom, radius, layers = shape(item)
        for obstacle, other_radius, other_layers in obstacles:
            if layers & other_layers and geom.distance(obstacle) < radius + other_radius + clearance - 1e-6:
                return False
    return True


def existing_holes(board: Board, copper: list[dict]) -> list[tuple[float, float, float]]:
    # Use the larger drill dimension as a conservative bound for slotted pads.
    pads = [(x, y, max(pad.drill)) for fp in board.footprints for pad, (x, y) in fp.pads_world() if pad.drill]
    return pads + [(v["x"], v["y"], v["drill"]) for v in copper if "drill" in v]


def holes_separated(vias: list[dict], holes: list[tuple[float, float, float]], clearance: float) -> bool:
    existing = list(holes)
    for via in vias:
        x, y, drill = via["x"], via["y"], via["drill"]
        if any(math.hypot(x - px, y - py) < (drill + pd) / 2 + clearance - 1e-6
               for px, py, pd in existing):
            return False
        existing.append((x, y, drill))
    return True
