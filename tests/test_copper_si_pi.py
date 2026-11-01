"""Real 2-D copper counterexamples, independent of Altium/native availability."""
import math
import time

import pytest

from eda_agent.design.copper_audit import audit_board_copper, net_connectivity, CopperGraph
from eda_agent.design.copper_geometry import (
    arc_segments, pad_outline, point_in_ring, point_in_region,
    segment_region_intervals, uncovered_intervals, parallel_coupling, SpatialIndex,
)
from eda_agent.design.net_classes import classify_copper_intent
from eda_agent.design.plan import DesignPlan, Part, Net, PinRef, Sheet


def board(**kwargs):
    return {"complete": True, "missing": [], "layers": [{"name": "Top", "order": 0, "kind": "signal"}],
            "tracks": [], "arcs": [], "vias": [], "pads": [], "regions": [], "net_ties": [],
            "coverage": {k: "complete" for k in ("tracks", "arcs", "vias", "pads", "regions", "layers")}, **kwargs}


def track(id, net, a, b, width=0.2, layer="Top"):
    return {"id": id, "net_name": net, "start": a, "end": b, "width_mm": width, "layer": layer}


def pad(id, net, x, y, **kw):
    comp, pin = id.split(".")
    return {"id": id, "component": comp, "pad_name": pin, "net_name": net, "x_mm": x, "y_mm": y,
            "width_mm": 0.4, "height_mm": 0.4, "layer": "Top", "shape": "rectangle", **kw}


def region(id="plane", layer="GND1", net="G", outline=None, holes=None):
    return {"id": id, "net_name": net, "layer": layer, "poured": True, "kind": "copper",
            "outline": outline or [[-1, -1], [11, -1], [11, 1], [-1, 1]], "holes": holes or []}


def findings(report, check):
    return [v for v in report["violations"] if v["check"] == check]


def test_concave_polygon_and_holes_exact_intervals():
    r = region(outline=[[0, 0], [8, 0], [8, 8], [5, 8], [5, 3], [3, 3], [3, 8], [0, 8]],
               holes=[[[1, 1], [2, 1], [2, 2], [1, 2]]])
    assert not point_in_region((4, 6), r)  # inside AABB, outside actual copper
    assert not point_in_region((1.5, 1.5), r)
    assert point_in_region((2.5, 6), r)
    assert segment_region_intervals((0, 6), (8, 6), r) == [(0, 0.375), (0.625, 1)]
    assert uncovered_intervals((0, 1.5), (8, 1.5), [r]) == [(0.125, 0.25)]


def test_split_reference_crosses_small_hole_and_refuses_unpoured_definition():
    layers = [{"name": "Top", "order": 0, "reference_layer": "GND1"}, {"name": "GND1", "order": 1, "kind": "plane", "refnet": "G"}]
    hole = [[4.9, -0.4], [5.1, -0.4], [5.1, 0.4], [4.9, 0.4]]
    snapshot = board(layers=layers, tracks=[track("t", "CLK", (0, 0), (10, 0))], regions=[region(holes=[hole])])
    intent = {"net_classes": {"CLK": ["HIGH_SPEED_SINGLE"]}}
    report = audit_board_copper(snapshot, intent)
    found = findings(report, "split_reference")
    assert len(found) == 1 and found[0]["measured_value"] == pytest.approx(0.2)
    assert found[0]["x_mm"] == 5
    assert set(found[0]) == {"id", "check", "x_mm", "y_mm", "layer", "net_name", "measured_value", "limit_value", "fix_action", "evidence"}
    snapshot["regions"][0]["poured"] = False
    report = audit_board_copper(snapshot, intent)
    assert not findings(report, "split_reference")
    assert report["status"] == "unknown"


def test_tiny_offcentre_void_under_wide_trace_is_not_missed():
    layers = [{"name": "Top", "order": 0, "reference_layer": "GND1"}, {"name": "GND1", "order": 1, "kind": "plane", "refnet": "G"}]
    hole = [[4.9, 0.15], [5.1, 0.15], [5.1, 0.25], [4.9, 0.25]]
    snapshot = board(layers=layers, tracks=[track("t", "CLK", (0, 0), (10, 0), 1)], regions=[region(holes=[hole])])
    assert findings(audit_board_copper(snapshot, {"net_classes": {"CLK": ["HIGH_SPEED_SINGLE"]}}), "split_reference")


