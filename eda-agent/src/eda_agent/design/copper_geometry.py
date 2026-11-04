# SPDX-License-Identifier: MIT
"""Small, deterministic 2-D copper geometry kernel (all distances in mm).

No bounding rectangle is treated as copper. Rings may be concave and regions
may have holes. Arcs are flattened with a declared, bounded sagitta error;
their analytical lengths remain available independently of the approximation.
"""
from __future__ import annotations

import math
from collections import defaultdict

EPS = 1e-9


def xy(value):
    if isinstance(value, dict):
        x, y = value.get("x_mm", value.get("x")), value.get("y_mm", value.get("y"))
        if x is None or y is None:
            raise ValueError("physical XY coordinates unavailable")
        return float(x), float(y)
    return float(value[0]), float(value[1])


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def lerp(a, b, t):
    return a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def project(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / d2)) if d2 else 0.0
    return lerp(a, b, t), t


def point_segment_distance(point, a, b):
    return distance(point, project(point, a, b)[0])


def segment_intersection_parameters(a, b, c, d):
    """Parameters on AB at intersections, including collinear overlap ends."""
    r, s = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
    q = c[0] - a[0], c[1] - a[1]
    den = cross(r, s)
    if abs(den) > EPS:
        t, u = cross(q, s) / den, cross(q, r) / den
        return [max(0.0, min(1.0, t))] if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS else []
    if abs(cross(q, r)) > EPS:
        return []
    rr = r[0] ** 2 + r[1] ** 2
    if rr < EPS * EPS:
        return [0.0] if point_segment_distance(a, c, d) <= EPS else []
    ts = [((p[0] - a[0]) * r[0] + (p[1] - a[1]) * r[1]) / rr for p in (c, d)]
    lo, hi = max(0.0, min(ts)), min(1.0, max(ts))
    return [lo, hi] if lo <= hi + EPS else []


