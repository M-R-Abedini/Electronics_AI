# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Emit a wired KiCad schematic from a normalized parts-and-nets model."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from itertools import groupby
from math import ceil
from typing import Any, Callable
from uuid import UUID

from ..libimport.kicad.sexpr import find, find_all, loads, value
from .kicad_symbol import PIN_DIRECTION, _esc, build_symbol
from .placement_order import order_by_connectivity

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]
GRID = 1.27
LABEL_SIZE = 1.0
LABEL_STUB = 3 * GRID
PAPERS = (("A3", 420, 297), ("A2", 594, 420),
          ("A1", 841, 594), ("A0", 1189, 841))
# (schematic outward direction, KiCad text justification)
LABEL_ORIENTATION = {
    (1, 0): (0, "left"),
    (0, -1): (90, "left"),
    (-1, 0): (180, "right"),
    (0, 1): (270, "right"),
}


def _at(point: Point) -> str:
    return " ".join(f"{coord:.6f}".rstrip("0").rstrip(".") for coord in point)


def _add(point: Point, offset: Point) -> Point:
    return tuple(round(a + b, 6) for a, b in zip(point, offset))


def global_label(name: str, position: Point, angle: int, uuid: str,
                 size: float = LABEL_SIZE) -> str:
    """Use KiCad's paired spin and justification for a framed net label."""
    justify = {spin: align for spin, align in LABEL_ORIENTATION.values()}[angle]
    return (f'(global_label "{_esc(name)}" (shape bidirectional) '
            f'(at {_at(position)} {angle}) '
            f'(effects (font (size {size:g} {size:g})) (justify {justify})) '
            f'(uuid "{uuid}"))')


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    at: Point
    outward: Point
    electrical_type: str


@dataclass
class Symbol:
    source: str
    pins: list[Pin]
    bounds: Bounds
    footprint: str
    datasheet: str


def _bounds(points: list[Point]) -> Bounds:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _graphic_points(item: list) -> list[Point]:
    """Conservative drawing bounds in schematic coordinates (Y down)."""
    points = []
    for child in item:
        if not isinstance(child, list) or not child:
            continue
        tag = child[0]
        if tag in {"start", "mid", "end", "at", "xy"}:
            points.append((float(child[1]), -float(child[2])))
        elif tag == "center":
            radius = float(value(item, "radius", default=0))
            x, y = float(child[1]), -float(child[2])
            points.extend(((x - radius, y - radius), (x + radius, y + radius)))
        elif tag == "pts":
            points.extend(_graphic_points(child))
    return points


def _read_symbol(source: str) -> Symbol:
    tree = loads(source)
    if value(tree, "extends") is not None:
        raise ValueError(f'{tree[1]}: derived symbol must be resolved before placement')
    properties = {p[1]: p[2] for p in find_all(tree, "property")}
    points: list[Point] = [(0, 0)]
    pins = []
    for unit in find_all(tree, "symbol"):
        _, number, style = unit[1].rsplit("_", 2)
        if number not in {"0", "1"} or style not in {"0", "1"}:
            continue
        for item in unit[2:]:
            if not isinstance(item, list) or not item:
                continue
            points.extend(_graphic_points(item))
            if item[0] == "pin":
                x, y, angle = find(item, "at")[1:]
                dx, dy = PIN_DIRECTION[float(angle) % 360]
                pins.append(Pin(value(item, "number"), value(item, "name"),
                                (float(x), -float(y)), (-dx, dy), item[1]))
    return Symbol(source, pins, _bounds(points),
                  properties.get("Footprint", ""), properties.get("Datasheet", ""))


