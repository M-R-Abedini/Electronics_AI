"""Observable geometry, topology and selection counterexamples (synthetic mm)."""
import copy
import json
import math

import pytest
from jsonschema import Draft202012Validator

from eda_agent.design.placement_evidence import (
    compare_placement_candidates, contract_schemas, digest, evaluate_placement,
    example_board_context, generate_placement_candidates, inverse_transform_point,
    select_profiles, snap_to_origin_grid, transform_point, transformed_pads,
    validate_board_context,
)
from eda_agent.units import MM_PER_MIL
from eda_agent.design.copper_geometry import pad_outline

SOURCE = {"kind": "engineering_policy", "id": "synthetic-test", "revision": "1"}


def check(report, identifier):
    found = [item for item in report["checks"] if item["id"] == identifier]
    assert len(found) == 1
    return found[0]


def candidate(cid, **pose):
    return {"component_id": cid, **pose}


def test_contract_example_is_json_safe_and_identity_stays_exact():
    context = example_board_context()
    schemas = contract_schemas()
    for name, schema in schemas.items():
        if isinstance(schema, dict):
            Draft202012Validator.check_schema(schema)
    Draft202012Validator(schemas["BoardContext"]).validate(context)
    result = compare_placement_candidates(context)
    assert result["selection_status"] == "baseline_retained"
    assert result["plan"]["moves"] == []
    assert result["plan"]["baseline_snapshot_id"] == context["snapshot_id"]
    assert len(result["plan"]["plan_digest"]) == 64
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("side", ["top", "bottom"])
@pytest.mark.parametrize("rotation", [0, 37, 90, 180, 270])
def test_local_origin_rotation_bottom_mirror_roundtrip(side, rotation):
    pose = {"x_mm": 17.25, "y_mm": -9.875, "rotation_deg": rotation, "side": side}
    local = (2.3, -.7)
    expected = inverse_transform_point(transform_point(local, pose), pose)
    assert expected == pytest.approx(local, abs=1e-11)
    if rotation == 90 and side == "bottom":
        assert transform_point((1, 0), pose) == pytest.approx((17.25, -10.875))


def test_mm_mil_and_nonzero_origin_grid_are_not_zero_origin_snap():
    assert 1000 * MM_PER_MIL == pytest.approx(25.4)
    assert snap_to_origin_grid((10.14, 20.37), (10.03, 20.01), (.2, .3)) == pytest.approx((10.23, 20.31))
    with pytest.raises(ValueError, match="positive"):
        snap_to_origin_grid((1, 2), (0, 0), (0, .1))


def test_absolute_pad_binding_survives_rotation_flip_and_offset_origin():
    context = validate_board_context(example_board_context())
    context["pads"][0].pop("local_xy_mm")
    context["pads"][0].update(x_mm=15, y_mm=25, pad_name="P1", width_mm=1, height_mm=.4)
    context["opposite_layers"] = {"Top": "Bottom", "Bottom": "Top"}
    poses = {c["id"]: {key: c[key] for key in ("x_mm", "y_mm", "rotation_deg", "side")}
             for c in context["components"]}
    poses["a"] = {"x_mm": 20, "y_mm": 30, "rotation_deg": 90, "side": "bottom"}
    pad = transformed_pads(context, poses)["a.1"]
    assert (pad["x_mm"], pad["y_mm"]) == pytest.approx((20, 29))
    assert pad["pad_name"] == "P1" and pad["net_name"] == "SIGNAL"
    assert pad["layer"] == "Bottom"
    assert max(x for x, _ in pad["outline"]) - min(x for x, _ in pad["outline"]) == pytest.approx(.4)


