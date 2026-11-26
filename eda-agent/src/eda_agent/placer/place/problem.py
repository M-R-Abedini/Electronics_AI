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
    board_polygon,
    courtyard_polygon,
    is_shield_cavity_eligible,
    placement_obstacle_holes,
)
from ..eval.metrics import zone_nets
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
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


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
        if (
            fp.prefix in ("H", "MH", "HOLE", "FID") or "mountinghole" in fp.lib_id.lower()
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
    clusters: list[list[int]] = field(default_factory=list)
    repulsions: list[tuple[int, int, float, float]] = field(default_factory=list)
    local_block_links: int = 0
    sheet_cohesion_links: int = 0

    def poses(self) -> list[tuple[float, float, int]]:
        return [(p[7], p[8], p[9]) for p in self.parts]


def _quarter(rotation: float) -> int:
    return round(rotation / 90.0) % 4


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
    boxes = {fp.ref: local_box(fp) for fp in board.footprints}
    mechanics = (intent or {}).get("mechanical", {})
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
        if flip:  # the footprint frame mirrors in y when the part changes side (KiCad left-right flip)
            y0, y1 = -y1, -y0
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
            r = 0
        else:
            r = _quarter(fp.rotation) if on_grid else 0
        pb.parts.append((x0, y0, x1, y1, 0 if sides[fp.ref] == "F" else 1, is_fixed, not is_fixed,
                         fp.x, fp.y, r))
        pb.sub_rects.append(subs)
        pb.cavity_eligible.append(not access and is_shield_cavity_eligible(fp))
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
        if kind != "decap" and (not floorplan or kind not in LOCAL_BLOCK_ROLES):
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
                pb.affinities.append(([source[0][1], *pins], DECAP_AFFINITY_WEIGHT if kind == "decap" else LOCAL_BLOCK_WEIGHT))
                pb.local_block_links += kind != "decap"
                # Inline Connector -> ESD -> IC pull: gently attract ESD toward the downstream IC too.
                if floorplan and kind == "esd":
                    ic_pins = [
                        pi for pi in pin_of.get(common_net, [])
                        if pb.refs[pb.pins[pi][0]] not in (ref, target_ref)
                        and ((parts.get(pb.refs[pb.pins[pi][0]], {}).get("role") or {}).get("value")) in (
                            "ic", "converter_ic", "linear_regulator", "power_switch",
                        )
                    ]
                    if ic_pins:
                        pb.affinities.append(([source[0][1], *ic_pins], 0.6 * LOCAL_BLOCK_WEIGHT))
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
                    (fp for fp in members if fp.ref in center_pin and len(fp.pads) >= 4),
                    key=lambda f: (-len(f.pads), f.ref),
                )
                if not anchors:
                    continue
                anchor = anchors[0]
                anchor_pin = center_pin[anchor.ref]
                for fp in members:
                    if fp.ref == anchor.ref or fp.ref in fixed or fp.ref not in center_pin:
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
    return pb


def rust_problem(pb: PlaceProblem, *, w_hpwl: float = 1.0, w_cross: float = 2.0, gap: float = GAP_MM):
    try:
        from placer_core import PlacementProblem
    except ImportError as error:
        raise ImportError(
            "placer_core Rust extension is not built; the placement solver "
            "requires it. Build it with `maturin develop --release` in "
            "crates/placer-core (or `pip install ./crates/placer-core`)."
        ) from error

    args = (pb.parts, pb.pins, pb.nets, pb.outers, pb.holes, gap, w_hpwl, w_cross)
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


