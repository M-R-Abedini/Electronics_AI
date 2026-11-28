# SPDX-License-Identifier: MIT
"""Translate real KiCad custom DRC rules (``.kicad_dru``) into Specctra DSN and router constraints."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...units import MM_PER_MIL
from .dsn import _SCALE, _UNIT, _num, _quote, _sexp_end, _token, isolate_net_in_class

if TYPE_CHECKING:
    from ..ir import Board

_RULE_START = re.compile(r'\(rule\s+("[^"]+"|[^\s()]+)')
_COND_RE = re.compile(r'\(condition\s+"([^"]*)"\)')
_LAYER_RE = re.compile(r'\(layer\s+"?([^"\s()]+)"?\)')
_NETCLASS_COND = re.compile(
    r"^\(?(?:A\.NetClass\s*==\s*['\"]([^'\"]+)['\"]|A\.hasNetclass\(\s*['\"]([^'\"]+)['\"]\s*\))\)?$"
)
_NETCLASS_PAIR_COND = re.compile(
    r"^\(?(?:A\.NetClass\s*==\s*['\"]([^'\"]+)['\"]|A\.hasNetclass\(\s*['\"]([^'\"]+)['\"]\s*\))"
    r"\s*(?:&&|\|\|)\s*"
    r"(?:B\.NetClass\s*==\s*['\"]([^'\"]+)['\"]|B\.hasNetclass\(\s*['\"]([^'\"]+)['\"]\s*\))\)?$"
)
_A_NC_TERM = re.compile(r"A\.(?:NetClass\s*==\s*['\"]([^'\"]+)['\"]|hasNetclass\(\s*['\"]([^'\"]+)['\"]\s*\))")
_B_NC_TERM = re.compile(r"B\.(?:NetClass\s*==\s*['\"]([^'\"]+)['\"]|hasNetclass\(\s*['\"]([^'\"]+)['\"]\s*\))")
_A_NN_TERM = re.compile(r"A\.NetName\s*==\s*['\"]([^'\"]+)['\"]")
_B_NN_TERM = re.compile(r"B\.NetName\s*==\s*['\"]([^'\"]+)['\"]")
_INVALID_DRU_TOKEN = re.compile(r"\b[AB]\.Footprint\b")
_CLEARANCE_RE = re.compile(r"\(constraint\s+clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_WIDTH_RE = re.compile(r"\(constraint\s+track_width\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_CLR_RE = re.compile(r"\(constraint\s+hole_clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_TO_HOLE_RE = re.compile(r"\(constraint\s+hole_to_hole\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_EDGE_CLR_RE = re.compile(r"\(constraint\s+edge_clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_SIZE_RE = re.compile(r"\(constraint\s+hole_size\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_VIA_DIAM_RE = re.compile(r"\(constraint\s+via_diameter\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_ANNULAR_RE = re.compile(r"\(constraint\s+annular_width\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_SILK_CLR_RE = re.compile(r"\(constraint\s+silk_clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_TEXT_H_RE = re.compile(r"\(constraint\s+text_height\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_TEXT_T_RE = re.compile(r"\(constraint\s+text_thickness\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_OTHER_CONSTRAINT_RE = re.compile(r"\(constraint\s+([a-zA-Z0-9_]+)\b")
_MIN_VAL_RE = re.compile(r"\(min\s+([0-9.]+)\s*(mm|mil|um)?\)")
_OPT_VAL_RE = re.compile(r"\(opt\s+([0-9.]+)\s*(mm|mil|um)?\)")

_SUPPORTED_CONSTRAINTS = frozenset({
    "clearance", "track_width", "hole_clearance", "hole_to_hole",
    "edge_clearance", "hole_size", "via_diameter", "annular_width",
})


def _strip_comments(text: str) -> str:
    """Strip KiCad ``#`` line comments while preserving ``#`` inside double-quoted strings."""
    out_lines: list[str] = []
    for line in text.splitlines():
        in_quote = False
        cut = len(line)
        for idx, ch in enumerate(line):
            if ch == '"':
                in_quote = not in_quote
            elif ch == "#" and not in_quote:
                cut = idx
                break
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def _to_mm(val: str, unit: str | None) -> float:
    num = float(val)
    if unit == "mil":
        return num * MM_PER_MIL
    if unit == "um":
        return num / 1000.0
    return num


