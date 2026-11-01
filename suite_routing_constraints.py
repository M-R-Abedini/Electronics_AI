"""Bounded Specctra parsing, rule injection and physical post-route evidence.

Only supported line paths/vias are reconstructed. Unknown copper, arcs, units
or incomplete snapshots fail closed. No invented net route-off syntax.
"""
from __future__ import annotations
from copy import deepcopy
import json
import math
import re
import hashlib


def parse_specctra(text):
    """Parse quoted names without regex substitution of board contents."""
    if len(text) > 128*1024*1024:
        raise ValueError("Specctra document too large")
    # Specctra exporter convention uses double quoted names; preserve the
    # parser's string_quote declaration rather than accepting unknown dialects.
    text = text.lstrip("\ufeff")
    roots, stack, cursor = [], [], 0
    while cursor < len(text):
        token = text[cursor]
        cursor += 1
        if token.isspace():
            continue
        if token == ";":
            end = text.find("\n", cursor)
            cursor = len(text) if end < 0 else end+1
            continue
        if token == "(":
            if len(stack) >= 256:
                raise ValueError("Specctra nesting exceeds limit")
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced Specctra document")
            stack.pop()
        else:
            if not stack:
                raise ValueError("Specctra atom outside envelope")
            if token == '"' and stack[-1] == ["string_quote"]:
                # Standard DSN declaration is (string_quote "), with a
                # single quote character rather than a quoted string.
                stack[-1].append('"')
                continue
            if token == '"':
                chars = []
                while cursor < len(text):
                    char = text[cursor]
                    cursor += 1
                    if char == '"':
                        break
                    if char == "\\" and cursor < len(text) and text[cursor] in ('"', "\\"):
                        char = text[cursor]
                        cursor += 1
                    chars.append(char)
                else:
                    raise ValueError("Malformed Specctra quoted name")
                token = "".join(chars)
            else:
                begin = cursor-1
                while cursor < len(text) and not text[cursor].isspace() and text[cursor] not in '()";':
                    cursor += 1
                token = text[begin:cursor]
            stack[-1].append(token)
    if stack or len(roots) != 1:
        raise ValueError("Incomplete Specctra document")
    if any(node != ["string_quote", '"'] for node in walk(roots[0], "string_quote")):
        raise ValueError("Unsupported Specctra string quote dialect")
    return roots[0]


