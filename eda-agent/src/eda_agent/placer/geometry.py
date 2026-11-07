# SPDX-License-Identifier: MIT
"""Small geometry helpers shared by the placer.

Coordinate convention (identical to KiCad): millimetres, +X right, +Y *down*,
angles in degrees, positive angle = counter-clockwise as seen on screen.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)


def norm_angle(deg: float) -> float:
    deg = math.fmod(deg, 360.0)
    if deg < 0:
        deg += 360.0
    if abs(deg - 360.0) < 1e-9:
        deg = 0.0
    return round(deg, 6)


def rotate(x: float, y: float, deg: float) -> Point:
    """Rotate a local vector by ``deg`` (screen-CCW with +Y down)."""
    if deg == 0:
        return x, y
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def transform(local: Point, origin: Point, deg: float) -> Point:
    dx, dy = rotate(local[0], local[1], deg)
    return origin[0] + dx, origin[1] + dy


def box_of(points: Iterable[Point]) -> Box | None:
    xs, ys = [], []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def box_union(a: Box | None, b: Box | None) -> Box | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def box_expand(b: Box, d: float) -> Box:
    return b[0] - d, b[1] - d, b[2] + d, b[3] + d


def box_overlap_area(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def box_center(b: Box) -> Point:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def box_area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def rotated_box(local: Box, origin: Point, deg: float) -> Box:
    corners = [(local[0], local[1]), (local[2], local[1]), (local[2], local[3]), (local[0], local[3])]
    return box_of(transform(c, origin, deg) for c in corners)  # type: ignore[return-value]


def arc_points(start: Point, mid: Point, end: Point, segments: int = 12) -> list[Point]:
    """Approximate a three-point arc with a polyline (inclusive of endpoints)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def sweep(a, b):
        s = b - a
        while s <= -math.pi:
            s += 2 * math.pi
        while s > math.pi:
            s -= 2 * math.pi
        return s

    total = sweep(a1, a2) + sweep(a2, a3)
    return [
        (ux + r * math.cos(a1 + total * i / segments), uy + r * math.sin(a1 + total * i / segments))
        for i in range(segments + 1)
    ]


def segments_cross(p1: Point, p2: Point, q1: Point, q2: Point, eps: float = 1e-9) -> bool:
    """Proper intersection of two segments (shared endpoints do not count)."""

    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    return ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    )


def point_in_polygon(pt: Point, poly: Sequence[Point]) -> bool:
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def polygon_area(poly: Sequence[Point]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2
