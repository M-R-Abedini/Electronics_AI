# SPDX-License-Identifier: MIT
"""S6a short differential-pair router (architecture 7.1, plan F3 task 5).

A pair is routed as one centreline on a grid, then split into two legs:

1. terminals: places where a pad of each leg sits side by side (connector
   pins, the two parts of an ESD pair, IC pins); they are chained in the
   order that joins them with the shortest total distance;
2. per segment between two terminals: breakout stubs from each pad to a
   start point outside the part, A* (Rust core) for the centreline on a grid
   whose obstacles are grown by the clearance plus half the pair's width, so a
   free cell holds both legs; a layer change needs a cell where both vias fit;
3. legs: the centreline offset by half the leg pitch on each side (mitre
   joins keep 45 degree corners); vias side by side across the centreline.

Everything is measured on the result (lengths, skew, vias per leg) and
written as fixed copper; the route check and KiCad DRC judge it like any
other copper.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..geometry import transform
from ..ir import Board, Footprint, Pad

GRID_MM = 0.05  # declared: centreline grid
WINDOW_MM = 6.0  # declared: grid window margin around the terminals
BREAKOUT_MM = 4.0  # declared: the coupled section starts within this distance of a terminal
VIA_COST, TURN45, TURN90 = 40.0, 2.0, 6.0  # in grid steps (via = 2 mm of detour)


@dataclass
class PairRule:
    width: float
    gap: float
    clearance: float
    via_d: float
    via_drill: float
    skew_max: float = 0.15

    @property
    def pitch(self) -> float:  # leg centre to leg centre
        return self.width + self.gap

    @property
    def via_pitch(self) -> float:  # via centre to via centre across the pair
        return max(self.via_d + self.clearance, self.pitch)


@dataclass
class Terminal:
    pads: dict[str, tuple[Footprint, Pad]]  # leg ("pos"/"neg") -> pad
    layer: str

    def xy(self, leg: str) -> tuple[float, float]:
        fp, pad = self.pads[leg]
        return fp.pad_world(pad)

    @property
    def mid(self) -> tuple[float, float]:
        (ax, ay), (bx, by) = self.xy("pos"), self.xy("neg")
        return (ax + bx) / 2, (ay + by) / 2

    @property
    def refs(self) -> str:
        return "/".join(sorted({fp.ref for fp, _p in self.pads.values()}))


@dataclass
class PairRoute:
    pos: str
    neg: str
    tracks: list[dict] = field(default_factory=list)
    vias: list[dict] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    failed: str | None = None

    def lengths(self) -> dict[str, float]:
        out = {"pos": 0.0, "neg": 0.0}
        for t in self.tracks:
            leg = "pos" if t["net"] == self.pos else "neg"
            out[leg] += LineString(t["points"]).length
        return {k: round(v, 3) for k, v in out.items()}

    def summary(self) -> dict:
        ln = self.lengths()
        vias = {"pos": sum(v["net"] == self.pos for v in self.vias), "neg": sum(v["net"] == self.neg for v in self.vias)}
        return {"pair": [self.pos, self.neg], "routed": self.failed is None, "failed": self.failed,
                "length_mm": ln, "skew_mm": round(abs(ln["pos"] - ln["neg"]), 3), "vias": vias,
                "segments": self.segments}


# --------------------------------------------------------------------------- geometry helpers
def pad_polygon(fp: Footprint, pad: Pad, grow: float = 0.0) -> Polygon:
    cx, cy = fp.pad_world(pad)
    w, h = pad.size
    ang = fp.rotation + pad.rotation
    if pad.shape in ("circle", "oval") and abs(w - h) < 1e-6:
        return Point(cx, cy).buffer(w / 2 + grow, quad_segs=8)
    corners = [transform(c, (cx, cy), ang) for c in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
    return Polygon(corners).buffer(grow, join_style="mitre") if grow else Polygon(corners)


def pad_layers(fp: Footprint, pad: Pad, layers: list[str]) -> list[str]:
    if pad.kind in ("thru_hole", "np_thru_hole") or "*.Cu" in pad.layers:
        return list(layers)
    named = [lay for lay in pad.layers if lay.endswith(".Cu")]
    if named:
        return [lay for lay in named if lay in layers]
    return [("F.Cu" if fp.side == "F" else "B.Cu")]


def _outward(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Unit vector along the pad's long axis, pointing away from the part's centre (the way a pin escapes).

    The axis goes through the same ``transform`` as the pad itself, so it follows any rotation and side.
    """
    px, py = fp.pad_world(pad)
    dx, dy = px - fp.x, py - fp.y
    w, h = pad.size
    if abs(w - h) < 1e-6:
        ax, ay = dx, dy
    else:
        ox, oy = transform((0.0, 0.0), (0.0, 0.0), fp.rotation + pad.rotation)
        ux, uy = transform((1.0, 0.0) if w > h else (0.0, 1.0), (0.0, 0.0), fp.rotation + pad.rotation)
        ax, ay = ux - ox, uy - oy
        if ax * dx + ay * dy < 0:
            ax, ay = -ax, -ay
    n = math.hypot(ax, ay) or 1.0
    return ax / n, ay / n


