# SPDX-License-Identifier: MIT
"""EDA-neutral Board IR, schema version 1 (architecture 4.1 to 4.3).

Units are millimetres in KiCad's screen convention (+Y down, positive angle =
counter-clockwise on screen).  Footprint-local geometry (pads, courtyard) is
in the footprint's frame *on its current side*:
``world = transform(local, (x, y), rotation)``.

Adapters fill the IR; nothing here talks to an EDA tool.  Derived facts
(layer roles, stackup policy, differential pairs, unknowns) are computed by
``derive`` so every adapter gets the same rules.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field

from .geometry import Box, Point, box_of, polygon_area, transform

SCHEMA = "electronics-ai/board-ir"
SCHEMA_VERSION = 1

# Declared parameters of the layer-role rule (architecture 4.2).  They change
# only with evidence logged in the decision log.
ROLE_MIN_NET_FRACTION = 0.02  # a net's fill below 2 % of the board area is ignored (teardrops, islands)
ROLE_POUR_FRACTION = 0.30  # one net filling >= 30 % of the board makes the layer a pour/plane layer
ROLE_PLANE_SHARE = 0.80  # ... and that net must hold >= 80 % of the layer's counted fill
CAPACITY_WARN = 0.70  # courtyard area / usable area of a side above this is reported

GROUND_HINTS = ("GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD", "EARTH", "0V")


@dataclass
class Pad:
    number: str
    net: str | None
    local: Point
    size: tuple[float, float]
    rotation: float = 0.0  # relative to the footprint
    shape: str = "rect"
    kind: str = "smd"  # smd | thru_hole | np_thru_hole | connect
    layers: tuple[str, ...] = ()
    drill: tuple[float, float] | None = None
    function: str | None = None
    pin_type: str | None = None
    uuid: str | None = None


@dataclass
class Courtyard:
    source: str  # file | derived | none
    polygons: list[list[Point]] = field(default_factory=list)  # footprint frame
    margin_mm: float | None = None


@dataclass
class Footprint:
    ref: str
    value: str
    lib_id: str
    x: float
    y: float
    rotation: float
    side: str = "F"
    locked: bool = False
    pads: list[Pad] = field(default_factory=list)
    courtyard: Courtyard = field(default_factory=lambda: Courtyard("none"))
    uuid: str | None = None
    sheet: str | None = None
    description: str = ""
    attributes: tuple[str, ...] = ()

    @property
    def position(self) -> Point:
        return self.x, self.y

    def pad_world(self, pad: Pad) -> Point:
        return transform(pad.local, (self.x, self.y), self.rotation)

    def pads_world(self) -> Iterator[tuple[Pad, Point]]:
        for pad in self.pads:
            yield pad, self.pad_world(pad)

    def courtyard_world(self, x: float | None = None, y: float | None = None,
                        rotation: float | None = None) -> list[list[Point]]:
        o = (self.x if x is None else x, self.y if y is None else y)
        r = self.rotation if rotation is None else rotation
        return [[transform(p, o, r) for p in poly] for poly in self.courtyard.polygons]

    def box(self, x: float | None = None, y: float | None = None, rotation: float | None = None) -> Box:
        pts = [p for poly in self.courtyard_world(x, y, rotation) for p in poly]
        return box_of(pts) or (self.x, self.y, self.x, self.y)

    @property
    def courtyard_area(self) -> float:
        return sum(polygon_area(p) for p in self.courtyard.polygons)

    @property
    def is_through_hole(self) -> bool:
        return any(p.kind == "thru_hole" for p in self.pads)

    @property
    def nets(self) -> set[str]:
        return {p.net for p in self.pads if p.net}

    @property
    def prefix(self) -> str:
        return "".join(ch for ch in self.ref if ch.isalpha()).upper()


@dataclass
class NetClass:
    name: str
    clearance: float
    track_width: float
    via_diameter: float
    via_drill: float
    diff_pair_width: float | None = None
    diff_pair_gap: float | None = None


@dataclass
class Net:
    name: str
    netclass: str
    pads: int


@dataclass
class CopperLayer:
    name: str  # user name as the EDA shows it
    canonical: str  # F.Cu, In1.Cu, ... B.Cu
    index: int
    declared_type: str = "signal"


@dataclass
class StackupLayer:
    name: str
    type: str  # copper | core | prepreg | mask | silk | paste | other
    thickness_mm: float | None = None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    material: str | None = None


@dataclass
class LayerRole:
    layer: str  # canonical
    role: str  # signal | plane | mixed
    net: str | None = None  # plane net, or dominant pour net of a mixed layer
    nets: list[str] = field(default_factory=list)  # nets above ROLE_MIN_NET_FRACTION
    fill_fraction: float = 0.0
    confidence: float = 1.0


@dataclass
class Stackup:
    source: str  # file | missing
    layers: list[StackupLayer] = field(default_factory=list)
    board_thickness_mm: float | None = None
    roles: list[LayerRole] = field(default_factory=list)
    policy: str = "locked"  # locked | propose | needs_decision


@dataclass
class DiffPair:
    positive: str
    negative: str
    source: str  # name_pattern | netclass
    confirmed: bool = False  # set by intent (F2)


@dataclass
class Capabilities:
    can_flip: bool
    can_rotate_any_angle: bool
    can_write_copper: bool
    can_live_apply: bool


@dataclass
class Outline:
    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)


@dataclass
class Board:
    name: str
    footprints: list[Footprint]
    outline: list[Outline] = field(default_factory=list)
    outline_valid: bool = True
    board_area_mm2: float = 0.0
    copper_layers: list[CopperLayer] = field(default_factory=list)
    rules: dict[str, float] = field(default_factory=dict)
    netclasses: list[NetClass] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    zones: list[dict] = field(default_factory=list)
    rule_areas: list[dict] = field(default_factory=list)
    obstacles: list[dict] = field(default_factory=list)
    zone_fill_by_layer_mm2: dict[str, dict[str, float]] = field(default_factory=dict)
    stackup: Stackup = field(default_factory=lambda: Stackup("missing"))
    diff_pairs: list[DiffPair] = field(default_factory=list)
    capabilities: Capabilities = field(default_factory=lambda: Capabilities(True, True, True, False))
    unknowns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    routing_items: int = 0
    routing: dict = field(default_factory=dict)  # existing tracks and vias, for review renders
    teardrops: int = 0  # counted only; KiCad regenerates them
    drc_severities: dict[str, str] = field(default_factory=dict)  # project overrides, e.g. ignore

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self._index = {fp.ref: fp for fp in self.footprints}
        self._uuid = {fp.uuid: fp for fp in self.footprints if fp.uuid}

    def fp(self, ref: str) -> Footprint:
        return self._index[ref]

    def by_uuid(self, uuid: str) -> Footprint:
        return self._uuid[uuid]

    @property
    def outline_box(self) -> Box | None:
        return box_of(pt for poly in self.outline for pt in poly.outer)

    @property
    def layer_count(self) -> int:
        return len(self.copper_layers)

    def netclass_of(self, net: str) -> NetClass | None:
        names = {n.name: n.netclass for n in self.nets}
        classes = {c.name: c for c in self.netclasses}
        return classes.get(names.get(net, ""))

    def net_pads(self) -> dict[str, list[tuple[Footprint, Pad]]]:
        out: dict[str, list[tuple[Footprint, Pad]]] = {}
        for fp in self.footprints:
            for pad in fp.pads:
                if pad.net:
                    out.setdefault(pad.net, []).append((fp, pad))
        return out

    def fixed_refs(self) -> set[str]:
        """Parts the from-scratch mode keeps (architecture 4.1); intent may add more in F2."""
        return {fp.ref for fp in self.footprints if fp.locked}

    def side_counts(self) -> dict[str, int]:
        return {s: sum(1 for fp in self.footprints if fp.side == s) for s in ("F", "B")}

    def placement(self) -> dict[str, dict]:
        """``{uuid: pose}``: references can repeat, UUIDs cannot."""
        return {fp.uuid: {"ref": fp.ref, "x": fp.x, "y": fp.y, "rotation": fp.rotation, "side": fp.side}
                for fp in self.footprints if fp.uuid}

    def copy(self) -> Board:
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_index", None)
        data.pop("_uuid", None)
        return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, **data}


# --------------------------------------------------------------------------
# derived facts
# --------------------------------------------------------------------------
_PAIR_SUFFIXES = (("+", "-"), ("_P", "_N"), ("P", "N"), ("_DP", "_DN"), ("DP", "DM"))


def find_diff_pairs(net_names: list[str]) -> list[DiffPair]:
    """Pairs by KiCad's naming convention (suffix +/-, P/N); unconfirmed until intent (F2)."""
    names = set(net_names)
    pairs: list[DiffPair] = []
    used: set[str] = set()
    for name in sorted(names):
        for pos, neg in _PAIR_SUFFIXES:
            if name.endswith(pos) and len(name) > len(pos):
                other = name[: -len(pos)] + neg
                if other in names and name not in used and other not in used:
                    pairs.append(DiffPair(positive=name, negative=other, source="name_pattern"))
                    used |= {name, other}
                    break
    return pairs


