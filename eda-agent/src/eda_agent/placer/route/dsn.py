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


def enforce_via_hole_clearances(
    dsn: str,
    *,
    hole_clearance_mm: float,
    hole_to_hole_mm: float,
    via_diameter_mm: float,
    via_drill_mm: float,
    copper_clearance_mm: float,
) -> tuple[str, dict[str, float]]:
    """Inject Specctra typed via clearances into ``(structure (rule ...))`` when KiCad's
    ``m_HoleClearance`` or ``m_HoleToHoleMin`` is stricter than netclass copper clearance.

    KiCad exports only copper-to-copper ``(clearance C)`` in Specctra DSN, so when a via has
    a narrow annular ring (e.g., 0.20 mm pad / 0.10 mm drill => 0.05 mm ring) and the board
    enforces ``hole_clearance = 0.25 mm``, Freerouting routes tracks 0.09 mm from the via pad
    (0.14 mm from drill edge) and triggers mass ``hole_clearance`` DRC violations. Adding
    standard Specctra ``(clearance <val> (type via_*))`` rules prevents those violations.
    """
    if via_diameter_mm <= 0.0 or via_drill_mm <= 0.0:
        return dsn, {}
    annular = max(0.0, 0.5 * (via_diameter_mm - via_drill_mm))
    guard_mm = 0.005
    req_via_cu = (hole_clearance_mm - annular + guard_mm) if hole_clearance_mm > 0.0 else 0.0
    req_via_via = max(
        req_via_cu,
        (hole_to_hole_mm - 2.0 * annular + guard_mm) if hole_to_hole_mm > 0.0 else 0.0,
    )
    need_cu = req_via_cu > copper_clearance_mm + 1e-4
    need_vv = req_via_via > copper_clearance_mm + 1e-4
    if not need_cu and not need_vv:
        return dsn, {}

    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        return dsn, {}
    scale = _SCALE[unit.group(1)]

    struct_start = dsn.find("(structure")
    if struct_start < 0:
        return dsn, {}
    struct_end = _sexp_end(dsn, struct_start)
    if struct_end < 0:
        return dsn, {}
    header = dsn[struct_start:struct_end]
    rule_start = header.find("(rule")
    if rule_start < 0:
        return dsn, {}
    rule_end = _sexp_end(header, rule_start)
    if rule_end < 0:
        return dsn, {}

    rule_body = header[rule_start:rule_end - 1]
    clauses: list[str] = []
    applied: dict[str, float] = {}
    if need_cu:
        val_str = _num(req_via_cu * scale)
        for kind in ("via_wire", "via_smd", "via_pin", "via_area"):
            if f"(type {kind})" not in rule_body:
                clauses.append(f"\n      (clearance {val_str} (type {kind}))")
                applied[kind + "_mm"] = round(req_via_cu, 4)
    if need_vv:
        vv_val = _num(req_via_via * scale)
        if "(type via_via)" not in rule_body:
            clauses.append(f"\n      (clearance {vv_val} (type via_via))")
            applied["via_via_mm"] = round(req_via_via, 4)
    if not clauses:
        return dsn, {}

    new_rule = rule_body.rstrip() + "".join(clauses) + "\n    )"
    new_header = header[:rule_start] + new_rule + header[rule_end:]
    return dsn[:struct_start] + new_header + dsn[struct_end:], applied


_FIXED_SPAN_VIA = re.compile(
    r'^\s*\(via\s+"Via\[(\d+)-(\d+)\]_([^"]+)"\s+(\S+)\s+([^\s()]+)\s*\(net\s+([^)]+)\)\s*\(type\s+fix\)\s*\)\s*$',
    re.MULTILINE,
)