# --------------------------------------------------------------------------- terminals
def terminals(board: Board, pos: str, neg: str, *, near_mm: float = 2.0) -> list[Terminal]:
    """Side-by-side pad pairs of the two legs, at most one per part (IC pins, connector pins, ESD pair)."""
    copper = [cl.canonical for cl in board.copper_layers]
    pads = {leg: [(fp, p) for fp in board.footprints for p in fp.pads if p.net == net]
            for leg, net in (("pos", pos), ("neg", neg))}
    used: set[int] = set()
    out = []
    cands = []
    for fp_a, pa in pads["pos"]:
        for fp_b, pb in pads["neg"]:
            la, lb = pad_layers(fp_a, pa, copper), pad_layers(fp_b, pb, copper)
            common = [lay for lay in la if lay in lb]
            if not common:
                continue
            (xa, ya), (xb, yb) = fp_a.pad_world(pa), fp_b.pad_world(pb)
            d = math.hypot(xa - xb, ya - yb)
            if d <= near_mm:
                cands.append((d, fp_a.ref != fp_b.ref, id(pa), id(pb), fp_a, pa, fp_b, pb, common[0]))
    for d, _two_parts, ia, ib, fp_a, pa, fp_b, pb, layer in sorted(cands, key=lambda c: (c[1], c[0])):
        if ia in used or ib in used:
            continue
        used |= {ia, ib}
        out.append(Terminal({"pos": (fp_a, pa), "neg": (fp_b, pb)}, layer))
    return out


def chain(terms: list[Terminal]) -> list[Terminal]:
    """Order the terminals as a path: start at an end (the farthest pair), then nearest neighbour."""
    if len(terms) <= 2:
        return terms
    best = max(((a, b) for a in range(len(terms)) for b in range(len(terms)) if a < b),
               key=lambda ab: math.dist(terms[ab[0]].mid, terms[ab[1]].mid))
    order, rest = [terms[best[0]]], [t for i, t in enumerate(terms) if i != best[0]]
    while rest:
        nxt = min(rest, key=lambda t: math.dist(order[-1].mid, t.mid))
        order.append(nxt)
        rest.remove(nxt)
    return order


