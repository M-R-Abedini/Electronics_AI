# Modified 2026-11-02 by M-R-Abedini; see root MODIFICATIONS.md.
# SPDX-License-Identifier: MIT
"""Compose existing sizing engines into evidence-bearing copper constraints.

All geometry uses mils. Native rules contain only fields supported by
pcb_create_design_rule; unsupported coupled/skew constraints remain explicit.
"""
from __future__ import annotations

import math
import re
import hashlib
from copy import deepcopy
from ..units import OZ_TO_MILS
from .impedance_sizing import trace_width_for_impedance, impedance_validity
from .length_matching import assess_length_match
from .trace_sizing import trace_width_for_current, trace_width_for_current_qualified


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def _scope(net):
    return "InNet('" + net.replace("'", "''") + "')"


def _names(values):
    """Keep ordinary names legible; disambiguate sanitization collisions."""
    sanitized = {v: re.sub(r"[^A-Za-z0-9]", "_", v) for v in values}
    return {v: s + ("_" + hashlib.sha256(v.encode()).hexdigest()[:10] if sum(other == s for other in sanitized.values()) > 1 else "") for v, s in sanitized.items()}


def managed_class_name(tag):
    """Stable reserved name independent of other tags in the manifest.

    Always include the digest: adding a later sanitization collision must not
    rename an already-owned class or accidentally adopt a user's class.
    """
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("net-class tags and match groups must be nonempty strings")
    readable = re.sub(r"[^A-Za-z0-9]", "_", tag)[:40]
    return "MCP_SIPI_" + readable + "_" + hashlib.sha256(tag.encode("utf-8")).hexdigest()[:10]


def _snapshot_geometries(snapshot, net, constraint):
    coverage = snapshot.get("coverage", {})
    coverage = coverage.get("sections", coverage)
    covered = coverage.get("layers")
    if isinstance(covered, dict):
        covered = covered.get("complete") is True or covered.get("status") == "complete"
    if covered not in (True, "complete"):
        return {}, "stackup layer coverage is incomplete"
    layers = sorted(snapshot.get("layers", []), key=lambda row: row.get("order", -1))
    if not layers or any("order" not in row for row in layers) or len({row["order"] for row in layers}) != len(layers):
        return {}, "ordered physical layer stack unavailable"
    copper = [row for row in layers if row.get("kind") in {"signal", "plane", "copper"}]
    used = set(constraint.get("routing_layers", []))
    used.update(t.get("layer") for t in snapshot.get("tracks", []) + snapshot.get("arcs", []) if t.get("net_name") == net)
    if constraint.get("routing_layer"):
        used.add(constraint["routing_layer"])
    if not used:
        return {}, "net routing layer is not established"
    derived = {}
    for name in sorted(used):
        layer = next((row for row in copper if row.get("name") == name), None)
        if not layer:
            return {}, f"routing layer {name!r} absent from copper stack"
        position = copper.index(layer)
        neighbors = ([copper[position-1]] if position else []) + ([copper[position+1]] if position+1 < len(copper) else [])
        refs = constraint.get("reference_layers", layer.get("reference_layers", []))
        if constraint.get("reference_layer", layer.get("reference_layer")):
            refs = [constraint.get("reference_layer", layer.get("reference_layer"))]
        qualified = [ref for ref in neighbors if ref.get("reference_net", ref.get("refnet")) and (not refs or ref["name"] in refs)]
        outer = position in (0, len(copper)-1)
        if len(qualified) != (1 if outer else 2):
            return {}, f"{name}: actual adjacent reference-plane binding unavailable"
        spans = []
        for ref in qualified:
            low, high = sorted((layer["order"], ref["order"]))
            span = [row for row in layers if low < row["order"] < high]
            if not span or any(row.get("kind") != "dielectric" or not row.get("thickness_mm") or not row.get("er") for row in span):
                return {}, f"{name}: dielectric thickness/Er unavailable"
            ers = {_positive(row["er"], "dielectric Er") for row in span}
            if len(ers) != 1:
                return {}, f"{name}: mixed dielectric Er requires a qualified field solver"
            spans.append((sum(_positive(row["thickness_mm"], "dielectric thickness") for row in span), ers.pop()))
        if not outer and (abs(spans[0][0]-spans[1][0]) > 1e-9 or spans[0][1] != spans[1][1]):
            return {}, f"{name}: asymmetric stripline requires a qualified field solver"
        thickness = _positive(layer.get("copper_thickness_mm", layer.get("thickness_mm")), "copper thickness")
        height = spans[0][0] if outer else sum(span[0] for span in spans) + thickness
        derived[name] = {"geometry": "microstrip" if outer else "stripline", "dielectric_height_mils": height/.0254,
                         "dielectric_constant": spans[0][1], "copper_oz": thickness/.0254/OZ_TO_MILS,
                         "reference_layers": [ref["name"] for ref in qualified]}
    return derived, None


