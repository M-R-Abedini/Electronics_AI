# SPDX-License-Identifier: MIT
"""Mains-first outer-layer pre-routing for high-clearance AC mains nets (S6a stage).

High-voltage AC mains nets (AC_L, AC_L_FUSED, AC_N, MAINS, LINE, NEUTRAL) carry the widest
track width (>= 0.8-1.0 mm per IEC 61010-1 / IPC-2152 fusing margin) AND the largest clearance
corridors (1.5-2.5+ mm). When left to a general rip-up router alongside narrower 0.4-0.5 mm nets,
narrower nets often consume the wide outer-layer corridors first and force mains traces onto thin
inner copper or 0.14 mm neckdown.

``plan_mains_highways`` routes eligible AC mains nets FIRST on alternating outer copper layers
(F.Cu / B.Cu) using octilinear A* with exact segment-to-obstacle clearance verification against
all pads, NPTH mounting holes, rule-area keepouts, board outline margins, and prior fixed copper.
"""

from __future__ import annotations

import heapq
import math
import re
from typing import Any

from ..eval.emc_safety import hv_power_and_switch_nets
from ..ir import Board, Footprint, Pad

MAINS_TARGET_WIDTH_MM = 1.0
MAINS_MIN_SMD_PAD_MM = 0.60
MAINS_DEFAULT_CLR_MM = 1.60
MAINS_LN_CLR_MM = 2.55
NPTH_HV_CLR_MM = 6.15
GRID_STEP_MM = 0.50
MAX_ASTAR_EXPANSIONS = 6_000

_PRIMARY_CLASS_RE = re.compile(r"(HV|MAINS|PRIMARY|AUX_PRI)", re.IGNORECASE)
_PRIMARY_NET_RE = re.compile(r"(AUX|PRI_|FLY_|HV_|AC_|RCD_|V_SENSE)", re.IGNORECASE)
_MAINS_NEUTRAL_RE = re.compile(r"(?:^|[/_-])(AC_?N\w*|MAINS_?N\w*|NEUTRAL\w*)(?:$|[/_-])", re.IGNORECASE)


def _is_opposite_mains_polarity(net_a: str, net_b: str, mains_set: set[str]) -> bool:
    """True when one net is AC Line (L) and the other is AC Neutral (N) (requires 2.5+ mm L-N clearance)."""
    if net_a not in mains_set or net_b not in mains_set:
        return False
    a_n = bool(_MAINS_NEUTRAL_RE.search(net_a))
    b_n = bool(_MAINS_NEUTRAL_RE.search(net_b))
    return a_n != b_n


def _seg_pt_dist(x0: float, y0: float, x1: float, y1: float, px: float, py: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    u = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / l2))
    return math.hypot(px - (x0 + u * dx), py - (y0 + u * dy))