def test_reference_cannot_skip_an_intervening_unassigned_copper_layer():
    snapshot = board(layers=[{'name': 'Top', 'kind': 'signal', 'order': 0},
                             {'name': 'Inner', 'kind': 'signal', 'order': 1},
                             {'name': 'GND1', 'kind': 'plane', 'order': 2, 'reference_net': 'G'}],
                     tracks=[track('t', 'CLK', (0, 0), (10, 0))], regions=[region()])
    report = audit_board_copper(snapshot, {'net_classes': {'CLK': ['HIGH_SPEED_SINGLE'], 'G': ['GROUND']}})
    assert report['status'] == 'unknown' and not report['coverage_complete']
    assert any(u['check'] == 'split_reference' for u in report['unknowns'])


def test_declared_reference_pour_on_signal_layer_and_native_multilayer_cutout():
    snapshot = board(layers=[{'name': 'Top', 'kind': 'signal', 'order': 0},
                             {'name': 'Bottom', 'kind': 'signal', 'order': 1}],
                     tracks=[track('t', 'CLK', (0, 0), (10, 0))],
                     regions=[region(layer='Bottom')], cutouts=[{
                         'id': 'cut', 'kind': 'board_cutout', 'layer': 'MultiLayer',
                         'outline': [[4, -.5], [6, -.5], [6, .5], [4, .5]]}])
    intent = {'net_classes': {'CLK': ['HIGH_SPEED_SINGLE'], 'G': ['GROUND']},
              'constraints': {'CLK': {'reference_layer': 'Bottom', 'reference_net': 'G'}}}
    report = audit_board_copper(snapshot, intent)
    assert findings(report, 'split_reference')
    snapshot['cutouts'] = []
    assert not findings(audit_board_copper(snapshot, intent), 'split_reference')


def test_edge_spacing_and_true_parallel_overlap_not_centre_or_crossing():
    result = parallel_coupling((0, 0), (10, 0), (4, 0.75), (6, 0.75), .2, .2, .6)
    assert result["edge_spacing_mm"] == pytest.approx(.55)
    assert result["coupled_length_mm"] == 2
    assert parallel_coupling((0, 0), (10, 0), (5, -2), (5, 2), .2, .2, .6) is None
    snapshot = board(tracks=[track("v", "A", (0, 0), (10, 0)), track("a", "SW", (4, .75), (6, .75))])
    intent = {"net_classes": {"A": ["SENSITIVE_ANALOG"], "SW": ["AGGRESSIVE_SWITCHING"]}}
    found = findings(audit_board_copper(snapshot, intent), "parallel_coupling")
    assert len(found) == 1 and found[0]["limit_value"] == 1.0
    snapshot["tracks"][1]["start"] = (10.1, .1); snapshot["tracks"][1]["end"] = (20, .1)
    assert not findings(audit_board_copper(snapshot, intent), "parallel_coupling")


def test_differential_pair_internal_gap_not_3w_violation():
    snapshot = board(tracks=[track("p", "P", (0, 0), (10, 0)), track("n", "N", (0, .3), (10, .3))])
    intent = {"net_classes": {"P": ["HIGH_SPEED_DIFF"], "N": ["HIGH_SPEED_DIFF"]}, "differential_pairs": [{"nets": ["P", "N"]}]}
    assert not findings(audit_board_copper(snapshot, intent), "parallel_coupling")


def test_adjacent_layer_parallel_projection_is_audited_but_does_not_skip_a_plane():
    layers = [{'name': 'Top', 'kind': 'signal', 'order': 0},
              {'name': 'Core', 'kind': 'dielectric', 'order': 1, 'thickness_mm': .2, 'er': 4.2},
              {'name': 'Inner', 'kind': 'signal', 'order': 2}]
    data = board(layers=layers, tracks=[track('a', 'A', (0, 0), (10, 0)),
                                        track('s', 'SW', (0, .5), (10, .5), layer='Inner')])
    intent = {'net_classes': {'A': ['SENSITIVE_ANALOG'], 'SW': ['AGGRESSIVE_SWITCHING']}}
    violation = next(v for v in findings(audit_board_copper(data, intent), 'parallel_coupling') if v['net_name'] == 'A')
    assert violation['layer'] == 'Top' and violation['evidence']['aggressor_layer'] == 'Inner'
    assert '2D projection' in violation['evidence']['geometry_basis']
    layers.append({'name': 'GND1', 'kind': 'plane', 'order': 1.5, 'reference_net': 'G'})
    assert not findings(audit_board_copper(data, intent), 'parallel_coupling')


