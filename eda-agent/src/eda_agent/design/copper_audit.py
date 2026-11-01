# SPDX-License-Identifier: MIT
"""Offline, evidence-bearing SI/PI checks against actual 2-D board copper.

Policy distances are user-adjustable engineering heuristics, not a claim of
compliance with a standard. Missing geometry/intent always remains unknown;
an empty violation list alone never means a clean board.
"""
from __future__ import annotations

import math
from collections import defaultdict

import networkx as nx
from eda_agent.design.net_classes import classify_copper_intent
from eda_agent.units import MM_PER_MIL

from eda_agent.design.copper_geometry import (
    EPS, SpatialIndex, arc_segments, bbox_segment, distance, lerp,
    merge_intervals, pad_outline, parallel_coupling, point_in_region,
    point_in_ring, point_segment_distance, polygon_area, project, ring_edges,
    segment_distance, segment_intersection_parameters, segment_region_intervals, uncovered_intervals, xy,
    region_geometry_valid,
)

DEFAULT_POLICY = {
    "general_spacing_widths": 3.0, "sensitive_spacing_widths": 5.0,
    "minimum_coupled_length_mm": 0.5, "parallel_angle_deg": 5.0,
    "return_via_radius_mm": 1.5, "decoupling_path_limit_mm": 3.0,
    "arc_error_mm": 0.002, "maximum_switch_loop_area_mm2": None,
}
FAST = {"HIGH_SPEED_DIFF", "HIGH_SPEED_SINGLE"}


def _tags(intent, net):
    value = intent.get("net_classes", {}).get(net, [])
    return {value} if isinstance(value, str) else set(value)


def _unknown(report, check, reason, **evidence):
    item = {"check": check, "reason": reason, "evidence": evidence}
    if item not in report["unknowns"]:
        report["unknowns"].append(item)


def _violation(report, check, item, measured, limit, fix, evidence, point=None):
    point = point or xy(item.get("start", item))
    report["violations"].append({
        "id": f"{check}:{len(report['violations']) + 1}", "check": check,
        "x_mm": round(point[0], 8), "y_mm": round(point[1], 8),
        "layer": item.get("layer", item.get("start_layer", "unknown")),
        "net_name": item.get("net_name", ""), "measured_value": measured,
        "limit_value": limit, "fix_action": fix, "evidence": evidence,
    })


def _coverage_complete(snapshot, sections):
    coverage = snapshot.get("coverage", {})
    if isinstance(coverage, dict) and "sections" in coverage:
        coverage = coverage["sections"]
    missing = snapshot.get("missing", [])
    if any(str(section).lower() in str(m).lower() for section in sections for m in missing):
        return False
    def ok(value):
        if isinstance(value, dict):
            return value.get("complete") is True or value.get("status") == "complete"
        return value is True or value == "complete"
    return all(ok(coverage.get(section)) for section in sections)


def _segments(snapshot, report=None, error=0.002):
    out = []
    for i, raw in enumerate(snapshot.get("tracks", [])):
        if "start" not in raw or "end" not in raw or float(raw.get("width_mm", 0)) <= 0:
            if report is not None:
                _unknown(report, "geometry", "track endpoints/width unavailable", id=raw.get("id", i))
            continue
        out.append({**raw, "id": str(raw.get("id", f"track:{i}")), "start": xy(raw["start"]), "end": xy(raw["end"])})
    for i, raw in enumerate(snapshot.get("arcs", [])):
        try:
            if float(raw.get("width_mm", 0)) <= 0:
                raise ValueError("arc copper width unavailable")
            out.extend(arc_segments({**raw, "id": str(raw.get("id", f"arc:{i}"))}, error))
        except (ValueError, KeyError, TypeError) as exc:
            if report is not None:
                _unknown(report, "geometry", str(exc), arc_id=raw.get("id", i))
    return out


def _regions(snapshot):
    return [r for r in snapshot.get("regions", []) if r.get("poured") is True
            and r.get("geometry_complete", True) is True
            and len(r.get("outline", [])) >= 3 and r.get("kind", "copper") not in {"cutout", "keepout", "polygon_definition"}
            and region_geometry_valid(r)]


def _layer_data(snapshot):
    layers = sorted(snapshot.get("layers", []), key=lambda l: l.get("order", 0))
    return layers, {l["name"]: i for i, l in enumerate(layers)}


def _span(via, indices):
    a, b = via.get("start_layer"), via.get("end_layer")
    if a not in indices or b not in indices:
        return set()
    lo, hi = sorted((indices[a], indices[b]))
    return {name for name, i in indices.items() if lo <= i <= hi}


def _reference_layers(layer_name, layers, constraints):
    layer = next((l for l in layers if l["name"] == layer_name), {})
    def assigned(rows):
        mapping = constraints.get('reference_nets', {})
        return [{**row, 'reference_net': mapping.get(row['name'], constraints.get('reference_net',
                    row.get('refnet', row.get('reference_net'))))} for row in rows]
    explicit = constraints.get("reference_layers", layer.get("reference_layers"))
    if explicit:
        return assigned([l for l in layers if l["name"] in explicit])
    reference = constraints.get("reference_layer", layer.get("reference_layer"))
    if reference:
        return assigned([l for l in layers if l["name"] == reference])
    # A declared reference net and plane layer are evidence; an arbitrary
    # adjacent signal layer is never silently promoted to a solid plane.
    copper = sorted((l for l in layers if l.get('kind') in {'plane', 'signal', 'copper'}
                     or l['name'] == layer_name), key=lambda l: l.get('order', 0))
    if not layer or layer not in copper:
        return []
    index = copper.index(layer)
    adjacent = copper[max(0, index - 1):index] + copper[index + 1:index + 2]
    # Never skip an intervening signal layer to borrow a farther GND plane.
    # Both sides of stripline require coverage unless the caller supplied an
    # explicit qualified reference assignment above.
    if not adjacent or any(not l.get('refnet', l.get('reference_net')) for l in adjacent):
        return []
    return adjacent


