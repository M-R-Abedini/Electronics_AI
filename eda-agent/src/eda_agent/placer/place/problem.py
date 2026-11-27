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
from dataclasses import dataclass, field, replace

from ..eval.legality import (
    board_mask_obstacles,
    board_polygon,
    copper_layer_map,
    courtyard_polygon,
    has_opposite_side_copper,
    is_shield_cavity_eligible,
    placement_obstacle_holes,
)
from ..eval.metrics import PASSIVE_PREFIXES, zone_nets
from ..geometry import box_union, rotated_box
from ..intent import model
from ..ir import Board, Footprint, is_ground_net
from . import escape

WEIGHT_SIGNAL = 1.0
WEIGHT_PLANE_NET = 0.2
DECAP_AFFINITY_WEIGHT = 3.0  # local supply distance in mm, weighted relative to signal HPWL
CANDIDATE_DECAP_AFFINITY_WEIGHT = 1.0  # softer pull toward nearest valid candidate IC pad when target ref is unresolved
GAP_MM = 0.02  # declared: minimum courtyard gap (>= 20 um prevents touching-courtyard DRC)
LOCAL_BLOCK_WEIGHT = 2.0
SHEET_COHESION_WEIGHT = 0.35  # gentle sub-sheet macro cohesion on multi-sheet hierarchical designs (floorplan=True)
LOCAL_BLOCK_ROLES = frozenset({"crystal", "crystal_load_cap", "crystal_aux", "converter_inductor", "converter_diode",
                               "converter_control", "regulator_input_cap", "regulator_output_cap", "esd", "led_resistor"})
_NON_OVERHANGING_PREFIXES = frozenset({"TP", "C", "CP", "R", "RN", "L", "FB"})


def _is_real_net(net: str | None) -> bool:
    return bool(net) and not net.startswith("unconnected-(")