def _extract_mm(body: str, *, allow_opt: bool = False) -> float | None:
    m = _MIN_VAL_RE.search(body)
    if m:
        return _to_mm(m.group(1), m.group(2))
    if allow_opt:
        mo = _OPT_VAL_RE.search(body)
        if mo:
            return _to_mm(mo.group(1), mo.group(2))
    return None


def _parse_rules_for_pair_clearances(
    dru_text: str, all_ncs: set[str] | None = None,
) -> dict[tuple[str, str], float]:
    """Extract (nc_a, nc_b) -> min clearance (mm) from KiCad custom .kicad_dru rules."""
    out: dict[tuple[str, str], float] = {}
    for _name, block in _iter_rules(dru_text):
        cond_m = _COND_RE.search(block)
        if not cond_m:
            continue
        cond = cond_m.group(1)
        clr_m = _CLEARANCE_RE.search(block)
        if not clr_m:
            continue
        mm_val = _extract_mm(clr_m.group(1), allow_opt=False)
        if mm_val is None or mm_val <= 0.0:
            continue
        a_ncs = [m.group(1) or m.group(2) for m in _A_NC_TERM.finditer(cond)]
        b_ncs = [m.group(1) or m.group(2) for m in _B_NC_TERM.finditer(cond)]
        if a_ncs and b_ncs:
            for a in a_ncs:
                for b in b_ncs:
                    key = (a, b) if a <= b else (b, a)
                    out[key] = max(out.get(key, 0.0), mm_val)
        elif a_ncs and not b_ncs:
            targets = all_ncs if all_ncs else set(a_ncs)
            for a in a_ncs:
                for b in targets:
                    key = (a, b) if a <= b else (b, a)
                    out[key] = max(out.get(key, 0.0), mm_val)
    return out


def extract_netclass_clearances(dru_path: Path | None, board: Board) -> dict[tuple[str, str], float]:
    """Return canonical (nc_a, nc_b) -> min clearance (mm) for all pairs >= 1.2 mm.

    Combines KiCad custom ``.kicad_dru`` clearance rules with high-clearance
    netclasses declared in ``board.netclasses`` so the placement solver respects
    galvanic isolation domains and HV clearances from first principles. In KiCad,
    a netclass's own clearance applies against every other netclass via
    ``max(clearance(A), clearance(B))``.
    """
    raw: dict[tuple[str, str], float] = {}
    all_ncs: set[str] = (
        {nc.name for nc in board.netclasses}
        | {n.netclass for n in board.nets if n.netclass}
        | {"Default"}
    )
    for nc in board.netclasses:
        if nc.clearance and float(nc.clearance) >= 1.2:
            val = float(nc.clearance)
            for other in all_ncs:
                key = (nc.name, other) if nc.name <= other else (other, nc.name)
                raw[key] = max(raw.get(key, 0.0), val)
    if dru_path is not None and dru_path.is_file():
        parsed = _parse_rules_for_pair_clearances(dru_path.read_text(encoding="utf-8"), all_ncs=all_ncs)
        for key, val in parsed.items():
            if val >= 1.2:
                raw[key] = max(raw.get(key, 0.0), val)
    return raw


def _iter_rules(text: str) -> list[tuple[str, str]]:
    clean = _strip_comments(text)
    out: list[tuple[str, str]] = []
    pos = 0
    while True:
        m = _RULE_START.search(clean, pos)
        if m is None:
            break
        end = _sexp_end(clean, m.start())
        if end < 0:
            break
        name = m.group(1).strip('"')
        out.append((name, clean[m.start():end]))
        pos = end
    return out


def _resolve_layers(layer_spec: str | None, board: Board) -> list[str]:
    ordered = [cl for cl in sorted(board.copper_layers, key=lambda c: c.index)]
    if not layer_spec:
        return []
    low = layer_spec.strip().lower()
    if low == "outer":
        return [cl.name for cl in ordered if cl.canonical in ("F.Cu", "B.Cu")]
    if low == "inner":
        return [cl.name for cl in ordered if cl.canonical not in ("F.Cu", "B.Cu")]
    if "silk" in low or "mask" in low or "paste" in low or "fab" in low or "crtyd" in low:
        return []
    by_canon = {cl.canonical: cl.name for cl in ordered}
    if layer_spec in by_canon:
        return [by_canon[layer_spec]]
    for cl in ordered:
        if cl.name == layer_spec:
            return [cl.name]
    return []


