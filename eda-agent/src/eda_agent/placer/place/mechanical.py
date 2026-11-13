# SPDX-License-Identifier: MIT
"""Mechanical anchors resolved before the electrical placement stages."""

from __future__ import annotations

import math
from dataclasses import asdict, replace

from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..eval.legality import board_polygon, courtyard_polygon
from ..geometry import rotated_box
from ..intent import model
from ..ir import Board
from .problem import fixed_parts, local_box


def _area(row: dict):
    return unary_union([Polygon(ring).buffer(0) for ring in row["polygons"] if len(ring) >= 3])


def evidence(board: Board, intent: dict, refs: set[str] | None = None) -> dict:
    """Machine-readable facts for selecting constraints with a project or enclosure drawing."""
    outline = board_polygon(board)
    fixed = fixed_parts(board)
    parts = intent.get("parts", {})
    refs = refs if refs is not None else {ref for ref, part in parts.items() if part["role"]["value"] == "connector"}
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
             "unknown": ["external_access", "mating_direction", "cable_insertion_volume", "body_height"]}
            for fp in board.footprints if fp.ref in refs
            for shape in [courtyard_polygon(fp)]
        ],
    }


def anchor(board: Board, intent: dict) -> tuple[Board, list[dict]]:
    """Find legal mechanical poses deterministically, then hold them fixed through S3–S5."""
    constraints = intent.get("mechanical", {})
    if not constraints:
        return board, []
    placed = board.copy()
    outline = board_polygon(board) if board.outline_valid else None
    fixed = fixed_parts(board)
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
        allowed = outline
        region = areas.get(value.get("region"))
        if value["mode"] == "region":
            if (not region or region["keepout"].get("footprints")
                    or not any(names.get(layer, layer) == f"{fp.side}.Cu" for layer in region["layers"])):
                raise ValueError(f"mechanical/{ref}: missing usable placement region")
            allowed = outline.intersection(_area(region)) if outline else None
        if allowed is None or allowed.is_empty or not fp.courtyard.polygons:
            raise ValueError(f"mechanical/{ref}: valid board, region and courtyard geometry required")

        candidates = [fp] if value["mode"] == "preserve" or fp.locked else _candidates(fp, allowed.bounds, value["angles_deg"])
        if value["mode"] == "region" and fp.locked and fp.rotation % 360 not in value["angles_deg"]:
            raise ValueError(f"mechanical/{ref}: locked orientation conflicts with the region constraint")
        chosen = None
        for candidate in candidates:
            shape = courtyard_polygon(candidate)
            if shape is None or not allowed.covers(shape) or shape.intersection(keepouts[fp.side]).area > 1e-6:
                continue
            if any(other != ref and geom is not None and placed.fp(other).side == fp.side
                   and (shape.distance(geom) < clearance - 1e-8 or shape.intersection(geom).area > 1e-6)
                   for other, geom in occupied.items()):
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