def _reference_check(snapshot, intent, segments, report, policy):
    layers, _ = _layer_data(snapshot)
    regions = _regions(snapshot)
    cutouts = [r for r in snapshot.get("regions", []) + snapshot.get("keepouts", [])
               if r.get("kind") == "cutout" and r.get("outline")]
    cutouts += [r for r in snapshot.get("cutouts", []) + snapshot.get("holes", []) if r.get("outline")]
    for seg in segments:
        net = seg.get("net_name", "")
        if not (_tags(intent, net) & FAST):
            continue
        c = intent.get("constraints", {}).get(net, {})
        c = {**c, **c.get("layer_widths", {}).get(seg.get("layer"), {})}
        refs = _reference_layers(seg.get("layer"), layers, c)
        if not refs:
            _unknown(report, "split_reference", "reference plane assignment unavailable", net_name=net, layer=seg.get("layer"))
            continue
        for ref in refs:
            refnet = ref.get("refnet", ref.get("reference_net"))
            copper = [r for r in regions if r.get("layer") == ref["name"] and r.get("net_name") == refnet]
            if not copper:
                _unknown(report, "split_reference", "no exact poured reference contours available", net_name=net, reference_layer=ref["name"])
                continue
            a, b = seg["start"], seg["end"]
            length = distance(a, b)
            if length <= EPS:
                continue
            half = float(seg["width_mm"]) / 2
            nx_, ny_ = -(b[1] - a[1]) / length * half, (b[0] - a[0]) / length * half
            gaps = uncovered_intervals(a, b, copper)
            # Test both physical trace edges in addition to its centreline.
            for sign in (-1, 1):
                gaps.extend(uncovered_intervals((a[0] + sign * nx_, a[1] + sign * ny_),
                                                (b[0] + sign * nx_, b[1] + sign * ny_), copper))
            for cutout in cutouts:
                if cutout.get("layer") not in {None, "all", 'MultiLayer', 'Multi Layer', ref["name"]}:
                    continue
                # A small cutout entirely under a wide track need not cross
                # the centreline or either offset edge.
                if point_in_ring(a, cutout["outline"]) or any(segment_distance(a, b, c, d) < half - EPS for c, d in ring_edges(cutout["outline"])):
                    gaps.append((0.0, 1.0))
            for region in copper:
                for hole in region.get("holes", []):
                    if any(segment_distance(a, b, c, d) < half - EPS for c, d in ring_edges(hole)):
                        # Localize a hole violation to its projection range.
                        ts = [project(xy(p), a, b)[1] for p in hole]
                        gaps.append((min(ts), max(ts)))
            gaps = merge_intervals(gaps)
            if gaps:
                lo, hi = max(gaps, key=lambda interval: interval[1] - interval[0])
                _violation(report, "split_reference", seg, round(sum(hi_ - lo_ for lo_, hi_ in gaps) * length, 8), 0.0,
                           "Reroute over continuous reference copper or repair the reference-plane void.",
                           {"track_id": seg["id"], "reference_layer": ref["name"], "reference_net": refnet,
                            "region_ids": [r.get("id") for r in copper], "uncovered_intervals": gaps,
                            "units": "mm of routed projection without full-width reference", "arc_error_mm": seg.get("arc_error_mm", 0)},
                           lerp(a, b, (lo + hi) / 2))
            else:
                # Segment endpoint disks are part of the routed copper. Testing
                # only parallel offset lines would miss a plane edge under an
                # end cap. Angular sampling has the same sagitta bound as arcs.
                n = max(16, math.ceil(math.pi / math.acos(max(-1.0, 1 - min(policy["arc_error_mm"], half) / half)))) if half > EPS else 16
                cap_gap, cap_point = 0.0, None
                for endpoint in (a, b):
                    for i in range(n):
                        theta = 2 * math.pi * i / n
                        p = (endpoint[0] + half * math.cos(theta), endpoint[1] + half * math.sin(theta))
                        intervals = uncovered_intervals(endpoint, p, copper)
                        gap = sum(hi - lo for lo, hi in intervals) * half
                        if gap > cap_gap:
                            cap_gap, cap_point = gap, p
                if cap_gap > policy["arc_error_mm"] + EPS:
                    _violation(report, "split_reference", seg, cap_gap, 0.0,
                               "Extend continuous reference copper beneath the full routed trace end cap.",
                               {"track_id": seg["id"], "reference_layer": ref["name"], "reference_net": refnet,
                                "units": "mm uncovered radial end-cap extent", "cap_sagitta_error_mm": policy["arc_error_mm"]}, cap_point)
                elif cap_gap > EPS:
                    _unknown(report, "split_reference", "end-cap reference coverage lies within geometric approximation tolerance", track_id=seg.get("arc_id", seg["id"]), reference_layer=ref["name"], error_mm=policy["arc_error_mm"])
    if any(_tags(intent, s.get("net_name", "")) & FAST for s in segments) and not _coverage_complete(snapshot, ["regions", "layers"]):
        _unknown(report, "split_reference", "reference contours/holes/stackup extraction is incomplete")


def _spacing_check(snapshot, intent, segments, report, policy):
    by_layer = defaultdict(SpatialIndex)
    groups = {}
    maxwidth = max((s["width_mm"] for s in segments), default=0)
    maxerror = max((s.get("arc_error_mm", 0) for s in segments), default=0)
    for i, seg in enumerate(segments):
        by_layer[seg.get("layer")].insert(i, bbox_segment(seg["start"], seg["end"], seg["width_mm"] / 2))
    copper_layers = sorted((l for l in snapshot.get('layers', []) if l.get('kind') in
                            {'plane', 'signal', 'copper'}), key=lambda l: l.get('order', 0))
    adjacent = {}
    for index, layer in enumerate(copper_layers):
        adjacent[layer['name']] = [l['name'] for l in copper_layers[max(0, index - 1):index] +
                                  copper_layers[index + 1:index + 2]]
    intentional_pairs = [set(p.get("nets", [])) for p in intent.get("differential_pairs", [])]
    for i, seg in enumerate(segments):
        tags = _tags(intent, seg.get("net_name", ""))
        if not tags & (FAST | {"SENSITIVE_ANALOG"}):
            continue
        multiple = policy["sensitive_spacing_widths"] if "SENSITIVE_ANALOG" in tags else policy["general_spacing_widths"]
        limit = multiple * float(seg["width_mm"])
        box = bbox_segment(seg["start"], seg["end"], limit + (maxwidth + seg["width_mm"]) / 2 + 2 * maxerror)
        candidates = set()
        for layer_name in [seg.get('layer')] + adjacent.get(seg.get('layer'), []):
            candidates.update(by_layer[layer_name].query(box))
        for j in sorted(candidates):
            other = segments[j]
            if i == j or seg.get("net_name") == other.get("net_name"):
                continue
            # Intentional within-pair spacing is controlled by impedance rules.
            if any({seg.get("net_name"), other.get("net_name")} <= pair for pair in intentional_pairs):
                continue
            other_tags = _tags(intent, other.get("net_name", ""))
            if other_tags <= {"GROUND", "POWER"} and other_tags:
                continue
            error = seg.get("arc_error_mm", 0) + other.get("arc_error_mm", 0)
            result = parallel_coupling(seg["start"], seg["end"], other["start"], other["end"],
                                       seg["width_mm"], other["width_mm"], max(0, limit - error), policy["parallel_angle_deg"])
            if error:
                possible = parallel_coupling(seg["start"], seg["end"], other["start"], other["end"],
                                            seg["width_mm"], other["width_mm"], limit + error, policy["parallel_angle_deg"])
                if possible and (not result or possible["coupled_length_mm"] > result["coupled_length_mm"] + EPS):
                    _unknown(report, "parallel_coupling", "spacing is within bounded arc approximation uncertainty", track_id=seg.get("arc_id", seg["id"]), other_track_id=other.get("arc_id", other["id"]), error_mm=error)
            if result:
                key = (seg.get("arc_id", seg["id"]), other.get("arc_id", other["id"]), seg.get("layer"), multiple)
                group = groups.setdefault(key, {"segment": seg, "other": other, "limit": limit, "multiple": multiple,
                                                "result": result, "intervals": defaultdict(list), "error_mm": error})
                group["intervals"][seg["id"]].append(result["overlap_interval_mm"])
                if result["edge_spacing_mm"] < group["result"]["edge_spacing_mm"]:
                    group["result"] = result
    for (sid, oid, layer, multiple), group in groups.items():
        length = sum(hi - lo for intervals in group["intervals"].values() for lo, hi in merge_intervals(intervals))
        if length < policy["minimum_coupled_length_mm"] - EPS:
            continue
        seg, other, result = group["segment"], group["other"], group["result"]
        _violation(report, "parallel_coupling", seg, round(result["edge_spacing_mm"], 8), group["limit"],
                   f"Increase copper-edge spacing to {multiple:g} victim trace widths or reduce parallel overlap.",
                   {"track_id": sid, "aggressor_track_id": oid, "aggressor_net": other.get("net_name"),
                    "victim_width_mm": seg["width_mm"], "coupled_length_mm": length,
                    "aggressor_layer": other.get('layer'),
                    "geometry_basis": 'same-layer copper edges' if seg.get('layer') == other.get('layer') else 'adjacent-copper-layer 2D projection; no field-solver coupling estimate',
                    "spacing_widths": multiple, "arc_error_bound_mm": group["error_mm"],
                    "units": "mm edge-to-edge", "policy": "configurable heuristic"}, result["point"])


