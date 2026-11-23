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
from shapely.prepared import prep

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


_CY_POLY_CACHE: dict[tuple, Polygon | None] = {}
_BOARD_POLY_CACHE: dict[tuple, Polygon | None] = {}
_INNER_OUTLINE_CACHE: dict[tuple, tuple] = {}
_COPPER_OBS_CACHE: dict[tuple, list[tuple[str, str, Polygon]]] = {}
_MASK_OBS_CACHE: dict[tuple, list[tuple[str, str, Polygon]]] = {}
_TH_INFO_CACHE: dict[tuple, tuple] = {}
_SIDE_PAD_CACHE: dict[tuple, tuple] = {}
_PAD_UNION_CACHE: dict[tuple, Polygon | None] = {}


def _pad_union(
    fp: Footprint,
    side: str,
    names: dict[str, str],
    *,
    margin_mm: float = 0.02,
) -> Polygon | None:
    """Cached same-side pad union for *fp*, keyed on pose, margin, and pad geometry."""
    pad_sig = tuple(
        (p.local, p.size, p.shape, p.rotation, p.kind, p.drill, tuple(p.layers))
        for p in fp.pads
    )
    key = (
        fp.ref,
        fp.lib_id,
        round(fp.x, 4),
        round(fp.y, 4),
        round(fp.rotation % 360.0, 2),
        fp.side,
        side,
        round(margin_mm, 4),
        pad_sig,
    )
    if key in _PAD_UNION_CACHE:
        return _PAD_UNION_CACHE[key]
    polys = [
        poly for pad in fp.pads
        if side in pad_copper_sides(fp, pad, names)
        and (poly := _pad_polygon(fp, pad, margin_mm=margin_mm)) is not None
        and not poly.is_empty
    ]
    res = unary_union(polys) if polys else None
    if len(_PAD_UNION_CACHE) > 8192:
        _PAD_UNION_CACHE.clear()
    _PAD_UNION_CACHE[key] = res
    return res


def courtyard_polygon(fp: Footprint) -> Polygon | None:
    first_pt = fp.courtyard.polygons[0][0] if fp.courtyard.polygons and fp.courtyard.polygons[0] else None
    pose = (fp.ref, fp.lib_id, fp.x, fp.y, fp.rotation, len(fp.courtyard.polygons), first_pt)
    if pose in _CY_POLY_CACHE:
        return _CY_POLY_CACHE[pose]
    polys = [Polygon(p) for p in fp.courtyard_world() if len(p) >= 3]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    polys = [p for p in polys if not p.is_empty]
    if not polys:
        res = None
    else:
        shape = unary_union(polys)
        res = shape if shape.geom_type == "Polygon" else shape.convex_hull
    if len(_CY_POLY_CACHE) > 8192:
        _CY_POLY_CACHE.clear()
    _CY_POLY_CACHE[pose] = res
    return res


def board_polygon(board: Board) -> Polygon | None:
    first_pt = board.outline[0].outer[0] if board.outline and board.outline[0].outer else None
    key = (board.name, len(board.outline), board.board_area_mm2, first_pt,
           sum(len(o.outer) + len(o.holes) for o in board.outline))
    if key in _BOARD_POLY_CACHE:
        return _BOARD_POLY_CACHE[key]
    parts = [Polygon(o.outer, o.holes) for o in board.outline if len(o.outer) >= 3]
    parts = [p if p.is_valid else p.buffer(0) for p in parts]
    res = unary_union(parts) if parts else None
    if len(_BOARD_POLY_CACHE) > 256:
        _BOARD_POLY_CACHE.clear()
    _BOARD_POLY_CACHE[key] = res
    return res


