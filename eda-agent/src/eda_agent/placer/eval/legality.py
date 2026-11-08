# SPDX-License-Identifier: MIT
"""Placement legality, the first hard gate (architecture 8.1.1).

Checks courtyard overlap per side, courtyard outside the board outline,
footprint keepouts and side permission.  Every finding names the parts, so
on an expert placement each one can be judged an expert exception or an IR
bug (plan F0b acceptance).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from shapely import STRtree
from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..ir import Board, Footprint

OVERLAP_TOL_MM2 = 0.01  # touching or numerically grazing courtyards are not an overlap
OUTSIDE_TOL_MM2 = 0.01


@dataclass
class Finding:
    kind: str  # courtyard_overlap | outside_outline | keepout | side_not_allowed
    refs: list[str]
    area_mm2: float
    side: str | None = None
    detail: str = ""
    fixed: bool = False  # every part involved is fixed (locked); not the placer's doing
    waived_by: str | None = None  # project rule that accepts it on the input board (expert exception)


def courtyard_polygon(fp: Footprint) -> Polygon | None:
    polys = [Polygon(p) for p in fp.courtyard_world() if len(p) >= 3]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    polys = [p for p in polys if not p.is_empty]
    if not polys:
        return None
    shape = unary_union(polys)
    return shape if shape.geom_type == "Polygon" else shape.convex_hull


def board_polygon(board: Board) -> Polygon | None:
    parts = [Polygon(o.outer, o.holes) for o in board.outline if len(o.outer) >= 3]
    parts = [p if p.is_valid else p.buffer(0) for p in parts]
    return unary_union(parts) if parts else None


def check(board: Board, *, clearance_mm: float | None = None, fixed: set[str] | None = None) -> list[Finding]:
    """fixed: parts the placer did not move (S1); defaults to the locked footprints."""
    clearance = board.rules.get("courtyard_clearance", 0.0) if clearance_mm is None else clearance_mm
    fixed = board.fixed_refs() if fixed is None else set(fixed)
    shapes = {fp.ref: courtyard_polygon(fp) for fp in board.footprints}
    sev = board.drc_severities.get("courtyards_overlap")
    overlap_waiver = f"project rule courtyards_overlap={sev}" if sev else None
    findings: list[Finding] = []

    for side in ("F", "B"):
        refs = [fp.ref for fp in board.footprints if fp.side == side and shapes[fp.ref] is not None]
        geoms = [shapes[r].buffer(clearance / 2) if clearance else shapes[r] for r in refs]
        if len(geoms) < 2:
            continue
        tree = STRtree(geoms)
        for i, j in zip(*tree.query(geoms, predicate="intersects")):
            if i >= j:
                continue
            area = geoms[i].intersection(geoms[j]).area
            if area > OVERLAP_TOL_MM2:
                pair = sorted((refs[i], refs[j]))
                derived = [r for r in pair if board.fp(r).courtyard.source != "file"]
                findings.append(Finding("courtyard_overlap", pair, round(area, 4), side,
                                        detail=("derived courtyard: " + ", ".join(derived)) if derived else "",
                                        fixed=all(r in fixed for r in pair), waived_by=overlap_waiver))

    outline = board_polygon(board)
    if outline is not None:
        for fp in board.footprints:
            shape = shapes[fp.ref]
            if shape is None:
                continue
            outside = shape.difference(outline).area
            if outside > OUTSIDE_TOL_MM2:
                findings.append(Finding("outside_outline", [fp.ref], round(outside, 4), fp.side,
                                        detail=f"{outside / shape.area:.0%} of the courtyard", fixed=fp.ref in fixed))

    names = {cl.name: cl.canonical for cl in board.copper_layers}
    for area in board.rule_areas:
        if not area["keepout"].get("footprints"):
            continue
        sides = {"F" if names.get(n) == "F.Cu" else "B" if names.get(n) == "B.Cu" else None for n in area["layers"]}
        zone = unary_union([Polygon(p) for p in area["polygons"] if len(p) >= 3])
        for fp in board.footprints:
            shape = shapes[fp.ref]
            if shape is not None and fp.side in sides and shape.intersection(zone).area > OVERLAP_TOL_MM2:
                findings.append(Finding("keepout", [fp.ref], round(shape.intersection(zone).area, 4), fp.side,
                                        detail=area["uuid"], fixed=fp.ref in fixed))

    if not board.capabilities.can_flip:
        findings += [Finding("side_not_allowed", [fp.ref], 0.0, "B", detail="adapter cannot flip (level 1)")
                     for fp in board.footprints if fp.side == "B" and fp.ref not in fixed]
    findings.sort(key=lambda f: (f.kind, f.refs))
    return findings


def summary(findings: list[Finding]) -> dict:
    """``violations`` gates our own placements; waived and fixed findings are listed apart.

    A project waiver explains an expert board; it never excuses a placement we
    produce, so ``strict_violations`` counts waived findings too.
    """
    counts: dict[str, int] = {}
    strict = 0
    for f in findings:
        if f.fixed:
            continue
        strict += 1
        if not f.waived_by:
            counts[f.kind] = counts.get(f.kind, 0) + 1
    return {"violations": sum(counts.values()), "strict_violations": strict, "by_kind": dict(sorted(counts.items())),
            "waived": sum(1 for f in findings if f.waived_by and not f.fixed),
            "fixed_only": sum(1 for f in findings if f.fixed), "findings": [asdict(f) for f in findings]}
