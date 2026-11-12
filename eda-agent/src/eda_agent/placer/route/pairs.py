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
from collections import Counter
from dataclasses import dataclass, field, replace

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..ir import Board, Footprint, Pad
from .copper import copper_separated as _copper_separated
from .copper import existing_holes as _existing_holes
from .copper import holes_separated as _holes_separated
from .copper import pad_layers, pad_polygon

GRID_MM = 0.05  # declared: centreline grid
WINDOW_MM = 6.0  # declared: grid window margin around the terminals
BREAKOUT_MM = 4.0  # declared: the coupled section starts within this distance of a terminal
SECTORS, TRY_COMBOS = 8, 16  # declared: start directions per terminal; start/end pairs tried
TUNE_CANDIDATES = 4  # bounded alternatives if the best raw candidate cannot be tuned
COARSE_GRID_MM, COARSE_SECTORS, COARSE_COMBOS = 0.075, 12, 24
MARGIN_MM = GRID_MM / 2  # search cushion; copper still requires exact clearance and KiCad DRC
BREAKOUT_MAX_MM = 6.0  # declared: a leg's uncoupled breakout longer than this is a detour, not a breakout
DIRECT_TERMINAL_MM = 2.0  # short connector-to-protection links can share a layer without a coupled section
TUNE_TARGET, TUNE_HEIGHTS = 0.5, (0.6, 0.45, 0.3, 0.2)  # declared: tune to half the skew limit; bump heights
VIA_COST, TURN45, TURN90 = 40.0, 2.0, 6.0  # in grid steps (via = 2 mm of detour)


class OffsetError(ValueError):
    pass


@dataclass
class PairRule:
    width: float
    gap: float
    clearance: float
    via_d: float
    via_drill: float
    skew_max: float = 0.15
    max_vias_per_leg: int = 2  # architecture 7.1, short differential pairs

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
# --------------------------------------------------------------------------- terminals
def terminals(board: Board, pos: str, neg: str, *, near_mm: float = 2.0) -> list[Terminal]:
    """Side-by-side pad pairs of the two legs, at most one per part (IC pins, connector pins, ESD pair)."""
    copper = [cl.canonical for cl in board.copper_layers]
    pads = {leg: [(fp, p) for fp in board.footprints for p in fp.pads if p.net == net]
            for leg, net in (("pos", pos), ("neg", neg))}
    by_part = {leg: Counter(fp.ref for fp, _pad in items) for leg, items in pads.items()}
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
            unique_on_part = fp_a.ref == fp_b.ref and by_part["pos"][fp_a.ref] == by_part["neg"][fp_b.ref] == 1
            if d <= near_mm or unique_on_part:
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


def obstacles(board: Board, win: Window, rule: PairRule, extra: list[dict], *, skip_pad_nets: set[str] = frozenset(),
              skip_pads: set[int] = frozenset(), skip_copper_nets: set[str] = frozenset()) -> dict[str, np.ndarray]:
    """Everything a copper edge may not enter, per layer, grown by the clearance.

    ``skip_pad_nets`` / ``skip_pads`` (ids of Pad objects) leave out pads the copper may touch;
    ``skip_copper_nets`` leaves out fixed copper of those nets (an earlier segment of the same leg).
    """
    copper = [cl.canonical for cl in board.copper_layers]
    per = {lay: [] for lay in win.layers}
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net in skip_pad_nets or id(pad) in skip_pads:
                continue
            g = pad_polygon(fp, pad, rule.clearance + MARGIN_MM)
            for lay in pad_layers(fp, pad, copper):
                if lay in per:
                    per[lay].append(g)
            if pad.drill and pad.kind == "np_thru_hole":
                for geoms in per.values():
                    geoms.append(g)
    for item in extra:  # fixed copper already on the board (macros, earlier pairs)
        if item.get("net") in skip_copper_nets:
            continue
        if "points" in item:
            g = LineString(item["points"]).buffer(item["width"] / 2 + rule.clearance + MARGIN_MM)
            if item["layer"] in per:
                per[item["layer"]].append(g)
        else:
            g = Point(item["x"], item["y"]).buffer(item["diameter"] / 2 + rule.clearance + MARGIN_MM)
            for geoms in per.values():
                geoms.append(g)
    for row in board.obstacles:
        layer = row["copper"]
        if layer in per and not row["overlapping_pads"] and not row.get("net"):
            per[layer].extend(Polygon(ring) for ring in row["outlines"] if len(ring) >= 3)
    edge = None
    if board.outline:
        inside = unary_union([Polygon(o.outer, o.holes) for o in board.outline]).buffer(-rule.clearance - MARGIN_MM)
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


