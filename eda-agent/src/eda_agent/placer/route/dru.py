# SPDX-License-Identifier: MIT
"""Translate real KiCad custom DRC rules (``.kicad_dru``) into Specctra DSN and router constraints."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .dsn import _SCALE, _UNIT, _num, _quote, _sexp_end, _token

if TYPE_CHECKING:
    from ..ir import Board

_RULE_START = re.compile(r'\(rule\s+("[^"]+"|[^\s()]+)')
_COND_RE = re.compile(r'\(condition\s+"([^"]*)"\)')
_LAYER_RE = re.compile(r'\(layer\s+"?([^"\s()]+)"?\)')
_NETCLASS_COND = re.compile(r"^\(?A\.NetClass\s*==\s*['\"]([^'\"]+)['\"]\)?$")
_CLEARANCE_RE = re.compile(r"\(constraint\s+clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_WIDTH_RE = re.compile(r"\(constraint\s+track_width\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_CLR_RE = re.compile(r"\(constraint\s+hole_clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_TO_HOLE_RE = re.compile(r"\(constraint\s+hole_to_hole\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_EDGE_CLR_RE = re.compile(r"\(constraint\s+edge_clearance\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_HOLE_SIZE_RE = re.compile(r"\(constraint\s+hole_size\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_VIA_DIAM_RE = re.compile(r"\(constraint\s+via_diameter\b([^()]*(?:\([^()]*\)[^()]*)*)\)")
_OTHER_CONSTRAINT_RE = re.compile(r"\(constraint\s+([a-zA-Z0-9_]+)\b")
_MIN_VAL_RE = re.compile(r"\(min\s+([0-9.]+)\s*(mm|mil|um)?\)")
_OPT_VAL_RE = re.compile(r"\(opt\s+([0-9.]+)\s*(mm|mil|um)?\)")

_SUPPORTED_CONSTRAINTS = frozenset({
    "clearance", "track_width", "hole_clearance", "hole_to_hole",
    "edge_clearance", "hole_size", "via_diameter",
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
        return num * 0.0254
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
    return "a.netclass" not in c and any(p in c for p in allowed_patterns)


def compile_dru_to_dsn(dsn: str, dru_path: Path, board: Board) -> tuple[str, dict]:
    """Translate supported ``.kicad_dru`` constraints to Specctra DSN and effective router floors.

    Returns ``(updated_dsn, meta)`` where ``meta["effective_rules"]`` carries raised
    ``hole_clearance_mm``, ``hole_to_hole_mm``, ``edge_clearance_mm``, ``min_via_diameter_mm``,
    and ``min_via_drill_mm`` floors for ``route_check`` and ``dsn.enforce_via_hole_clearances``.
    """
    empty = {"translated": [], "untranslated": [], "applied_rules": 0, "effective_rules": {}}
    if not dru_path.is_file():
        return dsn, empty
    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        return dsn, empty
    scale = _SCALE[unit.group(1)]

    text = dru_path.read_text(encoding="utf-8", errors="replace")
    translated: list[str] = []
    untranslated: list[str] = []
    eff: dict[str, float] = {}
    dsn_classes = _dsn_class_names(dsn)

    def _raise_eff(key: str, val: float | None) -> bool:
        if val is None or val <= 0:
            return False
        eff[key] = max(eff.get(key, 0.0), round(val, 6))
        return True

    for rule_name, block in _iter_rules(text):
        found_constraints = {m.group(1) for m in _OTHER_CONSTRAINT_RE.finditer(block)}
        if not found_constraints or not (found_constraints & _SUPPORTED_CONSTRAINTS):
            untranslated.append(rule_name)
            continue
        has_unsupported_extra = bool(found_constraints - _SUPPORTED_CONSTRAINTS)

        cond_m = _COND_RE.search(block)
        cond = cond_m.group(1).strip() if cond_m else ""
        cond_low = re.sub(r"\s+", " ", cond).lower()
        lyr_m = _LAYER_RE.search(block)
        lyr_spec = lyr_m.group(1).strip() if lyr_m else None

        # Reject complex spatial / pair / zone / silk conditions cleanly (P5 honesty)
        if any(tok in cond_low for tok in ("intersectsarea", "fromto", "indiffpair", "netname", "zone", "silk", "text", "graphic", "castellated")):
            untranslated.append(rule_name)
            continue

        cm = _CLEARANCE_RE.search(block)
        wm = _WIDTH_RE.search(block)
        hcm = _HOLE_CLR_RE.search(block)
        hthm = _HOLE_TO_HOLE_RE.search(block)
        ecm = _EDGE_CLR_RE.search(block)
        hsm = _HOLE_SIZE_RE.search(block)
        vdm = _VIA_DIAM_RE.search(block)

        clr_mm = _extract_mm(cm.group(1)) if cm else None
        wid_mm = _extract_mm(wm.group(1), allow_opt=True) if wm else None
        hc_mm = _extract_mm(hcm.group(1)) if hcm else None
        hth_mm = _extract_mm(hthm.group(1)) if hthm else None
        ec_mm = _extract_mm(ecm.group(1)) if ecm else None
        hs_mm = _extract_mm(hsm.group(1)) if hsm else None
        vd_mm = _extract_mm(vdm.group(1)) if vdm else None

        applied = False
        # 1) Router floor rules (hole_clearance, hole_to_hole, edge_clearance, via size)
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

        # 2) DSN class or layer width/clearance rules
        if clr_mm is not None or wid_mm is not None:
            nc_m = _NETCLASS_COND.match(cond)
            if nc_m:
                pattern = nc_m.group(1)
                matched_classes = [c for c in dsn_classes if fnmatch.fnmatchcase(c, pattern)]
                if not matched_classes and "*" not in pattern and "?" not in pattern:
                    matched_classes = [pattern]
                for cname in matched_classes:
                    dsn, ok = _update_class_rule(dsn, cname, scale, width_mm=wid_mm, clearance_mm=clr_mm)
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
        "applied_rules": len(translated),
        "effective_rules": eff,
    }