def _dsn_class_names(dsn: str) -> list[str]:
    net_start = dsn.find("(network")
    wiring_start = dsn.find("(wiring", net_start) if net_start >= 0 else -1
    if net_start < 0:
        return []
    net_block = dsn[net_start:(wiring_start if wiring_start > net_start else len(dsn))]
    return [m.group(1).strip('"') for m in re.finditer(r'\(class\s+("[^"]+"|[^\s()]+)', net_block)]


def _update_class_rule(dsn: str, cls_name: str, scale: float, *, width_mm: float | None,
                       clearance_mm: float | None) -> tuple[str, bool]:
    net_start = dsn.find("(network")
    wiring_start = dsn.find("(wiring", net_start) if net_start >= 0 else -1
    if net_start < 0:
        return dsn, False
    net_end = wiring_start if wiring_start > net_start else len(dsn)
    net_block = dsn[net_start:net_end]
    pat = re.compile(r"\(class\s+" + re.escape(_token(cls_name)) + r"(?=[\s)])")
    cm = pat.search(net_block)
    if cm is None and cls_name != _quote(cls_name):
        pat = re.compile(r"\(class\s+" + re.escape(_quote(cls_name)) + r"(?=[\s)])")
        cm = pat.search(net_block)
    if cm is None:
        return dsn, False
    cls_end = _sexp_end(net_block, cm.start())
    if cls_end < 0:
        return dsn, False
    cls_block = net_block[cm.start():cls_end]
    r_idx = cls_block.find("(rule")
    rule_parts = []
    if width_mm is not None and width_mm > 0:
        rule_parts.append(f"(width {_num(width_mm * scale)})")
    if clearance_mm is not None and clearance_mm > 0:
        rule_parts.append(f"(clearance {_num(clearance_mm * scale)})")
    if not rule_parts:
        return dsn, False
    if r_idx >= 0:
        r_end = _sexp_end(cls_block, r_idx)
        r_body = cls_block[r_idx:r_end]
        for key, val_mm in (("width", width_mm), ("clearance", clearance_mm)):
            if val_mm is None or val_mm <= 0:
                continue
            target = val_mm * scale
            km = re.search(rf"\({key}\s+([0-9.]+)\)", r_body)
            if km:
                if target > float(km.group(1)):
                    r_body = r_body[:km.start()] + f"({key} {_num(target)})" + r_body[km.end():]
            else:
                r_body = r_body[:-1].rstrip() + f" ({key} {_num(target)})" + ")"
        new_cls = cls_block[:r_idx] + r_body + cls_block[r_end:]
    else:
        insert = "\n      (rule " + " ".join(rule_parts) + ")\n    "
        new_cls = cls_block[:-1].rstrip() + insert + ")"
    updated_net = net_block[:cm.start()] + new_cls + net_block[cls_end:]
    return dsn[:net_start] + updated_net + dsn[net_end:], True


