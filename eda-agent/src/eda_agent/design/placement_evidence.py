# SPDX-License-Identifier: MIT
"""Deterministic, evidence-bearing review of existing placement proposals.

This module qualifies proposals from the existing placer; it is not another
placer or a field solver. Geometry is in mm and poses refer to component
origins. A missing model never supplies a zero distance or a clean result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from itertools import chain

from eda_agent.design import copper_geometry as geom
from eda_agent.placement.autoplace import _rotate_offset

POLICY_VERSION = "placement-evidence-p0-v3"
STATUSES = {"pass", "fail", "unknown", "not_applicable"}
PROFILE_ROLES = {
    "precision_analog": {"precision_analog", "sensitive_analog", "analog_front_end"},
    "digital_highspeed": {"high_speed", "high_speed_diff", "high_speed_single"},
    "switching_power": {"switching_power", "switching_converter", "gate_driver"},
    "isolation": {"isolation", "isolation_barrier"},
}
PROFILE_INPUTS = {
    "precision_analog": ("sensitive_pin_bindings", "return_intent"),
    "digital_highspeed": ("interface_pin_bindings", "reference_intent", "timing_requirements"),
    "switching_power": ("switching_states", "commutation_pin_loops", "return_intent"),
    "isolation": ("isolation_requirements", "barriers"),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _number(value, label, *, positive=False, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    value = float(value)
    if positive and value <= 0 or minimum is not None and value < minimum:
        raise ValueError(f"{label} outside its valid range")
    return value


def _point(value, label="point"):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain XY in mm")
    return tuple(_number(v, label) for v in value)


def _ring(value, label):
    if not isinstance(value, (list, tuple)) or not 3 <= len(value) <= 10000:
        raise ValueError(f"{label} requires a bounded polygon")
    points = [_point(p, label) for p in value]
    if not geom.region_geometry_valid({"outline": points}):
        raise ValueError(f"{label} is collapsed or self-intersecting")
    return points


def _qualified(source):
    if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"]:
        return False
    kind = source.get("kind")
    if kind not in {"engineering_policy", "user_intent", "schematic", "datasheet", "standard", "measured_geometry"}:
        return False
    return kind not in {"datasheet", "standard"} or bool(source.get("revision"))


def transform_point(local_xy_mm, pose):
    """Mirror local X on bottom, then rotate and translate the actual origin."""
    x, y = _point(local_xy_mm)
    side = pose.get("side")
    if side not in {"top", "bottom"}:
        raise ValueError("Explicit component side required for a local transform")
    dx, dy = _rotate_offset(x if side == "top" else -x, y, pose["rotation_deg"])
    return pose["x_mm"] + dx, pose["y_mm"] + dy


def inverse_transform_point(board_xy_mm, pose):
    x, y = _point(board_xy_mm)
    dx, dy = _rotate_offset(x - pose["x_mm"], y - pose["y_mm"], -pose["rotation_deg"])
    if pose.get("side") not in {"top", "bottom"}:
        raise ValueError("Explicit component side required for an inverse transform")
    return (dx if pose["side"] == "top" else -dx), dy


def snap_to_origin_grid(point, origin, grid_xy_mm):
    """Computational snap, with explicit origin and independent X/Y pitch."""
    point, origin, pitch = _point(point), _point(origin), _point(grid_xy_mm)
    if min(pitch) <= 0:
        raise ValueError("Grid pitch must be positive")
    def nearest(v):
        return math.floor(v + .5) if v >= 0 else math.ceil(v - .5)
    return tuple(o + nearest((v - o) / g) * g for v, o, g in zip(point, origin, pitch))


def validate_board_context(context):
    """Validate the public mm geometry contract without filling missing models."""
    if not isinstance(context, dict) or context.get("units") != "mm":
        raise ValueError("BoardContext requires explicit units='mm'")
    out = copy.deepcopy(context)
    if "coverage" not in out:
        out["coverage"] = copy.deepcopy(out.get("geometry_coverage", {}))
    components = out.get("components")
    if not isinstance(components, list) or not components or len(components) > 2000:
        raise ValueError("BoardContext needs 1..2000 components")
    ids = set()
    for c in components:
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or cid in ids:
            raise ValueError("Component IDs must be unique nonempty strings")
        ids.add(cid)
        for key in ("x_mm", "y_mm", "rotation_deg"):
            c[key] = _number(c.get(key), f"{cid}.{key}")
        c["rotation_deg"] %= 360
        if c.get("side") not in {"top", "bottom", None}:
            raise ValueError("side must be top, bottom, or null/unknown")
        if c.get("courtyard_local") is not None:
            c["courtyard_local"] = _ring(c["courtyard_local"], cid + " courtyard")
        if not isinstance(c.get("fixed", False), bool) or not isinstance(c.get("locked", False), bool):
            raise ValueError("fixed/locked must be boolean")
        if c.get("allowed_rotations_deg") is not None:
            if not isinstance(c["allowed_rotations_deg"], list) or not c["allowed_rotations_deg"]:
                raise ValueError("allowed rotations must be a nonempty list")
            c["allowed_rotations_deg"] = [_number(x, "rotation") % 360 for x in c["allowed_rotations_deg"]]
    pads, pids = out.setdefault("pads", []), set()
    if not isinstance(pads, list) or len(pads) > 100000:
        raise ValueError("Pad list exceeds the supported budget")
    stationary = out.setdefault("stationary_pads", [])
    if not isinstance(stationary, list) or len(pads) + len(stationary) > 100000:
        raise ValueError("Pad list exceeds the supported budget")
    for p, owned in chain(((p, True) for p in pads), ((p, False) for p in stationary)):
        pid = p.get("id")
        if not isinstance(pid, str) or not pid or pid in pids:
            raise ValueError("Pads require globally unique nonempty IDs")
        if owned and p.get("component_id") not in ids:
            raise ValueError("Pads require captured component_id bindings")
        if not owned and ("component_id" in p or "local_xy_mm" in p):
            raise ValueError("Stationary pads require absolute XY and no component ownership")
        pids.add(pid)
        if "local_xy_mm" in p:
            p["local_xy_mm"] = _point(p["local_xy_mm"])
        elif p.get("x_mm") is not None and p.get("y_mm") is not None:
            p["x_mm"] = _number(p["x_mm"], "pad x")
            p["y_mm"] = _number(p["y_mm"], "pad y")
        else:
            raise ValueError("Pad position requires local_xy_mm or absolute board XY")
        for key in ("width_mm", "height_mm"):
            if p.get(key) is not None:
                p[key] = _number(p[key], key, positive=True)
        if p.get("rotation_deg") is not None:
            p["rotation_deg"] = _number(p["rotation_deg"], "pad rotation")
    outline = out.get("outline")
    if isinstance(outline, list):
        outline = {"outline": outline, "holes": out.get("cutouts", [])}
        out["outline"] = outline
    if outline is not None:
        if not isinstance(outline, dict) or not geom.region_geometry_valid(outline):
            raise ValueError("Board outline/cutouts require a valid polygon-with-holes")
        for ring in [outline["outline"], *outline.get("holes", [])]:
            for point in ring:
                _point(point)
    scope = out.get("scope_component_ids", sorted(ids))
    if not isinstance(scope, list) or not set(scope) <= ids:
        raise ValueError("Placement scope references absent components")
    out["scope_component_ids"] = sorted(set(scope))
    # Also reject nonfinite metadata before it can enter an evidence digest.
    digest(out)
    return out


def validate_design_intent(intent, context):
    if intent is None:
        intent = {}
    if not isinstance(intent, dict):
        raise ValueError("DesignIntent must be an object")
    out = copy.deepcopy(intent)
    ids = {c["id"] for c in context["components"]}
    pids = {p["id"] for p in context["pads"]}
    seen = set()
    for block in out.get("blocks", []):
        bid = block.get("id")
        if not isinstance(bid, str) or not bid or bid in seen:
            raise ValueError("Block IDs must be unique")
        seen.add(bid)
        if not set(block.get("component_ids", [])) <= ids:
            raise ValueError("Block references absent components")
        if not set(block.get("profiles", [])) <= PROFILE_INPUTS.keys():
            raise ValueError("Unknown placement profile")
    seen_paths = set()
    for path in out.get("critical_paths", []):
        if not isinstance(path.get("id"), str) or not path["id"] or path["id"] in seen_paths:
            raise ValueError("Critical paths require unique stable IDs")
        seen_paths.add(path["id"])
        if not set(path.get("pad_ids", [])) <= pids:
            raise ValueError("Critical path references absent pad IDs")
        if len(path.get("pad_ids", [])) < 2:
            raise ValueError("Critical path needs at least two explicit pad IDs")
    seen_bindings = set()
    for binding in out.get("decoupling_connections", []):
        if not isinstance(binding.get("id"), str) or not binding["id"] or binding["id"] in seen_bindings:
            raise ValueError("Decoupling mappings require unique stable IDs")
        seen_bindings.add(binding["id"])
        for key in ("ic_power_pad", "cap_power_pad", "ic_return_pad", "cap_return_pad"):
            if binding.get(key) is not None and binding[key] not in pids:
                raise ValueError("Decoupling mapping references an absent captured pad")
    digest(out)
    return out


def select_profiles(intent):
    """Composable per-block profiles from explicit roles; never from MPN/refdes."""
    assignments = []
    for block in intent.get("blocks", []):
        roles = set(block.get("roles", []))
        explicit = set(block.get("profiles", []))
        inferred = {p for p, allowed in PROFILE_ROLES.items() if roles & allowed}
        assignments.append({"block_id": block["id"], "component_ids": sorted(block.get("component_ids", [])),
            "profiles": sorted(explicit | inferred), "explicit_profiles": sorted(explicit),
            "role_inferred_profiles": sorted(inferred - explicit),
            "role_evidence_qualified": _qualified(block.get("source")),
            "inference": "declared_pin_or_block_roles; no part-number inference"})
    return assignments


def _pose(c):
    return {k: c.get(k) for k in ("x_mm", "y_mm", "rotation_deg", "side")}


def _normalise_moves(context, moves):
    comps = {c["id"]: c for c in context["components"]}
    poses = {cid: _pose(c) for cid, c in comps.items()}
    canonical, seen = [], set()
    if not isinstance(moves, (list, tuple)) or len(moves) > len(comps):
        raise ValueError("Moves require a bounded array")
    for move in moves:
        cid = move.get("component_id")
        if cid not in comps or cid in seen:
            raise ValueError("Moves must reference unique captured component IDs")
        if cid not in context["scope_component_ids"]:
            raise ValueError("Move lies outside the reviewed component scope")
        if set(move) - {"component_id", "designator", "x_mm", "y_mm", "rotation_deg", "side"}:
            raise ValueError("Unknown move field")
        designator = comps[cid].get("reference", comps[cid].get("designator"))
        if "designator" in move and move["designator"] != designator:
            raise ValueError("Move designator does not match its captured component identity")
        seen.add(cid)
        p = poses[cid]
        for key in ("x_mm", "y_mm", "rotation_deg"):
            if key in move:
                p[key] = _number(move[key], key)
        p["rotation_deg"] %= 360
        if "side" in move:
            if move["side"] not in {"top", "bottom"}:
                raise ValueError("Move side must be top or bottom")
            p["side"] = move["side"]
        canonical.append({"component_id": cid, "designator": designator, **p})
    return poses, sorted(canonical, key=lambda m: m["component_id"])


def _bbox(ring):
    return min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)


def _aabb_touch(a, b):
    a, b = _bbox(a), _bbox(b)
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _interior_overlap(a, b):
    if not _aabb_touch(a, b):
        return False
    if any(geom.point_in_ring(p, b, boundary=False) for p in a):
        return True
    if any(geom.point_in_ring(p, a, boundary=False) for p in b):
        return True
    for ring, other in ((a, b), (b, a)):
        for p, q in geom.ring_edges(ring):
            ts = [0.0, 1.0]
            for r, s in geom.ring_edges(other):
                ts.extend(geom.segment_intersection_parameters(p, q, r, s))
            ts = sorted(set(ts))
            if any(hi - lo > geom.EPS and geom.point_in_ring(geom.lerp(p, q, (hi + lo) / 2),
                    other, boundary=False) for lo, hi in zip(ts, ts[1:])):
                return True
    # Identical polygons have no strictly interior vertices.
    return all(any(geom.distance(p, q) <= geom.EPS for q in b) for p in a)


def _ring_distance(a, b):
    if _interior_overlap(a, b):
        return 0.0
    return min(geom.segment_distance(p, q, r, s) for p, q in geom.ring_edges(a) for r, s in geom.ring_edges(b))


def _layer(value):
    if not isinstance(value, str) or not value:
        return None
    compact = value.casefold().replace(" ", "").replace("_", "").replace("-", "")
    return {"toplayer": "top", "bottomlayer": "bottom", "multilayer": None}.get(compact, compact)


def _contained(ring, region, allowances=()):
    regions = [region, *allowances]
    if any(geom.uncovered_intervals(a, b, regions) for a, b in geom.ring_edges(ring)):
        return False
    # An enclosed hole must not disappear simply because all outer edges fit.
    for hole in region.get("holes", []):
        if any(geom.point_in_ring(p, ring, boundary=False) and not any(
                geom.point_in_region(p, allowance) for allowance in allowances) for p in hole):
            return False
    return True


def _component_ring(comp, pose):
    if comp.get("courtyard_local") is None or not _qualified(comp.get("courtyard_source")):
        return None
    if pose.get("side") not in {"top", "bottom"}:
        return None
    return [transform_point(p, pose) for p in comp["courtyard_local"]]


def transformed_pads(context, poses):
    """Move captured pad geometry rigidly; pin IDs and net bindings stay intact."""
    components = {c["id"]: c for c in context["components"]}
    result = {}
    error = _number(context.get("curve_error_mm", .0005), "curve_error_mm", positive=True)
    for raw in context["pads"]:
        c, pose = components[raw["component_id"]], poses[raw["component_id"]]
        old = _pose(c)
        pad = dict(raw)
        try:
            local = raw.get("local_xy_mm")
            if local is None:
                if pose == old:
                    center = (raw["x_mm"], raw["y_mm"])
                else:
                    center = transform_point(inverse_transform_point((raw["x_mm"], raw["y_mm"]), old), pose)
            else:
                center = transform_point(local, pose)
            pad["x_mm"], pad["y_mm"] = center
            raw_angle = raw.get('rotation_deg', 0)
            if local is not None:
                local_angle = raw_angle
            else:
                local_angle = raw_angle - old['rotation_deg']
                if old['side'] == 'bottom':
                    local_angle = -local_angle
            pad['rotation_deg'] = (pose['rotation_deg'] +
                (local_angle if pose['side'] == 'top' else -local_angle)) % 360
            if raw.get("width_mm") is not None and raw.get("height_mm") is not None:
                shape = {**raw, "x_mm": local[0] if local is not None else raw["x_mm"],
                         "y_mm": local[1] if local is not None else raw["y_mm"]}
                ring = geom.pad_outline(shape, error_mm=error)
                if local is None:
                    pad["outline"] = ring if pose == old else [transform_point(inverse_transform_point(p, old), pose) for p in ring]
                else:
                    pad["outline"] = [transform_point(p, pose) for p in ring]
                pad["curve_error_mm"] = 0.0 if raw.get("shape", "rectangle") in {"rectangle", "rect", "2"} else error
            if pose.get("side") != old.get("side"):
                mapping = context.get("opposite_layers", {})
                pad["layer"] = mapping.get(raw.get("layer"))
        except (ValueError, KeyError, TypeError):
            pad["x_mm"] = pad["y_mm"] = None
            pad.pop("outline", None)
        result[raw["id"]] = pad
    for raw in context.get("stationary_pads", []):
        pad = dict(raw)
        try:
            if raw.get("width_mm") is not None and raw.get("height_mm") is not None:
                pad["outline"] = geom.pad_outline(raw, error_mm=error)
                pad["curve_error_mm"] = 0.0 if raw.get("shape", "rectangle") in {"rectangle", "rect", "2"} else error
        except (ValueError, KeyError, TypeError):
            pad.pop("outline", None)
            pad["geometry_complete"] = False
        result[raw["id"]] = pad
    return result


def _check(check_id, status, snapshot, *, required=True, kind="hard", scope=None,
           measured=None, limit=None, unit=None, geometry=None, verification="deterministic_geometry", reason="", source=None):
    assert status in STATUSES
    return {"id": check_id, "status": status, "required": required, "kind": kind,
            "scope": scope or {}, "measured_value": measured, "limit_value": limit, "unit": unit,
            "geometry": geometry or {}, "coverage": "complete" if status in {"pass", "fail", "not_applicable"} else "unknown",
            "source_snapshot": snapshot, "verification": verification, "reason": reason,
            "source": source, "fix_action": None if status in {"pass", "not_applicable"} else reason}


def _profile_checks(context, intent, snapshot, constraint_ids=()):
    checks = []
    blocks = {b["id"]: b for b in intent.get("blocks", [])}
    scope = set(context["scope_component_ids"])
    for assignment in select_profiles(intent):
        bid, members = assignment["block_id"], assignment["component_ids"]
        if not scope & set(members):
            continue
        block = blocks[bid]
        for profile in assignment["profiles"]:
            bound = block.get('requirements', {}).get('placement_check_ids')
            by_id = {c.get('id'): c for c in constraint_ids}
            pad_owners = {p['id']: p['component_id'] for p in context['pads']}
            def relevant(identity):
                if not isinstance(identity, str) or identity not in by_id:
                    return False
                c = by_id[identity]
                s = c.get('scope', {})
                affected = set(s.get('components', [])) | {pad_owners[p] for p in s.get('pads', []) if p in pad_owners}
                return (c.get('kind', 'hard') == 'hard' and c.get('applicability', True) is True
                    and _qualified(c.get('source')) and bool(affected & set(members)))
            linked = isinstance(bound, list) and bool(bound) and all(relevant(x) for x in bound)
            checks.append(_check(f'profile:{bid}:{profile}:scoped_checks', 'pass' if linked else 'unknown', snapshot,
                scope={'components': members, 'block': bid}, verification='input_contract',
                reason='Profiles need actual placement-phase constraint IDs; input presence is not physical verification'))
            for field in PROFILE_INPUTS[profile]:
                value = block.get("requirements", {}).get(field)
                qualified = assignment["role_evidence_qualified"]
                status = "pass" if value and qualified else "unknown"
                checks.append(_check(f"profile:{bid}:{profile}:{field}", status, snapshot,
                    scope={"components": members, "block": bid}, verification="input_contract",
                    reason="Declared topology/budget input is present; physical verification remains separate" if status == "pass" else
                    f"Supply sourced {field} for the {profile} block", source=block.get("source")))
    return checks


def _decoupling_checks(intent, pads, snapshot, scope=None):
    checks = []
    for binding in intent.get("decoupling_connections", []):
        bid = binding.get("id")
        if not isinstance(bid, str) or not bid:
            raise ValueError("Decoupling mappings require stable IDs")
        names = ("ic_power_pad", "cap_power_pad", "ic_return_pad", "cap_return_pad")
        ids = [binding.get(key) for key in names]
        if scope is not None and not any(pads.get(pid, {}).get('component_id') in scope for pid in ids):
            continue
        evidence_scope = {"pads": [pid for pid in ids if pid is not None]}
        power, ret = binding.get("power_net"), binding.get("return_net")
        status, reason = "unknown", "Provide sourced power/return pad mappings; proximity alone is insufficient"
        if all(pid in pads for pid in ids) and power and ret and _qualified(binding.get("source")):
            valid = (power != ret and pads[ids[0]].get("net_name") == power and pads[ids[1]].get("net_name") == power
                     and pads[ids[2]].get("net_name") == ret and pads[ids[3]].get("net_name") == ret)
            status = "pass" if valid else "fail"
            reason = "Mapped power/return pads disagree with captured net bindings" if not valid else "Power/return mapping verified; routed loop is not verified"
        checks.append(_check("decoupling_binding:"+bid, status, snapshot, scope=evidence_scope,
            verification="topology_binding", reason=reason, source=binding.get("source")))
    return checks


def _decoupling_physical_checks(context, intent, pads, snapshot):
    """Verify both named branches using the existing routed copper graph.

    No same-layer rule or guessed loop-length limit is imposed. A valid path
    proves only routed connectivity/length in the captured geometry, not ESL,
    PDN impedance or suitability of a manufacturer's unverified topology.
    """
    from .copper_audit import CopperGraph
    result, graphs = [], {}
    physical = copy.deepcopy(context.get('copper_snapshot'))
    if physical is not None:
        for pad in physical.get('pads', []):
            proposed = pads.get(str(pad.get('id')))
            if proposed is not None:
                for key in ('x_mm', 'y_mm', 'rotation_deg', 'layer'):
                    if key in proposed:
                        pad[key] = proposed[key]
    scope = set(context['scope_component_ids'])
    for binding in intent.get('decoupling_connections', []):
        ids = [binding.get(k) for k in ('ic_power_pad', 'cap_power_pad', 'ic_return_pad', 'cap_return_pad')]
        if not any(pads.get(pid, {}).get('component_id') in scope for pid in ids):
            continue
        status, paths = 'unknown', []
        if physical is not None and all(pid in pads for pid in ids) and _qualified(binding.get('source')):
            for a, b, net in ((ids[0], ids[1], binding.get('power_net')),
                              (ids[2], ids[3], binding.get('return_net'))):
                if not net or pads[a].get('net_name') != net or pads[b].get('net_name') != net:
                    status = 'fail'; break
                if net not in graphs:
                    graphs[net] = CopperGraph(physical, net)
                graph = graphs[net]
                path = graph.shortest(graph.pad_nodes.get(a, []), graph.pad_nodes.get(b, []))
                if path is None or graph.unknowns:
                    break
                paths.append(path)
            if status != 'fail' and len(paths) == 2:
                status = 'pass'
        measured = sum(p['length_mm'] for p in paths) if len(paths) == 2 else None
        result.append(_check('decoupling_physical_return:'+binding['id'], status, snapshot,
            scope={'pads': ids}, measured=measured, unit='mm', geometry={'routed_paths': paths},
            verification='routed_copper_graph', source=binding.get('source'),
            reason='Both explicit IC/cap power and return connections need proven copper paths; proximity alone cannot pass'))
    return result


def _constraint_checks(context, intent, constraints, poses, pads, snapshot):
    checks = []
    seen = set()
    scoped = set(context["scope_component_ids"])
    for constraint in constraints:
        cid = constraint.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("Constraint IDs must be unique nonempty strings")
        seen.add(cid)
        kind = constraint.get("kind", "hard")
        if kind not in {"hard", "soft"}:
            raise ValueError("Constraint kind must be hard or soft")
        scope = constraint.get("scope", {})
        members = scope.get("components", [])
        if members and not set(members) <= poses.keys():
            raise ValueError("Constraint scope references absent components")
        applies = constraint.get("applicability", True)
        if applies is not True and applies is not False and applies is not None:
            raise ValueError("applicability must be true, false or null")
        base = dict(required=kind == "hard", kind=kind, scope=scope, source=constraint.get("source"))
        if applies is False or members and not set(members) & scoped:
            checks.append(_check(cid, "not_applicable", snapshot, **base)); continue
        if applies is None or not _qualified(constraint.get("source")):
            checks.append(_check(cid, "unknown", snapshot, reason="Constraint applicability or source is unqualified", **base)); continue
        check_type = constraint.get("check")
        tolerance = _number(constraint.get("tolerance_mm", 0), "constraint tolerance", minimum=0)
        if check_type in {"pad_distance", "pad_clearance"}:
            pair = scope.get("pads", [])
            if len(pair) != 2 or any(p not in pads for p in pair):
                raise ValueError("Pad constraint needs exactly two captured pad IDs")
            a, b = [pads[p] for p in pair]
            measure = constraint.get("measure", "edge" if check_type == "pad_clearance" else "center")
            limit = constraint.get("limit_mm")
            if limit is None:
                checks.append(_check(cid, "unknown", snapshot, reason="Project-specific limit is unknown", **base)); continue
            limit = _number(limit, "limit_mm", minimum=0)
            relation = constraint.get("relation", "min" if check_type == "pad_clearance" else "max")
            if relation not in {"min", "max"} or measure not in {"center", "edge"}:
                raise ValueError("Unsupported pad measurement/relation")
            if a.get("x_mm") is None or b.get("x_mm") is None or measure == "edge" and (
                    not a.get("outline") or not b.get("outline")):
                checks.append(_check(cid, "unknown", snapshot, reason="Required pad geometry/transform unavailable", **base)); continue
            if check_type == "pad_clearance" and (not _layer(a.get("layer")) or not _layer(b.get("layer"))):
                checks.append(_check(cid, "unknown", snapshot, reason="Pad layer binding unavailable", **base)); continue
            if check_type == "pad_clearance" and _layer(a["layer"]) != _layer(b["layer"]):
                checks.append(_check(cid, "not_applicable", snapshot, reason="Different copper layers; no same-layer clearance claim", **base)); continue
            measured = _ring_distance(a["outline"], b["outline"]) if measure == "edge" else geom.distance(geom.xy(a), geom.xy(b))
            error = sum(p.get("curve_error_mm", 0) for p in (a, b)) if measure == "edge" else 0
            lo, hi = max(0, measured - error), measured + error
            if relation == "min":
                status = "pass" if lo + tolerance >= limit else "fail" if hi + tolerance < limit else "unknown"
            else:
                status = "pass" if hi <= limit + tolerance else "fail" if lo > limit + tolerance else "unknown"
            checks.append(_check(cid, status, snapshot, measured=measured, limit=limit, unit="mm",
                geometry={"pads": pair, "centers": [geom.xy(a), geom.xy(b)], "measurement": measure,
                          "bounded_measurement_mm": [lo, hi]},
                reason="Pad geometry violates the sourced limit" if status == "fail" else
                       "Curve approximation straddles the limit" if status == "unknown" else "", **base))
        elif check_type == "grid":
            origin, pitch = constraint.get("origin_mm"), constraint.get("pitch_mm")
            if origin is None or pitch is None:
                checks.append(_check(cid, "unknown", snapshot, reason="Grid origin or pitch unknown", **base)); continue
            deviations = [geom.distance((poses[x]["x_mm"], poses[x]["y_mm"]), snap_to_origin_grid(
                (poses[x]["x_mm"], poses[x]["y_mm"]), origin, pitch)) for x in (members or sorted(scoped))]
            value = max(deviations, default=0)
            checks.append(_check(cid, "pass" if value <= tolerance + geom.EPS else "fail", snapshot,
                measured=value, limit=tolerance, unit="mm", reason="Move to the project grid relative to its origin", **base))
        else:
            checks.append(_check(cid, "unknown", snapshot, verification="unavailable",
                reason=f"No qualified verifier for {check_type}; native DRC/physics evidence cannot be caller-attested", **base))
    return checks


def _manufacturing_checks(context, pads, snapshot):
    """Project-qualified pad clearance, separate from courtyard collisions.

    Rectangles are exact polygons. Circular pads use their analytical radius;
    unsupported curved/stack geometries stay unknown instead of passing from
    an inscribed render polygon. This is not a complete fabrication DRC.
    """
    profile = context.get('fabrication_profile', {})
    limit = profile.get('minimum_pad_clearance_mm')
    source = profile.get('source')
    if limit is None or not _qualified(source):
        return [_check('manufacturing:pad_clearance_input', 'unknown', snapshot,
            reason='Supply the project fabrication minimum pad clearance and its source',
            verification='input_contract', source=source)]
    limit = _number(limit, 'minimum pad clearance', minimum=0)
    scope = set(context['scope_component_ids'])
    checks = []
    coverage = context.get('coverage', {}).get('pads')
    incomplete = (coverage is False or isinstance(coverage, str) and coverage in {'partial', 'unknown', 'unavailable'}
                  or isinstance(coverage, dict) and (coverage.get('complete') is False or coverage.get('status') in {'partial', 'unknown', 'unavailable'}))
    if incomplete:
        checks.append(_check('manufacturing:pad_extraction', 'unknown', snapshot,
            scope={'components': sorted(scope)}, source=source,
            reason='Captured pad extraction is incomplete; absent pad pairs cannot establish fabrication clearance'))
    values = sorted(pads.values(), key=lambda p: p['id'])
    def shape(p):
        if p.get('geometry_complete') is False:
            return None
        kind = str(p.get('shape', '')).lower()
        if kind in {'round', 'circle'} and p.get('width_mm') == p.get('height_mm') and p.get('width_mm'):
            return 'circle'
        if (kind in {'rectangle', 'rect', 'polygon'} and p.get('outline')
                and (kind != 'polygon' or not p.get('curve_error_mm'))
                and geom.region_geometry_valid({'outline': p['outline']})):
            return 'polygon'
        return None
    def clearance(a, b):
        ka, kb = shape(a), shape(b)
        if not ka or not kb or a.get('x_mm') is None or b.get('x_mm') is None:
            return None
        if ka == kb == 'circle':
            return max(0, geom.distance((a['x_mm'], a['y_mm']), (b['x_mm'], b['y_mm']))
                       - (a['width_mm'] + b['width_mm']) / 2)
        if ka == kb == 'polygon':
            return _ring_distance(a['outline'], b['outline'])
        circle, poly = (a, b) if ka == 'circle' else (b, a)
        point, ring = (circle['x_mm'], circle['y_mm']), poly['outline']
        if geom.point_in_region(point, {'outline': ring, 'holes': []}):
            return 0.0
        return max(0, min(geom.point_segment_distance(point, ring[i], ring[(i+1) % len(ring)])
                          for i in range(len(ring))) - circle['width_mm'] / 2)
    # Only nearby same-layer pairs can violate the minimum. Bounds are exact
    # for qualified rectangles/polygons and analytical circles; an AABB is a
    # broad-phase bound, never a replacement for the narrow copper contour.
    from collections import Counter
    indices, boxes, known, unknown = {}, {}, {}, []
    all_counts, scoped_counts = defaultdict(Counter), defaultdict(Counter)
    for i, pad in enumerate(values):
        layer = _layer(pad.get('layer'))
        all_counts[layer][pad.get('net_name')] += 1
        if pad.get('component_id') in scope:
            scoped_counts[layer][pad.get('net_name')] += 1
        kind = shape(pad)
        if not kind or layer is None or pad.get('x_mm') is None or pad.get('y_mm') is None:
            unknown.append(i); continue
        if kind == 'circle':
            radius = pad['width_mm'] / 2
            box = (pad['x_mm']-radius, pad['y_mm']-radius, pad['x_mm']+radius, pad['y_mm']+radius)
        else:
            box = _bbox(pad['outline'])
        index = indices.setdefault(layer, geom.SpatialIndex())
        index.insert(i, box); boxes[i] = box; known[i] = layer
    unresolved = []
    for i in unknown:
        pad = values[i]
        layer, net = _layer(pad.get('layer')), pad.get('net_name')
        groups = all_counts if pad.get('component_id') in scope else scoped_counts
        relevant = Counter()
        for candidate_layer, counts in groups.items():
            if layer is None or candidate_layer is None or candidate_layer == layer:
                relevant.update(counts)
        if pad.get('component_id') in scope:
            relevant[net] -= 1
        if (sum(relevant.values()) - (relevant[net] if net else 0)) > 0:
            unresolved.append(pad['id'])
    if unresolved:
        checks.append(_check('manufacturing:pad_clearance_unknown', 'unknown', snapshot,
            scope={'pads': unresolved}, source=source,
            reason='Unsupported pad contours/stacks or layer spans can affect this scope; distance is not guessed'))
    seen, narrow_count = set(), 0
    for i, layer in known.items():
        a = values[i]
        if a.get('component_id') not in scope:
            continue
        box = boxes[i]
        expanded = (box[0]-limit-geom.EPS, box[1]-limit-geom.EPS,
                    box[2]+limit+geom.EPS, box[3]+limit+geom.EPS)
        for j in indices[layer].query(expanded):
            pair = tuple(sorted((i, j)))
            if i == j or pair in seen:
                continue
            seen.add(pair)
            b = values[j]
            if a.get('net_name') and a.get('net_name') == b.get('net_name'):
                continue
            other = boxes[j]
            gap_x = max(0, box[0]-other[2], other[0]-box[2])
            gap_y = max(0, box[1]-other[3], other[1]-box[3])
            if math.hypot(gap_x, gap_y) + geom.EPS >= limit:
                continue
            narrow_count += 1
            measured = clearance(a, b)
            if measured + geom.EPS < limit:
                checks.append(_check('manufacturing:pad_clearance:'+a['id']+':'+b['id'], 'fail', snapshot,
                    measured=measured, limit=limit, unit='mm', scope={'pads': [a['id'], b['id']]}, source=source,
                    geometry={'narrow_phase_pairs': narrow_count, 'stop_after_first_violation': True},
                    reason='Exact supported pad copper violates the sourced clearance; remaining pairs not evaluated'))
                return checks
    # Far pairs are certified by their exact bounding-box separation; avoid
    # materializing O(P^2) individual passing records in every candidate.
    checks.append(_check('manufacturing:pad_clearance', 'unknown' if unresolved or incomplete else 'pass', snapshot,
        scope={'components': sorted(scope)}, limit=limit, unit='mm', source=source,
        geometry={'pad_count': len(values), 'narrow_phase_pairs': narrow_count,
                  'broad_phase': 'per_layer_exact_bounds_index', 'unsupported_pad_ids': unresolved},
        reason='Per-layer exact-bounds rejection plus analytical/contour narrow phase; not a complete fabrication DRC'))
    return checks


def evaluate_placement(context, intent=None, constraints=(), moves=(), *, candidate_id="baseline"):
    """Validate final batch geometry; hard failures/unknowns cannot be scored away."""
    context = validate_board_context(context)
    intent = validate_design_intent(intent, context)
    if not isinstance(constraints, (list, tuple)) or len(constraints) > 10000:
        raise ValueError("Constraint list exceeds the supported budget")
    constraints = copy.deepcopy(list(constraints))
    poses, canonical = _normalise_moves(context, moves)
    snapshot = context.get("snapshot_id")
    scoped = set(context["scope_component_ids"])
    comps = {c["id"]: c for c in context["components"]}
    checks = []
    rings = {cid: _component_ring(c, poses[cid]) for cid, c in comps.items()}
    for move in canonical:
        cid, c = move["component_id"], comps[move["component_id"]]
        before, after = _pose(c), poses[cid]
        changed = before != after
        checks.append(_check(f"fixed:{cid}", "fail" if (c.get("fixed") or c.get("locked")) and changed else "pass", snapshot,
            scope={"components": [cid]}, reason="Preserve locked/mechanical anchor pose"))
        allowed = c.get("allowed_rotations_deg")
        if allowed is not None:
            ok = any(abs((after["rotation_deg"] - v + 180) % 360 - 180) <= geom.EPS for v in allowed)
            checks.append(_check(f"rotation:{cid}", "pass" if ok else "fail", snapshot,
                scope={"components": [cid]}, reason="Rotation lies outside the supplied package/mechanical choices"))
        if before.get("side") != after.get("side"):
            qualified = c.get("allow_side_change") is True and context.get("opposite_layers") and _qualified(c.get("assembly_source"))
            checks.append(_check(f"side_change:{cid}", "pass" if qualified else "unknown", snapshot,
                scope={"components": [cid]}, reason="Side change needs an explicit assembly permission and layer/mirror model"))
    outline = context.get("outline")
    for cid in sorted(scoped):
        ring = rings[cid]
        status = "pass" if ring is not None else "unknown"
        checks.append(_check(f"courtyard:{cid}", status, snapshot, scope={"components": [cid]},
            reason="Supply sourced courtyard geometry and explicit component side" if status == "unknown" else ""))
        if outline is None or ring is None or context.get("coverage", {}).get("outline") not in {"complete", True}:
            checks.append(_check(f"outline:{cid}", "unknown", snapshot, scope={"components": [cid]},
                reason="Exact outline/cutout and courtyard evidence required")); continue
        allowances = []
        for allowance in context.get("overhang_allowances", []):
            if allowance.get("component_id") == cid and _qualified(allowance.get("source")):
                region = allowance.get("region")
                if not isinstance(region, dict) or not geom.region_geometry_valid(region):
                    raise ValueError("Overhang allowance must be a valid explicit region")
                allowances.append(region)
        inside = _contained(ring, outline, allowances)
        checks.append(_check(f"outline:{cid}", "pass" if inside else "fail", snapshot,
            scope={"components": [cid]}, geometry={"courtyard": ring},
            reason="Courtyard crosses the real outline/cutout outside a sourced overhang allowance" if not inside else ""))
    pairs = sorted(comps)
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            if not ({a, b} & scoped):
                continue
            sa, sb = poses[a].get("side"), poses[b].get("side")
            if sa != sb and sa is not None and sb is not None:
                physical = comps[a].get("through_body") or comps[b].get("through_body")
                if physical:
                    checks.append(_check(f"opposite_assembly:{a}:{b}", "unknown", snapshot,
                        scope={"components": [a, b]}, reason="Through-body/opposite-side assembly requires a 3-D envelope"))
                continue
            if rings[a] is None or rings[b] is None:
                checks.append(_check(f"collision:{a}:{b}", "unknown", snapshot,
                    scope={"components": [a, b]}, reason="Courtyard unavailable; AABB cannot certify a noncollision")); continue
            overlap = _interior_overlap(rings[a], rings[b])
            checks.append(_check(f"collision:{a}:{b}", "fail" if overlap else "pass", snapshot,
                scope={"components": [a, b]}, geometry={"courtyards": [rings[a], rings[b]]},
                reason="Final-state courtyards overlap" if overlap else ""))
    pads = transformed_pads(context, poses)
    checks += _manufacturing_checks(context, pads, snapshot)
    checks += _profile_checks(context, intent, snapshot, constraints)
    checks += _decoupling_checks(intent, pads, snapshot, scoped)
    checks += _decoupling_physical_checks(context, intent, pads, snapshot)
    checks += _constraint_checks(context, intent, constraints, poses, pads, snapshot)
    channel_checks, reservations = evaluate_channels(context, intent, poses, pads)
    checks += channel_checks
    if intent.get('copper_intent') is not None:
        from .copper_audit import audit_board_copper
        physical = context.get('copper_snapshot')
        if physical is None:
            checks.append(_check('physical_copper', 'unknown', snapshot,
                reason='Physical copper intent requires the captured copper snapshot'))
        else:
            virtual = copy.deepcopy(physical)
            for pad in virtual.get('pads', []):
                proposed = pads.get(str(pad.get('id')))
                if proposed is not None:
                    for key in ('x_mm', 'y_mm', 'rotation_deg', 'layer'):
                        if key in proposed:
                            pad[key] = proposed[key]
            audit = audit_board_copper(virtual, intent['copper_intent'], policy=intent.get('copper_policy'))
            checks.append(_check('physical_copper',
                'fail' if audit['violations'] else 'unknown' if not audit['coverage_complete'] else 'pass', snapshot,
                verification='copper_geometry_audit', geometry={'audit': audit},
                reason='Existing copper auditor reused on proposed pad positions; no routing or field simulation is implied'))
    metrics = _metrics(context, intent, poses, pads)
    hard = [c for c in checks if c["required"] and c["kind"] == "hard"]
    failed = any(c["status"] == "fail" for c in hard)
    unknown = any(c["status"] == "unknown" for c in hard)
    input_context = {k: v for k, v in context.items() if k not in {"capture_metrics", "captured_at", "native_snapshot_json", "native_snapshot_token"}}
    report = {"schema": 1, "policy_version": POLICY_VERSION, "candidate_id": candidate_id,
        "source_snapshot": snapshot, "input_digest": digest({"context": input_context, "intent": intent, "constraints": constraints}),
        "constraint_digest": digest(constraints), "status": "infeasible" if failed else "unknown" if unknown else "feasible",
        "feasible": not failed and not unknown, "required_checks": [c["id"] for c in hard], "checks": checks,
        "blockers": [c["id"] for c in hard if c["status"] not in {"pass", "not_applicable"}],
        "metrics": metrics, "final_poses": poses, "moves": canonical, "reservations": reservations,
        "profiles": select_profiles(intent),
        "coverage": {"electrical_routing": "not_verified", "courtyards": "complete" if all(rings[c] is not None for c in scoped) else "unknown",
                     "renderer": "not_evaluated", "physics": "not_simulated"}}
    report["evidence_id"] = digest(report)
    return report


def _metrics(context, intent, poses, pads):
    from .placement_p0 import transformed_pad_aabb_manhattan_mm as gap, crossing_proxy, alignment_proxy
    by_net = defaultdict(list)
    for pad in pads.values():
        if pad.get("net_name") and pad.get("x_mm") is not None:
            by_net[pad["net_name"]].append(geom.xy(pad))
    excluded = set(intent.get("signal_attraction_excluded_nets", [])) | set(intent.get('reference_nets', [])) | set(intent.get('power_nets', [])) | {'GND'}
    weights = intent.get('signal_net_weights', {})
    hpwl = sum(_number(weights.get(net, 1), 'signal net weight', minimum=0) * (max(x for x, y in points) - min(x for x, y in points) +
               max(y for x, y in points) - min(y for x, y in points))
               for net, points in by_net.items() if net not in excluded)
    complete = all(p.get("x_mm") is not None for p in pads.values())
    result = {"signal_hpwl": {"value": hpwl if complete else None, "unit": "mm", "method": "pad_hpwl_proxy",
                             "coverage": "complete" if complete else "unknown", "excluded_nets": sorted(excluded)},
              "movement": {"value": sum(geom.distance((c["x_mm"], c["y_mm"]), (poses[c["id"]]["x_mm"], poses[c["id"]]["y_mm"]))
                  for c in context["components"]), "unit": "mm", "method": "origin_displacement", "coverage": "complete"}}
    result['ratsnest_crossings'] = {'value': crossing_proxy(pads, excluded), 'unit': 'count',
        'method': 'existing_mst_crossing_diagnostic', 'routing_verified': False, 'pad_budget':512}
    alignment = alignment_proxy(context, intent, poses)
    result['alignment_penalty'] = {'value': None if alignment is None else 1-alignment, 'aligned_ratio':alignment,
        'unit':'ratio', 'method':'declared_passive_family_grid_and_collinearity'}
    penalty = intent.get('via_transition_penalty_mm')
    for path in intent.get("critical_paths", []):
        pid = path.get("id")
        if not isinstance(pid, str) or not pid:
            raise ValueError("Critical paths require stable IDs")
        values = [pads[p] for p in path["pad_ids"]]
        available = all(p.get("x_mm") is not None for p in values)
        legs = [gap(a,b,penalty) for a,b in zip(values,values[1:])] if available else [None]
        value = sum(legs) if all(v is not None for v in legs) else None
        result["critical_path:" + pid] = {"value": value, "unit": "mm", "method": "transformed_pad_aabb_manhattan_mm",
            "coverage": "complete" if available else "unknown", "pad_ids": list(path["pad_ids"]),
            "route_verified": False, "critical": any(p.get('component_id') in context['scope_component_ids'] for p in values)}
        for index, leg in enumerate(legs):
            result[f'critical_pair:{pid}:{index}'] = {**result['critical_path:'+pid], 'value':leg,
                'pad_ids': path['pad_ids'][index:index+2]}
    for binding in intent.get("decoupling_connections", []):
        ids = [binding.get(key) for key in ("ic_power_pad", "cap_power_pad", "ic_return_pad", "cap_return_pad")]
        available = all(pid in pads and pads[pid].get("x_mm") is not None for pid in ids)
        legs = [gap(pads[ids[0]],pads[ids[1]],penalty),gap(pads[ids[2]],pads[ids[3]],penalty)] if available else [None,None]
        value = sum(legs) if all(v is not None for v in legs) else None
        result["critical_decap:" + binding["id"]] = {"value": value, "unit": "mm", "method": "transformed_pad_aabb_manhattan_mm",
            "coverage": "complete" if available else "unknown", "pad_ids": ids,
            "route_verified": False, "package_internal_path_included": False,
            "critical": any(pads.get(pid, {}).get('component_id') in context['scope_component_ids'] for pid in ids)}
        for name, leg, pair in [('vcc',legs[0],ids[:2]),('gnd',legs[1],ids[2:])]:
            result['critical_decap:'+binding['id']+':'+name] = {**result['critical_decap:'+binding['id']],
                'value':leg, 'pad_ids':pair}
    return result


def evaluate_channels(context, intent, poses, pads):
    """Per-layer rectangular channel capacity estimate, not a route proof.

    A blocked straight corridor does not prove no other path exists. Conversely
    a clear width lower bound says nothing about bends, fanout, antipads or
    differential topology. Those remain explicit coverage gaps.
    """
    checks, output = [], []
    snapshot = context.get("snapshot_id")
    rings = {c["id"]: _component_ring(c, poses[c["id"]]) for c in context["components"]}
    seen = set()
    for reservation in intent.get("reservations", []):
        rid = reservation.get("id")
        if not isinstance(rid, str) or not rid or rid in seen:
            raise ValueError("Unique reservation ID required")
        seen.add(rid)
        source, layer = reservation.get("source"), reservation.get("layer")
        scope = {"layers": [layer] if layer else [], "nets": reservation.get("net_names", [])}
        base = dict(scope=scope, source=source, required=reservation.get("required", True), verification="estimate")
        required = ("start_mm", "end_mm", "width_mm", "track_width_mm", "spacing_mm", "margin_mm")
        if not layer or not _qualified(source) or any(reservation.get(k) is None for k in required):
            checks.append(_check("channel:" + rid, "unknown", snapshot, reason="Supply sourced per-layer channel geometry and routing dimensions", **base)); continue
        a, b = _point(reservation["start_mm"]), _point(reservation["end_mm"])
        width = _number(reservation["width_mm"], "channel width", positive=True)
        w = _number(reservation["track_width_mm"], "track width", positive=True)
        spacing = _number(reservation["spacing_mm"], "spacing", minimum=0)
        margin = _number(reservation["margin_mm"], "margin", minimum=0)
        nets = reservation.get("net_names", [])
        if not nets or len(set(nets)) != len(nets):
            raise ValueError("Channel requires distinct intended net names")
        n = len(nets)
        lower_bound = n * w + (n - 1) * spacing + 2 * margin
        length = geom.distance(a, b)
        if length <= geom.EPS:
            raise ValueError("Channel endpoints must differ")
        dx, dy = -(b[1] - a[1]) / length * width / 2, (b[0] - a[0]) / length * width / 2
        ring = [(a[0]+dx, a[1]+dy), (b[0]+dx, b[1]+dy), (b[0]-dx, b[1]-dy), (a[0]-dx, a[1]-dy)]
        excluded = set(reservation.get("endpoint_component_ids", []))
        blockers, missing = [], []
        for comp in context["components"]:
            cid = comp["id"]
            if cid in excluded:
                continue
            layers = comp.get("obstructs_layers", [comp.get("layer") or ("Top" if poses[cid].get("side") == "top" else "Bottom")])
            if comp.get("side") and _layer(layer) not in {_layer(value) for value in layers}:
                continue
            if rings[cid] is None:
                missing.append(cid)
            elif _interior_overlap(ring, rings[cid]):
                blockers.append(cid)
        for obstruction in context.get("route_obstacles", []):
            if _layer(obstruction.get("layer")) == _layer(layer):
                shape = obstruction.get("outline")
                if shape is None:
                    missing.append(obstruction.get("id", "unknown obstacle"))
                elif _interior_overlap(ring, _ring(shape, "route obstacle")):
                    blockers.append(obstruction.get("id", "route obstacle"))
        for pad in pads.values():
            if pad.get("net_name") in nets:
                continue
            if _layer(pad.get("layer")) is None:
                missing.append(pad["id"])
            elif _layer(pad.get("layer")) == _layer(layer):
                if not pad.get("outline"):
                    missing.append(pad["id"])
                elif _interior_overlap(ring, pad["outline"]):
                    blockers.append(pad["id"])
        geometry_complete = context.get("coverage", {}).get("routing_obstacles") in {"complete", True}
        inside = context.get("outline") is not None and _contained(ring, context["outline"])
        status = "fail" if lower_bound > width + geom.EPS or blockers or not inside else "unknown" if missing or not geometry_complete else "pass"
        note = "Width is a cross-section lower bound; bends, escape, antipads and coupled-pair topology are not proved"
        row = {"id": rid, "layer": layer, "outline": ring, "required_width_lower_bound_mm": lower_bound,
               "available_width_mm": width, "obstructions": blockers, "missing_geometry": missing,
               "routing_completion_verified": False, "limitations": note}
        output.append(row)
        checks.append(_check("channel:" + rid, status, snapshot, measured=width, limit=lower_bound, unit="mm",
            geometry=row, reason=note if status == "pass" else "Channel width, outline or obstruction evidence is insufficient", **base))
    return checks, output


def compare_placement_candidates(context, intent=None, constraints=(), candidates=(), selection_policy=None):
    """Baseline + supplied proposals; feasibility and critical tiers precede cost."""
    context = validate_board_context(context)
    intent = validate_design_intent(intent, context)
    policy = copy.deepcopy(selection_policy or {})
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 64:
        raise ValueError("Candidate budget is 64 proposals plus baseline")
    seen = {"baseline"}
    evaluations = [evaluate_placement(context, intent, constraints)]
    for candidate in candidates:
        cid = candidate.get("id", candidate.get("candidate_id"))
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("Candidate IDs must be unique; baseline is reserved")
        seen.add(cid)
        evaluations.append(evaluate_placement(context, intent, constraints, candidate.get("moves", []), candidate_id=cid))
    baseline = evaluations[0]
    critical = [key for key, metric in baseline["metrics"].items() if metric.get("critical")]
    tolerance = policy.get("critical_nonregression_mm", {})
    if not isinstance(tolerance, dict) or any(k not in critical for k in tolerance):
        raise ValueError("Critical tolerances must reference actual reported critical metrics")
    for key, value in tolerance.items():
        _number(value, "critical nonregression tolerance", minimum=0)
        if value != 0:
            raise ValueError('P0 Tier 1 permits no positive nonregression allowance')
    metric_order = critical + ['signal_hpwl', 'ratsnest_crossings']
    if baseline['metrics']['alignment_penalty']['value'] is not None:
        metric_order.append('alignment_penalty')
    metric_order.append('movement')
    if policy.get('priority_metrics') not in (None, metric_order):
        raise ValueError('P0 tier order cannot be overridden by a scalar or visual metric')
    if not isinstance(metric_order, list) or any(key not in baseline["metrics"] for key in metric_order):
        raise ValueError("Priority metrics must exist in the evaluator report")
    tradeoffs, eligible = [], []
    for report in evaluations:
        regressions = []
        for key in critical:
            before, after = baseline["metrics"][key]["value"], report["metrics"][key]["value"]
            if before is None or after is None:
                regressions.append({"metric": key, "reason": "critical metric unknown"})
            elif after > before:
                regressions.append({"metric": key, "before": before, "after": after,
                                    "tolerance_mm": tolerance.get(key, 0)})
        if not report["feasible"]:
            reason = "Hard constraints fail or required evidence is unknown"
        elif regressions:
            reason = "Critical path lower bound regressed; improved ordinary-net score cannot compensate"
        elif any(report["metrics"][key]["value"] is None for key in metric_order):
            reason = "A selection metric is unknown"
        elif report is not baseline and not any(report['metrics'][key]['value'] < baseline['metrics'][key]['value']
                for key in critical + ['signal_hpwl','ratsnest_crossings']):
            reason = 'No measurable Tier 1 or Tier 2 improvement; retain baseline'
        else:
            eligible.append(report); reason = "Eligible under hard constraints and critical nonregression"
        tradeoffs.append({"candidate_id": report["candidate_id"], "reason": reason, "critical_regressions": regressions,
                         "feasible": report["feasible"], "metrics": report["metrics"]})
    # Existing-board baseline wins ties and any proposal without a strict
    # improvement. No global scalar can compensate for an earlier tier.
    def key(report):
        return tuple(round(report["metrics"][m]["value"], 12) for m in metric_order) + (
            0 if report["candidate_id"] == "baseline" else 1, report["candidate_id"])
    winner = min(eligible, key=key) if eligible else baseline
    status = "blocked" if not eligible else "baseline_retained" if winner is baseline else "selected"
    plan = {"schema": 1, "baseline_snapshot_id": context.get("snapshot_id"), "input_digest": baseline["input_digest"],
            "constraint_digest": baseline["constraint_digest"], "candidate_id": winner["candidate_id"],
            "moves": winner["moves"] if eligible else [], "fixed_objects": sorted(c["id"] for c in context["components"] if c.get("fixed") or c.get("locked")),
            "reservations": winner["reservations"], "required_evidence_id": winner["evidence_id"],
            "search": {"method": "existing_planner_proposals", "candidate_count": len(evaluations),
                       "seeds": [candidate.get("seed") for candidate in candidates]},
            "apply_eligible": bool(eligible) and winner["feasible"]}
    plan["plan_digest"] = digest(plan)
    return {"schema": 1, "policy_version": POLICY_VERSION, "input_digest": baseline["input_digest"],
            "baseline_id": "baseline", "selected_candidate_id": winner["candidate_id"],
            "selection_status": status, "evaluations": evaluations, "tradeoffs": tradeoffs,
            "profiles": select_profiles(intent), "plan": plan}


def generate_placement_candidates(context, intent=None, options=None):
    """Bounded restarts of the existing refine/construct engines.

    Solver rectangles and zero unspecified proposal clearance are computational
    proxies, never qualified bodies or fabrication limits. Every output must
    pass :func:`evaluate_placement` against the unchanged real board geometry.
    No side change, outline resize, Altium mutation or cross-project state.
    """
    import random
    from eda_agent.units import MM_PER_MIL
    from eda_agent.placement.autoplace import BoardRegion, PlaceComp, PlaceNet, PlacePin, PlaceOptions, plan_placement
    from eda_agent.design.pcb_placement import ConstructOptions, DesignRules, construct_placement

    context = validate_board_context(context)
    intent = validate_design_intent(intent, context)
    options = dict(options or {})
    count, seed = options.get("candidate_count", 3), options.get("seed", 0)
    iterations = options.get("iterations", 60)
    for name, value, maximum in (("candidate_count", count, 16), ("seed", seed, 2**32 - 17), ("iterations", iterations, 2000)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"Invalid computational {name} budget")
    mode = options.get("mode", "refine")
    if mode not in {"refine", "construct"}:
        raise ValueError("Generator mode must be refine or construct")
    outline = context.get("outline")
    if outline is None:
        raise ValueError("An explicit existing outline is required; the generator never sizes a replacement board")
    pitch = _number(options.get("search_grid_mm", .127), "search grid", positive=True)
    radius = _number(options.get("restart_radius_mm", 0), "restart radius", minimum=0)
    minimum = options.get("proposal_clearance_mm")
    if minimum is not None:
        minimum = _number(minimum, "proposal clearance", minimum=0)
    scale = 1 / MM_PER_MIL
    bbox = _bbox(outline["outline"])
    origin = _point(context.get("origin_mm", [0, 0]), "board origin")
    region = BoardRegion((bbox[0]-origin[0])*scale, (bbox[1]-origin[1])*scale,
                         (bbox[2]-origin[0])*scale, (bbox[3]-origin[1])*scale)
    base_poses = {c["id"]: _pose(c) for c in context["components"]}
    pad_map = transformed_pads(context, base_poses)
    locals_by_comp = defaultdict(list)
    for p in pad_map.values():
        cid = p.get("component_id")
        if cid is None:
            continue  # Stationary board copper is audited, never proposed as a package.
        if p.get("x_mm") is None:
            raise ValueError("Known pad transforms required to generate pin-aware proposals")
        local = inverse_transform_point(geom.xy(p), base_poses[cid])
        locals_by_comp[cid].append((p, local))
    templates, centers = [], {}
    scoped = set(context["scope_component_ids"])
    roles = {}
    for block in intent.get("blocks", []):
        if _qualified(block.get("source")):
            for cid in block.get("component_ids", []):
                roles[cid] = block.get("placement_role", "")
    for c in context["components"]:
        cid = c["id"]
        if c.get("side") not in {"top", "bottom"}:
            raise ValueError("Known component sides required; unknown is not a top-side default")
        local_ring = c.get("courtyard_local")
        proxy = False
        if local_ring is None:
            points = []
            for pad, _ in locals_by_comp[cid]:
                if pad.get("outline"):
                    points.extend(inverse_transform_point(p, base_poses[cid]) for p in pad["outline"])
            if not points:
                raise ValueError(f"No package envelope or physical pad geometry for {cid}")
            local_ring = points
            proxy = True
        x1, y1, x2, y2 = _bbox(local_ring)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("A noncollapsed package/pad envelope is required for proposal generation")
        local_center = ((x1 + x2) / 2, (y1 + y2) / 2)
        centers[cid] = local_center
        world_center = transform_point(local_center, base_poses[cid])
        pins = tuple(PlacePin((point[0] - local_center[0]) * scale,
                             (point[1] - local_center[1]) * scale, pad.get("net_name", ""))
                     for pad, point in locals_by_comp[cid] if pad.get("net_name"))
        comp = PlaceComp(ref=cid, w=(x2-x1)*scale, h=(y2-y1)*scale,
            cx=(world_center[0]-origin[0])*scale, cy=(world_center[1]-origin[1])*scale,
            layer="Top" if c["side"] == "top" else "Bottom",
            fixed=c.get("fixed", False) or c.get("locked", False) or cid not in scoped,
            rotation=c["rotation_deg"], pins=pins, rotatable=len(c.get("allowed_rotations_deg", [])) > 1)
        comp.flippable = False
        comp.role = roles.get(cid, "")
        comp.courtyard_proxy = proxy
        templates.append(comp)
    net_members = defaultdict(set)
    for pad in context["pads"]:
        if pad.get("net_name"):
            net_members[pad["net_name"]].add(pad["component_id"])
    excluded = set(intent.get("signal_attraction_excluded_nets", []))
    nets = [PlaceNet(tuple(sorted(members)), name=name, weight=0.0 if name in excluded else 1.0)
            for name, members in sorted(net_members.items())]
    output = []
    for restart in range(count):
        actual_seed = seed + restart
        rng = random.Random(actual_seed)
        comps = copy.deepcopy(templates)
        if mode == "refine" and restart and radius:
            for comp in comps:
                if not comp.fixed:
                    comp.cx += rng.uniform(-radius, radius) * scale
                    comp.cy += rng.uniform(-radius, radius) * scale
        if mode == "construct":
            rules = DesignRules(layers=2, grid=pitch*scale, courtyard_clr=0,
                                edge_clr=0, component_clr=(minimum or 0)*scale)
            opts = ConstructOptions(seed=actual_seed, max_moves=iterations,
                                    max_grow_steps=0, move_flip=0)
            result = construct_placement(comps, nets, rules, opts,
                                         fixed_region=region, preserve_sides=True)
            positions, rotations = result.centroids, result.rotations
        else:
            opts = PlaceOptions(iterations=iterations, grid_mils=pitch*scale,
                clearance_mils=(minimum or 0)*scale, optimize_rotation=True)
            result = plan_placement(comps, nets, region, opts)
            positions, rotations = result.positions, result.rotations
        moves = []
        for c in context["components"]:
            cid = c["id"]
            if c.get("fixed") or c.get("locked") or cid not in scoped:
                continue
            angle = rotations.get(cid, c["rotation_deg"])
            local_center = centers[cid]
            # Both solvers return centroid positions; derive the origin in the
            # explicit mirrored footprint frame instead of trusting a bbox.
            dx, dy = _rotate_offset(local_center[0] if c["side"] == "top" else -local_center[0],
                                    local_center[1], angle)
            center = positions[cid]
            moves.append({"component_id": cid, "designator": c.get("reference", c.get("designator")),
                          "x_mm": center[0] / scale + origin[0] - dx,
                          "y_mm": center[1] / scale + origin[1] - dy, "rotation_deg": angle, "side": c["side"]})
        output.append({"candidate_id": f"{mode}:{actual_seed}", "moves": moves, "seed": actual_seed,
            "solver": "eda_agent.design.pcb_placement" if mode == "construct" else "eda_agent.placement.autoplace",
            "budget": {"iterations": iterations, "candidate_count": count}, "proxy_only": True,
            "proposal_policy": {"search_grid_mm": pitch, "restart_radius_mm": radius,
                "engineering_clearance_mm": minimum, "clearance_qualified": minimum is not None,
                "unknown_clearance_solver_value": 0 if minimum is None else None,
                "search_grid_origin_mm": origin, "board_resize_allowed": False, "side_change_allowed": False},
            "unqualified_envelope_components": [c.ref for c in comps if c.courtyard_proxy]})
    return output


def contract_schemas():
    """Versioned JSON schemas for the implemented core, not invented MCP names."""
    number = {"type": "number"}
    point = {"type": "array", "items": number, "minItems": 2, "maxItems": 2}
    source = {"type": "object", "required": ["kind", "id"], "properties": {
        "kind": {"enum": ["engineering_policy", "user_intent", "schematic", "datasheet", "standard", "measured_geometry"]},
        "id": {"type": "string", "minLength": 1}, "revision": {"type": "string"}, "page": {"type": ["string", "integer"]}}}
    component = {"type": "object", "required": ["id", "x_mm", "y_mm", "rotation_deg"], "properties": {
        "id": {"type": "string", "minLength": 1}, "reference": {"type": "string"}, "x_mm": number, "y_mm": number,
        "rotation_deg": number, "side": {"enum": ["top", "bottom", None]}, "fixed": {"type": "boolean"},
        "courtyard_local": {"type": ["array", "null"], "items": point, "minItems": 3}, "courtyard_source": source}}
    pad = {"type": "object", "required": ["id", "component_id"], "properties": {
        "id": {"type": "string", "minLength": 1}, "component_id": {"type": "string", "minLength": 1},
        "net_name": {"type": "string"}, "local_xy_mm": point, "x_mm": number, "y_mm": number,
        "width_mm": {"type": "number", "exclusiveMinimum": 0}, "height_mm": {"type": "number", "exclusiveMinimum": 0},
        "rotation_deg": number, "shape": {"type": "string"}, "layer": {"type": ["string", "null"]}},
        "anyOf": [{"required": ["local_xy_mm"]}, {"required": ["x_mm", "y_mm"]}]}
    move = {"type": "object", "required": ["component_id"], "additionalProperties": False, "properties": {
        "component_id": {"type": "string", "minLength": 1}, "x_mm": number, "y_mm": number,
        "designator": {"type": ["string", "null"]},
        "rotation_deg": number, "side": {"enum": ["top", "bottom"]}}}
    base = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    board = {**base, "required": ["units", "components"], "properties": {
        "units": {"const": "mm"}, "components": {"type": "array", "items": component, "minItems": 1, "maxItems": 2000},
        "pads": {"type": "array", "items": pad, "maxItems": 100000},
        "stationary_pads": {"type": "array", "maxItems": 100000, "items": {
            **pad, "required": ["id", "x_mm", "y_mm"],
            "properties": {k: v for k, v in pad["properties"].items() if k not in {"component_id", "local_xy_mm"}},
            "not": {"anyOf": [{"required": ["component_id"]}, {"required": ["local_xy_mm"]}]}}}, "snapshot_id": {"type": ["string", "null"]},
        "board_path": {"type": "string"}, "board_identity": {"type": ["string", "null"]},
        "scope_component_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "outline": {"oneOf": [{"type": "array", "items": point, "minItems": 3},
            {"type": "object", "required": ["outline"], "properties": {"outline": {"type": "array", "items": point, "minItems": 3},
             "holes": {"type": "array", "items": {"type": "array", "items": point, "minItems": 3}}}}, {"type": "null"}]}}}
    intent = {**base, "properties": {"blocks": {"type": "array", "items": {"type": "object", "required": ["id"],
        "properties": {"id": {"type": "string", "minLength": 1}, "component_ids": {"type": "array", "items": {"type": "string"}},
        "profiles": {"type": "array", "items": {"enum": list(PROFILE_INPUTS)}}, "source": source, "requirements": {"type": "object"}}}},
        "critical_paths": {"type": "array", "items": {"type": "object", "required": ["id", "pad_ids"],
        "properties": {"id": {"type": "string", "minLength": 1}, "pad_ids": {"type": "array", "minItems": 2, "items": {"type": "string"}}}}}}}
    constraint = {**base, "required": ["id"], "properties": {"id": {"type": "string", "minLength": 1},
        "kind": {"enum": ["hard", "soft"]}, "source": source, "applicability": {"type": ["boolean", "null"]},
        "check": {"type": "string"}, "scope": {"type": "object"}, "limit_mm": {"type": ["number", "null"], "minimum": 0},
        "tolerance_mm": {"type": "number", "minimum": 0}, "measure": {"enum": ["center", "edge"]}, "relation": {"enum": ["min", "max"]}}}
    return {"policy_version": POLICY_VERSION, "BoardContext": board, "DesignIntent": intent,
            "Constraint": constraint, "Move": move, "Source": source}


def example_board_context():
    """Synthetic mm fixture, deliberately unrelated to any part or project."""
    source = {"kind": "engineering_policy", "id": "synthetic-regression", "revision": "1"}
    return {"schema": 1, "units": "mm", "snapshot_id": "synthetic-baseline", "source": "synthetic",
        "fabrication_profile": {"minimum_pad_clearance_mm": .1, "source": source},
        "board_path": "synthetic.PcbDoc", "board_identity": "synthetic-only", "document_modified": None,
        "origin_mm": [10, 20], "outline": [[10, 20], [30, 20], [30, 40], [10, 40]], "cutouts": [],
        "coverage": {"outline": "complete", "routing_obstacles": "complete"},
        "components": [{"id": cid, "reference": ref, "x_mm": x, "y_mm": 25, "rotation_deg": 0,
                        "side": "top", "fixed": False, "courtyard_local": [[-1,-1],[1,-1],[1,1],[-1,1]],
                        "courtyard_source": source} for cid, ref, x in [("a", "U_A", 14), ("b", "R_B", 24)]],
        "pads": [{"id": cid+".1", "component_id": cid, "pad_name": "1", "net_name": "SIGNAL", "local_xy_mm": [0,0],
                  "width_mm": .5, "height_mm": .5, "shape": "rectangle", "rotation_deg": 0, "layer": "Top"} for cid in ("a", "b")]}


__all__ = ["POLICY_VERSION", "digest", "validate_board_context", "validate_design_intent", "select_profiles",
           "transform_point", "inverse_transform_point", "snap_to_origin_grid", "transformed_pads",
           "evaluate_placement", "compare_placement_candidates", "evaluate_channels", "generate_placement_candidates",
           "contract_schemas", "example_board_context"]