def local_sub_boxes(fp: Footprint) -> list[tuple[float, float, float, float]]:
    """Decompose hollow/multi-island courtyards into disjoint local wall/island boxes.

    Returns ``[]`` for single convex/solid courtyards so the Rust core keeps its single
    ``Rect`` fast path. Hollow perimeter shields (one outer polygon with one interior
    hole) produce 4 wall strips plus any protruding pad boxes so eligible interior parts fit inside.
    """
    if len(fp.courtyard.polygons) == 1 and fp.courtyard.holes and len(fp.courtyard.holes[0]) == 1:
        outer = fp.courtyard.polygons[0]
        hole = fp.courtyard.holes[0][0]
        if len(outer) >= 3 and len(hole) >= 3:
            ox0, oy0 = min(p[0] for p in outer), min(p[1] for p in outer)
            ox1, oy1 = max(p[0] for p in outer), max(p[1] for p in outer)
            hx0, hy0 = min(p[0] for p in hole), min(p[1] for p in hole)
            hx1, hy1 = max(p[0] for p in hole), max(p[1] for p in hole)
            strips: list[tuple[float, float, float, float]] = []
            if hy0 > oy0 + 1e-4:
                strips.append((ox0, oy0, ox1, hy0))
            if oy1 > hy1 + 1e-4:
                strips.append((ox0, hy1, ox1, oy1))
            y_mid0, y_mid1 = max(oy0, hy0), min(oy1, hy1)
            if y_mid1 > y_mid0 + 1e-4:
                if hx0 > ox0 + 1e-4:
                    strips.append((ox0, y_mid0, hx0, y_mid1))
                if ox1 > hx1 + 1e-4:
                    strips.append((hx1, y_mid0, ox1, y_mid1))
            for pad in fp.pads:
                w, h = pad.size[0] + 0.08, pad.size[1] + 0.08
                rad = math.radians(pad.rotation)
                c, s = math.cos(rad), math.sin(rad)
                corners = [
                    (pad.local[0] + dx * c - dy * s, pad.local[1] + dx * s + dy * c)
                    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))
                ]
                px0, py0 = min(p[0] for p in corners), min(p[1] for p in corners)
                px1, py1 = max(p[0] for p in corners), max(p[1] for p in corners)
                inside_wall = any(
                    px0 >= sx0 - 1e-4 and px1 <= sx1 + 1e-4 and py0 >= sy0 - 1e-4 and py1 <= sy1 + 1e-4
                    for sx0, sy0, sx1, sy1 in strips
                )
                if not inside_wall:
                    strips.append((px0, py0, px1, py1))
            if strips:
                return strips
    if len(fp.courtyard.polygons) > 1:
        islands = [
            (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
            for poly in fp.courtyard.polygons if poly
        ]
        if len(islands) > 1:
            return islands
    return []


def local_box(fp: Footprint) -> tuple[float, float, float, float]:
    pts = [p for poly in fp.courtyard.polygons for p in poly]
    # Always enclose pads (even when a connector's shield/tab pad protrudes beyond its file courtyard).
    for pad in fp.pads:
        w, h = pad.size[0] + 0.08, pad.size[1] + 0.08
        rad = math.radians(pad.rotation)
        c, s = math.cos(rad), math.sin(rad)
        for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            pts.append((pad.local[0] + dx * c - dy * s, pad.local[1] + dx * s + dy * c))
    if not pts:
        return (-0.1, -0.1, 0.1, 0.1)
    base = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    # Legacy/derived courtyards without explicit F.CrtYd/B.CrtYd wrap pads only, omitting bulky
    # physical bodies (radial electrolytics, shielded inductors, power diodes, shrouded headers).
    if fp.courtyard.source == "derived" and len(fp.courtyard.polygons) == 1 and not fp.courtyard.holes and fp.body_bounds_mm:
        bb = _local_body_box(fp, base)
        la = max(0.1, (base[2] - base[0]) * (base[3] - base[1]))
        ba = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if ba >= 15.0 and 1.15 * la <= ba <= 12.0 * la:
            return bb
    return base


def fixed_parts(board: Board, intent: dict | None = None) -> dict[str, str]:
    """S1: reference -> reason for every part the placer must not move."""
    from .mechanical import interface_inventory

    if board.placement_anchors is not None:
        return {fp.ref: board.placement_anchors.get(fp.ref, "locked in the file") for fp in board.footprints
                if fp.locked or fp.ref in board.placement_anchors}
    out: dict[str, str] = {}
    outline = board_polygon(board)
    interfaces = {row["ref"]: row for row in interface_inventory(board, intent, outline=outline) if row["protected"]}
    for fp in board.footprints:
        if fp.locked:
            out[fp.ref] = "locked in the file"
            continue
        if not any(_is_real_net(p.net) for p in fp.pads):
            out[fp.ref] = "mechanical (no nets)"
            continue
        if fp.prefix == "FID" or "fiducial" in fp.lib_id.lower():
            out[fp.ref] = "mechanical (fiducial)"
            continue
        if (
            fp.prefix in ("H", "MH", "HOLE") or "mountinghole" in fp.lib_id.lower()
        ) and all((not _is_real_net(p.net)) or is_ground_net(p.net) for p in fp.pads):
            out[fp.ref] = "mechanical (grounded mounting hole)"
            continue
        if fp.ref in interfaces:
            out[fp.ref] = interfaces[fp.ref]["reason"]
        shape = courtyard_polygon(fp)
        if shape is None or outline is None:
            continue
        if shape.difference(outline).area > 0.01 and fp.prefix not in _NON_OVERHANGING_PREFIXES:
            out.setdefault(fp.ref, "overhangs the board outline")
        elif fp.courtyard_area >= 500.0 and (
            any(p.kind in ("thru_hole", "np_thru_hole") for p in fp.pads)
            or bool(fp.courtyard.holes)
        ):
            # Oversized through-hole assemblies (e.g. elevated toroid/transformer) or hollow RF shield frames
            # that intentionally enclose/straddle multiple footprints in the authored board are mechanical anchors.
            if fp.courtyard.holes:
                out.setdefault(fp.ref, "mechanical (hollow RF shield frame)")
            else:
                overlaps = sum(
                    1 for other in board.footprints
                    if other.ref != fp.ref and other.side == fp.side
                    and (osh := courtyard_polygon(other)) is not None
                    and shape.intersection(osh).area > 0.01
                )
                if overlaps >= 5:
                    out.setdefault(fp.ref, "mechanical (oversized elevated through-hole assembly)")
    return dict(sorted(out.items()))


def assign_sides(board: Board, policy: str, fixed: dict[str, str], intent: dict | None = None) -> dict[str, str]:
    """S2b: side of every part.

    ``expert`` keeps input sides (P2); ``top_only`` is assembly level 1;
    ``level2_passives_back`` puts low-profile SMD decaps/passives onto B when top utilization is high;
    ``level3_two_sided`` balances movable SMD parts across F and B.
    """
    if policy == "expert":
        return {fp.ref: fp.side for fp in board.footprints}
    if policy == "top_only":
        return {fp.ref: (fp.side if fp.ref in fixed else "F") for fp in board.footprints}
    if policy in ("level2_passives_back", "level3_two_sided"):
        parts_meta = (intent or {}).get("parts", {})
        area = board.board_area_mm2 or 0.0
        top_used = sum(fp.courtyard_area for fp in board.footprints if fp.ref in fixed and fp.side == "F")
        sides: dict[str, str] = {}
        for fp in board.footprints:
            if fp.ref in fixed:
                sides[fp.ref] = fp.side
                continue
            role = ((parts_meta.get(fp.ref, {}).get("role") or {}).get("value")) or ""
            is_low_smd = is_shield_cavity_eligible(fp) and not fp.is_through_hole
            if policy == "level2_passives_back":
                if is_low_smd and role in ("decap", "pull_up", "pull_down", "series_resistor", "signal_passive", "test_point") and (
                    area > 0 and top_used > 0.50 * area
                ):
                    sides[fp.ref] = "B"
                else:
                    sides[fp.ref] = "F"
                    top_used += fp.courtyard_area
            else:
                if is_low_smd and area > 0 and top_used > 0.55 * area:
                    sides[fp.ref] = "B"
                else:
                    sides[fp.ref] = "F"
                    top_used += fp.courtyard_area
        return sides
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
    affinities: list[tuple[list[int], float]] = field(default_factory=list)
    outers: list[list[tuple[float, float]]] = field(default_factory=list)
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    exits: dict[str, list[escape.Escape]] = field(default_factory=dict)
    sub_rects: list[list[tuple[float, float, float, float]]] = field(default_factory=list)
    cavity_eligible: list[bool] = field(default_factory=list)
    outline_locals: list[tuple[float, float, float, float]] = field(default_factory=list)
    obstacle_locals: list[tuple[float, float, float, float]] = field(default_factory=list)
    opp_locals: list[tuple[float, float, float, float] | None] = field(default_factory=list)
    side_obstacles: list[tuple[float, float, float, float, int]] = field(default_factory=list)
    clusters: list[list[int]] = field(default_factory=list)
    repulsions: list[tuple[int, int, float, float]] = field(default_factory=list)
    reg_groups: list[int] = field(default_factory=list)
    w_reg: float = 0.0
    local_block_links: int = 0
    sheet_cohesion_links: int = 0

    def poses(self) -> list[tuple[float, float, int]]:
        return [(p[7], p[8], p[9]) for p in self.parts]


def _quarter(rotation: float) -> int:
    return round(rotation / 90.0) % 4


def _local_body_box(fp: Footprint, base_box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Union of ``base_box`` and the footprint's physical body bounds in local coordinates."""
    # World AABB of a non-orthogonal (e.g. 45-degree) footprint is inflated by sqrt(2); un-rotating
    # it would double-inflate by 2x linear (4x area), so only convert orthogonal footprints.
    if not fp.body_bounds_mm or abs(fp.rotation / 90.0 - round(fp.rotation / 90.0)) > 1e-3:
        return base_box
    bx0, by0, bx1, by1 = fp.body_bounds_mm
    rad = math.radians(fp.rotation)
    c, s = math.cos(rad), math.sin(rad)
    # Inverse of world = (fp.x + lx*c + ly*s, fp.y - lx*s + ly*c) (screen CCW)
    lx_vals, ly_vals = [], []
    for wx, wy in ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)):
        dx, dy = wx - fp.x, wy - fp.y
        lx_vals.append(dx * c - dy * s)
        ly_vals.append(dx * s + dy * c)
    return box_union(base_box, (min(lx_vals), min(ly_vals), max(lx_vals), max(ly_vals)))


