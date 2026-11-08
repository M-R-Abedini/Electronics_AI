# SPDX-License-Identifier: MIT
"""Board IR -> placement problem (S0/S1/S2b) and the Python reference of the core cost.

S1 fixed parts (architecture 5): locked footprints, plus -- until intent names
them in F2 -- mechanical parts without nets (holes, logos), connectors that sit
on the board edge, and parts that already overhang the outline.  The same set is
fixed for the expert in every comparison (principle P2).

S2b sides: ``expert`` keeps every part's side (comparisons, P2), ``top_only``
is assembly level 1 for a new design (architecture 4.3).

Net weights (declared parameters): signal nets 1.0 with crossing count; nets
reaching the board through a plane or pour 0.2 without crossings; ground nets 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..eval.legality import board_polygon, courtyard_polygon
from ..eval.metrics import zone_nets
from ..ir import Board, Footprint, is_ground_net

CONNECTOR_PREFIXES = ("J", "P", "CN", "CON", "USB")  # not "X": crystals in some libraries
EDGE_DISTANCE_MM = 1.5  # declared: a connector this close to the outline edge is an edge connector (StickHub JSTs: 1.125 mm)
WEIGHT_SIGNAL = 1.0
WEIGHT_PLANE_NET = 0.2
GAP_MM = 0.0  # declared: extra courtyard gap; courtyards already carry the library's clearance


def local_box(fp: Footprint) -> tuple[float, float, float, float]:
    pts = [p for poly in fp.courtyard.polygons for p in poly]
    if not pts:
        pts = []
        for pad in fp.pads:
            w, h = pad.size
            pts += [(pad.local[0] - w / 2, pad.local[1] - h / 2), (pad.local[0] + w / 2, pad.local[1] + h / 2)]
    if not pts:
        return (-0.1, -0.1, 0.1, 0.1)
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def fixed_parts(board: Board) -> dict[str, str]:
    """S1: reference -> reason for every part the placer must not move."""
    out: dict[str, str] = {}
    outline = board_polygon(board)
    edge = outline.exterior if outline is not None and outline.geom_type == "Polygon" else None
    for fp in board.footprints:
        if fp.locked:
            out[fp.ref] = "locked in the file"
            continue
        if not any(p.net for p in fp.pads):
            out[fp.ref] = "mechanical (no nets)"
            continue
        shape = courtyard_polygon(fp)
        if shape is None or outline is None:
            continue
        if shape.difference(outline).area > 0.01:
            out[fp.ref] = "overhangs the board outline"
        elif fp.prefix.startswith(CONNECTOR_PREFIXES) and edge is not None and shape.distance(edge) <= EDGE_DISTANCE_MM:
            out[fp.ref] = "connector on the board edge"
    return dict(sorted(out.items()))


def assign_sides(board: Board, policy: str, fixed: dict[str, str]) -> dict[str, str]:
    """S2b: side of every part.  ``expert`` keeps the input sides; ``top_only`` = assembly level 1."""
    if policy == "expert":
        return {fp.ref: fp.side for fp in board.footprints}
    if policy == "top_only":
        return {fp.ref: (fp.side if fp.ref in fixed else "F") for fp in board.footprints}
    raise ValueError(f"unknown side policy {policy!r}")


@dataclass
class PlaceProblem:
    """Index maps between the IR and the Rust problem, and the arrays the core consumes."""

    board: Board
    refs: list[str]
    fixed: dict[str, str]
    sides: dict[str, str]
    parts: list[tuple] = field(default_factory=list)
    pins: list[tuple[int, float, float]] = field(default_factory=list)
    nets: list[tuple[list[int], float, bool]] = field(default_factory=list)
    net_names: list[str] = field(default_factory=list)
    outers: list[list[tuple[float, float]]] = field(default_factory=list)
    holes: list[list[tuple[float, float]]] = field(default_factory=list)

    def poses(self) -> list[tuple[float, float, int]]:
        return [(p[7], p[8], p[9]) for p in self.parts]


def _quarter(rotation: float) -> int:
    return round(rotation / 90.0) % 4


def build(board: Board, *, side_policy: str = "expert") -> PlaceProblem:
    fixed = fixed_parts(board)
    sides = assign_sides(board, side_policy, fixed)
    planes = zone_nets(board) | {n for r in board.stackup.roles for n in r.nets}
    pb = PlaceProblem(board=board, refs=[fp.ref for fp in board.footprints], fixed=fixed, sides=sides)
    pin_of: dict[str, list[int]] = {}
    for i, fp in enumerate(board.footprints):
        is_fixed = fp.ref in fixed
        flip = sides[fp.ref] != fp.side
        x0, y0, x1, y1 = local_box(fp)
        if flip:  # the footprint frame mirrors in y when the part changes side (KiCad left-right flip)
            y0, y1 = -y1, -y0
        on_grid = abs(fp.rotation / 90.0 - round(fp.rotation / 90.0)) < 1e-6
        if is_fixed and not on_grid:
            # a fixed part at an odd angle keeps its true courtyard through its world bounding box
            shape = courtyard_polygon(fp)
            bx0, by0, bx1, by1 = shape.bounds if shape is not None else (fp.x, fp.y, fp.x, fp.y)
            x0, y0, x1, y1, r = bx0 - fp.x, by0 - fp.y, bx1 - fp.x, by1 - fp.y, 0
        else:
            r = _quarter(fp.rotation) if on_grid else 0
        pb.parts.append((x0, y0, x1, y1, 0 if sides[fp.ref] == "F" else 1, is_fixed, not is_fixed,
                         fp.x, fp.y, r))
        for pad in fp.pads:
            if not pad.net:
                continue
            lx, ly = pad.local
            if flip:
                ly = -ly
            if is_fixed and not on_grid:  # express the pin in the world-axis frame used above
                wx, wy = fp.pad_world(pad)
                lx, ly = wx - fp.x, wy - fp.y
            pin_of.setdefault(pad.net, []).append(len(pb.pins))
            pb.pins.append((i, lx, ly))
    for net, pins in sorted(pin_of.items()):
        if len(pins) < 2 or is_ground_net(net):
            continue
        plane = net in planes
        pb.nets.append((pins, WEIGHT_PLANE_NET if plane else WEIGHT_SIGNAL, not plane))
        pb.net_names.append(net)
    for o in board.outline:
        pb.outers.append([tuple(p) for p in o.outer])
        pb.holes += [[tuple(p) for p in h] for h in o.holes]
    return pb


def rust_problem(pb: PlaceProblem, *, w_hpwl: float = 1.0, w_cross: float = 2.0, gap: float = GAP_MM):
    from placer_core import PlacementProblem

    return PlacementProblem(pb.parts, pb.pins, pb.nets, pb.outers, pb.holes, gap, w_hpwl, w_cross)


def apply(pb: PlaceProblem, poses: list[tuple[float, float, int]]) -> Board:
    """A copy of the board with the solved poses (rotation in degrees, side from S2b)."""
    out = pb.board.copy()
    for fp, (x, y, r), part in zip(out.footprints, poses, pb.parts, strict=True):
        if part[5]:
            continue  # fixed parts keep their exact pose, odd angles included
        fp.x, fp.y, fp.rotation = round(x, 4), round(y, 4), float(r * 90 % 360)
        fp.side = pb.sides[fp.ref]
    return out


# --------------------------------------------------------------------------
# Python reference of the core cost (parity tests and the speed gate)
# --------------------------------------------------------------------------
def _rot90(x: float, y: float, r: int) -> tuple[float, float]:
    return ((x, y), (y, -x), (-x, -y), (-y, x))[r & 3]


def reference_cost(pb: PlaceProblem, poses: list[tuple[float, float, int]], *, w_hpwl: float = 1.0,
                   w_cross: float = 2.0) -> dict[str, float]:
    from ..eval.metrics import mst_edges
    from ..geometry import segments_cross

    def xy(pin: int) -> tuple[float, float]:
        part, lx, ly = pb.pins[pin]
        x, y, r = poses[part]
        dx, dy = _rot90(lx, ly, r)
        return x + dx, y + dy

    hpwl = 0.0
    edges: list[tuple[tuple[float, float], tuple[float, float], int]] = []
    for n, (pins, weight, crossing) in enumerate(pb.nets):
        pts = [xy(p) for p in pins]
        hpwl += weight * ((max(p[0] for p in pts) - min(p[0] for p in pts))
                          + (max(p[1] for p in pts) - min(p[1] for p in pts)))
        if crossing and 2 <= len(pins) <= 16:
            edges += [(a, b, n) for a, b in mst_edges(pts) if a != b]
    crossings = sum(1 for i in range(len(edges)) for j in range(i + 1, len(edges))
                    if edges[i][2] != edges[j][2] and segments_cross(edges[i][0], edges[i][1], edges[j][0], edges[j][1]))
    return {"hpwl": hpwl, "crossings": float(crossings), "total": w_hpwl * hpwl + w_cross * crossings}


def board_diag(pb: PlaceProblem) -> float:
    xs = [p[0] for ring in pb.outers for p in ring]
    ys = [p[1] for ring in pb.outers for p in ring]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 50.0