def test_transformed_pad_reconstruction_matches_the_contour_consumed_by_copper_audit():
    context = validate_board_context(example_board_context())
    context["pads"][0].pop("local_xy_mm")
    context["pads"][0].update(x_mm=15, y_mm=25, width_mm=1, height_mm=.4, rotation_deg=0)
    poses = {c["id"]: {key:c[key] for key in ("x_mm", "y_mm", "rotation_deg", "side")} for c in context["components"]}
    poses["a"]["rotation_deg"] = 90
    pad = transformed_pads(context, poses)["a.1"]
    # CopperGraph calls pad_outline; shape metadata must describe the same
    # rotated copper as the explicitly transformed contour, not the old pad.
    reconstructed = pad_outline(pad)
    assert len(reconstructed) == len(pad["outline"])
    assert all(any(math.dist(a, b) < 1e-10 for b in pad["outline"]) for a in reconstructed)


def test_concave_outline_rejects_pose_inside_its_bounding_rectangle():
    context = example_board_context()
    context["outline"] = [[10,20], [30,20], [30,40], [23,40], [23,28], [17,28], [17,40], [10,40]]
    context["components"][1]["x_mm"] = 27
    result = evaluate_placement(context, moves=[candidate("a", x_mm=20, y_mm=34)])
    assert check(result, "outline:a")["status"] == "fail"
    assert not result["feasible"]


def test_cutout_enclosed_by_a_courtyard_is_not_missed_by_edge_only_check():
    context = example_board_context()
    context["cutouts"] = [[[13.8,24.8], [14.2,24.8], [14.2,25.2], [13.8,25.2]]]
    result = evaluate_placement(context)
    assert check(result, "outline:a")["status"] == "fail"


def test_overhang_is_specific_sourced_region_not_component_whitelist():
    context = example_board_context()
    move = [candidate("a", x_mm=10.5)]
    assert check(evaluate_placement(context, moves=move), "outline:a")["status"] == "fail"
    context["overhang_allowances"] = [{"component_id": "a", "source": SOURCE,
        "region": {"outline": [[9,24], [10,24], [10,26], [9,26]]}}]
    assert check(evaluate_placement(context, moves=move), "outline:a")["status"] == "pass"
    context["overhang_allowances"][0]["component_id"] = "b"
    assert check(evaluate_placement(context, moves=move), "outline:a")["status"] == "fail"


def test_aabb_false_positive_is_rejected_by_real_polygon_narrow_phase():
    context = example_board_context()
    context["pads"] = []  # Two explicitly modelled mechanical envelope objects.
    context["components"][0]["courtyard_local"] = [[0,0], [2,0], [0,2]]
    context["components"][1].update(x_mm=14, courtyard_local=[[2,2], [2,.9], [.9,2]])
    assert check(evaluate_placement(context), "collision:a:b")["status"] == "pass"


def test_aligned_partial_overlap_is_detected_not_only_crossing_edges():
    context = example_board_context()
    context["pads"] = []
    context["components"][1]["x_mm"] = 15
    assert check(evaluate_placement(context), "collision:a:b")["status"] == "fail"


def test_final_state_swap_is_valid_while_one_move_against_old_state_collides():
    context = example_board_context()
    one = evaluate_placement(context, moves=[candidate("a", x_mm=24)])
    swap = evaluate_placement(context, moves=[candidate("a", x_mm=24), candidate("b", x_mm=14)])
    assert not one["feasible"]
    assert swap["feasible"] and check(swap, "collision:a:b")["status"] == "pass"
    assert [m["designator"] for m in swap["moves"]] == ["U_A", "R_B"]


def test_unknown_courtyard_and_fabrication_profile_are_blockers_not_clean():
    context = example_board_context()
    del context["components"][0]["courtyard_local"]
    result = evaluate_placement(context)
    assert check(result, "courtyard:a")["status"] == "unknown"
    assert result["status"] == "unknown" and not result["feasible"]
    context = example_board_context()
    del context["fabrication_profile"]
    assert check(evaluate_placement(context), "manufacturing:pad_clearance_input")["status"] == "unknown"