def infer_layer_roles(board: Board) -> list[LayerRole]:
    """Role of each copper layer from filled area per net, not from zone presence (4.2)."""
    area = board.board_area_mm2 or 0.0
    names = {cl.name: cl for cl in board.copper_layers}
    roles = []
    last = len(board.copper_layers) - 1
    for cl in board.copper_layers:
        fills = board.zone_fill_by_layer_mm2.get(cl.name, {})
        counted = {n: a for n, a in fills.items() if n and area and a / area >= ROLE_MIN_NET_FRACTION}
        total = sum(counted.values())
        frac = round(total / area, 4) if area else 0.0
        top = max(counted.items(), key=lambda kv: (kv[1], kv[0]), default=(None, 0.0))
        share = top[1] / total if total else 0.0
        inner = 0 < cl.index < last
        nets = sorted(counted, key=lambda n: (-counted[n], n))
        if frac < ROLE_POUR_FRACTION:
            role, net = "signal", None
            conf = 1.0 - frac / ROLE_POUR_FRACTION * 0.5
        elif inner and share >= ROLE_PLANE_SHARE:
            role, net = "plane", top[0]
            conf = min(1.0, 0.5 + (share - ROLE_PLANE_SHARE) / (1 - ROLE_PLANE_SHARE) * 0.5)
        else:
            role, net = "mixed", top[0]
            conf = 0.75
        roles.append(LayerRole(layer=names[cl.name].canonical, role=role, net=net, nets=nets,
                               fill_fraction=frac, confidence=round(conf, 3)))
    return roles