def copper_layer_map(board: Board | None) -> dict[str, str]:
    """Map board copper layer names to canonical KiCad names (F.Cu / B.Cu)."""
    names = {cl.name: cl.canonical for cl in board.copper_layers} if board is not None else {}
    names.update({
        "F.Cu": "F.Cu", "B.Cu": "B.Cu",
        "top_layer": "F.Cu", "bottom_layer": "B.Cu",
        "top_copper": "F.Cu", "bottom_copper": "B.Cu",
    })
    return names


def has_thru_hole_pad(pad) -> bool:
    drill = getattr(pad, "drill", None)
    has_drill = bool(drill and (max(drill) > 0 if isinstance(drill, (tuple, list)) else drill > 0))
    return (
        pad.kind in ("thru_hole", "np_thru_hole")
        or ("F.Cu" in pad.layers and "B.Cu" in pad.layers)
        or has_drill
    )


def _raw_pad_sides(pad, layer_map: dict[str, str]) -> set[str]:
    if has_thru_hole_pad(pad):
        return {"F", "B"}
    out: set[str] = set()
    for lyr in pad.layers:
        canon = layer_map.get(lyr, lyr)
        if canon == "F.Cu":
            out.add("F")
        elif canon == "B.Cu":
            out.add("B")
    return out


def pad_copper_sides(fp: Footprint, pad, layer_map: dict[str, str] | None = None) -> set[str]:
    """Copper sides ('F', 'B') touched by *pad* on *fp*."""
    lmap = layer_map if layer_map is not None else copper_layer_map(None)
    raw = _raw_pad_sides(pad, lmap)
    if not raw:
        return {fp.side}
    if fp.side == "B" and raw == {"F"} and not any("B" in _raw_pad_sides(p, lmap) for p in fp.pads):
        return {"B"}
    return raw


def has_opposite_side_copper(fp: Footprint, pad, layer_map: dict[str, str] | None = None) -> bool:
    """True when *pad* exposes copper on the side opposite to *fp.side*."""
    opp = "B" if fp.side == "F" else "F"
    return has_thru_hole_pad(pad) or opp in pad_copper_sides(fp, pad, layer_map)


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