def _update_layer_rule(dsn: str, layer_name: str, scale: float, *, width_mm: float | None,
                       clearance_mm: float | None) -> tuple[str, bool]:
    struct_start = dsn.find("(structure")
    if struct_start < 0:
        return dsn, False
    struct_end = _sexp_end(dsn, struct_start)
    if struct_end < 0:
        return dsn, False
    header = dsn[struct_start:struct_end]
    l_pat = re.compile(r"\(layer\s+" + re.escape(_token(layer_name)) + r"(?=[\s)])")
    lm = l_pat.search(header)
    if lm is None:
        return dsn, False
    l_end = _sexp_end(header, lm.start())
    if l_end < 0:
        return dsn, False
    l_block = header[lm.start():l_end]
    parts = []
    if width_mm is not None and width_mm > 0:
        parts.append(f"(width {_num(width_mm * scale)})")
    if clearance_mm is not None and clearance_mm > 0:
        parts.append(f"(clearance {_num(clearance_mm * scale)})")
    if not parts:
        return dsn, False
    r_idx = l_block.find("(rule")
    if r_idx >= 0:
        r_end = _sexp_end(l_block, r_idx)
        r_body = l_block[r_idx:r_end]
        for key, val_mm in (("width", width_mm), ("clearance", clearance_mm)):
            if val_mm is None or val_mm <= 0:
                continue
            target = val_mm * scale
            km = re.search(rf"\({key}\s+([0-9.]+)\)", r_body)
            if km:
                if target > float(km.group(1)):
                    r_body = r_body[:km.start()] + f"({key} {_num(target)})" + r_body[km.end():]
            else:
                r_body = r_body[:-1].rstrip() + f" ({key} {_num(target)})" + ")"
        new_l = l_block[:r_idx] + r_body + l_block[r_end:]
    else:
        new_l = l_block[:-1].rstrip() + f" (rule {' '.join(parts)})" + ")"
    new_header = header[:lm.start()] + new_l + header[l_end:]
    return dsn[:struct_start] + new_header + dsn[struct_end:], True


def _insert_class_class_clearance(dsn: str, left_classes: list[str], right_classes: list[str],
                                  scale: float, clearance_mm: float) -> tuple[str, bool]:
    """Insert or tighten Specctra ``(class_class ...)`` clearance rules inside ``(network ...)``.

    Updates existing ``(class_class ...)`` entries for the same unordered pair in place with
    ``max(existing, target)`` so Freerouting never sees shadowed/conflicting duplicate pairs.
    """
    if clearance_mm <= 0 or not left_classes or not right_classes:
        return dsn, False
    net_start = dsn.find("(network")
    if net_start < 0:
        return dsn, False
    net_end = _sexp_end(dsn, net_start)
    if net_end < 0:
        return dsn, False
    target = clearance_mm * scale
    val = _num(target)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in left_classes:
        for b in right_classes:
            key = (a, b) if a <= b else (b, a)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    if not pairs:
        return dsn, False

    net_block = dsn[net_start:net_end]
    # First update any existing (class_class ...) blocks matching a target pair in place.
    existing_pairs: set[tuple[str, str]] = set()
    cc_pat = re.compile(r"\(class_class\s+")
    matches: list[tuple[int, int, str, tuple[str, str]]] = []
    pos = 0
    while True:
        m = cc_pat.search(net_block, pos)
        if m is None:
            break
        cend = _sexp_end(net_block, m.start())
        if cend < 0:
            break
        cc_body = net_block[m.start():cend]
        r_idx = cc_body.find("(rule")
        cc_hdr = cc_body[:r_idx] if r_idx > 0 else cc_body[:-1]
        toks = re.findall(r'"[^"]+"|[^\s()]+', cc_hdr)
        if len(toks) == 4:
            ca, cb = toks[2].strip('"'), toks[3].strip('"')
            pkey = (ca, cb) if ca <= cb else (cb, ca)
            if pkey in seen:
                matches.append((m.start(), cend, cc_body, pkey))
                existing_pairs.add(pkey)
        pos = cend

    changed = False
    for s, e, cc_body, _pkey in reversed(matches):
        cm = re.search(r"\(clearance\s+([0-9.]+)\)", cc_body)
        if cm:
            if target > float(cm.group(1)) + 1e-6:
                new_body = cc_body[:cm.start()] + f"(clearance {val})" + cc_body[cm.end():]
                net_block = net_block[:s] + new_body + net_block[e:]
            changed = True
        elif "(rule" in cc_body:
            new_body = cc_body.replace("(rule", f"(rule (clearance {val})", 1)
            net_block = net_block[:s] + new_body + net_block[e:]
            changed = True

    entries = []
    for a, b in pairs:
        if (a, b) in existing_pairs:
            continue
        entries.append(
            f"    (class_class kicad_dru_{_token(a)}_{_token(b)} {_token(a)} {_token(b)}\n"
            f"      (rule (clearance {val}))\n    )"
        )
    if entries:
        net_block = net_block[:-1].rstrip() + "\n" + "\n".join(entries) + "\n  )"
        changed = True
    if not changed:
        return dsn, False
    return dsn[:net_start] + net_block + dsn[net_end:], True