def _return_via_check(snapshot, intent, report, policy):
    layers, indices = _layer_data(snapshot)
    regions = _regions(snapshot)
    vias = snapshot.get("vias", [])
    for via in vias:
        net = via.get("net_name", "")
        if not _tags(intent, net) & FAST:
            continue
        if not _span(via, indices):
            _unknown(report, "return_via", "signal via layer span unavailable", via_id=via.get("id"))
            continue
        c = intent.get("constraints", {}).get(net, {})
        before = _reference_layers(via.get("start_layer"), layers, {**c, **c.get("layer_widths", {}).get(via.get("start_layer"), {})})
        after = _reference_layers(via.get("end_layer"), layers, {**c, **c.get("layer_widths", {}).get(via.get("end_layer"), {})})
        if len(before) != 1 or len(after) != 1:
            _unknown(report, "return_via", "unambiguous references on both sides of transition unavailable", via_id=via.get("id"))
            continue
        a, b = before[0], after[0]
        if a["name"] == b["name"]:
            continue
        refnets = {l.get("refnet", l.get("reference_net")) for l in (a, b)}
        if None in refnets or len(refnets) != 1:
            _unknown(report, "return_via", "reference nets differ; a ground stitching via cannot prove return continuity", via_id=via.get("id"), references=[a["name"], b["name"]])
            continue
        refnet = next(iter(refnets))
        valid = []
        for candidate in vias:
            if candidate.get("net_name") != refnet or not {a["name"], b["name"]} <= _span(candidate, indices):
                continue
            # The via must physically touch poured copper on BOTH references.
            if all(any(r.get("layer") == plane["name"] and r.get("net_name") == refnet and point_in_region(xy(candidate), r)
                       for r in regions) for plane in (a, b)):
                valid.append(candidate)
        nearest = min((distance(xy(via), xy(v)) for v in valid), default=None)
        if nearest is None or nearest > policy["return_via_radius_mm"] + EPS:
            if not _coverage_complete(snapshot, ["vias", "regions", "layers"]):
                _unknown(report, "return_via", "complete via/reference copper evidence required to prove missing stitching", via_id=via.get("id"))
                continue
            _violation(report, "return_via", via, nearest, policy["return_via_radius_mm"],
                       "Add a reference-net stitching via connecting both actual reference planes near the signal transition.",
                       {"signal_via_id": via.get("id"), "reference_layers": [a["name"], b["name"]],
                        "reference_net": refnet, "eligible_via_ids": [v.get("id") for v in valid], "units": "mm"})


def _closest_pair(a, b, c, d):
    hits = segment_intersection_parameters(a, b, c, d)
    if hits:
        p = lerp(a, b, hits[0]); return p, p
    pairs = [(a, project(a, c, d)[0]), (b, project(b, c, d)[0]),
             (project(c, a, b)[0], c), (project(d, a, b)[0], d)]
    return min(pairs, key=lambda pair: distance(*pair))