def thru_hole_pad_polygon(fp: Footprint, margin_mm: float = 0.0, *, layer_map: dict[str, str] | None = None):
    polys = []
    for pad in fp.pads:
        if not has_opposite_side_copper(fp, pad, layer_map):
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
    layer_map: dict[str, str] | None = None,
) -> list[list[tuple[float, float]]]:
    """Merged rectangular hole rings around pads exposing opposite-side (or all) copper."""
    polys = [
        poly for pad in fp.pads
        if (not thru_hole_only or has_opposite_side_copper(fp, pad, layer_map))
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
    obs_sig = tuple((o.get("uuid"), o.get("kind"), o.get("copper"), o.get("layer"), o.get("net"),
                     len(o.get("outlines", ()))) for o in board.obstacles)
    cache_key = (
        board.name,
        obs_sig,
        tuple((r, board.fp(r).x, board.fp(r).y, board.fp(r).rotation, board.fp(r).side)
              for r in sorted(fixed_set) if r in existing),
    )
    if cache_key in _COPPER_OBS_CACHE:
        return _COPPER_OBS_CACHE[cache_key]
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
    if len(_COPPER_OBS_CACHE) > 256:
        _COPPER_OBS_CACHE.clear()
    _COPPER_OBS_CACHE[cache_key] = out
    return out


def board_mask_obstacles(board: Board, fixed: set[str] | None = None) -> list[tuple[str, str, Polygon]]:
    """Free-standing solder-mask apertures and unconnected/NPTH pad keepouts (board or fixed footprints)."""
    fixed_set = board.fixed_refs() if fixed is None else set(fixed)
    obs_sig = tuple(
        (o.get("uuid"), o.get("kind"), o.get("source"), o.get("layer"), len(o.get("outlines", ())))
        for o in board.obstacles if o.get("kind") in ("mask_aperture", "pad_keepout")
    )
    if not obs_sig:
        return []
    cache_key = (board.name, obs_sig, tuple(sorted(fixed_set)))
    if cache_key in _MASK_OBS_CACHE:
        return _MASK_OBS_CACHE[cache_key]
    names = copper_layer_map(board)
    out: list[tuple[str, str, Polygon]] = []
    for obs in board.obstacles:
        if obs.get("kind") not in ("mask_aperture", "pad_keepout") or obs.get("overlapping_pads"):
            continue
        src = str(obs.get("source") or "")
        if src != "board" and (not src.startswith("footprint:") or src.split(":", 1)[1] not in fixed_set):
            continue
        layer = str(obs.get("layer") or "")
        canon = names.get(obs.get("copper") or "")
        mask_side = (
            "F" if layer == "F.Mask" or canon == "F.Cu"
            else "B" if layer == "B.Mask" or canon == "B.Cu"
            else None
        )
        if mask_side is None:
            continue
        polys = [Polygon(ring) for ring in obs.get("outlines", []) if len(ring) >= 3]
        polys = [p if p.is_valid else p.buffer(0) for p in polys]
        polys = [p for p in polys if not p.is_empty]
        if not polys:
            continue
        poly = unary_union(polys)
        uid = str(obs.get("uuid") or "")
        for comp in _iter_polygons(poly):
            if comp.area > 1e-4:
                out.append((uid, mask_side, comp))
    if len(_MASK_OBS_CACHE) > 256:
        _MASK_OBS_CACHE.clear()
    _MASK_OBS_CACHE[cache_key] = out
    return out


def placement_obstacle_holes(board: Board, fixed: set[str], two_sided: bool) -> list[list[tuple[float, float]]]:
    """Hole rings for solver stage: free board copper graphics plus fixed TH pads on 2-sided boards."""
    holes: list[list[tuple[float, float]]] = []
    for _uid, _side, comp in board_copper_obstacles(board, fixed):
        coords = list(comp.exterior.coords)
        if len(coords) >= 4:
            holes.append([(round(x, 6), round(y, 6)) for x, y in coords[:-1]])
    if two_sided:
        lmap = copper_layer_map(board)
        existing = {fp.ref for fp in board.footprints}
        for ref in sorted(fixed):
            if ref in existing:
                holes.extend(thru_hole_hole_rings(board.fp(ref), margin_mm=0.08, layer_map=lmap))
    return holes


def check(board: Board, *, clearance_mm: float | None = None, fixed: set[str] | None = None) -> list[Finding]:
    """fixed: parts the placer did not move (S1); defaults to the locked footprints."""
    clearance = board.rules.get("courtyard_clearance", 0.0) if clearance_mm is None else clearance_mm
    fixed = board.fixed_refs() if fixed is None else set(fixed)
    shapes = {fp.ref: courtyard_polygon(fp) for fp in board.footprints}
    shape_bounds = {r: s.bounds for r, s in shapes.items() if s is not None}
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
    edge_clear = float(board.rules.get("copper_edge_clearance") or 0.0)
    if outline is not None:
        inner_key = (id(outline), edge_clear)
        if inner_key in _INNER_OUTLINE_CACHE:
            outline_prep, inner_outline, inner_prep = _INNER_OUTLINE_CACHE[inner_key]
        else:
            outline_prep = prep(outline)
            inner_outline = outline.buffer(-edge_clear + 1e-4) if edge_clear > 0 else None
            inner_prep = prep(inner_outline) if inner_outline is not None and not inner_outline.is_empty else None
            if len(_INNER_OUTLINE_CACHE) > 256:
                _INNER_OUTLINE_CACHE.clear()
            _INNER_OUTLINE_CACHE[inner_key] = (outline_prep, inner_outline, inner_prep)
        for fp in board.footprints:
            shape = shapes[fp.ref]
            if shape is not None and not outline_prep.covers(shape):
                outside = shape.difference(outline).area
                if outside > OUTSIDE_TOL_MM2:
                    findings.append(Finding("outside_outline", [fp.ref], round(outside, 4), fp.side,
                                            detail=f"{outside / shape.area:.0%} of the courtyard", fixed=fp.ref in fixed))
                    continue
            if inner_outline is not None and fp.ref not in fixed and (
                shape is None or inner_prep is None or not inner_prep.covers(shape)
            ):
                for pad in fp.pads:
                    p_poly = _pad_polygon(fp, pad, margin_mm=0.0)
                    if (
                        p_poly is not None
                        and (inner_prep is None or not inner_prep.covers(p_poly))
                        and (out_area := p_poly.difference(inner_outline).area) > 1e-4
                    ):
                        findings.append(Finding(
                            "outside_outline", [fp.ref], round(max(out_area, 0.0101), 4), fp.side,
                            detail=f"copper_edge_clearance:{pad.number}", fixed=False,
                        ))
                        break

    names = copper_layer_map(board)
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
        nc_clear = {nc.name: float(nc.clearance or 0.0) for nc in board.netclasses}
        default_nc_clear = nc_clear.get("Default", 0.0)
        net_clear = {n.name: nc_clear.get(n.netclass, default_nc_clear) for n in board.nets}
        base_clear = float(board.rules.get("min_clearance") or 0.0)
        max_board_clear = max([0.15, base_clear, default_nc_clear, *nc_clear.values()])

        pad_unions: dict[str, object] = {}
        raw_pad_unions: dict[str, object] = {}
        raw_pad_bounds: dict[str, tuple[float, float, float, float] | None] = {}
        side_pad_polys: dict[str, list[tuple[object, tuple[float, float, float, float], float]]] = {}
        th_pairs: dict[tuple[str, str], tuple[float, str, str]] = {}
        check_pth_cy = board.drc_severities.get("pth_inside_courtyard") != "ignore"
        rules_sig = (board.name, base_clear, default_nc_clear, tuple(sorted(nc_clear.items())), check_pth_cy)
        for fp1 in board.footprints:
            pose1 = (fp1.ref, fp1.lib_id, fp1.x, fp1.y, fp1.rotation, fp1.side, len(fp1.pads), rules_sig)
            if pose1 in _TH_INFO_CACHE:
                th_poly, th_raw, opp_pads1, drill_union1, dbx1, tbx = _TH_INFO_CACHE[pose1]
            else:
                th_poly = thru_hole_pad_polygon(fp1, margin_mm=0.05, layer_map=names)
                th_raw = thru_hole_pad_polygon(fp1, margin_mm=0.0, layer_map=names)
                opp_pads1 = []
                for p1 in fp1.pads:
                    if has_opposite_side_copper(fp1, p1, names) and (poly1 := _pad_polygon(fp1, p1, margin_mm=0.0)) is not None:
                        req1 = max(0.15, base_clear, net_clear.get(p1.net, default_nc_clear) if p1.net else default_nc_clear)
                        opp_pads1.append((pad_copper_sides(fp1, p1, names), poly1, poly1.bounds, req1))
                drill_polys1 = []
                if check_pth_cy:
                    for p1 in fp1.pads:
                        if p1.kind == "thru_hole" and p1.drill:
                            d_val = min(p1.drill) if isinstance(p1.drill, (tuple, list)) else float(p1.drill)
                            if d_val > 0:
                                px, py = fp1.pad_world(p1)
                                drill_polys1.append(Point(px, py).buffer(d_val / 2.0))
                drill_union1 = unary_union(drill_polys1) if drill_polys1 else None
                dbx1 = drill_union1.bounds if drill_union1 is not None and not drill_union1.is_empty else None
                tbx = th_raw.bounds if th_raw is not None else None
                if len(_TH_INFO_CACHE) > 8192:
                    _TH_INFO_CACHE.clear()
                _TH_INFO_CACHE[pose1] = (th_poly, th_raw, opp_pads1, drill_union1, dbx1, tbx)
            if (th_poly is None or th_raw is None) and drill_union1 is None:
                continue
            for fp2 in board.footprints:
                if fp1.ref == fp2.ref or fp1.side == fp2.side or (fp1.ref in fixed and fp2.ref in fixed):
                    continue
                if check_pth_cy and drill_union1 is not None and dbx1 is not None and (cy2 := shapes[fp2.ref]) is not None:
                    cbx2 = shape_bounds[fp2.ref]
                    if not (cbx2[0] > dbx1[2] or cbx2[2] < dbx1[0] or cbx2[1] > dbx1[3] or cbx2[3] < dbx1[1]):
                        d_area = drill_union1.intersection(cy2).area
                        if d_area > 1e-4:
                            pair = (fp1.ref, fp2.ref) if fp1.ref < fp2.ref else (fp2.ref, fp1.ref)
                            hit_area = max(d_area, 0.0101)
                            prev = th_pairs.get(pair)
                            if prev is None or hit_area > prev[0]:
                                th_pairs[pair] = (hit_area, fp2.side, fp1.ref)
                if th_poly is None or th_raw is None or tbx is None:
                    continue
                if fp2.ref not in pad_unions:
                    pose2 = (fp2.ref, fp2.lib_id, fp2.x, fp2.y, fp2.rotation, fp2.side, len(fp2.pads), rules_sig)
                    if pose2 in _SIDE_PAD_CACHE:
                        pad_unions[fp2.ref], raw_pad_unions[fp2.ref], raw_pad_bounds[fp2.ref], side_pad_polys[fp2.ref] = _SIDE_PAD_CACHE[pose2]
                    else:
                        pads2_buf = []
                        pads2_raw = []
                        pads2_info = []
                        for pad in fp2.pads:
                            if fp2.side not in pad_copper_sides(fp2, pad, names):
                                continue
                            if (pb := _pad_polygon(fp2, pad, margin_mm=0.10)) is not None:
                                pads2_buf.append(pb)
                            if (pr := _pad_polygon(fp2, pad, margin_mm=0.0)) is not None:
                                pads2_raw.append(pr)
                                req2 = max(
                                    0.15, base_clear,
                                    net_clear.get(pad.net, default_nc_clear) if pad.net else default_nc_clear,
                                )
                                pads2_info.append((pr, pr.bounds, req2))
                        u_buf = unary_union(pads2_buf) if pads2_buf else None
                        u_raw = unary_union(pads2_raw) if pads2_raw else None
                        u_bounds = u_raw.bounds if u_raw is not None and not u_raw.is_empty else None
                        if len(_SIDE_PAD_CACHE) > 8192:
                            _SIDE_PAD_CACHE.clear()
                        _SIDE_PAD_CACHE[pose2] = (u_buf, u_raw, u_bounds, pads2_info)
                        pad_unions[fp2.ref], raw_pad_unions[fp2.ref], raw_pad_bounds[fp2.ref], side_pad_polys[fp2.ref] = u_buf, u_raw, u_bounds, pads2_info
                u2 = pad_unions[fp2.ref]
                u2_raw = raw_pad_unions[fp2.ref]
                ubx = raw_pad_bounds[fp2.ref]
                if u2 is None or ubx is None or u2_raw is None:
                    continue
                if (
                    ubx[0] > tbx[2] + max_board_clear
                    or ubx[2] < tbx[0] - max_board_clear
                    or ubx[1] > tbx[3] + max_board_clear
                    or ubx[3] < tbx[1] - max_board_clear
                ):
                    continue
                area = u2.intersection(th_poly).area
                if area <= OVERLAP_TOL_MM2 and th_raw.distance(u2_raw) < max_board_clear - 1e-4:
                    for opp_sides1, poly1, b1, req1 in opp_pads1:
                        if fp2.side not in opp_sides1:
                            continue
                        for poly2, b2, req2 in side_pad_polys[fp2.ref]:
                            req = max(req1, req2)
                            if b2[0] > b1[2] + req or b2[2] < b1[0] - req or b2[1] > b1[3] + req or b2[3] < b1[1] - req:
                                continue
                            d = poly1.distance(poly2)
                            if d < req - 1e-3:
                                area = max(area, round(max(0.0101, (req - d) * 0.5), 4))
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
            if fp.side != copper_side and not any(has_opposite_side_copper(fp, pad, names) for pad in fp.pads):
                continue
            sbx = shape_bounds[fp.ref]
            if sbx[0] > obx[2] or sbx[2] < obx[0] or sbx[1] > obx[3] or sbx[3] < obx[1]:
                continue
            area = shape.intersection(obs_poly).area
            if area > OVERLAP_TOL_MM2:
                findings.append(Finding(
                    "keepout", [fp.ref], round(area, 4), fp.side,
                    detail=f"copper_graphic:{uid}", fixed=False,
                ))

    mask_obs = board_mask_obstacles(board, fixed)
    if mask_obs:
        raw_side_union: dict[tuple[str, str], Polygon | None] = {}
        for uid, mask_side, obs_poly in mask_obs:
            obx = obs_poly.bounds
            for fp in board.footprints:
                if fp.ref in fixed:
                    continue
                sbx = shape_bounds.get(fp.ref)
                if sbx is not None and (sbx[0] > obx[2] + 2.0 or sbx[2] < obx[0] - 2.0
                                        or sbx[1] > obx[3] + 2.0 or sbx[3] < obx[1] - 2.0):
                    continue
                ck = (fp.ref, mask_side)
                if ck not in raw_side_union:
                    raw_side_union[ck] = _pad_union(fp, mask_side, names, margin_mm=0.0)
                u_poly = raw_side_union[ck]
                if u_poly is None:
                    continue
                ubx = u_poly.bounds
                if ubx[0] > obx[2] or ubx[2] < obx[0] or ubx[1] > obx[3] or ubx[3] < obx[1]:
                    continue
                hit = u_poly.intersection(obs_poly).area
                if hit > 1e-4:
                    findings.append(Finding(
                        "keepout", [fp.ref], round(max(hit, 0.0101), 4), mask_side,
                        detail=f"mask_aperture:{uid}", fixed=False,
                    ))

    # Same-side pad-to-pad overlap guard (catches pads protruding outside a footprint's file courtyard).
    existing_cy: dict[tuple[str, str, str | None], Finding] = {
        (f.refs[0], f.refs[1], f.side): f
        for f in findings if f.kind == "courtyard_overlap" and len(f.refs) == 2
    }
    for side in ("F", "B"):
        pad_entries: list[tuple[str, Polygon]] = []
        for fp in board.footprints:
            merged = _pad_union(fp, side, names)
            if merged is not None:
                pad_entries.append((fp.ref, merged))
        if len(pad_entries) < 2:
            continue
        geoms = [g for _r, g in pad_entries]
        tree = STRtree(geoms)
        seen_pairs: set[tuple[str, str]] = set()
        for i, j in zip(*tree.query(geoms, predicate="intersects")):
            if i >= j:
                continue
            r1, r2 = pad_entries[i][0], pad_entries[j][0]
            if r1 in fixed and r2 in fixed:
                continue
            pair = (r1, r2) if r1 < r2 else (r2, r1)
            if pair in seen_pairs:
                continue
            area = geoms[i].intersection(geoms[j]).area
            if area > 1e-4:
                seen_pairs.add(pair)
                prev = existing_cy.get((pair[0], pair[1], side))
                if prev is not None:
                    continue
                findings.append(Finding(
                    "courtyard_overlap", [pair[0], pair[1]], round(max(area, 0.0101), 4), side,
                    detail="pad_overlap", fixed=False, waived_by=None,
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
