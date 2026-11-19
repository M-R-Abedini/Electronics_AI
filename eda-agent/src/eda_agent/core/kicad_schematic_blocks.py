# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Engineer-readable schematic block synthesis for KiCad (.kicad_sch).

Replaces isolated per-part net-label soup with idiomatic schematic patterns:
1. Parallel shunt/decoupling banks (e.g., multiple capacitors sharing (rail, GND))
   wired with shared horizontal top/bottom bus rails, junctions, and single
   Power / GND symbols.
2. Point-to-point wired passive/TP pairs (degree-2 nets connecting two 2-pin
   or 1-pin parts) joined by a direct wire instead of redundant global labels.
3. Real KiCad Power / GND symbols for standard power and ground nets.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Any, Callable

from .kicad_symbol import _esc

GRID = 1.27
Point = tuple[float, float]
Bounds = tuple[float, float, float, float]

_GND_RE = re.compile(r"^(?:A|D|P|RF|CHASSIS)?GND\d*$", re.IGNORECASE)
_PWR_RE = re.compile(
    r"^(?:V(?:DD|CC|SYS|BAT|BUS|IO|REF|IN|OUT|ANA|DIG|CORE|RTC|MEM|USB|LED|PP|REG|SW)\w*"
    r"|\+?\d+V\d+\w*|VBAT\w*|VSYS\w*|VBUS\w*)$",
    re.IGNORECASE,
)


def is_ground_net(name: str) -> bool:
    return bool(_GND_RE.match(name.strip()))


def is_power_net(name: str) -> bool:
    s = name.strip()
    return is_ground_net(s) or bool(_PWR_RE.match(s))


def make_gnd_symbol(lib_id: str) -> str:
    bare = lib_id.split(":", 1)[-1]
    return (
        f'(symbol "{_esc(lib_id)}"\n'
        f'  (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))\n'
        f'  (exclude_from_sim no) (in_bom yes) (on_board yes)\n'
        f'  (property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.0 1.0))))\n'
        f'  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (symbol "{_esc(bare)}_0_1"\n'
        f'    (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))\n'
        f'      (stroke (width 0) (type default)) (fill (type none)))\n'
        f'  )\n'
        f'  (symbol "{_esc(bare)}_1_1"\n'
        f'    (pin power_in line (at 0 0 270) (length 0) (name "GND" (effects (font (size 1.0 1.0)))) (number "1" (effects (font (size 1.0 1.0)))))\n'
        f'  )\n'
        f')'
    )


def make_rail_symbol(lib_id: str, rail_name: str) -> str:
    bare = lib_id.split(":", 1)[-1]
    return (
        f'(symbol "{_esc(lib_id)}"\n'
        f'  (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))\n'
        f'  (exclude_from_sim no) (in_bom yes) (on_board yes)\n'
        f'  (property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (property "Value" "{_esc(rail_name)}" (at 0 3.81 0) (effects (font (size 1.0 1.0))))\n'
        f'  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.0 1.0)) (hide yes)))\n'
        f'  (symbol "{_esc(bare)}_0_1"\n'
        f'    (polyline (pts (xy -0.762 1.27) (xy 0 2.54)) (stroke (width 0) (type default)) (fill (type none)))\n'
        f'    (polyline (pts (xy 0 0) (xy 0 2.54)) (stroke (width 0) (type default)) (fill (type none)))\n'
        f'    (polyline (pts (xy 0 2.54) (xy 0.762 1.27)) (stroke (width 0) (type default)) (fill (type none)))\n'
        f'  )\n'
        f'  (symbol "{_esc(bare)}_1_1"\n'
        f'    (pin power_in line (at 0 0 90) (length 0) (name "{_esc(rail_name)}" (effects (font (size 1.0 1.0)))) (number "1" (effects (font (size 1.0 1.0)))))\n'
        f'  )\n'
        f')'
    )


def _at(point: Point) -> str:
    return " ".join(f"{coord:.6f}".rstrip("0").rstrip(".") for coord in point)


