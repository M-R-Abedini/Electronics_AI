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


def _instance_references(symbol: list) -> list[tuple[str, str]]:
    """Return ``(instance_uuid_path, reference)`` pairs for a placed symbol."""
    pairs: list[tuple[str, str]] = []
    for inst in _children(symbol, "instances"):
        for project in _children(inst, "project"):
            for path in _children(project, "path"):
                p_str = _s(path[1]) if len(path) > 1 else ""
                for ref in _children(path, "reference"):
                    r = _s(ref[1])
                    if r and not r.endswith("?") and not r.startswith("#"):
                        pairs.append((p_str, r))
    if not pairs:
        for prop in _children(symbol, "property"):
            if len(prop) > 2 and _s(prop[1]) == "Reference":
                r = _s(prop[2])
                if r and not r.endswith("?") and not r.startswith("#"):
                    pairs.append(("", r))
    return pairs


def _references(symbol: list) -> set[str]:
    """Reference designators of a placed symbol: one per sheet instance, else its Reference property."""
    return {r for _, r in _instance_references(symbol)}


def _sheet_uuid(root: list) -> str:
    return next((_s(c[1]) for c in _children(root, "uuid") if len(c) > 1), "")


def _sheet_children(root: list) -> list[tuple[str, str, str]]:
    """Return ``(uuid, sheet_name, sheet_file)`` for every child ``(sheet ...)`` block."""
    out: list[tuple[str, str, str]] = []
    for sh in _children(root, "sheet"):
        uid = next((_s(c[1]) for c in _children(sh, "uuid") if len(c) > 1), "")
        sname, sfile = "", ""
        for prop in _children(sh, "property"):
            if len(prop) > 2:
                key = _s(prop[1])
                if key in ("Sheetname", "Sheet name"):
                    sname = _s(prop[2])
                elif key in ("Sheetfile", "Sheet file"):
                    sfile = _s(prop[2])
        if sfile:
            out.append((uid, sname or Path(sfile).stem, sfile))
    return out


def _walk_sheet_tree(root_sch: Path) -> list[tuple[Path, str, str]]:
    """Walk KiCad hierarchical sheets from *root_sch*.

    Returns ``(sch_path, uuid_path, human_sheet_path)`` for every sheet instance so
    reusable subsheets (e.g. 3x ``usb_phy.kicad_sch``) remain distinct instances.
    """
    visited_stack: list[tuple[Path, str, str]] = []
    out: list[tuple[Path, str, str]] = []

    def visit(sch_path: Path, parent_uuid_path: str, human_path: str, depth: int) -> None:
        if depth > 16 or not sch_path.is_file():
            return
        tree = _parse(sch_path.read_text(encoding="utf-8", errors="replace"))
        root = next((n for n in tree if isinstance(n, list) and n and n[0] == "kicad_sch"), None)
        if root is None:
            return
        cur_uuid_path = parent_uuid_path or ("/" + _sheet_uuid(root) if _sheet_uuid(root) else "/")
        out.append((sch_path, cur_uuid_path, human_path))
        for uid, sname, sfile in _sheet_children(root):
            child_path = (sch_path.parent / sfile).resolve()
            if not child_path.is_file() or any(p == child_path for p, _, _ in visited_stack):
                continue
            next_uuid = (cur_uuid_path.rstrip("/") + "/" + uid) if uid else cur_uuid_path
            next_human = human_path.rstrip("/") + "/" + sname + "/"
            visited_stack.append((child_path, next_uuid, next_human))
            visit(child_path, next_uuid, next_human, depth + 1)
            visited_stack.pop()

    visit(root_sch.resolve(), "", "/", 0)
    return out


def read_pins(sch: Path, *, positions: dict | None = None,
              instance_map: dict[str, str] | None = None) -> dict[str, dict[str, SchPin]]:
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
    default_label = instance_map.get("", str(sch)) if instance_map else str(sch)
    for symbol in _children(root, "symbol"):
        name = next((_s(c[1]) for c in _children(symbol, "lib_name")), None) or \
            next((_s(c[1]) for c in _children(symbol, "lib_id")), "")
        pins = library.get(name, {})
        at = next(iter(_children(symbol, "at")), [])
        xy = (float(_s(at[1])), float(_s(at[2]))) if len(at) >= 3 else None
        for uuid_path, ref in _instance_references(symbol):
            out.setdefault(ref, {}).update(pins)  # the units of one part are separate symbols with one reference
            if positions is not None and xy is not None:
                sheet_label = (
                    instance_map.get(uuid_path)
                    or instance_map.get(uuid_path.rstrip("/"))
                    or default_label
                ) if instance_map else str(sch)
                positions.setdefault(ref, set()).add((sheet_label, xy[0], xy[1]))
    return out


