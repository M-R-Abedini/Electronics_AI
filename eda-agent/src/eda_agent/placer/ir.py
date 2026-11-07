# SPDX-License-Identifier: MIT
"""EDA-neutral board model (Board IR) used by the placement engine.

The IR is deliberately small: it holds exactly what placement and routability
evaluation need, in millimetres and KiCad's screen convention (+Y down,
positive angle = counter-clockwise on screen).  Adapters convert KiCad or
Altium data into it and write placements back.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field

from .geometry import Box, Point, box_of, norm_angle, rotated_box, transform

GROUND_HINTS = ("GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD", "EARTH", "0V")


@dataclass
class Pad:
    number: str
    net: str | None
    local: Point  # footprint-local offset (unrotated frame)
    size: tuple[float, float]
    angle: float = 0.0  # pad orientation relative to the footprint
    shape: str = "rect"
    kind: str = "smd"  # smd | thru_hole | np_thru_hole | connect
    layers: tuple[str, ...] = ("F.Cu",)
    drill: float | None = None
    function: str | None = None
    pin_type: str | None = None


@dataclass
class Footprint:
    ref: str
    value: str
    lib_id: str
    x: float
    y: float
    rotation: float
    side: str = "F"  # F (top) | B (bottom)
    locked: bool = False
    pads: list[Pad] = field(default_factory=list)
    courtyard_local: Box | None = None
    uuid: str | None = None
    sheet: str | None = None
    description: str = ""
    attributes: tuple[str, ...] = ()

    # ------------------------------------------------------------------ pose
    @property
    def position(self) -> Point:
        return self.x, self.y

    def pad_world(self, pad: Pad) -> Point:
        return transform(pad.local, (self.x, self.y), self.rotation)

    def pads_world(self) -> Iterator[tuple[Pad, Point]]:
        for pad in self.pads:
            yield pad, self.pad_world(pad)

    @property
    def body_local(self) -> Box:
        """Courtyard if available, otherwise the pad envelope plus 0.25 mm."""
        if self.courtyard_local is not None:
            return self.courtyard_local
        pts = []
        for p in self.pads:
            hw, hh = p.size[0] / 2, p.size[1] / 2
            if p.angle % 180 not in (0.0,):
                hw = hh = max(hw, hh)
            pts += [(p.local[0] - hw, p.local[1] - hh), (p.local[0] + hw, p.local[1] + hh)]
        b = box_of(pts) or (-0.5, -0.5, 0.5, 0.5)
        return b[0] - 0.25, b[1] - 0.25, b[2] + 0.25, b[3] + 0.25

    def box(self, x: float | None = None, y: float | None = None, rotation: float | None = None) -> Box:
        return rotated_box(
            self.body_local,
            (self.x if x is None else x, self.y if y is None else y),
            self.rotation if rotation is None else rotation,
        )

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
class Rules:
    clearance: float = 0.2
    track_width: float = 0.25
    via_diameter: float = 0.6
    via_drill: float = 0.3
    courtyard_clearance: float = 0.0


@dataclass
class Board:
    footprints: list[Footprint]
    outline: list[list[Point]] = field(default_factory=list)  # closed polygons
    copper_layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    rules: Rules = field(default_factory=Rules)
    source_path: str | None = None
    name: str = "board"

    def __post_init__(self) -> None:
        self._index = {fp.ref: fp for fp in self.footprints}

    # ------------------------------------------------------------- lookups
    def fp(self, ref: str) -> Footprint:
        return self._index[ref]

    def reindex(self) -> None:
        self._index = {fp.ref: fp for fp in self.footprints}

    @property
    def outline_box(self) -> Box | None:
        return box_of(pt for poly in self.outline for pt in poly)

    def nets(self) -> dict[str, list[tuple[Footprint, Pad]]]:
        out: dict[str, list[tuple[Footprint, Pad]]] = {}
        for fp in self.footprints:
            for pad in fp.pads:
                if pad.net:
                    out.setdefault(pad.net, []).append((fp, pad))
        return out

    def copy(self) -> Board:
        return copy.deepcopy(self)

    # ----------------------------------------------------------- placement
    def placement(self) -> dict[str, tuple[float, float, float]]:
        return {fp.ref: (fp.x, fp.y, fp.rotation) for fp in self.footprints}

    def apply_placement(self, placement: dict[str, tuple[float, float, float]]) -> None:
        for ref, (x, y, rot) in placement.items():
            fp = self._index[ref]
            fp.x, fp.y, fp.rotation = float(x), float(y), norm_angle(rot)


def is_ground_net(name: str | None) -> bool:
    if not name:
        return False
    upper = name.upper().lstrip("/")
    return any(upper == h or upper.startswith(h + "_") or upper.endswith("_" + h) for h in GROUND_HINTS)


def is_power_net(name: str | None) -> bool:
    """Heuristic rail recognition; callers may override via intent."""
    if not name:
        return False
    upper = name.upper().lstrip("/")
    if is_ground_net(upper):
        return True
    if upper.startswith(("+", "VCC", "VDD", "VBUS", "VIN", "VBAT", "VSYS", "V_", "PWR", "VREF", "AVDD", "DVDD", "IOVDD")):
        return True
    if upper.startswith("-") and any(ch.isdigit() for ch in upper):
        return True
    # e.g. "3V3", "+1V8", "5V", "12V"
    core = upper.lstrip("+")
    return len(core) <= 6 and core[:1].isdigit() and "V" in core
