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
    if (pad.shape or "").lower() in ("circle", "oval") and abs(w - h) < 1e-6:
        return Point(cx, cy).buffer(w / 2 + grow, quad_segs=16)
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


def via_layer_span(via: dict, ordered_layers: list[str]) -> set[str]:
    """Return the exact set of copper layers crossed by ``via`` (through, micro, blind, or buried)."""
    raw = list(via.get("layers") or ())
    if not ordered_layers:
        return set(raw)
    if len(raw) >= 2 and raw[0] in ordered_layers and raw[-1] in ordered_layers:
        a = ordered_layers.index(raw[0])
        b = ordered_layers.index(raw[-1])
        return set(ordered_layers[min(a, b): max(a, b) + 1])
    if raw:
        return set(raw)
    return set(ordered_layers)


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


def existing_holes(board: Board, copper: list[dict]) -> list[tuple[float, float, float, frozenset[str] | None]]:
    # Use the larger drill dimension as a conservative bound for slotted pads.
    # None layer span means through-all-layers (PTH/NPTH pads and through vias).
    ordered = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    full_span = frozenset(ordered) if ordered else None
    pads: list[tuple[float, float, float, frozenset[str] | None]] = [
        (x, y, max(pad.drill), full_span)
        for fp in board.footprints
        for pad, (x, y) in fp.pads_world()
        if pad.drill
    ]
    vias: list[tuple[float, float, float, frozenset[str] | None]] = []
    for v in copper:
        if "drill" not in v:
            continue
        span = frozenset(via_layer_span(v, ordered)) if ordered and v.get("layers") else full_span
        vias.append((v["x"], v["y"], v["drill"], span))
    return pads + vias


def _hole_layers(entry: tuple) -> frozenset[str] | None:
    return entry[3] if len(entry) >= 4 else None


def holes_separated(
    vias: list[dict],
    holes: list[tuple],
    clearance: float,
    ordered_layers: list[str] | None = None,
) -> bool:
    existing = list(holes)
    for via in vias:
        x, y, drill = via["x"], via["y"], via["drill"]
        v_span = (
            frozenset(via_layer_span(via, ordered_layers))
            if ordered_layers and via.get("layers")
            else (frozenset(via["layers"]) if via.get("via_type", "through") != "through" and via.get("layers") else None)
        )
        for entry in existing:
            px, py, pd = entry[0], entry[1], entry[2]
            e_span = _hole_layers(entry)
            if v_span is not None and e_span is not None and not (v_span & e_span):
                continue
            if math.hypot(x - px, y - py) < (drill + pd) / 2 + clearance - 1e-6:
                return False
        existing.append((x, y, drill, v_span))
    return True