def merge_stacked_fixed_vias(dsn: str, ordered_layer_names: list[str]) -> tuple[str, int]:
    """Merge same-net, same-coordinate stacked fixed microvias (e.g. Via[0-1] + Via[1-2]) into
    one combined DSN span (Via[0-2]) so Freerouting does not report FixedVia-to-FixedVia
    self-collisions or enter PolylineTrace.normalize recursion loops.

    KiCad's original stacked microvia objects are restored bit-for-bit by ``finish`` after SES import.
    """
    w_idx = dsn.find("(wiring")
    if w_idx < 0 or len(ordered_layer_names) < 3:
        return dsn, 0
    wiring = dsn[w_idx:]
    groups: dict[tuple[str, str, str, str], list[re.Match[str]]] = {}
    for m in _FIXED_SPAN_VIA.finditer(wiring):
        key = (m.group(3), m.group(4), m.group(5), m.group(6))
        groups.setdefault(key, []).append(m)

    replacements: dict[int, tuple[int, str]] = {}
    needed_padstacks: dict[str, tuple[int, int, str]] = {}
    merged = 0
    for (spec, x_s, y_s, net_s), matches in groups.items():
        if len(matches) < 2:
            continue
        lo = min(int(m.group(1)) for m in matches)
        hi = max(int(m.group(2)) for m in matches)
        if hi <= lo or hi >= len(ordered_layer_names):
            continue
        merged += 1
        ps_name = f"Via[{lo}-{hi}]_{spec}"
        needed_padstacks[ps_name] = (lo, hi, spec)
        replacements[matches[0].start()] = (
            matches[0].end(),
            f'    (via "{ps_name}" {x_s} {y_s}(net {net_s})(type fix))',
        )
        for m in matches[1:]:
            replacements[m.start()] = (m.end(), "")

    if not merged:
        return dsn, 0

    out_w: list[str] = []
    last = 0
    for start in sorted(replacements):
        end, rep = replacements[start]
        out_w.append(wiring[last:start])
        out_w.append(rep)
        last = end
    out_w.append(wiring[last:])
    new_wiring = "".join(out_w)
    head = dsn[:w_idx]

    # Synthesize any missing merged padstacks inside (library ...) using the diameter from spec.
    lib_start = head.find("(library")
    lib_end = _sexp_end(head, lib_start) if lib_start >= 0 else -1
    if lib_start >= 0 and lib_end > lib_start:
        add_blocks: list[str] = []
        for ps_name, (lo, hi, spec) in sorted(needed_padstacks.items()):
            if f'(padstack "{ps_name}"' in head[lib_start:lib_end]:
                continue
            dm = re.match(r"([\d.]+):", spec)
            if not dm:
                continue
            diam_tok = dm.group(1)
            shapes = "\n".join(
                f"      (shape (circle {_token(ordered_layer_names[idx])} {diam_tok}))"
                for idx in range(lo, hi + 1)
            )
            add_blocks.append(f'    (padstack "{ps_name}"\n{shapes}\n      (attach off)\n    )\n')
        if add_blocks:
            head = head[:lib_end - 1] + "".join(add_blocks) + head[lib_end - 1:]

    return head + new_wiring, merged


