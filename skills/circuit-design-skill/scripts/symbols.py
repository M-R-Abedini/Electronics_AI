#!/usr/bin/env python3
"""Symbol geometry tables for LTspice ``.asc`` and KiCad ``.kicad_sch``.

The LTspice numbers come from the vendor schematic reference (local symbol
coordinates, +Y down).  The KiCad numbers are our own embedded library, with
pin numbers deliberately chosen to match SPICE netlist order so a generated
schematic and netlist cannot disagree.

Do not guess these numbers.  A pin that is one grid unit off produces a
schematic that netlists with ``NC_*`` nodes while looking almost correct.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# LTspice .asc geometry
# ---------------------------------------------------------------------------
# type -> (symbol name, {pin number: (local x, local y)})
# Pin numbers are SPICE netlist order.  Local coordinates are the values from
# the LTspice schematic reference, at rotation R0.
ASC_SYMBOLS: dict[str, tuple[str, dict[str, tuple[int, int]]]] = {
    "R": ("res", {"1": (16, 16), "2": (16, 96)}),
    "C": ("cap", {"1": (16, 0), "2": (16, 64)}),
    "L": ("ind", {"1": (16, 16), "2": (16, 96)}),
    "V": ("voltage", {"1": (0, 16), "2": (0, 96)}),
    "I": ("current", {"1": (0, 0), "2": (0, 80)}),
    "D": ("diode", {"1": (16, 0), "2": (16, 64)}),
    "LED": ("LED", {"1": (16, 0), "2": (16, 64)}),
    "ZENER": ("zener", {"1": (16, 0), "2": (16, 64)}),
    "NPN": ("npn", {"1": (64, 0), "2": (0, 48), "3": (64, 96)}),
    "PNP": ("pnp", {"1": (64, 0), "2": (0, 48), "3": (64, 96)}),
    "NMOS": ("nmos", {"1": (48, 0), "2": (0, 80), "3": (48, 96)}),
    "PMOS": ("pmos", {"1": (48, 0), "2": (0, 80), "3": (48, 96)}),
    "OPAMP": ("opamp2", {"1": (-32, 80), "2": (-32, 48), "3": (0, 32),
                         "4": (0, 96), "5": (32, 64)}),
    "SW": ("sw", {"1": (0, 16), "2": (0, 96), "3": (-48, 80), "4": (-48, 32)}),
    "E": ("e", {"1": (0, 16), "2": (0, 96), "3": (-48, 32), "4": (-48, 80)}),
    "G": ("g", {"1": (0, 96), "2": (0, 16), "3": (-48, 32), "4": (-48, 80)}),
}

# SPICE prefix used in the netlist.
SPICE_PREFIX: dict[str, str] = {
    "R": "R", "C": "C", "L": "L", "V": "V", "I": "I",
    "D": "D", "LED": "D", "ZENER": "D",
    "NPN": "Q", "PNP": "Q", "NMOS": "M", "PMOS": "M",
    "OPAMP": "X", "SW": "S", "E": "E", "G": "G",
}


def asc_pin_world(
    sx: int, sy: int, rot: int, mirror: bool, px: int, py: int
) -> tuple[int, int]:
    """Apply the LTspice orientation transform (Y is down in .asc files)."""
    rot = rot % 360
    if rot == 0:
        rx, ry = px, py
    elif rot == 90:
        rx, ry = -py, px
    elif rot == 180:
        rx, ry = -px, -py
    elif rot == 270:
        rx, ry = py, -px
    else:
        raise ValueError(f"unsupported LTspice rotation {rot}")
    if mirror:
        # Mirror is applied after rotation, by negating relative x.
        rx = -rx
    return sx + rx, sy + ry


def asc_pins(ctype: str, sx: int, sy: int, rot: int = 0, mirror: bool = False) -> dict[str, tuple[int, int]]:
    if ctype not in ASC_SYMBOLS:
        raise ValueError(f"no LTspice symbol for type {ctype!r}")
    _sym, pins = ASC_SYMBOLS[ctype]
    return {num: asc_pin_world(sx, sy, rot, mirror, px, py) for num, (px, py) in pins.items()}


# ---------------------------------------------------------------------------
# KiCad embedded symbol library
# ---------------------------------------------------------------------------
# type -> pin number -> (local x, local y, angle, length)
# Local Y is up in KiCad symbol libraries.  Angle is the direction from the
# connection point toward the body.
KICAD_PINS: dict[str, dict[str, tuple[float, float, int, float]]] = {
    "R": {"1": (0.0, 6.35, 270, 2.54), "2": (0.0, -6.35, 90, 2.54)},
    "C": {"1": (0.0, 5.08, 270, 4.572), "2": (0.0, -5.08, 90, 4.572)},
    "L": {"1": (0.0, 5.08, 270, 1.27), "2": (0.0, -5.08, 90, 1.27)},
    "D": {"1": (0.0, 3.81, 270, 2.54), "2": (0.0, -3.81, 90, 2.54)},
    "LED": {"1": (0.0, 3.81, 270, 2.54), "2": (0.0, -3.81, 90, 2.54)},
    "ZENER": {"1": (0.0, 3.81, 270, 2.54), "2": (0.0, -3.81, 90, 2.54)},
    "NPN": {"1": (0.0, 5.08, 270, 2.54), "2": (-5.08, 0.0, 0, 2.54), "3": (0.0, -5.08, 90, 2.54)},
    "PNP": {"1": (0.0, 5.08, 270, 2.54), "2": (-5.08, 0.0, 0, 2.54), "3": (0.0, -5.08, 90, 2.54)},
    "NMOS": {"1": (0.0, 5.08, 270, 2.54), "2": (-5.08, 0.0, 0, 2.54), "3": (0.0, -5.08, 90, 2.54)},
    "PMOS": {"1": (0.0, 5.08, 270, 2.54), "2": (-5.08, 0.0, 0, 2.54), "3": (0.0, -5.08, 90, 2.54)},
    "OPAMP": {
        "1": (-7.62, 2.54, 0, 2.54),
        "2": (-7.62, -2.54, 0, 2.54),
        "3": (0.0, 7.62, 270, 2.54),
        "4": (0.0, -7.62, 90, 2.54),
        "5": (7.62, 0.0, 180, 2.54),
    },
    "V": {"1": (0.0, 3.81, 270, 1.27), "2": (0.0, -3.81, 90, 1.27)},
    "I": {"1": (0.0, 3.81, 270, 1.27), "2": (0.0, -3.81, 90, 1.27)},
    "SW": {
        "1": (0.0, 3.81, 270, 1.27), "2": (0.0, -3.81, 90, 1.27),
        "3": (-5.08, 1.27, 0, 2.54), "4": (-5.08, -1.27, 0, 2.54),
    },
    "E": {
        "1": (0.0, 3.81, 270, 1.27), "2": (0.0, -3.81, 90, 1.27),
        "3": (-5.08, 1.27, 0, 2.54), "4": (-5.08, -1.27, 0, 2.54),
    },
    "G": {
        "1": (0.0, 3.81, 270, 1.27), "2": (0.0, -3.81, 90, 1.27),
        "3": (-5.08, 1.27, 0, 2.54), "4": (-5.08, -1.27, 0, 2.54),
    },
}


def kicad_rotate(px: float, py: float, rot: int) -> tuple[float, float]:
    """Rotate a local symbol coordinate (Y up) counter-clockwise."""
    rot = rot % 360
    if rot == 0:
        return px, py
    if rot == 90:
        return -py, px
    if rot == 180:
        return -px, -py
    if rot == 270:
        return py, -px
    raise ValueError(f"unsupported KiCad rotation {rot}")


def kicad_pin_world(
    sx: float, sy: float, rot: int, px: float, py: float,
    mirror: str | None = None,
) -> tuple[float, float]:
    """Convert a symbol-local pin to schematic world coordinates (Y down)."""
    rx, ry = kicad_rotate(px, py, rot)
    if mirror == "x":
        ry = -ry
    elif mirror == "y":
        rx = -rx
    return sx + rx, sy - ry


def kicad_pins(ctype: str, sx: float, sy: float, rot: int = 0,
               mirror: str | None = None) -> dict[str, tuple[float, float, int]]:
    """Return pin -> (world x, world y, world pin angle)."""
    if ctype not in KICAD_PINS:
        raise ValueError(f"no KiCad symbol for type {ctype!r}")
    out = {}
    for num, (px, py, angle, _length) in KICAD_PINS[ctype].items():
        x, y = kicad_pin_world(sx, sy, rot, px, py, mirror)
        out[num] = (x, y, (angle + rot) % 360)
    return out


# ---------------------------------------------------------------------------
# KiCad symbol s-expressions
# ---------------------------------------------------------------------------

def _effects(size: float = 1.27, hide: bool = False, justify: str | None = None) -> str:
    bits = [f"(font (size {size} {size}))"]
    if justify:
        bits.append(f"(justify {justify})")
    if hide:
        bits.append("(hide yes)")
    return "(effects " + " ".join(bits) + ")"


def _pin(num: str, name: str, x: float, y: float, angle: int, length: float,
         etype: str = "passive") -> str:
    return (
        f"\t\t\t(pin {etype} line (at {x:g} {y:g} {angle}) (length {length:g})\n"
        f"\t\t\t\t(name \"{name}\" {_effects()})\n"
        f"\t\t\t\t(number \"{num}\" {_effects()})\n"
        f"\t\t\t)"
    )


def _rect(x1: float, y1: float, x2: float, y2: float, fill: str = "none") -> str:
    return (
        f"\t\t\t(rectangle (start {x1:g} {y1:g}) (end {x2:g} {y2:g})\n"
        f"\t\t\t\t(stroke (width 0.254) (type default)) (fill (type {fill}))\n"
        f"\t\t\t)"
    )


def _poly(points: list[tuple[float, float]], fill: str = "none") -> str:
    pts = " ".join(f"(xy {x:g} {y:g})" for x, y in points)
    return (
        f"\t\t\t(polyline (pts {pts})\n"
        f"\t\t\t\t(stroke (width 0.254) (type default)) (fill (type {fill}))\n"
        f"\t\t\t)"
    )


def _circle(cx: float, cy: float, r: float) -> str:
    return (
        f"\t\t\t(circle (center {cx:g} {cy:g}) (radius {r:g})\n"
        f"\t\t\t\t(stroke (width 0.254) (type default)) (fill (type none))\n"
        f"\t\t\t)"
    )


def _lib_symbol(ctype: str) -> str:
    """Return one embedded KiCad library symbol."""
    ref_prefix = {
        "R": "R", "C": "C", "L": "L", "V": "V", "I": "I",
        "D": "D", "LED": "D", "ZENER": "D",
        "NPN": "Q", "PNP": "Q", "NMOS": "M", "PMOS": "M",
        "OPAMP": "U", "SW": "S", "E": "E", "G": "G",
    }[ctype]
    name = f"circuit:{ctype}"
    pins = KICAD_PINS[ctype]

    graphics: list[str] = []
    if ctype == "R":
        graphics.append(_rect(-1.016, -3.81, 1.016, 3.81))
    elif ctype == "C":
        graphics += [_poly([(-1.524, 0.508), (1.524, 0.508)]),
                     _poly([(-1.524, -0.508), (1.524, -0.508)])]
    elif ctype == "L":
        graphics.append(_rect(-1.016, -3.81, 1.016, 3.81))
    elif ctype in ("D", "LED", "ZENER"):
        graphics += [_poly([(-1.27, 1.27), (1.27, 1.27), (0, -1.27), (-1.27, 1.27)], "none"),
                     _poly([(-1.27, -1.27), (1.27, -1.27)])]
        if ctype == "ZENER":
            graphics.append(_poly([(-1.27, -1.27), (-0.635, -0.635)]))
        if ctype == "LED":
            graphics += [_poly([(1.27, 1.27), (2.54, 2.54)]),
                         _poly([(2.54, 2.54), (1.905, 2.54)]),
                         _poly([(2.54, 2.54), (2.54, 1.905)])]
    elif ctype in ("NPN", "PNP"):
        graphics += [_circle(0, 0, 2.54),
                     _poly([(-2.54, 0), (0, 0)]),
                     _poly([(0, 0), (1.27, 1.27)]),
                     _poly([(0, 0), (1.27, -1.27)]),
                     _poly([(0.508, 1.27), (1.27, 1.27), (1.27, 0.508)])]
        if ctype == "PNP":
            graphics.append(_poly([(-1.27, 0), (0, 0)]))
    elif ctype in ("NMOS", "PMOS"):
        graphics += [_rect(-1.27, -2.54, 1.27, 2.54),
                     _poly([(-2.54, 0), (-1.27, 0)]),
                     _poly([(0, 2.54), (0, 1.27)]),
                     _poly([(0, -2.54), (0, -1.27)])]
    elif ctype == "OPAMP":
        graphics.append(_poly([(-5.08, 5.08), (-5.08, -5.08), (5.08, 0), (-5.08, 5.08)], "none"))
    elif ctype in ("V", "I"):
        graphics.append(_circle(0, 0, 2.54))
        graphics += [_poly([(-0.635, 0), (0.635, 0)]), _poly([(0, -0.635), (0, 0.635)])]
        if ctype == "I":
            graphics.append(_poly([(-0.635, -1.27), (0, -0.635), (0.635, -1.27)]))
    elif ctype in ("SW", "E", "G"):
        graphics.append(_rect(-2.54, -2.54, 2.54, 2.54))

    pin_text = "\n".join(
        _pin(num, "~", x, y, angle, length)
        for num, (x, y, angle, length) in pins.items()
    )
    unit_graphics = "\n".join(graphics)
    return (
        f'\t\t(symbol "{name}"\n'
        f'\t\t\t(pin_names (offset 0.508))\n'
        f'\t\t\t(exclude_from_sim no)\n'
        f'\t\t\t(in_bom yes)\n'
        f'\t\t\t(on_board yes)\n'
        f'\t\t\t(property "Reference" "{ref_prefix}" (at 0 8.89 0) {_effects()})\n'
        f'\t\t\t(property "Value" "{ctype}" (at 0 -8.89 0) {_effects()})\n'
        f'\t\t\t(property "Footprint" "" (at 0 0 0) {_effects(hide=True)})\n'
        f'\t\t\t(property "Datasheet" "" (at 0 0 0) {_effects(hide=True)})\n'
        f'\t\t\t(property "Description" "" (at 0 0 0) {_effects(hide=True)})\n'
        f'\t\t\t(symbol "{ctype}_0_1"\n{unit_graphics}\n\t\t\t)\n'
        f'\t\t\t(symbol "{ctype}_1_1"\n{pin_text}\n\t\t\t)\n'
        f'\t\t)'
    )


def kicad_lib_symbols(types: list[str]) -> str:
    seen: list[str] = []
    for t in types:
        if t not in seen:
            seen.append(t)
    body = "\n".join(_lib_symbol(t) for t in seen)
    return "\t(lib_symbols\n" + body + "\n\t)"
