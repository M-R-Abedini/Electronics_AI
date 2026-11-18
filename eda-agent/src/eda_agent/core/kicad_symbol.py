# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Author KiCad symbol (.kicad_sym) library content.

The schematic-symbol counterpart to :mod:`kicad_footprint`: KiCad exposes no API
for creating symbols, but ``.kicad_sym`` files are text s-expressions, so
eda-agent generates them directly. This closes the symbol half of the
library-authoring gap versus Altium's ``lib_create_symbol``. Output validates by
rendering with ``kicad-cli sym export svg``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from .kicad_footprint import _block_end, is_inside, is_plain_name


def standard_symbol_dirs(cli_path: Optional[str] = None) -> list[str]:
    """Directories holding KiCad ``.kicad_sym`` symbol libraries."""
    dirs: list[str] = []
    for key in ("KICAD9_SYMBOL_DIR", "KICAD_SYMBOL_DIR", "KICAD8_SYMBOL_DIR",
                "KICAD10_SYMBOL_DIR"):
        v = os.environ.get(key)
        if v and os.path.isdir(v):
            dirs.append(v)
    if cli_path:
        root = os.path.dirname(os.path.dirname(cli_path))
        cand = os.path.join(root, "share", "kicad", "symbols")
        if os.path.isdir(cand):
            dirs.append(cand)
    return dirs


def extract_symbol(lib_id: str, search_dirs: list[str]) -> Optional[str]:
    """Extract a symbol block ("Lib:Name") from a .kicad_sym library, renamed
    to its full lib_id for embedding in a schematic's lib_symbols. None if not
    found.

    The library half of the id becomes a FILENAME, so it has to be a
    plain name. Nothing upstream constrains it: ``DesignPlan.Part.lib_ref``
    is validated for length and nothing else, and it reaches here from a
    caller-supplied plan. Without this, an id could name a
    ``.kicad_sym`` anywhere on disk, and this function would report it as
    a symbol found in one of ``search_dirs``.

    The symbol name is not part of the path, so it is left alone. It is
    searched for inside the file's text.
    """
    if not lib_id or ":" not in lib_id:
        return None
    lib, name = lib_id.split(":", 1)
    if not is_plain_name(lib):
        return None
    for d in search_dirs:
        f = os.path.join(d, lib + ".kicad_sym")
        if not os.path.isfile(f) or not is_inside(d, f):
            continue
        try:
            txt = open(f, "r", encoding="utf-8").read()
        except OSError:
            continue
        block = _library_symbol(txt, name, set())
        if block is not None:
            return block.replace(f'(symbol "{_esc(name)}"',
                                 f'(symbol "{_esc(lib_id)}"', 1)
    return None


def _library_symbol(text: str, name: str, ancestors: set[str]) -> str | None:
    if name in ancestors:
        raise ValueError(f"cyclic KiCad symbol inheritance at {name}")
    header = f'(symbol "{_esc(name)}"'
    start = text.find(header)
    if start < 0:
        return None
    end = _block_end(text, start)
    if end < 0:
        raise ValueError(f"invalid symbol block for {name}")
    block = text[start:end]
    from ..libimport.kicad.sexpr import loads, value

    parent_name = value(loads(block), "extends")
    if not parent_name:
        return block
    parent = _library_symbol(text, parent_name, ancestors | {name})
    if parent is None:
        raise ValueError(f"{name}: missing parent symbol {parent_name}")
    combined = parent.replace(f'(symbol "{_esc(parent_name)}"', header, 1)
    combined = combined.replace(f'(symbol "{_esc(parent_name)}_',
                                f'(symbol "{_esc(name)}_')
    for match in re.finditer(r'\(property "((?:[^"\\]|\\.)*)"', block):
        prop = block[match.start():_block_end(block, match.start())]
        old = combined.find(match.group())
        if old < 0:
            combined = combined.rstrip()[:-1] + "\n\t\t" + prop + "\n\t)"
        else:
            combined = combined[:old] + prop + combined[_block_end(combined, old):]
    return combined