def project_pins(
    board_path: Path, *, positions: dict[str, set[tuple[str, float, float]]] | None = None
) -> tuple[dict[str, dict[str, SchPin]], list[str]]:
    """Pins from the project's hierarchical schematic tree (falling back to folder glob)."""
    pins: dict[str, dict[str, SchPin]] = {}
    root_sch = board_path.with_suffix(".kicad_sch")
    instances = _walk_sheet_tree(root_sch) if root_sch.is_file() else []
    if instances:
        by_file: dict[Path, dict[str, str]] = {}
        sheet_names: list[str] = []
        for sch_path, uuid_path, human_path in instances:
            label = human_path.strip("/") or sch_path.stem
            by_file.setdefault(sch_path, {})[uuid_path] = label
            by_file[sch_path].setdefault("", label)
            if sch_path.name not in sheet_names:
                sheet_names.append(sch_path.name)
        for sch_path, inst_map in by_file.items():
            for ref, rows in read_pins(sch_path, positions=positions, instance_map=inst_map).items():
                pins.setdefault(ref, {}).update(rows)
        return pins, sorted(sheet_names)
    files = sorted(board_path.parent.glob("*.kicad_sch"))
    for sch in files:
        for ref, rows in read_pins(sch, positions=positions).items():
            pins.setdefault(ref, {}).update(rows)
    return pins, [f.name for f in files]


def enrich(board: Board, board_path: Path) -> dict:
    """Fill pad functions, pin types, hierarchical sheet paths, unit sheets, and schematic positions
    that the board file lacks from the schematics beside it.

    A value the board already has is kept: the board is what gets manufactured, the schematic is
    evidence for what is missing.  A pin whose name is its own number says nothing and is skipped.
    """
    missing = [(fp, pad) for fp in board.footprints for pad in fp.pads
               if pad.net and (not pad.function or not pad.pin_type)]
    missing_sheet = [fp for fp in board.footprints if not fp.sheet or fp.schematic_position is None or not fp.unit_sheets]
    info = {"sheets": [], "pads_named": 0, "pads_typed": 0, "parts_matched": 0, "parts_without_symbol": [],
            "sheets_assigned": 0, "positions_assigned": 0, "units_mapped": 0}
    if not missing and not missing_sheet:
        return info
    positions: dict[str, set[tuple[str, float, float]]] = {}
    pins, info["sheets"] = project_pins(board_path, positions=positions)
    if not pins and not positions:
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
    for fp in missing_sheet:
        pts = positions.get(fp.ref)
        if not pts:
            continue
        by_sheet: dict[str, list[tuple[float, float]]] = {}
        for sch_label, x, y in pts:
            key = Path(sch_label).stem if sch_label.lower().endswith(".kicad_sch") else sch_label
            by_sheet.setdefault(key, []).append((x, y))
        unit_avg = {
            s: (sum(c[0] for c in cs) / len(cs), sum(c[1] for c in cs) / len(cs))
            for s, cs in sorted(by_sheet.items())
        }
        if not fp.unit_sheets and unit_avg:
            fp.unit_sheets = unit_avg
            info["units_mapped"] += 1
        sheet_name = min(by_sheet)
        coords = by_sheet[sheet_name]
        if not fp.sheet or (len(by_sheet) == 1 and "/" in sheet_name):
            fp.sheet = sheet_name
            info["sheets_assigned"] += 1
        if not fp.sheet_path and len(by_sheet) == 1:
            fp.sheet_path = "/" + sheet_name.strip("/") + "/"
        if fp.schematic_position is None:
            fp.schematic_position = (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
            )
            info["positions_assigned"] += 1
    info["parts_matched"] = len(matched)
    info["parts_without_symbol"] = sorted({fp.ref for fp, _pad in missing} - matched)
    return info