def _snapshot_current_copper(snapshot, net, constraint):
    """Current sizing needs actual copper thickness even without plane binding."""
    coverage = snapshot.get("coverage", {})
    row = coverage.get("sections", coverage).get("layers")
    if isinstance(row, dict):
        row = row.get("complete") is True or row.get("status") == "complete"
    if row not in (True, "complete"):
        return None, None
    copper = sorted([layer for layer in snapshot.get("layers", []) if layer.get("kind") in {"signal", "plane", "copper"}], key=lambda layer: layer.get("order", -1))
    if not copper or any("order" not in layer for layer in copper) or len({layer["order"] for layer in copper}) != len(copper):
        return None, None
    used = set(constraint.get("routing_layers", []))
    used.update(t.get("layer") for t in snapshot.get("tracks", []) + snapshot.get("arcs", []) if t.get("net_name") == net)
    if constraint.get("routing_layer"):
        used.add(constraint["routing_layer"])
    selected = [layer for layer in copper if layer["name"] in used]
    if not used or len(selected) != len(used) or any(not layer.get("copper_thickness_mm", layer.get("thickness_mm")) for layer in selected):
        return None, None
    weight = min(_positive(layer.get("copper_thickness_mm", layer.get("thickness_mm")), "copper thickness")/.0254/OZ_TO_MILS for layer in selected)
    internal = any(layer not in (copper[0], copper[-1]) for layer in selected)
    return weight, "internal" if internal else "external"