def test_return_via_must_span_both_real_reference_planes():
    layers = [{"name": "Top", "order": 0, "reference_layer": "G1"}, {"name": "G1", "order": 1, "kind": "plane", "refnet": "G"},
              {"name": "Inner", "order": 2}, {"name": "G2", "order": 3, "kind": "plane", "refnet": "G"},
              {"name": "Bottom", "order": 4, "reference_layer": "G2"}]
    sig = {"id": "s", "net_name": "C", "x_mm": 0, "y_mm": 0, "start_layer": "Top", "end_layer": "Bottom", "diameter_mm": .6}
    wrong = {**sig, "id": "g", "net_name": "G", "x_mm": .4, "end_layer": "Inner"}
    snapshot = board(layers=layers, vias=[sig, wrong], regions=[region(layer="G1"), region(layer="G2")])
    intent = {"net_classes": {"C": ["HIGH_SPEED_SINGLE"], "G": ["GROUND"]}}
    assert findings(audit_board_copper(snapshot, intent), "return_via")
    snapshot["vias"][1]["end_layer"] = "Bottom"
    assert not findings(audit_board_copper(snapshot, intent), "return_via")
    snapshot["vias"][1]["x_mm"] = 2
    assert findings(audit_board_copper(snapshot, intent), "return_via")


def test_routed_decoupling_path_uses_detour_not_centroid_distance():
    pads = [pad("U1.1", "P", 0, 0), pad("C1.1", "P", 1, 0), pad("C1.2", "G", 1, -.8)]
    tracks = [track("a", "P", (0, 0), (0, 4)), track("b", "P", (0, 4), (1, 4)), track("c", "P", (1, 4), (1, 0))]
    snapshot = board(pads=pads, tracks=tracks)
    motif = {"kind": "decoupling", "component": "C1", "source_component": "U1", "source_pin": "1", "cap_power_pin": "1", "cap_ground_pin": "2", "net_name": "P", "intent_verified": True}
    report = audit_board_copper(snapshot, {"motifs": [motif], "net_classes": {"P": ["POWER"], "G": ["GROUND"]}})
    found = findings(report, "decoupling_path_length")
    assert found and found[0]["measured_value"] > 8
    snapshot["tracks"] = [track("short", "P", (0, 0), (1, 0))]
    assert not findings(audit_board_copper(snapshot, {"motifs": [motif]}), "decoupling_path_length")


def test_kelvin_branch_detects_shared_load_path_and_accepts_pad_branch():
    pads = [pad("R1.1", "I", 0, 0), pad("J1.1", "I", 6, 0), pad("U1.1", "I", 3, 2)]
    tracks = [track("load", "I", (0, 0), (6, 0), .6), track("sense", "I", (3, 0), (3, 2), .15)]
    motif = {"kind": "current_shunt", "component": "R1", "side": "high", "kelvin_paths": [{"shunt_pad": "R1.1", "load_pad": "J1.1", "sense_pad": "U1.1"}]}
    snapshot = board(pads=pads, tracks=tracks)
    found = findings(audit_board_copper(snapshot, {"motifs": [motif]}), "kelvin_branch")
    assert found and found[0]["measured_value"] > 2
    snapshot["tracks"] = [tracks[0], track("sense1", "I", (0, 0), (0, 2), .15), track("sense2", "I", (0, 2), (3, 2), .15)]
    assert not findings(audit_board_copper(snapshot, {"motifs": [motif]}), "kelvin_branch")


def test_switch_loop_area_requires_closed_physical_loop_and_explicit_limit():
    tracks = [track("a", "S", (0, 0), (4, 0)), track("b", "S", (4, 0), (4, 3)), track("c", "S", (4, 3), (0, 3)), track("d", "S", (0, 3), (0, 0))]
    intent = {"motifs": [{"kind": "switching_loop", "track_ids": ["a", "b", "c", "d"], "max_area_mm2": 10}]}
    found = findings(audit_board_copper(board(tracks=tracks), intent), "switch_loop_area")
    assert found[0]["measured_value"] == 12
    tracks.pop()
    report = audit_board_copper(board(tracks=tracks), intent)
    assert not findings(report, "switch_loop_area") and any(u["check"] == "switch_loop_area" for u in report["unknowns"])