class CopperGraph:
    """Physical connectivity plus routed distances, using real pad contours.

    Poured regions are connected sets with holes. A visibility graph around
    contour vertices supplies distances; a separate region node establishes
    connectivity without inventing a straight path through a hole.
    """
    def __init__(self, snapshot, net_name, error_mm=0.002):
        self.snapshot, self.net_name = snapshot, net_name
        self.graph, self.connectivity = nx.Graph(), nx.Graph()
        self.unknowns = []
        self.pad_nodes, self.via_nodes = {}, {}
        self.segments = [s for s in _segments(snapshot, error=error_mm) if s.get("net_name") == net_name]
        self.regions = [r for r in _regions(snapshot) if r.get("net_name") == net_name]
        self.layers, self.indices = _layer_data(snapshot)
        for region in snapshot.get("regions", []):
            if region.get("net_name") == net_name and region.get("poured") is True and region not in self.regions:
                self.unknowns.append(f"{region.get('id', 'region')}: incomplete or invalid copper contour")
        for section in ("tracks", "arcs"):
            if any(s.get("net_name") == net_name and float(s.get("width_mm", 0)) <= 0 for s in snapshot.get(section, [])):
                self.unknowns.append(f"{section}: copper width unavailable")
        self._build()

    def node(self, point, layer):
        n = (round(point[0], 8), round(point[1], 8), layer)
        self.graph.add_node(n); self.connectivity.add_node(n)
        return n

    def edge(self, a, b, length, **data):
        existing = self.graph.get_edge_data(a, b)
        if existing is None or existing["length_mm"] > length:
            self.graph.add_edge(a, b, length_mm=length, **data)
        self.connectivity.add_edge(a, b)

    def _build(self):
        cuts = [[(0.0, self.node(s["start"], s.get("layer"))), (1.0, self.node(s["end"], s.get("layer")))] for s in self.segments]
        indexes = defaultdict(SpatialIndex)
        for i, s in enumerate(self.segments):
            indexes[s.get("layer")].insert(i, bbox_segment(s["start"], s["end"], s["width_mm"] / 2))
        def attach(i, p):
            s = self.segments[i]
            p, t = project(p, s["start"], s["end"])
            n = self.node(p, s.get("layer")); cuts[i].append((t, n)); return n
        for i, s in enumerate(self.segments):
            for j in indexes[s.get("layer")].query(bbox_segment(s["start"], s["end"], s["width_mm"] / 2)):
                if j <= i:
                    continue
                t = self.segments[j]
                p, q = _closest_pair(s["start"], s["end"], t["start"], t["end"])
                if distance(p, q) <= (s["width_mm"] + t["width_mm"]) / 2 + EPS:
                    self.edge(attach(i, p), attach(j, q), distance(p, q), track_ids=[s["id"], t["id"]])
        for i, pad in enumerate(self.snapshot.get("pads", [])):
            if pad.get("net_name") != self.net_name:
                continue
            pid = str(pad.get("id", f"{pad.get('component')}.{pad.get('pad_name', i)}"))
            try:
                ring = pad_outline(pad)
            except ValueError as exc:
                self.unknowns.append(str(exc)); continue
            if not ring or pad.get("width_mm", 0) <= 0 or pad.get("height_mm", 0) <= 0:
                self.unknowns.append(f"{pid}: pad contour unavailable"); continue
            layers = pad.get("layers") or [pad.get("layer")]
            if layers == ["Multi-Layer"] or layers == ["multi_layer"]:
                layers = [l["name"] for l in self.layers]
            ns = []
            for layer in layers:
                center = self.node(xy(pad), layer); ns.append(center)
                box = (min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring))
                for j in indexes[layer].query(box):
                    s = self.segments[j]
                    candidates = [_closest_pair(s["start"], s["end"], a, b) for a, b in ring_edges(ring)]
                    candidates.extend((p, p) for p in (s["start"], s["end"], project(xy(pad), s["start"], s["end"])[0]) if point_in_ring(p, ring))
                    candidates = [(p, q) for p, q in candidates if distance(p, q) <= s["width_mm"] / 2 + EPS]
                    if not candidates:
                        continue
                    p, q = min(candidates, key=lambda pair: distance(xy(pad), pair[1]) + distance(*pair))
                    self.edge(center, attach(j, p), distance(xy(pad), q) + distance(p, q), pad_id=pid, pad_contact_point=q)
            plated = pad.get("plated", pad.get("is_plated"))
            if len(ns) > 1 and plated is None:
                self.unknowns.append(f"{pid}: through-pad plating is unknown")
            if plated is True:
                for a, b in zip(ns, ns[1:]):
                    self.edge(a, b, 0.0, plated_pad_id=pid)
            self.pad_nodes[pid] = ns
        for i, via in enumerate(self.snapshot.get("vias", [])):
            if via.get("net_name") != self.net_name:
                continue
            vid = str(via.get("id", f"via:{i}"))
            span = _span(via, self.indices)
            if not span:
                self.unknowns.append(f"{vid}: via layer span unavailable"); continue
            ns = []
            radius = float(via.get("diameter_mm", 0)) / 2
            for layer in sorted(span, key=self.indices.get):
                center = self.node(xy(via), layer); ns.append(center)
                for j in indexes[layer].query(bbox_segment(xy(via), xy(via), radius)):
                    s = self.segments[j]
                    p, _ = project(xy(via), s["start"], s["end"])
                    if distance(p, xy(via)) <= radius + s["width_mm"] / 2 + EPS:
                        self.edge(center, attach(j, p), distance(xy(via), p), via_id=vid)
            for a, b in zip(ns, ns[1:]):
                self.edge(a, b, 0.0, via_id=vid)
            self.via_nodes[vid] = ns
        # A via placed on a pad can touch its copper away from the pad centre;
        # that access must work without a synthetic connecting track.
        pad_index = SpatialIndex()
        contours = {}
        for pad in self.snapshot.get("pads", []):
            pid = _pad_key(pad)
            if pid not in self.pad_nodes:
                continue
            try:
                contour = pad_outline(pad)
            except ValueError:
                continue
            contours[pid] = contour
            pad_index.insert(pid, (min(x for x, y in contour), min(y for x, y in contour), max(x for x, y in contour), max(y for x, y in contour)))
        for via in self.snapshot.get("vias", []):
            vid = str(via.get("id"))
            if vid not in self.via_nodes:
                continue
            center, radius = xy(via), via.get("diameter_mm", 0) / 2
            for pid in pad_index.query(bbox_segment(center, center, radius)):
                contour = contours[pid]
                if point_in_ring(center, contour) or min(point_segment_distance(center, a, b) for a, b in ring_edges(contour)) <= radius + EPS:
                    contact = center if point_in_ring(center, contour) else min((project(center, a, b)[0] for a, b in ring_edges(contour)), key=lambda p: distance(p, center))
                    for a in self.via_nodes[vid]:
                        for b in self.pad_nodes[pid]:
                            if a[2] == b[2]:
                                self.edge(a, b, distance(a[:2], contact) + distance(contact, b[:2]), via_id=vid, pad_id=pid, pad_contact_point=contact)
        for s, points in zip(self.segments, cuts):
            points.sort(key=lambda p: p[0])
            for (t, a), (u, b) in zip(points, points[1:]):
                length = (u - t) * s.get("analytical_length_mm", distance(s["start"], s["end"]))
                self.edge(a, b, length, track_id=s["id"], arc_error_mm=s.get("arc_error_mm", 0))
        # Pad-to-pad contact is necessary for pad-side net ties and common
        # exposed-pad geometries. It is evaluated on rotated contours.
        pads = [p for p in self.snapshot.get("pads", []) if p.get("net_name") == self.net_name]
        pindex, outlines = SpatialIndex(), {}
        for i, p in enumerate(pads):
            try:
                r = pad_outline(p)
            except ValueError:
                continue
            outlines[i] = r
            pindex.insert(i, (min(x for x, y in r), min(y for x, y in r), max(x for x, y in r), max(y for x, y in r)))
        for i, r in outlines.items():
            for j in pindex.query(pindex.boxes[i]):
                if j <= i:
                    continue
                other = outlines[j]
                touching = point_in_ring(r[0], other) or point_in_ring(other[0], r) or any(
                    segment_intersection_parameters(a, b, c, d) for a, b in ring_edges(r) for c, d in ring_edges(other))
                if touching:
                    ida = str(pads[i].get("id", f"{pads[i].get('component')}.{pads[i].get('pad_name', i)}"))
                    idb = str(pads[j].get("id", f"{pads[j].get('component')}.{pads[j].get('pad_name', j)}"))
                    for a in self.pad_nodes.get(ida, []):
                        for b in self.pad_nodes.get(idb, []):
                            if a[2] == b[2]:
                                contacts = [p for p in r if point_in_ring(p, other)] + [p for p in other if point_in_ring(p, r)]
                                for p, q in ring_edges(r):
                                    for u, v in ring_edges(other):
                                        contacts.extend(lerp(p, q, t) for t in segment_intersection_parameters(p, q, u, v))
                                contact = min(contacts, key=lambda p: distance(a[:2], p) + distance(p, b[:2]))
                                self.edge(a, b, distance(a[:2], contact) + distance(contact, b[:2]), touching_pads=[ida, idb], pad_contact_point=contact)
        for i, region in enumerate(self.regions):
            layer = region.get("layer")
            attachments = [n for n in self.graph if n[2] == layer and point_in_region(n[:2], region)]
            # A track may traverse a small region with both ends outside.
            for j, s in enumerate(self.segments):
                if s.get("layer") != layer:
                    continue
                for lo, hi in segment_region_intervals(s["start"], s["end"], region):
                    for t in (lo, hi):
                        n = self.node(lerp(s["start"], s["end"], t), layer)
                        # Segment cuts were already committed: attach to its
                        # neighbouring graph points with true along-track cost.
                        for u, neighbor in cuts[j]:
                            self.edge(n, neighbor, abs(u - t) * distance(s["start"], s["end"]), track_id=s["id"])
                        attachments.append(n)
            # Region contact at an off-centre pad/via edge counts as real
            # copper contact. A centre-inside shortcut misses these cases.
            for pad in pads:
                pid = _pad_key(pad)
                try:
                    contour = pad_outline(pad)
                except ValueError:
                    continue
                ns = [n for n in self.pad_nodes.get(pid, []) if n[2] == layer]
                if not ns:
                    continue
                contacts = [p for p in contour if point_in_region(p, region)]
                for a, b in ring_edges(contour):
                    for lo, hi in segment_region_intervals(a, b, region):
                        contacts.append(lerp(a, b, (lo + hi) / 2))
                for p in contacts[:1]:
                    n = self.node(p, layer); attachments.append(n)
                    for center in ns:
                        self.edge(n, center, distance(p, center[:2]), pad_id=pid)
            for via in self.snapshot.get("vias", []):
                if via.get("net_name") != self.net_name:
                    continue
                ns = [n for n in self.via_nodes.get(str(via.get("id")), []) if n[2] == layer]
                if not ns:
                    continue
                center, radius = xy(via), via.get("diameter_mm", 0) / 2
                contacts = []
                for ring in [region["outline"]] + region.get("holes", []):
                    for a, b in ring_edges(ring):
                        p = project(center, a, b)[0]
                        if distance(p, center) < radius - EPS:
                            contacts.append(p)
                for p in contacts[:1]:
                    n = self.node(p, layer); attachments.append(n)
                    for center_node in ns:
                        self.edge(n, center_node, distance(p, center), via_id=str(via.get("id")))
            virtual = ("region", i, layer)
            self.connectivity.add_node(virtual)
            for n in attachments:
                self.connectivity.add_edge(virtual, n)
            vertices = [self.node(xy(p), layer) for ring in [region["outline"]] + region.get("holes", []) for p in ring]
            candidates = list(dict.fromkeys(attachments + vertices))
            if len(candidates) > 256:
                self.unknowns.append(f"{region.get('id', i)}: route-distance visibility graph exceeds 256 vertices")
                continue
            for j, a in enumerate(candidates):
                for b in candidates[j + 1:]:
                    if not uncovered_intervals(a[:2], b[:2], [region]):
                        self.edge(a, b, distance(a[:2], b[:2]), region_id=region.get("id", i))

    def shortest(self, starts, ends):
        best = None
        for a in starts:
            for b in ends:
                try:
                    path = nx.shortest_path(self.graph, a, b, weight="length_mm")
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                edges = [self.graph[u][v] for u, v in zip(path, path[1:])]
                length = sum(e["length_mm"] for e in edges)
                if best is None or length < best["length_mm"]:
                    best = {"length_mm": length, "nodes": path, "edges": edges,
                            "via_ids": sorted({e["via_id"] for e in edges if "via_id" in e})}
        return best


