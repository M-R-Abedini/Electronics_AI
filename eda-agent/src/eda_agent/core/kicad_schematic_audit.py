# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Strict geometric overlap and wire-crossing audit for KiCad schematics (.kicad_sch).

Detects visual collisions that electrical ERC does not catch:
- Symbol body vs. symbol body / power symbol overlaps
- Label (local, global, hierarchical) vs. symbol / power / label / text overlaps
- Rotated (0/90/180/270 deg) Reference and Value text vs. symbols, labels, and wires
- Wire segments passing through component graphic bodies (wire_through_symbol)
- Wire segments passing through net labels (wire_through_label)
- Sheet border / margin intrusions
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..libimport.kicad.sexpr import find, find_all, loads, value

PAPERS: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class GeometricViolation:
    kind: str
    a: str
    b: str
    at_mm: tuple[float, float]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "a": self.a,
            "b": self.b,
            "at_mm": [round(self.at_mm[0], 2), round(self.at_mm[1], 2)],
            "detail": self.detail,
        }


def _rot_sym_pt(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """Rotate a symbol-local point (Y-up) and convert to schematic coordinates (Y-down)."""
    rad = math.radians(angle_deg)
    c, s = round(math.cos(rad), 9), round(math.sin(rad), 9)
    return x * c - y * s, -(x * s + y * c)


def _bbox_intersect(a: BBox, b: BBox, tol: float = 0.2) -> bool:
    return (
        a[0] + tol < b[2]
        and a[2] - tol > b[0]
        and a[1] + tol < b[3]
        and a[3] - tol > b[1]
    )


def _seg_intersects_bbox(
    x1: float, y1: float, x2: float, y2: float, box: BBox, pad: float = 0.0
) -> bool:
    """Liang-Barsky segment vs. axis-aligned bounding box test."""
    bx1, by1, bx2, by2 = box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad
    if bx1 >= bx2 or by1 >= by2:
        return False
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x1 - bx1),
        (dx, bx2 - x1),
        (-dy, y1 - by1),
        (dy, by2 - y1),
    ):
        if abs(p) < 1e-9:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
    return t0 < t1


def _parse_lib_symbol(lib_sym: list) -> tuple[BBox, BBox, bool]:
    """Return (full_bbox_yup, body_only_bbox_yup, is_power)."""
    full_xs, full_ys = [0.0], [0.0]
    body_xs, body_ys = [], []
    is_power = any(isinstance(c, list) and c and c[0] == "power" for c in lib_sym)
    for unit in find_all(lib_sym, "symbol"):
        for item in unit[2:]:
            if not isinstance(item, list) or not item:
                continue
            tag = item[0]
            if tag == "rectangle":
                s, e = find(item, "start"), find(item, "end")
                if s and e:
                    pts_x = [float(s[1]), float(e[1])]
                    pts_y = [float(s[2]), float(e[2])]
                    full_xs.extend(pts_x)
                    full_ys.extend(pts_y)
                    body_xs.extend(pts_x)
                    body_ys.extend(pts_y)
            elif tag == "polyline":
                pts = find(item, "pts")
                if pts:
                    for xy in find_all(pts, "xy"):
                        vx, vy = float(xy[1]), float(xy[2])
                        full_xs.append(vx)
                        full_ys.append(vy)
                        body_xs.append(vx)
                        body_ys.append(vy)
            elif tag == "circle":
                c = find(item, "center")
                r = float(value(item, "radius", default=0))
                if c:
                    cx, cy = float(c[1]), float(c[2])
                    full_xs.extend([cx - r, cx + r])
                    full_ys.extend([cy - r, cy + r])
                    body_xs.extend([cx - r, cx + r])
                    body_ys.extend([cy - r, cy + r])
            elif tag == "arc":
                for k in ("start", "mid", "end"):
                    pt = find(item, k)
                    if pt:
                        vx, vy = float(pt[1]), float(pt[2])
                        full_xs.append(vx)
                        full_ys.append(vy)
                        body_xs.append(vx)
                        body_ys.append(vy)
            elif tag == "pin":
                at = find(item, "at")
                if at:
                    full_xs.append(float(at[1]))
                    full_ys.append(float(at[2]))
    full_bb = (min(full_xs), min(full_ys), max(full_xs), max(full_ys))
    if body_xs and body_ys:
        body_bb = (min(body_xs), min(body_ys), max(body_xs), max(body_ys))
    else:
        body_bb = full_bb
    return full_bb, body_bb, is_power