def test_locked_pose_and_hard_violation_cannot_be_redeemed_by_hpwl():
    context = example_board_context()
    context["components"][1]["locked"] = True
    result = compare_placement_candidates(context, candidates=[{"id": "shorter", "moves": [candidate("b", x_mm=18)]}])
    assert result["evaluations"][1]["metrics"]["signal_hpwl"]["value"] < result["evaluations"][0]["metrics"]["signal_hpwl"]["value"]
    assert check(result["evaluations"][1], "fixed:b")["status"] == "fail"
    assert result["selected_candidate_id"] == "baseline"


@pytest.mark.parametrize("applicability,expected", [(None,"unknown"), (False,"not_applicable")])
def test_unknown_applicability_blocks_but_explicit_not_applicable_does_not(applicability, expected):
    constraint = {"id": "custom", "check": "creepage", "kind": "hard", "source": SOURCE, "applicability": applicability}
    result = evaluate_placement(example_board_context(), constraints=[constraint])
    assert check(result, "custom")["status"] == expected
    assert result["feasible"] == (expected == "not_applicable")


def test_unsupported_predicate_caller_pass_and_image_cannot_supply_evidence():
    constraint = {"id": "return", "check": "reference_continuity", "kind": "hard", "source": SOURCE,
                  "status": "pass", "evidence": {"image": "looks good", "ok": True}}
    result = evaluate_placement(example_board_context(), constraints=[constraint])
    assert check(result, "return")["status"] == "unknown"
    assert not result["feasible"] and result["coverage"]["renderer"] == "not_evaluated"


def test_critical_path_regression_beats_many_ordinary_net_improvements():
    context = example_board_context()
    context["components"][1]["x_mm"] = 20
    third = copy.deepcopy(context["components"][1]); third.update(id="c", reference="C_ANY", x_mm=28)
    context["components"].append(third)
    context["pads"] = [
        {"id": "Acritical", "component_id": "a", "net_name": "CRITICAL", "local_xy_mm": [0,0], "width_mm": .1, "height_mm": .1, "shape": "rectangle", "layer": "Top"},
        {"id": "Bcritical", "component_id": "b", "net_name": "CRITICAL", "local_xy_mm": [0,0], "width_mm": .1, "height_mm": .1, "shape": "rectangle", "layer": "Top"}]
    for i in range(8):
        for cid in ("b", "c"):
            context["pads"].append({"id": cid+str(i), "component_id": cid, "net_name": "N"+str(i),
                "local_xy_mm": [0, .25 + i*.2], "width_mm": .05, "height_mm": .05, "shape": "rectangle", "layer": "Top"})
    intent = {"critical_paths": [{"id": "sensitive", "pad_ids": ["Acritical", "Bcritical"]}]}
    compared = compare_placement_candidates(context, intent, candidates=[{"id": "shorter_bus", "moves": [candidate("b", x_mm=24)]}])
    before, after = compared["evaluations"]
    assert after["feasible"]
    assert after["metrics"]["signal_hpwl"]["value"] < before["metrics"]["signal_hpwl"]["value"]
    assert compared["selected_candidate_id"] == "baseline"
    assert compared["tradeoffs"][1]["critical_regressions"][0]["metric"] == "critical_path:sensitive"


def test_baseline_wins_ties_and_eco_scope_prevents_unrelated_changes():
    context = example_board_context()
    same = compare_placement_candidates(context, candidates=[{"id": "gratuitous", "moves": [candidate("a", x_mm=14)]}])
    assert same["selected_candidate_id"] == "baseline"
    context["scope_component_ids"] = ["a"]
    with pytest.raises(ValueError, match="outside"):
        evaluate_placement(context, moves=[candidate("b", x_mm=18)])
    good = compare_placement_candidates(context, candidates=[{"id": "eco", "moves": [candidate("a", x_mm=18)]}])
    assert good["selected_candidate_id"] == "eco"
    assert good["evaluations"][1]["final_poses"]["b"]["x_mm"] == 24


