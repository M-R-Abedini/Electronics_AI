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
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from ..geometry import transform
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


def has_thru_hole_pad(pad) -> bool:
    drill = getattr(pad, "drill", None)
    has_drill = bool(drill and (max(drill) > 0 if isinstance(drill, (tuple, list)) else drill > 0))
    return (
        pad.kind in ("thru_hole", "np_thru_hole")
        or ("F.Cu" in pad.layers and "B.Cu" in pad.layers)
        or has_drill
    )


def _pad_polygon(fp: Footprint, pad, margin_mm: float = 0.0) -> Polygon | None:
    hw = pad.size[0] / 2 + margin_mm
    hh = pad.size[1] / 2 + margin_mm
    if hw <= 0 or hh <= 0:
        return None
    if pad.shape.lower() == "circle" and abs(pad.size[0] - pad.size[1]) < 1e-6:
        px, py = fp.pad_world(pad)
        return Point(px, py).buffer(hw)
    corners = [
        transform(transform((dx, dy), pad.local, pad.rotation), (fp.x, fp.y), fp.rotation)
        for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]
    return Polygon(corners)


def thru_hole_pad_polygon(fp: Footprint, margin_mm: float = 0.0):
    polys = []
    for pad in fp.pads:
        if not has_thru_hole_pad(pad):
            continue
        poly = _pad_polygon(fp, pad, margin_mm=margin_mm)
        if poly is not None and not poly.is_empty:
            polys.append(poly)
    return unary_union(polys) if polys else None


def _iter_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_iter_polygons(g))
        return out
    return []


def thru_hole_overhang_mm(fp: Footprint) -> float:
    """Maximum protrusion (mm) of through-hole pads beyond the footprint courtyard."""
    th = thru_hole_pad_polygon(fp, margin_mm=0.0)
    cy = courtyard_polygon(fp)
    if th is None or cy is None:
        return 0.0
    tbx, cbx = th.bounds, cy.bounds
    return max(0.0, cbx[0] - tbx[0], tbx[2] - cbx[2], cbx[1] - tbx[1], tbx[3] - cbx[3])


def thru_hole_hole_rings(
    fp: Footprint, margin_mm: float = 0.08, *, thru_hole_only: bool = True,
) -> list[list[tuple[float, float]]]:
    """Merged rectangular hole rings around a footprint's through-hole (or all) pads."""
    polys = [
        poly for pad in fp.pads
        if (not thru_hole_only or has_thru_hole_pad(pad))
        and (poly := _pad_polygon(fp, pad, margin_mm=margin_mm)) is not None
        and not poly.is_empty
    ]
    if not polys:
        return []
    merged = unary_union(polys).buffer(0.25).buffer(-0.25)
    rings: list[list[tuple[float, float]]] = []
    for comp in _iter_polygons(merged):
        if comp.area <= 0:
            continue
        x0, y0, x1, y1 = comp.bounds
        rings.append([
            (round(x0, 6), round(y0, 6)),
            (round(x1, 6), round(y0, 6)),
            (round(x1, 6), round(y1, 6)),
            (round(x0, 6), round(y1, 6)),
        ])
    return rings


def board_copper_obstacles(board: Board, fixed: set[str] | None = None) -> list[tuple[str, str, Polygon]]:
    """Free-standing board copper graphics (outside fixed footprints) per side."""
    fixed_set = board.fixed_refs() if fixed is None else set(fixed)
    existing = {fp.ref for fp in board.footprints}
    fixed_shapes = [
        shape for r in fixed_set
        if r in existing and (shape := courtyard_polygon(board.fp(r))) is not None
    ]
    fixed_union = unary_union(fixed_shapes) if fixed_shapes else None
    names = {cl.name: cl.canonical for cl in board.copper_layers}
    names.update({"F.Cu": "F.Cu", "B.Cu": "B.Cu", "top_layer": "F.Cu", "bottom_layer": "B.Cu"})
    out: list[tuple[str, str, Polygon]] = []
    for obs in board.obstacles:
        if obs.get("source") != "board" or obs.get("kind") != "copper_graphic" or obs.get("net"):
            continue
        canon = names.get(obs.get("copper") or obs.get("layer"))
        copper_side = "F" if canon == "F.Cu" else "B" if canon == "B.Cu" else None
        if copper_side is None:
            continue
        polys = [Polygon(ring) for ring in obs.get("outlines", []) if len(ring) >= 3]
        polys = [p if p.is_valid else p.buffer(0) for p in polys]
        polys = [p for p in polys if not p.is_empty]
        if not polys:
            continue
        poly = unary_union(polys)
        free_poly = poly.difference(fixed_union) if fixed_union is not None and not fixed_union.is_empty else poly
        uid = str(obs.get("uuid") or "")
        for comp in _iter_polygons(free_poly):
            if comp.area > OVERLAP_TOL_MM2:
                out.append((uid, copper_side, comp))
    return out