def synthesize_copper_rules(snapshot: dict, classification: dict, options: dict | None = None) -> dict:
    """Return native rules, per-net intent, 3W/5W matrix, locks and coverage.

    Inputs: classification ``net_classes`` (or ``by_net``) and optional
    ``constraints``; options ``constraints`` override only explicit values.
    Stackup geometry is explicit per-net or options.stackup_geometry, never a
    guessed dielectric default. Current defaults to requiring qualified 2152
    data; selecting current_model='IPC-2221' explicitly enables legacy sizing.
    """
    opts = dict(options or {})
    raw = classification.get("net_classes", classification.get("by_net", {}))
    classes = {str(n): ([v] if isinstance(v, str) else list(v)) for n, v in raw.items()}
    constraints = deepcopy(classification.get("constraints", {}))
    for n, values in opts.get("constraints", {}).items():
        constraints.setdefault(n, {}).update(values)
    pairs = deepcopy(opts.get("differential_pairs", classification.get("differential_pairs", [])))
    declared = set(classes) | set(classification.get("constraints", {}))
    known_names = {n if isinstance(n, str) else n.get("name", n.get("net_name")) for n in snapshot.get("nets", [])}
    declared.update(known_names - {None})
    if set(opts.get("constraints", {})) - declared or set(opts.get("critical_nets", [])) - declared:
        raise ValueError("Option constraints/critical nets reference unknown net names")
    pair_names_seen = set()
    for pair in pairs:
        members = pair.get("nets", [])
        if len(members) != 2 or len(set(members)) != 2 or set(members) - declared:
            raise ValueError("Differential pairs require two distinct declared net names")
        name = pair.get("name")
        if name:
            if name in pair_names_seen:
                raise ValueError("Differential pair names must be unique")
            pair_names_seen.add(name)
        if pair.get("target_zdiff_ohm") is None:
            continue
        target = _positive(pair["target_zdiff_ohm"], "pair target_zdiff_ohm")
        members = pair.get("nets", [])
        if len(members) != 2 or len(set(members)) != 2:
            raise ValueError("A differential target requires two distinct pair nets")
        for net in members:
            values = constraints.setdefault(net, {})
            if "target_zdiff_ohm" in values and values["target_zdiff_ohm"] != target:
                raise ValueError("Conflicting per-net and pair differential impedance targets")
            values["target_zdiff_ohm"] = target
            if pair.get("gap_mils") is not None:
                spacing = _positive(pair["gap_mils"], "pair gap")
                if "spacing_mils" in values and values["spacing_mils"] != spacing:
                    raise ValueError("Conflicting per-net and pair differential gaps")
                values["spacing_mils"] = spacing
    for net in constraints:
        classes.setdefault(net, ["unclassified"])
    known = snapshot.get("nets", [])
    if isinstance(known, dict):
        known = list(known)
    for net in known:
        name = net if isinstance(net, str) else net.get("name", net.get("net_name"))
        if name:
            classes.setdefault(name, ["unclassified"])
    floor = _positive(opts.get("min_width_mils", 6), "min_width_mils")
    gap = _positive(opts.get("min_clearance_mils", 6), "min_clearance_mils")
    rules, evidence, unsupported, critical = [], list(classification.get("evidence", [])), [], set(opts.get("critical_nets", []))
    unsupported.extend({"constraint": "classification", "reason": str(item)} for item in classification.get("unknowns", []))
    widths, matched_groups, differential_widths = {}, {}, {}
    qualified_differential_nets = set()
    explicit_width_nets = {net for net, values in constraints.items() if 'required_width_mils' in values}
    net_names = _names(classes)
    for net, tags in sorted(classes.items()):
        constraint = constraints.setdefault(net, {})
        tagset = {str(t).lower() for t in tags}
        if not tagset or tagset & {"unknown", "unclassified"}:
            unsupported.append({"net": net, "constraint": "classification", "reason": "physical electrical intent is unclassified"})
        if tagset & {"analog_sensitive", "analog", "sensitive_analog", "feedback", "switch", "aggressive_switching", "switch_node", "high_current", "high_current_power", "high_speed_diff", "high_speed_single", "differential", "clock", "rf", "gate_drive"}:
            critical.add(net)
        width = _positive(constraint.get("required_width_mils", floor), f"{net} width")
        derived, derivation_error = _snapshot_geometries(snapshot, net, constraint)
        if "current_a" in constraint:
            critical.add(net)
            model = constraint.get("current_model", opts.get("current_model", "IPC-2152"))
            copper = constraint.get("copper_oz", opts.get("copper_oz"))
            observed_copper, observed_layer = _snapshot_current_copper(snapshot, net, constraint)
            if copper is None and observed_copper is not None:
                copper = observed_copper
                constraint["copper_oz"] = copper
                evidence.append({"net": net, "source": "snapshot copper thickness; conservative minimum across used layers", "copper_oz": copper})
            dt = constraint.get("delta_t_c", opts.get("delta_t_c"))
            layer = constraint.get("layer", opts.get("layer", "external"))
            if "layer" not in constraint and "layer" not in opts and observed_layer:
                layer = observed_layer
            if copper is None or dt is None:
                unsupported.append({"net": net, "constraint": "current", "reason": "explicit copper_oz and delta_t_c required"})
            elif model == "IPC-2221":
                result = trace_width_for_current(constraint["current_a"], copper_oz=copper, delta_t_c=dt, layer=layer, margin=opts.get("track_margin", .2))
                width = max(width, result.recommended_width_mils)
                constraint["sizing_model"] = "IPC-2221 (legacy; not IPC-2152)"
                evidence.append({"net": net, "calculator": "trace_sizing.trace_width_for_current", "model": constraint["sizing_model"], "width_mils": width})
            elif model == "IPC-2152":
                result = trace_width_for_current_qualified(constraint["current_a"], dataset=constraint.get("ipc2152_dataset", opts.get("ipc2152_dataset")), copper_oz=copper, delta_t_c=dt, layer=layer, margin=opts.get("track_margin", .2))
                if result["supported"]:
                    width = max(width, result["recommended_width_mils"])
                    constraint["sizing_model"] = result["model"]
                    evidence.append({"net": net, "calculator": "trace_sizing.trace_width_for_current_qualified", **result})
                else:
                    unsupported.append({"net": net, "constraint": "current", "reason": result["reason"]})
            else:
                raise ValueError("current_model must be IPC-2152 or IPC-2221")
        target = constraint.get("target_z0_ohm", constraint.get("target_impedance_ohms"))
        differential_target = constraint.get("target_zdiff_ohm")
        if differential_target is not None:
            if target is not None:
                raise ValueError("Specify single-ended or differential impedance, not both")
            target = differential_target
        if target is None and tagset & {"high_speed_single", "high_speed_diff", "differential", "clock", "rf"}:
            unsupported.append({"net": net, "constraint": "impedance", "reason": "explicit single-ended or differential target impedance required; default fabrication width is not impedance qualification"})
        minimum_current_width = max(width, floor)
        geometry = dict(opts.get("stackup_geometry", {}))
        geometry.update(constraint.get("stackup_geometry", {}))
        layer_widths = {}
        if not geometry and derived:
            first = next(iter(derived.values()))
            geometry = {key: value for key, value in first.items() if key != "reference_layers"}
            constraint["stackup_geometries"] = derived
            constraint["reference_layers"] = sorted({ref for row in derived.values() for ref in row["reference_layers"]})
            evidence.append({"net": net, "source": "complete ordered snapshot.layers and actual adjacent reference-net binding", "derived_stackup_geometries": derived})
        if target is not None:
            target = _positive(target, "target impedance")
            critical.add(net)
            if differential_target is not None:
                if geometry.get("geometry") in {"microstrip", "stripline"}:
                    geometry["geometry"] += "_diff"
                if constraint.get("spacing_mils") is not None:
                    geometry["spacing_mils"] = _positive(constraint["spacing_mils"], "differential spacing")
            required = {"geometry", "dielectric_height_mils", "dielectric_constant", "copper_oz"}
            if differential_target is not None:
                required.add("spacing_mils")
            if not required <= geometry.keys():
                unsupported.append({"net": net, "constraint": "impedance", "reason": derivation_error or "explicit stackup geometry, dielectric height/Er and copper weight required"})
            else:
                result = trace_width_for_impedance(target, **geometry)
                validity = impedance_validity(result.single_ended_z0_ohms, result.width_mils, result.dielectric_height_mils, result.dielectric_constant)
                if not result.feasible or not validity["usable"] or validity["outside_validity_range"] or result.width_mils < width:
                    unsupported.append({"net": net, "constraint": "impedance", "reason": "infeasible/outside closed-form validity or incompatible minimum current/fab width"})
                else:
                    width = result.width_mils
                    constraint["max_width_mils"] = width
                    evidence.append({"net": net, "calculator": "impedance_sizing.trace_width_for_impedance", "model": "IPC-2141 approximate; field solver confirmation required", "width_mils": width, "target_ohms": target})
            if derived and not constraint.get("stackup_geometry") and not opts.get("stackup_geometry"):
                for layer_name, source_geometry in derived.items():
                    layer_geometry = {key: value for key, value in source_geometry.items() if key != "reference_layers"}
                    if differential_target is not None:
                        if "spacing_mils" not in geometry:
                            continue
                        layer_geometry["geometry"] += "_diff"
                        layer_geometry["spacing_mils"] = geometry["spacing_mils"]
                    sized = trace_width_for_impedance(target, **layer_geometry)
                    validity = impedance_validity(sized.single_ended_z0_ohms, sized.width_mils, sized.dielectric_height_mils, sized.dielectric_constant)
                    if not sized.feasible or not validity["usable"] or validity["outside_validity_range"] or sized.width_mils < minimum_current_width:
                        unsupported.append({"net": net, "layer": layer_name, "constraint": "impedance", "reason": "layer impedance geometry is infeasible/outside validity/fab floor"})
                        continue
                    layer_widths[layer_name] = {"required_width_mils": sized.width_mils, "max_width_mils": sized.width_mils, "reference_layers": source_geometry["reference_layers"]}
                constraint["layer_widths"] = layer_widths
        applied_width = max(width, floor)
        constraint["required_width_mils"] = applied_width
        observed_widths = []
        for track in snapshot.get("tracks", []) + snapshot.get("arcs", []):
            if track.get("net_name", track.get("net")) != net:
                continue
            if track.get("width_mm") is not None:
                observed_widths.append(_positive(track["width_mm"], "observed copper width") / .0254)
            elif track.get("width_mils") is not None:
                observed_widths.append(_positive(track["width_mils"], "observed copper width"))
        widths[net] = max([applied_width] + observed_widths)
        if "skew_budget_ps" in constraint:
            if "dielectric_constant" not in geometry:
                unsupported.append({"net": net, "constraint": "matched_length", "reason": "explicit dielectric_constant required"})
            else:
                match = assess_length_match(dielectric_constant=geometry["dielectric_constant"], geometry=geometry.get("geometry", "stripline").replace("_diff", ""), width_mils=width, dielectric_height_mils=geometry.get("dielectric_height_mils"), skew_budget_ps=constraint["skew_budget_ps"])
                constraint["length_tolerance_mils"] = match["tolerance_mils"]
                evidence.append({"net": net, "calculator": "length_matching.assess_length_match", **match})
        if "length_tolerance_mils" in constraint:
            critical.add(net)
            group = constraint.get("match_group")
            if group:
                managed_class_name(group)
                if group not in classes[net]:
                    classes[net].append(group)
                tolerance = _positive(constraint["length_tolerance_mils"], "length_tolerance_mils")
                matched_groups[group] = min(matched_groups.get(group, tolerance), tolerance)
            else:
                unsupported.append({"net": net, "constraint": "matched_length", "reason": "explicit match_group net-class name required"})
        name = "Copper_" + net_names[net]
        if layer_widths:
            for layer_name, layer_values in sorted(layer_widths.items()):
                layer_width = layer_values["required_width_mils"]
                rules.append({"name": f"{name}_Width_{len(rules)}", "rule_type": "width", "value": layer_width, "favored_value": layer_width, "max_value": layer_values["max_width_mils"], "scope": _scope(net) + " And OnLayer('" + layer_name.replace("'", "''") + "')"})
            widths[net] = max(widths[net], *(v["required_width_mils"] for v in layer_widths.values()))
            constraint["required_width_mils"] = min(v["required_width_mils"] for v in layer_widths.values())
            constraint["max_width_mils"] = max(v["max_width_mils"] for v in layer_widths.values())
        else:
            rules.append({"name": f"{name}_Width_{len(rules)}", "rule_type": "width", "value": applied_width, "favored_value": applied_width, "max_value": applied_width if target is not None else constraint.get("max_width_mils", applied_width*5), "scope": _scope(net)})
        if differential_target is not None:
            if not any(item.get("net") == net and item.get("constraint") == "impedance" for item in unsupported):
                differential_widths[net] = [v["required_width_mils"] for v in layer_widths.values()] or [applied_width]
                qualified_differential_nets.add(net)
        elif net in explicit_width_nets:
            differential_widths[net] = [applied_width]
    class_names = {tag: managed_class_name(tag) for tag in sorted({tag for tags in classes.values() for tag in tags})}
    for group, tolerance in sorted(matched_groups.items()):
        native_class = class_names[group]
        rules.append({"name": "Copper_Match_" + native_class, "rule_type": "matched_lengths", "value": tolerance, "scope": "InNetClass('" + native_class + "')"})
    sensitive = {n for n, tags in classes.items() if {str(t).lower() for t in tags} & {"analog", "analog_sensitive", "sensitive_analog", "feedback", "rf"}}
    aggressors = {n for n, tags in classes.items() if {str(t).lower() for t in tags} & {"switch", "aggressive_switching", "switch_node", "clock", "high_current", "high_current_power", "high_speed_single", "high_speed_diff", "gate_drive", "differential", "digital_fast"}}
    matrix = []
    nets = sorted(classes)
    matrix_omitted = len(nets)*(len(nets)-1)//2 + len(rules) + len(pairs) > 2000
    if matrix_omitted:
        unsupported.append({"constraint": "native_rule_budget", "reason": "exact per-net clearance matrix exceeds the 2000-rule native batch budget; matrix omitted, domain compaction requires separate qualification"})
    for i, left in enumerate(nets):
        if matrix_omitted:
            break
        for right in nets[i+1:]:
            factor = 5 if (left in sensitive and right in aggressors) or (right in sensitive and left in aggressors) else 3
            separation = max(gap, factor*max(widths[left], widths[right]))
            pair = next((p for p in pairs if set(p.get("nets", [])) == {left, right} and p.get("gap_mils") is not None), None)
            basis = "edge-to-edge heuristic; not an SI guarantee"
            if pair:
                separation, factor = _positive(pair["gap_mils"], "pair gap"), None
                basis = "explicit differential-pair coupling gap; 3W/5W applies between domains"
                if separation < gap:
                    unsupported.append({"constraint": "differential_pair", "reason": "pair gap below explicit fabrication clearance floor"})
            matrix.append({"net_a": left, "net_b": right, "clearance_mils": separation, "width_multiple": factor, "basis_width_mils": max(widths[left], widths[right]), "basis": basis})
            rules.append({"name": f"Copper_Domain_{len(rules)}", "rule_type": "clearance", "value": separation, "scope": _scope(left), "scope2": _scope(right), "net_scope": "different_nets"})
    for net in nets:
        clearance = max([gap] + [m["clearance_mils"] for m in matrix if net in (m["net_a"], m["net_b"])])
        constraints[net]["clearance_mils"] = clearance
    pair_names = _names([p["name"] for p in pairs if p.get("name")])
    for pair in pairs:
        critical.update(pair.get("nets", []))
        name = pair.get("name")
        if name and pair.get("gap_mils") is not None:
            pgap = _positive(pair["gap_mils"], "pair gap")
            rule = {"name": "Copper_Diff_" + pair_names[name], "rule_type": "differential_pairs", "value": pgap, "max_value": pgap, "favored_value": pgap, "scope": "InDifferentialPair('" + name.replace("'", "''") + "')"}
            width_keys = ("pair_width_min_mils", "pair_width_max_mils", "pair_width_preferred_mils")
            explicit = any(key in pair for key in width_keys)
            candidates = [value for net in pair["nets"] for value in differential_widths.get(net, [])]
            all_members_sized = all(net in differential_widths for net in pair["nets"])
            uniform = all_members_sized and max(candidates) - min(candidates) <= .00005
            controlled = any(constraints[net].get("target_zdiff_ohm") is not None for net in pair["nets"])
            if explicit:
                values = [_positive(pair.get(key), key) for key in width_keys]
                if not values[0] <= values[2] <= values[1] <= 200000:
                    raise ValueError("Differential pair widths require min <= preferred <= max <= 200000")
                rule.update(zip(width_keys, values))
            elif uniform:
                rule.update({key: candidates[0] for key in width_keys})
            else:
                unsupported.append({"constraint": "differential_pair_width", "pair": name,
                                    "reason": "matching explicit or qualified widths for both members/all used layers required; scalar pair width cannot represent missing or heterogeneous geometry"})
            if controlled and (not uniform or not all(net in qualified_differential_nets for net in pair["nets"])
                               or (explicit and any(abs(value - candidates[0]) > .00005 for value in values))):
                unsupported.append({"constraint": "differential_pair_width", "pair": name,
                                    "reason": "pair routing width must equal the qualified impedance width on both members/all used layers; differential rule omitted"})
            else:
                rules.append(rule)
        else:
            unsupported.append({"constraint": "differential_pair", "reason": "explicit existing Altium pair name and gap_mils required"})
    return {"ok": True, "net_classes": classes, "constraints": constraints, "rules": rules,
            "class_memberships": {class_names[tag]: sorted(n for n, tags in classes.items() if tag in tags) for tag in class_names},
            "class_name_map": class_names,
            "clearance_matrix": matrix, "differential_pairs": pairs, "critical_nets": sorted(critical),
            "coverage": {"complete": not unsupported, "unsupported": unsupported,
                         "native_clearance_mapping": "exact per-net pair Scope1/Scope2 and DSN class_class overrides",
                         "clearance_matrix_omitted": matrix_omitted},
            "evidence": evidence, "motifs": classification.get("motifs", []),
            "rule_evidence": [{"rule_name": r["name"], "scope": r["scope"], "scope2": r.get("scope2"), "units": "mils", "source": "existing sizing calculator or explicit minimum/domain spacing policy"} for r in rules],
            "board_modified": False}
