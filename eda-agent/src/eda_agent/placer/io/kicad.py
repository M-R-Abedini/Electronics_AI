# SPDX-License-Identifier: MIT
"""KiCad ``.kicad_pcb`` adapter for the Board IR (KiCad 6 .. 10 formats).

Reading needs no KiCad installation.  Writing patches only the footprint
``(at ...)`` nodes, pad / text angles, and optionally removes routed copper, so
every other byte of the original file is preserved.
"""

from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path

from ..geometry import Point, arc_points, box_of, norm_angle, transform
from ..ir import Board, Footprint, Pad, Rules
from ..sexpr import Node, fmt, parse

_COURTYARD = {"F.CrtYd", "B.CrtYd"}
_GRAPHIC_TAGS = ("line", "rect", "arc", "circle", "poly")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def _shape_points(node: Node) -> list[Point]:
    """Points describing a graphic item (fp_* or gr_*), in its own frame."""
    kind = node.tag.split("_", 1)[1]
    if kind == "line":
        return [tuple(node.floats("start")), tuple(node.floats("end"))]  # type: ignore[list-item]
    if kind == "rect":
        x1, y1 = node.floats("start")
        x2, y2 = node.floats("end")
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    if kind == "arc":
        return arc_points(tuple(node.floats("start")), tuple(node.floats("mid")), tuple(node.floats("end")))  # type: ignore[arg-type]
    if kind == "circle":
        cx, cy = node.floats("center")
        ex, ey = node.floats("end")
        r = math.hypot(ex - cx, ey - cy)
        return [(cx + r * math.cos(2 * math.pi * i / 24), cy + r * math.sin(2 * math.pi * i / 24)) for i in range(25)]
    if kind == "poly":
        pts_node = node.first("pts")
        out: list[Point] = []
        if pts_node is not None:
            for item in pts_node.children():
                if item.tag == "xy":
                    out.append((float(item[1]), float(item[2])))
                elif item.tag == "arc":
                    out += arc_points(tuple(item.floats("start")), tuple(item.floats("mid")), tuple(item.floats("end")))  # type: ignore[arg-type]
        if out:
            out.append(out[0])
        return out
    return []


def _segments(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(pairwise(points))


def _chain(segments: list[tuple[Point, Point]], tol: float = 0.01) -> list[list[Point]]:
    """Join loose outline segments into closed polygons (best effort)."""
    remaining = [s for s in segments if s[0] != s[1]]
    loops: list[list[Point]] = []

    def close(a: Point, b: Point) -> bool:
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    while remaining:
        a, b = remaining.pop()
        loop = [a, b]
        progressed = True
        while progressed and not close(loop[0], loop[-1]):
            progressed = False
            for i, (p, q) in enumerate(remaining):
                if close(loop[-1], p):
                    loop.append(q)
                elif close(loop[-1], q):
                    loop.append(p)
                else:
                    continue
                remaining.pop(i)
                progressed = True
                break
        if len(loop) >= 4 and close(loop[0], loop[-1]):
            loops.append(loop[:-1])
    loops.sort(key=lambda poly: -abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))))
    return loops


def _read_rules(pcb_path: Path) -> Rules:
    rules = Rules()
    pro = pcb_path.with_suffix(".kicad_pro")
    if not pro.exists():
        return rules
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rules
    classes = (data.get("net_settings") or {}).get("classes") or []
    default = next((c for c in classes if c.get("name") == "Default"), classes[0] if classes else {})
    for attr, key in (("clearance", "clearance"), ("track_width", "track_width"),
                      ("via_diameter", "via_diameter"), ("via_drill", "via_drill")):
        value = default.get(key)
        if isinstance(value, (int, float)) and value > 0:
            setattr(rules, attr, float(value))
    design = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    cy = design.get("min_courtyard_clearance") if isinstance(design, dict) else None
    if isinstance(cy, (int, float)) and cy >= 0:
        rules.courtyard_clearance = float(cy)
    return rules