# --------------------------------------------------------------------------- obstacle grids
class Window:
    def __init__(self, board: Board, box: tuple[float, float, float, float], layers: list[str], res: float = GRID_MM):
        x0, y0, x1, y1 = box
        ob = board.outline_box or box
        x0, y0, x1, y1 = max(x0, ob[0]), max(y0, ob[1]), min(x1, ob[2]), min(y1, ob[3])
        self.x0, self.y0, self.res = x0, y0, res
        self.nx, self.ny = math.ceil((x1 - x0) / res) + 1, math.ceil((y1 - y0) / res) + 1
        self.layers = layers
        xs = x0 + np.arange(self.nx) * res
        ys = y0 + np.arange(self.ny) * res
        self.X, self.Y = np.meshgrid(xs, ys)  # [y, x]

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return round((x - self.x0) / self.res), round((y - self.y0) / self.res)

    def xy(self, i: int, j: int) -> tuple[float, float]:
        return self.x0 + i * self.res, self.y0 + j * self.res

    def raster(self, geom) -> np.ndarray:
        out = np.zeros((self.ny, self.nx), bool)
        if geom is None or geom.is_empty:
            return out
        for g in getattr(geom, "geoms", [geom]):
            gx0, gy0, gx1, gy1 = g.bounds
            i0, j0 = self.cell(gx0, gy0)
            i1, j1 = self.cell(gx1, gy1)
            i0, j0 = max(i0 - 1, 0), max(j0 - 1, 0)
            i1, j1 = min(i1 + 1, self.nx - 1), min(j1 + 1, self.ny - 1)
            if i1 < i0 or j1 < j0:
                continue
            sub = shapely.contains_xy(g, self.X[j0:j1 + 1, i0:i1 + 1], self.Y[j0:j1 + 1, i0:i1 + 1])
            out[j0:j1 + 1, i0:i1 + 1] |= sub
        return out


def _disk(radius_cells: float) -> np.ndarray:
    r = math.ceil(radius_cells)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= radius_cells * radius_cells + 1e-9


def obstacles(board: Board, win: Window, rule: PairRule, own: set[str], extra: list[dict]) -> dict[str, np.ndarray]:
    """Copper of other nets per layer, grown by the clearance (raw: the space a leg edge may not enter)."""
    copper = [cl.canonical for cl in board.copper_layers]
    per = {lay: [] for lay in win.layers}
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net in own:
                continue
            g = pad_polygon(fp, pad, rule.clearance)
            for lay in pad_layers(fp, pad, copper):
                if lay in per:
                    per[lay].append(g)
            if pad.drill and pad.kind == "np_thru_hole":
                for geoms in per.values():
                    geoms.append(g)
    for item in extra:  # fixed copper already on the board (macros, earlier pairs)
        if item.get("net") in own:
            continue
        if "points" in item:
            g = LineString(item["points"]).buffer(item["width"] / 2 + rule.clearance)
            if item["layer"] in per:
                per[item["layer"]].append(g)
        else:
            g = Point(item["x"], item["y"]).buffer(item["diameter"] / 2 + rule.clearance)
            for geoms in per.values():
                geoms.append(g)
    edge = None
    if board.outline:
        inside = unary_union([Polygon(o.outer, o.holes) for o in board.outline]).buffer(-rule.clearance)
        box = Polygon([(win.x0 - 1, win.y0 - 1), (win.x0 + win.nx * win.res + 1, win.y0 - 1),
                       (win.x0 + win.nx * win.res + 1, win.y0 + win.ny * win.res + 1),
                       (win.x0 - 1, win.y0 + win.ny * win.res + 1)])
        edge = box.difference(inside)
    out = {}
    for lay, geoms in per.items():
        g = unary_union(geoms) if geoms else None
        if edge is not None:
            g = edge if g is None else g.union(edge)
        out[lay] = win.raster(g)
    return out


# --------------------------------------------------------------------------- routing
def route_pair(board: Board, pos: str, neg: str, rule: PairRule, *, fixed: list[dict] | None = None) -> PairRoute:
    import placer_core

    res = PairRoute(pos, neg)
    terms = chain(terminals(board, pos, neg))
    if len(terms) < 2:
        res.failed = f"found {len(terms)} side-by-side terminal(s); need 2"
        return res
    layers = [cl.canonical for cl in board.copper_layers if cl.canonical in ("F.Cu", "B.Cu")]  # outer layers
    fixed = list(fixed or [])
    for a, b in itertools.pairwise(terms):
        seg = _route_segment(board, a, b, rule, layers, fixed, placer_core)
        res.segments.append(seg["info"])
        if seg["failed"]:
            res.failed = seg["failed"]
            return res
        res.tracks += seg["tracks"]
        res.vias += seg["vias"]
        fixed += seg["tracks"] + seg["vias"]
    return res


