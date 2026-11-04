"""Exact rectilinear board subtraction; no guessed contours or CAD writes.

SPDX-License-Identifier: MIT
"""
from collections import defaultdict
from fractions import Fraction
import math


def subtract_rectilinear_cutouts(outline, cutouts, *, max_cells=65536,
                                ignore_full_board_artifacts=False):
    """Return one polygon-with-holes, or reject unsupported/disconnected results.

    All coordinates are exact rational representations of the supplied decimals.
    No snapping, buffer repair or tolerance can erase a narrow physical feature.
    """
    from eda_agent.design.copper_geometry import region_geometry_valid

    def ring(raw):
        if not isinstance(raw, (list, tuple)) or not 3 <= len(raw) <= 10000:
            raise ValueError('Invalid bounded board contour')
        points = []
        for point in raw:
            if len(point) != 2 or any(type(v) not in (float, int) or not math.isfinite(v) for v in point):
                raise ValueError('Nonfinite board contour')
            points.append(tuple(Fraction(str(v)) for v in point))
        if points[-1] == points[0]:
            points.pop()
        if not region_geometry_valid({'outline': [(float(x), float(y)) for x, y in points]}):
            raise ValueError('Invalid board contour')
        if any(a == b or (a[0] != b[0] and a[1] != b[1])
               for a, b in zip(points, points[1:] + points[:1])):
            raise ValueError('Only exact rectilinear contour subtraction is supported')
        return points

    subject = ring(outline)
    masks = [ring(r) for r in cutouts]
    xs = sorted({x for r in [subject] + masks for x, _ in r})
    ys = sorted({y for r in [subject] + masks for _, y in r})
    if (len(xs)-1)*(len(ys)-1) > max_cells:
        raise ValueError('Board contour subtraction exceeds cell budget')

    def inside(x, y, r):
        result = False
        for a, b in zip(r, r[1:] + r[:1]):
            if (a[1] > y) != (b[1] > y) and x < a[0] + (y-a[1])*(b[0]-a[0])/(b[1]-a[1]):
                result = not result
        return result

    ignored = []
    if ignore_full_board_artifacts:
        # Explicit P1 policy requested by the operator. Area does not prove a
        # contour is a nonphysical artifact; keep the rejected raw evidence.
        subject_area = Fraction(0)
        overlaps = [Fraction(0) for _ in masks]
        for i in range(len(xs)-1):
            for j in range(len(ys)-1):
                x, y = (xs[i]+xs[i+1])/2, (ys[j]+ys[j+1])/2
                if inside(x, y, subject):
                    area = (xs[i+1]-xs[i])*(ys[j+1]-ys[j])
                    subject_area += area
                    for k, mask in enumerate(masks):
                        if inside(x, y, mask):
                            overlaps[k] += area
        ignored = [dict(index=k, overlap_ratio=float(a/subject_area),
                        warning='ignored_full_board_cutout_artifact',
                        interpretation='operator_authorized_area_heuristic; physical_semantics_unverified',
                        raw_contour=cutouts[k])
                   for k,a in enumerate(overlaps) if a > subject_area*Fraction(9,10)]
        masks = [mask for k,mask in enumerate(masks) if k not in {r['index'] for r in ignored}]
    cells = set()
    area = Fraction(0)
    for i in range(len(xs)-1):
        for j in range(len(ys)-1):
            x, y = (xs[i]+xs[i+1])/2, (ys[j]+ys[j+1])/2
            if inside(x, y, subject) and not any(inside(x, y, r) for r in masks):
                cells.add((i, j))
                area += (xs[i+1]-xs[i])*(ys[j+1]-ys[j])
    if not cells:
        raise ValueError('Cutouts remove the entire board')
    edges = {}
    incoming = defaultdict(int)
    for i, j in sorted(cells):
        corners = [(xs[i],ys[j]), (xs[i+1],ys[j]), (xs[i+1],ys[j+1]), (xs[i],ys[j+1])]
        for k, neighbor in enumerate(((i,j-1), (i+1,j), (i,j+1), (i-1,j))):
            if neighbor in cells:
                continue
            a, b = corners[k], corners[(k+1)%4]
            if a in edges:
                raise ValueError('Non-manifold board boundary')
            edges[a] = b
            incoming[b] += 1
    if set(incoming) != set(edges) or any(n != 1 for n in incoming.values()):
        raise ValueError('Ambiguous board boundary')
    loops = []
    while edges:
        start = min(edges)
        p, loop = start, []
        while True:
            loop.append(p)
            p = edges.pop(p)
            if p == start:
                break
        # Remove collinear vertices without changing the represented set.
        loop = [p for i,p in enumerate(loop)
                if not (loop[i-1][0] == p[0] == loop[(i+1)%len(loop)][0]
                        or loop[i-1][1] == p[1] == loop[(i+1)%len(loop)][1])]
        signed = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(loop,loop[1:]+loop[:1]))/2
        loops.append((signed, loop))
    if sum(a for a, _ in loops) != area:
        raise ValueError('Board contour area verification failed')
    outer = [r for a, r in loops if a > 0]
    holes = [r for a, r in loops if a < 0]
    if len(outer) != 1:
        raise ValueError('Disconnected boards are unsupported by the placement contract')
    convert = lambda r: [[float(x),float(y)] for x,y in r]
    result = {'outline':convert(outer[0]),'holes':[convert(r) for r in holes]}
    if not region_geometry_valid(result):
        raise ValueError('Subtracted board is unsupported by the placement contract')
    if ignore_full_board_artifacts:
        result.update(ignored_full_board_cutouts=len(ignored), warnings=ignored,
                      interpretation_policy='placement-evidence-p1-v1')
    return result