def emit_specctra(node):
    if isinstance(node, list):
        if node == ["string_quote", '"']:
            return '(string_quote ")'
        return "(" + " ".join(emit_specctra(v) for v in node) + ")"
    value = str(node)
    if not value or any(c.isspace() or c in '()"\\;' for c in value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return value


def children(node, key):
    return [v for v in node if isinstance(v, list) and v and v[0] == key]


def walk(node, key):
    if isinstance(node, list):
        if node and node[0] == key:
            yield node
        for child in node:
            if isinstance(child, list):
                yield from walk(child, key)


def unit_scale(tree):
    """Return physical coordinate units to mm.

    DSN coordinates are in the declared unit; Freerouting SES coordinates
    are integer grid coordinates in unit/resolution. Primary writer:
    github.com/freerouting/freerouting, io/specctra/SesWriter.java (the
    session transform divides dsnToBoard(1) by communication.resolution).
    """
    resolutions = list(walk(tree, "resolution"))
    units = list(walk(tree, "unit"))
    declarations = resolutions or units
    if not declarations:
        raise ValueError("Explicit Specctra unit/resolution is required")
    factors = {"mil": .0254, "inch": 25.4, "mm": 1., "um": .001}
    resolved = set()
    for declaration in declarations:
        if len(declaration) < 2 or declaration[1] not in factors:
            raise ValueError("Unsupported Specctra units")
        divisor = 1.
        if declaration[0] == "resolution":
            if len(declaration) != 3:
                raise ValueError("Malformed Specctra resolution")
            number(declaration[2])
            if float(declaration[2]) <= 0:
                raise ValueError("Invalid Specctra resolution")
            if tree and tree[0] == "session":
                divisor = float(declaration[2])
        elif tree and tree[0] == "session":
            raise ValueError("SES requires explicit resolution, not unit alone")
        resolved.add(factors[declaration[1]]/divisor)
    if len(resolved) != 1:
        raise ValueError("Mixed Specctra coordinate units unsupported")
    return resolved.pop()


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite Specctra geometry")
    return result


def net_of(node, inherited=None):
    nets = children(node, "net")
    return nets[0][1] if nets and len(nets[0]) >= 2 else inherited


def copper_records(tree, *, inherited_scale=None):
    scale = unit_scale(tree) if inherited_scale is None else inherited_scale
    result = []
    def visit(node, net=None):
        if not isinstance(node, list) or not node:
            return
        if node[0] == "net" and len(node) > 1:
            net = node[1]
        if node[0] in ("wire", "via"):
            n = net_of(node, net)
            if n is None:
                raise ValueError("Copper without a resolved net is unsupported")
            if node[0] == "wire":
                paths = children(node, "path")
                if len(paths) != 1 or any(children(node, kind) for kind in ("polygon", "qarc", "rect", "circle")):
                    raise ValueError("Only line-path Specctra copper can be reconstructed")
                path = paths[0]
                if len(path) < 7 or (len(path)-3) % 2:
                    raise ValueError("Malformed Specctra path")
                width = number(path[2])*scale
                if width <= 0:
                    raise ValueError("Invalid Specctra width")
                points = [(number(path[i])*scale, number(path[i+1])*scale) for i in range(3, len(path), 2)]
                for a, b in zip(points, points[1:]):
                    result.append({"kind": "track", "net_name": n, "layer": path[1], "start": list(a), "end": list(b), "width_mm": width})
            else:
                if len(node) < 4:
                    raise ValueError("Malformed Specctra via")
                result.append({"kind": "via", "net_name": n, "padstack": node[1], "x_mm": number(node[2])*scale, "y_mm": number(node[3])*scale})
            return
        for child in node:
            if isinstance(child, list):
                visit(child, net)
    visit(tree)
    return result


def canonical(records, nets):
    output = []
    for r in records:
        if r["net_name"] not in nets:
            continue
        value = deepcopy(r)
        if value["kind"] == "track":
            value["start"], value["end"] = sorted([value["start"], value["end"]])
        def rounded(v):
            if type(v) in (int, float):
                return round(float(v), 9)
            if isinstance(v, list):
                return [rounded(x) for x in v]
            if isinstance(v, dict):
                return {k: rounded(x) for k, x in v.items()}
            return v
        output.append(json.dumps(rounded(value), sort_keys=True))
    return sorted(output)


def mapped_records(records, layer_map):
    result = deepcopy(records)
    for row in result:
        if "layer" in row:
            row["layer"] = layer_map.get(row["layer"], row["layer"])
    return result


def constrain_dsn(text, context, capabilities):
    tree = parse_specctra(text)
    if not tree or tree[0] != "pcb":
        raise ValueError("Expected Specctra PCB design")
    scale = unit_scale(tree)
    intent = context.get("intent", context)
    if intent.get("coverage", {}).get("complete") is False:
        raise ValueError("Constraint synthesis has unresolved coverage; routing refused")
    critical = set(intent.get("critical_nets", []))
    snapshot = context.get("snapshot", {})
    layer_map = context.get("layer_map", {})
    if critical and "exclude_nets" not in capabilities:
        # A complete graph proof must have been produced by the copper audit,
        # not inferred from a name, wire presence or an old unrouted count.
        proof = {}
        if snapshot:
            from eda_agent.design.copper_audit import net_connectivity
            proof = {n: net_connectivity(snapshot, n) for n in critical}
        if not all(proof.get(n, {}).get("connected") is True and proof[n].get("complete") is True for n in critical):
            raise ValueError("Unrouted/unproven critical nets require a qualified exclude_nets provider")
        actual = mapped_records(copper_records(tree), layer_map)
        expected_tracks = [{"kind": "track", **{k: t[k] for k in ("net_name", "layer", "start", "end", "width_mm")}} for t in snapshot.get("tracks", []) if t.get("net_name") in critical]
        if canonical([r for r in actual if r["kind"] == "track"], critical) != canonical(expected_tracks, critical):
            raise ValueError("Critical snapshot copper does not match input DSN")
        if any(a.get("net_name") in critical for a in snapshot.get("arcs", [])):
            raise ValueError("Critical arc DSN preservation is not qualified")
        expected_vias = sorted((v["net_name"], round(v["x_mm"], 9), round(v["y_mm"], 9)) for v in snapshot.get("vias", []) if v.get("net_name") in critical)
        actual_vias = sorted((v["net_name"], round(v["x_mm"], 9), round(v["y_mm"], 9)) for v in actual if v["kind"] == "via" and v["net_name"] in critical)
        if expected_vias != actual_vias:
            raise ValueError("Critical snapshot vias do not match input DSN")
    pairs = intent.get("differential_pairs", [])
    if pairs and "differential_pairs" not in capabilities:
        if not all(set(p.get("nets", [])) <= critical for p in pairs):
            raise ValueError("Router cannot enforce coupled differential pairs")
    constraints = intent.get("constraints", {})
    for net, values in constraints.items():
        if (values.get("length_tolerance_mils") is not None and net not in critical
                and "matched_lengths" not in capabilities):
            raise ValueError("Router cannot enforce matched-length constraints")
    network = children(tree, "network")
    if len(network) != 1:
        raise ValueError("DSN must contain one network")
    network = network[0]
    known = {n[1] for n in children(network, "net") if len(n) > 1}
    if set(constraints) - known or critical - known:
        raise ValueError("Constraint net is absent from DSN")
    # Remove affected memberships from prior classes, retain unrelated rules.
    for cls in children(network, "class"):
        cls[:] = [v for i, v in enumerate(cls) if i < 2 or isinstance(v, list) or v not in constraints]
    class_names = {net: 'MCP_SIPI_DSN_' + hashlib.sha256(net.encode('utf-8')).hexdigest()[:16] for net in constraints}
    old_class_names = {row[1] for row in children(network, "class") if len(row) > 1}
    if old_class_names & set(class_names.values()):
        raise ValueError("Input DSN collides with reserved generated class names")
    for net, values in sorted(constraints.items()):
        width = number(values["required_width_mils"])*(.0254/scale)
        clearance = number(values.get("clearance_mils", 0))*(.0254/scale)
        if width <= 0 or clearance < 0:
            raise ValueError("Invalid routing width/clearance")
        cls = ["class", class_names[net], net, ["rule", ["width", width], ["clearance", clearance]]]
        for layer_name, layer_values in sorted(values.get("layer_widths", {}).items()):
            matches = [dsn for dsn, native in layer_map.items() if native == layer_name]
            if len(matches) > 1:
                raise ValueError("Ambiguous DSN layer mapping")
            dsn_layer = matches[0] if matches else layer_name
            layer_width = number(layer_values["required_width_mils"])*(.0254/scale)
            if layer_width <= 0:
                raise ValueError("Invalid layer-specific routing width")
            cls.append(["layer_rule", dsn_layer, ["rule", ["width", layer_width], ["clearance", clearance]]])
        network.append(cls)
    # SPECCTRA class_class takes precedence over class defaults. Freerouting
    # v2.1.0 Network.insert_class_pair_info also applies these after classes:
    # https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/designforms/specctra/Network.java
    # Each explicit pair preserves a differential coupling gap while retaining
    # a larger 3W/5W clearance to other nets. Never flatten that distinction.
    matrix_seen = set()
    for row in intent.get("clearance_matrix", []):
        left, right = row["net_a"], row["net_b"]
        key = tuple(sorted((left, right)))
        if left == right or left not in class_names or right not in class_names or key in matrix_seen:
            raise ValueError("Invalid/duplicate domain clearance matrix pair")
        matrix_seen.add(key)
        clearance = number(row["clearance_mils"])*(.0254/scale)
        if clearance < 0:
            raise ValueError("Invalid domain clearance")
        network.append(["class_class", ["classes", class_names[left], class_names[right]],
                        ["rule", ["clearance", clearance]]])
    # Preserve every critical wire/via, not merely one segment per net.
    for kind in ("wire", "via"):
        for copper in walk(tree, kind):
            if net_of(copper) in critical:
                copper[:] = [v for v in copper if not (isinstance(v, list) and v and v[0] == "type")]
                copper.append(["type", "protect"])
    before = mapped_records(copper_records(tree), layer_map)
    return emit_specctra(tree), {"critical_nets": sorted(critical), "critical_copper": canonical(before, critical), "dsn_unit_to_mm": scale, "constraints": intent,
                                "via_styles": deepcopy(context.get("via_styles", {})), "layer_map": deepcopy(layer_map)}


def reconstruct_session(text, original_snapshot, evidence):
    tree = parse_specctra(text)
    if not tree or tree[0] != "session":
        raise ValueError("Expected Specctra session")
    records = mapped_records(copper_records(tree), evidence.get("layer_map", {}))
    critical = evidence["critical_nets"]
    if canonical(records, critical) != evidence["critical_copper"]:
        raise ValueError("Critical copper changed in routed session")
    snapshot = deepcopy(original_snapshot)
    if snapshot.get("complete") is not True:
        raise ValueError("Complete original copper snapshot required for route audit")
    if snapshot.get("arcs") or snapshot.get("regions"):
        raise ValueError("SES reconstruction of original arcs/regions is not qualified")
    snapshot["tracks"] = [{"id": f"SES-track-{i}", **{k: v for k, v in r.items() if k != "kind"}} for i, r in enumerate(records) if r["kind"] == "track"]
    # SES padstack/drill translation needs complete library geometry. Reusing
    # existing vias is permitted only with an exact coordinate+padstack map.
    vias = []
    for i, r in enumerate(records):
        if r["kind"] != "via":
            continue
        matches = [v for v in original_snapshot.get("vias", []) if v.get("net_name") == r["net_name"] and v.get("padstack") == r["padstack"] and abs(v["x_mm"]-r["x_mm"]) < 1e-9 and abs(v["y_mm"]-r["y_mm"]) < 1e-9]
        if len(matches) != 1:
            style = evidence.get("via_styles", {}).get(r["padstack"])
            if not style or not {"diameter_mm", "drill_mm", "start_layer", "end_layer"} <= style.keys():
                raise ValueError("SES via padstack/drill geometry unavailable; audit cannot be complete")
            diameter, drill = number(style["diameter_mm"]), number(style["drill_mm"])
            if not 0 < drill < diameter:
                raise ValueError("Invalid explicit SES via style geometry")
            vias.append({"id": f"SES-via-{i}", **style, **{k: v for k, v in r.items() if k != "kind"}})
        else:
            vias.append(deepcopy(matches[0]))
    snapshot["vias"] = vias
    snapshot["units"] = "mm"
    snapshot["source"] = "ses"
    snapshot["provenance"] = {**snapshot.get("provenance", {}),
                              "reader": "Specctra SES reconstruction", "native_imported": False, "live": False}
    return snapshot
