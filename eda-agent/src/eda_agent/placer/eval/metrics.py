# SPDX-License-Identifier: MIT
"""Placement metrics on the Board IR alone (no router): plan F0b, task 3.

hpwl            half-perimeter wire length, all nets and signal nets
crossings       proper crossings between pin-pair (MST) edges of different signal nets
rudy            rectangular uniform wire density against track capacity
critical_loops  decap -> target supply pin and crystal distances, from design_intent (F2);
                v0 prefix heuristics only when no intent is given
regularity      rotation grid and alignment of repeated passives

"Signal nets" exclude the nets that fill a plane or pour (layer roles), since
they connect through copper zones rather than pin-to-pin wires.  The critical
loop distances follow the intent's roles and targets; a target given only as
candidates is measured to the nearest candidate pin on the same net.
"""

from __future__ import annotations

import math
from collections import Counter
from statistics import median

import numpy as np
from shapely import STRtree
from shapely.geometry import LineString

from ..ir import Board, Footprint, is_ground_net, is_power_net

RUDY_CELL_MM = 1.0  # declared parameter
ALIGN_TOL_MM = 0.05
ALIGN_RADIUS_MM = 5.0
PASSIVE_PREFIXES = ("R", "C", "L", "D", "FB", "LED")


def zone_nets(board: Board) -> set[str]:
    return {r.net for r in board.stackup.roles if r.role in ("plane", "mixed") and r.net} | {
        n for r in board.stackup.roles if r.role == "plane" for n in r.nets}


def net_points(board: Board) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for fp in board.footprints:
        for pad, (x, y) in fp.pads_world():
            if pad.net:
                out.setdefault(pad.net, []).append((x, y))
    return {n: pts for n, pts in sorted(out.items()) if len(pts) >= 2}


def _hpwl(points: list[tuple[float, float]]) -> float:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def hpwl(board: Board) -> dict:
    nets = net_points(board)
    planes = zone_nets(board)
    try:  # the Rust core's kernel when it is built (F0b toolchain spike)
        import placer_core

        xs, ys, idx = [], [], []
        for pts in nets.values():
            idx.append(list(range(len(xs), len(xs) + len(pts))))
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
        total = placer_core.hpwl(xs, ys, idx)
        engine = "placer_core"
    except ImportError:
        total = sum(_hpwl(p) for p in nets.values())
        engine = "python"
    signal = sum(_hpwl(p) for n, p in nets.items() if n not in planes)
    return {"all_mm": round(total, 3), "signal_mm": round(signal, 3), "nets": len(nets), "engine": engine}