def test_current_track_polygon_neck_and_plating_unknown():
    outline = [[0, 0], [3, 0], [3, 1], [6, 1], [6, 0], [9, 0], [9, 3], [6, 3], [6, 1.4], [3, 1.4], [3, 3], [0, 3]]
    snapshot = board(tracks=[track("thin", "P", (0, 1.2), (9, 1.2), .3)], regions=[region(layer="Top", net="P", outline=outline)],
                     vias=[{"id": "v", "net_name": "P", "x_mm": 1, "y_mm": 1, "diameter_mm": .6, "drill_mm": .3, "start_layer": "Top", "end_layer": "Top"}])
    intent = {"net_classes": {"P": ["HIGH_CURRENT_POWER"]}, "constraints": {"P": {"required_width_mm": .8, "current_a": 3}}}
    report = audit_board_copper(snapshot, intent)
    assert findings(report, "high_current_neck")
    assert findings(report, "high_current_polygon_neck")[0]["measured_value"] == pytest.approx(.4)
    assert any(u["check"] == "high_current_via" for u in report["unknowns"])


def test_rotated_pad_contact_and_poured_region_hole_connectivity():
    p = pad("U1.1", "N", 0, 0, width_mm=3, height_mm=.3, rotation_deg=45)
    assert not point_in_ring((1, -1), pad_outline(p))
    snapshot = board(pads=[p, pad("U2.1", "N", 3, -1)], tracks=[track("a", "N", (1, -1), (3, -1), .1)])
    assert not net_connectivity(snapshot, "N")["connected"]  # inside AABB only
    snapshot = board(pads=[pad("U1.1", "N", 1, 1), pad("U2.1", "N", 7, 1)],
                     regions=[region(layer="Top", net="N", outline=[[0, 0], [8, 0], [8, 5], [0, 5]], holes=[[[3, .5], [5, .5], [5, 2], [3, 2]]])])
    assert net_connectivity(snapshot, "N")["connected"]
    graph = CopperGraph(snapshot, "N")
    path = graph.shortest(graph.pad_nodes["U1.1"], graph.pad_nodes["U2.1"])
    assert path and path["length_mm"] > 6  # straight path crosses a hole


def test_arc_adaptive_error_analytical_length_and_spatial_candidates():
    arc = {"id": "a", "center": [0, 0], "radius_mm": 10, "start_angle_deg": 0, "end_angle_deg": 180, "width_mm": .2}
    chords = arc_segments(arc, .001)
    assert sum(c["analytical_length_mm"] for c in chords) == pytest.approx(10 * math.pi)
    assert max(10 - math.hypot(*((c["start"][i] + c["end"][i]) / 2 for i in (0, 1))) for c in chords) <= .001 + 1e-12
    index = SpatialIndex()
    for i in range(5000):
        index.insert(i, (i * 10, 0, i * 10 + 1, 1))
    assert index.query((10, 0, 11, 1)) == [1]


def test_classification_is_role_and_topology_based_not_net_name():
    plan = DesignPlan(spec="test", summary="test", sheets=[Sheet(name="main")], parts=[Part(refdes="U1", lib_ref="unknown"), Part(refdes="U2", lib_ref="unknown")],
                      nets=[Net(name="SW_GND_VREF_CLK", pins=[PinRef(refdes="U1", pin="1"), PinRef(refdes="U2", pin="1")]),
                            Net(name="unnamed", role="adc_input", pins=[PinRef(refdes="U1", pin="2"), PinRef(refdes="U2", pin="2")])])
    intent = classify_copper_intent(plan)
    assert intent["net_classes"]["SW_GND_VREF_CLK"] == ["UNKNOWN"]
    assert intent["net_classes"]["unnamed"] == ["SENSITIVE_ANALOG"]
    intent = classify_copper_intent(None, {"board_snapshot": {"nets": ["A", "B"]}, "net_roles": {"A": "switch"}})
    assert intent["net_classes"] == {"A": ["AGGRESSIVE_SWITCHING"], "B": ["UNKNOWN"]}


def test_incomplete_coverage_never_claims_clean_or_complete_connectivity():
    snapshot = board(pads=[pad("U1.1", "N", 0, 0)], complete=False, missing=["regions holes unavailable"])
    report = audit_board_copper(snapshot, {"net_classes": {"N": ["POWER"]}})
    assert report["status"] == "unknown" and not report["coverage_complete"]
    assert not net_connectivity(snapshot, "N")["complete"]