def _esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _num(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


_PIN_TYPES = {
    "input", "output", "bidirectional", "tri_state", "passive", "free",
    "unspecified", "power_in", "power_out", "open_collector", "open_emitter",
    "no_connect",
}


def _pin_sexpr(p: dict[str, Any]) -> str:
    ptype = str(p.get("type", "passive")).lower()
    if ptype not in _PIN_TYPES:
        ptype = "passive"
    x, y = _num(p.get("x_mm", 0)), _num(p.get("y_mm", 0))
    angle = _num(p.get("angle", 0))
    length = _num(p.get("length", p.get("length_mm", 2.54)))
    name = _esc(p.get("name", "~"))
    number = _esc(p.get("number", ""))
    return (
        f'\t\t\t(pin {ptype} line\n'
        f'\t\t\t\t(at {x} {y} {angle})\n\t\t\t\t(length {length})\n'
        f'\t\t\t\t(name "{name}" (effects (font (size 1.27 1.27))))\n'
        f'\t\t\t\t(number "{number}" (effects (font (size 1.27 1.27))))\n'
        f'\t\t\t)')


# Pin angles point from the external connection towards the symbol (Y up).
PIN_DIRECTION = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def rectangular_pin_geometry(
        pins: list[dict[str, Any]], body_width: float, body_height: float,
) -> tuple[float, float, list[dict[str, Any]]]:
    """Keep electrical anchors fixed and terminate each pin at the body edge.

    The body dimensions are preferred values. Adjust them when needed to
    enclose the pin rows or leave room for the requested minimum pin length.
    """
    half = [body_width / 2, body_height / 2]
    minimum = [1.27, 1.27]
    maximum = [float("inf"), float("inf")]
    prepared = []
    for pin in pins:
        angle = float(pin.get("angle", 0)) % 360
        if angle not in PIN_DIRECTION:
            raise ValueError(f"pin {pin.get('number')}: expected a cardinal angle")
        dx, dy = PIN_DIRECTION[angle]
        axis = 0 if dx else 1
        x, y = float(pin.get("x_mm", 0)), float(pin.get("y_mm", 0))
        reach = -(x * dx + y * dy)
        stem = float(pin.get("length", pin.get("length_mm", 1.27)))
        if stem <= 0:
            raise ValueError(f"pin {pin.get('number')}: expected positive length")
        maximum[axis] = min(maximum[axis], reach - stem)
        minimum[1 - axis] = max(minimum[1 - axis], abs((x, y)[1 - axis]) + 1.27)
        prepared.append((pin, axis, reach))
    half = [min(max(target, low), high)
            for target, low, high in zip(half, minimum, maximum)]
    if any(edge + 1e-6 < low for edge, low in zip(half, minimum)):
        raise ValueError("pin anchors and lengths leave no room for the rectangular body")
    fitted = [{**pin, "length": round(reach - half[axis], 6)}
              for pin, axis, reach in prepared]
    return round(half[0] * 2, 6), round(half[1] * 2, 6), fitted


def build_symbol(name: str, pins: list[dict[str, Any]],
                 reference: str = "U", body_w_mm: float = 10.16,
                 body_h_mm: float = 7.62) -> str:
    """The ``(symbol ...)`` block for one symbol with a body rectangle+pins."""
    body_w_mm, body_h_mm, pins = rectangular_pin_geometry(pins, body_w_mm, body_h_mm)
    hw, hh = _num(body_w_mm / 2), _num(body_h_mm / 2)
    nhw, nhh = _num(-body_w_mm / 2), _num(-body_h_mm / 2)
    lines = [
        f'\t(symbol "{_esc(name)}"',
        '\t\t(pin_names (offset 1.016))',
        '\t\t(in_bom yes)\n\t\t(on_board yes)',
        f'\t\t(property "Reference" "{_esc(reference)}"\n'
        f'\t\t\t(at 0 {_num(body_h_mm / 2 + 1.27)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27))))',
        f'\t\t(property "Value" "{_esc(name)}"\n'
        f'\t\t\t(at 0 {_num(-body_h_mm / 2 - 1.27)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27))))',
        f'\t\t(symbol "{_esc(name.split(":", 1)[-1])}_0_1"',
        f'\t\t\t(rectangle\n\t\t\t\t(start {nhw} {hh})\n'
        f'\t\t\t\t(end {hw} {nhh})\n'
        '\t\t\t\t(stroke (width 0.254) (type default))\n'
        '\t\t\t\t(fill (type background))\n\t\t\t)',
        '\t\t)',
        f'\t\t(symbol "{_esc(name.split(":", 1)[-1])}_1_1"',
    ]
    for p in pins:
        lines.append(_pin_sexpr(p))
    lines.append('\t\t)')
    lines.append('\t)')
    return "\n".join(lines)


def build_symbol_lib(name: str, pins: list[dict[str, Any]],
                     reference: str = "U", body_w_mm: float = 10.16,
                     body_h_mm: float = 7.62) -> str:
    """A complete single-symbol ``.kicad_sym`` library."""
    return ("(kicad_symbol_lib\n\t(version 20240101)\n"
            '\t(generator "eda-agent")\n'
            + build_symbol(name, pins, reference, body_w_mm, body_h_mm)
            + "\n)\n")


def insert_symbol(existing: str, name: str, pins: list[dict[str, Any]],
                  reference: str = "U", body_w_mm: float = 10.16,
                  body_h_mm: float = 7.62) -> str:
    """Insert a symbol into an existing ``.kicad_sym`` library string, before
    its final closing paren."""
    block = build_symbol(name, pins, reference, body_w_mm, body_h_mm)
    idx = existing.rstrip().rfind(")")
    if idx == -1:
        return build_symbol_lib(name, pins, reference, body_w_mm, body_h_mm)
    return existing.rstrip()[:idx] + block + "\n)\n"