def test_profiles_are_composable_by_block_and_safety_unknown_stays_local():
    context = example_board_context()
    intent = {"blocks": [
        {"id": "front_end", "component_ids": ["a"], "roles": ["precision_analog", "isolation_barrier"], "source": SOURCE},
        {"id": "power", "component_ids": ["b"], "profiles": ["switching_power"], "source": SOURCE}]}
    profiles = select_profiles(intent)
    assert profiles[0]["profiles"] == ["isolation", "precision_analog"]
    assert profiles[1]["profiles"] == ["switching_power"]
    result = evaluate_placement(context, intent)
    assert check(result, "profile:front_end:isolation:isolation_requirements")["status"] == "unknown"
    assert not result["feasible"]
    context["scope_component_ids"] = ["b"]
    result = evaluate_placement(context, intent)
    assert not any(c["id"].startswith("profile:front_end:") for c in result["checks"])


def test_names_and_part_numbers_do_not_impose_profiles():
    context = example_board_context()
    context["components"][0].update(reference="U12", part_number="EXAMPLE_ADC")
    assert evaluate_placement(context)["profiles"] == []


def test_profile_cannot_link_an_unrelated_soft_not_applicable_check():
    context = example_board_context()
    intent = {"blocks": [{"id": "analog", "component_ids": ["a"], "profiles": ["precision_analog"],
        "source": SOURCE, "requirements": {"sensitive_pin_bindings": {"input": "a.1"},
            "return_intent": {"net": "GND"}, "placement_check_ids": ["cosmetic"]}}]}
    cosmetic = {"id": "cosmetic", "check": "grid", "kind": "soft", "applicability": False,
                "scope": {"components": ["b"]}, "source": SOURCE, "origin_mm": [0,0], "pitch_mm": [.1,.1]}
    result = evaluate_placement(context, intent, constraints=[cosmetic])
    assert check(result, "profile:analog:precision_analog:scoped_checks")["status"] == "unknown"
    assert not result["feasible"]


def reservation(layer="Top"):
    return {"id": "bus", "layer": layer, "start_mm": [12,30], "end_mm": [28,30], "width_mm": 2,
        "track_width_mm": .2, "spacing_mm": .2, "margin_mm": .2, "net_names": ["BUS1", "BUS2"], "source": SOURCE}


def test_lower_hpwl_candidate_is_rejected_when_it_blocks_reserved_escape_channel():
    context = example_board_context(); intent = {"reservations": [reservation()]}
    result = compare_placement_candidates(context, intent, candidates=[{"id": "shorter", "moves": [candidate("b", x_mm=18, y_mm=29)]}])
    assert result["evaluations"][1]["metrics"]["signal_hpwl"]["value"] < result["evaluations"][0]["metrics"]["signal_hpwl"]["value"]
    assert check(result["evaluations"][1], "channel:bus")["status"] == "fail"
    assert result["selected_candidate_id"] == "baseline"
    channel = result["evaluations"][0]["reservations"][0]
    assert channel["required_width_lower_bound_mm"] == pytest.approx(1.0)
    assert channel["routing_completion_verified"] is False


def test_channel_obstruction_is_layer_specific_and_unknown_pour_stays_unknown():
    context = example_board_context()
    context["route_obstacles"] = [{"id": "copper", "layer": "Bottom", "outline": [[19,29], [21,29], [21,31], [19,31]]}]
    assert check(evaluate_placement(context, {"reservations": [reservation("Top")]}), "channel:bus")["status"] == "pass"
    assert check(evaluate_placement(context, {"reservations": [reservation("Bottom")]}), "channel:bus")["status"] == "fail"
    context["coverage"]["routing_obstacles"] = "partial"
    result = evaluate_placement(context, {"reservations": [reservation("Top")]})
    assert check(result, "channel:bus")["status"] == "unknown"