def _text_bbox(
    text: str,
    x: float,
    y: float,
    angle: float,
    justify: str,
    font_size: float,
) -> BBox:
    """Compute schematic bounding box for horizontal (0/180) or vertical (90/270) text."""
    ang = int(round(angle)) % 360
    length = max(1.5, len(text) * font_size * 0.85)
    height = max(0.9, font_size * 1.35)
    if ang in (90, 270):
        # Vertical text: length runs along Y, height along X.
        if justify == "left":
            return (x - height / 2, y - length, x + height / 2, y)
        if justify == "right":
            return (x - height / 2, y, x + height / 2, y + length)
        return (x - height / 2, y - length / 2, x + height / 2, y + length / 2)
    if justify == "left":
        return (x, y - height / 2, x + length, y + height / 2)
    if justify == "right":
        return (x - length, y - height / 2, x, y + height / 2)
    return (x - length / 2, y - height / 2, x + length / 2, y + height / 2)


def _label_bbox(
    kind: str,
    name: str,
    x: float,
    y: float,
    angle: float,
    justify: str,
    font_size: float,
) -> BBox:
    ang = int(round(angle)) % 360
    extra = 2.5 if kind in ("global_label", "hierarchical_label") else 0.5
    w = max(2.54, len(name) * font_size * 0.95 + extra)
    h = max(1.6, font_size * 1.8)
    if ang in (0, 180):
        bx1, bx2 = (x - w, x) if justify == "right" else (x, x + w)
        return (bx1, y - h / 2, bx2, y + h / 2)
    by1, by2 = (y - w, y) if justify == "left" else (y, y + w)
    return (x - h / 2, by1, x + h / 2, by2)


def _extract_font_size(effects: list | None, default: float = 1.27) -> float:
    if not effects:
        return default
    font = find(effects, "font")
    if not font:
        return default
    size = find(font, "size")
    if size and len(size) >= 2:
        try:
            return float(size[1])
        except (TypeError, ValueError):
            return default
    return default