def _extract_compound_netclasses(cond: str) -> tuple[list[str], list[str]] | None:
    """Extract (A_patterns, B_patterns) from KiCad 8/9/10 compound netclass conditions.

    Handles both simple and OR-grouped expressions such as:
    ``(A.hasNetclass('P1') || A.hasNetclass('P2')) && (B.hasNetclass('S1') || B.hasNetclass('S2'))``
    and intra-class rules with simple ``A.Type`` / ``B.Type`` qualifiers.
    """
    a_pats = [m.group(1) or m.group(2) for m in _A_NC_TERM.finditer(cond)]
    b_pats = [m.group(1) or m.group(2) for m in _B_NC_TERM.finditer(cond)]
    if not a_pats and not b_pats:
        return None
    # Strip matched netclass terms, type qualifiers, boolean ops, and parentheses to verify no unsupported clauses remain
    stripped = _A_NC_TERM.sub("", cond)
    stripped = _B_NC_TERM.sub("", stripped)
    stripped = re.sub(r"[AB]\.Type\s*[=!]=\s*['\"][^'\"]+['\"]", "", stripped)
    stripped = re.sub(r"(&&|\|\||!|\(|\)|\s)+", "", stripped)
    if stripped:
        return None
    return (list(dict.fromkeys(a_pats)), list(dict.fromkeys(b_pats)))


def _extract_compound_netnames(cond: str) -> tuple[list[str], list[str]] | None:
    """Extract (A_net_patterns, B_net_patterns) from KiCad 8/9/10 compound NetName conditions.

    Handles expressions such as:
    ``(A.NetName == '/AUX/AC_L' || A.NetName == '/AUX/AC_L_FUSED') && (B.NetName == '/AUX/AC_N' || ...)``
    with optional ``A.Type`` / ``B.Type`` qualifiers.
    """
    a_pats = [m.group(1) for m in _A_NN_TERM.finditer(cond)]
    b_pats = [m.group(1) for m in _B_NN_TERM.finditer(cond)]
    if not a_pats or not b_pats:
        return None
    stripped = _A_NN_TERM.sub("", cond)
    stripped = _B_NN_TERM.sub("", stripped)
    stripped = re.sub(r"[AB]\.Type\s*[=!]=\s*['\"][^'\"]+['\"]", "", stripped)
    stripped = re.sub(r"(&&|\|\||!|\(|\)|\s)+", "", stripped)
    if stripped:
        return None
    return (list(dict.fromkeys(a_pats)), list(dict.fromkeys(b_pats)))


def _is_global_copper_cond(cond: str) -> bool:
    """Return True for simple board-wide different-net copper clearance conditions."""
    c = re.sub(r"\s+", " ", cond.strip()).lower()
    if not c or c == "true":
        return True
    if any(tok in c for tok in ("intersectsarea", "fromto", "indiffpair", "netname", "zone", "silk", "text", "graphic")):
        return False
    # Common JLCPCB/KiCad global pad/track/plated clearance clauses
    allowed_patterns = (
        "a.net != b.net",
        "a.type == 'track'",
        "b.type == 'track'",
        "a.type == 'pad'",
        "a.isplated()",
        "b.isplated()",
    )
    return "a.netclass" not in c and "hasnetclass" not in c and any(p in c for p in allowed_patterns)


