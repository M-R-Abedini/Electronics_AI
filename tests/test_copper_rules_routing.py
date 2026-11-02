"""Copper sizing composition and constrained routing safety contracts."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eda-agent" / "src"))
from eda_agent.design.copper_rules import synthesize_copper_rules
from eda_agent.design.trace_sizing import trace_width_for_current_qualified
from suite_autoroute import AutorouteService
from suite_routing_constraints import constrain_dsn, parse_specctra, emit_specctra, reconstruct_session


def table():
    return {"standard": "IPC-2152", "qualified": True, "source": "test fixture only, not published IPC data",
            "qualification": "test interpolation mechanics", "board_conditions": {"environment": "fixture"},
            "conditions": {"copper_oz": 1., "delta_t_c": 10., "layer": "external"},
            "points": [{"current_a": 1., "width_mils": 10.}, {"current_a": 3., "width_mils": 30.}]}


def test_2152_is_explicit_qualified_bounded_and_never_2221():
    kwargs = dict(copper_oz=1., delta_t_c=10., layer="external", margin=0)
    assert not trace_width_for_current_qualified(2, **kwargs)["supported"]
    result = trace_width_for_current_qualified(2, dataset=table(), **kwargs)
    assert result["min_width_mils"] == 30 and result["interpolated_width_mils"] == 20
    assert not result["certified"] and "2152" in result["model"]
    assert not trace_width_for_current_qualified(4, dataset=table(), **kwargs)["supported"]
    bad = table()
    bad["points"][1]["width_mils"] = 5
    with pytest.raises(ValueError, match="monotonic"):
        trace_width_for_current_qualified(2, dataset=bad, **kwargs)


def test_rule_matrix_uses_actual_current_width_and_scope2():
    intent = {"net_classes": {"SW": ["switch", "high_current"], "FB": ["analog_sensitive"]},
              "constraints": {"SW": {"current_a": 2}}}
    result = synthesize_copper_rules({}, intent, {"copper_oz": 1., "delta_t_c": 10., "ipc2152_dataset": table(), "track_margin": 0})
    assert result["constraints"]["SW"]["required_width_mils"] == 30
    assert result["clearance_matrix"][0]["clearance_mils"] == 150
    assert result["critical_nets"] == ["FB", "SW"]
    rule = next(r for r in result["rules"] if r["rule_type"] == "clearance")
    assert rule["scope"] == "InNet('FB')" and rule["scope2"] == "InNet('SW')"
    assert result["coverage"]["complete"]


def test_current_missing_dataset_stays_unsupported():
    result = synthesize_copper_rules({}, {"net_classes": {"PWR": ["high_current"]}, "constraints": {"PWR": {"current_a": 2}}}, {"copper_oz": 1., "delta_t_c": 10.})
    assert not result["coverage"]["complete"]
    assert "dataset" in result["coverage"]["unsupported"][0]["reason"]


def test_managed_class_names_are_stable_reserved_and_matching_scope_is_exact():
    classification = {"net_classes": {"A": ["HIGH_SPEED_SINGLE"], "B": ["HIGH_SPEED_SINGLE"]},
                      "constraints": {"A": {"match_group": "bus/a", "length_tolerance_mils": 10},
                                      "B": {"match_group": "bus/a", "length_tolerance_mils": 5}}}
    result = synthesize_copper_rules({}, classification)
    names = result["class_name_map"]
    assert all(name.startswith("MCP_SIPI_") for name in names.values())
    assert result["class_memberships"][names["bus/a"]] == ["A", "B"]
    matches = [rule for rule in result["rules"] if rule["rule_type"] == "matched_lengths"]
    assert len(matches) == 1 and matches[0]["value"] == 5
    assert matches[0]["scope"] == "InNetClass('" + names["bus/a"] + "')"
    # Adding a colliding tag must not rename an existing managed class.
    classification["net_classes"]["C"] = ["bus-a"]
    expanded = synthesize_copper_rules({}, classification)
    assert expanded["class_name_map"]["bus/a"] == names["bus/a"]
    assert expanded["class_name_map"]["bus-a"] != names["bus/a"]
    assert classification["net_classes"]["A"] == ["HIGH_SPEED_SINGLE"]


def test_domain_spacing_observes_existing_copper_width():
    snapshot = {"tracks": [{"net_name": "SW", "width_mm": .508}]}
    result = synthesize_copper_rules(snapshot, {"net_classes": {"SW": ["AGGRESSIVE_SWITCHING"], "FB": ["SENSITIVE_ANALOG"]}})
    assert result["clearance_matrix"][0]["basis_width_mils"] == 20
    assert result["clearance_matrix"][0]["clearance_mils"] == 100


def test_sanitized_pair_names_do_not_collide_and_pair_gap_is_not_3w():
    result = synthesize_copper_rules({}, {"net_classes": {"P": ["HIGH_SPEED_DIFF"], "N": ["HIGH_SPEED_DIFF"]}}, {
        "differential_pairs": [{"name": "A-B", "nets": ["P", "N"], "gap_mils": 6}, {"name": "A/B", "nets": ["P", "N"], "gap_mils": 6}]})
    names = [r["name"] for r in result["rules"] if r["rule_type"] == "differential_pairs"]
    assert len(set(names)) == 2 and all(name.startswith("Copper_Diff_A_B_") for name in names)
    assert result["clearance_matrix"][0]["clearance_mils"] == 6


def test_impedance_uses_snapshot_stackup_and_emits_layer_rules():
    snapshot = {"coverage": {"layers": True}, "layers": [
        {"name": "TopLayer", "kind": "signal", "order": 0, "thickness_mm": .0350012},
        {"name": "Top dielectric", "kind": "dielectric", "order": 1, "thickness_mm": .2, "er": 4.2},
        {"name": "GND", "kind": "plane", "order": 2, "thickness_mm": .0350012, "reference_net": "GND"},
        {"name": "Bottom dielectric", "kind": "dielectric", "order": 3, "thickness_mm": .15, "er": 4.2},
        {"name": "BottomLayer", "kind": "signal", "order": 4, "thickness_mm": .0350012}],
        "tracks": [{"net_name": "D", "layer": "TopLayer", "width_mm": .2}, {"net_name": "D", "layer": "BottomLayer", "width_mm": .2}]}
    result = synthesize_copper_rules(snapshot, {"net_classes": {"D": ["HIGH_SPEED_SINGLE"]}, "constraints": {"D": {"target_z0_ohm": 50}}})
    assert result["coverage"]["complete"], result["coverage"]
    widths = result["constraints"]["D"]["layer_widths"]
    assert widths["TopLayer"]["required_width_mils"] > widths["BottomLayer"]["required_width_mils"]
    rules = [r for r in result["rules"] if r["rule_type"] == "width"]
    assert len(rules) == 2 and all("OnLayer('" in r["scope"] for r in rules)
    snapshot["layers"][2].pop("reference_net")
    assert not synthesize_copper_rules(snapshot, {"net_classes": {"D": []}, "constraints": {"D": {"target_z0_ohm": 50}}})["coverage"]["complete"]


def test_differential_target_uses_pair_gap_and_existing_inverse():
    from eda_agent.design.impedance_sizing import trace_width_for_impedance
    snapshot = {"coverage": {"layers": True}, "layers": [
        {"name": "TopLayer", "kind": "signal", "order": 0, "thickness_mm": .0350012},
        {"name": "Dielectric", "kind": "dielectric", "order": 1, "thickness_mm": .2, "er": 4.2},
        {"name": "GND", "kind": "plane", "order": 2, "thickness_mm": .0350012, "reference_net": "GND"}],
        "tracks": [{"net_name": net, "layer": "TopLayer", "width_mm": .2} for net in ("P", "N")]}
    classification = {"net_classes": {net: ["HIGH_SPEED_DIFF"] for net in ("P", "N")},
                      "differential_pairs": [{"name": "USB", "nets": ["P", "N"], "gap_mils": 6, "target_zdiff_ohm": 90}]}
    result = synthesize_copper_rules(snapshot, classification)
    assert result["coverage"]["complete"], result["coverage"]
    expected = trace_width_for_impedance(90, "microstrip_diff", .2/.0254,
                                        dielectric_constant=4.2, copper_oz=1, spacing_mils=6)
    assert result["constraints"]["P"]["required_width_mils"] == pytest.approx(expected.width_mils)
    assert result["constraints"]["N"]["layer_widths"]["TopLayer"]["required_width_mils"] == pytest.approx(expected.width_mils)
    pair_rule = next(rule for rule in result["rules"] if rule["rule_type"] == "differential_pairs")
    for key in ("pair_width_min_mils", "pair_width_max_mils", "pair_width_preferred_mils"):
        assert pair_rule[key] == pytest.approx(expected.width_mils)
    assert "target_zdiff_ohm" not in classification.get("constraints", {}).get("P", {})
    classification["differential_pairs"][0].pop("gap_mils")
    assert not synthesize_copper_rules(snapshot, classification)["coverage"]["complete"]

    # A scalar pair width cannot express different layer impedance geometries.
    classification["differential_pairs"][0]["gap_mils"] = 6
    snapshot["layers"].extend([
        {"name": "Bottom dielectric", "kind": "dielectric", "order": 3, "thickness_mm": .15, "er": 4.2},
        {"name": "BottomLayer", "kind": "signal", "order": 4, "thickness_mm": .0350012}])
    snapshot["tracks"].extend({"net_name": net, "layer": "BottomLayer", "width_mm": .2} for net in ("P", "N"))
    mixed = synthesize_copper_rules(snapshot, classification)
    assert not mixed["coverage"]["complete"]
    assert not any(rule["rule_type"] == "differential_pairs" for rule in mixed["rules"])
    assert any(item["constraint"] == "differential_pair_width" for item in mixed["coverage"]["unsupported"])


def test_high_speed_default_fab_width_does_not_claim_controlled_impedance():
    result = synthesize_copper_rules({}, {"net_classes": {"CLK": ["HIGH_SPEED_SINGLE"]}})
    assert not result["coverage"]["complete"]
    assert "target impedance" in result["coverage"]["unsupported"][0]["reason"]


@pytest.mark.parametrize("pairs", [
    [{"name": "USB", "nets": ["P", "P"]}],
    [{"name": "USB", "nets": ["P", "MISSING"]}],
    [{"name": "USB", "nets": ["P", "N"]}, {"name": "USB", "nets": ["N", "P"]}],
])
def test_pair_option_overrides_cannot_bypass_declared_net_validation(pairs):
    with pytest.raises(ValueError, match="distinct declared|unique"):
        synthesize_copper_rules({}, {"net_classes": {"P": ["CONTROL"], "N": ["CONTROL"]}}, {"differential_pairs": pairs})


def test_unclassified_and_oversized_clearance_matrix_never_claim_complete():
    unknown = synthesize_copper_rules({"nets": ["NEW"]}, {})
    assert not unknown["coverage"]["complete"]
    assert any(item["constraint"] == "classification" for item in unknown["coverage"]["unsupported"])
    large = synthesize_copper_rules({}, {"net_classes": {f"N{i}": ["CONTROL"] for i in range(70)}})
    assert not large["coverage"]["complete"] and large["coverage"]["clearance_matrix_omitted"]
    assert not large["clearance_matrix"] and len(large["rules"]) == 70


DSN = '(pcb "board space" (resolution mil 1000) (network (net "a space" (pins U1-1 U2-1))) (wiring (wire (path top 6 0 0 100 0) (net "a space"))))'
SES = '(session "board space" (routes (resolution mil 1000) (network_out (net "a space" (wire (path top 6000 0 0 100000 0))))))'


def context(critical=False):
    return {"intent": {"constraints": {"a space": {"required_width_mils": 6, "clearance_mils": 18}},
                       "critical_nets": ["a space"] if critical else [], "coverage": {"complete": True}},
            "snapshot": {"complete": True, "units": "mm", "tracks": [], "vias": [], "arcs": [], "regions": []}}


def test_real_sexpression_rules_preserve_quoted_net_and_input():
    transformed, evidence = constrain_dsn(DSN, context(), set())
    tree = parse_specctra(transformed)
    assert parse_specctra(emit_specctra(tree)) == tree
    assert '"a space"' in transformed and "(width 6.0)" in transformed and "(clearance 18.0)" in transformed
    snapshot = reconstruct_session(SES, context()["snapshot"], evidence)
    assert snapshot["tracks"][0]["end"] == pytest.approx([2.54, 0.0])
    assert snapshot["tracks"][0]["net_name"] == "a space"


def test_standard_string_quote_declaration_and_escaped_names_round_trip():
    dsn = '(pcb "a \\"quoted\\" board" (parser (string_quote ") (space_in_quoted_tokens on)) (resolution um 10))'
    parsed = parse_specctra(dsn)
    assert parse_specctra(emit_specctra(parsed)) == parsed
    assert parsed[1] == 'a "quoted" board'


def test_unrouted_critical_rejected_before_provider():
    ctx = context(True)
    # No audit snapshot/proof: merely existing wires are not a proof.
    ctx.pop("snapshot")
    with pytest.raises(ValueError, match="Unrouted/unproven"):
        constrain_dsn(DSN, ctx, set())


def test_protected_critical_geometry_is_verified():
    ctx = context(True)
    ctx.pop("snapshot")
    transformed, evidence = constrain_dsn(DSN, ctx, {"exclude_nets"})
    assert "(type protect)" in transformed
    snapshot = reconstruct_session(SES, context()["snapshot"], evidence)
    assert snapshot["tracks"]
    with pytest.raises(ValueError, match="Critical copper changed"):
        reconstruct_session(SES.replace("100000 0", "101000 0"), context()["snapshot"], evidence)


def test_complete_connectivity_graph_prelocks_stock_router():
    ctx = context(True)
    ctx["snapshot"].update({
        "coverage": {key: True for key in ("tracks", "arcs", "vias", "pads", "regions", "layers")},
        "layers": [{"name": "top", "kind": "signal"}],
        "tracks": [{"id": "t", "net_name": "a space", "layer": "top", "start": [0, 0], "end": [2.54, 0], "width_mm": .1524}],
        "pads": [{"id": str(i), "net_name": "a space", "layer": "top", "x_mm": x, "y_mm": 0, "width_mm": .3, "height_mm": .3, "shape": "rect", "rotation_deg": 0} for i, x in enumerate((0, 2.54))]})
    transformed, _ = constrain_dsn(DSN, ctx, set())
    assert "(type protect)" in transformed
    with pytest.raises(ValueError, match="snapshot copper does not match"):
        constrain_dsn(DSN.replace("100 0", "101 0"), ctx, set())
    ctx["layer_map"] = {"top": "TopLayer"}
    ctx["snapshot"]["layers"][0]["name"] = "TopLayer"
    ctx["snapshot"]["tracks"][0]["layer"] = "TopLayer"
    for pad in ctx["snapshot"]["pads"]:
        pad["layer"] = "TopLayer"
    transformed, evidence = constrain_dsn(DSN, ctx, set())
    assert reconstruct_session(SES, ctx["snapshot"], evidence)["tracks"][0]["layer"] == "TopLayer"
    ctx["snapshot"]["tracks"] = []
    with pytest.raises(ValueError, match="Unrouted/unproven"):
        constrain_dsn(DSN, ctx, set())


def test_layer_specific_rules_are_real_dsn_layer_rule_scopes():
    ctx = context()
    ctx["intent"]["constraints"]["a space"]["layer_widths"] = {"TopLayer": {"required_width_mils": 8.25, "max_width_mils": 8.25}}
    ctx["layer_map"] = {"top": "TopLayer"}
    transformed, _ = constrain_dsn(DSN, ctx, set())
    assert "(layer_rule top (rule (width 8.25) (clearance 18.0)))" in transformed


def test_dsn_exact_class_matrix_preserves_pair_gap_beside_analog_clearance():
    from suite_routing_constraints import children
    dsn = '(pcb board (resolution mil 1000) (network (net P) (net N) (net FB)))'
    ctx = {"intent": {"constraints": {net: {"required_width_mils": 6, "clearance_mils": 50} for net in ("P", "N", "FB")},
                      "coverage": {"complete": True},
                      "clearance_matrix": [{"net_a": "P", "net_b": "N", "clearance_mils": 6},
                                           {"net_a": "P", "net_b": "FB", "clearance_mils": 50},
                                           {"net_a": "N", "net_b": "FB", "clearance_mils": 50}]}}
    transformed, _ = constrain_dsn(dsn, ctx, set())
    network = children(parse_specctra(transformed), "network")[0]
    by_net = {cls[2]: cls[1] for cls in children(network, "class")}
    pairs = {frozenset(children(row, "classes")[0][1:]): float(children(children(row, "rule")[0], "clearance")[0][1])
             for row in children(network, "class_class")}
    assert pairs[frozenset([by_net["P"], by_net["N"]])] == 6
    assert pairs[frozenset([by_net["P"], by_net["FB"]])] == 50


def test_production_postroute_audit_accepts_complete_ordinary_copper(tmp_path):
    from eda_agent.design.copper_snapshot import empty_snapshot, validate_snapshot
    ctx = context()
    snapshot = empty_snapshot(str(tmp_path / 'Board.PcbDoc'), source='file')
    snapshot.update(complete=True, missing=[],
                    coverage={section: 'complete' for section in ('tracks', 'arcs', 'vias', 'pads', 'regions', 'layers')},
                    nets=['a space'], layers=[{'name': 'top', 'kind': 'signal', 'order': 0, 'thickness_mm': .035}],
                    tracks=[{'id': 'old', 'net_name': 'a space', 'layer': 'top', 'start': [0, 0], 'end': [2.54, 0], 'width_mm': .1524}])
    ctx['snapshot'] = snapshot
    ctx['intent']['net_classes'] = {'a space': ['CONTROL']}
    dsn = tmp_path / 'in.dsn'
    dsn.write_text(DSN)
    class Router:
        def route(self, source, output, **kwargs):
            output.write_text(SES)
            return {'session_path': str(output)}
    result = AutorouteService(tmp_path / 'jobs', provider=Router()).route_dsn(str(dsn), constraints_context=ctx)
    assert result['copper_audit']['coverage_complete']
    assert result['copper_audit']['status'] == 'clean'
    import json
    written = json.loads((Path(result['session_path']).parent / 'routed-snapshot.json').read_bytes())
    validate_snapshot(written)
    assert written['source'] == 'ses' and written['provenance']['native_imported'] is False


@pytest.mark.parametrize("passed,complete", [(False, True), (True, False)])
def test_failed_postroute_audit_never_publishes_session(tmp_path, passed, complete):
    dsn = tmp_path / "in.dsn"
    dsn.write_text(DSN)
    class Router:
        def route(self, source, output, **kwargs):
            output.write_text(SES)
            return {"session_path": str(output)}
    service = AutorouteService(tmp_path / "jobs", provider=Router(), copper_audit=lambda *_: {"passed": passed, "complete": complete})
    with pytest.raises(ValueError, match="Post-route copper audit"):
        service.route_dsn(str(dsn), constraints_context=context())
    assert not list((tmp_path / "jobs").rglob("result.ses"))
    assert dsn.read_text() == DSN


def test_verified_postroute_records_provenance(tmp_path):
    dsn = tmp_path / "in.dsn"
    dsn.write_text(DSN)
    class Router:
        def route(self, source, output, **kwargs):
            output.write_text(SES)
            return {"session_path": str(output)}
    result = AutorouteService(tmp_path / "jobs", provider=Router(), copper_audit=lambda *_: {"passed": True, "complete": True}).route_dsn(str(dsn), constraints_context=context())
    assert result["copper_constraints_applied"] and result["copper_audit"]["passed"]
    assert result["original_input_sha256"] != result["constrained_input_sha256"]


@pytest.mark.parametrize('acknowledge,mutate', [(True, False), (False, False), (True, True)])
def test_qualified_provider_receives_and_acknowledges_coupling_and_matching(tmp_path, acknowledge, mutate):
    from copy import deepcopy
    dsn = tmp_path / 'in.dsn'
    dsn.write_text('(pcb board (resolution mil 1000) (network (net P) (net N)))')
    constraints = {net: {'required_width_mils': 6, 'clearance_mils': 6,
                          'match_group': 'USB', 'length_tolerance_mils': 10} for net in ('P', 'N')}
    pairs = [{'name': 'USB', 'nets': ['P', 'N'], 'gap_mils': 6}]
    ctx = {'intent': {'constraints': constraints, 'differential_pairs': pairs,
                      'net_classes': {net: ['CONTROL'] for net in ('P', 'N')}, 'coverage': {'complete': True}},
           'snapshot': {'complete': True, 'units': 'mm', 'tracks': [], 'arcs': [], 'regions': [], 'vias': []}}
    class QualifiedRouter:
        constraint_capabilities = {'differential_pairs', 'matched_lengths'}
        def route(self, source, output, **kwargs):
            assert kwargs['differential_pairs'] == pairs
            assert set(kwargs['matched_lengths']) == {'P', 'N'}
            output.write_text('(session board (routes (resolution mil 1000) (network_out (net P) (net N))))')
            if mutate:
                kwargs['differential_pairs'].clear()
                kwargs['matched_lengths'].clear()
            result = {'session_path': str(output)}
            if acknowledge:
                result.update(enforced_differential_pairs=kwargs['differential_pairs'],
                              enforced_matched_lengths=kwargs['matched_lengths'])
            return result
    service = AutorouteService(tmp_path / 'jobs', provider=QualifiedRouter(),
                               copper_audit=lambda *_: {'passed': True, 'complete': True})
    untouched = deepcopy(ctx)
    if acknowledge and not mutate:
        assert service.route_dsn(str(dsn), constraints_context=ctx)['copper_audit']['passed']
    else:
        with pytest.raises(ValueError, match='did not acknowledge'):
            service.route_dsn(str(dsn), constraints_context=ctx)
        assert not list((tmp_path / 'jobs').rglob('result.ses'))
    assert ctx == untouched