def audit_schematic_geometry(
    source: str,
    *,
    check_wires: bool = True,
    border_margin_mm: float = 10.0,
) -> dict[str, Any]:
    """Audit a single .kicad_sch S-expression string for visual/geometric collisions.

    Returns a JSON-serializable report with ``ok``, ``violation_count``, ``by_kind``,
    and ``violations`` (each with ``kind``, ``a``, ``b``, ``at_mm``, ``detail``).
    """
    tree = loads(source)
    paper_node = find(tree, "paper")
    paper = str(paper_node[1]) if paper_node and len(paper_node) > 1 else "A3"
    pw, ph = PAPERS.get(paper, (420.0, 297.0))
    border: BBox = (
        border_margin_mm,
        border_margin_mm,
        pw - border_margin_mm,
        ph - border_margin_mm,
    )

    lib_info: dict[str, tuple[BBox, BBox, bool]] = {}
    lib_syms = find(tree, "lib_symbols")
    if lib_syms:
        for sym in find_all(lib_syms, "symbol"):
            if isinstance(sym[1], str) and ":" in sym[1]:
                lib_info[sym[1]] = _parse_lib_symbol(sym)

    items: list[tuple[str, str, str, BBox, tuple[float, float]]] = []
    body_boxes: list[tuple[str, str, BBox]] = []
    label_boxes: list[tuple[str, str, BBox, tuple[float, float]]] = []
    text_boxes: list[tuple[str, str, BBox]] = []
    wires: list[tuple[float, float, float, float]] = []

    for idx, node in enumerate(tree):
        if not isinstance(node, list) or not node:
            continue
        tag = node[0]
        if tag == "symbol":
            lid = value(node, "lib_id")
            at = find(node, "at")
            if not at or not lid:
                continue
            sx, sy = float(at[1]), float(at[2])
            ang = float(at[3]) if len(at) > 3 else 0.0
            full_lb, body_lb, is_power = lib_info.get(
                lid,
                ((-1.27, -1.27, 1.27, 1.27), (-1.27, -1.27, 1.27, 1.27), str(lid).startswith("power:")),
            )
            sym_key = f"sym_{idx}"
            ref, val = "", ""
            for prop in find_all(node, "property"):
                if prop[1] == "Reference":
                    ref = str(prop[2])
                elif prop[1] == "Value":
                    val = str(prop[2])
                eff = find(prop, "effects")
                hidden = (value(prop, "hide") == "yes") or (eff and value(eff, "hide") == "yes")
                if not hidden and prop[1] in ("Reference", "Value") and prop[2]:
                    pat = find(prop, "at")
                    if pat:
                        px, py = float(pat[1]), float(pat[2])
                        pang = float(pat[3]) if len(pat) > 3 else 0.0
                        just = "center"
                        if eff:
                            jn = find(eff, "justify")
                            if jn and len(jn) > 1:
                                just = str(jn[1])
                        fsize = _extract_font_size(eff, default=1.27)
                        tb = _text_bbox(str(prop[2]), px, py, pang, just, fsize)
                        tname = f"{ref}.{prop[1]}={prop[2]}"
                        items.append(("text", sym_key, tname, tb, (px, py)))
                        text_boxes.append((sym_key, tname, tb))

            def _xform_box(lb: BBox) -> BBox:
                corners = [
                    _rot_sym_pt(lb[0], lb[1], ang),
                    _rot_sym_pt(lb[2], lb[1], ang),
                    _rot_sym_pt(lb[0], lb[3], ang),
                    _rot_sym_pt(lb[2], lb[3], ang),
                ]
                return (
                    sx + min(c[0] for c in corners),
                    sy + min(c[1] for c in corners),
                    sx + max(c[0] for c in corners),
                    sy + max(c[1] for c in corners),
                )

            full_box = _xform_box(full_lb)
            body_box = _xform_box(body_lb)
            kind = "power" if is_power else "symbol"
            sname = f"{ref}({val or lid})"
            items.append((kind, sym_key, sname, full_box, (sx, sy)))
            if not is_power:
                body_boxes.append((sym_key, sname, body_box))

        elif tag in ("label", "global_label", "hierarchical_label"):
            name = str(node[1])
            at = find(node, "at")
            if not at:
                continue
            lx, ly = float(at[1]), float(at[2])
            ang = float(at[3]) if len(at) > 3 else 0.0
            eff = find(node, "effects")
            just = "left"
            if eff:
                jn = find(eff, "justify")
                if jn and len(jn) > 1:
                    just = str(jn[1])
            fsize = _extract_font_size(eff, default=1.0)
            lb = _label_bbox(tag, name, lx, ly, ang, just, fsize)
            lkey = f"lbl_{idx}"
            items.append((tag, lkey, name, lb, (lx, ly)))
            label_boxes.append((lkey, f"{tag}:{name}", lb, (lx, ly)))

        elif tag == "wire":
            pts = find(node, "pts")
            if pts:
                xys = find_all(pts, "xy")
                if len(xys) >= 2:
                    wires.append((
                        float(xys[0][1]),
                        float(xys[0][2]),
                        float(xys[1][1]),
                        float(xys[1][2]),
                    ))

    violations: list[GeometricViolation] = []

    # 1. Pairwise item overlaps (symbols, power, labels, rotated/horizontal text)
    for i in range(len(items)):
        k1, id1, n1, b1, _ = items[i]
        for j in range(i + 1, len(items)):
            k2, id2, n2, b2, _ = items[j]
            if id1 == id2:
                continue
            if _bbox_intersect(b1, b2, tol=0.25):
                cx = (max(b1[0], b2[0]) + min(b1[2], b2[2])) / 2
                cy = (max(b1[1], b2[1]) + min(b1[3], b2[3])) / 2
                violations.append(
                    GeometricViolation(
                        kind=f"{k1}_overlap_{k2}",
                        a=f"{k1}:{n1}",
                        b=f"{k2}:{n2}",
                        at_mm=(cx, cy),
                        detail=f"{k1} {n1} overlaps {k2} {n2}",
                    )
                )

    # 2. Wire segments crossing through symbol bodies, labels, or visible text
    if check_wires:
        for x1, y1, x2, y2 in wires:
            if math.hypot(x2 - x1, y2 - y1) < 0.05:
                continue
            for _, sname, bbox in body_boxes:
                # Ignore tiny 0-area bodies; require wire to penetrate >= 0.35mm inside body
                if _seg_intersects_bbox(x1, y1, x2, y2, bbox, pad=0.35):
                    # Allow wires that merely terminate at the body boundary
                    in1 = (bbox[0] + 0.35 < x1 < bbox[2] - 0.35 and bbox[1] + 0.35 < y1 < bbox[3] - 0.35)
                    in2 = (bbox[0] + 0.35 < x2 < bbox[2] - 0.35 and bbox[1] + 0.35 < y2 < bbox[3] - 0.35)
                    if not (in1 or in2):
                        # Segment passes straight through the symbol body!
                        cx = (bbox[0] + bbox[2]) / 2
                        cy = (bbox[1] + bbox[3]) / 2
                        violations.append(
                            GeometricViolation(
                                kind="wire_through_symbol",
                                a=f"wire({x1:.1f},{y1:.1f}->{x2:.1f},{y2:.1f})",
                                b=f"symbol:{sname}",
                                at_mm=(cx, cy),
                                detail=f"wire crosses through body of {sname}",
                            )
                        )
            for _, lname, lbox, (lx, ly) in label_boxes:
                # Exclude wire whose endpoint IS the label's anchor point
                if (math.hypot(x1 - lx, y1 - ly) < 0.25) or (math.hypot(x2 - lx, y2 - ly) < 0.25):
                    continue
                if _seg_intersects_bbox(x1, y1, x2, y2, lbox, pad=0.2):
                    violations.append(
                        GeometricViolation(
                            kind="wire_through_label",
                            a=f"wire({x1:.1f},{y1:.1f}->{x2:.1f},{y2:.1f})",
                            b=lname,
                            at_mm=(lx, ly),
                            detail=f"wire crosses through {lname}",
                        )
                    )
            for _, tname, tbox in text_boxes:
                if _seg_intersects_bbox(x1, y1, x2, y2, tbox, pad=0.25):
                    cx = (tbox[0] + tbox[2]) / 2
                    cy = (tbox[1] + tbox[3]) / 2
                    violations.append(
                        GeometricViolation(
                            kind="wire_through_text",
                            a=f"wire({x1:.1f},{y1:.1f}->{x2:.1f},{y2:.1f})",
                            b=f"text:{tname}",
                            at_mm=(cx, cy),
                            detail=f"wire crosses through {tname}",
                        )
                    )

    # 3. Sheet border intrusion
    for k, _, n, b, (ax, ay) in items:
        if b[0] < border[0] or b[1] < border[1] or b[2] > border[2] or b[3] > border[3]:
            violations.append(
                GeometricViolation(
                    kind="border_intrusion",
                    a=f"{k}:{n}",
                    b=f"paper:{paper}",
                    at_mm=(ax, ay),
                    detail=f"{k} {n} extends outside {paper} border",
                )
            )

    by_kind: dict[str, int] = {}
    for v in violations:
        by_kind[v.kind] = by_kind.get(v.kind, 0) + 1

    return {
        "ok": len(violations) == 0,
        "paper": paper,
        "violation_count": len(violations),
        "by_kind": by_kind,
        "violations": [v.to_dict() for v in violations],
    }


