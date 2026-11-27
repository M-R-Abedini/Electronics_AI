# SPDX-License-Identifier: MIT
"""First-principles galvanic isolation domain decomposition for multi-domain PCBs.

When a board contains full-layer isolation moat rule areas (``keepout.pads`` or
``keepout.footprints``) that partition the board outline into two or more
disjoint physical regions (e.g. Primary / Secondary, or Primary / OUT / IN),
each movable component is mapped deterministically to its unique physical region
by propagating region ownership from fixed anchor pads (connectors, isolation
bridges) across the netlist graph.

Solving each disjoint region with its exact boundary polygon prevents movable
components from ever crossing an isolation barrier during S3 analytical
placement, S4 legalization, or S5 annealing.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from ..eval import legality
from ..ir import Board, Footprint, Outline

MIN_REGION_AREA_MM2 = 40.0


def _iter_polys(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_iter_polys(g))
        return out
    return []


def decompose_isolated_domains(
    board: Board,
    fixed: set[str],
) -> list[tuple[Polygon, set[str]]] | None:
    """Partition ``board`` into disjoint moat-separated regions and their movable refs.

    Returns ``None`` if the board does not split into >= 2 substantial regions
    or if any movable component cannot be uniquely assigned to one region.
    """
    bpoly = legality.board_polygon(board)
    if bpoly is None or bpoly.is_empty:
        return None
    lmap = legality.copper_layer_map(board)
    moat_polys: list[Polygon] = []
    for ra in board.rule_areas:
        ko = ra.get("keepout") or {}
        if not (ko.get("pads") or ko.get("footprints")):
            continue
        layers = {lmap.get(lname, lname) for lname in (ra.get("layers") or ())}
        if "F.Cu" not in layers:
            continue
        for ring in ra.get("polygons") or ():
            if len(ring) >= 3:
                p = Polygon(ring)
                p = p if p.is_valid else p.buffer(0)
                if not p.is_empty and p.area > 1.0:
                    moat_polys.append(p)
    if not moat_polys:
        return None

    rem = bpoly.difference(unary_union(moat_polys))
    regions = [p for p in _iter_polys(rem) if p.area >= MIN_REGION_AREA_MM2]
    if len(regions) < 2:
        return None
    regions.sort(key=lambda p: (round(p.bounds[0], 2), round(p.bounds[1], 2)))
    buffered = [r.buffer(0.6) for r in regions]

    # Seed net -> region(s) from fixed footprints' pads (connectors, isolation bridges)
    net_regions: dict[str, set[int]] = defaultdict(set)
    for fp in board.footprints:
        if fp.ref not in fixed:
            continue
        for pad, (wx, wy) in fp.pads_world():
            if not pad.net:
                continue
            pt = Point(wx, wy)
            hits = [k for k, rb in enumerate(buffered) if rb.contains(pt)]
            if len(hits) == 1:
                net_regions[pad.net].add(hits[0])

    # Propagate unique region ownership across movable footprints via BFS
    movable = [fp for fp in board.footprints if fp.ref not in fixed]
    if not movable:
        return None
    part_region: dict[str, int] = {}
    changed = True
    while changed:
        changed = False
        for fp in movable:
            if fp.ref in part_region:
                continue
            cand: set[int] = set()
            for net in fp.nets:
                regs = net_regions.get(net)
                if regs and len(regs) == 1:
                    cand.update(regs)
            if len(cand) == 1:
                reg_k = next(iter(cand))
                part_region[fp.ref] = reg_k
                changed = True
                for net in fp.nets:
                    net_regions[net].add(reg_k)

    # Fallback: any movable part still unassigned (e.g. unrouted/mechanical) uses its current region
    for fp in movable:
        if fp.ref in part_region:
            continue
        pt = Point(fp.x, fp.y)
        hits = [k for k, rb in enumerate(buffered) if rb.contains(pt)]
        if len(hits) == 1:
            part_region[fp.ref] = hits[0]
        else:
            return None

    grouped: list[set[str]] = [set() for _ in regions]
    for ref, k in part_region.items():
        grouped[k].add(ref)
    if sum(1 for g in grouped if g) < 2:
        return None
    return [(regions[k], grouped[k]) for k in range(len(regions)) if grouped[k]]


def make_domain_subboard(
    board: Board,
    region: Polygon,
    fixed: set[str],
    domain_movable: set[str],
    intent: dict | None = None,
) -> tuple[Board, dict | None]:
    """Create a sub-board restricted to ``region`` containing fixed anchors + ``domain_movable``."""
    keep_refs = fixed | domain_movable
    sub = board.copy()
    sub.footprints = [fp for fp in sub.footprints if fp.ref in keep_refs]
    sub.reindex()
    outer_ring = [(round(float(x), 4), round(float(y), 4)) for x, y in list(region.exterior.coords)[:-1]]
    hole_rings = [
        [(round(float(x), 4), round(float(y), 4)) for x, y in list(h.coords)[:-1]]
        for h in region.interiors
    ]
    sub.outline = [Outline(outer=outer_ring, holes=hole_rings)]
    sub.board_area_mm2 = float(region.area)
    # Keep only rule areas that intersect this region (skip the moat polygons already carved into sub.outline)
    sub.rule_areas = [
        ra for ra in sub.rule_areas
        if not ((ra.get("keepout") or {}).get("pads") or (ra.get("keepout") or {}).get("footprints"))
    ]
    if intent is None:
        return sub, None
    sub_intent = dict(intent)
    sub_intent["parts"] = {r: v for r, v in (intent.get("parts") or {}).items() if r in keep_refs}
    sub_blocks = []
    for blk in intent.get("blocks") or ():
        val = (blk.get("value") or {}) if isinstance(blk, dict) else {}
        anchor = val.get("anchor")
        members = [m for m in (val.get("members") or ()) if m in keep_refs]
        if anchor in keep_refs and len(members) >= 2:
            nb = dict(blk)
            nv = dict(val)
            nv["members"] = members
            nb["value"] = nv
            sub_blocks.append(nb)
    sub_intent["blocks"] = sub_blocks
    return sub, sub_intent


def ref_regions(board: Board, fixed: set[str]) -> dict[str, Polygon] | None:
    """Return ``{ref: region_polygon}`` for movable parts on a moat-split board, else ``None``."""
    doms = decompose_isolated_domains(board, fixed)
    if not doms:
        return None
    out: dict[str, Polygon] = {}
    for reg, refs in doms:
        for ref in refs:
            out[ref] = reg
    return out


def footprints_in_domains(fps: Iterable[Footprint], regions: dict[str, Polygon] | None) -> bool:
    """Return True if every footprint (and all of its pads) remains inside its assigned domain."""
    if not regions:
        return True
    for fp in fps:
        reg = regions.get(fp.ref)
        if reg is None:
            continue
        if not reg.covers(Point(fp.x, fp.y)):
            return False
        for _pad, (wx, wy) in fp.pads_world():
            if not reg.covers(Point(wx, wy)):
                return False
    return True


def make_clearance_guard(board: Board) -> Callable[[Board, Iterable[Footprint]], bool] | None:
    """Return a fast pad-to-pad HV/isolation clearance predicate from ``.kicad_dru`` + netclasses."""
    from ..route import dru as dru_mod

    src_path = (board.source or {}).get("path")
    dru_path = Path(src_path).with_suffix(".kicad_dru") if src_path else None
    nc_clearances = dru_mod.extract_netclass_clearances(dru_path, board)
    if not nc_clearances:
        return None
    net_to_nc = {n.name: n.netclass for n in board.nets}
    guarded_ncs = {nc for pair in nc_clearances for nc in pair}

    def _pad_radius(pad) -> float:
        return 0.5 * max(float(pad.size[0]), float(pad.size[1]))

    def _ok(board_state: Board, moved_fps: Iterable[Footprint]) -> bool:
        moved_list = list(moved_fps)
        for fp in moved_list:
            fp_pads = [
                (pad, pt, net_to_nc.get(pad.net, ""), _pad_radius(pad))
                for pad, pt in fp.pads_world()
                if pad.net and net_to_nc.get(pad.net, "") in guarded_ncs
            ]
            if not fp_pads:
                continue
            for other in board_state.footprints:
                if other is fp:
                    continue
                if fp.side != other.side and not fp.is_through_hole and not other.is_through_hole:
                    continue
                for pad_a, (ax, ay), nca, ra in fp_pads:
                    for pad_b, (bx, by) in other.pads_world():
                        if not pad_b.net or pad_b.net == pad_a.net:
                            continue
                        ncb = net_to_nc.get(pad_b.net, "")
                        if not ncb:
                            continue
                        key = (nca, ncb) if nca <= ncb else (ncb, nca)
                        req = nc_clearances.get(key, 0.0)
                        if req <= 0.0:
                            continue
                        rb = _pad_radius(pad_b)
                        if math.hypot(ax - bx, ay - by) - (ra + rb) < req - 1e-4:
                            return False
        return True

    return _ok
