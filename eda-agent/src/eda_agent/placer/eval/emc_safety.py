# SPDX-License-Identifier: MIT
"""First-principles PCB EMI/EMC, HV safety & thermal DFM checks (Rules 10-18).

Purely geometric and board-agnostic checks operating on the generic Board IR
plus filled copper geometry, designed to complement ``placer/eval/lint.py``:

10. ``mounting_hole_copper_clearance``  -- NPTH mounting-hole wall to copper clearance (LV & HV)
11. ``copper_pour_under_unfused_mains`` -- Copper plane/pour beneath unfused AC mains EMI filter
12. ``hv_pad_creepage_undersized``      -- Geometric pad-to-pad gap <1.2 mm on 2-pin HV passives
13. ``tvs_ground_return_inductance``    -- High-energy SMD TVS (<2 parallel GND return vias)
14. ``thermal_pad_via_paste_wicking``   -- Large open thermal vias (>=0.35 mm drill) inside SMD EP
15. ``hv_power_on_inner_layer``         -- Mains/HV power nets buried on thin inner copper (IPC-2152 / IEC 61010-1)
16. ``switch_node_inner_or_oversized``  -- High-dV/dt switch node on inner layers or oversized antenna (AN-70 / TIDA)
17. ``prefuse_mains_trace_undersized``  -- Mains AC / pre-fuse trace narrower than fusing margin (<0.80 mm)
18. ``hv_vertical_prepreg_overlap``     -- HV-to-LV or switch-to-feedback vertical overlap across thin (<0.40 mm) prepreg
"""

from __future__ import annotations

import math
import re
from typing import Any

from shapely.geometry import LineString, Point, Polygon
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

