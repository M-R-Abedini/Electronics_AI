# SPDX-License-Identifier: MIT
"""Layered macro contract (architecture 4.4), its rigid transform and flip, extraction and validation.

A macro is a rigid block: parts, their complete local copper (tracks, arcs,
vias, local zones), keepouts, one port per outgoing net and recorded checks.
Geometry is stored in the macro frame on the macro's ``home_side``.  Placing it
applies ``world = origin + R(rotation) * M(local)`` where ``M`` mirrors x when
the target side is not the home side; copper layers map F.Cu <-> B.Cu and
In(k) <-> In(n-1-k); every part toggles side and its orientation becomes
``180 - theta`` (KiCad's left-right flip, verified by read-back in F0c).

Rules checked by ``validate`` (architecture 4.4):
1. complete: every copper item reaches a part pad; loose ends only at ports
2. every via is joined to a pad of its own net by macro copper
3. exactly one port per outgoing signal net (plane nets connect through the plane)
4. flip only to a side whose mapped layers carry the same role
5. one block only: declared by intent (F2); not checkable from geometry
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .eval.legality import courtyard_polygon
from .eval.metrics import zone_nets
from .geometry import norm_angle, rotate, transform
from .ir import Board, Footprint

SCHEMA_VERSION = 1
TOL = 1e-4  # mm: endpoints closer than this are the same point (KiCad stores nm)
DEFAULT_MARGIN_MM = 1.0  # declared parameter: copper region = union of part courtyards grown by this


@dataclass(frozen=True)
class Transform:
    x: float
    y: float
    rotation: float = 0.0
    side: str = "F"


@dataclass
class MacroPart:
    ref: str
    uuid: str
    x: float
    y: float
    rotation: float
    side_rel: str  # same | opposite (relative to the macro's home side)


@dataclass
class MTrack:
    layer: str
    width: float
    net: str
    points: list[tuple[float, float]]  # 2 points, or 3 for an arc (start, mid, end)


@dataclass
class MVia:
    x: float
    y: float
    diameter: float
    drill: float
    net: str
    layers: tuple[str, str]


@dataclass
class MZone:
    net: str
    layers: list[str]
    outline: list[list[tuple[float, float]]]
    priority: int = 0
    name: str = ""
    pad_connection: str = "solid"  # solid | thermal: a macro polygon (SW node) joins its pads solidly


@dataclass
class Keepout:
    kind: str  # copper | via | track | placement
    layer: str
    polygon: list[tuple[float, float]]
    reason: str


@dataclass
class Port:
    net: str
    anchor: str  # pad | via | track_end
    layer: str
    x: float
    y: float
    escape_dir: float  # degrees, screen CCW, in the macro frame
    min_channel_mm: float


@dataclass
class Check:
    name: str
    measured: float
    limit: float
    source: str
    passed: bool


@dataclass
class Macro:
    template: str
    home_side: str
    layer_count: int
    parts: list[MacroPart]
    tracks: list[MTrack] = field(default_factory=list)
    vias: list[MVia] = field(default_factory=list)
    zones: list[MZone] = field(default_factory=list)
    keepouts: list[Keepout] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    layer_roles: dict[str, str] = field(default_factory=dict)  # canonical layer -> role (with net)
    origin: tuple[float, float] = (0.0, 0.0)  # extraction frame on the source board: the macro's home pose
    template_version: int = 1
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# transform
# --------------------------------------------------------------------------
def flip_layer(name: str, layer_count: int) -> str:
    if name == "F.Cu":
        return "B.Cu"
    if name == "B.Cu":
        return "F.Cu"
    if name.startswith("In") and name.endswith(".Cu"):
        k = int(name[2:-3])
        return f"In{layer_count - 1 - k}.Cu"
    return name


def _r6(v: float) -> float:
    return round(v + 0.0, 6) or 0.0


def to_world(pt: tuple[float, float], t: Transform, mirror: bool) -> tuple[float, float]:
    x, y = pt
    if mirror:
        x = -x
    wx, wy = transform((x, y), (t.x, t.y), t.rotation)
    return _r6(wx), _r6(wy)


def to_local(pt: tuple[float, float], t: Transform, mirror: bool) -> tuple[float, float]:
    x, y = rotate(pt[0] - t.x, pt[1] - t.y, -t.rotation)
    return _r6(-x if mirror else x), _r6(y)


@dataclass
class Instance:
    """A macro placed on a board: part poses keyed by UUID and world copper."""

    poses: dict[str, dict]
    tracks: list[MTrack]
    vias: list[MVia]
    zones: list[MZone]
    ports: list[Port]
    mirror: bool


def instantiate(macro: Macro, t: Transform) -> Instance:
    mirror = t.side != macro.home_side
    n = macro.layer_count
    lay = (lambda name: flip_layer(name, n)) if mirror else (lambda name: name)
    poses = {}
    for p in macro.parts:
        x, y = to_world((p.x, p.y), t, mirror)
        rot = (180.0 - p.rotation if mirror else p.rotation) + t.rotation
        on_home = p.side_rel == "same"
        side = t.side if on_home else ("B" if t.side == "F" else "F")
        poses[p.uuid] = {"ref": p.ref, "x": x, "y": y, "rotation": norm_angle(rot), "side": side}
    tracks = [MTrack(lay(tr.layer), tr.width, tr.net, [to_world(q, t, mirror) for q in tr.points])
              for tr in macro.tracks]
    vias = []
    for v in macro.vias:
        x, y = to_world((v.x, v.y), t, mirror)
        a, b = lay(v.layers[0]), lay(v.layers[1])
        vias.append(MVia(x, y, v.diameter, v.drill, v.net, (a, b)))
    zones = [MZone(z.net, [lay(name) for name in z.layers], [[to_world(q, t, mirror) for q in ring] for ring in z.outline],
                   z.priority, z.name, z.pad_connection) for z in macro.zones]
    ports = []
    for p in macro.ports:
        x, y = to_world((p.x, p.y), t, mirror)
        d = (180.0 - p.escape_dir if mirror else p.escape_dir) + t.rotation
        ports.append(Port(p.net, p.anchor, lay(p.layer), x, y, norm_angle(d), p.min_channel_mm))
    return Instance(poses, tracks, vias, zones, ports, mirror)


def predicted_pads(fp: Footprint, pose: dict) -> list[tuple[str, tuple[float, float]]]:
    """World pad centres of ``fp`` at ``pose``; a side change mirrors the footprint frame (y -> -y)."""
    flip = pose["side"] != fp.side
    out = []
    for pad in fp.pads:
        lx, ly = pad.local
        if flip:
            ly = -ly
        wx, wy = transform((lx, ly), (pose["x"], pose["y"]), pose["rotation"])
        out.append((pad.number, (_r6(wx), _r6(wy))))
    return out


# --------------------------------------------------------------------------
# extraction from existing copper
# --------------------------------------------------------------------------
def _pad_shape(fp: Footprint, pad) -> Polygon:
    cx, cy = fp.pad_world(pad)
    w, h = pad.size
    ang = fp.rotation + pad.rotation
    corners = [transform(c, (cx, cy), ang) for c in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
    return Polygon(corners).buffer(TOL)


def _layer_ok(item_layers: set[str], pad_layers: tuple[str, ...]) -> bool:
    return bool(item_layers & set(pad_layers)) or "*.Cu" in pad_layers


def _via_span(v: dict, order: list[str]) -> set[str]:
    a, b = order.index(v["layers"][0]), order.index(v["layers"][1])
    return set(order[min(a, b): max(a, b) + 1])


def pour_nets(board: Board) -> set[str]:
    """Nets that reach the rest of the board through a filled plane or pour (>= 2 % of the board area)."""
    return zone_nets(board) | {n for r in board.stackup.roles for n in r.nets}


def extract(board: Board, refs: list[str], *, template: str = "extracted", margin: float = DEFAULT_MARGIN_MM,
            origin: tuple[float, float] | None = None) -> Macro:
    """Cut a macro out of ``board``: the parts and the copper that joins their pads to each other.

    Per net, copper reachable from the parts' pads inside the region
    (courtyards grown by ``margin``) is collected, then dead ends are pruned
    until every remaining item lies on a path between the parts' pads or ends
    on a via of a plane net (a plane tap).  What the pruning removes is the
    route that leaves the block, for example ESD -> IC, which belongs to S6a
    (architecture 4.4 rule 5).  An outgoing signal net gets one port at the
    pad where that route left.
    """
    fps = [board.fp(r) for r in refs]
    order = [cl.canonical for cl in board.copper_layers]
    region = unary_union([s for fp in fps if (s := courtyard_polygon(fp)) is not None]).buffer(margin)
    if origin is None:
        origin = (_r6(sum(fp.x for fp in fps) / len(fps)), _r6(sum(fp.y for fp in fps) / len(fps)))
    home = max(("F", "B"), key=lambda s: (sum(1 for fp in fps if fp.side == s), s == "F"))
    frame = Transform(origin[0], origin[1], 0.0, home)
    planes = pour_nets(board)
    macro_nets = {p.net for fp in fps for p in fp.pads if p.net}
    pads = [(fp, p, _pad_shape(fp, p)) for fp in fps for p in fp.pads if p.net]
    tracks = [t for t in board.routing.get("tracks", []) if t["net"] in macro_nets]
    vias = [v for v in board.routing.get("vias", []) if v["net"] in macro_nets]
    net_pads_all = board.net_pads()
    outgoing = {n for n in macro_nets if n not in planes and any(fp.ref not in refs for fp, _p in net_pads_all[n])}

    def ends(i: int) -> list[tuple[float, float]]:
        return [tuple(tracks[i]["points"][0]), tuple(tracks[i]["points"][-1])]

    def touches_via(i: int, j: int) -> bool:
        v = vias[j]
        return tracks[i]["layer"] in _via_span(v, order) and any(
            math.dist(e, (v["x"], v["y"])) < v["diameter"] / 2 + TOL for e in ends(i))

    kept_t: set[int] = set()
    kept_v: set[int] = set()
    exit_pads: dict[str, list[tuple[Footprint, object, tuple[float, float]]]] = {}
    for net in sorted(macro_nets):
        t_idx = [i for i, t in enumerate(tracks) if t["net"] == net
                 and all(region.contains(Point(q)) for q in ends(i))
                 and not any(v["net"] == net and all(math.dist(e, (v["x"], v["y"])) < v["diameter"] / 2 + TOL
                                                     for e in ends(i)) for v in vias)]  # stubs inside a via add nothing
        v_idx = [i for i, v in enumerate(vias) if v["net"] == net and region.contains(Point(v["x"], v["y"]))]
        net_pads = [(fp, p, sh) for fp, p, sh in pads if p.net == net]

        def end_links(i, e, live_t, live_v, net_pads=net_pads):
            """What touches end ``e`` of track ``i``: part pads, live tracks, live vias."""
            t = tracks[i]
            out = [("p", k) for k, (_fp, p, sh) in enumerate(net_pads)
                   if _layer_ok({t["layer"]}, p.layers) and sh.contains(Point(e))]
            out += [("t", j) for j in live_t if j != i and tracks[j]["layer"] == t["layer"]
                    and LineString([tracks[j]["points"][0], tracks[j]["points"][-1]]).distance(Point(e)) < TOL]
            out += [("v", j) for j in live_v if t["layer"] in _via_span(vias[j], order)
                    and math.dist(e, (vias[j]["x"], vias[j]["y"])) < vias[j]["diameter"] / 2 + TOL]
            return out

        # 1. reach: copper joined to the parts' pads inside the region
        live_t: set[int] = set()
        live_v: set[int] = set()
        frontier = [("t", i) for i in t_idx if any(lk[0] == "p" for e in ends(i) for lk in end_links(i, e, (), ()))]
        frontier += [("v", i) for i in v_idx
                     if any(sh.contains(Point(vias[i]["x"], vias[i]["y"])) for _fp, _p, sh in net_pads)]
        while frontier:
            kind, i = frontier.pop()
            if kind == "t" and i not in live_t:
                live_t.add(i)
                frontier += [lk for e in ends(i) for lk in end_links(i, e, t_idx, v_idx) if lk[0] != "p"]
            elif kind == "v" and i not in live_v:
                live_v.add(i)
                frontier += [("t", j) for j in t_idx if touches_via(j, i)]

        # 2. prune dead ends; remember the pad a pruned branch hung from
        changed = True
        while changed:
            changed = False
            for i in sorted(live_t):
                links = [end_links(i, e, live_t, live_v) for e in ends(i)]
                touched = {x for group in links for x in group}
                if all(links) and not (len(touched) == 1 and next(iter(touched))[0] == "v"):
                    continue  # a stub lying inside one via's copper joins nothing
                live_t.discard(i)
                changed = True
                far = next((e for e, lk in zip(ends(i), links, strict=True) if not lk), ends(i)[-1])
                for lk in (x for group in links for x in group if x[0] == "p"):
                    fp, pad, _sh = net_pads[lk[1]]
                    exit_pads.setdefault(net, []).append((fp, pad, far))
            if net in planes:
                continue  # a via of a plane net is a terminal: it taps the plane
            for i in sorted(live_v):
                # a signal via earns its place only by joining two copper layers
                layers = {tracks[j]["layer"] for j in live_t if touches_via(j, i)}
                layers |= {lay for _fp, p, sh in net_pads if sh.contains(Point(vias[i]["x"], vias[i]["y"]))
                           for lay in p.layers}
                if len(layers) < 2:
                    live_v.discard(i)
                    changed = True
        kept_t |= live_t
        kept_v |= live_v

    cls_width = {}
    for net in macro_nets:
        nc = board.netclass_of(net)
        cls_width[net] = (nc.track_width + 2 * nc.clearance) if nc else 0.6
    parts = [MacroPart(fp.ref, fp.uuid, *to_local((fp.x, fp.y), frame, False), norm_angle(fp.rotation),
                       "same" if fp.side == home else "opposite") for fp in fps]
    m_tracks = [MTrack(tracks[i]["layer"], tracks[i]["width"], tracks[i]["net"],
                       [to_local(tuple(q), frame, False) for q in tracks[i]["points"]]) for i in sorted(kept_t)]
    m_vias = [MVia(*to_local((vias[i]["x"], vias[i]["y"]), frame, False), vias[i]["diameter"], vias[i]["drill"],
                   vias[i]["net"], tuple(vias[i]["layers"])) for i in sorted(kept_v)]
    ports = []
    for net in sorted(outgoing):
        candidates = exit_pads.get(net)
        if candidates:
            fp, pad, far = min(candidates, key=lambda c: (c[0].ref, c[1].number))
        else:  # the net leaves straight from a pad: the pad nearest the outside pads
            outside = [fp2.pad_world(p2) for fp2, p2 in net_pads_all[net] if fp2.ref not in refs]
            far = (sum(q[0] for q in outside) / len(outside), sum(q[1] for q in outside) / len(outside))
            fp, pad = min(((fp, p) for fp in fps for p in fp.pads if p.net == net),
                          key=lambda c: (math.dist(c[0].pad_world(c[1]), far), c[0].ref, c[1].number))
        px, py = fp.pad_world(pad)
        d = math.degrees(math.atan2(-(far[1] - py), far[0] - px))
        lx, ly = to_local((px, py), frame, False)
        layer = pad.layers[0] if pad.layers and pad.layers[0] != "*.Cu" else ("F.Cu" if fp.side == "F" else "B.Cu")
        ports.append(Port(net, "pad", layer, lx, ly, norm_angle(d), round(cls_width[net], 4)))
    assembly = [Keepout("placement", side, [to_local(q, frame, False) for q in shape.exterior.coords],
                        "assembly: union of courtyards")
                for side in ("F", "B")
                if (shape := unary_union([s for fp in fps if fp.side == side and (s := courtyard_polygon(fp)) is not None]))
                and not shape.is_empty and shape.geom_type == "Polygon"]
    roles = {r.layer: r.role + (":" + r.net if r.net else "") for r in board.stackup.roles}
    macro = Macro(template=template, home_side=home, layer_count=board.layer_count, parts=parts, tracks=m_tracks,
                  vias=m_vias, keepouts=assembly, ports=ports, layer_roles=roles, origin=origin)
    macro.checks = validate(macro, board)
    return macro


# --------------------------------------------------------------------------
# validation (rules 1-4)
# --------------------------------------------------------------------------
def _end_points(macro: Macro) -> list[tuple[str, str, tuple[float, float]]]:
    return [(t.net, t.layer, tuple(q)) for t in macro.tracks for q in (t.points[0], t.points[-1])]


def validate(macro: Macro, board: Board) -> list[Check]:
    fps = {p.uuid: board.by_uuid(p.uuid) for p in macro.parts}
    frame_parts = {p.uuid: p for p in macro.parts}
    pad_shapes = []
    for uuid, fp in fps.items():
        part = frame_parts[uuid]
        local_fp = Footprint(fp.ref, fp.value, fp.lib_id, part.x, part.y, part.rotation, fp.side, pads=fp.pads)
        pad_shapes += [(p.net, p.layers, _pad_shape(local_fp, p)) for p in fp.pads if p.net]
    planes = pour_nets(board)
    port_pts = [(p.net, (p.x, p.y)) for p in macro.ports]

    def touches(net: str, layer: str, pt: tuple[float, float], skip: int | None = None) -> bool:
        if any(n == net and _layer_ok({layer}, ls) and s.contains(Point(pt)) for n, ls, s in pad_shapes):
            return True
        if any(n == net and math.dist(pt, q) < TOL for n, q in port_pts):
            return True
        if any(v.net == net and math.dist(pt, (v.x, v.y)) < v.diameter / 2 + TOL for v in macro.vias):
            return True
        return any(i != skip and t.net == net and t.layer == layer
                   and LineString([t.points[0], t.points[-1]]).distance(Point(pt)) < TOL
                   for i, t in enumerate(macro.tracks))

    loose = 0
    for i, t in enumerate(macro.tracks):
        for q in (t.points[0], t.points[-1]):
            if not touches(t.net, t.layer, tuple(q), skip=i):
                loose += 1
    vias_unjoined = 0
    for v in macro.vias:
        joined = any(math.dist((v.x, v.y), tuple(q)) < v.diameter / 2 + TOL
                     for t in macro.tracks if t.net == v.net for q in (t.points[0], t.points[-1]))
        in_pad = any(n == v.net and s.contains(Point(v.x, v.y)) for n, _ls, s in pad_shapes)
        if not (joined or in_pad):
            vias_unjoined += 1
    per_net: dict[str, int] = {}
    for p in macro.ports:
        per_net[p.net] = per_net.get(p.net, 0) + 1
    extra_ports = sum(c - 1 for n, c in per_net.items() if n not in planes)
    used = {t.layer for t in macro.tracks} | {lay for v in macro.vias for lay in v.layers}
    used |= {lay for z in macro.zones for lay in z.layers}
    role_mismatch = sum(1 for lay in used
                        if macro.layer_roles.get(lay, "").split(":")[0]
                        != macro.layer_roles.get(flip_layer(lay, macro.layer_count), "").split(":")[0])
    return [
        Check("copper_complete_loose_ends", loose, 0, "architecture 4.4 rule 1", loose == 0),
        Check("vias_joined_to_own_pad", vias_unjoined, 0, "architecture 4.4 rule 2", vias_unjoined == 0),
        Check("extra_ports_per_net", extra_ports, 0, "architecture 4.4 rule 3", extra_ports == 0),
        Check("flip_layer_role_mismatch", role_mismatch, 0, "architecture 4.4 rule 4", role_mismatch == 0),
    ]


def home(macro: Macro) -> Transform:
    """The pose the macro was extracted at: instantiating there reproduces the source board."""
    return Transform(macro.origin[0], macro.origin[1], 0.0, macro.home_side)


def flip_allowed(macro: Macro) -> bool:
    return next(c.passed for c in macro.checks if c.name == "flip_layer_role_mismatch")