def test_switching_device_terminal_cycle_uses_copper_and_actual_pad_spans():
    pads = [pad("C1.1", "V", 0, 0), pad("Q1.1", "V", 4, 0), pad("Q1.2", "S", 4, 3),
            pad("D1.1", "S", 0, 3), pad("D1.2", "G", 0, 2), pad("C1.2", "G", 0, 1)]
    tracks = [track("v", "V", (0, 0), (4, 0)), track("s", "S", (4, 3), (0, 3)), track("g", "G", (0, 2), (0, 1))]
    motif = {"kind": "switching_loop", "terminal_cycle": [p["id"] for p in pads], "max_area_mm2": 10}
    report = audit_board_copper(board(pads=pads, tracks=tracks), {"motifs": [motif]})
    assert findings(report, "switch_loop_area")[0]["measured_value"] == pytest.approx(12)


def test_short_same_net_blind_via_does_not_prove_cross_layer_connectivity():
    layers = [{"name": "Top", "order": 0}, {"name": "Inner", "order": 1}, {"name": "Bottom", "order": 2}]
    pads = [pad("U1.1", "N", 0, 0), pad("U2.1", "N", 0, 0, layer="Bottom")]
    via = {"id": "v", "net_name": "N", "x_mm": .1, "y_mm": 0, "diameter_mm": .4, "start_layer": "Top", "end_layer": "Inner"}
    snapshot = board(layers=layers, pads=pads, vias=[via])
    assert not net_connectivity(snapshot, "N")["connected"]
    via["end_layer"] = "Bottom"
    assert net_connectivity(snapshot, "N")["connected"]


def test_high_and_low_side_shunts_need_explicit_roles_not_resistor_value():
    parts = [Part(refdes="R1", lib_ref="R", value="0.01", role="current_shunt"),
             Part(refdes="R2", lib_ref="R", value="0.01", role="high_side_shunt"),
             Part(refdes="R3", lib_ref="R", value="0.01")]
    nets = [Net(name="G", is_ground=True, pins=[PinRef(refdes="R1", pin="1"), PinRef(refdes="R3", pin="1")]),
            Net(name="I", role="high_current", pins=[PinRef(refdes="R1", pin="2"), PinRef(refdes="R2", pin="1")]),
            Net(name="P", is_power=True, pins=[PinRef(refdes="R2", pin="2"), PinRef(refdes="R3", pin="2")])]
    plan = DesignPlan(spec="shunt", summary="shunt", sheets=[Sheet(name="main")], parts=parts, nets=nets)
    found = [m for m in classify_copper_intent(plan)["motifs"] if m["kind"] == "current_shunt"]
    assert {m["component"]: m["side"] for m in found} == {"R1": "low", "R2": "high"}
    assert all(not m["kelvin_mapping_known"] for m in found)


def test_constraint_width_clearance_and_endpoint_length_checks():
    pads = [pad("U1.1", "A", 0, 0), pad("U2.1", "A", 4, 0), pad("U1.2", "B", 0, 1), pad("U2.2", "B", 2, 1)]
    tracks = [track("a", "A", (0, 0), (4, 0), .4), track("b", "B", (0, 1), (2, 1), .2)]
    intent = {"constraints": {"A": {"max_width_mm": .3, "match_group": "bus", "length_tolerance_mm": .1},
                               "B": {"required_width_mm": .3, "match_group": "bus", "length_tolerance_mm": .1}},
              "clearance_matrix": [{"net_a": "A", "net_b": "B", "clearance_mm": .8}]}
    report = audit_board_copper(board(pads=pads, tracks=tracks), intent)
    assert findings(report, "maximum_trace_width") and findings(report, "required_trace_width")
    assert findings(report, "constraint_clearance") and findings(report, "length_matching")


def test_sparse_track_audit_performance_boundary():
    tracks = [track(str(i), "A", (i * 10, 0), (i * 10 + 1, 0)) for i in range(5000)]
    start = time.perf_counter()
    report = audit_board_copper(board(tracks=tracks), {"net_classes": {"A": ["SENSITIVE_ANALOG"]}})
    assert not report["violations"]
    assert time.perf_counter() - start < 8  # 25M pair checks would violate this