def decoupling_fixture():
    context = example_board_context()
    context["components"][1].update(x_mm=14, side="bottom")
    context["pads"] = []
    for cid in ("a", "b"):
        for pin, net, x in (("power", "PWR", -.5), ("return", "GND", .5)):
            context["pads"].append({"id": cid+"."+pin, "component_id": cid, "net_name": net,
                "local_xy_mm": [x,0], "width_mm": .2, "height_mm": .2, "shape": "rectangle",
                "layer": "Top" if cid == "a" else "Bottom"})
    binding = {"id": "local_bypass", "ic_power_pad": "a.power", "cap_power_pad": "b.power",
        "ic_return_pad": "a.return", "cap_return_pad": "b.return", "power_net": "PWR", "return_net": "GND", "source": SOURCE}
    return context, {"decoupling_connections": [binding]}


def test_bottom_decap_mapping_is_valid_but_does_not_prove_physical_return():
    context, intent = decoupling_fixture()
    result = evaluate_placement(context, intent)
    assert check(result, "decoupling_binding:local_bypass")["status"] == "pass"
    assert check(result, "decoupling_physical_return:local_bypass")["status"] == "unknown"
    assert not any(c["status"] == "fail" for c in result["checks"])
    assert result["metrics"]["critical_decap:local_bypass"]["route_verified"] is False


def test_near_decap_with_wrong_return_or_swapped_power_pin_is_not_accepted():
    context, intent = decoupling_fixture()
    context["pads"][3]["net_name"] = "OTHER_RETURN"
    assert check(evaluate_placement(context, intent), "decoupling_binding:local_bypass")["status"] == "fail"
    context, intent = decoupling_fixture()
    intent["decoupling_connections"][0]["cap_power_pad"] = "b.return"
    assert check(evaluate_placement(context, intent), "decoupling_binding:local_bypass")["status"] == "fail"


def copper_fixture():
    layers = [{"name": "Top", "order": 0, "reference_layer": "G1"},
        {"name": "G1", "order": 1, "kind": "plane", "refnet": "GND"},
        {"name": "G2", "order": 2, "kind": "plane", "refnet": "GND"},
        {"name": "Bottom", "order": 3, "reference_layer": "G2"}]
    rectangle = [[10,20], [30,20], [30,40], [10,40]]
    physical = {"complete": True, "missing": [], "layers": layers, "tracks": [], "arcs": [], "pads": [],
        "regions": [{"id": n, "layer": n, "net_name": "GND", "poured": True, "kind": "copper", "outline": rectangle, "holes": []} for n in ("G1", "G2")],
        "vias": [{"id": "signal", "x_mm": 19, "y_mm": 30, "net_name": "CLK", "start_layer": "Top", "end_layer": "Bottom", "diameter_mm": .6, "hole_diameter_mm": .3},
                 {"id": "return", "x_mm": 19.5, "y_mm": 30, "net_name": "GND", "start_layer": "G1", "end_layer": "G2", "diameter_mm": .6, "hole_diameter_mm": .3}],
        "coverage": {k: "complete" for k in ("tracks", "arcs", "vias", "pads", "regions", "layers")}}
    intent = {"copper_intent": {"net_classes": {"CLK": ["HIGH_SPEED_SINGLE"], "GND": ["GROUND"], "OTHER": ["SIGNAL"]}}}
    return physical, intent


@pytest.mark.parametrize("fault", [None, "wrong_net", "wrong_span", "plane_hole"])
def test_existing_copper_auditor_requires_correct_return_net_span_and_contact(fault):
    context = example_board_context(); physical, intent = copper_fixture()
    if fault == "wrong_net":physical["vias"][1]["net_name"] = "OTHER"
    if fault == "wrong_span":physical["vias"][1]["end_layer"] = "G1"
    if fault == "plane_hole":physical["regions"][1]["holes"] = [[[19.3,29.8],[19.7,29.8],[19.7,30.2],[19.3,30.2]]]
    context["copper_snapshot"] = physical
    result = evaluate_placement(context, intent)
    physical_check = check(result, "physical_copper")
    findings = physical_check["geometry"]["audit"]["violations"]
    assert any(v["check"] == "return_via" for v in findings) == (fault is not None)
    if fault is not None:assert not result["feasible"]