def _add(a: Point, b: Point) -> Point:
    return (round(a[0] + b[0], 6), round(a[1] + b[1], 6))


@dataclass
class BankMember:
    ref: str
    value: str
    lid: str
    footprint: str
    datasheet: str
    top_pin_y: float
    bot_pin_y: float
    sym_bounds: Bounds


@dataclass
class ParallelBankBlock:
    """A row of k >= 2 parallel vertical 2-pin parts sharing (top_net, bot_net)."""
    top_net: str
    bot_net: str
    members: list[BankMember]
    pitch: float
    y_top_rail: float
    y_bot_rail: float
    top_pwr_lid: str
    bot_pwr_lid: str
    bounds: Bounds

    @property
    def ref(self) -> str:
        return f"BANK_{self.top_net}_{self.members[0].ref}"


def _is_vertical_two_pin(symbol) -> tuple[bool, float, float, str, str]:
    """Return (ok, top_y, bot_y, top_pin_num, bot_pin_num) in schematic Y-down coords."""
    if len(symbol.pins) != 2:
        return False, 0.0, 0.0, "", ""
    p0, p1 = symbol.pins
    if abs(p0.at[0] - p1.at[0]) > 0.05 or abs(p0.at[0]) > 0.05:
        return False, 0.0, 0.0, "", ""
    if abs(p0.at[1] - p1.at[1]) < 1.0:
        return False, 0.0, 0.0, "", ""
    top_p, bot_p = (p0, p1) if p0.at[1] < p1.at[1] else (p1, p0)
    return True, top_p.at[1], bot_p.at[1], top_p.number, bot_p.number


def build_parallel_banks(
    parts: list[dict[str, Any]],
    assigned: dict[str, dict[str, str]],
    resolved_symbols: dict[str, tuple[str, Any]],
    power_lib_prefix: str,
) -> tuple[list[ParallelBankBlock], set[str], dict[str, str]]:
    """Group vertical 2-pin shunt components sharing (rail, ground) into wired banks.

    Returns (banks, consumed_refs, extra_lib_symbols).
    """
    groups: dict[tuple[str, str], list[tuple[dict, str, Any, float, float]]] = defaultdict(list)

    for part in parts:
        ref = part["refdes"]
        if ref.startswith("#"):
            continue
        if ref not in resolved_symbols:
            continue
        lid, sym = resolved_symbols[ref]
        ok, top_y, bot_y, top_pnum, bot_pnum = _is_vertical_two_pin(sym)
        if not ok:
            continue
        pmap = assigned.get(ref, {})
        if len(pmap) != 2:
            continue
        n_top = pmap.get(top_pnum)
        n_bot = pmap.get(bot_pnum)
        if not n_top or not n_bot or n_top == n_bot:
            continue
        # Only group when the bottom pin is a ground net and top pin is a power/signal rail
        # (and pins are already oriented top=rail, bot=GND so no rotation needed)
        if is_ground_net(n_bot) and not is_ground_net(n_top):
            groups[(n_top, n_bot)].append((part, lid, sym, top_y, bot_y))

    banks: list[ParallelBankBlock] = []
    consumed: set[str] = set()
    extra_libs: dict[str, str] = {}

    for (top_net, bot_net), entries in sorted(groups.items()):
        # Group k >= 2 parallel shunts on ANY (net, GND), or k == 1 shunt on (power_net, GND)
        if len(entries) < 2 and not is_power_net(top_net):
            continue
        members: list[BankMember] = []
        max_text_w = 0.0
        min_top_y = min(e[3] for e in entries)
        max_bot_y = max(e[4] for e in entries)
        max_sym_r = max(e[2].bounds[2] for e in entries)
        min_sym_l = min(e[2].bounds[0] for e in entries)
        for part, lid, sym, ty, by in entries:
            ref = part["refdes"]
            val = part.get("value") or part.get("lib_ref") or lid.split(":")[-1]
            members.append(BankMember(
                ref=ref,
                value=val,
                lid=lid,
                footprint=part.get("footprint") or sym.footprint,
                datasheet=part.get("datasheet") or sym.datasheet,
                top_pin_y=ty,
                bot_pin_y=by,
                sym_bounds=sym.bounds,
            ))
            consumed.add(ref)
            max_text_w = max(max_text_w, max(len(ref), len(val)) * 0.9 + 1.5)

        text_x_off = ceil((max_sym_r + 1.27) / GRID) * GRID
        pitch = ceil((text_x_off + max_text_w + abs(min_sym_l) + 2 * GRID) / GRID) * GRID
        y_top_rail = round(min_top_y - 2 * GRID, 6)
        y_bot_rail = round(max_bot_y + 2 * GRID, 6)

        safe_top = re.sub(r"[^A-Za-z0-9_+.-]", "_", top_net)
        top_lid = f"{power_lib_prefix}:{safe_top}"
        bot_lid = f"{power_lib_prefix}:GND"
        extra_libs[top_lid] = make_rail_symbol(top_lid, top_net)
        extra_libs[bot_lid] = make_gnd_symbol(bot_lid)

        rail_label_half = max(len(top_net), len(bot_net)) * 0.6 + 2.0
        left = min(min_sym_l - GRID, -rail_label_half)
        right = (len(members) - 1) * pitch + text_x_off + max_text_w + GRID
        top = y_top_rail - 5 * GRID
        bottom = y_bot_rail + 5 * GRID

        banks.append(ParallelBankBlock(
            top_net=top_net,
            bot_net=bot_net,
            members=members,
            pitch=pitch,
            y_top_rail=y_top_rail,
            y_bot_rail=y_bot_rail,
            top_pwr_lid=top_lid,
            bot_pwr_lid=bot_lid,
            bounds=(left, top, right, bottom),
        ))

    return banks, consumed, extra_libs