def placement_obstacle_holes(board: Board, fixed: set[str], two_sided: bool) -> list[list[tuple[float, float]]]:
    """Hole rings for solver stage: free board copper graphics plus fixed TH pads on 2-sided boards."""
    holes: list[list[tuple[float, float]]] = []
    for _uid, _side, comp in board_copper_obstacles(board, fixed):
        coords = list(comp.exterior.coords)
        if len(coords) >= 4:
            holes.append([(round(x, 6), round(y, 6)) for x, y in coords[:-1]])
    if two_sided:
        existing = {fp.ref for fp in board.footprints}
        for ref in sorted(fixed):
            if ref in existing:
                holes.extend(thru_hole_hole_rings(board.fp(ref), margin_mm=0.08))
    return holes


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

    sides_present = {fp.side for fp in board.footprints}
    if "F" in sides_present and "B" in sides_present:
        min_pad_clear = max(0.15, float(board.rules.get("min_clearance") or 0.15))
        pad_unions: dict[str, object] = {}
        raw_pad_unions: dict[str, object] = {}
        th_pairs: dict[tuple[str, str], tuple[float, str, str]] = {}
        for fp1 in board.footprints:
            th_poly = thru_hole_pad_polygon(fp1, margin_mm=0.05)
            th_raw = thru_hole_pad_polygon(fp1, margin_mm=0.0)
            if th_poly is None or th_raw is None:
                continue
            tbx = th_raw.bounds
            for fp2 in board.footprints:
                if fp1.ref == fp2.ref or fp1.side == fp2.side or (fp1.ref in fixed and fp2.ref in fixed):
                    continue
                if fp2.ref not in pad_unions:
                    pads2 = [p_poly for pad in fp2.pads if (p_poly := _pad_polygon(fp2, pad, margin_mm=0.10)) is not None]
                    raw2 = [p_poly for pad in fp2.pads if (p_poly := _pad_polygon(fp2, pad, margin_mm=0.0)) is not None]
                    pad_unions[fp2.ref] = unary_union(pads2) if pads2 else None
                    raw_pad_unions[fp2.ref] = unary_union(raw2) if raw2 else None
                u2 = pad_unions[fp2.ref]
                u2_raw = raw_pad_unions[fp2.ref]
                if u2 is None or u2.is_empty or u2_raw is None or u2_raw.is_empty:
                    continue
                ubx = u2_raw.bounds
                if (
                    ubx[0] > tbx[2] + min_pad_clear
                    or ubx[2] < tbx[0] - min_pad_clear
                    or ubx[1] > tbx[3] + min_pad_clear
                    or ubx[3] < tbx[1] - min_pad_clear
                ):
                    continue
                area = u2.intersection(th_poly).area
                if area <= OVERLAP_TOL_MM2:
                    raw_dist = th_raw.distance(u2_raw)
                    if raw_dist < min_pad_clear - 1e-4:
                        area = round(max(0.0101, (min_pad_clear - raw_dist) * 0.5), 4)
                if area > OVERLAP_TOL_MM2:
                    pair = (fp1.ref, fp2.ref) if fp1.ref < fp2.ref else (fp2.ref, fp1.ref)
                    prev = th_pairs.get(pair)
                    if prev is None or area > prev[0]:
                        th_pairs[pair] = (area, fp2.side, fp1.ref)
        for (r1, r2), (area, side_hit, owner_ref) in sorted(th_pairs.items()):
            findings.append(Finding(
                "courtyard_overlap", [r1, r2], round(area, 4), side_hit,
                detail=f"thru_hole_pads:{owner_ref}", fixed=False, waived_by=overlap_waiver,
            ))

    for uid, copper_side, obs_poly in board_copper_obstacles(board, fixed):
        obx = obs_poly.bounds
        for fp in board.footprints:
            if fp.ref in fixed:
                continue
            shape = shapes[fp.ref]
            if shape is None:
                continue
            if fp.side != copper_side and not any(has_thru_hole_pad(pad) for pad in fp.pads):
                continue
            sbx = shape.bounds
            if sbx[0] > obx[2] or sbx[2] < obx[0] or sbx[1] > obx[3] or sbx[3] < obx[1]:
                continue
            area = shape.intersection(obs_poly).area
            if area > OVERLAP_TOL_MM2:
                findings.append(Finding(
                    "keepout", [fp.ref], round(area, 4), fp.side,
                    detail=f"copper_graphic:{uid}", fixed=False,
                ))

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