def _opp_th_box(
    fp: Footprint,
    lmap: dict[str, str],
    margin: float,
    *,
    flip: bool = False,
) -> tuple[float, float, float, float] | None:
    """Local bounding box of pads that protrude onto the opposite copper side (TH pins, thermal vias, NPTH)."""
    pts: list[tuple[float, float]] = []
    for pad in fp.pads:
        if not has_opposite_side_copper(fp, pad, lmap):
            continue
        w, h = pad.size[0] + 2.0 * margin, pad.size[1] + 2.0 * margin
        rad = math.radians(pad.rotation)
        c, s = math.cos(rad), math.sin(rad)
        for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            px = pad.local[0] + dx * c - dy * s
            py = pad.local[1] + dx * s + dy * c
            pts.append((px, -py if flip else py))
    if not pts:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _pad_outline_box(fp: Footprint, base_box: tuple[float, float, float, float], edge_clear: float) -> tuple[float, float, float, float]:
    """Union of ``base_box`` and every copper pad expanded by ``edge_clear`` (outline containment only)."""
    if edge_clear <= 0.0 or not fp.pads:
        return base_box
    margin = edge_clear + 0.03
    x0, y0, x1, y1 = base_box
    for pad in fp.pads:
        w, h = pad.size[0] + 2.0 * margin, pad.size[1] + 2.0 * margin
        rad = math.radians(pad.rotation)
        c, s = math.cos(rad), math.sin(rad)
        for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            px = pad.local[0] + dx * c - dy * s
            py = pad.local[1] + dx * s + dy * c
            x0, y0, x1, y1 = min(x0, px), min(y0, py), max(x1, px), max(y1, py)
    return (x0, y0, x1, y1)