def audit_schematic_file(path: str | Path, *, check_wires: bool = True) -> dict[str, Any]:
    """Audit a .kicad_sch file and all of its hierarchical sub-sheets."""
    p = Path(path)
    root_text = p.read_text(encoding="utf-8")
    tree = loads(root_text)
    sheets = [p]
    for node in find_all(tree, "sheet"):
        for prop in find_all(node, "property"):
            if prop[1] in ("Sheetfile", "Sheet file") and prop[2]:
                sub = p.parent / str(prop[2])
                if sub.is_file() and sub not in sheets:
                    sheets.append(sub)
    per_sheet = {}
    total = 0
    for s in sheets:
        rep = audit_schematic_geometry(s.read_text(encoding="utf-8"), check_wires=check_wires)
        per_sheet[s.name] = {
            "ok": rep["ok"],
            "violation_count": rep["violation_count"],
            "by_kind": rep["by_kind"],
            "violations": rep["violations"][:25],
        }
        total += rep["violation_count"]
    hierarchy_violations: list[dict[str, Any]] = []
    if len(sheets) > 1 or "(sheet\n" in root_text or "(sheet " in root_text:
        hierarchy_violations = [v.to_dict() for v in audit_schematic_hierarchy(p)]
    total += len(hierarchy_violations)
    return {
        "ok": total == 0,
        "sheet_count": len(sheets),
        "violation_count": total,
        "hierarchy_violations": hierarchy_violations,
        "sheets": per_sheet,
    }