def enable_vippo_attach(dsn: str, *, layer_count: int) -> tuple[str, int]:
    """Allow locked non-through (VIPPO micro/blind) fixed vias to sit on same-net SMD pads in Freerouting.

    Freerouting 2.5.0 ``Wiring.readViaScope`` sets ``Via.attachAllowed = viaAtSmdAllowed && padstack.attachAllowed``.
    By emitting ``(control (via_at_smd on))`` inside ``(structure ...)`` and rewriting ``(attach off)`` to
    ``(attach on)`` strictly on locked non-through microvia padstacks (while leaving through-via padstacks
    with ``(attach off)``), locked VIPPO microvias stop triggering same-net ``FixedVia <-> BGA Pad``
    violations without allowing Freerouting to drop through-vias into SMD pads.
    """
    if layer_count < 3:
        return dsn, 0
    w_idx = dsn.find("(wiring")
    if w_idx < 0:
        return dsn, 0
    last_idx = layer_count - 1
    vippo_padstacks: set[str] = set()
    for m in _FIXED_SPAN_VIA.finditer(dsn[w_idx:]):
        lo = int(m.group(1))
        hi = int(m.group(2))
        if min(lo, hi) == 0 and max(lo, hi) == last_idx:
            continue
        vippo_padstacks.add(f"Via[{lo}-{hi}]_{m.group(3)}")
    if not vippo_padstacks:
        return dsn, 0

    head = dsn[:w_idx]
    wiring = dsn[w_idx:]

    # 1. Rewrite (attach off) -> (attach on) strictly for locked non-through VIPPO padstacks in (library ...).
    lib_start = head.find("(library")
    lib_end = _sexp_end(head, lib_start) if lib_start >= 0 else -1
    attached = 0
    if lib_start >= 0 and lib_end > lib_start:
        lib = head[lib_start:lib_end]
        for ps_name in sorted(vippo_padstacks):
            marker = f'(padstack "{ps_name}"'
            ps_start = lib.find(marker)
            if ps_start < 0:
                continue
            ps_end = _sexp_end(lib, ps_start)
            if ps_end < 0:
                continue
            ps_block = lib[ps_start:ps_end]
            if "(attach off)" in ps_block:
                lib = lib[:ps_start] + ps_block.replace("(attach off)", "(attach on)", 1) + lib[ps_end:]
                attached += 1
            elif "(attach on)" in ps_block:
                attached += 1
        head = head[:lib_start] + lib + head[lib_end:]

    if attached == 0:
        return dsn, 0

    # 2. Ensure (control (via_at_smd on)) is present inside (structure ...).
    struct_start = head.find("(structure")
    struct_end = _sexp_end(head, struct_start) if struct_start >= 0 else -1
    if struct_start >= 0 and struct_end > struct_start:
        header = head[struct_start:struct_end]
        if "(via_at_smd on)" not in header:
            ctrl_start = header.find("(control")
            if ctrl_start >= 0:
                ctrl_end = _sexp_end(header, ctrl_start)
                if ctrl_end > ctrl_start:
                    ctrl_body = header[ctrl_start:ctrl_end]
                    if "(via_at_smd off)" in ctrl_body:
                        new_ctrl = ctrl_body.replace("(via_at_smd off)", "(via_at_smd on)", 1)
                    else:
                        new_ctrl = ctrl_body[:-1].rstrip() + " (via_at_smd on))"
                    header = header[:ctrl_start] + new_ctrl + header[ctrl_end:]
            else:
                header = header[:-1].rstrip() + "\n    (control (via_at_smd on))\n  )"
            head = head[:struct_start] + header + head[struct_end:]

    return head + wiring, attached


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