def side_obstacle_rects(board: Board, fixed: set[str]) -> list[tuple[float, float, float, float, int]]:
    """Per-side fixed obstacles: 0/1 = same-side silkscreen only, 2/3 = same-side AND opp-side TH pads."""
    out: list[tuple[float, float, float, float, int]] = []
    for _uid, side, poly in board_mask_obstacles(board, fixed):
        if poly.is_empty:
            continue
        bx0, by0, bx1, by1 = poly.bounds
        out.append((bx0 - 0.04, by0 - 0.04, bx1 + 0.04, by1 + 0.04, 2 if side == "F" else 3))
    # Fixed fiducials require a >= 1.016 mm copper/mask optical keepout on their own side only
    # (blocks same-side parts and opposite-side TH pads without blocking opposite-side SMD parts).
    existing = {fp.ref for fp in board.footprints}
    lmap = copper_layer_map(board)
    for ra in board.rule_areas:
        ko = ra.get("keepout") or {}
        blocks_parts = bool(
            ko.get("footprints")
            or ko.get("pads")
            or (ko.get("tracks") and ko.get("vias") and ko.get("zones"))
        )
        if not blocks_parts:
            continue
        ra_layers = {lmap.get(lname, lname) for lname in (ra.get("layers") or ())}
        side_codes = []
        if "F.Cu" in ra_layers:
            side_codes.append(2)
        if "B.Cu" in ra_layers:
            side_codes.append(3)
        if not side_codes:
            continue
        for ring in ra.get("polygons") or ():
            if len(ring) < 3:
                continue
            xs = [float(p[0]) for p in ring]
            ys = [float(p[1]) for p in ring]
            bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
            if bx1 <= bx0 or by1 <= by0:
                continue
            for sc in side_codes:
                out.append((bx0 - 0.05, by0 - 0.05, bx1 + 0.05, by1 + 0.05, sc))
    for ref in sorted(fixed):
        if ref not in existing:
            continue
        fp_fix = board.fp(ref)
        if fp_fix.prefix == "FID" or "fiducial" in fp_fix.lib_id.lower():
            side_idx = 2 if fp_fix.side == "F" else 3
            for pad in fp_fix.pads:
                wx, wy = fp_fix.pad_world(pad)
                rx = pad.size[0] / 2.0 + 1.10
                ry = pad.size[1] / 2.0 + 1.10
                out.append((wx - rx, wy - ry, wx + rx, wy + ry, side_idx))
    board_area = float(board.board_area_mm2 or 0.0)
    text_area_by_side = {"F": 0.0, "B": 0.0}
    for row in board.artwork:
        kind = row.get("kind") or ("text" if row.get("label") else "graphic")
        if kind == "text":
            bx0, by0, bx1, by1 = row["bounds_mm"]
            text_area_by_side[row["side"]] = text_area_by_side.get(row["side"], 0.0) + max(0.0, (bx1 - bx0) * (by1 - by0))
    for row in board.artwork:
        bx0, by0, bx1, by1 = row["bounds_mm"]
        if bx1 <= bx0 or by1 <= by0:
            continue
        side_idx = 0 if row["side"] == "F" else 1
        label = (row.get("label") or "").lower()
        kind = row.get("kind") or ("barcode" if ("qr" in label or "barcode" in label) else ("text" if label else "graphic"))
        if kind == "barcode":
            # KiCad 10 enforces a 1.0 mm visual separation clearance around PCB_BARCODE items.
            m = 1.05
            out.append((bx0 - m, by0 - m, bx1 + m, by1 + m, side_idx + 2))
        elif kind in ("artwork_footprint", "graphic"):
            m = 0.05
            out.append((bx0 - m, by0 - m, bx1 + m, by1 + m, side_idx))
        else:
            if board_area <= 0.0 or text_area_by_side.get(row["side"], 0.0) <= 0.30 * board_area:
                m = 0.05
                out.append((bx0 - m, by0 - m, bx1 + m, by1 + m, side_idx))
    return out