def test_copper_presence_does_not_certify_an_unmapped_decoupling_loop():
    context, intent = decoupling_fixture()
    physical, _ = copper_fixture(); physical["vias"] = []; physical["regions"] = []
    physical["pads"] = []
    intent["copper_intent"] = {"net_classes": {"PWR": ["POWER"], "GND": ["GROUND"]}}
    context["copper_snapshot"] = physical
    result = evaluate_placement(context, intent)
    assert not result["feasible"], "A generic copper audit did not test the declared bypass loop"


def test_unaffected_decap_block_does_not_block_a_distant_scoped_eco():
    context, intent = decoupling_fixture()
    third = copy.deepcopy(context["components"][0]); third.update(id="c", reference="C_ECO", x_mm=27, y_mm=35)
    context["components"].append(third)
    context["pads"].append({"id":"c.1", "component_id":"c", "net_name":"ECO", "local_xy_mm":[0,0],
                            "width_mm":.3,"height_mm":.3,"shape":"rectangle","layer":"Top"})
    context["scope_component_ids"] = ["c"]
    result = evaluate_placement(context, intent, moves=[candidate("c", x_mm=26)])
    assert result["feasible"]
    assert not any(c["id"].startswith("decoupling_") for c in result["checks"])
    assert result["final_poses"]["a"]["x_mm"] == 14


def test_every_decoupling_binding_is_checked_without_scope_variable_clobber():
    context, intent = decoupling_fixture()
    second = copy.deepcopy(intent["decoupling_connections"][0])
    second.update(id="second", cap_power_pad="b.return")
    intent["decoupling_connections"].append(second)
    result = evaluate_placement(context, intent)
    assert check(result, "decoupling_binding:local_bypass")["status"] == "pass"
    assert check(result, "decoupling_binding:second")["status"] == "fail"


def test_far_pad_pairs_use_bounds_and_only_near_pairs_reach_exact_copper(monkeypatch):
    import eda_agent.design.placement_evidence as core
    context = example_board_context()
    context["outline"] = [[0,0], [2000,0], [2000,2000], [0,2000]]
    context["pads"] = [{"id":f"p{i}","component_id":"a","net_name":f"n{i}","x_mm":5+i*5,
        "y_mm":500,"width_mm":.2,"height_mm":.2,"shape":"rectangle","layer":"Top"} for i in range(200)]
    calls = []
    original = core._ring_distance
    def counted(a,b):
        calls.append(1); return original(a,b)
    monkeypatch.setattr(core, "_ring_distance", counted)
    result = evaluate_placement(context)
    assert result["feasible"]
    assert len(calls) == 0  # 19,900 far pairs need no polygon narrow phase.
    assert len(result["checks"]) < 20
    context["pads"][1]["x_mm"] = 5.25
    result = evaluate_placement(context)
    assert not result["feasible"]
    assert len(calls) == 1
    assert any(c["status"] == "fail" and c["id"].startswith("manufacturing:pad_clearance:") for c in result["checks"])


def test_broad_phase_does_not_turn_unsupported_or_missing_pad_geometry_into_pass():
    context = example_board_context()
    context["pads"][1]["net_name"] = "OTHER"
    context["pads"][1].update(shape="ellipse", width_mm=.7, height_mm=.3)
    assert not evaluate_placement(context)["feasible"]
    context = example_board_context(); context["coverage"]["pads"] = "partial"
    result = evaluate_placement(context)
    assert check(result, "manufacturing:pad_extraction")["status"] == "unknown"
    assert not result["feasible"]