def stackup_policy(roles: list[LayerRole]) -> str:
    """locked for any board with a plane or pour; needs_decision when there is none (4.2)."""
    return "locked" if any(r.role in ("plane", "mixed") for r in roles) else "needs_decision"


def derive(board: Board) -> Board:
    board.stackup.roles = infer_layer_roles(board)
    board.stackup.policy = stackup_policy(board.stackup.roles)
    board.diff_pairs = find_diff_pairs([n.name for n in board.nets])
    unknowns = []
    if board.stackup.source != "file":
        unknowns.append("stackup: not described in the board file (dielectric thickness and εr unknown)")
    if board.stackup.policy == "needs_decision":
        unknowns.append("stackup_policy: no plane or pour on any layer; ask the user (architecture 4.2)")
    derived = sorted(fp.ref for fp in board.footprints if fp.courtyard.source != "file")
    if derived:
        unknowns.append(f"courtyard: {len(derived)} footprints without courtyard, derived from pads: "
                        + ", ".join(derived[:20]) + (" ..." if len(derived) > 20 else ""))
    if not board.outline_valid:
        unknowns.append("outline: Edge.Cuts does not form a closed outline")
    if board.diff_pairs:
        unknowns.append(f"diff_pairs: {len(board.diff_pairs)} pairs found by name, unconfirmed until intent (F2)")
    board.unknowns = unknowns
    board.warnings = capacity_warnings(board)
    return board


def capacity_warnings(board: Board) -> list[str]:
    """Courtyard area against usable board area per side (architecture 6, capacity row)."""
    area = board.board_area_mm2
    if not area:
        return ["capacity: board area unknown"]
    out = []
    for side in ("F", "B"):
        used = sum(fp.courtyard_area for fp in board.footprints if fp.side == side)
        ratio = used / area
        if ratio > CAPACITY_WARN:
            out.append(f"capacity: side {side} courtyards fill {ratio:.0%} of the board (> {CAPACITY_WARN:.0%})")
    return out


def is_ground_net(name: str | None) -> bool:
    if not name:
        return False
    upper = name.upper().lstrip("/")
    return any(upper == h or upper.startswith(h + "_") or upper.endswith("_" + h) for h in GROUND_HINTS)


def is_power_net(name: str | None) -> bool:
    """Heuristic rail recognition; intent (F2) overrides it."""
    if not name:
        return False
    upper = name.upper().lstrip("/")
    if is_ground_net(upper):
        return True
    if upper.startswith(("+", "VCC", "VDD", "VBUS", "VIN", "VBAT", "VSYS", "V_", "PWR", "VREF", "AVDD", "DVDD", "IOVDD")):
        return True
    if upper.startswith("-") and any(ch.isdigit() for ch in upper):
        return True
    core = upper.lstrip("+")
    return len(core) <= 6 and core[:1].isdigit() and "V" in core




def plane_zone_layers(board: Board) -> dict[str, list[str]]:
    """Zones the router must see as DSN planes: ``{zone uuid: [layer names]}`` (architecture 7.3).

    Only inner layers whose role is plane or mixed (split plane) qualify, and on
    them only zones of the nets that carry the layer's fill.  Pours on outer
    layers and small signal-net zones are removed for routing and refilled after.
    A multi-layer zone keeps just its plane layers (royalblue's GND zone spans
    F.Cu through B.Cu).
    """
    last = len(board.copper_layers) - 1
    nets_by_layer = {cl.name: set(role.nets) for cl, role in zip(board.copper_layers, board.stackup.roles)
                     if 0 < cl.index < last and role.role in ("plane", "mixed")}
    out: dict[str, list[str]] = {}
    for zone in board.zones:
        layers = [name for name in zone["layers"] if zone["net"] in nets_by_layer.get(name, ())]
        if layers:
            out[zone["uuid"]] = layers
    return dict(sorted(out.items()))