def _placeholder(numbers: tuple[str, ...]) -> Symbol:
    count = len(numbers)
    name = f"BOX{count}"
    if numbers != tuple(str(i) for i in range(1, count + 1)):
        digest = sha256(repr(numbers).encode()).hexdigest()[:12]
        name += "_" + digest
    rows = (count + 1) // 2
    half_height = max(2, ceil((rows + 1) / 2)) * 2 * GRID
    half_width = max(6, ceil(max(map(len, numbers)) * 1.2 / GRID) + 2) * GRID
    pins = []
    for index, number in enumerate(numbers):
        side, row = divmod(index, rows)
        pins.append({
            "number": number, "name": f"P{number}", "length": 2 * GRID,
            "x_mm": (-1 if side == 0 else 1) * (half_width + 2 * GRID),
            "y_mm": round(half_height - (row + 1) * 2 * GRID, 6),
            "angle": 180 * side,
        })
    return _read_symbol(build_symbol(f"eda:{name}", pins, body_w_mm=half_width * 2,
                                     body_h_mm=half_height * 2))


def _assign_nets(parts: list[dict], nets: list[dict]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {p["refdes"]: {} for p in parts}
    if len(mapped) != len(parts):
        raise ValueError("duplicate component references in schematic")
    for net in nets:
        name = net["name"]
        if not name:
            raise ValueError("unnamed schematic net")
        for node in net.get("nodes", []):
            ref = node.get("refdes") or node.get("reference", "")
            pin = str(node["pin"])
            if ref not in mapped:
                raise ValueError(f"{name}: unknown component {ref}")
            previous = mapped[ref].setdefault(pin, name)
            if previous != name:
                raise ValueError(f"{ref}.{pin} is assigned to both {previous} and {name}")
    return mapped


def _resolve_pins(ref: str, symbol: Symbol, assigned: dict[str, str]) -> dict[str, str]:
    by_number = {pin.number: pin for pin in symbol.pins}
    result = {}
    for key, net in assigned.items():
        pin = by_number.get(key)
        if pin is None:
            matches = [p for p in symbol.pins if p.name.casefold() == key.casefold()]
            if len(matches) != 1:
                raise ValueError(f"{ref}.{key}: expected a unique pin in symbol unit 1")
            pin = matches[0]
        if pin.electrical_type == "no_connect":
            raise ValueError(f"{ref}.{pin.number}: pin is marked no-connect in the library")
        previous = result.setdefault(pin.number, net)
        if previous != net:
            raise ValueError(f"{ref}.{pin.number}: conflicting pin name/number assignments")
    return result


def _label_width(name: str) -> float:
    # Reserve space for stroke text and the two ends of a bidirectional frame.
    return len(name) * LABEL_SIZE * 1.2 + 4.5


def _label_groups(ref: str, pins: list[Pin], assigned: dict[str, str]):
    """Share one label and a short wire spine for adjacent pins of one net."""
    sides = defaultdict(list)
    occupied: dict[Point, str | None] = {}
    for pin in pins:
        net = assigned.get(pin.number)
        if pin.at in occupied:
            if occupied[pin.at] != net:
                raise ValueError(f"{ref}.{pin.number}: conflicting coincident pin assignments")
            continue
        occupied[pin.at] = net
        dx, dy = pin.outward
        sides[(pin.outward, pin.at[0] if dx else pin.at[1])].append(pin)

    for (outward, _), side in sides.items():
        dx, dy = outward
        row_axis = 1 if dx else 0
        lanes: list[float] = []
        span = max((_label_width(assigned[p.number]) for p in side if p.number in assigned),
                   default=0)
        for net, adjacent in groupby(
            sorted(side, key=lambda p: p.at[row_axis]),
            key=lambda p: assigned.get(p.number),
        ):
            if net is None:
                continue
            starts = [p.at for p in adjacent]
            # Stagger tightly pitched labels while keeping each wire on its pin row.
            # Track the full group span [starts[0], starts[-1]] so multi-pin spines
            # never collide with subsequent pin labels on the same lane.
            lane = next((i for i, last in enumerate(lanes)
                         if round(starts[0][row_axis] - last, 6) >= 2 * GRID),
                        len(lanes))
            if lane == len(lanes):
                lanes.append(starts[-1][row_axis])
            else:
                lanes[lane] = starts[-1][row_axis]
            stub = LABEL_STUB + lane * ceil(span / GRID) * GRID
            ends = [_add(point, (dx * stub, dy * stub)) for point in starts]
            yield net, outward, starts, ends


@dataclass
class Placement:
    ref: str
    value: str
    lid: str
    footprint: str
    datasheet: str
    groups: list
    no_connects: list[Point]
    field_top: float
    bounds: Bounds


def _place_part(part: dict, lid: str, symbol: Symbol, assigned: dict[str, str]) -> Placement:
    ref = part["refdes"]
    label = part.get("value") or part.get("lib_ref") or lid.split(":")[-1]
    groups = list(_label_groups(ref, symbol.pins, assigned))
    points = [symbol.bounds[:2], symbol.bounds[2:]]
    for net, (dx, dy), _, ends in groups:
        x, y = ends[len(ends) // 2]
        length = _label_width(net)
        tips = _add((x, y), (dx * length, dy * length))
        points.extend(((min(x, tips[0]) - 1.25, min(y, tips[1]) - 1.25),
                       (max(x, tips[0]) + 1.25, max(y, tips[1]) + 1.25)))
        points.extend(ends)
    wired = {p.at for p in symbol.pins if p.number in assigned}
    nc = list(dict.fromkeys(p.at for p in symbol.pins
                           if p.electrical_type == "no_connect" and p.at not in wired))
    for nx, ny in nc:
        points.extend(((nx - GRID, ny - GRID), (nx + GRID, ny + GRID)))
    _, top, _, _ = _bounds(points)
    half_text_width = max(len(ref), len(label)) * 0.8 + GRID
    field_top = top - 4 * GRID
    points.extend(((-half_text_width, field_top - GRID),
                   (half_text_width, field_top + 3 * GRID)))
    return Placement(ref, label, lid, part.get("footprint") or symbol.footprint,
                     part.get("datasheet") or symbol.datasheet, groups, nc, field_top,
                     _bounds(points))


def _verify_non_overlapping(parts: list[Placement], positions: list[Point],
                            paper_w: float, paper_h: float, margin: float) -> None:
    """Ensure placed part bounding boxes do not overlap each other or sheet margins."""
    boxes = []
    for part, (px, py) in zip(parts, positions):
        l, t, r, b = part.bounds
        box = (px + l, py + t, px + r, py + b)
        if box[0] < margin - 1e-6 or box[1] < margin - 1e-6 or box[2] > paper_w - margin + 1e-6 or box[3] > paper_h - 45.72 + 1e-6:
            raise ValueError(f"{part.ref}: schematic placement intrudes on sheet margin or title block")
        for prev_ref, prev in boxes:
            if (box[0] + 1e-6 < prev[2] and box[2] - 1e-6 > prev[0] and
                    box[1] + 1e-6 < prev[3] and box[3] - 1e-6 > prev[1]):
                raise ValueError(f"{part.ref} overlaps {prev_ref} in schematic layout")
        boxes.append((part.ref, box))


def _pack(parts: list[Placement]) -> tuple[str, list[Point]]:
    margin, gap = 15.24, 6 * GRID
    for paper, width, height in PAPERS:
        positions = []
        cursor_x = row_top = row_bottom = margin
        for part in parts:
            left, top, right, bottom = part.bounds
            x = ceil((cursor_x - left) / GRID) * GRID
            if x + right > width - margin:
                cursor_x, row_top = margin, row_bottom + gap
                x = ceil((cursor_x - left) / GRID) * GRID
            y = ceil((row_top - top) / GRID) * GRID
            # Keep the whole bottom strip clear of the standard title block.
            if x + right > width - margin or y + bottom > height - 45.72:
                break
            positions.append((x, y))
            cursor_x = x + right + gap
            row_bottom = max(row_bottom, y + bottom)
        else:
            _verify_non_overlapping(parts, positions, width, height, margin)
            return paper, positions
    raise ValueError("schematic exceeds A0; split the design into smaller schematics")


def _emit(part: Placement, at: Point, uid: Callable[[], str], root: str,
          project: str) -> list[str]:
    lines = [f'(symbol (lib_id "{_esc(part.lid)}") (at {_at(at)} 0) (unit 1)',
             f'  (in_bom yes) (on_board yes) (dnp no) (uuid "{uid()}")']
    for key, val, y in (("Reference", part.ref, part.field_top),
                        ("Value", part.value, part.field_top + 2 * GRID),
                        ("Footprint", part.footprint, 0),
                        ("Datasheet", part.datasheet, 0)):
        hide = " (hide yes)" if key in {"Footprint", "Datasheet"} else ""
        field = _at(_add(at, (0, y)))
        lines.append(f'  (property "{key}" "{_esc(val)}" (at {field} 0) '
                     f'(effects (font (size 1.27 1.27)){hide}))')
    if project:
        lines.append(f'  (instances (project "{_esc(project)}" '
                     f'(path "/{root}" (reference "{_esc(part.ref)}") (unit 1))))')
    lines.append(")")
    for net, outward, starts, ends in part.groups:
        mid = len(ends) // 2
        for start, end in [*zip(starts, ends), *zip(ends, ends[1:])]:
            lines.append(f'(wire (pts (xy {_at(_add(at, start))}) '
                         f'(xy {_at(_add(at, end))})) (stroke (width 0) (type default)) '
                         f'(uuid "{uid()}"))')
        for joint in ends[1:-1]:
            lines.append(f'(junction (at {_at(_add(at, joint))}) (diameter 0) '
                         f'(color 0 0 0 0) (uuid "{uid()}"))')
        lines.append(global_label(net, _add(at, ends[mid]), LABEL_ORIENTATION[outward][0], uid()))
    for point in part.no_connects:
        lines.append(f'(no_connect (at {_at(_add(at, point))}) (uuid "{uid()}"))')
    return lines


def build_schematic(
        parts: list[dict[str, Any]], nets: list[dict[str, Any]],
        uid: Callable[[], str] | None = None, title: str = "",
        symbols: dict[str, str] | None = None,
        part_symbol: dict[str, str] | None = None, project: str = "",
) -> str:
    """Place parts, then connect the requested pins through wires and labels."""
    if uid is None:
        serial = iter(range(1, 1 << 64))

        def uid() -> str:
            return str(UUID(int=next(serial)))

    root = uid()
    assigned = _assign_nets(parts, nets)
    library = {}
    placements = []
    for part in order_by_connectivity(parts, nets):
        ref = part["refdes"]
        lid = (part_symbol or {}).get(ref)
        source = (symbols or {}).get(lid)
        if source:
            if lid not in library:
                library[lid] = _read_symbol(source)
            symbol = library[lid]
        else:
            numbers = list(assigned[ref])
            count = max(2, int(part.get("pin_count", 0) or 0))
            for number in map(str, range(1, count + 1)):
                if len(numbers) >= count:
                    break
                if number not in numbers:
                    numbers.append(number)
            numbers.sort(key=lambda n: (not n.isdecimal(), int(n) if n.isdecimal() else n, n))
            symbol = _placeholder(tuple(numbers))
            lid = loads(symbol.source)[1]
            library.setdefault(lid, symbol)
        placements.append(_place_part(part, lid, symbol, _resolve_pins(ref, symbol, assigned[ref])))

    paper, positions = _pack(placements)
    lines = ["(kicad_sch", "  (version 20240101)", '  (generator "eda-agent")',
             f'  (uuid "{root}")', f'  (paper "{paper}")']
    if title:
        lines.append(f'  (title_block (title "{_esc(title)}"))')
    lines.append("  (lib_symbols")
    lines.extend(library[lid].source for lid in sorted(library))
    lines.append("  )")
    for part, at in zip(placements, positions):
        lines.extend("  " + line for line in _emit(part, at, uid, root, project))
    lines.extend(('  (sheet_instances (path "/" (page "1")))',
                  "  (embedded_fonts no)", ")"))
    return "\n".join(lines) + "\n"