def mst_edges(points: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Prim's MST on Euclidean distance; deterministic for a fixed point order."""
    n = len(points)
    if n < 2:
        return []
    inside = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    edges = []
    for _ in range(n):
        u = min((i for i in range(n) if not inside[i]), key=lambda i: (best[i], i))
        inside[u] = True
        if parent[u] >= 0:
            edges.append((points[parent[u]], points[u]))
        ux, uy = points[u]
        for v in range(n):
            if not inside[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < best[v]:
                    best[v], parent[v] = d, u
    return edges


def crossings(board: Board) -> dict:
    planes = zone_nets(board)
    lines, owner = [], []
    for net, pts in net_points(board).items():
        if net in planes:
            continue
        for a, b in mst_edges(pts):
            if a != b:
                lines.append(LineString([a, b]))
                owner.append(net)
    if len(lines) < 2:
        return {"count": 0, "edges": len(lines), "per_edge": 0.0}
    tree = STRtree(lines)
    count = sum(1 for i, j in zip(*tree.query(lines, predicate="crosses")) if i < j and owner[i] != owner[j])
    return {"count": count, "edges": len(lines), "per_edge": round(count / len(lines), 4)}


def rudy(board: Board) -> dict:
    box = board.outline_box
    if box is None:
        return {"available": False}
    planes = zone_nets(board)
    cell = RUDY_CELL_MM
    nx, ny = max(1, math.ceil((box[2] - box[0]) / cell)), max(1, math.ceil((box[3] - box[1]) / cell))
    demand = np.zeros((ny, nx))
    for net, pts in net_points(board).items():
        if net in planes:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        w, h = max(max(xs) - min(xs), cell), max(max(ys) - min(ys), cell)
        i0, i1 = int((min(xs) - box[0]) // cell), int((max(xs) - box[0]) // cell)
        j0, j1 = int((min(ys) - box[1]) // cell), int((max(ys) - box[1]) // cell)
        demand[max(j0, 0):min(j1, ny - 1) + 1, max(i0, 0):min(i1, nx - 1) + 1] += (w + h) / (w * h) * cell * cell
    layers = sum(1 for r in board.stackup.roles if r.role != "plane") or 1
    default = next((c for c in board.netclasses if c.name == "Default"), None)
    pitch = (default.track_width + default.clearance) if default else 0.4
    capacity = layers * cell * cell / pitch  # mm of track a cell can hold
    util = demand / capacity
    return {"available": True, "cell_mm": cell, "capacity_mm_per_cell": round(capacity, 3),
            "peak": round(float(util.max()), 3), "p95": round(float(np.percentile(util, 95)), 3),
            "over_capacity_fraction": round(float((util > 1.0).mean()), 4)}


def _is_ic(fp: Footprint) -> bool:
    return fp.prefix in ("U", "IC") and len(fp.pads) >= 4


def _stats(v: list[float]) -> dict:
    if not v:
        return {"n": 0}
    s = sorted(v)
    return {"n": len(s), "median_mm": round(median(s), 3), "p90_mm": round(s[math.ceil(0.9 * len(s)) - 1], 3),
            "max_mm": round(s[-1], 3)}


def critical_loops_intent(board: Board, intent: dict) -> dict:
    """Distances that the roles in ``intent`` make critical, measured pad to pad on the same net."""
    from ..intent import model

    fps = {fp.ref: fp for fp in board.footprints}
    parts = intent.get("parts", {})

    def pins(ref: str, net: str, numbers: set[str]) -> list[tuple[float, float]]:
        fp = fps.get(ref)
        return [pos for pad, pos in fp.pads_world()
                if pad.net == net and (not numbers or pad.number in numbers)] if fp else []

    def nearest(ref: str, targets: list[str], net: str | None, numbers: set[str]) -> float | None:
        fp = fps.get(ref)
        if fp is None:
            return None
        best = None
        for pad, (x, y) in fp.pads_world():
            if not pad.net or is_ground_net(pad.net) or (net and pad.net != net):
                continue
            for t in targets:
                for a, b in pins(t, pad.net, numbers):
                    d = math.hypot(a - x, b - y)
                    best = d if best is None or d < best else best
        return best

    decap, resolved_decap, load, crystal, unsettled = [], [], [], [], 0
    for ref, part in parts.items():
        role = part["role"]["value"]
        tv = (part.get("target") or {}).get("value") or {}
        targets = [tv["ref"]] if tv.get("ref") else list(tv.get("candidates", []))
        if not targets:
            continue
        numbers = set(tv.get("pins", ())) if tv.get("ref") else set()
        d = nearest(ref, targets, tv.get("net"), numbers)
        if d is None:
            continue
        if role == "decap":
            decap.append(d)
            unsettled += tv.get("ref") is None
            if tv.get("ref") and not model.is_uncertain(part["role"]) and not model.is_uncertain(part.get("target")):
                resolved_decap.append(d)
        elif role == "crystal_load_cap":
            load.append(d)
        elif role == "crystal":
            crystal.append(d)
    return {"method": "design_intent v1 (role targets; candidates measured to the nearest)",
            "intent_sha256": None, "decap_pin_distance": _stats(decap), "decaps_with_candidate_targets": unsettled,
            "resolved_decap_pin_distance": _stats(resolved_decap),
            "crystal_ic_distance": _stats(crystal), "crystal_load_distance": _stats(load)}


def critical_loops(board: Board, intent: dict | None = None) -> dict:
    if intent is not None:
        return critical_loops_intent(board, intent)
    decaps = []
    for fp in board.footprints:
        nets = [p.net for p in fp.pads]
        if fp.prefix == "C" and len(fp.pads) == 2 and any(is_ground_net(n) for n in nets) and any(
                n and is_power_net(n) and not is_ground_net(n) for n in nets):
            power = next(p for p in fp.pads if p.net and not is_ground_net(p.net))
            decaps.append((power.net, fp.side, fp.pad_world(power)))
    pin_dist = []
    for fp in board.footprints:
        if not _is_ic(fp):
            continue
        for pad, (x, y) in fp.pads_world():
            if pad.net and is_power_net(pad.net) and not is_ground_net(pad.net):
                cands = [math.hypot(x - cx, y - cy) for net, _side, (cx, cy) in decaps if net == pad.net]
                if cands:
                    pin_dist.append(min(cands))
    crystal = []
    ic_pads = {}
    for fp in board.footprints:
        if _is_ic(fp):
            for pad, pos in fp.pads_world():
                if pad.net:
                    ic_pads.setdefault(pad.net, []).append(pos)
    for fp in board.footprints:
        if fp.prefix in ("Y", "X"):
            for pad, (x, y) in fp.pads_world():
                if pad.net and not is_ground_net(pad.net) and pad.net in ic_pads:
                    crystal.append(min(math.hypot(x - a, y - b) for a, b in ic_pads[pad.net]))

    return {"method": "v0 heuristic (prefix + net names); used only without an intent",
            "decap_pin_distance": _stats(pin_dist), "crystal_ic_distance": _stats(crystal)}


def regularity(board: Board) -> dict:
    passives = [fp for fp in board.footprints if fp.prefix in PASSIVE_PREFIXES and len(fp.pads) <= 3]
    if not passives:
        return {"n": 0}
    on_grid = sum(1 for fp in board.footprints if abs(fp.rotation / 90 - round(fp.rotation / 90)) < 1e-6)
    aligned = 0
    for fp in passives:
        for other in passives:
            if other is fp or other.lib_id != fp.lib_id or other.side != fp.side:
                continue
            dx, dy = abs(other.x - fp.x), abs(other.y - fp.y)
            if math.hypot(dx, dy) <= ALIGN_RADIUS_MM and (dx <= ALIGN_TOL_MM or dy <= ALIGN_TOL_MM):
                aligned += 1
                break
    axis = Counter(round(fp.rotation) % 180 for fp in passives)
    return {"n": len(passives), "aligned_fraction": round(aligned / len(passives), 4),
            "rotation_on_90_grid": round(on_grid / len(board.footprints), 4),
            "dominant_axis_fraction": round(axis.most_common(1)[0][1] / len(passives), 4)}