def test_long_coupled_arcs_aggregate_short_chords_and_preserve_error_bound():
    base = {"center": [0, 0], "start_angle_deg": 0, "end_angle_deg": 90, "width_mm": .2, "layer": "Top"}
    arcs = [{**base, "id": "victim", "net_name": "A", "radius_mm": 10},
            {**base, "id": "switch", "net_name": "S", "radius_mm": 10.7}]
    intent = {"net_classes": {"A": ["SENSITIVE_ANALOG"], "S": ["AGGRESSIVE_SWITCHING"]}}
    report = audit_board_copper(board(arcs=arcs), intent)
    found = findings(report, "parallel_coupling")
    assert len(found) == 1
    assert found[0]["evidence"]["coupled_length_mm"] > 10
    assert found[0]["evidence"]["track_id"] == "victim"
    assert found[0]["evidence"]["arc_error_bound_mm"] == .004


def test_kelvin_pad_side_net_tie_passes_and_remote_net_tie_shares_load_copper():
    pads = [pad("R1.1", "I", 0, 0), pad("J1.1", "I", 6, 0), pad("U1.1", "S", 0, 2),
            pad("NT1.1", "I", .1, 0), pad("NT1.2", "S", .1, .1)]
    motif = {"kind": "current_shunt", "component": "R1", "side": "low", "kelvin_paths": [
        {"sense_pad": "U1.1", "shunt_pad": "R1.1", "load_pad": "J1.1"}]}
    tie = {"id": "nt", "pad_ids": ["NT1.1", "NT1.2"]}
    tracks = [track("load", "I", (0, 0), (6, 0), .6), track("sense", "S", (.1, .1), (0, 2), .15)]
    snapshot = board(pads=pads, tracks=tracks, net_ties=[tie])
    assert not findings(audit_board_copper(snapshot, {"motifs": [motif]}), "kelvin_branch")
    pads[3]["x_mm"] = pads[4]["x_mm"] = 3
    tracks[1]["start"] = (3, .1)
    assert findings(audit_board_copper(snapshot, {"motifs": [motif]}), "kelvin_branch")
    snapshot["net_ties"] = []
    report = audit_board_copper(snapshot, {"motifs": [motif]})
    assert any(u["check"] == "kelvin_branch" for u in report["unknowns"])


def test_invalid_polygon_and_explicit_unknown_tag_cannot_produce_clean():
    crossed = [[0, 0], [4, 3], [0, 3], [4, 0]]
    snapshot = board(regions=[region(layer="Top", net="P", outline=crossed)])
    report = audit_board_copper(snapshot, {"net_classes": {"P": ["POWER"]}})
    assert report["status"] == "unknown" and any(u["check"] == "geometry" for u in report["unknowns"])
    report = audit_board_copper(board(tracks=[track("a", "U", (0, 0), (1, 0))]), {"net_classes": {"U": ["UNKNOWN"]}})
    assert report["status"] == "unknown"


def test_per_layer_width_overrides_general_width():
    snapshot = board(tracks=[track("t", "P", (0, 0), (4, 0), .4)])
    intent = {"net_classes": {"P": ["HIGH_CURRENT_POWER"]}, "constraints": {"P": {
        "required_width_mm": .8, "layer_widths": {"Top": {"required_width_mils": 10}}}}}
    assert not findings(audit_board_copper(snapshot, intent), "high_current_neck")


def test_wide_same_net_pour_reinforces_embedded_narrow_track():
    snapshot = board(tracks=[track("t", "P", (0, 0), (8, 0), .2)],
                     regions=[region(layer="Top", net="P", outline=[[-1, -1], [9, -1], [9, 1], [-1, 1]])])
    intent = {"net_classes": {"P": ["HIGH_CURRENT_POWER"]}, "constraints": {"P": {"required_width_mm": .8}}}
    assert not findings(audit_board_copper(snapshot, intent), "high_current_neck")
    snapshot["regions"][0]["net_name"] = "OTHER"
    assert findings(audit_board_copper(snapshot, intent), "high_current_neck")


def test_reference_plane_must_cover_trace_end_caps():
    layers = [{"name": "Top", "order": 0, "reference_layer": "GND1"}, {"name": "GND1", "order": 1, "kind": "plane", "refnet": "G"}]
    snapshot = board(layers=layers, tracks=[track("t", "C", (0, 0), (10, 0), 1)],
                     regions=[region(outline=[[-1, -1], [10.2, -1], [10.2, 1], [-1, 1]])])
    intent = {"net_classes": {"C": ["HIGH_SPEED_SINGLE"], "G": ["GROUND"]}}
    found = findings(audit_board_copper(snapshot, intent), "split_reference")
    assert found and found[0]["x_mm"] > 10.2 and found[0]["measured_value"] > .29