def net_connectivity(snapshot: dict, net_name: str) -> dict:
    """Conservative proof that all captured terminal pads are copper-connected."""
    graph = CopperGraph(snapshot, net_name)
    pad_ids = sorted(graph.pad_nodes)
    groups = []
    for component in nx.connected_components(graph.connectivity):
        terminals = sorted(pid for pid, ns in graph.pad_nodes.items() if any(n in component for n in ns))
        if terminals:
            groups.append(terminals)
    complete = _coverage_complete(snapshot, ["tracks", "arcs", "vias", "pads", "regions", "layers"]) and not graph.unknowns
    expected = [p for p in snapshot.get("pads", []) if p.get("net_name") == net_name]
    if len(pad_ids) != len(expected):
        complete = False
    return {"net_name": net_name, "complete": complete, "connected": len(groups) == 1 and bool(pad_ids),
            "pad_ids": pad_ids, "components": groups, "unconnected_components": groups[1:],
            "unknowns": graph.unknowns + ([] if complete else ["connectivity extraction coverage is incomplete"])}


def _find_pad(snapshot, component, pin):
    return next((p for p in snapshot.get("pads", []) if p.get("component") == component and str(p.get("pad_name")) == str(pin)), None)


def _pad_key(pad):
    return str(pad.get("id", f"{pad.get('component')}.{pad.get('pad_name')}"))


def _decoupling_check(snapshot, intent, report, policy, graphs):
    for motif in intent.get("motifs", []):
        if motif.get("kind") != "decoupling":
            continue
        if motif.get("intent_verified") is False:
            _unknown(report, "decoupling", "supply capacitor topology found but local decoupling purpose/value unverified", component=motif.get("component"))
            continue
        source = _find_pad(snapshot, motif.get("source_component"), motif.get("source_pin"))
        cap = _find_pad(snapshot, motif.get("component"), motif.get("cap_power_pin"))
        ground = _find_pad(snapshot, motif.get("component"), motif.get("cap_ground_pin"))
        if not source or not cap or not ground:
            _unknown(report, "decoupling", "IC/capacitor pad assignment unavailable", motif=motif)
            continue
        net = motif.get("net_name", source.get("net_name"))
        graph = graphs.setdefault(net, None)
        if graph is None:
            graph = graphs[net] = CopperGraph(snapshot, net, policy["arc_error_mm"])
        path = graph.shortest(graph.pad_nodes.get(_pad_key(source), []), graph.pad_nodes.get(_pad_key(cap), []))
        if path is None:
            _unknown(report, "decoupling", "no proven routed IC-to-capacitor path", source_pad=_pad_key(source), cap_pad=_pad_key(cap))
            continue
        evidence = {"source_pad": _pad_key(source), "cap_pad": _pad_key(cap), "routed_path": path,
                    "units": "mm routed copper length", "policy": "configurable heuristic"}
        limit = policy["decoupling_path_limit_mm"]
        if path["length_mm"] > limit + EPS:
            _violation(report, "decoupling_path_length", source, path["length_mm"], limit,
                       "Place the capacitor beside the actual supply pad and shorten its routed connection.", evidence)
        if path["via_ids"]:
            _violation(report, "decoupling_via_between", source, len(path["via_ids"]), 0,
                       "Connect the local decoupling capacitor before the supply changes layers.", evidence)
        gnet = ground.get("net_name")
        ggraph = graphs.get(gnet)
        if ggraph is None:
            ggraph = graphs[gnet] = CopperGraph(snapshot, gnet, policy["arc_error_mm"])
        # Ground via must really reach a declared reference plane, not just
        # be a same-net nearby blind via to an unrelated signal layer.
        layers, indices = _layer_data(snapshot)
        planes = [l["name"] for l in layers if l.get("refnet", l.get("reference_net")) == gnet]
        ends = [node for via in snapshot.get("vias", []) if via.get("net_name") == gnet and set(planes) & _span(via, indices)
                for node in ggraph.via_nodes.get(str(via.get("id")), [])]
        ret = ggraph.shortest(ggraph.pad_nodes.get(_pad_key(ground), []), ends)
        if ret is None:
            _unknown(report, "decoupling_ground_return", "no proven capacitor ground-to-reference via path", cap_ground_pad=_pad_key(ground))
        elif path["length_mm"] + ret["length_mm"] > limit + EPS:
            _violation(report, "decoupling_ground_return", ground, path["length_mm"] + ret["length_mm"], limit,
                       "Shorten the supply-to-capacitor-to-reference return path and place a ground via at the capacitor pad.",
                       {**evidence, "ground_pad": _pad_key(ground), "ground_path": ret, "units": "mm combined routed supply and ground access length"})