def _read_pad(node: Node, fp_rotation: float) -> Pad:
    number = str(node[1]) if len(node) > 1 else ""
    kind = str(node[2]) if len(node) > 2 else "smd"
    shape = str(node[3]) if len(node) > 3 else "rect"
    at = node.floats("at") or [0.0, 0.0]
    size = node.floats("size") or [0.0, 0.0]
    if len(size) == 1:
        size = [size[0], size[0]]
    layers_node = node.first("layers")
    layers = tuple(str(v) for v in layers_node[1:]) if layers_node is not None else ()
    copper = []
    for name in layers:
        if name in ("*.Cu", "F&B.Cu"):
            copper += ["F.Cu", "B.Cu"] if name == "F&B.Cu" else ["*.Cu"]
        elif name.endswith(".Cu"):
            copper.append(name)
    net_node = node.first("net")
    net = None
    if net_node is not None and len(net_node) >= 2:
        net = str(net_node[-1]) or None  # KiCad <=9: (net 3 "N"); KiCad 10 may write (net "N")
    drill_node = node.first("drill")
    drill = None
    if drill_node is not None:
        nums = [float(v) for v in drill_node[1:] if isinstance(v, str) and v.replace(".", "", 1).replace("-", "", 1).isdigit()]
        drill = nums[0] if nums else None
    absolute_angle = at[2] if len(at) > 2 else 0.0
    return Pad(
        number=number,
        net=net,
        local=(at[0], at[1]),
        size=(size[0], size[1]),
        angle=norm_angle(absolute_angle - fp_rotation),
        shape=shape,
        kind=kind,
        layers=tuple(copper),
        drill=drill,
        function=node.value("pinfunction"),
        pin_type=node.value("pintype"),
    )


def _read_footprint(node: Node) -> tuple[Footprint, list[tuple[Point, Point]]]:
    at = node.floats("at") or [0.0, 0.0]
    rotation = norm_angle(at[2] if len(at) > 2 else 0.0)
    layer = str(node.value("layer", default="F.Cu"))
    ref = value = descr = ""
    for prop in node.children("property"):
        if len(prop) >= 3 and prop[1] == "Reference":
            ref = str(prop[2])
        elif len(prop) >= 3 and prop[1] == "Value":
            value = str(prop[2])
        elif len(prop) >= 3 and prop[1] == "Description":
            descr = str(prop[2])
    for text in node.children("fp_text"):  # KiCad 6/7
        if len(text) >= 3 and text[1] == "reference" and not ref:
            ref = str(text[2])
        elif len(text) >= 3 and text[1] == "value" and not value:
            value = str(text[2])
    attr_node = node.first("attr")
    attributes = tuple(str(v) for v in attr_node[1:]) if attr_node is not None else ()
    locked = any(item == "locked" for item in node[1:3]) or node.value("locked") == "yes"
    pads = [_read_pad(p, rotation) for p in node.children("pad")]
    court_pts: list[Point] = []
    edges: list[tuple[Point, Point]] = []
    for child in node.children():
        tag = child.tag or ""
        if not tag.startswith("fp_") or tag[3:] not in _GRAPHIC_TAGS:
            continue
        lay = str(child.value("layer", default=""))
        if lay in _COURTYARD:
            court_pts += _shape_points(child)
        elif lay == "Edge.Cuts":
            pts = [transform(p, (at[0], at[1]), rotation) for p in _shape_points(child)]
            edges += _segments(pts)
    fp = Footprint(
        ref=ref,
        value=value,
        lib_id=str(node[1]) if len(node) > 1 else "",
        x=at[0],
        y=at[1],
        rotation=rotation,
        side="B" if layer.startswith("B.") else "F",
        locked=locked,
        pads=pads,
        courtyard_local=box_of(court_pts),
        uuid=node.value("uuid"),
        sheet=node.value("sheetname") or node.value("sheetfile"),
        description=descr,
        attributes=attributes,
    )
    return fp, edges