def _pair_start(win: Window, blocked: np.ndarray, term: Terminal, toward: tuple[float, float]) -> tuple | None:
    """The free centreline cell nearest the terminal (within BREAKOUT_MM), leaning toward the other end."""
    mx, my = term.mid
    dx, dy = win.X - mx, win.Y - my
    dist = np.hypot(dx, dy)
    score = dist - 0.5 * (dx * toward[0] + dy * toward[1])
    score[blocked | (dist > BREAKOUT_MM)] = np.inf
    j, i = np.unravel_index(int(np.argmin(score)), score.shape)
    return None if not np.isfinite(score[j, i]) else (int(i), int(j))


def _cells_to_copper(win: Window, path, layers, net: str, rule: PairRule) -> tuple[list[dict], list[dict]]:
    """A single-trace grid path as tracks (one per layer run) and vias at layer changes."""
    tracks, vias, run, run_layer = [], [], [], None
    for i, j, k in path:
        p = win.xy(i, j)
        if run_layer is not None and layers[k] != run_layer:
            if len(run) > 1:
                tracks.append({"layer": run_layer, "width": rule.width, "net": net, "points": _simplify(run)})
            vias.append({"x": p[0], "y": p[1], "diameter": rule.via_d, "drill": rule.via_drill, "net": net,
                         "layers": ["F.Cu", "B.Cu"]})
            run = []
        run_layer = layers[k]
        run.append(p)
    if len(run) > 1:
        tracks.append({"layer": run_layer, "width": rule.width, "net": net, "points": _simplify(run)})
    return tracks, vias


def _breakout(win: Window, raw: dict[str, np.ndarray], layers, own_copper: list[dict], start: tuple, end: tuple,
              net: str, rule: PairRule, core) -> tuple[list[dict], list[dict]] | None:
    """One leg from its pad to its place in the pair, at single-trace width (A*; a via if it must)."""
    blocked = {}
    for lay in layers:
        m = raw[lay].copy()
        for item in own_copper:  # the other leg is an obstacle like any other net
            if "points" in item:
                if item["layer"] != lay:
                    continue
                g = LineString(item["points"]).buffer(item["width"] / 2 + rule.clearance)
            else:
                g = Point(item["x"], item["y"]).buffer(item["diameter"] / 2 + rule.clearance)
            m |= win.raster(g)
        blocked[lay] = ndimage.binary_dilation(m, structure=_disk(rule.width / 2 / win.res))
    any_raw = np.zeros_like(next(iter(raw.values())))
    for m in raw.values():
        any_raw |= m
    via_ok = ~ndimage.binary_dilation(any_raw, structure=_disk(rule.via_d / 2 / win.res))
    li = {lay: k for k, lay in enumerate(layers)}
    (si, sj, sl), (ei, ej, el) = start, end
    for i, j, lay in (start, end):  # an endpoint may sit inside the grown pad of its own net
        blocked[lay][max(j - 1, 0):j + 2, max(i - 1, 0):i + 2] = False
    stack = np.stack([blocked[lay] for lay in layers]).astype(np.uint8)
    path = core.grid_astar(win.nx, win.ny, len(layers), stack.tobytes(), via_ok.astype(np.uint8).tobytes(),
                           [(si, sj, li[sl])], [(ei, ej, li[el])], VIA_COST, TURN45, TURN90, 4_000_000)
    if path is None:
        return None
    return _cells_to_copper(win, path, layers, net, rule)