def _seg_seg_dist(
    ax0: float, ay0: float, ax1: float, ay1: float,
    bx0: float, by0: float, bx1: float, by1: float,
) -> float:
    def _ccw(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p3[1] - p1[1]) * (p2[0] - p1[0]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    a, b, c, d = (ax0, ay0), (ax1, ay1), (bx0, by0), (bx1, by1)
    d1, d2 = _ccw(a, b, c), _ccw(a, b, d)
    d3, d4 = _ccw(c, d, a), _ccw(c, d, b)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return 0.0
    return min(
        _seg_pt_dist(ax0, ay0, ax1, ay1, bx0, by0),
        _seg_pt_dist(ax0, ay0, ax1, ay1, bx1, by1),
        _seg_pt_dist(bx0, by0, bx1, by1, ax0, ay0),
        _seg_pt_dist(bx0, by0, bx1, by1, ax1, ay1),
    )


def _pad_on_layer(fp: Footprint, pad: Pad, canon_layer: str) -> bool:
    if pad.kind in ("thru_hole", "np_thru_hole"):
        return True
    if pad.layers:
        return canon_layer in pad.layers or any(lyr.endswith(canon_layer) for lyr in pad.layers)
    return (fp.side == "F" and canon_layer == "F.Cu") or (fp.side == "B" and canon_layer == "B.Cu")


def _mst_edges(pts: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if len(pts) < 2:
        return []
    used = [False] * len(pts)
    used[0] = True
    edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for _ in range(len(pts) - 1):
        best_d = math.inf
        best_pair: tuple[int, int] | None = None
        for i, pi in enumerate(pts):
            if not used[i]:
                continue
            for j, pj in enumerate(pts):
                if used[j]:
                    continue
                d = math.hypot(pi[0] - pj[0], pi[1] - pj[1])
                if d < best_d:
                    best_d = d
                    best_pair = (i, j)
        if best_pair is None:
            break
        u, v = best_pair
        used[v] = True
        edges.append((pts[u], pts[v]))
    return edges


def _mains_safe_width(board: Board, net_name: str, target_mm: float = MAINS_TARGET_WIDTH_MM) -> float | None:
    smd_min = target_mm
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net != net_name or pad.kind != "smd":
                continue
            smd_min = min(smd_min, float(pad.size[0]), float(pad.size[1]))
    if smd_min < MAINS_MIN_SMD_PAD_MM:
        return None
    return round(min(target_mm, smd_min) * 20.0) / 20.0


def _route_single_layer_branch(
    board: Board,
    net_name: str,
    canon_layer: str,
    width_mm: float,
    src_xy: tuple[float, float],
    dst_xy: tuple[float, float],
    *,
    mains_set: set[str],
    hv_set: set[str],
    base_clr_mm: float,
    ln_clr_mm: float,
    fixed: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    x0_b, y0_b, x1_b, y1_b = board.outline_box
    edge_margin = max(0.60, float(board.rules.get("copper_edge_clearance") or 0.50))
    hw = 0.5 * width_mm
    gx0 = max(x0_b + edge_margin + hw, min(src_xy[0], dst_xy[0]) - 14.0)
    gy0 = max(y0_b + edge_margin + hw, min(src_xy[1], dst_xy[1]) - 14.0)
    gx1 = min(x1_b - edge_margin - hw, max(src_xy[0], dst_xy[0]) + 14.0)
    gy1 = min(y1_b - edge_margin - hw, max(src_xy[1], dst_xy[1]) + 14.0)
    if gx1 <= gx0 + GRID_STEP_MM or gy1 <= gy0 + GRID_STEP_MM:
        return None

    # Classify primary-domain nets so secondary/isolated nets keep 8.1 mm reinforced clearance
    # while primary-side LV nets (FLY_BPP, V_SENSE_MID, etc.) use the intra-primary HV clearance.
    net_to_nc = {n.name: n.netclass for n in board.nets}
    primary_nets: set[str] = set(hv_set) | set(mains_set)
    for n in board.nets:
        if _PRIMARY_CLASS_RE.search(n.netclass) or _PRIMARY_NET_RE.search(n.name):
            primary_nets.add(n.name)

    # Build obstacle disks, segments, and rule-area boxes within local search ROI
    obs_disks: list[tuple[float, float, float]] = []
    for fp in board.footprints:
        for pad, (px, py) in fp.pads_world():
            if pad.net == net_name:
                continue
            if not _pad_on_layer(fp, pad, canon_layer):
                continue
            if not (gx0 - 12.0 <= px <= gx1 + 12.0 and gy0 - 12.0 <= py <= gy1 + 12.0):
                continue
            if pad.kind == "np_thru_hole" and pad.drill:
                r = 0.5 * max(float(pad.drill[0]), float(pad.drill[1]))
                clr = NPTH_HV_CLR_MM
            else:
                sx, sy = float(pad.size[0]), float(pad.size[1])
                r = 0.5 * max(sx, sy)
                pnet = pad.net or ""
                if _is_opposite_mains_polarity(net_name, pnet, mains_set):
                    clr = ln_clr_mm
                elif pnet in primary_nets or (pnet in net_to_nc and _PRIMARY_CLASS_RE.search(net_to_nc[pnet])):
                    clr = base_clr_mm
                elif pnet:
                    clr = max(base_clr_mm, 8.10)
                else:
                    clr = base_clr_mm
            obs_disks.append((px, py, r + clr))

    obs_segs: list[tuple[float, float, float, float, float]] = []
    for item in fixed:
        inet = item.get("net") or ""
        if inet == net_name:
            continue
        clr = ln_clr_mm if _is_opposite_mains_polarity(net_name, inet, mains_set) else base_clr_mm
        if "points" in item:
            if item.get("layer") != canon_layer:
                continue
            pts = item["points"]
            ihw = 0.5 * float(item.get("width") or 0.2)
            for k in range(len(pts) - 1):
                obs_segs.append((float(pts[k][0]), float(pts[k][1]), float(pts[k + 1][0]), float(pts[k + 1][1]), ihw + clr))
        elif "x" in item and "y" in item:
            vr = 0.5 * float(item.get("diameter") or 0.6)
            obs_disks.append((float(item["x"]), float(item["y"]), vr + clr))

    ko_boxes: list[tuple[float, float, float, float]] = []
    for ra in board.rule_areas:
        ko = ra.get("keepout") or {}
        if not ko.get("tracks", False):
            continue
        lyrs = set(ra.get("layers") or ())
        if lyrs and canon_layer not in lyrs:
            continue
        for ring in ra.get("polygons") or ():
            if len(ring) >= 3:
                xs = [float(pt[0]) for pt in ring]
                ys = [float(pt[1]) for pt in ring]
                ko_boxes.append((min(xs), min(ys), max(xs), max(ys)))

    nx = int((gx1 - gx0) / GRID_STEP_MM) + 1
    ny = int((gy1 - gy0) / GRID_STEP_MM) + 1
    if nx * ny > 45_000:
        return None

    def _pt_ok(x: float, y: float) -> bool:
        if math.hypot(x - src_xy[0], y - src_xy[1]) < 0.75 or math.hypot(x - dst_xy[0], y - dst_xy[1]) < 0.75:
            return True
        for kx0, ky0, kx1, ky1 in ko_boxes:
            if kx0 - hw - 0.10 <= x <= kx1 + hw + 0.10 and ky0 - hw - 0.10 <= y <= ky1 + hw + 0.10:
                return False
        for ox, oy, rad in obs_disks:
            lim = rad + hw + 0.04
            dx = x - ox
            if dx >= lim or dx <= -lim:
                continue
            dy = y - oy
            if dy >= lim or dy <= -lim:
                continue
            if dx * dx + dy * dy < lim * lim:
                return False
        for sx0, sy0, sx1, sy1, sclr in obs_segs:
            if _seg_pt_dist(sx0, sy0, sx1, sy1, x, y) < sclr + hw + 0.04:
                return False
        return True

    def _seg_ok(ax: float, ay: float, bx: float, by: float) -> bool:
        for kx0, ky0, kx1, ky1 in ko_boxes:
            if max(ax, bx) >= kx0 - hw - 0.08 and min(ax, bx) <= kx1 + hw + 0.08 and max(ay, by) >= ky0 - hw - 0.08 and min(ay, by) <= ky1 + hw + 0.08:
                steps = max(2, int(math.hypot(bx - ax, by - ay) / 0.30) + 1)
                for s in range(steps + 1):
                    px = ax + (bx - ax) * s / steps
                    py = ay + (by - ay) * s / steps
                    if kx0 - hw - 0.08 <= px <= kx1 + hw + 0.08 and ky0 - hw - 0.08 <= py <= ky1 + hw + 0.08:
                        return False
        for ox, oy, rad in obs_disks:
            if _seg_pt_dist(ax, ay, bx, by, ox, oy) < rad + hw - 0.03:
                return False
        for sx0, sy0, sx1, sy1, sclr in obs_segs:
            if _seg_seg_dist(ax, ay, bx, by, sx0, sy0, sx1, sy1) < sclr + hw - 0.03:
                return False
        return True

    si = (max(0, min(nx - 1, round((src_xy[0] - gx0) / GRID_STEP_MM))),
          max(0, min(ny - 1, round((src_xy[1] - gy0) / GRID_STEP_MM))))
    ti = (max(0, min(nx - 1, round((dst_xy[0] - gx0) / GRID_STEP_MM))),
          max(0, min(ny - 1, round((dst_xy[1] - gy0) / GRID_STEP_MM))))

    cache: dict[tuple[int, int], bool] = {}

    def _cell_ok(ix: int, iy: int) -> bool:
        key = (ix, iy)
        if key not in cache:
            cache[key] = _pt_ok(gx0 + ix * GRID_STEP_MM, gy0 + iy * GRID_STEP_MM)
        return cache[key]

    dirs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
            (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
    pq: list[tuple[float, float, int, int, int]] = [(math.hypot(ti[0] - si[0], ti[1] - si[1]), 0.0, si[0], si[1], -1)]
    best: dict[tuple[int, int], float] = {si: 0.0}
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    found: tuple[int, int] | None = None
    expansions = 0
    while pq and expansions < MAX_ASTAR_EXPANSIONS:
        _f, g, cx, cy, pdir = heapq.heappop(pq)
        if (cx, cy) == ti:
            found = (cx, cy)
            break
        if g > best.get((cx, cy), 1e18) + 1e-6:
            continue
        expansions += 1
        for di, (dx, dy, cost) in enumerate(dirs):
            px, py = cx + dx, cy + dy
            if not (0 <= px < nx and 0 <= py < ny) or not _cell_ok(px, py):
                continue
            turn = 0.15 if (pdir >= 0 and pdir != di) else 0.0
            ng = g + cost + turn
            if ng < best.get((px, py), 1e18):
                best[(px, py)] = ng
                parent[(px, py)] = (cx, cy)
                heapq.heappush(pq, (ng + math.hypot(ti[0] - px, ti[1] - py), ng, px, py, di))

    if found is None:
        return None
    chain: list[tuple[int, int]] = []
    cur = found
    while cur in parent:
        chain.append(cur)
        cur = parent[cur]
    chain.append(si)
    chain.reverse()

    pts = [src_xy] + [(round(gx0 + ix * GRID_STEP_MM, 4), round(gy0 + iy * GRID_STEP_MM, 4)) for ix, iy in chain[1:-1]] + [dst_xy]
    # Safe collinear collapse
    simp = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = i + 1
        while j + 1 < len(pts):
            ax, ay = pts[i]
            bx, by = pts[j]
            cx, cy = pts[j + 1]
            if abs((bx - ax) * (cy - by) - (by - ay) * (cx - bx)) < 1e-4 and _seg_ok(ax, ay, cx, cy):
                j += 1
            else:
                break
        simp.append(pts[j])
        i = j

    tracks: list[dict[str, Any]] = []
    for k in range(len(simp) - 1):
        ax, ay = simp[k]
        bx, by = simp[k + 1]
        if math.hypot(bx - ax, by - ay) < 1e-4:
            continue
        if not _seg_ok(ax, ay, bx, by):
            return None
        tracks.append({
            "kind": "track",
            "net": net_name,
            "layer": canon_layer,
            "width": width_mm,
            "points": [(round(ax, 4), round(ay, 4)), (round(bx, 4), round(by, 4))],
            "locked": True,
        })
    return tracks or None


def plan_mains_highways(
    board: Board,
    intent: dict | None = None,
    *,
    fixed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pre-route eligible PTH/outer AC mains nets on alternating outer layers (F.Cu / B.Cu).

    Only activates on boards that contain high-clearance (>= 1.2 mm) AC mains nets with >= 2 pads.
    Returns ``{"copper": [...], "rows": [...]}`` matching ``s6a.plan`` conventions.
    """
    ordered = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    if len(ordered) < 2:
        return {"copper": [], "rows": []}
    outer_layers = [ordered[0], ordered[-1]]
    groups = hv_power_and_switch_nets(board, intent)
    mains_set = groups["mains_ac"]
    hv_set = groups["hv_all"]
    if not mains_set:
        return {"copper": [], "rows": []}

    # Only activate when the board genuinely has high-clearance mains/HV netclasses (>= 1.2 mm)
    nc_by_name = {nc.name: nc for nc in board.netclasses}
    net_to_nc = {n.name: n.netclass for n in board.nets}
    max_hv_clr = max(
        (float(nc_by_name[net_to_nc[n]].clearance) for n in mains_set if n in net_to_nc and net_to_nc[n] in nc_by_name),
        default=0.0,
    )
    if max_hv_clr < 1.2:
        return {"copper": [], "rows": []}

    base_clr = max(MAINS_DEFAULT_CLR_MM, max_hv_clr + 0.06)
    ln_clr = max(MAINS_LN_CLR_MM, base_clr)

    # Collect eligible AC mains nets:
    # - All-PTH mains nets can route on either outer layer (F.Cu / B.Cu)
    # - Mixed PTH + single-outer-side SMD mains nets (e.g. THT fuse -> SMD bridge rectifier)
    #   route on that outer SMD layer at SMD-safe width (capped at narrow pad dimension).
    candidates: list[tuple[int, str, float, list[tuple[float, float]], list[str]]] = []
    for nname in sorted(mains_set):
        pads_xy: list[tuple[float, float]] = []
        smd_outer_layers: set[str] = set()
        unsupported_pad = False
        for fp in board.footprints:
            for pad, pt in fp.pads_world():
                if pad.net != nname:
                    continue
                if pad.kind == "thru_hole":
                    pads_xy.append(pt)
                elif pad.kind == "smd":
                    pad_outer = [lyr for lyr in outer_layers if _pad_on_layer(fp, pad, lyr)]
                    if len(pad_outer) != 1:
                        unsupported_pad = True
                        break
                    smd_outer_layers.add(pad_outer[0])
                    pads_xy.append(pt)
                else:
                    unsupported_pad = True
                    break
            if unsupported_pad:
                break
        if unsupported_pad or len(pads_xy) < 2 or len(smd_outer_layers) > 1:
            continue
        w = _mains_safe_width(board, nname)
        if w is None:
            continue
        if smd_outer_layers:
            allowed = [next(iter(smd_outer_layers))]
            # Route single-layer-constrained mains nets after flexible all-PTH nets OR before?
            # Put constrained nets at priority 0 only if few pins; actually put all-PTH (flexible)
            # aware of constrained layers so AC_L and AC_L_FUSED naturally land on opposite outer layers!
            prio = 0
        else:
            allowed = list(outer_layers)
            prio = 1
        candidates.append((prio, nname, w, pads_xy, allowed))

    if not candidates:
        return {"copper": [], "rows": []}

    # Route single-side-constrained nets first (only 1 valid outer layer), then flexible all-PTH
    # nets preferring the opposite/less-used outer layer to avoid same-layer L-N clearance jams.
    candidates.sort(key=lambda item: (item[0], item[1]))

    acc_fixed = list(fixed or ())
    copper: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    layer_usage: dict[str, int] = {lyr: 0 for lyr in outer_layers}
    for _prio, nname, width_mm, pts, allowed in candidates:
        ordered_cands = sorted(allowed, key=lambda lyr: (layer_usage.get(lyr, 0), outer_layers.index(lyr)))
        edges = _mst_edges(pts)
        routed_net: list[dict[str, Any]] = []
        chosen_layer: str | None = None
        for cand_layer in ordered_cands:
            trial_tracks: list[dict[str, Any]] = []
            ok = True
            for src_xy, dst_xy in edges:
                got = _route_single_layer_branch(
                    board, nname, cand_layer, width_mm, src_xy, dst_xy,
                    mains_set=mains_set, hv_set=hv_set,
                    base_clr_mm=base_clr, ln_clr_mm=ln_clr,
                    fixed=acc_fixed,
                )
                if got is None:
                    ok = False
                    break
                trial_tracks.extend(got)
            if ok and trial_tracks:
                routed_net = trial_tracks
                chosen_layer = cand_layer
                break
        if routed_net and chosen_layer:
            copper.extend(routed_net)
            acc_fixed.extend(routed_net)
            layer_usage[chosen_layer] = layer_usage.get(chosen_layer, 0) + 1
            tot_len = sum(
                math.hypot(t["points"][1][0] - t["points"][0][0], t["points"][1][1] - t["points"][0][1])
                for t in routed_net
            )
            rows.append({
                "net": nname,
                "layer": chosen_layer,
                "width_mm": width_mm,
                "branches": len(edges),
                "length_mm": round(tot_len, 2),
                "routed": True,
            })
        else:
            rows.append({
                "net": nname,
                "layer": ordered_cands[0],
                "width_mm": width_mm,
                "branches": len(edges),
                "routed": False,
            })
    return {"copper": copper, "rows": rows}
