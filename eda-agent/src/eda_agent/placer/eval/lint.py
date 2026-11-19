# SPDX-License-Identifier: MIT
"""First-principles electrical & return-path Lint on real copper (architecture 8.3, plan F6).

All rules operate purely on the generic Board IR + design_intent (no board-specific
heuristics) so the same physics checks generalize to any KiCad or Altium PCB:

1. decap_loop                real copper path from decap supply pad to target IC pin (via order + length)
2. reference_continuity      adjacent filled reference plane coverage, fast-signal split crossings,
                             and escape-region via-antipad plane perforation (plan 4.2 F5)
3. return_via                layer transitions: nearby same-reference return via (<= 1.5 mm) or
                             stitching capacitor (<= 3.0 mm) on GND<->PWR reference changes
4. crystal_keepout           unrelated signal copper passing under a crystal without a shielding plane
5. fb_vs_sw                  converter feedback tracks near SW node or passing under converter inductor
6. buck_hot_loop             area of switching converter Cin <-> switch <-> GND hot loop
7. esd_to_connector          copper path length from connector pin to ESD protection device
8. two_layer_gnd_continuity  on 2-layer boards, continuous B.Cu GND fill fraction and island count

When the board has no routed tracks (``has_copper=False``), placement-only observations
are still reported, while ``score`` is ``None`` so placement-only Pro Score v1 remains
unchanged until real copper exists.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any

from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from ..intent import model
from ..ir import ROLE_MIN_NET_FRACTION, Board, Footprint, is_ground_net, is_power_net
from . import legality

# Declared physical thresholds (architecture 8.3)
RETURN_VIA_MAX_MM = 1.5
STITCH_CAP_MAX_MM = 3.0
DECAP_COPPER_WARN_MM = 6.0
DECAP_COPPER_CRIT_MM = 12.0
FB_SW_CRIT_MM = 1.0
FB_SW_WARN_MM = 2.0
HOT_LOOP_WARN_MM2 = 120.0
HOT_LOOP_CRIT_MM2 = 250.0
ESD_CONN_WARN_MM = 5.0
ESD_CONN_HIGH_MM = 10.0
MIN_ISLAND_AREA_MM2 = 1.0
ESCAPE_PERFORATION_LIMIT = 0.40

_FAST_NET_CLASSES = frozenset({
    "clock", "high_speed", "diff_pair", "rf", "sw_node", "crystal", "analog_sensitive", "sensitive_analog",
})


def _layer_maps(board: Board) -> tuple[dict[str, str], dict[str, int], list[str]]:
    name_to_canon = {cl.name: cl.canonical for cl in board.copper_layers}
    for cl in board.copper_layers:
        name_to_canon.setdefault(cl.canonical, cl.canonical)
    canon_to_idx = {cl.canonical: cl.index for cl in board.copper_layers}
    ordered = [cl.canonical for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    return name_to_canon, canon_to_idx, ordered


def _clearance_mm(board: Board) -> float:
    default = next((c for c in board.netclasses if c.name == "Default"), None)
    return max(
        0.12,
        float(board.rules.get("min_clearance") or 0.0),
        float(default.clearance if default and default.clearance else 0.0),
    )


def _track_line(track: dict) -> LineString | None:
    pts = track.get("points") or []
    if len(pts) < 2:
        return None
    if len(pts) == 2:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        if abs(x0 - x1) < 1e-9 and abs(y0 - y1) < 1e-9:
            return None
        return LineString([(float(x0), float(y0)), (float(x1), float(y1))])
    # 3-point arc: interpolate circle arc cleanly if non-collinear
    (x1, y1), (xm, ym), (x2, y2) = [(float(p[0]), float(p[1])) for p in pts[:3]]
    d = 2.0 * (x1 * (ym - y2) + xm * (y2 - y1) + x2 * (y1 - ym))
    if abs(d) < 1e-9:
        return LineString([(x1, y1), (xm, ym), (x2, y2)])
    ux = ((x1 * x1 + y1 * y1) * (ym - y2) + (xm * xm + ym * ym) * (y2 - y1) + (x2 * x2 + y2 * y2) * (y1 - ym)) / d
    uy = ((x1 * x1 + y1 * y1) * (x2 - xm) + (xm * xm + ym * ym) * (x1 - x2) + (x2 * x2 + y2 * y2) * (xm - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    if r <= 1e-6 or r > 1e5:
        return LineString([(x1, y1), (xm, ym), (x2, y2)])
    a1 = math.atan2(y1 - uy, x1 - ux)
    am = math.atan2(ym - uy, xm - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)

    def _ccw_delta(a: float, b: float) -> float:
        return (b - a) % (2.0 * math.pi)

    if _ccw_delta(a1, am) <= _ccw_delta(a1, a2):
        sweep = _ccw_delta(a1, a2)
    else:
        sweep = -_ccw_delta(a2, a1)
    steps = 8
    arc_pts = [(ux + r * math.cos(a1 + sweep * k / steps), uy + r * math.sin(a1 + sweep * k / steps))
               for k in range(steps + 1)]
    return LineString(arc_pts)


def _polygon_from_rings(rings: list[list[tuple[float, float]]]) -> Any:
    polys = []
    for ring in rings:
        if len(ring) >= 3:
            p = Polygon(ring)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-6:
                polys.append(p)
    if not polys:
        return None
    return unary_union(polys)


def _build_reference_planes(
    board: Board,
    name_to_canon: dict[str, str],
    canon_to_idx: dict[str, int],
    tracks: list[dict],
    vias: list[dict],
) -> dict[str, dict[str, Any]]:
    """Reconstruct effective filled reference copper polygons per canonical layer and net."""
    area = board.board_area_mm2 or 0.0
    outline = legality.board_polygon(board)
    clearance = _clearance_mm(board)
    role_plane_nets = {n for r in board.stackup.roles if r.role in ("plane", "mixed") for n in r.nets}

    raw_by_layer_net: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for zone in board.zones:
        net = zone.get("net")
        if not net or not (is_power_net(net) or is_ground_net(net) or net in role_plane_nets):
            continue
        filled_map = zone.get("filled_area_mm2") or {}
        poly = _polygon_from_rings(zone.get("outline") or [])
        if poly is None:
            continue
        for lname in zone.get("layers") or ():
            canon = name_to_canon.get(lname, lname)
            if canon not in canon_to_idx:
                continue
            f_area = float(filled_map.get(lname, 0.0))
            if f_area <= 0.0 and not zone.get("filled"):
                continue
            if area > 0 and f_area > 0 and f_area / area < ROLE_MIN_NET_FRACTION:
                continue
            raw_by_layer_net[canon][net].append(poly)

    # Pre-group tracks and via spans by canonical layer for fast void subtraction (cap_style=2/quad_segs=2).
    tracks_by_layer: dict[str, list[tuple[str, Any, tuple[float, float, float, float]]]] = defaultdict(list)
    for t in tracks:
        canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
        line = _track_line(t)
        if line is not None:
            w = max(0.05, float(t.get("width") or 0.15))
            g = line.buffer(0.5 * w + clearance, cap_style=2, quad_segs=2)
            tracks_by_layer[canon].append((t.get("net") or "", g, g.bounds))

    vias_by_layer: dict[str, list[tuple[str, Any, tuple[float, float, float, float]]]] = defaultdict(list)
    n_layers = len(board.copper_layers)
    idx_to_canon = {cl.index: cl.canonical for cl in board.copper_layers}
    for v in vias:
        vnet = v.get("net") or ""
        vlayers = v.get("layers") or []
        if len(vlayers) == 2:
            i0 = canon_to_idx.get(name_to_canon.get(vlayers[0], vlayers[0]), 0)
            i1 = canon_to_idx.get(name_to_canon.get(vlayers[1], vlayers[1]), n_layers - 1)
            lo, hi = min(i0, i1), max(i0, i1)
        else:
            lo, hi = 0, n_layers - 1
        rad = max(0.2, 0.5 * float(v.get("diameter") or 0.6)) + clearance
        disk = Point(float(v["x"]), float(v["y"])).buffer(rad, quad_segs=3)
        db = disk.bounds
        for li in range(lo, hi + 1):
            c = idx_to_canon.get(li)
            if c is not None:
                vias_by_layer[c].append((vnet, disk, db))

    planes: dict[str, dict[str, Any]] = {}
    for canon, by_net in raw_by_layer_net.items():
        layer_planes: dict[str, Any] = {}
        for net, polys in by_net.items():
            geom = unary_union(polys)
            if outline is not None:
                geom = geom.intersection(outline)
            if geom.is_empty or geom.area < 1e-4:
                continue
            gx0, gy0, gx1, gy1 = geom.bounds
            cutouts = [
                g for tnet, g, (x0, y0, x1, y1) in tracks_by_layer.get(canon, ())
                if tnet != net and x1 >= gx0 and x0 <= gx1 and y1 >= gy0 and y0 <= gy1
            ]
            cutouts += [
                g for vnet, g, (x0, y0, x1, y1) in vias_by_layer.get(canon, ())
                if vnet != net and x1 >= gx0 and x0 <= gx1 and y1 >= gy0 and y0 <= gy1
            ]
            if cutouts:
                voids = unary_union(cutouts)
                geom = geom.difference(voids)
            if not geom.is_empty and geom.area > 1e-4:
                layer_planes[net] = geom
        if layer_planes:
            planes[canon] = layer_planes
    return planes


def _intent_roles(intent: dict | None) -> tuple[dict[str, dict], set[str], set[str], set[str]]:
    parts = (intent or {}).get("parts") or {}
    nets = (intent or {}).get("nets") or {}
    diff_nets: set[str] = set()
    for pair in (intent or {}).get("diff_pairs") or ():
        val = pair.get("value") if isinstance(pair, dict) and "value" in pair else pair
        if isinstance(val, dict):
            for k in ("positive", "negative"):
                if val.get(k):
                    diff_nets.add(str(val[k]))
    fast_nets: set[str] = set(diff_nets)
    sw_nets: set[str] = set()
    fb_nets: set[str] = set()
    for net_name, meta in nets.items():
        cls_claim = meta.get("class") if isinstance(meta, dict) else None
        if cls_claim and not model.is_uncertain(cls_claim):
            cls_val = str(cls_claim.get("value") or "").lower()
            if cls_val in _FAST_NET_CLASSES:
                fast_nets.add(net_name)
            if cls_val == "sw_node":
                sw_nets.add(net_name)
            if cls_val in ("feedback", "converter_feedback", "fb"):
                fb_nets.add(net_name)
    return parts, fast_nets, sw_nets, fb_nets


def _copper_graph_for_net(
    board: Board,
    net: str,
    tracks: list[dict],
    vias: list[dict],
    name_to_canon: dict[str, str],
) -> tuple[list[tuple[float, float, str, str]], dict[int, list[tuple[int, float, bool]]]]:
    """Build node graph for one net: nodes are (x, y, layer_or_ALL, kind), edges are (neighbor, len_mm, is_via)."""
    nodes: list[tuple[float, float, str, str]] = []
    adj: dict[int, list[tuple[int, float, bool]]] = defaultdict(list)

    def add_node(x: float, y: float, layer: str, kind: str) -> int:
        idx = len(nodes)
        nodes.append((x, y, layer, kind))
        return idx

    # Track segments
    for t in tracks:
        if (t.get("net") or "") != net:
            continue
        pts = t.get("points") or []
        if len(pts) < 2:
            continue
        canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
        line = _track_line(t)
        seg_len = float(line.length) if line is not None else math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
        u = add_node(float(pts[0][0]), float(pts[0][1]), canon, "track")
        v = add_node(float(pts[-1][0]), float(pts[-1][1]), canon, "track")
        adj[u].append((v, seg_len, False))
        adj[v].append((u, seg_len, False))

    # Vias
    for v in vias:
        if (v.get("net") or "") != net:
            continue
        add_node(float(v["x"]), float(v["y"]), "ALL", "via")

    # Connect coincident endpoints (within 0.12 mm) on compatible layers using spatial buckets (O(n))
    tol = 0.12
    bucket_sz = 0.25
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (xi, yi, li, ki) in enumerate(nodes):
        bx, by = math.floor(xi / bucket_sz), math.floor(yi / bucket_sz)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((bx + dx, by + dy), ()):
                    xj, yj, lj, kj = nodes[j]
                    if abs(xi - xj) > tol or abs(yi - yj) > tol:
                        continue
                    d = math.hypot(xi - xj, yi - yj)
                    if d > tol:
                        continue
                    if li == lj or li == "ALL" or lj == "ALL":
                        is_via = ki == "via" or kj == "via"
                        adj[i].append((j, d, is_via))
                        adj[j].append((i, d, is_via))
        buckets[(bx, by)].append(i)
    return nodes, adj


def _shortest_copper_path(
    nodes: list[tuple[float, float, str, str]],
    adj: dict[int, list[tuple[int, float, bool]]],
    src_pts: list[tuple[float, float, set[str], float]],
    dst_pts: list[tuple[float, float, set[str], float]],
) -> tuple[float | None, bool]:
    """Dijkstra shortest copper length (mm) and whether path traverses a via."""
    if not nodes or not src_pts or not dst_pts:
        return None, False
    srcs: list[int] = []
    dsts: set[int] = set()
    for idx, (nx, ny, nlayer, _kind) in enumerate(nodes):
        for sx, sy, slayers, srad in src_pts:
            if (nlayer == "ALL" or nlayer in slayers) and math.hypot(nx - sx, ny - sy) <= srad:
                srcs.append(idx)
                break
        for dx, dy, dlayers, drad in dst_pts:
            if (nlayer == "ALL" or nlayer in dlayers) and math.hypot(nx - dx, ny - dy) <= drad:
                dsts.add(idx)
                break
    if not srcs or not dsts:
        return None, False
    # State: (dist, used_via_int, node)
    pq: list[tuple[float, int, int]] = [(0.0, 1 if nodes[s][3] == "via" else 0, s) for s in srcs]
    heapq.heapify(pq)
    best: dict[tuple[int, int], float] = {}
    while pq:
        dist, used_via, u = heapq.heappop(pq)
        if u in dsts:
            return dist, bool(used_via)
        state = (u, used_via)
        if dist > best.get(state, math.inf):
            continue
        for nxt, w, edge_via in adj.get(u, ()):
            nv = 1 if (used_via or edge_via or nodes[nxt][3] == "via") else 0
            nd = dist + w
            if nd < best.get((nxt, nv), math.inf):
                best[(nxt, nv)] = nd
                heapq.heappush(pq, (nd, nv, nxt))
    return None, False


def _pad_match_specs(
    fp: Footprint,
    net: str,
    name_to_canon: dict[str, str],
    allowed_numbers: set[str] | None = None,
) -> list[tuple[float, float, set[str], float]]:
    out = []
    for pad, (px, py) in fp.pads_world():
        if pad.net != net:
            continue
        if allowed_numbers and str(pad.number) not in allowed_numbers:
            continue
        layers = {name_to_canon.get(lyr, lyr) for lyr in pad.layers}
        rad = max(0.25, 0.5 * max(pad.size[0], pad.size[1]) + 0.1)
        out.append((px, py, layers, rad))
    return out


def audit(board: Board, intent: dict | None = None) -> dict[str, Any]:
    """Run all architecture 8.3 electrical & return-path lint rules on ``board``."""
    tracks = list((board.routing or {}).get("tracks") or [])
    vias = list((board.routing or {}).get("vias") or [])
    has_copper = bool(tracks)
    name_to_canon, canon_to_idx, ordered_layers = _layer_maps(board)
    n_layers = len(ordered_layers)
    parts_intent, fast_nets, sw_nets, fb_nets = _intent_roles(intent)
    for dp in board.diff_pairs:
        fast_nets.add(dp.positive)
        fast_nets.add(dp.negative)

    by_ref = {fp.ref: fp for fp in board.footprints}
    findings: list[dict[str, Any]] = []

    def add_finding(rule: str, severity: str, detail: str, *, net: str | None = None,
                    refs: list[str] | None = None, location_mm: tuple[float, float] | None = None,
                    value: float | None = None, limit: float | None = None) -> None:
        findings.append({
            "rule": rule,
            "severity": severity,
            "net": net,
            "refs": sorted(refs or []),
            "location_mm": [round(location_mm[0], 3), round(location_mm[1], 3)] if location_mm else None,
            "value": round(value, 4) if value is not None else None,
            "limit": round(limit, 4) if limit is not None else None,
            "detail": detail,
        })

    # Reconstruct effective reference copper planes on layers that have filled zones
    ref_planes = _build_reference_planes(board, name_to_canon, canon_to_idx, tracks, vias)
    ref_union_by_layer = {lyr: unary_union(list(nets_map.values())) for lyr, nets_map in ref_planes.items()}
    plane_nets = {net for nets_map in ref_planes.values() for net in nets_map}

    # Pre-index tracks by net (for copper graphs, crystal, FB/SW, ESD)
    tracks_by_net: dict[str, list[dict]] = defaultdict(list)
    for t in tracks:
        if t.get("net"):
            tracks_by_net[t["net"]].append(t)
    gnd_vias = [(float(v["x"]), float(v["y"])) for v in vias if is_ground_net(v.get("net"))]
    vias_by_net: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for v in vias:
        if v.get("net"):
            vias_by_net[v["net"]].append((float(v["x"]), float(v["y"])))

    # Identify crystals, converters, inductors, ESD, and decaps from intent
    crystals: list[Footprint] = []
    inductors: list[Footprint] = []
    converters: list[Footprint] = []
    esds: list[tuple[Footprint, dict]] = []
    decaps: list[tuple[Footprint, dict]] = []
    stitch_caps: list[tuple[Footprint, set[str]]] = []

    for ref, meta in sorted(parts_intent.items()):
        fp = by_ref.get(ref)
        if fp is None:
            continue
        role_claim = meta.get("role")
        if not role_claim or model.is_uncertain(role_claim):
            continue
        role = str(role_claim.get("value") or "")
        if role == "crystal":
            crystals.append(fp)
            fast_nets.update(n for n in fp.nets if not is_power_net(n))
        elif role == "converter_inductor":
            inductors.append(fp)
            for n in fp.nets:
                if not is_power_net(n) and not is_ground_net(n):
                    sw_nets.add(n)
        elif role == "converter_ic":
            converters.append(fp)
        elif role == "converter_control":
            for n in fp.nets:
                if not is_power_net(n) and not is_ground_net(n) and n not in sw_nets:
                    fb_nets.add(n)
        elif role == "esd":
            esds.append((fp, meta))
        elif role == "decap":
            decaps.append((fp, meta))
            stitch_caps.append((fp, set(fp.nets)))
        elif role in ("bulk_cap", "regulator_input_cap", "regulator_output_cap"):
            stitch_caps.append((fp, set(fp.nets)))

    # ------------------------------------------------------------------
    # Rule 1: decap_loop (real copper IC supply pin -> decap pad, no intervening via)
    # ------------------------------------------------------------------
    decap_checked = 0
    decap_via_between = 0
    decap_lengths: list[float] = []
    net_graph_cache: dict[str, tuple[list, dict]] = {}

    def get_net_graph(net_name: str) -> tuple[list, dict]:
        g = net_graph_cache.get(net_name)
        if g is None:
            g = net_graph_cache[net_name] = _copper_graph_for_net(board, net_name, tracks, vias, name_to_canon)
        return g

    if has_copper:
        for fp, meta in decaps:
            t_claim = meta.get("target")
            if not t_claim or model.is_uncertain(t_claim):
                continue
            target = t_claim.get("value") or {}
            t_ref, net = target.get("ref"), target.get("net")
            if not t_ref or not net or t_ref not in by_ref or is_ground_net(net):
                continue
            ic = by_ref[t_ref]
            # When the supply rail has an internal power plane, decap and IC connect via the plane
            # directly (low-inductance vertical path) rather than winding along outer-layer stubs.
            has_inner_plane = any(
                net in ref_planes.get(lyr, {})
                for idx_l, lyr in enumerate(ordered_layers)
                if 0 < idx_l < n_layers - 1
            )
            if has_inner_plane:
                continue
            nums = {str(x) for x in (target.get("pins") or ())} or None
            src_specs = _pad_match_specs(fp, net, name_to_canon)
            dst_specs = _pad_match_specs(ic, net, name_to_canon, nums)
            if not src_specs or not dst_specs:
                continue
            decap_checked += 1
            nodes, adj = get_net_graph(net)
            path_mm, used_via = _shortest_copper_path(nodes, adj, src_specs, dst_specs)
            if path_mm is None:
                continue
            decap_lengths.append(path_mm)
            same_side = fp.side == ic.side
            if used_via and same_side:
                decap_via_between += 1
                add_finding(
                    "decap_loop", "warning",
                    f"via lies between {t_ref} supply pin and same-side decap {fp.ref} on {net}",
                    net=net, refs=[fp.ref, t_ref], location_mm=fp.position, value=path_mm, limit=DECAP_COPPER_WARN_MM,
                )
            if path_mm > DECAP_COPPER_CRIT_MM:
                add_finding(
                    "decap_loop", "critical",
                    f"decap copper path {fp.ref}->{t_ref} on {net} is {path_mm:.2f} mm (> {DECAP_COPPER_CRIT_MM} mm)",
                    net=net, refs=[fp.ref, t_ref], location_mm=fp.position, value=path_mm, limit=DECAP_COPPER_CRIT_MM,
                )
            elif path_mm > DECAP_COPPER_WARN_MM:
                add_finding(
                    "decap_loop", "warning",
                    f"decap copper path {fp.ref}->{t_ref} on {net} is {path_mm:.2f} mm (> {DECAP_COPPER_WARN_MM} mm)",
                    net=net, refs=[fp.ref, t_ref], location_mm=fp.position, value=path_mm, limit=DECAP_COPPER_WARN_MM,
                )

    # ------------------------------------------------------------------
    # Rule 2: reference_continuity & split crossings + F5 escape plane perforation
    # ------------------------------------------------------------------
    adj_ref_layers: dict[str, list[str]] = {}
    for idx, canon in enumerate(ordered_layers):
        neigh = []
        if idx > 0 and ordered_layers[idx - 1] in ref_union_by_layer:
            neigh.append(ordered_layers[idx - 1])
        if idx + 1 < n_layers and ordered_layers[idx + 1] in ref_union_by_layer:
            neigh.append(ordered_layers[idx + 1])
        if neigh:
            adj_ref_layers[canon] = neigh

    adj_ref_union = {
        lyr: unary_union([ref_union_by_layer[r] for r in refs])
        for lyr, refs in adj_ref_layers.items()
    }
    adj_ref_prep = {lyr: prep(geom) for lyr, geom in adj_ref_union.items()}

    total_sig_len = 0.0
    covered_sig_len = 0.0
    fast_sig_len = 0.0
    fast_covered_len = 0.0
    split_crossings = 0
    # Deduplicate split findings per (net, layer) so one split doesn't spam multiple segments.
    worst_split: dict[tuple[str, str], tuple[float, float, tuple[float, float], float]] = {}

    if has_copper and adj_ref_union:
        for t in tracks:
            net = t.get("net") or ""
            if not net or is_power_net(net) or net in plane_nets:
                continue
            canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
            ref_poly = adj_ref_union.get(canon)
            if ref_poly is None:
                continue
            line = _track_line(t)
            if line is None or line.length < 0.05:
                continue
            seg_len = float(line.length)
            if adj_ref_prep[canon].covers(line):
                cov = seg_len
            else:
                cov = float(line.intersection(ref_poly).length)
            total_sig_len += seg_len
            covered_sig_len += cov
            if net in fast_nets:
                fast_sig_len += seg_len
                fast_covered_len += cov
                uncovered = seg_len - cov
                width = max(0.1, float(t.get("width") or 0.15))
                # On 2-layer boards (no dedicated inner plane), perpendicular crossings over B.Cu tracks
                # create unavoidable narrow (~0.4-0.6 mm) gaps; only flag wider return gaps as warnings.
                gap_limit = max(1.0, 4.0 * width) if n_layers <= 2 else max(0.35, 2.5 * width)
                if uncovered > gap_limit:
                    diff_geom = line.difference(ref_poly)
                    pieces = (
                        [diff_geom] if diff_geom.geom_type == "LineString"
                        else [g for g in getattr(diff_geom, "geoms", ()) if g.geom_type == "LineString"]
                    )
                    p_start = Point(line.coords[0])
                    p_end = Point(line.coords[-1])
                    for piece in pieces:
                        plen = float(piece.length)
                        if plen <= gap_limit:
                            continue
                        # Ignore tiny endpoint pad/via antipad escapes; flag interior splits or long gaps.
                        at_endpoint = piece.distance(p_start) < 0.05 or piece.distance(p_end) < 0.05
                        if at_endpoint and plen < max(1.0, 4.0 * width):
                            continue
                        mid = piece.interpolate(0.5, normalized=True)
                        key = (net, canon)
                        prev = worst_split.get(key)
                        if prev is None or plen > prev[0]:
                            worst_split[key] = (plen, cov / seg_len, (mid.x, mid.y), gap_limit)

    for (net, canon), (plen, cov_ratio, loc, gap_limit) in sorted(worst_split.items()):
        split_crossings += 1
        sev = "warning" if n_layers <= 2 else "critical"
        rule_name = "two_layer_return_gap" if n_layers <= 2 else "reference_split_crossing"
        add_finding(
            rule_name, sev,
            f"fast/critical net {net} on {canon} crosses a reference plane gap "
            f"({plen:.2f} mm uncovered, {cov_ratio:.0%} segment covered)",
            net=net, location_mm=loc, value=plen, limit=gap_limit,
        )

    # Plan 4.2 F5: plane continuity under dense active-IC escape regions (>= 16 pads, excluding connectors/mechanical)
    escape_perforations = 0
    if has_copper and ref_union_by_layer and n_layers >= 4:
        outline = legality.board_polygon(board)
        for fp in board.footprints:
            if len(fp.pads) < 16:
                continue
            role_v = ((parts_intent.get(fp.ref, {}).get("role") or {}).get("value")) or ""
            if role_v in ("connector", "mechanical", "test_point", "switch") or fp.prefix in ("J", "P", "CN", "CON", "USB", "BUS"):
                continue
            cy = legality.courtyard_polygon(fp)
            if cy is None or cy.area < 4.0:
                continue
            if outline is not None:
                cy = cy.intersection(outline)
            if cy.is_empty or cy.area < 4.0:
                continue
            side_canon = "F.Cu" if fp.side == "F" else "B.Cu"
            for r_lyr in adj_ref_layers.get(side_canon, ()):
                r_poly = ref_union_by_layer.get(r_lyr)
                if r_poly is None:
                    continue
                filled_frac = float(cy.intersection(r_poly).area) / float(cy.area)
                void_frac = 1.0 - filled_frac
                if void_frac > ESCAPE_PERFORATION_LIMIT:
                    escape_perforations += 1
                    add_finding(
                        "plane_perforation", "warning",
                        f"reference plane {r_lyr} under {fp.ref} escape region is {void_frac:.0%} perforated "
                        f"(> {ESCAPE_PERFORATION_LIMIT:.0%})",
                        refs=[fp.ref], location_mm=fp.position, value=void_frac, limit=ESCAPE_PERFORATION_LIMIT,
                    )

    # ------------------------------------------------------------------
    # Rule 3: return_via (layer transitions: <= 1.5 mm return via or <= 3.0 mm stitch cap)
    # ------------------------------------------------------------------
    transitions_checked = 0
    transitions_missing_return = 0
    transitions_need_stitch = 0
    if has_copper and n_layers >= 2 and ref_planes:
        # Map each signal via coordinate to the copper layers of tracks touching it on its net.
        tracks_by_net_layer: dict[str, list[tuple[str, float, float, float, float]]] = defaultdict(list)
        for t in tracks:
            net = t.get("net") or ""
            pts = t.get("points") or []
            if not net or len(pts) < 2 or is_power_net(net) or net in plane_nets:
                continue
            canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
            tracks_by_net_layer[net].append((canon, float(pts[0][0]), float(pts[0][1]), float(pts[-1][0]), float(pts[-1][1])))

        def _dominant_ref_net(layer_canon: str) -> str | None:
            cand_nets = {}
            for r_lyr in adj_ref_layers.get(layer_canon, ()):
                for rn, geom in ref_planes.get(r_lyr, {}).items():
                    cand_nets[rn] = cand_nets.get(rn, 0.0) + float(geom.area)
            if not cand_nets:
                return None
            # Prefer GND if present with significant area
            gnd_best = max(((a, n) for n, a in cand_nets.items() if is_ground_net(n)), default=None)
            if gnd_best is not None:
                return gnd_best[1]
            return max(cand_nets.items(), key=lambda kv: (kv[1], kv[0]))[0]

        for v in vias:
            net = v.get("net") or ""
            if not net or is_power_net(net) or net in plane_nets:
                continue
            vx, vy = float(v["x"]), float(v["y"])
            touched = set()
            for canon, x0, y0, x1, y1 in tracks_by_net_layer.get(net, ()):
                if math.hypot(vx - x0, vy - y0) <= 0.25 or math.hypot(vx - x1, vy - y1) <= 0.25:
                    touched.add(canon)
            if len(touched) < 2:
                continue
            ref_nets = {rn for lyr in touched if (rn := _dominant_ref_net(lyr)) is not None}
            if not ref_nets:
                continue
            transitions_checked += 1
            if len(ref_nets) == 1:
                rn = next(iter(ref_nets))
                ref_vias = vias_by_net.get(rn) or gnd_vias
                nearest = min((math.hypot(vx - gx, vy - gy) for gx, gy in ref_vias), default=math.inf)
                if nearest > RETURN_VIA_MAX_MM:
                    transitions_missing_return += 1
                    if net in fast_nets:
                        add_finding(
                            "return_via", "warning",
                            f"layer transition via on {net} is {nearest:.2f} mm from nearest {rn} return via "
                            f"(> {RETURN_VIA_MAX_MM} mm)",
                            net=net, location_mm=(vx, vy), value=nearest if math.isfinite(nearest) else None,
                            limit=RETURN_VIA_MAX_MM,
                        )
            else:
                # Reference plane changed between different nets (e.g., GND <-> PWR): check stitching cap
                has_stitch = any(
                    ref_nets <= c_nets and math.hypot(vx - c_fp.x, vy - c_fp.y) <= STITCH_CAP_MAX_MM
                    for c_fp, c_nets in stitch_caps
                )
                if not has_stitch:
                    transitions_need_stitch += 1
                    if net in fast_nets:
                        add_finding(
                            "reference_change_stitch_cap", "warning",
                            f"signal via on {net} changes reference plane ({'/'.join(sorted(ref_nets))}) "
                            f"without a stitching capacitor within {STITCH_CAP_MAX_MM} mm",
                            net=net, location_mm=(vx, vy), limit=STITCH_CAP_MAX_MM,
                        )

    # ------------------------------------------------------------------
    # Rule 4: crystal_keepout (unrelated signal copper under crystal body)
    # ------------------------------------------------------------------
    crystal_violations = 0
    if has_copper and crystals:
        for xtal in crystals:
            cy = legality.courtyard_polygon(xtal)
            if cy is None or cy.is_empty:
                continue
            inner = cy.buffer(-0.2)
            body = inner if (not inner.is_empty and inner.area >= 0.5) else cy
            body_prep = prep(body)
            xtal_idx = 0 if xtal.side == "F" else n_layers - 1
            # Layers shielded by a solid GND plane between xtal and the layer are exempt.
            unshielded: set[str] = {ordered_layers[xtal_idx]}
            step = 1 if xtal_idx == 0 else -1
            cur = xtal_idx + step
            while 0 <= cur < n_layers:
                lyr = ordered_layers[cur]
                has_gnd_shield = any(
                    is_ground_net(rn) and float(cy.intersection(g).area) >= 0.8 * float(cy.area)
                    for rn, g in ref_planes.get(lyr, {}).items()
                )
                if has_gnd_shield:
                    break
                unshielded.add(lyr)
                cur += step
            allowed_nets = set(xtal.nets)
            seen_xtal: set[tuple[str, str]] = set()
            for t in tracks:
                net = t.get("net") or ""
                if not net or net in allowed_nets or is_ground_net(net):
                    continue
                canon = name_to_canon.get(t.get("layer", ""), t.get("layer", ""))
                if canon not in unshielded or (net, canon) in seen_xtal:
                    continue
                line = _track_line(t)
                if line is not None and body_prep.intersects(line) and line.intersection(body).length > 0.15:
                    seen_xtal.add((net, canon))
                    crystal_violations += 1
                    mid = line.intersection(body).centroid
                    is_dc_rail = is_power_net(net) and net not in sw_nets
                    add_finding(
                        "signal_under_crystal", "warning" if is_dc_rail else "critical",
                        f"unrelated {'power rail' if is_dc_rail else 'signal'} {net} on {canon} "
                        f"passes inside crystal {xtal.ref} keepout",
                        net=net, refs=[xtal.ref], location_mm=(mid.x, mid.y),
                    )

    # ------------------------------------------------------------------
    # Rule 5: fb_vs_sw (FB distance from SW and not passing under converter inductor)
    # ------------------------------------------------------------------
    fb_sw_min_mm: float | None = None
    fb_under_inductor = 0
    if has_copper and fb_nets:
        fb_lines = [
            (t.get("net"), name_to_canon.get(t.get("layer", ""), t.get("layer", "")), line)
            for n in fb_nets for t in tracks_by_net.get(n, ())
            if (line := _track_line(t)) is not None
        ]
        sw_lines = [
            (t.get("net"), line)
            for n in sw_nets for t in tracks_by_net.get(n, ())
            if (line := _track_line(t)) is not None
        ]
        if fb_lines and sw_lines:
            best_d = math.inf
            best_pair = None
            for fnet, _flyr, fline in fb_lines:
                for snet, sline in sw_lines:
                    d = float(fline.distance(sline))
                    if d < best_d:
                        best_d = d
                        best_pair = (fnet, snet, fline)
            if math.isfinite(best_d) and best_pair is not None:
                fb_sw_min_mm = best_d
                fnet, snet, fline = best_pair
                mid = fline.interpolate(0.5, normalized=True)
                if best_d < FB_SW_CRIT_MM:
                    add_finding(
                        "fb_near_sw", "critical",
                        f"feedback net {fnet} runs {best_d:.2f} mm from switching node {snet} (< {FB_SW_CRIT_MM} mm)",
                        net=fnet, location_mm=(mid.x, mid.y), value=best_d, limit=FB_SW_CRIT_MM,
                    )
                elif best_d < FB_SW_WARN_MM:
                    add_finding(
                        "fb_near_sw", "warning",
                        f"feedback net {fnet} runs {best_d:.2f} mm from switching node {snet} (< {FB_SW_WARN_MM} mm)",
                        net=fnet, location_mm=(mid.x, mid.y), value=best_d, limit=FB_SW_WARN_MM,
                    )
        for ind in inductors:
            icy = legality.courtyard_polygon(ind)
            if icy is None or icy.is_empty:
                continue
            for fnet, flyr, fline in fb_lines:
                if fline.intersects(icy) and fline.intersection(icy).length > 0.1:
                    fb_under_inductor += 1
                    mid = fline.intersection(icy).centroid
                    add_finding(
                        "fb_under_inductor", "critical",
                        f"feedback net {fnet} on {flyr} passes beneath converter inductor {ind.ref}",
                        net=fnet, refs=[ind.ref], location_mm=(mid.x, mid.y),
                    )

    # ------------------------------------------------------------------
    # Rule 6: buck_hot_loop (convex hull area of Cin <-> converter IC <-> GND)
    # ------------------------------------------------------------------
    hot_loops: list[dict[str, Any]] = []
    for conv in converters:
        cin_fps = []
        for ref, meta in parts_intent.items():
            rc = meta.get("role")
            tc = meta.get("target")
            if not rc or model.is_uncertain(rc) or not tc or model.is_uncertain(tc):
                continue
            if (
                rc.get("value") in ("regulator_input_cap", "decap")
                and (tc.get("value") or {}).get("ref") == conv.ref
                and ref in by_ref
            ):
                cin_fps.append(by_ref[ref])
        if not cin_fps:
            continue
        pts: list[tuple[float, float]] = []
        for cin in cin_fps:
            for pad, pt in cin.pads_world():
                if pad.net:
                    pts.append(pt)
        shared_nets = {n for cin in cin_fps for n in cin.nets}
        for pad, pt in conv.pads_world():
            if pad.net in shared_nets or pad.net in sw_nets:
                pts.append(pt)
        if len(pts) >= 3:
            loop_area = float(MultiPoint(pts).convex_hull.area)
            hot_loops.append({"converter": conv.ref, "area_mm2": round(loop_area, 2),
                              "input_caps": sorted(c.ref for c in cin_fps)})
            if has_copper and loop_area > HOT_LOOP_CRIT_MM2:
                add_finding(
                    "buck_hot_loop", "critical",
                    f"converter {conv.ref} hot loop area is {loop_area:.1f} mm^2 (> {HOT_LOOP_CRIT_MM2} mm^2)",
                    refs=[conv.ref, *(c.ref for c in cin_fps)], location_mm=conv.position,
                    value=loop_area, limit=HOT_LOOP_CRIT_MM2,
                )
            elif has_copper and loop_area > HOT_LOOP_WARN_MM2:
                add_finding(
                    "buck_hot_loop", "warning",
                    f"converter {conv.ref} hot loop area is {loop_area:.1f} mm^2 (> {HOT_LOOP_WARN_MM2} mm^2)",
                    refs=[conv.ref, *(c.ref for c in cin_fps)], location_mm=conv.position,
                    value=loop_area, limit=HOT_LOOP_WARN_MM2,
                )

    # ------------------------------------------------------------------
    # Rule 7: esd_to_connector (path length from connector pin to ESD device)
    # ------------------------------------------------------------------
    esd_distances: list[dict[str, Any]] = []
    for esd_fp, meta in esds:
        tc = meta.get("target")
        conn_ref = (tc.get("value") or {}).get("ref") if tc and not model.is_uncertain(tc) else None
        if not conn_ref or conn_ref not in by_ref:
            continue
        conn = by_ref[conn_ref]
        shared = sorted((esd_fp.nets & conn.nets) - {n for n in esd_fp.nets if is_power_net(n)})
        if not shared:
            continue
        for net in shared:
            src_specs = _pad_match_specs(conn, net, name_to_canon)
            dst_specs = _pad_match_specs(esd_fp, net, name_to_canon)
            if not src_specs or not dst_specs:
                continue
            dist_mm: float | None = None
            if has_copper:
                nodes, adj = get_net_graph(net)
                dist_mm, _ = _shortest_copper_path(nodes, adj, src_specs, dst_specs)
            if dist_mm is None:
                dist_mm = min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in src_specs for b in dst_specs)
            esd_distances.append({"esd": esd_fp.ref, "connector": conn_ref, "net": net, "distance_mm": round(dist_mm, 3)})
            if has_copper and dist_mm > ESD_CONN_HIGH_MM:
                add_finding(
                    "esd_to_connector", "warning",
                    f"ESD {esd_fp.ref} is {dist_mm:.2f} mm along {net} from connector {conn_ref} (> {ESD_CONN_HIGH_MM} mm)",
                    net=net, refs=[esd_fp.ref, conn_ref], location_mm=esd_fp.position,
                    value=dist_mm, limit=ESD_CONN_HIGH_MM,
                )
            elif has_copper and dist_mm > ESD_CONN_WARN_MM:
                add_finding(
                    "esd_to_connector", "warning",
                    f"ESD {esd_fp.ref} is {dist_mm:.2f} mm along {net} from connector {conn_ref} (> {ESD_CONN_WARN_MM} mm)",
                    net=net, refs=[esd_fp.ref, conn_ref], location_mm=esd_fp.position,
                    value=dist_mm, limit=ESD_CONN_WARN_MM,
                )

    # ------------------------------------------------------------------
    # Rule 8: two_layer_gnd_continuity (2-layer B.Cu GND continuity & island count)
    # ------------------------------------------------------------------
    two_layer_gnd: dict[str, Any] = {"applicable": False}
    if n_layers == 2 and has_copper:
        b_planes = ref_planes.get("B.Cu") or {}
        gnd_polys = [g for n, g in b_planes.items() if is_ground_net(n)]
        if gnd_polys:
            gnd_geom = unary_union(gnd_polys)
            pieces = (
                [gnd_geom] if gnd_geom.geom_type == "Polygon"
                else [p for p in getattr(gnd_geom, "geoms", ()) if p.geom_type == "Polygon"]
            )
            sig_pieces = sorted((float(p.area) for p in pieces if p.area >= MIN_ISLAND_AREA_MM2), reverse=True)
            total_gnd = sum(sig_pieces)
            board_area = board.board_area_mm2 or max(total_gnd, 1.0)
            largest_frac = (sig_pieces[0] / total_gnd) if total_gnd > 0 else 0.0
            two_layer_gnd = {
                "applicable": True,
                "layer": "B.Cu",
                "gnd_fill_fraction": round(total_gnd / board_area, 4),
                "largest_island_fraction": round(largest_frac, 4),
                "island_count": len(sig_pieces),
            }
            if largest_frac < 0.75:
                add_finding(
                    "two_layer_gnd_fragmented", "warning",
                    f"2-layer B.Cu GND pour is fragmented into {len(sig_pieces)} islands "
                    f"(largest holds {largest_frac:.0%})",
                    value=largest_frac, limit=0.75,
                )

    # Deterministic finding order
    sev_order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (sev_order.get(f["severity"], 9), f["rule"], f["net"] or "", tuple(f["refs"])))
    critical_count = sum(1 for f in findings if f["severity"] == "critical")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")

    # ------------------------------------------------------------------
    # Rule 9: Lint Score (0..100 on real copper; None when unrouted)
    # ------------------------------------------------------------------
    score: float | None = None
    ref_cov = (covered_sig_len / total_sig_len) if total_sig_len > 0 else None
    fast_cov = (fast_covered_len / fast_sig_len) if fast_sig_len > 0 else None
    if has_copper:
        s = 100.0
        s -= min(60.0, 20.0 * critical_count)
        s -= min(30.0, 4.0 * warning_count)
        if ref_cov is not None and ref_cov < 0.90:
            s -= min(20.0, (0.90 - ref_cov) * 40.0)
        if two_layer_gnd.get("applicable"):
            lf = float(two_layer_gnd["largest_island_fraction"])
            if lf < 0.85:
                s -= min(15.0, (0.85 - lf) * 30.0)
        score = round(max(0.0, min(100.0, s)), 2)

    return {
        "available": True,
        "has_copper": has_copper,
        "score": score,
        "critical_errors": critical_count,
        "warnings": warning_count,
        "checks": {
            "decap_loop": {
                "checked": decap_checked,
                "via_between_count": decap_via_between,
                "max_copper_mm": round(max(decap_lengths), 3) if decap_lengths else None,
                "median_copper_mm": round(sorted(decap_lengths)[len(decap_lengths) // 2], 3) if decap_lengths else None,
            },
            "reference_continuity": {
                "signal_length_mm": round(total_sig_len, 2),
                "covered_fraction": round(ref_cov, 4) if ref_cov is not None else None,
                "fast_covered_fraction": round(fast_cov, 4) if fast_cov is not None else None,
                "split_crossings": split_crossings,
                "escape_perforations": escape_perforations,
            },
            "return_via": {
                "transitions_checked": transitions_checked,
                "missing_return_via": transitions_missing_return,
                "missing_stitch_cap": transitions_need_stitch,
                "limit_mm": RETURN_VIA_MAX_MM,
            },
            "crystal_keepout": {
                "crystals": len(crystals),
                "violations": crystal_violations,
            },
            "fb_vs_sw": {
                "min_distance_mm": round(fb_sw_min_mm, 3) if fb_sw_min_mm is not None else None,
                "under_inductor": fb_under_inductor,
            },
            "buck_hot_loop": hot_loops,
            "esd_to_connector": esd_distances,
            "two_layer_gnd_continuity": two_layer_gnd,
        },
        "findings": findings,
    }