def segment_distance(a, b, c, d):
    if segment_intersection_parameters(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def ring_edges(ring):
    points = [xy(p) for p in ring]
    return zip(points, points[1:] + points[:1])


def point_in_ring(point, ring, boundary=True):
    inside = False
    for a, b in ring_edges(ring):
        if point_segment_distance(point, a, b) <= EPS:
            return boundary
        if (a[1] > point[1]) != (b[1] > point[1]):
            x = a[0] + (point[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if point[0] < x:
                inside = not inside
    return inside


def point_in_region(point, region):
    holes=region.get('holes',[])
    index=region.get('_hole_index')
    if isinstance(index,SpatialIndex):
        holes=[holes[i] for i in index.query((point[0]-EPS,point[1]-EPS,point[0]+EPS,point[1]+EPS))]
    return point_in_ring(point, region.get("outline", [])) and not any(
        point_in_ring(point, hole, boundary=False) for hole in holes)


def indexed_region(region):
    """Ephemeral exact broad phase; never serialized into snapshot evidence."""
    if not region.get('holes'):return region
    index=SpatialIndex()
    for i,ring in enumerate(region['holes']):
        points=[xy(p) for p in ring]
        index.insert(i,(min(x for x,y in points),min(y for x,y in points),max(x for x,y in points),max(y for x,y in points)))
    return dict(region,_hole_index=index)


def segment_region_intervals(a, b, region):
    """Exact centreline parameter intervals inside a polygon with holes."""
    ts = [0.0, 1.0]
    holes=region.get('holes',[])
    index=region.get('_hole_index')
    if isinstance(index,SpatialIndex):
        holes=[holes[i] for i in index.query((min(a[0],b[0])-EPS,min(a[1],b[1])-EPS,max(a[0],b[0])+EPS,max(a[1],b[1])+EPS))]
    for ring in [region.get("outline", [])] + holes:
        for c, d in ring_edges(ring):
            ts.extend(segment_intersection_parameters(a, b, c, d))
    ts = sorted(set(round(t, 12) for t in ts))
    return [(lo, hi) for lo, hi in zip(ts, ts[1:])
            if hi - lo > EPS and point_in_region(lerp(a, b, (lo + hi) / 2), region)]


def merge_intervals(intervals):
    out = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + EPS:
            out[-1] = out[-1][0], max(hi, out[-1][1])
        else:
            out.append((lo, hi))
    return out


def uncovered_intervals(a, b, regions):
    merged = merge_intervals(i for r in regions for i in segment_region_intervals(a, b, r))
    out, cursor = [], 0.0
    for lo, hi in merged:
        if lo > cursor + EPS:
            out.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < 1.0 - EPS:
        out.append((cursor, 1.0))
    return out


def polygon_area(ring):
    return abs(sum(a[0] * b[1] - a[1] * b[0] for a, b in ring_edges(ring))) / 2


def region_geometry_valid(region):
    """Reject self crossings, collapsed rings and unsupported nested holes.

    The edge grid keeps validation local for dense contours. No guessed centre
    point is used to decide whether a hole belongs to a concave outline.
    """
    rings = []
    for raw in [region.get("outline", [])] + region.get("holes", []):
        ring = [xy(p) for p in raw]
        if len(ring) > 1 and distance(ring[0], ring[-1]) <= EPS:
            ring.pop()
        if len(ring) < 3 or polygon_area(ring) <= EPS * EPS:
            return False
        if any(distance(a, b) <= EPS for a, b in ring_edges(ring)):
            return False
        rings.append(ring)
    if not rings:
        return False
    index, edges = SpatialIndex(), []
    for ri, ring in enumerate(rings):
        for ei, (a, b) in enumerate(ring_edges(ring)):
            key = len(edges)
            for other in index.query(bbox_segment(a, b)):
                rj, ej, c, d, count = edges[other]
                if ri == rj and (ei - ej) % count in {0, 1, count - 1}:
                    continue
                if segment_intersection_parameters(a, b, c, d):
                    return False
            edges.append((ri, ei, a, b, len(ring)))
            index.insert(key, bbox_segment(a, b))
    for i, hole in enumerate(rings[1:]):
        if not point_in_ring(hole[0], rings[0], boundary=False):
            return False
        if any(point_in_ring(hole[0], other) or point_in_ring(other[0], hole)
               for other in rings[1:i + 1]):
            return False
    return True


def pad_outline(pad, error_mm=0.002):
    """Actual rotated rectangle/ellipse/rounded-rectangle, never its AABB."""
    x, y = xy(pad)
    w, h = float(pad.get("width_mm", 0)), float(pad.get("height_mm", 0))
    shape = str(pad.get("shape", "rectangle")).lower()
    angle = math.radians(float(pad.get("rotation_deg", 0)))
    if shape in {"round", "circle", "ellipse", "oval", "1"}:
        radius = max(w, h) / 2
        n = max(16, math.ceil(math.pi / math.acos(max(-1.0, 1 - min(error_mm, radius) / radius)))) if radius else 16
        if shape == "oval" and abs(w - h) > EPS:
            # Capsule: straight sides plus two semicircles.
            r, dx, dy = min(w, h) / 2, max(0, (w - h) / 2), max(0, (h - w) / 2)
            pts = [(dx * (1 if math.cos(t) >= 0 else -1) + r * math.cos(t),
                    dy * (1 if math.sin(t) >= 0 else -1) + r * math.sin(t))
                   for t in (2 * math.pi * i / n for i in range(n))]
        else:
            pts = [(w / 2 * math.cos(2 * math.pi * i / n), h / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)]
    elif shape in {"rectangle", "rect", "2"}:
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    elif shape in {"rounded_rectangle", "roundrect", "rounded_rect"}:
        if "corner_radius_mm" not in pad:
            raise ValueError("rounded rectangle pad corner radius unavailable")
        radius = float(pad["corner_radius_mm"])
        if not 0 <= radius <= min(w, h) / 2:
            raise ValueError("rounded rectangle corner radius outside pad dimensions")
        if radius <= EPS:
            pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        else:
            n = max(2, math.ceil((math.pi / 2) / (2 * math.acos(max(-1, 1 - min(error_mm, radius) / radius)))))
            pts = []
            for cx, cy, start in [(w / 2 - radius, h / 2 - radius, 0),
                                  (-w / 2 + radius, h / 2 - radius, 90),
                                  (-w / 2 + radius, -h / 2 + radius, 180),
                                  (w / 2 - radius, -h / 2 + radius, 270)]:
                pts.extend((cx + radius * math.cos(math.radians(start) + math.pi / 2 * i / n),
                            cy + radius * math.sin(math.radians(start) + math.pi / 2 * i / n)) for i in range(n + 1))
    elif pad.get("outline"):
        return [xy(p) for p in pad["outline"]]
    else:
        raise ValueError(f"unsupported pad shape: {shape}")
    ca, sa = math.cos(angle), math.sin(angle)
    return [(x + a * ca - b * sa, y + a * sa + b * ca) for a, b in pts]


def arc_segments(arc, error_mm=0.002):
    """Flatten an analytical arc; chord sagitta is <= error_mm."""
    if error_mm <= 0:
        raise ValueError("arc error must be positive")
    center = xy(arc.get("center", [arc.get("center_x_mm", 0), arc.get("center_y_mm", 0)]))
    r = float(arc["radius_mm"])
    if r <= 0:
        raise ValueError("arc radius must be positive")
    start, end = float(arc.get("start_angle_deg", 0)), float(arc.get("end_angle_deg", 360))
    sweep = arc.get("sweep_angle_deg")
    if sweep is None:
        sweep = (end - start) % 360
        if abs(end - start) >= 360 - EPS:
            sweep = 360.0
        if arc.get("clockwise"):
            sweep = -((start - end) % 360 or 360)
    sweep = math.radians(float(sweep))
    n = max(1, math.ceil(abs(sweep) / (2 * math.acos(max(-1, 1 - min(error_mm, r) / r)))))
    if n > 100000:
        raise ValueError("arc precision requires more than 100000 segments")
    angles = [math.radians(start) + sweep * i / n for i in range(n + 1)]
    pts = [(center[0] + r * math.cos(t), center[1] + r * math.sin(t)) for t in angles]
    return [{**arc, "start": a, "end": b, "arc_id": arc.get("id"), "arc_error_mm": error_mm,
             "analytical_length_mm": abs(sweep) * r / n,
             "id": f"{arc.get('id', 'arc')}:{i}"} for i, (a, b) in enumerate(zip(pts, pts[1:]))]


def bbox_segment(a, b, margin=0):
    return min(a[0], b[0]) - margin, min(a[1], b[1]) - margin, max(a[0], b[0]) + margin, max(a[1], b[1]) + margin


def bbox_overlap(a, b):
    return a[0] <= b[2] + EPS and a[2] + EPS >= b[0] and a[1] <= b[3] + EPS and a[3] + EPS >= b[1]


class SpatialIndex:
    """Uniform hash grid with bounded insertion for exceptionally long items."""
    def __init__(self, cell_mm=2.0):
        self.cell_mm = cell_mm
        self.cells = defaultdict(set)
        self.boxes = {}
        self.wide = set()

    def _cells(self, box):
        x0, y0, x1, y1 = (math.floor(v / self.cell_mm) for v in box)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096:
            return None
        return ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def insert(self, key, box):
        self.boxes[key] = box
        cells = self._cells(box)
        if cells is None:
            self.wide.add(key)
        else:
            for cell in cells:
                self.cells[cell].add(key)

    def query(self, box):
        cells = self._cells(box)
        candidates = set(self.boxes) if cells is None else set(self.wide)
        if cells is not None:
            for cell in cells:
                candidates.update(self.cells.get(cell, ()))
        return [key for key in sorted(candidates) if bbox_overlap(box, self.boxes[key])]


def parallel_coupling(a, b, c, d, width_a, width_b, spacing_limit, angle_deg=5.0):
    """Length of parallel overlap violating a true copper-edge spacing limit.

    For nearly parallel lines separation varies linearly over the overlap;
    clipping that linear function gives the coupled length, not merely the
    distance between segment centres or the distance at one endpoint.
    """
    length, other_length = distance(a, b), distance(c, d)
    if min(length, other_length) < EPS:
        return None
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    vx, vy = (d[0] - c[0]) / other_length, (d[1] - c[1]) / other_length
    if abs(ux * vx + uy * vy) < math.cos(math.radians(angle_deg)):
        return None
    sc, sd = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy, (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(0.0, min(sc, sd)), min(length, max(sc, sd))
    if hi - lo <= EPS:
        return None
    tc, td = (c[0] - a[0]) * -uy + (c[1] - a[1]) * ux, (d[0] - a[0]) * -uy + (d[1] - a[1]) * ux
    slope = (td - tc) / (sd - sc)
    intercept = tc - slope * sc
    threshold = spacing_limit + (width_a + width_b) / 2
    if abs(slope) <= EPS:
        if abs(intercept) >= threshold - EPS:
            return None
    else:
        roots = sorted(((-threshold - intercept) / slope, (threshold - intercept) / slope))
        lo, hi = max(lo, roots[0]), min(hi, roots[1])
    if hi - lo <= EPS:
        return None
    closest = min(abs(intercept + slope * lo), abs(intercept + slope * hi))
    if slope and lo <= -intercept / slope <= hi:
        closest = 0.0
    return {"coupled_length_mm": hi - lo, "edge_spacing_mm": max(0.0, closest - (width_a + width_b) / 2),
            "overlap_interval_mm": (lo, hi),
            "point": (a[0] + ux * (lo + hi) / 2, a[1] + uy * (lo + hi) / 2)}