def read_board(path: str | Path) -> Board:
    path = Path(path)
    root = parse(path.read_text(encoding="utf-8"))
    if root.tag != "kicad_pcb":
        raise ValueError(f"{path} is not a .kicad_pcb file")
    copper = []
    layers = root.first("layers")
    if layers is not None:
        for entry in layers.children():
            if len(entry) >= 3 and str(entry[1]).endswith(".Cu") and str(entry[2]) in ("signal", "power", "mixed", "jumper"):
                copper.append(str(entry[1]))
    footprints: list[Footprint] = []
    edges: list[tuple[Point, Point]] = []
    for node in root.children("footprint"):
        fp, fp_edges = _read_footprint(node)
        footprints.append(fp)
        edges += fp_edges
    for node in root.children():
        tag = node.tag or ""
        if tag.startswith("gr_") and tag[3:] in _GRAPHIC_TAGS and node.value("layer") == "Edge.Cuts":
            edges += _segments(_shape_points(node))
    _dedupe_refs(footprints)
    return Board(
        footprints=footprints,
        outline=_chain(edges),
        copper_layers=tuple(copper) or ("F.Cu", "B.Cu"),
        rules=_read_rules(path),
        source_path=str(path),
        name=path.stem,
    )


def _dedupe_refs(footprints: list[Footprint]) -> None:
    seen: dict[str, int] = {}
    for fp in footprints:
        if not fp.ref:
            fp.ref = "?"
        count = seen.get(fp.ref, 0)
        seen[fp.ref] = count + 1
        if count:
            fp.ref = f"{fp.ref}#{count}"


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
_ROUTING_TAGS = {"segment", "arc", "via"}


def write_board(src: str | Path, board: Board, dst: str | Path, *, strip_routing: bool = False,
                strip_zones: bool = False) -> Path:
    """Write ``board``'s placement into a copy of ``src``.

    Only the footprints' ``(at)`` nodes and the absolute angles of their pads
    and texts are changed.  ``strip_routing`` removes tracks/arcs/vias so the
    result can be used for unbiased trial routing.
    """
    src, dst = Path(src), Path(dst)
    text = src.read_text(encoding="utf-8")
    root = parse(text)
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    nodes = list(root.children("footprint"))
    parsed = [_read_footprint(n)[0] for n in nodes]
    _dedupe_refs(parsed)
    for node, original in zip(nodes, parsed):
        try:
            target = board.fp(original.ref)
        except KeyError:
            continue
        if (abs(target.x - original.x) < 1e-9 and abs(target.y - original.y) < 1e-9
                and abs(norm_angle(target.rotation - original.rotation)) < 1e-9):
            continue
        delta = norm_angle(target.rotation - original.rotation)
        at = node.first("at")
        edits.append((at.start, at.end, _at_text("at", target.x, target.y, target.rotation)))
        if delta:
            for child in node.children():
                if child.tag in ("pad", "property", "fp_text"):
                    sub = child.first("at")
                    if sub is None:
                        continue
                    vals = [float(v) for v in sub[1:] if _is_number(v)]
                    if len(vals) < 2:
                        continue
                    ang = norm_angle((vals[2] if len(vals) > 2 else 0.0) + delta)
                    keep = [str(v) for v in sub[1:] if not _is_number(v)]  # e.g. "unlocked"
                    edits.append((sub.start, sub.end, _at_text("at", vals[0], vals[1], ang, keep)))
    if strip_routing or strip_zones:
        tags = (_ROUTING_TAGS if strip_routing else set()) | ({"zone"} if strip_zones else set())
        for child in root.children():
            if child.tag in tags:
                start = text.rfind("\n", 0, child.start) + 1
                if text[start:child.start].strip():
                    start = child.start
                end = child.end
                if text[end:end + 2] == "\r\n":
                    end += 2
                elif text[end:end + 1] == "\n":
                    end += 1
                edits.append((start, end, ""))
    edits.sort(key=lambda e: e[0], reverse=True)
    out = text
    for start, end, repl in edits:
        out = out[:start] + repl + out[end:]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, encoding="utf-8", newline="")
    return dst


def _is_number(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def _at_text(tag: str, x: float, y: float, rot: float, extra: list[str] | None = None) -> str:
    parts = [tag, fmt(x), fmt(y)]
    rot = norm_angle(rot)
    if rot:
        parts.append(fmt(rot))
    parts += extra or []
    return "(" + " ".join(parts) + ")"
