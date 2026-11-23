# SPDX-License-Identifier: MIT
"""Coarse-grid PathFinder routability estimator (plan F5).

Wraps ``placer_core.pathfinder_estimate`` with board-derived edge capacities:
effective signal-layer count (discounting mixed planes and excluding pure planes),
track pitch, board outline masking, and pad blockage.
Provides both a congestion summary and per-part cell-inflation margins for hotspots.
"""

from __future__ import annotations

import math

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from ..ir import ROLE_MIN_NET_FRACTION, ROLE_POUR_FRACTION, Board
from . import legality, metrics

CAPACITY_EFFICIENCY = 0.55  # declared: vias, pads, and turns consume ~45% of raw tracks/cell
MAX_GRID_DIM = 72
SPLIT_PLANE_WEIGHT = 0.25  # declared: inner split-plane layers carry limited signal capacity


def _layer_zone_areas(board: Board, cl) -> dict[str, float]:
    """Per-net copper pour area on *cl*, falling back to zone outline polygons on unfilled boards."""
    fills = board.zone_fill_by_layer_mm2.get(cl.name) or board.zone_fill_by_layer_mm2.get(cl.canonical)
    if fills:
        return fills
    by_net: dict[str, list[Polygon]] = {}
    targets = {cl.name, cl.canonical}
    for zone in board.zones:
        net = zone.get("net") or ""
        if not net or not zone.get("outline"):
            continue
        if not (set(zone.get("layers") or ()) & targets):
            continue
        poly = Polygon(zone["outline"][0], zone["outline"][1:])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            by_net.setdefault(net, []).append(poly)
    return {net: float(unary_union(polys).area) for net, polys in by_net.items()}


def _effective_signal_layers(board: Board) -> float:
    """Routing-invariant signal layer count derived from stackup + zone pours (not existing tracks)."""
    n_layers = len(board.copper_layers)
    if n_layers <= 2:
        return float(max(1, n_layers))
    area = board.board_area_mm2 or 0.0
    last = n_layers - 1
    total = 0.0
    for cl in board.copper_layers:
        if cl.index == 0 or cl.index == last:
            total += 1.0
            continue
        fills = _layer_zone_areas(board, cl)
        counted = {n: a for n, a in fills.items() if n and area and a / area >= ROLE_MIN_NET_FRACTION}
        frac = sum(counted.values()) / area if area else 0.0
        if frac < ROLE_POUR_FRACTION:
            total += 1.0
        elif len(counted) >= 2:
            total += SPLIT_PLANE_WEIGHT
    return max(1.0, total)


def _plane_nets(board: Board) -> set[str]:
    """Routing-invariant set of nets carried by full/split copper pours."""
    area = board.board_area_mm2 or 0.0
    if not area:
        return metrics.zone_nets(board)
    last = len(board.copper_layers) - 1
    out: set[str] = set()
    for cl in board.copper_layers:
        fills = _layer_zone_areas(board, cl)
        counted = {n: a for n, a in fills.items() if n and a / area >= ROLE_MIN_NET_FRACTION}
        if not counted or sum(counted.values()) / area < ROLE_POUR_FRACTION:
            continue
        if 0 < cl.index < last:
            out.update(counted)
        else:
            top = max(counted.items(), key=lambda kv: (kv[1], kv[0]))[0]
            out.add(top)
    return out


def _track_pitch(board: Board) -> float:
    default = next((c for c in board.netclasses if c.name == "Default"), None)
    track_w = max(board.rules.get("min_track_width", 0.0), default.track_width if default else 0.15)
    clear = max(board.rules.get("min_clearance", 0.0), default.clearance if default else 0.15)
    return max(track_w + clear, 0.15)


def _grid_spec(board: Board, pitch: float) -> tuple[float, float, float, int, int] | None:
    box = board.outline_box
    if box is None:
        return None
    x0, y0, x1, y1 = box
    w, h = max(x1 - x0, 1e-3), max(y1 - y0, 1e-3)
    cell = max(1.0, 2.0 * pitch, max(w, h) / MAX_GRID_DIM)
    nx = max(1, math.ceil(w / cell))
    ny = max(1, math.ceil(h / cell))
    return x0, y0, cell, nx, ny