def build(board: Board, *, side_policy: str = "expert", intent: dict | None = None,
          floorplan: bool = False, reserve: bool | None = None,
          inflations: dict[str, float] | None = None,
          fixed_override: dict[str, str] | None = None) -> PlaceProblem:
    from .mechanical import access_geometry

    fixed = dict(fixed_override) if fixed_override is not None else fixed_parts(board, intent)
    sides = assign_sides(board, side_policy, fixed, intent=intent)
    planes = zone_nets(board)
    pb = PlaceProblem(board=board, refs=[fp.ref for fp in board.footprints], fixed=fixed, sides=sides)
    pin_of: dict[str, list[int]] = {}
    part_pins: dict[tuple[str, str], list[tuple[str, int]]] = {}
    # Compute per-part HV courtyard inflation so movable HV components maintain >= netclass/DRU pad clearance.
    from pathlib import Path

    from ..route import dru as dru_mod

    src_path = (board.source or {}).get("path")
    dru_path = Path(src_path).with_suffix(".kicad_dru") if src_path else None
    nc_clearances = dru_mod.extract_netclass_clearances(dru_path, board)
    net_to_nc = {n.name: n.netclass for n in board.nets}
    hv_inflations: dict[str, float] = {}
    if nc_clearances:
        for fp in board.footprints:
            if fp.ref in fixed:
                continue
            fp_ncs = {net_to_nc[p.net] for p in fp.pads if p.net and p.net in net_to_nc}
            hv_clr = max((nc_clearances.get((nc, nc), 0.0) for nc in fp_ncs), default=0.0)
            if hv_clr >= 1.2:
                hv_inflations[fp.ref] = min(1.15, max(0.5 * hv_clr, hv_clr - 0.45))
    if hv_inflations:
        merged_inf = dict(inflations or {})
        for r, v in hv_inflations.items():
            merged_inf[r] = max(merged_inf.get(r, 0.0), v)
        inflations = merged_inf

    boxes = {fp.ref: local_box(fp) for fp in board.footprints}
    mechanics = (intent or {}).get("mechanical", {})
    edge_clear = float(board.rules.get("copper_edge_clearance") or 0.0)
    two_sided = "F" in sides.values() and "B" in sides.values()
    lmap = copper_layer_map(board)
    if (floorplan if reserve is None else reserve) and intent:
        pb.exits = escape.select_reservations(escape.envelopes(board, intent, boxes))
    for i, fp in enumerate(board.footprints):
        is_fixed = fp.ref in fixed
        flip = sides[fp.ref] != fp.side
        x0, y0, x1, y1 = boxes[fp.ref]
        subs = local_sub_boxes(fp)
        access = (mechanics.get(fp.ref) or {}).get("value", {}).get("access") if is_fixed else None
        if fp.ref in pb.exits:
            x0, y0, x1, y1 = escape.reserved_box((x0, y0, x1, y1), pb.exits[fp.ref])
            subs = []
        if access:
            access_box = access_geometry(replace(fp, x=0.0, y=0.0, rotation=0.0), access).bounds
            subs = (subs or [(x0, y0, x1, y1)]) + [access_box]
            x0, y0, x1, y1 = box_union((x0, y0, x1, y1), access_box)
        if not is_fixed and inflations:
            pad_inf = float(inflations.get(fp.ref, 0.0))
            if pad_inf > 0.0:
                x0, y0, x1, y1 = x0 - pad_inf, y0 - pad_inf, x1 + pad_inf, y1 + pad_inf
                subs = [(sx0 - pad_inf, sy0 - pad_inf, sx1 + pad_inf, sy1 + pad_inf) for sx0, sy0, sx1, sy1 in subs]
        ox0, oy0, ox1, oy1 = (x0, y0, x1, y1) if is_fixed else _pad_outline_box(fp, (x0, y0, x1, y1), edge_clear)
        bx_obs0, by_obs0, bx_obs1, by_obs1 = (x0, y0, x1, y1) if is_fixed else _local_body_box(fp, (x0, y0, x1, y1))
        if flip:  # the footprint frame mirrors in y when the part changes side (KiCad left-right flip)
            y0, y1 = -y1, -y0
            oy0, oy1 = -oy1, -oy0
            by_obs0, by_obs1 = -by_obs1, -by_obs0
            subs = [(sx0, -sy1, sx1, -sy0) for sx0, sy0, sx1, sy1 in subs]
        on_grid = abs(fp.rotation / 90.0 - round(fp.rotation / 90.0)) < 1e-6
        if is_fixed and not on_grid:
            # a fixed part at an odd angle keeps its true courtyard through its world bounding box
            if access:
                subs = [rotated_box(rect, (0.0, 0.0), fp.rotation) for rect in subs]
                x0, y0, x1, y1 = rotated_box((x0, y0, x1, y1), (0.0, 0.0), fp.rotation)
            elif fp.ref in pb.exits:
                x0, y0, x1, y1 = rotated_box((x0, y0, x1, y1), (0.0, 0.0), fp.rotation)
            elif subs:
                subs = [rotated_box((sx0, sy0, sx1, sy1), (0.0, 0.0), fp.rotation) for sx0, sy0, sx1, sy1 in subs]
                shape = courtyard_polygon(fp)
                if shape is not None:
                    bx0, by0, bx1, by1 = shape.bounds
                    x0, y0, x1, y1 = bx0 - fp.x, by0 - fp.y, bx1 - fp.x, by1 - fp.y
            else:
                shape = courtyard_polygon(fp)
                if shape is not None:
                    bx0, by0, bx1, by1 = shape.bounds
                    x0, y0, x1, y1 = bx0 - fp.x, by0 - fp.y, bx1 - fp.x, by1 - fp.y
                else:
                    x0 = y0 = x1 = y1 = 0.0
            ox0, oy0, ox1, oy1 = x0, y0, x1, y1
            bx_obs0, by_obs0, bx_obs1, by_obs1 = x0, y0, x1, y1
            r = 0
        else:
            r = _quarter(fp.rotation) if on_grid else 0
        pb.parts.append((x0, y0, x1, y1, 0 if sides[fp.ref] == "F" else 1, is_fixed, not is_fixed,
                         fp.x, fp.y, r))
        pb.sub_rects.append(subs)
        pb.cavity_eligible.append(not access and is_shield_cavity_eligible(fp))
        pb.outline_locals.append((ox0, oy0, ox1, oy1))
        pb.obstacle_locals.append((bx_obs0, by_obs0, bx_obs1, by_obs1))
        opp_margin = max(0.20, float(board.rules.get("min_clearance") or 0.15) + 0.08)
        pb.opp_locals.append(
            _opp_th_box(fp, lmap, opp_margin, flip=flip) if (two_sided and not is_fixed) else None
        )
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
            part_pins.setdefault((fp.ref, pad.net), []).append((str(pad.number), len(pb.pins)))
            pb.pins.append((i, lx, ly))
    intent_nets = (intent or {}).get("nets", {})
    _PRIORITY_NET_CLASSES = frozenset({"DIFF_PAIR", "RF", "CLOCK", "SW_NODE"})
    for net, pins in sorted(pin_of.items()):
        if len(pins) < 2 or is_ground_net(net):
            continue
        plane = net in planes
        net_cls = ((intent_nets.get(net, {}).get("class") or {}).get("value")) or "GENERAL"
        w = WEIGHT_PLANE_NET if plane else (1.5 * WEIGHT_SIGNAL if (floorplan and net_cls in _PRIORITY_NET_CLASSES) else WEIGHT_SIGNAL)
        pb.nets.append((pins, w, not plane))
        pb.net_names.append(net)
    parts = (intent or {}).get("parts", {})
    ref_to_idx = {ref: idx for idx, ref in enumerate(pb.refs)}
    for ref, part in sorted(parts.items()):
        role, claim = part.get("role"), part.get("target")
        if model.is_uncertain(role) or not claim:
            continue
        kind = role["value"]
        if kind not in ("decap", "esd") and (not floorplan or kind not in LOCAL_BLOCK_ROLES):
            continue
        target = claim["value"]
        net, target_ref = target.get("net"), target.get("ref")
        if ref not in boxes:
            continue
        if not target_ref:
            candidates = [c for c in (target.get("candidates") or ()) if c != ref and c in boxes]
            has_non_connector = any(((parts.get(c, {}).get("role") or {}).get("value")) != "connector"
                                    for c in candidates)
            if (kind == "decap" and net and not is_ground_net(net) and CANDIDATE_DECAP_AFFINITY_WEIGHT > 0
                    and candidates and has_non_connector and float(claim.get("confidence", 0.0)) > 0):
                source = part_pins.get((ref, net), [])
                pins = [index for cand in candidates for _, index in part_pins.get((cand, net), [])]
                if len(source) == 1 and pins:
                    pb.affinities.append(([source[0][1], *pins], CANDIDATE_DECAP_AFFINITY_WEIGHT))
            continue
        if model.is_uncertain(claim) or target_ref == ref or target_ref not in boxes:
            continue
        numbers = set(target.get("pins") or [])
        shared = {net} if net else board.fp(ref).nets & board.fp(target_ref).nets
        if kind == "decap" and not net:
            continue
        for common_net in sorted(shared):
            if is_ground_net(common_net):
                continue
            source = part_pins.get((ref, common_net), [])
            pins = [index for number, index in part_pins.get((target_ref, common_net), [])
                    if not numbers or number in numbers]
            if len(source) == 1 and pins:
                if kind == "decap":
                    aff_w = DECAP_AFFINITY_WEIGHT
                elif kind == "esd":
                    # IEC 61000-4-2: anchor ESD protection tightly to its protected connector pin.
                    aff_w = 4.0 * LOCAL_BLOCK_WEIGHT
                else:
                    aff_w = LOCAL_BLOCK_WEIGHT
                pb.affinities.append(([source[0][1], *pins], aff_w))
                pb.local_block_links += kind != "decap"
                # Inline Connector -> ESD -> IC pull: mild directional bias so ESD sits between connector and IC
                # without ever being dragged far away from the connector pin.
                if floorplan and kind == "esd":
                    ic_pins = [
                        pi for pi in pin_of.get(common_net, [])
                        if pb.refs[pb.pins[pi][0]] not in (ref, target_ref)
                        and ((parts.get(pb.refs[pb.pins[pi][0]], {}).get("role") or {}).get("value")) in (
                            "ic", "converter_ic", "linear_regulator", "power_switch",
                        )
                    ]
                    if ic_pins:
                        pb.affinities.append(([source[0][1], *ic_pins], 0.15 * LOCAL_BLOCK_WEIGHT))
    # First-principles galvanic isolation & HV clearance repulsions from .kicad_dru + netclasses.
    part_ncs: list[set[str]] = [
        {net_to_nc[p.net] for p in fp.pads if p.net and p.net in net_to_nc}
        for fp in board.footprints
    ]

    def _pair_iso_mm(ncs_a: set[str], ncs_b: set[str]) -> float:
        best = 0.0
        for a in ncs_a:
            for b in ncs_b:
                if a == b:
                    continue
                k = (a, b) if a <= b else (b, a)
                v = nc_clearances.get(k, 0.0)
                best = max(best, v)
        return best

    # Parts spanning mutually isolated netclasses (e.g. flyback transformer, optocoupler, digital isolator)
    # are galvanic bridges and must not act as single-domain sheet anchors or self-repel.
    bridge_idxs: set[int] = {
        idx for idx, ncs in enumerate(part_ncs) if _pair_iso_mm(ncs, ncs) >= 2.0
    }
    if nc_clearances:
        n_fp = len(board.footprints)
        for i in range(n_fp):
            if i in bridge_idxs or not part_ncs[i]:
                continue
            for j in range(i + 1, n_fp):
                if j in bridge_idxs or not part_ncs[j]:
                    continue
                if pb.parts[i][5] and pb.parts[j][5]:
                    continue
                iso_mm = _pair_iso_mm(part_ncs[i], part_ncs[j])
                if iso_mm >= 2.0:
                    pb.repulsions.append((i, j, iso_mm + 0.4, 35.0))
                else:
                    shared_hv = max(
                        (nc_clearances.get((a, a), 0.0) for a in (part_ncs[i] & part_ncs[j])),
                        default=0.0,
                    )
                    hv_clr = max(iso_mm, shared_hv)
                    if hv_clr >= 1.2 and board.footprints[i].nets != board.footprints[j].nets:
                        pb.repulsions.append((i, j, hv_clr + 0.25, 10.0))

    if floorplan:
        # FB/COMP vs SW inductor repulsion (matches Hartley lint fb_near_sw / fb_under_inductor).
        conv_inductors: dict[str, list[int]] = {}
        conv_controls: dict[str, list[int]] = {}
        for ref, part in sorted(parts.items()):
            rval = ((part.get("role") or {}).get("value")) or ""
            tref = ((part.get("target") or {}).get("value") or {}).get("ref")
            if tref and ref in ref_to_idx:
                if rval == "converter_inductor":
                    conv_inductors.setdefault(tref, []).append(ref_to_idx[ref])
                elif rval == "converter_control":
                    conv_controls.setdefault(tref, []).append(ref_to_idx[ref])
        for conv_ref, ctrl_idxs in sorted(conv_controls.items()):
            for ci in ctrl_idxs:
                for li in conv_inductors.get(conv_ref, ()):
                    if ci != li:
                        pb.repulsions.append((ci, li, 3.0, 4.0))
        # Build disjoint movable same-side clusters for atomic Rust SA translate/rotate moves.
        used_in_cluster: set[int] = set()
        for bclaim in (intent or {}).get("blocks", []):
            if model.is_uncertain(bclaim):
                continue
            bval = bclaim.get("value") or {}
            anchor_ref = bval.get("anchor")
            if not anchor_ref or anchor_ref not in ref_to_idx:
                continue
            ai = ref_to_idx[anchor_ref]
            if pb.parts[ai][5] or ai in used_in_cluster:
                continue
            a_side = pb.parts[ai][4]
            members = [
                ref_to_idx[m] for m in bval.get("members", [])
                if m in ref_to_idx and m != anchor_ref
                and not pb.parts[ref_to_idx[m]][5]
                and pb.parts[ref_to_idx[m]][4] == a_side
                and ref_to_idx[m] not in used_in_cluster
            ]
            if 1 <= len(members) <= 10:
                cl = [ai, *members]
                pb.clusters.append(cl)
                used_in_cluster.update(cl)
        sheets_map: dict[str, list[Footprint]] = {}
        for fp in board.footprints:
            sh = fp.sheet.strip("/") if fp.sheet else ""
            if sh:
                sheets_map.setdefault(sh, []).append(fp)
        if len(sheets_map) >= 2:
            # Pick the pin closest to each footprint's centroid (0, 0) to avoid artificial rotational torque.
            center_pin: dict[str, int] = {}
            best_d2: dict[str, float] = {}
            for pin_idx, (part_idx, lx, ly) in enumerate(pb.pins):
                ref_i = pb.refs[part_idx]
                d2 = lx * lx + ly * ly
                if ref_i not in best_d2 or d2 < best_d2[ref_i]:
                    best_d2[ref_i] = d2
                    center_pin[ref_i] = pin_idx
            for _sheet_name, members in sorted(sheets_map.items()):
                anchors = sorted(
                    (
                        fp for fp in members
                        if fp.ref in center_pin
                        and len(fp.pads) >= 4
                        and ref_to_idx[fp.ref] not in bridge_idxs
                    ),
                    key=lambda f: (-len(f.pads), f.ref),
                )
                if not anchors:
                    continue
                anchor = anchors[0]
                anchor_idx = ref_to_idx[anchor.ref]
                anchor_pin = center_pin[anchor.ref]
                for fp in members:
                    if fp.ref == anchor.ref or fp.ref in fixed or fp.ref not in center_pin:
                        continue
                    fp_idx = ref_to_idx[fp.ref]
                    if fp_idx in bridge_idxs or _pair_iso_mm(part_ncs[fp_idx], part_ncs[anchor_idx]) >= 2.0:
                        continue
                    role_v = ((parts.get(fp.ref, {}).get("role") or {}).get("value")) or ""
                    if role_v in ("decap", "test_point", "mechanical"):
                        continue
                    pb.affinities.append(([center_pin[fp.ref], anchor_pin], SHEET_COHESION_WEIGHT))
                    pb.sheet_cohesion_links += 1
    for o in board.outline:
        pb.outers.append([tuple(p) for p in o.outer])
        pb.holes += [[tuple(p) for p in h] for h in o.holes]
    two_sided = "F" in sides.values() and "B" in sides.values()
    pb.holes.extend(placement_obstacle_holes(board, set(fixed), two_sided))
    pb.side_obstacles = side_obstacle_rects(board, set(fixed))
    # Build passive regularity groups matching metrics.regularity (same lib_id & side, >= 2 members).
    group_counts: dict[tuple[str, str], int] = {}
    for fp in board.footprints:
        if fp.prefix in PASSIVE_PREFIXES and len(fp.pads) <= 3:
            key = (fp.lib_id, sides[fp.ref])
            group_counts[key] = group_counts.get(key, 0) + 1
    group_ids: dict[tuple[str, str], int] = {}
    next_gid = 1
    for key, cnt in sorted(group_counts.items()):
        if cnt >= 2:
            group_ids[key] = next_gid
            next_gid += 1
    pb.reg_groups = [
        group_ids.get((fp.lib_id, sides[fp.ref]), 0)
        if (fp.prefix in PASSIVE_PREFIXES and len(fp.pads) <= 3)
        else 0
        for fp in board.footprints
    ]
    pb.w_reg = 2.2 if any(pb.reg_groups) else 0.0
    return pb