_HV_POWER_LOOP_RE = re.compile(
    r"(?:^|[/_-])("
    r"HV_?BUS|HV_?RECT|HV_?DC|HV_?IN|HV_?RAIL|BULK_?HV|DC_?BUS|BUS_?HV|"
    r"PRI_?RET|PRI_?GND|HV_?RET|PGND_?HV"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_SWITCH_NODE_RE = re.compile(
    r"(?:^|[/_-])("
    r"FLY_?DRAIN|PRI_?DRAIN|RCD_?CLAMP|SNUB(?:BER)?|SW_?NODE|"
    r"(?:BUCK|BOOST|FLY|SEC_IN|SEC_OUT)_?SW\d*|SW\d*|LX\d*|PHASE\d*"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_SWITCH_FALSE_POS_RE = re.compile(
    r"(SWDIO|SWCLK|SWO|SW_?BTN|BTN|BUTTON|KEY|DIP_?SW|TACT)",
    re.IGNORECASE,
)

_HV_LOW_CURRENT_SENSE_RE = re.compile(
    r"(?:^|[/_-])("
    r"V_?SENSE(?:_?MID)?|FLY_?V|FLY_?FB|FLY_?BPP|FLY_?BPS|FLY_?FWD|FLY_?SR|"
    r"FB|VFB|COMP|ISET|SS|EN|UVLO|OVP"
    r")(?:$|[/_-])",
    re.IGNORECASE,
)

_SENSITIVE_FEEDBACK_RE = re.compile(
    r"(?:^|[/_-])(FLY_?FB|FB|VFB|VSENSE|V_?SENSE|COMP|ADC_?.*|RTD_?.*|PT100_?.*)(?:$|[/_-])",
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
# Rules 15-18 physical thresholds (IEC 61010-1 / IEC 62368-1 / IPC-2152 / AN-70)
HV_INNER_LEN_MIN_MM = 5.0
HV_INNER_FRAC_WARN = 0.25
MAINS_INNER_FRAC_CRIT = 0.35
SW_INNER_LEN_WARN_MM = 2.0
HV_SW_LEN_WARN_MM = 55.0
HV_SW_LEN_CRIT_MM = 85.0
LV_SW_LEN_WARN_MM = 22.0
LV_SW_LEN_CRIT_MM = 40.0
SW_DETOUR_WARN_RATIO = 1.65
SW_DETOUR_MIN_LEN_MM = 22.0
MAINS_WIDTH_CRIT_MM = 0.45
MAINS_WIDTH_WARN_MM = 0.80
SOLID_INSULATION_MIN_MM = 0.40
SW_FB_DIELECTRIC_MIN_MM = 0.30


def hv_power_and_switch_nets(board: Board, intent: dict | None = None) -> dict[str, set[str]]:
    """Classify mains AC, HV power-loop, and high-dV/dt switching-node nets generically."""
    all_nets: set[str] = {n.name for n in board.nets if n.name}
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net:
                all_nets.add(pad.net)

    hv_nets = _hv_net_set(board)
    mains_ac: set[str] = {n for n in all_nets if _MAINS_AC_RE.search(n)}
    switch_nodes: set[str] = {
        n for n in all_nets
        if _SWITCH_NODE_RE.search(n) and not _SWITCH_FALSE_POS_RE.search(n)
    }
    if intent:
        for nname, meta in (intent.get("nets") or {}).items():
            cls_val = str(((meta.get("class") or {}).get("value")) or "").lower()
            role_val = str(((meta.get("role") or {}).get("value")) or "").lower()
            if cls_val in ("sw_node", "switch_node") or role_val in ("sw_node", "switch_node"):
                switch_nodes.add(nname)
            if cls_val in ("mains", "ac_mains"):
                mains_ac.add(nname)

    hv_power: set[str] = set(mains_ac)
    for n in all_nets:
        if _HV_LOW_CURRENT_SENSE_RE.search(n):
            continue
        if _HV_POWER_LOOP_RE.search(n) or (n in hv_nets and n not in switch_nodes):
            hv_power.add(n)

    hv_switch_nodes = switch_nodes & (hv_nets | {n for n in switch_nodes if re.search(r"(FLY_?DRAIN|PRI_?DRAIN|RCD_?CLAMP|SNUB)", n, re.IGNORECASE)})
    return {
        "hv_all": hv_nets | hv_power | hv_switch_nodes,
        "mains_ac": mains_ac,
        "hv_power": hv_power,
        "switch_nodes": switch_nodes,
        "hv_switch_nodes": hv_switch_nodes,
    }


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
    intent: dict | None = None,
) -> dict[str, Any]:
    """Run Rules 10-18 and return structured metrics dict for ``lint_board()``."""
    hv_nets = _hv_net_set(board)
    net_groups = hv_power_and_switch_nets(board, intent)

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

    # ------------------------------------------------------------------
    # Rules 15-18: Offline HV Power, Switch-Node EMI, Pre-Fuse Sizing & Solid Insulation
    # (IEC 61010-1 Clause 6.7 / IEC 62368-1 / IPC-2152 / Power Integrations AN-70 & TI TIDA)
    # ------------------------------------------------------------------
    ordered_layers = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    n_layers = len(ordered_layers)
    inner_layers = set(ordered_layers[1:-1]) if n_layers >= 3 else set()

    def _seg_len(t: dict[str, Any]) -> float:
        pts = t.get("points") or []
        if len(pts) < 2:
            return 0.0
        return sum(
            math.hypot(float(pts[i + 1][0]) - float(pts[i][0]), float(pts[i + 1][1]) - float(pts[i][1]))
            for i in range(len(pts) - 1)
        )

    def _seg_line(t: dict[str, Any]) -> LineString | None:
        pts = [(float(p[0]), float(p[1])) for p in (t.get("points") or ())]
        if len(pts) < 2:
            return None
        if len(pts) == 2 and math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]) < 1e-9:
            return None
        return LineString(pts)

    by_net_tracks: dict[str, list[dict[str, Any]]] = {}
    for t in tracks:
        tn = t.get("net") or ""
        if tn:
            by_net_tracks.setdefault(tn, []).append(t)

    # Rule 15: hv_power_on_inner_layer (mains & primary HV power prefer outer 1oz/2oz copper)
    hv_inner_rows: list[dict[str, Any]] = []
    if has_copper and n_layers >= 4 and inner_layers:
        for net in sorted(net_groups["hv_power"] - net_groups["switch_nodes"]):
            tlist = by_net_tracks.get(net) or []
            if not tlist:
                continue
            outer_mm = 0.0
            inner_mm = 0.0
            sample_pt: tuple[float, float] | None = None
            for t in tlist:
                sl = _seg_len(t)
                canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
                if canon in inner_layers:
                    inner_mm += sl
                    if sample_pt is None and t.get("points"):
                        sample_pt = (float(t["points"][0][0]), float(t["points"][0][1]))
                else:
                    outer_mm += sl
            tot_mm = outer_mm + inner_mm
            if tot_mm <= 0.0:
                continue
            frac = inner_mm / tot_mm
            is_mains = net in net_groups["mains_ac"]
            hv_inner_rows.append({
                "net": net,
                "mains_ac": is_mains,
                "outer_mm": round(outer_mm, 2),
                "inner_mm": round(inner_mm, 2),
                "inner_fraction": round(frac, 3),
            })
            if inner_mm > HV_INNER_LEN_MIN_MM and frac > HV_INNER_FRAC_WARN:
                sev = "critical" if (is_mains and frac > MAINS_INNER_FRAC_CRIT) else "warning"
                add_finding(
                    "hv_power_on_inner_layer",
                    sev,
                    f"{'Mains AC' if is_mains else 'HV power'} net '{net}' routes {inner_mm:.1f} mm "
                    f"({frac:.0%}) on inner copper layer(s); prefer outer 1oz/2oz F.Cu/B.Cu per "
                    f"IPC-2152 (2x thermal/fusing capacity) & IEC 61010-1 inspectability",
                    net=net,
                    location_mm=sample_pt,
                    value=round(frac, 3),
                    limit=MAINS_INNER_FRAC_CRIT if sev == "critical" else HV_INNER_FRAC_WARN,
                )

    # Rule 16: switch_node_inner_or_oversized (high-dV/dt switch node EMI & loop area)
    def _net_pad_mst_mm(net_name: str) -> float:
        pts: list[tuple[float, float]] = []
        for fp in board.footprints:
            for pad, pt in fp.pads_world():
                if pad.net == net_name:
                    pts.append(pt)
        if len(pts) < 2:
            return 0.0
        used = [False] * len(pts)
        used[0] = True
        total = 0.0
        for _ in range(len(pts) - 1):
            best = math.inf
            best_j = -1
            for i, pi in enumerate(pts):
                if not used[i]:
                    continue
                for j, pj in enumerate(pts):
                    if used[j]:
                        continue
                    d = math.hypot(pi[0] - pj[0], pi[1] - pj[1])
                    if d < best:
                        best = d
                        best_j = j
            if best_j < 0:
                break
            used[best_j] = True
            total += best
        return total

    sw_rows: list[dict[str, Any]] = []
    if has_copper:
        for net in sorted(net_groups["switch_nodes"]):
            tlist = by_net_tracks.get(net) or []
            if not tlist:
                continue
            outer_mm = 0.0
            inner_mm = 0.0
            sample_pt = None
            for t in tlist:
                sl = _seg_len(t)
                canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
                if canon in inner_layers:
                    inner_mm += sl
                    if sample_pt is None and t.get("points"):
                        sample_pt = (float(t["points"][0][0]), float(t["points"][0][1]))
                else:
                    outer_mm += sl
                    if sample_pt is None and t.get("points"):
                        sample_pt = (float(t["points"][0][0]), float(t["points"][0][1]))
            tot_mm = outer_mm + inner_mm
            pad_mst_mm = _net_pad_mst_mm(net)
            detour_ratio = (tot_mm / pad_mst_mm) if pad_mst_mm > 1.0 else None
            is_hv_sw = net in net_groups["hv_switch_nodes"]
            sw_rows.append({
                "net": net,
                "hv": is_hv_sw,
                "total_mm": round(tot_mm, 2),
                "inner_mm": round(inner_mm, 2),
                "pad_mst_mm": round(pad_mst_mm, 2),
                "detour_ratio": round(detour_ratio, 2) if detour_ratio is not None else None,
            })
            if n_layers >= 4 and inner_mm > SW_INNER_LEN_WARN_MM:
                add_finding(
                    "switch_node_inner_or_oversized",
                    "warning",
                    f"High-dV/dt switch node '{net}' routes {inner_mm:.1f} mm on inner layer(s); "
                    f"keep switch nodes strictly on outer F.Cu/B.Cu to avoid via inductance and "
                    f"inner-layer capacitive EMI coupling (Power Integrations AN-70 / TI TIDA)",
                    net=net,
                    location_mm=sample_pt,
                    value=round(inner_mm, 2),
                    limit=SW_INNER_LEN_WARN_MM,
                )
            warn_lim = HV_SW_LEN_WARN_MM if is_hv_sw else LV_SW_LEN_WARN_MM
            crit_lim = HV_SW_LEN_CRIT_MM if is_hv_sw else LV_SW_LEN_CRIT_MM
            if tot_mm > crit_lim:
                add_finding(
                    "switch_node_inner_or_oversized",
                    "critical",
                    f"High-dV/dt switch node '{net}' total copper length is {tot_mm:.1f} mm "
                    f"(> {crit_lim:.0f} mm); compact switch/transformer/clamp placement to "
                    f"minimize monopole EMI radiation and primary hot-loop inductance",
                    net=net,
                    location_mm=sample_pt,
                    value=round(tot_mm, 2),
                    limit=crit_lim,
                )
            elif tot_mm > warn_lim or (
                detour_ratio is not None
                and tot_mm > SW_DETOUR_MIN_LEN_MM
                and detour_ratio > SW_DETOUR_WARN_RATIO
            ):
                add_finding(
                    "switch_node_inner_or_oversized",
                    "warning",
                    f"High-dV/dt switch node '{net}' total copper length is {tot_mm:.1f} mm "
                    f"(pad MST {pad_mst_mm:.1f} mm, ratio {detour_ratio or 1.0:.2f}x); "
                    f"tighten switch-node copper area to reduce radiated EMI",
                    net=net,
                    location_mm=sample_pt,
                    value=round(tot_mm, 2),
                    limit=warn_lim,
                )

    # Rule 17: prefuse_mains_trace_undersized (IEC 61010-1 / UL 61010-1 / IPC-2152 fusing margin)
    mains_width_rows: list[dict[str, Any]] = []
    if has_copper:
        for net in sorted(net_groups["mains_ac"]):
            tlist = [t for t in (by_net_tracks.get(net) or ()) if _seg_len(t) >= 0.5]
            if not tlist:
                tlist = by_net_tracks.get(net) or []
            if not tlist:
                continue
            min_w = min(float(t.get("width") or 0.0) for t in tlist)
            sample_t = min(tlist, key=lambda t: float(t.get("width") or 0.0))
            sp = (float(sample_t["points"][0][0]), float(sample_t["points"][0][1])) if sample_t.get("points") else None
            mains_width_rows.append({"net": net, "min_width_mm": round(min_w, 3)})
            if min_w < MAINS_WIDTH_CRIT_MM:
                add_finding(
                    "prefuse_mains_trace_undersized",
                    "critical",
                    f"Mains AC net '{net}' has trace width {min_w:.2f} mm (< {MAINS_WIDTH_CRIT_MM:.2f} mm); "
                    f"use >=1.0 mm outer copper so the physical fuse opens before PCB copper vaporizes",
                    net=net,
                    location_mm=sp,
                    value=round(min_w, 3),
                    limit=MAINS_WIDTH_CRIT_MM,
                )
            elif min_w < MAINS_WIDTH_WARN_MM:
                add_finding(
                    "prefuse_mains_trace_undersized",
                    "warning",
                    f"Mains AC net '{net}' has minimum trace width {min_w:.2f} mm (< {MAINS_WIDTH_WARN_MM:.2f} mm); "
                    f"increase mains netclass width to >=1.0 mm on outer copper for IEC 61010-1/61000-4-5 surge margin",
                    net=net,
                    location_mm=sp,
                    value=round(min_w, 3),
                    limit=MAINS_WIDTH_WARN_MM,
                )

    # Rule 18: hv_vertical_prepreg_overlap (IEC 61010-1 Clause 6.7 >=0.40 mm solid insulation)
    vertical_overlaps: list[dict[str, Any]] = []
    if has_copper and n_layers >= 4:
        # Build adjacent-layer dielectric thickness lookup from stackup (fallback: standard 4L 0.21 mm prepreg)
        diel_mm: dict[tuple[str, str], float] = {}
        diels = list(getattr(board.stackup, "dielectrics", None) or ())
        if len(diels) >= n_layers - 1:
            for idx in range(n_layers - 1):
                th = float(getattr(diels[idx], "thickness_mm", 0.0) or 0.0)
                if th > 0.0:
                    a, b = ordered_layers[idx], ordered_layers[idx + 1]
                    diel_mm[(a, b)] = th
                    diel_mm[(b, a)] = th
        else:
            # Standard 4-layer 1.6 mm fab stackup: 0.21 mm outer prepregs, ~1.06 mm core
            diel_mm[(ordered_layers[0], ordered_layers[1])] = 0.21
            diel_mm[(ordered_layers[1], ordered_layers[0])] = 0.21
            diel_mm[(ordered_layers[-2], ordered_layers[-1])] = 0.21
            diel_mm[(ordered_layers[-1], ordered_layers[-2])] = 0.21

        # Partition tracks into HV-domain vs non-HV-domain (excluding primary low-voltage AUX_PRIMARY nets)
        hv_all_set = net_groups["hv_all"]
        primary_aux_classes = {
            nc.name for nc in board.netclasses
            if re.search(r"(HV|MAINS|PRIMARY)", nc.name, re.IGNORECASE)
        }
        primary_domain_nets = set(hv_all_set) | {
            n.name for n in board.nets
            if n.netclass in primary_aux_classes or re.search(r"(PRI_|FLY_V|FLY_BPP|V_SENSE_MID)", n.name, re.IGNORECASE)
        }
        # Group buffered lines by layer for adjacent thin-dielectric pairs
        hv_geoms_by_lyr: dict[str, list[tuple[str, Any]]] = {}
        lv_geoms_by_lyr: dict[str, list[tuple[str, Any]]] = {}
        sw_geoms_by_lyr: dict[str, list[tuple[str, Any]]] = {}
        fb_geoms_by_lyr: dict[str, list[tuple[str, Any]]] = {}
        for t in tracks:
            tn = t.get("net") or ""
            if not tn:
                continue
            line = _seg_line(t)
            if line is None:
                continue
            canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
            hw = max(0.08, 0.5 * float(t.get("width") or 0.2))
            buf = line.buffer(hw, cap_style=2, quad_segs=2)
            if tn in hv_all_set:
                hv_geoms_by_lyr.setdefault(canon, []).append((tn, buf))
            elif tn not in primary_domain_nets:
                lv_geoms_by_lyr.setdefault(canon, []).append((tn, buf))
            if tn in net_groups["hv_switch_nodes"]:
                sw_geoms_by_lyr.setdefault(canon, []).append((tn, buf))
            elif _SENSITIVE_FEEDBACK_RE.search(tn):
                fb_geoms_by_lyr.setdefault(canon, []).append((tn, buf))

        reported_pairs: set[tuple[str, str, str, str]] = set()
        for idx in range(n_layers - 1):
            la, lb = ordered_layers[idx], ordered_layers[idx + 1]
            th = diel_mm.get((la, lb))
            if th is None:
                continue
            if th < SOLID_INSULATION_MIN_MM:
                for l_hv, l_lv in ((la, lb), (lb, la)):
                    for hv_n, hv_g in hv_geoms_by_lyr.get(l_hv, ()):
                        for lv_n, lv_g in lv_geoms_by_lyr.get(l_lv, ()):
                            key = ("hv_lv", hv_n, lv_n, f"{l_hv}/{l_lv}")
                            if key in reported_pairs:
                                continue
                            if hv_g.intersects(lv_g) and hv_g.intersection(lv_g).area > 0.05:
                                reported_pairs.add(key)
                                c = hv_g.intersection(lv_g).centroid
                                vertical_overlaps.append({
                                    "kind": "hv_to_lv",
                                    "net_a": hv_n,
                                    "layer_a": l_hv,
                                    "net_b": lv_n,
                                    "layer_b": l_lv,
                                    "dielectric_mm": round(th, 3),
                                })
                                add_finding(
                                    "hv_vertical_prepreg_overlap",
                                    "critical",
                                    f"HV net '{hv_n}' on {l_hv} vertically overlaps secondary/LV net '{lv_n}' "
                                    f"on {l_lv} across {th:.2f} mm prepreg (< {SOLID_INSULATION_MIN_MM:.2f} mm "
                                    f"IEC 61010-1 Clause 6.7 reinforced solid insulation)",
                                    net=hv_n,
                                    location_mm=(c.x, c.y),
                                    value=round(th, 3),
                                    limit=SOLID_INSULATION_MIN_MM,
                                )
            if th < SW_FB_DIELECTRIC_MIN_MM:
                for l_sw, l_fb in ((la, lb), (lb, la)):
                    for sw_n, sw_g in sw_geoms_by_lyr.get(l_sw, ()):
                        for fb_n, fb_g in fb_geoms_by_lyr.get(l_fb, ()):
                            key = ("sw_fb", sw_n, fb_n, f"{l_sw}/{l_fb}")
                            if key in reported_pairs:
                                continue
                            if sw_g.intersects(fb_g) and sw_g.intersection(fb_g).area > 0.05:
                                reported_pairs.add(key)
                                c = sw_g.intersection(fb_g).centroid
                                vertical_overlaps.append({
                                    "kind": "sw_to_fb",
                                    "net_a": sw_n,
                                    "layer_a": l_sw,
                                    "net_b": fb_n,
                                    "layer_b": l_fb,
                                    "dielectric_mm": round(th, 3),
                                })
                                add_finding(
                                    "hv_vertical_prepreg_overlap",
                                    "warning",
                                    f"HV switch node '{sw_n}' on {l_sw} crosses vertically over sensitive "
                                    f"feedback/sense net '{fb_n}' on {l_fb} across {th:.2f} mm dielectric "
                                    f"(high C*dV/dt capacitive coupling)",
                                    net=sw_n,
                                    location_mm=(c.x, c.y),
                                    value=round(th, 3),
                                    limit=SW_FB_DIELECTRIC_MIN_MM,
                                )

    return {
        "mounting_hole_clearance": mh_rows,
        "copper_pour_under_unfused_mains": emi_pour_hits,
        "hv_pad_creepage": hv_passive_rows,
        "tvs_ground_return": tvs_rows,
        "thermal_pad_vias": thermal_rows,
        "hv_power_inner_layer": hv_inner_rows,
        "switch_node_emi": sw_rows,
        "prefuse_mains_width": mains_width_rows,
        "hv_vertical_prepreg_overlap": vertical_overlaps,
    }