def _iter_top_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    n = len(text)
    i = text.find("(")
    i = text.find("(", i + 1)
    depth = 0
    in_str = False
    esc = False
    start = None
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append((start, i + 1, text[start : i + 1]))
                    start = None
        i += 1
    return blocks


def repair_schematic_text_overlaps(source: str, *, max_passes: int = 4) -> str:
    """Normalize rotated (90/270) Reference/Value texts to horizontal and relocate
    visible property texts that collide with wires, labels, or other symbols.

    Electrical connectivity is never altered because only non-electrical property
    text positions/angles are updated.
    """
    import re

    current = source
    for _ in range(max_passes):
        rep = audit_schematic_geometry(current, check_wires=True)
        bad_texts: set[tuple[str, str]] = set()
        for v in rep["violations"]:
            for side in (v["a"], v["b"]):
                if side.startswith("text:"):
                    body = side[5:]
                    if "." in body:
                        ref, rest = body.split(".", 1)
                        pname = rest.split("=", 1)[0]
                        if not ref.startswith("#"):
                            bad_texts.add((ref, pname))
        if not bad_texts:
            break

        blocks = _iter_top_blocks(current)
        # Collect obstacle boxes & wires from a quick parse so candidate positions avoid EVERYTHING.
        tree = loads(current)
        wires: list[tuple[float, float, float, float]] = []
        for node in tree:
            if isinstance(node, list) and node and node[0] == "wire":
                pts = find(node, "pts")
                if pts:
                    xys = find_all(pts, "xy")
                    if len(xys) >= 2:
                        wires.append((
                            float(xys[0][1]), float(xys[0][2]),
                            float(xys[1][1]), float(xys[1][2]),
                        ))
        # Parse lib bounds & existing item boxes
        lib_info: dict[str, tuple[BBox, BBox, bool]] = {}
        lib_syms = find(tree, "lib_symbols")
        if lib_syms:
            for sym in find_all(lib_syms, "symbol"):
                if isinstance(sym[1], str) and ":" in sym[1]:
                    lib_info[sym[1]] = _parse_lib_symbol(sym)
        obstacles: list[tuple[str, BBox]] = []
        for node in tree:
            if not isinstance(node, list) or not node:
                continue
            tag = node[0]
            if tag == "symbol":
                lid = value(node, "lib_id")
                at = find(node, "at")
                if not at or not lid:
                    continue
                sx0, sy0 = float(at[1]), float(at[2])
                ang0 = float(at[3]) if len(at) > 3 else 0.0
                ref0 = ""
                for prop in find_all(node, "property"):
                    if prop[1] == "Reference":
                        ref0 = str(prop[2])
                full_lb, _, _ = lib_info.get(
                    lid, ((-1.27, -1.27, 1.27, 1.27), (-1.27, -1.27, 1.27, 1.27), False)
                )
                corners = [
                    _rot_sym_pt(full_lb[0], full_lb[1], ang0),
                    _rot_sym_pt(full_lb[2], full_lb[1], ang0),
                    _rot_sym_pt(full_lb[0], full_lb[3], ang0),
                    _rot_sym_pt(full_lb[2], full_lb[3], ang0),
                ]
                obstacles.append((ref0, (
                    sx0 + min(c[0] for c in corners),
                    sy0 + min(c[1] for c in corners),
                    sx0 + max(c[0] for c in corners),
                    sy0 + max(c[1] for c in corners),
                )))
            elif tag in ("label", "global_label", "hierarchical_label"):
                at = find(node, "at")
                if at:
                    lx, ly = float(at[1]), float(at[2])
                    lang = float(at[3]) if len(at) > 3 else 0.0
                    eff = find(node, "effects")
                    just = "left"
                    if eff:
                        jn = find(eff, "justify")
                        if jn and len(jn) > 1:
                            just = str(jn[1])
                    fsize = _extract_font_size(eff, default=1.0)
                    obstacles.append(("__lbl__", _label_bbox(tag, str(node[1]), lx, ly, lang, just, fsize)))

        replacements: list[tuple[int, int, str]] = []
        for s, e, blk in blocks:
            if not (blk.startswith("(symbol\n") or blk.startswith("(symbol ")):
                continue
            if "(lib_id " not in blk:
                continue
            m_ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', blk)
            m_at = re.search(r"\(at\s+([-0-9.]+)\s+([-0-9.]+)(?:\s+([-0-9.]+))?\)", blk)
            if not m_ref or not m_at:
                continue
            ref = m_ref.group(1)
            sx, sy = float(m_at.group(1)), float(m_at.group(2))
            if not any(r == ref for r, _ in bad_texts):
                continue
            nblk = blk
            changed = False
            for r, pname in sorted(bad_texts):
                if r != ref:
                    continue
                pat = re.compile(
                    rf'(\(property\s+"{re.escape(pname)}"\s+"([^"]*)"\s+\(at\s+([-0-9.]+)\s+([-0-9.]+)\s+([-0-9.]+)\))',
                )
                m = pat.search(nblk)
                if not m:
                    continue
                val_str = m.group(2)
                ox, oy, oang = float(m.group(3)), float(m.group(4)), float(m.group(5))

                def _score_pos(cx: float, cy: float, cang: float) -> int:
                    tb = _text_bbox(val_str, cx, cy, cang, "center", 0.85)
                    hits = sum(1 for x1, y1, x2, y2 in wires if _seg_intersects_bbox(x1, y1, x2, y2, tb, pad=0.2))
                    hits += sum(1 for oref, ob in obstacles if oref != ref and _bbox_intersect(tb, ob, tol=0.2))
                    return hits

                best_score = _score_pos(ox, oy, oang)
                chosen = (ox, oy, oang)
                candidates = [
                    (sx, sy - 2.54 if pname == "Reference" else sy + 2.54, 0.0),
                    (sx, sy - 3.81 if pname == "Reference" else sy + 3.81, 0.0),
                    (sx, sy + 2.54 if pname == "Reference" else sy - 2.54, 0.0),
                    (sx, sy - 5.08 if pname == "Reference" else sy + 5.08, 0.0),
                    (sx + 3.81, sy - 2.54, 0.0),
                    (sx - 3.81, sy + 2.54, 0.0),
                    (sx + 3.81, sy + 2.54, 0.0),
                    (sx - 3.81, sy - 2.54, 0.0),
                ]
                for cx, cy, cang in candidates:
                    sc = _score_pos(cx, cy, cang)
                    if sc < best_score:
                        best_score = sc
                        chosen = (cx, cy, cang)
                        if sc == 0:
                            break
                if chosen != (ox, oy, oang):
                    rep_hdr = f'(property "{pname}" "{val_str}" (at {chosen[0]:.2f} {chosen[1]:.2f} {int(chosen[2])})'
                    nblk = nblk[: m.start(1)] + rep_hdr + nblk[m.end(1) :]
                    changed = True
            if changed:
                replacements.append((s, e, nblk))

        if not replacements:
            break
        parts = []
        cur = 0
        for s, e, rblk in sorted(replacements, key=lambda x: x[0]):
            parts.append(current[cur:s])
            parts.append(rblk)
            cur = e
        parts.append(current[cur:])
        current = "".join(parts)
    return current