def _switch_loop_check(snapshot, intent, segments, report, policy):
    by_id = {s["id"]: s for s in segments}
    pads = {_pad_key(p): p for p in snapshot.get("pads", [])}
    for motif in intent.get("motifs", []):
        if motif.get("kind") != "switching_loop":
            continue
        cycle = motif.get("terminal_cycle", [])
        if cycle:
            if len(cycle) < 4 or any(pid not in pads for pid in cycle):
                _unknown(report, "switch_loop_area", "switching terminal cycle pad geometry unavailable", terminal_cycle=cycle)
                continue
            points, path_layers, paths, valid = [], set(), [], True
            for ida, idb in zip(cycle, cycle[1:] + cycle[:1]):
                a, b = pads[ida], pads[idb]
                if a.get("net_name") == b.get("net_name"):
                    graph = CopperGraph(snapshot, a.get("net_name"), policy["arc_error_mm"])
                    path = graph.shortest(graph.pad_nodes.get(ida, []), graph.pad_nodes.get(idb, []))
                    if path is None or graph.unknowns:
                        valid = False; break
                    points.extend(n[:2] for n in path["nodes"])
                    path_layers.update(n[2] for n in path["nodes"])
                    paths.append({"start_pad": ida, "end_pad": idb, **path})
                elif a.get("component") == b.get("component"):
                    # Explicit topology cycle asserts a component's conduction
                    # terminals. Use their physical pad span, not its centroid.
                    points.extend([xy(a), xy(b)])
                    paths.append({"component": a.get("component"), "terminal_span": [ida, idb],
                                  "basis": "explicit conduction-terminal cycle; 2-D package projection"})
                else:
                    valid = False; break
            if not valid or len(path_layers) != 1:
                _unknown(report, "switch_loop_area", "terminal cycle is unrouted or nonplanar", terminal_cycle=cycle)
                continue
            points = [p for i, p in enumerate(points) if i == 0 or distance(p, points[i - 1]) > EPS]
            if distance(points[0], points[-1]) > EPS:
                points.append(points[0])
            if not region_geometry_valid({"outline": points}):
                _unknown(report, "switch_loop_area", "projected terminal cycle self-intersects or collapses", terminal_cycle=cycle)
                continue
            area = polygon_area(points)
            limit = motif.get("max_area_mm2", policy["maximum_switch_loop_area_mm2"])
            if limit is None:
                _unknown(report, "switch_loop_area", "projected commutating loop measured but permitted area unspecified", measured_area_mm2=area, terminal_cycle=cycle)
            elif area > limit + EPS:
                _violation(report, "switch_loop_area", pads[cycle[0]], area, limit,
                           "Compact the actual commutating terminal/copper cycle, including input bypass and switching-device access.",
                           {"terminal_cycle": cycle, "physical_paths": paths, "loop_vertices": points,
                            "units": "mm2 projected board loop area", "package_internal_path": "not reconstructed; terminal-span projection"})
            continue
        ids = motif.get("track_ids", [])
        if not ids or any(str(i) not in by_id for i in ids):
            _unknown(report, "switch_loop_area", "ordered, physically connected loop track IDs unavailable", motif=motif)
            continue
        route = [by_id[str(i)] for i in ids]
        points = [route[0]["start"], route[0]["end"]]
        valid = True
        for s in route[1:]:
            if distance(points[-1], s["start"]) <= EPS:
                points.append(s["end"])
            elif distance(points[-1], s["end"]) <= EPS:
                points.append(s["start"])
            else:
                valid = False; break
        if not valid or distance(points[0], points[-1]) > EPS or len({s.get("layer") for s in route}) != 1:
            _unknown(report, "switch_loop_area", "loop is open, unordered, or nonplanar; enclosed 2-D area cannot be proven", track_ids=ids)
            continue
        if not region_geometry_valid({"outline": points}):
            _unknown(report, "switch_loop_area", "physical loop self-intersects or collapses", track_ids=ids)
            continue
        area = polygon_area(points)
        limit = motif.get("max_area_mm2", policy["maximum_switch_loop_area_mm2"])
        if limit is None:
            _unknown(report, "switch_loop_area", "loop area measured but permitted area is unspecified", measured_area_mm2=area, track_ids=ids)
        elif area > limit + EPS:
            _violation(report, "switch_loop_area", route[0], area, limit,
                       "Compact the verified commutating loop and colocate its switching devices and input bypass capacitor.",
                       {"track_ids": ids, "loop_vertices": points, "units": "mm2 enclosed physical route area"})


def _current_check(snapshot, intent, segments, report):
    layers, _ = _layer_data(snapshot)
    layer_by_name = {l["name"]: l for l in layers}
    for net, tags in intent.get("net_classes", {}).items():
        if "HIGH_CURRENT_POWER" not in tags:
            continue
        constraints = intent.get("constraints", {}).get(net, {})
        width = constraints.get("required_width_mm")
        if width is None and constraints.get("required_width_mils") is not None:
            width = float(constraints["required_width_mils"]) * MM_PER_MIL
        if width is None and constraints.get("current_a"):
            from eda_agent.design.trace_sizing import trace_width_for_current_qualified
            result = trace_width_for_current_qualified(constraints["current_a"], dataset=constraints.get("ipc2152_dataset"),
                                                       copper_oz=constraints.get("copper_oz", 1.0), delta_t_c=constraints.get("delta_t_c", 10),
                                                       layer=constraints.get("layer", "external"))
            if result.get("supported"):
                width = result["recommended_width_mils"] * MM_PER_MIL
            else:
                _unknown(report, "high_current_neck", result.get("reason", "qualified trace sizing unavailable"), net_name=net)
        layer_widths = constraints.get("layer_widths", {})
        if (width is None or width <= 0) and not layer_widths:
            _unknown(report, "high_current_neck", "required width or qualified current/thermal constraints unavailable", net_name=net)
            continue
        base_width = width
        def on_layer(layer):
            local = layer_widths.get(layer, {})
            return local.get("required_width_mm", local.get("required_width_mils", 0) * MM_PER_MIL) or base_width
        for seg in segments:
            if seg.get("net_name") != net:
                continue
            width = on_layer(seg.get("layer"))
            if not width:
                _unknown(report, "high_current_neck", "required current width unavailable on routed layer", net_name=net, layer=seg.get("layer"))
                continue
            if seg["width_mm"] < width - EPS:
                # A narrow trace embedded in a wide same-net poured corridor
                # is not itself a copper bottleneck. Require an exact whole-
                # capsule containment proof in at least one real region.
                reinforced = False
                for copper in _regions(snapshot):
                    if copper.get("net_name") != net or copper.get("layer") != seg.get("layer"):
                        continue
                    if uncovered_intervals(seg["start"], seg["end"], [copper]):
                        continue
                    boundary_distance = min(segment_distance(seg["start"], seg["end"], a, b)
                                            for ring in [copper["outline"]] + copper.get("holes", []) for a, b in ring_edges(ring))
                    if boundary_distance >= width / 2 + seg.get("arc_error_mm", 0) - EPS:
                        reinforced = True; break
                if reinforced:
                    continue
                _violation(report, "high_current_neck", seg, seg["width_mm"], width,
                           "Widen this current-path neck or use a verified copper/via path with sufficient cross section.",
                           {"track_id": seg["id"], "current_a": constraints.get("current_a"),
                            "sizing_model": constraints.get("sizing_model", "explicit required width"), "units": "mm copper width"})
        # A narrow passage is formed by two nonadjacent boundary edges whose
        # connecting chord lies entirely in copper. Scan actual polygon edges,
        # including holes; long passages, rather than corner tips, are counted.
        for region in _regions(snapshot):
            if region.get("net_name") != net:
                continue
            width = on_layer(region.get("layer"))
            if not width:
                _unknown(report, "high_current_polygon_neck", "required current width unavailable on poured layer", net_name=net, layer=region.get("layer"))
                continue
            rings = [region["outline"]] + region.get("holes", [])
            edges = [(ri, ei, a, b, len(ring)) for ri, ring in enumerate(rings) for ei, (a, b) in enumerate(ring_edges(ring))]
            index = SpatialIndex(max(width, 0.1))
            for i, (_, _, a, b, _) in enumerate(edges):
                index.insert(i, bbox_segment(a, b))
            best = None
            for i, (ri, ei, a, b, count) in enumerate(edges):
                for j in index.query(bbox_segment(a, b, width)):
                    rj, ej, c, d, _ = edges[j]
                    if j <= i or (ri == rj and ((ei - ej) % count in {0, 1, count - 1})):
                        continue
                    coupling = parallel_coupling(a, b, c, d, 0, 0, width, 10)
                    if not coupling or coupling["coupled_length_mm"] < width:
                        continue
                    p, q = _closest_pair(a, b, c, d)
                    gap = distance(p, q)
                    if gap <= EPS or uncovered_intervals(p, q, [region]):
                        continue
                    if best is None or gap < best[0]:
                        best = gap, p, q
            if best:
                _violation(report, "high_current_polygon_neck", region, best[0], width,
                           "Widen the poured-copper passage and repour; verify that the actual load path uses the enlarged copper.",
                           {"region_id": region.get("id"), "neck_endpoints": [best[1], best[2]], "units": "mm boundary-to-boundary copper neck"},
                           lerp(best[1], best[2], 0.5))
        for via in snapshot.get("vias", []):
            if via.get("net_name") != net:
                continue
            width = on_layer(via.get("start_layer"))
            if not width:
                _unknown(report, "high_current_via", "required current cross section unavailable", via_id=via.get("id"))
                continue
            # Barrel plating is not foil weight. No default plating thickness
            # is invented from the via's drill or pad diameter.
            plating = via.get("plating_thickness_mm", constraints.get("via_plating_thickness_mm"))
            drill = via.get("drill_mm")
            copper = constraints.get("copper_thickness_mm")
            if copper is None:
                layer = layer_by_name.get(via.get("start_layer"), {})
                copper = layer.get("copper_thickness_mm")
            if not plating or not drill or not copper:
                _unknown(report, "high_current_via", "barrel plating/drill/foil cross section unavailable", via_id=via.get("id"), net_name=net)
                continue
            barrel_area = math.pi * ((drill / 2 + plating) ** 2 - (drill / 2) ** 2)
            required = width * copper
            if barrel_area < required - EPS:
                _violation(report, "high_current_via", via, barrel_area, required,
                           "Increase verified via barrel cross section or use a parallel via array with a proven current-sharing model.",
                           {"via_id": via.get("id"), "plating_thickness_mm": plating, "drill_mm": drill,
                            "units": "mm2 copper cross section", "model": "cross-section bottleneck only; not a via thermal rating"})