def rust_problem(pb: PlaceProblem, *, w_hpwl: float = 1.0, w_cross: float = 2.0, gap: float = GAP_MM,
                 w_reg: float | None = None):
    try:
        from placer_core import PlacementProblem
    except ImportError as error:
        raise ImportError(
            "placer_core Rust extension is not built; the placement solver "
            "requires it. Build it with `maturin develop --release` in "
            "crates/placer-core (or `pip install ./crates/placer-core`)."
        ) from error

    eff_gap = gap
    silk_clr = float(pb.board.rules.get("silk_clearance") or 0.0)
    board_area = float(pb.board.board_area_mm2 or 0.0)
    cy_area = sum(fp.courtyard_area for fp in pb.board.footprints)
    if silk_clr >= 0.08 and board_area > 0.0 and cy_area < 0.50 * board_area:
        eff_gap = max(eff_gap, min(0.08, 0.6 * silk_clr))
    args = (pb.parts, pb.pins, pb.nets, pb.outers, pb.holes, eff_gap, w_hpwl, w_cross)
    kwargs: dict = {}
    if pb.affinities:
        kwargs["affinities"] = pb.affinities
    if any(pb.sub_rects):
        kwargs["sub_rects"] = pb.sub_rects
        if pb.cavity_eligible:
            kwargs["cavity_eligible"] = pb.cavity_eligible
    if pb.clusters:
        kwargs["clusters"] = pb.clusters
    if pb.repulsions:
        kwargs["repulsions"] = pb.repulsions
    if pb.outline_locals:
        kwargs["outline_locals"] = pb.outline_locals
    if pb.obstacle_locals:
        kwargs["obstacle_locals"] = pb.obstacle_locals
    if any(pb.opp_locals):
        kwargs["opp_locals"] = pb.opp_locals
    if pb.side_obstacles:
        kwargs["side_obstacles"] = pb.side_obstacles
    eff_w_reg = pb.w_reg if w_reg is None else w_reg
    if eff_w_reg > 0.0 and any(pb.reg_groups):
        kwargs["reg_groups"] = pb.reg_groups
        kwargs["w_reg"] = eff_w_reg
    return PlacementProblem(*args, **kwargs)