def emit_parallel_bank(
    bank: ParallelBankBlock,
    at: Point,
    uid: Callable[[], str],
    instance_path: str,
    project: str,
    pwr_counter: list[int],
) -> list[str]:
    """Emit a ParallelBankBlock with shared top/bottom bus rails, junctions, and single Power/GND symbols."""
    lines: list[str] = []
    n = len(bank.members)
    max_sym_r = max(m.sym_bounds[2] for m in bank.members)
    text_x_off = ceil((max_sym_r + 1.27) / GRID) * GRID

    # 1. Emit member symbols with clean non-overlapping right-side Reference/Value text
    for i, m in enumerate(bank.members):
        mx = i * bank.pitch
        pos = _add(at, (mx, 0.0))
        lines.append(f'(symbol (lib_id "{_esc(m.lid)}") (at {_at(pos)} 0) (unit 1)')
        lines.append(f'  (in_bom yes) (on_board yes) (dnp no) (uuid "{uid()}")')
        for key, val, ox, oy, hide in (
            ("Reference", m.ref, text_x_off, -GRID, ""),
            ("Value", m.value, text_x_off, GRID, ""),
            ("Footprint", m.footprint, 0.0, 0.0, " (hide yes)"),
            ("Datasheet", m.datasheet, 0.0, 0.0, " (hide yes)"),
        ):
            fpos = _at(_add(pos, (ox, oy)))
            just = " (justify left)" if not hide else ""
            lines.append(
                f'  (property "{key}" "{_esc(val)}" (at {fpos} 0) '
                f'(effects (font (size 0.9 0.9)){just}{hide}))'
            )
        lines.append(f'  (pin "1" (uuid "{uid()}"))')
        lines.append(f'  (pin "2" (uuid "{uid()}"))')
        if project:
            lines.append(
                f'  (instances (project "{_esc(project)}" '
                f'(path "{_esc(instance_path)}" (reference "{_esc(m.ref)}") (unit 1))))'
            )
        lines.append(")")
        # Vertical drops to top and bottom bus rails (only when n > 1; for n == 1, direct wire to power symbols)
        if n > 1:
            lines.append(
                f'(wire (pts (xy {_at(_add(at, (mx, m.top_pin_y)))}) '
                f'(xy {_at(_add(at, (mx, bank.y_top_rail)))})) '
                f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
            )
            lines.append(
                f'(wire (pts (xy {_at(_add(at, (mx, m.bot_pin_y)))}) '
                f'(xy {_at(_add(at, (mx, bank.y_bot_rail)))})) '
                f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
            )

    # 2. Horizontal top and bottom bus rails + interior junctions (only when n > 1)
    if n > 1:
        x_end = (n - 1) * bank.pitch
        lines.append(
            f'(wire (pts (xy {_at(_add(at, (0.0, bank.y_top_rail)))}) '
            f'(xy {_at(_add(at, (x_end, bank.y_top_rail)))})) '
            f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
        )
        lines.append(
            f'(wire (pts (xy {_at(_add(at, (0.0, bank.y_bot_rail)))}) '
            f'(xy {_at(_add(at, (x_end, bank.y_bot_rail)))})) '
            f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
        )
        for i in range(n):
            jx = i * bank.pitch
            if i == 0 or (0 < i < n - 1):
                lines.append(
                    f'(junction (at {_at(_add(at, (jx, bank.y_top_rail)))}) '
                    f'(diameter 0) (color 0 0 0 0) (uuid "{uid()}"))'
                )
                lines.append(
                    f'(junction (at {_at(_add(at, (jx, bank.y_bot_rail)))}) '
                    f'(diameter 0) (color 0 0 0 0) (uuid "{uid()}"))'
                )

    # 3. Single top Power symbol & single bottom GND symbol at x=0
    top_pwr_pt = _add(at, (0.0, bank.y_top_rail - 2 * GRID))
    bot_pwr_pt = _add(at, (0.0, bank.y_bot_rail + 2 * GRID))
    top_target_y = bank.y_top_rail if n > 1 else bank.members[0].top_pin_y
    bot_target_y = bank.y_bot_rail if n > 1 else bank.members[0].bot_pin_y
    lines.append(
        f'(wire (pts (xy {_at(top_pwr_pt)}) (xy {_at(_add(at, (0.0, top_target_y)))})) '
        f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
    )
    lines.append(
        f'(wire (pts (xy {_at(_add(at, (0.0, bot_target_y)))}) (xy {_at(bot_pwr_pt)})) '
        f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
    )

    for p_lid, p_val, p_at, val_dy in (
        (bank.top_pwr_lid, bank.top_net, top_pwr_pt, -3.81),
        (bank.bot_pwr_lid, bank.bot_net, bot_pwr_pt, 3.81),
    ):
        pwr_counter[0] += 1
        pref = f"#PWR{pwr_counter[0]:03d}"
        lines.append(f'(symbol (lib_id "{_esc(p_lid)}") (at {_at(p_at)} 0) (unit 1)')
        lines.append(f'  (in_bom yes) (on_board yes) (dnp no) (uuid "{uid()}")')
        lines.append(
            f'  (property "Reference" "{pref}" (at {_at(p_at)} 0) '
            f'(effects (font (size 1.0 1.0)) (hide yes)))'
        )
        lines.append(
            f'  (property "Value" "{_esc(p_val)}" (at {_at(_add(p_at, (0.0, val_dy)))} 0) '
            f'(effects (font (size 1.0 1.0))))'
        )
        lines.append(
            f'  (property "Footprint" "" (at {_at(p_at)} 0) (effects (font (size 1.0 1.0)) (hide yes)))'
        )
        lines.append(
            f'  (property "Datasheet" "" (at {_at(p_at)} 0) (effects (font (size 1.0 1.0)) (hide yes)))'
        )
        lines.append(f'  (pin "1" (uuid "{uid()}"))')
        if project:
            lines.append(
                f'  (instances (project "{_esc(project)}" '
                f'(path "{_esc(instance_path)}" (reference "{pref}") (unit 1))))'
            )
        lines.append(")")
    return lines
