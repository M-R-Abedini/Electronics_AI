# SPDX-License-Identifier: MIT
"""Floor baselines for the benchmark (plan F0b, task 5).

expert          the board as the designer placed it
random_legal    random legal poses for every movable part (seeded)
v1_constructor  the 1.0.x constructive placer ``design.pcb_placement``

Every baseline keeps the expert's fixed parts and each part's side
(principle P2: the comparison changes only what the placer decides).  A
baseline returns a new Board; it never writes files.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from shapely.geometry import Polygon
from shapely.prepared import prep

from .eval.legality import board_polygon, courtyard_polygon
from .geometry import transform
from .ir import Board, Footprint
from .place.problem import fixed_parts

MIL = 0.0254
RANDOM_TRIES = 400


def expert(board: Board, seed: int = 0) -> tuple[Board, dict]:
    return board.copy(), {"note": "designer placement"}


def _moved_shape(fp: Footprint, x: float, y: float, rot: float) -> Polygon | None:
    original = (fp.x, fp.y, fp.rotation)
    fp.x, fp.y, fp.rotation = x, y, rot
    try:
        return courtyard_polygon(fp)
    finally:
        fp.x, fp.y, fp.rotation = original


def random_legal(board: Board, seed: int = 0) -> tuple[Board, dict]:
    """Rejection sampling inside the outline, larger parts first; fixed parts stay."""
    out = board.copy()
    rng = random.Random(seed)
    outline = board_polygon(out)
    box = out.outline_box
    if outline is None or box is None:
        raise ValueError("random_legal needs a board outline")
    inside = prep(outline)
    fixed = set(fixed_parts(out))
    placed: dict[str, list[Polygon]] = {"F": [], "B": []}
    for fp in out.footprints:
        if fp.ref in fixed and (shape := courtyard_polygon(fp)) is not None:
            placed[fp.side].append(shape)
    movable = sorted((fp for fp in out.footprints if fp.ref not in fixed), key=lambda f: (-f.courtyard_area, f.ref))
    failed = []
    for fp in movable:
        for _ in range(RANDOM_TRIES):
            x, y = rng.uniform(box[0], box[2]), rng.uniform(box[1], box[3])
            rot = rng.choice((0.0, 90.0, 180.0, 270.0))
            shape = _moved_shape(fp, x, y, rot)
            if shape is None or (inside.contains(shape) and not any(shape.intersects(o) for o in placed[fp.side])):
                fp.x, fp.y, fp.rotation = round(x, 4), round(y, 4), rot
                if shape is not None:
                    placed[fp.side].append(shape)
                break
        else:
            failed.append(fp.ref)
            fp.x, fp.y = round(rng.uniform(box[0], box[2]), 4), round(rng.uniform(box[1], box[3]), 4)
    return out, {"seed": seed, "unplaced_legally": failed}


def v1_constructor(board: Board, seed: int = 0) -> tuple[Board, dict]:
    """Adapter to ``eda_agent.design.pcb_placement`` (mils, Y up, centroid frame)."""
    from eda_agent.design.pcb_placement import (
        ConstructOptions,
        DesignRules,
        construct_placement,
    )
    from eda_agent.placement.autoplace import BoardRegion, PlaceComp, PlaceNet, PlacePin

    out = board.copy()
    fixed = set(fixed_parts(out))
    comps, c0 = [], {}
    for fp in out.footprints:
        pts = [p for poly in fp.courtyard.polygons for p in poly] or [p.local for p in fp.pads] or [(0.0, 0.0)]
        lx0, ly0 = min(p[0] for p in pts), min(p[1] for p in pts)
        lx1, ly1 = max(p[0] for p in pts), max(p[1] for p in pts)
        c0[fp.ref] = ((lx0 + lx1) / 2, (ly0 + ly1) / 2)
        cx, cy = transform(c0[fp.ref], (fp.x, fp.y), fp.rotation)
        pins = tuple(PlacePin((p.local[0] - c0[fp.ref][0]) / MIL, -(p.local[1] - c0[fp.ref][1]) / MIL, p.net)
                     for p in fp.pads if p.net)
        comps.append(PlaceComp(ref=fp.ref, w=max(lx1 - lx0, 0.1) / MIL, h=max(ly1 - ly0, 0.1) / MIL,
                               cx=cx / MIL, cy=-cy / MIL, layer="Top" if fp.side == "F" else "Bottom",
                               fixed=fp.ref in fixed, rotation=fp.rotation, pins=pins,
                               rotatable=fp.ref not in fixed and abs(fp.rotation % 90) < 1e-6))
    nets = [PlaceNet(refs=tuple(sorted({f.ref for f, _ in members})), name=name)
            for name, members in sorted(out.net_pads().items()) if len({f.ref for f, _ in members}) >= 2]
    box = out.outline_box
    region = BoardRegion(box[0] / MIL, -box[3] / MIL, box[2] / MIL, -box[1] / MIL) if box else None
    rules = DesignRules(layers=min(out.layer_count, 2))
    result = construct_placement(comps, nets, rules, ConstructOptions(seed=seed), fixed_region=region,
                                 preserve_sides=True)
    for fp in out.footprints:
        if fp.ref in fixed or fp.ref not in result.centroids:
            continue
        cx, cy = result.centroids[fp.ref]
        rot = result.rotations.get(fp.ref, fp.rotation) % 360.0
        ox, oy = transform(c0[fp.ref], (0.0, 0.0), rot)
        fp.x, fp.y, fp.rotation = round(cx * MIL - ox, 4), round(-cy * MIL - oy, 4), rot
    return out, {"seed": seed, "notes": result.notes[:5], "grow_steps": result.grow_steps,
                 "region_mm": [round(v * MIL, 3) for v in (result.region.x1, result.region.y1,
                                                          result.region.x2, result.region.y2)]}


def placer_v2(board: Board, seed: int = 0) -> tuple[Board, dict]:
    """The 2.0 placer (S1 to S5), sides from the expert (P2)."""
    from .place.pipeline import solve

    return solve(board, side_policy="expert", mode="best", seed=seed)


BASELINES: dict[str, Callable[[Board, int], tuple[Board, dict]]] = {
    "expert": expert,
    "random_legal": random_legal,
    "v1_constructor": v1_constructor,
    "placer_v2": placer_v2,
}