def _kelvin_check(snapshot, intent, report, policy, graphs):
    pads = {_pad_key(p): p for p in snapshot.get("pads", [])}
    for motif in intent.get("motifs", []):
        if motif.get("kind") != "current_shunt":
            continue
        paths = motif.get("kelvin_paths", [])
        if not paths:
            _unknown(report, "kelvin_branch", "explicit shunt/load/sense terminal mapping required", component=motif.get("component"), side=motif.get("side"))
        for terminals in paths:
            ids = [terminals.get(k) for k in ("sense_pad", "shunt_pad", "load_pad")]
            sense_shunt = terminals.get("sense_shunt_pad", ids[1])
            if not all(i in pads for i in ids + [sense_shunt]):
                _unknown(report, "kelvin_branch", "mapped Kelvin/load pads not present", terminal_mapping=terminals)
                continue
            sense_id, shunt_id, load_id = ids
            nets = {pads[i].get("net_name") for i in ids + [sense_shunt]}
            combined, nodes_by_pad = nx.Graph(), {}
            for net in nets:
                graph = graphs.get(net)
                if graph is None:
                    graph = graphs[net] = CopperGraph(snapshot, net, policy["arc_error_mm"])
                if graph.unknowns:
                    _unknown(report, "kelvin_branch", "routing graph has unsupported geometry", net_name=net, details=graph.unknowns)
                relabel = {n: (net, n) for n in graph.graph}
                combined = nx.compose(combined, nx.relabel_nodes(graph.graph, relabel))
                nodes_by_pad.update({pid: [relabel[n] for n in ns] for pid, ns in graph.pad_nodes.items()})
            for tie in snapshot.get("net_ties", []):
                tie_ids = tie.get("pad_ids", tie.get("pads", []))
                if not all(isinstance(p, str) and p in pads for p in tie_ids):
                    continue
                for i, ida in enumerate(tie_ids):
                    for idb in tie_ids[i + 1:]:
                        try:
                            ra, rb = pad_outline(pads[ida]), pad_outline(pads[idb])
                        except ValueError:
                            continue
                        touches = point_in_ring(ra[0], rb) or point_in_ring(rb[0], ra) or any(
                            segment_intersection_parameters(a, b, c, d) for a, b in ring_edges(ra) for c, d in ring_edges(rb))
                        if touches:
                            for a in nodes_by_pad.get(ida, []):
                                for b in nodes_by_pad.get(idb, []):
                                    if a[1][2] == b[1][2]:
                                        combined.add_edge(a, b, length_mm=distance(xy(pads[ida]), xy(pads[idb])), net_tie_id=tie.get("id"))
            def shortest(aid, bid):
                best = None
                for a in nodes_by_pad.get(aid, []):
                    for b in nodes_by_pad.get(bid, []):
                        try:
                            path = nx.shortest_path(combined, a, b, weight="length_mm")
                        except (nx.NetworkXNoPath, nx.NodeNotFound):
                            continue
                        length = sum(combined[u][v]["length_mm"] for u, v in zip(path, path[1:]))
                        if best is None or length < best[0]:
                            best = length, path
                return best
            sense_path, load_path = shortest(sense_id, sense_shunt), shortest(load_id, shunt_id)
            if sense_path is None or load_path is None:
                _unknown(report, "kelvin_branch", "no proven routed sense/load connection to mapped shunt pads", terminal_mapping=terminals)
                continue
            load_edges = {frozenset((a, b)) for a, b in zip(load_path[1], load_path[1][1:])}
            try:
                shunt_rings = [pad_outline(pads[i]) for i in {shunt_id, sense_shunt}]
            except ValueError:
                shunt_rings = []
            common = []
            for a, b in zip(sense_path[1], sense_path[1][1:]):
                edge = combined[a][b]
                midpoint = lerp(a[1][:2], b[1][:2], 0.5)
                if (frozenset((a, b)) in load_edges and edge.get("length_mm", 0) > EPS
                        and edge.get("pad_id") not in {shunt_id, sense_shunt}
                        and not any(point_in_ring(midpoint, r) for r in shunt_rings)):
                    common.append((a, b, edge))
            if common:
                a, b, _ = common[0]
                _violation(report, "kelvin_branch", pads[shunt_id], sum(e["length_mm"] for _, _, e in common), 0.0,
                           "Route the sense branch independently to the shunt pad or verified pad-side net tie.",
                           {"component": motif.get("component"), "side": motif.get("side"), "terminal_mapping": terminals,
                            "shared_load_edges": [{"start": u[1], "end": v[1], **e} for u, v, e in common],
                            "units": "mm shared load-current path outside shunt copper"}, lerp(a[1][:2], b[1][:2], 0.5))