@pytest.mark.parametrize("mode", ["refine", "construct"])
def test_generator_reuses_existing_solver_reproducibly_and_never_flips_or_resizes(mode):
    context = example_board_context()
    context["components"][1]["side"] = "bottom"
    context["pads"][1]["layer"] = "Bottom"
    original = copy.deepcopy(context)
    options = {"mode": mode, "seed": 7, "candidate_count": 2, "iterations": 8, "restart_radius_mm": .3}
    first = generate_placement_candidates(context, options=options)
    second = generate_placement_candidates(context, options=options)
    assert first == second and context == original
    assert [c["seed"] for c in first] == [7, 8]
    for proposal in first:
        assert proposal["proxy_only"] and proposal["proposal_policy"]["board_resize_allowed"] is False
        assert proposal["solver"].startswith("eda_agent.")
        assert {m["component_id"]: m["side"] for m in proposal["moves"]} == {"a":"top", "b":"bottom"}


def test_unknown_pad_and_part_geometry_cannot_be_invented_by_the_generator():
    context = example_board_context(); del context["components"][0]["courtyard_local"]
    proposals = generate_placement_candidates(context, options={"candidate_count": 1, "iterations": 3})
    assert "a" in proposals[0]["unqualified_envelope_components"]
    assert not compare_placement_candidates(context, candidates=proposals)["plan"]["apply_eligible"]


def test_identity_digest_changes_with_constraints_and_invalid_numbers_are_refused():
    context = example_board_context()
    before = evaluate_placement(context)
    constraint = {"id": "pins", "check": "pad_distance", "scope": {"pads": ["a.1", "b.1"]}, "limit_mm": 20, "source": SOURCE}
    after = evaluate_placement(context, constraints=[constraint])
    assert before["input_digest"] != after["input_digest"]
    assert before["constraint_digest"] != after["constraint_digest"]
    bad = copy.deepcopy(context); bad["components"][0]["x_mm"] = math.inf
    with pytest.raises(ValueError, match="finite"):
        evaluate_placement(bad)
    with pytest.raises(ValueError, match="designator"):
        evaluate_placement(context, moves=[{"component_id":"a", "designator":"b", "x_mm":18}])
    without_id = dict(before); without_id.pop("evidence_id")
    assert digest(without_id) == before["evidence_id"]



def test_stationary_pad_is_fixed_and_blocks_a_reserved_channel():
    context = example_board_context()
    context['stationary_pads'] = [{'id': 'free-copper', 'x_mm': 20, 'y_mm': 30,
        'width_mm': .4, 'height_mm': .4, 'shape': 'rectangle', 'layer': 'Top', 'net_name': 'OTHER'}]
    before = copy.deepcopy(context['stationary_pads'])
    result = evaluate_placement(context, {'reservations': [reservation()]},
                                moves=[candidate('b', x_mm=19)])
    assert check(result, 'channel:bus')['status'] == 'fail'
    assert 'free-copper' in result['reservations'][0]['obstructions']
    assert context['stationary_pads'] == before
    poses = {c['id']: dict(x_mm=c['x_mm'], y_mm=c['y_mm'], rotation_deg=c['rotation_deg'], side=c['side'])
             for c in context['components']}
    poses['b']['x_mm'] = 19
    pad = transformed_pads(validate_board_context(context), poses)['free-copper']
    assert (pad['x_mm'], pad['y_mm']) == (20, 30)
    # The existing generator may propose moving packages; never the free pad.
    generated = generate_placement_candidates(context, options={'candidate_count': 1, 'iterations': 1})
    assert all(m['component_id'] in {'a', 'b'} for c in generated for m in c['moves'])


@pytest.mark.parametrize('corruption', ['duplicate_id', 'component_binding', 'local_coordinate'])
def test_stationary_pad_contract_rejects_ambiguous_identity_or_pose(corruption):
    context = example_board_context()
    pad = {'id': 'free', 'x_mm': 20, 'y_mm': 30, 'width_mm': .4, 'height_mm': .4,
           'shape': 'rectangle', 'layer': 'Top'}
    if corruption == 'duplicate_id': pad['id'] = context['pads'][0]['id']
    if corruption == 'component_binding': pad['component_id'] = 'a'
    if corruption == 'local_coordinate': pad['local_xy_mm'] = [0, 0]
    context['stationary_pads'] = [pad]
    with pytest.raises(ValueError): validate_board_context(context)