def _route_segment(board, a: Terminal, b: Terminal, rule: PairRule, layers, fixed, core) -> dict:
    xs = [a.mid[0], b.mid[0]]
    ys = [a.mid[1], b.mid[1]]
    win = Window(board, (min(xs) - WINDOW_MM, min(ys) - WINDOW_MM, max(xs) + WINDOW_MM, max(ys) + WINDOW_MM), layers)
    own = {a.pads["pos"][1].net, a.pads["neg"][1].net}
    raw = obstacles(board, win, rule, own, fixed)
    half = (rule.pitch / 2 + rule.width / 2) / win.res  # centreline to the outer edge of a leg
    # the coupled section also keeps off the terminals' own pads (a leg must not run over the other leg's pad)
    own_pads = {lay: np.zeros_like(m) for lay, m in raw.items()}
    for term in (a, b):
        for fp, pad in term.pads.values():
            g = pad_polygon(fp, pad, rule.clearance)
            for lay in pad_layers(fp, pad, layers):
                own_pads[lay] |= win.raster(g)
    center_blocked = {lay: ndimage.binary_dilation(m | own_pads[lay], structure=_disk(half)) for lay, m in raw.items()}
    via_reach = (rule.via_pitch / 2 + rule.via_d / 2) / win.res
    any_raw = np.zeros_like(next(iter(raw.values())))
    for lay, m in raw.items():
        any_raw |= m | own_pads[lay]
    via_ok = ~ndimage.binary_dilation(any_raw, structure=_disk(via_reach))
    info = {"from": a.refs, "to": b.refs}
    direction = np.subtract(b.mid, a.mid)
    norm = float(np.hypot(*direction)) or 1.0
    toward = (direction[0] / norm, direction[1] / norm)
    sa = _pair_start(win, center_blocked[a.layer], a, toward)
    sb = _pair_start(win, center_blocked[b.layer], b, (-toward[0], -toward[1]))
    if sa is None or sb is None:
        return {"failed": f"no room for the coupled pair within {BREAKOUT_MM} mm of {a.refs if sa is None else b.refs}",
                "info": info}
    li = {lay: k for k, lay in enumerate(layers)}
    blocked = np.stack([center_blocked[lay] for lay in layers]).astype(np.uint8)
    path = core.grid_astar(win.nx, win.ny, len(layers), blocked.tobytes(), via_ok.astype(np.uint8).tobytes(),
                           [(sa[0], sa[1], li[a.layer])], [(sb[0], sb[1], li[b.layer])], VIA_COST, TURN45, TURN90)
    if path is None:
        return {"failed": f"no centreline between {a.refs} and {b.refs}", "info": info}
    tracks, vias, ends = _legs(win, path, layers, a, b, rule)
    nets = {"pos": a.pads["pos"][1].net, "neg": a.pads["neg"][1].net}
    uncoupled = 0.0
    for term, which in ((a, 0), (b, -1)):
        for leg in ("pos", "neg"):
            fp, pad = term.pads[leg]
            pi, pj = win.cell(*fp.pad_world(pad))
            lay_e, (ex, ey) = ends[leg][which]
            ei, ej = win.cell(ex, ey)
            others = [t for t in tracks + vias if t["net"] != nets[leg]]
            got = _breakout(win, raw, layers, others, (pi, pj, term.layer), (ei, ej, lay_e), nets[leg], rule, core)
            if got is None:
                return {"failed": f"breakout of {nets[leg]} at {fp.ref} blocked", "info": info}
            tracks += got[0]
            vias += got[1]
            uncoupled += sum(LineString(t["points"]).length for t in got[0])
    info.update(cells=len(path), layer_changes=sum(1 for p, q in itertools.pairwise(path) if p[2] != q[2]),
                uncoupled_mm=round(uncoupled, 3))
    return {"failed": None, "info": info, "tracks": tracks, "vias": vias}


def _simplify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [points[0]]
    for k in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = out[-1], points[k], points[k + 1]
        if abs((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)) > 1e-9:
            out.append(points[k])
    out.append(points[-1])
    return out