def _constraint_check(snapshot, intent, segments, report, graphs):
    constraints = intent.get("constraints", {})
    for seg in segments:
        c = constraints.get(seg.get("net_name"), {})
        c = {**c, **c.get("layer_widths", {}).get(seg.get("layer"), {})}
        lower = c.get("required_width_mm", c.get("required_width_mils", 0) * MM_PER_MIL)
        upper = c.get("max_width_mm", c.get("max_width_mils", math.inf) * MM_PER_MIL)
        if lower and seg["width_mm"] < lower - EPS and "HIGH_CURRENT_POWER" not in _tags(intent, seg.get("net_name")):
            _violation(report, "required_trace_width", seg, seg["width_mm"], lower,
                       "Use the synthesized trace width on this routed segment.", {"track_id": seg["id"], "units": "mm", "constraints": c})
        if seg["width_mm"] > upper + EPS:
            _violation(report, "maximum_trace_width", seg, seg["width_mm"], upper,
                       "Restore the impedance-qualified maximum trace width.", {"track_id": seg["id"], "units": "mm", "constraints": c})
    rules = intent.get("clearance_matrix", [])
    by_net = defaultdict(list)
    for seg in segments:
        by_net[seg.get("net_name")].append(seg)
    for rule in rules:
        first, second = by_net[rule["net_a"]], by_net[rule["net_b"]]
        limit = rule.get("clearance_mm", rule.get("clearance_mils", 0) * MM_PER_MIL)
        if limit <= 0:
            continue
        index = SpatialIndex()
        for i, seg in enumerate(second):
            index.insert(i, bbox_segment(seg["start"], seg["end"], seg["width_mm"] / 2))
        for seg in first:
            for j in index.query(bbox_segment(seg["start"], seg["end"], seg["width_mm"] / 2 + limit)):
                other = second[j]
                if seg.get("layer") != other.get("layer"):
                    continue
                spacing = max(0.0, segment_distance(seg["start"], seg["end"], other["start"], other["end"]) - (seg["width_mm"] + other["width_mm"]) / 2)
                if spacing < limit - EPS:
                    p, q = _closest_pair(seg["start"], seg["end"], other["start"], other["end"])
                    _violation(report, "constraint_clearance", seg, spacing, limit,
                               "Increase actual copper-edge clearance between these constrained nets.",
                               {"track_id": seg["id"], "other_track_id": other["id"], "other_net": other.get("net_name"), "units": "mm", "rule": rule}, lerp(p, q, 0.5))
    groups = defaultdict(list)
    for net, c in constraints.items():
        if c.get("match_group"):
            groups[c["match_group"]].append(net)
    for group, nets in groups.items():
        lengths = {}
        for net in nets:
            graph = graphs.get(net)
            if graph is None:
                graph = graphs[net] = CopperGraph(snapshot, net)
            if len(graph.pad_nodes) != 2:
                _unknown(report, "length_matching", "branched/unterminated net needs explicit endpoint paths", net_name=net, match_group=group)
                continue
            path = graph.shortest(*list(graph.pad_nodes.values()))
            if path is None:
                _unknown(report, "length_matching", "no proven routed endpoint path", net_name=net, match_group=group)
            else:
                lengths[net] = path["length_mm"]
        if len(lengths) == len(nets) and len(lengths) >= 2:
            mismatch = max(lengths.values()) - min(lengths.values())
            tolerance = min(constraints[n].get("length_tolerance_mm", constraints[n].get("length_tolerance_mils", math.inf) * MM_PER_MIL) for n in nets)
            if mismatch > tolerance + EPS:
                net = min(lengths, key=lengths.get)
                source = next((s for s in segments if s.get("net_name") == net),
                              next(p for p in snapshot.get("pads", []) if p.get("net_name") == net))
                _violation(report, "length_matching", source, mismatch, tolerance,
                           "Match verified endpoint-to-endpoint routed lengths within tolerance.",
                           {"match_group": group, "lengths_mm": lengths, "units": "mm"})


def _critical_connectivity_check(snapshot, intent, report, graphs, policy):
    for net in intent.get("net_classes", {}):
        if not _tags(intent, net) & (FAST | {"SENSITIVE_ANALOG", "HIGH_CURRENT_POWER", "AGGRESSIVE_SWITCHING"}):
            continue
        pads = [p for p in snapshot.get("pads", []) if p.get("net_name") == net]
        if len(pads) < 2:
            _unknown(report, "critical_connectivity", "at least two mapped terminal pads required to assess a critical routed net", net_name=net)
            continue
        graph = graphs.get(net)
        if graph is None:
            graph = graphs[net] = CopperGraph(snapshot, net, policy["arc_error_mm"])
        groups = []
        for component in nx.connected_components(graph.connectivity):
            members = [pid for pid, ns in graph.pad_nodes.items() if any(n in component for n in ns)]
            if members:
                groups.append(sorted(members))
        if len(groups) > 1:
            _unknown(report, "critical_connectivity", "critical terminal pads are not proven copper-connected", net_name=net, terminal_groups=groups)
        if len(graph.pad_nodes) != len(pads):
            _unknown(report, "critical_connectivity", "some critical pad contours are unsupported", net_name=net)


def audit_board_copper(snapshot: dict, intent: dict | None = None, *, policy: dict | None = None) -> dict:
    """Return structured physical findings and independent unknown coverage.

    Violation dictionaries have the exact stable fields requested by the suite.
    ``status=clean`` is possible only with complete coverage and no unknowns.
    """
    intent = intent or {}
    policy = {**DEFAULT_POLICY, **(policy or {})}
    if any(not isinstance(policy[k], (int, float)) or not math.isfinite(policy[k]) or policy[k] <= 0
           for k in DEFAULT_POLICY if k != "maximum_switch_loop_area_mm2"):
        raise ValueError("audit policy distances, widths, angles and arc error must be finite and positive")
    report = {"violations": [], "unknowns": [], "coverage": snapshot.get("coverage", {}),
              "evidence": [], "policy": {**policy, "basis": "configurable engineering heuristics"}}
    for unknown in intent.get("unknowns", []):
        _unknown(report, "classification", str(unknown))
    if not intent.get("net_classes"):
        _unknown(report, "classification", "no electrical intent; copper alone does not identify sensitive or switching nets")
    if snapshot.get("complete") is False or not _coverage_complete(snapshot, ["tracks", "arcs", "vias", "pads", "regions", "layers"]):
        _unknown(report, "extraction", "snapshot coverage incomplete; absence of violations cannot establish a clean board", missing=snapshot.get("missing", []))
    segments = _segments(snapshot, report, policy["arc_error_mm"])
    for region in snapshot.get("regions", []):
        if region.get("poured") is True and (region.get("geometry_complete", True) is not True or not region_geometry_valid(region)):
            _unknown(report, "geometry", "poured region contour/holes incomplete or invalid", region_id=region.get("id"))
    captured_nets = {s.get("net_name") for s in segments} | {p.get("net_name") for section in ("pads", "vias", "regions") for p in snapshot.get(section, [])}
    captured_nets.update(n for n in snapshot.get("nets", []) if isinstance(n, str))
    for net in captured_nets - {None, ""}:
        if not _tags(intent, net) or "UNKNOWN" in _tags(intent, net):
            _unknown(report, "classification", "captured net has no resolved physical electrical class", net_name=net)
    if any("AGGRESSIVE_SWITCHING" in _tags(intent, net) for net in captured_nets) and not any(m.get("kind") == "switching_loop" for m in intent.get("motifs", [])):
        _unknown(report, "switch_loop_area", "commutating-loop terminal mapping unavailable for switching circuitry")
    report["evidence"].append({"geometry": "analytical segments/polygons; bounded-sagitta arc chords",
                               "arc_error_mm": policy["arc_error_mm"], "segments_evaluated": len(segments),
                               "classification": intent.get("evidence", [])})
    _reference_check(snapshot, intent, segments, report, policy)
    _spacing_check(snapshot, intent, segments, report, policy)
    _return_via_check(snapshot, intent, report, policy)
    graphs = {}
    _decoupling_check(snapshot, intent, report, policy, graphs)
    _switch_loop_check(snapshot, intent, segments, report, policy)
    _current_check(snapshot, intent, segments, report)
    _kelvin_check(snapshot, intent, report, policy, graphs)
    _constraint_check(snapshot, intent, segments, report, graphs)
    _critical_connectivity_check(snapshot, intent, report, graphs, policy)
    for net, graph in graphs.items():
        for unknown in graph.unknowns:
            _unknown(report, "routed_geometry", unknown, net_name=net)
    report["status"] = "violations" if report["violations"] else "unknown" if report["unknowns"] else "clean"
    report["coverage_complete"] = not report["unknowns"]
    return report


__all__ = ["audit_board_copper", "net_connectivity", "classify_copper_intent", "CopperGraph", "DEFAULT_POLICY"]
