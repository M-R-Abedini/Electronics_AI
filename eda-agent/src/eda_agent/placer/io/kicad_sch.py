# SPDX-License-Identifier: MIT
"""Pin names and electrical types from KiCad schematics (plan F3b, item 3).

A board file carries each pad's pin function only when it was updated from its
schematic inside KiCad.  A board converted from another EDA has pads with a net
and nothing else, and the intent stage then cannot tell a supply pin from a
signal pin.  The schematic next to the board has the same information per
symbol; this module reads it and nothing else (no nets, no wires).

Only ``.kicad_sch`` files are read, never written.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ir import Board


@dataclass(frozen=True)
class SchPin:
    name: str
    pin_type: str  # KiCad's electrical type: input, power_in, passive, ...


def _parse(text: str) -> list:
    """S-expression to nested lists; a quoted string becomes ``("str", value)`` so it differs from a token."""
    stack: list[list] = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            stack.append([])
            i += 1
        elif c == ")":
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif c == '"':
            j, out = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                out.append(text[j])
                j += 1
            stack[-1].append(("str", "".join(out)))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    return stack[0]


def _s(node) -> str:
    return node[1] if isinstance(node, tuple) else str(node)


def _children(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _lib_pins(symbol: list) -> dict[str, SchPin]:
    """Every pin of a library symbol, through all its units and body styles."""
    pins: dict[str, SchPin] = {}
    for pin in _children(symbol, "pin"):
        name = next((_s(c[1]) for c in _children(pin, "name")), "")
        number = next((_s(c[1]) for c in _children(pin, "number")), "")
        if number:
            pins[number] = SchPin(name, _s(pin[1]) if len(pin) > 1 else "")
    for unit in _children(symbol, "symbol"):
        pins.update(_lib_pins(unit))
    return pins


def _references(symbol: list) -> set[str]:
    """Reference designators of a placed symbol: one per sheet instance, else its Reference property."""
    refs = {_s(ref[1]) for inst in _children(symbol, "instances") for project in _children(inst, "project")
            for path in _children(project, "path") for ref in _children(path, "reference")}
    if not refs:
        refs = {_s(prop[2]) for prop in _children(symbol, "property") if _s(prop[1]) == "Reference"}
    return {r for r in refs if r and not r.endswith("?") and not r.startswith("#")}


def read_pins(sch: Path, *, positions: dict | None = None) -> dict[str, dict[str, SchPin]]:
    """``{reference: {pin number: SchPin}}`` for every symbol placed on one schematic sheet file."""
    tree = _parse(sch.read_text(encoding="utf-8", errors="replace"))
    root = next((n for n in tree if isinstance(n, list) and n and n[0] == "kicad_sch"), None)
    if root is None:
        return {}
    library: dict[str, dict[str, SchPin]] = {}
    for lib in _children(root, "lib_symbols"):
        for symbol in _children(lib, "symbol"):
            pins = _lib_pins(symbol)
            base = next((_s(c[1]) for c in _children(symbol, "extends")), None)
            library[_s(symbol[1])] = pins or library.get(base or "", {})
    out: dict[str, dict[str, SchPin]] = {}
    for symbol in _children(root, "symbol"):
        name = next((_s(c[1]) for c in _children(symbol, "lib_name")), None) or \
            next((_s(c[1]) for c in _children(symbol, "lib_id")), "")
        pins = library.get(name, {})
        for ref in _references(symbol):
            out.setdefault(ref, {}).update(pins)  # the units of one part are separate symbols with one reference
            if positions is not None:
                at = next(iter(_children(symbol, "at")), [])
                if len(at) >= 3:
                    positions.setdefault(ref, set()).add((str(sch), float(_s(at[1])), float(_s(at[2]))))
    return out


def project_pins(board_path: Path) -> tuple[dict[str, dict[str, SchPin]], list[str]]:
    """Pins from every schematic sheet in the board's folder; the files read are listed for the report."""
    pins: dict[str, dict[str, SchPin]] = {}
    files = sorted(board_path.parent.glob("*.kicad_sch"))
    for sch in files:
        for ref, rows in read_pins(sch).items():
            pins.setdefault(ref, {}).update(rows)
    return pins, [f.name for f in files]


def enrich(board: Board, board_path: Path) -> dict:
    """Fill pad functions and pin types the board file lacks from the schematics beside it.

    A value the board already has is kept: the board is what gets manufactured, the schematic is
    evidence for what is missing.  A pin whose name is its own number says nothing and is skipped.
    """
    missing = [(fp, pad) for fp in board.footprints for pad in fp.pads
               if pad.net and (not pad.function or not pad.pin_type)]
    info = {"sheets": [], "pads_named": 0, "pads_typed": 0, "parts_matched": 0, "parts_without_symbol": []}
    if not missing:
        return info
    pins, info["sheets"] = project_pins(board_path)
    if not pins:
        return info
    matched: set[str] = set()
    for fp, pad in missing:
        pin = pins.get(fp.ref, {}).get(str(pad.number))
        if pin is None:
            continue
        matched.add(fp.ref)
        if not pad.function and pin.name and pin.name != "~" and pin.name != str(pad.number):
            pad.function = pin.name
            info["pads_named"] += 1
        if not pad.pin_type and pin.pin_type:
            pad.pin_type = pin.pin_type
            info["pads_typed"] += 1
    info["parts_matched"] = len(matched)
    info["parts_without_symbol"] = sorted({fp.ref for fp, _pad in missing} - matched)
    return info