def audit_schematic_hierarchy(root_sch_path: Path | str) -> list[GeometricViolation]:
    """Audit KiCad 8/9/10 hierarchical schematic metadata that triggers KiCad's
    auto-repair dialog (`sheetList.IsModified() || repairedPageNumbers`):
    - Root `(sheet_instances (path "/" (page "1")))` presence
    - Every `(sheet ...)` block in the root sheet has `(instances (project ... (path "/<root_uuid>" (page "<N>"))))`
    - No blank or duplicate sheet page numbers
    - No `(sheet_instances ...)` inside sub-sheets
    - No duplicate UUIDs or duplicate symbol references (`#PWR`, `#FLG`, parts) across the hierarchy
    - Sub-sheet symbol `(instances ... (path ...))` matches `/<root_uuid>/<sheet_box_uuid>`
    """
    import re

    root_path = Path(root_sch_path)
    root_text = root_path.read_text(encoding="utf-8")
    root_tree = loads(root_text)
    violations: list[GeometricViolation] = []

    root_uuid = value(root_tree, "uuid") or ""
    if not root_uuid:
        violations.append(GeometricViolation(
            "hierarchy_missing_root_uuid", root_path.name, "", (0.0, 0.0),
            "Root schematic is missing top-level (uuid ...)",
        ))

    sheet_inst = find(root_tree, "sheet_instances")
    if not sheet_inst:
        violations.append(GeometricViolation(
            "hierarchy_missing_sheet_instances", root_path.name, "", (0.0, 0.0),
            "Root schematic is missing (sheet_instances (path \"/\" (page \"1\")))",
        ))

    used_pages: dict[str, str] = {"1": root_path.name}
    all_uuids: dict[str, str] = {}
    all_refs: dict[str, str] = {}

    for u in re.findall(r'\(uuid\s+"([^"]+)"\)', root_text):
        if u in all_uuids:
            violations.append(GeometricViolation(
                "hierarchy_duplicate_uuid", root_path.name, all_uuids[u], (0.0, 0.0),
                f"Duplicate UUID {u}",
            ))
        else:
            all_uuids[u] = root_path.name

    for sh in find_all(root_tree, "sheet"):
        sh_uuid = value(sh, "uuid") or ""
        sh_name = ""
        sh_file = ""
        for prop in find_all(sh, "property"):
            if len(prop) >= 3:
                if prop[1] == "Sheetname":
                    sh_name = str(prop[2])
                elif prop[1] == "Sheetfile":
                    sh_file = str(prop[2])
        inst = find(sh, "instances")
        page_num = ""
        inst_path = ""
        if inst:
            proj_node = find(inst, "project")
            if proj_node:
                path_node = find(proj_node, "path")
                if path_node and len(path_node) >= 2:
                    inst_path = str(path_node[1])
                    page_num = str(value(path_node, "page") or "").strip()
        if not inst or not page_num:
            violations.append(GeometricViolation(
                "hierarchy_missing_sheet_page", sh_name or sh_file, root_path.name, (0.0, 0.0),
                f"Sheet '{sh_name}' ({sh_file}) is missing (instances (project ... (path \"/{root_uuid}\" (page \"N\"))))",
            ))
        else:
            if root_uuid and inst_path != f"/{root_uuid}":
                violations.append(GeometricViolation(
                    "hierarchy_bad_sheet_instance_path", sh_name or sh_file, root_path.name, (0.0, 0.0),
                    f"Sheet '{sh_name}' instance path '{inst_path}' != '/{root_uuid}'",
                ))
            if page_num in used_pages:
                violations.append(GeometricViolation(
                    "hierarchy_duplicate_page_number", sh_name or sh_file, used_pages[page_num], (0.0, 0.0),
                    f"Duplicate page number '{page_num}' on '{sh_name}' and '{used_pages[page_num]}'",
                ))
            else:
                used_pages[page_num] = sh_name or sh_file

        if not sh_file:
            continue
        sub_path = root_path.parent / sh_file
        if not sub_path.is_file():
            violations.append(GeometricViolation(
                "hierarchy_missing_subsheet_file", sh_name or sh_file, root_path.name, (0.0, 0.0),
                f"Referenced sub-sheet file does not exist: {sh_file}",
            ))
            continue

        sub_text = sub_path.read_text(encoding="utf-8")
        sub_tree = loads(sub_text)
        if find(sub_tree, "sheet_instances"):
            violations.append(GeometricViolation(
                "hierarchy_subsheet_has_sheet_instances", sub_path.name, root_path.name, (0.0, 0.0),
                f"Sub-sheet {sub_path.name} must not contain (sheet_instances ...)",
            ))
        for u in re.findall(r'\(uuid\s+"([^"]+)"\)', sub_text):
            if u in all_uuids:
                violations.append(GeometricViolation(
                    "hierarchy_duplicate_uuid", sub_path.name, all_uuids[u], (0.0, 0.0),
                    f"Duplicate UUID {u} across {sub_path.name} and {all_uuids[u]}",
                ))
            else:
                all_uuids[u] = sub_path.name

        expected_path = f"/{root_uuid}/{sh_uuid}" if (root_uuid and sh_uuid) else ""
        for sym in find_all(sub_tree, "symbol"):
            if not find(sym, "lib_id"):
                continue
            ref = ""
            for prop in find_all(sym, "property"):
                if len(prop) >= 3 and prop[1] == "Reference":
                    ref = str(prop[2])
                    break
            unit_str = str(value(sym, "unit") or "1")
            ref_key = f"{ref}#{unit_str}"
            if ref and ref_key in all_refs and all_refs[ref_key] != sub_path.name:
                violations.append(GeometricViolation(
                    "hierarchy_duplicate_ref", ref, f"{sub_path.name} vs {all_refs[ref_key]}", (0.0, 0.0),
                    f"Duplicate symbol reference {ref} (unit {unit_str}) across sheets",
                ))
            elif ref:
                all_refs[ref_key] = sub_path.name

            sinst = find(sym, "instances")
            if expected_path and sinst:
                sproj = find(sinst, "project")
                spath = find(sproj, "path") if sproj else None
                actual = str(spath[1]) if (spath and len(spath) >= 2) else ""
                if actual != expected_path:
                    violations.append(GeometricViolation(
                        "hierarchy_bad_symbol_path", ref or sub_path.name, sub_path.name, (0.0, 0.0),
                        f"Symbol {ref} instance path '{actual}' != '{expected_path}'",
                    ))
    return violations