def apply(pb: PlaceProblem, poses: list[tuple[float, float, int]]) -> Board:
    """A copy of the board with the solved poses (rotation in degrees, side from S2b)."""
    out = pb.board.copy()
    layer_flip = {layer.canonical: other.canonical for layer, other in zip(
        out.copper_layers, reversed(out.copper_layers), strict=True)}
    layer_flip.update({"F.Cu": "B.Cu", "B.Cu": "F.Cu"})
    for fp, (x, y, r), part in zip(out.footprints, poses, pb.parts, strict=True):
        if part[5]:
            continue  # fixed parts keep their exact pose, odd angles included
        fp.x, fp.y, fp.rotation = round(x, 4), round(y, 4), float(r * 90 % 360)
        if fp.side != pb.sides[fp.ref]:
            for pad in fp.pads:
                pad.local = (pad.local[0], -pad.local[1])
                pad.rotation = -pad.rotation
                pad.layers = tuple(layer_flip.get(layer, layer) for layer in pad.layers)
            fp.courtyard.polygons = [[(x, -y) for x, y in ring] for ring in fp.courtyard.polygons]
            fp.courtyard.holes = [[[(x, -y) for x, y in hole] for hole in poly_holes] for poly_holes in fp.courtyard.holes]
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
    for pins, weight in pb.affinities:
        origin = xy(pins[0])
        hpwl += weight * min(math.dist(origin, xy(index)) for index in pins[1:])
    crossings = sum(1 for i in range(len(edges)) for j in range(i + 1, len(edges))
                    if edges[i][2] != edges[j][2] and segments_cross(edges[i][0], edges[i][1], edges[j][0], edges[j][1]))
    return {"hpwl": hpwl, "crossings": float(crossings), "total": w_hpwl * hpwl + w_cross * crossings}


def board_diag(pb: PlaceProblem) -> float:
    xs = [p[0] for ring in pb.outers for p in ring]
    ys = [p[1] for ring in pb.outers for p in ring]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 50.0