def extract_effective_rules(dru_path: Path) -> dict[str, float]:
    """Extract board-wide numeric constraint floors from ``.kicad_dru`` for S6a, routing, and silkscreen.

    Includes copper/drill/annular floors plus silkscreen text/clearance floors (which Specctra DSN
    cannot represent natively, so they remain in ``untranslated`` for DSN P5 honesty while being
    honored directly by ``silkscreen.build_config`` and ``s6a``).
    """
    if not dru_path.is_file():
        return {}
    text = dru_path.read_text(encoding="utf-8", errors="replace")
    eff: dict[str, float] = {}

    def _raise(key: str, val: float | None) -> None:
        if val is not None and val > 0:
            eff[key] = max(eff.get(key, 0.0), round(val, 6))

    for _rule_name, block in _iter_rules(text):
        cond_m = _COND_RE.search(block)
        cond = cond_m.group(1).strip() if cond_m else ""
        cond_low = re.sub(r"\s+", " ", cond).lower()
        lyr_m = _LAYER_RE.search(block)
        lyr_spec = (lyr_m.group(1).strip() if lyr_m else "")
        lyr_low = lyr_spec.lower()
        if any(tok in cond_low for tok in ("intersectsarea", "fromto", "indiffpair", "netname", "castellated")):
            continue
        is_silk = "silk" in lyr_low or "silk" in cond_low
        if (m := _SILK_CLR_RE.search(block)):
            _raise("silk_clearance_mm", _extract_mm(m.group(1)))
        if is_silk:
            if (m := _TEXT_H_RE.search(block)):
                _raise("min_silk_text_height_mm", _extract_mm(m.group(1)))
            if (m := _TEXT_T_RE.search(block)):
                _raise("min_silk_text_thickness_mm", _extract_mm(m.group(1)))
            continue
        if any(tok in cond_low for tok in ("zone", "text", "graphic")):
            continue
        if (m := _HOLE_CLR_RE.search(block)) and "!a.isplated()" not in cond_low:
            _raise("hole_clearance_mm", _extract_mm(m.group(1)))
        if (m := _HOLE_TO_HOLE_RE.search(block)) and "a.net == b.net" not in cond_low:
            _raise("hole_to_hole_mm", _extract_mm(m.group(1)))
        if (m := _EDGE_CLR_RE.search(block)) and "'zone'" not in cond_low:
            _raise("edge_clearance_mm", _extract_mm(m.group(1)))
        if (m := _VIA_DIAM_RE.search(block)) and "!a.isplated()" not in cond_low:
            _raise("min_via_diameter_mm", _extract_mm(m.group(1)))
        if (m := _HOLE_SIZE_RE.search(block)) and ("'via'" in cond_low or (not cond_low and not lyr_spec)):
            _raise("min_via_drill_mm", _extract_mm(m.group(1)))
        if (m := _ANNULAR_RE.search(block)) and ("'via'" in cond_low or not cond_low):
            _raise("min_via_annular_width_mm", _extract_mm(m.group(1)))
    return eff