def _unit(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
    dx, dy = q[0] - p[0], q[1] - p[1]
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _legs(win: Window, path, layers, a: Terminal, b: Terminal, rule: PairRule) -> tuple[list, list, dict]:
    """Two offset legs per layer run, via pairs across the path at layer changes, breakout stubs at both ends.

    Sides are the mathematical left/right of the path direction (shapely's offset sign); a leg keeps its side
    from start to end, so the legs never cross.
    """
    runs: list[tuple[str, list[tuple[float, float]]]] = []
    for i, j, k in path:
        p = win.xy(i, j)
        if runs and runs[-1][0] == layers[k]:
            runs[-1][1].append(p)
        else:
            runs.append((layers[k], [p]))
    runs = [(lay, _simplify(pts) if len(pts) > 1 else pts) for lay, pts in runs]
    nets = {"pos": a.pads["pos"][1].net, "neg": a.pads["neg"][1].net}
    half, vhalf = rule.pitch / 2, rule.via_pitch / 2
    # which leg is on the left: the pad of that leg lies left of the first direction of travel
    d0 = _unit(runs[0][1][0], runs[0][1][1]) if len(runs[0][1]) > 1 else _unit(a.mid, b.mid)
    start = runs[0][1][0]

    def left_of(p):
        return d0[0] * (p[1] - start[1]) - d0[1] * (p[0] - start[0])

    side_of = {"pos": "left", "neg": "right"} if left_of(a.xy("pos")) >= left_of(a.xy("neg")) else \
        {"pos": "right", "neg": "left"}
    tracks: list[dict] = []
    vias: list[dict] = []
    leg_ends: dict[str, list[tuple[str, tuple[float, float]]]] = {"pos": [], "neg": []}
    for r, (layer, pts) in enumerate(runs):
        if len(pts) >= 2:
            line = LineString(pts)
            for leg in ("pos", "neg"):
                sign = 1 if side_of[leg] == "left" else -1
                coords = list(line.offset_curve(sign * half, join_style="mitre").coords)
                tracks.append({"layer": layer, "width": rule.width, "net": nets[leg], "points": coords})
                leg_ends[leg] += [(layer, coords[0]), (layer, coords[-1])]
        else:  # a one-cell run: the legs' places across the direction of travel
            c, nl = pts[0], (-d0[1], d0[0])
            for leg in ("pos", "neg"):
                sign = 1 if side_of[leg] == "left" else -1
                leg_ends[leg].append((layer, (c[0] + sign * nl[0] * half, c[1] + sign * nl[1] * half)))
        if r + 1 < len(runs):  # a via pair at the junction, across the incoming direction
            c = pts[-1]
            din = _unit(pts[-2], pts[-1]) if len(pts) > 1 else d0
            nleft = (-din[1], din[0])
            nxt_layer, nxt = runs[r + 1]
            dout = _unit(nxt[0], nxt[1]) if len(nxt) > 1 else din
            nleft2 = (-dout[1], dout[0])
            for leg in ("pos", "neg"):
                sign = 1 if side_of[leg] == "left" else -1
                v = (c[0] + sign * nleft[0] * vhalf, c[1] + sign * nleft[1] * vhalf)
                vias.append({"x": v[0], "y": v[1], "diameter": rule.via_d, "drill": rule.via_drill, "net": nets[leg],
                             "layers": ["F.Cu", "B.Cu"]})
                end_in = (c[0] + sign * nleft[0] * half, c[1] + sign * nleft[1] * half)
                start_out = (nxt[0][0] + sign * nleft2[0] * half, nxt[0][1] + sign * nleft2[1] * half)
                for lay, p, q in ((layer, end_in, v), (nxt_layer, v, start_out)):
                    if math.dist(p, q) > 1e-6:
                        tracks.append({"layer": lay, "width": rule.width, "net": nets[leg], "points": [p, q]})
                leg_ends[leg] += [(layer, v), (nxt_layer, v)]  # a via is reachable on both layers
    first_layer, last_layer = runs[0][0], runs[-1][0]
    ends = {}
    for leg in ("pos", "neg"):
        firsts = [e for lay, e in leg_ends[leg] if lay == first_layer]
        lasts = [e for lay, e in leg_ends[leg] if lay == last_layer]
        ends[leg] = [(first_layer, min(firsts, key=lambda q: math.dist(q, runs[0][1][0]))),
                     (last_layer, min(lasts, key=lambda q: math.dist(q, runs[-1][1][-1])))]
    return tracks, vias, ends


def rule_for(board: Board, net: str) -> PairRule:
    nc = board.netclass_of(net) or next(c for c in board.netclasses if c.name == "Default")
    return PairRule(width=nc.diff_pair_width or nc.track_width, gap=nc.diff_pair_gap or nc.clearance,
                    clearance=nc.clearance, via_d=nc.via_diameter, via_drill=nc.via_drill)