def restrict_fast_net_layers(dsn: str, board: Board, intent: dict | None) -> tuple[str, list[str]]:
    """Keep high-speed / diff-pair nets strictly on signal layers adjacent to a GND plane.

    Active only on >= 6-layer boards when >= 2 signal layers sit adjacent to a GND plane AND
    >= 1 signal layer lacks direct GND adjacency. Only restricts nets whose ALL SMD pads and
    locked S6a routing already lie strictly inside ``gnd_adj_signals`` (so no net can ever be
    locked off its own pad layer).
    """
    ordered = [cl for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    n_layers = len(ordered)
    if n_layers < 6:
        return dsn, []

    from ..ir import is_ground_net, plane_zone_layers

    by_canon = {cl.name: cl.canonical for cl in ordered}
    canon_order = [cl.canonical for cl in ordered]
    planes = plane_zone_layers(board)
    gnd_plane_layers: set[str] = set()
    power_plane_layers: set[str] = set()
    for z in board.zones:
        net = z.get("net") or ""
        if z.get("uuid") not in planes and not z.get("filled") and z.get("fill_fraction", 0.0) <= 0.3:
            continue
        lyrs = {by_canon.get(n, n) for n in z.get("layers", ())}
        if is_ground_net(net):
            gnd_plane_layers |= lyrs
        elif net:
            power_plane_layers |= lyrs
    for role in board.stackup.roles:
        if role.role in ("plane", "mixed") and role.net:
            c = by_canon.get(role.layer, role.layer)
            if is_ground_net(role.net):
                gnd_plane_layers.add(c)
            else:
                power_plane_layers.add(c)
    if not gnd_plane_layers:
        return dsn, []

    gnd_adj_signals: list[str] = []
    non_gnd_signals: list[str] = []
    for idx, cl in enumerate(ordered):
        if cl.canonical in gnd_plane_layers or cl.canonical in power_plane_layers:
            continue
        adj = {canon_order[j] for j in (idx - 1, idx + 1) if 0 <= j < n_layers}
        if adj & gnd_plane_layers:
            gnd_adj_signals.append(cl.canonical)
        else:
            non_gnd_signals.append(cl.canonical)

    if len(gnd_adj_signals) < 2 or not non_gnd_signals:
        return dsn, []

    fast_nets: set[str] = set()
    for dp in board.diff_pairs:
        fast_nets.add(dp.positive)
        fast_nets.add(dp.negative)
    if intent:
        for dp in intent.get("diff_pairs") or ():
            val = dp.get("value") if isinstance(dp, dict) and "value" in dp else dp
            if isinstance(val, dict):
                for k in ("positive", "negative", "pos", "neg"):
                    if val.get(k):
                        fast_nets.add(str(val[k]))
        for nname, meta in (intent.get("nets") or {}).items():
            cls_val = str(((meta.get("class") or {}).get("value")) or "").upper()
            if cls_val in ("HIGH_SPEED", "DIFF_PAIR", "CLOCK"):
                fast_nets.add(nname)
    if not fast_nets:
        return dsn, []

    name_by_canon = {cl.canonical: cl.name for cl in ordered}
    # Exact parity: every SMD pad and locked non-plane track/via must lie inside use_layer!
    allowed_set = set(gnd_adj_signals)
    end_layers = {canon_order[0], canon_order[-1]}
    non_signal_layers = gnd_plane_layers | power_plane_layers
    eligible: list[str] = []
    for nname in sorted(fast_nets):
        ok = True
        for fp in board.footprints:
            for pad in fp.pads:
                if pad.net != nname or pad.kind != "smd":
                    continue
                plyrs = {by_canon.get(lyr, lyr) for lyr in pad.layers}
                if not (plyrs <= allowed_set):
                    ok = False
                    break
            if not ok:
                break
        if ok and board.routing:
            for trk in board.routing.get("tracks", ()):
                if trk.get("locked") and trk.get("net") == nname:
                    tlyr = by_canon.get(trk.get("layer", ""), trk.get("layer", ""))
                    if tlyr not in allowed_set:
                        ok = False
                        break
        if ok and board.routing:
            for via in board.routing.get("vias", ()):
                if via.get("locked") and via.get("net") == nname:
                    v_canons = {by_canon.get(lyr, lyr) for lyr in via.get("layers", ())}
                    if v_canons != end_layers and any(
                        slyr not in allowed_set for slyr in (v_canons - non_signal_layers)
                    ):
                        ok = False
                        break
        if ok:
            eligible.append(nname)
    if not eligible:
        return dsn, []

    net_start = dsn.find("(network")
    wiring_start = dsn.find("(wiring", net_start) if net_start >= 0 else -1
    if net_start < 0:
        return dsn, []
    net_end = wiring_start if wiring_start > net_start else len(dsn)
    net_block = dsn[net_start:net_end]
    use_layers = " ".join(_token(name_by_canon.get(c, c)) for c in gnd_adj_signals)
    applied_nets: list[str] = []
    for nname in eligible:
        pat = re.compile(r"\(net\s+" + re.escape(_token(nname)) + r"(?=[\s)])")
        nm = pat.search(net_block)
        if nm is None:
            pat = re.compile(r"\(net\s+" + re.escape(_quote(nname)) + r"(?=[\s)])")
            nm = pat.search(net_block)
        if nm is None:
            continue
        n_end = _sexp_end(net_block, nm.start())
        if n_end < 0:
            continue
        n_body = net_block[nm.start():n_end]
        if "(circuit" in n_body:
            continue
        updated_body = n_body[:-1].rstrip() + f"\n      (circuit (use_layer {use_layers}))\n    )"
        net_block = net_block[:nm.start()] + updated_body + net_block[n_end:]
        applied_nets.append(nname)
    if not applied_nets:
        return dsn, []
    return dsn[:net_start] + net_block + dsn[net_end:], applied_nets
