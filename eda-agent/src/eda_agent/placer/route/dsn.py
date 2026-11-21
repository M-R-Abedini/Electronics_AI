# SPDX-License-Identifier: MIT
"""Edits to the Specctra DSN that KiCad exports (architecture 7.3).

KiCad's exporter writes coordinates in micrometres with the Y axis negated
(``(unit um)``; checked against the board outline and component places).  We
only ever *add* keepouts; nothing KiCad wrote is changed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ir import Board

_UNIT = re.compile(r"^\s*\(unit (\w+)\)", re.MULTILINE)
_SCALE = {"um": 1000.0, "mil": 1000.0 / 25.4, "mm": 1.0}


@dataclass(frozen=True)
class Keepout:
    """A wire + via keepout on one copper layer (board millimetres, KiCad Y down)."""

    name: str
    layer: str
    outline: tuple[tuple[float, float], ...]


def _num(v: float) -> str:
    text = f"{v:.1f}"
    return text.removesuffix(".0")


def _quote(name: str) -> str:
    return '"' + name.replace('"', "'") + '"'


def _token(name: str) -> str:
    return name if re.fullmatch(r"[\w.+\-]+", name) else _quote(name)


def keepout_text(keepout: Keepout, scale: float) -> str:
    coords = " ".join(f"{_num(x * scale)} {_num(-y * scale)}" for x, y in keepout.outline)
    return f"    (keepout {_quote(keepout.name)} (polygon {_token(keepout.layer)} 0 {coords}))\n"


def add_keepouts(dsn: str, keepouts: list[Keepout]) -> str:
    """Insert keepouts into the ``(structure ...)`` section, before its via list."""
    if not keepouts:
        return dsn
    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        raise ValueError("DSN has no supported (unit ...) declaration")
    scale = _SCALE[unit.group(1)]
    start = dsn.find("(structure")
    via = re.compile(r"^\s*\(via ", re.MULTILINE).search(dsn, start)
    if start < 0 or via is None:
        raise ValueError("DSN structure section not recognised")
    block = "".join(keepout_text(k, scale) for k in keepouts)
    return dsn[:via.start()] + block + dsn[via.start():]


def layers(dsn: str) -> list[str]:
    start = dsn.find("(structure")
    end = dsn.find("(boundary", start)
    return re.findall(r"\(layer ([^\s()]+)", dsn[start:end])


def set_layer_types(dsn: str, types: dict[str, str]) -> str:
    """Change ``(layer NAME (type ...))`` of the given layers in the structure section."""
    for name, kind in types.items():
        token = re.escape(_token(name))
        pattern = re.compile(r"(\(layer " + token + r"\s*\(type )(\w+)(\))")
        dsn, count = pattern.subn(lambda m, kind=kind: m.group(1) + kind + m.group(3), dsn, count=1)
        if count != 1:
            raise ValueError(f"DSN layer {name!r} not found")
    return dsn


def add_wire_keepouts(dsn: str, keepouts: list[Keepout]) -> str:
    """Like ``add_keepouts`` but blocks wires only; vias may still enter (used over macro zones)."""
    if not keepouts:
        return dsn
    text = add_keepouts(dsn, keepouts)
    for k in keepouts:
        text = text.replace(f"(keepout {_quote(k.name)} ", f"(wire_keepout {_quote(k.name)} ", 1)
    return text


_WIRE_FIX = re.compile(
    r"(\(wire\s+\(path\s+(?:\"[^\"]+\"|[^\s()]+)\s+[^\s()]+\s+)([\s\S]*?)(\)\s*\(net\s+(\"[^\"]+\"|[^\s()]+)\)\s*\(type\s+fix\)\))"
)
_VIA_FIX = re.compile(
    r"(\(via\s+(?:\"[^\"]+\"|[^\s()]+)\s+)([^\s()]+)\s+([^\s()]+)(\s*\(net\s+(\"[^\"]+\"|[^\s()]+)\)\s*\(type\s+fix\)\))"
)


def _round_tenth(v: float) -> float:
    """Round to 0.1 unit using floor(x + 0.5) to match Java Math.round in Freerouting."""
    return math.floor(v * 10.0 + 0.5) / 10.0


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token[0] == '"' and token[-1] == '"' else token


def restore_wiring_precision(dsn: str, board: Board) -> str:
    """Restore 0.1 um precision on fixed wiring coordinates truncated by KiCad's %.6g DSN formatter.

    KiCad 10 formats ``(wiring ...)`` path/via coordinates in micrometres with ``%.6g``, dropping
    sub-micron digits whenever ``|coord_um| >= 100000`` (>= 100 mm). Freerouting computes pad
    centres from ``(place ...)`` + ``(pin ...)`` at 0.1 um resolution, so a 0.1-0.5 um truncation
    disconnects fixed tracks from rotated/fractional pads. Snaps fixed wire vertices and vias
    within 0.75 um back to exact same-net pad centres first, then exact locked copper vertices.
    """
    idx = dsn.find("(wiring")
    if idx < 0:
        return dsn
    tracks = [t for t in board.routing.get("tracks", ()) if t.get("locked")]
    vias = [v for v in board.routing.get("vias", ()) if v.get("locked")]
    if not tracks and not vias:
        return dsn
    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        return dsn
    scale = _SCALE[unit.group(1)]
    tol = 0.75 * (scale / 1000.0)

    pad_pts: dict[str, list[tuple[float, float]]] = {}
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net:
                px, py = fp.pad_world(pad)
                pad_pts.setdefault(pad.net, []).append((_round_tenth(px * scale), _round_tenth(-py * scale)))

    copper_pts: dict[str, list[tuple[float, float]]] = {}
    for t in tracks:
        net = t.get("net")
        if net:
            pts = copper_pts.setdefault(net, [])
            for x, y in t.get("points", ()):
                pts.append((_round_tenth(x * scale), _round_tenth(-y * scale)))
    via_pts: dict[str, list[tuple[float, float]]] = {}
    for v in vias:
        net = v.get("net")
        if net:
            pt = (_round_tenth(v["x"] * scale), _round_tenth(-v["y"] * scale))
            via_pts.setdefault(net, []).append(pt)
            copper_pts.setdefault(net, []).append(pt)

    def snap(net: str, x: float, y: float, *, via_only: bool = False) -> tuple[float, float]:
        pools = (via_pts.get(net, ()),) if via_only else (pad_pts.get(net, ()), copper_pts.get(net, ()))
        for pool in pools:
            if not pool:
                continue
            bx, by = min(pool, key=lambda q: math.hypot(q[0] - x, q[1] - y))
            if math.hypot(bx - x, by - y) <= tol:
                return bx, by
        return x, y

    def fix_wire(m: re.Match[str]) -> str:
        prefix, coords_str, suffix, net_tok = m.group(1), m.group(2), m.group(3), m.group(4)
        net = _unquote(net_tok)
        nums = [float(z) for z in coords_str.split()]
        if len(nums) < 4 or len(nums) % 2 != 0:
            return m.group(0)
        out = []
        for px, py in zip(nums[0::2], nums[1::2], strict=True):
            sx, sy = snap(net, px, py)
            out.append(f"{_num(sx)} {_num(sy)}")
        return prefix + "  ".join(out) + suffix

    def fix_via(m: re.Match[str]) -> str:
        prefix, vx, vy, suffix, net_tok = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4), m.group(5)
        sx, sy = snap(_unquote(net_tok), vx, vy, via_only=True)
        return f"{prefix}{_num(sx)} {_num(sy)}{suffix}"

    wiring = dsn[idx:]
    wiring = _WIRE_FIX.sub(fix_wire, wiring)
    wiring = _VIA_FIX.sub(fix_via, wiring)
    return dsn[:idx] + wiring


_STRUCT_VIA = re.compile(r"(\(via\s+)([^)]+)(\))")
_VIA_SPAN_TOKEN = re.compile(r'("Via\[(\d+)-(\d+)\][^"]*"|Via\[(\d+)-(\d+)\][^\s()]+)')


def _sexp_end(text: str, start: int) -> int:
    """Return index right after the closing paren of the s-expression starting at ``start``."""
    depth = 0
    in_quote = False
    pos = start
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return pos + 1
        pos += 1
    return -1


def restrict_structure_vias(dsn: str, *, layer_count: int, allow_blind_micro: bool = False) -> tuple[str, list[str]]:
    """Keep micro/blind via padstacks in ``(library ...)`` for locked ``(type fix)`` S6a wiring,
    but remove non-through padstacks from ``(structure (via ...))`` unless ``allow_blind_micro=True``.

    Prevents Freerouting from placing costly blind/microvias outside deterministic S6a BGA escapes
    while preserving 100% valid Specctra DSN references for S6a locked microvias.
    """
    if allow_blind_micro or layer_count < 2:
        return dsn, []
    struct_start = dsn.find("(structure")
    if struct_start < 0:
        return dsn, []
    struct_end = _sexp_end(dsn, struct_start)
    if struct_end < 0:
        return dsn, []
    header = dsn[struct_start:struct_end]
    m = _STRUCT_VIA.search(header)
    if m is None:
        return dsn, []
    tokens = re.findall(r'"[^"]+"|[^\s()]+', m.group(2))
    through_tokens: list[str] = []
    removed: list[str] = []
    last_idx = layer_count - 1
    for tok in tokens:
        sm = _VIA_SPAN_TOKEN.fullmatch(tok)
        if sm:
            lo = int(sm.group(2) if sm.group(2) is not None else sm.group(4))
            hi = int(sm.group(3) if sm.group(3) is not None else sm.group(5))
            if min(lo, hi) == 0 and max(lo, hi) == last_idx:
                through_tokens.append(tok)
            else:
                removed.append(_unquote(tok))
        else:
            through_tokens.append(tok)
    if not removed or not through_tokens:
        return dsn, []
    new_line = m.group(1) + " ".join(through_tokens) + m.group(3)
    new_header = header[:m.start()] + new_line + header[m.end():]
    return dsn[:struct_start] + new_header + dsn[struct_end:], removed


def add_class_via_rule(dsn: str, class_name: str, via_names: list[str]) -> str:
    """Attach an explicit ``(circuit (use_via ...))`` list to one DSN net class (opt-in HDI routing)."""
    if not via_names:
        return dsn
    net_start = dsn.find("(network")
    wiring_start = dsn.find("(wiring", net_start) if net_start >= 0 else -1
    if net_start < 0:
        return dsn
    end = wiring_start if wiring_start > net_start else len(dsn)
    net_block = dsn[net_start:end]
    pat = re.compile(r"\(class\s+" + re.escape(_token(class_name)) + r"(?=[\s)])")
    cm = pat.search(net_block)
    if cm is None:
        return dsn
    # Find matching closing paren for this (class ...) s-expression
    depth = 0
    pos = cm.start()
    close_idx = -1
    in_quote = False
    while pos < len(net_block):
        ch = net_block[pos]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_idx = pos
                    break
        pos += 1
    if close_idx < 0:
        return dsn
    cls_body = net_block[cm.start():close_idx]
    if "(use_via" in cls_body:
        return dsn
    via_clause = " ".join(_quote(v) for v in via_names)
    insert = f"\n      (circuit (use_via {via_clause}))\n    "
    updated = net_block[:close_idx] + insert + net_block[close_idx:]
    return dsn[:net_start] + updated + dsn[end:]
