# SPDX-License-Identifier: MIT
"""Mechanical anchors resolved before the electrical placement stages."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, replace

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry import box as shapely_box
from shapely.ops import nearest_points, unary_union

from ..eval.legality import _pad_polygon, board_polygon, courtyard_polygon
from ..geometry import rotated_box, transform
from ..intent import model
from ..ir import Board

# Distances here establish whether an authored pose is plausibly edge-accessible.
# Relocations require a dimensioned access envelope, supplied by the design.
_PROFILES = (
    ("usb", "edge", 4, r"\busb(?: ?[abc]| ?[234])?\b|\btype ?c\b"),
    ("card", "edge", 5, r"\b(?:micro ?sd|sd ?card|tf ?(?:card|smd|socket|110)|sim ?card|sim ?socket)\b"),
    ("ethernet", "edge", 6, r"\brj ?45\b|\bmagjack\b|\bhr ?91\d+[a-z]*\b|\bethernet (?:jack|connector)\b"),
    ("external_jack", "edge", 5, r"\b(?:bnc|hdmi|displayport|sma|rp sma|db ?9|dsub|barrel jack|audio jack|power jack|pwrj)\b"),
    ("terminal", "service", 8, r"\b(?:terminal ?block|screw terminal|phoenix contact|xt30|xt60|tb\d+|tb ?[25]mm)\b"),
    ("actuator", "normal", 5, r"\b(?:tact(?:ile)?|push ?button|slide switch|toggle switch|switch|ts ?1088)\b"),
    ("flex", "internal", 3, r"\b(?:fpc|ffc|zif)\b"),
    ("mezzanine", "internal", 0, r"\b(?:board to board|mezzanine|sodimm|sodimm socket|pmod)\b"),
    ("header", "internal", 2, r"\b(?:pin ?header|pin ?socket|box ?header|swd|jtag|tag ?connect|hn[12]x\d+)\b"),
    ("wire", "internal", 2, r"\b(?:jst|molex|jst ph|jst sh|wire to board)\b"),
)
_CLASSIFIERS = tuple((kind, mode, margin, re.compile(pattern)) for kind, mode, margin, pattern in _PROFILES)
POSE_MM = 0.0001
POSE_DEG = 0.001


def _words(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def interface_kind(fp, role: str | None = None) -> tuple[str, str, float] | None:
    """Use physical package evidence first and schematic identity as corroboration."""
    physical = _words(f"{fp.lib_id} {fp.description}")
    identity = _words(fp.value)
    if role == "test_point" or re.search(r"\btest ?points?\b", _words(fp.lib_id)):
        return None
    for kind, mode, margin, pattern in _CLASSIFIERS:
        actual = pattern.search(physical)
        # SMA is also the JEDEC DO-214AC diode package.
        if (kind == "external_jack" and actual and actual[0] == "sma"
                and re.search(r"\b(?:diodes?|do ?214[a-z]*|rectifier)\b", physical)):
            continue
        connector = kind != "actuator" and (role == "connector" or re.search(r"\b(?:conn|connectors?|socket)\b", physical))
        if actual or (connector and pattern.search(identity)):
            if kind == "usb" and re.search(r"\b(?:vertical|top entry)\b", physical):
                mode = "normal"
            return kind, mode, margin
    if role in ("connector", "switch") or re.search(r"\b(?:connectors?|conn|socket)\b", physical):
        return ("connector", "internal", 2) if role != "switch" else ("actuator", "normal", 5)
    if fp.prefix in ("J", "P", "CN", "CON", "USB") and len(fp.pads) >= 2:
        return "unclassified_connector", "internal", 1.5
    return None


def interface_inventory(board: Board, intent: dict | None = None, *, outline=None) -> list[dict]:
    """Resolve which source poses should be retained and the limits of that evidence."""
    if outline is None and board.outline_valid:
        outline = board_polygon(board)
    valid = outline is not None and not outline.is_empty
    intent = intent or {}
    claims = intent.get("mechanical", {})
    parts = intent.get("parts", {})
    rows = []
    for fp in board.footprints:
        role = ((parts.get(fp.ref) or {}).get("role") or {}).get("value")
        kind_mode = interface_kind(fp, role)
        if kind_mode is None and fp.ref not in claims:
            continue
        kind, mode, margin = kind_mode or ("declared", "internal", 0)
        shape = courtyard_polygon(fp)
        if fp.courtyard.source == "derived" and fp.body_bounds_mm:
            body_poly = shapely_box(*fp.body_bounds_mm)
            shape = body_poly if shape is None else shape.union(body_poly)
        usable = valid and shape is not None and shape.intersection(outline).area > 0
        distance = shape.distance(outline.boundary) if usable else None
        near = distance is not None and distance <= margin + 1e-8
        value = (claims.get(fp.ref) or {}).get("value", {})
        scope = value.get("mode")
        protected = bool(fp.locked or scope or mode in ("edge", "normal", "service") or near)
        unknown = protected and (not usable or (mode == "edge" and not near and not value.get("access")))
        datum = None
        if usable and near and mode != "normal":
            _, edge_point = nearest_points(shape, outline.boundary)
            datum = [round(edge_point.x, 6), round(edge_point.y, 6)]
        rows.append({
            "ref": fp.ref, "uuid": fp.uuid, "kind": kind, "access_type": mode, "protected": protected,
            "status": "needs_evidence" if unknown else "source_pose" if protected else "internal",
            "reason": "outline or reference seating is unverified" if unknown else
                      f"{kind} package and reference placement" if protected else "interior interface",
            "pose": [fp.x, fp.y, fp.rotation, fp.side], "edge_datum_mm": datum,
            "edge_distance_mm": round(distance, 4) if distance is not None else None,
            "access": value.get("access"),
        })
    return rows


def same_pose(a, b) -> bool:
    return (a.side == b.side and math.dist(a.position, b.position) <= POSE_MM
            and abs((a.rotation - b.rotation + 180) % 360 - 180) <= POSE_DEG)


def access_geometry(fp, access: dict):
    def world(p):
        return transform(tuple(p), fp.position, fp.rotation)

    if access["kind"] == "normal":
        return Point(world(access["center_mm"])).buffer(access["diameter_mm"] / 2)
    return LineString([world(access["entry_mm"]), world(access["exit_mm"])]).buffer(
        access["width_mm"] / 2, cap_style="flat")


def _fits_access(fp, access, outline, occupied, keepout):
    corridor = access_geometry(fp, access)
    if corridor.is_empty or corridor.intersection(keepout).area > 1e-6:
        return False
    if access["kind"] == "planar":
        entry = Point(transform(tuple(access["entry_mm"]), fp.position, fp.rotation))
        exit_point = Point(transform(tuple(access["exit_mm"]), fp.position, fp.rotation))
        ray = LineString([entry, exit_point])
        # One crossing from substrate to free space permits an exterior edge or a real cutout.
        crossing = ray.intersection(outline.boundary)
        if not outline.covers(entry) or outline.covers(exit_point) or crossing.geom_type != "Point":
            return False
    return all(corridor.intersection(shape).area <= 1e-6 for shape in occupied if shape is not None)


def _area(row: dict):
    return unary_union([Polygon(ring).buffer(0) for ring in row["polygons"] if len(ring) >= 3])


def evidence(board: Board, intent: dict, refs: set[str] | None = None) -> dict:
    """Machine-readable facts for selecting constraints with a project or enclosure drawing."""
    from .problem import fixed_parts

    outline = board_polygon(board)
    fixed = fixed_parts(board, intent)
    parts = intent.get("parts", {})
    inventory = {row["ref"]: row for row in interface_inventory(board, intent, outline=outline)}
    refs = refs if refs is not None else set(inventory)
    dependents: dict[str, list[dict]] = {}
    interface_pairs: dict[str, list[dict]] = {}
    pads_by_net = board.net_pads()
    for pair in intent.get("diff_pairs", []):
        value = pair["value"]
        pos, neg = ({fp.ref for fp, _ in pads_by_net.get(value[key], [])} for key in ("positive", "negative"))
        for ref in pos & neg & refs:
            interface_pairs.setdefault(ref, []).append(pair)
    for ref, part in parts.items():
        claim = part.get("target") or {}
        if dest := claim.get("value", {}).get("ref"):
            dependents.setdefault(dest, []).append({"ref": ref, "target": claim})
    return {
        "units": {"length": "mm", "angle": "degree, screen CCW"},
        "source": board.source,
        "coordinate_frame": "+X right, +Y down, board file origin; pad and courtyard coordinates are footprint-local",
        "stackup": asdict(board.stackup),
        "routing_rules": board.rules,
        "netclasses": [asdict(row) for row in board.netclasses],
        "outline_valid": board.outline_valid,
        "outline": [asdict(row) for row in board.outline],
        "rule_areas": board.rule_areas,
        "mounts": [{"ref": fp.ref, "position_mm": fp.position, "courtyard": asdict(fp.courtyard),
                    "drills": [p.drill for p in fp.pads if p.drill]}
                   for fp in board.footprints if any(p.kind == "np_thru_hole" for p in fp.pads)],
        "interfaces": [
            {"ref": fp.ref, "library": fp.lib_id, "value": fp.value, "sheet": fp.sheet,
             "pose": {"position_mm": fp.position, "angle_deg": fp.rotation, "side": fp.side},
             "distance_to_edge_mm": round(shape.distance(outline.boundary), 4) if outline and shape else None,
             "courtyard": asdict(fp.courtyard), "pads": [asdict(p) for p in fp.pads],
             "fixed_reason": fixed.get(fp.ref), "role": parts.get(fp.ref, {}).get("role"),
             "diff_pairs": interface_pairs.get(fp.ref, []),
             "dependents": dependents.get(fp.ref, []), "constraint": intent.get("mechanical", {}).get(fp.ref),
             "interface": inventory.get(fp.ref),
             "unknown": (["body_height"] if inventory.get(fp.ref, {}).get("access")
                         else ["external_access", "mating_direction", "cable_insertion_volume", "body_height"])}
            for fp in board.footprints if fp.ref in refs
            for shape in [courtyard_polygon(fp)]
        ],
    }


def anchor(board: Board, intent: dict) -> tuple[Board, list[dict]]:
    """Find legal mechanical poses deterministically, then hold them fixed through S3-S5."""
    from .problem import fixed_parts

    constraints = intent.get("mechanical", {})
    if not constraints:
        return board, []
    placed = board.copy()
    outline = board_polygon(board) if board.outline_valid else None
    fixed = fixed_parts(board, intent)
    areas = {area["uuid"]: area for area in board.rule_areas}
    names = {layer.name: layer.canonical for layer in board.copper_layers}
    keepouts = {
        side: unary_union([_area(area) for area in board.rule_areas if area["keepout"].get("footprints")
                           and any(names.get(layer, layer) == f"{side}.Cu" for layer in area["layers"])])
        for side in ("F", "B")
    }
    preserve = {ref for ref, c in constraints.items() if c["value"]["mode"] == "preserve"}
    occupied = {ref: courtyard_polygon(board.fp(ref)) for ref in (set(fixed) - set(constraints)) | preserve
                | board.fixed_refs()}
    clearance = max(board.rules.get("courtyard_clearance", 0.0), 0.0)
    results = []
    for ref, claim in sorted(constraints.items(), key=lambda item: model.natural_key(item[0])):
        if not model.is_hard(claim) or claim["source"]["kind"] not in ("user", "board", "datasheet"):
            raise ValueError(f"mechanical/{ref}: missing authoritative evidence")
        fp = placed.fp(ref)
        value = claim["value"]
        access = value.get("access")
        overhang = float(access.get("max_overhang_mm", 0)) if access else 0.0
        allowed = outline
        region = areas.get(value.get("region"))
        if value["mode"] == "region":
            if (not region or region["keepout"].get("footprints")
                    or not any(names.get(layer, layer) == f"{fp.side}.Cu" for layer in region["layers"])):
                raise ValueError(f"mechanical/{ref}: missing usable placement region")
            allowed = outline.buffer(overhang).intersection(_area(region)) if outline is not None else None
        kind_mode = interface_kind(fp)
        if value["mode"] == "region" and kind_mode and kind_mode[1] in ("edge", "normal", "service") and not access:
            raise ValueError(f"mechanical/{ref}: relocation requires a dimensioned mating envelope")
        if access and kind_mode and kind_mode[1] == "edge" and access["kind"] != "planar":
            raise ValueError(f"mechanical/{ref}: edge mating requires a planar envelope")
        if allowed is None or allowed.is_empty or not fp.courtyard.polygons:
            raise ValueError(f"mechanical/{ref}: valid board, region and courtyard geometry required")

        candidates = [fp] if value["mode"] == "preserve" or fp.locked else _candidates(fp, allowed.bounds, value["angles_deg"])
        if value["mode"] == "region" and fp.locked and fp.rotation % 360 not in value["angles_deg"]:
            raise ValueError(f"mechanical/{ref}: locked orientation conflicts with the region constraint")
        chosen = None
        for candidate in candidates:
            shape = courtyard_polygon(candidate)
            neighbors = [geom for other, geom in occupied.items() if other != ref and placed.fp(other).side == fp.side]
            if shape is None or shape.intersection(keepouts[fp.side]).area > 1e-6:
                continue
            if value["mode"] == "region":
                if not allowed.covers(shape):
                    continue
                if access and any(not outline.covers(poly) for pad in candidate.pads
                                  if (poly := _pad_polygon(candidate, pad)) is not None):
                    continue
            elif not outline.intersects(shape):
                continue
            if access and not _fits_access(candidate, access, outline, neighbors, keepouts[fp.side]):
                continue
            if any(geom is not None and (shape.distance(geom) < clearance - 1e-8
                                        or shape.intersection(geom).area > 1e-6) for geom in neighbors):
                continue
            chosen = candidate
            occupied[ref] = shape
            break
        if chosen is None:
            raise ValueError(f"mechanical/{ref}: no legal pose among the sampled positions and allowed orientations")
        fp.x, fp.y, fp.rotation, fp.locked = chosen.x, chosen.y, chosen.rotation, True
        results.append({"ref": ref, "mode": value["mode"], "position_mm": list(fp.position),
                        "angle_deg": fp.rotation, "side": fp.side, "source": claim["source"],
                        **({"region": value["region"]} if value["mode"] == "region" else {})})
    return placed, results


def _candidates(fp, bounds, angles):
    from .problem import local_box

    x0, y0, x1, y1 = bounds
    center = ((x0 + x1) / 2, (y0 + y1) / 2)
    candidates = []
    for angle in sorted(angles):
        a, b, c, d = rotated_box(local_box(fp), (0, 0), angle)
        lo_x, hi_x = x0 - a, x1 - c
        lo_y, hi_y = y0 - b, y1 - d
        if lo_x > hi_x or lo_y > hi_y:
            continue
        xs = [lo_x + (hi_x - lo_x) * i / 16 for i in range(17)]
        ys = [lo_y + (hi_y - lo_y) * i / 16 for i in range(17)]
        candidates.extend(replace(fp, x=x, y=y, rotation=angle) for x in xs for y in ys)
    return sorted(candidates, key=lambda p: (math.dist(p.position, center), p.rotation, p.x, p.y))