def compile_dru_to_dsn(dsn: str, dru_path: Path, board: Board) -> tuple[str, dict]:
    """Translate supported ``.kicad_dru`` constraints to Specctra DSN and effective router floors.

    Returns ``(updated_dsn, meta)`` where ``meta["effective_rules"]`` carries raised
    ``hole_clearance_mm``, ``hole_to_hole_mm``, ``edge_clearance_mm``, ``min_via_diameter_mm``,
    ``min_via_drill_mm``, ``min_via_annular_width_mm``, and silkscreen floors for downstream stages.
    """
    empty = {"translated": [], "untranslated": [], "invalid_syntax": [], "applied_rules": 0, "effective_rules": {}}
    if not dru_path.is_file():
        return dsn, empty
    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        return dsn, empty
    scale = _SCALE[unit.group(1)]

    text = dru_path.read_text(encoding="utf-8", errors="replace")
    translated: list[str] = []
    untranslated: list[str] = []
    invalid_syntax: list[str] = []
    eff: dict[str, float] = {}
    dsn_classes = _dsn_class_names(dsn)

    def _raise_eff(key: str, val: float | None) -> bool:
        if val is None or val <= 0:
            return False
        eff[key] = max(eff.get(key, 0.0), round(val, 6))
        return True

    for rule_name, block in _iter_rules(text):
        found_constraints = {m.group(1) for m in _OTHER_CONSTRAINT_RE.finditer(block)}
        cond_m = _COND_RE.search(block)
        cond = cond_m.group(1).strip() if cond_m else ""
        if cond and _INVALID_DRU_TOKEN.search(cond):
            invalid_syntax.append(f"{rule_name}: invalid KiCad DRU token in condition {cond!r}")
            untranslated.append(rule_name)
            continue
        cond_low = re.sub(r"\s+", " ", cond).lower()
        lyr_m = _LAYER_RE.search(block)
        lyr_spec = lyr_m.group(1).strip() if lyr_m else None
        lyr_low = (lyr_spec or "").lower()

        # Harvest silkscreen floors into effective_rules for silkscreen.build_config, while
        # keeping silkscreen rules in untranslated (Specctra DSN cannot enforce silk rules).
        if "silk" in lyr_low or "silk" in cond_low or found_constraints & {"silk_clearance", "text_height", "text_thickness"}:
            if (m := _SILK_CLR_RE.search(block)):
                _raise_eff("silk_clearance_mm", _extract_mm(m.group(1)))
            if "silk" in lyr_low or "silk" in cond_low:
                if (m := _TEXT_H_RE.search(block)):
                    _raise_eff("min_silk_text_height_mm", _extract_mm(m.group(1)))
                if (m := _TEXT_T_RE.search(block)):
                    _raise_eff("min_silk_text_thickness_mm", _extract_mm(m.group(1)))
            untranslated.append(rule_name)
            continue

        if not found_constraints or not (found_constraints & _SUPPORTED_CONSTRAINTS):
            untranslated.append(rule_name)
            continue
        has_unsupported_extra = bool(found_constraints - _SUPPORTED_CONSTRAINTS)

        # Reject complex spatial / pair / zone / silk conditions cleanly (P5 honesty);
        # compound A.NetName == ... && B.NetName == ... clearance rules ARE translated below.
        compound_nn = _extract_compound_netnames(cond) if "netname" in cond_low else None
        if any(tok in cond_low for tok in ("intersectsarea", "fromto", "indiffpair", "zone", "silk", "text", "graphic", "castellated")):
            untranslated.append(rule_name)
            continue
        if "netname" in cond_low and compound_nn is None:
            untranslated.append(rule_name)
            continue

        cm = _CLEARANCE_RE.search(block)
        wm = _WIDTH_RE.search(block)
        hcm = _HOLE_CLR_RE.search(block)
        hthm = _HOLE_TO_HOLE_RE.search(block)
        ecm = _EDGE_CLR_RE.search(block)
        hsm = _HOLE_SIZE_RE.search(block)
        vdm = _VIA_DIAM_RE.search(block)
        anm = _ANNULAR_RE.search(block)

        clr_mm = _extract_mm(cm.group(1)) if cm else None
        wid_mm = _extract_mm(wm.group(1), allow_opt=True) if wm else None
        hc_mm = _extract_mm(hcm.group(1)) if hcm else None
        hth_mm = _extract_mm(hthm.group(1)) if hthm else None
        ec_mm = _extract_mm(ecm.group(1)) if ecm else None
        hs_mm = _extract_mm(hsm.group(1)) if hsm else None
        vd_mm = _extract_mm(vdm.group(1)) if vdm else None
        an_mm = _extract_mm(anm.group(1)) if anm else None

        applied = False
        # 1) Router floor rules (hole_clearance, hole_to_hole, edge_clearance, via size, annular width)
        if hc_mm and ("!a.isplated()" not in cond_low):
            applied |= _raise_eff("hole_clearance_mm", hc_mm)
        if hth_mm and ("a.net == b.net" not in cond_low):
            applied |= _raise_eff("hole_to_hole_mm", hth_mm)
        if ec_mm and ("'zone'" not in cond_low):
            applied |= _raise_eff("edge_clearance_mm", ec_mm)
        if vd_mm and ("!a.isplated()" not in cond_low):
            applied |= _raise_eff("min_via_diameter_mm", vd_mm)
        if hs_mm and ("'via'" in cond_low or (not cond_low and not lyr_spec)):
            applied |= _raise_eff("min_via_drill_mm", hs_mm)
        if an_mm and ("'via'" in cond_low or not cond_low):
            applied |= _raise_eff("min_via_annular_width_mm", an_mm)

        # 2) DSN class or layer width/clearance rules
        if clr_mm is not None or wid_mm is not None:
            def _resolve_nc(patterns: list[str], classes: list[str] = dsn_classes) -> list[str]:
                out_c: list[str] = []
                for pat in patterns:
                    hits = [c for c in classes if fnmatch.fnmatchcase(c, pat)]
                    if not hits and "*" not in pat and "?" not in pat:
                        hits = [pat]
                    out_c.extend(hits)
                return list(dict.fromkeys(out_c))

            nc_m = _NETCLASS_COND.match(cond)
            nc_pair_m = _NETCLASS_PAIR_COND.match(cond)
            compound_nc = _extract_compound_netclasses(cond) if not (nc_m or nc_pair_m) else None
            if nc_m:
                pattern = nc_m.group(1) or nc_m.group(2)
                for cname in _resolve_nc([pattern]):
                    dsn, ok = _update_class_rule(dsn, cname, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                    applied |= ok
            elif nc_pair_m:
                a_pat = nc_pair_m.group(1) or nc_pair_m.group(2)
                b_pat = nc_pair_m.group(3) or nc_pair_m.group(4)
                a_cls = _resolve_nc([a_pat])
                b_cls = _resolve_nc([b_pat])
                if a_cls == b_cls or "||" in cond:
                    for cname in dict.fromkeys(a_cls + b_cls):
                        dsn, ok = _update_class_rule(dsn, cname, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                        applied |= ok
                elif clr_mm is not None and wid_mm is None:
                    dsn, ok = _insert_class_class_clearance(dsn, a_cls, b_cls, scale, clr_mm)
                    applied |= ok
                else:
                    for cname in dict.fromkeys(a_cls + b_cls):
                        dsn, ok = _update_class_rule(dsn, cname, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                        applied |= ok
            elif compound_nc is not None:
                a_pats, b_pats = compound_nc
                a_cls = _resolve_nc(a_pats)
                b_cls = _resolve_nc(b_pats)
                if a_cls and b_cls and a_cls != b_cls and clr_mm is not None and wid_mm is None:
                    dsn, ok = _insert_class_class_clearance(dsn, a_cls, b_cls, scale, clr_mm)
                    applied |= ok
                elif a_cls or b_cls:
                    for cname in dict.fromkeys(a_cls + b_cls):
                        dsn, ok = _update_class_rule(dsn, cname, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                        applied |= ok
            elif compound_nn is not None and clr_mm is not None and wid_mm is None:
                a_npats, b_npats = compound_nn
                board_net_names = [n.name for n in board.nets if n.name]

                def _resolve_nets(pats: list[str], names: list[str] = board_net_names) -> list[str]:
                    hits: list[str] = []
                    for pat in pats:
                        matched = [bn for bn in names if fnmatch.fnmatchcase(bn, pat)]
                        if not matched and pat in names:
                            matched = [pat]
                        hits.extend(matched)
                    return list(dict.fromkeys(hits))

                a_nets = _resolve_nets(a_npats)
                b_nets = _resolve_nets(b_npats)
                if a_nets and b_nets:
                    net_to_cls: dict[str, str] = {}
                    for nname in dict.fromkeys(a_nets + b_nets):
                        dsn, cname = isolate_net_in_class(dsn, nname, suffix="drunet")
                        if cname:
                            net_to_cls[nname] = cname
                    dsn_classes = _dsn_class_names(dsn)
                    # Insert pairwise (class_class ...) strictly between distinct nets (a != b)
                    distinct_pairs: set[tuple[str, str]] = set()
                    for na in a_nets:
                        ca = net_to_cls.get(na)
                        if not ca:
                            continue
                        for nb in b_nets:
                            cb = net_to_cls.get(nb)
                            if not cb or ca == cb:
                                continue
                            pair = (ca, cb) if ca <= cb else (cb, ca)
                            distinct_pairs.add(pair)
                    for ca, cb in sorted(distinct_pairs):
                        dsn, ok = _insert_class_class_clearance(dsn, [ca], [cb], scale, clr_mm)
                        applied |= ok
            elif _is_global_copper_cond(cond):
                target_layers = _resolve_layers(lyr_spec, board)
                if lyr_spec:
                    for tlyr in target_layers:
                        dsn, ok = _update_layer_rule(dsn, tlyr, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                        applied |= ok
                else:
                    for default_name in ("kicad_default", "Default"):
                        dsn, ok = _update_class_rule(dsn, default_name, scale, width_mm=wid_mm, clearance_mm=clr_mm)
                        if ok:
                            applied = True
                            break

        if applied and not has_unsupported_extra:
            translated.append(rule_name)
        else:
            untranslated.append(rule_name)

    return dsn, {
        "translated": sorted(set(translated)),
        "untranslated": sorted(set(untranslated)),
        "invalid_syntax": invalid_syntax,
        "applied_rules": len(translated),
        "effective_rules": eff,
    }