def repair_hierarchical_sheet_instances(root_sch_path: Path | str, *, project_name: str | None = None) -> bool:
    """Ensure every `(sheet ...)` block in a root hierarchical schematic has a valid
    KiCad 8/9/10 `(instances (project "<name>" (path "/<root_uuid>" (page "<N>"))))`
    with unique, non-blank page numbers so KiCad opens the hierarchy with zero
    auto-repair dialogs. Returns True if the file was modified.
    """
    import re

    root_path = Path(root_sch_path)
    text = root_path.read_text(encoding="utf-8")
    tree = loads(text)
    root_uuid = value(tree, "uuid")
    if not root_uuid:
        return False
    proj = project_name or root_path.stem

    blocks = _iter_top_blocks(text)
    sheet_entries: list[tuple[int, int, str, str]] = []
    for s, e, blk in blocks:
        if blk.startswith("(sheet\n") or blk.startswith("(sheet "):
            m_name = re.search(r'\(property\s+"Sheetname"\s+"([^"]*)"', blk)
            sh_name = m_name.group(1) if m_name else ""
            sheet_entries.append((s, e, blk, sh_name))
    if not sheet_entries:
        return False

    # Assign deterministic unique page numbers starting at 2, respecting numeric prefixes like "02_MCU"
    def _sort_key(item: tuple[int, tuple[int, int, str, str]]) -> tuple[int, int, str]:
        idx, (_, _, _, name) = item
        m = re.match(r"^(\d+)", name)
        return (0, int(m.group(1)), name) if m else (1, idx, name)

    ordered = sorted(enumerate(sheet_entries), key=_sort_key)
    page_by_idx: dict[int, str] = {}
    for page_i, (orig_idx, _) in enumerate(ordered, start=2):
        page_by_idx[orig_idx] = str(page_i)

    replacements: list[tuple[int, int, str]] = []
    for idx, (s, e, blk, _name) in enumerate(sheet_entries):
        page_num = page_by_idx[idx]
        inst_block = (
            f'\t\t(instances\n'
            f'\t\t\t(project "{proj}"\n'
            f'\t\t\t\t(path "/{root_uuid}"\n'
            f'\t\t\t\t\t(page "{page_num}")\n'
            f'\t\t\t\t)\n'
            f'\t\t\t)\n'
            f'\t\t)\n'
        )
        if "(instances" in blk:
            # Replace existing instances block if path/page is missing or wrong
            m_p = re.search(r'\(page\s+"([^"]*)"\)', blk)
            m_path = re.search(r'\(path\s+"([^"]*)"', blk)
            if m_p and m_p.group(1).strip() and m_path and m_path.group(1) == f"/{root_uuid}":
                continue
            new_blk = re.sub(r"\t\t\(instances\b.*?\n\t\t\)\n", inst_block, blk, flags=re.S)
        else:
            # Insert right before the closing `\t)` of the sheet block
            close_pos = blk.rfind("\n\t)")
            if close_pos == -1:
                close_pos = blk.rfind(")")
                new_blk = blk[:close_pos] + "\n" + inst_block + "\t)"
            else:
                new_blk = blk[:close_pos] + "\n" + inst_block + "\t)"
        if new_blk != blk:
            replacements.append((s, e, new_blk))

    if not replacements:
        return False

    parts: list[str] = []
    cur = 0
    for s, e, rblk in sorted(replacements, key=lambda x: x[0]):
        parts.append(text[cur:s])
        parts.append(rblk)
        cur = e
    parts.append(text[cur:])
    root_path.write_text("".join(parts), encoding="utf-8")
    return True