class Exits:
    """Which pads can still be reached by a trace once a pair's copper is locked.

    A locked pair is a wall on its layer.  On StickHub the router then left 12 supply connections
    unrouted that it had routed without the pairs (gate C3): a pair ran around a corner of the hub IC
    and closed a pin and its capacitor into a pocket.  So a candidate is judged like this: the free
    room for a trace centre (both outer layers, joined where a via fits) is split into connected
    regions; a pad that could reach the largest region before the candidate must still reach it after.
    """

    def __init__(self, board: Board, win: Window, rule: PairRule, fixed: list[dict], own_nets: set[str],
                 blocked_drills: np.ndarray):
        self.win, self.rule, self.blocked_drills = win, rule, blocked_drills
        self.raw = obstacles(board, win, rule, fixed)
        self.grow = _disk(rule.width / 2 / win.res)
        self.via_grow = _disk(rule.via_d / 2 / win.res)
        reach = rule.clearance + MARGIN_MM + rule.width / 2 + 2 * win.res  # just outside a pad's own halo
        self.pads: list[tuple[str, dict[str, np.ndarray]]] = []
        for fp in board.footprints:
            for pad in fp.pads:
                if not pad.net or pad.net in own_nets or pad.kind == "np_thru_hole":
                    continue
                x, y = fp.pad_world(pad)
                i, j = win.cell(x, y)
                if not (0 <= i < win.nx and 0 <= j < win.ny):
                    continue
                ring = win.raster(pad_polygon(fp, pad, reach))
                self.pads.append((f"{fp.ref}-{pad.number}", {lay: np.flatnonzero(ring)
                                                           for lay in pad_layers(fp, pad, win.layers)}))
        main = self._main(self.raw)
        self.before = [self._reaches(main, cells) for _name, cells in self.pads]

    def _main(self, raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Per layer, the free trace-centre cells that belong to the largest region."""
        free = {lay: ~ndimage.binary_dilation(m, structure=self.grow) for lay, m in raw.items()}
        labels, offset = {}, 0
        for lay, m in free.items():
            lab, n = ndimage.label(m, structure=np.ones((3, 3), bool))
            labels[lay] = np.where(lab > 0, lab + offset, 0)
            offset += n
        parent = list(range(offset + 1))

        def find(k: int) -> int:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        lays = list(labels)
        if len(lays) > 1:
            union = np.zeros((self.win.ny, self.win.nx), bool)
            for m in raw.values():
                union |= m
            via_ok = ~ndimage.binary_dilation(union, structure=self.via_grow) & ~self.blocked_drills
            for la, lb in itertools.pairwise(lays):
                both = via_ok & (labels[la] > 0) & (labels[lb] > 0)
                for x, y in set(zip(labels[la][both].tolist(), labels[lb][both].tolist(), strict=True)):
                    parent[find(x)] = find(y)
        root = np.array([find(k) for k in range(offset + 1)])
        sizes = np.zeros(offset + 1, np.int64)
        for lab in labels.values():
            sizes += np.bincount(root[lab].ravel(), minlength=offset + 1)
        sizes[0] = 0
        best = int(np.argmax(sizes))
        return {lay: (root[lab] == best) & (lab > 0) for lay, lab in labels.items()}

    @staticmethod
    def _reaches(main: dict[str, np.ndarray], cells: dict[str, np.ndarray]) -> bool:
        return any(main[lay].ravel()[idx].any() for lay, idx in cells.items() if lay in main)

    def closed_in(self, tracks: list[dict], vias: list[dict]) -> list[str]:
        """Pads that the given copper cuts off from the largest free region."""
        grow = self.rule.clearance + MARGIN_MM
        raw = {lay: m.copy() for lay, m in self.raw.items()}
        for t in tracks:
            if t["layer"] in raw:
                raw[t["layer"]] |= self.win.raster(LineString(t["points"]).buffer(t["width"] / 2 + grow))
        for v in vias:
            g = self.win.raster(Point(v["x"], v["y"]).buffer(v["diameter"] / 2 + grow))
            for m in raw.values():
                m |= g
        main = self._main(raw)
        return [name for (name, cells), was in zip(self.pads, self.before, strict=True)
                if was and not self._reaches(main, cells)]


# --------------------------------------------------------------------------- routing
def route_pair(board: Board, pos: str, neg: str, rule: PairRule, *,
               fixed: list[dict] | None = None, retry_coarse: bool = False) -> PairRoute:
    import placer_core

    res = PairRoute(pos, neg)
    terms = chain(terminals(board, pos, neg))
    if len(terms) < 2:
        res.failed = f"found {len(terms)} side-by-side terminal(s); need 2"
        return res
    total_pads = sum(pad.net in (pos, neg) for fp in board.footprints for pad in fp.pads)
    if 2 * len(terms) != total_pads:
        res.failed = f"{total_pads - 2 * len(terms)} pair pad(s) have no matched terminal"
        return res
    result = _route_order(board, terms, pos, neg, rule, fixed or [], placer_core)
    if result.failed is not None and len(terms) == 3:
        # One bounded alternative for connector, IC, and protection terminal.
        alternate = _route_order(board, [terms[0], terms[2], terms[1]], pos, neg, rule, fixed or [], placer_core)
        if alternate.failed is None:
            return alternate
    if result.failed is not None and retry_coarse:
        coarse = _route_order(board, terms, pos, neg, rule, fixed or [], placer_core, coarse=True)
        if coarse.failed is None:
            return coarse
        if len(terms) > 2 and coarse.failed.startswith("pair skew"):
            balanced = _route_order(
                board, terms, pos, neg, rule, fixed or [], placer_core,
                coarse=True, segment_skew=rule.skew_max / (len(terms) - 1),
            )
            if balanced.failed is None:
                return balanced
    return result


def _route_order(board: Board, terms: list[Terminal], pos: str, neg: str,
                 rule: PairRule, fixed: list[dict], core, *, coarse: bool = False,
                 segment_skew: float | None = None) -> PairRoute:
    res = PairRoute(pos, neg)
    layers = [cl.canonical for cl in board.copper_layers if cl.canonical in ("F.Cu", "B.Cu")]  # outer layers
    fixed = list(fixed)
    holes = _existing_holes(board, fixed)
    hole_clearance = board.rules.get("hole_to_hole", 0.0)
    tracks, vias = [], []
    segment_rule = rule if segment_skew is None else replace(rule, skew_max=segment_skew)
    for a, b in itertools.pairwise(terms):
        seg = _route_segment(board, a, b, segment_rule, layers, fixed, core, coarse)
        res.segments.append(seg["info"])
        if seg["failed"]:
            res.failed = seg["failed"]
            return res
        tracks.extend(seg["tracks"])
        vias.extend(seg["vias"])
        fixed += seg["tracks"] + seg["vias"]

    lengths = _leg_lengths(tracks, {"pos": pos, "neg": neg})
    skew = abs(lengths["pos"] - lengths["neg"])
    pos_vias = sum(v["net"] == pos for v in vias)
    neg_vias = sum(v["net"] == neg for v in vias)
    if skew > rule.skew_max + 1e-9:
        res.failed = f"pair skew {skew:.6g} mm exceeds {rule.skew_max:.6g} mm"
    elif pos_vias != neg_vias:
        res.failed = f"unequal via counts ({pos_vias} vs {neg_vias})"
    elif pos_vias > rule.max_vias_per_leg:
        res.failed = f"{pos_vias} vias per leg exceeds {rule.max_vias_per_leg}"
    elif not _holes_separated(vias, holes, hole_clearance):
        res.failed = "via holes violate drill clearance"
    elif not _copper_separated(
        [t for t in tracks if t["net"] == pos],
        [v for v in vias if v["net"] == pos],
        [item for item in tracks + vias if item["net"] == neg],
        rule.clearance,
    ):
        res.failed = "pair legs violate copper clearance"
    else:
        res.tracks = tracks
        res.vias = vias
    return res


def _pair_starts(win: Window, blocked: np.ndarray, term: Terminal, toward: tuple[float, float],
                 sectors: int = SECTORS) -> list[tuple[int, int]]:
    """Free centreline cells within BREAKOUT_MM, taking the nearest one in each sector.

    Cells on every side matter: where the coupled section starts decides which leg runs on which side,
    so a pair whose pads are in the opposite order at the two ends is solved by starting behind the pads.
    """
    mx, my = term.mid
    dx, dy = win.X - mx, win.Y - my
    dist = np.hypot(dx, dy)
    score = dist - 0.3 * (dx * toward[0] + dy * toward[1])
    score[blocked | (dist > BREAKOUT_MM)] = np.inf
    sector = (np.floor((np.arctan2(dy, dx) + math.pi) / (2 * math.pi) * sectors).astype(int)) % sectors
    out = []
    for k in range(sectors):
        masked = np.where(sector == k, score, np.inf)
        flat = int(np.argmin(masked))
        j, i = np.unravel_index(flat, masked.shape)
        if np.isfinite(masked[j, i]):
            out.append((float(masked[j, i]), int(i), int(j)))
    return [(i, j) for _s, i, j in sorted(out)]


def _spread(combos: list[tuple]) -> list[tuple]:
    """Order the start/end combinations: cheapest first, but every other try goes to an end place not tried yet.

    Sorted by cost alone, the first dozen combinations were one end place with a dozen near-identical
    starts (StickHub ``/U7D``: the entry beside the connector pads, the one the expert used, was never tried).
    """
    fresh, rest, seen = [], [], set()
    for combo in sorted(combos):
        if combo[3] not in seen:
            seen.add(combo[3])
            fresh.append(combo)
        else:
            rest.append(combo)
    out = []
    for k in range(max(len(fresh), len(rest))):
        out += fresh[k:k + 1] + rest[k:k + 1]
    return out


def _pos_left(term: Terminal, origin: tuple[float, float], d: tuple[float, float]) -> bool:
    def left(p):
        return d[0] * (p[1] - origin[1]) - d[1] * (p[0] - origin[0])
    return left(term.xy("pos")) >= left(term.xy("neg"))


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


def _block_drills(win: Window, mask: np.ndarray, holes: list[tuple[float, float, float]],
                  diameter: float, clearance: float) -> None:
    for x, y, drill in holes:
        radius = (diameter + drill) / 2 + clearance
        i0, j0 = win.cell(x - radius, y - radius)
        i1, j1 = win.cell(x + radius, y + radius)
        i0, j0 = max(0, i0), max(0, j0)
        i1, j1 = min(win.nx - 1, i1), min(win.ny - 1, j1)
        if i0 > i1 or j0 > j1:
            continue
        area = np.s_[j0:j1 + 1, i0:i1 + 1]
        mask[area] |= (win.X[area] - x) ** 2 + (win.Y[area] - y) ** 2 < (radius - 1e-6) ** 2


def _breakout(win: Window, raw: dict[str, np.ndarray], layers, own_copper: list[dict], start: tuple, end: tuple,
              net: str, rule: PairRule, core, allow_via: bool, *,
              blocked_drills: np.ndarray | None = None) -> tuple[list[dict], list[dict]] | None:
    """One leg from its pad to its place in the pair, at single-trace width (A*).

    A via is allowed only when the pad and the coupled section are on different layers: then each leg takes
    exactly one, as an expert does next to a fine-pitch IC, and the legs may cross on the pad's layer.
    """
    grown = {}
    for lay in layers:
        m = raw[lay].copy()
        for item in own_copper:  # the other leg is an obstacle like any other net
            if "points" in item:
                if item["layer"] != lay:
                    continue
                g = LineString(item["points"]).buffer(item["width"] / 2 + rule.clearance + MARGIN_MM)
            else:
                g = Point(item["x"], item["y"]).buffer(item["diameter"] / 2 + rule.clearance + MARGIN_MM)
            m |= win.raster(g)
        grown[lay] = m
    blocked = {lay: ndimage.binary_dilation(m, structure=_disk(rule.width / 2 / win.res)) for lay, m in grown.items()}
    if allow_via:
        union = np.zeros((win.ny, win.nx), bool)
        for m in grown.values():
            union |= m
        via_blocked = ndimage.binary_dilation(union, structure=_disk(rule.via_d / 2 / win.res))
        if blocked_drills is not None:
            via_blocked |= blocked_drills
        via_ok = (~via_blocked).astype(np.uint8)
    else:
        via_ok = np.zeros((win.ny, win.nx), np.uint8)
    li = {lay: k for k, lay in enumerate(layers)}
    (start_xy, sl), (end_xy, el) = start, end
    si, sj = win.cell(*start_xy)
    ei, ej = win.cell(*end_xy)
    for i, j, lay in ((si, sj, sl), (ei, ej, el)):
        blocked[lay][j, i] = False
    stack = np.stack([blocked[lay] for lay in layers]).astype(np.uint8)
    path = core.grid_astar(win.nx, win.ny, len(layers), stack.tobytes(), via_ok.tobytes(),
                           [(si, sj, li[sl])], [(ei, ej, li[el])], VIA_COST, TURN45, TURN90, 2_000_000)
    if path is None:
        return None
    tracks, vias = _cells_to_copper(win, path, layers, net, rule)
    if not tracks and not vias:
        if start_xy != end_xy:
            tracks.append({"layer": sl, "width": rule.width, "net": net, "points": [start_xy, end_xy]})
    else:
        first, last = (tracks[0], tracks[-1]) if tracks else (None, None)
        for track, index, layer, grid_xy, actual in (
            (first, 0, sl, win.xy(si, sj), start_xy), (last, -1, el, win.xy(ei, ej), end_xy),
        ):
            if grid_xy == actual:
                continue
            if track is not None and track["layer"] == layer and track["points"][index] == grid_xy:
                track["points"][index] = actual
            else:
                tracks.append({"layer": layer, "width": rule.width, "net": net, "points": [grid_xy, actual]})
    if not _copper_separated(tracks, vias, own_copper, rule.clearance):
        return None
    return tracks, vias


def _leg_lengths(tracks: list[dict], nets: dict[str, str]) -> dict[str, float]:
    out = {"pos": 0.0, "neg": 0.0}
    for t in tracks:
        leg = "pos" if t["net"] == nets["pos"] else "neg"
        out[leg] += LineString(t["points"]).length
    return out


def _breakouts_at(win, leg_raw, layers, term: Terminal, ends, which: int, nets, rule, core, tracks, vias, cl,
                  blocked_drills: np.ndarray, hole_clearance: float):
    """Both legs' breakouts at one terminal; if the first one blocks the second, try the other order."""
    allow_via = term.layer != cl
    for order in (("pos", "neg"), ("neg", "pos")):
        t_add, v_add, ok = [], [], True
        for leg in order:
            fp, pad = term.pads[leg]
            lay_e, end_xy = ends[leg][which]
            others = [t for t in tracks + vias + t_add + v_add if t["net"] != nets[leg]]
            via_mask = blocked_drills.copy() if allow_via else None
            if via_mask is not None:
                holes = [(v["x"], v["y"], v["drill"]) for v in vias + v_add]
                _block_drills(win, via_mask, holes, rule.via_drill, hole_clearance)
            got = _breakout(win, leg_raw[leg], layers, others, (fp.pad_world(pad), term.layer), (end_xy, lay_e), nets[leg],
                            rule, core, allow_via, blocked_drills=via_mask)
            if got is None or sum(LineString(t["points"]).length for t in got[0]) > BREAKOUT_MAX_MM:
                ok = False
                break
            t_add += got[0]
            v_add += got[1]
        if ok:
            counts = {leg: sum(v["net"] == nets[leg] for v in v_add) for leg in ("pos", "neg")}
            if counts["pos"] == counts["neg"]:
                return t_add, v_add, None
    return None, None, f"breakout at {term.refs}"


def _tune(win: Window, leg_raw: dict[str, dict[str, np.ndarray]], tracks: list[dict], nets: dict[str, str], rule: PairRule,
          skew: float) -> tuple[list[dict], float]:
    """Add length to the shorter leg with rectangular bumps (a small serpentine) until the skew is within limits.

    A bump adds twice its height. It goes on a straight piece of the shorter leg, on the side away from the
    other leg, and only where its copper keeps the clearance to every obstacle and to the other leg.
    """
    ln = _leg_lengths(tracks, nets)
    short = "pos" if ln["pos"] < ln["neg"] else "neg"
    other = "neg" if short == "pos" else "pos"
    raw = leg_raw[short]  # the partner's pads and copper are obstacles for the bumps
    need = skew - TUNE_TARGET * rule.skew_max
    if need <= 0:
        return tracks, skew
    base = max(3 * rule.width, rule.width + rule.clearance + rule.width)  # bump top length
    gap = base  # straight copper between bumps
    tracks = [dict(t) for t in tracks]
    partner = {lay: np.zeros((win.ny, win.nx), bool) for lay in raw}
    for t in tracks:
        if t["net"] == nets[other] and t["layer"] in partner:
            partner[t["layer"]] |= win.raster(LineString(t["points"]).buffer(t["width"] / 2 + rule.clearance
                                                                              + MARGIN_MM))
    cands = []
    for ti, t in enumerate(tracks):
        if t["net"] != nets[short]:
            continue
        for si, (p, q) in enumerate(itertools.pairwise(t["points"])):
            cands.append((math.dist(p, q), ti, si))
    insertions: dict[int, list[tuple[int, int]]] = {}
    for _length, ti, original_index in sorted(cands, reverse=True):
        if need <= 0:
            break
        t = tracks[ti]
        si = original_index + sum(count for index, count in insertions.get(ti, []) if index < original_index)
        p, q = t["points"][si], t["points"][si + 1]
        seg_len = math.dist(p, q)
        n_max = int((seg_len - gap) // (base + gap))
        if n_max < 1:
            continue
        d = _unit(p, q)
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        # away from the partner leg: the side whose normal points from the partner to this segment
        others_xy = [pt for o in tracks if o["net"] == nets[other] for pt in o["points"]]
        nrm = (-d[1], d[0])
        if others_xy:
            near = min(others_xy, key=lambda pt: math.dist(pt, mid))
            if (mid[0] - near[0]) * nrm[0] + (mid[1] - near[1]) * nrm[1] < 0:
                nrm = (-nrm[0], -nrm[1])
        blocked = ndimage.binary_dilation(raw[t["layer"]] | partner[t["layer"]],
                                          structure=_disk(rule.width / 2 / win.res))
        for h in TUNE_HEIGHTS:
            per = 2 * h
            n = min(n_max, max(1, math.ceil(need / per)))
            h_use = min(h, need / (2 * n))
            pts = [p]
            start = (seg_len - n * base - (n - 1) * gap) / 2
            for k in range(n):
                s0 = start + k * (base + gap)
                a0 = (p[0] + d[0] * s0, p[1] + d[1] * s0)
                a1 = (a0[0] + d[0] * base, a0[1] + d[1] * base)
                pts += [a0, (a0[0] + nrm[0] * h_use, a0[1] + nrm[1] * h_use),
                        (a1[0] + nrm[0] * h_use, a1[1] + nrm[1] * h_use), a1]
            pts.append(q)
            new_geom = LineString(pts).buffer(rule.width / 2).difference(LineString([p, q]).buffer(rule.width / 2 + 1e-3))
            if (win.raster(new_geom) & blocked).any():
                continue
            t["points"] = t["points"][:si] + pts + t["points"][si + 2:]
            insertions.setdefault(ti, []).append((original_index, len(pts) - 2))
            need -= 2 * n * h_use
            break
    ln = _leg_lengths(tracks, nets)
    return tracks, abs(ln["pos"] - ln["neg"])


def _direct_terminal_link(win, leg_raw, a: Terminal, b: Terminal, nets, rule: PairRule, core):
    if a.layer != b.layer or any(math.dist(a.xy(leg), b.xy(leg)) > DIRECT_TERMINAL_MM for leg in nets):
        return None
    for order in (("pos", "neg"), ("neg", "pos")):
        tracks = []
        for leg in order:
            result = _breakout(win, leg_raw[leg], [a.layer], tracks, (a.xy(leg), a.layer),
                               (b.xy(leg), b.layer), nets[leg], rule, core, False)
            if result is None:
                break
            tracks.extend(result[0])
        else:
            lengths = _leg_lengths(tracks, nets)
            skew = abs(lengths["pos"] - lengths["neg"])
            if max(lengths.values()) <= DIRECT_TERMINAL_MM and skew <= rule.skew_max:
                return tracks, sum(lengths.values()), skew
    return None


def _route_segment(board, a: Terminal, b: Terminal, rule: PairRule, layers, fixed, core, coarse=False) -> dict:
    xs = [a.mid[0], b.mid[0]]
    ys = [a.mid[1], b.mid[1]]
    res = COARSE_GRID_MM if coarse else GRID_MM
    sectors = COARSE_SECTORS if coarse else SECTORS
    try_combos = COARSE_COMBOS if coarse else TRY_COMBOS
    win = Window(board, (min(xs) - WINDOW_MM, min(ys) - WINDOW_MM, max(xs) + WINDOW_MM, max(ys) + WINDOW_MM),
                 layers, res=res)
    blocked_drills = np.zeros((win.ny, win.nx), bool)
    hole_clearance = board.rules.get("hole_to_hole", 0.0)
    _block_drills(win, blocked_drills, _existing_holes(board, fixed), rule.via_drill, hole_clearance)
    own = {a.pads["pos"][1].net, a.pads["neg"][1].net}
    term_pads = {id(pad) for term in (a, b) for _fp, pad in term.pads.values()}
    # coupled section: every pad except the four terminal pads; earlier copper of these nets is not in the way
    raw = obstacles(board, win, rule, fixed, skip_pads=term_pads, skip_copper_nets=own)
    # a leg's breakout: everything except its own net, so the partner's pads and copper are obstacles
    leg_raw = {leg: obstacles(board, win, rule, fixed, skip_pad_nets={term_net}, skip_copper_nets={term_net})
               for leg, term_net in (("pos", a.pads["pos"][1].net), ("neg", a.pads["neg"][1].net))}
    half = (rule.pitch / 2 + rule.width / 2) / win.res  # centreline to the outer edge of a leg
    own_pads = {lay: np.zeros_like(m) for lay, m in raw.items()}
    for term in (a, b):
        for fp, pad in term.pads.values():
            g = pad_polygon(fp, pad, rule.clearance + MARGIN_MM)
            for lay in pad_layers(fp, pad, layers):
                own_pads[lay] |= win.raster(g)
    center_blocked = {lay: ndimage.binary_dilation(m | own_pads[lay], structure=_disk(half)) for lay, m in raw.items()}
    info = {"from": a.refs, "to": b.refs}
    if coarse:
        info["grid_mm"] = res
    direction = np.subtract(b.mid, a.mid)
    norm = float(np.hypot(*direction)) or 1.0
    toward = (direction[0] / norm, direction[1] / norm)
    nets = {"pos": a.pads["pos"][1].net, "neg": a.pads["neg"][1].net}
    # the coupled section runs on one layer; both are tried, the terminals' own layer first
    order = sorted(layers, key=lambda lay: (lay != b.layer, lay != a.layer))
    # pads a couple of millimetres apart on one layer (connector pin to its protection part) are joined
    # pad to pad, as an expert does: no vias, nothing shorter exists
    direct = _direct_terminal_link(win, leg_raw, a, b, nets, rule, core)
    if direct is not None:
        tracks, uncoupled, skew = direct
        info.update(direct_layer=a.layer, uncoupled_mm=round(uncoupled, 3), skew_mm=round(skew, 3))
        return {"failed": None, "info": info, "tracks": tracks, "vias": []}
    candidates, reasons = [], []
    exits = Exits(board, win, rule, fixed, own, blocked_drills)
    no_via = np.zeros((win.ny, win.nx), np.uint8).tobytes()
    for cl in order:
        starts_a = _pair_starts(win, center_blocked[cl], a, toward, sectors)
        starts_b = _pair_starts(win, center_blocked[cl], b, (-toward[0], -toward[1]), sectors)
        if not starts_a or not starts_b:
            reasons.append(f"no room for the coupled pair on {cl}")
            continue
        blocked = center_blocked[cl].astype(np.uint8).tobytes()
        combos = []
        for sa, sb in itertools.product(starts_a, starts_b):
            pa, pb = win.xy(*sa), win.xy(*sb)
            d = np.subtract(pb, pa)
            n = float(np.hypot(*d)) or 1.0
            d = (d[0] / n, d[1] / n)
            # legs keep their order on one layer; a terminal on the other layer can swap them before its vias
            same = (_pos_left(a, pa, d) == _pos_left(b, pb, d)) or a.layer != cl or b.layer != cl
            combos.append((not same, math.dist(pa, a.mid) + math.dist(pb, b.mid) + n, sa, sb))
        for _swap, _cost, sa, sb in _spread(combos)[:try_combos]:
            path = core.grid_astar(win.nx, win.ny, 1, blocked, no_via, [(sa[0], sa[1], 0)], [(sb[0], sb[1], 0)],
                                   VIA_COST, TURN45, TURN90, 6_000_000)
            if path is None:
                reasons.append("centreline")
                continue
            try:
                tracks, vias, ends = _legs(win, path, [cl], a, b, rule)
            except OffsetError:
                reasons.append("centreline folds back")
                continue
            if b.layer == cl:  # same layer at b: the legs must arrive in the order of b's pads
                (_l, pos_end), (_l2, neg_end) = ends["pos"][-1], ends["neg"][-1]
                if math.dist(pos_end, b.xy("pos")) + math.dist(neg_end, b.xy("neg")) > \
                        math.dist(pos_end, b.xy("neg")) + math.dist(neg_end, b.xy("pos")):
                    reasons.append("legs arrive in swapped order")
                    continue
            uncoupled, ok = 0.0, True
            for term, which in ((a, 0), (b, -1)):
                t_add, v_add, why = _breakouts_at(win, leg_raw, layers, term, ends, which, nets, rule, core,
                                                  tracks, vias, cl, blocked_drills, hole_clearance)
                if why:
                    reasons.append(why)
                    ok = False
                    break
                tracks += t_add
                vias += v_add
                uncoupled += sum(LineString(t["points"]).length for t in t_add)
            if not ok:
                continue
            closed = exits.closed_in(tracks, vias)
            if closed:
                reasons.append(f"closes in {closed[0]}")
                continue
            ln = _leg_lengths(tracks, nets)
            skew = abs(ln["pos"] - ln["neg"])
            key = (skew > rule.skew_max, len(vias), round(skew, 3), round(ln["pos"] + ln["neg"], 3))
            candidates.append((key, tracks, vias, path, uncoupled, skew, cl))
            candidates.sort(key=lambda candidate: candidate[0])
            del candidates[TUNE_CANDIDATES:]
            if skew <= rule.skew_max / 2:
                break
        if candidates and not candidates[0][0][0]:
            break
    if not candidates:
        common = max(reasons, key=reasons.count) if reasons else "no attempt"
        info["attempts"] = {r: reasons.count(r) for r in sorted(set(reasons))}  # every reason, for the report
        return {"failed": f"{a.refs} -> {b.refs}: {common}", "info": info}
    _key, tracks, vias, path, uncoupled, raw_skew, cl = candidates[0]
    skew = raw_skew
    if raw_skew > rule.skew_max:
        for index, candidate in enumerate(candidates):
            _key, trial, trial_vias, trial_path, trial_uncoupled, trial_raw, trial_layer = candidate
            tuned, trial_skew = _tune(win, leg_raw, trial, nets, rule, trial_raw)
            if index == 0:
                tracks, skew = tuned, trial_skew
            if trial_skew <= rule.skew_max:
                tracks, vias, path, uncoupled = tuned, trial_vias, trial_path, trial_uncoupled
                skew, raw_skew, cl = trial_skew, trial_raw, trial_layer
                break
    info.update(coupled_layer=cl, cells=len(path), uncoupled_mm=round(uncoupled, 3),
                skew_before_tuning_mm=round(raw_skew, 3), skew_mm=round(skew, 3))
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
                off = line.offset_curve(sign * half, join_style="mitre")
                if off.is_empty or off.geom_type != "LineString":
                    raise OffsetError("the centreline folds back too tightly to offset")
                coords = list(off.coords)
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


trace_path = _breakout  # public name: one single-width trace between two cells (A*)
