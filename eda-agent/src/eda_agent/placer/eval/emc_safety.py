# SPDX-License-Identifier: MIT
"""First-principles PCB EMI/EMC, HV safety & thermal DFM checks (Rules 10-14).

Purely geometric and board-agnostic checks operating on the generic Board IR
plus filled copper geometry, designed to complement ``placer/eval/lint.py``:

10. ``mounting_hole_copper_clearance``  -- NPTH mounting-hole wall to copper clearance (LV & HV)
11. ``copper_pour_under_unfused_mains`` -- Copper plane/pour beneath unfused AC mains EMI filter
12. ``hv_pad_creepage_undersized``      -- Geometric pad-to-pad gap <1.2 mm on 2-pin HV passives
13. ``tvs_ground_return_inductance``    -- High-energy SMD TVS (<2 parallel GND return vias)
14. ``thermal_pad_via_paste_wicking``   -- Large open thermal vias (>=0.35 mm drill) inside SMD EP
"""

from __future__ import annotations

import math
import re
from typing import Any

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from ..ir import Board, Footprint, Pad, is_ground_net, is_power_net
from . import legality

_HV_NET_RE = re.compile(
    r"(?:^|[/_-])("
    r"AC_?L(?:INE)?|AC_?N(?:EUT(?:RAL)?)?|AC_?L_?FUSED|AC_?L_?RECT|"
    r"MAINS|LINE_?IN|NEUTRAL|HV_?BUS|HV_?RECT|HV_?DC|HV_?IN|HV_?RAIL|"
    r"FLY_?DRAIN|RCD_?CLAMP|SNUB(?:BER)?|PRI_?DRAIN|BULK_?HV|"
    r"(?:110|120|220|230|240|277|325|380|390|400|480)V(?:AC|DC)?"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_MAINS_AC_RE = re.compile(
    r"(?:^|[/_-])("
    r"AC_?L(?:INE)?|AC_?N(?:EUT(?:RAL)?)?|AC_?L_?FUSED|AC_?L_?RECT|"
    r"MAINS|LINE_?IN|NEUTRAL|(?:220|230|240|277)V(?:AC)?"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_SURGE_TVS_RE = re.compile(
    r"(SM[ABC]J|P[46]KE|1\.5KE|D_SM[ABC]|DO-214|TVS)",
    re.IGNORECASE,
)

_MOUNTING_LIB_RE = re.compile(r"(MountingHole|Plain_Hole|Hole_)", re.IGNORECASE)

# Declared physical thresholds
MH_LV_CRIT_MM = 0.50
MH_LV_WARN_MM = 1.20
MH_HV_CRIT_MM = 4.00
MH_HV_WARN_MM = 6.00
HV_ZONE_RADIUS_MM = 10.0
HV_PAD_CRIT_MM = 0.90
HV_PAD_WARN_MM = 1.20
EMI_POUR_OVERLAP_MM2 = 2.0
TVS_VIA_RADIUS_MM = 3.5
THERMAL_PAD_MIN_AREA_MM2 = 6.0
OPEN_THERMAL_VIA_DRILL_MM = 0.35


def _hv_net_set(board: Board) -> set[str]:
    """Identify HV/mains nets from net names and high-clearance HV/mains netclasses."""
    hv_classes = {
        nc.name
        for nc in board.netclasses
        if nc.clearance >= 1.0
        and re.search(r"(HV|MAINS|AC|PRIMARY|HIGH_?VOLT)", nc.name, re.IGNORECASE)
    }
    hv_nets = {n.name for n in board.nets if n.netclass in hv_classes or _HV_NET_RE.search(n.name)}
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net and _HV_NET_RE.search(pad.net):
                hv_nets.add(pad.net)
    return hv_nets


def _pad_half_extent(pad: Pad) -> float:
    return 0.5 * max(float(pad.size[0]), float(pad.size[1]))


def _is_npth_mounting_hole(fp: Footprint, pad: Pad) -> bool:
    if pad.kind != "np_thru_hole":
        return False
    if not pad.drill:
        return False
    dr = max(float(pad.drill[0]), float(pad.drill[1]))
    if dr < 2.0:
        return False
    return (
        fp.prefix in ("H", "MH", "HOLE")
        or bool(_MOUNTING_LIB_RE.search(fp.lib_id))
        or len(fp.pads) == 1
    )


def run_emc_safety_checks(
    board: Board,
    *,
    has_copper: bool,
    tracks: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    ref_planes: dict[str, dict[str, Any]],
    add_finding: Any,
) -> dict[str, Any]:
    """Run Rules 10-14 and return structured metrics dict for ``lint_board()``."""
    hv_nets = _hv_net_set(board)

    # ------------------------------------------------------------------
    # Rule 10: mounting_hole_copper_clearance (NPTH hole wall to any copper)
    # ------------------------------------------------------------------
    mh_rows: list[dict[str, Any]] = []
    # Pre-collect electrical pads for fast distance checks
    elec_pads: list[tuple[float, float, float, str, str]] = []
    for fp in board.footprints:
        for pad, (px, py) in fp.pads_world():
            if pad.kind == "np_thru_hole" or not pad.net:
                continue
            elec_pads.append((px, py, _pad_half_extent(pad), pad.net, f"{fp.ref}.{pad.number}"))

    all_plane_polys: list[tuple[str, str, Any]] = [
        (canon, net, geom)
        for canon, by_net in ref_planes.items()
        for net, geom in by_net.items()
        if geom is not None and not geom.is_empty
    ]

    for fp in sorted(board.footprints, key=lambda f: f.ref):
        for pad, (hx, hy) in fp.pads_world():
            if not _is_npth_mounting_hole(fp, pad):
                continue
            hole_r = 0.5 * max(float(pad.drill[0]), float(pad.drill[1]))
            hpt = Point(hx, hy)
            min_d = math.inf
            nearest_desc = ""
            near_hv = False

            for px, py, pr, pnet, pdesc in elec_pads:
                dc = math.hypot(px - hx, py - hy)
                if pnet in hv_nets and dc <= HV_ZONE_RADIUS_MM + hole_r:
                    near_hv = True
                d = dc - pr - hole_r
                if d < min_d:
                    min_d = d
                    nearest_desc = f"pad {pdesc} ({pnet})"

            if has_copper:
                for v in vias:
                    vx, vy = float(v["x"]), float(v["y"])
                    vr = 0.5 * float(v.get("diameter") or 0.5)
                    vnet = v.get("net") or ""
                    dc = math.hypot(vx - hx, vy - hy)
                    if vnet in hv_nets and dc <= HV_ZONE_RADIUS_MM + hole_r:
                        near_hv = True
                    d = dc - vr - hole_r
                    if d < min_d:
                        min_d = d
                        nearest_desc = f"via ({vnet})"

                for t in tracks:
                    pts = t.get("points") or []
                    if len(pts) < 2:
                        continue
                    tw = 0.5 * float(t.get("width") or 0.15)
                    tnet = t.get("net") or ""
                    # Fast endpoint + segment distance check
                    for i in range(len(pts) - 1):
                        x0, y0 = float(pts[i][0]), float(pts[i][1])
                        x1, y1 = float(pts[i + 1][0]), float(pts[i + 1][1])
                        dx, dy = x1 - x0, y1 - y0
                        seg2 = dx * dx + dy * dy
                        if seg2 <= 1e-12:
                            dc = math.hypot(hx - x0, hy - y0)
                        else:
                            u = max(0.0, min(1.0, ((hx - x0) * dx + (hy - y0) * dy) / seg2))
                            dc = math.hypot(hx - (x0 + u * dx), hy - (y0 + u * dy))
                        if tnet in hv_nets and dc <= HV_ZONE_RADIUS_MM + hole_r:
                            near_hv = True
                        d = dc - tw - hole_r
                        if d < min_d:
                            min_d = d
                            nearest_desc = f"track ({tnet})"

                for canon, pnet, geom in all_plane_polys:
                    d_plane = float(geom.distance(hpt)) - hole_r
                    if pnet in hv_nets and d_plane <= HV_ZONE_RADIUS_MM:
                        near_hv = True
                    if d_plane < min_d:
                        min_d = d_plane
                        nearest_desc = f"zone {pnet} on {canon}"

            if not math.isfinite(min_d):
                continue
            crit_lim = MH_HV_CRIT_MM if near_hv else MH_LV_CRIT_MM
            warn_lim = MH_HV_WARN_MM if near_hv else MH_LV_WARN_MM
            mh_rows.append({
                "ref": fp.ref,
                "hv_zone": near_hv,
                "min_clearance_mm": round(min_d, 3),
                "nearest": nearest_desc,
            })
            if has_copper and min_d < crit_lim:
                add_finding(
                    "mounting_hole_copper_clearance",
                    "critical",
                    f"NPTH mounting hole {fp.ref} has only {min_d:.2f} mm copper clearance to "
                    f"{nearest_desc} ({'< ' + str(crit_lim) + ' mm'} {'HV' if near_hv else 'LV'} limit); "
                    f"add a circular all-layer keepout around {fp.ref}",
                    refs=[fp.ref],
                    location_mm=(hx, hy),
                    value=min_d,
                    limit=crit_lim,
                )
            elif has_copper and min_d < warn_lim:
                add_finding(
                    "mounting_hole_copper_clearance",
                    "warning",
                    f"NPTH mounting hole {fp.ref} has {min_d:.2f} mm copper clearance to "
                    f"{nearest_desc} (< {warn_lim} mm {'HV' if near_hv else 'LV'} recommended)",
                    refs=[fp.ref],
                    location_mm=(hx, hy),
                    value=min_d,
                    limit=warn_lim,
                )

    # ------------------------------------------------------------------
    # Rule 11: copper_pour_under_unfused_mains (no plane/pour under AC EMI filter)
    # ------------------------------------------------------------------
    # Collect ALL filled copper zones (including PRI_RET / HV_RET / COM pours not in GND ref_planes)
    all_filled_zones: list[tuple[str, str, Any]] = list(all_plane_polys)
    seen_zone_keys = {(canon, net) for canon, net, _ in all_plane_polys}
    name_to_canon = {cl.name: cl.canonical for cl in board.copper_layers}
    for zone in board.zones:
        znet = zone.get("net") or ""
        if not znet:
            continue
        if "fill_geometry" in zone:
            for lname, rows in zone.get("fill_geometry", {}).items():
                canon = name_to_canon.get(lname, lname)
                if (canon, znet) in seen_zone_keys:
                    continue
                polys = []
                for row in rows:
                    if not row.get("island") and len(row.get("outer") or ()) >= 3:
                        p = Polygon(row["outer"], row.get("holes") or [])
                        p = p if p.is_valid else p.buffer(0)
                        if not p.is_empty:
                            polys.append(p)
                if polys:
                    all_filled_zones.append((canon, znet, unary_union(polys)))
                    seen_zone_keys.add((canon, znet))
        elif zone.get("filled") or any(float(v or 0.0) > 0.0 for v in (zone.get("filled_area_mm2") or {}).values()):
            rings = zone.get("outline") or [
                c.get("outer") for c in (zone.get("outline_components") or ()) if c.get("outer")
            ]
            polys = []
            for ring in rings:
                if len(ring) >= 3:
                    p = Polygon(ring)
                    p = p if p.is_valid else p.buffer(0)
                    if not p.is_empty:
                        polys.append(p)
            if polys:
                zgeom = unary_union(polys)
                for lname in zone.get("layers") or ():
                    canon = name_to_canon.get(lname, lname)
                    if (canon, znet) in seen_zone_keys:
                        continue
                    all_filled_zones.append((canon, znet, zgeom))
                    seen_zone_keys.add((canon, znet))

    emi_pour_hits: list[dict[str, Any]] = []
    if has_copper and all_filled_zones:
        for fp in sorted(board.footprints, key=lambda f: f.ref):
            ac_nets = {p.net for p in fp.pads if p.net and _MAINS_AC_RE.search(p.net)}
            if not ac_nets:
                continue
            is_emi_front = (
                fp.prefix in ("J", "P", "CON", "F", "RV", "MOV")
                or (fp.prefix == "C" and len(ac_nets) >= 2)
                or (fp.prefix == "L" and len(ac_nets) >= 1)
            )
            if not is_emi_front:
                continue
            cy = legality.courtyard_polygon(fp)
            if cy is None or cy.is_empty:
                continue
            for canon, pnet, geom in all_filled_zones:
                if not geom.intersects(cy):
                    continue
                ov_area = float(geom.intersection(cy).area)
                if ov_area >= EMI_POUR_OVERLAP_MM2:
                    emi_pour_hits.append({
                        "ref": fp.ref,
                        "layer": canon,
                        "zone_net": pnet,
                        "overlap_mm2": round(ov_area, 2),
                    })
                    add_finding(
                        "copper_pour_under_unfused_mains",
                        "critical",
                        f"Copper pour '{pnet}' on {canon} fills {ov_area:.1f} mm^2 beneath AC "
                        f"mains/EMI filter part {fp.ref}; remove planes under unfused mains/EMI "
                        f"components to prevent HF capacitive bypass and surge clearance loss",
                        net=pnet,
                        refs=[fp.ref],
                        location_mm=fp.position,
                        value=ov_area,
                        limit=EMI_POUR_OVERLAP_MM2,
                    )

    # ------------------------------------------------------------------
    # Rule 12: hv_pad_creepage_undersized (2-pin HV passive pad gap <1.2 mm)
    # ------------------------------------------------------------------
    hv_passive_rows: list[dict[str, Any]] = []
    for fp in sorted(board.footprints, key=lambda f: f.ref):
        if fp.prefix not in ("R", "C") or len(fp.pads) != 2:
            continue
        nets = {p.net for p in fp.pads if p.net}
        if len(nets) < 2 or not (nets & hv_nets):
            continue
        (p1, (x1, y1)), (p2, (x2, y2)) = list(fp.pads_world())
        # Conservative edge-to-edge gap along center-to-center axis
        dist_cc = math.hypot(x2 - x1, y2 - y1)
        # Project each pad's half-size along the axis connecting the two pads
        ang = math.atan2(y2 - y1, x2 - x1)
        def _proj_half(pad: Pad, fp_rot: float = fp.rotation, axis_ang: float = ang) -> float:
            pa = math.radians(fp_rot + pad.rotation)
            d_ang = axis_ang - pa
            return 0.5 * (abs(pad.size[0] * math.cos(d_ang)) + abs(pad.size[1] * math.sin(d_ang)))
        gap = max(0.0, dist_cc - _proj_half(p1) - _proj_half(p2))
        hv_net = min(nets & hv_nets)
        hv_passive_rows.append({"ref": fp.ref, "net": hv_net, "pad_gap_mm": round(gap, 3)})
        if gap < HV_PAD_CRIT_MM:
            add_finding(
                "hv_pad_creepage_undersized",
                "critical",
                f"HV passive {fp.ref} ({fp.value}) on '{hv_net}' has only {gap:.2f} mm pad-to-pad "
                f"creepage (< {HV_PAD_CRIT_MM} mm); upgrade to >=1206 HV series parts or THT axial/disc",
                net=hv_net,
                refs=[fp.ref],
                location_mm=fp.position,
                value=gap,
                limit=HV_PAD_CRIT_MM,
            )
        elif gap < HV_PAD_WARN_MM:
            add_finding(
                "hv_pad_creepage_undersized",
                "warning",
                f"HV passive {fp.ref} ({fp.value}) on '{hv_net}' has {gap:.2f} mm pad-to-pad "
                f"creepage (< {HV_PAD_WARN_MM} mm); verify working voltage rating and creepage margin",
                net=hv_net,
                refs=[fp.ref],
                location_mm=fp.position,
                value=gap,
                limit=HV_PAD_WARN_MM,
            )

    # ------------------------------------------------------------------
    # Rule 13: tvs_ground_return_inductance (surge TVS >=2 GND return vias)
    # ------------------------------------------------------------------
    tvs_rows: list[dict[str, Any]] = []
    if has_copper:
        conn_nets = {
            p.net
            for fp in board.footprints
            if fp.prefix in ("J", "P", "CN", "CON")
            for p in fp.pads
            if p.net and not is_ground_net(p.net)
        }
        vias_by_net: dict[str, list[tuple[float, float]]] = {}
        for v in vias:
            vn = v.get("net") or ""
            if vn:
                vias_by_net.setdefault(vn, []).append((float(v["x"]), float(v["y"])))

        for fp in sorted(board.footprints, key=lambda f: f.ref):
            if fp.prefix not in ("D", "TVS", "Z", "RV"):
                continue
            if not _SURGE_TVS_RE.search(f"{fp.value} {fp.lib_id}"):
                continue
            if not (fp.nets & conn_nets):
                continue
            gnd_pads = [(p, pt) for p, pt in fp.pads_world() if p.net and is_ground_net(p.net) and p.kind == "smd"]
            for gpad, (gx, gy) in gnd_pads:
                gnet = gpad.net or ""
                # Check if same-layer ground pour already covers the TVS ground pad directly
                pad_layer = "F.Cu" if fp.side == "F" else "B.Cu"
                same_lyr_plane = (ref_planes.get(pad_layer) or {}).get(gnet)
                if same_lyr_plane is not None and same_lyr_plane.distance(Point(gx, gy)) <= 0.6:
                    tvs_rows.append({"ref": fp.ref, "net": gnet, "gnd_vias": None, "direct_pour": True})
                    continue
                near_vias = sum(
                    1 for vx, vy in vias_by_net.get(gnet, ())
                    if math.hypot(vx - gx, vy - gy) <= TVS_VIA_RADIUS_MM
                )
                tvs_rows.append({"ref": fp.ref, "net": gnet, "gnd_vias": near_vias, "direct_pour": False})
                if near_vias < 2:
                    add_finding(
                        "tvs_ground_return_inductance",
                        "warning",
                        f"Surge TVS {fp.ref} ({fp.value}) has only {near_vias} ground via(s) on "
                        f"'{gnet}' within {TVS_VIA_RADIUS_MM} mm; use >=2 parallel low-inductance "
                        f"vias (or direct top ground pour) for IEC 61000-4-2/4-5 surge clamping",
                        net=gnet,
                        refs=[fp.ref],
                        location_mm=(gx, gy),
                        value=float(near_vias),
                        limit=2.0,
                    )

    # ------------------------------------------------------------------
    # Rule 14: thermal_pad_via_paste_wicking (large open vias in SMD EP)
    # ------------------------------------------------------------------
    thermal_rows: list[dict[str, Any]] = []
    if has_copper:
        for fp in sorted(board.footprints, key=lambda f: f.ref):
            if fp.prefix not in ("U", "IC", "Q"):
                continue
            for pad, (px, py) in fp.pads_world():
                if pad.kind != "smd" or not pad.net:
                    continue
                area = float(pad.size[0]) * float(pad.size[1])
                if area < THERMAL_PAD_MIN_AREA_MM2:
                    continue
                if not (is_ground_net(pad.net) or is_power_net(pad.net)):
                    continue
                # Build rotated exposed-pad polygon
                hw, hh = 0.5 * float(pad.size[0]), 0.5 * float(pad.size[1])
                ang = math.radians(fp.rotation + pad.rotation)
                ca, sa = math.cos(ang), math.sin(ang)
                corners = [
                    (px + cx * ca - cy * sa, py + cx * sa + cy * ca)
                    for cx, cy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
                ]
                ep_poly = Polygon(corners)
                large_vias = [
                    v for v in vias
                    if (v.get("net") or "") == pad.net
                    and float(v.get("drill") or 0.0) >= OPEN_THERMAL_VIA_DRILL_MM
                    and ep_poly.contains(Point(float(v["x"]), float(v["y"])))
                ]
                thermal_rows.append({
                    "ref": fp.ref,
                    "pad": pad.number,
                    "large_open_vias": len(large_vias),
                })
                if large_vias:
                    max_drill = max(float(v.get("drill") or 0.0) for v in large_vias)
                    add_finding(
                        "thermal_pad_via_paste_wicking",
                        "warning",
                        f"Exposed thermal pad {fp.ref}.{pad.number} contains {len(large_vias)} "
                        f"open through-via(s) with drill up to {max_drill:.2f} mm (>= {OPEN_THERMAL_VIA_DRILL_MM} mm); "
                        f"use <=0.30 mm vias offset into un-pasted mask webs or plugged/capped VIPPO "
                        f"to prevent solder paste wicking during reflow",
                        net=pad.net,
                        refs=[fp.ref],
                        location_mm=(px, py),
                        value=max_drill,
                        limit=OPEN_THERMAL_VIA_DRILL_MM,
                    )

    return {
        "mounting_hole_clearance": mh_rows,
        "copper_pour_under_unfused_mains": emi_pour_hits,
        "hv_pad_creepage": hv_passive_rows,
        "tvs_ground_return": tvs_rows,
        "thermal_pad_vias": thermal_rows,
    }