def estimate(board: Board, *, iterations: int = 8, capacity_scale: float = 1.0) -> dict:
    """Run Rust PathFinder global routing on ``board`` and return overflow + hotspots.

    ``capacity_scale`` (< 1.0) probes near-saturation regions (e.g. 0.85 highlights cells above
    85% edge utilization) without changing the default routing-congestion score.
    """
    pitch = _track_pitch(board)
    spec = _grid_spec(board, pitch)
    if spec is None:
        return {"available": False}

    try:
        import placer_core
        pf_fn = placer_core.pathfinder_estimate
    except (ImportError, AttributeError):
        return {"available": False}

    x0, y0, cell, nx, ny = spec
    sig_layers = _effective_signal_layers(board)
    scale = max(0.25, min(1.0, float(capacity_scale)))
    base_cap = max(1.0, sig_layers * (cell / pitch) * CAPACITY_EFFICIENCY * scale)

    outline = legality.board_polygon(board)
    outline_prep = prep(outline) if outline is not None else None
    inside = [True] * (nx * ny)
    if outline_prep is not None:
        for gy in range(ny):
            cy = y0 + (gy + 0.5) * cell
            for gx in range(nx):
                cx = x0 + (gx + 0.5) * cell
                if not outline_prep.covers(Point(cx, cy)):
                    inside[gy * nx + gx] = False

    # Pad blockage reduces local edge capacity proportionally to pad footprint / track pitch.
    cell_block = [0.0] * (nx * ny)
    for fp in board.footprints:
        for pad, (px, py) in fp.pads_world():
            gx = min(nx - 1, max(0, int((px - x0) // cell)))
            gy = min(ny - 1, max(0, int((py - y0) // cell)))
            layer_factor = sig_layers if pad.kind in ("thru_hole", "np_thru_hole") else 1.0
            span = max(pad.size[0], pad.size[1]) / pitch
            cell_block[gy * nx + gx] += 0.25 * layer_factor * min(span, cell / pitch)

    nh = max(0, nx - 1) * ny
    nv = nx * max(0, ny - 1)
    cap_h = [0.0] * nh
    cap_v = [0.0] * nv
    for gy in range(ny):
        for gx in range(nx - 1):
            a, b = gy * nx + gx, gy * nx + gx + 1
            if inside[a] and inside[b]:
                blk = 0.5 * (cell_block[a] + cell_block[b])
                cap_h[gy * (nx - 1) + gx] = max(0.5 if blk > 0 else 1.0, base_cap - blk)
    for gy in range(ny - 1):
        for gx in range(nx):
            a, b = gy * nx + gx, (gy + 1) * nx + gx
            if inside[a] and inside[b]:
                blk = 0.5 * (cell_block[a] + cell_block[b])
                cap_v[gy * nx + gx] = max(0.5 if blk > 0 else 1.0, base_cap - blk)

    planes = _plane_nets(board)
    nets_cells: list[list[tuple[int, int]]] = []
    for net, pts in sorted(metrics.net_points(board).items()):
        if net in planes or len(pts) < 2:
            continue
        cells = [(min(nx - 1, max(0, int((px - x0) // cell))),
                  min(ny - 1, max(0, int((py - y0) // cell)))) for px, py in pts]
        nets_cells.append(cells)

    raw = pf_fn(nx, ny, cap_h, cap_v, nets_cells, iterations=iterations)
    cell_ov: list[float] = raw["cell_overflow"]
    hotspots = []
    for gy in range(ny):
        for gx in range(nx):
            ov = cell_ov[gy * nx + gx]
            if ov > 1e-6:
                hotspots.append({
                    "gx": gx, "gy": gy,
                    "x_mm": round(x0 + (gx + 0.5) * cell, 3),
                    "y_mm": round(y0 + (gy + 0.5) * cell, 3),
                    "overflow": round(ov, 3),
                })
    hotspots.sort(key=lambda h: (-h["overflow"], h["gy"], h["gx"]))

    total_ov = float(raw["total_overflow"])
    energy = float(raw["congestion_energy"])
    wl_mm = float(raw["total_wirelength_cells"]) * cell
    routability_cost = round(total_ov * 10.0 + energy + 0.05 * wl_mm, 3)

    return {
        "available": True,
        "engine": "placer_core.pathfinder",
        "cell_mm": round(cell, 3),
        "grid": [nx, ny],
        "signal_layers": round(sig_layers, 2),
        "edge_capacity": round(base_cap, 3),
        "total_overflow": round(total_ov, 3),
        "max_overflow": round(float(raw["max_overflow"]), 3),
        "overflowed_edges": int(raw["overflowed_edges"]),
        "peak_utilization": round(float(raw["peak_utilization"]), 3),
        "congestion_energy": round(energy, 3),
        "total_wirelength_mm": round(wl_mm, 2),
        "routed_connections": int(raw["routed_connections"]),
        "iterations_run": int(raw["iterations_run"]),
        "congestion_score": float(raw["congestion_score"]),
        "routability_cost": routability_cost,
        "hotspots": hotspots[:16],
        "_grid_meta": (x0, y0, cell, nx, ny, cell_ov),
    }


def public_summary(est: dict) -> dict:
    """Return a JSON-serializable view of ``estimate`` without internal grid metadata."""
    return {k: v for k, v in est.items() if not k.startswith("_")}


def summary(board: Board, *, iterations: int = 8) -> dict:
    """Run ``estimate`` and strip internal grid metadata for reports/evaluations."""
    return public_summary(estimate(board, iterations=iterations))


def part_inflations(board: Board, est: dict, fixed: set[str], *,
                    max_inflate_mm: float = 0.6, scale_mm: float = 0.2) -> dict[str, float]:
    """Compute per-movable-part courtyard inflation (mm) from PathFinder cell overflow."""
    meta = est.get("_grid_meta")
    if not est.get("available") or meta is None or est.get("total_overflow", 0.0) <= 1e-6:
        return {}
    x0, y0, cell, nx, ny, cell_ov = meta
    inflations: dict[str, float] = {}
    for fp in board.footprints:
        if fp.ref in fixed or fp.locked:
            continue
        bx0, by0, bx1, by1 = fp.box()
        gx0 = min(nx - 1, max(0, int((bx0 - x0) // cell)))
        gx1 = min(nx - 1, max(0, int((bx1 - x0) // cell)))
        gy0 = min(ny - 1, max(0, int((by0 - y0) // cell)))
        gy1 = min(ny - 1, max(0, int((by1 - y0) // cell)))
        peak = 0.0
        for gy in range(gy0, gy1 + 1):
            for gx in range(gx0, gx1 + 1):
                peak = max(peak, cell_ov[gy * nx + gx])
        if peak > 0.05:
            inflations[fp.ref] = round(min(max_inflate_mm, scale_mm * math.sqrt(peak)), 3)
    return inflations
